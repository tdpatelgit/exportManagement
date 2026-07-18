"""
app/repositories.py
--------------------
The Repository layer: every class here knows how to load/save exactly ONE
kind of entity, and nothing else (Single Responsibility). Services depend on
these classes' abstract base classes, not on SQLite (Dependency Inversion) -
so a future PostgreSQL-backed repository could be dropped in by implementing
the same ABC, with zero changes to services or routes.

Each concrete repository is (Interface Segregation) - a UserRepository has
no idea what a Lead is, a LeadRepository has no idea how payments work, etc.
"""

import json
from abc import ABC, abstractmethod
from typing import Optional, List

from app.database import Database
from app.models import (
    Tenant, User, Lead, Client, ContactPerson, Communication,
    PaymentEntry, DocumentEntry, OurCompany, Category, Product, ProductPalletType, ProductFolder, Design,
    Quotation, QuotationItem, ProformaInvoice, ProformaInvoiceItem,
    PackingList, PackingListItem, DocumentVersion,
)


# ============================================================
# TENANT REPOSITORY (the company/workspace picker - NOT the same thing as
# CompanyRepository below, which manages one tenant's own business profile)
# ============================================================
class TenantRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_active(self) -> List[Tenant]:
        rows = self.db.query("SELECT * FROM tenants WHERE is_active = 1 ORDER BY name")
        return [Tenant.from_row(r) for r in rows]

    def get_by_id(self, company_id: int) -> Optional[Tenant]:
        row = self.db.query_one("SELECT * FROM tenants WHERE id = ?", (company_id,))
        return Tenant.from_row(row) if row else None

    def is_active(self, company_id: int) -> bool:
        row = self.db.query_one("SELECT is_active FROM tenants WHERE id = ?", (company_id,))
        return bool(row["is_active"]) if row else False

    def create(self, name: str, slug: str) -> Tenant:
        new_id = self.db.execute("INSERT INTO tenants (name, slug) VALUES (?, ?)", (name, slug))
        return self.get_by_id(new_id)


# ============================================================
# USER REPOSITORY
# ============================================================
class UserRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, user_id: int) -> Optional[User]: ...

    @abstractmethod
    def get_by_username(self, company_id: int, username: str) -> Optional[User]: ...

    @abstractmethod
    def list_all(self, company_id: int, role: Optional[str] = None) -> List[User]: ...

    @abstractmethod
    def create(self, user: User) -> User: ...

    @abstractmethod
    def set_active(self, user_id: int, is_active: bool) -> None: ...

    @abstractmethod
    def update_username(self, user_id: int, username: str) -> None: ...

    @abstractmethod
    def update_password_hash(self, user_id: int, password_hash: str) -> None: ...


class SqliteUserRepository(UserRepositoryBase):
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, user_id: int) -> Optional[User]:
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        return User.from_row(row) if row else None

    def get_by_username(self, company_id: int, username: str) -> Optional[User]:
        row = self.db.query_one(
            "SELECT * FROM users WHERE company_id = ? AND username = ?", (company_id, username)
        )
        return User.from_row(row) if row else None

    def list_all(self, company_id: int, role: Optional[str] = None) -> List[User]:
        if role:
            rows = self.db.query(
                "SELECT * FROM users WHERE company_id = ? AND role = ? ORDER BY full_name",
                (company_id, role),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM users WHERE company_id = ? ORDER BY full_name", (company_id,)
            )
        return [User.from_row(r) for r in rows]

    def create(self, user: User) -> User:
        new_id = self.db.execute(
            """INSERT INTO users (company_id, username, password_hash, full_name, role, is_active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user.company_id, user.username, user.password_hash, user.full_name,
             user.role, int(user.is_active)),
        )
        user.id = new_id
        return user

    def set_active(self, user_id: int, is_active: bool) -> None:
        self.db.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(is_active), user_id))

    def update_username(self, user_id: int, username: str) -> None:
        self.db.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))

    def update_password_hash(self, user_id: int, password_hash: str) -> None:
        self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


# ============================================================
# CONTACT REPOSITORY (shared shape for lead_contacts / client_contacts)
# ============================================================
class ContactRepository:
    """Not behind an ABC on purpose - it's a small internal helper used by
    LeadRepository and ClientRepository, not injected into services
    directly, so an interface isn't pulling its weight here."""

    def __init__(self, db: Database, table: str, fk_column: str):
        self.db = db
        self.table = table          # 'lead_contacts' | 'client_contacts'
        self.fk_column = fk_column  # 'lead_id' | 'client_id'

    def list_for(self, parent_id: int) -> List[ContactPerson]:
        rows = self.db.query(
            f"SELECT * FROM {self.table} WHERE {self.fk_column} = ? ORDER BY is_primary DESC, id",
            (parent_id,),
        )
        return [ContactPerson.from_row(r) for r in rows]

    def add(self, parent_id: int, contact: ContactPerson) -> ContactPerson:
        new_id = self.db.execute(
            f"""INSERT INTO {self.table} (name, phone, email, is_primary, {self.fk_column})
                VALUES (?, ?, ?, ?, ?)""",
            (contact.name, contact.phone, contact.email, int(contact.is_primary), parent_id),
        )
        contact.id = new_id
        return contact

    def set_primary(self, parent_id: int, contact_id: int) -> None:
        """Marks one contact as the primary and un-marks every other contact
        under the same parent, so there's always at most one primary."""
        with self.db.get_connection() as conn:
            conn.execute(
                f"UPDATE {self.table} SET is_primary = 0 WHERE {self.fk_column} = ?",
                (parent_id,),
            )
            conn.execute(
                f"UPDATE {self.table} SET is_primary = 1 WHERE id = ? AND {self.fk_column} = ?",
                (contact_id, parent_id),
            )


# ============================================================
# LEAD REPOSITORY
# ============================================================
class LeadRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, lead_id: int) -> Optional[Lead]: ...

    @abstractmethod
    def list_all(self, company_id: int, employee_id: Optional[int] = None,
                 status: Optional[str] = None) -> List[Lead]: ...

    @abstractmethod
    def create(self, lead: Lead) -> Lead: ...

    @abstractmethod
    def update_compulsory_fields(self, lead_id: int, fields: dict) -> None: ...

    @abstractmethod
    def update_status(self, lead_id: int, status: str) -> None: ...

    @abstractmethod
    def count_by_employee(self, company_id: int) -> dict: ...


