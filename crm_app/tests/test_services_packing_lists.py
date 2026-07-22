"""
Tests for PackingListService (app/services.py)

The packing list has the most intricate server-side arithmetic in the app:
boxes/pallets/pcs/qty/weights all derive from each other under specific rules.
These tests pin those rules down, because they're easy to break accidentally
and hard to notice - a wrong figure just quietly prints on a shipping document.
"""

import pytest

from app.services import _leading_number, pallet_alt_quantity
from app.models import ProductPalletType, Product
from app.exceptions import ValidationError


def make_product(container, admin, **over):
    kwargs = dict(product_name="Tiles", description="", hsn_code="6907",
                  igst_percent="18", quantity="10", alternate_quantity="1.44")
    kwargs.update(over)
    return container.product_service.create_product(admin, **kwargs)


# ==========================================================================
# Free-function helpers
# ==========================================================================
class TestLeadingNumber:
    @pytest.mark.parametrize("text,expected", [
        ("31 boxes", 31.0),
        ("1.44", 1.44),
        ("  7 per box", 7.0),
        ("no number here", 0.0),
        ("", 0.0),
        (None, 0.0),
    ])
    def test_parses_leading_figure(self, text, expected):
        assert _leading_number(text) == expected


class TestPalletAltQuantity:
    def test_derived_from_boxes_times_per_box_alt_qty(self):
        product = Product(id=1, company_id=1, product_name="P", alternate_quantity="1.5")
        pallet = ProductPalletType(id=1, company_id=1, product_id=1, name="oak",
                                   boxes_per_pallet=10)
        assert pallet_alt_quantity(pallet, product) == 15.0

    def test_zero_when_product_has_no_alt_quantity(self):
        product = Product(id=1, company_id=1, product_name="P", alternate_quantity=None)
        pallet = ProductPalletType(id=1, company_id=1, product_id=1, name="oak",
                                   boxes_per_pallet=10)
        assert pallet_alt_quantity(pallet, product) == 0.0

    def test_zero_when_no_product(self):
        pallet = ProductPalletType(id=1, company_id=1, product_id=1, name="oak",
                                   boxes_per_pallet=10)
        assert pallet_alt_quantity(pallet, None) == 0.0


