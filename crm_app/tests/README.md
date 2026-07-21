# Test suite

Regression tests for the CRM. The goal is simple: **before you ship a change,
run these and make sure nothing that used to work is now broken.**

## Running

From the `crm_app/` directory:

```bash
python -m pytest                    # run everything
python -m pytest tests/test_utils.py   # one file
python -m pytest -k quotation       # tests matching a keyword
python -m pytest -v                 # verbose: show every test name
```

(If you use the project virtualenv: `../CRMenv/bin/python -m pytest`.)

Every test runs against a **throwaway SQLite database in a temp folder** — the
real `instance/crm.db` and the real product-upload folder are never touched. No
network is used either: the live exchange-rate API call is monkeypatched.

> **Why `tmp_config` patches the real `Config` class:** `ServiceContainer` reads
> `Config.PRODUCT_UPLOAD_FOLDER` from its module-level import rather than from
> the config passed to `create_app`. Without patching the class attribute, a
> test that uploads an image would write into the developer's real
> `app/static/uploads/products/`. `TestTestIsolation` in
> `test_config_and_exceptions.py` guards this — if those tests ever fail, stop
> and fix the fixture before running the rest of the suite.

## Layout

| File | What it covers |
|------|----------------|
| `conftest.py` | Shared fixtures: `tmp_config`, `db`, `container`, `seed`, `app`, `client`, `logged_in_admin` |
| `test_utils.py` | Number-to-words, amount/INR formatters, all Jinja template filters |
| `test_models.py` | `from_row` mapping + every computed property (subtotals, tax, totals, round-off) |
| `test_config_and_exceptions.py` | Config constants and the custom exception types |
| `test_database.py` | Schema init/versioning, query/execute, rollback, FK enforcement, backup copy |
| `test_repositories.py` | Repository CRUD round-trips (catches column drift between schema.sql, SQL, and models) |
| `test_services_auth_currency.py` | AuthService (hashing, permissions) and CurrencyService (live + fallback rates) |
| `test_services_leads_clients.py` | Lead rules, permission split, lead→client conversion, payments |
| `test_services_products.py` | Product parse helpers, IGST tax split, pallet-type rules, catalog CRUD |
| `test_services_documents.py` | Quotation/PO/PI/packing-list number generation, line-item math, versioning |
| `test_services_proforma_po.py` | Proforma & Purchase Order flows, quotation→PI→PO prefills, client document feed |
| `test_services_packing_lists.py` | Packing-list arithmetic: boxes/pallets/pcs/qty/weight derivation rules |
| `test_services_catalog_tree.py` | Sub-category nesting, designs, image upload/replace/delete on disk |
| `test_services_company_stats_reports.py` | Our Company profile rules, dashboard stats, reports, client-status pipeline |
| `test_services_backup.py` | Backup ZIP round-trip + every restore rejection path (zip-slip, bad signature, newer schema) |
| `test_decorators_and_seed.py` | `login_required` / `admin_required` decorators, `seed.py` slugify |
| `test_routes.py` | Flask wiring, auth guards, login/logout, admin-only 403, error pages |
| `test_routes_pages.py` | Every major page renders, key POST flows, JSON APIs, tenant isolation over HTTP |

## Adding tests when you add code

- **New business rule in a service?** Add a case to the matching
  `test_services_*.py`. Use the `container` + `seed` fixtures — they give you a
  wired service layer over a fresh DB with one admin and one employee.
- **New model field / computed property?** Add to `test_models.py`.
- **New schema column or migration?** The repository round-trip tests will fail
  loudly if a column name drifts — extend `test_repositories.py` to cover it.
- **New route?** Add a smoke test to `test_routes.py` (renders / redirects /
  enforces its permission).

Keep tests fast and DB-only; don't reach for the network.
