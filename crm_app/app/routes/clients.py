"""
app/routes/clients.py
----------------------
HTTP layer for clients (leads that an admin has approved/converted).
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort
)

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required

clients_bp = Blueprint("clients", __name__, url_prefix="/clients")


@clients_bp.route("/")
@login_required
def list_clients():
    client_type = request.args.get("client_type") or None
    status = request.args.get("status") or None
    clients = current_app.container.client_service.list_all(g.user.company_id, client_type=client_type, status=status)
    return render_template("clients/list.html", clients=clients,
                            client_type_filter=client_type, status_filter=status)


@clients_bp.route("/<int:client_id>")
@login_required
def view_client(client_id):
    container = current_app.container
    try:
        client = container.client_service.get(client_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    communications = container.communication_service.list_for("client", client_id)
    payments = container.payment_repo.list_for_client(client_id)
    total_received_inr = sum(p.amount_inr for p in payments)
    # Documents card shows manually recorded entries plus every auto-generated
    # document (currently just quotations) made against this client's
    # originating lead - see ClientService.document_feed.
    document_rows = container.client_service.document_feed(client)
    return render_template(
        "clients/detail.html", client=client, communications=communications,
        payments=payments, document_rows=document_rows, total_received_inr=total_received_inr,
    )


@clients_bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_client(client_id):
    container = current_app.container
    try:
        client = container.client_service.get(client_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.client_service.update_compulsory_fields(client_id, g.user, {
                "company_name": request.form.get("company_name", ""),
                "phone": request.form.get("phone", ""),
                "email": request.form.get("email", ""),
                "facebook": request.form.get("facebook", ""),
                "instagram": request.form.get("instagram", ""),
                "other_social": request.form.get("other_social", ""),
                "address": request.form.get("address", ""),
                "client_type": request.form.get("client_type", "Buyer"),
            })
            flash("Client details updated.", "success")
            return redirect(url_for("clients.view_client", client_id=client_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")

    return render_template("clients/edit.html", client=client)


@clients_bp.route("/<int:client_id>/status", methods=["POST"])
@login_required
def update_status(client_id):
    try:
        current_app.container.client_service.update_status(
            client_id, g.user, request.form.get("status", "")
        )
        flash("Client status updated.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("clients.view_client", client_id=client_id))


@clients_bp.route("/<int:client_id>/contacts", methods=["POST"])
@login_required
def add_contact(client_id):
    try:
        current_app.container.client_service.add_contact(
            client_id, g.user,
            name=request.form.get("name", ""),
            phone=request.form.get("phone", ""),
            email=request.form.get("email", ""),
        )
        flash("Contact person added.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("clients.view_client", client_id=client_id))


@clients_bp.route("/<int:client_id>/contacts/<int:contact_id>/primary", methods=["POST"])
@login_required
def set_primary_contact(client_id, contact_id):
    try:
        current_app.container.client_service.set_primary_contact(client_id, g.user, contact_id)
        flash("Primary contact updated.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("clients.view_client", client_id=client_id))


@clients_bp.route("/<int:client_id>/communications", methods=["POST"])
@login_required
def add_communication(client_id):
    try:
        current_app.container.client_service.add_communication(
            client_id, g.user,
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
    return redirect(url_for("clients.view_client", client_id=client_id))


@clients_bp.route("/<int:client_id>/payments", methods=["POST"])
@login_required
def add_payment(client_id):
    try:
        amount_raw = request.form.get("amount_original", "0")
        amount = float(amount_raw) if amount_raw else 0
        payment = current_app.container.client_service.add_payment(
            client_id, g.user,
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
    return redirect(url_for("clients.view_client", client_id=client_id))


@clients_bp.route("/<int:client_id>/documents", methods=["POST"])
@login_required
def add_document(client_id):
    try:
        current_app.container.client_service.add_document(
            client_id, g.user,
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
    return redirect(url_for("clients.view_client", client_id=client_id))
