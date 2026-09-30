# Write-side capture

Lifecycle capture proposes memory. It does not make that proposal trusted.

At a host boundary, a caller may submit a bounded `memory.lifecycle_capture_event.v1`. The supported boundaries are `pre_compaction`, `session_stop`, `explicit_handoff`, and `post_run`. The capture lane accepts only structured visible artifacts in these classes:

- explicit decision plus reason
- explicit constraint
- validated gotcha, with both a symptom and a fix
- user correction of an identified record
- bounded handoff fact

Anything else is left uncaptured. Raw transcripts, hidden reasoning, secrets, status chatter, generic summaries, and events without a safe bounded artifact produce no candidate. An unsupported host input stays unsupported; capture does not invent context.

Accepted artifacts are stored through the existing learning-candidate path with `needs_review`. Ordinary recall, browse, and export continue to hide that lane. `promote` still refuses a learning candidate. Explicit `store` behavior is unchanged, and this lane does not add an MCP tool.

The store step recomputes the writeback decision. Caller fields such as `decision`, `candidate_status`, `authority_class`, `tags`, and an artifact-local namespace cannot grant review approval or retarget the write. A host seam may also call `capture_at_boundary`; that boundary argument wins over a boundary label inside the event.

Before inserting a new candidate, capture compares the normalized claim with durable memory and open candidates in the same namespace:

- the same current durable claim is not captured again; the receipt says `reject`
- the same claim on only a stale or superseded durable row is stored for review; the receipt says `revalidate`, and the old row is neither revived nor changed
- the same open candidate is strengthened in place; the receipt says `merge`
- a contradiction or user correction is stored for review; the receipt says `revise`, and the contradicted row is not changed

`automatic_promotion` is decided only from the rows this capture inserted or updated. A durable write from another session in the same namespace does not count.

The checked benchmark is `benchmark/write-side-capture-cases.json`. Run it with:

```bash
python ./scripts/run_write_side_capture_benchmark.py
```

The Codex host connection is only the `Stop` hook. Official Codex `PreCompact` carries a compaction trigger and an unstable transcript path, not a bounded visible artifact, so this lane does not read it and does not summarize the session. `Stop` carries `last_assistant_message`. The hook forwards that message only when the entire message is one `memory.visible_artifact.v1` JSON object. A prose reply, a summary, or JSON wrapped in other text produces no candidate. The stored row stays a hidden `needs_review` candidate on the `post_run` boundary.

The script `scripts/capture_lifecycle_candidates.py` is the local seam for an explicit event file. It requires an explicit database and log directory so it does not write to an ambient bridge by default. The Codex hook resolves the project itself and then calls the same capture boundary. This lane still does not add an MCP tool or promote a candidate.

## Codex Stop input and evidence

The complete visible message must be at most 4,000 UTF-8 bytes. For example:

```json
{"schema":"memory.visible_artifact.v1","artifact_id":"decision-1","artifact_class":"decision","claim":"Keep write-side capture in the hidden review lane until a person reviews it.","reason":"This constraint prevents unreviewed proposals from becoming trusted project guidance.","scope":"project"}
```

Session and turn provenance come from the host event; the namespace comes from
the governed project binding. The adapter replaces caller evidence refs with
`codex-stop:sha256:<digest>` of the exact visible message. This binds the proposal
to visible input, not to verified truth. No transcript is opened or summary model
called. Each Stop appends metadata-only `lifecycle/capture-evidence.jsonl` under
the selected bridge home. Capture errors have unknown write/promotion counts,
not a passing zero-write result. Unbound or ambiguous scope and remote authority
fail closed without a local fallback.

## Live host check

Use a Python environment with this repository and its dependencies:

```bash
python scripts/check_codex_stop_capture.py \
  --output /path/to/new-private-evidence-directory \
  --model <available-model> --reviewed-fixture-hook
```

The collector ignores user config and rules. Supply necessary model/provider
settings with repeated `-c key=value` arguments; never put credentials in
arguments. Authentication stays with the host's usual mechanism. The approval
flag permits the reviewed, invocation-scoped fixture hook without installing
hooks or persisting trust. Known ambient hook files are rejected and plugins
disabled. Two fixture repositories and local stores leave the operator's AMB
authority untouched.

Both lanes require exit 0, the exact nonempty visible fixture reply,
`turn.completed`, and one matching Stop receipt. Missing callbacks cannot pass
the negative control; model tool execution invalidates the trial. The positive
lane requires one hidden `needs_review` candidate, the negative lane zero.
Explicitly seeded memory must remain unchanged and visible. Ordinary recall,
browse, and export must exclude the candidate.

Private output includes `report.json`, controlled visible messages, receipts,
and fixture databases, but no reasoning or full host event stream. The report
hashes the tested adapter, capture core, and collector.

On 2026-09-30, Codex CLI 0.157.1 passed both lanes with an invocation-inline
Stop hook: positive 1 candidate, negative 0, no automatic promotion, and seeded
memory unchanged. Positive message SHA-256:
`6f405badc5544ca4a41875d580245e65c630a6e70bd5c25dd3a62bd422aaa5ae`.
Tested adapter SHA-256:
`a63f2ccb66323a560d7ce5d0d1c43d3c6aaa81388681870ae19561081ba506bd`.
Two earlier project-file hook attempts ended without invoking Stop and did not
pass. This check does not certify plugin installation, project-file discovery,
Windows hosts, remote capture, or read-side activation. It proves mechanical
capture of an explicitly structured visible message, not autonomous discovery
or summarization quality.

Host field reference: [official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).