# ==========================================================================
# _build_items arithmetic
# ==========================================================================
class TestPackingListBuildItems:
    def _build(self, container, seed, rows):
        return container.packing_list_service._build_items(seed.company_id, rows)

    def test_boxes_is_compulsory(self, container, seed):
        with pytest.raises(ValidationError) as exc:
            self._build(container, seed, [{"product_name": "P"}])
        assert "boxes is compulsory" in str(exc.value)

    def test_boxes_derived_from_pallets_times_box_per_pallet(self, container, seed):
        items = self._build(container, seed, [
            {"product_name": "P", "pallets": "3", "box_per_pallet": "10"}])
        assert items[0].quantity_boxes == 30.0

    def test_pallets_derived_when_boxes_divide_evenly(self, container, seed):
        items = self._build(container, seed, [
            {"product_name": "P", "quantity_boxes": "30", "box_per_pallet": "10"}])
        assert items[0].pallets == 3.0

    def test_partial_pallet_keeps_user_typed_value(self, container, seed):
        # 35 boxes / 10 per pallet = 3.5 -> not clean, so the typed value stands.
        items = self._build(container, seed, [
            {"product_name": "P", "quantity_boxes": "35", "box_per_pallet": "10",
             "pallets": "4"}])
        assert items[0].pallets == 4.0

    def test_loose_row_has_no_pallets(self, container, seed):
        # No box_per_pallet selected = the built-in 'loose' option.
        items = self._build(container, seed, [
            {"product_name": "P", "quantity_boxes": "10", "pallets": "9"}])
        assert items[0].pallets is None

    def test_qty_auto_calculates_from_product_alt_quantity(self, container, seed):
        product = make_product(container, seed.admin, alternate_quantity="1.5")
        items = self._build(container, seed, [
            {"product_name": "Tiles", "product_id": str(product.id),
             "quantity_boxes": "10", "quantity_value": "999"}])
        assert items[0].quantity_value == 15.0  # 10 x 1.5, not the client's 999

    def test_pcs_auto_calculates_when_blank(self, container, seed):
        product = make_product(container, seed.admin, quantity="7")
        items = self._build(container, seed, [
            {"product_name": "Tiles", "product_id": str(product.id),
             "quantity_boxes": "10"}])
        assert items[0].pcs == 70.0  # 10 boxes x 7 pcs per box

    def test_weights_auto_calculate_from_product(self, container, seed):
        product = make_product(container, seed.admin,
                               net_weight_kg="20", gross_weight_kg="22.5")
        items = self._build(container, seed, [
            {"product_name": "Tiles", "product_id": str(product.id),
             "quantity_boxes": "10"}])
        assert items[0].net_weight_kg == 200.0
        assert items[0].gross_weight_kg == 225.0

    def test_submitted_weight_is_not_overwritten(self, container, seed):
        # A hand-typed weight stays editable and must survive the save.
        product = make_product(container, seed.admin, net_weight_kg="20")
        items = self._build(container, seed, [
            {"product_name": "Tiles", "product_id": str(product.id),
             "quantity_boxes": "10", "net_weight_kg": "123.45"}])
        assert items[0].net_weight_kg == 123.45

    def test_foreign_product_reference_dropped(self, container, seed):
        other = container.tenant_repo.create("Other", "other")
        other_admin = container.auth_service.create_user(
            other.id, "oadm", "pw123456", "O", "admin")
        foreign = make_product(container, other_admin)
        items = self._build(container, seed, [
            {"product_name": "Tiles", "product_id": str(foreign.id),
             "quantity_boxes": "5"}])
        assert items[0].product_id is None

    def test_non_numeric_values_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            self._build(container, seed, [
                {"product_name": "P", "quantity_boxes": "ten"}])

    def test_blank_rows_skipped_and_empty_is_an_error(self, container, seed):
        with pytest.raises(ValidationError):
            self._build(container, seed, [{"product_name": "   "}])

    def test_unit_defaults_to_sqm(self, container, seed):
        items = self._build(container, seed, [
            {"product_name": "P", "quantity_boxes": "1"}])
        assert items[0].unit == "SQM"


