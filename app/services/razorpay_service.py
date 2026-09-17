"""Razorpay integration (TEST MODE only).

Two modes:

* `RAZORPAY_MODE=test`  - real Razorpay test-mode API via the official SDK.
* `RAZORPAY_MODE=mock`  - an in-process simulator implementing the same client
  surface, so the end-to-end flow (including HMAC signature verification) can be
  exercised offline and in CI without network access or credentials.

Verification is deliberately two-sided: the HMAC signature must be valid **and**
the payment must actually be captured for the correct amount and order before a
purchase is confirmed. Creating an order proves nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ConfigurationError,
    PaymentAmountMismatchError,
    PaymentError,
    PaymentNotCapturedError,
    PaymentSignatureError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

CAPTURED_STATUSES = {"captured"}
AUTHORIZED_STATUSES = {"authorized"}


class RazorpayClientProtocol(Protocol):
    """Minimal surface the service needs from a Razorpay client."""

    def create_order(self, data: dict[str, Any]) -> dict[str, Any]: ...

    def fetch_order(self, order_id: str) -> dict[str, Any]: ...

    def fetch_payment(self, payment_id: str) -> dict[str, Any]: ...

    def capture_payment(self, payment_id: str, amount: int, currency: str) -> dict[str, Any]: ...


class RealRazorpayClient:
    """Thin adapter over the official `razorpay` SDK."""

    def __init__(self, key_id: str, key_secret: str) -> None:
        try:
            import razorpay
        except ImportError as exc:  # pragma: no cover
            raise ConfigurationError("The 'razorpay' package is required in test mode") from exc
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.set_app_details({"title": "agentic-commerce-platform", "version": "1.0.0"})

    def create_order(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._client.order.create(data=data)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        return self._client.order.fetch(order_id)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self._client.payment.fetch(payment_id)

    def capture_payment(self, payment_id: str, amount: int, currency: str) -> dict[str, Any]:
        return self._client.payment.capture(payment_id, amount, {"currency": currency})


class MockRazorpayClient:
    """Offline simulator with the same shapes as the Razorpay API."""

    def __init__(self, key_secret: str) -> None:
        self._key_secret = key_secret
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}

    def create_order(self, data: dict[str, Any]) -> dict[str, Any]:
        order_id = f"order_{uuid.uuid4().hex[:14]}"
        order = {
            "id": order_id,
            "entity": "order",
            "amount": int(data["amount"]),
            "amount_paid": 0,
            "amount_due": int(data["amount"]),
            "currency": data.get("currency", "INR"),
            "receipt": data.get("receipt"),
            "status": "created",
            "notes": data.get("notes", {}),
            "created_at": int(time.time()),
        }
        self.orders[order_id] = order
        return order

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if order is None:
            raise PaymentError("Order not found", details={"order_id": order_id})
        return order

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        payment = self.payments.get(payment_id)
        if payment is None:
            raise PaymentError("Payment not found", details={"payment_id": payment_id})
        return payment

    def capture_payment(self, payment_id: str, amount: int, currency: str) -> dict[str, Any]:
        payment = self.fetch_payment(payment_id)
        payment["status"] = "captured"
        payment["amount"] = amount
        self.payments[payment_id] = payment
        return payment

    # --- simulation helpers (mock mode only) -----------------------------

    def simulate_payment(
        self, order_id: str, *, status: str = "captured", amount: int | None = None
    ) -> dict[str, Any]:
        """Pretend the buyer completed checkout; returns id + valid signature."""
        order = self.fetch_order(order_id)
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        paid = amount if amount is not None else int(order["amount"])
        self.payments[payment_id] = {
            "id": payment_id,
            "entity": "payment",
            "order_id": order_id,
            "amount": paid,
            "currency": order["currency"],
            "status": status,
            "method": "upi",
            "captured": status == "captured",
            "created_at": int(time.time()),
        }
        if status == "captured":
            order["status"] = "paid"
            order["amount_paid"] = paid
            order["amount_due"] = max(int(order["amount"]) - paid, 0)
        signature = hmac.new(
            self._key_secret.encode("utf-8"),
            f"{order_id}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {"order_id": order_id, "payment_id": payment_id, "signature": signature}


class RazorpayService:
    """Order creation and strict payment verification."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: RazorpayClientProtocol | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.mode = self._settings.razorpay_mode
        self._key_id = self._settings.razorpay_key_id or "rzp_test_mock_key"
        self._key_secret = self._settings.razorpay_key_secret or "mock_key_secret"

        if client is not None:
            self._client: RazorpayClientProtocol = client
        elif self.mode == "test":
            if not self._settings.razorpay_key_id or not self._settings.razorpay_key_secret:
                raise ConfigurationError(
                    "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required when RAZORPAY_MODE=test"
                )
            if not self._settings.razorpay_key_id.startswith("rzp_test_"):
                raise ConfigurationError(
                    "Refusing to start: RAZORPAY_KEY_ID is not a test-mode key (rzp_test_...)"
                )
            self._client = RealRazorpayClient(self._key_id, self._key_secret)
        else:
            self._client = MockRazorpayClient(self._key_secret)
            logger.warning(
                "Razorpay running in offline MOCK mode - no real API calls will be made"
            )

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def client(self) -> RazorpayClientProtocol:
        return self._client

    # --- orders ----------------------------------------------------------

    def create_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise PaymentError("Order amount must be positive", details={"amount": amount})
        payload = {
            "amount": int(amount),
            "currency": currency.upper(),
            "receipt": receipt[:40],
            "payment_capture": 1,
            "notes": notes or {},
        }
        try:
            order = self._client.create_order(payload)
        except PaymentError:
            raise
        except Exception as exc:
            logger.error(
                "Razorpay order creation failed",
                extra={"error_type": type(exc).__name__, "receipt": receipt},
            )
            raise PaymentError(
                "Razorpay order creation failed",
                details={"error_type": type(exc).__name__},
            ) from exc
        logger.info(
            "Razorpay order created",
            extra={
                "order_id": order.get("id"),
                "amount": order.get("amount"),
                "currency": order.get("currency"),
                "mode": self.mode,
            },
        )
        return order

    # --- verification ----------------------------------------------------

    def expected_signature(self, order_id: str, payment_id: str) -> str:
        return hmac.new(
            self._key_secret.encode("utf-8"),
            f"{order_id}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Constant-time HMAC-SHA256 check of `order_id|payment_id`."""
        if not signature:
            return False
        return hmac.compare_digest(self.expected_signature(order_id, payment_id), signature)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        secret = self._settings.razorpay_webhook_secret
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        try:
            return self._client.fetch_payment(payment_id)
        except PaymentError:
            raise
        except Exception as exc:
            raise PaymentError(
                "Could not fetch payment from Razorpay",
                details={"payment_id": payment_id, "error_type": type(exc).__name__},
            ) from exc

    def verify_payment(
        self,
        *,
        order_id: str,
        payment_id: str,
        signature: str,
        expected_amount: int,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Full verification. Raises unless the payment is genuinely captured.

        1. HMAC signature must match.
        2. Payment must exist and belong to this order.
        3. Amount and currency must match the order exactly.
        4. Status must be `captured` (an `authorized` payment is captured first).
        """
        if not self.verify_signature(order_id, payment_id, signature):
            logger.warning(
                "Razorpay signature verification failed",
                extra={"order_id": order_id, "payment_id": payment_id},
            )
            raise PaymentSignatureError(
                "Razorpay payment signature is invalid",
                details={"order_id": order_id, "payment_id": payment_id},
            )

        payment = self.fetch_payment(payment_id)

        if payment.get("order_id") and payment["order_id"] != order_id:
            raise PaymentSignatureError(
                "Payment does not belong to this order",
                details={"order_id": order_id, "payment_id": payment_id},
            )

        if int(payment.get("amount", 0)) != int(expected_amount):
            raise PaymentAmountMismatchError(
                "Paid amount does not match the order amount",
                details={
                    "expected": int(expected_amount),
                    "received": int(payment.get("amount", 0)),
                    "order_id": order_id,
                },
            )

        if str(payment.get("currency", currency)).upper() != currency.upper():
            raise PaymentAmountMismatchError(
                "Payment currency does not match the order currency",
                details={"expected": currency.upper(), "received": payment.get("currency")},
            )

        status = str(payment.get("status", "")).lower()
        if status in AUTHORIZED_STATUSES:
            payment = self._client.capture_payment(payment_id, int(expected_amount), currency)
            status = str(payment.get("status", "")).lower()

        if status not in CAPTURED_STATUSES:
            logger.warning(
                "Razorpay payment not captured",
                extra={"order_id": order_id, "payment_id": payment_id, "status": status},
            )
            raise PaymentNotCapturedError(
                f"Payment is not captured (status='{status}')",
                details={"order_id": order_id, "payment_id": payment_id, "status": status},
            )

        logger.info(
            "Razorpay payment verified",
            extra={
                "order_id": order_id,
                "payment_id": payment_id,
                "amount": payment.get("amount"),
                "status": status,
            },
        )
        return payment

    # --- mock-only convenience ------------------------------------------

    def simulate_payment(
        self, order_id: str, *, status: str = "captured", amount: int | None = None
    ) -> dict[str, Any]:
        """Available only in mock mode; drives the offline demo/smoke test."""
        if not isinstance(self._client, MockRazorpayClient):
            raise ConfigurationError(
                "Payment simulation is only available when RAZORPAY_MODE=mock"
            )
        return self._client.simulate_payment(order_id, status=status, amount=amount)

    def health(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "key_id": self._key_id,
            "test_mode": self._key_id.startswith("rzp_test_") or self.mode == "mock",
        }
