> Superseded behavior (2026-09-28): `parallel_no_backup_lock` remains an accepted API value, but now uses one worker and one transaction to preserve consistency across tables and chunks. It emits `mysql_single_connection_consistent_snapshot`. Independent worker snapshots cannot guarantee a consistent export. The historical parallel design below is not the current execution contract.

# MySQL Parallel Export Without BACKUP_ADMIN Design

## Goal

Let a MySQL user finish a **parallel** consistent Export when the account has
`RELOAD`/`FLUSH_TABLES` (so the brief global read lock succeeds) but lacks
`BACKUP_ADMIN` (so `LOCK INSTANCE FOR BACKUP` is denied). This is the exact
situation on Amazon RDS for MySQL 8.4, where `BACKUP_ADMIN` cannot be granted at
all. The existing `parallel_strict` and `single_connection` modes remain
unchanged.

## Background

The current `parallel_strict` protocol acquires two locks:

1. `FLUSH TABLES WITH READ LOCK` (needs `RELOAD`) — held only long enough for
   every worker to open a `START TRANSACTION WITH CONSISTENT SNAPSHOT`.
2. `LOCK INSTANCE FOR BACKUP` (needs `BACKUP_ADMIN`) — held for the whole dump so
   that DDL cannot change table structure while data is streaming.

Snapshot consistency across workers is fully established by step 1 + the shared
consistent-snapshot transactions. Step 2 only *prevents* concurrent DDL; it is
not what makes the dump consistent. `single_connection` already proves this by
replacing the backup lock with a before/after schema comparison (DDL-drift
check).

## New Snapshot Mode: `parallel_no_backup_lock`

Same as `parallel_strict` but:

- **skips** `LOCK INSTANCE FOR BACKUP` entirely (never requests `BACKUP_ADMIN`);
- keeps the full parallel worker fan-out and the shared consistent snapshot;
- runs the same DDL-drift check as `single_connection` after data extraction,
  failing the Export if the selected schema changed during the dump.

| Step | parallel_strict | parallel_no_backup_lock | single_connection |
|------|:---:|:---:|:---:|
| N workers `CONSISTENT SNAPSHOT` | yes | **yes** | 1 only |
| `FLUSH TABLES WITH READ LOCK` (RELOAD) | yes | **yes** | no |
| `LOCK INSTANCE FOR BACKUP` (BACKUP_ADMIN) | yes | **no** | no |
| Parallel data reads | yes | **yes** | no |
| Post-dump DDL-drift check | no | **yes** | yes |

Data integrity equals `parallel_strict` (identical shared snapshot). The only
trade-off: `parallel_strict` *blocks* concurrent DDL, while this mode *detects*
it and fails, exactly like `single_connection`.

## Rust Core Contract

`dump.run` accepts a third `mysql_snapshot_mode` value: `parallel_no_backup_lock`.

- `MysqlSharedSnapshot::acquire` takes a `use_backup_lock: bool`. When false the
  `LOCK INSTANCE FOR BACKUP` statement is not issued, so a missing
  `BACKUP_ADMIN` privilege never surfaces.
- A denied `FLUSH TABLES WITH READ LOCK` still returns the stable
  `MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:FLUSH_TABLES_OR_RELOAD:...` marker
  (this mode cannot help when even FTWRL is denied).
- The DDL-drift check that previously guarded only `single_connection` now also
  guards `parallel_no_backup_lock`.
- Manifest `snapshot_policy` is `mysql_parallel_no_backup_lock_consistent_snapshot`.
  `strict_export` stays true (a successful dump is consistent); a warning notes
  that the instance backup lock was skipped and DDL drift was checked instead.

## Python and UI Contract

The Python exporter registers the new mode and forwards it without parsing raw
MySQL text. It keeps the requested worker count (no forced single thread).

The privilege-denial dialog distinguishes the privilege in the Rust marker:

- `BACKUP_ADMIN` denied (FTWRL already succeeded): recommend **[병렬로 계속
  (백업 락 생략)]** which retries the same Export with `parallel_no_backup_lock`,
  keeping parallelism. `[단일 연결로 계속]` and `[취소]` remain available.
- `FLUSH_TABLES_OR_RELOAD` denied (FTWRL itself failed): parallel is impossible;
  offer only `[단일 연결로 계속]` / `[권한 설정 안내]` / `[취소]` as today.

The continuation attempt is labeled and never re-prompts. No silent downgrade.

## Failure and Cleanup

- The initial denied attempt writes no table data.
- A drifted-schema failure is a normal Export failure with its real reason.
- Non-InnoDB selections are rejected with the table/engine list (unchanged).
- `Drop` still issues `UNLOCK TABLES`/`UNLOCK INSTANCE` defensively; with no
  backup lock taken, `UNLOCK INSTANCE` is a harmless no-op.

## Testing

- Rust: `acquire(..., use_backup_lock=false)` issues no `LOCK INSTANCE FOR BACKUP`
  SQL; the coordinator lock sequence still releases the global read lock before
  dumping; manifest metadata reports the new policy; DDL drift fails the Export.
- Python exporter: the new mode is accepted, keeps parallel threads, and maps to
  the new snapshot-policy message.
- PyQt: `BACKUP_ADMIN` marker surfaces the parallel-continue action with the
  correct retry arguments; `FLUSH_TABLES_OR_RELOAD` marker does not; no repeated
  prompt; no silent fallback.
- Existing `parallel_strict` and `single_connection` tests remain unchanged.
