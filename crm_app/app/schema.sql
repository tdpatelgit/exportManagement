-- schema.sql
-- ----------
-- Full data definition for the CRM. Run once at startup by app/database.py
-- (CREATE TABLE IF NOT EXISTS, so it is always safe to re-run).
--
-- Naming convention: every table has an integer primary key `id`, a
-- `created_at` timestamp, and foreign keys named `<table>_id`.
--
-- Multi-tenancy: `tenants` is a company/business using this CRM (picked on
-- the login screen). Root entities (users, leads, clients, product_groups,
-- products, quotations, our_company) carry `company_id` directly; everything
-- else (contacts, communications, payments, documents, quotation_items, the
-- our_company_* detail tables) is scoped transitively through its parent FK
-- instead of duplicating company_id everywhere.

PRAGMA foreign_keys = ON;

-- ============================================================
-- TENANTS  (each is an independent company/business using this CRM)
-- ============================================================
CREATE TABLE IF NOT EXISTS tenants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,                 -- shown in the login dropdown
    slug        TEXT NOT NULL UNIQUE,
    is_active   INTEGER NOT NULL DEFAULT 1,     -- 1 = can log in, 0 = whole company locked out
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- USERS  (admins + employees)
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES tenants(id),
    username        TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'employee')),
    is_active       INTEGER NOT NULL DEFAULT 1,   -- 1 = can log in, 0 = disabled
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, username)
);

-- ============================================================
-- LEADS
-- ============================================================
CREATE TABLE IF NOT EXISTS leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    company_name        TEXT NOT NULL,                 -- compulsory (the LEAD's own business name - not the tenant)
    phone               TEXT NOT NULL,                  -- compulsory
    email               TEXT NOT NULL,                  -- compulsory
    facebook            TEXT,                           -- not compulsory
    instagram            TEXT,                           -- not compulsory
    other_social        TEXT,                           -- not compulsory
    status              TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN (
                            'new', 'in_communication', 'in_follow_up',
                            'long_follow_up', 'quotation_submission_pending', 'in_client'
                        )),
    created_by          INTEGER NOT NULL REFERENCES users(id),  -- employee who filled it
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    is_converted         INTEGER NOT NULL DEFAULT 0,     -- becomes 1 once turned into a client
    converted_client_id  INTEGER REFERENCES clients(id)
);

