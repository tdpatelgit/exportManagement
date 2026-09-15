"""
Tests for LoadingPlanningService (app/services.py) - the LOADING PLANNING
document, which works out which goods physically go in which container.

This document packs nothing. A PACKING PLANNING has already turned production
into whole numbered pallets and cartons and minted a permanent label id for
each, so the behaviours worth pinning down are all about IMPORT and
ASSIGNMENT:

  - loading narrows in two steps: proforma invoices -> the packing plannings
    covering them. No purchase-order checkpoint in between, because this
    document picks packing runs and a run already names its own orders.

  - SEVERAL packing plannings merge into one document, because one container
    load draws on more than one packing run. Each numbers its own batches and
    pallets from 1, so both are renumbered document-wide - and a packing's
    contents must still land on the right goods line afterwards, which is the
    single most breakable thing here.

  - goods arrive one line per BATCH. `sr_no` is the document's own and a save
    must never renumber it by position, because the packings reference it;
    `source_sr_no` keeps what the batch was called on its own document.

  - a line's quantity is what the numbered packings HOLD, not what was
    produced. 317 boxes packed as nine pallets of 32 arrive as 288; the 29
    nobody has packed are the packing plan's problem and must not show up
    here as something loadable.

  - `source_packing_no` and the unique packing ids come across UNCHANGED.
    They are already printed on labels stuck to the pallets; a number
    invented here would contradict the floor.

  - auto-assign spreads the packings evenly across the containers by weight
    and refuses to put one in a container that has no room, leaving it
    unassigned instead.

  - nothing here BLOCKS a save. Unassigned packings and an over-weight
    container are reported as warnings, because a plan is built over several
    sittings.
"""

import dataclasses

import pytest

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


# --------------------------------------------------------------------------
# Fixture data: a packing planning, built the way the real one is - product
# with a packing type, PI -> PO -> production batches -> packing plan.
# --------------------------------------------------------------------------
def make_product(container, seed, name, hsn, net, qty_unit, alt_qty, pallet_types):
    return container.product_service.create_product(
        current_user=seed.admin, product_name=name, description="", hsn_code=hsn,
        igst_percent="18", quantity="2", alternate_quantity=alt_qty,
        quantity_unit=qty_unit, alternate_quantity_unit="SQM",
        net_weight_kg=str(net), pallet_types=pallet_types,
    )


def make_design(container, seed, product, name):
    return container.product_service.create_design(
        current_user=seed.admin, product_id=product.id, design_name=name, description="",
        folder_id=None, price_usd="", alt_text="", photo_file=None, dimension_photo_file=None,
    )


def make_chain(container, seed, *, pi_items, po_items, pi_number, po_number):
    pi = container.proforma_invoice_service.create(
        seed.admin,
        {"consignee_name": "ROBUST INTERNATIONAL LIMITADA", "invoice_date": "2026-08-27",
         "invoice_number": pi_number, "currency_code": "USD"},
        pi_items,
    )
    po = container.purchase_order_service.create(
        seed.admin,
        {"seller_name": "ALIVE GRANITO LLP", "po_date": "2026-08-27", "po_number": po_number,
         "proforma_invoice_id": str(pi.id)},
        po_items,
    )
    return pi, po


def record_batches(container, seed, po, item_index, design, batches):
    item = po.items[item_index]
    container.purchase_order_production_service.save_row(
        purchase_order_id=po.id, purchase_order_item_id=item.id,
        design_id=(design.id if design else None),
        design_name=(design.design_name if design else None),
        status="ready", batches=batches, company_id=seed.company_id, user_id=seed.admin.id,
    )


def batch(number, date, qty):
    return {"batch_number": number, "production_date": date,
            "quantity_boxes": str(qty), "remarks": ""}


def _as_form(obj):
    return {k: ("" if v is None else v) for k, v in dataclasses.asdict(obj).items()}


