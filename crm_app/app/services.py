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
)
from app.repositories import (
    UserRepositoryBase, LeadRepositoryBase, ClientRepositoryBase,
    CommunicationRepository, PaymentRepository, DocumentRepository, CompanyRepository,
    ProductGroupRepository, ProductRepository, QuotationRepository,
)


# ============================================================
# AUTH SERVICE
# ============================================================
class AuthService:
    """Owns password hashing and credential checking. Nothing else in the
    app should call werkzeug.security directly - that's this class's job."""

    def __init__(self, user_repo: UserRepositoryBase):
        self.user_repo = user_repo

    def authenticate(self, username: str, password: str) -> Optional[User]:
        user = self.user_repo.get_by_username(username)
        if not user or not user.is_active:
            return None
        if not check_password_hash(user.password_hash, password):
            return None
        return user

    def create_user(self, username: str, password: str, full_name: str, role: str) -> User:
        if not username or not password or not full_name:
            raise ValidationError("Username, password and full name are all required.")
        if role not in ("admin", "employee"):
            raise ValidationError("Role must be 'admin' or 'employee'.")
        if self.user_repo.get_by_username(username):
            raise ValidationError(f"Username '{username}' is already taken.")
        user = User(
            id=None, username=username,
            password_hash=generate_password_hash(password),
            full_name=full_name, role=role, is_active=True,
        )
        return self.user_repo.create(user)

    def change_username(self, current_user: User, target_user_id: int, new_username: str) -> User:
        """Employees may only rename themselves; admins may rename anyone
        (including themselves)."""
        if current_user.id != target_user_id and not current_user.is_admin:
            raise PermissionDeniedError("You can only change your own username.")
        target = self.user_repo.get_by_id(target_user_id)
        if not target:
            raise NotFoundError(f"User #{target_user_id} not found.")
        new_username = (new_username or "").strip()
        if not new_username:
            raise ValidationError("Username is required.")
        existing = self.user_repo.get_by_username(new_username)
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

    def upcoming_followups(self, employee_id: Optional[int], within_days: int) -> List[Communication]:
        return self.comm_repo.upcoming_followups(employee_id, within_days)


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
            id=None, company_name=company_name.strip(), phone=phone.strip(), email=email.strip(),
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
    def get(self, lead_id: int) -> Lead:
        lead = self.lead_repo.get_by_id(lead_id)
        if not lead:
            raise NotFoundError(f"Lead #{lead_id} not found.")
        return lead

    def list_for_dashboard(self, current_user: User, status: Optional[str] = None) -> List[Lead]:
        """Employees see only their own leads; admins see everyone's."""
        if current_user.is_admin:
            return self.lead_repo.list_all(status=status)
        return self.lead_repo.list_all(employee_id=current_user.id, status=status)

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
        self._validate_compulsory(fields.get("company_name"), fields.get("phone"),
                                   fields.get("email"), [{"name": "existing"}])
        self.lead_repo.update_compulsory_fields(lead_id, fields)

    def update_status(self, lead_id: int, current_user: User, status: str) -> None:
        lead = self.get(lead_id)
        self._assert_can_modify(lead, current_user)
        valid_statuses = {s for s, _ in LEAD_STATUSES}
        if status not in valid_statuses:
            raise ValidationError("Invalid lead status.")
        self.lead_repo.update_status(lead_id, status)

    def add_contact(self, lead_id: int, current_user: User, name: str, phone: str, email: str) -> ContactPerson:
        lead = self.get(lead_id)
        self._assert_can_modify(lead, current_user)
        if not name or not name.strip():
            raise ValidationError("Contact person name is required.")
        return self.lead_repo.contacts.add(lead_id, ContactPerson(
            id=None, name=name.strip(), phone=phone or None, email=email or None, is_primary=False
        ))

    def set_primary_contact(self, lead_id: int, current_user: User, contact_id: int) -> None:
        lead = self.get(lead_id)
        self._assert_can_modify(lead, current_user)
        if not any(c.id == contact_id for c in lead.contacts):
            raise ValidationError("That contact does not belong to this lead.")
        self.lead_repo.contacts.set_primary(lead_id, contact_id)

    def add_communication(self, lead_id: int, current_user: User, **comm_kwargs) -> Communication:
        lead = self.get(lead_id)
        self._assert_can_modify(lead, current_user)
        return self.comm_service.add("lead", lead_id, current_user.id, **comm_kwargs)


