"""
seed.py
-------
Run this to create the database and the first admin login for a company:

    python seed.py                             # first company on a fresh install
    python seed.py --new-company "Acme Exports" # add another, fully separate company

It is safe to run again later - it will not create a second admin for a
company that already has one, and it never touches existing data. Adding
companies is deliberately a CLI-only action (no in-app UI) - there's no
in-app concept of one admin managing multiple companies, so this stays a
one-time, explicit setup step per company.
"""

import argparse
import getpass
import re

from app import create_app


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "company"


def _create_admin_for(container, company_id: int, company_name: str) -> None:
    existing_admins = container.user_repo.list_all(company_id, role="admin")
    if existing_admins:
        print(f"'{company_name}' already has an admin account - nothing to do:")
        for admin in existing_admins:
            print(f"   - {admin.username} ({admin.full_name})")
        return

    print(f"\nLet's create the first admin login for '{company_name}'.\n")
    username = input("Admin username: ").strip() or "admin"
    full_name = input("Admin full name: ").strip() or "Administrator"
    password = getpass.getpass("Admin password: ").strip() or "admin123"

    container.auth_service.create_user(
        company_id=company_id, username=username, password=password,
        full_name=full_name, role="admin",
    )
    print(f"\nAdmin '{username}' created for '{company_name}'. You can now run `python run.py` and log in.")


def main():
    parser = argparse.ArgumentParser(description="Seed a company and its first admin login.")
    parser.add_argument(
        "--new-company", metavar="NAME",
        help="Create a brand new, fully separate company alongside any existing ones.",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        container = app.container

        if args.new_company:
            name = args.new_company.strip()
            tenant = container.tenant_repo.create(name, _slugify(name))
            print(f"Company '{name}' created.")
            _create_admin_for(container, tenant.id, name)
            return

        companies = container.tenant_repo.list_active()
        if not companies:
            print("No company exists yet. Let's create the first one.\n")
            name = input("Company name: ").strip() or "Company 1"
            tenant = container.tenant_repo.create(name, _slugify(name))
            _create_admin_for(container, tenant.id, name)
            return

        if len(companies) == 1:
            tenant = companies[0]
        else:
            print("Existing companies:")
            for i, t in enumerate(companies, start=1):
                print(f"  {i}. {t.name}")
            choice = input("Pick a number to seed an admin for (or press Enter to cancel): ").strip()
            if not choice.isdigit() or not (1 <= int(choice) <= len(companies)):
                print('Cancelled. Use --new-company "Name" to add a new company instead.')
                return
            tenant = companies[int(choice) - 1]

        _create_admin_for(container, tenant.id, tenant.name)


if __name__ == "__main__":
    main()
