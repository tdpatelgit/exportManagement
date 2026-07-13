"""
app/routes/leads.py
--------------------
HTTP layer for leads. Every handler is thin: parse the request, call a
service method, flash the result. Validation/permission logic lives in
LeadService, not here (Single Responsibility).
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort
)

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

leads_bp = Blueprint("leads", __name__, url_prefix="/leads")


def _extract_contacts_from_form(form) -> list:
    """The lead form submits parallel arrays: contact_name[], contact_phone[],
    contact_email[], contact_primary[] (a single index marked primary)."""
    names = form.getlist("contact_name[]")
    phones = form.getlist("contact_phone[]")
    emails = form.getlist("contact_email[]")
    primary_index = form.get("primary_contact_index", "0")
    contacts = []
    for i, name in enumerate(names):
        if not name.strip():
            continue
        contacts.append({
            "name": name.strip(),
            "phone": phones[i].strip() if i < len(phones) else "",
            "email": emails[i].strip() if i < len(emails) else "",
            "is_primary": str(i) == primary_index,
        })
    return contacts


@leads_bp.route("/")
@login_required
def list_leads():
    status_filter = request.args.get("status") or None
    leads = current_app.container.lead_service.list_for_dashboard(g.user, status=status_filter)
    return render_template("leads/list.html", leads=leads, status_filter=status_filter)


@leads_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_lead():
    if request.method == "POST":
        try:
            contacts = _extract_contacts_from_form(request.form)
            lead = current_app.container.lead_service.create_lead(
                current_user=g.user,
                company_name=request.form.get("company_name", ""),
                phone=request.form.get("phone", ""),
                email=request.form.get("email", ""),
                facebook=request.form.get("facebook", ""),
                instagram=request.form.get("instagram", ""),
                other_social=request.form.get("other_social", ""),
                contacts=contacts,
            )
            flash(f"Lead '{lead.company_name}' created.", "success")
            return redirect(url_for("leads.view_lead", lead_id=lead.id))
        except ValidationError as e:
            flash(str(e), "error")
            return render_template("leads/form.html", lead=None, form_data=request.form), 400

    return render_template("leads/form.html", lead=None, form_data=None)


@leads_bp.route("/<int:lead_id>")
@login_required
def view_lead(lead_id):
    container = current_app.container
    try:
        lead = container.lead_service.get(lead_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    if not g.user.is_admin and lead.created_by != g.user.id:
        abort(403)
    communications = container.communication_service.list_for("lead", lead_id)
    quotations = container.quotation_service.list_for_lead(lead_id)
    proforma_invoices = container.proforma_invoice_service.list_for_lead(lead_id)
    return render_template(
        "leads/detail.html", lead=lead, communications=communications,
        quotations=quotations, proforma_invoices=proforma_invoices,
    )


@leads_bp.route("/<int:lead_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_lead(lead_id):
    container = current_app.container
    try:
        lead = container.lead_service.get(lead_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.lead_service.update_compulsory_fields(lead_id, g.user, {
                "company_name": request.form.get("company_name", ""),
                "phone": request.form.get("phone", ""),
                "email": request.form.get("email", ""),
                "facebook": request.form.get("facebook", ""),
                "instagram": request.form.get("instagram", ""),
                "other_social": request.form.get("other_social", ""),
            })
            flash("Lead details updated.", "success")
            return redirect(url_for("leads.view_lead", lead_id=lead_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    return render_template("leads/form.html", lead=lead, form_data=None, editing=True)


@leads_bp.route("/<int:lead_id>/status", methods=["POST"])
@login_required
def update_status(lead_id):
    try:
        current_app.container.lead_service.update_status(
            lead_id, g.user, request.form.get("status", "")
        )
        flash("Lead status updated.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("leads.view_lead", lead_id=lead_id))


@leads_bp.route("/<int:lead_id>/contacts", methods=["POST"])
@login_required
def add_contact(lead_id):
    try:
        current_app.container.lead_service.add_contact(
            lead_id, g.user,
            name=request.form.get("name", ""),
            phone=request.form.get("phone", ""),
            email=request.form.get("email", ""),
        )
        flash("Contact person added.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("leads.view_lead", lead_id=lead_id))


@leads_bp.route("/<int:lead_id>/contacts/<int:contact_id>/primary", methods=["POST"])
@login_required
def set_primary_contact(lead_id, contact_id):
    try:
        current_app.container.lead_service.set_primary_contact(lead_id, g.user, contact_id)
        flash("Primary contact updated.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("leads.view_lead", lead_id=lead_id))


@leads_bp.route("/<int:lead_id>/communications", methods=["POST"])
@login_required
def add_communication(lead_id):
    try:
        current_app.container.lead_service.add_communication(
            lead_id, g.user,
            comm_date=request.form.get("comm_date", ""),
            mode=request.form.get("mode", ""),
            description=request.form.get("description", ""),
            follow_up_date=request.form.get("follow_up_date") or None,
        )
        flash("Communication logged.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("leads.view_lead", lead_id=lead_id))


@leads_bp.route("/<int:lead_id>/convert", methods=["POST"])
@admin_required
def convert_to_client(lead_id):
    try:
        client = current_app.container.client_service.convert_lead(
            lead_id, g.user, client_type=request.form.get("client_type", "Buyer")
        )
        flash(f"Lead approved and converted to client '{client.company_name}'.", "success")
        return redirect(url_for("clients.view_client", client_id=client.id))
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("leads.view_lead", lead_id=lead_id))
