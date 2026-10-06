# Matched Codex lifecycle activation evidence

This is historical v0.35 evidence. The collector command below is preserved on
tag `v0.35.0` and is not a current Core entry point.

The repaired source at `483c755e7299b96990b0a75f4363027c21138c47` completed
all ten frozen v2 cases: **10 PASS / 0 FAIL / 0 INCONCLUSIVE**.
`report.json` is the unchanged frozen grader output; `receipt.json` binds the
source bytes, conditions, actual callback states and isolation checks.

The reference condition is Codex CLI 0.160.0 / `gpt-6-luna` / `adapter_enabled`
/ `remote_loopback` / `invocation_inline`, with `codex-repo-read-v1`, unchanged
cases/expected decisions/grader, 150-second native timeout, 210-second outer
timeout and two-case parallelism. A clean temporary Python 3.12.3 environment
uses the exact pinned dependency versions from the worktree environment.
The remote fixture authority is not mounted into the model filesystem. Every
case passes both isolation preflights, ends normally, has a nonempty final
answer and preserves durable-memory rows. No-hit and unavailable have actual
`no_hit` and `error` callbacks; stale reconciliation reads current repository
evidence. Negative controls do not recall.

The canonical #50 baseline remains **3 PASS / 7 FAIL** in
[`lifecycle-activation-codex-2026-10-03.anchor.json`](../../../benchmark/lifecycle-activation-codex-2026-10-03.anchor.json).
An isolation-rejected attempt and a valid intermediate **8 PASS / 2 FAIL** run
are retained separately. The latter recalled the correct historical values
but refused to use values absent from repository files. The final repair
clarifies historical answers versus current-state verification without
changing recall scope, policy cues, cases, expected decisions or scoring.
Old verdicts and raw evidence are not overwritten.

Use the repaired collection entry point:

```bash
python scripts/run_codex_lifecycle_activation_benchmark.py collect-codex \
  --pack v2 --condition adapter_enabled --model gpt-6-luna \
  --adapter-backend remote_loopback --measurement-revision codex-repo-read-v1 \
  --timeout 150 --case-id known-project-gotcha --out /path/to/new/evidence
```

#50 freezes the entire original CLI and evidence helper. They remain unchanged;
the repaired entry point delegates re-scoring and non-Codex commands to the
canonical CLI. The frozen grader's legacy runnable-path metadata is retained
in the report; the command above selects the repaired collector.

These are bounded rule-based activation results, not general natural-language
history detection or a productivity/performance result. The frozen repeated
case is a recall-budget negative control; exact-material-repeat and native
compaction remain separate auxiliary evidence. The retained Sol/local/project
hook 10/10 is additional historical evidence on its original source bytes.
OpenCode is NOT_RUN in its documented pending lane. Native Windows activation,
project/plugin discovery, concurrent receipt streams and broad model/host
generalization are not certified here.

Raw host traces, credentials, databases and private paths are not published.
Hashes bind retained bytes under the trusted collector; they are not signatures,
authenticated vendor identity or independent public reconstruction of raw trials.
CI/review, merge, source-bound post-merge verification, issue closure and release
approval remain separate gates.
