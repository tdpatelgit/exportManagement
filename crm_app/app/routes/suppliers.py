"""
app/routes/suppliers.py
------------------------
HTTP layer for Suppliers. Unlike Buyer/Exporter (see app/routes/parties.py),
a Supplier's profile mirrors OUR COMPANY's own shape - GSTIN/PAN/IEC/bank/
contacts - instead of a lead's phone/email/socials, so its edit form and
extraction helpers below mirror app/routes/company.py rather than
parties.py. Payments/documents/communications still reuse the same shared
mechanism as Buyer/Exporter (parent_type='supplier').
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort
)

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

suppliers_bp = Blueprint("suppliers", __name__, url_prefix="/suppliers")


def _extract_contact_details(form) -> list:
    types = form.getlist("detail_type[]")
    values = form.getlist("detail_value[]")
    primaries = set(form.getlist("detail_primary[]"))
    details = []
    for i, (t, v) in enumerate(zip(types, values)):
        if v.strip():
            details.append({"type": t, "value": v.strip(), "is_primary": str(i) in primaries})
    return details


def _extract_contact_persons(form) -> list:
    names = form.getlist("person_name[]")
    primaries = set(form.getlist("person_primary[]"))
    persons = []
    for i, name in enumerate(names):
        if name.strip():
            persons.append({"name": name.strip(), "is_primary": str(i) in primaries})
    return persons


def _extract_bank_details(form) -> list:
    bank_names = form.getlist("bank_name[]")
    account_numbers = form.getlist("bank_account_number[]")
    ifsc_codes = form.getlist("bank_ifsc_code[]")
    swift_codes = form.getlist("bank_swift_code[]")
    branches = form.getlist("bank_branch[]")
    addresses = form.getlist("bank_address[]")
    primaries = set(form.getlist("bank_primary[]"))
    banks = []
    for i, name in enumerate(bank_names):
        row = {
            "bank_name": name.strip(),
            "account_number": account_numbers[i].strip() if i < len(account_numbers) else "",
            "ifsc_code": ifsc_codes[i].strip() if i < len(ifsc_codes) else "",
            "swift_code": swift_codes[i].strip() if i < len(swift_codes) else "",
            "branch": branches[i].strip() if i < len(branches) else "",
            "bank_address": addresses[i].strip() if i < len(addresses) else "",
            "is_primary": str(i) in primaries,
        }
        if any(v for k, v in row.items() if k != "is_primary"):
            banks.append(row)
    return banks


@suppliers_bp.route("/")
@login_required
def list_suppliers():
    status = request.args.get("status") or None
    suppliers = current_app.container.supplier_service.list_all(g.user.company_id, status=status)
    return render_template("suppliers/list.html", suppliers=suppliers, status_filter=status)


@suppliers_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_supplier():
    container = current_app.container
    if request.method == "POST":
        try:
            supplier = container.supplier_service.create(
                g.user,
                company_name=request.form.get("company_name", ""),
                address=request.form.get("address", ""),
                gstin=request.form.get("gstin", ""),
                pan_no=request.form.get("pan_no", ""),
                iec=request.form.get("iec", ""),
                contact_details=_extract_contact_details(request.form),
                contact_persons=_extract_contact_persons(request.form),
                bank_details=_extract_bank_details(request.form),
            )
            flash(f"Supplier '{supplier.company_name}' added.", "success")
            return redirect(url_for("suppliers.view_supplier", supplier_id=supplier.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template("suppliers/new.html"), 400

    return render_template("suppliers/new.html")


@suppliers_bp.route("/<int:supplier_id>")
@login_required
def view_supplier(supplier_id):
    container = current_app.container
    try:
        supplier = container.supplier_service.get(supplier_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    communications = container.communication_service.list_for("supplier", supplier_id)
    payments = container.payment_repo.list_for("supplier", supplier_id)
    total_received_inr = sum(p.amount_inr for p in payments)
    document_rows = container.supplier_service.document_feed(supplier)
    return render_template(
        "suppliers/detail.html", supplier=supplier, communications=communications,
        payments=payments, document_rows=document_rows, total_received_inr=total_received_inr,
    )


@suppliers_bp.route("/<int:supplier_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_supplier(supplier_id):
    container = current_app.container
    try:
        supplier = container.supplier_service.get(supplier_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.supplier_service.update_profile(
                supplier_id, g.user,
                company_name=request.form.get("company_name", ""),
                address=request.form.get("address", ""),
                gstin=request.form.get("gstin", ""),
                pan_no=request.form.get("pan_no", ""),
                iec=request.form.get("iec", ""),
                contact_details=_extract_contact_details(request.form),
                contact_persons=_extract_contact_persons(request.form),
                bank_details=_extract_bank_details(request.form),
            )
            flash("Supplier profile saved.", "success")
            return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            supplier = container.supplier_service.get(supplier_id, g.user.company_id)

    return render_template("suppliers/edit.html", supplier=supplier)


@suppliers_bp.route("/<int:supplier_id>/status", methods=["POST"])
@login_required
def update_status(supplier_id):
    try:
        current_app.container.supplier_service.update_status(
            supplier_id, g.user, request.form.get("status", "")
        )
        flash("Supplier status updated.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/communications", methods=["POST"])
@login_required
def add_communication(supplier_id):
    try:
        current_app.container.supplier_service.add_communication(
            supplier_id, g.user,
            comm_date=request.form.get("comm_date", ""),
            mode=request.form.get("mode", ""),
            description=request.form.get("description", ""),
            follow_up_date=request.form.get("follow_up_date") or None,
        )
        flash("Communication logged.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/payments", methods=["POST"])
@login_required
def add_payment(supplier_id):
    try:
        amount_raw = request.form.get("amount_original", "0")
        amount = float(amount_raw) if amount_raw else 0
        payment = current_app.container.supplier_service.add_payment(
            supplier_id, g.user,
            account_name=request.form.get("account_name", ""),
            payment_datetime=request.form.get("payment_datetime", ""),
            amount_original=amount,
            currency_code=request.form.get("currency_code", ""),
        )
        flash(
            f"Payment recorded: {payment.amount_original} {payment.currency_code} "
            f"= ₹{payment.amount_inr:,.2f} (rate {payment.conversion_rate}).",
            "success",
        )
    except ValueError:
        flash("Amount must be a valid number.", "error")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))


@suppliers_bp.route("/<int:supplier_id>/documents", methods=["POST"])
@login_required
def add_document(supplier_id):
    try:
        current_app.container.supplier_service.add_document(
            supplier_id, g.user,
            document_name=request.form.get("document_name", ""),
            document_type=request.form.get("document_type", ""),
            document_date=request.form.get("document_date", ""),
            notes=request.form.get("notes", ""),
        )
        flash("Document recorded.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("suppliers.view_supplier", supplier_id=supplier_id))
