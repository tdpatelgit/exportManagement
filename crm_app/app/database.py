"""
app/database.py
----------------
The ONLY module in the app that knows this is SQLite.

Single Responsibility: open connections, run the schema, expose a small
`execute` / `query` API. Repositories depend on this class, never on
`sqlite3` directly. That indirection is what lets us swap SQLite for
PostgreSQL/MySQL later (future plan: "store all data for each document
separately on a separate database") by rewriting this one file only -
every Repository, Service and Route stays untouched (Dependency Inversion).
"""

import re
import sqlite3
import os
import shutil
from contextlib import contextmanager
from datetime import datetime


# The current shape of the database, bumped by one every time the schema is
# restructured. Stamped onto every DB via `PRAGMA user_version` at the end of
# `init_schema`, so we can tell how old any given database file is - most
# importantly, a backup an admin uploads on the Database Backup page (see
# BackupService). The rule the restore flow relies on:
#   * a DB whose version is <= SCHEMA_VERSION can be forward-migrated to the
#     current shape simply by running `init_schema` on it (the guarded,
#     idempotent migrations in `_migrate` bring it up to date);
#   * a DB whose version is > SCHEMA_VERSION was written by a NEWER build of
#     the app - we can't safely downgrade it, so restore refuses it.
#
# HOW TO EVOLVE THE SCHEMA (keeps old backups integrable):
#   1. Change schema.sql to the new shape (for fresh installs).
#   2. Add a guarded, DATA-PRESERVING step to `_migrate` that transforms an
#      already-populated older DB into the new shape - ALTER TABLE to add a
#      column, or the rename-create-copy-drop dance for constraint changes,
#      following the `PRAGMA table_info`-guarded blocks already there. Never
#      DROP rows to "start fresh": that is what makes an old backup lossy.
#   3. Increment SCHEMA_VERSION below.
# Because `_migrate` is idempotent and runs on every startup AND on every
# restore, any backup - however old - is carried forward through the whole
# chain of steps, never discarded.
SCHEMA_VERSION = 96  # v96: packing_plannings + its child tables (packing_planning_proforma_links/_items/_manual_units/_manual_contents) - the PACKING PLANNING document, which sits between the purchase order's Production Status card and Loading Planning and answers the question that comes before "which container": how many WHOLE pallets or cartons does what the supplier has actually produced make, and what is left over. Lines are loaded the way a loading plan's goods are - each selected proforma invoice traced through its purchase orders - but one hop further, to those orders' production BATCHES (purchase_order_item_batches), because a batch number and manufacturing date exist nowhere else. One row per BATCH rather than per design: a design is routinely fired in several batches on different days (ARKOSE in one of 317, ATLANTA in two of 200 and 117) and a pallet is packed out of one of them. Capacity and the PLT/CTN label come from product_pallet_types.boxes_per_pallet/unit_kind, the same lookup LoadingPlanningService.auto_build_packing already uses, so 317 BOX at 32/pallet reads 9.91 PLT and packs 9, and 45 PCS at 30/CTN reads 1.50 CTN and packs 1. The two tables the sheet prints are NOT two stored lists: only the batch rows are stored, and PACKING REMAIN BY MANUAL is derived as every row whose ready quantity did not divide exactly (317 leaves 29; 160 at 32 leaves nothing and so has no manual row at all) - storing the remainder as well would let the two halves drift apart on the next edit. as_per_pl_packing, packed_quantity, remain_quantity and the END NUMBER are all likewise derived on the model, the call LoadingPlanning.container_summary already makes. Packing numbers run continuously down the document and carry on through the manual units, so a pallet number is unique however it was packed; packing_no_start is NULL to follow on from the row above and set to PIN a row, with every row after it counting on from its end - which is what the spreadsheet this replaces could not do, and why its rows 8-10 silently reused 41-46. Every packing check is a WARNING, never a ValidationError, for the reason LoadingPlanningService states about its own: the document is worked on across sittings and a half-built plan must save. All five tables are brand new, so schema.sql creates them on old and fresh databases alike and nothing already saved changes. v95: purchase_order_item_production + purchase_order_item_batches (two new tables) - the PURCHASE ORDER PRODUCTION STATUS card, a screen-only section on the purchase order preview page (never printed, not on the combined document) that answers what the supplier has actually made against the order. Tracked BY DESIGN, which is why a row is keyed on (purchase_order_item_id, design_id, design_name) rather than on any of them alone - the name is part of the key because the packing list rows that supply the split are frequently free-typed, with no catalog design behind them, so two designs of one product would otherwise collide on a shared NULL design_id: a purchase order orders by PRODUCT, and which designs those boxes are is only ever settled on the linked proforma invoice's packing list - the same breakdown the sheet's own PACKING DETAILS block already prints (see _packing_details_rows). A line with no such split keeps a single design_id-NULL row, so a hand-typed product still gets a status. Each row carries a hand-set status (pending | in_production | ready) plus any number of BATCHES under it, each with a batch number, production date, quantity (in the line's own quantity_unit) and remarks - one design's ordered quantity is routinely fired in several batches, so the batches are child rows and their quantities roll up as a produced total shown against the ordered one. The status is deliberately NOT derived from those quantities: it is a statement about the supplier's floor, which can legitimately read Ready before every batch has been keyed in or Pending after a trial batch. The purchase order list gains a "Production status" column after "Created by" showing n/m ready, and PurchaseOrderRepository._replace_items - which deletes and re-inserts every line on each PO edit - now carries the production rows across by sr_no so editing an order does not silently cascade its production history away. Both tables are new, so every existing purchase order starts with no rows at all and reads as Pending. v94: loading_plannings + its child tables (loading_planning_proforma_links/_items/_containers/_cartons/_carton_contents/_pallets/_pallet_contents) - the LOADING PLANNING document, which sits between the purchase side and the export invoice and answers the one question nothing else in the app does: which goods physically go in which container. Goods are loaded the way the Export Invoice's own "Reference proforma invoices" card loads them, but one hop differently: each selected PI is traced through its purchase orders to THOSE ORDERS' packing lists, so the lines come out at DESIGN level (a PO orders 1268 boxes of a product; its packing list is what says those are 4 designs of 317) rather than merged to product level the way ExportInvoiceService.build_prefill_from_proformas does, priced at the PI's own quoted USD rate matched by product_id, and falling back to the PO's own product lines when a PO has no packing list. From there a human builds the packing by hand, because no rule can do it: `packing_list_items.pallets` has always been stored as boxes/box_per_pallet, a DECIMAL, and 9.91 pallets or 1.5 cartons is not a thing anyone can ship. A carton and a pallet are now real numbered objects that may hold any mix of designs and products - 317 boxes at 32/pallet is 9 full pallets plus one holding 29, and 45 + 45 PCS at 30/CTN is best packed as two full cartons plus one mixed 15+15, all three on a single pallet. The carton level is OPTIONAL (tiles sit straight on the pallet, hardware goes through cartons first), which makes one weight rule cover both: pallet gross = contents net + carton tare + pallet tare. Pallets are then assigned whole to the containers copied off a Booking Detail, and each container's VGM (pallet gross + container tare) is checked against its max permitted weight as a WARNING only - a plan may legitimately be saved half-built, unlike the export packing list's own container split, which hard-enforces its equivalent invariant. Also product_pallet_types.unit_kind (new column, default 'pallet', backfilled to 'carton' where weight_kg < 5) so a packing type says which of the two it is instead of being guessed at from its weight, and export_invoices.booking_detail_id (new nullable FK): the Export Invoice's Container details card already had a booking picker that filled its 11B table in, but it only ever stored the booking NUMBER, so the link back was lost as soon as a booking was renumbered - it now keeps the id alongside, while the 11B rows stay the snapshot they always were. All eight loading-planning tables are brand new, so schema.sql creates them on old and fresh databases alike; nothing already saved changes, and existing packing lists keep their decimal `pallets` field untouched. v93: export_invoice_job_ins (new table) - the Export Invoice form gains a read-only "Job In details" card alongside "Purchase details": when goods are loaded from the selected proforma invoices, the prefill also walks each PI -> its job works -> their purchase invoices -> job outs -> job ins, lists one row per job in (job manufacturer, our challan no, the manufacturer's return challan no/date, stock inward no/date) and merges each job in's returned designs into the Products card as one line per jobbed product, priced at the PI's quoted USD rate. Display/traceability only, mirrors export_invoice_product_sources - never prints on the sheet or annexure. No existing export invoice has any job-in row, exactly as today. v92: job_outs.transporter_name (new nullable column) - the Job Out form's Transport details card gains a "Transport name" field ahead of Transport GSTIN, and the delivery challan prints it as its own TRANSPORT NAME row. NULL on every existing job out, which falls back to the name of whichever transporter carries the typed Transport GSTIN, then to the purchase invoice's own transporter_name - so an existing challan prints a name without being re-edited. v91: products.is_job_work_product (new column, default 0) + products.master_product_id (new nullable self-referencing FK) - the product form gains a "Job Work Product" checkbox right after Net Weight per Unit (KG); ticking it reveals a "Select Master Product" dropdown listing every other product in the catalog, and the one picked is stamped as master_product_id. 0/NULL on every existing product, which shows the checkbox unticked and no master product selected. v90: products.price_usd (new nullable column) - the product form gains a Price field between the IGST%/SGST%/CGST% row and the Unit row, holding the product's own price. NULL on every existing product, which shows as blank. v89: purchase_invoices.job_work_id + purchase_invoice_items.job_work_id (new nullable FKs) + purchase_invoice_job_work_links (new table) - a Job Work now prints/numbers as a purchase order (see job_works.job_work_number), so it can be a purchase invoice's own "generated from" origin the same way a real purchase order already can: the Purchase Invoice form's "Start from" picker gains a second, parallel list of outstanding job works (matched against the chosen supplier as the Job Manufacturer, not the From Seller), and a line prefilled from one is tagged the same way a line prefilled from a purchase order already is. NULL/empty on every existing purchase invoice, which simply has no job-work origin, exactly as today.
# v88: packing_lists.job_work_id (new nullable FK) - a packing list can now be generated directly from a Job Work, same "generated from" reference pattern as proforma_invoice_id/quotation_id/purchase_order_id/purchase_invoice_id. NULL on every existing packing list, which is exactly what one already had. v87: job_works.igst_percent/cgst_percent/sgst_percent/purchase_type/tax_as_actual (new columns) + job_work_products (new table) - the Job Manufacturer card gains a "Products" line-items section, a copy of the Purchase Order form's own Products card: one row per product (Sr/Product/HSN code/Boxes/Qty/Unit/Price/Per/Total), a "Tax as Actual" checkbox, a "Purchase under" (full_tax | exemption) dropdown, and derived IGST/CGST/SGST percentages (same rate-from-purchase_type, intra/inter-state-from-GSTIN logic PurchaseOrderService already runs, based on the Job Manufacturer's own GSTIN rather than the seller's). A row's Product prefills from the Job Manufacturer card's "Product" picker (the invoice's own products), not "To Product" (the conversion target the design lines use) - the two product concepts are unrelated, this one is purely a costing/reference line. Nothing here prints on the job work sheet or feeds the design/Job Quantity chain; it exists only to be saved and reopened, same treatment purchase_order_items.total_inr gets. 0/'full_tax' on every existing job work, and no products, which is exactly what an old job work already had (nothing to migrate away from). v86: job_work_items rework - the whole per-design calculation flips. Job Quantity is no longer typed by hand; it is derived from a chain of new columns computed server-side and persisted: source_quantity (this design's quantity_boxes fetched off the proforma invoice's packing list, matched by product_id + design name), conversion_value (typed, must be > 0), extra_percent (typed), then converted_quantity = source_quantity / conversion_value, extra_quantity = converted_quantity * extra_percent / 100, and job_quantity = converted_quantity + extra_quantity. jobed_quantity/jobed_unit (the manufacturer- reported figure) are DROPPED - job_quantity is now the document's one final figure per design, so there is no separate reported-back number to reconcile. product_id/product_name flip roles: they now name the SOURCE proforma invoice product (kept only to look up source_quantity), while to_product_id/to_product_name - previously an optional size-conversion target - becomes what the printed sheet actually names as the goods (hsn_code now snapshots FROM to_product too), and design_id/design_name is chosen from to_product's own catalog designs rather than the invoice's. The Job Manufacturer card gains the whole product-and-design picker that used to sit under From Seller (To Product first, driving the design list; Product second, driving the source_quantity lookup) - From Seller keeps only its party fields. Existing rows are forward-migrated on a best-effort basis: conversion_value=1, extra_percent=0, source_quantity=job_quantity (so converted_quantity/extra_quantity/job_quantity stay internally consistent with the OLD job_quantity a user already typed), and to_product/product are left exactly as they were (a size-converted line already had a real to_product; a line with none keeps none, which now shows as an unfilled To Product rather than "comes back as the same product"). The dropped jobed_quantity (what the manufacturer had reported back) is not carried anywhere - that concept no longer exists in this document. v85: job_work_items.unit/jobed_unit now hold the product's QTY unit (products.quantity_unit, e.g. BOX/PCS) instead of its ALTERNATE-quantity unit (products.alternate_quantity_unit, e.g. SQM). Job Quantity and Jobed Qty are counts of what physically goes to the manufacturer and comes back, so they belong in the same unit the product is counted in, not in the area unit the invoice prices by. The From Seller card takes each design's unit from its packing-list line's quantity_unit, and the Job Manufacturer card takes Jobed Qty's from the To Product's own quantity_unit. Existing rows are backfilled once from the catalog (each line's own product, and its To Product where one was chosen), so a job work saved before this stops labelling counted quantities as SQM; a line whose product has since left the catalog keeps whatever it had. v84: job_work_items.quantity_boxes/quantity_unit/quantity_value DROPPED - a job work no longer carries the proforma invoice's own quantity for the design alongside its Job Quantity. It never reconciled against it (v81 already removed the only comparison there was), and carrying it implied a draw-down relationship that doesn't exist: a job work is a fresh instruction to a manufacturer, so what some other document happens to have ordered of that design is beside the point. The invoice's packing list now supplies the product and design LIST only. The From Seller card follows suit - the read-only Qty/Alt qty cells are gone from its design rows, from the Job work lines table and from the printed sheet, leaving design + Job Quantity; designs are added a row at a time with "+ Add design" rather than the whole product's set being listed at once. `unit` stays, as what Job Quantity is measured in. The dropped values were only ever a display echo of the packing list they came from, so nothing is lost that the packing list doesn't still hold. v83: job_works.seller_buyer_id reverts to seller_supplier_id - the FROM SELLER on a job work picks from SUPPLIERS after all, not from buyers (v82 briefly had it the other way round). Both parties on the sheet are therefore suppliers again: the From Seller whose goods go out, and the Job Manufacturer who does the work. A pre-v82 database still holds its original seller_supplier_id and carries it straight across untouched; a database that actually reached v82 has an all-NULL seller_buyer_id (v82 never backfilled it), which lands as an unselected dropdown with the row's snapshotted seller_name/address/PAN/GSTIN unchanged, so nothing already printed moves. The rebuild that does this is the same one v82 introduced, now written to converge on the FINAL shape from either starting point rather than one version at a time. v82: job_works is repointed from the PURCHASE ORDER to the PROFORMA INVOICE - purchase_order_id becomes proforma_invoice_id, and seller_supplier_id becomes seller_buyer_id. Job work is raised against the invoice the goods are being made for, not against one of the several purchase orders placed under it, so the "Start from" picker, the design rows and the new-job-work button all move up one level: the button now lives on the proforma invoice's own preview toolbar (beside its purchase orders), the designs are read off the INVOICE's packing list, and the From Seller card picks from BUYERS rather than suppliers (the Job Manufacturer card still picks a supplier). Existing job works are forward-migrated rather than orphaned: each one's proforma_invoice_id is taken from the purchase order it was raised against, so a job work placed under PO -> PI 26 now points straight at PI 26. seller_buyer_id is deliberately NOT backfilled - a supplier id is not a buyer id - which shows as a From Seller with nothing selected in the dropdown; the snapshotted seller_name/address/PAN/GSTIN on the row are untouched, so nothing already printed changes. Rebuilt with the create-copy-drop dance rather than ALTER, so a migrated table ends up with exactly schema.sql's foreign keys (including ON DELETE SET NULL on the invoice). v81: job_work_items.to_product_id/to_product_name/jobed_unit (new nullable columns) - job work is normally a SIZE CHANGE, so the goods come back as a different catalog product than they went out as (e.g. GVT/PGVT 600X1200MM [2PCS=1.44SQM] cut down to GVT/PGVT 200X1200MM [6PCS=1.44SQM]). The Job Manufacturer card's Jobed quantities block therefore gains a "To Product" dropdown beside its Product dropdown, listing the whole catalog rather than just the purchase order's own products, and stamping the chosen target onto every line of the source product being converted (snapshotted by name, same treatment product_name gets). Jobed Qty gains its own unit dropdown beside it, since the converted product is often measured differently from the one that went out - it defaults to the target product's own alternate quantity unit and falls back to the line's own unit. All three are NULL on existing lines, which reads as "comes back as the product it went out as, in that line's own unit". v80: job_works + job_work_items (new tables) - the JOB WORK document: a purchase order's goods handed on to be worked on, with a FROM SELLER (the supplier the goods come from) and a JOB MANUFACTURER (whoever does the job work) on the same sheet, and one line per catalog DESIGN taken off that purchase order. The designs, their boxes and their alternate quantity are read-only on the form - they come from the purchase order's own packing list (its linked proforma invoice's packing list as a fallback, the same source the PO's printed PACKING DETAILS block reads), since a PO only ever orders by product and designs are chosen one level down. The two figures the document owns are JOB QUANTITY (typed per design in the From Seller card, then "Add to Job Work") and JOBED QTY (typed against those same lines in the Job Manufacturer card). purchase_order_id is a "generated from" reference only and is ON DELETE SET NULL, so deleting a PO leaves its job works standing as independent records with their own snapshotted lines. Both tables are brand new, so schema.sql creates them on fresh installs; guarded here too so an older database gets them. v79: export_designs_packing_lists (new table) - the DESIGNS PACKING LIST becomes a document in its own right rather than just a data-entry screen: it gets its own DSGPL number and date, assigned once when "Create designs packing list" is clicked and kept thereafter (the same treatment export_packing_lists gives its EXPPL number), plus a printable sheet restating the container split with every line broken into its designs - the second packing list that ships alongside the regular one. The allocation rows themselves stay in export_packing_list_item_designs (v78), keyed off the export packing list; this table only carries the document's identity, so an invoice whose designs are still being filled in simply has no row here yet. v78: export_invoice_items.design_id/design_name DROPPED, replaced by a new export_packing_list_item_designs table (Sale Qty is now tracked at the Export Packing List's container-split level, not the export invoice item level - a line's boxes are often a mix of designs split across containers, which a single per-line design tag couldn't represent). The "Designs Packing List" page (app/routes/export_designs_packing_lists.py) lets each (container, goods line) row of the container split be broken into design + qty rows, keyed on that row's own natural key (invoice_item_sr_no, container_sr_no) rather than export_packing_list_items.id since that table is wholesale replaced on every export invoice save. A reference table of what's actually been purchased (by design) for that line's product, company-wide, is shown alongside as a guide. ExportInvoiceRepository.sold_totals_by_design now reads from export_packing_list_item_designs instead of the dropped columns. The v77 picker/columns never held real data (added and superseded within the same working session), so nothing is lost by dropping them. v77: purchase_order_items.design_id/design_name (new nullable columns, UNCHANGED by this version - still feeds PO Qty on the Stock History card) - a PO's product lines can optionally be tagged with which catalog design the boxes are for (a "Choose design" picker beside the existing "Choose product" one, scoped to the row's chosen product, same picker pattern packing_lists/form.html already uses). See InventoryService.stock_history_summary, PurchaseOrderRepository.ordered_totals_by_design. NULL on every existing line, which simply doesn't count toward any design's Stock History until re-picked/re-saved with a design chosen; nothing already printed changes, since design_id/design_name are never shown on any PO sheet. v76: export_invoices.bill_of_lading_pdf_path (new nullable column) - the commercial invoice packing list's Bill of lading card gains an uploaded PDF of the actual bill of lading, alongside the existing number/date, saved/removed via the same _save_pdf/_delete_pdf_file pattern the export invoice's own Shipping Bill PDF already uses (ExportInvoiceService.update_packing_list_details, now taking pdf_file/remove_pdf). NULL on every existing invoice, which shows as no file attached. v75: purchase_orders.tax_as_actual (new column, default 0) - the Purchase Order form gains a "Tax as Actual" checkbox right before the "Purchase under" dropdown; when ticked, the printed sheet's IGST/CGST/SGST rows are replaced by a single "TAX AS ACTUAL" line and the Order Value is just the goods subtotal with no tax added (the real tax will only be known once the supplier's own purchase invoice is raised) - see PurchaseOrder.order_value_inr/round_off_inr. 0 on every existing PO, which keeps computing/printing the three tax rows exactly as before. v74: export_invoice_purchase_details.epcg_number/epcg_date (new nullable columns) - each imported supplier row now also snapshots that purchase invoice's own EPCG licence no./date (if any), shown alongside the existing GSTIN/invoice-no/name/Purchase under fields on the Export Invoice form's Purchase details card - display/reference only, does not drive the export invoice's own single EPCG line (ExportInvoiceService._resolve_epcg still independently picks the first match across the whole chain). NULL on every existing row, which shows as blank; re-loading the invoice from its PIs fills them in. v73: export_invoice_items.pallet_weight_kg (new nullable column) - snapshots the weight_kg of whichever named pallet type is selected on a goods line the moment it's picked (product_pallet_types.weight_kg, added in v72). Drives the Export Packing List's container-split Gross (KG), now Net (KG) + Plts x this instead of Boxes x the product's own flat per-box gross weight - falling back to that old formula when no pallet type is known for the row (Loose, or Plts typed by hand with no type picked). NULL on every existing line, so an already-saved invoice's packing list keeps deriving Gross the old way until its goods lines are re-picked/re-saved with a pallet type chosen. v72: product_pallet_types.weight_kg (new nullable column) - the Packing Details table on the product form gains a Weight cell right after Packing Type, alongside the existing Unit Per Packing/Alt Qty per Packing columns; purely informational, nothing computes from it yet. NULL on every existing pallet type, which shows as blank. v71: misc_units (new table) - the seventh Administration -> Miscellaneous drop list: a Unit abbreviation (e.g. SQM) plus what it means in words (e.g. Square Meter), kept together on one row the same way misc_ports_of_loading pairs a port with its PIN code. The table is brand new, so schema.sql creates it on old and fresh databases alike; guarded here too so an older database gets it. v70: export_invoice_purchase_details.purchase_type (new column, default 'full_tax') - each imported supplier row now snapshots the purchase invoice's own "Purchase under" (full_tax | exemption, from purchase_invoices.purchase_type added in v69) alongside its GSTIN/invoice-no/name. Drives two things: the Export Invoice form's "Supply meant for" now auto-selects "Without Payment of IGST under LUT" and locks read-only the moment any linked purchase invoice is under exemption (recomputed fresh on every save from the PI -> PO -> purchase-invoice chain, not trusted from a posted value, same treatment EPCG already gets); and the printed sheet's "Purchase Details of 0.1% GST" block now only lists the exemption rows, and only when the invoice itself is actually under LUT - previously it listed every imported row regardless of purchase type or tax mode. Existing rows default to 'full_tax' (the same fallback every purchase invoice without this field already gets), so an already-issued invoice's printed Purchase Details block goes blank unless it was genuinely under LUT; nothing about the invoice's own tax_mode changes retroactively. v69: purchase_invoices.purchase_type (new column, default 'full_tax') - the Charges & taxes card gains a "Purchase under" dropdown (Full Tax Purchase | Exemption), the same PURCHASE_TYPES list and label the Purchase Order form already uses, typed in here rather than derived since a purchase invoice's tax amounts are the supplier's own figures, not our computed rates. 'full_tax' on every existing invoice, matching what the form already defaulted to before this field existed. v68: misc_countries (new table) + buyers.country (new nullable column) - the sixth Administration -> Miscellaneous drop list, a single "name" per row (e.g. UNITED ARAB EMIRATES), and a "Country Name" field on the Buyer form sitting right below Address, picked from that list the same way currency/port-of-loading/HSN are (with an inline "+ Add a new country" option on the dropdown itself, admin-only). misc_countries is brand new so schema.sql alone creates it on a fresh install; both are guarded here too so an older database gets them. NULL on every existing buyer, which shows as unset exactly like a buyer that has never had its address filled in. v67: proforma_invoices.lead_id/purchase_orders.lead_id/packing_lists.lead_id DROPPED - the "Prefill from an existing lead" dropdown these three forms carried is gone; a Proforma Invoice/Purchase Order/Packing List can now only be tied back to a lead by walking its existing quotation_id/proforma_invoice_id/purchase_order_id chain up to the Quotation, which is the only document type that still has its own lead_id (see PartyService.document_feed and the advance_client_status call sites in ProformaInvoiceService/PurchaseOrderService, both rewired to resolve a lead by walking that chain instead of reading the column directly). No data is carried forward - a document whose only lead link was its own now-dropped lead_id (never chained through a quotation) simply stops being found via a lead/client's document feed; nothing printed changes since lead_id was never shown on any sheet. v66: export_invoice_product_sources (new table) - the Export Invoice's goods lines built from a linked PI's purchase-invoice chain (see v-numberless earlier change: goods now come from what was actually bought, not from the PI's own quoted lines) are aggregated to one row per product, since a single purchase invoice can cover several purchase orders as separate lines for the same product and previously left the same product listed twice (e.g. EXP/25-26/025). This table keeps which purchase order(s) contributed how many boxes to each aggregated goods line, shown read-only under Products - persisted rather than recomputed on reopen, since re-walking the chain later could disagree with a since-edited purchase order/invoice. The table is brand new, so schema.sql creates it on old and fresh databases alike and _migrate needs no step; nothing already saved changes. v65: proforma_invoices.container_details (free TEXT) replaced by proforma_invoice_containers (new table, container type + count rows) - same treatment as quotations.container_details got in v64, and for the same reason: the Proforma Invoice form's single "Container details" text input becomes a repeatable "+ Add container type" list (placed below Payment terms), drawing on the same Administration -> Miscellaneous container-type list, and the printed sheet's "Container Details" cell keeps listing "COUNT x TYPE" per row (see ProformaInvoice.container_details, now a read-only property). QuotationService.build_prefill_from_quotation's "generate a PI from this quotation" flow now carries the quotation's own container rows across structurally instead of as prefilled text. Every existing invoice's old free-typed value is carried over as one row (container_type = the old text, container_count = 1) before the old column is dropped, so nothing already printed goes blank. v64: quotations.container_details (free TEXT) replaced by quotation_containers (new table, container type + count rows, same shape as booking_detail_containers/export_invoice_containers) - the Quotation form's single "Container" text input becomes a repeatable "+ Add container type" list drawing on the same Administration -> Miscellaneous container-type list Booking Detail already uses, and the printed sheet's "Container Details" cell now lists "COUNT x TYPE" per row exactly like the Export Invoice's own Container Details cell (see Quotation.container_details, now a read-only property joining the rows). Every existing quotation's old free-typed value is carried over as one row (container_type = the old text, container_count = 1) so nothing already printed goes blank, then the old column is dropped. v63: export_invoice_purchase_details.supplier_name (new nullable column) - the Purchase details card on the Export Invoice form gains a Supplier name field beside Supplier invoice no., so each imported GSTIN/invoice-no pair says who it belongs to instead of leaving a bare GSTIN to be recognised. Filled by the same PI -> purchase orders -> purchase invoices walk that already imports the GSTIN and invoice number, taking the purchase invoice's own seller_name (falling back to the purchase order's when that invoice has none), and snapshotted onto the row the same way every other imported party name is - so renaming a supplier later can't rewrite an already-issued export invoice. Existing rows are deliberately left NULL rather than backfilled: the column records what the purchase invoice said at import time, and reconstructing it now would quote today's supplier records instead. A NULL shows as a blank input, exactly as those rows show today, and re-loading the invoice from its PIs fills it in. Nothing printed changes - the sheets' Purchase Details block keeps its four GSTIN/INVOICE NO columns. v62: purchase_invoice_purchase_order_links (new table) + purchase_invoice_items.purchase_order_id (new nullable column) - a purchase invoice can now be raised against SEVERAL purchase orders of the same supplier at once (a shipment covering more than one order), not just one. The link table is the authoritative list; purchase_invoices.purchase_order_id (unchanged) keeps holding the first/primary one so every older single-PO call site keeps working untouched. Each item line now remembers which of the invoice's (possibly several) purchase orders it was prefilled from, which is what lets the "Start from" picker work out whether a purchase order still has anything outstanding to invoice (ordered quantity minus what every purchase invoice item tagged with it already covers) - the same outstanding-remainder idea ProformaFulfilmentService already applies one document up the chain. A database whose purchase_invoices predate this gets the column added and both backfilled once: every item's purchase_order_id from its own invoice's (single, at the time) purchase_order_id, and one link row per existing invoice that had one - so nothing already saved changes, and every existing purchase invoice keeps showing up against its purchase order exactly as before. v61: misc_hsn_codes.related_products (new nullable column) - a "Related to Products" note sitting between HSN Code and GST Slab on that Miscellaneous card, saying in words what the code covers (e.g. GLAZED VITRIFIED TILES). Free text and optional, purely a note for whoever reads the list - nothing reads it back, and it is deliberately NOT a link to catalog products, since the product form already records which HSN code a product carries. misc_hsn_codes arrived in v60, so a database created at that version already has the table and needs this ALTER. NULL on every existing row, which shows as blank. v60: misc_hsn_codes (new table) - the fifth Administration -> Miscellaneous drop list: an HSN CODE plus the GST SLAB that applies to it, kept together on one row so the code and its rate can never disagree (the same reasoning behind misc_ports_of_loading pairing a port with its PIN code). The table is brand new, so schema.sql creates it on old and fresh databases alike and _migrate needs no step; nothing already saved changes. v59: misc_container_types (new table) - the fourth Administration -> Miscellaneous drop list: a single "name" per row (e.g. 20FT FCL), replacing the hard-coded container-type list Booking Detail's form used to draw its dropdown from. The table is brand new, so schema.sql creates it on old and fresh databases alike and _migrate needs no step; nothing already saved changes, and a booking's row holding a name no longer on the list stays selectable, same treatment every other Miscellaneous list gets. v58: export_invoice_container_details.container_type - closes a gap where a server whose table predated this column in schema.sql never got it, since CREATE TABLE IF NOT EXISTS is a no-op on an existing table (the same gap v29/v42/etc. closed for their own columns). The column itself is not new to schema.sql, but it was never read or written until now: the 11B table gains a Container type cell, before Container no., filled in read-only from whichever booking is picked (same as every other cell there) and shown only once that booking lists more than one container type - mirroring the column Booking Detail's own 11B table just gained. Existing rows keep it NULL, which prints as blank exactly like an invoice whose booking had a single container type. v57: booking_detail_container_details.container_type (new nullable column) - the Booking Detail's "Upload from Excel" button moves from below the 11B table to sitting next to each container type row, so an uploaded sheet's rows are tagged with that row's container type and added to the table (rather than replacing it wholesale); the 11B table shows a "Container type" column, before Container no., whenever the booking has more than one container type row. Existing rows keep it NULL, which prints as blank exactly like a booking whose 11B rows were never tagged. v56: export_invoices.vessel_voyage_no (one free-text field covering both vessel/flight name and voyage number) splits into vessel_name and voyage_no - the Container details card had a single input for both, typed like "MSC ANNA / VOY 214W". The old value is carried over wholesale into vessel_name (no reliable separator to split an already-free-typed value on), leaving voyage_no blank, and the old column is dropped. ExportInvoice.vessel_voyage_no becomes a read-only property that rejoins the two with " / " for the "Vessel / Flight Name & No" cell both sheets print, so every existing sheet keeps printing exactly what it did before. v55: the Exporter party type is retired - drops the `exporters` table (backed up first, it's a real DROP TABLE) and deletes its party_contacts/communications/payment_history/documents rows (parent_type='exporter'). Buyer and Exporter were always identically-shaped (see the v13 changelog entry below); Exporter never grew its own document types, so it's dropped outright rather than merged into Buyer. v54: quotations.final_destination (new nullable column) - the same field proforma_invoices already carries, now also on the Quotation so its printed sheet can show a Final Destination cell the way proforma invoices do. NULL on every existing quotation, which prints as blank exactly like a proforma invoice with no final destination typed. v53: proforma_invoices.packing_details (new nullable column) - the same "Packing" free-text field quotations already carry (e.g. "PALLATE"), now also on the Proforma Invoice so its printed sheet can show a Packing cell the way quotations do. NULL on every existing invoice, which prints as blank exactly like a quotation with no packing details typed. v52: export_invoice_container_details.tare_weight (free TEXT, never validated) renamed to tare_weight_kg (REAL, checked to actually be a number when typed) - the 11B Tare Weight cell on the Export Invoice form now rejects a non-numeric value instead of silently storing it as text. Existing values that parse as a number are carried over during the migration; a value that doesn't (or a blank cell) becomes NULL, same as it would print today. v51: quotations.cif_adjust_usd (new column, default 0) - quotations no longer have an FOB-typed-price mode (fob_pricing/round_off on quotations are now unused vestiges, kept only so an old row still loads): the typed price is always the absolute FOB price, quantity_value * price_usd summed across every line is the FOB invoice total (Quotation.subtotal_usd), and CIF Value is that total plus the charges (sea freight, insurance, certification, other) - see Quotation.cif_value_usd, which overrides the CifMoneyLadder base formula for quotations only. Typing a different CIF value into the form still works, but the gap between what was typed and what the ladder computes is now a single document-level figure here rather than being spread across line items. 0 on every existing quotation, which keeps totalling exactly as the ladder computes. v50: export_invoices.permission_is_one_time (new column, default 0) - the Permit dropdown on the Export Invoice form can prefill from a permit that has no expiry (validity_type = 'one_time'); this flag remembers that choice so the Annexure-C sheet's Expiry Date cell prints "One Time" instead of a blank date. Set from the permit's own is_one_time when picked from the dropdown, and cleared if the expiry date is typed in by hand. 0 on every existing invoice, which keeps printing the (blank) expiry date exactly as before. v49: round_off on quotations (already there since v39) plus fob_pricing/round_off on proforma_invoices + export_invoices, and fob_price_usd on proforma_invoice_items + export_invoice_items (new columns) - the FOB-first pricing v39 gave the quotation is now available on every buyer-facing document: the user types the FOB price per line, ticks "Prices typed are FOB", and the four charges that sit between FOB and CIF (sea freight, insurance, certification, other - the discount is NOT one of them) are summed, divided by the total alt qty of every line, and that one per-unit figure is added uniformly onto every line's price. price_usd stays what it has always been - the CIF price the sheet prints and every downstream document carries - so the printed sheet, the money ladder and the prefill chain need no special case; fob_price_usd only remembers what was typed so reopening the form shows it back. The uplifted price is rounded to the cent so every printed column multiplies out, and whatever that rounding leaves over is carried in round_off and printed as its own ROUND-OFF row just above CIF VALUE - so the FOB value still lands exactly on the goods total that was typed. Both are 0/off on every existing document, which therefore keeps behaving exactly as before: the typed price IS the CIF price. v48: export_invoices.bill_of_lading_no/bill_of_lading_date (new nullable columns) - the only two typed cells on the new COMMERCIAL INVOICE PACKING LIST, which otherwise restates the export invoice's header over its export packing list's container split (container/seal/RFID numbers, goods, quantities and per-container net/gross weights, with a totals row). Both are optional and print blank until filled in, so an existing invoice's sheet is complete without anyone typing. Like the tax invoice's and the VGM declaration's own columns they are written by their own targeted update, never by the export invoice form. v47: export_invoice_container_details.sealing_time/sealing_date (new nullable columns) - the two typed cells on the new E-SEAL sheet, which is one row per physical container: shipping bill no/date and the e-way bill number come off the export invoice, and the vehicle number, container number and e-seal number (the 11B row's RFID seal) off that container's own 11B row, leaving only when the seal was applied to be typed. Like the VGM attachment's weighbridge pair they have no input on the export invoice form, so ExportInvoiceService carries them forward by row position when that form is saved (which rewrites the 11B rows wholesale). Existing rows keep both NULL. v46: export_invoices.vgm_signatory/vgm_contact_24x7/vgm_weighing_method/vgm_cargo_type/vgm_hazardous_details (new nullable columns) - the manual-entry cells of the new VGM declaration (INFORMATION ABOUT VERIFIED GROSS MASS OF CONTAINER), which is the shipper-level counterpart to the per-container VGM attachment. Every other cell on it is derived: booking no/shipper/IEC from the invoice and Our Company, and the six per-container cells from the VGM attachment's rows - quoted inline for three containers or fewer, and replaced by the words "VGM ATTACHMENT" beyond that, since the declaration has one cell per field rather than one row per container. All five are optional and fall back to a sensible default (the invoice's authorised signatory, the company phone, METHOD-1, NORMAL, N/A), so an existing invoice prints a complete sheet without anyone typing anything. Like the tax invoice's own fields they are written by their own targeted update, never by the export invoice form. v45: export_invoice_container_details.weighbridge_name/weighing_slip_no (new nullable columns) - the two typed cells on the new VGM ATTACHMENT, which is one row per physical container: booking no, container no and size, max permitted and tare weight all come off the export invoice's 11B row, cargo weight off that container's export-packing-list total, and Total VGM Weight is tare + cargo, leaving the weighbridge and its slip number as the only facts nobody else knows. They are typed on that sheet, row by row, and (like gross_weight/net_weight before them) have no input on the export invoice form - so ExportInvoiceService.update carries them forward by row position, since saving that form rewrites the 11B rows wholesale. Existing rows keep both NULL. v44: export_invoices.tax_invoice_number/tax_invoice_date (new nullable columns) - the Tax Invoice attachment gains the one thing it owns: its own number and date, typed on a small edit form of its own (everything else on that sheet still derives from the parent export invoice, so the form asks for nothing else). Both are optional and fall back to the export invoice's own number and date while blank - which is what every existing tax invoice does, so nothing already printed changes. They are deliberately NOT part of the export invoice form's header fields: that form never posts them, and treating them as header fields would blank them on every export invoice save. v43: export_invoices.eway_bill_no/eway_bill_date (new nullable columns) - the only two cells on the new TAX INVOICE attachment that the export invoice did not already carry. The Tax Invoice itself stores nothing: like the Export Packing List and Annexure-C it is a pure read-only view of its parent export invoice, printing that invoice's own number and date and its whole money ladder converted to INR at the invoice's own exchange rate. Existing invoices keep both NULL and print '-' in those cells. v42: export_invoice_container_details.lr_no/transporter_name/max_permitted_weight (new nullable columns) - three more per-container inputs on the Export Invoice's 11B table, sitting right after Vehicle no. LR NO and MAX PERMITTED WEIGHT are free text; TRANSPORTER NAME is picked from the tenant's Transporters list (v38) and the chosen NAME is snapshotted onto the row - the same treatment currencies/ports/nature-of-contract get - so renaming or deleting a transporter later can't rewrite a saved invoice, and a row holding a name no longer on the list stays selectable. Like vehicle_no these are captured and stored but not printed on any sheet. Existing 11B rows keep all three NULL, so nothing already saved changes. v41: export_invoices.vessel_voyage_no (new nullable column) - the VESSEL AND VOYAGE NO typed on the Export Invoice form's Container details card, alongside the booking number. Both the Export Invoice and the Export Packing List sheets printed a hard-coded "N/A" in their "Vessel / Flight Name & No" cell because nothing on the invoice captured it; they now print this when it is set and keep falling back to "N/A" when it is blank, so nothing already saved changes. v40: misc_ports_of_loading (new table) - the third Administration -> Miscellaneous drop list: a PORT OF LOADING name plus that port's PIN code, kept together so the port and the PIN the GST/e-way-bill paperwork asks for can never disagree. The table is brand new, so schema.sql creates it on old and fresh databases alike and _migrate needs no step; nothing already saved changes. v39: quotations.fob_pricing + quotation_items.fob_price_usd (new columns) - a quotation can now be priced the way the export invoice needs it: the user types the FOB price per line, ticks "Prices typed are FOB", and the four charges that sit between FOB and CIF (sea freight, insurance, certification, other - the discount is NOT one of them) are summed, divided by the total alt qty of every line, and that one per-unit figure is added uniformly onto every line's price. price_usd stays what it has always been - the CIF price the sheet prints and every downstream document carries - so the printed sheet, the money ladder and the proforma-invoice prefill need no special case; fob_price_usd only remembers what was typed so reopening the form shows it back. The uplift is kept at full precision so CIF - charges lands exactly on the typed FOB total. Off (0) on every existing quotation, which therefore keeps behaving exactly as before: the typed price IS the CIF price. v38: transporters + transporter_contacts (new tables) - a fourth party type alongside buyer/supplier/exporter, and the only one that is NOT reachable from a lead: a transporter is the haulier whose registration details get quoted on consignment paperwork, so it carries name/address/GSTIN-or-transporter-no/PAN/CIN-LLP/email plus buyer-shaped contact persons, and deliberately has no status pipeline and no payments/communications/documents feed. Both tables are brand new, so schema.sql creates them on old and fresh databases alike and _migrate needs no step. v37: export_invoice_container_details.excise_seal_no/plts/boxes DROPPED - the Excise seal no., Plts and Boxes inputs on the Export Invoice's 11B container table are gone from the form, the model and the columns. Nothing printed used them (the sheet's Plts/Boxes totals come from the goods lines, not these rows), so no sheet changes. v36: quotations/proforma_invoices/purchase_orders/purchase_invoices.currency_code+currency_symbol (new nullable columns) - every document now carries the currency it is written in, picked from the Administration -> Miscellaneous currency list the same way the Export Invoice already did in v34, and snapshotted (name + symbol) so editing that list can't rewrite an issued sheet. It is display information only: no conversion happens and the stored amounts are untouched. Documents saved before this fall back to what their template used to hard-code (USD on quotations/proforma invoices, INR on purchase orders/purchase invoices), so nothing already printed changes. v35: misc_nature_of_contracts (new table) - the second Administration -> Miscellaneous drop list, a single "name" per row, feeding the delivery-terms fields that are worded differently per document: "Nature of contract" (export invoice), "Shipping terms" (quotation) and "Terms of delivery" (proforma invoice), all three of which were free-text inputs before. The documents keep storing the chosen text, so nothing already saved changes and a value no longer on the list stays selectable. v34: misc_currencies (new table) + export_invoices.currency_code/currency_symbol (new nullable columns) - Administration -> Miscellaneous is where an admin maintains the app's hand-kept drop lists, the first being CURRENCY (name of currency + currency symbol). Every currency dropdown now reads that list (payment history on a buyer/exporter/supplier, and the Export Invoice's new Currency field, whose chosen name+symbol is snapshotted onto the invoice so editing the list later can't rewrite an already-printed sheet). Until an admin adds a row the list falls back to the six codes that used to be hard-coded in the payment form. v33: export_invoices.c_no/c_date/stuffing_start_time/stuffing_completion_time and export_invoice_purchase_details.supplier_invoice_qty/supplier_taxable_amount/supplier_cgst_amount/supplier_sgst_amount DROPPED - none of them were ever printed on the Export Invoice or Annexure-C sheets after the annexure redesign, so the inputs, the columns and the computed ExportInvoice.stuffing_time_taken are all gone. v32: our_company.government_schemes (new nullable column) - free text set once under Our Company settings, used as the default for the Export Annexure's section 13 claim-scheme cell and printed as a heading on the Export Invoice's own sheet. v31: export_invoices.shipping_bill_date/stuffing_start_time/stuffing_completion_time (new nullable columns) - the standalone Annexure-C (Examination Report For Self Sealed Container For Export) document needs a Shipping Bill Date alongside the existing Shipping Bill No, and a Time Of Stuffing (Starting/Completion; "time taken" is computed, not stored) that nothing previously captured. v30: export_invoices.total_net_weight_kg/total_gross_weight_kg/c_no/c_date/shipping_bill_no, export_invoice_purchase_details.supplier_invoice_qty/supplier_taxable_amount/supplier_cgst_amount/supplier_sgst_amount, export_invoice_container_details.excise_seal_no/plts/boxes, our_company.branch_code (all new nullable columns) - closes the remaining Export Invoice cells missing against the reference PDF (06/07 examiner placeholders and 08's duplicate labeled row need no new columns, just template changes). v29: export_invoices.stuffing_location (new nullable column) - the "Stuff At" address printed on the export packing list. It was only ever added to schema.sql, so a server whose export_invoices table predated it never got the column and every export-invoice read/write there failed with `no such column: stuffing_location` (the same CREATE TABLE IF NOT EXISTS no-op gap v26 closed for buyer_order_no/buyer_order_date/booking_no). v28: export_invoice_container_details.gross_weight/net_weight (new nullable columns) - shown on the 11B table and stored, but with no form input (unlike tare_weight). v27: export_invoice_container_details.tare_weight (new nullable column) - the 11B container table's Tare Weight column is now typed in per row instead of always blank. v26: export_invoices.buyer_order_no/buyer_order_date/booking_no (new nullable columns) - closes a gap where a server whose export_invoices table predated these columns in schema.sql never got them, since CREATE TABLE IF NOT EXISTS is a no-op on an existing table. v25: permits.stuffing_place_number renamed to stuffing_place_name (holds a name, not a number). v24: permits.supplier_id dropped, permits.stuffing_place_number added - a permit records a stuffing-place name (shown right before place_of_stuffing) instead of being tied to a supplier. v23: proforma_invoice_items/purchase_order_items/export_invoice_items/packing_list_items.quantity_unit (new columns, default 'PCS') - same treatment as quotation_items.quantity_unit in v22, extended to the other three document types' Boxes columns. v22: quotation_items.quantity_unit (new column, default 'PCS') - the Boxes column (renamed QTY on the printed sheet) now shows its product's Quantity unit as small text after the number, snapshotted from products.quantity_unit at save time the same way `unit` already snapshots alternate_quantity_unit. v21: export_invoices + its child tables (export_invoice_items/_proforma_links/_buyer_orders/_containers/_container_details/_purchase_details) - the customer/customs-facing Export Invoice at the buyer end of the pipeline, raised against one or more Proforma Invoices (many-to-many), with per-product tax, a manual admin-locked exchange rate, imported EPCG/export-under/supplier-exemption details, and a two-page printed sheet; plus our_company.self_sealing_declaration and our_company_contact_persons.designation (both new nullable columns feeding the Export Invoice's declaration block and Authorised Signatory dropdown). v20: suppliers.cin_llp_no (new nullable column) - optional CIN (company) / LLPIN (LLP) registration number, shown alongside GSTIN on a supplier's profile. v19: quotation_items.pallets (new nullable column) - a quotation's product lines can now carry a Plts figure the same way proforma_invoice_items/purchase_order_items/packing_list_items already do, so a Proforma Invoice generated from a Quotation (build_prefill_from_quotation) can carry that pallet count over instead of starting blank. v18: packing_lists.purchase_invoice_id (new nullable FK) - a Purchase Invoice can now carry its own packing list, imported wholesale from its linked purchase order's own PL. v17: purchase_invoices/purchase_invoice_items/purchase_invoice_vehicles (new tables) - the document raised once a supplier's goods against one of our purchase orders actually arrive, carrying the supplier's own invoice number/date, transporter/vehicle details, optional EPCG number/date, an uploaded copy of the supplier's own invoice PDF (nothing is generated/printed for this document type), and typed-in discount/insurance/freight/tax/round-off figures matching what the supplier actually charged. v16: proforma_invoices.status ('draft' | 'confirmed') - confirming a PI locks it for editing (an admin can move it back to draft) and starts the "still to be ordered" reminder that runs until every design on the PI's packing list has been placed on the packing list of some purchase order linked to that PI. v15: purchase_orders.purchase_type ('full_tax' | 'exemption') - a PO's GST percentages are no longer typed in by hand, they follow from this choice plus the GSTIN state-code comparison between our company and the seller. v14: our_company_rcmc_details (new table) - repeatable RCMC (Registration-cum-Membership Certificate) records per company, same shape/pattern as our_company_lut_details. v13: the single `clients` table (Buyer/Supplier/Exporter via client_type) is split into three separate entities - buyers/exporters (same shape as before, minus client_type), and suppliers (an our_company-shaped profile: GSTIN/PAN/IEC/bank/contacts, no logo/BIN/LUT). party_contacts replaces client_contacts for buyer/exporter; payment_history/documents/communications gain a parent_type discriminator so one type's ids can't collide with another's; purchase_orders.seller_client_id becomes seller_supplier_id; leads gains converted_client_type alongside converted_client_id. v12: purchase orders (new purchase_orders/purchase_order_items tables via schema.sql, plus packing_lists.purchase_order_id so a PO can carry its own packing list) and our_company.logo_path (company logo shown in the app and on generated documents). v11: each product quantity gets its own unit - quantity_unit (new, 'PCS' for existing rows) and alternate_quantity_unit (renamed from `unit`)


