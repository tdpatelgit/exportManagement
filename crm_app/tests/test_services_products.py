"""
Tests for ProductService (app/services.py)

Focus areas:
  - the static parse helpers (_parse_price/_weight/_unit/_percent) that guard
    every numeric input on the product form
  - IGST -> SGST/CGST half-split (_tax_fields)
  - pallet-type validation rules
  - catalog CRUD + admin-only permission enforcement, over a real tmp DB
"""

import pytest

from app.services import ProductService
from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


# ==========================================================================
# Static / pure parse helpers
# ==========================================================================
class TestParseHelpers:
    def test_parse_price_valid(self):
        assert ProductService._parse_price("12.345") == 12.35  # rounds to 2dp

    def test_parse_price_blank_is_none(self):
        assert ProductService._parse_price("") is None
        assert ProductService._parse_price(None) is None

    def test_parse_price_non_numeric_raises(self):
        with pytest.raises(ValidationError):
            ProductService._parse_price("abc")

    def test_parse_weight_valid_rounds_to_3dp(self):
        assert ProductService._parse_weight("Net", "1.23456") == 1.235

    def test_parse_weight_blank_is_none(self):
        assert ProductService._parse_weight("Net", "") is None

    def test_parse_weight_negative_raises(self):
        with pytest.raises(ValidationError):
            ProductService._parse_weight("Net", "-1")

    def test_parse_weight_non_numeric_raises(self):
        with pytest.raises(ValidationError):
            ProductService._parse_weight("Net", "heavy")

    def test_parse_unit_uppercases(self):
        assert ProductService._parse_unit("sqm") == "SQM"

    def test_parse_unit_blank_uses_default(self):
        assert ProductService._parse_unit("", default="PCS") == "PCS"
        assert ProductService._parse_unit(None) == "SQM"

    def test_parse_percent_valid(self):
        assert ProductService._parse_percent("IGST", "18") == 18.0

    def test_parse_percent_blank_is_none(self):
        assert ProductService._parse_percent("IGST", "") is None

    def test_parse_percent_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            ProductService._parse_percent("IGST", "150")
        with pytest.raises(ValidationError):
            ProductService._parse_percent("IGST", "-5")

    def test_parse_percent_non_numeric_raises(self):
        with pytest.raises(ValidationError):
            ProductService._parse_percent("IGST", "lots")


# ==========================================================================
# _tax_fields: IGST split
# ==========================================================================
class TestTaxFields:
    def test_igst_halved_into_sgst_and_cgst(self, container):
        fields = container.product_service._tax_fields("18")
        assert fields == {"igst_percent": 18.0, "sgst_percent": 9.0, "cgst_percent": 9.0}

    def test_odd_igst_rounds_each_half(self, container):
        fields = container.product_service._tax_fields("5")
        assert fields["sgst_percent"] == 2.5 and fields["cgst_percent"] == 2.5

    def test_blank_igst_leaves_all_none(self, container):
        fields = container.product_service._tax_fields("")
        assert fields == {"igst_percent": None, "sgst_percent": None, "cgst_percent": None}


# ==========================================================================
# _parse_pallet_types validation
# ==========================================================================
class TestParsePalletTypes:
    def test_none_and_blank_rows_skipped(self, container):
        assert container.product_service._parse_pallet_types(None) == []
        assert container.product_service._parse_pallet_types(
            [{"name": "", "boxes_per_pallet": ""}]) == []

    def test_valid_row_parsed(self, container):
        parsed = container.product_service._parse_pallet_types(
            [{"name": "pine", "boxes_per_pallet": "31"}])
        assert len(parsed) == 1
        assert parsed[0].name == "pine" and parsed[0].boxes_per_pallet == 31.0

    def test_name_without_boxes_raises(self, container):
        with pytest.raises(ValidationError):
            container.product_service._parse_pallet_types([{"name": "pine", "boxes_per_pallet": ""}])

    def test_boxes_without_name_raises(self, container):
        with pytest.raises(ValidationError):
            container.product_service._parse_pallet_types([{"name": "", "boxes_per_pallet": "10"}])

    def test_reserved_loose_name_rejected(self, container):
        with pytest.raises(ValidationError):
            container.product_service._parse_pallet_types([{"name": "loose", "boxes_per_pallet": "5"}])

    def test_non_numeric_boxes_raises(self, container):
        with pytest.raises(ValidationError):
            container.product_service._parse_pallet_types([{"name": "p", "boxes_per_pallet": "many"}])

    def test_zero_or_negative_boxes_raises(self, container):
        with pytest.raises(ValidationError):
            container.product_service._parse_pallet_types([{"name": "p", "boxes_per_pallet": "0"}])


