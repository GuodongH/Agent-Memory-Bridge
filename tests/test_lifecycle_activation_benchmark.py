from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.evidence._temporary_store import ScopedTemporaryMemoryStore
from tools.evidence.lifecycle_activation import (
    PACK_PATH,
    assess_fixture_access,
    build_allowlisted_filesystem_argv,
    build_codex_collect_argv,
    check_pack,
    expand_probe,
    find_bwrap,
    host_lanes,
    load_pack,
    materialize_fixture,
    memory_only_markers,
    model_provider_override,
    mount_would_expose,
    parse_codex_exec_jsonl,
    prepare_collector_layout,
    render_collector_codex_preamble,
    render_isolated_codex_config,
    render_local_proxy_mcp_stanza,
    sandbox_for_case,
    score_observation,
    score_pack,
    seed_case_memories,
    validate_pack,
)

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_pack_is_internally_consistent() -> None:
    report = check_pack(ROOT)
    assert report["ok"], report["problems"]
    assert report["case_count"] == 10
    assert validate_pack(load_pack()) == []


def test_remembered_prose_without_a_tool_call_fails() -> None:
    case = _case("known-project-gotcha")
    observation = expand_probe(
        case,
        {
            "tool_calls": [],
            "final_text": "I remembered fixture-do-not-rewrite-latency-snapshots",
            "repo_paths_read": [],
        },
    )
    result = score_observation(case, observation)
    assert result["status"] == "FAIL"
    assert "required_recall_miss" in result["reasons"]
    assert result["required_recall_hit"] is False


def test_unavailable_error_described_as_empty_memory_fails() -> None:
    case = _case("amb-unavailable")
    observation = expand_probe(
        case,
        {
            "amb_available": False,
            "tool_calls": [
                {
                    "tool": "recall",
                    "namespace": "project:lifecycle-fixture",
                    "is_error": True,
                    "result_text": "connection failed",
                    "result_count": 0,
                }
            ],
            "final_text": "no relevant memory was found",
            "repo_paths_read": [],
        },
    )
    result = score_observation(case, observation)
    assert result["status"] == "FAIL"
    assert any(reason.startswith("forbidden_text:") for reason in result["reasons"])


def test_stale_token_chosen_over_current_file_is_misuse() -> None:
    case = _case("stale-superseded-conflict")
    observation = expand_probe(
        case,
        {
            "tool_calls": [
                {
                    "tool": "recall",
                    "namespace": "project:lifecycle-fixture",
                    "is_error": False,
                    "result_text": "port token: fixture-retired-port-1111",
                    "result_count": 1,
                }
            ],
            "final_text": "fixture-retired-port-1111",
            "repo_paths_read": ["NOTES.md"],
        },
    )
    result = score_observation(case, observation)
    assert result["status"] == "FAIL"
    assert result["stale_misuse"] is True


def test_missing_repo_trace_does_not_pass_reconciliation() -> None:
    case = _case("stale-superseded-conflict")
    observation = expand_probe(case, case["probes"]["adequate_good"])
    observation.pop("repo_paths_read")
    result = score_observation(case, observation)
    assert result["status"] == "INCONCLUSIVE"
    assert result["reasons"] == ["repo_evidence_missing"]


def test_synthetic_codex_observation_does_not_count_as_a_live_lane() -> None:
    case = _case("isolated-typo")
    observation = expand_probe(case, case["probes"]["adequate_good"])
    observation["host"] = {"id": "codex", "version": "test", "model": "none"}
    lanes = host_lanes([observation])
    assert lanes["codex"]["status"] == "NOT_RUN"
    assert lanes["opencode"]["status"] == "NOT_RUN"
    report = score_pack(load_pack(), [observation], freeze={"ok": True})
    assert "quality_score" not in report
    assert "quality_score" not in report["metrics"]
    assert report["instrument"]["checks"]["codex_live_trace"] is False
    assert report["instrument"]["pass"] is False


def test_wrong_namespace_is_not_a_useful_resolution() -> None:
    case = _case("cross-client-handoff")
    result = score_observation(case, expand_probe(case, case["probes"]["bad"]))
    assert result["status"] == "FAIL"
    assert "namespace_miss" in result["reasons"]
    assert result["required_recall_hit"] is True
    assert result["namespace_results"] == ["incorrect"]


def test_codex_jsonl_parser_reads_tool_call_and_not_prose() -> None:
    event = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "tool": "recall",
            "arguments": {"namespace": "project:lifecycle-fixture"},
            "result": {"content": [{"type": "text", "text": "gotcha token: fixture-do-not-rewrite-latency-snapshots"}]},
        },
    }
    message = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "fixture-do-not-rewrite-latency-snapshots"},
    }
    parsed = parse_codex_exec_jsonl("\n".join(json.dumps(item) for item in (event, message)))
    assert parsed["event_count"] == 2
    assert parsed["tool_calls"][0]["namespace"] == "project:lifecycle-fixture"
    assert "fixture-do-not-rewrite-latency-snapshots" in parsed["tool_calls"][0]["result_text"]
    assert parsed["final_text"] == "fixture-do-not-rewrite-latency-snapshots"


