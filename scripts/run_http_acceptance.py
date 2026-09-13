"""Exercise exact small and large MCP export responses over Streamable HTTP.

Run this from a client environment. It never creates a local AMB database and
prints only digests and counts, never exported memory contents or bearer tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

Mode = Literal["modern", "legacy"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify small and large AMB exports over Streamable HTTP.")
    parser.add_argument("--url", required=True, help="Absolute MCP Streamable HTTP endpoint URL.")
    parser.add_argument("--token-file", type=Path, default=None, help="Optional private deployment bearer token file.")
    parser.add_argument("--namespace", required=True, help="Existing namespace to export; this script never writes.")
    parser.add_argument("--mode", choices=("modern", "legacy", "all"), default="all")
    parser.add_argument("--small-limit", type=int, default=1)
    parser.add_argument("--large-limit", type=int, default=500)
    parser.add_argument("--small-expected-count", type=int, required=True)
    parser.add_argument("--large-expected-count", type=int, required=True)
    parser.add_argument("--small-content-sha256", default=None)
    parser.add_argument("--large-content-sha256", default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    try:
        parsed = urlsplit(args.url)
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint must be an absolute HTTP(S) URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 for char in args.url)
        or (port is not None and not 0 < port < 65536)
    ):
        raise ValueError("endpoint must be an absolute HTTP(S) URL")
    if not 0 < args.timeout <= 300:
        raise ValueError("timeout must be between 0 and 300 seconds")
    for name in ("small_limit", "large_limit", "small_expected_count", "large_expected_count"):
        if not 0 <= getattr(args, name) <= 500:
            raise ValueError(f"{name.replace('_', ' ')} must be between 0 and 500")
    if not 1 <= args.small_limit <= args.large_limit <= 500:
        raise ValueError("export limits must satisfy 1 <= small <= large <= 500")
    for digest in (args.small_content_sha256, args.large_content_sha256):
        if digest is not None and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("expected content digest must be lowercase SHA-256")


async def _run_mcp2(
    url: str,
    headers: dict[str, str],
    mode: Mode,
    namespace: str,
    small_limit: int,
    large_limit: int,
    timeout: float,
) -> dict[str, Any]:
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    client_mode: Literal["auto", "legacy"] = "auto" if mode == "modern" else "legacy"
    async with httpx2.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=client_mode) as client:
            small = await asyncio.wait_for(
                client.call_tool("export", {"namespace": namespace, "format": "json", "limit": small_limit}),
                timeout=timeout,
            )
            large = await asyncio.wait_for(
                client.call_tool("export", {"namespace": namespace, "format": "json", "limit": large_limit}),
                timeout=timeout,
            )
            return {
                "protocol_version": client.protocol_version,
                "small": _result_payload(small),
                "large": _result_payload(large),
            }


async def _run_mcp1(
    url: str,
    headers: dict[str, str],
    namespace: str,
    small_limit: int,
    large_limit: int,
    timeout: float,
) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers, timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as client:
            initialized = await asyncio.wait_for(client.initialize(), timeout=timeout)
            small = await asyncio.wait_for(
                client.call_tool("export", arguments={"namespace": namespace, "format": "json", "limit": small_limit}),
                timeout=timeout,
            )
            large = await asyncio.wait_for(
                client.call_tool("export", arguments={"namespace": namespace, "format": "json", "limit": large_limit}),
                timeout=timeout,
            )
            protocol_version = getattr(initialized, "protocol_version", None) or getattr(
                initialized, "protocolVersion", None
            )
            return {
                "protocol_version": protocol_version,
                "small": _result_payload(small),
                "large": _result_payload(large),
            }


def _result_payload(result: Any) -> dict[str, Any]:
    if bool(getattr(result, "is_error", False) or getattr(result, "isError", False)):
        raise RuntimeError("export tool returned an error")
    payload = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if not isinstance(payload, dict):
        raise RuntimeError("export tool returned no structured result")
    content = payload.get("content")
    count = payload.get("count")
    if not isinstance(content, str) or not isinstance(count, int):
        raise RuntimeError("export tool returned an invalid structured result")
    return {
        "count": count,
        "content_bytes": len(content.encode("utf-8")),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _evaluate_export(observed: dict[str, Any], *, expected_count: int, expected_digest: str | None) -> dict[str, Any]:
    digest_match = expected_digest is None or observed["content_sha256"] == expected_digest
    return {
        **observed,
        "expected_count": expected_count,
        "expected_content_sha256": expected_digest,
        "exact": observed["count"] == expected_count and digest_match,
    }


async def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    headers: dict[str, str] = {}
    if args.token_file is not None:
        headers["Authorization"] = f"Bearer {_read_token_file(args.token_file)}"
    sdk_version = importlib.metadata.version("mcp")
    modes: tuple[Mode, ...]
    if sdk_version.startswith("1."):
        if args.mode == "modern":
            raise ValueError("MCP 1.x client can exercise only the legacy HTTP path")
        modes = ("legacy",)
    elif args.mode == "all":
        modes = ("modern", "legacy")
    else:
        modes = (args.mode,)

    reports: dict[str, Any] = {}
    for mode in modes:
        try:
            observed = await (
                _run_mcp1(
                    args.url,
                    headers,
                    args.namespace,
                    args.small_limit,
                    args.large_limit,
                    args.timeout,
                )
                if sdk_version.startswith("1.")
                else _run_mcp2(
                    args.url,
                    headers,
                    mode,
                    args.namespace,
                    args.small_limit,
                    args.large_limit,
                    args.timeout,
                )
            )
            small = _evaluate_export(
                observed["small"],
                expected_count=args.small_expected_count,
                expected_digest=args.small_content_sha256,
            )
            large = _evaluate_export(
                observed["large"],
                expected_count=args.large_expected_count,
                expected_digest=args.large_content_sha256,
            )
            expected_protocol = "2026-07-28" if mode == "modern" else "2025-11-25"
            reports[mode] = {
                "ok": observed["protocol_version"] == expected_protocol and small["exact"] and large["exact"],
                "protocol_version": observed["protocol_version"],
                "small": small,
                "large": large,
            }
        except Exception as exc:
            reports[mode] = {"ok": False, "error_type": type(exc).__name__, "error": "HTTP export acceptance failed"}
    parsed = urlsplit(args.url)
    return {
        "ok": all(report["ok"] for report in reports.values()),
        "transport": "streamable-http",
        "endpoint": {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port},
        "mcp_sdk_version": sdk_version,
        "writes_performed": False,
        "local_fallback": False,
        "reports": reports,
    }


def _read_token_file(path: Path) -> str:
    from agent_mem_bridge.deployment_config import read_token_file

    return read_token_file(path)


def main() -> int:
    args = _parse_args()
    try:
        report = asyncio.run(run_acceptance(args))
    except (OSError, ValueError) as exc:
        report = {
            "ok": False,
            "transport": "streamable-http",
            "error_type": type(exc).__name__,
            "error": "invalid HTTP export acceptance configuration",
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
