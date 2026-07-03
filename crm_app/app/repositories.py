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

from abc import ABC, abstractmethod
from typing import Optional, List

from app.database import Database
from app.models import (
    User, Lead, Client, ContactPerson, Communication,
    PaymentEntry, DocumentEntry, OurCompany, ProductGroup, Product,
    Quotation, QuotationItem,
)


# ============================================================
# USER REPOSITORY
# ============================================================
class UserRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, user_id: int) -> Optional[User]: ...

    @abstractmethod
    def get_by_username(self, username: str) -> Optional[User]: ...

    @abstractmethod
    def list_all(self, role: Optional[str] = None) -> List[User]: ...

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

    def get_by_username(self, username: str) -> Optional[User]:
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        return User.from_row(row) if row else None

    def list_all(self, role: Optional[str] = None) -> List[User]:
        if role:
            rows = self.db.query("SELECT * FROM users WHERE role = ? ORDER BY full_name", (role,))
        else:
            rows = self.db.query("SELECT * FROM users ORDER BY full_name")
        return [User.from_row(r) for r in rows]

    def create(self, user: User) -> User:
        new_id = self.db.execute(
            """INSERT INTO users (username, password_hash, full_name, role, is_active)
               VALUES (?, ?, ?, ?, ?)""",
            (user.username, user.password_hash, user.full_name, user.role, int(user.is_active)),
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
    def list_all(self, employee_id: Optional[int] = None, status: Optional[str] = None) -> List[Lead]: ...

    @abstractmethod
    def create(self, lead: Lead) -> Lead: ...

    @abstractmethod
    def update_compulsory_fields(self, lead_id: int, fields: dict) -> None: ...

    @abstractmethod
    def update_status(self, lead_id: int, status: str) -> None: ...

    @abstractmethod
    def count_by_employee(self) -> dict: ...


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

    def list_all(self, employee_id: Optional[int] = None, status: Optional[str] = None) -> List[Lead]:
        sql = self._SELECT + " WHERE 1=1"
        params: list = []
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
            """INSERT INTO leads (company_name, phone, email, facebook, instagram,
                                   other_social, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead.company_name, lead.phone, lead.email, lead.facebook, lead.instagram,
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

    def count_by_employee(self) -> dict:
        """Returns {employee_id: lead_count} - powers the admin dashboard."""
        rows = self.db.query("SELECT created_by, COUNT(*) AS cnt FROM leads GROUP BY created_by")
        return {r["created_by"]: r["cnt"] for r in rows}


# ============================================================
# CLIENT REPOSITORY
# ============================================================
class ClientRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, client_id: int) -> Optional[Client]: ...

    @abstractmethod
    def list_all(self, client_type: Optional[str] = None, status: Optional[str] = None) -> List[Client]: ...

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

    def list_all(self, client_type: Optional[str] = None, status: Optional[str] = None) -> List[Client]:
        sql = "SELECT * FROM clients WHERE 1=1"
        params: list = []
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
                """INSERT INTO clients (lead_id, company_name, phone, email, facebook,
                                         instagram, other_social, client_type, status, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (client.lead_id, client.company_name, client.phone, client.email, client.facebook,
                 client.instagram, client.other_social, client.client_type, client.status,
                 client.created_by),
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

    def count_by_employee(self) -> dict:
        """{employee_id: communication_count} - powers the admin dashboard."""
        rows = self.db.query("SELECT employee_id, COUNT(*) AS cnt FROM communications GROUP BY employee_id")
        return {r["employee_id"]: r["cnt"] for r in rows}

    def upcoming_followups(self, employee_id: Optional[int], within_days: int) -> List[Communication]:
        """Communications whose follow_up_date is today or overdue, used for
        the employee notification panel."""
        sql = """
            SELECT communications.*, users.full_name AS employee_name
            FROM communications JOIN users ON users.id = communications.employee_id
            WHERE follow_up_date IS NOT NULL
              AND date(follow_up_date) <= date('now', ?)
        """
        params: list = [f"+{within_days} days"]
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
# OUR COMPANY REPOSITORY (singleton row, id = 1)
# ============================================================
class CompanyRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self) -> Optional[OurCompany]:
        row = self.db.query_one("SELECT * FROM our_company WHERE id = 1")
        if not row:
            return None
        company = OurCompany.from_row(row)
        company.contact_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_contact_details ORDER BY is_primary DESC, id"
            )
        ]
        company.contact_persons = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_contact_persons ORDER BY is_primary DESC, id"
            )
        ]
        company.bank_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_bank_details ORDER BY is_primary DESC, id"
            )
        ]
        company.lut_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_lut_details ORDER BY is_primary DESC, financial_year DESC, id"
            )
        ]
        return company

    def upsert(self, company_name: str, address: str, gstin: str, pan_no: str, iec: str, bin_no: str) -> None:
        existing = self.db.query_one("SELECT id FROM our_company WHERE id = 1")
        if existing:
            self.db.execute(
                """UPDATE our_company SET company_name = ?, address = ?, gstin = ?, pan_no = ?, iec = ?, bin = ?,
                                           updated_at = datetime('now') WHERE id = 1""",
                (company_name, address, gstin, pan_no, iec, bin_no),
            )
        else:
            self.db.execute(
                "INSERT INTO our_company (id, company_name, address, gstin, pan_no, iec, bin) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (company_name, address, gstin, pan_no, iec, bin_no),
            )

    def replace_lut_details(self, lut_details: list) -> None:
        """lut_details: [{'lut_number': str, 'financial_year': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_lut_details")
            for l in lut_details:
                conn.execute(
                    "INSERT INTO our_company_lut_details (lut_number, financial_year, is_primary) VALUES (?, ?, ?)",
                    (l["lut_number"], l["financial_year"], int(l["is_primary"])),
                )

    def replace_contact_details(self, details: list) -> None:
        """details: [{'type': 'phone'|'email', 'value': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_contact_details")
            for d in details:
                conn.execute(
                    "INSERT INTO our_company_contact_details (type, value, is_primary) VALUES (?, ?, ?)",
                    (d["type"], d["value"], int(d["is_primary"])),
                )

    def replace_contact_persons(self, persons: list) -> None:
        """persons: [{'name': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_contact_persons")
            for p in persons:
                conn.execute(
                    "INSERT INTO our_company_contact_persons (name, is_primary) VALUES (?, ?)",
                    (p["name"], int(p["is_primary"])),
                )

    def replace_bank_details(self, bank_details: list) -> None:
        """bank_details: [{'bank_name': str, 'account_number': str, 'ifsc_code': str,
        'swift_code': str, 'branch': str, 'bank_address': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_bank_details")
            for b in bank_details:
                conn.execute(
                    """INSERT INTO our_company_bank_details
                       (bank_name, account_number, ifsc_code, swift_code, branch, bank_address, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (b["bank_name"], b["account_number"], b.get("ifsc_code") or None,
                     b.get("swift_code") or None, b.get("branch") or None,
                     b.get("bank_address") or None, int(b["is_primary"])),
                )


# ============================================================
# PRODUCT CATALOG (groups = folders, products = files)
# ============================================================
class ProductGroupRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, group_id: int) -> Optional[ProductGroup]:
        row = self.db.query_one("SELECT * FROM product_groups WHERE id = ?", (group_id,))
        return ProductGroup.from_row(row) if row else None

    def list_children(self, parent_id: Optional[int]) -> List[ProductGroup]:
        if parent_id is None:
            rows = self.db.query("SELECT * FROM product_groups WHERE parent_id IS NULL ORDER BY name")
        else:
            rows = self.db.query("SELECT * FROM product_groups WHERE parent_id = ? ORDER BY name", (parent_id,))
        return [ProductGroup.from_row(r) for r in rows]

    def list_ancestors(self, group_id: int) -> List[ProductGroup]:
        """Walks parent_id up to the root - powers the breadcrumb trail."""
        trail = []
        current = self.get_by_id(group_id)
        while current:
            trail.append(current)
            current = self.get_by_id(current.parent_id) if current.parent_id else None
        trail.reverse()
        return trail

    def create(self, name: str, parent_id: Optional[int]) -> ProductGroup:
        new_id = self.db.execute(
            "INSERT INTO product_groups (name, parent_id) VALUES (?, ?)", (name, parent_id)
        )
        return self.get_by_id(new_id)

    def update(self, group_id: int, name: str) -> None:
        self.db.execute("UPDATE product_groups SET name = ? WHERE id = ?", (name, group_id))

    def delete(self, group_id: int) -> None:
        """Cascades to subgroups and products via ON DELETE CASCADE."""
        self.db.execute("DELETE FROM product_groups WHERE id = ?", (group_id,))

    def has_children(self, group_id: int) -> bool:
        subgroup = self.db.query_one("SELECT id FROM product_groups WHERE parent_id = ? LIMIT 1", (group_id,))
        product = self.db.query_one("SELECT id FROM products WHERE group_id = ? LIMIT 1", (group_id,))
        return bool(subgroup or product)


class ProductRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, product_id: int) -> Optional[Product]:
        row = self.db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
        return Product.from_row(row) if row else None

    def list_in_group(self, group_id: Optional[int]) -> List[Product]:
        if group_id is None:
            rows = self.db.query("SELECT * FROM products WHERE group_id IS NULL ORDER BY product_name")
        else:
            rows = self.db.query("SELECT * FROM products WHERE group_id = ? ORDER BY product_name", (group_id,))
        return [Product.from_row(r) for r in rows]

    def create(self, product: Product) -> Product:
        new_id = self.db.execute(
            """INSERT INTO products
               (group_id, product_name, description, hsn_code, packing, quantity,
                alternate_quantity, weight_class, price_usd, photo_path, dimension_photo_path, alt_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product.group_id, product.product_name, product.description, product.hsn_code,
             product.packing, product.quantity, product.alternate_quantity, product.weight_class,
             product.price_usd, product.photo_path, product.dimension_photo_path, product.alt_text),
        )
        return self.get_by_id(new_id)

    def update(self, product_id: int, fields: dict) -> None:
        """fields may include any column except id/group_id/created_at."""
        columns = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE products SET {columns}, updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), product_id),
        )

    def delete(self, product_id: int) -> None:
        self.db.execute("DELETE FROM products WHERE id = ?", (product_id,))


# ============================================================
# QUOTATION REPOSITORY (header + line items)
# ============================================================
class QuotationRepository:
    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, number_prefix: str) -> int:
        """Counts existing quotations whose number starts with QT{YYYYMMDD} -
        used to compute the next sequence for that day."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM quotations WHERE quotation_number LIKE ?",
            (f"{number_prefix}%",),
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

    def list_all(self) -> List[Quotation]:
        rows = self.db.query(
            """SELECT q.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM quotation_items WHERE quotation_id = q.id), 0) AS items_total
               FROM quotations q
               JOIN users u ON u.id = q.created_by ORDER BY q.quotation_date DESC, q.id DESC"""
        )
        return [Quotation.from_row(r) for r in rows]

    def create(self, quotation: Quotation) -> Quotation:
        new_id = self.db.execute(
            """INSERT INTO quotations
               (quotation_number, quotation_date, lead_id, buyer_name, buyer_address,
                buyer_reference_no, port_of_loading, port_of_discharge, packing_details,
                container_details, shipping_mode, shipping_terms, payment_terms,
                advance_percent, against_bl_percent, price_validity_days, remarks,
                discount_amount, bank_name, bank_account_number, bank_ifsc_code,
                bank_swift_code, bank_branch, bank_address, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (quotation.quotation_number, quotation.quotation_date, quotation.lead_id,
             quotation.buyer_name, quotation.buyer_address, quotation.buyer_reference_no,
             quotation.port_of_loading, quotation.port_of_discharge, quotation.packing_details,
             quotation.container_details, quotation.shipping_mode, quotation.shipping_terms,
             quotation.payment_terms, quotation.advance_percent, quotation.against_bl_percent,
             quotation.price_validity_days, quotation.remarks, quotation.discount_amount,
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
                                      advance_percent = ?, against_bl_percent = ?, price_validity_days = ?,
                                      remarks = ?, discount_amount = ?, bank_name = ?, bank_account_number = ?,
                                      bank_ifsc_code = ?, bank_swift_code = ?, bank_branch = ?, bank_address = ?,
                                      updated_at = datetime('now')
               WHERE id = ?""",
            (quotation.quotation_date, quotation.lead_id, quotation.buyer_name,
             quotation.buyer_address, quotation.buyer_reference_no, quotation.port_of_loading,
             quotation.port_of_discharge, quotation.packing_details, quotation.container_details,
             quotation.shipping_mode, quotation.shipping_terms, quotation.payment_terms,
             quotation.advance_percent, quotation.against_bl_percent, quotation.price_validity_days,
             quotation.remarks, quotation.discount_amount, quotation.bank_name,
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
