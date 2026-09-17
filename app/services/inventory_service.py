"""Inventory checks and reservations.

Order of operations matters: inventory is checked **and reserved before** a
Razorpay order is created. That closes the "payment succeeded but stock was
gone" window. A reservation is either committed (payment verified) or released
(anything failed).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

from app.core.exceptions import (
    InsufficientInventoryError,
    ReservationNotFoundError,
)
from app.core.logging import get_logger
from app.models.catalog import InventoryCheck, Reservation
from app.services.catalog_service import CatalogService

logger = get_logger(__name__)


class InventoryService:
    """Availability = on-hand stock minus quantities held by open reservations."""

    def __init__(self, catalog: CatalogService) -> None:
        self._catalog = catalog
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}

    # --- reads -----------------------------------------------------------

    def _held(self, product_id: str) -> int:
        return sum(
            reservation.items.get(product_id, 0)
            for reservation in self._reservations.values()
            if not reservation.committed and not reservation.released
        )

    def available(self, product_id: str) -> int:
        with self._lock:
            product = self._catalog.get_product(product_id)
            return max(product.inventory - self._held(product_id), 0)

    def check(self, product_id: str, quantity: int = 1) -> InventoryCheck:
        """Non-raising availability check (used by MCP `check_inventory`)."""
        if quantity < 1:
            return InventoryCheck(
                product_id=product_id,
                requested_quantity=quantity,
                available_quantity=0,
                in_stock=False,
                reason="invalid_quantity",
            )
        available = self.available(product_id)
        in_stock = available >= quantity
        return InventoryCheck(
            product_id=product_id,
            requested_quantity=quantity,
            available_quantity=available,
            in_stock=in_stock,
            reason="available" if in_stock else "insufficient_inventory",
        )

    def require(self, product_id: str, quantity: int = 1) -> InventoryCheck:
        """Raising variant used on the purchase path."""
        result = self.check(product_id, quantity)
        if not result.in_stock:
            raise InsufficientInventoryError(
                f"Only {result.available_quantity} unit(s) of '{product_id}' available",
                details={
                    "product_id": product_id,
                    "requested": quantity,
                    "available": result.available_quantity,
                },
            )
        return result

    # --- reservations ----------------------------------------------------

    def reserve(self, transaction_id: str, items: dict[str, int]) -> Reservation:
        """Atomically hold stock for every item or nothing at all."""
        with self._lock:
            for product_id, quantity in items.items():
                self.require(product_id, quantity)
            reservation = Reservation(
                reservation_id=f"rsv_{uuid.uuid4().hex[:16]}",
                transaction_id=transaction_id,
                items=dict(items),
                created_at=datetime.now(UTC),
            )
            self._reservations[reservation.reservation_id] = reservation
        logger.info(
            "Inventory reserved",
            extra={
                "reservation_id": reservation.reservation_id,
                "transaction_id": transaction_id,
                "items": items,
            },
        )
        return reservation

    def _get(self, reservation_id: str) -> Reservation:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise ReservationNotFoundError(
                f"Reservation '{reservation_id}' not found",
                details={"reservation_id": reservation_id},
            )
        return reservation

    def commit(self, reservation_id: str) -> Reservation:
        """Convert a hold into a real stock decrement (payment verified)."""
        with self._lock:
            reservation = self._get(reservation_id)
            if reservation.released:
                raise ReservationNotFoundError(
                    "Reservation was already released",
                    details={"reservation_id": reservation_id},
                )
            if reservation.committed:
                return reservation
            for product_id, quantity in reservation.items.items():
                self._catalog.adjust_inventory(product_id, -quantity)
            updated = reservation.model_copy(update={"committed": True})
            self._reservations[reservation_id] = updated
        logger.info(
            "Inventory committed",
            extra={"reservation_id": reservation_id, "transaction_id": updated.transaction_id},
        )
        return updated

    def release(self, reservation_id: str) -> Reservation:
        """Return held stock to the pool (payment failed or flow aborted)."""
        with self._lock:
            reservation = self._get(reservation_id)
            if reservation.committed:
                return reservation
            updated = reservation.model_copy(update={"released": True})
            self._reservations[reservation_id] = updated
        logger.info(
            "Inventory released",
            extra={"reservation_id": reservation_id, "transaction_id": updated.transaction_id},
        )
        return updated

    def open_reservations(self) -> list[Reservation]:
        with self._lock:
            return [
                r for r in self._reservations.values() if not r.committed and not r.released
            ]
