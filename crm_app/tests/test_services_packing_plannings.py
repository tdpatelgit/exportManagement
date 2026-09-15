"""
Tests for PackingPlanningService (app/services.py) - the PACKING PLANNING
document, which works out how what the supplier has actually PRODUCED breaks
into whole numbered pallets and cartons, and what is left over.

The behaviours worth pinning down are the ones the document exists for, and
they are checked against the two real orders that motivated it:

  - loading is two explicit steps: list the purchase orders the ticked PIs
    pulled in, then load batches from whichever of THOSE are ticked. Lines
    arrive per BATCH, one level past the PO - a batch number and a
    manufacturing date exist nowhere else in the app, and a pallet is packed
    out of one firing: ATLANTA LIGHT GREY is one design and two lines, 200
    boxes under batch 102 and 117 under 103.

  - only WHOLE units are taken. 317 boxes at 32/pallet reads 9.91 PLT and
    packs 9 holding 288, leaving 29 - and 45 PCS at 30/CTN reads 1.50 CTN and
    packs 1 holding 30, leaving 15. A batch that divides exactly (160 at 32 is
    5.00 PLT) leaves nothing and never reaches the manual table at all.

  - packing numbers run continuously down the document and carry on through
    the hand-packed units, so a pallet number is unique however it was packed.
    Pinning one row's start reseats every row after it - which is exactly what
    the spreadsheet this replaces could not do, and why its rows 8-10 silently
    reused numbers 41-46 that rows 6 and 7 had already taken.

  - leftovers from different batches can share one unit. Whether ARKOSE's 29
    and ATLANTA's 8 go on the same pallet is the judgement call no rule can
    make, which is why that half is manual.

  - nothing here BLOCKS a save. Ungrouped leftovers, over-packing and
    duplicate numbers are all reported as warnings, because the batches are
    keyed in as the supplier reports them and grouped days later.
"""

import pytest

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


# --------------------------------------------------------------------------
# Fixture data: the two real orders, rebuilt from scratch.
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
    """One proforma invoice -> one purchase order, which is the shape
    purchase_orders_for_proformas walks before build_prefill_from_purchase_orders
    reaches the batches."""
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
    """Key production batches against one purchase order line, the way the
    Production Status page does."""
    item = po.items[item_index]
    container.purchase_order_production_service.save_row(
        purchase_order_id=po.id, purchase_order_item_id=item.id,
        design_id=(design.id if design else None),
        design_name=(design.design_name if design else None),
        status="ready", batches=batches, company_id=seed.company_id, user_id=seed.admin.id,
    )


def batch(number, date, qty, remarks=""):
    return {"batch_number": number, "production_date": date,
            "quantity_boxes": str(qty), "remarks": remarks}


