"""
app/routes/parties.py
----------------------
HTTP layer shared by Buyer and Exporter (the "clients" tab used to cover
both, plus Supplier, via one client_type field on one table - it's now
three separate tabs backed by three separate tables). Buyer and Exporter
are treated as having identical data/documentation structure for now, so
one blueprint FACTORY builds both blueprints instead of duplicating the
same routes twice - `build_party_blueprint("buyers", ...)` and
`build_party_blueprint("exporters", ...)` are called once each in
app/__init__.py. Supplier's shape has diverged (see app/routes/suppliers.py),
so it gets its own module instead of a third call here.
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort
)

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required


def build_party_blueprint(name: str, service_attr: str) -> Blueprint:
    """name: 'buyers' | 'exporters' - the blueprint name AND url_prefix.
    service_attr: 'buyer_service' | 'exporter_service' - the ServiceContainer
    attribute this blueprint's routes should call."""
    bp = Blueprint(name, __name__, url_prefix=f"/{name}")

    def _service():
        return getattr(current_app.container, service_attr)

    def _extract_contacts(form) -> list:
        """Same parallel-array shape as leads._extract_contacts_from_form -
        contact_name[]/contact_phone[]/contact_email[] plus a single index
        marked primary."""
        names = form.getlist("contact_name[]")
        phones = form.getlist("contact_phone[]")
        emails = form.getlist("contact_email[]")
        primary_index = form.get("primary_contact_index", "0")
        contacts = []
        for i, contact_name in enumerate(names):
            if not contact_name.strip():
                continue
            contacts.append({
                "name": contact_name.strip(),
                "phone": phones[i].strip() if i < len(phones) else "",
                "email": emails[i].strip() if i < len(emails) else "",
                "is_primary": str(i) == primary_index,
            })
        return contacts

    @bp.route("/")
    @login_required
    def list_parties():
        status = request.args.get("status") or None
        parties = _service().list_all(g.user.company_id, status=status)
        return render_template(
            "parties/list.html", parties=parties, status_filter=status,
            endpoint_prefix=name, type_label=_service().client_type,
        )

    @bp.route("/new", methods=["GET", "POST"])
    @admin_required
    def new_party():
        service = _service()
        if request.method == "POST":
            try:
                party = service.create(g.user, {
                    "company_name": request.form.get("company_name", ""),
                    "phone": request.form.get("phone", ""),
                    "email": request.form.get("email", ""),
                    "facebook": request.form.get("facebook", ""),
                    "instagram": request.form.get("instagram", ""),
                    "other_social": request.form.get("other_social", ""),
                    "address": request.form.get("address", ""),
                }, contacts=_extract_contacts(request.form))
                flash(f"{service.client_type} '{party.company_name}' added.", "success")
                return redirect(url_for(f"{name}.view_party", party_id=party.id))
            except (ValidationError, PermissionDeniedError) as e:
                flash(str(e), "error")
                return render_template(
                    "parties/new.html", form_data=request.form,
                    endpoint_prefix=name, type_label=service.client_type,
                ), 400

        return render_template(
            "parties/new.html", form_data=None, endpoint_prefix=name, type_label=service.client_type,
        )

    @bp.route("/<int:party_id>")
    @login_required
    def view_party(party_id):
        service = _service()
        try:
            party = service.get(party_id, g.user.company_id)
        except NotFoundError:
            abort(404)
        communications = current_app.container.communication_service.list_for(name[:-1], party_id)
        payments = current_app.container.payment_repo.list_for(name[:-1], party_id)
        total_received_inr = sum(p.amount_inr for p in payments)
        document_rows = service.document_feed(party)
        return render_template(
            "parties/detail.html", party=party, communications=communications,
            payments=payments, document_rows=document_rows, total_received_inr=total_received_inr,
            endpoint_prefix=name, type_label=service.client_type,
        )

    @bp.route("/<int:party_id>/edit", methods=["GET", "POST"])
    @admin_required
    def edit_party(party_id):
        service = _service()
        try:
            party = service.get(party_id, g.user.company_id)
        except NotFoundError:
            abort(404)

        if request.method == "POST":
            try:
                service.update_compulsory_fields(party_id, g.user, {
                    "company_name": request.form.get("company_name", ""),
                    "phone": request.form.get("phone", ""),
                    "email": request.form.get("email", ""),
                    "facebook": request.form.get("facebook", ""),
                    "instagram": request.form.get("instagram", ""),
                    "other_social": request.form.get("other_social", ""),
                    "address": request.form.get("address", ""),
                })
                flash(f"{service.client_type} details updated.", "success")
                return redirect(url_for(f"{name}.view_party", party_id=party_id))
            except (ValidationError, PermissionDeniedError) as e:
                flash(str(e), "error")

        return render_template(
            "parties/edit.html", party=party, endpoint_prefix=name, type_label=service.client_type,
        )

    @bp.route("/<int:party_id>/status", methods=["POST"])
    @login_required
    def update_status(party_id):
        try:
            _service().update_status(party_id, g.user, request.form.get("status", ""))
            flash(f"{_service().client_type} status updated.", "success")
        except ValidationError as e:
            flash(str(e), "error")
        except NotFoundError:
            abort(404)
        return redirect(url_for(f"{name}.view_party", party_id=party_id))

    @bp.route("/<int:party_id>/contacts", methods=["POST"])
    @login_required
    def add_contact(party_id):
        try:
            _service().add_contact(
                party_id, g.user,
                name=request.form.get("name", ""),
                phone=request.form.get("phone", ""),
                email=request.form.get("email", ""),
            )
            flash("Contact person added.", "success")
        except ValidationError as e:
            flash(str(e), "error")
        except NotFoundError:
            abort(404)
        return redirect(url_for(f"{name}.view_party", party_id=party_id))

    @bp.route("/<int:party_id>/contacts/<int:contact_id>/primary", methods=["POST"])
    @login_required
    def set_primary_contact(party_id, contact_id):
        try:
            _service().set_primary_contact(party_id, g.user, contact_id)
            flash("Primary contact updated.", "success")
        except ValidationError as e:
            flash(str(e), "error")
        except NotFoundError:
            abort(404)
        return redirect(url_for(f"{name}.view_party", party_id=party_id))

    @bp.route("/<int:party_id>/communications", methods=["POST"])
    @login_required
    def add_communication(party_id):
        try:
            _service().add_communication(
                party_id, g.user,
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
        return redirect(url_for(f"{name}.view_party", party_id=party_id))

    @bp.route("/<int:party_id>/payments", methods=["POST"])
    @login_required
    def add_payment(party_id):
        try:
            amount_raw = request.form.get("amount_original", "0")
            amount = float(amount_raw) if amount_raw else 0
            payment = _service().add_payment(
                party_id, g.user,
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
        return redirect(url_for(f"{name}.view_party", party_id=party_id))

    @bp.route("/<int:party_id>/documents", methods=["POST"])
    @login_required
    def add_document(party_id):
        try:
            _service().add_document(
                party_id, g.user,
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
        return redirect(url_for(f"{name}.view_party", party_id=party_id))

    return bp
