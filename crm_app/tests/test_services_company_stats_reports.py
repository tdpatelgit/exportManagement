"""
Tests for CompanyService, StatsService, ReportService and the shared
advance_client_status() pipeline helper (app/services.py).

CompanyService has a long list of "compulsory" rules for the Our Company
profile that print onto every document - each one gets a test here.
advance_client_status is the mechanism that walks a client through the
document pipeline, including the rule that it must never walk backwards.
"""

import pytest

from app.services import advance_client_status
from app.exceptions import ValidationError, PermissionDeniedError


# --------------------------------------------------------------------------
# Valid payload builders for CompanyService.save
# --------------------------------------------------------------------------
def contact_details():
    return [{"type": "phone", "value": "+91 99999 11111", "is_primary": True},
            {"type": "email", "value": "info@acme.test", "is_primary": True}]


def contact_persons():
    return [{"name": "Ada Admin", "is_primary": True}]


def bank_details():
    return [{"bank_name": "HDFC", "account_number": "123456", "ifsc_code": "HDFC0001",
             "swift_code": "HDFCINBB", "branch": "Morbi", "bank_address": "Main Rd",
             "is_primary": True}]


def lut_details():
    return [{"lut_number": "LUT123", "financial_year": "2025-26", "is_primary": True}]


def save_company(container, user, **over):
    kwargs = dict(
        company_name="Acme Exports", address="Morbi, Gujarat", gstin="24AAAAA0000A1Z5",
        pan_no="AAAAA0000A", iec="1234567890", bin_no="BIN1",
        contact_details=contact_details(), contact_persons=contact_persons(),
        bank_details=bank_details(), lut_details=lut_details(),
    )
    kwargs.update(over)
    return container.company_service.save(user, **kwargs)


# ==========================================================================
# CompanyService
# ==========================================================================
class TestCompanySave:
    def test_happy_path_persists_profile(self, container, seed):
        save_company(container, seed.admin)
        profile = container.company_service.get(seed.company_id)
        assert profile.company_name == "Acme Exports"
        assert profile.gstin == "24AAAAA0000A1Z5"

    def test_saved_child_lists_round_trip(self, container, seed):
        save_company(container, seed.admin)
        profile = container.company_service.get(seed.company_id)
        assert any(b["bank_name"] == "HDFC" for b in profile.bank_details)
        assert any(p["name"] == "Ada Admin" for p in profile.contact_persons)
        assert any(l["lut_number"] == "LUT123" for l in profile.lut_details)

    def test_employee_cannot_save(self, container, seed):
        with pytest.raises(PermissionDeniedError):
            save_company(container, seed.employee)

    def test_company_name_compulsory(self, container, seed):
        with pytest.raises(ValidationError):
            save_company(container, seed.admin, company_name="   ")

    def test_at_least_one_phone_compulsory(self, container, seed):
        only_email = [{"type": "email", "value": "a@b.test"}]
        with pytest.raises(ValidationError) as exc:
            save_company(container, seed.admin, contact_details=only_email)
        assert "phone" in str(exc.value).lower()

    def test_at_least_one_email_compulsory(self, container, seed):
        only_phone = [{"type": "phone", "value": "12345"}]
        with pytest.raises(ValidationError) as exc:
            save_company(container, seed.admin, contact_details=only_phone)
        assert "email" in str(exc.value).lower()

    def test_at_least_one_contact_person_compulsory(self, container, seed):
        with pytest.raises(ValidationError):
            save_company(container, seed.admin, contact_persons=[{"name": "  "}])

    def test_at_least_one_bank_detail_compulsory(self, container, seed):
        with pytest.raises(ValidationError):
            save_company(container, seed.admin, bank_details=[])

    def test_incomplete_bank_detail_rejected(self, container, seed):
        incomplete = [{"bank_name": "HDFC", "account_number": "", "ifsc_code": "",
                       "swift_code": "", "branch": "", "bank_address": ""}]
        with pytest.raises(ValidationError) as exc:
            save_company(container, seed.admin, bank_details=incomplete)
        assert "account number" in str(exc.value)

    def test_lut_row_needs_both_number_and_year(self, container, seed):
        with pytest.raises(ValidationError):
            save_company(container, seed.admin,
                         lut_details=[{"lut_number": "L1", "financial_year": ""}])

    def test_save_is_an_upsert(self, container, seed):
        save_company(container, seed.admin)
        save_company(container, seed.admin, company_name="Acme Exports Renamed")
        assert container.company_service.get(seed.company_id).company_name == "Acme Exports Renamed"

    def test_get_returns_none_before_first_save(self, container, seed):
        assert container.company_service.get(seed.company_id) is None

    def test_profiles_are_per_tenant(self, container, seed):
        save_company(container, seed.admin)
        other = container.tenant_repo.create("Other Co", "other-co")
        other_admin = container.auth_service.create_user(
            other.id, "oadmin", "pw123456", "O Admin", "admin")
        save_company(container, other_admin, company_name="Other Exports")
        assert container.company_service.get(seed.company_id).company_name == "Acme Exports"
        assert container.company_service.get(other.id).company_name == "Other Exports"


