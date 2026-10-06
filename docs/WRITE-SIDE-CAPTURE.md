# Write-side capture

A capture proposes memory. It does not make that proposal trusted.

Current Core accepts capture through generic lifecycle input. The event uses `kind: "capture"` and carries one `visible_artifact`. The artifact schema is `memory.visible_artifact.v1`. Core resolves the checkout, and only a single bound project can proceed. The caller does not choose the namespace.

These vendor-shaped keys make the whole event `ignore`: `hook_event_name`, `event`, `host`, `last_assistant_message`, `transcript_path`, and `hookSpecificOutput`. Current Core does not translate them into a capture and does not open a transcript.

The supported artifact classes are:

- `decision`, with a claim and a reason
- `constraint`, with a claim and a reason
- `gotcha`, already validated, with both a symptom and a fix
- `user_correction` of an identified record
- `handoff`, with a bounded claim and a reason

Anything else is left uncaptured. Raw transcripts, hidden reasoning, secrets, status chatter, generic summaries, and events without one safe bounded artifact produce no candidate. Capture does not invent context.

The complete `visible_artifact` object must be at most 4,000 UTF-8 bytes. For example:

```json
{
  "schema": "memory.visible_artifact.v1",
  "artifact_id": "decision-1",
  "artifact_class": "decision",
  "claim": "Keep write-side capture in the hidden review lane until a person reviews it.",
  "reason": "This constraint prevents unreviewed proposals from becoming trusted project guidance.",
  "scope": "project"
}
```

`lifecycle-hook` stores an accepted artifact through the existing learning-candidate path with `needs_review`. The boundary is `post_run`. Ordinary recall, browse, and export continue to hide that lane. `promote` still refuses a learning candidate. Explicit `store` behavior is unchanged, and this lane does not add an MCP tool.

The store step recomputes the writeback decision. Caller fields such as `decision`, `candidate_status`, `authority_class`, `tags`, and an artifact-local namespace cannot grant review approval or retarget the write. Core replaces caller evidence refs with `lifecycle:sha256:<digest>` of the exact visible artifact. That digest binds the proposal to the submitted object, not to verified truth.

Before inserting a new candidate, capture compares the normalized claim with durable memory and open candidates in the same namespace:

- the same current durable claim is not captured again; the receipt says `reject`
- the same claim on only a stale or superseded durable row is stored for review; the receipt says `revalidate`, and the old row is neither revived nor changed
- the same open candidate is strengthened in place; the receipt says `merge`
- a contradiction or user correction is stored for review; the receipt says `revise`, and the contradicted row is not changed

`automatic_promotion` comes only from the rows this capture inserted or updated. A durable write from another session in the same namespace does not count.

Unbound or ambiguous scope produces no candidate. Remote authority also produces no candidate: this lane does not fall back to a local database when a remote authority is selected. A capture error records unknown write and promotion counts. It is not a passing zero-write result.

Each capture appends metadata-only `lifecycle/capture-evidence.jsonl` under the selected bridge home.

The script `scripts/capture_lifecycle_candidates.py` is the local seam for an explicit `memory.lifecycle_capture_event.v1` file. It requires an explicit database and log directory, so it does not write to an ambient bridge by default. The checked benchmark is `benchmark/write-side-capture-cases.json`:

```bash
python ./scripts/run_write_side_capture_benchmark.py
```

## Historical v0.35 Codex Stop capture

Everything in this section describes tag `v0.35.0`. It is not current Core behavior. Current Core does not ship the Codex adapter, the Stop hook, or `scripts/check_codex_stop_capture.py`.

On that tag, the Codex host connection for this lane was only the Stop hook. Official Codex `PreCompact` carried a compaction trigger and an unstable transcript path, not a bounded visible artifact, so that historical lane did not read it and did not summarize the session. Stop carried `last_assistant_message`. The hook forwarded that message only when the entire message was one `memory.visible_artifact.v1` JSON object. A prose reply, a summary, or JSON wrapped in other text produced no candidate. The stored row stayed a hidden `needs_review` candidate on the `post_run` boundary.

The historical Codex hook resolved the project itself and then called the same capture boundary. The adapter replaced caller evidence refs with `codex-stop:sha256:<digest>` of the exact visible message. No transcript was opened and no summary model was called. Capture errors had unknown write and promotion counts, not a passing zero-write result. Unbound or ambiguous scope and remote authority failed closed without a local fallback.

Tag `v0.35.0` preserves `scripts/check_codex_stop_capture.py` and the recorded Codex Stop trial. That trial does not accept the current generic lifecycle input. The historical positive lane created one hidden `needs_review` candidate; the negative lane created zero. Explicitly seeded memory stayed unchanged, and ordinary recall, browse, and export excluded the candidate.

Private output includes `report.json`, controlled visible messages, receipts, and fixture databases, but no reasoning or full host event stream. The report hashes the tested adapter, capture core, and collector.

On 2026-09-30, Codex CLI 0.157.1 passed both lanes with an invocation-inline Stop hook: positive 1 candidate, negative 0, no automatic promotion, and seeded memory unchanged. Positive message SHA-256: `6f405badc5544ca4a41875d580245e65c630a6e70bd5c25dd3a62bd422aaa5ae`. The [checked-in evidence bundle](evidence/issue-53-codex-stop/README.md) contains the rerun after integration with HTTP recall, redacted native callback projections, verifier results, and a source-hash manifest for post-merge checks. Two earlier project-file hook attempts ended without invoking Stop and did not pass. This check does not certify plugin installation, project-file discovery, Windows hosts, remote capture, or read-side activation. It proves mechanical capture of an explicitly structured visible message, not autonomous discovery or summarization quality.

Host field reference for that historical hook: [official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).