# ============================================================
# CLIENT SERVICE
# ============================================================
class ClientService:
    def __init__(self, client_repo: ClientRepositoryBase, lead_repo: LeadRepositoryBase,
                 comm_service: CommunicationService, payment_repo: PaymentRepository,
                 document_repo: DocumentRepository, currency_service: CurrencyService):
        self.client_repo = client_repo
        self.lead_repo = lead_repo
        self.comm_service = comm_service
        self.payment_repo = payment_repo
        self.document_repo = document_repo
        self.currency_service = currency_service

    # ---- lead -> client conversion (admin only) --------------------------------------------------
    def convert_lead(self, lead_id: int, admin_user: User, client_type: str = "Buyer") -> Client:
        if not admin_user.is_admin:
            raise PermissionDeniedError("Only an admin can approve a lead for conversion to client.")
        lead = self.lead_repo.get_by_id(lead_id)
        if not lead:
            raise NotFoundError(f"Lead #{lead_id} not found.")
        if lead.is_converted:
            raise ValidationError("This lead has already been converted to a client.")
        if client_type not in ("Supplier", "Exporter", "Buyer"):
            client_type = "Buyer"

        client = Client(
            id=None, lead_id=lead.id, company_name=lead.company_name, phone=lead.phone,
            email=lead.email, facebook=lead.facebook, instagram=lead.instagram,
            other_social=lead.other_social, client_type=client_type,
            status="proforma_invoice_submission_pending", created_by=admin_user.id,
        )
        client = self.client_repo.create_from_lead(client, lead.id)
        # Carry every contact person across from the lead.
        self.lead_repo.contacts.copy_all(lead.id, client.id, self.client_repo.contacts)
        self.lead_repo.mark_converted(lead.id, client.id)
        return client

    # ---- reads --------------------------------------------------
    def get(self, client_id: int) -> Client:
        client = self.client_repo.get_by_id(client_id)
        if not client:
            raise NotFoundError(f"Client #{client_id} not found.")
        return client

    def list_all(self, client_type: Optional[str] = None, status: Optional[str] = None) -> List[Client]:
        return self.client_repo.list_all(client_type, status)

    # ---- writes --------------------------------------------------
    def update_compulsory_fields(self, client_id: int, current_user: User, fields: dict) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can change a client's compulsory fields.")
        if not fields.get("company_name") or not fields.get("phone") or not fields.get("email"):
            raise ValidationError("Company name, phone and email are all compulsory.")
        self.client_repo.update_compulsory_fields(client_id, fields)

    def update_status(self, client_id: int, current_user: User, status: str) -> None:
        valid_statuses = {s for s, _ in CLIENT_STATUSES}
        if status not in valid_statuses:
            raise ValidationError("Invalid client status.")
        self.client_repo.update_status(client_id, status)

    def add_contact(self, client_id: int, name: str, phone: str, email: str) -> ContactPerson:
        if not name or not name.strip():
            raise ValidationError("Contact person name is required.")
        return self.client_repo.contacts.add(client_id, ContactPerson(
            id=None, name=name.strip(), phone=phone or None, email=email or None, is_primary=False
        ))

    def set_primary_contact(self, client_id: int, contact_id: int) -> None:
        client = self.get(client_id)
        if not any(c.id == contact_id for c in client.contacts):
            raise ValidationError("That contact does not belong to this client.")
        self.client_repo.contacts.set_primary(client_id, contact_id)

    def add_communication(self, client_id: int, current_user: User, **comm_kwargs) -> Communication:
        self.get(client_id)  # 404s if missing
        return self.comm_service.add("client", client_id, current_user.id, **comm_kwargs)

    def add_payment(self, client_id: int, account_name: str, payment_datetime: str,
                     amount_original: float, currency_code: str) -> PaymentEntry:
        self.get(client_id)
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

    def add_document(self, client_id: int, document_name: str, document_type: str,
                      document_date: str, notes: str) -> DocumentEntry:
        self.get(client_id)
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


