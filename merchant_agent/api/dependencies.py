"""Dependency wiring for the merchant agent.

A single `MerchantContainer` builds every service once and hands them to the
routes. Tests construct their own container with in-memory stores, which is why
nothing here reaches for module-level globals beyond the app state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException, Request

from app.core.config import Settings, get_settings
from app.services.ai_provider import AIProvider, get_ai_provider
from app.services.ap2 import MandateService, SQLiteNonceStore
from app.services.catalog_service import CatalogService
from app.services.embedding_service import EmbeddingService, JsonFileEmbeddingCache
from app.services.inventory_service import InventoryService
from app.services.ledger import AuditLedger, build_ledger
from app.services.razorpay_service import RazorpayService
from merchant_agent.agent.a2a_handler import A2ASkillHandler
from merchant_agent.agent.merchant_core import MerchantAgent
from merchant_agent.mcp.tools import MCPToolRegistry


@dataclass
class MerchantContainer:
    settings: Settings
    provider: AIProvider
    embeddings: EmbeddingService
    catalog: CatalogService
    inventory: InventoryService
    mandates: MandateService
    payments: RazorpayService
    ledger: AuditLedger
    merchant: MerchantAgent
    tools: MCPToolRegistry
    a2a: A2ASkillHandler

    def health(self) -> dict[str, Any]:
        return {
            "ai": self.provider.health(),
            "payments": self.payments.health(),
            "ledger": {
                "backend": self.settings.ledger_backend,
                "events": self.ledger.backend.count(),
            },
            "catalog": {"products": len(self.catalog.list_products())},
        }


def build_container(settings: Settings | None = None) -> MerchantContainer:
    settings = settings or get_settings()
    provider = get_ai_provider(settings)
    embeddings = EmbeddingService(
        provider, JsonFileEmbeddingCache(settings.embedding_cache_path)
    )
    catalog = CatalogService.from_file(settings.catalog_path, embedding_service=embeddings)
    inventory = InventoryService(catalog)
    nonce_store = (
        SQLiteNonceStore(settings.sqlite_path) if settings.ledger_backend == "sqlite" else None
    )
    mandates = MandateService(settings=settings, nonce_store=nonce_store)
    payments = RazorpayService(settings=settings)
    ledger = build_ledger(settings, actor="merchant_agent")
    merchant = MerchantAgent(
        catalog=catalog,
        inventory=inventory,
        mandates=mandates,
        payments=payments,
        ledger=ledger,
    )
    tools = MCPToolRegistry(catalog, inventory)
    return MerchantContainer(
        settings=settings,
        provider=provider,
        embeddings=embeddings,
        catalog=catalog,
        inventory=inventory,
        mandates=mandates,
        payments=payments,
        ledger=ledger,
        merchant=merchant,
        tools=tools,
        a2a=A2ASkillHandler(merchant, tools),
    )


def get_container(request: Request) -> MerchantContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Merchant agent is not initialised")
    return container


def require_a2a_key(
    request: Request, x_a2a_key: str | None = Header(default=None, alias="X-A2A-Key")
) -> None:
    """Shared-secret authentication for agent-to-agent calls.

    Local development uses the default key from `.env`; in AWS the value comes
    from Secrets Manager and API Gateway can enforce it at the edge as well.
    """
    import hmac

    container = get_container(request)
    expected = container.settings.a2a_api_key
    if not expected:
        return
    if not x_a2a_key or not hmac.compare_digest(x_a2a_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-A2A-Key")
