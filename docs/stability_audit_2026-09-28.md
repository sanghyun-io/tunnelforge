# TunnelForge stability audit — 2026-09-28

## Scope and acceptance criteria

The user confirmed MySQL/PostgreSQL and Windows/macOS/Linux as the immediate
support scope. Preserve Rust Core ownership of database operations and the
unsigned direct-download macOS policy. No release or remote repository changes
are part of this local audit.

Acceptance criteria: deterministic regression tests for reproduced defects;
real disposable database roundtrips for data fidelity; full Python and Rust
tests; optimized Core builds; frozen application UI/Core smoke checks where
the operating system is available. A passing unit suite is not proof of full
commercial readiness or absence of bugs.

## Changes

| Issue | Defect and correction |
| --- | --- |
| TF-STATUS-100 | SSH listener exposed all interfaces, leaked failed startup resources, and reported configured port zero instead of the assigned port. Bind loopback, clean up failures/dead sessions, return actual endpoints, and support IPv6 direct probes. |
| TF-STATUS-101 | SQL parameter replacement rewrote literals/comments/inserted data. PostgreSQL query wrapping rejected ordinary commands. Use a single scan with engine session rules and execute original statements; preserve result metadata and disambiguate duplicate column names. |
| TF-STATUS-102 | Import could delete targets before rejecting invalid schemas or payloads; checksum maps could name wrong chunks. Validate selected metadata, hashes, decompression, row shapes and row counts before connecting. Restrict post-load DDL to imported tables and restore temporary settings on preparation errors. |
| TF-STATUS-103 | SQL splitting treated PostgreSQL operators/strings as MySQL syntax. Cursor reads repeated old results, and failed database selection changed local state. Propagate dialect, consume cursor rows, clear failed results, and publish selection only after success. |
| TF-STATUS-104 | Empty strings in single-column TSV tables disappeared. Preserve empty physical rows in both buffered and streaming import paths. |
| TF-STATUS-105 | PostgreSQL chunks and lock-free MySQL workers could capture different data versions. PostgreSQL now uses one read-only repeatable-read transaction with table locks. Lock-free MySQL uses one worker and reports it explicitly. Propagate count errors. |
| TF-STATUS-106 | Foreign-key actions were lost and PostgreSQL composite key inspection could pair the wrong columns. Preserve validated delete/update actions and pair PostgreSQL columns by ordinal. |
| TF-STATUS-107 | Windows redirected release CLI text used the locale encoding. Emit UTF-8 for redirected CLI streams; preserve interactive behavior. |
| TF-STATUS-108 | Merge retry could truncate existing data and ambiguous chunk replay could duplicate committed rows. Allow table restart only for replacement/recreation and remove blind chunk replay and unsafe fallback after partial load. |
| TF-STATUS-109 | Linux had no Python/application gate; Windows release only checked that the main EXE existed. Add a required Linux test/build/smoke job and bounded frozen main-app checks on Windows PRs/releases. |

## Verification

Final source checks in this session:

| Check | Result |
| --- | --- |
| Windows Python 3.12, full `pytest -q --tb=short` | 2800 passed, 1 skipped, 6 warnings (104.29 s) |
| Linux Debian Docker, Python 3.11, full `pytest -q --tb=short` | 2767 passed, 34 skipped, 4 warnings (50.84 s) |
| Windows and Linux `cargo test --manifest-path migration_core/Cargo.toml` | Both passed; 244 library tests; platform-independent CLI/stress checks passed |
| Linux configured live database integration | 11 `live_roundtrip` tests and 1 FK integration test passed; includes eight format/compression roundtrips and both migration directions |
| Configured SQL tests | Both actual engines exercised; parameter SQL modes, duplicate aliases, PostgreSQL utility/RETURNING/CTE, arrays and catalog vectors passed |
| Three explicitly ignored library tests, run with disposable databases | Both PostgreSQL concurrency/permission tests and MySQL killed-connection global-setting restoration passed |
| Real SSH protocol smoke on loopback | Windows and Linux both passed encrypted binary/Unicode forwarding and tunnel shutdown |
| Optimized Rust Core builds | Windows MSVC and Linux both passed |
| Final-source frozen applications | Windows and Linux rebuilt; both passed bounded main-window/icon/bundled-Core smoke checks |

Platform-specific skips are retained. The remaining warnings are existing Qt
test-class collection warnings and, on Windows, Paramiko dependency deprecations.
The ten-million-row stress test was not run. Linux setup initially needed Qt/Tk
runtime packages and a container-local Git metadata copy because the host uses
a Windows Git worktree; those setup failures are not counted as passing checks.
Full suites run sequentially to avoid sharing test-generated build fixtures.

Verification is recorded in `docs/current_status.md`. Database tests run against
new disposable MySQL 8.4 and PostgreSQL 18.4 containers, never existing user
databases. The matrix covers both engines, JSONL/TSV, none/Zstandard, small
chunks, selected-table restore, Unicode, empty strings, NULL, binary bytes,
exact decimal text and fractional timestamps. Foreign-key tests exercise real
DELETE CASCADE and UPDATE SET NULL after restoration. PostgreSQL snapshot tests
write between chunks and verify a single data version with one and eight
requested workers.

Tests that return early without `TF_MYSQL_*`/`TF_POSTGRES_*` configuration are
not counted as live DB evidence. Explicitly ignored PostgreSQL snapshot tests
must be run separately with their database URL. The stress test requiring ten
million rows is not part of this audit.

## Remaining limits and follow-up

These are concrete incomplete areas; this change must not be described as a
perfect or fully certified commercial database manager.

- **TF-STATUS-110, server authentication and transport:** PostgreSQL uses
  `NoTls`, endpoint configuration exposes no TLS policy, SSH forwarder creation
  supplies no expected host key, and the SSH reachability client uses
  `AutoAddPolicy`. A complete TLS/certificate and persisted SSH trust flow is
  still required for secure direct/public-network deployment. Password-protected
  SSH keys are explicitly unsupported today.
- **TF-STATUS-111, full schema fidelity:** triggers, routines, permissions,
  generated/check/partition/expression-index details and other engine-specific
  objects are not all represented by `NormalizedSchema`. Full backup fidelity
  requires native object preservation or explicit preflight refusal for each
  unsupported construct. Legacy manifests cannot recover FK actions that were
  never recorded. Views are collected separately from the table snapshot;
  manifest warnings must disclose that distinction.
- **TF-STATUS-112, execution limits:** PostgreSQL anonymous records and temporal
  values now use native server display text; typed primitives, arrays and named
  composites are retained. The JSONL query protocol exposes one result schema,
  so multi-result procedures need a richer result model. SQL script splitting
  assumes default lexical session settings. Query cancellation/timeouts and
  bounded handling of very large interactive results need separate fault/load
  tests. A destructive replace import is not an atomic database restore;
  server-side DDL/type errors or later failures can leave a partial target even
  after file preflight succeeds. Input files must remain unchanged during import.
- **TF-STATUS-008, operating systems:** this Windows host can execute Windows
  and Linux Docker checks, not a real Mac. New remote CI jobs and macOS hardware
  validation have not been run in this session. Linux release installers and
  desktop-distribution compatibility remain separate from a local frozen build.
- **TF-STATUS-096/098:** disposable tests strengthen evidence, but do not replace
  the original provider-specific restricted-account run or original 185-table
  restore. No claim is made about those external environments.

Recommended next order: run the added hosted release gates, implement transport/
host identity controls, complete the schema-fidelity support matrix and reject
unsupported restores, add cancellation/load/fault-injection coverage, then
validate release candidates on actual target operating systems and providers.
