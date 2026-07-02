"""
app/routes/company.py
----------------------
"OUR COMPANY" - the single-row table describing the CRM owner's own
business (GSTIN, PAN, IEC, contact points). Admin-only, as specified.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g

from app.exceptions import ValidationError, PermissionDeniedError
from app.utils import admin_required

company_bp = Blueprint("company", __name__, url_prefix="/company")


def _extract_contact_details(form) -> list:
    types = form.getlist("detail_type[]")
    values = form.getlist("detail_value[]")
    primaries = set(form.getlist("detail_primary[]"))  # indices marked primary
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
    """Keeps any row that has at least one field filled in (rather than
    silently dropping incomplete rows), so the service layer can reject
    rows that are missing a compulsory field instead of ignoring them."""
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


@company_bp.route("/", methods=["GET", "POST"])
@admin_required
def settings():
    container = current_app.container

    if request.method == "POST":
        try:
            container.company_service.save(
                current_user=g.user,
                company_name=request.form.get("company_name", ""),
                gstin=request.form.get("gstin", ""),
                pan_no=request.form.get("pan_no", ""),
                iec=request.form.get("iec", ""),
                lut=request.form.get("lut", ""),
                bin_no=request.form.get("bin_no", ""),
                contact_details=_extract_contact_details(request.form),
                contact_persons=_extract_contact_persons(request.form),
                bank_details=_extract_bank_details(request.form),
            )
            flash("Our Company profile saved.", "success")
            return redirect(url_for("company.settings"))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    company = container.company_service.get()
    return render_template("company/settings.html", company=company)