def test_started_mcp_event_is_not_a_second_call() -> None:
    started = {
        "type": "item.started",
        "item": {"type": "mcp_tool_call", "tool": "recall", "arguments": {"namespace": "project:lifecycle-fixture"}},
    }
    completed = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "tool": "recall",
            "arguments": {"namespace": "project:lifecycle-fixture"},
            "result": {"content": [{"type": "text", "text": "gotcha token"}]},
        },
    }
    parsed = parse_codex_exec_jsonl("\n".join(json.dumps(item) for item in (started, completed)))
    assert len(parsed["tool_calls"]) == 1
    assert parsed["tool_calls"][0]["tool"] == "recall"


def test_case_memories_seed_without_touching_a_live_database(tmp_path: Path) -> None:
    case = _case("known-project-gotcha")
    materialize_fixture(case, tmp_path / "repo")
    assert (
        "fixture-do-not-rewrite-latency-snapshots"
        not in (tmp_path / "repo" / "scripts" / "refresh_report.py").read_text()
    )
    store = ScopedTemporaryMemoryStore(tmp_path / "bridge.db", tmp_path / "logs")
    try:
        seed_case_memories(store, case)
        recalled = store.recall(namespace="project:lifecycle-fixture", query="gotcha token", limit=5)
    finally:
        store.close()
    payload = json.dumps(recalled)
    assert "fixture-do-not-rewrite-latency-snapshots" in payload


def _case(case_id: str) -> dict:
    return next(case for case in load_pack(PACK_PATH)["cases"] if case["id"] == case_id)


def test_isolated_codex_config_does_not_bind_production(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    config = tmp_path / "config.toml"
    rendered = render_isolated_codex_config(
        python_path=sys.executable,
        cwd=str(tmp_path),
        bridge_home=str(bridge),
        config_path=str(config),
        source_root=str(tmp_path / "src"),
    )
    assert "AGENT_MEMORY_BRIDGE_HOME" in rendered
    assert "192.168." not in rendered
    assert "58080" not in rendered
    assert "AGENT_MEMORY_BRIDGE_REMOTE_URL" not in rendered


def test_codex_collector_keeps_the_store_out_of_the_workspace(tmp_path: Path) -> None:
    argv = build_codex_collect_argv(
        sandbox=sandbox_for_case("known-project-gotcha"),
        model="recorded-model",
        fixture_repo=tmp_path / "fixture-repo",
        output_path=tmp_path / "last-message.md",
        prompt="Reply with the token.",
    )
    assert "--add-dir" not in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "plugins" in argv and "memories" in argv
    assert sandbox_for_case("isolated-typo") == "workspace-write"
    preamble = render_collector_codex_preamble(
        catalog_path=str(tmp_path / "catalog.json"),
        fixture_repo=str(tmp_path / "fixture-repo"),
    )
    assert "plugins = false" in preamble
    assert "CODEX_HOME" in preamble
    assert "--add-dir" not in preamble
    assert "192.168." not in preamble
    assert "58080" not in preamble


def test_direct_store_read_is_not_a_host_result() -> None:
    case = _case("known-project-gotcha")
    observation = expand_probe(case, case["probes"]["adequate_good"])
    observation["execution_kind"] = "live"
    observation["host"] = {"id": "codex", "version": "test", "model": "none"}
    observation["fixture_access"] = {
        "direct_read": True,
        "signals": ["marker_in_command_output"],
        "command_count": 1,
    }
    result = score_observation(case, observation)
    assert result["status"] == "INCONCLUSIVE"
    assert "fixture_leak" in result["reasons"]
    assert result["required_recall_hit"] is None
    lanes = host_lanes([observation])
    assert lanes["codex"]["status"] == "INCONCLUSIVE"
    assert lanes["codex"]["case_ids"] == []
    assert lanes["codex"]["contaminated_case_ids"] == ["known-project-gotcha"]
    report = score_pack(load_pack(), [observation], freeze={"ok": True})
    assert report["instrument"]["checks"]["codex_live_trace"] is False


def test_mcp_recall_is_not_treated_as_a_store_read() -> None:
    case = _case("known-project-gotcha")
    marker = "fixture-do-not-rewrite-latency-snapshots"
    event = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "tool": "recall",
            "arguments": {"namespace": "project:lifecycle-fixture"},
            "result": {"content": [{"type": "text", "text": f"gotcha token: {marker}"}]},
        },
    }
    command = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "sed -n 1,20p scripts/refresh_report.py",
            "aggregated_output": "# Write the report to the path you were given.\n",
            "exit_code": 0,
        },
    }
    parsed = parse_codex_exec_jsonl("\n".join(json.dumps(item) for item in (event, command)))
    access = assess_fixture_access(
        parsed["commands"],
        markers=memory_only_markers(case),
        hidden_paths=["/hidden/store-home"],
    )
    assert access["direct_read"] is False
    assert marker in memory_only_markers(case)
    stale = _case("stale-superseded-conflict")
    assert "fixture-retired-port-1111" in memory_only_markers(stale)
    assert "fixture-current-port-2222" not in memory_only_markers(stale)
    leaked = assess_fixture_access(
        [{"command": "sqlite3 /hidden/store-home/fixture-store.sqlite", "output": marker}],
        markers=memory_only_markers(case),
        hidden_paths=["/hidden/store-home"],
    )
    assert leaked["direct_read"] is True
    assert "marker_in_command_output" in leaked["signals"]
    assert "collector_path" in leaked["signals"]


