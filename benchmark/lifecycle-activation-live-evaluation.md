# Lifecycle activation live evaluation

The 2026-10-03 Codex collection completed all ten frozen v2 cases: **3 PASS,
7 FAIL, 0 INCONCLUSIVE**. The benchmark instrument passed; lifecycle activation
did not. This is an authored summary of private live artifacts, not a checked-in
raw trace or a release acceptance record.

v0.35.0 is on owner-directed **release hold**. The seven activation failures are
product repair evidence for #51; they do not make the completed measurement
cohort invalid.

## Collection condition

The run used Codex 0.160.0 with `gpt-6-luna`, `adapter_enabled`,
`remote_loopback`, and `invocation_inline` hook loading. Source was based on
`73529b052f86a285a36f00376b5829b8ef74444e`, with the scoped collector repair.

The collector starts a temporary authenticated loopback HTTP authority outside
the agent filesystem. The isolated client receives governed bindings, a temporary
credential, and callback evidence; the seeded database is not mounted. The
worktree interpreter and source are mounted at separate sandbox paths. An
unavailable-case socket reserves its port without listening.

Each final trace has exit status 0, terminal `turn.completed`, nonempty final
text, one `UserPromptSubmit` callback, matching frozen identities, and a passing
isolation preflight. No final case was marked contaminated. The material handoff
has one adapter-mediated remote recall with a useful hit in the governed
namespace; its final response contains the memory-only handoff token.

This condition explicitly enables native hooks and supplies the callback through
invocation configuration. It does not establish plugin or project-file discovery.
The historical plain-MCP FAIL remains a separate baseline. The original
isolation failure, HTTP-startup failure, and duplicate-hook pilot were retained
separately and are excluded from these final results.

## Per-case results

| Frozen case | Verdict | Observed behavior |
| --- | --- | --- |
| `fresh-session-continuation` | FAIL | Required recall skipped; continuation token absent. |
| `architecture-constraint` | FAIL | Required recall skipped; architecture token absent. |
| `known-project-gotcha` | FAIL | Required recall skipped; gotcha token absent. |
| `indirect-history-dependency` | FAIL | Required recall skipped; agreed approach token absent. |
| `stale-superseded-conflict` | FAIL | Required recall skipped; scorer also reports `repo_evidence_missing`. |
| `no-relevant-memory` | FAIL | No-hit wording without an actual recall/no-hit receipt. |
| `amb-unavailable` | FAIL | Recall skipped; required unavailable-status phrase absent. |
| `isolated-typo` | PASS | No unnecessary recall during the local edit. |
| `repeated-recall` | PASS | No unnecessary recall; the requested edit was not performed. |
| `cross-client-handoff` | PASS | One useful remote recall; correct namespace and final token. |

The frozen scorer recognizes only a bounded set of repository-read commands.
The stale-conflict raw trace actually shows `rg` finding the current token in
`NOTES.md`; its unrecognized read is an instrumentation limitation, not proof
that the agent inspected nothing. Its required-recall failure remains valid.
The repeated-recall verdict measures activation restraint, not edit completion
or repeat-suppression across multiple native turns.

### Preserved result and read-measurement revision

The opt-in `codex-repo-read-v1` revision corrected that `rg` blind spot and
re-scored all ten existing raw traces. Both the original legacy-v2 report and
revised report remain **3 PASS, 7 FAIL, 0 INCONCLUSIVE**. Only stale-conflict's
false `repo_evidence_missing` reason was removed; its `required_recall_miss`
remains. Required recall is still 1/5, and false activation is still 0/2.

The re-score validated legacy raw-field extraction, each original saved report,
isolation preflights, grader and case-pack identities, and all 40 original file
hashes. It wrote a separate manifest, old report, new report and observations,
without overwriting prior artifacts or making model calls. The unchanged frozen
grader SHA-256 starts `61683f3a7f87`. See
[the versioned contract](lifecycle-activation-codex-repo-read-v1.md) for supported
read shapes and source-freeze requirements. This correction does not establish
improved activation policy; that needs fresh #51 trials.

## Separate metrics

| Metric | Final Codex result |
| --- | --- |
| Required-recall hit rate | 1/5 |
| False activation on negative controls | 0/2 |
| Namespace correctness given actual recall | 1/1 |
| Useful hit given actual recall | 1/1 |
| Successful no-hit correctness | 0/1 |
| Unavailable-vs-empty correctness | 0/1 |
| Stale-memory misuse count | 0 |
| Repeated/unnecessary recall count | 0 |
| End-to-end native latency range | 2.58–16.51 seconds |
| Reported input/output token range | 7,228–37,438 / 8–247 |

Token counts include the host turn; latency includes native execution and is not
isolated hook overhead. There is no matched timing baseline or efficacy claim.
Conditional 1/1 results do not establish broad retrieval quality. Zero stale
misuse does not establish reconciliation, because the stale case failed. The
typo fixture's ambiguous binding does not prove namespace resolution.

