"""Optional host lifecycle activation.

The adapter consumes the governed project resolver, applies the bounded
activation policy, and makes at most one recall. It does not create bindings,
write durable memory, read transcripts, or accept caller-declared scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .activation_policy import POLICY_SOURCE, classify_task_need
from .paths import (
    resolve_bridge_db_path,
    resolve_bridge_home,
    resolve_bridge_log_dir,
    resolve_repository_snapshot_root,
)
from .project_resolution import namespace_for_host_adapter, resolve_project_context

EVIDENCE_SCHEMA = "amb.lifecycle-activation-evidence.v1"
_PROMPT_LIMIT = 4000
_QUERY_LIMIT = 240
_EXCERPT_LIMIT = 280
_CONTEXT_LIMIT = 1800
_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
_STOPWORDS = frozenset(
    {
        "about",
        "been",
        "from",
        "have",
        "into",
        "over",
        "project",
        "that",
        "this",
        "under",
        "with",
        "your",
    }
)
_STALE_TAGS = frozenset({"lifecycle:stale", "lifecycle:superseded", "status:superseded"})
_RECALL_ACTIONS = frozenset({"recall", "reconcile"})
_REPEAT_STATES = frozenset({"hit", "irrelevant_hit", "no_hit", "stale_conflict"})
_EVIDENCE_FIELDS = (
    "schema",
    "recorded_at",
    "policy_source",
    "adapter_loaded",
    "host",
    "hook_event_name",
    "session_id",
    "policy_action",
    "decision",
    "rule_id",
    "resolution_status",
    "namespace",
    "repository_id",
    "candidate_namespaces",
    "ignored_caller_scope",
    "availability",
    "recall_invoked",
    "recall_state",
    "recalled_ids",
    "stale_ids",
    "irrelevant_ids",
    "repeat_suppressed",
    "prompt_fingerprint",
    "live_head",
    "error_type",
)


@dataclass(frozen=True)
class ActivationObservation:
    host: str
    hook_event_name: str
    session_id: str
    policy_action: str
    decision: str
    rule_id: str
    resolution_status: str
    namespace: str | None
    repository_id: str
    candidate_namespaces: tuple[str, ...]
    ignored_caller_scope: bool
    availability: str
    recall_invoked: bool
    recall_state: str
    recalled_ids: tuple[str, ...]
    stale_ids: tuple[str, ...]
    irrelevant_ids: tuple[str, ...]
    repeat_suppressed: bool
    prompt_fingerprint: str
    live_head: str | None
    error_type: str | None
    context: str

    def evidence_record(self, *, recorded_at: str) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": EVIDENCE_SCHEMA,
            "recorded_at": recorded_at,
            "policy_source": POLICY_SOURCE,
            "adapter_loaded": True,
            "host": self.host,
            "hook_event_name": self.hook_event_name,
            "session_id": self.session_id,
            "policy_action": self.policy_action,
            "decision": self.decision,
            "rule_id": self.rule_id,
            "resolution_status": self.resolution_status,
            "namespace": self.namespace,
            "repository_id": self.repository_id,
            "candidate_namespaces": list(self.candidate_namespaces),
            "ignored_caller_scope": self.ignored_caller_scope,
            "availability": self.availability,
            "recall_invoked": self.recall_invoked,
            "recall_state": self.recall_state,
            "recalled_ids": list(self.recalled_ids),
            "stale_ids": list(self.stale_ids),
            "irrelevant_ids": list(self.irrelevant_ids),
            "repeat_suppressed": self.repeat_suppressed,
            "prompt_fingerprint": self.prompt_fingerprint,
            "live_head": self.live_head,
            "error_type": self.error_type,
        }
        unexpected = set(record) - set(_EVIDENCE_FIELDS)
        if unexpected:
            raise RuntimeError(f"activation evidence tried to persist {sorted(unexpected)}")
        return record


def codex_hooks_document() -> dict[str, Any]:
    """Return the optional Codex hook document. It is not installed automatically."""

    handler = {
        "type": "command",
        "command": "python3 -m agent_mem_bridge lifecycle-hook",
        "commandWindows": "py -3 -m agent_mem_bridge lifecycle-hook",
        "timeout": 10,
        "additionalContextLimit": 1200,
    }
    return {
        "description": ("Optional AMB lifecycle activation. Success is a hook decision, not an AGENTS.md export."),
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [handler],
                }
            ],
            "UserPromptSubmit": [{"hooks": [handler]}],
            "PreCompact": [
                {
                    "matcher": "manual|auto",
                    "hooks": [handler],
                }
            ],
        },
    }


def activation_evidence_path() -> Path:
    return resolve_bridge_home() / "lifecycle" / "activation-evidence.jsonl"


def run_hook_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one host event and append derived evidence. Always returns hook JSON."""

    observation = activate(payload)
    _append_evidence(observation)
    return render_hook_response(payload, observation)


