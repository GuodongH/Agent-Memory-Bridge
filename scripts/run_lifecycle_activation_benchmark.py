from __future__ import annotations
# ruff: noqa: E402, I001

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from _source_imports import ensure_source_root

ensure_source_root()

from tools.evidence._temporary_store import ScopedTemporaryMemoryStore
from tools.evidence.lifecycle_activation import (
    ROOT,
    assess_fixture_access,
    bind_fixture_namespace,
    build_allowlisted_filesystem_argv,
    build_codex_collect_argv,
    find_bwrap,
    git_commit_fixture,
    model_provider_override,
    check_pack,
    load_pack,
    materialize_fixture,
    memory_only_markers,
    parse_codex_exec_jsonl,
    prepare_collector_layout,
    prepare_governed_fixture,
    render_collector_codex_preamble,
    render_host_plan,
    render_isolated_codex_config,
    render_local_proxy_mcp_stanza,
    render_text,
    sandbox_for_case,
    score_pack,
    scorer_sha256,
    seed_case_memories,
    verify_freeze,
)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        report = check_pack()
        print(
            json.dumps(
                {"ok": report["ok"], "problems": report["problems"], "case_count": report["case_count"]}, indent=2
            )
        )
        return 0 if report["ok"] else 1
    if args.command in {"print-host-plan", "prepare-host"} and args.host == "opencode":
        if args.out is None:
            print("opencode prepare-host requires --out", file=sys.stderr)
            return 2
        pack = load_pack(version="v2")
        case = next((item for item in pack["cases"] if item["id"] == args.case_id), None)
        if case is None:
            print(f"unknown case {args.case_id}", file=sys.stderr)
            return 2
        prepared = prepare_governed_fixture(case, args.out, host="opencode", model=args.model)
        print(prepared["command"])
        return 0
    if args.command == "print-host-plan":
        print(render_host_plan(load_pack(version=args.pack), args.host, args.case_id), end="")
        return 0
    if args.command == "score":
        return _score(args.observations, args.format, args.report_path, args.pack)
    if args.command == "collect-codex":
        return collect_codex(args.case_id, args.out, args.model, args.timeout, args.pack, args.condition)
    parser.error(f"unsupported command {args.command}")
    return 2


def _bridge_python() -> Path:
    candidate = Path.home() / ".local/share/agent-memory-bridge/client-venv/bin/python"
    if not candidate.is_file():
        raise RuntimeError("local AMB client python is unavailable")
    return candidate


def _require_bwrap() -> Path:
    found = find_bwrap()
    if found is None:
        raise RuntimeError("bwrap is unavailable; refusing an unisolated host run")
    return found


def _codex_native() -> Path:
    matches = sorted(
        (Path.home() / ".nvm/versions/node").glob(
            "*/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/*/bin/codex"
        )
    )
    if matches:
        return matches[-1]
    found = shutil.which("codex")
    if not found:
        raise RuntimeError("codex binary is unavailable")
    return Path(found)


def _node_prefix(native: Path) -> Path:
    for parent in native.parents:
        if (parent / "bin" / "node").is_file():
            return parent
    raise RuntimeError("node prefix for the Codex binary is unavailable")


