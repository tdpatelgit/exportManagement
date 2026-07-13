"""
app/services.py
----------------
The Service layer holds every business rule in the spec ("compulsory field",
"admin only", "one contact required", "auto-convert currency"...). Routes
should never contain this logic directly - they just call a service method
and turn the result (or the exception it raises) into an HTTP response.

Every service takes its repositories as constructor arguments (Dependency
Inversion) instead of importing SqliteXRepository itself, so services can be
unit-tested with fake in-memory repositories.
"""

import os
import uuid
from datetime import datetime, date
from typing import Optional, List

import requests
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.models import (
    User, Lead, Client, ContactPerson, Communication, PaymentEntry, DocumentEntry,
    LEAD_STATUSES, CLIENT_STATUSES, ProductGroup, Product, Quotation, QuotationItem,
    ProformaInvoice, ProformaInvoiceItem, PackingList, PackingListItem,
)
from app.repositories import (
    TenantRepository, UserRepositoryBase, LeadRepositoryBase, ClientRepositoryBase,
    CommunicationRepository, PaymentRepository, DocumentRepository, CompanyRepository,
    ProductGroupRepository, ProductRepository, QuotationRepository, ProformaInvoiceRepository,
    PackingListRepository,
)


# ============================================================
# AUTH SERVICE
# ============================================================
class AuthService:
    """Owns password hashing and credential checking. Nothing else in the
    app should call werkzeug.security directly - that's this class's job."""

    def __init__(self, user_repo: UserRepositoryBase, tenant_repo: TenantRepository):
        self.user_repo = user_repo
        self.tenant_repo = tenant_repo

    def authenticate(self, company_id: int, username: str, password: str) -> Optional[User]:
        if not self.tenant_repo.is_active(company_id):
            return None
        user = self.user_repo.get_by_username(company_id, username)
        if not user or not user.is_active:
            return None
        if not check_password_hash(user.password_hash, password):
            return None
        return user

    def create_user(self, company_id: int, username: str, password: str, full_name: str, role: str) -> User:
        if not username or not password or not full_name:
            raise ValidationError("Username, password and full name are all required.")
        if role not in ("admin", "employee"):
            raise ValidationError("Role must be 'admin' or 'employee'.")
        if self.user_repo.get_by_username(company_id, username):
            raise ValidationError(f"Username '{username}' is already taken.")
        user = User(
            id=None, company_id=company_id, username=username,
            password_hash=generate_password_hash(password),
            full_name=full_name, role=role, is_active=True,
        )
        return self.user_repo.create(user)

    def change_username(self, current_user: User, target_user_id: int, new_username: str) -> User:
        """Employees may only rename themselves; admins may rename anyone
        in their own company (including themselves)."""
        if current_user.id != target_user_id and not current_user.is_admin:
            raise PermissionDeniedError("You can only change your own username.")
        target = self.user_repo.get_by_id(target_user_id)
        if not target or target.company_id != current_user.company_id:
            raise NotFoundError(f"User #{target_user_id} not found.")
        new_username = (new_username or "").strip()
        if not new_username:
            raise ValidationError("Username is required.")
        existing = self.user_repo.get_by_username(current_user.company_id, new_username)
        if existing and existing.id != target.id:
            raise ValidationError(f"Username '{new_username}' is already taken.")
        self.user_repo.update_username(target.id, new_username)
        target.username = new_username
        return target

    def change_password(self, user: User, current_password: str, new_password: str) -> None:
        """Self-service only - the caller must already know their current
        password, so there's no separate permission check to make here."""
        if not check_password_hash(user.password_hash, current_password):
            raise ValidationError("Current password is incorrect.")
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters.")
        self.user_repo.update_password_hash(user.id, generate_password_hash(new_password))


# ============================================================
# CURRENCY CONVERSION SERVICE
# ============================================================
class CurrencyService:
    """Converts a foreign-currency amount to INR.

    Tries a live exchange-rate API first; falls back to the static rates in
    Config if there is no internet connection (so the CRM keeps working
    offline, just with slightly stale rates - clearly recorded in the
    payment record via `conversion_rate` for audit purposes either way).
    """

    def __init__(self, api_url: str, fallback_rates: dict):
        self.api_url = api_url
        self.fallback_rates = fallback_rates

    def get_rate_to_inr(self, currency_code: str) -> float:
        currency_code = currency_code.upper()
        try:
            response = requests.get(
                self.api_url, params={"from": currency_code, "to": "INR"}, timeout=5
            )
            response.raise_for_status()
            data = response.json()
            rate = data.get("rates", {}).get("INR")
            if rate:
                return float(rate)
        except (requests.RequestException, ValueError, KeyError):
            pass  # fall through to the static table below

        if currency_code in self.fallback_rates:
            return float(self.fallback_rates[currency_code])

        raise ValidationError(
            f"No exchange rate available for '{currency_code}' (no internet "
            f"connection and no fallback rate configured). Add one to "
            f"FALLBACK_RATES_TO_INR in config.py."
        )

    def convert(self, amount: float, currency_code: str) -> tuple:
        """Returns (rate_used, amount_in_inr)."""
        if currency_code.upper() == "INR":
            raise ValidationError("Payments must be recorded in a currency other than INR.")
        rate = self.get_rate_to_inr(currency_code)
        return rate, round(amount * rate, 2)


# ============================================================
# COMMUNICATION SERVICE (shared by leads and clients)
# ============================================================
class CommunicationService:
    def __init__(self, comm_repo: CommunicationRepository):
        self.comm_repo = comm_repo

    def add(self, parent_type: str, parent_id: int, employee_id: int,
            comm_date: str, mode: str, description: str,
            follow_up_date: Optional[str] = None) -> Communication:
        if not mode:
            raise ValidationError("Mode of communication is required.")
        if not description or not description.strip():
            raise ValidationError("Please describe what the communication was about.")
        if not comm_date:
            comm_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        comm = Communication(
            id=None, parent_type=parent_type, parent_id=parent_id,
            employee_id=employee_id, comm_date=comm_date, mode=mode,
            description=description.strip(),
            follow_up_date=follow_up_date or None,
        )
        return self.comm_repo.add(comm)

    def list_for(self, parent_type: str, parent_id: int) -> List[Communication]:
        return self.comm_repo.list_for(parent_type, parent_id)

    def upcoming_followups(self, company_id: int, employee_id: Optional[int], within_days: int) -> List[Communication]:
        return self.comm_repo.upcoming_followups(company_id, employee_id, within_days)


