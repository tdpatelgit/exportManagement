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
    is_converted         INTEGER NOT NULL DEFAULT 0,     -- becomes 1 once turned into a buyer/supplier/exporter
    converted_client_type TEXT CHECK (converted_client_type IN ('Buyer', 'Supplier', 'Exporter')),
    converted_client_id  INTEGER   -- id in whichever of buyers/suppliers/exporters converted_client_type names
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
-- BUYERS / EXPORTERS  (a lead "graduates" into one of these once approved
-- by an admin. The two tables are deliberately identical in shape - Buyer
-- and Exporter are treated as having the same data/documentation structure
-- for now, per the same "generated from a lead" pattern as clients used to
-- work; they may diverge later once exporter document types are defined.)
-- ============================================================
CREATE TABLE IF NOT EXISTS buyers (
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

CREATE TABLE IF NOT EXISTS exporters (
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

-- Contact persons for a Buyer or Exporter - identical shape, so one table
-- (with a parent_type discriminator, same pattern as `communications` below)
-- serves both instead of two near-identical tables.
CREATE TABLE IF NOT EXISTS party_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type TEXT NOT NULL CHECK (parent_type IN ('buyer', 'exporter')),
    parent_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- SUPPLIERS  (also "graduates" from an approved lead, but its data mirrors
-- OUR COMPANY's own profile shape - GSTIN/PAN/IEC/bank/contacts - instead of
-- a buyer/exporter's lead-shaped fields. Company logo, BIN and LUT are
-- deliberately NOT carried (those are our_company-specific). Document types
-- for suppliers aren't defined yet - status is borrowed from the buyer/
-- exporter pipeline for now and may change once that's specified.)
-- ============================================================
CREATE TABLE IF NOT EXISTS suppliers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    lead_id             INTEGER REFERENCES leads(id),   -- originating lead
    company_name        TEXT NOT NULL,
    address             TEXT,
    gstin               TEXT,
    cin_llp_no          TEXT,       -- optional: CIN (company) or LLPIN (LLP) registration number
    pan_no              TEXT,
    iec                 TEXT,
    status              TEXT NOT NULL DEFAULT 'proforma_invoice_submission_pending'
                        CHECK (status IN (
                            'proforma_invoice_submission_pending',
                            'purchase_order_submission_pending',
                            'purchase_invoice_submission_pending',
                            'export_invoice_submission_pending',
                            'commercial_invoice_submission_pending'
                        )),
    created_by          INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supplier_contact_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('phone', 'email')),
    value           TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier_contact_persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier_bank_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    bank_name       TEXT NOT NULL,
    account_number  TEXT NOT NULL,
    ifsc_code       TEXT,
    swift_code      TEXT,
    branch          TEXT,
    bank_address    TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- COMMUNICATIONS
-- One shared table for lead, buyer, supplier and exporter communications.
-- `parent_type` + `parent_id` act as a polymorphic foreign key - this keeps
-- one CommunicationRepository usable for every parent (Liskov substitution:
-- a Lead, Buyer, Supplier and Exporter are all "communicable" parents)
-- instead of four near-identical tables/classes. Scoped transitively via the
-- parent's own company_id - no company_id column here.
-- ============================================================
CREATE TABLE IF NOT EXISTS communications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type     TEXT NOT NULL CHECK (parent_type IN ('lead', 'buyer', 'supplier', 'exporter')),
    parent_id       INTEGER NOT NULL,
    employee_id     INTEGER NOT NULL REFERENCES users(id),
    comm_date       TEXT NOT NULL,              -- date/time of the communication
    mode            TEXT NOT NULL,              -- whatsapp, wechat, call, email, in_person, other
    description     TEXT NOT NULL,              -- what was discussed
    follow_up_date  TEXT,                       -- optional
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- PAYMENT HISTORY (buyer/supplier/exporter only)
-- `parent_type` + `parent_id` is the same polymorphic pattern as
-- `communications` - buyers/exporters/suppliers each have their own id
-- space, so a plain client_id would be ambiguous once more than one type
-- has data.
-- ============================================================
CREATE TABLE IF NOT EXISTS payment_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type         TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier', 'exporter')),
    parent_id           INTEGER NOT NULL,
    account_name        TEXT NOT NULL,          -- which of our accounts received/sent it
    payment_datetime    TEXT NOT NULL,
    amount_original     REAL NOT NULL,
    currency_code       TEXT NOT NULL,          -- e.g. USD, EUR (never INR, per brief)
    conversion_rate     REAL NOT NULL,           -- rate used at time of entry (1 unit -> INR)
    amount_inr          REAL NOT NULL,           -- auto-calculated amount_original * conversion_rate
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- DOCUMENTS (buyer/supplier/exporter only) - metadata for now; future plan
-- will move this to its own dedicated database once file storage is
-- introduced. Same parent_type/parent_id pattern as payment_history above.
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type     TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier', 'exporter')),
    parent_id       INTEGER NOT NULL,
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
    logo_path       TEXT,       -- company logo, relative to static/ (shown in the app sidebar and on generated documents)
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
-- PRODUCT CATALOG  (category / product / sub category / design:
-- a CATEGORY is a folder at the catalog root that groups products and can
-- nest to any depth via self-reference (category_id=NULL products sit
-- directly at the root, the same way a design can sit directly under a
-- product); a PRODUCT is the tax/HSN identity AND the physical packing spec
-- (pallet types, quantity, alternate quantity, unit, weight class) that
-- quotations, proforma invoices and packing lists all read from - every
-- design under a product shares that spec; SUB CATEGORIES (the
-- product_folders table) organise designs under a product and can nest to
-- any depth (but only inside a product); a DESIGN is the sellable leaf
-- holding price and photos)
-- ============================================================
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL REFERENCES tenants(id),
    parent_id   INTEGER REFERENCES categories(id) ON DELETE CASCADE,  -- NULL = catalog root
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    category_id         INTEGER REFERENCES categories(id) ON DELETE CASCADE,  -- NULL = catalog root
    product_name        TEXT NOT NULL,
    description         TEXT,
    hsn_code            TEXT,
    igst_percent        REAL,           -- the only tax input; SGST/CGST are stored as half of it
    sgst_percent        REAL,
    cgst_percent        REAL,
    quantity_unit       TEXT NOT NULL DEFAULT 'PCS',   -- what `quantity` is measured in
    quantity            TEXT,           -- per-box quantity (e.g. pcs per box)
    alternate_quantity_unit TEXT NOT NULL DEFAULT 'SQM',  -- what `alternate_quantity` is measured in; prefills document lines' Unit column
    alternate_quantity  TEXT,           -- per-box quantity, drives the Boxes x AltQty auto-calc
    weight_class        TEXT,
    net_weight_kg       REAL,           -- net weight per box (KG); drives the packing list's Boxes x weight auto-calc
    gross_weight_kg     REAL,           -- gross weight per box (KG); same auto-calc as net_weight_kg
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Named pallet storage options for one product (e.g. "pine pallet" holding
-- 31 boxes). Every product also implicitly has a "loose" option - goods
-- sold unpalletised, zero pallets - which is NOT stored here. The alternate
-- quantity a pallet holds is never stored: it's always derived as
-- boxes_per_pallet x the product's per-box alternate_quantity.
CREATE TABLE IF NOT EXISTS product_pallet_types (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    boxes_per_pallet    REAL NOT NULL,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL REFERENCES tenants(id),
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES product_folders(id) ON DELETE CASCADE,  -- NULL = top level inside the product
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS designs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    product_id              INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    folder_id               INTEGER REFERENCES product_folders(id) ON DELETE CASCADE,  -- NULL = directly under the product
    design_name             TEXT NOT NULL,
    description             TEXT,
    surface                 TEXT,          -- optional finish, e.g. GLOSSY / MATT / CHROME (prints on packing lists)
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
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,   -- optional, just for prefill/reference
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
    display_mode              TEXT NOT NULL DEFAULT 'index',  -- goods layout: 'index' (numbered) | 'surface' (grouped by category + surface)
    created_by                 INTEGER NOT NULL REFERENCES users(id),
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, invoice_number)
);

CREATE TABLE IF NOT EXISTS proforma_invoice_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    proforma_invoice_id   INTEGER NOT NULL REFERENCES proforma_invoices(id) ON DELETE CASCADE,
    sr_no                 INTEGER NOT NULL,
    product_id            INTEGER REFERENCES products(id) ON DELETE SET NULL,   -- optional, just for prefill/reference
    product_name          TEXT NOT NULL,
    dimension_mm          TEXT,
    hsn_code              TEXT,
    surface               TEXT,      -- optional finish (GLOSSY / MATT / ...), drives the surface-grouped print view
    pallets                REAL,      -- "Plts" column
    quantity_boxes        REAL,
    quantity_value         REAL NOT NULL DEFAULT 0,
    unit                  TEXT NOT NULL DEFAULT 'SQM',
    price_usd             REAL NOT NULL DEFAULT 0,
    total_usd             REAL NOT NULL DEFAULT 0
);

