# Export/Import policy and compatibility

This contract applies to the proposed 2.6.0 release. Database operations belong
to the bundled Rust Core. Use the matching application/Core build.

## File compatibility

| File | Import support |
| --- | --- |
| Existing manifest v1/v2 | Supported, with legacy metadata limitations below |
| New manifest v3 | Requires TunnelForge 2.6.0 or later; both JSONL and TSV use v3 |
| Future/unknown version | Rejected before target modification |

V3 protects namespace, timezone and column attribute semantics. Older importers
reject its version instead of silently ignoring these fields. JSONL/TSV and
none/Zstandard compression are explicit independent manifest properties.

Checksums, filenames, schema structure, decoded row shape and declared row counts
are checked before connecting to the target. Do not edit dump files while an
import is running. These checks cannot prove that an old source snapshot was
consistent or predict every target-server DDL/data restriction.

## Destination and modes

MySQL database and PostgreSQL database/schema are different concepts. PostgreSQL
Import stays in the selected database and chooses a schema inside it. Cross-engine
Import requires an explicit destination. Legacy PostgreSQL dumps without
`source_schema` also require an explicit destination; `public` is not guessed.

| Mode | Effect and constraints |
| --- | --- |
| `safe` (default) | Restores the complete same-engine dump into a new owned namespace, verifies rows, values and schema, then offers reviewed replacement when supported. Existing namespaces remain unchanged during preparation. |
| `replace` | Drops and recreates only the selected dump tables, then loads data and finalizes indexes/FKs/views. Existing selected-table data is lost. |
| `recreate` | Legacy alias of `replace`; does not delete the entire database or unrelated tables. |
| `merge` | Appends dump rows to compatible existing tables (creates missing tables). It is not synchronization, deduplication, upsert, or change-data capture. Existing keys/FKs can reject rows. Existing MySQL targets must use InnoDB. |

Target-only tables are preserved. A target-only FK referencing an imported parent
requires the recreated parent to retain a compatible key/type contract. MySQL
secondary UNIQUE keys are created with the table so surviving children do not
prevent its recreation. Unsupported target actions, including MySQL `SET DEFAULT`
foreign keys, fail preflight.

### Safe restoration and destination names

If the requested destination does not exist, use the source name or the user's
explicit destination and create it exclusively. If it exists, restore into a
separate candidate first. Compare the existing objects and offer retention of
the original, use of the candidate, or explicitly confirmed replacement. A new
name may be entered before preparation. An existing namespace is never silently
reused as a staging area. PostgreSQL stays in the selected database.

Verification includes streamed SHA-256 content digests, not just row counts.
Preparation failures retain the candidate for inspection and preserve the
existing namespace. Unsupported replacement plans leave the verified candidate
available without changing the original. Partial table filters and cross-engine
safe restoration are currently refused; use the explicit migration workflow for
cross-engine changes.

Supported MySQL replacement uses write locks and one atomic multi-object RENAME;
PostgreSQL replacement uses locks and a transaction. Both recheck dependencies and
the candidate before switching and retain displaced tables in an owned backup
namespace. Unsupported object graphs, privileges and grants block replacement.
MySQL requires proven complete metadata visibility (direct global SELECT,
SHOW VIEW and PROCESS privileges); database-scoped permissions can hide incoming
dependencies. Ambiguous role/partial-revoke grants are refused. Original routines
or events also block MySQL cutover when their preservation cannot be certified.
This restriction affects cutover, not preparation or use of a verified new target.
MySQL saved old-view aliases preserve definitions but reference the active table
names: they are not views of backup data. The journal records their definitions.
Backup namespaces do not represent an independently complete full-database backup.

An interrupted cutover can have an unknown outcome. Its journal must be reconciled
before retry; a client-side error is not evidence that replacement did not occur.
After a proven rollback, reviewing a fresh plan can reuse the verified candidate;
the old plan cannot be replayed and its journal remains intact. Candidate table,
constraint and view definitions are bound into the verification proof so later
changes invalidate it, even when all row counts still match.
See the [design and verification contract](superpowers/specs/2026-09-28-safe-restore-design.md).

## Data and transaction guarantees

