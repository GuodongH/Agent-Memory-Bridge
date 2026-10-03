"""Score frozen live-host observations for AMB lifecycle activation.

This module does not launch a host, open a network database, or add an MCP
tool. Synthetic probe results stay synthetic. A missing trace is inconclusive
or not run, never a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tools.evidence.lifecycle_activation_grade import (
    AMB_TOOL_NAMES,
    OBSERVATION_SCHEMA,
    REQUIRED_CASE_IDS,
    host_lanes,  # noqa: F401
    score_observation,
    score_pack,  # noqa: F401
    scorer_sha256,  # noqa: F401
)

ROOT = Path(__file__).resolve().parents[2]
PACK_PATH = ROOT / "benchmark" / "lifecycle-activation-v1.json"
RUBRIC_PATH = ROOT / "benchmark" / "lifecycle-activation-v1.md"
FREEZE_PATH = ROOT / "benchmark" / "lifecycle-activation-v1.sha256"

PACK_SCHEMA = "amb.lifecycle-activation-benchmark.v1"
OPENCODE_STORE_MOUNT = "/opt/amb-lifecycle-store"
OPENCODE_SOURCE_MOUNT = "/opt/amb-lifecycle-src"
PLAN_LOOPBACK_MCP_URL = "http://127.0.0.1:9/mcp"
INDIRECT_FORBIDDEN_WORDS = re.compile(r"\b(?:memory|remember|recall|amb)\b", re.IGNORECASE)
PROBE_EXPECTATIONS = {
    "adequate_good": "PASS",
    "bad": "FAIL",
    "unavailable_evidence": "INCONCLUSIVE",
}


PACK_VERSIONS = {
    "v1": {
        "schema": "amb.lifecycle-activation-benchmark.v1",
        "pack": "benchmark/lifecycle-activation-v1.json",
        "rubric": "benchmark/lifecycle-activation-v1.md",
        "freeze": "benchmark/lifecycle-activation-v1.sha256",
    },
    "v2": {
        "schema": "amb.lifecycle-activation-benchmark.v2",
        "pack": "benchmark/lifecycle-activation-v2.json",
        "rubric": "benchmark/lifecycle-activation-v2.md",
        "freeze": "benchmark/lifecycle-activation-v2.sha256",
    },
}


def load_pack(path: Path | None = None, version: str = "v1") -> dict[str, Any]:
    if version not in PACK_VERSIONS:
        raise ValueError(f"unsupported lifecycle pack {version}")
    pack_path = path or (ROOT / PACK_VERSIONS[version]["pack"])
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    if not isinstance(pack, dict) or not isinstance(pack.get("cases"), list):
        raise ValueError("lifecycle activation pack must be an object with cases")
    return pack


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_freeze(root: Path | None = None, version: str = "v1") -> dict[str, Any]:
    if version not in PACK_VERSIONS:
        raise ValueError(f"unsupported lifecycle pack {version}")
    project_root = root or ROOT
    freeze_path = project_root / PACK_VERSIONS[version]["freeze"]
    expected: dict[str, str] = {}
    for line in freeze_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split(maxsplit=1)
        expected[relative.strip()] = digest.strip()
    actual: dict[str, str] = {}
    mismatches: list[str] = []
    for relative, digest in expected.items():
        candidate = project_root / relative
        if not candidate.is_file():
            mismatches.append(relative)
            continue
        seen = file_sha256(candidate)
        actual[relative] = seen
        if seen != digest:
            mismatches.append(relative)
    return {"ok": not mismatches, "expected": expected, "actual": actual, "mismatches": mismatches}


def validate_pack(pack: dict[str, Any], version: str = "v1") -> list[str]:
    problems: list[str] = []
    cases = pack.get("cases")
    if version not in PACK_VERSIONS:
        return [f"unsupported pack {version}"]
    if pack.get("schema") != PACK_VERSIONS[version]["schema"]:
        problems.append("schema")
    if not isinstance(cases, list):
        return ["cases"]
    ids = [case.get("id") for case in cases]
    if tuple(ids) != REQUIRED_CASE_IDS:
        problems.append("required case ids drifted")
    if len(set(ids)) != len(ids):
        problems.append("duplicate case id")
    if not any(case.get("class") == "negative_control" for case in cases):
        problems.append("missing negative control")
    for case in cases:
        problems.extend(_validate_case(case))
        if version == "v2":
            problems.extend(_namespace_leaks(case))
    if "quality_score" in pack or "overall_score" in pack:
        problems.append("pack collapses metrics")
    return problems


def _namespace_leaks(case: dict[str, Any]) -> list[str]:
    namespace = case.get("expected_namespace")
    if not isinstance(namespace, str) or not namespace:
        return []
    problems: list[str] = []
    if namespace in str(case.get("prompt") or ""):
        problems.append(f"{case.get('id')} namespace is in the prompt")
    files = (case.get("fixture") or {}).get("files") or {}
    for relative, content in files.items():
        if namespace in str(content):
            problems.append(f"{case.get('id')} namespace is in {relative}")
    return problems


def check_pack(root: Path | None = None) -> dict[str, Any]:
    project_root = root or ROOT
    problems: list[str] = []
    freezes: dict[str, dict[str, Any]] = {}
    packs: dict[str, dict[str, Any]] = {}
    for version in ("v1", "v2"):
        freeze = verify_freeze(project_root, version)
        freezes[version] = freeze
        if not freeze["ok"]:
            problems.append(f"{version} freeze mismatch")
            continue
        pack = load_pack(project_root / PACK_VERSIONS[version]["pack"])
        packs[version] = pack
        problems.extend(f"{version}: {item}" for item in validate_pack(pack, version))
        problems.extend(f"{version}: {item}" for item in score_embedded_probes(pack))
    if "v2" in packs:
        with tempfile.TemporaryDirectory(prefix="lifecycle-prepare-") as temporary:
            prepared = prepare_governed_fixture(
                next(case for case in packs["v2"]["cases"] if case["id"] == "known-project-gotcha"),
                Path(temporary),
                host="opencode",
            )
            command = prepared["command"]
            plugin_text = prepared["plugin_path"].read_text(encoding="utf-8")
            config_text = prepared["client_config_path"].read_text(encoding="utf-8")
            wrapper_text = prepared["wrapper_path"].read_text(encoding="utf-8")
            store = str(prepared["bridge_home"])
            plain_tail = _argv_tail(prepared["plain_sandbox_argv"])
            adapter_tail = _argv_tail(prepared["adapter_sandbox_argv"])
            plain_argv = "\n".join(prepared["plain_sandbox_argv"])
            adapter_argv = "\n".join(prepared["adapter_sandbox_argv"])
            env_text = json.dumps(prepared["process_env"])
            if (
                "collect-opencode" not in command
                or "--pure" in command.split()
                or store in command
                or "fixture-store.sqlite" in command
                or "AGENT_MEMORY_BRIDGE_HOME" in command
                or "192.168." in command
                or ":58080" in command
                or "opencode run" in command
                or "session.created" not in plugin_text
                or "experimental.session.compacting" not in plugin_text
                or "lifecycle-hook" not in plugin_text
                or "AGENT_MEMORY_BRIDGE_HOME" in config_text
                or store in config_text
                or "fixture-store.sqlite" in config_text
                or "192.168." in config_text
                or "58080" in config_text
                or '"type": "remote"' not in config_text
                or "http://127.0.0.1:" not in config_text
                or "/mcp" not in config_text
                or '"oauth": false' not in config_text
                or '"task": "deny"' not in config_text
                or OPENCODE_STORE_MOUNT not in wrapper_text
                or store in wrapper_text
                or "fixture-store.sqlite" in wrapper_text
                or prepared["wrapper_path"].is_relative_to(prepared["fixture_repo"])
                or "AGENT_MEMORY_BRIDGE_HOME" in env_text
                or store in env_text
                or "fixture-store.sqlite" in env_text
                or store in plain_tail
                or "AGENT_MEMORY_BRIDGE_HOME" in plain_tail
                or "fixture-store.sqlite" in plain_tail
                or "--pure" in plain_tail.split()
                or "opencode" not in plain_tail.split()
                or "run" not in plain_tail.split()
                or store in adapter_tail
                or "AGENT_MEMORY_BRIDGE_HOME" in adapter_tail
                or "fixture-store.sqlite" in adapter_tail
                or OPENCODE_STORE_MOUNT not in adapter_argv
                or store not in adapter_argv
                or OPENCODE_STORE_MOUNT in plain_argv
                or store in plain_argv
                or not prepared["fixture_repo"].joinpath(".git").exists()
                or prepared["bound_repository_id"] != prepared["repository_id"]
                or prepared["bridge_home"].parent == prepared["fixture_repo"].parent
            ):
                problems.append("opencode plan is not an isolated runnable path")
    else:
        problems.append("opencode plan is not an isolated runnable path")
    pack = packs.get("v1")
    return {
        "ok": not problems,
        "problems": problems,
        "freeze": freezes.get("v1"),
        "freezes": freezes,
        "case_count": 0 if pack is None else len(pack["cases"]),
    }


def score_embedded_probes(pack: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for case in pack["cases"]:
        probes = case.get("probes") or {}
        for probe_name, expected in PROBE_EXPECTATIONS.items():
            probe = probes.get(probe_name)
            if not isinstance(probe, dict):
                problems.append(f"{case['id']} missing {probe_name}")
                continue
            result = score_observation(case, expand_probe(case, probe))
            if result["status"] != expected:
                problems.append(f"{case['id']} {probe_name} scored {result['status']} not {expected}")
    return problems


def expand_probe(case: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "schema": OBSERVATION_SCHEMA,
        "case_id": case["id"],
        "trace_complete": True,
        "execution_kind": "synthetic",
        "host": {"id": "synthetic", "version": "probe", "model": "none"},
        "amb_available": case["fixture"]["amb_mode"] == "available",
        "namespace_bound": case.get("expected_namespace"),
        "tool_calls": [],
        "final_text": "",
        "repo_paths_read": [],
        "latency_ms": 1,
        "input_tokens": None,
        "output_tokens": None,
    }
    observation.update(probe)
    observation["schema"] = OBSERVATION_SCHEMA
    observation["case_id"] = case["id"]
    observation["execution_kind"] = "synthetic"
    return observation


def render_text(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "Lifecycle activation v1",
        f"instrument_pass={report['instrument']['pass']}",
        f"codex={report['host_lanes']['codex']['status']}",
        f"codex_contaminated={len(report['host_lanes']['codex'].get('contaminated_case_ids') or [])}",
        f"opencode={report['host_lanes']['opencode']['status']}",
    ]
    for name in (
        "required_recall_hit_rate",
        "false_activation_rate",
        "namespace_resolution_correct_rate",
        "useful_hit_rate_given_recall",
        "no_hit_correct_rate",
        "unavailable_vs_empty_correct_rate",
    ):
        rate = metrics[name]
        lines.append(
            f"{name}={rate['rate']} numerator={rate['numerator']} complement={rate['complement']} "
            f"inconclusive={rate['inconclusive']} not_run={rate['not_run']}"
        )
    lines.append(f"stale_memory_misuse_count={metrics['stale_memory_misuse_count']}")
    lines.append(f"repeated_or_unnecessary_recall_count={metrics['repeated_or_unnecessary_recall_count']}")
    for result in report["results"]:
        condition = result.get("condition")
        condition_label = f" {condition}" if isinstance(condition, str) and condition else ""
        lines.append(f"{result['id']} {result['status']}{condition_label} {','.join(result['reasons'])}")
    return "\n".join(lines) + "\n"


def materialize_fixture(case: dict[str, Any], dest: Path) -> None:
    root = dest.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in case["fixture"]["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"fixture path escapes destination: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def seed_case_memories(store: Any, case: dict[str, Any]) -> None:
    for memory in case["fixture"]["memories"]:
        store.store(
            namespace=memory["namespace"],
            content=memory["content"],
            title=memory.get("title"),
            tags=list(memory.get("tags") or []),
            kind="memory",
        )


EDIT_CASE_IDS = frozenset({"isolated-typo", "repeated-recall"})


def sandbox_for_case(case_id: str) -> str:
    return "workspace-write" if case_id in EDIT_CASE_IDS else "read-only"


def memory_only_markers(case: dict[str, Any]) -> list[str]:
    files = case.get("fixture", {}).get("files", {})
    visible = "\n".join(str(text) for text in files.values()) + "\n" + str(case.get("prompt") or "")
    markers: list[str] = []
    for key in ("useful_marker", "stale_marker", "distractor_marker"):
        marker = str(case.get(key) or "")
        if marker and marker not in visible:
            markers.append(marker)
    return markers


def assess_fixture_access(
    commands: list[dict[str, Any]],
    *,
    markers: list[str],
    hidden_paths: list[str],
) -> dict[str, Any]:
    """Flag model filesystem access to the hidden store. MCP tool results are not commands."""
    signals: list[str] = []
    needles = [item for item in hidden_paths if item]
    needles.extend(["fixture-store.sqlite", "recall-receipt-secret.json"])
    for command in commands:
        text = str(command.get("command") or "")
        output = str(command.get("output") or "")
        blob = text + "\n" + output
        if any(needle in blob for needle in needles):
            signals.append("collector_path")
        if any(marker and marker in output for marker in markers):
            signals.append("marker_in_command_output")
    return {
        "direct_read": bool(signals),
        "signals": sorted(set(signals)),
        "command_count": len(commands),
    }


def model_provider_override(base_url: str) -> str:
    """Codex model-provider override. This is not the AMB server route."""
    if "://" not in base_url or base_url.startswith(("file:", "javascript:")):
        raise ValueError("model gateway URL is unusable")
    if ":58080" in base_url or "/mcp" in base_url:
        raise ValueError("model gateway points at production AMB")
    return f"model_providers.local.base_url={_quote_toml_basic(base_url)}"


def build_codex_collect_argv(
    *,
    sandbox: str,
    model: str,
    fixture_repo: Path,
    output_path: Path,
    prompt: str,
    codex_bin: str = "codex",
    config_overrides: list[str] | None = None,
    condition: str = "plain_mcp_baseline",
) -> list[str]:
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError(f"unsupported sandbox {sandbox}")
    if condition not in {"plain_mcp_baseline", "adapter_enabled"}:
        raise ValueError(f"unsupported collector condition {condition}")
    argv = [codex_bin, "exec"]
    for override in config_overrides or []:
        if ":58080" in override or "--add-dir" in override or "fixture-store.sqlite" in override:
            raise ValueError("collector override points at production AMB or the fixture store")
        argv.extend(["-c", override])
    mode_flags = ["--disable", "plugins", "--disable", "memories"]
    if condition == "adapter_enabled":
        mode_flags = ["--dangerously-bypass-hook-trust", "--disable", "memories"]
    argv.extend(
        [
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            *mode_flags,
            "-s",
            sandbox,
            "-C",
            str(fixture_repo),
            "-m",
            model,
            "-c",
            'model_reasoning_effort="low"',
            "-o",
            str(output_path),
            prompt,
        ]
    )
    return argv


def render_local_proxy_mcp_stanza(*, python_path: str, url: str, token_file: str) -> str:
    """Codex MCP stanza for a localhost proxy. It must not point at the fixture database."""
    if any(marker in url or marker in token_file or marker in python_path for marker in ("192.168.", "58080")):
        raise RuntimeError("local proxy stanza includes a production route")
    args = ["-m", "agent_mem_bridge", "proxy", "--url", url, "--token-file", token_file]
    rendered = "\n".join(
        [
            "[mcp_servers.agentMemoryBridge]",
            f"command = {_quote_toml_basic(python_path)}",
            f"args = [{', '.join(_quote_toml_basic(item) for item in args)}]",
            "",
        ]
    )
    if "AGENT_MEMORY_BRIDGE_HOME" in rendered or "fixture-store.sqlite" in rendered:
        raise RuntimeError("local proxy stanza exposes the fixture store")
    return rendered


def mount_would_expose(source: Path, protected: Path) -> bool:
    """True when mounting source would also reveal a protected file or directory."""
    source_resolved = source.resolve()
    protected_resolved = protected.resolve()
    if source_resolved == Path("/"):
        return True
    return protected_resolved == source_resolved or protected_resolved.is_relative_to(source_resolved)


def base_sandbox_spec() -> dict[str, list[tuple[str, str]]]:
    """System mounts a command needs, without the caller home directory or /tmp."""
    ro_binds: list[tuple[str, str]] = []
    if Path("/usr").is_dir():
        ro_binds.append(("/usr", "/usr"))
    if Path("/etc").is_dir():
        ro_binds.append(("/etc", "/etc"))
    symlinks: list[tuple[str, str]] = []
    for name in ("bin", "lib", "lib64", "sbin"):
        path = Path("/") / name
        if path.is_symlink():
            symlinks.append((os.readlink(path), f"/{name}"))
        elif path.is_dir():
            ro_binds.append((f"/{name}", f"/{name}"))
    resolv = Path("/etc/resolv.conf")
    if resolv.is_symlink():
        target = resolv.resolve()
        if target.is_file():
            ro_binds.append((str(target), str(target)))
    return {"ro_binds": ro_binds, "symlinks": symlinks}


def find_bwrap() -> Path | None:
    found = shutil.which("bwrap")
    if found:
        return Path(found)
    matches = sorted(
        (Path.home() / ".nvm/versions/node").glob(
            "*/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/*/codex-resources/bwrap"
        )
    )
    return matches[-1] if matches else None


def build_allowlisted_filesystem_argv(
    *,
    bwrap: Path,
    ro_binds: list[tuple[Path, Path | str]],
    rw_binds: list[tuple[Path, Path | str]],
    command: list[str],
    protected_paths: list[Path] | None = None,
) -> list[str]:
    """Run a command where only explicit paths are mounted.

    The host root and host /tmp are not mounted. Hiding one more directory on
    top of a full-disk mount is not sufficient, because a read-only host can
    search any path the mount still contains.
    """
    if not command:
        raise ValueError("allowlisted sandbox command is empty")
    protected = list(protected_paths or [])
    spec = base_sandbox_spec()
    sources = [Path(source) for source, _dest in spec["ro_binds"]]
    sources.extend(source for source, _dest in ro_binds)
    sources.extend(source for source, _dest in rw_binds)
    for source in sources:
        if source.resolve() == Path("/"):
            raise ValueError("refusing to mount the host root")
        exposed = [path for path in protected if mount_would_expose(source, path)]
        if exposed:
            raise ValueError("mount would expose a protected benchmark path")
    argv = [str(bwrap), "--die-with-parent", "--unshare-user", "--unshare-pid"]
    for source, dest in spec["ro_binds"]:
        argv.extend(["--ro-bind", source, dest])
    for target, link in spec["symlinks"]:
        argv.extend(["--symlink", target, link])
    argv.extend(["--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc"])
    for source, dest in ro_binds:
        argv.extend(["--ro-bind", str(source), str(dest)])
    for source, dest in rw_binds:
        argv.extend(["--bind", str(source), str(dest)])
    argv.append("--")
    argv.extend(command)
    return argv


def render_collector_codex_preamble(*, catalog_path: str, fixture_repo: str, plugins: bool = False) -> str:
    """Codex config that loads only the isolated MCP stanza appended by the caller."""
    lines = [
        'model_provider = "local"',
        'approval_policy = "never"',
        f"model_catalog_json = {_quote_toml_basic(catalog_path)}",
        "",
        "[model_providers.local]",
        'name = "Local Gateway"',
        'wire_api = "responses"',
        'env_key = "OPENAI_API_KEY"',
        "",
        "[shell_environment_policy]",
        'inherit = "core"',
        "exclude = ["
        '"CODEX_HOME", "OPENAI_API_KEY", "CODEX_GATEWAY_API_KEY", "CODEX_GATEWAY_BASE_URL", '
        '"AGENT_MEMORY_BRIDGE_HOME", "AGENT_MEMORY_BRIDGE_CONFIG", '
        '"AGENT_MEMORY_BRIDGE_DEFAULT_SOURCE_CLIENT", "AGENT_MEMORY_BRIDGE_DEFAULT_CLIENT_TRANSPORT"'
        "]",
        "",
        "[features]",
        "plugins = true" if plugins else "plugins = false",
        "memories = false",
        "",
        f"[projects.{_quote_toml_basic(fixture_repo)}]",
        'trust_level = "trusted"',
        "",
        "",
    ]
    rendered = "\n".join(lines)
    if any(marker in rendered for marker in ("192.168.", "58080", "--add-dir")):
        raise RuntimeError("collector preamble exposes a production route or bridge workspace")
    return rendered


def prepare_collector_layout(*, store_root: Path, client_root: Path, workspace_root: Path) -> dict[str, Path]:
    """Put the checkout, database, and client home in different directory trees."""
    store_root.mkdir(parents=True, exist_ok=True)
    client_root.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)
    roots = {path.resolve() for path in (store_root, client_root, workspace_root)}
    if len(roots) != 3:
        raise ValueError("store, client, and workspace roots must be different directories")
    workspace_parent = Path(tempfile.mkdtemp(prefix="ws-", dir=workspace_root))
    fixture_repo = workspace_parent / "checkout"
    fixture_repo.mkdir()
    store_home = Path(tempfile.mkdtemp(prefix="db-", dir=store_root))
    codex_home = Path(tempfile.mkdtemp(prefix="cx-", dir=client_root))
    fixture_resolved = fixture_repo.resolve()
    for hidden in (store_home, codex_home):
        resolved = hidden.resolve()
        if fixture_resolved.is_relative_to(resolved) or resolved.is_relative_to(fixture_resolved.parent):
            raise ValueError("hidden collector directory is visible from the checkout parent")
    return {
        "fixture_repo": fixture_repo,
        "workspace_parent": workspace_parent,
        "store_home": store_home,
        "codex_home": codex_home,
    }


def _argv_tail(argv: list[str]) -> str:
    if "--" not in argv:
        return ""
    return "\n".join(argv[argv.index("--") + 1 :])


def _reject_store_exposure(text: str, message: str) -> None:
    if any(marker in text for marker in ("AGENT_MEMORY_BRIDGE_HOME", "fixture-store.sqlite", "192.168.", ":58080")):
        raise RuntimeError(message)


def render_opencode_public_command(*, case_id: str, pack: str = "v2", condition: str = "plain_mcp_baseline") -> str:
    """Operator command. It does not carry the fixture store into the model process."""
    if condition not in {"plain_mcp_baseline", "adapter_enabled"}:
        raise ValueError(f"unsupported collector condition {condition}")
    command = (
        "python ./scripts/run_lifecycle_activation_benchmark.py collect-opencode "
        f"--case-id {case_id} --pack {pack} --condition {condition}"
    )
    _reject_store_exposure(command, "opencode collector command exposes the fixture store or production AMB")
    if "--pure" in command.split():
        raise RuntimeError("opencode collector command disables the lifecycle plugin")
    return command


def render_opencode_remote_config(*, url: str, headers: dict[str, str] | None = None) -> str:
    """OpenCode 1.18 loopback MCP config. `task` stays denied so child sessions cannot hide store reads."""
    if not url.startswith("http://127.0.0.1:") or not url.endswith("/mcp"):
        raise RuntimeError("OpenCode MCP URL must be a loopback /mcp endpoint")
    server: dict[str, Any] = {"type": "remote", "url": url, "enabled": True, "oauth": False}
    if headers:
        server["headers"] = dict(headers)
    rendered = (
        json.dumps(
            {"permission": {"task": "deny"}, "mcp": {"agentMemoryBridge": server}},
            indent=2,
        )
        + "\n"
    )
    if '"task": "deny"' not in rendered:
        raise RuntimeError("OpenCode config must deny the task tool")
    _reject_store_exposure(rendered, "OpenCode MCP config exposes the fixture store or production AMB")
    return rendered


def render_opencode_hook_wrapper() -> str:
    """Sandbox-side hook. It can see only the fixed mount, never the host bridge path."""
    store = OPENCODE_STORE_MOUNT
    source = OPENCODE_SOURCE_MOUNT
    lines = [
        "import json, os, subprocess, sys",
        "payload = sys.stdin.read()",
        "env = os.environ.copy()",
        f"env['PYTHONPATH'] = {source!r}",
        f"env['AGENT_MEMORY_BRIDGE_HOME'] = {store!r}",
        f"env['AGENT_MEMORY_BRIDGE_CONFIG'] = {f'{store}/config.toml'!r}",
        "env.pop('AGENT_MEMORY_BRIDGE_AUTHORITY_URL', None)",
        "completed = subprocess.run(",
        "    [sys.executable, '-m', 'agent_mem_bridge', 'lifecycle-hook'],",
        "    input=payload,",
        "    text=True,",
        "    capture_output=True,",
        "    env=env,",
        ")",
        f"log = os.path.join({store!r}, 'lifecycle', 'hook-responses.jsonl')",
        "os.makedirs(os.path.dirname(log), exist_ok=True)",
        "with open(log, 'a', encoding='utf-8') as handle:",
        "    handle.write(json.dumps({'returncode': completed.returncode, 'stdout': completed.stdout}) + '\\n')",
        "sys.stdout.write(completed.stdout)",
        "raise SystemExit(completed.returncode)",
        "",
    ]
    rendered = "\n".join(lines)
    if "fixture-store.sqlite" in rendered or "192.168." in rendered or ":58080" in rendered:
        raise RuntimeError("hook wrapper names the fixture database or production AMB")
    return rendered


def render_opencode_hook_command(python_path: str, wrapper_path: Path) -> list[str]:
    command = [python_path, str(wrapper_path)]
    _reject_store_exposure("\n".join(command), "hook command exposes the fixture store or production AMB")
    return command


def build_opencode_exec_argv(
    *,
    fixture_repo: Path,
    model: str,
    prompt: str,
    opencode_bin: str = "opencode",
) -> list[str]:
    """Argv that runs inside the sandbox, after bwrap's ``--``."""
    argv = [
        opencode_bin,
        "run",
        "--format",
        "json",
        "--auto",
        "--dir",
        str(fixture_repo),
        "--model",
        model,
        "--",
        prompt,
    ]
    if "--pure" in argv:
        raise RuntimeError("opencode exec argv disables the lifecycle plugin")
    _reject_store_exposure("\n".join(argv), "opencode exec argv exposes the fixture store or production AMB")
    return argv


