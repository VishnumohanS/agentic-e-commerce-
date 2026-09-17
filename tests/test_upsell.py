"""Upsell safety: the buyer's spending ceiling is enforced in code, not by the model."""

from __future__ import annotations

from app.core.money import to_minor
from app.models.catalog import UpsellOffer

import pytest

BASE_PRODUCT = {"product_id": "prd_hp_001", "name": "Headphones", "category": "audio"}


@pytest.fixture
def keen_advisor(buyer, monkeypatch):
    """Force the advisory model to recommend the add-on.

    This isolates the deterministic budget gate from the model's taste: an
    affordable add-on should be accepted, an over-budget one refused, no matter
    how enthusiastic the advisory is.
    """
    monkeypatch.setattr(
        buyer, "_safe_ai_json", lambda *a, **k: {"desirable": True, "rationale": "complementary"}
    )
    return buyer


def offer(price: int, *, category="accessories", quantity=1, currency="INR") -> dict:
    return UpsellOffer(
        product_id="prd_ac_005",
        name="Carry case",
        category=category,
        price=price,
        currency=currency,
        quantity=quantity,
    ).model_dump(mode="json")


class TestUpsellBudgetGate:
    def test_spec_example_is_rejected(self, buyer, mandate_factory):
        """Mandate 2000, product 1500, add-on 800 -> total 2300 -> reject."""
        mandate = mandate_factory(amount=to_minor(2000))
        decision = buyer._evaluate_upsell(
            offer(to_minor(800)), BASE_PRODUCT, mandate, to_minor(1500)
        )
        assert not decision.accepted
        assert decision.reason == "budget_exceeded"
        assert decision.projected_total == to_minor(2300)

    def test_affordable_upsell_can_be_accepted(self, buyer, mandate_factory, keen_advisor):
        mandate = mandate_factory(amount=to_minor(2000))
        decision = buyer._evaluate_upsell(
            offer(to_minor(300)), BASE_PRODUCT, mandate, to_minor(1500)
        )
        assert decision.accepted
        assert decision.projected_total == to_minor(1800)

    def test_upsell_landing_exactly_on_the_limit_is_allowed(
        self, buyer, mandate_factory, keen_advisor
    ):
        mandate = mandate_factory(amount=to_minor(2000))
        decision = buyer._evaluate_upsell(
            offer(to_minor(500)), BASE_PRODUCT, mandate, to_minor(1500)
        )
        assert decision.accepted
        assert decision.projected_total == mandate.maximum_amount

    def test_one_paisa_over_the_limit_is_rejected(self, buyer, mandate_factory):
        mandate = mandate_factory(amount=to_minor(2000))
        decision = buyer._evaluate_upsell(
            offer(to_minor(500) + 1), BASE_PRODUCT, mandate, to_minor(1500)
        )
        assert not decision.accepted
        assert decision.reason == "budget_exceeded"

    def test_quantity_is_included_in_the_projection(self, buyer, mandate_factory):
        mandate = mandate_factory(amount=to_minor(2000))
        decision = buyer._evaluate_upsell(
            offer(to_minor(300), quantity=3), BASE_PRODUCT, mandate, to_minor(1500)
        )
        assert not decision.accepted
        assert decision.projected_total == to_minor(2400)


class TestUpsellValidation:
    def test_no_offer_is_handled(self, buyer, mandate_factory):
        decision = buyer._evaluate_upsell(None, BASE_PRODUCT, mandate_factory(), 100000)
        assert not decision.accepted
        assert decision.reason == "no_offer"

    def test_malformed_offer_is_rejected(self, buyer, mandate_factory):
        decision = buyer._evaluate_upsell(
            {"garbage": True}, BASE_PRODUCT, mandate_factory(), 100000
        )
        assert not decision.accepted
        assert decision.reason == "malformed_offer"

    def test_negative_price_offer_is_rejected(self, buyer, mandate_factory):
        decision = buyer._evaluate_upsell(
            {"product_id": "x", "name": "x", "price": -5000, "currency": "INR"},
            BASE_PRODUCT,
            mandate_factory(),
            100000,
        )
        assert not decision.accepted

    def test_currency_mismatch_is_rejected(self, buyer, mandate_factory):
        decision = buyer._evaluate_upsell(
            offer(10000, currency="USD"), BASE_PRODUCT, mandate_factory(amount=500000), 100000
        )
        assert not decision.accepted
        assert decision.reason == "currency_mismatch"

    def test_out_of_scope_category_is_rejected(self, buyer, mandate_factory):
        mandate = mandate_factory(amount=500000, allowed_categories=["audio"])
        decision = buyer._evaluate_upsell(
            offer(10000, category="computing"), BASE_PRODUCT, mandate, 100000
        )
        assert not decision.accepted
        assert decision.reason == "out_of_scope"

    def test_upsell_disallowed_by_mandate_is_rejected(self, buyer, mandate_factory):
        mandate = mandate_factory(amount=500000, allow_upsell=False)
        decision = buyer._evaluate_upsell(offer(10000), BASE_PRODUCT, mandate, 100000)
        assert not decision.accepted
        assert decision.reason == "upsell_not_permitted"

    def test_model_opinion_cannot_override_the_budget(self, buyer, mandate_factory, monkeypatch):
        """Even if the advisory model insists, an over-budget add-on stays rejected."""
        monkeypatch.setattr(
            buyer, "_safe_ai_json", lambda *a, **k: {"desirable": True, "rationale": "buy it!"}
        )
        mandate = mandate_factory(amount=to_minor(2000))
        decision = buyer._evaluate_upsell(
            offer(to_minor(800)), BASE_PRODUCT, mandate, to_minor(1500)
        )
        assert not decision.accepted
        assert decision.reason == "budget_exceeded"


class TestMerchantUpsellProposals:
    def test_merchant_only_proposes_offers_that_fit_the_headroom(self, catalog):
        offer_small = catalog.build_upsell_offer("prd_hp_001", headroom=to_minor(100))
        assert offer_small is None

    def test_merchant_proposes_the_cheapest_fitting_addon(self, catalog):
        proposal = catalog.build_upsell_offer("prd_hp_001", headroom=to_minor(600))
        assert proposal is not None
        assert proposal.price <= to_minor(600)

    def test_out_of_stock_addons_are_never_proposed(self, catalog):
        catalog.adjust_inventory("prd_ac_006", -1000)
        catalog.adjust_inventory("prd_ac_005", -1000)
        catalog.adjust_inventory("prd_ac_007", -1000)
        assert catalog.build_upsell_offer("prd_hp_001") is None
