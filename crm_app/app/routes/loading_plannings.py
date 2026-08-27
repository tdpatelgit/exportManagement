"""
app/routes/loading_plannings.py
--------------------------------
"Loading Planning" - the document that works out which goods physically go in
which container, before the export invoice is cut.

Goods are loaded the way the Export Invoice's own "Reference proforma
invoices" card loads them, but traced one hop differently (PI -> purchase
orders -> THOSE ORDERS' packing lists, so lines arrive at design level); then
a human builds cartons and pallets by hand, because `packing_list_items.
pallets` has always been a decimal and 9.91 pallets is not a thing anyone can
ship; then those pallets are assigned whole to a booking's containers.

Reads are open to anyone signed in, writes are admin-only - the same split
Booking Detail uses, and for the same reason: this is the document the
loading bay works from.

Everything the packing cards check is reported as a WARNING on the form
rather than raised: a plan is legitimately built across several sittings, so
a half-built one must save. See LoadingPlanningService.packing_warnings.
"""

import json
import dataclasses
from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort, jsonify

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required, verify_delete_password

loading_plannings_bp = Blueprint("loading_plannings", __name__, url_prefix="/loading-plannings")

_FIELDS = [
    "loading_planning_number", "loading_planning_date", "booking_detail_id",
    "booking_no", "vessel_name", "voyage_no", "transporter_name", "remarks",
]


def _extract_fields(form) -> dict:
    return {key: form.get(key, "") for key in _FIELDS}


def _extract_items(form) -> list:
    """One goods line per row of the Products card. Every column is posted as
    its own repeated field, the same idiom every other line-items form here
    uses."""
    keys = ("proforma_invoice_id", "purchase_order_id", "po_number", "product_id", "product_name",
            "design_id", "design_name", "hsn_code", "quantity_boxes", "quantity_unit",
            "quantity_value", "unit", "net_weight_kg", "price_usd")
    lists = {k: form.getlist(f"item_{k}[]") for k in keys}
    n = max((len(v) for v in lists.values()), default=0)
    return [{k: (lists[k][i] if i < len(lists[k]) else "") for k in keys} for i in range(n)]


def _extract_containers(form) -> list:
    keys = ("container_type", "container_no", "line_seal_no", "rfid_seal_no", "vehicle_no",
            "lr_no", "transporter_name", "max_permitted_weight", "tare_weight_kg")
    lists = {k: form.getlist(f"cd_{k}[]") for k in keys}
    n = max((len(v) for v in lists.values()), default=0)
    return [{k: (lists[k][i] if i < len(lists[k]) else "") for k in keys} for i in range(n)]


def _extract_packing(form) -> tuple:
    """Cartons and pallets come back as JSON rather than as parallel repeated
    fields.

    Both are trees - a carton holds several goods lines, a pallet holds
    cartons AND loose boxes - and flattening a tree into `foo[]` arrays would
    need a fragile index-matching convention that the drag-and-drop card
    would have to keep in sync on every move. The card owns one JS object and
    posts it whole; the service still cleans and revalidates every field of
    it, so nothing here is trusted."""
    def parse(name):
        raw = (form.get(name) or "").strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except ValueError:
            raise ValidationError("The packing could not be read - try rebuilding it.")
        return value if isinstance(value, list) else []

    return parse("cartons_json"), parse("pallets_json")


def _form_context(container, company_id):
    return {
        "buyers": container.buyer_repo.list_all(company_id),
        "proforma_invoices": container.proforma_invoice_service.list_all(company_id),
        "bookings": container.booking_detail_service.list_all(company_id),
    }


def _render_form(container, plan, warnings=None, status_code=200):
    """Re-render after a failed POST with exactly what was typed, so nothing
    the operator did is lost - the packing cards especially, which can
    represent an afternoon's work."""
    html = render_template(
        "loading_plannings/form.html", plan=plan, form_data=request.form,
        items_json=json.dumps(_extract_items(request.form)),
        form_containers=_extract_containers(request.form),
        cartons_json=request.form.get("cartons_json") or "[]",
        pallets_json=request.form.get("pallets_json") or "[]",
        selected_proforma_ids=[int(v) for v in request.form.getlist("proforma_invoice_ids[]") if v.isdigit()],
        warnings=warnings or [],
        suggested_number=(plan.loading_planning_number if plan else request.form.get("loading_planning_number")),
        today=request.form.get("loading_planning_date") or date.today().isoformat(),
        **_form_context(container, g.user.company_id),
    )
    return (html, status_code) if status_code != 200 else html


