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
