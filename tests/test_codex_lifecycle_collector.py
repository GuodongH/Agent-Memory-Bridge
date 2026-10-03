from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from tools.evidence.lifecycle_activation import (
    bind_fixture_namespace,
    git_commit_fixture,
    load_pack,
    materialize_fixture,
    render_collector_codex_preamble,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "codex_collector", ROOT / "scripts/run_codex_lifecycle_activation_benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_permission_profile_is_not_overridden_by_legacy_sandbox(collector) -> None:
    argv = collector._build_codex_collect_argv(
        sandbox="workspace-write",
        model="fixture-model",
        fixture_repo=Path("/fixture"),
        output_path=Path("/last-message"),
        prompt="Fix the typo",
        condition="adapter_enabled",
        permission_profile="amb-benchmark",
    )
    assert "-s" not in argv
    assert "--sandbox" not in argv
    assert 'default_permissions="amb-benchmark"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("sandbox,parent", [("read-only", ":read-only"), ("workspace-write", ":workspace")])
def test_profile_denies_private_paths_but_preserves_builtin_safety(collector, sandbox, parent) -> None:
    client, repo, source = Path("/fixture client"), Path("/fixture repo"), Path("/opt/amb-lifecycle-src")
    config = render_collector_codex_preamble(catalog_path=str(client / "catalog.json"), fixture_repo=str(repo))
    config += collector._codex_fixture_permissions(
        sandbox=sandbox, codex_home=client, fixture_repo=repo, source_mount=source
    )
    parsed = tomllib.loads(config)
    profile = parsed["permissions"]["amb-benchmark"]
    assert profile["extends"] == parent
    assert profile["network"]["enabled"] is False
    for path in (collector.CODEX_STORE_MOUNT, client, source, repo / ".codex"):
        assert profile["filesystem"][str(path)] == "deny"


def test_hook_uses_guest_store_path_and_quotes_space_path(collector, tmp_path) -> None:
    assert str(collector.CODEX_STORE_MOUNT) == "/opt/amb-lifecycle-store"
    assert str(collector.CODEX_STORE_MOUNT / "config.toml") == "/opt/amb-lifecycle-store/config.toml"
    client, repo = tmp_path / "client with spaces", tmp_path / "repo"
    client.mkdir()
    repo.mkdir()
    collector._install_adapter_hook(
        codex_home=client,
        fixture_repo=repo,
        store_home=collector.CODEX_STORE_MOUNT,
        python_path=Path(sys.executable),
        source_mount=Path("/opt/amb-lifecycle-src"),
    )
    wrapper = (client / "hook_capture.py").read_text()
    assert str(collector.CODEX_STORE_MOUNT) in wrapper
    assert ".cache" not in wrapper
    hooks = json.loads((repo / ".codex/hooks.json").read_text())
    assert shlex.split(hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]) == [
        "/usr/bin/python3",
        str(client / "hook_capture.py"),
    ]


def test_null_namespace_control_does_not_bind_string_none(collector, tmp_path) -> None:
    case = next(item for item in load_pack(version="v2")["cases"] if item["id"] == "isolated-typo")
    repo, home = tmp_path / "repo", tmp_path / "home"
    materialize_fixture(case, repo)
    git_commit_fixture(repo)
    assert collector._bind_codex_fixture_namespace(case, repo, home)
    assert (home / "config.toml").is_file()
    assert (home / "fixture-store.sqlite").is_file()
    assert not (home / "repository/bindings.json").exists()


def test_source_receipt_includes_edited_policy_and_resolver(collector) -> None:
    identity = collector._source_sha256()
    assert all("\\" not in path for path in identity)
    for path in (
        "scripts/run_codex_lifecycle_activation_benchmark.py",
        "src/agent_mem_bridge/activation_policy.py",
        "src/agent_mem_bridge/lifecycle_activation.py",
        "src/agent_mem_bridge/project_resolution.py",
    ):
        assert len(identity[path]) == 64


