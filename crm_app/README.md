# Ledger CRM — Trade & Export CRM (web-based)

A role-based CRM for a trading/export business: employees generate and work
leads, admins approve leads into clients, track payments (auto-converted to
INR), documents, and see per-employee performance.

---

## 1. Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create the database + your first admin login
python seed.py
#    -> follow the prompts (username / full name / password)

# 3. Run the app
python run.py
#    -> open http://127.0.0.1:5000 and log in
```

That's it — the SQLite database is created automatically on first run at
`instance/crm.db`. No external database server is required.

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
| Add contacts / log communications / change pipeline status on **their own** leads | ✅ | ✅ (any lead) |
| Record client payments, documents, communications | ✅ | ✅ |
| Approve a lead → convert it to a client | ❌ | ✅ |
| See every employee's lead & communication counts | ❌ | ✅ |
| Edit "Our Company" profile | ❌ | ✅ |
| Create new logins, run reports | ❌ | ✅ |

This mirrors the brief exactly: *"Any changes to compulsory fields must be
done by admins only"* — everything else (contacts, communications, status,
payments, documents) can be added by the employee working the account.

---

## 3. Data model

```
LEAD
 ├─ company_name, phone, email          (compulsory)
 ├─ facebook, instagram, other_social   (optional)
 ├─ contact person(s)                   (1+ required, multiple allowed)
 ├─ communications                      (employee, date, mode, notes, optional follow-up)
 └─ status: new → in_communication → in_follow_up → long_follow_up → quotation_submission_pending

        │  admin approves
        ▼

CLIENT   (created from an approved lead — carries every field + contact across)
 ├─ status: proforma invoice / purchase order / purchase invoice /
 │          export invoice / commercial invoice — submission pending
 ├─ client_type: Supplier / Exporter / Buyer  (Buyer is default)
 ├─ payment history   (account, datetime, amount + currency, auto-converted to INR)
 ├─ documents          (name, type, date, notes — metadata only for now)
 └─ communications      (same shape as a lead's)

OUR COMPANY   (single row, admin-only)
 ├─ company_name, GSTIN, PAN, IEC
 ├─ contact details   (multiple phones/emails, one of each required)
 └─ contact person(s) (multiple, one required)
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
                     conversion, employee/lead/communication counting.
                     Depends on repository ABCs, not on SQLite directly.
        │
        ▼
app/repositories.py Persistence layer — one class per entity
                     (LeadRepository, ClientRepository, ...), each behind
                     an abstract base class. This is the only layer that
                     writes SQL.
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

---

## 5. Project structure

```
crm_app/
├── run.py                 # entry point: python run.py
├── seed.py                # one-time script to create the first admin
├── config.py               # all settings, loaded from .env
├── requirements.txt
├── app/
│   ├── __init__.py         # app factory + composition root (wires everything together)
│   ├── database.py          # the only file that knows this is SQLite
│   ├── schema.sql            # full table definitions
│   ├── models.py             # plain data classes (Lead, Client, User, ...)
│   ├── repositories.py        # persistence layer, one class per entity
│   ├── services.py            # business rules layer
│   ├── exceptions.py          # ValidationError / PermissionDeniedError / NotFoundError
│   ├── utils.py                # @login_required / @admin_required + template filters
│   ├── routes/                 # one blueprint per area (auth, leads, clients, admin, company, reports)
│   ├── templates/               # Jinja2 templates ("Trade Ledger" visual design)
│   └── static/css/style.css      # the whole design system in one file
└── instance/
    └── crm.db                    # created automatically on first run (not in git)
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
- [x] Employee dashboard with follow-up notifications — **done** (payment due-date notifications need a "due date" field on documents/invoices first — see below)
- [x] Basic monthly/quarterly/yearly activity reports — **done** (`/reports`), will expand once product & document data exists
- [ ] Product catalogue (needs a data model for products — not yet specified)
- [ ] Auto-generating documents (proforma invoice, quotation, export/commercial invoice) from client + product data
- [ ] Moving each document type to its own dedicated database/table set as volume grows
- [ ] Overdue-payment notifications (needs an expected-payment/due-date field, which doesn't exist yet — payment_history currently only records payments *received*)
- [ ] Real file storage for documents (currently metadata only: name/type/date/notes)
- [ ] Self-service password change / password reset flow

---

## 8. Notes for whoever picks this up next

- Passwords are hashed with Werkzeug's `generate_password_hash` (scrypt) —
  never stored in plain text.
- Every write endpoint re-validates permissions server-side in the service
  layer, not just by hiding buttons in the template — so there's no way to
  bypass a compulsory-field-edit restriction by posting the form URL
  directly.
- The `ContactRepository` class is intentionally shared by both leads and
  clients (same table shape, different table name) instead of copy-pasted —
  see `app/repositories.py`.
- All list/detail templates read from `LEAD_STATUSES` / `CLIENT_STATUSES` /
  `CLIENT_TYPES` / `COMMUNICATION_MODES` (injected into every template via
  `app/__init__.py`'s context processor) — add a new status or mode there
  and it shows up everywhere automatically.