- New MySQL import tables use InnoDB. Each INSERT/LOAD statement is transactional;
  coercion/truncation/overflow/invalid-enum and unexpected warnings cause rollback
  of that statement. Duplicate rows are not silently discarded.
- A narrow legacy exception preserves MySQL ENUM storage index zero: only exact
  empty source cells with identical source/target enum labels and their expected
  warnings are accepted. Genuine empty ENUM labels are not confused with index zero.
  Other warnings in the same statement still cause rollback. Diagnostic-capacity
  overflow uses bounded INSERT batches rather than accepting unobserved warnings.
- Advanced `replace`/`recreate`/`merge` are **not one atomic transaction**. MySQL DDL commits,
  earlier successful chunks and already running parallel workers may remain after
  a later failure. No new chunks are scheduled once a worker failure is known.
- Failed/unattempted/data-loaded tables are reported separately. Data-loaded does
  not mean all deferred indexes/FKs or final verification completed.
- UI subset retry is disabled: retrying only unfinished tables could leave earlier
  tables without deferred constraints. Inspect the partial target and rerun the
  full original selected scope from the dump with explicit confirmation. Never
  blindly repeat `merge` against a partially loaded target.
- Views have explicit imported/failed/skipped results. Table data may already be
  committed if view recreation fails; the application must not report complete
  restoration in that case.

## Snapshot, time and parallelism

- Strict parallel MySQL export uses a shared consistent InnoDB snapshot and
  requires the corresponding lock privileges. The privilege fallback is an
  explicitly selected single-connection snapshot. The old no-backup-lock option
  now uses one worker, not independent inconsistent snapshots.
- PostgreSQL table export uses one read-only repeatable-read transaction and
  locks exported tables against destructive DDL. Nullable UNIQUE columns are not
  used as keyset cursors. Large keyless MySQL tables use a single streaming read.
- PostgreSQL export and content verification pin `DateStyle` to `ISO, YMD` so
  locale-specific day/month formatting cannot hide a changed date.
- New exports set source sessions to UTC and record `source_timezone="UTC"`.
  Import precedence is explicit timezone override, recorded source timezone,
  then legacy server default. Explicit "server default" disables manifest override.
  The setting reaches initial, reconnected and parallel sessions.
- A legacy dump without timezone metadata cannot identify its original timezone.
  Choose a known source timezone explicitly when preserving timestamp instants;
  do not infer KST from the application's display locale. Wall-time values and
  timezone-aware instants are distinct and tested separately.
- Legacy independent-snapshot policy labels do not prove a consistent source
  backup even if an old manifest says `strict_export=true`. Warnings remain in
  Import results and reports. Worker pools use one physical connection per worker.

## Schema support and explicit limits

Verified cases include primary/secondary/composite UNIQUE keys and MySQL prefix
lengths, compatible FK actions, NULL/empty/Unicode/control text, binary values,
decimal precision, fractional timestamps, MySQL ENUM/defaults/ON UPDATE,
same-engine MySQL CHECK enforcement, expression defaults, invisible indexes,
AUTO_INCREMENT allocation gaps, table/column comments, and
PostgreSQL arrays, supported scalar defaults, UUID generation and ordinary
serial/BY DEFAULT identity columns. MySQL 8.0.46/8.4.11 and PostgreSQL 13.23/18.4
were exercised locally; this is not a claim covering every engine version.

The normalized format does not yet preserve every DB object. Selected tables
with detected unrepresentable generated/custom-type/advanced-index/default/
cross-schema-FK semantics are refused before export rather than producing a file
that appears complete. PostgreSQL ALWAYS identities and custom/shared sequence
semantics are also refused. Ordinary identity restoration uses `MAX(id)+1`, not
the source's unused/deleted allocation gaps.

Triggers/routines and excluded nontransactional tables are not a full backup.
Known omissions and separately captured views produce persistent warnings and
`strict_export=false`; retain and review them. A successful table transfer does
not certify complete database backup fidelity. No new signing/notarization
requirement or external database dump tool is introduced.

See [verification evidence](export_import_verification_2026-09-28.md) for fixtures
and results. Keep a recoverable target backup before destructive replacement.
