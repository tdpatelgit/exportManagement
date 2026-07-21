"""
Tests for LeadService and ClientService (app/services.py)

Exercised end-to-end against real repositories on a tmp DB. The rules under
test are the spec's core CRM rules: compulsory fields, the "at least one
contact" rule, the employee-vs-admin permission split, and lead->client
conversion.
"""

import pytest

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError


def _contacts(*names, primary_index=None):
    out = []
    for i, n in enumerate(names):
        out.append({"name": n, "phone": "1", "email": f"{n}@x.com",
                    "is_primary": (i == primary_index)})
    return out


# ==========================================================================
# LeadService
# ==========================================================================
class TestCreateLead:
    def _create(self, container, user, **over):
        kwargs = dict(
            company_name="Acme", phone="123", email="a@acme.com",
            facebook=None, instagram=None, other_social=None,
            contacts=_contacts("Bob"),
        )
        kwargs.update(over)
        return container.lead_service.create_lead(user, **kwargs)

    def test_happy_path(self, container, seed):
        lead = self._create(container, seed.employee)
        assert lead.id is not None
        assert lead.status == "new"
        assert lead.company_id == seed.company_id
        assert lead.created_by == seed.employee.id

    def test_first_contact_auto_primary(self, container, seed):
        lead = self._create(container, seed.employee, contacts=_contacts("Bob", "Carol"))
        primaries = [c for c in lead.contacts if c.is_primary]
        assert len(primaries) == 1
        assert primaries[0].name == "Bob"

    def test_explicit_primary_respected(self, container, seed):
        lead = self._create(container, seed.employee,
                            contacts=_contacts("Bob", "Carol", primary_index=1))
        assert [c.name for c in lead.contacts if c.is_primary] == ["Carol"]

    def test_missing_company_name_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            self._create(container, seed.employee, company_name="  ")

    def test_missing_phone_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            self._create(container, seed.employee, phone="")

    def test_missing_email_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            self._create(container, seed.employee, email="")

    def test_no_named_contact_rejected(self, container, seed):
        with pytest.raises(ValidationError):
            self._create(container, seed.employee, contacts=[{"name": "  "}])


class TestLeadReadsAndPermissions:
    def _make_lead(self, container, user):
        return container.lead_service.create_lead(
            user, "Acme", "1", "a@x.com", None, None, None, _contacts("Bob"))

    def test_get_wrong_company_is_not_found(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        other = container.tenant_repo.create("Other", "other")
        with pytest.raises(NotFoundError):
            container.lead_service.get(lead.id, other.id)

    def test_employee_sees_only_own_leads(self, container, seed):
        self._make_lead(container, seed.employee)      # employee's
        self._make_lead(container, seed.admin)         # admin's
        emp_view = container.lead_service.list_for_dashboard(seed.employee)
        assert all(l.created_by == seed.employee.id for l in emp_view)

    def test_admin_sees_all_leads(self, container, seed):
        self._make_lead(container, seed.employee)
        self._make_lead(container, seed.admin)
        admin_view = container.lead_service.list_for_dashboard(seed.admin)
        assert len(admin_view) == 2

    def test_employee_cannot_edit_compulsory_fields(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        with pytest.raises(PermissionDeniedError):
            container.lead_service.update_compulsory_fields(
                lead.id, seed.employee, {"company_name": "New", "phone": "9", "email": "n@x.com"})

    def test_admin_can_edit_compulsory_fields(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        container.lead_service.update_compulsory_fields(
            lead.id, seed.admin, {"company_name": "Renamed", "phone": "9", "email": "n@x.com"})
        assert container.lead_service.get(lead.id, seed.company_id).company_name == "Renamed"

    def test_update_status_rejects_invalid_status(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        with pytest.raises(ValidationError):
            container.lead_service.update_status(lead.id, seed.employee, "bogus")

    def test_update_status_happy_path(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        container.lead_service.update_status(lead.id, seed.employee, "in_follow_up")
        assert container.lead_service.get(lead.id, seed.company_id).status == "in_follow_up"

    def test_employee_cannot_modify_another_employees_lead(self, container, seed):
        other_emp = container.auth_service.create_user(
            seed.company_id, "emp2", "pw123456", "Emp Two", "employee")
        lead = self._make_lead(container, seed.employee)
        with pytest.raises(PermissionDeniedError):
            container.lead_service.update_status(lead.id, other_emp, "in_follow_up")

    def test_add_contact_requires_name(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        with pytest.raises(ValidationError):
            container.lead_service.add_contact(lead.id, seed.employee, "  ", "1", "x@x.com")

    def test_set_primary_rejects_foreign_contact(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        with pytest.raises(ValidationError):
            container.lead_service.set_primary_contact(lead.id, seed.employee, 99999)


# ==========================================================================
# ClientService: lead -> client conversion
# ==========================================================================
class TestConvertLead:
    def _make_lead(self, container, user):
        return container.lead_service.create_lead(
            user, "Acme", "1", "a@x.com", None, None, None,
            _contacts("Bob", primary_index=0))

    def test_admin_converts_lead_to_client(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        client = container.client_service.convert_lead(lead.id, seed.admin, "Buyer")
        assert client.id is not None
        assert client.lead_id == lead.id
        assert client.client_type == "Buyer"
        assert client.status == "proforma_invoice_submission_pending"

    def test_conversion_copies_contacts(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        # Re-fetch: get_by_id is what eagerly loads a client's contacts.
        reloaded = container.client_service.get(client.id, seed.company_id)
        assert any(c.name == "Bob" for c in reloaded.contacts)

    def test_employee_cannot_convert(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        with pytest.raises(PermissionDeniedError):
            container.client_service.convert_lead(lead.id, seed.employee)

    def test_double_conversion_rejected(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        container.client_service.convert_lead(lead.id, seed.admin)
        with pytest.raises(ValidationError):
            container.client_service.convert_lead(lead.id, seed.admin)

    def test_unknown_client_type_defaults_to_buyer(self, container, seed):
        lead = self._make_lead(container, seed.employee)
        client = container.client_service.convert_lead(lead.id, seed.admin, "NotAType")
        assert client.client_type == "Buyer"

    def test_convert_missing_lead_is_not_found(self, container, seed):
        with pytest.raises(NotFoundError):
            container.client_service.convert_lead(99999, seed.admin)


class TestClientPayments:
    def _client(self, container, seed):
        lead = container.lead_service.create_lead(
            seed.employee, "Acme", "1", "a@x.com", None, None, None, _contacts("Bob"))
        return container.client_service.convert_lead(lead.id, seed.admin)

    def test_add_payment_converts_to_inr(self, container, seed, monkeypatch):
        client = self._client(container, seed)
        monkeypatch.setattr(
            "app.services.requests.get",
            lambda *a, **k: _FakeResp({"rates": {"INR": 86.0}}),
        )
        entry = container.client_service.add_payment(
            client.id, seed.admin, "Main Acct", "2025-01-01 10:00", 100, "USD")
        assert entry.currency_code == "USD"
        assert entry.amount_inr == 8600.0
        assert entry.conversion_rate == 86.0

    def test_add_payment_rejects_inr(self, container, seed):
        client = self._client(container, seed)
        with pytest.raises(ValidationError):
            container.client_service.add_payment(
                client.id, seed.admin, "Acct", "2025-01-01 10:00", 100, "INR")


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p