def build_opencode_sandbox_argv(
    *,
    bwrap: Path,
    condition: str,
    store_home: Path,
    client_home: Path,
    workspace_parent: Path,
    source_root: Path,
    command: list[str],
    ro_binds: list[tuple[Path, str]] | None = None,
    rw_binds: list[tuple[Path, str]] | None = None,
    protected_paths: list[Path] | None = None,
) -> list[str]:
    """Plain MCP does not mount the store. Adapter mode mounts it only at the fixed sandbox path."""
    if condition not in {"plain_mcp_baseline", "adapter_enabled"}:
        raise ValueError(f"unsupported collector condition {condition}")
    ro: list[tuple[Path, str]] = list(ro_binds or [])
    rw: list[tuple[Path, str]] = [
        *list(rw_binds or []),
        (client_home, str(client_home)),
        (workspace_parent, str(workspace_parent)),
    ]
    protected = list(protected_paths or [])
    if condition == "adapter_enabled":
        rw.append((store_home, OPENCODE_STORE_MOUNT))
        ro.append((source_root, OPENCODE_SOURCE_MOUNT))
    else:
        protected.append(store_home)
    argv = build_allowlisted_filesystem_argv(
        bwrap=bwrap,
        ro_binds=ro,
        rw_binds=rw,
        command=command,
        protected_paths=protected,
    )
    tail = _argv_tail(argv)
    _reject_store_exposure(tail, "opencode sandbox command exposes the fixture store or production AMB")
    if str(store_home) in tail:
        raise RuntimeError("opencode sandbox command contains the host store path")
    rendered = "\n".join(argv)
    if condition == "plain_mcp_baseline" and (OPENCODE_STORE_MOUNT in rendered or str(store_home) in rendered):
        raise RuntimeError("plain OpenCode sandbox mounts the fixture store")
    return argv


