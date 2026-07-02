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
                contact_details=_extract_contact_details(request.form),
                contact_persons=_extract_contact_persons(request.form),
            )
            flash("Our Company profile saved.", "success")
            return redirect(url_for("company.settings"))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    company = container.company_service.get()
    return render_template("company/settings.html", company=company)
