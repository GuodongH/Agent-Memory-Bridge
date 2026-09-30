# Codex Stop capture acceptance

On 2026-09-30, Codex CLI 0.157.1 invoked one native Stop callback in each of
two controlled turns on Linux/WSL. The positive turn created one hidden
`needs_review` candidate. The negative turn created none because it had no
bounded artifact. Both turns completed with exit 0, no model tool calls, and
unchanged ordinary memory (one seeded row, with the same ID and content).

This bundle supports review of #53. It does not close the issue or establish
that the tested source has merged.

## What was observed

`report.json` contains three distinct forms of evidence:

- `native_callback_projection`: an allowlisted projection of stdin received
  inside the process launched by Codex Stop, recorded before capture wiring.
- `host_event_projection` and `receipts`: projected host lifecycle events and
  capture output. Session hashes join the host, callback, and capture receipt;
  turn hashes join the callback and receipt.
- Counts, visibility checks, and `passed`: derived verifier results from the
  fixture database and host output, not raw host assertions.

Identifiers are SHA-256 pseudonyms, consistent across those projections.
Private paths, databases, credentials, transcript contents, hidden reasoning,
and the full host event stream are not published. These local observations
are reproducible evidence, not a signed host attestation.

The exact visible fixture texts are constants in
`scripts/check_codex_stop_capture.py`: `json.dumps(ARTIFACT)` for the positive
turn (307 UTF-8 bytes), and the fixed negative reply (63 bytes). Their hashes
appear in the callback projections. The positive capture receipt carries the
same hash as the visible artifact.

## Native call path and input boundary

The collector supplies an invocation-inline `hooks.Stop` command pointing to
its own `--callback` entry point. Codex executes that command; `callback()`
projects stdin and calls `run_hook_payload(payload)` in the callback process.
`collect_lane()` never calls capture after Codex exits. Model tool calls
invalidate the trial, and a missing callback cannot pass either lane.

The production hook uses `python -m agent_mem_bridge lifecycle-hook`, which
enters the same `run_hook_payload` wiring. The instrumentation wrapper is
specific to this proof, so this result does not certify plugin discovery.

`_codex_stop_capture_event()` selects only `last_assistant_message`, requires
the entire message to be a `memory.visible_artifact.v1` JSON object of at most
4,000 UTF-8 bytes, and supplies resolver-owned scope and host provenance.
It passes that one artifact to `capture_at_boundary(..., "post_run", event)`.
It never opens `transcript_path`, forwards a conversation body, or invokes a
summarizer. Existing capture policy filters the structured artifact further.
Remote authority selection blocks this write path without local fallback.

## Source binding and reproduction

From the repository root, including after merge:

```bash
sha256sum --check docs/evidence/issue-53-codex-stop/source.sha256
python scripts/check_codex_stop_capture.py \
  --output /path/to/new-private-proof --model <available-model> \
  --reviewed-fixture-hook
```

The manifest matches `report.json` and binds the adapter, unchanged capture
core, and collector/verifier. A changed byte makes the hash check fail; rerun
the live proof if any of these files changes during review or merge. The live
command needs Codex authentication and may need invocation-only provider
settings via `-c`. Do not put credentials in arguments.

This run used `gpt-6-luna` through the operator's existing Responses-compatible
provider, with low reasoning effort. Provider configuration and credentials
are excluded. User config/rules and plugins were disabled, and only this
reviewed fixture hook received invocation-scoped trust. No global hook or
trust configuration was installed.

## Limits and retained failures

Two earlier project-file hook attempts completed without a Stop callback and
did not pass acceptance. Their private raw outputs are not included here;
this bundle certifies only the later invocation-inline run. Windows host
execution, plugin/project-file discovery, remote capture, OpenCode,
PreCompact capture, and autonomous artifact discovery were not tested.
Read-side activation acceptance belongs to a separate issue. No independent
specialist review is claimed. Review, green remote CI, merge, and a post-merge
source-hash check remain closure gates for #53.
