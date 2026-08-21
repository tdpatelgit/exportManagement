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
    is_converted         INTEGER NOT NULL DEFAULT 0,     -- becomes 1 once turned into a buyer/supplier
    converted_client_type TEXT CHECK (converted_client_type IN ('Buyer', 'Supplier')),
    converted_client_id  INTEGER   -- id in whichever of buyers/suppliers converted_client_type names
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
-- BUYERS  (a lead "graduates" into one of these once approved by an admin,
-- per the same "generated from a lead" pattern as clients used to work.
-- Exporter used to be a second, identically-shaped table here alongside
-- Buyer - retired along with the rest of that party type.)
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
    country             TEXT,   -- "Country Name", picked from the Administration -> Miscellaneous country list
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

-- Contact persons for a Buyer (parent_type discriminator kept for the same
-- pattern as `communications` below, even with a single type using it now).
CREATE TABLE IF NOT EXISTS party_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type TEXT NOT NULL CHECK (parent_type IN ('buyer')),
    parent_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- SUPPLIERS  (also "graduates" from an approved lead, but its data mirrors
-- OUR COMPANY's own profile shape - GSTIN/PAN/IEC/bank/contacts - instead of
-- a buyer's lead-shaped fields. Company logo, BIN and LUT are deliberately
-- NOT carried (those are our_company-specific). Document types for
-- suppliers aren't defined yet - status is borrowed from the buyer
-- pipeline for now and may change once that's specified.)
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
-- TRANSPORTERS  (the fourth party type, and the only one that does NOT come
-- from a lead: a transporter is never sold to, it's just the haulier whose
-- registration details we have to quote on documents. So there's no lead_id,
-- no status pipeline, and no payments/communications/documents feed - only a
-- profile plus contact persons in the same shape buyers use. Its GSTIN cell
-- doubles as the transporter number, which is what the field is called on the
-- consignment paperwork.)
-- ============================================================
CREATE TABLE IF NOT EXISTS transporters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    address             TEXT,
    gstin_transporter_no TEXT,      -- GSTIN / Transporter No. (one and the same cell)
    pan_no              TEXT,
    cin_llp_no          TEXT,       -- optional: CIN (company) or LLPIN (LLP) registration number
    email               TEXT,
    created_by          INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Same name/phone/email/primary shape as party_contacts, but its own table
-- rather than another parent_type on that one - keeps this table's rows
-- physically separate from Buyer's.
CREATE TABLE IF NOT EXISTS transporter_contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transporter_id  INTEGER NOT NULL REFERENCES transporters(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    phone           TEXT,
    email           TEXT,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- COMMUNICATIONS
-- One shared table for lead, buyer and supplier communications.
-- `parent_type` + `parent_id` act as a polymorphic foreign key - this keeps
-- one CommunicationRepository usable for every parent (Liskov substitution:
-- a Lead, Buyer and Supplier are all "communicable" parents) instead of
-- near-identical tables/classes per type. Scoped transitively via the
-- parent's own company_id - no company_id column here.
-- ============================================================
CREATE TABLE IF NOT EXISTS communications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type     TEXT NOT NULL CHECK (parent_type IN ('lead', 'buyer', 'supplier')),
    parent_id       INTEGER NOT NULL,
    employee_id     INTEGER NOT NULL REFERENCES users(id),
    comm_date       TEXT NOT NULL,              -- date/time of the communication
    mode            TEXT NOT NULL,              -- whatsapp, wechat, call, email, in_person, other
    description     TEXT NOT NULL,              -- what was discussed
    follow_up_date  TEXT,                       -- optional
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- PAYMENT HISTORY (buyer/supplier only)
-- `parent_type` + `parent_id` is the same polymorphic pattern as
-- `communications` - buyers/suppliers each have their own id space, so a
-- plain client_id would be ambiguous once more than one type has data.
-- ============================================================
CREATE TABLE IF NOT EXISTS payment_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type         TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier')),
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
-- DOCUMENTS (buyer/supplier only) - metadata for now; future plan will move
-- this to its own dedicated database once file storage is introduced. Same
-- parent_type/parent_id pattern as payment_history above.
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_type     TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier')),
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
    branch_code     TEXT,       -- IEC branch code, printed on the Export Invoice annexure (section 2B)
    logo_path       TEXT,       -- company logo, relative to static/ (shown in the app sidebar and on generated documents)
    self_sealing_declaration TEXT,  -- standard self-sealing declaration text, printed on the Export Invoice
    government_schemes TEXT,   -- government schemes text; default for the Export Annexure's section 13 and printed as a heading on the Export Invoice
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS our_company_lut_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    our_company_id  INTEGER NOT NULL REFERENCES our_company(id) ON DELETE CASCADE,
    lut_number      TEXT NOT NULL,
    financial_year  TEXT NOT NULL,
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS our_company_rcmc_details (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    our_company_id         INTEGER NOT NULL REFERENCES our_company(id) ON DELETE CASCADE,
    registration_number    TEXT NOT NULL,
    registration_date      TEXT NOT NULL,
    valid_until            TEXT NOT NULL,
    organisation_name      TEXT,
    organisation_address   TEXT,
    contact_number         TEXT,
    email_address          TEXT,
    is_primary             INTEGER NOT NULL DEFAULT 0
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
    designation     TEXT,       -- e.g. "Partner Of Aayu Exim"; printed under the Authorised Signatory
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
-- MISCELLANEOUS DROP LISTS  (Administration -> Miscellaneous: the option
-- lists an admin maintains by hand instead of them being hard-coded.
-- CURRENCY fills every currency dropdown (payment history, export
-- invoice); NATURE OF CONTRACT fills the delivery-terms dropdowns that
-- are worded differently per document - "Nature of contract" on an export
-- invoice, "Shipping terms" on a quotation, "Terms of delivery" on a
-- proforma invoice.)
-- ============================================================
CREATE TABLE IF NOT EXISTS misc_currencies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- "name of currency", e.g. USD / US Dollar
    symbol       TEXT NOT NULL,   -- "currency symbol", e.g. $
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_currencies_company ON misc_currencies(company_id);

CREATE TABLE IF NOT EXISTS misc_nature_of_contracts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- e.g. CIF - BEIRA / FOB
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_noc_company ON misc_nature_of_contracts(company_id);

CREATE TABLE IF NOT EXISTS misc_ports_of_loading (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- "Port of Loading", e.g. MUNDRA
    pin_code     TEXT NOT NULL,   -- "Port of loading Pincode", e.g. 370421
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_pol_company ON misc_ports_of_loading(company_id);

CREATE TABLE IF NOT EXISTS misc_container_types (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- "Container type", e.g. 20FT FCL
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_container_types_company ON misc_container_types(company_id);

CREATE TABLE IF NOT EXISTS misc_hsn_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- "HSN CODE", e.g. 69072100
    related_products TEXT,        -- "Related to Products" - what the code covers, e.g. GLAZED VITRIFIED TILES (a note for whoever reads the list; optional)
    gst_slab     TEXT NOT NULL,   -- "GST SLAB" for that HSN, e.g. 18
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_hsn_codes_company ON misc_hsn_codes(company_id);

CREATE TABLE IF NOT EXISTS misc_countries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- "Country Name", e.g. UNITED ARAB EMIRATES
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_countries_company ON misc_countries(company_id);

CREATE TABLE IF NOT EXISTS misc_units (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES tenants(id),
    name         TEXT NOT NULL,   -- "Unit", e.g. SQM
    meaning      TEXT NOT NULL,   -- "Meaning", e.g. Square Meter
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_misc_units_company ON misc_units(company_id);

-- ============================================================
-- PERMITS  (the "permissions" a company holds, managed under the "Our
-- Company" area. Each permit records a stuffing-place name + place of
-- stuffing, the issuing authority, is either valid until an expiry date OR
-- a one-time permit, and can carry an uploaded PDF.)
-- ============================================================
CREATE TABLE IF NOT EXISTS permits (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id                INTEGER NOT NULL REFERENCES tenants(id),
    stuffing_place_name       TEXT,
    place_of_stuffing         TEXT,
    permission_number         TEXT NOT NULL,
    date_of_issue             TEXT,
    issuing_authority         TEXT,
    issuing_authority_address TEXT,
    validity_type             TEXT NOT NULL DEFAULT 'expiry' CHECK (validity_type IN ('expiry', 'one_time')),
    date_of_expiry            TEXT,       -- set only when validity_type = 'expiry'
    pdf_path                  TEXT,       -- uploaded permit PDF, relative to static/
    created_by                INTEGER NOT NULL REFERENCES users(id),
    created_at                TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_permits_company ON permits(company_id);

-- ============================================================
-- BOOKING DETAILS  (a standalone shipping-booking log under Master Data,
-- with the same field shape as an Export Invoice's own "Container details"
-- card - booking no./vessel/voyage, one transporter for the whole booking,
-- the container type/count list, and one row per physical container - but
-- not tied to any invoice, so a booking can be logged on its own.)
-- ============================================================
CREATE TABLE IF NOT EXISTS booking_details (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES tenants(id),
    buyer_id            INTEGER NOT NULL REFERENCES buyers(id),
    booking_no          TEXT,
    vessel_name         TEXT,
    voyage_no           TEXT,
    transporter_name    TEXT,       -- one transporter for every container below, same idea as ExportInvoice's
    created_by          INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_booking_details_company ON booking_details(company_id);
CREATE INDEX IF NOT EXISTS idx_booking_details_buyer ON booking_details(buyer_id);

-- Container type/count list, e.g. "2 x 20FT FCL" - same shape as
-- export_invoice_containers.
CREATE TABLE IF NOT EXISTS booking_detail_containers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_detail_id   INTEGER NOT NULL REFERENCES booking_details(id) ON DELETE CASCADE,
    sr_no               INTEGER NOT NULL,
    container_type      TEXT NOT NULL,
    container_count     INTEGER NOT NULL DEFAULT 0
);

-- One row per physical container - same field shape as an export invoice's
-- own section-11B table (minus the fields that only ever come from a later
-- process outside a booking, like gross/net weight and the VGM/E-seal pair).
CREATE TABLE IF NOT EXISTS booking_detail_container_details (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_detail_id     INTEGER NOT NULL REFERENCES booking_details(id) ON DELETE CASCADE,
    sr_no                 INTEGER NOT NULL,
    container_type        TEXT,
    container_no          TEXT,
    max_permitted_weight  TEXT,
    tare_weight_kg        REAL,
    vehicle_no            TEXT,
    lr_no                 TEXT,
    line_seal_no          TEXT,
    rfid_seal_no          TEXT
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
    weight_kg           REAL,
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
    final_destination       TEXT,
    packing_details         TEXT,
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
    -- Unused (kept so an old quotation's row still loads): quotations no
    -- longer have an FOB-typed-price mode - the typed price is always the
    -- absolute FOB price. See Quotation.cif_value_usd / cif_adjust_usd below.
    fob_pricing             INTEGER NOT NULL DEFAULT 0,
    round_off               REAL NOT NULL DEFAULT 0,
    -- The manual gap between what the CIF value field was typed as and what
    -- the ladder computes (goods total + charges) - see the form's
    -- subtotal-input handler and Quotation.cif_value_usd. 0 on a quotation
    -- whose CIF value was never overridden.
    cif_adjust_usd          REAL NOT NULL DEFAULT 0,
    bank_name               TEXT,
    bank_account_number     TEXT,
    bank_ifsc_code          TEXT,
    bank_swift_code         TEXT,
    bank_branch             TEXT,
    bank_address            TEXT,
    -- Currency shown on the document, picked from the Administration -> Miscellaneous
    -- list and snapshotted (name + symbol) so editing that list can't rewrite an issued sheet.
    currency_code           TEXT,
    currency_symbol         TEXT,
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
    quantity_unit       TEXT NOT NULL DEFAULT 'PCS',  -- snapshots products.quantity_unit, printed as small text after quantity_boxes
    pallets             REAL,      -- "Plts" column - same derived-from-boxes pattern as proforma_invoice_items.pallets
    quantity_value       REAL NOT NULL DEFAULT 0,
    unit                TEXT NOT NULL DEFAULT 'SQM',
    price_usd           REAL NOT NULL DEFAULT 0,   -- the absolute FOB price the user typed - never adjusted
    fob_price_usd       REAL,                      -- unused (kept so an old row still loads) - see quotations.fob_pricing
    total_usd           REAL NOT NULL DEFAULT 0
);

-- Container type/count list, e.g. "2 x 20FT FCL" - same shape as
-- booking_detail_containers/export_invoice_containers, replacing the single
-- free-text "Container" field the quotation form used to have.
CREATE TABLE IF NOT EXISTS quotation_containers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id        INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    sr_no               INTEGER NOT NULL,
    container_type      TEXT NOT NULL,
    container_count     INTEGER NOT NULL DEFAULT 0
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
    packing_details          TEXT,          -- e.g. "PALLATE" - same field as quotations.packing_details
    terms_of_delivery       TEXT,
    payment_terms           TEXT,
    remarks                 TEXT,
    sea_freight              REAL NOT NULL DEFAULT 0,
    insurance                REAL NOT NULL DEFAULT 0,
    certification             REAL NOT NULL DEFAULT 0,
    other_charges             REAL NOT NULL DEFAULT 0,
    discount_amount          REAL NOT NULL DEFAULT 0,
    -- Unused (kept so an old invoice's row still loads): proforma invoices no
    -- longer have an FOB-typed-price mode. See export_invoices.fob_pricing,
    -- which has this now.
    fob_pricing              INTEGER NOT NULL DEFAULT 0,
    round_off                REAL NOT NULL DEFAULT 0,     -- see quotations.round_off
    bank_name                TEXT,
    bank_account_number      TEXT,
    bank_ifsc_code            TEXT,
    bank_swift_code           TEXT,
    bank_branch               TEXT,
    bank_address               TEXT,
    display_mode              TEXT NOT NULL DEFAULT 'index',  -- goods layout: 'index' (numbered) | 'surface' (grouped by category + surface)
    status                    TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'confirmed'; a confirmed PI is locked for editing and reminds until every design on its packing list is placed on a linked PO
    -- Currency shown on the document, picked from the Administration -> Miscellaneous
    -- list and snapshotted (name + symbol) so editing that list can't rewrite an issued sheet.
    currency_code           TEXT,
    currency_symbol         TEXT,
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
    quantity_unit         TEXT NOT NULL DEFAULT 'PCS',  -- snapshots products.quantity_unit, printed as small text after quantity_boxes
    quantity_value         REAL NOT NULL DEFAULT 0,
    unit                  TEXT NOT NULL DEFAULT 'SQM',
    price_usd             REAL NOT NULL DEFAULT 0,   -- always the CIF price: what the sheet prints
    fob_price_usd         REAL,                      -- unused (kept so an old row still loads) - see export_invoice_items.fob_price_usd
    total_usd             REAL NOT NULL DEFAULT 0
);

-- Container type/count list, e.g. "2 x 20FT FCL" - same shape as
-- quotation_containers, replacing the single free-text "Container details"
-- field the proforma invoice form used to have.
CREATE TABLE IF NOT EXISTS proforma_invoice_containers (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    proforma_invoice_id    INTEGER NOT NULL REFERENCES proforma_invoices(id) ON DELETE CASCADE,
    sr_no                  INTEGER NOT NULL,
    container_type         TEXT NOT NULL,
    container_count        INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- PURCHASE ORDERS  (header + line items, number generated as
-- PO{YYYYMMDD}{seq-of-that-day} per company. The next document after the
-- Proforma Invoice in the client pipeline: OUR company is the BUYER and a
-- supplier is the SELLER, prices are in INR (typically ex-factory per box).
-- Can be started from an existing proforma invoice - proforma_invoice_id is
-- a "generated from" reference only, same pattern as
-- proforma_invoices.quotation_id. ONE PI CAN HAVE MANY POs: a single order is
-- normally split across several suppliers, so the PI page lists every PO
-- pointing at it and tracks which of its packing-list designs are still
-- unplaced. Tax percentages are stored; the amounts,
-- round-off and final order value are always derived from the items.
-- ============================================================
CREATE TABLE IF NOT EXISTS purchase_orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    po_number               TEXT NOT NULL,
    po_date                 TEXT NOT NULL,
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
    purchase_type           TEXT NOT NULL DEFAULT 'full_tax',   -- 'full_tax' | 'exemption'; drives the three percentages above
    tax_as_actual           INTEGER NOT NULL DEFAULT 0,   -- when set, the printed sheet skips the computed tax rows and prints "TAX AS ACTUAL" instead
    -- Currency shown on the document, picked from the Administration -> Miscellaneous
    -- list and snapshotted (name + symbol) so editing that list can't rewrite an issued sheet.
    currency_code           TEXT,
    currency_symbol         TEXT,
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
    quantity_unit       TEXT NOT NULL DEFAULT 'PCS',  -- snapshots products.quantity_unit, printed as small text after quantity_boxes
    quantity_value      REAL NOT NULL DEFAULT 0,
    unit                TEXT NOT NULL DEFAULT 'SQM',
    price_inr           REAL NOT NULL DEFAULT 0,
    price_per           TEXT NOT NULL DEFAULT 'BOX',   -- what price_inr is per: 'BOX' or the row's unit
    total_inr           REAL NOT NULL DEFAULT 0,
    design_id           INTEGER REFERENCES designs(id) ON DELETE SET NULL,   -- optional: which catalog design this line's boxes are for (feeds Inventory's per-design PO Qty)
    design_name         TEXT   -- snapshot of the chosen design's name at save time
);

-- ============================================================
-- PURCHASE INVOICES  (header + line items + vehicle numbers, number
-- generated as PINV{YYYYMMDD}{seq-of-that-day} per company. The last
-- document in the pipeline: raised once a supplier's goods (against one of
-- our purchase orders) actually arrive, carrying the supplier's own
-- invoice/transport details. purchase_order_id is a "generated from"
-- reference only, same pattern as purchase_orders.proforma_invoice_id -
-- one supplier, one PO, one purchase invoice. Unlike every other document
-- type there is nothing to print here: the supplier already sent their own
-- invoice as a PDF (supplier_pdf_path), we just record its numbers
-- alongside it. invoice_number/invoice_date are the SUPPLIER's own values
-- as printed on that PDF - purchase_invoice_number is our own internal,
-- auto-generated identifier (kept for consistency with every other
-- document type's numbering/version-history machinery). Discount/
-- insurance/freight/tax/round-off are typed in directly from the supplier's
-- invoice rather than derived, since they must match what the supplier
-- actually charged, not what our own tax rules would compute.
-- ============================================================
CREATE TABLE IF NOT EXISTS purchase_invoices (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    purchase_invoice_number TEXT NOT NULL,
    invoice_number          TEXT NOT NULL,   -- the supplier's own invoice number, printed on their PDF
    invoice_date            TEXT NOT NULL,
    purchase_order_id       INTEGER REFERENCES purchase_orders(id),   -- optional, "generated from" reference only
    lead_id                 INTEGER REFERENCES leads(id),             -- optional, prefill/reference only
    seller_supplier_id      INTEGER REFERENCES suppliers(id),
    seller_name             TEXT NOT NULL,
    seller_address          TEXT,
    seller_pan              TEXT,
    seller_gstin            TEXT,
    seller_ref_no           TEXT,
    port_of_loading         TEXT,
    port_of_discharge       TEXT,
    container_details       TEXT,
    transporter_name        TEXT,
    epcg_number             TEXT,
    epcg_date               TEXT,
    supplier_pdf_path       TEXT,   -- the supplier's own Purchase Invoice PDF, relative to static/
    discount_amount         REAL NOT NULL DEFAULT 0,
    insurance_other         REAL NOT NULL DEFAULT 0,
    freight                 REAL NOT NULL DEFAULT 0,
    igst_amount             REAL NOT NULL DEFAULT 0,
    cgst_amount             REAL NOT NULL DEFAULT 0,
    sgst_amount             REAL NOT NULL DEFAULT 0,
    round_off               REAL NOT NULL DEFAULT 0,
    purchase_type           TEXT NOT NULL DEFAULT 'full_tax',   -- 'full_tax' | 'exemption', same list as purchase_orders.purchase_type - typed here, not derived
    remarks                 TEXT,
    -- Currency shown on the document, picked from the Administration -> Miscellaneous
    -- list and snapshotted (name + symbol) so editing that list can't rewrite an issued sheet.
    currency_code           TEXT,
    currency_symbol         TEXT,
    created_by              INTEGER NOT NULL REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, purchase_invoice_number)
);

CREATE TABLE IF NOT EXISTS purchase_invoice_items (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_invoice_id    INTEGER NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
    sr_no                  INTEGER NOT NULL,
    product_id             INTEGER REFERENCES products(id) ON DELETE SET NULL,   -- optional, just for prefill/reference
    product_name           TEXT NOT NULL,
    hsn_code               TEXT,
    quantity_boxes         REAL,
    quantity_value         REAL NOT NULL DEFAULT 0,
    unit                   TEXT NOT NULL DEFAULT 'SQM',
    price_inr              REAL NOT NULL DEFAULT 0,
    price_per              TEXT NOT NULL DEFAULT 'BOX',
    total_inr              REAL NOT NULL DEFAULT 0,
    -- Which purchase order (of possibly several on the same purchase invoice)
    -- this line was prefilled from; NULL for a row typed in by hand with no
    -- PO origin. Drives both the "grouped by purchase order" product table
    -- and the outstanding-quantity check that decides whether a PO still
    -- needs invoicing (see PurchaseInvoiceRepository.invoiced_totals_for_purchase_order).
    purchase_order_id      INTEGER REFERENCES purchase_orders(id) ON DELETE SET NULL
);

-- The many-to-many between a purchase invoice and the purchase orders it was
-- raised against - a supplier's shipment can cover more than one of our
-- purchase orders at once. purchase_invoices.purchase_order_id (above) still
-- holds the first/primary one for the older single-PO call sites; this table
-- is the authoritative full list.
CREATE TABLE IF NOT EXISTS purchase_invoice_purchase_order_links (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_invoice_id   INTEGER NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
    purchase_order_id     INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    UNIQUE (purchase_invoice_id, purchase_order_id)
);

-- Vehicle numbers are a plain repeatable list of values (a supplier's
-- shipment can arrive split across several trucks) - no other columns, so
-- no separate model class, just a sr_no-ordered list of strings.
CREATE TABLE IF NOT EXISTS purchase_invoice_vehicles (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_invoice_id    INTEGER NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
    sr_no                  INTEGER NOT NULL,
    vehicle_number         TEXT NOT NULL
);

-- ============================================================
-- EXPORT INVOICES  (header + line items + several child lists, number
-- generated as EXPINV{YYYYMMDD}{seq-of-that-day} per company. The
-- customer/customs-facing document at the buyer end of the pipeline
-- (Quotation -> Proforma Invoice -> Purchase Order -> Purchase Invoice),
-- raised against ONE OR MORE Proforma Invoices at once - a single buyer
-- order is normally fulfilled across several PIs/suppliers, so the link is
-- many-to-many via export_invoice_proforma_links. Goods lines are prefilled
-- from the linked PIs then edited freely. Tax is computed per-product (each
-- HSN taxes differently): every line snapshots its own igst_percent, the
-- amounts are summed and shown as IGST or CGST/SGST per tax_mode. The
-- exchange rate is typed in manually and, once set, only an admin can change
-- it. EPCG number/date and the "export under" text are imported (when
-- present) by walking each linked PI's purchase orders to their purchase
-- invoices; supplier GSTIN/invoice-no rows (export_invoice_purchase_details)
-- come from the same walk for exemption purchases. There is no draft/
-- confirmed lock (always editable) but admin version history is kept.
-- ============================================================
CREATE TABLE IF NOT EXISTS export_invoices (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id                  INTEGER NOT NULL REFERENCES tenants(id),
    export_invoice_number       TEXT NOT NULL,
    invoice_date                TEXT NOT NULL,
    lead_id                     INTEGER REFERENCES leads(id),   -- optional, prefill/reference only
    consignee_name              TEXT NOT NULL,
    consignee_address           TEXT,
    notify_name                 TEXT,          -- "Buyer if other than consignee"
    notify_address              TEXT,
    country_of_origin           TEXT DEFAULT 'INDIA',
    country_of_destination      TEXT,
    place_of_receipt            TEXT,
    pre_carriage_by             TEXT,
    port_of_loading             TEXT,
    port_of_discharge           TEXT,
    final_destination           TEXT,
    nature_of_contract          TEXT,          -- e.g. "CNF- (Beira)"
    payment_terms               TEXT,
    buyer_order_no               TEXT,
    buyer_order_date             TEXT,
    export_under                TEXT,          -- the government-scheme line of the Export Under block; blank means "use our_company.government_schemes as it stands today". The other lines of that block (the SUPPLY MEANT FOR EXPORT heading, the EPCG licence, the LUT number) are derived at print time, never stored here
    epcg_number                 TEXT,          -- imported from the chain's purchase invoices when present, editable
    epcg_date                   TEXT,
    loading_type                TEXT NOT NULL DEFAULT 'self_sealing',  -- 'buffer' | 'self_sealing'
    tax_mode                    TEXT NOT NULL DEFAULT 'igst',          -- 'igst' | 'cgst_sgst'; where the summed per-product tax lands
    exchange_rate               REAL NOT NULL DEFAULT 0,               -- USD->INR, manual; only an admin may change once set
    sea_freight                 REAL NOT NULL DEFAULT 0,
    insurance                   REAL NOT NULL DEFAULT 0,
    certification               REAL NOT NULL DEFAULT 0,
    other_charges               REAL NOT NULL DEFAULT 0,
    discount_amount             REAL NOT NULL DEFAULT 0,
    fob_pricing                 INTEGER NOT NULL DEFAULT 0,  -- unused; kept so old rows still load. Always written 0 - the typed price is always FOB
    round_off                   REAL NOT NULL DEFAULT 0,     -- see quotations.round_off
    fob_value                   REAL NOT NULL DEFAULT 0,
    cnf_value                   REAL NOT NULL DEFAULT 0,
    bank_name                   TEXT,
    bank_account_number         TEXT,
    bank_ifsc_code              TEXT,
    bank_swift_code             TEXT,
    bank_branch                 TEXT,
    bank_address                TEXT,
    authorised_person_name          TEXT,      -- snapshot of the chosen Our-Company contact person
    authorised_person_designation   TEXT,
    self_sealing_declaration    TEXT,          -- snapshot of the Our-Company declaration at save time
    shipping_bill_pdf_path      TEXT,          -- optional uploaded shipping bill, relative to static/
    -- page-2 Self-Sealing Examination Report annexure fields
    examination_date            TEXT,          -- defaults to the creation date
    location_code_08b           TEXT,          -- section 08B, free text
    booking_no                  TEXT,          -- shipping line booking number, printed above the 11B container table
    vessel_name                 TEXT,          -- vessel or flight name, printed together with voyage_no in the "Vessel / Flight Name & No" cell of both sheets
    voyage_no                   TEXT,          -- voyage number, printed alongside vessel_name in the same cell
    -- The four columns the Tax Invoice attachment owns. All are typed on that
    -- document's own edit form, never on the export invoice form, so they are
    -- written by ExportInvoiceRepository.update_tax_invoice_details rather
    -- than the shared header tuple. tax_invoice_number/_date fall back to the
    -- export invoice's own number/date while blank, which is how every tax
    -- invoice starts out.
    eway_bill_no                TEXT,          -- e-way bill number + date, printed on the Tax Invoice only
    eway_bill_date              TEXT,
    tax_invoice_number          TEXT,
    tax_invoice_date            TEXT,
    -- The VGM declaration's manual-entry cells (the shaded rows of the
    -- reference). Everything else on that sheet is derived from this invoice,
    -- its containers or Our Company. Typed on the VGM declaration's own edit
    -- form, so they too bypass the shared header tuple. Blank means "use the
    -- default" - see the ExportInvoice.vgm_* properties.
    vgm_signatory               TEXT,          -- name & designation of the official signing
    vgm_contact_24x7            TEXT,          -- 24x7 contact for that official
    vgm_weighing_method         TEXT,          -- Method-1 / Method-2
    vgm_cargo_type              TEXT,          -- Normal / Reefer / Hazardous / Others
    vgm_hazardous_details       TEXT,          -- UN No, IMDG Class when hazardous
    -- The commercial invoice packing list's typed cells.
    bill_of_lading_no           TEXT,
    bill_of_lading_date         TEXT,
    bill_of_lading_pdf_path     TEXT,          -- uploaded copy of the actual bill of lading (uploads/export_invoices/...)
    issuing_authority           TEXT,
    issuing_authority_address   TEXT,
    permission_no               TEXT,
    permission_date             TEXT,
    permission_expiry           TEXT,
    permission_is_one_time      INTEGER NOT NULL DEFAULT 0,  -- 1 when the chosen permit has no expiry (validity_type = 'one_time'); printed as "One Time" instead of the (blank) expiry date
    manufacturer_name           TEXT,
    manufacturer_address        TEXT,
    stuffing_location           TEXT,          -- "Stuff At" address, printed on the export packing list
    remarks                     TEXT,
    total_net_weight_kg         REAL,          -- invoice-level totals printed on the front page (typed, not summed from containers)
    total_gross_weight_kg       REAL,
    shipping_bill_no            TEXT,
    shipping_bill_date          TEXT,          -- Annexure-C header: Shipping Bill Date
    currency_code               TEXT,          -- snapshot of the misc_currencies row picked on the form
    currency_symbol             TEXT,          -- (name + symbol, so a later edit of the list can't rewrite a printed invoice)
    created_by                  INTEGER NOT NULL REFERENCES users(id),
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, export_invoice_number)
);

CREATE TABLE IF NOT EXISTS export_invoice_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    export_invoice_id     INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    sr_no                 INTEGER NOT NULL,
    product_id            INTEGER REFERENCES products(id) ON DELETE SET NULL,   -- optional, just for prefill/reference
    product_name          TEXT NOT NULL,
    dimension_mm          TEXT,
    hsn_code              TEXT,
    surface               TEXT,
    pallets               REAL,
    quantity_boxes        REAL,
    quantity_unit         TEXT NOT NULL DEFAULT 'PCS',  -- snapshots products.quantity_unit, printed as small text after quantity_boxes
    quantity_value        REAL NOT NULL DEFAULT 0,
    unit                  TEXT NOT NULL DEFAULT 'SQM',
    price_usd             REAL NOT NULL DEFAULT 0,   -- always the CIF price: what the sheet prints
    fob_price_usd         REAL,                      -- the price as TYPED under fob_pricing (NULL otherwise)
    total_usd             REAL NOT NULL DEFAULT 0,
    igst_percent          REAL NOT NULL DEFAULT 0,  -- snapshot of the product's IGST %, so tax is per-product and stable
    -- The weight_kg of whichever named pallet type is currently selected on
    -- this line (product_pallet_types.weight_kg), snapshotted the moment a
    -- type is picked - NULL for Loose/Manual/no product. Feeds the Export
    -- Packing List's container-split Gross (KG) = Net (KG) + Plts x this,
    -- falling back to the product's own per-box gross weight when unset.
    pallet_weight_kg      REAL
);

-- The many-to-many between an export invoice and the proforma invoices it
-- references (one buyer order fulfilled across several PIs/suppliers).
CREATE TABLE IF NOT EXISTS export_invoice_proforma_links (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    export_invoice_id     INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    proforma_invoice_id   INTEGER NOT NULL REFERENCES proforma_invoices(id) ON DELETE CASCADE,
    UNIQUE (export_invoice_id, proforma_invoice_id)
);

-- Front-page Container Details: a list of {type, count} (e.g. 9 x 20FT FCL).
-- The sum of the counts drives how many section-11B rows are captured.
CREATE TABLE IF NOT EXISTS export_invoice_containers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    export_invoice_id     INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    sr_no                 INTEGER NOT NULL,
    container_type        TEXT NOT NULL,       -- e.g. '20FT FCL' / '40FT FCL'
    container_count       INTEGER NOT NULL DEFAULT 0
);

-- Page-2 section 11B: one row per PHYSICAL container. gross_weight/net_weight
-- have no form input (unlike tare_weight_kg) - they only ever hold whatever is
-- already stored on the row, e.g. set by a later process outside this form.
CREATE TABLE IF NOT EXISTS export_invoice_container_details (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    export_invoice_id     INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    sr_no                 INTEGER NOT NULL,
    container_type        TEXT,
    container_no          TEXT,
    line_seal_no          TEXT,
    rfid_seal_no          TEXT,
    vehicle_no            TEXT,
    lr_no                 TEXT,          -- lorry receipt / consignment note number for that container's road leg
    transporter_name      TEXT,          -- snapshot of the chosen transporters.name, so renaming/deleting a transporter can't rewrite a saved invoice
    max_permitted_weight  TEXT,
    tare_weight_kg        REAL,
    gross_weight          TEXT,
    net_weight            TEXT,
    -- Typed on the VGM attachment, which is a row per physical container:
    -- everything else on that sheet is derived, these two are weighed facts.
    -- Like gross_weight/net_weight they have no input on the export invoice
    -- form, so ExportInvoiceService.update carries them forward by row
    -- position rather than letting a re-save of that form blank them.
    weighbridge_name      TEXT,
    weighing_slip_no      TEXT,
    -- Typed on the E-Seal sheet, the other per-container document: when this
    -- container's e-seal was applied. Carried forward on an export invoice
    -- save the same way the weighbridge pair is.
    sealing_time          TEXT,          -- HH:mm
    sealing_date          TEXT           -- yyyy-mm-dd, printed dd/mm/yyyy
);

-- Purchase Details: supplier name + GSTIN + invoice-no rows imported from
-- the exemption purchases in the chain, editable, and spilling past 4 in
-- print. supplier_name is the seller as that purchase invoice recorded it,
-- snapshotted at import time so renaming the supplier later can't rewrite
-- an already-issued export invoice.
CREATE TABLE IF NOT EXISTS export_invoice_purchase_details (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    export_invoice_id        INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    sr_no                    INTEGER NOT NULL,
    supplier_gstin           TEXT,
    supplier_invoice_no      TEXT,
    supplier_name            TEXT,
    -- The purchase invoice's own "Purchase under" (full_tax | exemption),
    -- snapshotted like the rest of the row. Drives the printed sheet's
    -- "Concessional Purchase & EPCG details" block, which only lists the exemption
    -- rows and only when the invoice itself is raised under LUT.
    purchase_type            TEXT NOT NULL DEFAULT 'full_tax',
    -- That purchase invoice's own EPCG licence no./date (if any), shown
    -- alongside the row for reference - display only, does not drive the
    -- export invoice's own EPCG line (ExportInvoiceService._resolve_epcg
    -- still picks the first match across the whole chain independently).
    epcg_number               TEXT,
    epcg_date                 TEXT
);

-- One row per (goods line's product, purchase order) it was summed from -
-- a goods line is one aggregated row per product on the invoice, but the
-- purchase invoice(s) behind it can split that product's boxes across
-- several purchase orders (see ExportInvoiceService.build_prefill_from_proformas).
CREATE TABLE IF NOT EXISTS export_invoice_product_sources (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    export_invoice_id        INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    sr_no                    INTEGER NOT NULL,
    product_name             TEXT NOT NULL,
    po_number                TEXT NOT NULL,
    quantity_boxes           REAL NOT NULL DEFAULT 0
);

-- ============================================================
-- EXPORT PACKING LISTS  (header + allocation lines, number generated as
-- EXPPL{YYYYMMDD}{seq-of-that-day} per company. The customs-facing EXPORT
-- PACKING LIST that always accompanies an Export Invoice - exactly one per
-- export invoice, generated automatically whenever that invoice is saved,
-- never created or edited on its own. Its whole header (consigner,
-- consignee, ports, bank, declarations, EPCG, self-sealing block) is read
-- live off the parent export invoice; the only thing it stores is HOW the
-- invoice's goods were split across the physical containers.
--
-- One row per (container, goods line) allocation: the container identity is
-- SNAPSHOTTED from the invoice's own section-11B container rows (so the
-- printed sheet stays stable even if 11B is later re-ordered), and the
-- quantity columns are all derived from `quantity_boxes` - the number of
-- boxes of that goods line loaded into that container. The invariant the
-- service enforces is that, per goods line, the boxes allocated across all
-- containers add up to EXACTLY the boxes on the export invoice: no box
-- double-loaded, none left behind.
--
-- `group_label` is the HSN heading the printed sheet groups rows under
-- (e.g. "CERAMIC GLAZED VITRIFIED TILES" above the 69072100 lines). It is
-- derived automatically from the line's catalog product (its category, else
-- the product name) and snapshotted here; the sheet emits a heading row
-- whenever (group_label, hsn_code) changes from the previous printed row,
-- which is what produces the grouped-by-HSN layout.
-- ============================================================
CREATE TABLE IF NOT EXISTS export_packing_lists (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id               INTEGER NOT NULL REFERENCES tenants(id),
    export_invoice_id        INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    packing_list_number      TEXT NOT NULL,
    packing_list_date        TEXT NOT NULL,
    created_by               INTEGER NOT NULL REFERENCES users(id),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (export_invoice_id),
    UNIQUE (company_id, packing_list_number)
);

CREATE TABLE IF NOT EXISTS export_packing_list_items (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    export_packing_list_id    INTEGER NOT NULL REFERENCES export_packing_lists(id) ON DELETE CASCADE,
    sr_no                     INTEGER NOT NULL,
    container_sr_no           INTEGER NOT NULL DEFAULT 1,  -- which section-11B container row this sits in
    container_no              TEXT,                        -- snapshot of that 11B row
    seal_no                   TEXT,
    rfid_seal_no              TEXT,
    invoice_item_sr_no        INTEGER,                     -- which export_invoice_items.sr_no was split
    product_id                INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name              TEXT NOT NULL,
    group_label               TEXT,                        -- HSN heading this row prints under
    hsn_code                  TEXT,
    pallets                   REAL,
    quantity_boxes            REAL,
    quantity_unit             TEXT NOT NULL DEFAULT 'PCS',
    quantity_value            REAL NOT NULL DEFAULT 0,
    unit                      TEXT NOT NULL DEFAULT 'SQM',
    net_weight_kg             REAL,
    gross_weight_kg           REAL
);

-- Splits one (container x goods line) row of the Export Packing List
-- (export_packing_list_items) further into the catalog designs its boxes
-- actually are, entered on the "Designs Packing List" page (see
-- app/routes/export_designs_packing_lists.py). Keyed on the container
-- split's own natural key (invoice_item_sr_no, container_sr_no) rather than
-- export_packing_list_items.id, because that table is wholesale deleted and
-- re-inserted every time the parent export invoice is saved
-- (ExportPackingListRepository._replace_items) - an FK to its id would lose
-- every allocation on the next invoice edit. Per line, quantity_boxes across
-- every design row must sum to exactly that line's own quantity_boxes.
-- The DESIGNS PACKING LIST document itself: the second packing list that
-- ships alongside the regular one, restating the same container split with
-- each line broken into its designs. Exactly one per export invoice, and it
-- owns nothing but its own number/date - every figure it prints comes from
-- the export packing list and the design rows below.
CREATE TABLE IF NOT EXISTS export_designs_packing_lists (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id               INTEGER NOT NULL REFERENCES tenants(id),
    export_invoice_id        INTEGER NOT NULL REFERENCES export_invoices(id) ON DELETE CASCADE,
    packing_list_number      TEXT NOT NULL,
    packing_list_date        TEXT NOT NULL,
    created_by               INTEGER NOT NULL REFERENCES users(id),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (export_invoice_id),
    UNIQUE (company_id, packing_list_number)
);

CREATE TABLE IF NOT EXISTS export_packing_list_item_designs (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    export_packing_list_id   INTEGER NOT NULL REFERENCES export_packing_lists(id) ON DELETE CASCADE,
    invoice_item_sr_no       INTEGER NOT NULL,
    container_sr_no          INTEGER NOT NULL,
    design_id                INTEGER REFERENCES designs(id) ON DELETE SET NULL,
    design_name              TEXT,
    quantity_boxes           REAL NOT NULL DEFAULT 0,
    quantity_value           REAL NOT NULL DEFAULT 0,
    unit                     TEXT
);

-- ============================================================
-- PACKING LISTS  (header + line items, number generated as
-- PL{YYYYMMDD}{seq-of-that-day} per company. Normally started from an
-- existing proforma invoice, but can also be started directly from a
-- Quotation (skipping the PI step), from a Purchase Order (the PO's own
-- PL), or from a Purchase Invoice (that invoice's own PL, importing the
-- linked PO's PL wholesale) - proforma_invoice_id/quotation_id/
-- purchase_order_id/purchase_invoice_id are all "generated from" reference
-- only, same pattern as proforma_invoices.quotation_id. Each line breaks a
-- product's quantity down into a specific DESIGN in smaller quantities.)
-- ============================================================
CREATE TABLE IF NOT EXISTS packing_lists (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id              INTEGER NOT NULL REFERENCES tenants(id),
    packing_list_number     TEXT NOT NULL,
    packing_list_date       TEXT NOT NULL,
    proforma_invoice_id     INTEGER REFERENCES proforma_invoices(id),   -- optional, "generated from" reference only
    quotation_id            INTEGER REFERENCES quotations(id),         -- optional, "generated from" reference only (skips the PI step)
    purchase_order_id       INTEGER REFERENCES purchase_orders(id),    -- optional, "generated from" reference only (the PO's own PL)
    purchase_invoice_id     INTEGER REFERENCES purchase_invoices(id),  -- optional, "generated from" reference only (the Purchase Invoice's own PL)
    job_work_id             INTEGER REFERENCES job_works(id),          -- optional, "generated from" reference only (the Job Work's own PL)
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
    quantity_unit       TEXT NOT NULL DEFAULT 'PCS',  -- snapshots products.quantity_unit, printed as small text after quantity_boxes
    pcs                 REAL,                              -- PCS column on the printed sheet
    quantity_value      REAL NOT NULL DEFAULT 0,
    unit                TEXT NOT NULL DEFAULT 'SQM',
    net_weight_kg       REAL,
    gross_weight_kg     REAL
);

-- ============================================================
-- JOB WORKS  (header + design lines, number generated as
-- JW{YYYYMMDD}{seq-of-that-day} per company. The document that hands a
-- proforma invoice's goods on to be worked on: a FROM SELLER (the supplier the
-- goods come from) and a JOB MANUFACTURER (the supplier doing the job work),
-- with one line per catalog DESIGN taken off that proforma invoice.
-- proforma_invoice_id is a "generated from" reference only, same pattern as
-- purchase_orders.proforma_invoice_id - every line is snapshotted here, so a
-- later edit of the invoice can't rewrite an issued sheet. Each line carries
-- the invoice's own quantity (quantity_boxes / quantity_value, both read-only
-- on the form), the JOB QUANTITY sent out for job work, and the JOBED QTY the
-- manufacturer reports back against it.
-- ============================================================
CREATE TABLE IF NOT EXISTS job_works (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id               INTEGER NOT NULL REFERENCES tenants(id),
    job_work_number          TEXT NOT NULL,
    job_work_date            TEXT NOT NULL,
    proforma_invoice_id      INTEGER REFERENCES proforma_invoices(id) ON DELETE SET NULL,  -- optional, "generated from" reference only
    seller_supplier_id       INTEGER REFERENCES suppliers(id),   -- optional, the Supplier picked as "From Seller"
    seller_name              TEXT NOT NULL,
    seller_address           TEXT,
    seller_pan               TEXT,
    seller_gstin             TEXT,
    manufacturer_supplier_id INTEGER REFERENCES suppliers(id),   -- optional, the Supplier picked as "Job Manufacturer"
    manufacturer_name        TEXT,
    manufacturer_address     TEXT,
    manufacturer_pan         TEXT,
    manufacturer_gstin       TEXT,
    seller_ref_no            TEXT,
    delivery_time            TEXT,          -- e.g. "20 DAY FROM JOB WORK DATE"
    advance_percent          TEXT,
    payment_terms            TEXT,
    remarks                  TEXT,
    -- Currency shown on the document, snapshotted (name + symbol) exactly as
    -- purchase_orders does it.
    currency_code            TEXT,
    currency_symbol          TEXT,
    -- Products card (a copy of purchase_orders' own tax block): the rate
    -- comes from purchase_type, split into IGST or CGST+SGST by comparing
    -- our own GSTIN against manufacturer_gstin - see
    -- JobWorkService._tax_percentages. Purely a costing reference; nothing
    -- here feeds the design lines above or the printed sheet.
    igst_percent             REAL NOT NULL DEFAULT 0,
    cgst_percent             REAL NOT NULL DEFAULT 0,
    sgst_percent             REAL NOT NULL DEFAULT 0,
    purchase_type            TEXT NOT NULL DEFAULT 'full_tax',
    tax_as_actual            INTEGER NOT NULL DEFAULT 0,
    created_by               INTEGER NOT NULL REFERENCES users(id),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, job_work_number)
);

CREATE TABLE IF NOT EXISTS job_work_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_work_id        INTEGER NOT NULL REFERENCES job_works(id) ON DELETE CASCADE,
    sr_no              INTEGER NOT NULL,
    -- The SOURCE side: the proforma invoice's own product, kept only to look
    -- up source_quantity below (the invoice's packing list, matched by this
    -- product + the chosen design's name) - not shown as "the" product on the
    -- printed sheet, which describes what actually goes out (to_product).
    product_id         INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name       TEXT NOT NULL,
    -- The TARGET side: what the job work converts the design INTO (job work
    -- is normally a size change, e.g. GVT/PGVT 600X1200MM [2PCS=1.44SQM] cut
    -- down to GVT/PGVT 200X1200MM [6PCS=1.44SQM]) - this is what the printed
    -- sheet's DESCRIPTION OF GOODS actually names, and hsn_code snapshots
    -- its HSN. Picked first, in the Job Manufacturer card; its own designs
    -- are what the Design column below offers.
    to_product_id      INTEGER REFERENCES products(id) ON DELETE SET NULL,
    to_product_name    TEXT,
    hsn_code           TEXT,
    design_id          INTEGER REFERENCES designs(id) ON DELETE SET NULL,   -- a design of to_product; optional, just for prefill/reference
    design_name        TEXT,                            -- snapshot of the chosen design
    -- What every quantity below is measured in: the product's QTY unit
    -- (products.quantity_unit, e.g. BOX/PCS), taken from to_product - job
    -- work is counted, not measured by area.
    unit               TEXT NOT NULL DEFAULT 'PCS',
    -- The whole line is a chain of derived figures, computed server-side on
    -- every save and persisted (same treatment purchase_order_items.total_inr
    -- gets) rather than recomputed live, so a printed sheet never disagrees
    -- with what was actually saved:
    --   source_quantity   fetched from the proforma invoice's packing list -
    --                      this design's quantity_boxes under `product_id`,
    --                      matched by design name (0 when no match is found)
    --   conversion_value  typed; must be > 0
    --   extra_percent     typed; may be 0
    --   converted_quantity = source_quantity / conversion_value
    --   extra_quantity     = converted_quantity * extra_percent / 100
    --   job_quantity        = converted_quantity + extra_quantity  (the
    --                         document's one final figure per design - no
    --                         longer typed by hand)
    source_quantity    REAL NOT NULL DEFAULT 0,
    conversion_value   REAL NOT NULL DEFAULT 1,
    extra_percent      REAL NOT NULL DEFAULT 0,
    converted_quantity REAL NOT NULL DEFAULT 0,
    extra_quantity     REAL NOT NULL DEFAULT 0,
    job_quantity       REAL NOT NULL DEFAULT 0
);

-- Products card of a job work: a plain copy of purchase_order_items'
-- shape, one row per product picked from the Job Manufacturer -> Product
-- dropdown (the invoice's own products - NOT to_product, the design lines'
-- conversion target). Purely a costing/reference line - never printed and
-- never feeds job_work_items' derived chain.
CREATE TABLE IF NOT EXISTS job_work_products (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_work_id        INTEGER NOT NULL REFERENCES job_works(id) ON DELETE CASCADE,
    sr_no              INTEGER NOT NULL,
    product_id         INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name       TEXT NOT NULL,
    hsn_code           TEXT,
    quantity_boxes     REAL,
    quantity_unit      TEXT NOT NULL DEFAULT 'PCS',
    quantity_value     REAL NOT NULL DEFAULT 0,
    unit               TEXT NOT NULL DEFAULT 'SQM',
    price_inr          REAL NOT NULL DEFAULT 0,
    price_per          TEXT NOT NULL DEFAULT 'BOX',
    total_inr          REAL NOT NULL DEFAULT 0
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
    document_type       TEXT NOT NULL,   -- 'quotation' | 'proforma_invoice' | 'purchase_order' | 'packing_list' | 'purchase_invoice'
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
CREATE INDEX IF NOT EXISTS idx_suppliers_company ON suppliers(company_id);
CREATE INDEX IF NOT EXISTS idx_transporters_company ON transporters(company_id);
CREATE INDEX IF NOT EXISTS idx_transporter_contacts_transporter ON transporter_contacts(transporter_id);
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
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_company ON purchase_invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_created_by ON purchase_invoices(created_by);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_date ON purchase_invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_po ON purchase_invoices(purchase_order_id);
CREATE INDEX IF NOT EXISTS idx_purchase_invoice_items_pi ON purchase_invoice_items(purchase_invoice_id);
CREATE INDEX IF NOT EXISTS idx_purchase_invoice_vehicles_pi ON purchase_invoice_vehicles(purchase_invoice_id);
CREATE INDEX IF NOT EXISTS idx_pi_po_links_invoice ON purchase_invoice_purchase_order_links(purchase_invoice_id);
CREATE INDEX IF NOT EXISTS idx_pi_po_links_po ON purchase_invoice_purchase_order_links(purchase_order_id);
CREATE INDEX IF NOT EXISTS idx_export_invoices_company ON export_invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_export_invoices_created_by ON export_invoices(created_by);
CREATE INDEX IF NOT EXISTS idx_export_invoices_date ON export_invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_export_invoice_items_invoice ON export_invoice_items(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_invoice_links_invoice ON export_invoice_proforma_links(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_invoice_links_proforma ON export_invoice_proforma_links(proforma_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_invoice_containers_invoice ON export_invoice_containers(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_invoice_container_details_invoice ON export_invoice_container_details(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_invoice_purchase_details_invoice ON export_invoice_purchase_details(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_invoice_product_sources_invoice ON export_invoice_product_sources(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_packing_lists_company ON export_packing_lists(company_id);
CREATE INDEX IF NOT EXISTS idx_export_packing_lists_invoice ON export_packing_lists(export_invoice_id);
CREATE INDEX IF NOT EXISTS idx_export_packing_list_items_list ON export_packing_list_items(export_packing_list_id);
CREATE INDEX IF NOT EXISTS idx_packing_lists_company ON packing_lists(company_id);
CREATE INDEX IF NOT EXISTS idx_packing_lists_created_by ON packing_lists(created_by);
CREATE INDEX IF NOT EXISTS idx_packing_lists_date ON packing_lists(packing_list_date);
CREATE INDEX IF NOT EXISTS idx_packing_list_items_list ON packing_list_items(packing_list_id);
CREATE INDEX IF NOT EXISTS idx_job_works_company ON job_works(company_id);
CREATE INDEX IF NOT EXISTS idx_job_works_created_by ON job_works(created_by);
CREATE INDEX IF NOT EXISTS idx_job_works_date ON job_works(job_work_date);
-- idx_job_works_proforma lives in database.py's _migrate: on a pre-v82 DB the
-- proforma_invoice_id column doesn't exist yet when this script runs.
CREATE INDEX IF NOT EXISTS idx_job_work_items_job_work ON job_work_items(job_work_id);
CREATE INDEX IF NOT EXISTS idx_job_work_products_job_work ON job_work_products(job_work_id);
CREATE INDEX IF NOT EXISTS idx_document_versions_lookup ON document_versions(document_type, document_id);