# ==========================================================================
# Create + prefill flows
# ==========================================================================
class TestPackingListCrud:
    def test_create_assigns_number_and_persists(self, container, seed):
        pl = container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-03-04"},
            [{"product_name": "P", "quantity_boxes": "10"}])
        assert pl.id is not None
        assert "20260304" in pl.packing_list_number
        reloaded = container.packing_list_service.get(pl.id, seed.company_id)
        assert len(reloaded.items) == 1

    def test_totals_reflect_items(self, container, seed):
        pl = container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-03-04"},
            [{"product_name": "A", "quantity_boxes": "10", "box_per_pallet": "5"},
             {"product_name": "B", "quantity_boxes": "20", "box_per_pallet": "10"}])
        reloaded = container.packing_list_service.get(pl.id, seed.company_id)
        assert reloaded.total_boxes == 30
        assert reloaded.total_pallets == 4  # 2 + 2

    def test_prefill_from_quotation_links_back_and_copies_lines(self, container, seed):
        q = container.quotation_service.create(
            seed.admin,
            {"buyer_name": "Buyer Co", "quotation_date": "2026-01-01",
             "buyer_reference_no": "REF-9"},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2",
              "hsn_code": "6907", "unit": "SQM"}])
        prefill = container.packing_list_service.build_prefill_from_quotation(q)

        # Shape: {"fields": {...}, "items": [...]}
        assert prefill["fields"]["quotation_id"] == q.id
        assert prefill["fields"]["buyer_order_no"] == "REF-9"
        assert prefill["fields"]["remarks"] == "MADE IN INDIA"

        # One placeholder line per quotation line, with the quantities left
        # blank for the packer to fill in.
        assert len(prefill["items"]) == 1
        line = prefill["items"][0]
        assert line["product_name"] == "P"
        assert line["hsn_code"] == "6907"
        assert line["unit"] == "SQM"
        assert line["quantity_boxes"] == ""
        assert line["is_placeholder"] is True

    def test_prefill_from_proforma_imports_quotations_packing_list(self, container, seed):
        # Quotation -> PL, then a proforma invoice is linked to that quotation.
        # Generating the PI's PL must carry over the quotation's PL rows in
        # full (designs, boxes, weights), not start from blank placeholders.
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "B", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2", "hsn_code": "6907"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-01-02", "quotation_id": q.id, "remarks": "PACKED"},
            [{"product_name": "P", "design_name": "D1", "quantity_boxes": "8",
              "box_per_pallet": "4", "net_weight_kg": "16"}])
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01", "quotation_id": q.id},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2"}])

        prefill = container.packing_list_service.build_prefill_from_proforma(pi)
        assert prefill["fields"]["proforma_invoice_id"] == pi.id
        assert prefill["fields"]["remarks"] == "PACKED"
        assert len(prefill["items"]) == 1
        line = prefill["items"][0]
        assert line["design_name"] == "D1"
        assert line["quantity_boxes"] == 8
        assert line["net_weight_kg"] == 16
        assert "is_placeholder" not in line

    def test_prefill_from_purchase_order_walks_to_quotation_packing_list(self, container, seed):
        # PO -> PI (no PL on the PI) -> quotation (has a PL). The PO's PL must
        # reach past the intermediate invoice to the quotation's PL.
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "B", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-01-02", "quotation_id": q.id},
            [{"product_name": "P", "design_name": "D9", "quantity_boxes": "5"}])
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01", "quotation_id": q.id},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2"}])
        po = container.purchase_order_service.create(
            seed.admin, {"seller_name": "S", "po_date": "2026-03-01", "proforma_invoice_id": pi.id},
            [{"product_name": "P", "quantity_boxes": "10", "quantity_value": "100", "price_inr": "500", "price_per": "BOX"}])

        prefill = container.packing_list_service.build_prefill_from_purchase_order(po)
        assert len(prefill["items"]) == 1
        assert prefill["items"][0]["design_name"] == "D9"
        assert prefill["items"][0]["quantity_boxes"] == 5
        assert "is_placeholder" not in prefill["items"][0]

    def test_prefill_prefers_nearer_ancestor_packing_list(self, container, seed):
        # Both the quotation and the proforma invoice have a PL - the PO's PL
        # must import the nearer one (the proforma invoice's).
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "B", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-01-02", "quotation_id": q.id},
            [{"product_name": "P", "design_name": "FROM_QUOTE", "quantity_boxes": "5"}])
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01", "quotation_id": q.id},
            [{"product_name": "P", "quantity_value": "10", "price_usd": "2"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-02-02", "proforma_invoice_id": pi.id},
            [{"product_name": "P", "design_name": "FROM_PI", "quantity_boxes": "7"}])
        po = container.purchase_order_service.create(
            seed.admin, {"seller_name": "S", "po_date": "2026-03-01", "proforma_invoice_id": pi.id},
            [{"product_name": "P", "quantity_boxes": "10", "quantity_value": "100", "price_inr": "500", "price_per": "BOX"}])

        prefill = container.packing_list_service.build_prefill_from_purchase_order(po)
        assert prefill["items"][0]["design_name"] == "FROM_PI"

    def test_list_for_quotation_finds_generated_list(self, container, seed):
        q = container.quotation_service.create(
            seed.admin, {"buyer_name": "B", "quotation_date": "2026-01-01"},
            [{"product_name": "P", "quantity_value": "1", "price_usd": "1"}])
        container.packing_list_service.create(
            seed.admin, {"packing_list_date": "2026-01-02", "quotation_id": q.id},
            [{"product_name": "P", "quantity_boxes": "5"}])
        found = container.packing_list_service.list_for_quotation(q.id, seed.company_id)
        assert len(found) == 1
