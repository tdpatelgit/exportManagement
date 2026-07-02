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

from flask import Flask, g, session, render_template

from config import Config
from app.database import Database
from app.repositories import (
    SqliteUserRepository, SqliteLeadRepository, SqliteClientRepository,
    CommunicationRepository, PaymentRepository, DocumentRepository, CompanyRepository,
    ProductGroupRepository, ProductRepository, QuotationRepository,
)
from app.services import (
    AuthService, LeadService, ClientService, CurrencyService,
    CommunicationService, StatsService, CompanyService, ReportService, ProductService,
    QuotationService,
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
        self.user_repo = SqliteUserRepository(db)
        self.lead_repo = SqliteLeadRepository(db)
        self.client_repo = SqliteClientRepository(db)
        self.comm_repo = CommunicationRepository(db)
        self.payment_repo = PaymentRepository(db)
        self.document_repo = DocumentRepository(db)
        self.company_repo = CompanyRepository(db)
        self.product_group_repo = ProductGroupRepository(db)
        self.product_repo = ProductRepository(db)
        self.quotation_repo = QuotationRepository(db)

        # Services (business logic layer)
        self.auth_service = AuthService(self.user_repo)
        self.currency_service = CurrencyService(Config.EXCHANGE_RATE_API_URL, Config.FALLBACK_RATES_TO_INR)
        self.communication_service = CommunicationService(self.comm_repo)
        self.lead_service = LeadService(self.lead_repo, self.communication_service)
        self.client_service = ClientService(
            self.client_repo, self.lead_repo, self.communication_service,
            self.payment_repo, self.document_repo, self.currency_service,
        )
        self.stats_service = StatsService(self.user_repo, self.lead_repo, self.comm_repo, self.client_repo)
        self.company_service = CompanyService(self.company_repo)
        self.report_service = ReportService(db)
        self.product_service = ProductService(
            self.product_group_repo, self.product_repo,
            Config.PRODUCT_UPLOAD_FOLDER, Config.ALLOWED_IMAGE_EXTENSIONS,
        )
        self.quotation_service = QuotationService(self.quotation_repo, self.product_repo)


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
        g.user = app.container.user_repo.get_by_id(user_id) if user_id else None

    # --- make the current user + status constants available in every template --------------------------------------------------
    @app.context_processor
    def inject_globals():
        from app.models import LEAD_STATUSES, CLIENT_STATUSES, CLIENT_TYPES, COMMUNICATION_MODES
        return dict(
            current_user=g.get("user"),
            LEAD_STATUSES=LEAD_STATUSES,
            CLIENT_STATUSES=CLIENT_STATUSES,
            CLIENT_TYPES=CLIENT_TYPES,
            COMMUNICATION_MODES=COMMUNICATION_MODES,
        )

    register_template_helpers(app)

    # --- blueprints --------------------------------------------------
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.leads import leads_bp
    from app.routes.clients import clients_bp
    from app.routes.admin import admin_bp
    from app.routes.company import company_bp
    from app.routes.reports import reports_bp
    from app.routes.products import products_bp
    from app.routes.quotations import quotations_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(clients_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(company_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(quotations_bp)

    # --- friendly error pages --------------------------------------------------
    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    return app
