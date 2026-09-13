"""Run a self-contained Streamable HTTP candidate acceptance check.

This is a local test harness: it creates only the caller-selected fresh runtime
directory, starts one child authority process, and terminates only that child.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TOOL_ORDER = (
    "store",
    "recall",
    "browse",
    "stats",
    "forget",
    "feedback",
    "promote",
    "annotate",
    "revise",
    "export",
    "begin_run",
    "record_run_event",
    "get_run",
    "complete_run",
    "claim_signal",
    "extend_signal_lease",
    "ack_signal",
)
EXPORT_NAMESPACE = "project:http-deployment-export"
PUBLIC_TOOL_DIGEST = "24c5c52321d61b4b6f647c0d74e2d8304ca68716c403e08a274e9badfd8dc9f8"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start and verify one isolated AMB HTTP authority candidate.")
    parser.add_argument(
        "--server-python", type=Path, required=True, help="Python interpreter containing the candidate."
    )
    parser.add_argument("--runtime-dir", type=Path, required=True, help="New, nonexistent isolated AMB home to create.")
    parser.add_argument(
        "--project-root", type=Path, default=PROJECT_ROOT, help="Candidate source root used as child cwd."
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="Overall per-probe timeout in seconds.")
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if not args.server_python.is_file():
        raise ValueError("server Python interpreter is unavailable")
    if not args.project_root.is_dir():
        raise ValueError("project root is unavailable")
    if args.runtime_dir.exists():
        raise ValueError("runtime directory must not already exist")
    if not 1 <= args.timeout <= 300:
        raise ValueError("timeout must be between 1 and 300 seconds")


def _runtime_env(runtime_dir: Path) -> dict[str, str]:
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("AGENT_MEMORY_BRIDGE_")},
        "AGENT_MEMORY_BRIDGE_HOME": str(runtime_dir),
        "AGENT_MEMORY_BRIDGE_CONFIG": str(runtime_dir / "config.toml"),
        "AGENT_MEMORY_BRIDGE_DB_PATH": str(runtime_dir / "bridge.db"),
        "AGENT_MEMORY_BRIDGE_LOG_DIR": str(runtime_dir / "logs"),
        "AGENT_MEMORY_BRIDGE_TELEMETRY_MODE": "off",
        "AGENT_MEMORY_BRIDGE_RETRIEVAL_MODE": "lexical",
        "AGENT_MEMORY_BRIDGE_EMBEDDING_PROVIDER": "hash",
        "AGENT_MEMORY_BRIDGE_CLASSIFIER_MODE": "off",
    }


def _contract_digest(tools: list[Any]) -> str:
    snapshot = sorted(
        (
            {
                "name": tool.name,
                "inputSchema": getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None),
                "outputSchema": getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None),
            }
            for tool in tools
        ),
        key=lambda item: item["name"],
    )
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _exact_recall(payload: Any, marker: str) -> bool:
    return isinstance(payload, dict) and any(item.get("content") == marker for item in payload.get("items", []))


def _pick_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _seed_export_corpus(
    server_python: Path, project_root: Path, env: dict[str, str], timeout: float
) -> dict[str, dict[str, Any]]:
    seed_code = """
import hashlib
import json
from agent_mem_bridge.storage import MemoryStore

store = MemoryStore.from_env()
for index in range(500):
    store.store(
        namespace='project:http-deployment-export',
        content=f'HTTP deployment export fixture {index:03d}: ' + ('x' * 32768),
        kind='memory',
    )
for limit in (1, 500):
    exported = store.export(namespace='project:http-deployment-export', format='json', limit=limit)
    content = exported['content'].encode('utf-8')
    print(json.dumps({'limit': limit, 'count': exported['count'], 'content_sha256': hashlib.sha256(content).hexdigest()}))