@pytest.fixture
def tiles(container, seed):
    """PO20260827001: one product at 32 boxes/pallet, 27kg a box, fired as two
    batches - 317 (nine pallets and 29 over) and 160 (exactly five, nothing
    over). The two cases the packing plan distinguishes."""
    pallet = [{"name": "Pallet", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    base = make_product(container, seed, "GVT/PGVT 600X1200MM", "69072100", 27.0, "BOX", "1.44", pallet)
    arkose = make_design(container, seed, base, "ARKOSE")
    celeste = make_design(container, seed, base, "CELESTE BLUE")

    def pi_item(boxes, price):
        return {"product_id": str(base.id), "product_name": base.product_name,
                "hsn_code": "69072100", "quantity_boxes": str(boxes), "quantity_unit": "BOX",
                "quantity_value": str(boxes * 1.44), "unit": "SQM", "price_usd": str(price)}

    def po_item(boxes, price):
        return {"product_id": str(base.id), "product_name": base.product_name,
                "hsn_code": "69072100", "quantity_boxes": str(boxes), "quantity_unit": "BOX",
                "quantity_value": str(boxes * 1.44), "unit": "SQM", "price_inr": str(price),
                "price_per": "BOX"}

    pi, po = make_chain(
        container, seed, pi_items=[pi_item(477, 5.5)], po_items=[po_item(477, 418.5)],
        pi_number="PI20260827001", po_number="PO20260827001",
    )
    record_batches(container, seed, po, 0, arkose, [batch("101", "2026-08-29", 317)])
    record_batches(container, seed, po, 0, celeste, [batch("107", "2026-08-28", 160)])
    return {"pi": pi, "po": po, "product": base}


@pytest.fixture
def hardware(container, seed):
    """PO20260827002, packed on its own separate run: 45 PCS each at 30/CTN,
    so 1 full carton and 15 over. A SECOND packing planning, which is what
    makes the merge testable - both documents number their pallets from 1."""
    ctn = [{"name": "CTN", "boxes_per_pallet": "30", "weight_kg": "0.3", "unit_kind": "carton"}]
    rod = make_product(container, seed, "904 TOWEL ROD", "73269030", 0.56, "PCS", "1", ctn)
    d904 = make_design(container, seed, rod, "904")

    def line(price_key, price):
        return {"product_id": str(rod.id), "product_name": rod.product_name,
                "hsn_code": "73269030", "quantity_boxes": "45", "quantity_unit": "PCS",
                "quantity_value": "45", "unit": "PCS", price_key: str(price)}

    pi, po = make_chain(
        container, seed, pi_items=[line("price_usd", 12)],
        po_items=[dict(line("price_inr", 1150), price_per="PCS")],
        pi_number="PI20260827002", po_number="PO20260827002",
    )
    record_batches(container, seed, po, 0, d904, [batch("YU012", "2026-08-22", 45)])
    return {"pi": pi, "po": po, "product": rod}


def packing_plan(container, seed, source, manual_units=None, date="2026-08-30"):
    """Build and save a packing planning off the fixture's purchase order,
    the way its own two-step form does."""
    pp = container.packing_planning_service
    rows = pp.build_prefill_from_purchase_orders([source["po"].id], seed.company_id)["items"]
    items = pp._clean_items(rows)
    return pp.create(current_user=seed.admin, fields={"packing_planning_date": date},
                     proforma_ids=[source["pi"].id], items=[_as_form(i) for i in items],
                     manual_units=manual_units or [])


def prefill(container, seed, *plans):
    return svc(container).build_prefill_from_packing_plannings(
        [p.id for p in plans], seed.company_id)


def svc(container):
    return container.loading_planning_service


def containers_for(*specs):
    """Container rows in the shape the 11B table posts."""
    return [{"container_no": no, "container_type": "20FT FCL",
             "tare_weight_kg": str(tare), "max_permitted_weight": str(mx)}
            for no, tare, mx in specs]


def save(container, seed, loaded, container_rows=None, packings=None, date="2026-09-06"):
    return svc(container).create(
        current_user=seed.admin, fields={"loading_planning_date": date},
        items=loaded["items"], containers=container_rows or [],
        packings=loaded["packings"] if packings is None else packings,
    )


# --------------------------------------------------------------------------
# Importing a packing planning
# --------------------------------------------------------------------------
def test_import_takes_one_goods_line_per_batch(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)

    assert [i["batch_number"] for i in out["items"]] == ["101", "107"]
    assert [i["design_name"] for i in out["items"]] == ["ARKOSE", "CELESTE BLUE"]
    assert all(i["production_date"] for i in out["items"])


def test_goods_lines_keep_the_packing_plans_own_sr_nos(container, seed, tiles):
    """The whole reason for importing rather than re-deriving: a packing's
    contents reference their batch by sr_no."""
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)

    assert [i["sr_no"] for i in out["items"]] == [i.sr_no for i in source.items]
    referenced = {c["item_sr_no"] for p in out["packings"] for c in p["contents"]}
    assert referenced <= {i["sr_no"] for i in out["items"]}


def test_quantity_is_what_the_packings_hold_not_what_was_produced(container, seed, tiles):
    """317 packs as nine pallets of 32 = 288. The 29 left over are the packing
    plan's problem; importing them as loadable would put this document
    permanently out of balance over someone else's decision."""
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    by_batch = {i["batch_number"]: i for i in out["items"]}

    assert by_batch["101"]["quantity_boxes"] == 288      # not 317
    assert by_batch["107"]["quantity_boxes"] == 160      # divided exactly


def test_a_grouped_leftover_does_come_across(container, seed, tiles):
    """Once the packing plan hand-packs the 29 into a mixed unit, they are on
    a real numbered pallet and become loadable."""
    plain = packing_plan(container, seed, tiles)
    leftover = [u for u in plain.remain_rows if u["item_sr_no"] == 1][0]
    source = packing_plan(container, seed, tiles, manual_units=[{
        "unit_no": plain.next_packing_no, "packing_unit_label": "PLT",
        "contents": [{"item_sr_no": leftover["item_sr_no"], "quantity_boxes": leftover["quantity"]}],
    }])
    out = prefill(container, seed, source)
    by_batch = {i["batch_number"]: i for i in out["items"]}

    assert by_batch["101"]["quantity_boxes"] == 317
    assert any(p["is_manual"] for p in out["packings"])


def test_one_packing_per_physical_pallet_with_its_label_ids(container, seed, tiles):
    """Nine pallets and five pallets is fourteen things to put in a container,
    not two rows - and each carries the id already printed on its label."""
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    labels = {l.packing_no: l for l in container.packing_planning_repo.labels_for_plan(source.id)}

    assert len(out["packings"]) == 14
    assert [p["packing_no"] for p in out["packings"]] == list(range(1, 15))
    for p in out["packings"]:
        assert p["unique_packing_id"] == labels[p["packing_no"]].unique_packing_id
        assert p["unique_qr_id"] == labels[p["packing_no"]].unique_qr_id


def test_packings_carry_the_type_label_and_its_tare(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)

    assert {p["packing_unit_label"] for p in out["packings"]} == {"PLT"}
    assert {p["tare_weight_kg"] for p in out["packings"]} == {20.0}


def test_header_reports_the_documents_behind_the_goods(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)

    assert out["header"]["packing_planning_numbers"] == [source.packing_planning_number]
    assert out["header"]["proforma_invoice_numbers"] == ["PI20260827001"]
    assert out["header"]["purchase_order_numbers"] == ["PO20260827001"]


def test_import_is_company_scoped(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    with pytest.raises(NotFoundError):
        svc(container).build_prefill_from_packing_plannings([source.id], seed.company_id + 999)


# --------------------------------------------------------------------------
# The two-step narrowing: PIs -> the packing plannings covering them
# --------------------------------------------------------------------------
def test_step_two_lists_only_the_plans_covering_the_ticked_proformas(container, seed, tiles, hardware):
    """The checkpoint that matters: two PIs packed on two separate runs, and
    ticking one PI must not offer the other's run."""
    tile_plan = packing_plan(container, seed, tiles)
    hw_plan = packing_plan(container, seed, hardware, date="2026-08-31")

    only_tiles = svc(container).packing_plannings_for_proformas(
        [tiles["pi"].id], seed.company_id)
    assert [r["packing_planning_number"] for r in only_tiles] == [tile_plan.packing_planning_number]
    assert only_tiles[0]["po_numbers"] == ["PO20260827001"]
    assert only_tiles[0]["batch_count"] > 0

    both = svc(container).packing_plannings_for_proformas(
        [tiles["pi"].id, hardware["pi"].id], seed.company_id)
    assert {r["packing_planning_number"] for r in both} == {
        tile_plan.packing_planning_number, hw_plan.packing_planning_number}


def test_step_two_lists_a_plan_packed_out_of_job_in_returns(container, seed, tiles):
    """The regression that matters: a JOB IN row carries neither a purchase
    order nor a proforma invoice - it is keyed to its job in - so a plan
    packed wholly out of returned job-work goods is reachable only through
    the plan's OWN proforma links. Walking PI -> POs -> items hid it."""
    pp = container.packing_planning_service
    rows = pp.build_prefill_from_purchase_orders([tiles["po"].id], seed.company_id)["items"]
    items = []
    for row in pp._clean_items(rows):
        form = _as_form(row)
        # Exactly what the job-in loader leaves behind on a line.
        form.update({"purchase_order_id": "", "po_number": "",
                     "purchase_order_item_id": "", "proforma_invoice_id": ""})
        items.append(form)
    plan = pp.create(current_user=seed.admin, fields={"packing_planning_date": "2026-09-01"},
                     proforma_ids=[tiles["pi"].id], items=items, manual_units=[])

    listed = svc(container).packing_plannings_for_proformas([tiles["pi"].id], seed.company_id)
    row = next(r for r in listed if r["packing_planning_number"] == plan.packing_planning_number)
    assert row["po_numbers"] == []          # nothing to name, and that is fine
    assert row["batch_count"] > 0


def test_step_two_is_company_scoped_and_empty_without_proformas(container, seed, tiles):
    packing_plan(container, seed, tiles)
    assert svc(container).packing_plannings_for_proformas([], seed.company_id) == []
    assert svc(container).packing_plannings_for_proformas(
        [tiles["pi"].id], seed.company_id + 999) == []


# --------------------------------------------------------------------------
# Merging several packing plannings into one document
# --------------------------------------------------------------------------
def test_several_packing_plannings_merge_into_one_document(container, seed, tiles, hardware):
    tile_plan = packing_plan(container, seed, tiles)
    hw_plan = packing_plan(container, seed, hardware, date="2026-08-31")
    out = prefill(container, seed, tile_plan, hw_plan)

    assert len(out["items"]) == len(tile_plan.items) + len(hw_plan.items)
    assert len(out["packings"]) == len(tile_plan.packings) + len(hw_plan.packings)
    assert out["header"]["packing_planning_numbers"] == [
        tile_plan.packing_planning_number, hw_plan.packing_planning_number]
    assert out["header"]["purchase_order_numbers"] == ["PO20260827001", "PO20260827002"]


def test_merging_renumbers_both_halves_without_collision(container, seed, tiles, hardware):
    """Both source documents number their batches and pallets from 1, so a
    merge that kept either would silently fuse two different pallets."""
    tile_plan = packing_plan(container, seed, tiles)
    hw_plan = packing_plan(container, seed, hardware, date="2026-08-31")
    out = prefill(container, seed, tile_plan, hw_plan)

    srs = [i["sr_no"] for i in out["items"]]
    nos = [p["packing_no"] for p in out["packings"]]
    assert srs == list(range(1, len(srs) + 1))
    assert nos == list(range(1, len(nos) + 1))
    # Both documents really did contribute a "1".
    assert sorted(i["source_sr_no"] for i in out["items"]).count(1) == 2
    assert sorted(p["source_packing_no"] for p in out["packings"]).count(1) == 2


def test_every_packings_contents_still_land_on_the_right_goods_line(container, seed, tiles, hardware):
    """The single most breakable thing about merging: a packing from the
    second document must reference the SECOND document's batch, not the
    first's line that happens to share its old number."""
    tile_plan = packing_plan(container, seed, tiles)
    hw_plan = packing_plan(container, seed, hardware, date="2026-08-31")
    out = prefill(container, seed, tile_plan, hw_plan)
    by_sr = {i["sr_no"]: i for i in out["items"]}

    for packing in out["packings"]:
        assert packing["contents"], "a packing came across holding nothing"
        for line in packing["contents"]:
            item = by_sr[line["item_sr_no"]]
            assert item["packing_planning_id"] == packing["packing_planning_id"]


def test_a_merged_packing_keeps_its_own_number_and_label(container, seed, tiles, hardware):
    tile_plan = packing_plan(container, seed, tiles)
    hw_plan = packing_plan(container, seed, hardware, date="2026-08-31")
    out = prefill(container, seed, tile_plan, hw_plan)
    hw_labels = {l.packing_no: l for l in container.packing_planning_repo.labels_for_plan(hw_plan.id)}

    from_hw = [p for p in out["packings"] if p["packing_planning_id"] == hw_plan.id]
    assert from_hw
    for packing in from_hw:
        label = hw_labels[packing["source_packing_no"]]
        assert packing["unique_packing_id"] == label.unique_packing_id
        # Displayed as its own number qualified by the document that issued it.
        assert packing["label"] == f"{hw_plan.packing_planning_number} · {packing['source_packing_no']}"


def test_a_merged_plan_round_trips_and_balances(container, seed, tiles, hardware):
    tile_plan = packing_plan(container, seed, tiles)
    hw_plan = packing_plan(container, seed, hardware, date="2026-08-31")
    out = prefill(container, seed, tile_plan, hw_plan)
    saved = save(container, seed, out, containers_for(("AAAA1111111", 2200, 60000)))

    plan = svc(container).get(saved.id, seed.company_id)
    assert sorted(plan.packing_planning_ids) == sorted([tile_plan.id, hw_plan.id])
    assert plan.proforma_invoice_numbers == ["PI20260827001", "PI20260827002"]
    assert len(plan.items) == len(out["items"])
    assert len(plan.packings) == len(out["packings"])
    assert all(abs(b["left"]) < 0.001 for b in plan.line_balances)


# --------------------------------------------------------------------------
# Auto-assign: evenly across the containers, by weight
# --------------------------------------------------------------------------
def test_auto_assign_spreads_packings_evenly(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    items = service._clean_items(out["items"])
    packings = service._clean_packings(out["packings"])
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 30000),
                                                    ("BBBB2222222", 2200, 30000)))

    assigned = service.auto_assign_containers(packings, rows, items)["packings"]
    counts = {}
    for p in assigned:
        counts[p["container_sr_no"]] = counts.get(p["container_sr_no"], 0) + 1

    # Fourteen identical pallets across two containers is seven each.
    assert counts == {1: 7, 2: 7}
    assert None not in counts


