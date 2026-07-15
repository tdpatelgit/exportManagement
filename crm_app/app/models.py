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
            converted_client_id=row["converted_client_id"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
        )

    @property
    def status_label(self) -> str:
        return dict(LEAD_STATUSES).get(self.status, self.status)


@dataclass
class Client:
    id: Optional[int]
    company_id: int
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
            company_id=row["company_id"],
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
    """Metadata-only placeholder for now (see the hint on the client detail
    page) - a future update will auto-generate and file-store these the
    same way Quotation already works. When that happens, give the new
    document type its own optional `lead_id` (like Quotation.lead_id)
    instead of a `client_id` - a client has no document link of its own;
    QuotationRepository.list_for_lead shows the pattern: a converted
    client's documents are found via `client.lead_id`, so anything created
    against the lead (before OR after conversion) stays visible on the
    client automatically, with nothing to copy or keep in sync by hand."""
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
    company_id: int
    company_name: str
    gstin: Optional[str]
    pan_no: Optional[str]
    iec: Optional[str]
    bin: Optional[str] = None
    address: Optional[str] = None
    updated_at: Optional[str] = None
    contact_details: List[dict] = field(default_factory=list)  # [{type, value, is_primary}]
    contact_persons: List[dict] = field(default_factory=list)  # [{name, is_primary}]
    bank_details: List[dict] = field(default_factory=list)  # [{bank_name, account_number, ifsc_code, branch, is_primary}]
    lut_details: List[dict] = field(default_factory=list)  # [{lut_number, financial_year, is_primary}]

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
            address=row["address"] if "address" in row.keys() else None,
            updated_at=row["updated_at"],
        )


