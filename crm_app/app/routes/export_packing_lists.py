"""
app/routes/export_packing_lists.py
----------------------------------
The EXPORT PACKING LIST: the customs-facing sheet that accompanies an Export
Invoice, listing how that invoice's goods were split across the physical
containers.

This blueprint is deliberately READ-ONLY. There is no new/edit/delete route
because an export packing list is never authored on its own: exactly one is
generated for every export invoice the moment that invoice is saved, and the
container split it holds is typed into the export invoice's own form (see the
"Export packing list" card in templates/export_invoices/form.html). Editing
therefore means editing the parent invoice, and deleting means deleting it -
both of which the export_invoices blueprint already owns.
"""

from flask import Blueprint, render_template, url_for, current_app, g, abort, redirect

from app.exceptions import NotFoundError
from app.routes.export_invoices import _packing_labels
from app.utils import login_required

export_packing_lists_bp = Blueprint("export_packing_lists", __name__, url_prefix="/export-packing-lists")


@export_packing_lists_bp.route("/")
@login_required
def list_export_packing_lists():
    packing_lists = current_app.container.export_packing_list_service.list_all(g.user.company_id)
    return render_template("export_packing_lists/list.html", packing_lists=packing_lists)


@export_packing_lists_bp.route("/<int:export_packing_list_id>")
@login_required
def view_export_packing_list(export_packing_list_id):
    container = current_app.container
    try:
        packing_list = container.export_packing_list_service.get(export_packing_list_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    if not packing_list.invoice:
        abort(404)
    company = container.company_service.get(g.user.company_id)
    lut_no = None
    if company and company.lut_details:
        primary_lut = next((l for l in company.lut_details if l.get("is_primary")), None)
        lut_no = (primary_lut or company.lut_details[0]).get("lut_number")
    return render_template(
        "export_packing_lists/print.html",
        packing_list=packing_list, invoice=packing_list.invoice, company=company, lut_no=lut_no,
        # Same per-goods-line units the export invoice prints (PLTS / CTNS /
        # PCS, from Packing Planning), keyed by the invoice line's sr_no -
        # which is what every packing-list row's invoice_item_sr_no points at.
        packing_labels=_packing_labels(packing_list.invoice),
    )


@export_packing_lists_bp.route("/for-invoice/<int:export_invoice_id>")
@login_required
def view_for_invoice(export_invoice_id):
    """Stable entry point from an export invoice, which knows its own id but
    not its packing list's. A list is generated on every save, so a miss here
    only happens for an invoice saved before this feature existed - send
    those to the edit form, where saving once generates it."""
    container = current_app.container
    packing_list = container.export_packing_list_service.get_for_invoice(export_invoice_id, g.user.company_id)
    if not packing_list:
        return redirect(url_for("export_invoices.edit_export_invoice", export_invoice_id=export_invoice_id))
    return redirect(url_for(
        "export_packing_lists.view_export_packing_list", export_packing_list_id=packing_list.id
    ))
