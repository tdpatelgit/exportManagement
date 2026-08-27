"""
app/routes/export_invoices.py
-----------------------------
Export Invoice generation: the customer/customs-facing document at the buyer
end of the pipeline (Quotation -> Proforma Invoice -> Purchase Order ->
Purchase Invoice). Unlike the other document types it references MANY
proforma invoices at once (a single buyer order fulfilled across several
PIs/suppliers), so it is normally started from one or more PIs via
`?proforma_invoice_ids=` (a comma-separated or repeated query arg) which
prefills the goods lines and imports EPCG / export-under / supplier-exemption
details by walking each PI's purchase orders to their purchase invoices. The
invoice number is typed in by hand (up to 16 digits, unique per company),
matching the number on the physical customs paperwork.
Mirrors app/routes/proforma_invoices.py, plus an optional Shipping Bill PDF
upload (same idiom as purchase_invoices.py).
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort, jsonify

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.services import pallet_alt_quantity
from app.utils import login_required, admin_required, verify_delete_password

export_invoices_bp = Blueprint("export_invoices", __name__, url_prefix="/export-invoices")

_HEADER_FIELDS = [
    "export_invoice_number",
    "invoice_date", "lead_id", "consignee_name", "consignee_address", "notify_name", "notify_address",
    "country_of_origin", "country_of_destination", "place_of_receipt", "pre_carriage_by",
    "port_of_loading", "port_of_discharge", "final_destination", "nature_of_contract", "payment_terms",
    "buyer_order_no", "buyer_order_date",
    "loading_type", "tax_mode", "exchange_rate",
    "sea_freight", "insurance", "certification", "other_charges", "discount_amount",
    "fob_pricing",
    "bank_name", "bank_account_number", "bank_ifsc_code", "bank_swift_code", "bank_branch", "bank_address",
    "authorised_person_name", "authorised_person_designation", "self_sealing_declaration",
    "examination_date", "location_code_08b", "booking_no", "booking_detail_id", "vessel_name", "voyage_no",
    "issuing_authority", "issuing_authority_address",
    "permission_no", "permission_date", "permission_expiry", "permission_is_one_time", "manufacturer_name", "manufacturer_address",
    "stuffing_location", "remarks",
    "total_net_weight_kg", "total_gross_weight_kg", "shipping_bill_no", "shipping_bill_date",
    "currency_code",
]


def _extract_fields(form) -> dict:
    """The scalar header fields plus the parsed child lists, in the shape
    ExportInvoiceService.create/update expect (all lists live inside the
    `fields` dict alongside the scalars)."""
    fields = {key: form.get(key, "") for key in _HEADER_FIELDS}
    fields["proforma_invoice_ids"] = form.getlist("proforma_invoice_ids[]")
    fields["containers"] = _extract_containers(form)
    fields["container_details_list"] = _extract_container_details(form)
    fields["purchase_details"] = _extract_purchase_details(form)
    fields["product_sources"] = _extract_product_sources(form)
    fields["job_ins"] = _extract_job_ins(form)
    fields["packing_allocations"] = _extract_packing_allocations(form)
    return fields


def _extract_items(form) -> list:
    product_ids = form.getlist("item_product_id[]")
    product_names = form.getlist("item_product_name[]")
    hsn_codes = form.getlist("item_hsn_code[]")
    surfaces = form.getlist("item_surface[]")
    pallets = form.getlist("item_pallets[]")
    pallet_weights = form.getlist("item_pallet_weight_kg[]")
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
            "pallet_weight_kg": pallet_weights[i] if i < len(pallet_weights) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
            "quantity_value": values[i] if i < len(values) else "",
            "unit": units[i] if i < len(units) else "SQM",
            "price_usd": prices[i] if i < len(prices) else "",
        })
    return items


def _extract_containers(form) -> list:
    types = form.getlist("container_type[]")
    counts = form.getlist("container_count[]")
    rows = []
    for i in range(max(len(types), len(counts))):
        rows.append({
            "container_type": types[i] if i < len(types) else "",
            "container_count": counts[i] if i < len(counts) else "",
        })
    return rows


def _extract_container_details(form) -> list:
    types = form.getlist("cd_container_type[]")
    nos = form.getlist("cd_container_no[]")
    line_seals = form.getlist("cd_line_seal_no[]")
    rfids = form.getlist("cd_rfid_seal_no[]")
    vehicles = form.getlist("cd_vehicle_no[]")
    lr_nos = form.getlist("cd_lr_no[]")
    max_weights = form.getlist("cd_max_permitted_weight[]")
    tares = form.getlist("cd_tare_weight_kg[]")
    # Transporter is now one invoice-level pick (not typed per container) -
    # the same value is stamped onto every row here so storage/printing,
    # which are still per-row, don't need to change. A row that ends up with
    # nothing else typed is still dropped by
    # ExportInvoiceService._clean_container_details regardless of this.
    transporter = form.get("cd_transporter_name", "")
    n = max(len(types), len(nos), len(line_seals), len(rfids), len(vehicles),
            len(lr_nos), len(max_weights), len(tares))
    rows = []
    for i in range(n):
        rows.append({
            "container_type": types[i] if i < len(types) else "",
            "container_no": nos[i] if i < len(nos) else "",
            "line_seal_no": line_seals[i] if i < len(line_seals) else "",
            "rfid_seal_no": rfids[i] if i < len(rfids) else "",
            "vehicle_no": vehicles[i] if i < len(vehicles) else "",
            "lr_no": lr_nos[i] if i < len(lr_nos) else "",
            "transporter_name": transporter,
            "max_permitted_weight": max_weights[i] if i < len(max_weights) else "",
            "tare_weight_kg": tares[i] if i < len(tares) else "",
        })
    return rows


def _extract_purchase_details(form) -> list:
    gstins = form.getlist("pd_supplier_gstin[]")
    inv_nos = form.getlist("pd_supplier_invoice_no[]")
    names = form.getlist("pd_supplier_name[]")
    types = form.getlist("pd_purchase_type[]")
    epcg_numbers = form.getlist("pd_epcg_number[]")
    epcg_dates = form.getlist("pd_epcg_date[]")
    n = max(len(gstins), len(inv_nos), len(names), len(types), len(epcg_numbers), len(epcg_dates))
    rows = []
    for i in range(n):
        rows.append({
            "supplier_gstin": gstins[i] if i < len(gstins) else "",
            "supplier_invoice_no": inv_nos[i] if i < len(inv_nos) else "",
            "supplier_name": names[i] if i < len(names) else "",
            "purchase_type": types[i] if i < len(types) else "",
            "epcg_number": epcg_numbers[i] if i < len(epcg_numbers) else "",
            "epcg_date": epcg_dates[i] if i < len(epcg_dates) else "",
        })
    return rows


def _extract_product_sources(form) -> list:
    """The read-only "which purchase order(s) this goods line's boxes came
    from" breakdown - see ExportInvoiceService._clean_product_sources -
    round-tripped as hidden fields so it survives a save/reopen."""
    names = form.getlist("ps_product_name[]")
    po_numbers = form.getlist("ps_po_number[]")
    boxes = form.getlist("ps_quantity_boxes[]")
    n = max(len(names), len(po_numbers), len(boxes))
    rows = []
    for i in range(n):
        rows.append({
            "product_name": names[i] if i < len(names) else "",
            "po_number": po_numbers[i] if i < len(po_numbers) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
        })
    return rows


def _extract_job_ins(form) -> list:
    """The read-only "Job In details" breakdown - which job in(s) the
    returned/jobbed goods on the Products card were imported from - round-
    tripped as hidden fields so it survives a save/reopen, mirrors
    _extract_product_sources. Display only, never printed."""
    names = form.getlist("ji_manufacturer_name[]")
    gstins = form.getlist("ji_manufacturer_gstin[]")
    job_out_challans = form.getlist("ji_job_out_challan_no[]")
    jw_challans = form.getlist("ji_jw_challan_no[]")
    jw_challan_dates = form.getlist("ji_jw_challan_date[]")
    inward_nos = form.getlist("ji_stock_inward_no[]")
    inward_dates = form.getlist("ji_stock_inward_date[]")
    n = max(len(names), len(gstins), len(job_out_challans), len(jw_challans),
            len(jw_challan_dates), len(inward_nos), len(inward_dates))
    rows = []
    for i in range(n):
        rows.append({
            "manufacturer_name": names[i] if i < len(names) else "",
            "manufacturer_gstin": gstins[i] if i < len(gstins) else "",
            "job_out_challan_no": job_out_challans[i] if i < len(job_out_challans) else "",
            "jw_challan_no": jw_challans[i] if i < len(jw_challans) else "",
            "jw_challan_date": jw_challan_dates[i] if i < len(jw_challan_dates) else "",
            "stock_inward_no": inward_nos[i] if i < len(inward_nos) else "",
            "stock_inward_date": inward_dates[i] if i < len(inward_dates) else "",
        })
    return rows


def _extract_packing_allocations(form) -> list:
    """The Export Packing List's container split, captured on this same form
    (the packing list has no form of its own - it is generated from here).
    One row per "N boxes of goods line X went into container C"; the derived
    columns are submitted too so a hand-corrected figure survives."""
    container_indexes = form.getlist("alloc_container_index[]")
    item_indexes = form.getlist("alloc_invoice_item_index[]")
    boxes = form.getlist("alloc_boxes[]")
    group_labels = form.getlist("alloc_group_label[]")
    pallets = form.getlist("alloc_pallets[]")
    nets = form.getlist("alloc_net_weight[]")
    grosses = form.getlist("alloc_gross_weight[]")

    def at(values, i):
        return values[i] if i < len(values) else ""

    return [
        {
            "container_index": at(container_indexes, i),
            "invoice_item_index": at(item_indexes, i),
            "quantity_boxes": at(boxes, i),
            "group_label": at(group_labels, i),
            "pallets": at(pallets, i),
            "net_weight_kg": at(nets, i),
            "gross_weight_kg": at(grosses, i),
        }
        for i in range(len(item_indexes))
    ]


def _product_maps(items) -> tuple:
    """(pallet_types_map, product_meta_map), both keyed by product_id, for
    the goods rows already tied to a catalog product.

    - pallet_types_map fills each row's Pallet type dropdown ('loose', the
      built-in no-pallet default, is added by the form itself). Mirrors
      app/routes/proforma_invoices.py's helper of the same name; without it
      every row's dropdown falls back to just 'Loose'.
    - product_meta_map carries the per-box weights and the HSN group heading
      the Export Packing List's split auto-fills from, so a saved invoice's
      rows behave like freshly picked ones without a second round trip.

    Both come out of one pass so a product is fetched only once."""
    container = current_app.container
    pallet_types_map, meta_map = {}, {}
    for item in items or []:
        raw_id = item.get("product_id") if isinstance(item, dict) else item.product_id
        if not raw_id:
            continue
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if product_id in pallet_types_map:
            continue
        try:
            product = container.product_service.get_product(product_id, g.user.company_id)
        except NotFoundError:
            continue
        pallet_types_map[product_id] = [
            {"name": pt.name, "boxes_per_pallet": pt.boxes_per_pallet,
             "alt_qty_per_pallet": pallet_alt_quantity(pt, product), "weight_kg": pt.weight_kg}
            for pt in container.product_service.pallet_types_for_product(product_id)
        ]
        meta_map[product_id] = {
            "net_weight_kg": product.net_weight_kg,
            "gross_weight_kg": product.gross_weight_kg,
            "group_label": container.export_packing_list_service.group_label_for(product, product.product_name),
        }
    return pallet_types_map, meta_map


def _allocation_rows(invoice, form_allocations):
    """The container-split rows to render: whatever was just submitted (so a
    rejected save comes back with the user's own split intact), else the
    invoice's saved packing list, else nothing - the form's JavaScript then
    seeds the default "every line whole, in container 1"."""
    if form_allocations is not None:
        return form_allocations
    if not invoice:
        return []
    packing_list = current_app.container.export_packing_list_service.get_for_invoice(
        invoice.id, g.user.company_id
    )
    if not packing_list:
        return []
    # Goods lines are numbered 1..n in order, so a stored invoice_item_sr_no
    # maps straight back onto the products table's row index.
    return [
        {
            "container_index": (item.container_sr_no or 1) - 1,
            "invoice_item_index": (item.invoice_item_sr_no or 1) - 1,
            "quantity_boxes": item.quantity_boxes,
            "group_label": item.group_label,
            "pallets": item.pallets,
            "net_weight_kg": item.net_weight_kg,
            "gross_weight_kg": item.gross_weight_kg,
        }
        for item in packing_list.items
    ]


def _form_context():
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    proforma_invoices = container.proforma_invoice_service.list_all(g.user.company_id)
    buyers = container.buyer_service.list_all(g.user.company_id)
    company = container.company_service.get(g.user.company_id)
    permits = container.permit_service.list_all(g.user.company_id)
    # The Booking no. dropdown - list_all only returns the header + a row
    # count, so each one is re-fetched for its full containers/
    # container_details to drive the client-side auto-fill (see the
    # booking_no <select> in form.html). Master-data sized list, so the
    # extra per-row fetch isn't worth a bespoke bulk query. This also feeds
    # the Transporter field, which - like every other field in the Container
    # details card except Booking no. itself - is now read-only, filled in
    # from whichever booking is picked rather than chosen separately.
    bookings = [
        container.booking_detail_service.get(b.id, g.user.company_id)
        for b in container.booking_detail_service.list_all(g.user.company_id)
    ]
    return leads, proforma_invoices, buyers, company, permits, bookings


def _render_form(invoice, form_data, form_items, containers=None,
                 container_details=None, purchase_details=None, product_sources=None,
                 job_ins=None, allocations=None, status_code=200):
    leads, proforma_invoices, buyers, company, permits, bookings = _form_context()
    rows = form_items if form_items is not None else (invoice.items if invoice else [])
    pallet_types_map, product_meta_map = _product_maps(rows)
    html = render_template(
        "export_invoices/form.html", invoice=invoice, leads=leads, proforma_invoices=proforma_invoices,
        buyers=buyers, company=company, permits=permits, bookings=bookings,
        form_data=form_data, form_items=form_items,
        form_containers=containers,
        form_container_details=container_details, form_purchase_details=purchase_details,
        form_product_sources=product_sources, form_job_ins=job_ins,
        form_allocations=_allocation_rows(invoice, allocations),
        pallet_types_map=pallet_types_map, product_meta_map=product_meta_map,
        today=date.today().isoformat(),
    )
    return (html, status_code) if status_code != 200 else html


@export_invoices_bp.route("/")
@login_required
def list_export_invoices():
    invoices = current_app.container.export_invoice_service.list_all(g.user.company_id)
    return render_template("export_invoices/list.html", invoices=invoices)


@export_invoices_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_export_invoice():
    container = current_app.container
    if request.method == "POST":
        try:
            invoice = container.export_invoice_service.create(
                current_user=g.user, fields=_extract_fields(request.form),
                raw_items=_extract_items(request.form), pdf_file=request.files.get("shipping_bill_pdf"),
            )
            flash(f"Export invoice {invoice.export_invoice_number} created.", "success")
            return redirect(url_for("export_invoices.view_export_invoice", export_invoice_id=invoice.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            fields = _extract_fields(request.form)
            return _render_form(
                None, request.form, _extract_items(request.form),
                containers=fields["containers"],
                container_details=fields["container_details_list"], purchase_details=fields["purchase_details"],
                product_sources=fields["product_sources"], job_ins=fields["job_ins"],
                allocations=fields["packing_allocations"],
                status_code=400,
            )

    # GET: optionally prefill from one or more proforma invoices.
    prefill = None
    form_items = None
    containers = container_details = purchase_details = product_sources = job_ins = None
    raw_ids = request.args.get("proforma_invoice_ids") or request.args.get("proforma_invoice_id")
    proforma_ids = [p for p in (raw_ids.split(",") if raw_ids else []) if p.strip()]
    if proforma_ids:
        built = container.export_invoice_service.build_prefill_from_proformas(proforma_ids, g.user.company_id)
        prefill = dict(built["fields"])
        prefill["invoice_date"] = date.today().isoformat()
        form_items = built["items"]
        purchase_details = built["purchase_details"]
        product_sources = built["product_sources"]
        job_ins = built["job_ins"]
    return _render_form(None, prefill, form_items, containers=containers,
                        container_details=container_details, purchase_details=purchase_details,
                        product_sources=product_sources, job_ins=job_ins)


@export_invoices_bp.route("/api/prefill")
@login_required
def export_invoice_prefill():
    """The same prefill as `?proforma_invoice_ids=`, as JSON.

    The form's "Load goods & details from selected PIs" button uses this
    instead of reloading the page, so it can overwrite only the fields the
    proforma invoices actually decide and leave everything else the user has
    already typed (invoice number, permit details, containers, 11B, ...)
    untouched."""
    raw_ids = request.args.get("proforma_invoice_ids") or request.args.get("proforma_invoice_id") or ""
    proforma_ids = [p for p in raw_ids.split(",") if p.strip()]
    built = current_app.container.export_invoice_service.build_prefill_from_proformas(
        proforma_ids, g.user.company_id
    )
    pallet_types_map, product_meta_map = _product_maps(built["items"])

    def iso(value):
        return str(value)[:10] if value else ""

    items = []
    for item in built["items"]:
        try:
            product_id = int(item["product_id"]) if item.get("product_id") else None
        except (TypeError, ValueError):
            product_id = None
        meta = product_meta_map.get(product_id, {})
        items.append({
            **{k: ("" if v is None else v) for k, v in item.items()},
            # The catalog extras the rendered rows carry as data- attributes,
            # so a loaded row behaves like a freshly picked product.
            "pallet_types": pallet_types_map.get(product_id, []),
            "net_weight_kg": meta.get("net_weight_kg") or "",
            "gross_weight_kg": meta.get("gross_weight_kg") or "",
            "group_label": meta.get("group_label") or "",
        })

    fields = dict(built["fields"])
    for key in ("buyer_order_date", "epcg_date"):
        fields[key] = iso(fields.get(key))
    fields = {k: ("" if v is None else v) for k, v in fields.items()}

    job_ins = [
        {**{k: ("" if v is None else v) for k, v in ji.items()},
         "jw_challan_date": iso(ji.get("jw_challan_date")),
         "stock_inward_date": iso(ji.get("stock_inward_date"))}
        for ji in built["job_ins"]
    ]
    return jsonify({"fields": fields, "items": items, "purchase_details": built["purchase_details"],
                    "product_sources": built["product_sources"], "job_ins": job_ins})


@export_invoices_bp.route("/<int:export_invoice_id>")
@login_required
def view_export_invoice(export_invoice_id):
    container = current_app.container
    try:
        invoice = container.export_invoice_service.get(export_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    packing_list = container.export_packing_list_service.get_for_invoice(invoice.id, g.user.company_id)
    return render_template("export_invoices/print.html", invoice=invoice, company=company, packing_list=packing_list)


@export_invoices_bp.route("/<int:export_invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_export_invoice(export_invoice_id):
    container = current_app.container
    try:
        invoice = container.export_invoice_service.get(export_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.export_invoice_service.update(
                current_user=g.user, invoice_id=export_invoice_id, fields=_extract_fields(request.form),
                raw_items=_extract_items(request.form), pdf_file=request.files.get("shipping_bill_pdf"),
                remove_pdf=bool(request.form.get("remove_shipping_bill_pdf")),
            )
            flash(f"Export invoice {invoice.export_invoice_number} updated.", "success")
            return redirect(url_for("export_invoices.view_export_invoice", export_invoice_id=export_invoice_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            fields = _extract_fields(request.form)
            return _render_form(
                invoice, request.form, _extract_items(request.form),
                containers=fields["containers"],
                container_details=fields["container_details_list"], purchase_details=fields["purchase_details"],
                product_sources=fields["product_sources"], job_ins=fields["job_ins"],
                allocations=fields["packing_allocations"],
                status_code=400,
            )

    return _render_form(
        invoice, None, None,
        containers=invoice.containers,
        container_details=invoice.container_details, purchase_details=invoice.purchase_details,
        product_sources=invoice.product_sources, job_ins=invoice.job_ins,
    )


@export_invoices_bp.route("/<int:export_invoice_id>/delete", methods=["POST"])
@login_required
def delete_export_invoice(export_invoice_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Export invoice not deleted.", "error")
        return redirect(url_for("export_invoices.view_export_invoice", export_invoice_id=export_invoice_id))
    try:
        invoice = current_app.container.export_invoice_service.get(export_invoice_id, g.user.company_id)
        current_app.container.export_invoice_service.delete(g.user, export_invoice_id)
        flash(f"Export invoice {invoice.export_invoice_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("export_invoices.list_export_invoices"))


@export_invoices_bp.route("/<int:export_invoice_id>/versions")
@admin_required
def export_invoice_versions(export_invoice_id):
    container = current_app.container
    try:
        invoice = container.export_invoice_service.get(export_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    versions = container.document_version_service.list_for_document("export_invoice", export_invoice_id)
    rows = [
        {
            "version_number": v.version_number,
            "created_at": v.created_at,
            "changed_by_name": v.changed_by_name,
            "url": url_for("export_invoices.view_export_invoice", export_invoice_id=export_invoice_id) if i == 0 else
                   url_for("export_invoices.view_export_invoice_version",
                           export_invoice_id=export_invoice_id, version_number=v.version_number),
        }
        for i, v in enumerate(versions)
    ]
    return render_template(
        "document_versions/list.html", document_number=invoice.export_invoice_number, versions=rows,
        back_url=url_for("export_invoices.view_export_invoice", export_invoice_id=export_invoice_id),
    )


@export_invoices_bp.route("/<int:export_invoice_id>/versions/<int:version_number>")
@admin_required
def view_export_invoice_version(export_invoice_id, version_number):
    container = current_app.container
    try:
        container.export_invoice_service.get(export_invoice_id, g.user.company_id)  # tenant-scope check
        historical_invoice, version = container.document_version_service.get_version(
            "export_invoice", export_invoice_id, version_number
        )
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    return render_template(
        "export_invoices/print.html", invoice=historical_invoice, company=company,
        historical_version=version, packing_list=None,
    )
