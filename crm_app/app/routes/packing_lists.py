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
from app.utils import login_required, admin_required

packing_lists_bp = Blueprint("packing_lists", __name__, url_prefix="/packing-lists")

_HEADER_FIELDS = [
    "packing_list_date", "lead_id", "proforma_invoice_id", "quotation_id", "export_ref_no", "buyer_order_no",
    "other_reference", "remarks",
]


def _extract_header(form) -> dict:
    return {key: form.get(key, "") for key in _HEADER_FIELDS}


def _extract_items(form) -> list:
    product_ids = form.getlist("item_product_id[]")
    product_names = form.getlist("item_product_name[]")
    design_ids = form.getlist("item_design_id[]")
    design_names = form.getlist("item_design_name[]")
    hsn_codes = form.getlist("item_hsn_code[]")
    box_per_pallets = form.getlist("item_box_per_pallet[]")
    pallets = form.getlist("item_pallets[]")
    boxes = form.getlist("item_quantity_boxes[]")
    pcs = form.getlist("item_pcs[]")
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
            "box_per_pallet": box_per_pallets[i] if i < len(box_per_pallets) else "",
            "pallets": pallets[i] if i < len(pallets) else "",
            "quantity_boxes": boxes[i] if i < len(boxes) else "",
            "pcs": pcs[i] if i < len(pcs) else "",
            "quantity_value": values[i] if i < len(values) else "",
            "unit": units[i] if i < len(units) else "SQM",
            "net_weight_kg": net_weights[i] if i < len(net_weights) else "",
            "gross_weight_kg": gross_weights[i] if i < len(gross_weights) else "",
        })
    return items


def _group_items_by_product(items) -> list:
    """Groups raw item dicts/PackingListItem rows by (product_id,
    product_name), preserving first-seen order of both groups and items
    within a group. The edit form renders one 'product block' per group,
    holding every design line for that product - purely a rendering
    grouping: each design still submits its own full set of item_*[]
    fields (see _extract_items) and becomes its own PackingListItem /
    printed row regardless of how the form displayed it."""
    def get(item, key):
        return (item.get(key) if isinstance(item, dict) else getattr(item, key)) or ""
    groups, index = [], {}
    for item in items:
        key = (get(item, "product_id"), get(item, "product_name"))
        if key not in index:
            index[key] = len(groups)
            groups.append([])
        groups[index[key]].append(item)
    return groups


def _source_boxes_map(proforma_invoice_id, quotation_id, company_id) -> dict:
    """product_id -> boxes for that product on the linked quotation/proforma
    invoice, so the packing list form can remind the user how many boxes
    they're meant to split across design rows - even when reopening a
    packing list that was created a while ago."""
    container = current_app.container
    source_items = []
    if proforma_invoice_id:
        try:
            invoice = container.proforma_invoice_service.get(int(proforma_invoice_id), company_id)
            source_items = invoice.items
        except (NotFoundError, ValueError, TypeError):
            pass
    elif quotation_id:
        try:
            quotation = container.quotation_service.get(int(quotation_id), company_id)
            source_items = quotation.items
        except (NotFoundError, ValueError, TypeError):
            pass
    return {item.product_id: item.quantity_boxes for item in source_items if item.product_id}


def _form_context():
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    invoices = container.proforma_invoice_service.list_all(g.user.company_id)
    quotations = container.quotation_service.list_all(g.user.company_id)
    return leads, invoices, quotations


def _product_map(items) -> dict:
    """Maps product_id -> that Product, so the form can prefill each row's
    packing-spec dataset attributes (drives the Boxes-based Qty/Pcs/Box-per-
    pallet auto-calc, same as _alt_qty_map on the quotation/proforma forms)
    for rows already tied to a catalog product."""
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
        try:
            result[product_id] = container.product_service.get_product(product_id, g.user.company_id)
        except NotFoundError:
            pass
    return result


