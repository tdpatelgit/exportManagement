"""
Tests for app/repositories.py

The repository layer is thin, but it's the boundary that turns rows into
dataclasses and back. These round-trip tests (write via repo -> read via repo)
catch column-name drift between schema.sql, the INSERT/SELECT SQL, and the
model.from_row mappers - the classic thing that breaks silently on a schema
change.
"""

import pytest

from app.models import (
    User, Lead, ContactPerson, Communication, Category, Product, Design,
)


@pytest.fixture
def company_id(container):
    return container.tenant_repo.create("Repo Co", "repo-co").id


# ==========================================================================
# TenantRepository
# ==========================================================================
class TestTenantRepository:
    def test_create_and_get(self, container):
        t = container.tenant_repo.create("Acme", "acme")
        assert container.tenant_repo.get_by_id(t.id).name == "Acme"

    def test_is_active_true_by_default(self, container):
        t = container.tenant_repo.create("Acme", "acme")
        assert container.tenant_repo.is_active(t.id) is True

    def test_is_active_false_for_unknown(self, container):
        assert container.tenant_repo.is_active(9999) is False

    def test_list_active_excludes_deactivated(self, container):
        a = container.tenant_repo.create("Active", "active")
        d = container.tenant_repo.create("Dead", "dead")
        container.db.execute("UPDATE tenants SET is_active = 0 WHERE id = ?", (d.id,))
        slugs = [t.slug for t in container.tenant_repo.list_active()]
        assert "active" in slugs and "dead" not in slugs


# ==========================================================================
# SqliteUserRepository
# ==========================================================================
class TestUserRepository:
    def _user(self, company_id, username="bob"):
        return User(id=None, company_id=company_id, username=username,
                    password_hash="h", full_name="Bob", role="employee")

    def test_create_and_get_by_id(self, container, company_id):
        u = container.user_repo.create(self._user(company_id))
        assert container.user_repo.get_by_id(u.id).username == "bob"

    def test_get_by_username_scoped_to_company(self, container, company_id):
        container.user_repo.create(self._user(company_id, "bob"))
        assert container.user_repo.get_by_username(company_id, "bob") is not None
        assert container.user_repo.get_by_username(9999, "bob") is None

    def test_list_all_filter_by_role(self, container, company_id):
        container.user_repo.create(self._user(company_id, "emp"))
        admin = self._user(company_id, "adm")
        admin.role = "admin"
        container.user_repo.create(admin)
        admins = container.user_repo.list_all(company_id, role="admin")
        assert [u.username for u in admins] == ["adm"]

    def test_set_active(self, container, company_id):
        u = container.user_repo.create(self._user(company_id))
        container.user_repo.set_active(u.id, False)
        assert container.user_repo.get_by_id(u.id).is_active is False

    def test_update_username_and_password(self, container, company_id):
        u = container.user_repo.create(self._user(company_id))
        container.user_repo.update_username(u.id, "robert")
        container.user_repo.update_password_hash(u.id, "newhash")
        reloaded = container.user_repo.get_by_id(u.id)
        assert reloaded.username == "robert" and reloaded.password_hash == "newhash"


# ==========================================================================
# SqliteLeadRepository (+ ContactRepository)
# ==========================================================================
class TestLeadRepository:
    def _lead(self, container, company_id, created_by):
        lead = Lead(id=None, company_id=company_id, company_name="Acme", phone="1",
                    email="a@x.com", facebook=None, instagram=None, other_social=None,
                    status="new", created_by=created_by)
        lead.contacts = [ContactPerson(id=None, name="Bob", is_primary=True)]
        return lead

    @pytest.fixture
    def creator(self, container, company_id):
        return container.user_repo.create(
            User(id=None, company_id=company_id, username="creator",
                 password_hash="h", full_name="C", role="admin"))

    def test_create_persists_lead_and_contacts(self, container, company_id, creator):
        lead = container.lead_repo.create(self._lead(container, company_id, creator.id))
        reloaded = container.lead_repo.get_by_id(lead.id)
        assert reloaded.company_name == "Acme"
        assert [c.name for c in reloaded.contacts] == ["Bob"]

    def test_list_all_filters_by_employee(self, container, company_id, creator):
        other = container.user_repo.create(
            User(id=None, company_id=company_id, username="other",
                 password_hash="h", full_name="O", role="employee"))
        container.lead_repo.create(self._lead(container, company_id, creator.id))
        container.lead_repo.create(self._lead(container, company_id, other.id))
        mine = container.lead_repo.list_all(company_id, employee_id=creator.id)
        assert all(l.created_by == creator.id for l in mine)

    def test_update_status(self, container, company_id, creator):
        lead = container.lead_repo.create(self._lead(container, company_id, creator.id))
        container.lead_repo.update_status(lead.id, "in_follow_up")
        assert container.lead_repo.get_by_id(lead.id).status == "in_follow_up"

    def test_contacts_set_primary(self, container, company_id, creator):
        lead = container.lead_repo.create(self._lead(container, company_id, creator.id))
        new_contact = container.lead_repo.contacts.add(
            lead.id, ContactPerson(id=None, name="Carol", is_primary=False))
        container.lead_repo.contacts.set_primary(lead.id, new_contact.id)
        reloaded = container.lead_repo.get_by_id(lead.id)
        primaries = [c.name for c in reloaded.contacts if c.is_primary]
        assert primaries == ["Carol"]


# ==========================================================================
# CategoryRepository / ProductRepository / DesignRepository
# ==========================================================================
class TestCatalogRepositories:
    def test_category_create_and_get(self, container, company_id):
        cat = container.category_repo.create(company_id, "Tiles")
        assert container.category_repo.get_by_id(cat.id).name == "Tiles"

    def test_product_create_and_update(self, container, company_id):
        product = container.product_repo.create(Product(
            id=None, company_id=company_id, product_name="Slab", igst_percent=18,
            sgst_percent=9, cgst_percent=9))
        assert container.product_repo.get_by_id(product.id).product_name == "Slab"
        container.product_repo.update(product.id, {"product_name": "Slab2"})
        assert container.product_repo.get_by_id(product.id).product_name == "Slab2"

    def test_design_create_and_list_for_product(self, container, company_id):
        product = container.product_repo.create(Product(
            id=None, company_id=company_id, product_name="Slab"))
        design = container.design_repo.create(Design(
            id=None, company_id=company_id, product_id=product.id,
            design_name="Marble White", price_usd=12.5))
        listed = container.design_repo.list_for_product(product.id)
        assert [d.design_name for d in listed] == ["Marble White"]
        assert listed[0].price_usd == 12.5


# ==========================================================================
# CommunicationRepository
# ==========================================================================
class TestCommunicationRepository:
    @pytest.fixture
    def lead_id(self, container, company_id):
        creator = container.user_repo.create(
            User(id=None, company_id=company_id, username="c", password_hash="h",
                 full_name="C", role="admin"))
        lead = Lead(id=None, company_id=company_id, company_name="Acme", phone="1",
                    email="a@x.com", facebook=None, instagram=None, other_social=None,
                    status="new", created_by=creator.id)
        lead.contacts = [ContactPerson(id=None, name="Bob", is_primary=True)]
        return container.lead_repo.create(lead).id, creator.id

    def test_add_and_list(self, container, lead_id):
        lid, emp_id = lead_id
        comm = Communication(id=None, parent_type="lead", parent_id=lid, employee_id=emp_id,
                             comm_date="2025-01-01 10:00", mode="Call", description="Talked")
        container.comm_repo.add(comm)
        listed = container.comm_repo.list_for("lead", lid)
        assert len(listed) == 1 and listed[0].mode == "Call"
