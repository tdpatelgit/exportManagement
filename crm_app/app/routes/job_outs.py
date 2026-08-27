"""
app/routes/job_outs.py
-----------------------
Job Out generation: the "DELIVERY CHALLAN FOR JOBWORK" sheet that physically
travels with goods going out to a job manufacturer. Mirrors app/routes/
job_works.py layer for layer, but is by far the thinnest document form in
the app - and deliberately so.

A job out is always raised against ONE purchase invoice, normally straight
from that invoice's own preview toolbar via `?purchase_invoice_id=`. Only
the figures that are actually typed at dispatch time are on the form:

  * DELIVERY CHALLAN NO / DATE - the challan's own identifier, typed rather
    than auto-generated (unlike every other document number here), because a
    challan number is the supplier-facing reference the goods travel under;
  * the transport block - TRANSPORT NAME, TRANSPORT GSTIN, LR NO, VEHICLE NO;
  * EWAYBILL NO & DATE;
  * "Dispatched from our own company/warehouse" - the one switch on the
    form. Left unticked (the default) the Dispatch From block prints the
    purchase invoice's own SELLER; ticked, it prints our own company
    instead. The letterhead stays ours either way.

Everything else the sheet prints - the receiver party, the goods lines
(master product dispatched -> jobbed product expected back), HSN/qty/rate/
taxable value, the packing-list design breakdown and the tax footer - is
read LIVE off the purchase invoice when the sheet renders, never typed and
never snapshotted here. See JobOutService.build_sheet.
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required, verify_delete_password

job_outs_bp = Blueprint("job_outs", __name__, url_prefix="/job-outs")

_HEADER_FIELDS = [
    "purchase_invoice_id", "delivery_challan_no", "delivery_challan_date",
    "dispatch_from_company", "transporter_name", "transport_gstin", "lr_no", "vehicle_no",
    "eway_bill_no", "eway_bill_date", "remarks",
]


def _extract_header(form) -> dict:
    return {key: form.get(key, "") for key in _HEADER_FIELDS}


def _render_form(job_out, form_data, status=200):
    """(purchase invoices, transporters) for the form's source picker and its
    TRANSPORT GSTIN convenience list - the transporter directory is offered
    as a datalist, but the field itself stays free text (a purchase invoice
    already snapshots its transporter as a plain name rather than an FK, and
    the transport on a challan is whoever actually showed up)."""
    container = current_app.container
    purchase_invoices = container.purchase_invoice_service.list_all(g.user.company_id)
    transporters = container.transporter_service.list_all(g.user.company_id)
    return render_template(
        "job_outs/form.html", job_out=job_out, purchase_invoices=purchase_invoices,
        transporters=transporters, form_data=form_data, today=date.today().isoformat(),
    ), status


@job_outs_bp.route("/")
@login_required
def list_job_outs():
    job_outs = current_app.container.job_out_service.list_all(g.user.company_id)
    return render_template("job_outs/list.html", job_outs=job_outs)


@job_outs_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_job_out():
    container = current_app.container
    if request.method == "POST":
        try:
            job_out = container.job_out_service.create(
                current_user=g.user, fields=_extract_header(request.form),
            )
            flash(f"Job out {job_out.delivery_challan_no} created.", "success")
            return redirect(url_for("job_outs.view_job_out", job_out_id=job_out.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return _render_form(None, request.form, status=400)

    prefill = None
    purchase_invoice_id = request.args.get("purchase_invoice_id")
    if purchase_invoice_id:
        try:
            purchase_invoice = container.purchase_invoice_service.get(
                int(purchase_invoice_id), g.user.company_id
            )
            prefill = container.job_out_service.build_prefill_from_purchase_invoice(purchase_invoice)
        except (NotFoundError, ValueError):
            pass
    return _render_form(None, prefill)[0]


@job_outs_bp.route("/<int:job_out_id>")
@login_required
def view_job_out(job_out_id):
    """The job out's detail page IS its printed sheet - same as a job work,
    there's no separate view.html. Everything below the typed header is
    assembled fresh off the purchase invoice by build_sheet."""
    container = current_app.container
    try:
        job_out = container.job_out_service.get(job_out_id, g.user.company_id)
        sheet = container.job_out_service.build_sheet(job_out, g.user.company_id)
    except NotFoundError:
        abort(404)
    except ValidationError:
        # The purchase invoice behind this challan has gone missing - the
        # cascade should prevent it, so this is a broken row rather than a
        # normal state.
        abort(404)
    job_ins = container.job_in_service.list_for_job_out(job_out_id, g.user.company_id)
    return render_template("job_outs/print.html", job_out=job_out, job_ins=job_ins, **sheet)


@job_outs_bp.route("/<int:job_out_id>/edit", methods=["GET", "POST"])
@login_required
def edit_job_out(job_out_id):
    container = current_app.container
    try:
        job_out = container.job_out_service.get(job_out_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            container.job_out_service.update(
                current_user=g.user, job_out_id=job_out_id, fields=_extract_header(request.form),
            )
            flash(f"Job out {job_out.delivery_challan_no} updated.", "success")
            return redirect(url_for("job_outs.view_job_out", job_out_id=job_out_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return _render_form(job_out, request.form, status=400)

    return _render_form(job_out, None)[0]


@job_outs_bp.route("/<int:job_out_id>/delete", methods=["POST"])
@login_required
def delete_job_out(job_out_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Job out not deleted.", "error")
        return redirect(url_for("job_outs.view_job_out", job_out_id=job_out_id))
    try:
        job_out = current_app.container.job_out_service.get(job_out_id, g.user.company_id)
        current_app.container.job_out_service.delete(g.user, job_out_id)
        flash(f"Job out {job_out.delivery_challan_no} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
        return redirect(url_for("job_outs.view_job_out", job_out_id=job_out_id))
    except NotFoundError:
        abort(404)
    return redirect(url_for("job_outs.list_job_outs"))


@job_outs_bp.route("/<int:job_out_id>/versions")
@admin_required
def job_out_versions(job_out_id):
    container = current_app.container
    try:
        job_out = container.job_out_service.get(job_out_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    versions = container.document_version_service.list_for_document("job_out", job_out_id)
    rows = [
        {
            "version_number": v.version_number,
            "created_at": v.created_at,
            "changed_by_name": v.changed_by_name,
            "url": url_for("job_outs.view_job_out", job_out_id=job_out_id) if i == 0 else
                   url_for("job_outs.view_job_out_version",
                           job_out_id=job_out_id, version_number=v.version_number),
        }
        for i, v in enumerate(versions)
    ]
    return render_template(
        "document_versions/list.html", document_number=job_out.delivery_challan_no, versions=rows,
        back_url=url_for("job_outs.view_job_out", job_out_id=job_out_id),
    )


@job_outs_bp.route("/<int:job_out_id>/versions/<int:version_number>")
@admin_required
def view_job_out_version(job_out_id, version_number):
    """A historical job out still renders its body off the purchase invoice
    as it stands NOW - only the typed header (challan number/date, transport,
    e-way bill) is what the snapshot actually preserved, which is the whole
    of what this document owns."""
    container = current_app.container
    try:
        container.job_out_service.get(job_out_id, g.user.company_id)  # tenant-scope check
        historical_job_out, version = container.document_version_service.get_version(
            "job_out", job_out_id, version_number
        )
        sheet = container.job_out_service.build_sheet(historical_job_out, g.user.company_id)
    except (NotFoundError, ValidationError):
        abort(404)
    return render_template(
        "job_outs/print.html", job_out=historical_job_out, historical_version=version,
        job_ins=[], **sheet
    )