def test_auto_assign_leaves_a_packing_unassigned_rather_than_overloading(container, seed, tiles):
    """A pallet is 884kg gross (32 x 27 + 20). A container permitting 5000kg
    over a 2200kg tare has room for three, not fourteen."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    items = service._clean_items(out["items"])
    packings = service._clean_packings(out["packings"])
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 5000)))

    assigned = service.auto_assign_containers(packings, rows, items)["packings"]
    loaded = [p for p in assigned if p["container_sr_no"] == 1]
    assert len(loaded) == 3
    assert len([p for p in assigned if p["container_sr_no"] is None]) == 11


def test_auto_assign_respects_a_container_with_no_stated_limit(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    items = service._clean_items(out["items"])
    packings = service._clean_packings(out["packings"])
    rows = service._clean_containers([{"container_no": "AAAA1111111", "tare_weight_kg": "2200"}])

    assigned = service.auto_assign_containers(packings, rows, items)["packings"]
    assert all(p["container_sr_no"] == 1 for p in assigned)


# --------------------------------------------------------------------------
# Weights and the VGM check
# --------------------------------------------------------------------------
def test_packing_gross_is_contents_net_plus_its_own_tare(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    plan = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))

    packing = plan.packings[0]
    assert packing.net_weight_kg(plan.items_by_sr) == pytest.approx(32 * 27)
    assert packing.gross_weight_kg(plan.items_by_sr) == pytest.approx(32 * 27 + 20)


def test_container_summary_totals_and_flags_an_overweight_container(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    for p in out["packings"]:
        p["container_sr_no"] = 1
    plan = save(container, seed, out, containers_for(("AAAA1111111", 2200, 5000)))

    row = plan.container_summary[0]
    assert row["packing_count"] == 14
    assert row["cargo_weight_kg"] == pytest.approx(14 * (32 * 27 + 20))
    assert row["vgm_kg"] == pytest.approx(row["cargo_weight_kg"] + 2200)
    assert row["over_weight"] is True
    assert any("over the" in w for w in service.packing_warnings(plan))


def test_unassigned_packings_go_in_their_own_summary_row(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    plan = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))

    last = plan.container_summary[-1]
    assert last["container_no"] == "Unassigned"
    assert last["packing_count"] == 14
    assert last["vgm_kg"] is None


# --------------------------------------------------------------------------
# Warnings never block a save
# --------------------------------------------------------------------------
def test_a_plan_with_packings_still_to_assign_saves_with_a_warning(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    plan = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))

    assert plan.id is not None
    assert "14 packing(s) not yet assigned to a container." in service.packing_warnings(plan)


def test_a_freshly_imported_plan_balances(container, seed, tiles):
    """Both halves come off the same document, so nothing is unaccounted
    for - the only warning is about assignment."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    for p in out["packings"]:
        p["container_sr_no"] = 1
    plan = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))

    assert all(abs(b["left"]) < 0.001 for b in plan.line_balances)
    assert service.packing_warnings(plan) == []


