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


_ONES = [
    "", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN",
    "SEVENTEEN", "EIGHTEEN", "NINETEEN",
]
_TENS = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY"]
_SCALES = [(1_000_000_000, "BILLION"), (1_000_000, "MILLION"), (1_000, "THOUSAND")]


def _three_digit_words(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100])
        parts.append("HUNDRED")
        n %= 100
    if n >= 20:
        tens_word = _TENS[n // 10]
        parts.append(f"{tens_word}-{_ONES[n % 10]}" if n % 10 else tens_word)
    elif n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


def number_to_words(n: int) -> str:
    """Spells out a non-negative whole number in English, e.g. 15640 ->
    'FIFTEEN THOUSAND SIX HUNDRED FORTY'. Used to print quotation totals
    in words alongside the numeric amount, as export documents expect."""
    if n == 0:
        return "ZERO"
    words = []
    remaining = n
    for value, name in _SCALES:
        if remaining >= value:
            count = remaining // value
            words.append(f"{_three_digit_words(count)} {name}")
            remaining %= value
    if remaining > 0:
        words.append(_three_digit_words(remaining))
    return " ".join(words)


def amount_in_words(amount, currency_label: str = "US DOLLARS") -> str:
    """e.g. 15640.50 -> 'US DOLLARS FIFTEEN THOUSAND SIX HUNDRED FORTY AND CENTS FIFTY ONLY'."""
    amount = round(float(amount or 0), 2)
    whole = int(amount)
    cents = int(round((amount - whole) * 100))
    words = f"{currency_label} {number_to_words(whole)}"
    if cents:
        words += f" AND CENTS {number_to_words(cents)}"
    return words + " ONLY"


_INR_SCALES = [(10_000_000, "CRORE"), (100_000, "LAKH"), (1_000, "THOUSAND")]


def number_to_words_indian(n: int) -> str:
    """Like number_to_words but with the Indian crore/lakh grouping - the
    style INR purchase orders spell their order value in."""
    if n == 0:
        return "ZERO"
    words = []
    remaining = n
    for value, name in _INR_SCALES:
        if remaining >= value:
            count = remaining // value
            words.append(f"{_three_digit_words(count)} {name}")
            remaining %= value
    if remaining > 0:
        words.append(_three_digit_words(remaining))
    return " ".join(words)


def inr_in_words(amount) -> str:
    """e.g. 383833 -> 'THREE LAKH EIGHTY-THREE THOUSAND EIGHT HUNDRED
    THIRTY-THREE INR ONLY' - used by the printed Purchase Order."""
    amount = round(float(amount or 0), 2)
    whole = int(amount)
    paise = int(round((amount - whole) * 100))
    words = number_to_words_indian(whole)
    if paise:
        words += f" AND PAISE {number_to_words_indian(paise)}"
    return words + " INR ONLY"


def register_template_helpers(app):
    """Small, presentation-only helpers exposed to every Jinja template."""

    @app.template_filter("amount_in_words")
    def amount_in_words_filter(value):
        return amount_in_words(value)

    @app.template_filter("inr_in_words")
    def inr_in_words_filter(value):
        return inr_in_words(value)

    @app.template_filter("long_date")
    def long_date(value):
        """'2025-01-23' -> '23 January 2025' (the date style the Packing
        Details sheet prints in its header)."""
        if not value:
            return "—"
        from datetime import datetime
        try:
            parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return str(value)
        return f"{parsed.day} {parsed.strftime('%B %Y')}"

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
