"""AP2 bounded spending mandates.

Security model
--------------
* A mandate is a JSON document signed with HMAC-SHA256 over a canonical
  serialization of every field except the signature.
* The merchant agent verifies the signature independently before honouring it.
* **No LLM output is ever trusted for spending enforcement.** `authorize()` is
  pure, deterministic Python: it compares integers and set-memberships only.
* Nonces are single-use; a `NonceStore` rejects replays.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    BudgetExceededError,
    MandateCurrencyError,
    MandateExpiredError,
    MandateMalformedError,
    MandateReplayError,
    MandateScopeError,
    MandateSignatureError,
)
from app.core.logging import get_logger
from app.models.mandate import (
    LineItem,
    MandateAuthorization,
    MandateRequest,
    SpendingMandate,
)

logger = get_logger(__name__)


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable serialization: sorted keys, no whitespace, UTF-8 safe."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class NonceStore(Protocol):
    """Replay protection for mandate nonces."""

    def consume(self, nonce: str, mandate_id: str) -> None: ...

    def seen(self, nonce: str) -> bool: ...


class InMemoryNonceStore:
    """Process-local nonce store (default for tests and single-process dev)."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str, mandate_id: str) -> None:
        with self._lock:
            if nonce in self._seen:
                raise MandateReplayError(
                    "Mandate nonce has already been used",
                    details={"nonce_prefix": nonce[:8], "mandate_id": mandate_id},
                )
            self._seen[nonce] = mandate_id

    def seen(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._seen


class SQLiteNonceStore:
    """Durable nonce store so replays are rejected across restarts."""

    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mandate_nonces (
                    nonce TEXT PRIMARY KEY,
                    mandate_id TEXT NOT NULL,
                    used_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def consume(self, nonce: str, mandate_id: str) -> None:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO mandate_nonces (nonce, mandate_id, used_at) VALUES (?, ?, ?)",
                    (nonce, mandate_id, datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise MandateReplayError(
                    "Mandate nonce has already been used",
                    details={"nonce_prefix": nonce[:8], "mandate_id": mandate_id},
                ) from exc

    def seen(self, nonce: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM mandate_nonces WHERE nonce = ?", (nonce,)
            ).fetchone()
        return row is not None


class MandateService:
    """Mint, verify and enforce AP2 spending mandates."""

    def __init__(
        self,
        secret: str | None = None,
        *,
        settings: Settings | None = None,
        nonce_store: NonceStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._secret = (secret or self._settings.ap2_mandate_secret).encode("utf-8")
        self._nonce_store: NonceStore = nonce_store or InMemoryNonceStore()
        self._default_ttl = self._settings.ap2_mandate_ttl_seconds
        self._absolute_max = self._settings.ap2_absolute_max_amount

    # --- signing ---------------------------------------------------------

    def sign_payload(self, payload: dict[str, Any]) -> str:
        message = canonical_json(payload).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def create_mandate(self, request: MandateRequest) -> SpendingMandate:
        """Mint a signed mandate, capped by the configured absolute ceiling."""
        if request.maximum_amount <= 0:
            raise MandateMalformedError("Mandate maximum_amount must be positive")
        capped = min(request.maximum_amount, self._absolute_max)
        now = datetime.now(UTC)
        ttl = request.ttl_seconds or self._default_ttl
        mandate = SpendingMandate(
            mandate_id=f"mnd_{uuid.uuid4().hex[:16]}",
            buyer_id=request.buyer_id,
            currency=request.currency.upper(),
            maximum_amount=capped,
            allowed_categories=[c.lower().strip() for c in request.allowed_categories if c],
            allowed_product_ids=[p.strip() for p in request.allowed_product_ids if p],
            max_items=request.max_items,
            allow_upsell=request.allow_upsell,
            intent=request.intent[:500],
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
            nonce=secrets.token_hex(16),
        )
        mandate.signature = self.sign_payload(mandate.signing_payload())
        logger.info(
            "AP2 mandate created",
            extra={
                "mandate_id": mandate.mandate_id,
                "maximum_amount": mandate.maximum_amount,
                "currency": mandate.currency,
                "expires_at": mandate.expires_at.isoformat(),
                "capped": capped != request.maximum_amount,
            },
        )
        return mandate

    # --- verification ----------------------------------------------------

    @staticmethod
    def parse(raw: dict[str, Any] | SpendingMandate) -> SpendingMandate:
        if isinstance(raw, SpendingMandate):
            return raw
        try:
            return SpendingMandate.model_validate(raw)
        except Exception as exc:
            raise MandateMalformedError(
                "Mandate document is malformed", details={"error": str(exc)[:200]}
            ) from exc

    def verify_signature(self, mandate: SpendingMandate) -> bool:
        if not mandate.signature:
            return False
        expected = self.sign_payload(mandate.signing_payload())
        return hmac.compare_digest(expected, mandate.signature)

    def validate(
        self,
        raw: dict[str, Any] | SpendingMandate,
        *,
        now: datetime | None = None,
        consume_nonce: bool = False,
    ) -> SpendingMandate:
        """Validate structure, signature and expiry. Raises on any failure."""
        mandate = self.parse(raw)
        if not self.verify_signature(mandate):
            raise MandateSignatureError(
                "Mandate signature verification failed",
                details={"mandate_id": mandate.mandate_id},
            )
        current = now or datetime.now(UTC)
        expires_at = _as_aware(mandate.expires_at)
        created_at = _as_aware(mandate.created_at)
        if expires_at <= current:
            raise MandateExpiredError(
                "Mandate has expired",
                details={
                    "mandate_id": mandate.mandate_id,
                    "expires_at": expires_at.isoformat(),
                },
            )
        if created_at > current + timedelta(minutes=5):
            raise MandateMalformedError(
                "Mandate created_at is in the future",
                details={"mandate_id": mandate.mandate_id},
            )
        if mandate.maximum_amount > self._absolute_max:
            raise BudgetExceededError(
                "Mandate exceeds the platform spending ceiling",
                details={
                    "mandate_id": mandate.mandate_id,
                    "maximum_amount": mandate.maximum_amount,
                    "ceiling": self._absolute_max,
                },
            )
        if consume_nonce:
            self._nonce_store.consume(mandate.nonce, mandate.mandate_id)
        return mandate

    # --- deterministic enforcement --------------------------------------

    def authorize(
        self,
        mandate: SpendingMandate,
        items: Iterable[LineItem],
        *,
        currency: str = "INR",
    ) -> MandateAuthorization:
        """Decide whether a basket is within mandate. Pure integer arithmetic.

        This function never calls an LLM and never consults external state, so
        its verdict is fully reproducible from the mandate plus the basket.
        """
        line_items = list(items)
        violations: list[str] = []
        total = sum(item.total for item in line_items)

        if currency.upper() != mandate.currency.upper():
            violations.append(
                f"currency_mismatch:{currency.upper()}!={mandate.currency.upper()}"
            )

        quantity = sum(item.quantity for item in line_items)
        if quantity > mandate.max_items:
            violations.append(f"item_count_exceeded:{quantity}>{mandate.max_items}")

        for item in line_items:
            if mandate.allowed_product_ids and item.product_id not in mandate.allowed_product_ids:
                violations.append(f"product_out_of_scope:{item.product_id}")
            if mandate.allowed_categories and item.category.lower() not in mandate.allowed_categories:
                violations.append(f"category_out_of_scope:{item.category or 'unknown'}")
            if item.kind == "upsell" and not mandate.allow_upsell:
                violations.append(f"upsell_not_permitted:{item.product_id}")

        if total > mandate.maximum_amount:
            violations.append(f"budget_exceeded:{total}>{mandate.maximum_amount}")

        approved = not violations
        return MandateAuthorization(
            approved=approved,
            mandate_id=mandate.mandate_id,
            total_amount=total,
            remaining_budget=max(mandate.maximum_amount - total, 0),
            reason="approved" if approved else violations[0].split(":")[0],
            violations=violations,
        )

    def enforce(
        self,
        mandate: SpendingMandate,
        items: Iterable[LineItem],
        *,
        currency: str = "INR",
    ) -> MandateAuthorization:
        """Like `authorize`, but raises the most specific error on rejection."""
        authorization = self.authorize(mandate, items, currency=currency)
        if authorization.approved:
            return authorization
        first = authorization.violations[0]
        details = {
            "mandate_id": mandate.mandate_id,
            "total_amount": authorization.total_amount,
            "maximum_amount": mandate.maximum_amount,
            "violations": authorization.violations,
        }
        if first.startswith("currency_mismatch"):
            raise MandateCurrencyError("Currency does not match the mandate", details=details)
        if first.startswith("budget_exceeded"):
            raise BudgetExceededError(
                "Purchase total exceeds the mandate maximum", details=details
            )
        raise MandateScopeError("Purchase is outside the mandate scope", details=details)

    # --- helpers ---------------------------------------------------------

    @property
    def nonce_store(self) -> NonceStore:
        return self._nonce_store


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
