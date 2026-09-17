"""Merchant catalog: loading, semantic search and upsell selection."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from app.core.exceptions import ConfigurationError, ProductNotFoundError
from app.core.logging import get_logger
from app.models.catalog import Product, ProductMatch, UpsellOffer
from app.services.embedding_service import EmbeddingService

logger = get_logger(__name__)


class CatalogService:
    """In-process product catalog backed by a JSON document.

    In production this loads from DynamoDB/RDS instead; the interface used by
    the merchant agent (`list_products`, `get_product`, `search`) stays the same.
    """

    def __init__(
        self,
        products: Iterable[Product],
        *,
        embedding_service: EmbeddingService | None = None,
        merchant: dict[str, Any] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._products: dict[str, Product] = {p.product_id: p for p in products}
        self._embeddings = embedding_service
        self._merchant = merchant or {
            "merchant_id": "mrc_demo",
            "name": "Demo Merchant",
            "currency": "INR",
        }

    # --- construction ----------------------------------------------------

    @classmethod
    def from_file(
        cls, path: str, *, embedding_service: EmbeddingService | None = None
    ) -> "CatalogService":
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigurationError(f"Catalog file not found: {path}")
        try:
            raw = json.loads(file_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Catalog file is not valid JSON: {path}") from exc
        products = [Product.model_validate(item) for item in raw.get("products", [])]
        if not products:
            raise ConfigurationError("Catalog contains no products")
        logger.info("Catalog loaded", extra={"product_count": len(products), "source": path})
        return cls(products, embedding_service=embedding_service, merchant=raw.get("merchant"))

    # --- reads -----------------------------------------------------------

    @property
    def merchant(self) -> dict[str, Any]:
        return dict(self._merchant)

    def list_products(self, *, category: str | None = None) -> list[Product]:
        with self._lock:
            products = list(self._products.values())
        if category:
            products = [p for p in products if p.category.lower() == category.lower()]
        return sorted(products, key=lambda p: p.product_id)

    def get_product(self, product_id: str) -> Product:
        with self._lock:
            product = self._products.get(product_id)
        if product is None:
            raise ProductNotFoundError(
                f"Product '{product_id}' is not in the catalog",
                details={"product_id": product_id},
            )
        return product

    def categories(self) -> list[str]:
        return sorted({p.category for p in self.list_products() if p.category})

    # --- search ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_price: int | None = None,
        category: str | None = None,
        in_stock_only: bool = False,
        limit: int = 5,
    ) -> list[ProductMatch]:
        """Filter deterministically first, then rank semantically.

        Price and stock filters are hard constraints applied in Python, so the
        model can never surface a product the buyer is not allowed to consider.
        """
        candidates = self.list_products(category=category)
        if max_price is not None:
            candidates = [p for p in candidates if p.price <= max_price]
        if in_stock_only:
            candidates = [p for p in candidates if p.inventory > 0]
        if not candidates:
            return []

        if self._embeddings is None or not query.strip():
            matches = [
                ProductMatch(product=p, score=0.0, match_reason="catalog_order")
                for p in candidates
            ]
            return matches[:limit]

        documents = {p.product_id: p.searchable_text() for p in candidates}
        ranked = self._embeddings.rank(query, documents, top_k=limit)
        by_id = {p.product_id: p for p in candidates}
        return [
            ProductMatch(
                product=by_id[product_id],
                score=round(score, 4),
                match_reason="semantic_similarity",
            )
            for product_id, score in ranked
            if product_id in by_id
        ]

    # --- upsell ----------------------------------------------------------

    def upsell_candidates(self, product_id: str) -> list[Product]:
        """Accessories explicitly linked to a product, in stock, cheapest first."""
        return sorted(
            (
                p
                for p in self.list_products()
                if product_id in p.upsell_for and p.inventory > 0
            ),
            key=lambda p: p.price,
        )

    def build_upsell_offer(
        self, product_id: str, *, headroom: int | None = None
    ) -> UpsellOffer | None:
        """Pick the best add-on that fits the buyer's remaining budget.

        Passing `headroom` lets the merchant be a good citizen and only propose
        affordable add-ons; the buyer still re-checks independently.
        """
        for candidate in self.upsell_candidates(product_id):
            if headroom is not None and candidate.price > headroom:
                continue
            return UpsellOffer(
                product_id=candidate.product_id,
                name=candidate.name,
                category=candidate.category,
                price=candidate.price,
                currency=candidate.currency,
                quantity=1,
                pitch=f"Customers who bought this also added the {candidate.name}.",
            )
        return None

    # --- mutation (inventory lives in InventoryService) -------------------

    def adjust_inventory(self, product_id: str, delta: int) -> Product:
        with self._lock:
            product = self.get_product(product_id)
            new_value = product.inventory + delta
            if new_value < 0:
                new_value = 0
            updated = product.model_copy(update={"inventory": new_value})
            self._products[product_id] = updated
            return updated

    def snapshot(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant,
            "products": [p.public_view() for p in self.list_products()],
        }
