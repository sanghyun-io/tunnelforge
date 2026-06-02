# MySQL Shell Style Strict Parallel Export Design

## Goal

Make TunnelForge's MySQL dump export behave like the MySQL Shell consistency model where it matters:
parallel export may be marked `strict_restorable` only when all worker reads are anchored to a proven common snapshot point.

## Current Problem

The Rust Core currently opens worker connections inside each worker as work starts. With `threads > 1`, those connections cannot share one `START TRANSACTION WITH CONSISTENT SNAPSHOT` transaction, so the manifest correctly falls back to `limited_restorable`.

This is not a UI-only problem. Enabling Import for limited dumps helps usability, but it does not make the exported artifact strict. The strict path needs a source-side synchronization step before worker SELECT statements begin.

## Target Behavior

For MySQL exports with `threads > 1`, Rust Core should attempt a MySQL Shell style strict path:

1. Inspect selected table engines.
2. Acquire a short global read lock using `FLUSH TABLES WITH READ LOCK`.
3. Open the worker MySQL connections before export work starts.
4. On each worker connection, run:
   - `SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ`
   - `START TRANSACTION WITH CONSISTENT SNAPSHOT`
5. Confirm every worker has started its snapshot transaction.
6. Attempt `LOCK INSTANCE FOR BACKUP` when available.
7. Release the initial global read lock.
8. Run parallel dump work using only the pre-snapshotted worker connections.
9. Commit worker transactions and release backup lock after export completes.

The manifest may be `strict_restorable` only if:

- all dumped tables are covered by the chosen consistency strategy,
- every worker read uses a snapshot transaction that was started under the initial lock,
- checksums are complete,
- no manifest blockers or warnings remain.

## Failure Behavior

If strict parallel setup fails, Rust Core must not silently label the dump strict.

Expected outcomes:

- `FLUSH TABLES WITH READ LOCK` denied or unavailable: fail strict parallel setup with a clear message.
- worker snapshot start fails: release any acquired lock, close opened connections, and fail the export.
- `LOCK INSTANCE FOR BACKUP` denied: continue only if the initial snapshot synchronization already succeeded and all dumped tables are InnoDB; record no strict warning for pure InnoDB table data. For non-InnoDB tables, this remains not strict.
- mixed/non-transactional table engines without a lock-based strategy: not strict.

## UI and Import Policy

Import must reflect manifest grade:

- `strict_restorable`: recommended Import is enabled and uses `strict_manifest=true`.
- `limited_restorable`: Import is enabled only after an explicit Korean confirmation explaining that the dump can be loaded but is not a fully proven point-in-time backup; it uses `strict_manifest=false`.
- `not_restorable`: Import is blocked with blockers shown first.

Export UI copy should explain in simple Korean that strict parallel export briefly waits for writes so worker snapshots can be aligned, then continues without holding the heavy initial read lock.

## Testing Requirements

Rust tests must cover:

- snapshot strategy classification for parallel InnoDB with lock synchronization,
- manifest grade strict when snapshot warnings are absent,
- cleanup SQL order for lock/snapshot setup helpers,
- worker dumping through provided snapshot connections where practical without a live database.

Python tests must cover:

- limited dump enables a warning/confirmation Import path,
- limited Import passes `strict_manifest=false`,
- strict Import keeps `strict_manifest=true`,
- not-restorable dumps remain blocked,
- Korean UI strings are translated and do not hardcode unregistered text.

## Out of Scope

- Replacing Rust Core with `mysqlsh`.
- Supporting PostgreSQL exported snapshots.
- Guaranteeing strict consistency for non-InnoDB tables without a lock-based read strategy.
- Long-running production lock hold. The initial read lock must be held only for snapshot startup.