# ==========================================================================
# StatsService
# ==========================================================================
def make_lead(container, user, name="Acme"):
    return container.lead_service.create_lead(
        user, name, "1", "a@x.com", None, None, None,
        [{"name": "Bob", "is_primary": True}])


class TestStatsService:
    def test_employee_performance_counts_leads(self, container, seed):
        make_lead(container, seed.employee)
        make_lead(container, seed.employee, "Second")
        rows = container.stats_service.employee_performance(seed.company_id)
        row = next(r for r in rows if r["employee"].id == seed.employee.id)
        assert row["lead_count"] == 2

    def test_employee_performance_counts_communications(self, container, seed):
        lead = make_lead(container, seed.employee)
        container.lead_service.add_communication(
            lead.id, seed.employee, comm_date="2026-01-01 10:00",
            mode="Call", description="Spoke to buyer")
        rows = container.stats_service.employee_performance(seed.company_id)
        row = next(r for r in rows if r["employee"].id == seed.employee.id)
        assert row["communication_count"] == 1

    def test_performance_lists_employees_only_not_admins(self, container, seed):
        rows = container.stats_service.employee_performance(seed.company_id)
        assert all(r["employee"].role == "employee" for r in rows)

    def test_overview_counts_empty(self, container, seed):
        counts = container.stats_service.overview_counts(seed.company_id)
        assert counts["total_leads"] == 0
        assert counts["total_clients"] == 0
        assert counts["open_leads"] == 0

    def test_overview_counts_with_data(self, container, seed):
        make_lead(container, seed.employee)
        lead2 = make_lead(container, seed.employee, "Convert Me")
        container.client_service.convert_lead(lead2.id, seed.admin)
        counts = container.stats_service.overview_counts(seed.company_id)
        assert counts["total_leads"] == 2
        assert counts["total_clients"] == 1
        assert counts["open_leads"] == 1  # the converted one no longer counts

    def test_overview_status_breakdown(self, container, seed):
        lead = make_lead(container, seed.employee)
        container.lead_service.update_status(lead.id, seed.employee, "in_follow_up")
        counts = container.stats_service.overview_counts(seed.company_id)
        assert counts["lead_status_breakdown"]["in_follow_up"] == 1

    def test_stats_are_scoped_per_company(self, container, seed):
        make_lead(container, seed.employee)
        other = container.tenant_repo.create("Other", "other")
        assert container.stats_service.overview_counts(other.id)["total_leads"] == 0