# ==========================================================================
# Product CRUD (real DB)
# ==========================================================================
class TestProductCrud:
    def test_create_product_persists_tax_split(self, container, seed):
        p = container.product_service.create_product(
            seed.admin, product_name="Tiles", description="d", hsn_code="6907",
            igst_percent="18", quantity="10", alternate_quantity="1.44")
        assert p.id is not None
        assert p.sgst_percent == 9.0 and p.cgst_percent == 9.0

    def test_create_product_requires_admin(self, container, seed):
        with pytest.raises(PermissionDeniedError):
            container.product_service.create_product(
                seed.employee, product_name="X", description="", hsn_code="",
                igst_percent="", quantity="", alternate_quantity="")

    def test_create_product_requires_name(self, container, seed):
        with pytest.raises(ValidationError):
            container.product_service.create_product(
                seed.admin, product_name="  ", description="", hsn_code="",
                igst_percent="", quantity="", alternate_quantity="")

    def test_create_product_with_pallet_types(self, container, seed):
        p = container.product_service.create_product(
            seed.admin, product_name="Slabs", description="", hsn_code="",
            igst_percent="", quantity="", alternate_quantity="",
            pallet_types=[{"name": "oak", "boxes_per_pallet": "40"}])
        pallets = container.product_service.pallet_types_for_product(p.id)
        assert [pt.name for pt in pallets] == ["oak"]

    def test_update_product(self, container, seed):
        p = container.product_service.create_product(
            seed.admin, "Tiles", "", "", "18", "10", "1.44")
        container.product_service.update_product(
            seed.admin, p.id, "Renamed Tiles", "", "", "12", "5", "1.0")
        reloaded = container.product_service.get_product(p.id, seed.company_id)
        assert reloaded.product_name == "Renamed Tiles"
        assert reloaded.igst_percent == 12.0 and reloaded.sgst_percent == 6.0

    def test_get_product_wrong_company_not_found(self, container, seed):
        p = container.product_service.create_product(
            seed.admin, "Tiles", "", "", "", "", "")
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.product_service.get_product(p.id, other.id)

    def test_delete_product(self, container, seed):
        p = container.product_service.create_product(
            seed.admin, "Tiles", "", "", "", "", "")
        container.product_service.delete_product(seed.admin, p.id)
        with pytest.raises(NotFoundError):
            container.product_service.get_product(p.id, seed.company_id)

    def test_delete_requires_admin(self, container, seed):
        p = container.product_service.create_product(
            seed.admin, "Tiles", "", "", "", "", "")
        with pytest.raises(PermissionDeniedError):
            container.product_service.delete_product(seed.employee, p.id)


# ==========================================================================
# Category CRUD
# ==========================================================================
class TestCategories:
    def test_create_and_list_category(self, container, seed):
        cat = container.product_service.create_category(seed.admin, "Floor Tiles")
        assert cat.id is not None
        names = [c.name for c in container.product_service.list_categories(seed.company_id)]
        assert "Floor Tiles" in names

    def test_create_category_requires_admin(self, container, seed):
        with pytest.raises(PermissionDeniedError):
            container.product_service.create_category(seed.employee, "X")

    def test_rename_category(self, container, seed):
        cat = container.product_service.create_category(seed.admin, "Old")
        container.product_service.rename_category(seed.admin, cat.id, "New")
        assert container.product_service.get_category(cat.id, seed.company_id).name == "New"

    def test_nested_category_parent(self, container, seed):
        parent = container.product_service.create_category(seed.admin, "Parent")
        child = container.product_service.create_category(seed.admin, "Child", parent_id=parent.id)
        assert child.parent_id == parent.id
