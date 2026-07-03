"""
app/routes/products.py
------------------------
Product catalog: a folder-tree of groups (any depth of subgroups) holding
products (the "files"). Everyone signed in can browse; only admins can
create/edit/delete groups and products.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort, jsonify

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

products_bp = Blueprint("products", __name__, url_prefix="/products")


def _int_or_none(value):
    return int(value) if value not in (None, "", "None") else None


@products_bp.route("/")
@products_bp.route("/group/<int:group_id>")
@login_required
def browse(group_id=None):
    container = current_app.container
    try:
        current_group = container.product_service.get_group(group_id, g.user.company_id) if group_id else None
    except NotFoundError:
        abort(404)
    breadcrumb = container.product_service.breadcrumb(g.user.company_id, group_id)
    subgroups, products = container.product_service.list_contents(g.user.company_id, group_id)
    return render_template(
        "products/browse.html", current_group=current_group, breadcrumb=breadcrumb,
        subgroups=subgroups, products=products,
    )


@products_bp.route("/api/browse")
@login_required
def api_browse():
    """JSON version of the folder browser, used by the product-picker modal
    on the quotation form so it can navigate the same folder tree in place
    instead of a flat dropdown."""
    container = current_app.container
    group_id = _int_or_none(request.args.get("group_id"))
    try:
        current_group = container.product_service.get_group(group_id, g.user.company_id) if group_id else None
    except NotFoundError:
        return jsonify({"error": "not found"}), 404
    breadcrumb = container.product_service.breadcrumb(g.user.company_id, group_id)
    subgroups, products = container.product_service.list_contents(g.user.company_id, group_id)
    return jsonify({
        "current_group": {"id": current_group.id, "name": current_group.name} if current_group else None,
        "breadcrumb": [{"id": g.id, "name": g.name} for g in breadcrumb],
        "subgroups": [{"id": g.id, "name": g.name} for g in subgroups],
        "products": [
            {
                "id": p.id, "name": p.product_name, "hsn_code": p.hsn_code, "packing": p.packing,
                "quantity": p.quantity, "alternate_quantity": p.alternate_quantity,
                "weight_class": p.weight_class, "price_usd": p.price_usd,
                "photo_url": url_for("static", filename=p.photo_path) if p.photo_path else None,
            }
            for p in products
        ],
    })


@products_bp.route("/api/quick-create", methods=["POST"])
@admin_required
def api_quick_create():
    """Lets an admin add a brand new catalog product without leaving the
    product picker modal (used from the quotation form), so a missing
    product doesn't force a detour to the full Products page. Deliberately
    skips photo uploads to keep the inline form quick - those can be added
    later from the full product edit page."""
    container = current_app.container
    group_id = _int_or_none(request.form.get("group_id"))
    try:
        product = container.product_service.create_product(
            current_user=g.user, group_id=group_id,
            product_name=request.form.get("product_name", ""),
            description="",
            hsn_code=request.form.get("hsn_code", ""),
            packing=request.form.get("packing", ""),
            quantity=request.form.get("quantity", ""),
            alternate_quantity=request.form.get("alternate_quantity", ""),
            weight_class=request.form.get("weight_class", ""),
            price_usd=request.form.get("price_usd", ""),
            alt_text="",
            photo_file=None, dimension_photo_file=None,
        )
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    except NotFoundError:
        return jsonify({"error": "That folder no longer exists."}), 404
    return jsonify({
        "id": product.id, "name": product.product_name, "hsn_code": product.hsn_code,
        "packing": product.packing, "quantity": product.quantity,
        "alternate_quantity": product.alternate_quantity, "weight_class": product.weight_class,
        "price_usd": product.price_usd,
        "photo_url": url_for("static", filename=product.photo_path) if product.photo_path else None,
    })


@products_bp.route("/group/new", methods=["GET", "POST"])
@admin_required
def new_group():
    parent_id = _int_or_none(request.args.get("parent_id") or request.form.get("parent_id"))
    container = current_app.container
    if request.method == "POST":
        try:
            group = container.product_service.create_group(
                current_user=g.user, name=request.form.get("name", ""), parent_id=parent_id,
            )
            flash(f"Group '{group.name}' created.", "success")
            return redirect(url_for("products.browse", group_id=group.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
        except NotFoundError:
            abort(404)

    parent = container.product_service.get_group(parent_id, g.user.company_id) if parent_id else None
    return render_template("products/group_form.html", group=None, parent=parent)


@products_bp.route("/group/<int:group_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_group(group_id):
    container = current_app.container
    try:
        group = container.product_service.get_group(group_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.product_service.rename_group(g.user, group_id, request.form.get("name", ""))
            flash("Group renamed.", "success")
            return redirect(url_for("products.browse", group_id=group.parent_id) if group.parent_id
                             else url_for("products.browse"))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    parent = container.product_service.get_group(group.parent_id, g.user.company_id) if group.parent_id else None
    return render_template("products/group_form.html", group=group, parent=parent)


@products_bp.route("/group/<int:group_id>/delete", methods=["POST"])
@admin_required
def delete_group(group_id):
    container = current_app.container
    try:
        group = container.product_service.get_group(group_id, g.user.company_id)
        parent_id = group.parent_id
        container.product_service.delete_group(g.user, group_id)
        flash(f"Group '{group.name}' and everything inside it was deleted.", "success")
        return redirect(url_for("products.browse", group_id=parent_id) if parent_id
                         else url_for("products.browse"))
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("products.browse"))


@products_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_product():
    container = current_app.container
    group_id = _int_or_none(request.args.get("group_id") or request.form.get("group_id"))

    if request.method == "POST":
        try:
            product = container.product_service.create_product(
                current_user=g.user, group_id=group_id,
                product_name=request.form.get("product_name", ""),
                description=request.form.get("description", ""),
                hsn_code=request.form.get("hsn_code", ""),
                packing=request.form.get("packing", ""),
                quantity=request.form.get("quantity", ""),
                alternate_quantity=request.form.get("alternate_quantity", ""),
                weight_class=request.form.get("weight_class", ""),
                price_usd=request.form.get("price_usd", ""),
                alt_text=request.form.get("alt_text", ""),
                photo_file=request.files.get("photo"),
                dimension_photo_file=request.files.get("dimension_photo"),
            )
            flash(f"Product '{product.product_name}' added.", "success")
            return redirect(url_for("products.view_product", product_id=product.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template("products/product_form.html", product=None, group_id=group_id,
                                    form_data=request.form), 400
        except NotFoundError:
            abort(404)

    return render_template("products/product_form.html", product=None, group_id=group_id, form_data=None)


@products_bp.route("/<int:product_id>")
@login_required
def view_product(product_id):
    container = current_app.container
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    breadcrumb = container.product_service.breadcrumb(g.user.company_id, product.group_id)
    return render_template("products/detail.html", product=product, breadcrumb=breadcrumb)


@products_bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_product(product_id):
    container = current_app.container
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.product_service.update_product(
                current_user=g.user, product_id=product_id,
                product_name=request.form.get("product_name", ""),
                description=request.form.get("description", ""),
                hsn_code=request.form.get("hsn_code", ""),
                packing=request.form.get("packing", ""),
                quantity=request.form.get("quantity", ""),
                alternate_quantity=request.form.get("alternate_quantity", ""),
                weight_class=request.form.get("weight_class", ""),
                price_usd=request.form.get("price_usd", ""),
                alt_text=request.form.get("alt_text", ""),
                photo_file=request.files.get("photo"),
                dimension_photo_file=request.files.get("dimension_photo"),
            )
            flash("Product updated.", "success")
            return redirect(url_for("products.view_product", product_id=product_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    return render_template("products/product_form.html", product=product, group_id=product.group_id, form_data=None)


@products_bp.route("/<int:product_id>/delete", methods=["POST"])
@admin_required
def delete_product(product_id):
    container = current_app.container
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
        group_id = product.group_id
        container.product_service.delete_product(g.user, product_id)
        flash(f"Product '{product.product_name}' deleted.", "success")
        return redirect(url_for("products.browse", group_id=group_id) if group_id else url_for("products.browse"))
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("products.browse"))
