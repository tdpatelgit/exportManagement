"""
app/routes/profile.py
----------------------
Self-service account settings: any signed-in user (employee or admin) can
change their own username and password here.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g

from app.exceptions import ValidationError
from app.utils import login_required

profile_bp = Blueprint("profile", __name__, url_prefix="/account")


@profile_bp.route("")
@login_required
def settings():
    return render_template("account.html")


@profile_bp.route("/username", methods=["POST"])
@login_required
def change_username():
    try:
        current_app.container.auth_service.change_username(
            g.user, g.user.id, request.form.get("username", "")
        )
        flash("Username updated.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    return redirect(url_for("profile.settings"))


@profile_bp.route("/password", methods=["POST"])
@login_required
def change_password():
    new_password = request.form.get("new_password", "")
    if new_password != request.form.get("confirm_password", ""):
        flash("New password and confirmation do not match.", "error")
        return redirect(url_for("profile.settings"))
    try:
        current_app.container.auth_service.change_password(
            g.user, request.form.get("current_password", ""), new_password
        )
        flash("Password updated.", "success")
    except ValidationError as e:
        flash(str(e), "error")
    return redirect(url_for("profile.settings"))
