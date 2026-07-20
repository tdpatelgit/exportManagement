"""
Tests for the document services: Quotation, ProformaInvoice, PurchaseOrder,
PackingList, and DocumentVersion (app/services.py).

These cover the parts most likely to break silently on a refactor:
  - document number generation (per-company, per-day sequence)
  - line-item building: totals, the "boxes x product alt-qty" server-side
    recompute, and the validation guards
  - create/update round-trips that also snapshot a version
  - lead->in_client auto-advance on first quotation
"""

import pytest

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def make_lead(container, user):
    return container.lead_service.create_lead(
        user, "Buyer Co", "1", "b@x.com", None, None, None,
        [{"name": "Bob", "is_primary": True}])


def make_product(container, admin, alt_qty="1.44"):
    return container.product_service.create_product(
        admin, product_name="Tiles", description="", hsn_code="6907",
        igst_percent="18", quantity="10", alternate_quantity=alt_qty)


# ==========================================================================
# QuotationService
# ==========================================================================
class TestQuotationNumber:
    def test_number_format_and_daily_sequence(self, container, seed):
        svc = container.quotation_service
        n1 = svc._generate_number(seed.company_id, "2026-07-02")
        assert n1 == "QT20260702001"
        # After one exists for that day, the next increments.
        svc.create(seed.admin,
                   {"buyer_name": "B", "quotation_date": "2026-07-02"},
                   [{"product_name": "P", "quantity_value": "1", "price_usd": "1"}])
        n2 = svc._generate_number(seed.company_id, "2026-07-02")
        assert n2 == "QT20260702002"


