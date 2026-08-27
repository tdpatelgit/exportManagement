"""
tests/test_services_inventory.py
--------------------------------
Stock arithmetic - previously untested territory.

Stock per design is RECEIVED (a purchase invoice's own packing list) less
DISPATCHED (received goods belonging to an invoice that has a Job Out challan
against it) less SOLD (allocated on an export invoice's Designs Packing List).
These tests pin down which document is the receipt event, since that is the
part most easily broken by accident: a purchase order's or a job work's own
packing list normally lists the SAME goods as the invoice raised from it, so
counting either of those as well would double every purchase.

Packing lists are built through the repository rather than the service so a
test can plant one of a specific origin (purchase order / job work / purchase
invoice) without dragging in the whole proforma -> PO -> invoice pipeline.
Foreign keys are ON, so every parent row is real.
"""

import pytest

from app.models import PackingList, PackingListItem


# --------------------------------------------------------------------------
# Fixtures: one product, one design, one supplier-less purchase invoice
# --------------------------------------------------------------------------
@pytest.fixture
def catalog(container, seed):
    """A product with one design, and the design's id."""
    product = container.product_service.create_product(
        current_user=seed.admin, product_name="GVT 600X1200", description="",
        hsn_code="69072100", igst_percent="18", quantity="", alternate_quantity="1.44",
        quantity_unit="BOX", alternate_quantity_unit="SQM",
    )
    design = container.product_service.create_design(
        current_user=seed.admin, product_id=product.id, folder_id=None,
        design_name="ATLANTA LIGHT GREY", description="", price_usd="",
        alt_text="", photo_file=None, dimension_photo_file=None,
    )
    return product, design


@pytest.fixture
def purchase_invoice(container, seed, catalog):
    """A purchase invoice carrying the catalog product."""
    product, _ = catalog
    return container.purchase_invoice_service.create(
        current_user=seed.admin,
        fields={
            "invoice_number": "GST/1", "invoice_date": "2026-04-22",
            "seller_name": "ALIVE GRANITO LLP", "purchase_type": "full_tax",
            "currency_code": "INR",
        },
        raw_items=[{
            "product_id": str(product.id), "product_name": product.product_name,
            "hsn_code": "69072100", "quantity_boxes": "100", "quantity_value": "144",
            "unit": "SQM", "price_inr": "400", "price_per": "BOX",
        }],
        raw_vehicle_numbers=[],
    )


def _plant_packing_list(container, seed, catalog, number, boxes, **origin):
    """One packing list of a given origin carrying `boxes` of the design.
    `origin` is exactly one of purchase_invoice_id / purchase_order_id /
    job_work_id."""
    product, design = catalog
    packing_list = PackingList(
        id=None, company_id=seed.company_id, packing_list_number=number,
        packing_list_date="2026-04-22", consignee_name="ANY CONSIGNEE",
        created_by=seed.admin.id, **origin,
    )
    packing_list.items = [PackingListItem(
        id=None, packing_list_id=None, sr_no=1, product_id=product.id,
        product_name=product.product_name, design_id=design.id,
        design_name=design.design_name, hsn_code="69072100",
        quantity_boxes=boxes, quantity_unit="BOX", quantity_value=boxes * 1.44,
        unit="SQM",
    )]
    return container.packing_list_repo.create(packing_list)


def _bare_purchase_order(container, seed):
    """The minimum real purchase_orders row a packing list can point at -
    foreign keys are ON, so the origin has to exist."""
    return container.db.execute(
        "INSERT INTO purchase_orders (company_id, po_number, po_date, seller_name, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (seed.company_id, "PO-TEST-1", "2026-04-22", "ALIVE GRANITO LLP", seed.admin.id),
    )


def _bare_job_work(container, seed):
    return container.db.execute(
        "INSERT INTO job_works (company_id, job_work_number, job_work_date, seller_name, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (seed.company_id, "PO-TEST-JW-1", "2026-04-22", "ALIVE GRANITO LLP", seed.admin.id),
    )


def _job_out(container, seed, purchase_invoice, challan_no):
    return container.job_out_service.create(current_user=seed.admin, fields={
        "purchase_invoice_id": str(purchase_invoice.id),
        "delivery_challan_no": challan_no, "delivery_challan_date": "2026-04-22",
    })


def _job_in(container, seed, job_out, inward_no, product, design, boxes):
    """Receive `boxes` of one design back against a job out."""
    return container.job_in_service.create(current_user=seed.admin, fields={
        "job_out_id": str(job_out.id),
        "stock_inward_no": inward_no, "stock_inward_date": "2026-04-30",
    }, raw_items=[{
        "product_id": str(product.id), "product_name": product.product_name,
        "design_id": str(design.id), "design_name": design.design_name,
        "quantity_boxes": str(boxes),
    }])


