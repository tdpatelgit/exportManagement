"""
app/routes/packing_plannings.py
--------------------------------
"Packing Planning" - how what the supplier has actually PRODUCED breaks into
whole numbered pallets and cartons, and what is left over.

The step before Loading Planning. A loading plan answers which goods go in
which container; this answers what there is to load in the first place. A
purchase order's Production Status card knows the batches, and
product_pallet_types knows a pallet takes 32 boxes, but until now nothing put
the two together and said "317 ready is nine full pallets and 29 boxes
somebody has to pack by hand".

Loading is two explicit steps: tick proforma invoices and list the purchase
orders they pulled in (`/api/purchase-orders`), then tick which of THOSE to
actually draw from (`/api/prefill`, now keyed on purchase_order_ids rather
than proforma_invoice_ids). The checkpoint exists because not every PO under
a selected PI belongs in this packing run - a supplier not ready yet, a PO
already packed in an earlier plan.

Lines come in per BATCH, traced purchase order -> its production batches,
because a batch number and a manufacturing date exist nowhere else in the app
- and a pallet is packed out of one firing, not out of a design's yearly
total.

The sheet's second table is not a second stored list. PACKING REMAIN BY MANUAL
is derived from the batch rows (see PackingPlanning.remain_rows); what IS
stored is the manual units the operator groups those leftovers into, which
carry on the same packing-number sequence.

Reads are open to anyone signed in, writes are admin-only - the same split
Loading Planning and Booking Detail use, and for the same reason: this is the
document the packing floor works from.

Everything that doesn't add up is reported as a WARNING on the form rather
than raised: batches are keyed in as the supplier reports them and the
leftovers get grouped days later, so a half-planned document must save. See
PackingPlanningService.packing_warnings.
"""

import json
import dataclasses
from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, abort, jsonify

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.utils import login_required, admin_required, verify_delete_password

packing_plannings_bp = Blueprint("packing_plannings", __name__, url_prefix="/packing-plannings")

_FIELDS = ["packing_planning_number", "packing_planning_date", "remarks"]


def _extract_fields(form) -> dict:
    return {key: form.get(key, "") for key in _FIELDS}


def _extract_items(form) -> list:
    """One line per produced batch. Every column is posted as its own repeated
    field, the same idiom every other line-items form here uses."""
    keys = ("proforma_invoice_id", "purchase_order_id", "po_number", "purchase_order_item_id",
            "product_id", "product_name", "design_id", "design_name", "batch_number",
            "production_date", "ready_quantity", "quantity_unit", "packing_type_id",
            "packing_type_name", "packing_unit_label", "boxes_per_unit", "actual_packing",
            "packing_no_start")
    lists = {k: form.getlist(f"item_{k}[]") for k in keys}
    n = max((len(v) for v in lists.values()), default=0)
    return [{k: (lists[k][i] if i < len(lists[k]) else "") for k in keys} for i in range(n)]