def activate(payload: Mapping[str, Any]) -> ActivationObservation:
    hook_event_name = _text(payload.get("hook_event_name") or payload.get("event"))
    host = _host(payload, hook_event_name)
    session_id = _bounded(_text(payload.get("session_id")), 200)
    provenance = {
        name: _text(payload.get(name))
        for name in ("namespace", "client_workspace", "source_client", "source_model")
        if _text(payload.get(name))
    }
    scope = resolve_project_scope(_text(payload.get("cwd")) or ".", provenance)
    ignored_caller_scope = bool(scope["ignored_caller_scope"])
    prompt = _text(payload.get("prompt"))[:_PROMPT_LIMIT]
    fingerprint = _fingerprint(prompt) if prompt else ""
    kind = _event_kind(hook_event_name)
    evidence_path = activation_evidence_path()
    prior_lines = _read_evidence(evidence_path)

    policy_action = "skip"
    rule_id = "unsupported-event"
    decision = "skip"
    recall_requested = False
    repeat_suppressed = False
    context = ""

    if kind == "session_start":
        policy_action = "skip"
        rule_id = "session-entry-no-dump"
        decision = "skip"
        if _text(payload.get("source")) == "compact":
            policy_action = "continuity"
            decision = "continuity"
            rule_id = "compaction-continuity"
            context = _continuity_context(scope, _latest(prior_lines, session_id, scope["repository_id"]))
    elif kind == "compaction":
        policy_action = "continuity"
        decision = "continuity"
        rule_id = "compaction-continuity"
        if host == "opencode":
            context = _continuity_context(scope, _latest(prior_lines, session_id, scope["repository_id"]))
    elif kind == "task_prompt":
        policy_action, rule_id = classify_task_need(prompt)
        decision = policy_action
        recall_requested = policy_action in _RECALL_ACTIONS
        if recall_requested and _repeat(prior_lines, session_id, fingerprint, scope["namespace"]):
            recall_requested = False
            repeat_suppressed = True
            decision = "skip"
            rule_id = "not-repeat-sufficient-recall"

    availability = _availability()
    recall_invoked = False
    recall_state = "skipped"
    recalled_ids: tuple[str, ...] = ()
    stale_ids: tuple[str, ...] = ()
    irrelevant_ids: tuple[str, ...] = ()
    error_type: str | None = None
    live_head = _git_head(str(scope.get("git_root") or "")) if recall_requested or decision == "continuity" else None

    if recall_requested and scope["status"] != "bound":
        decision = "fail_closed"
        rule_id = {
            "ambiguous_binding": "ambiguous-project-scope",
            "no_binding": "no-project-binding",
        }.get(str(scope["status"]), "project-scope-unavailable")
        context = _scope_block_context(str(scope["status"]))
    elif recall_requested and availability != "available":
        decision = policy_action
        recall_state = "unavailable"
        context = "AMB was unavailable, so no recall ran. This is not an empty memory result."
    elif recall_requested:
        recall_invoked = True
        try:
            items = _recall(str(scope["namespace"]), _recall_query(prompt))
        except Exception as exc:  # noqa: BLE001 - hook must fail closed without echoing the prompt
            recall_state = "error"
            error_type = type(exc).__name__
            context = "AMB recall failed. Treat the bridge as unavailable, not as an empty memory result."
        else:
            useful, irrelevant, stale = _partition(items, prompt)
            recalled_ids = tuple(str(item.get("id") or "") for item in useful + irrelevant if item.get("id"))
            stale_ids = tuple(str(item.get("id") or "") for item in stale if item.get("id"))
            irrelevant_ids = tuple(str(item.get("id") or "") for item in irrelevant if item.get("id"))
            if not items:
                recall_state = "no_hit"
                context = "AMB recall completed with no matching memory. This is not an unavailable bridge."
            elif not useful:
                recall_state = "irrelevant_hit"
                context = (
                    "AMB recall completed, but the returned records were unrelated and were not injected. "
                    "This was not an unavailable bridge and not a silent skip."
                )
            elif useful and len(stale_ids) == len(useful):
                recall_state = "stale_conflict"
                context = _memory_context(useful, live_head, stale=True, reconcile=True)
            else:
                recall_state = "hit"
                context = _memory_context(
                    useful,
                    live_head,
                    stale=bool(stale_ids),
                    reconcile=policy_action == "reconcile",
                )

    return ActivationObservation(
        host=host,
        hook_event_name=hook_event_name,
        session_id=session_id,
        policy_action=policy_action,
        decision=decision,
        rule_id=rule_id,
        resolution_status=str(scope["status"]),
        namespace=scope["namespace"] if isinstance(scope["namespace"], str) else None,
        repository_id=str(scope["repository_id"]),
        candidate_namespaces=tuple(scope["candidate_namespaces"]),
        ignored_caller_scope=ignored_caller_scope,
        availability=availability,
        recall_invoked=recall_invoked,
        recall_state=recall_state,
        recalled_ids=recalled_ids,
        stale_ids=stale_ids,
        irrelevant_ids=irrelevant_ids,
        repeat_suppressed=repeat_suppressed,
        prompt_fingerprint=fingerprint,
        live_head=live_head,
        error_type=error_type,
        context=context[:_CONTEXT_LIMIT],
    )