def test_a_goods_line_no_packing_covers_warns(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    plan = save(container, seed, out, [], packings=out["packings"][:1])

    assert any("not in any packing" in w for w in service.packing_warnings(plan))


# --------------------------------------------------------------------------
# Persistence and permissions
# --------------------------------------------------------------------------
def test_save_and_reload_round_trips_everything(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    out["packings"][0]["container_sr_no"] = 1
    saved = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))

    plan = service.get(saved.id, seed.company_id)
    assert plan.packing_planning_ids == [source.id]
    assert plan.packing_planning_numbers == [source.packing_planning_number]
    assert len(plan.items) == 2
    assert len(plan.packings) == 14
    assert plan.packings[0].container_sr_no == 1
    assert plan.packings[0].unique_packing_id == out["packings"][0]["unique_packing_id"]
    assert plan.packings[0].contents == out["packings"][0]["contents"]
    # Derived on save from the goods lines, not posted.
    assert plan.proforma_invoice_numbers == ["PI20260827001"]
    assert plan.purchase_order_numbers == ["PO20260827001"]


def test_editing_does_not_renumber_the_goods_lines(container, seed, tiles):
    """sr_no is the packing plan's, and the packings point at it - a save that
    renumbered by position would silently repoint every packing."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    out = prefill(container, seed, source)
    saved = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))

    updated = service.update(
        loading_planning_id=saved.id, current_user=seed.admin,
        fields={"loading_planning_date": "2026-09-07"},
        items=[_as_form(i) for i in saved.items], containers=[],
        packings=[service._packing_json(p) for p in saved.packings],
    )
    assert [i.sr_no for i in updated.items] == [i.sr_no for i in saved.items]
    assert updated.loading_planning_number == saved.loading_planning_number  # frozen


def test_numbering_is_day_scoped(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)

    first = save(container, seed, out)
    second = save(container, seed, out)
    assert first.loading_planning_number == "LP20260906001"
    assert second.loading_planning_number == "LP20260906002"


def test_date_is_required(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    with pytest.raises(ValidationError):
        save(container, seed, out, date="")


def test_a_packing_planning_that_does_not_exist_is_not_found(container, seed, tiles):
    with pytest.raises(NotFoundError):
        svc(container).build_prefill_from_packing_plannings([999999], seed.company_id)


def test_another_companys_plan_is_not_found(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    plan = save(container, seed, out)
    with pytest.raises(NotFoundError):
        svc(container).get(plan.id, seed.company_id + 999)


def test_only_an_admin_can_delete(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    plan = save(container, seed, out)
    with pytest.raises(PermissionDeniedError):
        svc(container).delete(plan.id, seed.employee)


def test_delete_takes_every_child_row_with_it(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    plan = save(container, seed, out, containers_for(("AAAA1111111", 2200, 30000)))
    svc(container).delete(plan.id, seed.admin)

    for table in ("loading_planning_items", "loading_planning_containers",
                  "loading_planning_packings", "loading_planning_packing_contents",
                  "loading_planning_proforma_links"):
        rows = container.db.query(f"SELECT 1 FROM {table} WHERE loading_planning_id = ?", (plan.id,))
        assert rows == []


def test_list_all_counts_both_halves(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    out = prefill(container, seed, source)
    save(container, seed, out)

    row = svc(container).list_all(seed.company_id)[0]
    assert row.item_count == 2
    assert row.packing_count == 14
# --------------------------------------------------------------------------
# Handing the plan to an export invoice
#
# The export invoice form has two importers. The reference-PI one fills the
# commercial half (consignee, charges, bank, supplier/EPCG rows); this one
# fills the physical half - the containers that went out, the goods on them,
# and the container split - and the two must fill disjoint sets of fields so
# either can be re-run on its own.
#
# Three things are worth pinning down, because all three are easy to get
# quietly wrong:
#   - goods MERGE to one row per product. A plan's lines are batches, which
#     is what a pallet is packed out of and nothing an invoice prints.
#   - container_sr_no is 1-based here and the split's container_index is
#     0-based there, over the SAME blank-row filtering the export invoice
#     applies at save time - an off-by-one puts cargo in the wrong container.
#   - a pallet carrying two products contributes its BOX SHARE to each, so
#     the parts still add back up to exactly one pallet.
# --------------------------------------------------------------------------
@pytest.fixture
def mixed(container, seed):
    """One order, two products, each fired in a batch that does NOT divide by
    its packing type - so both leave a remainder and the two remainders can be
    hand-packed onto ONE shared pallet. That shared pallet is the case the
    split's pallet arithmetic turns on."""
    pallet = [{"name": "Pallet", "boxes_per_pallet": "10", "weight_kg": "20", "unit_kind": "pallet"}]
    alpha = make_product(container, seed, "ALPHA 300X300MM", "69072100", 10.0, "BOX", "1", pallet)
    beta = make_product(container, seed, "BETA 300X300MM", "69072200", 20.0, "BOX", "1", pallet)
    a_design = make_design(container, seed, alpha, "A1")
    b_design = make_design(container, seed, beta, "B1")

    def line(product, hsn, boxes, price_key, price, **extra):
        return dict({"product_id": str(product.id), "product_name": product.product_name,
                     "hsn_code": hsn, "quantity_boxes": str(boxes), "quantity_unit": "BOX",
                     "quantity_value": str(boxes), "unit": "SQM", price_key: str(price)}, **extra)

    pi, po = make_chain(
        container, seed,
        pi_items=[line(alpha, "69072100", 25, "price_usd", 4),
                  line(beta, "69072200", 14, "price_usd", 9)],
        po_items=[line(alpha, "69072100", 25, "price_inr", 300, price_per="BOX"),
                  line(beta, "69072200", 14, "price_inr", 700, price_per="BOX")],
        pi_number="PI20260827003", po_number="PO20260827003",
    )
    # 25 packs as two pallets of 10 with 5 over; 14 as one with 4 over.
    record_batches(container, seed, po, 0, a_design, [batch("A101", "2026-08-29", 25)])
    record_batches(container, seed, po, 1, b_design, [batch("B101", "2026-08-29", 14)])
    return {"pi": pi, "po": po, "alpha": alpha, "beta": beta}


