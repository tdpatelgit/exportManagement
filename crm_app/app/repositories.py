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
from typing import Optional, List, Sequence

from app.database import Database
from app.models import (
    Tenant, User, Lead, Party, Supplier, Transporter, ContactPerson, Communication,
    PaymentEntry, DocumentEntry, OurCompany, MiscCurrency, MiscNatureOfContract, MiscPortOfLoading, MiscContainerType, MiscHsnCode, MiscCountry, MiscUnit, Permit, BookingDetail, Category, Product, ProductPalletType, ProductFolder, Design,
    Quotation, QuotationItem, ProformaInvoice, ProformaInvoiceItem,
    PurchaseOrder, PurchaseOrderItem,
    JobWork, JobWorkItem, JobWorkProduct,
    PurchaseInvoice, PurchaseInvoiceItem,
    ExportInvoice, ExportInvoiceItem,
    ExportPackingList, ExportPackingListItem, ExportPackingListItemDesign, ExportDesignsPackingList,
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


class PartyContactRepository:
    """Contact persons for a Buyer (the `party_contacts` table) - same
    shape/behaviour as ContactRepository above, but keyed by (parent_type,
    parent_id) rather than each type getting its own table."""

    def __init__(self, db: Database, parent_type: str):
        self.db = db
        self.parent_type = parent_type  # 'buyer'

    def list_for(self, parent_id: int) -> List[ContactPerson]:
        rows = self.db.query(
            "SELECT * FROM party_contacts WHERE parent_type = ? AND parent_id = ? ORDER BY is_primary DESC, id",
            (self.parent_type, parent_id),
        )
        return [ContactPerson.from_row(r) for r in rows]

    def add(self, parent_id: int, contact: ContactPerson) -> ContactPerson:
        new_id = self.db.execute(
            """INSERT INTO party_contacts (parent_type, parent_id, name, phone, email, is_primary)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (self.parent_type, parent_id, contact.name, contact.phone, contact.email, int(contact.is_primary)),
        )
        contact.id = new_id
        return contact

    def set_primary(self, parent_id: int, contact_id: int) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE party_contacts SET is_primary = 0 WHERE parent_type = ? AND parent_id = ?",
                (self.parent_type, parent_id),
            )
            conn.execute(
                "UPDATE party_contacts SET is_primary = 1 WHERE parent_type = ? AND id = ? AND parent_id = ?",
                (self.parent_type, contact_id, parent_id),
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
# PARTY REPOSITORY (currently just Buyer; see models.Party. Parametrized by
# which table a row lives in and stays generic enough to serve more than
# one type again if another one shows up.)
# ============================================================
class PartyRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, party_id: int) -> Optional[Party]: ...

    @abstractmethod
    def list_all(self, company_id: int, status: Optional[str] = None) -> List[Party]: ...

    @abstractmethod
    def convert_from_lead(self, party: Party, lead_contacts: List[ContactPerson]) -> Party: ...

    @abstractmethod
    def create(self, party: Party) -> Party: ...

    @abstractmethod
    def update_status(self, party_id: int, status: str) -> None: ...

    @abstractmethod
    def update_compulsory_fields(self, party_id: int, fields: dict) -> None: ...

    @abstractmethod
    def delete(self, party_id: int) -> None: ...


class SqlitePartyRepository(PartyRepositoryBase):
    def __init__(self, db: Database, table: str, client_type: str):
        self.db = db
        self.table = table                  # 'buyers'
        self.client_type = client_type      # 'Buyer' - only used to stamp leads.converted_client_type
        self.contacts = PartyContactRepository(db, client_type.lower())

    def get_by_id(self, party_id: int) -> Optional[Party]:
        row = self.db.query_one(f"SELECT * FROM {self.table} WHERE id = ?", (party_id,))
        if not row:
            return None
        party = Party.from_row(row)
        party.contacts = self.contacts.list_for(party_id)
        return party

    def list_all(self, company_id: int, status: Optional[str] = None) -> List[Party]:
        sql = f"SELECT * FROM {self.table} WHERE company_id = ?"
        params: list = [company_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return [Party.from_row(r) for r in self.db.query(sql, tuple(params))]

    def convert_from_lead(self, party: Party, lead_contacts: List[ContactPerson]) -> Party:
        """Creates the party, copies every lead contact person across, and
        marks the originating lead as converted - all inside ONE transaction.
        This has to be atomic: previously the client row, its contacts, and
        the lead's converted flag were written in three separate
        transactions, so a failure on the last write (e.g. a status value
        the DB didn't allow yet) left a row already created but the lead
        still un-converted - and every retry created another duplicate."""
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                f"""INSERT INTO {self.table} (company_id, lead_id, company_name, phone, email, facebook,
                                               instagram, other_social, status, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (party.company_id, party.lead_id, party.company_name, party.phone, party.email,
                 party.facebook, party.instagram, party.other_social, party.status, party.created_by),
            )
            party.id = cursor.lastrowid
            for contact in lead_contacts:
                conn.execute(
                    """INSERT INTO party_contacts (parent_type, parent_id, name, phone, email, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (self.client_type.lower(), party.id, contact.name, contact.phone, contact.email,
                     int(contact.is_primary)),
                )
            conn.execute(
                "UPDATE leads SET is_converted = 1, converted_client_type = ?, converted_client_id = ?, "
                "status = 'in_client', updated_at = datetime('now') WHERE id = ?",
                (self.client_type, party.id, party.lead_id),
            )
        return party

    def create(self, party: Party) -> Party:
        """Adds a party directly (no originating lead) - no lead to mark
        converted, so this is a plain insert rather than the atomic
        multi-table write convert_from_lead needs."""
        new_id = self.db.execute(
            f"""INSERT INTO {self.table} (company_id, lead_id, company_name, phone, email, facebook,
                                           instagram, other_social, address, country, status, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (party.company_id, party.lead_id, party.company_name, party.phone, party.email,
             party.facebook, party.instagram, party.other_social, party.address, party.country,
             party.status, party.created_by),
        )
        party.id = new_id
        return party

    def update_status(self, party_id: int, status: str) -> None:
        self.db.execute(
            f"UPDATE {self.table} SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, party_id),
        )

    def update_compulsory_fields(self, party_id: int, fields: dict) -> None:
        self.db.execute(
            f"""UPDATE {self.table} SET company_name = ?, phone = ?, email = ?,
                                        facebook = ?, instagram = ?, other_social = ?,
                                        address = ?, country = ?, updated_at = datetime('now')
                WHERE id = ?""",
            (fields["company_name"], fields["phone"], fields["email"],
             fields.get("facebook"), fields.get("instagram"), fields.get("other_social"),
             fields.get("address"), fields.get("country"), party_id),
        )

    def delete(self, party_id: int) -> None:
        """Removes the party plus everything hanging off it by
        parent_type/parent_id - contacts, communications, payments and
        recorded documents - in ONE transaction, so a half-deleted party
        can never be left behind. Quotations/PIs/POs are NOT touched: they
        hang off the originating lead (see PartyService.document_feed), not
        off this row. If the party came from a lead, that lead is put back
        to un-converted so it can be converted again."""
        parent = self.client_type.lower()   # 'buyer'
        with self.db.get_connection() as conn:
            row = conn.execute(
                f"SELECT lead_id FROM {self.table} WHERE id = ?", (party_id,)
            ).fetchone()
            lead_id = row["lead_id"] if row else None
            conn.execute(
                "DELETE FROM party_contacts WHERE parent_type = ? AND parent_id = ?", (parent, party_id))
            conn.execute(
                "DELETE FROM communications WHERE parent_type = ? AND parent_id = ?", (parent, party_id))
            conn.execute(
                "DELETE FROM payment_history WHERE parent_type = ? AND parent_id = ?", (parent, party_id))
            conn.execute(
                "DELETE FROM documents WHERE parent_type = ? AND parent_id = ?", (parent, party_id))
            conn.execute(f"DELETE FROM {self.table} WHERE id = ?", (party_id,))
            if lead_id:
                conn.execute(
                    "UPDATE leads SET is_converted = 0, converted_client_type = NULL, "
                    "converted_client_id = NULL, status = 'quotation_submission_pending', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (lead_id,),
                )


# ============================================================
# SUPPLIER REPOSITORY (its own profile shape - GSTIN/PAN/IEC/bank/contacts,
# modeled on CompanyRepository/OurCompany rather than on Party/Lead; see
# models.Supplier.)
# ============================================================
class SupplierRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, supplier_id: int) -> Optional[Supplier]: ...

    @abstractmethod
    def list_all(self, company_id: int, status: Optional[str] = None) -> List[Supplier]: ...

    @abstractmethod
    def convert_from_lead(self, supplier: Supplier) -> Supplier: ...

    @abstractmethod
    def create(self, supplier: Supplier) -> Supplier: ...

    @abstractmethod
    def update_status(self, supplier_id: int, status: str) -> None: ...

    @abstractmethod
    def update_profile(self, supplier_id: int, fields: dict) -> None: ...


class SqliteSupplierRepository(SupplierRepositoryBase):
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, supplier_id: int) -> Optional[Supplier]:
        row = self.db.query_one("SELECT * FROM suppliers WHERE id = ?", (supplier_id,))
        if not row:
            return None
        supplier = Supplier.from_row(row)
        supplier.contact_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM supplier_contact_details WHERE supplier_id = ? ORDER BY is_primary DESC, id",
                (supplier.id,),
            )
        ]
        supplier.contact_persons = [
            dict(r) for r in self.db.query(
                "SELECT * FROM supplier_contact_persons WHERE supplier_id = ? ORDER BY is_primary DESC, id",
                (supplier.id,),
            )
        ]
        supplier.bank_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM supplier_bank_details WHERE supplier_id = ? ORDER BY is_primary DESC, id",
                (supplier.id,),
            )
        ]
        return supplier

    def list_all(self, company_id: int, status: Optional[str] = None) -> List[Supplier]:
        sql = "SELECT * FROM suppliers WHERE company_id = ?"
        params: list = [company_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return [Supplier.from_row(r) for r in self.db.query(sql, tuple(params))]

    def convert_from_lead(self, supplier: Supplier) -> Supplier:
        """Creates the supplier (company name + lead link only - GSTIN/PAN/
        IEC/bank/contacts are filled in afterward on the supplier record,
        since a Lead doesn't capture them) and marks the originating lead as
        converted, atomically (same reasoning as SqlitePartyRepository's)."""
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO suppliers (company_id, lead_id, company_name, status, created_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (supplier.company_id, supplier.lead_id, supplier.company_name, supplier.status, supplier.created_by),
            )
            supplier.id = cursor.lastrowid
            conn.execute(
                "UPDATE leads SET is_converted = 1, converted_client_type = 'Supplier', converted_client_id = ?, "
                "status = 'in_client', updated_at = datetime('now') WHERE id = ?",
                (supplier.id, supplier.lead_id),
            )
        return supplier

    def create(self, supplier: Supplier) -> Supplier:
        """Adds a supplier directly (no originating lead) - no lead to mark
        converted, so this is a plain insert rather than the atomic write
        convert_from_lead needs. GSTIN/PAN/IEC and contacts/bank details are
        set separately via update_profile/replace_* right after."""
        new_id = self.db.execute(
            """INSERT INTO suppliers (company_id, lead_id, company_name, address, gstin, cin_llp_no, pan_no, iec, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (supplier.company_id, supplier.lead_id, supplier.company_name, supplier.address,
             supplier.gstin, supplier.cin_llp_no, supplier.pan_no, supplier.iec, supplier.status, supplier.created_by),
        )
        supplier.id = new_id
        return supplier

    def update_status(self, supplier_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE suppliers SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, supplier_id),
        )

    def update_profile(self, supplier_id: int, fields: dict) -> None:
        self.db.execute(
            """UPDATE suppliers SET company_name = ?, address = ?, gstin = ?, cin_llp_no = ?, pan_no = ?, iec = ?,
                                     updated_at = datetime('now') WHERE id = ?""",
            (fields["company_name"], fields.get("address"), fields.get("gstin"), fields.get("cin_llp_no"),
             fields.get("pan_no"), fields.get("iec"), supplier_id),
        )

    def replace_contact_details(self, supplier_id: int, details: list) -> None:
        """details: [{'type': 'phone'|'email', 'value': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM supplier_contact_details WHERE supplier_id = ?", (supplier_id,))
            for d in details:
                conn.execute(
                    "INSERT INTO supplier_contact_details (supplier_id, type, value, is_primary) VALUES (?, ?, ?, ?)",
                    (supplier_id, d["type"], d["value"], int(d["is_primary"])),
                )

    def replace_contact_persons(self, supplier_id: int, persons: list) -> None:
        """persons: [{'name': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM supplier_contact_persons WHERE supplier_id = ?", (supplier_id,))
            for p in persons:
                conn.execute(
                    "INSERT INTO supplier_contact_persons (supplier_id, name, is_primary) VALUES (?, ?, ?)",
                    (supplier_id, p["name"], int(p["is_primary"])),
                )

    def replace_bank_details(self, supplier_id: int, bank_details: list) -> None:
        """bank_details: [{'bank_name': str, 'account_number': str, 'ifsc_code': str,
        'swift_code': str, 'branch': str, 'bank_address': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM supplier_bank_details WHERE supplier_id = ?", (supplier_id,))
            for b in bank_details:
                conn.execute(
                    """INSERT INTO supplier_bank_details
                       (supplier_id, bank_name, account_number, ifsc_code, swift_code, branch, bank_address, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (supplier_id, b["bank_name"], b["account_number"], b.get("ifsc_code") or None,
                     b.get("swift_code") or None, b.get("branch") or None,
                     b.get("bank_address") or None, int(b["is_primary"])),
                )


# ============================================================
# TRANSPORTER REPOSITORY (a standalone directory entry - no originating
# lead, no status, no satellite tables; see models.Transporter.)
# ============================================================
class TransporterRepositoryBase(ABC):
    @abstractmethod
    def get_by_id(self, transporter_id: int) -> Optional[Transporter]: ...

    @abstractmethod
    def list_all(self, company_id: int) -> List[Transporter]: ...

    @abstractmethod
    def create(self, transporter: Transporter) -> Transporter: ...

    @abstractmethod
    def update(self, transporter_id: int, fields: dict) -> None: ...

    @abstractmethod
    def replace_contacts(self, transporter_id: int, contacts: list) -> None: ...

    @abstractmethod
    def delete(self, transporter_id: int) -> None: ...


class SqliteTransporterRepository(TransporterRepositoryBase):
    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, transporter_id: int) -> Optional[Transporter]:
        row = self.db.query_one("SELECT * FROM transporters WHERE id = ?", (transporter_id,))
        if not row:
            return None
        transporter = Transporter.from_row(row)
        transporter.contacts = self._list_contacts(transporter_id)
        return transporter

    def list_all(self, company_id: int) -> List[Transporter]:
        rows = self.db.query(
            "SELECT * FROM transporters WHERE company_id = ? ORDER BY name COLLATE NOCASE", (company_id,)
        )
        transporters = [Transporter.from_row(r) for r in rows]
        for transporter in transporters:
            transporter.contacts = self._list_contacts(transporter.id)
        return transporters

    def _list_contacts(self, transporter_id: int) -> List[ContactPerson]:
        rows = self.db.query(
            "SELECT * FROM transporter_contacts WHERE transporter_id = ? ORDER BY is_primary DESC, id",
            (transporter_id,),
        )
        return [ContactPerson.from_row(r) for r in rows]

    def create(self, transporter: Transporter) -> Transporter:
        new_id = self.db.execute(
            """INSERT INTO transporters (company_id, name, address, gstin_transporter_no,
                                          pan_no, cin_llp_no, email, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (transporter.company_id, transporter.name, transporter.address,
             transporter.gstin_transporter_no, transporter.pan_no, transporter.cin_llp_no,
             transporter.email, transporter.created_by),
        )
        transporter.id = new_id
        return transporter

    def update(self, transporter_id: int, fields: dict) -> None:
        self.db.execute(
            """UPDATE transporters SET name = ?, address = ?, gstin_transporter_no = ?,
                                       pan_no = ?, cin_llp_no = ?, email = ?,
                                       updated_at = datetime('now')
               WHERE id = ?""",
            (fields["name"], fields.get("address"), fields.get("gstin_transporter_no"),
             fields.get("pan_no"), fields.get("cin_llp_no"), fields.get("email"), transporter_id),
        )

    def replace_contacts(self, transporter_id: int, contacts: list) -> None:
        """contacts: [{'name': str, 'phone': str, 'email': str, 'is_primary': bool}] -
        the whole set is rewritten on every save, the same way a supplier's
        contact rows are, so the form is the single source of truth."""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM transporter_contacts WHERE transporter_id = ?", (transporter_id,))
            for c in contacts:
                conn.execute(
                    """INSERT INTO transporter_contacts (transporter_id, name, phone, email, is_primary)
                       VALUES (?, ?, ?, ?, ?)""",
                    (transporter_id, c["name"], c.get("phone") or None,
                     c.get("email") or None, int(c.get("is_primary", False))),
                )

    def delete(self, transporter_id: int) -> None:
        """Its contact rows are ON DELETE CASCADE, but foreign keys are only
        enforced when the pragma is on, so they're removed explicitly - and
        in the same transaction, so no orphan set can be left behind."""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM transporter_contacts WHERE transporter_id = ?", (transporter_id,))
            conn.execute("DELETE FROM transporters WHERE id = ?", (transporter_id,))


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
    """Shared by Buyer and Supplier - parent_type/parent_id is the
    same polymorphic pattern as CommunicationRepository below."""

    def __init__(self, db: Database):
        self.db = db

    def list_for(self, parent_type: str, parent_id: int) -> List[PaymentEntry]:
        rows = self.db.query(
            "SELECT * FROM payment_history WHERE parent_type = ? AND parent_id = ? ORDER BY payment_datetime DESC",
            (parent_type, parent_id),
        )
        return [PaymentEntry.from_row(r) for r in rows]

    def add(self, payment: PaymentEntry) -> PaymentEntry:
        new_id = self.db.execute(
            """INSERT INTO payment_history (parent_type, parent_id, account_name, payment_datetime,
                                              amount_original, currency_code, conversion_rate, amount_inr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (payment.parent_type, payment.parent_id, payment.account_name, payment.payment_datetime,
             payment.amount_original, payment.currency_code, payment.conversion_rate,
             payment.amount_inr),
        )
        payment.id = new_id
        return payment


# ============================================================
# DOCUMENT REPOSITORY
# ============================================================
class DocumentRepository:
    """Shared by Buyer and Supplier - same parent_type/parent_id
    pattern as PaymentRepository above."""

    def __init__(self, db: Database):
        self.db = db

    def list_for(self, parent_type: str, parent_id: int) -> List[DocumentEntry]:
        rows = self.db.query(
            "SELECT * FROM documents WHERE parent_type = ? AND parent_id = ? ORDER BY document_date DESC",
            (parent_type, parent_id),
        )
        return [DocumentEntry.from_row(r) for r in rows]

    def add(self, doc: DocumentEntry) -> DocumentEntry:
        new_id = self.db.execute(
            """INSERT INTO documents (parent_type, parent_id, document_name, document_type, document_date, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (doc.parent_type, doc.parent_id, doc.document_name, doc.document_type, doc.document_date, doc.notes),
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
        company.rcmc_details = [
            dict(r) for r in self.db.query(
                "SELECT * FROM our_company_rcmc_details WHERE our_company_id = ? "
                "ORDER BY is_primary DESC, registration_date DESC, id",
                (company.id,),
            )
        ]
        return company

    def upsert(self, company_id: int, company_name: str, address: str, gstin: str,
               pan_no: str, iec: str, bin_no: str, self_sealing_declaration: str = None,
               branch_code: str = None, government_schemes: str = None) -> int:
        """Returns the `our_company.id` row (not the tenant's company_id) -
        callers need it to scope the four detail-table replace_* calls."""
        existing = self.db.query_one("SELECT id FROM our_company WHERE company_id = ?", (company_id,))
        if existing:
            self.db.execute(
                """UPDATE our_company SET company_name = ?, address = ?, gstin = ?, pan_no = ?, iec = ?, bin = ?,
                                           self_sealing_declaration = ?, branch_code = ?, government_schemes = ?,
                                           updated_at = datetime('now') WHERE company_id = ?""",
                (company_name, address, gstin, pan_no, iec, bin_no, self_sealing_declaration, branch_code,
                 government_schemes, company_id),
            )
            return existing["id"]
        return self.db.execute(
            "INSERT INTO our_company (company_id, company_name, address, gstin, pan_no, iec, bin, self_sealing_declaration, branch_code, government_schemes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, company_name, address, gstin, pan_no, iec, bin_no, self_sealing_declaration, branch_code,
             government_schemes),
        )

    def set_logo(self, our_company_id: int, logo_path: Optional[str]) -> None:
        """logo_path is relative to static/ (None clears the logo)."""
        self.db.execute(
            "UPDATE our_company SET logo_path = ?, updated_at = datetime('now') WHERE id = ?",
            (logo_path, our_company_id),
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

    def replace_rcmc_details(self, our_company_id: int, rcmc_details: list) -> None:
        """rcmc_details: [{'registration_number': str, 'registration_date': str, 'valid_until': str,
        'organisation_name': str, 'organisation_address': str, 'contact_number': str,
        'email_address': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_rcmc_details WHERE our_company_id = ?", (our_company_id,))
            for r in rcmc_details:
                conn.execute(
                    "INSERT INTO our_company_rcmc_details (our_company_id, registration_number, registration_date, "
                    "valid_until, organisation_name, organisation_address, contact_number, email_address, is_primary) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (our_company_id, r["registration_number"], r["registration_date"], r["valid_until"],
                     r.get("organisation_name", ""), r.get("organisation_address", ""),
                     r.get("contact_number", ""), r.get("email_address", ""), int(r["is_primary"])),
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
        """persons: [{'name': str, 'designation': str, 'is_primary': bool}]"""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM our_company_contact_persons WHERE our_company_id = ?", (our_company_id,))
            for p in persons:
                conn.execute(
                    "INSERT INTO our_company_contact_persons (our_company_id, name, designation, is_primary) "
                    "VALUES (?, ?, ?, ?)",
                    (our_company_id, p["name"], (p.get("designation") or "").strip() or None, int(p["is_primary"])),
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
# MISCELLANEOUS DROP LISTS (Administration -> Miscellaneous)
# ============================================================
class MiscCurrencyRepository:
    """The CURRENCY drop list an admin maintains under Administration ->
    Miscellaneous. Not to be confused with CurrencyService, which converts
    foreign amounts to INR."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, currency_id: int) -> Optional[MiscCurrency]:
        row = self.db.query_one("SELECT * FROM misc_currencies WHERE id = ?", (currency_id,))
        return MiscCurrency.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscCurrency]:
        rows = self.db.query(
            "SELECT * FROM misc_currencies WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscCurrency.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscCurrency]:
        row = self.db.query_one(
            "SELECT * FROM misc_currencies WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscCurrency.from_row(row) if row else None

    def create(self, currency: MiscCurrency) -> MiscCurrency:
        new_id = self.db.execute(
            "INSERT INTO misc_currencies (company_id, name, symbol) VALUES (?, ?, ?)",
            (currency.company_id, currency.name, currency.symbol),
        )
        return self.get_by_id(new_id)

    def update(self, currency_id: int, currency: MiscCurrency) -> None:
        self.db.execute(
            "UPDATE misc_currencies SET name = ?, symbol = ?, updated_at = datetime('now') WHERE id = ?",
            (currency.name, currency.symbol, currency_id),
        )

    def delete(self, currency_id: int) -> None:
        self.db.execute("DELETE FROM misc_currencies WHERE id = ?", (currency_id,))


class MiscNatureOfContractRepository:
    """The NATURE OF CONTRACT drop list (Administration -> Miscellaneous):
    one name per row, shared by every document's delivery-terms field."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, row_id: int) -> Optional[MiscNatureOfContract]:
        row = self.db.query_one("SELECT * FROM misc_nature_of_contracts WHERE id = ?", (row_id,))
        return MiscNatureOfContract.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscNatureOfContract]:
        rows = self.db.query(
            "SELECT * FROM misc_nature_of_contracts WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscNatureOfContract.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscNatureOfContract]:
        row = self.db.query_one(
            "SELECT * FROM misc_nature_of_contracts WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscNatureOfContract.from_row(row) if row else None

    def create(self, entry: MiscNatureOfContract) -> MiscNatureOfContract:
        new_id = self.db.execute(
            "INSERT INTO misc_nature_of_contracts (company_id, name) VALUES (?, ?)",
            (entry.company_id, entry.name),
        )
        return self.get_by_id(new_id)

    def update(self, row_id: int, entry: MiscNatureOfContract) -> None:
        self.db.execute(
            "UPDATE misc_nature_of_contracts SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (entry.name, row_id),
        )

    def delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM misc_nature_of_contracts WHERE id = ?", (row_id,))


class MiscContainerTypeRepository:
    """The CONTAINER TYPE drop list (Administration -> Miscellaneous):
    one name per row, feeding the Booking Detail form's container-type
    dropdown."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, row_id: int) -> Optional[MiscContainerType]:
        row = self.db.query_one("SELECT * FROM misc_container_types WHERE id = ?", (row_id,))
        return MiscContainerType.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscContainerType]:
        rows = self.db.query(
            "SELECT * FROM misc_container_types WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscContainerType.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscContainerType]:
        row = self.db.query_one(
            "SELECT * FROM misc_container_types WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscContainerType.from_row(row) if row else None

    def create(self, entry: MiscContainerType) -> MiscContainerType:
        new_id = self.db.execute(
            "INSERT INTO misc_container_types (company_id, name) VALUES (?, ?)",
            (entry.company_id, entry.name),
        )
        return self.get_by_id(new_id)

    def update(self, row_id: int, entry: MiscContainerType) -> None:
        self.db.execute(
            "UPDATE misc_container_types SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (entry.name, row_id),
        )

    def delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM misc_container_types WHERE id = ?", (row_id,))


class MiscPortOfLoadingRepository:
    """The PORT OF LOADING drop list (Administration -> Miscellaneous): a
    port name plus that port's PIN code."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, row_id: int) -> Optional[MiscPortOfLoading]:
        row = self.db.query_one("SELECT * FROM misc_ports_of_loading WHERE id = ?", (row_id,))
        return MiscPortOfLoading.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscPortOfLoading]:
        rows = self.db.query(
            "SELECT * FROM misc_ports_of_loading WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscPortOfLoading.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscPortOfLoading]:
        row = self.db.query_one(
            "SELECT * FROM misc_ports_of_loading WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscPortOfLoading.from_row(row) if row else None

    def create(self, entry: MiscPortOfLoading) -> MiscPortOfLoading:
        new_id = self.db.execute(
            "INSERT INTO misc_ports_of_loading (company_id, name, pin_code) VALUES (?, ?, ?)",
            (entry.company_id, entry.name, entry.pin_code),
        )
        return self.get_by_id(new_id)

    def update(self, row_id: int, entry: MiscPortOfLoading) -> None:
        self.db.execute(
            "UPDATE misc_ports_of_loading SET name = ?, pin_code = ?, updated_at = datetime('now') WHERE id = ?",
            (entry.name, entry.pin_code, row_id),
        )

    def delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM misc_ports_of_loading WHERE id = ?", (row_id,))


class MiscHsnCodeRepository:
    """The HSN CODE drop list (Administration -> Miscellaneous): an HSN code
    plus the GST slab that applies to it."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, row_id: int) -> Optional[MiscHsnCode]:
        row = self.db.query_one("SELECT * FROM misc_hsn_codes WHERE id = ?", (row_id,))
        return MiscHsnCode.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscHsnCode]:
        rows = self.db.query(
            "SELECT * FROM misc_hsn_codes WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscHsnCode.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscHsnCode]:
        row = self.db.query_one(
            "SELECT * FROM misc_hsn_codes WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscHsnCode.from_row(row) if row else None

    def create(self, entry: MiscHsnCode) -> MiscHsnCode:
        new_id = self.db.execute(
            "INSERT INTO misc_hsn_codes (company_id, name, related_products, gst_slab) VALUES (?, ?, ?, ?)",
            (entry.company_id, entry.name, entry.related_products, entry.gst_slab),
        )
        return self.get_by_id(new_id)

    def update(self, row_id: int, entry: MiscHsnCode) -> None:
        self.db.execute(
            "UPDATE misc_hsn_codes SET name = ?, related_products = ?, gst_slab = ?,"
            " updated_at = datetime('now') WHERE id = ?",
            (entry.name, entry.related_products, entry.gst_slab, row_id),
        )

    def delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM misc_hsn_codes WHERE id = ?", (row_id,))


class MiscCountryRepository:
    """The COUNTRY drop list (Administration -> Miscellaneous): one name
    per row, feeding a Buyer's "Country Name" field."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, row_id: int) -> Optional[MiscCountry]:
        row = self.db.query_one("SELECT * FROM misc_countries WHERE id = ?", (row_id,))
        return MiscCountry.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscCountry]:
        rows = self.db.query(
            "SELECT * FROM misc_countries WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscCountry.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscCountry]:
        row = self.db.query_one(
            "SELECT * FROM misc_countries WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscCountry.from_row(row) if row else None

    def create(self, entry: MiscCountry) -> MiscCountry:
        new_id = self.db.execute(
            "INSERT INTO misc_countries (company_id, name) VALUES (?, ?)",
            (entry.company_id, entry.name),
        )
        return self.get_by_id(new_id)

    def update(self, row_id: int, entry: MiscCountry) -> None:
        self.db.execute(
            "UPDATE misc_countries SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (entry.name, row_id),
        )

    def delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM misc_countries WHERE id = ?", (row_id,))


class MiscUnitRepository:
    """The UNIT drop list (Administration -> Miscellaneous): a unit
    abbreviation plus what it means in words, kept together on one row."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, row_id: int) -> Optional[MiscUnit]:
        row = self.db.query_one("SELECT * FROM misc_units WHERE id = ?", (row_id,))
        return MiscUnit.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[MiscUnit]:
        rows = self.db.query(
            "SELECT * FROM misc_units WHERE company_id = ? ORDER BY name COLLATE NOCASE",
            (company_id,),
        )
        return [MiscUnit.from_row(r) for r in rows]

    def find_by_name(self, company_id: int, name: str) -> Optional[MiscUnit]:
        row = self.db.query_one(
            "SELECT * FROM misc_units WHERE company_id = ? AND name = ? COLLATE NOCASE",
            (company_id, name),
        )
        return MiscUnit.from_row(row) if row else None

    def create(self, entry: MiscUnit) -> MiscUnit:
        new_id = self.db.execute(
            "INSERT INTO misc_units (company_id, name, meaning) VALUES (?, ?, ?)",
            (entry.company_id, entry.name, entry.meaning),
        )
        return self.get_by_id(new_id)

    def update(self, row_id: int, entry: MiscUnit) -> None:
        self.db.execute(
            "UPDATE misc_units SET name = ?, meaning = ?, updated_at = datetime('now') WHERE id = ?",
            (entry.name, entry.meaning, row_id),
        )

    def delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM misc_units WHERE id = ?", (row_id,))


# ============================================================
# PRODUCT CATALOG (products -> folders -> designs)
# ============================================================
class PermitRepository:
    """The permits ("permissions") a company holds, each recording a
    stuffing-place name + place of stuffing and optionally carrying an
    uploaded PDF. Managed under the Our Company area."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, permit_id: int) -> Optional[Permit]:
        row = self.db.query_one("SELECT * FROM permits WHERE id = ?", (permit_id,))
        return Permit.from_row(row) if row else None

    def list_all(self, company_id: int) -> List[Permit]:
        rows = self.db.query(
            "SELECT * FROM permits WHERE company_id = ? ORDER BY date_of_issue DESC, id DESC",
            (company_id,),
        )
        return [Permit.from_row(r) for r in rows]

    def create(self, permit: Permit) -> Permit:
        new_id = self.db.execute(
            "INSERT INTO permits (company_id, stuffing_place_name, place_of_stuffing, permission_number, "
            "date_of_issue, issuing_authority, issuing_authority_address, validity_type, "
            "date_of_expiry, pdf_path, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (permit.company_id, permit.stuffing_place_name, permit.place_of_stuffing, permit.permission_number,
             permit.date_of_issue, permit.issuing_authority, permit.issuing_authority_address,
             permit.validity_type, permit.date_of_expiry, permit.pdf_path, permit.created_by),
        )
        return self.get_by_id(new_id)

    def update(self, permit_id: int, permit: Permit) -> None:
        self.db.execute(
            "UPDATE permits SET stuffing_place_name = ?, place_of_stuffing = ?, permission_number = ?, "
            "date_of_issue = ?, issuing_authority = ?, issuing_authority_address = ?, "
            "validity_type = ?, date_of_expiry = ?, pdf_path = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (permit.stuffing_place_name, permit.place_of_stuffing, permit.permission_number,
             permit.date_of_issue, permit.issuing_authority, permit.issuing_authority_address,
             permit.validity_type, permit.date_of_expiry, permit.pdf_path, permit_id),
        )

    def delete(self, permit_id: int) -> None:
        self.db.execute("DELETE FROM permits WHERE id = ?", (permit_id,))


class BookingDetailRepository:
    """Standalone shipping-booking log under Master Data - the same field
    shape as an Export Invoice's own "Container details" card (booking no. /
    vessel / voyage, one transporter for the whole booking, the container
    type/count list, and one row per physical container), owned directly by
    a buyer rather than any invoice. Mirrors ExportInvoiceRepository's own
    handling of those same two child lists, minus everything invoice-only
    (items, money, the fields another per-container document carries
    forward)."""

    def __init__(self, db: Database):
        self.db = db

    def get_by_id(self, booking_detail_id: int) -> Optional[BookingDetail]:
        row = self.db.query_one(
            """SELECT bd.*, b.company_name AS buyer_name, u.full_name AS created_by_name
               FROM booking_details bd
               JOIN buyers b ON b.id = bd.buyer_id
               JOIN users u ON u.id = bd.created_by
               WHERE bd.id = ?""",
            (booking_detail_id,),
        )
        if not row:
            return None
        booking = BookingDetail.from_row(row)
        booking.containers = [
            dict(r) for r in self.db.query(
                "SELECT container_type, container_count FROM booking_detail_containers "
                "WHERE booking_detail_id = ? ORDER BY sr_no", (booking_detail_id,)
            )
        ]
        booking.container_details = [
            dict(r) for r in self.db.query(
                "SELECT container_type, container_no, max_permitted_weight, tare_weight_kg, vehicle_no, lr_no, "
                "line_seal_no, rfid_seal_no FROM booking_detail_container_details "
                "WHERE booking_detail_id = ? ORDER BY sr_no", (booking_detail_id,)
            )
        ]
        return booking

    def list_all(self, company_id: int) -> List[BookingDetail]:
        rows = self.db.query(
            """SELECT bd.*, b.company_name AS buyer_name, u.full_name AS created_by_name,
                      (SELECT COUNT(*) FROM booking_detail_container_details
                       WHERE booking_detail_id = bd.id) AS container_count
               FROM booking_details bd
               JOIN buyers b ON b.id = bd.buyer_id
               JOIN users u ON u.id = bd.created_by
               WHERE bd.company_id = ?
               ORDER BY bd.created_at DESC, bd.id DESC""",
            (company_id,),
        )
        bookings = []
        for r in rows:
            booking = BookingDetail.from_row(r)
            booking.container_count = r["container_count"]
            bookings.append(booking)
        return bookings

    def create(self, booking: BookingDetail) -> BookingDetail:
        new_id = self.db.execute(
            """INSERT INTO booking_details
               (company_id, buyer_id, booking_no, vessel_name, voyage_no, transporter_name, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (booking.company_id, booking.buyer_id, booking.booking_no, booking.vessel_name,
             booking.voyage_no, booking.transporter_name, booking.created_by),
        )
        self._replace_containers(new_id, booking.containers)
        self._replace_container_details(new_id, booking.container_details)
        return self.get_by_id(new_id)

    def update(self, booking_detail_id: int, booking: BookingDetail) -> None:
        self.db.execute(
            """UPDATE booking_details SET buyer_id = ?, booking_no = ?, vessel_name = ?, voyage_no = ?,
                                          transporter_name = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (booking.buyer_id, booking.booking_no, booking.vessel_name, booking.voyage_no,
             booking.transporter_name, booking_detail_id),
        )
        self._replace_containers(booking_detail_id, booking.containers)
        self._replace_container_details(booking_detail_id, booking.container_details)

    def _replace_containers(self, booking_detail_id: int, containers: list) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM booking_detail_containers WHERE booking_detail_id = ?", (booking_detail_id,))
            for i, c in enumerate(containers, start=1):
                conn.execute(
                    "INSERT INTO booking_detail_containers (booking_detail_id, sr_no, container_type, container_count) "
                    "VALUES (?, ?, ?, ?)",
                    (booking_detail_id, i, c["container_type"], c["container_count"]),
                )

    def _replace_container_details(self, booking_detail_id: int, rows: list) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM booking_detail_container_details WHERE booking_detail_id = ?", (booking_detail_id,))
            for i, cd in enumerate(rows, start=1):
                conn.execute(
                    """INSERT INTO booking_detail_container_details
                       (booking_detail_id, sr_no, container_type, container_no, max_permitted_weight, tare_weight_kg,
                        vehicle_no, lr_no, line_seal_no, rfid_seal_no)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (booking_detail_id, i, cd.get("container_type"), cd.get("container_no"), cd.get("max_permitted_weight"),
                     cd.get("tare_weight_kg"), cd.get("vehicle_no"), cd.get("lr_no"),
                     cd.get("line_seal_no"), cd.get("rfid_seal_no")),
                )

    def delete(self, booking_detail_id: int) -> None:
        """Child rows are ON DELETE CASCADE, but foreign keys are only
        enforced when the pragma is on, so they're removed explicitly - and
        in the same transaction, so no orphan set can be left behind."""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM booking_detail_container_details WHERE booking_detail_id = ?", (booking_detail_id,))
            conn.execute("DELETE FROM booking_detail_containers WHERE booking_detail_id = ?", (booking_detail_id,))
            conn.execute("DELETE FROM booking_details WHERE id = ?", (booking_detail_id,))


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

    def search(self, company_id: int, query: str) -> List[Product]:
        """Products whose name matches `query` (case-insensitive substring),
        regardless of category - used by the catalog-wide search bar."""
        rows = self.db.query(
            "SELECT * FROM products WHERE company_id = ? AND product_name LIKE ? ORDER BY product_name",
            (company_id, f"%{query}%"),
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
                quantity_unit, quantity, alternate_quantity_unit, alternate_quantity,
                net_weight_kg, gross_weight_kg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product.company_id, product.category_id, product.product_name, product.description,
             product.hsn_code, product.igst_percent, product.sgst_percent, product.cgst_percent,
             product.quantity_unit, product.quantity, product.alternate_quantity_unit, product.alternate_quantity,
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
        """Cascades to the products folders and designs via ON DELETE CASCADE.
        Document line items keep their snapshot text (name/HSN) - only the
        catalog reference is nulled out first. Done explicitly rather than
        relying on ON DELETE SET NULL because quotation/proforma item tables
        created before this rule existed dont carry it."""
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
        product-picker JSON API attach each products list without one
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
                    "INSERT INTO product_pallet_types (company_id, product_id, name, boxes_per_pallet, weight_kg, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (company_id, product_id, pt.name, pt.boxes_per_pallet, pt.weight_kg, order),
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
        """Walks parent_id up to the products top level - powers the breadcrumb trail."""
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
        nulled for every design in the folders subtree first."""
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
        """Designs sitting in one folder - folder_id=None is the products top level."""
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

    def search(self, company_id: int, query: str) -> List[dict]:
        """Designs (with their product's name attached) whose own name OR
        product name matches `query` - a product-name search should surface
        its designs too, not just the product tile. Used by the
        catalog-wide search bar (Products and Inventory both)."""
        term = f"%{query}%"
        rows = self.db.query(
            """SELECT d.*, p.product_name AS product_name
               FROM designs d JOIN products p ON p.id = d.product_id
               WHERE d.company_id = ? AND (d.design_name LIKE ? OR p.product_name LIKE ?)
               ORDER BY d.design_name""",
            (company_id, term, term),
        )
        return [dict(r) for r in rows]

    def list_by_ids_with_product(self, design_ids: List[int]) -> List[dict]:
        """Designs (with their product's name attached) for a batch of ids,
        one query - used by the Inventory "in stock" summary instead of
        looking each design up one at a time."""
        if not design_ids:
            return []
        placeholders = ",".join("?" for _ in design_ids)
        rows = self.db.query(
            f"""SELECT d.*, p.product_name AS product_name
                FROM designs d JOIN products p ON p.id = d.product_id
                WHERE d.id IN ({placeholders})
                ORDER BY d.design_name""",
            tuple(design_ids),
        )
        return [dict(r) for r in rows]

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
# DOCUMENT CASCADE DELETE HELPERS
# ------------------------------------------------------------
# The pipeline is Quotation -> Proforma Invoice -> Purchase Order(s) ->
# Purchase Invoice -> Packing List. Deleting any document deletes every
# document generated under it (a real cascade), so these are called from
# the repository .delete() methods below, deepest table first, all inside
# the same transaction as the parent's own DELETE.
# ============================================================
def _delete_purchase_invoice_pdf_files(paths) -> None:
    if not paths:
        return
    import os
    from config import Config
    for path in paths:
        if not path:
            continue
        full_path = os.path.join(Config.PURCHASE_INVOICE_UPLOAD_FOLDER, os.path.basename(path))
        if os.path.exists(full_path):
            os.remove(full_path)


def _cascade_delete_purchase_invoice(conn, purchase_invoice_id: int) -> None:
    row = conn.execute(
        "SELECT supplier_pdf_path FROM purchase_invoices WHERE id = ?", (purchase_invoice_id,)
    ).fetchone()
    conn.execute("DELETE FROM packing_lists WHERE purchase_invoice_id = ?", (purchase_invoice_id,))
    conn.execute("DELETE FROM purchase_invoices WHERE id = ?", (purchase_invoice_id,))
    if row:
        _delete_purchase_invoice_pdf_files([row["supplier_pdf_path"]])


def _cascade_delete_purchase_order(conn, purchase_order_id: int) -> None:
    for row in conn.execute(
        "SELECT id FROM purchase_invoices WHERE purchase_order_id = ?", (purchase_order_id,)
    ).fetchall():
        _cascade_delete_purchase_invoice(conn, row["id"])
    conn.execute("DELETE FROM packing_lists WHERE purchase_order_id = ?", (purchase_order_id,))
    conn.execute("DELETE FROM purchase_orders WHERE id = ?", (purchase_order_id,))


def _cascade_delete_proforma_invoice(conn, proforma_invoice_id: int) -> None:
    for row in conn.execute(
        "SELECT id FROM purchase_orders WHERE proforma_invoice_id = ?", (proforma_invoice_id,)
    ).fetchall():
        _cascade_delete_purchase_order(conn, row["id"])
    conn.execute("DELETE FROM packing_lists WHERE proforma_invoice_id = ?", (proforma_invoice_id,))
    conn.execute("DELETE FROM proforma_invoices WHERE id = ?", (proforma_invoice_id,))


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
        quotation.containers = [
            dict(r) for r in self.db.query(
                "SELECT container_type, container_count FROM quotation_containers "
                "WHERE quotation_id = ? ORDER BY sr_no", (quotation_id,)
            )
        ]
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
        quotation link; the clients originating `lead_id` (Client.lead_id)
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
                buyer_reference_no, port_of_loading, port_of_discharge, final_destination, packing_details,
                shipping_mode, shipping_terms, payment_terms,
                price_validity_days, remarks,
                sea_freight, insurance, certification, other_charges,
                discount_amount, fob_pricing, round_off, cif_adjust_usd, bank_name, bank_account_number, bank_ifsc_code,
                bank_swift_code, bank_branch, bank_address, currency_code, currency_symbol, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (quotation.company_id, quotation.quotation_number, quotation.quotation_date, quotation.lead_id,
             quotation.buyer_name, quotation.buyer_address, quotation.buyer_reference_no,
             quotation.port_of_loading, quotation.port_of_discharge, quotation.final_destination, quotation.packing_details,
             quotation.shipping_mode, quotation.shipping_terms,
             quotation.payment_terms,
             quotation.price_validity_days, quotation.remarks,
             quotation.sea_freight, quotation.insurance, quotation.certification, quotation.other_charges,
             quotation.discount_amount, int(bool(quotation.fob_pricing)), quotation.round_off, quotation.cif_adjust_usd,
             quotation.bank_name, quotation.bank_account_number, quotation.bank_ifsc_code,
             quotation.bank_swift_code, quotation.bank_branch, quotation.bank_address,
             quotation.currency_code, quotation.currency_symbol,
             quotation.created_by),
        )
        self._replace_items(new_id, quotation.items)
        self._replace_containers(new_id, quotation.containers)
        return self.get_by_id(new_id)

    def update(self, quotation_id: int, quotation: Quotation) -> None:
        self.db.execute(
            """UPDATE quotations SET quotation_date = ?, lead_id = ?, buyer_name = ?,
                                      buyer_address = ?, buyer_reference_no = ?, port_of_loading = ?,
                                      port_of_discharge = ?, final_destination = ?, packing_details = ?,
                                      shipping_mode = ?, shipping_terms = ?, payment_terms = ?,
                                      price_validity_days = ?,
                                      remarks = ?, sea_freight = ?, insurance = ?, certification = ?,
                                      other_charges = ?, discount_amount = ?, fob_pricing = ?, round_off = ?,
                                      cif_adjust_usd = ?,
                                      bank_name = ?, bank_account_number = ?,
                                      bank_ifsc_code = ?, bank_swift_code = ?, bank_branch = ?, bank_address = ?,
                                      currency_code = ?, currency_symbol = ?,
                                      updated_at = datetime('now')
               WHERE id = ?""",
            (quotation.quotation_date, quotation.lead_id, quotation.buyer_name,
             quotation.buyer_address, quotation.buyer_reference_no, quotation.port_of_loading,
             quotation.port_of_discharge, quotation.final_destination, quotation.packing_details,
             quotation.shipping_mode, quotation.shipping_terms, quotation.payment_terms,
             quotation.price_validity_days,
             quotation.remarks, quotation.sea_freight, quotation.insurance, quotation.certification,
             quotation.other_charges, quotation.discount_amount, int(bool(quotation.fob_pricing)),
             quotation.round_off, quotation.cif_adjust_usd, quotation.bank_name,
             quotation.bank_account_number, quotation.bank_ifsc_code, quotation.bank_swift_code,
             quotation.bank_branch, quotation.bank_address,
             quotation.currency_code, quotation.currency_symbol, quotation_id),
        )
        self._replace_items(quotation_id, quotation.items)
        self._replace_containers(quotation_id, quotation.containers)

    def _replace_items(self, quotation_id: int, items: List[QuotationItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM quotation_items WHERE quotation_id = ?", (quotation_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO quotation_items
                       (quotation_id, sr_no, product_id, product_name, dimension_mm, hsn_code,
                        quantity_boxes, quantity_unit, pallets, quantity_value, unit, price_usd,
                        fob_price_usd, total_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (quotation_id, item.sr_no, item.product_id, item.product_name, item.dimension_mm,
                     item.hsn_code, item.quantity_boxes, item.quantity_unit, item.pallets, item.quantity_value,
                     item.unit, item.price_usd, item.fob_price_usd, item.total_usd),
                )

    def _replace_containers(self, quotation_id: int, containers: list) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM quotation_containers WHERE quotation_id = ?", (quotation_id,))
            for i, c in enumerate(containers, start=1):
                conn.execute(
                    "INSERT INTO quotation_containers (quotation_id, sr_no, container_type, container_count) "
                    "VALUES (?, ?, ?, ?)",
                    (quotation_id, i, c["container_type"], c["container_count"]),
                )

    def delete(self, quotation_id: int) -> None:
        """Deleting a quotation cascades to every document generated under
        it: proforma invoices raised from it (which themselves cascade to
        their purchase orders/invoices/packing lists - see
        _cascade_delete_proforma_invoice) and any packing list made
        directly from the quotation."""
        with self.db.get_connection() as conn:
            for row in conn.execute(
                "SELECT id FROM proforma_invoices WHERE quotation_id = ?", (quotation_id,)
            ).fetchall():
                _cascade_delete_proforma_invoice(conn, row["id"])
            conn.execute("DELETE FROM packing_lists WHERE quotation_id = ?", (quotation_id,))
            conn.execute("DELETE FROM quotations WHERE id = ?", (quotation_id,))


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
        invoice.containers = [
            dict(r) for r in self.db.query(
                "SELECT container_type, container_count FROM proforma_invoice_containers "
                "WHERE proforma_invoice_id = ? ORDER BY sr_no", (invoice_id,)
            )
        ]
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

    def quotation_id_map(self, proforma_invoice_ids: List[int]) -> dict:
        """proforma_invoice_id -> quotation_id for every invoice in the list
        that has one, in a single query - ProformaFulfilmentService's
        quotation-ancestor fallback batches through this instead of loading
        each invoice one at a time."""
        if not proforma_invoice_ids:
            return {}
        placeholders = ",".join("?" for _ in proforma_invoice_ids)
        rows = self.db.query(
            f"SELECT id, quotation_id FROM proforma_invoices "
            f"WHERE id IN ({placeholders}) AND quotation_id IS NOT NULL",
            tuple(proforma_invoice_ids),
        )
        return {row["id"]: row["quotation_id"] for row in rows}

    def create(self, invoice: ProformaInvoice) -> ProformaInvoice:
        new_id = self.db.execute(
            """INSERT INTO proforma_invoices
               (company_id, invoice_number, invoice_date, quotation_id, export_ref_no,
                buyer_order_no, other_reference, consignee_name, consignee_address, notify_name,
                notify_address, country_of_origin, country_of_destination,
                port_of_loading, port_of_discharge, final_destination, transhipment, partial_shipment,
                variation_in_qty, delivery_period, packing_details, terms_of_delivery,
                payment_terms, remarks, sea_freight, insurance, certification, other_charges, discount_amount,
                fob_pricing, round_off,
                bank_name, bank_account_number, bank_ifsc_code, bank_swift_code, bank_branch,
                bank_address, display_mode, status, currency_code, currency_symbol, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice.company_id, invoice.invoice_number, invoice.invoice_date,
             invoice.quotation_id, invoice.export_ref_no, invoice.buyer_order_no, invoice.other_reference,
             invoice.consignee_name, invoice.consignee_address, invoice.notify_name, invoice.notify_address,
             invoice.country_of_origin, invoice.country_of_destination,
             invoice.port_of_loading, invoice.port_of_discharge, invoice.final_destination,
             invoice.transhipment, invoice.partial_shipment, invoice.variation_in_qty,
             invoice.delivery_period, invoice.packing_details, invoice.terms_of_delivery,
             invoice.payment_terms, invoice.remarks, invoice.sea_freight, invoice.insurance,
             invoice.certification, invoice.other_charges, invoice.discount_amount,
             int(bool(invoice.fob_pricing)), invoice.round_off, invoice.bank_name,
             invoice.bank_account_number, invoice.bank_ifsc_code, invoice.bank_swift_code,
             invoice.bank_branch, invoice.bank_address, invoice.display_mode, invoice.status,
             invoice.currency_code, invoice.currency_symbol,
             invoice.created_by),
        )
        self._replace_items(new_id, invoice.items)
        self._replace_containers(new_id, invoice.containers)
        return self.get_by_id(new_id)

    def update(self, invoice_id: int, invoice: ProformaInvoice) -> None:
        """Deliberately does NOT write `status` - draft/confirmed is only ever
        moved by update_status below, so re-saving an invoice can never
        silently un-confirm it (or confirm it) as a side effect."""
        self.db.execute(
            """UPDATE proforma_invoices SET invoice_date = ?, quotation_id = ?,
                                             export_ref_no = ?, buyer_order_no = ?, other_reference = ?,
                                             consignee_name = ?, consignee_address = ?, notify_name = ?,
                                             notify_address = ?, country_of_origin = ?, country_of_destination = ?,
                                             port_of_loading = ?, port_of_discharge = ?,
                                             final_destination = ?, transhipment = ?, partial_shipment = ?,
                                             variation_in_qty = ?, delivery_period = ?,
                                             packing_details = ?, terms_of_delivery = ?, payment_terms = ?, remarks = ?,
                                             sea_freight = ?, insurance = ?, certification = ?,
                                             other_charges = ?, discount_amount = ?,
                                             fob_pricing = ?, round_off = ?, bank_name = ?,
                                             bank_account_number = ?, bank_ifsc_code = ?, bank_swift_code = ?,
                                             bank_branch = ?, bank_address = ?, display_mode = ?,
                                             currency_code = ?, currency_symbol = ?,
                                             updated_at = datetime('now')
               WHERE id = ?""",
            (invoice.invoice_date, invoice.quotation_id, invoice.export_ref_no,
             invoice.buyer_order_no, invoice.other_reference, invoice.consignee_name,
             invoice.consignee_address, invoice.notify_name, invoice.notify_address,
             invoice.country_of_origin, invoice.country_of_destination,
             invoice.port_of_loading, invoice.port_of_discharge, invoice.final_destination,
             invoice.transhipment, invoice.partial_shipment, invoice.variation_in_qty,
             invoice.delivery_period, invoice.packing_details, invoice.terms_of_delivery,
             invoice.payment_terms, invoice.remarks, invoice.sea_freight, invoice.insurance,
             invoice.certification, invoice.other_charges, invoice.discount_amount,
             int(bool(invoice.fob_pricing)), invoice.round_off, invoice.bank_name,
             invoice.bank_account_number, invoice.bank_ifsc_code, invoice.bank_swift_code,
             invoice.bank_branch, invoice.bank_address, invoice.display_mode,
             invoice.currency_code, invoice.currency_symbol, invoice_id),
        )
        self._replace_items(invoice_id, invoice.items)
        self._replace_containers(invoice_id, invoice.containers)

    def update_status(self, invoice_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE proforma_invoices SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, invoice_id),
        )

    def list_by_status(self, company_id: int, status: str) -> List[ProformaInvoice]:
        """Every invoice at one status, newest first - powers the "confirmed,
        purchase orders still pending" reminder feed."""
        rows = self.db.query(
            """SELECT pi.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM proforma_invoice_items WHERE proforma_invoice_id = pi.id), 0) AS items_total
               FROM proforma_invoices pi
               JOIN users u ON u.id = pi.created_by
               WHERE pi.company_id = ? AND pi.status = ?
               ORDER BY pi.invoice_date DESC, pi.id DESC""",
            (company_id, status),
        )
        return [ProformaInvoice.from_row(r) for r in rows]

    def _replace_items(self, invoice_id: int, items: List[ProformaInvoiceItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM proforma_invoice_items WHERE proforma_invoice_id = ?", (invoice_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO proforma_invoice_items
                       (proforma_invoice_id, sr_no, product_id, product_name, dimension_mm, hsn_code,
                        surface, pallets, quantity_boxes, quantity_unit, quantity_value, unit, price_usd,
                        fob_price_usd, total_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, item.sr_no, item.product_id, item.product_name, item.dimension_mm,
                     item.hsn_code, item.surface, item.pallets, item.quantity_boxes, item.quantity_unit,
                     item.quantity_value, item.unit, item.price_usd, item.fob_price_usd, item.total_usd),
                )

    def _replace_containers(self, invoice_id: int, containers: list) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM proforma_invoice_containers WHERE proforma_invoice_id = ?", (invoice_id,))
            for i, c in enumerate(containers, start=1):
                conn.execute(
                    "INSERT INTO proforma_invoice_containers (proforma_invoice_id, sr_no, container_type, container_count) "
                    "VALUES (?, ?, ?, ?)",
                    (invoice_id, i, c["container_type"], c["container_count"]),
                )

    def delete(self, invoice_id: int) -> None:
        """Deleting a proforma invoice cascades to every purchase order
        raised against it (which themselves cascade further, see
        _cascade_delete_purchase_order) and any packing list made directly
        from this proforma invoice."""
        with self.db.get_connection() as conn:
            _cascade_delete_proforma_invoice(conn, invoice_id)


class ExportInvoiceRepository:
    """Persistence for the Export Invoice: the header row (which carries the
    single Buyer Order No & Date shared by every linked PI) plus its items
    and child lists (proforma links, containers, per-container 11B rows,
    purchase details). It is a leaf of the pipeline - nothing is generated
    from it - so delete just cascades its own children (all ON DELETE
    CASCADE). Mirrors ProformaInvoiceRepository."""

    def __init__(self, db: Database):
        self.db = db

    def number_exists(self, company_id: int, export_invoice_number: str, exclude_id: Optional[int] = None) -> bool:
        """Export invoice numbers are typed in by hand (not auto-generated),
        so create/update check this first to give a clean validation error
        instead of tripping the UNIQUE(company_id, export_invoice_number)
        constraint."""
        row = self.db.query_one(
            "SELECT id FROM export_invoices WHERE company_id = ? AND export_invoice_number = ? AND id != ?",
            (company_id, export_invoice_number, exclude_id or 0),
        )
        return row is not None

    def get_by_id(self, invoice_id: int) -> Optional[ExportInvoice]:
        row = self.db.query_one(
            """SELECT ei.*, u.full_name AS created_by_name FROM export_invoices ei
               JOIN users u ON u.id = ei.created_by WHERE ei.id = ?""",
            (invoice_id,),
        )
        if not row:
            return None
        invoice = ExportInvoice.from_row(row)
        invoice.items = [
            ExportInvoiceItem.from_row(r) for r in self.db.query(
                "SELECT * FROM export_invoice_items WHERE export_invoice_id = ? ORDER BY sr_no", (invoice_id,)
            )
        ]
        invoice.proforma_invoice_ids = [
            r["proforma_invoice_id"] for r in self.db.query(
                "SELECT proforma_invoice_id FROM export_invoice_proforma_links WHERE export_invoice_id = ? ORDER BY id",
                (invoice_id,),
            )
        ]
        invoice.linked_proformas = [
            dict(r) for r in self.db.query(
                """SELECT pi.id, pi.invoice_number, pi.invoice_date
                   FROM export_invoice_proforma_links l
                   JOIN proforma_invoices pi ON pi.id = l.proforma_invoice_id
                   WHERE l.export_invoice_id = ? ORDER BY pi.invoice_date, pi.id""",
                (invoice_id,),
            )
        ]
        invoice.containers = [
            dict(r) for r in self.db.query(
                "SELECT container_type, container_count FROM export_invoice_containers "
                "WHERE export_invoice_id = ? ORDER BY sr_no", (invoice_id,)
            )
        ]
        invoice.container_details = [
            dict(r) for r in self.db.query(
                "SELECT sr_no, container_type, container_no, line_seal_no, rfid_seal_no, vehicle_no, lr_no, transporter_name, "
                "max_permitted_weight, tare_weight_kg, gross_weight, net_weight, "
                "weighbridge_name, weighing_slip_no, sealing_time, sealing_date "
                "FROM export_invoice_container_details WHERE export_invoice_id = ? ORDER BY sr_no", (invoice_id,)
            )
        ]
        invoice.purchase_details = [
            dict(r) for r in self.db.query(
                "SELECT supplier_gstin, supplier_invoice_no, supplier_name, purchase_type, epcg_number, epcg_date "
                "FROM export_invoice_purchase_details "
                "WHERE export_invoice_id = ? ORDER BY sr_no", (invoice_id,)
            )
        ]
        invoice.product_sources = [
            dict(r) for r in self.db.query(
                "SELECT product_name, po_number, quantity_boxes FROM export_invoice_product_sources "
                "WHERE export_invoice_id = ? ORDER BY sr_no", (invoice_id,)
            )
        ]
        return invoice

    def list_all(self, company_id: int) -> List[ExportInvoice]:
        rows = self.db.query(
            """SELECT ei.*, u.full_name AS created_by_name,
                      COALESCE((SELECT SUM(total_usd) FROM export_invoice_items WHERE export_invoice_id = ei.id), 0) AS items_total,
                      (SELECT COUNT(*) FROM export_invoice_proforma_links WHERE export_invoice_id = ei.id) AS proforma_link_count
               FROM export_invoices ei
               JOIN users u ON u.id = ei.created_by
               WHERE ei.company_id = ?
               ORDER BY ei.invoice_date DESC, ei.id DESC""",
            (company_id,),
        )
        invoices = []
        for r in rows:
            invoice = ExportInvoice.from_row(r)
            # list view doesn't load the link rows; carry just the count as a
            # placeholder list so templates can show "how many PIs".
            invoice.proforma_invoice_ids = [None] * (r["proforma_link_count"] or 0)
            invoices.append(invoice)
        return invoices

    def list_for_proforma(self, proforma_invoice_id: int, company_id: int) -> List[ExportInvoice]:
        """Every export invoice generated from this proforma invoice, newest
        first - the reverse of proforma_invoice_ids, for the PI page's link
        back to whatever export invoice(s) it was already used to build."""
        rows = self.db.query(
            """SELECT ei.*, u.full_name AS created_by_name
               FROM export_invoices ei
               JOIN users u ON u.id = ei.created_by
               JOIN export_invoice_proforma_links l ON l.export_invoice_id = ei.id
               WHERE l.proforma_invoice_id = ? AND ei.company_id = ?
               ORDER BY ei.id DESC""",
            (proforma_invoice_id, company_id),
        )
        return [ExportInvoice.from_row(r) for r in rows]

    def create(self, invoice: ExportInvoice) -> ExportInvoice:
        new_id = self.db.execute(
            """INSERT INTO export_invoices
               (company_id, export_invoice_number, invoice_date, lead_id, consignee_name, consignee_address,
                notify_name, notify_address, country_of_origin, country_of_destination, place_of_receipt,
                pre_carriage_by, port_of_loading, port_of_discharge, final_destination, nature_of_contract,
                payment_terms, buyer_order_no, buyer_order_date, export_under, epcg_number, epcg_date, loading_type, tax_mode, exchange_rate,
                sea_freight, insurance, certification, other_charges, discount_amount,
                fob_pricing, round_off, fob_value, cnf_value,
                bank_name, bank_account_number, bank_ifsc_code, bank_swift_code, bank_branch, bank_address,
                authorised_person_name, authorised_person_designation, self_sealing_declaration,
                shipping_bill_pdf_path, examination_date, location_code_08b, booking_no, vessel_name, voyage_no,
                issuing_authority,
                issuing_authority_address, permission_no, permission_date, permission_expiry,
                permission_is_one_time,
                manufacturer_name, manufacturer_address, stuffing_location, remarks,
                total_net_weight_kg, total_gross_weight_kg, shipping_bill_no,
                shipping_bill_date, currency_code, currency_symbol, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice.company_id, invoice.export_invoice_number) + self._header_params(invoice) + (invoice.created_by,),
        )
        self._replace_children(new_id, invoice)
        return self.get_by_id(new_id)

    def update(self, invoice_id: int, invoice: ExportInvoice) -> None:
        self.db.execute(
            """UPDATE export_invoices SET export_invoice_number = ?, invoice_date = ?, lead_id = ?, consignee_name = ?, consignee_address = ?,
                   notify_name = ?, notify_address = ?, country_of_origin = ?, country_of_destination = ?,
                   place_of_receipt = ?, pre_carriage_by = ?, port_of_loading = ?, port_of_discharge = ?,
                   final_destination = ?, nature_of_contract = ?, payment_terms = ?, buyer_order_no = ?, buyer_order_date = ?, export_under = ?,
                   epcg_number = ?, epcg_date = ?, loading_type = ?, tax_mode = ?, exchange_rate = ?,
                   sea_freight = ?, insurance = ?, certification = ?, other_charges = ?, discount_amount = ?,
                   fob_pricing = ?, round_off = ?, fob_value = ?, cnf_value = ?, bank_name = ?, bank_account_number = ?, bank_ifsc_code = ?,
                   bank_swift_code = ?, bank_branch = ?, bank_address = ?, authorised_person_name = ?,
                   authorised_person_designation = ?, self_sealing_declaration = ?, shipping_bill_pdf_path = ?,
                   examination_date = ?, location_code_08b = ?, booking_no = ?, vessel_name = ?, voyage_no = ?,
                   issuing_authority = ?, issuing_authority_address = ?,
                   permission_no = ?, permission_date = ?, permission_expiry = ?, permission_is_one_time = ?, manufacturer_name = ?,
                   manufacturer_address = ?, stuffing_location = ?, remarks = ?,
                   total_net_weight_kg = ?, total_gross_weight_kg = ?, shipping_bill_no = ?,
                   shipping_bill_date = ?, currency_code = ?, currency_symbol = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (invoice.export_invoice_number,) + self._header_params(invoice) + (invoice_id,),
        )
        self._replace_children(invoice_id, invoice)

    # Columns on export_invoices that belong to one of its ATTACHMENTS rather
    # than to the export invoice form: each attachment has its own small form
    # and writes only its own columns, via update_document_fields below.
    #
    # None of them may join the header tuple further down. The export invoice
    # form never posts them, and _build_header turns an absent field into
    # None - so carrying them through the shared create/update path would
    # blank them every time that form is saved.
    TAX_INVOICE_FIELDS = (
        "tax_invoice_number", "tax_invoice_date", "eway_bill_no", "eway_bill_date",
    )
    VGM_DECLARATION_FIELDS = (
        "vgm_signatory", "vgm_contact_24x7", "vgm_weighing_method",
        "vgm_cargo_type", "vgm_hazardous_details",
    )
    PACKING_LIST_FIELDS = ("bill_of_lading_no", "bill_of_lading_date")

    def update_document_fields(self, invoice_id: int, fields: dict, names: Sequence[str]) -> None:
        """Write just `names` onto this invoice - see the note above."""
        assignments = ", ".join(f"{name} = ?" for name in names)
        self.db.execute(
            f"UPDATE export_invoices SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            tuple(fields.get(name) for name in names) + (invoice_id,),
        )

    def update_container_detail_fields(self, invoice_id: int, rows: List[dict],
                                       fields: Sequence[str]) -> None:
        """Write `fields` onto this invoice's 11B rows, matched on sr_no.

        Used by the per-container documents that own a couple of columns each
        (the VGM attachment's weighbridge pair, the E-Seal sheet's sealing
        time/date). A targeted write for the same reason
        update_tax_invoice_details is one: the export invoice form has no
        input for any of them, so they must not travel on the path that
        rewrites the 11B rows wholesale.

        An sr_no this invoice doesn't have matches nothing, so a stale form
        can neither create rows nor reach another invoice's."""
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.db.get_connection() as conn:
            for row in rows:
                conn.execute(
                    f"UPDATE export_invoice_container_details SET {assignments} "
                    "WHERE export_invoice_id = ? AND sr_no = ?",
                    tuple(row.get(name) for name in fields) + (invoice_id, row.get("sr_no")),
                )

    def _header_params(self, invoice: ExportInvoice) -> tuple:
        """The EDITABLE header column values, in the exact order both INSERT
        and UPDATE list them (from invoice_date onward). company_id is
        immutable, so create() prepends it separately; export_invoice_number
        is now hand-entered and editable, so create() also prepends it
        separately (right after company_id) while update() prepends it on
        its own ahead of this tuple. Kept as one tuple so the two long column
        lists can never drift apart."""
        return (
            invoice.invoice_date, invoice.lead_id,
            invoice.consignee_name, invoice.consignee_address, invoice.notify_name, invoice.notify_address,
            invoice.country_of_origin, invoice.country_of_destination, invoice.place_of_receipt,
            invoice.pre_carriage_by, invoice.port_of_loading, invoice.port_of_discharge, invoice.final_destination,
            invoice.nature_of_contract, invoice.payment_terms, invoice.buyer_order_no, invoice.buyer_order_date,
            invoice.export_under, invoice.epcg_number,
            invoice.epcg_date, invoice.loading_type, invoice.tax_mode, invoice.exchange_rate, invoice.sea_freight,
            invoice.insurance, invoice.certification, invoice.other_charges, invoice.discount_amount,
            int(bool(invoice.fob_pricing)), invoice.round_off, invoice.fob_value, invoice.cnf_value, invoice.bank_name, invoice.bank_account_number,
            invoice.bank_ifsc_code, invoice.bank_swift_code, invoice.bank_branch, invoice.bank_address,
            invoice.authorised_person_name, invoice.authorised_person_designation, invoice.self_sealing_declaration,
            invoice.shipping_bill_pdf_path, invoice.examination_date, invoice.location_code_08b, invoice.booking_no,
            invoice.vessel_name, invoice.voyage_no, invoice.issuing_authority, invoice.issuing_authority_address, invoice.permission_no,
            invoice.permission_date, invoice.permission_expiry, int(bool(invoice.permission_is_one_time)), invoice.manufacturer_name,
            invoice.manufacturer_address, invoice.stuffing_location, invoice.remarks,
            invoice.total_net_weight_kg, invoice.total_gross_weight_kg,
            invoice.shipping_bill_no, invoice.shipping_bill_date,
            invoice.currency_code, invoice.currency_symbol,
        )

    def _replace_children(self, invoice_id: int, invoice: ExportInvoice) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM export_invoice_items WHERE export_invoice_id = ?", (invoice_id,))
            for item in invoice.items:
                conn.execute(
                    """INSERT INTO export_invoice_items
                       (export_invoice_id, sr_no, product_id, product_name, dimension_mm, hsn_code, surface,
                        pallets, quantity_boxes, quantity_unit, quantity_value, unit, price_usd,
                        fob_price_usd, total_usd, igst_percent, pallet_weight_kg)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, item.sr_no, item.product_id, item.product_name, item.dimension_mm, item.hsn_code,
                     item.surface, item.pallets, item.quantity_boxes, item.quantity_unit, item.quantity_value,
                     item.unit, item.price_usd, item.fob_price_usd, item.total_usd, item.igst_percent,
                     item.pallet_weight_kg),
                )

            conn.execute("DELETE FROM export_invoice_proforma_links WHERE export_invoice_id = ?", (invoice_id,))
            for pid in dict.fromkeys(invoice.proforma_invoice_ids):  # de-dup, keep order
                conn.execute(
                    "INSERT INTO export_invoice_proforma_links (export_invoice_id, proforma_invoice_id) VALUES (?, ?)",
                    (invoice_id, pid),
                )

            conn.execute("DELETE FROM export_invoice_containers WHERE export_invoice_id = ?", (invoice_id,))
            for i, c in enumerate(invoice.containers, start=1):
                conn.execute(
                    "INSERT INTO export_invoice_containers (export_invoice_id, sr_no, container_type, container_count) "
                    "VALUES (?, ?, ?, ?)",
                    (invoice_id, i, c.get("container_type") or "", int(c.get("container_count") or 0)),
                )

            conn.execute("DELETE FROM export_invoice_container_details WHERE export_invoice_id = ?", (invoice_id,))
            for i, cd in enumerate(invoice.container_details, start=1):
                conn.execute(
                    "INSERT INTO export_invoice_container_details "
                    "(export_invoice_id, sr_no, container_type, container_no, line_seal_no, rfid_seal_no, vehicle_no, "
                    "lr_no, transporter_name, max_permitted_weight, tare_weight_kg, "
                    "gross_weight, net_weight, weighbridge_name, weighing_slip_no, "
                    "sealing_time, sealing_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (invoice_id, i, cd.get("container_type") or None, cd.get("container_no") or None,
                     cd.get("line_seal_no") or None, cd.get("rfid_seal_no") or None, cd.get("vehicle_no") or None,
                     cd.get("lr_no") or None, cd.get("transporter_name") or None,
                     cd.get("max_permitted_weight") or None,
                     cd.get("tare_weight_kg"), cd.get("gross_weight") or None, cd.get("net_weight") or None,
                     cd.get("weighbridge_name") or None, cd.get("weighing_slip_no") or None,
                     cd.get("sealing_time") or None, cd.get("sealing_date") or None),
                )

            conn.execute("DELETE FROM export_invoice_purchase_details WHERE export_invoice_id = ?", (invoice_id,))
            for i, pd in enumerate(invoice.purchase_details, start=1):
                conn.execute(
                    "INSERT INTO export_invoice_purchase_details "
                    "(export_invoice_id, sr_no, supplier_gstin, supplier_invoice_no, supplier_name, purchase_type, "
                    " epcg_number, epcg_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (invoice_id, i, pd.get("supplier_gstin") or None, pd.get("supplier_invoice_no") or None,
                     pd.get("supplier_name") or None, pd.get("purchase_type") or "full_tax",
                     pd.get("epcg_number") or None, pd.get("epcg_date") or None),
                )

            conn.execute("DELETE FROM export_invoice_product_sources WHERE export_invoice_id = ?", (invoice_id,))
            for i, ps in enumerate(invoice.product_sources, start=1):
                conn.execute(
                    "INSERT INTO export_invoice_product_sources "
                    "(export_invoice_id, sr_no, product_name, po_number, quantity_boxes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (invoice_id, i, ps.get("product_name") or None, ps.get("po_number") or None,
                     ps.get("quantity_boxes") or 0),
                )

    def delete(self, invoice_id: int) -> None:
        # Leaf document - the child tables all cascade, nothing downstream to null.
        self.db.execute("DELETE FROM export_invoices WHERE id = ?", (invoice_id,))

    def source_purchase_order_ids(self, export_invoice_id: int, company_id: int) -> List[int]:
        """Which purchase orders actually supplied this export invoice's
        goods - what scopes the Designs Packing List's reference list to the
        designs that were really on this shipment.

        Two routes, because the precise one isn't always populated:
        1. export_invoice_product_sources names the contributing PO per goods
           line, but only by po_number (free text, no FK), so it is resolved
           back to an id here;
        2. otherwise the linked proforma invoices' own purchase orders - the
           same walk design_totals_for_linked_purchase_orders already does.
        Returns [] when neither resolves, and the caller falls back to an
        unscoped reference."""
        rows = self.db.query(
            """SELECT DISTINCT po.id AS id
               FROM export_invoice_product_sources ps
               JOIN purchase_orders po ON po.po_number = ps.po_number AND po.company_id = ?
               WHERE ps.export_invoice_id = ?""",
            (company_id, export_invoice_id),
        )
        if rows:
            return [r["id"] for r in rows]
        rows = self.db.query(
            """SELECT DISTINCT po.id AS id
               FROM export_invoice_proforma_links l
               JOIN purchase_orders po ON po.proforma_invoice_id = l.proforma_invoice_id
                                      AND po.company_id = ?
               WHERE l.export_invoice_id = ?""",
            (company_id, export_invoice_id),
        )
        return [r["id"] for r in rows]

    # ---- inventory (per-design Sale Qty, for the design's Stock History card) ----
    def sold_totals_by_design(self, company_id: int) -> dict:
        """design_id -> {boxes, quantity, qty_unit, unit} sold, read off the
        Designs Packing List's per-container allocation
        (export_packing_list_item_designs) rather than the export invoice's
        own goods lines: a goods line is priced per PRODUCT and its boxes are
        usually a mix of designs split across containers, which is exactly
        what that table records. Lines nobody has allocated designs for yet
        simply don't count."""
        rows = self.db.query(
            """SELECT dz.design_id AS design_id,
                      COALESCE(SUM(dz.quantity_boxes), 0) AS boxes,
                      COALESCE(SUM(dz.quantity_value), 0) AS quantity,
                      MIN(p.quantity_unit) AS qty_unit,
                      MIN(p.alternate_quantity_unit) AS unit
               FROM export_packing_lists epl
               JOIN export_invoices ei ON ei.id = epl.export_invoice_id
               JOIN export_packing_list_item_designs dz ON dz.export_packing_list_id = epl.id
               JOIN designs d ON d.id = dz.design_id
               JOIN products p ON p.id = d.product_id
               WHERE ei.company_id = ? AND dz.design_id IS NOT NULL
               GROUP BY dz.design_id""",
            (company_id,),
        )
        return {r["design_id"]: {"boxes": r["boxes"], "quantity": r["quantity"],
                                 "qty_unit": r["qty_unit"], "unit": r["unit"]}
                for r in rows}

    def sold_history_for_design(self, company_id: int, design_id: int) -> List[dict]:
        """One row per export invoice this design was sold on (via the
        Designs Packing List allocation) - the sell side of the design's
        Purchase / Sale history. Newest first.

        Two independent signals let the caller trace a sale back to its
        source PO -> Purchase Invoice chain:
        - `po_numbers`: purchase order numbers the invoice's own goods line
          (same product) was sourced from (export_invoice_product_sources,
          filled in from ExportInvoiceService.build_prefill_from_proformas).
        - `pi_invoice_numbers`: supplier invoice numbers from this export
          invoice's own Purchase Details block (export_invoice_purchase_details,
          same build), which is `purchase_invoices.invoice_number` for
          whichever purchase invoices actually fed it - so a PO's own
          purchase invoice(s) can be matched here directly, PO -> PI ->
          Export Invoice, not just PO -> Export Invoice.

        Neither is scoped to this exact design's boxes specifically (a
        goods line/Purchase Details block covers the whole product, not a
        design split), so a match is corroborating evidence of the chain,
        not a guaranteed exact lineage."""
        rows = self.db.query(
            """SELECT ei.id AS export_invoice_id, ei.export_invoice_number AS export_invoice_number,
                      ei.invoice_date AS invoice_date, ei.consignee_name AS consignee_name,
                      dz.quantity_boxes AS boxes, dz.quantity_value AS quantity,
                      p.quantity_unit AS qty_unit, p.alternate_quantity_unit AS unit,
                      (SELECT GROUP_CONCAT(DISTINCT eps.po_number) FROM export_invoice_product_sources eps
                        WHERE eps.export_invoice_id = ei.id AND eps.product_name = p.product_name) AS po_numbers_raw,
                      (SELECT GROUP_CONCAT(DISTINCT epd.supplier_invoice_no) FROM export_invoice_purchase_details epd
                        WHERE epd.export_invoice_id = ei.id AND epd.supplier_invoice_no IS NOT NULL) AS pi_invoice_numbers_raw
               FROM export_packing_lists epl
               JOIN export_invoices ei ON ei.id = epl.export_invoice_id
               JOIN export_packing_list_item_designs dz ON dz.export_packing_list_id = epl.id
               JOIN designs d ON d.id = dz.design_id
               JOIN products p ON p.id = d.product_id
               WHERE ei.company_id = ? AND dz.design_id = ?
               ORDER BY ei.invoice_date DESC, ei.id DESC""",
            (company_id, design_id),
        )
        results = []
        for r in rows:
            row = dict(r)
            po_raw = row.pop("po_numbers_raw")
            pi_raw = row.pop("pi_invoice_numbers_raw")
            row["po_numbers"] = po_raw.split(",") if po_raw else []
            row["pi_invoice_numbers"] = pi_raw.split(",") if pi_raw else []
            results.append(row)
        return results


class ExportPackingListRepository:
    """Persistence for the Export Packing List: a thin header (number/date)
    plus its allocation rows. Unlike every other document repository here it
    has no standalone create path in practice - `upsert_for_invoice` is what
    ExportInvoiceService calls on every save of the parent invoice, replacing
    the allocation wholesale while keeping the number/date the list was first
    given. The parent export invoice is loaded and attached by get_by_id,
    because the printed sheet reads its whole header off it."""

    def __init__(self, db: Database, export_invoice_repo: "ExportInvoiceRepository"):
        self.db = db
        self.export_invoice_repo = export_invoice_repo

    def next_number(self, company_id: int, packing_list_date: str) -> str:
        """EXPPL{YYYYMMDD}{seq}, the day-scoped sequence every generated
        document number in this app uses."""
        date_part = (packing_list_date or "")[:10].replace("-", "")
        prefix = f"EXPPL{date_part}"
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM export_packing_lists WHERE company_id = ? AND packing_list_number LIKE ?",
            (company_id, f"{prefix}%"),
        )
        return f"{prefix}{(row['c'] if row else 0) + 1:03d}"

    def _load(self, row) -> Optional[ExportPackingList]:
        if not row:
            return None
        packing_list = ExportPackingList.from_row(row)
        packing_list.items = [
            ExportPackingListItem.from_row(r) for r in self.db.query(
                "SELECT * FROM export_packing_list_items WHERE export_packing_list_id = ? ORDER BY sr_no",
                (packing_list.id,),
            )
        ]
        # Design allocations are matched onto their item by the container
        # split's own natural key (invoice_item_sr_no, container_sr_no),
        # never by export_packing_list_items.id - that id doesn't survive a
        # re-save of the parent invoice (see ExportPackingListItemDesign's
        # docstring).
        designs_by_key: dict = {}
        for r in self.db.query(
            "SELECT * FROM export_packing_list_item_designs WHERE export_packing_list_id = ?",
            (packing_list.id,),
        ):
            d = ExportPackingListItemDesign.from_row(r)
            designs_by_key.setdefault((d.invoice_item_sr_no, d.container_sr_no), []).append(d)
        for item in packing_list.items:
            item.designs = designs_by_key.get((item.invoice_item_sr_no, item.container_sr_no), [])
        packing_list.invoice = self.export_invoice_repo.get_by_id(packing_list.export_invoice_id)
        if packing_list.invoice:
            packing_list.export_invoice_number = packing_list.invoice.export_invoice_number
        return packing_list

    def get_by_id(self, packing_list_id: int) -> Optional[ExportPackingList]:
        return self._load(self.db.query_one(
            """SELECT epl.*, u.full_name AS created_by_name FROM export_packing_lists epl
               JOIN users u ON u.id = epl.created_by WHERE epl.id = ?""",
            (packing_list_id,),
        ))

    def get_for_invoice(self, export_invoice_id: int) -> Optional[ExportPackingList]:
        return self._load(self.db.query_one(
            """SELECT epl.*, u.full_name AS created_by_name FROM export_packing_lists epl
               JOIN users u ON u.id = epl.created_by WHERE epl.export_invoice_id = ?""",
            (export_invoice_id,),
        ))

    def list_all(self, company_id: int) -> List[ExportPackingList]:
        """List view only - the parent invoice's number is joined in, but
        neither the full invoice nor the allocation rows are loaded."""
        rows = self.db.query(
            """SELECT epl.*, u.full_name AS created_by_name, ei.export_invoice_number,
                      (SELECT COUNT(DISTINCT container_sr_no) FROM export_packing_list_items
                        WHERE export_packing_list_id = epl.id) AS container_count
               FROM export_packing_lists epl
               JOIN users u ON u.id = epl.created_by
               JOIN export_invoices ei ON ei.id = epl.export_invoice_id
               WHERE epl.company_id = ?
               ORDER BY epl.packing_list_date DESC, epl.id DESC""",
            (company_id,),
        )
        return [ExportPackingList.from_row(r) for r in rows]

    def upsert_for_invoice(self, packing_list: ExportPackingList) -> ExportPackingList:
        """Create the invoice's packing list, or replace the allocation of
        the one it already has. The number and date are assigned once, at
        first generation, and deliberately survive every later edit of the
        parent invoice - the paperwork already went out under that number."""
        existing = self.db.query_one(
            "SELECT id FROM export_packing_lists WHERE export_invoice_id = ?", (packing_list.export_invoice_id,)
        )
        if existing:
            packing_list_id = existing["id"]
            self.db.execute(
                "UPDATE export_packing_lists SET updated_at = datetime('now') WHERE id = ?", (packing_list_id,)
            )
        else:
            packing_list_id = self.db.execute(
                """INSERT INTO export_packing_lists
                   (company_id, export_invoice_id, packing_list_number, packing_list_date, created_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (packing_list.company_id, packing_list.export_invoice_id, packing_list.packing_list_number,
                 packing_list.packing_list_date, packing_list.created_by),
            )
        self._replace_items(packing_list_id, packing_list.items)
        return self.get_by_id(packing_list_id)

    def _replace_items(self, packing_list_id: int, items: List[ExportPackingListItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM export_packing_list_items WHERE export_packing_list_id = ?", (packing_list_id,))
            for i, item in enumerate(items, start=1):
                conn.execute(
                    """INSERT INTO export_packing_list_items
                       (export_packing_list_id, sr_no, container_sr_no, container_no, seal_no, rfid_seal_no,
                        invoice_item_sr_no, product_id, product_name, group_label, hsn_code, pallets,
                        quantity_boxes, quantity_unit, quantity_value, unit, net_weight_kg, gross_weight_kg)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (packing_list_id, i, item.container_sr_no, item.container_no, item.seal_no, item.rfid_seal_no,
                     item.invoice_item_sr_no, item.product_id, item.product_name, item.group_label, item.hsn_code,
                     item.pallets, item.quantity_boxes, item.quantity_unit, item.quantity_value, item.unit,
                     item.net_weight_kg, item.gross_weight_kg),
                )

    def delete_for_invoice(self, export_invoice_id: int) -> None:
        """Only needed for an explicit "regenerate from scratch" - deleting
        the parent invoice already cascades to both tables."""
        self.db.execute("DELETE FROM export_packing_lists WHERE export_invoice_id = ?", (export_invoice_id,))

    # ---- Designs Packing List (per-line design allocation) ----
    def save_item_designs(self, export_packing_list_id: int, invoice_item_sr_no: int, container_sr_no: int,
                          rows: List[ExportPackingListItemDesign]) -> None:
        """Replaces one container-split line's design allocation wholesale.
        Keyed on the natural key, not export_packing_list_items.id (see
        ExportPackingListItemDesign's docstring) - this table is untouched
        by _replace_items, so a normal export invoice re-save never wipes
        an allocation already saved here."""
        with self.db.get_connection() as conn:
            conn.execute(
                """DELETE FROM export_packing_list_item_designs
                   WHERE export_packing_list_id = ? AND invoice_item_sr_no = ? AND container_sr_no = ?""",
                (export_packing_list_id, invoice_item_sr_no, container_sr_no),
            )
            for row in rows:
                conn.execute(
                    """INSERT INTO export_packing_list_item_designs
                       (export_packing_list_id, invoice_item_sr_no, container_sr_no,
                        design_id, design_name, quantity_boxes, quantity_value, unit)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (export_packing_list_id, invoice_item_sr_no, container_sr_no,
                     row.design_id, row.design_name, row.quantity_boxes, row.quantity_value, row.unit),
                )


class ExportDesignsPackingListRepository:
    """Persistence for the DESIGNS PACKING LIST document. Deliberately thin:
    the allocation rows it prints live on the export packing list
    (export_packing_list_item_designs, see ExportPackingListRepository), so
    all this table holds is the document's own number/date - assigned once at
    creation and never rewritten, the same rule export_packing_lists follows."""

    def __init__(self, db: Database, export_invoice_repo: "ExportInvoiceRepository",
                 export_packing_list_repo: "ExportPackingListRepository"):
        self.db = db
        self.export_invoice_repo = export_invoice_repo
        self.export_packing_list_repo = export_packing_list_repo

    def next_number(self, company_id: int, packing_list_date: str) -> str:
        """DSGPL{YYYYMMDD}{seq}, the day-scoped sequence every generated
        document number in this app uses."""
        date_part = (packing_list_date or "")[:10].replace("-", "")
        prefix = f"DSGPL{date_part}"
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM export_designs_packing_lists "
            "WHERE company_id = ? AND packing_list_number LIKE ?",
            (company_id, f"{prefix}%"),
        )
        return f"{prefix}{(row['c'] if row else 0) + 1:03d}"

    def _load(self, row) -> Optional[ExportDesignsPackingList]:
        if not row:
            return None
        doc = ExportDesignsPackingList.from_row(row)
        doc.invoice = self.export_invoice_repo.get_by_id(doc.export_invoice_id)
        doc.packing_list = self.export_packing_list_repo.get_for_invoice(doc.export_invoice_id)
        if doc.invoice:
            doc.export_invoice_number = doc.invoice.export_invoice_number
        return doc

    def get_by_id(self, doc_id: int) -> Optional[ExportDesignsPackingList]:
        return self._load(self.db.query_one(
            """SELECT d.*, u.full_name AS created_by_name FROM export_designs_packing_lists d
               JOIN users u ON u.id = d.created_by WHERE d.id = ?""",
            (doc_id,),
        ))

    def get_for_invoice(self, export_invoice_id: int) -> Optional[ExportDesignsPackingList]:
        return self._load(self.db.query_one(
            """SELECT d.*, u.full_name AS created_by_name FROM export_designs_packing_lists d
               JOIN users u ON u.id = d.created_by WHERE d.export_invoice_id = ?""",
            (export_invoice_id,),
        ))

    def list_all(self, company_id: int) -> List[ExportDesignsPackingList]:
        rows = self.db.query(
            """SELECT d.*, u.full_name AS created_by_name, ei.export_invoice_number
               FROM export_designs_packing_lists d
               JOIN users u ON u.id = d.created_by
               JOIN export_invoices ei ON ei.id = d.export_invoice_id
               WHERE d.company_id = ?
               ORDER BY d.packing_list_date DESC, d.id DESC""",
            (company_id,),
        )
        return [ExportDesignsPackingList.from_row(r) for r in rows]

    def create(self, doc: ExportDesignsPackingList) -> ExportDesignsPackingList:
        new_id = self.db.execute(
            """INSERT INTO export_designs_packing_lists
               (company_id, export_invoice_id, packing_list_number, packing_list_date, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (doc.company_id, doc.export_invoice_id, doc.packing_list_number,
             doc.packing_list_date, doc.created_by),
        )
        return self.get_by_id(new_id)

    def touch(self, doc_id: int) -> None:
        """Bumps updated_at when the allocation behind an already-created
        document changes - the number and date deliberately stay put."""
        self.db.execute(
            "UPDATE export_designs_packing_lists SET updated_at = datetime('now') WHERE id = ?", (doc_id,)
        )

    def delete(self, doc_id: int) -> None:
        self.db.execute("DELETE FROM export_designs_packing_lists WHERE id = ?", (doc_id,))


class PurchaseOrderRepository:
    """Mirrors ProformaInvoiceRepository layer-for-layer: header + line
    items, day-scoped number sequence, reference-only lead/proforma links."""

    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM purchase_orders WHERE company_id = ? AND po_number LIKE ?",
            (company_id, f"{number_prefix}%"),
        )
        return row["cnt"] if row else 0

    # {items_total} slot lets list queries add a precomputed per-PO subtotal
    # without repeating the join block.
    _SELECT = """
        SELECT po.*, u.full_name AS created_by_name, pi.invoice_number AS proforma_invoice_number{items_total}
        FROM purchase_orders po
        JOIN users u ON u.id = po.created_by
        LEFT JOIN proforma_invoices pi ON pi.id = po.proforma_invoice_id
    """
    _ITEMS_TOTAL = (", COALESCE((SELECT SUM(total_inr) FROM purchase_order_items "
                    "WHERE purchase_order_id = po.id), 0) AS items_total")

    def get_by_id(self, purchase_order_id: int) -> Optional[PurchaseOrder]:
        row = self.db.query_one(
            self._SELECT.format(items_total="") + " WHERE po.id = ?", (purchase_order_id,)
        )
        if not row:
            return None
        purchase_order = PurchaseOrder.from_row(row)
        item_rows = self.db.query(
            "SELECT * FROM purchase_order_items WHERE purchase_order_id = ? ORDER BY sr_no", (purchase_order_id,)
        )
        purchase_order.items = [PurchaseOrderItem.from_row(r) for r in item_rows]
        return purchase_order

    def list_all(self, company_id: int) -> List[PurchaseOrder]:
        rows = self.db.query(
            self._SELECT.format(items_total=self._ITEMS_TOTAL) +
            " WHERE po.company_id = ? ORDER BY po.po_date DESC, po.id DESC",
            (company_id,),
        )
        return [PurchaseOrder.from_row(r) for r in rows]

    def list_for_seller(self, supplier_id: int) -> List[PurchaseOrder]:
        """A Supplier's natural link to its purchase orders is
        seller_supplier_id, not an originating lead - unlike Buyer,
        which sees its documents by walking up the proforma_invoice_id ->
        quotation_id chain to the Quotation's own lead_id instead."""
        rows = self.db.query(
            self._SELECT.format(items_total=self._ITEMS_TOTAL) +
            " WHERE po.seller_supplier_id = ? ORDER BY po.po_date DESC, po.id DESC",
            (supplier_id,),
        )
        return [PurchaseOrder.from_row(r) for r in rows]

    def list_for_proforma(self, proforma_invoice_id: int) -> List[PurchaseOrder]:
        """Every purchase order generated from this proforma invoice, newest
        first. One invoice is normally split across several suppliers, so the
        invoice page lists all of them rather than linking to a single PO."""
        rows = self.db.query(
            self._SELECT.format(items_total=self._ITEMS_TOTAL) +
            " WHERE po.proforma_invoice_id = ? ORDER BY po.id DESC",
            (proforma_invoice_id,),
        )
        return [PurchaseOrder.from_row(r) for r in rows]

    def count_map_by_proforma(self, company_id: int) -> dict:
        """proforma_invoice_id -> how many purchase orders point at it, so the
        proforma list can show "3 POs" without an N+1 query."""
        rows = self.db.query(
            "SELECT proforma_invoice_id, COUNT(*) AS cnt FROM purchase_orders "
            "WHERE company_id = ? AND proforma_invoice_id IS NOT NULL GROUP BY proforma_invoice_id",
            (company_id,),
        )
        return {row["proforma_invoice_id"]: row["cnt"] for row in rows}

    def product_totals_for_proforma(self, company_id: int, proforma_invoice_id: int) -> List[dict]:
        """What's already been placed across every purchase order already
        linked to this proforma invoice, summed per product line - the
        'placed' side of ProformaFulfilmentService.product_status (the PO-
        creation-time analogue of design_totals_for_linked_purchase_orders,
        which does the same job one level down at packing-list granularity).
        Keyed by product_id when known, else the row groups on product_name
        too so hand-typed lines aren't collapsed into one NULL bucket."""
        rows = self.db.query(
            """SELECT i.product_id, i.product_name,
                      COALESCE(SUM(i.quantity_boxes), 0) AS boxes,
                      COALESCE(SUM(i.quantity_value), 0) AS quantity,
                      MIN(i.unit) AS unit
               FROM purchase_orders po
               JOIN purchase_order_items i ON i.purchase_order_id = po.id
               WHERE po.company_id = ? AND po.proforma_invoice_id = ?
               GROUP BY i.product_id, i.product_name""",
            (company_id, proforma_invoice_id),
        )
        return [dict(r) for r in rows]

    def create(self, purchase_order: PurchaseOrder) -> PurchaseOrder:
        new_id = self.db.execute(
            """INSERT INTO purchase_orders
               (company_id, po_number, po_date, proforma_invoice_id, seller_supplier_id,
                seller_name, seller_address, seller_pan, seller_gstin, seller_ref_no,
                port_of_loading, port_of_discharge, container_details, delivery_time,
                advance_percent, payment_terms, remarks, igst_percent, cgst_percent, sgst_percent,
                purchase_type, tax_as_actual, currency_code, currency_symbol, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (purchase_order.company_id, purchase_order.po_number, purchase_order.po_date,
             purchase_order.proforma_invoice_id, purchase_order.seller_supplier_id,
             purchase_order.seller_name, purchase_order.seller_address, purchase_order.seller_pan,
             purchase_order.seller_gstin, purchase_order.seller_ref_no, purchase_order.port_of_loading,
             purchase_order.port_of_discharge, purchase_order.container_details, purchase_order.delivery_time,
             purchase_order.advance_percent, purchase_order.payment_terms, purchase_order.remarks,
             purchase_order.igst_percent, purchase_order.cgst_percent, purchase_order.sgst_percent,
             purchase_order.purchase_type, int(purchase_order.tax_as_actual),
             purchase_order.currency_code, purchase_order.currency_symbol,
             purchase_order.created_by),
        )
        self._replace_items(new_id, purchase_order.items)
        return self.get_by_id(new_id)

    def update(self, purchase_order_id: int, purchase_order: PurchaseOrder) -> None:
        self.db.execute(
            """UPDATE purchase_orders SET po_date = ?, proforma_invoice_id = ?,
                                           seller_supplier_id = ?, seller_name = ?, seller_address = ?,
                                           seller_pan = ?, seller_gstin = ?, seller_ref_no = ?,
                                           port_of_loading = ?, port_of_discharge = ?, container_details = ?,
                                           delivery_time = ?, advance_percent = ?, payment_terms = ?,
                                           remarks = ?, igst_percent = ?, cgst_percent = ?, sgst_percent = ?,
                                           purchase_type = ?, tax_as_actual = ?, currency_code = ?, currency_symbol = ?,
                                           updated_at = datetime('now')
               WHERE id = ?""",
            (purchase_order.po_date, purchase_order.proforma_invoice_id,
             purchase_order.seller_supplier_id, purchase_order.seller_name, purchase_order.seller_address,
             purchase_order.seller_pan, purchase_order.seller_gstin, purchase_order.seller_ref_no,
             purchase_order.port_of_loading, purchase_order.port_of_discharge, purchase_order.container_details,
             purchase_order.delivery_time, purchase_order.advance_percent, purchase_order.payment_terms,
             purchase_order.remarks, purchase_order.igst_percent, purchase_order.cgst_percent,
             purchase_order.sgst_percent, purchase_order.purchase_type, int(purchase_order.tax_as_actual),
             purchase_order.currency_code, purchase_order.currency_symbol, purchase_order_id),
        )
        self._replace_items(purchase_order_id, purchase_order.items)

    def _replace_items(self, purchase_order_id: int, items: List[PurchaseOrderItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM purchase_order_items WHERE purchase_order_id = ?", (purchase_order_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO purchase_order_items
                       (purchase_order_id, sr_no, product_id, product_name, hsn_code,
                        quantity_boxes, quantity_unit, quantity_value, unit, price_inr, price_per, total_inr,
                        design_id, design_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (purchase_order_id, item.sr_no, item.product_id, item.product_name, item.hsn_code,
                     item.quantity_boxes, item.quantity_unit, item.quantity_value, item.unit, item.price_inr,
                     item.price_per, item.total_inr, item.design_id, item.design_name),
                )

    def delete(self, purchase_order_id: int) -> None:
        """Deleting a purchase order cascades to its purchase invoice
        (including the supplier's uploaded PDF, see
        _cascade_delete_purchase_invoice) and any packing list made
        directly from this purchase order."""
        with self.db.get_connection() as conn:
            _cascade_delete_purchase_order(conn, purchase_order_id)

    # ---- inventory (per-design PO Qty, for the design's Stock History card) ----
    def ordered_totals_by_design(self, company_id: int) -> dict:
        """design_id -> {boxes, quantity, qty_unit, unit} ordered across every
        purchase order line, attributed to a design via that PO's own linked
        packing list (a PO line no longer carries a design tag itself - the
        packing list, which already tags designs for the Received/Stock
        columns, is the single source of truth). A PO+product with no
        design-tagged packing list line is skipped entirely; a PO+product
        split across several designs on the packing list is split the same
        way here, pro-rated by each design's share of that packing list's
        boxes for the product."""
        po_items = self.db.query(
            """SELECT i.purchase_order_id AS po_id, i.product_id AS product_id,
                      COALESCE(SUM(i.quantity_boxes), 0) AS boxes,
                      COALESCE(SUM(i.quantity_value), 0) AS quantity,
                      MIN(p.quantity_unit) AS qty_unit,
                      MIN(p.alternate_quantity_unit) AS unit
               FROM purchase_orders po
               JOIN purchase_order_items i ON i.purchase_order_id = po.id
               JOIN products p ON p.id = i.product_id
               WHERE po.company_id = ?
               GROUP BY i.purchase_order_id, i.product_id""",
            (company_id,),
        )
        design_tags = self.db.query(
            """SELECT pl.purchase_order_id AS po_id, pli.product_id AS product_id,
                      pli.design_id AS design_id,
                      COALESCE(SUM(pli.quantity_boxes), 0) AS boxes
               FROM packing_lists pl
               JOIN packing_list_items pli ON pli.packing_list_id = pl.id
               WHERE pl.company_id = ? AND pl.purchase_order_id IS NOT NULL
                     AND pli.design_id IS NOT NULL
               GROUP BY pl.purchase_order_id, pli.product_id, pli.design_id""",
            (company_id,),
        )
        tag_map: dict = {}
        for r in design_tags:
            tag_map.setdefault((r["po_id"], r["product_id"]), []).append((r["design_id"], r["boxes"]))

        totals: dict = {}
        for item in po_items:
            tags = tag_map.get((item["po_id"], item["product_id"]))
            if not tags:
                continue
            tag_total = sum(boxes for _, boxes in tags)
            if tag_total <= 0:
                continue
            for design_id, boxes in tags:
                share = boxes / tag_total
                entry = totals.setdefault(design_id, {"boxes": 0.0, "quantity": 0.0, "qty_unit": None, "unit": None})
                entry["boxes"] += item["boxes"] * share
                entry["quantity"] += item["quantity"] * share
                entry["qty_unit"] = entry["qty_unit"] or item["qty_unit"]
                entry["unit"] = entry["unit"] or item["unit"]
        return totals

    def purchase_history_for_design(self, company_id: int, design_id: int) -> List[dict]:
        """One row per purchase order this design was received against (via
        that PO's packing list design tags) - the buy side of the design's
        Purchase / Sale history. Newest first. po_ordered_boxes/
        po_product_tagged_boxes let the caller pro-rate the PO's own ordered
        quantity down to this design's share, the same way
        ordered_totals_by_design does at the company level."""
        rows = self.db.query(
            """SELECT po.id AS purchase_order_id, po.po_number AS po_number, po.po_date AS po_date,
                      pl.packing_list_number AS packing_list_number, i.product_id AS product_id,
                      i.quantity_boxes AS received_boxes, i.quantity_value AS received_quantity,
                      p.alternate_quantity_unit AS unit, p.quantity_unit AS qty_unit,
                      (SELECT COALESCE(SUM(oi.quantity_boxes), 0) FROM purchase_order_items oi
                        WHERE oi.purchase_order_id = po.id AND oi.product_id = i.product_id) AS po_ordered_boxes,
                      (SELECT COALESCE(SUM(pli.quantity_boxes), 0) FROM packing_lists pl2
                        JOIN packing_list_items pli ON pli.packing_list_id = pl2.id
                        WHERE pl2.purchase_order_id = po.id AND pli.product_id = i.product_id
                              AND pli.design_id IS NOT NULL) AS po_product_tagged_boxes
               FROM packing_lists pl
               JOIN purchase_orders po ON po.id = pl.purchase_order_id
               JOIN packing_list_items i ON i.packing_list_id = pl.id
               JOIN designs d ON d.id = i.design_id
               JOIN products p ON p.id = d.product_id
               WHERE pl.company_id = ? AND i.design_id = ?
               ORDER BY po.po_date DESC, po.id DESC""",
            (company_id, design_id),
        )
        return [dict(r) for r in rows]


class JobWorkRepository:
    """Persistence for the JOB WORK document: header + design lines, a
    day-scoped number sequence, and a reference-only proforma_invoice_id -
    mirrors PurchaseOrderRepository layer for layer."""

    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM job_works WHERE company_id = ? AND job_work_number LIKE ?",
            (company_id, f"{number_prefix}%"),
        )
        return row["cnt"] if row else 0

    # {totals} lets list queries add precomputed per-job-work quantity sums
    # without repeating the join block (same trick as
    # PurchaseOrderRepository._SELECT's {items_total}).
    _SELECT = """
        SELECT jw.*, u.full_name AS created_by_name, pi.invoice_number AS proforma_invoice_number{totals}
        FROM job_works jw
        JOIN users u ON u.id = jw.created_by
        LEFT JOIN proforma_invoices pi ON pi.id = jw.proforma_invoice_id
    """
    _TOTALS = (", COALESCE((SELECT SUM(job_quantity) FROM job_work_items "
               "WHERE job_work_id = jw.id), 0) AS items_job_quantity")

    def get_by_id(self, job_work_id: int) -> Optional[JobWork]:
        row = self.db.query_one(self._SELECT.format(totals="") + " WHERE jw.id = ?", (job_work_id,))
        if not row:
            return None
        job_work = JobWork.from_row(row)
        item_rows = self.db.query(
            "SELECT * FROM job_work_items WHERE job_work_id = ? ORDER BY sr_no", (job_work_id,)
        )
        job_work.items = [JobWorkItem.from_row(r) for r in item_rows]
        product_rows = self.db.query(
            "SELECT * FROM job_work_products WHERE job_work_id = ? ORDER BY sr_no", (job_work_id,)
        )
        job_work.products = [JobWorkProduct.from_row(r) for r in product_rows]
        return job_work

    def list_all(self, company_id: int) -> List[JobWork]:
        rows = self.db.query(
            self._SELECT.format(totals=self._TOTALS) +
            " WHERE jw.company_id = ? ORDER BY jw.job_work_date DESC, jw.id DESC",
            (company_id,),
        )
        return [JobWork.from_row(r) for r in rows]

    def list_for_proforma(self, proforma_invoice_id: int) -> List[JobWork]:
        """Every job work raised against one proforma invoice, newest first -
        an invoice's goods can be sent out for job work more than once."""
        rows = self.db.query(
            self._SELECT.format(totals=self._TOTALS) +
            " WHERE jw.proforma_invoice_id = ? ORDER BY jw.id DESC",
            (proforma_invoice_id,),
        )
        return [JobWork.from_row(r) for r in rows]

    def sum_job_quantity_for_product(self, product_id: int) -> float:
        """Total Job Quantity across every job-work line that produced this
        product - either directly (to_product_id) or via a design that
        belongs to it (design_id -> designs.product_id) - for the Products
        catalog page's read-only total."""
        row = self.db.query_one(
            """SELECT COALESCE(SUM(jwi.job_quantity), 0) AS total
               FROM job_work_items jwi
               LEFT JOIN designs d ON d.id = jwi.design_id
               WHERE jwi.to_product_id = ? OR d.product_id = ?""",
            (product_id, product_id),
        )
        return row["total"] if row else 0.0

    def count_map_by_proforma(self, company_id: int) -> dict:
        """proforma_invoice_id -> how many job works point at it, so the
        proforma list can show a Job work count without an N+1 query
        (mirrors PurchaseOrderRepository.count_map_by_proforma)."""
        rows = self.db.query(
            "SELECT proforma_invoice_id, COUNT(*) AS cnt FROM job_works "
            "WHERE company_id = ? AND proforma_invoice_id IS NOT NULL GROUP BY proforma_invoice_id",
            (company_id,),
        )
        return {row["proforma_invoice_id"]: row["cnt"] for row in rows}

    def create(self, job_work: JobWork) -> JobWork:
        new_id = self.db.execute(
            """INSERT INTO job_works
               (company_id, job_work_number, job_work_date, proforma_invoice_id,
                seller_supplier_id, seller_name, seller_address, seller_pan, seller_gstin,
                manufacturer_supplier_id, manufacturer_name, manufacturer_address,
                manufacturer_pan, manufacturer_gstin,
                seller_ref_no, delivery_time, advance_percent, payment_terms, remarks,
                currency_code, currency_symbol,
                igst_percent, cgst_percent, sgst_percent, purchase_type, tax_as_actual,
                created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_work.company_id, job_work.job_work_number, job_work.job_work_date,
             job_work.proforma_invoice_id, job_work.seller_supplier_id, job_work.seller_name,
             job_work.seller_address, job_work.seller_pan, job_work.seller_gstin,
             job_work.manufacturer_supplier_id, job_work.manufacturer_name,
             job_work.manufacturer_address, job_work.manufacturer_pan, job_work.manufacturer_gstin,
             job_work.seller_ref_no, job_work.delivery_time, job_work.advance_percent,
             job_work.payment_terms, job_work.remarks,
             job_work.currency_code, job_work.currency_symbol,
             job_work.igst_percent, job_work.cgst_percent, job_work.sgst_percent,
             job_work.purchase_type, int(job_work.tax_as_actual),
             job_work.created_by),
        )
        self._replace_items(new_id, job_work.items)
        self._replace_products(new_id, job_work.products)
        return self.get_by_id(new_id)

    def update(self, job_work_id: int, job_work: JobWork) -> None:
        self.db.execute(
            """UPDATE job_works SET job_work_date = ?, proforma_invoice_id = ?,
                                    seller_supplier_id = ?, seller_name = ?, seller_address = ?,
                                    seller_pan = ?, seller_gstin = ?,
                                    manufacturer_supplier_id = ?, manufacturer_name = ?,
                                    manufacturer_address = ?, manufacturer_pan = ?, manufacturer_gstin = ?,
                                    seller_ref_no = ?, delivery_time = ?, advance_percent = ?,
                                    payment_terms = ?, remarks = ?,
                                    currency_code = ?, currency_symbol = ?,
                                    igst_percent = ?, cgst_percent = ?, sgst_percent = ?,
                                    purchase_type = ?, tax_as_actual = ?,
                                    updated_at = datetime('now')
               WHERE id = ?""",
            (job_work.job_work_date, job_work.proforma_invoice_id,
             job_work.seller_supplier_id, job_work.seller_name, job_work.seller_address,
             job_work.seller_pan, job_work.seller_gstin,
             job_work.manufacturer_supplier_id, job_work.manufacturer_name,
             job_work.manufacturer_address, job_work.manufacturer_pan, job_work.manufacturer_gstin,
             job_work.seller_ref_no, job_work.delivery_time, job_work.advance_percent,
             job_work.payment_terms, job_work.remarks,
             job_work.currency_code, job_work.currency_symbol,
             job_work.igst_percent, job_work.cgst_percent, job_work.sgst_percent,
             job_work.purchase_type, int(job_work.tax_as_actual),
             job_work_id),
        )
        self._replace_items(job_work_id, job_work.items)
        self._replace_products(job_work_id, job_work.products)

    def _replace_items(self, job_work_id: int, items: List[JobWorkItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM job_work_items WHERE job_work_id = ?", (job_work_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO job_work_items
                       (job_work_id, sr_no, product_id, product_name, to_product_id, to_product_name,
                        hsn_code, design_id, design_name, unit,
                        source_quantity, conversion_value, extra_percent, converted_quantity,
                        extra_quantity, job_quantity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job_work_id, item.sr_no, item.product_id, item.product_name,
                     item.to_product_id, item.to_product_name, item.hsn_code,
                     item.design_id, item.design_name, item.unit,
                     item.source_quantity, item.conversion_value, item.extra_percent,
                     item.converted_quantity, item.extra_quantity, item.job_quantity),
                )

    def _replace_products(self, job_work_id: int, products: List[JobWorkProduct]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM job_work_products WHERE job_work_id = ?", (job_work_id,))
            for product in products:
                conn.execute(
                    """INSERT INTO job_work_products
                       (job_work_id, sr_no, product_id, product_name, hsn_code,
                        quantity_boxes, quantity_unit, quantity_value, unit,
                        price_inr, price_per, total_inr)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job_work_id, product.sr_no, product.product_id, product.product_name,
                     product.hsn_code, product.quantity_boxes, product.quantity_unit,
                     product.quantity_value, product.unit,
                     product.price_inr, product.price_per, product.total_inr),
                )

    def delete(self, job_work_id: int) -> None:
        """job_work_items/job_work_products cascade on the FK; nothing else
        hangs off a job work, so there is no cascade helper to route
        through."""
        self.db.execute("DELETE FROM job_works WHERE id = ?", (job_work_id,))


class PurchaseInvoiceRepository:
    """Mirrors PurchaseOrderRepository layer-for-layer: header + line items,
    day-scoped number sequence, reference-only purchase_order/lead links -
    plus a third child collection, vehicle numbers, which are a plain
    ordered list of strings rather than a full line-item table."""

    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM purchase_invoices WHERE company_id = ? AND purchase_invoice_number LIKE ?",
            (company_id, f"{number_prefix}%"),
        )
        return row["cnt"] if row else 0

    _SELECT = """
        SELECT pinv.*, u.full_name AS created_by_name, po.po_number AS purchase_order_number{items_total}
        FROM purchase_invoices pinv
        JOIN users u ON u.id = pinv.created_by
        LEFT JOIN purchase_orders po ON po.id = pinv.purchase_order_id
    """
    _ITEMS_TOTAL = (", COALESCE((SELECT SUM(total_inr) FROM purchase_invoice_items "
                    "WHERE purchase_invoice_id = pinv.id), 0) AS items_total")

    def _attach_children(self, purchase_invoice: PurchaseInvoice) -> PurchaseInvoice:
        item_rows = self.db.query(
            """SELECT pinvi.*, po.po_number AS source_po_number
               FROM purchase_invoice_items pinvi
               LEFT JOIN purchase_orders po ON po.id = pinvi.purchase_order_id
               WHERE pinvi.purchase_invoice_id = ? ORDER BY pinvi.sr_no""",
            (purchase_invoice.id,),
        )
        purchase_invoice.items = [PurchaseInvoiceItem.from_row(r) for r in item_rows]
        vehicle_rows = self.db.query(
            "SELECT vehicle_number FROM purchase_invoice_vehicles WHERE purchase_invoice_id = ? ORDER BY sr_no",
            (purchase_invoice.id,),
        )
        purchase_invoice.vehicle_numbers = [r["vehicle_number"] for r in vehicle_rows]
        link_rows = self.db.query(
            "SELECT purchase_order_id FROM purchase_invoice_purchase_order_links "
            "WHERE purchase_invoice_id = ? ORDER BY id",
            (purchase_invoice.id,),
        )
        purchase_invoice.purchase_order_ids = [r["purchase_order_id"] for r in link_rows]
        return purchase_invoice

    def get_by_id(self, purchase_invoice_id: int) -> Optional[PurchaseInvoice]:
        row = self.db.query_one(
            self._SELECT.format(items_total="") + " WHERE pinv.id = ?", (purchase_invoice_id,)
        )
        if not row:
            return None
        return self._attach_children(PurchaseInvoice.from_row(row))

    def list_all(self, company_id: int) -> List[PurchaseInvoice]:
        rows = self.db.query(
            self._SELECT.format(items_total=self._ITEMS_TOTAL) +
            " WHERE pinv.company_id = ? ORDER BY pinv.invoice_date DESC, pinv.id DESC",
            (company_id,),
        )
        return [PurchaseInvoice.from_row(r) for r in rows]

    def list_for_purchase_order(self, purchase_order_id: int) -> List[PurchaseInvoice]:
        """Every purchase invoice raised against this purchase order, newest
        first - via the link table, so this also finds an invoice that
        covers this PO as one of several rather than as its sole/primary
        one. Normally just one, but nothing stops a supplier's shipment
        against one PO arriving (and being invoiced) in more than one part."""
        rows = self.db.query(
            self._SELECT.format(items_total=self._ITEMS_TOTAL) +
            """ WHERE pinv.id IN (
                    SELECT purchase_invoice_id FROM purchase_invoice_purchase_order_links
                    WHERE purchase_order_id = ?
                ) ORDER BY pinv.id DESC""",
            (purchase_order_id,),
        )
        return [PurchaseInvoice.from_row(r) for r in rows]

    def invoiced_totals_for_purchase_order(self, purchase_order_id: int,
                                            exclude_purchase_invoice_id: Optional[int] = None) -> List[dict]:
        """Already-invoiced qty/boxes per product line for this PO, summed
        across every purchase invoice item tagged with it (an item's own
        purchase_order_id, not the invoice header's) - the outstanding-
        quantity counterpart of PurchaseOrderRepository.product_totals_for_proforma,
        one document further down the chain. `exclude_purchase_invoice_id`
        lets the edit form ask "outstanding if I ignore what I myself already
        cover" - otherwise re-opening an invoice that already fully covers
        its own PO(s) would make them vanish from its own "Start from" list."""
        sql = """SELECT product_id, product_name,
                        COALESCE(SUM(quantity_boxes), 0) AS boxes,
                        COALESCE(SUM(quantity_value), 0) AS quantity
                 FROM purchase_invoice_items
                 WHERE purchase_order_id = ?"""
        params = [purchase_order_id]
        if exclude_purchase_invoice_id:
            sql += " AND purchase_invoice_id != ?"
            params.append(exclude_purchase_invoice_id)
        sql += " GROUP BY product_id, product_name"
        rows = self.db.query(sql, tuple(params))
        return [dict(r) for r in rows]

    def list_for_lead(self, lead_id: int) -> List[PurchaseInvoice]:
        rows = self.db.query(
            self._SELECT.format(items_total=self._ITEMS_TOTAL) +
            " WHERE pinv.lead_id = ? ORDER BY pinv.invoice_date DESC, pinv.id DESC",
            (lead_id,),
        )
        return [PurchaseInvoice.from_row(r) for r in rows]

    def count_map_by_purchase_order(self, company_id: int) -> dict:
        """purchase_order_id -> how many purchase invoices point at it (via
        the link table, so a PO counted as one of several on an invoice
        still counts), so the PO list can show a count without an N+1 query."""
        rows = self.db.query(
            """SELECT l.purchase_order_id, COUNT(DISTINCT l.purchase_invoice_id) AS cnt
               FROM purchase_invoice_purchase_order_links l
               JOIN purchase_invoices pinv ON pinv.id = l.purchase_invoice_id
               WHERE pinv.company_id = ?
               GROUP BY l.purchase_order_id""",
            (company_id,),
        )
        return {row["purchase_order_id"]: row["cnt"] for row in rows}

    def create(self, purchase_invoice: PurchaseInvoice) -> PurchaseInvoice:
        new_id = self.db.execute(
            """INSERT INTO purchase_invoices
               (company_id, purchase_invoice_number, invoice_number, invoice_date, purchase_order_id, lead_id,
                seller_supplier_id, seller_name, seller_address, seller_pan, seller_gstin, seller_ref_no,
                port_of_loading, port_of_discharge, container_details, transporter_name, epcg_number, epcg_date,
                supplier_pdf_path, discount_amount, insurance_other, freight, igst_amount, cgst_amount,
                sgst_amount, round_off, purchase_type, remarks, currency_code, currency_symbol, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (purchase_invoice.company_id, purchase_invoice.purchase_invoice_number, purchase_invoice.invoice_number,
             purchase_invoice.invoice_date, purchase_invoice.purchase_order_id, purchase_invoice.lead_id,
             purchase_invoice.seller_supplier_id, purchase_invoice.seller_name, purchase_invoice.seller_address,
             purchase_invoice.seller_pan, purchase_invoice.seller_gstin, purchase_invoice.seller_ref_no,
             purchase_invoice.port_of_loading, purchase_invoice.port_of_discharge, purchase_invoice.container_details,
             purchase_invoice.transporter_name, purchase_invoice.epcg_number, purchase_invoice.epcg_date,
             purchase_invoice.supplier_pdf_path, purchase_invoice.discount_amount, purchase_invoice.insurance_other,
             purchase_invoice.freight, purchase_invoice.igst_amount, purchase_invoice.cgst_amount,
             purchase_invoice.sgst_amount, purchase_invoice.round_off, purchase_invoice.purchase_type,
             purchase_invoice.remarks, purchase_invoice.currency_code, purchase_invoice.currency_symbol,
             purchase_invoice.created_by),
        )
        self._replace_items(new_id, purchase_invoice.items)
        self._replace_vehicles(new_id, purchase_invoice.vehicle_numbers)
        self._replace_purchase_order_links(new_id, purchase_invoice.purchase_order_ids)
        return self.get_by_id(new_id)

    def update(self, purchase_invoice_id: int, purchase_invoice: PurchaseInvoice) -> None:
        self.db.execute(
            """UPDATE purchase_invoices SET invoice_number = ?, invoice_date = ?, purchase_order_id = ?,
                                             lead_id = ?, seller_supplier_id = ?, seller_name = ?,
                                             seller_address = ?, seller_pan = ?, seller_gstin = ?, seller_ref_no = ?,
                                             port_of_loading = ?, port_of_discharge = ?, container_details = ?,
                                             transporter_name = ?, epcg_number = ?, epcg_date = ?,
                                             supplier_pdf_path = ?, discount_amount = ?, insurance_other = ?,
                                             freight = ?, igst_amount = ?, cgst_amount = ?, sgst_amount = ?,
                                             round_off = ?, purchase_type = ?, remarks = ?, currency_code = ?,
                                             currency_symbol = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (purchase_invoice.invoice_number, purchase_invoice.invoice_date, purchase_invoice.purchase_order_id,
             purchase_invoice.lead_id, purchase_invoice.seller_supplier_id, purchase_invoice.seller_name,
             purchase_invoice.seller_address, purchase_invoice.seller_pan, purchase_invoice.seller_gstin,
             purchase_invoice.seller_ref_no, purchase_invoice.port_of_loading, purchase_invoice.port_of_discharge,
             purchase_invoice.container_details, purchase_invoice.transporter_name, purchase_invoice.epcg_number,
             purchase_invoice.epcg_date, purchase_invoice.supplier_pdf_path, purchase_invoice.discount_amount,
             purchase_invoice.insurance_other, purchase_invoice.freight, purchase_invoice.igst_amount,
             purchase_invoice.cgst_amount, purchase_invoice.sgst_amount, purchase_invoice.round_off,
             purchase_invoice.purchase_type, purchase_invoice.remarks, purchase_invoice.currency_code,
             purchase_invoice.currency_symbol, purchase_invoice_id),
        )
        self._replace_items(purchase_invoice_id, purchase_invoice.items)
        self._replace_vehicles(purchase_invoice_id, purchase_invoice.vehicle_numbers)
        self._replace_purchase_order_links(purchase_invoice_id, purchase_invoice.purchase_order_ids)

    def _replace_items(self, purchase_invoice_id: int, items: List[PurchaseInvoiceItem]) -> None:
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM purchase_invoice_items WHERE purchase_invoice_id = ?", (purchase_invoice_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO purchase_invoice_items
                       (purchase_invoice_id, sr_no, product_id, product_name, hsn_code,
                        quantity_boxes, quantity_value, unit, price_inr, price_per, total_inr, purchase_order_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (purchase_invoice_id, item.sr_no, item.product_id, item.product_name, item.hsn_code,
                     item.quantity_boxes, item.quantity_value, item.unit, item.price_inr,
                     item.price_per, item.total_inr, item.purchase_order_id),
                )

    def _replace_purchase_order_links(self, purchase_invoice_id: int, purchase_order_ids: List[int]) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "DELETE FROM purchase_invoice_purchase_order_links WHERE purchase_invoice_id = ?",
                (purchase_invoice_id,),
            )
            for purchase_order_id in purchase_order_ids:
                conn.execute(
                    "INSERT INTO purchase_invoice_purchase_order_links (purchase_invoice_id, purchase_order_id) "
                    "VALUES (?, ?)",
                    (purchase_invoice_id, purchase_order_id),
                )

    def _replace_vehicles(self, purchase_invoice_id: int, vehicle_numbers: List[str]) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "DELETE FROM purchase_invoice_vehicles WHERE purchase_invoice_id = ?", (purchase_invoice_id,)
            )
            for sr_no, vehicle_number in enumerate(vehicle_numbers, start=1):
                conn.execute(
                    "INSERT INTO purchase_invoice_vehicles (purchase_invoice_id, sr_no, vehicle_number) "
                    "VALUES (?, ?, ?)",
                    (purchase_invoice_id, sr_no, vehicle_number),
                )

    def delete(self, purchase_invoice_id: int) -> None:
        """Deleting a purchase invoice cascades to any packing list made
        directly from it, and removes its own uploaded supplier PDF."""
        with self.db.get_connection() as conn:
            _cascade_delete_purchase_invoice(conn, purchase_invoice_id)


class PackingListRepository:
    """Mirrors ProformaInvoiceRepository layer-for-layer: header + line
    items, day-scoped number sequence, reference-only lead link."""

    def __init__(self, db: Database):
        self.db = db

    def count_for_date_prefix(self, company_id: int, number_prefix: str) -> int:
        """Highest existing sequence suffix for this date prefix, not a row
        count - a plain COUNT(*) collides with an existing later number once
        an earlier same-day packing list has been deleted."""
        row = self.db.query_one(
            "SELECT MAX(CAST(SUBSTR(packing_list_number, ?) AS INTEGER)) AS max_seq "
            "FROM packing_lists WHERE company_id = ? AND packing_list_number LIKE ?",
            (len(number_prefix) + 1, company_id, f"{number_prefix}%"),
        )
        return row["max_seq"] if row and row["max_seq"] is not None else 0

    _SELECT = """
        SELECT pl.*, u.full_name AS created_by_name, pi.invoice_number AS proforma_invoice_number,
               q.quotation_number AS quotation_number, po.po_number AS purchase_order_number,
               pinv.purchase_invoice_number AS purchase_invoice_number,
               jw.job_work_number AS job_work_number
        FROM packing_lists pl
        JOIN users u ON u.id = pl.created_by
        LEFT JOIN proforma_invoices pi ON pi.id = pl.proforma_invoice_id
        LEFT JOIN quotations q ON q.id = pl.quotation_id
        LEFT JOIN purchase_orders po ON po.id = pl.purchase_order_id
        LEFT JOIN purchase_invoices pinv ON pinv.id = pl.purchase_invoice_id
        LEFT JOIN job_works jw ON jw.id = pl.job_work_id
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

    def list_all(
        self, company_id: int, doc_type: Optional[str] = None, client_name: Optional[str] = None
    ) -> List[PackingList]:
        query = self._SELECT + " WHERE pl.company_id = ?"
        params: list = [company_id]
        if doc_type == "proforma_invoice":
            query += " AND pl.proforma_invoice_id IS NOT NULL"
        elif doc_type == "quotation":
            query += " AND pl.quotation_id IS NOT NULL"
        elif doc_type == "purchase_order":
            query += " AND pl.purchase_order_id IS NOT NULL"
        elif doc_type == "purchase_invoice":
            query += " AND pl.purchase_invoice_id IS NOT NULL"
        elif doc_type == "job_work":
            query += " AND pl.job_work_id IS NOT NULL"
        if client_name:
            query += " AND pl.consignee_name = ?"
            params.append(client_name)
        query += " ORDER BY pl.packing_list_date DESC, pl.id DESC"
        rows = self.db.query(query, tuple(params))
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def list_distinct_consignees(self, company_id: int) -> List[str]:
        """Populates the client filter dropdown on the packing lists list
        page - consignee_name is denormalized onto each packing list, same
        pattern as buyer_name/consignee_name on quotations/proforma invoices."""
        rows = self.db.query(
            """SELECT DISTINCT consignee_name FROM packing_lists
               WHERE company_id = ? AND consignee_name IS NOT NULL AND consignee_name != ''
               ORDER BY consignee_name""",
            (company_id,),
        )
        return [r["consignee_name"] for r in rows]

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

    def list_for_purchase_order(self, purchase_order_id: int) -> List[PackingList]:
        """Every packing list generated from one purchase order (the PO's own
        PL) - drives the combined PO + packing details print view, same as
        list_for_proforma."""
        rows = self.db.query(
            self._SELECT + " WHERE pl.purchase_order_id = ? ORDER BY pl.id",
            (purchase_order_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def list_for_purchase_invoice(self, purchase_invoice_id: int) -> List[PackingList]:
        """Every packing list generated from one Purchase Invoice (that
        invoice's own PL) - same shape as list_for_purchase_order."""
        rows = self.db.query(
            self._SELECT + " WHERE pl.purchase_invoice_id = ? ORDER BY pl.id",
            (purchase_invoice_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    def list_for_job_work(self, job_work_id: int) -> List[PackingList]:
        """Every packing list generated from one Job Work (the job work's own
        PL) - same shape as list_for_purchase_order."""
        rows = self.db.query(
            self._SELECT + " WHERE pl.job_work_id = ? ORDER BY pl.id",
            (job_work_id,),
        )
        return self._attach_items([PackingList.from_row(r) for r in rows])

    # ---- design coverage (PI packing list vs. its purchase orders' packing lists) ----
    # Both queries below return the SAME row shape - one row per
    # (proforma invoice, product, design) with the boxes/quantity summed
    # across every matching packing list - so the service can subtract one
    # side from the other without reshaping anything. Grouping on the stored
    # *names* as well as the ids keeps hand-typed rows (no product_id/
    # design_id) visible instead of collapsing them all into one NULL group.
    _DESIGN_TOTALS_COLUMNS = """
        i.product_id, i.product_name, i.design_id, i.design_name,
        COALESCE(SUM(i.quantity_boxes), 0) AS boxes,
        COALESCE(SUM(i.quantity_value), 0) AS quantity,
        MIN(i.unit) AS unit
    """
    @staticmethod
    def _design_totals_group_by(key_alias: str) -> str:
        return f" GROUP BY {key_alias}, i.product_id, i.product_name, i.design_id, i.design_name"

    def design_totals_for_proforma(self, company_id: int, proforma_invoice_ids: List[int]) -> List[dict]:
        """What each proforma invoice's OWN packing list(s) say has to be
        made. A PL carrying a purchase_order_id is that PO's packing list,
        not the PI's, so it is excluded here even if it also happens to
        reference the invoice."""
        if not proforma_invoice_ids:
            return []
        placeholders = ",".join("?" for _ in proforma_invoice_ids)
        rows = self.db.query(
            f"""SELECT pl.proforma_invoice_id AS pi_id, {self._DESIGN_TOTALS_COLUMNS}
                FROM packing_lists pl
                JOIN packing_list_items i ON i.packing_list_id = pl.id
                WHERE pl.company_id = ? AND pl.purchase_order_id IS NULL
                  AND pl.proforma_invoice_id IN ({placeholders})
                {self._design_totals_group_by("pi_id")}""",
            (company_id, *proforma_invoice_ids),
        )
        return [dict(r) for r in rows]

    def design_totals_for_quotation(self, company_id: int, quotation_ids: List[int]) -> List[dict]:
        """Same shape as design_totals_for_proforma, but grouped by the
        packing list's quotation_id - the fallback source for a proforma
        invoice that was itself generated from a quotation which already has
        its own packing list (skipping the PI step), so the invoice never
        got a packing list directly against it. Mirrors the ancestor walk
        PackingListService._ancestor_packing_list already does for imports -
        see ProformaFulfilmentService.design_status_map for why the
        fulfilment side has to resolve the same ancestor, not just the
        direct one."""
        if not quotation_ids:
            return []
        placeholders = ",".join("?" for _ in quotation_ids)
        rows = self.db.query(
            f"""SELECT pl.quotation_id AS q_id, {self._DESIGN_TOTALS_COLUMNS}
                FROM packing_lists pl
                JOIN packing_list_items i ON i.packing_list_id = pl.id
                WHERE pl.company_id = ? AND pl.purchase_order_id IS NULL
                  AND pl.quotation_id IN ({placeholders})
                {self._design_totals_group_by("q_id")}""",
            (company_id, *quotation_ids),
        )
        return [dict(r) for r in rows]

    def design_totals_for_linked_purchase_orders(self, company_id: int,
                                                  proforma_invoice_ids: List[int]) -> List[dict]:
        """What has already been placed: the same totals taken across the
        packing lists of every purchase order linked to each invoice. Keyed
        by the PO's proforma_invoice_id, so a PO PL never needs a PI
        reference of its own."""
        if not proforma_invoice_ids:
            return []
        placeholders = ",".join("?" for _ in proforma_invoice_ids)
        rows = self.db.query(
            f"""SELECT po.proforma_invoice_id AS pi_id, {self._DESIGN_TOTALS_COLUMNS}
                FROM packing_lists pl
                JOIN purchase_orders po ON po.id = pl.purchase_order_id
                JOIN packing_list_items i ON i.packing_list_id = pl.id
                WHERE pl.company_id = ? AND po.proforma_invoice_id IN ({placeholders})
                {self._design_totals_group_by("pi_id")}""",
            (company_id, *proforma_invoice_ids),
        )
        return [dict(r) for r in rows]

    def design_totals_for_product(self, company_id: int, product_id: int,
                                  purchase_order_ids: Optional[List[int]] = None) -> List[dict]:
        """The designs actually received for one product, with their box/qty
        totals - the purchase-side reference shown beside each line on the
        Designs Packing List, so whoever allocates knows exactly which
        designs came in on this shipment and how many.

        `purchase_order_ids` scopes it to the purchase orders that actually
        fed this export invoice (see ExportInvoiceRepository.
        source_purchase_order_ids); without it the answer would be every
        design ever bought for that product across every order, which lists
        designs that were never on this shipment. Only falls back to
        company-wide when the caller can't resolve any source PO at all."""
        if not product_id:
            return []
        sql = f"""SELECT {self._DESIGN_TOTALS_COLUMNS}
                  FROM packing_lists pl
                  JOIN packing_list_items i ON i.packing_list_id = pl.id
                  WHERE pl.company_id = ? AND pl.purchase_order_id IS NOT NULL
                    AND i.product_id = ? AND i.design_id IS NOT NULL"""
        params: list = [company_id, product_id]
        if purchase_order_ids:
            sql += f" AND pl.purchase_order_id IN ({','.join('?' for _ in purchase_order_ids)})"
            params.extend(purchase_order_ids)
        sql += """ GROUP BY i.product_id, i.product_name, i.design_id, i.design_name
                   ORDER BY i.design_name"""
        return [dict(r) for r in self.db.query(sql, tuple(params))]

    # ---- inventory (designs bought = placed on a purchase order's packing list) ----
    # A packing list carrying a purchase_order_id is a PO's packing list: the
    # goods we've bought in. Summed per design, that's everything purchased;
    # sales (yet to be modelled) will subtract from the same totals later.
    def bought_totals_by_design(self, company_id: int) -> dict:
        """design_id -> {boxes, pcs, quantity} bought across every purchase
        order's packing list. Rows with no design_id (hand-typed lines) are
        skipped - stock is only tracked for real catalog designs."""
        rows = self.db.query(
            """SELECT i.design_id AS design_id,
                      COALESCE(SUM(i.quantity_boxes), 0) AS boxes,
                      COALESCE(SUM(i.pcs), 0) AS pcs,
                      COALESCE(SUM(i.quantity_value), 0) AS quantity,
                      MIN(p.alternate_quantity_unit) AS unit,
                      MIN(p.quantity_unit) AS qty_unit
               FROM packing_lists pl
               JOIN packing_list_items i ON i.packing_list_id = pl.id
               JOIN designs d ON d.id = i.design_id
               JOIN products p ON p.id = d.product_id
               WHERE pl.company_id = ? AND pl.purchase_order_id IS NOT NULL
                 AND i.design_id IS NOT NULL
               GROUP BY i.design_id""",
            (company_id,),
        )
        return {r["design_id"]: {"boxes": r["boxes"], "pcs": r["pcs"],
                                 "quantity": r["quantity"], "unit": r["unit"],
                                 "qty_unit": r["qty_unit"]}
                for r in rows}

    def bought_history_for_design(self, company_id: int, design_id: int) -> List[dict]:
        """One row per purchase-order packing list this design appears on -
        the design's purchase history (the buy side of Purchase/Sale
        history). Newest first."""
        rows = self.db.query(
            """SELECT po.po_number AS po_number, po.po_date AS po_date,
                      po.seller_name AS seller_name, po.id AS purchase_order_id,
                      pl.packing_list_number AS packing_list_number,
                      i.quantity_boxes AS boxes, i.pcs AS pcs,
                      i.quantity_value AS quantity, p.alternate_quantity_unit AS unit,
                      p.quantity_unit AS qty_unit
               FROM packing_lists pl
               JOIN purchase_orders po ON po.id = pl.purchase_order_id
               JOIN packing_list_items i ON i.packing_list_id = pl.id
               JOIN designs d ON d.id = i.design_id
               JOIN products p ON p.id = d.product_id
               WHERE pl.company_id = ? AND i.design_id = ?
               ORDER BY po.po_date DESC, po.id DESC""",
            (company_id, design_id),
        )
        return [dict(r) for r in rows]

    def create(self, packing_list: PackingList) -> PackingList:
        new_id = self.db.execute(
            """INSERT INTO packing_lists
               (company_id, packing_list_number, packing_list_date, proforma_invoice_id,
                quotation_id, purchase_order_id, purchase_invoice_id, job_work_id, export_ref_no, buyer_order_no,
                other_reference, consignee_name, consignee_address,
                notify_name, notify_address, country_of_origin, country_of_destination, vessel_flight,
                port_of_loading, port_of_discharge, final_destination, container_details,
                terms_of_delivery, remarks, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (packing_list.company_id, packing_list.packing_list_number, packing_list.packing_list_date,
             packing_list.proforma_invoice_id, packing_list.quotation_id,
             packing_list.purchase_order_id, packing_list.purchase_invoice_id, packing_list.job_work_id,
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
            """UPDATE packing_lists SET packing_list_date = ?, proforma_invoice_id = ?,
                                         quotation_id = ?, purchase_order_id = ?, purchase_invoice_id = ?,
                                         job_work_id = ?,
                                         export_ref_no = ?, buyer_order_no = ?, other_reference = ?,
                                         consignee_name = ?, consignee_address = ?, notify_name = ?,
                                         notify_address = ?, country_of_origin = ?, country_of_destination = ?,
                                         vessel_flight = ?, port_of_loading = ?, port_of_discharge = ?,
                                         final_destination = ?, container_details = ?, terms_of_delivery = ?,
                                         remarks = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (packing_list.packing_list_date, packing_list.proforma_invoice_id,
             packing_list.quotation_id, packing_list.purchase_order_id, packing_list.purchase_invoice_id,
             packing_list.job_work_id,
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
                        hsn_code, box_per_pallet, pallets, quantity_boxes, quantity_unit, pcs, quantity_value,
                        unit, net_weight_kg, gross_weight_kg)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (packing_list_id, item.sr_no, item.product_id, item.product_name, item.design_id,
                     item.design_name, item.hsn_code, item.box_per_pallet, item.pallets,
                     item.quantity_boxes, item.quantity_unit, item.pcs, item.quantity_value, item.unit,
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