## Reproduce the reference condition

Use Linux with working `bwrap`, an installed Codex CLI, the collector's configured
model gateway, and a worktree-local environment installed with `.[dev]`. Choose
a currently available model explicitly; these commands used `gpt-6-luna`.
Keep output outside the repository and use a fresh directory for each run.

```bash
.venv/bin/python scripts/run_lifecycle_activation_benchmark.py check
.venv/bin/python scripts/run_lifecycle_activation_benchmark.py collect-codex \
  --pack v2 --condition adapter_enabled --adapter-backend remote_loopback \
  --case-id cross-client-handoff --model gpt-6-luna --timeout 150 \
  --out /path/to/private-output/codex-handoff
```

Repeat with each ID in `lifecycle-activation-v2.json`. Aggregate the resulting
observation objects into a JSON array and run:

```bash
.venv/bin/python scripts/run_lifecycle_activation_benchmark.py score \
  --observations /path/to/private-output/observations.json --pack v2 --format json \
  --report-path /path/to/private-output/report.json
```

Each collector output contains `observation.json`, `codex.jsonl`,
`preflight.json`, and `report.json`. Review callback evidence and terminal output
alongside the score; the instrument's PASS is not a host-product PASS.

## Secondary host

OpenCode remains **NOT_RUN** in this collection. The installed CLI's run options
were inspected, but no secondary native trial or isolation acceptance is claimed.
Its existing collector has a separate local adapter binding; the Codex-only
`remote_loopback` option does not repair or validate that lane. On a configured
OpenCode host, the explicit trial entry point is below. The isolated client does
not inherit the user's provider configuration; model-provider/auth binding must
also be established there. These commands are not a secondary readiness claim.

```bash
.venv/bin/python scripts/run_lifecycle_activation_benchmark.py collect-opencode \
  --pack v2 --condition adapter_enabled --case-id cross-client-handoff \
  --model local/gpt-6-luna --timeout 150 \
  --out /path/to/private-output/opencode-handoff
```

Retain preflight failures or missing terminal/tool evidence as failed collection
or INCONCLUSIVE, never as a passing secondary lane.

## Remaining work and authority

This change is limited to #50's measurement system. Activation-policy repair
belongs to #51's separate worktree and is excluded from this change. References
to #51 below identify product failures exposed by the benchmark; passing those
product cases is not a prerequisite for completing #50's measurement contract.
The v0.35.0 release hold is a separate owner decision.

Most required scenarios were skipped by `no-material-history-need`. Activation
coverage, no-hit execution, unavailable discrimination, and live repeat handling
are follow-up repair targets for #51; this measurement change does not edit the
policy. It does not supply #58's full native remote-authority acceptance.

The collection-time focused collector suite passed 39 tests. The combined lifecycle,
HTTP, OpenCode-plugin, and remote-authority-check suites returned 104 PASS and
one FAIL: the unchanged OpenCode hanging-child test reported 12.523 seconds
against its 12-second upper bound. An isolated rerun also failed at 12.532
seconds; an earlier isolated rerun passed. The threshold was not relaxed.
Ruff and diff-whitespace checks passed. Those collection checks did not cover
the full project suite. The attempted final collection review by Ren returned no
verdict after two timeouts and an unavailable fallback. Collection acceptance
relied on Cole self-review plus executable checks.

The separate measurement-revision source review by Ren returned
`PASS_WITH_WARNINGS`: extraction, source binding and unit tests were inspected,
but the private live cohort was not independently reproduced. Its nonblocking
limits include multi-`pwd` grammar coverage, receipts living in observations
rather than reports, and nontransactional output-directory writes. The
measurement-only PR preflight passed 148 focused tests, Ruff lint/format, the
23-file CI mypy target, frozen pack/revision hashes, the ten-case benchmark check,
and release-contract/public-surface/onboarding checks. It reproduced the saved
old/new reports byte-for-byte by re-scoring the original ten traces with zero
model calls. A separate OpenCode-plugin/HTTP run returned 13 PASS and one FAIL:
the unchanged hanging-child test measured 12.668 seconds against the same
12-second limit. The assertion and adapter were not changed.

The full project-suite PR run subsequently passed all 1309 tests in 221.52
seconds, with one Python tarfile deprecation warning. That successful run does
not erase the separate intermittent OpenCode timing failure above. The canonical
status page's collected test count was synchronized to 1309; no package version
or release authority changed.

The live evidence now includes a passing material adapter-enabled case, making
Issue #50's measurement acceptance reviewable. Issue status, merge, publication,
deployment, and production acceptance remain owner decisions. Nothing was
pushed, published, deployed, or closed by this collection. Private traces and
credentials must not be committed or pasted into public issues.
