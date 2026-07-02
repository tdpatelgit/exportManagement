"""
config.py
---------
Centralised, single-responsibility configuration object.

Why this exists (SOLID):
  - Single Responsibility: this module's only job is to know *where settings
    come from*. Nothing else in the app reads environment variables directly.
  - Open/Closed: to add a new setting, add an attribute here. Nothing that
    consumes `Config` needs to change.

All secrets/paths are overridable via a `.env` file (see .env.example) so the
same code can run in development, testing, or production without edits.
"""

import os
from dotenv import load_dotenv

# Load variables from a .env file into the process environment, if present.
load_dotenv()

# Base directory of the project (folder that contains this file).
BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Holds every tunable setting the app needs. Import this, don't
    scatter os.environ.get() calls throughout the codebase."""

    # Flask needs this to sign session cookies. CHANGE THIS IN PRODUCTION.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

    # Path to the SQLite database file.
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH", os.path.join(BASE_DIR, "instance", "crm.db")
    )

    # Path to the .sql file used to (re)create the schema on first run.
    SCHEMA_PATH = os.path.join(BASE_DIR, "app", "schema.sql")

    # Base currency all monetary values are converted into for reporting.
    # The brief says "amount in currency other than INR and its conversion",
    # so INR is our fixed base currency.
    BASE_CURRENCY = "INR"

    # Free, no-API-key exchange rate service used by CurrencyConversionService.
    # Swappable: change this one line to point at a different provider.
    EXCHANGE_RATE_API_URL = "https://api.frankfurter.app/latest"

    # If the exchange-rate API can't be reached (offline demo, no internet),
    # we fall back to these approximate static rates (units of foreign
    # currency per 1 INR is NOT how these are stored -- these are
    # "1 unit of FOREIGN currency = X INR", updated occasionally by an admin
    # editing this file). This keeps the app usable even without internet.
    FALLBACK_RATES_TO_INR = {
        "USD": 86.0,
        "EUR": 93.0,
        "GBP": 109.0,
        "AED": 23.4,
        "CNY": 12.0,
        "SAR": 22.9,
    }

    # How many days ahead counts as an "upcoming" follow-up on the employee
    # dashboard (used by StatsService).
    FOLLOWUP_LOOKAHEAD_DAYS = 3

    # Pagination default for list pages.
    PAGE_SIZE = 20

    # Where product photos / dimension photos get saved. Lives under
    # static/ so Flask can serve the files directly via url_for('static', ...).
    PRODUCT_UPLOAD_FOLDER = os.path.join(BASE_DIR, "app", "static", "uploads", "products")
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB upload cap
