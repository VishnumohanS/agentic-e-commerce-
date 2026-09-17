"""End-to-end buyer -> A2A -> merchant -> payment -> ledger tests."""

from __future__ import annotations

from app.core.money import to_minor


class TestHappyPath:
    def test_purchase_completes(self, buyer):
        outcome = buyer.purchase("wireless bluetooth headphones under 2500 rupees")
        assert outcome.status == "completed"
        assert outcome.confirmation["status"] == "confirmed"

    def test_charge_never_exceeds_the_mandate(self, buyer):
        outcome = buyer.purchase("bluetooth headphones", budget=to_minor(2000))
        assert outcome.confirmation["amount"] <= outcome.mandate["maximum_amount"]

    def test_decision_trail_covers_every_stage(self, buyer):
        outcome = buyer.purchase("bluetooth headphones under 2500")
        stages = [step.stage for step in outcome.steps]
        for stage in ("request", "intent", "mandate", "search", "selection", "order", "confirmation"):
            assert stage in stages

    def test_mandate_signature_is_truncated_in_output(self, buyer):
        outcome = buyer.purchase("bluetooth headphones under 2500")
        assert outcome.mandate["signature"].endswith("...")

    def test_explicit_budget_overrides_the_request_text(self, buyer):
        outcome = buyer.purchase("headphones under 50000 rupees", budget=to_minor(900))
        assert outcome.mandate["maximum_amount"] == to_minor(900)

    def test_inventory_is_decremented_after_confirmation(self, buyer, merchant_container):
        before = merchant_container.inventory.available("prd_hp_002")
        outcome = buyer.purchase("cheap bluetooth headphones", budget=to_minor(900))
        assert outcome.status == "completed"
        assert merchant_container.inventory.available("prd_hp_002") == before - 1

    def test_auto_pay_disabled_stops_before_payment(self, buyer):
        outcome = buyer.purchase("bluetooth headphones under 2500", auto_pay=False)
        assert outcome.status == "awaiting_payment"
        assert outcome.confirmation is None


class TestFailurePaths:
    def test_empty_request_is_rejected(self, buyer):
        outcome = buyer.purchase("   ")
        assert outcome.status == "rejected"

    def test_unaffordable_request_fails_cleanly(self, buyer):
        outcome = buyer.purchase("mechanical keyboard", budget=to_minor(10))
        assert outcome.status == "failed"
        assert outcome.error["code"] == "purchase_rejected"

    def test_no_order_is_created_when_the_budget_is_too_low(self, buyer, merchant_container):
        buyer.purchase("mechanical keyboard", budget=to_minor(10))
        assert merchant_container.payments.client.orders == {}

    def test_merchant_outage_is_handled(self, buyer, transport):
        transport.shutdown()
        outcome = buyer.purchase("headphones under 2000")
        assert outcome.status == "failed"

    def test_model_failure_degrades_but_still_buys(self, buyer, monkeypatch):
        """If Bedrock is unavailable the agent falls back deterministically."""
        from app.core.exceptions import AIProviderError

        def broken(*args, **kwargs):
            raise AIProviderError("bedrock down")

        monkeypatch.setattr(buyer._provider, "complete_json", broken)
        outcome = buyer.purchase("bluetooth headphones", budget=to_minor(1000))
        assert outcome.status == "completed"

    def test_stock_is_released_when_payment_verification_fails(self, buyer, merchant_container):
        from app.models.mandate import MandateRequest

        mandate = merchant_container.mandates.create_mandate(
            MandateRequest(maximum_amount=to_minor(2000))
        )
        before = merchant_container.inventory.available("prd_hp_002")
        order = merchant_container.merchant.create_order(
            transaction_id="txn_fail",
            mandate_raw=mandate.model_dump(mode="json"),
            items=[{"product_id": "prd_hp_002", "quantity": 1}],
        )
        assert merchant_container.inventory.available("prd_hp_002") == before - 1
        try:
            merchant_container.merchant.confirm_payment(
                transaction_id="txn_fail",
                order_id=order.order_id,
                payment_id="pay_x",
                signature="bad",
            )
        except Exception:
            pass
        assert merchant_container.inventory.available("prd_hp_002") == before


class TestAuditTrail:
    def test_both_chains_stay_valid_after_a_purchase(self, buyer, buyer_container, merchant_container):
        buyer.purchase("bluetooth headphones under 2500")
        assert buyer_container.ledger.verify_integrity().valid
        assert merchant_container.ledger.verify_integrity().valid

    def test_failed_purchases_are_recorded(self, buyer, buyer_container):
        outcome = buyer.purchase("mechanical keyboard", budget=to_minor(10))
        events = buyer_container.ledger.read_transaction(outcome.transaction_id)
        assert any(event.event_type == "purchase.failed" for event in events)

    def test_merchant_records_the_confirmation(self, buyer, merchant_container):
        outcome = buyer.purchase("bluetooth headphones under 2500")
        events = merchant_container.ledger.read_transaction(outcome.transaction_id)
        types = {event.event_type for event in events}
        assert {"mandate.validated", "inventory.reserved", "order.created",
                "payment.verified", "transaction.confirmed"} <= types

    def test_ledger_payloads_never_contain_the_mandate_signature(self, buyer, merchant_container):
        outcome = buyer.purchase("bluetooth headphones under 2500")
        for event in merchant_container.ledger.read_transaction(outcome.transaction_id):
            assert "***REDACTED***" == event.payload.get("signature", "***REDACTED***")


class TestBuyerAPI:
    def test_purchase_endpoint(self, settings, buyer_container):
        from fastapi.testclient import TestClient
        from buyer_agent.main import create_app

        with TestClient(create_app(settings, buyer_container)) as client:
            response = client.post("/purchase", json={"request": "bluetooth headphones", "budget": 2000})
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    def test_empty_request_is_rejected_by_validation(self, settings, buyer_container):
        from fastapi.testclient import TestClient
        from buyer_agent.main import create_app

        with TestClient(create_app(settings, buyer_container)) as client:
            assert client.post("/purchase", json={"request": ""}).status_code == 422

    def test_negative_budget_is_rejected(self, settings, buyer_container):
        from fastapi.testclient import TestClient
        from buyer_agent.main import create_app

        with TestClient(create_app(settings, buyer_container)) as client:
            assert client.post("/purchase", json={"request": "x", "budget": -5}).status_code == 422

    def test_stream_emits_steps_then_an_outcome(self, settings, buyer_container):
        from fastapi.testclient import TestClient
        from buyer_agent.main import create_app

        with TestClient(create_app(settings, buyer_container)) as client:
            response = client.post(
                "/purchase/stream", json={"request": "bluetooth headphones", "budget": 2000}
            )
        assert "event: step" in response.text
        assert "event: outcome" in response.text

    def test_health_endpoint(self, settings, buyer_container):
        from fastapi.testclient import TestClient
        from buyer_agent.main import create_app

        with TestClient(create_app(settings, buyer_container)) as client:
            assert client.get("/health").json()["agent"] == "buyer_agent"
