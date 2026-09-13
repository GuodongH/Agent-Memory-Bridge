from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from agent_mem_bridge.database_maintenance import backup_database, verify_backup
from agent_mem_bridge.repository import content_hash_for_content, exact_content_hash_for_content
from agent_mem_bridge.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "rehearse_remote_migration.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("rehearse_remote_migration", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)


def _source_snapshot(tmp_path: Path) -> Path:
    source_dir = tmp_path / "source"
    store = MemoryStore(source_dir / "bridge.db", log_dir=source_dir / "logs")
    store.store(
        namespace="project:migration",
        kind="memory",
        title="Snapshot rehearsal evidence",
        content="The durable migration marker must survive rollback rehearsal.",
        tags=["migration:fixture"],
    )
    store.store(
        namespace="project:migration",
        kind="memory",
        title="Hidden rehearsal review evidence",
        content="The rehearsal review candidate must stay excluded from default recall.",
        tags=["kind:learning-candidate"],
    )
    store.store(namespace="project:migration", kind="signal", content="A durable signal fixture.")
    store.begin_run(
        workspace_key="project:migration",
        goal="Create a synthetic migration fixture.",
        idempotency_key="migration-fixture-run",
    )
    snapshot = tmp_path / "source-snapshot.db"
    backup_database(store.db_path, snapshot, full_verify=True)
    return snapshot


def test_rehearsal_uses_only_isolated_copies_and_preserves_rollback_contents(tmp_path: Path) -> None:
    snapshot = _source_snapshot(tmp_path)
    runtime = tmp_path / "rehearsal-runtime"

    report = rehearsal.rehearse_remote_migration(snapshot, runtime)

    assert report["ok"] is True
    assert report["evaluation"]["status"] == "INCONCLUSIVE"
    assert all(report["checks"].values())
    assert {
        "candidate_durable_equivalence_after_startup",
        "candidate_embedding_deficit_accounted",
        "candidate_governed_exclusion_preserved",
        "candidate_index_health_after_startup",
        "candidate_marker_exact_recall",
        "candidate_representative_lexical_parity",
        "rollback_index_health_after_startup",
        "rollback_representative_lexical_parity",
        "source_bytes_unchanged",
        "source_clone_index_health_after_startup",
    }.issubset(report["checks"])
    assert (runtime / "pre-manifest.json").is_file()
    assert (runtime / "post-manifest.json").is_file()
    assert (runtime / "rollback-manifest.json").is_file()
    assert (runtime / "candidate" / "bridge.db").is_file()
    assert (runtime / "rollback" / "bridge.db").is_file()
    assert verify_backup(snapshot, full=True)["ok"] is True
    assert len(report["representative"]["source_result_versions"]) == 1
    assert report["representative"]["source_result_versions"] == report["representative"]["candidate_result_versions"]
    assert report["source_snapshot_sha256"] == {
        "before": report["source_snapshot_sha256"]["before"],
        "after_candidate": report["source_snapshot_sha256"]["before"],
        "after_rollback": report["source_snapshot_sha256"]["before"],
    }
    pre_manifest = json.loads((runtime / "pre-manifest.json").read_text(encoding="utf-8"))
    post_manifest = json.loads((runtime / "post-manifest.json").read_text(encoding="utf-8"))
    assert post_manifest["durable_tables"]["memories"]["row_count"] == (
        pre_manifest["durable_tables"]["memories"]["row_count"] + 1
    )
    assert report["index_health"]["original_embedding_deficit"]["missing_after_marker"] == (
        report["index_health"]["original_embedding_deficit"]["missing_before"] + 1
    )


