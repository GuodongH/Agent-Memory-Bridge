from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from tools.evidence.lifecycle_activation import (
    expand_probe,
    load_pack,
    parse_codex_exec_jsonl,
    score_observation,
    score_pack,
    scorer_sha256,
    verify_freeze,
)
from tools.evidence.lifecycle_activation_cohort import binding_identity
from tools.evidence.lifecycle_activation_cohort import rescore_codex as rescore_bound_cohort
from tools.evidence.lifecycle_activation_measurement import (
    READ_REVISION,
    measurement_identity,
    rescore_codex,
    rg_read_evidence,
)


def _event(**changes: object) -> str:
    item = {
        "id": "item_3",
        "type": "command_execution",
        "status": "completed",
        "exit_code": 0,
        "command": "/bin/bash -lc 'pwd; rg -n -i \"fixture|port token|listener\" .'",
        "aggregated_output": "/fixture/repo\n./NOTES.md:1:current port token: fixture-current-port-2222\n",
    }
    item.update(changes)
    return json.dumps({"type": "item.completed", "item": item})


def test_rg_revision_adds_read_without_changing_legacy() -> None:
    raw = _event()
    old = parse_codex_exec_jsonl(raw, ["NOTES.md"])
    new = parse_codex_exec_jsonl(raw, ["NOTES.md"], measurement_revision=READ_REVISION)
    assert old["repo_paths_read"] == []
    assert "repository_read_evidence" not in old
    assert new["repo_paths_read"] == ["NOTES.md"]
    receipt = new["repository_read_evidence"][0]
    assert receipt["event_line"] == 1 and receipt["output_line"] == 2
    assert receipt["item_id"] == "item_3" and receipt["kind"] == "rg_matching_content_line"
    assert receipt["command_sha256"] == hashlib.sha256(json.loads(raw)["item"]["command"].encode()).hexdigest()
    assert "fixture-current-port-2222" not in json.dumps(receipt)
    assert {key: value for key, value in new.items() if key not in {"repository_read_evidence", "repo_paths_read"}} == {
        key: value for key, value in old.items() if key != "repo_paths_read"
    }


@pytest.mark.parametrize(
    "command",
    ["rg -n -i 'fixture|listener' .", "/usr/bin/rg --line-number fixture .", "/bin/sh -c 'pwd && rg -n fixture .'"],
)
def test_bounded_rg_shapes(command: str) -> None:
    assert rg_read_evidence(_event(command=command), ["NOTES.md"])


@pytest.mark.parametrize(
    "command",
    [
        "echo fake",
        "rg --files .",
        "rg -n -l fixture .",
        "rg -n --count fixture .",
        "rg -n --json fixture .",
        "rg -n --replace fake fixture .",
        "rg -n fixture -",
        "rg -n fixture . | echo fake",
        "rg -n fixture .; echo fake",
        "rg -n fixture . > result",
        "rg -n fixture . <<< fake",
        "rg -n fixture .\necho fake",
        "rg -n fixture . &",
        "rg -n fixture . && echo fake",
        "/bin/bash -lc 'echo fake; rg -n fixture .'",
        "/tmp/bash -lc 'rg -n fixture .'",
        "rg -n fixture $(pwd)",
        "rg -n fixture `pwd`",
        "rg -n fixture .;",
        "rg fixture .",
    ],
)
def test_non_content_or_complex_commands_produce_no_rg_receipt(command: str) -> None:
    assert rg_read_evidence(_event(command=command), ["NOTES.md"]) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"exit_code": 1},
        {"exit_code": None},
        {"exit_code": False},
        {"status": "failed"},
        {"type": "agent_message"},
        {"aggregated_output": "NOTES.md\n"},
        {"aggregated_output": "../NOTES.md:1:fake\n"},
        {"aggregated_output": "/fixture/NOTES.md:1:fake\n"},
        {"aggregated_output": "other.md:1:fake\n"},
        {"aggregated_output": "NOTES.md:0:fake\n"},
    ],
)
def test_invalid_event_output_and_paths_produce_no_rg_receipt(changes: dict) -> None:
    assert rg_read_evidence(_event(**changes), ["NOTES.md"]) == []


def test_started_or_prose_is_not_read_evidence() -> None:
    started = json.loads(_event())
    started["type"] = "item.started"
    assert rg_read_evidence(json.dumps(started), ["NOTES.md"]) == []
    assert rg_read_evidence("I read NOTES.md:1:current token", ["NOTES.md"]) == []


