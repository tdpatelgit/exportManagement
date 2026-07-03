"""
app/database.py
----------------
The ONLY module in the app that knows this is SQLite.

Single Responsibility: open connections, run the schema, expose a small
`execute` / `query` API. Repositories depend on this class, never on
`sqlite3` directly. That indirection is what lets us swap SQLite for
PostgreSQL/MySQL later (future plan: "store all data for each document
separately on a separate database") by rewriting this one file only -
every Repository, Service and Route stays untouched (Dependency Inversion).
"""

import sqlite3
import os
from contextlib import contextmanager


class Database:
    """Thin wrapper around sqlite3 connections.

    Usage:
        db = Database(path)
        db.init_schema(schema_path)
        with db.get_connection() as conn:
            conn.execute(...)
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        # Rows behave like dicts (row["column"]) - much friendlier for
        # templates/services than positional tuples.
        conn.row_factory = sqlite3.Row
        # Enforce FOREIGN KEY / CASCADE rules declared in schema.sql -
        # SQLite ignores them unless this pragma is turned on per-connection.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def get_connection(self):
        """Context manager that commits on success and rolls back on error,
        so callers never have to remember to do either."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self, schema_path: str) -> None:
        """Create every table defined in schema.sql if it doesn't exist yet.
        Safe to call on every app startup."""
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        with self.get_connection() as conn:
            conn.executescript(schema_sql)
        self._migrate(conn=None)

    def _migrate(self, conn=None) -> None:
        """Add columns to already-created tables that predate a schema change.
        `CREATE TABLE IF NOT EXISTS` can't retrofit columns onto an existing
        table, so new nullable columns are added here, guarded by a check
        against the live column list (ALTER TABLE has no IF NOT EXISTS)."""
        with self.get_connection() as conn:
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company_bank_details)")}
            for column in ("swift_code", "bank_address"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE our_company_bank_details ADD COLUMN {column} TEXT")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            for column in ("bin", "address"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE our_company ADD COLUMN {column} TEXT")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(clients)")}
            if existing and "address" not in existing:
                conn.execute("ALTER TABLE clients ADD COLUMN address TEXT")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if existing and "weight_class" not in existing:
                conn.execute("ALTER TABLE products ADD COLUMN weight_class TEXT")
            if existing and "price_usd" not in existing:
                conn.execute("ALTER TABLE products ADD COLUMN price_usd REAL")

            # The original `leads.status` CHECK constraint didn't allow
            # 'in_client', so converting a lead to a client crashed on the
            # final UPDATE (after the client row was already created) - a
            # CHECK constraint can't be altered in place, so the table has
            # to be rebuilt.
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='leads'"
            ).fetchone()
            if row and "in_client" not in row["sql"]:
                conn.execute("PRAGMA foreign_keys = OFF")
                # Without this, SQLite silently rewrites the REFERENCES
                # clauses in `clients.lead_id` and `lead_contacts.lead_id`
                # to point at `leads_old`, which breaks once that table is
                # dropped below.
                conn.execute("PRAGMA legacy_alter_table = ON")
                conn.execute("ALTER TABLE leads RENAME TO leads_old")
                conn.execute("""
                    CREATE TABLE leads (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_name        TEXT NOT NULL,
                        phone               TEXT NOT NULL,
                        email               TEXT NOT NULL,
                        facebook            TEXT,
                        instagram           TEXT,
                        other_social        TEXT,
                        status              TEXT NOT NULL DEFAULT 'new'
                                            CHECK (status IN (
                                                'new', 'in_communication', 'in_follow_up',
                                                'long_follow_up', 'quotation_submission_pending', 'in_client'
                                            )),
                        created_by          INTEGER NOT NULL REFERENCES users(id),
                        created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
                        is_converted         INTEGER NOT NULL DEFAULT 0,
                        converted_client_id  INTEGER REFERENCES clients(id)
                    )
                """)
                conn.execute("""
                    INSERT INTO leads (id, company_name, phone, email, facebook, instagram,
                                        other_social, status, created_by, created_at, updated_at,
                                        is_converted, converted_client_id)
                    SELECT id, company_name, phone, email, facebook, instagram,
                           other_social, status, created_by, created_at, updated_at,
                           is_converted, converted_client_id
                    FROM leads_old
                """)
                conn.execute("DROP TABLE leads_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_by ON leads(created_by)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
            if existing and "lead_id" not in existing:
                conn.execute("ALTER TABLE quotations ADD COLUMN lead_id INTEGER REFERENCES leads(id)")
            if existing:
                for column in ("sea_freight", "insurance", "certification", "other_charges"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE quotations ADD COLUMN {column} REAL NOT NULL DEFAULT 0")

            # `our_company.lut` used to hold a single LUT number; it's now a
            # list in `our_company_lut_details` (one row per financial year).
            # Carry over any existing value once, then null the old column
            # out so this seed never fires again even if every LUT row is
            # later deleted on purpose.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "lut" in existing:
                row = conn.execute("SELECT lut FROM our_company WHERE id = 1").fetchone()
                if row and row["lut"]:
                    conn.execute(
                        "INSERT INTO our_company_lut_details (lut_number, financial_year, is_primary) "
                        "VALUES (?, '', 1)",
                        (row["lut"],),
                    )
                conn.execute("UPDATE our_company SET lut = NULL WHERE id = 1")

    def query(self, sql: str, params: tuple = ()) -> list:
        """Run a SELECT and return a list of sqlite3.Row objects."""
        with self.get_connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()

    def query_one(self, sql: str, params: tuple = ()):
        """Run a SELECT expected to return 0 or 1 rows."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run an INSERT/UPDATE/DELETE. Returns the new row id for INSERTs
        (lastrowid), which repositories use to return the created object."""
        with self.get_connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid
