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

The script `scripts/capture_lifecycle_candidates.py` is the local seam. It requires an explicit database and log directory so it does not write to an ambient bridge by default. Host adapters from the lifecycle issue can call this seam; this lane does not own project resolution or host hook installation.
