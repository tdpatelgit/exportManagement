# Ledger CRM — Trade & Export CRM (web-based)

A multi-tenant, role-based CRM for trading/export businesses: each company
gets its own fully separate employees, leads, clients, product catalog,
quotations, and business profile. Employees generate and work leads, admins
approve leads into clients, generate quotations, track payments
(auto-converted to INR), documents, and see per-employee performance.

---

## 1. Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create the database + your first company and its first admin login
python seed.py
#    -> follow the prompts (company name / username / full name / password)

# 3. Run the app
python run.py
#    -> open http://127.0.0.1:5000, pick your company, and log in
```

That's it — the SQLite database is created automatically on first run at
`instance/crm.db`. No external database server is required.

### Adding another company

Each company is a fully independent workspace (own employees, leads,
clients, catalog, quotations, business profile). Adding one is a deliberate
CLI step, not an in-app action:

```bash
python seed.py --new-company "Acme Exports"
#    -> follow the prompts to create that company's first admin
```

Usernames only need to be unique *within* a company — two different
companies can each have an "admin".

### Configuration

Copy `.env.example` to `.env` and adjust as needed (secret key, host/port,
database path). See the comments in `config.py` for every available
setting, including the fallback currency-conversion rates used when there
is no internet connection.

---

## 2. Who can do what

| | Employee | Admin |
|---|---|---|
| Generate a new lead | ✅ | ✅ |
| Edit a lead/client's **compulsory** fields (company name, phone, email, client type) | ❌ | ✅ |
| Add contacts / log communications / change pipeline status on **their own** leads | ✅ | ✅ (any lead in their company) |
| Record client payments, documents, communications | ✅ | ✅ |
| Approve a lead → convert it to a client | ❌ | ✅ |
| Generate a quotation (for a lead, synced automatically to the client once converted) | ✅ | ✅ |
| Browse the product catalog | ✅ | ✅ |
| Manage the product catalog (add/edit/delete groups & products) | ❌ | ✅ |
| Change their own username / password | ✅ | ✅ |
| Change any employee's username / lock an employee's account | ❌ | ✅ |
| See every employee's lead & communication counts | ❌ | ✅ |
| Edit "Our Company" profile (their own company's GSTIN/PAN/bank/LUT) | ❌ | ✅ |
| Create new logins, run reports | ❌ | ✅ |

Every one of these checks is enforced server-side in the service layer
(never just by hiding a button), and every record fetch also verifies it
belongs to the current user's own company before returning it.

This mirrors the brief exactly: *"Any changes to compulsory fields must be
done by admins only"* — everything else (contacts, communications, status,
payments, documents) can be added by the employee working the account.

---

## 3. Data model

```
TENANT (company/workspace, picked at login)
 └─ everything below belongs to exactly one tenant

LEAD
 ├─ company_name, phone, email          (compulsory)
 ├─ facebook, instagram, other_social   (optional)
 ├─ contact person(s)                   (1+ required, multiple allowed)
 ├─ communications                      (employee, date, mode, notes, optional follow-up)
 ├─ quotations                          (0+, see QUOTATION below)
 └─ status: new → in_communication → in_follow_up → long_follow_up → quotation_submission_pending → in_client

        │  admin approves
        ▼