# ============================================================
# LEAD SERVICE
# ============================================================
class LeadService:
    def __init__(self, lead_repo: LeadRepositoryBase, comm_service: CommunicationService):
        self.lead_repo = lead_repo
        self.comm_service = comm_service

    # ---- creation --------------------------------------------------
    def create_lead(self, current_user: User, company_name: str, phone: str, email: str,
                     facebook: str, instagram: str, other_social: str,
                     contacts: List[dict]) -> Lead:
        self._validate_compulsory(company_name, phone, email, contacts)
        lead = Lead(
            id=None, company_id=current_user.company_id,
            company_name=company_name.strip(), phone=phone.strip(), email=email.strip(),
            facebook=facebook or None, instagram=instagram or None, other_social=other_social or None,
            status="new", created_by=current_user.id,
        )
        lead.contacts = [
            ContactPerson(id=None, name=c["name"], phone=c.get("phone"), email=c.get("email"),
                          is_primary=bool(c.get("is_primary")))
            for c in contacts
        ]
        # Guarantee exactly one primary contact even if the form didn't mark one.
        if lead.contacts and not any(c.is_primary for c in lead.contacts):
            lead.contacts[0].is_primary = True
        return self.lead_repo.create(lead)

    @staticmethod
    def _validate_compulsory(company_name, phone, email, contacts):
        if not company_name or not company_name.strip():
            raise ValidationError("Company name is compulsory.")
        if not phone or not phone.strip():
            raise ValidationError("Company contact phone number is compulsory.")
        if not email or not email.strip():
            raise ValidationError("Company contact email is compulsory.")
        valid_contacts = [c for c in contacts if c.get("name", "").strip()]
        if not valid_contacts:
            raise ValidationError("At least one company contact person is compulsory.")

    # ---- reads --------------------------------------------------
    def get(self, lead_id: int, company_id: int) -> Lead:
        lead = self.lead_repo.get_by_id(lead_id)
        if not lead or lead.company_id != company_id:
            # 404, not 403 - don't reveal that another company's lead exists.
            raise NotFoundError(f"Lead #{lead_id} not found.")
        return lead

    def list_for_dashboard(self, current_user: User, status: Optional[str] = None) -> List[Lead]:
        """Employees see only their own leads; admins see everyone's (within
        their own company)."""
        if current_user.is_admin:
            return self.lead_repo.list_all(current_user.company_id, status=status)
        return self.lead_repo.list_all(current_user.company_id, employee_id=current_user.id, status=status)

    # ---- writes with permission checks --------------------------------------------------
    def _assert_can_modify(self, lead: Lead, current_user: User):
        if current_user.is_admin:
            return
        if lead.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage leads you generated yourself.")

    def update_compulsory_fields(self, lead_id: int, current_user: User, fields: dict) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError(
                "Only an admin can change a lead's compulsory fields (company name / contact details)."
            )
        self.get(lead_id, current_user.company_id)  # 404s if missing/another company's
        self._validate_compulsory(fields.get("company_name"), fields.get("phone"),
                                   fields.get("email"), [{"name": "existing"}])
        self.lead_repo.update_compulsory_fields(lead_id, fields)

    def update_status(self, lead_id: int, current_user: User, status: str) -> None:
        lead = self.get(lead_id, current_user.company_id)
        self._assert_can_modify(lead, current_user)
        valid_statuses = {s for s, _ in LEAD_STATUSES}
        if status not in valid_statuses:
            raise ValidationError("Invalid lead status.")
        self.lead_repo.update_status(lead_id, status)

    def add_contact(self, lead_id: int, current_user: User, name: str, phone: str, email: str) -> ContactPerson:
        lead = self.get(lead_id, current_user.company_id)
        self._assert_can_modify(lead, current_user)
        if not name or not name.strip():
            raise ValidationError("Contact person name is required.")
        return self.lead_repo.contacts.add(lead_id, ContactPerson(
            id=None, name=name.strip(), phone=phone or None, email=email or None, is_primary=False
        ))

    def set_primary_contact(self, lead_id: int, current_user: User, contact_id: int) -> None:
        lead = self.get(lead_id, current_user.company_id)
        self._assert_can_modify(lead, current_user)
        if not any(c.id == contact_id for c in lead.contacts):
            raise ValidationError("That contact does not belong to this lead.")
        self.lead_repo.contacts.set_primary(lead_id, contact_id)

    def add_communication(self, lead_id: int, current_user: User, **comm_kwargs) -> Communication:
        lead = self.get(lead_id, current_user.company_id)
        self._assert_can_modify(lead, current_user)
        return self.comm_service.add("lead", lead_id, current_user.id, **comm_kwargs)