-- ============================================================
-- PURCHASE ORDERS  (header + line items, number generated as
-- PO{YYYYMMDD}{seq-of-that-day} per company. The next document after the
-- Proforma Invoice in the client pipeline: OUR company is the BUYER and a
-- supplier is the SELLER, prices are in INR (typically ex-factory per box).
-- Can be started from an existing proforma invoice - proforma_invoice_id is
-- a "generated from" reference only, same pattern as
-- proforma_invoices.quotation_id. Tax percentages are stored; the amounts,
-- round-off and final order value are always derived from the items.
-- ============================================================
CREATE TABLE IF NOT EXISTS purchase_orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    po_number               TEXT NOT NULL,
    po_date                 TEXT NOT NULL,
    lead_id                 INTEGER REFERENCES leads(id),               -- optional, prefill/reference only
    proforma_invoice_id     INTEGER REFERENCES proforma_invoices(id),   -- optional, "generated from" reference only
    seller_supplier_id      INTEGER REFERENCES suppliers(id),           -- optional, the Supplier picked as seller
    seller_name             TEXT NOT NULL,
    seller_address          TEXT,
    seller_pan              TEXT,
    seller_gstin            TEXT,
    seller_ref_no           TEXT,
    port_of_loading         TEXT,
    port_of_discharge       TEXT,
    container_details       TEXT,
    delivery_time           TEXT,          -- e.g. "20 DAY FROM PO DATE"
    advance_percent         TEXT,          -- e.g. "0%" - free text half of the payment terms block
    payment_terms           TEXT,          -- e.g. "40 DAYS AGAINST INVOICE DATE 100%"
    remarks                 TEXT,
    igst_percent            REAL NOT NULL DEFAULT 0,
    cgst_percent            REAL NOT NULL DEFAULT 0,
    sgst_percent            REAL NOT NULL DEFAULT 0,
    created_by              INTEGER NOT NULL REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, po_number)
);