-- Contact persons for a lead. "Multiple allowed, one compulsory" is enforced
-- in the service layer (LeadService requires >= 1 row on create).
CREATE TABLE IF NOT EXISTS lead_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- CLIENTS  (a lead "graduates" into a client once approved by an admin)
-- ============================================================
CREATE TABLE IF NOT EXISTS clients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    lead_id             INTEGER REFERENCES leads(id),   -- originating lead
    company_name        TEXT NOT NULL,
    phone               TEXT NOT NULL,
    email               TEXT NOT NULL,
    facebook            TEXT,
    instagram           TEXT,
    other_social        TEXT,
    address             TEXT,
    client_type         TEXT NOT NULL DEFAULT 'Buyer'
                        CHECK (client_type IN ('Supplier', 'Exporter', 'Buyer')),
    status              TEXT NOT NULL DEFAULT 'proforma_invoice_submission_pending'
                        CHECK (status IN (
                            'proforma_invoice_submission_pending',
                            'purchase_order_submission_pending',
                            'purchase_invoice_submission_pending',
                            'export_invoice_submission_pending',
                            'commercial_invoice_submission_pending'
                        )),
    created_by          INTEGER NOT NULL REFERENCES users(id),  -- admin who approved conversion
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS client_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- COMMUNICATIONS
-- One shared table for BOTH lead communications and client communications.
-- `parent_type` + `parent_id` act as a polymorphic foreign key. This keeps
-- one CommunicationRepository usable for both entities (Liskov substitution:
-- a Lead and a Client are both "communicable" parents) instead of two
-- near-identical tables/classes. Scoped transitively via the parent lead/
-- client's own company_id - no company_id column here.
-- ============================================================
CREATE TABLE IF NOT EXISTS communications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type     TEXT NOT NULL CHECK (parent_type IN ('lead', 'client')),
    parent_id       INTEGER NOT NULL,
    employee_id     INTEGER NOT NULL REFERENCES users(id),
    comm_date       TEXT NOT NULL,              -- date/time of the communication
    mode            TEXT NOT NULL,              -- whatsapp, wechat, call, email, in_person, other
    description     TEXT NOT NULL,              -- what was discussed
    follow_up_date  TEXT,                       -- optional
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- PAYMENT HISTORY (client only)
-- ============================================================
CREATE TABLE IF NOT EXISTS payment_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    account_name        TEXT NOT NULL,          -- which of our accounts received/sent it
    payment_datetime    TEXT NOT NULL,
    amount_original     REAL NOT NULL,
    currency_code       TEXT NOT NULL,          -- e.g. USD, EUR (never INR, per brief)
    conversion_rate     REAL NOT NULL,           -- rate used at time of entry (1 unit -> INR)
    amount_inr          REAL NOT NULL,           -- auto-calculated amount_original * conversion_rate
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- DOCUMENTS (client only) - metadata for now; future plan will move this
-- to its own dedicated database once file storage is introduced.
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    document_name   TEXT NOT NULL,
    document_type   TEXT NOT NULL,      -- e.g. Proforma Invoice, Purchase Order...
    document_date   TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- OUR COMPANY  (one row per tenant - this tenant's own business profile,
-- shown on generated quotations. NOT the same thing as the `tenants` table
-- above: `tenants` is the workspace/login concept, `our_company` is that
-- workspace's own GSTIN/PAN/bank-details profile.)
-- ============================================================
CREATE TABLE IF NOT EXISTS our_company (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
    company_name    TEXT NOT NULL,
    address         TEXT,
    gstin           TEXT,
    pan_no          TEXT,
    iec             TEXT,
    bin             TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS our_company_lut_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    our_company_id  INTEGER NOT NULL REFERENCES our_company(id) ON DELETE CASCADE,
    lut_number      TEXT NOT NULL,
    financial_year  TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS our_company_contact_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    our_company_id  INTEGER NOT NULL REFERENCES our_company(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('phone', 'email')),
    value           TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS our_company_contact_persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    our_company_id  INTEGER NOT NULL REFERENCES our_company(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS our_company_bank_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    our_company_id  INTEGER NOT NULL REFERENCES our_company(id) ON DELETE CASCADE,
    bank_name       TEXT NOT NULL,
    account_number  TEXT NOT NULL,
    ifsc_code       TEXT,
    swift_code      TEXT,
    branch          TEXT,
    bank_address    TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- PRODUCTS  (folder-style catalog: groups can nest under groups to any
-- depth, and each group can hold any number of subgroups and products)
-- ============================================================
CREATE TABLE IF NOT EXISTS product_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES product_groups(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    group_id                INTEGER REFERENCES product_groups(id) ON DELETE CASCADE,
    product_name            TEXT NOT NULL,
    description             TEXT,
    hsn_code                TEXT,
    packing                 TEXT,
    quantity                TEXT,
    alternate_quantity      TEXT,
    weight_class            TEXT,
    price_usd               REAL,
    photo_path              TEXT,
    dimension_photo_path    TEXT,
    alt_text                TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- QUOTATIONS  (header + line items; the number is generated as
-- QT{YYYYMMDD}{seq-of-that-day}, e.g. QT20260702001, per company)
-- ============================================================
CREATE TABLE IF NOT EXISTS quotations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    quotation_number        TEXT NOT NULL,
    quotation_date          TEXT NOT NULL,
    lead_id                  INTEGER REFERENCES leads(id),   -- optional, just for prefill/reference
    buyer_name              TEXT NOT NULL,
    buyer_address           TEXT,
    buyer_reference_no      TEXT,
    port_of_loading         TEXT,
    port_of_discharge       TEXT,
    packing_details         TEXT,
    container_details       TEXT,
    shipping_mode           TEXT,
    shipping_terms          TEXT,
    payment_terms           TEXT,
    price_validity_days     INTEGER NOT NULL DEFAULT 30,
    remarks                 TEXT,
    sea_freight              REAL NOT NULL DEFAULT 0,
    insurance                REAL NOT NULL DEFAULT 0,
    certification            REAL NOT NULL DEFAULT 0,
    other_charges            REAL NOT NULL DEFAULT 0,
    discount_amount         REAL NOT NULL DEFAULT 0,
    bank_name               TEXT,
    bank_account_number     TEXT,
    bank_ifsc_code          TEXT,
    bank_swift_code         TEXT,
    bank_branch             TEXT,
    bank_address            TEXT,
    created_by              INTEGER NOT NULL REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, quotation_number)
);

CREATE TABLE IF NOT EXISTS quotation_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id        INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    sr_no               INTEGER NOT NULL,
    product_id          INTEGER REFERENCES products(id),   -- optional, just for prefill/reference
    product_name        TEXT NOT NULL,
    dimension_mm        TEXT,
    hsn_code            TEXT,
    quantity_boxes      REAL,
    quantity_value       REAL NOT NULL DEFAULT 0,
    unit                TEXT NOT NULL DEFAULT 'SQM',
    price_usd           REAL NOT NULL DEFAULT 0,
    total_usd           REAL NOT NULL DEFAULT 0
);

-- ============================================================
-- PROFORMA INVOICES  (header + line items, number generated as
-- PI{YYYYMMDD}{seq-of-that-day} per company. Can be started from an
-- existing quotation - quotation_id is a "generated from" reference only,
-- the row is its own independent record from then on, same as how
-- quotations reference an optional lead_id.)
-- ============================================================
CREATE TABLE IF NOT EXISTS proforma_invoices (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    invoice_number          TEXT NOT NULL,
    invoice_date            TEXT NOT NULL,
    lead_id                 INTEGER REFERENCES leads(id),        -- optional, prefill/reference only
    quotation_id            INTEGER REFERENCES quotations(id),   -- optional, "generated from" reference only
    export_ref_no           TEXT,
    buyer_order_no          TEXT,
    other_reference         TEXT,
    consignee_name          TEXT NOT NULL,
    consignee_address       TEXT,
    notify_name             TEXT,          -- "Buyer if other than consignee"
    notify_address          TEXT,
    country_of_origin       TEXT DEFAULT 'INDIA',
    country_of_destination  TEXT,
    vessel_flight           TEXT,
    port_of_loading         TEXT,
    port_of_discharge       TEXT,
    final_destination       TEXT,
    transhipment            TEXT,
    partial_shipment        TEXT,
    variation_in_qty        TEXT,
    delivery_period         TEXT,
    container_details       TEXT,
    terms_of_delivery       TEXT,
    payment_terms           TEXT,
    remarks                 TEXT,
    sea_freight              REAL NOT NULL DEFAULT 0,
    insurance                REAL NOT NULL DEFAULT 0,
    certification             REAL NOT NULL DEFAULT 0,
    other_charges             REAL NOT NULL DEFAULT 0,
    discount_amount          REAL NOT NULL DEFAULT 0,
    bank_name                TEXT,
    bank_account_number      TEXT,
    bank_ifsc_code            TEXT,
    bank_swift_code           TEXT,
    bank_branch               TEXT,
    bank_address               TEXT,
    created_by                 INTEGER NOT NULL REFERENCES users(id),
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, invoice_number)
);

