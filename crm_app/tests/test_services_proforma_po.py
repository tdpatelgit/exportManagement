"""
Tests for ProformaInvoiceService and PurchaseOrderService (app/services.py),
plus ClientService.document_feed - the combined document card on the client
page that stitches all four document types together.

The prefill builders are the interesting part: they're what makes
"Quotation -> Proforma Invoice -> Purchase Order" one continuous flow, and a
dropped field there silently loses data the user already typed once.
"""

import pytest

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


def make_lead(container, user):
    return container.lead_service.create_lead(
        user, "Buyer Co", "1", "b@x.com", None, None, None,
        [{"name": "Bob", "is_primary": True}])


def make_quotation(container, seed, lead_id=None):
    fields = {
        "buyer_name": "Buyer Co", "quotation_date": "2026-01-01",
        "buyer_address": "Dubai", "buyer_reference_no": "REF-1",
        "port_of_loading": "Mundra", "port_of_discharge": "Jebel Ali",
        "shipping_terms": "CIF", "payment_terms": "30% advance",
        "sea_freight": "100", "insurance": "50", "discount_amount": "25",
        "bank_name": "HDFC", "remarks": "Handle with care",
    }
    if lead_id:
        fields["lead_id"] = lead_id
    return container.quotation_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2",
          "hsn_code": "6907", "unit": "SQM", "quantity_boxes": "10"}])


# ==========================================================================
# ProformaInvoiceService
# ==========================================================================
class TestProformaPrefill:
    def test_prefill_carries_header_fields_from_quotation(self, container, seed):
        q = make_quotation(container, seed)
        prefill = container.proforma_invoice_service.build_prefill_from_quotation(q)
        f = prefill["fields"]
        assert f["quotation_id"] == q.id
        assert f["consignee_name"] == "Buyer Co"
        assert f["consignee_address"] == "Dubai"
        assert f["buyer_order_no"] == "REF-1"
        assert f["port_of_loading"] == "Mundra"
        assert f["terms_of_delivery"] == "CIF"

    def test_prefill_carries_charges_and_bank(self, container, seed):
        q = make_quotation(container, seed)
        f = container.proforma_invoice_service.build_prefill_from_quotation(q)["fields"]
        assert f["sea_freight"] == 100
        assert f["insurance"] == 50
        assert f["discount_amount"] == 25
        assert f["bank_name"] == "HDFC"

    def test_prefill_copies_line_items_with_prices(self, container, seed):
        q = make_quotation(container, seed)
        items = container.proforma_invoice_service.build_prefill_from_quotation(q)["items"]
        assert len(items) == 1
        assert items[0]["product_name"] == "Tiles"
        assert items[0]["price_usd"] == 2
        assert items[0]["quantity_value"] == 100

    def test_prefill_carries_pallets_from_the_quotation_line(self, container, seed):
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "Buyer Co", "quotation_date": "2026-01-01"},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2", "pallets": "4.5"}])
        items = container.proforma_invoice_service.build_prefill_from_quotation(q)["items"]
        assert items[0]["pallets"] == 4.5

    def test_prefill_pallets_is_none_when_the_quotation_line_has_none(self, container, seed):
        q = make_quotation(container, seed)
        items = container.proforma_invoice_service.build_prefill_from_quotation(q)["items"]
        assert items[0]["pallets"] is None

    def test_generated_invoice_persists_the_carried_over_pallets(self, container, seed):
        """End to end: the number surviving the prefill dict also survives
        being submitted back through create() as a real PI item."""
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "Buyer Co", "quotation_date": "2026-01-01"},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2", "pallets": "4.5"}])
        prefill = container.proforma_invoice_service.build_prefill_from_quotation(q)
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01",
                        **prefill["fields"]},
            prefill["items"])
        assert pi.items[0].pallets == 4.5