def install_isolated_opencode(checkout: Path, client_home: Path, *, mcp_url: str) -> dict[str, Any]:
    """Copy the repo plugin into the checkout and keep the store path out of opencode.json."""
    plugin_source = ROOT / "adapters" / "opencode" / "amb-lifecycle.js"
    plugin_path = checkout / ".opencode" / "plugins" / plugin_source.name
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(plugin_source, plugin_path)
    homes = {
        "config_home": client_home / "xdg-config",
        "data_home": client_home / "xdg-data",
        "state_home": client_home / "xdg-state",
        "cache_home": client_home / "xdg-cache",
        "opencode_home": client_home / "opencode-home",
    }
    for path in homes.values():
        path.mkdir(parents=True, exist_ok=True)
    wrapper_path = client_home / "amb-lifecycle-wrapper.py"
    wrapper_path.write_text(render_opencode_hook_wrapper(), encoding="utf-8")
    if wrapper_path.resolve().is_relative_to(checkout.resolve()):
        raise RuntimeError("OpenCode hook wrapper is inside the checkout")
    rendered = render_opencode_remote_config(url=mcp_url)
    plugin_text = plugin_path.read_text(encoding="utf-8")
    if (
        "session.created" not in plugin_text
        or "experimental.session.compacting" not in plugin_text
        or "lifecycle-hook" not in plugin_text
    ):
        raise RuntimeError("OpenCode lifecycle plugin is missing a supported entrypoint")
    config_path = checkout / "opencode.json"
    config_path.write_text(rendered, encoding="utf-8")
    process_env = {
        "XDG_CONFIG_HOME": str(homes["config_home"]),
        "XDG_DATA_HOME": str(homes["data_home"]),
        "XDG_STATE_HOME": str(homes["state_home"]),
        "XDG_CACHE_HOME": str(homes["cache_home"]),
        "OPENCODE_TEST_HOME": str(homes["opencode_home"]),
    }
    _reject_store_exposure(json.dumps(process_env), "OpenCode process env exposes the fixture store")
    return {
        **homes,
        "plugin_path": plugin_path,
        "client_config_path": config_path,
        "wrapper_path": wrapper_path,
        "process_env": process_env,
    }


