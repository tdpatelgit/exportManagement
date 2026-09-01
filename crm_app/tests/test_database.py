"""
Tests for app/database.py

The Database class is the only module that talks to sqlite3 directly, so its
contract (query / query_one / execute, schema init + version stamping, backup
copy, foreign-key enforcement, rollback-on-error) is worth pinning tightly.
"""

import os
import sqlite3

import pytest

from app.database import Database, SCHEMA_VERSION


class TestSchemaInit:
    def test_creates_db_file_and_stamps_version(self, db, tmp_config):
        assert os.path.exists(tmp_config.DATABASE_PATH)
        assert db.get_schema_version() == SCHEMA_VERSION

    def test_init_schema_is_idempotent(self, db, tmp_config):
        # Running it again on an existing DB must not raise or wipe anything.
        db.execute("INSERT INTO tenants (name, slug) VALUES ('Keep', 'keep')")
        db.init_schema(tmp_config.SCHEMA_PATH)
        row = db.query_one("SELECT name FROM tenants WHERE slug = 'keep'")
        assert row["name"] == "Keep"

    def test_read_user_version_static(self, db, tmp_config):
        assert Database.read_user_version(tmp_config.DATABASE_PATH) == SCHEMA_VERSION

    def test_migrate_reshapes_first_cut_v95_production_tables(self, db, tmp_config):
        """The first cut of v95 tracked production per PO LINE: both tables
        shipped without their design columns, and with a UNIQUE that capped a
        line at one design. Such a database is ALREADY stamped 95, so nothing
        but a shape check brings it forward - without one every read of the
        Production Status page dies on `no such column: design_id`."""
        with db.get_connection() as conn:
            conn.execute("DROP TABLE purchase_order_item_batches")
            conn.execute("DROP TABLE purchase_order_item_production")
            conn.execute("""
                CREATE TABLE purchase_order_item_production (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchase_order_item_id  INTEGER NOT NULL UNIQUE
                                              REFERENCES purchase_order_items(id) ON DELETE CASCADE,
                    status                  TEXT NOT NULL DEFAULT 'pending',
                    updated_by              INTEGER REFERENCES users(id),
                    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE purchase_order_item_batches (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchase_order_item_id  INTEGER NOT NULL
                                              REFERENCES purchase_order_items(id) ON DELETE CASCADE,
                    sr_no                   INTEGER NOT NULL,
                    batch_number            TEXT,
                    production_date         TEXT,
                    quantity_boxes          REAL NOT NULL DEFAULT 0,
                    remarks                 TEXT
                )
            """)
            conn.execute("CREATE INDEX idx_po_item_production_item "
                         "ON purchase_order_item_production(purchase_order_item_id)")
            # A real line to hang the rows off - both tables are FK'd to it.
            conn.execute("INSERT INTO tenants (name, slug) VALUES ('T', 't')")
            conn.execute("INSERT INTO users (company_id, username, password_hash, full_name, role) "
                         "VALUES (1, 'u', 'x', 'U', 'admin')")
            conn.execute("INSERT INTO purchase_orders (company_id, po_number, po_date, seller_name, created_by) "
                         "VALUES (1, 'PO1', '2026-03-01', 'S', 1)")
            item_id = conn.execute(
                "INSERT INTO purchase_order_items (purchase_order_id, sr_no, product_name) "
                "VALUES (1, 1, 'Tiles')").lastrowid
            # Rows recorded under that cut must survive, as design-less.
            conn.execute("INSERT INTO purchase_order_item_production "
                         "(purchase_order_item_id, status) VALUES (?, 'ready')", (item_id,))
            conn.execute("INSERT INTO purchase_order_item_batches "
                         "(purchase_order_item_id, sr_no, batch_number) VALUES (?, 1, 'B-OLD')", (item_id,))

        db.init_schema(tmp_config.SCHEMA_PATH)

        for table in ("purchase_order_item_production", "purchase_order_item_batches"):
            columns = {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
            assert {"design_id", "design_name"} <= columns, table
        kept = db.query_one("SELECT purchase_order_item_id, design_id, status "
                            "FROM purchase_order_item_production")
        assert (kept["design_id"], kept["status"]) == (None, "ready")
        assert db.query_one("SELECT batch_number FROM purchase_order_item_batches")["batch_number"] == "B-OLD"

        # The old UNIQUE is gone, so one line can now carry several designs...
        item_id = kept["purchase_order_item_id"]
        with db.get_connection() as conn:
            for design in ("Carrara", "Statuario"):
                conn.execute("INSERT INTO purchase_order_item_production "
                             "(purchase_order_item_id, design_name, status) VALUES (?, ?, 'pending')",
                             (item_id, design))
        assert db.query_one("SELECT COUNT(*) AS c FROM purchase_order_item_production")["c"] == 3
        # ...but still only one row per design.
        with pytest.raises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute("INSERT INTO purchase_order_item_production "
                             "(purchase_order_item_id, design_name, status) VALUES (?, 'carrara', 'ready')",
                             (item_id,))

    def test_migrate_adds_the_production_status_tables(self, db, tmp_config):
        """v95. A database written before Production Status existed has
        neither table; init_schema must bring both back on the next start."""
        with db.get_connection() as conn:
            conn.execute("DROP TABLE purchase_order_item_batches")
            conn.execute("DROP TABLE purchase_order_item_production")
        db.init_schema(tmp_config.SCHEMA_PATH)
        tables = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'purchase_order_item_%'")}
        assert {"purchase_order_item_production", "purchase_order_item_batches"} <= tables
        assert db.get_schema_version() == SCHEMA_VERSION


class TestQueryExecute:
    def test_execute_insert_returns_lastrowid(self, db):
        new_id = db.execute("INSERT INTO tenants (name, slug) VALUES (?, ?)", ("A", "a"))
        assert isinstance(new_id, int) and new_id > 0

    def test_query_returns_rows_as_mappings(self, db):
        db.execute("INSERT INTO tenants (name, slug) VALUES (?, ?)", ("A", "a"))
        rows = db.query("SELECT * FROM tenants")
        assert rows[0]["name"] == "A"  # sqlite3.Row indexes by column name

    def test_query_one_returns_none_when_empty(self, db):
        assert db.query_one("SELECT * FROM tenants WHERE id = 999") is None

    def test_query_one_returns_single_row(self, db):
        db.execute("INSERT INTO tenants (name, slug) VALUES (?, ?)", ("Solo", "solo"))
        row = db.query_one("SELECT name FROM tenants WHERE slug = 'solo'")
        assert row["name"] == "Solo"


class TestTransactionSemantics:
    def test_error_inside_connection_rolls_back(self, db):
        db.execute("INSERT INTO tenants (name, slug) VALUES ('First', 'first')")
        with pytest.raises(sqlite3.Error):
            with db.get_connection() as conn:
                conn.execute("INSERT INTO tenants (name, slug) VALUES ('Second', 'second')")
                # Violate NOT NULL to force an error after a valid insert.
                conn.execute("INSERT INTO tenants (name, slug) VALUES (NULL, NULL)")
        # The whole block rolled back: 'Second' must not survive.
        assert db.query_one("SELECT id FROM tenants WHERE slug = 'second'") is None

    def test_foreign_keys_are_enforced(self, db):
        # users.company_id references tenants(id); an orphan must be rejected.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO users (company_id, username, password_hash, full_name, role) "
                "VALUES (?, ?, ?, ?, ?)",
                (999, "x", "h", "X", "admin"),
            )


class TestBackupCopy:
    def test_create_backup_copy_is_a_usable_db(self, db, tmp_path):
        db.execute("INSERT INTO tenants (name, slug) VALUES ('Backup Me', 'backup-me')")
        dest = str(tmp_path / "copy.db")
        db.create_backup_copy(dest)
        assert os.path.exists(dest)
        # The copy carries both the data and the version stamp.
        assert Database.read_user_version(dest) == SCHEMA_VERSION
        conn = sqlite3.connect(dest)
        try:
            name = conn.execute("SELECT name FROM tenants WHERE slug='backup-me'").fetchone()[0]
        finally:
            conn.close()
        assert name == "Backup Me"

    def test_get_schema_version_of_versionless_db(self, tmp_path):
        # A raw sqlite file with no user_version stamp reads back as 0.
        path = str(tmp_path / "raw.db")
        sqlite3.connect(path).close()
        assert Database.read_user_version(path) == 0