CREATE TABLE IF NOT EXISTS purchase_order_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_id   INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    sr_no               INTEGER NOT NULL,
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,   -- optional, just for prefill/reference
    product_name        TEXT NOT NULL,
    hsn_code            TEXT,
    quantity_boxes      REAL,
    quantity_value      REAL NOT NULL DEFAULT 0,
    unit                TEXT NOT NULL DEFAULT 'SQM',
    price_inr           REAL NOT NULL DEFAULT 0,
    price_per           TEXT NOT NULL DEFAULT 'BOX',   -- what price_inr is per: 'BOX' or the row's unit
    total_inr           REAL NOT NULL DEFAULT 0
);

-- ============================================================
-- PACKING LISTS  (header + line items, number generated as
-- PL{YYYYMMDD}{seq-of-that-day} per company. Normally started from an
-- existing proforma invoice, but can also be started directly from a
-- Quotation (skipping the PI step) - proforma_invoice_id/quotation_id are
-- both "generated from" reference only, same pattern as
-- proforma_invoices.quotation_id. Each line breaks a product's quantity down
-- into a specific DESIGN in smaller quantities.)
-- ============================================================
CREATE TABLE IF NOT EXISTS packing_lists (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    packing_list_number     TEXT NOT NULL,
    packing_list_date       TEXT NOT NULL,
    lead_id                 INTEGER REFERENCES leads(id),               -- optional, prefill/reference only
    proforma_invoice_id     INTEGER REFERENCES proforma_invoices(id),   -- optional, "generated from" reference only
    quotation_id            INTEGER REFERENCES quotations(id),         -- optional, "generated from" reference only (skips the PI step)
    purchase_order_id       INTEGER REFERENCES purchase_orders(id),    -- optional, "generated from" reference only (the PO's own PL)
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
    container_details       TEXT,
    terms_of_delivery       TEXT,
    remarks                 TEXT,
    created_by              INTEGER NOT NULL REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, packing_list_number)
);

CREATE TABLE IF NOT EXISTS packing_list_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    packing_list_id     INTEGER NOT NULL REFERENCES packing_lists(id) ON DELETE CASCADE,
    sr_no               INTEGER NOT NULL,
    product_id          INTEGER REFERENCES products(id) ON DELETE SET NULL,   -- optional, just for prefill/reference
    product_name        TEXT NOT NULL,
    design_id           INTEGER REFERENCES designs(id) ON DELETE SET NULL,    -- optional, just for prefill/reference
    design_name         TEXT,                              -- snapshot of the chosen design
    hsn_code            TEXT,
    box_per_pallet      REAL,                              -- BOX PER PALLET column on the printed sheet
    pallets             REAL,
    quantity_boxes      REAL,
    pcs                 REAL,                              -- PCS column on the printed sheet
    quantity_value      REAL NOT NULL DEFAULT 0,
    unit                TEXT NOT NULL DEFAULT 'SQM',
    net_weight_kg       REAL,
    gross_weight_kg     REAL
);

