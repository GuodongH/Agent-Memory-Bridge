from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_mem_bridge.lifecycle_http import recall_remote


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"items": None},
        {"items": [1]},
        {"items": [{}]},
        {"items": [{"namespace": "wrong", "kind": "memory", "content": "secret"}]},
        {"items": [{}] * 4},
    ],
)
def test_malformed_result_is_error_not_no_hit(monkeypatch, payload):
    @asynccontextmanager
    async def client(*args, **kwargs):
        async def call(*args, **kwargs):
            return SimpleNamespace(is_error=False, structured_content=payload)

        yield SimpleNamespace(session=SimpleNamespace(call_tool=call))

    monkeypatch.setattr("agent_mem_bridge.lifecycle_http.Client", client)
    with pytest.raises(ValueError):
        asyncio.run(recall_remote("http://127.0.0.1/mcp", "project:test", "schema"))


@pytest.mark.parametrize("url", ["file:///tmp/bridge.db", "https://user:password@example.com/mcp", "invalid"])
def test_invalid_url_never_contacts_transport(monkeypatch, url):
    calls = []
    monkeypatch.setattr("agent_mem_bridge.lifecycle_http.Client", lambda *a, **k: calls.append(1))
    with pytest.raises(ValueError):
        asyncio.run(recall_remote(url, "project:test", "schema"))
    assert calls == []


def test_tool_error_is_not_empty_result(monkeypatch):
    calls = []

    @asynccontextmanager
    async def client(*args, **kwargs):
        async def call(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(is_error=True, structured_content={"items": []})

        yield SimpleNamespace(session=SimpleNamespace(call_tool=call))

    monkeypatch.setattr("agent_mem_bridge.lifecycle_http.Client", client)
    with pytest.raises(RuntimeError):
        asyncio.run(recall_remote("http://127.0.0.1/mcp", "project:test", "schema"))
    assert len(calls) == 1
    assert calls[0][1]["allow_input_required"] is True


def test_total_timeout_bounds_slow_remote(monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr("agent_mem_bridge.lifecycle_http.asyncio.timeout", lambda _: timeout(0.02))

    @asynccontextmanager
    async def client(*args, **kwargs):
        async def call(*args, **kwargs):
            await asyncio.sleep(60)

        yield SimpleNamespace(session=SimpleNamespace(call_tool=call))

    monkeypatch.setattr("agent_mem_bridge.lifecycle_http.Client", client)
    with pytest.raises(TimeoutError):
        asyncio.run(recall_remote("http://127.0.0.1/mcp", "project:test", "schema"))
