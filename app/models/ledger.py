"""Audit ledger models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

GENESIS_HASH = "0" * 64


class EventType:
    """Canonical audit event types (string constants, not an Enum, so that
    unknown/forward-compatible values from storage never fail to load)."""

    # Buyer agent
    PURCHASE_REQUESTED = "purchase.requested"
    INTENT_INTERPRETED = "intent.interpreted"
    MANDATE_CREATED = "mandate.created"
    PRODUCT_SELECTED = "product.selected"
    UPSELL_EVALUATED = "upsell.evaluated"
    UPSELL_REJECTED = "upsell.rejected"
    UPSELL_ACCEPTED = "upsell.accepted"
    PURCHASE_COMPLETED = "purchase.completed"
    PURCHASE_FAILED = "purchase.failed"

    # Merchant agent
    MANDATE_VALIDATED = "mandate.validated"
    MANDATE_REJECTED = "mandate.rejected"
    INVENTORY_CHECKED = "inventory.checked"
    INVENTORY_REJECTED = "inventory.rejected"
    INVENTORY_RESERVED = "inventory.reserved"
    INVENTORY_RELEASED = "inventory.released"
    INVENTORY_COMMITTED = "inventory.committed"
    ORDER_CREATED = "order.created"
    ORDER_FAILED = "order.failed"
    PAYMENT_VERIFIED = "payment.verified"
    PAYMENT_REJECTED = "payment.rejected"
    TRANSACTION_CONFIRMED = "transaction.confirmed"

    # Protocol / infrastructure
    A2A_REQUEST = "a2a.request"
    A2A_RESPONSE = "a2a.response"
    A2A_ERROR = "a2a.error"
    MCP_TOOL_CALLED = "mcp.tool_called"
    AI_INVOKED = "ai.invoked"
    AI_FAILED = "ai.failed"
    SYSTEM_ERROR = "system.error"


class LedgerEvent(BaseModel):
    """One tamper-evident entry in the hash chain."""

    sequence: int = Field(ge=1)
    event_id: str
    timestamp: str
    event_type: str
    transaction_id: str
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str
    current_hash: str

    def chain_core(self) -> dict[str, Any]:
        """The exact fields protected by `current_hash`."""
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "transaction_id": self.transaction_id,
            "actor": self.actor,
            "payload": self.payload,
        }


class LedgerVerificationResult(BaseModel):
    valid: bool
    events_checked: int = 0
    broken_at_sequence: int | None = None
    reason: str | None = None
    head_hash: str = GENESIS_HASH
