"""Merchant agent application entrypoint.

    uvicorn merchant_agent.main:app --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.http import install_exception_handlers, install_middleware
from app.core.logging import configure_logging, get_logger
from merchant_agent.api.dependencies import MerchantContainer, build_container
from merchant_agent.api.routes import router

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None, container: MerchantContainer | None = None
) -> FastAPI:
    """Build the merchant FastAPI app. Tests inject their own container."""
    settings = settings or get_settings()
    configure_logging(settings, force=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "Merchant agent starting",
            extra={
                "environment": settings.environment,
                "ai_provider": settings.ai_provider,
                "ledger_backend": settings.ledger_backend,
                "razorpay_mode": settings.razorpay_mode,
            },
        )
        yield
        logger.info("Merchant agent stopped")

    app = FastAPI(
        title="Merchant Agent",
        description=(
            "Agent-readable storefront: UCP catalog, MCP tools, A2A transactions "
            "under AP2 mandates, Razorpay test payments and a hash-chained audit ledger."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.container = container or build_container(settings)
    install_middleware(app, agent="merchant_agent")
    install_exception_handlers(app, agent="merchant_agent")
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    active = get_settings()
    uvicorn.run(
        "merchant_agent.main:app",
        host="0.0.0.0",
        port=active.merchant_agent_port,
        reload=not active.is_production,
    )
