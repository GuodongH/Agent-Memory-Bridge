from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mem_bridge.cli import main
from agent_mem_bridge.http_probe import run_http_probe, tool_schema_digest, validate_http_url


def _rejected_http_urls() -> list[str]:
    secret = "secret"
    return [
        "file:///bridge.db",
        "http://user:" + secret + "@example.test/mcp",
        "http://example.test/mcp?token=" + secret,
        "http://example.test/mcp#" + secret,
        "http://example.test:invalid/mcp",
        "http://example.test:0/mcp",
        "http://example.test/mcp\n",
        "not-a-url",
    ]


@pytest.mark.parametrize("url", _rejected_http_urls())
def test_probe_rejects_credential_or_invalid_urls_without_echo(url: str) -> None:
    with pytest.raises(ValueError) as failure:
        validate_http_url(url)
    assert "secret" not in str(failure.value)
    report = asyncio.run(run_http_probe(url))
    assert report["ok"] is False
    assert "secret" not in json.dumps(report)


def test_tool_digest_changes_for_altered_contract() -> None:
    first = [SimpleNamespace(name="store", input_schema={}, output_schema={})]
    changed = [SimpleNamespace(name="not_store", input_schema={}, output_schema={})]
    assert tool_schema_digest(first) != tool_schema_digest(changed)


@pytest.mark.parametrize("command", ["doctor", "verify"])
def test_remote_probe_cannot_fall_back_to_local_authority(tmp_path: Path, monkeypatch, capsys, command: str) -> None:
    bridge_home = tmp_path / "must-not-exist"
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HOME", str(bridge_home))

    async def unavailable(*args, **kwargs):
        return {"ok": False, "transport": "streamable-http", "checks": []}

    monkeypatch.setattr("agent_mem_bridge.http_probe.run_http_probe", unavailable)
    assert main([command, "--url", "http://127.0.0.1:1/mcp", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not bridge_home.exists()


def test_doctor_rejects_remote_and_local_transport_combination(capsys) -> None:
    assert main(["doctor", "--url", "http://127.0.0.1:1/mcp", "--include-stdio"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_verify_rejects_remote_and_runtime_directory_combination(tmp_path: Path, capsys) -> None:
    assert main(["verify", "--url", "http://127.0.0.1:1/mcp", "--runtime-dir", str(tmp_path)]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_unavailable_endpoint_is_non_pass_and_does_not_create_home(tmp_path: Path, monkeypatch) -> None:
    bridge_home = tmp_path / "absent"
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HOME", str(bridge_home))
    report = asyncio.run(run_http_probe("http://127.0.0.1:1/mcp", timeout=0.1))
    assert report["ok"] is False
    assert all(check["status"] == "fail" for check in report["checks"])
    assert report["local_fallback"] is False
    assert not bridge_home.exists()