def test_stale_paired_controls_keep_unchanged_decision_rules() -> None:
    case = next(case for case in load_pack(version="v2")["cases"] if case["id"] == "stale-superseded-conflict")
    good = expand_probe(case, case["probes"]["adequate_good"])
    good["repo_paths_read"] = parse_codex_exec_jsonl(_event(), ["NOTES.md"], measurement_revision=READ_REVISION)[
        "repo_paths_read"
    ]
    assert score_observation(case, good)["status"] == "PASS"
    no_read = {**good, "repo_paths_read": []}
    assert "repo_evidence_missing" in score_observation(case, no_read)["reasons"]
    no_recall = {**good, "tool_calls": []}
    assert "required_recall_miss" in score_observation(case, no_recall)["reasons"]
    stale = {**good, "final_text": "fixture-retired-port-1111"}
    assert score_observation(case, stale)["stale_misuse"] is True


def _saved_inputs(root: Path) -> None:
    pack = load_pack(version="v2")
    freeze = verify_freeze(version="v2")
    for case in pack["cases"]:
        folder = root / case["id"]
        folder.mkdir(parents=True)
        raw = "\n".join(
            (
                _event(),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "unknown"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}}),
            )
        )
        parsed = parse_codex_exec_jsonl(raw, list(case["fixture"]["files"]))
        observation = expand_probe(case, {"tool_calls": [], "final_text": "unknown"})
        observation.update(
            {
                "pack": "v2",
                "host": {"id": "codex", "version": "test", "model": "test"},
                "execution_kind": "live",
                "condition": "adapter_enabled",
                "adapter": {"loaded": True},
                "collector_exit_code": 0,
                "scorer_sha256": scorer_sha256(),
                "freeze_sha256": freeze["actual"],
            }
        )
        for key in ("final_text", "tool_calls", "terminal_event", "repo_paths_read", "input_tokens", "output_tokens"):
            observation[key] = parsed[key]
        (folder / "codex.jsonl").write_text(raw)
        (folder / "observation.json").write_text(json.dumps(observation))
        (folder / "preflight.json").write_text(json.dumps({"ok": True, "visible": [], "hit_count": 0}))
        (folder / "report.json").write_text(json.dumps(score_pack(pack, [observation], freeze=freeze)))


def test_full_rescore_preserves_sources_and_refuses_overwrite(tmp_path: Path) -> None:
    source, out = tmp_path / "live", tmp_path / "rescore"
    _saved_inputs(source)
    before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    result = rescore_codex(source, out)
    assert len(result["source_sha256"]) == 40 and len(result["changes"]) == 10
    assert result["new_model_calls"] == 0
    assert result["measurement"] == measurement_identity()
    stale = next(change for change in result["changes"] if change["case_id"] == "stale-superseded-conflict")
    assert "repo_evidence_missing" in stale["old_reasons"]
    assert "repo_evidence_missing" not in stale["new_reasons"]
    assert "required_recall_miss" in stale["new_reasons"]
    assert before == {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="must be new"):
        rescore_codex(source, out)


@pytest.mark.parametrize("defect", ["missing", "raw", "report", "grader", "isolation"])
def test_rescore_fails_closed_before_writing(tmp_path: Path, defect: str) -> None:
    source, out = tmp_path / "live", tmp_path / "rescore"
    _saved_inputs(source)
    folder = source / "stale-superseded-conflict"
    if defect == "missing":
        (folder / "codex.jsonl").unlink()
    elif defect == "raw":
        (folder / "codex.jsonl").write_text("{}")
    elif defect == "report":
        (folder / "report.json").write_text("{}")
    else:
        path = folder / ("observation.json" if defect == "grader" else "preflight.json")
        data = json.loads(path.read_text())
        data["scorer_sha256" if defect == "grader" else "ok"] = "wrong" if defect == "grader" else False
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, OSError)):
        rescore_codex(source, out)
    assert not out.exists()


def test_revision_freeze_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    real_read = Path.read_bytes

    def tampered(path: Path) -> bytes:
        content = real_read(path)
        return content + b"changed" if path.name == "lifecycle_activation_measurement.py" else content

    monkeypatch.setattr(Path, "read_bytes", tampered)
    with pytest.raises(ValueError, match="source freeze mismatch"):
        measurement_identity()


