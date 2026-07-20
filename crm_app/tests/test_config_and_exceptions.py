"""
Tests for config.py and app/exceptions.py

These are tiny modules, but pinning their shape stops an accidental rename or
default change from silently breaking currency conversion / auth flows that
read these values.
"""

from config import Config
from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


class TestConfig:
    def test_base_currency_is_inr(self):
        assert Config.BASE_CURRENCY == "INR"

    def test_fallback_rates_present_for_common_currencies(self):
        for code in ("USD", "EUR", "GBP", "AED", "CNY", "SAR"):
            assert code in Config.FALLBACK_RATES_TO_INR
            assert Config.FALLBACK_RATES_TO_INR[code] > 0

    def test_allowed_image_extensions(self):
        assert "png" in Config.ALLOWED_IMAGE_EXTENSIONS
        assert "jpg" in Config.ALLOWED_IMAGE_EXTENSIONS

    def test_pagination_and_lookahead_defaults(self):
        assert Config.PAGE_SIZE == 20
        assert Config.FOLLOWUP_LOOKAHEAD_DAYS == 3

    def test_max_content_length_is_generous_for_backups(self):
        # 500 MB default (see config comment): raw file-copy restores are big.
        assert Config.MAX_CONTENT_LENGTH == 500 * 1024 * 1024

    def test_schema_path_points_at_schema_sql(self):
        assert Config.SCHEMA_PATH.endswith("schema.sql")


class TestExceptions:
    def test_all_are_exception_subclasses(self):
        for exc in (ValidationError, PermissionDeniedError, NotFoundError):
            assert issubclass(exc, Exception)

    def test_they_are_distinct_types(self):
        assert ValidationError is not PermissionDeniedError
        assert PermissionDeniedError is not NotFoundError

    def test_can_carry_a_message(self):
        try:
            raise ValidationError("boom")
        except ValidationError as e:
            assert str(e) == "boom"
