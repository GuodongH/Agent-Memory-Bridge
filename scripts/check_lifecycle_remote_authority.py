"""Collect isolated HTTP hook evidence for #58, without claiming native host acceptance."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx2  # noqa: E402
import uvicorn  # noqa: E402

from agent_mem_bridge.deployment_config import HttpTransportConfig  # noqa: E402
from agent_mem_bridge.http_transport import build_http_app  # noqa: E402
from agent_mem_bridge.lifecycle_activation import run_hook_payload  # noqa: E402
from agent_mem_bridge.repository_snapshot_store import RepositorySnapshotStore, repository_identity  # noqa: E402
from agent_mem_bridge.storage import MemoryStore  # noqa: E402

PACK = ROOT / "benchmark/lifecycle-activation-remote-v1.json"
FROZEN = tuple(
    f"benchmark/lifecycle-activation-{version}.{suffix}"
    for version in ("v1", "v2", "remote-v1")
    for suffix in ("json", "md")
)
SOURCES = (
    "scripts/check_lifecycle_remote_authority.py",
    "src/agent_mem_bridge/lifecycle_activation.py",
    "src/agent_mem_bridge/lifecycle_http.py",
    "src/agent_mem_bridge/activation_policy.py",
    "src/agent_mem_bridge/project_resolution.py",
)


def hashes(paths):
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def verify_frozen():
    expected = {}
    for version in ("v1", "v2", "remote-v1"):
        for line in (ROOT / f"benchmark/lifecycle-activation-{version}.sha256").read_text().splitlines():
            digest, path = line.split()
            expected[path] = digest
    return expected == hashes(FROZEN)


def durable_rows_digest(path):
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    finally:
        connection.close()


@contextmanager
def remote_server(root):
    """Observe actual server recall dispatch, not the hook's claim of dispatch."""
    store = MemoryStore(root / "remote.db", log_dir=root / "remote-logs")
    calls = []
    original = store.recall

    def observed(**kwargs):
        calls.append({key: kwargs.get(key) for key in ("namespace", "kind", "limit")})
        return original(**kwargs)

    store.recall = observed
    app = build_http_app(HttpTransportConfig(), store=store, readiness_db_path=store.db_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise RuntimeError("fixture_server_unavailable")
            yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", store, calls
        finally:
            server.should_exit = True
            thread.join(10)
            store.recall = original
            gc.collect()
            if thread.is_alive():
                raise RuntimeError("fixture_server_did_not_stop")


def collect_case(case, root, pack):
    home, repo = root / "home", root / "repo"
    home.mkdir(parents=True)
    repo.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
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
        capture_output=True,
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_MEMORY_BRIDGE_")}
    for key, value in {
        "HOME": home,
        "CONFIG": home / "config.toml",
        "DB_PATH": home / "bridge.db",
        "LOG_DIR": home / "logs",
        "REPOSITORY_SNAPSHOT_ROOT": home / "repository",
        "RECALL_RECEIPT_SECRET_PATH": home / "receipt-secret.json",
        "TELEMETRY_MODE": "off",
    }.items():
        env["AGENT_MEMORY_BRIDGE_" + key] = str(value)
    attempts, clients, responses = [], [], []
    original_connect, original_client = sqlite3.connect, httpx2.AsyncClient

    def guarded_connect(database, *args, **kwargs):
        if str(home / "bridge.db") in str(database):
            attempts.append("local_trap")
            raise RuntimeError("local_authority_open_attempt")
        return original_connect(database, *args, **kwargs)

    def observed_client(*args, **kwargs):
        clients.append("http_client")
        return original_client(*args, **kwargs)

    with patch.dict(os.environ, env, clear=True):
        RepositorySnapshotStore(home / "repository").bind_namespace(
            pack["namespace"], repository_identity(repo)["repository_id"]
        )
        local = MemoryStore(home / "bridge.db", log_dir=home / "logs")
        local.store(namespace=pack["namespace"], kind="memory", content=pack["local_trap"])
        del local
        gc.collect()
        trap_hash = hashlib.sha256((home / "bridge.db").read_bytes()).hexdigest()
        with remote_server(root) as (url, store, calls), socket.socket() as refused:
            if case["id"] in {"hit", "local_trap", "repeat"}:
                store.store(namespace=pack["namespace"], kind="memory", content=pack["remote_memory"])
            remote_before = durable_rows_digest(store.db_path)
            if case["id"] == "unavailable":
                # Reserve a local port without listening: no race to another service.
                refused.bind(("127.0.0.1", 0))
                url = f"http://127.0.0.1:{refused.getsockname()[1]}/mcp"
            os.environ["AGENT_MEMORY_BRIDGE_AUTHORITY_URL"] = url
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "remote-fixture",
                "cwd": str(repo),
                "namespace": "project:forged",
                "prompt": pack["negative_prompt"] if case["id"] == "skip" else pack["material_prompt"],
            }
            with (
                patch("sqlite3.connect", side_effect=guarded_connect),
                patch("agent_mem_bridge.lifecycle_http.httpx2.AsyncClient", side_effect=observed_client),
            ):
                for _ in range(2 if case["id"] == "repeat" else 1):
                    responses.append(run_hook_payload(payload))
            remote_after = durable_rows_digest(store.db_path)
        del store
        gc.collect()
    rows = [json.loads(line) for line in (home / "lifecycle/activation-evidence.jsonl").read_text().splitlines()]
    return {
        "id": case["id"],
        "observations": rows,
        "responses": responses,
        "remote_calls": calls,
        "http_clients": len(clients),
        "local_open_attempts": attempts,
        "trap_seeded": True,
        "trap_unchanged": trap_hash == hashlib.sha256((home / "bridge.db").read_bytes()).hexdigest(),
        "remote_rows_before": remote_before,
        "remote_rows_after": remote_after,
    }