@loading_plannings_bp.route("/api/prefill")
@login_required
def loading_planning_prefill():
    """Goods for the ticked proforma invoices - the `load goods & details from
    selected PIs` button. Overwrites only the PI-derived goods lines, leaving
    the document's number/date, its booking and any packing already built
    untouched, the same rule the Export Invoice's own prefill follows."""
    raw = request.args.get("proforma_invoice_ids", "")
    ids = [p for p in raw.split(",") if p.strip()]
    return jsonify(
        current_app.container.loading_planning_service.build_prefill_from_proformas(ids, g.user.company_id)
    )


@loading_plannings_bp.route("/api/auto-build", methods=["POST"])
@login_required
def loading_planning_auto_build():
    """The `auto-build` button: whole units to capacity, then one flagged
    part-unit for the remainder, per goods line. Deliberately does NOT merge
    part-units - that is the judgement call this whole document exists to let
    a person make."""
    service = current_app.container.loading_planning_service
    payload = request.get_json(silent=True) or {}
    try:
        items = service._clean_items(payload.get("items") or [])
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.auto_build_packing(g.user.company_id, items))


@loading_plannings_bp.route("/")
@login_required
def list_loading_plannings():
    plans = current_app.container.loading_planning_service.list_all(g.user.company_id)
    return render_template("loading_plannings/list.html", plans=plans)


@loading_plannings_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_loading_planning():
    container = current_app.container
    service = container.loading_planning_service
    if request.method == "POST":
        cartons, pallets = [], []
        try:
            cartons, pallets = _extract_packing(request.form)
            plan = service.create(
                current_user=g.user, fields=_extract_fields(request.form),
                proforma_ids=request.form.getlist("proforma_invoice_ids[]"),
                items=_extract_items(request.form),
                containers=_extract_containers(request.form),
                cartons=cartons, pallets=pallets,
            )
            _flash_with_warnings(service, plan, "added")
            return redirect(url_for("loading_plannings.edit_loading_planning", loading_planning_id=plan.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return _render_form(container, None, status_code=400)

    today = date.today().isoformat()
    return render_template(
        "loading_plannings/form.html", plan=None, form_data=None, items_json="[]",
        form_containers=None, cartons_json="[]", pallets_json="[]",
        selected_proforma_ids=[], warnings=[],
        suggested_number=service.next_number(g.user.company_id, today), today=today,
        **_form_context(container, g.user.company_id),
    )


@loading_plannings_bp.route("/<int:loading_planning_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_loading_planning(loading_planning_id):
    container = current_app.container
    service = container.loading_planning_service
    try:
        plan = service.get(loading_planning_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            cartons, pallets = _extract_packing(request.form)
            updated = service.update(
                loading_planning_id=loading_planning_id, current_user=g.user,
                fields=_extract_fields(request.form),
                proforma_ids=request.form.getlist("proforma_invoice_ids[]"),
                items=_extract_items(request.form),
                containers=_extract_containers(request.form),
                cartons=cartons, pallets=pallets,
            )
            _flash_with_warnings(service, updated, "updated")
            return redirect(url_for("loading_plannings.edit_loading_planning",
                                    loading_planning_id=loading_planning_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return _render_form(container, plan, status_code=400)

    return render_template(
        "loading_plannings/form.html", plan=plan, form_data=None,
        items_json=json.dumps([dataclasses.asdict(i) for i in plan.items]),
        form_containers=None,
        cartons_json=json.dumps([service._carton_json(c) for c in plan.cartons]),
        pallets_json=json.dumps([service._pallet_json(p) for p in plan.pallets]),
        selected_proforma_ids=plan.proforma_invoice_ids,
        warnings=service.packing_warnings(plan),
        suggested_number=plan.loading_planning_number, today=plan.loading_planning_date,
        **_form_context(container, g.user.company_id),
    )


def _flash_with_warnings(service, plan, verb: str) -> None:
    """Saved is saved - the packing checks are reported alongside, never
    instead of. A plan with 160 boxes still to pack is a normal, useful
    intermediate state."""
    flash(f"Loading planning {plan.loading_planning_number} {verb}.", "success")
    for warning in service.packing_warnings(plan):
        flash(warning, "warning")


@loading_plannings_bp.route("/<int:loading_planning_id>")
@login_required
def view_loading_planning(loading_planning_id):
    try:
        plan = current_app.container.loading_planning_service.get(loading_planning_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    return render_template("loading_plannings/print.html", plan=plan)


@loading_plannings_bp.route("/<int:loading_planning_id>/delete", methods=["POST"])
@admin_required
def delete_loading_planning(loading_planning_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Loading planning not deleted.", "error")
        return redirect(url_for("loading_plannings.list_loading_plannings"))
    service = current_app.container.loading_planning_service
    try:
        plan = service.get(loading_planning_id, g.user.company_id)
        service.delete(loading_planning_id, g.user)
        flash(f"Loading planning {plan.loading_planning_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("loading_plannings.list_loading_plannings"))
