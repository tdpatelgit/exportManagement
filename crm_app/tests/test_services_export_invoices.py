"""
Tests for ExportInvoiceService (app/services.py) - the customer/customs-
facing document at the buyer end of the pipeline. Mirrors
test_services_proforma_po.py / test_services_purchase_invoices.py, focusing
on what is unique to this document type:

  - references MANY proforma invoices at once (many-to-many),
  - prefill that walks each PI -> its purchase orders -> their purchase
    invoices to import EPCG / export-under / supplier purchase-detail rows,
  - per-product tax computed and summed into IGST vs CGST/SGST,
  - a manual exchange rate that only an admin can change once set,
  - the container-count -> section-11B rows, and the optional shipping
    bill PDF upload.
"""

import io

import pytest
from werkzeug.datastructures import FileStorage

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


def upload(filename="shipping-bill.pdf", data=b"fake-pdf-bytes"):
    return FileStorage(stream=io.BytesIO(data), filename=filename)


def make_company(container, seed, gstin="24AABFO8212B1ZV", declaration="DECL", lut="AD240225016083O",
                 government_schemes=""):
    container.company_repo.upsert(seed.company_id, "AAYU EXIM", "MORBI", gstin, "AABFO8212B", "IEC1", declaration,
                                  government_schemes=government_schemes or None)
    if lut:
        oc = container.company_repo.get(seed.company_id)
        container.company_repo.replace_lut_details(oc.id, [{"lut_number": lut, "financial_year": "2024-25", "is_primary": True}])
    container.company_repo.replace_contact_persons(
        container.company_repo.get(seed.company_id).id,
        [{"name": "Mr. Jignesh", "designation": "Partner Of Aayu Exim", "is_primary": True}],
    )


def make_product(container, seed, name="Tiles", igst="18"):
    return container.product_service.create_product(
        current_user=seed.admin, product_name=name, description="", hsn_code="69072100",
        igst_percent=igst, quantity="", alternate_quantity="")


def make_proforma(container, seed, product=None, buyer_order_no="EXP/003", **over):
    fields = {"consignee_name": "ROBUST INTERNATIONAL", "invoice_date": "2026-01-06",
              "consignee_address": "BEIRA", "buyer_order_no": buyer_order_no,
              "port_of_loading": "MUNDRA", "country_of_destination": "MOZAMBIQUE"}
    fields.update(over)
    item = {"product_name": product.product_name if product else "Tiles", "quantity_value": "100",
            "price_usd": "5.92", "hsn_code": "69072100", "unit": "SQM"}
    if product:
        item["product_id"] = str(product.id)
    return container.proforma_invoice_service.create(seed.admin, fields, [item])


def make_export(container, seed, proforma_ids=None, items=None, export_invoice_number="1000000001", **over):
    fields = {"consignee_name": "ROBUST INTERNATIONAL", "invoice_date": "2026-02-20",
              "tax_mode": "igst", "exchange_rate": "86.70", "export_invoice_number": export_invoice_number}
    if proforma_ids:
        fields["proforma_invoice_ids"] = [str(p) for p in proforma_ids]
    fields.update(over)
    raw_items = items or [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}]
    return container.export_invoice_service.create(seed.admin, fields, raw_items)


def make_chain_pinv(container, seed, pi, product, quantity_boxes="10", quantity_value="100", price_inr="500"):
    """Raises a purchase order + purchase invoice against `pi`, buying
    `product` - the PI -> PO -> PInv chain goods lines are now sourced from
    (see ExportInvoiceService.build_prefill_from_proformas)."""
    po = container.purchase_order_service.create(
        seed.admin,
        {"seller_name": "Alive Granito", "po_date": "2026-01-10", "seller_gstin": "24ABVFA1170D1ZO",
         "proforma_invoice_id": str(pi.id)},
        [{"product_name": product.product_name, "product_id": str(product.id), "quantity_boxes": quantity_boxes,
          "quantity_value": quantity_value, "price_inr": price_inr, "price_per": "BOX"}])
    pinv = container.purchase_invoice_service.create(
        seed.admin,
        {"seller_name": "Alive Granito", "invoice_number": "GSTT/4987", "invoice_date": "2026-01-15",
         "seller_gstin": "24ABVFA1170D1ZO", "purchase_order_id": str(po.id)},
        [{"product_name": product.product_name, "product_id": str(product.id), "quantity_value": quantity_value,
          "price_inr": price_inr, "price_per": "BOX", "quantity_boxes": quantity_boxes}], [])
    return po, pinv