class TestProformaCrud:
    def _create(self, container, seed, **over):
        fields = {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"}
        fields.update(over)
        return container.proforma_invoice_service.create(
            seed.admin, fields,
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])

    def test_create_assigns_number(self, container, seed):
        pi = self._create(container, seed)
        assert pi.id is not None
        assert pi.invoice_number.startswith("PI20260201")

    def test_create_persists_items_and_totals(self, container, seed):
        # Same upward ladder as the quotation: the typed price is always FOB,
        # and CIF is built by adding the charges back onto it (see
        # ProformaInvoice.cif_value_usd).
        pi = self._create(container, seed, sea_freight="10", discount_amount="5")
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert reloaded.subtotal_usd == 200.0
        assert reloaded.cif_value_usd == 210.0       # 200 FOB total + 10 sea freight
        assert reloaded.invoice_value_usd == 205.0   # 210 - 5 discount
        assert reloaded.fob_value_usd == 200.0       # always just the goods total

    def test_fob_pricing_is_never_revived_even_if_posted(self, container, seed):
        """Proforma invoices no longer have an FOB-typed-price mode - a stale
        client (or a direct API call) posting the old fob_pricing checkbox
        must not trigger the removed uplift. The typed price is always the
        absolute FOB price, exactly as if fob_pricing had never existed. See
        ExportInvoiceService, which took over the "Prices typed above are
        FOB" checkbox."""
        pi = container.proforma_invoice_service.create(
            seed.admin,
            {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01",
             "terms_of_delivery": "CIF", "sea_freight": "100", "fob_pricing": "1"},
            [{"product_name": "Tiles", "quantity_value": "3", "price_usd": "7"}])
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        item = reloaded.items[0]
        assert reloaded.fob_pricing is False
        assert item.fob_price_usd is None
        assert item.price_usd == 7.0
        assert item.total_usd == 21.0
        assert reloaded.cif_value_usd == pytest.approx(121.0)  # 21 FOB total + 100 sea freight

    def test_without_fob_pricing_the_proforma_prices_are_untouched(self, container, seed):
        pi = self._create(container, seed, sea_freight="10")
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert (reloaded.fob_pricing, reloaded.round_off) == (False, 0)
        assert reloaded.items[0].price_usd == 2.0
        assert reloaded.items[0].fob_price_usd is None

    def test_fob_terms_drop_sea_freight_and_insurance(self, container, seed):
        # FOB puts the ocean leg on the buyer, so both charges are stored as
        # zero even if the form posts them. Certification/other charges still
        # add onto the FOB total to reach the invoice value (see
        # ProformaInvoice.cif_value_usd) - it's only sea freight/insurance
        # that FOB holds at zero.
        pi = self._create(container, seed, terms_of_delivery="FOB MUNDRA",
                          sea_freight="10", insurance="20", certification="15",
                          other_charges="5")
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert reloaded.sea_freight == 0
        assert reloaded.insurance == 0
        assert reloaded.certification == 15         # never dropped by any term
        assert reloaded.invoice_value_usd == 220.0  # 200 FOB total + 15 certification + 5 other charges
        assert reloaded.fob_value_usd == 200.0      # always just the goods total

    def test_cfr_terms_drop_only_the_insurance(self, container, seed):
        # CFR = cost AND FREIGHT: the seller keeps paying the freight, the
        # buyer insures the cargo. Certification is only dropped under FOB
        # (see drops_certification in app/utils.py), so CFR carries it same
        # as sea freight.
        pi = self._create(container, seed, terms_of_delivery="CFR BEIRA",
                          sea_freight="10", insurance="20", certification="15")
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert reloaded.sea_freight == 10
        assert reloaded.insurance == 0
        assert reloaded.certification == 15
        assert reloaded.cif_value_usd == 225.0  # 200 FOB total + 10 freight + 15 certification
        assert reloaded.fob_value_usd == 200.0  # always just the goods total

    def test_create_records_a_version(self, container, seed):
        pi = self._create(container, seed)
        versions = container.document_version_service.list_for_document(
            "proforma_invoice", pi.id)
        assert len(versions) == 1

    def test_update_keeps_number_and_adds_version(self, container, seed):
        pi = self._create(container, seed)
        container.proforma_invoice_service.update(
            seed.admin, pi.id, {"consignee_name": "Renamed", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "50", "price_usd": "4"}])
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert reloaded.consignee_name == "Renamed"
        assert reloaded.invoice_number == pi.invoice_number
        assert len(container.document_version_service.list_for_document(
            "proforma_invoice", pi.id)) == 2

    def test_get_for_quotation_links_back(self, container, seed):
        q = make_quotation(container, seed)
        pi = self._create(container, seed, quotation_id=q.id)
        found = container.proforma_invoice_service.get_for_quotation(q.id)
        assert found is not None and found.id == pi.id

    def test_cross_company_get_is_not_found(self, container, seed):
        pi = self._create(container, seed)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.proforma_invoice_service.get(pi.id, other.id)

    def test_delete(self, container, seed):
        pi = self._create(container, seed)
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        with pytest.raises(NotFoundError):
            container.proforma_invoice_service.get(pi.id, seed.company_id)

    def test_generating_proforma_advances_client_status(self, container, seed):
        # A proforma invoice has no lead_id of its own - advance_client_status
        # resolves the lead by walking up quotation_id to the Quotation, which
        # is the only document type that still carries lead_id directly.
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        q = make_quotation(container, seed, lead_id=lead.id)
        self._create(container, seed, quotation_id=q.id)
        reloaded = container.buyer_service.get(client.id, seed.company_id)
        assert reloaded.status == "purchase_order_submission_pending"


