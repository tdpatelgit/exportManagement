"""
app/routes/auth.py
-------------------
Login / logout. This is the only place that touches Flask's `session`
object for authentication - everywhere else just reads `g.user`.
"""

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app, g

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = current_app.container.auth_service.authenticate(username, password)
        if user:
            session.clear()
            session["user_id"] = user.id
            flash(f"Welcome back, {user.full_name}.", "success")
            return redirect(url_for("dashboard.home"))
        flash("Incorrect username or password.", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
