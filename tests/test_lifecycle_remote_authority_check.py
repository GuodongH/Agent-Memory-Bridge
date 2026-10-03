from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remote_check", ROOT / "scripts/check_lifecycle_remote_authority.py")
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture(scope="module")
def report():
    return check.collect()


def test_real_http_contract_and_native_lane_stay_separate(report):
    assert report["result"] == {"hook_contract": "PASS", "native_codex_acceptance": "NOT_RUN", "reasons": []}
    assert report["execution_kind"] == "direct_hook"
    assert len(report["cases"]) == 6


@pytest.mark.parametrize(
    "field",
    [
        "observations",
        "responses",
        "remote_calls",
        "http_clients",
        "local_open_attempts",
        "trap_seeded",
        "trap_unchanged",
    ],
)
def test_missing_evidence_cannot_pass(report, field):
    damaged = copy.deepcopy(report)
    del damaged["cases"][0][field]
    assert check.score(damaged)["hook_contract"] == "FAIL"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_namespace",
        "extra_recall",
        "local_open",
        "negative_network",
        "fake_no_hit",
        "repeat_spam",
        "missing_case",
        "duplicate_case",
        "wrong_source",
        "wrong_freeze",
        "missing_all",
        "trap_injection",
    ],
)
def test_falsified_evidence_fails(report, mutation):
    damaged = copy.deepcopy(report)
    hit, skip, empty, error, trap, repeat = damaged["cases"]
    if mutation == "wrong_namespace":
        hit["remote_calls"][0]["namespace"] = "project:forged"
    elif mutation == "extra_recall":
        hit["remote_calls"] *= 2
    elif mutation == "local_open":
        trap["local_open_attempts"] = ["local_trap"]
    elif mutation == "negative_network":
        skip["http_clients"] = 1
    elif mutation == "fake_no_hit":
        error["observations"][0]["recall_state"] = "no_hit"
    elif mutation == "repeat_spam":
        repeat["http_clients"] = 2
    elif mutation == "missing_case":
        damaged["cases"].pop()
    elif mutation == "duplicate_case":
        damaged["cases"].append(hit)
    elif mutation == "wrong_source":
        damaged["source_sha256"] = {}
    elif mutation == "wrong_freeze":
        damaged["frozen_sha256"] = {}
    elif mutation == "missing_all":
        damaged = {"cases": [{"id": "hit", "checks": {"looks_good": True}, "status": "PASS"}]}
    elif mutation == "trap_injection":
        trap["responses"][0]["context"] = "CONFLICTING-LOCAL-TRAP"
    assert check.score(damaged)["hook_contract"] == "FAIL"


def test_ambient_authority_and_token_are_not_used(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_AUTHORITY_URL", "https://production.invalid/mcp")
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HTTP_TOKEN_FILE", str(tmp_path / "missing-token"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_DB_PATH", str(tmp_path / "must-not-exist.db"))
    pack = json.loads(check.PACK.read_text())
    row = check.collect_case(pack["cases"][0], tmp_path / "fixture", pack)
    assert row["observations"][0]["recall_state"] == "hit"
    assert not (tmp_path / "must-not-exist.db").exists()
    assert check.os.environ["AGENT_MEMORY_BRIDGE_AUTHORITY_URL"] == "https://production.invalid/mcp"


def test_missing_freeze_entry_fails(monkeypatch):
    original = check.hashes
    monkeypatch.setattr(check, "hashes", lambda paths: {**original(paths), "unexpected-file": "digest"})
    assert check.verify_frozen() is False


def test_remote_durable_mutation_fails(report):
    damaged = copy.deepcopy(report)
    damaged["cases"][0]["remote_rows_after"] = "0" * 64
    assert check.score(damaged)["hook_contract"] == "FAIL"


def test_direct_hook_cannot_be_relabelled_as_native(report):
    damaged = copy.deepcopy(report)
    damaged["execution_kind"] = "live"
    result = check.score(damaged)
    assert result["hook_contract"] == "FAIL"
    assert result["native_codex_acceptance"] == "NOT_RUN"