def export_prefill(container, seed, plan):
    return svc(container).build_export_invoice_prefill(plan.id, seed.company_id)


def test_export_prefill_merges_batches_into_one_product_line(container, seed, tiles):
    """Two batches of one product are ONE sellable line. The invoice has no
    use for the batch numbers a pallet is packed out of."""
    source = packing_plan(container, seed, tiles)
    loaded = prefill(container, seed, source)
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 30000)))

    out = export_prefill(container, seed, plan)

    assert len(loaded["items"]) == 2                     # two batches on the plan
    assert len(out["items"]) == 1                        # one product on the invoice
    line = out["items"][0]
    assert line["product_name"] == "GVT/PGVT 600X1200MM"
    assert line["quantity_boxes"] == 288 + 160
    assert line["pallets"] == 14                         # nine pallets plus five
    assert line["price_usd"] == 5.5                      # the PI's own quoted rate
    assert line["hsn_code"] == "69072100"


def test_export_prefill_fills_qty_from_boxes_and_alternate_quantity(container, seed, tiles):
    """Qty (SQM/LM) must not arrive blank: it is boxes x the product's
    alternate quantity (1.44 SQM a box here), the same figure the export
    invoice settles on at save time."""
    source = packing_plan(container, seed, tiles)
    plan = save(container, seed, prefill(container, seed, source),
                containers_for(("AAAA1111111", 2200, 30000)))

    assert export_prefill(container, seed, plan)["items"][0]["quantity_value"] == round(448 * 1.44, 2)