# ============================================================
# COMPANY SERVICE (our own company profile - admin only)
# ============================================================
class CompanyService:
    def __init__(self, company_repo: CompanyRepository):
        self.company_repo = company_repo

    def get(self):
        return self.company_repo.get()

    def save(self, current_user: User, company_name: str, address: str, gstin: str, pan_no: str, iec: str,
              lut: str, bin_no: str, contact_details: list, contact_persons: list, bank_details: list) -> None:
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

        self.company_repo.upsert(company_name.strip(), address, gstin, pan_no, iec, lut, bin_no)
        self.company_repo.replace_contact_details(valid_details)
        self.company_repo.replace_contact_persons(valid_persons)
        self.company_repo.replace_bank_details(valid_banks)


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

    def employee_performance(self) -> List[dict]:
        """One row per employee: leads generated + communications logged.
        This directly satisfies 'admin ... can see how many leads is
        generated by each employee and how many communications is done by
        each employee'."""
        employees = self.user_repo.list_all(role="employee")
        lead_counts = self.lead_repo.count_by_employee()
        comm_counts = self.comm_repo.count_by_employee()
        return [
            {
                "employee": emp,
                "lead_count": lead_counts.get(emp.id, 0),
                "communication_count": comm_counts.get(emp.id, 0),
            }
            for emp in employees
        ]

    def overview_counts(self) -> dict:
        all_leads = self.lead_repo.list_all()
        all_clients = self.client_repo.list_all()
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

    def activity_report(self, start_date: str, end_date: str) -> List[dict]:
        rows = self.db.query(
            """
            SELECT u.id, u.full_name,
                   (SELECT COUNT(*) FROM leads l
                     WHERE l.created_by = u.id AND date(l.created_at) BETWEEN date(?) AND date(?)
                   ) AS leads_generated,
                   (SELECT COUNT(*) FROM communications c
                     WHERE c.employee_id = u.id AND date(c.comm_date) BETWEEN date(?) AND date(?)
                   ) AS communications_logged,
                   (SELECT COUNT(*) FROM clients cl
                     WHERE cl.lead_id IN (SELECT id FROM leads WHERE created_by = u.id)
                       AND date(cl.created_at) BETWEEN date(?) AND date(?)
                   ) AS clients_converted
            FROM users u
            WHERE u.role = 'employee'
            ORDER BY u.full_name
            """,
            (start_date, end_date, start_date, end_date, start_date, end_date),
        )
        return [dict(r) for r in rows]

    def payments_received_total(self, start_date: str, end_date: str) -> dict:
        row = self.db.query_one(
            """SELECT COUNT(*) AS payment_count, COALESCE(SUM(amount_inr), 0) AS total_inr
               FROM payment_history
               WHERE date(payment_datetime) BETWEEN date(?) AND date(?)""",
            (start_date, end_date),
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
    def get_group(self, group_id: int) -> ProductGroup:
        group = self.group_repo.get_by_id(group_id)
        if not group:
            raise NotFoundError(f"Product group #{group_id} not found.")
        return group

    def breadcrumb(self, group_id: Optional[int]) -> List[ProductGroup]:
        return self.group_repo.list_ancestors(group_id) if group_id else []

    def list_contents(self, group_id: Optional[int]):
        """Returns (subgroups, products) for a folder - group_id=None is the catalog root."""
        return self.group_repo.list_children(group_id), self.product_repo.list_in_group(group_id)

    def get_product(self, product_id: int) -> Product:
        product = self.product_repo.get_by_id(product_id)
        if not product:
            raise NotFoundError(f"Product #{product_id} not found.")
        return product

    # ---- groups --------------------------------------------------
    def create_group(self, current_user: User, name: str, parent_id: Optional[int]) -> ProductGroup:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Group name is compulsory.")
        if parent_id is not None:
            self.get_group(parent_id)  # 404s if the parent doesn't exist
        return self.group_repo.create(name.strip(), parent_id)

    def rename_group(self, current_user: User, group_id: int, name: str) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Group name is compulsory.")
        self.get_group(group_id)
        self.group_repo.update(group_id, name.strip())

    def delete_group(self, current_user: User, group_id: int) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        self.get_group(group_id)
        self._delete_group_images_recursive(group_id)
        self.group_repo.delete(group_id)  # cascades to subgroups/products in the DB

    def _delete_group_images_recursive(self, group_id: int) -> None:
        """Product image files live on disk, not in the DB, so cascading
        deletes don't clean them up on their own - walk the subtree first."""
        for product in self.product_repo.list_in_group(group_id):
            self._delete_image_file(product.photo_path)
            self._delete_image_file(product.dimension_photo_path)
        for subgroup in self.group_repo.list_children(group_id):
            self._delete_group_images_recursive(subgroup.id)

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
            self.get_group(group_id)

        photo_path = self._save_image(photo_file)
        dimension_photo_path = self._save_image(dimension_photo_file)
        product = Product(
            id=None, group_id=group_id, product_name=product_name.strip(),
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
        existing = self.get_product(product_id)

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
        product = self.get_product(product_id)
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
    def __init__(self, quotation_repo: QuotationRepository, product_repo: ProductRepository):
        self.quotation_repo = quotation_repo
        self.product_repo = product_repo

    # ---- reads --------------------------------------------------
    def get(self, quotation_id: int) -> Quotation:
        quotation = self.quotation_repo.get_by_id(quotation_id)
        if not quotation:
            raise NotFoundError(f"Quotation #{quotation_id} not found.")
        return quotation

    def list_all(self) -> List[Quotation]:
        return self.quotation_repo.list_all()

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, quotation: Quotation, current_user: User):
        if current_user.is_admin:
            return
        if quotation.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage quotations you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, quotation_date: str) -> str:
        """QT{YYYYMMDD}{seq} where seq is that day's quotation count + 1,
        zero-padded to 3 digits (e.g. QT20260702001)."""
        date_part = quotation_date.replace("-", "")
        prefix = f"QT{date_part}"
        seq = self.quotation_repo.count_for_date_prefix(prefix) + 1
        return f"{prefix}{seq:03d}"

    # ---- validation --------------------------------------------------
    def _build_items(self, raw_items: list) -> List[QuotationItem]:
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
            # a convenience preview, not trusted for the stored total.
            if product_id and quantity_boxes:
                product = self.product_repo.get_by_id(product_id)
                if product and product.alternate_quantity:
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

        client_id = int(fields["client_id"]) if fields.get("client_id") else None

        quotation = Quotation(
            id=None, quotation_number="", quotation_date=quotation_date, buyer_name=buyer_name,
            created_by=current_user.id, client_id=client_id,
            buyer_address=(fields.get("buyer_address") or "").strip() or None,
            buyer_reference_no=(fields.get("buyer_reference_no") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            packing_details=(fields.get("packing_details") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            shipping_mode=(fields.get("shipping_mode") or "").strip() or None,
            shipping_terms=(fields.get("shipping_terms") or "").strip() or None,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            advance_percent=_float("advance_percent", 0),
            against_bl_percent=_float("against_bl_percent", 0),
            price_validity_days=_int("price_validity_days", 30),
            remarks=(fields.get("remarks") or "").strip() or None,
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
        items = self._build_items(raw_items)
        quotation = self._build_header(current_user, fields, items)
        quotation.quotation_number = self._generate_number(quotation.quotation_date)
        return self.quotation_repo.create(quotation)

    def update(self, current_user: User, quotation_id: int, fields: dict, raw_items: list) -> Quotation:
        existing = self.get(quotation_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(raw_items)
        quotation = self._build_header(current_user, fields, items)
        self.quotation_repo.update(quotation_id, quotation)
        return self.get(quotation_id)

    def delete(self, current_user: User, quotation_id: int) -> None:
        existing = self.get(quotation_id)
        self._assert_can_modify(existing, current_user)
        self.quotation_repo.delete(quotation_id)
