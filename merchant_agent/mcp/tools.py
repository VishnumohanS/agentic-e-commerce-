"""MCP tool surface for the merchant catalog.

The tool *definitions* and *implementations* live here, independent of any
transport. They are exposed two ways:

* `merchant_agent/api/routes.py` serves them over HTTP using MCP's JSON-RPC
  shapes (`tools/list`, `tools/call`), which is what the buyer agent uses.
* `merchant_agent/mcp/server.py` serves the same registry over stdio using the
  official `mcp` SDK for MCP-native clients such as Claude Desktop.

Read-only catalog capabilities only. Anything that spends money goes through
A2A with an AP2 mandate, never through an unauthenticated tool call.
"""

from __future__ import annotations

from typing import Any, Callable

from app.core.exceptions import AgenticCommerceError, ProtocolError
from app.core.logging import get_logger
from app.models.catalog import Product
from app.services.catalog_service import CatalogService
from app.services.inventory_service import InventoryService

logger = get_logger(__name__)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": (
            "Semantic search over the merchant catalog. Supports optional maximum "
            "price (minor units), category filter and in-stock-only filter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language product query"},
                "max_price": {
                    "type": "integer",
                    "description": "Maximum unit price in minor units (paise)",
                },
                "category": {"type": "string"},
                "in_stock_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "description": "Fetch one product by its catalog id.",
        "inputSchema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "check_inventory",
        "description": "Check whether a given quantity of a product is currently available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity": {"type": "integer", "default": 1, "minimum": 1},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "list_categories",
        "description": "List the catalog's product categories.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _product_payload(product: Product, available: int | None = None) -> dict[str, Any]:
    data = product.public_view()
    if available is not None:
        data["available_quantity"] = available
    return data


class MCPToolRegistry:
    """Executes MCP tool calls against the catalog and inventory services."""

    def __init__(self, catalog: CatalogService, inventory: InventoryService) -> None:
        self._catalog = catalog
        self._inventory = inventory
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "search_products": self._search_products,
            "get_product": self._get_product,
            "check_inventory": self._check_inventory,
            "list_categories": self._list_categories,
        }

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return [dict(definition) for definition in TOOL_DEFINITIONS]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch a tool call. Raises `ProtocolError` for unknown tools."""
        handler = self._handlers.get(name)
        if handler is None:
            raise ProtocolError(
                f"Unknown MCP tool '{name}'",
                details={"available": sorted(self._handlers)},
            )
        arguments = arguments or {}
        if not isinstance(arguments, dict):
            raise ProtocolError("MCP tool arguments must be an object")
        try:
            result = handler(arguments)
        except AgenticCommerceError:
            raise
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                f"Invalid arguments for MCP tool '{name}'", details={"error": str(exc)[:200]}
            ) from exc
        logger.debug("MCP tool executed", extra={"tool": name})
        return result

    # --- handlers --------------------------------------------------------

    def _search_products(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ProtocolError("search_products requires a non-empty 'query'")
        max_price = arguments.get("max_price")
        matches = self._catalog.search(
            query,
            max_price=int(max_price) if max_price not in (None, "") else None,
            category=arguments.get("category") or None,
            in_stock_only=bool(arguments.get("in_stock_only", True)),
            limit=int(arguments.get("limit", 5)),
        )
        return {
            "query": query,
            "count": len(matches),
            "results": [
                {
                    **_product_payload(
                        match.product, self._inventory.available(match.product.product_id)
                    ),
                    "score": match.score,
                    "match_reason": match.match_reason,
                }
                for match in matches
            ],
        }

    def _get_product(self, arguments: dict[str, Any]) -> dict[str, Any]:
        product_id = str(arguments.get("product_id", "")).strip()
        if not product_id:
            raise ProtocolError("get_product requires 'product_id'")
        product = self._catalog.get_product(product_id)
        return _product_payload(product, self._inventory.available(product_id))

    def _check_inventory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        product_id = str(arguments.get("product_id", "")).strip()
        if not product_id:
            raise ProtocolError("check_inventory requires 'product_id'")
        self._catalog.get_product(product_id)  # 404s for unknown products
        quantity = int(arguments.get("quantity", 1))
        return self._inventory.check(product_id, quantity).model_dump()

    def _list_categories(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"categories": self._catalog.categories()}
