"""
delete_company.py
------------------
One-off cleanup script: safely delete a duplicate/empty tenant (company)
row that never got an admin (or any other data) attached to it.

    python delete_company.py

Lists every tenant with its user count, lets you pick one, and refuses to
delete it unless it truly has zero users and zero rows in every other
table that hangs off tenants.id. This is deliberately conservative and
raw-SQL (there is no in-app "delete company" feature) - it is meant to
clean up a duplicate created by mistake, not to remove a real company's
data.
"""

import os
import sqlite3
import sys

from config import Config

# Every table with a company_id column that references tenants.id directly.
TENANT_OWNED_TABLES = [
    "users", "leads", "buyers", "suppliers", "transporters", "platform_logins",
    "our_company", "misc_currencies", "misc_nature_of_contracts",
    "misc_ports_of_loading", "misc_container_types", "misc_hsn_codes",
    "misc_countries", "misc_units", "permits", "booking_details",
    "categories", "products", "designs", "quotations", "proforma_invoices",
    "purchase_orders", "purchase_invoices", "export_invoices",
    "export_packing_lists", "loading_plannings", "packing_plannings",
    "packing_planning_labels", "packing_lists", "job_works", "job_outs",
    "job_ins",
]


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def main():
    db_path = Config.DATABASE_PATH
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}. Set DATABASE_PATH if it lives elsewhere.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    tenants = conn.execute("SELECT id, name, slug FROM tenants ORDER BY name, id").fetchall()
    if not tenants:
        print("No tenants found.")
        return

    print("Existing companies:")
    for tid, name, slug in tenants:
        user_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE company_id = ?", (tid,)
        ).fetchone()[0]
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE company_id = ? AND role = 'admin'", (tid,)
        ).fetchone()[0]
        print(f"  {tid}. {name}  (slug={slug}, users={user_count}, admins={admin_count})")

    choice = input("\nEnter the id of the company to delete (or press Enter to cancel): ").strip()
    if not choice.isdigit():
        print("Cancelled.")
        return
    company_id = int(choice)

    row = conn.execute("SELECT id, name FROM tenants WHERE id = ?", (company_id,)).fetchone()
    if not row:
        print(f"No company with id {company_id}.")
        return
    _, company_name = row

    # Refuse if there is ANY data anywhere under this tenant - this is a
    # cleanup tool for an empty duplicate, not a general "delete company" tool.
    blockers = []
    for table in TENANT_OWNED_TABLES:
        if not _table_exists(conn, table):
            continue
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE company_id = ?", (company_id,)
        ).fetchone()[0]
        if count:
            blockers.append(f"{table} ({count} row{'s' if count != 1 else ''})")

    if blockers:
        print(f"\nRefusing to delete '{company_name}' (id={company_id}) - it still has data in: "
              + ", ".join(blockers))
        print("This script only deletes companies with zero rows anywhere. Investigate first.")
        return

    confirm = input(
        f"\nThis will PERMANENTLY delete company '{company_name}' (id={company_id}), "
        f"which has no users or data. Type the company name to confirm: "
    ).strip()
    if confirm != company_name:
        print("Name did not match. Cancelled.")
        return

    conn.execute("DELETE FROM tenants WHERE id = ?", (company_id,))
    conn.commit()
    print(f"Company '{company_name}' (id={company_id}) deleted.")


if __name__ == "__main__":
    main()
