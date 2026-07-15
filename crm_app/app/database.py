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
import shutil
from contextlib import contextmanager
from datetime import datetime


# The current shape of the database, bumped by one every time the schema is
# restructured. Stamped onto every DB via `PRAGMA user_version` at the end of
# `init_schema`, so we can tell how old any given database file is - most
# importantly, a backup an admin uploads on the Database Backup page (see
# BackupService). The rule the restore flow relies on:
#   * a DB whose version is <= SCHEMA_VERSION can be forward-migrated to the
#     current shape simply by running `init_schema` on it (the guarded,
#     idempotent migrations in `_migrate` bring it up to date);
#   * a DB whose version is > SCHEMA_VERSION was written by a NEWER build of
#     the app - we can't safely downgrade it, so restore refuses it.
#
# HOW TO EVOLVE THE SCHEMA (keeps old backups integrable):
#   1. Change schema.sql to the new shape (for fresh installs).
#   2. Add a guarded, DATA-PRESERVING step to `_migrate` that transforms an
#      already-populated older DB into the new shape - ALTER TABLE to add a
#      column, or the rename-create-copy-drop dance for constraint changes,
#      following the `PRAGMA table_info`-guarded blocks already there. Never
#      DROP rows to "start fresh": that is what makes an old backup lossy.
#   3. Increment SCHEMA_VERSION below.
# Because `_migrate` is idempotent and runs on every startup AND on every
# restore, any backup - however old - is carried forward through the whole
# chain of steps, never discarded.
SCHEMA_VERSION = 1


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
        self._pre_schema_migrate()
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        with self.get_connection() as conn:
            conn.executescript(schema_sql)
        self._migrate(conn=None)
        # Record the shape this DB is now in. Runs on fresh installs, on
        # startup upgrades, and again when a restored backup is migrated
        # forward - so `user_version` always reflects the live schema.
        with self.get_connection() as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _pre_schema_migrate(self) -> None:
        """Drops old-format tables BEFORE schema.sql runs, so its
        CREATE TABLE IF NOT EXISTS statements recreate them in the new
        shape (an old-shape survivor would also crash the CREATE INDEX
        statements at the bottom of schema.sql).

        This is the "start fresh" product/folder/design restructure: the
        catalog used to be product_groups (nested folders) holding products
        as the leaves; it is now products (tax + HSN identity) ->
        product_folders -> designs. Old catalog data is NOT converted - the
        whole DB file is backed up to instance/backups/ first, then the old
        tables are dropped. Old-format tables are recognised by columns the
        new shapes don't have, so this runs exactly once. The same applies
        to an abandoned early packing_lists experiment some databases carry.
        """
        if not os.path.exists(self.db_path):
            return
        with self.get_connection() as conn:
            product_cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            packing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            legacy_products = bool(product_cols) and "group_id" in product_cols
            legacy_packing = bool(packing_cols) and "packing_list_number" not in packing_cols
            if not legacy_products and not legacy_packing:
                return
            self._backup_db_file("pre_product_redesign")
            conn.execute("PRAGMA foreign_keys = OFF")
            if legacy_products:
                # Line items keep their snapshot columns (name/hsn/price) but
                # their product_id points into the dropped catalog - null the
                # stale references out.
                conn.execute("UPDATE quotation_items SET product_id = NULL")
                conn.execute("UPDATE proforma_invoice_items SET product_id = NULL")
                conn.execute("DROP TABLE products")
                conn.execute("DROP TABLE IF EXISTS product_groups")
            if legacy_packing:
                conn.execute("DROP TABLE IF EXISTS packing_list_items")
                conn.execute("DROP TABLE packing_lists")
            conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self, conn=None) -> None:
        """Add columns to already-created tables that predate a schema change.
        `CREATE TABLE IF NOT EXISTS` can't retrofit columns onto an existing
        table, so new nullable columns are added here, guarded by a check
        against the live column list (ALTER TABLE has no IF NOT EXISTS).

        Every future restructure adds a step here and bumps `SCHEMA_VERSION`
        (see the module-level comment). Steps must be DATA-PRESERVING and
        idempotent: this method runs on every startup and every backup
        restore, so it is what forward-migrates an old uploaded backup instead
        of discarding its data."""
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

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_list_items)")}
            for column in ("box_per_pallet", "pcs"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE packing_list_items ADD COLUMN {column} REAL")

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

            # ---- MULTI-TENANCY ----
            # Everything below gives every pre-existing single-tenant install
            # a home ("Company #1" in `tenants`) and adds `company_id`
            # everywhere so multiple independent businesses can share one
            # install. Gated on `users` lacking `company_id`: a brand-new
            # install's schema.sql already includes it on every table, so
            # this whole block only ever runs once, only for databases that
            # predate multi-tenancy.
            #
            # `PRAGMA foreign_keys`/`legacy_alter_table` are no-ops inside an
            # active transaction, and the UPDATE statements in the migration
            # steps above (e.g. the lut nulling-out) already opened one
            # implicitly - commit first so the toggle below actually applies.
            conn.commit()
            users_existing = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if users_existing and "company_id" not in users_existing:
                conn.execute("PRAGMA foreign_keys = OFF")
                # Same reasoning as the `leads` rebuild above: without this,
                # SQLite silently rewrites every other table's REFERENCES
                # clause that points at a table we're about to rename (users,
                # quotations) to point at the "_old" name instead, which
                # breaks once that table is dropped. One bracket covers all
                # three rebuilds below.
                conn.execute("PRAGMA legacy_alter_table = ON")

                # `tenants` already exists (created by the executescript
                # above) but is empty on a legacy install - seed Company #1,
                # named after the existing Our Company profile if one was
                # ever filled in, so the upcoming backfills have a home.
                if not conn.execute("SELECT id FROM tenants WHERE id = 1").fetchone():
                    company_cols = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
                    company_row = conn.execute("SELECT company_name FROM our_company WHERE id = 1").fetchone() \
                        if company_cols else None
                    default_name = (company_row["company_name"] if company_row else None) or "Company 1"
                    conn.execute(
                        "INSERT INTO tenants (id, name, slug, is_active) VALUES (1, ?, 'company-1', 1)",
                        (default_name,),
                    )

                # leads / clients / product_groups / products: plain ADD
                # COLUMN + backfill, no constraint changes needed.
                for table in ("leads", "clients", "product_groups", "products"):
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if cols and "company_id" not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN company_id INTEGER REFERENCES tenants(id)")
                        conn.execute(f"UPDATE {table} SET company_id = 1 WHERE company_id IS NULL")

                # users: rebuild for the new UNIQUE(company_id, username).
                # Every existing `id` is preserved explicitly - leads.created_by,
                # communications.employee_id, clients.created_by and
                # quotations.created_by all reference these ids by number and
                # must not shift.
                conn.execute("ALTER TABLE users RENAME TO users_old")
                conn.execute("""
                    CREATE TABLE users (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id      INTEGER NOT NULL REFERENCES tenants(id),
                        username        TEXT NOT NULL,
                        password_hash   TEXT NOT NULL,
                        full_name       TEXT NOT NULL,
                        role            TEXT NOT NULL CHECK (role IN ('admin', 'employee')),
                        is_active       INTEGER NOT NULL DEFAULT 1,
                        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE (company_id, username)
                    )
                """)
                conn.execute("""
                    INSERT INTO users (id, company_id, username, password_hash, full_name, role, is_active, created_at)
                    SELECT id, 1, username, password_hash, full_name, role, is_active, created_at FROM users_old
                """)
                conn.execute("DROP TABLE users_old")

                # quotations: rebuild for UNIQUE(company_id, quotation_number).
                # By this point every earlier migration step in this function
                # (lead_id, sea_freight/insurance/certification/other_charges)
                # has already run, so `quotations_old` already has those
                # columns and the copy below carries them across.
                q_cols = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
                if q_cols and "company_id" not in q_cols:
                    conn.execute("ALTER TABLE quotations RENAME TO quotations_old")
                    conn.execute("""
                        CREATE TABLE quotations (
                            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id              INTEGER NOT NULL REFERENCES tenants(id),
                            quotation_number        TEXT NOT NULL,
                            quotation_date          TEXT NOT NULL,
                            lead_id                  INTEGER REFERENCES leads(id),
                            buyer_name              TEXT NOT NULL,
                            buyer_address           TEXT,
                            buyer_reference_no      TEXT,
                            port_of_loading         TEXT,
                            port_of_discharge       TEXT,
                            packing_details         TEXT,
                            container_details       TEXT,
                            shipping_mode           TEXT,
                            shipping_terms          TEXT,
                            payment_terms           TEXT,
                            price_validity_days     INTEGER NOT NULL DEFAULT 30,
                            remarks                 TEXT,
                            sea_freight              REAL NOT NULL DEFAULT 0,
                            insurance                REAL NOT NULL DEFAULT 0,
                            certification            REAL NOT NULL DEFAULT 0,
                            other_charges            REAL NOT NULL DEFAULT 0,
                            discount_amount         REAL NOT NULL DEFAULT 0,
                            bank_name               TEXT,
                            bank_account_number     TEXT,
                            bank_ifsc_code          TEXT,
                            bank_swift_code         TEXT,
                            bank_branch             TEXT,
                            bank_address            TEXT,
                            created_by              INTEGER NOT NULL REFERENCES users(id),
                            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            UNIQUE (company_id, quotation_number)
                        )
                    """)
                    conn.execute("""
                        INSERT INTO quotations (id, company_id, quotation_number, quotation_date, lead_id,
                            buyer_name, buyer_address, buyer_reference_no, port_of_loading, port_of_discharge,
                            packing_details, container_details, shipping_mode, shipping_terms, payment_terms,
                            price_validity_days, remarks, sea_freight, insurance, certification, other_charges,
                            discount_amount, bank_name, bank_account_number, bank_ifsc_code, bank_swift_code,
                            bank_branch, bank_address, created_by, created_at, updated_at)
                        SELECT id, 1, quotation_number, quotation_date, lead_id,
                            buyer_name, buyer_address, buyer_reference_no, port_of_loading, port_of_discharge,
                            packing_details, container_details, shipping_mode, shipping_terms, payment_terms,
                            price_validity_days, remarks, sea_freight, insurance, certification, other_charges,
                            discount_amount, bank_name, bank_account_number, bank_ifsc_code, bank_swift_code,
                            bank_branch, bank_address, created_by, created_at, updated_at
                        FROM quotations_old
                    """)
                    conn.execute("DROP TABLE quotations_old")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotations_created_by ON quotations(created_by)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotations_date ON quotations(quotation_date)")

                # our_company: drop the old `id = 1` singleton CHECK, key by
                # company_id instead (one row per tenant instead of one row
                # total). `id` is preserved so the child detail tables' new
                # `our_company_id` FK (backfilled below) stays valid.
                oc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
                if oc_cols and "company_id" not in oc_cols:
                    conn.execute("ALTER TABLE our_company RENAME TO our_company_old")
                    conn.execute("""
                        CREATE TABLE our_company (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id      INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
                            company_name    TEXT NOT NULL,
                            address         TEXT,
                            gstin           TEXT,
                            pan_no          TEXT,
                            iec             TEXT,
                            bin             TEXT,
                            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO our_company (id, company_id, company_name, address, gstin, pan_no, iec, bin, updated_at)
                        SELECT id, 1, company_name, address, gstin, pan_no, iec, bin, updated_at FROM our_company_old
                    """)
                    conn.execute("DROP TABLE our_company_old")

                # our_company_* child tables: plain ADD COLUMN + backfill,
                # pointing at whichever our_company.id belongs to company_id 1
                # (at most one existing row on a legacy install).
                for table in ("our_company_lut_details", "our_company_contact_details",
                              "our_company_contact_persons", "our_company_bank_details"):
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if cols and "our_company_id" not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN our_company_id INTEGER REFERENCES our_company(id)")
                        conn.execute(
                            f"UPDATE {table} SET our_company_id = "
                            f"(SELECT id FROM our_company WHERE company_id = 1) "
                            f"WHERE our_company_id IS NULL"
                        )

                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            # Company-scoped queries filter by company_id constantly - index
            # it on every root table now that the column is guaranteed to
            # exist (either from a fresh install's schema.sql, or from the
            # legacy-upgrade block above). Safe to run unconditionally.
            for table in ("users", "leads", "clients", "products", "product_folders", "designs", "quotations"):
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_company ON {table}(company_id)")

    def _backup_db_file(self, tag: str) -> None:
        """Copies the live DB file into instance/backups/ before a
        destructive migration, following the crm_<tag>_<timestamp>.db naming
        already used in that folder. Callers must commit any open
        transaction first so the copy is consistent."""
        if not os.path.exists(self.db_path):
            return
        backup_dir = os.path.join(os.path.dirname(self.db_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.db_path))[0]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(self.db_path, os.path.join(backup_dir, f"{stem}_{tag}_{stamp}.db"))

    def create_backup_copy(self, dest_path: str) -> None:
        """Write a CONSISTENT snapshot of the live DB to `dest_path` using
        SQLite's online backup API. Unlike a raw file copy, this is safe even
        if another request is mid-write, so it's what the Database Backup
        download uses to bundle the DB."""
        src = self._connect()
        try:
            dst = sqlite3.connect(dest_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

    def get_schema_version(self) -> int:
        """The live DB's `PRAGMA user_version` (0 for a DB that predates
        version stamping)."""
        return self.read_user_version(self.db_path)

    @staticmethod
    def read_user_version(db_path: str) -> int:
        """Read `PRAGMA user_version` from an arbitrary SQLite file - used to
        check how old an uploaded backup is before restoring it."""
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()

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
