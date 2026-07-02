"""
app/utils.py
------------
Cross-cutting helpers that don't belong to any single layer:
  - session-based auth decorators (`login_required`, `admin_required`)
  - a couple of Jinja template filters for date formatting

Kept separate from services.py because these are HTTP/session concerns,
not business rules (Single Responsibility again).
"""

from functools import wraps
from flask import session, redirect, url_for, flash, g, abort


def login_required(view_func):
    """Redirects to /login if nobody is signed in."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view_func(*args, **kwargs)
    return wrapped


def admin_required(view_func):
    """Redirects non-admins away from admin-only pages (e.g. Our Company
    settings, employee management, lead-to-client conversion)."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        if not g.user.is_admin:
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped


def register_template_helpers(app):
    """Small, presentation-only helpers exposed to every Jinja template."""

    @app.template_filter("friendly_date")
    def friendly_date(value):
        if not value:
            return "—"
        # Values come out of SQLite as 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'
        return str(value)[:16]

    @app.template_filter("status_css")
    def status_css(status_value):
        """Maps a status code to a CSS class suffix so badges get a
        consistent color without a big if/elif chain in every template."""
        mapping = {
            "new": "slate",
            "in_communication": "blue",
            "in_follow_up": "amber",
            "long_follow_up": "rust",
            "quotation_submission_pending": "green",
            "proforma_invoice_submission_pending": "amber",
            "purchase_order_submission_pending": "blue",
            "purchase_invoice_submission_pending": "violet",
            "export_invoice_submission_pending": "teal",
            "commercial_invoice_submission_pending": "green",
        }
        return mapping.get(status_value, "slate")
