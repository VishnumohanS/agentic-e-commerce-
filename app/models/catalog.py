"""Catalog, inventory and commerce models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.money import format_money


class Product(BaseModel):
    """A merchant catalog product. Prices are in minor units."""

    product_id: str
    name: str
    description: str = ""
    price: int = Field(ge=0)
    currency: str = "INR"
    category: str = ""
    brand: str = ""
    inventory: int = Field(default=0, ge=0)
    upsell_for: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def price_display(self) -> str:
        return format_money(self.price, self.currency)

    def searchable_text(self) -> str:
        parts = [self.name, self.brand, self.category, self.description, " ".join(self.tags)]
        return " ".join(p for p in parts if p).strip()

    def public_view(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["price_display"] = self.price_display
        return data


class ProductMatch(BaseModel):
    """A product plus its relevance score from semantic search."""

    product: Product
    score: float = 0.0
    match_reason: str = ""


class InventoryCheck(BaseModel):
    product_id: str
    requested_quantity: int
    available_quantity: int
    in_stock: bool
    reason: str = ""


class Reservation(BaseModel):
    reservation_id: str
    transaction_id: str
    items: dict[str, int]
    created_at: datetime
    committed: bool = False
    released: bool = False


class UpsellOffer(BaseModel):
    """A merchant-proposed add-on."""

    product_id: str
    name: str
    category: str = ""
    price: int = Field(ge=0)
    currency: str = "INR"
    quantity: int = 1
    pitch: str = ""

    @property
    def total(self) -> int:
        return self.price * self.quantity


class UpsellDecision(BaseModel):
    """Result of the buyer agent evaluating an upsell."""

    offer: UpsellOffer | None = None
    accepted: bool = False
    reason: str = "no_offer"
    base_total: int = 0
    projected_total: int = 0
    mandate_maximum: int = 0
    advisory: str = ""


class OrderRequest(BaseModel):
    transaction_id: str
    mandate: dict[str, Any]
    items: list[dict[str, Any]]
    buyer_id: str = "buyer-demo"
    currency: str = "INR"


class OrderResult(BaseModel):
    transaction_id: str
    order_id: str
    amount: int
    currency: str
    status: str
    reservation_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    razorpay_key_id: str | None = None
    created_at: datetime | None = None


class PaymentVerificationRequest(BaseModel):
    transaction_id: str
    order_id: str
    payment_id: str
    signature: str


class PurchaseConfirmation(BaseModel):
    transaction_id: str
    order_id: str
    payment_id: str
    status: str
    amount: int
    currency: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_at: datetime | None = None
    ledger_event_ids: list[str] = Field(default_factory=list)
