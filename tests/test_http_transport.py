from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_mem_bridge.deployment_config import (
    HttpTransportConfig,
    add_http_arguments,
    config_from_namespace,
    read_token_file,
)
from agent_mem_bridge.http_transport import build_http_app
from agent_mem_bridge.storage import MemoryStore


def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    bridge_home = tmp_path / "bridge-home"
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HOME", str(bridge_home))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_DB_PATH", str(bridge_home / "bridge.db"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_LOG_DIR", str(bridge_home / "logs"))
    return MemoryStore.from_env()


def _initialize_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "amb-http-transport-test", "version": "1"},
        },
    }


def test_loopback_defaults_and_non_loopback_boundary(tmp_path: Path) -> None:
    config = HttpTransportConfig()
    config.validate()
    assert config.is_loopback is True
    assert "127.0.0.1:*" in config.effective_allowed_hosts
    assert "http://localhost:*" in config.effective_allowed_origins

    with pytest.raises(ValueError, match="bearer token file"):
        HttpTransportConfig(host="0.0.0.0").validate()
    with pytest.raises(ValueError, match="explicit allowed hosts"):
        HttpTransportConfig(host="0.0.0.0", token_file=tmp_path / "token").validate()
    with pytest.raises(ValueError, match="explicit allowed origins"):
        HttpTransportConfig(
            host="0.0.0.0",
            token_file=tmp_path / "token",
            allowed_hosts=("amb.example.test",),
        ).validate()
    with pytest.raises(ValueError, match="allowed host"):
        HttpTransportConfig(allowed_hosts=("*",)).validate()
    with pytest.raises(ValueError, match="allowed origin"):
        HttpTransportConfig(allowed_origins=("https://*.example.test",)).validate()
    with pytest.raises(ValueError, match="allowed origin"):
        HttpTransportConfig(allowed_origins=("https://amb.example.test/not-an-origin",)).validate()


def test_parser_reuses_explicit_http_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HTTP_PORT", "0")
    parser = argparse.ArgumentParser()
    add_http_arguments(parser)
    namespace = parser.parse_args(
        [
            "--allowed-host",
            "amb.example.test",
            "--allowed-origin",
            "https://amb.example.test",
            "--token-file",
            str(tmp_path / "dedicated-token"),
            "--host",
            "127.0.0.1",
            "--port",
            "9080",
        ]
    )
    config = config_from_namespace(namespace)
    assert config.host == "127.0.0.1"
    assert config.port == 9080
    assert config.allowed_hosts == ("amb.example.test",)
    assert config.allowed_origins == ("https://amb.example.test",)
    assert config.token_file == tmp_path / "dedicated-token"


def test_token_file_is_private_single_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("private-test-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    assert read_token_file(token_file) == "private-test-token"
    if os.name == "posix":
        token_file.chmod(0o644)
        with pytest.raises(ValueError, match="unable to read"):
            read_token_file(token_file)
        token_file.chmod(0o600)
    token_file.write_text("bad token", encoding="utf-8")
    with pytest.raises(ValueError, match="one non-empty token"):
        read_token_file(token_file)


def test_http_app_enforces_sdk_auth_and_transport_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _isolated_store(tmp_path, monkeypatch)
    token = "private-test-token"
    token_file = tmp_path / "token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    app = build_http_app(
        HttpTransportConfig(
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            token_file=token_file,
        ),
        store=store,
    )
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

    with TestClient(app) as client:
        liveness = client.get("/healthz")
        readiness = client.get("/readyz")
        absent = client.post("/mcp", json=_initialize_request(), headers=headers)
        invalid = client.post(
            "/mcp", json=_initialize_request(), headers={**headers, "Authorization": "Bearer wrong-token"}
        )
        trailing = client.post("/mcp/", json=_initialize_request(), headers=headers)
        origin = client.post(
            "/mcp",
            json=_initialize_request(),
            headers={**headers, "Authorization": f"Bearer {token}", "origin": "http://wrong.test"},
        )
        valid = client.post(
            "/mcp",
            json=_initialize_request(),
            headers={**headers, "Authorization": f"Bearer {token}"},
        )

    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok"}
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    assert "bridge.db" not in readiness.text
    assert absent.status_code == 401
    assert invalid.status_code == 401
    assert trailing.status_code == 401
    assert origin.status_code == 403
    assert valid.status_code == 200
    assert valid.headers["content-type"].startswith("application/json")
    assert valid.json()["result"]["serverInfo"]["name"] == "agent-memory-bridge"
    assert token not in valid.text


def test_missing_database_is_not_created_or_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _isolated_store(tmp_path, monkeypatch)
    missing_db = tmp_path / "missing" / "bridge.db"
    app = build_http_app(HttpTransportConfig(), store=store, readiness_db_path=missing_db)

    with TestClient(app) as client:
        readiness = client.get("/readyz")

    assert readiness.status_code == 503
    assert readiness.json()["ready"] is False
    assert missing_db.exists() is False


def test_sdk_rejection_logs_do_not_include_untrusted_headers(tmp_path: Path, monkeypatch, caplog) -> None:
    store = _isolated_store(tmp_path, monkeypatch)
    app = build_http_app(
        HttpTransportConfig(allowed_hosts=("testserver",), allowed_origins=("http://testserver",)), store=store
    )
    secret_canary = "credential-canary-must-not-appear"
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        host = client.post("/mcp", json=_initialize_request(), headers={**headers, "host": secret_canary})
        origin = client.post("/mcp", json=_initialize_request(), headers={**headers, "origin": secret_canary})
    assert host.status_code == 421
    assert origin.status_code == 403
    assert secret_canary not in caplog.text
    assert "request details omitted" in caplog.text


def test_readiness_rejects_bad_schema_and_authority_epoch_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _isolated_store(tmp_path, monkeypatch)
    malformed_db = tmp_path / "malformed.db"
    with sqlite3.connect(malformed_db) as connection:
        connection.execute("PRAGMA user_version = 12")
        connection.execute("CREATE TABLE bridge_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO bridge_metadata (key, value) VALUES ('database_epoch', 'bad-schema')")
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")

    malformed_app = build_http_app(HttpTransportConfig(), store=store, readiness_db_path=malformed_db)
    with TestClient(malformed_app) as client:
        malformed_readiness = client.get("/readyz")
    assert malformed_readiness.status_code == 503
    with sqlite3.connect(malformed_db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "agent_runs" not in tables
    assert "run_events" not in tables

    app = build_http_app(HttpTransportConfig(), store=store)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("UPDATE bridge_metadata SET value = 'changed-epoch' WHERE key = 'database_epoch'")
        changed_epoch = client.get("/readyz")
    assert changed_epoch.status_code == 503
