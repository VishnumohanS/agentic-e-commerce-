"""A2A server side: agent card + skill dispatch.

The merchant advertises its skills at `/.well-known/agent.json` and accepts
JSON-RPC 2.0 `message/send` calls at `/a2a/message`. Each message carries a
`data` part naming a skill and its input. Unknown skills, malformed envelopes
and invalid inputs are rejected with proper JSON-RPC error codes and recorded
in the audit ledger.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from app.core.exceptions import ProtocolError
from app.core.logging import get_logger
from app.models.ledger import EventType
from app.models.protocol import A2AMessage
from merchant_agent.agent.merchant_core import MerchantAgent
from merchant_agent.mcp.tools import MCPToolRegistry

logger = get_logger(__name__)

AGENT_ACTOR = "merchant_agent"

SKILLS: list[dict[str, Any]] = [
    {
        "id": "catalog.search",
        "name": "Search catalog",
        "description": "Semantic product search with price, category and stock filters.",
        "tags": ["catalog", "mcp"],
    },
    {
        "id": "catalog.get_product",
        "name": "Get product",
        "description": "Fetch a single product by id.",
        "tags": ["catalog", "mcp"],
    },
    {
        "id": "inventory.check",
        "name": "Check inventory",
        "description": "Check live availability for a product and quantity.",
        "tags": ["inventory", "mcp"],
    },
    {
        "id": "purchase.quote",
        "name": "Quote purchase",
        "description": "Price a basket and optionally receive an upsell proposal.",
        "tags": ["commerce"],
    },
    {
        "id": "purchase.create_order",
        "name": "Create order",
        "description": "Validate the AP2 mandate, reserve stock and create a Razorpay test order.",
        "tags": ["commerce", "ap2", "payments"],
    },
    {
        "id": "purchase.confirm",
        "name": "Confirm payment",
        "description": "Verify a Razorpay payment signature and status, then confirm the sale.",
        "tags": ["commerce", "payments"],
    },
]


def build_agent_card(base_url: str, merchant: dict[str, Any]) -> dict[str, Any]:
    """The A2A agent card describing this merchant agent."""
    return {
        "protocolVersion": "0.2.0",
        "name": merchant.get("name", "Merchant Agent"),
        "description": (
            "Agent-readable storefront exposing a UCP catalog over MCP and "
            "transacting over A2A under AP2 spending mandates."
        ),
        "url": f"{base_url.rstrip('/')}/a2a/message",
        "version": "1.0.0",
        "provider": {
            "organization": merchant.get("name", "Merchant"),
            "url": merchant.get("url", base_url),
        },
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "securitySchemes": {
            "apiKey": {"type": "apiKey", "in": "header", "name": "X-A2A-Key"}
        },
        "security": [{"apiKey": []}],
        "skills": SKILLS,
        "additionalInterfaces": [
            {"transport": "MCP", "url": f"{base_url.rstrip('/')}/mcp"},
            {"transport": "UCP", "url": f"{base_url.rstrip('/')}/catalog"},
        ],
    }


class A2ASkillHandler:
    """Validates A2A envelopes and routes skills to the merchant agent."""

    def __init__(self, merchant: MerchantAgent, tools: MCPToolRegistry) -> None:
        self._merchant = merchant
        self._tools = tools
        self._skills: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
            "catalog.search": self._catalog_search,
            "catalog.get_product": self._catalog_get_product,
            "inventory.check": self._inventory_check,
            "purchase.quote": self._purchase_quote,
            "purchase.create_order": self._purchase_create_order,
            "purchase.confirm": self._purchase_confirm,
        }

    @property
    def skill_ids(self) -> list[str]:
        return sorted(self._skills)

    def handle(self, message: A2AMessage) -> dict[str, Any]:
        """Execute one A2A message and return the response payload."""
        try:
            data = message.data_payload()
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc

        skill = str(data.get("skill", "")).strip()
        payload = data.get("input") or {}
        if not isinstance(payload, dict):
            raise ProtocolError("A2A 'input' must be an object")

        transaction_id = message.context_id or f"txn_{uuid.uuid4().hex[:16]}"
        handler = self._skills.get(skill)
        if handler is None:
            self._merchant.ledger.append(
                EventType.A2A_ERROR,
                transaction_id=transaction_id,
                payload={"skill": skill or "<missing>", "reason": "unknown_skill"},
                actor=AGENT_ACTOR,
            )
            raise ProtocolError(
                f"Unknown A2A skill '{skill}'", details={"available": self.skill_ids}
            )

        self._merchant.ledger.append(
            EventType.A2A_REQUEST,
            transaction_id=transaction_id,
            payload={"skill": skill, "message_id": message.message_id, "role": message.role},
            actor=AGENT_ACTOR,
        )
        result = handler(payload, transaction_id)
        self._merchant.ledger.append(
            EventType.A2A_RESPONSE,
            transaction_id=transaction_id,
            payload={"skill": skill, "status": "ok"},
            actor=AGENT_ACTOR,
        )
        return {
            "messageId": f"msg_{uuid.uuid4().hex[:16]}",
            "role": "agent",
            "contextId": transaction_id,
            "parts": [{"kind": "data", "data": {"skill": skill, "output": result}}],
        }

    # --- skills ----------------------------------------------------------

    def _catalog_search(self, payload: dict[str, Any], transaction_id: str) -> dict[str, Any]:
        self._merchant.ledger.append(
            EventType.MCP_TOOL_CALLED,
            transaction_id=transaction_id,
            payload={"tool": "search_products"},
            actor=AGENT_ACTOR,
        )
        return self._tools.call("search_products", payload)

    def _catalog_get_product(self, payload: dict[str, Any], transaction_id: str) -> dict[str, Any]:
        return self._tools.call("get_product", payload)

    def _inventory_check(self, payload: dict[str, Any], transaction_id: str) -> dict[str, Any]:
        return self._tools.call("check_inventory", payload)

    def _purchase_quote(self, payload: dict[str, Any], transaction_id: str) -> dict[str, Any]:
        return self._merchant.quote(
            transaction_id=transaction_id,
            product_id=str(payload.get("product_id", "")),
            quantity=int(payload.get("quantity", 1) or 1),
            mandate_raw=payload.get("mandate"),
        )

    def _purchase_create_order(
        self, payload: dict[str, Any], transaction_id: str
    ) -> dict[str, Any]:
        mandate = payload.get("mandate")
        if not isinstance(mandate, dict):
            raise ProtocolError("purchase.create_order requires a 'mandate' object")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ProtocolError("purchase.create_order requires a non-empty 'items' array")
        result = self._merchant.create_order(
            transaction_id=transaction_id,
            mandate_raw=mandate,
            items=items,
            buyer_id=str(payload.get("buyer_id", mandate.get("buyer_id", "buyer-demo"))),
            currency=str(payload.get("currency", "INR")),
        )
        return result.model_dump(mode="json")

    def _purchase_confirm(self, payload: dict[str, Any], transaction_id: str) -> dict[str, Any]:
        for field in ("order_id", "payment_id", "signature"):
            if not str(payload.get(field, "")).strip():
                raise ProtocolError(f"purchase.confirm requires '{field}'")
        confirmation = self._merchant.confirm_payment(
            transaction_id=transaction_id,
            order_id=str(payload["order_id"]),
            payment_id=str(payload["payment_id"]),
            signature=str(payload["signature"]),
        )
        return confirmation.model_dump(mode="json")
