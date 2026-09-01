"""
app/__init__.py
----------------
The application factory + composition root.

This is the ONE place in the whole codebase that wires concrete classes
together: `Database` -> `SqliteXRepository` -> `XService`. Every other
module receives its dependencies through a constructor and never
instantiates a repository or the database itself (Dependency Inversion in
action). Swapping SQLite for another database later means editing this file
plus `app/database.py` - routes, services and templates are untouched.
"""

import os

from flask import Flask, g, session, render_template

from config import Config
from app.database import Database
from app.repositories import (
    TenantRepository, SqliteUserRepository, SqliteLeadRepository,
    SqlitePartyRepository, SqliteSupplierRepository, SqliteTransporterRepository,
    CommunicationRepository, PaymentRepository, DocumentRepository, CompanyRepository,
    CategoryRepository, ProductRepository, ProductPalletTypeRepository, ProductFolderRepository, DesignRepository,
    QuotationRepository, ProformaInvoiceRepository, PurchaseOrderRepository,
    PurchaseOrderProductionRepository, JobWorkRepository,
    PurchaseInvoiceRepository, JobOutRepository, JobInRepository,
    ExportInvoiceRepository, ExportPackingListRepository, ExportDesignsPackingListRepository,
    PackingListRepository, LoadingPlanningRepository, PackingPlanningRepository, DocumentVersionRepository, PermitRepository, BookingDetailRepository, MiscCurrencyRepository, MiscNatureOfContractRepository,
    MiscPortOfLoadingRepository, MiscContainerTypeRepository, MiscHsnCodeRepository, MiscCountryRepository, MiscUnitRepository,
)
from app.services import (
    AuthService, LeadService, PartyService, SupplierService, TransporterService, CurrencyService,
    CommunicationService, StatsService, CompanyService, ReportService, ProductService,
    QuotationService, ProformaInvoiceService, PurchaseOrderService, PurchaseOrderProductionService,
    JobWorkService, PurchaseInvoiceService,
    JobOutService, JobInService,
    ExportInvoiceService, ExportPackingListService, PackingListService, LoadingPlanningService,
    PackingPlanningService, BackupService,
    DocumentVersionService, ProformaFulfilmentService,
    InventoryService, PermitService, BookingDetailService, MiscListService,
)
from app.utils import register_template_helpers


