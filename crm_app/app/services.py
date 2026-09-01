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
import math
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
    User, Lead, Party, Supplier, Transporter, Permit, BookingDetail, MiscCurrency, MiscNatureOfContract, MiscPortOfLoading, MiscContainerType, MiscHsnCode, MiscCountry, MiscUnit, DEFAULT_CURRENCIES, DEFAULT_CONTAINER_TYPES, ContactPerson, Communication, PaymentEntry, DocumentEntry,
    LEAD_STATUSES, CLIENT_STATUSES, CLIENT_STATUS_ADVANCE_ON, PRODUCT_UNITS, Category, Product,
    ProductPalletType, ProductFolder,
    Design, Quotation, QuotationItem, ProformaInvoice, ProformaInvoiceItem,
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderItemBatch, PurchaseOrderItemProduction, JobWork, JobWorkItem, JobWorkProduct, JobOut, JobIn, JobInItem,
    PurchaseInvoice, PurchaseInvoiceItem, PackingList, PackingListItem,
    DocumentVersion, PURCHASE_TYPES, DEFAULT_PURCHASE_TYPE, EXEMPTION_IGST_PERCENT,
    PRODUCTION_STATUSES, DEFAULT_PRODUCTION_STATUS,
    PROFORMA_STATUSES, PROFORMA_STATUS_DRAFT, PROFORMA_STATUS_CONFIRMED,
    ExportInvoice, ExportInvoiceItem, EXPORT_TAX_MODES, EXPORT_TAX_MODE_IGST, EXPORT_TAX_MODE_LUT,
    EXPORT_LOADING_TYPES, EXPORT_LOADING_SELF_SEALING,
    ExportPackingList, ExportPackingListItem, ExportPackingListItemDesign, ExportDesignsPackingList,
    LoadingPlanning, LoadingPlanningItem, LoadingPlanningCarton, LoadingPlanningPallet,
    PackingPlanning, PackingPlanningItem, PackingPlanningManualUnit,
)
from app.repositories import (
    TenantRepository, UserRepositoryBase, LeadRepositoryBase, PartyRepositoryBase, SupplierRepositoryBase,
    TransporterRepositoryBase,
    CommunicationRepository, PaymentRepository, DocumentRepository, CompanyRepository,
    CategoryRepository, ProductRepository, ProductPalletTypeRepository, ProductFolderRepository, DesignRepository,
    QuotationRepository, ProformaInvoiceRepository, PurchaseOrderRepository,
    PurchaseOrderProductionRepository, JobWorkRepository,
    JobOutRepository, JobInRepository,
    PurchaseInvoiceRepository,
    ExportInvoiceRepository, ExportPackingListRepository,
    PackingListRepository, LoadingPlanningRepository, PackingPlanningRepository, DocumentVersionRepository, PermitRepository, BookingDetailRepository, MiscCurrencyRepository, MiscNatureOfContractRepository,
    MiscPortOfLoadingRepository, MiscContainerTypeRepository, MiscHsnCodeRepository, MiscCountryRepository, MiscUnitRepository,
)
from app.database import Database, SCHEMA_VERSION
from app.utils import drops_certification, drops_insurance, drops_sea_freight


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
                 port_of_loading_repo: Optional[MiscPortOfLoadingRepository] = None,
                 container_type_repo: Optional[MiscContainerTypeRepository] = None,
                 hsn_code_repo: Optional[MiscHsnCodeRepository] = None,
                 country_repo: Optional[MiscCountryRepository] = None,
                 unit_repo: Optional[MiscUnitRepository] = None):
        self.currency_repo = currency_repo
        self.nature_of_contract_repo = nature_of_contract_repo
        self.port_of_loading_repo = port_of_loading_repo
        self.container_type_repo = container_type_repo
        self.hsn_code_repo = hsn_code_repo
        self.country_repo = country_repo
        self.unit_repo = unit_repo

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

    # ---- container type --------------------------------------------------
    def list_container_types(self, company_id: int) -> List[MiscContainerType]:
        """Only what the company actually saved - what the Miscellaneous
        page manages."""
        return self.container_type_repo.list_all(company_id)

    def container_type_options(self, company_id: int) -> List[MiscContainerType]:
        """What the Booking Detail dropdown shows: the saved list, or the
        built-in fallback (the list this used to be hard-coded to) while it
        is still empty."""
        stored = self.container_type_repo.list_all(company_id)
        if stored:
            return stored
        return [MiscContainerType(id=None, company_id=company_id, name=name)
                for name in DEFAULT_CONTAINER_TYPES]

    def get_container_type(self, entry_id: int, company_id: int) -> MiscContainerType:
        entry = self.container_type_repo.get_by_id(entry_id)
        if not entry or entry.company_id != company_id:
            raise NotFoundError(f"Container type #{entry_id} not found.")
        return entry

    def _clean_container_type(self, current_user: User, fields: dict) -> MiscContainerType:
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValidationError("Name is compulsory.")
        return MiscContainerType(id=None, company_id=current_user.company_id, name=name)

    def create_container_type(self, current_user: User, fields: dict) -> MiscContainerType:
        entry = self._clean_container_type(current_user, fields)
        if self.container_type_repo.find_by_name(current_user.company_id, entry.name):
            raise ValidationError(f"'{entry.name}' is already on the container type list.")
        return self.container_type_repo.create(entry)

    def update_container_type(self, current_user: User, entry_id: int, fields: dict) -> MiscContainerType:
        self.get_container_type(entry_id, current_user.company_id)
        entry = self._clean_container_type(current_user, fields)
        clash = self.container_type_repo.find_by_name(current_user.company_id, entry.name)
        if clash and clash.id != entry_id:
            raise ValidationError(f"'{entry.name}' is already on the container type list.")
        self.container_type_repo.update(entry_id, entry)
        return self.get_container_type(entry_id, current_user.company_id)

    def delete_container_type(self, current_user: User, entry_id: int) -> MiscContainerType:
        entry = self.get_container_type(entry_id, current_user.company_id)
        self.container_type_repo.delete(entry_id)
        return entry

    # ---- HSN code --------------------------------------------------
    def list_hsn_codes(self, company_id: int) -> List[MiscHsnCode]:
        return self.hsn_code_repo.list_all(company_id)

    def get_hsn_code(self, entry_id: int, company_id: int) -> MiscHsnCode:
        entry = self.hsn_code_repo.get_by_id(entry_id)
        if not entry or entry.company_id != company_id:
            raise NotFoundError(f"HSN code #{entry_id} not found.")
        return entry

    def find_hsn_code(self, company_id: int, name: str) -> Optional[MiscHsnCode]:
        """The saved row matching a submitted HSN code - used to pick up that
        code's GST slab without asking for it a second time."""
        name = (name or "").strip()
        if not name:
            return None
        return self.hsn_code_repo.find_by_name(company_id, name)

    def _clean_hsn_code(self, current_user: User, fields: dict) -> MiscHsnCode:
        name = (fields.get("name") or "").strip()
        gst_slab = (fields.get("gst_slab") or "").strip()
        # Optional - the list is usable with the note left blank.
        related_products = (fields.get("related_products") or "").strip() or None
        if not name:
            raise ValidationError("HSN Code is compulsory.")
        if not gst_slab:
            raise ValidationError("GST Slab is compulsory.")
        return MiscHsnCode(id=None, company_id=current_user.company_id, name=name,
                           gst_slab=gst_slab, related_products=related_products)

    def create_hsn_code(self, current_user: User, fields: dict) -> MiscHsnCode:
        entry = self._clean_hsn_code(current_user, fields)
        if self.hsn_code_repo.find_by_name(current_user.company_id, entry.name):
            raise ValidationError(f"'{entry.name}' is already on the HSN code list.")
        return self.hsn_code_repo.create(entry)

    def update_hsn_code(self, current_user: User, entry_id: int, fields: dict) -> MiscHsnCode:
        self.get_hsn_code(entry_id, current_user.company_id)
        entry = self._clean_hsn_code(current_user, fields)
        clash = self.hsn_code_repo.find_by_name(current_user.company_id, entry.name)
        if clash and clash.id != entry_id:
            raise ValidationError(f"'{entry.name}' is already on the HSN code list.")
        self.hsn_code_repo.update(entry_id, entry)
        return self.get_hsn_code(entry_id, current_user.company_id)

    def delete_hsn_code(self, current_user: User, entry_id: int) -> MiscHsnCode:
        entry = self.get_hsn_code(entry_id, current_user.company_id)
        self.hsn_code_repo.delete(entry_id)
        return entry

    # ---- country --------------------------------------------------
    def list_countries(self, company_id: int) -> List[MiscCountry]:
        return self.country_repo.list_all(company_id)

    def get_country(self, entry_id: int, company_id: int) -> MiscCountry:
        entry = self.country_repo.get_by_id(entry_id)
        if not entry or entry.company_id != company_id:
            raise NotFoundError(f"Country #{entry_id} not found.")
        return entry

    def _clean_country(self, current_user: User, fields: dict) -> MiscCountry:
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValidationError("Country Name is compulsory.")
        return MiscCountry(id=None, company_id=current_user.company_id, name=name)

    def create_country(self, current_user: User, fields: dict) -> MiscCountry:
        entry = self._clean_country(current_user, fields)
        if self.country_repo.find_by_name(current_user.company_id, entry.name):
            raise ValidationError(f"'{entry.name}' is already on the country list.")
        return self.country_repo.create(entry)

    def update_country(self, current_user: User, entry_id: int, fields: dict) -> MiscCountry:
        self.get_country(entry_id, current_user.company_id)
        entry = self._clean_country(current_user, fields)
        clash = self.country_repo.find_by_name(current_user.company_id, entry.name)
        if clash and clash.id != entry_id:
            raise ValidationError(f"'{entry.name}' is already on the country list.")
        self.country_repo.update(entry_id, entry)
        return self.get_country(entry_id, current_user.company_id)

    def delete_country(self, current_user: User, entry_id: int) -> MiscCountry:
        entry = self.get_country(entry_id, current_user.company_id)
        self.country_repo.delete(entry_id)
        return entry

    # ---- unit --------------------------------------------------
    def list_units(self, company_id: int) -> List[MiscUnit]:
        return self.unit_repo.list_all(company_id)

    def get_unit(self, entry_id: int, company_id: int) -> MiscUnit:
        entry = self.unit_repo.get_by_id(entry_id)
        if not entry or entry.company_id != company_id:
            raise NotFoundError(f"Unit #{entry_id} not found.")
        return entry

    def _clean_unit(self, current_user: User, fields: dict) -> MiscUnit:
        name = (fields.get("name") or "").strip()
        meaning = (fields.get("meaning") or "").strip()
        if not name:
            raise ValidationError("Unit is compulsory.")
        if not meaning:
            raise ValidationError("Meaning is compulsory.")
        return MiscUnit(id=None, company_id=current_user.company_id, name=name, meaning=meaning)

    def create_unit(self, current_user: User, fields: dict) -> MiscUnit:
        entry = self._clean_unit(current_user, fields)
        if self.unit_repo.find_by_name(current_user.company_id, entry.name):
            raise ValidationError(f"'{entry.name}' is already on the unit list.")
        return self.unit_repo.create(entry)

    def update_unit(self, current_user: User, entry_id: int, fields: dict) -> MiscUnit:
        self.get_unit(entry_id, current_user.company_id)
        entry = self._clean_unit(current_user, fields)
        clash = self.unit_repo.find_by_name(current_user.company_id, entry.name)
        if clash and clash.id != entry_id:
            raise ValidationError(f"'{entry.name}' is already on the unit list.")
        self.unit_repo.update(entry_id, entry)
        return self.get_unit(entry_id, current_user.company_id)

    def delete_unit(self, current_user: User, entry_id: int) -> MiscUnit:
        entry = self.get_unit(entry_id, current_user.company_id)
        self.unit_repo.delete(entry_id)
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
# PARTY SERVICE (currently just Buyer - constructed with that type's repo
# and parent_type, generic enough to serve more than one type again if
# another one shows up. Supplier has its own SupplierService below since
# its shape has diverged.)
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
        self.parent_type = parent_type  # 'buyer'
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
        return self.parent_type.capitalize()  # 'Buyer' - matches leads.converted_client_type

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
            country=(fields.get("country") or "").strip() or None,
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
        invoice/purchase order/packing list made against the party's
        originating lead (these aren't separate sections here - they're just
        auto-generated document types feeding the same card). Future
        auto-generated document types should feed into this the same way.
        `link` carries its own kwarg dict so each document type's route can
        name its id param however it likes.

        Only Quotation carries its own lead_id - Proforma Invoice, Purchase
        Order and Packing List are found by walking UP their own
        quotation_id/proforma_invoice_id/purchase_order_id chain to that
        Quotation instead (see database.py's SCHEMA_VERSION v67 changelog
        entry)."""
        rows = [
            {
                "name": d.document_name, "type": d.document_type, "date": d.document_date,
                "notes": d.notes, "link": None,
            }
            for d in self.document_repo.list_for(self.parent_type, party.id)
        ]
        quotations = self.quotation_repo.list_for_lead(party.lead_id) if party.lead_id else []
        for q in quotations:
            rows.append({
                "name": q.quotation_number, "type": "Quotation", "date": q.quotation_date,
                "notes": f"{q.buyer_name} · $ {q.invoice_value_usd:,.2f}",
                "link": ("quotations.view_quotation", {"quotation_id": q.id}),
            })

        proforma_invoices = []
        if self.proforma_invoice_repo:
            for q in quotations:
                proforma_invoices.extend(self.proforma_invoice_repo.list_for_quotation(q.id))
            for pi in proforma_invoices:
                rows.append({
                    "name": pi.invoice_number, "type": "Proforma Invoice", "date": pi.invoice_date,
                    "notes": f"{pi.consignee_name} · $ {pi.invoice_value_usd:,.2f}",
                    "link": ("proforma_invoices.view_proforma_invoice", {"proforma_invoice_id": pi.id}),
                })

        purchase_orders = []
        if self.purchase_order_repo:
            for pi in proforma_invoices:
                purchase_orders.extend(self.purchase_order_repo.list_for_proforma(pi.id))
            for po in purchase_orders:
                rows.append({
                    "name": po.po_number, "type": "Purchase Order", "date": po.po_date,
                    "notes": f"{po.seller_name} · ₹ {po.order_value_inr:,.2f}",
                    "link": ("purchase_orders.view_purchase_order", {"purchase_order_id": po.id}),
                })

        if self.packing_list_repo:
            packing_lists = {}
            for q in quotations:
                for pl in self.packing_list_repo.list_for_quotation(q.id):
                    packing_lists[pl.id] = pl
            for pi in proforma_invoices:
                for pl in self.packing_list_repo.list_for_proforma(pi.id):
                    packing_lists[pl.id] = pl
            for po in purchase_orders:
                for pl in self.packing_list_repo.list_for_purchase_order(po.id):
                    packing_lists[pl.id] = pl
            for pl in packing_lists.values():
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
# satellite tables as Buyer, tagged parent_type='supplier'.)
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

        # Branch, SWIFT code and bank address are no longer asked for when a
        # supplier is added, so a row is complete without them. They stay on
        # the model and in the table - an older supplier still holds whatever
        # was typed back then - they are simply not compulsory any more.
        # (CompanyService below keeps demanding all six: Our Company's own
        # bank block is what gets printed on documents.)
        bank_fields = ["bank_name", "account_number", "ifsc_code"]
        bank_labels = {
            "bank_name": "bank name", "account_number": "account number", "ifsc_code": "IFSC code",
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
        is seller_supplier_id, not an originating lead (unlike Buyer,
        whose auto-generated documents are found via lead_id)."""
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
    line with how Buyer/Supplier gate their own edits."""

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
    """Moves the buyer/supplier tied to `lead_id` forward to whatever
    CLIENT_STATUSES stage becomes pending once `document_type` has just
    been generated - e.g. generating a Proforma Invoice clears
    "proforma invoice submission pending" and lands on "purchase order
    submission pending". Every document service calls this same helper
    after create/update; adding a new document type only means registering
    it in models.CLIENT_STATUS_ADVANCE_ON, no other wiring needed.
    `party_repos` maps 'Buyer'/'Supplier' -> that type's repo, so
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


class BookingDetailService:
    """A standalone shipping-booking log under Master Data, with the same
    field shape as an Export Invoice's own "Container details" card
    (booking no. / vessel / voyage, one transporter for the whole booking,
    the container type/count list, and one row per physical container) -
    but owned directly by a buyer rather than any invoice, so a booking can
    be logged on its own. Admin-only, like the rest of the master-data
    directory entities (Transporter, Buyer); the service still keeps
    everything company-scoped."""

    def __init__(self, booking_detail_repo: BookingDetailRepository, buyer_repo: PartyRepositoryBase):
        self.booking_detail_repo = booking_detail_repo
        self.buyer_repo = buyer_repo

    # ---- reads --------------------------------------------------
    def get(self, booking_detail_id: int, company_id: int) -> BookingDetail:
        booking = self.booking_detail_repo.get_by_id(booking_detail_id)
        if not booking or booking.company_id != company_id:
            # 404, not 403 - don't reveal that another company's booking exists.
            raise NotFoundError(f"Booking detail #{booking_detail_id} not found.")
        return booking

    def list_all(self, company_id: int) -> List[BookingDetail]:
        return self.booking_detail_repo.list_all(company_id)

    # ---- validation --------------------------------------------------
    def _build(self, current_user: User, fields: dict) -> BookingDetail:
        try:
            buyer_id = int(fields.get("buyer_id") or 0)
        except (TypeError, ValueError):
            buyer_id = 0
        buyer = self.buyer_repo.get_by_id(buyer_id) if buyer_id else None
        if not buyer or buyer.company_id != current_user.company_id:
            raise ValidationError("Pick a buyer for this booking.")

        return BookingDetail(
            id=None, company_id=current_user.company_id, buyer_id=buyer_id,
            created_by=current_user.id,
            booking_no=(fields.get("booking_no") or "").strip() or None,
            vessel_name=(fields.get("vessel_name") or "").strip() or None,
            voyage_no=(fields.get("voyage_no") or "").strip() or None,
            transporter_name=(fields.get("transporter_name") or "").strip() or None,
        )

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
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None
                      for k in ("container_type", "container_no", "max_permitted_weight", "vehicle_no", "lr_no",
                                "line_seal_no", "rfid_seal_no")}
            tare_raw = (r.get("tare_weight_kg") or "").strip()
            if tare_raw:
                try:
                    values["tare_weight_kg"] = float(tare_raw)
                except ValueError:
                    raise ValidationError("Container details: tare weight must be a number.")
            else:
                values["tare_weight_kg"] = None
            if any(v is not None for v in values.values()):
                rows.append(values)
        return rows

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, containers: list, container_details: list) -> BookingDetail:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can add a booking detail.")
        booking = self._build(current_user, fields)
        booking.containers = self._clean_containers(containers)
        booking.container_details = self._clean_container_details(container_details)
        created = self.booking_detail_repo.create(booking)
        return created

    def update(self, booking_detail_id: int, current_user: User, fields: dict,
               containers: list, container_details: list) -> BookingDetail:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can edit a booking detail.")
        self.get(booking_detail_id, current_user.company_id)  # 404s if missing/another company's
        booking = self._build(current_user, fields)
        booking.containers = self._clean_containers(containers)
        booking.container_details = self._clean_container_details(container_details)
        self.booking_detail_repo.update(booking_detail_id, booking)
        return self.get(booking_detail_id, current_user.company_id)

    def delete(self, booking_detail_id: int, current_user: User) -> BookingDetail:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can delete a booking detail.")
        booking = self.get(booking_detail_id, current_user.company_id)
        self.booking_detail_repo.delete(booking_detail_id)
        return booking