def score(report):
    """Fail closed on missing evidence; never consume caller-supplied pass flags."""
    pack = json.loads(PACK.read_text())
    reasons = []
    if report.get("schema") != "amb.lifecycle-remote-evidence.v1":
        reasons.append("invalid_schema")
    if report.get("condition") != "remote_authority" or report.get("execution_kind") != "direct_hook":
        reasons.append("invalid_condition")
    if not verify_frozen() or report.get("frozen_sha256") != hashes(FROZEN):
        reasons.append("freeze_mismatch")
    if report.get("source_sha256") != hashes(SOURCES):
        reasons.append("source_mismatch")
    records = report.get("cases")
    if not isinstance(records, list):
        records = []
    if [row.get("id") for row in records if isinstance(row, dict)] != [case["id"] for case in pack["cases"]]:
        reasons.append("case_set_mismatch")
    for case in pack["cases"]:
        row = next((row for row in records if isinstance(row, dict) and row.get("id") == case["id"]), {})
        errors = []
        before = row.get("remote_rows_before")
        if not isinstance(before, str) or len(before) != 64 or row.get("remote_rows_after") != before:
            errors.append("remote_durable_rows_changed_or_missing")
        calls = row.get("remote_calls")
        expected_call = {"namespace": pack["namespace"], "kind": "memory", "limit": 3}
        if calls != [expected_call] * case["remote_recalls"]:
            errors.append("remote_calls")
        if type(row.get("http_clients")) is not int or row["http_clients"] != case["http_clients"]:
            errors.append("http_clients")
        if (
            row.get("local_open_attempts") != []
            or row.get("trap_seeded") is not True
            or row.get("trap_unchanged") is not True
        ):
            errors.append("local_trap")
        observations, responses = row.get("observations"), row.get("responses")
        count = 2 if case["id"] == "repeat" else 1
        if (
            not isinstance(observations, list)
            or len(observations) != count
            or not all(isinstance(o, dict) for o in observations)
        ):
            errors.append("observations_missing")
        else:
            for index, observation in enumerate(observations):
                state = "hit" if count == 2 and index == 0 else case["state"]
                repeated = count == 2 and index == 1
                if any(
                    observation.get(key) != value
                    for key, value in {
                        "authority_mode": "remote",
                        "namespace": pack["namespace"],
                        "resolution_status": "bound",
                        "recall_state": state,
                        "recall_invoked": state != "skipped",
                        "repeat_suppressed": repeated,
                        "ignored_caller_scope": True,
                        "adapter_loaded": True,
                        "host": "codex",
                        "hook_event_name": "UserPromptSubmit",
                        "availability": "unknown"
                        if state == "skipped"
                        else "error"
                        if state == "error"
                        else "available",
                    }.items()
                ):
                    errors.append("activation_contract")
                if state == "error" and not observation.get("error_type"):
                    errors.append("error_evidence")
                if state == "hit" and not observation.get("recalled_ids"):
                    errors.append("hit_evidence")
                if state in {"no_hit", "error", "skipped"} and observation.get("recalled_ids") != []:
                    errors.append("unexpected_memory")
        if (
            not isinstance(responses, list)
            or len(responses) != count
            or not all(isinstance(r, dict) for r in responses)
        ):
            errors.append("responses_missing")
        else:
            if any(r.get("continue") is not True for r in responses):
                errors.append("host_not_continued")
            text = json.dumps(responses)
            if "CONFLICTING-LOCAL-TRAP" in text:
                errors.append("local_trap_injected")
            if case["id"] in {"hit", "local_trap", "repeat"} and "REMOTE-AUTHORITY-CONSTRAINT" not in text:
                errors.append("remote_context_missing")
            if case["id"] in {"hit", "local_trap", "repeat"} and (
                "Untrusted governed context" not in text or "Live repository evidence wins" not in text
            ):
                errors.append("untrusted_boundary_missing")
            if case["id"] == "no_hit" and "no matching memory" not in text:
                errors.append("no_hit_context_missing")
            if case["id"] == "unavailable" and "not an empty memory result" not in text:
                errors.append("error_context_missing")
            if case["id"] in {"skip", "no_hit", "unavailable"} and "REMOTE-AUTHORITY-CONSTRAINT" in text:
                errors.append("unexpected_context")
            if case["id"] in {"skip", "repeat"} and responses[-1] != {"continue": True}:
                errors.append("skip_context_injected")
        reasons.extend(f"{case['id']}:{error}" for error in errors)
    return {"hook_contract": "FAIL" if reasons else "PASS", "native_codex_acceptance": "NOT_RUN", "reasons": reasons}


def collect():
    if not verify_frozen():
        raise ValueError("freeze_mismatch")
    pack = json.loads(PACK.read_text())
    with tempfile.TemporaryDirectory(prefix="amb-remote-contract-") as directory:
        cases = [collect_case(case, Path(directory) / case["id"], pack) for case in pack["cases"]]
    report = {
        "schema": "amb.lifecycle-remote-evidence.v1",
        "condition": "remote_authority",
        "execution_kind": "direct_hook",
        "frozen_sha256": hashes(FROZEN),
        "source_sha256": hashes(SOURCES),
        "cases": cases,
    }
    report["result"] = score(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-path", type=Path, help="New report path; existing files are not overwritten.")
    args = parser.parse_args()
    if args.report_path and args.report_path.exists():
        parser.error("report path already exists")
    report = collect()
    if args.report_path:
        with args.report_path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    print(json.dumps(report["result"], indent=2))
    return int(report["result"]["hook_contract"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
