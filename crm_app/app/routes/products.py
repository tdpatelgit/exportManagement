"""
app/routes/products.py
------------------------
Product catalog, three levels deep:
  - PRODUCT: the tax/HSN identity (name, description, HSN code, GST/IGST/
    SGST/CGST percentages) AND the physical packing spec (packing, quantity,
    alternate quantity, unit, weight class) that quotations, proforma
    invoices and packing lists all read from - every design under a product
    shares the same packing spec.
  - FOLDER: organises designs inside a product; folders nest to any depth
    but can only be created under a product.
  - DESIGN: the sellable leaf holding price and photos - what packing lists
    pick alongside the row's product.
Everyone signed in can browse; only admins can create/edit/delete.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort, jsonify

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

products_bp = Blueprint("products", __name__, url_prefix="/products")


def _int_or_none(value):
    return int(value) if value not in (None, "", "None") else None


def _product_form_fields(form) -> dict:
    return {
        "product_name": form.get("product_name", ""),
        "description": form.get("description", ""),
        "hsn_code": form.get("hsn_code", ""),
        "gst_percent": form.get("gst_percent", ""),
        "igst_percent": form.get("igst_percent", ""),
        "sgst_percent": form.get("sgst_percent", ""),
        "cgst_percent": form.get("cgst_percent", ""),
        "packing": form.get("packing", ""),
        "quantity": form.get("quantity", ""),
        "alternate_quantity": form.get("alternate_quantity", ""),
        "unit": form.get("unit", ""),
        "weight_class": form.get("weight_class", ""),
    }


def _design_form_fields(form) -> dict:
    return {
        "design_name": form.get("design_name", ""),
        "description": form.get("description", ""),
        "price_usd": form.get("price_usd", ""),
        "alt_text": form.get("alt_text", ""),
    }


# ============================================================
# PRODUCTS (catalog root)
# ============================================================
@products_bp.route("/")
@login_required
def list_products():
    products = current_app.container.product_service.list_products(g.user.company_id)
    return render_template("products/list.html", products=products)


@products_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_product():
    container = current_app.container
    if request.method == "POST":
        try:
            product = container.product_service.create_product(current_user=g.user, **_product_form_fields(request.form))
            flash(f"Product '{product.product_name}' added.", "success")
            return redirect(url_for("products.view_product", product_id=product.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template("products/product_form.html", product=None, form_data=request.form), 400
    return render_template("products/product_form.html", product=None, form_data=None)


@products_bp.route("/<int:product_id>")
@products_bp.route("/<int:product_id>/folder/<int:folder_id>")
@login_required
def view_product(product_id, folder_id=None):
    """The product page doubles as the folder browser: it shows the
    product's tax identity plus whichever folder level is open."""
    container = current_app.container
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
        subfolders, designs = container.product_service.list_contents(g.user.company_id, product_id, folder_id)
        current_folder = container.product_service.get_folder(folder_id, g.user.company_id) if folder_id else None
    except NotFoundError:
        abort(404)
    breadcrumb = container.product_service.breadcrumb(g.user.company_id, folder_id)
    return render_template(
        "products/detail.html", product=product, current_folder=current_folder,
        breadcrumb=breadcrumb, subfolders=subfolders, designs=designs,
    )


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
                current_user=g.user, product_id=product_id, **_product_form_fields(request.form)
            )
            flash("Product updated.", "success")
            return redirect(url_for("products.view_product", product_id=product_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    return render_template("products/product_form.html", product=product, form_data=None)


@products_bp.route("/<int:product_id>/delete", methods=["POST"])
@admin_required
def delete_product(product_id):
    container = current_app.container
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
        container.product_service.delete_product(g.user, product_id)
        flash(f"Product '{product.product_name}' and everything inside it was deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("products.list_products"))


# ============================================================
# FOLDERS (only ever inside a product)
# ============================================================
@products_bp.route("/<int:product_id>/folder/new", methods=["GET", "POST"])
@admin_required
def new_folder(product_id):
    container = current_app.container
    parent_id = _int_or_none(request.args.get("parent_id") or request.form.get("parent_id"))
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
        parent = container.product_service.get_folder(parent_id, g.user.company_id) if parent_id else None
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            folder = container.product_service.create_folder(
                current_user=g.user, product_id=product_id,
                name=request.form.get("name", ""), parent_id=parent_id,
            )
            flash(f"Folder '{folder.name}' created.", "success")
            return redirect(url_for("products.view_product", product_id=product_id, folder_id=folder.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    return render_template("products/folder_form.html", folder=None, product=product, parent=parent)


@products_bp.route("/folder/<int:folder_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_folder(folder_id):
    container = current_app.container
    try:
        folder = container.product_service.get_folder(folder_id, g.user.company_id)
        product = container.product_service.get_product(folder.product_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.product_service.rename_folder(g.user, folder_id, request.form.get("name", ""))
            flash("Folder renamed.", "success")
            return redirect(url_for("products.view_product", product_id=folder.product_id,
                                     folder_id=folder.parent_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    parent = container.product_service.get_folder(folder.parent_id, g.user.company_id) if folder.parent_id else None
    return render_template("products/folder_form.html", folder=folder, product=product, parent=parent)


@products_bp.route("/folder/<int:folder_id>/delete", methods=["POST"])
@admin_required
def delete_folder(folder_id):
    container = current_app.container
    try:
        folder = container.product_service.get_folder(folder_id, g.user.company_id)
        container.product_service.delete_folder(g.user, folder_id)
        flash(f"Folder '{folder.name}' and everything inside it was deleted.", "success")
        return redirect(url_for("products.view_product", product_id=folder.product_id,
                                 folder_id=folder.parent_id))
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("products.list_products"))


# ============================================================
# DESIGNS (the leaves - price/packing/photos live here)
# ============================================================
@products_bp.route("/<int:product_id>/design/new", methods=["GET", "POST"])
@admin_required
def new_design(product_id):
    container = current_app.container
    folder_id = _int_or_none(request.args.get("folder_id") or request.form.get("folder_id"))
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
        folder = container.product_service.get_folder(folder_id, g.user.company_id) if folder_id else None
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            design = container.product_service.create_design(
                current_user=g.user, product_id=product_id, folder_id=folder_id,
                photo_file=request.files.get("photo"),
                dimension_photo_file=request.files.get("dimension_photo"),
                **_design_form_fields(request.form),
            )
            flash(f"Design '{design.design_name}' added.", "success")
            return redirect(url_for("products.view_design", design_id=design.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template("products/design_form.html", design=None, product=product,
                                    folder=folder, form_data=request.form), 400

    return render_template("products/design_form.html", design=None, product=product,
                            folder=folder, form_data=None)


@products_bp.route("/design/<int:design_id>")
@login_required
def view_design(design_id):
    container = current_app.container
    try:
        design = container.product_service.get_design(design_id, g.user.company_id)
        product = container.product_service.get_product(design.product_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    breadcrumb = container.product_service.breadcrumb(g.user.company_id, design.folder_id)
    return render_template("products/design_detail.html", design=design, product=product, breadcrumb=breadcrumb)


@products_bp.route("/design/<int:design_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_design(design_id):
    container = current_app.container
    try:
        design = container.product_service.get_design(design_id, g.user.company_id)
        product = container.product_service.get_product(design.product_id, g.user.company_id)
        folder = container.product_service.get_folder(design.folder_id, g.user.company_id) if design.folder_id else None
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.product_service.update_design(
                current_user=g.user, design_id=design_id,
                photo_file=request.files.get("photo"),
                dimension_photo_file=request.files.get("dimension_photo"),
                **_design_form_fields(request.form),
            )
            flash("Design updated.", "success")
            return redirect(url_for("products.view_design", design_id=design_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    return render_template("products/design_form.html", design=design, product=product,
                            folder=folder, form_data=None)


@products_bp.route("/design/<int:design_id>/delete", methods=["POST"])
@admin_required
def delete_design(design_id):
    container = current_app.container
    try:
        design = container.product_service.get_design(design_id, g.user.company_id)
        container.product_service.delete_design(g.user, design_id)
        flash(f"Design '{design.design_name}' deleted.", "success")
        return redirect(url_for("products.view_product", product_id=design.product_id,
                                 folder_id=design.folder_id))
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("products.list_products"))


# ============================================================
# JSON APIs (power the pickers on the quotation / proforma /
# packing list forms)
# ============================================================
def _product_json(p) -> dict:
    return {
        "id": p.id, "name": p.product_name, "description": p.description,
        "hsn_code": p.hsn_code, "gst_percent": p.gst_percent,
        "igst_percent": p.igst_percent, "sgst_percent": p.sgst_percent,
        "cgst_percent": p.cgst_percent,
        "packing": p.packing, "quantity": p.quantity, "alternate_quantity": p.alternate_quantity,
        "unit": p.unit, "weight_class": p.weight_class,
    }


@products_bp.route("/api/list")
@login_required
def api_list_products():
    """Flat product list for the product picker on the quotation, proforma
    and packing list forms - a product is picked directly (no tree to walk),
    its name/HSN/packing-spec prefill the line item."""
    products = current_app.container.product_service.list_products(g.user.company_id)
    return jsonify({"products": [_product_json(p) for p in products]})


@products_bp.route("/api/quick-create", methods=["POST"])
@admin_required
def api_quick_create():
    """Lets an admin add a brand new catalog product without leaving the
    product picker modal (used from the quotation/proforma/packing-list
    forms), so a missing product doesn't force a detour to the full
    Products page. Folders/designs can be added later from the product's page."""
    container = current_app.container
    try:
        product = container.product_service.create_product(
            current_user=g.user, **_product_form_fields(request.form)
        )
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_product_json(product))


@products_bp.route("/api/<int:product_id>/designs")
@login_required
def api_browse_designs(product_id):
    """JSON folder browser scoped to one product, used by the design-picker
    modal on the packing list form - it navigates the product's folder tree
    in place and returns the designs at each level. Designs no longer carry
    a packing spec of their own (that lives on the product, picked
    separately) - just their name/price/photo."""
    container = current_app.container
    folder_id = _int_or_none(request.args.get("folder_id"))
    try:
        product = container.product_service.get_product(product_id, g.user.company_id)
        subfolders, designs = container.product_service.list_contents(g.user.company_id, product_id, folder_id)
    except NotFoundError:
        return jsonify({"error": "not found"}), 404
    breadcrumb = container.product_service.breadcrumb(g.user.company_id, folder_id)
    return jsonify({
        "product": _product_json(product),
        "breadcrumb": [{"id": f.id, "name": f.name} for f in breadcrumb],
        "subfolders": [{"id": f.id, "name": f.name} for f in subfolders],
        "designs": [
            {
                "id": d.id, "name": d.design_name, "price_usd": d.price_usd,
                "photo_url": url_for("static", filename=d.photo_path) if d.photo_path else None,
            }
            for d in designs
        ],
    })