class TestQuotationBuildItems:
    def test_totals_computed_from_qty_times_price(self, container, seed):
        items = container.quotation_service._build_items(
            seed.company_id,
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2.5"}])
        assert items[0].total_usd == 25.0

    def test_boxes_times_alt_qty_overrides_client_quantity(self, container, seed):
        product = make_product(container, seed.admin, alt_qty="1.5")
        items = container.quotation_service._build_items(
            seed.company_id,
            [{"product_name": "Tiles", "product_id": str(product.id),
              "quantity_boxes": "10", "quantity_value": "999", "price_usd": "2"}])
        # 10 boxes * 1.5 = 15, NOT the client-sent 999
        assert items[0].quantity_value == 15.0
        assert items[0].total_usd == 30.0

    def test_foreign_product_id_is_dropped(self, container, seed):
        other = container.tenant_repo.create("Other", "other")
        other_admin = container.auth_service.create_user(other.id, "oadmin", "pw123456", "O", "admin")
        foreign = make_product(container, other_admin)
        items = container.quotation_service._build_items(
            seed.company_id,
            [{"product_name": "Tiles", "product_id": str(foreign.id),
              "quantity_value": "5", "price_usd": "1"}])
        assert items[0].product_id is None  # not trusted across companies

    def test_blank_product_name_row_skipped(self, container, seed):
        items = container.quotation_service._build_items(
            seed.company_id,
            [{"product_name": "  ", "quantity_value": "1", "price_usd": "1"},
             {"product_name": "Real", "quantity_value": "2", "price_usd": "3"}])
        assert len(items) == 1 and items[0].product_name == "Real"

    def test_no_valid_items_raises(self, container, seed):
        with pytest.raises(ValidationError):
            container.quotation_service._build_items(seed.company_id, [{"product_name": ""}])

    def test_zero_quantity_raises(self, container, seed):
        with pytest.raises(ValidationError):
            container.quotation_service._build_items(
                seed.company_id,
                [{"product_name": "P", "quantity_value": "0", "price_usd": "1"}])

    def test_negative_price_raises(self, container, seed):
        with pytest.raises(ValidationError):
            container.quotation_service._build_items(
                seed.company_id,
                [{"product_name": "P", "quantity_value": "1", "price_usd": "-1"}])

    def test_non_numeric_raises(self, container, seed):
        with pytest.raises(ValidationError):
            container.quotation_service._build_items(
                seed.company_id,
                [{"product_name": "P", "quantity_value": "lots", "price_usd": "1"}])


class TestQuotationCrud:
    def _create(self, container, seed, **fld):
        fields = {"buyer_name": "Buyer", "quotation_date": "2026-01-01"}
        fields.update(fld)
        return container.quotation_service.create(
            seed.admin, fields,
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2"}])

    def test_create_assigns_number_and_persists(self, container, seed):
        q = self._create(container, seed)
        assert q.id is not None and q.quotation_number.startswith("QT")
        assert container.quotation_service.get(q.id, seed.company_id).buyer_name == "Buyer"

    def test_create_requires_buyer_name(self, container, seed):
        with pytest.raises(ValidationError):
            container.quotation_service.create(
                seed.admin, {"buyer_name": ""},
                [{"product_name": "P", "quantity_value": "1", "price_usd": "1"}])

    def test_create_records_a_version(self, container, seed):
        q = self._create(container, seed)
        versions = container.document_version_service.list_for_document("quotation", q.id)
        assert len(versions) == 1
        assert versions[0].version_number == 1

    def test_update_adds_second_version_same_number(self, container, seed):
        q = self._create(container, seed)
        container.quotation_service.update(
            seed.admin, q.id, {"buyer_name": "Buyer 2", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "5", "price_usd": "3"}])
        reloaded = container.quotation_service.get(q.id, seed.company_id)
        assert reloaded.buyer_name == "Buyer 2"
        assert reloaded.quotation_number == q.quotation_number  # number never changes
        versions = container.document_version_service.list_for_document("quotation", q.id)
        assert len(versions) == 2

    def test_first_quotation_advances_lead_to_in_client(self, container, seed):
        lead = make_lead(container, seed.admin)
        container.quotation_service.create(
            seed.admin, {"buyer_name": "Buyer", "quotation_date": "2026-01-01",
                         "lead_id": lead.id},
            [{"product_name": "P", "quantity_value": "1", "price_usd": "1"}])
        assert container.lead_service.get(lead.id, seed.company_id).status == "in_client"

    def test_delete_quotation(self, container, seed):
        q = self._create(container, seed)
        container.quotation_service.delete(seed.admin, q.id)
        with pytest.raises(NotFoundError):
            container.quotation_service.get(q.id, seed.company_id)


# ==========================================================================
# ProformaInvoice / PurchaseOrder / PackingList number formats
# ==========================================================================
class TestOtherDocumentNumbers:
    def test_proforma_number_prefix(self, container, seed):
        n = container.proforma_invoice_service._generate_number(seed.company_id, "2026-07-02")
        assert n.startswith("PI20260702")

    def test_purchase_order_number_prefix(self, container, seed):
        n = container.purchase_order_service._generate_number(seed.company_id, "2026-07-02")
        assert n[:2] in ("PO",) and "20260702" in n

    def test_packing_list_number_prefix(self, container, seed):
        n = container.packing_list_service._generate_number(seed.company_id, "2026-07-02")
        assert "20260702" in n


# ==========================================================================
# PurchaseOrderService line items (INR, price_per basis)
# ==========================================================================
class TestPurchaseOrderBuildItems:
    def test_build_items_totals(self, container, seed):
        items = container.purchase_order_service._build_items(
            seed.company_id,
            [{"product_name": "P", "quantity_boxes": "10", "quantity_value": "10",
              "price_inr": "100", "price_per": "BOX"}])
        assert len(items) == 1
        assert items[0].total_inr > 0

    def test_no_items_raises(self, container, seed):
        with pytest.raises(ValidationError):
            container.purchase_order_service._build_items(seed.company_id, [{"product_name": ""}])


# ==========================================================================
# DocumentVersionService
# ==========================================================================
class TestDocumentVersionService:
    def test_get_specific_version_rehydrates_document(self, container, seed):
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "Buyer", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "1", "price_usd": "1"}])
        # Returns (rehydrated document dataclass, DocumentVersion).
        document, version = container.document_version_service.get_version("quotation", q.id, 1)
        assert version.version_number == 1
        assert version.document_number == q.quotation_number
        assert document.buyer_name == "Buyer"
        # Rehydrated into a real Quotation, so computed props still work.
        assert document.subtotal_usd == 1.0

    def test_missing_version_raises_not_found(self, container, seed):
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "Buyer", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "1", "price_usd": "1"}])
        with pytest.raises(NotFoundError):
            container.document_version_service.get_version("quotation", q.id, 99)
