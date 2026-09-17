"""Buyer agent HTTP API."""

from __future__ import annotations

import json
from typing import Any, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.core.money import to_minor
from buyer_agent.agent.buyer_core import PurchaseOutcome, PurchaseStep
from buyer_agent.api.dependencies import BuyerContainer, get_container

logger = get_logger(__name__)

router = APIRouter()


class PurchaseRequest(BaseModel):
    """A natural-language purchase request with an optional hard budget."""

    request: str = Field(min_length=1, max_length=1000)
    budget: float | None = Field(
        default=None, gt=0, description="Optional budget ceiling in rupees (major units)"
    )
    auto_pay: bool = True
    buyer_id: str = "buyer-demo"


def _budget_minor(payload: PurchaseRequest) -> int | None:
    if payload.budget is None:
        return None
    return to_minor(payload.budget)


@router.get("/health", tags=["system"])
def health(container: BuyerContainer = Depends(get_container)) -> dict[str, Any]:
    return {
        "status": "ok",
        "agent": "buyer_agent",
        "version": "1.0.0",
        "environment": container.settings.environment,
        **container.health(),
    }


@router.get("/merchant/card", tags=["a2a"])
def merchant_card(container: BuyerContainer = Depends(get_container)) -> dict[str, Any]:
    """Proxy the merchant's A2A agent card (useful for the UI and debugging)."""
    return container.client.fetch_agent_card()


@router.post("/purchase", tags=["commerce"])
def purchase(
    payload: PurchaseRequest, container: BuyerContainer = Depends(get_container)
) -> dict[str, Any]:
    """Run a complete autonomous purchase and return the full decision trail."""
    outcome = container.buyer.purchase(
        payload.request,
        budget=_budget_minor(payload),
        auto_pay=payload.auto_pay,
        buyer_id=payload.buyer_id,
    )
    return outcome.to_dict()


@router.post("/purchase/stream", tags=["commerce"])
def purchase_stream(
    payload: PurchaseRequest, container: BuyerContainer = Depends(get_container)
) -> StreamingResponse:
    """Server-sent events: one `step` per decision, then a final `outcome`."""

    def event_source() -> Iterator[str]:
        try:
            for event in container.buyer.purchase_stream(
                payload.request,
                budget=_budget_minor(payload),
                auto_pay=payload.auto_pay,
                buyer_id=payload.buyer_id,
            ):
                if isinstance(event, PurchaseStep):
                    yield _sse("step", event.to_dict())
                elif isinstance(event, PurchaseOutcome):
                    yield _sse("outcome", event.to_dict())
        except Exception:  # pragma: no cover - the agent handles its own errors
            logger.exception("purchase.stream_failed")
            yield _sse(
                "outcome",
                {
                    "status": "failed",
                    "error": {"code": "internal_error", "message": "Stream failed"},
                    "steps": [],
                },
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/ledger/verify", tags=["audit"])
def verify_ledger(container: BuyerContainer = Depends(get_container)) -> dict[str, Any]:
    return container.ledger.verify_integrity().model_dump()


@router.get("/ledger/events", tags=["audit"])
def ledger_events(
    transaction_id: str | None = None,
    limit: int = 100,
    container: BuyerContainer = Depends(get_container),
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


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"
