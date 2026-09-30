from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "codex_stop_proof", Path(__file__).resolve().parents[1] / "scripts/check_codex_stop_capture.py"
)
assert SPEC and SPEC.loader
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)


def test_callback_projects_input_before_running_wiring(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "callback.jsonl"
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "stop_hook_active": False,
        "last_assistant_message": "visible",
        "transcript_path": "/private/transcript",
        "hidden_reasoning": "must not be retained",
    }
    monkeypatch.setattr(PROOF.sys, "stdin", io.StringIO(json.dumps(payload)))

    def wiring(actual):
        assert actual == payload
        assert path.exists()
        return {"continue": True}

    monkeypatch.setattr("agent_mem_bridge.lifecycle_activation.run_hook_payload", wiring)
    assert PROOF.callback(path) == 0
    assert json.loads(capsys.readouterr().out) == {"continue": True}
    projection = json.loads(path.read_text())
    assert projection["visible_sha256"] == hashlib.sha256(b"visible").hexdigest()
    assert projection["visible_bytes"] == 7
    assert set(projection) == {
        "hook_event_name",
        "session_id",
        "turn_id",
        "stop_hook_active",
        "visible_sha256",
        "visible_bytes",
    }
    redacted = PROOF.redact({"native": [projection], "thread_id": "session-1", "record_ids": ["candidate-1"]})
    assert redacted["thread_id"] == redacted["native"][0]["session_id"]
    assert "session-1" not in json.dumps(redacted)
    assert "candidate-1" not in json.dumps(redacted)


@pytest.mark.parametrize("positive", [True, False])
def test_host_proof_requires_completed_host_and_actual_callback(positive: bool) -> None:
    final = "bounded visible fixture"
    events = [
        {"type": "thread.started", "thread_id": "session-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
        {"type": "turn.completed"},
    ]
    receipt = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "hook_event_name": "Stop",
        "writes": int(positive),
        "disposition": "captured" if positive else "no_capture",
        "reason": "capture_policy_evaluated" if positive else "no_bounded_artifact",
        "automatic_promotion": False,
        "artifact_sha256": hashlib.sha256(final.encode()).hexdigest(),
    }
    assert PROOF.host_gate(events, [receipt], 0, final, positive)
    assert not PROOF.host_gate(events, [], 0, final, positive)
    assert not PROOF.host_gate(events[:-1], [receipt], 0, final, positive)
    assert not PROOF.host_gate(events, [receipt], 1, final, positive)
    assert not PROOF.host_gate(events, [receipt], 0, "different final", positive)
    assert not PROOF.host_gate(events, [receipt, receipt], 0, final, positive)
    if positive:
        assert not PROOF.host_gate(events, [{**receipt, "artifact_sha256": "wrong"}], 0, final, positive)
    for field, value in [
        ("session_id", "another-session"),
        ("turn_id", ""),
        ("hook_event_name", "PreCompact"),
        ("disposition", "error"),
        ("writes", None),
        ("automatic_promotion", True),
        ("reason", "local_authority_not_selected"),
    ]:
        changed = copy.deepcopy(receipt)
        changed[field] = value
        assert not PROOF.host_gate(events, [changed], 0, final, positive)
    tool = {"type": "item.completed", "item": {"type": "command_execution"}}
    assert not PROOF.host_gate([*events, tool], [receipt], 0, final, positive)
