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
        pi = self._create(container, seed, sea_freight="10", discount_amount="5")
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert reloaded.subtotal_usd == 200.0
        assert reloaded.invoice_value_usd == 205.0  # 200 + 10 - 5

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
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        self._create(container, seed, lead_id=lead.id)
        reloaded = container.client_service.get(client.id, seed.company_id)
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
# ClientService.document_feed
# ==========================================================================
class TestDocumentFeed:
    def test_empty_for_a_client_with_no_documents(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        assert container.client_service.document_feed(client) == []

    def test_includes_manual_document_entries(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        container.client_service.add_document(
            client.id, seed.admin, "Contract.pdf", "Contract", "2026-01-05", "signed")
        feed = container.client_service.document_feed(client)
        assert any(r["name"] == "Contract.pdf" and r["type"] == "Contract" for r in feed)

    def test_includes_quotations_made_against_the_lead(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        q = make_quotation(container, seed, lead_id=lead.id)
        feed = container.client_service.document_feed(client)
        row = next(r for r in feed if r["type"] == "Quotation")
        assert row["name"] == q.quotation_number
        assert row["link"][0] == "quotations.view_quotation"

    def test_includes_all_four_document_types(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        make_quotation(container, seed, lead_id=lead.id)
        container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01",
                         "lead_id": lead.id},
            [{"product_name": "T", "quantity_value": "1", "price_usd": "1"}])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "S", "po_date": "2026-03-01", "lead_id": lead.id},
            [{"product_name": "T", "quantity_boxes": "1", "quantity_value": "1",
              "price_inr": "10", "price_per": "BOX"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-04-01", "lead_id": lead.id},
            [{"product_name": "T", "quantity_boxes": "1"}])

        types = {r["type"] for r in container.client_service.document_feed(client)}
        assert types == {"Quotation", "Proforma Invoice", "Purchase Order", "Packing List"}

    def test_feed_is_sorted_newest_first(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        container.client_service.add_document(
            client.id, seed.admin, "Old", "Note", "2026-01-01", "")
        container.client_service.add_document(
            client.id, seed.admin, "New", "Note", "2026-12-31", "")
        feed = container.client_service.document_feed(client)
        dates = [r["date"] for r in feed]
        assert dates == sorted(dates, reverse=True)

    def test_client_without_lead_shows_only_manual_entries(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        make_quotation(container, seed, lead_id=lead.id)
        client.lead_id = None  # simulate a client with no originating lead
        assert container.client_service.document_feed(client) == []


# ==========================================================================
# add_document validation
# ==========================================================================
class TestAddDocument:
    def _client(self, container, seed):
        lead = make_lead(container, seed.admin)
        return container.client_service.convert_lead(lead.id, seed.admin)

    def test_requires_name(self, container, seed):
        client = self._client(container, seed)
        with pytest.raises(ValidationError):
            container.client_service.add_document(
                client.id, seed.admin, "  ", "Contract", "2026-01-01", "")

    def test_requires_type(self, container, seed):
        client = self._client(container, seed)
        with pytest.raises(ValidationError):
            container.client_service.add_document(
                client.id, seed.admin, "Doc", "  ", "2026-01-01", "")

    def test_blank_date_defaults_to_today(self, container, seed):
        from datetime import date
        client = self._client(container, seed)
        doc = container.client_service.add_document(
            client.id, seed.admin, "Doc", "Contract", "", "")
        assert doc.document_date == date.today().isoformat()