def test_export_prefill_takes_the_containers_that_actually_went_out(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    plan = save(container, seed, prefill(container, seed, source),
                containers_for(("AAAA1111111", 2200, 30000), ("BBBB2222222", 2100, 30000)))

    out = export_prefill(container, seed, plan)

    assert [c["container_no"] for c in out["container_details"]] == ["AAAA1111111", "BBBB2222222"]
    assert out["container_details"][0]["tare_weight_kg"] == 2200
    # The type/count summary above the 11B table: two of the one type.
    assert out["containers"] == [{"container_type": "20FT FCL", "container_count": 2}]


def test_export_prefill_leaves_out_a_container_that_never_shipped(container, seed, tiles):
    """A plan row carrying nothing but a type is a placeholder. The export
    invoice's split filters exactly such a row out of its own container list,
    so emitting it would put it in the 11B table and shift every container
    index after it by one - cargo silently in the wrong container."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    loaded = prefill(container, seed, source)
    rows = [{"container_type": "20FT FCL", "container_no": "", "tare_weight_kg": "",
             "max_permitted_weight": "30000"},
            {"container_no": "BBBB2222222", "container_type": "20FT FCL",
             "tare_weight_kg": "2200", "max_permitted_weight": "30000"}]
    packings = loaded["packings"]
    for p in packings:
        p["container_sr_no"] = 2                     # everything in the REAL container
    plan = save(container, seed, loaded, rows, packings=packings)

    out = export_prefill(container, seed, plan)

    # The placeholder is gone, so the real container is index 0 - not 1.
    assert [c["container_no"] for c in out["container_details"]] == ["BBBB2222222"]
    assert {a["container_index"] for a in out["allocations"]} == {0}
    assert any("no container number" in w for w in out["warnings"])


def test_export_prefill_maps_containers_onto_zero_based_indexes(container, seed, tiles):
    """container_sr_no is 1-based on the plan; the export invoice's split is
    0-based. Getting this wrong puts cargo in the wrong container."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    loaded = prefill(container, seed, source)
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 30000),
                                                    ("BBBB2222222", 2200, 30000)))
    packings = service._clean_packings(loaded["packings"])
    items = service._clean_items(loaded["items"])
    assigned = service.auto_assign_containers(packings, rows, items)["packings"]
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 30000),
                                                        ("BBBB2222222", 2200, 30000)),
                packings=assigned)

    out = export_prefill(container, seed, plan)

    assert sorted({a["container_index"] for a in out["allocations"]}) == [0, 1]
    # Seven pallets each, one product - so seven pallets and 7 x 32 boxes a side.
    assert sorted(a["quantity_boxes"] for a in out["allocations"]) == [224, 224]
    assert sorted(a["pallets"] for a in out["allocations"]) == [7, 7]


