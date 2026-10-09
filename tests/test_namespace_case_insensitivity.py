from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_mem_bridge.repository_snapshot_store import BINDING_STORE_SCHEMA, RepositorySnapshotStore
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


def test_migration_collapses_multiple_mixed_case_duplicates_into_survivor(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_dups.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        from agent_mem_bridge.schema import init_db

        init_db(conn)
        content = "Identical content in Moebius and MOEBIUS"
        h = exact_content_hash(content)

        # Older record: project:Moebius (survivor)
        conn.execute(
            """
            INSERT INTO memories (id, namespace, kind, title, content, content_hash, exact_content_hash, created_at)
            VALUES ('m-orig', 'project:Moebius', 'memory', 'Original Title', ?, 'hash1', ?, '2026-01-01T00:00:00Z')
            """,
            (content, h),
        )
        conn.execute("INSERT INTO memory_metadata (memory_id, metadata_schema_version) VALUES ('m-orig', 1)")
        conn.execute(
            "INSERT INTO memories_fts (memory_id, title, content) VALUES ('m-orig', 'Original Title', ?)",
            (content,),
        )

        # Newer record: project:MOEBIUS (duplicate to be merged)
        conn.execute(
            """
            INSERT INTO memories (id, namespace, kind, title, content, content_hash, exact_content_hash, created_at)
            VALUES ('m-dup', 'project:MOEBIUS', 'memory', 'Duplicate Title', ?, 'hash1', ?, '2026-01-02T00:00:00Z')
            """,
            (content, h),
        )
        conn.execute("INSERT INTO memory_metadata (memory_id, metadata_schema_version) VALUES ('m-dup', 1)")
        conn.execute(
            "INSERT INTO memories_fts (memory_id, title, content) VALUES ('m-dup', 'Duplicate Title', ?)",
            (content,),
        )

        # Outgoing edge on duplicate
        conn.execute(
            """
            INSERT INTO memory_edges (source_id, target_id, relation, position, machine_owned, target_namespace, target_exists)
            VALUES ('m-dup', 'target-node', 'supersedes', 0, 0, 'project:Moebius', 1)
            """
        )

        # Incoming edge pointing to duplicate from another node
        conn.execute(
            """
            INSERT INTO memories (id, namespace, kind, title, content, content_hash, exact_content_hash, created_at)
            VALUES ('m-other', 'project:other', 'memory', 'Other', 'Other content', 'h2', 'h2', '2026-01-03T00:00:00Z')
            """
        )
        conn.execute("INSERT INTO memory_metadata (memory_id, metadata_schema_version) VALUES ('m-other', 1)")
        conn.execute(
            """
            INSERT INTO memory_edges (source_id, target_id, relation, position, machine_owned, target_namespace, target_exists)
            VALUES ('m-other', 'm-dup', 'depends_on', 0, 0, 'project:MOEBIUS', 1)
            """
        )

        # Annotation on duplicate
        conn.execute(
            """
            INSERT INTO memory_annotations (memory_id, title_before, title_after, added_tags_json, provenance_json, created_at)
            VALUES ('m-dup', 'Title A', 'Title B', '[]', '{}', '2026-01-02T00:00:00Z')
            """
        )
        conn.commit()
    finally:
        conn.close()

    # MemoryStore init must not raise UNIQUE constraint failed
    store = MemoryStore(db_path, log_dir=tmp_path / "logs")

    with store._connect() as check_conn:
        mems = check_conn.execute("SELECT id, namespace FROM memories").fetchall()
        mem_map = {row["id"]: row["namespace"] for row in mems}
        # Original survived, duplicate removed
        assert "m-orig" in mem_map
        assert "m-dup" not in mem_map
        assert mem_map["m-orig"] == "project:moebius"

        # Outgoing edge moved to survivor
        out_edge = check_conn.execute(
            "SELECT source_id, target_id, relation, target_namespace FROM memory_edges WHERE target_id = 'target-node'"
        ).fetchone()
        assert out_edge["source_id"] == "m-orig"
        assert out_edge["target_namespace"] == "project:moebius"

        # Incoming edge moved to point to survivor
        in_edge = check_conn.execute(
            "SELECT source_id, target_id, relation, target_namespace FROM memory_edges WHERE source_id = 'm-other'"
        ).fetchone()
        assert in_edge["target_id"] == "m-orig"
        assert in_edge["target_namespace"] == "project:moebius"

        # Annotation moved to survivor
        ann = check_conn.execute(
            "SELECT memory_id, title_after FROM memory_annotations WHERE memory_id = 'm-orig'"
        ).fetchone()
        assert ann is not None
        assert ann["title_after"] == "Title B"

        # memories_fts has survivor and no orphan
        fts_orig = check_conn.execute(
            "SELECT COUNT(*) AS count FROM memories_fts WHERE memory_id = 'm-orig'"
        ).fetchone()["count"]
        assert fts_orig == 1
        fts_dup = check_conn.execute("SELECT COUNT(*) AS count FROM memories_fts WHERE memory_id = 'm-dup'").fetchone()[
            "count"
        ]
        assert fts_dup == 0


def test_migration_preserves_earlier_original_when_lowercase_already_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_orig.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        from agent_mem_bridge.schema import init_db

        init_db(conn)
        content = "Shared rule"
        h = exact_content_hash(content)

        # Earlier original: project:Moebius
        conn.execute(
            """
            INSERT INTO memories (id, namespace, kind, title, content, content_hash, exact_content_hash, created_at)
            VALUES ('id-early', 'project:Moebius', 'memory', 'Early Rule', ?, 'h1', ?, '2026-01-01T00:00:00Z')
            """,
            (content, h),
        )
        # Later copy: project:moebius
        conn.execute(
            """
            INSERT INTO memories (id, namespace, kind, title, content, content_hash, exact_content_hash, created_at)
            VALUES ('id-later', 'project:moebius', 'memory', 'Later Rule', ?, 'h1', ?, '2026-01-02T00:00:00Z')
            """,
            (content, h),
        )
        conn.commit()
    finally:
        conn.close()

    store = MemoryStore(db_path, log_dir=tmp_path / "logs")

    with store._connect() as check_conn:
        rows = check_conn.execute("SELECT id, namespace, created_at FROM memories").fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "id-early"
        assert rows[0]["namespace"] == "project:moebius"
        assert rows[0]["created_at"] == "2026-01-01T00:00:00Z"


def test_migration_handles_unicode_case_folding(tmp_path: Path) -> None:
    db_path = tmp_path / "unicode.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        from agent_mem_bridge.schema import init_db

        init_db(conn)
        content = "Turkey market strategy"
        conn.execute(
            """
            INSERT INTO memories (id, namespace, kind, title, content, content_hash, exact_content_hash, created_at)
            VALUES ('tr-1', 'project:İstanbul', 'memory', 'Istanbul Strategy', ?, 'h1', ?, '2026-01-01T00:00:00Z')
            """,
            (content, exact_content_hash(content)),
        )
        conn.commit()
    finally:
        conn.close()

    store = MemoryStore(db_path, log_dir=tmp_path / "logs")

    # Both mixed-case and lowercase forms find the migrated memory
    recalled_1 = store.recall(
        namespace="project:İstanbul",
        query="Turkey market",
        kind="memory",
    )
    assert recalled_1["count"] == 1
    assert recalled_1["items"][0]["id"] == "tr-1"

    recalled_2 = store.recall(
        namespace="project:i̇stanbul",
        query="Turkey market",
        kind="memory",
    )
    assert recalled_2["count"] == 1
    assert recalled_2["items"][0]["id"] == "tr-1"


def test_bindings_file_with_conflicting_case_variants_raises_collision(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "repository"
    snapshot_root.mkdir(parents=True)
    bindings_file = snapshot_root / "bindings.json"

    # Conflicting case variants pointing to different repositories
    bindings_file.write_text(
        json.dumps(
            {
                "store_schema": BINDING_STORE_SCHEMA,
                "bindings": {
                    "project:Moebius": {"repository_id": "repo-A"},
                    "project:moebius": {"repository_id": "repo-B"},
                },
            }
        ),
        encoding="utf-8",
    )

    store = RepositorySnapshotStore(snapshot_root)
    with pytest.raises(ValueError, match="binding collision"):
        store.bindings()

    # Identical repository ID across case variants collapses cleanly without error
    bindings_file.write_text(
        json.dumps(
            {
                "store_schema": BINDING_STORE_SCHEMA,
                "bindings": {
                    "project:Moebius": {"repository_id": "repo-A"},
                    "project:moebius": {"repository_id": "repo-A"},
                },
            }
        ),
        encoding="utf-8",
    )
    resolved = store.bindings()
    assert "project:moebius" in resolved["bindings"]
    assert resolved["bindings"]["project:moebius"]["repository_id"] == "repo-A"


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
