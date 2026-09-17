"""Inventory availability, reservation and ordering-guarantee tests."""

from __future__ import annotations

import pytest

from app.core.exceptions import InsufficientInventoryError, ProductNotFoundError

IN_STOCK = "prd_hp_002"   # 30 units
OUT_OF_STOCK = "prd_eb_003"  # 0 units
LOW_STOCK = "prd_cm_010"  # 9 units


class TestAvailability:
    def test_available_product_is_in_stock(self, inventory):
        result = inventory.check(IN_STOCK, 1)
        assert result.in_stock
        assert result.available_quantity == 30

    def test_out_of_stock_product_is_not_available(self, inventory):
        result = inventory.check(OUT_OF_STOCK, 1)
        assert not result.in_stock
        assert result.reason == "insufficient_inventory"

    def test_quantity_beyond_stock_is_rejected(self, inventory):
        assert not inventory.check(LOW_STOCK, 50).in_stock

    def test_exact_stock_quantity_is_allowed(self, inventory):
        assert inventory.check(LOW_STOCK, 9).in_stock

    def test_zero_quantity_is_invalid(self, inventory):
        result = inventory.check(IN_STOCK, 0)
        assert not result.in_stock
        assert result.reason == "invalid_quantity"

    def test_unknown_product_raises(self, inventory):
        with pytest.raises(ProductNotFoundError):
            inventory.check("prd_does_not_exist", 1)

    def test_require_raises_when_short(self, inventory):
        with pytest.raises(InsufficientInventoryError):
            inventory.require(OUT_OF_STOCK, 1)


class TestReservations:
    def test_reservation_reduces_availability(self, inventory):
        inventory.reserve("txn_1", {LOW_STOCK: 4})
        assert inventory.available(LOW_STOCK) == 5

    def test_reservations_cannot_oversell(self, inventory):
        inventory.reserve("txn_1", {LOW_STOCK: 9})
        with pytest.raises(InsufficientInventoryError):
            inventory.reserve("txn_2", {LOW_STOCK: 1})

    def test_reservation_is_all_or_nothing(self, inventory):
        with pytest.raises(InsufficientInventoryError):
            inventory.reserve("txn_1", {IN_STOCK: 1, OUT_OF_STOCK: 1})
        assert inventory.available(IN_STOCK) == 30
        assert inventory.open_reservations() == []

    def test_release_returns_stock(self, inventory):
        reservation = inventory.reserve("txn_1", {LOW_STOCK: 4})
        inventory.release(reservation.reservation_id)
        assert inventory.available(LOW_STOCK) == 9

    def test_commit_decrements_real_stock(self, inventory, catalog):
        reservation = inventory.reserve("txn_1", {LOW_STOCK: 3})
        inventory.commit(reservation.reservation_id)
        assert catalog.get_product(LOW_STOCK).inventory == 6
        assert inventory.available(LOW_STOCK) == 6

    def test_commit_is_idempotent(self, inventory, catalog):
        reservation = inventory.reserve("txn_1", {LOW_STOCK: 3})
        inventory.commit(reservation.reservation_id)
        inventory.commit(reservation.reservation_id)
        assert catalog.get_product(LOW_STOCK).inventory == 6

    def test_released_reservation_cannot_be_committed(self, inventory):
        reservation = inventory.reserve("txn_1", {LOW_STOCK: 3})
        inventory.release(reservation.reservation_id)
        with pytest.raises(Exception):
            inventory.commit(reservation.reservation_id)


class TestOrderingGuarantee:
    """Inventory must be checked and held *before* any order is created."""

    def test_no_order_is_created_when_stock_is_missing(self, merchant, mandate_factory):
        mandate = mandate_factory(amount=500000)
        before = len(merchant.payments.client.orders)
        with pytest.raises(InsufficientInventoryError):
            merchant.create_order(
                transaction_id="txn_stock",
                mandate_raw=mandate.model_dump(mode="json"),
                items=[{"product_id": OUT_OF_STOCK, "quantity": 1}],
            )
        assert len(merchant.payments.client.orders) == before

    def test_order_creation_holds_stock(self, merchant, mandate_factory):
        mandate = mandate_factory(amount=300000)  # two headphones at 799 each
        merchant.create_order(
            transaction_id="txn_hold",
            mandate_raw=mandate.model_dump(mode="json"),
            items=[{"product_id": IN_STOCK, "quantity": 2}],
        )
        assert merchant.inventory.available(IN_STOCK) == 28

    def test_failed_mandate_never_touches_stock(self, merchant, mandate_factory):
        mandate = mandate_factory(amount=1000)  # far too small
        with pytest.raises(Exception):
            merchant.create_order(
                transaction_id="txn_budget",
                mandate_raw=mandate.model_dump(mode="json"),
                items=[{"product_id": LOW_STOCK, "quantity": 1}],
            )
        assert merchant.inventory.available(LOW_STOCK) == 9
