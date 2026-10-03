"""Repaired Codex collection; the canonical #50 CLI/instrument remain frozen."""

from __future__ import annotations
# ruff: noqa: E402, I001

from collections import defaultdict, deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

from _source_imports import ensure_source_root

ensure_source_root()

import run_lifecycle_activation_benchmark as canonical
from run_lifecycle_activation_benchmark import (
    _bridge_python,
    _codex_native,
    _command_version,
    _model_gateway,
    _node_prefix,
    _probe_isolation,
    _redact,
    _redact_obj,
    _require_bwrap,
    _sanitize,
    _start_fixture_http,
    _stop_process,
)
from tools.evidence._temporary_store import ScopedTemporaryMemoryStore
from tools.evidence.lifecycle_activation_measurement import (
    LEGACY_REVISION,
    READ_REVISION,
    REVISIONS,
    measurement_identity,
)
from tools.evidence.lifecycle_activation import (
    ROOT,
    assess_fixture_access,
    bind_fixture_namespace,
    build_allowlisted_filesystem_argv,
    build_codex_collect_argv,
    git_commit_fixture,
    load_pack,
    materialize_fixture,
    memory_only_markers,
    model_provider_override,
    parse_codex_exec_jsonl,
    prepare_collector_layout,
    render_collector_codex_preamble,
    render_isolated_codex_config,
    render_local_proxy_mcp_stanza,
    render_text,
    sandbox_for_case,
    scorer_sha256,
    seed_case_memories,
    verify_freeze,
    write_scored_observation,
)

CODEX_STORE_MOUNT = PurePosixPath("/opt/amb-lifecycle-store")


def _build_codex_collect_argv(*, permission_profile: str | None = None, **kwargs) -> list[str]:
    # Collector permissions are not part of the frozen measurement instrument.
    argv = build_codex_collect_argv(**kwargs)
    if permission_profile:
        index = argv.index("-s")
        argv[index : index + 2] = ["-c", f"default_permissions={json.dumps(permission_profile)}"]
    return argv


def _bind_codex_fixture_namespace(case: dict, checkout: Path, bridge_home: Path) -> str:
    if case.get("expected_namespace") is not None:
        return bind_fixture_namespace(case, checkout, bridge_home)

    # Null-namespace controls need the normal fixture store, but no binding.
    from agent_mem_bridge.repository_snapshot_store import repository_identity

    repository_id = str(repository_identity(checkout)["repository_id"])
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "config.toml").write_text('[bridge]\ndb_path = "fixture-store.sqlite"\n', encoding="utf-8")
    if case["fixture"]["amb_mode"] == "available":
        store = ScopedTemporaryMemoryStore(bridge_home / "fixture-store.sqlite", bridge_home / "logs")
        try:
            seed_case_memories(store, case)
        finally:
            store.close()
    return repository_id


