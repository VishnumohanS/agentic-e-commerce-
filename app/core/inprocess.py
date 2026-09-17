"""Synchronous in-process transport for agent-to-agent calls.

`httpx.ASGITransport` is async-only, but the buyer agent's A2A client is
synchronous. This transport bridges the two by running the merchant's ASGI app
on a dedicated background event loop, so the buyer can call the merchant
in-process - no sockets, no ports, no flaky test servers - using the exact same
client code path as a real deployment.

Used by the test suite, the smoke script and the single-process demo mode.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx


class InProcessASGITransport(httpx.BaseTransport):
    """Sync `httpx` transport that dispatches into an ASGI application."""

    def __init__(self, app: Any, *, root_path: str = "") -> None:
        self._transport = httpx.ASGITransport(app=app, root_path=root_path)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="inprocess-asgi", daemon=True
        )
        self._thread.start()
        self._closed = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _dispatch() -> httpx.Response:
            response = await self._transport.handle_async_request(request)
            content = await response.aread()
            await response.aclose()
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=content,
                request=request,
            )

        future = asyncio.run_coroutine_threadsafe(_dispatch(), self._loop)
        return future.result(timeout=60)

    def close(self) -> None:
        """No-op.

        `httpx.Client.close()` closes its transport, but this transport is
        shared across many short-lived clients, so tearing down the event loop
        here would break every later request. Call `shutdown()` explicitly.
        """

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