def test_isolated_environment_clears_inherited_bridge_controls_and_restores_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    db_path = runtime / "candidate" / "bridge.db"
    log_dir = runtime / "candidate" / "logs"
    inherited = {
        "AGENT_MEMORY_BRIDGE_CONFIG": str(tmp_path / "outside-config.toml"),
        "AGENT_MEMORY_BRIDGE_TELEMETRY_MODE": "jsonl",
        "AGENT_MEMORY_BRIDGE_TELEMETRY_LOG_DIR": str(tmp_path / "outside-telemetry"),
        "AGENT_MEMORY_BRIDGE_RETRIEVAL_MODE": "semantic",
        "AGENT_MEMORY_BRIDGE_EMBEDDING_PROVIDER": "command",
        "AGENT_MEMORY_BRIDGE_EMBEDDING_COMMAND": "/not-a-rehearsal-command",
        "AGENT_MEMORY_BRIDGE_CLASSIFIER_MODE": "assist",
        "AGENT_MEMORY_BRIDGE_CLASSIFIER_COMMAND": "/not-a-rehearsal-classifier",
        "AGENT_MEMORY_BRIDGE_WATCHER_ENABLED": "true",
        "AGENT_MEMORY_BRIDGE_REFLEX_ENABLED": "true",
        "AGENT_MEMORY_BRIDGE_CONSOLIDATION_ENABLED": "true",
        "AGENT_MEMORY_BRIDGE_EMBEDDING_SCHEDULER_ENABLED": "true",
        "AGENT_MEMORY_BRIDGE_UNRELATED_TEST_FLAG": "must-not-leak",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    before = {name: value for name, value in os.environ.items() if name.startswith("AGENT_MEMORY_BRIDGE_")}

    with rehearsal._isolated_store_environment(runtime, "candidate", db_path=db_path, log_dir=log_dir):
        assert os.environ["AGENT_MEMORY_BRIDGE_CONFIG"] == str(runtime / "candidate-config.toml")
        assert os.environ["AGENT_MEMORY_BRIDGE_HOME"] == str(runtime / "candidate-home")
        assert os.environ["AGENT_MEMORY_BRIDGE_DB_PATH"] == str(db_path)
        assert os.environ["AGENT_MEMORY_BRIDGE_LOG_DIR"] == str(log_dir)
        assert os.environ["AGENT_MEMORY_BRIDGE_RECALL_RECEIPT_SECRET_PATH"] == str(
            runtime / "candidate-receipt-secret.json"
        )
        assert os.environ["AGENT_MEMORY_BRIDGE_TELEMETRY_MODE"] == "off"
        assert os.environ["AGENT_MEMORY_BRIDGE_RETRIEVAL_MODE"] == "lexical"
        assert os.environ["AGENT_MEMORY_BRIDGE_EMBEDDING_PROVIDER"] == "hash"
        assert os.environ["AGENT_MEMORY_BRIDGE_EMBEDDING_CAPABILITY"] == "hashed_lexical"
        assert os.environ["AGENT_MEMORY_BRIDGE_CLASSIFIER_MODE"] == "off"
        assert os.environ["AGENT_MEMORY_BRIDGE_EMBEDDING_SCHEDULER_ENABLED"] == "false"
        assert os.environ["AGENT_MEMORY_BRIDGE_WATCHER_ENABLED"] == "false"
        assert os.environ["AGENT_MEMORY_BRIDGE_REFLEX_ENABLED"] == "false"
        assert os.environ["AGENT_MEMORY_BRIDGE_CONSOLIDATION_ENABLED"] == "false"
        assert "AGENT_MEMORY_BRIDGE_UNRELATED_TEST_FLAG" not in os.environ
        config_path = runtime / "candidate-config.toml"
        assert config_path.read_text(encoding="utf-8") == ""
        assert (runtime / "candidate-home").is_dir()
        assert db_path.parent.is_dir()
        assert log_dir.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
            assert stat.S_IMODE((runtime / "candidate-home").stat().st_mode) == 0o700

    assert {name: value for name, value in os.environ.items() if name.startswith("AGENT_MEMORY_BRIDGE_")} == before


def test_rehearsal_does_not_inherit_external_telemetry_or_command_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _source_snapshot(tmp_path)
    runtime = tmp_path / "rehearsal-runtime"
    outside_telemetry = tmp_path / "outside-telemetry"
    inherited = {
        "AGENT_MEMORY_BRIDGE_TELEMETRY_MODE": "jsonl",
        "AGENT_MEMORY_BRIDGE_TELEMETRY_LOG_DIR": str(outside_telemetry),
        "AGENT_MEMORY_BRIDGE_RETRIEVAL_MODE": "semantic",
        "AGENT_MEMORY_BRIDGE_EMBEDDING_PROVIDER": "command",
        "AGENT_MEMORY_BRIDGE_EMBEDDING_COMMAND": "/not-a-rehearsal-command",
        "AGENT_MEMORY_BRIDGE_CLASSIFIER_MODE": "assist",
        "AGENT_MEMORY_BRIDGE_CLASSIFIER_COMMAND": "/not-a-rehearsal-classifier",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    before = {name: value for name, value in os.environ.items() if name.startswith("AGENT_MEMORY_BRIDGE_")}

    report = rehearsal.rehearse_remote_migration(snapshot, runtime)

    assert report["ok"] is True
    assert not outside_telemetry.exists()
    assert (runtime / "candidate-config.toml").read_text(encoding="utf-8") == ""
    assert (runtime / "rollback-config.toml").read_text(encoding="utf-8") == ""
    assert {name: value for name, value in os.environ.items() if name.startswith("AGENT_MEMORY_BRIDGE_")} == before


def test_manifest_detects_equal_count_content_divergence(tmp_path: Path) -> None:
    snapshot = _source_snapshot(tmp_path)
    changed = tmp_path / "changed-snapshot.db"
    backup_database(snapshot, changed, full_verify=True)
    before = rehearsal.build_database_manifest(snapshot)
    new_content = "The durable migration marker was silently changed."
    with sqlite3.connect(changed) as conn:
        memory_id, title = conn.execute("SELECT id, title FROM memories WHERE kind = 'memory' LIMIT 1").fetchone()
        conn.execute(
            "UPDATE memories SET content = ?, content_hash = ?, exact_content_hash = ? WHERE id = ?",
            (
                new_content,
                content_hash_for_content(new_content),
                exact_content_hash_for_content(new_content),
                memory_id,
            ),
        )
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        conn.execute(
            "INSERT INTO memories_fts(memory_id, title, content) VALUES (?, ?, ?)",
            (memory_id, title or "", new_content),
        )
        conn.commit()

    after = rehearsal.build_database_manifest(changed)

    assert after["durable_tables"]["memories"]["row_count"] == before["durable_tables"]["memories"]["row_count"]
    assert rehearsal.durable_manifests_equivalent(before, after) is False
    assert verify_backup(changed, full=True)["ok"] is True


def test_rehearsal_rejects_wal_file_set_before_creating_runtime_dir(tmp_path: Path) -> None:
    snapshot = _source_snapshot(tmp_path)
    wal_sidecar = Path(f"{snapshot}-wal")
    wal_sidecar.write_bytes(b"not a snapshot WAL")
    runtime = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="consistent standalone backup"):
        rehearsal.rehearse_remote_migration(snapshot, runtime)

    assert not runtime.exists()


def test_rehearsal_rejects_corrupt_snapshot_before_creating_runtime_dir(tmp_path: Path) -> None:
    snapshot = tmp_path / "corrupt-snapshot.db"
    snapshot.write_bytes(b"not a SQLite database")
    runtime = tmp_path / "must-not-exist"

    with pytest.raises(RuntimeError, match="source snapshot failed full integrity verification"):
        rehearsal.rehearse_remote_migration(snapshot, runtime)

    assert not runtime.exists()
