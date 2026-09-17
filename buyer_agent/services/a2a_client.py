"""A2A client used by the buyer agent to talk to the merchant agent.

Transport is JSON-RPC 2.0 over HTTP. Tests pass an `httpx.ASGITransport` so the
two agents talk to each other in-process without sockets; production passes
nothing and real HTTP is used.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx

from app.core.exceptions import (
    AgentUnavailableError,
    ProtocolError,
    map_remote_error,
)
from app.core.logging import get_logger
from app.models.protocol import build_data_message

logger = get_logger(__name__)


class A2AClient:
    """Minimal, validated A2A caller."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport
        self._max_retries = max_retries
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        """One pooled client for the lifetime of the agent.

        Creating a client per request would also *close* the shared transport,
        which matters for the in-process transport used in tests and the
        single-process demo.
        """
        if self._http is None:
            with self._lock:
                if self._http is None:
                    headers = {"Content-Type": "application/json"}
                    if self._api_key:
                        headers["X-A2A-Key"] = self._api_key
                    self._http = httpx.Client(
                        base_url=self._base_url,
                        headers=headers,
                        timeout=self._timeout,
                        transport=self._transport,
                    )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    # --- discovery -------------------------------------------------------

    def fetch_agent_card(self) -> dict[str, Any]:
        """Read the merchant's `/.well-known/agent.json` capability document."""
        try:
            client = self._client()
            response = client.get("/.well-known/agent.json")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise AgentUnavailableError(
                "Could not fetch the merchant agent card",
                details={"error_type": type(exc).__name__, "url": self._base_url},
            ) from exc

    # --- messaging -------------------------------------------------------

    def send(
        self, skill: str, payload: dict[str, Any], *, transaction_id: str
    ) -> dict[str, Any]:
        """Invoke a merchant skill and return its `output` payload.

        Remote application errors are re-raised as the matching local exception
        type so the buyer's failure handling is identical whether the merchant
        is in-process or remote.
        """
        request = {
            "jsonrpc": "2.0",
            "id": f"rpc_{uuid.uuid4().hex[:12]}",
            "method": "message/send",
            "params": {
                "message": build_data_message(
                    f"msg_{uuid.uuid4().hex[:16]}", transaction_id, skill, payload
                )
            },
        }
        body = self._post(request, skill=skill, transaction_id=transaction_id)

        if "error" in body and body["error"]:
            error = body["error"]
            data = error.get("data") or {}
            logger.info(
                "a2a.remote_error",
                extra={
                    "skill": skill,
                    "transaction_id": transaction_id,
                    "error_code": data.get("code") or error.get("code"),
                },
            )
            raise map_remote_error(
                data.get("code", "protocol_error"),
                error.get("message", "Merchant agent returned an error"),
                data.get("details"),
            )

        result = body.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("Merchant response is missing a result object")
        for part in result.get("parts", []):
            if part.get("kind") == "data":
                output = part.get("data", {}).get("output")
                if isinstance(output, dict):
                    return output
        raise ProtocolError("Merchant response contained no data output")

    def _post(
        self, request: dict[str, Any], *, skill: str, transaction_id: str
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client().post("/a2a/message", json=request)
                if response.status_code == 401:
                    raise AgentUnavailableError(
                        "Merchant agent rejected the A2A credentials",
                        details={"status": 401},
                    )
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "server error", request=response.request, response=response
                    )
                return response.json()
            except AgentUnavailableError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "a2a.request_failed",
                    extra={
                        "skill": skill,
                        "transaction_id": transaction_id,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
        raise AgentUnavailableError(
            "Merchant agent is unreachable",
            details={"skill": skill, "error_type": type(last_error).__name__},
        )

    # --- convenience wrappers -------------------------------------------

    def search_catalog(
        self,
        query: str,
        *,
        transaction_id: str,
        max_price: int | None = None,
        category: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "limit": limit, "in_stock_only": True}
        if max_price is not None:
            payload["max_price"] = max_price
        if category:
            payload["category"] = category
        return self.send("catalog.search", payload, transaction_id=transaction_id)

    def quote(
        self,
        *,
        transaction_id: str,
        product_id: str,
        quantity: int,
        mandate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"product_id": product_id, "quantity": quantity}
        if mandate:
            payload["mandate"] = mandate
        return self.send("purchase.quote", payload, transaction_id=transaction_id)

    def create_order(
        self,
        *,
        transaction_id: str,
        mandate: dict[str, Any],
        items: list[dict[str, Any]],
        buyer_id: str,
        currency: str = "INR",
    ) -> dict[str, Any]:
        return self.send(
            "purchase.create_order",
            {
                "mandate": mandate,
                "items": items,
                "buyer_id": buyer_id,
                "currency": currency,
            },
            transaction_id=transaction_id,
        )

    def confirm(
        self, *, transaction_id: str, order_id: str, payment_id: str, signature: str
    ) -> dict[str, Any]:
        return self.send(
            "purchase.confirm",
            {"order_id": order_id, "payment_id": payment_id, "signature": signature},
            transaction_id=transaction_id,
        )

    def simulate_payment(self, order_id: str, *, status: str = "captured") -> dict[str, Any]:
        """Drive the merchant's offline checkout simulator (mock Razorpay only)."""
        try:
            response = self._client().post(
                "/payments/simulate", json={"order_id": order_id, "status": status}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise AgentUnavailableError(
                "Could not reach the merchant payment simulator",
                details={"error_type": type(exc).__name__},
            ) from exc
