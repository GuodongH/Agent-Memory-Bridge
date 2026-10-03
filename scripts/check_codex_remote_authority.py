"""Opt-in native Codex remote-authority proof with an isolated authority filesystem."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from _source_imports import ensure_source_root

ensure_source_root()

from check_codex_stop_capture import redact  # noqa: E402
from check_lifecycle_remote_authority import (  # noqa: E402
    FROZEN,
    PACK,
    ROOT,
    SOURCES,
    MemoryStore,
    RepositorySnapshotStore,
    durable_rows_digest,
    hashes,
    remote_server,
    repository_identity,
    verify_frozen,
)
from run_lifecycle_activation_benchmark import (  # noqa: E402
    _bridge_python,
    _codex_native,
    _model_gateway,
    _node_prefix,
    _probe_isolation,
    _require_bwrap,
)

from tools.evidence.lifecycle_activation import build_allowlisted_filesystem_argv  # noqa: E402

NATIVE_SOURCES = (
    *SOURCES,
    "scripts/check_codex_remote_authority.py",
    "scripts/check_codex_stop_capture.py",
    "scripts/run_lifecycle_activation_benchmark.py",
    "tools/evidence/lifecycle_activation.py",
)
HOOK_TRUST_WARNING = (
    "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation."
)
# This callback contains no answer key. Only runtime source and this callback are
# visible to the host; the collector, manifest and authority database are not.
CALLBACK = """import hashlib, json, os, sqlite3, sys
from pathlib import Path
import httpx2
from agent_mem_bridge.lifecycle_activation import run_hook_payload
payload = json.load(sys.stdin)
home = Path(os.environ["AGENT_MEMORY_BRIDGE_HOME"])
receipt = {"hook_event_name": payload.get("hook_event_name"), "session_id": payload.get("session_id"),
           "http_clients": 0, "local_open_attempts": [],
           "prompt_sha256": hashlib.sha256(payload.get("prompt", "").encode()).hexdigest()}
connect, client = sqlite3.connect, httpx2.AsyncClient
def guarded(database, *args, **kwargs):
    if str(home / "bridge.db") in str(database):
        receipt["local_open_attempts"].append("local_trap")
        raise RuntimeError("local_authority_open_attempt")
    return connect(database, *args, **kwargs)
def observed(*args, **kwargs):
    receipt["http_clients"] += 1
    return client(*args, **kwargs)
sqlite3.connect, httpx2.AsyncClient = guarded, observed
try:
    response = run_hook_payload(payload)
    receipt["response"] = response
    print(json.dumps(response))
finally:
    with (home / "native-callback.jsonl").open("a") as handle:
        handle.write(json.dumps(receipt) + "\\n")
