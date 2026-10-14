# Export/Import policy and compatibility

This contract applies to current releases. Database operations belong
to the bundled Rust Core. Use the matching application/Core build.

## File compatibility

| File | Import support |
| --- | --- |
| Existing manifest v1/v2 | Supported, with legacy metadata limitations below |
| New manifest v3 | Requires TunnelForge 2.6.0 or later; both JSONL and TSV use v3 |
| Manifest v4 | Written when a MySQL BIT column is present; requires TunnelForge 2.12.2 or later |
| Manifest v5 | Written when a MySQL spatial column is present; requires TunnelForge 2.12.4 or later |
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
| Overwrite (UI) | Runs `safe`, then, after one up-front confirmation, swaps the verified candidate in under the original name with the core's promotion plan; the replaced data stays under a backup name. The swap is automatic only for a `ready_for_switch` restore whose plan is promotable with no blockers; anything else falls back to the reviewed choice. The core receives `mode: safe` and `dump.promote`; there is no separate core mode. |
| `replace` | Like mysqldump: each selected dump table is dropped, recreated, loaded and finished (its secondary indexes, PostgreSQL unique indexes/comments/sequences) one table at a time; each FK is added as soon as both of its tables are done (a parent outside the dump counts as done), and views follow at the end. Existing selected-table data is lost. A failure leaves at most the table in progress missing, partially loaded or unfinished; tables already replaced are complete, and tables not yet reached keep their original data; an FK such a table lost when its parent was replaced is linked again after the failure if that parent was reloaded (otherwise it stays reported as removed). |
| `recreate` | Legacy alias of `replace`; does not delete the entire database or unrelated tables. |

The former `merge` mode (append rows to existing tables) was removed. A request
with `mode` or `import_mode` set to `merge`, or to any value not listed above, is
refused as `import_plan_invalid` before anything is written, including the
import report.

Target-only tables are preserved. A target-only FK referencing an imported parent
requires the recreated parent to retain a compatible key/type contract, including
children in other schemas. MySQL creates only the primary key and full-column
UNIQUE keys with the table, so a surviving child FK must reference one of them;
FKs bound to a non-unique or prefix index (possible on MySQL 8.0 or with
`restrict_fk_on_non_standard_key=OFF`) are refused before any table is dropped,
because re-creating the parent would otherwise fail (ERROR 1822/6125) right after
the drop. FKs between selected tables are dropped from the not-yet-replaced child
just before its parent is replaced and recreated from the dump after loading.
Unsupported target actions, including MySQL `SET DEFAULT` foreign keys, fail
preflight. The server makes this call, not a comparison of type names: before
any DROP, a copy of each such child FK is created first and the dumped parent
is then created under a probe name, the same order as the real replace, so
whatever MySQL refuses there (type, character set, collation, missing key or
column) stops the import with the target unchanged.

Before any table is dropped, the target server checks the import itself: every
selected table is created once under a temporary probe name, for `replace` with
its indexes and foreign keys (an FK to a table outside the dump points at the
live target table), and all probes are then removed. A rejected definition, a
missing REFERENCES privilege, or an FK that no longer fits a parent outside the
dump stops the import with the target unchanged. A view or other non-table
object that already uses a selected table's name is refused the same way.

### Safe restoration and destination names

If the requested destination does not exist, use the source name or the user's
explicit destination and create it exclusively. If it exists, restore into a
separate candidate first. Compare the existing objects and offer retention of
the original, use of the candidate, or explicitly confirmed replacement. A new
name may be entered before preparation. An existing namespace is never silently
reused as a staging area. PostgreSQL stays in the selected database.
Preparation requires permission to create a database (MySQL) or schema
(PostgreSQL), plus the privileges needed to load the declared objects. A denied
creation leaves the existing destination unchanged.

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

### Backup lifecycle (`restore.backups`)

The Core command `restore.backups` (UI: Import dialog, "복원 백업 관리") works from the
promotion journals stored in the dump directories passed as `input_dirs`; a
namespace that only follows the `tf_backup_` naming is listed as unproven and is
never deleted.

| Action | Effect |
| --- | --- |
| `list` | Journal status, backup namespace, ownership proof, table count and estimated rows, cutover verdict, candidate namespace. MySQL saved view aliases are reported as "not views over backup data". |
| `reconcile` | Compares an unknown/pending journal with live identities (MySQL InnoDB table ids, PostgreSQL relation OIDs): `promoted`, `not_promoted` or `undeterminable`. Report-only; retry and cleanup stay blocked unless the result is `promoted`. |
| `cleanup_plan` | Read-only preview of exactly what would be dropped, with blockers. |
| `cleanup_apply` | Requires `confirmed: true` and the digest of a plan reviewed just before; every check is re-derived from the live objects, then one owned namespace is dropped. |

