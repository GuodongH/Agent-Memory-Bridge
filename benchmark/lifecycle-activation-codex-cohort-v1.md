# Codex cohort binding v1

`codex-cohort-v1` adds collection provenance checks to `rescore-codex` without
editing the frozen `codex-repo-read-v1` sources, legacy extraction, case packs,
expected decisions or decision grader. The original v1 re-score remains a
historical implementation; the CLI now applies this binding before invoking it.

All ten saved cases must agree on host ID/version/model, execution kind,
condition, adapter backend, hook loading, pack, freeze hashes and scorer hash.
Missing fields, unsupported condition/backend/hook combinations, mixed cohorts
or changed grader/pack identities are errors before re-scoring. Model/version
values are not hard-coded: a consistent future cohort can use a different model.

The binding records all 40 input hashes before dispatch and requires the preserved
v1 re-score to have used the same snapshot. Collection identity and the separately
frozen binding-source identity are written to the manifest and revised report.
The original aggregate report is retained unchanged. The additional
`evidence-anchor.json` contains only collection/measurement identities, relative
source hashes, old/new verdicts and reasons, repository-read deltas, separate
metrics, secondary-host status/runnable path and `new_model_calls=0`.

To pin a historical cohort, supply `--expected-anchor`; its collection identity
and all 40 source hashes must match before any output is written:

```bash
python scripts/run_lifecycle_activation_benchmark.py rescore-codex \
  --evidence-root /path/to/private/ten-case/live --out /path/to/new/rescore \
  --expected-anchor benchmark/lifecycle-activation-codex-2026-10-03.anchor.json
```

The checked-in anchor supplements the authored live summary. Its collection is
Codex CLI 0.160.0 / `gpt-6-luna` / `adapter_enabled` / `remote_loopback` /
`invocation_inline`. Both reports retain 3 PASS / 7 FAIL / 0 INCONCLUSIVE; only
the stale-conflict missing-read reason changes. OpenCode remains NOT_RUN.

The anchor's authored source chronology records the live run's base commit and
the first preserved measurement-source commit. The collector repair was
uncommitted during collection; that later commit is not falsely presented as a
clean-checkout native run. Hashes bind retained bytes under the trusted collector;
they do not authenticate a model/vendor or replace private raw evidence. If raw
evidence is lost, the public anchor preserves the result and expected input
digests but cannot independently reconstruct the raw trial. No body, credential,
database, private filesystem root or raw trace is included. Re-scoring is not
fresh host execution, #51 repair, merge approval or release approval.
