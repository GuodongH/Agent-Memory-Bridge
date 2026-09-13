# NAS fork difference classification

## Scope and source references

This is a sanitized classification of the historical NAS deployment fork. It
is evidence for the v0.34 migration work, not a patch plan and not release or
cutover authority.

| Ref | Meaning |
| --- | --- |
| `cb314ebbf96d48c13c0c298f6ac7273d701682f4` | Frozen v0.33 official baseline |
| `acdafc3d6ff429d25a70bb56160fc72df39764a1` | Common ancestor of the official baseline and NAS fork |
| `c78c895ffee001827283c035f3f47ff5a7d6146b` | NAS-fork HTTP transport addition |
| `a82d20ce2049ea99ee36e855947bdb3ece8072d9` | NAS-fork bounded health-count follow-up |

The NAS fork is not an ancestor of the frozen baseline. The four files below
are the complete source change set from the common ancestor to the NAS fork.
Changes that appear only on the official baseline are not NAS-fork changes and
must not be folded into this classification by applying a whole-branch diff.

## Runtime provenance observation

An independent read-only container probe observed the running service at
version `0.32.1`. SHA-256 values of its loaded `cli.py`, `http_server.py`, and
`remote_proxy.py` exactly matched their blobs at `a82d20c`. This establishes
the running code provenance for the classified source files; it does not
establish that an operator-maintained source checkout is clean or present.

An isolated, retrievable SQLite-consistent rehearsal snapshot has now been
captured with SQLite's online backup API. Its local copy and archived
container-export copy have the same SHA-256, and independent read-only checks
observed `integrity_check=ok`, zero foreign-key violations, schema version 12,
and restrictive `0600` file permissions. This proves SQLite consistency only.

The snapshot is explicitly classified `SQLITE_CONSISTENT_AMB_CONTENT_INVALID`:
the source service's official full verification passed SQLite integrity and
foreign-key checks but failed AMB content validation solely with
`invalid_metadata_value_count=339`. It is valid only as an isolated rehearsal
input. It is not an AMB-content-valid backup, and it does not establish a
migration pass or cutover readiness. The private artifact locations and
checksums are intentionally retained outside this public evidence document.

## AMB source classification

| File and difference | Classification | v0.34 handling |
| --- | --- | --- |
| `src/agent_mem_bridge/cli.py`: explicit `serve-http` dispatch | `GENERIC_DEPLOYMENT_FEATURE` | Reimplement through the official CLI path. |
| `src/agent_mem_bridge/cli.py`: explicit `proxy` dispatch | `GENERIC_DEPLOYMENT_FEATURE` | Keep as design evidence only; the stdio-to-HTTP shim is deferred unless a target client requires it. |
| `src/agent_mem_bridge/cli.py`: no-argument switch to proxy when a remote URL is present | `OBSOLETE_WORKAROUND` | Do not port; local stdio remains the default. |
| `src/agent_mem_bridge/http_server.py`: SDK Streamable HTTP application, loopback default, and explicit host allowlist | `GENERIC_DEPLOYMENT_FEATURE` | Reimplement with the official server and security surfaces. The transport is standard MCP HTTP, not custom RPC. |
| `src/agent_mem_bridge/http_server.py`: static bearer-file verifier and custom bearer middleware | `OBSOLETE_WORKAROUND` | Preserve only as historical evidence; do not make it the official authentication protocol. |
| `src/agent_mem_bridge/http_server.py`: health endpoint returning a local database path and full table census | `MIGRATION_ONLY` | Replace with minimal liveness/readiness; counts are migration diagnostics, not derived-index parity proof. |
| `src/agent_mem_bridge/http_server.py`: accepted-but-unused public URL setting | `OBSOLETE_WORKAROUND` | Do not port a no-op setting. |
| `src/agent_mem_bridge/remote_proxy.py`: SDK stdio-to-HTTP adapter with explicit failure and no local writer fallback | `GENERIC_DEPLOYMENT_FEATURE` | Retain the no-fallback invariant; defer the adapter pending a demonstrated client need. |
| `src/agent_mem_bridge/remote_proxy.py`: host-local token-file default | `HOME_SYSTEM_CONFIGURATION` | Keep credentials outside source and host-specific configuration. |
| `tests/test_http_transport.py`: HTTP, host-allowlist, health, and proxy coverage | `GENERIC_DEPLOYMENT_FEATURE` | Recreate tests for the official implementation; do not retain tests that require the obsolete bearer scheme or count-heavy health payload. |

No NAS-fork source change is classified as `CORE_BUG_FIX`.

## Compose material, classified separately

The historical Compose file is deployment configuration, not an AMB source-file
change. Its non-root service, persistent local storage, deterministic command,
and healthcheck are `GENERIC_DEPLOYMENT_FEATURE`s. Its project/image names,
host bind, service account, filesystem layout, and secret-file location are
`HOME_SYSTEM_CONFIGURATION`. Its pinned-image and former-authority procedures
are `MIGRATION_ONLY`.

## Guardrails for the next phase

- Do not import the NAS fork wholesale.
- Do not copy a running SQLite database or its WAL files as a snapshot.
- Use the qualified SQLite-consistent snapshot only in an isolated rehearsal;
  preserve its content-validation diagnosis as the reason the rehearsal cannot
  be called migration-ready.
- Before migration readiness can be established, compare durable and derived
  state, correct or explicitly disposition the 339 invalid metadata values,
  rerun full AMB verification, and rehearse rollback on copies.
- No production stop, client switch, database replacement, publication, or
  cutover follows from this classification.