# ============================================================
# CLIENT SERVICE
# ============================================================
class ClientService:
    def __init__(self, client_repo: ClientRepositoryBase, lead_repo: LeadRepositoryBase,
                 comm_service: CommunicationService, payment_repo: PaymentRepository,
                 document_repo: DocumentRepository, currency_service: CurrencyService,
                 quotation_repo: QuotationRepository,
                 proforma_invoice_repo: Optional[ProformaInvoiceRepository] = None,
                 packing_list_repo: Optional["PackingListRepository"] = None):
        self.client_repo = client_repo
        self.lead_repo = lead_repo
        self.comm_service = comm_service
        self.payment_repo = payment_repo
        self.document_repo = document_repo
        self.currency_service = currency_service
        self.quotation_repo = quotation_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.packing_list_repo = packing_list_repo

    # ---- lead -> client conversion (admin only) --------------------------------------------------
    def convert_lead(self, lead_id: int, admin_user: User, client_type: str = "Buyer") -> Client:
        if not admin_user.is_admin:
            raise PermissionDeniedError("Only an admin can approve a lead for conversion to client.")
        lead = self.lead_repo.get_by_id(lead_id)
        if not lead or lead.company_id != admin_user.company_id:
            raise NotFoundError(f"Lead #{lead_id} not found.")
        if lead.is_converted:
            raise ValidationError("This lead has already been converted to a client.")
        if client_type not in ("Supplier", "Exporter", "Buyer"):
            client_type = "Buyer"

        client = Client(
            id=None, company_id=lead.company_id, lead_id=lead.id, company_name=lead.company_name,
            phone=lead.phone, email=lead.email, facebook=lead.facebook, instagram=lead.instagram,
            other_social=lead.other_social, client_type=client_type,
            status="proforma_invoice_submission_pending", created_by=admin_user.id,
        )
        return self.client_repo.convert_from_lead(client, lead.contacts)

    # ---- reads --------------------------------------------------
    def get(self, client_id: int, company_id: int) -> Client:
        client = self.client_repo.get_by_id(client_id)
        if not client or client.company_id != company_id:
            # 404, not 403 - don't reveal that another company's client exists.
            raise NotFoundError(f"Client #{client_id} not found.")
        return client

    def list_all(self, company_id: int, client_type: Optional[str] = None,
                 status: Optional[str] = None) -> List[Client]:
        return self.client_repo.list_all(company_id, client_type, status)

    # ---- writes --------------------------------------------------
    def update_compulsory_fields(self, client_id: int, current_user: User, fields: dict) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can change a client's compulsory fields.")
        self.get(client_id, current_user.company_id)  # 404s if missing/another company's
        if not fields.get("company_name") or not fields.get("phone") or not fields.get("email"):
            raise ValidationError("Company name, phone and email are all compulsory.")
        self.client_repo.update_compulsory_fields(client_id, fields)

    def update_status(self, client_id: int, current_user: User, status: str) -> None:
        self.get(client_id, current_user.company_id)  # 404s if missing/another company's
        valid_statuses = {s for s, _ in CLIENT_STATUSES}
        if status not in valid_statuses:
            raise ValidationError("Invalid client status.")
        self.client_repo.update_status(client_id, status)

    def add_contact(self, client_id: int, current_user: User, name: str, phone: str, email: str) -> ContactPerson:
        self.get(client_id, current_user.company_id)  # 404s if missing/another company's
        if not name or not name.strip():
            raise ValidationError("Contact person name is required.")
        return self.client_repo.contacts.add(client_id, ContactPerson(
            id=None, name=name.strip(), phone=phone or None, email=email or None, is_primary=False
        ))

    def set_primary_contact(self, client_id: int, current_user: User, contact_id: int) -> None:
        client = self.get(client_id, current_user.company_id)
        if not any(c.id == contact_id for c in client.contacts):
            raise ValidationError("That contact does not belong to this client.")
        self.client_repo.contacts.set_primary(client_id, contact_id)

    def add_communication(self, client_id: int, current_user: User, **comm_kwargs) -> Communication:
        self.get(client_id, current_user.company_id)  # 404s if missing/another company's
        return self.comm_service.add("client", client_id, current_user.id, **comm_kwargs)

    def add_payment(self, client_id: int, current_user: User, account_name: str, payment_datetime: str,
                     amount_original: float, currency_code: str) -> PaymentEntry:
        self.get(client_id, current_user.company_id)
        if not account_name or not account_name.strip():
            raise ValidationError("Account name is required for a payment entry.")
        if amount_original is None or amount_original <= 0:
            raise ValidationError("Payment amount must be a positive number.")
        rate, amount_inr = self.currency_service.convert(amount_original, currency_code)
        payment = PaymentEntry(
            id=None, client_id=client_id, account_name=account_name.strip(),
            payment_datetime=payment_datetime or datetime.now().strftime("%Y-%m-%d %H:%M"),
            amount_original=amount_original, currency_code=currency_code.upper(),
            conversion_rate=rate, amount_inr=amount_inr,
        )
        return self.payment_repo.add(payment)

    def add_document(self, client_id: int, current_user: User, document_name: str, document_type: str,
                      document_date: str, notes: str) -> DocumentEntry:
        self.get(client_id, current_user.company_id)
        if not document_name or not document_name.strip():
            raise ValidationError("Document name is required.")
        if not document_type or not document_type.strip():
            raise ValidationError("Document type is required.")
        doc = DocumentEntry(
            id=None, client_id=client_id, document_name=document_name.strip(),
            document_type=document_type.strip(),
            document_date=document_date or date.today().isoformat(), notes=notes or None,
        )
        return self.document_repo.add(doc)

    def document_feed(self, client: Client) -> List[dict]:
        """One combined, date-sorted list for the client's 'Documents' card:
        manually recorded DocumentEntry rows plus every quotation/proforma
        invoice made against the client's originating lead (these aren't
        separate sections here - they're just auto-generated document types
        feeding the same card). Future auto-generated document types should
        feed into this the same way. `link` carries its own kwarg dict so
        each document type's route can name its id param however it likes."""
        rows = [
            {
                "name": d.document_name, "type": d.document_type, "date": d.document_date,
                "notes": d.notes, "link": None,
            }
            for d in self.document_repo.list_for_client(client.id)
        ]
        for q in self.quotation_repo.list_for_lead(client.lead_id) if client.lead_id else []:
            rows.append({
                "name": q.quotation_number, "type": "Quotation", "date": q.quotation_date,
                "notes": f"{q.buyer_name} · $ {q.invoice_value_usd:,.2f}",
                "link": ("quotations.view_quotation", {"quotation_id": q.id}),
            })
        if self.proforma_invoice_repo:
            for pi in self.proforma_invoice_repo.list_for_lead(client.lead_id) if client.lead_id else []:
                rows.append({
                    "name": pi.invoice_number, "type": "Proforma Invoice", "date": pi.invoice_date,
                    "notes": f"{pi.consignee_name} · $ {pi.invoice_value_usd:,.2f}",
                    "link": ("proforma_invoices.view_proforma_invoice", {"proforma_invoice_id": pi.id}),
                })
        if self.packing_list_repo:
            for pl in self.packing_list_repo.list_for_lead(client.lead_id) if client.lead_id else []:
                rows.append({
                    "name": f"Packing Details · {pl.proforma_invoice_no or ('#%d' % pl.id)}",
                    "type": "Packing Details", "date": pl.packing_date,
                    "notes": f"{pl.total_boxes:,.0f} boxes · {pl.total_quantity:,.2f} SQM/LM",
                    "link": ("packing_lists.view_packing_list", {"packing_list_id": pl.id}),
                })
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows


# ============================================================
# COMPANY SERVICE (our own company profile - admin only)
# ============================================================
class CompanyService:
    def __init__(self, company_repo: CompanyRepository):
        self.company_repo = company_repo

    def get(self, company_id: int):
        return self.company_repo.get(company_id)

    def save(self, current_user: User, company_name: str, address: str, gstin: str, pan_no: str, iec: str,
              bin_no: str, contact_details: list, contact_persons: list, bank_details: list, lut_details: list) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can edit our company's profile.")
        if not company_name or not company_name.strip():
            raise ValidationError("Company name is compulsory.")

        valid_details = [d for d in contact_details if d.get("value", "").strip()]
        if not any(d["type"] == "phone" for d in valid_details):
            raise ValidationError("At least one company phone number is compulsory.")
        if not any(d["type"] == "email" for d in valid_details):
            raise ValidationError("At least one company email is compulsory.")
        for d in valid_details:
            if not d.get("type", "").strip():
                raise ValidationError("Every contact detail row needs a type.")

        valid_persons = [p for p in contact_persons if p.get("name", "").strip()]
        if not valid_persons:
            raise ValidationError("At least one company contact person is compulsory.")

        if not bank_details:
            raise ValidationError("At least one bank detail is compulsory.")
        bank_fields = ["bank_name", "account_number", "ifsc_code", "swift_code", "branch", "bank_address"]
        bank_labels = {
            "bank_name": "bank name", "account_number": "account number", "ifsc_code": "IFSC code",
            "swift_code": "SWIFT code", "branch": "branch", "bank_address": "bank address",
        }
        for b in bank_details:
            missing = [bank_labels[f] for f in bank_fields if not b.get(f, "").strip()]
            if missing:
                raise ValidationError(f"Bank detail '{b.get('bank_name') or '(unnamed)'}' is missing: {', '.join(missing)}.")
        valid_banks = bank_details

        for l in lut_details:
            if not l.get("lut_number", "").strip() or not l.get("financial_year", "").strip():
                raise ValidationError("Every LUT row needs both a LUT number and a financial year.")

        our_company_id = self.company_repo.upsert(
            current_user.company_id, company_name.strip(), address, gstin, pan_no, iec, bin_no
        )
        self.company_repo.replace_contact_details(our_company_id, valid_details)
        self.company_repo.replace_contact_persons(our_company_id, valid_persons)
        self.company_repo.replace_bank_details(our_company_id, valid_banks)
        self.company_repo.replace_lut_details(our_company_id, lut_details)


# ============================================================
# STATS SERVICE (powers the admin dashboard)
# ============================================================
class StatsService:
    def __init__(self, user_repo: UserRepositoryBase, lead_repo: LeadRepositoryBase,
                 comm_repo: CommunicationRepository, client_repo: ClientRepositoryBase):
        self.user_repo = user_repo
        self.lead_repo = lead_repo
        self.comm_repo = comm_repo
        self.client_repo = client_repo

    def employee_performance(self, company_id: int) -> List[dict]:
        """One row per employee: leads generated + communications logged.
        This directly satisfies 'admin ... can see how many leads is
        generated by each employee and how many communications is done by
        each employee'."""
        employees = self.user_repo.list_all(company_id, role="employee")
        lead_counts = self.lead_repo.count_by_employee(company_id)
        comm_counts = self.comm_repo.count_by_employee(company_id)
        return [
            {
                "employee": emp,
                "lead_count": lead_counts.get(emp.id, 0),
                "communication_count": comm_counts.get(emp.id, 0),
            }
            for emp in employees
        ]

    def overview_counts(self, company_id: int) -> dict:
        all_leads = self.lead_repo.list_all(company_id)
        all_clients = self.client_repo.list_all(company_id)
        status_breakdown = {}
        for lead in all_leads:
            status_breakdown[lead.status] = status_breakdown.get(lead.status, 0) + 1
        client_status_breakdown = {}
        for client in all_clients:
            client_status_breakdown[client.status] = client_status_breakdown.get(client.status, 0) + 1
        return {
            "total_leads": len(all_leads),
            "total_clients": len(all_clients),
            "open_leads": len([l for l in all_leads if not l.is_converted]),
            "lead_status_breakdown": status_breakdown,
            "client_status_breakdown": client_status_breakdown,
        }


# ============================================================
# REPORT SERVICE (basic monthly/quarterly/yearly summaries)
# ============================================================
class ReportService:
    """Generates a summary of activity between two dates, grouped by
    employee. This is the first slice of the 'monthly/quarterly/yearly
    reports' future plan - it works today because it only needs data
    already captured (leads.created_at, communications.comm_date)."""

    def __init__(self, db):
        self.db = db  # direct Database access - reports run ad-hoc aggregate SQL

    def activity_report(self, company_id: int, start_date: str, end_date: str) -> List[dict]:
        rows = self.db.query(
            """
            SELECT u.id, u.full_name,
                   (SELECT COUNT(*) FROM leads l
                     WHERE l.created_by = u.id AND l.company_id = ?
                       AND date(l.created_at) BETWEEN date(?) AND date(?)
                   ) AS leads_generated,
                   (SELECT COUNT(*) FROM communications c
                     WHERE c.employee_id = u.id AND date(c.comm_date) BETWEEN date(?) AND date(?)
                   ) AS communications_logged,
                   (SELECT COUNT(*) FROM clients cl
                     WHERE cl.lead_id IN (SELECT id FROM leads WHERE created_by = u.id)
                       AND cl.company_id = ?
                       AND date(cl.created_at) BETWEEN date(?) AND date(?)
                   ) AS clients_converted
            FROM users u
            WHERE u.role = 'employee' AND u.company_id = ?
            ORDER BY u.full_name
            """,
            (company_id, start_date, end_date, start_date, end_date,
             company_id, start_date, end_date, company_id),
        )
        return [dict(r) for r in rows]

    def payments_received_total(self, company_id: int, start_date: str, end_date: str) -> dict:
        row = self.db.query_one(
            """SELECT COUNT(*) AS payment_count, COALESCE(SUM(ph.amount_inr), 0) AS total_inr
               FROM payment_history ph JOIN clients c ON c.id = ph.client_id
               WHERE c.company_id = ? AND date(ph.payment_datetime) BETWEEN date(?) AND date(?)""",
            (company_id, start_date, end_date),
        )
        return dict(row) if row else {"payment_count": 0, "total_inr": 0}


