"""
Tests for PurchaseInvoiceService (app/services.py) - the last document in
the pipeline, raised once a supplier's goods against one of our purchase
orders actually arrive. Mirrors test_services_proforma_po.py's
TestPurchaseOrder coverage, plus the two things unique to this document
type: the supplier PDF upload (no print/PDF generation of our own) and the
plain vehicle-number list.
"""

import io

import pytest
from werkzeug.datastructures import FileStorage

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


def upload(filename="invoice.pdf", data=b"fake-pdf-bytes"):
    return FileStorage(stream=io.BytesIO(data), filename=filename)


def make_lead(container, user):
    return container.lead_service.create_lead(
        user, "Buyer Co", "1", "b@x.com", None, None, None,
        [{"name": "Bob", "is_primary": True}])


def make_purchase_order(container, seed, item=None, **over):
    fields = {"seller_name": "Supplier Ltd", "po_date": "2026-03-01"}
    fields.update(over)
    return container.purchase_order_service.create(
        seed.admin, fields,
        [item or {"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
                  "price_inr": "500", "price_per": "BOX"}])


class TestPurchaseInvoicePrefill:
    def test_prefill_carries_seller_and_reference_fields(self, container, seed):
        po = make_purchase_order(container, seed)
        prefill = container.purchase_invoice_service.build_prefill_from_purchase_order(po)
        f = prefill["fields"]
        assert f["purchase_order_id"] == po.id
        assert f["seller_name"] == "Supplier Ltd"
        assert f["seller_supplier_id"] == po.seller_supplier_id

    def test_prefill_copies_line_items_in_full(self, container, seed):
        po = make_purchase_order(container, seed)
        items = container.purchase_invoice_service.build_prefill_from_purchase_order(po)["items"]
        assert len(items) == 1
        assert items[0]["product_name"] == "Tiles"
        assert items[0]["quantity_boxes"] == 10
        assert items[0]["price_inr"] == 500

    def test_prefill_copies_the_pos_own_computed_tax_amounts(self, container, seed):
        container.company_repo.upsert(seed.company_id, "Test Exports", "Morbi", "24AAAAA0000A1Z5", "", "", "")
        product = container.product_service.create_product(
            current_user=seed.admin, product_name="Tiles", description="", hsn_code="6907",
            igst_percent="18", quantity="", alternate_quantity="")
        po = make_purchase_order(
            container, seed, item={
                "product_id": str(product.id), "product_name": "Tiles", "quantity_boxes": "10",
                "quantity_value": "100", "price_inr": "500", "price_per": "BOX",
            },
            seller_gstin="27BBBBB0000B1Z5",  # different state -> IGST, not CGST/SGST
        )
        fields = container.purchase_invoice_service.build_prefill_from_purchase_order(po)["fields"]
        assert fields["igst_amount"] == po.igst_amount == 900.0  # 5000 * 18%
        assert fields["cgst_amount"] == 0
        assert fields["sgst_amount"] == 0