# ==========================================================================
# Basic create / read / update / delete
# ==========================================================================
class TestExportCrud:
    def test_create_persists_the_typed_number(self, container, seed):
        inv = make_export(container, seed, export_invoice_number="1234567890")
        assert inv.export_invoice_number == "1234567890"
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.consignee_name == "ROBUST INTERNATIONAL"
        assert len(got.items) == 1

    def test_the_ladder_builds_up_from_the_typed_fob_price(self, container, seed):
        # The typed price is always the absolute FOB price and nothing ever
        # adjusts it, so the goods total IS the FOB value and the ladder is
        # built UP from there: CIF = FOB + the charges, invoice value = that
        # minus the discount. Mirrors Quotation/ProformaInvoice exactly.
        inv = make_export(container, seed, nature_of_contract="CIF", sea_freight="100",
                          items=[{"product_name": "Tiles", "quantity_value": "3", "unit": "SQM",
                                  "price_usd": "7", "igst_percent": "18"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        item = got.items[0]
        assert item.price_usd == 7.0                      # untouched by the charges
        assert item.total_usd == 21.0
        assert got.round_off == 0
        assert got.fob_value_usd == pytest.approx(21.0)   # exactly 3 x the typed 7
        assert got.cif_value_usd == pytest.approx(121.0)  # 21 + 100 sea freight

    def test_invoice_value_includes_other_charges_under_fob_terms(self, container, seed):
        # Under FOB terms the FOB value must move only when a line's typed
        # price or quantity changes - never with the discount or the charges.
        # Invoice value is then built back UP from it: FOB + every charge -
        # discount. sea_freight/insurance are auto-zeroed by _build_header
        # under FOB terms (drops_sea_freight/drops_insurance - same rule
        # quotations and proforma invoices use), but the certification and
        # other_charges are not gated by any delivery term - they still have
        # to reach the buyer's payable figure rather than silently vanishing.
        inv = make_export(container, seed, nature_of_contract="FOB", sea_freight="100",
                          insurance="50", certification="20", other_charges="10", discount_amount="10",
                          items=[{"product_name": "Tiles", "quantity_value": "10", "unit": "SQM",
                                  "price_usd": "6", "igst_percent": "18"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.sea_freight == 0 and got.insurance == 0   # dropped by FOB terms
        assert got.certification == 20                       # never dropped by any term
        assert got.fob_value_usd == 60.0                     # goods total only
        assert got.cif_value_usd == 90.0                     # 60 + (0+0+20+10)
        assert got.invoice_value_usd == 80.0                 # 90 - 10

    def test_the_ladder_is_the_same_shape_under_cif_terms(self, container, seed):
        # Identical arithmetic under CIF terms - the delivery term only
        # decides which charge fields are non-zero, never how the ladder is
        # worked out.
        inv = make_export(container, seed, nature_of_contract="CIF", insurance="50", discount_amount="100",
                          items=[{"product_name": "Tiles", "quantity_value": "144", "unit": "SQM",
                                  "price_usd": "5.92", "igst_percent": "18"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.fob_value_usd == pytest.approx(852.48)       # the goods total
        assert got.cif_value_usd == pytest.approx(902.48)       # 852.48 + 50
        assert got.invoice_value_usd == pytest.approx(802.48)   # 902.48 - 100

    def test_the_discount_never_touches_the_fob_value(self, container, seed):
        inv = make_export(container, seed, nature_of_contract="CIF", sea_freight="100",
                          discount_amount="15",
                          items=[{"product_name": "Tiles", "quantity_value": "3", "unit": "SQM",
                                  "price_usd": "7", "igst_percent": "18"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.fob_value_usd == pytest.approx(21.0)     # exactly 3 x the typed 7

    def test_the_export_prices_are_stored_exactly_as_typed(self, container, seed):
        got = container.export_invoice_service.get(
            make_export(container, seed, nature_of_contract="CIF", sea_freight="100").id, seed.company_id)
        assert got.round_off == 0
        assert got.items[0].price_usd == 5.92

    def test_fob_nature_of_contract_drops_sea_freight_and_insurance(self, container, seed):
        # FOB puts the ocean leg on the buyer - neither charge is stored, and
        # the printed sheet drops their rows with them. The certification is
        # a seller-side cost and stays payable, like other charges.
        inv = make_export(container, seed, nature_of_contract="FOB",
                          sea_freight="100", insurance="20", certification="30")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.sea_freight == 0
        assert got.insurance == 0
        assert got.certification == 30

    def test_cfr_nature_of_contract_drops_only_the_insurance(self, container, seed):
        # CFR keeps the freight with the seller and moves only the cargo
        # insurance to the buyer, so only that row leaves the printed sheet.
        inv = make_export(container, seed, nature_of_contract="CFR - BEIRA",
                          sea_freight="100", insurance="20", certification="30")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.sea_freight == 100
        assert got.insurance == 0
        assert got.certification == 30

    def test_number_is_required(self, container, seed):
        with pytest.raises(ValidationError):
            make_export(container, seed, export_invoice_number="")

    def test_number_allows_free_text(self, container, seed):
        inv = make_export(container, seed, export_invoice_number="EXP/AB-001")
        assert inv.export_invoice_number == "EXP/AB-001"

    def test_number_must_be_at_most_16_chars(self, container, seed):
        with pytest.raises(ValidationError):
            make_export(container, seed, export_invoice_number="1" * 17)

    def test_number_must_be_unique_per_company(self, container, seed):
        make_export(container, seed, export_invoice_number="5555555555")
        with pytest.raises(ValidationError):
            make_export(container, seed, export_invoice_number="5555555555")

    def test_get_is_tenant_scoped(self, container, seed):
        inv = make_export(container, seed)
        other = container.tenant_repo.create("Other Co", "other")
        with pytest.raises(NotFoundError):
            container.export_invoice_service.get(inv.id, other.id)

    def test_requires_a_consignee(self, container, seed):
        with pytest.raises(ValidationError):
            make_export(container, seed, consignee_name="")

    def test_requires_at_least_one_item(self, container, seed):
        with pytest.raises(ValidationError):
            container.export_invoice_service.create(
                seed.admin, {"consignee_name": "X", "invoice_date": "2026-02-20"}, [])

    def test_delete_removes_it(self, container, seed):
        inv = make_export(container, seed)
        container.export_invoice_service.delete(seed.admin, inv.id)
        with pytest.raises(NotFoundError):
            container.export_invoice_service.get(inv.id, seed.company_id)

    def test_examination_date_defaults_to_creation_date_not_edit(self, container, seed):
        inv = make_export(container, seed)  # no examination_date given
        assert inv.examination_date == inv.invoice_date
        # editing later (with a new invoice_date) does not move examination_date
        updated = container.export_invoice_service.update(
            seed.admin, inv.id,
            {"consignee_name": "ROBUST", "invoice_date": "2026-03-01", "exchange_rate": "86.70", "export_invoice_number": "1000000001"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}])
        assert updated.examination_date == "2026-02-20"


# ==========================================================================
# Many-to-many links + prefill from proforma invoices
# ==========================================================================
class TestExportProformaLinks:
    def test_links_multiple_proformas(self, container, seed):
        p1 = make_proforma(container, seed)
        p2 = make_proforma(container, seed)
        inv = make_export(container, seed, proforma_ids=[p1.id, p2.id])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert set(got.proforma_invoice_ids) == {p1.id, p2.id}
        assert len(got.linked_proformas) == 2

    def test_rejects_proformas_from_different_buyers(self, container, seed):
        p1 = make_proforma(container, seed, consignee_name="ROBUST INTERNATIONAL")
        p2 = make_proforma(container, seed, consignee_name="OTHER BUYER LTD")
        with pytest.raises(ValidationError):
            make_export(container, seed, proforma_ids=[p1.id, p2.id])

    def test_prefill_merges_goods_from_all_selected_pis(self, container, seed):
        # Goods now come from each PI's own purchase-invoice chain, not from
        # the PI's quoted lines directly - so both PIs need one to contribute
        # an item. Two DIFFERENT products, since the same product bought
        # under both PIs would correctly collapse into one summed line (see
        # test_same_product_across_multiple_purchase_orders_is_summed_into_one_line).
        make_company(container, seed)
        product1 = make_product(container, seed, name="Tiles A")
        product2 = make_product(container, seed, name="Tiles B")
        p1 = make_proforma(container, seed, product=product1)
        p2 = make_proforma(container, seed, product=product2)
        make_chain_pinv(container, seed, p1, product1)
        make_chain_pinv(container, seed, p2, product2)
        built = container.export_invoice_service.build_prefill_from_proformas([p1.id, p2.id], seed.company_id)
        assert len(built["items"]) == 2
        assert built["fields"]["consignee_name"] == "ROBUST INTERNATIONAL"

    def test_prefill_takes_buyer_order_from_first_pi_that_has_one(self, container, seed):
        # All PIs under one export invoice share the same buyer order, so the
        # prefill is a single field taken from the first PI that has one.
        p1 = make_proforma(container, seed, buyer_order_no="")
        p2 = make_proforma(container, seed, buyer_order_no="EXP/002")
        built = container.export_invoice_service.build_prefill_from_proformas([p1.id, p2.id], seed.company_id)
        assert built["fields"]["buyer_order_no"] == "EXP/002"

    def test_prefill_sums_charges_from_all_selected_pis(self, container, seed):
        p1 = make_proforma(container, seed, sea_freight="100", insurance="20", certification="5",
                            other_charges="10", discount_amount="2")
        p2 = make_proforma(container, seed, sea_freight="50", insurance="30", certification="0",
                            other_charges="0", discount_amount="8")
        built = container.export_invoice_service.build_prefill_from_proformas([p1.id, p2.id], seed.company_id)
        fields = built["fields"]
        assert fields["sea_freight"] == 150
        assert fields["insurance"] == 50
        assert fields["certification"] == 5
        assert fields["other_charges"] == 10
        assert fields["discount_amount"] == 10

    def test_prefill_carries_the_pis_typed_fob_price_across_unchanged(self, container, seed):
        """Both documents hold their prices the same way now: price_usd is the
        typed FOB price, and the charges are added on top as a document-level
        figure. So the PI's own typed rate crosses over untouched - taking its
        CIF-priced view (printed_items) instead would fold the PI's charges
        into the export invoice's per-unit rate and then add the export
        invoice's own charges on top of that again. The goods line itself
        (identity/qty) comes from the PI's purchase-invoice chain, but price is
        still matched by product_id back to the PI's own rate."""
        make_company(container, seed)
        product = make_product(container, seed)
        pi = make_proforma(container, seed, product=product, terms_of_delivery="CIF", sea_freight="100",
                            insurance="20")   # 120 of charges, NOT spread into the rate
        make_chain_pinv(container, seed, pi, product)
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert built["items"][0]["price_usd"] == pytest.approx(5.92)
        assert built["items"][0]["price_usd"] == pi.items[0].price_usd
        # The export invoice raised off that prefill lands on the PI's own ladder.
        inv = make_export(container, seed, proforma_ids=[pi.id],
                          nature_of_contract="CIF", sea_freight="100", insurance="20",
                          items=[{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM",
                                  "price_usd": str(built["items"][0]["price_usd"])}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.cif_value_usd == pytest.approx(pi.cif_value_usd)
        assert got.fob_value_usd == pytest.approx(pi.fob_value_usd)

    def test_prefill_carries_the_price_across_when_the_pi_has_no_charges(self, container, seed):
        # FOB terms hold the freight and the insurance at zero; either way the
        # typed rate is what crosses over.
        make_company(container, seed)
        product = make_product(container, seed)
        pi = make_proforma(container, seed, product=product, terms_of_delivery="FOB MUNDRA", sea_freight="100")
        make_chain_pinv(container, seed, pi, product)
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert built["items"][0]["price_usd"] == 5.92

    def test_prefill_nature_of_contract_from_first_pi_terms_of_delivery(self, container, seed):
        p1 = make_proforma(container, seed, terms_of_delivery="CNF- (Beira)")
        p2 = make_proforma(container, seed, terms_of_delivery="FOB- (Mundra)")
        built = container.export_invoice_service.build_prefill_from_proformas([p1.id, p2.id], seed.company_id)
        assert built["fields"]["nature_of_contract"] == "CNF- (Beira)"

    def test_prefill_export_under_from_company_government_schemes(self, container, seed):
        make_company(container, seed, government_schemes="WE INTEND TO CLAIM RoDTEP & DBK")
        p1 = make_proforma(container, seed)
        built = container.export_invoice_service.build_prefill_from_proformas([p1.id], seed.company_id)
        assert built["fields"]["export_under"] == "WE INTEND TO CLAIM RoDTEP & DBK"

    def test_prefill_ignores_other_companys_pi(self, container, seed):
        other = container.tenant_repo.create("Other Co", "other")
        other_admin = container.auth_service.create_user(
            company_id=other.id, username="o", password="pass-123456", full_name="O", role="admin")
        p_other = make_proforma(container, type("S", (), {"admin": other_admin, "company_id": other.id}))
        built = container.export_invoice_service.build_prefill_from_proformas([p_other.id], seed.company_id)
        assert built["items"] == []


# ==========================================================================
# Import EPCG / supplier purchase details through the PI -> PO -> PInv chain
# ==========================================================================
class TestExportChainImport:
    def _chain(self, container, seed, purchase_type="exemption"):
        make_company(container, seed)
        product = make_product(container, seed)
        pi = make_proforma(container, seed, product=product)
        po = container.purchase_order_service.create(
            seed.admin,
            {"seller_name": "Alive Granito", "po_date": "2026-01-10", "seller_gstin": "24ABVFA1170D1ZO",
             "proforma_invoice_id": str(pi.id), "purchase_type": purchase_type},
            [{"product_name": "Tiles", "product_id": str(product.id), "quantity_boxes": "10",
              "quantity_value": "100", "price_inr": "500", "price_per": "BOX"}])
        pinv = container.purchase_invoice_service.create(
            seed.admin,
            {"seller_name": "Alive Granito", "invoice_number": "GSTT/4987", "invoice_date": "2026-01-15",
             "seller_gstin": "24ABVFA1170D1ZO", "purchase_order_id": str(po.id),
             "epcg_number": "2431000888", "epcg_date": "2021-09-17", "purchase_type": purchase_type},
            [{"product_name": "Tiles", "quantity_value": "100", "price_inr": "500", "price_per": "BOX",
              "quantity_boxes": "10"}], [])
        return pi, po, pinv

    def test_imports_epcg_from_purchase_invoice(self, container, seed):
        pi, po, pinv = self._chain(container, seed)
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert built["fields"]["epcg_number"] == "2431000888"
        assert built["fields"]["epcg_date"] == "2021-09-17"

    def test_creating_an_export_invoice_resolves_epcg_from_the_chain(self, container, seed):
        """Unlike the prefill above (which only feeds the form), the export
        invoice itself now resolves EPCG the same way at create() time - not
        from a posted field, which is ignored even if a tampered POST sends
        one. export_under is likewise always ignored; the sheet's own
        fallback to the live company scheme is what actually prints it."""
        pi, po, pinv = self._chain(container, seed)
        inv = make_export(container, seed, proforma_ids=[pi.id],
                           export_under="TYPED OVERRIDE ATTEMPT",
                           epcg_number="SHOULD BE IGNORED", epcg_date="2000-01-01")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.epcg_number == "2431000888"
        assert got.epcg_date == "2021-09-17"
        assert got.export_under is None

    def test_no_linked_pi_leaves_epcg_blank(self, container, seed):
        make_company(container, seed)
        inv = make_export(container, seed)
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.epcg_number is None
        assert got.epcg_date is None

    def test_imports_supplier_exemption_purchase_details(self, container, seed):
        pi, po, pinv = self._chain(container, seed, purchase_type="exemption")
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        pd = built["purchase_details"]
        assert len(pd) == 1
        assert pd[0]["supplier_gstin"] == "24ABVFA1170D1ZO"
        assert pd[0]["supplier_invoice_no"] == "GSTT/4987"
        assert pd[0]["purchase_type"] == "exemption"
        assert pd[0]["epcg_number"] == "2431000888"
        assert pd[0]["epcg_date"] == "2021-09-17"

    def test_full_tax_purchase_also_contributes_a_purchase_detail_row(self, container, seed):
        pi, po, pinv = self._chain(container, seed, purchase_type="full_tax")
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        pd = built["purchase_details"]
        assert len(pd) == 1
        assert pd[0]["supplier_gstin"] == "24ABVFA1170D1ZO"
        assert pd[0]["supplier_invoice_no"] == "GSTT/4987"

    def test_purchase_detail_row_carries_the_purchase_invoices_own_purchase_type(self, container, seed):
        pi, po, pinv = self._chain(container, seed, purchase_type="exemption")
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert built["purchase_details"][0]["purchase_type"] == "exemption"

    def test_prefill_forces_lut_and_locks_it_when_any_purchase_is_under_exemption(self, container, seed):
        pi, po, pinv = self._chain(container, seed, purchase_type="exemption")
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert built["fields"]["tax_mode"] == "lut"
        assert built["fields"]["tax_mode_locked"] is True

    def test_prefill_leaves_tax_mode_untouched_when_every_purchase_is_full_tax(self, container, seed):
        pi, po, pinv = self._chain(container, seed, purchase_type="full_tax")
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert "tax_mode" not in built["fields"]
        assert built["fields"]["tax_mode_locked"] is False

    def test_creating_under_an_exemption_purchase_forces_lut_even_if_igst_was_posted(self, container, seed):
        """Mirrors the EPCG/export_under treatment above: the posted
        tax_mode is ignored (even a tampered 'igst') the moment any linked
        purchase invoice is under exemption - recomputed fresh from the
        purchase chain at save time, not trusted from the form."""
        pi, po, pinv = self._chain(container, seed, purchase_type="exemption")
        inv = make_export(container, seed, proforma_ids=[pi.id], tax_mode="igst")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.tax_mode == "lut"

    def test_creating_with_only_full_tax_purchases_keeps_the_posted_tax_mode(self, container, seed):
        pi, po, pinv = self._chain(container, seed, purchase_type="full_tax")
        inv = make_export(container, seed, proforma_ids=[pi.id], tax_mode="igst")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.tax_mode == "igst"

    def test_same_product_bought_against_two_purchase_orders_is_summed_into_one_line(self, container, seed):
        """Reproduces EXP/25-26/025: one purchase invoice can cover several
        purchase orders of the same supplier at once (a shipment invoiced
        against two orders together), each as its own item line for the SAME
        product. Left unmerged that put the product on the Export Invoice
        twice - it must instead collapse into one goods line with the boxes
        (and pallets) summed, with the per-PO split kept in product_sources
        for traceability."""
        make_company(container, seed)
        product = make_product(container, seed)
        pi = make_proforma(container, seed, product=product)
        po1 = container.purchase_order_service.create(
            seed.admin,
            {"seller_name": "Alive Granito", "po_date": "2026-01-10", "seller_gstin": "24ABVFA1170D1ZO",
             "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "product_id": str(product.id), "quantity_boxes": "10",
              "quantity_value": "100", "price_inr": "500", "price_per": "BOX"}])
        po2 = container.purchase_order_service.create(
            seed.admin,
            {"seller_name": "Alive Granito", "po_date": "2026-01-11", "seller_gstin": "24ABVFA1170D1ZO",
             "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "product_id": str(product.id), "quantity_boxes": "5",
              "quantity_value": "50", "price_inr": "500", "price_per": "BOX"}])
        container.purchase_invoice_service.create(
            seed.admin,
            {"seller_name": "Alive Granito", "invoice_number": "STL/0025/26-27", "invoice_date": "2026-01-15",
             "seller_gstin": "24ABVFA1170D1ZO", "purchase_order_ids": [str(po1.id), str(po2.id)]},
            [{"product_name": "Tiles", "product_id": str(product.id), "quantity_value": "100",
              "price_inr": "500", "price_per": "BOX", "quantity_boxes": "10", "purchase_order_id": str(po1.id)},
             {"product_name": "Tiles", "product_id": str(product.id), "quantity_value": "50",
              "price_inr": "500", "price_per": "BOX", "quantity_boxes": "5", "purchase_order_id": str(po2.id)}],
            [])
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert len(built["items"]) == 1
        assert built["items"][0]["quantity_boxes"] == 15
        assert built["items"][0]["quantity_value"] == 150

        sources = built["product_sources"]
        assert len(sources) == 2
        by_po = {s["po_number"]: s["quantity_boxes"] for s in sources}
        assert by_po[po1.po_number] == 10
        assert by_po[po2.po_number] == 5


# ==========================================================================
# Import Job In details + jobbed products through the PI -> job work ->
# job-work purchase invoice -> job out -> job in chain
# ==========================================================================
class TestExportJobInImport:
    def _chain(self, container, seed):
        make_company(container, seed)
        product = container.product_service.create_product(
            current_user=seed.admin, product_name="Tiles", description="", hsn_code="69072100",
            igst_percent="18", quantity="", alternate_quantity="1.44",
            quantity_unit="BOX", alternate_quantity_unit="SQM")
        design = container.product_service.create_design(
            current_user=seed.admin, product_id=product.id, folder_id=None,
            design_name="ATLANTA LIGHT GREY", description="", price_usd="",
            alt_text="", photo_file=None, dimension_photo_file=None)
        pi = make_proforma(container, seed, product=product)

        # The job work raised off the proforma invoice - the design/Job
        # Quantity chain isn't exercised here, so plant the row directly (the
        # same "plant a real parent row" style test_services_inventory uses).
        job_work_id = container.db.execute(
            "INSERT INTO job_works (company_id, job_work_number, job_work_date, seller_name, "
            "created_by, proforma_invoice_id) VALUES (?, ?, ?, ?, ?, ?)",
            (seed.company_id, "PO20260110001", "2026-01-10", "Alive Granito", seed.admin.id, pi.id))

        # The job-work purchase invoice: what was actually bought for the job,
        # linked to the job work via purchase_invoice_job_work_links.
        jw_pinv = container.purchase_invoice_service.create(
            seed.admin,
            {"seller_name": "Alive Granito", "invoice_number": "JW/PINV/1", "invoice_date": "2026-01-15",
             "seller_gstin": "24ABVFA1170D1ZO", "currency_code": "INR"},
            [{"product_name": product.product_name, "product_id": str(product.id), "quantity_value": "144",
              "price_inr": "400", "price_per": "BOX", "quantity_boxes": "100"}], [])
        container.db.execute(
            "INSERT INTO purchase_invoice_job_work_links (purchase_invoice_id, job_work_id) VALUES (?, ?)",
            (jw_pinv.id, job_work_id))

        job_out = container.job_out_service.create(current_user=seed.admin, fields={
            "purchase_invoice_id": str(jw_pinv.id),
            "delivery_challan_no": "DC/OUT/1", "delivery_challan_date": "2026-01-20"})

        def receive(inward_no, boxes):
            return container.job_in_service.create(current_user=seed.admin, fields={
                "job_out_id": str(job_out.id),
                "stock_inward_no": inward_no, "stock_inward_date": "2026-01-30",
                "jw_delivery_challan_no": "JWDC/1", "jw_delivery_challan_date": "2026-01-29",
            }, raw_items=[{
                "product_id": str(product.id), "product_name": product.product_name,
                "design_id": str(design.id), "design_name": design.design_name,
                "quantity_boxes": str(boxes),
            }])

        return pi, product, job_out, receive

    def test_job_in_details_row_is_imported(self, container, seed):
        pi, product, job_out, receive = self._chain(container, seed)
        receive("STINW/1", 30)
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert len(built["job_ins"]) == 1
        row = built["job_ins"][0]
        assert row["stock_inward_no"] == "STINW/1"
        assert row["jw_challan_no"] == "JWDC/1"
        assert row["job_out_challan_no"] == "DC/OUT/1"

    def test_jobbed_product_merges_into_one_line_priced_at_the_pi_rate(self, container, seed):
        pi, product, job_out, receive = self._chain(container, seed)
        # Two return lots of the same jobbed product/design.
        receive("STINW/1", 30)
        receive("STINW/2", 20)
        built = container.export_invoice_service.build_prefill_from_proformas([pi.id], seed.company_id)
        assert len(built["job_ins"]) == 2
        assert len(built["items"]) == 1
        item = built["items"][0]
        assert item["product_id"] == product.id
        assert item["quantity_boxes"] == 50            # 30 + 20, summed across lots
        assert item["price_usd"] == 5.92               # the PI's own quoted USD rate

    def test_job_ins_round_trip(self, container, seed):
        inv = make_export(container, seed, job_ins=[{
            "manufacturer_name": "Alive Granito", "manufacturer_gstin": "24ABVFA1170D1ZO",
            "job_out_challan_no": "DC/OUT/1", "jw_challan_no": "JWDC/1",
            "jw_challan_date": "2026-01-29", "stock_inward_no": "STINW/1",
            "stock_inward_date": "2026-01-30",
        }])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert len(got.job_ins) == 1
        assert got.job_ins[0]["stock_inward_no"] == "STINW/1"
        assert got.job_ins[0]["jw_challan_no"] == "JWDC/1"


# ==========================================================================
# Designs Packing List offers designs that only came in via a Job In
# (the jobbed product never touches a purchase invoice, so the purchase-
# side design_totals_for_product can't see it - reference_designs merges
# JobInRepository.returned_design_totals_for_product on top, scoped to the
# job works behind this invoice's linked proformas)
# ==========================================================================
class TestExportDesignsPackingListJobIn(TestExportJobInImport):
    def _export_with_split(self, container, seed, boxes):
        """The full chain plus an export invoice built off the proforma with
        one `boxes`-box line of the jobbed product, so its Export Packing
        List has a container line to allocate designs against."""
        pi, product, job_out, receive = self._chain(container, seed)
        design = container.product_service.list_designs_for_product(product.id, seed.company_id)[0]
        inv = make_export(
            container, seed, proforma_ids=[pi.id],
            items=[{"product_name": product.product_name, "product_id": str(product.id),
                    "quantity_value": "144", "quantity_boxes": str(boxes),
                    "unit": "SQM", "price_usd": "5.92"}],
        )
        pl = container.export_packing_list_service.get_for_invoice(inv.id, seed.company_id)
        return inv, product, design, pl, receive

    def test_job_in_design_is_offered_for_its_container_line(self, container, seed):
        inv, product, design, pl, receive = self._export_with_split(container, seed, 30)
        receive("STINW/1", 30)

        ref = container.export_packing_list_service.reference_designs(seed.company_id, pl)
        line = pl.items[0]
        rows = ref[(line.invoice_item_sr_no, line.container_sr_no)]
        assert [(r["design_id"], r["boxes"], r["remaining"]) for r in rows] == [(design.id, 30, 30)]

    def test_nothing_offered_until_the_goods_are_actually_received(self, container, seed):
        inv, product, design, pl, receive = self._export_with_split(container, seed, 30)
        # No receive() call - the job out exists but nothing has come back.
        ref = container.export_packing_list_service.reference_designs(seed.company_id, pl)
        assert ref[(pl.items[0].invoice_item_sr_no, pl.items[0].container_sr_no)] == []

    def test_save_allocation_accepts_up_to_the_received_boxes_and_rejects_beyond(self, container, seed):
        inv, product, design, pl, receive = self._export_with_split(container, seed, 30)
        receive("STINW/1", 30)
        line = pl.items[0]

        container.export_packing_list_service.save_design_allocation(
            seed.company_id, pl.id, line.invoice_item_sr_no, line.container_sr_no,
            [{"design_id": str(design.id), "quantity_boxes": "30"}],
        )
        saved = container.export_packing_list_service.get(pl.id, seed.company_id).items[0]
        assert [(d.design_id, d.quantity_boxes) for d in saved.designs] == [(design.id, 30)]

    def test_save_allocation_rejects_more_than_was_received(self, container, seed):
        inv, product, design, pl, receive = self._export_with_split(container, seed, 40)
        receive("STINW/1", 30)
        line = pl.items[0]
        with pytest.raises(ValidationError):
            container.export_packing_list_service.save_design_allocation(
                seed.company_id, pl.id, line.invoice_item_sr_no, line.container_sr_no,
                [{"design_id": str(design.id), "quantity_boxes": "40"}],
            )

    def test_two_return_lots_of_one_design_sum(self, container, seed):
        inv, product, design, pl, receive = self._export_with_split(container, seed, 50)
        receive("STINW/1", 30)
        receive("STINW/2", 20)
        ref = container.export_packing_list_service.reference_designs(seed.company_id, pl)
        rows = ref[(pl.items[0].invoice_item_sr_no, pl.items[0].container_sr_no)]
        assert [(r["design_id"], r["boxes"]) for r in rows] == [(design.id, 50)]


# ==========================================================================
# Per-product tax
# ==========================================================================
class TestExportTax:
    def test_tax_is_per_product_summed_into_igst(self, container, seed):
        make_company(container, seed)
        p18 = make_product(container, seed, name="WallTile", igst="18")
        p5 = make_product(container, seed, name="Adhesive", igst="5")
        inv = make_export(container, seed, exchange_rate="100", tax_mode="igst", items=[
            {"product_name": "WallTile", "product_id": str(p18.id), "quantity_value": "10", "unit": "SQM", "price_usd": "10"},
            {"product_name": "Adhesive", "product_id": str(p5.id), "quantity_value": "10", "unit": "SQM", "price_usd": "10"},
        ])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        # line1: 100usd*100rate*18% = 1800 ; line2: 100usd*100rate*5% = 500
        assert round(got.tax_total_inr, 2) == 2300.0
        assert round(got.igst_amount_inr, 2) == 2300.0

    def test_lut_mode_is_zero_rated(self, container, seed):
        make_company(container, seed)
        p18 = make_product(container, seed, name="WallTile", igst="18")
        inv = make_export(container, seed, exchange_rate="100", tax_mode="lut", items=[
            {"product_name": "WallTile", "product_id": str(p18.id), "quantity_value": "10", "unit": "SQM", "price_usd": "10"},
        ])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert round(got.tax_total_inr, 2) == 1800.0
        assert got.igst_amount_inr == 0


# ==========================================================================
# Manual, admin-locked exchange rate
# ==========================================================================
class TestExportExchangeRate:
    def test_admin_can_change_rate(self, container, seed):
        inv = make_export(container, seed, exchange_rate="86.70")
        updated = container.export_invoice_service.update(
            seed.admin, inv.id, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "90", "export_invoice_number": "1000000002"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}])
        assert updated.exchange_rate == 90

    def test_non_admin_owner_cannot_change_rate(self, container, seed):
        inv = container.export_invoice_service.create(
            seed.employee, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "86.70", "export_invoice_number": "1000000002"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}])
        with pytest.raises(PermissionDeniedError):
            container.export_invoice_service.update(
                seed.employee, inv.id, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "99", "export_invoice_number": "1000000003"},
                [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}])

    def test_non_admin_blank_rate_keeps_stored_value(self, container, seed):
        inv = container.export_invoice_service.create(
            seed.employee, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "86.70", "export_invoice_number": "1000000002"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}])
        updated = container.export_invoice_service.update(
            seed.employee, inv.id, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "", "export_invoice_number": "1000000004"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}])
        assert updated.exchange_rate == 86.70


# ==========================================================================
# Child lists
# ==========================================================================
class TestExportChildLists:
    def test_containers_and_11b_rows_round_trip(self, container, seed):
        inv = make_export(
            container, seed,
            containers=[{"container_type": "20FT FCL", "container_count": "2"}],
            container_details_list=[
                {"container_type": "20FT FCL", "container_no": "ABCD1234", "line_seal_no": "LS1",
                 "rfid_seal_no": "RF1", "vehicle_no": "GJ01", "tare_weight_kg": "2200"}],
        )
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.total_containers == 2
        assert got.container_details[0]["container_no"] == "ABCD1234"
        assert got.container_details[0]["tare_weight_kg"] == 2200

    def test_gross_and_net_weight_have_no_form_field_but_survive_edits(self, container, seed):
        # No form field sets these - they start out blank.
        inv = make_export(
            container, seed,
            containers=[{"container_type": "20FT FCL", "container_count": "1"}],
            container_details_list=[{"container_no": "ABCD1234", "tare_weight_kg": "2200"}],
        )
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.container_details[0]["gross_weight"] is None
        assert got.container_details[0]["net_weight"] is None

        # Simulate them being set some other way (outside this form).
        container.export_invoice_repo.db.execute(
            "UPDATE export_invoice_container_details SET gross_weight = ?, net_weight = ? "
            "WHERE export_invoice_id = ?", ("5000", "2800", inv.id))

        # An unrelated edit through the service - the form always resubmits
        # every current 11B row's editable fields (container_no/tare_weight_kg
        # etc.), but never gross/net weight, since they aren't form fields.
        updated = container.export_invoice_service.update(
            seed.admin, inv.id,
            {"consignee_name": "NEW NAME", "invoice_date": "2026-02-20",
             "export_invoice_number": inv.export_invoice_number,
             "containers": [{"container_type": "20FT FCL", "container_count": "1"}],
             "container_details_list": [{"container_no": "ABCD1234", "tare_weight_kg": "2200"}]},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}],
        )
        assert updated.container_details[0]["gross_weight"] == "5000"
        assert updated.container_details[0]["net_weight"] == "2800"
        assert updated.container_details[0]["tare_weight_kg"] == 2200

    def test_11b_tare_weight_round_trips(self, container, seed):
        inv = make_export(
            container, seed,
            container_details_list=[
                {"container_no": "ABCD1234", "line_seal_no": "LS1", "rfid_seal_no": "RF1",
                 "vehicle_no": "GJ01", "tare_weight_kg": "2250.5"}],
        )
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.container_details[0]["tare_weight_kg"] == 2250.5

    def test_11b_tare_weight_must_be_a_number(self, container, seed):
        with pytest.raises(ValidationError):
            make_export(
                container, seed,
                container_details_list=[{"container_no": "ABCD1234", "tare_weight_kg": "heavy"}],
            )

    def test_a_row_carrying_only_a_tare_weight_is_still_kept(self, container, seed):
        """Blank 11B rows are dropped, and the export packing list's split
        indexes into what survives - so a row is 'filled in' if ANY column
        is, tare weight included, or the container numbering would shift."""
        inv = make_export(
            container, seed,
            container_details_list=[
                {"container_no": "", "line_seal_no": "", "rfid_seal_no": "", "vehicle_no": "",
                 "tare_weight_kg": "2100"},
                {"container_no": "", "line_seal_no": "", "rfid_seal_no": "", "vehicle_no": "",
                 "tare_weight_kg": ""}],
        )
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert len(got.container_details) == 1
        assert got.container_details[0]["tare_weight_kg"] == 2100

    def test_buyer_order_no_and_date_round_trip(self, container, seed):
        inv = make_export(container, seed, buyer_order_no="EXP/1", buyer_order_date="2026-02-01")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.buyer_order_no == "EXP/1"
        assert got.buyer_order_date == "2026-02-01"

    def test_purchase_details_round_trip(self, container, seed):
        inv = make_export(container, seed, purchase_details=[
            {"supplier_gstin": "24ABVFA1170D1ZO", "supplier_invoice_no": "GSTT/4987",
             "purchase_type": "exemption", "epcg_number": "2431000888", "epcg_date": "2021-09-17"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        pd = got.purchase_details[0]
        assert pd["supplier_invoice_no"] == "GSTT/4987"
        assert pd["purchase_type"] == "exemption"
        assert pd["epcg_number"] == "2431000888"
        assert pd["epcg_date"] == "2021-09-17"

    def test_11b_row_round_trip_without_dropped_fields(self, container, seed):
        # excise_seal_no/plts/boxes were dropped in v37 - anything still
        # sending them is ignored rather than stored.
        inv = make_export(container, seed, container_details_list=[
            {"container_no": "BLJU2253726", "tare_weight_kg": "3800",
             "excise_seal_no": "WIND02432727", "plts": "24", "boxes": "1919"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        cd = got.container_details[0]
        assert cd["container_no"] == "BLJU2253726"
        assert cd["tare_weight_kg"] == 3800
        assert not {"excise_seal_no", "plts", "boxes"} & set(cd)

    def test_booking_no_round_trip(self, container, seed):
        inv = make_export(container, seed, booking_no="BKG/12345")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.booking_no == "BKG/12345"

    def test_11b_lr_transporter_and_max_weight_round_trip(self, container, seed):
        inv = make_export(container, seed, container_details_list=[
            {"container_no": "BLJU2253726", "vehicle_no": "GJ01AB1234", "lr_no": "LR/2026/88",
             "transporter_name": "SHREE ROAD LINES", "max_permitted_weight": "36000"}])
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        cd = got.container_details[0]
        assert cd["lr_no"] == "LR/2026/88"
        assert cd["transporter_name"] == "SHREE ROAD LINES"
        assert cd["max_permitted_weight"] == "36000"

    def test_vessel_voyage_no_round_trip(self, container, seed):
        inv = make_export(container, seed, vessel_name="MSC ANNA", voyage_no="VOY 214W")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.vessel_name == "MSC ANNA"
        assert got.voyage_no == "VOY 214W"
        # The computed property joins them for the printed cell.
        assert got.vessel_voyage_no == "MSC ANNA / VOY 214W"

    def test_vessel_voyage_no_falls_back_to_whichever_half_is_typed(self, container, seed):
        inv = make_export(container, seed, vessel_name="MSC ANNA")
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.vessel_voyage_no == "MSC ANNA"
        inv2 = make_export(container, seed, export_invoice_number="1000000099", voyage_no="VOY 214W")
        got2 = container.export_invoice_service.get(inv2.id, seed.company_id)
        assert got2.vessel_voyage_no == "VOY 214W"
        inv3 = make_export(container, seed, export_invoice_number="1000000098")
        got3 = container.export_invoice_service.get(inv3.id, seed.company_id)
        assert got3.vessel_voyage_no is None

    def test_weight_totals_and_shipping_bill_round_trip(self, container, seed):
        inv = make_export(
            container, seed, total_net_weight_kg="244019.00", total_gross_weight_kg="248099.00",
            shipping_bill_no="SB-9001",
        )
        got = container.export_invoice_service.get(inv.id, seed.company_id)
        assert got.total_net_weight_kg == 244019.00
        assert got.total_gross_weight_kg == 248099.00
        assert got.shipping_bill_no == "SB-9001"


# ==========================================================================
# Shipping bill PDF + version history
# ==========================================================================
class TestExportPdfAndVersions:
    def test_shipping_bill_pdf_upload_and_remove(self, container, seed):
        inv = container.export_invoice_service.create(
            seed.admin, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "86.70", "export_invoice_number": "1000000002"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}],
            pdf_file=upload())
        assert inv.shipping_bill_pdf_path
        removed = container.export_invoice_service.update(
            seed.admin, inv.id, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "86.70", "export_invoice_number": "1000000002"},
            [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}],
            remove_pdf=True)
        assert removed.shipping_bill_pdf_path is None

    def test_rejects_non_pdf_shipping_bill(self, container, seed):
        with pytest.raises(ValidationError):
            container.export_invoice_service.create(
                seed.admin, {"consignee_name": "R", "invoice_date": "2026-02-20", "exchange_rate": "86.70", "export_invoice_number": "1000000002"},
                [{"product_name": "Tiles", "quantity_value": "100", "unit": "SQM", "price_usd": "5.92"}],
                pdf_file=upload("evil.exe"))

    def test_version_recorded_and_rehydrates(self, container, seed):
        inv = make_export(container, seed)
        versions = container.document_version_service.list_for_document("export_invoice", inv.id)
        assert len(versions) == 1
        doc, ver = container.document_version_service.get_version("export_invoice", inv.id, versions[0].version_number)
        assert doc.export_invoice_number == inv.export_invoice_number
        assert len(doc.items) == 1


# ==========================================================================
# Currency (picked from Administration -> Miscellaneous, snapshotted here)
# ==========================================================================
class TestExportInvoiceCurrency:
    def test_defaults_to_usd_when_nothing_is_picked(self, container, seed):
        invoice = make_export(container, seed)
        assert invoice.currency_code is None
        assert invoice.currency_label == "USD [ $ ]"

    def test_picked_currency_snapshots_its_symbol(self, container, seed):
        container.misc_list_service.create_currency(seed.admin, {"name": "JPY", "symbol": "¥"})
        invoice = make_export(container, seed, currency_code="JPY")
        assert (invoice.currency_code, invoice.currency_symbol) == ("JPY", "¥")
        assert invoice.currency_label == "JPY [ ¥ ]"

    def test_editing_the_list_later_does_not_rewrite_an_issued_invoice(self, container, seed):
        currency = container.misc_list_service.create_currency(seed.admin, {"name": "JPY", "symbol": "¥"})
        invoice = make_export(container, seed, currency_code="JPY")
        container.misc_list_service.update_currency(seed.admin, currency.id, {"name": "JPY", "symbol": "YEN"})
        assert container.export_invoice_service.get(invoice.id, seed.company_id).currency_symbol == "¥"

    def test_a_currency_that_is_not_on_the_list_keeps_its_name_only(self, container, seed):
        invoice = make_export(container, seed, currency_code="XYZ")
        assert (invoice.currency_code, invoice.currency_symbol) == ("XYZ", None)
        assert invoice.currency_label == "XYZ"
