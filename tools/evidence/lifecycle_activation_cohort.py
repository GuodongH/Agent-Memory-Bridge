"""Cohort binding around the unchanged, source-frozen read revision v1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tools.evidence.lifecycle_activation import ROOT, load_pack, scorer_sha256, verify_freeze
from tools.evidence.lifecycle_activation_measurement import rescore_codex as legacy_rescore_codex

COHORT_REVISION = "codex-cohort-v1"


def binding_identity() -> dict[str, Any]:
    sources = [
        "tools/evidence/lifecycle_activation_cohort.py",
        "scripts/run_lifecycle_activation_benchmark.py",
        "benchmark/lifecycle-activation-codex-cohort-v1.md",
    ]
    actual = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    expected = {}
    for line in (ROOT / "benchmark/lifecycle-activation-codex-cohort-v1.sha256").read_text().splitlines():
        checksum, name = line.split(maxsplit=1)
        expected[name] = checksum
    if expected != actual:
        raise ValueError("collection binding source freeze mismatch")
    return {"revision": COHORT_REVISION, "source_sha256": actual}


def collection_identity(observation: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(observation, dict):
        raise ValueError("collection observation must be an object")
    host = observation.get("host")
    if not isinstance(host, dict) or any(
        not isinstance(host.get(field), str) or not host[field].strip() for field in ("id", "version", "model")
    ):
        raise ValueError("collection host id/version/model required")
    fields = (
        "execution_kind",
        "condition",
        "adapter_backend",
        "hook_loading",
        "pack",
        "freeze_sha256",
        "scorer_sha256",
    )
    if any(field not in observation for field in fields):
        raise ValueError("collection condition/backend/hook/pack identities required")
    identity = {"host": {field: host[field] for field in ("id", "version", "model")}}
    identity.update({field: observation[field] for field in fields})
    if host["id"] != "codex" or identity["execution_kind"] != "live" or identity["pack"] != "v2":
        raise ValueError("collection must be native Codex frozen v2")
    condition, backend, hooks = (identity[field] for field in ("condition", "adapter_backend", "hook_loading"))
    if (
        not isinstance(condition, str)
        or not isinstance(backend, str)
        or (hooks is not None and not isinstance(hooks, str))
    ):
        raise ValueError("collection condition/backend/hook must be strings or explicit no-hook")
    if not (
        (condition == "plain_mcp_baseline" and backend == "local" and hooks is None)
        or (
            condition == "adapter_enabled"
            and (backend, hooks) in {("local", "project_hooks"), ("remote_loopback", "invocation_inline")}
        )
    ):
        raise ValueError("unsupported collection condition/backend/hook binding")
    freeze = verify_freeze(version="v2")
    if (
        not freeze["ok"]
        or identity["freeze_sha256"] != freeze["actual"]
        or identity["scorer_sha256"] != scorer_sha256()
    ):
        raise ValueError("collection pack/freeze/scorer identity mismatch")
    return identity


def _inputs(evidence_root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    identity = None
    sources = {}
    for case in load_pack(version="v2")["cases"]:
        folder = evidence_root / case["id"]
        for name in ("codex.jsonl", "observation.json", "preflight.json", "report.json"):
            contents = (folder / name).read_bytes()
            sources[f"{case['id']}/{name}"] = hashlib.sha256(contents).hexdigest()
            if name == "observation.json":
                current = collection_identity(json.loads(contents))
                if identity is not None and current != identity:
                    raise ValueError(f"mixed collection identity: {case['id']}")
                identity = current
    assert identity is not None
    return identity, sources


def evidence_anchor(manifest: dict[str, Any], old_report: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted projections only: no private root, raw output, bodies or credentials."""
    return {
        "schema": "lifecycle-activation-evidence-anchor-v1",
        "execution_kind": "source_bound_rescore",
        "collection_identity": manifest["collection_identity"],
        "collection_binding": manifest["collection_binding"],
        "measurement": manifest["measurement"],
        "source_sha256": manifest["source_sha256"],
        "changes": manifest["changes"],
        "old_verdict_counts": _counts(old_report),
        "new_verdict_counts": _counts(report),
        "old_metrics": old_report["metrics"],
        "new_metrics": report["metrics"],
        "secondary_host": report["host_lanes"]["opencode"],
        "new_model_calls": manifest["new_model_calls"],
    }


def _counts(report: dict[str, Any]) -> dict[str, int]:
    return {
        status: sum(result["status"] == status for result in report["results"])
        for status in ("PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN")
    }


def rescore_codex(evidence_root: Path, out: Path, *, expected_anchor: Path | None = None) -> dict[str, Any]:
    """Bind one complete cohort before invoking the preserved v1 re-score."""
    if out.exists():
        raise ValueError("rescore output must be new; do not overwrite an earlier result")
    binding = binding_identity()
    collection, source_hashes = _inputs(evidence_root)
    if expected_anchor is not None:
        expected = json.loads(expected_anchor.read_text(encoding="utf-8"))
        if not isinstance(expected, dict) or expected.get("schema") != "lifecycle-activation-evidence-anchor-v1":
            raise ValueError("unsupported expected evidence anchor")
        if expected.get("collection_identity") != collection or expected.get("source_sha256") != source_hashes:
            raise ValueError("expected collection/source anchor mismatch")
    manifest = legacy_rescore_codex(evidence_root, out)
    # The v1 routine re-reads its inputs. Bind its exact snapshot to the metadata
    # checked above, and reject a concurrent change rather than issuing an anchor.
    if manifest["source_sha256"] != source_hashes:
        raise ValueError("collection sources changed during binding")
    observations = json.loads((out / "observations.json").read_text(encoding="utf-8"))
    if any(collection_identity(observation) != collection for observation in observations):
        raise ValueError("re-scored collection identity mismatch")
    manifest["collection_identity"] = collection
    manifest["collection_binding"] = binding
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    old_report = json.loads((out / "old-report.json").read_text(encoding="utf-8"))
    report["collection_identity"] = collection
    report["collection_binding"] = binding
    for name, value in {
        "manifest.json": manifest,
        "report.json": report,
        "evidence-anchor.json": evidence_anchor(manifest, old_report, report),
    }.items():
        (out / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return manifest
