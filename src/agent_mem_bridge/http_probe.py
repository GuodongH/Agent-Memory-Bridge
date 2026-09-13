"""Read-only deployment checks using the supported MCP HTTP client.

These probes never create a local MemoryStore, retry a write, or fall back to
stdio. Deployment acceptance exercises writes separately in an isolated home.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from .mcp_boundary import (
    MCP_LEGACY_TEST_VERSION,
    MCP_MODERN_VERSION,
    PUBLIC_TOOL_ORDER,
    PUBLIC_TOOL_SCHEMA_SHA256,
)
from .schema import CURRENT_SCHEMA_VERSION


def validate_http_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError(
            "HTTP endpoint must be an absolute HTTP(S) URL without credentials or query parameters"
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 for char in url)
        or (port is not None and not 0 < port < 65536)
    ):
        raise ValueError("HTTP endpoint must be an absolute HTTP(S) URL without credentials or query parameters")
    return url


def tool_schema_digest(tools: list[Any]) -> str:
    snapshot = sorted(
        ({"name": tool.name, "inputSchema": tool.input_schema, "outputSchema": tool.output_schema} for tool in tools),
        key=lambda item: item["name"],
    )
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def run_http_probe(
    url: str,
    *,
    token_file: Path | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Check readiness and read-only modern/legacy MCP list/call round trips."""
    try:
        validate_http_url(url)
        if not 0 < timeout <= 300:
            raise ValueError("HTTP probe timeout must be between 0 and 300 seconds")
        headers: dict[str, str] = {}
        if token_file is not None:
            from .deployment_config import read_token_file

            headers["Authorization"] = f"Bearer {read_token_file(token_file)}"
    except (OSError, ValueError):
        return {
            "ok": False,
            "transport": "streamable-http",
            "checks": [_check("http_configuration", False, "Invalid endpoint, timeout, or token file.")],
        }

    checks: list[dict[str, Any]] = []
    parsed = urlsplit(url)
    readiness_url = f"{parsed.scheme}://{parsed.netloc}/readyz"
    readiness: dict[str, Any] = {}
    try:
        async with asyncio.timeout(timeout):
            async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as http:
                response = await http.get(readiness_url, headers=headers)
                payload = response.json()
                if isinstance(payload, dict):
                    readiness = {
                        key: payload[key]
                        for key in ("status", "ready", "version", "schema_version", "tool_count", "tool_schema_sha256")
                        if key in payload
                    }
                ready = (
                    response.status_code == 200
                    and readiness.get("ready") is True
                    and readiness.get("schema_version") == CURRENT_SCHEMA_VERSION
                    and readiness.get("tool_count") == len(PUBLIC_TOOL_ORDER)
                    and readiness.get("tool_schema_sha256") == PUBLIC_TOOL_SCHEMA_SHA256
                )
                checks.append(
                    _check("http_readiness", ready, "Readiness endpoint accepted." if ready else "Not ready.")
                )
    except Exception as exc:
        checks.append(_check("http_readiness", False, "Readiness endpoint unavailable.", error_type=type(exc).__name__))

    reports = {}
    modes: tuple[tuple[Literal["auto", "legacy"], str], ...] = (("auto", "modern"), ("legacy", "legacy"))
    for mode, era in modes:
        report = await _run_era(url, headers=headers, mode=mode, timeout=timeout)
        reports[era] = report
        checks.append(
            _check(
                f"mcp_{era}_http",
                bool(report["ok"]),
                f"{era.capitalize()} HTTP contract and read-only stats round trip "
                + ("passed." if report["ok"] else "failed."),
            )
        )
    return {
        "ok": all(check["ok"] for check in checks),
        "transport": "streamable-http",
        "endpoint": url,
        "mcp_sdk_version": importlib.metadata.version("mcp"),
        "checks": checks,
        "readiness": readiness,
        "modern_http": reports["modern"],
        "legacy_http": reports["legacy"],
        "tool_count": reports["modern"].get("tool_count"),
        "tool_schema_sha256": reports["modern"].get("tool_schema_sha256"),
        "writes_performed": False,
        "local_fallback": False,
    }


async def _run_era(
    url: str,
    *,
    headers: dict[str, str],
    mode: Literal["auto", "legacy"],
    timeout: float,
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(timeout):
            async with httpx2.AsyncClient(
                headers=headers, timeout=timeout, follow_redirects=False, trust_env=False
            ) as http:
                async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
                    listed = await client.list_tools(cache_mode="bypass")
                    names = [tool.name for tool in listed.tools]
                    digest = tool_schema_digest(listed.tools)
                    result = await client.call_tool("stats", {"namespace": "__amb_deployment_verify__"})
                    expected = MCP_MODERN_VERSION if mode == "auto" else MCP_LEGACY_TEST_VERSION
                    return {
                        "ok": names == list(PUBLIC_TOOL_ORDER)
                        and digest == PUBLIC_TOOL_SCHEMA_SHA256
                        and client.protocol_version == expected
                        and not result.is_error
                        and isinstance(result.structured_content, dict),
                        "protocol_version": client.protocol_version,
                        "tool_count": len(names),
                        "tool_schema_sha256": digest,
                        "read_only_call": "stats",
                    }
    except Exception as exc:
        # Transport exception strings may contain URLs, headers or response bodies.
        return {"ok": False, "error_type": type(exc).__name__, "error": "HTTP MCP probe failed"}


def _check(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, "status": "pass" if ok else "fail", "detail": detail, **extra}
