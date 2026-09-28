# Lifecycle Activation Benchmark v1

Status: **FROZEN** on 2026-09-27, before any measured host run.
The companion `.sha256` file is the identity of this rubric and the case file.
Do not edit either file after observing a live host. A defective case must be
versioned as v2 with a new hash; the v1 result remains part of the record.

This pack measures whether a coding host invokes AMB at the moments prior
project context can change a task, and whether it avoids invocation when the
checkout is already sufficient. It does not rank hosts, change durable memory
semantics, learn automatically, require the Context Compiler, or add a public
MCP tool.

Closed pull request 48 supplied the activation classes, negative controls, and
the rule that a sentence such as "I remembered" is not invocation evidence.
That pull request is not the architecture of this benchmark, and its synthetic
trials are not host results.

## Evidence

A case is graded only from an observation trace:

- host id, version, and model;
- the bound fixture namespace;
- whether AMB was available;
- actual AMB tool calls, including a complete trace with zero calls;
- the namespace argument on each recall;
- result state: useful hit, irrelevant hit, no-hit, or unavailable;
- for the stale-port case, the current checkout file that was read;
- latency and token counts when the host trace exposes them.

`PASS`, `FAIL`, and `INCONCLUSIVE` stay separate. A missing observation is
`NOT_RUN`, which is not success. `N/A` is not used for a missing host.

## Cases

| Case | Expected decision |
|---|---|
| fresh-session continuation | Recall the listener token before answering. |
| prior architecture constraint | Recall the storage-authority token before adding a cache. |
| known project gotcha | Recall the report-writing gotcha token. |
| indirect history dependency | Recall without the words memory, remember, recall, or AMB. |
| stale/superseded conflict | Recall the retired port, read `NOTES.md`, and answer with the current port only. |
| no relevant memory | A successful empty recall is a no-hit, not an outage. |
| AMB unavailable | An error is unavailable. Do not call it an empty memory or invent the token. |
| isolated typo | Do not call AMB. |
| repeated recall | Do not call AMB again when the token is already in the session and file. |
| cross-client handoff | Recall `project:lifecycle-fixture`, not the other fixture project. |

The JSON file holds the prompts, fixture files, seeded memories, and the
adequate, defective, and missing-evidence probes. Probes are grading controls.
They are not substitute prompts.

## Metrics

Report these separately. Do not collapse them into one score. Rates use
adjudicated observations only. `INCONCLUSIVE` and `NOT_RUN` are counts, not
passes. A rate is null when its denominator is zero.

- required-recall hit rate: `must_recall` cases with at least one recall call;
- false activation rate: negative controls with any AMB tool call;
- namespace resolution correctness: recall calls whose namespace matches the bound project, over recall calls;
- useful-hit rate conditional on recall: useful results over recall calls in cases that have a useful fixture memory;
- no-hit correctness: empty successful recalls judged `PASS`;
- unavailable-versus-empty discrimination: unavailable cases judged `PASS`;
- stale-memory misuse count;
- repeated or unnecessary recall count;
- overhead: per-case latency, recall-call count, and token counts. There is no numeric latency gate. A recall count above the case maximum is pathological.

## Host lanes

Codex is the reference host. OpenCode is the secondary host. A lane with no
complete live trace stays `NOT_RUN`.

Isolated Codex collection:

```bash
python ./scripts/run_lifecycle_activation_benchmark.py collect-codex --case-id known-project-gotcha --out <temp-dir>
```

`collect-codex` is intentionally not part of `check`. The frozen checker must
pass before a live run, and a live run must use a temporary bridge home.
Do not point it at a production database.

OpenCode is runnable without being claimed as run:

```bash
python ./scripts/run_lifecycle_activation_benchmark.py print-host-plan --host opencode --case-id known-project-gotcha
```

The printed command is the runnable path. It is `NOT_RUN` until an observation
with `execution_kind=live` and a complete trace is scored.

## Non-claims

- Synthetic probe passes do not prove a live host.
- A live `FAIL` is a host result. It does not make the frozen instrument invalid.
- This pack does not implement a host adapter, a project-id resolver, or write-side capture.
- Task-edit quality is outside the activation verdict.
