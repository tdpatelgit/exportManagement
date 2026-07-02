"""
app/routes/quotations.py
--------------------------
Quotation generation: a header (buyer, shipping, bank, terms) plus a list of
product line items. The quotation number is auto-generated as
QT{YYYYMMDD}{seq-of-that-day} and is never user-editable.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required

quotations_bp = Blueprint("quotations", __name__, url_prefix="/quotations")

_HEADER_FIELDS = [
    "quotation_date", "client_id", "buyer_name", "buyer_address", "buyer_reference_no",
    "port_of_loading", "port_of_discharge", "packing_details", "container_details",
    "shipping_mode", "shipping_terms", "payment_terms", "advance_percent", "against_bl_percent",
    "price_validity_days", "remarks", "discount_amount",
    "bank_name", "bank_account_number", "bank_ifsc_code", "bank_swift_code", "bank_branch", "bank_address",
]


def _extract_header(form) -> dict:
    return {key: form.get(key, "") for key in _HEADER_FIELDS}


def _extract_items(form) -> list:
    product_ids = form.getlist("item_product_id[]")
    product_names = form.getlist("item_product_name[]")
    hsn_codes = form.getlist("item_hsn_code[]")
    boxes = form.getlist("item_quantity_boxes[]")
    values = form.getlist("item_quantity_value[]")
    units = form.getlist("item_unit[]")
    prices = form.getlist("item_price_usd[]")
    items = []
    for i in range(len(product_names)):
        items.append({
            "product_id": product_ids[i] if i < len(product_ids) else "",
            "product_name": product_names[i],
            "hsn_code": hsn_codes[i] if i < len(hsn_codes) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
            "quantity_value": values[i] if i < len(values) else "",
            "unit": units[i] if i < len(units) else "SQM",
            "price_usd": prices[i] if i < len(prices) else "",
        })
    return items


def _form_context():
    container = current_app.container
    clients = container.client_service.list_all()
    company = container.company_service.get()
    bank_options = company.bank_details if company else []
    return clients, bank_options


def _alt_qty_map(items) -> dict:
    """Maps product_id -> that product's Alternate Quantity, so the form can
    reproduce the Boxes x Alternate Quantity auto-calc for rows that are
    already tied to a catalog product (re-displayed after a validation error,
    or when editing an existing quotation)."""
    container = current_app.container
    result = {}
    for item in items:
        raw_id = item.get("product_id") if isinstance(item, dict) else item.product_id
        if not raw_id or raw_id in result:
            continue
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if product_id in result:
            continue
        try:
            product = container.product_service.get_product(product_id)
            result[product_id] = product.alternate_quantity or ""
        except NotFoundError:
            pass
    return result


@quotations_bp.route("/")
@login_required
def list_quotations():
    quotations = current_app.container.quotation_service.list_all()
    return render_template("quotations/list.html", quotations=quotations)


@quotations_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_quotation():
    container = current_app.container
    if request.method == "POST":
        try:
            quotation = container.quotation_service.create(
                current_user=g.user, fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash(f"Quotation {quotation.quotation_number} created.", "success")
            return redirect(url_for("quotations.view_quotation", quotation_id=quotation.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            clients, bank_options = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "quotations/form.html", quotation=None, clients=clients, bank_options=bank_options,
                form_data=request.form, form_items=items, alt_qty_map=_alt_qty_map(items),
                today=date.today().isoformat(),
            ), 400

    clients, bank_options = _form_context()
    prefill = None
    lead_id = request.args.get("lead_id")
    if lead_id:
        try:
            lead = container.lead_service.get(int(lead_id))
            prefill = {
                "buyer_name": lead.company_name, "quotation_date": date.today().isoformat(),
                "price_validity_days": 30, "advance_percent": 0, "against_bl_percent": 0, "discount_amount": 0,
            }
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "quotations/form.html", quotation=None, clients=clients, bank_options=bank_options,
        form_data=prefill, form_items=None, alt_qty_map={}, today=date.today().isoformat(),
    )


@quotations_bp.route("/<int:quotation_id>")
@login_required
def view_quotation(quotation_id):
    container = current_app.container
    try:
        quotation = container.quotation_service.get(quotation_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get()
    return render_template("quotations/print.html", quotation=quotation, company=company)


@quotations_bp.route("/<int:quotation_id>/edit", methods=["GET", "POST"])
@login_required
def edit_quotation(quotation_id):
    container = current_app.container
    try:
        quotation = container.quotation_service.get(quotation_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.quotation_service.update(
                current_user=g.user, quotation_id=quotation_id,
                fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash(f"Quotation {quotation.quotation_number} updated.", "success")
            return redirect(url_for("quotations.view_quotation", quotation_id=quotation_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            clients, bank_options = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "quotations/form.html", quotation=quotation, clients=clients, bank_options=bank_options,
                form_data=request.form, form_items=items, alt_qty_map=_alt_qty_map(items),
                today=date.today().isoformat(),
            ), 400

    clients, bank_options = _form_context()
    return render_template(
        "quotations/form.html", quotation=quotation, clients=clients, bank_options=bank_options,
        form_data=None, form_items=None, alt_qty_map=_alt_qty_map(quotation.items), today=date.today().isoformat(),
    )


@quotations_bp.route("/<int:quotation_id>/delete", methods=["POST"])
@login_required
def delete_quotation(quotation_id):
    try:
        quotation = current_app.container.quotation_service.get(quotation_id)
        current_app.container.quotation_service.delete(g.user, quotation_id)
        flash(f"Quotation {quotation.quotation_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("quotations.list_quotations"))