def git_commit_fixture(checkout: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )

    def run(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args],
            cwd=checkout,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(detail or "git fixture setup failed")

    run("init", "-b", "main")
    run("add", "-A")
    run(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "fixture checkout",
    )


def bind_fixture_namespace(case: dict[str, Any], checkout: Path, bridge_home: Path) -> str:
    from agent_mem_bridge.repository_snapshot_store import RepositorySnapshotStore, repository_identity

    identity = repository_identity(checkout)
    repository_id = str(identity["repository_id"])
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "config.toml").write_text('[bridge]\ndb_path = "fixture-store.sqlite"\n', encoding="utf-8")
    if case["fixture"]["amb_mode"] == "available":
        from tools.evidence._temporary_store import ScopedTemporaryMemoryStore

        store = ScopedTemporaryMemoryStore(bridge_home / "fixture-store.sqlite", bridge_home / "logs")
        try:
            seed_case_memories(store, case)
        finally:
            store.close()
    binding = RepositorySnapshotStore(bridge_home / "repository").bind_namespace(
        str(case["expected_namespace"]),
        repository_id,
    )
    if binding["repository_id"] != repository_id:
        raise RuntimeError("fixture binding did not keep the checkout repository id")
    return repository_id


def prepare_governed_fixture(
    case: dict[str, Any],
    dest: Path,
    *,
    host: str,
    model: str = "grok-4.7-build-fast",
    source_root: Path | None = None,
) -> dict[str, Any]:
    if host != "opencode":
        raise ValueError(f"unsupported prepare host {host}")
    destination = dest.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    layout = prepare_collector_layout(
        store_root=destination / "store-root",
        client_root=destination / "client-root",
        workspace_root=destination / "workspace-root",
    )
    checkout = layout["fixture_repo"]
    bridge_home = layout["store_home"]
    client_home = layout["codex_home"]
    materialize_fixture(case, checkout)
    # The discarded port is only an offline plan. collect-opencode rewrites the URL before launch.
    installed = install_isolated_opencode(checkout, client_home, mcp_url=PLAN_LOOPBACK_MCP_URL)
    git_commit_fixture(checkout)
    repository_id = bind_fixture_namespace(case, checkout, bridge_home)
    prompt = str(case["prompt"])
    prompt_path = destination / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    command = render_opencode_public_command(case_id=str(case["id"]))
    exec_argv = build_opencode_exec_argv(fixture_repo=checkout, model=model, prompt=prompt)
    sandbox = {
        "bwrap": destination / "bwrap-plan",
        "store_home": bridge_home,
        "client_home": client_home,
        "workspace_parent": layout["workspace_parent"],
        "source_root": (source_root or (ROOT / "src")).resolve(),
        "command": exec_argv,
    }
    plain_argv = build_opencode_sandbox_argv(condition="plain_mcp_baseline", **sandbox)
    adapter_argv = build_opencode_sandbox_argv(condition="adapter_enabled", **sandbox)
    (destination / "command.txt").write_text(command + "\n", encoding="utf-8")
    return {
        "command": command,
        "fixture_repo": checkout,
        "bridge_home": bridge_home,
        "client_home": client_home,
        "repository_id": repository_id,
        "bound_repository_id": repository_id,
        "prompt_path": prompt_path,
        "plugin_path": installed["plugin_path"],
        "client_config_path": installed["client_config_path"],
        "config_home": installed["config_home"],
        "wrapper_path": installed["wrapper_path"],
        "process_env": installed["process_env"],
        "hook_command": render_opencode_hook_command("/usr/bin/python3", installed["wrapper_path"]),
        "exec_argv": exec_argv,
        "plain_sandbox_argv": plain_argv,
        "adapter_sandbox_argv": adapter_argv,
    }


