"""
run.py
------
Entry point for local development.

    python run.py

Reads HOST/PORT/DEBUG from environment variables (see .env.example) so
production deployment (behind gunicorn/waitress, etc.) doesn't need this
file at all - it can import `create_app` from `app` directly instead.
"""

import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
