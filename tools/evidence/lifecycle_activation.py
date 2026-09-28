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
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PACK_PATH = ROOT / "benchmark" / "lifecycle-activation-v1.json"
RUBRIC_PATH = ROOT / "benchmark" / "lifecycle-activation-v1.md"
FREEZE_PATH = ROOT / "benchmark" / "lifecycle-activation-v1.sha256"

PACK_SCHEMA = "amb.lifecycle-activation-benchmark.v1"
OBSERVATION_SCHEMA = "amb.lifecycle-activation-observation.v1"
REPORT_SCHEMA = "amb.lifecycle-activation-report.v1"

REQUIRED_CASE_IDS = (
    "fresh-session-continuation",
    "architecture-constraint",
    "known-project-gotcha",
    "indirect-history-dependency",
    "stale-superseded-conflict",
    "no-relevant-memory",
    "amb-unavailable",
    "isolated-typo",
    "repeated-recall",
    "cross-client-handoff",
)

AMB_TOOL_NAMES = frozenset(
    {
        "store",
        "recall",
        "browse",
        "stats",
        "forget",
        "feedback",
        "promote",
        "annotate",
        "revise",
        "export",
        "begin_run",
        "record_run_event",
        "get_run",
        "complete_run",
        "claim_signal",
        "extend_signal_lease",
        "ack_signal",
    }
)

INDIRECT_FORBIDDEN_WORDS = re.compile(r"\b(?:memory|remember|recall|amb)\b", re.IGNORECASE)
PROBE_EXPECTATIONS = {
    "adequate_good": "PASS",
    "bad": "FAIL",
    "unavailable_evidence": "INCONCLUSIVE",
}


def load_pack(path: Path | None = None) -> dict[str, Any]:
    pack_path = path or PACK_PATH
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    if not isinstance(pack, dict) or not isinstance(pack.get("cases"), list):
        raise ValueError("lifecycle activation pack must be an object with cases")
    return pack


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_freeze(root: Path | None = None) -> dict[str, Any]:
    project_root = root or ROOT
    freeze_path = project_root / "benchmark" / "lifecycle-activation-v1.sha256"
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