def _extract_manual_units(form) -> list:
    """The hand-packed units come back as JSON rather than as parallel
    repeated fields.

    Each is a tree - one mixed pallet holds leftovers from several batches -
    and flattening a tree into `foo[]` arrays would need a fragile
    index-matching convention the card would have to keep in sync on every
    regroup. The card owns one JS object and posts it whole; the service
    still cleans and revalidates every field of it, so nothing here is
    trusted. Same call loading_plannings._extract_packing makes."""
    raw = (form.get("manual_units_json") or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValidationError("The manual packing could not be read - try rebuilding it.")
    return value if isinstance(value, list) else []


def _form_context(container, company_id):
    return {
        "buyers": container.buyer_repo.list_all(company_id),
        "proforma_invoices": container.proforma_invoice_service.list_all(company_id),
    }


def _packing_types_json(container, company_id, items) -> str:
    """What each row's packing-type dropdown offers. Built off whatever lines
    are on the form right now, so a re-render after a failed POST still has
    its pickers."""
    service = container.packing_planning_service
    return json.dumps(service.packing_types_for_items(company_id, items))


def _purchase_orders_json(container, company_id, plan) -> str:
    """Seeds step 1's PO picker on an existing document with the purchase
    orders it was actually built from, pre-checked - so opening it for edit
    shows what was loaded rather than an empty picker the operator has to
    re-list from scratch. Scoped to the plan's own proforma invoices, the
    same set step 1 would offer if run again by hand."""
    if not plan:
        return "[]"
    service = container.packing_planning_service
    rows = service.purchase_orders_for_proformas(plan.proforma_invoice_ids, company_id)
    used = {i.purchase_order_id for i in plan.items if i.purchase_order_id}
    for row in rows:
        row["checked"] = row["id"] in used
    return json.dumps(rows)


def _render_form(container, plan, warnings=None, status_code=200):
    """Re-render after a failed POST with exactly what was typed, so nothing
    the operator did is lost - the manual grouping especially, which is the
    part of this document a person actually spends time on.

    The PO picker itself isn't POSTed (only the goods lines it produced are),
    so it comes back empty here - a step 1 click re-lists it in a click."""
    items = _extract_items(request.form)
    html = render_template(
        "packing_plannings/form.html", plan=plan, form_data=request.form,
        items_json=json.dumps(items),
        manual_units_json=request.form.get("manual_units_json") or "[]",
        packing_types_json=_packing_types_json(
            container, g.user.company_id, container.packing_planning_service._clean_items(items)),
        purchase_orders_json="[]",
        selected_proforma_ids=[int(v) for v in request.form.getlist("proforma_invoice_ids[]") if v.isdigit()],
        warnings=warnings or [],
        suggested_number=(plan.packing_planning_number if plan else request.form.get("packing_planning_number")),
        today=request.form.get("packing_planning_date") or date.today().isoformat(),
        **_form_context(container, g.user.company_id),
    )
    return (html, status_code) if status_code != 200 else html


@packing_plannings_bp.route("/api/purchase-orders")
@login_required
def packing_planning_purchase_orders():
    """Step 1 of loading - the `list purchase orders for selected PIs`
    button. Every PO the ticked PIs pulled in, each with a batch-count/
    ready-quantity summary, so the operator can narrow to just the orders
    this packing run wants before step 2 commits to loading anything."""
    raw = request.args.get("proforma_invoice_ids", "")
    ids = [p for p in raw.split(",") if p.strip()]
    return jsonify({
        "purchase_orders": current_app.container.packing_planning_service
        .purchase_orders_for_proformas(ids, g.user.company_id)
    })


@packing_plannings_bp.route("/api/prefill")
@login_required
def packing_planning_prefill():
    """Step 2 of loading - the `load batches from selected POs` button.
    Arrives already auto-filled, since the packing type and the whole-unit
    count are arithmetic, not judgement. Overwrites only the PO-derived
    lines, leaving the document's number/date and any manual grouping
    already built untouched."""
    raw = request.args.get("purchase_order_ids", "")
    ids = [p for p in raw.split(",") if p.strip()]
    return jsonify(
        current_app.container.packing_planning_service.build_prefill_from_purchase_orders(ids, g.user.company_id)
    )


@packing_plannings_bp.route("/api/auto-fill", methods=["POST"])
@login_required
def packing_planning_auto_fill():
    """The `auto-fill` button: re-derive the packing type, its capacity and
    the whole-unit count for every line. Only WHOLE units are taken - the
    remainder is exactly what the second table exists for."""
    service = current_app.container.packing_planning_service
    payload = request.get_json(silent=True) or {}
    try:
        items = service._clean_items(payload.get("items") or [])
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    service.auto_fill(g.user.company_id, items)
    return jsonify({
        "items": [dataclasses.asdict(i) for i in items],
        "packing_types": service.packing_types_for_items(g.user.company_id, items),
    })


@packing_plannings_bp.route("/")
@login_required
def list_packing_plannings():
    plans = current_app.container.packing_planning_service.list_all(g.user.company_id)
    return render_template("packing_plannings/list.html", plans=plans)


@packing_plannings_bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_packing_planning():
    container = current_app.container
    service = container.packing_planning_service
    if request.method == "POST":
        try:
            plan = service.create(
                current_user=g.user, fields=_extract_fields(request.form),
                proforma_ids=request.form.getlist("proforma_invoice_ids[]"),
                items=_extract_items(request.form),
                manual_units=_extract_manual_units(request.form),
            )
            _flash_with_warnings(service, plan, "added")
            return redirect(url_for("packing_plannings.edit_packing_planning", packing_planning_id=plan.id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return _render_form(container, None, status_code=400)

    today = date.today().isoformat()
    return render_template(
        "packing_plannings/form.html", plan=None, form_data=None, items_json="[]",
        manual_units_json="[]", packing_types_json="{}", purchase_orders_json="[]",
        selected_proforma_ids=[], warnings=[],
        suggested_number=service.next_number(g.user.company_id, today), today=today,
        **_form_context(container, g.user.company_id),
    )


@packing_plannings_bp.route("/<int:packing_planning_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_packing_planning(packing_planning_id):
    container = current_app.container
    service = container.packing_planning_service
    try:
        plan = service.get(packing_planning_id, g.user.company_id)
    except NotFoundError:
        abort(404)

    if request.method == "POST":
        try:
            updated = service.update(
                packing_planning_id=packing_planning_id, current_user=g.user,
                fields=_extract_fields(request.form),
                proforma_ids=request.form.getlist("proforma_invoice_ids[]"),
                items=_extract_items(request.form),
                manual_units=_extract_manual_units(request.form),
            )
            _flash_with_warnings(service, updated, "updated")
            return redirect(url_for("packing_plannings.edit_packing_planning",
                                    packing_planning_id=packing_planning_id))
        except (ValidationError, PermissionDeniedError) as e:
            flash(str(e), "error")
            return _render_form(container, plan, status_code=400)

    return render_template(
        "packing_plannings/form.html", plan=plan, form_data=None,
        items_json=json.dumps([dataclasses.asdict(i) for i in plan.items]),
        manual_units_json=json.dumps([_unit_json(u) for u in plan.manual_units]),
        packing_types_json=_packing_types_json(container, g.user.company_id, plan.items),
        purchase_orders_json=_purchase_orders_json(container, g.user.company_id, plan),
        selected_proforma_ids=plan.proforma_invoice_ids,
        warnings=service.packing_warnings(plan),
        suggested_number=plan.packing_planning_number, today=plan.packing_planning_date,
        **_form_context(container, g.user.company_id),
    )


def _unit_json(unit) -> dict:
    """A manual unit as the form's JS holds it - contents included, which
    dataclasses.asdict would give us too, but spelling it out keeps the shape
    the card reads from in one obvious place."""
    return {
        "unit_no": unit.unit_no,
        "packing_type_id": unit.packing_type_id,
        "packing_type_name": unit.packing_type_name,
        "packing_unit_label": unit.packing_unit_label,
        "capacity_boxes": unit.capacity_boxes,
        "remarks": unit.remarks,
        "contents": [dict(c) for c in unit.contents],
    }


def _flash_with_warnings(service, plan, verb: str) -> None:
    """Saved is saved - the packing checks are reported alongside, never
    instead of. A document with 29 boxes still to be grouped by hand is a
    normal, useful intermediate state."""
    flash(f"Packing planning {plan.packing_planning_number} {verb}.", "success")
    for warning in service.packing_warnings(plan):
        flash(warning, "warning")


@packing_plannings_bp.route("/<int:packing_planning_id>")
@login_required
def view_packing_planning(packing_planning_id):
    try:
        plan = current_app.container.packing_planning_service.get(packing_planning_id, g.user.company_id)
    except NotFoundError:
        abort(404)
    return render_template("packing_plannings/print.html", plan=plan)


@packing_plannings_bp.route("/<int:packing_planning_id>/delete", methods=["POST"])
@admin_required
def delete_packing_planning(packing_planning_id):
    if not verify_delete_password(g.user, request.form):
        flash("Incorrect password. Packing planning not deleted.", "error")
        return redirect(url_for("packing_plannings.list_packing_plannings"))
    service = current_app.container.packing_planning_service
    try:
        plan = service.get(packing_planning_id, g.user.company_id)
        service.delete(packing_planning_id, g.user)
        flash(f"Packing planning {plan.packing_planning_number} deleted.", "success")
    except (ValidationError, PermissionDeniedError) as e:
        flash(str(e), "error")
    except NotFoundError:
        abort(404)
    return redirect(url_for("packing_plannings.list_packing_plannings"))
