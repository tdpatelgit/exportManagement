"""
Tests for AuthService and CurrencyService (app/services.py)

AuthService is exercised against a real repository over a tmp DB so password
hashing, uniqueness and permission rules are all tested end-to-end.
CurrencyService's live HTTP call is monkeypatched so tests are deterministic
and offline.
"""

import pytest

from app.services import AuthService, CurrencyService
from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


# ==========================================================================
# AuthService
# ==========================================================================
class TestAuthCreateUser:
    def test_create_user_hashes_password(self, container, seed):
        u = container.auth_service.create_user(
            seed.company_id, "newuser", "secret123", "New User", "employee")
        assert u.id is not None
        assert u.password_hash != "secret123"  # never stored in the clear

    def test_duplicate_username_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.create_user(
                seed.company_id, "admin", "whatever1", "Dup", "admin")

    def test_missing_fields_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.create_user(seed.company_id, "", "pw", "Name", "admin")

    def test_invalid_role_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.create_user(
                seed.company_id, "u2", "pw123456", "Name", "superuser")

    def test_same_username_ok_in_different_company(self, container, seed):
        other = container.tenant_repo.create("Other Co", "other-co")
        # 'admin' already exists in seed.company_id but not in `other`.
        u = container.auth_service.create_user(other.id, "admin", "pw123456", "Other Admin", "admin")
        assert u.company_id == other.id


class TestAuthAuthenticate:
    def test_correct_credentials(self, container, seed):
        user = container.auth_service.authenticate(seed.company_id, "admin", "admin-pass-123")
        assert user is not None and user.username == "admin"

    def test_wrong_password(self, container, seed):
        assert container.auth_service.authenticate(seed.company_id, "admin", "nope") is None

    def test_unknown_username(self, container, seed):
        assert container.auth_service.authenticate(seed.company_id, "ghost", "x") is None

    def test_inactive_tenant_cannot_authenticate(self, container, seed):
        container.db.execute("UPDATE tenants SET is_active = 0 WHERE id = ?", (seed.company_id,))
        assert container.auth_service.authenticate(seed.company_id, "admin", "admin-pass-123") is None

    def test_inactive_user_cannot_authenticate(self, container, seed):
        container.user_repo.set_active(seed.employee.id, False)
        assert container.auth_service.authenticate(seed.company_id, "employee", "emp-pass-123") is None


class TestAuthChangeUsername:
    def test_employee_can_rename_self(self, container, seed):
        updated = container.auth_service.change_username(seed.employee, seed.employee.id, "eve2")
        assert updated.username == "eve2"

    def test_employee_cannot_rename_others(self, container, seed):
        with pytest.raises(PermissionDeniedError):
            container.auth_service.change_username(seed.employee, seed.admin.id, "hacked")

    def test_admin_can_rename_others(self, container, seed):
        updated = container.auth_service.change_username(seed.admin, seed.employee.id, "eve3")
        assert updated.username == "eve3"

    def test_blank_username_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.change_username(seed.admin, seed.admin.id, "   ")

    def test_duplicate_username_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.change_username(seed.admin, seed.employee.id, "admin")

    def test_cross_company_target_is_not_found(self, container, seed):
        other = container.tenant_repo.create("Other", "other")
        stranger = container.auth_service.create_user(other.id, "stranger", "pw123456", "S", "admin")
        with pytest.raises(NotFoundError):
            container.auth_service.change_username(seed.admin, stranger.id, "renamed")


class TestAuthChangePassword:
    def test_happy_path(self, container, seed):
        container.auth_service.change_password(seed.admin, "admin-pass-123", "brand-new-1")
        # Old password no longer works, new one does.
        assert container.auth_service.authenticate(seed.company_id, "admin", "admin-pass-123") is None
        assert container.auth_service.authenticate(seed.company_id, "admin", "brand-new-1") is not None

    def test_wrong_current_password_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.change_password(seed.admin, "wrong", "brand-new-1")

    def test_too_short_new_password_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            container.auth_service.change_password(seed.admin, "admin-pass-123", "12345")


# ==========================================================================
# CurrencyService
# ==========================================================================
class FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            import requests
            raise requests.RequestException("boom")

    def json(self):
        return self._payload


@pytest.fixture
def currency():
    return CurrencyService("http://fake.test/latest", {"USD": 86.0, "EUR": 93.0})


class TestCurrencyRate:
    def test_uses_live_rate_when_available(self, currency, monkeypatch):
        monkeypatch.setattr(
            "app.services.requests.get",
            lambda url, params, timeout: FakeResponse({"rates": {"INR": 84.5}}),
        )
        assert currency.get_rate_to_inr("USD") == 84.5

    def test_falls_back_when_api_errors(self, currency, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.RequestException("no internet")

        monkeypatch.setattr("app.services.requests.get", boom)
        assert currency.get_rate_to_inr("USD") == 86.0

    def test_falls_back_when_rate_missing_in_payload(self, currency, monkeypatch):
        monkeypatch.setattr(
            "app.services.requests.get",
            lambda url, params, timeout: FakeResponse({"rates": {}}),
        )
        assert currency.get_rate_to_inr("EUR") == 93.0

    def test_unknown_currency_with_no_fallback_raises(self, currency, monkeypatch):
        import requests
        monkeypatch.setattr(
            "app.services.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(requests.RequestException()),
        )
        with pytest.raises(ValidationError):
            currency.get_rate_to_inr("JPY")

    def test_currency_code_is_case_insensitive(self, currency, monkeypatch):
        import requests
        monkeypatch.setattr(
            "app.services.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(requests.RequestException()),
        )
        assert currency.get_rate_to_inr("usd") == 86.0


class TestCurrencyConvert:
    def test_convert_returns_rate_and_rounded_inr(self, currency, monkeypatch):
        monkeypatch.setattr(
            "app.services.requests.get",
            lambda url, params, timeout: FakeResponse({"rates": {"INR": 86.0}}),
        )
        rate, inr = currency.convert(10, "USD")
        assert rate == 86.0
        assert inr == 860.0

    def test_inr_input_is_rejected(self, currency):
        with pytest.raises(ValidationError):
            currency.convert(100, "INR")