CLIENT   (created from an approved lead — carries every field + contact across)
 ├─ status: proforma invoice / purchase order / purchase invoice /
 │          export invoice / commercial invoice — submission pending
 ├─ client_type: Supplier / Exporter / Buyer  (Buyer is default)
 ├─ payment history   (account, datetime, amount + currency, auto-converted to INR)
 ├─ documents          (manually recorded metadata, plus every quotation made
 │                      against the originating lead - shown together)
 └─ communications      (same shape as a lead's)

QUOTATION   (auto-numbered QT{YYYYMMDD}{seq}, one product-line table + a
             header of shipping/bank/payment terms)
 ├─ linked to a lead (optional) - a converted client's quotations are derived
 │  from its originating lead, not stored twice
 ├─ line items pulled from the product catalog, or typed free-hand
 └─ invoice value = subtotal + sea freight + insurance + certification +
                     other charges − discount, printed with an amount-in-words line

PRODUCT CATALOG   (folder tree, admin-managed, employee-browsable)
 └─ groups nest to any depth; each group holds subgroups and/or products

OUR COMPANY   (one row per tenant, admin-only)
 ├─ company_name, GSTIN, PAN, IEC, BIN
 ├─ LUT(s)             (multiple, one per financial year)
 ├─ contact details    (multiple phones/emails, one of each required)
 ├─ contact person(s)  (multiple, one required)
 └─ bank detail(s)     (multiple, one required)
```

---

## 4. Architecture (why it's built this way)

The codebase is layered so that **SQL lives in one place, business rules
live in another, and HTTP handling lives in a third** — this is what the
brief's "strictly follow SOLID principles" translates to in practice:

```
app/routes/*.py     HTTP layer — parses requests, calls a service, flashes
                     the result. Contains no SQL and no business rules.
        │
        ▼
app/services.py     Business rules layer — "company name is compulsory",
                     "only an admin can edit compulsory fields", currency
                     conversion, employee/lead/communication counting,
                     cross-tenant ownership checks.
                     Depends on repository ABCs, not on SQLite directly.
        │
        ▼
app/repositories.py Persistence layer — one class per entity
                     (LeadRepository, ClientRepository, TenantRepository,
                     ...), each behind an abstract base class. This is the
                     only layer that writes SQL.
        │
        ▼
app/database.py     The only module that imports `sqlite3`.
```

**How this maps to SOLID:**
- **S**ingle Responsibility — each class in each layer does exactly one job
  (e.g. `CurrencyService` only converts currency; `SqliteLeadRepository`
  only persists leads).
- **O**pen/Closed — new lead/client statuses, currencies, or report types
  are added by extending a list/method, not by editing unrelated code.
- **L**iskov Substitution — `communications` is one table/class used
  identically for both leads and clients (`parent_type` discriminator), so
  any code that logs a communication works the same regardless of parent.
- **I**nterface Segregation — `UserRepositoryBase`, `LeadRepositoryBase`,
  `ClientRepositoryBase` are small and specific, not one giant interface.
- **D**ependency Inversion — services take repository *abstractions* as
  constructor arguments (see `app/__init__.py`'s `ServiceContainer`, the
  composition root). Swapping SQLite for PostgreSQL later means writing a
  new `Postgres*Repository` set and changing `ServiceContainer` only —
  routes, services, and templates never change.

### Multi-tenancy

Every root entity (users, leads, clients, quotations, the product catalog,
each company's own business profile) carries a `company_id`. Everything
else (contacts, communications, payments, documents, quotation line items)
is scoped transitively through its parent, so nothing is duplicated. Any
`get_by_id`-style repository lookup is a raw, unscoped PK lookup by design —
the owning **service** method is responsible for asserting the fetched
row's `company_id` matches the current user's before using it (raising a
404-style "not found" rather than a 403, so a stray ID never confirms that
another company's record exists). See `LeadService.get`,
`ClientService.get`, `QuotationService.get`, and `ProductService.get_group`
/`get_product` for the pattern.

---

## 5. Project structure

```
crm_app/
├── run.py                 # entry point: python run.py
├── seed.py                # create a company + its first admin (see "Adding another company" above)
├── config.py               # all settings, loaded from .env
├── requirements.txt
├── app/
│   ├── __init__.py         # app factory + composition root (wires everything together)
│   ├── database.py          # the only file that knows this is SQLite; also owns schema migrations
│   ├── schema.sql            # full table definitions
│   ├── models.py             # plain data classes (Tenant, User, Lead, Client, Quotation, ...)
│   ├── repositories.py        # persistence layer, one class per entity
│   ├── services.py            # business rules layer
│   ├── exceptions.py          # ValidationError / PermissionDeniedError / NotFoundError
│   ├── utils.py                # @login_required / @admin_required + template filters (incl. amount-in-words)
│   ├── routes/                 # one blueprint per area (auth, leads, clients, admin, company, products, quotations, reports, profile)
│   ├── templates/               # Jinja2 templates ("Trade Ledger" visual design)
│   └── static/css/style.css      # the whole design system in one file
└── instance/
    ├── crm.db                    # created automatically on first run (not in git)
    └── backups/                   # manual pre-migration snapshots (not in git)
```

---

## 6. Currency conversion

Payments must be entered in a currency **other than INR**. When a payment
is recorded, `CurrencyService`:

1. Tries a live, no-API-key exchange rate lookup
   (`https://api.frankfurter.app/latest`).
2. If there's no internet connection, falls back to the static rates in
   `config.py` → `FALLBACK_RATES_TO_INR` (update these occasionally by
   hand if you're running the app offline for long periods).

Either way, the exact rate used is stored alongside the payment, so every
INR figure is always auditable.

---

## 7. Roadmap / future plans

Straight from the brief, in the order they make sense to build:

- [x] Admin dashboard tracking employee history & company records — **done**
- [x] Employee dashboard with follow-up notifications — **done**
- [x] Basic monthly/quarterly/yearly activity reports — **done** (`/reports`)
- [x] Product catalogue — **done** (folder-tree, admin-managed, browsable from the quotation builder)
- [x] Auto-generating documents from client + product data — **done** for quotations (`/quotations`); other document types (proforma invoice, export/commercial invoice) are still metadata-only under a client's Documents card and should follow the same lead-linked pattern quotations use when built
- [x] Self-service password / username change — **done** (`/account`)
- [x] Multiple independent companies on one install — **done** (see "Adding another company" above)
- [ ] Moving each document type to its own dedicated database/table set as volume grows
- [ ] Overdue-payment notifications (needs an expected-payment/due-date field, which doesn't exist yet — payment_history currently only records payments *received*)
- [ ] Real file storage for documents (currently metadata only: name/type/date/notes)
- [ ] In-app company management (currently CLI-only via `seed.py`)

---

## 8. Notes for whoever picks this up next

- Passwords are hashed with Werkzeug's `generate_password_hash` (scrypt) —
  never stored in plain text.
- Every write endpoint re-validates permissions server-side in the service
  layer, not just by hiding buttons in the template — so there's no way to
  bypass a compulsory-field-edit restriction by posting the form URL
  directly. The same layer also enforces the multi-tenancy boundary — see
  "Multi-tenancy" above.
- The `ContactRepository` class is intentionally shared by both leads and
  clients (same table shape, different table name) instead of copy-pasted —
  see `app/repositories.py`.
- All list/detail templates read from `LEAD_STATUSES` / `CLIENT_STATUSES` /
  `CLIENT_TYPES` / `COMMUNICATION_MODES` (injected into every template via
  `app/__init__.py`'s context processor) — add a new status or mode there
  and it shows up everywhere automatically.
- `app/database.py`'s `_migrate()` is where every schema change since launch
  lives (added columns, rebuilt tables when a constraint changed, the
  multi-tenancy backfill). It's idempotent and safe to run on every
  startup — new migrations should follow the same guarded,
  `PRAGMA table_info`-checked pattern already there.
- **Schema version + backups.** `app/database.py` defines `SCHEMA_VERSION` and
  stamps it onto every DB via `PRAGMA user_version`. Any admin can download a
  full snapshot (SQLite DB + product images, one ZIP) and restore one from the
  **Database Backup** page (`app/routes/backup.py` + `BackupService` in
  `app/services.py`). Because `_migrate()` runs on every restore, an older
  backup is carried forward automatically — so **every future restructure must
  bump `SCHEMA_VERSION` and add a data-preserving step to `_migrate()` (never
  drop rows)** to keep old backups integrable. See the comment above
  `SCHEMA_VERSION` for the exact recipe.