# ==========================================================================
# PurchaseOrderService
# ==========================================================================
class TestPurchaseOrder:
    def _create(self, container, seed, item=None, **over):
        fields = {"seller_name": "Supplier Ltd", "po_date": "2026-03-01"}
        fields.update(over)
        return container.purchase_order_service.create(
            seed.admin, fields,
            [item or {"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
                      "price_inr": "500", "price_per": "BOX"}])

    def _our_gstin(self, container, seed, gstin):
        container.company_repo.upsert(seed.company_id, "Test Exports", "Morbi", gstin, "", "", "")

    def _taxed_product(self, container, seed, igst=18):
        return container.product_service.create_product(
            current_user=seed.admin, product_name="Tiles", description="", hsn_code="6907",
            igst_percent=str(igst), quantity="", alternate_quantity="")

    def _line(self, product_id):
        return {"product_id": str(product_id), "product_name": "Tiles", "quantity_boxes": "10",
                "quantity_value": "100", "price_inr": "500", "price_per": "BOX"}

    def test_create_assigns_number(self, container, seed):
        po = self._create(container, seed)
        assert po.id is not None and "20260301" in po.po_number

    def test_full_tax_purchase_takes_the_rate_from_the_product(self, container, seed):
        self._our_gstin(container, seed, "24AAAAA0000A1Z5")
        product = self._taxed_product(container, seed, igst=18)
        po = self._create(container, seed, item=self._line(product.id),
                          purchase_type="full_tax", seller_gstin="27BBBBB0000B1Z5")
        reloaded = container.purchase_order_service.get(po.id, seed.company_id)
        assert reloaded.subtotal_inr == 5000.0     # 10 boxes x 500
        assert reloaded.igst_percent == 18         # another state -> IGST alone
        assert (reloaded.cgst_percent, reloaded.sgst_percent) == (0, 0)
        assert reloaded.igst_amount == 900.0       # 18% of 5000
        assert reloaded.order_value_inr == 5900.0

    def test_same_state_splits_the_rate_into_cgst_and_sgst(self, container, seed):
        self._our_gstin(container, seed, "24AAAAA0000A1Z5")
        product = self._taxed_product(container, seed, igst=18)
        po = self._create(container, seed, item=self._line(product.id),
                          purchase_type="full_tax", seller_gstin="24BBBBB0000B1Z5")
        assert po.igst_percent == 0
        assert (po.cgst_percent, po.sgst_percent) == (9, 9)
        assert po.order_value_inr == 5900.0        # same total, split differently

    def test_exemption_uses_the_concessional_rate(self, container, seed):
        self._our_gstin(container, seed, "24AAAAA0000A1Z5")
        product = self._taxed_product(container, seed, igst=18)  # ignored under exemption
        po = self._create(container, seed, item=self._line(product.id),
                          purchase_type="exemption", seller_gstin="27BBBBB0000B1Z5")
        assert po.igst_percent == 0.1
        assert (po.cgst_percent, po.sgst_percent) == (0, 0)

    def test_exemption_within_one_state_halves_into_cgst_and_sgst(self, container, seed):
        self._our_gstin(container, seed, "24AAAAA0000A1Z5")
        po = self._create(container, seed, purchase_type="exemption", seller_gstin="24BBBBB0000B1Z5")
        assert po.igst_percent == 0
        assert (po.cgst_percent, po.sgst_percent) == (0.05, 0.05)

    def test_missing_gstins_are_treated_as_inter_state(self, container, seed):
        product = self._taxed_product(container, seed, igst=18)
        po = self._create(container, seed, item=self._line(product.id), purchase_type="full_tax")
        assert po.igst_percent == 18
        assert (po.cgst_percent, po.sgst_percent) == (0, 0)

    def test_typed_percentages_are_ignored(self, container, seed):
        """The form only displays the rates - a posted one is never trusted."""
        po = self._create(container, seed, igst_percent="18", cgst_percent="9")
        assert (po.igst_percent, po.cgst_percent, po.sgst_percent) == (0, 0, 0)

    def test_unknown_purchase_type_is_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            self._create(container, seed, purchase_type="no_tax_at_all")

    def test_prefill_from_proforma(self, container, seed):
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2",
              "hsn_code": "6907"}])
        prefill = container.purchase_order_service.build_prefill_from_proforma(pi)
        assert prefill["fields"]["proforma_invoice_id"] == pi.id
        assert len(prefill["items"]) == 1
        assert prefill["items"][0]["product_name"] == "Tiles"

    def test_list_for_proforma_links_back(self, container, seed):
        """One invoice can be ordered from several suppliers, so the link
        back is a list - newest PO first."""
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01"},
            [{"product_name": "T", "quantity_value": "1", "price_usd": "1"}])
        first = self._create(container, seed, proforma_invoice_id=pi.id)
        second = self._create(container, seed, proforma_invoice_id=pi.id)
        found = container.purchase_order_service.list_for_proforma(pi.id, seed.company_id)
        assert [po.id for po in found] == [second.id, first.id]
        assert container.purchase_order_service.count_map_by_proforma(seed.company_id)[pi.id] == 2

    def test_list_for_proforma_is_company_scoped(self, container, seed):
        assert container.purchase_order_service.list_for_proforma(None, seed.company_id) == []

    def test_create_records_a_version(self, container, seed):
        po = self._create(container, seed)
        assert len(container.document_version_service.list_for_document(
            "purchase_order", po.id)) == 1

    def test_delete(self, container, seed):
        po = self._create(container, seed)
        container.purchase_order_service.delete(seed.admin, po.id)
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, seed.company_id)

    def test_cross_company_get_is_not_found(self, container, seed):
        po = self._create(container, seed)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, other.id)


