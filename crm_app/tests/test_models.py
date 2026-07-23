"""
Tests for app/models.py

Two things matter about the model layer and both are covered here:
  1. `from_row` correctly maps a sqlite3.Row-like mapping onto the dataclass,
     including the "column may be absent" guards (row.keys() checks).
  2. The computed @property accessors (subtotals, tax amounts, totals,
     round-off) do the arithmetic the printed documents depend on.
"""

import pytest

from app.models import (
    Tenant, User, ContactPerson, Communication, Lead, Party, Supplier,
    Quotation, QuotationItem, ProformaInvoice, ProformaInvoiceItem,
    PurchaseOrder, PurchaseOrderItem, PackingList, PackingListItem,
    DocumentVersion,
    LEAD_STATUSES, CLIENT_STATUSES,
)


class FakeRow(dict):
    """Mimics sqlite3.Row: indexable by column name AND exposes .keys().
    Using a dict subclass gives us __getitem__ and keys() for free, matching
    how from_row probes optional columns via `"col" in row.keys()`.
    """


# --------------------------------------------------------------------------
# Simple dataclasses
# --------------------------------------------------------------------------
class TestTenant:
    def test_from_row_casts_is_active(self):
        t = Tenant.from_row(FakeRow(id=1, name="Acme", slug="acme", is_active=1, created_at="2025-01-01"))
        assert t.id == 1 and t.name == "Acme" and t.slug == "acme"
        assert t.is_active is True

    def test_from_row_inactive(self):
        t = Tenant.from_row(FakeRow(id=2, name="X", slug="x", is_active=0, created_at=None))
        assert t.is_active is False


class TestUser:
    def _row(self, role="employee", is_active=1):
        return FakeRow(id=1, company_id=7, username="u", password_hash="h",
                       full_name="Full Name", role=role, is_active=is_active, created_at=None)

    def test_from_row(self):
        u = User.from_row(self._row())
        assert u.company_id == 7 and u.username == "u"
        assert u.is_active is True

    def test_is_admin_true(self):
        assert User.from_row(self._row(role="admin")).is_admin is True

    def test_is_admin_false(self):
        assert User.from_row(self._row(role="employee")).is_admin is False


class TestContactPerson:
    def test_from_row_casts_primary(self):
        c = ContactPerson.from_row(FakeRow(id=1, name="Bob", phone="1", email="b@x.com", is_primary=1))
        assert c.is_primary is True and c.name == "Bob"


class TestCommunication:
    def test_from_row_without_employee_name(self):
        row = FakeRow(id=1, parent_type="lead", parent_id=3, employee_id=9,
                      comm_date="2025-01-01", mode="Call", description="hi",
                      follow_up_date=None, created_at=None)
        c = Communication.from_row(row)
        assert c.parent_type == "lead" and c.employee_name is None

    def test_from_row_with_employee_name(self):
        row = FakeRow(id=1, parent_type="buyer", parent_id=3, employee_id=9,
                      comm_date="2025-01-01", mode="Email", description="hi",
                      follow_up_date="2025-02-01", created_at=None, employee_name="Eve")
        assert Communication.from_row(row).employee_name == "Eve"


class TestLead:
    def _row(self, status="new"):
        return FakeRow(id=1, company_id=1, company_name="Acme", phone="123", email="a@x.com",
                       facebook=None, instagram=None, other_social=None, status=status,
                       created_by=2, created_at=None, updated_at=None, is_converted=0,
                       converted_client_id=None)

    def test_from_row(self):
        lead = Lead.from_row(self._row())
        assert lead.company_name == "Acme"
        assert lead.is_converted is False
        assert lead.created_by_name is None  # column absent

    def test_status_label_known(self):
        assert Lead.from_row(self._row("in_follow_up")).status_label == "In Follow Up"

    def test_status_label_unknown_falls_back_to_code(self):
        assert Lead.from_row(self._row("weird")).status_label == "weird"


class TestParty:
    """Buyer and Exporter share this one dataclass - which table the row came
    from is what says which type it is, so there is no type column to map."""

    def _row(self, status="proforma_invoice_submission_pending", **over):
        row = FakeRow(id=1, company_id=1, lead_id=5, company_name="Acme", phone="1",
                      email="a@x.com", facebook=None, instagram=None, other_social=None,
                      status=status, created_by=2, address="Somewhere",
                      created_at=None, updated_at=None)
        row.update(over)
        return row

    def test_from_row(self):
        party = Party.from_row(self._row())
        assert party.company_name == "Acme" and party.address == "Somewhere"
        assert party.lead_id == 5
        assert party.contacts == []

    def test_from_row_without_the_optional_address_column(self):
        row = self._row()
        del row["address"]
        assert Party.from_row(row).address is None

    def test_status_label(self):
        assert Party.from_row(self._row()).status_label == "Proforma Invoice Submission Pending"

    def test_status_label_unknown_falls_back_to_code(self):
        assert Party.from_row(self._row("weird")).status_label == "weird"


