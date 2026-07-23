"""
Deleting a document deletes every document generated under it (a real
cascade): a quotation's proforma invoices, a proforma invoice's purchase
orders, a purchase order's purchase invoice, and any packing list generated
directly from any of them. Previously these "generated from" links
(quotation_id / proforma_invoice_id / purchase_order_id / purchase_invoice_id
on a downstream document) were only nulled out so the downstream document
survived standalone - the user explicitly asked for the opposite: deleting a
document should take its sub-documents down with it.
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
    def test_delete_cascades_to_a_generated_proforma_invoice(self, container, seed):
        q = make_quotation(container, seed)
        pi = make_proforma(container, seed, quotation_id=q.id)
        container.quotation_service.delete(seed.admin, q.id)
        with pytest.raises(NotFoundError):
            container.quotation_service.get(q.id, seed.company_id)
        with pytest.raises(NotFoundError):
            container.proforma_invoice_service.get(pi.id, seed.company_id)

    def test_delete_cascades_to_a_generated_packing_list(self, container, seed):
        q = make_quotation(container, seed)
        pl = make_packing_list(container, seed, quotation_id=q.id)
        container.quotation_service.delete(seed.admin, q.id)
        with pytest.raises(NotFoundError):
            container.packing_list_service.get(pl.id, seed.company_id)


class TestDeletingAProformaInvoiceWithDownstreamDocuments:
    def test_delete_cascades_to_a_generated_purchase_order(self, container, seed):
        pi = make_proforma(container, seed)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        with pytest.raises(NotFoundError):
            container.proforma_invoice_service.get(pi.id, seed.company_id)
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, seed.company_id)

    def test_delete_cascades_to_a_generated_packing_list(self, container, seed):
        pi = make_proforma(container, seed)
        pl = make_packing_list(container, seed, proforma_invoice_id=pi.id)
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        with pytest.raises(NotFoundError):
            container.packing_list_service.get(pl.id, seed.company_id)

    def test_delete_cascades_to_several_linked_purchase_orders_at_once(self, container, seed):
        """The many-POs-per-PI case: deleting the PI must take every one of
        them down with it."""
        pi = make_proforma(container, seed)
        pos = [make_purchase_order(container, seed, proforma_invoice_id=pi.id) for _ in range(3)]
        container.proforma_invoice_service.delete(seed.admin, pi.id)
        for po in pos:
            with pytest.raises(NotFoundError):
                container.purchase_order_service.get(po.id, seed.company_id)


class TestDeletingAPurchaseOrderWithItsOwnPackingList:
    def test_delete_cascades_to_its_own_packing_list(self, container, seed):
        po = make_purchase_order(container, seed)
        pl = make_packing_list(container, seed, purchase_order_id=po.id)
        container.purchase_order_service.delete(seed.admin, po.id)
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, seed.company_id)
        with pytest.raises(NotFoundError):
            container.packing_list_service.get(pl.id, seed.company_id)

    def test_delete_without_a_packing_list_still_works(self, container, seed):
        po = make_purchase_order(container, seed)
        container.purchase_order_service.delete(seed.admin, po.id)
        with pytest.raises(NotFoundError):
            container.purchase_order_service.get(po.id, seed.company_id)


class TestDeletingAPurchaseOrderWithAPurchaseInvoice:
    def test_delete_cascades_to_its_purchase_invoice_and_that_invoices_packing_list(self, container, seed):
        po = make_purchase_order(container, seed)
        purchase_invoice = container.purchase_invoice_service.create(
            seed.admin,
            {"purchase_order_id": po.id, "seller_name": "Supplier Co",
             "invoice_number": "SUP-INV-1", "invoice_date": "2026-02-04"},
            [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "10", "price_inr": "5"}], [])
        pl = make_packing_list(container, seed, purchase_invoice_id=purchase_invoice.id)
        container.purchase_order_service.delete(seed.admin, po.id)
        with pytest.raises(NotFoundError):
            container.purchase_invoice_service.get(purchase_invoice.id, seed.company_id)
        with pytest.raises(NotFoundError):
            container.packing_list_service.get(pl.id, seed.company_id)