def catalog_maps(packing_lists) -> tuple:
    """(product_map, design_map) for every item across the given packing
    lists - the print sheet reads each product's shared per-box packing spec
    and each design's photo from these. Also used by the combined
    invoice + packing view in routes/proforma_invoices.py."""
    container = current_app.container
    product_map, design_map = {}, {}
    for packing_list in packing_lists:
        for item in packing_list.items:
            if item.product_id and item.product_id not in product_map:
                try:
                    product_map[item.product_id] = container.product_service.get_product(item.product_id, g.user.company_id)
                except NotFoundError:
                    pass
            if item.design_id and item.design_id not in design_map:
                try:
                    design_map[item.design_id] = container.product_service.get_design(item.design_id, g.user.company_id)
                except NotFoundError:
                    pass
    return product_map, design_map


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
            leads, invoices, quotations = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "packing_lists/form.html", packing_list=None, leads=leads, invoices=invoices, quotations=quotations,
                form_data=request.form, form_items=items, item_groups=_group_items_by_product(items),
                product_map=_product_map(items), today=date.today().isoformat(),
                source_boxes_map=_source_boxes_map(
                    request.form.get("proforma_invoice_id"), request.form.get("quotation_id"), g.user.company_id,
                ),
            ), 400

    leads, invoices, quotations = _form_context()
    prefill = None
    form_items = None
    proforma_invoice_id = request.args.get("proforma_invoice_id")
    quotation_id = request.args.get("quotation_id")
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
    elif quotation_id:
        try:
            quotation = container.quotation_service.get(int(quotation_id), g.user.company_id)
            built = container.packing_list_service.build_prefill_from_quotation(quotation)
            prefill = built["fields"]
            prefill["packing_list_date"] = date.today().isoformat()
            form_items = built["items"]
        except (NotFoundError, ValueError):
            pass
    elif lead_id:
        try:
            lead = container.lead_service.get(int(lead_id), g.user.company_id)
            prefill = {"lead_id": lead.id, "packing_list_date": date.today().isoformat()}
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "packing_lists/form.html", packing_list=None, leads=leads, invoices=invoices, quotations=quotations,
        form_data=prefill, form_items=form_items, item_groups=_group_items_by_product(form_items or []),
        product_map=_product_map(form_items) if form_items else {}, today=date.today().isoformat(),
        source_boxes_map=_source_boxes_map(proforma_invoice_id, quotation_id, g.user.company_id),
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
    product_map, design_map = catalog_maps([packing_list])
    return render_template("packing_lists/print.html", packing_list=packing_list, company=company,
                           product_map=product_map, design_map=design_map)


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
            leads, invoices, quotations = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "packing_lists/form.html", packing_list=packing_list, leads=leads, invoices=invoices,
                quotations=quotations,
                form_data=request.form, form_items=items, item_groups=_group_items_by_product(items),
                product_map=_product_map(items), today=date.today().isoformat(),
                source_boxes_map=_source_boxes_map(
                    request.form.get("proforma_invoice_id"), request.form.get("quotation_id"), g.user.company_id,
                ),
            ), 400

    leads, invoices, quotations = _form_context()
    return render_template(
        "packing_lists/form.html", packing_list=packing_list, leads=leads, invoices=invoices, quotations=quotations,
        form_data=None, form_items=None, item_groups=_group_items_by_product(packing_list.items),
        product_map=_product_map(packing_list.items), today=date.today().isoformat(),
        source_boxes_map=_source_boxes_map(packing_list.proforma_invoice_id, packing_list.quotation_id, g.user.company_id),
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


@packing_lists_bp.route("/<int:packing_list_id>/versions")
@admin_required
def packing_list_versions(packing_list_id):
    container = current_app.container
    try:
        packing_list = container.packing_list_service.get(packing_list_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    versions = container.document_version_service.list_for_document("packing_list", packing_list_id)
    rows = [
        {
            "version_number": v.version_number,
            "created_at": v.created_at,
            "changed_by_name": v.changed_by_name,
            "url": url_for("packing_lists.view_packing_list", packing_list_id=packing_list_id) if i == 0 else
                   url_for("packing_lists.view_packing_list_version",
                           packing_list_id=packing_list_id, version_number=v.version_number),
        }
        for i, v in enumerate(versions)
    ]
    return render_template(
        "document_versions/list.html", document_number=packing_list.packing_list_number, versions=rows,
        back_url=url_for("packing_lists.view_packing_list", packing_list_id=packing_list_id),
    )


@packing_lists_bp.route("/<int:packing_list_id>/versions/<int:version_number>")
@admin_required
def view_packing_list_version(packing_list_id, version_number):
    container = current_app.container
    try:
        container.packing_list_service.get(packing_list_id, g.user.company_id)  # tenant-scope check
        historical_packing_list, version = container.document_version_service.get_version(
            "packing_list", packing_list_id, version_number
        )
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    product_map, design_map = catalog_maps([historical_packing_list])
    return render_template(
        "packing_lists/print.html", packing_list=historical_packing_list, company=company,
        product_map=product_map, design_map=design_map, historical_version=version,
    )
