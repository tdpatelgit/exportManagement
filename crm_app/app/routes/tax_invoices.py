"""
app/routes/tax_invoices.py
---------------------------
The TAX INVOICE that accompanies an Export Invoice - the same consignment
restated in INR for the GST/e-way-bill paperwork.

Like the Export Packing List and Annexure-C this is a pure read-only
attachment with no identity of its own: it carries its parent export
invoice's OWN number and date (a tax invoice is not separately numbered
here), and every other field is either that invoice's or is derived from it
by converting the money at the invoice's own exchange rate. So there is
nothing to create, edit or delete - just a list (of export invoices, since
there is no tax-invoice table to list) and a single detail view. The e-way
bill no. and date it prints are set from the Export Invoices list's
"Update Eway bill no" popup (export_invoices.update_eway_bill).

Two cells on the sheet are lookups rather than plain invoice fields, and
both are assembled here so the template stays presentation-only:
GSTIN/Transporter ID (the chosen transporter's registration number) and
Loading Port PIN Code (the port list keeps the port and its PIN together).
"""

from flask import Blueprint, render_template, current_app, g, abort

from app.exceptions import NotFoundError
from app.utils import login_required

tax_invoices_bp = Blueprint("tax_invoices", __name__, url_prefix="/tax-invoices")


def _distinct(values):
    """The distinct non-blank values, in the order the 11B rows list them."""
    seen = []
    for value in values:
        value = (value or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _first(values):
    """The first non-blank value only - the sheet's Vehicle No and LR No cells
    each name a single one, so a multi-container invoice shows the first 11B
    row's rather than stacking every vehicle into the cell."""
    return next(iter(_distinct(values)), None)


def _consignment(invoice, transporters, ports):
    """The road-leg cells the sheet prints - transporter name(s), their
    registration number(s), the vehicle no and the LR no - gathered off the
    invoice's own section-11B container rows, plus the loading port's PIN.
    Vehicle and LR are single-valued (the first row that carries one).

    The 11B row stores only the transporter's NAME (a snapshot, so renaming
    one can't rewrite a saved invoice), so the registration number is looked
    up by that name here; a transporter since renamed or deleted simply has
    no number to show.
    """
    rows = invoice.container_details or []
    names = _distinct(r.get("transporter_name") for r in rows)
    gstin_by_name = {t.name: t.gstin_transporter_no for t in transporters}
    pin_by_port = {p.name: p.pin_code for p in ports}
    return {
        "transporter_names": names,
        "transporter_gstins": _distinct(gstin_by_name.get(n) for n in names),
        "vehicle_no": _first(r.get("vehicle_no") for r in rows),
        "lr_no": _first(r.get("lr_no") for r in rows),
        "loading_port_pin": pin_by_port.get(invoice.port_of_loading),
    }


@tax_invoices_bp.route("/")
@login_required
def list_tax_invoices():
    invoices = current_app.container.export_invoice_service.list_all(g.user.company_id)
    return render_template("tax_invoices/list.html", invoices=invoices)


@tax_invoices_bp.route("/<int:export_invoice_id>")
@login_required
def view_tax_invoice(export_invoice_id):
    container = current_app.container
    try:
        invoice = container.export_invoice_service.get(export_invoice_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    consignment = _consignment(
        invoice,
        container.transporter_service.list_all(g.user.company_id),
        container.misc_list_service.list_ports_of_loading(g.user.company_id),
    )
    return render_template(
        "tax_invoices/print.html", invoice=invoice, company=company, consignment=consignment,
    )
