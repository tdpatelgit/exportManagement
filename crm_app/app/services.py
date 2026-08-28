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
import re
import json
import uuid
import shutil
import zipfile
import tempfile
import sqlite3
import dataclasses
from datetime import datetime, date
from typing import Optional, List

import requests
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from app.exceptions import ValidationError, PermissionDeniedError, NotFoundError
from app.models import (
    User, Lead, Party, Supplier, Transporter, Permit, MiscCurrency, MiscNatureOfContract, MiscPortOfLoading, DEFAULT_CURRENCIES, ContactPerson, Communication, PaymentEntry, DocumentEntry,
    LEAD_STATUSES, CLIENT_STATUSES, CLIENT_STATUS_ADVANCE_ON, PRODUCT_UNITS, Category, Product,
    ProductPalletType, ProductFolder,
    Design, Quotation, QuotationItem, ProformaInvoice, ProformaInvoiceItem,
    PurchaseOrder, PurchaseOrderItem, PurchaseInvoice, PurchaseInvoiceItem, PackingList, PackingListItem,
    DocumentVersion, PURCHASE_TYPES, DEFAULT_PURCHASE_TYPE, EXEMPTION_IGST_PERCENT,
    PROFORMA_STATUSES, PROFORMA_STATUS_DRAFT, PROFORMA_STATUS_CONFIRMED,
    ExportInvoice, ExportInvoiceItem, EXPORT_TAX_MODES, EXPORT_TAX_MODE_IGST, EXPORT_TAX_MODE_LUT,
    EXPORT_LOADING_TYPES, EXPORT_LOADING_SELF_SEALING,
    ExportPackingList, ExportPackingListItem,
)
from app.repositories import (
    TenantRepository, UserRepositoryBase, LeadRepositoryBase, PartyRepositoryBase, SupplierRepositoryBase,
    TransporterRepositoryBase,
    CommunicationRepository, PaymentRepository, DocumentRepository, CompanyRepository,
    CategoryRepository, ProductRepository, ProductPalletTypeRepository, ProductFolderRepository, DesignRepository,
    QuotationRepository, ProformaInvoiceRepository, PurchaseOrderRepository, PurchaseInvoiceRepository,
    ExportInvoiceRepository, ExportPackingListRepository,
    PackingListRepository, DocumentVersionRepository, PermitRepository, MiscCurrencyRepository, MiscNatureOfContractRepository,
    MiscPortOfLoadingRepository,
)
from app.database import Database, SCHEMA_VERSION
from app.utils import drops_insurance, drops_sea_freight, is_fob_terms

# Fix for NameError: drops_certification not defined
def drops_certification(shipping_terms):
    """Helper to check if shipping terms drop certification requirement."""
    if not shipping_terms:
        return False
    return "NO CERTIFICATION" in shipping_terms.upper() or "WITHOUT CERTIFICATION" in shipping_terms.upper()



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
# MISCELLANEOUS DROP LISTS (Administration -> Miscellaneous)
# ============================================================
class MiscListService:
    """The hand-maintained option lists behind the app's dropdowns:

      - CURRENCY (name of currency + currency symbol),
      - NATURE OF CONTRACT (a name), which fills the delivery-terms field
        on every document whatever that document calls it ("Nature of
        contract", "Shipping terms", "Terms of delivery"),
      - PORT OF LOADING (a port name + that port's PIN code).

    Admin-only to edit (the route enforces that); everything is
    company-scoped. Currency reads fall back to DEFAULT_CURRENCIES while a
    company has not added one of its own, so that dropdown is never empty;
    nature of contract and port of loading have no built-in list, since the
    values are entirely a company's own trade terms and shipping ports."""

    def __init__(self, currency_repo: MiscCurrencyRepository,
                 nature_of_contract_repo: Optional[MiscNatureOfContractRepository] = None,
                 port_of_loading_repo: Optional[MiscPortOfLoadingRepository] = None):
        self.currency_repo = currency_repo
        self.nature_of_contract_repo = nature_of_contract_repo
        self.port_of_loading_repo = port_of_loading_repo

    # ---- reads --------------------------------------------------
    def list_currencies(self, company_id: int) -> List[MiscCurrency]:
        """Only what the company actually saved - what the Miscellaneous
        page manages."""
        return self.currency_repo.list_all(company_id)

    def currency_options(self, company_id: int) -> List[MiscCurrency]:
        """What the dropdowns show: the saved list, or the built-in
        fallback while it is still empty."""
        stored = self.currency_repo.list_all(company_id)
        if stored:
            return stored
        return [MiscCurrency(id=None, company_id=company_id, name=name, symbol=symbol)
                for name, symbol in DEFAULT_CURRENCIES]

    def get_currency(self, currency_id: int, company_id: int) -> MiscCurrency:
        currency = self.currency_repo.get_by_id(currency_id)
        if not currency or currency.company_id != company_id:
            # 404, not 403 - don't reveal another company's rows.
            raise NotFoundError(f"Currency #{currency_id} not found.")
        return currency

    def find_currency(self, company_id: int, name: str) -> Optional[MiscCurrency]:
        """The option matching a submitted currency name (stored list first,
        then the fallback) - used to snapshot name + symbol onto a document."""
        name = (name or "").strip()
        if not name:
            return None
        for option in self.currency_options(company_id):
            if option.name.upper() == name.upper():
                return option
        return None

    def resolve_currency(self, company_id: int, name: Optional[str]) -> tuple:
        """(name, symbol) to snapshot onto a document for a submitted
        currency name. An unknown name is kept as typed with no symbol
        rather than being dropped, and a blank one stays blank so the
        document falls back to what its sheet always printed."""
        name = (name or "").strip() or None
        if not name:
            return None, None
        match = self.find_currency(company_id, name)
        return (match.name, match.symbol) if match else (name, None)

    # ---- writes --------------------------------------------------
    def _clean(self, current_user: User, fields: dict) -> MiscCurrency:
        name = (fields.get("name") or "").strip()
        symbol = (fields.get("symbol") or "").strip()
        if not name:
            raise ValidationError("Name of currency is compulsory.")
        if not symbol:
            raise ValidationError("Currency symbol is compulsory.")
        return MiscCurrency(id=None, company_id=current_user.company_id, name=name, symbol=symbol)

    def create_currency(self, current_user: User, fields: dict) -> MiscCurrency:
        currency = self._clean(current_user, fields)
        if self.currency_repo.find_by_name(current_user.company_id, currency.name):
            raise ValidationError(f"'{currency.name}' is already on the currency list.")
        return self.currency_repo.create(currency)

    def update_currency(self, current_user: User, currency_id: int, fields: dict) -> MiscCurrency:
        self.get_currency(currency_id, current_user.company_id)
        currency = self._clean(current_user, fields)
        clash = self.currency_repo.find_by_name(current_user.company_id, currency.name)
        if clash and clash.id != currency_id:
            raise ValidationError(f"'{currency.name}' is already on the currency list.")
        self.currency_repo.update(currency_id, currency)
        return self.get_currency(currency_id, current_user.company_id)

    def delete_currency(self, current_user: User, currency_id: int) -> MiscCurrency:
        currency = self.get_currency(currency_id, current_user.company_id)
        self.currency_repo.delete(currency_id)
        return currency

    # ---- nature of contract --------------------------------------------------
    def list_nature_of_contracts(self, company_id: int) -> List[MiscNatureOfContract]:
        return self.nature_of_contract_repo.list_all(company_id)

    def get_nature_of_contract(self, entry_id: int, company_id: int) -> MiscNatureOfContract:
        entry = self.nature_of_contract_repo.get_by_id(entry_id)
        if not entry or entry.company_id != company_id:
            raise NotFoundError(f"Nature of contract #{entry_id} not found.")
        return entry

    def _clean_nature_of_contract(self, current_user: User, fields: dict) -> MiscNatureOfContract:
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValidationError("Name is compulsory.")
        return MiscNatureOfContract(id=None, company_id=current_user.company_id, name=name)

    def create_nature_of_contract(self, current_user: User, fields: dict) -> MiscNatureOfContract:
        entry = self._clean_nature_of_contract(current_user, fields)
        if self.nature_of_contract_repo.find_by_name(current_user.company_id, entry.name):
            raise ValidationError(f"'{entry.name}' is already on the nature of contract list.")
        return self.nature_of_contract_repo.create(entry)

    def update_nature_of_contract(self, current_user: User, entry_id: int, fields: dict) -> MiscNatureOfContract:
        self.get_nature_of_contract(entry_id, current_user.company_id)
        entry = self._clean_nature_of_contract(current_user, fields)
        clash = self.nature_of_contract_repo.find_by_name(current_user.company_id, entry.name)
        if clash and clash.id != entry_id:
            raise ValidationError(f"'{entry.name}' is already on the nature of contract list.")
        self.nature_of_contract_repo.update(entry_id, entry)
        return self.get_nature_of_contract(entry_id, current_user.company_id)

    def delete_nature_of_contract(self, current_user: User, entry_id: int) -> MiscNatureOfContract:
        entry = self.get_nature_of_contract(entry_id, current_user.company_id)
        self.nature_of_contract_repo.delete(entry_id)
        return entry

    # ---- port of loading --------------------------------------------------
    def list_ports_of_loading(self, company_id: int) -> List[MiscPortOfLoading]:
        return self.port_of_loading_repo.list_all(company_id)

    def get_port_of_loading(self, entry_id: int, company_id: int) -> MiscPortOfLoading:
        entry = self.port_of_loading_repo.get_by_id(entry_id)
        if not entry or entry.company_id != company_id:
            raise NotFoundError(f"Port of loading #{entry_id} not found.")
        return entry

    def find_port_of_loading(self, company_id: int, name: str) -> Optional[MiscPortOfLoading]:
        """The saved port matching a submitted name - used to pick up that
        port's PIN code without asking for it a second time."""
        name = (name or "").strip()
        if not name:
            return None
        return self.port_of_loading_repo.find_by_name(company_id, name)

    def _clean_port_of_loading(self, current_user: User, fields: dict) -> MiscPortOfLoading:
        name = (fields.get("name") or "").strip()
        pin_code = (fields.get("pin_code") or "").strip()
        if not name:
            raise ValidationError("Port of Loading is compulsory.")
        if not pin_code:
            raise ValidationError("Port of loading Pincode is compulsory.")
        return MiscPortOfLoading(id=None, company_id=current_user.company_id, name=name, pin_code=pin_code)

    def create_port_of_loading(self, current_user: User, fields: dict) -> MiscPortOfLoading:
        entry = self._clean_port_of_loading(current_user, fields)
        if self.port_of_loading_repo.find_by_name(current_user.company_id, entry.name):
            raise ValidationError(f"'{entry.name}' is already on the port of loading list.")
        return self.port_of_loading_repo.create(entry)

    def update_port_of_loading(self, current_user: User, entry_id: int, fields: dict) -> MiscPortOfLoading:
        self.get_port_of_loading(entry_id, current_user.company_id)
        entry = self._clean_port_of_loading(current_user, fields)
        clash = self.port_of_loading_repo.find_by_name(current_user.company_id, entry.name)
        if clash and clash.id != entry_id:
            raise ValidationError(f"'{entry.name}' is already on the port of loading list.")
        self.port_of_loading_repo.update(entry_id, entry)
        return self.get_port_of_loading(entry_id, current_user.company_id)

    def delete_port_of_loading(self, current_user: User, entry_id: int) -> MiscPortOfLoading:
        entry = self.get_port_of_loading(entry_id, current_user.company_id)
        self.port_of_loading_repo.delete(entry_id)
        return entry


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
# PARTY SERVICE (Buyer / Exporter - identical behaviour; one instance per
# type, constructed with that type's repo and parent_type. Supplier has its
# own SupplierService below since its shape has diverged.)
# ============================================================
class PartyService:
    def __init__(self, party_repo: PartyRepositoryBase, parent_type: str, lead_repo: LeadRepositoryBase,
                 comm_service: CommunicationService, payment_repo: PaymentRepository,
                 document_repo: DocumentRepository, currency_service: CurrencyService,
                 quotation_repo: QuotationRepository,
                 proforma_invoice_repo: Optional[ProformaInvoiceRepository] = None,
                 packing_list_repo: Optional[PackingListRepository] = None,
                 purchase_order_repo: Optional[PurchaseOrderRepository] = None):
        self.party_repo = party_repo
        self.parent_type = parent_type  # 'buyer' | 'exporter'
        self.lead_repo = lead_repo
        self.comm_service = comm_service
        self.payment_repo = payment_repo
        self.document_repo = document_repo
        self.currency_service = currency_service
        self.quotation_repo = quotation_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.packing_list_repo = packing_list_repo
        self.purchase_order_repo = purchase_order_repo

    @property
    def client_type(self) -> str:
        return self.parent_type.capitalize()  # 'Buyer' | 'Exporter' - matches leads.converted_client_type

    # ---- lead -> party conversion (admin only) --------------------------------------------------
    def convert_lead(self, lead_id: int, admin_user: User) -> Party:
        if not admin_user.is_admin:
            raise PermissionDeniedError(f"Only an admin can approve a lead for conversion to {self.client_type.lower()}.")
        lead = self.lead_repo.get_by_id(lead_id)
        if not lead or lead.company_id != admin_user.company_id:
            raise NotFoundError(f"Lead #{lead_id} not found.")
        if lead.is_converted:
            raise ValidationError("This lead has already been converted.")

        party = Party(
            id=None, company_id=lead.company_id, lead_id=lead.id, company_name=lead.company_name,
            phone=lead.phone, email=lead.email, facebook=lead.facebook, instagram=lead.instagram,
            other_social=lead.other_social,
            status="proforma_invoice_submission_pending", created_by=admin_user.id,
        )
        return self.party_repo.convert_from_lead(party, lead.contacts)

    # ---- add directly (admin only, no originating lead) --------------------------------------------------
    def create(self, current_user: User, fields: dict, contacts: Optional[List[dict]] = None) -> Party:
        if not current_user.is_admin:
            raise PermissionDeniedError(f"Only an admin can add a new {self.client_type.lower()}.")
        company_name = (fields.get("company_name") or "").strip()
        phone = (fields.get("phone") or "").strip()
        email = (fields.get("email") or "").strip()
        if not company_name or not phone or not email:
            raise ValidationError("Company name, phone and email are all compulsory.")

        party = Party(
            id=None, company_id=current_user.company_id, lead_id=None, company_name=company_name,
            phone=phone, email=email,
            facebook=(fields.get("facebook") or "").strip() or None,
            instagram=(fields.get("instagram") or "").strip() or None,
            other_social=(fields.get("other_social") or "").strip() or None,
            address=(fields.get("address") or "").strip() or None,
            status="proforma_invoice_submission_pending", created_by=current_user.id,
        )
        party = self.party_repo.create(party)
        for c in (contacts or []):
            if not (c.get("name") or "").strip():
                continue
            self.party_repo.contacts.add(party.id, ContactPerson(
                id=None, name=c["name"].strip(), phone=c.get("phone") or None, email=c.get("email") or None,
                is_primary=bool(c.get("is_primary")),
            ))
        return self.get(party.id, current_user.company_id)

    # ---- reads --------------------------------------------------
    def get(self, party_id: int, company_id: int) -> Party:
        party = self.party_repo.get_by_id(party_id)
        if not party or party.company_id != company_id:
            # 404, not 403 - don't reveal that another company's record exists.
            raise NotFoundError(f"{self.client_type} #{party_id} not found.")
        return party

    def list_all(self, company_id: int, status: Optional[str] = None) -> List[Party]:
        return self.party_repo.list_all(company_id, status)

    # ---- writes --------------------------------------------------
    def update_compulsory_fields(self, party_id: int, current_user: User, fields: dict) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError(f"Only an admin can change a {self.client_type.lower()}'s compulsory fields.")
        self.get(party_id, current_user.company_id)  # 404s if missing/another company's
        if not fields.get("company_name") or not fields.get("phone") or not fields.get("email"):
            raise ValidationError("Company name, phone and email are all compulsory.")
        self.party_repo.update_compulsory_fields(party_id, fields)

    def delete(self, party_id: int, current_user: User) -> Party:
        """Admin-only, and the route additionally re-checks the admin's own
        password before calling this - deleting a party also deletes its
        contacts, communications, payments and recorded documents."""
        if not current_user.is_admin:
            raise PermissionDeniedError(f"Only an admin can delete a {self.client_type.lower()}.")
        party = self.get(party_id, current_user.company_id)  # 404s if missing/another company's
        self.party_repo.delete(party_id)
        return party

    def update_status(self, party_id: int, current_user: User, status: str) -> None:
        self.get(party_id, current_user.company_id)  # 404s if missing/another company's
        valid_statuses = {s for s, _ in CLIENT_STATUSES}
        if status not in valid_statuses:
            raise ValidationError("Invalid status.")
        self.party_repo.update_status(party_id, status)

    def add_contact(self, party_id: int, current_user: User, name: str, phone: str, email: str) -> ContactPerson:
        self.get(party_id, current_user.company_id)  # 404s if missing/another company's
        if not name or not name.strip():
            raise ValidationError("Contact person name is required.")
        return self.party_repo.contacts.add(party_id, ContactPerson(
            id=None, name=name.strip(), phone=phone or None, email=email or None, is_primary=False
        ))

    def set_primary_contact(self, party_id: int, current_user: User, contact_id: int) -> None:
        party = self.get(party_id, current_user.company_id)
        if not any(c.id == contact_id for c in party.contacts):
            raise ValidationError("That contact does not belong to this record.")
        self.party_repo.contacts.set_primary(party_id, contact_id)

    def add_communication(self, party_id: int, current_user: User, **comm_kwargs) -> Communication:
        self.get(party_id, current_user.company_id)  # 404s if missing/another company's
        return self.comm_service.add(self.parent_type, party_id, current_user.id, **comm_kwargs)

    def add_payment(self, party_id: int, current_user: User, account_name: str, payment_datetime: str,
                     amount_original: float, currency_code: str) -> PaymentEntry:
        self.get(party_id, current_user.company_id)
        if not account_name or not account_name.strip():
            raise ValidationError("Account name is required for a payment entry.")
        if amount_original is None or amount_original <= 0:
            raise ValidationError("Payment amount must be a positive number.")
        rate, amount_inr = self.currency_service.convert(amount_original, currency_code)
        payment = PaymentEntry(
            id=None, parent_type=self.parent_type, parent_id=party_id, account_name=account_name.strip(),
            payment_datetime=payment_datetime or datetime.now().strftime("%Y-%m-%d %H:%M"),
            amount_original=amount_original, currency_code=currency_code.upper(),
            conversion_rate=rate, amount_inr=amount_inr,
        )
        return self.payment_repo.add(payment)

    def add_document(self, party_id: int, current_user: User, document_name: str, document_type: str,
                      document_date: str, notes: str) -> DocumentEntry:
        self.get(party_id, current_user.company_id)
        if not document_name or not document_name.strip():
            raise ValidationError("Document name is required.")
        if not document_type or not document_type.strip():
            raise ValidationError("Document type is required.")
        doc = DocumentEntry(
            id=None, parent_type=self.parent_type, parent_id=party_id, document_name=document_name.strip(),
            document_type=document_type.strip(),
            document_date=document_date or date.today().isoformat(), notes=notes or None,
        )
        return self.document_repo.add(doc)

    def document_feed(self, party: Party) -> List[dict]:
        """One combined, date-sorted list for the 'Documents' card:
        manually recorded DocumentEntry rows plus every quotation/proforma
        invoice made against the party's originating lead (these aren't
        separate sections here - they're just auto-generated document types
        feeding the same card). Future auto-generated document types should
        feed into this the same way. `link` carries its own kwarg dict so
        each document type's route can name its id param however it likes."""
        rows = [
            {
                "name": d.document_name, "type": d.document_type, "date": d.document_date,
                "notes": d.notes, "link": None,
            }
            for d in self.document_repo.list_for(self.parent_type, party.id)
        ]
        for q in self.quotation_repo.list_for_lead(party.lead_id) if party.lead_id else []:
            rows.append({
                "name": q.quotation_number, "type": "Quotation", "date": q.quotation_date,
                "notes": f"{q.buyer_name} · $ {q.invoice_value_usd:,.2f}",
                "link": ("quotations.view_quotation", {"quotation_id": q.id}),
            })
        if self.proforma_invoice_repo:
            for pi in self.proforma_invoice_repo.list_for_lead(party.lead_id) if party.lead_id else []:
                rows.append({
                    "name": pi.invoice_number, "type": "Proforma Invoice", "date": pi.invoice_date,
                    "notes": f"{pi.consignee_name} · $ {pi.invoice_value_usd:,.2f}",
                    "link": ("proforma_invoices.view_proforma_invoice", {"proforma_invoice_id": pi.id}),
                })
        if self.purchase_order_repo:
            for po in self.purchase_order_repo.list_for_lead(party.lead_id) if party.lead_id else []:
                rows.append({
                    "name": po.po_number, "type": "Purchase Order", "date": po.po_date,
                    "notes": f"{po.seller_name} · ₹ {po.order_value_inr:,.2f}",
                    "link": ("purchase_orders.view_purchase_order", {"purchase_order_id": po.id}),
                })
        if self.packing_list_repo:
            for pl in self.packing_list_repo.list_for_lead(party.lead_id) if party.lead_id else []:
                rows.append({
                    "name": pl.packing_list_number, "type": "Packing List", "date": pl.packing_list_date,
                    "notes": f"{pl.total_quantity:,.2f} qty",
                    "link": ("packing_lists.view_packing_list", {"packing_list_id": pl.id}),
                })
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows


# ============================================================
# SUPPLIER SERVICE (its own profile shape - GSTIN/PAN/IEC/bank/contacts,
# modeled on CompanyService but per-supplier rather than per-tenant, since a
# company can have many suppliers. Document types for suppliers aren't
# defined yet, so payments/documents/communications reuse the same shared
# satellite tables as Buyer/Exporter, tagged parent_type='supplier'.)
# ============================================================
class SupplierService:
    def __init__(self, supplier_repo: SupplierRepositoryBase, lead_repo: LeadRepositoryBase,
                 comm_service: CommunicationService, payment_repo: PaymentRepository,
                 document_repo: DocumentRepository, currency_service: CurrencyService,
                 purchase_order_repo: Optional[PurchaseOrderRepository] = None):
        self.supplier_repo = supplier_repo
        self.lead_repo = lead_repo
        self.comm_service = comm_service
        self.payment_repo = payment_repo
        self.document_repo = document_repo
        self.currency_service = currency_service
        self.purchase_order_repo = purchase_order_repo

    # ---- lead -> supplier conversion (admin only) --------------------------------------------------
    def convert_lead(self, lead_id: int, admin_user: User) -> Supplier:
        if not admin_user.is_admin:
            raise PermissionDeniedError("Only an admin can approve a lead for conversion to supplier.")
        lead = self.lead_repo.get_by_id(lead_id)
        if not lead or lead.company_id != admin_user.company_id:
            raise NotFoundError(f"Lead #{lead_id} not found.")
        if lead.is_converted:
            raise ValidationError("This lead has already been converted.")

        supplier = Supplier(
            id=None, company_id=lead.company_id, lead_id=lead.id, company_name=lead.company_name,
            status="proforma_invoice_submission_pending", created_by=admin_user.id,
        )
        supplier = self.supplier_repo.convert_from_lead(supplier)
        # A Lead doesn't capture GSTIN/PAN/IEC/bank details - those are
        # filled in afterward on the supplier record. It does capture a
        # phone/email and contact persons, so seed those across in the same
        # shape our_company itself uses.
        details = []
        if lead.phone:
            details.append({"type": "phone", "value": lead.phone, "is_primary": True})
        if lead.email:
            details.append({"type": "email", "value": lead.email, "is_primary": True})
        if details:
            self.supplier_repo.replace_contact_details(supplier.id, details)
        if lead.contacts:
            primary = next((c for c in lead.contacts if c.is_primary), lead.contacts[0])
            self.supplier_repo.replace_contact_persons(supplier.id, [{"name": primary.name, "is_primary": True}])
        return self.get(supplier.id, admin_user.company_id)

    # ---- reads --------------------------------------------------
    def get(self, supplier_id: int, company_id: int) -> Supplier:
        supplier = self.supplier_repo.get_by_id(supplier_id)
        if not supplier or supplier.company_id != company_id:
            # 404, not 403 - don't reveal that another company's record exists.
            raise NotFoundError(f"Supplier #{supplier_id} not found.")
        return supplier

    def list_all(self, company_id: int, status: Optional[str] = None) -> List[Supplier]:
        return self.supplier_repo.list_all(company_id, status)

    # ---- add directly (admin only, no originating lead) --------------------------------------------------
    def create(self, current_user: User, company_name: str, address: str, gstin: str, cin_llp_no: str, pan_no: str,
               iec: str, contact_details: list, contact_persons: list, bank_details: list) -> Supplier:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can add a new supplier.")
        if not company_name or not company_name.strip():
            raise ValidationError("Company name is compulsory.")
        valid_details, valid_persons = self._validate_profile_rows(contact_details, contact_persons, bank_details)

        supplier = Supplier(
            id=None, company_id=current_user.company_id, lead_id=None, company_name=company_name.strip(),
            status="proforma_invoice_submission_pending", created_by=current_user.id,
            address=(address or "").strip() or None, gstin=gstin or None,
            cin_llp_no=(cin_llp_no or "").strip() or None, pan_no=pan_no or None, iec=iec or None,
        )
        supplier = self.supplier_repo.create(supplier)
        self.supplier_repo.replace_contact_details(supplier.id, valid_details)
        self.supplier_repo.replace_contact_persons(supplier.id, valid_persons)
        self.supplier_repo.replace_bank_details(supplier.id, bank_details)
        return self.get(supplier.id, current_user.company_id)

    @staticmethod
    def _validate_profile_rows(contact_details: list, contact_persons: list, bank_details: list) -> tuple:
        """Shared by create/update_profile: every contact detail row needs a
        type once it has a value, and every bank detail row is all-or-
        nothing once any of its fields is filled in. Returns
        (valid_details, valid_persons) - bank_details doesn't need
        filtering, just validating in place."""
        valid_details = [d for d in contact_details if d.get("value", "").strip()]
        for d in valid_details:
            if not d.get("type", "").strip():
                raise ValidationError("Every contact detail row needs a type.")
        valid_persons = [p for p in contact_persons if p.get("name", "").strip()]

        bank_fields = ["bank_name", "account_number", "ifsc_code", "swift_code", "branch", "bank_address"]
        bank_labels = {
            "bank_name": "bank name", "account_number": "account number", "ifsc_code": "IFSC code",
            "swift_code": "SWIFT code", "branch": "branch", "bank_address": "bank address",
        }
        for b in bank_details:
            missing = [bank_labels[f] for f in bank_fields if not b.get(f, "").strip()]
            if missing:
                raise ValidationError(f"Bank detail '{b.get('bank_name') or '(unnamed)'}' is missing: {', '.join(missing)}.")

        return valid_details, valid_persons

    # ---- writes --------------------------------------------------
    def update_profile(self, supplier_id: int, current_user: User, company_name: str, address: str,
                        gstin: str, cin_llp_no: str, pan_no: str, iec: str, contact_details: list,
                        contact_persons: list, bank_details: list) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can edit a supplier's profile.")
        self.get(supplier_id, current_user.company_id)  # 404s if missing/another company's
        if not company_name or not company_name.strip():
            raise ValidationError("Company name is compulsory.")
        valid_details, valid_persons = self._validate_profile_rows(contact_details, contact_persons, bank_details)

        self.supplier_repo.update_profile(supplier_id, {
            "company_name": company_name.strip(), "address": address or None,
            "gstin": gstin or None, "cin_llp_no": (cin_llp_no or "").strip() or None,
            "pan_no": pan_no or None, "iec": iec or None,
        })
        self.supplier_repo.replace_contact_details(supplier_id, valid_details)
        self.supplier_repo.replace_contact_persons(supplier_id, valid_persons)
        self.supplier_repo.replace_bank_details(supplier_id, bank_details)

    def update_status(self, supplier_id: int, current_user: User, status: str) -> None:
        self.get(supplier_id, current_user.company_id)  # 404s if missing/another company's
        valid_statuses = {s for s, _ in CLIENT_STATUSES}
        if status not in valid_statuses:
            raise ValidationError("Invalid status.")
        self.supplier_repo.update_status(supplier_id, status)

    def add_communication(self, supplier_id: int, current_user: User, **comm_kwargs) -> Communication:
        self.get(supplier_id, current_user.company_id)  # 404s if missing/another company's
        return self.comm_service.add("supplier", supplier_id, current_user.id, **comm_kwargs)

    def add_payment(self, supplier_id: int, current_user: User, account_name: str, payment_datetime: str,
                     amount_original: float, currency_code: str) -> PaymentEntry:
        self.get(supplier_id, current_user.company_id)
        if not account_name or not account_name.strip():
            raise ValidationError("Account name is required for a payment entry.")
        if amount_original is None or amount_original <= 0:
            raise ValidationError("Payment amount must be a positive number.")
        rate, amount_inr = self.currency_service.convert(amount_original, currency_code)
        payment = PaymentEntry(
            id=None, parent_type="supplier", parent_id=supplier_id, account_name=account_name.strip(),
            payment_datetime=payment_datetime or datetime.now().strftime("%Y-%m-%d %H:%M"),
            amount_original=amount_original, currency_code=currency_code.upper(),
            conversion_rate=rate, amount_inr=amount_inr,
        )
        return self.payment_repo.add(payment)

    def add_document(self, supplier_id: int, current_user: User, document_name: str, document_type: str,
                      document_date: str, notes: str) -> DocumentEntry:
        self.get(supplier_id, current_user.company_id)
        if not document_name or not document_name.strip():
            raise ValidationError("Document name is required.")
        if not document_type or not document_type.strip():
            raise ValidationError("Document type is required.")
        doc = DocumentEntry(
            id=None, parent_type="supplier", parent_id=supplier_id, document_name=document_name.strip(),
            document_type=document_type.strip(),
            document_date=document_date or date.today().isoformat(), notes=notes or None,
        )
        return self.document_repo.add(doc)

    def document_feed(self, supplier: Supplier) -> List[dict]:
        """Manually recorded documents plus every Purchase Order where this
        supplier was picked as the seller - a Supplier's natural link to POs
        is seller_supplier_id, not an originating lead (unlike Buyer/
        Exporter, whose auto-generated documents are found via lead_id)."""
        rows = [
            {
                "name": d.document_name, "type": d.document_type, "date": d.document_date,
                "notes": d.notes, "link": None,
            }
            for d in self.document_repo.list_for("supplier", supplier.id)
        ]
        if self.purchase_order_repo:
            for po in self.purchase_order_repo.list_for_seller(supplier.id):
                rows.append({
                    "name": po.po_number, "type": "Purchase Order", "date": po.po_date,
                    "notes": f"{po.seller_name} · ₹ {po.order_value_inr:,.2f}",
                    "link": ("purchase_orders.view_purchase_order", {"purchase_order_id": po.id}),
                })
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows


# ============================================================
# TRANSPORTER SERVICE
# ============================================================
class TransporterService:
    """The haulier directory. The one party type with no lead behind it and
    no status pipeline (see models.Transporter), so this service is a plain
    company-scoped CRUD: reads for anyone signed in, writes admin-only, in
    line with how Buyer/Supplier/Exporter gate their own edits."""

    def __init__(self, transporter_repo: TransporterRepositoryBase):
        self.transporter_repo = transporter_repo

    # ---- reads --------------------------------------------------
    def get(self, transporter_id: int, company_id: int) -> Transporter:
        transporter = self.transporter_repo.get_by_id(transporter_id)
        if not transporter or transporter.company_id != company_id:
            # 404, not 403 - don't reveal that another company's record exists.
            raise NotFoundError(f"Transporter #{transporter_id} not found.")
        return transporter

    def list_all(self, company_id: int) -> List[Transporter]:
        return self.transporter_repo.list_all(company_id)

    # ---- validation --------------------------------------------------
    @staticmethod
    def _clean_contacts(contacts: list) -> list:
        """Drops the blank rows the repeatable form always submits, and makes
        sure exactly one of the survivors is primary (the first one, if the
        form marked none)."""
        valid = [c for c in contacts if (c.get("name") or "").strip()]
        for c in valid:
            c["name"] = c["name"].strip()
        if valid and not any(c.get("is_primary") for c in valid):
            valid[0]["is_primary"] = True
        return valid

    @staticmethod
    def _clean_fields(fields: dict) -> dict:
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValidationError("Transporter name is compulsory.")
        return {
            "name": name,
            "address": (fields.get("address") or "").strip() or None,
            "gstin_transporter_no": (fields.get("gstin_transporter_no") or "").strip() or None,
            "pan_no": (fields.get("pan_no") or "").strip() or None,
            "cin_llp_no": (fields.get("cin_llp_no") or "").strip() or None,
            "email": (fields.get("email") or "").strip() or None,
        }

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, contacts: list) -> Transporter:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can add a transporter.")
        clean = self._clean_fields(fields)
        transporter = self.transporter_repo.create(Transporter(
            id=None, company_id=current_user.company_id, created_by=current_user.id, **clean,
        ))
        self.transporter_repo.replace_contacts(transporter.id, self._clean_contacts(contacts))
        return self.get(transporter.id, current_user.company_id)

    def update(self, transporter_id: int, current_user: User, fields: dict, contacts: list) -> Transporter:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can edit a transporter.")
        self.get(transporter_id, current_user.company_id)  # 404s if missing/another company's
        self.transporter_repo.update(transporter_id, self._clean_fields(fields))
        self.transporter_repo.replace_contacts(transporter_id, self._clean_contacts(contacts))
        return self.get(transporter_id, current_user.company_id)

    def delete(self, transporter_id: int, current_user: User) -> Transporter:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can delete a transporter.")
        transporter = self.get(transporter_id, current_user.company_id)
        self.transporter_repo.delete(transporter_id)
        return transporter


def advance_client_status(party_repos: dict, lead_repo: LeadRepositoryBase,
                           lead_id: Optional[int], document_type: str) -> None:
    """Moves the buyer/supplier/exporter tied to `lead_id` forward to
    whatever CLIENT_STATUSES stage becomes pending once `document_type` has
    just been generated - e.g. generating a Proforma Invoice clears
    "proforma invoice submission pending" and lands on "purchase order
    submission pending". Every document service calls this same helper
    after create/update; adding a new document type only means registering
    it in models.CLIENT_STATUS_ADVANCE_ON, no other wiring needed.
    `party_repos` maps 'Buyer'/'Supplier'/'Exporter' -> that type's repo, so
    the right table can be looked up once `lead.converted_client_type` is
    known. No-op for document types that don't map to a stage (e.g. Packing
    List), leads that haven't converted yet, or when the record is already
    at or past the target stage (regenerating/editing a document shouldn't
    walk the status backwards)."""
    target_status = CLIENT_STATUS_ADVANCE_ON.get(document_type)
    if not target_status or not lead_id:
        return
    lead = lead_repo.get_by_id(lead_id)
    if not lead or not lead.is_converted or not lead.converted_client_id or not lead.converted_client_type:
        return
    repo = party_repos.get(lead.converted_client_type)
    if not repo:
        return
    record = repo.get_by_id(lead.converted_client_id)
    if not record:
        return
    order = [key for key, _ in CLIENT_STATUSES]
    try:
        if order.index(target_status) <= order.index(record.status):
            return
    except ValueError:
        pass  # current status isn't a recognized stage - advance anyway
    repo.update_status(record.id, target_status)


# ============================================================
# COMPANY SERVICE (our own company profile - admin only)
# ============================================================
class CompanyService:
    def __init__(self, company_repo: CompanyRepository, upload_folder: str = "", allowed_extensions: set = frozenset()):
        self.company_repo = company_repo
        # Logo images are stored in the same static uploads folder as product
        # photos - deliberately, so the Database Backup ZIP (which bundles
        # that folder) carries the logo through backup/restore too.
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions

    def get(self, company_id: int):
        return self.company_repo.get(company_id)

    def save(self, current_user: User, company_name: str, address: str, gstin: str, pan_no: str, iec: str,
              bin_no: str, contact_details: list, contact_persons: list, bank_details: list, lut_details: list,
              rcmc_details: list, logo_file=None, remove_logo: bool = False,
              self_sealing_declaration: str = "", branch_code: str = "", government_schemes: str = "") -> None:
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

        for r in rcmc_details:
            if not r.get("registration_number", "").strip() or not r.get("registration_date", "").strip() or not r.get("valid_until", "").strip():
                raise ValidationError("Every RCMC row needs a registration number, registration date, and valid-until date.")

        existing = self.company_repo.get(current_user.company_id)
        # Blank submissions keep the previously stored value instead of clearing it -
        # these are long-lived defaults typed once and reused, not fields meant to be erased by an empty save.
        our_company_id = self.company_repo.upsert(
            current_user.company_id, company_name.strip(), address, gstin, pan_no, iec, bin_no,
            (self_sealing_declaration or "").strip() or (existing.self_sealing_declaration if existing else None),
            (branch_code or "").strip() or (existing.branch_code if existing else None),
            (government_schemes or "").strip() or (existing.government_schemes if existing else None),
        )
        self.company_repo.replace_contact_details(our_company_id, valid_details)
        self.company_repo.replace_contact_persons(our_company_id, valid_persons)
        self.company_repo.replace_bank_details(our_company_id, valid_banks)
        self.company_repo.replace_lut_details(our_company_id, lut_details)
        self.company_repo.replace_rcmc_details(our_company_id, rcmc_details)

        old_logo = existing.logo_path if existing else None
        if logo_file is not None and getattr(logo_file, "filename", ""):
            new_logo = self._save_logo(logo_file)
            self.company_repo.set_logo(our_company_id, new_logo)
            self._delete_logo_file(old_logo)
        elif remove_logo and old_logo:
            self.company_repo.set_logo(our_company_id, None)
            self._delete_logo_file(old_logo)

    # ---- logo storage (same folder as product images, so backups cover it) --------------------------------------------------
    def _save_logo(self, file_storage) -> str:
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in self.allowed_extensions:
            raise ValidationError(
                f"Unsupported logo image type '.{ext}'. Allowed: {', '.join(sorted(self.allowed_extensions))}."
            )
        os.makedirs(self.upload_folder, exist_ok=True)
        stored_name = f"logo_{uuid.uuid4().hex}_{filename}"
        file_storage.save(os.path.join(self.upload_folder, stored_name))
        return f"uploads/products/{stored_name}"

    def _delete_logo_file(self, relative_path: Optional[str]) -> None:
        if not relative_path:
            return
        full_path = os.path.join(self.upload_folder, os.path.basename(relative_path))
        if os.path.exists(full_path):
            os.remove(full_path)