class TestSupplier:
    """A Supplier is shaped like our own company profile, not like a lead -
    no phone/email/social columns, but GSTIN/PAN/IEC instead."""

    def _row(self, status="proforma_invoice_submission_pending"):
        return FakeRow(id=1, company_id=1, lead_id=5, company_name="Kiln Co",
                       status=status, created_by=2, address="Morbi",
                       gstin="24ABCDE1234F1Z5", pan_no="ABCDE1234F", iec="0123456789",
                       created_at=None, updated_at=None)

    def test_from_row(self):
        supplier = Supplier.from_row(self._row())
        assert supplier.company_name == "Kiln Co" and supplier.gstin == "24ABCDE1234F1Z5"
        assert supplier.pan_no == "ABCDE1234F" and supplier.iec == "0123456789"
        # The repeatable satellite rows are loaded separately, never by from_row.
        assert supplier.contact_details == [] and supplier.bank_details == []

    def test_status_label_borrows_the_client_pipeline(self):
        assert Supplier.from_row(self._row()).status_label == "Proforma Invoice Submission Pending"


# --------------------------------------------------------------------------
# Quotation totals
# --------------------------------------------------------------------------
class TestQuotation:
    def _quotation(self, **overrides):
        base = dict(id=1, company_id=1, quotation_number="QT1", quotation_date="2025-01-01",
                    buyer_name="Buyer", created_by=1)
        base.update(overrides)
        return Quotation(**base)

    def _item(self, total):
        return QuotationItem(id=None, quotation_id=1, sr_no=1, product_name="P", total_usd=total)

    def test_subtotal_sums_items(self):
        q = self._quotation()
        q.items = [self._item(100), self._item(50.5)]
        assert q.subtotal_usd == 150.5

    def test_subtotal_uses_precomputed_when_present(self):
        q = self._quotation(computed_subtotal_usd=999)
        q.items = [self._item(1)]  # ignored because precomputed is set
        assert q.subtotal_usd == 999

    def test_invoice_value_adds_charges_and_subtracts_discount(self):
        q = self._quotation(sea_freight=10, insurance=5, certification=2,
                            other_charges=3, discount_amount=4)
        q.items = [self._item(100)]
        # 100 + 10 + 5 + 2 + 3 - 4
        assert q.invoice_value_usd == 116


# --------------------------------------------------------------------------
# PurchaseOrder derived amounts
# --------------------------------------------------------------------------
class TestPurchaseOrder:
    def _po(self, **overrides):
        base = dict(id=1, company_id=1, po_number="PO1", po_date="2025-01-01",
                    seller_name="Seller", created_by=1)
        base.update(overrides)
        return PurchaseOrder(**base)

    def _item(self, total, boxes=0, qty=0):
        return PurchaseOrderItem(id=None, purchase_order_id=1, sr_no=1, product_name="P",
                                 total_inr=total, quantity_boxes=boxes, quantity_value=qty)

    def test_subtotal_sums_items(self):
        po = self._po()
        po.items = [self._item(1000), self._item(500)]
        assert po.subtotal_inr == 1500

    def test_totals_boxes_and_quantity(self):
        po = self._po()
        po.items = [self._item(0, boxes=3, qty=30), self._item(0, boxes=2, qty=20)]
        assert po.total_boxes == 5
        assert po.total_quantity == 50

    def test_igst_amount(self):
        po = self._po(igst_percent=18)
        po.items = [self._item(1000)]
        assert po.igst_amount == 180.0

    def test_cgst_sgst_amounts(self):
        po = self._po(cgst_percent=9, sgst_percent=9)
        po.items = [self._item(1000)]
        assert po.cgst_amount == 90.0
        assert po.sgst_amount == 90.0

    def test_order_value_rounds_to_whole_rupee(self):
        po = self._po(igst_percent=18)
        po.items = [self._item(1000.40)]
        # subtotal 1000.40 + igst 180.07 = 1180.47 -> rounds to 1180
        assert po.order_value_inr == 1180.0

    def test_round_off_bridges_the_difference(self):
        po = self._po(igst_percent=18)
        po.items = [self._item(1000.40)]
        gross = po.subtotal_inr + po.igst_amount
        assert po.round_off_inr == round(po.order_value_inr - gross, 2)

    def test_subtotal_precomputed_used_only_when_no_items(self):
        po = self._po(computed_subtotal_inr=5000)
        assert po.subtotal_inr == 5000  # no items -> precomputed wins
        po.items = [self._item(100)]
        assert po.subtotal_inr == 100  # items present -> recomputed


