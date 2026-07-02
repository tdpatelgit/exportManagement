"""
app/routes/admin.py
--------------------
Admin-only area: see every employee, how many leads they generated and how
many communications they logged (the exact requirement from the brief), and
create new employee/admin logins.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError
from app.utils import admin_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/employees")
@admin_required
def list_employees():
    performance = current_app.container.stats_service.employee_performance()
    admins = current_app.container.user_repo.list_all(role="admin")
    return render_template("employees/list.html", performance=performance, admins=admins)


@admin_bp.route("/employees/<int:user_id>")
@admin_required
def employee_detail(user_id):
    container = current_app.container
    employee = container.user_repo.get_by_id(user_id)
    if not employee:
        abort(404)
    leads = container.lead_repo.list_all(employee_id=user_id)
    comm_counts = container.comm_repo.count_by_employee()
    return render_template(
        "employees/detail.html", employee=employee, leads=leads,
        communication_count=comm_counts.get(user_id, 0),
    )


@admin_bp.route("/employees/new", methods=["GET", "POST"])
@admin_required
def new_employee():
    if request.method == "POST":
        try:
            user = current_app.container.auth_service.create_user(
                username=request.form.get("username", "").strip(),
                password=request.form.get("password", ""),
                full_name=request.form.get("full_name", "").strip(),
                role=request.form.get("role", "employee"),
            )
            flash(f"User '{user.username}' ({user.role}) created.", "success")
            return redirect(url_for("admin.list_employees"))
        except ValidationError as e:
            flash(str(e), "error")

    return render_template("employees/form.html")


@admin_bp.route("/employees/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_active(user_id):
    container = current_app.container
    employee = container.user_repo.get_by_id(user_id)
    if not employee:
        abort(404)
    if employee.id == g.user.id:
        flash("You can't deactivate your own account.", "error")
        return redirect(url_for("admin.list_employees"))
    container.user_repo.set_active(user_id, not employee.is_active)
    flash(f"{employee.full_name} is now {'inactive' if employee.is_active else 'active'}.", "success")
    return redirect(url_for("admin.list_employees"))