# ==========================================================================
# PurchaseOrderProductionService
# ==========================================================================
class TestPurchaseOrderProduction:
    def _po(self, container, seed, items=None):
        return container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier Ltd", "po_date": "2026-03-01"},
            items or [{"product_name": "Tiles", "design_name": "Carrara", "quantity_boxes": "10",
                       "quantity_value": "100", "price_inr": "500", "price_per": "BOX"}])

    def _batch(self, **over):
        batch = {"batch_number": "B-101", "production_date": "2026-03-05",
                 "quantity_boxes": "4", "remarks": "first kiln run"}
        batch.update(over)
        return batch

    def test_rows_default_to_pending_with_no_batches(self, container, seed):
        po = self._po(container, seed)
        rows = container.purchase_order_production_service.get_rows(po.id, seed.company_id)
        assert len(rows) == 1
        # No packing list, so no design breakdown - the line stands alone and
        # is labelled by its product.
        assert rows[0]["design_id"] is None and rows[0]["design_name"] is None
        assert rows[0]["design_label"] == "Tiles"
        assert rows[0]["status"] == "pending"
        assert rows[0]["batches"] == [] and rows[0]["produced_boxes"] == 0

    def test_status_and_batches_round_trip(self, container, seed):
        po = self._po(container, seed)
        item_id = po.items[0].id
        container.purchase_order_production_service.save_row(
            po.id, item_id, None, None, "ready",
            [self._batch(), self._batch(batch_number="B-102", quantity_boxes="6", remarks="")],
            seed.company_id, seed.admin.id)
        row = container.purchase_order_production_service.get_rows(po.id, seed.company_id)[0]
        assert row["status"] == "ready" and row["status_label"] == "Ready"
        assert [b.batch_number for b in row["batches"]] == ["B-101", "B-102"]
        assert row["produced_boxes"] == 10          # 4 + 6, the full ordered qty
        assert row["updated_by_name"] == "Ada Admin"

    def test_saving_again_replaces_the_batches(self, container, seed):
        po = self._po(container, seed)
        service = container.purchase_order_production_service
        service.save_row(po.id, po.items[0].id, None, None, "in_production",
                         [self._batch()], seed.company_id, seed.admin.id)
        service.save_row(po.id, po.items[0].id, None, None, "ready",
                         [self._batch(batch_number="B-999", quantity_boxes="10")],
                         seed.company_id, seed.admin.id)
        row = service.get_rows(po.id, seed.company_id)[0]
        assert [b.batch_number for b in row["batches"]] == ["B-999"]

    def test_blank_batch_rows_are_dropped(self, container, seed):
        """The form always carries one spare row - saving with it untouched
        must not store an empty batch."""
        po = self._po(container, seed)
        container.purchase_order_production_service.save_row(
            po.id, po.items[0].id, None, None, "pending",
            [self._batch(), {"batch_number": "", "production_date": "", "quantity_boxes": "", "remarks": ""}],
            seed.company_id, seed.admin.id)
        assert len(container.purchase_order_production_service.get_rows(
            po.id, seed.company_id)[0]["batches"]) == 1

    def test_unknown_status_is_rejected(self, container, seed):
        po = self._po(container, seed)
        with pytest.raises(ValidationError):
            container.purchase_order_production_service.save_row(
                po.id, po.items[0].id, None, None, "almost_ready", [],
                seed.company_id, seed.admin.id)

    def test_non_numeric_batch_quantity_is_rejected(self, container, seed):
        po = self._po(container, seed)
        with pytest.raises(ValidationError):
            container.purchase_order_production_service.save_row(
                po.id, po.items[0].id, None, None, "ready",
                [self._batch(quantity_boxes="ten")], seed.company_id, seed.admin.id)

    def test_a_line_from_another_purchase_order_is_not_found(self, container, seed):
        first, second = self._po(container, seed), self._po(container, seed)
        with pytest.raises(NotFoundError):
            container.purchase_order_production_service.save_row(
                second.id, first.items[0].id, None, None, "ready", [],
                seed.company_id, seed.admin.id)

    def test_another_companys_order_is_not_found(self, container, seed):
        po = self._po(container, seed)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.purchase_order_production_service.get_rows(po.id, other.id)

    def test_designs_come_from_the_proforma_invoices_packing_list(self, container, seed):
        """A purchase order orders by PRODUCT - one line of 10 boxes of Tiles.
        Which designs those boxes are is settled on the linked proforma
        invoice's packing list, so the card must split that one line into a
        row per design, each carrying its own share of the boxes."""
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-02-02", "proforma_invoice_id": pi.id},
            [{"product_name": "Tiles", "design_name": "Carrara", "quantity_boxes": "6"},
             {"product_name": "Tiles", "design_name": "Statuario", "quantity_boxes": "4"}])
        po = container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier Ltd", "po_date": "2026-03-01",
                         "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
              "price_inr": "500", "price_per": "BOX"}])

        design_rows = container.packing_list_service.list_for_proforma(pi.id, seed.company_id)[0].items
        rows = container.purchase_order_production_service.get_rows(
            po.id, seed.company_id, design_rows)
        assert [(r["design_label"], r["ordered_boxes"]) for r in rows] == [
            ("Carrara", 6), ("Statuario", 4)]

        # Each design carries its own status and batches, independently.
        container.purchase_order_production_service.save_row(
            po.id, rows[0]["item_id"], rows[0]["design_id"], "Carrara", "ready",
            [self._batch(quantity_boxes="6")], seed.company_id, seed.admin.id)
        reloaded = container.purchase_order_production_service.get_rows(
            po.id, seed.company_id, design_rows)
        assert [r["status"] for r in reloaded] == ["ready", "pending"]
        assert reloaded[0]["produced_boxes"] == 6 and reloaded[1]["batches"] == []

    def test_summary_map_counts_ready_lines(self, container, seed):
        po = self._po(container, seed, items=[
            {"product_name": "Tiles", "design_name": "Carrara", "quantity_boxes": "10",
             "quantity_value": "100", "price_inr": "500", "price_per": "BOX"},
            {"product_name": "Tiles", "design_name": "Statuario", "quantity_boxes": "5",
             "quantity_value": "50", "price_inr": "500", "price_per": "BOX"},
        ])
        container.purchase_order_production_service.save_row(
            po.id, po.items[0].id, None, None, "ready", [], seed.company_id, seed.admin.id)
        summary = container.purchase_order_production_service.summary_map(seed.company_id)
        assert summary[po.id] == {"ready": 1, "total": 2}

    def test_summary_counts_designs_not_lines_when_there_is_a_packing_list(self, container, seed):
        """One PO line of Tiles that the packing list splits into two designs
        is two rows on the card, so marking one Ready is 1/2 - counting the
        LINE would have shown a misleading 1/1."""
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-02-02", "proforma_invoice_id": pi.id},
            [{"product_name": "Tiles", "design_name": "Carrara", "quantity_boxes": "6"},
             {"product_name": "Tiles", "design_name": "Statuario", "quantity_boxes": "4"}])
        po = container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier Ltd", "po_date": "2026-03-01",
                         "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
              "price_inr": "500", "price_per": "BOX"}])
        service = container.purchase_order_production_service
        assert service.summary_map(seed.company_id)[po.id] == {"ready": 0, "total": 2}
        service.save_row(po.id, po.items[0].id, None, "Carrara", "ready", [],
                         seed.company_id, seed.admin.id)
        assert service.summary_map(seed.company_id)[po.id] == {"ready": 1, "total": 2}

    def test_editing_the_order_keeps_the_production_history(self, container, seed):
        """PurchaseOrderRepository._replace_items deletes and re-inserts every
        line on save, and production rows cascade off those ids - they have to
        be carried across by sr_no or a plain re-save wipes them."""
        po = self._po(container, seed)
        container.purchase_order_production_service.save_row(
            po.id, po.items[0].id, None, None, "ready", [self._batch()],
            seed.company_id, seed.admin.id)
        container.purchase_order_service.update(
            seed.admin, po.id, {"seller_name": "Supplier Ltd", "po_date": "2026-03-02"},
            [{"product_name": "Tiles", "design_name": "Carrara", "quantity_boxes": "12",
              "quantity_value": "120", "price_inr": "500", "price_per": "BOX"}])
        row = container.purchase_order_production_service.get_rows(po.id, seed.company_id)[0]
        assert row["status"] == "ready"
        assert [b.batch_number for b in row["batches"]] == ["B-101"]


