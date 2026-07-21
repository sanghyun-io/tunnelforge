# MySQL-Standard Dump/Import Design

## Goal

Make TunnelForge's MySQL dump and load behavior match the object scope and
consistency expectations of `mysqldump` and MySQL Shell while preserving the
Rust Core architecture, eight-worker throughput, and production safety.

This change resolves TF-STATUS-004 and TF-STATUS-084 and ships as patch release
`2.4.1` after review and release verification.

## Scope

This release covers:

1. MySQL full and partial exports use one shared consistent InnoDB snapshot
   across all database-reading workers.
2. Eight workers remain the default; the export does not hold table read locks
   for the duration of large-table scans.
3. Replace/recreate imports drop and recreate only tables present in the dump.
   Target-only tables and their rows remain untouched.
4. Target-only foreign keys into imported tables no longer cause a blanket
   preflight abort. Only proven structural incompatibilities block before
   mutation.
5. A global preflight failure is reported once and is not copied onto every
   table as an apparent independent failure.
6. Export and Import UI copy states these contracts and exposes snapshot setup,
   lock timing, and actionable privilege/timeout failures.
7. Version sources are synchronized to `2.4.1`; the reviewed branch is merged,
   tagged, built, and published through the existing release workflow.

PostgreSQL dump/import behavior is not redesigned in this release. Its current
paths must remain regression-clean.

## MySQL Reference Semantics

TunnelForge adopts these MySQL-compatible meanings:

- A dump contains DDL and data only for selected objects.
- Replace/recreate affects only objects present in that dump.
- Objects absent from the dump are preserved.
- Existing-object behavior is explicit: merge uses existing tables;
  replace/recreate rebuild dumped tables.
- Foreign-key checks may be disabled during ordered restore, but structural
  requirements for surviving foreign keys are checked before destructive DDL.
- A consistent parallel dump coordinates worker transactions before releasing
  the short write barrier.

TunnelForge may fail earlier than raw `mysql < dump.sql` when it can prove that
the requested operation cannot complete safely, but it must not reject a load
solely because a target-only object exists.

## Export Architecture

### Shared snapshot coordinator

MySQL parallel export uses a coordinator connection plus a fixed set of worker
connections. Connections are opened before any global lock is requested.

The coordinator performs the following sequence:

1. Set a two-second acquisition bound for metadata/backup lock setup.
2. Acquire `FLUSH TABLES WITH READ LOCK`.
3. Acquire `LOCK INSTANCE FOR BACKUP` while the global read lock is held.
4. On every worker connection, set `REPEATABLE READ`, read-only mode, and start
   `START TRANSACTION WITH CONSISTENT SNAPSHOT`.
5. Release the global read lock immediately after every worker snapshot is
   established.
6. Keep only the instance backup lock until all worker reads finish.
7. Commit or roll back worker transactions and release the backup lock on every
   success, error, cancellation, panic, and connection-loss path.

The global read lock is never held while table data is streamed. Normal DML can
continue after snapshot setup. The backup lock intentionally blocks DDL and
other file-changing operations for the export duration.

If the server version or account cannot support this protocol, strict export
fails before any table data file is written. TunnelForge does not silently
downgrade to independent worker snapshots. The error names the missing
privilege or unsupported server operation and recommends retrying with the
proper backup account.

### Fixed worker sessions

Current workers open a new connection per range or table. The new scheduler
starts exactly the bounded worker count, gives each worker one snapshot-bound
connection, and lets each worker consume multiple queue items until completion.
No work item may create a fresh MySQL read connection.

Planning reads that define the dump boundary—row counts, primary-key min/max,
and range planning—run on a snapshot-bound connection. Schema inspection and
view collection run under the backup-lock lifecycle so DDL cannot change the
object definitions between inspection and manifest finalization.

Single-thread MySQL export uses the same transaction protocol with one worker.
It no longer claims consistency merely because one connection was used.

### Manifest and progress contract

Successful MySQL dumps record:

- `snapshot_policy = "mysql_shared_consistent_snapshot"`;
- `strict_export = true`;
- no non-consistent snapshot warning;
- worker count and snapshot setup duration in result/progress metadata.

Snapshot setup emits distinct phases for connection preparation, lock
acquisition, snapshot establishment, and lock release. The UI remains
responsive and keeps the existing thread-count control with eight as default.

## Import Architecture

### Dump-scoped replacement

For `replace` and `recreate`, Rust Core builds the import set from the selected
dump manifest tables. It drops those tables child-first and creates/loads only
those tables. It never adds a target-only table to the import set and never
drops that table merely because it references an imported parent.

`merge` keeps its existing non-recreate behavior.

### Compatibility-aware surviving FK preflight

The existing table-name-only `surviving_fk_offenders` check is replaced by a
structural compatibility check. For every target-only FK that references an
imported table, Rust Core inspects:

