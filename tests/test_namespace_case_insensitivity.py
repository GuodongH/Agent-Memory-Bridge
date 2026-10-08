from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_mem_bridge.repository_snapshot_store import RepositorySnapshotStore
from agent_mem_bridge.schema import exact_content_hash
from agent_mem_bridge.storage import MemoryStore


def test_store_deduplication_canonicalizes_namespace(tmp_path: Path) -> None:
    home = tmp_path / "amb-home"
    home.mkdir()
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")

    first = store.store(
        namespace="project:Moebius",
        content="Important trading rule for Moebius",
        title="Moebius rule",
        kind="memory",
    )
    assert first["id"] is not None
    assert first["duplicate_of"] is None

    second = store.store(
        namespace="project:moebius",
        content="Important trading rule for Moebius",
        title="Moebius rule duplicate",
        kind="memory",
    )
    assert second["duplicate_of"] == first["id"]

    recalled = store.recall(
        namespace="project:moebius",
        query="trading rule",
        kind="memory",
    )
    assert recalled["count"] == 1
    assert recalled["items"][0]["id"] == first["id"]


def test_feedback_with_mixed_case_namespaces(tmp_path: Path) -> None:
    home = tmp_path / "amb-home"
    home.mkdir()
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")

    stored = store.store(
        namespace="project:Moebius",
        content="Alpha generation pattern",
        title="Alpha pattern",
        kind="memory",
    )
    memory_id = stored["id"]

    recalled = store.recall(
        namespace="project:moebius",
        query="Alpha generation",
        kind="memory",
    )
    receipt = recalled["recall_receipt"]["token"]

    result = store.feedback(
        namespace="project:moebius",
        recall_receipt=receipt,
        memory_id=memory_id,
        result_rank=1,
        outcome="helpful",
    )
    assert result["stored"] is True
    assert result["duplicate"] is False

    # A retry of feedback under upper case namespace is identified as a duplicate of the same vote
    result_upper = store.feedback(
        namespace="project:MOEBIUS",
        recall_receipt=receipt,
        memory_id=memory_id,
        result_rank=1,
        outcome="helpful",
    )
    assert result_upper["duplicate"] is True
    assert result_upper["feedback_id"] == result["feedback_id"]


def test_signals_poll_and_claim_canonicalize_namespace(tmp_path: Path) -> None:
    home = tmp_path / "amb-home"
    home.mkdir()
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")

    stored = store.store(
        namespace="project:Moebius",
        content="Rebalance portfolio signal",
        title="Rebalance signal",
        kind="signal",
    )
    signal_id = stored["id"]

    polled = store.recall(
        namespace="project:moebius",
        kind="signal",
        signal_status="pending",
    )
    assert polled["count"] == 1
    assert polled["items"][0]["id"] == signal_id

    claim_result = store.claim_signal(
        namespace="project:moebius",
        consumer="agent-trader",
        lease_seconds=60,
        signal_id=signal_id,
    )
    assert claim_result["claimed"] is True
    assert claim_result["item"]["id"] == signal_id

    ack_result = store.ack_signal(
        memory_id=signal_id,
        consumer="agent-trader",
    )
    assert ack_result["acked"] is True


def test_database_migration_normalizes_existing_mixed_case_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        from agent_mem_bridge.schema import init_db

        init_db(conn)
        content = "Legacy memory with mixed case namespace"
        conn.execute(
            """
            INSERT INTO memories (
                id, namespace, kind, title, content, content_hash, exact_content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-1",
                "project:LegacyMoebius",
                "memory",
                "Legacy Title",
                content,
                "dummy_hash",
                exact_content_hash(content),
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    store = MemoryStore(db_path, log_dir=tmp_path / "logs")

    recalled = store.recall(
        namespace="project:legacymoebius",
        query="Legacy memory",
        kind="memory",
    )
    assert recalled["count"] == 1
    assert recalled["items"][0]["id"] == "legacy-1"
    assert recalled["items"][0]["namespace"] == "project:legacymoebius"


def test_repository_binding_collision_across_case_variations(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "repository"
    store = RepositorySnapshotStore(snapshot_root)
    store.bind_namespace("project:Moebius", "repo-111")

    # Attempting to bind project:moebius to a different repo must fail without allow_rebind
    with pytest.raises(ValueError, match="already bound to a different repository"):
        store.bind_namespace("project:moebius", "repo-222")

    # Rebinding with allow_rebind succeeds and updates the canonical lowercase entry
    rebound = store.bind_namespace("project:moebius", "repo-222", allow_rebind=True)
    assert rebound["rebound"] is True
    assert store.bindings()["bindings"]["project:moebius"]["repository_id"] == "repo-222"

    # Unbinding with mixed case removes the canonical lowercase binding
    assert store.unbind_namespace("project:MOEBIUS") is True
    assert "project:moebius" not in store.bindings()["bindings"]

