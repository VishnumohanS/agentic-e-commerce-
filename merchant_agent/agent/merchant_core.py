"""Merchant agent business logic.

Purchase pipeline (order is a security property, not a style choice):

    validate mandate -> validate products -> check & reserve inventory
      -> compute total -> enforce mandate limit -> create Razorpay order
      -> [buyer pays] -> verify payment -> commit inventory -> confirm

Every step writes an audit event, and every failure writes one too, so a
rejected transaction is exactly as auditable as a successful one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import (
    AgenticCommerceError,
    InsufficientInventoryError,
    MandateError,
    PaymentError,
    ProductNotFoundError,
    ProtocolError,
)
from app.core.logging import get_logger
from app.models.catalog import (
    OrderResult,
    Product,
    PurchaseConfirmation,
    UpsellOffer,
)
from app.models.ledger import EventType
from app.models.mandate import LineItem, SpendingMandate
from app.services.ap2 import MandateService
from app.services.catalog_service import CatalogService
from app.services.inventory_service import InventoryService
from app.services.ledger import AuditLedger
from app.services.razorpay_service import RazorpayService

logger = get_logger(__name__)

ACTOR = "merchant_agent"


class PendingOrder:
    """Server-side record binding an order to its mandate and reservation.

    The buyer cannot influence the amount at confirmation time: the amount used
    for payment verification is read from here, not from the request body.
    """

    __slots__ = (
        "transaction_id",
        "order_id",
        "amount",
        "currency",
        "items",
        "reservation_id",
        "mandate_id",
        "buyer_id",
        "created_at",
        "status",
    )

    def __init__(
        self,
        *,
        transaction_id: str,
        order_id: str,
        amount: int,
        currency: str,
        items: list[LineItem],
        reservation_id: str,
        mandate_id: str,
        buyer_id: str,
    ) -> None:
        self.transaction_id = transaction_id
        self.order_id = order_id
        self.amount = amount
        self.currency = currency
        self.items = items
        self.reservation_id = reservation_id
        self.mandate_id = mandate_id
        self.buyer_id = buyer_id
        self.created_at = datetime.now(UTC)
        self.status = "created"


class MerchantAgent:
    """Coordinates catalog, mandate, inventory, payment and ledger services."""

    def __init__(
        self,
        *,
        catalog: CatalogService,
        inventory: InventoryService,
        mandates: MandateService,
        payments: RazorpayService,
        ledger: AuditLedger,
    ) -> None:
        self._catalog = catalog
        self._inventory = inventory
        self._mandates = mandates
        self._payments = payments
        self._ledger = ledger
        self._orders: dict[str, PendingOrder] = {}

    # --- accessors -------------------------------------------------------

    @property
    def catalog(self) -> CatalogService:
        return self._catalog

    @property
    def inventory(self) -> InventoryService:
        return self._inventory

    @property
    def ledger(self) -> AuditLedger:
        return self._ledger

    @property
    def payments(self) -> RazorpayService:
        return self._payments

    def get_order(self, order_id: str) -> PendingOrder | None:
        return self._orders.get(order_id)

    def _audit(self, event_type: str, transaction_id: str, payload: dict[str, Any]) -> str:
        return self._ledger.append(
            event_type, transaction_id=transaction_id, payload=payload, actor=ACTOR
        ).event_id

    def _audit_failure(self, transaction_id: str, stage: str, exc: Exception) -> None:
        if isinstance(exc, AgenticCommerceError):
            payload = {"stage": stage, **exc.to_dict()}
        else:
            payload = {
                "stage": stage,
                "code": "internal_error",
                "message": type(exc).__name__,
                "details": {},
            }
        event_type = {
            "mandate": EventType.MANDATE_REJECTED,
            "inventory": EventType.INVENTORY_REJECTED,
            "order": EventType.ORDER_FAILED,
            "payment": EventType.PAYMENT_REJECTED,
        }.get(stage, EventType.SYSTEM_ERROR)
        self._audit(event_type, transaction_id, payload)

    # --- quoting ---------------------------------------------------------

    def quote(
        self,
        *,
        transaction_id: str,
        product_id: str,
        quantity: int,
        mandate_raw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Price a basket and optionally propose an add-on.

        Called before any money moves; it never reserves stock. If a mandate is
        supplied the merchant only proposes upsells that fit the remaining
        headroom, but the buyer re-checks the numbers itself.
        """
        product = self._catalog.get_product(product_id)
        quantity = max(1, int(quantity))
        availability = self._inventory.check(product_id, quantity)
        base_total = product.price * quantity

        headroom: int | None = None
        mandate_id = None
        if mandate_raw:
            mandate = self._mandates.validate(mandate_raw)
            mandate_id = mandate.mandate_id
            headroom = max(mandate.maximum_amount - base_total, 0)
            if not mandate.allow_upsell:
                headroom = 0

        offer: UpsellOffer | None = (
            self._catalog.build_upsell_offer(product_id, headroom=headroom)
            if availability.in_stock
            else None
        )

        self._audit(
            EventType.INVENTORY_CHECKED,
            transaction_id,
            {
                "product_id": product_id,
                "quantity": quantity,
                "available": availability.available_quantity,
                "in_stock": availability.in_stock,
                "mandate_id": mandate_id,
            },
        )
        return {
            "transaction_id": transaction_id,
            "product": product.public_view(),
            "quantity": quantity,
            "base_total": base_total,
            "currency": product.currency,
            "availability": availability.model_dump(),
            "upsell_offer": offer.model_dump() if offer else None,
        }

    # --- order creation --------------------------------------------------

    def create_order(
        self,
        *,
        transaction_id: str,
        mandate_raw: dict[str, Any],
        items: list[dict[str, Any]],
        buyer_id: str = "buyer-demo",
        currency: str = "INR",
    ) -> OrderResult:
        """Run the full pre-payment pipeline and create a Razorpay test order."""
        reservation_id: str | None = None
        try:
            # 1. Mandate: signature, expiry, ceiling, replay.
            mandate = self._mandates.validate(mandate_raw, consume_nonce=True)
            if mandate.buyer_id != buyer_id:
                raise MandateError(
                    "Mandate buyer does not match the requesting agent",
                    details={"mandate_id": mandate.mandate_id},
                )
            self._audit(
                EventType.MANDATE_VALIDATED,
                transaction_id,
                {
                    "mandate_id": mandate.mandate_id,
                    "buyer_id": mandate.buyer_id,
                    "maximum_amount": mandate.maximum_amount,
                    "currency": mandate.currency,
                    "expires_at": mandate.expires_at.isoformat(),
                },
            )

            # 2. Products must exist; prices come from the catalog, never the request.
            line_items = self._resolve_items(items)
            if not line_items:
                raise ProtocolError("Order must contain at least one item")

            # 3. Inventory BEFORE any order creation.
            quantities: dict[str, int] = {}
            for item in line_items:
                quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
            for product_id, quantity in quantities.items():
                self._inventory.require(product_id, quantity)

            # 4/5. Total and deterministic mandate enforcement.
            authorization = self._mandates.enforce(mandate, line_items, currency=currency)
            total = authorization.total_amount

            # 6. Hold the stock, then create the order.
            reservation = self._inventory.reserve(transaction_id, quantities)
            reservation_id = reservation.reservation_id
            self._audit(
                EventType.INVENTORY_RESERVED,
                transaction_id,
                {"reservation_id": reservation_id, "items": quantities},
            )

            order = self._payments.create_order(
                amount=total,
                currency=currency,
                receipt=transaction_id,
                notes={
                    "transaction_id": transaction_id,
                    "mandate_id": mandate.mandate_id,
                    "buyer_id": buyer_id,
                },
            )

            pending = PendingOrder(
                transaction_id=transaction_id,
                order_id=str(order["id"]),
                amount=total,
                currency=currency,
                items=line_items,
                reservation_id=reservation_id,
                mandate_id=mandate.mandate_id,
                buyer_id=buyer_id,
            )
            self._orders[pending.order_id] = pending

            self._audit(
                EventType.ORDER_CREATED,
                transaction_id,
                {
                    "order_id": pending.order_id,
                    "amount": total,
                    "currency": currency,
                    "mandate_id": mandate.mandate_id,
                    "remaining_budget": authorization.remaining_budget,
                    "items": [item.model_dump() for item in line_items],
                },
            )
            return OrderResult(
                transaction_id=transaction_id,
                order_id=pending.order_id,
                amount=total,
                currency=currency,
                status="created",
                reservation_id=reservation_id,
                items=[item.model_dump() for item in line_items],
                razorpay_key_id=self._payments.key_id,
                created_at=pending.created_at,
            )
        except Exception as exc:
            if reservation_id:
                self._inventory.release(reservation_id)
                self._audit(
                    EventType.INVENTORY_RELEASED,
                    transaction_id,
                    {"reservation_id": reservation_id, "reason": "order_creation_failed"},
                )
            stage = _stage_for(exc)
            self._audit_failure(transaction_id, stage, exc)
            logger.warning(
                "Order creation rejected",
                extra={
                    "transaction_id": transaction_id,
                    "stage": stage,
                    "error_type": type(exc).__name__,
                },
            )
            raise

    def _resolve_items(self, items: list[dict[str, Any]]) -> list[LineItem]:
        """Build line items from catalog truth, ignoring client-supplied prices."""
        resolved: list[LineItem] = []
        for raw in items:
            if not isinstance(raw, dict):
                raise ProtocolError("Each order item must be an object")
            product_id = str(raw.get("product_id", "")).strip()
            if not product_id:
                raise ProtocolError("Order item is missing 'product_id'")
            quantity = int(raw.get("quantity", 1) or 1)
            if quantity < 1:
                raise ProtocolError(f"Invalid quantity for '{product_id}'")
            product: Product = self._catalog.get_product(product_id)
            resolved.append(
                LineItem(
                    product_id=product.product_id,
                    name=product.name,
                    category=product.category,
                    unit_price=product.price,
                    quantity=quantity,
                    kind=str(raw.get("kind", "primary")),
                )
            )
        return resolved

    # --- payment confirmation -------------------------------------------

    def confirm_payment(
        self,
        *,
        transaction_id: str,
        order_id: str,
        payment_id: str,
        signature: str,
    ) -> PurchaseConfirmation:
        """Verify the payment, then and only then commit stock and confirm."""
        pending = self._orders.get(order_id)
        if pending is None:
            error = PaymentError(
                "Unknown order - cannot confirm a payment for it",
                details={"order_id": order_id},
            )
            self._audit_failure(transaction_id, "payment", error)
            raise error
        if pending.transaction_id != transaction_id:
            error = PaymentError(
                "Order does not belong to this transaction",
                details={"order_id": order_id},
            )
            self._audit_failure(transaction_id, "payment", error)
            raise error
        if pending.status == "confirmed":
            return self._confirmation_for(pending, payment_id)

        try:
            payment = self._payments.verify_payment(
                order_id=order_id,
                payment_id=payment_id,
                signature=signature,
                expected_amount=pending.amount,  # server-side amount, not client-supplied
                currency=pending.currency,
            )
        except Exception as exc:
            self._inventory.release(pending.reservation_id)
            pending.status = "failed"
            self._audit(
                EventType.INVENTORY_RELEASED,
                transaction_id,
                {"reservation_id": pending.reservation_id, "reason": "payment_verification_failed"},
            )
            self._audit_failure(transaction_id, "payment", exc)
            raise

        verified_event = self._audit(
            EventType.PAYMENT_VERIFIED,
            transaction_id,
            {
                "order_id": order_id,
                "payment_id": payment_id,
                "amount": int(payment.get("amount", pending.amount)),
                "currency": pending.currency,
                "status": payment.get("status"),
                "method": payment.get("method"),
            },
        )

        self._inventory.commit(pending.reservation_id)
        committed_event = self._audit(
            EventType.INVENTORY_COMMITTED,
            transaction_id,
            {"reservation_id": pending.reservation_id},
        )

        pending.status = "confirmed"
        confirmed_event = self._audit(
            EventType.TRANSACTION_CONFIRMED,
            transaction_id,
            {
                "order_id": order_id,
                "payment_id": payment_id,
                "amount": pending.amount,
                "currency": pending.currency,
                "mandate_id": pending.mandate_id,
                "items": [item.model_dump() for item in pending.items],
            },
        )
        logger.info(
            "Transaction confirmed",
            extra={
                "transaction_id": transaction_id,
                "order_id": order_id,
                "amount": pending.amount,
                "status": "confirmed",
            },
        )
        return self._confirmation_for(
            pending, payment_id, [verified_event, committed_event, confirmed_event]
        )

    def _confirmation_for(
        self,
        pending: PendingOrder,
        payment_id: str,
        event_ids: list[str] | None = None,
    ) -> PurchaseConfirmation:
        return PurchaseConfirmation(
            transaction_id=pending.transaction_id,
            order_id=pending.order_id,
            payment_id=payment_id,
            status="confirmed",
            amount=pending.amount,
            currency=pending.currency,
            items=[item.model_dump() for item in pending.items],
            confirmed_at=datetime.now(UTC),
            ledger_event_ids=event_ids or [],
        )

    # --- webhooks --------------------------------------------------------

    def handle_webhook(self, event: dict[str, Any]) -> dict[str, Any]:
        """Record a verified Razorpay webhook in the ledger.

        Webhooks are treated as *notifications*. The authoritative confirmation
        path is still signature + status verification in `confirm_payment`.
        """
        event_name = str(event.get("event", "unknown"))
        payload = event.get("payload", {}) or {}
        payment_entity = (payload.get("payment", {}) or {}).get("entity", {}) or {}
        order_id = str(payment_entity.get("order_id", "")) or "unknown"
        pending = self._orders.get(order_id)
        transaction_id = pending.transaction_id if pending else f"webhook_{uuid.uuid4().hex[:12]}"
        self._audit(
            EventType.PAYMENT_VERIFIED if "captured" in event_name else EventType.A2A_RESPONSE,
            transaction_id,
            {
                "source": "razorpay_webhook",
                "event": event_name,
                "order_id": order_id,
                "payment_id": payment_entity.get("id"),
                "status": payment_entity.get("status"),
                "amount": payment_entity.get("amount"),
            },
        )
        return {"acknowledged": True, "event": event_name, "transaction_id": transaction_id}


def _stage_for(exc: Exception) -> str:
    if isinstance(exc, MandateError):
        return "mandate"
    if isinstance(exc, (InsufficientInventoryError,)):
        return "inventory"
    if isinstance(exc, PaymentError):
        return "payment"
    if isinstance(exc, (ProductNotFoundError, ProtocolError)):
        return "order"
    return "order"