# ============================================================
# STATS SERVICE (powers the admin dashboard)
# ============================================================
class StatsService:
    def __init__(self, user_repo: UserRepositoryBase, lead_repo: LeadRepositoryBase,
                 comm_repo: CommunicationRepository, buyer_repo: PartyRepositoryBase,
                 supplier_repo: SupplierRepositoryBase):
        self.user_repo = user_repo
        self.lead_repo = lead_repo
        self.comm_repo = comm_repo
        self.buyer_repo = buyer_repo
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
        # "Clients" on the dashboard spans both separate entities - a buyer
        # and a supplier each still count as one client.
        all_clients = (
            self.buyer_repo.list_all(company_id)
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
                        (SELECT COUNT(*) FROM suppliers s WHERE s.lead_id IN (SELECT id FROM leads WHERE created_by = u.id)
                           AND s.company_id = ? AND date(s.created_at) BETWEEN date(?) AND date(?))
                   ) AS clients_converted
            FROM users u
            WHERE u.role = 'employee' AND u.company_id = ?
            ORDER BY u.full_name
            """,
            (company_id, start_date, end_date, start_date, end_date,
             company_id, start_date, end_date, company_id, start_date, end_date,
             company_id),
        )
        return [dict(r) for r in rows]

    def payments_received_total(self, company_id: int, start_date: str, end_date: str) -> dict:
        row = self.db.query_one(
            """SELECT COUNT(*) AS payment_count, COALESCE(SUM(ph.amount_inr), 0) AS total_inr
               FROM payment_history ph
               WHERE date(ph.payment_datetime) BETWEEN date(?) AND date(?)
                 AND (
                   (ph.parent_type = 'buyer' AND ph.parent_id IN (SELECT id FROM buyers WHERE company_id = ?))
                   OR (ph.parent_type = 'supplier' AND ph.parent_id IN (SELECT id FROM suppliers WHERE company_id = ?))
                 )""",
            (start_date, end_date, company_id, company_id),
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
                 upload_folder: str, allowed_extensions: set,
                 job_work_repo: Optional["JobWorkRepository"] = None):
        self.category_repo = category_repo
        self.product_repo = product_repo
        self.folder_repo = folder_repo
        self.design_repo = design_repo
        self.pallet_type_repo = pallet_type_repo
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions
        self.job_work_repo = job_work_repo

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

    PACKING_UNIT_KINDS = ("pallet", "carton")

    def _parse_pallet_types(self, pallet_types: Optional[list]) -> List[ProductPalletType]:
        """Validates the raw name/boxes pairs submitted by the product form
        into ProductPalletType rows. Rows left entirely blank are skipped;
        a row with only one half filled in is an error. 'loose' is reserved
        for the built-in no-pallet option every product already has.

        `unit_kind` says which LEVEL of packing the row describes - a 'carton'
        is an inner box that then goes on a pallet, a 'pallet' is what a
        forklift moves. Only Loading Planning reads it; anything else that
        doesn't say defaults to 'pallet', which is what every row was
        implicitly treated as before this existed."""
        parsed = []
        for i, raw in enumerate(pallet_types or [], start=1):
            name = (raw.get("name") or "").strip()
            boxes_raw = (raw.get("boxes_per_pallet") or "").strip()
            weight_raw = (raw.get("weight_kg") or "").strip()
            unit_kind = (raw.get("unit_kind") or "").strip().lower() or "pallet"
            if unit_kind not in self.PACKING_UNIT_KINDS:
                unit_kind = "pallet"
            if not name and not boxes_raw and not weight_raw:
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
            weight = None
            if weight_raw:
                try:
                    weight = float(weight_raw)
                except ValueError:
                    raise ValidationError(f"Pallet type '{name}': weight must be a number.")
                if weight < 0:
                    raise ValidationError(f"Pallet type '{name}': weight cannot be negative.")
            parsed.append(ProductPalletType(
                id=None, company_id=0, product_id=0, name=name, boxes_per_pallet=boxes, weight_kg=weight,
                unit_kind=unit_kind,
            ))
        return parsed

    def pallet_types_for_product(self, product_id: int) -> List[ProductPalletType]:
        return self.pallet_type_repo.list_for_product(product_id)

    def total_job_quantity_for_product(self, product_id: int) -> float:
        """Sum of Job Quantity across every job-work line that produced this
        product, for the read-only total on the Products catalog page."""
        if not self.job_work_repo:
            return 0.0
        return self.job_work_repo.sum_job_quantity_for_product(product_id)

    def pallet_types_by_product(self, company_id: int) -> dict:
        """product_id -> [ProductPalletType, ...] for the whole company in
        one query - what the JSON product list and the document forms use."""
        grouped = {}
        for pt in self.pallet_type_repo.list_all(company_id):
            grouped.setdefault(pt.product_id, []).append(pt)
        return grouped

    def _parse_master_product_id(self, company_id: int, is_job_work_product: bool,
                                  master_product_id, exclude_product_id: Optional[int] = None) -> Optional[int]:
        """The master product a job-work product is made from. Only meaningful
        (and required) when the "Job Work Product" checkbox is ticked; ignored
        otherwise so an unticked checkbox always clears it."""
        if not is_job_work_product:
            return None
        if master_product_id in (None, "", "None"):
            raise ValidationError("Select a master product for a job work product.")
        master_id = int(master_product_id)
        if exclude_product_id is not None and master_id == exclude_product_id:
            raise ValidationError("A product cannot be its own master product.")
        self.get_product(master_id, company_id)  # 404s if missing/another company's
        return master_id

    def create_product(self, current_user: User, product_name: str, description: str,
                        hsn_code: str, igst_percent: str, quantity: str,
                        alternate_quantity: str, quantity_unit: str = "",
                        alternate_quantity_unit: str = "",
                        net_weight_kg: str = "", gross_weight_kg: str = "",
                        pallet_types: Optional[list] = None,
                        category_id=None, price_usd: str = "",
                        is_job_work_product=None, master_product_id=None) -> Product:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not product_name or not product_name.strip():
            raise ValidationError("Product name is compulsory.")
        parsed_pallet_types = self._parse_pallet_types(pallet_types)
        is_job_work = bool(is_job_work_product)
        product = Product(
            id=None, company_id=current_user.company_id, product_name=product_name.strip(),
            category_id=self._parse_category_id(current_user.company_id, category_id),
            description=description or None, hsn_code=hsn_code or None,
            price_usd=self._parse_price(price_usd),
            quantity_unit=self._parse_unit(quantity_unit, default="PCS"),
            quantity=quantity or None,
            alternate_quantity_unit=self._parse_unit(alternate_quantity_unit, default="SQM"),
            alternate_quantity=alternate_quantity or None,
            net_weight_kg=self._parse_weight("Net weight", net_weight_kg),
            gross_weight_kg=self._parse_weight("Gross weight", gross_weight_kg),
            is_job_work_product=is_job_work,
            master_product_id=self._parse_master_product_id(current_user.company_id, is_job_work, master_product_id),
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
                        category_id=None, price_usd: str = "",
                        is_job_work_product=None, master_product_id=None) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        if not product_name or not product_name.strip():
            raise ValidationError("Product name is compulsory.")
        self.get_product(product_id, current_user.company_id)
        parsed_pallet_types = self._parse_pallet_types(pallet_types)
        is_job_work = bool(is_job_work_product)
        self.product_repo.update(product_id, {
            "product_name": product_name.strip(), "description": description or None,
            "hsn_code": hsn_code or None,
            "price_usd": self._parse_price(price_usd),
            "category_id": self._parse_category_id(current_user.company_id, category_id),
            "quantity_unit": self._parse_unit(quantity_unit, default="PCS"),
            "quantity": quantity or None,
            "alternate_quantity_unit": self._parse_unit(alternate_quantity_unit, default="SQM"),
            "alternate_quantity": alternate_quantity or None,
            "net_weight_kg": self._parse_weight("Net weight", net_weight_kg),
            "gross_weight_kg": self._parse_weight("Gross weight", gross_weight_kg),
            "is_job_work_product": is_job_work,
            "master_product_id": self._parse_master_product_id(
                current_user.company_id, is_job_work, master_product_id, exclude_product_id=product_id),
            **self._tax_fields(igst_percent),
        })
        self.pallet_type_repo.replace_for_product(current_user.company_id, product_id, parsed_pallet_types)

    def duplicate_product(self, current_user: User, product_id: int) -> Product:
        """Creates a second, independent product carrying over every catalog
        field (HSN/IGST, packing spec, pallet types) of an existing one, so a
        near-identical product doesn't have to be retyped from scratch. Only
        the name changes (" (copy)" appended) so the two are told apart in
        the listing; designs are not copied - they carry their own price and
        photos and are added fresh under the new product."""
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can manage the product catalog.")
        source = self.get_product(product_id, current_user.company_id)
        copy = dataclasses.replace(source, id=None, product_name=f"{source.product_name} (copy)")
        created = self.product_repo.create(copy)
        source_pallet_types = self.pallet_type_repo.list_for_product(product_id)
        if source_pallet_types:
            copies = [
                dataclasses.replace(pt, id=None, product_id=0) for pt in source_pallet_types
            ]
            self.pallet_type_repo.replace_for_product(current_user.company_id, created.id, copies)
        return created

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
    categories, designs) and adds the stock numbers on top.

    Stock per design moves in three ways:

      RECEIVED  `boxes`/`pcs`/`quantity` - the raw received totals, listed on
                a PURCHASE INVOICE's own packing list (bought_totals_by_design).
      DISPATCHED `dispatched_boxes`/`dispatched_quantity` - received goods
                sent back out for job work, i.e. belonging to an invoice that
                has a Job Out challan against it (dispatched_totals_by_design).
      RETURNED  goods coming BACK from job work, received on a Job In
                (received_back_totals_by_design). These land on the JOBBED
                product's designs, not the master's - which is the whole
                point of the cycle: the master goes out, something else comes
                back. Counted into `boxes`/`quantity` alongside what was
                purchased, since both are stock genuinely on hand.
      SOLD      `sold_boxes`/`sold_quantity` - allocated on an export
                invoice's Designs Packing List (sold_totals_by_design).

    `net_boxes`/`net_pcs`/`net_quantity` are received (purchased + returned)
    less dispatched less sold. pcs is tracked on neither the dispatched nor
    the sold side, so net_pcs is netted proportionally off the received
    pcs-per-box ratio."""

    def __init__(self, product_service: "ProductService", packing_list_repo: PackingListRepository,
                 design_repo: DesignRepository,
                 purchase_order_repo: Optional[PurchaseOrderRepository] = None,
                 export_invoice_repo: Optional[ExportInvoiceRepository] = None,
                 purchase_invoice_repo: Optional[PurchaseInvoiceRepository] = None,
                 job_in_repo: Optional[JobInRepository] = None):
        self.products = product_service
        self.packing_list_repo = packing_list_repo
        self.design_repo = design_repo
        # Goods returned from job work - the jobbed product's way into stock.
        # Optional so an unwired caller simply sees no returns.
        self.job_in_repo = job_in_repo
        # Optional: only needed for stock_history_summary's PO Qty/Sale Qty
        # columns and purchase_sale_history's document links - the rest of
        # this service works without them.
        self.purchase_order_repo = purchase_order_repo
        self.export_invoice_repo = export_invoice_repo
        self.purchase_invoice_repo = purchase_invoice_repo

    def _stock_from_bought(self, bought: dict, sold: Optional[dict] = None,
                           dispatched: Optional[dict] = None) -> dict:
        """Turn a {boxes, pcs, quantity} received total into a stock figure
        carrying the raw received totals and the same figures net of what has
        been dispatched for job work and what has been sold. pcs is tracked on
        neither of those sides, so net_pcs is netted proportionally off the
        received pcs-per-box ratio - the same fraction of boxes gone is
        treated as that fraction of pcs gone, so net_pcs reaches zero exactly
        when net_boxes does.

        This is the ONE place stock arithmetic happens; stock_history_summary
        reads its result rather than repeating the subtraction."""
        sold = sold or {}
        dispatched = dispatched or {}
        bought_boxes = bought.get("boxes", 0) or 0
        bought_pcs = bought.get("pcs", 0) or 0
        bought_quantity = bought.get("quantity", 0) or 0
        sold_boxes = sold.get("boxes", 0) or 0
        sold_quantity = sold.get("quantity", 0) or 0
        dispatched_boxes = dispatched.get("boxes", 0) or 0
        dispatched_quantity = dispatched.get("quantity", 0) or 0
        net_boxes = bought_boxes - dispatched_boxes - sold_boxes
        pcs_per_box = (bought_pcs / bought_boxes) if bought_boxes else 0
        return {
            "boxes": bought_boxes,
            "pcs": bought_pcs,
            "quantity": bought_quantity,
            "dispatched_boxes": dispatched_boxes,
            "dispatched_quantity": dispatched_quantity,
            "sold_boxes": sold_boxes,
            "sold_quantity": sold_quantity,
            "net_boxes": net_boxes,
            "net_pcs": round(net_boxes * pcs_per_box, 2) if bought_boxes else bought_pcs,
            "net_quantity": bought_quantity - dispatched_quantity - sold_quantity,
            "unit": bought.get("unit") or None,  # the quantity's unit (SQM/PCS/...)
            "qty_unit": bought.get("qty_unit") or None,  # the boxes' unit (product.quantity_unit)
        }

    def _received_totals(self, company_id: int) -> dict:
        """design_id -> {boxes, pcs, quantity, unit, qty_unit} of everything
        genuinely on hand's way IN: goods purchased (a purchase invoice's
        packing list) plus goods returned from job work (a Job In). The two
        land on different designs in the normal case - the master's on the
        purchase side, the jobbed product's on the return side - but they are
        summed rather than assumed disjoint, so a product that is both bought
        directly and produced by job work totals correctly."""
        totals = dict(self.packing_list_repo.bought_totals_by_design(company_id))
        if not self.job_in_repo:
            return totals
        for design_id, returned in self.job_in_repo.received_back_totals_by_design(company_id).items():
            existing = totals.get(design_id)
            if existing is None:
                totals[design_id] = dict(returned)
                continue
            totals[design_id] = {
                "boxes": (existing.get("boxes") or 0) + (returned.get("boxes") or 0),
                "pcs": (existing.get("pcs") or 0) + (returned.get("pcs") or 0),
                "quantity": (existing.get("quantity") or 0) + (returned.get("quantity") or 0),
                "unit": existing.get("unit") or returned.get("unit"),
                "qty_unit": existing.get("qty_unit") or returned.get("qty_unit"),
            }
        return totals

    def stock_by_design(self, company_id: int) -> dict:
        """design_id -> {boxes, pcs, quantity, dispatched_boxes,
        dispatched_quantity, sold_boxes, sold_quantity, net_boxes, net_pcs,
        net_quantity} on hand, for the whole company in one query. Designs
        never received are simply absent."""
        totals = self._received_totals(company_id)
        sold_totals = self.export_invoice_repo.sold_totals_by_design(company_id) if self.export_invoice_repo else {}
        dispatched_totals = self.packing_list_repo.dispatched_totals_by_design(company_id)
        return {design_id: self._stock_from_bought(bought, sold_totals.get(design_id),
                                                   dispatched_totals.get(design_id))
                for design_id, bought in totals.items()}

    def stock_for_design(self, company_id: int, design_id: int) -> dict:
        """Current stock for a single design (zeros when never received)."""
        return self.stock_by_design(company_id).get(
            design_id, {"boxes": 0, "pcs": 0, "quantity": 0,
                        "dispatched_boxes": 0, "dispatched_quantity": 0,
                        "sold_boxes": 0, "sold_quantity": 0,
                        "net_boxes": 0, "net_pcs": 0, "net_quantity": 0,
                        "unit": None, "qty_unit": None}
        )

    def in_stock_designs(self, company_id: int) -> List[dict]:
        """Every design ever bought in, newest-purchase concerns aside - just
        design + product name + stock, for the "in stock right now" summary
        at the top of the Inventory catalog root. Shown even once net stock
        has been sold down to zero, so the Qty (raw purchased) and Stock
        (net of sales) columns both stay visible for a design that's fully
        sold out rather than the row disappearing. One batched design
        lookup, no per-design queries."""
        totals = self._received_totals(company_id)
        if not totals:
            return []
        sold_totals = self.export_invoice_repo.sold_totals_by_design(company_id) if self.export_invoice_repo else {}
        dispatched_totals = self.packing_list_repo.dispatched_totals_by_design(company_id)
        rows = self.design_repo.list_by_ids_with_product(list(totals.keys()))
        in_stock = []
        for row in rows:
            stock = self._stock_from_bought(totals.get(row["id"], {}), sold_totals.get(row["id"]),
                                            dispatched_totals.get(row["id"]))
            if stock["boxes"] or stock["pcs"] or stock["quantity"]:
                in_stock.append({**row, "stock": stock})
        return in_stock

    def purchase_sale_history(self, company_id: int, design_id: int) -> List[dict]:
        """The design's Purchase / Sale history, one row per Purchase Order
        the design was received against, each carrying its own Purchase
        Invoice(s), Received/PO Remain Qty, and whichever Export Invoice(s)
        that PO's goods were eventually sold on - PO20260815001 -> PINV.. ->
        EXP/25-26/002 on one line, the way the buy and sell side of the same
        stock actually connect. A sale is attached to a PO's row if EITHER
        signal matches: the sale's own PO number, or one of the PO's own
        Purchase Invoice numbers appearing in the sale's Purchase Details -
        so the chain still joins even when a sale only carries the
        PI leg (goods line prefilled from the PI's own numbers) rather than
        the PO number. A sale that matches neither (older data, or a
        hand-typed PO number) still appears, as its own row with blank
        purchase columns - nothing is dropped. purchase_order_repo/
        purchase_invoice_repo/export_invoice_repo are optional on this
        service, so a caller that never wired them just sees an empty list
        instead of an error."""
        sales = self.export_invoice_repo.sold_history_for_design(company_id, design_id) if self.export_invoice_repo else []
        sales_by_po: dict = {}
        sales_by_pi_number: dict = {}
        for sale in sales:
            for po_number in sale.get("po_numbers") or []:
                sales_by_po.setdefault(po_number, []).append(sale)
            for pi_number in sale.get("pi_invoice_numbers") or []:
                sales_by_pi_number.setdefault(pi_number, []).append(sale)

        rows = []
        matched_sale_ids = set()
        if self.purchase_order_repo:
            for row in self.purchase_order_repo.purchase_history_for_design(company_id, design_id):
                ordered = row["po_ordered_boxes"] or 0
                tagged = row["po_product_tagged_boxes"] or 0
                po_qty = (row["received_boxes"] / tagged * ordered) if tagged else 0
                row["po_qty"] = round(po_qty, 2)
                row["po_remain_boxes"] = round(po_qty - (row["received_boxes"] or 0), 2)
                purchase_invoices = (
                    self.purchase_invoice_repo.list_for_purchase_order(row["purchase_order_id"])
                    if self.purchase_invoice_repo else []
                )
                row["purchase_invoices"] = purchase_invoices

                matched_sales = list(sales_by_po.get(row["po_number"], []))
                for pinv in purchase_invoices:
                    for sale in sales_by_pi_number.get(pinv.invoice_number, []):
                        if sale not in matched_sales:
                            matched_sales.append(sale)
                row["sales"] = matched_sales
                matched_sale_ids.update(id(s) for s in matched_sales)
                rows.append(row)
        unmatched_sales = [s for s in sales if id(s) not in matched_sale_ids]
        for sale in unmatched_sales:
            rows.append({
                "purchase_order_id": None, "po_number": None, "po_date": None,
                "packing_list_number": None, "po_qty": None, "purchase_invoices": [],
                "received_boxes": None, "po_remain_boxes": None, "qty_unit": None,
                "sales": [sale],
            })
        return rows

    def stock_history_summary(self, company_id: int, design_id: int) -> dict:
        """The design's Stock History card: one row of totals - PO Qty
        (ordered), Received Qty (listed on a purchase invoice's packing list),
        PO Remain Qty, Dispatched Qty (sent back out on a Job Out), Sale Qty
        (sold via an export invoice), Stock and its Alt Qty.

        Stock/Alt Qty come straight off stock_for_design rather than being
        subtracted again here, so this card and the Inventory card above it
        can't disagree - _stock_from_bought is the single formula.

        PO Qty and Sale Qty are only as complete as the PO/export invoice
        lines that were actually tagged with this design (design tagging on
        those two document types is optional) - untagged lines simply don't
        count, the same way stock itself only counts packing lists with a
        design chosen. purchase_order_repo/export_invoice_repo are optional
        on this service, so a caller that never wired them just sees zeros
        for those two columns instead of an error.

        Note PO Qty stays purchase-ORDER based while Received Qty is now
        purchase-INVOICE based, so PO Remain Qty reads as "ordered but not yet
        invoiced" - which is what it means."""
        stock = self.stock_for_design(company_id, design_id)
        ordered = (self.purchase_order_repo.ordered_totals_by_design(company_id).get(design_id)
                   if self.purchase_order_repo else None) or {"boxes": 0, "quantity": 0, "qty_unit": None, "unit": None}
        return {
            "po_boxes": ordered["boxes"],
            "po_qty_unit": ordered["qty_unit"] or stock["qty_unit"],
            "received_boxes": stock["boxes"],
            "received_qty_unit": stock["qty_unit"],
            "po_remain_boxes": (ordered["boxes"] - stock["boxes"]) if ordered["boxes"] else None,
            "dispatched_boxes": stock["dispatched_boxes"],
            "dispatched_qty_unit": stock["qty_unit"],
            "sale_boxes": stock["sold_boxes"],
            "sale_qty_unit": stock["qty_unit"],
            "stock_boxes": stock["net_boxes"],
            "stock_qty_unit": stock["qty_unit"],
            "stock_alt_qty": stock["net_quantity"],
            "alt_unit": stock["unit"],
        }


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
    "job_work": (JobWork, JobWorkItem, "job_work_number"),
    # A job out has no line items of its own (its goods table is read live
    # off its purchase invoice), so the item class here is never used -
    # get_version's items list always comes back empty.
    "job_out": (JobOut, JobOut, "delivery_challan_no"),
    "job_in": (JobIn, JobInItem, "stock_inward_no"),
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
            shipping_mode=(fields.get("shipping_mode") or "").strip() or None,
            shipping_terms=shipping_terms,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            price_validity_days=_int("price_validity_days", 30),
            remarks=(fields.get("remarks") or "").strip() or None,
            sea_freight=0 if no_sea_freight else _float("sea_freight", 0),
            insurance=0 if no_insurance else _float("insurance", 0),
            certification=0 if no_certification else _float("certification", 0),
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

    @staticmethod
    def _clean_containers(raw) -> List[dict]:
        """Same shape/cleaning as BookingDetailService._clean_containers - a
        row is kept once it has a type or a count, count never goes negative."""
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

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list, raw_containers: Optional[list] = None) -> Quotation:
        items = self._build_items(current_user.company_id, raw_items)
        quotation = self._build_header(current_user, fields, items)
        quotation.containers = self._clean_containers(raw_containers)
        quotation.quotation_number = self._generate_number(current_user.company_id, quotation.quotation_date)
        created = self.quotation_repo.create(quotation)
        self.version_service.record("quotation", created, current_user.id)
        self._advance_lead_to_in_client(created.lead_id)
        return created

    def update(self, current_user: User, quotation_id: int, fields: dict, raw_items: list,
               raw_containers: Optional[list] = None) -> Quotation:
        existing = self.get(quotation_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        quotation = self._build_header(current_user, fields, items)
        quotation.containers = self._clean_containers(raw_containers)
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
            containers=[dict(c) for c in source.containers],
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
        self.party_repos = party_repos  # {'Buyer': ..., 'Supplier': ...} for advance_client_status

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
        """A proforma invoice has no lead_id of its own - it's reached by
        walking up its quotation_id to the Quotation, which is the only
        document type that still carries lead_id directly. Unscoped by
        company_id because the caller already owns the lead/client."""
        if not lead_id:
            return []
        quotations = self.quotation_repo.list_for_lead(lead_id)
        invoices = []
        for quotation in quotations:
            invoices.extend(self.invoice_repo.list_for_quotation(quotation.id))
        return invoices

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
            "consignee_name": quotation.buyer_name,
            "consignee_address": quotation.buyer_address,
            "buyer_order_no": quotation.buyer_reference_no,
            "port_of_loading": quotation.port_of_loading,
            "port_of_discharge": quotation.port_of_discharge,
            "final_destination": quotation.final_destination,
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
        containers = [dict(c) for c in quotation.containers]
        return {"fields": fields, "items": items, "containers": containers}

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

        quotation_id = int(fields["quotation_id"]) if fields.get("quotation_id") else None
        linked_quotation = None
        if quotation_id is not None:
            # Only trust a quotation from this same company - otherwise a crafted
            # quotation_id could attach this invoice to another company's quotation.
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
            consignee_name=consignee_name, created_by=current_user.id,
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
            packing_details=(fields.get("packing_details") or "").strip() or None,
            terms_of_delivery=terms_of_delivery,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            sea_freight=0 if no_sea_freight else _float("sea_freight", 0),
            insurance=0 if no_insurance else _float("insurance", 0),
            certification=0 if no_certification else _float("certification", 0),
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

    @staticmethod
    def _clean_containers(raw) -> List[dict]:
        """Same shape/cleaning as QuotationService._clean_containers."""
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

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list, raw_containers: Optional[list] = None) -> ProformaInvoice:
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        invoice.containers = self._clean_containers(raw_containers)
        invoice.invoice_number = self._generate_number(current_user.company_id, invoice.invoice_date)
        created = self.invoice_repo.create(invoice)
        self.version_service.record("proforma_invoice", created, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo,
                                   self._lead_id_for_quotation(created.quotation_id), "proforma_invoice")
        return created

    def update(self, current_user: User, invoice_id: int, fields: dict, raw_items: list,
               raw_containers: Optional[list] = None) -> ProformaInvoice:
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        invoice = self._build_header(current_user, fields, items)
        invoice.containers = self._clean_containers(raw_containers)
        self.invoice_repo.update(invoice_id, invoice)
        updated = self.get(invoice_id, current_user.company_id)
        self.version_service.record("proforma_invoice", updated, current_user.id)
        if self.party_repos:
            advance_client_status(self.party_repos, self.lead_repo,
                                   self._lead_id_for_quotation(updated.quotation_id), "proforma_invoice")
        return updated

    def _lead_id_for_quotation(self, quotation_id: Optional[int]) -> Optional[int]:
        """A proforma invoice has no lead_id of its own - advance_client_status
        needs the lead its linked Quotation (if any) was made against."""
        if not quotation_id:
            return None
        quotation = self.quotation_repo.get_by_id(quotation_id)
        return quotation.lead_id if quotation else None

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
                 misc_list_service: Optional["MiscListService"] = None,
                 quotation_repo: Optional[QuotationRepository] = None):
        self.misc_list_service = misc_list_service
        self.purchase_order_repo = purchase_order_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.version_service = version_service
        # Used only to resolve advance_client_status's lead_id by walking
        # proforma_invoice_id -> quotation_id -> Quotation.lead_id, since a
        # purchase order no longer carries its own lead_id.
        self.quotation_repo = quotation_repo
        self.party_repos = party_repos  # {'Buyer': ..., 'Supplier': ...} for advance_client_status
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

        proforma_invoice_id = int(fields["proforma_invoice_id"]) if fields.get("proforma_invoice_id") else None
        if proforma_invoice_id is not None:
            # Only trust a proforma invoice from this same company - same reasoning as seller_supplier_id below.
            invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
            if not invoice or invoice.company_id != current_user.company_id:
                proforma_invoice_id = None

        seller_supplier_id = int(fields["seller_supplier_id"]) if fields.get("seller_supplier_id") else None
        if seller_supplier_id is not None and self.supplier_repo is not None:
            # Only trust a supplier from this same company - same reasoning as proforma_invoice_id above.
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
            seller_name=seller_name, created_by=current_user.id,
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
            tax_as_actual=str(fields.get("tax_as_actual") or "").lower() in ("1", "true", "on", "yes"),
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
            advance_client_status(self.party_repos, self.lead_repo,
                                   self._lead_id_for_proforma(created.proforma_invoice_id), "purchase_order")
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
            advance_client_status(self.party_repos, self.lead_repo,
                                   self._lead_id_for_proforma(updated.proforma_invoice_id), "purchase_order")
        return updated

    def delete(self, current_user: User, purchase_order_id: int) -> None:
        existing = self.get(purchase_order_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.purchase_order_repo.delete(purchase_order_id)

    def _lead_id_for_proforma(self, proforma_invoice_id: Optional[int]) -> Optional[int]:
        """A purchase order has no lead_id of its own - advance_client_status
        needs the lead behind its linked Proforma Invoice's own Quotation, if
        any (PO -> Proforma Invoice -> Quotation -> lead_id)."""
        if not proforma_invoice_id or not self.quotation_repo:
            return None
        invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
        if not invoice or not invoice.quotation_id:
            return None
        quotation = self.quotation_repo.get_by_id(invoice.quotation_id)
        return quotation.lead_id if quotation else None


# ============================================================
# PURCHASE ORDER PRODUCTION STATUS
# ============================================================
class PurchaseOrderProductionService:
    """The Production Status card on a purchase order's preview page: per
    DESIGN a hand-set status, plus the batches it was actually produced in.
    Screen-only - none of this touches the printed purchase order or the
    combined document.

    A purchase order orders by PRODUCT; which designs those boxes are is only
    ever settled on the linked proforma invoice's packing list - the same
    breakdown the sheet's own PACKING DETAILS block prints (see
    purchase_orders._packing_details_rows, whose rows this takes as
    `design_rows`). A product with no such breakdown keeps one design-less
    row, so a hand-typed line still gets a status."""

    def __init__(self, production_repo: PurchaseOrderProductionRepository,
                 purchase_order_service: PurchaseOrderService):
        self.production_repo = production_repo
        # Used for its company scoping: every read and write here goes
        # through its get(), which 404s on another company's order.
        self.purchase_order_service = purchase_order_service

    # ---- reads --------------------------------------------------
    @staticmethod
    def _product_key(product_id, product_name):
        """Same key _packing_details_rows matches PO lines to packing list
        lines on: the catalog id when there is one, else the typed name."""
        return product_id or (product_name or "").strip().upper()

    def _designs_for_item(self, item, design_rows) -> List[dict]:
        """The designs of one PO line, in packing-list order, each with the
        boxes packed for it. Several packing list lines can name the same
        design (one per pallet split), so they are merged."""
        wanted = self._product_key(item.product_id, item.product_name)
        merged = {}
        for row in design_rows or []:
            if self._product_key(row.product_id, row.product_name) != wanted:
                continue
            if not row.design_id and not row.design_name:
                continue
            key = row.design_id or (row.design_name or "").strip().upper()
            entry = merged.setdefault(key, {"design_id": row.design_id,
                                            "design_name": row.design_name, "boxes": 0.0})
            entry["boxes"] += row.quantity_boxes or 0
        return list(merged.values())

    def get_rows(self, purchase_order_id: int, company_id: int, design_rows=None) -> List[dict]:
        """One display row per (line, design), in the order the sheet prints
        its lines: the design being made, the boxes ordered of it, the
        hand-set status, and the batches under it with their produced total.
        `design_rows` is the packing-list breakdown; without it every line
        falls back to a single design-less row carrying the whole quantity."""
        purchase_order = self.purchase_order_service.get(purchase_order_id, company_id)
        production = self.production_repo.map_for_purchase_order(purchase_order_id)
        rows = []
        for item in purchase_order.items:
            designs = self._designs_for_item(item, design_rows)
            if not designs:
                designs = [{"design_id": None, "design_name": item.design_name,
                            "boxes": item.quantity_boxes or 0}]
            for design in designs:
                key = (item.id,) + self.production_repo.design_key(
                    design["design_id"], design["design_name"])
                record = production.get(key) or PurchaseOrderItemProduction(
                    purchase_order_item_id=item.id, design_id=design["design_id"],
                    design_name=design["design_name"])
                rows.append({
                    "item_id": item.id,
                    "design_id": design["design_id"],
                    # design_name is the design's IDENTITY - half of the key a
                    # row is stored under, and empty for a line with no design
                    # breakdown at all. design_label is only what to print, so
                    # such a line still shows something (its product's name).
                    # Posting the label back as the identity would file the
                    # save under a key the next read can't find.
                    "design_name": design["design_name"],
                    "design_label": design["design_name"] or item.product_name,
                    "product_name": item.product_name,
                    "ordered_boxes": design["boxes"],
                    "quantity_unit": item.quantity_unit,
                    "status": record.status,
                    "status_label": PRODUCTION_STATUSES.get(record.status, record.status),
                    "batches": record.batches,
                    "produced_boxes": record.produced_boxes,
                    "updated_by_name": record.updated_by_name,
                    "updated_at": record.updated_at,
                })
        return rows

    def summary_map(self, company_id: int) -> dict:
        """purchase_order_id -> {'ready': n, 'total': n} for the list page."""
        return self.production_repo.summary_map_for_company(company_id)

    # ---- writes -------------------------------------------------
    def save_row(self, purchase_order_id: int, purchase_order_item_id: int,
                 design_id: Optional[int], design_name: Optional[str], status: str,
                 batches: List[dict], company_id: int, user_id: Optional[int] = None) -> None:
        """Saves one design's status and its full set of batches. `batches`
        is the raw form data - dicts of batch_number/production_date/
        quantity_boxes/remarks - which is normalised here."""
        purchase_order = self.purchase_order_service.get(purchase_order_id, company_id)
        if purchase_order_item_id not in {item.id for item in purchase_order.items}:
            raise NotFoundError("That line is not part of this purchase order.")
        status = (status or DEFAULT_PRODUCTION_STATUS).strip()
        if status not in PRODUCTION_STATUSES:
            raise ValidationError(f"'{status}' is not a valid production status.")
        self.production_repo.save_for_design(
            purchase_order_item_id, design_id, (design_name or "").strip() or None,
            status, self._clean_batches(batches), user_id,
        )

    @staticmethod
    def _clean_batches(batches: List[dict]) -> List[PurchaseOrderItemBatch]:
        """Drops the rows the user left entirely blank (the form always
        carries a spare one) and coerces the rest. A quantity that isn't a
        number is a typo worth reporting, not something to silently zero."""
        cleaned = []
        for sr_no, raw in enumerate(batches or [], start=1):
            batch_number = (raw.get("batch_number") or "").strip()
            production_date = (raw.get("production_date") or "").strip()
            remarks = (raw.get("remarks") or "").strip()
            quantity_raw = str(raw.get("quantity_boxes") or "").strip()
            if not (batch_number or production_date or remarks or quantity_raw):
                continue
            try:
                quantity = float(quantity_raw) if quantity_raw else 0.0
            except ValueError:
                raise ValidationError(f"Batch quantity '{quantity_raw}' is not a number.")
            if quantity < 0:
                raise ValidationError("Batch quantity cannot be negative.")
            cleaned.append(PurchaseOrderItemBatch(
                id=None, purchase_order_item_id=None, sr_no=sr_no, design_id=None,
                batch_number=batch_number or None,
                production_date=production_date or None,
                quantity_boxes=quantity,
                remarks=remarks or None,
            ))
        return cleaned


# ============================================================
# JOB WORK SERVICE
# ============================================================
class JobWorkService:
    """The JOB WORK document: a proforma invoice's goods handed on to be
    worked on, normally as a size CONVERSION. Mirrors PurchaseOrderService
    layer for layer, with the differences that follow from what job work
    actually is:

      * its lines are DESIGNS, not products, picked in the Job Manufacturer
        card - "To Product" first (the WHOLE catalog; its own designs are
        what the Design column offers) and "Product" second (the proforma
        invoice's own products, kept only to look up each design's
        source_quantity - see _invoice_quantities);
      * it carries two parties, the FROM SELLER whose goods go out and the
        JOB MANUFACTURER who does the work - both Suppliers;
      * no money at all: Job Quantity is the document's one figure per
        design, and it is entirely DERIVED (see JobWorkItem's own docstring
        for the source_quantity -> conversion_value/extra_percent ->
        job_quantity chain) - nothing here is typed in directly."""

    def __init__(self, job_work_repo: JobWorkRepository, proforma_invoice_repo: ProformaInvoiceRepository,
                 packing_list_repo: PackingListRepository, product_repo: ProductRepository,
                 version_service: "DocumentVersionService",
                 supplier_repo: Optional[SupplierRepositoryBase] = None,
                 misc_list_service: Optional["MiscListService"] = None,
                 company_repo: Optional[CompanyRepository] = None,
                 purchase_order_repo: Optional[PurchaseOrderRepository] = None):
        self.job_work_repo = job_work_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        # An invoice sells by product; its designs live on a packing list.
        self.packing_list_repo = packing_list_repo
        self.product_repo = product_repo
        self.version_service = version_service
        # Validates that both parties - the From Seller and the Job
        # Manufacturer, each a Supplier - belong to this company.
        self.supplier_repo = supplier_repo
        self.misc_list_service = misc_list_service
        # Our own GSTIN, for the Products card's intra/inter-state tax split
        # against the Job Manufacturer's GSTIN - same use PurchaseOrderService
        # makes of it against the seller's.
        self.company_repo = company_repo
        # A job work now prints and numbers as a purchase order (see
        # _generate_number) - optional so a caller that never wires it just
        # falls back to counting only against other job works.
        self.purchase_order_repo = purchase_order_repo

    # ---- reads --------------------------------------------------
    def get(self, job_work_id: int, company_id: int) -> JobWork:
        job_work = self.job_work_repo.get_by_id(job_work_id)
        if not job_work or job_work.company_id != company_id:
            # 404, not 403 - don't reveal that another company's job work exists.
            raise NotFoundError(f"Job work #{job_work_id} not found.")
        return job_work

    def list_all(self, company_id: int) -> List[JobWork]:
        return self.job_work_repo.list_all(company_id)

    def list_for_proforma(self, proforma_invoice_id: Optional[int], company_id: int) -> List[JobWork]:
        """Every job work raised against this proforma invoice, newest first.
        company_id is re-checked here because the caller passes an id taken
        straight off an invoice."""
        if not proforma_invoice_id:
            return []
        return [jw for jw in self.job_work_repo.list_for_proforma(proforma_invoice_id)
                if jw.company_id == company_id]

    def count_map_by_proforma(self, company_id: int) -> dict:
        """proforma_invoice_id -> number of job works raised against it, for
        the proforma invoice list page's Job work column."""
        return self.job_work_repo.count_map_by_proforma(company_id)

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, job_work: JobWork, current_user: User):
        if current_user.is_admin:
            return
        if job_work.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage job works you created yourself.")

    # ---- number generation --------------------------------------------------
    def _generate_number(self, company_id: int, job_work_date: str) -> str:
        """PO{YYYYMMDD}{seq} - a job work now prints as, and is numbered as,
        a regular purchase order (see _sheet.html's title/label), so it
        shares the same daily counter as PurchaseOrderService._generate_number
        rather than its own JW-prefixed sequence: seq is that day's purchase
        order count PLUS that day's job work count, plus 1, zero-padded to 3
        digits (e.g. PO20260817004)."""
        date_part = (job_work_date or "")[:10].replace("-", "")
        prefix = f"PO{date_part}"
        po_count = self.purchase_order_repo.count_for_date_prefix(company_id, prefix) \
            if self.purchase_order_repo else 0
        jw_count = self.job_work_repo.count_for_date_prefix(company_id, prefix)
        seq = po_count + jw_count + 1
        return f"{prefix}{seq:03d}"

    # ---- the proforma invoice's own products (the "Product" picker) --------
    def invoice_products(self, invoice: ProformaInvoice) -> list:
        """The invoice's own product lines, as plain dicts:
        {product_id, product_name, hsn_code} - the "Product" dropdown's
        options (the SOURCE side, second in the Job Manufacturer card), kept
        only so a design's source_quantity can be looked up against it. Not
        the design picker - that comes from whichever "To Product" is chosen,
        the WHOLE catalog's own designs, not the invoice's."""
        seen: dict = {}
        for item in invoice.items:
            key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
            if key not in seen:
                seen[key] = {
                    "product_id": item.product_id, "product_name": item.product_name,
                    "hsn_code": item.hsn_code,
                }
        return sorted(seen.values(), key=lambda r: (r["product_name"] or "").upper())

    # ---- source_quantity lookup (fetched from the invoice, not typed) ------
    def invoice_quantities(self, company_id: int, invoice: ProformaInvoice) -> tuple:
        """Two lookups for source_quantity, built off the same pass over the
        invoice's own packing lists (restricted to the products the invoice
        actually carries), each summing quantity_boxes across however many
        packing lists a (product, design) is split over:

          by_product_and_design  (product_key, normalized design name) ->
                                  {design_name, quantity_boxes, quantity_unit}
                                  - exact, for when a "Product" has been
                                  picked to disambiguate.
          by_design_only         normalized design name -> same shape, but
                                  ONLY for a design name that appears under
                                  exactly ONE product on this invoice - the
                                  fallback used when no "Product" is picked
                                  (or it doesn't match). A design shared
                                  across several products (a common case in
                                  this catalog: the same finish sold in more
                                  than one size, e.g. "CELESTE BLUE" on both
                                  a 600X1200 and a 96X1200 product) is
                                  deliberately left OUT of this map rather
                                  than summed across them - summing would
                                  silently manufacture a wrong total (960 +
                                  200 read as 1160 for either one alone), so
                                  an ambiguous design name simply falls
                                  through to requiring the exact match.

        Matched by design NAME rather than design_id in both cases - the
        same design carries a different catalog id under the "To Product"
        it's being converted to, since that is a different product's own
        design list entirely."""
        invoice_keys = {_product_key({"product_id": item.product_id, "product_name": item.product_name})
                        for item in invoice.items}
        packing_lists = [pl for pl in self.packing_list_repo.list_for_proforma(invoice.id)
                         if pl.company_id == company_id]

        by_product_and_design: dict = {}
        for packing_list in packing_lists:
            for item in packing_list.items:
                key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
                if key not in invoice_keys:
                    continue
                design_name = (item.design_name or "").strip()
                if not design_name:
                    continue  # nothing to send out for job work without a named design
                design_key = _normalize_name(design_name)

                pd_entry = by_product_and_design.setdefault((key, design_key), {
                    "design_name": design_name, "quantity_boxes": 0.0,
                    "quantity_unit": item.quantity_unit or "PCS",
                })
                pd_entry["quantity_boxes"] += item.quantity_boxes or 0
        for entry in by_product_and_design.values():
            entry["quantity_boxes"] = round(entry["quantity_boxes"], 2)

        # A design name's products on this invoice, so the fallback map can
        # skip any design that isn't unique to one of them.
        products_by_design: dict = {}
        for product_key, design_key in by_product_and_design:
            products_by_design.setdefault(design_key, set()).add(product_key)
        by_design_only = {
            design_key: by_product_and_design[(next(iter(product_keys)), design_key)]
            for design_key, product_keys in products_by_design.items()
            if len(product_keys) == 1
        }
        return by_product_and_design, by_design_only

    # ---- prefill from an existing proforma invoice --------------------------------------------------
    def build_prefill_from_proforma(self, invoice: ProformaInvoice) -> dict:
        """Caller must have already loaded `invoice` via
        ProformaInvoiceService.get(id, current_user.company_id), so cross-
        company ownership is verified before we get here. The invoice's own
        consignee carries over as the FROM SELLER's name and address, as a
        starting point for whoever the goods are actually going out from -
        the supplier dropdown is right there to correct it. The Job
        Manufacturer is deliberately left blank: nothing on the invoice knows
        who that is.

        No lines are prefilled. Which designs go out for job work, and in what
        quantity, is the whole point of the form - so the designs are offered
        (via invoice_products + invoice_quantities) and added deliberately."""
        return {
            "proforma_invoice_id": invoice.id,
            "job_work_date": invoice.invoice_date,
            "seller_name": invoice.consignee_name,
            "seller_address": invoice.consignee_address,
            "payment_terms": invoice.payment_terms,
            "remarks": invoice.remarks,
            "currency_code": invoice.currency_code,
        }

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list,
                     invoice_quantities: dict, invoice_quantities_by_design: dict) -> List[JobWorkItem]:
        """`invoice_quantities`/`invoice_quantities_by_design` are the two
        maps invoice_quantities() builds for whichever proforma invoice this
        job work is linked to ({} for both when there isn't one) - the
        single source of truth for source_quantity, looked up fresh here
        rather than trusted from the posted value, since it is meant to be
        fetched, not typed."""
        items = []
        for i, raw in enumerate(raw_items, start=1):
            design_name = (raw.get("design_name") or "").strip()
            to_product_name_raw = (raw.get("to_product_name") or "").strip()
            if not design_name and not to_product_name_raw:
                continue
            label = f"{to_product_name_raw} / {design_name}".strip(" /")

            # The TARGET: what the job work converts the design into. Same
            # trust boundary as PurchaseOrderService._build_items - only keep
            # a product reference from this same company - and both the NAME
            # and hsn_code are taken from the catalog rather than the form,
            # so a crafted post can't label one product as another.
            to_product_id = int(raw["to_product_id"]) if raw.get("to_product_id") else None
            to_product_name = to_product_name_raw or None
            hsn_code = None
            unit = "PCS"
            if to_product_id:
                to_product = self.product_repo.get_by_id(to_product_id)
                if not to_product or to_product.company_id != company_id:
                    to_product_id, to_product_name = None, None
                else:
                    to_product_name = to_product.product_name
                    hsn_code = to_product.hsn_code
                    unit = to_product.quantity_unit or "PCS"  # counted, not measured by area
            if not to_product_id:
                raise ValidationError(f"Row {i} ('{label}'): To Product is compulsory.")

            # The design: one of to_product's own catalog designs. Kept even
            # when it no longer resolves (a design since deleted), same as
            # every other design reference in this app.
            design_id = int(raw["design_id"]) if raw.get("design_id") else None
            if not design_name:
                raise ValidationError(f"Row {i} ('{label}'): a design is compulsory.")

            # The SOURCE: the invoice's own product, kept only to look up
            # source_quantity below. Same trust boundary as to_product.
            product_id = int(raw["product_id"]) if raw.get("product_id") else None
            product_name = (raw.get("product_name") or "").strip()
            if product_id:
                product = self.product_repo.get_by_id(product_id)
                if not product or product.company_id != company_id:
                    product_id = None

            try:
                conversion_value = float(raw.get("conversion_value") or 0)
                extra_percent = float(raw.get("extra_percent") or 0)
            except ValueError:
                raise ValidationError(f"Row {i} ('{label}'): conversion value and extra qty % must be numbers.")
            if conversion_value <= 0:
                raise ValidationError(f"Row {i} ('{label}'): conversion value is compulsory and must be greater than zero.")
            if extra_percent < 0:
                raise ValidationError(f"Row {i} ('{label}'): extra qty % can't be negative.")

            # Fetched, not typed: this design's quantity off the invoice's
            # own packing list. Tried first under the chosen TO PRODUCT
            # (exact) - not "Product", the source-side field kept only for
            # the Products card below - since Jobed Qty is now read straight
            # off whichever design list the To Product itself offered
            # (JobWorkService.invoice_quantities filtered client-side by the
            # same key in designsForToProduct()). Empty whenever To Product
            # isn't itself one of the invoice's own products, which falls
            # back to whatever this design name adds up to across the WHOLE
            # invoice (one invoice essentially never repeats the same design
            # under two different products).
            source_quantity = 0.0
            design_key = _normalize_name(design_name)
            entry = None
            if to_product_id or to_product_name:
                lookup_key = (_product_key({"product_id": to_product_id, "product_name": to_product_name}), design_key)
                entry = invoice_quantities.get(lookup_key)
            if not entry:
                entry = invoice_quantities_by_design.get(design_key)
            if entry:
                source_quantity = entry["quantity_boxes"]

            converted_quantity = round(source_quantity / conversion_value, 2)
            extra_quantity = round(converted_quantity * extra_percent / 100, 2)
            job_quantity = math.ceil(converted_quantity + extra_quantity)
            if job_quantity <= 0:
                raise ValidationError(
                    f"Row {i} ('{label}'): job quantity works out to zero - check that To Product's design "
                    f"list picks up this design's quantity from the proforma invoice."
                )

            items.append(JobWorkItem(
                id=None, job_work_id=None, sr_no=i,
                product_id=product_id, product_name=product_name or to_product_name,
                to_product_id=to_product_id, to_product_name=to_product_name,
                hsn_code=hsn_code, design_id=design_id, design_name=design_name,
                unit=unit, source_quantity=source_quantity,
                conversion_value=conversion_value, extra_percent=extra_percent,
                converted_quantity=converted_quantity, extra_quantity=extra_quantity,
                job_quantity=job_quantity,
            ))
        if not items:
            raise ValidationError(
                "At least one design line is compulsory - pick a To Product, add a design "
                "and use \"Add to Job Work\"."
            )
        return items

    # ---- Products card (a copy of PurchaseOrderService's own Products lines) --------------------------------------------------
    def _build_products(self, company_id: int, raw_products: list) -> List[JobWorkProduct]:
        """One row per product on the Products card - a plain copy of
        PurchaseOrderService._build_items, minus design tagging (not
        relevant here; the design lines above already carry that). Optional:
        an empty list is fine, this card is a costing reference, not a
        requirement for saving a job work."""
        products = []
        for i, raw in enumerate(raw_products, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            try:
                quantity_value = float(raw.get("quantity_value") or 0)
                price_inr = float(raw.get("price_inr") or 0)
                quantity_boxes = float(raw["quantity_boxes"]) if raw.get("quantity_boxes") else None
            except ValueError:
                raise ValidationError(f"Products row {i}: quantity and price must be numbers.")
            product_id = int(raw["product_id"]) if raw.get("product_id") else None
            quantity_unit = "PCS"

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
                raise ValidationError(f"Products row {i} ('{product_name}'): quantity is compulsory and must be greater than zero.")
            if price_inr < 0:
                raise ValidationError(f"Products row {i} ('{product_name}'): price can't be negative.")

            unit = (raw.get("unit") or "SQM").strip() or "SQM"
            price_per = "BOX" if (raw.get("price_per") or "BOX").strip().upper() == "BOX" else unit
            if price_per == "BOX":
                if not quantity_boxes:
                    raise ValidationError(f"Products row {i} ('{product_name}'): boxes is compulsory when the price is per box.")
                total_inr = round(quantity_boxes * price_inr, 2)
            else:
                total_inr = round(quantity_value * price_inr, 2)

            products.append(JobWorkProduct(
                id=None, job_work_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                quantity_boxes=quantity_boxes, quantity_unit=quantity_unit, quantity_value=quantity_value, unit=unit,
                price_inr=price_inr, price_per=price_per, total_inr=total_inr,
            ))
        return products

    # ---- Products card tax derivation (mirrors PurchaseOrderService) --------------------------------------------------
    def base_igst_percent(self, company_id: int, purchase_type: str, products: List[JobWorkProduct]) -> float:
        if purchase_type == "exemption":
            return EXEMPTION_IGST_PERCENT
        rate = 0.0
        for product in products:
            if not product.product_id:
                continue
            catalog_product = self.product_repo.get_by_id(product.product_id)
            if catalog_product and catalog_product.company_id == company_id and catalog_product.igst_percent:
                rate = max(rate, float(catalog_product.igst_percent))
        return rate

    def _tax_percentages(self, company_id: int, purchase_type: str, manufacturer_gstin: Optional[str],
                         products: List[JobWorkProduct]) -> tuple:
        """(igst, cgst, sgst) for the Products card - same state-code split
        PurchaseOrderService._tax_percentages runs, against the Job
        Manufacturer's own GSTIN rather than a seller's."""
        rate = self.base_igst_percent(company_id, purchase_type, products)
        our_company = self.company_repo.get(company_id) if self.company_repo else None
        if is_intra_state(our_company.gstin if our_company else None, manufacturer_gstin):
            half = round(rate / 2, 4)
            return 0.0, half, half
        return rate, 0.0, 0.0

    def _supplier_id(self, raw, company_id: int) -> Optional[int]:
        """Only trust a supplier from this same company - same reasoning as
        PurchaseOrderService._build_header applies to seller_supplier_id.
        Used for both parties on the sheet, the From Seller and the Job
        Manufacturer."""
        supplier_id = int(raw) if raw else None
        if supplier_id is None or self.supplier_repo is None:
            return supplier_id
        supplier = self.supplier_repo.get_by_id(supplier_id)
        if not supplier or supplier.company_id != company_id:
            return None
        return supplier_id

    def _resolve_invoice(self, current_user: User, fields: dict) -> Optional[ProformaInvoice]:
        """Only trust a proforma invoice from this same company - called once
        per save and the result threaded through both _build_items (for its
        invoice_quantities) and _build_header (for proforma_invoice_id),
        rather than resolving it twice."""
        proforma_invoice_id = int(fields["proforma_invoice_id"]) if fields.get("proforma_invoice_id") else None
        if proforma_invoice_id is None:
            return None
        invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
        if not invoice or invoice.company_id != current_user.company_id:
            return None
        return invoice

    def _build_header(self, current_user: User, fields: dict, items: List[JobWorkItem],
                       products: List[JobWorkProduct], invoice: Optional[ProformaInvoice]) -> JobWork:
        seller_name = (fields.get("seller_name") or "").strip()
        if not seller_name:
            raise ValidationError("From Seller name is compulsory.")
        manufacturer_name = (fields.get("manufacturer_name") or "").strip()
        if not manufacturer_name:
            raise ValidationError("Job Manufacturer name is compulsory.")
        job_work_date = (fields.get("job_work_date") or "").strip() or date.today().isoformat()
        proforma_invoice_id = invoice.id if invoice else None
        manufacturer_gstin = (fields.get("manufacturer_gstin") or "").strip() or None

        purchase_type = (fields.get("purchase_type") or "").strip() or DEFAULT_PURCHASE_TYPE
        if purchase_type not in PURCHASE_TYPES:
            raise ValidationError("'Purchase under' must be either a full tax purchase or an exemption.")
        # Percentages are never taken from the form - the form only displays
        # them, so a posted value would be a stale (or crafted) copy of what
        # is derived here (same reasoning as PurchaseOrderService).
        igst_percent, cgst_percent, sgst_percent = self._tax_percentages(
            current_user.company_id, purchase_type, manufacturer_gstin, products
        )

        # Currency name + symbol are snapshotted off the Miscellaneous list so
        # editing that list later can't rewrite an issued sheet, exactly as
        # PurchaseOrderService does it.
        currency_code, currency_symbol = (
            self.misc_list_service.resolve_currency(current_user.company_id, fields.get("currency_code"))
            if self.misc_list_service else ((fields.get("currency_code") or "").strip() or None, None)
        )

        return JobWork(
            id=None, company_id=current_user.company_id, job_work_number="", job_work_date=job_work_date,
            seller_name=seller_name, created_by=current_user.id,
            proforma_invoice_id=proforma_invoice_id,
            seller_supplier_id=self._supplier_id(
                fields.get("seller_supplier_id"), current_user.company_id
            ),
            seller_address=(fields.get("seller_address") or "").strip() or None,
            seller_pan=(fields.get("seller_pan") or "").strip() or None,
            seller_gstin=(fields.get("seller_gstin") or "").strip() or None,
            manufacturer_supplier_id=self._supplier_id(
                fields.get("manufacturer_supplier_id"), current_user.company_id
            ),
            manufacturer_name=manufacturer_name,
            manufacturer_address=(fields.get("manufacturer_address") or "").strip() or None,
            manufacturer_pan=(fields.get("manufacturer_pan") or "").strip() or None,
            manufacturer_gstin=manufacturer_gstin,
            seller_ref_no=(fields.get("seller_ref_no") or "").strip() or None,
            delivery_time=(fields.get("delivery_time") or "").strip() or None,
            advance_percent=(fields.get("advance_percent") or "").strip() or None,
            payment_terms=(fields.get("payment_terms") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            currency_code=currency_code, currency_symbol=currency_symbol,
            igst_percent=igst_percent, cgst_percent=cgst_percent, sgst_percent=sgst_percent,
            purchase_type=purchase_type,
            tax_as_actual=str(fields.get("tax_as_actual") or "").lower() in ("1", "true", "on", "yes"),
            items=items, products=products,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list, raw_products: Optional[list] = None) -> JobWork:
        invoice = self._resolve_invoice(current_user, fields)
        quantities, quantities_by_design = (
            self.invoice_quantities(current_user.company_id, invoice) if invoice else ({}, {})
        )
        items = self._build_items(current_user.company_id, raw_items, quantities, quantities_by_design)
        products = self._build_products(current_user.company_id, raw_products or [])
        job_work = self._build_header(current_user, fields, items, products, invoice)
        job_work.job_work_number = self._generate_number(current_user.company_id, job_work.job_work_date)
        created = self.job_work_repo.create(job_work)
        self.version_service.record("job_work", created, current_user.id)
        return created

    def update(self, current_user: User, job_work_id: int, fields: dict, raw_items: list,
               raw_products: Optional[list] = None) -> JobWork:
        existing = self.get(job_work_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        invoice = self._resolve_invoice(current_user, fields)
        quantities, quantities_by_design = (
            self.invoice_quantities(current_user.company_id, invoice) if invoice else ({}, {})
        )
        items = self._build_items(current_user.company_id, raw_items, quantities, quantities_by_design)
        products = self._build_products(current_user.company_id, raw_products or [])
        job_work = self._build_header(current_user, fields, items, products, invoice)
        self.job_work_repo.update(job_work_id, job_work)
        updated = self.get(job_work_id, current_user.company_id)
        self.version_service.record("job_work", updated, current_user.id)
        return updated

    def delete(self, current_user: User, job_work_id: int) -> None:
        existing = self.get(job_work_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.job_work_repo.delete(job_work_id)


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
                 misc_list_service: Optional["MiscListService"] = None,
                 job_work_repo: Optional[JobWorkRepository] = None):
        self.misc_list_service = misc_list_service
        self.purchase_invoice_repo = purchase_invoice_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.purchase_order_repo = purchase_order_repo
        self.version_service = version_service
        self.party_repos = party_repos  # {'Buyer': ..., 'Supplier': ...} for advance_client_status
        self.supplier_repo = supplier_repo  # for validating seller_supplier_id belongs to this company
        self.upload_folder = upload_folder
        self.allowed_extensions = allowed_extensions
        # A job work now prints/numbers as a purchase order, so it can be a
        # "Start from" candidate here too - optional so a caller that never
        # wires it just sees no job works in the picker.
        self.job_work_repo = job_work_repo

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

    def list_for_job_work(self, job_work_id: Optional[int], company_id: int) -> List[PurchaseInvoice]:
        if not job_work_id:
            return []
        return [pinv for pinv in self.purchase_invoice_repo.list_for_job_work(job_work_id)
                if pinv.company_id == company_id]

    def list_for_proforma(self, purchase_orders: List[PurchaseOrder], company_id: int,
                           job_works: Optional[list] = None) -> List[PurchaseInvoice]:
        """Every purchase invoice raised against any purchase order OR job
        work placed under a proforma invoice - there's no direct
        proforma_invoice_id on a purchase invoice, the link runs through its
        purchase order/job work, so the caller passes in both already loaded
        via PurchaseOrderService.list_for_proforma and
        JobWorkService.list_for_proforma. A job work now prints/numbers as a
        purchase order and can be invoiced the same way, so a purchase
        invoice raised against one has to be picked up here too, not just
        ones raised against a real purchase order."""
        result = []
        seen_ids = set()
        for po in purchase_orders:
            for pinv in self.list_for_purchase_order(po.id, company_id):
                if pinv.id not in seen_ids:
                    seen_ids.add(pinv.id)
                    result.append(pinv)
        for jw in job_works or []:
            for pinv in self.list_for_job_work(jw.id, company_id):
                if pinv.id not in seen_ids:
                    seen_ids.add(pinv.id)
                    result.append(pinv)
        return result

    def count_map_by_purchase_order(self, company_id: int) -> dict:
        return self.purchase_invoice_repo.count_map_by_purchase_order(company_id)

    def list_all_outstanding(self, company_id: int, exclude_purchase_invoice_id: Optional[int] = None) -> List[PurchaseOrder]:
        """Every purchase order in this company, across every supplier, that
        still has at least one product line not fully covered by purchase
        invoices raised against it yet - the unfiltered candidate pool the
        "Start from" picker narrows to one supplier client-side once a
        seller is chosen. `exclude_purchase_invoice_id` (set when editing an
        existing invoice) ignores that invoice's own items when deciding
        what's still outstanding, so a PO it already fully covers doesn't
        vanish from its own edit form's picker."""
        purchase_orders = self.purchase_order_repo.list_all(company_id)
        return [po for po in purchase_orders if self._has_outstanding(po, exclude_purchase_invoice_id)]

    def list_outstanding_for_supplier(self, seller_supplier_id: Optional[int], company_id: int,
                                       exclude_purchase_invoice_id: Optional[int] = None) -> List[PurchaseOrder]:
        """Every purchase order placed with this supplier that still has at
        least one product line not fully covered by purchase invoices raised
        against it yet - the "Start from" picker's candidate list, so a PO
        drops out the moment its last outstanding line is invoiced. Same
        outstanding-remainder idea as ProformaFulfilmentService.product_status,
        one document further down the chain (PO -> its own purchase
        invoices, rather than proforma -> its purchase orders)."""
        if not seller_supplier_id:
            return []
        purchase_orders = self.purchase_order_repo.list_for_seller(seller_supplier_id)
        return [po for po in purchase_orders if po.company_id == company_id
                and self._has_outstanding(po, exclude_purchase_invoice_id)]

    def _invoiced_totals(self, purchase_order_id: int, exclude_purchase_invoice_id: Optional[int] = None) -> dict:
        return {
            _product_key({"product_id": row["product_id"], "product_name": row["product_name"]}): row
            for row in self.purchase_invoice_repo.invoiced_totals_for_purchase_order(
                purchase_order_id, exclude_purchase_invoice_id
            )
        }

    def _has_outstanding(self, purchase_order: PurchaseOrder, exclude_purchase_invoice_id: Optional[int] = None) -> bool:
        # list_all/list_for_seller (unlike get_by_id) don't load line items -
        # a saved PO always has at least one (enforced at create time), so an
        # empty list here means "not loaded", not "genuinely empty".
        items = purchase_order.items or self.purchase_order_repo.get_by_id(purchase_order.id).items
        invoiced = self._invoiced_totals(purchase_order.id, exclude_purchase_invoice_id)
        for item in items:
            key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
            row = invoiced.get(key)
            invoiced_boxes = row["boxes"] if row else 0
            invoiced_qty = row["quantity"] if row else 0
            ordered_boxes = item.quantity_boxes or 0
            ordered_qty = item.quantity_value or 0
            outstanding = (ordered_boxes - invoiced_boxes) if ordered_boxes > 0 else (ordered_qty - invoiced_qty)
            if outstanding > _DESIGN_QTY_TOLERANCE:
                return True
        return False

    # ---- job works as "Start from" candidates (a job work now prints/
    # numbers as a purchase order, so it is invoiced the same way) --------
    def list_all_outstanding_job_works(self, company_id: int,
                                        exclude_purchase_invoice_id: Optional[int] = None) -> List[JobWork]:
        """Every job work in this company that still has at least one
        Products-card line not fully covered by purchase invoices raised
        against it yet - the job-work counterpart of list_all_outstanding."""
        if not self.job_work_repo:
            return []
        job_works = self.job_work_repo.list_all(company_id)
        return [jw for jw in job_works if self._has_outstanding_job_work(jw, exclude_purchase_invoice_id)]

    def list_outstanding_job_works_for_seller(self, seller_supplier_id: Optional[int], company_id: int,
                                               exclude_purchase_invoice_id: Optional[int] = None) -> List[JobWork]:
        """Job-work counterpart of list_outstanding_for_supplier - a job
        work's natural link to a supplier for invoicing is seller_supplier_id,
        the FROM SELLER (see JobWorkRepository.list_for_seller)."""
        if not seller_supplier_id or not self.job_work_repo:
            return []
        job_works = self.job_work_repo.list_for_seller(seller_supplier_id)
        return [jw for jw in job_works if jw.company_id == company_id
                and self._has_outstanding_job_work(jw, exclude_purchase_invoice_id)]

    def _invoiced_totals_for_job_work(self, job_work_id: int, exclude_purchase_invoice_id: Optional[int] = None) -> dict:
        return {
            _product_key({"product_id": row["product_id"], "product_name": row["product_name"]}): row
            for row in self.purchase_invoice_repo.invoiced_totals_for_job_work(
                job_work_id, exclude_purchase_invoice_id
            )
        }

    def _has_outstanding_job_work(self, job_work: JobWork, exclude_purchase_invoice_id: Optional[int] = None) -> bool:
        # list_all/list_for_manufacturer don't load the Products card - unlike
        # a purchase order, a job work's Products card is optional (a costing
        # reference), so an empty list here can genuinely mean "nothing to
        # invoice" rather than "not loaded"; get_by_id settles which.
        products = job_work.products or self.job_work_repo.get_by_id(job_work.id).products
        invoiced = self._invoiced_totals_for_job_work(job_work.id, exclude_purchase_invoice_id)
        for product in products:
            key = _product_key({"product_id": product.product_id, "product_name": product.product_name})
            row = invoiced.get(key)
            invoiced_boxes = row["boxes"] if row else 0
            invoiced_qty = row["quantity"] if row else 0
            ordered_boxes = product.quantity_boxes or 0
            ordered_qty = product.quantity_value or 0
            outstanding = (ordered_boxes - invoiced_boxes) if ordered_boxes > 0 else (ordered_qty - invoiced_qty)
            if outstanding > _DESIGN_QTY_TOLERANCE:
                return True
        return False

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
            "purchase_type": purchase_order.purchase_type,
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

    # ---- prefill from several purchase orders at once --------------------------------------------------
    def build_prefill_from_purchase_orders(self, purchase_orders: List[PurchaseOrder]) -> dict:
        """Same idea as build_prefill_from_purchase_order, extended to
        several purchase orders of the same supplier at once - a shipment
        can cover more than one of our orders. Each PO's product lines are
        cut down to what's still outstanding (see _has_outstanding /
        _remaining_items) - a line already fully invoiced on an earlier
        purchase invoice against that PO is dropped, a partly-invoiced one
        comes through at its remaining boxes/quantity only, same
        outstanding-remainder treatment PurchaseOrderService.build_prefill_from_proforma
        already gives POs built off a proforma invoice. Every surviving row
        is tagged with the PO it came from (source_po_id/source_po_number)
        so the form can group them by origin. Header fields (seller/
        currency/port/etc.) are taken from the first PO - callers only ever
        offer POs already filtered to one supplier, so every candidate
        agrees on these; tax amounts are summed across all of them."""
        if not purchase_orders:
            return {"fields": {}, "items": []}
        primary = purchase_orders[0]
        fields = {
            "purchase_order_ids": [po.id for po in purchase_orders],
            "seller_supplier_id": primary.seller_supplier_id,
            "seller_name": primary.seller_name,
            "seller_address": primary.seller_address,
            "seller_pan": primary.seller_pan,
            "seller_gstin": primary.seller_gstin,
            "seller_ref_no": primary.seller_ref_no,
            "port_of_loading": primary.port_of_loading,
            "port_of_discharge": primary.port_of_discharge,
            "container_details": primary.container_details,
            "igst_amount": round(sum(po.igst_amount for po in purchase_orders), 2),
            "cgst_amount": round(sum(po.cgst_amount for po in purchase_orders), 2),
            "sgst_amount": round(sum(po.sgst_amount for po in purchase_orders), 2),
            "purchase_type": primary.purchase_type,
            "currency_code": primary.currency_code,
        }
        items = []
        for po in purchase_orders:
            items.extend(self._remaining_items(po))
        return {"fields": fields, "items": items}

    def _remaining_items(self, purchase_order: PurchaseOrder) -> list:
        """One purchase order's product lines cut down to their outstanding
        remainder and tagged with the PO they came from - the multi-PO
        counterpart of _raw_item, scaling by boxes when the order is priced
        per box and by quantity otherwise (mirrors PurchaseOrderService's
        own _scaled_item ratio approach)."""
        invoiced = self._invoiced_totals(purchase_order.id)
        result = []
        for item in purchase_order.items:
            key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
            row = invoiced.get(key)
            invoiced_boxes = row["boxes"] if row else 0
            invoiced_qty = row["quantity"] if row else 0
            ordered_boxes = item.quantity_boxes or 0
            ordered_qty = item.quantity_value or 0
            if ordered_boxes > 0:
                ratio = max(ordered_boxes - invoiced_boxes, 0) / ordered_boxes
            elif ordered_qty > 0:
                ratio = max(ordered_qty - invoiced_qty, 0) / ordered_qty
            else:
                ratio = 0
            if ratio <= 0:
                continue  # already fully invoiced on an earlier purchase invoice against this PO
            raw = self._raw_item(item)
            if ratio < 1:
                for k in ("quantity_boxes", "quantity_value"):
                    if isinstance(raw.get(k), (int, float)):
                        raw[k] = round(raw[k] * ratio, 2)
            raw["purchase_order_id"] = purchase_order.id
            raw["source_po_number"] = purchase_order.po_number
            result.append(raw)
        return result

    # ---- prefill from one or several job works at once (a job work now
    # prints/numbers as a purchase order, so it's prefilled the same way) ---
    @staticmethod
    def _raw_product(product: "JobWorkProduct") -> dict:
        return {
            "product_id": product.product_id, "product_name": product.product_name,
            "hsn_code": product.hsn_code, "quantity_boxes": product.quantity_boxes,
            "quantity_value": product.quantity_value, "unit": product.unit,
            "price_inr": product.price_inr, "price_per": product.price_per,
        }

    def build_prefill_from_job_works(self, job_works: List[JobWork]) -> dict:
        """Same idea as build_prefill_from_purchase_orders, over job works'
        Products card lines instead of a purchase order's items. Header
        fields are taken from the first job work - callers only ever offer
        job works already filtered to one manufacturer, so every candidate
        agrees on these; tax amounts are summed across all of them."""
        if not job_works:
            return {"fields": {}, "items": []}
        primary = job_works[0]
        fields = {
            "job_work_ids": [jw.id for jw in job_works],
            "seller_supplier_id": primary.seller_supplier_id,
            "seller_name": primary.seller_name,
            "seller_address": primary.seller_address,
            "seller_pan": primary.seller_pan,
            "seller_gstin": primary.seller_gstin,
            "igst_amount": round(sum(jw.igst_amount for jw in job_works), 2),
            "cgst_amount": round(sum(jw.cgst_amount for jw in job_works), 2),
            "sgst_amount": round(sum(jw.sgst_amount for jw in job_works), 2),
            "purchase_type": primary.purchase_type,
            "currency_code": primary.currency_code,
        }
        items = []
        for jw in job_works:
            items.extend(self._remaining_products(jw))
        return {"fields": fields, "items": items}

    def _remaining_products(self, job_work: JobWork) -> list:
        """One job work's Products-card lines cut down to their outstanding
        remainder and tagged with the job work they came from - the
        job-work counterpart of _remaining_items."""
        invoiced = self._invoiced_totals_for_job_work(job_work.id)
        result = []
        for product in job_work.products:
            key = _product_key({"product_id": product.product_id, "product_name": product.product_name})
            row = invoiced.get(key)
            invoiced_boxes = row["boxes"] if row else 0
            invoiced_qty = row["quantity"] if row else 0
            ordered_boxes = product.quantity_boxes or 0
            ordered_qty = product.quantity_value or 0
            if ordered_boxes > 0:
                ratio = max(ordered_boxes - invoiced_boxes, 0) / ordered_boxes
            elif ordered_qty > 0:
                ratio = max(ordered_qty - invoiced_qty, 0) / ordered_qty
            else:
                ratio = 0
            if ratio <= 0:
                continue  # already fully invoiced on an earlier purchase invoice against this job work
            raw = self._raw_product(product)
            if ratio < 1:
                for k in ("quantity_boxes", "quantity_value"):
                    if isinstance(raw.get(k), (int, float)):
                        raw[k] = round(raw[k] * ratio, 2)
            raw["job_work_id"] = job_work.id
            raw["source_jw_number"] = job_work.job_work_number
            result.append(raw)
        return result

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

            # Which purchase order (of possibly several selected under
            # "Start from") this row was prefilled from - not validated
            # against the company here since it only ever drives display
            # grouping and the outstanding-quantity check, never access
            # control; a stray/foreign id just fails to match anything.
            purchase_order_id = int(raw["purchase_order_id"]) if raw.get("purchase_order_id") else None
            # Same idea for a row prefilled from a job work's Products card.
            job_work_id = int(raw["job_work_id"]) if raw.get("job_work_id") else None

            items.append(PurchaseInvoiceItem(
                id=None, purchase_invoice_id=None, sr_no=i, product_id=product_id, product_name=product_name,
                hsn_code=(raw.get("hsn_code") or "").strip() or None,
                quantity_boxes=quantity_boxes, quantity_value=quantity_value, unit=unit,
                price_inr=price_inr, price_per=price_per, total_inr=total_inr,
                purchase_order_id=purchase_order_id, job_work_id=job_work_id,
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
        purchase_type = (fields.get("purchase_type") or "").strip() or DEFAULT_PURCHASE_TYPE
        if purchase_type not in PURCHASE_TYPES:
            raise ValidationError("Invalid purchase type.")

        # A purchase invoice can be raised against several purchase orders of
        # the same supplier at once. `purchase_order_ids` (a list, from the
        # "Start from" multi-select) is the primary path; a lone legacy
        # `purchase_order_id` is still accepted as a one-item list for
        # backward compatibility with older callers/tests. Only ids that
        # actually belong to this company are kept - a crafted id could
        # otherwise attach this invoice to another company's PO.
        raw_po_ids = fields.get("purchase_order_ids") or (
            [fields["purchase_order_id"]] if fields.get("purchase_order_id") else []
        )
        purchase_order_ids = []
        for raw in raw_po_ids:
            try:
                po_id = int(raw)
            except (TypeError, ValueError):
                continue
            purchase_order = self.purchase_order_repo.get_by_id(po_id)
            if purchase_order and purchase_order.company_id == current_user.company_id and po_id not in purchase_order_ids:
                purchase_order_ids.append(po_id)
        purchase_order_id = purchase_order_ids[0] if purchase_order_ids else None

        # Same idea for the job works (of possibly several) this invoice was
        # raised against instead - see purchase_order_ids just above.
        raw_jw_ids = fields.get("job_work_ids") or (
            [fields["job_work_id"]] if fields.get("job_work_id") else []
        )
        job_work_ids = []
        if self.job_work_repo:
            for raw in raw_jw_ids:
                try:
                    jw_id = int(raw)
                except (TypeError, ValueError):
                    continue
                job_work = self.job_work_repo.get_by_id(jw_id)
                if job_work and job_work.company_id == current_user.company_id and jw_id not in job_work_ids:
                    job_work_ids.append(jw_id)
        job_work_id = job_work_ids[0] if job_work_ids else None

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
            purchase_order_id=purchase_order_id, job_work_id=job_work_id,
            lead_id=lead_id, seller_supplier_id=seller_supplier_id,
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
            purchase_type=purchase_type,
            remarks=(fields.get("remarks") or "").strip() or None,
            currency_code=currency_code, currency_symbol=currency_symbol,
            items=items, purchase_order_ids=purchase_order_ids, job_work_ids=job_work_ids,
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
# JOB OUT SERVICE
# ============================================================
class JobOutService:
    """The JOB OUT sheet - "DELIVERY CHALLAN FOR JOBWORK", the paper that
    travels with goods going out to a job manufacturer, raised off ONE
    purchase invoice.

    The unusual thing about this service is how little it stores. Every
    other document here snapshots its lines on save so a later edit of the
    source can't rewrite an issued sheet; a job out does the opposite and
    reads its whole body live off its purchase invoice every time it renders
    (see build_sheet). That is deliberate - a challan is a dispatch note
    AGAINST an invoice that already exists, so the two must agree; a
    corrected invoice should correct its challan, not leave it stale. What
    IS stored is only what's typed at dispatch time: the challan number/date,
    the transport block and the e-way bill.

    The goods table's two Description columns come from either side of the
    JOB WORK this invoice was raised against:

      "Dispatch for Jobwork"          the purchase invoice line's OWN product
                                      - the master goods physically going out
                                      (JobWorkItem.product_id, the source
                                      side);
      "Require OutPut After Jobwork"  the jobbed product those goods are
                                      being converted INTO
                                      (JobWorkItem.to_product_id), listed
                                      with each of its designs and the job
                                      quantity expected back.

    Note the direction: it is the invoice that carries the master product and
    the job work that names the jobbed one - NOT Product.master_product_id,
    which points the other way and is not what this document follows."""

    def __init__(self, job_out_repo: JobOutRepository, purchase_invoice_repo: PurchaseInvoiceRepository,
                 product_repo: ProductRepository, company_repo: CompanyRepository,
                 packing_list_repo: PackingListRepository,
                 purchase_order_repo: Optional[PurchaseOrderRepository] = None,
                 version_service: Optional["DocumentVersionService"] = None,
                 job_work_repo: Optional[JobWorkRepository] = None,
                 transporter_repo: Optional[TransporterRepositoryBase] = None):
        self.job_out_repo = job_out_repo
        self.purchase_invoice_repo = purchase_invoice_repo
        self.product_repo = product_repo
        # The job work behind this invoice: the ONLY place that knows which
        # jobbed product each dispatched product becomes, and with which
        # designs - see _jobbed_map.
        self.job_work_repo = job_work_repo
        # Our own letterhead, and the Dispatch From block when the form's
        # "dispatched from our own warehouse" box is ticked.
        self.company_repo = company_repo
        # The invoice's own packing list, for the per-design breakdown under
        # each goods line.
        self.packing_list_repo = packing_list_repo
        # For the sheet's PURCHASE ORDER NO/DATE cells - a purchase invoice
        # doesn't carry those itself, they come from whichever purchase
        # order(s) it was raised against.
        self.purchase_order_repo = purchase_order_repo
        # Resolves a TRANSPORT NAME off the typed Transport GSTIN when the
        # name itself wasn't filled in - see _transporter_name.
        self.transporter_repo = transporter_repo
        self.version_service = version_service

    # ---- reads --------------------------------------------------
    def get(self, job_out_id: int, company_id: int) -> JobOut:
        job_out = self.job_out_repo.get_by_id(job_out_id)
        if not job_out or job_out.company_id != company_id:
            # 404, not 403 - don't reveal that another company's job out exists.
            raise NotFoundError(f"Job out #{job_out_id} not found.")
        return job_out

    def list_all(self, company_id: int) -> List[JobOut]:
        return self.job_out_repo.list_all(company_id)

    def list_for_purchase_invoice(self, purchase_invoice_id: Optional[int], company_id: int) -> List[JobOut]:
        """Every job out dispatched against this purchase invoice, newest
        first. company_id is re-checked here because the caller passes an id
        taken straight off an invoice."""
        if not purchase_invoice_id:
            return []
        return [jo for jo in self.job_out_repo.list_for_purchase_invoice(purchase_invoice_id)
                if jo.company_id == company_id]

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, job_out: JobOut, current_user: User):
        if current_user.is_admin:
            return
        if job_out.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage job outs you created yourself.")

    # ---- the printed sheet, assembled live off the purchase invoice --------
    def _resolve_purchase_invoice(self, purchase_invoice_id, company_id: int) -> PurchaseInvoice:
        """Only ever accept a purchase invoice from this same company - this
        is the one id a job out is posted with, and every figure on the sheet
        is read through it."""
        try:
            resolved_id = int(purchase_invoice_id) if purchase_invoice_id else None
        except (TypeError, ValueError):
            resolved_id = None
        if resolved_id is None:
            raise ValidationError("A job out must be raised against a purchase invoice.")
        purchase_invoice = self.purchase_invoice_repo.get_by_id(resolved_id)
        if not purchase_invoice or purchase_invoice.company_id != company_id:
            raise ValidationError("That purchase invoice could not be found.")
        return purchase_invoice

    def _jobbed_map(self, purchase_invoice: PurchaseInvoice, company_id: int) -> dict:
        """product_key of the DISPATCHED product -> {output_name, hsn_code,
        designs: [{design_name, quantity, unit}]}, read off the job work(s)
        this purchase invoice was raised against.

        A job work line is exactly this document's two columns already: its
        `product_id` is the master going out (which is what the purchase
        invoice itself is billed for), and its `to_product_id` is the jobbed
        product expected back, tagged with one design. Several job work lines
        normally share one source product - one per design - so they collapse
        into a single goods row carrying a design list.

        The per-design figure is `source_quantity`, NOT `job_quantity`. Read
        JobWorkService._build_items: source_quantity is fetched off the
        proforma invoice's packing list keyed on the TO PRODUCT (the jobbed
        one) plus design - it is literally the jobbed product's boxes per
        design, which is what this column reports. job_quantity is the
        derived ceil(converted + extra) dispatch figure and belongs to the
        job work's own arithmetic, not here. The job work form labels them
        "Jobed Qty" and "Job Quantity" respectively.

        Empty when the invoice has no linked job work (a challan raised off
        a plain purchase-order invoice), which prints the output column
        blank."""
        result: dict = {}
        if not self.job_work_repo:
            return result
        for jw_id in purchase_invoice.job_work_ids or []:
            job_work = self.job_work_repo.get_by_id(jw_id)
            if not job_work or job_work.company_id != company_id:
                continue
            for item in job_work.items:
                key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
                entry = result.setdefault(key, {
                    "output_name": item.to_product_name, "hsn_code": item.hsn_code,
                    "output_designs": [],
                })
                # First line to name an output product wins; a later blank
                # one shouldn't blank the column back out.
                if not entry["output_name"] and item.to_product_name:
                    entry["output_name"] = item.to_product_name
                if not entry["hsn_code"] and item.hsn_code:
                    entry["hsn_code"] = item.hsn_code
                design_name = (item.design_name or "").strip()
                if not design_name:
                    continue
                existing = next(
                    (d for d in entry["output_designs"]
                     if _normalize_name(d["design_name"]) == _normalize_name(design_name)), None
                )
                if existing:
                    existing["quantity"] += item.source_quantity or 0
                else:
                    entry["output_designs"].append({
                        "design_name": design_name,
                        "quantity": item.source_quantity or 0,
                        "unit": item.unit or "BOX",
                    })
        return result

    def _dispatch_designs(self, purchase_invoice: PurchaseInvoice, company_id: int) -> dict:
        """product_key -> [{design_name, quantity, unit}, ...] off this
        purchase invoice's OWN packing list(s) - the designs actually going
        out, printed as small print under the DISPATCH column so the challan
        names them rather than only a product total.

        Same {design_name, quantity, unit} shape _jobbed_map emits for the
        output column, so the sheet renders both lists through one macro.
        Empty for a purchase invoice with no packing list yet, which prints
        the dispatched product name on its own."""
        details: dict = {}
        packing_lists = [pl for pl in self.packing_list_repo.list_for_purchase_invoice(purchase_invoice.id)
                         if pl.company_id == company_id]
        for packing_list in packing_lists:
            for item in packing_list.items:
                key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
                design_name = (item.design_name or "").strip()
                if not design_name:
                    continue
                rows = details.setdefault(key, [])
                existing = next((r for r in rows if _normalize_name(r["design_name"]) == _normalize_name(design_name)), None)
                if existing:
                    existing["quantity"] += item.quantity_boxes or 0
                else:
                    rows.append({
                        "design_name": design_name,
                        "quantity": item.quantity_boxes or 0,
                        "unit": item.quantity_unit or "BOX",
                    })
        return details

    def _receiver_party(self, purchase_invoice: PurchaseInvoice, company_id: int) -> Optional[dict]:
        """The Job Manufacturer (Receiver) block: the MANUFACTURER named on
        the job work this invoice was raised against - the party the goods
        physically go to for the work.

        Deliberately not the purchase invoice's seller, which is a different
        party: a job work carries both a FROM SELLER (whose goods go out, and
        who bills us - so they become the invoice's seller) and a JOB
        MANUFACTURER (who does the work). The challan is addressed to the
        manufacturer, while the seller is who it dispatches FROM.

        None when no linked job work names one, which build_sheet falls back
        from - a challan raised off a plain purchase-order invoice has no job
        manufacturer to name, so the seller is the best available party."""
        if not self.job_work_repo:
            return None
        for jw_id in purchase_invoice.job_work_ids or []:
            job_work = self.job_work_repo.get_by_id(jw_id)
            if not job_work or job_work.company_id != company_id:
                continue
            if (job_work.manufacturer_name or "").strip():
                return {
                    "name": job_work.manufacturer_name,
                    "address": job_work.manufacturer_address,
                    "gstin": job_work.manufacturer_gstin,
                }
        return None

    def _transporter_name(self, job_out: JobOut, purchase_invoice: PurchaseInvoice,
                          company_id: int) -> str:
        """The TRANSPORT NAME printed on the challan, in order of preference:

          1. what was typed on this job out;
          2. the name of whichever transporter in the directory carries the
             typed Transport GSTIN - the form offers that GSTIN as a datalist
             off the same directory, so a challan filled in that way already
             identifies the transporter without the name being retyped;
          3. the purchase invoice's own transporter_name.

        Steps 2 and 3 are why a job out saved before this field existed still
        prints a name rather than a blank row."""
        typed = (job_out.transporter_name or "").strip()
        if typed:
            return typed
        gstin = (job_out.transport_gstin or "").strip()
        if gstin and self.transporter_repo:
            for transporter in self.transporter_repo.list_all(company_id):
                if (transporter.gstin_transporter_no or "").strip().upper() == gstin.upper():
                    return transporter.name
        return (purchase_invoice.transporter_name or "").strip()

    def _order_references(self, purchase_invoice: PurchaseInvoice) -> list:
        """[{number, date}] for the sheet's PURCHASE ORDER NO/DATE cells.

        Reads BOTH origins a purchase invoice can have, because a job work
        already numbers itself as a purchase order (JobWorkService.
        _generate_number mints PO{YYYYMMDD}{seq} off the shared daily
        counter) and prints as one. An invoice raised against a job work has
        an empty purchase_order_ids, so looking only at real purchase orders
        - as this did at first - left the cells blank on exactly the invoices
        a job out is normally raised from."""
        refs = []
        if self.purchase_order_repo:
            for po_id in purchase_invoice.purchase_order_ids or []:
                purchase_order = self.purchase_order_repo.get_by_id(po_id)
                if purchase_order and purchase_order.company_id == purchase_invoice.company_id:
                    refs.append({"number": purchase_order.po_number, "date": purchase_order.po_date})
        if self.job_work_repo:
            for jw_id in purchase_invoice.job_work_ids or []:
                job_work = self.job_work_repo.get_by_id(jw_id)
                if job_work and job_work.company_id == purchase_invoice.company_id:
                    refs.append({"number": job_work.job_work_number, "date": job_work.job_work_date})
        return refs

    def build_sheet(self, job_out: JobOut, company_id: int) -> dict:
        """Everything the print template needs, resolved fresh off the
        purchase invoice this job out points at. Returns:

          company           our own letterhead (always ours, top of sheet)
          purchase_invoice  the source invoice
          receiver          {name, address, gstin} - the Job Manufacturer
                            block: the linked job work's MANUFACTURER, the
                            party the goods are sent to for the work (see
                            _receiver_party)
          dispatch_from     {name, address, gstin} - the purchase invoice's
                            own SELLER, who the goods leave from, or our own
                            company when the job out's dispatch_from_company
                            box was ticked (they left our warehouse instead)
          goods             one row per purchase invoice line. Each of the
                            two Description columns carries its own product
                            name AND its own design list, read from a
                            different document:
                              dispatch_name / dispatch_designs - the
                                invoice's own product line, broken down by
                                the invoice's OWN packing list
                              output_name / output_designs - the jobbed
                                product off the linked job work, broken down
                                by that job work's design lines
                            plus hsn_code, boxes, quantity_unit, rate and
                            taxable_value, all from the invoice line
          totals            {taxable, igst, cgst, sgst, invoice_value}
          order_refs        [{number, date}] for the PURCHASE ORDER NO/DATE
                            cells - a linked job work counts as one, since
                            it numbers and prints as a purchase order
          transporter_name  the TRANSPORT NAME cell, typed or resolved
        """
        purchase_invoice = self._resolve_purchase_invoice(job_out.purchase_invoice_id, company_id)
        company = self.company_repo.get(company_id) if self.company_repo else None
        jobbed = self._jobbed_map(purchase_invoice, company_id)
        dispatch_designs = self._dispatch_designs(purchase_invoice, company_id)

        # The invoice's seller: who the goods leave FROM, and who billed us.
        seller = {
            "name": purchase_invoice.seller_name,
            "address": purchase_invoice.seller_address,
            "gstin": purchase_invoice.seller_gstin,
        }
        # Who the goods go TO - the job work's manufacturer. Falls back to
        # the seller only when no job work names one.
        receiver = self._receiver_party(purchase_invoice, company_id) or seller
        if job_out.dispatch_from_company and company:
            dispatch_from = {
                "name": company.company_name, "address": company.address, "gstin": company.gstin,
            }
        else:
            dispatch_from = seller

        goods = []
        for item in purchase_invoice.items:
            key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
            entry = jobbed.get(key, {})
            goods.append({
                "sr_no": item.sr_no,
                # What physically goes OUT: the invoice's own product line,
                # broken down by the invoice's own packing list.
                "dispatch_name": item.product_name,
                "dispatch_designs": dispatch_designs.get(key, []),
                # What is expected BACK: the jobbed product the linked job
                # work converts it into, broken down by that job work's own
                # design lines. Both blank when nothing links them - the
                # output product exists ONLY on the job work, never on the
                # purchase invoice.
                "output_name": entry.get("output_name") or "",
                "output_designs": entry.get("output_designs", []),
                "hsn_code": item.hsn_code or entry.get("hsn_code"),
                "boxes": item.quantity_boxes,
                "quantity_unit": item.price_per or "BOX",
                "quantity_value": item.quantity_value,
                "unit": item.unit,
                "rate": item.price_inr,
                "taxable_value": item.total_inr,
            })

        taxable = round(sum(item.total_inr or 0 for item in purchase_invoice.items), 2)
        totals = {
            "taxable": taxable,
            "igst": purchase_invoice.igst_amount or 0,
            "cgst": purchase_invoice.cgst_amount or 0,
            "sgst": purchase_invoice.sgst_amount or 0,
        }
        totals["invoice_value"] = round(
            taxable + totals["igst"] + totals["cgst"] + totals["sgst"], 2
        )
        return {
            "company": company,
            "purchase_invoice": purchase_invoice,
            "receiver": receiver,
            "dispatch_from": dispatch_from,
            "goods": goods,
            "totals": totals,
            "order_refs": self._order_references(purchase_invoice),
            "transporter_name": self._transporter_name(job_out, purchase_invoice, company_id),
        }

    # ---- prefill from the purchase invoice the button was pressed on -------
    def build_prefill_from_purchase_invoice(self, purchase_invoice: PurchaseInvoice) -> dict:
        """Caller must have already loaded `purchase_invoice` via
        PurchaseInvoiceService.get(id, current_user.company_id), so cross-
        company ownership is verified before we get here. Only the transport
        block carries over (the supplier's own transporter, and its single
        vehicle if it has exactly one) - the challan number, its date and the
        e-way bill are the whole point of the form and are typed."""
        return {
            "purchase_invoice_id": purchase_invoice.id,
            "delivery_challan_date": date.today().isoformat(),
            "transporter_name": purchase_invoice.transporter_name or "",
            "vehicle_no": (purchase_invoice.vehicle_numbers[0]
                           if len(purchase_invoice.vehicle_numbers or []) == 1 else ""),
            "remarks": purchase_invoice.remarks,
        }

    # ---- validation --------------------------------------------------
    def _build_header(self, current_user: User, fields: dict, job_out_id: Optional[int] = None) -> JobOut:
        delivery_challan_no = (fields.get("delivery_challan_no") or "").strip()
        if not delivery_challan_no:
            raise ValidationError("Delivery challan no is compulsory.")
        purchase_invoice = self._resolve_purchase_invoice(
            fields.get("purchase_invoice_id"), current_user.company_id
        )
        # UNIQUE (company_id, delivery_challan_no) would otherwise surface as
        # a raw IntegrityError; catch it here where we can name the clash.
        clash = self.job_out_repo.find_by_challan_no(current_user.company_id, delivery_challan_no)
        if clash and clash.id != job_out_id:
            raise ValidationError(
                f"Delivery challan no {delivery_challan_no} is already used by another job out."
            )
        return JobOut(
            id=None, company_id=current_user.company_id, purchase_invoice_id=purchase_invoice.id,
            delivery_challan_no=delivery_challan_no,
            delivery_challan_date=(fields.get("delivery_challan_date") or "").strip() or date.today().isoformat(),
            created_by=current_user.id,
            dispatch_from_company=str(fields.get("dispatch_from_company") or "").lower() in ("1", "true", "on", "yes"),
            transporter_name=(fields.get("transporter_name") or "").strip() or None,
            transport_gstin=(fields.get("transport_gstin") or "").strip() or None,
            lr_no=(fields.get("lr_no") or "").strip() or None,
            vehicle_no=(fields.get("vehicle_no") or "").strip() or None,
            eway_bill_no=(fields.get("eway_bill_no") or "").strip() or None,
            eway_bill_date=(fields.get("eway_bill_date") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict) -> JobOut:
        job_out = self._build_header(current_user, fields)
        created = self.job_out_repo.create(job_out)
        if self.version_service:
            self.version_service.record("job_out", created, current_user.id)
        return created

    def update(self, current_user: User, job_out_id: int, fields: dict) -> JobOut:
        existing = self.get(job_out_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        job_out = self._build_header(current_user, fields, job_out_id=job_out_id)
        self.job_out_repo.update(job_out_id, job_out)
        updated = self.get(job_out_id, current_user.company_id)
        if self.version_service:
            self.version_service.record("job_out", updated, current_user.id)
        return updated

    def delete(self, current_user: User, job_out_id: int) -> None:
        existing = self.get(job_out_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.job_out_repo.delete(job_out_id)


# ============================================================
# JOB IN SERVICE
# ============================================================
class JobInService:
    """The JOB IN sheet - "JOBWORK INWARD CHALLAN / RETURN", raised against
    ONE job out when jobbed goods come back from the manufacturer.

    The mirror of JobOutService, and deliberately different in one respect:
    a job in STORES its line items. What actually came back is typed at the
    door and this document is the only record of it, so there is nothing to
    derive it from - whereas a job out derives its entire sheet off its
    purchase invoice. Those stored per-design quantities are what ADDS stock
    for the jobbed product (JobInRepository.received_back_totals_by_design),
    completing the cycle a job out starts by deducting the master's designs.

    Everything else the sheet prints - our own company as the receiver, the
    Job Manufacturer (Sender), our own DC number/date and the purchase
    invoice reference - is read live off the job out, which in turn reads it
    off its purchase invoice and job work. See build_sheet."""

    def __init__(self, job_in_repo: JobInRepository, job_out_service: "JobOutService",
                 product_repo: ProductRepository, design_repo: DesignRepository,
                 company_repo: CompanyRepository,
                 version_service: Optional["DocumentVersionService"] = None):
        self.job_in_repo = job_in_repo
        # Reused wholesale rather than re-deriving the chain: the job out
        # already resolves the manufacturer, the purchase invoice and the
        # jobbed product's expected designs.
        self.job_out_service = job_out_service
        self.product_repo = product_repo
        self.design_repo = design_repo
        self.company_repo = company_repo
        self.version_service = version_service

    # ---- reads --------------------------------------------------
    def get(self, job_in_id: int, company_id: int) -> JobIn:
        job_in = self.job_in_repo.get_by_id(job_in_id)
        if not job_in or job_in.company_id != company_id:
            # 404, not 403 - don't reveal that another company's job in exists.
            raise NotFoundError(f"Job in #{job_in_id} not found.")
        return job_in

    def list_all(self, company_id: int) -> List[JobIn]:
        return self.job_in_repo.list_all(company_id)

    def list_for_job_out(self, job_out_id: Optional[int], company_id: int) -> List[JobIn]:
        """Every job in received against this job out, newest first."""
        if not job_out_id:
            return []
        return [ji for ji in self.job_in_repo.list_for_job_out(job_out_id)
                if ji.company_id == company_id]

    # ---- permission --------------------------------------------------
    def _assert_can_modify(self, job_in: JobIn, current_user: User):
        if current_user.is_admin:
            return
        if job_in.created_by != current_user.id:
            raise PermissionDeniedError("You can only manage job ins you created yourself.")

    # ---- the printed sheet --------------------------------------------------
    def _resolve_job_out(self, job_out_id, company_id: int) -> JobOut:
        try:
            resolved_id = int(job_out_id) if job_out_id else None
        except (TypeError, ValueError):
            resolved_id = None
        if resolved_id is None:
            raise ValidationError("A job in must be received against a job out.")
        try:
            return self.job_out_service.get(resolved_id, company_id)
        except NotFoundError:
            raise ValidationError("That job out could not be found.")

    def build_sheet(self, job_in: JobIn, company_id: int) -> dict:
        """Everything the print template needs. Returns:

          company     our own letterhead - and also the RECEIVER block, since
                      the goods are coming back to us
          job_out     the challan these goods went out on (its
                      delivery_challan_no/date print as DELIVERY CHALLAN
                      NO/DATE, distinct from the manufacturer's own
                      jw_delivery_challan_no on the job in itself)
          sender      {name, address, gstin} - the Job Manufacturer (Sender),
                      i.e. the party the job out was addressed TO
          purchase_invoice  for the PURCHASE INVOICE NO/DATE cells
        """
        job_out = self.job_out_service.get(job_in.job_out_id, company_id)
        out_sheet = self.job_out_service.build_sheet(job_out, company_id)
        return {
            "company": self.company_repo.get(company_id) if self.company_repo else None,
            "job_out": job_out,
            # The job out's RECEIVER is this document's SENDER - the goods
            # are coming back from whoever they were sent to.
            "sender": out_sheet["receiver"],
            "purchase_invoice": out_sheet["purchase_invoice"],
        }

    # ---- prefill from the job out the button was pressed on ----------------
    def build_prefill_from_job_out(self, job_out: JobOut, company_id: int) -> dict:
        """Caller must have already loaded `job_out` via JobOutService.get(id,
        company_id), so ownership is verified before we get here.

        The lines come back prefilled with the jobbed product and one row per
        design at the quantity EXPECTED back (the job out sheet's own output
        designs), for the receiver to correct to what actually arrived. The
        transport block carries over from the job out as a starting point -
        goods often return with the same transporter - and the stock inward
        number/date are typed."""
        sheet = self.job_out_service.build_sheet(job_out, company_id)
        rows = []
        for goods in sheet["goods"]:
            if not goods["output_name"]:
                continue
            for design in goods["output_designs"]:
                rows.append({
                    "product_name": goods["output_name"],
                    "hsn_code": goods["hsn_code"] or "",
                    "design_name": design["design_name"],
                    "quantity_boxes": design["quantity"],
                })
        return {
            "job_out_id": job_out.id,
            "stock_inward_date": date.today().isoformat(),
            "transporter_name": sheet.get("transporter_name") or "",
            "transport_gstin": job_out.transport_gstin or "",
            "lr_no": job_out.lr_no or "",
            "vehicle_no": job_out.vehicle_no or "",
            "items": rows,
        }

    def jobbed_products_for_job_out(self, job_out: JobOut, company_id: int) -> list:
        """[{id, name, hsn_code, alt_qty, qty_unit, unit, designs:[{id, name}]}]
        - the jobbed products this job out sends for conversion, each with its
        full catalog design list, so the form's "add design" picker offers
        every design of the product rather than only the ones expected back."""
        sheet = self.job_out_service.build_sheet(job_out, company_id)
        result = []
        seen = set()
        for goods in sheet["goods"]:
            name = goods["output_name"]
            if not name or name in seen:
                continue
            seen.add(name)
            product = self._product_by_name(name, company_id)
            designs = self.design_repo.list_for_product(product.id) if product else []
            result.append({
                "id": product.id if product else "",
                "name": name,
                "hsn_code": (product.hsn_code if product else goods["hsn_code"]) or "",
                "alt_qty": (product.alternate_quantity if product else "") or "",
                "qty_unit": (product.quantity_unit if product else "BOX") or "BOX",
                "unit": (product.alternate_quantity_unit if product else "") or "",
                "designs": [{"id": d.id, "name": d.design_name} for d in designs],
            })
        return result

    def _product_by_name(self, product_name: str, company_id: int):
        """The jobbed product is named on the job work's design lines rather
        than carried as an id through the job out (whose sheet is derived),
        so it's resolved back to the catalog by name here - the same
        normalized match the packing list prefills use."""
        if not product_name:
            return None
        target = _normalize_name(product_name)
        for product in self.product_repo.list_all(company_id):
            if _normalize_name(product.product_name) == target:
                return product
        return None

    # ---- validation --------------------------------------------------
    def _build_items(self, company_id: int, raw_items: list) -> List[JobInItem]:
        """One row per design received back. Boxes are typed; Alt Qty is
        derived from the catalog product's alternate_quantity and persisted,
        so the sheet always agrees with what was saved.

        A design that can't be resolved in the catalog is kept as a plain
        name: the row still prints, it just never moves stock (design_id is
        what stock keys on) - the same rule packing list lines follow."""
        items = []
        for i, raw in enumerate(raw_items, start=1):
            product_name = (raw.get("product_name") or "").strip()
            if not product_name:
                continue
            design_name = (raw.get("design_name") or "").strip()
            try:
                quantity_boxes = float(raw.get("quantity_boxes") or 0)
            except ValueError:
                raise ValidationError(f"Row {i} ('{design_name or product_name}'): quantity must be a number.")
            if quantity_boxes < 0:
                raise ValidationError(f"Row {i} ('{design_name or product_name}'): quantity can't be negative.")

            # Only ever trust a product/design from this same company - same
            # trust boundary as JobWorkService._build_items.
            product = None
            product_id = None
            if raw.get("product_id"):
                try:
                    candidate = self.product_repo.get_by_id(int(raw["product_id"]))
                except (TypeError, ValueError):
                    candidate = None
                if candidate and candidate.company_id == company_id:
                    product, product_id = candidate, candidate.id
            if product is None:
                product = self._product_by_name(product_name, company_id)
                product_id = product.id if product else None
            if product:
                product_name = product.product_name

            design_id = None
            if raw.get("design_id"):
                try:
                    design = self.design_repo.get_by_id(int(raw["design_id"]))
                except (TypeError, ValueError):
                    design = None
                # The design must belong to THIS product - a job in is a
                # stock document, so a cross-product design id here would
                # credit the wrong product's stock (exactly the mismatch the
                # packing list prefills already suffer from).
                if design and product_id and design.product_id == product_id:
                    design_id = design.id
                    design_name = design.design_name
            if design_id is None and design_name and product_id:
                for design in self.design_repo.list_for_product(product_id):
                    if _normalize_name(design.design_name) == _normalize_name(design_name):
                        design_id = design.id
                        design_name = design.design_name
                        break

            quantity_value = 0.0
            if product and product.alternate_quantity:
                try:
                    quantity_value = round(quantity_boxes * float(product.alternate_quantity), 2)
                except (TypeError, ValueError):
                    quantity_value = 0.0

            items.append(JobInItem(
                id=None, job_in_id=None, sr_no=len(items) + 1,
                product_id=product_id, product_name=product_name,
                hsn_code=(product.hsn_code if product else (raw.get("hsn_code") or "").strip()) or None,
                design_id=design_id, design_name=design_name or None,
                quantity_boxes=quantity_boxes,
                quantity_unit=(product.quantity_unit if product else "BOX") or "BOX",
                quantity_value=quantity_value,
                unit=(product.alternate_quantity_unit if product else "SQM") or "SQM",
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    def _build_header(self, current_user: User, fields: dict, items: List[JobInItem],
                      job_in_id: Optional[int] = None) -> JobIn:
        stock_inward_no = (fields.get("stock_inward_no") or "").strip()
        if not stock_inward_no:
            raise ValidationError("Stock inward no is compulsory.")
        job_out = self._resolve_job_out(fields.get("job_out_id"), current_user.company_id)
        clash = self.job_in_repo.find_by_inward_no(current_user.company_id, stock_inward_no)
        if clash and clash.id != job_in_id:
            raise ValidationError(
                f"Stock inward no {stock_inward_no} is already used by another job in."
            )
        return JobIn(
            id=None, company_id=current_user.company_id, job_out_id=job_out.id,
            stock_inward_no=stock_inward_no,
            stock_inward_date=(fields.get("stock_inward_date") or "").strip() or date.today().isoformat(),
            created_by=current_user.id,
            jw_delivery_challan_no=(fields.get("jw_delivery_challan_no") or "").strip() or None,
            jw_delivery_challan_date=(fields.get("jw_delivery_challan_date") or "").strip() or None,
            transporter_name=(fields.get("transporter_name") or "").strip() or None,
            transport_gstin=(fields.get("transport_gstin") or "").strip() or None,
            lr_no=(fields.get("lr_no") or "").strip() or None,
            vehicle_no=(fields.get("vehicle_no") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
            items=items,
        )

    # ---- writes --------------------------------------------------
    def create(self, current_user: User, fields: dict, raw_items: list) -> JobIn:
        items = self._build_items(current_user.company_id, raw_items)
        job_in = self._build_header(current_user, fields, items)
        created = self.job_in_repo.create(job_in)
        if self.version_service:
            self.version_service.record("job_in", created, current_user.id)
        return created

    def update(self, current_user: User, job_in_id: int, fields: dict, raw_items: list) -> JobIn:
        existing = self.get(job_in_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        items = self._build_items(current_user.company_id, raw_items)
        job_in = self._build_header(current_user, fields, items, job_in_id=job_in_id)
        self.job_in_repo.update(job_in_id, job_in)
        updated = self.get(job_in_id, current_user.company_id)
        if self.version_service:
            self.version_service.record("job_in", updated, current_user.id)
        return updated

    def delete(self, current_user: User, job_in_id: int) -> None:
        existing = self.get(job_in_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        self.job_in_repo.delete(job_in_id)


# ============================================================
# EXPORT INVOICE SERVICE
# ============================================================
class ExportInvoiceService:
    """The customer/customs-facing Export Invoice at the buyer end of the
    pipeline. Mirrors ProformaInvoiceService, with the additions that make
    this document what it is:

    - It references MANY proforma invoices (many-to-many). Goods are
      prefilled (build_prefill_from_proformas) then edited - but NOT from
      those PIs' own quoted lines: each linked PI is walked through its
      purchase orders to their purchase invoices, and the goods lines come
      from what those purchase invoices actually record buying, restricted
      to invoices that also produced a Purchase Details row. Price is still
      the USD rate quoted on the PI (matched by product_id), so the buyer
      reads the agreed price rather than the INR purchase cost.
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
      invoices; a row is added for every purchase order regardless of its
      purchase_type (full-tax or exemption).
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
                 misc_list_service: Optional["MiscListService"] = None,
                 job_work_repo: Optional["JobWorkRepository"] = None,
                 job_out_repo: Optional["JobOutRepository"] = None,
                 job_in_repo: Optional["JobInRepository"] = None,
                 job_out_service: Optional["JobOutService"] = None):
        self.export_invoice_repo = export_invoice_repo
        self.product_repo = product_repo
        self.lead_repo = lead_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.purchase_order_repo = purchase_order_repo
        self.purchase_invoice_repo = purchase_invoice_repo
        self.company_repo = company_repo
        # Optional, wired in production: the chain that pulls returned
        # ("jobbed") goods into the Products card and the Job In details card -
        # each linked PI -> its job works -> their purchase invoices -> job
        # outs -> job ins. Unwired (tests that don't need it) just skips that
        # part of the prefill.
        self.job_work_repo = job_work_repo
        self.job_out_repo = job_out_repo
        self.job_in_repo = job_in_repo
        self.job_out_service = job_out_service
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
        """Sum the selected PIs' sea freight / insurance / certification /
        other charges / discount, take Nature of contract from the first PI's
        Terms of delivery, and walk each PI -> its purchase orders -> their
        purchase invoices to import EPCG / export-under / supplier
        purchase-detail rows (all purchase types, not just exemption) AND the
        goods lines themselves - goods now come from what was actually
        bought (those purchase invoices' own lines), priced at the USD rate
        quoted to the buyer on the PI, not from the PI's own quoted lines
        directly. Returns a dict the form/route consumes."""
        proformas = self._load_proformas(proforma_ids, company_id)
        first = proformas[0] if proformas else None

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

        # Walk the chain for EPCG / export-under / supplier purchase details.
        epcg_number = epcg_date = None
        purchase_details = []
        seen_pd = set()
        collected_pinvs = []
        seen_pinv_ids = set()
        po_by_id = {}
        for pi in proformas:
            for po in self.purchase_order_repo.list_for_proforma(pi.id):
                if po.company_id != company_id:
                    continue
                po_by_id[po.id] = po
                pinvs = [p for p in self.purchase_invoice_repo.list_for_purchase_order(po.id)
                         if p.company_id == company_id]
                for pinv in pinvs:
                    if not epcg_number and pinv.epcg_number:
                        epcg_number, epcg_date = pinv.epcg_number, pinv.epcg_date
                    if pinv.id not in seen_pinv_ids:
                        seen_pinv_ids.add(pinv.id)
                        collected_pinvs.append(pinv)
                # Every purchase (full-tax or exemption) contributes a supplier
                # name + GSTIN + invoice-no row. The name is the seller as that
                # purchase invoice recorded it (the purchase order's own seller
                # when it has none), snapshotted rather than looked up later -
                # the same treatment every other imported party name gets, so
                # renaming a supplier can't rewrite an already-issued export
                # invoice. purchase_type is the purchase invoice's own "Purchase
                # under" (falling back to the purchase order's when there's no
                # invoice yet) - it decides whether this row is one of the
                # exemption purchases the printed "Purchase Details of 0.1% GST"
                # block lists, and whether Supply meant for locks to LUT below.
                # epcg_number/epcg_date are that same purchase invoice's own
                # EPCG licence (display only - the export invoice's own single
                # EPCG line is resolved independently by _resolve_epcg).
                if pinvs:
                    for pinv in pinvs:
                        key = ((pinv.seller_gstin or po.seller_gstin or ""), (pinv.invoice_number or ""))
                        if key not in seen_pd:
                            seen_pd.add(key)
                            purchase_details.append({
                                "supplier_gstin": pinv.seller_gstin or po.seller_gstin,
                                "supplier_invoice_no": pinv.invoice_number,
                                "supplier_name": pinv.seller_name or po.seller_name,
                                "purchase_type": pinv.purchase_type or "full_tax",
                                "epcg_number": pinv.epcg_number,
                                "epcg_date": pinv.epcg_date,
                            })
                elif po.seller_gstin:
                    key = (po.seller_gstin, "")
                    if key not in seen_pd:
                        seen_pd.add(key)
                        purchase_details.append({"supplier_gstin": po.seller_gstin, "supplier_invoice_no": None,
                                                 "supplier_name": po.seller_name,
                                                 "purchase_type": po.purchase_type or "full_tax",
                                                 "epcg_number": None, "epcg_date": None})

        # Goods: sourced from the purchase invoices collected above - i.e.
        # exactly the invoices listed in Purchase Details - rather than from
        # the proforma's own quoted lines, so the export invoice reflects
        # what was actually bought. Product identity/HSN/unit come from the
        # purchase invoice lines (which carry neither price nor pallet info);
        # price, surface finish and a pallets-per-box ratio stay whatever was
        # quoted to the buyer, matched by product_id against the selected
        # PIs' own lines.
        #
        # One purchase invoice can itself cover several purchase orders as
        # separate line items for the SAME product (PurchaseInvoiceItem.
        # purchase_order_id) - e.g. one supplier shipment invoiced against two
        # POs at once. Left unmerged that put the same product on the Export
        # Invoice twice (see EXP/25-26/025). So every purchase-invoice line
        # for a product is summed into ONE goods line (boxes, qty and pallets
        # all add up), and which purchase order(s) contributed how many boxes
        # is kept alongside as `product_sources`, for the same traceability
        # Purchase Details already gives at the supplier level - persisted on
        # the invoice (export_invoice_product_sources) rather than recomputed
        # each time, since re-walking the chain later could disagree with
        # what was actually saved.
        price_by_product = {}
        surface_by_product = {}
        pallet_ratio_by_product = {}  # pallets per box, from the PI's own line
        # `pi.items`, not `pi.printed_items`: both documents now hold the same
        # kind of price - the typed FOB rate, with the charges added on top as
        # a document-level figure (ExportInvoice.cif_value_usd mirrors
        # ProformaInvoice.cif_value_usd). Taking the PI's CIF-priced view here
        # would fold its charges into the export invoice's per-unit rate and
        # then add the export invoice's own charges on top of that again.
        for pi in proformas:
            for it in pi.items:
                if it.product_id is None:
                    continue
                if it.product_id not in price_by_product:
                    price_by_product[it.product_id] = it.price_usd
                if it.product_id not in surface_by_product:
                    surface_by_product[it.product_id] = it.surface
                if it.product_id not in pallet_ratio_by_product and it.quantity_boxes:
                    pallet_ratio_by_product[it.product_id] = (it.pallets or 0) / it.quantity_boxes

        merged = {}  # product_id (or name, when it has none) -> aggregated line
        for pinv in collected_pinvs:
            full_pinv = self.purchase_invoice_repo.get_by_id(pinv.id)
            if not full_pinv:
                continue
            for it in full_pinv.items:
                key = it.product_id if it.product_id is not None else it.product_name
                line = merged.get(key)
                if line is None:
                    line = {"product_id": it.product_id, "product_name": it.product_name,
                            "hsn_code": it.hsn_code, "unit": it.unit,
                            "quantity_boxes": 0.0, "quantity_value": 0.0, "sources": {}}
                    merged[key] = line
                line["quantity_boxes"] += it.quantity_boxes or 0
                line["quantity_value"] += it.quantity_value or 0
                po = po_by_id.get(it.purchase_order_id)
                source_label = po.po_number if po else f"{full_pinv.invoice_number} (no PO)"
                line["sources"][source_label] = line["sources"].get(source_label, 0) + (it.quantity_boxes or 0)

        # Job work leg: goods sent out for conversion come back on a JOB IN,
        # never on a purchase invoice reachable through a purchase order - the
        # job-work purchase invoice links straight to the job work
        # (purchase_invoice_job_work_links). Walk each linked PI -> its job
        # works -> their purchase invoices -> job outs -> job ins: list one
        # row per job in for the read-only "Job In details" card, and merge
        # every job in's returned design lines into the SAME `merged` goods
        # dict - one aggregated line per jobbed product, boxes/qty summed
        # across every design and every return lot. Price is still taken from
        # price_by_product in the items loop below (the PI's quoted USD rate,
        # matched by product_id), 0 when the jobbed product was not itself a
        # quoted line. Display only, like product_sources - never printed.
        job_ins = []
        if self.job_work_repo and self.job_out_repo and self.job_in_repo:
            seen_job_in_ids = set()
            manufacturer_by_job_out = {}
            for pi in proformas:
                for jw in self.job_work_repo.list_for_proforma(pi.id):
                    if jw.company_id != company_id:
                        continue
                    for jw_pinv in self.purchase_invoice_repo.list_for_job_work(jw.id):
                        if jw_pinv.company_id != company_id:
                            continue
                        for jo in self.job_out_repo.list_for_purchase_invoice(jw_pinv.id):
                            if jo.company_id != company_id:
                                continue
                            for ji in self.job_in_repo.list_for_job_out(jo.id):
                                if ji.company_id != company_id or ji.id in seen_job_in_ids:
                                    continue
                                seen_job_in_ids.add(ji.id)
                                if jo.id not in manufacturer_by_job_out:
                                    receiver = {}
                                    if self.job_out_service:
                                        try:
                                            receiver = self.job_out_service.build_sheet(
                                                jo, company_id).get("receiver") or {}
                                        except Exception:
                                            receiver = {}
                                    manufacturer_by_job_out[jo.id] = receiver
                                receiver = manufacturer_by_job_out[jo.id]
                                job_ins.append({
                                    "manufacturer_name": receiver.get("name") or jo.seller_name,
                                    "manufacturer_gstin": receiver.get("gstin"),
                                    "job_out_challan_no": jo.delivery_challan_no,
                                    "jw_challan_no": ji.jw_delivery_challan_no,
                                    "jw_challan_date": ji.jw_delivery_challan_date,
                                    "stock_inward_no": ji.stock_inward_no,
                                    "stock_inward_date": ji.stock_inward_date,
                                })
                                full_ji = self.job_in_repo.get_by_id(ji.id) or ji
                                label = (f"JOB IN {ji.stock_inward_no}" if ji.stock_inward_no
                                         else f"JOB IN #{ji.id}")
                                for it in full_ji.items:
                                    key = it.product_id if it.product_id is not None else it.product_name
                                    line = merged.get(key)
                                    if line is None:
                                        line = {"product_id": it.product_id, "product_name": it.product_name,
                                                "hsn_code": it.hsn_code, "unit": it.unit,
                                                "quantity_boxes": 0.0, "quantity_value": 0.0, "sources": {}}
                                        merged[key] = line
                                    line["quantity_boxes"] += it.quantity_boxes or 0
                                    line["quantity_value"] += it.quantity_value or 0
                                    line["sources"][label] = line["sources"].get(label, 0) + (it.quantity_boxes or 0)

        items = []
        product_sources = []
        for line in merged.values():
            pid = line["product_id"]
            ratio = pallet_ratio_by_product.get(pid)
            items.append({
                "product_id": pid, "product_name": line["product_name"],
                "hsn_code": line["hsn_code"],
                "surface": surface_by_product.get(pid),
                "pallets": round(ratio * line["quantity_boxes"], 2) if ratio else None,
                "quantity_boxes": line["quantity_boxes"] or None, "quantity_value": line["quantity_value"],
                "unit": line["unit"], "price_usd": price_by_product.get(pid, 0.0),
                "igst_percent": self._product_igst_percent(pid, company_id),
            })
            for po_number, boxes in line["sources"].items():
                product_sources.append({
                    "product_name": line["product_name"], "po_number": po_number, "quantity_boxes": boxes,
                })

        # The government-scheme line of the Export Under block: the company's
        # government schemes, the same text the Annexure's section 13 defaults
        # to. Editable afterwards, and blanking it just means "use whatever
        # Our Company says today". The rest of that block - the SUPPLY MEANT
        # FOR EXPORT heading, the EPCG licence, the LUT number - is derived by
        # the sheets from tax_mode/epcg_number/the company, never baked in.
        company = self.company_repo.get(company_id)
        export_under = (company.government_schemes if company else None) or None

        # Supply meant for: any purchase under exemption (0.1% GST, the
        # merchant-exporter concessional rate) forces the export invoice onto
        # LUT - that concessional rate is only valid when the export itself
        # carries no IGST, so "With Payment of IGST" would misstate the very
        # thing the 0.1% purchase depends on. tax_mode is only ever included
        # here when this is true; otherwise the key is left out entirely
        # (same "fields no PI decides are simply absent" rule the rest of the
        # prefill follows), leaving whatever the user already has typed.
        # tax_mode_locked is always included so the form/JS knows to release
        # the field again when a reload no longer has an exemption purchase.
        has_exemption_purchase = any(pd.get("purchase_type") == "exemption" for pd in purchase_details)

        fields = {
            "proforma_invoice_ids": [pi.id for pi in proformas],
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
            "tax_mode_locked": has_exemption_purchase,
        }
        if has_exemption_purchase:
            fields["tax_mode"] = EXPORT_TAX_MODE_LUT
        return {"fields": fields, "items": items, "purchase_details": purchase_details,
                "product_sources": product_sources, "job_ins": job_ins}

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
                pallet_weight_kg = float(raw["pallet_weight_kg"]) if raw.get("pallet_weight_kg") else None
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
                igst_percent=igst_percent, pallet_weight_kg=pallet_weight_kg,
            ))
        if not items:
            raise ValidationError("At least one product line is compulsory.")
        return items

    def _resolve_epcg(self, proforma_ids: List[int], company_id: int):
        """EPCG licence no./date are never typed on the export invoice itself -
        they're read off whichever purchase invoice under the linked
        proforma invoices' purchase orders has one, same chain (and same
        first-match-wins rule) build_prefill_from_proformas walks for the
        "Load from PIs" prefill. Recomputed on every save rather than
        trusting a posted value, so it can't go stale if the purchase-side
        data changes after the export invoice was first created."""
        epcg_number = epcg_date = None
        for pid in proforma_ids or []:
            pi = self.proforma_invoice_repo.get_by_id(pid)
            if not pi or pi.company_id != company_id:
                continue
            for po in self.purchase_order_repo.list_for_proforma(pi.id):
                if po.company_id != company_id:
                    continue
                for pinv in self.purchase_invoice_repo.list_for_purchase_order(po.id):
                    if pinv.company_id == company_id and not epcg_number and pinv.epcg_number:
                        epcg_number, epcg_date = pinv.epcg_number, pinv.epcg_date
            if epcg_number:
                break
        return epcg_number, epcg_date

    def _has_exemption_purchase(self, proforma_ids: List[int], company_id: int) -> bool:
        """True the moment any purchase invoice reachable through the linked
        proforma invoices' purchase orders is itself under exemption (0.1%
        GST) - same chain build_prefill_from_proformas walks. Recomputed
        fresh on every save (never trusted from a posted purchase_details
        row) so tax_mode's LUT lock below can't be bypassed by a tampered
        POST, and can't disagree with the purchase side if it changes after
        the export invoice was first raised. A purchase order with no
        invoice raised against it yet has no "Purchase under" of its own to
        check - nothing to force LUT over until an actual purchase invoice
        exists."""
        for pid in proforma_ids or []:
            pi = self.proforma_invoice_repo.get_by_id(pid)
            if not pi or pi.company_id != company_id:
                continue
            for po in self.purchase_order_repo.list_for_proforma(pi.id):
                if po.company_id != company_id:
                    continue
                for pinv in self.purchase_invoice_repo.list_for_purchase_order(po.id):
                    if pinv.company_id == company_id and pinv.purchase_type == "exemption":
                        return True
        return False

    @staticmethod
    def _optional_int(raw) -> Optional[int]:
        """A posted id, or None when it's blank/unparseable - the form can
        legitimately have no booking picked."""
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

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

        # See the quotation builder: FOB drops the freight and the insurance;
        # CFR drops the insurance only. The certification is never dropped.
        nature_of_contract = (fields.get("nature_of_contract") or "").strip() or None
        no_sea_freight = drops_sea_freight(nature_of_contract)
        no_insurance = drops_insurance(nature_of_contract)
        no_certification = drops_certification(nature_of_contract)

        lead_id = int(fields["lead_id"]) if fields.get("lead_id") else None
        if lead_id is not None:
            lead = self.lead_repo.get_by_id(lead_id)
            if not lead or lead.company_id != current_user.company_id:
                lead_id = None

        # Computed early (rather than where it's assigned onto the invoice
        # below) so the linked PIs' currency is known before the currency
        # resolution just below needs it.
        proforma_ids = self._clean_proforma_ids(fields.get("proforma_invoice_ids"), current_user.company_id)
        epcg_number, epcg_date = self._resolve_epcg(proforma_ids, current_user.company_id)

        # Supply meant for: forced onto LUT the moment any linked purchase
        # invoice is under exemption, overriding whatever was posted (even a
        # tampered one) - see _has_exemption_purchase. Otherwise falls back
        # to whatever was typed, same as always.
        if self._has_exemption_purchase(proforma_ids, current_user.company_id):
            tax_mode = EXPORT_TAX_MODE_LUT
        else:
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
            # Government scheme is never stored per-invoice any more - the
            # printed sheets already fall back to OurCompany.government_schemes
            # whenever export_under is blank (see _sheet.html), so leaving it
            # None here means every sheet always shows the company's current
            # scheme, live, with no per-invoice copy to go stale.
            export_under=None,
            epcg_number=epcg_number,
            epcg_date=epcg_date,
            loading_type=loading_type, tax_mode=tax_mode,
            exchange_rate=_float("exchange_rate", 0),
            sea_freight=0 if no_sea_freight else _float("sea_freight", 0),
            insurance=0 if no_insurance else _float("insurance", 0),
            certification=0 if no_certification else _float("certification", 0),
            other_charges=_float("other_charges", 0),
            discount_amount=_float("discount_amount", 0),
            # Export invoices no longer have an FOB-typed-price mode either -
            # the typed price is always the absolute FOB price and
            # ExportInvoice.cif_value_usd builds CIF upward from it, exactly
            # as the quotation and proforma invoice builders above do.
            # Hardcoded off (rather than read from `fields`) so even a direct
            # API/service call can't revive the old uplift.
            fob_pricing=False,
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
            # The booking's own id alongside the number it prints - the number
            # can be re-typed or a booking renumbered, so this is what still
            # says which Booking Detail the 11B rows were copied from. The
            # rows themselves stay a snapshot.
            booking_detail_id=self._optional_int(fields.get("booking_detail_id")),
            vessel_name=(fields.get("vessel_name") or "").strip() or None,
            voyage_no=(fields.get("voyage_no") or "").strip() or None,
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
        invoice.product_sources = self._clean_product_sources(fields.get("product_sources"))
        invoice.job_ins = self._clean_job_ins(fields.get("job_ins"))
        return invoice

    def _clean_proforma_ids(self, raw_ids, company_id: int) -> List[int]:
        """One export invoice covers a single buyer, so every selected PI
        must share the same buyer - its consignee name. The form already
        restricts the picker to one buyer at a time; this is the
        server-side backstop."""
        pis = []
        for pid in dict.fromkeys(raw_ids or []):
            try:
                pi = self.proforma_invoice_repo.get_by_id(int(pid))
            except (TypeError, ValueError):
                continue
            if pi and pi.company_id == company_id:
                pis.append(pi)
        if pis:
            keys = {(pi.consignee_name or "").strip().lower() for pi in pis}
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
        #
        # transporter_name is excluded from the "is this row blank" check
        # below: the form now sends one invoice-level transporter stamped
        # onto every row (see routes/export_invoices.py _extract_container_details),
        # so a genuinely empty placeholder row (nothing else typed) must
        # still be dropped even though it carries that stamped value.
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None
                      for k in ("container_type", "container_no", "line_seal_no", "rfid_seal_no", "vehicle_no",
                                "lr_no", "max_permitted_weight")}
            transporter_name = (r.get("transporter_name") or "").strip() or None
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
                values["transporter_name"] = transporter_name
                rows.append(values)
        return rows

    @staticmethod
    def _clean_purchase_details(raw) -> List[dict]:
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None
                      for k in ("supplier_gstin", "supplier_invoice_no", "supplier_name",
                                "epcg_number", "epcg_date")}
            if any(values.values()):
                values["purchase_type"] = (r.get("purchase_type") or "").strip() or "full_tax"
                rows.append(values)
        return rows

    @staticmethod
    def _clean_product_sources(raw) -> List[dict]:
        """Read-only breakdown - which purchase order(s) each goods line's
        boxes came from - round-tripped as hidden fields rather than
        recomputed on every save, since re-walking the PI chain later could
        disagree with what a since-edited purchase order/invoice says now."""
        rows = []
        for r in raw or []:
            product_name = (r.get("product_name") or "").strip()
            po_number = (r.get("po_number") or "").strip()
            try:
                quantity_boxes = float(r.get("quantity_boxes") or 0)
            except (TypeError, ValueError):
                quantity_boxes = 0
            if product_name and po_number:
                rows.append({"product_name": product_name, "po_number": po_number,
                            "quantity_boxes": quantity_boxes})
        return rows

    @staticmethod
    def _clean_job_ins(raw) -> List[dict]:
        """The read-only "Job In details" breakdown - which job in(s) the
        returned/jobbed goods were imported from - round-tripped as hidden
        fields so it survives a save/reopen, exactly like _clean_product_sources.
        Display only, never printed."""
        keys = ("manufacturer_name", "manufacturer_gstin", "job_out_challan_no",
                "jw_challan_no", "jw_challan_date", "stock_inward_no", "stock_inward_date")
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None for k in keys}
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
                                    fields: dict, pdf_file=None, remove_pdf: bool = False) -> ExportInvoice:
        """The commercial invoice packing list's bill of lading number, date
        and an optional uploaded PDF of the bill of lading itself - the only
        cells on that sheet that aren't derived. The PDF is saved/removed the
        same way the export invoice's own Shipping Bill PDF is
        (_save_pdf/_delete_pdf_file)."""
        existing = self.get(invoice_id, current_user.company_id)
        self._assert_can_modify(existing, current_user)
        names = ExportInvoiceRepository.PACKING_LIST_FIELDS
        cleaned = {name: (fields.get(name) or "").strip() or None for name in names}
        if pdf_file and pdf_file.filename:
            cleaned["bill_of_lading_pdf_path"] = self._save_pdf(pdf_file)
            self._delete_pdf_file(existing.bill_of_lading_pdf_path)
        elif remove_pdf:
            self._delete_pdf_file(existing.bill_of_lading_pdf_path)
            cleaned["bill_of_lading_pdf_path"] = None
        else:
            cleaned["bill_of_lading_pdf_path"] = existing.bill_of_lading_pdf_path
        self.export_invoice_repo.update_document_fields(
            invoice_id, cleaned, names + ("bill_of_lading_pdf_path",))
        return self.get(invoice_id, current_user.company_id)

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
                 category_repo: Optional[CategoryRepository] = None,
                 design_repo: Optional[DesignRepository] = None,
                 packing_list_repo: Optional[PackingListRepository] = None,
                 designs_packing_list_repo: Optional["ExportDesignsPackingListRepository"] = None,
                 job_in_repo: Optional["JobInRepository"] = None):
        self.export_packing_list_repo = export_packing_list_repo
        self.export_invoice_repo = export_invoice_repo
        self.product_repo = product_repo
        self.category_repo = category_repo
        # All four are only used by the Designs Packing List (the design
        # allocation and the document it becomes) - the container split
        # itself needs none of them, so they stay optional. job_in_repo lets
        # the allocation reference also offer designs that only entered stock
        # through a job-work return (a Job In), never a purchase invoice.
        self.design_repo = design_repo
        self.packing_list_repo = packing_list_repo
        self.designs_packing_list_repo = designs_packing_list_repo
        self.job_in_repo = job_in_repo

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

    # ---- Designs Packing List: the document --------------------------------------------------
    def get_designs_document(self, export_invoice_id: int, company_id: int) -> Optional[ExportDesignsPackingList]:
        if not self.designs_packing_list_repo:
            return None
        doc = self.designs_packing_list_repo.get_for_invoice(export_invoice_id)
        return doc if doc and doc.company_id == company_id else None

    def create_designs_document(self, current_user: User, export_invoice_id: int) -> ExportDesignsPackingList:
        """Turns a filled-in allocation into the DESIGNS PACKING LIST proper,
        giving it its own DSGPL number and date. Refuses while any container
        line is still part-allocated: the sheet is a packing list, and one
        that accounts for only some of a container's boxes is worse than
        none. Creating it twice is a no-op that returns the existing
        document - the number it already went out under is never reissued."""
        company_id = current_user.company_id
        existing = self.get_designs_document(export_invoice_id, company_id)
        packing_list = self.get_for_invoice(export_invoice_id, company_id)
        if not packing_list:
            raise ValidationError(
                "This export invoice has no container split yet - open the invoice, "
                "allocate its goods to containers, and save."
            )
        unfilled = []
        for item in packing_list.items:
            allocated = sum(d.quantity_boxes or 0 for d in item.designs)
            if abs(allocated - (item.quantity_boxes or 0)) > _BOX_TOLERANCE:
                unfilled.append(f"container {item.container_sr_no} ({item.product_name})")
        if unfilled:
            raise ValidationError(
                "Every container's boxes must be split across designs first - still to do: "
                + ", ".join(dict.fromkeys(unfilled)) + "."
            )
        if existing:
            self.designs_packing_list_repo.touch(existing.id)
            return self.designs_packing_list_repo.get_by_id(existing.id)
        packing_list_date = (packing_list.invoice.invoice_date if packing_list.invoice
                             else datetime.now().strftime("%Y-%m-%d"))
        return self.designs_packing_list_repo.create(ExportDesignsPackingList(
            id=None, company_id=company_id, export_invoice_id=export_invoice_id,
            packing_list_number=self.designs_packing_list_repo.next_number(company_id, packing_list_date),
            packing_list_date=packing_list_date, created_by=current_user.id,
        ))

    def list_designs_documents(self, company_id: int) -> List[ExportDesignsPackingList]:
        return self.designs_packing_list_repo.list_all(company_id) if self.designs_packing_list_repo else []

    # ---- Designs Packing List (per-line design allocation) --------------------------------------------------
    def _received_design_totals(self, company_id: int, product_id: int,
                                export_invoice_id: int) -> List[dict]:
        """Every design received for one product on this shipment, merged
        across both ways goods come in: bought against a purchase invoice
        (PackingListRepository.design_totals_for_product, scoped to this
        export invoice's source purchase orders) and returned from job work
        on a Job In (JobInRepository.returned_design_totals_for_product,
        scoped to its source job works). Rows are keyed by design and their
        boxes/qty summed, so a design that came in both ways totals
        correctly. The one shared source for reference_designs and
        save_design_allocation, so the form and the save-time cap can never
        disagree about how much of a design came in."""
        if not self.packing_list_repo or not product_id:
            return []
        source_po_ids = self.export_invoice_repo.source_purchase_order_ids(
            export_invoice_id, company_id
        )
        merged: dict = {}
        for r in self.packing_list_repo.design_totals_for_product(
                company_id, int(product_id), source_po_ids):
            merged[r["design_id"]] = dict(r)
        if self.job_in_repo:
            job_work_ids = self.export_invoice_repo.source_job_work_ids(
                export_invoice_id, company_id
            )
            for r in self.job_in_repo.returned_design_totals_for_product(
                    company_id, int(product_id), job_work_ids):
                existing = merged.get(r["design_id"])
                if existing is None:
                    merged[r["design_id"]] = dict(r)
                else:
                    existing["boxes"] = (existing.get("boxes") or 0) + (r.get("boxes") or 0)
                    existing["quantity"] = (existing.get("quantity") or 0) + (r.get("quantity") or 0)
        return list(merged.values())

    def reference_designs(self, company_id: int, packing_list: ExportPackingList) -> dict:
        """(invoice_item_sr_no, container_sr_no) -> the design rows this
        specific container/line's allocation form should offer: every design
        that actually came in for its product ON THIS SHIPMENT (scoped to the
        purchase orders that fed this export invoice - see
        ExportInvoiceRepository.source_purchase_order_ids), each carrying:

        - `on_this_line`: boxes already allocated to THIS line (prefills its
          checkbox/qty box)
        - `remaining`: boxes still needing a container ACROSS THE WHOLE
          INVOICE, counting this line's own share back in - so a design shows
          0 remaining once every container between them has claimed all of
          it, and a row with 0 remaining is dropped from every line except
          the one(s) that already hold it (so it stays editable there, but
          stops being offered as an option on later containers once there's
          nothing left of it to load)."""
        if not self.packing_list_repo:
            return {}
        by_product: dict = {}
        # Total already allocated per design, across every container on this
        # invoice - what "remaining" is measured against.
        allocated_by_design: dict = {}
        for item in packing_list.items:
            for d in item.designs:
                if d.design_id:
                    allocated_by_design[d.design_id] = allocated_by_design.get(d.design_id, 0) + (d.quantity_boxes or 0)

        reference: dict = {}
        for item in packing_list.items:
            key = (item.invoice_item_sr_no, item.container_sr_no)
            if not item.product_id:
                reference[key] = []
                continue
            if item.product_id not in by_product:
                by_product[item.product_id] = self._received_design_totals(
                    company_id, int(item.product_id), packing_list.export_invoice_id
                )
            on_this_line = {d.design_id: d.quantity_boxes or 0 for d in item.designs if d.design_id}
            rows = []
            for r in by_product[item.product_id]:
                design_id = r.get("design_id")
                received = r.get("boxes") or 0
                mine = on_this_line.get(design_id, 0)
                remaining = round(received - allocated_by_design.get(design_id, 0) + mine, 2)
                if remaining <= _BOX_TOLERANCE and not mine:
                    continue  # nothing left of this design, and this line never held it
                rows.append({**r, "on_this_line": mine, "remaining": max(remaining, 0)})
            reference[key] = rows
        return reference

    def save_design_allocation(self, company_id: int, export_packing_list_id: int,
                               invoice_item_sr_no: int, container_sr_no: int, raw_rows: list) -> None:
        """Replaces one container-split line's design breakdown. The boxes
        allocated across its design rows must add up to EXACTLY that line's
        own boxes - the same all-or-nothing rule _assert_balanced applies one
        level up, and for the same reason: a design short is a box nobody can
        account for, a design over is the same box counted twice."""
        packing_list = self.get(export_packing_list_id, company_id)
        line = next(
            (i for i in packing_list.items
             if i.invoice_item_sr_no == invoice_item_sr_no and i.container_sr_no == container_sr_no),
            None,
        )
        if not line:
            raise NotFoundError("That line is no longer on this packing list - reload the page and try again.")

        # How many boxes of each design the OTHER containers have already
        # claimed - a design can't be loaded onto this one beyond what that
        # leaves, or the same physical boxes ship twice. Excludes this line's
        # own current allocation, which is being replaced.
        claimed_elsewhere: dict = {}
        for other in packing_list.items:
            if other.invoice_item_sr_no == invoice_item_sr_no and other.container_sr_no == container_sr_no:
                continue
            for d in other.designs:
                if d.design_id:
                    claimed_elsewhere[d.design_id] = claimed_elsewhere.get(d.design_id, 0) + (d.quantity_boxes or 0)
        received_by_design = {}
        if self.packing_list_repo and line.product_id:
            received_by_design = {
                r["design_id"]: (r.get("boxes") or 0)
                for r in self._received_design_totals(
                    company_id, int(line.product_id), packing_list.export_invoice_id
                )
            }

        rows = []
        for i, raw in enumerate(raw_rows, start=1):
            design_id = int(raw["design_id"]) if raw.get("design_id") else None
            if not design_id:
                continue
            try:
                quantity_boxes = float(raw.get("quantity_boxes") or 0)
            except (TypeError, ValueError):
                raise ValidationError(f"Design row {i}: boxes must be a number.")
            if quantity_boxes <= 0:
                raise ValidationError(f"Design row {i}: boxes must be greater than zero.")
            # Same ownership check every other design reference in this app
            # applies: it must be this company's, and it must live under the
            # line's own product.
            design = self.design_repo.get_by_id(design_id) if self.design_repo else None
            if not design or design.company_id != company_id or \
                    (line.product_id and design.product_id != line.product_id):
                raise ValidationError(
                    f"Design row {i}: that design doesn't belong to '{line.product_name}'."
                )
            # Never load more of a design than this shipment actually received
            # once the other containers have taken their share.
            if design_id in received_by_design:
                loadable = received_by_design[design_id] - claimed_elsewhere.get(design_id, 0)
                if quantity_boxes - loadable > _BOX_TOLERANCE:
                    raise ValidationError(
                        f"'{design.design_name}': only {max(loadable, 0):g} boxes are left to load "
                        f"(the other containers already hold {claimed_elsewhere.get(design_id, 0):g} "
                        f"of the {received_by_design[design_id]:g} received)."
                    )
            # Qty follows the boxes at the line's own per-box rate, so the
            # design rows always add back up to the line - the same reasoning
            # behind _per_box for the container split itself.
            qty_per_box = self._per_box(line.quantity_value, line.quantity_boxes)
            rows.append(ExportPackingListItemDesign(
                id=None, export_packing_list_id=export_packing_list_id,
                invoice_item_sr_no=invoice_item_sr_no, container_sr_no=container_sr_no,
                design_id=design_id, design_name=design.design_name,
                quantity_boxes=quantity_boxes,
                quantity_value=round(quantity_boxes * qty_per_box, 2) if qty_per_box is not None else 0,
                unit=line.unit,
            ))

        # No design rows at all is the untouched state, and clearing every row
        # is how you get back to it - only a PARTIALLY filled line is refused.
        line_boxes = line.quantity_boxes or 0
        allocated = sum(r.quantity_boxes for r in rows)
        if rows:
            diff = allocated - line_boxes
            if diff > _BOX_TOLERANCE:
                raise ValidationError(
                    f"'{line.product_name}' in container {container_sr_no}: {allocated:g} boxes split across "
                    f"designs, but the line only has {line_boxes:g} - remove {diff:g}."
                )
            if diff < -_BOX_TOLERANCE:
                raise ValidationError(
                    f"'{line.product_name}' in container {container_sr_no}: {allocated:g} of {line_boxes:g} "
                    f"boxes split across designs - {-diff:g} still unassigned."
                )

        self.export_packing_list_repo.save_item_designs(
            export_packing_list_id, invoice_item_sr_no, container_sr_no, rows
        )
        # An already-issued document keeps its number and date, but records
        # that what it prints has changed.
        doc = self.get_designs_document(packing_list.export_invoice_id, company_id)
        if doc:
            self.designs_packing_list_repo.touch(doc.id)

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

            pallets = self._override(alloc.get("pallets"), default=pallets)

            net = self._override(alloc.get("net_weight_kg"))
            gross = self._override(alloc.get("gross_weight_kg"))
            if net is None and boxes is not None and product and product.net_weight_kg:
                net = round(boxes * product.net_weight_kg, 2)
            if gross is None and boxes is not None:
                # Gross = Net + Plts x the weight of whichever named pallet
                # type was selected on the goods line (item.pallet_weight_kg,
                # snapshotted the moment it was picked - see
                # ExportInvoiceItem.pallet_weight_kg and the form's
                # recalcRowPallets). Falls back to the old flat per-box
                # formula when no pallet type is known for the line (Loose,
                # or Plts typed by hand with no type picked).
                if item.pallet_weight_kg and pallets:
                    gross = round((net or 0) + pallets * item.pallet_weight_kg, 2)
                elif product and product.gross_weight_kg:
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
                pallets=pallets,
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
                 purchase_invoice_repo: Optional[PurchaseInvoiceRepository] = None,
                 job_work_repo: Optional[JobWorkRepository] = None):
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
        # Optional: lets a Job Work's own packing list validate its job_work_id
        # and prefill from the job work's Products card.
        self.job_work_repo = job_work_repo

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

    def list_for_job_work(self, job_work_id: int, company_id: int) -> List[PackingList]:
        """Every packing list generated from one Job Work (the job work's own
        PL), company-scoped - same as list_for_purchase_order."""
        return [pl for pl in self.packing_list_repo.list_for_job_work(job_work_id)
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

    def _design_id_resolver(self, company_id: int):
        """Returns a resolve(product_id, design_name) function that matches a
        typed design name against that product's own catalog designs,
        falling back to a company-wide by-name match when the design isn't
        catalogued under this exact product (e.g. it only exists under a
        sibling size/finish variant of the same tile), OR when there is no
        product_id to try at all - a job work design line not sourced from
        a proforma product (see JobWorkItem.product_id's own docstring)
        still names a real design, it just has nothing to narrow the lookup
        by, so skipping straight to the company-wide match is the only way
        it ever resolves. Without this, such a line's design column prints
        its name but silently drops the photo, since design_id never gets
        set at all. Lookups are cached per product/company across every
        call made through the returned function, shared by
        build_prefill_from_job_work and _placeholder_items_from_job_works."""
        design_ids_by_product: dict = {}
        design_ids_by_name: Optional[dict] = None

        def resolve(product_id, design_name):
            nonlocal design_ids_by_name
            if not design_name:
                return None
            design_id = None
            if product_id:
                if product_id not in design_ids_by_product:
                    design_ids_by_product[product_id] = {
                        _normalize_name(d.design_name): d.id
                        for d in self.design_repo.list_for_product(product_id)
                    }
                design_id = design_ids_by_product[product_id].get(_normalize_name(design_name))
            if design_id is None:
                if design_ids_by_name is None:
                    design_ids_by_name = {
                        _normalize_name(d.design_name): d.id
                        for d in self.design_repo.list_for_company(company_id)
                    }
                design_id = design_ids_by_name.get(_normalize_name(design_name))
            return design_id

        return resolve

    def _placeholder_items_from_job_works(self, purchase_invoice_items: list, job_works: list) -> list:
        """Product-level placeholder rows (see _placeholder_items), but
        exploded into one row per design when the purchase invoice's source
        job work(s) tag that product with individual design lines. A
        purchase invoice built straight off job works (no purchase_order_id)
        has neither an upstream packing list nor a linked PO to borrow one
        design per product from, so without this it falls all the way back
        to one bare, design-less block per product line.

        A job work prints/numbers as a purchase order in this reduced flow
        (see PurchaseInvoice.job_work_id), and can just as well already have
        its own packing list - made directly against the job work, same as a
        real PO's own PL. That packing list's actual Boxes/Qty/Pallets/
        weights are imported product-by-product, same as a PO's; only
        products with no packing list of their own (or no packing list on
        the job work at all) fall back to a design row with boxes/quantity
        left blank for the user to fill in by hand."""
        design_lines_by_jw_product: dict = {}
        source_pl_items_by_jw: dict = {}
        for jw in job_works:
            grouped: dict = {}
            for line in jw.items:
                if line.design_name:
                    grouped.setdefault(line.product_id, []).append(line)
            design_lines_by_jw_product[jw.id] = grouped
            source_pl = self._newest_packing_list(
                self.packing_list_repo.list_for_job_work(jw.id), jw.company_id)
            if source_pl:
                by_product: dict = {}
                for pl_item in self._items_from_packing_list(source_pl):
                    by_product.setdefault(pl_item["product_id"], []).append(pl_item)
                source_pl_items_by_jw[jw.id] = by_product
        resolve_design_id = self._design_id_resolver(job_works[0].company_id) if job_works else None

        result = []
        for item in purchase_invoice_items:
            pl_items = source_pl_items_by_jw.get(item.job_work_id, {}).get(item.product_id)
            if pl_items:
                for pl_item in pl_items:
                    row = dict(pl_item)
                    row["product_name"], row["hsn_code"] = item.product_name, item.hsn_code
                    # The job work's own packing list can have been saved
                    # before its design lines resolved a catalog id (e.g.
                    # created before this photo-fallback logic existed, or a
                    # row the user hand-added there without a product to
                    # narrow the match by) - re-resolve here rather than
                    # carrying a stale blank forward onto this new PL too.
                    if not row["design_id"] and row["design_name"]:
                        row["design_id"] = resolve_design_id(row["product_id"], row["design_name"])
                    result.append(row)
                continue
            design_lines = design_lines_by_jw_product.get(item.job_work_id, {}).get(item.product_id)
            if not design_lines:
                result.append({
                    "product_id": item.product_id, "product_name": item.product_name,
                    "design_id": None, "design_name": "",
                    "hsn_code": item.hsn_code, "box_per_pallet": "", "pallets": "",
                    "quantity_boxes": "", "pcs": "",
                    "quantity_value": "", "unit": item.unit,
                    "net_weight_kg": "", "gross_weight_kg": "",
                    "is_placeholder": True,
                })
                continue
            for line in design_lines:
                result.append({
                    "product_id": item.product_id, "product_name": item.product_name,
                    "design_id": resolve_design_id(item.product_id, line.design_name),
                    "design_name": line.design_name or "",
                    "hsn_code": item.hsn_code, "box_per_pallet": "", "pallets": "",
                    "quantity_boxes": "", "pcs": "",
                    "quantity_value": "", "unit": item.unit,
                    "net_weight_kg": "", "gross_weight_kg": "",
                })
        return result

    def _placeholder_items(self, source_items: list, design_source_items: Optional[list] = None) -> list:
        """One product block per source line, with boxes/qty left blank for
        the user to fill in - used when no upstream packing list exists to
        import. When the line already carries a design (a purchase order's
        own item can be design-tagged, see PurchaseOrderRepository/v77), or
        one can be matched off `design_source_items` by product (a purchase
        invoice's own items never carry a design tag themselves, so its
        design comes from the linked purchase order's matching line
        instead), a real design row is emitted with that design filled in;
        otherwise the block is marked is_placeholder (header only, no design
        row at all) so the form doesn't render a blank design row, and the
        user picks a design themselves via "+ Add design"."""
        design_by_key = {}
        for item in (design_source_items or []):
            design_id = getattr(item, "design_id", None)
            design_name = getattr(item, "design_name", None)
            if design_id or design_name:
                key = _product_key({"product_id": item.product_id, "product_name": item.product_name})
                design_by_key.setdefault(key, (design_id, design_name))

        result = []
        for item in source_items:
            design_id = getattr(item, "design_id", None)
            design_name = getattr(item, "design_name", None)
            if not (design_id or design_name):
                design_id, design_name = design_by_key.get(
                    _product_key({"product_id": item.product_id, "product_name": item.product_name}), (None, None)
                )
            row = {
                "product_id": item.product_id, "product_name": item.product_name,
                "design_id": design_id, "design_name": design_name or "",
                "hsn_code": item.hsn_code, "box_per_pallet": "", "pallets": "",
                "quantity_boxes": "", "pcs": "",
                "quantity_value": "", "unit": item.unit,
                "net_weight_kg": "", "gross_weight_kg": "",
            }
            if not (design_id or design_name):
                row["is_placeholder"] = True
            result.append(row)
        return result

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
        goods. The invoice's packing list can carry products the invoice
        never split across separate POs for, or products this particular PO
        simply doesn't cover - those are filtered out too, so only the
        products actually on this PO's own line items ever show up here."""
        fields = {
            "purchase_order_id": purchase_order.id,
            "buyer_order_no": purchase_order.seller_ref_no,
            "remarks": purchase_order.remarks or "MADE IN INDIA",
        }
        source_pl = self._ancestor_packing_list(
            purchase_order.company_id, proforma_invoice_id=purchase_order.proforma_invoice_id)
        if source_pl:
            po_product_keys = {
                _product_key({"product_id": item.product_id, "product_name": item.product_name})
                for item in purchase_order.items
            }
            items = self._items_from_packing_list(source_pl)
            items = [item for item in items if _product_key(item) in po_product_keys]
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
        packing list of its own yet - unless the invoice was built straight
        off job work(s) instead of a PO, in which case the fallback explodes
        each product line into its job work's own design rows (see
        _placeholder_items_from_job_works) rather than leaving the block
        design-less, since job works carry their design breakdown on their
        own items, not a linked document's."""
        fields = {
            "purchase_invoice_id": purchase_invoice.id,
            "buyer_order_no": purchase_invoice.seller_ref_no,
            "remarks": purchase_invoice.remarks or "MADE IN INDIA",
        }
        source_pl = None
        linked_po = None
        if purchase_invoice.purchase_order_id:
            source_pl = self._newest_packing_list(
                self.packing_list_repo.list_for_purchase_order(purchase_invoice.purchase_order_id),
                purchase_invoice.company_id,
            )
            if self.purchase_order_repo:
                linked_po = self.purchase_order_repo.get_by_id(purchase_invoice.purchase_order_id)
        if source_pl:
            items = self._items_from_packing_list(source_pl)
        elif not linked_po and self.job_work_repo and purchase_invoice.job_work_ids:
            job_works = [
                jw for jw in (
                    self.job_work_repo.get_by_id(jw_id) for jw_id in purchase_invoice.job_work_ids
                ) if jw
            ]
            items = self._placeholder_items_from_job_works(purchase_invoice.items, job_works)
        else:
            # The invoice's own items never carry a design tag - only a
            # purchase order's own line can be (see v77) - so the design
            # comes off the linked PO's matching product line instead.
            items = self._placeholder_items(
                purchase_invoice.items, design_source_items=linked_po.items if linked_po else None
            )
        return {"fields": fields, "items": items}

    # ---- prefill from an existing job work --------------------------------------------------
    def build_prefill_from_job_work(self, job_work: "JobWork") -> dict:
        """The Job Work's own packing list. Caller must have already loaded
        `job_work` via JobWorkService.get(...) so cross-company ownership is
        already verified. Unlike the purchase order/invoice imports above,
        there is no upstream packing list to bring in wholesale - instead the
        design rows come straight off the Job Manufacturer card's own design
        lines (job_work.items), one row per design, with Boxes = that
        design's JOB QUANTITY (the document's one final figure per design)
        rather than a typed/imported value. Quantity (SQM/LM) is left for
        the Boxes x Alt Quantity auto-calc to fill in once the row's product
        is recognised, same as every other packing list row.

        The row's Product is taken from the Products card (job_work.products,
        matched by product_id), NOT from the design line's own to_product -
        to_product is only the SIZE-CONVERSION target a design line
        optionally names (see JobWorkItem's own docstring), whereas the
        Products card is the actual costing/reference line the goods are
        being packed against. A design line whose product_id has no matching
        Products card row falls back to its own product_id/product_name.

        job_work_items.design_id is never posted (design is matched by NAME
        only there, not a catalog id - see the job work form's own
        designsForToProduct()), so it has to be re-resolved against the
        resolved product's own catalog designs here; without it the packing
        list's design photo column has nothing to look up and prints blank.
        A design not catalogued under this exact product (e.g. it only
        exists under a sibling size/finish variant of the same tile) falls
        back to a company-wide by-name match, so the photo still shows up
        rather than silently going blank over a product mismatch."""
        fields = {
            "job_work_id": job_work.id,
            "buyer_order_no": job_work.seller_ref_no,
            "remarks": job_work.remarks or "MADE IN INDIA",
        }
        products_by_id = {p.product_id: p for p in job_work.products if p.product_id}
        resolve_design_id = self._design_id_resolver(job_work.company_id)
        items = []
        for item in job_work.items:
            product = products_by_id.get(item.product_id)
            product_id = product.product_id if product else item.product_id
            product_name = product.product_name if product else (item.product_name or item.to_product_name)
            hsn_code = product.hsn_code if product else item.hsn_code
            design_id = resolve_design_id(product_id, item.design_name)

            items.append({
                "product_id": product_id, "product_name": product_name,
                "design_id": design_id, "design_name": item.design_name or "",
                "hsn_code": hsn_code, "box_per_pallet": "", "pallets": "",
                "quantity_boxes": item.job_quantity or "", "pcs": "",
                "quantity_value": "", "unit": item.unit,
                "net_weight_kg": "", "gross_weight_kg": "",
            })
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
            # keep product/design references from this same company. The
            # Boxes column's unit (printed as small text after the number)
            # is likewise always the product's own Quantity unit.
            #
            # A design is NOT required to belong to the row's own product -
            # a job work's design lines are prefilled by a company-wide
            # by-name match whenever the design isn't catalogued under that
            # exact product (see PackingListService._design_id_resolver), so
            # enforcing an exact product match here would silently null out
            # every one of those on save, right back to a photo-less row.
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
                if not design or design.company_id != company_id:
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

        proforma_invoice_id = int(fields["proforma_invoice_id"]) if fields.get("proforma_invoice_id") else None
        if proforma_invoice_id is not None:
            # Only trust a proforma invoice from this same company - same reasoning as quotation_id below.
            invoice = self.proforma_invoice_repo.get_by_id(proforma_invoice_id)
            if not invoice or invoice.company_id != current_user.company_id:
                proforma_invoice_id = None

        quotation_id = int(fields["quotation_id"]) if fields.get("quotation_id") else None
        if quotation_id is not None and self.quotation_repo is not None:
            # Only trust a quotation from this same company - otherwise a crafted
            # quotation_id could attach this packing list to another company's quotation.
            quotation = self.quotation_repo.get_by_id(quotation_id)
            if not quotation or quotation.company_id != current_user.company_id:
                quotation_id = None

        purchase_order_id = int(fields["purchase_order_id"]) if fields.get("purchase_order_id") else None
        if purchase_order_id is not None and self.purchase_order_repo is not None:
            # Only trust a purchase order from this same company - same reasoning as quotation_id above.
            purchase_order = self.purchase_order_repo.get_by_id(purchase_order_id)
            if not purchase_order or purchase_order.company_id != current_user.company_id:
                purchase_order_id = None

        purchase_invoice_id = int(fields["purchase_invoice_id"]) if fields.get("purchase_invoice_id") else None
        if purchase_invoice_id is not None and self.purchase_invoice_repo is not None:
            # Only trust a purchase invoice from this same company - same reasoning as quotation_id above.
            purchase_invoice = self.purchase_invoice_repo.get_by_id(purchase_invoice_id)
            if not purchase_invoice or purchase_invoice.company_id != current_user.company_id:
                purchase_invoice_id = None

        job_work_id = int(fields["job_work_id"]) if fields.get("job_work_id") else None
        if job_work_id is not None and self.job_work_repo is not None:
            # Only trust a job work from this same company - same reasoning as quotation_id above.
            job_work = self.job_work_repo.get_by_id(job_work_id)
            if not job_work or job_work.company_id != current_user.company_id:
                job_work_id = None

        return PackingList(
            id=None, company_id=current_user.company_id, packing_list_number="",
            packing_list_date=packing_list_date, consignee_name="",
            created_by=current_user.id, proforma_invoice_id=proforma_invoice_id,
            quotation_id=quotation_id, purchase_order_id=purchase_order_id,
            purchase_invoice_id=purchase_invoice_id, job_work_id=job_work_id,
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
                 purchase_order_repo: PurchaseOrderRepository,
                 job_work_repo: "JobWorkRepository"):
        self.proforma_invoice_repo = proforma_invoice_repo
        self.packing_list_repo = packing_list_repo
        self.purchase_order_repo = purchase_order_repo
        self.job_work_repo = job_work_repo

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

        # A design sent out for job work instead of a straight purchase is
        # just as "handled" as one placed on a PO - it shouldn't keep
        # showing up as still to be ordered. A job work line never carries a
        # catalog design_id (matched by name only, see JobWorkItem's own
        # docstring), so this is keyed by (product, normalized design name)
        # rather than _design_key's id-preferring match.
        job_placed_index = {}
        for row in self.job_work_repo.source_design_totals_for_proforma(company_id, ids):
            key = (row["pi_id"], _product_key(row), _normalize_name(row["design_name"]))
            job_placed_index[key] = row

        result = {pi_id: {"designs": [], "pending": [], "over_ordered": [],
                          "placed_count": 0, "is_fully_placed": True}
                  for pi_id in ids}
        for row in required_rows:
            pi_id = row["pi_id"]
            placed = placed_index.get((pi_id, _design_key(row)))
            job_placed = job_placed_index.get(
                (pi_id, _product_key(row), _normalize_name(row["design_name"]))
            )
            required_boxes = row["boxes"] or 0
            required_quantity = row["quantity"] or 0
            placed_boxes = ((placed["boxes"] if placed else 0) or 0) + ((job_placed["boxes"] if job_placed else 0) or 0)
            placed_quantity = ((placed["quantity"] if placed else 0) or 0) + ((job_placed["quantity"] if job_placed else 0) or 0)

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
# PACKING PLANNING SERVICE
# ============================================================
class PackingPlanningService:
    """The document that works out how what has actually been PRODUCED
    breaks into whole numbered pallets and cartons - the step before a
    loading plan decides which container they go in.

    Two things here are worth stating outright:

    * loading is two explicit steps, not one hop. `purchase_orders_for_proformas`
      lists the purchase orders the ticked PIs pulled in - not their goods yet -
      so a run that doesn't want every one of them (a supplier not ready, a PO
      already packed elsewhere) can narrow the set before
      `build_prefill_from_purchase_orders` commits to loading anything. Goods
      come in per BATCH, not per design, one level past where
      LoadingPlanningService stops: a batch number and a manufacturing date
      exist nowhere else in the app, and they have to ride on the line that
      gets packed because a pallet is packed out of one batch. ATLANTA LIGHT
      GREY is one design and two lines here - 200 boxes under batch 102 on the
      27th, 117 under 103 on the 28th - and packing them as one 317 would put
      two firings on one pallet.

    * every packing check is a WARNING, never a ValidationError, for the same
      reason LoadingPlanningService gives about its own: batches are keyed in
      as the supplier reports them and the leftovers are grouped days later,
      so a half-planned document must save."""

    def __init__(self, packing_planning_repo: PackingPlanningRepository,
                 proforma_invoice_repo: ProformaInvoiceRepository,
                 purchase_order_repo: PurchaseOrderRepository,
                 production_repo: PurchaseOrderProductionRepository,
                 product_repo: ProductRepository,
                 pallet_type_repo: ProductPalletTypeRepository):
        self.packing_planning_repo = packing_planning_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.purchase_order_repo = purchase_order_repo
        self.production_repo = production_repo
        self.product_repo = product_repo
        self.pallet_type_repo = pallet_type_repo

    # ---- reads --------------------------------------------------
    def get(self, packing_planning_id: int, company_id: int) -> PackingPlanning:
        plan = self.packing_planning_repo.get_by_id(packing_planning_id)
        if not plan or plan.company_id != company_id:
            # 404, not 403 - don't reveal that another company's plan exists.
            raise NotFoundError(f"Packing planning #{packing_planning_id} not found.")
        return plan

    def list_all(self, company_id: int) -> List[PackingPlanning]:
        return self.packing_planning_repo.list_all(company_id)

    def next_number(self, company_id: int, planning_date: str) -> str:
        return self.packing_planning_repo.next_number(company_id, planning_date)

    # ---- loading batches from the selected proforma invoices ------------
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

    def _items_for_purchase_order(self, po: PurchaseOrder) -> List[PackingPlanningItem]:
        """One line per produced BATCH on this order, `sr_no` left at 0 for
        the caller to number once every order's lines are merged.

        A purchase order line is a product; its production rows are the
        designs of that product the supplier is making (settled on the
        linked PI's packing list); and under each design are the batches it
        was fired in. Only the last of those carries a batch number and a
        manufacturing date, which is why this document loads at that depth
        and not the design level a loading plan is happy with.

        Batches with no quantity are skipped - the production form always
        carries a spare blank row, and one that was only ever used to type a
        remark has nothing to pack."""
        items: List[PackingPlanningItem] = []
        production = self.production_repo.map_for_purchase_order(po.id)
        for item in po.items:
            for record in production.values():
                if record.purchase_order_item_id != item.id:
                    continue
                for batch in record.batches:
                    if (batch.quantity_boxes or 0) <= 0:
                        continue
                    items.append(PackingPlanningItem(
                        id=None, packing_planning_id=None, sr_no=0,
                        product_name=item.product_name,
                        proforma_invoice_id=po.proforma_invoice_id, purchase_order_id=po.id,
                        po_number=po.po_number, purchase_order_item_id=item.id,
                        product_id=item.product_id,
                        design_id=record.design_id, design_name=record.design_name,
                        batch_number=batch.batch_number,
                        production_date=batch.production_date,
                        ready_quantity=batch.quantity_boxes or 0,
                        quantity_unit=item.quantity_unit or "BOX",
                    ))
        return items

    def purchase_orders_for_proformas(self, proforma_ids: list, company_id: int) -> List[dict]:
        """Step 1 of loading: every purchase order the ticked PIs pulled in,
        each with a summary of what it would add - not yet the batch lines
        themselves. Lets the operator narrow to just the orders this packing
        run actually wants (a supplier not ready yet, a PO already packed in
        an earlier plan) before step 2 commits to loading anything.

        A PO's `proforma_invoice_id` is a single FK, so one order can never
        surface twice even when several selected PIs are checked."""
        proformas = self._load_proformas(proforma_ids, company_id)

        rows = []
        for pi in proformas:
            for po_header in self.purchase_order_repo.list_for_proforma(pi.id):
                if po_header.company_id != company_id:
                    continue
                # list_for_proforma returns header rows only, so the order
                # has to be re-fetched to see its lines and match a batch's
                # unit - the same reason build_prefill_from_purchase_orders
                # re-fetches its own.
                po = self.purchase_order_repo.get_by_id(po_header.id) or po_header
                production = self.production_repo.map_for_purchase_order(po.id)
                batch_count = 0
                ready_totals: dict = {}
                for record in production.values():
                    unit = None
                    for item in po.items:
                        if item.id == record.purchase_order_item_id:
                            unit = item.quantity_unit or "BOX"
                            break
                    for batch in record.batches:
                        qty = batch.quantity_boxes or 0
                        if qty <= 0:
                            continue
                        batch_count += 1
                        ready_totals[unit or "BOX"] = round(ready_totals.get(unit or "BOX", 0) + qty, 3)
                rows.append({
                    "id": po.id, "po_number": po.po_number, "seller_name": po.seller_name,
                    "proforma_invoice_id": pi.id, "proforma_invoice_number": po.proforma_invoice_number,
                    "batch_count": batch_count, "ready_totals": ready_totals,
                })
        return rows

    def build_prefill_from_purchase_orders(self, purchase_order_ids: list, company_id: int) -> dict:
        """Step 2 of loading: the batch lines of exactly the purchase orders
        ticked in step 1's list, merged and renumbered as one document."""
        pos = []
        for poid in dict.fromkeys(purchase_order_ids or []):
            try:
                po = self.purchase_order_repo.get_by_id(int(poid))
            except (TypeError, ValueError):
                continue
            # A crafted id can never pull another company's order in - same
            # guard _load_proformas uses for PIs.
            if po and po.company_id == company_id:
                pos.append(po)

        items: List[PackingPlanningItem] = []
        for po in pos:
            items.extend(self._items_for_purchase_order(po))
        for i, item in enumerate(items, start=1):
            item.sr_no = i

        self.auto_fill(company_id, items)
        return {
            "items": [dataclasses.asdict(i) for i in items],
            "packing_types": self.packing_types_for_items(company_id, items),
        }

    def packing_types_for_items(self, company_id: int, items: List[PackingPlanningItem]) -> dict:
        """What each row's packing-type dropdown offers, scoped to the
        products these batches actually mention. Split by unit_kind exactly
        as Loading Planning's own pickers are: a CTN of 30 pieces and a
        pallet of 32 boxes are different objects, and the sheet prints which
        one a row is packing into."""
        product_ids = [i.product_id for i in items if i.product_id]
        out: dict = {}
        for pt in self.pallet_type_repo.list_for_products(company_id, product_ids):
            bucket = out.setdefault(str(pt.product_id), {"carton": [], "pallet": []})
            bucket["carton" if pt.is_carton else "pallet"].append({
                "id": pt.id, "name": pt.name,
                "boxes_per_pallet": pt.boxes_per_pallet, "weight_kg": pt.weight_kg,
                "label": "CTN" if pt.is_carton else "PLT",
            })
        return out

    # ---- auto-fill --------------------------------------------------
    def auto_fill(self, company_id: int, items: List[PackingPlanningItem]) -> List[PackingPlanningItem]:
        """Fill in the four columns nobody should have to work out by hand:
        the packing type, its capacity, how many WHOLE units the ready
        quantity makes, and - through the model - what those units hold.

        A product packed in cartons uses its carton type, otherwise its
        pallet type: the same rule LoadingPlanningService.auto_build_packing
        picks by, so both documents agree that tiles go 32 to a pallet and
        hardware 30 to a carton. Rows already carrying a type are left
        alone, so re-running this can't undo a hand-picked one.

        Only WHOLE units are taken. 317 at 32 is nine, not 9.91 - the 29
        that are left are the whole point of the second table, and no rule
        can decide who they share a pallet with."""
        types = self.packing_types_for_items(company_id, items)
        for item in items:
            if not item.packing_type_id and not item.boxes_per_unit:
                bucket = types.get(str(item.product_id)) or {}
                chosen = (bucket.get("carton") or bucket.get("pallet") or [None])[0]
                if chosen:
                    item.packing_type_id = chosen["id"]
                    item.packing_type_name = chosen["name"]
                    item.packing_unit_label = chosen["label"]
                    item.boxes_per_unit = chosen["boxes_per_pallet"]
            if item.boxes_per_unit:
                item.actual_packing = int((item.ready_quantity or 0) // item.boxes_per_unit)
        return items

    # ---- validation --------------------------------------------------
    def _build(self, current_user: User, fields: dict,
               existing: Optional[PackingPlanning] = None) -> PackingPlanning:
        planning_date = (fields.get("packing_planning_date") or "").strip()
        if not planning_date:
            raise ValidationError("Packing planning date is required.")
        number = existing.packing_planning_number if existing else (
            (fields.get("packing_planning_number") or "").strip()
            or self.next_number(current_user.company_id, planning_date)
        )
        return PackingPlanning(
            id=existing.id if existing else None,
            company_id=current_user.company_id,
            created_by=existing.created_by if existing else current_user.id,
            packing_planning_number=number,
            packing_planning_date=planning_date,
            remarks=(fields.get("remarks") or "").strip() or None,
        )

    @staticmethod
    def _to_float(raw, label: str) -> Optional[float]:
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            raise ValidationError(f"{label} must be a number.")

    @staticmethod
    def _optional_int(raw) -> Optional[int]:
        text = (str(raw) if raw is not None else "").strip()
        if not text or text.lower() in ("none", "null"):
            return None
        try:
            return int(float(text))
        except ValueError:
            return None

    @staticmethod
    def _clean_proforma_ids(raw) -> List[int]:
        out = []
        for value in raw or []:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                continue
        return list(dict.fromkeys(out))

    def _clean_items(self, raw) -> List[PackingPlanningItem]:
        items = []
        for i, r in enumerate(raw or [], start=1):
            name = (r.get("product_name") or "").strip()
            if not name:
                continue
            actual = self._optional_int(r.get("actual_packing")) or 0
            items.append(PackingPlanningItem(
                id=None, packing_planning_id=None, sr_no=i, product_name=name,
                proforma_invoice_id=self._optional_int(r.get("proforma_invoice_id")),
                purchase_order_id=self._optional_int(r.get("purchase_order_id")),
                po_number=(r.get("po_number") or "").strip() or None,
                purchase_order_item_id=self._optional_int(r.get("purchase_order_item_id")),
                product_id=self._optional_int(r.get("product_id")),
                design_id=self._optional_int(r.get("design_id")),
                design_name=(r.get("design_name") or "").strip() or None,
                batch_number=(r.get("batch_number") or "").strip() or None,
                production_date=(r.get("production_date") or "").strip() or None,
                ready_quantity=self._to_float(r.get("ready_quantity"), "Production ready qty") or 0,
                quantity_unit=(r.get("quantity_unit") or "BOX").strip() or "BOX",
                packing_type_id=self._optional_int(r.get("packing_type_id")),
                packing_type_name=(r.get("packing_type_name") or "").strip() or None,
                packing_unit_label=(r.get("packing_unit_label") or "PLT").strip() or "PLT",
                boxes_per_unit=self._to_float(r.get("boxes_per_unit"), "Boxes per unit"),
                # Negative actual packing is a typo, not a decision to make.
                actual_packing=max(actual, 0),
                packing_no_start=self._optional_int(r.get("packing_no_start")),
            ))
        return items

    def _clean_manual_units(self, raw) -> List[PackingPlanningManualUnit]:
        units = []
        for r in raw or []:
            no = self._optional_int(r.get("unit_no"))
            if not no:
                continue
            units.append(PackingPlanningManualUnit(
                id=None, packing_planning_id=None, unit_no=no,
                packing_type_id=self._optional_int(r.get("packing_type_id")),
                packing_type_name=(r.get("packing_type_name") or "").strip() or None,
                packing_unit_label=(r.get("packing_unit_label") or "PLT").strip() or "PLT",
                capacity_boxes=self._to_float(r.get("capacity_boxes"), "Manual unit: capacity"),
                remarks=(r.get("remarks") or "").strip() or None,
                contents=self._clean_contents(r.get("contents")),
            ))
        return units

    def _clean_contents(self, raw) -> List[dict]:
        rows = []
        for r in raw or []:
            sr = self._optional_int(r.get("item_sr_no"))
            qty = self._to_float(r.get("quantity_boxes"), "Packed quantity") or 0
            if sr and qty > 0:
                rows.append({"item_sr_no": sr, "quantity_boxes": qty})
        return rows

    def packing_warnings(self, plan: PackingPlanning) -> List[str]:
        """Everything that doesn't add up, phrased for the operator - and
        returned rather than raised, because none of it stops a save.

        Batches are keyed in as the supplier reports them and the leftovers
        get grouped days later, so refusing an incomplete document would
        just mean losing the work. Same call LoadingPlanningService makes."""
        warnings = []
        for item in plan.items:
            if item.over_packed:
                warnings.append(
                    f"{item.label}: packing {abs(item.remain_quantity):g} "
                    f"{item.quantity_unit} MORE than was produced."
                )
            elif item.ready_quantity and not item.boxes_per_unit:
                warnings.append(
                    f"{item.label}: no packing type, so nothing can be worked out for it."
                )
        for row in plan.remain_rows:
            left = row["left"]
            if left > 0.001:
                warnings.append(
                    f"{row['product_name']}"
                    f"{' - ' + row['design_name'] if row['design_name'] else ''}"
                    f"{' [' + row['batch_number'] + ']' if row['batch_number'] else ''}: "
                    f"{left:g} {row['quantity_unit']} still to be packed by hand."
                )
            elif left < -0.001:
                warnings.append(
                    f"{row['product_name']}: {abs(left):g} {row['quantity_unit']} MORE "
                    f"packed by hand than is left over."
                )
        for unit in plan.manual_units:
            if unit.over_capacity:
                warnings.append(
                    f"{unit.packing_unit_label} {unit.unit_no}: holds {unit.packed_boxes:g} "
                    f"against a capacity of {unit.capacity_boxes:g}."
                )
        dupes = plan.duplicate_packing_numbers
        if dupes:
            warnings.append(
                "Packing number(s) used more than once: "
                + ", ".join(str(n) for n in dupes[:10])
                + ("..." if len(dupes) > 10 else "")
            )
        return warnings

    # ---- writes --------------------------------------------------
    def _assemble(self, plan: PackingPlanning, proforma_ids, items, manual_units) -> PackingPlanning:
        plan.proforma_invoice_ids = self._clean_proforma_ids(proforma_ids)
        plan.items = self._clean_items(items)
        plan.manual_units = self._clean_manual_units(manual_units)
        return plan

    def create(self, current_user: User, fields: dict, proforma_ids: list,
               items: list, manual_units: list) -> PackingPlanning:
        plan = self._build(current_user, fields)
        self._assemble(plan, proforma_ids, items, manual_units)
        return self.packing_planning_repo.create(plan)

    def update(self, packing_planning_id: int, current_user: User, fields: dict,
               proforma_ids: list, items: list, manual_units: list) -> PackingPlanning:
        existing = self.get(packing_planning_id, current_user.company_id)
        plan = self._build(current_user, fields, existing=existing)
        self._assemble(plan, proforma_ids, items, manual_units)
        self.packing_planning_repo.update(packing_planning_id, plan)
        return self.get(packing_planning_id, current_user.company_id)

    def delete(self, packing_planning_id: int, current_user: User) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can delete a packing planning.")
        self.get(packing_planning_id, current_user.company_id)  # 404s if missing/another company's
        self.packing_planning_repo.delete(packing_planning_id)


# ============================================================
# LOADING PLANNING SERVICE
# ============================================================
class LoadingPlanningService:
    """The document that works out which goods physically go in which
    container, before the export invoice is cut.

    Two things here are deliberately unlike their Export Invoice cousins:

    * `build_prefill_from_proformas` traces PI -> purchase orders -> THOSE
      ORDERS' PACKING LISTS, where ExportInvoiceService's method of the same
      name traces PI -> purchase orders -> purchase invoices and merges to
      product level. Both are right for their own document: an invoice bills
      a product, but a container is loaded design by design. A PO orders 1268
      boxes of one product; only its packing list knows those are four
      designs of 317.

    * every packing check is a WARNING, never a ValidationError. A loading
      plan is worked on across sittings - goods loaded today, pallets built
      tomorrow, containers assigned when the booking firms up - so a
      half-built plan must save. ExportPackingListService.build_items takes
      the opposite line about its own container split, because an invoice
      that doesn't add up cannot be issued at all."""

    def __init__(self, loading_planning_repo: LoadingPlanningRepository,
                 proforma_invoice_repo: ProformaInvoiceRepository,
                 purchase_order_repo: PurchaseOrderRepository,
                 packing_list_repo: PackingListRepository,
                 booking_detail_repo: BookingDetailRepository,
                 product_repo: ProductRepository,
                 pallet_type_repo: ProductPalletTypeRepository):
        self.loading_planning_repo = loading_planning_repo
        self.proforma_invoice_repo = proforma_invoice_repo
        self.purchase_order_repo = purchase_order_repo
        self.packing_list_repo = packing_list_repo
        self.booking_detail_repo = booking_detail_repo
        self.product_repo = product_repo
        self.pallet_type_repo = pallet_type_repo

    # ---- reads --------------------------------------------------
    def get(self, loading_planning_id: int, company_id: int) -> LoadingPlanning:
        plan = self.loading_planning_repo.get_by_id(loading_planning_id)
        if not plan or plan.company_id != company_id:
            # 404, not 403 - don't reveal that another company's plan exists.
            raise NotFoundError(f"Loading planning #{loading_planning_id} not found.")
        return plan

    def list_all(self, company_id: int) -> List[LoadingPlanning]:
        return self.loading_planning_repo.list_all(company_id)

    def next_number(self, company_id: int, planning_date: str) -> str:
        return self.loading_planning_repo.next_number(company_id, planning_date)

    # ---- loading goods from the selected proforma invoices ------------
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
        """Trace each selected PI through its purchase orders and pull in the
        goods lines actually bought on those orders, priced at the PI's own
        quoted USD rate.

        The design split comes from each PO's own packing list, which is the
        only place it exists: a purchase order line says "1268 boxes of
        GVT/PGVT 600X1200", and its packing list is what says those 1268 are
        ARKOSE/ATLANTA/ARTISTIC/BELLY at 317 each. A PO with no packing list
        yet falls back to its own product lines, which come through with no
        design - still loadable, just coarser.

        The USD rate is matched by product_id against the PI's own quoted
        lines (`pi.items`, the typed FOB rate - not `printed_items`, whose
        CIF view would fold the PI's charges into a per-unit figure this
        document has no use for). No match leaves the rate at 0 to be typed."""
        proformas = self._load_proformas(proforma_ids, company_id)

        items: List[LoadingPlanningItem] = []
        sr = 0
        for pi in proformas:
            rate_by_product = {}
            for it in pi.items:
                if it.product_id is not None and it.product_id not in rate_by_product:
                    rate_by_product[it.product_id] = it.price_usd or 0

            for po in self.purchase_order_repo.list_for_proforma(pi.id):
                if po.company_id != company_id:
                    continue
                packing_lists = [pl for pl in self.packing_list_repo.list_for_purchase_order(po.id)
                                 if pl.company_id == company_id]
                lines = []
                for pl in packing_lists:
                    for pli in pl.items:
                        lines.append({
                            "product_id": pli.product_id, "product_name": pli.product_name,
                            "design_id": pli.design_id, "design_name": pli.design_name,
                            "hsn_code": pli.hsn_code, "quantity_boxes": pli.quantity_boxes or 0,
                            "quantity_unit": pli.quantity_unit or "PCS",
                            "quantity_value": pli.quantity_value or 0, "unit": pli.unit or "SQM",
                        })
                if not lines:
                    # No packing list for this PO - take what it ordered, at
                    # product level, with no design to split by. list_for_proforma
                    # returns header rows only, so the PO has to be re-fetched
                    # to see its lines (same reason ExportInvoiceService
                    # re-fetches each purchase invoice by id).
                    full_po = self.purchase_order_repo.get_by_id(po.id) or po
                    for poi in full_po.items:
                        lines.append({
                            "product_id": poi.product_id, "product_name": poi.product_name,
                            "design_id": poi.design_id, "design_name": poi.design_name,
                            "hsn_code": poi.hsn_code, "quantity_boxes": poi.quantity_boxes or 0,
                            "quantity_unit": poi.quantity_unit or "PCS",
                            "quantity_value": poi.quantity_value or 0, "unit": poi.unit or "SQM",
                        })

                for line in lines:
                    sr += 1
                    price = rate_by_product.get(line["product_id"], 0) or 0
                    boxes = line["quantity_boxes"] or 0
                    product = self.product_repo.get_by_id(line["product_id"]) if line["product_id"] else None
                    items.append(LoadingPlanningItem(
                        id=None, loading_planning_id=None, sr_no=sr,
                        proforma_invoice_id=pi.id, purchase_order_id=po.id, po_number=po.po_number,
                        product_id=line["product_id"], product_name=line["product_name"],
                        design_id=line["design_id"], design_name=line["design_name"],
                        hsn_code=line["hsn_code"], quantity_boxes=boxes,
                        quantity_unit=line["quantity_unit"], quantity_value=line["quantity_value"],
                        unit=line["unit"],
                        # Per box/pc, not the line total - a line gets split
                        # across cartons and pallets in quantities nobody
                        # knows yet.
                        net_weight_kg=(product.net_weight_kg if product else None),
                        price_usd=price, total_usd=round(price * boxes, 2),
                    ))

        return {
            "items": [dataclasses.asdict(i) for i in items],
            "packing_types": self.packing_types_for_items(company_id, items),
        }

    def packing_types_for_items(self, company_id: int, items: List[LoadingPlanningItem]) -> dict:
        """What the carton and pallet pickers offer, scoped to the products
        these goods lines actually mention. Split by unit_kind, because the
        two levels are picked separately: a CTN is an inner box that goes ON
        a pallet, a JUNGLE KHATLI is the pallet itself."""
        product_ids = [i.product_id for i in items if i.product_id]
        out: dict = {}
        for pt in self.pallet_type_repo.list_for_products(company_id, product_ids):
            bucket = out.setdefault(str(pt.product_id), {"carton": [], "pallet": []})
            bucket["carton" if pt.is_carton else "pallet"].append({
                "id": pt.id, "name": pt.name,
                "boxes_per_pallet": pt.boxes_per_pallet, "weight_kg": pt.weight_kg,
            })
        return out

    # ---- auto-build --------------------------------------------------
    def auto_build_packing(self, company_id: int, items: List[LoadingPlanningItem]) -> dict:
        """Do the boring 90% of the packing, and leave the judgement calls.

        Per goods line: fill whole units up to the packing type's capacity,
        then emit ONE part-filled unit for the remainder. Part units are
        flagged, never merged - merging is exactly the decision a person has
        to make. 317 boxes at 32/pallet becomes nine full pallets plus one
        holding 29; 45 PCS at 30/CTN becomes one full carton plus one holding
        15, and whether that 15 shares a carton with another product's 15 is
        not something a rule can know.

        When a product has a carton type, its goods go into cartons and those
        cartons onto one pallet per product; otherwise boxes sit directly on
        pallets. There is deliberately no cartons-per-pallet capacity."""
        types = self.packing_types_for_items(company_id, items)
        cartons: List[LoadingPlanningCarton] = []
        pallets: List[LoadingPlanningPallet] = []
        carton_no = pallet_no = 0

        for item in items:
            bucket = types.get(str(item.product_id)) or {}
            carton_type = (bucket.get("carton") or [None])[0]
            pallet_type = (bucket.get("pallet") or [None])[0]
            remaining = item.quantity_boxes or 0
            if remaining <= 0:
                continue

            if carton_type:
                capacity = carton_type["boxes_per_pallet"] or remaining
                pallet_no += 1
                pallets.append(LoadingPlanningPallet(
                    id=None, loading_planning_id=None, pallet_no=pallet_no,
                    pallet_type_id=(pallet_type or {}).get("id"),
                    pallet_type_name=(pallet_type or {}).get("name") or "Pallet",
                    capacity_boxes=None,  # cartons-per-pallet is the operator's call
                    tare_weight_kg=(pallet_type or {}).get("weight_kg"),
                ))
                while remaining > 0:
                    take = min(capacity, remaining)
                    carton_no += 1
                    cartons.append(LoadingPlanningCarton(
                        id=None, loading_planning_id=None, carton_no=carton_no,
                        carton_type_id=carton_type["id"], carton_type_name=carton_type["name"],
                        capacity_boxes=capacity, tare_weight_kg=carton_type["weight_kg"],
                        pallet_no=pallet_no,
                        contents=[{"item_sr_no": item.sr_no, "quantity_boxes": take}],
                    ))
                    remaining = round(remaining - take, 3)
            else:
                capacity = (pallet_type or {}).get("boxes_per_pallet") or remaining
                while remaining > 0:
                    take = min(capacity, remaining)
                    pallet_no += 1
                    pallets.append(LoadingPlanningPallet(
                        id=None, loading_planning_id=None, pallet_no=pallet_no,
                        pallet_type_id=(pallet_type or {}).get("id"),
                        pallet_type_name=(pallet_type or {}).get("name") or "Pallet",
                        capacity_boxes=capacity, tare_weight_kg=(pallet_type or {}).get("weight_kg"),
                        contents=[{"item_sr_no": item.sr_no, "quantity_boxes": take}],
                    ))
                    remaining = round(remaining - take, 3)

        return {
            "cartons": [self._carton_json(c) for c in cartons],
            "pallets": [self._pallet_json(p) for p in pallets],
        }

    @staticmethod
    def _carton_json(carton: LoadingPlanningCarton) -> dict:
        return {
            "carton_no": carton.carton_no, "carton_type_id": carton.carton_type_id,
            "carton_type_name": carton.carton_type_name, "capacity_boxes": carton.capacity_boxes,
            "tare_weight_kg": carton.tare_weight_kg, "pallet_no": carton.pallet_no,
            "contents": carton.contents,
        }

    @staticmethod
    def _pallet_json(pallet: LoadingPlanningPallet) -> dict:
        return {
            "pallet_no": pallet.pallet_no, "pallet_type_id": pallet.pallet_type_id,
            "pallet_type_name": pallet.pallet_type_name, "capacity_boxes": pallet.capacity_boxes,
            "tare_weight_kg": pallet.tare_weight_kg, "container_sr_no": pallet.container_sr_no,
            "contents": pallet.contents,
        }

    # ---- booking --------------------------------------------------
    def booking_snapshot(self, booking_detail_id: int, company_id: int) -> dict:
        """The containers a plan (or an export invoice) copies off a booking.

        A copy, not a live link: the 11B rows are a snapshot from the moment
        the booking is picked, so editing the booking afterwards can't
        rewrite a finished plan. The transporter is booking-level and gets
        stamped onto every row, since that is how Booking Detail records it."""
        booking = self.booking_detail_repo.get_by_id(booking_detail_id)
        if not booking or booking.company_id != company_id:
            raise NotFoundError(f"Booking detail #{booking_detail_id} not found.")
        return {
            "booking_no": booking.booking_no,
            "vessel_name": booking.vessel_name,
            "voyage_no": booking.voyage_no,
            "transporter_name": booking.transporter_name,
            "container_summary": [dict(c) for c in booking.containers],
            "containers": [{
                "container_type": cd.get("container_type"),
                "container_no": cd.get("container_no"),
                "line_seal_no": cd.get("line_seal_no"),
                "rfid_seal_no": cd.get("rfid_seal_no"),
                "vehicle_no": cd.get("vehicle_no"),
                "lr_no": cd.get("lr_no"),
                "transporter_name": booking.transporter_name,
                "max_permitted_weight": cd.get("max_permitted_weight"),
                "tare_weight_kg": cd.get("tare_weight_kg"),
            } for cd in booking.container_details],
        }

    # ---- validation --------------------------------------------------
    def _build(self, current_user: User, fields: dict, existing: Optional[LoadingPlanning] = None) -> LoadingPlanning:
        planning_date = (fields.get("loading_planning_date") or "").strip()
        if not planning_date:
            raise ValidationError("Loading planning date is required.")

        booking_detail_id = None
        raw_booking = (fields.get("booking_detail_id") or "").strip()
        if raw_booking:
            try:
                booking_detail_id = int(raw_booking)
            except (TypeError, ValueError):
                raise ValidationError("Pick a booking from the list.")
            booking = self.booking_detail_repo.get_by_id(booking_detail_id)
            if not booking or booking.company_id != current_user.company_id:
                raise ValidationError("Pick a booking from the list.")

        number = existing.loading_planning_number if existing else (
            (fields.get("loading_planning_number") or "").strip()
            or self.next_number(current_user.company_id, planning_date)
        )
        return LoadingPlanning(
            id=existing.id if existing else None,
            company_id=current_user.company_id,
            created_by=existing.created_by if existing else current_user.id,
            loading_planning_number=number,
            loading_planning_date=planning_date,
            booking_detail_id=booking_detail_id,
            booking_no=(fields.get("booking_no") or "").strip() or None,
            vessel_name=(fields.get("vessel_name") or "").strip() or None,
            voyage_no=(fields.get("voyage_no") or "").strip() or None,
            transporter_name=(fields.get("transporter_name") or "").strip() or None,
            remarks=(fields.get("remarks") or "").strip() or None,
        )

    @staticmethod
    def _to_float(raw, label: str) -> Optional[float]:
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            raise ValidationError(f"{label} must be a number.")

    @staticmethod
    def _clean_proforma_ids(raw) -> List[int]:
        out = []
        for value in raw or []:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                continue
        return list(dict.fromkeys(out))

    def _clean_items(self, raw) -> List[LoadingPlanningItem]:
        items = []
        for i, r in enumerate(raw or [], start=1):
            name = (r.get("product_name") or "").strip()
            if not name:
                continue
            boxes = self._to_float(r.get("quantity_boxes"), "Goods: quantity") or 0
            price = self._to_float(r.get("price_usd"), "Goods: price") or 0
            items.append(LoadingPlanningItem(
                id=None, loading_planning_id=None, sr_no=i, product_name=name,
                proforma_invoice_id=self._optional_int(r.get("proforma_invoice_id")),
                purchase_order_id=self._optional_int(r.get("purchase_order_id")),
                po_number=(r.get("po_number") or "").strip() or None,
                product_id=self._optional_int(r.get("product_id")),
                design_id=self._optional_int(r.get("design_id")),
                design_name=(r.get("design_name") or "").strip() or None,
                hsn_code=(r.get("hsn_code") or "").strip() or None,
                quantity_boxes=boxes,
                quantity_unit=(r.get("quantity_unit") or "PCS").strip() or "PCS",
                quantity_value=self._to_float(r.get("quantity_value"), "Goods: alt quantity") or 0,
                unit=(r.get("unit") or "SQM").strip() or "SQM",
                net_weight_kg=self._to_float(r.get("net_weight_kg"), "Goods: net weight"),
                price_usd=price, total_usd=round(price * boxes, 2),
            ))
        return items

    @staticmethod
    def _optional_int(raw) -> Optional[int]:
        text = (str(raw) if raw is not None else "").strip()
        if not text or text.lower() in ("none", "null"):
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _clean_containers(self, raw) -> List[dict]:
        rows = []
        for r in raw or []:
            values = {k: (r.get(k) or "").strip() or None
                      for k in ("container_type", "container_no", "line_seal_no", "rfid_seal_no",
                                "vehicle_no", "lr_no", "transporter_name", "max_permitted_weight")}
            values["tare_weight_kg"] = self._to_float(r.get("tare_weight_kg"), "Container details: tare weight")
            if any(v is not None for v in values.values()):
                rows.append(values)
        return rows

    def _clean_cartons(self, raw) -> List[LoadingPlanningCarton]:
        cartons = []
        for r in raw or []:
            no = self._optional_int(r.get("carton_no"))
            if not no:
                continue
            cartons.append(LoadingPlanningCarton(
                id=None, loading_planning_id=None, carton_no=no,
                carton_type_id=self._optional_int(r.get("carton_type_id")),
                carton_type_name=(r.get("carton_type_name") or "").strip() or None,
                capacity_boxes=self._to_float(r.get("capacity_boxes"), "Carton: capacity"),
                tare_weight_kg=self._to_float(r.get("tare_weight_kg"), "Carton: tare weight"),
                pallet_no=self._optional_int(r.get("pallet_no")),
                contents=self._clean_contents(r.get("contents")),
            ))
        return cartons

    def _clean_pallets(self, raw) -> List[LoadingPlanningPallet]:
        pallets = []
        for r in raw or []:
            no = self._optional_int(r.get("pallet_no"))
            if not no:
                continue
            pallets.append(LoadingPlanningPallet(
                id=None, loading_planning_id=None, pallet_no=no,
                pallet_type_id=self._optional_int(r.get("pallet_type_id")),
                pallet_type_name=(r.get("pallet_type_name") or "").strip() or None,
                capacity_boxes=self._to_float(r.get("capacity_boxes"), "Pallet: capacity"),
                tare_weight_kg=self._to_float(r.get("tare_weight_kg"), "Pallet: tare weight"),
                container_sr_no=self._optional_int(r.get("container_sr_no")),
                contents=self._clean_contents(r.get("contents")),
            ))
        return pallets

    def _clean_contents(self, raw) -> List[dict]:
        rows = []
        for r in raw or []:
            sr = self._optional_int(r.get("item_sr_no"))
            qty = self._to_float(r.get("quantity_boxes"), "Packed quantity") or 0
            if sr and qty > 0:
                rows.append({"item_sr_no": sr, "quantity_boxes": qty})
        return rows

    def packing_warnings(self, plan: LoadingPlanning) -> List[str]:
        """Everything that doesn't add up, phrased for the operator - and
        returned rather than raised, because none of it stops a save.

        A plan is legitimately built over several sittings: goods loaded
        today, pallets built tomorrow, containers assigned when the booking
        firms up. Refusing to save an incomplete one would just mean losing
        the work."""
        warnings = []
        for balance in plan.line_balances:
            left = balance["left"]
            if abs(left) < 0.001:
                continue
            if left > 0:
                warnings.append(
                    f"{balance['label']}: {left:g} {balance['quantity_unit']} still to be packed."
                )
            else:
                warnings.append(
                    f"{balance['label']}: {abs(left):g} {balance['quantity_unit']} MORE packed than planned."
                )
        for row in plan.container_summary:
            if row.get("over_weight"):
                warnings.append(
                    f"Container {row['container_no']}: VGM {row['vgm_kg']:,.0f} kg is over the "
                    f"{row['max_permitted_weight']:,.0f} kg permitted."
                )
        loose = [p for p in plan.pallets if p.container_sr_no is None]
        if loose and plan.containers:
            warnings.append(f"{len(loose)} pallet(s) not yet assigned to a container.")
        return warnings

    # ---- writes --------------------------------------------------
    def _assemble(self, plan: LoadingPlanning, proforma_ids, items, containers,
                  cartons, pallets) -> LoadingPlanning:
        plan.proforma_invoice_ids = self._clean_proforma_ids(proforma_ids)
        plan.items = self._clean_items(items)
        plan.containers = self._clean_containers(containers)
        plan.cartons = self._clean_cartons(cartons)
        plan.pallets = self._clean_pallets(pallets)
        return plan

    def create(self, current_user: User, fields: dict, proforma_ids: list, items: list,
               containers: list, cartons: list, pallets: list) -> LoadingPlanning:
        plan = self._build(current_user, fields)
        self._assemble(plan, proforma_ids, items, containers, cartons, pallets)
        return self.loading_planning_repo.create(plan)

    def update(self, loading_planning_id: int, current_user: User, fields: dict, proforma_ids: list,
               items: list, containers: list, cartons: list, pallets: list) -> LoadingPlanning:
        existing = self.get(loading_planning_id, current_user.company_id)
        plan = self._build(current_user, fields, existing=existing)
        self._assemble(plan, proforma_ids, items, containers, cartons, pallets)
        self.loading_planning_repo.update(loading_planning_id, plan)
        return self.get(loading_planning_id, current_user.company_id)

    def delete(self, loading_planning_id: int, current_user: User) -> None:
        if not current_user.is_admin:
            raise PermissionDeniedError("Only an admin can delete a loading planning.")
        self.get(loading_planning_id, current_user.company_id)  # 404s if missing/another company's
        self.loading_planning_repo.delete(loading_planning_id)


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
