"""Tamper-evident, hash-chained audit ledger.

Chain rule
----------
    current_hash = SHA256(previous_hash + canonical_json(event_core))

`event_core` covers sequence, event_id, timestamp, event_type, transaction_id,
actor and payload. Any edit, deletion or reordering of stored rows breaks
`verify_integrity()`.

Backends
--------
* `SQLiteLedgerBackend`  - local development and tests.
* `DynamoDBLedgerBackend`- production. Partition key `chain_id`, sort key
  `sequence`, with a conditional write so two writers can never claim the same
  sequence number. A `transaction_id-index` GSI supports per-transaction reads.

QLDB is deliberately not used: the application-level hash chain provides the
tamper evidence the project requires, and DynamoDB is cheaper and simpler for a
prototype. The chain logic is storage-independent, so a QLDB backend could be
added later without touching callers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import LedgerError
from app.core.logging import get_logger, redact_mapping
from app.models.ledger import (
    GENESIS_HASH,
    LedgerEvent,
    LedgerVerificationResult,
)

logger = get_logger(__name__)

DEFAULT_CHAIN_ID = "GLOBAL"


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_hash(previous_hash: str, event_core: dict[str, Any]) -> str:
    """SHA-256 over previous hash concatenated with the canonical event body."""
    material = f"{previous_hash}{canonical_json(event_core)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class LedgerBackend(ABC):
    """Storage contract for the chain."""

    @abstractmethod
    def append(self, event: LedgerEvent) -> None: ...

    @abstractmethod
    def read_all(self) -> list[LedgerEvent]: ...

    @abstractmethod
    def read_by_transaction(self, transaction_id: str) -> list[LedgerEvent]: ...

    @abstractmethod
    def head(self) -> LedgerEvent | None: ...

    @abstractmethod
    def count(self) -> int: ...


class SQLiteLedgerBackend(LedgerBackend):
    def __init__(self, path: str, chain_id: str = DEFAULT_CHAIN_ID) -> None:
        self._path = path
        self._chain_id = chain_id
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            self._shared = sqlite3.connect(":memory:", check_same_thread=False)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = sqlite3.connect(self._path, timeout=15, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_events (
                    chain_id       TEXT NOT NULL,
                    sequence       INTEGER NOT NULL,
                    event_id       TEXT NOT NULL,
                    timestamp      TEXT NOT NULL,
                    event_type     TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    actor          TEXT NOT NULL,
                    payload        TEXT NOT NULL,
                    previous_hash  TEXT NOT NULL,
                    current_hash   TEXT NOT NULL,
                    PRIMARY KEY (chain_id, sequence)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_tx ON ledger_events (transaction_id)"
            )
            conn.commit()
        finally:
            self._maybe_close(conn)

    def _maybe_close(self, conn: sqlite3.Connection) -> None:
        if self._shared is None:
            conn.close()

    def append(self, event: LedgerEvent) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO ledger_events
                    (chain_id, sequence, event_id, timestamp, event_type,
                     transaction_id, actor, payload, previous_hash, current_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._chain_id,
                    event.sequence,
                    event.event_id,
                    event.timestamp,
                    event.event_type,
                    event.transaction_id,
                    event.actor,
                    canonical_json(event.payload),
                    event.previous_hash,
                    event.current_hash,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise LedgerError(
                "Ledger sequence collision - concurrent write detected",
                details={"sequence": event.sequence},
            ) from exc
        finally:
            self._maybe_close(conn)

    def _rows_to_events(self, rows: list[tuple[Any, ...]]) -> list[LedgerEvent]:
        events: list[LedgerEvent] = []
        for row in rows:
            events.append(
                LedgerEvent(
                    sequence=row[0],
                    event_id=row[1],
                    timestamp=row[2],
                    event_type=row[3],
                    transaction_id=row[4],
                    actor=row[5],
                    payload=json.loads(row[6]),
                    previous_hash=row[7],
                    current_hash=row[8],
                )
            )
        return events

    _SELECT = (
        "SELECT sequence, event_id, timestamp, event_type, transaction_id, actor, "
        "payload, previous_hash, current_hash FROM ledger_events"
    )

    def read_all(self) -> list[LedgerEvent]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"{self._SELECT} WHERE chain_id = ? ORDER BY sequence ASC", (self._chain_id,)
            ).fetchall()
        finally:
            self._maybe_close(conn)
        return self._rows_to_events(rows)

    def read_by_transaction(self, transaction_id: str) -> list[LedgerEvent]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"{self._SELECT} WHERE chain_id = ? AND transaction_id = ? ORDER BY sequence ASC",
                (self._chain_id, transaction_id),
            ).fetchall()
        finally:
            self._maybe_close(conn)
        return self._rows_to_events(rows)

    def head(self) -> LedgerEvent | None:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"{self._SELECT} WHERE chain_id = ? ORDER BY sequence DESC LIMIT 1",
                (self._chain_id,),
            ).fetchall()
        finally:
            self._maybe_close(conn)
        events = self._rows_to_events(rows)
        return events[0] if events else None

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM ledger_events WHERE chain_id = ?", (self._chain_id,)
            ).fetchone()
        finally:
            self._maybe_close(conn)
        return int(row[0]) if row else 0


class DynamoDBLedgerBackend(LedgerBackend):  # pragma: no cover - requires AWS
    """Production backend. See `infrastructure/cloudformation.yaml` for the table."""

    def __init__(
        self,
        table_name: str,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        chain_id: str = DEFAULT_CHAIN_ID,
        resource: Any | None = None,
    ) -> None:
        self._chain_id = chain_id
        if resource is None:
            import boto3

            resource = boto3.resource(
                "dynamodb", region_name=region_name, endpoint_url=endpoint_url
            )
        self._table = resource.Table(table_name)

    def append(self, event: LedgerEvent) -> None:
        from botocore.exceptions import ClientError

        item = {
            "chain_id": self._chain_id,
            "sequence": event.sequence,
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "event_type": event.event_type,
            "transaction_id": event.transaction_id,
            "actor": event.actor,
            "payload": canonical_json(event.payload),
            "previous_hash": event.previous_hash,
            "current_hash": event.current_hash,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(chain_id) AND attribute_not_exists(#s)",
                ExpressionAttributeNames={"#s": "sequence"},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise LedgerError(
                    "Ledger sequence collision - concurrent write detected",
                    details={"sequence": event.sequence},
                ) from exc
            raise LedgerError("DynamoDB ledger write failed", details={"aws_code": code}) from exc

    @staticmethod
    def _item_to_event(item: dict[str, Any]) -> LedgerEvent:
        return LedgerEvent(
            sequence=int(item["sequence"]),
            event_id=item["event_id"],
            timestamp=item["timestamp"],
            event_type=item["event_type"],
            transaction_id=item["transaction_id"],
            actor=item["actor"],
            payload=json.loads(item["payload"]),
            previous_hash=item["previous_hash"],
            current_hash=item["current_hash"],
        )

    def read_all(self) -> list[LedgerEvent]:
        from boto3.dynamodb.conditions import Key

        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("chain_id").eq(self._chain_id),
            "ScanIndexForward": True,
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return [self._item_to_event(item) for item in items]

    def read_by_transaction(self, transaction_id: str) -> list[LedgerEvent]:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            IndexName="transaction_id-index",
            KeyConditionExpression=Key("transaction_id").eq(transaction_id),
        )
        events = [self._item_to_event(item) for item in response.get("Items", [])]
        return sorted(events, key=lambda e: e.sequence)

    def head(self) -> LedgerEvent | None:
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=Key("chain_id").eq(self._chain_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return self._item_to_event(items[0]) if items else None

    def count(self) -> int:
        return len(self.read_all())


class AuditLedger:
    """Append-only hash chain over a pluggable backend."""

    def __init__(self, backend: LedgerBackend, *, actor: str = "system") -> None:
        self._backend = backend
        self._actor = actor
        self._lock = threading.Lock()

    @property
    def backend(self) -> LedgerBackend:
        return self._backend

    def append(
        self,
        event_type: str,
        *,
        transaction_id: str,
        payload: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> LedgerEvent:
        """Append an event, linking it to the current chain head."""
        with self._lock:
            head = self._backend.head()
            sequence = (head.sequence + 1) if head else 1
            previous_hash = head.current_hash if head else GENESIS_HASH
            core = {
                "sequence": sequence,
                "event_id": f"evt_{uuid.uuid4().hex[:20]}",
                "timestamp": datetime.now(UTC).isoformat(),
                "event_type": event_type,
                "transaction_id": transaction_id,
                "actor": actor or self._actor,
                "payload": redact_mapping(payload or {}),
            }
            event = LedgerEvent(
                **core,
                previous_hash=previous_hash,
                current_hash=compute_hash(previous_hash, core),
            )
            self._backend.append(event)
        logger.debug(
            "Ledger event appended",
            extra={
                "event_type": event_type,
                "transaction_id": transaction_id,
                "sequence": event.sequence,
            },
        )
        return event

    def read_all(self) -> list[LedgerEvent]:
        return self._backend.read_all()

    def read_transaction(self, transaction_id: str) -> list[LedgerEvent]:
        return self._backend.read_by_transaction(transaction_id)

    def head_hash(self) -> str:
        head = self._backend.head()
        return head.current_hash if head else GENESIS_HASH

    def verify_integrity(self) -> LedgerVerificationResult:
        """Recompute the whole chain and report the first inconsistency.

        Detects: edited payload/metadata, edited previous_hash, deleted events
        (sequence gap), and reordered events (hash mismatch at the swap point).
        """
        events = self._backend.read_all()
        previous_hash = GENESIS_HASH
        expected_sequence = 1
        for event in events:
            if event.sequence != expected_sequence:
                return LedgerVerificationResult(
                    valid=False,
                    events_checked=expected_sequence - 1,
                    broken_at_sequence=event.sequence,
                    reason=(
                        f"sequence_gap: expected {expected_sequence}, found {event.sequence} "
                        "(an event was deleted or inserted)"
                    ),
                    head_hash=previous_hash,
                )
            if event.previous_hash != previous_hash:
                return LedgerVerificationResult(
                    valid=False,
                    events_checked=expected_sequence - 1,
                    broken_at_sequence=event.sequence,
                    reason="previous_hash_mismatch: chain link broken",
                    head_hash=previous_hash,
                )
            recomputed = compute_hash(event.previous_hash, event.chain_core())
            if recomputed != event.current_hash:
                return LedgerVerificationResult(
                    valid=False,
                    events_checked=expected_sequence - 1,
                    broken_at_sequence=event.sequence,
                    reason="hash_mismatch: event content was modified after being written",
                    head_hash=previous_hash,
                )
            previous_hash = event.current_hash
            expected_sequence += 1
        return LedgerVerificationResult(
            valid=True,
            events_checked=len(events),
            broken_at_sequence=None,
            reason=None,
            head_hash=previous_hash,
        )


def build_ledger(
    settings: Settings | None = None,
    *,
    actor: str = "system",
    sqlite_path: str | None = None,
    chain_id: str = DEFAULT_CHAIN_ID,
) -> AuditLedger:
    """Create a ledger for the configured backend (sqlite locally, DynamoDB in AWS)."""
    settings = settings or get_settings()
    if settings.ledger_backend == "dynamodb":
        backend: LedgerBackend = DynamoDBLedgerBackend(
            settings.dynamodb_table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
            chain_id=chain_id,
        )
    else:
        backend = SQLiteLedgerBackend(sqlite_path or settings.sqlite_path, chain_id=chain_id)
    return AuditLedger(backend, actor=actor)
