"""
Tests for LoadingPlanningService (app/services.py) - the LOADING PLANNING
document, which works out which goods physically go in which container.

The behaviours worth pinning down are the ones the document exists for, and
they are checked against the two real orders that motivated it:

  - goods arrive at DESIGN level, by tracing PI -> purchase orders -> those
    orders' PACKING LISTS. A PO orders 1268 boxes of a product; only its
    packing list knows those are four designs of 317. (A PO with no packing
    list falls back to its own product lines.)

  - `pallets` stops being a decimal. 317 boxes at 32/pallet is nine full
    pallets plus one holding 29 - not 9.91 - and auto-build FLAGS that part
    pallet rather than quietly merging it.

  - one weight rule covers both packing shapes, because the carton level is
    optional:
        pallet gross = contents net + carton tare + pallet tare
    Tiles: (32 x 27) + 0 + 20 = 884kg. Hardware: 44.325 + (3 x 0.3) + 20 =
    65.225kg.

  - a mixed carton is possible at all. 45 + 45 PCS at 30/CTN auto-builds to
    four cartons; the operator merges the two part-cartons into one holding
    15 + 15, giving three cartons on one pallet. No rule can make that call,
    which is why the packing is manual.

  - nothing here BLOCKS a save. Unpacked goods and an over-weight container
    are reported as warnings, because a plan is built over several sittings.
"""

import pytest

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


# --------------------------------------------------------------------------
# Fixture data: the two real orders, rebuilt from scratch.
# --------------------------------------------------------------------------
TILE_DESIGNS = ["ARKOSE", "ATLANTA LIGHT GREY", "ARTISTIC BEIGE", "BELLY WOOD BROWN"]


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


def make_chain(container, seed, *, pi_items, po_items, pl_items, pi_number, po_number, pl_number):
    """One proforma invoice -> one purchase order -> one packing list, which
    is the exact shape build_prefill_from_proformas walks."""
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
    pl = None
    if pl_items:
        pl = container.packing_list_service.create(
            seed.admin,
            {"packing_list_date": "2026-08-27", "packing_list_number": pl_number,
             "purchase_order_id": str(po.id)},
            pl_items,
        )
    return pi, po, pl


