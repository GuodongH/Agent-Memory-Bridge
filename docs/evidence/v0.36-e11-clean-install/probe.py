"""E11 clean-install probe. Runs inside a container that has the installed wheel and no coding harness."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import agent_mem_bridge

PROTOCOL_VERSION = "2026-07-28"
PUBLIC_TOOL_COUNT = 17
HARNESS_NAMES = (
    "codex",
    "opencode",
    "claude",
    "cursor",
    "gemini",
    "hermes",
    "cline",
    "antigravity",
    "windsurf",
)
SKIP_DIRS = {"proc", "sys", "dev", "run"}


def _excerpt(text: str, limit: int = 4000) -> str:
    cleaned = text.replace("\x00", "")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "\n…[truncated]"


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "args": args,
        "cwd": str(cwd) if cwd is not None else None,
        "exit_code": completed.returncode,
        "stdout": _excerpt(completed.stdout),
        "stderr": _excerpt(completed.stderr),
    }


def _meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "amb-e11-clean-install", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _request(process: subprocess.Popen[bytes], request: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        stderr = b""
        if process.stderr is not None:
            stderr = process.stderr.read()
        raise RuntimeError(f"stdio server closed before response: {stderr.decode(errors='replace')}")
    response = json.loads(line)
    if response.get("jsonrpc") != "2.0" or response.get("id") != request["id"]:
        raise RuntimeError(f"unexpected stdio response: {response}")
    return response


def _call(process: subprocess.Popen[bytes], request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _request(
        process,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "_meta": _meta()},
        },
    )


def _filesystem_matches() -> list[str]:
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk("/", followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            if name.casefold() in HARNESS_NAMES:
                matches.append(str(Path(dirpath) / name))
        if len(matches) > 50:
            break
    return matches


def _prepare_repo(repo: Path, env: dict[str, str]) -> None:
    repo.mkdir()
    git_env = {**env, "GIT_AUTHOR_NAME": "E11", "GIT_AUTHOR_EMAIL": "e11@example.invalid"}
    git_env["GIT_COMMITTER_NAME"] = git_env["GIT_AUTHOR_NAME"]
    git_env["GIT_COMMITTER_EMAIL"] = git_env["GIT_AUTHOR_EMAIL"]
    commands = (
        ["git", "init"],
        ["git", "config", "user.name", "E11"],
        ["git", "config", "user.email", "e11@example.invalid"],
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "initial"],
    )
    (repo / "README.md").write_text("e11 clean install\n", encoding="utf-8")
    for command in commands:
        completed = subprocess.run(command, cwd=repo, env=git_env, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"{command} failed: {completed.stderr}")


def _stdio(env: dict[str, str], runtime: Path) -> dict[str, Any]:
    runtime.mkdir()
    child_env = {
        **env,
        "AGENT_MEMORY_BRIDGE_HOME": str(runtime),
        "AGENT_MEMORY_BRIDGE_DB_PATH": str(runtime / "bridge.db"),
        "AGENT_MEMORY_BRIDGE_LOG_DIR": str(runtime / "logs"),
    }
    namespace = "project:e11-clean"
    decision = "record_type: decision\nclaim: Merge only after CI is green.\nreason: Protect the release branch."
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_mem_bridge"],
        cwd=runtime,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        discover = _request(
            process,
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": _meta()}},
        )
        tools = _request(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": _meta()}},
        )
        stored = _call(
            process,
            3,
            "store",
            {"namespace": namespace, "kind": "memory", "title": "Release decision", "content": decision},
        )
        tool_names = [tool["name"] for tool in tools["result"]["tools"]]
        stored_payload = stored["result"]["structuredContent"]
        store_id = stored_payload.get("id") or stored_payload.get("memory_id")
        if len(tool_names) != PUBLIC_TOOL_COUNT or not store_id:
            raise RuntimeError(f"stdio discovery/store failed: tools={len(tool_names)} id={store_id}")
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=20)

    recall = subprocess.Popen(
        [sys.executable, "-m", "agent_mem_bridge"],
        cwd=runtime,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        recalled = _call(
            recall,
            4,
            "recall",
            {"namespace": namespace, "kind": "memory", "query": "What is required before merge?", "limit": 5},
        )
        items = recalled["result"]["structuredContent"]["items"]
        recall_hit = any("Merge only after CI is green" in item.get("content", "") for item in items)
        if not recall_hit:
            raise RuntimeError("recall did not return the stored decision")
    finally:
        if recall.stdin is not None:
            recall.stdin.close()
        recall.wait(timeout=20)

    return {
        "protocol_supported_versions": discover["result"].get("supportedVersions"),
        "tool_count": len(tool_names),
        "tool_names": tool_names,
        "store_id_present": bool(store_id),
        "recall_hit": recall_hit,
        "server_exit_codes": [process.returncode, recall.returncode],
    }


def main() -> int:
    home = Path("/tmp/e11-home")
    repo = Path("/tmp/e11-clean")
    runtime = home / "bridge-runtime"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    for name in list(env):
        if name.startswith("AGENT_MEMORY_BRIDGE") or name in {"CODEX_HOME"}:
            del env[name]

    installed = Path(agent_mem_bridge.__file__).resolve()
    paths_text = (installed.parent / "paths.py").read_text(encoding="utf-8")
    which = {name: shutil.which(name, path=env.get("PATH")) for name in HARNESS_NAMES}
    matches = _filesystem_matches()
    version = importlib.metadata.version("agent-memory-bridge")
    _prepare_repo(repo, env)
    help_step = _run([sys.executable, "-m", "agent_mem_bridge", "--help"], env=env)
    absent_commands = [
        _run([sys.executable, "-m", "agent_mem_bridge", command], env=env) for command in ("setup", "config")
    ]
    init_step = _run([sys.executable, "-m", "agent_mem_bridge", "project", "init", "--yes", "."], cwd=repo, env=env)
    resolve_step = _run([sys.executable, "-m", "agent_mem_bridge", "project", "resolve", "."], cwd=repo, env=env)
    stdio = _stdio(env, runtime)
    resolve_payload = json.loads(resolve_step["stdout"])
    legacy_home = home / ".codex" / "mem-bridge"
    neutral_home = home / ".local" / "share" / "agent-memory-bridge"
    failures: list[str] = []
    if version != "0.35.0":
        failures.append(f"version={version}")
    if "site-packages" not in str(installed):
        failures.append(f"not an installed distribution: {installed}")
    if "CODEX_HOME" in paths_text or "mem-bridge" in paths_text:
        failures.append("installed paths.py still names the legacy home")
    if any(which.values()) or matches:
        failures.append("named harness present")
    if help_step["exit_code"] != 0 or "project" not in help_step["stdout"]:
        failures.append("help failed")
    if any(step["exit_code"] != 2 for step in absent_commands):
        failures.append("removed commands did not exit 2")
    if init_step["exit_code"] != 0:
        failures.append("project init failed")
    if resolve_step["exit_code"] != 0 or resolve_payload.get("status") != "bound":
        failures.append("project resolve failed")
    if resolve_payload.get("namespace") != "project:e11-clean":
        failures.append(f"namespace={resolve_payload.get('namespace')}")
    if stdio["tool_count"] != PUBLIC_TOOL_COUNT or not stdio["recall_hit"]:
        failures.append("stdio store/recall failed")
    if any(code != 0 for code in stdio["server_exit_codes"]):
        failures.append(f"server exits={stdio['server_exit_codes']}")
    if legacy_home.exists():
        failures.append("legacy home was created")

    receipt = {
        "schema": "amb.v0.36.e11-clean-install.v1",
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "image": os.environ.get("AMB_E11_IMAGE", "python:3.12-slim"),
        "image_id": os.environ.get("AMB_E11_IMAGE_ID", ""),
        "installed_version": version,
        "installed_module": str(installed),
        "installed_paths_sha256": hashlib.sha256(paths_text.encode("utf-8")).hexdigest(),
        "neutral_home_created": neutral_home.exists(),
        "legacy_home_created": legacy_home.exists(),
        "harness_probe": {
            "names": list(HARNESS_NAMES),
            "which": which,
            "filesystem_matches": matches,
            "skipped_walk_directories": sorted(SKIP_DIRS),
        },
        "help": help_step,
        "removed_commands": absent_commands,
        "project_init": init_step,
        "project_resolve": resolve_step,
        "stdio": stdio,
    }
    destination = Path(os.environ.get("AMB_E11_RECEIPT", "/out/receipt.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "failures": failures, "receipt": str(destination)}))
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # The container log should keep the failure instead of a bare traceback-only exit.
        print(f"E11 probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