def test_export_prefill_split_adds_back_up_to_the_goods_line(container, seed, tiles):
    """The invariant the export invoice's own submit guard enforces: every box
    on a goods line is in exactly one container."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    loaded = prefill(container, seed, source)
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 30000),
                                                    ("BBBB2222222", 2200, 30000)))
    assigned = service.auto_assign_containers(
        service._clean_packings(loaded["packings"]), rows,
        service._clean_items(loaded["items"]))["packings"]
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 30000),
                                                        ("BBBB2222222", 2200, 30000)),
                packings=assigned)

    out = export_prefill(container, seed, plan)
    allocated = sum(a["quantity_boxes"] for a in out["allocations"])

    assert allocated == out["items"][0]["quantity_boxes"]
    assert not out["warnings"]


def test_export_prefill_weights_are_the_plans_own(container, seed, tiles):
    """Net is boxes x the per-box weight; gross adds the packing's REAL tare,
    which the plan snapshots - not the export form's catalog guess."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    loaded = prefill(container, seed, source)
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 300000)))
    assigned = service.auto_assign_containers(
        service._clean_packings(loaded["packings"]), rows,
        service._clean_items(loaded["items"]))["packings"]
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 300000)),
                packings=assigned)

    out = export_prefill(container, seed, plan)
    alloc = out["allocations"][0]

    assert alloc["net_weight_kg"] == pytest.approx(448 * 27)
    assert alloc["gross_weight_kg"] == pytest.approx(448 * 27 + 14 * 20)


def test_export_prefill_splits_a_shared_pallet_by_box_share(container, seed, mixed):
    """Five boxes of ALPHA and four of BETA hand-packed onto one pallet is
    5/9 and 4/9 of a pallet, not one pallet each - so the pallet counts still
    add back up to what physically went out."""
    plain = packing_plan(container, seed, mixed)
    leftovers = plain.remain_rows
    source = packing_plan(container, seed, mixed, manual_units=[{
        "unit_no": plain.next_packing_no, "packing_unit_label": "PLT",
        "contents": [{"item_sr_no": r["item_sr_no"], "quantity_boxes": r["left"]}
                     for r in leftovers if r["left"]],
    }])
    service = svc(container)
    loaded = prefill(container, seed, source)
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 300000)))
    assigned = service.auto_assign_containers(
        service._clean_packings(loaded["packings"]), rows,
        service._clean_items(loaded["items"]))["packings"]
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 300000)),
                packings=assigned)

    out = export_prefill(container, seed, plan)
    by_name = {i["product_name"]: i for i in out["items"]}

    # Everything produced is now on a numbered pallet, shared one included.
    assert by_name["ALPHA 300X300MM"]["quantity_boxes"] == 25
    assert by_name["BETA 300X300MM"]["quantity_boxes"] == 14
    # Four whole pallets (two ALPHA, one BETA, and the shared one split 5:4).
    assert by_name["ALPHA 300X300MM"]["pallets"] == pytest.approx(2 + 5 / 9, abs=0.01)
    assert by_name["BETA 300X300MM"]["pallets"] == pytest.approx(1 + 4 / 9, abs=0.01)
    assert sum(a["pallets"] for a in out["allocations"]) == pytest.approx(4, abs=0.01)


