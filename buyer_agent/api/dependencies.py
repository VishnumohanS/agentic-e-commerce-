"""Dependency wiring for the buyer agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request

from app.core.config import Settings, get_settings
from app.services.ai_provider import AIProvider, get_ai_provider
from app.services.ap2 import MandateService
from app.services.ledger import AuditLedger, build_ledger
from buyer_agent.agent.buyer_core import BuyerAgent
from buyer_agent.services.a2a_client import A2AClient


@dataclass
class BuyerContainer:
    settings: Settings
    provider: AIProvider
    mandates: MandateService
    client: A2AClient
    ledger: AuditLedger
    buyer: BuyerAgent

    def health(self) -> dict[str, Any]:
        return {
            "ai": self.provider.health(),
            "payments": {"mode": self.settings.razorpay_mode},
            "merchant_agent_url": self.settings.merchant_agent_url,
            "ledger": {
                "backend": self.settings.ledger_backend,
                "events": self.ledger.backend.count(),
            },
        }


def build_container(
    settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None
) -> BuyerContainer:
    settings = settings or get_settings()
    provider = get_ai_provider(settings)
    mandates = MandateService(settings=settings)
    client = A2AClient(
        settings.merchant_agent_url,
        api_key=settings.a2a_api_key,
        timeout=settings.http_timeout_seconds,
        transport=transport,
    )
    ledger = build_ledger(
        settings, actor="buyer_agent", sqlite_path=settings.buyer_sqlite_path, chain_id="BUYER"
    )
    buyer = BuyerAgent(
        provider=provider, mandates=mandates, client=client, ledger=ledger
    )
    return BuyerContainer(
        settings=settings,
        provider=provider,
        mandates=mandates,
        client=client,
        ledger=ledger,
        buyer=buyer,
    )


def get_container(request: Request) -> BuyerContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Buyer agent is not initialised")
    return container