def test_repaired_entrypoint_selects_repaired_codex_collector(collector, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(collector, "collect_codex", lambda *args: calls.append(args) or 0)
    assert (
        collector.main(
            [
                "collect-codex",
                "--pack",
                "v2",
                "--condition",
                "adapter_enabled",
                "--adapter-backend",
                "remote_loopback",
                "--measurement-revision",
                "codex-repo-read-v1",
                "--case-id",
                "known-project-gotcha",
                "--out",
                str(tmp_path),
                "--model",
                "gpt-6-luna",
                "--timeout",
                "150",
            ]
        )
        == 0
    )
    assert calls == [
        (
            "known-project-gotcha",
            tmp_path,
            "gpt-6-luna",
            150.0,
            "v2",
            "adapter_enabled",
            "remote_loopback",
            "codex-repo-read-v1",
        )
    ]


def test_repaired_entrypoint_retains_canonical_rescore_dispatch(collector, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(collector.canonical, "main", lambda args: calls.append(args) or 0)
    argv = ["rescore-codex", "--evidence-root", "fixture-live", "--out", "fixture-rescore"]
    assert collector.main(argv) == 0
    assert calls == [argv]


def test_broken_local_authority_is_error_not_empty_or_unconfigured(collector, tmp_path, monkeypatch) -> None:
    from agent_mem_bridge.lifecycle_activation import run_hook_payload

    case = next(item for item in load_pack(version="v2")["cases"] if item["id"] == "amb-unavailable")
    repo, home = tmp_path / "repo", tmp_path / "home"
    materialize_fixture(case, repo)
    git_commit_fixture(repo)
    bind_fixture_namespace(case, repo, home)
    database = home / "fixture-store.sqlite"
    database.write_bytes(b"unavailable local SQLite fixture\n")
    before = collector._fixture_memory_digest(home)
    for name in list(os.environ):
        if name.startswith("AGENT_MEMORY_BRIDGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HOME", str(home))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_CONFIG", str(home / "config.toml"))
    response = run_hook_payload(
        {"hook_event_name": "UserPromptSubmit", "session_id": "local-error", "cwd": str(repo), "prompt": case["prompt"]}
    )
    row = json.loads((home / "lifecycle/activation-evidence.jsonl").read_text().splitlines()[-1])
    assert row["authority_mode"] == "local"
    assert row["availability"] == "error"
    assert row["recall_state"] == "error"
    assert row["recall_invoked"] is True
    assert "not an empty memory result" in response["hookSpecificOutput"]["additionalContext"]
    assert "Tell the user that AMB was unavailable" in response["hookSpecificOutput"]["additionalContext"]
    assert collector._fixture_memory_digest(home) == before


@pytest.mark.parametrize("identified", [True, False])
def test_context_receipts_keep_compaction_and_repeat_separate_from_recalls(collector, tmp_path, identified) -> None:
    events = [
        ("SessionStart", False, ""),
        ("UserPromptSubmit", True, "first recall"),
        ("UserPromptSubmit", False, ""),
        ("PreCompact", False, ""),
        ("SessionStart", False, "derived continuity"),
        ("UserPromptSubmit", True, "second recall"),
    ]
    rows = []
    responses = []
    for event, recalled, context in events:
        key = {"session_id": "fixture-session", "hook_event_name": event}
        rows.append({**key, "recall_invoked": recalled})
        payload = {"continue": True}
        if context:
            payload["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
        responses.append({**(key if identified else {}), "returncode": 0, "stdout": json.dumps(payload)})
    directory = tmp_path / "lifecycle"
    directory.mkdir()
    (directory / "activation-evidence.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    (directory / "hook-responses.jsonl").write_text("\n".join(map(json.dumps, responses)) + "\n")
    records = collector._adapter_records(tmp_path)
    assert [row.get("result_text") for row in records] == [
        None,
        "first recall",
        None,
        None,
        "derived continuity",
        "second recall",
    ]