def collect_codex(
    case_id: str,
    out_dir: Path,
    model: str,
    timeout: float,
    pack_version: str = "v1",
    condition: str = "plain_mcp_baseline",
    adapter_backend: str = "local",
    measurement_revision: str = LEGACY_REVISION,
) -> int:
    if measurement_revision not in REVISIONS:
        print("unsupported measurement revision", file=sys.stderr)
        return 2
    measurement = measurement_identity() if measurement_revision == READ_REVISION else None
    if condition == "adapter_enabled" and pack_version != "v2":
        print("adapter collection requires the frozen v2 pack", file=sys.stderr)
        return 2
    if condition not in {"plain_mcp_baseline", "adapter_enabled"}:
        print(f"unsupported condition {condition}", file=sys.stderr)
        return 2
    if adapter_backend not in {"local", "remote_loopback"} or (
        adapter_backend == "remote_loopback" and condition != "adapter_enabled"
    ):
        print("remote_loopback requires the adapter_enabled condition", file=sys.stderr)
        return 2
    freeze = verify_freeze(version=pack_version)
    if not freeze["ok"]:
        print(json.dumps({"ok": False, "problems": ["freeze mismatch"]}, indent=2))
        return 1
    pack = load_pack(version=pack_version)
    case = next((item for item in pack["cases"] if item["id"] == case_id), None)
    if case is None:
        print(f"unknown case {case_id}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    layout = prepare_collector_layout(
        store_root=Path.home() / ".cache",
        client_root=Path.home() / ".local" / "share",
        workspace_root=Path("/tmp"),
    )
    fixture_repo = layout["fixture_repo"]
    store_home = layout["store_home"]
    codex_home = layout["codex_home"]
    materialize_fixture(case, fixture_repo)
    store_home.chmod(0o700)
    (out_dir / "layout.json").write_text(
        json.dumps({key: str(value) for key, value in layout.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    config_path = store_home / "config.toml"
    if pack_version == "v2":
        git_commit_fixture(fixture_repo)
        _bind_codex_fixture_namespace(case, fixture_repo, store_home)
        if condition == "adapter_enabled" and case["fixture"]["amb_mode"] == "unavailable":
            # A missing DB means unconfigured/unknown, not backend failure. Use
            # a genuinely failing local authority for this frozen error lane.
            (store_home / "fixture-store.sqlite").write_bytes(b"unavailable local SQLite fixture\n")
    else:
        config_path.write_text('[bridge]\ndb_path = "fixture-store.sqlite"\n', encoding="utf-8")
        if case["fixture"]["amb_mode"] == "available":
            store = ScopedTemporaryMemoryStore(store_home / "fixture-store.sqlite", store_home / "logs")
            try:
                seed_case_memories(store, case)
            finally:
                store.close()
    durable_before = _fixture_memory_digest(store_home)
    codex_home.mkdir(parents=True, exist_ok=True)
    codex_home.chmod(0o700)
    server: subprocess.Popen[str] | None = None
    unavailable_socket = None
    bridge_home = store_home
    fixture_secrets: list[str] = []
    hook_overrides: list[str] = []
    try:
        if condition == "adapter_enabled":
            bridge_python = Path(sys.executable) if adapter_backend == "remote_loopback" else _bridge_python()
            hook_python = bridge_python
            authority_url = None
            hook_token = None
            if adapter_backend == "remote_loopback":
                import socket

                bridge_home = codex_home / "bridge"
                (bridge_home / "repository").mkdir(parents=True)
                binding_path = store_home / "repository" / "bindings.json"
                if binding_path.is_file():
                    shutil.copyfile(binding_path, bridge_home / "repository" / "bindings.json")
                if case["fixture"]["amb_mode"] == "available":
                    server, authority_url, token_source = _start_fixture_http(store_home, bridge_python)
                    hook_token = bridge_home / "http-token"
                    hook_token.write_bytes(token_source.read_bytes())
                    hook_token.chmod(0o600)
                    fixture_secrets.append(hook_token.read_text(encoding="utf-8").strip())
                else:
                    # Reserve the unavailable endpoint for the entire run.
                    unavailable_socket = socket.socket()
                    unavailable_socket.bind(("127.0.0.1", 0))
                    authority_url = f"http://127.0.0.1:{unavailable_socket.getsockname()[1]}/mcp"
                hook_python = Path("/opt/amb-lifecycle-python/bin/python")
                command = f"/usr/bin/python3 {codex_home / 'hook_capture.py'}"
                hook_overrides = [
                    "features.hooks=true",
                    'hooks.UserPromptSubmit=[{hooks=[{type="command",command='
                    + json.dumps(command)
                    + ",timeout=10}]}]",
                ]
            _install_adapter_hook(
                codex_home=codex_home,
                fixture_repo=fixture_repo,
                store_home=CODEX_STORE_MOUNT if adapter_backend == "local" else bridge_home,
                python_path=hook_python,
                source_mount=Path("/opt/amb-lifecycle-src"),
                authority_url=authority_url,
                token_file=hook_token,
                install_project_hooks=adapter_backend == "local",
            )
            codex_config = ""
        elif case["fixture"]["amb_mode"] == "available":
            bridge_python = _bridge_python()
            server, url, token_source = _start_fixture_http(store_home, bridge_python)
            proxy_token = codex_home / "proxy-token"
            proxy_token.write_bytes(token_source.read_bytes())
            proxy_token.chmod(0o600)
            codex_config = render_local_proxy_mcp_stanza(
                python_path=str(bridge_python), url=url, token_file=str(proxy_token)
            )
        else:
            fail_path = store_home / "unavailable_server.py"
            fail_path.write_text(
                "import sys\nsys.stderr.write('fixture AMB unavailable\\n')\nraise SystemExit(1)\n",
                encoding="utf-8",
            )
            codex_config = render_isolated_codex_config(
                python_path=sys.executable,
                cwd=str(ROOT),
                bridge_home=str(store_home),
                config_path=str(config_path),
                source_root=str(ROOT / "src"),
                unavailable_command=[sys.executable, str(fail_path)],
            )
        if str(store_home) in codex_config or "192.168." in codex_config:
            print("collector config exposes the fixture store or a production route", file=sys.stderr)
            return 1
        catalog_source = Path.home() / ".codex" / "model-catalog.json"
        catalog_copy = codex_home / "model-catalog.json"
        if catalog_source.is_file():
            shutil.copyfile(catalog_source, catalog_copy)
        preamble = render_collector_codex_preamble(
            catalog_path=str(catalog_copy),
            fixture_repo=str(fixture_repo),
            plugins=condition == "adapter_enabled",
        )
        if condition == "adapter_enabled":
            preamble += _codex_fixture_permissions(
                sandbox=sandbox_for_case(case_id),
                codex_home=codex_home,
                fixture_repo=fixture_repo,
                source_mount=Path("/opt/amb-lifecycle-src"),
            )
        (codex_home / "config.toml").write_text(preamble + codex_config, encoding="utf-8")
        native = _codex_native()
        node_prefix = _node_prefix(native)
        venv = bridge_python.parent.parent if adapter_backend == "remote_loopback" else _bridge_python().parent.parent
        api_key, base_url = _model_gateway()
        override = model_provider_override(base_url)
        markers = memory_only_markers(case)
        if any(marker and marker in override for marker in markers):
            print("model gateway URL contains a fixture marker", file=sys.stderr)
            return 1
        version = _command_version([str(native), "--version"])
        prompt_path = out_dir / "prompt.txt"
        prompt_path.write_text(case["prompt"], encoding="utf-8")
        stdout_path = out_dir / "codex.jsonl"
        bwrap = _require_bwrap()
        source_mount = Path("/opt/amb-lifecycle-src")
        protected = [ROOT, Path.home() / ".codex", Path.home() / ".cache", Path("/tmp")]
        venv_mount = Path("/opt/amb-lifecycle-python") if adapter_backend == "remote_loopback" else venv
        ro_binds = [(node_prefix, node_prefix), (venv, venv_mount)]
        rw_binds = [(codex_home, codex_home), (layout["workspace_parent"], layout["workspace_parent"])]
        scan_roots = [node_prefix, venv_mount, fixture_repo, codex_home]
        absent = [ROOT, Path.home() / ".codex", Path.home() / ".cache"]
        if condition == "adapter_enabled":
            ro_binds.append((ROOT / "src", source_mount))
            if adapter_backend == "local":
                rw_binds.append((store_home, CODEX_STORE_MOUNT))
            else:
                protected.append(store_home)
                absent.extend([store_home, store_home / "fixture-store.sqlite"])
            scan_roots.append(source_mount)
        else:
            protected.append(store_home)
            absent.append(store_home)
        try:
            probe = _probe_isolation(
                bwrap=bwrap,
                ro_binds=ro_binds,
                rw_binds=rw_binds,
                protected=protected,
                markers=markers,
                scan_roots=scan_roots,
                absent=absent,
            )
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            print(
                json.dumps({"ok": False, "status": "ISOLATION_PREFLIGHT_FAILED", "reason": str(exc)[:400]}),
                file=sys.stderr,
            )
            return 1
        preflight_ok = not probe["visible"] and not probe["hits"] and probe["nested"]["ok"]
        (out_dir / "preflight.json").write_text(
            json.dumps(
                {
                    "ok": preflight_ok,
                    "visible": probe["visible"],
                    "hit_count": len(probe["hits"]),
                    "hit_paths": probe["hits"],
                    "nested_ok": probe["nested"]["ok"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if not preflight_ok:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "ISOLATION_PREFLIGHT_FAILED",
                        "visible": probe["visible"],
                        "hit_count": len(probe["hits"]),
                        "nested_ok": probe["nested"]["ok"],
                    }
                ),
                file=sys.stderr,
            )
            return 1
        if condition == "adapter_enabled":
            tool_probe = _probe_codex_tool_access(
                native=native,
                bwrap=bwrap,
                codex_home=codex_home,
                fixture_repo=fixture_repo,
                ro_binds=ro_binds,
                rw_binds=rw_binds,
                protected=protected,
                denied=[CODEX_STORE_MOUNT, codex_home, source_mount, fixture_repo / ".codex"],
            )
            (out_dir / "tool-preflight.json").write_text(json.dumps(tool_probe, indent=2) + "\n", encoding="utf-8")
            if not tool_probe["ok"]:
                print(json.dumps({"ok": False, "status": "TOOL_ISOLATION_PREFLIGHT_FAILED", **tool_probe}))
                return 1
        started = time.perf_counter()
        env = {
            "PATH": f"{node_prefix / 'bin'}:/usr/bin:/bin",
            "HOME": "/tmp",
            "CODEX_HOME": str(codex_home),
            "TMPDIR": "/tmp",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "USER": os.environ.get("USER", "user"),
            "LOGNAME": os.environ.get("USER", "user"),
            "TERM": "xterm",
            "OPENAI_API_KEY": api_key,
        }
        argv = _build_codex_collect_argv(
            sandbox=sandbox_for_case(case_id),
            model=model,
            fixture_repo=fixture_repo,
            output_path=fixture_repo.parent / "last-message.md",
            prompt=case["prompt"],
            codex_bin=str(native),
            config_overrides=[override, *hook_overrides],
            condition=condition,
            permission_profile="amb-benchmark" if condition == "adapter_enabled" else None,
        )
        if "--add-dir" in argv or str(store_home) in argv or ":58080" in " ".join(argv):
            print("collector argv exposes the fixture store or production AMB", file=sys.stderr)
            return 1
        isolated = build_allowlisted_filesystem_argv(
            bwrap=bwrap,
            ro_binds=ro_binds,
            rw_binds=rw_binds,
            command=argv,
            protected_paths=protected,
        )
        try:
            completed = subprocess.run(
                isolated,
                cwd=fixture_repo,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stderr = f"{stderr}\ncollector timeout\n"
            returncode = None
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        secrets = [api_key, *fixture_secrets]
        token_path = codex_home / "proxy-token"
        if token_path.is_file():
            secrets.append(token_path.read_text(encoding="utf-8").strip())
        stdout_path.write_text(_redact(stdout, secrets), encoding="utf-8")
        (out_dir / "stderr.txt").write_text(_redact(stderr, secrets), encoding="utf-8")
        parsed = parse_codex_exec_jsonl(
            stdout,
            interesting_paths=list(case["fixture"]["files"]),
            measurement_revision=measurement_revision,
        )
        fixture_access = assess_fixture_access(
            parsed["commands"],
            markers=markers,
            hidden_paths=[
                str(store_home),
                str(CODEX_STORE_MOUNT),
                str(codex_home),
                str(source_mount),
                "fixture-store.sqlite",
            ],
        )
        observation = _redact_obj(
            {
                "schema": "amb.lifecycle-activation-observation.v1",
                "case_id": case_id,
                "trace_complete": returncode == 0
                and parsed["terminal_event"] == "turn.completed"
                and bool(str(parsed["final_text"]).strip()),
                "terminal_event": parsed["terminal_event"],
                "execution_kind": "live",
                "condition": condition,
                "adapter_backend": adapter_backend,
                "hook_loading": ("invocation_inline" if adapter_backend == "remote_loopback" else "project_hooks")
                if condition == "adapter_enabled"
                else None,
                "pack": pack_version,
                "host": {"id": "codex", "version": version, "model": model},
                "amb_available": case["fixture"]["amb_mode"] == "available",
                "namespace_bound": case.get("expected_namespace"),
                "tool_calls": parsed["tool_calls"],
                "final_text": parsed["final_text"],
                "repo_paths_read": parsed["repo_paths_read"],
                "fixture_access": fixture_access,
                "latency_ms": elapsed_ms,
                "input_tokens": parsed["input_tokens"],
                "output_tokens": parsed["output_tokens"],
                "collector_exit_code": returncode,
                "freeze_sha256": freeze["actual"],
                "scorer_sha256": scorer_sha256(),
                "source_sha256": _source_sha256(),
                "durable_memory_unchanged": durable_before == _fixture_memory_digest(store_home),
                "adapter": _adapter_observation(bridge_home) if condition == "adapter_enabled" else {"loaded": False},
                "adapter_evidence": _adapter_evidence(bridge_home) if condition == "adapter_enabled" else [],
            },
            secrets,
        )
        if measurement is not None:
            observation["measurement"] = measurement
            observation["repository_read_evidence"] = parsed["repository_read_evidence"]
        report = write_scored_observation(out_dir, pack, observation, freeze)
        print(render_text(report), end="")
        if returncode is None:
            return 1
        return 0 if returncode == 0 or observation["trace_complete"] else returncode
    finally:
        _stop_process(server)
        if unavailable_socket is not None:
            unavailable_socket.close()


def _codex_fixture_permissions(*, sandbox: str, codex_home: Path, fixture_repo: Path, source_mount: Path) -> str:
    """Deny model tools access to the store and collector, not the trusted hook.

    A legacy --sandbox argument overrides these permissions. The collector must
    select this profile instead, and verify it with a real native sandbox probe.
    """
    parent = ":workspace" if sandbox == "workspace-write" else ":read-only"
    lines = [
        "\n[permissions.amb-benchmark]",
        f"extends = {json.dumps(parent)}",
        "[permissions.amb-benchmark.filesystem]",
    ]
    # Exact denials preserve the built-in workspace/.git protections. Hooks run
    # in the trusted host process and can still use these private fixture paths.
    for path in (CODEX_STORE_MOUNT, codex_home, source_mount, fixture_repo / ".codex"):
        lines.append(f'{json.dumps(str(path))} = "deny"')
    lines.extend(["[permissions.amb-benchmark.network]", "enabled = false", ""])
    return "\n".join(lines)


def _probe_codex_tool_access(
    *,
    native: Path,
    bwrap: Path,
    codex_home: Path,
    fixture_repo: Path,
    ro_binds: list[tuple[Path, Path]],
    rw_binds: list[tuple[Path, Path]],
    protected: list[Path],
    denied: list[Path],
) -> dict:
    # Read actual files, not just a permission-profile field or directory name.
    targets = [
        CODEX_STORE_MOUNT / "config.toml",
        CODEX_STORE_MOUNT / "fixture-store.sqlite",
        codex_home / "hook_capture.py",
        codex_home / "config.toml",
        denied[2] / "agent_mem_bridge" / "lifecycle_activation.py",
        fixture_repo / ".codex" / "hooks.json",
    ]
    code = (
        "import json, sys; from pathlib import Path\n"
        "readable=[]\n"
        "for name in sys.argv[1:-1]:\n"
        "    try: Path(name).read_bytes(); readable.append(name)\n"
        "    except OSError: pass\n"
        "workspace_ok=Path(sys.argv[-1]).is_dir()\n"
        "print(json.dumps({'ok': not readable and workspace_ok, 'readable': readable, 'workspace_ok': workspace_ok}))\n"
    )
    command = [
        str(native),
        "sandbox",
        "-P",
        "amb-benchmark",
        "-C",
        str(fixture_repo),
        "--",
        "/usr/bin/python3",
        "-c",
        code,
        *map(str, targets),
        str(fixture_repo),
    ]
    isolated = build_allowlisted_filesystem_argv(
        bwrap=bwrap,
        ro_binds=ro_binds,
        rw_binds=rw_binds,
        command=command,
        protected_paths=protected,
    )
    completed = subprocess.run(
        isolated,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "CODEX_HOME": str(codex_home), "LANG": "C.UTF-8"},
        cwd=fixture_repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        return {"ok": False, "exit_code": completed.returncode, "error": _sanitize(completed.stderr)[-500:]}
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "native sandbox probe produced no valid result"}


def _source_sha256() -> dict[str, str]:
    paths = [
        ROOT / "scripts/run_lifecycle_activation_benchmark.py",
        ROOT / "scripts/run_codex_lifecycle_activation_benchmark.py",
        ROOT / "tools/evidence/lifecycle_activation.py",
        *sorted((ROOT / "src/agent_mem_bridge").glob("*.py")),
    ]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _fixture_memory_digest(store_home: Path) -> str | None:
    path = store_home / "fixture-store.sqlite"
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
    except sqlite3.DatabaseError:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _install_adapter_hook(
    *,
    codex_home: Path,
    fixture_repo: Path,
    store_home: Path | PurePosixPath,
    python_path: Path,
    source_mount: Path,
    authority_url: str | None = None,
    token_file: Path | None = None,
    install_project_hooks: bool = True,
) -> None:
    capture = codex_home / "hook_capture.py"
    log_path = store_home / "lifecycle" / "hook-responses.jsonl"
    config_path = store_home / "config.toml"
    capture.write_text(
        "\n".join(
            [
                "import json, os, subprocess, sys",
                "from pathlib import Path",
                "payload = sys.stdin.read()",
                "event = json.loads(payload)",
                "env = os.environ.copy()",
                f"env['PYTHONPATH'] = {str(source_mount)!r}",
                f"env['AGENT_MEMORY_BRIDGE_HOME'] = {str(store_home)!r}",
                f"env['AGENT_MEMORY_BRIDGE_CONFIG'] = {str(config_path)!r}",
                "env.pop('AGENT_MEMORY_BRIDGE_AUTHORITY_URL', None)"
                if authority_url is None
                else f"env['AGENT_MEMORY_BRIDGE_AUTHORITY_URL'] = {authority_url!r}",
                "env.pop('AGENT_MEMORY_BRIDGE_HTTP_TOKEN_FILE', None)"
                if token_file is None
                else f"env['AGENT_MEMORY_BRIDGE_HTTP_TOKEN_FILE'] = {str(token_file)!r}",
                f"completed = subprocess.run([{str(python_path)!r}, '-m', 'agent_mem_bridge', 'lifecycle-hook'], input=payload, text=True, capture_output=True, env=env)",
                f"log = Path({str(log_path)!r})",
                "log.parent.mkdir(parents=True, exist_ok=True)",
                "with log.open('a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps({'session_id': event.get('session_id'), 'hook_event_name': event.get('hook_event_name'), 'returncode': completed.returncode, 'stdout': completed.stdout}) + chr(10))",
                "sys.stdout.write(completed.stdout)",
                "raise SystemExit(completed.returncode)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    command = shlex.join(["/usr/bin/python3", str(capture)])
    if not install_project_hooks:
        return
    document = {
        "description": "Optional AMB lifecycle activation for one benchmark run.",
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [{"type": "command", "command": command, "timeout": 10}],
                }
            ],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}],
            "PreCompact": [
                {"matcher": "manual|auto", "hooks": [{"type": "command", "command": command, "timeout": 10}]}
            ],
        },
    }
    hook_dir = fixture_repo / ".codex"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "hooks.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _adapter_records(store_home: Path) -> list[dict]:
    path = store_home / "lifecycle" / "activation-evidence.jsonl"
    records: list[dict] = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    contexts: dict[tuple[str, str], deque[str]] = defaultdict(deque)
    legacy_contexts: deque[str] = deque()
    log_path = store_home / "lifecycle" / "hook-responses.jsonl"
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            stdout = item.get("stdout") if isinstance(item, dict) else ""
            try:
                payload = json.loads(stdout) if isinstance(stdout, str) and stdout.strip() else {}
            except json.JSONDecodeError:
                payload = {}
            specific = payload.get("hookSpecificOutput") if isinstance(payload, dict) else None
            context = specific.get("additionalContext") if isinstance(specific, dict) else ""
            context = context if isinstance(context, str) else ""
            if isinstance(item, dict) and isinstance(item.get("session_id"), str) and item.get("hook_event_name"):
                contexts[(item["session_id"], item["hook_event_name"])].append(context)
            else:
                # Older serial collector logs contain one response per event,
                # including empty responses. Never pair only nonempty contexts
                # with recall rows: compaction can inject context without recall.
                legacy_contexts.append(context)
    for record in records:
        responses = contexts[(str(record.get("session_id") or ""), str(record.get("hook_event_name") or ""))]
        context = responses.popleft() if responses else legacy_contexts.popleft() if legacy_contexts else ""
        if context or record.get("recall_invoked") is True:
            record["result_text"] = context
    return records


def _adapter_observation(store_home: Path) -> dict[str, bool]:
    return {"loaded": any(record.get("adapter_loaded") is True for record in _adapter_records(store_home))}


def _adapter_evidence(store_home: Path) -> list[dict]:
    return _adapter_records(store_home)


def main(argv: list[str] | None = None) -> int:
    args = canonical._parser().parse_args(argv)
    if args.command == "collect-codex":
        return collect_codex(
            args.case_id,
            args.out,
            args.model,
            args.timeout,
            args.pack,
            args.condition,
            args.adapter_backend,
            args.measurement_revision,
        )
    return canonical.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