# ==========================================================================
# ClientService.document_feed
# ==========================================================================
class TestDocumentFeed:
    def test_empty_for_a_client_with_no_documents(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        assert container.buyer_service.document_feed(client) == []

    def test_includes_manual_document_entries(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        container.buyer_service.add_document(
            client.id, seed.admin, "Contract.pdf", "Contract", "2026-01-05", "signed")
        feed = container.buyer_service.document_feed(client)
        assert any(r["name"] == "Contract.pdf" and r["type"] == "Contract" for r in feed)

    def test_includes_quotations_made_against_the_lead(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        q = make_quotation(container, seed, lead_id=lead.id)
        feed = container.buyer_service.document_feed(client)
        row = next(r for r in feed if r["type"] == "Quotation")
        assert row["name"] == q.quotation_number
        assert row["link"][0] == "quotations.view_quotation"

    def test_includes_all_four_document_types(self, container, seed):
        # Only the Quotation carries lead_id directly - the Proforma
        # Invoice/Purchase Order/Packing List are found by document_feed
        # walking UP their own quotation_id/proforma_invoice_id chain to it.
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        q = make_quotation(container, seed, lead_id=lead.id)
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01",
                         "quotation_id": q.id},
            [{"product_name": "T", "quantity_value": "1", "price_usd": "1"}])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "S", "po_date": "2026-03-01", "proforma_invoice_id": pi.id},
            [{"product_name": "T", "quantity_boxes": "1", "quantity_value": "1",
              "price_inr": "10", "price_per": "BOX"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-04-01", "proforma_invoice_id": pi.id},
            [{"product_name": "T", "quantity_boxes": "1"}])

        types = {r["type"] for r in container.buyer_service.document_feed(client)}
        assert types == {"Quotation", "Proforma Invoice", "Purchase Order", "Packing List"}

    def test_feed_is_sorted_newest_first(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        container.buyer_service.add_document(
            client.id, seed.admin, "Old", "Note", "2026-01-01", "")
        container.buyer_service.add_document(
            client.id, seed.admin, "New", "Note", "2026-12-31", "")
        feed = container.buyer_service.document_feed(client)
        dates = [r["date"] for r in feed]
        assert dates == sorted(dates, reverse=True)

    def test_client_without_lead_shows_only_manual_entries(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        make_quotation(container, seed, lead_id=lead.id)
        client.lead_id = None  # simulate a client with no originating lead
        assert container.buyer_service.document_feed(client) == []


# ==========================================================================
# add_document validation
# ==========================================================================
class TestAddDocument:
    def _client(self, container, seed):
        lead = make_lead(container, seed.admin)
        return container.buyer_service.convert_lead(lead.id, seed.admin)

    def test_requires_name(self, container, seed):
        client = self._client(container, seed)
        with pytest.raises(ValidationError):
            container.buyer_service.add_document(
                client.id, seed.admin, "  ", "Contract", "2026-01-01", "")

    def test_requires_type(self, container, seed):
        client = self._client(container, seed)
        with pytest.raises(ValidationError):
            container.buyer_service.add_document(
                client.id, seed.admin, "Doc", "  ", "2026-01-01", "")

    def test_blank_date_defaults_to_today(self, container, seed):
        from datetime import date
        client = self._client(container, seed)
        doc = container.buyer_service.add_document(
            client.id, seed.admin, "Doc", "Contract", "", "")
        assert doc.document_date == date.today().isoformat()