# ==========================================================================
# ReportService
# ==========================================================================
class TestReportService:
    def test_activity_report_lists_employees(self, container, seed):
        rows = container.report_service.activity_report(
            seed.company_id, "2020-01-01", "2030-12-31")
        assert any(r["full_name"] == "Eve Employee" for r in rows)

    def test_activity_report_counts_leads_in_range(self, container, seed):
        make_lead(container, seed.employee)
        rows = container.report_service.activity_report(
            seed.company_id, "2020-01-01", "2030-12-31")
        row = next(r for r in rows if r["id"] == seed.employee.id)
        assert row["leads_generated"] == 1

    def test_activity_report_excludes_out_of_range(self, container, seed):
        make_lead(container, seed.employee)
        rows = container.report_service.activity_report(
            seed.company_id, "1999-01-01", "1999-12-31")
        row = next(r for r in rows if r["id"] == seed.employee.id)
        assert row["leads_generated"] == 0

    def test_payments_total_empty(self, container, seed):
        result = container.report_service.payments_received_total(
            seed.company_id, "2020-01-01", "2030-12-31")
        assert result["payment_count"] == 0
        assert result["total_inr"] == 0

    def test_payments_total_sums_amounts(self, container, seed, monkeypatch):
        lead = make_lead(container, seed.employee)
        client = container.client_service.convert_lead(lead.id, seed.admin)

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"rates": {"INR": 80.0}}

        monkeypatch.setattr("app.services.requests.get", lambda *a, **k: _Resp())
        container.client_service.add_payment(
            client.id, seed.admin, "Acct", "2026-05-05 10:00", 100, "USD")
        result = container.report_service.payments_received_total(
            seed.company_id, "2026-01-01", "2026-12-31")
        assert result["payment_count"] == 1
        assert result["total_inr"] == 8000.0


# ==========================================================================
# advance_client_status pipeline helper
# ==========================================================================
class TestAdvanceClientStatus:
    def _converted_client(self, container, seed):
        lead = make_lead(container, seed.employee)
        client = container.client_service.convert_lead(lead.id, seed.admin)
        return lead, client

    def test_proforma_advances_to_purchase_order_pending(self, container, seed):
        lead, client = self._converted_client(container, seed)
        advance_client_status(container.client_repo, container.lead_repo,
                              lead.id, "proforma_invoice")
        reloaded = container.client_repo.get_by_id(client.id)
        assert reloaded.status == "purchase_order_submission_pending"

    def test_purchase_order_advances_to_purchase_invoice_pending(self, container, seed):
        lead, client = self._converted_client(container, seed)
        advance_client_status(container.client_repo, container.lead_repo,
                              lead.id, "purchase_order")
        assert container.client_repo.get_by_id(client.id).status == \
            "purchase_invoice_submission_pending"

    def test_never_walks_status_backwards(self, container, seed):
        lead, client = self._converted_client(container, seed)
        # Jump forward two stages...
        advance_client_status(container.client_repo, container.lead_repo,
                              lead.id, "export_invoice")
        forward = container.client_repo.get_by_id(client.id).status
        # ...then re-run an earlier document type; status must not regress.
        advance_client_status(container.client_repo, container.lead_repo,
                              lead.id, "proforma_invoice")
        assert container.client_repo.get_by_id(client.id).status == forward

    def test_packing_list_is_a_no_op(self, container, seed):
        lead, client = self._converted_client(container, seed)
        before = container.client_repo.get_by_id(client.id).status
        advance_client_status(container.client_repo, container.lead_repo,
                              lead.id, "packing_list")
        assert container.client_repo.get_by_id(client.id).status == before

    def test_unconverted_lead_is_a_no_op(self, container, seed):
        lead = make_lead(container, seed.employee)
        # Must not raise even though there's no client behind this lead.
        advance_client_status(container.client_repo, container.lead_repo,
                              lead.id, "proforma_invoice")

    def test_missing_lead_id_is_a_no_op(self, container, seed):
        advance_client_status(container.client_repo, container.lead_repo,
                              None, "proforma_invoice")