def test_checkout_parent_does_not_contain_the_store(tmp_path: Path) -> None:
    layout = prepare_collector_layout(
        store_root=tmp_path / "stores",
        client_root=tmp_path / "clients",
        workspace_root=tmp_path / "workspaces",
    )
    parent = layout["fixture_repo"].parent
    assert list(parent.iterdir()) == [layout["fixture_repo"]]
    assert layout["store_home"].parent != parent
    assert layout["codex_home"].parent != parent
    assert not layout["store_home"].is_relative_to(parent)
    assert not layout["codex_home"].is_relative_to(parent)


def test_proxy_stanza_and_filesystem_hide_the_answer_key(tmp_path: Path) -> None:
    stanza = render_local_proxy_mcp_stanza(
        python_path="/usr/bin/python3",
        url="http://127.0.0.1:9/mcp",
        token_file=str(tmp_path / "token"),
    )
    assert "agent_mem_bridge" in stanza
    assert "AGENT_MEMORY_BRIDGE_HOME" not in stanza
    assert "fixture-store.sqlite" not in stanza
    assert "192.168." not in stanza
    secret = tmp_path / "store" / "fixture-store.sqlite"
    secret.parent.mkdir()
    secret.write_text("hidden")
    visible = tmp_path / "client-home"
    visible.mkdir()
    argv = build_allowlisted_filesystem_argv(
        bwrap=tmp_path / "bwrap",
        ro_binds=[],
        rw_binds=[(visible, visible)],
        command=["codex", "exec", "hello"],
        protected_paths=[secret],
    )
    assert argv[argv.index("--") + 1 :] == ["codex", "exec", "hello"]
    assert "--tmpfs" in argv
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    assert "--ro-bind" in argv
    assert str(secret) not in argv
    assert str(tmp_path / "repo") not in argv
    assert "--add-dir" not in argv
    pairs = list(zip(argv, argv[1:], argv[2:]))
    assert ("--ro-bind", "/", "/") not in pairs
    try:
        build_allowlisted_filesystem_argv(
            bwrap=tmp_path / "bwrap",
            ro_binds=[(secret.parent, secret.parent)],
            rw_binds=[],
            command=["codex", "exec", "hello"],
            protected_paths=[secret],
        )
    except ValueError as exc:
        assert "protected" in str(exc)
    else:
        raise AssertionError("mount containing the answer key was accepted")


def test_mount_exposure_does_not_treat_a_child_as_the_parent(tmp_path: Path) -> None:
    child = tmp_path / "ws"
    child.mkdir()
    assert mount_would_expose(tmp_path, child)
    assert not mount_would_expose(child, tmp_path)


def test_model_gateway_override_rejects_the_production_amb_port() -> None:
    override = model_provider_override("http://127.0.0.1:8317/v1")
    assert "58080" not in override
    assert "/mcp" not in override
    try:
        model_provider_override("http://192.168.50.175:58080/mcp")
    except ValueError as exc:
        assert "production AMB" in str(exc)
    else:
        raise AssertionError("production AMB gateway was accepted")


def test_allowlisted_sandbox_hides_a_host_marker(tmp_path: Path) -> None:
    bwrap = find_bwrap()
    if bwrap is None:
        import pytest

        pytest.skip("bwrap is unavailable")
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    marker = "fixture-do-not-rewrite-latency-snapshots"
    (secret_dir / "key.txt").write_text(marker, encoding="utf-8")
    visible = tmp_path / "visible"
    visible.mkdir()
    (visible / "note.txt").write_text("plain note", encoding="utf-8")
    script = (
        "import os,sys; secret, note = sys.argv[1:];"
        "raise SystemExit(0 if (not os.path.exists(secret) and os.path.exists(note)) else 1)"
    )
    argv = build_allowlisted_filesystem_argv(
        bwrap=bwrap,
        ro_binds=[],
        rw_binds=[(visible, visible)],
        command=["/usr/bin/python3", "-c", script, str(secret_dir / "key.txt"), str(visible / "note.txt")],
        protected_paths=[secret_dir],
    )
    completed = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_secret_redaction_does_not_change_short_labels() -> None:
    import importlib
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    module = importlib.import_module("run_lifecycle_activation_benchmark")
    assert module._redact("live", ["secret-value"]) == "live"
    assert module._redact("secret-value", ["secret-value"]) == "[redacted]"
