"""
app/routes/platform_logins.py
------------------------------
HTTP layer for Platform Logins - the saved-credentials directory for
third-party platforms (Facebook, IndiaMART, a marketplace portal, ...).
Same shape as app/routes/transporters.py: a plain admin-gated CRUD, reads
open to anyone signed in.
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort
)

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required, verify_delete_password

platform_logins_bp = Blueprint("platform_logins", __name__, url_prefix="/platform-logins")

_FIELDS = ["platform_name", "login_url", "username", "password", "email", "mobile_number", "notes"]


def _extract_fields(form) -> dict:
    return {key: form.get(key, "") for key in _FIELDS}


@platform_logins_bp.route("/")
@login_required
def list_platform_logins():
    platform_logins = current_app.container.platform_login_service.list_all(g.user.company_id)
    return render_template("platform_logins/list.html", platform_logins=platform_logins)


@platform_logins_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_platform_login():
    container = current_app.container
    if request.method == "POST":
        try:
            platform_login = container.platform_login_service.create(g.user, _extract_fields(request.form))
            flash(f"Platform login '{platform_login.platform_name}' added.", "success")
            return redirect(url_for("platform_logins.view_platform_login", platform_login_id=platform_login.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template(
                "platform_logins/form.html", platform_login=None, form_data=request.form,
            ), 400

    return render_template("platform_logins/form.html", platform_login=None, form_data=None)


@platform_logins_bp.route("/<int:platform_login_id>")
@login_required
def view_platform_login(platform_login_id):
    try:
        platform_login = current_app.container.platform_login_service.get(platform_login_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    return render_template("platform_logins/detail.html", platform_login=platform_login)


@platform_logins_bp.route("/<int:platform_login_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_platform_login(platform_login_id):
    container = current_app.container
    try:
        platform_login = container.platform_login_service.get(platform_login_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.platform_login_service.update(platform_login_id, g.user, _extract_fields(request.form))
            flash("Platform login updated.", "success")
            return redirect(url_for("platform_logins.view_platform_login", platform_login_id=platform_login_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return render_template(
                "platform_logins/form.html", platform_login=platform_login, form_data=request.form,
            ), 400

    return render_template("platform_logins/form.html", platform_login=platform_login, form_data=None)


@platform_logins_bp.route("/<int:platform_login_id>/delete", methods=["POST"])
@admin_required
def delete_platform_login(platform_login_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Platform login not deleted.", "error")
        return redirect(url_for("platform_logins.view_platform_login", platform_login_id=platform_login_id))
    try:
        platform_login = current_app.container.platform_login_service.delete(platform_login_id, g.user)
        flash(f"Platform login '{platform_login.platform_name}' deleted.", "success")
    except PermissionDeniedError as e:
        flash(str(e), "error")
        return redirect(url_for("platform_logins.view_platform_login", platform_login_id=platform_login_id))
    except NotFoundError:
        abort(404)
    return redirect(url_for("platform_logins.list_platform_logins"))