class ServiceContainer:
    """A single object bundling every service, so routes can do
    `container.lead_service.create_lead(...)` instead of importing and
    constructing services themselves. This is a composition root pattern,
    not a full DI framework - deliberately simple for a project this size."""

    def __init__(self, db: Database):
        self.db = db

        # Repositories (persistence layer)
        self.tenant_repo = TenantRepository(db)
        self.user_repo = SqliteUserRepository(db)
        self.lead_repo = SqliteLeadRepository(db)
        self.buyer_repo = SqlitePartyRepository(db, table="buyers", client_type="Buyer")
        self.supplier_repo = SqliteSupplierRepository(db)
        self.transporter_repo = SqliteTransporterRepository(db)
        self.comm_repo = CommunicationRepository(db)
        self.payment_repo = PaymentRepository(db)
        self.document_repo = DocumentRepository(db)
        self.company_repo = CompanyRepository(db)
        self.category_repo = CategoryRepository(db)
        self.product_repo = ProductRepository(db)
        self.product_pallet_type_repo = ProductPalletTypeRepository(db)
        self.product_folder_repo = ProductFolderRepository(db)
        self.design_repo = DesignRepository(db)
        self.quotation_repo = QuotationRepository(db)
        self.proforma_invoice_repo = ProformaInvoiceRepository(db)
        self.purchase_order_repo = PurchaseOrderRepository(db)
        self.purchase_order_production_repo = PurchaseOrderProductionRepository(db)
        self.job_work_repo = JobWorkRepository(db)
        self.purchase_invoice_repo = PurchaseInvoiceRepository(db)
        self.job_out_repo = JobOutRepository(db)
        self.job_in_repo = JobInRepository(db)
        self.export_invoice_repo = ExportInvoiceRepository(db)
        self.export_packing_list_repo = ExportPackingListRepository(db, self.export_invoice_repo)
        self.export_designs_packing_list_repo = ExportDesignsPackingListRepository(
            db, self.export_invoice_repo, self.export_packing_list_repo
        )
        self.packing_list_repo = PackingListRepository(db)
        self.loading_planning_repo = LoadingPlanningRepository(db)
        self.packing_planning_repo = PackingPlanningRepository(db)
        self.document_version_repo = DocumentVersionRepository(db)
        self.permit_repo = PermitRepository(db)
        self.booking_detail_repo = BookingDetailRepository(db)
        self.misc_currency_repo = MiscCurrencyRepository(db)
        self.misc_nature_of_contract_repo = MiscNatureOfContractRepository(db)
        self.misc_port_of_loading_repo = MiscPortOfLoadingRepository(db)
        self.misc_container_type_repo = MiscContainerTypeRepository(db)
        self.misc_hsn_code_repo = MiscHsnCodeRepository(db)
        self.misc_country_repo = MiscCountryRepository(db)
        self.misc_unit_repo = MiscUnitRepository(db)

        # Services (business logic layer)
        self.auth_service = AuthService(self.user_repo, self.tenant_repo)
        self.currency_service = CurrencyService(Config.EXCHANGE_RATE_API_URL, Config.FALLBACK_RATES_TO_INR)
        self.communication_service = CommunicationService(self.comm_repo)
        self.lead_service = LeadService(self.lead_repo, self.communication_service)
        self.buyer_service = PartyService(
            self.buyer_repo, "buyer", self.lead_repo, self.communication_service,
            self.payment_repo, self.document_repo, self.currency_service,
            self.quotation_repo, self.proforma_invoice_repo, self.packing_list_repo,
            self.purchase_order_repo,
        )
        self.supplier_service = SupplierService(
            self.supplier_repo, self.lead_repo, self.communication_service,
            self.payment_repo, self.document_repo, self.currency_service,
            self.purchase_order_repo,
        )
        # No lead_repo/comm/payment/document wiring here: a transporter has
        # none of those satellites (see models.Transporter).
        self.transporter_service = TransporterService(self.transporter_repo)
        # Keyed by leads.converted_client_type - advance_client_status looks
        # up the right repo once it knows which type a lead converted to.
        self.party_repos = {"Buyer": self.buyer_repo, "Supplier": self.supplier_repo}
        self.stats_service = StatsService(
            self.user_repo, self.lead_repo, self.comm_repo,
            self.buyer_repo, self.supplier_repo,
        )
        self.company_service = CompanyService(
            self.company_repo, Config.PRODUCT_UPLOAD_FOLDER, Config.ALLOWED_IMAGE_EXTENSIONS,
        )
        self.report_service = ReportService(db)
        self.product_service = ProductService(
            self.category_repo, self.product_repo, self.product_folder_repo, self.design_repo,
            self.product_pallet_type_repo,
            Config.PRODUCT_UPLOAD_FOLDER, Config.ALLOWED_IMAGE_EXTENSIONS,
            self.job_work_repo,
        )
        self.inventory_service = InventoryService(
            self.product_service, self.packing_list_repo, self.design_repo,
            self.purchase_order_repo, self.export_invoice_repo, self.purchase_invoice_repo,
            self.job_in_repo,
        )
        self.permit_service = PermitService(
            self.permit_repo,
            Config.PERMIT_UPLOAD_FOLDER, Config.ALLOWED_DOCUMENT_EXTENSIONS,
        )
        self.misc_list_service = MiscListService(
            self.misc_currency_repo, self.misc_nature_of_contract_repo, self.misc_port_of_loading_repo,
            self.misc_container_type_repo, self.misc_hsn_code_repo, self.misc_country_repo,
            self.misc_unit_repo,
        )
        self.booking_detail_service = BookingDetailService(self.booking_detail_repo, self.buyer_repo)
        self.document_version_service = DocumentVersionService(self.document_version_repo)
        self.quotation_service = QuotationService(
            self.quotation_repo, self.product_repo, self.lead_repo, self.document_version_service,
            self.misc_list_service,
        )
        self.proforma_invoice_service = ProformaInvoiceService(
            self.proforma_invoice_repo, self.product_repo, self.lead_repo, self.quotation_repo,
            self.document_version_service, self.party_repos, self.misc_list_service,
        )
        # Read-only, repo-only service: which of a proforma invoice's product
        # lines (and, one level finer, which of its packing-list designs)
        # are still not placed on any of its purchase orders. Wired before
        # PurchaseOrderService/PackingListService, which use it to prefill a
        # new PO/its packing list with just the outstanding amounts.
        self.proforma_fulfilment_service = ProformaFulfilmentService(
            self.proforma_invoice_repo, self.packing_list_repo, self.purchase_order_repo,
            self.job_work_repo,
        )
        self.purchase_order_service = PurchaseOrderService(
            self.purchase_order_repo, self.product_repo, self.lead_repo, self.proforma_invoice_repo,
            self.document_version_service, self.party_repos, self.supplier_repo, self.company_repo,
            self.proforma_fulfilment_service, self.misc_list_service, self.quotation_repo,
        )
        # Screen-only companion to the purchase order: what the supplier has
        # actually produced against each of its lines (see the Production
        # Status card on the PO preview page). Leans on PurchaseOrderService
        # for its company scoping rather than re-checking company_id itself.
        self.purchase_order_production_service = PurchaseOrderProductionService(
            self.purchase_order_production_repo, self.purchase_order_service,
        )
        # Wired after PackingListService's own repo (job work reads a proforma
        # invoice's designs off its packing list, since an invoice only sells
        # by product - see JobWorkService.design_rows_for_proforma_invoice).
        self.job_work_service = JobWorkService(
            self.job_work_repo, self.proforma_invoice_repo, self.packing_list_repo, self.product_repo,
            self.document_version_service, self.supplier_repo, self.misc_list_service, self.company_repo,
            self.purchase_order_repo,
        )
        self.packing_list_service = PackingListService(
            self.packing_list_repo, self.product_repo, self.design_repo,
            self.lead_repo, self.proforma_invoice_repo, self.document_version_service,
            self.quotation_repo, self.purchase_order_repo, self.proforma_fulfilment_service,
            self.purchase_invoice_repo, self.job_work_repo,
        )
        # Packing Planning. Wired before Loading Planning because that is
        # the order the two documents run in: this one turns what the
        # supplier has produced into whole numbered pallets and cartons,
        # and only then does a loading plan decide which container they
        # go in. It takes purchase_order_production_repo, not
        # packing_list_repo, because its lines are BATCHES - a batch
        # number and a manufacturing date exist nowhere else.
        self.packing_planning_service = PackingPlanningService(
            self.packing_planning_repo, self.proforma_invoice_repo, self.purchase_order_repo,
            self.purchase_order_production_repo, self.product_repo, self.product_pallet_type_repo,
        )
        # Loading Planning. Wired after packing_list_repo because its whole
        # reason for existing is that hop: it traces a proforma invoice
        # through its purchase orders to THOSE ORDERS' packing lists, which
        # is the only place the design split lives (a PO orders 1268 boxes of
        # a product; its packing list says those are four designs of 317).
        self.loading_planning_service = LoadingPlanningService(
            self.loading_planning_repo, self.proforma_invoice_repo, self.purchase_order_repo,
            self.packing_list_repo, self.booking_detail_repo, self.product_repo,
            self.product_pallet_type_repo,
        )
        self.purchase_invoice_service = PurchaseInvoiceService(
            self.purchase_invoice_repo, self.product_repo, self.lead_repo, self.purchase_order_repo,
            self.document_version_service, self.party_repos, self.supplier_repo,
            Config.PURCHASE_INVOICE_UPLOAD_FOLDER, Config.ALLOWED_DOCUMENT_EXTENSIONS,
            self.misc_list_service, self.job_work_repo,
        )
        # The delivery challan for jobwork. Wired after packing_list_repo and
        # purchase_invoice_repo because it reads its whole printed body live
        # off a purchase invoice (and that invoice's packing list) at render
        # time rather than snapshotting any of it - see JobOutService.
        self.job_out_service = JobOutService(
            self.job_out_repo, self.purchase_invoice_repo, self.product_repo, self.company_repo,
            self.packing_list_repo, self.purchase_order_repo, self.document_version_service,
            self.job_work_repo, self.transporter_repo,
        )
        # The return leg. Wired after job_out_service because it reads its
        # whole sheet (and its prefilled design lines) through it, and it is
        # what puts the jobbed product back into stock.
        self.job_in_service = JobInService(
            self.job_in_repo, self.job_out_service, self.product_repo, self.design_repo,
            self.company_repo, self.document_version_service,
        )
        # The Export Packing List is generated by ExportInvoiceService on
        # every save of its invoice, so it is wired first and handed in.
        self.export_packing_list_service = ExportPackingListService(
            self.export_packing_list_repo, self.export_invoice_repo, self.product_repo, self.category_repo,
            self.design_repo, self.packing_list_repo, self.export_designs_packing_list_repo,
            self.job_in_repo,
        )
        self.export_invoice_service = ExportInvoiceService(
            self.export_invoice_repo, self.product_repo, self.lead_repo, self.proforma_invoice_repo,
            self.purchase_order_repo, self.purchase_invoice_repo, self.company_repo,
            self.document_version_service, self.party_repos,
            Config.EXPORT_INVOICE_UPLOAD_FOLDER, Config.ALLOWED_DOCUMENT_EXTENSIONS,
            self.export_packing_list_service, self.misc_list_service,
            self.job_work_repo, self.job_out_repo, self.job_in_repo, self.job_out_service,
        )
        self.backup_service = BackupService(
            db, Config.DATABASE_PATH,
            {
                "uploads/products": Config.PRODUCT_UPLOAD_FOLDER,
                "uploads/purchase_invoices": Config.PURCHASE_INVOICE_UPLOAD_FOLDER,
                "uploads/export_invoices": Config.EXPORT_INVOICE_UPLOAD_FOLDER,
                "uploads/permits": Config.PERMIT_UPLOAD_FOLDER,
            },
            Config.SCHEMA_PATH,
        )


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- database + composition root --------------------------------------------------
    db = Database(config_class.DATABASE_PATH)
    db.init_schema(config_class.SCHEMA_PATH)
    app.container = ServiceContainer(db)

    # --- load the logged-in user (if any) before every request --------------------------------------------------
    @app.before_request
    def load_logged_in_user():
        user_id = session.get("user_id")
        user = app.container.user_repo.get_by_id(user_id) if user_id else None
        # Enforce a whole-company lockout immediately, not just at next
        # login - a tenant deactivated mid-session shouldn't stay usable
        # until the session cookie happens to expire.
        if user and not app.container.tenant_repo.is_active(user.company_id):
            session.clear()
            user = None
        g.user = user

    # --- make the current user + status constants available in every template --------------------------------------------------
    @app.context_processor
    def inject_globals():
        from app.models import (LEAD_STATUSES, CLIENT_STATUSES, CLIENT_TYPES, COMMUNICATION_MODES,
                                PRODUCT_UNITS, PURCHASE_TYPES, EXEMPTION_IGST_PERCENT,
                                PROFORMA_STATUSES)
        # The logged-in tenant's own company profile (for the sidebar logo) -
        # one small query per request, only when someone is signed in.
        user = g.get("user")
        our_company = app.container.company_service.get(user.company_id) if user else None
        # The hand-maintained currency drop list (Administration ->
        # Miscellaneous), so every currency dropdown reads the same options
        # without each route having to pass them in.
        currency_options = app.container.misc_list_service.currency_options(user.company_id) if user else []
        # Same for the delivery-terms list, however the document labels it.
        nature_of_contract_options = (
            app.container.misc_list_service.list_nature_of_contracts(user.company_id) if user else []
        )
        # ...and for the ports of loading every shipping document picks from.
        port_of_loading_options = (
            app.container.misc_list_service.list_ports_of_loading(user.company_id) if user else []
        )
        # ...and for the container types the Booking Detail form picks from.
        container_type_options = (
            app.container.misc_list_service.container_type_options(user.company_id) if user else []
        )
        # ...and for the countries a Buyer's "Country Name" field picks from.
        country_options = (
            app.container.misc_list_service.list_countries(user.company_id) if user else []
        )
        return dict(
            current_user=g.get("user"),
            our_company=our_company,
            currency_options=currency_options,
            nature_of_contract_options=nature_of_contract_options,
            port_of_loading_options=port_of_loading_options,
            container_type_options=container_type_options,
            country_options=country_options,
            LEAD_STATUSES=LEAD_STATUSES,
            CLIENT_STATUSES=CLIENT_STATUSES,
            CLIENT_TYPES=CLIENT_TYPES,
            COMMUNICATION_MODES=COMMUNICATION_MODES,
            PRODUCT_UNITS=PRODUCT_UNITS,
            PURCHASE_TYPES=PURCHASE_TYPES,
            EXEMPTION_IGST_PERCENT=EXEMPTION_IGST_PERCENT,
            PROFORMA_STATUSES=PROFORMA_STATUSES,
        )

    register_template_helpers(app)

    # --- blueprints --------------------------------------------------
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.leads import leads_bp
    from app.routes.parties import build_party_blueprint
    from app.routes.suppliers import suppliers_bp
    from app.routes.transporters import transporters_bp
    from app.routes.admin import admin_bp
    from app.routes.company import company_bp
    from app.routes.permits import permits_bp
    from app.routes.misc import misc_bp
    from app.routes.reports import reports_bp
    from app.routes.products import products_bp
    from app.routes.inventory import inventory_bp
    from app.routes.booking_details import booking_details_bp
    from app.routes.packing_plannings import packing_plannings_bp
    from app.routes.loading_plannings import loading_plannings_bp
    from app.routes.quotations import quotations_bp
    from app.routes.proforma_invoices import proforma_invoices_bp
    from app.routes.purchase_orders import purchase_orders_bp
    from app.routes.job_works import job_works_bp
    from app.routes.purchase_invoices import purchase_invoices_bp
    from app.routes.job_outs import job_outs_bp
    from app.routes.job_ins import job_ins_bp
    from app.routes.export_invoices import export_invoices_bp
    from app.routes.export_packing_lists import export_packing_lists_bp
    from app.routes.export_annexures import export_annexures_bp
    from app.routes.export_designs_packing_lists import export_designs_packing_lists_bp
    from app.routes.tax_invoices import tax_invoices_bp
    from app.routes.bl_drafts import bl_drafts_bp
    from app.routes.vgm_attachments import vgm_attachments_bp
    from app.routes.vgm_declarations import vgm_declarations_bp
    from app.routes.eseals import eseals_bp
    from app.routes.eway_bills import eway_bills_bp
    from app.routes.commercial_invoices import commercial_invoices_bp
    from app.routes.commercial_packing_lists import commercial_packing_lists_bp
    from app.routes.customer_invoices import customer_invoices_bp
    from app.routes.packing_lists import packing_lists_bp
    from app.routes.profile import profile_bp
    from app.routes.backup import backup_bp

    buyers_bp = build_party_blueprint("buyers", "buyer_service")

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(buyers_bp)
    app.register_blueprint(suppliers_bp)
    app.register_blueprint(transporters_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(company_bp)
    app.register_blueprint(permits_bp)
    app.register_blueprint(misc_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(booking_details_bp)
    app.register_blueprint(packing_plannings_bp)
    app.register_blueprint(loading_plannings_bp)
    app.register_blueprint(quotations_bp)
    app.register_blueprint(proforma_invoices_bp)
    app.register_blueprint(purchase_orders_bp)
    app.register_blueprint(job_works_bp)
    app.register_blueprint(purchase_invoices_bp)
    app.register_blueprint(job_outs_bp)
    app.register_blueprint(job_ins_bp)
    app.register_blueprint(export_invoices_bp)
    app.register_blueprint(export_packing_lists_bp)
    app.register_blueprint(export_annexures_bp)
    app.register_blueprint(export_designs_packing_lists_bp)
    app.register_blueprint(tax_invoices_bp)
    app.register_blueprint(bl_drafts_bp)
    app.register_blueprint(vgm_attachments_bp)
    app.register_blueprint(vgm_declarations_bp)
    app.register_blueprint(eseals_bp)
    app.register_blueprint(eway_bills_bp)
    app.register_blueprint(commercial_invoices_bp)
    app.register_blueprint(commercial_packing_lists_bp)
    app.register_blueprint(customer_invoices_bp)
    app.register_blueprint(packing_lists_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(backup_bp)

    # --- friendly error pages --------------------------------------------------
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    # --- opt-in interactive debugger for a WSGI server (gunicorn/waitress) --------------------------------------------------
    # Under those servers, app.run(debug=True) never runs, so Flask's normal
    # debug wiring has no effect - the browser just gets a bare 500. Setting
    # WERKZEUG_DEBUG=1 does the two things app.run(debug=True) would normally
    # do for us: (1) app.debug = True stops Flask from swallowing the
    # exception into a generic 500 response itself, so it actually propagates
    # out of the WSGI app; (2) DebuggedApplication then catches that
    # propagated exception and renders the full interactive traceback.
    # Without both, the exception never reaches the debugger and you just
    # get a bare "Internal Server Error" - which is exactly what step (1)
    # being missing looks like.
    # SECURITY: this traceback page includes an interactive Python console
    # that can execute arbitrary code for anyone who can reach it - only set
    # WERKZEUG_DEBUG=1 while a firewall/VPN restricts access to trusted IPs,
    # and unset it (then restart) as soon as you're done diagnosing.
    if os.environ.get("WERKZEUG_DEBUG") == "1":
        from werkzeug.debug import DebuggedApplication
        app.debug = True
        app.wsgi_app = DebuggedApplication(app.wsgi_app, evalex=True)
        # Unambiguous proof-of-life in the process's own stdout/stderr - if
        # this line never shows up in the server's logs after a restart,
        # the env var isn't reaching this process (wrong .env file, wrong
        # working directory, or gunicorn wasn't actually restarted) and no
        # amount of waiting for the browser to show a traceback will help.
        print(">>> WERKZEUG_DEBUG=1 detected - interactive debugger is ACTIVE for this worker <<<", flush=True)

    return app
