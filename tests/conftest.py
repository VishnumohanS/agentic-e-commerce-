"""Shared pytest fixtures.

Everything runs offline: `AI_PROVIDER=mock`, `RAZORPAY_MODE=mock`, SQLite
ledgers in a per-test temporary directory. No AWS calls, no Razorpay calls, no
network and no spend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from app.core.config import Settings
from app.core.inprocess import InProcessASGITransport
from app.models.mandate import MandateRequest
from app.services.ap2 import MandateService
from app.services.catalog_service import CatalogService
from app.services.embedding_service import EmbeddingService, InMemoryEmbeddingCache
from app.services.inventory_service import InventoryService
from app.services.ledger import AuditLedger, SQLiteLedgerBackend
from app.services.local_provider import LocalAIProvider
from app.services.razorpay_service import RazorpayService
from buyer_agent.agent.buyer_core import BuyerAgent
from buyer_agent.api.dependencies import build_container as build_buyer_container
from buyer_agent.services.a2a_client import A2AClient
from merchant_agent.agent.merchant_core import MerchantAgent
from merchant_agent.api.dependencies import build_container as build_merchant_container
from merchant_agent.mcp.tools import MCPToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"

TEST_A2A_KEY = "test-a2a-key"
TEST_MANDATE_SECRET = "test-mandate-secret-value"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ENVIRONMENT="test",
        LOG_LEVEL="WARNING",
        AI_PROVIDER="mock",
        RAZORPAY_MODE="mock",
        RAZORPAY_WEBHOOK_SECRET="test-webhook-secret",
        LEDGER_BACKEND="sqlite",
        DATABASE_URL=f"sqlite:///{tmp_path}/merchant.db",
        BUYER_DATABASE_URL=f"sqlite:///{tmp_path}/buyer.db",
        EMBEDDING_CACHE_PATH=str(tmp_path / "embeddings.json"),
        CATALOG_PATH=str(CATALOG_PATH),
        AP2_MANDATE_SECRET=TEST_MANDATE_SECRET,
        A2A_API_KEY=TEST_A2A_KEY,
    )


@pytest.fixture
def provider(settings: Settings) -> LocalAIProvider:
    return LocalAIProvider(settings=settings)


@pytest.fixture
def ledger(tmp_path: Path) -> AuditLedger:
    return AuditLedger(SQLiteLedgerBackend(str(tmp_path / "ledger.db")), actor="test")


@pytest.fixture
def mandates(settings: Settings) -> MandateService:
    return MandateService(settings=settings)


@pytest.fixture
def catalog(settings: Settings, provider: LocalAIProvider) -> CatalogService:
    embeddings = EmbeddingService(provider, InMemoryEmbeddingCache())
    return CatalogService.from_file(settings.catalog_path, embedding_service=embeddings)


@pytest.fixture
def inventory(catalog: CatalogService) -> InventoryService:
    return InventoryService(catalog)


@pytest.fixture
def payments(settings: Settings) -> RazorpayService:
    return RazorpayService(settings=settings)


@pytest.fixture
def tools(catalog: CatalogService, inventory: InventoryService) -> MCPToolRegistry:
    return MCPToolRegistry(catalog, inventory)


@pytest.fixture
def merchant(
    catalog: CatalogService,
    inventory: InventoryService,
    mandates: MandateService,
    payments: RazorpayService,
    ledger: AuditLedger,
) -> MerchantAgent:
    return MerchantAgent(
        catalog=catalog,
        inventory=inventory,
        mandates=mandates,
        payments=payments,
        ledger=ledger,
    )


@pytest.fixture
def mandate_factory(mandates: MandateService):
    def _make(amount: int = 200000, **kwargs):
        payload = {"buyer_id": "buyer-demo", "currency": "INR", "maximum_amount": amount}
        payload.update(kwargs)
        return mandates.create_mandate(MandateRequest(**payload))

    return _make


# --- wired agents -------------------------------------------------------


@pytest.fixture
def merchant_container(settings: Settings):
    return build_merchant_container(settings)


@pytest.fixture
def merchant_app(settings: Settings, merchant_container):
    from merchant_agent.main import create_app

    return create_app(settings, merchant_container)


@pytest.fixture
def merchant_client(merchant_app) -> Iterator:
    from fastapi.testclient import TestClient

    with TestClient(merchant_app) as client:
        client.headers.update({"X-A2A-Key": TEST_A2A_KEY})
        yield client


@pytest.fixture
def transport(merchant_app) -> Iterator[InProcessASGITransport]:
    transport = InProcessASGITransport(merchant_app)
    yield transport
    transport.shutdown()


@pytest.fixture
def a2a_client(settings: Settings, transport: InProcessASGITransport) -> A2AClient:
    return A2AClient(
        "http://merchant.test", api_key=settings.a2a_api_key, transport=transport
    )


@pytest.fixture
def buyer_container(settings: Settings, transport: InProcessASGITransport):
    return build_buyer_container(settings, transport=transport)


@pytest.fixture
def buyer(buyer_container) -> BuyerAgent:
    return buyer_container.buyer
