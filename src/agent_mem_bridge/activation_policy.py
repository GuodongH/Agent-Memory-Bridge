"""Host-independent activation rules for optional AMB lifecycle adapters.

The rule ids and lexical cues come from the closed canonical semantic policy.
This module does not render host instructions and does not treat an ``AGENTS.md``
export as activation.
"""

from __future__ import annotations

POLICY_SOURCE = "canonical-policy-v1"

# Bounded lexical cues. They approximate the canonical rules for a deterministic
# hook. They are not an LLM classifier and they do not decide bridge health.
_DECISION_MARKERS = (
    "we decided",
    "decided previously",
    "prior decision",
    "previous decision",
    "recorded constraint",
    "project rule",
    "project constraint",
)
_FRESH_SESSION_MARKERS = (
    "fresh session",
    "new session",
    "handoff",
    "compaction",
    "pick up",
    "other coding client",
    "cross-client",
    "from the other",
)
_ARCHITECTURE_MARKERS = (
    "architecture",
    "schema",
    "new table",
    "run ledger",
    "durable authority",
    "migration",
    "release check",
    "security",
    "governance",
)
_GOTCHA_MARKERS = (
    "same failure",
    "same gotcha",
    "hit this same",
    "cross-client configuration failure",
    "known failure",
)
_INDIRECT_HISTORY_MARKERS = (
    "way this repository has been kept safe",
    "honor the way",
    "project-specific exception",
)
_CONFLICT_MARKERS = (
    "old setup",
    "superseded",
    "stale",
    "remembered release",
    "current server shows",
    "which should guide",
    "conflict",
    "setup --force",
)
_TRIVIAL_MARKERS = (
    "misspelled",
    "typo",
    "formatting",
    "import fix",
    "run formatting",
    "deterministic",
)
_MUST_MARKERS = (
    *_DECISION_MARKERS,
    *_FRESH_SESSION_MARKERS,
    *_ARCHITECTURE_MARKERS,
    *_GOTCHA_MARKERS,
    *_INDIRECT_HISTORY_MARKERS,
)


def classify_task_need(prompt: str) -> tuple[str, str]:
    """Return ``(action, rule_id)`` for one task prompt.

    ``action`` is ``skip``, ``recall``, or ``reconcile``. Availability and
    project scope are decided by the caller from live evidence, never from
    wording in the prompt.
    """

    text = " ".join(prompt.lower().split())
    if not text:
        return "skip", "no-material-history-need"
    trivial = any(marker in text for marker in _TRIVIAL_MARKERS) and not any(marker in text for marker in _MUST_MARKERS)
    if trivial:
        return "skip", "not-for-deterministic-edit"
    if any(marker in text for marker in _CONFLICT_MARKERS):
        return "reconcile", "live-history-conflict"
    if any(marker in text for marker in _INDIRECT_HISTORY_MARKERS):
        return "recall", "indirect-history-reference"
    if any(marker in text for marker in _GOTCHA_MARKERS):
        return "recall", "known-project-gotcha"
    if any(marker in text for marker in _FRESH_SESSION_MARKERS):
        return "recall", "fresh-session-or-handoff"
    if any(marker in text for marker in _ARCHITECTURE_MARKERS):
        return "recall", "constrained-architecture-choice"
    if any(marker in text for marker in _DECISION_MARKERS):
        return "recall", "material-prior-context"
    if "refactor" in text or "alternatives" in text or "ambiguous" in text or "design choice" in text:
        return "skip", "large-design-choice"
    return "skip", "no-material-history-need"
