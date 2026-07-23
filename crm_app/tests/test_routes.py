"""
Integration / smoke tests for the route layer (app/routes/*) via the Flask
test client.

These don't re-test business logic (the service tests do that) - they verify
the HTTP wiring the user actually hits: the app boots and registers every
blueprint, auth guards redirect anonymous users, login/logout work against a
real seeded user, admin-only pages 403 for employees, and the custom error
pages render.
"""

import pytest


# ==========================================================================
# App factory / wiring
# ==========================================================================
class TestAppBoots:
    def test_create_app_registers_all_blueprints(self, app):
        expected = {
            "auth", "dashboard", "leads", "clients", "admin", "company",
            "reports", "products", "quotations", "proforma_invoices",
            "purchase_orders", "packing_lists", "profile", "backup",
        }
        assert expected.issubset(set(app.blueprints))

    def test_container_is_attached(self, app):
        assert app.container is not None
        assert app.container.auth_service is not None


# ==========================================================================
# Auth guards for anonymous users
# ==========================================================================
class TestAuthGuards:
    def test_login_page_renders(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", ["/", "/leads/", "/clients/", "/products/"])
    def test_protected_pages_redirect_anonymous_to_login(self, client, path):
        resp = client.get(path, follow_redirects=False)
        # login_required redirects (302) to the login page.
        assert resp.status_code in (301, 302)
        assert "/login" in resp.headers.get("Location", "")


# ==========================================================================
# Login / logout flow against a real seeded user
# ==========================================================================
class TestLoginFlow:
    def _seed_user(self, app):
        container = app.container
        tenant = container.tenant_repo.create("Login Co", "login-co")
        container.auth_service.create_user(
            tenant.id, "boss", "boss-pass-1", "The Boss", "admin")
        return tenant.id

    def test_successful_login_sets_session(self, app, client):
        company_id = self._seed_user(app)
        resp = client.post("/login", data={
            "company_id": company_id, "username": "boss", "password": "boss-pass-1",
        }, follow_redirects=False)
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("user_id") is not None

    def test_wrong_password_does_not_authenticate(self, app, client):
        company_id = self._seed_user(app)
        client.post("/login", data={
            "company_id": company_id, "username": "boss", "password": "WRONG",
        })
        with client.session_transaction() as sess:
            assert sess.get("user_id") is None

    def test_logout_clears_session(self, app, client):
        company_id = self._seed_user(app)
        client.post("/login", data={
            "company_id": company_id, "username": "boss", "password": "boss-pass-1"})
        client.get("/logout")
        with client.session_transaction() as sess:
            assert sess.get("user_id") is None


# ==========================================================================
# Authenticated access + admin-only enforcement
# ==========================================================================
class TestAuthenticatedAccess:
    def test_dashboard_loads_for_logged_in_user(self, logged_in_admin):
        client, admin, company_id = logged_in_admin
        resp = client.get("/")
        assert resp.status_code == 200

    def test_employee_forbidden_from_admin_area(self, app, client):
        container = app.container
        tenant = container.tenant_repo.create("Emp Co", "emp-co")
        emp = container.auth_service.create_user(
            tenant.id, "emp", "emp-pass-1", "Emp", "employee")
        with client.session_transaction() as sess:
            sess["user_id"] = emp.id
        resp = client.get("/admin/employees")
        assert resp.status_code == 403


# ==========================================================================
# Error handlers
# ==========================================================================
class TestErrorHandlers:
    def test_unknown_route_renders_404(self, client):
        resp = client.get("/this/does/not/exist")
        assert resp.status_code == 404
