"""
Regression tests for a real bug: quotations, proforma invoices and purchase
orders could never be deleted once some other document had been generated
"from" them (a proforma invoice from a quotation, a purchase order or
packing list from a proforma invoice, a packing list from a purchase
order). Those links (quotation_id / proforma_invoice_id / purchase_order_id
on a downstream document) are documented in schema.sql as "generated from
reference only, not an ownership link" - but the columns never had an
ON DELETE clause, so with `PRAGMA foreign_keys = ON` (always on) SQLite
rejected the delete outright with IntegrityError the moment any downstream
document existed.

The fix nulls out every downstream reference in the same transaction right
before the delete, so the ancestor can always be removed and the downstream
document survives as a standalone record (exactly what "reference only"
already promised) instead of the delete crashing.
"""

import pytest

from app.exceptions import NotFoundError


def make_quotation(container, seed):
    return container.quotation_service.create(
        seed.admin, {"buyer_name": "Buyer Co", "quotation_date": "2026-01-01"},
        [{"product_name": "Tiles", "quantity_value": "10", "price_usd": "1"}])


def make_proforma(container, seed, **over):
    fields = {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"}
    fields.update(over)
    return container.proforma_invoice_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "quantity_value": "10", "price_usd": "1"}])


def make_purchase_order(container, seed, **over):
    fields = {"seller_name": "Supplier Co", "po_date": "2026-02-02"}
    fields.update(over)
    return container.purchase_order_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "10", "price_inr": "5"}])


def make_packing_list(container, seed, **over):
    fields = {"consignee_name": "Buyer Co", "packing_list_date": "2026-02-03"}
    fields.update(over)
    return container.packing_list_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "10", "unit": "SQM"}])


class TestDeletingAQuotationWithDownstreamDocuments:
    def test_delete_succeeds_when_a_proforma_invoice_was_generated_from_it(self, container, seed):
        q = make_quotation(container, seed)
        pi = make_proforma(container, seed, quotation_id=q.id)
        container.quotation_service.delete(seed.admin, q.id)
        with pytest.raises(NotFoundError):
            container.quotation_service.get(q.id, seed.company_id)
        # The proforma invoice survives - it just loses the breadcrumb.
        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        assert reloaded.quotation_id is None

    def test_delete_succeeds_when_a_packing_list_was_generated_from_it(self, container, seed):
        q = make_quotation(container, seed)
        pl = make_packing_list(container, seed, quotation_id=q.id)
        container.quotation_service.delete(seed.admin, q.id)
        reloaded = container.packing_list_service.get(pl.id, seed.company_id)
        assert reloaded.quotation_id is None


class TestDeletingAProformaInvoiceWithDownstreamDocuments:
    def test_delete_succeeds_when_a_purchase_order_was_generated_from_it(self, container, seed):
        pi = make_proforma(container, seed)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        with pytest.raises(NotFoundError):
            container.proforma_invoice_service.get(pi.id, seed.company_id)
        reloaded = container.purchase_order_service.get(po.id, seed.company_id)
        assert reloaded.proforma_invoice_id is None

    def test_delete_succeeds_when_a_packing_list_was_generated_from_it(self, container, seed):
        pi = make_proforma(container, seed)
        pl = make_packing_list(container, seed, proforma_invoice_id=pi.id)
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        reloaded = container.packing_list_service.get(pl.id, seed.company_id)
        assert reloaded.proforma_invoice_id is None

    def test_delete_succeeds_with_several_linked_purchase_orders_at_once(self, container, seed):
        """The many-POs-per-PI case: deleting the PI must not be blocked by
        (or destroy) any of them."""
        pi = make_proforma(container, seed)
        pos = [make_purchase_order(container, seed, proforma_invoice_id=pi.id) for _ in range(3)]
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        for po in pos:
            assert container.purchase_order_service.get(po.id, seed.company_id).proforma_invoice_id is None


class TestDeletingAPurchaseOrderWithItsOwnPackingList:
    def test_delete_succeeds_when_it_has_its_own_packing_list(self, container, seed):
        po = make_purchase_order(container, seed)
        pl = make_packing_list(container, seed, purchase_order_id=po.id)
        container.purchase_order_service.delete(seed.admin, po.id)
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, seed.company_id)
        # The packing list survives, standalone - this is the exact case that
        # made every PO with its own packing list permanently undeletable.
        reloaded = container.packing_list_service.get(pl.id, seed.company_id)
        assert reloaded.purchase_order_id is None

    def test_delete_without_a_packing_list_still_works(self, container, seed):
        po = make_purchase_order(container, seed)
        container.purchase_order_service.delete(seed.admin, po.id)
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, seed.company_id)