# ============================================================
# PRODUCT SERVICE (folder-tree catalog: groups nest to any depth, each
# group holds any number of subgroups + products)
# ============================================================
class ProductService:
    def __init__(self, group_repo: ProductGroupRepository, product_repo: ProductRepository,
                 upload_folder: str, allowed_extensions: set):
        self.group_repo = group_repo
        self.product_repo = product_repo
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions

    # ---- browsing --------------------------------------------------
    def get_group(self, group_id: int, company_id: int) -> ProductGroup:
        group = self.group_repo.get_by_id(group_id)
        if not group or group.company_id != company_id:
            raise NotFoundError(f"Product group #{group_id} not found.")
        return group

    def breadcrumb(self, company_id: int, group_id: Optional[int]) -> List[ProductGroup]:
        if not group_id:
            return []
        self.get_group(group_id, company_id)  # 404s if missing/another company's before walking up
        return self.group_repo.list_ancestors(group_id)

    def list_contents(self, company_id: int, group_id: Optional[int]):
        """Returns (subgroups, products) for a folder - group_id=None is the catalog root."""
        if group_id is not None:
            self.get_group(group_id, company_id)  # 404s if missing/another company's
        return (self.group_repo.list_children(company_id, group_id),
                self.product_repo.list_in_group(company_id, group_id))

    def get_product(self, product_id: int, company_id: int) -> Product:
        product = self.product_repo.get_by_id(product_id)
        if not product or product.company_id != company_id:
            raise NotFoundError(f"Product #{product_id} not found.")
        return product

    # ---- groups --------------------------------------------------
    def create_group(self, current_user: User, name: str, parent_id: Optional[int]) -> ProductGroup:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Group name is compulsory.")
        if parent_id is not None:
            self.get_group(parent_id, current_user.company_id)  # 404s if the parent doesn't exist
        return self.group_repo.create(current_user.company_id, name.strip(), parent_id)

    def rename_group(self, current_user: User, group_id: int, name: str) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Group name is compulsory.")
        self.get_group(group_id, current_user.company_id)
        self.group_repo.update(group_id, name.strip())

    def delete_group(self, current_user: User, group_id: int) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        self.get_group(group_id, current_user.company_id)
        self._delete_group_images_recursive(current_user.company_id, group_id)
        self.group_repo.delete(group_id)  # cascades to subgroups/products in the DB

    def _delete_group_images_recursive(self, company_id: int, group_id: int) -> None:
        """Product image files live on disk, not in the DB, so cascading
        deletes don't clean them up on their own - walk the subtree first."""
        for product in self.product_repo.list_in_group(company_id, group_id):
            self._delete_image_file(product.photo_path)
            self._delete_image_file(product.dimension_photo_path)
        for subgroup in self.group_repo.list_children(company_id, group_id):
            self._delete_group_images_recursive(company_id, subgroup.id)

    # ---- products --------------------------------------------------
    def create_product(self, current_user: User, group_id: Optional[int], product_name: str,
                        description: str, hsn_code: str, packing: str, quantity: str,
                        alternate_quantity: str, weight_class: str, price_usd: str, alt_text: str,
                        photo_file, dimension_photo_file) -> Product:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not product_name or not product_name.strip():
            raise ValidationError("Product name is compulsory.")
        if group_id is not None:
            self.get_group(group_id, current_user.company_id)

        photo_path = self._save_image(photo_file)
        dimension_photo_path = self._save_image(dimension_photo_file)
        product = Product(
            id=None, company_id=current_user.company_id, group_id=group_id, product_name=product_name.strip(),
            description=description or None, hsn_code=hsn_code or None, packing=packing or None,
            quantity=quantity or None, alternate_quantity=alternate_quantity or None,
            weight_class=weight_class or None, price_usd=self._parse_price(price_usd),
            photo_path=photo_path, dimension_photo_path=dimension_photo_path, alt_text=alt_text or None,
        )
        return self.product_repo.create(product)

    def update_product(self, current_user: User, product_id: int, product_name: str,
                        description: str, hsn_code: str, packing: str, quantity: str,
                        alternate_quantity: str, weight_class: str, price_usd: str, alt_text: str,
                        photo_file, dimension_photo_file) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not product_name or not product_name.strip():
            raise ValidationError("Product name is compulsory.")
        existing = self.get_product(product_id, current_user.company_id)

        fields = {
            "product_name": product_name.strip(), "description": description or None,
            "hsn_code": hsn_code or None, "packing": packing or None, "quantity": quantity or None,
            "alternate_quantity": alternate_quantity or None, "weight_class": weight_class or None,
            "price_usd": self._parse_price(price_usd), "alt_text": alt_text or None,
        }
        if photo_file and photo_file.filename:
            fields["photo_path"] = self._save_image(photo_file)
            self._delete_image_file(existing.photo_path)
        if dimension_photo_file and dimension_photo_file.filename:
            fields["dimension_photo_path"] = self._save_image(dimension_photo_file)
            self._delete_image_file(existing.dimension_photo_path)

        self.product_repo.update(product_id, fields)

    def delete_product(self, current_user: User, product_id: int) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        product = self.get_product(product_id, current_user.company_id)
        self._delete_image_file(product.photo_path)
        self._delete_image_file(product.dimension_photo_path)
        self.product_repo.delete(product_id)

    @staticmethod
    def _parse_price(price_usd: str) -> Optional[float]:
        if not price_usd or not price_usd.strip():
            return None
        try:
            return round(float(price_usd), 2)
        except ValueError:
            raise ValidationError("Price (USD) must be a number.")

    # ---- image storage --------------------------------------------------
    def _save_image(self, file_storage) -> Optional[str]:
        """Saves an uploaded image under the product upload folder with a
        collision-proof name and returns the path relative to static/
        (so templates can do url_for('static', filename=path))."""
        if not file_storage or not file_storage.filename:
            return None
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in self.allowed_extensions:
            raise ValidationError(f"Unsupported image type '.{ext}'. Allowed: {', '.join(sorted(self.allowed_extensions))}.")
        os.makedirs(self.upload_folder, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{filename}"
        file_storage.save(os.path.join(self.upload_folder, stored_name))
        return f"uploads/products/{stored_name}"

    def _delete_image_file(self, relative_path: Optional[str]) -> None:
        if not relative_path:
            return
        full_path = os.path.join(self.upload_folder, os.path.basename(relative_path))
        if os.path.exists(full_path):
            os.remove(full_path)


# ============================================================
# QUOTATION SERVICE
# ============================================================
class QuotationService:
    def __init__(self, quotation_repo: QuotationRepository, product_repo: ProductRepository,
                 lead_repo: LeadRepositoryBase):
        self.quotation_repo = quotation_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo

    # ---- reads --------------------------------------------------
    def get(self, quotation_id: int, company_id: int) -> Quotation:
        quotation = self.quotation_repo.get_by_id(quotation_id)
        if not quotation or quotation.company_id != company_id:
            # 404, not 403 - don't reveal that another company's quotation exists.
            raise NotFoundError(f"Quotation #{quotation_id} not found.")
        return quotation

    def list_all(self, company_id: int) -> List[Quotation]:
        return self.quotation_repo.list_all(company_id)

    def list_for_lead(self, lead_id: Optional[int]) -> List[Quotation]:
        """Used by both the lead detail page and the client detail page -
        see QuotationRepository.list_for_lead for why a client doesn't need
        its own quotation link. Unscoped by company_id because the caller
        always already owns (has fetched-and-checked) the lead/client this
        is being looked up for."""
        if not lead_id:
            return []
        return self.quotation_repo.list_for_lead(lead_id)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, quotation: Quotation, current_user: User):
        if current_user.is_admin:
            return
        if quotation.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage quotations you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, company_id: int, quotation_date: str) -> str:
        """QT{YYYYMMDD}{seq} where seq is that day's quotation count + 1 for
        this company, zero-padded to 3 digits (e.g. QT20260702001)."""
        date_part = quotation_date.replace("-", "")
        prefix = f"QT{date_part}"
        seq = self.quotation_repo.count_for_date_prefix(company_id, prefix) + 1
        return f"{prefix}{seq:03d}"

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[QuotationItem]:
        items = []
        for i, raw in enumerate(raw_items, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            try:
                quantity_value = float(raw.get("quantity_value") or 0)
                price_usd = float(raw.get("price_usd") or 0)
                quantity_boxes = float(raw["quantity_boxes"]) if raw.get("quantity_boxes") else None
            except ValueError:
                raise ValidationError(f"Row {i}: quantity and price must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None

            # Qty is authoritatively boxes x the catalog product's Alternate
            # Quantity whenever both are known - the client-side value is only
            # a convenience preview, not trusted for the stored total. Only
            # trust a product from this same company - otherwise a crafted
            # product_id could pull another company's catalog data in.
            if product_id and quantity_boxes:
                product = self.product_repo.get_by_id(product_id)
                if product and product.company_id == company_id and product.alternate_quantity:
                    try:
                        quantity_value = round(quantity_boxes * float(product.alternate_quantity), 2)
                    except ValueError:
                        pass

            if quantity_value <= 0:
                raise ValidationError(f"Row {i} ('{product_name}'): quantity is compulsory and must be greater than zero.")
            if price_usd < 0:
                raise ValidationError(f"Row {i} ('{product_name}'): price can't be negative.")
            items.append(QuotationItem(
                id=None, quotation_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                quantity_boxes=quantity_boxes, quantity_value=quantity_value,
                unit=(raw.get("unit") or "SQM").strip() or "SQM",
                price_usd=price_usd, total_usd=round(quantity_value * price_usd, 2),
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    def _build_header(self, current_user: User, fields: dict, items: List[QuotationItem]) -> Quotation:
        buyer_name = (fields.get("buyer_name") or "").strip()
        if not buyer_name:
            raise ValidationError("Buyer name is compulsory.")
        quotation_date = (fields.get("quotation_date") or "").strip() or date.today().isoformat()

        def _float(key, default=0):
            raw = fields.get(key)
            try:
                return float(raw) if raw not in (None, "") else default
            except ValueError:
                raise ValidationError(f"'{key}' must be a number.")

        def _int(key, default):
            raw = fields.get(key)
            try:
                return int(raw) if raw not in (None, "") else default
            except ValueError:
                raise ValidationError(f"'{key}' must be a whole number.")

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            # Only trust a lead from this same company - otherwise a crafted
            # lead_id could attach this quotation to another company's lead.
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        quotation = Quotation(
            id=None, company_id=current_user.company_id, quotation_number="", quotation_date=quotation_date,
            buyer_name=buyer_name, created_by=current_user.id, lead_id=lead_id,
            buyer_address=(fields.get("buyer_address") or "").strip() or None,
            buyer_reference_no=(fields.get("buyer_reference_no") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            packing_details=(fields.get("packing_details") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            shipping_mode=(fields.get("shipping_mode") or "").strip() or None,
            shipping_terms=(fields.get("shipping_terms") or "").strip() or None,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            price_validity_days=_int("price_validity_days", 30),
            remarks=(fields.get("remarks") or "").strip() or None,
            sea_freight=_float("sea_freight", 0),
            insurance=_float("insurance", 0),
            certification=_float("certification", 0),
            other_charges=_float("other_charges", 0),
            discount_amount=_float("discount_amount", 0),
            bank_name=(fields.get("bank_name") or "").strip() or None,
            bank_account_number=(fields.get("bank_account_number") or "").strip() or None,
            bank_ifsc_code=(fields.get("bank_ifsc_code") or "").strip() or None,
            bank_swift_code=(fields.get("bank_swift_code") or "").strip() or None,
            bank_branch=(fields.get("bank_branch") or "").strip() or None,
            bank_address=(fields.get("bank_address") or "").strip() or None,
            items=items,
        )
        return quotation

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> Quotation:
        items = self._build_items(current_user.company_id, raw_items)
        quotation = self._build_header(current_user, fields, items)
        quotation.quotation_number = self._generate_number(current_user.company_id, quotation.quotation_date)
        return self.quotation_repo.create(quotation)

    def update(self, current_user: User, quotation_id: int, fields: dict, raw_items: list) -> Quotation:
        existing = self.get(quotation_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        quotation = self._build_header(current_user, fields, items)
        self.quotation_repo.update(quotation_id, quotation)
        return self.get(quotation_id, current_user.company_id)

    def delete(self, current_user: User, quotation_id: int) -> None:
        existing = self.get(quotation_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.quotation_repo.delete(quotation_id)


# ============================================================
# PROFORMA INVOICE SERVICE
# ============================================================
class ProformaInvoiceService:
    """Mirrors QuotationService layer-for-layer. The one thing it adds is
    build_prefill_from_quotation - a Proforma Invoice can be started from an
    existing Quotation, copying its buyer/product/bank data in as a one-time
    prefill (not a live link) the same way `?lead_id=` prefills a new
    Quotation from a Lead."""

    def __init__(self, invoice_repo: ProformaInvoiceRepository, product_repo: ProductRepository,
                 lead_repo: LeadRepositoryBase, quotation_repo: QuotationRepository):
        self.invoice_repo = invoice_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.quotation_repo = quotation_repo

    # ---- reads --------------------------------------------------
    def get(self, invoice_id: int, company_id: int) -> ProformaInvoice:
        invoice = self.invoice_repo.get_by_id(invoice_id)
        if not invoice or invoice.company_id != company_id:
            # 404, not 403 - don't reveal that another company's invoice exists.
            raise NotFoundError(f"Proforma invoice #{invoice_id} not found.")
        return invoice

    def list_all(self, company_id: int) -> List[ProformaInvoice]:
        return self.invoice_repo.list_all(company_id)

    def list_for_lead(self, lead_id: Optional[int]) -> List[ProformaInvoice]:
        """Same shape as QuotationService.list_for_lead - unscoped by
        company_id because the caller already owns the lead/client."""
        if not lead_id:
            return []
        return self.invoice_repo.list_for_lead(lead_id)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, invoice: ProformaInvoice, current_user: User):
        if current_user.is_admin:
            return
        if invoice.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage proforma invoices you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, company_id: int, invoice_date: str) -> str:
        """PI{YYYYMMDD}{seq} where seq is that day's proforma invoice count + 1
        for this company, zero-padded to 3 digits (e.g. PI20260702001)."""
        date_part = invoice_date.replace("-", "")
        prefix = f"PI{date_part}"
        seq = self.invoice_repo.count_for_date_prefix(company_id, prefix) + 1
        return f"{prefix}{seq:03d}"

    # ---- prefill from an existing quotation --------------------------------------------------
    def build_prefill_from_quotation(self, quotation: Quotation) -> dict:
        """Caller must have already loaded `quotation` via
        QuotationService.get(quotation_id, current_user.company_id) so
        cross-company ownership is already verified."""
        fields = {
            "quotation_id": quotation.id,
            "lead_id": quotation.lead_id,
            "consignee_name": quotation.buyer_name,
            "consignee_address": quotation.buyer_address,
            "buyer_order_no": quotation.buyer_reference_no,
            "port_of_loading": quotation.port_of_loading,
            "port_of_discharge": quotation.port_of_discharge,
            "container_details": quotation.container_details,
            "terms_of_delivery": quotation.shipping_terms,
            "payment_terms": quotation.payment_terms,
            "sea_freight": quotation.sea_freight,
            "insurance": quotation.insurance,
            "certification": quotation.certification,
            "other_charges": quotation.other_charges,
            "discount_amount": quotation.discount_amount,
            "bank_name": quotation.bank_name,
            "bank_account_number": quotation.bank_account_number,
            "bank_ifsc_code": quotation.bank_ifsc_code,
            "bank_swift_code": quotation.bank_swift_code,
            "bank_branch": quotation.bank_branch,
            "bank_address": quotation.bank_address,
        }
        items = [
            {
                "product_id": item.product_id, "product_name": item.product_name,
                "hsn_code": item.hsn_code, "quantity_boxes": item.quantity_boxes,
                "quantity_value": item.quantity_value, "unit": item.unit,
                "price_usd": item.price_usd,
            }
            for item in quotation.items
        ]
        return {"fields": fields, "items": items}

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[ProformaInvoiceItem]:
        items = []
        for i, raw in enumerate(raw_items, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            try:
                quantity_value = float(raw.get("quantity_value") or 0)
                price_usd = float(raw.get("price_usd") or 0)
                quantity_boxes = float(raw["quantity_boxes"]) if raw.get("quantity_boxes") else None
                pallets = float(raw["pallets"]) if raw.get("pallets") else None
            except ValueError:
                raise ValidationError(f"Row {i}: quantity, pallets and price must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None

            # Same trust boundary as QuotationService._build_items - only
            # trust a product from this same company for the Boxes x
            # Alternate Quantity auto-calc.
            if product_id and quantity_boxes:
                product = self.product_repo.get_by_id(product_id)
                if product and product.company_id == company_id and product.alternate_quantity:
                    try:
                        quantity_value = round(quantity_boxes * float(product.alternate_quantity), 2)
                    except ValueError:
                        pass

            if quantity_value <= 0:
                raise ValidationError(f"Row {i} ('{product_name}'): quantity is compulsory and must be greater than zero.")
            if price_usd < 0:
                raise ValidationError(f"Row {i} ('{product_name}'): price can't be negative.")
            items.append(ProformaInvoiceItem(
                id=None, proforma_invoice_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                pallets=pallets, quantity_boxes=quantity_boxes, quantity_value=quantity_value,
                unit=(raw.get("unit") or "SQM").strip() or "SQM",
                price_usd=price_usd, total_usd=round(quantity_value * price_usd, 2),
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    def _build_header(self, current_user: User, fields: dict, items: List[ProformaInvoiceItem]) -> ProformaInvoice:
        consignee_name = (fields.get("consignee_name") or "").strip()
        if not consignee_name:
            raise ValidationError("Consignee name is compulsory.")
        invoice_date = (fields.get("invoice_date") or "").strip() or date.today().isoformat()

        def _float(key, default=0):
            raw = fields.get(key)
            try:
                return float(raw) if raw not in (None, "") else default
            except ValueError:
                raise ValidationError(f"'{key}' must be a number.")

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            # Only trust a lead from this same company - otherwise a crafted
            # lead_id could attach this invoice to another company's lead.
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        quotation_id = int(fields["quotation_id"]) if fields.get("quotation_id") else None
        if quotation_id is not None:
            # Only trust a quotation from this same company - same reasoning as lead_id above.
            quotation = self.quotation_repo.get_by_id(quotation_id)
            if not quotation or quotation.company_id != current_user.company_id:
                quotation_id = None

        invoice = ProformaInvoice(
            id=None, company_id=current_user.company_id, invoice_number="", invoice_date=invoice_date,
            consignee_name=consignee_name, created_by=current_user.id, lead_id=lead_id,
            quotation_id=quotation_id,
            export_ref_no=(fields.get("export_ref_no") or "").strip() or None,
            buyer_order_no=(fields.get("buyer_order_no") or "").strip() or None,
            other_reference=(fields.get("other_reference") or "").strip() or None,
            consignee_address=(fields.get("consignee_address") or "").strip() or None,
            notify_name=(fields.get("notify_name") or "").strip() or None,
            notify_address=(fields.get("notify_address") or "").strip() or None,
            country_of_origin=(fields.get("country_of_origin") or "").strip() or "INDIA",
            country_of_destination=(fields.get("country_of_destination") or "").strip() or None,
            vessel_flight=(fields.get("vessel_flight") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            final_destination=(fields.get("final_destination") or "").strip() or None,
            transhipment=(fields.get("transhipment") or "").strip() or None,
            partial_shipment=(fields.get("partial_shipment") or "").strip() or None,
            variation_in_qty=(fields.get("variation_in_qty") or "").strip() or None,
            delivery_period=(fields.get("delivery_period") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            terms_of_delivery=(fields.get("terms_of_delivery") or "").strip() or None,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            sea_freight=_float("sea_freight", 0),
            insurance=_float("insurance", 0),
            certification=_float("certification", 0),
            other_charges=_float("other_charges", 0),
            discount_amount=_float("discount_amount", 0),
            bank_name=(fields.get("bank_name") or "").strip() or None,
            bank_account_number=(fields.get("bank_account_number") or "").strip() or None,
            bank_ifsc_code=(fields.get("bank_ifsc_code") or "").strip() or None,
            bank_swift_code=(fields.get("bank_swift_code") or "").strip() or None,
            bank_branch=(fields.get("bank_branch") or "").strip() or None,
            bank_address=(fields.get("bank_address") or "").strip() or None,
            items=items,
        )
        return invoice

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> ProformaInvoice:
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        invoice.invoice_number = self._generate_number(current_user.company_id, invoice.invoice_date)
        return self.invoice_repo.create(invoice)

    def update(self, current_user: User, invoice_id: int, fields: dict, raw_items: list) -> ProformaInvoice:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        self.invoice_repo.update(invoice_id, invoice)
        return self.get(invoice_id, current_user.company_id)

    def delete(self, current_user: User, invoice_id: int) -> None:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.invoice_repo.delete(invoice_id)


# ============================================================
# PACKING LIST SERVICE ("Packing Details" document)
# ============================================================
class PackingListService:
    """Mirrors ProformaInvoiceService layer-for-layer. A Packing List is
    normally generated from a Proforma Invoice - either automatically right
    after the PI is created (the 'auto_packing' checkbox on the PI form) or
    manually via `?proforma_invoice_id=` prefill - but is then its own
    independent, editable record."""

    def __init__(self, packing_list_repo: PackingListRepository,
                 invoice_repo: ProformaInvoiceRepository, lead_repo: LeadRepositoryBase):
        self.packing_list_repo = packing_list_repo
        self.invoice_repo = invoice_repo
        self.lead_repo = lead_repo

    # ---- reads --------------------------------------------------
    def get(self, packing_list_id: int, company_id: int) -> PackingList:
        packing_list = self.packing_list_repo.get_by_id(packing_list_id)
        if not packing_list or packing_list.company_id != company_id:
            # 404, not 403 - don't reveal that another company's document exists.
            raise NotFoundError(f"Packing list #{packing_list_id} not found.")
        return packing_list

    def list_all(self, company_id: int) -> List[PackingList]:
        return self.packing_list_repo.list_all(company_id)

    def list_for_lead(self, lead_id: Optional[int]) -> List[PackingList]:
        if not lead_id:
            return []
        return self.packing_list_repo.list_for_lead(lead_id)

    def list_for_proforma(self, proforma_invoice_id: int, company_id: int) -> List[PackingList]:
        return [pl for pl in self.packing_list_repo.list_for_proforma(proforma_invoice_id)
                if pl.company_id == company_id]

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, packing_list: PackingList, current_user: User):
        if current_user.is_admin:
            return
        if packing_list.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage packing lists you created yourself.")

    # ---- prefill / auto-generation from a proforma invoice --------------------------------------------------
    def build_prefill_from_invoice(self, invoice: ProformaInvoice) -> dict:
        """Caller must have already loaded `invoice` via
        ProformaInvoiceService.get(...) so ownership is verified. One packing
        row per invoice line; PCS and BOX PER PALLET aren't known to the PI,
        so they start blank for the user to fill in."""
        fields = {
            "proforma_invoice_id": invoice.id,
            "lead_id": invoice.lead_id,
            "packing_date": invoice.invoice_date,
            "remarks": "MADE IN INDIA",
        }
        items = [
            {
                "description": item.product_name,
                "box_per_pallet": "",
                "model_name": "",
                "no_of_pallet": item.pallets or "",
                "boxes": item.quantity_boxes or "",
                "pcs": "",
                "quantity_value": item.quantity_value or "",
            }
            for item in invoice.items
        ]
        return {"fields": fields, "items": items}

    def create_from_invoice(self, current_user: User, invoice: ProformaInvoice) -> PackingList:
        """The 'generate automatically after the proforma invoice' path -
        same data as build_prefill_from_invoice, saved without a form trip."""
        built = self.build_prefill_from_invoice(invoice)
        return self.create(current_user, built["fields"], built["items"])

    # ---- validation --------------------------------------------------
    def _build_items(self, raw_items: list) -> List[PackingListItem]:
        items = []
        for i, raw in enumerate(raw_items, start=1):
            description = (raw.get("description") or "").strip()
            if not description:
                continue

            def _optional_float(key):
                value = raw.get(key)
                if value in (None, ""):
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    raise ValidationError(f"Row {i} ('{description}'): '{key}' must be a number.")

            items.append(PackingListItem(
                id=None, packing_list_id=None, sr_no=i, description=description,
                box_per_pallet=_optional_float("box_per_pallet"),
                model_name=(raw.get("model_name") or "").strip() or None,
                no_of_pallet=_optional_float("no_of_pallet"),
                boxes=_optional_float("boxes"),
                pcs=_optional_float("pcs"),
                quantity_value=_optional_float("quantity_value"),
            ))
        if not items:
            raise ValidationError("At least one packing line is compulsory.")
        return items

    def _build_header(self, current_user: User, fields: dict, items: List[PackingListItem]) -> PackingList:
        packing_date = (str(fields.get("packing_date") or "")).strip() or date.today().isoformat()

        proforma_invoice_id = int(fields["proforma_invoice_id"]) if fields.get("proforma_invoice_id") else None
        proforma_invoice_no = None
        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if proforma_invoice_id is not None:
            # Only trust an invoice from this same company - otherwise a
            # crafted id could attach this document to another company's PI.
            invoice = self.invoice_repo.get_by_id(proforma_invoice_id)
            if not invoice or invoice.company_id != current_user.company_id:
                proforma_invoice_id = None
            else:
                proforma_invoice_no = invoice.invoice_number
                lead_id = lead_id or invoice.lead_id

        if lead_id is not None:
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        return PackingList(
            id=None, company_id=current_user.company_id, packing_date=packing_date,
            created_by=current_user.id, proforma_invoice_id=proforma_invoice_id,
            proforma_invoice_no=proforma_invoice_no, lead_id=lead_id,
            remarks=(fields.get("remarks") or "").strip() or None,
            items=items,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> PackingList:
        items = self._build_items(raw_items)
        packing_list = self._build_header(current_user, fields, items)
        return self.packing_list_repo.create(packing_list)

    def update(self, current_user: User, packing_list_id: int, fields: dict, raw_items: list) -> PackingList:
        existing = self.get(packing_list_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(raw_items)
        packing_list = self._build_header(current_user, fields, items)
        self.packing_list_repo.update(packing_list_id, packing_list)
        return self.get(packing_list_id, current_user.company_id)

    def delete(self, current_user: User, packing_list_id: int) -> None:
        existing = self.get(packing_list_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.packing_list_repo.delete(packing_list_id)
