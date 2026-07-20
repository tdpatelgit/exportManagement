"""
tests/conftest.py
-----------------
Shared pytest fixtures for the whole suite.

The golden rule of this test suite: NO test ever touches the real
instance/crm.db or the real product upload folder. Every fixture below wires
the app at a throwaway temp path, so the suite is safe to run on a developer
machine or in CI without clobbering live data.

Fixtures provided:
  - `tmp_config`     : a Config subclass pointing every path at a tmp dir
  - `db`             : a fresh migrated Database on a tmp file
  - `container`      : a fully wired ServiceContainer over `db`
  - `seed`           : a small, known dataset (tenant + admin + employee)
  - `app` / `client` : a Flask app + test client over the same tmp DB
"""

import os
import sys
from dataclasses import dataclass

import pytest

# Make `import app` / `import config` resolve to crm_app/ no matter where
# pytest is invoked from.
CRM_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CRM_APP_DIR not in sys.path:
    sys.path.insert(0, CRM_APP_DIR)

from config import Config
from app.database import Database
from app import create_app, ServiceContainer


# --------------------------------------------------------------------------
# Config + database
# --------------------------------------------------------------------------
@pytest.fixture
def tmp_config(tmp_path):
    """A Config clone whose DB and upload paths live under a per-test tmp dir."""
    schema_path = os.path.join(CRM_APP_DIR, "app", "schema.sql")

    class TestConfig(Config):
        TESTING = True
        SECRET_KEY = "test-secret-key"
        DATABASE_PATH = str(tmp_path / "instance" / "test.db")
        SCHEMA_PATH = schema_path
        PRODUCT_UPLOAD_FOLDER = str(tmp_path / "uploads" / "products")
        WTF_CSRF_ENABLED = False

    return TestConfig


@pytest.fixture
def db(tmp_config):
    """A migrated, empty Database on a throwaway file."""
    database = Database(tmp_config.DATABASE_PATH)
    database.init_schema(tmp_config.SCHEMA_PATH)
    return database


@pytest.fixture
def container(db):
    """A fully wired service container over the tmp database."""
    return ServiceContainer(db)


# --------------------------------------------------------------------------
# A tiny known dataset every "needs data" test can lean on.
# --------------------------------------------------------------------------
@dataclass
class Seed:
    company_id: int
    admin: object
    employee: object


@pytest.fixture
def seed(container):
    """Creates one tenant with one admin and one employee.

    Passwords are fixed so auth tests can log in:
      admin    / admin-pass-123
      employee / emp-pass-123
    """
    tenant = container.tenant_repo.create("Test Exports", "test-exports")
    admin = container.auth_service.create_user(
        company_id=tenant.id, username="admin", password="admin-pass-123",
        full_name="Ada Admin", role="admin",
    )
    employee = container.auth_service.create_user(
        company_id=tenant.id, username="employee", password="emp-pass-123",
        full_name="Eve Employee", role="employee",
    )
    return Seed(company_id=tenant.id, admin=admin, employee=employee)


# --------------------------------------------------------------------------
# Flask app + client (integration tests)
# --------------------------------------------------------------------------
@pytest.fixture
def app(tmp_config):
    application = create_app(tmp_config)
    application.config.update(TESTING=True)
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in_admin(app, client):
    """A client already authenticated as a freshly seeded admin.

    Uses the app's own container so the session user resolves on every
    request. Returns (client, admin_user, company_id).
    """
    container = app.container
    tenant = container.tenant_repo.create("Web Co", "web-co")
    admin = container.auth_service.create_user(
        company_id=tenant.id, username="webadmin", password="web-pass-123",
        full_name="Web Admin", role="admin",
    )
    with client.session_transaction() as sess:
        sess["user_id"] = admin.id
    return client, admin, tenant.id
