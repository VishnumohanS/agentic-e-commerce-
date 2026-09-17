"""UCP (Universal Commerce Protocol) catalog representation.

The catalog is published as Schema.org JSON-LD so any agent can consume it
without bespoke parsing: `ItemList` of `Product` nodes, each carrying an
`Offer` with price, currency and availability. Prices are serialized in major
units because Schema.org `price` is defined that way, while the `acp:`
extension block keeps the exact minor-unit integer the payment layer uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.money import to_major
from app.models.catalog import Product

SCHEMA_CONTEXT = "https://schema.org"
ACP_NAMESPACE = "https://agentic-commerce.example/ns#"

IN_STOCK = "https://schema.org/InStock"
OUT_OF_STOCK = "https://schema.org/OutOfStock"


def product_to_jsonld(product: Product, *, available: int | None = None) -> dict[str, Any]:
    """Render one product as a Schema.org Product node."""
    stock = product.inventory if available is None else available
    return {
        "@type": "Product",
        "@id": f"urn:product:{product.product_id}",
        "sku": product.product_id,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "brand": {"@type": "Brand", "name": product.brand} if product.brand else None,
        "keywords": product.tags,
        "offers": {
            "@type": "Offer",
            "priceCurrency": product.currency,
            "price": f"{to_major(product.price):.2f}",
            "availability": IN_STOCK if stock > 0 else OUT_OF_STOCK,
            "inventoryLevel": {
                "@type": "QuantitativeValue",
                "value": stock,
            },
            "acp:priceMinorUnits": product.price,
        },
        "acp:metadata": product.metadata,
        "acp:upsellFor": product.upsell_for,
    }


def _prune(node: Any) -> Any:
    """Drop null values so the JSON-LD stays clean."""
    if isinstance(node, dict):
        return {k: _prune(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_prune(v) for v in node]
    return node


def build_ucp_catalog(
    merchant: dict[str, Any],
    products: list[Product],
    *,
    availability: dict[str, int] | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Build the full UCP catalog document served at `GET /catalog`."""
    availability = availability or {}
    items = [
        {
            "@type": "ListItem",
            "position": index + 1,
            "item": _prune(
                product_to_jsonld(product, available=availability.get(product.product_id))
            ),
        }
        for index, product in enumerate(products)
    ]
    return {
        "@context": {
            "@vocab": SCHEMA_CONTEXT,
            "acp": ACP_NAMESPACE,
        },
        "@type": "ItemList",
        "name": f"{merchant.get('name', 'Merchant')} catalog",
        "numberOfItems": len(items),
        "acp:protocolVersion": "ucp-1.0",
        "acp:generatedAt": datetime.now(UTC).isoformat(),
        "acp:merchant": {
            "@type": "Organization",
            "@id": f"urn:merchant:{merchant.get('merchant_id', 'unknown')}",
            "name": merchant.get("name", "Merchant"),
            "url": merchant.get("url"),
            "currenciesAccepted": merchant.get("currency", "INR"),
        },
        "acp:capabilities": capabilities
        or ["catalog.search", "inventory.check", "order.create", "order.confirm"],
        "itemListElement": items,
    }
