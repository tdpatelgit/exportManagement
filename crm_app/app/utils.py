"""
app/utils.py
------------
Cross-cutting helpers that don't belong to any single layer:
  - session-based auth decorators (`login_required`, `admin_required`)
  - a couple of Jinja template filters for date formatting

Kept separate from services.py because these are HTTP/session concerns,
not business rules (Single Responsibility again).
"""

import re
from functools import wraps
from flask import session, redirect, url_for, flash, g, abort
from werkzeug.security import check_password_hash


def verify_own_password(user, form, field: str) -> bool:
    """Confirms the password typed into `form[field]` is the signed-in user's
    own - the second gate on an action that is admin-only and can't simply be
    undone."""
    password = form.get(field, "")
    return bool(password) and check_password_hash(user.password_hash, password)


def verify_delete_password(user, form) -> bool:
    """Required before any document delete goes through, since deleting
    cascades to every sub-document made under it."""
    return verify_own_password(user, form, "delete_password")


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


# GST state codes - the first two digits of any GSTIN. Used by the gst_state
# template filter to print the STATE line on GST documents (the delivery
# challan for jobwork, for one) without storing a state against every party.
GST_STATE_NAMES = {
    "01": "JAMMU AND KASHMIR", "02": "HIMACHAL PRADESH", "03": "PUNJAB", "04": "CHANDIGARH",
    "05": "UTTARAKHAND", "06": "HARYANA", "07": "DELHI", "08": "RAJASTHAN", "09": "UTTAR PRADESH",
    "10": "BIHAR", "11": "SIKKIM", "12": "ARUNACHAL PRADESH", "13": "NAGALAND", "14": "MANIPUR",
    "15": "MIZORAM", "16": "TRIPURA", "17": "MEGHALAYA", "18": "ASSAM", "19": "WEST BENGAL",
    "20": "JHARKHAND", "21": "ODISHA", "22": "CHHATTISGARH", "23": "MADHYA PRADESH", "24": "GUJARAT",
    "26": "DADRA AND NAGAR HAVELI AND DAMAN AND DIU", "27": "MAHARASHTRA", "29": "KARNATAKA",
    "30": "GOA", "31": "LAKSHADWEEP", "32": "KERALA", "33": "TAMIL NADU", "34": "PUDUCHERRY",
    "35": "ANDAMAN AND NICOBAR ISLANDS", "36": "TELANGANA", "37": "ANDHRA PRADESH", "38": "LADAKH",
    "97": "OTHER TERRITORY", "99": "CENTRE JURISDICTION",
}


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


def inr_in_words(amount, currency_label: str = "INR") -> str:
    """e.g. 383833 -> 'THREE LAKH EIGHTY-THREE THOUSAND EIGHT HUNDRED
    THIRTY-THREE INR ONLY' - used by the printed Purchase Order."""
    amount = round(float(amount or 0), 2)
    whole = int(amount)
    paise = int(round((amount - whole) * 100))
    words = number_to_words_indian(whole)
    if paise:
        words += f" AND PAISE {number_to_words_indian(paise)}"
    return f"{words} {currency_label} ONLY"


def _starts_with_term(value, incoterm: str) -> bool:
    """Delivery-terms options are hand-maintained in Administration ->
    Miscellaneous, so they arrive as free text ('FOB', 'fob', 'FOB Mundra',
    'CFR - BEIRA') and are matched on the leading word rather than by
    equality."""
    text = str(value or "").strip().upper()
    return text == incoterm or text.startswith(incoterm + " ") or text.startswith(incoterm + "-")


def is_fob_terms(value) -> bool:
    """True when a document's nature of contract / terms of delivery is FOB.

    Under FOB the buyer carries the whole ocean leg, so neither sea freight nor
    insurance is part of the price."""
    return _starts_with_term(value, "FOB")


def is_cfr_terms(value) -> bool:
    """True when the terms are CFR (Cost and Freight).

    Under CFR the seller still pays the freight but the buyer insures the
    cargo - so CFR drops the insurance and ONLY the insurance."""
    return _starts_with_term(value, "CFR")


# Which charges a delivery term takes off the price. Both the services (which
# store the dropped charges as zero) and the printed sheets (which leave the
# row out) ask these questions rather than testing for an incoterm by name,
# so a term can never be handled one way on the form and another on the sheet.
# Only the freight and the insurance are ever dropped; the certification and
# other_charges are payable under every term.
def drops_sea_freight(value) -> bool:
    """FOB alone hands the freight to the buyer; CFR keeps paying it."""
    return is_fob_terms(value)