# The v95 production tables' columns, kept here because _migrate both creates
# them and (on a database that ran the first cut of v95) rebuilds one of them
# - two spellings of the same DDL would be a shape mismatch waiting to happen.
# schema.sql holds the same definitions, commented, for fresh databases.
_PRODUCTION_COLUMNS = """
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_item_id  INTEGER NOT NULL REFERENCES purchase_order_items(id) ON DELETE CASCADE,
    design_id               INTEGER REFERENCES designs(id) ON DELETE SET NULL,
    design_name             TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending',
    updated_by              INTEGER REFERENCES users(id),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
"""
_BATCH_COLUMNS = """
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_item_id  INTEGER NOT NULL REFERENCES purchase_order_items(id) ON DELETE CASCADE,
    design_id               INTEGER REFERENCES designs(id) ON DELETE SET NULL,
    design_name             TEXT,
    sr_no                   INTEGER NOT NULL,
    batch_number            TEXT,
    production_date         TEXT,
    quantity_boxes          REAL NOT NULL DEFAULT 0,
    remarks                 TEXT
"""


class Database:
    """Thin wrapper around sqlite3 connections.

    Usage:
        db = Database(path)
        db.init_schema(schema_path)
        with db.get_connection() as conn:
            conn.execute(...)
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        # Rows behave like dicts (row["column"]) - much friendlier for
        # templates/services than positional tuples.
        conn.row_factory = sqlite3.Row
        # Enforce FOREIGN KEY / CASCADE rules declared in schema.sql -
        # SQLite ignores them unless this pragma is turned on per-connection.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def get_connection(self):
        """Context manager that commits on success and rolls back on error,
        so callers never have to remember to do either."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self, schema_path: str) -> None:
        """Create every table defined in schema.sql if it doesn't exist yet.
        Safe to call on every app startup."""
        self._pre_schema_migrate()
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        with self.get_connection() as conn:
            conn.executescript(schema_sql)
        self._migrate(conn=None)
        # Needs its own foreign-keys-off connection, so it can't live inside
        # _migrate's shared one (see the method's own docstring).
        self._rebuild_job_works()
        # Record the shape this DB is now in. Runs on fresh installs, on
        # startup upgrades, and again when a restored backup is migrated
        # forward - so `user_version` always reflects the live schema.
        with self.get_connection() as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _pre_schema_migrate(self) -> None:
        """Drops old-format tables BEFORE schema.sql runs, so its
        CREATE TABLE IF NOT EXISTS statements recreate them in the new
        shape (an old-shape survivor would also crash the CREATE INDEX
        statements at the bottom of schema.sql).

        This is the "start fresh" product/folder/design restructure: the
        catalog used to be product_groups (nested folders) holding products
        as the leaves; it is now products (tax + HSN identity) ->
        product_folders -> designs. Old catalog data is NOT converted - the
        whole DB file is backed up to instance/backups/ first, then the old
        tables are dropped. Old-format tables are recognised by columns the
        new shapes don't have, so this runs exactly once. The same applies
        to an abandoned early packing_lists experiment some databases carry.
        """
        if not os.path.exists(self.db_path):
            return
        with self.get_connection() as conn:
            product_cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            packing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            legacy_products = bool(product_cols) and "group_id" in product_cols
            legacy_packing = bool(packing_cols) and "packing_list_number" not in packing_cols
            if not legacy_products and not legacy_packing:
                return
            self._backup_db_file("pre_product_redesign")
            conn.execute("PRAGMA foreign_keys = OFF")
            if legacy_products:
                # Line items keep their snapshot columns (name/hsn/price) but
                # their product_id points into the dropped catalog - null the
                # stale references out.
                conn.execute("UPDATE quotation_items SET product_id = NULL")
                conn.execute("UPDATE proforma_invoice_items SET product_id = NULL")
                conn.execute("DROP TABLE products")
                conn.execute("DROP TABLE IF EXISTS product_groups")
            if legacy_packing:
                conn.execute("DROP TABLE IF EXISTS packing_list_items")
                conn.execute("DROP TABLE packing_lists")
            conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self, conn=None) -> None:
        """Add columns to already-created tables that predate a schema change.
        `CREATE TABLE IF NOT EXISTS` can't retrofit columns onto an existing
        table, so new nullable columns are added here, guarded by a check
        against the live column list (ALTER TABLE has no IF NOT EXISTS).

        Every future restructure adds a step here and bumps `SCHEMA_VERSION`
        (see the module-level comment). Steps must be DATA-PRESERVING and
        idempotent: this method runs on every startup and every backup
        restore, so it is what forward-migrates an old uploaded backup instead
        of discarding its data."""
        with self.get_connection() as conn:
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company_bank_details)")}
            for column in ("swift_code", "bank_address"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE our_company_bank_details ADD COLUMN {column} TEXT")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            for column in ("bin", "address"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE our_company ADD COLUMN {column} TEXT")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(clients)")}
            if existing and "address" not in existing:
                conn.execute("ALTER TABLE clients ADD COLUMN address TEXT")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_list_items)")}
            for column in ("box_per_pallet", "pcs"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE packing_list_items ADD COLUMN {column} REAL")

            # v6: optional surface finish on designs (GLOSSY / MATT / ...)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(designs)")}
            if existing and "surface" not in existing:
                conn.execute("ALTER TABLE designs ADD COLUMN surface TEXT")

            # v7: proforma goods layout choice + per-line surface finish
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoices)")}
            if existing and "display_mode" not in existing:
                conn.execute("ALTER TABLE proforma_invoices ADD COLUMN display_mode TEXT NOT NULL DEFAULT 'index'")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoice_items)")}
            if existing and "surface" not in existing:
                conn.execute("ALTER TABLE proforma_invoice_items ADD COLUMN surface TEXT")

            # v9: product net/gross weight per box (KG) - drives the packing
            # list's Boxes x weight auto-calc, same pattern as
            # alternate_quantity driving the Qty auto-calc.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            for column in ("net_weight_kg", "gross_weight_kg"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE products ADD COLUMN {column} REAL")

            # v90: product's own Price field.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if existing and "price_usd" not in existing:
                conn.execute("ALTER TABLE products ADD COLUMN price_usd REAL")

            # v91: "Job Work Product" checkbox + its master product reference.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if existing and "is_job_work_product" not in existing:
                conn.execute("ALTER TABLE products ADD COLUMN is_job_work_product INTEGER NOT NULL DEFAULT 0")
            if existing and "master_product_id" not in existing:
                conn.execute("ALTER TABLE products ADD COLUMN master_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL")

            # v92: the job out's TRANSPORT NAME - the challan names the
            # transporter alongside its GSTIN. NULL on every existing job
            # out, which falls back to resolving the name off transport_gstin
            # (and then the purchase invoice's own transporter_name) - see
            # JobOutService._transporter_name.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_outs)")}
            if existing and "transporter_name" not in existing:
                conn.execute("ALTER TABLE job_outs ADD COLUMN transporter_name TEXT")

            # v9: a packing list can now be generated directly from a
            # Quotation (skipping the proforma invoice step) - same
            # "generated from" reference pattern as proforma_invoice_id.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "quotation_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN quotation_id INTEGER REFERENCES quotations(id)")

            # ---- PACKING SPEC MOVES FROM DESIGN TO PRODUCT ----
            # packing / quantity / alternate_quantity / unit / weight_class
            # describe a PRODUCT's physical packing (its box config, unit of
            # measure) - they don't vary by design/finish, so they move up
            # from `designs` to `products` once. Live data is forward-
            # migrated, not discarded: each product backfills these fields
            # from whichever of its designs happens to carry them (first by
            # id), then the columns are dropped from `designs`. Recognised
            # by `packing` still being a column on `designs` - true whether
            # or not that design table ever got `unit` added in an earlier
            # run, so the backfill below tolerates either case.
            designs_existing = {r["name"] for r in conn.execute("PRAGMA table_info(designs)")}
            if designs_existing and "packing" in designs_existing:
                has_unit = "unit" in designs_existing

                products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
                if "packing" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN packing TEXT")
                if "quantity" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN quantity TEXT")
                if "alternate_quantity" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN alternate_quantity TEXT")
                if "unit" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN unit TEXT NOT NULL DEFAULT 'SQM'")
                if "weight_class" not in products_existing:
                    conn.execute("ALTER TABLE products ADD COLUMN weight_class TEXT")

                # Backfill: one representative design's value per product
                # (first by id that has a non-null value), only where the
                # product doesn't already have a value of its own.
                for field in ("packing", "quantity", "alternate_quantity", "weight_class"):
                    conn.execute(f"""
                        UPDATE products SET {field} = (
                            SELECT d.{field} FROM designs d
                            WHERE d.product_id = products.id AND d.{field} IS NOT NULL
                            ORDER BY d.id LIMIT 1
                        )
                        WHERE {field} IS NULL
                    """)
                if has_unit:
                    conn.execute("""
                        UPDATE products SET unit = (
                            SELECT d.unit FROM designs d
                            WHERE d.product_id = products.id
                            ORDER BY d.id LIMIT 1
                        )
                        WHERE EXISTS (SELECT 1 FROM designs d WHERE d.product_id = products.id)
                    """)

                # Rebuild `designs` without the five columns that just moved
                # up - other tables' FKs to designs(id) (packing_list_items)
                # must not get silently rewritten to the "_old" table, hence
                # the same foreign_keys/legacy_alter_table dance used
                # elsewhere in this file.
                conn.commit()
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("PRAGMA legacy_alter_table = ON")
                conn.execute("ALTER TABLE designs RENAME TO designs_old")
                conn.execute("""
                    CREATE TABLE designs (
                        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id              INTEGER NOT NULL REFERENCES tenants(id),
                        product_id              INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                        folder_id               INTEGER REFERENCES product_folders(id) ON DELETE CASCADE,
                        design_name             TEXT NOT NULL,
                        description             TEXT,
                        price_usd               REAL,
                        photo_path              TEXT,
                        dimension_photo_path    TEXT,
                        alt_text                TEXT,
                        created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.execute("""
                    INSERT INTO designs (id, company_id, product_id, folder_id, design_name, description,
                                          price_usd, photo_path, dimension_photo_path, alt_text, created_at, updated_at)
                    SELECT id, company_id, product_id, folder_id, design_name, description,
                           price_usd, photo_path, dimension_photo_path, alt_text, created_at, updated_at
                    FROM designs_old
                """)
                conn.execute("DROP TABLE designs_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_designs_product ON designs(product_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_designs_folder ON designs(folder_id)")
                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            # ---- v4: CATEGORY LEVEL + GST COLUMN RETIRED ----
            # The catalog is now category -> product -> sub category ->
            # design. Categories behave like folders at the catalog root:
            # products carry a nullable category_id (NULL = catalog root).
            # At the same time the product's standalone gst_percent input is
            # retired: IGST is the only tax input, and SGST/CGST are always
            # stored as half of it. Existing rows get their SGST/CGST
            # recalculated from IGST once, then the gst_percent column is
            # dropped (which is also the guard that makes this one-shot).
            products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if products_existing and "category_id" not in products_existing:
                conn.execute(
                    "ALTER TABLE products ADD COLUMN category_id INTEGER "
                    "REFERENCES categories(id) ON DELETE CASCADE"
                )
            if products_existing and "gst_percent" in products_existing:
                conn.execute("""
                    UPDATE products
                    SET sgst_percent = ROUND(igst_percent / 2.0, 2),
                        cgst_percent = ROUND(igst_percent / 2.0, 2)
                """)
                conn.execute("ALTER TABLE products DROP COLUMN gst_percent")
            # Lives here instead of schema.sql: on a pre-v4 DB the column
            # doesn't exist yet when schema.sql runs.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)")

            # ---- v5: CATEGORIES CAN NEST ----
            # Categories now behave exactly like sub categories (product_folders):
            # a self-referencing, nullable parent_id lets one category sit
            # inside another to any depth. A plain ADD COLUMN is enough - no
            # existing category has a parent to backfill.
            categories_existing = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
            if categories_existing and "parent_id" not in categories_existing:
                conn.execute(
                    "ALTER TABLE categories ADD COLUMN parent_id INTEGER "
                    "REFERENCES categories(id) ON DELETE CASCADE"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)")

            # The original `leads.status` CHECK constraint didn't allow
            # 'in_client', so converting a lead to a client crashed on the
            # final UPDATE (after the client row was already created) - a
            # CHECK constraint can't be altered in place, so the table has
            # to be rebuilt.
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='leads'"
            ).fetchone()
            if row and "in_client" not in row["sql"]:
                conn.execute("PRAGMA foreign_keys = OFF")
                # Without this, SQLite silently rewrites the REFERENCES
                # clauses in `clients.lead_id` and `lead_contacts.lead_id`
                # to point at `leads_old`, which breaks once that table is
                # dropped below.
                conn.execute("PRAGMA legacy_alter_table = ON")
                conn.execute("ALTER TABLE leads RENAME TO leads_old")
                conn.execute("""
                    CREATE TABLE leads (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_name        TEXT NOT NULL,
                        phone               TEXT NOT NULL,
                        email               TEXT NOT NULL,
                        facebook            TEXT,
                        instagram           TEXT,
                        other_social        TEXT,
                        status              TEXT NOT NULL DEFAULT 'new'
                                            CHECK (status IN (
                                                'new', 'in_communication', 'in_follow_up',
                                                'long_follow_up', 'quotation_submission_pending', 'in_client'
                                            )),
                        created_by          INTEGER NOT NULL REFERENCES users(id),
                        created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
                        is_converted         INTEGER NOT NULL DEFAULT 0,
                        converted_client_id  INTEGER REFERENCES clients(id)
                    )
                """)
                conn.execute("""
                    INSERT INTO leads (id, company_name, phone, email, facebook, instagram,
                                        other_social, status, created_by, created_at, updated_at,
                                        is_converted, converted_client_id)
                    SELECT id, company_name, phone, email, facebook, instagram,
                           other_social, status, created_by, created_at, updated_at,
                           is_converted, converted_client_id
                    FROM leads_old
                """)
                conn.execute("DROP TABLE leads_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_by ON leads(created_by)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
            if existing and "lead_id" not in existing:
                conn.execute("ALTER TABLE quotations ADD COLUMN lead_id INTEGER REFERENCES leads(id)")
            if existing:
                for column in ("sea_freight", "insurance", "certification", "other_charges"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE quotations ADD COLUMN {column} REAL NOT NULL DEFAULT 0")

            # `our_company.lut` used to hold a single LUT number; it's now a
            # list in `our_company_lut_details` (one row per financial year).
            # Carry over any existing value once, then null the old column
            # out so this seed never fires again even if every LUT row is
            # later deleted on purpose.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "lut" in existing:
                row = conn.execute("SELECT lut FROM our_company WHERE id = 1").fetchone()
                if row and row["lut"]:
                    conn.execute(
                        "INSERT INTO our_company_lut_details (lut_number, financial_year, is_primary) "
                        "VALUES (?, '', 1)",
                        (row["lut"],),
                    )
                conn.execute("UPDATE our_company SET lut = NULL WHERE id = 1")

            # ---- MULTI-TENANCY ----
            # Everything below gives every pre-existing single-tenant install
            # a home ("Company #1" in `tenants`) and adds `company_id`
            # everywhere so multiple independent businesses can share one
            # install. Gated on `users` lacking `company_id`: a brand-new
            # install's schema.sql already includes it on every table, so
            # this whole block only ever runs once, only for databases that
            # predate multi-tenancy.
            #
            # `PRAGMA foreign_keys`/`legacy_alter_table` are no-ops inside an
            # active transaction, and the UPDATE statements in the migration
            # steps above (e.g. the lut nulling-out) already opened one
            # implicitly - commit first so the toggle below actually applies.
            conn.commit()
            users_existing = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if users_existing and "company_id" not in users_existing:
                conn.execute("PRAGMA foreign_keys = OFF")
                # Same reasoning as the `leads` rebuild above: without this,
                # SQLite silently rewrites every other table's REFERENCES
                # clause that points at a table we're about to rename (users,
                # quotations) to point at the "_old" name instead, which
                # breaks once that table is dropped. One bracket covers all
                # three rebuilds below.
                conn.execute("PRAGMA legacy_alter_table = ON")

                # `tenants` already exists (created by the executescript
                # above) but is empty on a legacy install - seed Company #1,
                # named after the existing Our Company profile if one was
                # ever filled in, so the upcoming backfills have a home.
                if not conn.execute("SELECT id FROM tenants WHERE id = 1").fetchone():
                    company_cols = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
                    company_row = conn.execute("SELECT company_name FROM our_company WHERE id = 1").fetchone() \
                        if company_cols else None
                    default_name = (company_row["company_name"] if company_row else None) or "Company 1"
                    conn.execute(
                        "INSERT INTO tenants (id, name, slug, is_active) VALUES (1, ?, 'company-1', 1)",
                        (default_name,),
                    )

                # leads / clients / product_groups / products: plain ADD
                # COLUMN + backfill, no constraint changes needed.
                for table in ("leads", "clients", "product_groups", "products"):
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if cols and "company_id" not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN company_id INTEGER REFERENCES tenants(id)")
                        conn.execute(f"UPDATE {table} SET company_id = 1 WHERE company_id IS NULL")

                # users: rebuild for the new UNIQUE(company_id, username).
                # Every existing `id` is preserved explicitly - leads.created_by,
                # communications.employee_id, clients.created_by and
                # quotations.created_by all reference these ids by number and
                # must not shift.
                conn.execute("ALTER TABLE users RENAME TO users_old")
                conn.execute("""
                    CREATE TABLE users (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id      INTEGER NOT NULL REFERENCES tenants(id),
                        username        TEXT NOT NULL,
                        password_hash   TEXT NOT NULL,
                        full_name       TEXT NOT NULL,
                        role            TEXT NOT NULL CHECK (role IN ('admin', 'employee')),
                        is_active       INTEGER NOT NULL DEFAULT 1,
                        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE (company_id, username)
                    )
                """)
                conn.execute("""
                    INSERT INTO users (id, company_id, username, password_hash, full_name, role, is_active, created_at)
                    SELECT id, 1, username, password_hash, full_name, role, is_active, created_at FROM users_old
                """)
                conn.execute("DROP TABLE users_old")

                # quotations: rebuild for UNIQUE(company_id, quotation_number).
                # By this point every earlier migration step in this function
                # (lead_id, sea_freight/insurance/certification/other_charges)
                # has already run, so `quotations_old` already has those
                # columns and the copy below carries them across.
                q_cols = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
                if q_cols and "company_id" not in q_cols:
                    conn.execute("ALTER TABLE quotations RENAME TO quotations_old")
                    conn.execute("""
                        CREATE TABLE quotations (
                            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id              INTEGER NOT NULL REFERENCES tenants(id),
                            quotation_number        TEXT NOT NULL,
                            quotation_date          TEXT NOT NULL,
                            lead_id                  INTEGER REFERENCES leads(id),
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
                        )
                    """)
                    conn.execute("""
                        INSERT INTO quotations (id, company_id, quotation_number, quotation_date, lead_id,
                            buyer_name, buyer_address, buyer_reference_no, port_of_loading, port_of_discharge,
                            packing_details, container_details, shipping_mode, shipping_terms, payment_terms,
                            price_validity_days, remarks, sea_freight, insurance, certification, other_charges,
                            discount_amount, bank_name, bank_account_number, bank_ifsc_code, bank_swift_code,
                            bank_branch, bank_address, created_by, created_at, updated_at)
                        SELECT id, 1, quotation_number, quotation_date, lead_id,
                            buyer_name, buyer_address, buyer_reference_no, port_of_loading, port_of_discharge,
                            packing_details, container_details, shipping_mode, shipping_terms, payment_terms,
                            price_validity_days, remarks, sea_freight, insurance, certification, other_charges,
                            discount_amount, bank_name, bank_account_number, bank_ifsc_code, bank_swift_code,
                            bank_branch, bank_address, created_by, created_at, updated_at
                        FROM quotations_old
                    """)
                    conn.execute("DROP TABLE quotations_old")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotations_created_by ON quotations(created_by)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotations_date ON quotations(quotation_date)")

                # our_company: drop the old `id = 1` singleton CHECK, key by
                # company_id instead (one row per tenant instead of one row
                # total). `id` is preserved so the child detail tables' new
                # `our_company_id` FK (backfilled below) stays valid.
                oc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
                if oc_cols and "company_id" not in oc_cols:
                    conn.execute("ALTER TABLE our_company RENAME TO our_company_old")
                    conn.execute("""
                        CREATE TABLE our_company (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id      INTEGER NOT NULL UNIQUE REFERENCES tenants(id),
                            company_name    TEXT NOT NULL,
                            address         TEXT,
                            gstin           TEXT,
                            pan_no          TEXT,
                            iec             TEXT,
                            bin             TEXT,
                            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO our_company (id, company_id, company_name, address, gstin, pan_no, iec, bin, updated_at)
                        SELECT id, 1, company_name, address, gstin, pan_no, iec, bin, updated_at FROM our_company_old
                    """)
                    conn.execute("DROP TABLE our_company_old")

                # our_company_* child tables: plain ADD COLUMN + backfill,
                # pointing at whichever our_company.id belongs to company_id 1
                # (at most one existing row on a legacy install).
                for table in ("our_company_lut_details", "our_company_contact_details",
                              "our_company_contact_persons", "our_company_bank_details"):
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if cols and "our_company_id" not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN our_company_id INTEGER REFERENCES our_company(id)")
                        conn.execute(
                            f"UPDATE {table} SET our_company_id = "
                            f"(SELECT id FROM our_company WHERE company_id = 1) "
                            f"WHERE our_company_id IS NULL"
                        )

                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            # Company-scoped queries filter by company_id constantly - index
            # it on every root table now that the column is guaranteed to
            # exist (either from a fresh install's schema.sql, or from the
            # legacy-upgrade block above). Safe to run unconditionally.
            for table in ("users", "leads", "categories", "products", "product_folders", "designs", "quotations"):
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_company ON {table}(company_id)")

            # ---- v10: PALLET PACKING BECOMES A LIST OF PALLET TYPES ----
            # products.packing used to hold one boxes-per-pallet figure; a
            # product now carries any number of NAMED pallet storage options
            # in product_pallet_types (plus an implicit, unstored "loose"
            # option = no pallets). Each existing packing value becomes one
            # pallet type named 'pallet' (its leading number as the
            # boxes-per-pallet count), then the column is dropped - which is
            # also the guard that makes this one-shot. Runs after the
            # multi-tenancy block so products.company_id is guaranteed to
            # exist even on legacy databases.
            products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if products_existing and "packing" in products_existing:
                rows = conn.execute(
                    "SELECT id, company_id, packing FROM products "
                    "WHERE packing IS NOT NULL AND TRIM(packing) != ''"
                ).fetchall()
                for row in rows:
                    m = re.match(r"\s*([\d.]+)", str(row["packing"]))
                    try:
                        boxes = float(m.group(1)) if m else 0.0
                    except ValueError:
                        boxes = 0.0
                    if boxes > 0:
                        conn.execute(
                            "INSERT INTO product_pallet_types (company_id, product_id, name, boxes_per_pallet) "
                            "VALUES (?, ?, 'pallet', ?)",
                            (row["company_id"], row["id"], boxes),
                        )
                conn.execute("ALTER TABLE products DROP COLUMN packing")

            # ---- v11: EACH PRODUCT QUANTITY GETS ITS OWN UNIT ----
            # The product spec is now (quantity unit, quantity) +
            # (alt quantity unit, alt quantity) + pallet types. `unit` only
            # ever described the alternate quantity (it prefills the Unit
            # column on document lines), so it's renamed to
            # alternate_quantity_unit - the rename is also the one-shot
            # guard. quantity was always a pcs-per-box figure, so existing
            # rows get quantity_unit = 'PCS'.
            products_existing = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
            if products_existing and "unit" in products_existing:
                conn.execute("ALTER TABLE products RENAME COLUMN unit TO alternate_quantity_unit")
            if products_existing and "quantity_unit" not in products_existing:
                conn.execute("ALTER TABLE products ADD COLUMN quantity_unit TEXT NOT NULL DEFAULT 'PCS'")

            # ---- v12: PURCHASE ORDERS + COMPANY LOGO ----
            # The purchase_orders/purchase_order_items tables themselves are
            # created by schema.sql (CREATE TABLE IF NOT EXISTS covers old
            # databases too); only the columns retrofitted onto existing
            # tables need guarded ALTERs here: a packing list can now be
            # generated from a purchase order (same "generated from"
            # reference pattern as proforma_invoice_id/quotation_id), and
            # Our Company gains an optional logo image.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "purchase_order_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN purchase_order_id INTEGER REFERENCES purchase_orders(id)")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "logo_path" not in existing:
                conn.execute("ALTER TABLE our_company ADD COLUMN logo_path TEXT")

            # ---- v13: BUYERS / SUPPLIERS / EXPORTERS REPLACE `clients` ----
            # Buyer, Supplier and Exporter become separate entities instead
            # of one `clients` table with a client_type discriminator.
            # Buyers/exporters keep the old shape verbatim (their ids are
            # preserved so every other table's reference to the old
            # clients.id keeps resolving with no remap); suppliers get an
            # our_company-shaped profile instead (GSTIN/PAN/IEC/bank/
            # contacts - no logo/BIN/LUT). Guarded on `clients` still
            # existing, so this runs exactly once per database.
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
            ).fetchone():
                conn.commit()
                self._backup_db_file("pre_client_split")

                # 1. Split every client row into its new home table.
                conn.execute("""
                    INSERT INTO buyers (id, company_id, lead_id, company_name, phone, email,
                                         facebook, instagram, other_social, address, status,
                                         created_by, created_at, updated_at)
                    SELECT id, company_id, lead_id, company_name, phone, email,
                           facebook, instagram, other_social, address, status,
                           created_by, created_at, updated_at
                    FROM clients WHERE client_type = 'Buyer'
                """)
                conn.execute("""
                    INSERT INTO exporters (id, company_id, lead_id, company_name, phone, email,
                                            facebook, instagram, other_social, address, status,
                                            created_by, created_at, updated_at)
                    SELECT id, company_id, lead_id, company_name, phone, email,
                           facebook, instagram, other_social, address, status,
                           created_by, created_at, updated_at
                    FROM clients WHERE client_type = 'Exporter'
                """)
                conn.execute("""
                    INSERT INTO suppliers (id, company_id, lead_id, company_name, address, status,
                                            created_by, created_at, updated_at)
                    SELECT id, company_id, lead_id, company_name, address, status,
                           created_by, created_at, updated_at
                    FROM clients WHERE client_type = 'Supplier'
                """)
                # A migrated supplier's phone/email is all it had - seed it
                # into supplier_contact_details, the same shape
                # our_company's own contact details already use.
                for row in conn.execute(
                    "SELECT id, phone, email FROM clients WHERE client_type = 'Supplier'"
                ).fetchall():
                    if row["phone"]:
                        conn.execute(
                            "INSERT INTO supplier_contact_details (supplier_id, type, value, is_primary) "
                            "VALUES (?, 'phone', ?, 1)",
                            (row["id"], row["phone"]),
                        )
                    if row["email"]:
                        conn.execute(
                            "INSERT INTO supplier_contact_details (supplier_id, type, value, is_primary) "
                            "VALUES (?, 'email', ?, 1)",
                            (row["id"], row["email"]),
                        )

                # 2. client_contacts -> party_contacts (buyer/exporter) or
                #    supplier_contact_persons (supplier - name only; phone/
                #    email don't fit that table's shape, same as
                #    our_company's own contact persons never carrying one).
                conn.execute("""
                    INSERT INTO party_contacts (parent_type, parent_id, name, phone, email, is_primary)
                    SELECT 'buyer', cc.client_id, cc.name, cc.phone, cc.email, cc.is_primary
                    FROM client_contacts cc JOIN clients c ON c.id = cc.client_id
                    WHERE c.client_type = 'Buyer'
                """)
                conn.execute("""
                    INSERT INTO party_contacts (parent_type, parent_id, name, phone, email, is_primary)
                    SELECT 'exporter', cc.client_id, cc.name, cc.phone, cc.email, cc.is_primary
                    FROM client_contacts cc JOIN clients c ON c.id = cc.client_id
                    WHERE c.client_type = 'Exporter'
                """)
                conn.execute("""
                    INSERT INTO supplier_contact_persons (supplier_id, name, is_primary)
                    SELECT cc.client_id, cc.name, cc.is_primary
                    FROM client_contacts cc JOIN clients c ON c.id = cc.client_id
                    WHERE c.client_type = 'Supplier'
                """)

                conn.commit()
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("PRAGMA legacy_alter_table = ON")

                # 3. communications: widen the parent_type CHECK from
                #    ('lead', 'client') to ('lead', 'buyer', 'supplier',
                #    'exporter') - a plain UPDATE can't do this alone since
                #    the OLD constraint would reject 'buyer'/'supplier'/
                #    'exporter' values, so this needs the same rebuild dance
                #    as payment_history/documents below. Guarded on the live
                #    table's own CHECK constraint text (not just column
                #    presence, which existed under the old shape too) so a
                #    database that already has the new shape - e.g. a retry
                #    after this step previously got interrupted - doesn't
                #    redo it; a stray `_old` table from such an interrupted
                #    attempt is dropped first so the rename below can't
                #    collide with it.
                comm_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='communications'"
                ).fetchone()
                if comm_row and "'buyer'" not in comm_row["sql"]:
                    conn.execute("DROP TABLE IF EXISTS communications_old")
                    conn.execute("ALTER TABLE communications RENAME TO communications_old")
                    conn.execute("""
                        CREATE TABLE communications (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            parent_type     TEXT NOT NULL CHECK (parent_type IN ('lead', 'buyer', 'supplier', 'exporter')),
                            parent_id       INTEGER NOT NULL,
                            employee_id     INTEGER NOT NULL REFERENCES users(id),
                            comm_date       TEXT NOT NULL,
                            mode            TEXT NOT NULL,
                            description     TEXT NOT NULL,
                            follow_up_date  TEXT,
                            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO communications (id, parent_type, parent_id, employee_id, comm_date,
                                                     mode, description, follow_up_date, created_at)
                        SELECT co.id,
                               CASE WHEN co.parent_type = 'lead' THEN 'lead' ELSE LOWER(c.client_type) END,
                               co.parent_id, co.employee_id, co.comm_date, co.mode, co.description,
                               co.follow_up_date, co.created_at
                        FROM communications_old co
                        LEFT JOIN clients c ON co.parent_type = 'client' AND c.id = co.parent_id
                        WHERE co.parent_type = 'lead' OR c.id IS NOT NULL
                    """)
                    conn.execute("DROP TABLE communications_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comms_parent ON communications(parent_type, parent_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comms_employee ON communications(employee_id)")

                # 4. payment_history / documents: add parent_type, rename
                #    client_id -> parent_id. Needs a full rebuild (adding a
                #    CHECK constraint can't be done with a plain ALTER),
                #    same rename-create-copy-drop dance used elsewhere here.
                #    Guarded on the old `client_id` column still being
                #    present (the new shape drops it entirely, unlike
                #    communications above where the column name doesn't
                #    change) - same "don't redo it, don't collide with a
                #    stray `_old` from an interrupted attempt" reasoning.
                ph_cols = {r["name"] for r in conn.execute("PRAGMA table_info(payment_history)")}
                if "client_id" in ph_cols:
                    conn.execute("DROP TABLE IF EXISTS payment_history_old")
                    conn.execute("ALTER TABLE payment_history RENAME TO payment_history_old")
                    conn.execute("""
                        CREATE TABLE payment_history (
                            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                            parent_type         TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier', 'exporter')),
                            parent_id           INTEGER NOT NULL,
                            account_name        TEXT NOT NULL,
                            payment_datetime    TEXT NOT NULL,
                            amount_original     REAL NOT NULL,
                            currency_code       TEXT NOT NULL,
                            conversion_rate     REAL NOT NULL,
                            amount_inr          REAL NOT NULL,
                            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO payment_history (id, parent_type, parent_id, account_name, payment_datetime,
                                                      amount_original, currency_code, conversion_rate, amount_inr, created_at)
                        SELECT ph.id, LOWER(c.client_type), ph.client_id, ph.account_name, ph.payment_datetime,
                               ph.amount_original, ph.currency_code, ph.conversion_rate, ph.amount_inr, ph.created_at
                        FROM payment_history_old ph JOIN clients c ON c.id = ph.client_id
                    """)
                    conn.execute("DROP TABLE payment_history_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_parent ON payment_history(parent_type, parent_id)")

                doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
                if "client_id" in doc_cols:
                    conn.execute("DROP TABLE IF EXISTS documents_old")
                    conn.execute("ALTER TABLE documents RENAME TO documents_old")
                    conn.execute("""
                        CREATE TABLE documents (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            parent_type     TEXT NOT NULL CHECK (parent_type IN ('buyer', 'supplier', 'exporter')),
                            parent_id       INTEGER NOT NULL,
                            document_name   TEXT NOT NULL,
                            document_type   TEXT NOT NULL,
                            document_date   TEXT NOT NULL,
                            notes           TEXT,
                            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                        )
                    """)
                    conn.execute("""
                        INSERT INTO documents (id, parent_type, parent_id, document_name, document_type,
                                                document_date, notes, created_at)
                        SELECT d.id, LOWER(c.client_type), d.client_id, d.document_name, d.document_type,
                               d.document_date, d.notes, d.created_at
                        FROM documents_old d JOIN clients c ON c.id = d.client_id
                    """)
                    conn.execute("DROP TABLE documents_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_type, parent_id)")

                # 5. leads: converted_client_id needs a converted_client_type
                #    alongside it now that there are three possible target
                #    tables instead of one. Guarded on that column's absence
                #    (same "don't redo it, don't collide with a stray `_old`"
                #    reasoning as the rebuilds above).
                leads_cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
                if "converted_client_type" not in leads_cols:
                    conn.execute("DROP TABLE IF EXISTS leads_old")
                    conn.execute("ALTER TABLE leads RENAME TO leads_old")
                    conn.execute("""
                        CREATE TABLE leads (
                            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id              INTEGER NOT NULL REFERENCES tenants(id),
                            company_name            TEXT NOT NULL,
                            phone                   TEXT NOT NULL,
                            email                   TEXT NOT NULL,
                            facebook                TEXT,
                            instagram               TEXT,
                            other_social            TEXT,
                            status                  TEXT NOT NULL DEFAULT 'new'
                                                    CHECK (status IN (
                                                        'new', 'in_communication', 'in_follow_up',
                                                        'long_follow_up', 'quotation_submission_pending', 'in_client'
                                                    )),
                            created_by              INTEGER NOT NULL REFERENCES users(id),
                            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            is_converted            INTEGER NOT NULL DEFAULT 0,
                            converted_client_type   TEXT CHECK (converted_client_type IN ('Buyer', 'Supplier', 'Exporter')),
                            converted_client_id     INTEGER
                        )
                    """)
                    conn.execute("""
                        INSERT INTO leads (id, company_id, company_name, phone, email, facebook, instagram,
                                            other_social, status, created_by, created_at, updated_at,
                                            is_converted, converted_client_type, converted_client_id)
                        SELECT lo.id, lo.company_id, lo.company_name, lo.phone, lo.email, lo.facebook, lo.instagram,
                               lo.other_social, lo.status, lo.created_by, lo.created_at, lo.updated_at,
                               lo.is_converted, c.client_type, lo.converted_client_id
                        FROM leads_old lo LEFT JOIN clients c ON c.id = lo.converted_client_id
                    """)
                    conn.execute("DROP TABLE leads_old")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_by ON leads(created_by)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id)")

                # 6. purchase_orders.seller_client_id -> seller_supplier_id.
                #    A PO's seller was always meant to be a Supplier - any
                #    legacy row pointing at a non-Supplier client is stale
                #    test data, so it's nulled out rather than carried into
                #    the wrong table.
                po_cols = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_orders)")}
                if "seller_client_id" in po_cols:
                    conn.execute("DROP TABLE IF EXISTS purchase_orders_old")
                    conn.execute("ALTER TABLE purchase_orders RENAME TO purchase_orders_old")
                    conn.execute("""
                        CREATE TABLE purchase_orders (
                            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                            company_id              INTEGER NOT NULL REFERENCES tenants(id),
                            po_number               TEXT NOT NULL,
                            po_date                 TEXT NOT NULL,
                            lead_id                 INTEGER REFERENCES leads(id),
                            proforma_invoice_id     INTEGER REFERENCES proforma_invoices(id),
                            seller_supplier_id      INTEGER REFERENCES suppliers(id),
                            seller_name             TEXT NOT NULL,
                            seller_address          TEXT,
                            seller_pan              TEXT,
                            seller_gstin            TEXT,
                            seller_ref_no           TEXT,
                            port_of_loading         TEXT,
                            port_of_discharge       TEXT,
                            container_details       TEXT,
                            delivery_time           TEXT,
                            advance_percent         TEXT,
                            payment_terms           TEXT,
                            remarks                 TEXT,
                            igst_percent            REAL NOT NULL DEFAULT 0,
                            cgst_percent            REAL NOT NULL DEFAULT 0,
                            sgst_percent            REAL NOT NULL DEFAULT 0,
                            created_by              INTEGER NOT NULL REFERENCES users(id),
                            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
                            UNIQUE (company_id, po_number)
                        )
                    """)
                    conn.execute("""
                        INSERT INTO purchase_orders (id, company_id, po_number, po_date, lead_id,
                            proforma_invoice_id, seller_supplier_id, seller_name, seller_address, seller_pan,
                            seller_gstin, seller_ref_no, port_of_loading, port_of_discharge, container_details,
                            delivery_time, advance_percent, payment_terms, remarks,
                            igst_percent, cgst_percent, sgst_percent, created_by, created_at, updated_at)
                        SELECT po.id, po.company_id, po.po_number, po.po_date, po.lead_id,
                            po.proforma_invoice_id,
                            CASE WHEN po.seller_client_id IN (SELECT id FROM suppliers) THEN po.seller_client_id ELSE NULL END,
                            po.seller_name, po.seller_address, po.seller_pan,
                            po.seller_gstin, po.seller_ref_no, po.port_of_loading, po.port_of_discharge, po.container_details,
                            po.delivery_time, po.advance_percent, po.payment_terms, po.remarks,
                            po.igst_percent, po.cgst_percent, po.sgst_percent, po.created_by, po.created_at, po.updated_at
                        FROM purchase_orders_old po
                    """)
                    conn.execute("DROP TABLE purchase_orders_old")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_company ON purchase_orders(company_id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_created_by ON purchase_orders(created_by)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_date ON purchase_orders(po_date)")

                # Safety net: an earlier interrupted attempt (e.g. the
                # process killed mid-migration) can leave a stray `_old`
                # table behind that this run's per-step guards above didn't
                # touch, because the live table already had the new shape
                # (so that step decided there was nothing to redo). Drop it
                # now if it's empty; if it still holds rows, a previous
                # attempt's copy never finished, and silently discarding
                # that data is worse than a loud failure - surface it
                # instead of guessing.
                for stray in ("communications_old", "payment_history_old", "documents_old",
                              "leads_old", "purchase_orders_old"):
                    if not conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (stray,)
                    ).fetchone():
                        continue
                    count = conn.execute(f"SELECT COUNT(*) AS c FROM {stray}").fetchone()["c"]
                    if count == 0:
                        conn.execute(f"DROP TABLE {stray}")
                    else:
                        raise RuntimeError(
                            f"Migration safety check: leftover table '{stray}' from an earlier "
                            f"interrupted migration attempt still holds {count} row(s) that were "
                            f"never copied into its replacement - refusing to drop it silently. "
                            f"Inspect it manually before removing it."
                        )

                # 7. clients / client_contacts are now fully migrated away.
                conn.execute("DROP TABLE IF EXISTS client_contacts")
                conn.execute("DROP TABLE IF EXISTS clients")

                conn.execute("PRAGMA legacy_alter_table = OFF")
                conn.execute("PRAGMA foreign_keys = ON")

            # Unconditional (unlike the block above, which only fires once
            # per legacy DB): a fresh install's schema.sql already creates
            # payment_history/documents with parent_type, so these indexes
            # need to exist either way.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_parent ON payment_history(parent_type, parent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_type, parent_id)")

            # v15: a purchase order is now placed under a purchase type
            # ('full_tax' | 'exemption') which derives its GST percentages,
            # instead of the percentages being typed in. Existing POs keep
            # the percentages already stored on them and are treated as
            # full-tax purchases - re-saving one recomputes them.
            # (Must stay AFTER the v13 block above, which rebuilds
            # purchase_orders from scratch in its pre-v15 shape.)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_orders)")}
            if existing and "purchase_type" not in existing:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN purchase_type TEXT NOT NULL DEFAULT 'full_tax'")

            # v16: a proforma invoice is now either a draft or confirmed.
            # Existing invoices stay drafts (freely editable, no reminder) -
            # confirming one is always an explicit action on the PI page.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoices)")}
            if existing and "status" not in existing:
                conn.execute("ALTER TABLE proforma_invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")

            # v18: a packing list can now also be generated from a Purchase
            # Invoice (that invoice's own PL, importing its linked PO's PL
            # wholesale) - a plain nullable FK, same "generated from"
            # reference-only pattern as purchase_order_id above it. The
            # index can't live in schema.sql's unconditional block (an old
            # DB's packing_lists table won't have the column yet when that
            # block runs), so it's created here too.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "purchase_invoice_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN purchase_invoice_id INTEGER REFERENCES purchase_invoices(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_packing_lists_purchase_invoice ON packing_lists(purchase_invoice_id)")

            # v19: quotation_items gets a Plts column, same as the other
            # three document types' items already have.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotation_items)")}
            if existing and "pallets" not in existing:
                conn.execute("ALTER TABLE quotation_items ADD COLUMN pallets REAL")

            # v20: suppliers gets an optional CIN/LLPIN registration number,
            # shown alongside GSTIN on the profile.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(suppliers)")}
            if existing and "cin_llp_no" not in existing:
                conn.execute("ALTER TABLE suppliers ADD COLUMN cin_llp_no TEXT")

            # v21: the Export Invoice document. Its own tables are wholly new
            # (created by schema.sql's CREATE TABLE IF NOT EXISTS, no migration
            # needed), but the two Our-Company columns it reads from must be
            # retrofitted onto the existing our_company / contact-persons
            # tables here. The standard self-sealing declaration text is left
            # NULL so the user fills it in under Our Company settings.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "self_sealing_declaration" not in existing:
                conn.execute("ALTER TABLE our_company ADD COLUMN self_sealing_declaration TEXT")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company_contact_persons)")}
            if existing and "designation" not in existing:
                conn.execute("ALTER TABLE our_company_contact_persons ADD COLUMN designation TEXT")

            # v22: quotation_items gets a quantity_unit column, snapshotting
            # products.quantity_unit the same way `unit` already snapshots
            # alternate_quantity_unit.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotation_items)")}
            if existing and "quantity_unit" not in existing:
                conn.execute("ALTER TABLE quotation_items ADD COLUMN quantity_unit TEXT NOT NULL DEFAULT 'PCS'")

            # v23: the same quantity_unit column, extended to the other three
            # document types' item tables (Quotation got it in v22).
            for table in ("proforma_invoice_items", "purchase_order_items",
                          "export_invoice_items", "packing_list_items"):
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if existing and "quantity_unit" not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN quantity_unit TEXT NOT NULL DEFAULT 'PCS'")

            # v24: a permit ("Permission", under Our Company) is no longer
            # tied to a supplier - it now records a stuffing-place number
            # (shown right before place_of_stuffing) instead. Add the new
            # column and drop supplier_id. Guarded by the live column list so
            # it runs exactly once and is safe to re-run (both ALTERs are
            # skipped once the table is already in the new shape).
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(permits)")}
            if existing:
                if "stuffing_place_number" not in existing and "stuffing_place_name" not in existing:
                    conn.execute("ALTER TABLE permits ADD COLUMN stuffing_place_number TEXT")
                if "supplier_id" in existing:
                    conn.execute("ALTER TABLE permits DROP COLUMN supplier_id")

            # v25: permits.stuffing_place_number renamed to stuffing_place_name
            # (it holds a name, not a number). Guarded so it runs once and is
            # safe to re-run.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(permits)")}
            if existing and "stuffing_place_number" in existing and "stuffing_place_name" not in existing:
                conn.execute("ALTER TABLE permits RENAME COLUMN stuffing_place_number TO stuffing_place_name")

            # v26: export_invoices gains buyer_order_no/buyer_order_date
            # (Buyer Order No & Date, once a repeatable per-PI list backed by
            # its own export_invoice_buyer_orders table, is now these two
            # plain fields shared across every linked PI) plus booking_no
            # (the shipping line booking number printed above the 11B
            # container table). Any server whose export_invoices table was
            # first created before schema.sql grew these columns never got
            # them - CREATE TABLE IF NOT EXISTS is a no-op on an existing
            # table, so this closes that gap explicitly. Once the columns are
            # in place, any rows still sitting in the short-lived v21
            # export_invoice_buyer_orders table are folded into them (first
            # order per invoice wins, since one EI now carries a single buyer
            # order) and that table is dropped - separately guarded on the
            # table's existence so this is safe to re-run.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing:
                if "buyer_order_no" not in existing:
                    conn.execute("ALTER TABLE export_invoices ADD COLUMN buyer_order_no TEXT")
                if "buyer_order_date" not in existing:
                    conn.execute("ALTER TABLE export_invoices ADD COLUMN buyer_order_date TEXT")
                if "booking_no" not in existing:
                    conn.execute("ALTER TABLE export_invoices ADD COLUMN booking_no TEXT")

                old_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='export_invoice_buyer_orders'"
                ).fetchone()
                if old_table:
                    rows = conn.execute(
                        "SELECT export_invoice_id, order_no, order_date FROM export_invoice_buyer_orders "
                        "ORDER BY export_invoice_id, sr_no"
                    ).fetchall()
                    seen = set()
                    for r in rows:
                        if r["export_invoice_id"] in seen:
                            continue
                        seen.add(r["export_invoice_id"])
                        conn.execute(
                            "UPDATE export_invoices SET buyer_order_no = ?, buyer_order_date = ? WHERE id = ?",
                            (r["order_no"], r["order_date"], r["export_invoice_id"]),
                        )
                    conn.execute("DROP TABLE export_invoice_buyer_orders")

            # v27: export_invoice_container_details gains tare_weight - the
            # 11B container table's Tare Weight column was print-only
            # (always blank); it's now a typed-in field per container row.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing and "tare_weight" not in existing:
                conn.execute("ALTER TABLE export_invoice_container_details ADD COLUMN tare_weight TEXT")

            # v28: export_invoice_container_details gains gross_weight/net_weight
            # - shown on the 11B table and stored, but (unlike tare_weight)
            # with no form input, so they stay blank unless set some other way.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing:
                if "gross_weight" not in existing:
                    conn.execute("ALTER TABLE export_invoice_container_details ADD COLUMN gross_weight TEXT")
                if "net_weight" not in existing:
                    conn.execute("ALTER TABLE export_invoice_container_details ADD COLUMN net_weight TEXT")

            # v29: export_invoices gains stuffing_location (the "Stuff At"
            # address printed on the export packing list). It was added to
            # schema.sql only, so a table created before that never got it -
            # the same gap v26 closed for buyer_order_no/booking_no, and the
            # reason repositories.py's export-invoice INSERT/UPDATE (which
            # names the column explicitly) failed on an older database.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "stuffing_location" not in existing:
                conn.execute("ALTER TABLE export_invoices ADD COLUMN stuffing_location TEXT")

            # v30: closing out the remaining Export Invoice cells missing
            # against the reference PDF - export_invoices gains
            # total_net_weight_kg/total_gross_weight_kg (front-page weight
            # totals, typed not summed) and c_no/c_date/shipping_bill_no (the
            # annexure's header row above the examination report title);
            # export_invoice_purchase_details gains
            # supplier_invoice_qty/supplier_taxable_amount/supplier_cgst_amount/
            # supplier_sgst_amount (the reference's Purchase Details block has
            # six rows, we only had two); export_invoice_container_details
            # gained excise_seal_no/plts/boxes (dropped again in v37);
            # our_company gains branch_code (section 2B, IEC branch code).
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing:
                for col in ("total_net_weight_kg", "total_gross_weight_kg"):
                    if col not in existing:
                        conn.execute(f"ALTER TABLE export_invoices ADD COLUMN {col} REAL")
                # c_no/c_date and the purchase-details supplier amount columns
                # this step used to add were dropped again in v33 - adding them
                # here would only make v33 drop them right back.
                if "shipping_bill_no" not in existing:
                    conn.execute("ALTER TABLE export_invoices ADD COLUMN shipping_bill_no TEXT")

            # (the excise_seal_no/plts/boxes columns this step used to add on
            # export_invoice_container_details were dropped again in v37 -
            # adding them here would only make v37 drop them right back.)

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "branch_code" not in existing:
                conn.execute("ALTER TABLE our_company ADD COLUMN branch_code TEXT")

            # v31: export_invoices gains shipping_bill_date (the
            # stuffing_start_time/stuffing_completion_time pair this step used
            # to add alongside it was dropped again in v33).
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "shipping_bill_date" not in existing:
                conn.execute("ALTER TABLE export_invoices ADD COLUMN shipping_bill_date TEXT")

            # v32: our_company gains a free-text government_schemes field -
            # see the changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(our_company)")}
            if existing and "government_schemes" not in existing:
                conn.execute("ALTER TABLE our_company ADD COLUMN government_schemes TEXT")

            # v33: the C No./C date header pair, the Time Of Stuffing
            # starting/completion pair and the four supplier amount columns on
            # the purchase-details rows are gone from the form, the sheets and
            # the model - see the changelog entry above. Dropped rather than
            # left dangling so the table matches schema.sql on both fresh and
            # migrated installs. Guarded per column so a half-finished run
            # (debug reloader) can resume.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            dead_ei = [c for c in ("c_no", "c_date", "stuffing_start_time", "stuffing_completion_time")
                       if c in existing]
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_purchase_details)")}
            dead_pd = [c for c in ("supplier_invoice_qty", "supplier_taxable_amount",
                                   "supplier_cgst_amount", "supplier_sgst_amount") if c in existing]
            if dead_ei or dead_pd:
                conn.commit()  # so the copy below is a consistent snapshot
                self._backup_db_file("pre_v33_drop_annexure_fields")
                for col in dead_ei:
                    conn.execute(f"ALTER TABLE export_invoices DROP COLUMN {col}")
                for col in dead_pd:
                    conn.execute(f"ALTER TABLE export_invoice_purchase_details DROP COLUMN {col}")

            # v34: the export invoice's Currency cell is picked from the
            # Administration -> Miscellaneous currency list instead of being
            # hard-coded "USD [ $ ]" on the sheets, and the chosen name +
            # symbol are snapshotted onto the invoice (misc_currencies itself
            # is a brand-new table, so schema.sql creates it).
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            for column in ("currency_code", "currency_symbol"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE export_invoices ADD COLUMN {column} TEXT")

            # v36: the same pair on every other priced document - see the
            # changelog entry above.
            for table in ("quotations", "proforma_invoices", "purchase_orders", "purchase_invoices"):
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for column in ("currency_code", "currency_symbol"):
                    if existing and column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

            # v37: the 11B container table's Excise seal no./Plts/Boxes inputs
            # are gone from the form and the model - see the changelog entry
            # above. Dropped (same treatment as v33) so the table matches
            # schema.sql on both fresh and migrated installs, guarded per
            # column so a half-finished run can resume.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            dead_cd = [c for c in ("excise_seal_no", "plts", "boxes") if c in existing]
            if dead_cd:
                conn.commit()  # so the copy below is a consistent snapshot
                self._backup_db_file("pre_v37_drop_container_detail_fields")
                for col in dead_cd:
                    conn.execute(f"ALTER TABLE export_invoice_container_details DROP COLUMN {col}")

            # v39/v49: quotations (v39), then proforma and export invoices too
            # (v49), can be priced FOB-first - see the changelog entries above.
            # Existing documents keep fob_pricing = 0 (prices typed are already
            # CIF, exactly as before), round_off = 0, and their items keep a
            # NULL fob_price_usd, so nothing already saved reprices itself.
            for table in ("quotations", "proforma_invoices", "export_invoices"):
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if existing and "fob_pricing" not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN fob_pricing INTEGER NOT NULL DEFAULT 0")
                if existing and "round_off" not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN round_off REAL NOT NULL DEFAULT 0")
            for table in ("quotation_items", "proforma_invoice_items", "export_invoice_items"):
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if existing and "fob_price_usd" not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN fob_price_usd REAL")

            # v41: export_invoices gains vessel_voyage_no - see the changelog
            # entry above. Existing invoices keep it NULL, so their sheets go
            # on printing "N/A" in the Vessel / Flight Name & No cell exactly
            # as before until someone fills it in.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "vessel_voyage_no" not in existing:
                conn.execute("ALTER TABLE export_invoices ADD COLUMN vessel_voyage_no TEXT")

            # v42: the 11B container table gains LR no. / transporter name /
            # max permitted weight - see the changelog entry above. Guarded per
            # column so a half-finished run can resume; existing rows keep them
            # NULL and go on behaving exactly as before.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing:
                for column in ("lr_no", "transporter_name", "max_permitted_weight"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE export_invoice_container_details ADD COLUMN {column} TEXT")

            # v43: export_invoices gains eway_bill_no/eway_bill_date, the two
            # cells the Tax Invoice attachment needs that nothing else on the
            # invoice already held - see the changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing:
                for column in ("eway_bill_no", "eway_bill_date"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE export_invoices ADD COLUMN {column} TEXT")

            # v44: the Tax Invoice attachment's own number/date - see the
            # changelog entry above. Left NULL on every existing invoice, which
            # means "use the export invoice's own", exactly as before.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing:
                for column in ("tax_invoice_number", "tax_invoice_date"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE export_invoices ADD COLUMN {column} TEXT")

            # v45: the VGM attachment's two typed cells, per physical
            # container - see the changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing:
                for column in ("weighbridge_name", "weighing_slip_no"):
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE export_invoice_container_details ADD COLUMN {column} TEXT")

            # v46: the VGM declaration's manual-entry cells - see the changelog
            # entry above. NULL on every existing invoice, which means "use the
            # default", so the sheet prints in full without anyone typing.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing:
                for column in ("vgm_signatory", "vgm_contact_24x7", "vgm_weighing_method",
                               "vgm_cargo_type", "vgm_hazardous_details"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE export_invoices ADD COLUMN {column} TEXT")

            # v47: the E-Seal sheet's two typed cells, per container - see the
            # changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing:
                for column in ("sealing_time", "sealing_date"):
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE export_invoice_container_details ADD COLUMN {column} TEXT")

            # v48: the commercial invoice packing list's bill of lading pair -
            # see the changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing:
                for column in ("bill_of_lading_no", "bill_of_lading_date"):
                    if column not in existing:
                        conn.execute(f"ALTER TABLE export_invoices ADD COLUMN {column} TEXT")

            # v50: export_invoices.permission_is_one_time - see the changelog
            # entry above. Defaults to 0 on every existing invoice, which
            # keeps printing the (blank) expiry date exactly as before.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "permission_is_one_time" not in existing:
                conn.execute(
                    "ALTER TABLE export_invoices ADD COLUMN permission_is_one_time INTEGER NOT NULL DEFAULT 0")

            # v51: quotations.cif_adjust_usd - see the changelog entry above.
            # Defaults to 0 on every existing quotation, which keeps
            # totalling exactly as the ladder computes.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
            if existing and "cif_adjust_usd" not in existing:
                conn.execute(
                    "ALTER TABLE quotations ADD COLUMN cif_adjust_usd REAL NOT NULL DEFAULT 0")

            # v52: export_invoice_container_details.tare_weight (free TEXT,
            # never validated) becomes tare_weight_kg (REAL) - the 11B Tare
            # Weight cell is now checked to actually be a number when typed.
            # A plain ALTER ... RENAME COLUMN would keep the old TEXT
            # affinity, so instead: add the new column, carry over whatever
            # of the old column's values parse as a number (blank/unparsable
            # ones are dropped rather than guessed at), then drop the old one.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing and "tare_weight" in existing and "tare_weight_kg" not in existing:
                conn.execute("ALTER TABLE export_invoice_container_details ADD COLUMN tare_weight_kg REAL")
                rows = conn.execute(
                    "SELECT id, tare_weight FROM export_invoice_container_details "
                    "WHERE tare_weight IS NOT NULL AND TRIM(tare_weight) != ''").fetchall()
                for r in rows:
                    try:
                        value = float(r["tare_weight"])
                    except (TypeError, ValueError):
                        continue
                    conn.execute(
                        "UPDATE export_invoice_container_details SET tare_weight_kg = ? WHERE id = ?",
                        (value, r["id"]))
                conn.execute("ALTER TABLE export_invoice_container_details DROP COLUMN tare_weight")

            # v53: proforma_invoices.packing_details (new nullable column) -
            # the same "Packing" free-text field quotations already carry
            # (e.g. "PALLATE"), now also on the Proforma Invoice so its
            # printed sheet can show a Packing cell the way quotations do.
            # NULL on every existing invoice, which prints as blank exactly
            # like a quotation with no packing details typed.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoices)")}
            if existing and "packing_details" not in existing:
                conn.execute("ALTER TABLE proforma_invoices ADD COLUMN packing_details TEXT")

            # v54: quotations.final_destination (new nullable column) - the
            # same field proforma_invoices already carries, now also on the
            # Quotation so its printed sheet can show a Final Destination
            # cell the way proforma invoices do. NULL on every existing
            # quotation, which prints as blank exactly like a proforma
            # invoice with no final destination typed.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
            if existing and "final_destination" not in existing:
                conn.execute("ALTER TABLE quotations ADD COLUMN final_destination TEXT")

            # v55: the Exporter party type is retired - Buyer and Exporter
            # were always identically-shaped (see the v13 changelog entry
            # above), and Exporter never grew its own document types, so it's
            # dropped outright rather than merged into Buyer. Any existing
            # exporters row and its party_contacts/communications/
            # payment_history/documents rows (parent_type='exporter') go with
            # it - backed up first since this one is a real DROP TABLE, not
            # just an unused column coming out. leads.converted_client_type/
            # party_contacts.parent_type/communications.parent_type/
            # payment_history.parent_type/documents.parent_type keep
            # 'Exporter'/'exporter' in their CHECK constraints on an existing
            # database (SQLite can't ALTER a CHECK without a full table
            # rebuild) - harmless, since nothing in the app writes that value
            # anymore.
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "exporters" in tables:
                conn.commit()  # so the copy below is a consistent snapshot
                self._backup_db_file("pre_v55_drop_exporters")
                conn.execute("DELETE FROM party_contacts WHERE parent_type = 'exporter'")
                conn.execute("DELETE FROM communications WHERE parent_type = 'exporter'")
                conn.execute("DELETE FROM payment_history WHERE parent_type = 'exporter'")
                conn.execute("DELETE FROM documents WHERE parent_type = 'exporter'")
                conn.execute("DROP TABLE exporters")

            # v56: export_invoices.vessel_voyage_no (free TEXT, one field for
            # both vessel/flight name and voyage number) splits into
            # vessel_name and voyage_no - the Container details card had one
            # input covering both, typed like "MSC ANNA / VOY 214W". A plain
            # rename can't split it, so instead: add both new columns, carry
            # the old value over wholesale into vessel_name (there's no
            # reliable separator to split an already-free-typed value on),
            # leaving voyage_no blank, then drop the old column. Every sheet
            # keeps printing exactly what it did before - see
            # ExportInvoice.vessel_voyage_no, the read-only property that
            # rejoins the two for the "Vessel / Flight Name & No" cell.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "vessel_voyage_no" in existing and "vessel_name" not in existing:
                conn.execute("ALTER TABLE export_invoices ADD COLUMN vessel_name TEXT")
                conn.execute("ALTER TABLE export_invoices ADD COLUMN voyage_no TEXT")
                conn.execute(
                    "UPDATE export_invoices SET vessel_name = vessel_voyage_no "
                    "WHERE vessel_voyage_no IS NOT NULL AND TRIM(vessel_voyage_no) != ''")
                conn.execute("ALTER TABLE export_invoices DROP COLUMN vessel_voyage_no")

            # v57: booking_detail_container_details.container_type (new
            # nullable column) - a row added via the per-container-type
            # "Upload from Excel" button is tagged with that row's container
            # type, shown in the 11B table's new Container type column.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(booking_detail_container_details)")}
            if existing and "container_type" not in existing:
                conn.execute("ALTER TABLE booking_detail_container_details ADD COLUMN container_type TEXT")

            # v58: export_invoice_container_details.container_type predates
            # this version's schema.sql on any server whose table was created
            # before that column existed there - see the v58 changelog entry.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_container_details)")}
            if existing and "container_type" not in existing:
                conn.execute("ALTER TABLE export_invoice_container_details ADD COLUMN container_type TEXT")

            # v61: misc_hsn_codes.related_products (new nullable column) - the
            # "Related to Products" note sitting between HSN Code and GST Slab
            # on that Miscellaneous card, saying in words what the code covers
            # (e.g. GLAZED VITRIFIED TILES). misc_hsn_codes itself arrived in
            # v60, so a database created at that version already has the table
            # and CREATE TABLE IF NOT EXISTS won't add the column to it - hence
            # this step. NULL on every existing row, which shows as blank.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(misc_hsn_codes)")}
            if existing and "related_products" not in existing:
                conn.execute("ALTER TABLE misc_hsn_codes ADD COLUMN related_products TEXT")

            # v62: purchase_invoice_items.purchase_order_id (new nullable
            # column) + purchase_invoice_purchase_order_links (new table) - a
            # purchase invoice can now be raised against several purchase
            # orders of the same supplier at once. The link table is brand
            # new so schema.sql alone creates it; the item column needs the
            # usual guarded ALTER. Both are backfilled once from the existing
            # single purchase_order_id on each invoice's own header - the
            # link-table backfill is guarded on the table still being empty
            # (rather than column presence, since the table itself is never
            # missing) so a link removed on purpose later is never silently
            # re-added on a subsequent startup.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_invoice_items)")}
            if existing and "purchase_order_id" not in existing:
                conn.execute("ALTER TABLE purchase_invoice_items ADD COLUMN purchase_order_id INTEGER REFERENCES purchase_orders(id)")
                conn.execute("""
                    UPDATE purchase_invoice_items
                    SET purchase_order_id = (
                        SELECT purchase_order_id FROM purchase_invoices
                        WHERE purchase_invoices.id = purchase_invoice_items.purchase_invoice_id
                    )
                """)
            # The column is guaranteed to exist by this point (either from a
            # fresh install's schema.sql, or the ALTER just above) - unlike
            # schema.sql's own static index block, which runs BEFORE this
            # migration step and would fail with "no such column" on a
            # database that still had the old-shape table at that point.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_invoice_items_po ON purchase_invoice_items(purchase_order_id)")
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_invoice_purchase_order_links'"
            ).fetchone() and not conn.execute("SELECT id FROM purchase_invoice_purchase_order_links LIMIT 1").fetchone():
                conn.execute("""
                    INSERT INTO purchase_invoice_purchase_order_links (purchase_invoice_id, purchase_order_id)
                    SELECT id, purchase_order_id FROM purchase_invoices WHERE purchase_order_id IS NOT NULL
                """)

            # v63: export_invoice_purchase_details.supplier_name (new nullable
            # column) - the Purchase details card on the Export Invoice form
            # shows WHO each supplier GSTIN/invoice-no pair belongs to, beside
            # the invoice number. Deliberately NOT backfilled: the name is a
            # snapshot of what the purchase invoice said at import time, and
            # reconstructing it now would walk today's supplier records rather
            # than the ones those rows were imported from. Existing rows stay
            # NULL (blank in the form) until the invoice is re-loaded from its
            # PIs, exactly as they show today.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_purchase_details)")}
            if existing and "supplier_name" not in existing:
                conn.execute("ALTER TABLE export_invoice_purchase_details ADD COLUMN supplier_name TEXT")

            # v64: quotations.container_details (free TEXT) -> quotation_containers
            # (new table) - see the SCHEMA_VERSION changelog entry above.
            # quotation_containers is brand new so schema.sql alone creates it
            # on a fresh install; here it's guarded the same way so an older
            # database gets it too. Any quotation whose old text field held a
            # value gets one row carried over (count 1, type = the old text)
            # before that column is dropped, so its printed sheet keeps
            # showing something rather than going blank.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quotation_containers (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    quotation_id        INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
                    sr_no               INTEGER NOT NULL,
                    container_type      TEXT NOT NULL,
                    container_count     INTEGER NOT NULL DEFAULT 0
                )
            """)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(quotations)")}
            if existing and "container_details" in existing:
                conn.execute("""
                    INSERT INTO quotation_containers (quotation_id, sr_no, container_type, container_count)
                    SELECT id, 1, TRIM(container_details), 1 FROM quotations
                    WHERE container_details IS NOT NULL AND TRIM(container_details) != ''
                """)
                conn.execute("ALTER TABLE quotations DROP COLUMN container_details")

            # v65: proforma_invoices.container_details (free TEXT) ->
            # proforma_invoice_containers (new table) - same treatment as v64
            # gave quotations, see the SCHEMA_VERSION changelog entry above.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proforma_invoice_containers (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    proforma_invoice_id    INTEGER NOT NULL REFERENCES proforma_invoices(id) ON DELETE CASCADE,
                    sr_no                  INTEGER NOT NULL,
                    container_type         TEXT NOT NULL,
                    container_count        INTEGER NOT NULL DEFAULT 0
                )
            """)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(proforma_invoices)")}
            if existing and "container_details" in existing:
                conn.execute("""
                    INSERT INTO proforma_invoice_containers (proforma_invoice_id, sr_no, container_type, container_count)
                    SELECT id, 1, TRIM(container_details), 1 FROM proforma_invoices
                    WHERE container_details IS NOT NULL AND TRIM(container_details) != ''
                """)
                conn.execute("ALTER TABLE proforma_invoices DROP COLUMN container_details")

            # v67: drop the per-document lead_id column from proforma_invoices,
            # purchase_orders and packing_lists - see the SCHEMA_VERSION
            # changelog entry above. Quotation keeps its own lead_id untouched.
            for table in ("proforma_invoices", "purchase_orders", "packing_lists"):
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if existing and "lead_id" in existing:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN lead_id")

            # v68: misc_countries (new table) + buyers.country - see the
            # SCHEMA_VERSION changelog entry above. misc_countries is brand
            # new so schema.sql alone creates it on a fresh install; guarded
            # here too so an older database gets it.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS misc_countries (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id   INTEGER NOT NULL REFERENCES tenants(id),
                    name         TEXT NOT NULL,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (company_id, name)
                )
            """)
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(buyers)")}
            if existing and "country" not in existing:
                conn.execute("ALTER TABLE buyers ADD COLUMN country TEXT")

            # v69: purchase_invoices.purchase_type - see the SCHEMA_VERSION
            # changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_invoices)")}
            if existing and "purchase_type" not in existing:
                conn.execute("ALTER TABLE purchase_invoices ADD COLUMN purchase_type TEXT NOT NULL DEFAULT 'full_tax'")

            # v70: export_invoice_purchase_details.purchase_type - see the
            # SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_purchase_details)")}
            if existing and "purchase_type" not in existing:
                conn.execute(
                    "ALTER TABLE export_invoice_purchase_details ADD COLUMN purchase_type TEXT NOT NULL DEFAULT 'full_tax'"
                )

            # v71: misc_units (new table) - see the SCHEMA_VERSION changelog
            # entry above. misc_units is brand new so schema.sql alone
            # creates it on a fresh install; guarded here too so an older
            # database gets it.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS misc_units (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id   INTEGER NOT NULL REFERENCES tenants(id),
                    name         TEXT NOT NULL,
                    meaning      TEXT NOT NULL,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (company_id, name)
                )
            """)

            # v72: product_pallet_types.weight_kg - see the SCHEMA_VERSION
            # changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(product_pallet_types)")}
            if existing and "weight_kg" not in existing:
                conn.execute("ALTER TABLE product_pallet_types ADD COLUMN weight_kg REAL")

            # v73: export_invoice_items.pallet_weight_kg - see the
            # SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_items)")}
            if existing and "pallet_weight_kg" not in existing:
                conn.execute("ALTER TABLE export_invoice_items ADD COLUMN pallet_weight_kg REAL")

            # v74: export_invoice_purchase_details.epcg_number/epcg_date -
            # see the SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_purchase_details)")}
            if existing and "epcg_number" not in existing:
                conn.execute("ALTER TABLE export_invoice_purchase_details ADD COLUMN epcg_number TEXT")
            if existing and "epcg_date" not in existing:
                conn.execute("ALTER TABLE export_invoice_purchase_details ADD COLUMN epcg_date TEXT")

            # v75: purchase_orders.tax_as_actual (new column, default 0) -
            # see the SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_orders)")}
            if existing and "tax_as_actual" not in existing:
                conn.execute("ALTER TABLE purchase_orders ADD COLUMN tax_as_actual INTEGER NOT NULL DEFAULT 0")

            # v76: export_invoices.bill_of_lading_pdf_path - see the
            # SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "bill_of_lading_pdf_path" not in existing:
                conn.execute("ALTER TABLE export_invoices ADD COLUMN bill_of_lading_pdf_path TEXT")

            # v77: purchase_order_items.design_id/design_name +
            # export_invoice_items.design_id/design_name - see the
            # SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_order_items)")}
            if existing and "design_id" not in existing:
                conn.execute("ALTER TABLE purchase_order_items ADD COLUMN design_id INTEGER REFERENCES designs(id)")
            if existing and "design_name" not in existing:
                conn.execute("ALTER TABLE purchase_order_items ADD COLUMN design_name TEXT")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_items)")}
            if existing and "design_id" not in existing:
                conn.execute("ALTER TABLE export_invoice_items ADD COLUMN design_id INTEGER REFERENCES designs(id)")
            if existing and "design_name" not in existing:
                conn.execute("ALTER TABLE export_invoice_items ADD COLUMN design_name TEXT")

            # v78: export_invoice_items.design_id/design_name DROPPED +
            # export_packing_list_item_designs (new table) - see the
            # SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoice_items)")}
            if existing and "design_id" in existing:
                conn.execute("ALTER TABLE export_invoice_items DROP COLUMN design_id")
            if existing and "design_name" in existing:
                conn.execute("ALTER TABLE export_invoice_items DROP COLUMN design_name")
            conn.execute("""
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
                )
            """)

            # v79: export_designs_packing_lists - see the SCHEMA_VERSION
            # changelog entry above.
            conn.execute("""
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
                )
            """)

            # v80: job_works + job_work_items - see the SCHEMA_VERSION
            # changelog entry above.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_works (
                    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id               INTEGER NOT NULL REFERENCES tenants(id),
                    job_work_number          TEXT NOT NULL,
                    job_work_date            TEXT NOT NULL,
                    purchase_order_id        INTEGER REFERENCES purchase_orders(id) ON DELETE SET NULL,
                    seller_supplier_id       INTEGER REFERENCES suppliers(id),
                    seller_name              TEXT NOT NULL,
                    seller_address           TEXT,
                    seller_pan               TEXT,
                    seller_gstin             TEXT,
                    manufacturer_supplier_id INTEGER REFERENCES suppliers(id),
                    manufacturer_name        TEXT,
                    manufacturer_address     TEXT,
                    manufacturer_pan         TEXT,
                    manufacturer_gstin       TEXT,
                    seller_ref_no            TEXT,
                    delivery_time            TEXT,
                    advance_percent          TEXT,
                    payment_terms            TEXT,
                    remarks                  TEXT,
                    currency_code            TEXT,
                    currency_symbol          TEXT,
                    created_by               INTEGER NOT NULL REFERENCES users(id),
                    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (company_id, job_work_number)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_work_items (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_work_id      INTEGER NOT NULL REFERENCES job_works(id) ON DELETE CASCADE,
                    sr_no            INTEGER NOT NULL,
                    product_id       INTEGER REFERENCES products(id) ON DELETE SET NULL,
                    product_name     TEXT NOT NULL,
                    hsn_code         TEXT,
                    design_id        INTEGER REFERENCES designs(id) ON DELETE SET NULL,
                    design_name      TEXT,
                    quantity_boxes   REAL,
                    quantity_unit    TEXT NOT NULL DEFAULT 'PCS',
                    quantity_value   REAL NOT NULL DEFAULT 0,
                    unit             TEXT NOT NULL DEFAULT 'SQM',
                    job_quantity     REAL NOT NULL DEFAULT 0,
                    jobed_quantity   REAL
                )
            """)

            # v81: job_work_items.to_product_id/to_product_name/jobed_unit -
            # see the SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_work_items)")}
            if existing and "to_product_id" not in existing:
                conn.execute("ALTER TABLE job_work_items ADD COLUMN to_product_id INTEGER REFERENCES products(id)")
            if existing and "to_product_name" not in existing:
                conn.execute("ALTER TABLE job_work_items ADD COLUMN to_product_name TEXT")
            if existing and "jobed_unit" not in existing:
                conn.execute("ALTER TABLE job_work_items ADD COLUMN jobed_unit TEXT")

            # v85: Job Quantity / Jobed Qty are counted in the product's QTY
            # unit, not its alternate-quantity unit - see the SCHEMA_VERSION
            # changelog entry above. Guarded on the version the database is
            # arriving at, since this rewrites values rather than structure and
            # must not re-run over units edited by hand afterwards.
            if self.get_schema_version() < 85:
                existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_work_items)")}
                if existing:
                    conn.execute("""
                        UPDATE job_work_items SET unit = COALESCE(
                            (SELECT p.quantity_unit FROM products p WHERE p.id = job_work_items.product_id),
                            unit)
                    """)
                    conn.execute("""
                        UPDATE job_work_items SET jobed_unit = (
                            SELECT p.quantity_unit FROM products p WHERE p.id = job_work_items.to_product_id)
                        WHERE to_product_id IS NOT NULL
                    """)

            # v86: job_work_items rework (source_quantity/conversion_value/
            # extra_percent/converted_quantity/extra_quantity added,
            # jobed_quantity/jobed_unit dropped, hsn_code re-snapshotted from
            # to_product) - see the SCHEMA_VERSION changelog entry above.
            # Plain ALTERs: job_work_items is the CHILD in the cascade, so
            # unlike job_works itself this needs none of _rebuild_job_works'
            # foreign-key-off care.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_work_items)")}
            for column in ("source_quantity", "converted_quantity", "extra_quantity"):
                if existing and column not in existing:
                    conn.execute(f"ALTER TABLE job_work_items ADD COLUMN {column} REAL NOT NULL DEFAULT 0")
            if existing and "conversion_value" not in existing:
                conn.execute("ALTER TABLE job_work_items ADD COLUMN conversion_value REAL NOT NULL DEFAULT 1")
            if existing and "extra_percent" not in existing:
                conn.execute("ALTER TABLE job_work_items ADD COLUMN extra_percent REAL NOT NULL DEFAULT 0")
            # Best-effort forward migration of a job quantity someone already
            # typed under the old model: treat it as if conversion_value=1,
            # extra_percent=0 had produced it, so the new chain stays
            # internally consistent (converted_quantity = job_quantity,
            # extra_quantity = 0) instead of silently going to zero.
            if existing and "source_quantity" not in existing and "job_quantity" in existing:
                conn.execute("UPDATE job_work_items SET source_quantity = job_quantity, "
                             "converted_quantity = job_quantity")
            if existing and "jobed_quantity" in existing:
                conn.execute("ALTER TABLE job_work_items DROP COLUMN jobed_quantity")
            if existing and "jobed_unit" in existing:
                conn.execute("ALTER TABLE job_work_items DROP COLUMN jobed_unit")

            # v84: the proforma invoice's own quantity per design is no
            # longer carried onto a job work - see the SCHEMA_VERSION
            # changelog entry above. Plain DROP COLUMNs: job_work_items is the
            # CHILD in the cascade, so there is no table to drop here and none
            # of _rebuild_job_works' foreign-key care is needed.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_work_items)")}
            for column in ("quantity_boxes", "quantity_unit", "quantity_value"):
                if existing and column in existing:
                    conn.execute(f"ALTER TABLE job_work_items DROP COLUMN {column}")

            # v82/v83: job_works moves from the purchase order to the proforma
            # invoice, and its From Seller settles on a supplier - see the
            # SCHEMA_VERSION changelog entries above. Done on its own
            # connection with foreign keys OFF (_rebuild_job_works),
            # because this connection has them ON and DROP TABLE job_works
            # would then CASCADE straight through job_work_items' foreign key
            # and take every line of every job work with it.

            # Lives here rather than in schema.sql: on a pre-v82 database the
            # proforma_invoice_id column doesn't exist when that script runs.
            # (Guarded, since the rebuild below may not have run yet.)

            # v87: job_works.igst_percent/cgst_percent/sgst_percent/purchase_type/
            # tax_as_actual (new columns) + job_work_products (new table) - see
            # the SCHEMA_VERSION changelog entry above. Plain ALTERs: none of
            # these need the foreign-key-off rebuild dance since they are new
            # columns, not constraint changes.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_works)")}
            if existing and "igst_percent" not in existing:
                conn.execute("ALTER TABLE job_works ADD COLUMN igst_percent REAL NOT NULL DEFAULT 0")
            if existing and "cgst_percent" not in existing:
                conn.execute("ALTER TABLE job_works ADD COLUMN cgst_percent REAL NOT NULL DEFAULT 0")
            if existing and "sgst_percent" not in existing:
                conn.execute("ALTER TABLE job_works ADD COLUMN sgst_percent REAL NOT NULL DEFAULT 0")
            if existing and "purchase_type" not in existing:
                conn.execute("ALTER TABLE job_works ADD COLUMN purchase_type TEXT NOT NULL DEFAULT 'full_tax'")
            if existing and "tax_as_actual" not in existing:
                conn.execute("ALTER TABLE job_works ADD COLUMN tax_as_actual INTEGER NOT NULL DEFAULT 0")
            conn.execute("""
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
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_work_products_job_work ON job_work_products(job_work_id)"
            )

            # v88: a packing list can now be generated directly from a Job
            # Work - same "generated from" reference pattern as
            # proforma_invoice_id/quotation_id/purchase_order_id.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(packing_lists)")}
            if existing and "job_work_id" not in existing:
                conn.execute("ALTER TABLE packing_lists ADD COLUMN job_work_id INTEGER REFERENCES job_works(id)")

            # v89: a Job Work now prints/numbers as a purchase order (see
            # job_works.job_work_number), so a purchase invoice can be raised
            # against it the same way it already can against a real purchase
            # order - see the SCHEMA_VERSION changelog entry above.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_invoices)")}
            if existing and "job_work_id" not in existing:
                conn.execute("ALTER TABLE purchase_invoices ADD COLUMN job_work_id INTEGER REFERENCES job_works(id)")
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_invoice_items)")}
            if existing and "job_work_id" not in existing:
                conn.execute(
                    "ALTER TABLE purchase_invoice_items ADD COLUMN job_work_id INTEGER REFERENCES job_works(id)"
                )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS purchase_invoice_job_work_links (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchase_invoice_id   INTEGER NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
                    job_work_id           INTEGER NOT NULL REFERENCES job_works(id) ON DELETE CASCADE,
                    UNIQUE (purchase_invoice_id, job_work_id)
                )
            """)

            # v94: a packing type now says WHAT KIND of unit it describes, so
            # Loading Planning can tell a carton from a pallet - see the
            # SCHEMA_VERSION changelog above. Backfilled by weight: the rows
            # holding a real pallet weigh 20kg (Pallet, JUNGLE KHATLI), the
            # ones holding a carton weigh 0.3kg (CTN). A row with no weight
            # typed stays 'pallet', which is what every existing row is
            # treated as today.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(product_pallet_types)")}
            if existing and "unit_kind" not in existing:
                conn.execute(
                    "ALTER TABLE product_pallet_types ADD COLUMN unit_kind TEXT NOT NULL DEFAULT 'pallet'"
                )
                conn.execute(
                    "UPDATE product_pallet_types SET unit_kind = 'carton' "
                    "WHERE weight_kg IS NOT NULL AND weight_kg < 5"
                )

            # v94: the export invoice's booking picker already filled its 11B
            # table in; it only ever kept the booking NUMBER, so the link was
            # lost the moment a booking was renumbered. Keep the id too.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(export_invoices)")}
            if existing and "booking_detail_id" not in existing:
                conn.execute(
                    "ALTER TABLE export_invoices ADD COLUMN booking_detail_id "
                    "INTEGER REFERENCES booking_details(id)"
                )

            # v95: purchase_order_item_production + purchase_order_item_batches
            # - see the SCHEMA_VERSION changelog entry above. Both tables are
            # brand new, so every existing purchase order simply starts with
            # no production status and no batches, which reads as Pending.
            #
            # The first cut of v95 tracked production per PO LINE, before it
            # was clear that a line's designs only ever come from the linked
            # proforma invoice's packing list. A database that ran that cut
            # already holds both tables WITHOUT their design columns, and is
            # already stamped 95 - so CREATE TABLE IF NOT EXISTS alone would
            # leave it there forever, and every read would fail on `no such
            # column: design_id`. Hence the shape check below, which converges
            # on the final shape from either starting point.
            conn.execute(f"CREATE TABLE IF NOT EXISTS purchase_order_item_production ({_PRODUCTION_COLUMNS})")
            conn.execute(f"CREATE TABLE IF NOT EXISTS purchase_order_item_batches ({_BATCH_COLUMNS})")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_order_item_production)")}
            if "design_id" not in existing:
                # A table-level UNIQUE(purchase_order_item_id) came with that
                # first cut and would now cap a line at one design, so this
                # has to be a rebuild rather than an ALTER. Rows are carried
                # over as design-less, which is what they were recorded as.
                conn.execute("DROP INDEX IF EXISTS idx_po_item_production_item")
                conn.execute("ALTER TABLE purchase_order_item_production "
                             "RENAME TO purchase_order_item_production_old")
                conn.execute(f"CREATE TABLE purchase_order_item_production ({_PRODUCTION_COLUMNS})")
                conn.execute("""
                    INSERT INTO purchase_order_item_production
                        (id, purchase_order_item_id, design_id, design_name, status, updated_by, updated_at)
                    SELECT id, purchase_order_item_id, NULL, NULL, status, updated_by, updated_at
                    FROM purchase_order_item_production_old
                """)
                conn.execute("DROP TABLE purchase_order_item_production_old")

            # The batches table carries no such constraint, so it only needs
            # its two new columns.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_order_item_batches)")}
            if "design_id" not in existing:
                conn.execute("ALTER TABLE purchase_order_item_batches "
                             "ADD COLUMN design_id INTEGER REFERENCES designs(id)")
            if "design_name" not in existing:
                conn.execute("ALTER TABLE purchase_order_item_batches ADD COLUMN design_name TEXT")

            # Recreated unconditionally: that first cut left a plain,
            # non-unique index of the same name behind, which IF NOT EXISTS
            # would happily keep.
            conn.execute("DROP INDEX IF EXISTS idx_po_item_production_item")
            conn.execute("CREATE UNIQUE INDEX idx_po_item_production_item "
                         "ON purchase_order_item_production(purchase_order_item_id, "
                         "COALESCE(design_id, -1), COALESCE(UPPER(TRIM(design_name)), ''))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_po_item_batches_item "
                         "ON purchase_order_item_batches(purchase_order_item_id)")

    def _rebuild_job_works(self) -> None:
        """Brings `job_works` to its current shape (v82 + v83): a
        proforma_invoice_id where a purchase_order_id used to be, and a
        seller_supplier_id where v82 briefly had a seller_buyer_id. Rebuilds
        the table rather than ALTERing it, so it ends up with exactly
        schema.sql's foreign keys - including ON DELETE SET NULL on the
        invoice, which ALTER TABLE ADD COLUMN cannot express.

        Converges on the FINAL shape from any earlier one rather than
        migrating a version at a time, so a v81 database (purchase_order_id +
        seller_supplier_id) and a v82 one (proforma_invoice_id +
        seller_buyer_id) both land in the same place in a single pass:

          * proforma_invoice_id  - kept as-is when it already exists,
            otherwise taken from the purchase order the job work was raised
            against (PO -> its own proforma_invoice_id);
          * seller_supplier_id   - kept as-is when it already exists (a
            pre-v82 database never lost it), otherwise NULL, since v82's
            seller_buyer_id holds buyer ids that are meaningless here and was
            never backfilled anyway.

        On its OWN connection with `PRAGMA foreign_keys = OFF`, and NOT inside
        a transaction - both are required. job_work_items references
        job_works(id) ON DELETE CASCADE, so with foreign keys on (as every
        connection Database hands out has them) the `DROP TABLE job_works`
        step cascades into job_work_items and silently deletes every line of
        every job work. The pragma is also a no-op inside a transaction, which
        is why this can't just be another block in `_migrate`.

        Idempotent: a no-op once the table already has both target columns,
        and on a database with no job_works table at all yet."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(job_works)")}
            if existing and not {"proforma_invoice_id", "seller_supplier_id"} <= existing:
                invoice_source = (
                    "jw.proforma_invoice_id" if "proforma_invoice_id" in existing else
                    "(SELECT po.proforma_invoice_id FROM purchase_orders po WHERE po.id = jw.purchase_order_id)"
                )
                supplier_source = "jw.seller_supplier_id" if "seller_supplier_id" in existing else "NULL"
                # Autocommit, so the pragma actually takes effect.
                conn.isolation_level = None
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("BEGIN")
                conn.execute("""
                    CREATE TABLE job_works_rebuilt (
                        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id               INTEGER NOT NULL REFERENCES tenants(id),
                        job_work_number          TEXT NOT NULL,
                        job_work_date            TEXT NOT NULL,
                        proforma_invoice_id      INTEGER REFERENCES proforma_invoices(id) ON DELETE SET NULL,
                        seller_supplier_id       INTEGER REFERENCES suppliers(id),
                        seller_name              TEXT NOT NULL,
                        seller_address           TEXT,
                        seller_pan               TEXT,
                        seller_gstin             TEXT,
                        manufacturer_supplier_id INTEGER REFERENCES suppliers(id),
                        manufacturer_name        TEXT,
                        manufacturer_address     TEXT,
                        manufacturer_pan         TEXT,
                        manufacturer_gstin       TEXT,
                        seller_ref_no            TEXT,
                        delivery_time            TEXT,
                        advance_percent          TEXT,
                        payment_terms            TEXT,
                        remarks                  TEXT,
                        currency_code            TEXT,
                        currency_symbol          TEXT,
                        created_by               INTEGER NOT NULL REFERENCES users(id),
                        created_at               TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE (company_id, job_work_number)
                    )
                """)
                conn.execute(f"""
                    INSERT INTO job_works_rebuilt
                        (id, company_id, job_work_number, job_work_date, proforma_invoice_id,
                         seller_supplier_id, seller_name, seller_address, seller_pan, seller_gstin,
                         manufacturer_supplier_id, manufacturer_name, manufacturer_address,
                         manufacturer_pan, manufacturer_gstin, seller_ref_no, delivery_time,
                         advance_percent, payment_terms, remarks, currency_code, currency_symbol,
                         created_by, created_at, updated_at)
                    SELECT jw.id, jw.company_id, jw.job_work_number, jw.job_work_date,
                           {invoice_source}, {supplier_source},
                           jw.seller_name, jw.seller_address, jw.seller_pan, jw.seller_gstin,
                           jw.manufacturer_supplier_id, jw.manufacturer_name, jw.manufacturer_address,
                           jw.manufacturer_pan, jw.manufacturer_gstin, jw.seller_ref_no, jw.delivery_time,
                           jw.advance_percent, jw.payment_terms, jw.remarks, jw.currency_code,
                           jw.currency_symbol, jw.created_by, jw.created_at, jw.updated_at
                    FROM job_works jw
                """)
                conn.execute("DROP TABLE job_works")
                conn.execute("ALTER TABLE job_works_rebuilt RENAME TO job_works")
                # Dropping the old table took its indexes with it, and
                # schema.sql already ran for this startup.
                conn.execute("CREATE INDEX IF NOT EXISTS idx_job_works_company ON job_works(company_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_job_works_created_by ON job_works(created_by)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_job_works_date ON job_works(job_work_date)")
                conn.execute("COMMIT")
                conn.execute("PRAGMA foreign_keys = ON")
            # Lives here rather than in schema.sql: on a pre-v82 database the
            # proforma_invoice_id column doesn't exist when that script runs.
            if {r["name"] for r in conn.execute("PRAGMA table_info(job_works)")}:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_job_works_proforma ON job_works(proforma_invoice_id)"
                )
                conn.commit()
        finally:
            conn.close()

    def _backup_db_file(self, tag: str) -> None:
        """Copies the live DB file into instance/backups/ before a
        destructive migration, following the crm_<tag>_<timestamp>.db naming
        already used in that folder. Callers must commit any open
        transaction first so the copy is consistent."""
        if not os.path.exists(self.db_path):
            return
        backup_dir = os.path.join(os.path.dirname(self.db_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.db_path))[0]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(self.db_path, os.path.join(backup_dir, f"{stem}_{tag}_{stamp}.db"))

    def create_backup_copy(self, dest_path: str) -> None:
        """Write a CONSISTENT snapshot of the live DB to `dest_path` using
        SQLite's online backup API. Unlike a raw file copy, this is safe even
        if another request is mid-write, so it's what the Database Backup
        download uses to bundle the DB."""
        src = self._connect()
        try:
            dst = sqlite3.connect(dest_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

    def get_schema_version(self) -> int:
        """The live DB's `PRAGMA user_version` (0 for a DB that predates
        version stamping)."""
        return self.read_user_version(self.db_path)

    @staticmethod
    def read_user_version(db_path: str) -> int:
        """Read `PRAGMA user_version` from an arbitrary SQLite file - used to
        check how old an uploaded backup is before restoring it."""
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list:
        """Run a SELECT and return a list of sqlite3.Row objects."""
        with self.get_connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()

    def query_one(self, sql: str, params: tuple = ()):
        """Run a SELECT expected to return 0 or 1 rows."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run an INSERT/UPDATE/DELETE. Returns the new row id for INSERTs
        (lastrowid), which repositories use to return the created object."""
        with self.get_connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid
