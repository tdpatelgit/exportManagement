"""
Tests for the many-POs-per-PI rework:

  * ProformaFulfilmentService - which designs on a proforma invoice's packing
    list are still not placed on any purchase order linked to that invoice,
    and the confirmed-invoice reminder feed built on top of it.
  * The proforma invoice's draft/confirmed status and the editing lock it
    puts on the document.
  * PackingListService.build_prefill_from_purchase_order, which now starts a
    PO's packing list from just the outstanding designs.

The comparison is quantity-aware: a design half ordered is still pending for
its other half, which is what stops the second PO from re-ordering the
first one's goods.
"""

import pytest

from app.exceptions import ValidationError, PermissionDeniedError


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
def make_proforma(container, seed, **over):
    fields = {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"}
    fields.update(over)
    return container.proforma_invoice_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])


def make_purchase_order(container, seed, proforma_invoice_id=None):
    fields = {"seller_name": "Supplier Co", "po_date": "2026-02-02"}
    if proforma_invoice_id:
        fields["proforma_invoice_id"] = proforma_invoice_id
    return container.purchase_order_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "quantity_boxes": "10", "quantity_value": "100",
          "price_inr": "50"}])


def make_packing_list(container, seed, designs, **over):
    """`designs` is a list of (design_name, boxes) - one packing list line
    each, all for the same product."""
    fields = {"consignee_name": "Buyer Co", "packing_list_date": "2026-02-03"}
    fields.update(over)
    return container.packing_list_service.create(
        seed.admin, fields,
        [{"product_name": "Tiles", "design_name": name, "quantity_boxes": str(boxes),
          "quantity_value": str(boxes * 10), "unit": "SQM"}
         for name, boxes in designs])


def design_names(designs):
    return sorted(d["design_name"] for d in designs)


def make_quotation(container, seed):
    return container.quotation_service.create(
        seed.admin, {"buyer_name": "Buyer Co", "quotation_date": "2026-01-01"},
        [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])


def make_proforma_with_items(container, seed, items):
    """`items` is a list of (product_name, boxes) - one proforma product
    line each, used by the PO product-selection tests below."""
    return container.proforma_invoice_service.create(
        seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"},
        [{"product_name": name, "quantity_boxes": str(boxes), "quantity_value": str(boxes * 10),
          "price_usd": "2"}
         for name, boxes in items])


# ==========================================================================
# Design coverage
# ==========================================================================
class TestDesignStatus:
    def test_no_packing_list_means_nothing_to_track(self, container, seed):
        pi = make_proforma(container, seed)
        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert status["designs"] == []
        assert status["pending"] == []
        # Nothing broken down yet is not the same as "all done".
        assert status["is_fully_placed"] is False

    def test_every_design_pending_before_any_po(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10), ("Sand", 5)],
                          proforma_invoice_id=pi.id)
        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert design_names(status["pending"]) == ["Ocean Blue", "Sand"]
        assert status["placed_count"] == 0
        assert status["is_fully_placed"] is False

    def test_design_drops_off_once_a_linked_po_packs_it_in_full(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10), ("Sand", 5)],
                          proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=po.id)

        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert design_names(status["pending"]) == ["Sand"]
        assert status["placed_count"] == 1
        assert status["is_fully_placed"] is False

    def test_partial_quantity_leaves_the_remainder_pending(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 4)], purchase_order_id=po.id)

        pending = container.proforma_fulfilment_service.pending_designs(seed.company_id, pi.id)
        assert len(pending) == 1
        assert pending[0]["required_boxes"] == 10
        assert pending[0]["placed_boxes"] == 4
        assert pending[0]["pending_boxes"] == 6

    def test_quantities_add_up_across_several_purchase_orders(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        first = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        second = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 4)], purchase_order_id=first.id)
        make_packing_list(container, seed, [("Ocean Blue", 6)], purchase_order_id=second.id)

        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert status["pending"] == []
        assert status["is_fully_placed"] is True

    def test_an_unlinked_purchase_order_does_not_count(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        # Same design, same boxes - but this PO belongs to no invoice.
        other_po = make_purchase_order(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=other_po.id)

        pending = container.proforma_fulfilment_service.pending_designs(seed.company_id, pi.id)
        assert design_names(pending) == ["Ocean Blue"]

    def test_design_names_match_case_and_space_insensitively(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("  ocean   blue ", 10)], purchase_order_id=po.id)

        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert status["pending"] == []

    def test_status_map_covers_many_invoices_at_once(self, container, seed):
        first = make_proforma(container, seed)
        second = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=first.id)
        make_packing_list(container, seed, [("Sand", 5)], proforma_invoice_id=second.id)

        result = container.proforma_fulfilment_service.design_status_map(
            seed.company_id, [first.id, second.id])
        assert design_names(result[first.id]["pending"]) == ["Ocean Blue"]
        assert design_names(result[second.id]["pending"]) == ["Sand"]


class TestDesignOverOrdering:
    """A design need not be bought from a single PO - it can be split across
    several, and their packing lists can add up to MORE boxes than the
    invoice's own packing list called for. That's a different state from
    "pending" (under, not over), tracked separately so both can be shown."""

    def test_a_single_po_that_overshoots_is_flagged(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 15)], purchase_order_id=po.id)

        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert status["pending"] == []
        assert design_names(status["over_ordered"]) == ["Ocean Blue"]
        over = status["over_ordered"][0]
        assert over["placed_boxes"] == 15 and over["required_boxes"] == 10
        assert over["excess_boxes"] == 5
        assert over["is_over_ordered"] is True
        assert over["is_placed"] is True  # over-ordered is still "not pending"

    def test_several_pos_together_overshoot_even_though_none_alone_does(self, container, seed):
        """The whole point: no single PO looks wrong on its own (6 and 7
        boxes are both under the required 10), but their combined total
        (13) is over - so the aggregate, not any one PO, is what matters."""
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        first = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        second = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 6)], purchase_order_id=first.id)
        make_packing_list(container, seed, [("Ocean Blue", 7)], purchase_order_id=second.id)

        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        over = status["over_ordered"][0]
        assert over["placed_boxes"] == 13
        assert over["excess_boxes"] == 3

    def test_exact_match_is_neither_pending_nor_over_ordered(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=po.id)

        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert status["pending"] == [] and status["over_ordered"] == []

    def test_over_ordered_designs_helper(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 12)], purchase_order_id=po.id)

        over = container.proforma_fulfilment_service.over_ordered_designs(seed.company_id, pi.id)
        assert design_names(over) == ["Ocean Blue"]


class TestProductOverOrdering:
    """Same concept, one level coarser - a PI's own product line (not
    broken into designs) can also be over-ordered across several POs."""

    def test_several_pos_together_overshoot_a_product_line(self, container, seed):
        pi = make_proforma_with_items(container, seed, [("Tiles", 10)])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier A", "po_date": "2026-02-02",
                        "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "6", "quantity_value": "60", "price_inr": "50"}])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier B", "po_date": "2026-02-03",
                        "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "7", "quantity_value": "70", "price_inr": "50"}])

        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        status = container.proforma_fulfilment_service.product_status(seed.company_id, reloaded)
        assert status["pending"] == []
        over = status["over_ordered"][0]
        assert over["product_name"] == "Tiles"
        assert over["placed_boxes"] == 13 and over["excess_boxes"] == 3


class TestDesignStatusQuotationAncestorFallback:
    """Regression coverage for a real bug: an invoice generated straight from
    a quotation that already has its OWN packing list (skipping the PI step)
    never gets a packing list directly against it. design_status used to see
    zero required rows for that invoice and treat that as "nothing to
    compare against", so EVERY purchase order after the first re-imported
    the quotation's full packing list, ignoring what earlier purchase
    orders had already placed. Coverage now falls back to the ancestor
    quotation's packing list, same as the import path already does."""

    def _pi_from_quotation_with_pl(self, container, seed, designs):
        quotation = make_quotation(container, seed)
        make_packing_list(container, seed, designs, quotation_id=quotation.id)
        return container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01",
                        "quotation_id": quotation.id},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])

    def test_design_status_falls_back_to_the_quotations_packing_list(self, container, seed):
        pi = self._pi_from_quotation_with_pl(container, seed, [("Ocean Blue", 10), ("Sand", 5)])
        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert design_names(status["designs"]) == ["Ocean Blue", "Sand"]
        assert design_names(status["pending"]) == ["Ocean Blue", "Sand"]

    def test_second_po_no_longer_reimports_what_the_first_already_placed(self, container, seed):
        pi = self._pi_from_quotation_with_pl(container, seed, [("Ocean Blue", 10), ("Sand", 5)])
        first = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=first.id)
        second = make_purchase_order(container, seed, proforma_invoice_id=pi.id)

        pending = container.proforma_fulfilment_service.pending_designs(seed.company_id, pi.id)
        assert design_names(pending) == ["Sand"]

        items = container.packing_list_service.build_prefill_from_purchase_order(second)["items"]
        assert [i["design_name"] for i in items] == ["Sand"]

    def test_two_invoices_from_the_same_quotation_are_tracked_independently(self, container, seed):
        quotation = make_quotation(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], quotation_id=quotation.id)
        first_pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "A", "invoice_date": "2026-02-01", "quotation_id": quotation.id},
            [{"product_name": "Tiles", "quantity_value": "1", "price_usd": "1"}])
        second_pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "B", "invoice_date": "2026-02-01", "quotation_id": quotation.id},
            [{"product_name": "Tiles", "quantity_value": "1", "price_usd": "1"}])

        # Fully order the first invoice's design - the second, sharing the
        # same quotation PL but with no purchase orders of its own, must
        # still report it as pending.
        po = make_purchase_order(container, seed, proforma_invoice_id=first_pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=po.id)

        result = container.proforma_fulfilment_service.design_status_map(
            seed.company_id, [first_pi.id, second_pi.id])
        assert result[first_pi.id]["pending"] == []
        assert design_names(result[second_pi.id]["pending"]) == ["Ocean Blue"]

    def test_a_quotation_generated_pi_with_no_quotation_pl_still_reports_nothing_to_track(self, container, seed):
        """No PL on the invoice AND no PL on its quotation - genuinely
        nothing to compare against, not a fallback case."""
        quotation = make_quotation(container, seed)
        pi = container.proforma_invoice_service.create(
            seed.admin, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01",
                        "quotation_id": quotation.id},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])
        status = container.proforma_fulfilment_service.design_status(seed.company_id, pi.id)
        assert status["designs"] == [] and status["is_fully_placed"] is False


# ==========================================================================
# Draft / confirmed status
# ==========================================================================
class TestProformaStatus:
    def test_new_invoice_starts_as_a_draft(self, container, seed):
        pi = make_proforma(container, seed)
        assert pi.status == "draft"
        assert pi.is_confirmed is False

    def test_confirming_persists(self, container, seed):
        pi = make_proforma(container, seed)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")
        assert container.proforma_invoice_service.get(pi.id, seed.company_id).is_confirmed

    def test_invalid_status_rejected(self, container, seed):
        pi = make_proforma(container, seed)
        with pytest.raises(ValidationError):
            container.proforma_invoice_service.set_status(seed.admin, pi.id, "shipped")

    def test_confirmed_invoice_cannot_be_edited(self, container, seed):
        pi = make_proforma(container, seed)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")
        with pytest.raises(ValidationError):
            container.proforma_invoice_service.update(
                seed.admin, pi.id, {"consignee_name": "Renamed", "invoice_date": "2026-02-01"},
                [{"product_name": "Tiles", "quantity_value": "1", "price_usd": "1"}])

    def test_confirmed_invoice_cannot_be_deleted(self, container, seed):
        pi = make_proforma(container, seed)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")
        with pytest.raises(ValidationError):
            container.proforma_invoice_service.delete(seed.admin, pi.id)

    def test_admin_can_reopen_and_then_edit(self, container, seed):
        pi = make_proforma(container, seed)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "draft")
        container.proforma_invoice_service.update(
            seed.admin, pi.id, {"consignee_name": "Renamed", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "1", "price_usd": "1"}])
        assert container.proforma_invoice_service.get(pi.id, seed.company_id).consignee_name == "Renamed"

    def test_employee_cannot_reopen_a_confirmed_invoice(self, container, seed):
        pi = container.proforma_invoice_service.create(
            seed.employee, {"consignee_name": "Buyer Co", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "100", "price_usd": "2"}])
        container.proforma_invoice_service.set_status(seed.employee, pi.id, "confirmed")
        with pytest.raises(PermissionDeniedError):
            container.proforma_invoice_service.set_status(seed.employee, pi.id, "draft")

    def test_re_saving_never_changes_status(self, container, seed):
        """The update path doesn't write `status` at all, so a draft can't be
        confirmed (or a reopened invoice re-locked) as a side effect."""
        pi = make_proforma(container, seed)
        container.proforma_invoice_service.update(
            seed.admin, pi.id, {"consignee_name": "Renamed", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "1", "price_usd": "1"}])
        assert container.proforma_invoice_service.get(pi.id, seed.company_id).status == "draft"


# ==========================================================================
# The reminder feed
# ==========================================================================
class TestPurchaseOrderReminders:
    def test_drafts_are_never_reminded_about(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        assert container.proforma_fulfilment_service.pending_purchase_order_reminders(
            seed.company_id) == []

    def test_confirmed_invoice_with_unplaced_designs_is_reminded(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10), ("Sand", 5)],
                          proforma_invoice_id=pi.id)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")

        reminders = container.proforma_fulfilment_service.pending_purchase_order_reminders(
            seed.company_id)
        assert len(reminders) == 1
        assert reminders[0]["invoice"].id == pi.id
        assert reminders[0]["pending_count"] == 2
        assert reminders[0]["purchase_order_count"] == 0

    def test_reminder_clears_once_every_design_is_placed(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=po.id)

        assert container.proforma_fulfilment_service.pending_purchase_order_reminders(
            seed.company_id) == []

    def test_confirmed_invoice_without_a_packing_list_still_reminds(self, container, seed):
        pi = make_proforma(container, seed)
        container.proforma_invoice_service.set_status(seed.admin, pi.id, "confirmed")
        reminders = container.proforma_fulfilment_service.pending_purchase_order_reminders(
            seed.company_id)
        assert len(reminders) == 1
        assert reminders[0]["has_packing_list"] is False

    def test_feed_can_be_narrowed_to_one_employee(self, container, seed):
        mine = container.proforma_invoice_service.create(
            seed.employee, {"consignee_name": "Mine", "invoice_date": "2026-02-01"},
            [{"product_name": "Tiles", "quantity_value": "1", "price_usd": "1"}])
        theirs = make_proforma(container, seed)
        container.proforma_invoice_service.set_status(seed.employee, mine.id, "confirmed")
        container.proforma_invoice_service.set_status(seed.admin, theirs.id, "confirmed")

        reminders = container.proforma_fulfilment_service.pending_purchase_order_reminders(
            seed.company_id, created_by=seed.employee.id)
        assert [r["invoice"].id for r in reminders] == [mine.id]


# ==========================================================================
# A PO's packing list starts from what is still outstanding
# ==========================================================================
class TestPurchaseOrderPackingListPrefill:
    def test_first_po_imports_the_whole_invoice_packing_list(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10), ("Sand", 5)],
                          proforma_invoice_id=pi.id)
        po = make_purchase_order(container, seed, proforma_invoice_id=pi.id)

        items = container.packing_list_service.build_prefill_from_purchase_order(po)["items"]
        assert sorted(i["design_name"] for i in items) == ["Ocean Blue", "Sand"]
        assert {i["design_name"]: i["quantity_boxes"] for i in items}["Ocean Blue"] == 10

    def test_already_ordered_designs_are_left_out(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10), ("Sand", 5)],
                          proforma_invoice_id=pi.id)
        first = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 10)], purchase_order_id=first.id)
        second = make_purchase_order(container, seed, proforma_invoice_id=pi.id)

        items = container.packing_list_service.build_prefill_from_purchase_order(second)["items"]
        assert [i["design_name"] for i in items] == ["Sand"]

    def test_partly_ordered_design_comes_through_scaled_down(self, container, seed):
        pi = make_proforma(container, seed)
        make_packing_list(container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        first = make_purchase_order(container, seed, proforma_invoice_id=pi.id)
        make_packing_list(container, seed, [("Ocean Blue", 4)], purchase_order_id=first.id)
        second = make_purchase_order(container, seed, proforma_invoice_id=pi.id)

        items = container.packing_list_service.build_prefill_from_purchase_order(second)["items"]
        assert len(items) == 1
        assert items[0]["quantity_boxes"] == 6      # 10 needed - 4 already ordered
        assert items[0]["quantity_value"] == 60     # scaled with it

    def test_po_without_an_invoice_is_unaffected(self, container, seed):
        """No invoice to compare against - the old behaviour (import the
        ancestor PL wholesale, or an empty block per product line) stands."""
        po = make_purchase_order(container, seed)
        items = container.packing_list_service.build_prefill_from_purchase_order(po)["items"]
        assert [i["product_name"] for i in items] == ["Tiles"]
        assert items[0]["is_placeholder"] is True


# ==========================================================================
# The same "outstanding only" treatment, one level up: a new PO's OWN
# product lines (not its packing list) are cut down to what the invoice
# still needs ordered.
# ==========================================================================
class TestPurchaseOrderProductPrefill:
    def test_first_po_gets_every_product_line_unchanged(self, container, seed):
        pi = make_proforma_with_items(container, seed, [("Tiles", 20), ("Marble", 8)])
        built = container.purchase_order_service.build_prefill_from_proforma(pi)
        items = {i["product_name"]: i["quantity_boxes"] for i in built["items"]}
        assert items == {"Tiles": 20, "Marble": 8}

    def test_second_po_only_gets_the_remaining_boxes(self, container, seed):
        pi = make_proforma_with_items(container, seed, [("Tiles", 20), ("Marble", 8)])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier A", "po_date": "2026-02-02",
                        "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "12", "quantity_value": "120", "price_inr": "50"}])

        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        built = container.purchase_order_service.build_prefill_from_proforma(reloaded)
        items = {i["product_name"]: i["quantity_boxes"] for i in built["items"]}
        # Tiles: 20 needed - 12 already placed by Supplier A = 8 remaining.
        assert items["Tiles"] == 8
        assert items["Marble"] == 8

    def test_fully_ordered_product_is_dropped_entirely(self, container, seed):
        pi = make_proforma_with_items(container, seed, [("Tiles", 20), ("Marble", 8)])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier A", "po_date": "2026-02-02",
                        "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "20", "quantity_value": "200", "price_inr": "50"}])

        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        built = container.purchase_order_service.build_prefill_from_proforma(reloaded)
        assert [i["product_name"] for i in built["items"]] == ["Marble"]

    def test_scaled_quantity_value_follows_the_boxes_ratio(self, container, seed):
        pi = make_proforma_with_items(container, seed, [("Tiles", 10)])
        container.purchase_order_service.create(
            seed.admin, {"seller_name": "Supplier A", "po_date": "2026-02-02",
                        "proforma_invoice_id": str(pi.id)},
            [{"product_name": "Tiles", "quantity_boxes": "4", "quantity_value": "40", "price_inr": "50"}])

        reloaded = container.proforma_invoice_service.get(pi.id, seed.company_id)
        built = container.purchase_order_service.build_prefill_from_proforma(reloaded)
        assert len(built["items"]) == 1
        assert built["items"][0]["quantity_boxes"] == 6    # 10 - 4
        assert built["items"][0]["quantity_value"] == 60   # scaled with it

    def test_price_and_price_per_are_never_carried_over(self, container, seed):
        """The INR ex-factory rate is the new supplier's own figure, not
        anything from the invoice - scaling a quantity must never touch it."""
        pi = make_proforma_with_items(container, seed, [("Tiles", 10)])
        built = container.purchase_order_service.build_prefill_from_proforma(pi)
        assert built["items"][0]["price_inr"] == ""
        assert built["items"][0]["price_per"] == "BOX"


# ==========================================================================
# Through HTTP - the pages and the confirm button itself
# ==========================================================================
@pytest.fixture
def web(app, client):
    """Logged-in admin plus the app's own container, for seeding data that
    the request-time container will see."""
    container = app.container
    tenant = container.tenant_repo.create("Fulfil Co", "fulfil-co")
    admin = container.auth_service.create_user(
        tenant.id, "fulfiladmin", "fulfil-pass-1", "Fulfil Admin", "admin")
    with client.session_transaction() as sess:
        sess["user_id"] = admin.id

    class Ctx:
        pass

    ctx = Ctx()
    ctx.client, ctx.container, ctx.admin, ctx.company_id = client, container, admin, tenant.id
    return ctx


class TestProformaPages:
    def _seed_invoice_with_two_pos(self, web):
        seed = type("S", (), {"admin": web.admin, "company_id": web.company_id})
        pi = make_proforma(web.container, seed)
        make_packing_list(web.container, seed, [("Ocean Blue", 10), ("Sand", 5)],
                          proforma_invoice_id=pi.id)
        make_purchase_order(web.container, seed, proforma_invoice_id=pi.id)
        make_purchase_order(web.container, seed, proforma_invoice_id=pi.id)
        return pi

    def test_invoice_page_lists_every_linked_po_and_pending_designs(self, web):
        pi = self._seed_invoice_with_two_pos(web)
        body = web.client.get(f"/proforma-invoices/{pi.id}").get_data(as_text=True)
        assert "Purchase orders (2)" in body
        assert "New purchase order" in body
        assert "2 designs still to be ordered" in body
        assert "Ocean Blue" in body and "Sand" in body

    def test_confirm_button_locks_the_invoice(self, web):
        pi = self._seed_invoice_with_two_pos(web)
        resp = web.client.post(f"/proforma-invoices/{pi.id}/status",
                               data={"status": "confirmed"}, follow_redirects=True)
        assert resp.status_code == 200
        assert web.container.proforma_invoice_service.get(pi.id, web.company_id).is_confirmed
        # The edit page now bounces back to the invoice instead of opening.
        edit = web.client.get(f"/proforma-invoices/{pi.id}/edit")
        assert edit.status_code == 302

    def test_dashboard_shows_the_reminder(self, web):
        pi = self._seed_invoice_with_two_pos(web)
        web.client.post(f"/proforma-invoices/{pi.id}/status", data={"status": "confirmed"})
        body = web.client.get("/").get_data(as_text=True)
        assert "Purchase orders still to place" in body
        assert pi.invoice_number in body

    def test_reminder_gone_once_everything_is_ordered(self, web):
        seed = type("S", (), {"admin": web.admin, "company_id": web.company_id})
        pi = make_proforma(web.container, seed)
        make_packing_list(web.container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(web.container, seed, proforma_invoice_id=pi.id)
        make_packing_list(web.container, seed, [("Ocean Blue", 10)], purchase_order_id=po.id)
        web.client.post(f"/proforma-invoices/{pi.id}/status", data={"status": "confirmed"})

        assert "Purchase orders still to place" not in web.client.get("/").get_data(as_text=True)
        assert "Every design ordered" in web.client.get(
            f"/proforma-invoices/{pi.id}").get_data(as_text=True)


# ==========================================================================
# The "bought more than necessary" notification, fired right after saving
# the document that pushed something over.
# ==========================================================================
class TestOverOrderedNotification:
    def test_new_po_packing_list_that_overshoots_flashes_an_error(self, web):
        seed = type("S", (), {"admin": web.admin, "company_id": web.company_id})
        pi = make_proforma(web.container, seed)
        make_packing_list(web.container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(web.container, seed, proforma_invoice_id=pi.id)

        resp = web.client.post("/packing-lists/new", data={
            "packing_list_date": "2026-02-04",
            "purchase_order_id": str(po.id),
            "item_product_id[]": [""],
            "item_product_name[]": ["Tiles"],
            "item_design_id[]": [""],
            "item_design_name[]": ["Ocean Blue"],
            "item_hsn_code[]": [""],
            "item_box_per_pallet[]": [""],
            "item_pallets[]": [""],
            "item_quantity_boxes[]": ["15"],   # only 10 required - 5 too many
            "item_pcs[]": [""],
            "item_quantity_value[]": ["150"],
            "item_unit[]": ["SQM"],
            "item_net_weight_kg[]": [""],
            "item_gross_weight_kg[]": [""],
        }, follow_redirects=True)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Bought more than the proforma invoice" in body
        assert "Ocean Blue" in body

    def test_exact_match_does_not_flash(self, web):
        seed = type("S", (), {"admin": web.admin, "company_id": web.company_id})
        pi = make_proforma(web.container, seed)
        make_packing_list(web.container, seed, [("Ocean Blue", 10)], proforma_invoice_id=pi.id)
        po = make_purchase_order(web.container, seed, proforma_invoice_id=pi.id)

        resp = web.client.post("/packing-lists/new", data={
            "packing_list_date": "2026-02-04",
            "purchase_order_id": str(po.id),
            "item_product_id[]": [""],
            "item_product_name[]": ["Tiles"],
            "item_design_id[]": [""],
            "item_design_name[]": ["Ocean Blue"],
            "item_hsn_code[]": [""],
            "item_box_per_pallet[]": [""],
            "item_pallets[]": [""],
            "item_quantity_boxes[]": ["10"],
            "item_pcs[]": [""],
            "item_quantity_value[]": ["100"],
            "item_unit[]": ["SQM"],
            "item_net_weight_kg[]": [""],
            "item_gross_weight_kg[]": [""],
        }, follow_redirects=True)
        body = resp.get_data(as_text=True)
        assert "Bought more than the proforma invoice" not in body

    def test_new_po_that_overshoots_a_product_line_flashes_an_error(self, web):
        seed = type("S", (), {"admin": web.admin, "company_id": web.company_id})
        pi = make_proforma_with_items(web.container, seed, [("Tiles", 10)])

        resp = web.client.post("/purchase-orders/new", data={
            "po_date": "2026-02-02",
            "proforma_invoice_id": str(pi.id),
            "seller_name": "Supplier A",
            "purchase_type": "full_tax",
            "item_product_id[]": [""],
            "item_product_name[]": ["Tiles"],
            "item_hsn_code[]": [""],
            "item_quantity_boxes[]": ["15"],
            "item_quantity_value[]": ["150"],
            "item_unit[]": ["SQM"],
            "item_price_inr[]": ["50"],
            "item_price_per[]": ["BOX"],
        }, follow_redirects=True)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Ordered more than the proforma invoice calls for" in body
        assert "Tiles" in body
