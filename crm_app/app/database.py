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

import re
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
SCHEMA_VERSION = 18  # v18: packing_lists.purchase_invoice_id (new nullable FK) - a Purchase Invoice can now carry its own packing list, imported wholesale from its linked purchase order's own PL. v17: purchase_invoices/purchase_invoice_items/purchase_invoice_vehicles (new tables) - the document raised once a supplier's goods against one of our purchase orders actually arrive, carrying the supplier's own invoice number/date, transporter/vehicle details, optional EPCG number/date, an uploaded copy of the supplier's own invoice PDF (nothing is generated/printed for this document type), and typed-in discount/insurance/freight/tax/round-off figures matching what the supplier actually charged. v16: proforma_invoices.status ('draft' | 'confirmed') - confirming a PI locks it for editing (an admin can move it back to draft) and starts the "still to be ordered" reminder that runs until every design on the PI's packing list has been placed on the packing list of some purchase order linked to that PI. v15: purchase_orders.purchase_type ('full_tax' | 'exemption') - a PO's GST percentages are no longer typed in by hand, they follow from this choice plus the GSTIN state-code comparison between our company and the seller. v14: our_company_rcmc_details (new table) - repeatable RCMC (Registration-cum-Membership Certificate) records per company, same shape/pattern as our_company_lut_details. v13: the single `clients` table (Buyer/Supplier/Exporter via client_type) is split into three separate entities - buyers/exporters (same shape as before, minus client_type), and suppliers (an our_company-shaped profile: GSTIN/PAN/IEC/bank/contacts, no logo/BIN/LUT). party_contacts replaces client_contacts for buyer/exporter; payment_history/documents/communications gain a parent_type discriminator so one type's ids can't collide with another's; purchase_orders.seller_client_id becomes seller_supplier_id; leads gains converted_client_type alongside converted_client_id. v12: purchase orders (new purchase_orders/purchase_order_items tables via schema.sql, plus packing_lists.purchase_order_id so a PO can carry its own packing list) and our_company.logo_path (company logo shown in the app and on generated documents). v11: each product quantity gets its own unit - quantity_unit (new, 'PCS' for existing rows) and alternate_quantity_unit (renamed from `unit`)


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

            # v6: optional surface finish on designs (GLOSSY / MATT / ...)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(designs)")}
            if existing and "surface" not in existing:
                conn.execute("ALTER TABLE designs ADD COLUMN surface TEXT")

            # v7: proforma goods layout choice + per-line surface finish
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoices)")}
            if existing and "display_mode" not in existing:
                conn.execute("ALTER TABLE proforma_invoices ADD COLUMN display_mode TEXT NOT NULL DEFAULT 'index'")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoice_items)")}
            if existing and "surface" not in existing:
                conn.execute("ALTER TABLE proforma_invoice_items ADD COLUMN surface TEXT")

            # v9: product net/gross weight per box (KG) - drives the packing
            # list's Boxes x weight auto-calc, same pattern as
            # alternate_quantity driving the Qty auto-calc.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            for column in ("net_weight_kg", "gross_weight_kg"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE products ADD COLUMN {column} REAL")

            # v9: a packing list can now be generated directly from a
            # Quotation (skipping the proforma invoice step) - same
            # "generated from" reference pattern as proforma_invoice_id.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "quotation_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN quotation_id INTEGER REFERENCES quotations(id)")

            # ---- PACKING SPEC MOVES FROM DESIGN TO PRODUCT ----
            # packing / quantity / alternate_quantity / unit / weight_class
            # describe a PRODUCT's physical packing (its box config, unit of
            # measure) - they don't vary by design/finish, so they move up
            # from `designs` to `products` once. Live data is forward-
            # migrated, not discarded: each product backfills these fields
            # from whichever of its designs happens to carry them (first by
            # id), then the columns are dropped from `designs`. Recognised
            # by `packing` still being a column on `designs` - true whether
            # or not that design table ever got `unit` added in an earlier
            # run, so the backfill below tolerates either case.
            designs_existing = {r["name"] for r in conn.execute("PRAGMA table_info(designs)")}
            if designs_existing and "packing" in designs_existing:
                has_unit = "unit" in designs_existing

                products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
                if "packing" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN packing TEXT")
                if "quantity" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN quantity TEXT")
                if "alternate_quantity" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN alternate_quantity TEXT")
                if "unit" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN unit TEXT NOT NULL DEFAULT 'SQM'")
                if "weight_class" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN weight_class TEXT")

                # Backfill: one representative design's value per product
                # (first by id that has a non-null value), only where the
                # product doesn't already have a value of its own.
                for field in ("packing", "quantity", "alternate_quantity", "weight_class"):
                    conn.execute(f"""
                        UPDATE products SET {field} = (
                            SELECT d.{field} FROM designs d
                            WHERE d.product_id = products.id AND d.{field} IS NOT NULL
                            ORDER BY d.id LIMIT 1
                        )
                        WHERE {field} IS NULL
                    """)
                if has_unit:
                    conn.execute("""
                        UPDATE products SET unit = (
                            SELECT d.unit FROM designs d
                            WHERE d.product_id = products.id
                            ORDER BY d.id LIMIT 1
                        )
                        WHERE EXISTS (SELECT 1 FROM designs d WHERE d.product_id = products.id)
                    """)

                # Rebuild `designs` without the five columns that just moved
                # up - other tables' FKs to designs(id) (packing_list_items)
                # must not get silently rewritten to the "_old" table, hence
                # the same foreign_keys/legacy_alter_table dance used
                # elsewhere in this file.
                conn.commit()
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("PRAGMA legacy_alter_table = ON")
                conn.execute("ALTER TABLE designs RENAME TO designs_old")
                conn.execute("""
                    CREATE TABLE designs (
                        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id              INTEGER NOT NULL REFERENCES tenants(id),
                        product_id              INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                        folder_id               INTEGER REFERENCES product_folders(id) ON DELETE CASCADE,
                        design_name             TEXT NOT NULL,
                        description             TEXT,
                        price_usd               REAL,
                        photo_path              TEXT,
                        dimension_photo_path    TEXT,
                        alt_text                TEXT,
                        created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.execute("""
                    INSERT INTO designs (id, company_id, product_id, folder_id, design_name, description,
                                          price_usd, photo_path, dimension_photo_path, alt_text, created_at, updated_at)
                    SELECT id, company_id, product_id, folder_id, design_name, description,
                           price_usd, photo_path, dimension_photo_path, alt_text, created_at, updated_at
                    FROM designs_old
                """)
                conn.execute("DROP TABLE designs_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_designs_product ON designs(product_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_designs_folder ON designs(folder_id)")
                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            # ---- v4: CATEGORY LEVEL + GST COLUMN RETIRED ----
            # The catalog is now category -> product -> sub category ->
            # design. Categories behave like folders at the catalog root:
            # products carry a nullable category_id (NULL = catalog root).
            # At the same time the product's standalone gst_percent input is
            # retired: IGST is the only tax input, and SGST/CGST are always
            # stored as half of it. Existing rows get their SGST/CGST
            # recalculated from IGST once, then the gst_percent column is
            # dropped (which is also the guard that makes this one-shot).
            products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if products_existing and "category_id" not in products_existing:
                conn.execute(
                    "ALTER TABLE products ADD COLUMN category_id INTEGER "
                    "REFERENCES categories(id) ON DELETE CASCADE"
                )
            if products_existing and "gst_percent" in products_existing:
                conn.execute("""
                    UPDATE products
                    SET sgst_percent = ROUND(igst_percent / 2.0, 2),
                        cgst_percent = ROUND(igst_percent / 2.0, 2)
                """)
                conn.execute("ALTER TABLE products DROP COLUMN gst_percent")
            # Lives here instead of schema.sql: on a pre-v4 DB the column
            # doesn't exist yet when schema.sql runs.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)")

            # ---- v5: CATEGORIES CAN NEST ----
            # Categories now behave exactly like sub categories (product_folders):
            # a self-referencing, nullable parent_id lets one category sit
            # inside another to any depth. A plain ADD COLUMN is enough - no
            # existing category has a parent to backfill.
            categories_existing = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
            if categories_existing and "parent_id" not in categories_existing:
                conn.execute(
                    "ALTER TABLE categories ADD COLUMN parent_id INTEGER "
                    "REFERENCES categories(id) ON DELETE CASCADE"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)")

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
            for table in ("users", "leads", "categories", "products", "product_folders", "designs", "quotations"):
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_company ON {table}(company_id)")

            # ---- v10: PALLET PACKING BECOMES A LIST OF PALLET TYPES ----
            # products.packing used to hold one boxes-per-pallet figure; a
            # product now carries any number of NAMED pallet storage options
            # in product_pallet_types (plus an implicit, unstored "loose"
            # option = no pallets). Each existing packing value becomes one
            # pallet type named 'pallet' (its leading number as the
            # boxes-per-pallet count), then the column is dropped - which is
            # also the guard that makes this one-shot. Runs after the
            # multi-tenancy block so products.company_id is guaranteed to
            # exist even on legacy databases.
            products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if products_existing and "packing" in products_existing:
                rows = conn.execute(
                    "SELECT id, company_id, packing FROM products "
                    "WHERE packing IS NOT NULL AND TRIM(packing) != ''"
                ).fetchall()
                for row in rows:
                    m = re.match(r"\s*([\d.]+)", str(row["packing"]))
                    try:
                        boxes = float(m.group(1)) if m else 0.0
                    except ValueError:
                        boxes = 0.0
                    if boxes > 0:
                        conn.execute(
                            "INSERT INTO product_pallet_types (company_id, product_id, name, boxes_per_pallet) "
                            "VALUES (?, ?, 'pallet', ?)",
                            (row["company_id"], row["id"], boxes),
                        )
                conn.execute("ALTER TABLE products DROP COLUMN packing")

            # ---- v11: EACH PRODUCT QUANTITY GETS ITS OWN UNIT ----
            # The product spec is now (quantity unit, quantity) +
            # (alt quantity unit, alt quantity) + pallet types. `unit` only
            # ever described the alternate quantity (it prefills the Unit
            # column on document lines), so it's renamed to
            # alternate_quantity_unit - the rename is also the one-shot
            # guard. quantity was always a pcs-per-box figure, so existing
            # rows get quantity_unit = 'PCS'.
            products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if products_existing and "unit" in products_existing:
                conn.execute("ALTER TABLE products RENAME COLUMN unit TO alternate_quantity_unit")
            if products_existing and "quantity_unit" not in products_existing:
                conn.execute("ALTER TABLE products ADD COLUMN quantity_unit TEXT NOT NULL DEFAULT 'PCS'")

            # ---- v12: PURCHASE ORDERS + COMPANY LOGO ----
            # The purchase_orders/purchase_order_items tables themselves are
            # created by schema.sql (CREATE TABLE IF NOT EXISTS covers old
            # databases too); only the columns retrofitted onto existing
            # tables need guarded ALTERs here: a packing list can now be
            # generated from a purchase order (same "generated from"
            # reference pattern as proforma_invoice_id/quotation_id), and
            # Our Company gains an optional logo image.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "purchase_order_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN purchase_order_id INTEGER REFERENCES purchase_orders(id)")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "logo_path" not in existing:
                conn.execute("ALTER TABLE our_company ADD COLUMN logo_path TEXT")

            # ---- v13: BUYERS / SUPPLIERS / EXPORTERS REPLACE `clients` ----
            # Buyer, Supplier and Exporter become separate entities instead
            # of one `clients` table with a client_type discriminator.
            # Buyers/exporters keep the old shape verbatim (their ids are
            # preserved so every other table's reference to the old
            # clients.id keeps resolving with no remap); suppliers get an
            # our_company-shaped profile instead (GSTIN/PAN/IEC/bank/
            # contacts - no logo/BIN/LUT). Guarded on `clients` still
            # existing, so this runs exactly once per database.
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
            ).fetchone():
                conn.commit()
                self._backup_db_file("pre_client_split")

                # 1. Split every client row into its new home table.
                conn.execute("""
                    INSERT INTO buyers (id, company_id, lead_id, company_name, phone, email,
                                         facebook, instagram, other_social, address, status,
                                         created_by, created_at, updated_at)
                    SELECT id, company_id, lead_id, company_name, phone, email,
                           facebook, instagram, other_social, address, status,
                           created_by, created_at, updated_at
                    FROM clients WHERE client_type = 'Buyer'
                """)
                conn.execute("""
                    INSERT INTO exporters (id, company_id, lead_id, company_name, phone, email,
                                            facebook, instagram, other_social, address, status,
                                            created_by, created_at, updated_at)
                    SELECT id, company_id, lead_id, company_name, phone, email,
                           facebook, instagram, other_social, address, status,
                           created_by, created_at, updated_at
                    FROM clients WHERE client_type = 'Exporter'
                """)
                conn.execute("""
                    INSERT INTO suppliers (id, company_id, lead_id, company_name, address, status,
                                            created_by, created_at, updated_at)
                    SELECT id, company_id, lead_id, company_name, address, status,
                           created_by, created_at, updated_at
                    FROM clients WHERE client_type = 'Supplier'
                """)
                # A migrated supplier's phone/email is all it had - seed it
                # into supplier_contact_details, the same shape
                # our_company's own contact details already use.
                for row in conn.execute(
                    "SELECT id, phone, email FROM clients WHERE client_type = 'Supplier'"
                ).fetchall():
                    if row["phone"]:
                        conn.execute(
                            "INSERT INTO supplier_contact_details (supplier_id, type, value, is_primary) "
                            "VALUES (?, 'phone', ?, 1)",
                            (row["id"], row["phone"]),
                        )
                    if row["email"]:
                        conn.execute(
                            "INSERT INTO supplier_contact_details (supplier_id, type, value, is_primary) "
                            "VALUES (?, 'email', ?, 1)",
                            (row["id"], row["email"]),
                        )

                # 2. client_contacts -> party_contacts (buyer/exporter) or
                #    supplier_contact_persons (supplier - name only; phone/
                #    email don't fit that table's shape, same as
                #    our_company's own contact persons never carrying one).
                conn.execute("""
                    INSERT INTO party_contacts (parent_type, parent_id, name, phone, email, is_primary)
                    SELECT 'buyer', cc.client_id, cc.name, cc.phone, cc.email, cc.is_primary
                    FROM client_contacts cc JOIN clients c ON c.id = cc.client_id
                    WHERE c.client_type = 'Buyer'
                """)
                conn.execute("""
                    INSERT INTO party_contacts (parent_type, parent_id, name, phone, email, is_primary)
                    SELECT 'exporter', cc.client_id, cc.name, cc.phone, cc.email, cc.is_primary
                    FROM client_contacts cc JOIN clients c ON c.id = cc.client_id
                    WHERE c.client_type = 'Exporter'
                """)
                conn.execute("""
                    INSERT INTO supplier_contact_persons (supplier_id, name, is_primary)
                    SELECT cc.client_id, cc.name, cc.is_primary
                    FROM client_contacts cc JOIN clients c ON c.id = cc.client_id
                    WHERE c.client_type = 'Supplier'
                """)

                conn.commit()
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("PRAGMA legacy_alter_table = ON")

                # 3. communications: widen the parent_type CHECK from
                #    ('lead', 'client') to ('lead', 'buyer', 'supplier',
                #    'exporter') - a plain UPDATE can't do this alone since
                #    the OLD constraint would reject 'buyer'/'supplier'/
                #    'exporter' values, so this needs the same rebuild dance
                #    as payment_history/documents below. Guarded on the live
                #    table's own CHECK constraint text (not just column
                #    presence, which existed under the old shape too) so a
                #    database that already has the new shape - e.g. a retry
                #    after this step previously got interrupted - doesn't
                #    redo it; a stray `_old` table from such an interrupted
                #    attempt is dropped first so the rename below can't
                #    collide with it.
                comm_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='communications'"
                ).fetchone()
                if comm_row and "'buyer'" not in comm_row["sql"]:
                    conn.execute("DROP TABLE IF EXISTS communications_old")
                    conn.execute("ALTER TABLE communications RENAME TO communications_old")
                    conn.execute("""
                        CREATE TABLE communications (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            parent_type     TEXT NOT NULL CHECK (parent_type IN ('lead', 'buyer', 'supplier', 'exporter')),
                            parent_id       INTEGER NOT NULL,
                            employee_id     INTEGER NOT NULL REFERENCES users(id),
                            comm_date       TEXT NOT NULL,
                            mode            TEXT NOT NULL,
                            description     TEXT NOT NULL,
                            follow_up_date  TEXT,
                            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO communications (id, parent_type, parent_id, employee_id, comm_date,
                                                     mode, description, follow_up_date, created_at)
                        SELECT co.id,
                               CASE WHEN co.parent_type = 'lead' THEN 'lead' ELSE LOWER(c.client_type) END,
                               co.parent_id, co.employee_id, co.comm_date, co.mode, co.description,
                               co.follow_up_date, co.created_at
                        FROM communications_old co
                        LEFT JOIN clients c ON co.parent_type = 'client' AND c.id = co.parent_id
                        WHERE co.parent_type = 'lead' OR c.id IS NOT NULL
                    """)
                    conn.execute("DROP TABLE communications_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comms_parent ON communications(parent_type, parent_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comms_employee ON communications(employee_id)")

                # 4. payment_history / documents: add parent_type, rename
                #    client_id -> parent_id. Needs a full rebuild (adding a
                #    CHECK constraint can't be done with a plain ALTER),
                #    same rename-create-copy-drop dance used elsewhere here.
                #    Guarded on the old `client_id` column still being
                #    present (the new shape drops it entirely, unlike
                #    communications above where the column name doesn't
                #    change) - same "don't redo it, don't collide with a
                #    stray `_old` from an interrupted attempt" reasoning.
                ph_cols = {r["name"] for r in conn.execute("PRAGMA table_info(payment_history)")}
                if "client_id" in ph_cols:
                    conn.execute("DROP TABLE IF EXISTS payment_history_old")
                    conn.execute("ALTER TABLE payment_history RENAME TO payment_history_old")
                    conn.execute("""
                        CREATE TABLE payment_history (
                            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                            parent_type         TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier', 'exporter')),
                            parent_id           INTEGER NOT NULL,
                            account_name        TEXT NOT NULL,
                            payment_datetime    TEXT NOT NULL,
                            amount_original     REAL NOT NULL,
                            currency_code       TEXT NOT NULL,
                            conversion_rate     REAL NOT NULL,
                            amount_inr          REAL NOT NULL,
                            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO payment_history (id, parent_type, parent_id, account_name, payment_datetime,
                                                      amount_original, currency_code, conversion_rate, amount_inr, created_at)
                        SELECT ph.id, LOWER(c.client_type), ph.client_id, ph.account_name, ph.payment_datetime,
                               ph.amount_original, ph.currency_code, ph.conversion_rate, ph.amount_inr, ph.created_at
                        FROM payment_history_old ph JOIN clients c ON c.id = ph.client_id
                    """)
                    conn.execute("DROP TABLE payment_history_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_parent ON payment_history(parent_type, parent_id)")

                doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
                if "client_id" in doc_cols:
                    conn.execute("DROP TABLE IF EXISTS documents_old")
                    conn.execute("ALTER TABLE documents RENAME TO documents_old")
                    conn.execute("""
                        CREATE TABLE documents (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            parent_type     TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier', 'exporter')),
                            parent_id       INTEGER NOT NULL,
                            document_name   TEXT NOT NULL,
                            document_type   TEXT NOT NULL,
                            document_date   TEXT NOT NULL,
                            notes           TEXT,
                            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO documents (id, parent_type, parent_id, document_name, document_type,
                                                document_date, notes, created_at)
                        SELECT d.id, LOWER(c.client_type), d.client_id, d.document_name, d.document_type,
                               d.document_date, d.notes, d.created_at
                        FROM documents_old d JOIN clients c ON c.id = d.client_id
                    """)
                    conn.execute("DROP TABLE documents_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_type, parent_id)")

                # 5. leads: converted_client_id needs a converted_client_type
                #    alongside it now that there are three possible target
                #    tables instead of one. Guarded on that column's absence
                #    (same "don't redo it, don't collide with a stray `_old`"
                #    reasoning as the rebuilds above).
                leads_cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
                if "converted_client_type" not in leads_cols:
                    conn.execute("DROP TABLE IF EXISTS leads_old")
                    conn.execute("ALTER TABLE leads RENAME TO leads_old")
                    conn.execute("""
                        CREATE TABLE leads (
                            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id              INTEGER NOT NULL REFERENCES tenants(id),
                            company_name            TEXT NOT NULL,
                            phone                   TEXT NOT NULL,
                            email                   TEXT NOT NULL,
                            facebook                TEXT,
                            instagram               TEXT,
                            other_social            TEXT,
                            status                  TEXT NOT NULL DEFAULT 'new'
                                                    CHECK (status IN (
                                                        'new', 'in_communication', 'in_follow_up',
                                                        'long_follow_up', 'quotation_submission_pending', 'in_client'
                                                    )),
                            created_by              INTEGER NOT NULL REFERENCES users(id),
                            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            is_converted            INTEGER NOT NULL DEFAULT 0,
                            converted_client_type   TEXT CHECK (converted_client_type IN ('Buyer', 'Supplier', 'Exporter')),
                            converted_client_id     INTEGER
                        )
                    """)
                    conn.execute("""
                        INSERT INTO leads (id, company_id, company_name, phone, email, facebook, instagram,
                                            other_social, status, created_by, created_at, updated_at,
                                            is_converted, converted_client_type, converted_client_id)
                        SELECT lo.id, lo.company_id, lo.company_name, lo.phone, lo.email, lo.facebook, lo.instagram,
                               lo.other_social, lo.status, lo.created_by, lo.created_at, lo.updated_at,
                               lo.is_converted, c.client_type, lo.converted_client_id
                        FROM leads_old lo LEFT JOIN clients c ON c.id = lo.converted_client_id
                    """)
                    conn.execute("DROP TABLE leads_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_by ON leads(created_by)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)")

                # 6. purchase_orders.seller_client_id -> seller_supplier_id.
                #    A PO's seller was always meant to be a Supplier - any
                #    legacy row pointing at a non-Supplier client is stale
                #    test data, so it's nulled out rather than carried into
                #    the wrong table.
                po_cols = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_orders)")}
                if "seller_client_id" in po_cols:
                    conn.execute("DROP TABLE IF EXISTS purchase_orders_old")
                    conn.execute("ALTER TABLE purchase_orders RENAME TO purchase_orders_old")
                    conn.execute("""
                        CREATE TABLE purchase_orders (
                            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id              INTEGER NOT NULL REFERENCES tenants(id),
                            po_number               TEXT NOT NULL,
                            po_date                 TEXT NOT NULL,
                            lead_id                 INTEGER REFERENCES leads(id),
                            proforma_invoice_id     INTEGER REFERENCES proforma_invoices(id),
                            seller_supplier_id      INTEGER REFERENCES suppliers(id),
                            seller_name             TEXT NOT NULL,
                            seller_address          TEXT,
                            seller_pan              TEXT,
                            seller_gstin            TEXT,
                            seller_ref_no           TEXT,
                            port_of_loading         TEXT,
                            port_of_discharge       TEXT,
                            container_details       TEXT,
                            delivery_time           TEXT,
                            advance_percent         TEXT,
                            payment_terms           TEXT,
                            remarks                 TEXT,
                            igst_percent            REAL NOT NULL DEFAULT 0,
                            cgst_percent            REAL NOT NULL DEFAULT 0,
                            sgst_percent            REAL NOT NULL DEFAULT 0,
                            created_by              INTEGER NOT NULL REFERENCES users(id),
                            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            UNIQUE (company_id, po_number)
                        )
                    """)
                    conn.execute("""
                        INSERT INTO purchase_orders (id, company_id, po_number, po_date, lead_id,
                            proforma_invoice_id, seller_supplier_id, seller_name, seller_address, seller_pan,
                            seller_gstin, seller_ref_no, port_of_loading, port_of_discharge, container_details,
                            delivery_time, advance_percent, payment_terms, remarks,
                            igst_percent, cgst_percent, sgst_percent, created_by, created_at, updated_at)
                        SELECT po.id, po.company_id, po.po_number, po.po_date, po.lead_id,
                            po.proforma_invoice_id,
                            CASE WHEN po.seller_client_id IN (SELECT id FROM suppliers) THEN po.seller_client_id ELSE NULL END,
                            po.seller_name, po.seller_address, po.seller_pan,
                            po.seller_gstin, po.seller_ref_no, po.port_of_loading, po.port_of_discharge, po.container_details,
                            po.delivery_time, po.advance_percent, po.payment_terms, po.remarks,
                            po.igst_percent, po.cgst_percent, po.sgst_percent, po.created_by, po.created_at, po.updated_at
                        FROM purchase_orders_old po
                    """)
                    conn.execute("DROP TABLE purchase_orders_old")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_company ON purchase_orders(company_id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_created_by ON purchase_orders(created_by)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_date ON purchase_orders(po_date)")

                # Safety net: an earlier interrupted attempt (e.g. the
                # process killed mid-migration) can leave a stray `_old`
                # table behind that this run's per-step guards above didn't
                # touch, because the live table already had the new shape
                # (so that step decided there was nothing to redo). Drop it
                # now if it's empty; if it still holds rows, a previous
                # attempt's copy never finished, and silently discarding
                # that data is worse than a loud failure - surface it
                # instead of guessing.
                for stray in ("communications_old", "payment_history_old", "documents_old",
                              "leads_old", "purchase_orders_old"):
                    if not conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (stray,)
                    ).fetchone():
                        continue
                    count = conn.execute(f"SELECT COUNT(*) AS c FROM {stray}").fetchone()["c"]
                    if count == 0:
                        conn.execute(f"DROP TABLE {stray}")
                    else:
                        raise RuntimeError(
                            f"Migration safety check: leftover table '{stray}' from an earlier "
                            f"interrupted migration attempt still holds {count} row(s) that were "
                            f"never copied into its replacement - refusing to drop it silently. "
                            f"Inspect it manually before removing it."
                        )

                # 7. clients / client_contacts are now fully migrated away.
                conn.execute("DROP TABLE IF EXISTS client_contacts")
                conn.execute("DROP TABLE IF EXISTS clients")

                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            # Unconditional (unlike the block above, which only fires once
            # per legacy DB): a fresh install's schema.sql already creates
            # payment_history/documents with parent_type, so these indexes
            # need to exist either way.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_parent ON payment_history(parent_type, parent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_type, parent_id)")

            # v15: a purchase order is now placed under a purchase type
            # ('full_tax' | 'exemption') which derives its GST percentages,
            # instead of the percentages being typed in. Existing POs keep
            # the percentages already stored on them and are treated as
            # full-tax purchases - re-saving one recomputes them.
            # (Must stay AFTER the v13 block above, which rebuilds
            # purchase_orders from scratch in its pre-v15 shape.)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_orders)")}
            if existing and "purchase_type" not in existing:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN purchase_type TEXT NOT NULL DEFAULT 'full_tax'")

            # v16: a proforma invoice is now either a draft or confirmed.
            # Existing invoices stay drafts (freely editable, no reminder) -
            # confirming one is always an explicit action on the PI page.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoices)")}
            if existing and "status" not in existing:
                conn.execute("ALTER TABLE proforma_invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")

            # v18: a packing list can now also be generated from a Purchase
            # Invoice (that invoice's own PL, importing its linked PO's PL
            # wholesale) - a plain nullable FK, same "generated from"
            # reference-only pattern as purchase_order_id above it. The
            # index can't live in schema.sql's unconditional block (an old
            # DB's packing_lists table won't have the column yet when that
            # block runs), so it's created here too.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "purchase_invoice_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN purchase_invoice_id INTEGER REFERENCES purchase_invoices(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_packing_lists_purchase_invoice ON packing_lists(purchase_invoice_id)")

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