# --------------------------------------------------------------------------
# Which document is the receipt event
# --------------------------------------------------------------------------
def test_purchase_invoice_packing_list_adds_stock(container, seed, catalog, purchase_invoice):
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["boxes"] == 100
    assert stock["net_boxes"] == 100


def test_purchase_order_packing_list_does_not_add_stock(container, seed, catalog):
    """A PO's own packing list is not the receipt event - the invoice raised
    from it copies the same lines, so counting both would double the goods."""
    _, design = catalog
    po_id = _bare_purchase_order(container, seed)
    _plant_packing_list(container, seed, catalog, "PL-PO", 100, purchase_order_id=po_id)

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["boxes"] == 0
    assert stock["net_boxes"] == 0


def test_job_work_packing_list_does_not_add_stock(container, seed, catalog):
    """Same reasoning as the purchase order: a job-work-sourced invoice has a
    job work packing list beside its own."""
    _, design = catalog
    jw_id = _bare_job_work(container, seed)
    _plant_packing_list(container, seed, catalog, "PL-JW", 100, job_work_id=jw_id)

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["boxes"] == 0


# --------------------------------------------------------------------------
# The Job Out deduction
# --------------------------------------------------------------------------
def test_job_out_deducts_the_invoices_stock(container, seed, catalog, purchase_invoice):
    """The whole point: goods received then sent out for job work net to zero."""
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    _job_out(container, seed, purchase_invoice, "DC-1")

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["boxes"] == 100          # still shows what came in
    assert stock["dispatched_boxes"] == 100
    assert stock["net_boxes"] == 0


def test_two_job_outs_on_one_invoice_deduct_only_once(container, seed, catalog, purchase_invoice):
    """A job out stores no per-design quantities of its own, so the deduction
    is keyed on the INVOICE having a challan, not on how many challans exist -
    otherwise a second lot would deduct the invoice's full quantity twice."""
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    _job_out(container, seed, purchase_invoice, "DC-1")
    _job_out(container, seed, purchase_invoice, "DC-2")

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["dispatched_boxes"] == 100
    assert stock["net_boxes"] == 0


def test_no_job_out_means_no_deduction(container, seed, catalog, purchase_invoice):
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["dispatched_boxes"] == 0
    assert stock["net_boxes"] == 100


def test_deleting_the_job_out_restores_the_stock(container, seed, catalog, purchase_invoice):
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["net_boxes"] == 0

    container.job_out_service.delete(seed.admin, job_out.id)
    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["net_boxes"] == 100


# --------------------------------------------------------------------------
# The Job In addition - the jobbed product's way into stock
# --------------------------------------------------------------------------
def test_job_in_adds_stock_for_the_received_design(container, seed, catalog, purchase_invoice):
    """The return leg. In real use the design received back belongs to the
    JOBBED product, but the arithmetic is the same whichever design it is."""
    product, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    _job_in(container, seed, job_out, "STINW-1", product, design, 90)

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["boxes"] == 190          # 100 purchased + 90 returned
    assert stock["dispatched_boxes"] == 100
    assert stock["net_boxes"] == 90       # what actually came back


def test_several_job_ins_accumulate(container, seed, catalog, purchase_invoice):
    """Goods come back in lots, so each job in adds its own quantity."""
    product, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    _job_in(container, seed, job_out, "STINW-1", product, design, 50)
    _job_in(container, seed, job_out, "STINW-2", product, design, 40)

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["net_boxes"] == 90


def test_deleting_a_job_in_removes_its_stock(container, seed, catalog, purchase_invoice):
    product, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    job_in = _job_in(container, seed, job_out, "STINW-1", product, design, 90)
    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["net_boxes"] == 90

    container.job_in_service.delete(seed.admin, job_in.id)
    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["net_boxes"] == 0


def test_job_in_alt_qty_is_derived_from_the_product(container, seed, catalog, purchase_invoice):
    """Alt Qty is computed server-side (boxes x alternate_quantity) and
    persisted, so the printed sheet can't disagree with what was saved."""
    product, design = catalog
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    job_in = _job_in(container, seed, job_out, "STINW-1", product, design, 10)

    item = job_in.items[0]
    assert item.quantity_boxes == 10
    assert item.quantity_value == 14.4   # 10 x the product's 1.44
    assert item.unit == "SQM"
    assert item.quantity_unit == "BOX"


def test_job_in_rejects_a_duplicate_inward_number(container, seed, catalog, purchase_invoice):
    from app.exceptions import ValidationError
    product, design = catalog
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    _job_in(container, seed, job_out, "STINW-1", product, design, 10)
    with pytest.raises(ValidationError):
        _job_in(container, seed, job_out, "STINW-1", product, design, 5)


def test_deleting_the_job_out_takes_its_job_ins_with_it(container, seed, catalog, purchase_invoice):
    """A job in reads its whole sheet off its job out, so it can't outlive
    it - and its stock must go with it."""
    product, design = catalog
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    _job_in(container, seed, job_out, "STINW-1", product, design, 90)

    container.job_out_service.delete(seed.admin, job_out.id)
    assert container.job_in_service.list_all(seed.company_id) == []
    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["net_boxes"] == 0


def test_job_in_line_without_a_catalog_design_moves_no_stock(container, seed, catalog, purchase_invoice):
    """Same rule packing list lines follow: the row prints, but stock is only
    tracked for real catalog designs."""
    product, design = catalog
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    container.job_in_service.create(current_user=seed.admin, fields={
        "job_out_id": str(job_out.id),
        "stock_inward_no": "STINW-1", "stock_inward_date": "2026-04-30",
    }, raw_items=[{
        "product_id": str(product.id), "product_name": product.product_name,
        "design_id": "", "design_name": "NOT IN THE CATALOG", "quantity_boxes": "40",
    }])

    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["boxes"] == 0


# --------------------------------------------------------------------------
# One formula everywhere
# --------------------------------------------------------------------------
def test_stock_history_summary_agrees_with_stock_by_design(container, seed, catalog, purchase_invoice):
    """These two used to subtract independently; they must now agree."""
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    _job_out(container, seed, purchase_invoice, "DC-1")

    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    summary = container.inventory_service.stock_history_summary(seed.company_id, design.id)
    assert summary["received_boxes"] == stock["boxes"]
    assert summary["dispatched_boxes"] == stock["dispatched_boxes"]
    assert summary["sale_boxes"] == stock["sold_boxes"]
    assert summary["stock_boxes"] == stock["net_boxes"]
    assert summary["stock_alt_qty"] == stock["net_quantity"]


def test_stock_history_po_remain_is_blank_when_stock_came_from_job_work(
        container, seed, catalog, purchase_invoice):
    """A design whose only receipt is a Job In has no purchase order behind it,
    so PO Remain Qty must read blank (None) rather than a negative number."""
    product, _ = catalog
    jobbed = container.product_service.create_design(
        current_user=seed.admin, product_id=product.id, folder_id=None,
        design_name="ATLANTA DARK GREY", description="", price_usd="",
        alt_text="", photo_file=None, dimension_photo_file=None,
    )
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    job_out = _job_out(container, seed, purchase_invoice, "DC-1")
    _job_in(container, seed, job_out, "STINW-1", product, jobbed, 62)

    summary = container.inventory_service.stock_history_summary(seed.company_id, jobbed.id)
    assert summary["po_boxes"] == 0
    assert summary["received_boxes"] == 62
    assert summary["po_remain_boxes"] is None


def test_in_stock_designs_reports_the_same_numbers(container, seed, catalog, purchase_invoice):
    _, design = catalog
    _plant_packing_list(container, seed, catalog, "PL-PINV", 100,
                        purchase_invoice_id=purchase_invoice.id)
    _job_out(container, seed, purchase_invoice, "DC-1")

    rows = container.inventory_service.in_stock_designs(seed.company_id)
    row = next(r for r in rows if r["id"] == design.id)
    assert row["stock"]["dispatched_boxes"] == 100
    assert row["stock"]["net_boxes"] == 0


def test_design_never_received_reads_as_zero(container, seed, catalog):
    _, design = catalog
    stock = container.inventory_service.stock_for_design(seed.company_id, design.id)
    assert stock["boxes"] == 0
    assert stock["dispatched_boxes"] == 0
    assert stock["net_boxes"] == 0


def test_untagged_packing_list_lines_are_ignored(container, seed, catalog, purchase_invoice):
    """Stock is only tracked for real catalog designs - a hand-typed line with
    no design_id contributes nothing."""
    product, design = catalog
    packing_list = PackingList(
        id=None, company_id=seed.company_id, packing_list_number="PL-UNTAGGED",
        packing_list_date="2026-04-22", consignee_name="ANY", created_by=seed.admin.id,
        purchase_invoice_id=purchase_invoice.id,
    )
    packing_list.items = [PackingListItem(
        id=None, packing_list_id=None, sr_no=1, product_id=product.id,
        product_name=product.product_name, design_id=None, design_name="TYPED BY HAND",
        quantity_boxes=50, quantity_unit="BOX", quantity_value=72, unit="SQM",
    )]
    container.packing_list_repo.create(packing_list)

    assert container.inventory_service.stock_for_design(seed.company_id, design.id)["boxes"] == 0
