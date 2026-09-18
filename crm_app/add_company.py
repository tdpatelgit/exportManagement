"""
add_company.py
---------------
Create a brand new, fully separate company plus its first admin login,
prompting for the details interactively:

    python add_company.py

Asks for the company name, then the admin's full name, admin id
(login username) and admin number (login password). This is the
interactive counterpart to `seed.py --new-company "Name"` - use that
instead if you want to pass the company name as a CLI argument.
"""

import getpass
import re
import sqlite3

from app import create_app


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "company"


def _create_tenant_unique(container, name: str):
    """tenants.slug is globally unique (including inactive tenants), so retry
    with a numeric suffix on collision instead of assuming the base slug is free."""
    base = _slugify(name)
    slug = base
    n = 2
    while True:
        try:
            return container.tenant_repo.create(name, slug)
        except sqlite3.IntegrityError:
            slug = f"{base}-{n}"
            n += 1


def main():
    app = create_app()
    with app.app_context():
        container = app.container

        company_name = input("Company name: ").strip()
        while not company_name:
            company_name = input("Company name (required): ").strip()

        admin_name = input("Admin name: ").strip()
        while not admin_name:
            admin_name = input("Admin name (required): ").strip()

        admin_id = input("Admin id (login username): ").strip()
        while not admin_id:
            admin_id = input("Admin id (required): ").strip()

        # Minimum length is enforced by AuthService (Config.PASSWORD_MIN_LENGTH).
        admin_number = getpass.getpass("Admin number (login password, min 10 chars): ").strip()
        while not admin_number:
            admin_number = getpass.getpass("Admin number (required): ").strip()

        tenant = _create_tenant_unique(container, company_name)
        container.auth_service.create_user(
            company_id=tenant.id, username=admin_id, password=admin_number,
            full_name=admin_name, role="admin",
        )

        print(f"\nCompany '{company_name}' created with admin '{admin_id}' ({admin_name}).")
        print("You can now run `python run.py` and log in.")


if __name__ == "__main__":
    main()