def validate_pack(pack: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    cases = pack.get("cases")
    if pack.get("schema") != PACK_SCHEMA:
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
    if "quality_score" in pack or "overall_score" in pack:
        problems.append("pack collapses metrics")
    return problems


def check_pack(root: Path | None = None) -> dict[str, Any]:
    project_root = root or ROOT
    freeze = verify_freeze(project_root)
    pack = load_pack(project_root / "benchmark" / "lifecycle-activation-v1.json")
    problems = [] if freeze["ok"] else ["freeze mismatch"]
    problems.extend(validate_pack(pack))
    problems.extend(score_embedded_probes(pack))
    plan = render_host_plan(pack, "opencode", "known-project-gotcha")
    if "opencode run" not in plan or "192.168." in plan:
        problems.append("opencode plan is not an isolated runnable path")
    return {
        "ok": not problems,
        "problems": problems,
        "freeze": freeze,
        "case_count": len(pack["cases"]),
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


def score_pack(
    pack: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_case: dict[str, dict[str, Any]] = {}
    for observation in observations:
        case_id = observation.get("case_id")
        if not isinstance(case_id, str) or case_id in by_case:
            raise ValueError(f"observations need one object per case, got {case_id!r}")
        by_case[case_id] = observation
    results = []
    for case in pack["cases"]:
        observation = by_case.get(case["id"])
        if observation is None:
            results.append(_not_run(case))
        else:
            results.append(score_observation(case, observation))
    lanes = host_lanes(observations)
    metrics = build_metrics(results)
    return {
        "schema": REPORT_SCHEMA,
        "freeze": freeze,
        "host_lanes": lanes,
        "instrument": instrument_checks(pack, lanes, freeze),
        "metrics": metrics,
        "results": results,
    }


def score_observation(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    base = _result_shell(case, observation)
    if observation.get("schema") != OBSERVATION_SCHEMA or observation.get("case_id") != case["id"]:
        return _conclude(base, "INCONCLUSIVE", ["invalid_observation"])
    if observation.get("trace_complete") is not True:
        return _conclude(base, "INCONCLUSIVE", ["trace_incomplete"])
    available = observation.get("amb_available")
    if available not in {True, False}:
        return _conclude(base, "INCONCLUSIVE", ["availability_unknown"])
    access = observation.get("fixture_access") if isinstance(observation.get("fixture_access"), dict) else {}
    if access.get("direct_read") is True:
        reasons = ["fixture_leak"]
        reasons.extend(
            f"fixture_leak:{signal}" for signal in access.get("signals") or [] if isinstance(signal, str) and signal
        )
        return _conclude(base, "INCONCLUSIVE", reasons)

    mode = case["fixture"]["amb_mode"]
    calls = [_normalize_call(call) for call in observation.get("tool_calls") or []]
    recalls = [call for call in calls if call["tool"] == "recall"]
    amb_calls = [call for call in calls if call["tool"] in AMB_TOOL_NAMES]
    base["recall_calls"] = len(recalls)
    base["amb_calls"] = len(amb_calls)
    for call in recalls:
        state = _classify_result(call, case)
        base["result_states"].append(state)
        if call["namespace"] == case.get("expected_namespace"):
            base["namespace_results"].append("correct")
        else:
            base["namespace_results"].append("incorrect")

    if mode == "available" and available is False:
        return _conclude(base, "INCONCLUSIVE", ["precondition_not_met"])
    if mode == "unavailable" and available is True and any(not call["is_error"] for call in recalls):
        return _conclude(base, "INCONCLUSIVE", ["precondition_not_met"])

    reasons = _decision_failures(case, observation, recalls, amb_calls, base)
    reasons.extend(_text_failures(case, str(observation.get("final_text") or "")))
    if case.get("repo_evidence_path") and "repo_paths_read" not in observation and not reasons:
        return _conclude(base, "INCONCLUSIVE", ["repo_evidence_missing"])
    if case.get("repo_evidence_path"):
        reasons.extend(_repo_failures(case, observation))
    extra = _extra_recalls(case, len(recalls))
    base["repeated_recall_count"] = extra
    base["pathological_recall"] = len(recalls) > int(case["max_recall_calls"])
    if extra:
        reasons.append("repeated_or_unnecessary_recall")
    status = "FAIL" if reasons else "PASS"
    return _conclude(base, status, reasons)


def build_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "required_recall_hit_rate": _flag_rate(results, "must_recall", "required_recall_hit"),
        "false_activation_rate": _flag_rate(results, None, "false_activation", negative_only=True),
        "namespace_resolution_correct_rate": _namespace_rate(results),
        "useful_hit_rate_given_recall": _useful_rate(results),
        "no_hit_correct_rate": _status_rate(results, "must_recall_then_no_hit"),
        "unavailable_vs_empty_correct_rate": _status_rate(results, "recall_or_report_unavailable"),
        "stale_memory_misuse_count": sum(1 for result in results if result["stale_misuse"] is True),
        "repeated_or_unnecessary_recall_count": sum(result["repeated_recall_count"] for result in results),
        "overhead": [
            {
                "id": result["id"],
                "status": result["status"],
                "latency_ms": result["latency_ms"],
                "recall_calls": result["recall_calls"],
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "pathological_recall": result["pathological_recall"],
            }
            for result in results
        ],
    }


def host_lanes(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lanes = {
        "codex": {
            "status": "NOT_RUN",
            "execution_kind": None,
            "case_ids": [],
            "contaminated_case_ids": [],
            "runnable_path": "python ./scripts/run_lifecycle_activation_benchmark.py collect-codex --case-id known-project-gotcha",
        },
        "opencode": {
            "status": "NOT_RUN",
            "execution_kind": None,
            "case_ids": [],
            "contaminated_case_ids": [],
            "runnable_path": "python ./scripts/run_lifecycle_activation_benchmark.py print-host-plan --host opencode --case-id known-project-gotcha",
        },
    }
    for observation in observations:
        host = str((observation.get("host") or {}).get("id") or "")
        if host not in lanes:
            continue
        if observation.get("execution_kind") != "live" or observation.get("trace_complete") is not True:
            continue
        access = observation.get("fixture_access") if isinstance(observation.get("fixture_access"), dict) else {}
        if access.get("direct_read") is True:
            lanes[host]["contaminated_case_ids"].append(observation.get("case_id"))
            if lanes[host]["status"] != "SCORED":
                lanes[host]["status"] = "INCONCLUSIVE"
                lanes[host]["execution_kind"] = "live"
            continue
        lanes[host]["status"] = "SCORED"
        lanes[host]["execution_kind"] = "live"
        lanes[host]["case_ids"].append(observation.get("case_id"))
    return lanes


def instrument_checks(
    pack: dict[str, Any],
    lanes: dict[str, dict[str, Any]],
    freeze: dict[str, Any] | None,
) -> dict[str, Any]:
    ids = [case["id"] for case in pack["cases"]]
    opencode = lanes["opencode"]
    checks = {
        "freeze_verified": bool(freeze and freeze.get("ok")),
        "required_scenarios_present": tuple(ids) == REQUIRED_CASE_IDS,
        "negative_controls_present": sum(case["class"] == "negative_control" for case in pack["cases"]) >= 2,
        "metrics_not_collapsed": True,
        "codex_live_trace": lanes["codex"]["status"] == "SCORED",
        "opencode_explicit": opencode["status"] == "SCORED"
        or (opencode["status"] == "NOT_RUN" and bool(opencode["runnable_path"])),
    }
    return {"checks": checks, "pass": all(checks.values())}


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
        lines.append(f"{result['id']} {result['status']} {','.join(result['reasons'])}")
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
    """Flag shell access to the hidden store. MCP tool results are not commands."""
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
) -> list[str]:
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError(f"unsupported sandbox {sandbox}")
    argv = [codex_bin, "exec"]
    for override in config_overrides or []:
        if ":58080" in override or "--add-dir" in override or "fixture-store.sqlite" in override:
            raise ValueError("collector override points at production AMB or the fixture store")
        argv.extend(["-c", override])
    argv.extend(
        [
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--disable",
            "plugins",
            "--disable",
            "memories",
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
    ro_binds: list[tuple[Path, Path]],
    rw_binds: list[tuple[Path, Path]],
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


def render_collector_codex_preamble(*, catalog_path: str, fixture_repo: str) -> str:
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
        "plugins = false",
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


def render_host_plan(pack: dict[str, Any], host: str, case_id: str) -> str:
    case = next(item for item in pack["cases"] if item["id"] == case_id)
    if host == "codex":
        command = (
            "python ./scripts/run_lifecycle_activation_benchmark.py collect-codex "
            "--case-id <case-id> --out <temp-dir-outside-the-fixture>"
        )
    elif host == "opencode":
        command = "opencode run --format json --pure --dir <fixture-repo> --model <recorded-model> -- <frozen-prompt>"
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


def parse_codex_exec_jsonl(text: str, interesting_paths: list[str] | None = None) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    final_parts: list[str] = []
    repo_paths: list[str] = []
    commands: list[dict[str, str]] = []
    usage: dict[str, Any] = {}
    events = 0
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
    return {
        "event_count": events,
        "tool_calls": tool_calls,
        "commands": commands,
        "final_text": "\n".join(final_parts),
        "repo_paths_read": list(dict.fromkeys(repo_paths)),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


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


def _decision_failures(
    case: dict[str, Any],
    observation: dict[str, Any],
    recalls: list[dict[str, Any]],
    amb_calls: list[dict[str, Any]],
    base: dict[str, Any],
) -> list[str]:
    decision = case["expected_decision"]
    expected_ns = case.get("expected_namespace")
    reasons: list[str] = []
    matching = [call for call in recalls if call["namespace"] == expected_ns]
    if decision == "must_recall":
        base["required_recall_hit"] = bool(recalls)
        if not matching:
            reasons.append("required_recall_miss" if not recalls else "namespace_miss")
        elif not any(_classify_result(call, case) == "useful_hit" for call in matching):
            reasons.append("useful_hit_miss")
    elif decision == "recall_and_reconcile":
        base["required_recall_hit"] = bool(recalls)
        stale = case.get("stale_marker") or ""
        current = case.get("current_marker") or ""
        final = str(observation.get("final_text") or "")
        base["stale_misuse"] = bool(stale and stale in final)
        if not matching:
            reasons.append("required_recall_miss" if not recalls else "namespace_miss")
        elif not any(_classify_result(call, case) == "useful_hit" for call in matching):
            reasons.append("useful_hit_miss")
        if current and current not in final:
            reasons.append("current_evidence_not_used")
        if base["stale_misuse"]:
            reasons.append("stale_misuse")
    elif decision == "must_recall_then_no_hit":
        base["required_recall_hit"] = bool(recalls)
        if not matching:
            reasons.append("required_recall_miss" if not recalls else "namespace_miss")
            base["no_hit_correct"] = False
        else:
            states = [_classify_result(call, case) for call in matching]
            base["no_hit_correct"] = states == ["no_hit"] or (states and all(state == "no_hit" for state in states))
            if any(call["is_error"] for call in matching):
                reasons.append("unavailable_treated_as_empty")
                base["no_hit_correct"] = False
            elif not base["no_hit_correct"]:
                reasons.append("no_hit_mismatch")
    elif decision == "recall_or_report_unavailable":
        successful = [call for call in recalls if not call["is_error"]]
        discriminated = not successful
        base["unavailable_discriminated"] = discriminated
        if successful:
            reasons.append("unavailable_treated_as_empty")
            base["unavailable_discriminated"] = False
    elif decision in {"do_not_recall", "do_not_repeat"}:
        activated = bool(amb_calls)
        base["false_activation"] = activated
        if activated:
            reasons.append("false_activation" if decision == "do_not_recall" else "repeated_or_unnecessary_recall")
    else:
        reasons.append("unknown_decision")
    return reasons


def _text_failures(case: dict[str, Any], final: str) -> list[str]:
    reasons: list[str] = []
    for marker in case.get("final_text_must_include") or []:
        if marker not in final:
            reasons.append(f"missing_required_text:{marker}")
    for marker in case.get("final_text_must_not_include") or []:
        if marker and marker in final:
            reasons.append(f"forbidden_text:{marker}")
    options = case.get("final_text_must_match_any") or []
    if options and not any(option.casefold() in final.casefold() for option in options):
        reasons.append("missing_status_phrase")
    return reasons


def _repo_failures(case: dict[str, Any], observation: dict[str, Any]) -> list[str]:
    if "repo_paths_read" not in observation:
        return ["repo_evidence_missing"]
    required = str(case["repo_evidence_path"])
    paths = observation.get("repo_paths_read") or []
    if not isinstance(paths, list) or not _saw_path([str(path) for path in paths], required):
        return ["repo_evidence_missing"]
    return []


def _extra_recalls(case: dict[str, Any], recall_count: int) -> int:
    allowed = int(case["max_recall_calls"])
    if case["expected_decision"] in {"do_not_recall", "do_not_repeat"}:
        return recall_count
    return max(0, recall_count - allowed)


def _classify_result(call: dict[str, Any], case: dict[str, Any]) -> str:
    if call["is_error"]:
        return "unavailable"
    text = call["result_text"]
    count = call.get("result_count")
    if count == 0 or (count is None and not text.strip()):
        return "no_hit"
    useful = case.get("useful_marker") or ""
    distractor = case.get("distractor_marker") or ""
    if useful and useful in text:
        return "useful_hit"
    if text.strip() or (distractor and distractor in text):
        return "irrelevant_hit"
    return "no_hit"


def _normalize_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": _normalize_tool_name(str(call.get("tool") or "")),
        "namespace": str(call.get("namespace") or ""),
        "is_error": bool(call.get("is_error")),
        "result_text": str(call.get("result_text") or ""),
        "result_count": call.get("result_count"),
        "elapsed_ms": call.get("elapsed_ms"),
    }


def _normalize_tool_name(name: str) -> str:
    leaf = name.strip().replace("/", ".").split(".")[-1]
    if "__" in leaf:
        leaf = leaf.split("__")[-1]
    return leaf


def _saw_path(paths: list[str], required: str) -> bool:
    needle = required.replace("\\", "/").lstrip("./")
    for path in paths:
        cleaned = path.replace("\\", "/").lstrip("./")
        if cleaned == needle or cleaned.endswith("/" + needle):
            return True
    return False


def _result_shell(case: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any]:
    host = (observation or {}).get("host") or {}
    return {
        "id": case["id"],
        "scenario": case["scenario"],
        "class": case["class"],
        "expected_decision": case["expected_decision"],
        "status": "INCONCLUSIVE",
        "reasons": [],
        "recall_calls": 0,
        "amb_calls": 0,
        "namespace_results": [],
        "result_states": [],
        "required_recall_hit": None,
        "false_activation": None,
        "no_hit_correct": None,
        "unavailable_discriminated": None,
        "stale_misuse": None,
        "repeated_recall_count": 0,
        "pathological_recall": False,
        "latency_ms": None if observation is None else observation.get("latency_ms"),
        "input_tokens": None if observation is None else observation.get("input_tokens"),
        "output_tokens": None if observation is None else observation.get("output_tokens"),
        "execution_kind": None if observation is None else observation.get("execution_kind"),
        "host_id": host.get("id"),
    }


def _not_run(case: dict[str, Any]) -> dict[str, Any]:
    result = _result_shell(case, None)
    result["status"] = "NOT_RUN"
    result["reasons"] = ["observation_missing"]
    return result


def _conclude(result: dict[str, Any], status: str, reasons: list[str]) -> dict[str, Any]:
    result["status"] = status
    result["reasons"] = list(dict.fromkeys(reasons))
    decision = result["expected_decision"]
    if status == "INCONCLUSIVE":
        result["required_recall_hit"] = None
        result["false_activation"] = None
        result["no_hit_correct"] = None
        result["unavailable_discriminated"] = None
        result["stale_misuse"] = None
    elif decision == "must_recall_then_no_hit" and result["no_hit_correct"] is None:
        result["no_hit_correct"] = status == "PASS"
    elif decision == "recall_or_report_unavailable" and result["unavailable_discriminated"] is None:
        result["unavailable_discriminated"] = status == "PASS"
    return result


def _flag_rate(
    results: list[dict[str, Any]],
    decision: str | None,
    flag: str,
    *,
    negative_only: bool = False,
) -> dict[str, Any]:
    selected = []
    for result in results:
        if decision is not None and result["expected_decision"] != decision:
            continue
        if negative_only and result["class"] != "negative_control":
            continue
        selected.append(result)
    passed = sum(result[flag] is True for result in selected)
    failed = sum(result[flag] is False for result in selected)
    inconclusive = sum(result["status"] == "INCONCLUSIVE" for result in selected)
    not_run = sum(result["status"] == "NOT_RUN" for result in selected)
    denominator = passed + failed
    return {
        "numerator": passed,
        "complement": failed,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else passed / denominator,
    }


def _status_rate(results: list[dict[str, Any]], decision: str) -> dict[str, Any]:
    selected = [result for result in results if result["expected_decision"] == decision]
    passed = sum(result["status"] == "PASS" for result in selected)
    failed = sum(result["status"] == "FAIL" for result in selected)
    inconclusive = sum(result["status"] == "INCONCLUSIVE" for result in selected)
    not_run = sum(result["status"] == "NOT_RUN" for result in selected)
    denominator = passed + failed
    return {
        "numerator": passed,
        "complement": failed,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else passed / denominator,
    }


def _namespace_rate(results: list[dict[str, Any]]) -> dict[str, Any]:
    correct = 0
    incorrect = 0
    inconclusive = 0
    not_run = 0
    for result in results:
        if result["status"] == "INCONCLUSIVE":
            inconclusive += 1
        elif result["status"] == "NOT_RUN":
            not_run += 1
        else:
            correct += sum(item == "correct" for item in result["namespace_results"])
            incorrect += sum(item == "incorrect" for item in result["namespace_results"])
    denominator = correct + incorrect
    return {
        "numerator": correct,
        "complement": incorrect,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else correct / denominator,
    }


def _useful_rate(results: list[dict[str, Any]]) -> dict[str, Any]:
    useful = 0
    other = 0
    inconclusive = 0
    not_run = 0
    for result in results:
        if result["expected_decision"] not in {"must_recall", "recall_and_reconcile"}:
            continue
        if result["status"] == "INCONCLUSIVE":
            inconclusive += 1
        elif result["status"] == "NOT_RUN":
            not_run += 1
        else:
            useful += sum(state == "useful_hit" for state in result["result_states"])
            other += sum(state != "useful_hit" for state in result["result_states"])
    denominator = useful + other
    return {
        "numerator": useful,
        "complement": other,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else useful / denominator,
    }


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