- ordered child and referenced column names;
- MySQL data type, unsigned/signed attributes, and fixed-precision size;
- character set and collation for nonbinary character columns;
- the required referenced-key index and column order;
- storage-engine compatibility;
- the intended imported parent definition from the manifest.

If those definitions are compatible, Import proceeds and MySQL preserves the
target-only table and its FK metadata through the parent rebuild. If a mismatch
is proven, Import stops before any DROP and reports one actionable global
preflight error naming the child table, constraint, columns, and mismatch.

This patch does not add an expensive dump-wide row-value preflight. As with
standard MySQL logical restore under disabled foreign-key checks, re-enabling
checks does not retroactively scan existing target-only child rows. The Import
completion report therefore records surviving external FKs and explicitly
states that structural compatibility—not historical row revalidation—was
performed. Row-integrity validation can be added as an explicit verification
mode later without redefining replace semantics.

### Global failure reporting

Errors raised before table processing remain operation-level failures. Python
does not synthesize the same error for every manifest table. The UI and saved
log show zero completed tables, one preflight failure, and the affected FK list.
Table-level failures remain reserved for failures that occur while processing a
specific table.

## UX Contract

Normal users keep the existing workflow and eight-thread default. New visible
states are:

- `일관된 스냅샷 준비 중...`
- `8개 워커 스냅샷 준비 완료 (쓰기 중단 N ms)`
- `DDL 보호 잠금 유지 중 — 일반 읽기/쓰기는 계속 가능합니다.`
- completion metadata stating that a shared snapshot was guaranteed.

The initial global write barrier has a two-second acquisition bound. Failure
shows that no data files were produced and recommends a lower-traffic retry.
Privilege failure lists `RELOAD` or `FLUSH_TABLES` and `BACKUP_ADMIN` as
applicable to the server version.

Import copy states `덤프에 포함된 테이블만 교체하며, 대상에만 있는 테이블은 유지됩니다.`
The confirmation dialog retains the existing production guard.

## Cleanup and Failure Safety

- Snapshot setup failure releases any acquired global and backup locks before
  returning.
- Worker startup is all-or-nothing; partial snapshot pools are rolled back.
- Worker failure cancels further scheduling, joins every worker, rolls back all
  snapshot transactions, and releases coordinator locks.
- User cancellation follows the same cleanup path.
- Output directories retain TunnelForge's marker-based deletion boundary. A
  failed strict snapshot setup does not leave table data files.
- Import structural preflight completes before the first destructive DDL.
- Import failure after destructive DDL retains the existing report/resume
  behavior; no new claim of transactional DDL rollback is introduced.

## Testing

### Rust unit and protocol tests

- Snapshot setup SQL order and two-second lock bound.
- All worker connections begin a consistent read-only transaction before the
  global lock is released.
- Worker jobs reuse fixed connections and never connect per chunk.
- Cleanup releases locks and transactions for setup failure, worker failure,
  cancellation, and success.
- Single-thread and eight-thread manifests both report strict shared snapshots.
- Replace/recreate affects dump tables only.
- Compatible target-only FKs pass; type, signedness, charset/collation, index,
  and engine mismatches fail before DROP.
- Operation-level preflight errors are distinct from table failures.

### Python/UI tests

- Export payload, progress phases, strict-snapshot completion copy, privilege
  errors, and timeout errors.
- Import confirmation explains dump-scoped replacement.
- A global Rust preflight error produces one operation failure and no fabricated
  241-table failure list.
- Existing PostgreSQL and partial-export behavior remains unchanged.

### Live MySQL verification

A disposable MySQL 8 instance verifies:

1. eight snapshot workers observe one consistent point while concurrent DML
   continues after setup;
2. no long-lived table read lock exists during a large-table scan;
3. DDL waits while the backup lock is held;
4. target-only tables survive replace Import;
5. compatible external FKs survive and incompatible external FKs fail before
   mutation.

Final gates are full Rust tests, full Python tests with a sufficiently long
timeout, release build, formatting, compile checks, version synchronization,
installer/package checks, and `git diff --check`.

## Review, Version, and Release

After implementation:

1. Run focused correctness and safety review against this design.
2. Run repository code review and address actionable findings.
3. Update `docs/current_status.md`: close TF-STATUS-004 and TF-STATUS-084 only
   with fresh live/focused/full verification evidence.
4. Bump `src/version.py`, `pyproject.toml`, and
   `installer/TunnelForge.iss` from `2.4.0` to `2.4.1` using the repository
   versioning workflow.
5. Push the reviewed branch, open a pull request, wait for required checks, and
   merge through the protected branch workflow.
6. Tag the merged release commit `v2.4.1` and run the existing Build and Release
   workflow.
7. Verify the published release assets and checksums before reporting the
   release complete.

## Deferred Work

- PostgreSQL exported-snapshot sharing across parallel workers.
- Historical row-level revalidation for target-only children after replace.
- Unsafe non-consistent Export override in the UI.
- Long-lived table locks as a compatibility fallback.
