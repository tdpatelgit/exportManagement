"""
app/routes/packing_lists.py
----------------------------
Packing Details document: mirrors app/routes/proforma_invoices.py layer for
layer. A packing list is normally started from an existing Proforma Invoice
via `?proforma_invoice_id=` (or generated automatically right after the PI
is created - see the auto_packing hook in routes/proforma_invoices.py), but
is then its own independent, editable record.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required

packing_lists_bp = Blueprint("packing_lists", __name__, url_prefix="/packing-lists")

_HEADER_FIELDS = ["packing_date", "proforma_invoice_id", "lead_id", "remarks"]


def _extract_header(form) -> dict:
    return {key: form.get(key, "") for key in _HEADER_FIELDS}


def _extract_items(form) -> list:
    descriptions = form.getlist("item_description[]")
    box_per_pallets = form.getlist("item_box_per_pallet[]")
    model_names = form.getlist("item_model_name[]")
    no_of_pallets = form.getlist("item_no_of_pallet[]")
    boxes = form.getlist("item_boxes[]")
    pcs = form.getlist("item_pcs[]")
    quantities = form.getlist("item_quantity_value[]")
    items = []
    for i in range(len(descriptions)):
        items.append({
            "description": descriptions[i],
            "box_per_pallet": box_per_pallets[i] if i < len(box_per_pallets) else "",
            "model_name": model_names[i] if i < len(model_names) else "",
            "no_of_pallet": no_of_pallets[i] if i < len(no_of_pallets) else "",
            "boxes": boxes[i] if i < len(boxes) else "",
            "pcs": pcs[i] if i < len(pcs) else "",
            "quantity_value": quantities[i] if i < len(quantities) else "",
        })
    return items


def _invoice_options():
    return current_app.container.proforma_invoice_service.list_all(g.user.company_id)


@packing_lists_bp.route("/")
@login_required
def list_packing_lists():
    packing_lists = current_app.container.packing_list_service.list_all(g.user.company_id)
    return render_template("packing_lists/list.html", packing_lists=packing_lists)


@packing_lists_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_packing_list():
    container = current_app.container
    if request.method == "POST":
        try:
            packing_list = container.packing_list_service.create(
                current_user=g.user, fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash("Packing details created.", "success")
            return redirect(url_for("packing_lists.view_packing_list", packing_list_id=packing_list.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template(
                "packing_lists/form.html", packing_list=None, invoices=_invoice_options(),
                form_data=request.form, form_items=_extract_items(request.form),
                today=date.today().isoformat(),
            ), 400

    prefill = None
    form_items = None
    proforma_invoice_id = request.args.get("proforma_invoice_id")
    if proforma_invoice_id:
        try:
            invoice = container.proforma_invoice_service.get(int(proforma_invoice_id), g.user.company_id)
            built = container.packing_list_service.build_prefill_from_invoice(invoice)
            prefill = built["fields"]
            form_items = built["items"]
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "packing_lists/form.html", packing_list=None, invoices=_invoice_options(),
        form_data=prefill, form_items=form_items, today=date.today().isoformat(),
    )


@packing_lists_bp.route("/<int:packing_list_id>")
@login_required
def view_packing_list(packing_list_id):
    container = current_app.container
    try:
        packing_list = container.packing_list_service.get(packing_list_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    return render_template("packing_lists/print.html", packing_list=packing_list, company=company)


@packing_lists_bp.route("/<int:packing_list_id>/edit", methods=["GET", "POST"])
@login_required
def edit_packing_list(packing_list_id):
    container = current_app.container
    try:
        packing_list = container.packing_list_service.get(packing_list_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.packing_list_service.update(
                current_user=g.user, packing_list_id=packing_list_id,
                fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash("Packing details updated.", "success")
            return redirect(url_for("packing_lists.view_packing_list", packing_list_id=packing_list_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template(
                "packing_lists/form.html", packing_list=packing_list, invoices=_invoice_options(),
                form_data=request.form, form_items=_extract_items(request.form),
                today=date.today().isoformat(),
            ), 400

    return render_template(
        "packing_lists/form.html", packing_list=packing_list, invoices=_invoice_options(),
        form_data=None, form_items=None, today=date.today().isoformat(),
    )


@packing_lists_bp.route("/<int:packing_list_id>/delete", methods=["POST"])
@login_required
def delete_packing_list(packing_list_id):
    try:
        current_app.container.packing_list_service.delete(g.user, packing_list_id)
        flash("Packing details deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("packing_lists.list_packing_lists"))
