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

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class User:
    id: Optional[int]
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
    parent_type: str  # 'lead' | 'client'
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

CLIENT_TYPES = ["Supplier", "Exporter", "Buyer"]

COMMUNICATION_MODES = ["WhatsApp", "WeChat", "Call", "Email", "In Person", "Other"]


@dataclass
class Lead:
    id: Optional[int]
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
    converted_client_id: Optional[int] = None
    # populated by joins / repository convenience methods, not stored columns
    created_by_name: Optional[str] = None
    contacts: List[ContactPerson] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "Lead":
        return Lead(
            id=row["id"],
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
            converted_client_id=row["converted_client_id"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
        )

    @property
    def status_label(self) -> str:
        return dict(LEAD_STATUSES).get(self.status, self.status)


@dataclass
class Client:
    id: Optional[int]
    lead_id: Optional[int]
    company_name: str
    phone: str
    email: str
    facebook: Optional[str]
    instagram: Optional[str]
    other_social: Optional[str]
    client_type: str
    status: str
    created_by: int
    address: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    contacts: List[ContactPerson] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "Client":
        return Client(
            id=row["id"],
            lead_id=row["lead_id"],
            company_name=row["company_name"],
            phone=row["phone"],
            email=row["email"],
            facebook=row["facebook"],
            instagram=row["instagram"],
            other_social=row["other_social"],
            client_type=row["client_type"],
            status=row["status"],
            created_by=row["created_by"],
            address=row["address"] if "address" in row.keys() else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def status_label(self) -> str:
        return dict(CLIENT_STATUSES).get(self.status, self.status)


@dataclass
class PaymentEntry:
    id: Optional[int]
    client_id: int
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
            client_id=row["client_id"],
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
    id: Optional[int]
    client_id: int
    document_name: str
    document_type: str
    document_date: str
    notes: Optional[str] = None
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "DocumentEntry":
        return DocumentEntry(
            id=row["id"],
            client_id=row["client_id"],
            document_name=row["document_name"],
            document_type=row["document_type"],
            document_date=row["document_date"],
            notes=row["notes"],
            created_at=row["created_at"],
        )


@dataclass
class OurCompany:
    id: int
    company_name: str
    gstin: Optional[str]
    pan_no: Optional[str]
    iec: Optional[str]
    lut: Optional[str] = None
    bin: Optional[str] = None
    address: Optional[str] = None
    updated_at: Optional[str] = None
    contact_details: List[dict] = field(default_factory=list)  # [{type, value, is_primary}]
    contact_persons: List[dict] = field(default_factory=list)  # [{name, is_primary}]
    bank_details: List[dict] = field(default_factory=list)  # [{bank_name, account_number, ifsc_code, branch, is_primary}]

    @staticmethod
    def from_row(row) -> "OurCompany":
        return OurCompany(
            id=row["id"],
            company_name=row["company_name"],
            gstin=row["gstin"],
            pan_no=row["pan_no"],
            iec=row["iec"],
            lut=row["lut"] if "lut" in row.keys() else None,
            bin=row["bin"] if "bin" in row.keys() else None,
            address=row["address"] if "address" in row.keys() else None,
            updated_at=row["updated_at"],
        )


@dataclass
class ProductGroup:
    """A folder in the product catalog. `parent_id=None` means it's a
    top-level group; groups can nest to any depth via self-reference."""
    id: Optional[int]
    name: str
    parent_id: Optional[int] = None
    created_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "ProductGroup":
        return ProductGroup(
            id=row["id"],
            name=row["name"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
        )


@dataclass
class Product:
    """A file in the product catalog folder tree. `group_id=None` means it
    sits at the catalog root, alongside top-level groups."""
    id: Optional[int]
    group_id: Optional[int]
    product_name: str
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    packing: Optional[str] = None
    quantity: Optional[str] = None
    alternate_quantity: Optional[str] = None
    weight_class: Optional[str] = None
    price_usd: Optional[float] = None
    photo_path: Optional[str] = None
    dimension_photo_path: Optional[str] = None
    alt_text: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "Product":
        return Product(
            id=row["id"],
            group_id=row["group_id"],
            product_name=row["product_name"],
            description=row["description"],
            hsn_code=row["hsn_code"],
            packing=row["packing"],
            quantity=row["quantity"],
            alternate_quantity=row["alternate_quantity"],
            weight_class=row["weight_class"] if "weight_class" in row.keys() else None,
            price_usd=row["price_usd"] if "price_usd" in row.keys() else None,
            photo_path=row["photo_path"],
            dimension_photo_path=row["dimension_photo_path"],
            alt_text=row["alt_text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


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
    quantity_value: float = 0
    unit: str = "SQM"
    price_usd: float = 0
    total_usd: float = 0

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
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_usd=row["price_usd"],
            total_usd=row["total_usd"],
        )


@dataclass
class Quotation:
    id: Optional[int]
    quotation_number: str
    quotation_date: str
    buyer_name: str
    created_by: int
    lead_id: Optional[int] = None
    buyer_address: Optional[str] = None
    buyer_reference_no: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    packing_details: Optional[str] = None
    container_details: Optional[str] = None
    shipping_mode: Optional[str] = None
    shipping_terms: Optional[str] = None
    payment_terms: Optional[str] = None
    advance_percent: float = 0
    against_bl_percent: float = 0
    price_validity_days: int = 30
    remarks: Optional[str] = None
    discount_amount: float = 0
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc_code: Optional[str] = None
    bank_swift_code: Optional[str] = None
    bank_branch: Optional[str] = None
    bank_address: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    items: List[QuotationItem] = field(default_factory=list)
    computed_subtotal_usd: Optional[float] = None  # precomputed by list queries that don't load items

    @staticmethod
    def from_row(row) -> "Quotation":
        return Quotation(
            id=row["id"],
            quotation_number=row["quotation_number"],
            quotation_date=row["quotation_date"],
            lead_id=row["lead_id"] if "lead_id" in row.keys() else None,
            buyer_name=row["buyer_name"],
            buyer_address=row["buyer_address"],
            buyer_reference_no=row["buyer_reference_no"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            packing_details=row["packing_details"],
            container_details=row["container_details"],
            shipping_mode=row["shipping_mode"],
            shipping_terms=row["shipping_terms"],
            payment_terms=row["payment_terms"],
            advance_percent=row["advance_percent"],
            against_bl_percent=row["against_bl_percent"],
            price_validity_days=row["price_validity_days"],
            remarks=row["remarks"],
            discount_amount=row["discount_amount"],
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
        )

    @property
    def subtotal_usd(self) -> float:
        if self.computed_subtotal_usd is not None:
            return self.computed_subtotal_usd
        return sum(item.total_usd for item in self.items)

    @property
    def invoice_value_usd(self) -> float:
        return self.subtotal_usd - self.discount_amount