def _model_gateway() -> tuple[str, str]:
    key = os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("CODEX_GATEWAY_BASE_URL", "")
    if not (key and base):
        script = Path.home() / ".local/bin/codex-gateway-env"
        if not script.is_file():
            raise RuntimeError("local Codex model gateway is unavailable")
        completed = subprocess.run(
            [
                "bash",
                "-lc",
                'eval "$("$HOME/.local/bin/codex-gateway-env")"; printf "%s\n%s" "$OPENAI_API_KEY" "$CODEX_GATEWAY_BASE_URL"',
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        lines = [line for line in completed.stdout.splitlines() if line]
        if completed.returncode != 0 or len(lines) < 2:
            raise RuntimeError("local Codex model gateway environment is incomplete")
        key, base = lines[0], lines[1]
    model_provider_override(base)
    return key, base


_PROBE_SOURCE = r"""
import json, os, subprocess
markers = [item.encode() for item in json.loads(os.environ["LIFECYCLE_PROBE_MARKERS"]) if item]
absent = json.loads(os.environ["LIFECYCLE_ABSENT_PATHS"])
scan_roots = json.loads(os.environ["LIFECYCLE_SCAN_ROOTS"])
visible = [path for path in absent if os.path.lexists(path)]
hits = []
for root in scan_roots:
    if not os.path.exists(root):
        continue
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path):
                    continue
                size = os.path.getsize(path)
                if size == 0 or size > 8_000_000:
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if any(needle in data for needle in markers):
                hits.append(path)
nested = {"ok": True, "error": ""}
bwrap = os.environ.get("LIFECYCLE_BWRAP", "")
target = os.environ.get("LIFECYCLE_NESTED_ABSENT", "")
if bwrap and target:
    completed = subprocess.run(
        [
            bwrap, "--die-with-parent", "--unshare-user", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--", "/usr/bin/python3", "-c",
            "import os, sys; raise SystemExit(0 if not os.path.lexists(sys.argv[1]) else 1)",
            target,
        ],
        text=True, capture_output=True, check=False,
    )
    nested = {"ok": completed.returncode == 0, "error": (completed.stderr or "")[-300:]}
print(json.dumps({"visible": visible, "hits": hits, "nested": nested}))
"""


def _probe_isolation(
    *,
    bwrap: Path,
    ro_binds: list[tuple[Path, Path]],
    rw_binds: list[tuple[Path, Path]],
    protected: list[Path],
    markers: list[str],
    scan_roots: list[Path],
    absent: list[Path],
) -> dict:
    argv = build_allowlisted_filesystem_argv(
        bwrap=bwrap,
        ro_binds=ro_binds,
        rw_binds=rw_binds,
        command=["/usr/bin/python3", "-c", _PROBE_SOURCE],
        protected_paths=protected,
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LIFECYCLE_PROBE_MARKERS": json.dumps(markers),
        "LIFECYCLE_ABSENT_PATHS": json.dumps([str(path) for path in absent]),
        "LIFECYCLE_SCAN_ROOTS": json.dumps([str(path) for path in scan_roots]),
        "LIFECYCLE_BWRAP": str(bwrap),
        "LIFECYCLE_NESTED_ABSENT": str(ROOT),
    }
    completed = subprocess.run(argv, env=env, text=True, capture_output=True, check=False, timeout=90)
    if completed.returncode != 0 or not completed.stdout.strip():
        detail = _sanitize(completed.stderr or completed.stdout)
        raise RuntimeError(f"isolation preflight could not start: {detail[-300:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_fixture_http(store_home: Path, python_path: Path) -> tuple[subprocess.Popen[str], str, Path]:
    token_path = store_home / "http-token"
    token_path.write_text(secrets.token_hex(32), encoding="utf-8")
    token_path.chmod(0o600)
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_MEMORY_BRIDGE_") and key != "PYTHONPATH"
    }
    env["AGENT_MEMORY_BRIDGE_HOME"] = str(store_home)
    env["AGENT_MEMORY_BRIDGE_CONFIG"] = str(store_home / "config.toml")
    process = subprocess.Popen(
        [
            str(python_path),
            "-m",
            "agent_mem_bridge",
            "serve-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--path",
            "/mcp",
            "--token-file",
            str(token_path),
            "--allowed-host",
            "127.0.0.1:*",
            "--allowed-host",
            "localhost:*",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 15
    while time.time() < deadline:
        if process.poll() is not None:
            error = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"fixture HTTP server exited: {_sanitize(error)[:400]}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                body = response.read(2000)
            if b'"status": "ok"' in body or b'"status":"ok"' in body:
                return process, url, token_path
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError("fixture HTTP server did not become ready")


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def collect_codex(
    case_id: str,
    out_dir: Path,
    model: str,
    timeout: float,
    pack_version: str = "v1",
    condition: str = "plain_mcp_baseline",
) -> int:
    if condition == "adapter_enabled" and pack_version != "v2":
        print("adapter collection requires the frozen v2 pack", file=sys.stderr)
        return 2
    if condition not in {"plain_mcp_baseline", "adapter_enabled"}:
        print(f"unsupported condition {condition}", file=sys.stderr)
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
        bind_fixture_namespace(case, fixture_repo, store_home)
    else:
        config_path.write_text('[bridge]\ndb_path = "fixture-store.sqlite"\n', encoding="utf-8")
        if case["fixture"]["amb_mode"] == "available":
            store = ScopedTemporaryMemoryStore(store_home / "fixture-store.sqlite", store_home / "logs")
            try:
                seed_case_memories(store, case)
            finally:
                store.close()
    codex_home.mkdir(parents=True, exist_ok=True)
    codex_home.chmod(0o700)
    server: subprocess.Popen[str] | None = None
    try:
        if condition == "adapter_enabled":
            bridge_python = _bridge_python()
            _install_adapter_hook(
                codex_home=codex_home,
                fixture_repo=fixture_repo,
                store_home=store_home,
                python_path=bridge_python,
                source_mount=Path("/opt/amb-lifecycle-src"),
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
        (codex_home / "config.toml").write_text(preamble + codex_config, encoding="utf-8")
        native = _codex_native()
        node_prefix = _node_prefix(native)
        venv = _bridge_python().parent.parent
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
        ro_binds = [(node_prefix, node_prefix), (venv, venv)]
        rw_binds = [(codex_home, codex_home), (layout["workspace_parent"], layout["workspace_parent"])]
        scan_roots = [node_prefix, venv, fixture_repo, codex_home]
        absent = [ROOT, Path.home() / ".codex", Path.home() / ".cache"]
        if condition == "adapter_enabled":
            ro_binds.append((ROOT / "src", source_mount))
            rw_binds.append((store_home, store_home))
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
        started = time.perf_counter()
        env = {
            "PATH": f"{node_prefix / 'bin'}:/usr/bin:/bin",
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home),
            "TMPDIR": "/tmp",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "USER": os.environ.get("USER", "user"),
            "LOGNAME": os.environ.get("USER", "user"),
            "TERM": "xterm",
            "OPENAI_API_KEY": api_key,
        }
        argv = build_codex_collect_argv(
            sandbox=sandbox_for_case(case_id),
            model=model,
            fixture_repo=fixture_repo,
            output_path=fixture_repo.parent / "last-message.md",
            prompt=case["prompt"],
            codex_bin=str(native),
            config_overrides=[override],
            condition=condition,
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
        secrets = [api_key]
        token_path = codex_home / "proxy-token"
        if token_path.is_file():
            secrets.append(token_path.read_text(encoding="utf-8").strip())
        stdout_path.write_text(_redact(stdout, secrets), encoding="utf-8")
        (out_dir / "stderr.txt").write_text(_redact(stderr, secrets), encoding="utf-8")
        parsed = parse_codex_exec_jsonl(stdout, interesting_paths=list(case["fixture"]["files"]))
        fixture_access = assess_fixture_access(
            parsed["commands"],
            markers=markers,
            hidden_paths=[str(store_home)],
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
                "adapter": _adapter_observation(store_home) if condition == "adapter_enabled" else {"loaded": False},
                "adapter_evidence": _adapter_evidence(store_home) if condition == "adapter_enabled" else [],
            },
            secrets,
        )
        (out_dir / "observation.json").write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
        report = score_pack(pack, [observation], freeze=freeze)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(render_text(report), end="")
        if returncode is None:
            return 1
        return 0 if returncode == 0 or observation["trace_complete"] else returncode
    finally:
        _stop_process(server)


def _install_adapter_hook(
    *,
    codex_home: Path,
    fixture_repo: Path,
    store_home: Path,
    python_path: Path,
    source_mount: Path,
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
                "env = os.environ.copy()",
                f"env['PYTHONPATH'] = {str(source_mount)!r}",
                f"env['AGENT_MEMORY_BRIDGE_HOME'] = {str(store_home)!r}",
                f"env['AGENT_MEMORY_BRIDGE_CONFIG'] = {str(config_path)!r}",
                "env.pop('AGENT_MEMORY_BRIDGE_AUTHORITY_URL', None)",
                f"completed = subprocess.run([{str(python_path)!r}, '-m', 'agent_mem_bridge', 'lifecycle-hook'], input=payload, text=True, capture_output=True, env=env)",
                f"log = Path({str(log_path)!r})",
                "log.parent.mkdir(parents=True, exist_ok=True)",
                "with log.open('a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps({'returncode': completed.returncode, 'stdout': completed.stdout}) + '\\n')",
                "sys.stdout.write(completed.stdout)",
                "raise SystemExit(completed.returncode)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    command = f"/usr/bin/python3 {capture}"
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
    texts: list[str] = []
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
            if isinstance(context, str) and context:
                texts.append(context)
    text_index = 0
    for record in records:
        if record.get("recall_invoked") is True:
            record["result_text"] = texts[text_index] if text_index < len(texts) else ""
            text_index += 1
    return records


def _adapter_observation(store_home: Path) -> dict[str, bool]:
    return {"loaded": any(record.get("adapter_loaded") is True for record in _adapter_records(store_home))}


def _adapter_evidence(store_home: Path) -> list[dict]:
    return _adapter_records(store_home)


def _score(observations_path: Path, output_format: str, report_path: Path | None, pack_version: str = "v1") -> int:
    freeze = verify_freeze(version=pack_version)
    if not freeze["ok"]:
        print(json.dumps({"ok": False, "problems": ["freeze mismatch"], "mismatches": freeze["mismatches"]}, indent=2))
        return 1
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    if not isinstance(observations, list):
        print("observations must be a JSON list", file=sys.stderr)
        return 2
    report = score_pack(load_pack(version=pack_version), observations, freeze=freeze)
    rendered = render_text(report) if output_format == "text" else json.dumps(report, indent=2) + "\n"
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def _command_version(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    return (completed.stdout or completed.stderr).strip().splitlines()[0] if completed.returncode == 0 else "unknown"


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _redact_obj(value, secrets: list[str]):
    if isinstance(value, str):
        return _redact(value, secrets)
    if isinstance(value, list):
        return [_redact_obj(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_obj(item, secrets) for key, item in value.items()}
    return value


def _sanitize(text: str) -> str:
    redacted = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ("authorization", "bearer", "api_key", "secret", "http-token")):
            redacted.append("[redacted]")
        else:
            redacted.append(line)
    return "\n".join(redacted[-200:]) + ("\n" if redacted else "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check or score the frozen AMB lifecycle activation benchmark.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Verify the freeze, case contract, and embedded probes.")
    plan = subparsers.add_parser("print-host-plan", help="Print an isolated host command without running it.")
    plan.add_argument("--host", choices=("codex", "opencode"), required=True)
    plan.add_argument("--case-id", default="known-project-gotcha")
    plan.add_argument("--pack", choices=("v1", "v2"), default="v1")
    plan.add_argument("--out", type=Path)
    plan.add_argument("--model", default="grok-4.7-build-fast")
    prepare = subparsers.add_parser("prepare-host", help="Materialize one governed fixture and print its host command.")
    prepare.add_argument("--host", choices=("opencode",), required=True)
    prepare.add_argument("--case-id", default="known-project-gotcha")
    prepare.add_argument("--pack", choices=("v2",), default="v2")
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--model", default="grok-4.7-build-fast")
    score = subparsers.add_parser("score", help="Score a JSON list of observations. Refuses a drifted freeze.")
    score.add_argument("--observations", type=Path, required=True)
    score.add_argument("--pack", choices=("v1", "v2"), default="v1")
    score.add_argument("--format", choices=("json", "text"), default="json")
    score.add_argument("--report-path", type=Path)
    collect = subparsers.add_parser("collect-codex", help="Run one isolated Codex case and score its trace.")
    collect.add_argument("--case-id", default="known-project-gotcha")
    collect.add_argument("--pack", choices=("v1", "v2"), default="v1")
    collect.add_argument("--condition", choices=("plain_mcp_baseline", "adapter_enabled"), default="plain_mcp_baseline")
    collect.add_argument("--out", type=Path, required=True)
    collect.add_argument("--model", default="grok-4.7-build-fast")
    collect.add_argument("--timeout", type=float, default=240)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
