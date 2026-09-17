"""Buyer agent application entrypoint.

    uvicorn buyer_agent.main:app --port 8001

Also serves the demo UI at `/` when the `static/` directory is present. In AWS
the UI is better served from S3 + CloudFront; this local mount keeps the
one-command demo working.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import Settings, get_settings
from app.core.http import install_exception_handlers, install_middleware
from app.core.logging import configure_logging, get_logger
from buyer_agent.api.dependencies import BuyerContainer, build_container
from buyer_agent.api.routes import router

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(
    settings: Settings | None = None, container: BuyerContainer | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings, force=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "Buyer agent starting",
            extra={
                "environment": settings.environment,
                "ai_provider": settings.ai_provider,
                "merchant_agent_url": settings.merchant_agent_url,
            },
        )
        yield
        logger.info("Buyer agent stopped")

    app = FastAPI(
        title="Buyer Agent",
        description=(
            "Autonomous buyer agent: interprets a natural-language request, mints a "
            "bounded AP2 mandate and transacts with the merchant agent over A2A."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.container = container or build_container(settings)
    install_middleware(app, agent="buyer_agent")
    install_exception_handlers(app, agent="buyer_agent")
    app.include_router(router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    active = get_settings()
    uvicorn.run(
        "buyer_agent.main:app",
        host="0.0.0.0",
        port=active.buyer_agent_port,
        reload=not active.is_production,
    )