"""


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def host_projection(stdout):
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return {
        "item_types": sorted(
            {str(e.get("item", {}).get("type")) for e in events if e.get("type") in {"item.started", "item.completed"}}
        ),
        "threads": [e.get("thread_id") for e in events if e.get("type") == "thread.started"],
        "completed_turns": sum(e.get("type") == "turn.completed" for e in events),
        "errors": [e.get("item", {}) for e in events if e.get("item", {}).get("type") == "error"],
        "tool_events": sum(
            e.get("type") in {"item.started", "item.completed"}
            and e.get("item", {}).get("type") not in {"agent_message", "reasoning", "todo_list", "error"}
            for e in events
        ),
        "messages": [
            e["item"].get("text", "")
            for e in events
            if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"
        ],
    }


def grade_lane(lane, case, pack):
    reasons = []
    host = lane.get("host", {})
    if lane.get("exit_code") != 0 or host.get("completed_turns") != 1 or host.get("tool_events") != 0:
        reasons.append("host_incomplete_or_used_tools")
    # Codex emits the explicit fixture-hook opt-in warning as an error item.
    # Retain it, but reject every other diagnostic instead of hiding host errors.
    if "errors" not in host or any(error.get("message") != HOOK_TRUST_WARNING for error in host["errors"]):
        reasons.append("host_errors_or_missing_diagnostics")
    messages = host.get("messages", [])
    if not messages or not all(isinstance(m, str) and m.strip() for m in messages):
        reasons.append("final_text_missing")
    threads = host.get("threads", [])
    callbacks, observations = lane.get("callbacks", []), lane.get("observations", [])
    if len(threads) != 1 or len(callbacks) != 1 or len(observations) != 1:
        reasons.append("native_callback_missing_or_repeated")
    else:
        callback, observation = callbacks[0], observations[0]
        prompt = pack["negative_prompt"] if case["id"] == "skip" else pack["material_prompt"]
        if callback.get("prompt_sha256") != hashlib.sha256(prompt.encode()).hexdigest():
            reasons.append("prompt_mismatch")
        if not threads[0] or callback.get("session_id") != threads[0] or observation.get("session_id") != threads[0]:
            reasons.append("session_mismatch")
        if (
            callback.get("hook_event_name") != "UserPromptSubmit"
            or callback.get("response", {}).get("continue") is not True
        ):
            reasons.append("native_hook_not_continued")
        if callback.get("http_clients") != case["http_clients"] or callback.get("local_open_attempts") != []:
            reasons.append("transport_or_local_fallback")
        if any(
            observation.get(k) != v
            for k, v in {
                "authority_mode": "remote",
                "resolution_status": "bound",
                "namespace": pack["namespace"],
                "recall_state": case["state"],
                "recall_invoked": case["state"] != "skipped",
                "adapter_loaded": True,
                "host": "codex",
                "hook_event_name": "UserPromptSubmit",
                "availability": "unknown"
                if case["id"] == "skip"
                else "error"
                if case["id"] == "unavailable"
                else "available",
            }.items()
        ):
            reasons.append("activation_contract")
        context = json.dumps(callback.get("response", {}))
        if case["state"] == "hit" and (
            not observation.get("recalled_ids")
            or "Untrusted governed context" not in context
            or "Live repository evidence wins" not in context
        ):
            reasons.append("hit_context_missing")
        if case["state"] == "error" and (
            not observation.get("error_type") or "not an empty memory result" not in context
        ):
            reasons.append("error_evidence_missing")
        if case["state"] == "no_hit" and "no matching memory" not in context:
            reasons.append("no_hit_evidence_missing")
        if case["state"] != "hit" and observation.get("recalled_ids") != []:
            reasons.append("unexpected_memory")
        if case["state"] == "skipped" and callback.get("response") != {"continue": True}:
            reasons.append("negative_context_injected")
    expected = {"namespace": pack["namespace"], "kind": "memory", "limit": 3}
    if lane.get("remote_calls") != [expected] * case["remote_recalls"]:
        reasons.append("remote_calls")
    if lane.get("isolation") != {"passed": True, "visible": [], "hit_count": 0, "nested_ok": True}:
        reasons.append("isolation_not_proved")
    before = lane.get("remote_rows_before")
    if not before or before != lane.get("remote_rows_after") or lane.get("trap_unchanged") is not True:
        reasons.append("durable_rows_changed_or_missing")
    final = "\n".join(m for m in messages if isinstance(m, str))
    if case["id"] in {"hit", "local_trap"} and "REMOTE-AUTHORITY-CONSTRAINT" not in final:
        reasons.append("remote_hit_not_used")
    if "CONFLICTING-LOCAL-TRAP" in json.dumps([messages, callbacks]):
        reasons.append("local_trap_used")
    return {"status": "FAIL" if reasons else "PASS", "reasons": reasons}


def score_report(report):
    pack = json.loads(PACK.read_text())
    expected = [case for case in pack["cases"] if case["id"] != "repeat"]
    lanes = report.get("lanes", [])
    reasons = []
    if report.get("schema") != "amb.codex-remote-authority-proof.v1" or report.get("condition") != "remote_authority":
        reasons.append("invalid_report")
    if report.get("frozen_sha256") != hashes(FROZEN) or not verify_frozen():
        reasons.append("freeze_mismatch")
    if report.get("source_sha256") != hashes(NATIVE_SOURCES):
        reasons.append("source_mismatch")
    if not report.get("host_version") or not report.get("model") or report.get("hook_loading") != "invocation_inline":
        reasons.append("host_identity_missing")
    if not isinstance(lanes, list) or not all(isinstance(lane, dict) for lane in lanes):
        lanes = []
    results = {}
    if [lane.get("id") for lane in lanes] != [case["id"] for case in expected]:
        reasons.append("case_set_incomplete")
    for case in expected:
        lane = next((lane for lane in lanes if lane.get("id") == case["id"]), None)
        if lane is None:
            results[case["id"]] = {"status": "NOT_RUN", "reasons": ["missing_lane"]}
        else:
            try:
                results[case["id"]] = grade_lane(lane, case, pack)
            except (TypeError, KeyError, AttributeError):
                results[case["id"]] = {"status": "FAIL", "reasons": ["malformed_evidence"]}
    passed = not reasons and all(result["status"] == "PASS" for result in results.values())
    return {"status": "PASS" if passed else "FAIL", "reasons": reasons, "lanes": results}


def collect_lane(case, pack, root, model, timeout):
    native, python, bwrap = _codex_native(), _bridge_python(), _require_bwrap()
    node, venv = _node_prefix(native), python.parent.parent
    repo, client, authority = root / "repo", root / "client", root / "authority"
    home = client / "bridge"
    for path in (repo, home, authority):
        path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    (home / "config.toml").write_text("")
    RepositorySnapshotStore(home / "repository").bind_namespace(
        pack["namespace"], repository_identity(repo)["repository_id"]
    )
    api_key, gateway = _model_gateway()
    source_mount = Path("/opt/amb-runtime-src")
    callback = client / "callback.py"
    callback.write_text(CALLBACK)
    catalog = client / "catalog.json"
    shutil.copyfile(Path.home() / ".codex/model-catalog.json", catalog)
    command = shlex.join([str(python), str(callback)])
    hook = 'hooks.UserPromptSubmit=[{hooks=[{type="command",command=' + json.dumps(command) + ",timeout=10}]}]"
    overrides = [
        'model_provider="local"',
        'model_providers.local.name="Fixture gateway"',
        'model_providers.local.wire_api="responses"',
        'model_providers.local.env_key="OPENAI_API_KEY"',
        "model_providers.local.base_url=" + json.dumps(gateway),
        "model_catalog_json=" + json.dumps(str(catalog)),
        'model_reasoning_effort="low"',
        "features.hooks=true",
        "features.plugins=false",
        "features.memories=false",
        "project_doc_max_bytes=0",
        'approval_policy="never"',
        hook,
        'developer_instructions="This is a controlled memory benchmark. Do not use tools. Answer the user from the provided context; if information is missing, say so. Do not invent remembered constraints."',
    ]
    prompt = pack["negative_prompt"] if case["id"] == "skip" else pack["material_prompt"]
    argv = [
        str(native),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--json",
        "--dangerously-bypass-hook-trust",
        "-s",
        "read-only",
        "-C",
        str(repo),
        "-m",
        model,
    ]
    for override in overrides:
        argv.extend(["-c", override])
    argv.append(prompt)
    protected = [ROOT, authority, Path.home() / ".codex", Path.home() / ".cache"]
    ro_binds = [(node, node), (venv, venv), (ROOT / "src", source_mount)]
    rw_binds = [(repo, repo), (client, client)]
    # Probe before planting the trap: the remote answer must never be visible.
    probe = _probe_isolation(
        bwrap=bwrap,
        ro_binds=ro_binds,
        rw_binds=rw_binds,
        protected=protected,
        markers=["REMOTE-AUTHORITY-CONSTRAINT"],
        scan_roots=[repo, client, source_mount, venv, node],
        absent=protected,
    )
    isolation = {
        "passed": not probe["visible"] and not probe["hits"] and probe["nested"]["ok"],
        "visible": probe["visible"],
        "hit_count": len(probe["hits"]),
        "nested_ok": probe["nested"]["ok"],
    }
    if not isolation["passed"]:
        return {
            "id": case["id"],
            "isolation": isolation,
            "result": {"status": "INCONCLUSIVE", "reasons": ["isolation_preflight_failed"]},
        }
    env = {
        "PATH": f"{node / 'bin'}:/usr/bin:/bin",
        "HOME": str(client),
        "CODEX_HOME": str(client),
        "LANG": "C.UTF-8",
        "OPENAI_API_KEY": api_key,
        "PYTHONPATH": str(source_mount),
    }
    for key, value in {
        "HOME": home,
        "CONFIG": home / "config.toml",
        "DB_PATH": home / "bridge.db",
        "LOG_DIR": home / "logs",
        "REPOSITORY_SNAPSHOT_ROOT": home / "repository",
        "RECALL_RECEIPT_SECRET_PATH": home / "secret.json",
        "TELEMETRY_MODE": "off",
    }.items():
        env["AGENT_MEMORY_BRIDGE_" + key] = str(value)
    setup_env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_MEMORY_BRIDGE_")}
    setup_env.update({k: v for k, v in env.items() if k.startswith("AGENT_MEMORY_BRIDGE_")})
    with patch.dict(os.environ, setup_env, clear=True):
        trap = MemoryStore(home / "bridge.db", log_dir=home / "logs")
        trap.store(namespace=pack["namespace"], kind="memory", content=pack["local_trap"])
        del trap
        gc.collect()
        trap_before = durable_rows_digest(home / "bridge.db")
        with remote_server(authority) as (url, store, calls), socket.socket() as refused:
            if case["id"] in {"hit", "local_trap"}:
                store.store(namespace=pack["namespace"], kind="memory", content=pack["remote_memory"])
            before = durable_rows_digest(store.db_path)
            if case["id"] == "unavailable":
                refused.bind(("127.0.0.1", 0))
                url = f"http://127.0.0.1:{refused.getsockname()[1]}/mcp"
            env["AGENT_MEMORY_BRIDGE_AUTHORITY_URL"] = url
            isolated = build_allowlisted_filesystem_argv(
                bwrap=bwrap, ro_binds=ro_binds, rw_binds=rw_binds, command=argv, protected_paths=protected
            )
            try:
                run = subprocess.run(
                    isolated,
                    cwd=repo,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                stdout, code = run.stdout, run.returncode
                stderr = run.stderr.replace(api_key, "[REDACTED]")[-2000:]
            except subprocess.TimeoutExpired:
                stdout, code, stderr = "", None, "host_timeout"
            after = durable_rows_digest(store.db_path)
        del store
        gc.collect()
    lane = {
        "id": case["id"],
        "isolation": isolation,
        "exit_code": code,
        "host": host_projection(stdout),
        "stderr_tail": stderr,
        "callbacks": rows(home / "native-callback.jsonl"),
        "observations": rows(home / "lifecycle/activation-evidence.jsonl"),
        "remote_calls": calls,
        "remote_rows_before": before,
        "remote_rows_after": after,
        "trap_unchanged": trap_before == durable_rows_digest(home / "bridge.db"),
    }
    lane["result"] = grade_lane(lane, case, pack)
    return lane


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model")
    parser.add_argument(
        "--score", type=Path, help="Re-score an existing report against current source and frozen inputs."
    )
    parser.add_argument("--case", choices=["hit", "skip", "no_hit", "unavailable", "local_trap"], action="append")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--reviewed-fixture-hook", action="store_true")
    args = parser.parse_args()
    if args.score:
        result = score_report(json.loads(args.score.read_text()))
        print(json.dumps(result, indent=2))
        return int(result["status"] != "PASS")
    if not args.output or not args.model or not args.reviewed_fixture_hook:
        parser.error("collection requires --output, --model and --reviewed-fixture-hook")
    if args.output.exists():
        parser.error("output must be a new private directory")
    if not verify_frozen():
        parser.error("freeze mismatch")
    # /etc is mounted for system libraries; never inherit administrator hooks.
    if Path("/etc/codex").exists():
        parser.error("ambient system policy exists; use a separately reviewed isolated host")
    args.output.mkdir(parents=True, mode=0o700)
    source_snapshot = hashes(NATIVE_SOURCES)
    pack = json.loads(PACK.read_text())
    cases = [c for c in pack["cases"] if c["id"] != "repeat" and (not args.case or c["id"] in args.case)]
    lanes = []
    with tempfile.TemporaryDirectory(prefix="amb-native-remote-") as directory:
        for case in cases:
            lane = collect_lane(case, pack, Path(directory) / case["id"], args.model, args.timeout)
            lanes.append(lane)
            print(json.dumps({"case": case["id"], **lane["result"]}), flush=True)
    report = {
        "schema": "amb.codex-remote-authority-proof.v1",
        "condition": "remote_authority",
        "hook_loading": "invocation_inline",
        "host_version": subprocess.check_output([str(_codex_native()), "--version"], text=True).strip(),
        "model": args.model,
        "frozen_sha256": hashes(FROZEN),
        "source_sha256": source_snapshot,
        "lanes": lanes,
        "native_codex_acceptance": "PASS"
        if len(lanes) == 5 and all(lane["result"]["status"] == "PASS" for lane in lanes)
        else "FAIL"
        if any(lane["result"]["status"] == "FAIL" for lane in lanes)
        else "INCONCLUSIVE",
    }
    report["score"] = score_report(report)
    report["native_codex_acceptance"] = report["score"]["status"]
    for lane in lanes:
        if "host" in lane:
            lane["host"]["threads"] = [
                "sha256:" + hashlib.sha256(value.encode()).hexdigest()
                for value in lane["host"]["threads"]
                if isinstance(value, str)
            ]
    (args.output / "report.json").write_text(json.dumps(redact(report), indent=2) + "\n")
    return int(report["score"]["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
