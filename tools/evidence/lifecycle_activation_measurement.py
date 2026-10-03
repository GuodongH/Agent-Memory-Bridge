"""Explicit Codex read-evidence revision; the frozen decision grader is unchanged."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

LEGACY_REVISION = "legacy-v2"
READ_REVISION = "codex-repo-read-v1"
REVISIONS = (LEGACY_REVISION, READ_REVISION)
_MATCH_LINE = re.compile(r"^(?P<path>.+?):[1-9][0-9]*:(?P<content>.*)$")
_SHELLS = {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"}
_RG_FLAGS = {
    "-n",
    "--line-number",
    "-i",
    "--ignore-case",
    "-S",
    "--smart-case",
    "-H",
    "--with-filename",
    "--no-heading",
    "--color=never",
}


def _rg_command(command: str) -> bool:
    """Recognize a bounded command shape, not arbitrary shell execution."""
    try:
        argv = shlex.split(command)
        shell_text = argv[2] if len(argv) == 3 and argv[0] in _SHELLS and argv[1] in {"-c", "-lc"} else command
        if "\n" in shell_text or "`" in shell_text or "$" in shell_text:
            return False
        lexer = shlex.shlex(shell_text, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&"}:
            groups.append([])
        elif token and all(char in ";&|<>" for char in token):
            return False
        else:
            groups[-1].append(token)
    if not groups[-1] or any(group != ["pwd"] for group in groups[:-1]):
        return False
    rg = groups[-1]
    if rg[0] not in {"rg", "/usr/bin/rg", "/bin/rg"}:
        return False
    index = 1
    while index < len(rg) and rg[index].startswith("-"):
        if rg[index] not in _RG_FLAGS:
            return False
        index += 1
    # Require a line-numbered content search with a pattern and repository target.
    # Do not infer reads from --files, -l, --count, JSON, replacement, or stdin modes.
    return (
        ("-n" in rg[1:index] or "--line-number" in rg[1:index])
        and index + 1 < len(rg)
        and all(target != "-" and not target.startswith("-") for target in rg[index + 1 :])
    )


def rg_read_evidence(text: str, interesting_paths: list[str]) -> list[dict[str, Any]]:
    """Only successful completed command events with allowlisted content lines count."""
    allowed = set(interesting_paths)
    reads: list[dict[str, Any]] = []
    for event_line, raw in enumerate(text.splitlines(), 1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        if item.get("status") != "completed" or type(item.get("exit_code")) is not int or item["exit_code"] != 0:
            continue
        command, output = item.get("command"), item.get("aggregated_output")
        if not isinstance(command, str) or not isinstance(output, str) or not _rg_command(command):
            continue
        for output_line, line in enumerate(output.splitlines(), 1):
            match = _MATCH_LINE.fullmatch(line)
            if not match:
                continue
            path = PurePosixPath(match["path"])
            if path.is_absolute() or ".." in path.parts or path.as_posix() not in allowed:
                continue
            reads.append(
                {
                    "path": path.as_posix(),
                    "event_line": event_line,
                    "output_line": output_line,
                    "item_id": item.get("id"),
                    "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                    "kind": "rg_matching_content_line",
                }
            )
    return reads


def measurement_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    sources = [
        "tools/evidence/lifecycle_activation_measurement.py",
        "tools/evidence/lifecycle_activation.py",
        "tools/evidence/lifecycle_activation_grade.py",
        "benchmark/lifecycle-activation-codex-repo-read-v1.md",
    ]
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources}
    expected = {}
    for line in (root / "benchmark/lifecycle-activation-codex-repo-read-v1.sha256").read_text().splitlines():
        checksum, name = line.split(maxsplit=1)
        expected[name] = checksum
    if expected != hashes:
        raise ValueError("measurement revision source freeze mismatch")
    return {"revision": READ_REVISION, "source_sha256": hashes}


def rescore_codex(evidence_root: Path, out: Path) -> dict[str, Any]:
    """Re-extract all ten saved v2 traces, preserving every original source byte."""
    from tools.evidence.lifecycle_activation import (
        load_pack,
        parse_codex_exec_jsonl,
        score_pack,
        scorer_sha256,
        verify_freeze,
    )

    if out.exists():
        raise ValueError("rescore output must be new; do not overwrite an earlier result")
    identity = measurement_identity()
    freeze = verify_freeze(version="v2")
    if not freeze["ok"]:
        raise ValueError("frozen v2 pack mismatch")
    pack = load_pack(version="v2")
    original: list[dict[str, Any]] = []
    revised: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    for case in pack["cases"]:
        folder = evidence_root / case["id"]
        contents = {}
        for name in ("codex.jsonl", "observation.json", "preflight.json", "report.json"):
            contents[name] = (folder / name).read_bytes()
            sources[f"{case['id']}/{name}"] = hashlib.sha256(contents[name]).hexdigest()
        raw = contents["codex.jsonl"].decode("utf-8")
        observation = json.loads(contents["observation.json"])
        preflight = json.loads(contents["preflight.json"])
        if observation.get("case_id") != case["id"] or observation.get("pack") != "v2":
            raise ValueError(f"observation binding mismatch: {case['id']}")
        if (observation.get("host") or {}).get("id") != "codex" or observation.get("execution_kind") != "live":
            raise ValueError(f"not a native Codex observation: {case['id']}")
        if observation.get("scorer_sha256") != scorer_sha256() or observation.get("freeze_sha256") != freeze["actual"]:
            raise ValueError(f"legacy grader/freeze binding mismatch: {case['id']}")
        if not preflight.get("ok") or preflight.get("visible") or preflight.get("hit_count") != 0:
            raise ValueError(f"isolation evidence not clear: {case['id']}")
        legacy = parse_codex_exec_jsonl(raw, interesting_paths=list(case["fixture"]["files"]))
        for key in ("final_text", "tool_calls", "terminal_event", "repo_paths_read", "input_tokens", "output_tokens"):
            if legacy[key] != observation.get(key):
                raise ValueError(f"raw trace/observation mismatch: {case['id']}:{key}")
        if score_pack(pack, [observation], freeze=freeze) != json.loads(contents["report.json"]):
            raise ValueError(f"saved legacy report mismatch: {case['id']}")
        parsed = parse_codex_exec_jsonl(
            raw, interesting_paths=list(case["fixture"]["files"]), measurement_revision=READ_REVISION
        )
        current = dict(observation)
        current["repo_paths_read"] = parsed["repo_paths_read"]
        current["repository_read_evidence"] = parsed["repository_read_evidence"]
        current["measurement"] = identity
        original.append(observation)
        revised.append(current)
    old_report = score_pack(pack, original, freeze=freeze)
    new_report = score_pack(pack, revised, freeze=freeze)
    changes = [
        {
            "case_id": old["id"],
            "old_status": old["status"],
            "new_status": new["status"],
            "old_reasons": old["reasons"],
            "new_reasons": new["reasons"],
            "old_repo_paths_read": before["repo_paths_read"],
            "new_repo_paths_read": after["repo_paths_read"],
        }
        for old, new, before, after in zip(old_report["results"], new_report["results"], original, revised, strict=True)
    ]
    new_report["measurement"] = identity
    manifest = {
        "measurement": identity,
        "pack": "v2",
        "source_evidence_root": str(evidence_root.resolve()),
        "source_sha256": sources,
        "changes": changes,
        "execution_kind": "source_bound_rescore",
        "new_model_calls": 0,
        "legacy_grader_sha256": scorer_sha256(),
    }
    for relative, expected_hash in sources.items():
        if hashlib.sha256((evidence_root / relative).read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"source changed during rescore: {relative}")
    out.mkdir(parents=True, exist_ok=False)
    for name, data in {
        "old-report.json": old_report,
        "report.json": new_report,
        "observations.json": revised,
        "manifest.json": manifest,
    }.items():
        (out / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return manifest
