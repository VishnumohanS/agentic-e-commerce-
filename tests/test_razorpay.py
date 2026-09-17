"""Razorpay tests. All external calls are mocked; no real payments occur."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    PaymentAmountMismatchError,
    PaymentError,
    PaymentNotCapturedError,
    PaymentSignatureError,
)
from app.services.razorpay_service import MockRazorpayClient, RazorpayService


@pytest.fixture
def order(payments: RazorpayService) -> dict:
    return payments.create_order(amount=149900, currency="INR", receipt="txn_test")


class TestOrderCreation:
    def test_order_is_created_with_the_requested_amount(self, order):
        assert order["amount"] == 149900
        assert order["currency"] == "INR"
        assert order["status"] == "created"

    def test_zero_amount_order_is_refused(self, payments):
        with pytest.raises(PaymentError):
            payments.create_order(amount=0, currency="INR", receipt="txn")

    def test_negative_amount_order_is_refused(self, payments):
        with pytest.raises(PaymentError):
            payments.create_order(amount=-100, currency="INR", receipt="txn")

    def test_sdk_failures_become_payment_errors(self, payments, monkeypatch):
        def boom(_data):
            raise RuntimeError("razorpay is down")

        monkeypatch.setattr(payments.client, "create_order", boom)
        with pytest.raises(PaymentError):
            payments.create_order(amount=1000, currency="INR", receipt="txn")


class TestSignatureVerification:
    def test_valid_signature_passes(self, payments, order):
        payment = payments.simulate_payment(order["id"])
        assert payments.verify_signature(order["id"], payment["payment_id"], payment["signature"])

    def test_invalid_signature_fails(self, payments, order):
        payment = payments.simulate_payment(order["id"])
        assert not payments.verify_signature(order["id"], payment["payment_id"], "deadbeef")

    def test_empty_signature_fails(self, payments, order):
        assert not payments.verify_signature(order["id"], "pay_1", "")

    def test_signature_is_bound_to_the_order(self, payments, order):
        other = payments.create_order(amount=1000, currency="INR", receipt="other")
        payment = payments.simulate_payment(order["id"])
        assert not payments.verify_signature(
            other["id"], payment["payment_id"], payment["signature"]
        )

    def test_signature_is_bound_to_the_payment(self, payments, order):
        payment = payments.simulate_payment(order["id"])
        assert not payments.verify_signature(order["id"], "pay_other", payment["signature"])

    def test_signature_uses_hmac_sha256_of_order_pipe_payment(self, settings, payments, order):
        payment = payments.simulate_payment(order["id"])
        expected = hmac.new(
            settings.razorpay_key_secret.encode() if settings.razorpay_key_secret
            else b"mock_key_secret",
            f"{order['id']}|{payment['payment_id']}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert payment["signature"] == expected


class TestPaymentVerification:
    def test_captured_payment_verifies(self, payments, order):
        payment = payments.simulate_payment(order["id"])
        verified = payments.verify_payment(
            order_id=order["id"],
            payment_id=payment["payment_id"],
            signature=payment["signature"],
            expected_amount=149900,
        )
        assert verified["status"] == "captured"

    def test_forged_signature_is_refused(self, payments, order):
        payment = payments.simulate_payment(order["id"])
        with pytest.raises(PaymentSignatureError):
            payments.verify_payment(
                order_id=order["id"],
                payment_id=payment["payment_id"],
                signature="f" * 64,
                expected_amount=149900,
            )

    def test_failed_payment_is_refused(self, payments, order):
        payment = payments.simulate_payment(order["id"], status="failed")
        with pytest.raises(PaymentNotCapturedError):
            payments.verify_payment(
                order_id=order["id"],
                payment_id=payment["payment_id"],
                signature=payment["signature"],
                expected_amount=149900,
            )

    def test_created_but_unpaid_order_is_refused(self, payments, order):
        payment = payments.simulate_payment(order["id"], status="created")
        with pytest.raises(PaymentNotCapturedError):
            payments.verify_payment(
                order_id=order["id"],
                payment_id=payment["payment_id"],
                signature=payment["signature"],
                expected_amount=149900,
            )

    def test_authorized_payment_is_captured_then_accepted(self, payments, order):
        payment = payments.simulate_payment(order["id"], status="authorized")
        verified = payments.verify_payment(
            order_id=order["id"],
            payment_id=payment["payment_id"],
            signature=payment["signature"],
            expected_amount=149900,
        )
        assert verified["status"] == "captured"

    def test_underpayment_is_refused(self, payments, order):
        payment = payments.simulate_payment(order["id"], amount=100)
        with pytest.raises(PaymentAmountMismatchError):
            payments.verify_payment(
                order_id=order["id"],
                payment_id=payment["payment_id"],
                signature=payment["signature"],
                expected_amount=149900,
            )

    def test_unknown_payment_is_refused(self, payments, order):
        signature = payments.expected_signature(order["id"], "pay_ghost")
        with pytest.raises(PaymentError):
            payments.verify_payment(
                order_id=order["id"],
                payment_id="pay_ghost",
                signature=signature,
                expected_amount=149900,
            )


class TestWebhooks:
    def test_valid_webhook_signature_passes(self, settings, payments):
        body = json.dumps({"event": "payment.captured"}).encode()
        signature = hmac.new(
            settings.razorpay_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        assert payments.verify_webhook_signature(body, signature)

    def test_tampered_webhook_body_fails(self, settings, payments):
        body = json.dumps({"event": "payment.captured"}).encode()
        signature = hmac.new(
            settings.razorpay_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        assert not payments.verify_webhook_signature(b'{"event":"forged"}', signature)

    def test_missing_signature_fails(self, payments):
        assert not payments.verify_webhook_signature(b"{}", "")


class TestConfigurationSafety:
    def test_test_mode_requires_credentials(self, settings):
        unsafe = settings.model_copy(update={"razorpay_mode": "test", "razorpay_key_id": None})
        with pytest.raises(ConfigurationError):
            RazorpayService(settings=unsafe)

    def test_live_keys_are_refused(self, settings):
        unsafe = settings.model_copy(
            update={
                "razorpay_mode": "test",
                "razorpay_key_id": "rzp_live_abc123",
                "razorpay_key_secret": "secret",
            }
        )
        with pytest.raises(ConfigurationError):
            RazorpayService(settings=unsafe)

    def test_simulation_is_unavailable_outside_mock_mode(self, settings):
        service = RazorpayService(
            settings=settings.model_copy(update={"razorpay_mode": "test"}),
            client=MockRazorpayClient("s"),
        )
        service.mode = "test"
        service._client = object()  # type: ignore[assignment]
        with pytest.raises(ConfigurationError):
            service.simulate_payment("order_x")

    def test_health_reports_test_mode(self, payments):
        assert payments.health()["test_mode"] is True
