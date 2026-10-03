from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "codex_remote_check", Path(__file__).resolve().parents[1] / "scripts/check_codex_remote_authority.py"
)
assert SPEC and SPEC.loader
check = importlib.util.module_from_spec(SPEC)
with pytest.MonkeyPatch.context() as patch:
    patch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    SPEC.loader.exec_module(check)


@pytest.fixture
def report():
    pack = json.loads(check.PACK.read_text())
    case = next(case for case in pack["cases"] if case["id"] == "skip")
    lane = {
        "id": "skip",
        "exit_code": 0,
        "host": {
            "threads": ["session"],
            "completed_turns": 1,
            "tool_events": 0,
            "messages": ["Which typo?"],
            "errors": [],
        },
        "callbacks": [
            {
                "session_id": "session",
                "hook_event_name": "UserPromptSubmit",
                "prompt_sha256": hashlib.sha256(pack["negative_prompt"].encode()).hexdigest(),
                "http_clients": 0,
                "local_open_attempts": [],
                "response": {"continue": True},
            }
        ],
        "observations": [
            {
                "session_id": "session",
                "authority_mode": "remote",
                "resolution_status": "bound",
                "namespace": pack["namespace"],
                "recall_state": "skipped",
                "recall_invoked": False,
                "adapter_loaded": True,
                "host": "codex",
                "hook_event_name": "UserPromptSubmit",
                "availability": "unknown",
                "recalled_ids": [],
            }
        ],
        "remote_calls": [],
        "isolation": {"passed": True, "visible": [], "hit_count": 0, "nested_ok": True},
        "remote_rows_before": "digest",
        "remote_rows_after": "digest",
        "trap_unchanged": True,
    }
    return lane, case, pack


def test_negative_lane_requires_callback_evidence(report):
    lane, case, pack = report
    assert check.grade_lane(lane, case, pack)["status"] == "PASS"
    lane["callbacks"] = []
    assert check.grade_lane(lane, case, pack)["status"] == "FAIL"


@pytest.mark.parametrize("field", ["errors", "threads", "completed_turns", "tool_events", "messages"])
def test_missing_host_evidence_fails(report, field):
    lane, case, pack = report
    del lane["host"][field]
    assert check.grade_lane(lane, case, pack)["status"] == "FAIL"


def test_warning_is_preserved_but_not_counted_as_tool(report):
    lane, case, pack = report
    events = [
        {"type": "thread.started", "thread_id": "session"},
        {"type": "item.completed", "item": {"type": "error", "message": check.HOOK_TRUST_WARNING}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Which typo?"}},
        {"type": "turn.completed"},
    ]
    lane["host"] = check.host_projection("\n".join(map(json.dumps, events)))
    assert lane["host"]["errors"]
    assert lane["host"]["tool_events"] == 0
    assert check.grade_lane(lane, case, pack)["status"] == "PASS"
    lane["host"]["errors"][0]["message"] = "unexpected failure"
    assert check.grade_lane(lane, case, pack)["status"] == "FAIL"


def test_unknown_item_remains_a_tool_event():
    assert (
        check.host_projection(json.dumps({"type": "item.completed", "item": {"type": "new_tool"}}))["tool_events"] == 1
    )


def test_redacted_session_joins_still_score(report):
    lane, case, pack = report
    published = copy.deepcopy(lane)
    published["host"]["threads"] = ["sha256:" + hashlib.sha256(b"session").hexdigest()]
    published = check.redact(published)
    assert check.grade_lane(published, case, pack)["status"] == "PASS"


def test_partial_report_cannot_pass(report):
    lane, _, _ = report
    result = check.score_report(
        {
            "schema": "amb.codex-remote-authority-proof.v1",
            "condition": "remote_authority",
            "frozen_sha256": check.hashes(check.FROZEN),
            "source_sha256": check.hashes(check.NATIVE_SOURCES),
            "host_version": "fixture",
            "model": "fixture",
            "hook_loading": "invocation_inline",
            "lanes": [lane],
        }
    )
    assert result["status"] == "FAIL"
    assert "case_set_incomplete" in result["reasons"]


def test_partial_collection_exits_nonzero(report, monkeypatch, tmp_path):
    lane, case, pack = report
    lane["result"] = check.grade_lane(lane, case, pack)
    output = tmp_path / "report"
    monkeypatch.setattr(
        "sys.argv",
        ["collector", "--output", str(output), "--model", "fixture", "--case", "skip", "--reviewed-fixture-hook"],
    )
    monkeypatch.setattr(check, "collect_lane", lambda *args: copy.deepcopy(lane))
    monkeypatch.setattr(check, "_codex_native", lambda: Path("fixture-codex"))
    monkeypatch.setattr(check.subprocess, "check_output", lambda *args, **kwargs: "fixture-version")
    assert check.main() == 1
    saved = json.loads((output / "report.json").read_text())
    assert saved["native_codex_acceptance"] == "FAIL"
    assert check.score_report(saved) == saved["score"]