class SqliteLeadRepository(LeadRepositoryBase):
    def __init__(self, db: Database):
        self.db = db
        self.contacts = ContactRepository(db, "lead_contacts", "lead_id")

    _SELECT = """
        SELECT leads.*, users.full_name AS created_by_name
        FROM leads JOIN users ON users.id = leads.created_by
    """

    def get_by_id(self, lead_id: int) -> Optional[Lead]:
        row = self.db.query_one(self._SELECT + " WHERE leads.id = ?", (lead_id,))
        if not row:
            return None
        lead = Lead.from_row(row)
        lead.contacts = self.contacts.list_for(lead_id)
        return lead

    def list_all(self, company_id: int, employee_id: Optional[int] = None,
                 status: Optional[str] = None) -> List[Lead]:
        sql = self._SELECT + " WHERE leads.company_id = ?"
        params: list = [company_id]
        if employee_id:
            sql += " AND leads.created_by = ?"
            params.append(employee_id)
        if status:
            sql += " AND leads.status = ?"
            params.append(status)
        sql += " ORDER BY leads.created_at DESC"
        return [Lead.from_row(r) for r in self.db.query(sql, tuple(params))]

    def create(self, lead: Lead) -> Lead:
        new_id = self.db.execute(
            """INSERT INTO leads (company_id, company_name, phone, email, facebook, instagram,
                                   other_social, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead.company_id, lead.company_name, lead.phone, lead.email, lead.facebook, lead.instagram,
             lead.other_social, lead.status, lead.created_by),
        )
        lead.id = new_id
        for contact in lead.contacts:
            self.contacts.add(new_id, contact)
        return lead

    def update_compulsory_fields(self, lead_id: int, fields: dict) -> None:
        """Admin-only edit of company_name/phone/email (per the brief: 'Any
        changes to compulsory fields must be done by admins only'). Callers
        must enforce the role check - this method just performs the write."""
        self.db.execute(
            """UPDATE leads SET company_name = ?, phone = ?, email = ?,
                                 facebook = ?, instagram = ?, other_social = ?,
                                 updated_at = datetime('now')
               WHERE id = ?""",
            (fields["company_name"], fields["phone"], fields["email"],
             fields.get("facebook"), fields.get("instagram"), fields.get("other_social"),
             lead_id),
        )

    def update_status(self, lead_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE leads SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, lead_id),
        )

    def count_by_employee(self, company_id: int) -> dict:
        """Returns {employee_id: lead_count} - powers the admin dashboard."""
        rows = self.db.query(
            "SELECT created_by, COUNT(*) AS cnt FROM leads WHERE company_id = ? GROUP BY created_by",
            (company_id,),
        )
        return {r["created_by"]: r["cnt"] for r in rows}


# ============================================================
# CLIENT REPOSITORY
# ============================================================
class ClientRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, client_id: int) -> Optional[Client]: ...

    @abstractmethod
    def list_all(self, company_id: int, client_type: Optional[str] = None,
                 status: Optional[str] = None) -> List[Client]: ...

    @abstractmethod
    def convert_from_lead(self, client: Client, lead_contacts: List[ContactPerson]) -> Client: ...

    @abstractmethod
    def update_status(self, client_id: int, status: str) -> None: ...

    @abstractmethod
    def update_compulsory_fields(self, client_id: int, fields: dict) -> None: ...


class SqliteClientRepository(ClientRepositoryBase):
    def __init__(self, db: Database):
        self.db = db
        self.contacts = ContactRepository(db, "client_contacts", "client_id")

    def get_by_id(self, client_id: int) -> Optional[Client]:
        row = self.db.query_one("SELECT * FROM clients WHERE id = ?", (client_id,))
        if not row:
            return None
        client = Client.from_row(row)
        client.contacts = self.contacts.list_for(client_id)
        return client

    def list_all(self, company_id: int, client_type: Optional[str] = None,
                 status: Optional[str] = None) -> List[Client]:
        sql = "SELECT * FROM clients WHERE company_id = ?"
        params: list = [company_id]
        if client_type:
            sql += " AND client_type = ?"
            params.append(client_type)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return [Client.from_row(r) for r in self.db.query(sql, tuple(params))]

    def convert_from_lead(self, client: Client, lead_contacts: List[ContactPerson]) -> Client:
        """Creates the client, copies every lead contact person across, and
        marks the originating lead as converted - all inside ONE transaction.
        This has to be atomic: previously the client row, its contacts, and
        the lead's converted flag were written in three separate
        transactions, so a failure on the last write (e.g. a status value
        the DB didn't allow yet) left a client already created but the lead
        still un-converted - and every retry created another duplicate
        client."""
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO clients (company_id, lead_id, company_name, phone, email, facebook,
                                         instagram, other_social, client_type, status, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (client.company_id, client.lead_id, client.company_name, client.phone, client.email,
                 client.facebook, client.instagram, client.other_social, client.client_type,
                 client.status, client.created_by),
            )
            client.id = cursor.lastrowid
            for contact in lead_contacts:
                conn.execute(
                    """INSERT INTO client_contacts (name, phone, email, is_primary, client_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (contact.name, contact.phone, contact.email, int(contact.is_primary), client.id),
                )
            conn.execute(
                "UPDATE leads SET is_converted = 1, converted_client_id = ?, status = 'in_client', "
                "updated_at = datetime('now') WHERE id = ?",
                (client.id, client.lead_id),
            )
        return client

    def update_status(self, client_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE clients SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, client_id),
        )

    def update_compulsory_fields(self, client_id: int, fields: dict) -> None:
        self.db.execute(
            """UPDATE clients SET company_name = ?, phone = ?, email = ?,
                                   facebook = ?, instagram = ?, other_social = ?,
                                   address = ?, client_type = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (fields["company_name"], fields["phone"], fields["email"],
             fields.get("facebook"), fields.get("instagram"), fields.get("other_social"),
             fields.get("address"), fields["client_type"], client_id),
        )


# ============================================================
# COMMUNICATION REPOSITORY (shared by Lead and Client)
# ============================================================
class CommunicationRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_for(self, parent_type: str, parent_id: int) -> List[Communication]:
        rows = self.db.query(
            """SELECT communications.*, users.full_name AS employee_name
               FROM communications JOIN users ON users.id = communications.employee_id
               WHERE parent_type = ? AND parent_id = ?
               ORDER BY comm_date DESC, communications.id DESC""",
            (parent_type, parent_id),
        )
        return [Communication.from_row(r) for r in rows]

    def add(self, comm: Communication) -> Communication:
        new_id = self.db.execute(
            """INSERT INTO communications (parent_type, parent_id, employee_id, comm_date,
                                            mode, description, follow_up_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (comm.parent_type, comm.parent_id, comm.employee_id, comm.comm_date,
             comm.mode, comm.description, comm.follow_up_date),
        )
        comm.id = new_id
        return comm

    def count_by_employee(self, company_id: int) -> dict:
        """{employee_id: communication_count} - powers the admin dashboard.
        `communications` has no company_id of its own - joined through the
        employee who logged it, which is always same-company by construction
        (an employee can only log communications against their own leads/
        clients, which are already company-scoped)."""
        rows = self.db.query(
            """SELECT c.employee_id, COUNT(*) AS cnt FROM communications c
               JOIN users u ON u.id = c.employee_id
               WHERE u.company_id = ? GROUP BY c.employee_id""",
            (company_id,),
        )
        return {r["employee_id"]: r["cnt"] for r in rows}

    def upcoming_followups(self, company_id: int, employee_id: Optional[int],
                            within_days: int) -> List[Communication]:
        """Communications whose follow_up_date is today or overdue, used for
        the employee notification panel."""
        sql = """
            SELECT communications.*, users.full_name AS employee_name
            FROM communications JOIN users ON users.id = communications.employee_id
            WHERE users.company_id = ?
              AND follow_up_date IS NOT NULL
              AND date(follow_up_date) <= date('now', ?)
        """
        params: list = [company_id, f"+{within_days} days"]
        if employee_id:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        sql += " ORDER BY date(follow_up_date) ASC"
        return [Communication.from_row(r) for r in self.db.query(sql, tuple(params))]


# ============================================================
# PAYMENT REPOSITORY
# ============================================================
class PaymentRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_for_client(self, client_id: int) -> List[PaymentEntry]:
        rows = self.db.query(
            "SELECT * FROM payment_history WHERE client_id = ? ORDER BY payment_datetime DESC",
            (client_id,),
        )
        return [PaymentEntry.from_row(r) for r in rows]

    def add(self, payment: PaymentEntry) -> PaymentEntry:
        new_id = self.db.execute(
            """INSERT INTO payment_history (client_id, account_name, payment_datetime,
                                              amount_original, currency_code, conversion_rate, amount_inr)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payment.client_id, payment.account_name, payment.payment_datetime,
             payment.amount_original, payment.currency_code, payment.conversion_rate,
             payment.amount_inr),
        )
        payment.id = new_id
        return payment


# ============================================================
# DOCUMENT REPOSITORY
# ============================================================
class DocumentRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_for_client(self, client_id: int) -> List[DocumentEntry]:
        rows = self.db.query(
            "SELECT * FROM documents WHERE client_id = ? ORDER BY document_date DESC",
            (client_id,),
        )
        return [DocumentEntry.from_row(r) for r in rows]

    def add(self, doc: DocumentEntry) -> DocumentEntry:
        new_id = self.db.execute(
            """INSERT INTO documents (client_id, document_name, document_type, document_date, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (doc.client_id, doc.document_name, doc.document_type, doc.document_date, doc.notes),
        )
        doc.id = new_id
        return doc


# ============================================================
# OUR COMPANY REPOSITORY (one row per tenant - that tenant's own business
# profile shown on quotations. NOT the same thing as TenantRepository above,
# which manages the `tenants` login/workspace table.)
# ============================================================
class CompanyRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, company_id: int) -> Optional[OurCompany]:
        row = self.db.query_one("SELECT * FROM our_company WHERE company_id = ?", (company_id,))
        if not row:
            return None
        company = OurCompany.from_row(row)
        company.contact_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_contact_details WHERE our_company_id = ? ORDER BY is_primary DESC, id",
                (company.id,),
            )
        ]
        company.contact_persons = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_contact_persons WHERE our_company_id = ? ORDER BY is_primary DESC, id",
                (company.id,),
            )
        ]
        company.bank_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_bank_details WHERE our_company_id = ? ORDER BY is_primary DESC, id",
                (company.id,),
            )
        ]
        company.lut_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_lut_details WHERE our_company_id = ? "
                "ORDER BY is_primary DESC, financial_year DESC, id",
                (company.id,),
            )
        ]
        return company

    def upsert(self, company_id: int, company_name: str, address: str, gstin: str,
               pan_no: str, iec: str, bin_no: str) -> int:
        """Returns the `our_company.id` row (not the tenant's company_id) -
        callers need it to scope the four detail-table replace_* calls."""
        existing = self.db.query_one("SELECT id FROM our_company WHERE company_id = ?", (company_id,))
        if existing:
            self.db.execute(
                """UPDATE our_company SET company_name = ?, address = ?, gstin = ?, pan_no = ?, iec = ?, bin = ?,
                                           updated_at = datetime('now') WHERE company_id = ?""",
                (company_name, address, gstin, pan_no, iec, bin_no, company_id),
            )
            return existing["id"]
        return self.db.execute(
            "INSERT INTO our_company (company_id, company_name, address, gstin, pan_no, iec, bin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, company_name, address, gstin, pan_no, iec, bin_no),
        )

    def replace_lut_details(self, our_company_id: int, lut_details: list) -> None:
        """lut_details: [{'lut_number': str, 'financial_year': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_lut_details WHERE our_company_id = ?", (our_company_id,))
            for l in lut_details:
                conn.execute(
                    "INSERT INTO our_company_lut_details (our_company_id, lut_number, financial_year, is_primary) "
                    "VALUES (?, ?, ?, ?)",
                    (our_company_id, l["lut_number"], l["financial_year"], int(l["is_primary"])),
                )

    def replace_contact_details(self, our_company_id: int, details: list) -> None:
        """details: [{'type': 'phone'|'email', 'value': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_contact_details WHERE our_company_id = ?", (our_company_id,))
            for d in details:
                conn.execute(
                    "INSERT INTO our_company_contact_details (our_company_id, type, value, is_primary) "
                    "VALUES (?, ?, ?, ?)",
                    (our_company_id, d["type"], d["value"], int(d["is_primary"])),
                )

    def replace_contact_persons(self, our_company_id: int, persons: list) -> None:
        """persons: [{'name': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_contact_persons WHERE our_company_id = ?", (our_company_id,))
            for p in persons:
                conn.execute(
                    "INSERT INTO our_company_contact_persons (our_company_id, name, is_primary) VALUES (?, ?, ?)",
                    (our_company_id, p["name"], int(p["is_primary"])),
                )

    def replace_bank_details(self, our_company_id: int, bank_details: list) -> None:
        """bank_details: [{'bank_name': str, 'account_number': str, 'ifsc_code': str,
        'swift_code': str, 'branch': str, 'bank_address': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_bank_details WHERE our_company_id = ?", (our_company_id,))
            for b in bank_details:
                conn.execute(
                    """INSERT INTO our_company_bank_details
                       (our_company_id, bank_name, account_number, ifsc_code, swift_code, branch, bank_address, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (our_company_id, b["bank_name"], b["account_number"], b.get("ifsc_code") or None,
                     b.get("swift_code") or None, b.get("branch") or None,
                     b.get("bank_address") or None, int(b["is_primary"])),
                )


# ============================================================
# PRODUCT CATALOG (products -> folders -> designs)
# ============================================================
class CategoryRepository:
    """Categories are folders at the catalog root that group products, and
    (like sub categories inside a product) nest to any depth via a
    self-referencing parent_id."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, category_id: int) -> Optional[Category]:
        row = self.db.query_one("SELECT * FROM categories WHERE id = ?", (category_id,))
        return Category.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[Category]:
        """Every category, flat - powers the product form's category picker."""
        rows = self.db.query(
            "SELECT * FROM categories WHERE company_id = ? ORDER BY name", (company_id,)
        )
        return [Category.from_row(r) for r in rows]

    def list_children(self, company_id: int, parent_id: Optional[int]) -> List[Category]:
        if parent_id is None:
            rows = self.db.query(
                "SELECT * FROM categories WHERE company_id = ? AND parent_id IS NULL ORDER BY name",
                (company_id,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM categories WHERE company_id = ? AND parent_id = ? ORDER BY name",
                (company_id, parent_id),
            )
        return [Category.from_row(r) for r in rows]

    def list_ancestors(self, category_id: int) -> List[Category]:
        """Walks parent_id up to the catalog root - powers the breadcrumb trail."""
        trail = []
        current = self.get_by_id(category_id)
        while current:
            trail.append(current)
            current = self.get_by_id(current.parent_id) if current.parent_id else None
        trail.reverse()
        return trail

    def list_descendant_ids(self, category_id: int) -> List[int]:
        """category_id plus every category nested under it, at any depth -
        used to block moving a category inside its own subtree."""
        rows = self.db.query(
            """WITH RECURSIVE subtree(id) AS (
                   SELECT ?
                   UNION ALL
                   SELECT c.id FROM categories c JOIN subtree s ON c.parent_id = s.id
               )
               SELECT id FROM subtree""",
            (category_id,),
        )
        return [r["id"] for r in rows]

    def create(self, company_id: int, name: str, parent_id: Optional[int] = None) -> Category:
        new_id = self.db.execute(
            "INSERT INTO categories (company_id, name, parent_id) VALUES (?, ?, ?)",
            (company_id, name, parent_id),
        )
        return self.get_by_id(new_id)

    def update(self, category_id: int, fields: dict) -> None:
        """fields may include name and/or parent_id."""
        columns = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE categories SET {columns} WHERE id = ?",
            (*fields.values(), category_id),
        )

    def delete(self, category_id: int) -> None:
        """Cascades to subcategories and their products via ON DELETE CASCADE
        at the DB level, but the service walks the subtree first to clean up
        each product's design image files and document line references, the
        same way ProductFolderRepository.delete does for sub categories."""
        self.db.execute("DELETE FROM categories WHERE id = ?", (category_id,))


class ProductRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, product_id: int) -> Optional[Product]:
        row = self.db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
        return Product.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[Product]:
        rows = self.db.query(
            "SELECT * FROM products WHERE company_id = ? ORDER BY product_name", (company_id,)
        )
        return [Product.from_row(r) for r in rows]

    def list_in_category(self, company_id: int, category_id: Optional[int]) -> List[Product]:
        """Products sitting in one category - category_id=None is the catalog root."""
        if category_id is None:
            rows = self.db.query(
                "SELECT * FROM products WHERE company_id = ? AND category_id IS NULL ORDER BY product_name",
                (company_id,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM products WHERE company_id = ? AND category_id = ? ORDER BY product_name",
                (company_id, category_id),
            )
        return [Product.from_row(r) for r in rows]

    def create(self, product: Product) -> Product:
        new_id = self.db.execute(
            """INSERT INTO products
               (company_id, category_id, product_name, description, hsn_code,
                igst_percent, sgst_percent, cgst_percent,
                quantity, alternate_quantity, unit,
                net_weight_kg, gross_weight_kg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product.company_id, product.category_id, product.product_name, product.description,
             product.hsn_code, product.igst_percent, product.sgst_percent, product.cgst_percent,
             product.quantity, product.alternate_quantity, product.unit,
             product.net_weight_kg, product.gross_weight_kg),
        )
        return self.get_by_id(new_id)

    def update(self, product_id: int, fields: dict) -> None:
        """fields may include any column except id/company_id/created_at."""
        columns = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE products SET {columns}, updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), product_id),
        )

    def delete(self, product_id: int) -> None:
        """Cascades to the product's folders and designs via ON DELETE CASCADE.
        Document line items keep their snapshot text (name/HSN) - only the
        catalog reference is nulled out first. Done explicitly rather than
        relying on ON DELETE SET NULL because quotation/proforma item tables
        created before this rule existed don't carry it."""
        with self.db.get_connection() as conn:
            conn.execute("UPDATE quotation_items SET product_id = NULL WHERE product_id = ?", (product_id,))
            conn.execute("UPDATE proforma_invoice_items SET product_id = NULL WHERE product_id = ?", (product_id,))
            conn.execute("UPDATE packing_list_items SET product_id = NULL WHERE product_id = ?", (product_id,))
            conn.execute(
                "UPDATE packing_list_items SET design_id = NULL "
                "WHERE design_id IN (SELECT id FROM designs WHERE product_id = ?)",
                (product_id,),
            )
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


class ProductPalletTypeRepository:
    """The named pallet storage options of each product (the implicit
    "loose" option is never stored). The whole list is replaced in one shot
    on every product save - the rows have no children, so delete + reinsert
    is simpler and safer than diffing."""

    def __init__(self, db: Database):
        self.db = db

    def list_for_product(self, product_id: int) -> List[ProductPalletType]:
        rows = self.db.query(
            "SELECT * FROM product_pallet_types WHERE product_id = ? ORDER BY sort_order, id",
            (product_id,),
        )
        return [ProductPalletType.from_row(r) for r in rows]

    def list_all(self, company_id: int) -> List[ProductPalletType]:
        """Every pallet type of every product in one company - lets the
        product-picker JSON API attach each product's list without one
        query per product."""
        rows = self.db.query(
            "SELECT * FROM product_pallet_types WHERE company_id = ? ORDER BY product_id, sort_order, id",
            (company_id,),
        )
        return [ProductPalletType.from_row(r) for r in rows]

    def replace_for_product(self, company_id: int, product_id: int,
                             pallet_types: List[ProductPalletType]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM product_pallet_types WHERE product_id = ?", (product_id,))
            for order, pt in enumerate(pallet_types):
                conn.execute(
                    "INSERT INTO product_pallet_types (company_id, product_id, name, boxes_per_pallet, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (company_id, product_id, pt.name, pt.boxes_per_pallet, order),
                )


class ProductFolderRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, folder_id: int) -> Optional[ProductFolder]:
        row = self.db.query_one("SELECT * FROM product_folders WHERE id = ?", (folder_id,))
        return ProductFolder.from_row(row) if row else None

    def list_children(self, product_id: int, parent_id: Optional[int]) -> List[ProductFolder]:
        if parent_id is None:
            rows = self.db.query(
                "SELECT * FROM product_folders WHERE product_id = ? AND parent_id IS NULL ORDER BY name",
                (product_id,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM product_folders WHERE product_id = ? AND parent_id = ? ORDER BY name",
                (product_id, parent_id),
            )
        return [ProductFolder.from_row(r) for r in rows]

    def list_ancestors(self, folder_id: int) -> List[ProductFolder]:
        """Walks parent_id up to the product's top level - powers the breadcrumb trail."""
        trail = []
        current = self.get_by_id(folder_id)
        while current:
            trail.append(current)
            current = self.get_by_id(current.parent_id) if current.parent_id else None
        trail.reverse()
        return trail

    def create(self, company_id: int, product_id: int, name: str, parent_id: Optional[int]) -> ProductFolder:
        new_id = self.db.execute(
            "INSERT INTO product_folders (company_id, product_id, name, parent_id) VALUES (?, ?, ?, ?)",
            (company_id, product_id, name, parent_id),
        )
        return self.get_by_id(new_id)

    def update(self, folder_id: int, name: str) -> None:
        self.db.execute("UPDATE product_folders SET name = ? WHERE id = ?", (name, folder_id))

    def delete(self, folder_id: int) -> None:
        """Cascades to subfolders and designs via ON DELETE CASCADE. Packing
        list lines keep their design_name snapshot - the design reference is
        nulled for every design in the folder's subtree first."""
        with self.db.get_connection() as conn:
            conn.execute(
                """UPDATE packing_list_items SET design_id = NULL WHERE design_id IN (
                       WITH RECURSIVE subtree(id) AS (
                           SELECT ?
                           UNION ALL
                           SELECT pf.id FROM product_folders pf JOIN subtree s ON pf.parent_id = s.id
                       )
                       SELECT d.id FROM designs d WHERE d.folder_id IN (SELECT id FROM subtree)
                   )""",
                (folder_id,),
            )
            conn.execute("DELETE FROM product_folders WHERE id = ?", (folder_id,))


class DesignRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, design_id: int) -> Optional[Design]:
        row = self.db.query_one("SELECT * FROM designs WHERE id = ?", (design_id,))
        return Design.from_row(row) if row else None

    def list_in(self, product_id: int, folder_id: Optional[int]) -> List[Design]:
        """Designs sitting in one folder - folder_id=None is the product's top level."""
        if folder_id is None:
            rows = self.db.query(
                "SELECT * FROM designs WHERE product_id = ? AND folder_id IS NULL ORDER BY design_name",
                (product_id,),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM designs WHERE product_id = ? AND folder_id = ? ORDER BY design_name",
                (product_id, folder_id),
            )
        return [Design.from_row(r) for r in rows]

    def list_for_product(self, product_id: int) -> List[Design]:
        """Every design anywhere under the product, regardless of folder."""
        rows = self.db.query(
            "SELECT * FROM designs WHERE product_id = ? ORDER BY design_name", (product_id,)
        )
        return [Design.from_row(r) for r in rows]

    def create(self, design: Design) -> Design:
        new_id = self.db.execute(
            """INSERT INTO designs
               (company_id, product_id, folder_id, design_name, description, surface,
                price_usd, photo_path, dimension_photo_path, alt_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (design.company_id, design.product_id, design.folder_id, design.design_name,
             design.description, design.surface, design.price_usd, design.photo_path,
             design.dimension_photo_path, design.alt_text),
        )
        return self.get_by_id(new_id)

    def update(self, design_id: int, fields: dict) -> None:
        """fields may include any column except id/product_id/created_at."""
        columns = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE designs SET {columns}, updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), design_id),
        )

    def delete(self, design_id: int) -> None:
        """Packing list lines keep their design_name snapshot - only the
        catalog reference is nulled out."""
        with self.db.get_connection() as conn:
            conn.execute("UPDATE packing_list_items SET design_id = NULL WHERE design_id = ?", (design_id,))
            conn.execute("DELETE FROM designs WHERE id = ?", (design_id,))


# ============================================================
# QUOTATION REPOSITORY (header + line items)
# ============================================================
class QuotationRepository:
    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        """Counts existing quotations whose number starts with QT{YYYYMMDD} -
        used to compute the next sequence for that day. Scoped per company so
        two tenants generating a quotation on the same day both start at 001."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM quotations WHERE company_id = ? AND quotation_number LIKE ?",
            (company_id, f"{number_prefix}%"),
        )
        return row["cnt"] if row else 0

    def get_by_id(self, quotation_id: int) -> Optional[Quotation]:
        row = self.db.query_one(
            """SELECT q.*, u.full_name AS created_by_name FROM quotations q
               JOIN users u ON u.id = q.created_by WHERE q.id = ?""",
            (quotation_id,),
        )
        if not row:
            return None
        quotation = Quotation.from_row(row)
        item_rows = self.db.query(
            "SELECT * FROM quotation_items WHERE quotation_id = ? ORDER BY sr_no", (quotation_id,)
        )
        quotation.items = [QuotationItem.from_row(r) for r in item_rows]
        return quotation

    def list_all(self, company_id: int) -> List[Quotation]:
        rows = self.db.query(
            """SELECT q.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM quotation_items WHERE quotation_id = q.id), 0) AS items_total
               FROM quotations q
               JOIN users u ON u.id = q.created_by
               WHERE q.company_id = ?
               ORDER BY q.quotation_date DESC, q.id DESC""",
            (company_id,),
        )
        return [Quotation.from_row(r) for r in rows]

    def list_for_lead(self, lead_id: int) -> List[Quotation]:
        """Quotations created against a given lead. This is also how a
        converted client 'sees' its quotations - a client never has its own
        quotation link; the client's originating `lead_id` (Client.lead_id)
        is reused to look them up here, so a quotation made while the
        company was still a lead automatically stays visible once it
        becomes a client, with nothing to keep in sync by hand."""
        rows = self.db.query(
            """SELECT q.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM quotation_items WHERE quotation_id = q.id), 0) AS items_total
               FROM quotations q
               JOIN users u ON u.id = q.created_by
               WHERE q.lead_id = ?
               ORDER BY q.quotation_date DESC, q.id DESC""",
            (lead_id,),
        )
        return [Quotation.from_row(r) for r in rows]

    def create(self, quotation: Quotation) -> Quotation:
        new_id = self.db.execute(
            """INSERT INTO quotations
               (company_id, quotation_number, quotation_date, lead_id, buyer_name, buyer_address,
                buyer_reference_no, port_of_loading, port_of_discharge, packing_details,
                container_details, shipping_mode, shipping_terms, payment_terms,
                price_validity_days, remarks,
                sea_freight, insurance, certification, other_charges,
                discount_amount, bank_name, bank_account_number, bank_ifsc_code,
                bank_swift_code, bank_branch, bank_address, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (quotation.company_id, quotation.quotation_number, quotation.quotation_date, quotation.lead_id,
             quotation.buyer_name, quotation.buyer_address, quotation.buyer_reference_no,
             quotation.port_of_loading, quotation.port_of_discharge, quotation.packing_details,
             quotation.container_details, quotation.shipping_mode, quotation.shipping_terms,
             quotation.payment_terms,
             quotation.price_validity_days, quotation.remarks,
             quotation.sea_freight, quotation.insurance, quotation.certification, quotation.other_charges,
             quotation.discount_amount,
             quotation.bank_name, quotation.bank_account_number, quotation.bank_ifsc_code,
             quotation.bank_swift_code, quotation.bank_branch, quotation.bank_address,
             quotation.created_by),
        )
        self._replace_items(new_id, quotation.items)
        return self.get_by_id(new_id)

    def update(self, quotation_id: int, quotation: Quotation) -> None:
        self.db.execute(
            """UPDATE quotations SET quotation_date = ?, lead_id = ?, buyer_name = ?,
                                      buyer_address = ?, buyer_reference_no = ?, port_of_loading = ?,
                                      port_of_discharge = ?, packing_details = ?, container_details = ?,
                                      shipping_mode = ?, shipping_terms = ?, payment_terms = ?,
                                      price_validity_days = ?,
                                      remarks = ?, sea_freight = ?, insurance = ?, certification = ?,
                                      other_charges = ?, discount_amount = ?, bank_name = ?, bank_account_number = ?,
                                      bank_ifsc_code = ?, bank_swift_code = ?, bank_branch = ?, bank_address = ?,
                                      updated_at = datetime('now')
               WHERE id = ?""",
            (quotation.quotation_date, quotation.lead_id, quotation.buyer_name,
             quotation.buyer_address, quotation.buyer_reference_no, quotation.port_of_loading,
             quotation.port_of_discharge, quotation.packing_details, quotation.container_details,
             quotation.shipping_mode, quotation.shipping_terms, quotation.payment_terms,
             quotation.price_validity_days,
             quotation.remarks, quotation.sea_freight, quotation.insurance, quotation.certification,
             quotation.other_charges, quotation.discount_amount, quotation.bank_name,
             quotation.bank_account_number, quotation.bank_ifsc_code, quotation.bank_swift_code,
             quotation.bank_branch, quotation.bank_address, quotation_id),
        )
        self._replace_items(quotation_id, quotation.items)

    def _replace_items(self, quotation_id: int, items: List[QuotationItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (quotation_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO quotation_items
                       (quotation_id, sr_no, product_id, product_name, dimension_mm, hsn_code,
                        quantity_boxes, quantity_value, unit, price_usd, total_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (quotation_id, item.sr_no, item.product_id, item.product_name, item.dimension_mm,
                     item.hsn_code, item.quantity_boxes, item.quantity_value, item.unit,
                     item.price_usd, item.total_usd),
                )

    def delete(self, quotation_id: int) -> None:
        self.db.execute("DELETE FROM quotations WHERE id = ?", (quotation_id,))


class ProformaInvoiceRepository:
    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        """Same purpose as QuotationRepository.count_for_date_prefix - used to
        compute the next PI{YYYYMMDD} sequence for the day, scoped per company."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM proforma_invoices WHERE company_id = ? AND invoice_number LIKE ?",
            (company_id, f"{number_prefix}%"),
        )
        return row["cnt"] if row else 0

    def get_by_id(self, invoice_id: int) -> Optional[ProformaInvoice]:
        row = self.db.query_one(
            """SELECT pi.*, u.full_name AS created_by_name FROM proforma_invoices pi
               JOIN users u ON u.id = pi.created_by WHERE pi.id = ?""",
            (invoice_id,),
        )
        if not row:
            return None
        invoice = ProformaInvoice.from_row(row)
        item_rows = self.db.query(
            "SELECT * FROM proforma_invoice_items WHERE proforma_invoice_id = ? ORDER BY sr_no", (invoice_id,)
        )
        invoice.items = [ProformaInvoiceItem.from_row(r) for r in item_rows]
        return invoice

    def list_all(self, company_id: int) -> List[ProformaInvoice]:
        rows = self.db.query(
            """SELECT pi.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM proforma_invoice_items WHERE proforma_invoice_id = pi.id), 0) AS items_total
               FROM proforma_invoices pi
               JOIN users u ON u.id = pi.created_by
               WHERE pi.company_id = ?
               ORDER BY pi.invoice_date DESC, pi.id DESC""",
            (company_id,),
        )
        return [ProformaInvoice.from_row(r) for r in rows]

    def list_for_lead(self, lead_id: int) -> List[ProformaInvoice]:
        """Same 'reference-only' join pattern as QuotationRepository.list_for_lead -
        a converted client sees its proforma invoices through its originating
        lead_id, nothing to keep in sync by hand."""
        rows = self.db.query(
            """SELECT pi.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM proforma_invoice_items WHERE proforma_invoice_id = pi.id), 0) AS items_total
               FROM proforma_invoices pi
               JOIN users u ON u.id = pi.created_by
               WHERE pi.lead_id = ?
               ORDER BY pi.invoice_date DESC, pi.id DESC""",
            (lead_id,),
        )
        return [ProformaInvoice.from_row(r) for r in rows]

    def list_for_quotation(self, quotation_id: int) -> List[ProformaInvoice]:
        """Every proforma invoice generated from this quotation, newest first -
        used to link back to an already-generated PI instead of starting a
        duplicate one."""
        rows = self.db.query(
            """SELECT pi.*, u.full_name AS created_by_name FROM proforma_invoices pi
               JOIN users u ON u.id = pi.created_by
               WHERE pi.quotation_id = ?
               ORDER BY pi.id DESC""",
            (quotation_id,),
        )
        return [ProformaInvoice.from_row(r) for r in rows]

    def map_by_quotation(self, company_id: int) -> dict:
        """quotation_id -> most recently created proforma_invoice id, for this
        company. Powers the quotations list page's "View PI" link."""
        rows = self.db.query(
            "SELECT quotation_id, id FROM proforma_invoices WHERE company_id = ? AND quotation_id IS NOT NULL ORDER BY id",
            (company_id,),
        )
        result = {}
        for row in rows:
            result[row["quotation_id"]] = row["id"]
        return result

    def create(self, invoice: ProformaInvoice) -> ProformaInvoice:
        new_id = self.db.execute(
            """INSERT INTO proforma_invoices
               (company_id, invoice_number, invoice_date, lead_id, quotation_id, export_ref_no,
                buyer_order_no, other_reference, consignee_name, consignee_address, notify_name,
                notify_address, country_of_origin, country_of_destination,
                port_of_loading, port_of_discharge, final_destination, transhipment, partial_shipment,
                variation_in_qty, delivery_period, container_details, terms_of_delivery, payment_terms,
                remarks, sea_freight, insurance, certification, other_charges, discount_amount,
                bank_name, bank_account_number, bank_ifsc_code, bank_swift_code, bank_branch,
                bank_address, display_mode, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice.company_id, invoice.invoice_number, invoice.invoice_date, invoice.lead_id,
             invoice.quotation_id, invoice.export_ref_no, invoice.buyer_order_no, invoice.other_reference,
             invoice.consignee_name, invoice.consignee_address, invoice.notify_name, invoice.notify_address,
             invoice.country_of_origin, invoice.country_of_destination,
             invoice.port_of_loading, invoice.port_of_discharge, invoice.final_destination,
             invoice.transhipment, invoice.partial_shipment, invoice.variation_in_qty,
             invoice.delivery_period, invoice.container_details, invoice.terms_of_delivery,
             invoice.payment_terms, invoice.remarks, invoice.sea_freight, invoice.insurance,
             invoice.certification, invoice.other_charges, invoice.discount_amount, invoice.bank_name,
             invoice.bank_account_number, invoice.bank_ifsc_code, invoice.bank_swift_code,
             invoice.bank_branch, invoice.bank_address, invoice.display_mode, invoice.created_by),
        )
        self._replace_items(new_id, invoice.items)
        return self.get_by_id(new_id)

    def update(self, invoice_id: int, invoice: ProformaInvoice) -> None:
        self.db.execute(
            """UPDATE proforma_invoices SET invoice_date = ?, lead_id = ?, quotation_id = ?,
                                             export_ref_no = ?, buyer_order_no = ?, other_reference = ?,
                                             consignee_name = ?, consignee_address = ?, notify_name = ?,
                                             notify_address = ?, country_of_origin = ?, country_of_destination = ?,
                                             port_of_loading = ?, port_of_discharge = ?,
                                             final_destination = ?, transhipment = ?, partial_shipment = ?,
                                             variation_in_qty = ?, delivery_period = ?, container_details = ?,
                                             terms_of_delivery = ?, payment_terms = ?, remarks = ?,
                                             sea_freight = ?, insurance = ?, certification = ?,
                                             other_charges = ?, discount_amount = ?, bank_name = ?,
                                             bank_account_number = ?, bank_ifsc_code = ?, bank_swift_code = ?,
                                             bank_branch = ?, bank_address = ?, display_mode = ?,
                                             updated_at = datetime('now')
               WHERE id = ?""",
            (invoice.invoice_date, invoice.lead_id, invoice.quotation_id, invoice.export_ref_no,
             invoice.buyer_order_no, invoice.other_reference, invoice.consignee_name,
             invoice.consignee_address, invoice.notify_name, invoice.notify_address,
             invoice.country_of_origin, invoice.country_of_destination,
             invoice.port_of_loading, invoice.port_of_discharge, invoice.final_destination,
             invoice.transhipment, invoice.partial_shipment, invoice.variation_in_qty,
             invoice.delivery_period, invoice.container_details, invoice.terms_of_delivery,
             invoice.payment_terms, invoice.remarks, invoice.sea_freight, invoice.insurance,
             invoice.certification, invoice.other_charges, invoice.discount_amount, invoice.bank_name,
             invoice.bank_account_number, invoice.bank_ifsc_code, invoice.bank_swift_code,
             invoice.bank_branch, invoice.bank_address, invoice.display_mode, invoice_id),
        )
        self._replace_items(invoice_id, invoice.items)

    def _replace_items(self, invoice_id: int, items: List[ProformaInvoiceItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM proforma_invoice_items WHERE proforma_invoice_id = ?", (invoice_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO proforma_invoice_items
                       (proforma_invoice_id, sr_no, product_id, product_name, dimension_mm, hsn_code,
                        surface, pallets, quantity_boxes, quantity_value, unit, price_usd, total_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, item.sr_no, item.product_id, item.product_name, item.dimension_mm,
                     item.hsn_code, item.surface, item.pallets, item.quantity_boxes, item.quantity_value,
                     item.unit, item.price_usd, item.total_usd),
                )

    def delete(self, invoice_id: int) -> None:
        self.db.execute("DELETE FROM proforma_invoices WHERE id = ?", (invoice_id,))


class PackingListRepository:
    """Mirrors ProformaInvoiceRepository layer-for-layer: header + line
    items, day-scoped number sequence, reference-only lead link."""

    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM packing_lists WHERE company_id = ? AND packing_list_number LIKE ?",
            (company_id, f"{number_prefix}%"),
        )
        return row["cnt"] if row else 0

    _SELECT = """
        SELECT pl.*, u.full_name AS created_by_name, pi.invoice_number AS proforma_invoice_number,
               q.quotation_number AS quotation_number
        FROM packing_lists pl
        JOIN users u ON u.id = pl.created_by
        LEFT JOIN proforma_invoices pi ON pi.id = pl.proforma_invoice_id
        LEFT JOIN quotations q ON q.id = pl.quotation_id
    """

    def get_by_id(self, packing_list_id: int) -> Optional[PackingList]:
        row = self.db.query_one(self._SELECT + " WHERE pl.id = ?", (packing_list_id,))
        if not row:
            return None
        packing_list = PackingList.from_row(row)
        item_rows = self.db.query(
            "SELECT * FROM packing_list_items WHERE packing_list_id = ? ORDER BY sr_no", (packing_list_id,)
        )
        packing_list.items = [PackingListItem.from_row(r) for r in item_rows]
        return packing_list

    def _attach_items(self, packing_lists: List[PackingList]) -> List[PackingList]:
        """List pages show each row's total quantity, which needs items -
        lists stay small enough that one query per row is fine here."""
        for packing_list in packing_lists:
            item_rows = self.db.query(
                "SELECT * FROM packing_list_items WHERE packing_list_id = ? ORDER BY sr_no", (packing_list.id,)
            )
            packing_list.items = [PackingListItem.from_row(r) for r in item_rows]
        return packing_lists

    def list_all(self, company_id: int) -> List[PackingList]:
        rows = self.db.query(
            self._SELECT + " WHERE pl.company_id = ? ORDER BY pl.packing_list_date DESC, pl.id DESC",
            (company_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def list_for_lead(self, lead_id: int) -> List[PackingList]:
        """Same 'reference-only' join pattern as QuotationRepository.list_for_lead -
        a converted client sees its packing lists through its originating lead_id."""
        rows = self.db.query(
            self._SELECT + " WHERE pl.lead_id = ? ORDER BY pl.packing_list_date DESC, pl.id DESC",
            (lead_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def list_for_proforma(self, proforma_invoice_id: int) -> List[PackingList]:
        """Every packing list generated from one proforma invoice - drives the
        combined invoice + packing details print view."""
        rows = self.db.query(
            self._SELECT + " WHERE pl.proforma_invoice_id = ? ORDER BY pl.id",
            (proforma_invoice_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def list_for_quotation(self, quotation_id: int) -> List[PackingList]:
        """Every packing list generated directly from a quotation (skipping
        the proforma invoice step) - drives the combined quotation + packing
        details print view, same as list_for_proforma."""
        rows = self.db.query(
            self._SELECT + " WHERE pl.quotation_id = ? ORDER BY pl.id",
            (quotation_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def create(self, packing_list: PackingList) -> PackingList:
        new_id = self.db.execute(
            """INSERT INTO packing_lists
               (company_id, packing_list_number, packing_list_date, lead_id, proforma_invoice_id,
                quotation_id, export_ref_no, buyer_order_no, other_reference, consignee_name, consignee_address,
                notify_name, notify_address, country_of_origin, country_of_destination, vessel_flight,
                port_of_loading, port_of_discharge, final_destination, container_details,
                terms_of_delivery, remarks, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (packing_list.company_id, packing_list.packing_list_number, packing_list.packing_list_date,
             packing_list.lead_id, packing_list.proforma_invoice_id, packing_list.quotation_id,
             packing_list.export_ref_no,
             packing_list.buyer_order_no, packing_list.other_reference, packing_list.consignee_name,
             packing_list.consignee_address, packing_list.notify_name, packing_list.notify_address,
             packing_list.country_of_origin, packing_list.country_of_destination,
             packing_list.vessel_flight, packing_list.port_of_loading, packing_list.port_of_discharge,
             packing_list.final_destination, packing_list.container_details,
             packing_list.terms_of_delivery, packing_list.remarks, packing_list.created_by),
        )
        self._replace_items(new_id, packing_list.items)
        return self.get_by_id(new_id)

    def update(self, packing_list_id: int, packing_list: PackingList) -> None:
        self.db.execute(
            """UPDATE packing_lists SET packing_list_date = ?, lead_id = ?, proforma_invoice_id = ?,
                                         quotation_id = ?,
                                         export_ref_no = ?, buyer_order_no = ?, other_reference = ?,
                                         consignee_name = ?, consignee_address = ?, notify_name = ?,
                                         notify_address = ?, country_of_origin = ?, country_of_destination = ?,
                                         vessel_flight = ?, port_of_loading = ?, port_of_discharge = ?,
                                         final_destination = ?, container_details = ?, terms_of_delivery = ?,
                                         remarks = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (packing_list.packing_list_date, packing_list.lead_id, packing_list.proforma_invoice_id,
             packing_list.quotation_id,
             packing_list.export_ref_no, packing_list.buyer_order_no, packing_list.other_reference,
             packing_list.consignee_name, packing_list.consignee_address, packing_list.notify_name,
             packing_list.notify_address, packing_list.country_of_origin,
             packing_list.country_of_destination, packing_list.vessel_flight,
             packing_list.port_of_loading, packing_list.port_of_discharge,
             packing_list.final_destination, packing_list.container_details,
             packing_list.terms_of_delivery, packing_list.remarks, packing_list_id),
        )
        self._replace_items(packing_list_id, packing_list.items)

    def _replace_items(self, packing_list_id: int, items: List[PackingListItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM packing_list_items WHERE packing_list_id = ?", (packing_list_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO packing_list_items
                       (packing_list_id, sr_no, product_id, product_name, design_id, design_name,
                        hsn_code, box_per_pallet, pallets, quantity_boxes, pcs, quantity_value,
                        unit, net_weight_kg, gross_weight_kg)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (packing_list_id, item.sr_no, item.product_id, item.product_name, item.design_id,
                     item.design_name, item.hsn_code, item.box_per_pallet, item.pallets,
                     item.quantity_boxes, item.pcs, item.quantity_value, item.unit,
                     item.net_weight_kg, item.gross_weight_kg),
                )

    def delete(self, packing_list_id: int) -> None:
        self.db.execute("DELETE FROM packing_lists WHERE id = ?", (packing_list_id,))


# ============================================================
# DOCUMENT VERSION REPOSITORY (append-only history for quotations/proforma
# invoices/packing lists - see schema.sql's document_versions table)
# ============================================================
class DocumentVersionRepository:
    def __init__(self, db: Database):
        self.db = db

    def _next_version_number(self, document_type: str, document_id: int) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(version_number), 0) AS mx FROM document_versions "
            "WHERE document_type = ? AND document_id = ?",
            (document_type, document_id),
        )
        return (row["mx"] if row else 0) + 1

    def record(self, company_id: int, document_type: str, document_id: int,
               document_number: str, snapshot: dict, changed_by: int) -> DocumentVersion:
        version_number = self._next_version_number(document_type, document_id)
        new_id = self.db.execute(
            """INSERT INTO document_versions
               (company_id, document_type, document_id, version_number, document_number, snapshot, changed_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (company_id, document_type, document_id, version_number, document_number,
             json.dumps(snapshot), changed_by),
        )
        return self.get_by_id(new_id)

    _SELECT = """
        SELECT dv.*, u.full_name AS changed_by_name
        FROM document_versions dv
        JOIN users u ON u.id = dv.changed_by
    """

    def get_by_id(self, version_id: int) -> Optional[DocumentVersion]:
        row = self.db.query_one(self._SELECT + " WHERE dv.id = ?", (version_id,))
        return DocumentVersion.from_row(row) if row else None

    def list_for_document(self, document_type: str, document_id: int) -> List[DocumentVersion]:
        """Newest first - drives the admin-only version history panel."""
        rows = self.db.query(
            self._SELECT + " WHERE dv.document_type = ? AND dv.document_id = ? ORDER BY dv.version_number DESC",
            (document_type, document_id),
        )
        return [DocumentVersion.from_row(r) for r in rows]

    def get_version(self, document_type: str, document_id: int, version_number: int) -> Optional[DocumentVersion]:
        row = self.db.query_one(
            self._SELECT + " WHERE dv.document_type = ? AND dv.document_id = ? AND dv.version_number = ?",
            (document_type, document_id, version_number),
        )
        return DocumentVersion.from_row(row) if row else None
