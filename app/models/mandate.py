"""AP2 spending mandate models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

MANDATE_VERSION = "ap2-1.0"


class LineItem(BaseModel):
    """A single item in a purchase request."""

    product_id: str
    name: str = ""
    category: str = ""
    unit_price: int = Field(ge=0, description="Minor units (paise)")
    quantity: int = Field(ge=1)
    kind: str = Field(default="primary", description="'primary' or 'upsell'")

    @property
    def total(self) -> int:
        return self.unit_price * self.quantity


class SpendingMandate(BaseModel):
    """A bounded, signed authorization to spend money on the buyer's behalf.

    The signature covers every field except `signature` itself. The merchant
    agent independently verifies the signature before honouring the mandate.
    """

    version: str = MANDATE_VERSION
    mandate_id: str
    buyer_id: str
    currency: str
    maximum_amount: int = Field(ge=0, description="Hard spending ceiling, minor units")
    allowed_categories: list[str] = Field(default_factory=list)
    allowed_product_ids: list[str] = Field(default_factory=list)
    max_items: int = Field(default=10, ge=1)
    allow_upsell: bool = True
    intent: str = ""
    created_at: datetime
    expires_at: datetime
    nonce: str
    signature: str = ""

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()

    def signing_payload(self) -> dict[str, Any]:
        """Canonical dict covering everything the signature protects."""
        data = self.model_dump(mode="json", exclude={"signature"})
        return data

    def public_view(self) -> dict[str, Any]:
        """Safe representation for UI/logs (signature truncated)."""
        data = self.model_dump(mode="json")
        signature = data.get("signature") or ""
        data["signature"] = f"{signature[:8]}..." if signature else ""
        return data


class MandateAuthorization(BaseModel):
    """Deterministic result of evaluating a basket against a mandate."""

    approved: bool
    mandate_id: str
    total_amount: int
    remaining_budget: int
    reason: str = "approved"
    violations: list[str] = Field(default_factory=list)


class MandateRequest(BaseModel):
    """Input for minting a mandate."""

    buyer_id: str = "buyer-demo"
    currency: str = "INR"
    maximum_amount: int = Field(ge=1)
    allowed_categories: list[str] = Field(default_factory=list)
    allowed_product_ids: list[str] = Field(default_factory=list)
    max_items: int = Field(default=10, ge=1)
    allow_upsell: bool = True
    intent: str = ""
    ttl_seconds: int | None = None
