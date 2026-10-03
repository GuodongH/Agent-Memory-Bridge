# Remote-authority lifecycle condition

Issue #58 adds a remote-authority condition alongside the frozen lifecycle
activation v1/v2 packs. The historical packs, checksums, and results remain
unchanged. This condition checks authority selection and observable transport
activity; a final answer alone cannot pass it.

Run the direct hook collector from a development environment:

```bash
python scripts/check_lifecycle_remote_authority.py --report-path /tmp/remote-contract.json
```

The report path must not already exist. Fixture databases are temporary and
removed when the collector exits. The JSON manifest and this contract are
frozen by `lifecycle-activation-remote-v1.sha256`; the report also binds the
original v1/v2 inputs and the current source hashes. A changed source version
requires fresh collection before its report can pass the scorer.

## Hook contract

The collector starts a temporary loopback AMB Streamable HTTP authority and
binds a temporary checkout through the governed project resolver. It invokes
the existing lifecycle entrypoint. It must never use the operator's configured
authority or database.

Required cases:

| Case | Required evidence |
| --- | --- |
| Material task with a useful hit | Exactly one remote recall in the bound namespace, bounded to three records; useful untrusted context returned. |
| Trivial deterministic task | No remote recall and no HTTP client creation. |
| Successful empty recall | One remote recall; `no_hit` and available authority. |
| Unavailable remote | Structured error, distinct from `no_hit`; host continues. |
| Conflicting local database | Local trap exists but no local SQLite open is attempted and no trap content is injected. |
| Exact repeat | Successful first recall, then repeat suppression with no additional remote call. |

The report must include lifecycle observations, independently observed remote
recall calls, local database-open attempts, and hashes of the collector, runtime
source, and frozen inputs. Missing evidence fails the contract. The collector
creates synthetic fixture records only; it adds no production writeback path.

## Native host acceptance

A hook-contract pass does not close issue #58. Native Codex acceptance remains
`NOT_RUN` until a separate adapter-enabled host trace proves:

- the native prompt hook ran for the same host session;
- the configured remote authority received the bounded recall;
- the fixture authority was inaccessible through host filesystem tools;
- no local fallback was opened;
- the host finished with exit code zero, `turn.completed`, and nonempty final text;
- negative controls made no remote calls.

An isolation-preflight failure is not a host execution result. Fix the collector
boundary before collecting again; do not remove the isolation gate or relabel
direct hook calls as native activation. Remote fixtures must stay outside the
host mounts. The host needs the governed binding and derived evidence directory,
not the authority database.