@pytest.fixture
def tiles(container, seed):
    """PO20260827001, and the batches its designs were actually fired in -
    the twelve tile rows of the source sheet, less the hardware."""
    pallet = [{"name": "Pallet", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    khatli = [{"name": "JUNGLE KHATLI", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    base = make_product(container, seed, "GVT/PGVT 600X1200MM", "69072100", 27.0, "BOX", "1.44", pallet)
    hg = make_product(container, seed, "GVT/PGVT 600X1200MM HG", "69072100", 27.0, "BOX", "1.44", khatli)
    carving = make_product(container, seed, "GVT/PGVT 600X1200MM CARVING", "69072100", 27.0, "BOX", "1.44", khatli)

    arkose = make_design(container, seed, base, "ARKOSE")
    atlanta = make_design(container, seed, base, "ATLANTA LIGHT GREY")
    artistic = make_design(container, seed, base, "ARTISTIC BEIGE")
    belly = make_design(container, seed, base, "BELLY WOOD BROWN")
    celeste = make_design(container, seed, hg, "CELESTE BLUE")
    morena = make_design(container, seed, carving, "MORENA MARFIL")

    def pi_item(product, boxes, price):
        return {"product_id": str(product.id), "product_name": product.product_name,
                "hsn_code": "69072100", "quantity_boxes": str(boxes), "quantity_unit": "BOX",
                "quantity_value": str(boxes * 1.44), "unit": "SQM", "price_usd": str(price)}

    def po_item(product, boxes, price):
        return {"product_id": str(product.id), "product_name": product.product_name,
                "hsn_code": "69072100", "quantity_boxes": str(boxes), "quantity_unit": "BOX",
                "quantity_value": str(boxes * 1.44), "unit": "SQM", "price_inr": str(price),
                "price_per": "BOX"}

    pi, po = make_chain(
        container, seed,
        pi_items=[pi_item(base, 1268, 5.5), pi_item(hg, 310, 7.5), pi_item(carving, 310, 8.5)],
        po_items=[po_item(base, 1268, 418.5), po_item(hg, 310, 496), po_item(carving, 310, 527)],
        pi_number="PI20260827001", po_number="PO20260827001",
    )

    # Line 0 (the 1268-box product) was fired as four designs; ATLANTA in two
    # firings, which is why it becomes two lines on the packing plan.
    record_batches(container, seed, po, 0, arkose, [batch("101", "2026-08-29", 317, "ok")])
    record_batches(container, seed, po, 0, atlanta,
                   [batch("102", "2026-08-27", 200), batch("103", "2026-08-28", 117)])
    record_batches(container, seed, po, 0, artistic, [batch("104", "2026-08-28", 317)])
    record_batches(container, seed, po, 0, belly, [batch("105", "2026-08-29", 317)])
    # 106 leaves 22; 107 is 160 = exactly five pallets and leaves nothing.
    record_batches(container, seed, po, 1, celeste,
                   [batch("106", "2026-08-27", 150), batch("107", "2026-08-28", 160)])
    record_batches(container, seed, po, 2, morena,
                   [batch("108", "2026-08-27", 100), batch("109", "2026-08-28", 90),
                    batch("111", "2026-08-29", 120)])
    return {"pi": pi, "po": po, "base": base, "hg": hg, "carving": carving,
            "arkose": arkose, "atlanta": atlanta, "celeste": celeste}


@pytest.fixture
def hardware(container, seed):
    """PO20260827002: 45 PCS each, packed 30 to a CTN. The CTN is a CARTON,
    not a pallet, and the sheet has to say CTN rather than PLT."""
    ctn = [{"name": "CTN", "boxes_per_pallet": "30", "weight_kg": "0.3", "unit_kind": "carton"}]
    rod = make_product(container, seed, "904 TOWEL ROD", "73269030", 0.56, "PCS", "1", ctn)
    dish = make_product(container, seed, "910 DOUBLE SOAP DISH", "73269030", 0.425, "PCS", "1", ctn)
    d904 = make_design(container, seed, rod, "904")
    d910 = make_design(container, seed, dish, "910")

    def line(product, price_key, price):
        return {"product_id": str(product.id), "product_name": product.product_name,
                "hsn_code": "73269030", "quantity_boxes": "45", "quantity_unit": "PCS",
                "quantity_value": "45", "unit": "PCS", price_key: str(price)}

    pi, po = make_chain(
        container, seed,
        pi_items=[line(rod, "price_usd", 12), line(dish, "price_usd", 15)],
        po_items=[dict(line(rod, "price_inr", 1150), price_per="PCS"),
                  dict(line(dish, "price_inr", 1175), price_per="PCS")],
        pi_number="PI20260827002", po_number="PO20260827002",
    )
    record_batches(container, seed, po, 0, d904, [batch("YU012", "2026-08-22", 45)])
    record_batches(container, seed, po, 1, d910, [batch("KL365", "2026-08-22", 45)])
    return {"pi": pi, "po": po, "rod": rod, "dish": dish}


def svc(container):
    return container.packing_planning_service


def prefill_items(container, seed, *pis):
    """The two-step load, run end to end: list the purchase orders the given
    PIs pulled in, then load batches from every one of them - the "just
    click List, then Load" path with nothing unticked."""
    service = svc(container)
    pi_ids = [pi.id for pi in pis]
    po_ids = [row["id"] for row in service.purchase_orders_for_proformas(pi_ids, seed.company_id)]
    return service._clean_items(service.build_prefill_from_purchase_orders(po_ids, seed.company_id)["items"])


def save(container, seed, items, manual_units=None, date="2026-08-30"):
    return svc(container).create(
        current_user=seed.admin, fields={"packing_planning_date": date},
        proforma_ids=[], items=[_as_form(i) for i in items], manual_units=manual_units or [],
    )


def _as_form(item):
    """A cleaned item back in the shape the form posts, so the round trip
    under test is the one the routes actually make."""
    import dataclasses
    return {k: ("" if v is None else v) for k, v in dataclasses.asdict(item).items()}


# --------------------------------------------------------------------------
# Loading batches: PI -> purchase orders -> their production batches
# --------------------------------------------------------------------------
def test_prefill_gives_one_line_per_batch_not_per_design(container, seed, tiles):
    """The order has three product lines and six designs, but TEN batches -
    ATLANTA, CELESTE and MORENA were each fired more than once."""
    items = prefill_items(container, seed, tiles["pi"])
    assert len(items) == 10
    atlanta = [i for i in items if i.design_name == "ATLANTA LIGHT GREY"]
    assert [(i.batch_number, i.ready_quantity) for i in atlanta] == [("102", 200), ("103", 117)]


def test_a_line_carries_its_batch_number_and_manufacturing_date(container, seed, tiles):
    """The two columns that exist nowhere else in the app."""
    items = prefill_items(container, seed, tiles["pi"])
    arkose = next(i for i in items if i.design_name == "ARKOSE")
    assert arkose.batch_number == "101"
    assert arkose.production_date == "2026-08-29"
    assert arkose.product_name == "GVT/PGVT 600X1200MM"
    assert arkose.po_number == "PO20260827001"


def test_two_proforma_invoices_load_into_one_document(container, seed, tiles, hardware):
    """The source sheet is one document over both orders."""
    items = prefill_items(container, seed, tiles["pi"], hardware["pi"])
    assert len(items) == 12
    assert [i.sr_no for i in items] == list(range(1, 13))
    assert {i.po_number for i in items} == {"PO20260827001", "PO20260827002"}


def test_a_batch_with_no_quantity_is_not_a_line(container, seed, tiles):
    """The production form always carries a spare blank row."""
    record_batches(container, seed, tiles["po"], 0, tiles["arkose"],
                   [batch("101", "2026-08-29", 317), batch("", "", 0, "nothing yet")])
    items = prefill_items(container, seed, tiles["pi"])
    assert len(items) == 10


# --------------------------------------------------------------------------
# Step 1: listing purchase orders before committing to loading their batches
# --------------------------------------------------------------------------
def test_purchase_orders_for_proformas_lists_one_row_per_po_with_a_batch_summary(container, seed, tiles):
    """Not the goods yet - just what loading this PI would offer, so the
    operator can narrow the set before step 2 commits to anything."""
    rows = svc(container).purchase_orders_for_proformas([tiles["pi"].id], seed.company_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["po_number"] == "PO20260827001"
    assert row["proforma_invoice_number"] == "PI20260827001"
    assert row["batch_count"] == 10
    assert row["ready_totals"] == {"BOX": 1888}


def test_two_selected_pis_list_both_their_purchase_orders(container, seed, tiles, hardware):
    rows = svc(container).purchase_orders_for_proformas(
        [tiles["pi"].id, hardware["pi"].id], seed.company_id)
    assert {r["po_number"] for r in rows} == {"PO20260827001", "PO20260827002"}
    hw_row = next(r for r in rows if r["po_number"] == "PO20260827002")
    assert hw_row["batch_count"] == 2
    assert hw_row["ready_totals"] == {"PCS": 90}


def test_only_the_ticked_purchase_orders_prefill(container, seed, tiles, hardware):
    """Step 1 lists both orders; step 2, scoped to just one of them, loads
    only that one's batches - the whole point of the checkpoint."""
    service = svc(container)
    rows = service.purchase_orders_for_proformas([tiles["pi"].id, hardware["pi"].id], seed.company_id)
    assert len(rows) == 2
    only_tiles = service.build_prefill_from_purchase_orders([tiles["po"].id], seed.company_id)
    assert len(only_tiles["items"]) == 10
    assert {i["po_number"] for i in only_tiles["items"]} == {"PO20260827001"}


def test_purchase_orders_for_proformas_scopes_to_company(container, seed, db, tiles):
    other = container.tenant_repo.create("OTHER CO", "other")
    assert svc(container).purchase_orders_for_proformas([tiles["pi"].id], other.id) == []


def test_build_prefill_from_purchase_orders_scopes_to_company(container, seed, db, tiles):
    other = container.tenant_repo.create("OTHER CO", "other")
    result = svc(container).build_prefill_from_purchase_orders([tiles["po"].id], other.id)
    assert result["items"] == []


# --------------------------------------------------------------------------
# The arithmetic: whole units only
# --------------------------------------------------------------------------
def test_317_boxes_at_32_per_pallet_plans_9_91_packs_9_and_leaves_29(container, seed, tiles):
    items = prefill_items(container, seed, tiles["pi"])
    arkose = next(i for i in items if i.batch_number == "101")
    assert arkose.boxes_per_unit == 32
    assert arkose.packing_unit_label == "PLT"
    assert arkose.as_per_pl_packing == pytest.approx(9.91)
    assert arkose.actual_packing == 9
    assert arkose.packed_quantity == pytest.approx(288)
    assert arkose.remain_quantity == pytest.approx(29)


def test_every_row_of_the_source_sheet_comes_out_the_same(container, seed, tiles, hardware):
    """The whole first table, against the spreadsheet it replaces."""
    items = prefill_items(container, seed, tiles["pi"], hardware["pi"])
    got = [(i.batch_number, i.ready_quantity, round(i.as_per_pl_packing, 2),
            i.actual_packing, i.packed_quantity) for i in items]
    assert got == [
        ("101", 317, 9.91, 9, 288), ("102", 200, 6.25, 6, 192), ("103", 117, 3.66, 3, 96),
        ("104", 317, 9.91, 9, 288), ("105", 317, 9.91, 9, 288), ("106", 150, 4.69, 4, 128),
        ("107", 160, 5.00, 5, 160), ("108", 100, 3.13, 3, 96), ("109", 90, 2.81, 2, 64),
        ("111", 120, 3.75, 3, 96), ("YU012", 45, 1.50, 1, 30), ("KL365", 45, 1.50, 1, 30),
    ]


def test_45_pcs_at_30_per_carton_is_1_50_ctn_labelled_ctn_not_plt(container, seed, hardware):
    """unit_kind is the whole reason the label is not hardcoded."""
    items = prefill_items(container, seed, hardware["pi"])
    rod = next(i for i in items if i.batch_number == "YU012")
    assert rod.boxes_per_unit == 30
    assert rod.packing_unit_label == "CTN"
    assert rod.packing_type_name == "CTN"
    assert rod.as_per_pl_packing == pytest.approx(1.50)
    assert rod.actual_packing == 1
    assert rod.remain_quantity == pytest.approx(15)


def test_a_product_with_no_packing_type_is_left_for_a_human(container, seed):
    """No capacity means no arithmetic - and a warning, not a guess."""
    product = make_product(container, seed, "UNPACKED TILE", "69072100", 27.0, "BOX", "1.44", [])
    design = make_design(container, seed, product, "PLAIN")
    pi, po = make_chain(
        container, seed,
        pi_items=[{"product_id": str(product.id), "product_name": product.product_name,
                   "quantity_boxes": "100", "quantity_unit": "BOX", "quantity_value": "144",
                   "unit": "SQM", "price_usd": "5"}],
        po_items=[{"product_id": str(product.id), "product_name": product.product_name,
                   "quantity_boxes": "100", "quantity_unit": "BOX", "quantity_value": "144",
                   "unit": "SQM", "price_inr": "400", "price_per": "BOX"}],
        pi_number="PI20260827009", po_number="PO20260827009",
    )
    record_batches(container, seed, po, 0, design, [batch("200", "2026-08-29", 100)])
    items = prefill_items(container, seed, pi)
    assert items[0].boxes_per_unit is None
    assert items[0].actual_packing == 0
    plan = save(container, seed, items)
    assert any("no packing type" in w for w in svc(container).packing_warnings(plan))


# --------------------------------------------------------------------------
# The manual half: what did not divide
# --------------------------------------------------------------------------
def test_a_batch_that_divides_exactly_leaves_no_manual_row(container, seed, tiles, hardware):
    """Batch 107's 160 boxes are exactly five pallets, so twelve auto rows
    produce eleven manual ones - which is what the source sheet shows."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]))
    rows = plan.remain_rows
    assert len(rows) == 11
    assert "107" not in [r["batch_number"] for r in rows]
    assert [r["quantity"] for r in rows] == [29, 8, 21, 29, 29, 22, 4, 26, 24, 15, 15]
    assert [r["sr_no"] for r in rows] == list(range(1, 12))


def test_the_manual_table_moves_when_actual_packing_is_typed_over(container, seed, tiles):
    """It is derived, not stored - which is exactly why it can't drift."""
    items = prefill_items(container, seed, tiles["pi"])
    items[0].actual_packing = 8                      # one pallet fewer than the nine offered
    plan = save(container, seed, items)
    assert plan.items[0].packed_quantity == pytest.approx(256)
    assert plan.remain_rows[0]["quantity"] == pytest.approx(61)


def test_leftovers_from_two_batches_can_share_one_manual_unit(container, seed, tiles):
    """ARKOSE's 29 and ATLANTA's 8 on one pallet - the judgement call no rule
    can make, and the reason this half is manual."""
    items = prefill_items(container, seed, tiles["pi"])
    plan = save(container, seed, items, manual_units=[
        {"unit_no": "50", "packing_type_name": "Pallet", "packing_unit_label": "PLT",
         "contents": [{"item_sr_no": "1", "quantity_boxes": "29"},
                      {"item_sr_no": "2", "quantity_boxes": "8"}]},
    ])
    unit = plan.manual_units[0]
    assert unit.unit_no == 50
    assert unit.packed_boxes == pytest.approx(37)
    rows = {r["batch_number"]: r for r in plan.remain_rows}
    assert rows["101"]["left"] == pytest.approx(0)
    assert rows["101"]["unit_nos"] == [50]
    assert rows["103"]["left"] == pytest.approx(21)   # untouched


def test_an_ungrouped_leftover_warns_but_still_saves(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    assert plan.id is not None
    assert not plan.is_fully_packed
    warnings = svc(container).packing_warnings(plan)
    assert any("29 BOX still to be packed by hand" in w for w in warnings)


def test_over_packing_a_batch_warns_but_still_saves(container, seed, tiles):
    """Ten pallets of a batch that only made 317 boxes."""
    items = prefill_items(container, seed, tiles["pi"])
    items[0].actual_packing = 10
    plan = save(container, seed, items)
    assert plan.id is not None
    assert any("MORE than was produced" in w for w in svc(container).packing_warnings(plan))


def test_over_filling_a_manual_unit_warns_but_still_saves(container, seed, tiles):
    items = prefill_items(container, seed, tiles["pi"])
    plan = save(container, seed, items, manual_units=[
        {"unit_no": "50", "capacity_boxes": "32",
         "contents": [{"item_sr_no": "1", "quantity_boxes": "29"},
                      {"item_sr_no": "2", "quantity_boxes": "8"}]},
    ])
    assert plan.id is not None
    assert any("against a capacity of 32" in w for w in svc(container).packing_warnings(plan))


# --------------------------------------------------------------------------
# Packing numbers: one continuous sequence over both halves
# --------------------------------------------------------------------------
def test_packing_numbers_run_continuously_down_the_document(container, seed, tiles, hardware):
    """1-9, 10-15, 16-18, 19-27 ... exactly as the source sheet's first rows
    read, and without the overlap its later ones had."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]))
    numbers = plan.packing_numbers
    assert [numbers[i.sr_no] for i in plan.items] == [
        (1, 9), (10, 15), (16, 18), (19, 27), (28, 36), (37, 40),
        (41, 45), (46, 48), (49, 50), (51, 53), (54, 54), (55, 55),
    ]
    assert plan.duplicate_packing_numbers == []


def test_a_row_that_packs_nothing_takes_no_number(container, seed, tiles):
    """It has no pallets to number, and must not consume one either."""
    items = prefill_items(container, seed, tiles["pi"])
    items[0].actual_packing = 0
    plan = save(container, seed, items)
    assert plan.packing_numbers[1] == (None, None)
    assert plan.packing_numbers[2] == (1, 6)


def test_a_pinned_start_number_reseats_every_row_after_it(container, seed, tiles):
    items = prefill_items(container, seed, tiles["pi"])
    items[2].packing_no_start = 100
    plan = save(container, seed, items)
    numbers = plan.packing_numbers
    assert numbers[2] == (10, 15)      # untouched, ahead of the pin
    assert numbers[3] == (100, 102)    # pinned
    assert numbers[4] == (103, 111)    # counts on from the pinned row's end


def test_a_pinned_number_that_collides_warns_but_still_saves(container, seed, tiles):
    """The exact error the spreadsheet made silently: rows 8-10 reusing
    41-46, which rows 6 and 7 had already taken."""
    items = prefill_items(container, seed, tiles["pi"])
    items[1].packing_no_start = 5      # row 1 already holds 1-9
    plan = save(container, seed, items)
    assert plan.id is not None
    assert plan.duplicate_packing_numbers == [5, 6, 7, 8, 9]
    assert any("used more than once" in w for w in svc(container).packing_warnings(plan))


def test_a_manual_unit_takes_the_next_number_after_the_auto_rows(container, seed, tiles, hardware):
    """Both halves share one sequence, so a pallet number is unique however
    it was packed."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]))
    assert plan.next_packing_no == 56
    with_unit = save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]),
                     manual_units=[{"unit_no": "56", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"}]}])
    assert with_unit.next_packing_no == 57
    assert with_unit.duplicate_packing_numbers == []
    assert with_unit.total_units == 55 + 1


# --------------------------------------------------------------------------
# Persistence, numbering and permissions
# --------------------------------------------------------------------------
def test_plan_round_trips_through_save_and_reload(container, seed, tiles, hardware):
    items = prefill_items(container, seed, tiles["pi"], hardware["pi"])
    plan = svc(container).create(
        current_user=seed.admin,
        fields={"packing_planning_date": "2026-08-30", "remarks": "first run"},
        proforma_ids=[str(tiles["pi"].id), str(hardware["pi"].id)],
        items=[_as_form(i) for i in items],
        manual_units=[{"unit_no": "56", "packing_type_name": "Pallet", "packing_unit_label": "PLT",
                       "capacity_boxes": "32", "remarks": "mixed",
                       "contents": [{"item_sr_no": "1", "quantity_boxes": "29"},
                                    {"item_sr_no": "2", "quantity_boxes": "8"}]}],
    )
    reloaded = svc(container).get(plan.id, seed.company_id)
    assert reloaded.remarks == "first run"
    assert reloaded.proforma_invoice_ids == sorted([tiles["pi"].id, hardware["pi"].id])
    assert len(reloaded.items) == 12
    assert reloaded.items[0].batch_number == "101"
    assert reloaded.items[0].production_date == "2026-08-29"
    assert reloaded.items[0].boxes_per_unit == 32
    unit = reloaded.manual_units[0]
    assert (unit.unit_no, unit.capacity_boxes, unit.remarks) == (56, 32, "mixed")
    assert len(unit.contents) == 2
    assert reloaded.packing_numbers[1] == (1, 9)


def test_editing_renumbers_the_lines_and_keeps_the_document_number(container, seed, tiles):
    items = prefill_items(container, seed, tiles["pi"])
    plan = save(container, seed, items)
    updated = svc(container).update(
        packing_planning_id=plan.id, current_user=seed.admin,
        fields={"packing_planning_date": "2026-08-31", "packing_planning_number": "PPNOPE"},
        proforma_ids=[], items=[_as_form(i) for i in plan.items[2:]], manual_units=[],
    )
    assert updated.packing_planning_number == plan.packing_planning_number  # frozen on edit
    assert updated.packing_planning_date == "2026-08-31"
    assert [i.sr_no for i in updated.items] == list(range(1, 9))
    assert updated.items[0].batch_number == "103"


def test_number_follows_the_day_scoped_sequence(container, seed, tiles):
    first = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    second = save(container, seed, [])
    third = save(container, seed, [], date="2026-08-31")
    assert first.packing_planning_number == "PP20260830001"
    assert second.packing_planning_number == "PP20260830002"
    assert third.packing_planning_number == "PP20260831001"


def test_a_date_is_required(container, seed):
    with pytest.raises(ValidationError):
        svc(container).create(current_user=seed.admin, fields={"packing_planning_date": ""},
                              proforma_ids=[], items=[], manual_units=[])


def test_a_quantity_that_is_not_a_number_is_a_typo_worth_reporting(container, seed):
    with pytest.raises(ValidationError):
        svc(container).create(
            current_user=seed.admin, fields={"packing_planning_date": "2026-08-30"},
            proforma_ids=[], manual_units=[],
            items=[{"product_name": "TILE", "ready_quantity": "three hundred"}],
        )


def test_another_companys_plan_is_a_404_not_a_403(container, seed, db, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    other = container.tenant_repo.create("OTHER CO", "other")
    with pytest.raises(NotFoundError):
        svc(container).get(plan.id, other.id)


def test_only_an_admin_can_delete(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    with pytest.raises(PermissionDeniedError):
        svc(container).delete(plan.id, seed.employee)
    svc(container).delete(plan.id, seed.admin)
    with pytest.raises(NotFoundError):
        svc(container).get(plan.id, seed.company_id)


def test_deleting_takes_the_manual_units_with_it(container, seed, db, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]),
                manual_units=[{"unit_no": "50", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"}]}])
    svc(container).delete(plan.id, seed.admin)
    for table in ("packing_planning_items", "packing_planning_manual_units",
                  "packing_planning_manual_contents", "packing_planning_proforma_links"):
        rows = db.query(f"SELECT * FROM {table} WHERE packing_planning_id = ?", (plan.id,))
        assert rows == [], table


def test_list_all_counts_both_halves(container, seed, tiles):
    save(container, seed, prefill_items(container, seed, tiles["pi"]),
         manual_units=[{"unit_no": "50", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"}]}])
    listed = svc(container).list_all(seed.company_id)
    assert len(listed) == 1
    assert listed[0].item_count == 10
    assert listed[0].unit_count == 1


# --------------------------------------------------------------------------
# Packing labels: the stickers that go on the pallets themselves
# --------------------------------------------------------------------------
def labels_of(container, plan):
    return container.packing_planning_repo.labels_for_plan(plan.id)


def test_an_auto_row_of_nine_pallets_is_nine_separate_packings(container, seed, tiles):
    """`pallet_rows` prints "9 PLT / 288 BOX / 1 TO 9" as ONE row; a label is
    needed per physical pallet, so that same row is nine packings of 32."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    arkose = [p for p in plan.packings if p["lines"][0]["batch_number"] == "101" and not p["is_manual"]]
    assert len(arkose) == 9
    assert [p["packing_no"] for p in arkose] == list(range(1, 10))
    assert all(p["lines"][0]["quantity"] == 32 for p in arkose)


def test_a_mixed_manual_pallet_is_one_packing_listing_every_batch_on_it(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]), manual_units=[
        {"unit_no": "54", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"},
                                       {"item_sr_no": "2", "quantity_boxes": "3"}]},
    ])
    mixed = next(p for p in plan.packings if p["packing_no"] == 54)
    assert mixed["is_manual"]
    assert [(l["batch_number"], l["quantity"]) for l in mixed["lines"]] == [("101", 29), ("102", 3)]


def test_every_physical_pallet_gets_exactly_one_label(container, seed, tiles, hardware):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]))
    labels = labels_of(container, plan)
    assert len(labels) == len(plan.packings)
    assert [l.packing_no for l in labels] == [p["packing_no"] for p in plan.packings]


def test_a_packing_id_is_the_document_number_plus_the_pallet_number(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    first = labels_of(container, plan)[0]
    assert first.unique_packing_id == f"{plan.packing_planning_number}0001"
    assert first.unique_packing_id.startswith("PP")


def test_qr_ids_are_unique_across_the_document(container, seed, tiles, hardware):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]))
    qr_ids = [l.unique_qr_id for l in labels_of(container, plan)]
    assert len(set(qr_ids)) == len(qr_ids)
    assert all(q.startswith("UPQR20260830") for q in qr_ids)


def test_editing_the_plan_keeps_the_ids_already_printed(container, seed, tiles):
    """The whole reason these ids are stored rather than derived: a label is
    stuck on a physical pallet, and an edit must not change it underneath."""
    items = prefill_items(container, seed, tiles["pi"])
    plan = save(container, seed, items)
    before = {l.packing_no: l.unique_qr_id for l in labels_of(container, plan)}

    updated = svc(container).update(
        packing_planning_id=plan.id, current_user=seed.admin,
        fields={"packing_planning_date": plan.packing_planning_date, "remarks": "edited"},
        proforma_ids=[], items=[_as_form(i) for i in plan.items], manual_units=[],
    )
    after = {l.packing_no: l.unique_qr_id for l in labels_of(container, updated)}
    assert after == before


def test_a_pallet_that_no_longer_exists_loses_its_label(container, seed, tiles):
    items = prefill_items(container, seed, tiles["pi"])
    plan = save(container, seed, items)
    assert max(l.packing_no for l in labels_of(container, plan)) == 53

    shorter = [dict(_as_form(i)) for i in plan.items]
    shorter[0]["actual_packing"] = "5"        # ARKOSE drops from 9 pallets to 5
    updated = svc(container).update(
        packing_planning_id=plan.id, current_user=seed.admin,
        fields={"packing_planning_date": plan.packing_planning_date},
        proforma_ids=[], items=shorter, manual_units=[],
    )
    assert max(l.packing_no for l in labels_of(container, updated)) == 49
    assert len(labels_of(container, updated)) == len(updated.packings)


def test_deleting_the_document_takes_its_labels_with_it(container, seed, db, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    assert labels_of(container, plan)
    svc(container).delete(plan.id, seed.admin)
    assert db.query("SELECT * FROM packing_planning_labels WHERE packing_planning_id = ?", (plan.id,)) == []


def sheet_of(container, seed, plan, **kw):
    return svc(container).label_sheet(plan.id, seed.company_id, **kw)


def test_alt_qty_is_boxes_times_the_products_alternate_quantity(container, seed, tiles):
    """32 BOX x 1.44 SQM per box = 46.08 SQM, the figure the paper label
    carries beside the quantity."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    first = sheet_of(container, seed, plan, copies=1, per_page=1)["pages"][0][0]
    assert first["lines"][0]["quantity"] == 32
    assert first["lines"][0]["alt_quantity"] == pytest.approx(46.08)
    assert first["lines"][0]["alt_unit"] == "SQM"
    assert first["total_quantity"] == 32
    assert first["total_alt_quantity"] == pytest.approx(46.08)


def test_a_mixed_pallets_label_totals_its_lines(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]), manual_units=[
        {"unit_no": "54", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"},
                                       {"item_sr_no": "2", "quantity_boxes": "3"}]},
    ])
    labels = {l["packing_no"]: l for p in sheet_of(container, seed, plan, copies=1, per_page=1)["pages"] for l in p}
    mixed = labels[54]
    assert len(mixed["lines"]) == 2
    assert mixed["total_quantity"] == 32
    assert mixed["total_alt_quantity"] == pytest.approx(46.08)   # 41.76 + 4.32


def test_the_qr_payload_carries_both_ids_the_pi_and_the_contents(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    label = labels_of(container, plan)[0]
    sheet_label = sheet_of(container, seed, plan, copies=1, per_page=1)["pages"][0][0]
    payload = svc(container)._qr_payload(label, sheet_label["proforma_numbers"], sheet_label["lines"])
    lines = payload.split("\n")
    assert lines[0] == label.unique_qr_id
    assert lines[1] == label.unique_packing_id
    assert lines[2] == "PI20260827001"
    assert lines[3] == "GVT/PGVT 600X1200MM|ARKOSE|101|32 BOX|46.08 SQM"


def test_the_qr_is_sized_from_its_module_count_so_it_stays_scannable(container, seed, tiles):
    """A code dropped into a fixed box gets denser as the payload grows; a
    three-line mixed pallet would end up unreadable. Every code is emitted at
    the same millimetres-per-module instead."""
    import re
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]), manual_units=[
        {"unit_no": "54", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"},
                                       {"item_sr_no": "2", "quantity_boxes": "3"},
                                       {"item_sr_no": "3", "quantity_boxes": "21"}]},
    ])
    labels = {l["packing_no"]: l for p in sheet_of(container, seed, plan, copies=1, per_page=1)["pages"] for l in p}

    def mm(label):
        return float(re.search(r'width="([\d.]+)mm"', label["qr_svg"]).group(1))

    assert mm(labels[54]) > mm(labels[1])     # denser payload, physically bigger code
    assert mm(labels[1]) >= 20                # and none of them printed tiny


def test_copies_and_per_page_are_clamped_to_1_2_or_4(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    packings = len(plan.packings)

    default = sheet_of(container, seed, plan)
    assert (default["copies"], default["per_page"]) == (4, 2)
    assert default["printed_count"] == packings * 4
    assert len(default["pages"]) == packings * 4 / 2

    for bad in ("3", "0", "", None, "abc", "-1", "99"):
        fallback = sheet_of(container, seed, plan, copies=bad, per_page=bad)
        assert (fallback["copies"], fallback["per_page"]) == (4, 2), bad

    four_up = sheet_of(container, seed, plan, copies=4, per_page=4)
    assert len(four_up["pages"]) == packings
    one_up = sheet_of(container, seed, plan, copies=1, per_page=1)
    assert len(one_up["pages"]) == packings


def test_4x6_label_size_forces_one_label_per_page(container, seed, tiles):
    """A label printer has no concept of tiling several stickers onto one
    sheet - picking the 4x6 size overrides whatever per_page was asked for."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    packings = len(plan.packings)

    sheet = sheet_of(container, seed, plan, label_size="4x6", copies=2, per_page=4)
    assert sheet["label_size"] == "4x6"
    assert sheet["per_page"] == 1
    assert len(sheet["pages"]) == packings * 2

    default = sheet_of(container, seed, plan)
    assert default["label_size"] == "a4"

    for bad in ("letter", "", None, "roll"):
        fallback = sheet_of(container, seed, plan, label_size=bad)
        assert fallback["label_size"] == "a4", bad

    assert sheet_of(container, seed, plan, label_size="4X6")["label_size"] == "4x6"


def test_4x4_label_size_also_forces_one_label_per_page(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    packings = len(plan.packings)

    sheet = sheet_of(container, seed, plan, label_size="4x4", copies=2, per_page=4)
    assert sheet["label_size"] == "4x4"
    assert sheet["per_page"] == 1
    assert len(sheet["pages"]) == packings * 2


def test_every_copy_of_a_packing_carries_the_same_ids(container, seed, tiles):
    """Four labels for four sides of one pallet - the same pallet, so the
    same ids on every one of them."""
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    sheet = sheet_of(container, seed, plan, copies=4, per_page=1)
    first_four = [page[0] for page in sheet["pages"][:4]]
    assert len({l["unique_qr_id"] for l in first_four}) == 1
    assert len({l["unique_packing_id"] for l in first_four}) == 1


def test_another_companys_labels_are_a_404(container, seed, tiles):
    plan = save(container, seed, prefill_items(container, seed, tiles["pi"]))
    other = container.tenant_repo.create("OTHER CO", "other")
    with pytest.raises(NotFoundError):
        svc(container).label_sheet(plan.id, other.id)


def test_a_duplicated_packing_number_still_saves_and_labels(container, seed, tiles):
    """A hand-pinned number can collide with a range already handed out, and
    this document warns about that rather than refusing the save - so
    minting labels must not be the thing that blows up instead. One number
    is one label; the warning is what tells the operator to fix it."""
    items = prefill_items(container, seed, tiles["pi"])
    plan = save(container, seed, items, manual_units=[
        {"unit_no": "50", "contents": [{"item_sr_no": "1", "quantity_boxes": "29"}]},
    ])
    assert 50 in plan.duplicate_packing_numbers
    numbers = [l.packing_no for l in labels_of(container, plan)]
    assert numbers == sorted(set(numbers))            # no duplicate label rows
    assert numbers.count(50) == 1
    assert any("used more than once" in w for w in svc(container).packing_warnings(plan))


# --------------------------------------------------------------------------
# Loading products from JOB INS: PI -> job work -> its purchase invoice ->
# job out -> job in. A second, parallel source alongside purchase orders.
# --------------------------------------------------------------------------
def _job_in_chain(container, seed, *, boxes_by_design):
    """The whole chain, planted the way test_services_export_invoices does:
    one proforma invoice, a job work raised off it, that job work's own
    purchase invoice (linked), a job out on that invoice, and one job in
    carrying the given designs. Returns (pi, product, job_in)."""
    pallet = [{"name": "Pallet", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    product = make_product(container, seed, "GVT/PGVT 600X1200MM JOB", "69072100", 27.0, "BOX", "1.44", pallet)
    designs = {name: make_design(container, seed, product, name) for name in boxes_by_design}

    pi = container.proforma_invoice_service.create(
        seed.admin,
        {"consignee_name": "ROBUST INTERNATIONAL LIMITADA", "invoice_date": "2026-08-27",
         "invoice_number": "PIJOB20260827", "currency_code": "USD"},
        [{"product_id": str(product.id), "product_name": product.product_name, "hsn_code": "69072100",
          "quantity_boxes": str(sum(boxes_by_design.values())), "quantity_unit": "BOX",
          "quantity_value": "1", "unit": "SQM", "price_usd": "5"}],
    )

    job_work_id = container.db.execute(
        "INSERT INTO job_works (company_id, job_work_number, job_work_date, seller_name, "
        "created_by, proforma_invoice_id) VALUES (?, ?, ?, ?, ?, ?)",
        (seed.company_id, "JW20260827001", "2026-08-27", "ALIVE GRANITO LLP", seed.admin.id, pi.id))

    jw_pinv = container.purchase_invoice_service.create(
        seed.admin,
        {"seller_name": "ALIVE GRANITO LLP", "invoice_number": "JW/PINV/1", "invoice_date": "2026-08-28",
         "seller_gstin": "24ABVFA1170D1ZO", "currency_code": "INR"},
        [{"product_name": product.product_name, "product_id": str(product.id), "quantity_value": "144",
          "price_inr": "400", "price_per": "BOX", "quantity_boxes": "100"}], [])
    container.db.execute(
        "INSERT INTO purchase_invoice_job_work_links (purchase_invoice_id, job_work_id) VALUES (?, ?)",
        (jw_pinv.id, job_work_id))

    job_out = container.job_out_service.create(current_user=seed.admin, fields={
        "purchase_invoice_id": str(jw_pinv.id),
        "delivery_challan_no": "DC/OUT/1", "delivery_challan_date": "2026-08-29"})

    job_in = container.job_in_service.create(current_user=seed.admin, fields={
        "job_out_id": str(job_out.id),
        "stock_inward_no": "STINW-1", "stock_inward_date": "2026-08-30",
    }, raw_items=[{
        "product_id": str(product.id), "product_name": product.product_name,
        "design_id": str(designs[name].id), "design_name": name, "quantity_boxes": str(b),
    } for name, b in boxes_by_design.items()])
    return pi, product, job_in


def test_job_ins_are_listed_for_the_ticked_proformas(container, seed):
    pi, _product, job_in = _job_in_chain(container, seed, boxes_by_design={"ARKOSE": 300, "ATLANTA": 100})
    rows = svc(container).job_ins_for_proformas([pi.id], seed.company_id)
    assert [r["id"] for r in rows] == [job_in.id]
    assert rows[0]["stock_inward_no"] == "STINW-1"
    assert rows[0]["line_count"] == 2
    assert rows[0]["ready_totals"] == {"BOX": 400}


def test_job_in_prefill_is_one_auto_filled_row_per_design_with_no_batch(container, seed):
    """The whole point of the job-in path: returned designs pack the same
    way PO batches do, but a return has no batch number or manf. date."""
    _pi, _product, job_in = _job_in_chain(container, seed, boxes_by_design={"ARKOSE": 300, "ATLANTA": 100})
    items = svc(container).build_prefill_from_job_ins([job_in.id], seed.company_id)["items"]
    assert len(items) == 2
    for it in items:
        assert it["batch_number"] is None
        assert it["production_date"] is None
        assert it["job_in_id"] == job_in.id
        assert it["purchase_order_id"] is None
        assert it["boxes_per_unit"] == 32          # auto-fill found the pallet type
    arkose = next(i for i in items if i["design_name"] == "ARKOSE")
    assert arkose["ready_quantity"] == 300
    assert arkose["actual_packing"] == 9           # 300 // 32


def test_job_in_rows_persist_with_their_provenance_and_no_batch_error(container, seed):
    _pi, _product, job_in = _job_in_chain(container, seed, boxes_by_design={"ARKOSE": 300})
    items = svc(container)._clean_items(
        svc(container).build_prefill_from_job_ins([job_in.id], seed.company_id)["items"])
    plan = save(container, seed, items)
    reloaded = svc(container).get(plan.id, seed.company_id)
    assert [i.job_in_id for i in reloaded.items] == [job_in.id]
    assert reloaded.items[0].batch_number is None


def test_another_companys_job_ins_are_never_reachable(container, seed):
    pi, _product, job_in = _job_in_chain(container, seed, boxes_by_design={"ARKOSE": 300})
    other = container.tenant_repo.create("OTHER CO", "other")
    assert svc(container).job_ins_for_proformas([pi.id], other.id) == []
    assert svc(container).build_prefill_from_job_ins([job_in.id], other.id)["items"] == []


# --------------------------------------------------------------------------
# The unit each product was planned in - what the export invoice prints
# --------------------------------------------------------------------------
def test_packing_unit_labels_report_the_planned_unit_per_product(container, seed, tiles, hardware):
    """Packing Planning decides PLT or CTN; the export invoice's printed
    Packing column follows it. Only products behind the asked-for PIs come
    back, each with the capacity it was planned at."""
    save(container, seed, prefill_items(container, seed, tiles["pi"], hardware["pi"]))

    labels = svc(container).packing_unit_labels(seed.company_id, [hardware["pi"].id])

    assert set(labels) == {hardware["rod"].id, hardware["dish"].id}
    assert labels[hardware["rod"].id] == [{"label": "CTN", "boxes_per_unit": 30}]


def test_packing_unit_labels_are_company_scoped_and_empty_without_proformas(container, seed, hardware):
    save(container, seed, prefill_items(container, seed, hardware["pi"]))

    assert svc(container).packing_unit_labels(seed.company_id + 999, [hardware["pi"].id]) == {}
    assert svc(container).packing_unit_labels(seed.company_id, []) == {}