# --------------------------------------------------------------------------
# PackingList totals
# --------------------------------------------------------------------------
class TestPackingList:
    def _pl(self):
        return PackingList(id=1, company_id=1, packing_list_number="PL1",
                           packing_list_date="2025-01-01", consignee_name="C", created_by=1)

    def _item(self, **kw):
        base = dict(id=None, packing_list_id=1, sr_no=1, product_name="P")
        base.update(kw)
        return PackingListItem(**base)

    def test_all_totals(self):
        pl = self._pl()
        pl.items = [
            self._item(pallets=2, quantity_boxes=10, pcs=100, quantity_value=50,
                       net_weight_kg=20, gross_weight_kg=25),
            self._item(pallets=3, quantity_boxes=5, pcs=50, quantity_value=25,
                       net_weight_kg=10, gross_weight_kg=12),
        ]
        assert pl.total_pallets == 5
        assert pl.total_boxes == 15
        assert pl.total_pcs == 150
        assert pl.total_quantity == 75
        assert pl.total_net_weight_kg == 30
        assert pl.total_gross_weight_kg == 37

    def test_totals_treat_none_as_zero(self):
        pl = self._pl()
        pl.items = [self._item(pallets=None, quantity_boxes=None, pcs=None)]
        assert pl.total_pallets == 0
        assert pl.total_boxes == 0
        assert pl.total_pcs == 0


# --------------------------------------------------------------------------
# ProformaInvoice totals + display_mode default
# --------------------------------------------------------------------------
class TestProformaInvoice:
    def _pi(self, **overrides):
        base = dict(id=1, company_id=1, invoice_number="PI1", invoice_date="2025-01-01",
                    consignee_name="C", created_by=1)
        base.update(overrides)
        return ProformaInvoice(**base)

    def test_invoice_value(self):
        pi = self._pi(sea_freight=10, discount_amount=5)
        pi.items = [ProformaInvoiceItem(id=None, proforma_invoice_id=1, sr_no=1,
                                        product_name="P", total_usd=100)]
        assert pi.invoice_value_usd == 105

    def test_display_mode_defaults_to_index_when_null(self):
        row = self._make_row(display_mode=None)
        assert ProformaInvoice.from_row(row).display_mode == "index"

    def test_display_mode_preserved(self):
        row = self._make_row(display_mode="surface")
        assert ProformaInvoice.from_row(row).display_mode == "surface"

    def test_status_defaults_to_draft_when_the_column_is_absent(self):
        """Historical document_versions snapshots and pre-v16 rows have no
        status - they must read back as editable drafts, not as locked."""
        row = self._make_row()
        row.pop("status", None)
        invoice = ProformaInvoice.from_row(row)
        assert invoice.status == "draft" and invoice.is_confirmed is False

    def test_confirmed_status_preserved(self):
        invoice = ProformaInvoice.from_row(self._make_row(status="confirmed"))
        assert invoice.is_confirmed is True
        assert invoice.status_label == "Confirmed"

    def _make_row(self, display_mode="index", status="draft"):
        return FakeRow(
            id=1, company_id=1, invoice_number="PI1", invoice_date="2025-01-01",
            lead_id=None, quotation_id=None, export_ref_no=None, buyer_order_no=None,
            other_reference=None, consignee_name="C", consignee_address=None,
            notify_name=None, notify_address=None, country_of_origin="INDIA",
            country_of_destination=None, port_of_loading=None, port_of_discharge=None,
            final_destination=None, transhipment=None, partial_shipment=None,
            variation_in_qty=None, delivery_period=None, container_details=None,
            terms_of_delivery=None, payment_terms=None, remarks=None,
            sea_freight=0, insurance=0, certification=0, other_charges=0, discount_amount=0,
            bank_name=None, bank_account_number=None, bank_ifsc_code=None,
            bank_swift_code=None, bank_branch=None, bank_address=None,
            display_mode=display_mode, status=status,
            created_by=1, created_at=None, updated_at=None,
        )


# --------------------------------------------------------------------------
# DocumentVersion JSON snapshot round-trip
# --------------------------------------------------------------------------
class TestDocumentVersion:
    def test_from_row_parses_snapshot_json(self):
        row = FakeRow(id=1, company_id=1, document_type="quotation", document_id=5,
                      version_number=2, document_number="QT1",
                      snapshot='{"a": 1, "items": []}', changed_by=3, created_at=None)
        dv = DocumentVersion.from_row(row)
        assert dv.snapshot == {"a": 1, "items": []}
        assert dv.version_number == 2
        assert dv.changed_by_name is None


# --------------------------------------------------------------------------
# Module-level constants stay internally consistent
# --------------------------------------------------------------------------
def test_status_constants_are_code_label_pairs():
    for code, label in LEAD_STATUSES + CLIENT_STATUSES:
        assert isinstance(code, str) and isinstance(label, str)

def test_in_client_is_a_valid_lead_status():
    # The DB migration comment insists 'in_client' must exist; guard it.
    assert "in_client" in dict(LEAD_STATUSES)
