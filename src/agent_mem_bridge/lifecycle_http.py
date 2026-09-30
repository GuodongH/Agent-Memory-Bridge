"""One bounded recall over the existing MCP HTTP transport; no local store."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from .deployment_config import read_token_file
from .http_probe import validate_http_url


async def recall_remote(url: str, namespace: str, query: str) -> list[dict[str, Any]]:
    """Use the configured authority once. Errors propagate, never become no-hit."""
    validate_http_url(url)
    headers = {}
    token_file = os.environ.get("AGENT_MEMORY_BRIDGE_HTTP_TOKEN_FILE")
    if token_file:
        headers["Authorization"] = f"Bearer {read_token_file(Path(token_file).expanduser())}"
    async with asyncio.timeout(5):
        async with httpx2.AsyncClient(headers=headers, timeout=4, follow_redirects=False, trust_env=False) as http:
            async with Client(streamable_http_client(url, http_client=http), mode="auto") as client:
                result = await client.session.call_tool(
                    "recall",
                    {"namespace": namespace, "query": query, "kind": "memory", "limit": 3},
                    allow_input_required=True,
                )
                if getattr(result, "is_error", True):
                    raise RuntimeError("Remote recall returned a tool error")
                payload = getattr(result, "structured_content", None)
                items = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(items, list) or len(items) > 3:
                    raise ValueError("Invalid remote recall result")
                if any(
                    not isinstance(item, dict)
                    or item.get("namespace") != namespace
                    or item.get("kind") != "memory"
                    or not isinstance(item.get("content"), str)
                    for item in items
                ):
                    raise ValueError("Invalid remote recall record")
                return items