-- ============================================================
-- DOCUMENT VERSIONS  (append-only history for quotations, proforma
-- invoices and packing lists. Every create/update snapshots the full
-- header+items state of the document as JSON under the next version
-- number for that (document_type, document_id) pair - the live row in
-- quotations/proforma_invoices/packing_lists always stays the current
-- version, editing never mints a new document number, and admins can
-- browse/open any past version read-only via this table.)
-- ============================================================
CREATE TABLE IF NOT EXISTS document_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    document_type       TEXT NOT NULL,   -- 'quotation' | 'proforma_invoice' | 'purchase_order' | 'packing_list'
    document_id         INTEGER NOT NULL,
    version_number      INTEGER NOT NULL,
    document_number     TEXT NOT NULL,   -- snapshot of quotation_number/invoice_number/packing_list_number, for display
    snapshot            TEXT NOT NULL,   -- JSON: full header fields + items, as they were at this version
    changed_by          INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_type, document_id, version_number)
);

-- Helpful indexes for the dashboards/reports (grouping by employee, date
-- range filters, and lookups by parent are the hottest queries).
CREATE INDEX IF NOT EXISTS idx_leads_created_by ON leads(created_by);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_comms_parent ON communications(parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_comms_employee ON communications(employee_id);
-- idx_payments_parent / idx_documents_parent live in database.py's _migrate:
-- on a pre-v13 DB, payment_history/documents don't have a parent_type
-- column yet when this script runs (see the v13 rebuild there).
CREATE INDEX IF NOT EXISTS idx_party_contacts_parent ON party_contacts(parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_buyers_company ON buyers(company_id);
CREATE INDEX IF NOT EXISTS idx_exporters_company ON exporters(company_id);
CREATE INDEX IF NOT EXISTS idx_suppliers_company ON suppliers(company_id);
CREATE INDEX IF NOT EXISTS idx_categories_company ON categories(company_id);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
-- idx_products_category lives in database.py's _migrate: on a pre-v4 DB the
-- category_id column doesn't exist yet when this script runs.
CREATE INDEX IF NOT EXISTS idx_pallet_types_product ON product_pallet_types(product_id);
CREATE INDEX IF NOT EXISTS idx_product_folders_product ON product_folders(product_id);
CREATE INDEX IF NOT EXISTS idx_product_folders_parent ON product_folders(parent_id);
CREATE INDEX IF NOT EXISTS idx_designs_product ON designs(product_id);
CREATE INDEX IF NOT EXISTS idx_designs_folder ON designs(folder_id);
CREATE INDEX IF NOT EXISTS idx_quotations_created_by ON quotations(created_by);
CREATE INDEX IF NOT EXISTS idx_quotations_date ON quotations(quotation_date);
CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation ON quotation_items(quotation_id);
CREATE INDEX IF NOT EXISTS idx_tenants_active ON tenants(is_active);
CREATE INDEX IF NOT EXISTS idx_proforma_invoices_created_by ON proforma_invoices(created_by);
CREATE INDEX IF NOT EXISTS idx_proforma_invoices_date ON proforma_invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_proforma_invoice_items_invoice ON proforma_invoice_items(proforma_invoice_id);
CREATE INDEX IF NOT EXISTS idx_proforma_invoices_company ON proforma_invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_company ON purchase_orders(company_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_created_by ON purchase_orders(created_by);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_date ON purchase_orders(po_date);
CREATE INDEX IF NOT EXISTS idx_purchase_order_items_po ON purchase_order_items(purchase_order_id);
CREATE INDEX IF NOT EXISTS idx_packing_lists_company ON packing_lists(company_id);
CREATE INDEX IF NOT EXISTS idx_packing_lists_created_by ON packing_lists(created_by);
CREATE INDEX IF NOT EXISTS idx_packing_lists_date ON packing_lists(packing_list_date);
CREATE INDEX IF NOT EXISTS idx_packing_list_items_list ON packing_list_items(packing_list_id);
CREATE INDEX IF NOT EXISTS idx_document_versions_lookup ON document_versions(document_type, document_id);
