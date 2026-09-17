"""Shared FastAPI plumbing: correlation IDs, access logging, error mapping."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AgenticCommerceError
from app.core.logging import get_logger, set_transaction_id

logger = get_logger(__name__)

TRANSACTION_HEADER = "X-Transaction-Id"


def install_middleware(app: FastAPI, *, agent: str) -> None:
    """Attach a correlation id to every request and emit a structured access log."""

    @app.middleware("http")
    async def _correlate(request: Request, call_next: Callable) -> Any:
        transaction_id = request.headers.get(TRANSACTION_HEADER) or f"req_{uuid.uuid4().hex[:16]}"
        set_transaction_id(transaction_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "Unhandled error during request",
                extra={
                    "agent": agent,
                    "path": request.url.path,
                    "method": request.method,
                    "duration_ms": duration_ms,
                    "status": 500,
                },
            )
            raise
        finally:
            set_transaction_id(None)
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers[TRANSACTION_HEADER] = transaction_id
        if request.url.path not in {"/health", "/favicon.ico"}:
            logger.info(
                "request.completed",
                extra={
                    "agent": agent,
                    "path": request.url.path,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "transaction_id": transaction_id,
                },
            )
        return response


def install_exception_handlers(app: FastAPI, *, agent: str) -> None:
    """Map domain errors onto stable JSON error responses."""

    @app.exception_handler(AgenticCommerceError)
    async def _domain_error(request: Request, exc: AgenticCommerceError) -> JSONResponse:
        logger.warning(
            "domain.error",
            extra={
                "agent": agent,
                "path": request.url.path,
                "error_code": exc.code,
                "error_type": type(exc).__name__,
                "status": exc.http_status,
            },
        )
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request payload failed validation",
                    "details": {"errors": _safe_errors(exc)},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                    "details": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled.error",
            extra={"agent": agent, "path": request.url.path, "error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred",
                    "details": {},
                }
            },
        )


def _safe_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    errors = []
    for error in exc.errors()[:10]:
        errors.append(
            {
                "location": ".".join(str(part) for part in error.get("loc", [])),
                "message": str(error.get("msg", ""))[:200],
                "type": error.get("type"),
            }
        )
    return errors
