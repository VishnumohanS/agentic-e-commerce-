"""Audit ledger tests: hash chaining and tamper detection."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.models.ledger import GENESIS_HASH, EventType, LedgerEvent
from app.services.ledger import (
    AuditLedger,
    SQLiteLedgerBackend,
    build_ledger,
    compute_hash,
)


@pytest.fixture
def populated(ledger: AuditLedger) -> AuditLedger:
    for index in range(5):
        ledger.append(
            EventType.ORDER_CREATED,
            transaction_id=f"txn_{index % 2}",
            payload={"index": index, "amount": 1000 * index},
        )
    return ledger


class TestChainConstruction:
    def test_first_event_links_to_genesis(self, ledger: AuditLedger):
        event = ledger.append(EventType.PURCHASE_REQUESTED, transaction_id="txn_1")
        assert event.sequence == 1
        assert event.previous_hash == GENESIS_HASH

    def test_events_link_to_their_predecessor(self, ledger: AuditLedger):
        first = ledger.append(EventType.PURCHASE_REQUESTED, transaction_id="txn_1")
        second = ledger.append(EventType.ORDER_CREATED, transaction_id="txn_1")
        assert second.previous_hash == first.current_hash
        assert second.sequence == first.sequence + 1

    def test_hash_matches_the_documented_formula(self, ledger: AuditLedger):
        event = ledger.append(EventType.ORDER_CREATED, transaction_id="txn_1", payload={"a": 1})
        assert event.current_hash == compute_hash(event.previous_hash, event.chain_core())

    def test_identical_payloads_get_different_hashes(self, ledger: AuditLedger):
        first = ledger.append(EventType.ORDER_CREATED, transaction_id="t", payload={"a": 1})
        second = ledger.append(EventType.ORDER_CREATED, transaction_id="t", payload={"a": 1})
        assert first.current_hash != second.current_hash

    def test_events_are_readable_by_transaction(self, populated: AuditLedger):
        events = populated.read_transaction("txn_0")
        assert len(events) == 3
        assert {e.transaction_id for e in events} == {"txn_0"}

    def test_head_hash_tracks_the_latest_event(self, populated: AuditLedger):
        assert populated.head_hash() == populated.read_all()[-1].current_hash

    def test_secrets_in_payloads_are_redacted(self, ledger: AuditLedger):
        event = ledger.append(
            EventType.ORDER_CREATED,
            transaction_id="txn_1",
            payload={"signature": "abc123", "api_key": "secret", "amount": 100},
        )
        assert event.payload["signature"] == "***REDACTED***"
        assert event.payload["api_key"] == "***REDACTED***"
        assert event.payload["amount"] == 100


class TestIntegrityVerification:
    def test_untouched_ledger_verifies(self, populated: AuditLedger):
        result = populated.verify_integrity()
        assert result.valid
        assert result.events_checked == 5

    def test_empty_ledger_verifies(self, ledger: AuditLedger):
        assert ledger.verify_integrity().valid

    def test_modified_event_payload_is_detected(self, populated: AuditLedger, tmp_path):
        _sql(tmp_path, "UPDATE ledger_events SET payload = ? WHERE sequence = 3", ('{"x":1}',))
        result = populated.verify_integrity()
        assert not result.valid
        assert result.broken_at_sequence == 3
        assert "hash_mismatch" in result.reason

    def test_modified_event_type_is_detected(self, populated: AuditLedger, tmp_path):
        _sql(tmp_path, "UPDATE ledger_events SET event_type = ? WHERE sequence = 2", ("forged",))
        assert not populated.verify_integrity().valid

    def test_modified_actor_is_detected(self, populated: AuditLedger, tmp_path):
        _sql(tmp_path, "UPDATE ledger_events SET actor = ? WHERE sequence = 4", ("attacker",))
        assert not populated.verify_integrity().valid

    def test_modified_previous_hash_is_detected(self, populated: AuditLedger, tmp_path):
        _sql(tmp_path, "UPDATE ledger_events SET previous_hash = ? WHERE sequence = 4", ("0" * 64,))
        result = populated.verify_integrity()
        assert not result.valid
        assert result.broken_at_sequence == 4

    def test_deleted_event_breaks_the_chain(self, populated: AuditLedger, tmp_path):
        _sql(tmp_path, "DELETE FROM ledger_events WHERE sequence = 3", ())
        result = populated.verify_integrity()
        assert not result.valid
        assert "sequence_gap" in result.reason

    def test_reordered_events_are_detected(self, populated: AuditLedger, tmp_path):
        path = str(tmp_path / "ledger.db")
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT sequence, payload, event_id FROM ledger_events WHERE sequence IN (2, 3)"
        ).fetchall()
        by_seq = {row[0]: row for row in rows}
        conn.execute(
            "UPDATE ledger_events SET payload = ?, event_id = ? WHERE sequence = 2",
            (by_seq[3][1], by_seq[3][2]),
        )
        conn.execute(
            "UPDATE ledger_events SET payload = ?, event_id = ? WHERE sequence = 3",
            (by_seq[2][1], by_seq[2][2]),
        )
        conn.commit()
        conn.close()
        result = populated.verify_integrity()
        assert not result.valid
        assert result.broken_at_sequence == 2

    def test_appended_forged_event_is_detected(self, populated: AuditLedger, tmp_path):
        path = str(tmp_path / "ledger.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """INSERT INTO ledger_events (chain_id, sequence, event_id, timestamp, event_type,
               transaction_id, actor, payload, previous_hash, current_hash)
               VALUES ('GLOBAL', 6, 'evt_forged', '2026-01-01T00:00:00Z', 'transaction.confirmed',
               'txn_forged', 'attacker', '{}', ?, ?)""",
            ("0" * 64, "f" * 64),
        )
        conn.commit()
        conn.close()
        result = populated.verify_integrity()
        assert not result.valid
        assert result.broken_at_sequence == 6

    def test_recomputing_the_chain_after_tampering_still_fails_downstream(
        self, populated: AuditLedger, tmp_path
    ):
        """Fixing one event's own hash does not repair the links after it."""
        path = str(tmp_path / "ledger.db")
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT sequence, event_id, timestamp, event_type, transaction_id, actor,"
            " previous_hash FROM ledger_events WHERE sequence = 2"
        ).fetchone()
        forged_payload = {"amount": 999999}
        core = {
            "sequence": row[0],
            "event_id": row[1],
            "timestamp": row[2],
            "event_type": row[3],
            "transaction_id": row[4],
            "actor": row[5],
            "payload": forged_payload,
        }
        conn.execute(
            "UPDATE ledger_events SET payload = ?, current_hash = ? WHERE sequence = 2",
            (json.dumps(forged_payload, sort_keys=True, separators=(",", ":")),
             compute_hash(row[6], core)),
        )
        conn.commit()
        conn.close()
        result = populated.verify_integrity()
        assert not result.valid
        assert result.broken_at_sequence == 3


