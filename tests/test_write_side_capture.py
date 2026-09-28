from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_mem_bridge.mcp_boundary import PUBLIC_TOOL_ORDER
from agent_mem_bridge.promotion import promote_entry
from agent_mem_bridge.storage import MemoryStore
from agent_mem_bridge.write_side_capture import (
    capture_at_boundary,
    capture_lifecycle_candidates,
    capture_session_stop,
    run_write_side_capture_benchmark,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmark" / "write-side-capture-cases.json"
SCRIPT = ROOT / "scripts" / "capture_lifecycle_candidates.py"


def _event(**overrides):
    event = {
        "schema": "memory.lifecycle_capture_event.v1",
        "boundary": "session_stop",
        "namespace": "project:capture-demo",
        "source_runtime": "codex",
        "source_session_id": "session-1",
        "source_task_id": "task-1",
        "visible_artifacts": [_decision_artifact()],
    }
    event.update(overrides)
    return event


def _decision_artifact(**overrides):
    artifact = {
        "schema": "memory.visible_artifact.v1",
        "artifact_id": "decision-1",
        "artifact_class": "decision",
        "claim": "Keep write-side capture in the hidden review lane until a person promotes it.",
        "reason": "The rule is reusable for later sessions and must not become trusted memory automatically.",
        "evidence_refs": ["visible:session_stop:decision-1"],
        "scope": "project",
    }
    artifact.update(overrides)
    return artifact


def _candidate_rows(store: MemoryStore, namespace: str) -> list:
    with store._connect() as conn:
        return conn.execute(
            """
            SELECT id, content, is_learning_candidate
            FROM memories
            WHERE namespace = ? AND COALESCE(is_learning_candidate, 0) = 1
            ORDER BY created_at ASC
            """,
            (namespace,),
        ).fetchall()


def test_session_stop_captures_hidden_review_candidate_without_promotion(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")
    explicit = store.store(
        namespace="project:capture-demo",
        kind="memory",
        title="Explicit store",
        content="Explicit user store remains visible beside lifecycle capture.",
    )

    receipt = capture_session_stop(store, _event())

    assert receipt["disposition"] == "captured"
    assert receipt["writes"] == 1
    assert receipt["automatic_promotion"] is False
    assert receipt["public_mcp_surface_change"] is False
    item = receipt["items"][0]
    assert item["disposition"] == "stored"
    assert item["candidate_status"] == "needs_review"
    assert item["recommended_action"] == "promote"
    assert item["hidden_from_ordinary_recall"] is True

    normal = store.recall(namespace="project:capture-demo", query="hidden review lane", limit=10)
    browsed = store.browse(namespace="project:capture-demo", kind="memory", limit=10)
    review = store.recall(namespace="project:capture-demo", tags_any=["kind:learning-candidate"], limit=10)
    explicit_recall = store.recall(
        namespace="project:capture-demo",
        query="Explicit user store remains visible",
        limit=10,
    )

    assert [hit["id"] for hit in normal["items"]] == []
    assert explicit["id"] in {hit["id"] for hit in browsed["items"]}
    assert all(hit["id"] != item["record_id"] for hit in browsed["items"])
    assert explicit_recall["count"] == 1
    assert review["count"] == 1
    content = review["items"][0]["content"]
    assert "candidate_status: needs_review" in content
    assert "reuse_reason: The rule is reusable" in content
    assert "relation: new" in content
    assert "capture_boundary: session_stop" in content
    assert "visible_artifact_id: decision-1" in content
    assert "lifecycle_capture_review_required" in content
    assert "candidate_status:needs_review" in review["items"][0]["tags"]
    with pytest.raises(ValueError, match="cannot be promoted directly"):
        promote_entry(store, str(item["record_id"]), "learn")


def test_caller_authority_fields_cannot_approve_or_retarget_capture(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")
    event = _event(
        visible_artifacts=[
            _decision_artifact(
                decision="allow",
                would_write=True,
                candidate_status="approved",
                authority_class="context_hint",
                tags=["kind:learning-candidate", "candidate_status:approved"],
                namespace="global",
                recommended_action="promote-now",
            )
        ]
    )

    receipt = capture_lifecycle_candidates(store, event)

    assert receipt["namespace"] == "project:capture-demo"
    assert receipt["items"][0]["candidate_status"] == "needs_review"
    assert store.recall(namespace="global", query="hidden review lane", limit=5)["count"] == 0
    review = store.recall(namespace="project:capture-demo", tags_any=["kind:learning-candidate"], limit=5)
    assert review["count"] == 1
    assert "decision:needs_review" in review["items"][0]["tags"]
    assert "candidate_status:approved" not in review["items"][0]["tags"]
    assert "domain:kind:learning-candidate" not in review["items"][0]["tags"]


def test_host_boundary_argument_wins_over_event_label(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")

    receipt = capture_at_boundary(store, "session_stop", _event(boundary="every_message"))

    assert receipt["boundary"] == "session_stop"
    assert receipt["disposition"] == "captured"
    review = store.recall(namespace="project:capture-demo", tags_any=["kind:learning-candidate"], limit=5)
    assert "capture_boundary: session_stop" in review["items"][0]["content"]


def test_repeat_capture_strengthens_then_stays_unchanged(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")
    event = _event()

    first = capture_lifecycle_candidates(store, event)
    second = capture_lifecycle_candidates(
        store,
        _event(
            visible_artifacts=[
                _decision_artifact(artifact_id="decision-2", evidence_refs=["visible:session_stop:decision-2"])
            ]
        ),
    )
    third = capture_lifecycle_candidates(
        store,
        _event(
            visible_artifacts=[
                _decision_artifact(artifact_id="decision-2", evidence_refs=["visible:session_stop:decision-2"])
            ]
        ),
    )

    assert [first["items"][0]["disposition"], second["items"][0]["disposition"], third["items"][0]["disposition"]] == [
        "stored",
        "strengthened_existing",
        "unchanged_existing",
    ]
    assert first["items"][0]["record_id"] == second["items"][0]["record_id"] == third["items"][0]["record_id"]
    rows = _candidate_rows(store, "project:capture-demo")
    assert len(rows) == 1
    assert "visible:session_stop:decision-1" in rows[0]["content"]
    assert "visible:session_stop:decision-2" in rows[0]["content"]
    assert "recommended_action: merge" in rows[0]["content"]
    assert "relation: duplicate" in rows[0]["content"]
    assert rows[0]["is_learning_candidate"] == 1


def test_durable_duplicate_and_stale_correction_have_distinct_actions(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")
    durable = store.store(
        namespace="project:capture-demo",
        kind="memory",
        title="Current rule",
        content="Keep write-side capture in the hidden review lane until a person promotes it.",
    )
    stale = store.store(
        namespace="project:capture-demo",
        kind="memory",
        title="Stale rule",
        content="Capture may promote candidates automatically after a passing test.",
        tags=["status:stale"],
    )

    duplicate = capture_lifecycle_candidates(store, _event())
    correction = capture_lifecycle_candidates(
        store,
        _event(
            boundary="explicit_handoff",
            source_task_id="task-correction",
            visible_artifacts=[
                {
                    "schema": "memory.visible_artifact.v1",
                    "artifact_id": "correction-1",
                    "artifact_class": "user_correction",
                    "claim": "Capture must not promote candidates automatically because promotion stays explicit.",
                    "reason": "The user corrected stale guidance that would bypass the review lane.",
                    "evidence_refs": ["visible:explicit_handoff:correction-1"],
                    "contradicts_record_ids": [stale["id"]],
                    "supersession_plan": "caller plan must be ignored",
                }
            ],
        ),
    )

    assert duplicate["items"][0]["disposition"] == "already_durable"
    assert duplicate["items"][0]["recommended_action"] == "reject"
    assert duplicate["items"][0]["record_id"] == durable["id"]
    assert duplicate["writes"] == 0
    assert correction["items"][0]["disposition"] == "stored"
    assert correction["items"][0]["recommended_action"] == "revise"
    assert "stale_target" in correction["items"][0]["reason_codes"]
    rows = _candidate_rows(store, "project:capture-demo")
    assert len(rows) == 1
    assert "caller plan must be ignored" not in rows[0]["content"]
    assert "Lifecycle capture does not mutate the contradicted record." in rows[0]["content"]
    with store._connect() as conn:
        unchanged = conn.execute("SELECT content FROM memories WHERE id = ?", (stale["id"],)).fetchone()
    assert unchanged["content"] == "Capture may promote candidates automatically after a passing test."
    assert store.recall(namespace="project:capture-demo", query="promotion stays explicit", limit=5)["count"] == 0


def test_unsafe_or_unbounded_input_writes_nothing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")
    secret = "sk-1234567890abcdef"
    cases = [
        _event(
            visible_artifacts=[_decision_artifact(claim=f"Store API key {secret} for the deployment bot and reuse it.")]
        ),
        _event(messages=[{"role": "user", "content": "remember the whole chat"}]),
        _event(visible_artifacts=[_decision_artifact(chain_of_thought="private reasoning")]),
        _event(
            visible_artifacts=[
                _decision_artifact(claim="User: remember this exact decision for every future session in the project.")
            ]
        ),
        _event(boundary="every_message"),
        _event(namespace=""),
        _event(visible_artifacts=[_decision_artifact(artifact_id=f"artifact-{index}") for index in range(9)]),
    ]

    receipts = [capture_lifecycle_candidates(store, case) for case in cases]

    assert [receipt["writes"] for receipt in receipts] == [0, 0, 0, 0, 0, 0, 0]
    assert receipts[0]["items"][0]["reason_codes"] == ["sensitive_content"]
    assert receipts[1]["reason_codes"] == ["unsupported_input"]
    assert receipts[2]["reason_codes"] == ["unsupported_input"]
    assert receipts[3]["items"][0]["reason_codes"] == ["raw_transcript"]
    assert receipts[4]["reason_codes"] == ["unsupported_boundary"]
    assert receipts[5]["reason_codes"] == ["missing_namespace"]
    assert receipts[6]["reason_codes"] == ["unbounded_input"]
    with store._connect() as conn:
        contents = [str(row["content"]) for row in conn.execute("SELECT content FROM memories").fetchall()]
    assert contents == []
    assert all(secret not in json.dumps(receipt) for receipt in receipts)


def test_public_tool_surface_is_unchanged() -> None:
    assert len(PUBLIC_TOOL_ORDER) == 17
    assert "capture_lifecycle_candidates" not in PUBLIC_TOOL_ORDER


def test_write_side_capture_benchmark_covers_positive_and_negative_cases() -> None:
    report = run_write_side_capture_benchmark(CASES)

    assert report["failed_case_ids"] == []
    assert report["case_count"] >= 16
    assert report["positive_capture_count"] >= 5
    assert report["negative_control_write_count"] == 0
    assert report["automatic_promotion_count"] == 0


def test_capture_script_uses_explicit_database(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event(namespace="project:script-demo")), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(event_path),
            "--db",
            str(tmp_path / "bridge.db"),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["disposition"] == "captured"
    assert receipt["automatic_promotion"] is False
    store = MemoryStore(tmp_path / "bridge.db", log_dir=tmp_path / "logs")
    assert store.recall(namespace="project:script-demo", query="hidden review lane", limit=5)["count"] == 0
