"""Opt-in live Codex Stop proof. Never uses the operator's AMB authority.

Only the controlled final message and lifecycle metadata are retained; model
reasoning and full event streams are not written by this collector.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_mem_bridge.promotion import parse_structured_record  # noqa: E402
from agent_mem_bridge.repository_snapshot_store import RepositorySnapshotStore, repository_identity  # noqa: E402
from agent_mem_bridge.storage import MemoryStore  # noqa: E402

NAMESPACE = "project:codex-stop-proof"
EXPLICIT = "Explicit user storage remains visible beside lifecycle capture."
ARTIFACT = {
    "schema": "memory.visible_artifact.v1",
    "artifact_id": "decision-1",
    "artifact_class": "decision",
    "claim": "Keep write-side capture in the hidden review lane until a person reviews it.",
    "reason": "This constraint prevents unreviewed proposals from becoming trusted project guidance.",
    "scope": "project",
}


def host_gate(events: list[dict], receipts: list[dict], returncode: int, final: str, positive: bool) -> bool:
    """A zero-row database without a completed callback is not a passing control."""
    threads = [row.get("thread_id") for row in events if row.get("type") == "thread.started"]
    terminals = [row for row in events if row.get("type") == "turn.completed"]
    messages = [
        row["item"].get("text")
        for row in events
        if row.get("type") == "item.completed" and row.get("item", {}).get("type") == "agent_message"
    ]
    tools = [
        row
        for row in events
        if row.get("type") in {"item.started", "item.completed"}
        and row.get("item", {}).get("type") in {"command_execution", "mcp_tool_call", "file_change", "web_search"}
    ]
    if returncode != 0 or len(threads) != 1 or len(terminals) != 1 or len(receipts) != 1 or tools:
        return False
    receipt = receipts[0]
    return bool(
        final
        and messages == [final]
        and receipt.get("session_id") == threads[0]
        and receipt.get("turn_id")
        and receipt.get("hook_event_name") == "Stop"
        and receipt.get("writes") == int(positive)
        and receipt.get("disposition") == ("captured" if positive else "no_capture")
        and receipt.get("reason") == ("capture_policy_evaluated" if positive else "no_bounded_artifact")
        and receipt.get("automatic_promotion") is False
        and (not positive or receipt.get("artifact_sha256") == hashlib.sha256(final.encode()).hexdigest())
    )


def collect_lane(args: argparse.Namespace, lane: str) -> dict:
    work = args.output.resolve() / lane
    repo, home = work / "repo", work / "bridge"
    repo.mkdir(parents=True)
    home.mkdir()
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
    (home / "config.toml").write_text("", encoding="utf-8")
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")
    seed = store.store(namespace=NAMESPACE, kind="memory", content=EXPLICIT)
    del store
    gc.collect()
    RepositorySnapshotStore(home / "repository").bind_namespace(NAMESPACE, repository_identity(repo)["repository_id"])
    env = os.environ.copy()
    env.pop("AGENT_MEMORY_BRIDGE_AUTHORITY_URL", None)
    for key, value in {
        "HOME": home,
        "CONFIG": home / "config.toml",
        "DB_PATH": home / "bridge.db",
        "LOG_DIR": home / "logs",
        "REPOSITORY_SNAPSHOT_ROOT": home / "repository",
        "RECALL_RECEIPT_SECRET_PATH": home / "receipt-secret.json",
    }.items():
        env["AGENT_MEMORY_BRIDGE_" + key] = str(value)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    command_args = [sys.executable, "-m", "agent_mem_bridge", "lifecycle-hook"]
    command = subprocess.list2cmdline(command_args) if os.name == "nt" else shlex.join(command_args)
    hook_config = 'hooks.Stop=[{hooks=[{type="command",command=' + json.dumps(command) + ",timeout=10}]}]"
    positive = lane == "positive"
    expected = json.dumps(ARTIFACT) if positive else "No reusable decision or handoff fact was produced in this turn."
    argv = [
        args.codex,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--json",
        "-s",
        "read-only",
        "-m",
        args.model,
    ]
    for config in args.config:
        argv.extend(["-c", config])
    for config in ["features.hooks=true", "features.plugins=false", "project_doc_max_bytes=0", hook_config]:
        argv.extend(["-c", config])
    # Invocation-scoped approval of this reviewed fixture hook, never persisted trust.
    argv.extend(
        [
            "--dangerously-bypass-hook-trust",
            "-C",
            str(repo),
            "Do not use tools. Reply with exactly this text, no markdown fences or commentary:\n" + expected,
        ]
    )
    try:
        run = subprocess.run(
            argv, cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        return {"lane": lane, "passed": False, "reason": "host_timeout"}
    events = []
    for line in run.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    messages = [
        row["item"]["text"]
        for row in events
        if row.get("type") == "item.completed" and row.get("item", {}).get("type") == "agent_message"
    ]
    final = messages[-1] if messages else ""
    receipt_path = home / "lifecycle" / "capture-evidence.jsonl"
    receipts = [json.loads(line) for line in receipt_path.read_text().splitlines()] if receipt_path.exists() else []
    with sqlite3.connect(home / "bridge.db") as conn:
        rows = conn.execute("SELECT id, content, is_learning_candidate FROM memories").fetchall()
    candidates = [row for row in rows if row[2]]
    durable = [row for row in rows if not row[2]]
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")
    visible = store.browse(namespace=NAMESPACE, kind="memory", limit=10)["items"]
    recalled = store.recall(namespace=NAMESPACE, query="hidden review lane", limit=10)["items"]
    exported = store.export(namespace=NAMESPACE)["content"]
    passed = (
        host_gate(events, receipts, run.returncode, final, positive)
        and final == expected
        and len(candidates) == int(positive)
        and durable == [(seed["id"], EXPLICIT, 0)]
        and {row["id"] for row in visible} == {seed["id"]}
        and not recalled
        and ARTIFACT["claim"] not in exported
    )
    if positive:
        passed = bool(
            passed
            and len(candidates) == 1
            and parse_structured_record(candidates[0][1])["candidate_status"] == "needs_review"
            and receipts[0]["record_ids"] == [candidates[0][0]]
            and f"codex-stop:sha256:{hashlib.sha256(final.encode()).hexdigest()}" in candidates[0][1]
        )
    # Retain only the exact controlled visible fixture, not arbitrary model output.
    if final == expected:
        (work / "visible-artifact.txt").write_text(final, encoding="utf-8")
    del store
    gc.collect()
    return {
        "lane": lane,
        "passed": bool(passed),
        "exit_code": run.returncode,
        "terminal_completed": any(row.get("type") == "turn.completed" for row in events),
        "callback_count": len(receipts),
        "candidate_count": len(candidates),
        "durable_count": len(durable),
        "visible_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "receipts": receipts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private evidence directory; must not exist.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("-c", "--config", action="append", default=[], help="Invocation-only model/provider settings.")
    parser.add_argument(
        "--reviewed-fixture-hook",
        action="store_true",
        required=True,
        help="Approve only this collector's invocation-scoped fixture hook.",
    )
    args = parser.parse_args()
    for override in args.config:
        key, separator, _value = override.partition("=")
        key = key.strip()
        provider_field = key.split(".")
        allowed_provider = (
            len(provider_field) == 3
            and provider_field[0] == "model_providers"
            and provider_field[2] in {"name", "base_url", "env_key", "wire_api", "requires_openai_auth"}
        )
        if not separator or not (
            key in {"model_provider", "model_catalog_json", "model_reasoning_effort"} or allowed_provider
        ):
            parser.error("Only model/provider overrides are allowed; hooks, MCP, and policy overrides are not.")
    # User and plugin configuration are disabled. Refuse known ambient hook files.
    for path in (
        Path.home() / ".codex/hooks.json",
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "hooks.json",
        Path("/etc/codex/hooks.json"),
        Path("/etc/codex/requirements.toml"),
    ):
        if path.exists():
            parser.error("Ambient hook/policy file exists; use a separately reviewed isolated host.")
    args.output.mkdir(parents=True, mode=0o700)
    lanes = [collect_lane(args, lane) for lane in ("positive", "negative")]
    report = {
        "schema": "amb.codex-stop-host-proof.v1",
        "passed": all(lane["passed"] for lane in lanes),
        "codex_version": subprocess.check_output([args.codex, "--version"], text=True).strip(),
        "model": args.model,
        "hook_loading": "invocation_inline",
        "lanes": lanes,
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                ROOT / "src/agent_mem_bridge/lifecycle_activation.py",
                ROOT / "src/agent_mem_bridge/write_side_capture.py",
                Path(__file__),
            ]
        },
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