CREATE TABLE IF NOT EXISTS proforma_invoice_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    proforma_invoice_id   INTEGER NOT NULL REFERENCES proforma_invoices(id) ON DELETE CASCADE,
    sr_no                 INTEGER NOT NULL,
    product_id            INTEGER REFERENCES products(id),   -- optional, just for prefill/reference
    product_name          TEXT NOT NULL,
    dimension_mm          TEXT,
    hsn_code              TEXT,
    pallets                REAL,      -- "Plts" column
    quantity_boxes        REAL,
    quantity_value         REAL NOT NULL DEFAULT 0,
    unit                  TEXT NOT NULL DEFAULT 'SQM',
    price_usd             REAL NOT NULL DEFAULT 0,
    total_usd             REAL NOT NULL DEFAULT 0
);

-- Helpful indexes for the dashboards/reports (grouping by employee, date
-- range filters, and lookups by parent are the hottest queries).
CREATE INDEX IF NOT EXISTS idx_leads_created_by ON leads(created_by);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_comms_parent ON communications(parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_comms_employee ON communications(employee_id);
CREATE INDEX IF NOT EXISTS idx_payments_client ON payment_history(client_id);
CREATE INDEX IF NOT EXISTS idx_documents_client ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_product_groups_parent ON product_groups(parent_id);
CREATE INDEX IF NOT EXISTS idx_products_group ON products(group_id);
CREATE INDEX IF NOT EXISTS idx_quotations_created_by ON quotations(created_by);
CREATE INDEX IF NOT EXISTS idx_quotations_date ON quotations(quotation_date);
CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation ON quotation_items(quotation_id);
CREATE INDEX IF NOT EXISTS idx_tenants_active ON tenants(is_active);
CREATE INDEX IF NOT EXISTS idx_proforma_invoices_created_by ON proforma_invoices(created_by);
CREATE INDEX IF NOT EXISTS idx_proforma_invoices_date ON proforma_invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_proforma_invoice_items_invoice ON proforma_invoice_items(proforma_invoice_id);
CREATE INDEX IF NOT EXISTS idx_proforma_invoices_company ON proforma_invoices(company_id);