def test_export_prefill_warns_about_a_packing_still_to_be_loaded(container, seed, tiles):
    """A plan half-assigned is a normal state here - it must come across with
    what IS loaded, and say plainly why the goods lines won't balance."""
    source = packing_plan(container, seed, tiles)
    loaded = prefill(container, seed, source)
    packings = loaded["packings"]
    for p in packings:
        p["container_sr_no"] = 1
    packings[0]["container_sr_no"] = None
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 300000)),
                packings=packings)

    out = export_prefill(container, seed, plan)
    allocated = sum(a["quantity_boxes"] for a in out["allocations"])

    assert allocated == 448 - 32                         # the unloaded pallet is missing
    assert out["items"][0]["quantity_boxes"] == 448      # but the goods line is whole
    assert len(out["warnings"]) == 1
    assert "not in a container" in out["warnings"][0]


def test_export_prefill_is_company_scoped(container, seed, tiles):
    source = packing_plan(container, seed, tiles)
    plan = save(container, seed, prefill(container, seed, source),
                containers_for(("AAAA1111111", 2200, 30000)))

    with pytest.raises(NotFoundError):
        svc(container).build_export_invoice_prefill(plan.id, seed.company_id + 999)


def test_export_dropdown_lists_only_plans_for_the_ticked_proformas(container, seed, tiles, hardware):
    source = packing_plan(container, seed, tiles)
    plan = save(container, seed, prefill(container, seed, source),
                containers_for(("AAAA1111111", 2200, 30000)))
    service = svc(container)

    listed = service.loading_plannings_for_proformas([tiles["pi"].id], seed.company_id)
    assert [p["loading_planning_number"] for p in listed] == [plan.loading_planning_number]
    assert listed[0]["container_count"] == 1
    assert listed[0]["packing_count"] == 14

    assert service.loading_plannings_for_proformas([hardware["pi"].id], seed.company_id) == []
    assert service.loading_plannings_for_proformas([], seed.company_id) == []
def _posted(rows):
    """The prefill as the browser actually sends it back: every value a
    string, the way the form's inputs post."""
    return [{k: ("" if v is None else str(v)) for k, v in row.items()} for row in rows]


def test_a_loaded_plan_becomes_a_balanced_export_invoice_and_packing_list(container, seed, tiles):
    """End to end, and the reason the index arithmetic above matters: the
    prefill goes back through the export invoice form unchanged and has to
    produce a packing list that balances - ExportPackingListService.build_items
    refuses one that doesn't, which is the hard check this whole feature has
    to satisfy."""
    source = packing_plan(container, seed, tiles)
    service = svc(container)
    loaded = prefill(container, seed, source)
    rows = service._clean_containers(containers_for(("AAAA1111111", 2200, 30000),
                                                    ("BBBB2222222", 2200, 30000)))
    assigned = service.auto_assign_containers(
        service._clean_packings(loaded["packings"]), rows,
        service._clean_items(loaded["items"]))["packings"]
    plan = save(container, seed, loaded, containers_for(("AAAA1111111", 2200, 30000),
                                                        ("BBBB2222222", 2200, 30000)),
                packings=assigned)

    out = export_prefill(container, seed, plan)
    invoice = container.export_invoice_service.create(
        seed.admin,
        {"consignee_name": "ROBUST INTERNATIONAL", "invoice_date": "2026-09-10",
         "tax_mode": "igst", "exchange_rate": "86.70", "export_invoice_number": "1000000042",
         "booking_no": out["fields"]["booking_no"] or "",
         "vessel_name": out["fields"]["vessel_name"] or "",
         "voyage_no": out["fields"]["voyage_no"] or "",
         "container_details_list": _posted(out["container_details"]),
         "packing_allocations": _posted(out["allocations"])},
        _posted(out["items"]),
    )

    packing_list = container.export_packing_list_service.get_for_invoice(invoice.id, seed.company_id)
    assert packing_list is not None
    # Both containers carry cargo, and the split adds back up to the invoice.
    assert {i.container_sr_no for i in packing_list.items} == {1, 2}
    assert sum(i.quantity_boxes for i in packing_list.items) == 448
    assert sum(i.quantity_boxes for i in invoice.items) == 448
    assert [c["container_no"] for c in invoice.container_details] == ["AAAA1111111", "BBBB2222222"]
