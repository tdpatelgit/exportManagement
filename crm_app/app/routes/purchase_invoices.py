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
from app.utils import login_required, admin_required

purchase_invoices_bp = Blueprint("purchase_invoices", __name__, url_prefix="/purchase-invoices")

_HEADER_FIELDS = [
    "invoice_number", "invoice_date", "purchase_order_id", "lead_id", "seller_supplier_id",
    "seller_name", "seller_address", "seller_pan", "seller_gstin", "seller_ref_no",
    "port_of_loading", "port_of_discharge", "container_details",
    "transporter_name", "epcg_number", "epcg_date",
    "discount_amount", "insurance_other", "freight", "igst_amount", "cgst_amount", "sgst_amount", "round_off",
    "remarks",
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


def _extract_vehicle_numbers(form) -> list:
    return form.getlist("vehicle_number[]")


def _form_context():
    """(leads, purchase orders, suppliers) for the form's Start-from and
    Seller pickers."""
    container = current_app.container
    leads = container.lead_service.list_for_dashboard(g.user)
    purchase_orders = container.purchase_order_service.list_all(g.user.company_id)
    suppliers = container.supplier_service.list_all(g.user.company_id)
    return leads, purchase_orders, suppliers


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
            leads, purchase_orders, suppliers = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "purchase_invoices/form.html", purchase_invoice=None, leads=leads, purchase_orders=purchase_orders,
                suppliers=suppliers, form_data=request.form, form_items=items,
                form_vehicle_numbers=_extract_vehicle_numbers(request.form), today=date.today().isoformat(),
            ), 400

    leads, purchase_orders, suppliers = _form_context()
    prefill = None
    form_items = None
    purchase_order_id = request.args.get("purchase_order_id")
    lead_id = request.args.get("lead_id")
    if purchase_order_id:
        try:
            purchase_order = container.purchase_order_service.get(int(purchase_order_id), g.user.company_id)
            built = container.purchase_invoice_service.build_prefill_from_purchase_order(purchase_order)
            prefill = built["fields"]
            prefill["invoice_date"] = date.today().isoformat()
            form_items = built["items"]
        except (NotFoundError, ValueError):
            pass
    elif lead_id:
        try:
            lead = container.lead_service.get(int(lead_id), g.user.company_id)
            prefill = {"lead_id": lead.id, "invoice_date": date.today().isoformat()}
        except (NotFoundError, ValueError):
            pass
    return render_template(
        "purchase_invoices/form.html", purchase_invoice=None, leads=leads, purchase_orders=purchase_orders,
        suppliers=suppliers, form_data=prefill, form_items=form_items,
        form_vehicle_numbers=None, today=date.today().isoformat(),
    )


@purchase_invoices_bp.route("/<int:purchase_invoice_id>")
@login_required
def view_purchase_invoice(purchase_invoice_id):
    container = current_app.container
    try:
        purchase_invoice = container.purchase_invoice_service.get(purchase_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    packing_lists = container.packing_list_service.list_for_purchase_invoice(purchase_invoice_id, g.user.company_id)
    return render_template("purchase_invoices/view.html", purchase_invoice=purchase_invoice,
                           packing_lists=packing_lists)


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
            leads, purchase_orders, suppliers = _form_context()
            items = _extract_items(request.form)
            return render_template(
                "purchase_invoices/form.html", purchase_invoice=purchase_invoice, leads=leads,
                purchase_orders=purchase_orders, suppliers=suppliers, form_data=request.form, form_items=items,
                form_vehicle_numbers=_extract_vehicle_numbers(request.form), today=date.today().isoformat(),
            ), 400

    leads, purchase_orders, suppliers = _form_context()
    return render_template(
        "purchase_invoices/form.html", purchase_invoice=purchase_invoice, leads=leads,
        purchase_orders=purchase_orders, suppliers=suppliers, form_data=None, form_items=None,
        form_vehicle_numbers=None, today=date.today().isoformat(),
    )


@purchase_invoices_bp.route("/<int:purchase_invoice_id>/delete", methods=["POST"])
@login_required
def delete_purchase_invoice(purchase_invoice_id):
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
    )
