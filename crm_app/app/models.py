"""
app/models.py
-------------
Plain data classes that mirror the tables in schema.sql.

These objects carry data only - no SQL, no Flask, no business rules. That
separation is what makes the Repository layer swappable and the Service
layer unit-testable without a real database.

Each class also knows how to build itself `from_row(sqlite3.Row)`. That's a
small convenience, not a violation of Single Responsibility - it's still
just "how do I represent myself", not "how do I persist myself".
"""

import json
import math
from dataclasses import dataclass, field, replace
from typing import Optional, List

from app.utils import is_fob_terms


@dataclass
class Tenant:
    """A company/business using this CRM, picked on the login screen before
    username/password. NOT the same thing as OurCompany below - a Tenant is
    the workspace/login concept, OurCompany is one specific tenant's own
    business profile (GSTIN/PAN/bank details) shown on its quotations."""
    id: Optional[int]
    name: str
    slug: str
    is_active: bool = True
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "Tenant":
        return Tenant(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )


@dataclass
class User:
    id: Optional[int]
    company_id: int
    username: str
    password_hash: str
    full_name: str
    role: str  # 'admin' | 'employee'
    is_active: bool = True
    created_at: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @staticmethod
    def from_row(row) -> "User":
        return User(
            id=row["id"],
            company_id=row["company_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            full_name=row["full_name"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )


@dataclass
class ContactPerson:
    """Used for lead_contacts and client_contacts - identical shape, so one
    class serves both (Interface Segregation without needless duplication)."""
    id: Optional[int]
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    is_primary: bool = False

    @staticmethod
    def from_row(row) -> "ContactPerson":
        return ContactPerson(
            id=row["id"],
            name=row["name"],
            phone=row["phone"],
            email=row["email"],
            is_primary=bool(row["is_primary"]),
        )


@dataclass
class Communication:
    id: Optional[int]
    parent_type: str  # 'lead' | 'buyer' | 'supplier'
    parent_id: int
    employee_id: int
    comm_date: str
    mode: str
    description: str
    follow_up_date: Optional[str] = None
    created_at: Optional[str] = None
    employee_name: Optional[str] = None  # populated by joined queries only

    @staticmethod
    def from_row(row) -> "Communication":
        return Communication(
            id=row["id"],
            parent_type=row["parent_type"],
            parent_id=row["parent_id"],
            employee_id=row["employee_id"],
            comm_date=row["comm_date"],
            mode=row["mode"],
            description=row["description"],
            follow_up_date=row["follow_up_date"],
            created_at=row["created_at"],
            employee_name=row["employee_name"] if "employee_name" in row.keys() else None,
        )


LEAD_STATUSES = [
    ("new", "New"),
    ("in_communication", "In Communication"),
    ("in_follow_up", "In Follow Up"),
    ("long_follow_up", "Long Follow Up"),
    ("quotation_submission_pending", "Quotation Submission Pending"),
    ("in_client", "In Client"),
]

CLIENT_STATUSES = [
    ("proforma_invoice_submission_pending", "Proforma Invoice Submission Pending"),
    ("purchase_order_submission_pending", "Purchase Order Submission Pending"),
    ("purchase_invoice_submission_pending", "Purchase Invoice Submission Pending"),
    ("export_invoice_submission_pending", "Export Invoice Submission Pending"),
    ("commercial_invoice_submission_pending", "Commercial Invoice Submission Pending"),
]

# Maps a document type just generated for a client -> the CLIENT_STATUSES
# stage that becomes pending once it's done (i.e. what's next). Document
# services call services.advance_client_status(...) with their key after
# create/update so client status auto-advances - adding a future document
# type (Purchase Order, Purchase Invoice, Export Invoice, Commercial
# Invoice) only requires registering it here, no other wiring. Packing List
# is deliberately absent: it doesn't correspond to any CLIENT_STATUSES stage.
CLIENT_STATUS_ADVANCE_ON = {
    "proforma_invoice": "purchase_order_submission_pending",
    "purchase_order": "purchase_invoice_submission_pending",
    "purchase_invoice": "export_invoice_submission_pending",
    "export_invoice": "commercial_invoice_submission_pending",
}

CLIENT_TYPES = ["Supplier", "Buyer"]  # the lead-conversion picker only; each type now lives in its own table

COMMUNICATION_MODES = ["WhatsApp", "WeChat", "Call", "Email", "In Person", "Other"]

# What a product's quantity is measured in. One shared list drives the
# product form, the Unit dropdowns on quotation/proforma/packing-list lines,
# and the service-side fallback - so the choices can't drift apart.
PRODUCT_UNITS = ["SQM", "LM", "PCS", "KG", "SET"]

# What a purchase order is bought under - it decides the GST rate applied to
# the whole order (see PurchaseOrderService._tax_percentages):
#   full_tax  - the ordinary rate, taken from the catalog products on the lines
#   exemption - the concessional rate for supplies meant for export (0.1% total)
PURCHASE_TYPES = {"full_tax": "Full Tax Purchase", "exemption": "Exemption"}
DEFAULT_PURCHASE_TYPE = "full_tax"

# How far along a purchase order line is on the supplier's floor. Set by
# hand on the preview page's Production Status card - never derived from the
# batch quantities recorded alongside it (see PurchaseOrderItemProduction).
PRODUCTION_STATUSES = {"pending": "Pending", "in_production": "In production", "ready": "Ready"}
DEFAULT_PRODUCTION_STATUS = "pending"
# The whole-order rate under Exemption: 0.1% inter-state, split into
# 0.05% + 0.05% when it's an intra-state purchase (same halving rule the
# catalog products follow for their own rates).
EXEMPTION_IGST_PERCENT = 0.1


@dataclass
class Lead:
    id: Optional[int]
    company_id: int
    company_name: str
    phone: str
    email: str
    facebook: Optional[str]
    instagram: Optional[str]
    other_social: Optional[str]
    status: str
    created_by: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_converted: bool = False
    converted_client_type: Optional[str] = None  # 'Buyer' | 'Supplier' - says which table converted_client_id names
    converted_client_id: Optional[int] = None
    # populated by joins / repository convenience methods, not stored columns
    created_by_name: Optional[str] = None
    contacts: List[ContactPerson] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "Lead":
        return Lead(
            id=row["id"],
            company_id=row["company_id"],
            company_name=row["company_name"],
            phone=row["phone"],
            email=row["email"],
            facebook=row["facebook"],
            instagram=row["instagram"],
            other_social=row["other_social"],
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_converted=bool(row["is_converted"]),
            converted_client_type=row["converted_client_type"] if "converted_client_type" in row.keys() else None,
            converted_client_id=row["converted_client_id"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
        )

    @property
    def status_label(self) -> str:
        return dict(LEAD_STATUSES).get(self.status, self.status)


@dataclass
class Party:
    """A Buyer record. Supplier has since diverged into its own shape (see
    Supplier below), modeled on OurCompany instead of on a lead."""
    id: Optional[int]
    company_id: int
    lead_id: Optional[int]
    company_name: str
    phone: str
    email: str
    facebook: Optional[str]
    instagram: Optional[str]
    other_social: Optional[str]
    status: str
    created_by: int
    address: Optional[str] = None
    country: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    contacts: List[ContactPerson] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "Party":
        return Party(
            id=row["id"],
            company_id=row["company_id"],
            lead_id=row["lead_id"],
            company_name=row["company_name"],
            phone=row["phone"],
            email=row["email"],
            facebook=row["facebook"],
            instagram=row["instagram"],
            other_social=row["other_social"],
            status=row["status"],
            created_by=row["created_by"],
            address=row["address"] if "address" in row.keys() else None,
            country=row["country"] if "country" in row.keys() else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def status_label(self) -> str:
        return dict(CLIENT_STATUSES).get(self.status, self.status)


@dataclass
class Supplier:
    """Also "graduates" from an approved lead, but its data mirrors
    OurCompany's own profile shape (GSTIN/PAN/IEC/bank/contacts) instead of
    a Party's lead-shaped fields - company logo, BIN and LUT are
    deliberately not carried. Document types for suppliers aren't defined
    yet; `status` is borrowed from the same CLIENT_STATUSES pipeline as
    Buyer for now and may change once that's specified."""
    id: Optional[int]
    company_id: int
    lead_id: Optional[int]
    company_name: str
    status: str
    created_by: int
    address: Optional[str] = None
    gstin: Optional[str] = None
    cin_llp_no: Optional[str] = None  # optional: CIN (company) or LLPIN (LLP) registration number
    pan_no: Optional[str] = None
    iec: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    contact_details: List[dict] = field(default_factory=list)  # [{type, value, is_primary}]
    contact_persons: List[dict] = field(default_factory=list)  # [{name, is_primary}]
    bank_details: List[dict] = field(default_factory=list)  # [{bank_name, account_number, ifsc_code, swift_code, branch, bank_address, is_primary}]

    @staticmethod
    def from_row(row) -> "Supplier":
        return Supplier(
            id=row["id"],
            company_id=row["company_id"],
            lead_id=row["lead_id"],
            company_name=row["company_name"],
            status=row["status"],
            created_by=row["created_by"],
            address=row["address"],
            gstin=row["gstin"],
            cin_llp_no=row["cin_llp_no"] if "cin_llp_no" in row.keys() else None,
            pan_no=row["pan_no"],
            iec=row["iec"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def status_label(self) -> str:
        return dict(CLIENT_STATUSES).get(self.status, self.status)


@dataclass
class Transporter:
    """The haulier a consignment moves with. Unlike Buyer/Supplier
    this one never comes from a lead - nobody prospects a transporter, we
    just keep the registration details that have to be quoted on the
    paperwork - so there's no lead_id and no status pipeline, and none of
    the payments/communications/documents satellites apply either. Contact
    persons are the same name/phone/email shape a Party uses."""
    id: Optional[int]
    company_id: int
    name: str
    created_by: int
    address: Optional[str] = None
    gstin_transporter_no: Optional[str] = None  # GSTIN / Transporter No. - one and the same cell
    pan_no: Optional[str] = None
    cin_llp_no: Optional[str] = None  # optional: CIN (company) or LLPIN (LLP) registration number
    email: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    contacts: List[ContactPerson] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "Transporter":
        return Transporter(
            id=row["id"],
            company_id=row["company_id"],
            name=row["name"],
            created_by=row["created_by"],
            address=row["address"],
            gstin_transporter_no=row["gstin_transporter_no"],
            pan_no=row["pan_no"],
            cin_llp_no=row["cin_llp_no"],
            email=row["email"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class PaymentEntry:
    id: Optional[int]
    parent_type: str  # 'buyer' | 'supplier'
    parent_id: int
    account_name: str
    payment_datetime: str
    amount_original: float
    currency_code: str
    conversion_rate: float
    amount_inr: float
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "PaymentEntry":
        return PaymentEntry(
            id=row["id"],
            parent_type=row["parent_type"],
            parent_id=row["parent_id"],
            account_name=row["account_name"],
            payment_datetime=row["payment_datetime"],
            amount_original=row["amount_original"],
            currency_code=row["currency_code"],
            conversion_rate=row["conversion_rate"],
            amount_inr=row["amount_inr"],
            created_at=row["created_at"],
        )


@dataclass
class DocumentEntry:
    """Metadata-only placeholder for now (see the hint on the buyer/
    supplier detail page) - a future update will auto-generate and
    file-store these the same way Quotation already works. When that
    happens, give the new document type its own optional `lead_id` (like
    Quotation.lead_id) instead of a parent link - a party has no document
    link of its own; QuotationRepository.list_for_lead shows the pattern: a
    converted party's documents are found via `party.lead_id`, so anything
    created against the lead (before OR after conversion) stays visible on
    the party automatically, with nothing to copy or keep in sync by hand."""
    id: Optional[int]
    parent_type: str  # 'buyer' | 'supplier'
    parent_id: int
    document_name: str
    document_type: str
    document_date: str
    notes: Optional[str] = None
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "DocumentEntry":
        return DocumentEntry(
            id=row["id"],
            parent_type=row["parent_type"],
            parent_id=row["parent_id"],
            document_name=row["document_name"],
            document_type=row["document_type"],
            document_date=row["document_date"],
            notes=row["notes"],
            created_at=row["created_at"],
        )


@dataclass
class OurCompany:
    id: int
    company_id: int
    company_name: str
    gstin: Optional[str]
    pan_no: Optional[str]
    iec: Optional[str]
    bin: Optional[str] = None
    branch_code: Optional[str] = None  # IEC branch code, printed on the Export Invoice annexure (section 2B)
    address: Optional[str] = None
    logo_path: Optional[str] = None  # relative to static/, shown in the app sidebar and on generated documents
    self_sealing_declaration: Optional[str] = None  # printed on the Export Invoice's declaration block
    government_schemes: Optional[str] = None  # default for the Export Annexure's section 13 and the Export Invoice's "Export under" text
    updated_at: Optional[str] = None
    contact_details: List[dict] = field(default_factory=list)  # [{type, value, is_primary}]
    contact_persons: List[dict] = field(default_factory=list)  # [{name, designation, is_primary}]
    bank_details: List[dict] = field(default_factory=list)  # [{bank_name, account_number, ifsc_code, branch, is_primary}]
    lut_details: List[dict] = field(default_factory=list)  # [{lut_number, financial_year, is_primary}]
    rcmc_details: List[dict] = field(default_factory=list)  # [{registration_number, registration_date, valid_until, organisation_name, organisation_address, contact_number, email_address, is_primary}]

    @staticmethod
    def from_row(row) -> "OurCompany":
        return OurCompany(
            id=row["id"],
            company_id=row["company_id"],
            company_name=row["company_name"],
            gstin=row["gstin"],
            pan_no=row["pan_no"],
            iec=row["iec"],
            bin=row["bin"] if "bin" in row.keys() else None,
            branch_code=row["branch_code"] if "branch_code" in row.keys() else None,
            address=row["address"] if "address" in row.keys() else None,
            logo_path=row["logo_path"] if "logo_path" in row.keys() else None,
            self_sealing_declaration=row["self_sealing_declaration"] if "self_sealing_declaration" in row.keys() else None,
            government_schemes=row["government_schemes"] if "government_schemes" in row.keys() else None,
            updated_at=row["updated_at"],
        )


@dataclass
class MiscCurrency:
    """One row of the CURRENCY drop list maintained under Administration ->
    Miscellaneous. Every currency dropdown in the app is filled from these."""
    id: Optional[int]
    company_id: int
    name: str        # "name of currency", e.g. USD
    symbol: str      # "currency symbol", e.g. $
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def label(self) -> str:
        """How the currency reads on a printed sheet: `USD [ $ ]`."""
        return f"{self.name} [ {self.symbol} ]"

    @staticmethod
    def from_row(row) -> "MiscCurrency":
        return MiscCurrency(
            id=row["id"], company_id=row["company_id"],
            name=row["name"], symbol=row["symbol"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


@dataclass
class MiscNatureOfContract:
    """One row of the NATURE OF CONTRACT drop list maintained under
    Administration -> Miscellaneous. The same list fills the delivery-terms
    field on every document, whatever that document calls it ("Nature of
    contract", "Shipping terms", "Terms of delivery")."""
    id: Optional[int]
    company_id: int
    name: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "MiscNatureOfContract":
        return MiscNatureOfContract(
            id=row["id"], company_id=row["company_id"], name=row["name"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


@dataclass
class MiscCountry:
    """One row of the COUNTRY drop list maintained under Administration ->
    Miscellaneous. Fills the "Country Name" field on a Buyer's profile."""
    id: Optional[int]
    company_id: int
    name: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "MiscCountry":
        return MiscCountry(
            id=row["id"], company_id=row["company_id"], name=row["name"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


@dataclass
class MiscContainerType:
    """One row of the CONTAINER TYPE drop list maintained under
    Administration -> Miscellaneous. Feeds the container-type dropdown on
    the Booking Detail form (the master-data table Export Invoice's
    Container details card is auto-filled from)."""
    id: Optional[int]
    company_id: int
    name: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "MiscContainerType":
        return MiscContainerType(
            id=row["id"], company_id=row["company_id"], name=row["name"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


@dataclass
class MiscHsnCode:
    """One row of the HSN CODE drop list maintained under Administration ->
    Miscellaneous: an HSN code and the GST slab that applies to it, kept
    together so the code and its rate can never disagree (the same reasoning
    as MiscPortOfLoading's name/pin_code pairing)."""
    id: Optional[int]
    company_id: int
    name: str        # "HSN CODE", e.g. 69072100
    gst_slab: str    # "GST SLAB", e.g. 18
    # "Related to Products" - what the code covers, in words (e.g. GLAZED
    # VITRIFIED TILES). A note for whoever reads the list; optional, and
    # deliberately not a link to catalog products - the product form is where
    # a product's own HSN code is recorded.
    related_products: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def label(self) -> str:
        """How the row reads where both halves are shown: `69072100 - 18`."""
        return f"{self.name} - {self.gst_slab}"

    @staticmethod
    def from_row(row) -> "MiscHsnCode":
        return MiscHsnCode(
            id=row["id"], company_id=row["company_id"], name=row["name"],
            gst_slab=row["gst_slab"],
            related_products=row["related_products"] if "related_products" in row.keys() else None,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


@dataclass
class MiscUnit:
    """One row of the UNIT drop list maintained under Administration ->
    Miscellaneous: a unit abbreviation and what it means in words, kept
    together so the two can never disagree (the same reasoning as
    MiscPortOfLoading's name/pin_code pairing)."""
    id: Optional[int]
    company_id: int
    name: str        # "Unit", e.g. SQM
    meaning: str      # "Meaning", e.g. Square Meter
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def label(self) -> str:
        """How the unit reads where both halves are shown: `SQM - Square Meter`."""
        return f"{self.name} - {self.meaning}"

    @staticmethod
    def from_row(row) -> "MiscUnit":
        return MiscUnit(
            id=row["id"], company_id=row["company_id"],
            name=row["name"], meaning=row["meaning"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


@dataclass
class MiscPortOfLoading:
    """One row of the PORT OF LOADING drop list maintained under
    Administration -> Miscellaneous: the port a shipment leaves from and that
    port's PIN code (the figure the GST/e-way-bill paperwork asks for)."""
    id: Optional[int]
    company_id: int
    name: str        # "Port of Loading", e.g. MUNDRA
    pin_code: str    # "Port of loading Pincode", e.g. 370421
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def label(self) -> str:
        """How the port reads where both halves are shown: `MUNDRA - 370421`."""
        return f"{self.name} - {self.pin_code}"

    @staticmethod
    def from_row(row) -> "MiscPortOfLoading":
        return MiscPortOfLoading(
            id=row["id"], company_id=row["company_id"],
            name=row["name"], pin_code=row["pin_code"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


# The currency dropdowns used to be a hard-coded list on the payment form.
# Until an admin adds a row under Administration -> Miscellaneous, that same
# list is what the dropdowns fall back to, so nothing breaks on upgrade.
DEFAULT_CURRENCIES = [
    ("USD", "$"), ("EUR", "€"), ("GBP", "£"),
    ("AED", "د.إ"), ("CNY", "¥"), ("SAR", "﷼"),
]

# Same idea for CONTAINER TYPE: this used to be a hard-coded list feeding the
# Booking Detail form's dropdown directly; now that it is an admin-managed
# Miscellaneous list, this is what that dropdown falls back to until a
# company adds its own rows, so nothing goes empty on upgrade.
DEFAULT_CONTAINER_TYPES = ["20FT FCL", "40FT FCL", "20FT LCL", "40FT LCL", "40FT HC"]


def currency_display(code: Optional[str], symbol: Optional[str],
                     default_code: str = "USD", default_symbol: str = "$") -> tuple:
    """(name, prefix, label) for a document's stored currency, e.g.
    ("USD", "$", "USD [ $ ]"). A document saved before the currency became a
    picked field has neither, and falls back to whatever its sheet used to
    hard-code - so nothing already printed changes."""
    if not code:
        return default_code, default_symbol, f"{default_code} [ {default_symbol} ]"
    prefix = symbol or ""
    return code, prefix, (f"{code} [ {prefix} ]" if prefix else code)


@dataclass
class Permit:
    """One "permission" the company holds, managed under the Our Company
    area. It records a stuffing-place name + place of stuffing, the issuing
    authority, is either valid until an expiry date OR a one-time permit
    (validity_type), and can carry an uploaded PDF."""
    id: Optional[int]
    company_id: int
    permission_number: str
    created_by: int
    stuffing_place_name: Optional[str] = None
    place_of_stuffing: Optional[str] = None
    date_of_issue: Optional[str] = None
    issuing_authority: Optional[str] = None
    issuing_authority_address: Optional[str] = None
    validity_type: str = "expiry"  # 'expiry' | 'one_time'
    date_of_expiry: Optional[str] = None  # only when validity_type == 'expiry'
    pdf_path: Optional[str] = None  # relative to static/
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def is_one_time(self) -> bool:
        return self.validity_type == "one_time"

    @staticmethod
    def from_row(row) -> "Permit":
        return Permit(
            id=row["id"],
            company_id=row["company_id"],
            permission_number=row["permission_number"],
            created_by=row["created_by"],
            stuffing_place_name=row["stuffing_place_name"],
            place_of_stuffing=row["place_of_stuffing"],
            date_of_issue=row["date_of_issue"],
            issuing_authority=row["issuing_authority"],
            issuing_authority_address=row["issuing_authority_address"],
            validity_type=row["validity_type"],
            date_of_expiry=row["date_of_expiry"],
            pdf_path=row["pdf_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class BookingDetail:
    """A standalone shipping-booking record under Master Data, with the same
    field shape as an Export Invoice's own "Container details" card - but
    not tied to any invoice, so a booking can be logged on its own for a
    buyer. containers/container_details are plain dicts, the same idiom
    ExportInvoice uses for its own two child lists of the same shape."""
    id: Optional[int]
    company_id: int
    buyer_id: int
    created_by: int
    booking_no: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_no: Optional[str] = None
    transporter_name: Optional[str] = None  # one transporter for every container below
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    buyer_name: Optional[str] = None  # populated by joined queries only
    created_by_name: Optional[str] = None  # populated by joined queries only
    container_count: Optional[int] = None  # list-view only: how many 11B rows this booking has
    containers: List[dict] = field(default_factory=list)  # [{container_type, container_count}]
    container_details: List[dict] = field(default_factory=list)  # [{container_type, container_no, max_permitted_weight, tare_weight_kg, vehicle_no, lr_no, line_seal_no, rfid_seal_no}]

    @staticmethod
    def from_row(row) -> "BookingDetail":
        keys = row.keys()
        return BookingDetail(
            id=row["id"],
            company_id=row["company_id"],
            buyer_id=row["buyer_id"],
            created_by=row["created_by"],
            booking_no=row["booking_no"],
            vessel_name=row["vessel_name"],
            voyage_no=row["voyage_no"],
            transporter_name=row["transporter_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            buyer_name=row["buyer_name"] if "buyer_name" in keys else None,
            created_by_name=row["created_by_name"] if "created_by_name" in keys else None,
        )


@dataclass
class Category:
    """A folder at the catalog root that groups products. `parent_id=None`
    means it sits at the catalog's top level; categories nest to any depth
    via self-reference, the same way sub categories (ProductFolder) nest
    inside a product. Products with category_id=NULL sit directly at the
    root, the same way a design can sit directly under a product."""
    id: Optional[int]
    company_id: int
    name: str
    parent_id: Optional[int] = None
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "Category":
        return Category(
            id=row["id"],
            company_id=row["company_id"],
            name=row["name"],
            parent_id=row["parent_id"] if "parent_id" in row.keys() else None,
            created_at=row["created_at"],
        )


@dataclass
class Product:
    """Second level of the catalog (inside a category, or at the root when
    category_id is None): the tax/HSN identity AND the physical packing spec
    (pallet types, quantity, alternate quantity, unit) that
    quotations, proforma invoices and packing lists all read from - every
    design under a product shares the same packing spec. Sub categories and
    designs live underneath it; price and photos belong to the Design.
    IGST is the only tax input - SGST and CGST are always stored as half of
    it (recalculated by ProductService on every save)."""
    id: Optional[int]
    company_id: int
    product_name: str
    category_id: Optional[int] = None
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    igst_percent: Optional[float] = None
    sgst_percent: Optional[float] = None
    cgst_percent: Optional[float] = None
    price_usd: Optional[float] = None
    quantity_unit: str = "PCS"  # what `quantity` is measured in
    quantity: Optional[str] = None  # per-box quantity (e.g. pcs per box)
    alternate_quantity_unit: str = "SQM"  # what `alternate_quantity` is measured in; prefills document lines' Unit column
    alternate_quantity: Optional[str] = None  # per-box quantity, drives the Boxes x AltQty auto-calc
    net_weight_kg: Optional[float] = None    # net weight per box (KG) - drives the packing list's Boxes x weight auto-calc
    gross_weight_kg: Optional[float] = None  # gross weight per box (KG) - same auto-calc as net_weight_kg
    is_job_work_product: bool = False  # ticked on the product form: this product is made via job work off master_product_id
    master_product_id: Optional[int] = None  # the product this one is job-worked from, when is_job_work_product is set
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "Product":
        return Product(
            id=row["id"],
            company_id=row["company_id"],
            product_name=row["product_name"],
            category_id=row["category_id"] if "category_id" in row.keys() else None,
            description=row["description"],
            hsn_code=row["hsn_code"],
            igst_percent=row["igst_percent"],
            sgst_percent=row["sgst_percent"],
            cgst_percent=row["cgst_percent"],
            price_usd=row["price_usd"] if "price_usd" in row.keys() else None,
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            quantity=row["quantity"] if "quantity" in row.keys() else None,
            alternate_quantity_unit=row["alternate_quantity_unit"] if "alternate_quantity_unit" in row.keys() else "SQM",
            alternate_quantity=row["alternate_quantity"] if "alternate_quantity" in row.keys() else None,
            net_weight_kg=row["net_weight_kg"] if "net_weight_kg" in row.keys() else None,
            gross_weight_kg=row["gross_weight_kg"] if "gross_weight_kg" in row.keys() else None,
            is_job_work_product=bool(row["is_job_work_product"]) if "is_job_work_product" in row.keys() else False,
            master_product_id=row["master_product_id"] if "master_product_id" in row.keys() else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class ProductPalletType:
    """One named packing option of a product (e.g. "pine pallet" holding 31
    boxes, or a "CTN" holding 30 pieces). A product can carry any number of
    these; every product ALSO implicitly offers "loose" (goods sold
    unpalletised, zero pallets), which is never stored. The alternate
    quantity one pallet holds is always derived - boxes_per_pallet x the
    product's per-box alternate_quantity - so it can't drift when the
    product spec changes.

    `unit_kind` says which LEVEL of packing this is: a 'carton' is an inner
    box that then goes ON a pallet, a 'pallet' is what a forklift moves into
    the container. Tiles have no carton level (boxes sit straight on the
    pallet); hardware goes pieces -> carton -> pallet. Loading Planning is
    the only thing that reads it - see LoadingPlanningPallet.gross_weight_kg
    for how the two tares stack."""
    id: Optional[int]
    company_id: int
    product_id: int
    name: str
    boxes_per_pallet: float
    weight_kg: Optional[float] = None
    unit_kind: str = "pallet"  # 'pallet' | 'carton'
    sort_order: int = 0
    created_at: Optional[str] = None

    @property
    def is_carton(self) -> bool:
        return (self.unit_kind or "pallet") == "carton"

    @staticmethod
    def from_row(row) -> "ProductPalletType":
        return ProductPalletType(
            id=row["id"],
            company_id=row["company_id"],
            product_id=row["product_id"],
            name=row["name"],
            boxes_per_pallet=row["boxes_per_pallet"],
            weight_kg=row["weight_kg"] if "weight_kg" in row.keys() else None,
            unit_kind=(row["unit_kind"] if "unit_kind" in row.keys() else None) or "pallet",
            sort_order=row["sort_order"],
            created_at=row["created_at"],
        )


@dataclass
class ProductFolder:
    """A sub category inside a product (shown as "Sub Category" in the UI;
    the table keeps its historical product_folders name). `parent_id=None`
    means it sits at the product's top level; sub categories can nest to any
    depth via self-reference, but always belong to exactly one product."""
    id: Optional[int]
    company_id: int
    product_id: int
    name: str
    parent_id: Optional[int] = None
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "ProductFolder":
        return ProductFolder(
            id=row["id"],
            company_id=row["company_id"],
            product_id=row["product_id"],
            name=row["name"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
        )


@dataclass
class Design:
    """The sellable leaf of the catalog: one concrete design (finish/color
    variant) of a product, carrying its own price and photos. Packing,
    quantity and weight are shared across every design of the same product,
    so they live on Product instead. `folder_id=None` means it sits directly
    under the product."""
    id: Optional[int]
    company_id: int
    product_id: int
    design_name: str
    folder_id: Optional[int] = None
    description: Optional[str] = None
    surface: Optional[str] = None  # optional finish, e.g. GLOSSY / MATT / CHROME
    price_usd: Optional[float] = None
    photo_path: Optional[str] = None
    dimension_photo_path: Optional[str] = None
    alt_text: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "Design":
        return Design(
            id=row["id"],
            company_id=row["company_id"],
            product_id=row["product_id"],
            folder_id=row["folder_id"],
            design_name=row["design_name"],
            description=row["description"],
            surface=row["surface"] if "surface" in row.keys() else None,
            price_usd=row["price_usd"],
            photo_path=row["photo_path"],
            dimension_photo_path=row["dimension_photo_path"],
            alt_text=row["alt_text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class CifMoneyLadder:
    """The money ladder shared by every document that quotes a buyer a price:
    Quotation, Proforma Invoice and Export Invoice.

    Every price typed into this app is an FOB price - the per-unit rate is the
    goods on their own, and the carriage/insurance/handling are charges added
    on top of it. So all three documents build the ladder UPWARDS from the
    goods total:

        FOB Value       goods total (the sum of the line totals: rate x qty),
                        the figure customs and the export incentives are
                        computed against
      + Insurance       whichever of the four the delivery term carries; the
      + Sea Freight     ones it doesn't are already stored as zero (see
      + Certification   drops_sea_freight/drops_insurance in app/utils.py), so
      + Other           adding all four unconditionally is always correct
      = CIF/CFR Value
      - Discount
      = Invoice Value   what the buyer actually pays; the figure spelled out
                        in words at the foot of the sheet

    Each document supplies that shape by overriding `cif_value_usd` and
    `fob_value_usd` (the two definitions below are the base ladder's older
    downward form, kept only as the fallback nothing now uses) and inheriting
    `invoice_value_usd` unchanged.

    Kept in one mixin (rather than repeated on each dataclass) so a change to
    the arithmetic can't leave one document type disagreeing with another.
    A mixin adds no fields, so the dataclasses that inherit it keep their exact
    field list - which matters, because document version history round-trips
    them through `dataclasses.asdict` / `cls(**data)`.

    Implementors supply `subtotal_usd` (the goods total, sometimes precomputed
    by a list query) plus the five charge/discount fields.
    """

    @property
    def round_off(self) -> float:
        """The cent or two the printed lines can't carry - see Quotation's own
        `round_off` field, which shadows this. Zero for every document that
        prices the plain way, where the lines ARE the CIF value."""
        return 0.0

    @property
    def cif_value_usd(self) -> float:
        """CIF Value - the goods total, i.e. what used to print as SUBTOTAL."""
        return self.subtotal_usd + self.round_off

    @property
    def invoice_value_usd(self) -> float:
        """CIF less the discount: the amount payable."""
        return self.cif_value_usd - self.discount_amount

    @property
    def charges_total(self) -> float:
        """The four charges that sit between FOB and CIF. The discount is NOT
        one of them - it comes off ABOVE the invoice value line."""
        return self.insurance + self.sea_freight + self.certification + self.other_charges

    @property
    def fob_value_usd(self) -> float:
        """The invoice value with the carriage/handling charges stripped back
        out. Under FOB terms sea freight and insurance are already held at zero
        (the buyer carries the ocean leg), so they drop out of the subtraction
        on their own and need no special case here."""
        return (self.invoice_value_usd - self.insurance - self.sea_freight
                - self.certification - self.other_charges)


@dataclass
class QuotationItem:
    id: Optional[int]
    quotation_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    dimension_mm: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    pallets: Optional[float] = None
    quantity_value: float = 0
    unit: str = "SQM"
    price_usd: float = 0
    total_usd: float = 0
    # Unused (kept so an old row still loads) - quotations no longer have an
    # FOB-typed-price mode; price_usd is always the absolute price the user
    # typed. See Quotation.fob_pricing / Quotation.cif_adjust_usd.
    fob_price_usd: Optional[float] = None

    @staticmethod
    def from_row(row) -> "QuotationItem":
        return QuotationItem(
            id=row["id"],
            quotation_id=row["quotation_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            dimension_mm=row["dimension_mm"],
            hsn_code=row["hsn_code"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            pallets=row["pallets"] if "pallets" in row.keys() else None,
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_usd=row["price_usd"],
            total_usd=row["total_usd"],
            fob_price_usd=row["fob_price_usd"] if "fob_price_usd" in row.keys() else None,
        )


@dataclass
class Quotation(CifMoneyLadder):
    id: Optional[int]
    company_id: int
    quotation_number: str
    quotation_date: str
    buyer_name: str
    created_by: int
    lead_id: Optional[int] = None
    buyer_address: Optional[str] = None
    buyer_reference_no: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    final_destination: Optional[str] = None
    packing_details: Optional[str] = None
    shipping_mode: Optional[str] = None
    shipping_terms: Optional[str] = None
    payment_terms: Optional[str] = None
    price_validity_days: int = 30
    remarks: Optional[str] = None
    sea_freight: float = 0
    insurance: float = 0
    certification: float = 0
    other_charges: float = 0
    discount_amount: float = 0
    # Unused (kept so an old quotation's row still loads): quotations no
    # longer have an FOB-typed-price mode - price_usd is always the absolute
    # price the user typed. See cif_adjust_usd and cif_value_usd below.
    fob_pricing: bool = False
    round_off: float = 0
    # The manual gap between what the CIF value field was typed as and what
    # the ladder computes (goods total + charges) - see cif_value_usd below
    # and the form's subtotal-input handler. Unlike price_usd this IS the one
    # place a manual adjustment is allowed to land; 0 on a quotation whose CIF
    # value was never overridden.
    cif_adjust_usd: float = 0
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    bank_branch: Optional[str] = None
    bank_address: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    # Currency the document is written in, picked from the Administration ->
    # Miscellaneous list and snapshotted so a later edit of that list can't
    # rewrite an issued sheet. Display information only - no conversion.
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    items: List[QuotationItem] = field(default_factory=list)
    computed_subtotal_usd: Optional[float] = None  # precomputed by list queries that don't load items
    # Container type/count list, e.g. "2 x 20FT FCL" - same shape as
    # BookingDetail.containers/ExportInvoice.containers. Loaded/replaced by
    # QuotationRepository, not a plain column - see container_details below
    # for the printed/prefill text built from it.
    containers: List[dict] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "Quotation":
        return Quotation(
            id=row["id"],
            company_id=row["company_id"],
            quotation_number=row["quotation_number"],
            quotation_date=row["quotation_date"],
            lead_id=row["lead_id"] if "lead_id" in row.keys() else None,
            buyer_name=row["buyer_name"],
            buyer_address=row["buyer_address"],
            buyer_reference_no=row["buyer_reference_no"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            final_destination=row["final_destination"] if "final_destination" in row.keys() else None,
            packing_details=row["packing_details"],
            shipping_mode=row["shipping_mode"],
            shipping_terms=row["shipping_terms"],
            payment_terms=row["payment_terms"],
            price_validity_days=row["price_validity_days"],
            remarks=row["remarks"],
            sea_freight=row["sea_freight"] if "sea_freight" in row.keys() else 0,
            insurance=row["insurance"] if "insurance" in row.keys() else 0,
            certification=row["certification"] if "certification" in row.keys() else 0,
            other_charges=row["other_charges"] if "other_charges" in row.keys() else 0,
            discount_amount=row["discount_amount"],
            fob_pricing=bool(row["fob_pricing"]) if "fob_pricing" in row.keys() else False,
            round_off=(row["round_off"] if "round_off" in row.keys() else 0) or 0,
            cif_adjust_usd=(row["cif_adjust_usd"] if "cif_adjust_usd" in row.keys() else 0) or 0,
            bank_name=row["bank_name"],
            bank_account_number=row["bank_account_number"],
            bank_ifsc_code=row["bank_ifsc_code"],
            bank_swift_code=row["bank_swift_code"],
            bank_branch=row["bank_branch"],
            bank_address=row["bank_address"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            computed_subtotal_usd=row["items_total"] if "items_total" in row.keys() else None,
            currency_code=row["currency_code"] if "currency_code" in row.keys() else None,
            currency_symbol=row["currency_symbol"] if "currency_symbol" in row.keys() else None,
        )

    @property
    def container_details(self) -> Optional[str]:
        """The printed/prefilled Container Details text, e.g. "2 x 20FT FCL" -
        one row per line, same "COUNT x TYPE" format the Export Invoice's own
        Container Details cell prints. Read-only: `containers` (loaded by
        QuotationRepository) is the stored data, this just formats it - used
        by the quotation sheet template and by
        ProformaInvoiceService.build_prefill_from_quotation."""
        if not self.containers:
            return None
        return "\n".join(f"{c['container_count']} x {c['container_type']}" for c in self.containers)

    @property
    def currency_name(self) -> str:
        """The currency's name, e.g. `USD` - what the money column headings
        and typed-amount labels read."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[0]

    @property
    def currency_prefix(self) -> str:
        """The symbol printed in front of an amount, e.g. `$`."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[1]

    @property
    def currency_label(self) -> str:
        """The Currency cell on a printed sheet, e.g. `USD [ $ ]`."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[2]

    @property
    def subtotal_usd(self) -> float:
        if self.computed_subtotal_usd is not None:
            return self.computed_subtotal_usd
        return sum(item.total_usd for item in self.items)

    @property
    def cif_value_usd(self) -> float:
        """Overrides CifMoneyLadder.cif_value_usd - a quotation's typed price
        is always the absolute FOB price (quantity_value * price_usd summed
        across every line, i.e. subtotal_usd, is the FOB invoice total), so
        unlike every other document here CIF is built UPWARDS from FOB by
        adding the charges rather than being the goods total on its own. This
        holds regardless of the shipping terms chosen - the terms only decide
        which charge fields are non-zero (see drops_sea_freight/drops_insurance),
        never the FOB total itself. cif_adjust_usd is the one place a manual
        typed-CIF-value override is allowed to land (see the form's
        subtotal-input handler); it is 0 on a quotation that was never
        overridden that way."""
        return self.subtotal_usd + self.charges_total + self.cif_adjust_usd

    @property
    def fob_value_usd(self) -> float:
        """Overrides CifMoneyLadder.fob_value_usd. A quotation's FOB value is
        simply the goods total (quantity x price, summed across every line) -
        it is never reduced by the discount or built up with charges, unlike
        the base ladder's version. The discount only ever comes off between
        CIF and invoice value (see invoice_value_usd); it does not touch this
        figure. This holds for every shipping term: under FOB terms nothing
        is added on top of it (sea freight/insurance are held at zero and CIF
        is not shown), and under CIF/CFR terms it is still the ex-charges
        value of the goods themselves."""
        return self.subtotal_usd


@dataclass
class PurchaseOrderItem:
    """One product line of a purchase order. Prices are INR - typically the
    ex-factory rate per BOX (price_per='BOX'), but a row can also price per
    its quantity unit (price_per=<unit>). total_inr is derived at save time
    from whichever basis the row uses."""
    id: Optional[int]
    purchase_order_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    hsn_code: Optional[str] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    quantity_value: float = 0
    unit: str = "SQM"
    price_inr: float = 0
    price_per: str = "BOX"
    total_inr: float = 0
    design_id: Optional[int] = None
    design_name: Optional[str] = None

    @staticmethod
    def from_row(row) -> "PurchaseOrderItem":
        return PurchaseOrderItem(
            id=row["id"],
            purchase_order_id=row["purchase_order_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            hsn_code=row["hsn_code"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_inr=row["price_inr"],
            price_per=row["price_per"],
            total_inr=row["total_inr"],
            design_id=row["design_id"] if "design_id" in row.keys() else None,
            design_name=row["design_name"] if "design_name" in row.keys() else None,
        )


@dataclass
class PurchaseOrderItemBatch:
    """One batch a purchase order line was actually produced in. A design's
    ordered quantity is routinely fired in several batches, so a line has any
    number of these. quantity_boxes is in the line's own quantity_unit."""
    id: Optional[int]
    purchase_order_item_id: Optional[int]
    sr_no: int
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    batch_number: Optional[str] = None
    production_date: Optional[str] = None
    quantity_boxes: float = 0
    remarks: Optional[str] = None

    @staticmethod
    def from_row(row) -> "PurchaseOrderItemBatch":
        return PurchaseOrderItemBatch(
            id=row["id"],
            purchase_order_item_id=row["purchase_order_item_id"],
            sr_no=row["sr_no"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            batch_number=row["batch_number"],
            production_date=row["production_date"],
            quantity_boxes=row["quantity_boxes"] or 0,
            remarks=row["remarks"],
        )


@dataclass
class PurchaseOrderItemProduction:
    """What the supplier has made against one design of one purchase order
    line - a PO orders by product, and the design split comes from the linked
    proforma invoice's packing list, so the key is the pair. `status`
    is set by hand - it is a statement about the supplier's floor, not
    something derived from the batches, which may legitimately lag behind it
    (Ready before every batch is keyed in) or run ahead of it (a trial batch
    on a line still Pending). produced_boxes is derived, never stored."""
    purchase_order_item_id: int
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    status: str = "pending"
    updated_by: Optional[int] = None
    updated_by_name: Optional[str] = None  # populated by joined queries only
    updated_at: Optional[str] = None
    batches: List[PurchaseOrderItemBatch] = field(default_factory=list)

    @property
    def produced_boxes(self) -> float:
        return sum(b.quantity_boxes or 0 for b in self.batches)

    @staticmethod
    def from_row(row) -> "PurchaseOrderItemProduction":
        return PurchaseOrderItemProduction(
            purchase_order_item_id=row["purchase_order_item_id"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            status=row["status"] or "pending",
            updated_by=row["updated_by"],
            updated_by_name=row["updated_by_name"] if "updated_by_name" in row.keys() else None,
            updated_at=row["updated_at"],
        )


@dataclass
class PurchaseOrder:
    """The next document after the Proforma Invoice in the client pipeline.
    Unlike the other documents, OUR company is the BUYER here and a supplier
    is the SELLER - so the header carries seller details instead of a
    consignee, and amounts are INR. Tax percentages are stored; every amount
    (tax, round-off, order value) is derived, never stored. The percentages
    themselves aren't typed in either - they follow from `purchase_type` plus
    the seller's GSTIN state code (see PurchaseOrderService._tax_percentages)."""
    id: Optional[int]
    company_id: int
    po_number: str
    po_date: str
    seller_name: str
    created_by: int
    proforma_invoice_id: Optional[int] = None
    seller_supplier_id: Optional[int] = None
    seller_address: Optional[str] = None
    seller_pan: Optional[str] = None
    seller_gstin: Optional[str] = None
    seller_ref_no: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    container_details: Optional[str] = None
    delivery_time: Optional[str] = None
    advance_percent: Optional[str] = None
    payment_terms: Optional[str] = None
    remarks: Optional[str] = None
    igst_percent: float = 0
    cgst_percent: float = 0
    sgst_percent: float = 0
    purchase_type: str = DEFAULT_PURCHASE_TYPE  # key of PURCHASE_TYPES
    # When set, the printed sheet skips the computed IGST/CGST/SGST rows and
    # prints a single "TAX AS ACTUAL" line instead - the order value then
    # equals the goods subtotal with no tax added, since the real tax will
    # only be known once the supplier's own purchase invoice is raised.
    tax_as_actual: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    proforma_invoice_number: Optional[str] = None  # populated by joined queries only
    # Currency the document is written in, picked from the Administration ->
    # Miscellaneous list and snapshotted so a later edit of that list can't
    # rewrite an issued sheet. Display information only - no conversion.
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    items: List[PurchaseOrderItem] = field(default_factory=list)
    computed_subtotal_inr: Optional[float] = None  # precomputed by list queries that don't load items

    @staticmethod
    def from_row(row) -> "PurchaseOrder":
        return PurchaseOrder(
            id=row["id"],
            company_id=row["company_id"],
            po_number=row["po_number"],
            po_date=row["po_date"],
            proforma_invoice_id=row["proforma_invoice_id"],
            seller_supplier_id=row["seller_supplier_id"],
            seller_name=row["seller_name"],
            seller_address=row["seller_address"],
            seller_pan=row["seller_pan"],
            seller_gstin=row["seller_gstin"],
            seller_ref_no=row["seller_ref_no"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            container_details=row["container_details"],
            delivery_time=row["delivery_time"],
            advance_percent=row["advance_percent"],
            payment_terms=row["payment_terms"],
            remarks=row["remarks"],
            igst_percent=row["igst_percent"],
            cgst_percent=row["cgst_percent"],
            sgst_percent=row["sgst_percent"],
            purchase_type=(row["purchase_type"] if "purchase_type" in row.keys() else None) or DEFAULT_PURCHASE_TYPE,
            tax_as_actual=bool(row["tax_as_actual"]) if "tax_as_actual" in row.keys() else False,
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            proforma_invoice_number=row["proforma_invoice_number"] if "proforma_invoice_number" in row.keys() else None,
            computed_subtotal_inr=row["items_total"] if "items_total" in row.keys() else None,
            currency_code=row["currency_code"] if "currency_code" in row.keys() else None,
            currency_symbol=row["currency_symbol"] if "currency_symbol" in row.keys() else None,
        )

    @property
    def currency_name(self) -> str:
        """The currency's name, e.g. `USD` - what the money column headings
        and typed-amount labels read."""
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[0]

    @property
    def currency_prefix(self) -> str:
        """The symbol printed in front of an amount, e.g. `$`."""
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[1]

    @property
    def currency_label(self) -> str:
        """The Currency cell on a printed sheet, e.g. `USD [ $ ]`."""
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[2]

    @property
    def total_boxes(self) -> float:
        return sum(item.quantity_boxes or 0 for item in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(item.quantity_value or 0 for item in self.items)

    @property
    def subtotal_inr(self) -> float:
        if self.computed_subtotal_inr is not None and not self.items:
            return self.computed_subtotal_inr
        return sum(item.total_inr for item in self.items)

    @property
    def igst_amount(self) -> float:
        return round(self.subtotal_inr * (self.igst_percent or 0) / 100, 2)

    @property
    def cgst_amount(self) -> float:
        return round(self.subtotal_inr * (self.cgst_percent or 0) / 100, 2)

    @property
    def sgst_amount(self) -> float:
        return round(self.subtotal_inr * (self.sgst_percent or 0) / 100, 2)

    @property
    def order_value_inr(self) -> float:
        """The final order value, rounded to the whole rupee (the round-off
        line on the printed PO bridges the difference). Under "Tax as
        actual" no tax is added - the real amount will only be known once
        the supplier's own purchase invoice is raised."""
        if self.tax_as_actual:
            return float(round(self.subtotal_inr))
        return float(round(self.subtotal_inr + self.igst_amount + self.cgst_amount + self.sgst_amount))

    @property
    def round_off_inr(self) -> float:
        gross = self.subtotal_inr if self.tax_as_actual else (
            self.subtotal_inr + self.igst_amount + self.cgst_amount + self.sgst_amount
        )
        return round(self.order_value_inr - gross, 2)


@dataclass
class JobWorkItem:
    """One DESIGN line of a job work - a chain of figures computed
    server-side and persisted (same treatment PurchaseOrderItem.total_inr
    gets), so a printed sheet never disagrees with what was actually saved:

        source_quantity     this design's quantity_boxes off the proforma
                             invoice's packing list under `product_id`,
                             matched by design name (0 with no match)
        conversion_value     typed; must be > 0
        extra_percent        typed; may be 0
        converted_quantity  = source_quantity / conversion_value
        extra_quantity       = converted_quantity * extra_percent / 100
        job_quantity          = converted_quantity + extra_quantity - the
                              document's one final figure per design

    product_id/product_name name the SOURCE proforma invoice product - kept
    only to look up source_quantity, not shown as "the" product on the
    printed sheet. to_product_id/to_product_name name what the job work
    converts the design INTO, which is what the sheet actually describes as
    the goods (its HSN is what hsn_code snapshots), and design_id/design_name
    is one of to_product's own catalog designs."""
    id: Optional[int]
    job_work_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    to_product_id: Optional[int] = None
    to_product_name: Optional[str] = None
    hsn_code: Optional[str] = None
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    # What every quantity below is counted in: the product's QTY unit
    # (BOX/PCS), taken from to_product - not its alternate-quantity unit,
    # since job work is counted, not measured by area.
    unit: str = "PCS"
    source_quantity: float = 0
    conversion_value: float = 1
    extra_percent: float = 0
    converted_quantity: float = 0
    extra_quantity: float = 0
    job_quantity: float = 0

    @staticmethod
    def from_row(row) -> "JobWorkItem":
        return JobWorkItem(
            id=row["id"],
            job_work_id=row["job_work_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            to_product_id=row["to_product_id"],
            to_product_name=row["to_product_name"],
            hsn_code=row["hsn_code"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            unit=row["unit"],
            source_quantity=row["source_quantity"],
            conversion_value=row["conversion_value"],
            extra_percent=row["extra_percent"],
            converted_quantity=row["converted_quantity"],
            extra_quantity=row["extra_quantity"],
            job_quantity=row["job_quantity"],
        )


@dataclass
class JobWorkProduct:
    """One row of a job work's Products card - a plain copy of
    PurchaseOrderItem's shape (product/HSN/boxes/qty/unit/price/total),
    picked from the Job Manufacturer -> Product dropdown (the invoice's own
    products), NOT "To Product" (the design lines' conversion target).
    Purely a costing/reference line: never printed, never feeds
    JobWorkItem's derived chain."""
    id: Optional[int]
    job_work_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    hsn_code: Optional[str] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    quantity_value: float = 0
    unit: str = "SQM"
    price_inr: float = 0
    price_per: str = "BOX"
    total_inr: float = 0

    @staticmethod
    def from_row(row) -> "JobWorkProduct":
        return JobWorkProduct(
            id=row["id"],
            job_work_id=row["job_work_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            hsn_code=row["hsn_code"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_inr=row["price_inr"],
            price_per=row["price_per"],
            total_inr=row["total_inr"],
        )


@dataclass
class JobWork:
    """The JOB WORK document: a proforma invoice's goods handed on to be
    worked on. Two parties sit on the one sheet - the FROM SELLER whose goods
    go out and the JOB MANUFACTURER who does the work, both Suppliers - and
    the lines are DESIGNS rather than products, since that is the granularity
    job work is actually sent out at. proforma_invoice_id is a
    "generated from" reference only, the same pattern as
    PurchaseOrder.proforma_invoice_id."""
    id: Optional[int]
    company_id: int
    job_work_number: str
    job_work_date: str
    seller_name: str
    created_by: int
    proforma_invoice_id: Optional[int] = None
    seller_supplier_id: Optional[int] = None
    seller_address: Optional[str] = None
    seller_pan: Optional[str] = None
    seller_gstin: Optional[str] = None
    manufacturer_supplier_id: Optional[int] = None
    manufacturer_name: Optional[str] = None
    manufacturer_address: Optional[str] = None
    manufacturer_pan: Optional[str] = None
    manufacturer_gstin: Optional[str] = None
    seller_ref_no: Optional[str] = None
    delivery_time: Optional[str] = None
    advance_percent: Optional[str] = None
    payment_terms: Optional[str] = None
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    proforma_invoice_number: Optional[str] = None  # populated by joined queries only
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    # Products card (a copy of PurchaseOrder's own tax block): the rate
    # follows purchase_type, split into IGST or CGST+SGST against
    # manufacturer_gstin - see JobWorkService._tax_percentages. Never
    # printed, never derived into job_quantity above.
    igst_percent: float = 0
    cgst_percent: float = 0
    sgst_percent: float = 0
    purchase_type: str = DEFAULT_PURCHASE_TYPE  # key of PURCHASE_TYPES
    tax_as_actual: bool = False
    items: List[JobWorkItem] = field(default_factory=list)
    products: List[JobWorkProduct] = field(default_factory=list)
    # Precomputed by list queries, which deliberately don't load items.
    computed_job_quantity: Optional[float] = None
    computed_subtotal_inr: Optional[float] = None

    @staticmethod
    def from_row(row) -> "JobWork":
        return JobWork(
            id=row["id"],
            company_id=row["company_id"],
            job_work_number=row["job_work_number"],
            job_work_date=row["job_work_date"],
            proforma_invoice_id=row["proforma_invoice_id"],
            seller_supplier_id=row["seller_supplier_id"],
            seller_name=row["seller_name"],
            seller_address=row["seller_address"],
            seller_pan=row["seller_pan"],
            seller_gstin=row["seller_gstin"],
            manufacturer_supplier_id=row["manufacturer_supplier_id"],
            manufacturer_name=row["manufacturer_name"],
            manufacturer_address=row["manufacturer_address"],
            manufacturer_pan=row["manufacturer_pan"],
            manufacturer_gstin=row["manufacturer_gstin"],
            seller_ref_no=row["seller_ref_no"],
            delivery_time=row["delivery_time"],
            advance_percent=row["advance_percent"],
            payment_terms=row["payment_terms"],
            remarks=row["remarks"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            proforma_invoice_number=(
                row["proforma_invoice_number"] if "proforma_invoice_number" in row.keys() else None
            ),
            currency_code=row["currency_code"],
            currency_symbol=row["currency_symbol"],
            igst_percent=(row["igst_percent"] if "igst_percent" in row.keys() else None) or 0,
            cgst_percent=(row["cgst_percent"] if "cgst_percent" in row.keys() else None) or 0,
            sgst_percent=(row["sgst_percent"] if "sgst_percent" in row.keys() else None) or 0,
            purchase_type=(row["purchase_type"] if "purchase_type" in row.keys() else None) or DEFAULT_PURCHASE_TYPE,
            tax_as_actual=bool(row["tax_as_actual"]) if "tax_as_actual" in row.keys() else False,
            computed_job_quantity=row["items_job_quantity"] if "items_job_quantity" in row.keys() else None,
            computed_subtotal_inr=row["products_total"] if "products_total" in row.keys() else None,
        )

    @property
    def currency_name(self) -> str:
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[0]

    @property
    def currency_prefix(self) -> str:
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[1]

    @property
    def currency_label(self) -> str:
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[2]

    @property
    def total_job_quantity(self) -> float:
        if self.computed_job_quantity is not None and not self.items:
            return self.computed_job_quantity
        return sum(item.job_quantity or 0 for item in self.items)

    @property
    def total_source_quantity(self) -> float:
        return sum(item.source_quantity or 0 for item in self.items)

    @property
    def total_converted_quantity(self) -> float:
        return sum(item.converted_quantity or 0 for item in self.items)

    @property
    def total_extra_quantity(self) -> float:
        return sum(item.extra_quantity or 0 for item in self.items)

    # ---- Products card money figures (mirrors PurchaseOrder's own, over
    # self.products rather than self.items - a job work now prints/numbers as
    # a purchase order, so the same "Start from" picker on a purchase
    # invoice needs the same order-value figure to display). ----
    @property
    def subtotal_inr(self) -> float:
        if self.computed_subtotal_inr is not None and not self.products:
            return self.computed_subtotal_inr
        return sum(product.total_inr for product in self.products)

    @property
    def igst_amount(self) -> float:
        return round(self.subtotal_inr * (self.igst_percent or 0) / 100, 2)

    @property
    def cgst_amount(self) -> float:
        return round(self.subtotal_inr * (self.cgst_percent or 0) / 100, 2)

    @property
    def sgst_amount(self) -> float:
        return round(self.subtotal_inr * (self.sgst_percent or 0) / 100, 2)

    @property
    def order_value_inr(self) -> float:
        if self.tax_as_actual:
            return float(round(self.subtotal_inr))
        return float(round(self.subtotal_inr + self.igst_amount + self.cgst_amount + self.sgst_amount))



@dataclass
class PurchaseInvoiceItem:
    """One product line of a purchase invoice - same shape as
    PurchaseOrderItem, copied in from the linked purchase order at creation
    time so the invoice stays a self-contained record even if that
    purchase order is later edited or deleted."""
    id: Optional[int]
    purchase_invoice_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    hsn_code: Optional[str] = None
    quantity_boxes: Optional[float] = None
    quantity_value: float = 0
    unit: str = "SQM"
    price_inr: float = 0
    price_per: str = "BOX"
    total_inr: float = 0
    # Which of the invoice's (possibly several) purchase orders this line
    # came from - None for a hand-added row with no PO origin. Drives the
    # "grouped by purchase order" product table; source_po_number is a
    # joined display-only convenience, never written.
    purchase_order_id: Optional[int] = None
    source_po_number: Optional[str] = None  # populated by joined queries only
    # Same idea as purchase_order_id/source_po_number above, for a line
    # prefilled from a Job Work's Products card instead - mutually exclusive
    # with purchase_order_id per row.
    job_work_id: Optional[int] = None
    source_jw_number: Optional[str] = None  # populated by joined queries only

    @staticmethod
    def from_row(row) -> "PurchaseInvoiceItem":
        return PurchaseInvoiceItem(
            id=row["id"],
            purchase_invoice_id=row["purchase_invoice_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            hsn_code=row["hsn_code"],
            quantity_boxes=row["quantity_boxes"],
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_inr=row["price_inr"],
            price_per=row["price_per"],
            total_inr=row["total_inr"],
            purchase_order_id=row["purchase_order_id"] if "purchase_order_id" in row.keys() else None,
            source_po_number=row["source_po_number"] if "source_po_number" in row.keys() else None,
            job_work_id=row["job_work_id"] if "job_work_id" in row.keys() else None,
            source_jw_number=row["source_jw_number"] if "source_jw_number" in row.keys() else None,
        )


@dataclass
class PurchaseInvoice:
    """The last document in the pipeline: raised once a supplier's goods
    (against one of our purchase orders) actually arrive, carrying the
    supplier's own invoice/transport details. Unlike every other document
    type here, WE don't generate a PDF for this one - the supplier already
    sent their own invoice as a PDF (supplier_pdf_path); this record just
    saves its numbers alongside it. `invoice_number`/`invoice_date` are the
    SUPPLIER's own values as printed on that PDF; `purchase_invoice_number`
    is our own internal, auto-generated identifier, kept only for
    consistency with every other document type's numbering/version-history
    machinery. Discount/insurance/freight/tax/round-off are typed in
    directly (not derived) since they must match what the supplier actually
    charged, not what our own tax rules would compute."""
    id: Optional[int]
    company_id: int
    purchase_invoice_number: str
    invoice_number: str
    invoice_date: str
    seller_name: str
    created_by: int
    purchase_order_id: Optional[int] = None
    # Same idea as purchase_order_id above, for a purchase invoice raised
    # against a Job Work instead - a job work now prints/numbers as a
    # purchase order (see job_works.job_work_number), so it can be the
    # "generated from" reference here too. Mutually exclusive in practice
    # with purchase_order_id, same as job_work_ids/purchase_order_ids below.
    job_work_id: Optional[int] = None
    lead_id: Optional[int] = None
    seller_supplier_id: Optional[int] = None
    seller_address: Optional[str] = None
    seller_pan: Optional[str] = None
    seller_gstin: Optional[str] = None
    seller_ref_no: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    container_details: Optional[str] = None
    transporter_name: Optional[str] = None
    epcg_number: Optional[str] = None
    epcg_date: Optional[str] = None
    supplier_pdf_path: Optional[str] = None
    discount_amount: float = 0
    insurance_other: float = 0
    freight: float = 0
    igst_amount: float = 0
    cgst_amount: float = 0
    sgst_amount: float = 0
    round_off: float = 0
    purchase_type: str = DEFAULT_PURCHASE_TYPE  # key of PURCHASE_TYPES - typed, not derived, unlike PurchaseOrder's
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    purchase_order_number: Optional[str] = None  # populated by joined queries only
    job_work_number: Optional[str] = None  # populated by joined queries only
    # Currency the document is written in, picked from the Administration ->
    # Miscellaneous list and snapshotted so a later edit of that list can't
    # rewrite an issued sheet. Display information only - no conversion.
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    items: List[PurchaseInvoiceItem] = field(default_factory=list)
    vehicle_numbers: List[str] = field(default_factory=list)
    computed_subtotal_inr: Optional[float] = None  # precomputed by list queries that don't load items
    # The full purchase-order list this invoice was raised against - a
    # shipment can cover more than one of our purchase orders (of the same
    # supplier) at once. purchase_order_id above stays the first/primary one
    # for older single-PO call sites; these are populated from the
    # purchase_invoice_purchase_order_links table.
    purchase_order_ids: List[int] = field(default_factory=list)
    purchase_orders: List["PurchaseOrder"] = field(default_factory=list)  # populated by the service, not from_row
    # Same idea as purchase_order_ids/purchase_orders above, for the Job
    # Works (of possibly several) this invoice was raised against instead.
    job_work_ids: List[int] = field(default_factory=list)
    job_works: List["JobWork"] = field(default_factory=list)  # populated by the service, not from_row

    @staticmethod
    def from_row(row) -> "PurchaseInvoice":
        return PurchaseInvoice(
            id=row["id"],
            company_id=row["company_id"],
            purchase_invoice_number=row["purchase_invoice_number"],
            invoice_number=row["invoice_number"],
            invoice_date=row["invoice_date"],
            purchase_order_id=row["purchase_order_id"],
            job_work_id=row["job_work_id"] if "job_work_id" in row.keys() else None,
            lead_id=row["lead_id"],
            seller_supplier_id=row["seller_supplier_id"],
            seller_name=row["seller_name"],
            seller_address=row["seller_address"],
            seller_pan=row["seller_pan"],
            seller_gstin=row["seller_gstin"],
            seller_ref_no=row["seller_ref_no"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            container_details=row["container_details"],
            transporter_name=row["transporter_name"],
            epcg_number=row["epcg_number"],
            epcg_date=row["epcg_date"],
            supplier_pdf_path=row["supplier_pdf_path"],
            discount_amount=row["discount_amount"],
            insurance_other=row["insurance_other"],
            freight=row["freight"],
            igst_amount=row["igst_amount"],
            cgst_amount=row["cgst_amount"],
            sgst_amount=row["sgst_amount"],
            round_off=row["round_off"],
            purchase_type=(row["purchase_type"] if "purchase_type" in row.keys() else None) or DEFAULT_PURCHASE_TYPE,
            remarks=row["remarks"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            purchase_order_number=row["purchase_order_number"] if "purchase_order_number" in row.keys() else None,
            job_work_number=row["job_work_number"] if "job_work_number" in row.keys() else None,
            computed_subtotal_inr=row["items_total"] if "items_total" in row.keys() else None,
            currency_code=row["currency_code"] if "currency_code" in row.keys() else None,
            currency_symbol=row["currency_symbol"] if "currency_symbol" in row.keys() else None,
        )

    @property
    def currency_name(self) -> str:
        """The currency's name, e.g. `USD` - what the money column headings
        and typed-amount labels read."""
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[0]

    @property
    def currency_prefix(self) -> str:
        """The symbol printed in front of an amount, e.g. `$`."""
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[1]

    @property
    def currency_label(self) -> str:
        """The Currency cell on a printed sheet, e.g. `USD [ $ ]`."""
        return currency_display(self.currency_code, self.currency_symbol, "INR", "₹")[2]

    @property
    def total_boxes(self) -> float:
        return sum(item.quantity_boxes or 0 for item in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(item.quantity_value or 0 for item in self.items)

    @property
    def subtotal_inr(self) -> float:
        if self.computed_subtotal_inr is not None and not self.items:
            return self.computed_subtotal_inr
        return sum(item.total_inr for item in self.items)

    @property
    def invoice_value_inr(self) -> float:
        return round(
            self.subtotal_inr + self.freight + self.insurance_other
            + self.igst_amount + self.cgst_amount + self.sgst_amount
            - self.discount_amount + self.round_off,
            2,
        )


@dataclass
class JobOut:
    """The JOB OUT sheet - "DELIVERY CHALLAN FOR JOBWORK", the paper that
    physically travels with goods going out to a job manufacturer. Raised
    off ONE purchase invoice and printed from it.

    Deliberately the thinnest document in this app: it stores ONLY what is
    actually typed at dispatch time (this challan's own number/date, the
    transport block, the e-way bill). Every other thing the sheet prints -
    the receiver party, the goods lines, HSN/qty/rate/taxable value and the
    whole tax footer - is read LIVE off purchase_invoice_id when the sheet
    renders (see JobOutService.build_sheet), NOT snapshotted here. That is
    the opposite of the snapshot convention every other document in this
    app follows, and it is intentional: a challan is a dispatch note against
    an invoice that already exists, so it should always agree with that
    invoice rather than preserve a stale copy of it.

    `dispatch_from_company` is the form's one "which address" switch: False
    prints the purchase invoice's own SELLER as the Dispatch From block,
    True prints our own company (the goods left our warehouse instead). The
    letterhead stays our own company either way - only that block swaps."""
    id: Optional[int]
    company_id: int
    purchase_invoice_id: int
    delivery_challan_no: str
    delivery_challan_date: str
    created_by: int
    dispatch_from_company: bool = False
    # Blank falls back, at render time only, to the transporter holding
    # transport_gstin and then to the purchase invoice's own transporter_name
    # - see JobOutService._transporter_name.
    transporter_name: Optional[str] = None
    transport_gstin: Optional[str] = None
    lr_no: Optional[str] = None
    vehicle_no: Optional[str] = None
    eway_bill_no: Optional[str] = None
    eway_bill_date: Optional[str] = None
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None            # populated by joined queries only
    purchase_invoice_number: Optional[str] = None    # populated by joined queries only
    seller_name: Optional[str] = None                # populated by joined queries only

    @staticmethod
    def from_row(row) -> "JobOut":
        return JobOut(
            id=row["id"],
            company_id=row["company_id"],
            purchase_invoice_id=row["purchase_invoice_id"],
            delivery_challan_no=row["delivery_challan_no"],
            delivery_challan_date=row["delivery_challan_date"],
            dispatch_from_company=bool(row["dispatch_from_company"]),
            transporter_name=row["transporter_name"] if "transporter_name" in row.keys() else None,
            transport_gstin=row["transport_gstin"],
            lr_no=row["lr_no"],
            vehicle_no=row["vehicle_no"],
            eway_bill_no=row["eway_bill_no"],
            eway_bill_date=row["eway_bill_date"],
            remarks=row["remarks"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            purchase_invoice_number=(
                row["purchase_invoice_number"] if "purchase_invoice_number" in row.keys() else None
            ),
            seller_name=row["seller_name"] if "seller_name" in row.keys() else None,
        )


@dataclass
class JobInItem:
    """One DESIGN received back on a job in. `product_id`/`product_name` name
    the jobbed product (the job work's to_product - what the challan's single
    Description column reads), and `design_id` is what stock is keyed on: a
    row without one still prints but never moves stock, the same rule
    PackingListItem follows.

    `quantity_value` is the Alt Qty column, computed server-side as
    quantity_boxes x products.alternate_quantity and persisted (same
    treatment PurchaseInvoiceItem.total_inr gets) rather than recomputed at
    render time, so a printed sheet can't disagree with what was saved."""
    id: Optional[int]
    job_in_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    hsn_code: Optional[str] = None
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    quantity_boxes: float = 0
    quantity_unit: str = "BOX"   # the boxes' unit (products.quantity_unit)
    quantity_value: float = 0    # Alt Qty
    unit: str = "SQM"            # Alt Qty's unit (products.alternate_quantity_unit)

    @staticmethod
    def from_row(row) -> "JobInItem":
        return JobInItem(
            id=row["id"],
            job_in_id=row["job_in_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            hsn_code=row["hsn_code"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"],
            quantity_value=row["quantity_value"],
            unit=row["unit"],
        )


@dataclass
class JobIn:
    """The JOB IN sheet - "JOBWORK INWARD CHALLAN / RETURN", raised against
    ONE job out when jobbed goods come back from the manufacturer. The mirror
    of JobOut, with one deliberate difference: a job in DOES carry its own
    line items. What actually came back is typed at the door and this is the
    only record of it - there is no upstream document to derive it from the
    way a job out derives its whole sheet off its purchase invoice.

    Those per-design quantities are what ADDS stock for the jobbed product,
    completing the cycle a job out starts by deducting the master's designs.
    A job out can have several job ins (goods return in lots), so stock
    accrues per job in.

    Everything else the sheet prints - our own receiver block, the Job
    Manufacturer (Sender), our own DC number/date and the purchase invoice
    reference - is read live off job_out_id at render time; see
    JobInService.build_sheet."""
    id: Optional[int]
    company_id: int
    job_out_id: int
    stock_inward_no: str
    stock_inward_date: str
    created_by: int
    # The job manufacturer's OWN challan for the return leg - their
    # paperwork, not ours (ours is the job out's delivery_challan_no).
    jw_delivery_challan_no: Optional[str] = None
    jw_delivery_challan_date: Optional[str] = None
    transporter_name: Optional[str] = None
    transport_gstin: Optional[str] = None
    lr_no: Optional[str] = None
    vehicle_no: Optional[str] = None
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None        # populated by joined queries only
    delivery_challan_no: Optional[str] = None    # populated by joined queries only (the job out's)
    items: List[JobInItem] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "JobIn":
        return JobIn(
            id=row["id"],
            company_id=row["company_id"],
            job_out_id=row["job_out_id"],
            stock_inward_no=row["stock_inward_no"],
            stock_inward_date=row["stock_inward_date"],
            jw_delivery_challan_no=row["jw_delivery_challan_no"],
            jw_delivery_challan_date=row["jw_delivery_challan_date"],
            transporter_name=row["transporter_name"],
            transport_gstin=row["transport_gstin"],
            lr_no=row["lr_no"],
            vehicle_no=row["vehicle_no"],
            remarks=row["remarks"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            delivery_challan_no=row["delivery_challan_no"] if "delivery_challan_no" in row.keys() else None,
        )

    @property
    def total_boxes(self) -> float:
        return sum(item.quantity_boxes or 0 for item in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(item.quantity_value or 0 for item in self.items)


@dataclass
class PackingListItem:
    """One design of a product packed in a given quantity. product_name and
    design_name are stored snapshots - product_id/design_id are reference
    only, same as QuotationItem.product_id."""
    id: Optional[int]
    packing_list_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    hsn_code: Optional[str] = None
    box_per_pallet: Optional[float] = None
    pallets: Optional[float] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    pcs: Optional[float] = None
    quantity_value: float = 0
    unit: str = "SQM"
    net_weight_kg: Optional[float] = None
    gross_weight_kg: Optional[float] = None

    @staticmethod
    def from_row(row) -> "PackingListItem":
        return PackingListItem(
            id=row["id"],
            packing_list_id=row["packing_list_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            hsn_code=row["hsn_code"],
            box_per_pallet=row["box_per_pallet"],
            pallets=row["pallets"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            pcs=row["pcs"],
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            net_weight_kg=row["net_weight_kg"],
            gross_weight_kg=row["gross_weight_kg"],
        )


@dataclass
class PackingList:
    id: Optional[int]
    company_id: int
    packing_list_number: str
    packing_list_date: str
    consignee_name: str
    created_by: int
    proforma_invoice_id: Optional[int] = None
    quotation_id: Optional[int] = None
    purchase_order_id: Optional[int] = None
    purchase_invoice_id: Optional[int] = None
    job_work_id: Optional[int] = None
    export_ref_no: Optional[str] = None
    buyer_order_no: Optional[str] = None
    other_reference: Optional[str] = None
    consignee_address: Optional[str] = None
    notify_name: Optional[str] = None
    notify_address: Optional[str] = None
    country_of_origin: Optional[str] = "INDIA"
    country_of_destination: Optional[str] = None
    vessel_flight: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    final_destination: Optional[str] = None
    container_details: Optional[str] = None
    terms_of_delivery: Optional[str] = None
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    proforma_invoice_number: Optional[str] = None  # populated by joined queries only
    quotation_number: Optional[str] = None  # populated by joined queries only
    purchase_order_number: Optional[str] = None  # populated by joined queries only
    purchase_invoice_number: Optional[str] = None  # populated by joined queries only
    job_work_number: Optional[str] = None  # populated by joined queries only
    items: List[PackingListItem] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "PackingList":
        return PackingList(
            id=row["id"],
            company_id=row["company_id"],
            packing_list_number=row["packing_list_number"],
            packing_list_date=row["packing_list_date"],
            proforma_invoice_id=row["proforma_invoice_id"],
            quotation_id=row["quotation_id"] if "quotation_id" in row.keys() else None,
            purchase_order_id=row["purchase_order_id"] if "purchase_order_id" in row.keys() else None,
            purchase_invoice_id=row["purchase_invoice_id"] if "purchase_invoice_id" in row.keys() else None,
            job_work_id=row["job_work_id"] if "job_work_id" in row.keys() else None,
            export_ref_no=row["export_ref_no"],
            buyer_order_no=row["buyer_order_no"],
            other_reference=row["other_reference"],
            consignee_name=row["consignee_name"],
            consignee_address=row["consignee_address"],
            notify_name=row["notify_name"],
            notify_address=row["notify_address"],
            country_of_origin=row["country_of_origin"],
            country_of_destination=row["country_of_destination"],
            vessel_flight=row["vessel_flight"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            final_destination=row["final_destination"],
            container_details=row["container_details"],
            terms_of_delivery=row["terms_of_delivery"],
            remarks=row["remarks"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            proforma_invoice_number=row["proforma_invoice_number"] if "proforma_invoice_number" in row.keys() else None,
            quotation_number=row["quotation_number"] if "quotation_number" in row.keys() else None,
            purchase_order_number=row["purchase_order_number"] if "purchase_order_number" in row.keys() else None,
            purchase_invoice_number=row["purchase_invoice_number"] if "purchase_invoice_number" in row.keys() else None,
            job_work_number=row["job_work_number"] if "job_work_number" in row.keys() else None,
        )

    @property
    def total_pallets(self) -> float:
        return sum(item.pallets or 0 for item in self.items)

    @property
    def total_boxes(self) -> float:
        return sum(item.quantity_boxes or 0 for item in self.items)

    @property
    def total_pcs(self) -> float:
        return sum(item.pcs or 0 for item in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(item.quantity_value or 0 for item in self.items)

    @property
    def total_net_weight_kg(self) -> float:
        return sum(item.net_weight_kg or 0 for item in self.items)

    @property
    def total_gross_weight_kg(self) -> float:
        return sum(item.gross_weight_kg or 0 for item in self.items)


# A proforma invoice is a draft until it is explicitly confirmed. Confirming
# it freezes the document (only an admin can move it back to draft) and turns
# on the "purchase orders still to be placed" reminder, which stays up until
# every design on the PI's packing list has been placed, in full, on the
# packing list of some purchase order linked to that PI.
PROFORMA_STATUS_DRAFT = "draft"
PROFORMA_STATUS_CONFIRMED = "confirmed"
PROFORMA_STATUSES = [
    (PROFORMA_STATUS_DRAFT, "Draft"),
    (PROFORMA_STATUS_CONFIRMED, "Confirmed"),
]


@dataclass
class ProformaInvoiceItem:
    id: Optional[int]
    proforma_invoice_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    dimension_mm: Optional[str] = None
    hsn_code: Optional[str] = None
    surface: Optional[str] = None  # optional finish (GLOSSY / MATT / ...), drives the surface-grouped print view
    pallets: Optional[float] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    quantity_value: float = 0
    unit: str = "SQM"
    price_usd: float = 0
    total_usd: float = 0
    # Unused (kept so an old row still loads) - proforma invoices no longer
    # have an FOB-typed-price mode; price_usd is always the absolute price
    # the user typed. See ExportInvoiceItem.fob_price_usd, which has this now.
    fob_price_usd: Optional[float] = None

    @staticmethod
    def from_row(row) -> "ProformaInvoiceItem":
        return ProformaInvoiceItem(
            id=row["id"],
            proforma_invoice_id=row["proforma_invoice_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            dimension_mm=row["dimension_mm"],
            hsn_code=row["hsn_code"],
            surface=row["surface"] if "surface" in row.keys() else None,
            pallets=row["pallets"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_usd=row["price_usd"],
            total_usd=row["total_usd"],
            fob_price_usd=row["fob_price_usd"] if "fob_price_usd" in row.keys() else None,
        )


@dataclass
class ProformaInvoice(CifMoneyLadder):
    id: Optional[int]
    company_id: int
    invoice_number: str
    invoice_date: str
    consignee_name: str
    created_by: int
    quotation_id: Optional[int] = None
    export_ref_no: Optional[str] = None
    buyer_order_no: Optional[str] = None
    other_reference: Optional[str] = None
    consignee_address: Optional[str] = None
    notify_name: Optional[str] = None
    notify_address: Optional[str] = None
    country_of_origin: Optional[str] = "INDIA"
    country_of_destination: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    final_destination: Optional[str] = None
    transhipment: Optional[str] = None
    partial_shipment: Optional[str] = None
    variation_in_qty: Optional[str] = None
    delivery_period: Optional[str] = None
    packing_details: Optional[str] = None  # e.g. "PALLATE" - same field as Quotation.packing_details
    terms_of_delivery: Optional[str] = None
    payment_terms: Optional[str] = None
    remarks: Optional[str] = None
    sea_freight: float = 0
    insurance: float = 0
    certification: float = 0
    other_charges: float = 0
    discount_amount: float = 0
    # Both unused (kept so an old row still loads): like the quotation, a
    # proforma invoice no longer has an FOB-typed-price mode - price_usd is
    # always the absolute FOB price the user typed. See cif_value_usd below.
    fob_pricing: bool = False  # see Quotation.fob_pricing
    round_off: float = 0       # see Quotation.round_off
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    bank_branch: Optional[str] = None
    bank_address: Optional[str] = None
    display_mode: str = "index"  # goods layout: 'index' (numbered) | 'surface' (grouped by category + surface)
    status: str = PROFORMA_STATUS_DRAFT  # 'draft' | 'confirmed' - see PROFORMA_STATUSES
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    # Currency the document is written in, picked from the Administration ->
    # Miscellaneous list and snapshotted so a later edit of that list can't
    # rewrite an issued sheet. Display information only - no conversion.
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    items: List[ProformaInvoiceItem] = field(default_factory=list)
    computed_subtotal_usd: Optional[float] = None  # precomputed by list queries that don't load items
    # Container type/count list, e.g. "2 x 20FT FCL" - same shape as
    # Quotation.containers. Loaded/replaced by ProformaInvoiceRepository, not
    # a plain column - see container_details below for the printed text
    # built from it.
    containers: List[dict] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "ProformaInvoice":
        return ProformaInvoice(
            id=row["id"],
            company_id=row["company_id"],
            invoice_number=row["invoice_number"],
            invoice_date=row["invoice_date"],
            quotation_id=row["quotation_id"],
            export_ref_no=row["export_ref_no"],
            buyer_order_no=row["buyer_order_no"],
            other_reference=row["other_reference"],
            consignee_name=row["consignee_name"],
            consignee_address=row["consignee_address"],
            notify_name=row["notify_name"],
            notify_address=row["notify_address"],
            country_of_origin=row["country_of_origin"],
            country_of_destination=row["country_of_destination"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            final_destination=row["final_destination"],
            transhipment=row["transhipment"],
            partial_shipment=row["partial_shipment"],
            variation_in_qty=row["variation_in_qty"],
            delivery_period=row["delivery_period"],
            packing_details=row["packing_details"] if "packing_details" in row.keys() else None,
            terms_of_delivery=row["terms_of_delivery"],
            payment_terms=row["payment_terms"],
            remarks=row["remarks"],
            sea_freight=row["sea_freight"],
            insurance=row["insurance"],
            certification=row["certification"],
            other_charges=row["other_charges"],
            discount_amount=row["discount_amount"],
            fob_pricing=bool(row["fob_pricing"]) if "fob_pricing" in row.keys() else False,
            round_off=(row["round_off"] if "round_off" in row.keys() else 0) or 0,
            bank_name=row["bank_name"],
            bank_account_number=row["bank_account_number"],
            bank_ifsc_code=row["bank_ifsc_code"],
            bank_swift_code=row["bank_swift_code"],
            bank_branch=row["bank_branch"],
            bank_address=row["bank_address"],
            display_mode=(row["display_mode"] if "display_mode" in row.keys() else None) or "index",
            status=(row["status"] if "status" in row.keys() else None) or PROFORMA_STATUS_DRAFT,
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
            computed_subtotal_usd=row["items_total"] if "items_total" in row.keys() else None,
            currency_code=row["currency_code"] if "currency_code" in row.keys() else None,
            currency_symbol=row["currency_symbol"] if "currency_symbol" in row.keys() else None,
        )

    @property
    def container_details(self) -> Optional[str]:
        """The printed Container Details text, e.g. "2 x 20FT FCL" - one row
        per line, same "COUNT x TYPE" format Quotation.container_details and
        the Export Invoice's own Container Details cell use. Read-only:
        `containers` (loaded by ProformaInvoiceRepository) is the stored
        data, this just formats it."""
        if not self.containers:
            return None
        return "\n".join(f"{c['container_count']} x {c['container_type']}" for c in self.containers)

    @property
    def currency_name(self) -> str:
        """The currency's name, e.g. `USD` - what the money column headings
        and typed-amount labels read."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[0]

    @property
    def currency_prefix(self) -> str:
        """The symbol printed in front of an amount, e.g. `$`."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[1]

    @property
    def currency_label(self) -> str:
        """The Currency cell on a printed sheet, e.g. `USD [ $ ]`."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[2]

    @property
    def is_confirmed(self) -> bool:
        return self.status == PROFORMA_STATUS_CONFIRMED

    @property
    def status_label(self) -> str:
        return dict(PROFORMA_STATUSES).get(self.status, self.status)

    @property
    def subtotal_usd(self) -> float:
        if self.computed_subtotal_usd is not None:
            return self.computed_subtotal_usd
        return sum(item.total_usd for item in self.items)

    @property
    def cif_value_usd(self) -> float:
        """Overrides CifMoneyLadder.cif_value_usd - like Quotation, a proforma
        invoice's typed price is always the absolute FOB price (fob_pricing is
        hardcoded off - see _build_header - and nothing ever adjusts it),
        so CIF is built UPWARDS from FOB by adding the charges rather
        than being the goods total on its own. This holds regardless of the
        shipping terms chosen - the terms only decide which charge fields are
        non-zero (see drops_sea_freight/drops_insurance), never the FOB total
        itself. Mirrors Quotation.cif_value_usd; a proforma invoice has no
        cif_adjust_usd equivalent (the printed/typed CIF figure is never
        manually overridden here)."""
        return self.subtotal_usd + self.charges_total

    @property
    def fob_value_usd(self) -> float:
        """Overrides CifMoneyLadder.fob_value_usd - the FOB value is simply
        the goods total (quantity x price, summed across every line), never
        reduced by the discount or rebuilt from the invoice value. Mirrors
        Quotation.fob_value_usd; see cif_value_usd above for why a proforma
        invoice's ladder runs upward like a quotation's rather than downward
        like the base CifMoneyLadder."""
        return self.subtotal_usd

    # ---- CIF-priced view of the goods lines -----------------------------
    # The rate typed on the form is the FOB rate, but the rate the buyer reads
    # on the sheet is the CIF rate: the charges between FOB and CIF are spread
    # uniformly over the total ALT QTY and that per-unit share is added to
    # every line. ExportInvoice.printed_items does the same thing, gated on
    # the delivery terms - keep the two in step.
    @property
    def charge_uplift_per_unit(self) -> float:
        """One unit of ALT QTY's share of the FOB->CIF charges, rounded to the
        two decimals a printed rate has room for - the closest printable
        figure, not the exact share. What that rounding leaves over is
        absorbed by the last printed line's Total; see printed_items."""
        total_qty = sum(item.quantity_value or 0 for item in self.items)
        return round(self.charges_total / total_qty, 2) if total_qty else 0.0

    @property
    def printed_items(self) -> List[ProformaInvoiceItem]:
        """The goods lines as the sheet and the annexure print them: same
        lines, but at the CIF rate, each line's Total worked out FROM that
        printed rate. Copies - the stored items keep the FOB rate that was
        typed.

        A per-unit uplift rounded to the cent can't land the column exactly on
        FOB + charges, and the few cents left over are absorbed into the LAST
        line's Total rather than printed as a round-off row of their own: this
        document is read by customs, which has no round-off line to accept,
        while the FOB value it is all built up from is the figure the buyer
        and the seller actually agreed. So the goods column always foots to
        the CIF value exactly, and the ladder below it reconciles all the way
        down to the agreed FOB value with no extra step to explain."""
        uplift = self.charge_uplift_per_unit
        printed = []
        for item in self.items:
            rate = round((item.price_usd or 0) + uplift, 2)
            printed.append(replace(
                item, price_usd=rate, total_usd=round(rate * (item.quantity_value or 0), 2),
            ))
        if printed:
            leftover = round(self.cif_value_usd - sum(i.total_usd for i in printed), 2)
            if leftover:
                last = printed[-1]
                printed[-1] = replace(last, total_usd=round(last.total_usd + leftover, 2))
        return printed

    @property
    def printed_goods_total(self) -> float:
        """What the printed goods column adds up to - the CIF value."""
        return round(sum(item.total_usd for item in self.printed_items), 2)


EXPORT_TAX_MODE_IGST = "igst"
EXPORT_TAX_MODE_LUT = "lut"
EXPORT_TAX_MODES = [
    (EXPORT_TAX_MODE_IGST, "With Payment of IGST"),
    (EXPORT_TAX_MODE_LUT, "Without Payment of IGST under LUT"),
]
EXPORT_LOADING_BUFFER = "buffer"
EXPORT_LOADING_SELF_SEALING = "self_sealing"
EXPORT_LOADING_TYPES = [(EXPORT_LOADING_BUFFER, "Buffer loading"), (EXPORT_LOADING_SELF_SEALING, "Self-sealing")]


@dataclass
class ExportInvoiceItem:
    """One goods line on an Export Invoice - same shape as
    ProformaInvoiceItem, plus a per-line igst_percent snapshot so the summed
    tax is computed per-product (each HSN taxes differently) and stays stable
    against later catalog edits."""
    id: Optional[int]
    export_invoice_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    dimension_mm: Optional[str] = None
    hsn_code: Optional[str] = None
    surface: Optional[str] = None
    pallets: Optional[float] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    quantity_value: float = 0
    unit: str = "SQM"
    price_usd: float = 0
    total_usd: float = 0
    igst_percent: float = 0
    # Unused (kept so an old row still loads) - export invoices no longer have
    # an FOB-typed-price mode; price_usd is always the absolute FOB price the
    # user typed. See ExportInvoice.cif_value_usd, which builds CIF up from it.
    fob_price_usd: Optional[float] = None
    # The weight_kg of whichever named pallet type is currently selected on
    # this line (product_pallet_types.weight_kg), snapshotted the moment it's
    # picked - None for Loose/Manual/no product. Feeds the Export Packing
    # List's container-split Gross (KG) = Net (KG) + Plts x this; see
    # ExportPackingListService.build_items.
    pallet_weight_kg: Optional[float] = None

    @property
    def tax_usd(self) -> float:
        return (self.total_usd or 0) * (self.igst_percent or 0) / 100.0

    @staticmethod
    def from_row(row) -> "ExportInvoiceItem":
        return ExportInvoiceItem(
            id=row["id"],
            export_invoice_id=row["export_invoice_id"],
            sr_no=row["sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            dimension_mm=row["dimension_mm"],
            hsn_code=row["hsn_code"],
            surface=row["surface"] if "surface" in row.keys() else None,
            pallets=row["pallets"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"] if "quantity_unit" in row.keys() else "PCS",
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_usd=row["price_usd"],
            total_usd=row["total_usd"],
            igst_percent=row["igst_percent"] if "igst_percent" in row.keys() else 0,
            fob_price_usd=row["fob_price_usd"] if "fob_price_usd" in row.keys() else None,
            pallet_weight_kg=row["pallet_weight_kg"] if "pallet_weight_kg" in row.keys() else None,
        )


@dataclass
class ExportInvoice(CifMoneyLadder):
    """The customer/customs-facing Export Invoice at the buyer end of the
    pipeline. References one or more Proforma Invoices (many-to-many via
    proforma_invoice_ids). Goods are prefilled from those PIs then edited.
    Tax is computed per-product and, per tax_mode ("Supply meant for"), either
    charged as IGST or zero-rated ("Without Payment of IGST under LUT");
    the exchange rate is manual and admin-locked once set. Buyer Order No &
    Date is a single field shared by every linked PI. The several child
    lists (containers / container_details / purchase_details) back the
    front-page and page-2 annexure blocks."""
    id: Optional[int]
    company_id: int
    export_invoice_number: str
    invoice_date: str
    consignee_name: str
    created_by: int
    lead_id: Optional[int] = None
    consignee_address: Optional[str] = None
    notify_name: Optional[str] = None
    notify_address: Optional[str] = None
    country_of_origin: Optional[str] = "INDIA"
    country_of_destination: Optional[str] = None
    place_of_receipt: Optional[str] = None
    pre_carriage_by: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    final_destination: Optional[str] = None
    nature_of_contract: Optional[str] = None
    payment_terms: Optional[str] = None
    buyer_order_no: Optional[str] = None
    buyer_order_date: Optional[str] = None
    # Only the government-scheme LINE of the printed "Export Under" block -
    # blank means "use OurCompany.government_schemes as it stands today". The
    # block's other lines (the SUPPLY MEANT FOR EXPORT heading from tax_mode,
    # the EPCG licence below, the company's LUT number) are composed by the
    # sheets at print time, so they can never go stale.
    export_under: Optional[str] = None
    epcg_number: Optional[str] = None
    epcg_date: Optional[str] = None
    loading_type: str = EXPORT_LOADING_SELF_SEALING
    tax_mode: str = EXPORT_TAX_MODE_IGST
    exchange_rate: float = 0
    sea_freight: float = 0
    insurance: float = 0
    certification: float = 0
    other_charges: float = 0
    discount_amount: float = 0
    # Unused (kept so an old row still loads) - always written False by
    # ExportInvoiceService._build_header, same as the quotation and proforma
    # invoice builders. The typed price is always the absolute FOB price.
    fob_pricing: bool = False
    round_off: float = 0       # see Quotation.round_off
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    bank_branch: Optional[str] = None
    bank_address: Optional[str] = None
    authorised_person_name: Optional[str] = None
    authorised_person_designation: Optional[str] = None
    self_sealing_declaration: Optional[str] = None
    shipping_bill_pdf_path: Optional[str] = None
    examination_date: Optional[str] = None
    location_code_08b: Optional[str] = None
    booking_no: Optional[str] = None
    # The Booking Detail the 11B rows were copied from. booking_no is what
    # prints; this is the durable link, so the invoice still says WHICH
    # booking it was built from after a renumber. The rows stay a snapshot.
    booking_detail_id: Optional[int] = None
    vessel_name: Optional[str] = None  # vessel or flight name
    voyage_no: Optional[str] = None
    # Both print together in the "Vessel / Flight Name & No" cell of both
    # sheets - see ExportInvoice.vessel_voyage_no below.
    eway_bill_no: Optional[str] = None  # printed on the Tax Invoice attachment only
    eway_bill_date: Optional[str] = None
    # The Tax Invoice attachment's own number/date. Blank means "the export
    # invoice's own" - see tax_invoice_number_printed / _date_printed below.
    tax_invoice_number: Optional[str] = None
    tax_invoice_date: Optional[str] = None
    # The VGM declaration's manual-entry cells. Blank means "use the default"
    # - see the vgm_*_printed properties below.
    vgm_signatory: Optional[str] = None
    vgm_contact_24x7: Optional[str] = None
    vgm_weighing_method: Optional[str] = None
    vgm_cargo_type: Optional[str] = None
    vgm_hazardous_details: Optional[str] = None
    # The commercial invoice packing list's typed cells.
    bill_of_lading_no: Optional[str] = None
    bill_of_lading_date: Optional[str] = None
    bill_of_lading_pdf_path: Optional[str] = None
    issuing_authority: Optional[str] = None
    issuing_authority_address: Optional[str] = None
    permission_no: Optional[str] = None
    permission_date: Optional[str] = None
    permission_expiry: Optional[str] = None
    permission_is_one_time: bool = False  # printed as "One Time" instead of the (blank) expiry date
    manufacturer_name: Optional[str] = None
    manufacturer_address: Optional[str] = None
    stuffing_location: Optional[str] = None  # "Stuff At" address, printed on the export packing list
    remarks: Optional[str] = None
    total_net_weight_kg: Optional[float] = None  # front-page weight totals, typed not summed from containers
    total_gross_weight_kg: Optional[float] = None
    shipping_bill_no: Optional[str] = None
    shipping_bill_date: Optional[str] = None  # Annexure-C header: Shipping Bill Date
    # Currency printed on the invoice + packing list, picked from the
    # Administration -> Miscellaneous currency list and snapshotted here so a
    # later edit of that list can't rewrite an already-printed sheet.
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    status: str = "active"  # no draft/confirmed lock; kept for interface symmetry with other documents
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    items: List[ExportInvoiceItem] = field(default_factory=list)
    proforma_invoice_ids: List[int] = field(default_factory=list)
    containers: List[dict] = field(default_factory=list)  # [{container_type, container_count}]
    container_details: List[dict] = field(default_factory=list)  # [{container_type, container_no, line_seal_no, rfid_seal_no, vehicle_no, lr_no, transporter_name, max_permitted_weight, tare_weight_kg, gross_weight, net_weight}]
    purchase_details: List[dict] = field(default_factory=list)  # [{supplier_gstin, supplier_invoice_no, supplier_name, purchase_type, epcg_number, epcg_date}]
    product_sources: List[dict] = field(default_factory=list)  # [{product_name, po_number, quantity_boxes}] - which PO(s) each goods line's boxes came from
    job_ins: List[dict] = field(default_factory=list)  # [{manufacturer_name, manufacturer_gstin, job_out_challan_no, jw_challan_no, jw_challan_date, stock_inward_no, stock_inward_date}] - job ins whose returned goods were merged into the Products card; display only, never printed
    linked_proformas: List[dict] = field(default_factory=list)  # [{id, invoice_number, invoice_date}] joined for display
    computed_subtotal_usd: Optional[float] = None  # precomputed by list queries that don't load items

    @staticmethod
    def from_row(row) -> "ExportInvoice":
        keys = row.keys()

        def g(name, default=None):
            return row[name] if name in keys else default

        return ExportInvoice(
            id=row["id"],
            company_id=row["company_id"],
            export_invoice_number=row["export_invoice_number"],
            invoice_date=row["invoice_date"],
            lead_id=g("lead_id"),
            consignee_name=row["consignee_name"],
            consignee_address=g("consignee_address"),
            notify_name=g("notify_name"),
            notify_address=g("notify_address"),
            country_of_origin=g("country_of_origin"),
            country_of_destination=g("country_of_destination"),
            place_of_receipt=g("place_of_receipt"),
            pre_carriage_by=g("pre_carriage_by"),
            port_of_loading=g("port_of_loading"),
            port_of_discharge=g("port_of_discharge"),
            final_destination=g("final_destination"),
            nature_of_contract=g("nature_of_contract"),
            payment_terms=g("payment_terms"),
            buyer_order_no=g("buyer_order_no"),
            buyer_order_date=g("buyer_order_date"),
            export_under=g("export_under"),
            epcg_number=g("epcg_number"),
            epcg_date=g("epcg_date"),
            loading_type=g("loading_type") or EXPORT_LOADING_SELF_SEALING,
            tax_mode=g("tax_mode") or EXPORT_TAX_MODE_IGST,
            exchange_rate=g("exchange_rate", 0) or 0,
            sea_freight=g("sea_freight", 0) or 0,
            insurance=g("insurance", 0) or 0,
            certification=g("certification", 0) or 0,
            other_charges=g("other_charges", 0) or 0,
            discount_amount=g("discount_amount", 0) or 0,
            fob_pricing=bool(g("fob_pricing", 0)),
            round_off=g("round_off", 0) or 0,
            bank_name=g("bank_name"),
            bank_account_number=g("bank_account_number"),
            bank_ifsc_code=g("bank_ifsc_code"),
            bank_swift_code=g("bank_swift_code"),
            bank_branch=g("bank_branch"),
            bank_address=g("bank_address"),
            authorised_person_name=g("authorised_person_name"),
            authorised_person_designation=g("authorised_person_designation"),
            self_sealing_declaration=g("self_sealing_declaration"),
            shipping_bill_pdf_path=g("shipping_bill_pdf_path"),
            examination_date=g("examination_date"),
            location_code_08b=g("location_code_08b"),
            booking_no=g("booking_no"),
            booking_detail_id=g("booking_detail_id"),
            vessel_name=g("vessel_name"),
            voyage_no=g("voyage_no"),
            eway_bill_no=g("eway_bill_no"),
            eway_bill_date=g("eway_bill_date"),
            tax_invoice_number=g("tax_invoice_number"),
            tax_invoice_date=g("tax_invoice_date"),
            vgm_signatory=g("vgm_signatory"),
            vgm_contact_24x7=g("vgm_contact_24x7"),
            vgm_weighing_method=g("vgm_weighing_method"),
            vgm_cargo_type=g("vgm_cargo_type"),
            vgm_hazardous_details=g("vgm_hazardous_details"),
            bill_of_lading_no=g("bill_of_lading_no"),
            bill_of_lading_date=g("bill_of_lading_date"),
            bill_of_lading_pdf_path=g("bill_of_lading_pdf_path"),
            issuing_authority=g("issuing_authority"),
            issuing_authority_address=g("issuing_authority_address"),
            permission_no=g("permission_no"),
            permission_date=g("permission_date"),
            permission_expiry=g("permission_expiry"),
            permission_is_one_time=bool(g("permission_is_one_time", 0)),
            manufacturer_name=g("manufacturer_name"),
            manufacturer_address=g("manufacturer_address"),
            stuffing_location=g("stuffing_location"),
            remarks=g("remarks"),
            total_net_weight_kg=g("total_net_weight_kg"),
            total_gross_weight_kg=g("total_gross_weight_kg"),
            shipping_bill_no=g("shipping_bill_no"),
            shipping_bill_date=g("shipping_bill_date"),
            currency_code=g("currency_code"),
            currency_symbol=g("currency_symbol"),
            created_by=row["created_by"],
            created_at=g("created_at"),
            updated_at=g("updated_at"),
            created_by_name=g("created_by_name"),
            computed_subtotal_usd=row["items_total"] if "items_total" in keys else None,
        )

    @property
    def currency_name(self) -> str:
        """The currency's name, e.g. `USD` - what the money column headings
        and typed-amount labels read."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[0]

    @property
    def currency_prefix(self) -> str:
        """The symbol printed in front of an amount, e.g. `$`."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[1]

    @property
    def currency_label(self) -> str:
        """The Currency cell on a printed sheet, e.g. `USD [ $ ]`."""
        return currency_display(self.currency_code, self.currency_symbol, "USD", "$")[2]

    @property
    def subtotal_usd(self) -> float:
        if self.computed_subtotal_usd is not None:
            return self.computed_subtotal_usd
        return sum(item.total_usd for item in self.items)

    @property
    def cif_value_usd(self) -> float:
        """Overrides CifMoneyLadder.cif_value_usd - an export invoice's typed
        price is always the absolute FOB price, so CIF is built UPWARDS from
        the goods total by adding the charges rather than being the goods
        total on its own. This holds regardless of the nature of contract -
        the terms only decide which charge fields are non-zero (see
        drops_sea_freight/drops_insurance), never the FOB total itself.
        Mirrors Quotation.cif_value_usd / ProformaInvoice.cif_value_usd; the
        round_off is carried through from the base ladder because unlike
        those two this document has a real stored round_off field (it is 0 on
        every invoice in practice)."""
        return self.subtotal_usd + self.charges_total + self.round_off

    @property
    def fob_value_usd(self) -> float:
        """Overrides CifMoneyLadder.fob_value_usd - the FOB value is simply
        the goods total (quantity x the typed price, summed across every
        line), never reduced by the discount or rebuilt from the invoice
        value. Mirrors Quotation.fob_value_usd/ProformaInvoice.fob_value_usd;
        see cif_value_usd above for why this document's ladder runs upward
        like theirs rather than downward like the base CifMoneyLadder."""
        return self.subtotal_usd

    # ---- CIF/CFR-priced view of the goods lines -------------------------
    # The rate typed on the form is the FOB rate, but under CIF/CFR terms the
    # rate the buyer reads on the sheet is the all-in one: the ocean leg is
    # part of what they pay per unit, so the charges are spread uniformly over
    # the total ALT QTY and that per-unit share is added to every line. Under
    # FOB terms there is no such figure to build - the buyer carries the ocean
    # leg themselves - so the typed rate prints through untouched.
    # The same mechanism ProformaInvoice uses; kept deliberately identical in
    # shape (see ProformaInvoice.printed_items) so the two can't drift apart.
    @property
    def charge_uplift_per_unit(self) -> float:
        """One unit of ALT QTY's share of the FOB->CIF charges, rounded to the
        two decimals a printed rate has room for - the closest printable
        figure, not the exact share. What that rounding leaves over is
        absorbed by the last printed line's Total; see printed_items.

        Zero under FOB terms. The question asked is `is_fob_terms`, not "is it
        CIF or CFR": the terms are hand-maintained free text (CIF, CFR, CNF,
        or nothing at all), and this has to stay in lockstep with the printed
        sheet, which shows its CIF/CFR VALUE row on exactly the same
        condition - anything that isn't FOB."""
        if is_fob_terms(self.nature_of_contract):
            return 0.0
        total_qty = sum(item.quantity_value or 0 for item in self.items)
        return round(self.charges_total / total_qty, 2) if total_qty else 0.0

    @property
    def printed_items(self) -> List[ExportInvoiceItem]:
        """The goods lines as every sheet prints them: same lines, but at the
        CIF/CFR rate, each line's Total worked out FROM that printed rate.
        Copies - the stored items keep the FOB rate that was typed.

        A per-unit uplift rounded to the cent can't land the column exactly on
        FOB + charges, and the few cents left over are absorbed into the LAST
        line's Total rather than printed as a round-off row of their own: this
        document is read by customs, which has no round-off line to accept,
        while the FOB value it is all built up from is the figure the buyer
        and the seller actually agreed. So the goods column always foots to
        the CIF value exactly, and the ladder below it reconciles all the way
        down to the agreed FOB value with no extra step to explain.

        With no uplift to apply (FOB terms, or no charges at all) the stored
        items are handed back as they are - not re-rounded - so those sheets
        print exactly what they always did."""
        uplift = self.charge_uplift_per_unit
        if not uplift:
            return list(self.items)
        printed = []
        for item in self.items:
            rate = round((item.price_usd or 0) + uplift, 2)
            printed.append(replace(
                item, price_usd=rate, total_usd=round(rate * (item.quantity_value or 0), 2),
            ))
        leftover = round(self.cif_value_usd - sum(i.total_usd for i in printed), 2)
        if leftover:
            last = printed[-1]
            printed[-1] = replace(last, total_usd=round(last.total_usd + leftover, 2))
        return printed

    @property
    def charge_uplift_per_unit_precise(self) -> float:
        """Same as charge_uplift_per_unit, but rounded to 5 decimal places
        instead of 2 - only for printed_items_precise below, which feeds
        the Rate column on the export invoice's own printed sheet under
        CIF/CFR terms. Every other CIF/CFR-priced sheet (tax invoice, BRC
        commercial invoice) still reads charge_uplift_per_unit/printed_items
        at their original 2-decimal precision, deliberately unchanged."""
        if is_fob_terms(self.nature_of_contract):
            return 0.0
        total_qty = sum(item.quantity_value or 0 for item in self.items)
        return round(self.charges_total / total_qty, 5) if total_qty else 0.0

    @property
    def printed_items_precise(self) -> List[ExportInvoiceItem]:
        """Same as printed_items, but the per-line CIF/CFR rate is rounded to
        5 decimal places rather than 2 (see charge_uplift_per_unit_precise) -
        used only by the export invoice's own printed sheet. Total_usd still
        rounds to 2 decimals (it's money), and the leftover-on-the-last-line
        reconciliation is unchanged, so the goods column still foots exactly
        to the CIF/CFR value either way."""
        uplift = self.charge_uplift_per_unit_precise
        if not uplift:
            return list(self.items)
        printed = []
        for item in self.items:
            rate = round((item.price_usd or 0) + uplift, 5)
            printed.append(replace(
                item, price_usd=rate, total_usd=round(rate * (item.quantity_value or 0), 2),
            ))
        leftover = round(self.cif_value_usd - sum(i.total_usd for i in printed), 2)
        if leftover:
            last = printed[-1]
            printed[-1] = replace(last, total_usd=round(last.total_usd + leftover, 2))
        return printed

    @property
    def printed_goods_total(self) -> float:
        """What the printed goods column adds up to - the CIF value."""
        return round(sum(item.total_usd for item in self.printed_items), 2)

    # The two persisted columns export_invoices.cnf_value / .fob_value are
    # written from these on every save (nothing reads them back - they exist so
    # the figures are queryable outside the app). Both are just the ladder in
    # CifMoneyLadder under this document's own names: CNF and CIF are the same
    # value here, and FOB is the ladder's bottom line.
    @property
    def cnf_value(self) -> float:
        """Goods total with the charges added on - see cif_value_usd."""
        return self.cif_value_usd

    @property
    def fob_value(self) -> float:
        """Quantity x the typed FOB price, summed - see fob_value_usd."""
        return self.fob_value_usd

    @property
    def tax_invoice_number_printed(self) -> str:
        """What the Tax Invoice attachment prints in its Invoice No cell. Its
        own number once given one, otherwise this export invoice's - a tax
        invoice starts out numbered exactly as its parent."""
        return self.tax_invoice_number or self.export_invoice_number

    @property
    def tax_invoice_date_printed(self) -> str:
        """As above, for the Invoice Date cell."""
        return self.tax_invoice_date or self.invoice_date

    @property
    def vessel_voyage_no(self) -> Optional[str]:
        """What prints in the "Vessel / Flight Name & No" cell of both the
        export invoice and export packing list sheets - vessel_name and
        voyage_no joined with a slash, same as the single field they used to
        be typed as one. Falls back to whichever half is actually filled in,
        and to None (prints as N/A on the sheets) when both are blank."""
        parts = [p for p in (self.vessel_name, self.voyage_no) if p and p.strip()]
        return " / ".join(parts) if parts else None

    # ---- VGM declaration defaults ---------------------------------------
    # The manual-entry cells are optional; each falls back to what the app
    # already knows (or to the value the reference form carries), so the sheet
    # is complete before anyone opens its edit form.
    VGM_DEFAULT_WEIGHING_METHOD = "METHOD-1"
    VGM_DEFAULT_CARGO_TYPE = "NORMAL"
    VGM_DEFAULT_HAZARDOUS_DETAILS = "N/A"

    def vgm_signatory_printed(self, company=None) -> str:
        """Falls back to this invoice's own authorised signatory, worded the
        way the sheets already word it ('<name> <designation> of <company>')."""
        if self.vgm_signatory:
            return self.vgm_signatory
        if not self.authorised_person_name:
            return "-"
        parts = [self.authorised_person_name]
        if self.authorised_person_designation:
            parts.append(self.authorised_person_designation)
        if company and company.company_name:
            parts.append(f"of {company.company_name.upper()}")
        return " ".join(parts)

    @property
    def vgm_weighing_method_printed(self) -> str:
        return self.vgm_weighing_method or self.VGM_DEFAULT_WEIGHING_METHOD

    @property
    def vgm_cargo_type_printed(self) -> str:
        return self.vgm_cargo_type or self.VGM_DEFAULT_CARGO_TYPE

    @property
    def vgm_hazardous_details_printed(self) -> str:
        return self.vgm_hazardous_details or self.VGM_DEFAULT_HAZARDOUS_DETAILS

    def in_inr(self, value: float) -> float:
        """Any of this invoice's own-currency figures at its own exchange
        rate. The Tax Invoice attachment prints the WHOLE money ladder in INR,
        so the conversion lives here once rather than being re-multiplied per
        row in a template."""
        return (value or 0) * (self.exchange_rate or 0)

    @property
    def invoice_value_inr(self) -> float:
        return self.in_inr(self.invoice_value_usd)

    @property
    def tax_total_inr(self) -> float:
        """Per-product tax, summed. Each line's USD total is converted to INR
        at the invoice's exchange rate then taxed at that product's own IGST
        percentage - so a mixed-HSN invoice totals each line separately.

        Taxed on printed_items, not items: the base is the line total the
        sheet actually shows, so the printed IGST is always that rate applied
        to the printed goods column rather than to a figure that appears
        nowhere on it. Under FOB terms the two are the same list."""
        rate = self.exchange_rate or 0
        return sum((item.total_usd or 0) * rate * (item.igst_percent or 0) / 100.0
                   for item in self.printed_items)

    @property
    def igst_amount_inr(self) -> float:
        """Zero-rated (LUT) supplies carry no IGST - only 'With Payment of
        IGST' actually charges the summed per-product tax."""
        return self.tax_total_inr if self.tax_mode == EXPORT_TAX_MODE_IGST else 0

    @property
    def tax_mode_label(self) -> str:
        return dict(EXPORT_TAX_MODES).get(self.tax_mode, self.tax_mode)

    @property
    def loading_type_label(self) -> str:
        return dict(EXPORT_LOADING_TYPES).get(self.loading_type, self.loading_type)

    @property
    def total_containers(self) -> int:
        return sum(int(c.get("container_count") or 0) for c in self.containers)

    @property
    def container_types_expanded(self) -> List[str]:
        """The booked Container Details list flattened to one TYPE per
        physical container - `2 x 20FT FCL` becomes ['20FT FCL', '20FT FCL'].

        That total is what drives how many section-11B rows the form asks for,
        so this list lines up with `container_details` by position, which is
        how the VGM attachment labels each container's size."""
        types: List[str] = []
        for c in self.containers:
            types += [c.get("container_type") or ""] * int(c.get("container_count") or 0)
        return types

    @property
    def top_costliest_items(self) -> List[ExportInvoiceItem]:
        """Section 09 lists the four costliest product lines (by line total)."""
        return sorted(self.items, key=lambda i: i.total_usd or 0, reverse=True)[:4]


@dataclass
class ExportPackingListItemDesign:
    """One catalog-design slice of an ExportPackingListItem's boxes, entered
    on the "Designs Packing List" page (app/routes/export_designs_packing_lists.py).
    Keyed on (export_packing_list_id, invoice_item_sr_no, container_sr_no) -
    the container split's own natural key - rather than an FK to the parent
    ExportPackingListItem's id, because that row's id is NOT stable: it is
    wholesale deleted and re-inserted every time the parent export invoice is
    saved (ExportPackingListRepository._replace_items). Per line, every
    design row's quantity_boxes must sum to exactly that line's own."""
    id: Optional[int]
    export_packing_list_id: int
    invoice_item_sr_no: int
    container_sr_no: int
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    quantity_boxes: float = 0
    quantity_value: float = 0
    unit: Optional[str] = None

    @staticmethod
    def from_row(row) -> "ExportPackingListItemDesign":
        return ExportPackingListItemDesign(
            id=row["id"],
            export_packing_list_id=row["export_packing_list_id"],
            invoice_item_sr_no=row["invoice_item_sr_no"],
            container_sr_no=row["container_sr_no"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            quantity_boxes=row["quantity_boxes"],
            quantity_value=row["quantity_value"],
            unit=row["unit"],
        )


@dataclass
class ExportPackingListItem:
    """One (container x goods line) allocation on an Export Packing List:
    `quantity_boxes` boxes of the export invoice's goods line
    `invoice_item_sr_no` loaded into the physical container
    `container_sr_no`. Every other quantity column is derived from the boxes
    (see ExportPackingListService), and the container identity is snapshotted
    off the invoice's section-11B row so a later 11B re-order can't silently
    rewrite an already-printed sheet."""
    id: Optional[int]
    export_packing_list_id: Optional[int]
    sr_no: int
    product_name: str
    container_sr_no: int = 1
    container_no: Optional[str] = None
    seal_no: Optional[str] = None
    rfid_seal_no: Optional[str] = None
    invoice_item_sr_no: Optional[int] = None
    product_id: Optional[int] = None
    group_label: Optional[str] = None
    hsn_code: Optional[str] = None
    pallets: Optional[float] = None
    quantity_boxes: Optional[float] = None
    quantity_unit: str = "PCS"
    quantity_value: float = 0
    unit: str = "SQM"
    net_weight_kg: Optional[float] = None
    gross_weight_kg: Optional[float] = None
    designs: List[ExportPackingListItemDesign] = field(default_factory=list)  # attached by the repository, see ExportPackingListRepository._load

    @staticmethod
    def from_row(row) -> "ExportPackingListItem":
        return ExportPackingListItem(
            id=row["id"],
            export_packing_list_id=row["export_packing_list_id"],
            sr_no=row["sr_no"],
            container_sr_no=row["container_sr_no"],
            container_no=row["container_no"],
            seal_no=row["seal_no"],
            rfid_seal_no=row["rfid_seal_no"],
            invoice_item_sr_no=row["invoice_item_sr_no"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            group_label=row["group_label"],
            hsn_code=row["hsn_code"],
            pallets=row["pallets"],
            quantity_boxes=row["quantity_boxes"],
            quantity_unit=row["quantity_unit"],
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            net_weight_kg=row["net_weight_kg"],
            gross_weight_kg=row["gross_weight_kg"],
        )


@dataclass
class ExportPackingList:
    """The EXPORT PACKING LIST that accompanies an Export Invoice - exactly
    one per invoice, generated automatically whenever that invoice is saved
    and never created or edited on its own. It owns nothing but the
    container allocation: the whole printed header (consigner, consignee,
    ports, bank, declarations, EPCG, self-sealing block) comes from
    `invoice`, which the repository joins in."""
    id: Optional[int]
    company_id: int
    export_invoice_id: int
    packing_list_number: str
    packing_list_date: str
    created_by: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    items: List[ExportPackingListItem] = field(default_factory=list)
    invoice: Optional[ExportInvoice] = None  # the parent, loaded by get_by_id
    export_invoice_number: Optional[str] = None  # carried by list queries, which don't load the parent

    @staticmethod
    def from_row(row) -> "ExportPackingList":
        keys = row.keys()
        return ExportPackingList(
            id=row["id"],
            company_id=row["company_id"],
            export_invoice_id=row["export_invoice_id"],
            packing_list_number=row["packing_list_number"],
            packing_list_date=row["packing_list_date"],
            created_by=row["created_by"],
            created_at=row["created_at"] if "created_at" in keys else None,
            updated_at=row["updated_at"] if "updated_at" in keys else None,
            created_by_name=row["created_by_name"] if "created_by_name" in keys else None,
            export_invoice_number=row["export_invoice_number"] if "export_invoice_number" in keys else None,
        )

    # ---- totals (the sheet's bottom row) --------------------------------
    @property
    def total_pallets(self) -> float:
        return sum(i.pallets or 0 for i in self.items)

    @property
    def total_boxes(self) -> float:
        return sum(i.quantity_boxes or 0 for i in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(i.quantity_value or 0 for i in self.items)

    @property
    def total_net_weight(self) -> float:
        return sum(i.net_weight_kg or 0 for i in self.items)

    @property
    def total_gross_weight(self) -> float:
        return sum(i.gross_weight_kg or 0 for i in self.items)

    @property
    def container_count(self) -> int:
        return len({i.container_sr_no for i in self.items})

    @property
    def container_totals(self) -> dict:
        """container_sr_no -> {net_weight_kg, gross_weight_kg, pallets,
        quantity_boxes, quantity_value} summed across every goods line loaded
        into that physical container. Lets the Export Invoice's own 11B table
        show each container's actual weight/pallets/boxes (from what was typed
        into the packing list's container split) instead of separately
        hand-typed figures, and gives the BL draft its per-container line."""
        totals: dict = {}
        for i in self.items:
            t = totals.setdefault(i.container_sr_no, {
                "net_weight_kg": 0.0, "gross_weight_kg": 0.0, "pallets": 0.0,
                "quantity_boxes": 0.0, "quantity_value": 0.0,
            })
            t["net_weight_kg"] += i.net_weight_kg or 0
            t["gross_weight_kg"] += i.gross_weight_kg or 0
            t["pallets"] += i.pallets or 0
            t["quantity_boxes"] += i.quantity_boxes or 0
            t["quantity_value"] += i.quantity_value or 0
        return totals

    @property
    def printed_containers(self) -> List[dict]:
        """The sheet's body, ready to render: one entry per physical
        container, each holding the flat run of item rows printed beside its
        (rowspan-ed) Container No / Seal No / RFID cells. Each row carries its
        own product name and HSN code, so designs are listed individually
        rather than banded under a shared group heading."""
        containers: List[dict] = []
        current = None
        for item in self.items:
            key = (item.container_sr_no, item.container_no or "", item.seal_no or "", item.rfid_seal_no or "")
            if current is None or current["key"] != key:
                current = {
                    "key": key, "container_sr_no": item.container_sr_no, "container_no": item.container_no,
                    "seal_no": item.seal_no, "rfid_seal_no": item.rfid_seal_no, "rows": [],
                }
                containers.append(current)
            current["rows"].append({"kind": "item", "item": item})
        for c in containers:
            c["rowspan"] = len(c["rows"])
        return containers

    @property
    def container_rows(self) -> List[dict]:
        """One row per physical container - container_no/seal_no/rfid_seal_no
        (snapshotted identity, from that container's first item) alongside
        every quantity summed across the goods lines loaded into it (via
        `container_totals`). Feeds the Export Invoice's standalone Annexure-C
        container table (section 11), which reads six of these keys, and the
        BL draft's container table, which reads seven."""
        totals = self.container_totals
        rows: List[dict] = []
        seen = set()
        for item in self.items:
            if item.container_sr_no in seen:
                continue
            seen.add(item.container_sr_no)
            t = totals.get(item.container_sr_no, {})
            rows.append({
                "sr_no": item.container_sr_no,
                "container_no": item.container_no,
                "seal_no": item.seal_no,
                "rfid_seal_no": item.rfid_seal_no,
                "pallets": t.get("pallets", 0.0),
                "quantity_boxes": t.get("quantity_boxes", 0.0),
                "quantity_value": t.get("quantity_value", 0.0),
                "gross_weight_kg": t.get("gross_weight_kg", 0.0),
                "net_weight_kg": t.get("net_weight_kg", 0.0),
            })
        return rows


@dataclass
class ExportDesignsPackingList:
    """The DESIGNS PACKING LIST: the second packing list that ships alongside
    the regular Export Packing List, restating the same container split with
    every goods line broken into the catalog designs its boxes actually are.

    Exactly one per export invoice. All it owns is its own number and date,
    assigned once when it is created and kept through every later edit of the
    allocation - the paperwork already went out under that number. Every
    figure it prints comes from `packing_list` (the export packing list, whose
    items carry the design rows) and from `invoice`, both attached by the
    repository."""
    id: Optional[int]
    company_id: int
    export_invoice_id: int
    packing_list_number: str
    packing_list_date: str
    created_by: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    invoice: Optional[ExportInvoice] = None
    packing_list: Optional[ExportPackingList] = None
    export_invoice_number: Optional[str] = None  # carried by list queries, which don't load the parent

    @staticmethod
    def from_row(row) -> "ExportDesignsPackingList":
        keys = row.keys()
        return ExportDesignsPackingList(
            id=row["id"],
            company_id=row["company_id"],
            export_invoice_id=row["export_invoice_id"],
            packing_list_number=row["packing_list_number"],
            packing_list_date=row["packing_list_date"],
            created_by=row["created_by"],
            created_at=row["created_at"] if "created_at" in keys else None,
            updated_at=row["updated_at"] if "updated_at" in keys else None,
            created_by_name=row["created_by_name"] if "created_by_name" in keys else None,
            export_invoice_number=row["export_invoice_number"] if "export_invoice_number" in keys else None,
        )

    @property
    def printed_containers(self) -> List[dict]:
        """The sheet's body: one entry per physical container, each listing
        its goods lines' design rows (design name, boxes, quantity) plus that
        container's own totals. Lines nobody has allocated designs for yet
        still appear, with their boxes shown against a blank design, so the
        sheet never silently drops goods that are physically in the box."""
        if not self.packing_list:
            return []
        containers: List[dict] = []
        current = None
        for item in sorted(self.packing_list.items, key=lambda i: (i.container_sr_no, i.sr_no)):
            if current is None or current["container_sr_no"] != item.container_sr_no:
                current = {
                    "container_sr_no": item.container_sr_no, "container_no": item.container_no,
                    "seal_no": item.seal_no, "rfid_seal_no": item.rfid_seal_no,
                    "rows": [], "total_boxes": 0.0, "total_quantity": 0.0,
                }
                containers.append(current)
            if item.designs:
                for d in item.designs:
                    current["rows"].append({
                        "product_name": item.product_name, "hsn_code": item.hsn_code,
                        "design_name": d.design_name, "quantity_boxes": d.quantity_boxes or 0,
                        "quantity_unit": item.quantity_unit, "quantity_value": d.quantity_value or 0,
                        "unit": d.unit or item.unit,
                    })
                    current["total_boxes"] += d.quantity_boxes or 0
                    current["total_quantity"] += d.quantity_value or 0
            else:
                current["rows"].append({
                    "product_name": item.product_name, "hsn_code": item.hsn_code,
                    "design_name": None, "quantity_boxes": item.quantity_boxes or 0,
                    "quantity_unit": item.quantity_unit, "quantity_value": item.quantity_value or 0,
                    "unit": item.unit,
                })
                current["total_boxes"] += item.quantity_boxes or 0
                current["total_quantity"] += item.quantity_value or 0
        for c in containers:
            c["rowspan"] = len(c["rows"])
        return containers

    @property
    def total_boxes(self) -> float:
        return sum(c["total_boxes"] for c in self.printed_containers)

    @property
    def total_quantity(self) -> float:
        return sum(c["total_quantity"] for c in self.printed_containers)


@dataclass
class LoadingPlanningItem:
    """One goods line on a Loading Planning, at DESIGN level.

    Loaded by tracing a proforma invoice through its purchase orders to THOSE
    ORDERS' packing lists - a PO orders 1268 boxes of a product, and its
    packing list is what says those 1268 are four designs of 317. When a PO
    has no packing list to explode, the PO's own product line comes through
    with design_id/design_name NULL.

    `net_weight_kg` is PER box/pc (a snapshot of products.net_weight_kg), not
    the line total - it has to be per-unit, because a line is split across
    several cartons and pallets in quantities nobody knows at load time.
    `price_usd` is the PI's own quoted rate, matched by product_id."""
    id: Optional[int]
    loading_planning_id: Optional[int]
    sr_no: int
    product_name: str
    proforma_invoice_id: Optional[int] = None
    purchase_order_id: Optional[int] = None
    po_number: Optional[str] = None
    product_id: Optional[int] = None
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity_boxes: float = 0
    quantity_unit: str = "PCS"
    quantity_value: float = 0
    unit: str = "SQM"
    net_weight_kg: Optional[float] = None
    price_usd: float = 0
    total_usd: float = 0

    @property
    def label(self) -> str:
        """How the line names itself in the packing cards' pickers."""
        return f"{self.product_name} - {self.design_name}" if self.design_name else self.product_name

    @property
    def total_net_weight_kg(self) -> float:
        """What the whole line weighs, goods only - no carton or pallet tare."""
        return (self.net_weight_kg or 0) * (self.quantity_boxes or 0)

    @staticmethod
    def from_row(row) -> "LoadingPlanningItem":
        keys = row.keys()
        return LoadingPlanningItem(
            id=row["id"],
            loading_planning_id=row["loading_planning_id"],
            sr_no=row["sr_no"],
            product_name=row["product_name"],
            proforma_invoice_id=row["proforma_invoice_id"],
            purchase_order_id=row["purchase_order_id"],
            po_number=row["po_number"],
            product_id=row["product_id"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            hsn_code=row["hsn_code"],
            quantity_boxes=row["quantity_boxes"] or 0,
            quantity_unit=row["quantity_unit"] or "PCS",
            quantity_value=row["quantity_value"] or 0,
            unit=row["unit"] or "SQM",
            net_weight_kg=row["net_weight_kg"] if "net_weight_kg" in keys else None,
            price_usd=row["price_usd"] or 0,
            total_usd=row["total_usd"] or 0,
        )


@dataclass
class LoadingPlanningCarton:
    """One physical carton on a Loading Planning - the OPTIONAL inner packing
    level, which then goes on a pallet.

    Tiles never have one (boxes sit straight on the pallet); hardware does:
    45 + 45 PCS at 30/CTN packs as two full cartons plus one holding 15 of
    each, which is why `contents` is a list rather than a single line. A
    carton with pallet_no None has been built but not yet placed."""
    id: Optional[int]
    loading_planning_id: Optional[int]
    carton_no: int
    carton_type_id: Optional[int] = None
    carton_type_name: Optional[str] = None
    capacity_boxes: Optional[float] = None
    tare_weight_kg: Optional[float] = None
    pallet_no: Optional[int] = None
    contents: List[dict] = field(default_factory=list)  # [{item_sr_no, quantity_boxes}]

    @property
    def packed_boxes(self) -> float:
        return sum((c.get("quantity_boxes") or 0) for c in self.contents)

    @property
    def is_part_filled(self) -> bool:
        """Flagged on the form: the carton nobody has finished deciding about."""
        return bool(self.capacity_boxes) and self.packed_boxes < self.capacity_boxes

    @staticmethod
    def from_row(row) -> "LoadingPlanningCarton":
        return LoadingPlanningCarton(
            id=row["id"],
            loading_planning_id=row["loading_planning_id"],
            carton_no=row["carton_no"],
            carton_type_id=row["carton_type_id"],
            carton_type_name=row["carton_type_name"],
            capacity_boxes=row["capacity_boxes"],
            tare_weight_kg=row["tare_weight_kg"],
            pallet_no=row["pallet_no"],
        )


@dataclass
class LoadingPlanningPallet:
    """One physical pallet on a Loading Planning - what a forklift moves into
    the container, and the unit containers are packed with.

    A pallet's goods are its `contents` (boxes placed directly on it, the
    tiles case) PLUS whatever its `cartons` hold (the hardware case); it may
    carry both. That is what lets one weight rule cover every case:

        gross = contents net + carton tare + pallet tare

    which is exactly what PO20260827001's tiles want - (32 x 27) + 0 + 20 =
    884kg - and what PO20260827002's hardware wants - 44.325 + (3 x 0.3) + 20
    = 65.225kg. `container_sr_no` None means built but not yet loaded."""
    id: Optional[int]
    loading_planning_id: Optional[int]
    pallet_no: int
    pallet_type_id: Optional[int] = None
    pallet_type_name: Optional[str] = None
    capacity_boxes: Optional[float] = None
    tare_weight_kg: Optional[float] = None
    container_sr_no: Optional[int] = None
    contents: List[dict] = field(default_factory=list)  # [{item_sr_no, quantity_boxes}] placed DIRECTLY on the pallet
    cartons: List[LoadingPlanningCarton] = field(default_factory=list)  # populated by the service, not the row

    @property
    def direct_boxes(self) -> float:
        return sum((c.get("quantity_boxes") or 0) for c in self.contents)

    @property
    def packed_boxes(self) -> float:
        """Everything on the pallet, whether it went through a carton or not."""
        return self.direct_boxes + sum(c.packed_boxes for c in self.cartons)

    @property
    def carton_tare_kg(self) -> float:
        return sum((c.tare_weight_kg or 0) for c in self.cartons)

    @property
    def is_part_filled(self) -> bool:
        """Only meaningful for a pallet loaded directly with boxes - a pallet
        carrying cartons has no capacity, since how many fit is the
        operator's call, not a rule."""
        return bool(self.capacity_boxes) and self.direct_boxes < self.capacity_boxes

    def net_weight_kg(self, items_by_sr: dict) -> float:
        """Goods only. Needs the plan's items to know what a box weighs, so
        it takes them rather than being a bare property."""
        total = 0.0
        for row in self.contents:
            item = items_by_sr.get(row.get("item_sr_no"))
            if item:
                total += (item.net_weight_kg or 0) * (row.get("quantity_boxes") or 0)
        for carton in self.cartons:
            for row in carton.contents:
                item = items_by_sr.get(row.get("item_sr_no"))
                if item:
                    total += (item.net_weight_kg or 0) * (row.get("quantity_boxes") or 0)
        return total

    def gross_weight_kg(self, items_by_sr: dict) -> float:
        """The rule the whole document is built around."""
        return self.net_weight_kg(items_by_sr) + self.carton_tare_kg + (self.tare_weight_kg or 0)

    @staticmethod
    def from_row(row) -> "LoadingPlanningPallet":
        return LoadingPlanningPallet(
            id=row["id"],
            loading_planning_id=row["loading_planning_id"],
            pallet_no=row["pallet_no"],
            pallet_type_id=row["pallet_type_id"],
            pallet_type_name=row["pallet_type_name"],
            capacity_boxes=row["capacity_boxes"],
            tare_weight_kg=row["tare_weight_kg"],
            container_sr_no=row["container_sr_no"],
        )


@dataclass
class LoadingPlanning:
    """The LOADING PLANNING document: which goods physically go in which
    container, worked out before the export invoice is cut.

    Nothing else in the app answers that. A purchase order knows what was
    bought, its packing list knows the design split, and a booking knows the
    containers - but `packing_list_items.pallets` is stored as
    boxes/box_per_pallet, a DECIMAL, and 9.91 pallets or 1.5 cartons is not
    a thing anyone can ship. So this document makes cartons and pallets real
    numbered objects that a human fills by hand, then assigns those pallets
    whole to the booking's containers.

    Container rows are a SNAPSHOT of the chosen booking, same treatment every
    other imported party detail gets, so editing the booking later can't
    rewrite a finished plan."""
    id: Optional[int]
    company_id: int
    created_by: int
    loading_planning_number: str
    loading_planning_date: str
    booking_detail_id: Optional[int] = None
    booking_no: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_no: Optional[str] = None
    transporter_name: Optional[str] = None
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    item_count: Optional[int] = None  # list-view only
    pallet_count: Optional[int] = None  # list-view only
    proforma_invoice_ids: List[int] = field(default_factory=list)
    proforma_invoice_numbers: List[str] = field(default_factory=list)
    items: List[LoadingPlanningItem] = field(default_factory=list)
    containers: List[dict] = field(default_factory=list)  # snapshot of the booking's 11B rows
    cartons: List[LoadingPlanningCarton] = field(default_factory=list)
    pallets: List[LoadingPlanningPallet] = field(default_factory=list)

    @property
    def items_by_sr(self) -> dict:
        return {i.sr_no: i for i in self.items}

    @property
    def total_boxes(self) -> float:
        return sum((i.quantity_boxes or 0) for i in self.items)

    @property
    def total_net_weight_kg(self) -> float:
        return sum(i.total_net_weight_kg for i in self.items)

    @property
    def line_balances(self) -> List[dict]:
        """Per goods line: how much is planned, how much has actually been
        packed (in a carton or straight onto a pallet), and what is left.

        `left` must reach 0 for the plan to be complete - but a non-zero
        left is a WARNING, never a refusal to save. Unlike the export packing
        list's container split, a loading plan is legitimately worked on over
        several sittings."""
        packed = {i.sr_no: 0.0 for i in self.items}
        for carton in self.cartons:
            for row in carton.contents:
                sr = row.get("item_sr_no")
                if sr in packed:
                    packed[sr] += row.get("quantity_boxes") or 0
        for pallet in self.pallets:
            for row in pallet.contents:
                sr = row.get("item_sr_no")
                if sr in packed:
                    packed[sr] += row.get("quantity_boxes") or 0
        out = []
        for item in self.items:
            done = packed.get(item.sr_no, 0.0)
            out.append({
                "sr_no": item.sr_no,
                "label": item.label,
                "quantity_unit": item.quantity_unit,
                "planned": item.quantity_boxes or 0,
                "packed": done,
                "left": round((item.quantity_boxes or 0) - done, 3),
            })
        return out

    @property
    def is_fully_packed(self) -> bool:
        return bool(self.items) and all(abs(b["left"]) < 0.001 for b in self.line_balances)

    @property
    def container_summary(self) -> List[dict]:
        """One row per container plus a final "unassigned" row, carrying the
        VGM check the loading bay actually cares about: a container's own
        tare plus everything stacked in it, against what it is allowed to
        weigh. `over_weight` turns the row red on the form and prints a
        warning - it never blocks a save."""
        by_sr = self.items_by_sr
        cartons_by_pallet: dict = {}
        for carton in self.cartons:
            cartons_by_pallet.setdefault(carton.pallet_no, []).append(carton)
        rows = []
        for container in self.containers:
            sr = container.get("sr_no")
            pallets = [p for p in self.pallets if p.container_sr_no == sr]
            for pallet in pallets:
                pallet.cartons = cartons_by_pallet.get(pallet.pallet_no, [])
            cargo = sum(p.gross_weight_kg(by_sr) for p in pallets)
            container_tare = container.get("tare_weight_kg") or 0
            vgm = cargo + container_tare
            try:
                max_weight = float(container.get("max_permitted_weight") or 0)
            except (TypeError, ValueError):
                max_weight = 0
            rows.append({
                "sr_no": sr,
                "container_no": container.get("container_no"),
                "container_type": container.get("container_type"),
                "pallet_count": len(pallets),
                "boxes": sum(p.packed_boxes for p in pallets),
                "net_weight_kg": sum(p.net_weight_kg(by_sr) for p in pallets),
                "carton_tare_kg": sum(p.carton_tare_kg for p in pallets),
                "pallet_tare_kg": sum((p.tare_weight_kg or 0) for p in pallets),
                "cargo_weight_kg": cargo,
                "container_tare_kg": container_tare,
                "vgm_kg": vgm,
                "max_permitted_weight": max_weight,
                "headroom_kg": (max_weight - vgm) if max_weight else None,
                "over_weight": bool(max_weight and vgm > max_weight),
            })
        loose = [p for p in self.pallets if p.container_sr_no is None]
        if loose:
            for pallet in loose:
                pallet.cartons = cartons_by_pallet.get(pallet.pallet_no, [])
            rows.append({
                "sr_no": None,
                "container_no": "Unassigned",
                "container_type": None,
                "pallet_count": len(loose),
                "boxes": sum(p.packed_boxes for p in loose),
                "net_weight_kg": sum(p.net_weight_kg(by_sr) for p in loose),
                "carton_tare_kg": sum(p.carton_tare_kg for p in loose),
                "pallet_tare_kg": sum((p.tare_weight_kg or 0) for p in loose),
                "cargo_weight_kg": sum(p.gross_weight_kg(by_sr) for p in loose),
                "container_tare_kg": 0,
                "vgm_kg": None,
                "max_permitted_weight": 0,
                "headroom_kg": None,
                "over_weight": False,
            })
        return rows

    @staticmethod
    def from_row(row) -> "LoadingPlanning":
        keys = row.keys()
        return LoadingPlanning(
            id=row["id"],
            company_id=row["company_id"],
            created_by=row["created_by"],
            loading_planning_number=row["loading_planning_number"],
            loading_planning_date=row["loading_planning_date"],
            booking_detail_id=row["booking_detail_id"],
            booking_no=row["booking_no"],
            vessel_name=row["vessel_name"],
            voyage_no=row["voyage_no"],
            transporter_name=row["transporter_name"],
            remarks=row["remarks"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in keys else None,
            item_count=row["item_count"] if "item_count" in keys else None,
            pallet_count=row["pallet_count"] if "pallet_count" in keys else None,
        )


@dataclass
class PackingPlanningItem:
    """One produced BATCH on a Packing Planning, and how it packs.

    A batch, not a design: a design is routinely fired in several batches on
    different days - ATLANTA LIGHT GREY came off as 200 on the 27th under
    batch 102 and 117 on the 28th under 103 - and a pallet is packed out of
    one of them, so the batch number and its manufacturing date have to ride
    on the line that gets packed.

    Everything the sheet's right-hand columns show is derived here rather
    than stored: `boxes_per_unit` (32 for a pallet of tiles, 30 for a carton
    of hardware) is the only input, and it comes off product_pallet_types."""
    id: Optional[int]
    packing_planning_id: Optional[int]
    sr_no: int
    product_name: str
    proforma_invoice_id: Optional[int] = None
    purchase_order_id: Optional[int] = None
    po_number: Optional[str] = None
    purchase_order_item_id: Optional[int] = None
    product_id: Optional[int] = None
    design_id: Optional[int] = None
    design_name: Optional[str] = None
    batch_number: Optional[str] = None
    production_date: Optional[str] = None
    ready_quantity: float = 0
    quantity_unit: str = "BOX"
    packing_type_id: Optional[int] = None
    packing_type_name: Optional[str] = None
    packing_unit_label: str = "PLT"
    boxes_per_unit: Optional[float] = None
    actual_packing: int = 0
    packing_no_start: Optional[int] = None

    @property
    def label(self) -> str:
        """How the line names itself in the manual-packing picker."""
        base = f"{self.product_name} - {self.design_name}" if self.design_name else self.product_name
        return f"{base} [{self.batch_number}]" if self.batch_number else base

    @property
    def as_per_pl_packing(self) -> float:
        """AS PER PL PACKING: the decimal number of units the ready quantity
        makes - 317 boxes at 32/pallet is 9.91 PLT. This is the figure
        packing_list_items.pallets has always carried, and the reason this
        document exists: 9.91 pallets is not a thing anyone can ship.

        Rounded HALF UP, not with round()'s banker's rounding, because this
        column is read against a spreadsheet that rounds the other way: 100
        boxes at 32 is 3.125, which the floor prints as 3.13, and a display
        figure that disagrees with the paper it is checked against is worse
        than useless. Nothing is computed from it - the packed quantity comes
        off actual_packing - so the rounding is presentational only."""
        if not self.boxes_per_unit:
            return 0.0
        exact = (self.ready_quantity or 0) / self.boxes_per_unit
        return math.floor(exact * 100 + 0.5) / 100 if exact >= 0 else -(math.floor(-exact * 100 + 0.5) / 100)

    @property
    def packed_quantity(self) -> float:
        """QTY: what the whole units actually hold - 9 x 32 = 288."""
        return round((self.actual_packing or 0) * (self.boxes_per_unit or 0), 3)

    @property
    def remain_quantity(self) -> float:
        """What is left for the manual table. 0 when the batch divides
        exactly (160 at 32 is five pallets and nothing over), in which case
        the line never appears down there at all."""
        return round((self.ready_quantity or 0) - self.packed_quantity, 3)

    @property
    def over_packed(self) -> bool:
        """Packing more than was produced - warned about, never blocked."""
        return self.remain_quantity < -0.001

    @staticmethod
    def from_row(row) -> "PackingPlanningItem":
        return PackingPlanningItem(
            id=row["id"],
            packing_planning_id=row["packing_planning_id"],
            sr_no=row["sr_no"],
            product_name=row["product_name"],
            proforma_invoice_id=row["proforma_invoice_id"],
            purchase_order_id=row["purchase_order_id"],
            po_number=row["po_number"],
            purchase_order_item_id=row["purchase_order_item_id"],
            product_id=row["product_id"],
            design_id=row["design_id"],
            design_name=row["design_name"],
            batch_number=row["batch_number"],
            production_date=row["production_date"],
            ready_quantity=row["ready_quantity"] or 0,
            quantity_unit=row["quantity_unit"] or "BOX",
            packing_type_id=row["packing_type_id"],
            packing_type_name=row["packing_type_name"],
            packing_unit_label=row["packing_unit_label"] or "PLT",
            boxes_per_unit=row["boxes_per_unit"],
            actual_packing=row["actual_packing"] or 0,
            packing_no_start=row["packing_no_start"],
        )


@dataclass
class PackingPlanningManualUnit:
    """One pallet or carton packed by hand out of the leftovers.

    The auto rows each pack a single batch, because a full pallet of one
    design is what a machine's output naturally makes. What is left over
    does not divide that way - ARKOSE leaves 29 boxes and ATLANTA leaves 8,
    and whether those two share a pallet is exactly the judgement call no
    rule can make - so a manual unit holds any mix, which is why `contents`
    is a list.

    Its `unit_no` carries on the same sequence the batch rows use, so a
    pallet number is unique across the document however it was packed."""
    id: Optional[int]
    packing_planning_id: Optional[int]
    unit_no: int
    packing_type_id: Optional[int] = None
    packing_type_name: Optional[str] = None
    packing_unit_label: str = "PLT"
    capacity_boxes: Optional[float] = None
    remarks: Optional[str] = None
    contents: List[dict] = field(default_factory=list)  # [{item_sr_no, quantity_boxes}]

    @property
    def packed_boxes(self) -> float:
        return round(sum((c.get("quantity_boxes") or 0) for c in self.contents), 3)

    @property
    def over_capacity(self) -> bool:
        return bool(self.capacity_boxes) and self.packed_boxes > self.capacity_boxes

    @staticmethod
    def from_row(row) -> "PackingPlanningManualUnit":
        return PackingPlanningManualUnit(
            id=row["id"],
            packing_planning_id=row["packing_planning_id"],
            unit_no=row["unit_no"],
            packing_type_id=row["packing_type_id"],
            packing_type_name=row["packing_type_name"],
            packing_unit_label=row["packing_unit_label"] or "PLT",
            capacity_boxes=row["capacity_boxes"],
            remarks=row["remarks"],
        )


@dataclass
class PackingPlanning:
    """The PACKING PLANNING document: how what has actually been produced
    breaks into numbered pallets and cartons, and what is left over.

    The step before Loading Planning. A loading plan says which goods go in
    which container; this says what there is to load in the first place -
    the purchase order's Production Status card knows the batches, and
    product_pallet_types knows a pallet takes 32 boxes, but nothing put the
    two together and said "317 ready is nine full pallets and 29 boxes
    somebody has to pack by hand".

    Only the batch rows are stored. The PACKING REMAIN BY MANUAL table is
    derived from them (`remain_rows`) rather than kept alongside, because
    two stored halves would drift apart the first time an actual packing
    figure was edited."""
    id: Optional[int]
    company_id: int
    created_by: int
    packing_planning_number: str
    packing_planning_date: str
    remarks: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    item_count: Optional[int] = None  # list-view only
    unit_count: Optional[int] = None  # list-view only
    proforma_invoice_ids: List[int] = field(default_factory=list)
    proforma_invoice_numbers: List[str] = field(default_factory=list)
    items: List[PackingPlanningItem] = field(default_factory=list)
    manual_units: List[PackingPlanningManualUnit] = field(default_factory=list)

    @property
    def items_by_sr(self) -> dict:
        return {i.sr_no: i for i in self.items}

    @property
    def packing_numbers(self) -> dict:
        """sr_no -> (start, end) for the PACKING NO START FROM / END NUMBER
        columns: a running counter down the document, so row 1's nine
        pallets are 1-9 and row 2's six are 10-15.

        A row with `packing_no_start` set PINS itself there and the counter
        carries on from ITS end, which is how a plan survives someone
        renumbering a pallet mid-sheet. The spreadsheet this replaces had no
        such rule, which is why its rows 8-10 silently reused 41-46 - numbers
        rows 6 and 7 had already taken.

        A row that packs nothing gets (None, None): it has no pallets to
        number, and must not consume one either."""
        out = {}
        counter = 1
        for item in self.items:
            if item.packing_no_start:
                counter = item.packing_no_start
            count = item.actual_packing or 0
            if count <= 0:
                out[item.sr_no] = (None, None)
                continue
            out[item.sr_no] = (counter, counter + count - 1)
            counter += count
        return out

    @property
    def next_packing_no(self) -> int:
        """The first free number: what a new manual unit takes. Counted off
        both halves of the document, since they share one sequence."""
        used = [end for _, end in self.packing_numbers.values() if end]
        used += [u.unit_no for u in self.manual_units if u.unit_no]
        return (max(used) + 1) if used else 1

    @property
    def duplicate_packing_numbers(self) -> List[int]:
        """Numbers used twice across the whole document - pinning a start
        number by hand can collide with a range already handed out."""
        seen, dupes = set(), set()
        for start, end in self.packing_numbers.values():
            if not start:
                continue
            for n in range(start, end + 1):
                (dupes if n in seen else seen).add(n)
        for unit in self.manual_units:
            (dupes if unit.unit_no in seen else seen).add(unit.unit_no)
        return sorted(dupes)

    @property
    def allocated_by_sr(self) -> dict:
        """sr_no -> how much of that batch's leftover the manual units hold."""
        out = {i.sr_no: 0.0 for i in self.items}
        for unit in self.manual_units:
            for row in unit.contents:
                sr = row.get("item_sr_no")
                if sr in out:
                    out[sr] += row.get("quantity_boxes") or 0
        return {sr: round(v, 3) for sr, v in out.items()}

    @property
    def remain_rows(self) -> List[dict]:
        """The PACKING REMAIN BY MANUAL table: every batch with something
        left after its whole units were taken out, renumbered 1..n in its
        own right. A batch that divided exactly is simply absent - which is
        why the source sheet's twelve auto rows produce eleven manual ones,
        batch 107's 160 boxes being exactly five pallets."""
        allocated = self.allocated_by_sr
        rows = []
        for item in self.items:
            remain = item.remain_quantity
            if remain <= 0.001:
                continue
            done = allocated.get(item.sr_no, 0.0)
            rows.append({
                "sr_no": len(rows) + 1,
                "item_sr_no": item.sr_no,
                "product_name": item.product_name,
                "design_name": item.design_name,
                "batch_number": item.batch_number,
                "production_date": item.production_date,
                "quantity": remain,
                "quantity_unit": item.quantity_unit,
                "allocated": done,
                "left": round(remain - done, 3),
                "unit_nos": sorted({u.unit_no for u in self.manual_units
                                    for c in u.contents if c.get("item_sr_no") == item.sr_no}),
            })
        return rows

    @property
    def pallet_rows(self) -> List[dict]:
        """The PALLET PACKING PLANNING sheet, as one continuous list: an
        AUTO row is a single batch with a packing-number RANGE ("1 TO 9"),
        a MANUAL row is one hand-packed unit built from several batches'
        leftovers with a single packing number ("54") and one line per
        batch it holds. Both halves share one SR NO sequence and one
        packing-number sequence, because that is how the sheet is read on
        the floor - a pallet number is unique however it was packed.

        A row with nothing packed prints nothing: there is no pallet to
        list. `lines` is always non-empty for a row that IS printed - an
        auto row has exactly one, a manual row one per batch it draws from."""
        numbers = self.packing_numbers
        by_sr = self.items_by_sr
        rows: List[dict] = []

        for item in self.items:
            start, end = numbers.get(item.sr_no) or (None, None)
            if not start:
                continue
            rows.append({
                "sr_no": len(rows) + 1,
                "actual_packing": item.actual_packing,
                "packing_unit_label": item.packing_unit_label,
                "packing_no": f"{start} TO {end}" if end > start else str(start),
                "lines": [{
                    "product_name": item.product_name, "design_name": item.design_name,
                    "batch_number": item.batch_number, "production_date": item.production_date,
                    "quantity": item.packed_quantity, "quantity_unit": item.quantity_unit,
                }],
            })

        for unit in self.manual_units:
            lines = []
            for content in unit.contents:
                src = by_sr.get(content.get("item_sr_no"))
                lines.append({
                    "product_name": src.product_name if src else None,
                    "design_name": src.design_name if src else None,
                    "batch_number": src.batch_number if src else None,
                    "production_date": src.production_date if src else None,
                    "quantity": content.get("quantity_boxes") or 0,
                    "quantity_unit": src.quantity_unit if src else "BOX",
                })
            rows.append({
                "sr_no": len(rows) + 1,
                "actual_packing": 1,
                "packing_unit_label": unit.packing_unit_label,
                "packing_no": str(unit.unit_no),
                "lines": lines or [{"product_name": "(empty)", "design_name": None,
                                     "batch_number": None, "production_date": None,
                                     "quantity": 0, "quantity_unit": ""}],
            })
        return rows

    @property
    def total_units_by_label(self) -> dict:
        """packing_unit_label -> how many numbered pallets/cartons print
        under it, across both halves - what the sheet's footer totals. Kept
        apart from `total_units` (a flat count, used by the warnings check)
        because a document mixing PLT and CTN can't sum those into one
        number and still mean anything printed."""
        out: dict = {}
        for item in self.items:
            if item.actual_packing:
                out[item.packing_unit_label] = out.get(item.packing_unit_label, 0) + item.actual_packing
        for unit in self.manual_units:
            out[unit.packing_unit_label] = out.get(unit.packing_unit_label, 0) + 1
        return out

    @property
    def total_quantity_by_unit(self) -> dict:
        """quantity_unit -> total boxes/pieces actually printed on
        `pallet_rows` - the auto rows' packed_quantity plus what the manual
        units hold, split by unit for the same reason total_units_by_label
        is."""
        out: dict = {}
        for item in self.items:
            if item.actual_packing:
                out[item.quantity_unit] = round(out.get(item.quantity_unit, 0) + item.packed_quantity, 3)
        by_sr = self.items_by_sr
        for unit in self.manual_units:
            for content in unit.contents:
                src = by_sr.get(content.get("item_sr_no"))
                key = src.quantity_unit if src else "BOX"
                out[key] = round(out.get(key, 0) + (content.get("quantity_boxes") or 0), 3)
        return out

    @property
    def total_ready(self) -> float:
        return round(sum((i.ready_quantity or 0) for i in self.items), 3)

    @property
    def total_packed(self) -> float:
        return round(sum(i.packed_quantity for i in self.items), 3)

    @property
    def total_remain(self) -> float:
        return round(sum(max(i.remain_quantity, 0) for i in self.items), 3)

    @property
    def total_units(self) -> int:
        """Every numbered pallet/carton the document plans, both halves."""
        return sum((i.actual_packing or 0) for i in self.items) + len(self.manual_units)

    @property
    def is_fully_packed(self) -> bool:
        """Every leftover accounted for by a manual unit. A false here is a
        warning on save, never a refusal - the document is worked on across
        sittings, the same call LoadingPlanning makes about its own."""
        return bool(self.items) and all(abs(r["left"]) < 0.001 for r in self.remain_rows)

    @staticmethod
    def from_row(row) -> "PackingPlanning":
        keys = row.keys()
        return PackingPlanning(
            id=row["id"],
            company_id=row["company_id"],
            created_by=row["created_by"],
            packing_planning_number=row["packing_planning_number"],
            packing_planning_date=row["packing_planning_date"],
            remarks=row["remarks"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in keys else None,
            item_count=row["item_count"] if "item_count" in keys else None,
            unit_count=row["unit_count"] if "unit_count" in keys else None,
        )


@dataclass
class DocumentVersion:
    """One past-or-current snapshot of a Quotation/ProformaInvoice/PackingList,
    taken on every create/update. `snapshot` is that document's full
    dataclass state (header fields + items) serialized as JSON - admin-only,
    read-only history, never itself editable."""
    id: Optional[int]
    company_id: int
    document_type: str
    document_id: int
    version_number: int
    document_number: str
    snapshot: dict
    changed_by: int
    created_at: Optional[str] = None
    changed_by_name: Optional[str] = None  # populated by joined queries only

    @staticmethod
    def from_row(row) -> "DocumentVersion":
        return DocumentVersion(
            id=row["id"],
            company_id=row["company_id"],
            document_type=row["document_type"],
            document_id=row["document_id"],
            version_number=row["version_number"],
            document_number=row["document_number"],
            snapshot=json.loads(row["snapshot"]),
            changed_by=row["changed_by"],
            created_at=row["created_at"],
            changed_by_name=row["changed_by_name"] if "changed_by_name" in row.keys() else None,
        )
