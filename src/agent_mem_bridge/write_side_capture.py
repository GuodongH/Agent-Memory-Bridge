from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .durable_data_policy import forbidden_durable_structured_field
from .learning_policy import evaluate_learning_candidate
from .promotion import parse_structured_record
from .record_projection import sync_record_projection
from .repository import (
    MEMORY_ROW_SELECT,
    MemoryRow,
    content_hash_for_content,
    exact_content_hash_for_content,
    fetch_row_by_id,
)

CAPTURE_EVENT_SCHEMA = "memory.lifecycle_capture_event.v1"
VISIBLE_ARTIFACT_SCHEMA = "memory.visible_artifact.v1"
CAPTURE_RECEIPT_SCHEMA = "memory.lifecycle_capture_receipt.v1"
CAPTURE_ORIGIN = "lifecycle_capture"
CAPTURE_ACTOR = "amb-lifecycle-capture"
SERVER_SUPERSESSION_PLAN = (
    "Lifecycle capture does not mutate the contradicted record. Reviewer must revise or reject explicitly."
)

SUPPORTED_BOUNDARIES = frozenset({"pre_compaction", "session_stop", "explicit_handoff", "post_run"})
CAPTURE_CLASSES = frozenset({"decision", "constraint", "gotcha", "user_correction", "handoff"})
OPEN_CANDIDATE_STATUSES = frozenset({"pending", "needs_review"})
STALE_MARKERS = frozenset({"stale", "superseded", "replaced", "expired"})
STALE_REVALIDATION_PLAN = (
    "Lifecycle capture does not revive or mutate the stale durable row. "
    "Reviewer must revalidate, replace, or reject this fresh confirmation explicitly."
)