def render_host_plan(pack: dict[str, Any], host: str, case_id: str) -> str:
    case = next(item for item in pack["cases"] if item["id"] == case_id)
    if host == "codex":
        command = (
            "python ./scripts/run_lifecycle_activation_benchmark.py collect-codex "
            f"--case-id {case['id']} --pack v1 --condition plain_mcp_baseline"
        )
    elif host == "opencode":
        command = render_opencode_public_command(case_id=str(case["id"]), pack="v2")
    else:
        raise ValueError(f"unsupported host {host}")
    return "\n".join(
        [
            f"host={host}",
            f"case_id={case['id']}",
            "isolation=temp bridge home outside the workspace; do not bind a production database",
            "evidence=tool trace, not a prose claim that memory was used",
            f"command={command}",
            "prompt:",
            case["prompt"],
            "",
        ]
    )


def parse_codex_exec_jsonl(
    text: str, interesting_paths: list[str] | None = None, *, measurement_revision: str = "legacy-v2"
) -> dict[str, Any]:
    from tools.evidence.lifecycle_activation_measurement import READ_REVISION, REVISIONS, rg_read_evidence

    if measurement_revision not in REVISIONS:
        raise ValueError(f"unsupported measurement revision {measurement_revision}")
    tool_calls: list[dict[str, Any]] = []
    final_parts: list[str] = []
    repo_paths: list[str] = []
    commands: list[dict[str, str]] = []
    usage: dict[str, Any] = {}
    events = 0
    terminal_event = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        if isinstance(event.get("type"), str):
            terminal_event = str(event["type"])
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        event_usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
        if event_usage:
            usage.update(event_usage)
        if item_type == "agent_message":
            message = item.get("text") or item.get("message") or ""
            if isinstance(message, str) and message.strip():
                final_parts.append(message)
        elif item_type == "mcp_tool_call" and event.get("type") != "item.started":
            tool_calls.append(_tool_call_from_event(item))
        elif item_type in {"command_execution", "file_change"}:
            repo_paths.extend(_paths_from_event(item, interesting_paths or []))
            if item_type == "command_execution" and event.get("type") != "item.started":
                commands.append(
                    {
                        "command": str(item.get("command") or ""),
                        "output": str(item.get("aggregated_output") or item.get("output") or ""),
                    }
                )
    read_evidence = rg_read_evidence(text, interesting_paths or []) if measurement_revision == READ_REVISION else []
    repo_paths.extend(read["path"] for read in read_evidence)
    result = {
        "event_count": events,
        "tool_calls": tool_calls,
        "commands": commands,
        "final_text": "\n".join(final_parts),
        "repo_paths_read": list(dict.fromkeys(repo_paths)),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "terminal_event": terminal_event,
    }
    if measurement_revision == READ_REVISION:
        result["repository_read_evidence"] = read_evidence
    return result


