"""Merchant agent HTTP API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from app.core.exceptions import AgenticCommerceError, ProtocolError
from app.core.logging import get_logger
from app.models.protocol import (
    APPLICATION_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    A2AMessage,
)
from merchant_agent.agent.a2a_handler import build_agent_card
from merchant_agent.api.dependencies import (
    MerchantContainer,
    get_container,
    require_a2a_key,
)
from merchant_agent.ucp.catalog import build_ucp_catalog

logger = get_logger(__name__)

router = APIRouter()


# --- request/response models ---------------------------------------------


class InventoryCheckRequest(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1)


class QuoteRequest(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1)
    transaction_id: str | None = None
    mandate: dict[str, Any] | None = None


class CreateOrderRequest(BaseModel):
    transaction_id: str | None = None
    mandate: dict[str, Any]
    items: list[dict[str, Any]] = Field(min_length=1)
    buyer_id: str = "buyer-demo"
    currency: str = "INR"


class ConfirmPaymentRequest(BaseModel):
    transaction_id: str
    order_id: str
    payment_id: str
    signature: str


class SimulatePaymentRequest(BaseModel):
    order_id: str
    status: str = "captured"
    amount: int | None = None


# --- health & discovery ---------------------------------------------------


@router.get("/health", tags=["system"])
def health(container: MerchantContainer = Depends(get_container)) -> dict[str, Any]:
    return {
        "status": "ok",
        "agent": "merchant_agent",
        "version": "1.0.0",
        "environment": container.settings.environment,
        **container.health(),
    }


@router.get("/.well-known/agent.json", tags=["a2a"])
def agent_card(
    request: Request, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    base_url = str(request.base_url).rstrip("/")
    return build_agent_card(base_url, container.catalog.merchant)


# --- UCP ------------------------------------------------------------------


@router.get("/catalog", tags=["ucp"])
def ucp_catalog(container: MerchantContainer = Depends(get_container)) -> dict[str, Any]:
    """Agent-readable Schema.org JSON-LD catalog (UCP)."""
    products = container.catalog.list_products()
    availability = {
        product.product_id: container.inventory.available(product.product_id)
        for product in products
    }
    return build_ucp_catalog(
        container.catalog.merchant,
        products,
        availability=availability,
        capabilities=container.a2a.skill_ids,
    )


@router.get("/catalog/products", tags=["catalog"])
def list_products(
    category: str | None = None, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    products = container.catalog.list_products(category=category)
    return {
        "count": len(products),
        "products": [
            {
                **product.public_view(),
                "available_quantity": container.inventory.available(product.product_id),
            }
            for product in products
        ],
    }


@router.get("/catalog/products/{product_id}", tags=["catalog"])
def get_product(
    product_id: str, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    product = container.catalog.get_product(product_id)
    return {
        **product.public_view(),
        "available_quantity": container.inventory.available(product_id),
    }


# --- inventory ------------------------------------------------------------


@router.post("/inventory/check", tags=["inventory"])
def check_inventory(
    payload: InventoryCheckRequest, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    container.catalog.get_product(payload.product_id)
    return container.inventory.check(payload.product_id, payload.quantity).model_dump()


# --- MCP ------------------------------------------------------------------


@router.get("/mcp/tools", tags=["mcp"])
def mcp_tools(container: MerchantContainer = Depends(get_container)) -> dict[str, Any]:
    return {"tools": container.tools.definitions()}


@router.post("/mcp", tags=["mcp"])
async def mcp_jsonrpc(
    request: Request, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    """MCP over HTTP using JSON-RPC 2.0 (`tools/list`, `tools/call`)."""
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, INVALID_REQUEST, "Request body is not valid JSON")
    if not isinstance(body, dict):
        return _rpc_error(None, INVALID_REQUEST, "Request body must be a JSON object")

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, INVALID_PARAMS, "'params' must be an object")

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": container.tools.definitions()}}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _rpc_error(request_id, INVALID_PARAMS, "'name' is required")
        try:
            result = container.tools.call(name, params.get("arguments") or {})
        except AgenticCommerceError as exc:
            return _rpc_error(request_id, APPLICATION_ERROR, exc.message, exc.to_dict())
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": _json_dumps(result)}],
                "structuredContent": result,
                "isError": False,
            },
        }
    return _rpc_error(request_id, METHOD_NOT_FOUND, f"Unsupported MCP method '{method}'")


# --- A2A ------------------------------------------------------------------


@router.post("/a2a/message", tags=["a2a"], dependencies=[Depends(require_a2a_key)])
async def a2a_message(
    request: Request, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    """A2A JSON-RPC 2.0 endpoint (`message/send`)."""
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, INVALID_REQUEST, "Request body is not valid JSON")
    if not isinstance(body, dict):
        return _rpc_error(None, INVALID_REQUEST, "Request body must be a JSON object")

    request_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _rpc_error(request_id, INVALID_REQUEST, "Only JSON-RPC 2.0 is supported")
    if body.get("method") != "message/send":
        return _rpc_error(
            request_id, METHOD_NOT_FOUND, f"Unsupported A2A method '{body.get('method')}'"
        )

    params = body.get("params") or {}
    if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
        return _rpc_error(request_id, INVALID_PARAMS, "'params.message' is required")

    try:
        message = A2AMessage.model_validate(params["message"])
    except Exception as exc:
        return _rpc_error(
            request_id, INVALID_PARAMS, "Malformed A2A message", {"error": str(exc)[:300]}
        )

    try:
        result = container.a2a.handle(message)
    except AgenticCommerceError as exc:
        code = INVALID_PARAMS if isinstance(exc, ProtocolError) else APPLICATION_ERROR
        logger.info(
            "A2A skill rejected",
            extra={
                "transaction_id": message.context_id,
                "error_code": exc.code,
                "error_type": type(exc).__name__,
            },
        )
        return _rpc_error(request_id, code, exc.message, exc.to_dict())

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# --- REST commerce endpoints ---------------------------------------------


@router.post("/quote", tags=["commerce"], dependencies=[Depends(require_a2a_key)])
def quote(
    payload: QuoteRequest, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    transaction_id = payload.transaction_id or f"txn_{uuid.uuid4().hex[:16]}"
    return container.merchant.quote(
        transaction_id=transaction_id,
        product_id=payload.product_id,
        quantity=payload.quantity,
        mandate_raw=payload.mandate,
    )


@router.post("/order", tags=["commerce"], dependencies=[Depends(require_a2a_key)], status_code=201)
def create_order(
    payload: CreateOrderRequest, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    transaction_id = payload.transaction_id or f"txn_{uuid.uuid4().hex[:16]}"
    result = container.merchant.create_order(
        transaction_id=transaction_id,
        mandate_raw=payload.mandate,
        items=payload.items,
        buyer_id=payload.buyer_id,
        currency=payload.currency,
    )
    return result.model_dump(mode="json")


@router.post("/order/confirm", tags=["commerce"], dependencies=[Depends(require_a2a_key)])
def confirm_order(
    payload: ConfirmPaymentRequest, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    confirmation = container.merchant.confirm_payment(
        transaction_id=payload.transaction_id,
        order_id=payload.order_id,
        payment_id=payload.payment_id,
        signature=payload.signature,
    )
    return confirmation.model_dump(mode="json")


@router.post("/payments/simulate", tags=["commerce"], dependencies=[Depends(require_a2a_key)])
def simulate_payment(
    payload: SimulatePaymentRequest, container: MerchantContainer = Depends(get_container)
) -> dict[str, Any]:
    """Offline checkout simulator. Only available when `RAZORPAY_MODE=mock`."""
    return container.payments.simulate_payment(
        payload.order_id, status=payload.status, amount=payload.amount
    )


@router.post("/webhooks/razorpay", tags=["commerce"])
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    container: MerchantContainer = Depends(get_container),
) -> Response:
    """Razorpay webhook receiver; rejects anything without a valid signature."""
    body = await request.body()
    if not container.payments.verify_webhook_signature(body, x_razorpay_signature or ""):
        logger.warning("Rejected Razorpay webhook with invalid signature")
        return Response(
            content=_json_dumps({"error": "invalid_signature"}),
            status_code=401,
            media_type="application/json",
        )
    import json

    try:
        event = json.loads(body.decode("utf-8"))
    except Exception:
        return Response(
            content=_json_dumps({"error": "invalid_payload"}),
            status_code=400,
            media_type="application/json",
        )
    result = container.merchant.handle_webhook(event)
    return Response(content=_json_dumps(result), media_type="application/json")


# --- audit ----------------------------------------------------------------


@router.get("/ledger/verify", tags=["audit"])
def verify_ledger(container: MerchantContainer = Depends(get_container)) -> dict[str, Any]:
    return container.ledger.verify_integrity().model_dump()


@router.get("/ledger/events", tags=["audit"])
def ledger_events(
    transaction_id: str | None = None,
    limit: int = 100,
    container: MerchantContainer = Depends(get_container),
) -> dict[str, Any]:
    events = (
        container.ledger.read_transaction(transaction_id)
        if transaction_id
        else container.ledger.read_all()
    )
    limited = events[-max(1, min(limit, 500)) :]
    return {
        "count": len(limited),
        "total": len(events),
        "head_hash": container.ledger.head_hash(),
        "events": [event.model_dump() for event in limited],
    }


# --- helpers --------------------------------------------------------------


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, default=str, ensure_ascii=False)


def _rpc_error(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