def drops_insurance(value) -> bool:
    """FOB and CFR both leave the cargo insurance to the buyer."""
    return is_fob_terms(value) or is_cfr_terms(value)


def drops_certification(value) -> bool:
    """Never dropped - unlike the freight and the insurance, the certification
    is a seller-side cost that stays payable whoever carries the ocean leg, so
    it adds onto the goods total under FOB exactly as it does under CFR/CIF
    (the same way other_charges has never been gated by a term either). Kept as
    a function so the services and the printed sheets keep asking about it in
    the same place as the other two charges."""
    return False


def register_template_helpers(app):
    """Small, presentation-only helpers exposed to every Jinja template."""

    @app.template_filter("is_fob")
    def is_fob_filter(value):
        """`{% if invoice.nature_of_contract | is_fob %}` - true for FOB terms."""
        return is_fob_terms(value)

    @app.template_filter("is_cfr")
    def is_cfr_filter(value):
        """`{% if quotation.shipping_terms | is_cfr %}` - true for CFR terms."""
        return is_cfr_terms(value)

    @app.template_filter("drops_sea_freight")
    def drops_sea_freight_filter(value):
        """`{% if not terms | drops_sea_freight %}` - hides the SEA FREIGHT row
        (FOB only)."""
        return drops_sea_freight(value)

    @app.template_filter("drops_insurance")
    def drops_insurance_filter(value):
        """`{% if not terms | drops_insurance %}` - hides the INSURANCE row
        (FOB and CFR). Asked separately from the freight so a term can drop one
        row without disturbing the other or the rows around them."""
        return drops_insurance(value)

    @app.template_filter("drops_certification")
    def drops_certification_filter(value):
        """`{% if not terms | drops_certification %}` - always false, so the
        CERTIFICATION row prints under every delivery term. Kept alongside the
        other two so a sheet asks the same question about all three charges."""
        return drops_certification(value)

    @app.template_filter("amount_in_words")
    def amount_in_words_filter(value, currency=None):
        """`{{ total | amount_in_words }}` keeps the historic US DOLLARS
        wording; pass a document's currency name to spell that instead."""
        return amount_in_words(value, currency.upper() if currency else "US DOLLARS")

    @app.template_filter("inr_in_words")
    def inr_in_words_filter(value, currency=None):
        """Indian crore/lakh grouping. The trailing unit follows the
        document's own currency when one is passed."""
        return inr_in_words(value, currency.upper() if currency else "INR")

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

    @app.template_filter("dmy_date")
    def dmy_date(value):
        """'2026-08-29' -> '29-08-2026' (the date style the Packing Planning
        sheet's MANF. DATE column prints in, because that sheet is read
        side by side with the spreadsheet it replaces)."""
        if not value:
            return "—"
        from datetime import datetime
        try:
            parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return str(value)
        return parsed.strftime("%d-%m-%Y")

    @app.template_filter("inr_group")
    def inr_group(value):
        """2480000 -> '24,80,000.00' - the Indian lakh/crore digit grouping
        (last three digits, then pairs) that a rupee figure is printed in on
        a GST document. Python's own ',' format only does the western
        thousands grouping, so this regroups it by hand."""
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            return value
        sign = "-" if amount < 0 else ""
        whole, _, frac = f"{abs(amount):.2f}".partition(".")
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            # Pairs from the right, e.g. '24' + '80' -> '24,80'
            head = re.sub(r"(?<=\d)(?=(?:\d\d)+$)", ",", head)
            whole = f"{head},{tail}"
        return f"{sign}{whole}.{frac}"

    @app.template_filter("gst_state")
    def gst_state(gstin):
        """'24AABFO8212B1ZV' -> 'GUJARAT (24)'. A GSTIN's first two digits
        are its state code, which is how the STATE line on a GST document is
        printed - so it's derived rather than stored separately anywhere.
        An unrecognised or malformed GSTIN falls back to just the code, and
        a blank one to an em dash."""
        code = str(gstin or "")[:2]
        if not code.isdigit():
            return "—"
        name = GST_STATE_NAMES.get(code)
        return f"{name} ({code})" if name else code

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
