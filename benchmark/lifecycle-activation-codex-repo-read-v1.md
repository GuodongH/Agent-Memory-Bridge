# Codex repository-read measurement revision v1

Revision ID: `codex-repo-read-v1`. This is an opt-in evidence-extraction revision,
not a change to the frozen v2 cases, expected decisions, or decision grader.
The default remains `legacy-v2`; original observations and reports remain valid
historical evidence under that extractor.

## Defect and boundary

The original stale-conflict native trace ran
`/bin/bash -lc 'pwd; rg -n -i "fixture|port token|listener" .'` and returned
`./NOTES.md:1:current port token: fixture-current-port-2222`.
Legacy extraction sees only paths named in commands, so it missed this repository
read and reported `repo_evidence_missing`. The same case independently missed
required recall. Fixing the read measurement cannot remove that product failure.

This revision additionally recognizes successful, completed Codex
`command_execution` events from bounded `rg` content searches. It accepts direct
`rg`, `/usr/bin/rg`, or `/bin/rg`, optionally wrapped by a standard bash/sh
`-c`/`-lc` command with only preceding `pwd` statements. Searches require explicit
line numbers, a pattern and a target. Supported switches are `-n`, `-i`, `-S`,
`-H`, their long forms, `--no-heading` and `--color=never`.

Only filename:positive-line-number:content output for allowlisted fixture paths
counts. Leading `./` is normalized; absolute and traversal paths are rejected.
Prose, started/failed commands, nonzero exits, list/count/JSON/replacement modes,
stdin, pipelines, redirection, substitution and other command chains do not
produce these additional receipts. This is deliberately not a general shell
interpreter. Existing explicit-path extraction remains unchanged; this revision
does not certify every possible repository-read command.

Each added receipt binds the trace event line, output line, item ID, normalized
path and command SHA-256, without copying content. A four-source SHA-256 manifest
freezes this contract, the new extractor, parser and unchanged decision grader.

## Re-score contract

`rescore-codex` requires all ten frozen v2 case directories with `codex.jsonl`,
`observation.json`, `preflight.json` and `report.json`. It validates case/host/pack,
grader/freeze bindings, clear isolation preflights, exact legacy re-extraction of
saved raw fields and exact reproduction of each saved report. It hashes all 40
source files and checks them again before writing to a new output directory.
Missing or changed evidence is an error, not a passing case. Existing output
directories are refused.

Outputs retain `old-report.json`, revised `report.json`, revised observations and
a manifest containing every source hash, old/new verdict/reason/read-path pairs,
measurement source identity and `new_model_calls=0`. Only read evidence and
measurement metadata change; invocation, hook, result, namespace, token usage,
latency and final response stay bound to their original observation.

```bash
python scripts/run_lifecycle_activation_benchmark.py rescore-codex \
  --evidence-root /path/to/original/ten-case/live \
  --out /path/to/new/rescore
```

Fresh Codex runs can explicitly select `--measurement-revision codex-repo-read-v1`.
Re-scoring old traces does not establish behavior of repaired activation policy.
v0.35.0 remains on owner-directed release hold pending #51 repair and its paired
required-recall / negative-control evidence.
