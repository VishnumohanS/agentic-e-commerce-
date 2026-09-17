"""AP2 spending mandate tests: signing, validation and deterministic limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.exceptions import (
    BudgetExceededError,
    MandateCurrencyError,
    MandateExpiredError,
    MandateMalformedError,
    MandateReplayError,
    MandateScopeError,
    MandateSignatureError,
)
from app.models.mandate import LineItem, MandateRequest
from app.services.ap2 import InMemoryNonceStore, MandateService, SQLiteNonceStore


def item(product_id="prd_hp_002", price=79900, quantity=1, category="audio", kind="primary"):
    return LineItem(
        product_id=product_id,
        name="Test product",
        category=category,
        unit_price=price,
        quantity=quantity,
        kind=kind,
    )


class TestMandateCreation:
    def test_created_mandate_is_signed_and_verifiable(self, mandates, mandate_factory):
        mandate = mandate_factory()
        assert mandate.signature
        assert mandates.verify_signature(mandate) is True

    def test_mandate_id_and_nonce_are_unique(self, mandate_factory):
        first, second = mandate_factory(), mandate_factory()
        assert first.mandate_id != second.mandate_id
        assert first.nonce != second.nonce

    def test_amount_is_capped_by_the_platform_ceiling(self, settings, mandates):
        mandate = mandates.create_mandate(
            MandateRequest(maximum_amount=settings.ap2_absolute_max_amount * 10)
        )
        assert mandate.maximum_amount == settings.ap2_absolute_max_amount

    def test_non_positive_amount_is_rejected(self, mandates):
        with pytest.raises(ValidationError):
            MandateRequest(maximum_amount=0)
        bypassed = MandateRequest(maximum_amount=1)
        object.__setattr__(bypassed, "maximum_amount", 0)
        with pytest.raises(MandateMalformedError):
            mandates.create_mandate(bypassed)


class TestMandateValidation:
    def test_valid_mandate_passes(self, mandates, mandate_factory):
        mandate = mandate_factory()
        assert mandates.validate(mandate.model_dump(mode="json")).mandate_id == mandate.mandate_id

    def test_tampered_amount_fails_signature(self, mandates, mandate_factory):
        raw = mandate_factory(amount=100000).model_dump(mode="json")
        raw["maximum_amount"] = 999999
        with pytest.raises(MandateSignatureError):
            mandates.validate(raw)

    def test_tampered_scope_fails_signature(self, mandates, mandate_factory):
        raw = mandate_factory().model_dump(mode="json")
        raw["allowed_categories"] = ["anything"]
        with pytest.raises(MandateSignatureError):
            mandates.validate(raw)

    def test_missing_signature_fails(self, mandates, mandate_factory):
        raw = mandate_factory().model_dump(mode="json")
        raw["signature"] = ""
        with pytest.raises(MandateSignatureError):
            mandates.validate(raw)

    def test_mandate_signed_with_another_secret_fails(self, settings, mandate_factory):
        other = MandateService(secret="a-completely-different-secret", settings=settings)
        with pytest.raises(MandateSignatureError):
            other.validate(mandate_factory().model_dump(mode="json"))

    def test_expired_mandate_is_rejected(self, mandates, mandate_factory):
        mandate = mandate_factory(ttl_seconds=60)
        future = datetime.now(UTC) + timedelta(seconds=120)
        with pytest.raises(MandateExpiredError):
            mandates.validate(mandate.model_dump(mode="json"), now=future)

    def test_malformed_document_is_rejected(self, mandates):
        with pytest.raises(MandateMalformedError):
            mandates.validate({"not": "a mandate"})

    def test_nonce_can_only_be_consumed_once(self, settings):
        service = MandateService(settings=settings, nonce_store=InMemoryNonceStore())
        mandate = service.create_mandate(MandateRequest(maximum_amount=100000))
        raw = mandate.model_dump(mode="json")
        service.validate(raw, consume_nonce=True)
        with pytest.raises(MandateReplayError):
            service.validate(raw, consume_nonce=True)

    def test_sqlite_nonce_store_survives_restart(self, settings, tmp_path):
        path = str(tmp_path / "nonces.db")
        service = MandateService(settings=settings, nonce_store=SQLiteNonceStore(path))
        mandate = service.create_mandate(MandateRequest(maximum_amount=100000))
        service.validate(mandate.model_dump(mode="json"), consume_nonce=True)

        restarted = MandateService(settings=settings, nonce_store=SQLiteNonceStore(path))
        with pytest.raises(MandateReplayError):
            restarted.validate(mandate.model_dump(mode="json"), consume_nonce=True)


class TestDeterministicAuthorization:
    def test_within_budget_is_approved(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=200000)
        result = mandates.authorize(mandate, [item(price=150000)])
        assert result.approved
        assert result.total_amount == 150000
        assert result.remaining_budget == 50000

    def test_exact_budget_is_approved(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=150000)
        assert mandates.authorize(mandate, [item(price=150000)]).approved

    def test_one_paisa_over_budget_is_rejected(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=150000)
        result = mandates.authorize(mandate, [item(price=150001)])
        assert not result.approved
        assert result.reason == "budget_exceeded"

    def test_quantity_multiplies_into_the_total(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=150000)
        assert not mandates.authorize(mandate, [item(price=80000, quantity=2)]).approved

    def test_category_outside_scope_is_rejected(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=500000, allowed_categories=["audio"])
        result = mandates.authorize(mandate, [item(category="computing", price=10000)])
        assert not result.approved
        assert any(v.startswith("category_out_of_scope") for v in result.violations)

    def test_product_outside_scope_is_rejected(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=500000, allowed_product_ids=["prd_hp_001"])
        result = mandates.authorize(mandate, [item(product_id="prd_cm_010", price=10000)])
        assert not result.approved
        assert any(v.startswith("product_out_of_scope") for v in result.violations)

    def test_currency_mismatch_is_rejected(self, mandates, mandate_factory):
        result = mandates.authorize(mandate_factory(), [item(price=1000)], currency="USD")
        assert not result.approved

    def test_item_count_limit_is_enforced(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=500000, max_items=2)
        result = mandates.authorize(mandate, [item(price=1000, quantity=5)])
        assert not result.approved
        assert any(v.startswith("item_count_exceeded") for v in result.violations)

    def test_upsell_blocked_when_not_permitted(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=500000, allow_upsell=False)
        result = mandates.authorize(mandate, [item(price=1000, kind="upsell")])
        assert not result.approved

    def test_enforce_raises_budget_error(self, mandates, mandate_factory):
        with pytest.raises(BudgetExceededError):
            mandates.enforce(mandate_factory(amount=100000), [item(price=200000)])

    def test_enforce_raises_scope_error(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=500000, allowed_categories=["audio"])
        with pytest.raises(MandateScopeError):
            mandates.enforce(mandate, [item(category="home", price=1000)])

    def test_enforce_raises_currency_error(self, mandates, mandate_factory):
        with pytest.raises(MandateCurrencyError):
            mandates.enforce(mandate_factory(), [item(price=100)], currency="EUR")

    def test_authorization_is_reproducible(self, mandates, mandate_factory):
        mandate = mandate_factory(amount=200000)
        basket = [item(price=99999, quantity=2)]
        assert mandates.authorize(mandate, basket) == mandates.authorize(mandate, basket)
