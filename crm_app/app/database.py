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
            for column in ("lut", "bin", "address"):
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