MAX_ARTIFACTS = 8
MAX_EVIDENCE_REFS = 8
SCAN_LIMIT = 400
MIN_CLAIM_CHARS = 24
MIN_CLAIM_WORDS = 4
MIN_REASON_CHARS = 12
MIN_REASON_WORDS = 3
MIN_GOTCHA_CHARS = 8

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")
_EVIDENCE_REF_RE = re.compile(r"^[^\s]{1,160}$")
_ROLE_LINE_RE = re.compile(r"(^|\n)\s*(user|assistant|tool)\s*:", re.IGNORECASE)
_STATUS_RE = re.compile(
    r"^(still working|in progress|working on it|tests passed|tests pass|done|ok|okay|lgtm|looking into it|wip)\.?$",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class _IndexedClaim:
    claim_key: str
    record_id: str
    lane: str
    stale: bool


def capture_lifecycle_candidates(store: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """Propose review-only candidates from one bounded lifecycle event.

    The caller may describe visible artifacts. Governance, deduplication, and
    the review decision are recomputed here. This function never promotes a
    candidate into ordinary durable memory.
    """

    if not isinstance(event, Mapping):
        return _event_receipt(boundary="", namespace="", disposition="rejected", reason_codes=["invalid_event"])

    blocked = _event_block_reason(event)
    boundary = str(event.get("boundary", "")).strip()
    namespace = str(event.get("namespace", "")).strip()
    if blocked is not None:
        return _event_receipt(boundary=boundary, namespace=namespace, disposition="rejected", reason_codes=[blocked])

    artifacts = event.get("visible_artifacts")
    assert isinstance(artifacts, list)
    index = _load_claim_index(store, namespace)
    items: list[dict[str, Any]] = []
    writes = 0
    mutations: list[str] = []
    for artifact in artifacts:
        item, wrote = _capture_artifact(
            store,
            event=event,
            artifact=artifact,
            boundary=boundary,
            namespace=namespace,
            index=index,
            mutations=mutations,
        )
        items.append(item)
        writes += wrote

    promoted = _mutations_promoted_durable(store, mutations)
    receipt = _event_receipt(
        boundary=boundary,
        namespace=namespace,
        disposition="captured" if writes else "no_capture",
        reason_codes=["durable_memory_promoted"] if promoted else [],
        items=items,
        writes=writes,
        automatic_promotion=promoted,
        dedup_scan_truncated=index["truncated"],
    )
    store._log(
        "lifecycle_capture",
        {
            "boundary": boundary,
            "namespace": namespace,
            "disposition": receipt["disposition"],
            "writes": writes,
            "automatic_promotion": promoted,
            "dedup_scan_truncated": index["truncated"],
        },
    )
    return receipt


def capture_at_boundary(store: Any, boundary: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """Capture at a host-owned boundary.

    The boundary argument wins over any boundary field inside the event, so a
    caller-supplied label cannot widen the host lifecycle seam.
    """

    payload = dict(event)
    payload["boundary"] = boundary
    return capture_lifecycle_candidates(store, payload)


def capture_session_stop(store: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """Session stop/close seam for review-only candidate capture."""

    return capture_at_boundary(store, "session_stop", event)


def _capture_artifact(
    store: Any,
    *,
    event: Mapping[str, Any],
    artifact: Any,
    boundary: str,
    namespace: str,
    index: dict[str, Any],
    mutations: list[str],
) -> tuple[dict[str, Any], int]:
    normalized, reason = _normalize_artifact(artifact, boundary=boundary)
    if normalized is None:
        artifact_id = _safe_artifact_id(artifact)
        return _item(artifact_id, disposition="rejected", reason_codes=[reason]), 0

    _note_conflict_targets(store, namespace, normalized)
    claim_key = _claim_key(normalized["claim"])
    durable_matches = _durable_matches(index, claim_key)
    current_durable = next((item for item in durable_matches if not item.stale), None)
    if current_durable is not None:
        return (
            _item(
                normalized["artifact_id"],
                disposition="already_durable",
                reason_codes=["equivalent_durable_claim"],
                recommended_action="reject",
                relation="duplicate",
                record_id=current_durable.record_id,
            ),
            0,
        )
    stale_matches = [item for item in durable_matches if item.stale]
    if stale_matches:
        _mark_stale_revalidation(normalized, stale_matches)
    open_match = _match(index, claim_key, lane="open")
    if open_match is not None:
        return _strengthen(store, open_match, normalized, boundary=boundary, index=index, mutations=mutations)

    candidate = _candidate_payload(event, normalized, boundary=boundary, namespace=namespace)
    decision = evaluate_learning_candidate(candidate)
    if decision.get("decision") != "needs_review":
        return (
            _item(
                normalized["artifact_id"],
                disposition="rejected",
                reason_codes=list(decision.get("reasons") or ["policy_not_review_required"]),
                recommended_action="reject",
            ),
            0,
        )

    try:
        stored = store.store_learning_candidate(candidate, decision, candidate_status="needs_review")
    except (ValueError, sqlite3.Error):
        return (
            _item(
                normalized["artifact_id"],
                disposition="rejected",
                reason_codes=["store_rejected"],
                recommended_action="reject",
            ),
            0,
        )
    record_id = str(stored.get("id") or "")
    if stored.get("stored") is not True:
        return (
            _item(
                normalized["artifact_id"],
                disposition="unchanged_existing",
                reason_codes=["exact_content_duplicate"],
                recommended_action="merge",
                relation="duplicate",
                record_id=record_id or None,
                candidate_status="needs_review",
                hidden_from_ordinary_recall=True,
            ),
            0,
        )
    if record_id:
        mutations.append(record_id)
        index["claims"].append(_IndexedClaim(claim_key, record_id, "open", False))
    reason_codes = ["new_candidate", *list(normalized["reason_codes"])]
    if index["truncated"]:
        reason_codes.append("dedup_scan_truncated")
    return (
        _item(
            normalized["artifact_id"],
            disposition="stored",
            reason_codes=reason_codes,
            recommended_action=str(normalized["recommended_action"]),
            relation=str(normalized["relation"]),
            record_id=record_id or None,
            candidate_status="needs_review",
            hidden_from_ordinary_recall=True,
        ),
        1,
    )


def _strengthen(
    store: Any,
    match: _IndexedClaim,
    normalized: Mapping[str, Any],
    *,
    boundary: str,
    index: dict[str, Any],
    mutations: list[str],
) -> tuple[dict[str, Any], int]:
    review_action, review_relation = _open_review_action(normalized)
    try:
        changed = _merge_open_candidate(store, match.record_id, normalized, boundary=boundary)
    except (sqlite3.Error, ValueError, json.JSONDecodeError):
        return (
            _item(
                str(normalized["artifact_id"]),
                disposition="rejected",
                reason_codes=["evidence_merge_failed"],
                recommended_action=review_action,
                relation=review_relation,
                record_id=match.record_id,
            ),
            0,
        )
    if not changed:
        return (
            _item(
                str(normalized["artifact_id"]),
                disposition="unchanged_existing",
                reason_codes=["equivalent_open_candidate"],
                recommended_action=review_action,
                relation=review_relation,
                record_id=match.record_id,
                candidate_status="needs_review",
                hidden_from_ordinary_recall=True,
            ),
            0,
        )
    mutations.append(match.record_id)
    if normalized["relation"] == "contradicted":
        index["claims"].append(_IndexedClaim(match.claim_key, match.record_id, "open", match.stale))
    return (
        _item(
            str(normalized["artifact_id"]),
            disposition="strengthened_existing",
            reason_codes=["equivalent_open_candidate", *list(normalized["reason_codes"])],
            recommended_action=review_action,
            relation=review_relation,
            record_id=match.record_id,
            candidate_status="needs_review",
            hidden_from_ordinary_recall=True,
        ),
        1,
    )


def _merge_open_candidate(
    store: Any,
    memory_id: str,
    normalized: Mapping[str, Any],
    *,
    boundary: str,
) -> bool:
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = fetch_row_by_id(conn, memory_id)
        if row is None:
            conn.rollback()
            raise ValueError("candidate disappeared before evidence merge")
        source = MemoryRow.from_sqlite(row)
        if not source.is_learning_candidate:
            conn.rollback()
            raise ValueError("evidence merge target is not a hidden candidate")
        fields = _content_fields(source.content)
        changed = False
        changed |= _merge_json_field(fields, "evidence_refs_json", list(normalized["evidence_refs"]))
        changed |= _merge_json_field(
            fields,
            "contradicts_record_ids_json",
            list(normalized["contradicts_record_ids"]),
        )
        changed |= _merge_json_field(fields, "visible_artifact_ids_json", [str(normalized["artifact_id"])])
        if normalized["relation"] == "contradicted":
            changed |= _set_field(fields, "recommended_action", "revise")
            changed |= _set_field(fields, "relation", "contradicted")
            changed |= _set_field(fields, "supersession_plan", SERVER_SUPERSESSION_PLAN)
        elif normalized["relation"] == "revalidation" or _field_value(fields, "relation") == "revalidation":
            changed |= _set_field(fields, "recommended_action", "revalidate")
            changed |= _set_field(fields, "relation", "revalidation")
            changed |= _set_field(fields, "supersession_plan", STALE_REVALIDATION_PLAN)
            changed |= _merge_json_field(
                fields,
                "supersedes_record_ids_json",
                list(normalized.get("supersedes_record_ids") or []),
            )
        elif _field_value(fields, "relation") != "contradicted":
            changed |= _set_field(fields, "recommended_action", "merge")
            changed |= _set_field(fields, "relation", "duplicate")
        if not changed:
            conn.rollback()
            return False

        content = "\n".join(f"{key}: {value}" for key, value in fields)
        conn.execute(
            """
            UPDATE memories
            SET content = ?, content_hash = ?, exact_content_hash = ?, is_learning_candidate = 1
            WHERE id = ?
            """,
            (
                content,
                content_hash_for_content(content),
                exact_content_hash_for_content(content),
                memory_id,
            ),
        )
        sync_record_projection(
            conn,
            memory_id=memory_id,
            namespace=source.namespace,
            content=content,
            tags=list(source.tags),
            kind=source.kind,
            actor=source.actor,
            source_app=source.source_app,
            is_learning_candidate=True,
        )
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        conn.execute(
            "INSERT INTO memories_fts(memory_id, title, content) VALUES (?, ?, ?)",
            (memory_id, source.title or "", content),
        )
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
        conn.execute(
            """
            INSERT INTO memory_annotations (
                memory_id,
                title_before,
                title_after,
                added_tags_json,
                provenance_json,
                actor,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                source.title,
                source.title,
                "[]",
                json.dumps(
                    {
                        "capture_strengthened": True,
                        "boundary": boundary,
                        "artifact_id": normalized["artifact_id"],
                        "added_evidence_refs": list(normalized["evidence_refs"]),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                CAPTURE_ACTOR,
                store._utc_now(),
            ),
        )
    return True


def _candidate_payload(
    event: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    boundary: str,
    namespace: str,
) -> dict[str, Any]:
    authority = "procedure" if normalized["capture_class"] == "gotcha" else "decision"
    return {
        "schema": "memory.candidate.v1",
        "namespace": namespace,
        "authority_class": authority,
        "claim": normalized["claim"],
        "evidence_refs": list(normalized["evidence_refs"]),
        "source_runtime": str(event["source_runtime"]),
        "source_session_id": str(event["source_session_id"]),
        "source_task_id": str(event["source_task_id"]),
        "capture_origin": CAPTURE_ORIGIN,
        "capture_boundary": boundary,
        "capture_class": normalized["capture_class"],
        "reuse_reason": normalized["reuse_reason"],
        "recommended_action": normalized["recommended_action"],
        "relation": normalized["relation"],
        "scope": normalized["scope"],
        "visible_artifact_id": normalized["artifact_id"],
        "visible_artifact_ids": [normalized["artifact_id"]],
        "contradicts_record_ids": list(normalized["contradicts_record_ids"]),
        "supersedes_record_ids": list(normalized.get("supersedes_record_ids") or []),
        "supersession_plan": normalized["supersession_plan"],
        "domain_tags": [f"capture-{normalized['capture_class']}"],
        "sensitivity": "safe",
        "created_by": CAPTURE_ACTOR,
    }


def _normalize_artifact(artifact: Any, *, boundary: str) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(artifact, Mapping):
        return None, "invalid_artifact"
    if forbidden_durable_structured_field(artifact) is not None:
        return None, "unsupported_input"
    if str(artifact.get("schema", "")).strip() != VISIBLE_ARTIFACT_SCHEMA:
        return None, "invalid_artifact"
    artifact_id = str(artifact.get("artifact_id", "")).strip()
    if _TOKEN_RE.fullmatch(artifact_id) is None:
        return None, "invalid_artifact"
    capture_class = str(artifact.get("artifact_class", "")).strip().lower()
    if capture_class not in CAPTURE_CLASSES:
        return None, "not_in_v1_class"

    scope = str(artifact.get("scope", "project")).strip().lower() or "project"
    if scope not in {"project", "broader"}:
        return None, "insufficient_structure"
    evidence_refs, evidence_reason = _evidence_refs(artifact.get("evidence_refs"), boundary, artifact_id)
    if evidence_reason is not None:
        return None, evidence_reason

    if capture_class == "gotcha":
        if artifact.get("validated") is not True:
            return None, "unvalidated_gotcha"
        symptom = _compact(artifact.get("symptom")).rstrip(".")
        fix = _compact(artifact.get("fix")).rstrip(".")
        if len(symptom) < MIN_GOTCHA_CHARS or len(fix) < MIN_GOTCHA_CHARS:
            return None, "insufficient_structure"
        claim = f"When {symptom}, the validated fix is {fix}."
    else:
        claim = _compact(artifact.get("claim"))

    reason = _compact(artifact.get("reason"))
    blocked = _text_block_reason(claim, reason, _compact(artifact.get("symptom")), _compact(artifact.get("fix")))
    if blocked is not None:
        return None, blocked
    if capture_class != "gotcha" and not _usable_prose(
        claim, minimum_chars=MIN_CLAIM_CHARS, minimum_words=MIN_CLAIM_WORDS
    ):
        return None, "not_reusable"
    if not _usable_prose(reason, minimum_chars=MIN_REASON_CHARS, minimum_words=MIN_REASON_WORDS):
        return None, "insufficient_structure"

    contradicts, contradict_reason = _record_ids(artifact.get("contradicts_record_ids"))
    if contradict_reason is not None:
        return None, contradict_reason
    corrected, corrected_reason = _record_ids(artifact.get("corrected_record_ids"))
    if corrected_reason is not None:
        return None, corrected_reason
    if capture_class == "user_correction":
        contradicts = sorted(set(contradicts) | set(corrected))
        if not contradicts:
            return None, "insufficient_structure"

    relation = "contradicted" if capture_class == "user_correction" or contradicts else "new"
    recommended = "revise" if relation == "contradicted" else "promote"
    reason_codes: list[str] = []
    supersession_plan = ""
    if relation == "contradicted":
        supersession_plan = SERVER_SUPERSESSION_PLAN
        reason_codes.append("contradiction_requires_review")
    return (
        {
            "artifact_id": artifact_id,
            "capture_class": capture_class,
            "claim": claim,
            "reuse_reason": reason,
            "evidence_refs": evidence_refs,
            "scope": scope,
            "contradicts_record_ids": contradicts,
            "supersession_plan": supersession_plan,
            "recommended_action": recommended,
            "relation": relation,
            "reason_codes": reason_codes,
        },
        "",
    )


def _event_block_reason(event: Mapping[str, Any]) -> str | None:
    forbidden = forbidden_durable_structured_field(event)
    if forbidden is not None:
        return "unsupported_input"
    if str(event.get("schema", "")).strip() != CAPTURE_EVENT_SCHEMA:
        return "invalid_schema"
    boundary = str(event.get("boundary", "")).strip()
    if boundary not in SUPPORTED_BOUNDARIES:
        return "unsupported_boundary"
    if _NAMESPACE_RE.fullmatch(str(event.get("namespace", "")).strip()) is None:
        return "missing_namespace"
    for key in ("source_runtime", "source_session_id", "source_task_id"):
        if _TOKEN_RE.fullmatch(str(event.get(key, "")).strip()) is None:
            return "missing_provenance"
    artifacts = event.get("visible_artifacts")
    if not isinstance(artifacts, list):
        return "invalid_event"
    if len(artifacts) > MAX_ARTIFACTS:
        return "unbounded_input"
    return None


def _evidence_refs(value: Any, boundary: str, artifact_id: str) -> tuple[list[str], str | None]:
    if value is None:
        refs = [f"visible:{boundary}:{artifact_id}"]
    elif isinstance(value, list):
        refs = []
        for item in value:
            if not isinstance(item, str) or _EVIDENCE_REF_RE.fullmatch(item.strip()) is None:
                return [], "invalid_evidence"
            refs.append(item.strip())
    else:
        return [], "invalid_evidence"
    unique = sorted(set(refs))
    if not unique or len(unique) > MAX_EVIDENCE_REFS:
        return [], "invalid_evidence"
    return unique, None


def _record_ids(value: Any) -> tuple[list[str], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], "invalid_artifact"
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str) or _TOKEN_RE.fullmatch(item.strip()) is None:
            return [], "invalid_artifact"
        refs.append(item.strip())
    return sorted(set(refs)), None


def _text_block_reason(*parts: str) -> str | None:
    text = "\n".join(part for part in parts if part)
    if _ROLE_LINE_RE.search(text):
        return "raw_transcript"
    decision = evaluate_learning_candidate(
        {
            "schema": "memory.candidate.v1",
            "namespace": "project:capture-probe",
            "authority_class": "context_hint",
            "claim": text or "placeholder",
            "evidence_refs": ["visible:local:check"],
            "source_runtime": "probe",
            "source_session_id": "probe",
            "source_task_id": "probe",
        }
    )
    if "sensitive_content" in decision.get("reasons", []):
        return "sensitive_content"
    if "raw_transcript" in decision.get("reasons", []):
        return "raw_transcript"
    return None


def _note_conflict_targets(store: Any, namespace: str, normalized: dict[str, Any]) -> None:
    record_ids = list(normalized["contradicts_record_ids"])
    if not record_ids:
        return
    states = _target_states(store, namespace, record_ids)
    for record_id in record_ids:
        if record_id not in states:
            normalized["reason_codes"].append("conflict_target_missing")
        elif states[record_id]:
            normalized["reason_codes"].append("stale_target")
        else:
            normalized["reason_codes"].append("conflict_target_present")


def _usable_prose(text: str, *, minimum_chars: int, minimum_words: int) -> bool:
    if len(text) < minimum_chars or len(_WORD_RE.findall(text)) < minimum_words:
        return False
    if text.endswith("?"):
        return False
    return _STATUS_RE.fullmatch(text) is None


def _load_claim_index(store: Any, namespace: str) -> dict[str, Any]:
    with store._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {MEMORY_ROW_SELECT}
            FROM memories
            WHERE namespace = ? AND kind = 'memory'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (namespace, SCAN_LIMIT + 1),
        ).fetchall()
    truncated = len(rows) > SCAN_LIMIT
    claims: list[_IndexedClaim] = []
    for sqlite_row in rows[:SCAN_LIMIT]:
        row = MemoryRow.from_sqlite(sqlite_row)
        indexed = _index_row(row)
        if indexed is not None:
            claims.append(indexed)
    return {"claims": claims, "truncated": truncated}


def _index_row(row: MemoryRow) -> _IndexedClaim | None:
    fields = parse_structured_record(row.content)
    if row.is_learning_candidate:
        if fields.get("record_type") != "learning-candidate":
            return None
        status = fields.get("candidate_status", "")
        if not status:
            status = next(
                (tag.split(":", 1)[1] for tag in row.tags if tag.startswith("candidate_status:")),
                "",
            )
        if status not in OPEN_CANDIDATE_STATUSES:
            return None
        claim = fields.get("claim", "")
        lane = "open"
    else:
        claim = fields.get("claim") or row.content
        if not fields.get("claim") and (len(claim) > 500 or "\n" in claim):
            return None
        lane = "durable"
    key = _claim_key(claim)
    if not key:
        return None
    tags = {str(tag) for tag in row.tags}
    stale = _is_stale(fields, tags)
    return _IndexedClaim(key, row.id, lane, stale)


def _is_stale(fields: Mapping[str, str], tags: set[str]) -> bool:
    values = {
        fields.get("governance_status", ""),
        fields.get("validity_status", ""),
        fields.get("status", ""),
    }
    if any(str(value).lower() in STALE_MARKERS for value in values if value):
        return True
    for tag in tags:
        prefix, separator, marker = tag.partition(":")
        if separator and prefix in {"status", "validity", "governance_status"} and marker in STALE_MARKERS:
            return True
    return False


def _target_states(store: Any, namespace: str, record_ids: Sequence[str]) -> dict[str, bool]:
    placeholders = ", ".join("?" for _ in record_ids)
    with store._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, tags_json, content
            FROM memories
            WHERE namespace = ? AND id IN ({placeholders})
            """,
            (namespace, *record_ids),
        ).fetchall()
    states: dict[str, bool] = {}
    for row in rows:
        try:
            tags = {str(tag) for tag in json.loads(row["tags_json"] or "[]") if isinstance(tag, str)}
        except json.JSONDecodeError:
            tags = set()
        states[str(row["id"])] = _is_stale(parse_structured_record(str(row["content"])), tags)
    return states


def _match(index: Mapping[str, Any], claim_key: str, *, lane: str) -> _IndexedClaim | None:
    for item in index["claims"]:
        if item.lane == lane and item.claim_key == claim_key:
            return item
    return None


def _durable_matches(index: Mapping[str, Any], claim_key: str) -> list[_IndexedClaim]:
    return [item for item in index["claims"] if item.lane == "durable" and item.claim_key == claim_key]


def _mark_stale_revalidation(normalized: dict[str, Any], matches: Sequence[_IndexedClaim]) -> None:
    normalized["recommended_action"] = "revalidate"
    normalized["relation"] = "revalidation"
    normalized["supersedes_record_ids"] = sorted({item.record_id for item in matches if item.stale})
    normalized["supersession_plan"] = STALE_REVALIDATION_PLAN
    normalized["reason_codes"] = [*list(normalized["reason_codes"]), "stale_durable_exact_claim"]


def _open_review_action(normalized: Mapping[str, Any]) -> tuple[str, str]:
    relation = str(normalized.get("relation") or "")
    if relation == "contradicted":
        return "revise", "contradicted"
    if relation == "revalidation":
        return "revalidate", "revalidation"
    return "merge", "duplicate"


def _mutations_promoted_durable(store: Any, memory_ids: Sequence[str]) -> bool:
    """Prove promotion from this capture's own rows, not a namespace count delta.

    Another writer can insert durable memory while capture is running. That row
    is not one of these ids, so it cannot flip the receipt.
    """

    unique_ids = list(dict.fromkeys(memory_id for memory_id in memory_ids if memory_id))
    if not unique_ids:
        return False
    placeholders = ", ".join("?" for _ in unique_ids)
    with store._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, is_learning_candidate
            FROM memories
            WHERE id IN ({placeholders})
            """,
            unique_ids,
        ).fetchall()
    candidate_by_id = {str(row["id"]): int(row["is_learning_candidate"] or 0) for row in rows}
    return any(candidate_by_id.get(memory_id, 1) == 0 for memory_id in unique_ids)


def _content_fields(content: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for line in content.splitlines():
        key, separator, value = line.partition(": ")
        if separator and key:
            fields.append((key, value))
    return fields


def _merge_json_field(fields: list[tuple[str, str]], key: str, extra: Sequence[str]) -> bool:
    if not extra:
        return False
    current_raw = _field_value(fields, key)
    try:
        current = json.loads(current_raw) if current_raw else []
    except json.JSONDecodeError:
        current = []
    if not isinstance(current, list):
        current = []
    merged = sorted({str(item) for item in current if str(item).strip()} | {item for item in extra if item})
    rendered = json.dumps(merged, ensure_ascii=True, sort_keys=True)
    if current_raw == rendered:
        return False
    _set_field(fields, key, rendered)
    return True


def _set_field(fields: list[tuple[str, str]], key: str, value: str) -> bool:
    for index, (existing_key, existing_value) in enumerate(fields):
        if existing_key == key:
            if existing_value == value:
                return False
            fields[index] = (key, value)
            return True
    fields.append((key, value))
    return True


def _field_value(fields: Sequence[tuple[str, str]], key: str) -> str:
    for existing_key, value in fields:
        if existing_key == key:
            return value
    return ""


def _claim_key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _safe_artifact_id(artifact: Any) -> str:
    if isinstance(artifact, Mapping):
        artifact_id = str(artifact.get("artifact_id", "")).strip()
        if _TOKEN_RE.fullmatch(artifact_id):
            return artifact_id
    return ""


def _item(
    artifact_id: str,
    *,
    disposition: str,
    reason_codes: list[str],
    recommended_action: str | None = None,
    relation: str | None = None,
    record_id: str | None = None,
    candidate_status: str | None = None,
    hidden_from_ordinary_recall: bool | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "disposition": disposition,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "recommended_action": recommended_action,
        "relation": relation,
        "record_id": record_id,
        "candidate_status": candidate_status,
        "hidden_from_ordinary_recall": hidden_from_ordinary_recall,
    }


def _event_receipt(
    *,
    boundary: str,
    namespace: str,
    disposition: str,
    reason_codes: list[str],
    items: list[dict[str, Any]] | None = None,
    writes: int = 0,
    automatic_promotion: bool = False,
    dedup_scan_truncated: bool = False,
) -> dict[str, Any]:
    return {
        "schema": CAPTURE_RECEIPT_SCHEMA,
        "boundary": boundary,
        "namespace": namespace,
        "disposition": disposition,
        "reason_codes": reason_codes,
        "writes": writes,
        "promotions": 1 if automatic_promotion else 0,
        "automatic_promotion": automatic_promotion,
        "public_mcp_surface_change": False,
        "dedup_scan_truncated": dedup_scan_truncated,
        "items": items or [],
    }


def run_write_side_capture_benchmark(cases_path: Path) -> dict[str, Any]:
    """Execute frozen capture cases and return separate pass/fail observations."""

    from .storage import MemoryStore

    payload = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    observations: list[dict[str, Any]] = []
    for case in payload["cases"]:
        observations.append(_run_benchmark_case(MemoryStore, TemporaryDirectory, case))
    failed = [item["id"] for item in observations if not item["passed"]]
    return {
        "schema": "memory.write_side_capture_benchmark_report.v1",
        "case_count": len(observations),
        "failed_case_count": len(failed),
        "failed_case_ids": failed,
        "positive_capture_count": sum(item["positive_captures"] for item in observations),
        "negative_control_write_count": sum(item["negative_writes"] for item in observations),
        "automatic_promotion_count": sum(item["promotions"] for item in observations),
        "observations": observations,
    }


def _run_benchmark_case(store_type: Any, temporary_directory: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    with temporary_directory() as temp_dir:
        root = Path(temp_dir)
        store = store_type(root / "bridge.db", log_dir=root / "logs")
        seed_ids: list[str] = []
        for seed in case.get("seed", []):
            stored = store.store(
                namespace=str(seed["namespace"]),
                kind="memory",
                title=str(seed.get("title") or "Seed memory"),
                content=str(seed["content"]),
                tags=list(seed.get("tags") or []),
            )
            seed_ids.append(str(stored["id"]))
        events = case.get("events") or [case["event"]]
        receipts = [capture_lifecycle_candidates(store, _bind_seed_ids(event, seed_ids)) for event in events]
        candidate_count = _candidate_count(store, str(events[-1].get("namespace", "")))
        explicit_visible = True
        if case.get("expect_explicit_recalled"):
            recalled = store.recall(
                namespace=str(case["seed"][0]["namespace"]),
                query=str(case["seed"][0]["content"]),
                limit=5,
            )
            explicit_visible = any(item["id"] == seed_ids[0] for item in recalled["items"])
        hidden_ok = True
        hidden_query = str(case.get("expect_hidden_query") or "")
        if hidden_query:
            hidden = store.recall(
                namespace=str((case.get("events") or [case["event"]])[-1]["namespace"]),
                query=hidden_query,
                limit=5,
            )
            hidden_ok = hidden["count"] == 0
        secret_absent = True
        secret = str(case.get("secret_absent") or "")
        if secret:
            secret_absent = _secret_absent(store, secret)
        passed = _benchmark_passed(
            case,
            receipts=receipts,
            candidate_count=candidate_count,
            explicit_visible=explicit_visible,
            secret_absent=secret_absent,
            hidden_ok=hidden_ok,
        )
        positive = sum(1 for receipt in receipts for item in receipt["items"] if item["disposition"] == "stored")
        negative_writes = sum(receipt["writes"] for receipt in receipts) if case.get("negative_control") else 0
        promotions = sum(receipt["promotions"] for receipt in receipts)
        return {
            "id": case["id"],
            "passed": passed,
            "positive_captures": positive,
            "negative_writes": negative_writes,
            "promotions": promotions,
            "dispositions": [item["disposition"] for receipt in receipts for item in receipt["items"]]
            or [receipt["disposition"] for receipt in receipts],
            "candidate_count": candidate_count,
        }


def _benchmark_passed(
    case: Mapping[str, Any],
    *,
    receipts: Sequence[Mapping[str, Any]],
    candidate_count: int,
    explicit_visible: bool,
    secret_absent: bool,
    hidden_ok: bool,
) -> bool:
    if case.get("expect_event_dispositions") != [receipt["disposition"] for receipt in receipts]:
        if "expect_event_dispositions" in case:
            return False
    expected_items = case.get("expect_item_dispositions")
    actual_items = [item["disposition"] for receipt in receipts for item in receipt["items"]]
    if expected_items is not None and actual_items != expected_items:
        return False
    if "expect_candidate_count" in case and candidate_count != case["expect_candidate_count"]:
        return False
    if any(receipt["automatic_promotion"] or receipt["public_mcp_surface_change"] for receipt in receipts):
        return False
    if case.get("expect_explicit_recalled") and not explicit_visible:
        return False
    if case.get("expect_hidden_query") and not hidden_ok:
        return False
    if case.get("secret_absent") and not secret_absent:
        return False
    if case.get("expect_recommended_action"):
        actions = [item.get("recommended_action") for receipt in receipts for item in receipt["items"]]
        if actions != case["expect_recommended_action"]:
            return False
    return True


def _bind_seed_ids(event: Mapping[str, Any], seed_ids: Sequence[str]) -> dict[str, Any]:
    rendered = json.dumps(event)
    for index, seed_id in enumerate(seed_ids):
        rendered = rendered.replace(f"__SEED_{index}__", seed_id)
    return json.loads(rendered)


def _candidate_count(store: Any, namespace: str) -> int:
    if not namespace:
        return 0
    with store._connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM memories
            WHERE namespace = ? AND COALESCE(is_learning_candidate, 0) = 1
            """,
            (namespace,),
        ).fetchone()
    return int(row["count"])


def _secret_absent(store: Any, secret: str) -> bool:
    with store._connect() as conn:
        rows = conn.execute("SELECT content FROM memories").fetchall()
    return all(secret not in str(row["content"]) for row in rows)