class TestBackends:
    def test_sqlite_backend_rejects_duplicate_sequences(self, tmp_path):
        backend = SQLiteLedgerBackend(str(tmp_path / "dup.db"))
        event = LedgerEvent(
            sequence=1,
            event_id="evt_1",
            timestamp="2026-01-01T00:00:00Z",
            event_type="test",
            transaction_id="txn_1",
            actor="test",
            payload={},
            previous_hash=GENESIS_HASH,
            current_hash="a" * 64,
        )
        backend.append(event)
        with pytest.raises(Exception):
            backend.append(event)

    def test_separate_chains_do_not_interfere(self, tmp_path):
        path = str(tmp_path / "chains.db")
        merchant = AuditLedger(SQLiteLedgerBackend(path, chain_id="MERCHANT"))
        buyer = AuditLedger(SQLiteLedgerBackend(path, chain_id="BUYER"))
        merchant.append("a", transaction_id="t1")
        buyer.append("b", transaction_id="t1")
        assert merchant.verify_integrity().valid
        assert buyer.verify_integrity().valid
        assert len(merchant.read_all()) == 1

    def test_build_ledger_uses_sqlite_by_default(self, settings):
        ledger = build_ledger(settings, actor="test")
        assert isinstance(ledger.backend, SQLiteLedgerBackend)


def _sql(tmp_path, statement: str, params: tuple) -> None:
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.execute(statement, params)
    conn.commit()
    conn.close()