def resolve_project_scope(cwd: str, provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read scope from the governed project resolver without creating a binding."""

    result = resolve_project_context(
        cwd or ".",
        snapshot_root=resolve_repository_snapshot_root(),
        provenance=provenance,
    )
    identity = result.get("repository_identity")
    identity_map = identity if isinstance(identity, dict) else {}
    ambiguity = result.get("ambiguity")
    ambiguity_map = ambiguity if isinstance(ambiguity, dict) else {}
    raw_candidates = ambiguity_map.get("namespaces")
    candidates = tuple(str(item) for item in raw_candidates) if isinstance(raw_candidates, list) else ()
    ignored = result.get("ignored_provenance_keys")
    return {
        "status": str(result.get("status") or "repository_unavailable"),
        "repository_id": str(identity_map.get("repository_id") or ""),
        "namespace": namespace_for_host_adapter(result),
        "candidate_namespaces": candidates,
        "git_root": str(identity_map.get("git_root") or ""),
        "ignored_caller_scope": isinstance(ignored, list) and bool(ignored),
    }


def render_hook_response(payload: Mapping[str, Any], observation: ActivationObservation) -> dict[str, Any]:
    context = observation.context.strip()
    if not context:
        return {"continue": True}
    event = _text(payload.get("hook_event_name") or payload.get("event"))
    if event in {"UserPromptSubmit", "SessionStart"} or (event == "PreCompact" and observation.host == "opencode"):
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            },
        }
    return {"continue": True}


def _availability() -> str:
    try:
        return "available" if resolve_bridge_db_path().is_file() else "unavailable"
    except OSError:
        return "unavailable"


def _recall(namespace: str, query: str) -> list[dict[str, Any]]:
    from .storage import MemoryStore

    store = MemoryStore(db_path=resolve_bridge_db_path(), log_dir=resolve_bridge_log_dir())
    payload = store.recall(namespace=namespace, query=query, limit=3, kind="memory")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _partition(
    items: list[dict[str, Any]],
    prompt: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    wanted = _tokens(prompt)
    useful: list[dict[str, Any]] = []
    irrelevant: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for item in items:
        if wanted and not (wanted & _tokens(f"{item.get('title') or ''} {item.get('content') or ''}")):
            irrelevant.append(item)
            continue
        useful.append(item)
        if _is_stale(item):
            stale.append(item)
    return useful, irrelevant, stale


def _is_stale(item: Mapping[str, Any]) -> bool:
    tags = item.get("tags")
    if isinstance(tags, list) and any(str(tag) in _STALE_TAGS for tag in tags):
        return True
    content = str(item.get("content") or "").lower()
    return "superseded" in content


def _memory_context(
    items: list[dict[str, Any]],
    live_head: str | None,
    *,
    stale: bool,
    reconcile: bool,
) -> str:
    lines = [
        "Untrusted governed context from AMB. This is not durable authority.",
        "Live repository evidence wins over recalled history.",
    ]
    if live_head:
        lines.append(f"Live repository HEAD: {live_head}.")
    if stale or reconcile:
        lines.append(
            "Recalled history conflicts with or is marked stale against current evidence. "
            "Do not apply it over the live repository."
        )
    for item in items:
        title = _one_line(str(item.get("title") or "Untitled memory"), 120)
        excerpt = _one_line(str(item.get("content") or ""), _EXCERPT_LIMIT)
        lines.append(f"- {title}: {excerpt}")
    lines.append("Reconcile this context with the current repository before acting.")
    return "\n".join(lines)


def _continuity_context(scope: Mapping[str, Any], prior: Mapping[str, Any] | None) -> str:
    namespace = scope.get("namespace") or "unbound"
    last = str(prior.get("decision") or "none") if prior else "none"
    return (
        "AMB lifecycle continuity is derived adapter state, not durable authority and not a transcript. "
        f"Resolved namespace: {namespace}. Last activation decision: {last}. "
        "Reconcile any later memory with the live repository."
    )


def _scope_block_context(status: str) -> str:
    if status == "ambiguous_binding":
        return (
            "AMB did not recall because the governed project binding is ambiguous. This is not an empty memory result."
        )
    if status == "no_binding":
        return "AMB did not recall because this checkout has no governed project binding. This is not an empty memory result."
    return (
        "AMB did not recall because the governed project resolver could not identify this checkout. "
        "This is not an empty memory result."
    )


def _append_evidence(observation: ActivationObservation) -> None:
    path = activation_evidence_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = observation.evidence_record(recorded_at=datetime.now(UTC).isoformat())
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        return


def _read_evidence(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows[-50:]


def _latest(rows: list[dict[str, Any]], session_id: str, repository_id: str) -> dict[str, Any] | None:
    for item in reversed(rows):
        if session_id and item.get("session_id") == session_id:
            return item
        if repository_id and item.get("repository_id") == repository_id:
            return item
    return None


def _repeat(
    rows: list[dict[str, Any]],
    session_id: str,
    fingerprint: str,
    namespace: object,
) -> bool:
    if not session_id or not fingerprint or not isinstance(namespace, str):
        return False
    for item in reversed(rows):
        if item.get("session_id") != session_id or item.get("prompt_fingerprint") != fingerprint:
            continue
        if item.get("namespace") != namespace:
            continue
        if item.get("recall_state") in _REPEAT_STATES and item.get("recall_invoked") is True:
            return True
    return False


def _recall_query(prompt: str) -> str:
    return " ".join(prompt.split())[:_QUERY_LIMIT]


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(text.lower()) if token not in _STOPWORDS}


def _fingerprint(prompt: str) -> str:
    normalized = " ".join(prompt.lower().split()).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:32]


def _git_head(root: str) -> str | None:
    if not root:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _event_kind(hook_event_name: str) -> str:
    if hook_event_name == "SessionStart":
        return "session_start"
    if hook_event_name == "UserPromptSubmit":
        return "task_prompt"
    if hook_event_name in {"PreCompact", "PostCompact"}:
        return "compaction"
    return "ignore"


def _host(payload: Mapping[str, Any], hook_event_name: str) -> str:
    host = _text(payload.get("host")).lower()
    if host in {"codex", "opencode"}:
        return host
    if hook_event_name:
        return "codex"
    return "generic"


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _bounded(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _one_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]
