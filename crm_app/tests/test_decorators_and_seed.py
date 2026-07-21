"""
Tests for the auth decorators in app/utils.py and the helpers in seed.py.

The decorators are the app's whole access-control surface at the HTTP layer,
so they're tested directly (against a minimal throwaway Flask app) rather than
only implicitly through real routes.
"""

import pytest
from flask import Flask, g

from app.utils import login_required, admin_required
from app.models import User
from seed import _slugify


def _app_with(view_decorator, user):
    """A tiny Flask app with one decorated view and `g.user` preset."""
    app = Flask(__name__)
    app.secret_key = "test"

    @app.route("/protected")
    @view_decorator
    def protected():
        return "SECRET"

    # The real app sets g.user in a before_request hook; mirror that.
    @app.before_request
    def set_user():
        g.user = user

    # login_required redirects to url_for("auth.login"), so that endpoint
    # must exist in this throwaway app too.
    @app.route("/login", endpoint="auth.login")
    def login():
        return "LOGIN PAGE"

    return app


def make_user(role="employee"):
    return User(id=1, company_id=1, username="u", password_hash="h",
                full_name="U", role=role)


# ==========================================================================
# login_required
# ==========================================================================
class TestLoginRequired:
    def test_anonymous_is_redirected_to_login(self):
        app = _app_with(login_required, None)
        resp = app.test_client().get("/protected")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_logged_in_user_passes_through(self):
        app = _app_with(login_required, make_user())
        resp = app.test_client().get("/protected")
        assert resp.status_code == 200
        assert b"SECRET" in resp.data

    def test_admin_also_passes_through(self):
        app = _app_with(login_required, make_user("admin"))
        assert app.test_client().get("/protected").status_code == 200

    def test_preserves_view_metadata(self):
        # @wraps must keep the original function name, or url_for/endpoint
        # registration breaks in confusing ways.
        def my_view():
            return "x"
        assert login_required(my_view).__name__ == "my_view"


# ==========================================================================
# admin_required
# ==========================================================================
class TestAdminRequired:
    def test_anonymous_is_redirected_to_login(self):
        app = _app_with(admin_required, None)
        resp = app.test_client().get("/protected")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_employee_gets_403(self):
        app = _app_with(admin_required, make_user("employee"))
        assert app.test_client().get("/protected").status_code == 403

    def test_admin_passes_through(self):
        app = _app_with(admin_required, make_user("admin"))
        resp = app.test_client().get("/protected")
        assert resp.status_code == 200
        assert b"SECRET" in resp.data

    def test_preserves_view_metadata(self):
        def my_admin_view():
            return "x"
        assert admin_required(my_admin_view).__name__ == "my_admin_view"


# ==========================================================================
# seed.py helpers
# ==========================================================================
class TestSlugify:
    @pytest.mark.parametrize("name,expected", [
        ("Acme Exports", "acme-exports"),
        ("ACME", "acme"),
        ("Acme  &  Co.", "acme-co"),
        ("Tiles/Slabs Ltd", "tiles-slabs-ltd"),
        ("  Padded  ", "padded"),
        ("Company 1", "company-1"),
    ])
    def test_slugs(self, name, expected):
        assert _slugify(name) == expected

    def test_unsluggable_name_falls_back(self):
        # Nothing alphanumeric survives, so the default kicks in.
        assert _slugify("!!!") == "company"

    def test_empty_string_falls_back(self):
        assert _slugify("") == "company"
