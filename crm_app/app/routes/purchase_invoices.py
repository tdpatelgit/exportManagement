"""
app/routes/purchase_invoices.py
--------------------------------
Purchase Invoice: the last document in the pipeline, mirroring
app/routes/purchase_orders.py layer for layer. Raised once a supplier's
goods against one of our purchase orders actually arrive - it's normally
started from an existing Purchase Order via `?purchase_order_id=` (copying
its seller details and product lines in as a one-time prefill). Unlike
every other document type here, nothing is generated/printed for a
purchase invoice: the supplier already sent their own invoice as a PDF,
so this form also accepts a file upload alongside the typed-in figures.
The internal purchase_invoice_number is auto-generated as
PINV{YYYYMMDD}{seq-of-that-day} and is never user-editable - the
supplier's own invoice_number is a separate, free-typed field.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required, verify_delete_password

purchase_invoices_bp = Blueprint("purchase_invoices", __name__, url_prefix="/purchase-invoices")

_HEADER_FIELDS = [
    "invoice_number", "invoice_date", "purchase_order_id", "job_work_id", "lead_id", "seller_supplier_id",
    "seller_name", "seller_address", "seller_pan", "seller_gstin", "seller_ref_no",
    "transporter_name", "epcg_number", "epcg_date",
    "discount_amount", "insurance_other", "freight", "igst_amount", "cgst_amount", "sgst_amount", "round_off",
    "purchase_type",
    "remarks", "currency_code",
]


def _extract_header(form) -> dict:
    fields = {key: form.get(key, "") for key in _HEADER_FIELDS}
    fields["purchase_order_ids"] = form.getlist("purchase_order_ids[]")
    fields["job_work_ids"] = form.getlist("job_work_ids[]")
    return fields


def _extract_items(form) -> list:
    product_ids = form.getlist("item_product_id[]")
    product_names = form.getlist("item_product_name[]")
    hsn_codes = form.getlist("item_hsn_code[]")
    boxes = form.getlist("item_quantity_boxes[]")
    values = form.getlist("item_quantity_value[]")
    units = form.getlist("item_unit[]")
    prices = form.getlist("item_price_inr[]")
    price_pers = form.getlist("item_price_per[]")
    source_po_ids = form.getlist("item_purchase_order_id[]")
    source_jw_ids = form.getlist("item_job_work_id[]")
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
            "purchase_order_id": source_po_ids[i] if i < len(source_po_ids) else "",
            "job_work_id": source_jw_ids[i] if i < len(source_jw_ids) else "",
        })
    return items


def _extract_vehicle_numbers(form) -> list:
    return form.getlist("vehicle_number[]")


def _product_meta_map(items) -> dict:
    """product_id -> {'alt_qty', 'igst'} for rows already tied to a catalog
    product - same shape and purpose as purchase_orders._product_meta_map:
    `alt_qty` drives the form's Boxes x Alternate Quantity auto-calc for
    Qty, `igst` lets the form auto-fill IGST/CGST/SGST from the chosen
    Purchase under type (the same rate a Full Tax Purchase would derive on
    a purchase order, see PurchaseOrderService.base_igst_percent). A
    purchase invoice has no product picker of its own (its lines are
    prefilled from the purchase order), so this map is the only source of
    both figures."""
    container = current_app.container
    result = {}
    for item in items or []:
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


def _form_context(exclude_purchase_invoice_id=None):
    """(leads, purchase orders, job works, suppliers) for the form's
    Start-from and Seller pickers. `purchase_orders`/`job_works` are only
    the ones still outstanding (see PurchaseInvoiceService.
    list_all_outstanding / list_all_outstanding_job_works) - a PO or job
    work already fully invoiced drops off the "Start from" picker, all
    suppliers at once since the template filters the checkbox list down to
    whichever supplier is currently selected (a job work's own supplier for
    this purpose is its From Seller, seller_supplier_id, the same party a
    real purchase order's Seller is).
    `exclude_purchase_invoice_id` (set when editing) makes that invoice's
    own items not count against its own PO(s)/job work(s), so re-opening it
    for edit doesn't make its own already-picked one(s) disappear."""
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    purchase_orders = container.purchase_invoice_service.list_all_outstanding(
        g.user.company_id, exclude_purchase_invoice_id
    )
    job_works = container.purchase_invoice_service.list_all_outstanding_job_works(
        g.user.company_id, exclude_purchase_invoice_id
    )
    suppliers = container.supplier_service.list_all(g.user.company_id)
    return leads, purchase_orders, job_works, suppliers


@purchase_invoices_bp.route("/")
@login_required
def list_purchase_invoices():
    purchase_invoices = current_app.container.purchase_invoice_service.list_all(g.user.company_id)
    return render_template("purchase_invoices/list.html", purchase_invoices=purchase_invoices)


@purchase_invoices_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_purchase_invoice():
    container = current_app.container
    if request.method == "POST":
        try:
            purchase_invoice = container.purchase_invoice_service.create(
                current_user=g.user, fields=_extract_header(request.form), raw_items=_extract_items(request.form),
                raw_vehicle_numbers=_extract_vehicle_numbers(request.form),
                pdf_file=request.files.get("supplier_pdf"),
            )
            flash(f"Purchase invoice {purchase_invoice.purchase_invoice_number} created.", "success")
            return redirect(url_for("purchase_invoices.view_purchase_invoice", purchase_invoice_id=purchase_invoice.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, purchase_orders, job_works, suppliers = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "purchase_invoices/form.html", purchase_invoice=None, leads=leads, purchase_orders=purchase_orders,
                job_works=job_works, suppliers=suppliers, form_data=request.form, form_items=items,
                form_vehicle_numbers=_extract_vehicle_numbers(request.form), today=date.today().isoformat(),
                product_meta_map=_product_meta_map(items),
                selected_purchase_order_ids=request.form.getlist("purchase_order_ids[]"),
                selected_job_work_ids=request.form.getlist("job_work_ids[]"),
            ), 400

    leads, purchase_orders, job_works, suppliers = _form_context()
    prefill = None
    form_items = None
    # `purchase_order_ids`/`job_work_ids` (each comma-separated) are the
    # multi-select "Start from" reload; a lone `purchase_order_id` is still
    # accepted for the older single-PO entry point (purchase_orders/
    # print.html's own "Create purchase invoice" button). Both kinds can be
    # picked together (a shipment covering a real PO and a job work from the
    # same supplier at once) - their fields are merged, job work fields only
    # filling in what the purchase order side left blank.
    raw_po_ids = request.args.get("purchase_order_ids") or request.args.get("purchase_order_id") or ""
    po_ids = [p for p in raw_po_ids.split(",") if p.strip()]
    raw_jw_ids = request.args.get("job_work_ids") or ""
    jw_ids = [p for p in raw_jw_ids.split(",") if p.strip()]
    lead_id = request.args.get("lead_id")
    po_built = None
    jw_built = None
    if po_ids:
        try:
            purchase_orders_selected = [
                container.purchase_order_service.get(int(p), g.user.company_id) for p in po_ids
            ]
            po_built = container.purchase_invoice_service.build_prefill_from_purchase_orders(purchase_orders_selected)
        except (NotFoundError, ValueError):
            pass
    if jw_ids:
        try:
            job_works_selected = [
                container.job_work_service.get(int(p), g.user.company_id) for p in jw_ids
            ]
            jw_built = container.purchase_invoice_service.build_prefill_from_job_works(job_works_selected)
        except (NotFoundError, ValueError):
            pass
    if po_built or jw_built:
        prefill = {**(jw_built["fields"] if jw_built else {}), **(po_built["fields"] if po_built else {})}
        prefill["invoice_date"] = date.today().isoformat()
        form_items = (po_built["items"] if po_built else []) + (jw_built["items"] if jw_built else [])
    elif lead_id:
        try:
            lead = container.lead_service.get(int(lead_id), g.user.company_id)
            prefill = {"lead_id": lead.id, "invoice_date": date.today().isoformat()}
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "purchase_invoices/form.html", purchase_invoice=None, leads=leads, purchase_orders=purchase_orders,
        job_works=job_works, suppliers=suppliers, form_data=prefill, form_items=form_items,
        form_vehicle_numbers=None, today=date.today().isoformat(),
        product_meta_map=_product_meta_map(form_items),
        selected_purchase_order_ids=(prefill or {}).get("purchase_order_ids") or [],
        selected_job_work_ids=(prefill or {}).get("job_work_ids") or [],
    )


def _linked_purchase_orders(container, purchase_invoice):
    """Every purchase order this purchase invoice was raised against, in the
    order they were selected - normally the whole point of the "several POs
    at once" feature is that they're all the same supplier, so this is
    usually a short list."""
    result = []
    for po_id in purchase_invoice.purchase_order_ids:
        try:
            result.append(container.purchase_order_service.get(po_id, g.user.company_id))
        except NotFoundError:
            pass
    return result


def _linked_job_works(container, purchase_invoice):
    """Every job work this purchase invoice was raised against, in the order
    they were selected - the job-work counterpart of _linked_purchase_orders
    (a job work now prints/numbers as a purchase order, so it can be one of
    an invoice's origins the same way)."""
    result = []
    for jw_id in purchase_invoice.job_work_ids:
        try:
            result.append(container.job_work_service.get(jw_id, g.user.company_id))
        except NotFoundError:
            pass
    return result


def _related_proformas(purchase_orders):
    """Every distinct proforma invoice behind this purchase invoice's linked
    purchase orders. A purchase invoice never points at a proforma directly -
    the link runs through its purchase order(s), where the proforma was
    chosen; several POs on the same invoice can (rarely) trace back to more
    than one proforma, so this is a list, not a single link."""
    seen = set()
    result = []
    for po in purchase_orders:
        if po.proforma_invoice_id and po.proforma_invoice_id not in seen:
            seen.add(po.proforma_invoice_id)
            result.append(po)
    return result


@purchase_invoices_bp.route("/<int:purchase_invoice_id>")
@login_required
def view_purchase_invoice(purchase_invoice_id):
    container = current_app.container
    try:
        purchase_invoice = container.purchase_invoice_service.get(purchase_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    packing_lists = container.packing_list_service.list_for_purchase_invoice(purchase_invoice_id, g.user.company_id)
    purchase_orders = _linked_purchase_orders(container, purchase_invoice)
    job_works = _linked_job_works(container, purchase_invoice)
    proforma_invoices = []
    for po in _related_proformas(purchase_orders):
        try:
            proforma_invoices.append(
                container.proforma_invoice_service.get(po.proforma_invoice_id, g.user.company_id)
            )
        except NotFoundError:
            pass
    job_outs = container.job_out_service.list_for_purchase_invoice(purchase_invoice_id, g.user.company_id)
    return render_template("purchase_invoices/view.html", purchase_invoice=purchase_invoice,
                           packing_lists=packing_lists, purchase_orders=purchase_orders, job_works=job_works,
                           proforma_invoices=proforma_invoices, job_outs=job_outs)


@purchase_invoices_bp.route("/<int:purchase_invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_purchase_invoice(purchase_invoice_id):
    container = current_app.container
    try:
        purchase_invoice = container.purchase_invoice_service.get(purchase_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            updated = container.purchase_invoice_service.update(
                current_user=g.user, purchase_invoice_id=purchase_invoice_id,
                fields=_extract_header(request.form), raw_items=_extract_items(request.form),
                raw_vehicle_numbers=_extract_vehicle_numbers(request.form),
                pdf_file=request.files.get("supplier_pdf"),
                remove_pdf=bool(request.form.get("remove_supplier_pdf")),
            )
            flash(f"Purchase invoice {purchase_invoice.purchase_invoice_number} updated.", "success")
            return redirect(url_for("purchase_invoices.view_purchase_invoice", purchase_invoice_id=purchase_invoice_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            leads, purchase_orders, job_works, suppliers = _form_context(exclude_purchase_invoice_id=purchase_invoice_id)
            items = _extract_items(request.form)
            return render_template(
                "purchase_invoices/form.html", purchase_invoice=purchase_invoice, leads=leads,
                purchase_orders=purchase_orders, job_works=job_works, suppliers=suppliers,
                form_data=request.form, form_items=items,
                form_vehicle_numbers=_extract_vehicle_numbers(request.form), today=date.today().isoformat(),
                product_meta_map=_product_meta_map(items),
                selected_purchase_order_ids=request.form.getlist("purchase_order_ids[]"),
                selected_job_work_ids=request.form.getlist("job_work_ids[]"),
            ), 400

    leads, purchase_orders, job_works, suppliers = _form_context(exclude_purchase_invoice_id=purchase_invoice_id)
    return render_template(
        "purchase_invoices/form.html", purchase_invoice=purchase_invoice, leads=leads,
        purchase_orders=purchase_orders, job_works=job_works, suppliers=suppliers, form_data=None, form_items=None,
        form_vehicle_numbers=None, today=date.today().isoformat(),
        product_meta_map=_product_meta_map(purchase_invoice.items),
        selected_purchase_order_ids=purchase_invoice.purchase_order_ids,
        selected_job_work_ids=purchase_invoice.job_work_ids,
    )


@purchase_invoices_bp.route("/<int:purchase_invoice_id>/delete", methods=["POST"])
@login_required
def delete_purchase_invoice(purchase_invoice_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Purchase invoice not deleted.", "error")
        return redirect(url_for("purchase_invoices.view_purchase_invoice", purchase_invoice_id=purchase_invoice_id))
    try:
        purchase_invoice = current_app.container.purchase_invoice_service.get(purchase_invoice_id, g.user.company_id)
        current_app.container.purchase_invoice_service.delete(g.user, purchase_invoice_id)
        flash(f"Purchase invoice {purchase_invoice.purchase_invoice_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("purchase_invoices.list_purchase_invoices"))


@purchase_invoices_bp.route("/<int:purchase_invoice_id>/versions")
@admin_required
def purchase_invoice_versions(purchase_invoice_id):
    container = current_app.container
    try:
        purchase_invoice = container.purchase_invoice_service.get(purchase_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    versions = container.document_version_service.list_for_document("purchase_invoice", purchase_invoice_id)
    rows = [
        {
            "version_number": v.version_number,
            "created_at": v.created_at,
            "changed_by_name": v.changed_by_name,
            "url": url_for("purchase_invoices.view_purchase_invoice", purchase_invoice_id=purchase_invoice_id) if i == 0 else
                   url_for("purchase_invoices.view_purchase_invoice_version",
                           purchase_invoice_id=purchase_invoice_id, version_number=v.version_number),
        }
        for i, v in enumerate(versions)
    ]
    return render_template(
        "document_versions/list.html", document_number=purchase_invoice.purchase_invoice_number, versions=rows,
        back_url=url_for("purchase_invoices.view_purchase_invoice", purchase_invoice_id=purchase_invoice_id),
    )


@purchase_invoices_bp.route("/<int:purchase_invoice_id>/versions/<int:version_number>")
@admin_required
def view_purchase_invoice_version(purchase_invoice_id, version_number):
    container = current_app.container
    try:
        container.purchase_invoice_service.get(purchase_invoice_id, g.user.company_id)  # tenant-scope check
        historical_purchase_invoice, version = container.document_version_service.get_version(
            "purchase_invoice", purchase_invoice_id, version_number
        )
    except NotFoundError:
        abort(404)
    return render_template(
        "purchase_invoices/view.html", purchase_invoice=historical_purchase_invoice, historical_version=version,
        purchase_orders=[], job_works=[], proforma_invoices=[], job_outs=[],
    )