def _opencode_strings(tool_input: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return values


# OpenCode 1.18 model tools. `read` also lists directories. Search tools return
# paths and matching lines; edit/write outputs can echo a legitimate recall.
_OPENCODE_FS_FIELDS: dict[str, tuple[str, ...]] = {
    "read": ("filePath", "filePaths", "path", "paths", "file", "files"),
    "grep": ("path", "paths", "pattern", "include"),
    "glob": ("path", "paths", "pattern"),
    "list": ("path", "paths", "directory", "filePath"),
    "edit": ("filePath", "filePaths", "path", "file"),
    "write": ("filePath", "filePaths", "path", "file"),
    "apply_patch": ("filePath", "path", "patchText", "patch"),
    "multiedit": ("filePath", "filePaths", "path", "patchText", "patch"),
}
_OPENCODE_FS_OUTPUTS = frozenset({"read", "grep", "glob", "list"})


def parse_opencode_run_json(text: str, interesting_paths: list[str] | None = None) -> dict[str, Any]:
    """Normalize `opencode run --format json` JSONL without launching OpenCode.

    `turn.completed` is reserved for a non-error trace that stopped with text.
    A missing step_finish stays incomplete. Filesystem tool paths stay visible
    to the fixture-access gate; repo matching still uses read paths only.
    """
    tool_calls: list[dict[str, Any]] = []
    final_parts: list[str] = []
    repo_paths: list[str] = []
    commands: list[dict[str, str]] = []
    events = 0
    saw_error = False
    step_reason = None
    last_type = None
    input_tokens = None
    output_tokens = None
    interesting = interesting_paths or []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        event_type = event.get("type")
        if isinstance(event_type, str):
            last_type = event_type
        if event_type == "error":
            saw_error = True
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event_type == "text":
            message = part.get("text") if isinstance(part.get("text"), str) else event.get("text")
            if isinstance(message, str) and message.strip():
                final_parts.append(message)
            continue
        if event_type == "step_finish":
            reason = part.get("reason") if isinstance(part.get("reason"), str) else event.get("reason")
            if isinstance(reason, str):
                step_reason = reason
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            if isinstance(tokens.get("input"), int):
                input_tokens = tokens["input"]
            if isinstance(tokens.get("output"), int):
                output_tokens = tokens["output"]
            continue
        if event_type != "tool_use":
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "completed")
        if status not in {"completed", "error"}:
            continue
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        raw_tool = str(part.get("tool") or "")
        output = state.get("output")
        output_text = output if isinstance(output, str) else _extract_text(output)
        if raw_tool in {"bash", "shell"}:
            commands.append({"command": str(tool_input.get("command") or ""), "output": output_text})
            continue
        if raw_tool in _OPENCODE_FS_FIELDS:
            accessed = _opencode_strings(tool_input, _OPENCODE_FS_FIELDS[raw_tool])
            if raw_tool == "read":
                for file_path in accessed:
                    repo_paths.extend(_match_interesting(file_path, interesting))
            commands.append(
                {
                    "command": "\n".join(accessed),
                    "output": output_text if raw_tool in _OPENCODE_FS_OUTPUTS else "",
                }
            )
            continue
        tool_name = _normalize_opencode_tool_name(raw_tool)
        if tool_name not in AMB_TOOL_NAMES:
            continue
        tool_calls.append(
            {
                "tool": tool_name,
                "namespace": str(tool_input.get("namespace") or ""),
                "is_error": status == "error" or bool(state.get("error")),
                "result_text": output_text,
                "result_count": tool_input.get("result_count"),
                "elapsed_ms": state.get("elapsed_ms"),
            }
        )
    final_text = "\n".join(final_parts)
    completed = not saw_error and step_reason == "stop" and bool(final_text.strip())
    return {
        "event_count": events,
        "tool_calls": tool_calls,
        "commands": commands,
        "final_text": final_text,
        "repo_paths_read": list(dict.fromkeys(repo_paths)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "terminal_event": "turn.completed" if completed else last_type,
    }


def _normalize_opencode_tool_name(name: str) -> str:
    cleaned = name.strip()
    if cleaned in AMB_TOOL_NAMES:
        return cleaned
    for tool in AMB_TOOL_NAMES:
        if cleaned.endswith(f"_{tool}") or cleaned.endswith(f".{tool}"):
            return tool
    return cleaned


def assemble_opencode_observation(
    *,
    case: dict[str, Any],
    pack_version: str,
    condition: str,
    model: str,
    version: str,
    parsed: dict[str, Any],
    store_home: Path,
    returncode: int | None,
    elapsed_ms: float,
    freeze: dict[str, Any],
    adapter: dict[str, Any] | None = None,
    adapter_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one live observation. Direct reads of the host store or fixed mount are leaks."""
    final_text = str(parsed.get("final_text") or "")
    terminal = parsed.get("terminal_event")
    access = assess_fixture_access(
        list(parsed.get("commands") or []),
        markers=memory_only_markers(case),
        hidden_paths=[str(store_home), OPENCODE_STORE_MOUNT, "fixture-store.sqlite"],
    )
    return {
        "schema": OBSERVATION_SCHEMA,
        "case_id": case["id"],
        "trace_complete": returncode == 0 and terminal == "turn.completed" and bool(final_text.strip()),
        "terminal_event": terminal,
        "execution_kind": "live",
        "condition": condition,
        "pack": pack_version,
        "host": {"id": "opencode", "version": version, "model": model},
        "amb_available": case["fixture"]["amb_mode"] == "available",
        "namespace_bound": case.get("expected_namespace"),
        "tool_calls": list(parsed.get("tool_calls") or []),
        "final_text": final_text,
        "repo_paths_read": list(parsed.get("repo_paths_read") or []),
        "fixture_access": access,
        "latency_ms": elapsed_ms,
        "input_tokens": parsed.get("input_tokens"),
        "output_tokens": parsed.get("output_tokens"),
        "collector_exit_code": returncode,
        "freeze_sha256": dict(freeze.get("actual") or {}),
        "scorer_sha256": scorer_sha256(),
        "adapter": adapter or {"loaded": False},
        "adapter_evidence": list(adapter_evidence or []),
    }


def write_scored_observation(
    out_dir: Path,
    pack: dict[str, Any],
    observation: dict[str, Any],
    freeze: dict[str, Any],
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "observation.json").write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    report = score_pack(pack, [observation], freeze=freeze)
    if "measurement" in observation:
        report["measurement"] = observation["measurement"]
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _validate_case(case: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    case_id = case.get("id")
    prompt = str(case.get("prompt") or "")
    files = case.get("fixture", {}).get("files", {})
    file_text = "\n".join(files.values())
    memories = "\n".join(memory.get("content", "") for memory in case.get("fixture", {}).get("memories", []))
    if set(case.get("probes") or {}) != set(PROBE_EXPECTATIONS):
        problems.append(f"{case_id} probes")
    if case_id == "indirect-history-dependency" and INDIRECT_FORBIDDEN_WORDS.search(prompt):
        problems.append("indirect prompt contains a memory keyword")
    decision = case.get("expected_decision")
    useful = case.get("useful_marker") or ""
    if decision in {"must_recall", "must_recall_then_no_hit", "recall_or_report_unavailable"} and useful:
        if useful in prompt or useful in file_text:
            problems.append(f"{case_id} leaks the memory-only marker")
    if decision == "recall_and_reconcile":
        current = case.get("current_marker") or ""
        stale = case.get("stale_marker") or ""
        if current not in file_text or current in memories:
            problems.append(f"{case_id} current marker is not repo-only")
        if stale not in memories or stale in file_text or stale in prompt:
            problems.append(f"{case_id} stale marker is not memory-only")
    return problems


def _tool_call_from_event(item: dict[str, Any]) -> dict[str, Any]:
    arguments = item.get("arguments") or item.get("args") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    result = item.get("result") or item.get("output") or {}
    error = bool(item.get("error")) or bool(item.get("is_error")) or bool(item.get("isError"))
    if isinstance(result, dict) and (result.get("isError") is True or result.get("is_error") is True):
        error = True
    result_text = _extract_text(result)
    count = result.get("result_count") if isinstance(result, dict) else None
    if count is None and isinstance(result, dict):
        rows = result.get("results") or result.get("entries") or result.get("items")
        if isinstance(rows, list):
            count = len(rows)
    return {
        "tool": str(item.get("tool") or item.get("name") or item.get("tool_name") or ""),
        "namespace": str(arguments.get("namespace") or "") if isinstance(arguments, dict) else "",
        "is_error": error,
        "result_text": result_text,
        "result_count": count,
        "elapsed_ms": item.get("elapsed_ms"),
    }


def _paths_from_event(item: dict[str, Any], interesting_paths: list[str]) -> list[str]:
    found: list[str] = []
    for key in ("path", "file", "target", "command"):
        value = item.get(key)
        if isinstance(value, str):
            found.extend(_match_interesting(value, interesting_paths))
    changes = item.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, str):
                found.extend(_match_interesting(change, interesting_paths))
            elif isinstance(change, dict):
                for key in ("path", "file"):
                    value = change.get(key)
                    if isinstance(value, str):
                        found.extend(_match_interesting(value, interesting_paths))
    return found


def _match_interesting(value: str, interesting_paths: list[str]) -> list[str]:
    return [relative for relative in interesting_paths if relative in value.replace("\\", "/")]


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_extract_text(item) for item in value) if part)
    if isinstance(value, dict):
        parts = []
        for key in ("text", "content", "result_text", "title", "structured_content", "structuredContent"):
            if key in value:
                parts.append(_extract_text(value[key]))
        return "\n".join(part for part in parts if part)
    return ""


def render_isolated_codex_config(
    *,
    python_path: str,
    cwd: str,
    bridge_home: str,
    config_path: str,
    source_root: str,
    unavailable_command: list[str] | None = None,
) -> str:
    """Render a Codex MCP stanza bound only to a temporary bridge home."""
    from agent_mem_bridge.client_config import build_client_config_options, render_client_config

    options = build_client_config_options(
        "codex",
        python_path=python_path,
        cwd=cwd,
        bridge_home=bridge_home,
        config_path=config_path,
    )
    rendered = render_client_config(options).content
    if unavailable_command:
        quoted_args = ", ".join(_quote_toml_basic(item) for item in unavailable_command[1:])
        lines = []
        for line in rendered.splitlines():
            if line.startswith("command = "):
                lines.append(f"command = {_quote_toml_basic(unavailable_command[0])}")
            elif line.startswith("args = "):
                lines.append(f"args = [{quoted_args}]")
            else:
                lines.append(line)
        rendered = "\n".join(lines)
    if "PYTHONPATH = " not in rendered:
        rendered += f"\nPYTHONPATH = {_quote_toml_basic(source_root)}\n"
    forbidden = ("192.168.", "58080", "Bearer ", "http-token", "AGENT_MEMORY_BRIDGE_REMOTE_URL")
    if any(marker in rendered for marker in forbidden):
        raise RuntimeError("isolated Codex config includes a production route")
    return rendered


def _quote_toml_basic(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
