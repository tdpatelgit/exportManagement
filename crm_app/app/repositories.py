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
    PaymentEntry, DocumentEntry, OurCompany,
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

    def copy_all(self, from_parent_id: int, to_parent_id: int, to_repo: "ContactRepository") -> None:
        """Used when a Lead converts into a Client - carries every contact
        person across without the caller needing to know the row shape."""
        for c in self.list_for(from_parent_id):
            to_repo.add(to_parent_id, ContactPerson(id=None, name=c.name, phone=c.phone,
                                                      email=c.email, is_primary=c.is_primary))

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
    def mark_converted(self, lead_id: int, client_id: int) -> None: ...

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

    def mark_converted(self, lead_id: int, client_id: int) -> None:
        self.db.execute(
            "UPDATE leads SET is_converted = 1, converted_client_id = ?, status = 'in_client', "
            "updated_at = datetime('now') WHERE id = ?",
            (client_id, lead_id),
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
    def create_from_lead(self, client: Client, lead_id: int) -> Client: ...

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

    def create_from_lead(self, client: Client, lead_id: int) -> Client:
        new_id = self.db.execute(
            """INSERT INTO clients (lead_id, company_name, phone, email, facebook,
                                     instagram, other_social, client_type, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, client.company_name, client.phone, client.email, client.facebook,
             client.instagram, client.other_social, client.client_type, client.status,
             client.created_by),
        )
        client.id = new_id
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
                                   client_type = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (fields["company_name"], fields["phone"], fields["email"],
             fields.get("facebook"), fields.get("instagram"), fields.get("other_social"),
             fields["client_type"], client_id),
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
        return company

    def upsert(self, company_name: str, gstin: str, pan_no: str, iec: str) -> None:
        existing = self.db.query_one("SELECT id FROM our_company WHERE id = 1")
        if existing:
            self.db.execute(
                """UPDATE our_company SET company_name = ?, gstin = ?, pan_no = ?, iec = ?,
                                           updated_at = datetime('now') WHERE id = 1""",
                (company_name, gstin, pan_no, iec),
            )
        else:
            self.db.execute(
                "INSERT INTO our_company (id, company_name, gstin, pan_no, iec) VALUES (1, ?, ?, ?, ?)",
                (company_name, gstin, pan_no, iec),
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