def test_score_command_preserves_revision_and_rejects_mixed_cohort(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    runner = importlib.import_module("run_lifecycle_activation_benchmark")
    source = tmp_path / "live"
    _saved_inputs(source)
    revised = tmp_path / "rescore"
    rescore_codex(source, revised)
    report = tmp_path / "report.json"
    assert (
        runner.main(
            [
                "score",
                "--observations",
                str(revised / "observations.json"),
                "--pack",
                "v2",
                "--format",
                "json",
                "--report-path",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text())["measurement"] == measurement_identity()
    observations = json.loads((revised / "observations.json").read_text())
    observations[0].pop("measurement")
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps(observations))
    rejected = tmp_path / "rejected.json"
    assert (
        runner.main(
            ["score", "--observations", str(mixed), "--pack", "v2", "--format", "json", "--report-path", str(rejected)]
        )
        == 2
    )
    assert not rejected.exists()


def _saved_cohort(root: Path) -> None:
    _saved_inputs(root)
    for case in load_pack(version="v2")["cases"]:
        path = root / case["id"] / "observation.json"
        observation = json.loads(path.read_text())
        observation.update(adapter_backend="remote_loopback", hook_loading="invocation_inline")
        path.write_text(json.dumps(observation))


def test_bound_rescore_retains_identity_history_and_sanitizes_anchor(tmp_path: Path) -> None:
    source, out = tmp_path / "private-source", tmp_path / "new"
    _saved_cohort(source)
    path = source / "cross-client-handoff" / "observation.json"
    observation = json.loads(path.read_text())
    observation.update(private_path="/private/user/source", credential="PRIVATE-CREDENTIAL-MARKER")
    path.write_text(json.dumps(observation))
    original = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    manifest = rescore_bound_cohort(source, out)
    report = json.loads((out / "report.json").read_text())
    anchor = json.loads((out / "evidence-anchor.json").read_text())
    assert manifest["collection_identity"] == report["collection_identity"] == anchor["collection_identity"]
    assert manifest["collection_identity"]["host"] == {"id": "codex", "version": "test", "model": "test"}
    assert manifest["collection_binding"] == report["collection_binding"] == binding_identity()
    assert manifest["measurement"] == measurement_identity()
    assert anchor["new_model_calls"] == 0 and len(anchor["source_sha256"]) == 40
    assert anchor["old_verdict_counts"] == anchor["new_verdict_counts"]
    assert anchor["old_metrics"] == anchor["new_metrics"]
    assert anchor["secondary_host"]["status"] == "NOT_RUN" and anchor["secondary_host"]["runnable_path"]
    assert "PRIVATE-CREDENTIAL-MARKER" not in json.dumps(anchor)
    assert "/private/user/source" not in json.dumps(anchor) and str(source) not in json.dumps(anchor)
    assert original == {
        str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    # The added binding does not rewrite the original aggregate report.
    legacy_out = tmp_path / "legacy"
    rescore_codex(source, legacy_out)
    assert (out / "old-report.json").read_bytes() == (legacy_out / "old-report.json").read_bytes()
    pinned = tmp_path / "pinned"
    rescore_bound_cohort(source, pinned, expected_anchor=out / "evidence-anchor.json")


@pytest.mark.parametrize(
    "field,value",
    [
        ("host.id", "opencode"),
        ("host.version", "codex-cli 0.999.0"),
        ("host.model", "another-model"),
        ("condition", "plain_mcp_baseline"),
        ("adapter_backend", "local"),
        ("hook_loading", "project_hooks"),
        ("pack", "v1"),
        ("freeze_sha256", {}),
        ("scorer_sha256", "wrong"),
        ("execution_kind", "synthetic"),
        ("adapter_backend", []),
        ("hook_loading", {}),
    ],
)
def test_mixed_or_invalid_collection_fails_before_output(tmp_path: Path, field: str, value: object) -> None:
    source, out = tmp_path / "live", tmp_path / "out"
    _saved_cohort(source)
    path = source / "stale-superseded-conflict" / "observation.json"
    observation = json.loads(path.read_text())
    if field.startswith("host."):
        observation["host"][field.removeprefix("host.")] = value
    else:
        observation[field] = value
    path.write_text(json.dumps(observation))
    with pytest.raises(ValueError):
        rescore_bound_cohort(source, out)
    assert not out.exists()


@pytest.mark.parametrize("field", ["host.version", "host.model", "condition", "adapter_backend", "hook_loading"])
def test_missing_collection_metadata_is_not_inferred(tmp_path: Path, field: str) -> None:
    source, out = tmp_path / "live", tmp_path / "out"
    _saved_cohort(source)
    path = source / "fresh-session-continuation" / "observation.json"
    observation = json.loads(path.read_text())
    if field.startswith("host."):
        observation["host"].pop(field.removeprefix("host."))
    else:
        observation.pop(field)
    path.write_text(json.dumps(observation))
    with pytest.raises(ValueError):
        rescore_bound_cohort(source, out)
    assert not out.exists()


def test_expected_anchor_rejects_consistently_changed_model_and_sources(tmp_path: Path) -> None:
    source, first = tmp_path / "live", tmp_path / "first"
    _saved_cohort(source)
    rescore_bound_cohort(source, first)
    expected = first / "evidence-anchor.json"
    for case in load_pack(version="v2")["cases"]:
        path = source / case["id"] / "observation.json"
        observation = json.loads(path.read_text())
        observation["host"]["model"] = "consistent-future-model"
        path.write_text(json.dumps(observation))
    out = tmp_path / "refused"
    with pytest.raises(ValueError, match="expected collection/source anchor mismatch"):
        rescore_bound_cohort(source, out, expected_anchor=expected)
    assert not out.exists()
    # A new, internally consistent cohort remains supported without a historical pin.
    rescore_bound_cohort(source, tmp_path / "future")


def test_changed_binding_freeze_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    real_read = Path.read_bytes

    def changed(path: Path) -> bytes:
        content = real_read(path)
        return content + b"changed" if path.name == "lifecycle_activation_cohort.py" else content

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ValueError, match="collection binding source freeze mismatch"):
        binding_identity()


def test_rescore_cli_binds_collection_and_rejects_changed_bytes(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    runner = importlib.import_module("run_lifecycle_activation_benchmark")
    source, first = tmp_path / "live", tmp_path / "first"
    _saved_cohort(source)
    assert runner.main(["rescore-codex", "--evidence-root", str(source), "--out", str(first)]) == 0
    report = json.loads((first / "report.json").read_text())
    assert report["collection_identity"]["adapter_backend"] == "remote_loopback"
    # Harmless metadata preserves identity and legacy scoring, but changes the
    # exact historical input. The pin must reject it independently of the grader.
    path = source / "cross-client-handoff" / "preflight.json"
    preflight = json.loads(path.read_text())
    preflight["extra"] = "changed bytes"
    path.write_text(json.dumps(preflight))
    rejected = tmp_path / "rejected"
    assert (
        runner.main(
            [
                "rescore-codex",
                "--evidence-root",
                str(source),
                "--out",
                str(rejected),
                "--expected-anchor",
                str(first / "evidence-anchor.json"),
            ]
        )
        == 1
    )
    assert not rejected.exists()
    rescore_bound_cohort(source, tmp_path / "new-cohort")


@pytest.mark.parametrize("contents", ["[]", '{"schema": "unknown"}'])
def test_invalid_expected_anchor_fails_before_output(tmp_path: Path, contents: str) -> None:
    source, out = tmp_path / "live", tmp_path / "out"
    _saved_cohort(source)
    expected = tmp_path / "anchor.json"
    expected.write_text(contents)
    with pytest.raises(ValueError, match="unsupported expected evidence anchor"):
        rescore_bound_cohort(source, out, expected_anchor=expected)
    assert not out.exists()


def test_checked_in_historical_anchor_retains_bound_results() -> None:
    root = Path(__file__).resolve().parents[1]
    anchor = json.loads((root / "benchmark/lifecycle-activation-codex-2026-10-03.anchor.json").read_text())
    assert anchor["measurement"] == measurement_identity()
    assert anchor["collection_binding"] == binding_identity()
    assert anchor["collection_identity"]["host"] == {
        "id": "codex",
        "version": "codex-cli 0.160.0",
        "model": "gpt-6-luna",
    }
    assert (
        anchor["old_verdict_counts"]
        == anchor["new_verdict_counts"]
        == {
            "PASS": 3,
            "FAIL": 7,
            "INCONCLUSIVE": 0,
            "NOT_RUN": 0,
        }
    )
    assert anchor["old_metrics"] == anchor["new_metrics"]
    expected_files = {
        f"{case['id']}/{name}"
        for case in load_pack(version="v2")["cases"]
        for name in ("codex.jsonl", "observation.json", "preflight.json", "report.json")
    }
    assert set(anchor["source_sha256"]) == expected_files
    assert all(len(bytes.fromhex(checksum)) == 32 for checksum in anchor["source_sha256"].values())
    changed = [delta for delta in anchor["changes"] if delta["old_reasons"] != delta["new_reasons"]]
    assert len(changed) == 1 and changed[0]["case_id"] == "stale-superseded-conflict"
    assert changed[0]["new_reasons"] == ["required_recall_miss"]
    assert changed[0]["old_repo_paths_read"] == [] and changed[0]["new_repo_paths_read"] == ["NOTES.md"]
    assert anchor["new_model_calls"] == 0 and anchor["secondary_host"]["status"] == "NOT_RUN"
    provenance = anchor["source_provenance"]
    assert provenance["collection_source_commit"] is None
    assert provenance["source_commit_role"] == "first_preserved_measurement_source_snapshot"
    assert len(bytes.fromhex(provenance["base_commit"])) == len(bytes.fromhex(provenance["source_commit"])) == 20
    assert all(value not in json.dumps(anchor) for value in ("/home/", "/tmp/", "source_evidence_root", "final_text"))
