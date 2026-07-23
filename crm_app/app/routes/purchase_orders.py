"""
app/routes/purchase_orders.py
------------------------------
Purchase Order generation: mirrors app/routes/proforma_invoices.py layer for
layer. The next document after the Proforma Invoice in the client pipeline,
with the roles flipped: OUR company is the BUYER and a supplier is the
SELLER, prices in INR. A PO is normally started from an existing Proforma
Invoice via `?proforma_invoice_id=` (copying its product lines in as a
one-time prefill), and can then carry its own Packing List, started from
the PO the same way a PL starts from a PI. The PO number is auto-generated
as PO{YYYYMMDD}{seq-of-that-day} and is never user-editable.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

purchase_orders_bp = Blueprint("purchase_orders", __name__, url_prefix="/purchase-orders")

_HEADER_FIELDS = [
    "po_date", "lead_id", "proforma_invoice_id", "seller_supplier_id",
    "seller_name", "seller_address", "seller_pan", "seller_gstin", "seller_ref_no",
    "port_of_loading", "port_of_discharge", "container_details", "delivery_time",
    "advance_percent", "payment_terms", "remarks",
    "purchase_type",
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
    prices = form.getlist("item_price_inr[]")
    price_pers = form.getlist("item_price_per[]")
    items = []
    for i in range(len(product_names)):
        items.append({
            "product_id": product_ids[i] if i < len(product_ids) else "",
            "product_name": product_names[i],
            "hsn_code": hsn_codes[i] if i < len(hsn_codes) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
            "quantity_value": values[i] if i < len(values) else "",
            "unit": units[i] if i < len(units) else "SQM",
            "price_inr": prices[i] if i < len(prices) else "",
            "price_per": price_pers[i] if i < len(price_pers) else "BOX",
        })
    return items


def _form_context():
    """(leads, proforma invoices, suppliers) for the form's Start-from
    and Seller pickers."""
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    invoices = container.proforma_invoice_service.list_all(g.user.company_id)
    suppliers = container.supplier_service.list_all(g.user.company_id)
    return leads, invoices, suppliers


def _flash_if_over_ordered(container, purchase_order) -> None:
    """A product line need not be ordered from a single PO - it can be split
    across several, and nothing stops their quantities adding up to MORE
    boxes than the proforma invoice actually called for. Checked right after
    saving a PO that's linked to one, flagging every product currently over
    its requirement (the whole picture across every PO on that invoice, not
    just this one - an earlier PO could be the one now pushed over)."""
    if not purchase_order.proforma_invoice_id:
        return
    try:
        invoice = container.proforma_invoice_service.get(purchase_order.proforma_invoice_id, g.user.company_id)
    except NotFoundError:
        return
    over_ordered = container.proforma_fulfilment_service.product_status(g.user.company_id, invoice)["over_ordered"]
    if not over_ordered:
        return
    parts = []
    for p in over_ordered:
        if p["required_boxes"] or p["placed_boxes"]:
            excess, unit = p["excess_boxes"], "boxes"
        else:
            excess, unit = p["excess_quantity"], p["unit"]
        parts.append(f"{p['product_name']} (+{excess:,.2f} {unit})")
    flash(
        "Ordered more than the proforma invoice calls for: " + ", ".join(parts) + ".", "error",
    )


def _product_meta_map(items) -> dict:
    """product_id -> {'alt_qty', 'igst'} for rows already tied to a catalog
    product: `alt_qty` reproduces proforma_invoices._alt_qty_map's Boxes x
    Alternate Quantity auto-calc, `igst` lets the form preview the tax a
    Full Tax Purchase will be charged (the figure the service derives on
    save - see PurchaseOrderService.base_igst_percent)."""
    container = current_app.container
    result = {}
    for item in items:
        raw_id = item.get("product_id") if isinstance(item, dict) else item.product_id
        if not raw_id:
            continue
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if product_id in result:
            continue
        try:
            product = container.product_service.get_product(product_id, g.user.company_id)
            result[product_id] = {"alt_qty": product.alternate_quantity or "", "igst": product.igst_percent or ""}
        except NotFoundError:
            pass
    return result


@purchase_orders_bp.route("/")
@login_required
def list_purchase_orders():
    purchase_orders = current_app.container.purchase_order_service.list_all(g.user.company_id)
    return render_template("purchase_orders/list.html", purchase_orders=purchase_orders)


@purchase_orders_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_purchase_order():
    container = current_app.container
    if request.method == "POST":
        try:
            purchase_order = container.purchase_order_service.create(
                current_user=g.user, fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash(f"Purchase order {purchase_order.po_number} created.", "success")
            _flash_if_over_ordered(container, purchase_order)
            return redirect(url_for("purchase_orders.view_purchase_order", purchase_order_id=purchase_order.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, invoices, suppliers = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "purchase_orders/form.html", purchase_order=None, leads=leads, invoices=invoices,
                suppliers=suppliers, form_data=request.form, form_items=items,
                product_meta_map=_product_meta_map(items), today=date.today().isoformat(),
            ), 400

    leads, invoices, suppliers = _form_context()
    prefill = None
    form_items = None
    proforma_invoice_id = request.args.get("proforma_invoice_id")
    lead_id = request.args.get("lead_id")
    if proforma_invoice_id:
        try:
            invoice = container.proforma_invoice_service.get(int(proforma_invoice_id), g.user.company_id)
            built = container.purchase_order_service.build_prefill_from_proforma(invoice)
            prefill = built["fields"]
            prefill["po_date"] = date.today().isoformat()
            form_items = built["items"]
        except (NotFoundError, ValueError):
            pass
    elif lead_id:
        try:
            lead = container.lead_service.get(int(lead_id), g.user.company_id)
            prefill = {"lead_id": lead.id, "po_date": date.today().isoformat()}
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "purchase_orders/form.html", purchase_order=None, leads=leads, invoices=invoices,
        suppliers=suppliers, form_data=prefill, form_items=form_items,
        product_meta_map=_product_meta_map(form_items) if form_items else {},
        today=date.today().isoformat(),
    )


@purchase_orders_bp.route("/<int:purchase_order_id>")
@login_required
def view_purchase_order(purchase_order_id):
    container = current_app.container
    try:
        purchase_order = container.purchase_order_service.get(purchase_order_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    packing_lists = container.packing_list_service.list_for_purchase_order(purchase_order_id, g.user.company_id)
    return render_template("purchase_orders/print.html", purchase_order=purchase_order, company=company,
                           packing_lists=packing_lists)


@purchase_orders_bp.route("/<int:purchase_order_id>/combined")
@login_required
def combined_purchase_order(purchase_order_id):
    """The combined printable document: the purchase order page followed by
    its packing details page(s), each on its own A4 sheet - same shape as
    the combined proforma invoice document."""
    container = current_app.container
    try:
        purchase_order = container.purchase_order_service.get(purchase_order_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    packing_lists = container.packing_list_service.list_for_purchase_order(purchase_order_id, g.user.company_id)
    from app.routes.packing_lists import catalog_maps
    product_map, design_map = catalog_maps(packing_lists)
    return render_template("purchase_orders/print_combined.html", purchase_order=purchase_order, company=company,
                           packing_lists=packing_lists, product_map=product_map, design_map=design_map)


@purchase_orders_bp.route("/<int:purchase_order_id>/edit", methods=["GET", "POST"])
@login_required
def edit_purchase_order(purchase_order_id):
    container = current_app.container
    try:
        purchase_order = container.purchase_order_service.get(purchase_order_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            updated = container.purchase_order_service.update(
                current_user=g.user, purchase_order_id=purchase_order_id,
                fields=_extract_header(request.form), raw_items=_extract_items(request.form),
            )
            flash(f"Purchase order {purchase_order.po_number} updated.", "success")
            _flash_if_over_ordered(container, updated)
            return redirect(url_for("purchase_orders.view_purchase_order", purchase_order_id=purchase_order_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, invoices, suppliers = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "purchase_orders/form.html", purchase_order=purchase_order, leads=leads, invoices=invoices,
                suppliers=suppliers, form_data=request.form, form_items=items,
                product_meta_map=_product_meta_map(items), today=date.today().isoformat(),
            ), 400

    leads, invoices, suppliers = _form_context()
    return render_template(
        "purchase_orders/form.html", purchase_order=purchase_order, leads=leads, invoices=invoices,
        suppliers=suppliers, form_data=None, form_items=None,
        product_meta_map=_product_meta_map(purchase_order.items), today=date.today().isoformat(),
    )


@purchase_orders_bp.route("/<int:purchase_order_id>/delete", methods=["POST"])
@login_required
def delete_purchase_order(purchase_order_id):
    try:
        purchase_order = current_app.container.purchase_order_service.get(purchase_order_id, g.user.company_id)
        current_app.container.purchase_order_service.delete(g.user, purchase_order_id)
        flash(f"Purchase order {purchase_order.po_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("purchase_orders.list_purchase_orders"))


@purchase_orders_bp.route("/<int:purchase_order_id>/versions")
@admin_required
def purchase_order_versions(purchase_order_id):
    container = current_app.container
    try:
        purchase_order = container.purchase_order_service.get(purchase_order_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    versions = container.document_version_service.list_for_document("purchase_order", purchase_order_id)
    rows = [
        {
            "version_number": v.version_number,
            "created_at": v.created_at,
            "changed_by_name": v.changed_by_name,
            "url": url_for("purchase_orders.view_purchase_order", purchase_order_id=purchase_order_id) if i == 0 else
                   url_for("purchase_orders.view_purchase_order_version",
                           purchase_order_id=purchase_order_id, version_number=v.version_number),
        }
        for i, v in enumerate(versions)
    ]
    return render_template(
        "document_versions/list.html", document_number=purchase_order.po_number, versions=rows,
        back_url=url_for("purchase_orders.view_purchase_order", purchase_order_id=purchase_order_id),
    )


@purchase_orders_bp.route("/<int:purchase_order_id>/versions/<int:version_number>")
@admin_required
def view_purchase_order_version(purchase_order_id, version_number):
    container = current_app.container
    try:
        container.purchase_order_service.get(purchase_order_id, g.user.company_id)  # tenant-scope check
        historical_purchase_order, version = container.document_version_service.get_version(
            "purchase_order", purchase_order_id, version_number
        )
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    return render_template(
        "purchase_orders/print.html", purchase_order=historical_purchase_order, company=company,
        packing_lists=[], historical_version=version,
    )
