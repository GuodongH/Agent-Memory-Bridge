# Lifecycle Activation Benchmark v2

Status: **FROZEN** on 2026-09-28, before any v2 live host run.
v1 stays frozen. Its one Codex trace remains `plain_mcp_baseline` / `known-project-gotcha` = FAIL.
That run disabled plugins, so it is not host-adapter evidence.

v2 asks the same ten cases without naming the governed namespace in the prompt or in checkout files.
`expected_namespace` stays in the case metadata for the scorer. A live v2 run has to get that namespace from the repository binding: initialize the fixture as a Git checkout, then write `bindings.json` under the temporary bridge home with `repository_identity`.

## Conditions

- `plain_mcp_baseline`: plugins disabled, plain MCP only. The archived v1 trace is this condition.
- `adapter_enabled`: the optional lifecycle hook must load. `activation-evidence.jsonl` has to contain `adapter_loaded: true`. If it does not, the result is `INCONCLUSIVE` (`adapter_not_observed`), not a product `FAIL`.

## Evidence rules

A live trace is complete only when the host exits 0, the trace ends in `turn.completed`, and the final text is non-empty. A crash, timeout, or partial trace is `INCONCLUSIVE`.

New live observations carry the v2 freeze hashes and the SHA-256 of `tools/evidence/lifecycle_activation_grade.py`. A missing or different scorer id is `INCONCLUSIVE`. Synthetic probes stay complete by construction and do not need those ids.

## Host lanes

Codex collection:

```bash
python ./scripts/run_lifecycle_activation_benchmark.py collect-codex --pack v2 --condition adapter_enabled --case-id known-project-gotcha --out /path/to/temp-dir
```

The temp directory is caller-chosen and must sit outside the fixture. Do not point collection at a production database.

OpenCode stays `NOT_RUN` until a complete live observation is scored. Prepare a concrete command with no placeholders:

```bash
python ./scripts/run_lifecycle_activation_benchmark.py prepare-host --host opencode --case-id known-project-gotcha --out /path/to/temp-dir
```

Prompt activation for OpenCode is still pending. The plugin binds session start and compaction only. It does not bind `message.updated`.

## Non-claims

- A v1 plain-MCP `FAIL` does not become a pass when the scorer changes.
- Adapter evidence is not implied by a Codex MCP trace.
- This pack does not add a public MCP tool or write durable memory.