Cleanup of a backup requires all of: recorded ownership (identities match the
journal and the namespace holds exactly the recorded tables), no object outside it
that references it (foreign keys, views, column defaults, inheritance, routine
text), no change since the cutover (definition and content fingerprints recorded
right after the commit; a journal without a fingerprint is refused), and a
confirmed promotion. MySQL additionally needs proven global SELECT/SHOW
VIEW/PROCESS visibility. A candidate (`tf_restore_<id>` only, never the restore
destination itself) is removable when unpromoted and still proven identical to its
verification, or, after promotion, when it holds only objects the promotion plan
recorded (PostgreSQL: the candidate's own views; MySQL: the recorded replacement
views, after the recorded temporary `tf_promote_` clone database, which is a separate
`target: "clone"` cleanup, is gone). Original objects and namespaces without a
journal are never dropped.

Guided recovery (`rollback_plan` / `rollback_apply`) is the inverse of the
promotion: one atomic multi-object RENAME (MySQL) or one transaction (PostgreSQL)
puts the retained original tables back and moves the active tables into a new
retained backup namespace (`tf_backup_rb_<hash>`); nothing is deleted and the
review plus `confirmed: true` are mandatory. It is refused unless the promotion is
confirmed, the retained backup is proven unchanged, the active tables still have
exactly the promoted content (MySQL: verified candidate/clone digests; PostgreSQL:
fingerprints taken from the verified candidate at plan time, so older plans are
refused) and identity, no view or routine is involved, and no other object depends
on the active tables (PostgreSQL: only the recorded incoming foreign keys, which are
re-pointed). Writes accepted after the promotion therefore block recovery instead of
being discarded. After a recovery the emptied promotion backup namespace and the
recovery backup can be removed with `target: "backup"` / `"displaced"` under the
same ownership, reference and no-change checks.
Verified live on MySQL 8.0.46/8.4.11 and PostgreSQL 13.23/18.4
(`live_backup_lifecycle`).

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
- Advanced `replace`/`recreate` are **not one atomic transaction**. MySQL DDL commits,
  earlier successful chunks and already running parallel workers may remain after
  a later failure. No new chunks are scheduled once a worker failure is known.
- Failed/unattempted/data-loaded tables are reported separately. In `replace`, a
  table that failed while being finished is the failed table; tables before it
  already have their indexes and FKs. Data-loaded does not mean views or final
  verification completed. A failed
  `replace` reports `dropped_not_restored_tables` (dropped, not fully reloaded),
  `unattempted_tables` (original data kept), `dropped_foreign_keys` (with each
  original definition) and `restored_foreign_keys` (dropped child FKs linked again
  after the failure); the Import failure dialog lists them, including FKs still
  missing from untouched original tables.
- UI subset retry is disabled: retrying only unfinished tables could leave earlier
  tables without deferred constraints. Inspect the partial target and rerun the
  full original selected scope from the dump with explicit confirmation.
- Views have explicit imported/failed/skipped results. Table data may already be
  committed if view recreation fails; the application must not report complete
  restoration in that case.

## Snapshot, time and parallelism

- MySQL export snapshot modes (`mysql_snapshot_mode` of `dump.run`):

  | Mode | Privileges | Behavior | Manifest |
  | --- | --- | --- | --- |
  | `parallel_strict` (default) | RELOAD (or FLUSH_TABLES), BACKUP_ADMIN | All workers open one shared consistent InnoDB snapshot under `FLUSH TABLES WITH READ LOCK` + `LOCK INSTANCE FOR BACKUP`; the global read lock is acquired within a 2-second limit and released right after the snapshot opens, while DDL stays blocked until the export ends. | `mysql_shared_consistent_snapshot`, `strict_export=true` |
  | `parallel_table_lock` | LOCK TABLES, SELECT | `LOCK TABLES ... READ` on exactly the exported tables (2-second wait limit) while every worker opens its consistent snapshot, then `UNLOCK TABLES` before any data is read. Writes to those tables wait during that moment. Same point-in-time guarantee as strict; DDL changes are checked after the dump. | `mysql_table_lock_shared_snapshot`, `strict_export=true` |
  | `parallel_unsynchronized` | none beyond SELECT | Explicit opt-in. Each worker takes its own snapshot without any lock, so cross-table and cross-chunk point-in-time consistency is **not** guaranteed. | `mysql_parallel_unsynchronized_snapshots`, `strict_export=false`, persistent warning; Import reports it again |
  | `single_connection` | none beyond SELECT | One connection, one consistent snapshot, one worker. | `mysql_single_connection_consistent_snapshot`, `strict_export=true` |
  | `parallel_no_backup_lock` | none beyond SELECT | Kept for API compatibility; runs like `single_connection`. | same as `single_connection` |

  When `parallel_strict` is refused for missing privileges, the Export dialog asks
  once: continue in parallel with the table lock (recommended, default), continue
  in parallel without point-in-time consistency, continue with one connection, or
  cancel. If the table-lock retry is refused because LOCK TABLES is also missing,
  it asks a second and last time with only one connection (default) or
  unsynchronized parallel. A new export starts from `parallel_strict` again, and
  re-opening an export from the job list never reuses a recorded snapshot mode.
  Granting RELOAD and BACKUP_ADMIN avoids the question.
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