# ============================================================
# PERMIT SERVICE (the "Permissions" tab under Our Company)
# ============================================================
class PermitService:
    """The permits ("permissions") a company holds. Each records a
    stuffing-place name + place of stuffing and the issuing-authority
    details, is either valid until an expiry date OR a one-time permit, and
    can carry an uploaded PDF (same save/delete pattern as
    PurchaseInvoiceService). Admin-only, like the rest of the Our Company
    area - the route enforces that; the service still keeps everything
    company-scoped."""

    VALIDITY_TYPES = ("expiry", "one_time")

    def __init__(self, permit_repo: PermitRepository,
                 upload_folder: str = "", allowed_extensions: set = frozenset()):
        self.permit_repo = permit_repo
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions

    # ---- reads --------------------------------------------------
    def get(self, permit_id: int, company_id: int) -> Permit:
        permit = self.permit_repo.get_by_id(permit_id)
        if not permit or permit.company_id != company_id:
            # 404, not 403 - don't reveal that another company's permit exists.
            raise NotFoundError(f"Permit #{permit_id} not found.")
        return permit

    def list_all(self, company_id: int) -> List[Permit]:
        return self.permit_repo.list_all(company_id)

    # ---- PDF storage (mirrors PurchaseInvoiceService._save_pdf) --------------------------------------------------
    def _save_pdf(self, file_storage) -> Optional[str]:
        if not file_storage or not file_storage.filename:
            return None
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in self.allowed_extensions:
            raise ValidationError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(self.allowed_extensions))}.")
        os.makedirs(self.upload_folder, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{filename}"
        file_storage.save(os.path.join(self.upload_folder, stored_name))
        return f"uploads/permits/{stored_name}"

    def _delete_pdf_file(self, relative_path: Optional[str]) -> None:
        if not relative_path:
            return
        full_path = os.path.join(self.upload_folder, os.path.basename(relative_path))
        if os.path.exists(full_path):
            os.remove(full_path)

    # ---- validation --------------------------------------------------
    def _build(self, current_user: User, fields: dict) -> Permit:
        permission_number = (fields.get("permission_number") or "").strip()
        if not permission_number:
            raise ValidationError("Permission number is compulsory.")

        validity_type = (fields.get("validity_type") or "expiry").strip()
        if validity_type not in self.VALIDITY_TYPES:
            validity_type = "expiry"
        date_of_expiry = (fields.get("date_of_expiry") or "").strip() or None
        if validity_type == "one_time":
            date_of_expiry = None
        elif not date_of_expiry:
            raise ValidationError("Date of expiry is compulsory unless the permit is one-time.")

        return Permit(
            id=None, company_id=current_user.company_id,
            permission_number=permission_number, created_by=current_user.id,
            stuffing_place_name=(fields.get("stuffing_place_name") or "").strip() or None,
            place_of_stuffing=(fields.get("place_of_stuffing") or "").strip() or None,
            date_of_issue=(fields.get("date_of_issue") or "").strip() or None,
            issuing_authority=(fields.get("issuing_authority") or "").strip() or None,
            issuing_authority_address=(fields.get("issuing_authority_address") or "").strip() or None,
            validity_type=validity_type, date_of_expiry=date_of_expiry,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, pdf_file=None) -> Permit:
        permit = self._build(current_user, fields)
        permit.pdf_path = self._save_pdf(pdf_file)
        return self.permit_repo.create(permit)

    def update(self, current_user: User, permit_id: int, fields: dict,
               pdf_file=None, remove_pdf: bool = False) -> Permit:
        existing = self.get(permit_id, current_user.company_id)
        permit = self._build(current_user, fields)
        if pdf_file and pdf_file.filename:
            permit.pdf_path = self._save_pdf(pdf_file)
            self._delete_pdf_file(existing.pdf_path)
        elif remove_pdf:
            self._delete_pdf_file(existing.pdf_path)
            permit.pdf_path = None
        else:
            permit.pdf_path = existing.pdf_path
        self.permit_repo.update(permit_id, permit)
        return self.get(permit_id, current_user.company_id)

    def delete(self, current_user: User, permit_id: int) -> None:
        existing = self.get(permit_id, current_user.company_id)
        self._delete_pdf_file(existing.pdf_path)
        self.permit_repo.delete(permit_id)


# ============================================================
# STATS SERVICE (powers the admin dashboard)
# ============================================================
class StatsService:
    def __init__(self, user_repo: UserRepositoryBase, lead_repo: LeadRepositoryBase,
                 comm_repo: CommunicationRepository, buyer_repo: PartyRepositoryBase,
                 exporter_repo: PartyRepositoryBase, supplier_repo: SupplierRepositoryBase):
        self.user_repo = user_repo
        self.lead_repo = lead_repo
        self.comm_repo = comm_repo
        self.buyer_repo = buyer_repo
        self.exporter_repo = exporter_repo
        self.supplier_repo = supplier_repo

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
        # "Clients" on the dashboard now spans all three separate entities -
        # a buyer, a supplier and an exporter each still count as one client.
        all_clients = (
            self.buyer_repo.list_all(company_id)
            + self.exporter_repo.list_all(company_id)
            + self.supplier_repo.list_all(company_id)
        )
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
                   (SELECT
                        (SELECT COUNT(*) FROM buyers b WHERE b.lead_id IN (SELECT id FROM leads WHERE created_by = u.id)
                           AND b.company_id = ? AND date(b.created_at) BETWEEN date(?) AND date(?)) +
                        (SELECT COUNT(*) FROM exporters e WHERE e.lead_id IN (SELECT id FROM leads WHERE created_by = u.id)
                           AND e.company_id = ? AND date(e.created_at) BETWEEN date(?) AND date(?)) +
                        (SELECT COUNT(*) FROM suppliers s WHERE s.lead_id IN (SELECT id FROM leads WHERE created_by = u.id)
                           AND s.company_id = ? AND date(s.created_at) BETWEEN date(?) AND date(?))
                   ) AS clients_converted
            FROM users u
            WHERE u.role = 'employee' AND u.company_id = ?
            ORDER BY u.full_name
            """,
            (company_id, start_date, end_date, start_date, end_date,
             company_id, start_date, end_date, company_id, start_date, end_date,
             company_id, start_date, end_date, company_id),
        )
        return [dict(r) for r in rows]

    def payments_received_total(self, company_id: int, start_date: str, end_date: str) -> dict:
        row = self.db.query_one(
            """SELECT COUNT(*) AS payment_count, COALESCE(SUM(ph.amount_inr), 0) AS total_inr
               FROM payment_history ph
               WHERE date(ph.payment_datetime) BETWEEN date(?) AND date(?)
                 AND (
                   (ph.parent_type = 'buyer' AND ph.parent_id IN (SELECT id FROM buyers WHERE company_id = ?))
                   OR (ph.parent_type = 'exporter' AND ph.parent_id IN (SELECT id FROM exporters WHERE company_id = ?))
                   OR (ph.parent_type = 'supplier' AND ph.parent_id IN (SELECT id FROM suppliers WHERE company_id = ?))
                 )""",
            (start_date, end_date, company_id, company_id, company_id),
        )
        return dict(row) if row else {"payment_count": 0, "total_inr": 0}


# ============================================================
# PRODUCT SERVICE (three-level catalog: products carry the tax/HSN
# identity, folders nest to any depth inside one product, designs are the
# sellable leaves with price/packing/photos)
# ============================================================
def _leading_number(text) -> float:
    """The number a free-text packing figure starts with ('31 boxes' ->
    31.0), 0.0 when there isn't one - shared by the per-box auto-calc
    factors and the pallet types' derived alternate-quantity figure."""
    m = re.match(r"\s*([\d.]+)", str(text or ""))
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


def pallet_alt_quantity(pallet_type: ProductPalletType, product: Optional[Product]) -> float:
    """The alternate quantity one pallet of this type holds - always derived
    (boxes on the pallet x the product's per-box alternate quantity), never
    stored, so it can't drift when the product spec changes. 0.0 when the
    product has no usable alternate-quantity figure."""
    per_box = _leading_number(product.alternate_quantity) if product else 0.0
    return round(pallet_type.boxes_per_pallet * per_box, 2) if per_box else 0.0


class ProductService:
    # The unstored palleting option every product offers: goods sold loose,
    # no pallets at all. Reserved so a stored pallet type can't shadow it.
    LOOSE_NAME = "loose"

    def __init__(self, category_repo: CategoryRepository, product_repo: ProductRepository,
                 folder_repo: ProductFolderRepository, design_repo: DesignRepository,
                 pallet_type_repo: ProductPalletTypeRepository,
                 upload_folder: str, allowed_extensions: set):
        self.category_repo = category_repo
        self.product_repo = product_repo
        self.folder_repo = folder_repo
        self.design_repo = design_repo
        self.pallet_type_repo = pallet_type_repo
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions

    # ---- categories (nestable folders at the catalog root) -------------------
    def list_categories(self, company_id: int) -> List[Category]:
        """Every category, flat - powers the product form's category picker."""
        return self.category_repo.list_all(company_id)

    def list_categories_tree(self, company_id: int) -> List[tuple]:
        """Every category as (category, depth) pairs, ordered depth-first
        (each category immediately followed by its own subtree) - lets the
        product form's category <select> show nesting via indentation
        without needing a recursive template."""
        all_categories = self.category_repo.list_all(company_id)
        children_by_parent = {}
        for category in all_categories:
            children_by_parent.setdefault(category.parent_id, []).append(category)

        ordered = []

        def visit(parent_id, depth):
            for category in children_by_parent.get(parent_id, []):
                ordered.append((category, depth))
                visit(category.id, depth + 1)

        visit(None, 0)
        return ordered

    def get_category(self, category_id: int, company_id: int) -> Category:
        category = self.category_repo.get_by_id(category_id)
        if not category or category.company_id != company_id:
            raise NotFoundError(f"Category #{category_id} not found.")
        return category

    def category_breadcrumb(self, company_id: int, category_id: Optional[int]) -> List[Category]:
        if not category_id:
            return []
        self.get_category(category_id, company_id)  # 404s if missing/another company's before walking up
        return self.category_repo.list_ancestors(category_id)

    def create_category(self, current_user: User, name: str, parent_id=None) -> Category:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Category name is compulsory.")
        parent_id = self._parse_category_id(current_user.company_id, parent_id)
        return self.category_repo.create(current_user.company_id, name.strip(), parent_id)

    def rename_category(self, current_user: User, category_id: int, name: str) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Category name is compulsory.")
        self.get_category(category_id, current_user.company_id)
        self.category_repo.update(category_id, {"name": name.strip()})

    def delete_category(self, current_user: User, category_id: int) -> None:
        """Deletes the category, every subcategory nested under it, and every
        product inside any of them - like deleting a folder tree. Each
        product delete also cleans up its designs' image files and nulls out
        document line references."""
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        self.get_category(category_id, current_user.company_id)
        for descendant_id in self.category_repo.list_descendant_ids(category_id):
            for product in self.product_repo.list_in_category(current_user.company_id, descendant_id):
                self.delete_product(current_user, product.id)
        self.category_repo.delete(category_id)  # cascades to subcategories in the DB

    # ---- products --------------------------------------------------
    def list_products(self, company_id: int) -> List[Product]:
        return self.product_repo.list_all(company_id)

    def list_catalog(self, company_id: int, category_id: Optional[int]):
        """Returns (subcategories, products) for one level of the catalog
        root browser - category_id=None is the catalog root."""
        if category_id is not None:
            self.get_category(category_id, company_id)  # 404s if missing/another company's
        return (self.category_repo.list_children(company_id, category_id),
                self.product_repo.list_in_category(company_id, category_id))

    def search_catalog(self, company_id: int, query: str) -> dict:
        """Products and designs matching `query` by name, flattened across
        every category/sub category - used by the search bar on the
        Products and Inventory catalog roots instead of folder navigation.
        Blank query returns nothing (the caller falls back to the normal
        browse view)."""
        query = (query or "").strip()
        if not query:
            return {"products": [], "designs": []}
        return {
            "products": self.product_repo.search(company_id, query),
            "designs": self.design_repo.search(company_id, query),
        }

    def get_product(self, product_id: int, company_id: int) -> Product:
        product = self.product_repo.get_by_id(product_id)
        if not product or product.company_id != company_id:
            raise NotFoundError(f"Product #{product_id} not found.")
        return product

    def _parse_category_id(self, company_id: int, category_id) -> Optional[int]:
        """Shared by product.category_id and category.parent_id - both are
        optional references to a category that must belong to this company."""
        if category_id in (None, "", "None"):
            return None
        self.get_category(int(category_id), company_id)  # validates ownership
        return int(category_id)

    def _tax_fields(self, igst_percent: str) -> dict:
        """IGST is the only tax input; SGST and CGST are each half of it."""
        igst = self._parse_percent("IGST", igst_percent)
        half = round(igst / 2, 2) if igst is not None else None
        return {"igst_percent": igst, "sgst_percent": half, "cgst_percent": half}

    def _parse_pallet_types(self, pallet_types: Optional[list]) -> List[ProductPalletType]:
        """Validates the raw name/boxes pairs submitted by the product form
        into ProductPalletType rows. Rows left entirely blank are skipped;
        a row with only one half filled in is an error. 'loose' is reserved
        for the built-in no-pallet option every product already has."""
        parsed = []
        for i, raw in enumerate(pallet_types or [], start=1):
            name = (raw.get("name") or "").strip()
            boxes_raw = (raw.get("boxes_per_pallet") or "").strip()
            if not name and not boxes_raw:
                continue
            if not name:
                raise ValidationError(f"Pallet type {i}: a name is compulsory.")
            if name.lower() == self.LOOSE_NAME:
                raise ValidationError(
                    f"Pallet type {i}: '{self.LOOSE_NAME}' is reserved - every product "
                    "already offers it as the built-in no-pallet option."
                )
            try:
                boxes = float(boxes_raw)
            except ValueError:
                raise ValidationError(f"Pallet type '{name}': boxes per pallet must be a number.")
            if boxes <= 0:
                raise ValidationError(f"Pallet type '{name}': boxes per pallet must be greater than zero.")
            parsed.append(ProductPalletType(
                id=None, company_id=0, product_id=0, name=name, boxes_per_pallet=boxes,
            ))
        return parsed

    def pallet_types_for_product(self, product_id: int) -> List[ProductPalletType]:
        return self.pallet_type_repo.list_for_product(product_id)

    def pallet_types_by_product(self, company_id: int) -> dict:
        """product_id -> [ProductPalletType, ...] for the whole company in
        one query - what the JSON product list and the document forms use."""
        grouped = {}
        for pt in self.pallet_type_repo.list_all(company_id):
            grouped.setdefault(pt.product_id, []).append(pt)
        return grouped

    def create_product(self, current_user: User, product_name: str, description: str,
                        hsn_code: str, igst_percent: str, quantity: str,
                        alternate_quantity: str, quantity_unit: str = "",
                        alternate_quantity_unit: str = "",
                        net_weight_kg: str = "", gross_weight_kg: str = "",
                        pallet_types: Optional[list] = None,
                        category_id=None) -> Product:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not product_name or not product_name.strip():
            raise ValidationError("Product name is compulsory.")
        parsed_pallet_types = self._parse_pallet_types(pallet_types)
        product = Product(
            id=None, company_id=current_user.company_id, product_name=product_name.strip(),
            category_id=self._parse_category_id(current_user.company_id, category_id),
            description=description or None, hsn_code=hsn_code or None,
            quantity_unit=self._parse_unit(quantity_unit, default="PCS"),
            quantity=quantity or None,
            alternate_quantity_unit=self._parse_unit(alternate_quantity_unit, default="SQM"),
            alternate_quantity=alternate_quantity or None,
            net_weight_kg=self._parse_weight("Net weight", net_weight_kg),
            gross_weight_kg=self._parse_weight("Gross weight", gross_weight_kg),
            **self._tax_fields(igst_percent),
        )
        product = self.product_repo.create(product)
        if parsed_pallet_types:
            self.pallet_type_repo.replace_for_product(current_user.company_id, product.id, parsed_pallet_types)
        return product

    def update_product(self, current_user: User, product_id: int, product_name: str,
                        description: str, hsn_code: str, igst_percent: str,
                        quantity: str, alternate_quantity: str,
                        quantity_unit: str = "", alternate_quantity_unit: str = "",
                        net_weight_kg: str = "", gross_weight_kg: str = "",
                        pallet_types: Optional[list] = None,
                        category_id=None) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not product_name or not product_name.strip():
            raise ValidationError("Product name is compulsory.")
        self.get_product(product_id, current_user.company_id)
        parsed_pallet_types = self._parse_pallet_types(pallet_types)
        self.product_repo.update(product_id, {
            "product_name": product_name.strip(), "description": description or None,
            "hsn_code": hsn_code or None,
            "category_id": self._parse_category_id(current_user.company_id, category_id),
            "quantity_unit": self._parse_unit(quantity_unit, default="PCS"),
            "quantity": quantity or None,
            "alternate_quantity_unit": self._parse_unit(alternate_quantity_unit, default="SQM"),
            "alternate_quantity": alternate_quantity or None,
            "net_weight_kg": self._parse_weight("Net weight", net_weight_kg),
            "gross_weight_kg": self._parse_weight("Gross weight", gross_weight_kg),
            **self._tax_fields(igst_percent),
        })
        self.pallet_type_repo.replace_for_product(current_user.company_id, product_id, parsed_pallet_types)

    def delete_product(self, current_user: User, product_id: int) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        self.get_product(product_id, current_user.company_id)
        # Design image files live on disk, not in the DB, so the CASCADE
        # delete doesn't clean them up on its own.
        for design in self.design_repo.list_for_product(product_id):
            self._delete_image_file(design.photo_path)
            self._delete_image_file(design.dimension_photo_path)
        self.product_repo.delete(product_id)  # cascades to folders/designs in the DB

    # ---- browsing inside a product --------------------------------------------------
    def get_folder(self, folder_id: int, company_id: int) -> ProductFolder:
        folder = self.folder_repo.get_by_id(folder_id)
        if not folder or folder.company_id != company_id:
            raise NotFoundError(f"Folder #{folder_id} not found.")
        return folder

    def breadcrumb(self, company_id: int, folder_id: Optional[int]) -> List[ProductFolder]:
        if not folder_id:
            return []
        self.get_folder(folder_id, company_id)  # 404s if missing/another company's before walking up
        return self.folder_repo.list_ancestors(folder_id)

    def list_contents(self, company_id: int, product_id: int, folder_id: Optional[int]):
        """Returns (subfolders, designs) for one level inside a product -
        folder_id=None is the product's top level."""
        self.get_product(product_id, company_id)  # 404s if missing/another company's
        if folder_id is not None:
            folder = self.get_folder(folder_id, company_id)
            if folder.product_id != product_id:
                raise NotFoundError(f"Folder #{folder_id} not found.")
        return (self.folder_repo.list_children(product_id, folder_id),
                self.design_repo.list_in(product_id, folder_id))

    # ---- folders --------------------------------------------------
    def create_folder(self, current_user: User, product_id: int, name: str,
                       parent_id: Optional[int]) -> ProductFolder:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Folder name is compulsory.")
        self.get_product(product_id, current_user.company_id)
        if parent_id is not None:
            parent = self.get_folder(parent_id, current_user.company_id)
            if parent.product_id != product_id:
                raise ValidationError("The parent folder belongs to a different product.")
        return self.folder_repo.create(current_user.company_id, product_id, name.strip(), parent_id)

    def rename_folder(self, current_user: User, folder_id: int, name: str) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not name or not name.strip():
            raise ValidationError("Folder name is compulsory.")
        self.get_folder(folder_id, current_user.company_id)
        self.folder_repo.update(folder_id, name.strip())

    def delete_folder(self, current_user: User, folder_id: int) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        folder = self.get_folder(folder_id, current_user.company_id)
        self._delete_folder_images_recursive(folder.product_id, folder_id)
        self.folder_repo.delete(folder_id)  # cascades to subfolders/designs in the DB

    def _delete_folder_images_recursive(self, product_id: int, folder_id: int) -> None:
        """Design image files live on disk, not in the DB, so cascading
        deletes don't clean them up on their own - walk the subtree first."""
        for design in self.design_repo.list_in(product_id, folder_id):
            self._delete_image_file(design.photo_path)
            self._delete_image_file(design.dimension_photo_path)
        for subfolder in self.folder_repo.list_children(product_id, folder_id):
            self._delete_folder_images_recursive(product_id, subfolder.id)

    # ---- designs --------------------------------------------------
    def get_design(self, design_id: int, company_id: int) -> Design:
        design = self.design_repo.get_by_id(design_id)
        if not design or design.company_id != company_id:
            raise NotFoundError(f"Design #{design_id} not found.")
        return design

    def list_designs_for_product(self, product_id: int, company_id: int) -> List[Design]:
        self.get_product(product_id, company_id)
        return self.design_repo.list_for_product(product_id)

    def create_design(self, current_user: User, product_id: int, folder_id: Optional[int],
                       design_name: str, description: str, price_usd: str,
                       alt_text: str, photo_file, dimension_photo_file,
                       surface: str = "") -> Design:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not design_name or not design_name.strip():
            raise ValidationError("Design name is compulsory.")
        self.get_product(product_id, current_user.company_id)
        if folder_id is not None:
            folder = self.get_folder(folder_id, current_user.company_id)
            if folder.product_id != product_id:
                raise ValidationError("That folder belongs to a different product.")

        photo_path = self._save_image(photo_file)
        dimension_photo_path = self._save_image(dimension_photo_file)
        design = Design(
            id=None, company_id=current_user.company_id, product_id=product_id, folder_id=folder_id,
            design_name=design_name.strip(), description=description or None,
            surface=(surface or "").strip() or None,
            price_usd=self._parse_price(price_usd),
            photo_path=photo_path, dimension_photo_path=dimension_photo_path, alt_text=alt_text or None,
        )
        return self.design_repo.create(design)

    def update_design(self, current_user: User, design_id: int, design_name: str,
                       description: str, price_usd: str, alt_text: str,
                       photo_file, dimension_photo_file, surface: str = "") -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not design_name or not design_name.strip():
            raise ValidationError("Design name is compulsory.")
        existing = self.get_design(design_id, current_user.company_id)

        fields = {
            "design_name": design_name.strip(), "description": description or None,
            "surface": (surface or "").strip() or None,
            "price_usd": self._parse_price(price_usd), "alt_text": alt_text or None,
        }
        if photo_file and photo_file.filename:
            fields["photo_path"] = self._save_image(photo_file)
            self._delete_image_file(existing.photo_path)
        if dimension_photo_file and dimension_photo_file.filename:
            fields["dimension_photo_path"] = self._save_image(dimension_photo_file)
            self._delete_image_file(existing.dimension_photo_path)

        self.design_repo.update(design_id, fields)

    def delete_design(self, current_user: User, design_id: int) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        design = self.get_design(design_id, current_user.company_id)
        self._delete_image_file(design.photo_path)
        self._delete_image_file(design.dimension_photo_path)
        self.design_repo.delete(design_id)

    @staticmethod
    def _parse_price(price_usd: str) -> Optional[float]:
        if not price_usd or not price_usd.strip():
            return None
        try:
            return round(float(price_usd), 2)
        except ValueError:
            raise ValidationError("Price (USD) must be a number.")

    @staticmethod
    def _parse_weight(label: str, value: str) -> Optional[float]:
        """Net/gross weight per box (KG) - drives the packing list's Boxes x
        weight auto-calc, same role alternate_quantity plays for Qty."""
        if not value or not str(value).strip():
            return None
        try:
            weight = float(value)
        except ValueError:
            raise ValidationError(f"{label} must be a number (KG per box).")
        if weight < 0:
            raise ValidationError(f"{label} can't be negative.")
        return round(weight, 3)

    @staticmethod
    def _parse_unit(unit: str, default: str = "SQM") -> str:
        """A unit one of the product's quantities is measured in. Free text
        typed on the product form (SQM, LM, PCS, BOX, ...), normalised to
        uppercase; blank falls back to the default - quantity_unit defaults
        to PCS, and alternate_quantity_unit to SQM (it's what prefills the
        Unit column on document forms)."""
        unit = (unit or "").strip().upper()
        return unit or default

    @staticmethod
    def _parse_percent(label: str, value: str) -> Optional[float]:
        if not value or not str(value).strip():
            return None
        try:
            percent = float(value)
        except ValueError:
            raise ValidationError(f"{label} must be a number (percentage).")
        if percent < 0 or percent > 100:
            raise ValidationError(f"{label} must be between 0 and 100 (it's a percentage).")
        return round(percent, 2)

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


class InventoryService:
    """Read-only view of the catalog focused on stock on hand. It reuses
    ProductService for all catalog navigation (categories, products, sub
    categories, designs) and adds the stock numbers on top: a design's
    CURRENT STOCK is everything bought in (placed on a purchase order's
    packing list) minus everything sold. Sales aren't modelled yet, so stock
    currently equals the purchased totals; the shape below already leaves
    room to subtract sales once they exist."""

    def __init__(self, product_service: "ProductService", packing_list_repo: PackingListRepository,
                 design_repo: DesignRepository):
        self.products = product_service
        self.packing_list_repo = packing_list_repo
        self.design_repo = design_repo

    def _stock_from_bought(self, bought: dict) -> dict:
        """Turn a {boxes, pcs, quantity} purchased total into a stock figure.
        Sales, once modelled, get subtracted here."""
        return {
            "boxes": bought.get("boxes", 0) or 0,
            "pcs": bought.get("pcs", 0) or 0,
            "quantity": bought.get("quantity", 0) or 0,
            "unit": bought.get("unit") or None,  # the quantity's unit (SQM/PCS/...)
        }

    def stock_by_design(self, company_id: int) -> dict:
        """design_id -> {boxes, pcs, quantity} on hand, for the whole
        company in one query. Designs never bought are simply absent."""
        totals = self.packing_list_repo.bought_totals_by_design(company_id)
        return {design_id: self._stock_from_bought(bought) for design_id, bought in totals.items()}

    def stock_for_design(self, company_id: int, design_id: int) -> dict:
        """Current stock for a single design (zeros when never bought)."""
        return self.stock_by_design(company_id).get(
            design_id, {"boxes": 0, "pcs": 0, "quantity": 0, "unit": None}
        )

    def in_stock_designs(self, company_id: int) -> List[dict]:
        """Every design with nonzero current stock, newest-purchase concerns
        aside - just design + product name + stock, for the "in stock right
        now" summary at the top of the Inventory catalog root. One batched
        design lookup, no per-design queries."""
        totals = self.packing_list_repo.bought_totals_by_design(company_id)
        if not totals:
            return []
        rows = self.design_repo.list_by_ids_with_product(list(totals.keys()))
        in_stock = []
        for row in rows:
            stock = self._stock_from_bought(totals.get(row["id"], {}))
            if stock["boxes"] or stock["pcs"] or stock["quantity"]:
                in_stock.append({**row, "stock": stock})
        return in_stock

    def purchase_sale_history(self, company_id: int, design_id: int) -> List[dict]:
        """The design's Purchase/Sale history, newest first. Every row is a
        purchase for now (goods placed on a purchase order's packing list);
        each is tagged so sales can join the same timeline later."""
        history = []
        for row in self.packing_list_repo.bought_history_for_design(company_id, design_id):
            row["kind"] = "purchase"
            history.append(row)
        return history


# ============================================================
# DOCUMENT VERSION SERVICE (shared version-history mechanism for
# quotations/proforma invoices/packing lists - see DocumentVersionRepository
# and the document_versions table in schema.sql)
# ============================================================

# document_type -> (header dataclass, item dataclass, number field name).
# Every versioned document exposes an `items: List[...]` field, so a single
# rehydrate routine works for all three.
_VERSIONED_TYPES = {
    "quotation": (Quotation, QuotationItem, "quotation_number"),
    "proforma_invoice": (ProformaInvoice, ProformaInvoiceItem, "invoice_number"),
    "purchase_order": (PurchaseOrder, PurchaseOrderItem, "po_number"),
    "purchase_invoice": (PurchaseInvoice, PurchaseInvoiceItem, "purchase_invoice_number"),
    "export_invoice": (ExportInvoice, ExportInvoiceItem, "export_invoice_number"),
    "packing_list": (PackingList, PackingListItem, "packing_list_number"),
}


class DocumentVersionService:
    """Snapshots a document's full state on every create/update, under the
    same document number - editing a quotation/PI/packing list never mints a
    new document number, it just adds a version. Read access is admin-only,
    enforced at the route layer (a low-privilege user's own edit history
    isn't theirs to browse)."""

    def __init__(self, version_repo: DocumentVersionRepository):
        self.version_repo = version_repo

    def record(self, document_type: str, document, changed_by: int) -> None:
        """`document` is the freshly persisted Quotation/ProformaInvoice/
        PackingList (company_id/id/number/items already set by the caller's
        create()/update())."""
        _, _, number_field = _VERSIONED_TYPES[document_type]
        self.version_repo.record(
            company_id=document.company_id, document_type=document_type, document_id=document.id,
            document_number=getattr(document, number_field), snapshot=dataclasses.asdict(document),
            changed_by=changed_by,
        )

    def list_for_document(self, document_type: str, document_id: int) -> List[DocumentVersion]:
        return self.version_repo.list_for_document(document_type, document_id)

    def get_version(self, document_type: str, document_id: int, version_number: int):
        """Returns (rehydrated document, DocumentVersion) for one historical
        version - rehydrated back into its real dataclass (not a bare dict)
        so print templates and computed properties like invoice_value_usd
        keep working unmodified."""
        version = self.version_repo.get_version(document_type, document_id, version_number)
        if not version:
            raise NotFoundError(f"Version {version_number} not found.")
        header_cls, item_cls, _ = _VERSIONED_TYPES[document_type]
        data = dict(version.snapshot)
        items_data = data.pop("items", [])
        document = header_cls(**data)
        document.items = [item_cls(**item) for item in items_data]
        return document, version


# ============================================================
# FOB-TYPED PRICING (shared by quotation / proforma / export invoice)
# ============================================================
def apply_fob_uplift(document) -> None:
    """Clears the vestigial per-item `fob_price_usd`.

    There used to be an FOB-typed-price mode on all three buyer-facing
    documents: a "Prices typed are FOB" checkbox spread the charges over the
    total ALT QTY and added that per-unit figure onto every line, so the
    stored price_usd became a CIF price and fob_price_usd remembered what was
    typed. That mode is gone from the quotation, the proforma invoice and the
    export invoice alike - the typed price is now taken at face value and the
    CIF value is a document-level figure (see Quotation.cif_value_usd /
    ProformaInvoice.cif_value_usd, which add the charges on top of the FOB
    goods total).

    `fob_pricing` is therefore hardcoded off in all three builders, and all
    this does is make sure no line keeps a stale fob_price_usd. Kept as one
    shared function (rather than three inline loops) so the three document
    types can't drift apart if the field is ever revived.
    """
    for item in document.items or []:
        item.fob_price_usd = None


# ============================================================
# QUOTATION SERVICE
# ============================================================
class QuotationService:
    def __init__(self, quotation_repo: QuotationRepository, product_repo: ProductRepository,
                 lead_repo: LeadRepositoryBase, version_service: "DocumentVersionService",
                 misc_list_service: Optional["MiscListService"] = None):
        self.quotation_repo = quotation_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.version_service = version_service
        # Resolves the picked currency name to its symbol (Administration ->
        # Miscellaneous). Optional, so an unwired service just keeps the
        # submitted name and no symbol.
        self.misc_list_service = misc_list_service

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
                pallets = float(raw["pallets"]) if raw.get("pallets") else None
            except ValueError:
                raise ValidationError(f"Row {i}: quantity, pallets and price must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None
            quantity_unit = "PCS"

            # Only trust a product from this same company - otherwise a
            # crafted product_id could pull another company's catalog data
            # in. Qty is then authoritatively boxes x that product's
            # Alternate Quantity whenever both are known - the client-side
            # value is only a convenience preview, not trusted for storage.
            # The Boxes column's unit (printed as small text after the
            # number) is likewise always the product's own Quantity unit,
            # snapshotted at save time the same way `unit` snapshots
            # Alternate Quantity unit.
            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None
                else:
                    quantity_unit = product.quantity_unit or "PCS"
                    if quantity_boxes and product.alternate_quantity:
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
                quantity_boxes=quantity_boxes, quantity_unit=quantity_unit, pallets=pallets, quantity_value=quantity_value,
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
        # The delivery terms decide which charges are chargeable at all: FOB
        # hands the whole ocean leg to the buyer (no freight, no insurance and
        # no certification), CFR keeps both but leaves the buyer to insure the
        # cargo. The
        # form hides whichever inputs don't apply, and anything that still
        # reaches here (a stale form, an API post) is stored as zero. The two
        # questions are asked separately so a term can drop one charge without
        # touching the other.
        shipping_terms = (fields.get("shipping_terms") or "").strip() or None
        no_sea_freight = drops_sea_freight(shipping_terms)
        no_insurance = drops_insurance(shipping_terms)
        no_certification = drops_certification(shipping_terms)

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

        # Currency the document is written in: the form posts a name off the
        # Miscellaneous currency list, and both name and symbol are
        # snapshotted so editing that list later can't rewrite an issued
        # sheet. Display information only - amounts are stored as typed.
        currency_code, currency_symbol = (
            self.misc_list_service.resolve_currency(current_user.company_id, fields.get("currency_code"))
            if self.misc_list_service else ((fields.get("currency_code") or "").strip() or None, None)
        )

        quotation = Quotation(
            id=None, company_id=current_user.company_id, quotation_number="", quotation_date=quotation_date,
            buyer_name=buyer_name, created_by=current_user.id, lead_id=lead_id,
            buyer_address=(fields.get("buyer_address") or "").strip() or None,
            buyer_reference_no=(fields.get("buyer_reference_no") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            final_destination=(fields.get("final_destination") or "").strip() or None,
            packing_details=(fields.get("packing_details") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            shipping_mode=(fields.get("shipping_mode") or "").strip() or None,
            shipping_terms=shipping_terms,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            price_validity_days=_int("price_validity_days", 30),
            remarks=(fields.get("remarks") or "").strip() or None,
            sea_freight=0 if no_sea_freight else _float("sea_freight", 0),
            insurance=0 if no_insurance else _float("insurance", 0),
            certification=0 if fields.get("no_certification") else _float("certification", 0),
            other_charges=_float("other_charges", 0),
            discount_amount=_float("discount_amount", 0),
            # Quotations no longer have an FOB-typed-price mode - the typed
            # price is always the absolute FOB price, never adjusted by an
            # uplift. Hardcoded off (rather than read from `fields`) so even
            # a direct API/service call can't revive the old behavior.
            fob_pricing=False,
            cif_adjust_usd=_float("cif_adjust_usd", 0),
            bank_name=(fields.get("bank_name") or "").strip() or None,
            bank_account_number=(fields.get("bank_account_number") or "").strip() or None,
            bank_ifsc_code=(fields.get("bank_ifsc_code") or "").strip() or None,
            bank_swift_code=(fields.get("bank_swift_code") or "").strip() or None,
            bank_branch=(fields.get("bank_branch") or "").strip() or None,
            bank_address=(fields.get("bank_address") or "").strip() or None,
            currency_code=currency_code, currency_symbol=currency_symbol,
            items=items,
        )
        return quotation

    def _advance_lead_to_in_client(self, lead_id: Optional[int]) -> None:
        """A quotation being generated for a lead - or attached to one on
        edit - means that lead has moved past pure follow-up into active
        quotation/client territory, so its status jumps straight to the
        final LEAD_STATUSES stage. Left alone once the lead has actually
        converted to a client (its own status then lives on the Client
        record, not the Lead)."""
        if not lead_id:
            return
        lead = self.lead_repo.get_by_id(lead_id)
        if lead and not lead.is_converted and lead.status != "in_client":
            self.lead_repo.update_status(lead_id, "in_client")

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> Quotation:
        items = self._build_items(current_user.company_id, raw_items)
        quotation = self._build_header(current_user, fields, items)
        apply_fob_uplift(quotation)
        quotation.quotation_number = self._generate_number(current_user.company_id, quotation.quotation_date)
        created = self.quotation_repo.create(quotation)
        self.version_service.record("quotation", created, current_user.id)
        self._advance_lead_to_in_client(created.lead_id)
        return created

    def update(self, current_user: User, quotation_id: int, fields: dict, raw_items: list) -> Quotation:
        existing = self.get(quotation_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        quotation = self._build_header(current_user, fields, items)
        apply_fob_uplift(quotation)
        self.quotation_repo.update(quotation_id, quotation)
        updated = self.get(quotation_id, current_user.company_id)
        self.version_service.record("quotation", updated, current_user.id)
        self._advance_lead_to_in_client(updated.lead_id)
        return updated

    def duplicate(self, current_user: User, quotation_id: int) -> Quotation:
        """Creates a second, independent quotation carrying over every header
        field and product line of an existing one. Only the identity of the
        document is fresh: a newly generated number and today's date (a copy
        is being raised now, not back when the original was). Nothing links
        the two afterwards - the copy is edited and deleted on its own."""
        source = self.get(quotation_id, current_user.company_id)
        copy = dataclasses.replace(
            source, id=None, quotation_number="", quotation_date=date.today().isoformat(),
            created_by=current_user.id, created_at=None, updated_at=None,
            items=[dataclasses.replace(item, id=None, quotation_id=None) for item in source.items],
        )
        copy.quotation_number = self._generate_number(current_user.company_id, copy.quotation_date)
        created = self.quotation_repo.create(copy)
        self.version_service.record("quotation", created, current_user.id)
        return created

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
                 lead_repo: LeadRepositoryBase, quotation_repo: QuotationRepository,
                 version_service: "DocumentVersionService", party_repos: Optional[dict] = None,
                 misc_list_service: Optional["MiscListService"] = None):
        self.misc_list_service = misc_list_service
        self.invoice_repo = invoice_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.quotation_repo = quotation_repo
        self.version_service = version_service
        self.party_repos = party_repos  # {'Buyer': ..., 'Supplier': ..., 'Exporter': ...} for advance_client_status

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

    def get_for_quotation(self, quotation_id: Optional[int]) -> Optional[ProformaInvoice]:
        """Returns the most recently created proforma invoice already
        generated from this quotation, or None if none exists yet."""
        if not quotation_id:
            return None
        invoices = self.invoice_repo.list_for_quotation(quotation_id)
        return invoices[0] if invoices else None

    def map_by_quotation(self, company_id: int) -> dict:
        """quotation_id -> most recent proforma_invoice id, for the quotations
        list page to switch "Generate PI" to "View PI" without an N+1 query."""
        return self.invoice_repo.map_by_quotation(company_id)

    def list_by_status(self, company_id: int, status: str) -> List[ProformaInvoice]:
        return self.invoice_repo.list_by_status(company_id, status)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, invoice: ProformaInvoice, current_user: User):
        """Ownership first, then the confirmation lock: a confirmed invoice is
        the version the buyer has agreed to and the version the purchase
        orders are being placed against, so it is frozen for everyone until
        an admin deliberately moves it back to draft (set_status below)."""
        if not current_user.is_admin and invoice.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage proforma invoices you created yourself.")
        if invoice.is_confirmed:
            raise ValidationError(
                f"Proforma invoice {invoice.invoice_number} is confirmed and locked. "
                "An admin has to move it back to draft before it can be edited or deleted."
            )

    # ---- status --------------------------------------------------
    def set_status(self, current_user: User, invoice_id: int, status: str) -> ProformaInvoice:
        """Confirm an invoice (anyone who could edit it) or send it back to
        draft (admins only - reopening a confirmed document is the override,
        not the everyday action). Deliberately does not go through
        _assert_can_modify, which is what enforces the lock this method
        releases."""
        invoice = self.get(invoice_id, current_user.company_id)
        if status not in dict(PROFORMA_STATUSES):
            raise ValidationError("Invalid proforma invoice status.")
        if not current_user.is_admin and invoice.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage proforma invoices you created yourself.")
        if status == PROFORMA_STATUS_DRAFT and not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can move a confirmed proforma invoice back to draft.")
        if status != invoice.status:
            self.invoice_repo.update_status(invoice_id, status)
        return self.get(invoice_id, current_user.company_id)

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
            "final_destination": quotation.final_destination,
            "container_details": quotation.container_details,
            "packing_details": quotation.packing_details,
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
            "remarks": quotation.remarks,
            # The document the goods were quoted in is the one they are
            # invoiced in - locked, not just prefilled (see _build_header).
            "currency_code": quotation.currency_code,
        }
        items = [
            {
                "product_id": item.product_id, "product_name": item.product_name,
                "hsn_code": item.hsn_code, "quantity_boxes": item.quantity_boxes,
                "pallets": item.pallets, "quantity_value": item.quantity_value, "unit": item.unit,
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
            quantity_unit = "PCS"

            # Same trust boundary as QuotationService._build_items - only
            # keep a product reference from this same company, and the same
            # Boxes x Alternate Quantity auto-calc when both are known. The
            # Boxes column's unit (printed as small text after the number)
            # is likewise always the product's own Quantity unit.
            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None
                else:
                    quantity_unit = product.quantity_unit or "PCS"
                    if quantity_boxes and product.alternate_quantity:
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
                pallets=pallets, quantity_boxes=quantity_boxes, quantity_unit=quantity_unit, quantity_value=quantity_value,
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

        # See the quotation builder: FOB drops the freight, the insurance and
        # the certification; CFR drops the insurance only.
        terms_of_delivery = (fields.get("terms_of_delivery") or "").strip() or None
        no_sea_freight = drops_sea_freight(terms_of_delivery)
        no_insurance = drops_insurance(terms_of_delivery)
        no_certification = drops_certification(terms_of_delivery)

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            # Only trust a lead from this same company - otherwise a crafted
            # lead_id could attach this invoice to another company's lead.
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        quotation_id = int(fields["quotation_id"]) if fields.get("quotation_id") else None
        linked_quotation = None
        if quotation_id is not None:
            # Only trust a quotation from this same company - same reasoning as lead_id above.
            linked_quotation = self.quotation_repo.get_by_id(quotation_id)
            if not linked_quotation or linked_quotation.company_id != current_user.company_id:
                quotation_id = None
                linked_quotation = None

        # Currency the document is written in: the form posts a name off the
        # Miscellaneous currency list, and both name and symbol are
        # snapshotted so editing that list later can't rewrite an issued
        # sheet. Display information only - amounts are stored as typed.
        # Once linked to a quotation, the currency is inherited from it
        # instead - the same document is invoiced in what it was quoted in,
        # and the form disables the field once linked, so this also guards a
        # tampered POST.
        currency_source = linked_quotation.currency_code if linked_quotation else fields.get("currency_code")
        currency_code, currency_symbol = (
            self.misc_list_service.resolve_currency(current_user.company_id, currency_source)
            if self.misc_list_service else ((currency_source or "").strip() or None, None)
        )

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
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            final_destination=(fields.get("final_destination") or "").strip() or None,
            transhipment=(fields.get("transhipment") or "").strip() or None,
            partial_shipment=(fields.get("partial_shipment") or "").strip() or None,
            variation_in_qty=(fields.get("variation_in_qty") or "").strip() or None,
            delivery_period=(fields.get("delivery_period") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            packing_details=(fields.get("packing_details") or "").strip() or None,
            terms_of_delivery=terms_of_delivery,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            sea_freight=0 if no_sea_freight else _float("sea_freight", 0),
            insurance=0 if no_insurance else _float("insurance", 0),
            certification=0 if fields.get("no_certification") else _float("certification", 0),
            other_charges=_float("other_charges", 0),
            discount_amount=_float("discount_amount", 0),
            # Proforma invoices no longer have an FOB-typed-price mode - the
            # typed price is always the absolute FOB price, never adjusted by
            # an uplift (see ProformaInvoice.cif_value_usd, which builds CIF
            # upward from it, mirroring Quotation.cif_value_usd). Hardcoded
            # off (rather than read from `fields`) so even a direct API/
            # service call can't revive the old uplift. See
            # ExportInvoiceService, which took over the "Prices typed above
            # are FOB" checkbox.
            fob_pricing=False,
            bank_name=(fields.get("bank_name") or "").strip() or None,
            bank_account_number=(fields.get("bank_account_number") or "").strip() or None,
            bank_ifsc_code=(fields.get("bank_ifsc_code") or "").strip() or None,
            bank_swift_code=(fields.get("bank_swift_code") or "").strip() or None,
            bank_branch=(fields.get("bank_branch") or "").strip() or None,
            bank_address=(fields.get("bank_address") or "").strip() or None,
            currency_code=currency_code, currency_symbol=currency_symbol,
            display_mode=fields.get("display_mode") if fields.get("display_mode") in ("index", "surface") else "index",
            items=items,
        )
        return invoice

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> ProformaInvoice:
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        apply_fob_uplift(invoice)
        invoice.invoice_number = self._generate_number(current_user.company_id, invoice.invoice_date)
        created = self.invoice_repo.create(invoice)
        self.version_service.record("proforma_invoice", created, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, created.lead_id, "proforma_invoice")
        return created

    def update(self, current_user: User, invoice_id: int, fields: dict, raw_items: list) -> ProformaInvoice:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        apply_fob_uplift(invoice)
        self.invoice_repo.update(invoice_id, invoice)
        updated = self.get(invoice_id, current_user.company_id)
        self.version_service.record("proforma_invoice", updated, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, updated.lead_id, "proforma_invoice")
        return updated

    def delete(self, current_user: User, invoice_id: int) -> None:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.invoice_repo.delete(invoice_id)


# ============================================================
# PURCHASE ORDER SERVICE
# ============================================================
def is_intra_state(gstin_a: Optional[str], gstin_b: Optional[str]) -> bool:
    """True when both GSTINs belong to the same state - the first two digits
    of a GSTIN are its state code. A purchase inside one state is taxed
    CGST + SGST; across states it's IGST instead. Unknown either way (a
    missing or malformed GSTIN) counts as inter-state, which is the norm for
    an exporter buying from out-of-state suppliers."""
    a = (gstin_a or "").strip()[:2]
    b = (gstin_b or "").strip()[:2]
    return len(a) == 2 and a.isdigit() and a == b


class PurchaseOrderService:
    """Mirrors ProformaInvoiceService layer-for-layer. A purchase order is
    the next document after the Proforma Invoice in the client pipeline, but
    with the roles flipped: OUR company is the BUYER and a supplier is the
    SELLER, with prices in INR. It can be started from an existing proforma
    invoice (build_prefill_from_proforma) - copying the product lines in as
    a one-time prefill the same way a PI starts from a quotation."""

    def __init__(self, purchase_order_repo: PurchaseOrderRepository, product_repo: ProductRepository,
                 lead_repo: LeadRepositoryBase, proforma_invoice_repo: ProformaInvoiceRepository,
                 version_service: "DocumentVersionService", party_repos: Optional[dict] = None,
                 supplier_repo: Optional[SupplierRepositoryBase] = None,
                 company_repo: Optional[CompanyRepository] = None,
                 fulfilment_service: Optional["ProformaFulfilmentService"] = None,
                 misc_list_service: Optional["MiscListService"] = None):
        self.misc_list_service = misc_list_service
        self.purchase_order_repo = purchase_order_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.version_service = version_service
        self.party_repos = party_repos  # {'Buyer': ..., 'Supplier': ..., 'Exporter': ...} for advance_client_status
        self.supplier_repo = supplier_repo  # for validating seller_supplier_id belongs to this company
        self.company_repo = company_repo  # our own GSTIN, for the intra/inter-state tax split
        # Optional: when present, a new PO's product lines are cut down to
        # what the invoice still needs ordered (see build_prefill_from_proforma).
        self.fulfilment_service = fulfilment_service

    # ---- reads --------------------------------------------------
    def get(self, purchase_order_id: int, company_id: int) -> PurchaseOrder:
        purchase_order = self.purchase_order_repo.get_by_id(purchase_order_id)
        if not purchase_order or purchase_order.company_id != company_id:
            # 404, not 403 - don't reveal that another company's PO exists.
            raise NotFoundError(f"Purchase order #{purchase_order_id} not found.")
        return purchase_order

    def list_all(self, company_id: int) -> List[PurchaseOrder]:
        return self.purchase_order_repo.list_all(company_id)

    def list_for_lead(self, lead_id: Optional[int]) -> List[PurchaseOrder]:
        """Same shape as QuotationService.list_for_lead - unscoped by
        company_id because the caller already owns the lead/client."""
        if not lead_id:
            return []
        return self.purchase_order_repo.list_for_lead(lead_id)

    def list_for_proforma(self, proforma_invoice_id: Optional[int], company_id: int) -> List[PurchaseOrder]:
        """Every purchase order generated from this proforma invoice, newest
        first. One PI is normally ordered from several suppliers, so its page
        lists all of them; company_id is re-checked here because the caller
        passes an id straight off the invoice."""
        if not proforma_invoice_id:
            return []
        return [po for po in self.purchase_order_repo.list_for_proforma(proforma_invoice_id)
                if po.company_id == company_id]

    def count_map_by_proforma(self, company_id: int) -> dict:
        """proforma_invoice_id -> number of purchase orders placed against it,
        for the proforma list page's PO column."""
        return self.purchase_order_repo.count_map_by_proforma(company_id)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, purchase_order: PurchaseOrder, current_user: User):
        if current_user.is_admin:
            return
        if purchase_order.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage purchase orders you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, company_id: int, po_date: str) -> str:
        """PO{YYYYMMDD}{seq} where seq is that day's purchase order count + 1
        for this company, zero-padded to 3 digits (e.g. PO20260718001)."""
        date_part = po_date.replace("-", "")
        prefix = f"PO{date_part}"
        seq = self.purchase_order_repo.count_for_date_prefix(company_id, prefix) + 1
        return f"{prefix}{seq:03d}"

    # ---- prefill from an existing proforma invoice --------------------------------------------------
    def build_prefill_from_proforma(self, invoice: ProformaInvoice) -> dict:
        """Caller must have already loaded `invoice` via
        ProformaInvoiceService.get(invoice_id, current_user.company_id) so
        cross-company ownership is already verified. Product lines carry
        over (product/HSN/boxes/qty/unit); the INR ex-factory price is a
        different figure from the proforma's USD selling price, so it is
        left for the user to type in. Seller details also stay blank - the
        proforma's consignee is the foreign buyer, not the supplier this PO
        is being placed with.

        One invoice is normally split across several suppliers, so the
        product lines are cut down to what's still outstanding - a line
        already placed in full on another purchase order linked to this
        same invoice is dropped, and a partly-placed one comes through at
        its remaining boxes/quantity only (see _remaining_products). The
        second and third PO built from the same invoice therefore don't
        start out re-ordering the first one's goods, same as the packing-
        list side already does (PackingListService._remaining_designs)."""
        fields = {
            "proforma_invoice_id": invoice.id,
            "lead_id": invoice.lead_id,
            "port_of_loading": invoice.port_of_loading,
            "port_of_discharge": invoice.port_of_discharge,
            "container_details": invoice.container_details,
            "remarks": invoice.remarks,
        }
        return {"fields": fields, "items": self._remaining_products(invoice)}

    @staticmethod
    def _raw_item(item: ProformaInvoiceItem) -> dict:
        return {
            "product_id": item.product_id, "product_name": item.product_name,
            "hsn_code": item.hsn_code, "quantity_boxes": item.quantity_boxes,
            "quantity_value": item.quantity_value, "unit": item.unit,
            "price_inr": "", "price_per": "BOX",
        }

    def _remaining_products(self, invoice: ProformaInvoice) -> list:
        """Every one of the invoice's product lines, cut down to what's
        still outstanding and scaled to that outstanding share - the
        product-level counterpart of PackingListService._remaining_designs.

        No-op (every line unchanged) when there's no fulfilment service
        wired in, so the plain "copy the invoice's product lines over"
        behaviour still holds wherever this isn't available."""
        if not self.fulfilment_service:
            return [self._raw_item(item) for item in invoice.items]
        status = self.fulfilment_service.product_status(invoice.company_id, invoice)
        pending = {_product_key({"product_id": p["product_id"], "product_name": p["product_name"]}): p
                   for p in status["pending"]}

        remaining = []
        for item in invoice.items:
            key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
            product = pending.get(key)
            if not product:
                continue  # already placed in full on another linked purchase order
            remaining.append(self._scaled_item(item, product))
        return remaining

    @classmethod
    def _scaled_item(cls, item: ProformaInvoiceItem, product: dict) -> dict:
        """One invoice product line rescaled to its outstanding share - same
        ratio approach as PackingListService._scaled_row. A ratio of 1 -
        nothing placed yet, the usual case for the first PO - leaves the
        row unchanged."""
        row = cls._raw_item(item)
        if product["required_boxes"] > 0:
            ratio = product["pending_boxes"] / product["required_boxes"]
        elif product["required_quantity"] > 0:
            ratio = product["pending_quantity"] / product["required_quantity"]
        else:
            ratio = 1
        if ratio >= 1:
            return row
        for key in ("quantity_boxes", "quantity_value"):
            if row.get(key) not in (None, ""):
                row[key] = round(float(row[key]) * ratio, 2) or ""
        return row

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[PurchaseOrderItem]:
        items = []
        for i, raw in enumerate(raw_items, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            try:
                quantity_value = float(raw.get("quantity_value") or 0)
                price_inr = float(raw.get("price_inr") or 0)
                quantity_boxes = float(raw["quantity_boxes"]) if raw.get("quantity_boxes") else None
            except ValueError:
                raise ValidationError(f"Row {i}: quantity and price must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None
            quantity_unit = "PCS"

            # Same trust boundary as QuotationService._build_items - only
            # keep a product reference from this same company, and the same
            # Boxes x Alternate Quantity auto-calc when both are known. The
            # Boxes column's unit (printed as small text after the number)
            # is likewise always the product's own Quantity unit.
            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None
                else:
                    quantity_unit = product.quantity_unit or "PCS"
                    if quantity_boxes and product.alternate_quantity:
                        try:
                            quantity_value = round(quantity_boxes * float(product.alternate_quantity), 2)
                        except ValueError:
                            pass

            if quantity_value <= 0:
                raise ValidationError(f"Row {i} ('{product_name}'): quantity is compulsory and must be greater than zero.")
            if price_inr < 0:
                raise ValidationError(f"Row {i} ('{product_name}'): price can't be negative.")

            unit = (raw.get("unit") or "SQM").strip() or "SQM"
            # The rate is per BOX (the ex-factory norm, as on the reference
            # PO) or per the row's quantity unit - the total follows from
            # whichever basis the row uses.
            price_per = "BOX" if (raw.get("price_per") or "BOX").strip().upper() == "BOX" else unit
            if price_per == "BOX":
                if not quantity_boxes:
                    raise ValidationError(f"Row {i} ('{product_name}'): boxes is compulsory when the price is per box.")
                total_inr = round(quantity_boxes * price_inr, 2)
            else:
                total_inr = round(quantity_value * price_inr, 2)

            items.append(PurchaseOrderItem(
                id=None, purchase_order_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                quantity_boxes=quantity_boxes, quantity_unit=quantity_unit, quantity_value=quantity_value, unit=unit,
                price_inr=price_inr, price_per=price_per, total_inr=total_inr,
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    # ---- tax derivation --------------------------------------------------
    def base_igst_percent(self, company_id: int, purchase_type: str, items: List[PurchaseOrderItem]) -> float:
        """The full order's tax rate before it is split into IGST or
        CGST+SGST. Under Exemption it's the flat concessional rate; under a
        Full Tax Purchase it comes from the catalog products on the lines
        (their own stored IGST %). Lines can in principle carry different
        rates while the order stores one - the highest wins, so the order is
        never under-taxed. Typed-in lines with no catalog product behind them
        contribute nothing."""
        if purchase_type == "exemption":
            return EXEMPTION_IGST_PERCENT
        rate = 0.0
        for item in items:
            if not item.product_id:
                continue
            product = self.product_repo.get_by_id(item.product_id)
            if product and product.company_id == company_id and product.igst_percent:
                rate = max(rate, float(product.igst_percent))
        return rate

    def _tax_percentages(self, company_id: int, purchase_type: str, seller_gstin: Optional[str],
                         items: List[PurchaseOrderItem]) -> tuple:
        """(igst, cgst, sgst) for the order. The rate itself comes from
        `purchase_type` (see base_igst_percent); where it lands depends on
        the state codes of our GSTIN and the seller's - same state means
        CGST + SGST at half each, different states means IGST alone."""
        rate = self.base_igst_percent(company_id, purchase_type, items)
        our_company = self.company_repo.get(company_id) if self.company_repo else None
        if is_intra_state(our_company.gstin if our_company else None, seller_gstin):
            half = round(rate / 2, 4)
            return 0.0, half, half
        return rate, 0.0, 0.0

    def _build_header(self, current_user: User, fields: dict, items: List[PurchaseOrderItem]) -> PurchaseOrder:
        seller_name = (fields.get("seller_name") or "").strip()
        if not seller_name:
            raise ValidationError("Seller name is compulsory.")
        po_date = (fields.get("po_date") or "").strip() or date.today().isoformat()

        purchase_type = (fields.get("purchase_type") or "").strip() or DEFAULT_PURCHASE_TYPE
        if purchase_type not in PURCHASE_TYPES:
            raise ValidationError("'Purchase under' must be either a full tax purchase or an exemption.")
        seller_gstin = (fields.get("seller_gstin") or "").strip() or None
        # Percentages are never taken from the form - the form only displays
        # them, so a posted value would be a stale (or crafted) copy of what
        # is derived here.
        igst_percent, cgst_percent, sgst_percent = self._tax_percentages(
            current_user.company_id, purchase_type, seller_gstin, items
        )

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            # Only trust a lead from this same company - otherwise a crafted
            # lead_id could attach this PO to another company's lead.
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        proforma_invoice_id = int(fields["proforma_invoice_id"]) if fields.get("proforma_invoice_id") else None
        if proforma_invoice_id is not None:
            # Only trust a proforma invoice from this same company - same reasoning as lead_id above.
            invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
            if not invoice or invoice.company_id != current_user.company_id:
                proforma_invoice_id = None

        seller_supplier_id = int(fields["seller_supplier_id"]) if fields.get("seller_supplier_id") else None
        if seller_supplier_id is not None and self.supplier_repo is not None:
            # Only trust a supplier from this same company - same reasoning as lead_id above.
            supplier = self.supplier_repo.get_by_id(seller_supplier_id)
            if not supplier or supplier.company_id != current_user.company_id:
                seller_supplier_id = None

        # Currency the document is written in: the form posts a name off the
        # Miscellaneous currency list, and both name and symbol are
        # snapshotted so editing that list later can't rewrite an issued
        # sheet. Display information only - amounts are stored as typed.
        currency_code, currency_symbol = (
            self.misc_list_service.resolve_currency(current_user.company_id, fields.get("currency_code"))
            if self.misc_list_service else ((fields.get("currency_code") or "").strip() or None, None)
        )

        return PurchaseOrder(
            id=None, company_id=current_user.company_id, po_number="", po_date=po_date,
            seller_name=seller_name, created_by=current_user.id, lead_id=lead_id,
            proforma_invoice_id=proforma_invoice_id, seller_supplier_id=seller_supplier_id,
            seller_address=(fields.get("seller_address") or "").strip() or None,
            seller_pan=(fields.get("seller_pan") or "").strip() or None,
            seller_gstin=seller_gstin,
            seller_ref_no=(fields.get("seller_ref_no") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            delivery_time=(fields.get("delivery_time") or "").strip() or None,
            advance_percent=(fields.get("advance_percent") or "").strip() or None,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            igst_percent=igst_percent,
            cgst_percent=cgst_percent,
            sgst_percent=sgst_percent,
            purchase_type=purchase_type,
            currency_code=currency_code, currency_symbol=currency_symbol,
            items=items,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> PurchaseOrder:
        items = self._build_items(current_user.company_id, raw_items)
        purchase_order = self._build_header(current_user, fields, items)
        purchase_order.po_number = self._generate_number(current_user.company_id, purchase_order.po_date)
        created = self.purchase_order_repo.create(purchase_order)
        self.version_service.record("purchase_order", created, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, created.lead_id, "purchase_order")
        return created

    def update(self, current_user: User, purchase_order_id: int, fields: dict, raw_items: list) -> PurchaseOrder:
        existing = self.get(purchase_order_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        purchase_order = self._build_header(current_user, fields, items)
        self.purchase_order_repo.update(purchase_order_id, purchase_order)
        updated = self.get(purchase_order_id, current_user.company_id)
        self.version_service.record("purchase_order", updated, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, updated.lead_id, "purchase_order")
        return updated

    def delete(self, current_user: User, purchase_order_id: int) -> None:
        existing = self.get(purchase_order_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.purchase_order_repo.delete(purchase_order_id)


# ============================================================
# PURCHASE INVOICE SERVICE
# ============================================================
class PurchaseInvoiceService:
    """The last document in the pipeline: raised once a supplier's goods
    against one of our purchase orders actually arrive. Mirrors
    PurchaseOrderService's shape (header + line items, day-scoped number
    sequence, creator-or-admin edit lock), but with two differences that
    follow from what this document actually is:

    - There is nothing to print/generate here - the supplier already sent
      their own invoice as a PDF, so this service also stores an uploaded
      file (see _save_pdf/_delete_pdf_file, same pattern as
      ProductService._save_image) alongside the typed-in figures.
    - Its product lines are copied over from the linked purchase order in
      FULL (see build_prefill_from_purchase_order) rather than cut down to
      an outstanding remainder - unlike a PI's purchase orders, a purchase
      invoice isn't splitting anything further, it's recording what one
      specific supplier shipment actually contained."""

    def __init__(self, purchase_invoice_repo: PurchaseInvoiceRepository, product_repo: ProductRepository,
                 lead_repo: LeadRepositoryBase, purchase_order_repo: PurchaseOrderRepository,
                 version_service: "DocumentVersionService", party_repos: Optional[dict] = None,
                 supplier_repo: Optional[SupplierRepositoryBase] = None,
                 upload_folder: str = "", allowed_extensions: set = frozenset(),
                 misc_list_service: Optional["MiscListService"] = None):
        self.misc_list_service = misc_list_service
        self.purchase_invoice_repo = purchase_invoice_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.purchase_order_repo = purchase_order_repo
        self.version_service = version_service
        self.party_repos = party_repos  # {'Buyer': ..., 'Supplier': ..., 'Exporter': ...} for advance_client_status
        self.supplier_repo = supplier_repo  # for validating seller_supplier_id belongs to this company
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions

    # ---- reads --------------------------------------------------
    def get(self, purchase_invoice_id: int, company_id: int) -> PurchaseInvoice:
        purchase_invoice = self.purchase_invoice_repo.get_by_id(purchase_invoice_id)
        if not purchase_invoice or purchase_invoice.company_id != company_id:
            # 404, not 403 - don't reveal that another company's purchase invoice exists.
            raise NotFoundError(f"Purchase invoice #{purchase_invoice_id} not found.")
        return purchase_invoice

    def list_all(self, company_id: int) -> List[PurchaseInvoice]:
        return self.purchase_invoice_repo.list_all(company_id)

    def list_for_purchase_order(self, purchase_order_id: Optional[int], company_id: int) -> List[PurchaseInvoice]:
        if not purchase_order_id:
            return []
        return [pinv for pinv in self.purchase_invoice_repo.list_for_purchase_order(purchase_order_id)
                if pinv.company_id == company_id]

    def count_map_by_purchase_order(self, company_id: int) -> dict:
        return self.purchase_invoice_repo.count_map_by_purchase_order(company_id)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, purchase_invoice: PurchaseInvoice, current_user: User):
        if current_user.is_admin:
            return
        if purchase_invoice.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage purchase invoices you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, company_id: int, invoice_date: str) -> str:
        """PINV{YYYYMMDD}{seq} where seq is that day's purchase invoice count
        + 1 for this company - our own internal identifier, distinct from
        the supplier's own invoice_number typed in on the form."""
        date_part = invoice_date.replace("-", "")
        prefix = f"PINV{date_part}"
        seq = self.purchase_invoice_repo.count_for_date_prefix(company_id, prefix) + 1
        return f"{prefix}{seq:03d}"

    # ---- prefill from an existing purchase order --------------------------------------------------
    def build_prefill_from_purchase_order(self, purchase_order: PurchaseOrder) -> dict:
        """Caller must have already loaded `purchase_order` via
        PurchaseOrderService.get(purchase_order_id, current_user.company_id)
        so cross-company ownership is already verified. Seller details and
        product lines carry over in full - each supplier's Purchase Invoice
        covers exactly the one PO it's raised against, so there's no
        "outstanding remainder" to cut down to (unlike a PO built from a
        proforma invoice, which can be one of several splitting the same
        order). The PO's own computed tax amounts (igst_amount/cgst_amount/
        sgst_amount - derived from its stored percentages, see
        PurchaseOrder.igst_amount etc.) are copied in as a starting point
        too, since the supplier's actual invoice should normally charge the
        same tax the PO was placed under - the user can still adjust them
        here to match what the supplier's invoice actually says."""
        fields = {
            "purchase_order_id": purchase_order.id,
            "lead_id": purchase_order.lead_id,
            "seller_supplier_id": purchase_order.seller_supplier_id,
            "seller_name": purchase_order.seller_name,
            "seller_address": purchase_order.seller_address,
            "seller_pan": purchase_order.seller_pan,
            "seller_gstin": purchase_order.seller_gstin,
            "seller_ref_no": purchase_order.seller_ref_no,
            "port_of_loading": purchase_order.port_of_loading,
            "port_of_discharge": purchase_order.port_of_discharge,
            "container_details": purchase_order.container_details,
            "igst_amount": purchase_order.igst_amount,
            "cgst_amount": purchase_order.cgst_amount,
            "sgst_amount": purchase_order.sgst_amount,
            # The supplier invoices in the currency the order was placed in.
            "currency_code": purchase_order.currency_code,
        }
        items = [self._raw_item(item) for item in purchase_order.items]
        return {"fields": fields, "items": items}

    @staticmethod
    def _raw_item(item: PurchaseOrderItem) -> dict:
        return {
            "product_id": item.product_id, "product_name": item.product_name,
            "hsn_code": item.hsn_code, "quantity_boxes": item.quantity_boxes,
            "quantity_value": item.quantity_value, "unit": item.unit,
            "price_inr": item.price_inr, "price_per": item.price_per,
        }

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[PurchaseInvoiceItem]:
        items = []
        for i, raw in enumerate(raw_items, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            try:
                quantity_value = float(raw.get("quantity_value") or 0)
                price_inr = float(raw.get("price_inr") or 0)
                quantity_boxes = float(raw["quantity_boxes"]) if raw.get("quantity_boxes") else None
            except ValueError:
                raise ValidationError(f"Row {i}: quantity and price must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None

            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None

            if quantity_value <= 0:
                raise ValidationError(f"Row {i} ('{product_name}'): quantity is compulsory and must be greater than zero.")
            if price_inr < 0:
                raise ValidationError(f"Row {i} ('{product_name}'): price can't be negative.")

            unit = (raw.get("unit") or "SQM").strip() or "SQM"
            price_per = "BOX" if (raw.get("price_per") or "BOX").strip().upper() == "BOX" else unit
            if price_per == "BOX":
                if not quantity_boxes:
                    raise ValidationError(f"Row {i} ('{product_name}'): boxes is compulsory when the price is per box.")
                total_inr = round(quantity_boxes * price_inr, 2)
            else:
                total_inr = round(quantity_value * price_inr, 2)

            items.append(PurchaseInvoiceItem(
                id=None, purchase_invoice_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                quantity_boxes=quantity_boxes, quantity_value=quantity_value, unit=unit,
                price_inr=price_inr, price_per=price_per, total_inr=total_inr,
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    @staticmethod
    def _parse_amount(fields: dict, key: str, label: str) -> float:
        raw = fields.get(key)
        if raw in (None, ""):
            return 0.0
        try:
            return float(raw)
        except ValueError:
            raise ValidationError(f"{label} must be a number.")

    @staticmethod
    def _clean_vehicle_numbers(raw_vehicle_numbers: list) -> List[str]:
        return [v.strip() for v in (raw_vehicle_numbers or []) if v and v.strip()]

    def _build_header(self, current_user: User, fields: dict, items: List[PurchaseInvoiceItem]) -> PurchaseInvoice:
        seller_name = (fields.get("seller_name") or "").strip()
        if not seller_name:
            raise ValidationError("Seller name is compulsory.")
        invoice_number = (fields.get("invoice_number") or "").strip()
        if not invoice_number:
            raise ValidationError("Invoice number is compulsory.")
        invoice_date = (fields.get("invoice_date") or "").strip()
        if not invoice_date:
            raise ValidationError("Invoice date is compulsory.")
        epcg_number = (fields.get("epcg_number") or "").strip() or None
        epcg_date = (fields.get("epcg_date") or "").strip() or None

        purchase_order_id = int(fields["purchase_order_id"]) if fields.get("purchase_order_id") else None
        if purchase_order_id is not None:
            # Only trust a purchase order from this same company - a crafted
            # id could otherwise attach this invoice to another company's PO.
            purchase_order = self.purchase_order_repo.get_by_id(purchase_order_id)
            if not purchase_order or purchase_order.company_id != current_user.company_id:
                purchase_order_id = None

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        seller_supplier_id = int(fields["seller_supplier_id"]) if fields.get("seller_supplier_id") else None
        if seller_supplier_id is not None and self.supplier_repo is not None:
            supplier = self.supplier_repo.get_by_id(seller_supplier_id)
            if not supplier or supplier.company_id != current_user.company_id:
                seller_supplier_id = None

        # Currency the document is written in: the form posts a name off the
        # Miscellaneous currency list, and both name and symbol are
        # snapshotted so editing that list later can't rewrite an issued
        # sheet. Display information only - amounts are stored as typed.
        currency_code, currency_symbol = (
            self.misc_list_service.resolve_currency(current_user.company_id, fields.get("currency_code"))
            if self.misc_list_service else ((fields.get("currency_code") or "").strip() or None, None)
        )

        return PurchaseInvoice(
            id=None, company_id=current_user.company_id, purchase_invoice_number="",
            invoice_number=invoice_number, invoice_date=invoice_date,
            seller_name=seller_name, created_by=current_user.id,
            purchase_order_id=purchase_order_id, lead_id=lead_id, seller_supplier_id=seller_supplier_id,
            seller_address=(fields.get("seller_address") or "").strip() or None,
            seller_pan=(fields.get("seller_pan") or "").strip() or None,
            seller_gstin=(fields.get("seller_gstin") or "").strip() or None,
            seller_ref_no=(fields.get("seller_ref_no") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            container_details=(fields.get("container_details") or "").strip() or None,
            transporter_name=(fields.get("transporter_name") or "").strip() or None,
            epcg_number=epcg_number, epcg_date=epcg_date,
            discount_amount=self._parse_amount(fields, "discount_amount", "Discount"),
            insurance_other=self._parse_amount(fields, "insurance_other", "Insurance and other"),
            freight=self._parse_amount(fields, "freight", "Freight"),
            igst_amount=self._parse_amount(fields, "igst_amount", "IGST"),
            cgst_amount=self._parse_amount(fields, "cgst_amount", "CGST"),
            sgst_amount=self._parse_amount(fields, "sgst_amount", "SGST"),
            round_off=self._parse_amount(fields, "round_off", "Round off"),
            remarks=(fields.get("remarks") or "").strip() or None,
            currency_code=currency_code, currency_symbol=currency_symbol,
            items=items,
        )

    # ---- supplier PDF storage --------------------------------------------------
    def _save_pdf(self, file_storage) -> Optional[str]:
        """Saves the supplier's own Purchase Invoice PDF under the purchase
        invoice upload folder with a collision-proof name and returns the
        path relative to static/ (same pattern as ProductService._save_image,
        restricted to PDFs since that's the only thing a supplier sends)."""
        if not file_storage or not file_storage.filename:
            return None
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in self.allowed_extensions:
            raise ValidationError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(self.allowed_extensions))}.")
        os.makedirs(self.upload_folder, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{filename}"
        file_storage.save(os.path.join(self.upload_folder, stored_name))
        return f"uploads/purchase_invoices/{stored_name}"

    def _delete_pdf_file(self, relative_path: Optional[str]) -> None:
        if not relative_path:
            return
        full_path = os.path.join(self.upload_folder, os.path.basename(relative_path))
        if os.path.exists(full_path):
            os.remove(full_path)

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list, raw_vehicle_numbers: list,
               pdf_file=None) -> PurchaseInvoice:
        items = self._build_items(current_user.company_id, raw_items)
        purchase_invoice = self._build_header(current_user, fields, items)
        purchase_invoice.vehicle_numbers = self._clean_vehicle_numbers(raw_vehicle_numbers)
        purchase_invoice.supplier_pdf_path = self._save_pdf(pdf_file)
        purchase_invoice.purchase_invoice_number = self._generate_number(current_user.company_id, purchase_invoice.invoice_date)
        created = self.purchase_invoice_repo.create(purchase_invoice)
        self.version_service.record("purchase_invoice", created, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, created.lead_id, "purchase_invoice")
        return created

    def update(self, current_user: User, purchase_invoice_id: int, fields: dict, raw_items: list,
               raw_vehicle_numbers: list, pdf_file=None, remove_pdf: bool = False) -> PurchaseInvoice:
        existing = self.get(purchase_invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        purchase_invoice = self._build_header(current_user, fields, items)
        purchase_invoice.vehicle_numbers = self._clean_vehicle_numbers(raw_vehicle_numbers)
        if pdf_file and pdf_file.filename:
            purchase_invoice.supplier_pdf_path = self._save_pdf(pdf_file)
            self._delete_pdf_file(existing.supplier_pdf_path)
        elif remove_pdf:
            self._delete_pdf_file(existing.supplier_pdf_path)
            purchase_invoice.supplier_pdf_path = None
        else:
            purchase_invoice.supplier_pdf_path = existing.supplier_pdf_path
        self.purchase_invoice_repo.update(purchase_invoice_id, purchase_invoice)
        updated = self.get(purchase_invoice_id, current_user.company_id)
        self.version_service.record("purchase_invoice", updated, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, updated.lead_id, "purchase_invoice")
        return updated

    def delete(self, current_user: User, purchase_invoice_id: int) -> None:
        existing = self.get(purchase_invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self._delete_pdf_file(existing.supplier_pdf_path)
        self.purchase_invoice_repo.delete(purchase_invoice_id)


# ============================================================
# EXPORT INVOICE SERVICE
# ============================================================
class ExportInvoiceService:
    """The customer/customs-facing Export Invoice at the buyer end of the
    pipeline. Mirrors ProformaInvoiceService, with the additions that make
    this document what it is:

    - It references MANY proforma invoices (many-to-many). Goods are
      prefilled from those PIs (build_prefill_from_proformas) then edited.
    - Tax is computed per-product: each line snapshots its product's IGST %,
      the amounts are summed, and charged (or not) per tax_mode - "Supply
      meant for" is either "With Payment of IGST" or "Without Payment of
      IGST under LUT" (zero-rated) (see ExportInvoice.tax_total_inr /
      igst_amount_inr).
    - The exchange rate is typed in manually; once a value is set only an
      admin may change it (enforced in update()).
    - EPCG number/date, the "export under" text and the supplier
      GSTIN/invoice-no purchase-detail rows are imported (when present) by
      walking each linked PI -> its purchase orders -> their purchase
      invoices; the exemption rows come from POs whose purchase_type is
      'exemption'.
    - An optional Shipping Bill PDF is stored the same way a Purchase
      Invoice stores the supplier's PDF (_save_pdf/_delete_pdf_file).
    - Saving one also generates its EXPORT PACKING LIST (one per invoice,
      see ExportPackingListService) from the container split submitted with
      the form - the split is validated BEFORE anything is written, so a
      split that doesn't balance never leaves a half-saved invoice behind.
    - No draft/confirmed lock (always editable), but admin version history is
      kept via DocumentVersionService."""

    def __init__(self, export_invoice_repo: ExportInvoiceRepository, product_repo: ProductRepository,
                 lead_repo: LeadRepositoryBase, proforma_invoice_repo: ProformaInvoiceRepository,
                 purchase_order_repo: PurchaseOrderRepository, purchase_invoice_repo: PurchaseInvoiceRepository,
                 company_repo: CompanyRepository, version_service: "DocumentVersionService",
                 party_repos: Optional[dict] = None, upload_folder: str = "",
                 allowed_extensions: set = frozenset(),
                 export_packing_list_service: Optional["ExportPackingListService"] = None,
                 misc_list_service: Optional["MiscListService"] = None):
        self.export_invoice_repo = export_invoice_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.purchase_order_repo = purchase_order_repo
        self.purchase_invoice_repo = purchase_invoice_repo
        self.company_repo = company_repo
        self.version_service = version_service
        self.party_repos = party_repos
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions
        # Optional so the service stays constructible on its own in tests;
        # when wired, every save also regenerates the invoice's packing list.
        self.export_packing_list_service = export_packing_list_service
        # Resolves the picked currency name to its symbol (Administration ->
        # Miscellaneous); also optional, so an unwired service just keeps the
        # submitted name and no symbol.
        self.misc_list_service = misc_list_service

    # ---- reads --------------------------------------------------
    def get(self, invoice_id: int, company_id: int) -> ExportInvoice:
        invoice = self.export_invoice_repo.get_by_id(invoice_id)
        if not invoice or invoice.company_id != company_id:
            raise NotFoundError(f"Export invoice #{invoice_id} not found.")
        return invoice

    def list_all(self, company_id: int) -> List[ExportInvoice]:
        return self.export_invoice_repo.list_all(company_id)

    def list_for_proforma(self, proforma_invoice_id: int, company_id: int) -> List[ExportInvoice]:
        return self.export_invoice_repo.list_for_proforma(proforma_invoice_id, company_id)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, invoice: ExportInvoice, current_user: User):
        if current_user.is_admin:
            return
        if invoice.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage export invoices you created yourself.")

    # ---- number validation --------------------------------------------------
    def _clean_export_invoice_number(self, company_id: int, raw: str, exclude_id: Optional[int] = None) -> str:
        """Unlike the other documents in this pipeline, the export invoice
        number is typed in by hand (it must match the number on the physical
        customs paperwork), not auto-generated. Free text, up to 16 characters."""
        number = (raw or "").strip()
        if not number:
            raise ValidationError("Export invoice number is compulsory.")
        if len(number) > 16:
            raise ValidationError("Export invoice number must be at most 16 characters.")
        if self.export_invoice_repo.number_exists(company_id, number, exclude_id):
            raise ValidationError(f"Export invoice number '{number}' is already in use.")
        return number

    # ---- prefill from selected proforma invoices --------------------------------------------------
    def _load_proformas(self, proforma_ids: list, company_id: int) -> List[ProformaInvoice]:
        """Load only the proforma invoices that belong to this company - a
        crafted id in the request can never pull another company's PI in."""
        result = []
        for pid in dict.fromkeys(proforma_ids or []):
            try:
                pi = self.proforma_invoice_repo.get_by_id(int(pid))
            except (TypeError, ValueError):
                continue
            if pi and pi.company_id == company_id:
                result.append(pi)
        return result

    def build_prefill_from_proformas(self, proforma_ids: list, company_id: int) -> dict:
        """Merge the selected PIs' goods lines, sum their sea freight /
        insurance / certification / other charges / discount, take Nature of
        contract from the first PI's Terms of delivery, and import EPCG /
        export-under / supplier-exemption rows by walking each PI -> its
        purchase orders -> their purchase invoices. Returns a dict the
        form/route consumes."""
        proformas = self._load_proformas(proforma_ids, company_id)
        first = proformas[0] if proformas else None

        # Goods: every line from every selected PI, in selection order.
        #
        # The two documents hold their prices differently, so the price is
        # converted rather than copied: a proforma invoice's price_usd is the
        # absolute FOB price and its CIF value is that goods total PLUS the
        # charges (ProformaInvoice.cif_value_usd), while an export invoice's
        # price_usd is the CIF price and its ladder runs the other way, down
        # from the goods total. So each PI's own charges are spread over its
        # own lines' total qty. That is exactly what ProformaInvoice.printed_items
        # already works out for the PI's own printed Rate column, so the
        # export invoice is prefilled straight from those - the two documents
        # then quote a buyer the identical rate down to the cent, instead of
        # the same figure computed twice. A PI with no charges (FOB terms,
        # say) carries its prices over untouched.
        items = []
        for pi in proformas:
            for it in pi.printed_items:
                items.append({
                    "product_id": it.product_id, "product_name": it.product_name,
                    "hsn_code": it.hsn_code, "surface": it.surface, "pallets": it.pallets,
                    "quantity_boxes": it.quantity_boxes, "quantity_value": it.quantity_value,
                    "unit": it.unit, "price_usd": it.price_usd,
                    "igst_percent": self._product_igst_percent(it.product_id, company_id),
                })

        # Buyer Order No & Date: every PI under one export invoice shares the
        # same buyer order, so it's a single field taken from the first
        # selected PI that has one, not a per-PI list.
        buyer_order_pi = next((pi for pi in proformas if pi.buyer_order_no), None)
        buyer_order_no = buyer_order_pi.buyer_order_no if buyer_order_pi else None
        buyer_order_date = buyer_order_pi.invoice_date if buyer_order_pi else None

        # Charges: every selected PI contributes its own sea freight /
        # insurance / certification / other charges / discount, so these
        # import as the SUM across all of them, not a single PI's value.
        sea_freight = sum(pi.sea_freight or 0 for pi in proformas)
        insurance = sum(pi.insurance or 0 for pi in proformas)
        certification = sum(pi.certification or 0 for pi in proformas)
        other_charges = sum(pi.other_charges or 0 for pi in proformas)
        discount_amount = sum(pi.discount_amount or 0 for pi in proformas)

        # Walk the chain for EPCG / export-under / exemption purchase details.
        epcg_number = epcg_date = None
        purchase_details = []
        seen_pd = set()
        for pi in proformas:
            for po in self.purchase_order_repo.list_for_proforma(pi.id):
                if po.company_id != company_id:
                    continue
                pinvs = [p for p in self.purchase_invoice_repo.list_for_purchase_order(po.id)
                         if p.company_id == company_id]
                for pinv in pinvs:
                    if not epcg_number and pinv.epcg_number:
                        epcg_number, epcg_date = pinv.epcg_number, pinv.epcg_date
                # Exemption purchases contribute a supplier GSTIN + invoice-no row.
                if getattr(po, "purchase_type", "") == "exemption":
                    if pinvs:
                        for pinv in pinvs:
                            key = ((pinv.seller_gstin or po.seller_gstin or ""), (pinv.invoice_number or ""))
                            if key not in seen_pd:
                                seen_pd.add(key)
                                purchase_details.append({
                                    "supplier_gstin": pinv.seller_gstin or po.seller_gstin,
                                    "supplier_invoice_no": pinv.invoice_number,
                                })
                    elif po.seller_gstin:
                        key = (po.seller_gstin, "")
                        if key not in seen_pd:
                            seen_pd.add(key)
                            purchase_details.append({"supplier_gstin": po.seller_gstin, "supplier_invoice_no": None})

        # The government-scheme line of the Export Under block: the company's
        # government schemes, the same text the Annexure's section 13 defaults
        # to. Editable afterwards, and blanking it just means "use whatever
        # Our Company says today". The rest of that block - the SUPPLY MEANT
        # FOR EXPORT heading, the EPCG licence, the LUT number - is derived by
        # the sheets from tax_mode/epcg_number/the company, never baked in.
        company = self.company_repo.get(company_id)
        export_under = (company.government_schemes if company else None) or None

        fields = {
            "proforma_invoice_ids": [pi.id for pi in proformas],
            "lead_id": first.lead_id if first else None,
            "consignee_name": first.consignee_name if first else None,
            "consignee_address": first.consignee_address if first else None,
            "notify_name": first.notify_name if first else None,
            "notify_address": first.notify_address if first else None,
            "country_of_origin": first.country_of_origin if first else "INDIA",
            "country_of_destination": first.country_of_destination if first else None,
            "port_of_loading": first.port_of_loading if first else None,
            "port_of_discharge": first.port_of_discharge if first else None,
            "final_destination": first.final_destination if first else None,
            "nature_of_contract": first.terms_of_delivery if first else None,
            "payment_terms": first.payment_terms if first else None,
            # Same currency as the proforma invoices being exported under.
            "currency_code": first.currency_code if first else None,
            "buyer_order_no": buyer_order_no,
            "buyer_order_date": buyer_order_date,
            "sea_freight": sea_freight,
            "insurance": insurance,
            "certification": certification,
            "other_charges": other_charges,
            "discount_amount": discount_amount,
            "bank_name": first.bank_name if first else None,
            "bank_account_number": first.bank_account_number if first else None,
            "bank_ifsc_code": first.bank_ifsc_code if first else None,
            "remarks": first.remarks if first else None,
            "bank_swift_code": first.bank_swift_code if first else None,
            "bank_branch": first.bank_branch if first else None,
            "bank_address": first.bank_address if first else None,
            "export_under": export_under,
            "epcg_number": epcg_number,
            "epcg_date": epcg_date,
            "self_sealing_declaration": company.self_sealing_declaration if company else None,
        }
        return {"fields": fields, "items": items, "purchase_details": purchase_details}

    def _product_igst_percent(self, product_id, company_id: int) -> float:
        if not product_id:
            return 0.0
        product = self.product_repo.get_by_id(int(product_id))
        if not product or product.company_id != company_id:
            return 0.0
        return product.igst_percent or 0.0

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[ExportInvoiceItem]:
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

            # Snapshot the product's IGST % (and re-derive qty from boxes)
            # only for a product that belongs to this same company. The
            # Boxes column's unit (printed as small text after the number)
            # is likewise always the product's own Quantity unit.
            igst_percent = 0.0
            quantity_unit = "PCS"
            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None
                else:
                    igst_percent = product.igst_percent or 0.0
                    quantity_unit = product.quantity_unit or "PCS"
                    if quantity_boxes and product.alternate_quantity:
                        try:
                            quantity_value = round(quantity_boxes * float(product.alternate_quantity), 2)
                        except ValueError:
                            pass

            if quantity_value <= 0:
                raise ValidationError(f"Row {i} ('{product_name}'): quantity is compulsory and must be greater than zero.")
            if price_usd < 0:
                raise ValidationError(f"Row {i} ('{product_name}'): price can't be negative.")
            items.append(ExportInvoiceItem(
                id=None, export_invoice_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                surface=(raw.get("surface") or "").strip() or None,
                pallets=pallets, quantity_boxes=quantity_boxes, quantity_unit=quantity_unit, quantity_value=quantity_value,
                unit=(raw.get("unit") or "SQM").strip() or "SQM",
                price_usd=price_usd, total_usd=round(quantity_value * price_usd, 2),
                igst_percent=igst_percent,
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    def _build_header(self, current_user: User, fields: dict, items: List[ExportInvoiceItem],
                       invoice_id: Optional[int] = None) -> ExportInvoice:
        export_invoice_number = self._clean_export_invoice_number(
            current_user.company_id, fields.get("export_invoice_number"), exclude_id=invoice_id,
        )
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

        # See the quotation builder: FOB drops the freight, the insurance and
        # the certification; CFR drops the insurance only.
        nature_of_contract = (fields.get("nature_of_contract") or "").strip() or None
        no_sea_freight = drops_sea_freight(nature_of_contract)
        no_insurance = drops_insurance(nature_of_contract)
        # "Calculate CIF pricing" only means something when there's a CIF
        # figure to build - under FOB terms there's no CIF to show at all
        # (see is_fob in the printed sheet/form), so the button is hidden
        # there and this ignores it even if a stale client/API call posts it
        # anyway. Same trust boundary as the charge zeroing above.
        no_fob_pricing = is_fob_terms(nature_of_contract)

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        # Computed early (rather than where it's assigned onto the invoice
        # below) so the linked PIs' currency is known before the currency
        # resolution just below needs it.
        proforma_ids = self._clean_proforma_ids(fields.get("proforma_invoice_ids"), current_user.company_id)

        tax_mode = fields.get("tax_mode") if fields.get("tax_mode") in dict(EXPORT_TAX_MODES) else EXPORT_TAX_MODE_IGST
        loading_type = fields.get("loading_type") if fields.get("loading_type") in dict(EXPORT_LOADING_TYPES) else EXPORT_LOADING_SELF_SEALING

        # Authorised Signatory: the form is just a dropdown of the company's
        # contact-person NAMES; the designation printed beneath it is looked
        # up here so it always matches the current Our-Company profile.
        authorised_name = (fields.get("authorised_person_name") or "").strip() or None
        authorised_desig = (fields.get("authorised_person_designation") or "").strip() or None
        if authorised_name and not authorised_desig:
            company = self.company_repo.get(current_user.company_id)
            if company:
                match = next((p for p in company.contact_persons
                              if (p.get("name") or "").strip() == authorised_name), None)
                if match:
                    authorised_desig = (match.get("designation") or "").strip() or None

        # Currency: the form posts a name off the Miscellaneous currency
        # list; both name and symbol are snapshotted so editing that list
        # later can't change what an already-issued invoice printed. Once
        # linked to a proforma invoice, the currency is inherited from it
        # instead - the same document is invoiced in what it was quoted in,
        # and the form disables the field once linked, so this also guards a
        # tampered POST.
        currency_source = fields.get("currency_code")
        if proforma_ids:
            linked_pi = self.proforma_invoice_repo.get_by_id(proforma_ids[0])
            if linked_pi:
                currency_source = linked_pi.currency_code
        currency_name, currency_symbol = (
            self.misc_list_service.resolve_currency(current_user.company_id, currency_source)
            if self.misc_list_service else ((currency_source or "").strip() or None, None)
        )

        invoice = ExportInvoice(
            id=None, company_id=current_user.company_id, export_invoice_number=export_invoice_number,
            invoice_date=invoice_date,
            consignee_name=consignee_name, created_by=current_user.id, lead_id=lead_id,
            consignee_address=(fields.get("consignee_address") or "").strip() or None,
            notify_name=(fields.get("notify_name") or "").strip() or None,
            notify_address=(fields.get("notify_address") or "").strip() or None,
            country_of_origin=(fields.get("country_of_origin") or "").strip() or "INDIA",
            country_of_destination=(fields.get("country_of_destination") or "").strip() or None,
            place_of_receipt=(fields.get("place_of_receipt") or "").strip() or None,
            pre_carriage_by=(fields.get("pre_carriage_by") or "").strip() or None,
            port_of_loading=(fields.get("port_of_loading") or "").strip() or None,
            port_of_discharge=(fields.get("port_of_discharge") or "").strip() or None,
            final_destination=(fields.get("final_destination") or "").strip() or None,
            nature_of_contract=nature_of_contract,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            buyer_order_no=(fields.get("buyer_order_no") or "").strip() or None,
            buyer_order_date=(fields.get("buyer_order_date") or "").strip() or None,
            export_under=(fields.get("export_under") or "").strip() or None,
            epcg_number=(fields.get("epcg_number") or "").strip() or None,
            epcg_date=(fields.get("epcg_date") or "").strip() or None,
            loading_type=loading_type, tax_mode=tax_mode,
            exchange_rate=_float("exchange_rate", 0),
            sea_freight=0 if no_sea_freight else _float("sea_freight", 0),
            insurance=0 if no_insurance else _float("insurance", 0),
            certification=0 if fields.get("no_certification") else _float("certification", 0),
            other_charges=_float("other_charges", 0),
            discount_amount=_float("discount_amount", 0),
            fob_pricing=False if no_fob_pricing else is_fob_terms(fields),
            bank_name=(fields.get("bank_name") or "").strip() or None,
            bank_account_number=(fields.get("bank_account_number") or "").strip() or None,
            bank_ifsc_code=(fields.get("bank_ifsc_code") or "").strip() or None,
            bank_swift_code=(fields.get("bank_swift_code") or "").strip() or None,
            bank_branch=(fields.get("bank_branch") or "").strip() or None,
            bank_address=(fields.get("bank_address") or "").strip() or None,
            authorised_person_name=authorised_name,
            authorised_person_designation=authorised_desig,
            self_sealing_declaration=(fields.get("self_sealing_declaration") or "").strip() or None,
            examination_date=(fields.get("examination_date") or "").strip() or None,
            location_code_08b=(fields.get("location_code_08b") or "").strip() or None,
            booking_no=(fields.get("booking_no") or "").strip() or None,
            vessel_voyage_no=(fields.get("vessel_voyage_no") or "").strip() or None,
            issuing_authority=(fields.get("issuing_authority") or "").strip() or None,
            issuing_authority_address=(fields.get("issuing_authority_address") or "").strip() or None,
            permission_no=(fields.get("permission_no") or "").strip() or None,
            permission_date=(fields.get("permission_date") or "").strip() or None,
            permission_expiry=(fields.get("permission_expiry") or "").strip() or None,
            permission_is_one_time=(fields.get("permission_is_one_time") or "").strip() in ("1", "true", "on"),
            manufacturer_name=(fields.get("manufacturer_name") or "").strip() or None,
            manufacturer_address=(fields.get("manufacturer_address") or "").strip() or None,
            stuffing_location=(fields.get("stuffing_location") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            total_net_weight_kg=_float("total_net_weight_kg", None),
            total_gross_weight_kg=_float("total_gross_weight_kg", None),
            shipping_bill_no=(fields.get("shipping_bill_no") or "").strip() or None,
            shipping_bill_date=(fields.get("shipping_bill_date") or "").strip() or None,
            currency_code=currency_name,
            currency_symbol=currency_symbol,
            items=items,
        )
        invoice.proforma_invoice_ids = proforma_ids
        invoice.containers = self._clean_containers(fields.get("containers"))
        invoice.container_details = self._clean_container_details(fields.get("container_details_list"))
        invoice.purchase_details = self._clean_purchase_details(fields.get("purchase_details"))
        return invoice

    def _clean_proforma_ids(self, raw_ids, company_id: int) -> List[int]:
        """One export invoice covers a single buyer, so every selected PI
        must share the same buyer - its lead_id when linked to one, else its
        consignee name. The form already restricts the picker to one buyer
        at a time; this is the server-side backstop."""
        pis = []
        for pid in dict.fromkeys(raw_ids or []):
            try:
                pi = self.proforma_invoice_repo.get_by_id(int(pid))
            except (TypeError, ValueError):
                continue
            if pi and pi.company_id == company_id:
                pis.append(pi)
        if pis:
            keys = {pi.lead_id or (pi.consignee_name or "").strip().lower() for pi in pis}
            if len(keys) > 1:
                raise ValidationError("All selected proforma invoices must belong to the same buyer.")
        return [pi.id for pi in pis]

    @staticmethod
    def _clean_containers(raw) -> List[dict]:
        rows = []
        for r in raw or []:
            ctype = (r.get("container_type") or "").strip()
            try:
                count = int(r.get("container_count") or 0)
            except (TypeError, ValueError):
                count = 0
            if not ctype and count <= 0:
                continue
            rows.append({"container_type": ctype, "container_count": max(count, 0)})
        return rows

    @staticmethod
    def _clean_container_details(raw) -> List[dict]:
        # CARRIED_CONTAINER_FIELDS have no input on the export invoice form
        # (unlike tare_weight_kg and the other typed fields above), so they
        # default to None here and update() carries the stored values forward
        # by row position - see the comment on that constant.
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None
                      for k in ("container_no", "line_seal_no", "rfid_seal_no", "vehicle_no",
                                "lr_no", "transporter_name", "max_permitted_weight")}
            tare_raw = (r.get("tare_weight_kg") or "").strip()
            if tare_raw:
                try:
                    values["tare_weight_kg"] = float(tare_raw)
                except ValueError:
                    raise ValidationError("Container details: tare weight must be a number.")
            else:
                values["tare_weight_kg"] = None
            for key in ExportInvoiceService.CARRIED_CONTAINER_FIELDS:
                values[key] = None
            if any(v is not None for v in values.values()):
                rows.append(values)
        return rows

    @staticmethod
    def _clean_purchase_details(raw) -> List[dict]:
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None
                      for k in ("supplier_gstin", "supplier_invoice_no")}
            if any(values.values()):
                rows.append(values)
        return rows

    # ---- shipping bill PDF storage --------------------------------------------------
    def _save_pdf(self, file_storage) -> Optional[str]:
        if not file_storage or not file_storage.filename:
            return None
        filename = secure_filename(file_storage.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in self.allowed_extensions:
            raise ValidationError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(self.allowed_extensions))}.")
        os.makedirs(self.upload_folder, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{filename}"
        file_storage.save(os.path.join(self.upload_folder, stored_name))
        return f"uploads/export_invoices/{stored_name}"

    def _delete_pdf_file(self, relative_path: Optional[str]) -> None:
        if not relative_path:
            return
        full_path = os.path.join(self.upload_folder, os.path.basename(relative_path))
        if os.path.exists(full_path):
            os.remove(full_path)

    # ---- export packing list --------------------------------------------------
    @staticmethod
    def _remap_allocations(fields: dict, raw_items: list) -> list:
        """The form's container-split rows point at goods lines by their
        position in the PRODUCTS table, but _build_items silently drops rows
        with a blank product name - so the two lists don't line up 1:1.
        Rewrite each allocation's index into the built-items list, dropping
        any that pointed at a row that was skipped."""
        kept = {}
        for i, raw in enumerate(raw_items or []):
            if (raw.get("product_name") or "").strip():
                kept[i] = len(kept)
        remapped = []
        for alloc in fields.get("packing_allocations") or []:
            try:
                form_index = int(alloc.get("invoice_item_index"))
            except (TypeError, ValueError):
                continue
            if form_index in kept:
                remapped.append({**alloc, "invoice_item_index": kept[form_index]})
        return remapped

    def _build_packing_items(self, fields: dict, raw_items: list, invoice: ExportInvoice):
        """Validate the container split against the goods lines. Returns the
        packing-list items to persist once the invoice is written, or None
        when no packing-list service is wired. Raises ValidationError - it is
        called before the invoice is saved, on purpose."""
        if not self.export_packing_list_service:
            return None
        return self.export_packing_list_service.build_items(
            invoice.company_id, invoice.items, self._remap_allocations(fields, raw_items),
            invoice.container_details,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list, pdf_file=None) -> ExportInvoice:
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        apply_fob_uplift(invoice)
        # Examination date defaults to the creation date, not a later edit.
        if not invoice.examination_date:
            invoice.examination_date = invoice.invoice_date
        packing_items = self._build_packing_items(fields, raw_items, invoice)
        invoice.shipping_bill_pdf_path = self._save_pdf(pdf_file)
        created = self.export_invoice_repo.create(invoice)
        self.version_service.record("export_invoice", created, current_user.id)
        if packing_items is not None:
            self.export_packing_list_service.save_for_invoice(current_user, created, packing_items)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, created.lead_id, "export_invoice")
        return created

    def update(self, current_user: User, invoice_id: int, fields: dict, raw_items: list,
               pdf_file=None, remove_pdf: bool = False) -> ExportInvoice:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items, invoice_id=invoice_id)
        apply_fob_uplift(invoice)

        # None of CARRIED_CONTAINER_FIELDS is editable from this form - carry
        # the stored values forward by row position so editing anything else
        # doesn't wipe them. Saving this form rewrites the 11B rows wholesale,
        # so without this they would be lost.
        for i, cd in enumerate(invoice.container_details):
            if i < len(existing.container_details):
                for key in self.CARRIED_CONTAINER_FIELDS:
                    cd[key] = existing.container_details[i].get(key)

        # Exchange rate is set once by anyone; changing it later is admin-only.
        # (The form disables the field for non-admins, so a normal edit
        # doesn't even submit it - default back to the stored value then.)
        if existing.exchange_rate and not current_user.is_admin:
            if invoice.exchange_rate not in (0, existing.exchange_rate):
                raise PermissionDeniedError("Only an admin can change the exchange rate once it has been set.")
            invoice.exchange_rate = existing.exchange_rate

        # Examination date is fixed at creation - keep the stored value.
        invoice.examination_date = invoice.examination_date or existing.examination_date

        packing_items = self._build_packing_items(fields, raw_items, invoice)

        if pdf_file and pdf_file.filename:
            invoice.shipping_bill_pdf_path = self._save_pdf(pdf_file)
            self._delete_pdf_file(existing.shipping_bill_pdf_path)
        elif remove_pdf:
            self._delete_pdf_file(existing.shipping_bill_pdf_path)
            invoice.shipping_bill_pdf_path = None
        else:
            invoice.shipping_bill_pdf_path = existing.shipping_bill_pdf_path

        self.export_invoice_repo.update(invoice_id, invoice)
        updated = self.get(invoice_id, current_user.company_id)
        self.version_service.record("export_invoice", updated, current_user.id)
        if packing_items is not None:
            self.export_packing_list_service.save_for_invoice(current_user, updated, packing_items)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo, updated.lead_id, "export_invoice")
        return updated

    def update_tax_invoice_details(self, current_user: User, invoice_id: int, fields: dict) -> ExportInvoice:
        """Everything the Tax Invoice attachment owns: its own number and
        date, and the e-way bill number and date (which appear on that sheet
        and nowhere else, so they are asked for there rather than on the
        export invoice form). Every other field on the sheet derives from this
        invoice, so its form asks for nothing more.

        All four are optional. A blank tax invoice number/date falls back to
        this invoice's own (see ExportInvoice.tax_invoice_*_printed), which is
        how a tax invoice starts out.

        Unlike the export invoice number the tax invoice number is not checked
        for uniqueness: it is a reference typed to match the physical
        paperwork, and nothing looks an invoice up by it."""
        # Ownership first, so a too-long number posted at another company's
        # invoice still 404s rather than answering with a validation message.
        self._assert_can_modify(self.get(invoice_id, current_user.company_id), current_user)
        if len((fields.get("tax_invoice_number") or "").strip()) > 16:
            raise ValidationError("Tax invoice number must be at most 16 characters.")
        return self._update_document_fields(
            current_user, invoice_id, fields, ExportInvoiceRepository.TAX_INVOICE_FIELDS)

    def _update_document_fields(self, current_user: User, invoice_id: int, fields: dict,
                                names) -> ExportInvoice:
        """Shared by the attachments that own a handful of columns on the
        export invoice each (tax invoice, VGM declaration, commercial packing
        list): check ownership, blank-to-NULL, and write only `names`."""
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        cleaned = {name: (fields.get(name) or "").strip() or None for name in names}
        self.export_invoice_repo.update_document_fields(invoice_id, cleaned, names)
        return self.get(invoice_id, current_user.company_id)

    def update_packing_list_details(self, current_user: User, invoice_id: int,
                                    fields: dict) -> ExportInvoice:
        """The commercial invoice packing list's bill of lading number and
        date - the only two cells on that sheet that aren't derived."""
        return self._update_document_fields(
            current_user, invoice_id, fields, ExportInvoiceRepository.PACKING_LIST_FIELDS)

    # 11B columns with no input on the export invoice form: gross/net weight
    # are set elsewhere, and the rest are typed on the per-container documents
    # (VGM attachment, E-Seal sheet). Saving the export invoice rewrites its
    # 11B rows wholesale, so _clean_container_details leaves these None and
    # update() carries the stored values forward by row position. One list, so
    # the two halves of that rule can never drift apart.
    CARRIED_CONTAINER_FIELDS = (
        "gross_weight", "net_weight",
        "weighbridge_name", "weighing_slip_no",
        "sealing_time", "sealing_date",
    )
    # Which of them each per-container document owns.
    VGM_CONTAINER_FIELDS = ("weighbridge_name", "weighing_slip_no")
    ESEAL_CONTAINER_FIELDS = ("sealing_time", "sealing_date")

    def _update_container_fields(self, current_user: User, invoice_id: int,
                                 rows: List[dict], fields) -> ExportInvoice:
        """Shared by the per-container documents: validate ownership, keep
        only rows naming a usable sr_no, and write just `fields`."""
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        cleaned = []
        for row in rows or []:
            try:
                sr_no = int(row.get("sr_no"))
            except (TypeError, ValueError):
                continue
            entry = {"sr_no": sr_no}
            entry.update({name: (row.get(name) or "").strip() or None for name in fields})
            cleaned.append(entry)
        self.export_invoice_repo.update_container_detail_fields(invoice_id, cleaned, fields)
        return self.get(invoice_id, current_user.company_id)

    @staticmethod
    def _normalise_sealing_time(value: Optional[str]) -> Optional[str]:
        """The E-Seal sheet is always 24-hour. Accept the forms people
        actually type (9:5, 09.05, 0905) and store the padded HH:mm; reject
        anything that is not a time rather than printing it as typed, since a
        customs form carrying '25:99' is worse than a rejected save."""
        raw = (value or "").strip()
        if not raw:
            return None
        text = raw.replace(".", ":").replace(" ", "")
        if ":" not in text and text.isdigit() and len(text) in (3, 4):
            text = f"{text[:-2]}:{text[-2:]}"
        parts = text.split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            hours, minutes = int(parts[0]), int(parts[1])
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                return f"{hours:02d}:{minutes:02d}"
        raise ValidationError(f"Sealing time '{raw}' is not a 24-hour time (HH:mm).")

    def update_eseal_details(self, current_user: User, invoice_id: int, rows: List[dict]) -> ExportInvoice:
        """The E-Seal sheet's sealing time and date, one pair per physical
        container. Every other cell on that sheet is derived."""
        # Ownership first: a malformed time posted at another company's
        # invoice must still 404 rather than answer with a validation message.
        self._assert_can_modify(self.get(invoice_id, current_user.company_id), current_user)
        normalised = [dict(row, sealing_time=self._normalise_sealing_time(row.get("sealing_time")))
                      for row in rows or []]
        return self._update_container_fields(
            current_user, invoice_id, normalised, self.ESEAL_CONTAINER_FIELDS)

    def update_vgm_declaration(self, current_user: User, invoice_id: int, fields: dict) -> ExportInvoice:
        """The VGM declaration's manual-entry cells. Every other cell on that
        sheet is derived, so these are all its form asks for. All optional:
        blank falls back to the default (see the ExportInvoice.vgm_* helpers),
        which is what an invoice nobody has edited prints."""
        return self._update_document_fields(
            current_user, invoice_id, fields, ExportInvoiceRepository.VGM_DECLARATION_FIELDS)

    def update_vgm_details(self, current_user: User, invoice_id: int, rows: List[dict]) -> ExportInvoice:
        """The VGM attachment's weighbridge name/address and weighing slip
        number, one pair per physical container. Every other cell on that
        sheet is derived, so these two are all it asks for."""
        return self._update_container_fields(
            current_user, invoice_id, rows, self.VGM_CONTAINER_FIELDS)

    def delete(self, current_user: User, invoice_id: int) -> None:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self._delete_pdf_file(existing.shipping_bill_pdf_path)
        self.export_invoice_repo.delete(invoice_id)


# ============================================================
# EXPORT PACKING LIST SERVICE
# ============================================================
# How many boxes may be out before the split is called unbalanced. Boxes are
# whole things, but the form's inputs allow decimals (a part-pallet line can
# legitimately read 0.5), so compare with a float tolerance rather than ==.
_BOX_TOLERANCE = 0.001


class ExportPackingListService:
    """The EXPORT PACKING LIST: exactly one per Export Invoice, generated
    automatically every time that invoice is saved and never created or
    edited on its own (there is no create/update route - see
    app/routes/export_packing_lists.py, which is read-only).

    All it owns is the SPLIT: how the invoice's goods lines were divided
    across the physical containers. Everything else the sheet prints comes
    off the parent invoice.

    Three things are computed rather than typed:

    - **The quantities.** A row says only "N boxes of goods line X went into
      container C". SQM/LM, pallets and net/gross weight all follow from N -
      pallets and quantity pro-rata from the invoice line itself (so the
      parts always add back up to the whole), weights from the catalog
      product's per-box figures. A submitted value overrides its derived one,
      for the odd container that really was weighed differently.

    - **The balance.** Per goods line, the boxes allocated across every
      container must add up to EXACTLY the boxes on the export invoice:
      over-allocating loads the same box twice, under-allocating leaves boxes
      on the floor, and both are refused with a message naming the line and
      the shortfall. This is checked BEFORE the parent invoice is written, so
      a bad split never half-saves.

    - **The grouping.** Rows carry the HSN heading they print under
      (`group_label`, derived from the line's catalog category, else its
      product name). Within each container the rows are re-ordered so one
      HSN group runs together, and containers are ordered so a group that
      spilled over the end of one container resumes at the top of the next -
      which is what lets the sheet print one heading per group instead of one
      per row.

    No admin version history: the list holds no independently-authored data
    (it is regenerated from its invoice on every save), so the parent export
    invoice's own history is the record."""

    def __init__(self, export_packing_list_repo: ExportPackingListRepository,
                 export_invoice_repo: ExportInvoiceRepository, product_repo: ProductRepository,
                 category_repo: Optional[CategoryRepository] = None):
        self.export_packing_list_repo = export_packing_list_repo
        self.export_invoice_repo = export_invoice_repo
        self.product_repo = product_repo
        self.category_repo = category_repo

    # ---- reads --------------------------------------------------
    def get(self, packing_list_id: int, company_id: int) -> ExportPackingList:
        packing_list = self.export_packing_list_repo.get_by_id(packing_list_id)
        if not packing_list or packing_list.company_id != company_id:
            # 404, not 403 - don't reveal that another company's list exists.
            raise NotFoundError(f"Export packing list #{packing_list_id} not found.")
        return packing_list

    def list_all(self, company_id: int) -> List[ExportPackingList]:
        return self.export_packing_list_repo.list_all(company_id)

    def get_for_invoice(self, export_invoice_id: int, company_id: int) -> Optional[ExportPackingList]:
        packing_list = self.export_packing_list_repo.get_for_invoice(export_invoice_id)
        if not packing_list or packing_list.company_id != company_id:
            return None
        return packing_list

    # ---- derived per-row figures --------------------------------------------------
    def _product(self, product_id, company_id: int) -> Optional[Product]:
        if not product_id:
            return None
        try:
            product = self.product_repo.get_by_id(int(product_id))
        except (TypeError, ValueError):
            return None
        return product if product and product.company_id == company_id else None

    def group_label_for(self, product, fallback_name: str) -> str:
        """The HSN heading a goods line prints under. A catalog product sits
        inside a category ("GLAZED VIRTIFIED TILES"), and that category is
        what the paper form names above the sizes underneath it - so use it
        when there is one, and fall back to the line's own name when the
        product sits at the catalog root or isn't a catalog product at all.
        Always overridable per row on the form, because the wording on the
        customs paperwork is not always the catalog's wording."""
        if product and product.category_id and self.category_repo:
            category = self.category_repo.get_by_id(product.category_id)
            if category and category.company_id == product.company_id and category.name:
                return category.name.strip().upper()
        return (fallback_name or "").strip().upper()

    @staticmethod
    def _per_box(total, boxes) -> Optional[float]:
        """A goods line's per-box figure, taken from the line itself rather
        than the catalog, so that splitting a line into containers always
        adds back up to the line - even when the line was hand-edited away
        from the catalog spec."""
        try:
            total, boxes = float(total or 0), float(boxes or 0)
        except (TypeError, ValueError):
            return None
        return (total / boxes) if boxes else None

    # ---- building the allocation --------------------------------------------------
    def default_allocations(self, items: List[ExportInvoiceItem]) -> List[dict]:
        """The split an invoice gets when nobody has typed one: every goods
        line whole, in the first container. That is what "generated by
        default" means here - saving an export invoice always produces a
        printable packing list, and splitting it across containers is a
        refinement the user makes afterwards."""
        return [
            {"container_index": 0, "invoice_item_index": i, "quantity_boxes": item.quantity_boxes}
            for i, item in enumerate(items)
        ]

    def build_items(self, company_id: int, items: List[ExportInvoiceItem], raw_allocations,
                    container_details: List[dict]) -> List[ExportPackingListItem]:
        """Turn the form's allocation rows into packing-list items, deriving
        every quantity from the boxes and refusing a split that doesn't
        balance. Raises ValidationError; call it BEFORE persisting the parent
        invoice."""
        allocations = self._clean_allocations(raw_allocations, len(items))
        if not allocations:
            allocations = self.default_allocations(items)
        self._assert_balanced(items, allocations)

        built = []
        for alloc in allocations:
            item = items[alloc["invoice_item_index"]]
            product = self._product(item.product_id, company_id)
            boxes = alloc.get("quantity_boxes")
            boxes = None if boxes in (None, "") else float(boxes)

            # Quantity + pallets pro-rata off the invoice line; weights off
            # the catalog product's per-box figures. A submitted value always
            # wins, so an odd container can be corrected by hand.
            qty_per_box = self._per_box(item.quantity_value, item.quantity_boxes)
            pallets_per_box = self._per_box(item.pallets, item.quantity_boxes)
            if boxes is None:
                # A goods line with no box count can't be split by boxes -
                # _assert_balanced has already limited it to a single row, so
                # this one row simply carries the whole line.
                quantity_value = item.quantity_value or 0
                pallets = item.pallets
            else:
                quantity_value = round(boxes * qty_per_box, 2) if qty_per_box is not None else 0
                pallets = round(boxes * pallets_per_box, 2) if pallets_per_box is not None else None

            net = self._override(alloc.get("net_weight_kg"))
            gross = self._override(alloc.get("gross_weight_kg"))
            if net is None and boxes is not None and product and product.net_weight_kg:
                net = round(boxes * product.net_weight_kg, 2)
            if gross is None and boxes is not None and product and product.gross_weight_kg:
                gross = round(boxes * product.gross_weight_kg, 2)

            container = self._container_at(container_details, alloc["container_index"])
            built.append(ExportPackingListItem(
                id=None, export_packing_list_id=None, sr_no=0,
                container_sr_no=alloc["container_index"] + 1,
                container_no=container.get("container_no"),
                seal_no=container.get("line_seal_no"),
                rfid_seal_no=container.get("rfid_seal_no"),
                invoice_item_sr_no=item.sr_no,
                product_id=item.product_id, product_name=item.product_name,
                group_label=(alloc.get("group_label") or "").strip().upper()
                            or self.group_label_for(product, item.product_name),
                hsn_code=item.hsn_code,
                pallets=self._override(alloc.get("pallets"), default=pallets),
                quantity_boxes=boxes, quantity_unit=item.quantity_unit,
                quantity_value=quantity_value, unit=item.unit,
                net_weight_kg=net, gross_weight_kg=gross,
            ))
        return self._ordered_by_container(built)

    @staticmethod
    def _override(raw, default=None):
        """A hand-typed figure, or `default` when the field was left blank -
        blank means "keep the derived value", not "zero"."""
        if raw in (None, ""):
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _container_at(container_details: List[dict], index: int) -> dict:
        """The section-11B row an allocation points at, or an empty snapshot
        when 11B hasn't been filled in yet (the split is still meaningful -
        the container numbers just print blank)."""
        if 0 <= index < len(container_details or []):
            return container_details[index] or {}
        return {}

    @staticmethod
    def _clean_allocations(raw_allocations, item_count: int) -> List[dict]:
        """Drop blank rows and anything pointing at a goods line that isn't
        there any more (the form can submit a stale row after a product line
        is removed). Container index is clamped at 0, not bounded above -
        section 11B may legitimately still be empty."""
        rows = []
        for raw in raw_allocations or []:
            try:
                item_index = int(raw.get("invoice_item_index"))
            except (TypeError, ValueError):
                continue
            if not 0 <= item_index < item_count:
                continue
            boxes_raw = raw.get("quantity_boxes")
            if boxes_raw in (None, ""):
                boxes = None
            else:
                try:
                    boxes = float(boxes_raw)
                except (TypeError, ValueError):
                    raise ValidationError("Container split: boxes must be a number.")
                if boxes <= 0:
                    continue  # a zero-box row means "nothing of this line here"
            try:
                container_index = max(int(raw.get("container_index") or 0), 0)
            except (TypeError, ValueError):
                container_index = 0
            rows.append({
                "container_index": container_index, "invoice_item_index": item_index,
                "quantity_boxes": boxes, "group_label": raw.get("group_label"),
                "pallets": raw.get("pallets"),
                "net_weight_kg": raw.get("net_weight_kg"), "gross_weight_kg": raw.get("gross_weight_kg"),
            })
        return rows

    @staticmethod
    def _assert_balanced(items: List[ExportInvoiceItem], allocations: List[dict]) -> None:
        """Every box on the export invoice is in exactly one container: no
        line over-allocated (the same box loaded twice), none left behind."""
        for i, item in enumerate(items):
            mine = [a for a in allocations if a["invoice_item_index"] == i]
            name = item.product_name
            if not item.quantity_boxes:
                # Nothing to split by - the line goes into one container whole.
                if len(mine) > 1:
                    raise ValidationError(
                        f"'{name}' has no box count on the export invoice, so it can't be split across "
                        f"containers - keep it on a single container row."
                    )
                continue
            if not mine:
                raise ValidationError(
                    f"'{name}': all {item.quantity_boxes:g} boxes are still unassigned - "
                    f"add a container row for this product."
                )
            allocated = sum(a["quantity_boxes"] or 0 for a in mine)
            diff = allocated - item.quantity_boxes
            if diff > _BOX_TOLERANCE:
                raise ValidationError(
                    f"'{name}': {allocated:g} boxes split across containers, but the export invoice only "
                    f"has {item.quantity_boxes:g} - remove {diff:g}."
                )
            if diff < -_BOX_TOLERANCE:
                raise ValidationError(
                    f"'{name}': {allocated:g} of {item.quantity_boxes:g} boxes split across containers - "
                    f"{-diff:g} still unassigned."
                )

    @staticmethod
    def _ordered_by_container(built: List[ExportPackingListItem]) -> List[ExportPackingListItem]:
        """Print order: containers in order, rows within a container kept in
        the order they were entered/allocated - designs are no longer grouped
        or re-sorted by product/HSN."""
        ordered = sorted(built, key=lambda item: item.container_sr_no)
        for i, item in enumerate(ordered, start=1):
            item.sr_no = i
        return ordered

    # ---- writes (only ever called by ExportInvoiceService) ----------------------
    def save_for_invoice(self, current_user: User, invoice: ExportInvoice,
                         items: List[ExportPackingListItem]) -> ExportPackingList:
        """Persist the split built by build_items against a freshly saved
        export invoice. The number/date are minted once, on first
        generation, and the repository keeps them across later saves."""
        packing_list = ExportPackingList(
            id=None, company_id=invoice.company_id, export_invoice_id=invoice.id,
            packing_list_number=self.export_packing_list_repo.next_number(invoice.company_id, invoice.invoice_date),
            packing_list_date=invoice.invoice_date, created_by=current_user.id, items=items,
        )
        return self.export_packing_list_repo.upsert_for_invoice(packing_list)


# ============================================================
# PACKING LIST SERVICE
# ============================================================
# A "2 PCS = 0.72 SQM" (or LM) note anywhere in a row's description carries
# its per-box packing figures - the same pattern the packing list form's
# JavaScript parses for its live auto-calc.
_PACK_NOTE_PATTERN = re.compile(r"([\d.]+)\s*PCS?\s*=\s*([\d.]+)\s*(?:SQM|LM)", re.IGNORECASE)


def _per_box_factors(product, description: str) -> tuple:
    """(pcs_per_box, qty_per_box) for one packing row: the row's catalog
    product's Quantity / Alternate Quantity when set - every design under a
    product shares the same packing spec - else the packing note parsed
    from the description. 0.0 means unknown - callers skip that auto-calc.
    (Boxes-per-pallet is NOT a product-level fallback any more: it comes
    from the pallet type the row explicitly selected, because the default
    palleting option is 'loose' - no pallets at all.)"""
    pcs_per_box = _leading_number(product.quantity) if product else 0.0
    qty_per_box = _leading_number(product.alternate_quantity) if product else 0.0
    note = _PACK_NOTE_PATTERN.search(description or "")
    if note:
        try:
            pcs_per_box = pcs_per_box or float(note.group(1))
            qty_per_box = qty_per_box or float(note.group(2))
        except ValueError:
            pass
    return pcs_per_box, qty_per_box


class PackingListService:
    """Mirrors ProformaInvoiceService layer-for-layer. A packing list is
    normally started from an existing Proforma Invoice
    (build_prefill_from_proforma) - each product line from the proforma is
    then broken down into one or more DESIGN rows in smaller quantities."""

    def __init__(self, packing_list_repo: PackingListRepository, product_repo: ProductRepository,
                 design_repo: DesignRepository, lead_repo: LeadRepositoryBase,
                 proforma_invoice_repo: ProformaInvoiceRepository, version_service: "DocumentVersionService",
                 quotation_repo: Optional[QuotationRepository] = None,
                 purchase_order_repo: Optional[PurchaseOrderRepository] = None,
                 fulfilment_service: Optional["ProformaFulfilmentService"] = None,
                 purchase_invoice_repo: Optional[PurchaseInvoiceRepository] = None):
        self.packing_list_repo = packing_list_repo
        self.product_repo = product_repo
        self.design_repo = design_repo
        self.lead_repo = lead_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.version_service = version_service
        self.quotation_repo = quotation_repo
        self.purchase_order_repo = purchase_order_repo
        # Optional: when present, a PO's packing list is prefilled with only
        # the designs its proforma invoice still needs ordered.
        self.fulfilment_service = fulfilment_service
        # Optional: lets a Purchase Invoice's own packing list validate its
        # purchase_invoice_id and import its linked PO's PL wholesale.
        self.purchase_invoice_repo = purchase_invoice_repo

    # ---- reads --------------------------------------------------
    def get(self, packing_list_id: int, company_id: int) -> PackingList:
        packing_list = self.packing_list_repo.get_by_id(packing_list_id)
        if not packing_list or packing_list.company_id != company_id:
            # 404, not 403 - don't reveal that another company's packing list exists.
            raise NotFoundError(f"Packing list #{packing_list_id} not found.")
        return packing_list

    def list_all(
        self, company_id: int, doc_type: Optional[str] = None, client_name: Optional[str] = None
    ) -> List[PackingList]:
        return self.packing_list_repo.list_all(company_id, doc_type=doc_type, client_name=client_name)

    def list_consignees(self, company_id: int) -> List[str]:
        return self.packing_list_repo.list_distinct_consignees(company_id)

    def list_for_lead(self, lead_id: Optional[int]) -> List[PackingList]:
        """Same shape as QuotationService.list_for_lead - unscoped by
        company_id because the caller already owns the lead/client."""
        if not lead_id:
            return []
        return self.packing_list_repo.list_for_lead(lead_id)

    def list_for_proforma(self, proforma_invoice_id: int, company_id: int) -> List[PackingList]:
        """Every packing list generated from one proforma invoice, company-
        scoped - drives the combined invoice + packing details print view."""
        return [pl for pl in self.packing_list_repo.list_for_proforma(proforma_invoice_id)
                if pl.company_id == company_id]

    def list_for_quotation(self, quotation_id: int, company_id: int) -> List[PackingList]:
        """Every packing list generated directly from a quotation (skipping
        the proforma invoice step), company-scoped - drives the combined
        quotation + packing details print view, same as list_for_proforma."""
        return [pl for pl in self.packing_list_repo.list_for_quotation(quotation_id)
                if pl.company_id == company_id]

    def list_for_purchase_order(self, purchase_order_id: int, company_id: int) -> List[PackingList]:
        """Every packing list generated from one purchase order (the PO's
        own PL), company-scoped - drives the combined PO + packing details
        print view, same as list_for_proforma."""
        return [pl for pl in self.packing_list_repo.list_for_purchase_order(purchase_order_id)
                if pl.company_id == company_id]

    def list_for_purchase_invoice(self, purchase_invoice_id: int, company_id: int) -> List[PackingList]:
        """Every packing list generated from one Purchase Invoice (that
        invoice's own PL), company-scoped - same as list_for_purchase_order."""
        return [pl for pl in self.packing_list_repo.list_for_purchase_invoice(purchase_invoice_id)
                if pl.company_id == company_id]

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, packing_list: PackingList, current_user: User):
        if current_user.is_admin:
            return
        if packing_list.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage packing lists you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, company_id: int, packing_list_date: str) -> str:
        """PL{YYYYMMDD}{seq} where seq is that day's packing list count + 1
        for this company, zero-padded to 3 digits (e.g. PL20260714001)."""
        date_part = packing_list_date.replace("-", "")
        prefix = f"PL{date_part}"
        seq = self.packing_list_repo.count_for_date_prefix(company_id, prefix) + 1
        return f"{prefix}{seq:03d}"

    # ---- importing an ancestor document's packing list --------------------------------------------------
    def _newest_packing_list(self, packing_lists: list, company_id: int) -> Optional[PackingList]:
        """Newest company-scoped packing list from a repo list (which orders
        by id), or None."""
        scoped = [pl for pl in packing_lists if pl.company_id == company_id]
        return scoped[-1] if scoped else None

    def _ancestor_packing_list(self, company_id: int, *, proforma_invoice_id: Optional[int] = None,
                               quotation_id: Optional[int] = None) -> Optional[PackingList]:
        """Newest packing list found on an ancestor document, walking up the
        link chain Purchase Order -> Proforma Invoice -> Quotation. A nearer
        ancestor wins: a PL already generated from the proforma invoice is
        preferred over one from the quotation the invoice itself came from.
        Returns the PackingList (items loaded) or None so the goods on the
        latest document's PL start from whatever was last shipped/packed
        upstream instead of an empty sheet."""
        if proforma_invoice_id:
            found = self._newest_packing_list(
                self.packing_list_repo.list_for_proforma(proforma_invoice_id), company_id)
            if found:
                return found
            # No PL on the proforma invoice itself - fall through to the
            # quotation it was generated from, if any.
            invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
            if invoice and invoice.company_id == company_id and invoice.quotation_id:
                quotation_id = invoice.quotation_id
        if quotation_id and self.quotation_repo is not None:
            return self._newest_packing_list(
                self.packing_list_repo.list_for_quotation(quotation_id), company_id)
        return None

    def _items_from_packing_list(self, source_pl: PackingList) -> list:
        """Full design-level rows copied from an existing packing list, so the
        new PL starts pre-filled with the same designs, boxes, pallets, pcs,
        quantities and weights."""
        return [
            {
                "product_id": item.product_id, "product_name": item.product_name,
                "design_id": item.design_id, "design_name": item.design_name or "",
                "hsn_code": item.hsn_code, "box_per_pallet": item.box_per_pallet or "",
                "pallets": item.pallets or "",
                "quantity_boxes": item.quantity_boxes or "", "pcs": item.pcs or "",
                "quantity_value": item.quantity_value or "", "unit": item.unit,
                "net_weight_kg": item.net_weight_kg or "", "gross_weight_kg": item.gross_weight_kg or "",
            }
            for item in source_pl.items
        ]

    def _placeholder_items(self, source_items: list) -> list:
        """One empty product block per source line (header only, marked
        is_placeholder so the form doesn't render a blank design row) - used
        when no upstream packing list exists to import, so the user picks
        designs and per-design box counts themselves."""
        return [
            {
                "product_id": item.product_id, "product_name": item.product_name,
                "design_id": None, "design_name": "",
                "hsn_code": item.hsn_code, "box_per_pallet": "", "pallets": "",
                "quantity_boxes": "", "pcs": "",
                "quantity_value": "", "unit": item.unit,
                "net_weight_kg": "", "gross_weight_kg": "",
                "is_placeholder": True,
            }
            for item in source_items
        ]

    # ---- prefill from an existing proforma invoice --------------------------------------------------
    def build_prefill_from_proforma(self, invoice: ProformaInvoice) -> dict:
        """Caller must have already loaded `invoice` via
        ProformaInvoiceService.get(invoice_id, current_user.company_id) so
        cross-company ownership is already verified. When the invoice was
        generated from a quotation that already has a packing list, that PL's
        full design-level rows are imported as the starting point; otherwise
        each proforma product line becomes one empty product block (marked
        is_placeholder) and the user fills in designs and box counts."""
        source_pl = self._ancestor_packing_list(invoice.company_id, quotation_id=invoice.quotation_id)
        fields = {
            "proforma_invoice_id": invoice.id,
            "lead_id": invoice.lead_id,
            "export_ref_no": invoice.export_ref_no,
            "buyer_order_no": invoice.buyer_order_no,
            "other_reference": invoice.other_reference,
            "remarks": (source_pl.remarks if source_pl else None) or invoice.remarks or "MADE IN INDIA",
        }
        if source_pl:
            items = self._items_from_packing_list(source_pl)
        else:
            items = self._placeholder_items(invoice.items)
        return {"fields": fields, "items": items}

    # ---- prefill from an existing quotation (skips the PI step) --------------------------------------------------
    def build_prefill_from_quotation(self, quotation: Quotation) -> dict:
        """Same shape as build_prefill_from_proforma, but starting straight
        from a Quotation - lets a packing list be generated without an
        intermediate proforma invoice. Caller must have already loaded
        `quotation` via QuotationService.get(quotation_id, current_user.company_id)
        so cross-company ownership is already verified. A quotation is the top
        of the document chain, so there is no upstream PL to import - each
        product line becomes one empty product block."""
        fields = {
            "quotation_id": quotation.id,
            "lead_id": quotation.lead_id,
            "buyer_order_no": quotation.buyer_reference_no,
            "remarks": quotation.remarks or "MADE IN INDIA",
        }
        items = self._placeholder_items(quotation.items)
        return {"fields": fields, "items": items}

    # ---- prefill from an existing purchase order --------------------------------------------------
    def build_prefill_from_purchase_order(self, purchase_order: PurchaseOrder) -> dict:
        """The PO's own packing list. Caller must have already loaded
        `purchase_order` via PurchaseOrderService.get(...) so cross-company
        ownership is already verified. When an ancestor document already has a
        packing list - the proforma invoice the PO came from, or failing that
        the quotation that invoice came from - that PL's full design-level
        rows are imported as the starting point (the goods being ordered are
        the goods being shipped); otherwise each PO product line becomes one
        empty product block, same as build_prefill_from_proforma.

        When the PO came from a proforma invoice that has a packing list of
        its own, the imported rows are cut down to what that invoice still
        needs ordered (_remaining_designs below) - a design already covered
        in full by an earlier PO for the same invoice is dropped, so the
        second and third PO don't start out re-ordering the first one's
        goods."""
        fields = {
            "purchase_order_id": purchase_order.id,
            "lead_id": purchase_order.lead_id,
            "buyer_order_no": purchase_order.seller_ref_no,
            "remarks": purchase_order.remarks or "MADE IN INDIA",
        }
        source_pl = self._ancestor_packing_list(
            purchase_order.company_id, proforma_invoice_id=purchase_order.proforma_invoice_id)
        if source_pl:
            items = self._items_from_packing_list(source_pl)
            items = self._remaining_designs(
                purchase_order.company_id, purchase_order.proforma_invoice_id, items)
        else:
            items = self._placeholder_items(purchase_order.items)
        return {"fields": fields, "items": items}

    # ---- prefill from an existing purchase invoice --------------------------------------------------
    def build_prefill_from_purchase_invoice(self, purchase_invoice: PurchaseInvoice) -> dict:
        """The Purchase Invoice's own packing list. Caller must have already
        loaded `purchase_invoice` via PurchaseInvoiceService.get(...) so
        cross-company ownership is already verified. Imports its linked
        purchase order's own packing list WHOLESALE - unlike
        build_prefill_from_purchase_order, there is no _remaining_designs
        cut-down here, since a Purchase Invoice already corresponds to
        exactly one PO's shipment, not a split still being placed across
        several. Falls back to one empty product block per invoice line
        (same as build_prefill_from_proforma) when the linked PO has no
        packing list of its own yet."""
        fields = {
            "purchase_invoice_id": purchase_invoice.id,
            "lead_id": purchase_invoice.lead_id,
            "buyer_order_no": purchase_invoice.seller_ref_no,
            "remarks": purchase_invoice.remarks or "MADE IN INDIA",
        }
        source_pl = None
        if purchase_invoice.purchase_order_id:
            source_pl = self._newest_packing_list(
                self.packing_list_repo.list_for_purchase_order(purchase_invoice.purchase_order_id),
                purchase_invoice.company_id,
            )
        if source_pl:
            items = self._items_from_packing_list(source_pl)
        else:
            items = self._placeholder_items(purchase_invoice.items)
        return {"fields": fields, "items": items}

    def _remaining_designs(self, company_id: int, proforma_invoice_id: Optional[int], items: list) -> list:
        """Filters imported packing-list rows down to the designs the invoice
        still needs placed, scaling each surviving row to its outstanding
        share. A design that is half ordered comes through at half its
        boxes/pallets/pcs/quantity/weights; one that is fully ordered is
        dropped entirely.

        No-ops (returns `items` untouched) when there is nothing to compare
        against - no fulfilment service wired in, no invoice, or an invoice
        whose own packing list doesn't exist yet - so importing from a
        quotation's packing list still behaves exactly as before."""
        if not self.fulfilment_service or not proforma_invoice_id:
            return items
        status = self.fulfilment_service.design_status(company_id, proforma_invoice_id)
        if not status["designs"]:
            return items
        pending = {_design_key(design): design for design in status["pending"]}

        remaining = []
        for item in items:
            if item.get("is_placeholder"):
                remaining.append(item)
                continue
            design = pending.get(_design_key(item))
            if not design:
                continue  # already ordered in full on another purchase order
            remaining.append(self._scaled_row(item, design))
        return remaining

    @staticmethod
    def _scaled_row(item: dict, design: dict) -> dict:
        """One imported row rescaled to the outstanding part of its design.
        Every per-row figure moves together (they all describe the same
        goods), so one ratio drives them all; a ratio of 1 - nothing ordered
        yet, the usual case for the first PO - copies the row unchanged.
        box_per_pallet is a packing spec, not a quantity, so it never
        scales."""
        if design["required_boxes"] > 0:
            ratio = design["pending_boxes"] / design["required_boxes"]
        elif design["required_quantity"] > 0:
            ratio = design["pending_quantity"] / design["required_quantity"]
        else:
            ratio = 1
        if ratio >= 1:
            return item

        def scale(value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return value
            return round(number * ratio, 2) or ""

        scaled = dict(item)
        for key in ("pallets", "quantity_boxes", "pcs", "quantity_value",
                    "net_weight_kg", "gross_weight_kg"):
            if scaled.get(key) not in (None, ""):
                scaled[key] = scale(scaled[key])
        return scaled

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[PackingListItem]:
        items = []
        for i, raw in enumerate(raw_items, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            try:
                quantity_value = float(raw["quantity_value"]) if raw.get("quantity_value") else None
                quantity_boxes = float(raw["quantity_boxes"]) if raw.get("quantity_boxes") else None
                box_per_pallet = float(raw["box_per_pallet"]) if raw.get("box_per_pallet") else None
                pallets = float(raw["pallets"]) if raw.get("pallets") else None
                pcs = float(raw["pcs"]) if raw.get("pcs") else None
                net_weight_kg = float(raw["net_weight_kg"]) if raw.get("net_weight_kg") else None
                gross_weight_kg = float(raw["gross_weight_kg"]) if raw.get("gross_weight_kg") else None
            except ValueError:
                raise ValidationError(f"Row {i}: quantity, pallets, pcs and weights must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None
            design_id = int(raw["design_id"]) if raw.get("design_id") else None
            design_name = (raw.get("design_name") or "").strip() or None

            # Same trust boundary as QuotationService._build_items - only
            # keep product/design references from this same company (and a
            # design must actually belong to the row's product). The Boxes
            # column's unit (printed as small text after the number) is
            # likewise always the product's own Quantity unit.
            product = None
            quantity_unit = "PCS"
            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None
                    product = None
                else:
                    quantity_unit = product.quantity_unit or "PCS"
            if design_id:
                design = self.design_repo.get_by_id(design_id)
                if not design or design.company_id != company_id or \
                        (product_id and design.product_id != product_id):
                    design_id = None

            # Boxes is the compulsory field the rest of the row is driven
            # from - Pallets is only an alternative way to arrive at it. If
            # Boxes is missing (only possible by bypassing the form's
            # `required` attribute) but Pallets and Box-per-pallet are both
            # known, fall back to deriving Boxes from those; otherwise
            # Boxes truly is missing and that's an error. Box-per-pallet is
            # whatever pallet type the row selected on the form (empty =
            # the default 'loose' option: goods unpalletised, no pallets) -
            # it deliberately does NOT fall back to the catalog product,
            # since a product's pallet types are options, not a default.
            pcs_per_box, qty_per_box = _per_box_factors(product, product_name)
            if quantity_boxes is None:
                if pallets and box_per_pallet:
                    quantity_boxes = round(pallets * box_per_pallet, 2)
                else:
                    raise ValidationError(f"Row {i} ('{product_name}'): boxes is compulsory.")

            # Pallets always auto-derives from Boxes / Box-per-pallet, kept
            # to 2 decimals so a partial last pallet (e.g. 3.5) is expressed
            # exactly rather than rounded to a whole pallet.
            # No pallet type selected ('loose') means zero pallets, full stop.
            if box_per_pallet:
                pallets = round(quantity_boxes / box_per_pallet, 2)
            else:
                pallets = None

            # Qty (and Pcs, when left blank) are authoritatively Boxes x the
            # per-box factors whenever those are known (design's own figures,
            # or a packing note parsed out of the description) - the
            # client-side value is only a preview, not trusted for storage.
            # Qty is otherwise optional and defaults to 0 when no factor is
            # known and nothing was typed in.
            if quantity_boxes and qty_per_box:
                quantity_value = round(quantity_boxes * qty_per_box, 2)
            elif quantity_value is None:
                quantity_value = 0
            if pcs is None and quantity_boxes and pcs_per_box:
                pcs = round(quantity_boxes * pcs_per_box, 2)

            # Net/gross weight auto-calculate from Boxes x the row's catalog
            # product's per-box weight, same trigger as Qty/Pcs above - but
            # only to fill in a blank: a weight the row already submitted
            # (typed by hand, or set from the client-side auto-calc) is kept
            # as-is, so it stays manually editable on this document instead
            # of being silently recalculated back on every save.
            if product and quantity_boxes:
                if net_weight_kg is None and product.net_weight_kg:
                    net_weight_kg = round(quantity_boxes * product.net_weight_kg, 2)
                if gross_weight_kg is None and product.gross_weight_kg:
                    gross_weight_kg = round(quantity_boxes * product.gross_weight_kg, 2)

            items.append(PackingListItem(
                id=None, packing_list_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                design_id=design_id, design_name=design_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                box_per_pallet=box_per_pallet, pcs=pcs,
                pallets=pallets, quantity_boxes=quantity_boxes, quantity_unit=quantity_unit, quantity_value=quantity_value,
                unit=(raw.get("unit") or "SQM").strip() or "SQM",
                net_weight_kg=net_weight_kg, gross_weight_kg=gross_weight_kg,
            ))
        if not items:
            raise ValidationError("At least one design line is compulsory.")
        return items

    def _build_header(self, current_user: User, fields: dict, items: List[PackingListItem]) -> PackingList:
        # Consignee/buyer/shipment details aren't collected on this form (the
        # printed sheet only shows the proforma invoice no., date and item
        # rows) - `consignee_name` stays on the model/schema for now since it's
        # NOT NULL, but is stored blank rather than asked of the user.
        packing_list_date = (fields.get("packing_list_date") or "").strip() or date.today().isoformat()

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            # Only trust a lead from this same company - otherwise a crafted
            # lead_id could attach this packing list to another company's lead.
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        proforma_invoice_id = int(fields["proforma_invoice_id"]) if fields.get("proforma_invoice_id") else None
        if proforma_invoice_id is not None:
            # Only trust a proforma invoice from this same company - same reasoning as lead_id above.
            invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
            if not invoice or invoice.company_id != current_user.company_id:
                proforma_invoice_id = None

        quotation_id = int(fields["quotation_id"]) if fields.get("quotation_id") else None
        if quotation_id is not None and self.quotation_repo is not None:
            # Only trust a quotation from this same company - same reasoning as lead_id above.
            quotation = self.quotation_repo.get_by_id(quotation_id)
            if not quotation or quotation.company_id != current_user.company_id:
                quotation_id = None

        purchase_order_id = int(fields["purchase_order_id"]) if fields.get("purchase_order_id") else None
        if purchase_order_id is not None and self.purchase_order_repo is not None:
            # Only trust a purchase order from this same company - same reasoning as lead_id above.
            purchase_order = self.purchase_order_repo.get_by_id(purchase_order_id)
            if not purchase_order or purchase_order.company_id != current_user.company_id:
                purchase_order_id = None

        purchase_invoice_id = int(fields["purchase_invoice_id"]) if fields.get("purchase_invoice_id") else None
        if purchase_invoice_id is not None and self.purchase_invoice_repo is not None:
            # Only trust a purchase invoice from this same company - same reasoning as lead_id above.
            purchase_invoice = self.purchase_invoice_repo.get_by_id(purchase_invoice_id)
            if not purchase_invoice or purchase_invoice.company_id != current_user.company_id:
                purchase_invoice_id = None

        return PackingList(
            id=None, company_id=current_user.company_id, packing_list_number="",
            packing_list_date=packing_list_date, consignee_name="",
            created_by=current_user.id, lead_id=lead_id, proforma_invoice_id=proforma_invoice_id,
            quotation_id=quotation_id, purchase_order_id=purchase_order_id,
            purchase_invoice_id=purchase_invoice_id,
            export_ref_no=(fields.get("export_ref_no") or "").strip() or None,
            buyer_order_no=(fields.get("buyer_order_no") or "").strip() or None,
            other_reference=(fields.get("other_reference") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            items=items,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> PackingList:
        items = self._build_items(current_user.company_id, raw_items)
        packing_list = self._build_header(current_user, fields, items)
        packing_list.packing_list_number = self._generate_number(
            current_user.company_id, packing_list.packing_list_date
        )
        created = self.packing_list_repo.create(packing_list)
        self.version_service.record("packing_list", created, current_user.id)
        return created

    def update(self, current_user: User, packing_list_id: int, fields: dict, raw_items: list) -> PackingList:
        existing = self.get(packing_list_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        packing_list = self._build_header(current_user, fields, items)
        self.packing_list_repo.update(packing_list_id, packing_list)
        updated = self.get(packing_list_id, current_user.company_id)
        self.version_service.record("packing_list", updated, current_user.id)
        return updated

    def delete(self, current_user: User, packing_list_id: int) -> None:
        existing = self.get(packing_list_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.packing_list_repo.delete(packing_list_id)


# ============================================================
# PROFORMA FULFILMENT SERVICE
# ============================================================
# A proforma invoice says WHAT is being sold; its packing list breaks that
# down into the individual DESIGNS that actually have to be manufactured.
# Those designs are then ordered from suppliers through purchase orders, and
# each purchase order carries its own packing list saying which designs (and
# how many boxes) that supplier is making. One invoice is normally split
# across several suppliers, so "have we ordered everything yet?" means
# comparing the invoice's packing list against the packing lists of every PO
# linked to it. That comparison lives here.
#
# Rounding tolerance: box counts are stored as REALs, so a design that is
# covered to within a thousandth of a box counts as fully placed rather than
# leaving a 0.0000001 sliver pending forever.
_DESIGN_QTY_TOLERANCE = 0.001


def _normalize_name(value: Optional[str]) -> str:
    """Case/whitespace-insensitive comparison key for a hand-typed name, so
    'Ocean Blue' and 'OCEAN BLUE ' are treated as the same thing."""
    return " ".join((value or "").split()).upper()


def _design_key(row: dict) -> tuple:
    """How a packing-list line on the invoice side is matched to a line on
    the purchase-order side. Catalog ids win when both sides have them;
    hand-typed rows fall back to their stored names."""
    return (
        row.get("product_id") or _normalize_name(row.get("product_name")),
        row.get("design_id") or _normalize_name(row.get("design_name")),
    )


def _product_key(row: dict):
    """Product-level analogue of _design_key, for the PO-creation-time
    comparison - a purchase order's product lines have no design dimension,
    so this is just the product half of _design_key's tuple."""
    return row.get("product_id") or _normalize_name(row.get("product_name"))


class ProformaFulfilmentService:
    """Answers one question per proforma invoice: which designs from its
    packing list have NOT yet been placed on a purchase order?

    Reads only - it owns no writes and no state. Everything is derived live
    from the packing lists on both sides, so placing a design on a PO (or
    editing/deleting that PO) updates the answer immediately with nothing to
    keep in sync."""

    def __init__(self, proforma_invoice_repo: ProformaInvoiceRepository,
                 packing_list_repo: PackingListRepository,
                 purchase_order_repo: PurchaseOrderRepository):
        self.proforma_invoice_repo = proforma_invoice_repo
        self.packing_list_repo = packing_list_repo
        self.purchase_order_repo = purchase_order_repo

    # ---- the core comparison --------------------------------------------------
    def design_status_map(self, company_id: int, proforma_invoice_ids: List[int]) -> dict:
        """{proforma_invoice_id: {"designs": [...], "pending": [...],
        "placed_count": int, "is_fully_placed": bool}} for many invoices in
        two queries, so the reminder feed never goes N+1.

        Each design row carries required/placed/pending in BOTH boxes and
        alternate quantity. Boxes are the yardstick whenever the invoice side
        states them (that is what a PO is placed in); rows packed without a
        box count fall back to the quantity column.
        """
        ids = [int(i) for i in proforma_invoice_ids if i]
        if not ids:
            return {}
        required_rows = self.packing_list_repo.design_totals_for_proforma(company_id, ids)
        required_rows += self._quotation_ancestor_fallback_rows(company_id, ids, required_rows)
        placed_rows = self.packing_list_repo.design_totals_for_linked_purchase_orders(company_id, ids)

        placed_index = {}
        for row in placed_rows:
            placed_index[(row["pi_id"], _design_key(row))] = row

        result = {pi_id: {"designs": [], "pending": [], "over_ordered": [],
                          "placed_count": 0, "is_fully_placed": True}
                  for pi_id in ids}
        for row in required_rows:
            pi_id = row["pi_id"]
            placed = placed_index.get((pi_id, _design_key(row)))
            required_boxes = row["boxes"] or 0
            required_quantity = row["quantity"] or 0
            placed_boxes = (placed["boxes"] if placed else 0) or 0
            placed_quantity = (placed["quantity"] if placed else 0) or 0

            # Which column decides "done" - boxes when the invoice's packing
            # list stated them, otherwise the alternate quantity.
            if required_boxes > 0:
                outstanding = required_boxes - placed_boxes
            else:
                outstanding = required_quantity - placed_quantity
            is_placed = outstanding <= _DESIGN_QTY_TOLERANCE
            # A design isn't only ever "not enough yet" - since it can be
            # bought piecemeal across several purchase orders, nothing stops
            # the same design being placed on more than one and adding up to
            # MORE than the invoice's packing list called for. Over-ordered
            # is its own state, not just "is_placed" - both get reported so
            # a caller can flag it without treating it as still-pending.
            is_over_ordered = outstanding < -_DESIGN_QTY_TOLERANCE

            design = {
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "design_id": row["design_id"],
                "design_name": row["design_name"],
                "unit": row["unit"] or "SQM",
                "required_boxes": required_boxes,
                "required_quantity": required_quantity,
                "placed_boxes": placed_boxes,
                "placed_quantity": placed_quantity,
                "pending_boxes": max(required_boxes - placed_boxes, 0),
                "pending_quantity": max(required_quantity - placed_quantity, 0),
                "excess_boxes": max(placed_boxes - required_boxes, 0),
                "excess_quantity": max(placed_quantity - required_quantity, 0),
                "is_placed": is_placed,
                "is_over_ordered": is_over_ordered,
            }
            status = result[pi_id]
            status["designs"].append(design)
            if is_over_ordered:
                status["over_ordered"].append(design)
            if is_placed:
                status["placed_count"] += 1
            else:
                status["pending"].append(design)
                status["is_fully_placed"] = False
        return result

    def _quotation_ancestor_fallback_rows(self, company_id: int, ids: List[int],
                                           required_rows: List[dict]) -> List[dict]:
        """For every invoice that got NO rows from design_totals_for_proforma
        (no packing list directly against it), fall back to the packing
        list of the quotation it was generated from - the same ancestor
        PackingListService._ancestor_packing_list already walks to when
        deciding what to IMPORT into a new PO's packing list.

        Without this fallback, an invoice generated straight from a
        quotation that skips the PI step (a supported flow: the quotation
        already has its own packing list, the invoice never gets one of its
        own) always reports zero required designs - which design_status_map
        treats as "nothing to compare against" and stops filtering
        entirely, so every purchase order after the first re-imports the
        quotation's FULL packing list forever, oblivious to what earlier
        purchase orders already placed. That was a real bug, not a
        hypothetical one.

        Two PIs can share the same quotation (each independently missing
        its own PL), so this fans a quotation's totals out to every invoice
        that resolves to it rather than picking just one."""
        covered_ids = {row["pi_id"] for row in required_rows}
        uncovered_ids = [pi_id for pi_id in ids if pi_id not in covered_ids]
        if not uncovered_ids:
            return []
        quotation_by_pi = self.proforma_invoice_repo.quotation_id_map(uncovered_ids)
        if not quotation_by_pi:
            return []
        pi_ids_by_quotation: dict = {}
        for pi_id, quotation_id in quotation_by_pi.items():
            pi_ids_by_quotation.setdefault(quotation_id, []).append(pi_id)
        quotation_rows = self.packing_list_repo.design_totals_for_quotation(
            company_id, list(pi_ids_by_quotation.keys()))
        fallback_rows = []
        for row in quotation_rows:
            for pi_id in pi_ids_by_quotation[row["q_id"]]:
                fallback_rows.append({**row, "pi_id": pi_id})
        return fallback_rows

    def design_status(self, company_id: int, proforma_invoice_id: int) -> dict:
        """design_status_map for a single invoice. An invoice with no
        packing list to track - neither its own nor (via the quotation
        fallback above) its ancestor quotation's - has nothing to compare
        against, so it reports zero designs and is_fully_placed=False -
        there is still work to do, it just isn't broken down into designs
        yet."""
        status = self.design_status_map(company_id, [proforma_invoice_id]).get(proforma_invoice_id)
        if not status:
            status = {"designs": [], "pending": [], "over_ordered": [],
                      "placed_count": 0, "is_fully_placed": True}
        if not status["designs"]:
            status["is_fully_placed"] = False
        return status

    def pending_designs(self, company_id: int, proforma_invoice_id: int) -> List[dict]:
        """Just the designs still to be ordered - what the invoice page shows
        and what a new PO's packing list is prefilled with."""
        return self.design_status(company_id, proforma_invoice_id)["pending"]

    def over_ordered_designs(self, company_id: int, proforma_invoice_id: int) -> List[dict]:
        """Designs bought (summed across every purchase order linked to this
        invoice) in excess of what the invoice's own packing list called
        for - a design need not come from a single PO, so this only shows up
        once the total across all of them overshoots. What the "bought more
        than necessary" notification is built from."""
        return self.design_status(company_id, proforma_invoice_id)["over_ordered"]

    # ---- the same comparison one level up: PO product lines, not packing-list designs ----
    def product_status(self, company_id: int, invoice: ProformaInvoice) -> dict:
        """Which of the invoice's OWN product lines still have quantity not
        yet placed on any purchase order already linked to it - the PO-
        creation-time analogue of design_status, one level coarser
        (product/quantity, not product+design). Takes the already-loaded
        invoice itself rather than a bare id: its `items` ARE the 'required'
        side, so unlike design_status (which has to go looking for the PI's
        packing list) there is no extra read for that half."""
        placed_rows = self.purchase_order_repo.product_totals_for_proforma(company_id, invoice.id)
        placed_index = {_product_key(row): row for row in placed_rows}

        products, pending = [], []
        for item in invoice.items:
            placed = placed_index.get(_product_key(
                {"product_id": item.product_id, "product_name": item.product_name}))
            required_boxes = item.quantity_boxes or 0
            required_quantity = item.quantity_value or 0
            placed_boxes = (placed["boxes"] if placed else 0) or 0
            placed_quantity = (placed["quantity"] if placed else 0) or 0

            # Same yardstick as design_status: boxes decide "done" whenever
            # the invoice line states them, otherwise the quantity column.
            if required_boxes > 0:
                outstanding = required_boxes - placed_boxes
            else:
                outstanding = required_quantity - placed_quantity
            is_placed = outstanding <= _DESIGN_QTY_TOLERANCE
            is_over_ordered = outstanding < -_DESIGN_QTY_TOLERANCE

            product = {
                "product_id": item.product_id, "product_name": item.product_name,
                "hsn_code": item.hsn_code, "unit": item.unit,
                "required_boxes": required_boxes, "required_quantity": required_quantity,
                "placed_boxes": placed_boxes, "placed_quantity": placed_quantity,
                "pending_boxes": max(required_boxes - placed_boxes, 0),
                "pending_quantity": max(required_quantity - placed_quantity, 0),
                "excess_boxes": max(placed_boxes - required_boxes, 0),
                "excess_quantity": max(placed_quantity - required_quantity, 0),
                "is_placed": is_placed,
                "is_over_ordered": is_over_ordered,
            }
            products.append(product)
            if not is_placed:
                pending.append(product)
        over_ordered = [p for p in products if p["is_over_ordered"]]
        return {"products": products, "pending": pending, "over_ordered": over_ordered,
                "placed_count": len(products) - len(pending), "is_fully_placed": not pending}

    # ---- the reminder feed --------------------------------------------------
    def pending_purchase_order_reminders(self, company_id: int,
                                          created_by: Optional[int] = None) -> List[dict]:
        """Every CONFIRMED proforma invoice that still has designs nobody has
        placed a purchase order for, newest invoice first. Derived live on
        each page load rather than stored, so a reminder appears the moment
        an invoice is confirmed and disappears the moment the last design is
        placed - there is no reminder row that can go stale.

        `created_by` narrows the feed to one employee's own invoices (the
        employee dashboard); admins pass None and see the whole company."""
        invoices = self.proforma_invoice_repo.list_by_status(company_id, PROFORMA_STATUS_CONFIRMED)
        if created_by is not None:
            invoices = [i for i in invoices if i.created_by == created_by]
        if not invoices:
            return []
        status_map = self.design_status_map(company_id, [i.id for i in invoices])
        po_counts = self.purchase_order_repo.count_map_by_proforma(company_id)
        reminders = []
        for invoice in invoices:
            status = status_map.get(invoice.id) or {}
            pending = status.get("pending", [])
            has_packing_list = bool(status.get("designs"))
            if not pending and has_packing_list:
                continue  # fully ordered - nothing left to chase
            reminders.append({
                "invoice": invoice,
                "pending": pending,
                "pending_count": len(pending),
                "placed_count": status.get("placed_count", 0),
                "purchase_order_count": po_counts.get(invoice.id, 0),
                "has_packing_list": has_packing_list,
            })
        return reminders


# ============================================================
# BACKUP SERVICE
# ============================================================

# Fingerprint written into every backup so a restore can tell one of OUR
# backups apart from any other .zip the admin might upload by mistake.
BACKUP_SIGNATURE = "crm-app-backup"
BACKUP_FORMAT_VERSION = 1          # bump if the ZIP layout itself changes
_MANIFEST_NAME = "manifest.json"
_DB_ARCNAME = "database/crm.db"    # where the DB lives inside the ZIP
_SQLITE_MAGIC = b"SQLite format 3\x00"   # first 16 bytes of any SQLite file
_CORE_TABLES = ("tenants", "users")      # tables a real app DB must have


class BackupService:
    """Download and restore the ENTIRE dataset - the SQLite database plus
    every folder of uploaded files that live on disk (not in the DB) - as a
    single ZIP.

    Admin-only (enforced by the route layer). The ZIP carries a manifest with
    a signature + schema version so a restore can (a) confirm the upload is
    genuinely one of our backups, not the wrong file, and (b) forward-migrate
    an older backup to the current schema instead of rejecting or corrupting
    it (see SCHEMA_VERSION in app/database.py).
    """

    def __init__(self, db: Database, db_path: str, uploads_folders: dict, schema_path: str):
        """`uploads_folders` maps an arcname prefix (e.g. "uploads/products")
        to the on-disk folder it corresponds to - one entry per kind of
        upload the app has (product photos, suppliers' Purchase Invoice
        PDFs, ...). Adding a new upload folder later only means adding one
        more entry here, no other changes to the backup/restore logic."""
        self.db = db
        self.db_path = db_path
        self.uploads_folders = uploads_folders
        self.schema_path = schema_path

    # ---- download --------------------------------------------------
    def create_backup_zip(self):
        """Build a full-snapshot ZIP and return (zip_path, download_name).
        The caller streams `zip_path` with send_file and must delete it
        afterwards (it's a temp file)."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        tmp_db_fd, tmp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_db_fd)
        try:
            # Consistent snapshot even if another request is writing.
            self.db.create_backup_copy(tmp_db_path)

            manifest = {
                "signature": BACKUP_SIGNATURE,
                "format_version": BACKUP_FORMAT_VERSION,
                "app": "crm",
                "schema_version": self.db.get_schema_version(),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "db_filename": _DB_ARCNAME,
                "contents": ["database", "uploads"],
            }

            zip_fd, zip_path = tempfile.mkstemp(suffix=".zip")
            os.close(zip_fd)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
                zf.write(tmp_db_path, _DB_ARCNAME)
                for arcprefix, folder in self.uploads_folders.items():
                    if not os.path.isdir(folder):
                        continue
                    for root, _dirs, files in os.walk(folder):
                        for name in files:
                            abs_path = os.path.join(root, name)
                            rel = os.path.relpath(abs_path, folder)
                            arcname = f"{arcprefix}/{rel.replace(os.sep, '/')}"
                            zf.write(abs_path, arcname)
            return zip_path, f"crm-backup-{stamp}.zip"
        finally:
            if os.path.exists(tmp_db_path):
                os.remove(tmp_db_path)

    # ---- restore --------------------------------------------------
    def restore_from_zip(self, file_storage) -> dict:
        """Validate an uploaded backup and, only if it is genuinely one of our
        backups, replace the live DB + product images with its contents and
        forward-migrate. On ANY problem raises ValidationError with a clear
        message and leaves the current data untouched. Returns a small summary
        dict on success."""
        if file_storage is None or not getattr(file_storage, "filename", ""):
            raise ValidationError("Please choose a backup .zip file to restore.")

        up_fd, up_path = tempfile.mkstemp(suffix=".zip")
        os.close(up_fd)
        work_dir = tempfile.mkdtemp(prefix="crm_restore_")
        try:
            file_storage.save(up_path)

            if not zipfile.is_zipfile(up_path):
                raise ValidationError(
                    "That file isn't a valid .zip backup. Upload a backup you "
                    "downloaded from this page."
                )

            with zipfile.ZipFile(up_path) as zf:
                names = zf.namelist()
                self._assert_no_zip_slip(names, work_dir)
                # --- identity: is this really OUR backup? ---
                if _MANIFEST_NAME not in names:
                    raise ValidationError(self._not_our_backup_msg())
                try:
                    manifest = json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ValidationError(self._not_our_backup_msg())
                zf.extractall(work_dir)

            if not isinstance(manifest, dict) or manifest.get("signature") != BACKUP_SIGNATURE:
                raise ValidationError(self._not_our_backup_msg())
            fmt = manifest.get("format_version")
            if not isinstance(fmt, int) or fmt > BACKUP_FORMAT_VERSION:
                raise ValidationError(
                    "This backup was made by a newer version of the app and can't "
                    "be restored here. Update the app first."
                )

            db_arcname = manifest.get("db_filename") or _DB_ARCNAME
            extracted_db = os.path.join(work_dir, *db_arcname.split("/"))
            if not os.path.isfile(extracted_db):
                raise ValidationError(self._not_our_backup_msg())

            # The bundled file must actually BE a SQLite DB with our tables.
            self._assert_valid_app_db(extracted_db)

            # --- version rule: can we carry this backup forward? ---
            backup_version = Database.read_user_version(extracted_db)
            if backup_version == 0:
                backup_version = int(manifest.get("schema_version") or 0)
            if backup_version > SCHEMA_VERSION:
                raise ValidationError(
                    f"This backup is from a newer app version (schema v{backup_version} "
                    f"> v{SCHEMA_VERSION}) and can't be safely restored. Update the app first."
                )

            # --- everything checks out: snapshot current data, then swap ---
            self._snapshot_current("pre_restore")
            extracted_uploads = {
                arcprefix: os.path.join(work_dir, *arcprefix.split("/"))
                for arcprefix in self.uploads_folders
            }
            self._swap_in(extracted_db, extracted_uploads)

            # Bring the restored (possibly older) DB up to the current shape.
            self.db.init_schema(self.schema_path)

            return {
                "restored_from_schema_version": backup_version,
                "current_schema_version": self.db.get_schema_version(),
                "created_at": manifest.get("created_at"),
            }
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if os.path.exists(up_path):
                os.remove(up_path)

    # ---- automatic snapshots (instance/backups/*.db) --------------------------------------------------
    def list_auto_backups(self) -> list:
        """The .db snapshots the app writes before risky migrations / restores,
        newest first, for the download list on the page."""
        backup_dir = self._backups_dir()
        if not os.path.isdir(backup_dir):
            return []
        items = []
        for name in os.listdir(backup_dir):
            path = os.path.join(backup_dir, name)
            if not name.endswith(".db") or not os.path.isfile(path):
                continue
            st = os.stat(path)
            items.append({
                "name": name,
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "modified_ts": st.st_mtime,
            })
        items.sort(key=lambda x: x["modified_ts"], reverse=True)
        return items

    def get_auto_backup_path(self, name: str) -> str:
        """Resolve a requested snapshot name to a safe path (basename only, no
        traversal, must exist)."""
        safe = os.path.basename(name or "")
        if safe != name or not safe.endswith(".db"):
            raise ValidationError("Invalid backup file name.")
        path = os.path.join(self._backups_dir(), safe)
        if not os.path.isfile(path):
            raise NotFoundError("That backup no longer exists.")
        return path

    # ---- internals --------------------------------------------------
    def _backups_dir(self) -> str:
        return os.path.join(os.path.dirname(self.db_path), "backups")

    def _snapshot_current(self, tag: str) -> None:
        """Copy the CURRENT db + every upload folder into instance/backups/
        so a restore is reversible. Uses the same crm_<tag>_<stamp>.db
        naming the migration backups use, so it shows up in
        list_auto_backups()."""
        backup_dir = self._backups_dir()
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = os.path.splitext(os.path.basename(self.db_path))[0]
        if os.path.exists(self.db_path):
            self.db.create_backup_copy(os.path.join(backup_dir, f"{stem}_{tag}_{stamp}.db"))
        for arcprefix, folder in self.uploads_folders.items():
            if os.path.isdir(folder):
                safe_name = arcprefix.replace("/", "_")
                shutil.copytree(folder, os.path.join(backup_dir, f"{safe_name}_{tag}_{stamp}"))

    def _swap_in(self, new_db_path: str, new_uploads_dirs: dict) -> None:
        """Replace the live DB file and every upload folder with the
        restored ones. DB swap is atomic (os.replace on the same filesystem);
        each uploads folder is moved aside first and rolled back on failure.
        `new_uploads_dirs` maps the same arcprefix keys as self.uploads_folders
        to the corresponding extracted folder from the backup ZIP."""
        # DB: stage next to the target (same filesystem) then atomic replace.
        staging = self.db_path + ".restore_tmp"
        shutil.copy2(new_db_path, staging)
        os.replace(staging, self.db_path)
        # Drop any stale WAL/SHM sidecars so they can't shadow the new file.
        for sidecar in (self.db_path + "-wal", self.db_path + "-shm"):
            if os.path.exists(sidecar):
                os.remove(sidecar)

        for arcprefix, folder in self.uploads_folders.items():
            new_dir = new_uploads_dirs.get(arcprefix)
            # Uploads: move current aside, then put the restored folder in place.
            os.makedirs(os.path.dirname(folder), exist_ok=True)
            aside = None
            if os.path.isdir(folder):
                aside = f"{folder}_old_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                os.rename(folder, aside)
            try:
                if new_dir and os.path.isdir(new_dir):
                    shutil.copytree(new_dir, folder)
                else:
                    os.makedirs(folder, exist_ok=True)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                if aside:
                    os.rename(aside, folder)
                raise
            if aside:
                shutil.rmtree(aside, ignore_errors=True)

    @staticmethod
    def _assert_no_zip_slip(names: list, dest_dir: str) -> None:
        dest_root = os.path.abspath(dest_dir)
        for member in names:
            target = os.path.abspath(os.path.join(dest_root, member))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ValidationError("Backup archive contains unsafe file paths and was rejected.")

    @staticmethod
    def _assert_valid_app_db(db_path: str) -> None:
        with open(db_path, "rb") as f:
            if f.read(16) != _SQLITE_MAGIC:
                raise ValidationError(BackupService._not_our_backup_msg())
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise ValidationError(
                    "The database inside this backup is corrupted and can't be restored."
                )
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(t not in tables for t in _CORE_TABLES):
                raise ValidationError(BackupService._not_our_backup_msg())
        finally:
            conn.close()

    @staticmethod
    def _not_our_backup_msg() -> str:
        return ("This file doesn't look like a backup created by this app. Please upload a "
                ".zip you downloaded from the Database Backup page.")
