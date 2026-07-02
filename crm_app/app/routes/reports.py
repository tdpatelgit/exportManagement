"""
app/routes/reports.py
----------------------
A first, working slice of the "monthly/quarterly/yearly reports" future
plan. Lets an admin pick any date range (a month, a quarter, a year, or a
custom range) and see leads generated / communications logged / clients
converted per employee, plus total payments received - all from data the
app already stores.
"""

from datetime import date

from flask import Blueprint, render_template, request, current_app

from app.utils import admin_required

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def _default_month_range():
    today = date.today()
    start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


@reports_bp.route("/")
@admin_required
def index():
    default_start, default_end = _default_month_range()
    start_date = request.args.get("start_date") or default_start
    end_date = request.args.get("end_date") or default_end

    container = current_app.container
    rows = container.report_service.activity_report(start_date, end_date)
    payments_summary = container.report_service.payments_received_total(start_date, end_date)

    return render_template(
        "reports/index.html",
        rows=rows, start_date=start_date, end_date=end_date,
        payments_summary=payments_summary,
    )
