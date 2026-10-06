# Codex remote-authority acceptance

This is historical v0.35 evidence. `scripts/check_codex_remote_authority.py`
is preserved on tag `v0.35.0` and is not part of current Core.

On 2026-10-03, Codex CLI 0.160.0 completed five isolated native
UserPromptSubmit trials with `gpt-6-luna`: hit, deterministic skip, successful
no-hit, unavailable authority, and conflicting local database. All five passed.
This bundle supports review of #58; it does not establish review, remote CI,
merge, or issue closure.

## Evidence and limits

`report.json` preserves session-linked callback and activation observations,
host event projections, server-observed recall calls, local SQLite-open traps,
isolation preflight results, and before/after durable-row digests. Every trial
completed with exit 0, one completed turn, and nonempty final text. The negative
trial completed its callback with zero HTTP clients and zero remote recalls.

Session identifiers were hashed consistently by the collector. For publication,
only temporary filesystem paths in stderr were replaced with placeholders.
Synthetic repository and memory identifiers remain fixture data. Counts,
source/frozen hashes, host warnings, responses, and verdicts are unchanged.
No private database, credential, operator configuration, hidden reasoning, or
full host transcript is included. This is inspectable local evidence, not a
signed host attestation.

The collector uses invocation-inline hooks and calls the existing
`run_hook_payload` inside the native callback. It does not certify plugin or
project-file discovery, Windows native execution, or OpenCode activation.
The separate direct-hook collector includes the existing repeat-suppression
case; the native acceptance condition has exactly the five required cases.

Codex reports its explicit `--dangerously-bypass-hook-trust` warning as an
`error` item. The report retains that warning. The scorer permits only its exact
known message, fails other error-item messages or missing diagnostic evidence,
and counts unknown non-message item types as tool events. The opt-in applies
only to the reviewed fixture hook; no global hook/trust configuration changed.

Earlier local runs failed because the collector counted that warning as a tool
event. Those failures are not native PASS evidence. This report was collected
after the scorer correction and passes serialized-report re-scoring.

## Source binding and review

From the repository root, including canonical main after merge:

```bash
sha256sum --check docs/evidence/issue-58-codex-remote/source.sha256
python scripts/check_codex_remote_authority.py \
  --score docs/evidence/issue-58-codex-remote/report.json
```

The report binds the collector, scorer, adapter, policy, project resolver, and
isolation helpers through source hashes. It also binds the frozen v1/v2 and
remote-v1 manifests and contracts. Any bound source change requires fresh
native collection; do not edit stored hashes to make old evidence pass.

For a fresh collection on a provisioned Linux host with Codex, the AMB client
environment, bubblewrap, and an authenticated Responses-compatible provider:

```bash
python scripts/check_codex_remote_authority.py \
  --output /path/to/new-private-proof --model <available-model> \
  --reviewed-fixture-hook
```

Provider configuration and credentials are intentionally excluded. A partial
`--case` run is diagnostic only and cannot pass the complete report.

Local validation before publication: 1,260 tests passed with one tarfile
deprecation warning; Ruff, formatting, the Linux CI mypy target, benchmark,
deterministic proof, release-contract, public-surface, and onboarding checks
passed. The public surface remains 17 tools. Reviewer approval, green remote CI,
merge, and source-hash plus saved-report re-scoring on canonical main are still
required before closure.