"""
    completed = subprocess.run(
        [str(server_python), "-c", seed_code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("candidate corpus seed failed")
    reports: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("candidate corpus seed returned invalid output") from exc
        if not isinstance(item, dict) or item.get("limit") not in {1, 500}:
            raise RuntimeError("candidate corpus seed returned incomplete output")
        reports[str(item["limit"])] = item
    if set(reports) != {"1", "500"} or reports["1"].get("count") != 1 or reports["500"].get("count") != 500:
        raise RuntimeError("candidate corpus seed did not produce exact exports")
    return reports


def _start_server(server_python: Path, project_root: Path, env: dict[str, str], port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(server_python),
            "-m",
            "agent_mem_bridge",
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=project_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_ready(url: str, process: subprocess.Popen[bytes], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("candidate HTTP server exited before readiness")
        try:
            with opener.open(f"{url}/readyz", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and isinstance(payload, dict) and payload.get("ready") is True:
                return {
                    key: payload[key]
                    for key in ("status", "ready", "version", "schema_version", "tool_count", "tool_schema_sha256")
                    if key in payload
                }
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("candidate HTTP server did not become ready")


async def _mcp2_parity(url: str, marker: str, timeout: float) -> dict[str, Any]:
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http), mode="auto") as client:
            protocol_version = client.protocol_version
            listed = await asyncio.wait_for(client.list_tools(cache_mode="bypass"), timeout=timeout)
            stored = await asyncio.wait_for(
                client.call_tool(
                    "store",
                    {
                        "namespace": "project:http-deployment-parity",
                        "content": marker,
                        "kind": "memory",
                    },
                ),
                timeout=timeout,
            )
            recalled = await asyncio.wait_for(
                client.call_tool(
                    "recall",
                    {
                        "namespace": "project:http-deployment-parity",
                        "query": marker,
                        "kind": "memory",
                        "limit": 5,
                    },
                ),
                timeout=timeout,
            )
    payload = getattr(recalled, "structured_content", None)
    return {
        "protocol_version": protocol_version,
        "tool_surface": tuple(tool.name for tool in listed.tools) == PUBLIC_TOOL_ORDER,
        "tool_schema_sha256": _contract_digest(listed.tools),
        "store": not stored.is_error,
        "recall": not recalled.is_error and _exact_recall(payload, marker),
    }


async def _mcp1_parity(url: str, marker: str, timeout: float) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(f"{url}/mcp", timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as client:
            initialized = await asyncio.wait_for(client.initialize(), timeout=timeout)
            listed = await asyncio.wait_for(client.list_tools(), timeout=timeout)
            stored = await asyncio.wait_for(
                client.call_tool(
                    "store",
                    arguments={
                        "namespace": "project:http-deployment-parity",
                        "content": marker,
                        "kind": "memory",
                    },
                ),
                timeout=timeout,
            )
            recalled = await asyncio.wait_for(
                client.call_tool(
                    "recall",
                    arguments={
                        "namespace": "project:http-deployment-parity",
                        "query": marker,
                        "kind": "memory",
                        "limit": 5,
                    },
                ),
                timeout=timeout,
            )
    payload = getattr(recalled, "structured_content", None) or getattr(recalled, "structuredContent", None)
    protocol_version = getattr(initialized, "protocol_version", None) or getattr(initialized, "protocolVersion", None)
    return {
        "protocol_version": protocol_version,
        "tool_surface": tuple(tool.name for tool in listed.tools) == PUBLIC_TOOL_ORDER,
        "tool_schema_sha256": _contract_digest(listed.tools),
        "store": not stored.isError,
        "recall": not recalled.isError and _exact_recall(payload, marker),
    }


def _run_export_acceptance(url: str, seeded: dict[str, dict[str, Any]], timeout: float) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_http_acceptance.py"),
        "--url",
        f"{url}/mcp",
        "--namespace",
        EXPORT_NAMESPACE,
        "--small-expected-count",
        str(seeded["1"]["count"]),
        "--large-expected-count",
        str(seeded["500"]["count"]),
        "--small-content-sha256",
        str(seeded["1"]["content_sha256"]),
        "--large-content-sha256",
        str(seeded["500"]["content_sha256"]),
        "--timeout",
        str(timeout),
    ]
    completed = subprocess.run(
        command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout * 3, check=False
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("HTTP export acceptance returned invalid output") from exc
    if completed.returncode != 0 or not isinstance(report, dict):
        raise RuntimeError("HTTP export acceptance failed")
    return report


def run_check(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    args.runtime_dir.mkdir(mode=0o700, parents=True)
    (args.runtime_dir / "config.toml").touch(mode=0o600)
    env = _runtime_env(args.runtime_dir)
    seeded = _seed_export_corpus(args.server_python, args.project_root, env, args.timeout)
    port = _pick_loopback_port()
    url = f"http://127.0.0.1:{port}"
    process = _start_server(args.server_python, args.project_root, env, port)
    try:
        readiness = _wait_for_ready(url, process, args.timeout)
        marker = f"http-parity-{uuid.uuid4().hex}"
        sdk_version = importlib.metadata.version("mcp")
        parity = asyncio.run(
            _mcp1_parity(url, marker, args.timeout)
            if sdk_version.startswith("1.")
            else _mcp2_parity(url, marker, args.timeout)
        )
        exports = _run_export_acceptance(url, seeded, args.timeout)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    expected_protocol = "2025-11-25" if sdk_version.startswith("1.") else "2026-07-28"
    parity_ok = (
        parity["protocol_version"] == expected_protocol
        and bool(parity["tool_surface"])
        and parity["tool_schema_sha256"] == PUBLIC_TOOL_DIGEST
        and bool(parity["store"])
        and bool(parity["recall"])
    )
    return {
        "ok": bool(exports.get("ok")) and parity_ok,
        "transport": "streamable-http",
        "mcp_sdk_version": sdk_version,
        "readiness": readiness,
        "parity": parity,
        "exports": exports,
        "runtime_dir": str(args.runtime_dir),
        "writes_performed": True,
        "local_fallback": False,
    }


def main() -> int:
    args = _parse_args()
    try:
        report = run_check(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        report = {
            "ok": False,
            "transport": "streamable-http",
            "error_type": type(exc).__name__,
            "error": "isolated HTTP deployment check failed",
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
