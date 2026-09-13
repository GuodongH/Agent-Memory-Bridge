"""Independent-machine acceptance client; installs only the standard MCP SDK.

Run store on machine A, disconnect it, then recall on machine B. This script has
no AMB storage dependency and never starts a server or opens a local database.
Only acceptance markers may be written; do not target production during rehearsal.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

EXPECTED_DIGEST = "24c5c52321d61b4b6f647c0d74e2d8304ca68716c403e08a274e9badfd8dc9f8"
DECISION = "Merge pull requests only after CI passes on the target branch."
RATIONALE = "Broken main blocked two releases, so passing branch-specific CI is required."


def connection_unavailable(exc: BaseException) -> bool:
    # An SSH tunnel can accept TCP and then close it when the upstream authority
    # is down. That is a read/protocol transport failure, not ConnectError.
    if isinstance(exc, httpx2.TransportError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(connection_unavailable(child) for child in exc.exceptions)
    return exc.__cause__ is not None and connection_unavailable(exc.__cause__)


async def evaluate(args: argparse.Namespace) -> dict[str, object]:
    url = urlsplit(args.url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("invalid acceptance endpoint")
    token = args.token_file.read_text(encoding="ascii").strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("invalid acceptance credential")
    namespace = f"project:remote-first-win-{args.run_id}"
    report: dict[str, object] = {
        "ok": False,
        "action": args.action,
        "machine_os": platform.system(),
        "mcp_version": importlib.metadata.version("mcp"),
        "local_storage_implementation": False,
    }
    try:
        async with asyncio.timeout(20):
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=10, trust_env=False, follow_redirects=False
            ) as http:
                async with Client(streamable_http_client(args.url, http_client=http)) as client:
                    listed = await client.list_tools()
                    snapshot = sorted(
                        (
                            {"name": tool.name, "inputSchema": tool.input_schema, "outputSchema": tool.output_schema}
                            for tool in listed.tools
                        ),
                        key=lambda item: item["name"],
                    )
                    digest = hashlib.sha256(
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                    ).hexdigest()
                    report.update(tool_count=len(snapshot), tool_schema_sha256=digest)
                    if len(snapshot) != 17 or digest != EXPECTED_DIGEST:
                        return report
                    if args.action in {"store", "unavailable"}:
                        result = await client.call_tool(
                            "store",
                            {
                                "namespace": namespace,
                                "kind": "memory",
                                "title": "Project merge policy",
                                "content": f"Decision: {DECISION}\nRationale: {RATIONALE}",
                            },
                        )
                        report["store_succeeded"] = not result.is_error
                        report["ok"] = args.action == "store" and not result.is_error
                    else:
                        # Deliberately use a normal project question, not the
                        # unique run identifier, to exercise retrieval.
                        result = await client.call_tool(
                            "recall",
                            {
                                "namespace": namespace + ("-unrelated" if args.action == "absent" else ""),
                                "query": "What is required before we merge a pull request?",
                                "kind": "memory",
                            },
                        )
                        items = (result.structured_content or {}).get("items", [])
                        report["decision_and_rationale_retrieved"] = any(
                            DECISION in item.get("content", "") and RATIONALE in item.get("content", "")
                            for item in items
                        )
                        report["ok"] = not result.is_error and (
                            not items if args.action == "absent" else report["decision_and_rationale_retrieved"]
                        )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["transport_unavailable"] = connection_unavailable(exc)
        # Only the deliberate outage probe regards inability to reach the
        # authority as success. No exception text/headers are serialized.
        report["ok"] = args.action == "unavailable" and report["transport_unavailable"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--action", choices=("store", "recall", "absent", "unavailable"), required=True)
    args = parser.parse_args()
    if not args.run_id.isalnum():
        parser.error("run-id must contain only letters and digits")
    try:
        report = asyncio.run(evaluate(args))
    except Exception as exc:
        report = {"ok": False, "error_type": type(exc).__name__}
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
