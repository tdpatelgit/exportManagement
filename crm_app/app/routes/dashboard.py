"""
app/routes/dashboard.py
------------------------
The landing page after login. Admins and employees see different views of
the same data, built entirely from services - no SQL here.
"""

from flask import Blueprint, render_template, current_app, g
from config import Config
from app.utils import login_required

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def home():
    container = current_app.container

    # Confirmed proforma invoices whose designs aren't all on a purchase
    # order yet - the whole company's for an admin, your own otherwise.
    po_reminders = container.proforma_fulfilment_service.pending_purchase_order_reminders(
        g.user.company_id, created_by=None if g.user.is_admin else g.user.id
    )

    if g.user.is_admin:
        performance = container.stats_service.employee_performance(g.user.company_id)
        overview = container.stats_service.overview_counts(g.user.company_id)
        recent_leads = container.lead_service.list_for_dashboard(g.user)[:8]
        recent_buyers = container.buyer_service.list_all(g.user.company_id)[:8]
        return render_template(
            "dashboard_admin.html",
            performance=performance,
            overview=overview,
            recent_leads=recent_leads,
            recent_buyers=recent_buyers,
            po_reminders=po_reminders,
        )

    # Employee view: their own leads + upcoming/overdue follow-ups.
    my_leads = container.lead_service.list_for_dashboard(g.user)
    my_lead_count = len(my_leads)
    my_comm_count = container.stats_service.employee_performance(g.user.company_id)
    my_comm_count = next(
        (row["communication_count"] for row in my_comm_count if row["employee"].id == g.user.id), 0
    )
    followups = container.communication_service.upcoming_followups(
        g.user.company_id, g.user.id, Config.FOLLOWUP_LOOKAHEAD_DAYS
    )
    return render_template(
        "dashboard_employee.html",
        my_leads=my_leads[:8],
        my_lead_count=my_lead_count,
        my_comm_count=my_comm_count,
        followups=followups,
        po_reminders=po_reminders,
    )