@pytest.fixture
def tiles(container, seed):
    """PO20260827001: three tile products, 27kg/box, 32 boxes to a 20kg
    pallet. The purchase order buys 1268 + 310 + 310 boxes at product level;
    the packing list is what splits the 1268 into four designs of 317."""
    pallet = [{"name": "Pallet", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    khatli = [{"name": "JUNGLE KHATLI", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    base = make_product(container, seed, "GVT/PGVT 600X1200MM", "69072100", 27.0, "BOX", "1.44", pallet)
    hg = make_product(container, seed, "GVT/PGVT 600X1200MM HG", "69072100", 27.0, "BOX", "1.44", khatli)
    carving = make_product(container, seed, "GVT/PGVT 600X1200MM CARVING", "69072100", 27.0, "BOX", "1.44", khatli)

    designs = {d: make_design(container, seed, base, d) for d in TILE_DESIGNS}
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

    def pl_item(product, design, boxes):
        return {"product_id": str(product.id), "product_name": product.product_name,
                "design_id": str(design.id), "design_name": design.design_name,
                "hsn_code": "69072100", "quantity_boxes": str(boxes), "quantity_unit": "BOX",
                "quantity_value": str(boxes * 1.44), "unit": "SQM"}

    pi, po, pl = make_chain(
        container, seed,
        pi_items=[pi_item(base, 1268, 5.5), pi_item(hg, 310, 7.5), pi_item(carving, 310, 8.5)],
        po_items=[po_item(base, 1268, 418.5), po_item(hg, 310, 496), po_item(carving, 310, 527)],
        pl_items=([pl_item(base, designs[d], 317) for d in TILE_DESIGNS]
                  + [pl_item(hg, celeste, 310), pl_item(carving, morena, 310)]),
        pi_number="PI20260827001", po_number="PO20260827001", pl_number="PL20260827008",
    )
    return {"pi": pi, "po": po, "pl": pl, "base": base, "hg": hg, "carving": carving}


@pytest.fixture
def hardware(container, seed):
    """PO20260827002: two hardware products, 45 PCS each, packed 30 to a CTN
    weighing 0.3kg. The CTN is a CARTON, not a pallet - which is the whole
    reason unit_kind exists."""
    ctn = [{"name": "CTN", "boxes_per_pallet": "30", "weight_kg": "0.3", "unit_kind": "carton"}]
    rod = make_product(container, seed, "904 TOWEL ROD", "73269030", 0.56, "PCS", "1", ctn)
    dish = make_product(container, seed, "910 DOUBLE SOAP DISH", "73269030", 0.425, "PCS", "1", ctn)
    d904 = make_design(container, seed, rod, "904")
    d910 = make_design(container, seed, dish, "910")

    def line(product, price_key, price):
        return {"product_id": str(product.id), "product_name": product.product_name,
                "hsn_code": "73269030", "quantity_boxes": "45", "quantity_unit": "PCS",
                "quantity_value": "45", "unit": "PCS", price_key: str(price)}

    pi, po, pl = make_chain(
        container, seed,
        pi_items=[line(rod, "price_usd", 12), line(dish, "price_usd", 15)],
        po_items=[dict(line(rod, "price_inr", 1150), price_per="PCS"),
                  dict(line(dish, "price_inr", 1175), price_per="PCS")],
        pl_items=[{"product_id": str(rod.id), "product_name": rod.product_name,
                   "design_id": str(d904.id), "design_name": "904", "hsn_code": "73269030",
                   "quantity_boxes": "45", "quantity_unit": "PCS", "quantity_value": "45", "unit": "PCS"},
                  {"product_id": str(dish.id), "product_name": dish.product_name,
                   "design_id": str(d910.id), "design_name": "910", "hsn_code": "73269030",
                   "quantity_boxes": "45", "quantity_unit": "PCS", "quantity_value": "45", "unit": "PCS"}],
        pi_number="PI20260827002", po_number="PO20260827002", pl_number="PL20260827007",
    )
    return {"pi": pi, "po": po, "rod": rod, "dish": dish}


@pytest.fixture
def booking(container, seed):
    """The two 20FT containers from booking EBKG1652237584: tare 2100kg,
    max permitted 38400kg."""
    buyer = container.buyer_service.create(
        seed.admin,
        {"company_name": "ROBUST INTERNATIONAL LIMITADA", "phone": "1", "email": "a@example.com"},
        [{"name": "A", "is_primary": True}],
    )
    return container.booking_detail_service.create(
        seed.admin,
        {"buyer_id": str(buyer.id), "booking_no": "EBKG1652237584", "vessel_name": "MSC KETRINA",
         "voyage_no": "1345645", "transporter_name": "FORTUNE SHIPPING PVT LTD"},
        [{"container_type": "20FT FCL", "container_count": "2"}],
        [{"container_type": "20FT FCL", "container_no": "DFSU2889215", "max_permitted_weight": "38400",
          "tare_weight_kg": "2100", "vehicle_no": "GJ39X2361", "lr_no": "LR00102",
          "line_seal_no": "IN1955841", "rfid_seal_no": "WIND03022679"},
         {"container_type": "20FT FCL", "container_no": "DFSU2889216", "max_permitted_weight": "38400",
          "tare_weight_kg": "2100", "vehicle_no": "GJ39X3251", "lr_no": "LR00103",
          "line_seal_no": "IN1955842", "rfid_seal_no": "WIND03022680"}],
    )


def svc(container):
    return container.loading_planning_service


def prefill_items(container, seed, pi):
    service = svc(container)
    return service._clean_items(service.build_prefill_from_proformas([pi.id], seed.company_id)["items"])


# --------------------------------------------------------------------------
# Loading goods: PI -> purchase orders -> their packing lists
# --------------------------------------------------------------------------
def test_prefill_explodes_purchase_order_lines_into_designs(container, seed, tiles):
    """The PO has three product lines; the plan gets six DESIGN lines, because
    the design split only exists on the PO's packing list."""
    result = svc(container).build_prefill_from_proformas([tiles["pi"].id], seed.company_id)
    items = result["items"]

    assert len(items) == 6
    assert [i["design_name"] for i in items] == TILE_DESIGNS + ["CELESTE BLUE", "MORENA MARFIL"]
    assert [i["quantity_boxes"] for i in items] == [317, 317, 317, 317, 310, 310]
    assert sum(i["quantity_boxes"] for i in items) == 1888
    # Provenance survives the explode, so a line still says which PO bought it.
    assert {i["po_number"] for i in items} == {"PO20260827001"}


def test_prefill_prices_each_line_at_the_pis_own_usd_rate(container, seed, tiles):
    """Rates are matched by product_id, so all four designs of one product
    carry that product's quoted price."""
    items = svc(container).build_prefill_from_proformas([tiles["pi"].id], seed.company_id)["items"]
    by_design = {i["design_name"]: i for i in items}

    assert [by_design[d]["price_usd"] for d in TILE_DESIGNS] == [5.5, 5.5, 5.5, 5.5]
    assert by_design["CELESTE BLUE"]["price_usd"] == 7.5
    assert by_design["MORENA MARFIL"]["price_usd"] == 8.5
    assert by_design["ARKOSE"]["total_usd"] == pytest.approx(317 * 5.5)


def test_prefill_carries_the_per_unit_net_weight_not_the_line_total(container, seed, tiles):
    """Per box, because a line gets split across cartons and pallets in
    quantities nobody knows at load time."""
    items = svc(container).build_prefill_from_proformas([tiles["pi"].id], seed.company_id)["items"]

    assert {i["net_weight_kg"] for i in items} == {27.0}
    assert sum(i["net_weight_kg"] * i["quantity_boxes"] for i in items) == pytest.approx(50976.0)


def test_prefill_falls_back_to_product_lines_when_a_po_has_no_packing_list(container, seed):
    """Still loadable, just coarser - the lines come through with no design."""
    pallet = [{"name": "Pallet", "boxes_per_pallet": "32", "weight_kg": "20", "unit_kind": "pallet"}]
    product = make_product(container, seed, "GVT PLAIN", "69072100", 27.0, "BOX", "1.44", pallet)
    line = {"product_id": str(product.id), "product_name": product.product_name,
            "hsn_code": "69072100", "quantity_boxes": "500", "quantity_unit": "BOX",
            "quantity_value": "720", "unit": "SQM"}
    pi, po, _ = make_chain(
        container, seed,
        pi_items=[dict(line, price_usd="6")],
        po_items=[dict(line, price_inr="400", price_per="BOX")],
        pl_items=None, pi_number="PI-NOPL", po_number="PO-NOPL", pl_number=None,
    )

    items = svc(container).build_prefill_from_proformas([pi.id], seed.company_id)["items"]

    assert len(items) == 1
    assert items[0]["design_name"] is None
    assert items[0]["quantity_boxes"] == 500
    assert items[0]["price_usd"] == 6


def test_prefill_separates_carton_packing_types_from_pallet_ones(container, seed, tiles, hardware):
    """The carton and pallet pickers are fed from the same table, split on
    unit_kind - a CTN must never be offered as a pallet."""
    tile_types = svc(container).build_prefill_from_proformas(
        [tiles["pi"].id], seed.company_id)["packing_types"]
    hw_types = svc(container).build_prefill_from_proformas(
        [hardware["pi"].id], seed.company_id)["packing_types"]

    assert tile_types[str(tiles["base"].id)]["carton"] == []
    assert tile_types[str(tiles["base"].id)]["pallet"][0]["name"] == "Pallet"
    assert hw_types[str(hardware["rod"].id)]["pallet"] == []
    assert hw_types[str(hardware["rod"].id)]["carton"][0]["name"] == "CTN"


def test_prefill_ignores_another_companys_proforma_invoice(container, seed, tiles):
    other = container.tenant_repo.create("Other Co", "other-co")

    assert svc(container).build_prefill_from_proformas([tiles["pi"].id], other.id)["items"] == []


# --------------------------------------------------------------------------
# Auto-build: whole units, then ONE flagged part-unit
# --------------------------------------------------------------------------
def test_auto_build_turns_9_91_pallets_into_9_full_plus_1_holding_29(container, seed, tiles):
    """The defect this document exists to fix. 317 / 32 is 9.906 - which is
    not a shippable quantity - so it becomes 10 real pallets."""
    items = prefill_items(container, seed, tiles["pi"])
    built = svc(container).auto_build_packing(seed.company_id, items)

    assert built["cartons"] == []          # tiles sit straight on the pallet
    assert len(built["pallets"]) == 60     # NOT 59.02

    arkose = [p for p in built["pallets"]
              if p["contents"][0]["item_sr_no"] == 1]
    loads = sorted(p["contents"][0]["quantity_boxes"] for p in arkose)
    assert loads == [29] + [32] * 9
    assert sum(loads) == 317


def test_auto_build_flags_the_part_pallets_rather_than_merging_them(container, seed, tiles):
    """Merging is the judgement call the operator makes, so auto-build must
    leave it alone - all six remainders stay separate and flagged."""
    items = prefill_items(container, seed, tiles["pi"])
    built = svc(container).auto_build_packing(seed.company_id, items)
    pallets = svc(container)._clean_pallets(built["pallets"])

    part = [p for p in pallets if p.is_part_filled]
    assert len(part) == 6
    assert sorted(p.direct_boxes for p in part) == [22, 22, 29, 29, 29, 29]
    assert len([p for p in pallets if not p.is_part_filled]) == 54


def test_auto_build_puts_hardware_through_cartons_and_tiles_straight_on_pallets(container, seed, hardware):
    """The carton level is optional, and which shape applies comes from the
    product's own packing types."""
    items = prefill_items(container, seed, hardware["pi"])
    built = svc(container).auto_build_packing(seed.company_id, items)

    assert len(built["cartons"]) == 4      # 30 + 15 for each of two products
    loads = sorted(c["contents"][0]["quantity_boxes"] for c in built["cartons"])
    assert loads == [15, 15, 30, 30]
    # every carton lands on a pallet rather than floating
    assert all(c["pallet_no"] for c in built["cartons"])


# --------------------------------------------------------------------------
# The weight rule: gross = contents net + carton tare + pallet tare
# --------------------------------------------------------------------------
def test_tile_pallet_weighs_boxes_plus_pallet_with_no_carton_in_between(container, seed, tiles, booking):
    """(32 x 27) + 0 + 20 = 884kg for a full pallet; the part pallet holding
    29 comes to 803."""
    plan = build_plan(container, seed, tiles["pi"], booking, assign=False)
    by_sr = plan.items_by_sr

    full = [p for p in plan.pallets if p.direct_boxes == 32][0]
    part = [p for p in plan.pallets if p.direct_boxes == 29][0]

    assert full.carton_tare_kg == 0
    assert full.net_weight_kg(by_sr) == pytest.approx(864.0)
    assert full.gross_weight_kg(by_sr) == pytest.approx(884.0)
    assert part.gross_weight_kg(by_sr) == pytest.approx(803.0)


def test_whole_tile_order_grosses_52176_kg(container, seed, tiles, booking):
    """1888 boxes at 27kg is 50,976 net; 60 pallets at 20kg add 1,200."""
    plan = build_plan(container, seed, tiles["pi"], booking, assign=False)
    by_sr = plan.items_by_sr

    assert plan.total_net_weight_kg == pytest.approx(50976.0)
    assert sum(p.tare_weight_kg for p in plan.pallets) == pytest.approx(1200.0)
    assert sum(p.gross_weight_kg(by_sr) for p in plan.pallets) == pytest.approx(52176.0)


def test_a_mixed_carton_turns_four_cartons_into_three_on_one_pallet(container, seed, hardware, booking):
    """The move no rule can make: the two part-cartons of 15 become one
    carton holding 15 of each, and all three ride one pallet.

        44.325 net + (3 x 0.3) carton tare + 20 pallet tare = 65.225kg
    """
    items = prefill_items(container, seed, hardware["pi"])
    cartons = [
        {"carton_no": 1, "carton_type_name": "CTN", "capacity_boxes": 30, "tare_weight_kg": 0.3,
         "pallet_no": 1, "contents": [{"item_sr_no": 1, "quantity_boxes": 30}]},
        {"carton_no": 2, "carton_type_name": "CTN", "capacity_boxes": 30, "tare_weight_kg": 0.3,
         "pallet_no": 1, "contents": [{"item_sr_no": 2, "quantity_boxes": 30}]},
        {"carton_no": 3, "carton_type_name": "CTN", "capacity_boxes": 30, "tare_weight_kg": 0.3,
         "pallet_no": 1, "contents": [{"item_sr_no": 1, "quantity_boxes": 15},
                                      {"item_sr_no": 2, "quantity_boxes": 15}]},
    ]
    pallets = [{"pallet_no": 1, "pallet_type_name": "Pallet", "capacity_boxes": None,
                "tare_weight_kg": 20.0, "container_sr_no": 1, "contents": []}]
    plan = save_plan(container, seed, hardware["pi"], booking, items, cartons, pallets,
                     number_date="2026-08-27")

    assert len(plan.cartons) == 3
    assert len(plan.pallets) == 1
    pallet = plan.pallets[0]
    by_sr = plan.items_by_sr

    assert pallet.packed_boxes == 90
    assert pallet.net_weight_kg(by_sr) == pytest.approx(44.325)
    assert pallet.carton_tare_kg == pytest.approx(0.9)
    assert pallet.gross_weight_kg(by_sr) == pytest.approx(65.225)
    assert plan.is_fully_packed


def test_a_carton_may_hold_more_than_one_product(container, seed, hardware, booking):
    """The mixed carton has to be representable at all - both goods lines
    count as packed from the same carton."""
    items = prefill_items(container, seed, hardware["pi"])
    cartons = [{"carton_no": 1, "carton_type_name": "CTN", "capacity_boxes": 30, "tare_weight_kg": 0.3,
                "pallet_no": None,
                "contents": [{"item_sr_no": 1, "quantity_boxes": 15},
                             {"item_sr_no": 2, "quantity_boxes": 15}]}]
    plan = save_plan(container, seed, hardware["pi"], booking, items, cartons, [])

    balances = {b["sr_no"]: b for b in plan.line_balances}
    assert balances[1]["packed"] == 15
    assert balances[2]["packed"] == 15


# --------------------------------------------------------------------------
# Containers: VGM, and the fact that none of it blocks a save
# --------------------------------------------------------------------------
def test_container_vgm_is_pallet_gross_plus_the_containers_own_tare(container, seed, tiles, booking):
    plan = build_plan(container, seed, tiles["pi"], booking, assign=True)
    rows = [r for r in plan.container_summary if r["sr_no"]]

    assert len(rows) == 2
    for row in rows:
        assert row["pallet_count"] == 30
        assert row["container_tare_kg"] == 2100
        assert row["vgm_kg"] == pytest.approx(row["cargo_weight_kg"] + 2100)
        assert row["max_permitted_weight"] == 38400
        assert not row["over_weight"]
        assert row["headroom_kg"] > 0
    assert sum(r["cargo_weight_kg"] for r in rows) == pytest.approx(52176.0)


def test_an_over_weight_container_warns_and_still_saves(container, seed, tiles, booking):
    """A warning, never a refusal - unlike the export packing list's own
    container split, which hard-enforces its equivalent invariant."""
    plan = build_plan(container, seed, tiles["pi"], booking, assign="all-in-one")

    warnings = svc(container).packing_warnings(plan)
    assert any("over the" in w for w in warnings)
    assert plan.container_summary[0]["over_weight"]
    # It is on disk regardless.
    assert svc(container).get(plan.id, seed.company_id).id == plan.id


def test_unpacked_goods_warn_and_still_save(container, seed, tiles, booking):
    """A half-built plan is a normal intermediate state, not an error."""
    items = prefill_items(container, seed, tiles["pi"])
    # Only the first design gets a pallet.
    pallets = [{"pallet_no": 1, "pallet_type_name": "Pallet", "capacity_boxes": 32,
                "tare_weight_kg": 20.0, "container_sr_no": None,
                "contents": [{"item_sr_no": 1, "quantity_boxes": 32}]}]
    plan = save_plan(container, seed, tiles["pi"], booking, items, [], pallets)

    assert not plan.is_fully_packed
    warnings = svc(container).packing_warnings(plan)
    assert any("still to be packed" in w for w in warnings)
    assert any("not yet assigned to a container" in w for w in warnings)


def test_pallets_with_no_container_are_reported_as_unassigned(container, seed, tiles, booking):
    plan = build_plan(container, seed, tiles["pi"], booking, assign=False)
    unassigned = [r for r in plan.container_summary if r["sr_no"] is None]

    assert len(unassigned) == 1
    assert unassigned[0]["pallet_count"] == 60
    assert unassigned[0]["container_no"] == "Unassigned"


# --------------------------------------------------------------------------
# Round-trip, numbering, scoping
# --------------------------------------------------------------------------
def test_plan_round_trips_through_save_and_reload(container, seed, tiles, booking):
    plan = build_plan(container, seed, tiles["pi"], booking, assign=True)
    reloaded = svc(container).get(plan.id, seed.company_id)

    assert len(reloaded.items) == 6
    assert len(reloaded.pallets) == 60
    assert len(reloaded.containers) == 2
    assert reloaded.proforma_invoice_ids == [tiles["pi"].id]
    assert reloaded.booking_no == "EBKG1652237584"
    assert reloaded.is_fully_packed
    # Cartons hang off their pallet after a reload, so the weight rule works.
    assert all(p.cartons == [] for p in reloaded.pallets)


def test_number_follows_the_day_scoped_sequence(container, seed, tiles, booking):
    first = build_plan(container, seed, tiles["pi"], booking, assign=False)
    second = save_plan(container, seed, tiles["pi"], booking, [], [], [])

    assert first.loading_planning_number == "LP20260827001"
    assert second.loading_planning_number == "LP20260827002"


def test_a_date_is_required(container, seed):
    with pytest.raises(ValidationError):
        svc(container).create(seed.admin, {"loading_planning_date": ""}, [], [], [], [], [])


def test_another_companys_plan_is_a_404_not_a_403(container, seed, tiles, booking):
    plan = build_plan(container, seed, tiles["pi"], booking, assign=False)
    other = container.tenant_repo.create("Other Co", "other-co")

    with pytest.raises(NotFoundError):
        svc(container).get(plan.id, other.id)


def test_only_an_admin_can_delete(container, seed, tiles, booking):
    plan = build_plan(container, seed, tiles["pi"], booking, assign=False)

    with pytest.raises(PermissionDeniedError):
        svc(container).delete(plan.id, seed.employee)

    svc(container).delete(plan.id, seed.admin)
    with pytest.raises(NotFoundError):
        svc(container).get(plan.id, seed.company_id)


def test_booking_snapshot_copies_every_container_column(container, seed, booking):
    """A copy, not a live link - the two tables already share every column."""
    snap = svc(container).booking_snapshot(booking.id, seed.company_id)

    assert snap["booking_no"] == "EBKG1652237584"
    assert [c["container_no"] for c in snap["containers"]] == ["DFSU2889215", "DFSU2889216"]
    first = snap["containers"][0]
    assert first["tare_weight_kg"] == 2100
    assert first["max_permitted_weight"] == "38400"
    assert first["rfid_seal_no"] == "WIND03022679"
    # The transporter is booking-level, stamped onto every row.
    assert first["transporter_name"] == "FORTUNE SHIPPING PVT LTD"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def save_plan(container, seed, pi, booking, items, cartons, pallets, number_date="2026-08-27"):
    service = svc(container)
    snap = service.booking_snapshot(booking.id, seed.company_id)
    raw_items = [{
        "proforma_invoice_id": i.proforma_invoice_id, "purchase_order_id": i.purchase_order_id,
        "po_number": i.po_number, "product_id": i.product_id, "product_name": i.product_name,
        "design_id": i.design_id, "design_name": i.design_name, "hsn_code": i.hsn_code,
        "quantity_boxes": i.quantity_boxes, "quantity_unit": i.quantity_unit,
        "quantity_value": i.quantity_value, "unit": i.unit, "net_weight_kg": i.net_weight_kg,
        "price_usd": i.price_usd,
    } for i in items]
    return service.create(
        seed.admin,
        {"loading_planning_date": number_date, "booking_detail_id": str(booking.id),
         "booking_no": snap["booking_no"], "vessel_name": snap["vessel_name"],
         "voyage_no": snap["voyage_no"], "transporter_name": snap["transporter_name"]},
        [pi.id], raw_items, snap["containers"], cartons, pallets,
    )


def build_plan(container, seed, pi, booking, assign):
    """Load goods, auto-build the packing, optionally spread the pallets over
    the booking's containers, then save."""
    service = svc(container)
    items = prefill_items(container, seed, pi)
    built = service.auto_build_packing(seed.company_id, items)
    pallets = built["pallets"]
    if assign == "all-in-one":
        for p in pallets:
            p["container_sr_no"] = 1
    elif assign:
        half = len(pallets) // 2
        for i, p in enumerate(pallets):
            p["container_sr_no"] = 1 if i < half else 2
    return save_plan(container, seed, pi, booking, items, built["cartons"], pallets)
