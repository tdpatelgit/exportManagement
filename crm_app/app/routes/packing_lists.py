"""
app/routes/packing_lists.py
----------------------------
Packing list generation: mirrors app/routes/proforma_invoices.py layer for
layer. A packing list is normally started from an existing Proforma Invoice
via `?proforma_invoice_id=` (the same way a proforma starts from a
quotation) - each product line from the proforma is then broken down into
one or more DESIGN rows in smaller quantities. The packing list number is
auto-generated as PL{YYYYMMDD}{seq-of-that-day} and is never user-editable.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required

packing_lists_bp = Blueprint("packing_lists", __name__, url_prefix="/packing-lists")

_HEADER_FIELDS = [
    "packing_list_date", "lead_id", "proforma_invoice_id", "export_ref_no", "buyer_order_no",
    "other_reference", "consignee_name", "consignee_address", "notify_name", "notify_address",
    "country_of_origin", "country_of_destination", "vessel_flight",
    "port_of_loading", "port_of_discharge", "final_destination",
    "container_details", "terms_of_delivery", "remarks",
]


def _extract_header(form) -> dict:
    return {key: form.get(key, "") for key in _HEADER_FIELDS}


def _extract_items(form) -> list:
    product_ids = form.getlist("item_product_id[]")
    product_names = form.getlist("item_product_name[]")
    design_ids = form.getlist("item_design_id[]")
    design_names = form.getlist("item_design_name[]")
    hsn_codes = form.getlist("item_hsn_code[]")
    pallets = form.getlist("item_pallets[]")
    boxes = form.getlist("item_quantity_boxes[]")
    values = form.getlist("item_quantity_value[]")
    units = form.getlist("item_unit[]")
    net_weights = form.getlist("item_net_weight_kg[]")
    gross_weights = form.getlist("item_gross_weight_kg[]")
    items = []
    for i in range(len(product_names)):
        items.append({
            "product_id": product_ids[i] if i < len(product_ids) else "",
            "product_name": product_names[i],
            "design_id": design_ids[i] if i < len(design_ids) else "",
            "design_name": design_names[i] if i < len(design_names) else "",
            "hsn_code": hsn_codes[i] if i < len(hsn_codes) else "",
            "pallets": pallets[i] if i < len(pallets) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
            "quantity_value": values[i] if i < len(values) else "",
            "unit": units[i] if i < len(units) else "SQM",
            "net_weight_kg": net_weights[i] if i < len(net_weights) else "",
            "gross_weight_kg": gross_weights[i] if i < len(gross_weights) else "",
        })
    return items


def _form_context():
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    invoices = container.proforma_invoice_service.list_all(g.user.company_id)
    return leads, invoices


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
            flash(f"Packing list {packing_list.packing_list_number} created.", "success")
            return redirect(url_for("packing_lists.view_packing_list", packing_list_id=packing_list.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, invoices = _form_context()
            return render_template(
                "packing_lists/form.html", packing_list=None, leads=leads, invoices=invoices,
                form_data=request.form, form_items=_extract_items(request.form),
                today=date.today().isoformat(),
            ), 400

    leads, invoices = _form_context()
    prefill = None
    form_items = None
    proforma_invoice_id = request.args.get("proforma_invoice_id")
    lead_id = request.args.get("lead_id")
    if proforma_invoice_id:
        try:
            invoice = container.proforma_invoice_service.get(int(proforma_invoice_id), g.user.company_id)
            built = container.packing_list_service.build_prefill_from_proforma(invoice)
            prefill = built["fields"]
            prefill["packing_list_date"] = date.today().isoformat()
            form_items = built["items"]
        except (NotFoundError, ValueError):
            pass
    elif lead_id:
        try:
            lead = container.lead_service.get(int(lead_id), g.user.company_id)
            prefill = {
                "lead_id": lead.id, "consignee_name": lead.company_name,
                "packing_list_date": date.today().isoformat(),
            }
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "packing_lists/form.html", packing_list=None, leads=leads, invoices=invoices,
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
            flash(f"Packing list {packing_list.packing_list_number} updated.", "success")
            return redirect(url_for("packing_lists.view_packing_list", packing_list_id=packing_list_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, invoices = _form_context()
            return render_template(
                "packing_lists/form.html", packing_list=packing_list, leads=leads, invoices=invoices,
                form_data=request.form, form_items=_extract_items(request.form),
                today=date.today().isoformat(),
            ), 400

    leads, invoices = _form_context()
    return render_template(
        "packing_lists/form.html", packing_list=packing_list, leads=leads, invoices=invoices,
        form_data=None, form_items=None, today=date.today().isoformat(),
    )


@packing_lists_bp.route("/<int:packing_list_id>/delete", methods=["POST"])
@login_required
def delete_packing_list(packing_list_id):
    try:
        packing_list = current_app.container.packing_list_service.get(packing_list_id, g.user.company_id)
        current_app.container.packing_list_service.delete(g.user, packing_list_id)
        flash(f"Packing list {packing_list.packing_list_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("packing_lists.list_packing_lists"))