@dataclass
class Product:
    """Top level of the catalog: the tax/HSN identity that quotations and
    proforma invoices bill against. Folders and designs live underneath it;
    price, packing and photos belong to the Design, not the Product."""
    id: Optional[int]
    company_id: int
    product_name: str
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    gst_percent: Optional[float] = None
    igst_percent: Optional[float] = None
    sgst_percent: Optional[float] = None
    cgst_percent: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def from_row(row) -> "Product":
        return Product(
            id=row["id"],
            company_id=row["company_id"],
            product_name=row["product_name"],
            description=row["description"],
            hsn_code=row["hsn_code"],
            gst_percent=row["gst_percent"],
            igst_percent=row["igst_percent"],
            sgst_percent=row["sgst_percent"],
            cgst_percent=row["cgst_percent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class ProductFolder:
    """A folder inside a product. `parent_id=None` means it sits at the
    product's top level; folders can nest to any depth via self-reference,
    but always belong to exactly one product."""
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
    """The sellable leaf of the catalog: one concrete design of a product,
    carrying the price, packing, per-box quantity, weights and photos.
    `folder_id=None` means it sits directly under the product."""
    id: Optional[int]
    company_id: int
    product_id: int
    design_name: str
    folder_id: Optional[int] = None
    description: Optional[str] = None
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
    def from_row(row) -> "Design":
        return Design(
            id=row["id"],
            company_id=row["company_id"],
            product_id=row["product_id"],
            folder_id=row["folder_id"],
            design_name=row["design_name"],
            description=row["description"],
            packing=row["packing"],
            quantity=row["quantity"],
            alternate_quantity=row["alternate_quantity"],
            weight_class=row["weight_class"],
            price_usd=row["price_usd"],
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
    packing_details: Optional[str] = None
    container_details: Optional[str] = None
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
            company_id=row["company_id"],
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
            price_validity_days=row["price_validity_days"],
            remarks=row["remarks"],
            sea_freight=row["sea_freight"] if "sea_freight" in row.keys() else 0,
            insurance=row["insurance"] if "insurance" in row.keys() else 0,
            certification=row["certification"] if "certification" in row.keys() else 0,
            other_charges=row["other_charges"] if "other_charges" in row.keys() else 0,
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
        return (self.subtotal_usd + self.sea_freight + self.insurance
                + self.certification + self.other_charges - self.discount_amount)


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
    pallets: Optional[float] = None
    quantity_boxes: Optional[float] = None
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
            pallets=row["pallets"],
            quantity_boxes=row["quantity_boxes"],
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
    lead_id: Optional[int] = None
    proforma_invoice_id: Optional[int] = None
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
    items: List[PackingListItem] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "PackingList":
        return PackingList(
            id=row["id"],
            company_id=row["company_id"],
            packing_list_number=row["packing_list_number"],
            packing_list_date=row["packing_list_date"],
            lead_id=row["lead_id"],
            proforma_invoice_id=row["proforma_invoice_id"],
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
        )

    @property
    def total_pallets(self) -> float:
        return sum(item.pallets or 0 for item in self.items)

    @property
    def total_boxes(self) -> float:
        return sum(item.quantity_boxes or 0 for item in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(item.quantity_value or 0 for item in self.items)

    @property
    def total_net_weight_kg(self) -> float:
        return sum(item.net_weight_kg or 0 for item in self.items)

    @property
    def total_gross_weight_kg(self) -> float:
        return sum(item.gross_weight_kg or 0 for item in self.items)


@dataclass
class ProformaInvoiceItem:
    id: Optional[int]
    proforma_invoice_id: Optional[int]
    sr_no: int
    product_name: str
    product_id: Optional[int] = None
    dimension_mm: Optional[str] = None
    hsn_code: Optional[str] = None
    pallets: Optional[float] = None
    quantity_boxes: Optional[float] = None
    quantity_value: float = 0
    unit: str = "SQM"
    price_usd: float = 0
    total_usd: float = 0

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
            pallets=row["pallets"],
            quantity_boxes=row["quantity_boxes"],
            quantity_value=row["quantity_value"],
            unit=row["unit"],
            price_usd=row["price_usd"],
            total_usd=row["total_usd"],
        )


@dataclass
class ProformaInvoice:
    id: Optional[int]
    company_id: int
    invoice_number: str
    invoice_date: str
    consignee_name: str
    created_by: int
    lead_id: Optional[int] = None
    quotation_id: Optional[int] = None
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
    transhipment: Optional[str] = None
    partial_shipment: Optional[str] = None
    variation_in_qty: Optional[str] = None
    delivery_period: Optional[str] = None
    container_details: Optional[str] = None
    terms_of_delivery: Optional[str] = None
    payment_terms: Optional[str] = None
    remarks: Optional[str] = None
    sea_freight: float = 0
    insurance: float = 0
    certification: float = 0
    other_charges: float = 0
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
    items: List[ProformaInvoiceItem] = field(default_factory=list)
    computed_subtotal_usd: Optional[float] = None  # precomputed by list queries that don't load items

    @staticmethod
    def from_row(row) -> "ProformaInvoice":
        return ProformaInvoice(
            id=row["id"],
            company_id=row["company_id"],
            invoice_number=row["invoice_number"],
            invoice_date=row["invoice_date"],
            lead_id=row["lead_id"],
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
            vessel_flight=row["vessel_flight"],
            port_of_loading=row["port_of_loading"],
            port_of_discharge=row["port_of_discharge"],
            final_destination=row["final_destination"],
            transhipment=row["transhipment"],
            partial_shipment=row["partial_shipment"],
            variation_in_qty=row["variation_in_qty"],
            delivery_period=row["delivery_period"],
            container_details=row["container_details"],
            terms_of_delivery=row["terms_of_delivery"],
            payment_terms=row["payment_terms"],
            remarks=row["remarks"],
            sea_freight=row["sea_freight"],
            insurance=row["insurance"],
            certification=row["certification"],
            other_charges=row["other_charges"],
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
        return (self.subtotal_usd + self.sea_freight + self.insurance
                + self.certification + self.other_charges - self.discount_amount)

# ============================================================
# PACKING LIST  ("Packing Details" document generated from a Proforma
# Invoice - header + line items. proforma_invoice_id/lead_id follow the
# same "generated from, reference only" pattern as ProformaInvoice's
# quotation_id/lead_id.)
# ============================================================
@dataclass
class PackingListItem:
    id: Optional[int]
    packing_list_id: Optional[int]
    sr_no: int
    description: str
    box_per_pallet: Optional[float] = None
    model_name: Optional[str] = None
    no_of_pallet: Optional[float] = None
    boxes: Optional[float] = None
    pcs: Optional[float] = None
    quantity_value: Optional[float] = None

    @property
    def is_heading(self) -> bool:
        """A row with no numbers at all prints as a section heading (e.g.
        'CERAMIC GLAZED VITRIFIED TILES - HSNC 69072100') instead of a
        numbered product line."""
        return not any((self.box_per_pallet, self.no_of_pallet, self.boxes, self.pcs, self.quantity_value))

    @staticmethod
    def from_row(row) -> "PackingListItem":
        return PackingListItem(
            id=row["id"],
            packing_list_id=row["packing_list_id"],
            sr_no=row["sr_no"],
            description=row["description"],
            box_per_pallet=row["box_per_pallet"],
            model_name=row["model_name"],
            no_of_pallet=row["no_of_pallet"],
            boxes=row["boxes"],
            pcs=row["pcs"],
            quantity_value=row["quantity_value"],
        )


@dataclass
class PackingList:
    id: Optional[int]
    company_id: int
    packing_date: str
    created_by: int
    proforma_invoice_id: Optional[int] = None
    proforma_invoice_no: Optional[str] = None
    lead_id: Optional[int] = None
    remarks: Optional[str] = "MADE IN INDIA"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by_name: Optional[str] = None  # populated by joined queries only
    items: List[PackingListItem] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "PackingList":
        return PackingList(
            id=row["id"],
            company_id=row["company_id"],
            proforma_invoice_id=row["proforma_invoice_id"],
            proforma_invoice_no=row["proforma_invoice_no"],
            lead_id=row["lead_id"],
            packing_date=row["packing_date"],
            remarks=row["remarks"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
        )

    @property
    def total_pallets(self) -> float:
        return sum(item.no_of_pallet or 0 for item in self.items)

    @property
    def total_boxes(self) -> float:
        return sum(item.boxes or 0 for item in self.items)

    @property
    def total_pcs(self) -> float:
        return sum(item.pcs or 0 for item in self.items)

    @property
    def total_quantity(self) -> float:
        return sum(item.quantity_value or 0 for item in self.items)
