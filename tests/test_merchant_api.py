"""Merchant HTTP API tests: validation, status codes and error handling."""

from __future__ import annotations

import hashlib
import hmac
import json


class TestHealthAndDiscovery:
    def test_health_reports_ok(self, merchant_client):
        body = merchant_client.get("/health").json()
        assert body["status"] == "ok"
        assert body["agent"] == "merchant_agent"

    def test_health_does_not_leak_secrets(self, merchant_client):
        text = merchant_client.get("/health").text
        assert "test-mandate-secret-value" not in text
        assert "test-a2a-key" not in text

    def test_product_listing(self, merchant_client):
        body = merchant_client.get("/catalog/products").json()
        assert body["count"] == 10

    def test_product_listing_filters_by_category(self, merchant_client):
        body = merchant_client.get("/catalog/products?category=audio").json()
        assert {p["category"] for p in body["products"]} == {"audio"}

    def test_single_product(self, merchant_client):
        assert merchant_client.get("/catalog/products/prd_hp_001").json()["product_id"] == "prd_hp_001"

    def test_unknown_product_returns_404(self, merchant_client):
        response = merchant_client.get("/catalog/products/ghost")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "product_not_found"


class TestInventoryEndpoint:
    def test_available_product(self, merchant_client):
        response = merchant_client.post("/inventory/check", json={"product_id": "prd_hp_002", "quantity": 2})
        assert response.json()["in_stock"] is True

    def test_out_of_stock_product(self, merchant_client):
        response = merchant_client.post("/inventory/check", json={"product_id": "prd_eb_003", "quantity": 1})
        assert response.json()["in_stock"] is False

    def test_invalid_quantity_is_rejected(self, merchant_client):
        response = merchant_client.post("/inventory/check", json={"product_id": "prd_hp_002", "quantity": 0})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_missing_field_is_rejected(self, merchant_client):
        assert merchant_client.post("/inventory/check", json={}).status_code == 422


class TestOrderEndpoints:
    def _mandate(self, merchant_container, amount=300000, **kwargs):
        from app.models.mandate import MandateRequest

        return merchant_container.mandates.create_mandate(
            MandateRequest(maximum_amount=amount, **kwargs)
        ).model_dump(mode="json")

    def test_order_creation_returns_201(self, merchant_client, merchant_container):
        response = merchant_client.post(
            "/order",
            json={
                "transaction_id": "txn_api_1",
                "mandate": self._mandate(merchant_container),
                "items": [{"product_id": "prd_hp_002", "quantity": 1}],
            },
        )
        assert response.status_code == 201
        assert response.json()["amount"] == 79900

    def test_over_budget_order_is_refused(self, merchant_client, merchant_container):
        response = merchant_client.post(
            "/order",
            json={
                "transaction_id": "txn_api_2",
                "mandate": self._mandate(merchant_container, amount=1000),
                "items": [{"product_id": "prd_hp_001", "quantity": 1}],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "mandate_budget_exceeded"

    def test_forged_mandate_is_refused(self, merchant_client, merchant_container):
        mandate = self._mandate(merchant_container)
        mandate["maximum_amount"] = 99999999
        response = merchant_client.post(
            "/order",
            json={"transaction_id": "txn_api_3", "mandate": mandate,
                  "items": [{"product_id": "prd_hp_001", "quantity": 1}]},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "mandate_invalid_signature"

    def test_out_of_stock_order_returns_409(self, merchant_client, merchant_container):
        response = merchant_client.post(
            "/order",
            json={"transaction_id": "txn_api_4", "mandate": self._mandate(merchant_container),
                  "items": [{"product_id": "prd_eb_003", "quantity": 1}]},
        )
        assert response.status_code == 409

    def test_empty_items_are_rejected(self, merchant_client, merchant_container):
        response = merchant_client.post(
            "/order",
            json={"transaction_id": "txn_api_5", "mandate": self._mandate(merchant_container), "items": []},
        )
        assert response.status_code == 422

    def test_client_supplied_prices_are_ignored(self, merchant_client, merchant_container):
        response = merchant_client.post(
            "/order",
            json={
                "transaction_id": "txn_api_6",
                "mandate": self._mandate(merchant_container),
                "items": [{"product_id": "prd_hp_002", "quantity": 1, "unit_price": 1}],
            },
        )
        assert response.json()["amount"] == 79900  # catalog price, not the client's

    def test_replayed_mandate_is_refused(self, merchant_client, merchant_container):
        mandate = self._mandate(merchant_container)
        payload = {"mandate": mandate, "items": [{"product_id": "prd_hp_002", "quantity": 1}]}
        first = merchant_client.post("/order", json={"transaction_id": "txn_r1", **payload})
        second = merchant_client.post("/order", json={"transaction_id": "txn_r2", **payload})
        assert first.status_code == 201
        assert second.json()["error"]["code"] == "mandate_replay_detected"

    def test_full_confirmation_flow(self, merchant_client, merchant_container):
        order = merchant_client.post(
            "/order",
            json={"transaction_id": "txn_api_7", "mandate": self._mandate(merchant_container),
                  "items": [{"product_id": "prd_hp_002", "quantity": 1}]},
        ).json()
        payment = merchant_client.post(
            "/payments/simulate", json={"order_id": order["order_id"]}
        ).json()
        confirmation = merchant_client.post(
            "/order/confirm",
            json={
                "transaction_id": "txn_api_7",
                "order_id": order["order_id"],
                "payment_id": payment["payment_id"],
                "signature": payment["signature"],
            },
        )
        assert confirmation.status_code == 200
        assert confirmation.json()["status"] == "confirmed"

    def test_confirmation_with_a_forged_signature_fails(self, merchant_client, merchant_container):
        order = merchant_client.post(
            "/order",
            json={"transaction_id": "txn_api_8", "mandate": self._mandate(merchant_container),
                  "items": [{"product_id": "prd_hp_002", "quantity": 1}]},
        ).json()
        response = merchant_client.post(
            "/order/confirm",
            json={
                "transaction_id": "txn_api_8",
                "order_id": order["order_id"],
                "payment_id": "pay_forged",
                "signature": "0" * 64,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "payment_signature_invalid"

    def test_confirming_an_unknown_order_fails(self, merchant_client):
        response = merchant_client.post(
            "/order/confirm",
            json={"transaction_id": "t", "order_id": "order_ghost", "payment_id": "p", "signature": "s"},
        )
        assert response.status_code == 502


class TestWebhookEndpoint:
    def test_unsigned_webhook_is_rejected(self, merchant_client):
        assert merchant_client.post("/webhooks/razorpay", json={"event": "payment.captured"}).status_code == 401

    def test_signed_webhook_is_accepted(self, merchant_client, settings):
        body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
        signature = hmac.new(
            settings.razorpay_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        response = merchant_client.post(
            "/webhooks/razorpay", content=body,
            headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["acknowledged"] is True


class TestAuditEndpoints:
    def test_ledger_verification_endpoint(self, merchant_client):
        assert merchant_client.get("/ledger/verify").json()["valid"] is True

    def test_ledger_events_can_be_filtered(self, merchant_client):
        merchant_client.post("/inventory/check", json={"product_id": "prd_hp_002", "quantity": 1})
        body = merchant_client.get("/ledger/events?transaction_id=does_not_exist").json()
        assert body["count"] == 0

    def test_correlation_header_is_returned(self, merchant_client):
        response = merchant_client.get("/catalog/products")
        assert response.headers["X-Transaction-Id"]