class TestPurchaseInvoiceCrud:
    def _create(self, container, seed, vehicle_numbers=None, pdf_file=None, **over):
        fields = {"seller_name": "Supplier Ltd", "invoice_number": "SUP-INV-1", "invoice_date": "2026-04-01"}
        fields.update(over)
        return container.purchase_invoice_service.create(
            seed.admin, fields,
            [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
              "price_inr": "500", "price_per": "BOX"}],
            vehicle_numbers or ["GJ-01-AB-1234"],
            pdf_file=pdf_file,
        )

    def test_create_assigns_our_own_number_and_keeps_supplier_number_separate(self, container, seed):
        pinv = self._create(container, seed)
        assert pinv.id is not None
        assert pinv.purchase_invoice_number.startswith("PINV20260401")
        assert pinv.invoice_number == "SUP-INV-1"

    def test_create_persists_items_and_vehicle_numbers(self, container, seed):
        pinv = self._create(container, seed, vehicle_numbers=["GJ-01-AB-1234", "GJ-01-CD-5678"])
        reloaded = container.purchase_invoice_service.get(pinv.id, seed.company_id)
        assert reloaded.subtotal_inr == 5000.0
        assert reloaded.vehicle_numbers == ["GJ-01-AB-1234", "GJ-01-CD-5678"]

    def test_blank_vehicle_rows_are_dropped(self, container, seed):
        pinv = self._create(container, seed, vehicle_numbers=["GJ-01-AB-1234", "  ", ""])
        reloaded = container.purchase_invoice_service.get(pinv.id, seed.company_id)
        assert reloaded.vehicle_numbers == ["GJ-01-AB-1234"]

    def test_invoice_value_sums_charges_and_subtracts_discount(self, container, seed):
        pinv = self._create(
            container, seed, freight="100", insurance_other="50", igst_amount="200",
            discount_amount="30", round_off="0.50",
        )
        reloaded = container.purchase_invoice_service.get(pinv.id, seed.company_id)
        # subtotal 5000 + 100 + 50 + 200 - 30 + 0.50
        assert reloaded.invoice_value_inr == 5320.50

    def test_requires_invoice_number(self, container, seed):
        with pytest.raises(ValidationError):
            container.purchase_invoice_service.create(
                seed.admin, {"seller_name": "Supplier Ltd", "invoice_date": "2026-04-01"},
                [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
                  "price_inr": "500", "price_per": "BOX"}],
                [],
            )

    def test_create_records_a_version(self, container, seed):
        pinv = self._create(container, seed)
        versions = container.document_version_service.list_for_document("purchase_invoice", pinv.id)
        assert len(versions) == 1

    def test_update_keeps_number_and_adds_version(self, container, seed):
        pinv = self._create(container, seed)
        container.purchase_invoice_service.update(
            seed.admin, pinv.id,
            {"seller_name": "Renamed Supplier", "invoice_number": "SUP-INV-1", "invoice_date": "2026-04-01"},
            [{"product_name": "Tiles", "quantity_boxes": "5", "quantity_value": "50",
              "price_inr": "500", "price_per": "BOX"}],
            ["GJ-01-AB-1234"],
        )
        reloaded = container.purchase_invoice_service.get(pinv.id, seed.company_id)
        assert reloaded.seller_name == "Renamed Supplier"
        assert reloaded.purchase_invoice_number == pinv.purchase_invoice_number
        assert len(container.document_version_service.list_for_document(
            "purchase_invoice", pinv.id)) == 2

    def test_links_back_to_its_purchase_order(self, container, seed):
        po = make_purchase_order(container, seed)
        pinv = self._create(container, seed, purchase_order_id=str(po.id))
        found = container.purchase_invoice_service.list_for_purchase_order(po.id, seed.company_id)
        assert len(found) == 1 and found[0].id == pinv.id

    def test_cross_company_get_is_not_found(self, container, seed):
        pinv = self._create(container, seed)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.purchase_invoice_service.get(pinv.id, other.id)

    def test_only_creator_or_admin_can_edit(self, container, seed):
        pinv = self._create(container, seed)
        with pytest.raises(PermissionDeniedError):
            container.purchase_invoice_service.update(
                seed.employee, pinv.id,
                {"seller_name": "Supplier Ltd", "invoice_number": "SUP-INV-1", "invoice_date": "2026-04-01"},
                [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
                  "price_inr": "500", "price_per": "BOX"}],
                [],
            )

    def test_delete(self, container, seed):
        pinv = self._create(container, seed)
        container.purchase_invoice_service.delete(seed.admin, pinv.id)
        with pytest.raises(NotFoundError):
            container.purchase_invoice_service.get(pinv.id, seed.company_id)

    def test_generating_a_purchase_invoice_advances_client_status(self, container, seed):
        lead = make_lead(container, seed.admin)
        client = container.buyer_service.convert_lead(lead.id, seed.admin)
        self._create(container, seed, lead_id=str(lead.id))
        reloaded = container.buyer_service.get(client.id, seed.company_id)
        assert reloaded.status == "export_invoice_submission_pending"


class TestPurchaseInvoicePdfUpload:
    def test_uploaded_pdf_is_saved_and_served_from_static(self, container, seed, tmp_path):
        pinv = TestPurchaseInvoiceCrud()._create(container, seed, pdf_file=upload())
        assert pinv.supplier_pdf_path is not None
        assert pinv.supplier_pdf_path.startswith("uploads/purchase_invoices/")

    def test_replacing_the_pdf_deletes_the_old_file(self, container, seed):
        pinv = TestPurchaseInvoiceCrud()._create(container, seed, pdf_file=upload("first.pdf"))
        first_path = container.purchase_invoice_service.upload_folder
        import os
        first_full_path = os.path.join(first_path, os.path.basename(pinv.supplier_pdf_path))
        assert os.path.exists(first_full_path)

        container.purchase_invoice_service.update(
            seed.admin, pinv.id,
            {"seller_name": "Supplier Ltd", "invoice_number": "SUP-INV-1", "invoice_date": "2026-04-01"},
            [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
              "price_inr": "500", "price_per": "BOX"}],
            [], pdf_file=upload("second.pdf"),
        )
        assert not os.path.exists(first_full_path)

    def test_rejects_non_pdf_extensions(self, container, seed):
        with pytest.raises(ValidationError):
            TestPurchaseInvoiceCrud()._create(container, seed, pdf_file=upload("virus.exe"))

    def test_remove_pdf_flag_clears_the_file(self, container, seed):
        pinv = TestPurchaseInvoiceCrud()._create(container, seed, pdf_file=upload())
        updated = container.purchase_invoice_service.update(
            seed.admin, pinv.id,
            {"seller_name": "Supplier Ltd", "invoice_number": "SUP-INV-1", "invoice_date": "2026-04-01"},
            [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
              "price_inr": "500", "price_per": "BOX"}],
            [], remove_pdf=True,
        )
        assert updated.supplier_pdf_path is None
