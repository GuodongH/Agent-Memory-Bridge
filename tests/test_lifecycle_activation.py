from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from agent_mem_bridge.cli import main
from agent_mem_bridge.lifecycle_activation import (
    activation_evidence_path,
    codex_hooks_document,
    run_hook_payload,
)
from agent_mem_bridge.mcp_boundary import PUBLIC_TOOL_NAMES
from agent_mem_bridge.repository_snapshot_store import RepositorySnapshotStore, repository_identity
from agent_mem_bridge.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
MATERIAL = "The schema choice for the run ledger may be constrained by a prior project decision. PROMPT-SECRET-991"
MEMORY = "Prior decision: keep the run ledger schema constrained by the existing governance choice."
STALE_MEMORY = "Superseded decision: the old setup used a different run ledger schema."


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_repo(parent: Path) -> Path:
    repo = parent / "work tree" / "fixture repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return repo


def isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("AGENT_MEMORY_BRIDGE_AUTHORITY_URL", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_BRIDGE_HTTP_TOKEN_FILE", raising=False)
    home = tmp_path / "amb-home"
    home.mkdir(parents=True)
    (home / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HOME", str(home))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_CONFIG", str(home / "config.toml"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_DB_PATH", str(home / "bridge.db"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_LOG_DIR", str(home / "logs"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_REPOSITORY_SNAPSHOT_ROOT", str(home / "repository"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_RECALL_RECEIPT_SECRET_PATH", str(home / "recall-receipt-secret.json"))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_TELEMETRY_MODE", "off")
    return home


def bind(repo: Path, home: Path, namespace: str) -> None:
    identity = repository_identity(repo)
    RepositorySnapshotStore(home / "repository").bind_namespace(namespace, identity["repository_id"])


def remember(
    home: Path,
    namespace: str,
    content: str,
    *,
    title: str = "Run ledger decision",
    tags: list[str] | None = None,
) -> None:
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")
    store.store(
        namespace=namespace,
        title=title,
        content=content,
        kind="memory",
        tags=tags or ["kind:decision"],
    )


def memory_count(home: Path) -> int:
    db = home / "bridge.db"
    if not db.exists():
        return 0
    connection = sqlite3.connect(db)
    try:
        return int(connection.execute("select count(*) from memories").fetchone()[0])
    finally:
        connection.close()


def tree_bytes(path: Path) -> bytes:
    if not path.exists():
        return b""
    chunks: list[bytes] = []
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        chunks.append(file.read_bytes())
    return b"".join(chunks)


def evidence_text(home: Path) -> str:
    path = home / "lifecycle" / "activation-evidence.jsonl"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def evidence_rows(home: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in evidence_text(home).splitlines() if line.strip()]


def assert_no_private_text(home: Path, response: dict[str, object]) -> None:
    blob = evidence_text(home) + json.dumps(response)
    for root, _dirs, files in os.walk(home):
        for name in files:
            file = Path(root) / name
            if file.suffix in {".jsonl", ".log", ".txt", ".json"} or "log" in name:
                blob += file.read_text(encoding="utf-8", errors="ignore")
    assert "PROMPT-SECRET-991" not in blob
    assert "TRANSCRIPT-SECRET" not in blob
    assert "additionalContext" not in evidence_text(home)


def task_payload(repo: Path, prompt: str, **extra: object) -> dict[str, object]:
    transcript = repo / "transcript.txt"
    transcript.write_text("TRANSCRIPT-SECRET hidden reasoning\n", encoding="utf-8")
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "cwd": str(repo / "nested dir"),
        "prompt": prompt,
        "namespace": "project:forged",
        "client_workspace": str(repo / "forged workspace"),
        "source_client": "caller",
        "transcript_path": str(transcript),
        **extra,
    }


def prepare_task_repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = isolate(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    nested = repo / "nested dir"
    nested.mkdir()
    bind(repo, home, "project:fixture")
    return home, repo


def test_public_mcp_surface_does_not_grow() -> None:
    assert len(PUBLIC_TOOL_NAMES) == 17
    assert "lifecycle-hook" not in PUBLIC_TOOL_NAMES


def test_codex_hook_file_matches_cross_platform_entrypoint() -> None:
    document = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
    assert document == codex_hooks_document()
    handler = document["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert handler["command"] == "python3 -m agent_mem_bridge lifecycle-hook"
    assert handler["commandWindows"] == "py -3 -m agent_mem_bridge lifecycle-hook"
    assert "AGENTS.md" not in handler["command"]
    assert "AGENTS.md" not in handler["commandWindows"]


def test_opencode_plugin_uses_same_entrypoint_and_pending_prompt_lane() -> None:
    source = (ROOT / "adapters/opencode/amb-lifecycle.js").read_text(encoding="utf-8")
    assert "lifecycle-hook" in source
    assert "session.created" in source
    assert "experimental.session.compacting" in source
    assert "properties.info" in source or "info.id" in source
    assert "HOOK_TIMEOUT_MS = 10000" in source
    assert '"message.updated"' not in source
    assert "JSON.stringify(event" not in source
    assert "pending lane" in source


def test_session_start_resolves_binding_without_recall_or_dump(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", MEMORY)
    (repo / "UNTRACKED.txt").write_text("dirty\n", encoding="utf-8")
    bindings = (home / "repository" / "bindings.json").read_bytes()
    logs = tree_bytes(home / "logs")
    before = memory_count(home)
    response = run_hook_payload(
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": "session-1",
            "cwd": str(repo / "nested dir"),
            "prompt": MATERIAL,
            "namespace": "project:forged",
            "client_workspace": "forged",
        }
    )
    row = evidence_rows(home)[-1]
    assert response == {"continue": True}
    assert row["resolution_status"] == "bound"
    assert row["namespace"] == "project:fixture"
    assert row["ignored_caller_scope"] is True
    assert row["decision"] == "skip"
    assert row["rule_id"] == "session-entry-no-dump"
    assert row["recall_invoked"] is False
    assert row["adapter_loaded"] is True
    assert memory_count(home) == before
    assert (home / "repository" / "bindings.json").read_bytes() == bindings
    assert tree_bytes(home / "logs") == logs
    assert_no_private_text(home, response)


def test_negative_controls_do_not_recall(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", MEMORY)
    before = memory_count(home)
    typo = run_hook_payload(task_payload(repo, "Fix the misspelled import in this file. PROMPT-SECRET-991"))
    optional = run_hook_payload(
        task_payload(
            repo, "There are several alternatives for this refactor. PROMPT-SECRET-991", session_id="session-2"
        )
    )
    rows = evidence_rows(home)
    assert typo == {"continue": True}
    assert optional == {"continue": True}
    assert rows[-2]["rule_id"] == "not-for-deterministic-edit"
    assert rows[-1]["rule_id"] == "large-design-choice"
    assert all(row["recall_invoked"] is False for row in rows)
    assert memory_count(home) == before


def test_material_recall_distinguishes_hit_no_hit_irrelevant_and_repeat(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", MEMORY)
    before = memory_count(home)
    hit = run_hook_payload(task_payload(repo, MATERIAL))
    logs_after_hit = tree_bytes(home / "logs")
    repeated = run_hook_payload(task_payload(repo, MATERIAL))
    assert tree_bytes(home / "logs") == logs_after_hit
    changed = run_hook_payload(
        task_payload(repo, MATERIAL + " The governance migration also changed.", session_id="session-1")
    )
    assert "Untrusted governed context" in changed["hookSpecificOutput"]["additionalContext"]
    hit_row, repeat_row, changed_row = evidence_rows(home)[-3:]
    assert hit_row["recall_invoked"] is True
    assert hit_row["recall_state"] == "hit"
    assert hit_row["namespace"] == "project:fixture"
    assert "Untrusted governed context" in hit["hookSpecificOutput"]["additionalContext"]
    assert "PROMPT-SECRET-991" not in hit["hookSpecificOutput"]["additionalContext"]
    assert repeat_row["repeat_suppressed"] is True
    assert repeat_row["recall_invoked"] is False
    assert repeated == {"continue": True}
    assert changed_row["recall_invoked"] is True
    assert memory_count(home) == before
    assert_no_private_text(home, hit)

    empty_home = isolate(tmp_path / "empty", monkeypatch)
    empty_repo = make_repo(tmp_path / "empty-repo")
    (empty_repo / "nested dir").mkdir()
    bind(empty_repo, empty_home, "project:fixture")
    MemoryStore(empty_home / "bridge.db", log_dir=empty_home / "logs")
    no_hit = run_hook_payload(task_payload(empty_repo, MATERIAL, session_id="empty-session"))
    no_hit_row = evidence_rows(empty_home)[-1]
    assert no_hit_row["recall_state"] == "no_hit"
    assert no_hit_row["recall_invoked"] is True
    assert "not an unavailable bridge" in no_hit["hookSpecificOutput"]["additionalContext"]

    unrelated_home = isolate(tmp_path / "unrelated", monkeypatch)
    unrelated_repo = make_repo(tmp_path / "unrelated-repo")
    (unrelated_repo / "nested dir").mkdir()
    bind(unrelated_repo, unrelated_home, "project:fixture")
    remember(
        unrelated_home,
        "project:fixture",
        "Banana pancake recipe for the office kitchen.",
        title="Kitchen note",
    )
    irrelevant = run_hook_payload(task_payload(unrelated_repo, MATERIAL, session_id="unrelated-session"))
    irrelevant_row = evidence_rows(unrelated_home)[-1]
    assert irrelevant_row["recall_state"] == "irrelevant_hit"
    assert "banana" not in irrelevant["hookSpecificOutput"]["additionalContext"].lower()
    assert "not an unavailable bridge" in irrelevant["hookSpecificOutput"]["additionalContext"]


def test_missing_local_database_is_not_reported_as_amb_unavailable(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    response = run_hook_payload(task_payload(repo, MATERIAL))
    row = evidence_rows(home)[-1]
    context = response["hookSpecificOutput"]["additionalContext"]
    assert row["authority_mode"] == "unknown"
    assert row["availability"] == "unknown"
    assert row["recall_state"] == "skipped"
    assert row["recall_invoked"] is False
    assert row["rule_id"] == "authority-unknown"
    assert not (home / "bridge.db").exists()
    assert "not an unavailable bridge" in context
    assert "not an empty memory result" in context
    assert "AMB was unavailable" not in context
    assert "no matching memory" not in context


@contextmanager
def remote_authority(tmp_path: Path, monkeypatch, *, token_file=None):
    from agent_mem_bridge.deployment_config import HttpTransportConfig
    from agent_mem_bridge.http_transport import build_http_app

    store = MemoryStore(tmp_path / "remote.db", log_dir=tmp_path / "remote-logs")
    calls = []
    original = store.recall

    def observed(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(store, "recall", observed)
    app = build_http_app(HttpTransportConfig(token_file=token_file), store=store, readiness_db_path=store.db_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{port}/mcp", store, calls
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


@pytest.mark.parametrize("configured_by", ["env", "config"])
@pytest.mark.parametrize("scenario", ["hit", "no_hit", "skip", "unavailable", "irrelevant_hit", "stale_conflict"])
def test_remote_authority_does_not_open_a_stale_local_database(
    tmp_path: Path, monkeypatch, configured_by, scenario
) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    trap = "Prior decision: replace the run ledger schema with CONFLICTING-LOCAL-TRAP."
    remember(home, "project:fixture", trap)
    attempts = []
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if str(home / "bridge.db") in str(database):
            attempts.append(str(database))
            raise AssertionError("local database opened despite remote selection")
        return original_connect(database, *args, **kwargs)

    with remote_authority(tmp_path, monkeypatch) as (url, store, calls):
        if scenario in {"hit", "stale_conflict", "irrelevant_hit"}:
            store.store(
                namespace="project:fixture",
                kind="memory",
                content=(
                    STALE_MEMORY
                    if scenario == "stale_conflict"
                    else "Banana pancake recipe"
                    if scenario == "irrelevant_hit"
                    else MEMORY
                ),
            )
        if scenario == "irrelevant_hit":
            monkeypatch.setattr("agent_mem_bridge.lifecycle_activation._recall_query", lambda _: "banana")
        if scenario == "unavailable":
            url = "http://127.0.0.1:1/mcp"
        if configured_by == "env":
            monkeypatch.setenv("AGENT_MEMORY_BRIDGE_AUTHORITY_URL", url)
        else:
            (home / "config.toml").write_text(f'[deployment]\nauthority_url = "{url}"\n', encoding="utf-8")
        monkeypatch.setattr(sqlite3, "connect", guarded_connect)
        transport_attempts = []
        if scenario == "skip":

            def forbidden_transport(*args, **kwargs):
                transport_attempts.append(1)
                raise AssertionError("negative control created an HTTP client")

            monkeypatch.setattr("agent_mem_bridge.lifecycle_http.httpx2.AsyncClient", forbidden_transport)
        prompt = "Fix the typo" if scenario == "skip" else MATERIAL
        response = run_hook_payload(task_payload(repo, prompt))
        row = evidence_rows(home)[-1]
        assert row["authority_mode"] == "remote"
        assert row["recall_state"] == {"skip": "skipped", "unavailable": "error"}.get(scenario, scenario)
        assert len(calls) == (0 if scenario in {"skip", "unavailable"} else 1)
        if calls:
            assert calls[0]["namespace"] == "project:fixture"
            assert calls[0]["limit"] == 3
            assert calls[0]["kind"] == "memory"
            run_hook_payload(task_payload(repo, prompt))
            assert evidence_rows(home)[-1]["repeat_suppressed"] is True
            assert len(calls) == 1
        assert attempts == []  # Independent of hook exception handling.
        assert transport_attempts == []
        assert "CONFLICTING-LOCAL-TRAP" not in json.dumps(response)
        assert url not in evidence_text(home)
        assert response["continue"] is True
        if scenario == "unavailable":
            assert row["availability"] == "error"
            assert "not an empty memory result" in json.dumps(response)


@pytest.mark.parametrize("authorized", [True, False])
def test_remote_bearer_authentication(tmp_path: Path, monkeypatch, authorized) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", "CONFLICTING-LOCAL-TRAP run ledger schema")
    token = tmp_path / "token"
    token.write_text("private-fixture-token", encoding="ascii")
    token.chmod(0o600)
    with remote_authority(tmp_path, monkeypatch, token_file=token) as (url, store, calls):
        store.store(namespace="project:fixture", kind="memory", content=MEMORY)
        monkeypatch.setenv("AGENT_MEMORY_BRIDGE_AUTHORITY_URL", url)
        if authorized:
            monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HTTP_TOKEN_FILE", str(token))
        local_calls = []

        def forbidden(*args, **kwargs):
            local_calls.append(1)
            raise AssertionError("local fallback")

        monkeypatch.setattr("agent_mem_bridge.lifecycle_activation._recall", forbidden)
        response = run_hook_payload(task_payload(repo, MATERIAL))
        assert evidence_rows(home)[-1]["recall_state"] == ("hit" if authorized else "error")
        assert len(calls) == int(authorized)
        assert local_calls == []
        assert "private-fixture-token" not in json.dumps(response) + evidence_text(home)


def test_exact_repeat_is_suppressed_but_a_paraphrase_recalls_again(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", MEMORY)
    first = "The schema choice for the run ledger may be constrained by a prior project decision."
    paraphrase = "A prior project decision may still constrain the run ledger schema choice."
    run_hook_payload(task_payload(repo, first, session_id="same-need"))
    repeated = run_hook_payload(task_payload(repo, first, session_id="same-need"))
    changed = run_hook_payload(task_payload(repo, paraphrase, session_id="same-need"))
    first_row, repeat_row, paraphrase_row = evidence_rows(home)[-3:]
    assert first_row["recall_invoked"] is True
    assert repeat_row["repeat_suppressed"] is True
    assert repeat_row["rule_id"] == "exact-prompt-repeat"
    assert repeat_row["recall_invoked"] is False
    assert repeated == {"continue": True}
    assert paraphrase_row["recall_invoked"] is True
    assert paraphrase_row["repeat_suppressed"] is False
    assert "Untrusted governed context" in changed["hookSpecificOutput"]["additionalContext"]


def test_scope_failures_do_not_mutate_bindings_or_accept_caller_namespace(tmp_path: Path, monkeypatch) -> None:
    home = isolate(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    (repo / "nested dir").mkdir()
    missing = home / "repository" / "bindings.json"
    unbound = run_hook_payload(task_payload(repo, MATERIAL))
    assert evidence_rows(home)[-1]["resolution_status"] == "no_binding"
    assert evidence_rows(home)[-1]["rule_id"] == "no-project-binding"
    assert evidence_rows(home)[-1]["namespace"] is None
    assert not missing.exists()
    assert "no governed project binding" in unbound["hookSpecificOutput"]["additionalContext"]

    bind(repo, home, "project:one")
    bind(repo, home, "project:two")
    before = (home / "repository" / "bindings.json").read_bytes()
    ambiguous = run_hook_payload(task_payload(repo, MATERIAL, session_id="other-session"))
    row = evidence_rows(home)[-1]
    assert row["resolution_status"] == "ambiguous_binding"
    assert row["decision"] == "fail_closed"
    assert row["recall_invoked"] is False
    assert "project:forged" not in evidence_text(home)
    assert (home / "repository" / "bindings.json").read_bytes() == before
    assert "ambiguous" in ambiguous["hookSpecificOutput"]["additionalContext"]


def test_stale_memory_is_labeled_and_not_treated_as_authority(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", STALE_MEMORY, tags=["kind:decision", "lifecycle:stale"])
    response = run_hook_payload(
        task_payload(repo, "The old setup looks superseded and conflicts with the current server. PROMPT-SECRET-991")
    )
    row = evidence_rows(home)[-1]
    context = response["hookSpecificOutput"]["additionalContext"]
    assert row["recall_state"] == "stale_conflict"
    assert row["stale_ids"]
    assert "not durable authority" in context
    assert "Do not apply it over the live repository." in context
    assert memory_count(home) == 1


def test_compaction_preserves_continuity_without_transcript_or_recall(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", MEMORY)
    before = memory_count(home)
    logs = tree_bytes(home / "logs")
    codex = run_hook_payload(
        {
            "hook_event_name": "PreCompact",
            "trigger": "auto",
            "session_id": "session-1",
            "cwd": str(repo),
            "prompt": MATERIAL,
            "transcript_path": str(repo / "transcript.txt"),
        }
    )
    resumed = run_hook_payload(
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "session-1",
            "cwd": str(repo),
            "prompt": MATERIAL,
        }
    )
    opencode = run_hook_payload(
        {
            "host": "opencode",
            "hook_event_name": "PreCompact",
            "trigger": "auto",
            "session_id": "session-1",
            "cwd": str(repo),
            "prompt": MATERIAL,
        }
    )
    rows = evidence_rows(home)
    assert codex == {"continue": True}
    assert rows[-3]["decision"] == "continuity"
    assert rows[-3]["recall_invoked"] is False
    assert "not a transcript" in resumed["hookSpecificOutput"]["additionalContext"]
    assert "project:fixture" in resumed["hookSpecificOutput"]["additionalContext"]
    assert MEMORY not in resumed["hookSpecificOutput"]["additionalContext"]
    assert "not a transcript" in opencode["hookSpecificOutput"]["additionalContext"]
    assert memory_count(home) == before
    assert tree_bytes(home / "logs") == logs
    assert_no_private_text(home, resumed)


def test_recall_error_is_not_a_no_hit(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    (home / "bridge.db").write_text("not a database", encoding="utf-8")
    response = run_hook_payload(task_payload(repo, MATERIAL))
    row = evidence_rows(home)[-1]
    assert row["recall_state"] == "error"
    assert row["recall_invoked"] is True
    assert row["error_type"]
    assert "not an empty memory result" in response["hookSpecificOutput"]["additionalContext"]


def test_cli_hook_entrypoint_handles_spaced_cwd_and_invalid_input(tmp_path: Path, monkeypatch) -> None:
    home, repo = prepare_task_repo(tmp_path, monkeypatch)
    remember(home, "project:fixture", MEMORY)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "agent_mem_bridge", "lifecycle-hook"],
        input=json.dumps(task_payload(repo, MATERIAL, session_id="cli-session")),
        text=True,
        cwd=repo / "nested dir",
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert "Untrusted governed context" in payload["hookSpecificOutput"]["additionalContext"]
    assert "PROMPT-SECRET-991" not in completed.stdout
    assert evidence_rows(home)[-1]["resolution_status"] == "bound"
    assert activation_evidence_path() == home / "lifecycle" / "activation-evidence.jsonl"

    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    assert main(["lifecycle-hook"]) == 0
