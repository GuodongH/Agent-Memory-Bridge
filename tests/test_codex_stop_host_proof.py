from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "codex_stop_proof", Path(__file__).resolve().parents[1] / "scripts/check_codex_stop_capture.py"
)
assert SPEC and SPEC.loader
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)


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
