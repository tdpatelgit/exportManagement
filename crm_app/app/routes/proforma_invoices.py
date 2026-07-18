"""
app/routes/proforma_invoices.py
--------------------------------
Proforma Invoice generation: mirrors app/routes/quotations.py layer for
layer. The one thing this adds is the ability to start a new invoice from an
existing Quotation via `?quotation_id=`, prefilling consignee/product/bank
data the same way `?lead_id=` prefills a new Quotation from a Lead. The
invoice number is auto-generated as PI{YYYYMMDD}{seq-of-that-day} and is
never user-editable.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

proforma_invoices_bp = Blueprint("proforma_invoices", __name__, url_prefix="/proforma-invoices")

_HEADER_FIELDS = [
    "invoice_date", "lead_id", "quotation_id", "export_ref_no", "buyer_order_no", "other_reference",
    "consignee_name", "consignee_address", "notify_name", "notify_address",
    "country_of_origin", "country_of_destination",
    "port_of_loading", "port_of_discharge", "final_destination",
    "transhipment", "partial_shipment", "variation_in_qty", "delivery_period",
    "container_details", "terms_of_delivery", "payment_terms", "remarks",
    "sea_freight", "insurance", "certification", "other_charges", "discount_amount",
    "bank_name", "bank_account_number", "bank_ifsc_code", "bank_swift_code", "bank_branch", "bank_address",
    "display_mode",
]


def _extract_header(form) -> dict:
    return {key: form.get(key, "") for key in _HEADER_FIELDS}


def _extract_items(form) -> list:
    product_ids = form.getlist("item_product_id[]")
    product_names = form.getlist("item_product_name[]")
    hsn_codes = form.getlist("item_hsn_code[]")
    surfaces = form.getlist("item_surface[]")
    pallets = form.getlist("item_pallets[]")
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
            "surface": surfaces[i] if i < len(surfaces) else "",
            "pallets": pallets[i] if i < len(pallets) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
            "quantity_value": values[i] if i < len(values) else "",
            "unit": units[i] if i < len(units) else "SQM",
            "price_usd": prices[i] if i < len(prices) else "",
        })
    return items


def _form_context():
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    quotations = container.quotation_service.list_all(g.user.company_id)
    company = container.company_service.get(g.user.company_id)
    bank_options = company.bank_details if company else []
    return leads, quotations, bank_options


def _alt_qty_map(items) -> dict:
    """Same purpose as quotations._alt_qty_map - reproduces the Boxes x
    Alternate Quantity auto-calc for rows already tied to a catalog product."""
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
            product = container.product_service.get_product(product_id, g.user.company_id)
            result[product_id] = product.alternate_quantity or ""
        except NotFoundError:
            pass
    return result


@proforma_invoices_bp.route("/")
@login_required
def list_proforma_invoices():
    invoices = current_app.container.proforma_invoice_service.list_all(g.user.company_id)
    return render_template("proforma_invoices/list.html", invoices=invoices)


@proforma_invoices_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_proforma_invoice():
    container = current_app.container
    if request.method == "POST":
        try:
            invoice = container.proforma_invoice_service.create(
                current_user=g.user, fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash(f"Proforma invoice {invoice.invoice_number} created.", "success")
            return redirect(url_for("proforma_invoices.view_proforma_invoice", proforma_invoice_id=invoice.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, quotations, bank_options = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "proforma_invoices/form.html", invoice=None, leads=leads, quotations=quotations,
                bank_options=bank_options, form_data=request.form, form_items=items,
                alt_qty_map=_alt_qty_map(items), today=date.today().isoformat(),
            ), 400

    leads, quotations, bank_options = _form_context()
    prefill = None
    form_items = None
    quotation_id = request.args.get("quotation_id")
    lead_id = request.args.get("lead_id")
    if quotation_id:
        try:
            quotation = container.quotation_service.get(int(quotation_id), g.user.company_id)
            built = container.proforma_invoice_service.build_prefill_from_quotation(quotation)
            prefill = built["fields"]
            prefill["invoice_date"] = date.today().isoformat()
            form_items = built["items"]
        except (NotFoundError, ValueError):
            pass
    elif lead_id:
        try:
            lead = container.lead_service.get(int(lead_id), g.user.company_id)
            prefill = {
                "lead_id": lead.id, "consignee_name": lead.company_name,
                "invoice_date": date.today().isoformat(),
            }
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "proforma_invoices/form.html", invoice=None, leads=leads, quotations=quotations,
        bank_options=bank_options, form_data=prefill, form_items=form_items,
        alt_qty_map=_alt_qty_map(form_items) if form_items else {}, today=date.today().isoformat(),
    )


@proforma_invoices_bp.route("/<int:proforma_invoice_id>")
@login_required
def view_proforma_invoice(proforma_invoice_id):
    container = current_app.container
    try:
        invoice = container.proforma_invoice_service.get(proforma_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    packing_lists = container.packing_list_service.list_for_proforma(proforma_invoice_id, g.user.company_id)
    return render_template("proforma_invoices/print.html", invoice=invoice, company=company,
                           packing_lists=packing_lists)


@proforma_invoices_bp.route("/<int:proforma_invoice_id>/combined")
@login_required
def combined_proforma_invoice(proforma_invoice_id):
    """The combined printable document: the proforma invoice page followed by
    its packing details page(s), each on its own A4 sheet."""
    container = current_app.container
    try:
        invoice = container.proforma_invoice_service.get(proforma_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    packing_lists = container.packing_list_service.list_for_proforma(proforma_invoice_id, g.user.company_id)
    from app.routes.packing_lists import catalog_maps
    product_map, design_map = catalog_maps(packing_lists)
    return render_template("proforma_invoices/print_combined.html", invoice=invoice, company=company,
                           packing_lists=packing_lists, product_map=product_map, design_map=design_map)


@proforma_invoices_bp.route("/<int:proforma_invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_proforma_invoice(proforma_invoice_id):
    container = current_app.container
    try:
        invoice = container.proforma_invoice_service.get(proforma_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.proforma_invoice_service.update(
                current_user=g.user, invoice_id=proforma_invoice_id,
                fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash(f"Proforma invoice {invoice.invoice_number} updated.", "success")
            return redirect(url_for("proforma_invoices.view_proforma_invoice", proforma_invoice_id=proforma_invoice_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, quotations, bank_options = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "proforma_invoices/form.html", invoice=invoice, leads=leads, quotations=quotations,
                bank_options=bank_options, form_data=request.form, form_items=items,
                alt_qty_map=_alt_qty_map(items), today=date.today().isoformat(),
            ), 400

    leads, quotations, bank_options = _form_context()
    return render_template(
        "proforma_invoices/form.html", invoice=invoice, leads=leads, quotations=quotations,
        bank_options=bank_options, form_data=None, form_items=None,
        alt_qty_map=_alt_qty_map(invoice.items), today=date.today().isoformat(),
    )


@proforma_invoices_bp.route("/<int:proforma_invoice_id>/delete", methods=["POST"])
@login_required
def delete_proforma_invoice(proforma_invoice_id):
    try:
        invoice = current_app.container.proforma_invoice_service.get(proforma_invoice_id, g.user.company_id)
        current_app.container.proforma_invoice_service.delete(g.user, proforma_invoice_id)
        flash(f"Proforma invoice {invoice.invoice_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("proforma_invoices.list_proforma_invoices"))


@proforma_invoices_bp.route("/<int:proforma_invoice_id>/versions")
@admin_required
def proforma_invoice_versions(proforma_invoice_id):
    container = current_app.container
    try:
        invoice = container.proforma_invoice_service.get(proforma_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    versions = container.document_version_service.list_for_document("proforma_invoice", proforma_invoice_id)
    rows = [
        {
            "version_number": v.version_number,
            "created_at": v.created_at,
            "changed_by_name": v.changed_by_name,
            "url": url_for("proforma_invoices.view_proforma_invoice", proforma_invoice_id=proforma_invoice_id) if i == 0 else
                   url_for("proforma_invoices.view_proforma_invoice_version",
                           proforma_invoice_id=proforma_invoice_id, version_number=v.version_number),
        }
        for i, v in enumerate(versions)
    ]
    return render_template(
        "document_versions/list.html", document_number=invoice.invoice_number, versions=rows,
        back_url=url_for("proforma_invoices.view_proforma_invoice", proforma_invoice_id=proforma_invoice_id),
    )


@proforma_invoices_bp.route("/<int:proforma_invoice_id>/versions/<int:version_number>")
@admin_required
def view_proforma_invoice_version(proforma_invoice_id, version_number):
    container = current_app.container
    try:
        container.proforma_invoice_service.get(proforma_invoice_id, g.user.company_id)  # tenant-scope check
        historical_invoice, version = container.document_version_service.get_version(
            "proforma_invoice", proforma_invoice_id, version_number
        )
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    return render_template(
        "proforma_invoices/print.html", invoice=historical_invoice, company=company,
        packing_lists=[], historical_version=version,
    )