CI also replays the shape of a real production schema (311 tables, 11 views),
anonymized by `scripts/anonymize_dump_schema.py`: replace into an empty and an
existing database, with target-only children on every single-column key, with
a database-scoped account, safe restore and switch, and export/re-import
(`live_real_schema`). Into PostgreSQL, a MySQL dump is refused before any change
when tables use MySQL `ON UPDATE` columns or CHECK expressions, which have no
PostgreSQL translation yet; every such table is listed at once.

The normalized format does not yet preserve every DB object. Selected tables
with detected unrepresentable generated/custom-type/advanced-index/default/
cross-schema-FK semantics are refused before export rather than producing a file
that appears complete. PostgreSQL ALWAYS identities and custom/shared sequence
semantics are also refused. Ordinary identity restoration uses `MAX(id)+1`, not
the source's unused/deleted allocation gaps.

### Object support table (Export)

Only the scope below is officially supported; the format is not being extended to
triggers, routines or events. Live-verified on MySQL 8.0.46 / 8.4.11 and
PostgreSQL 13.23 / 18.4 (`live_export_contract`,
`unsupported_objects_are_refused_before_any_file_and_opt_in_proceeds`): the
preserved core, triggers, routines, events, materialized views, MEMORY tables,
default refusal, opt-in and a strict export/import round trip. The lossy-definition
rows (generated columns, CHECK, defaults, advanced indexes, cross-schema FKs)
come from the existing inspection and its earlier fixtures. Other versions are not
claimed.

| Object | MySQL | PostgreSQL |
| --- | --- | --- |
| Tables, rows, PK/UNIQUE/secondary indexes, supported FKs, comments, supported defaults | Preserved | Preserved |
| Views (full export only) | Preserved as separately captured definitions; persistent warning, `strict_export=false` | Same |
| Generated columns, unsafe CHECK, unsupported defaults, advanced indexes (functional/descending/FULLTEXT/SPATIAL/partial/non-btree), cross-schema FKs | Refused before export; **not waivable** | Same, plus custom types/ALWAYS identity/custom sequences |
| Triggers on an exported table | Refused before export; waivable | Same |
| Routines (functions/procedures), events, materialized views | Refused before export for a **full** export; waivable. Not in scope of a table subset, so a subset export proceeds with a persistent warning (unchanged behavior) | Same (no events; materialized views apply) |
| Non-InnoDB tables (MyISAM, MEMORY, ...) in the exported scope | Refused before export; waivable | n/a |

Default behavior: when the selected scope contains any refused object, `dump.run`
fails before the target directory or any dump file is created, with
`error_code: "unsupported_objects"`, `objects` (one `kind:table:name` entry per
object) and `bypassable`. Only the waivable objects can be omitted, and only by
the explicit choice "Export without these objects (incomplete)" (`allow_incomplete:
true` in the `dump.run` payload; the UI lists the objects, up to 10 inline plus a
count of the rest, before asking). The structure and data of all remaining tables
are still exported; only the listed objects are left out. That choice
keeps `strict_export=false` and adds a persistent `Object not exported: ...`
warning per object. Lossy table definitions (`bypassable: false`) cannot be
waived. Scheduled backups have no such prompt and therefore fail with the
refusal instead of silently producing an incomplete backup. Import behavior is
unchanged. Objects this inspection does not detect (for example MySQL
partitioning, PostgreSQL extensions, rules or privileges) are not reported.

A successful table transfer does not certify complete database backup fidelity.
No new signing/notarization requirement or external database dump tool is
introduced.

See [verification evidence](export_import_verification_2026-09-28.md) for fixtures
and results. Keep a recoverable target backup before destructive replacement.
