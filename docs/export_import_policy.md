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

The Import dialog has one default behavior and an Advanced panel that starts
closed; the line beside its "고급 설정 열기" button lists every setting that is on
or forced. There are no mode choices.

| Setting | Default | Sends |
| --- | --- | --- |
| (default) | always | `mode: "replace"` into the dump's own database (PostgreSQL: schema), shown read-only on the target line: `host:port / DB`, on PostgreSQL `host:port / DB / schema` (`postgres` when the connection names no database) |
| 다른 DB 이름으로 넣기 (PostgreSQL: 다른 스키마 이름으로 넣기) | off | that name as the target database (schema). The name field starts empty whenever it appears, so no existing database is ever picked for the user; an empty name starts nothing. Forced on, with its name field on the target line and one line saying why, when the dump names no database of this engine (another engine's dump, or a legacy PostgreSQL dump without `source_schema`) |
| 먼저 복사본에서 검증하고 교체 | off | `mode: "safe"`; a verified copy whose promotion plan is clean is swapped in without another question (below). Disabled for another engine's dump. The description under the target line follows it: with it off, tables are dropped and recreated one at a time and a stop leaves the table in progress possibly empty; with it on, the original stays as it is until the swap |
| 시간대 직접 정하기 | off | offered only for a legacy dump without `source_timezone`: 한국 시간(+09:00) and 세계 표준시(UTC) send `timezone_sql`, 대상 서버 기본값 sends `use_source_timezone: false`. Current dumps are written in UTC and record it, so for them the setting is disabled |
| 동시 작업 수 | 8 (1-16) | `threads` |

The thread count and the verify-in-copy setting are remembered. The other name
and the timezone never are: a wrong one writes into another database or shifts
TIMESTAMP values. `import_mode` has no default in the application
(`RustDumpImporter.import_dump`, `import_dump`, the import worker); the core's
`safe` fallback for a request without a mode serves old callers only.

| Mode | Effect and constraints |
| --- | --- |
| `safe` | Restores the complete same-engine dump into a new owned namespace and verifies rows, values and schema; existing namespaces remain unchanged during preparation. In the dialog ("먼저 복사본에서 검증하고 교체"), a `ready_for_switch` restore whose `dump.promote` plan is promotable with no blockers is then swapped in under the original name, and the replaced tables stay under a backup name. Anything else stops with one window (below). Into a missing namespace the core restores directly (`completed_new_target`). |
| `replace` | Like mysqldump: each selected dump table is dropped, recreated, loaded and finished (its secondary indexes, PostgreSQL unique indexes/comments/sequences) one table at a time; each FK is added as soon as both of its tables are done (a parent outside the dump counts as done), and views follow at the end. Existing selected-table data is lost. A failure leaves at most the table in progress missing, partially loaded or unfinished; tables already replaced are complete, and tables not yet reached keep their original data; an FK such a table lost when its parent was replaced is linked again after the failure if that parent was reloaded (otherwise it stays reported as removed). |
| `recreate` | Legacy alias of `replace`; does not delete the entire database or unrelated tables. |

A `replace` (or `recreate`) import into a MySQL database or PostgreSQL schema
that does not exist creates it first, after the dump checks and before any table
work. A new MySQL database is created as `utf8mb4`, with the server's default
collation when that is a `utf8mb4` one and `utf8mb4_unicode_ci` otherwise, so
cross-engine tables (which carry no table collation) never inherit a latin1
server default; a safe restore into a missing MySQL database does the same.
This needs the CREATE privilege for that database (MySQL) or on the database
(PostgreSQL). A database the account cannot see (no privilege on it) counts as
missing, so its CREATE fails as `target_create_failed`.
The import report records `namespace_creation_outcome_unknown: true` before the
CREATE and `namespace_created: true` only after it succeeded, so a failed CREATE
stays reported as of unknown outcome; `target_change` events (`create_database`
or `create_schema`, status `started` then `completed`) bracket it. A namespace
created this way is not dropped when the import fails later; the result and the
failure report carry `namespace_created`. A missing PostgreSQL database is never
created: the import fails to connect.

The former `merge` mode (append rows to existing tables) was removed. A request
with `mode` or `import_mode` set to `merge`, or to any value not listed above, is
refused as `import_plan_invalid` before anything is written, including the
import report.

Every import attempt (`replace`, `recreate` or `safe`) writes its own report into
the dump folder, `_tunnelforge_import_report_<nanoseconds>_<process>_<n>.json`,
and names it (`report_path`) in its `import_report` events from the start, so a
retry never overwrites the record of an earlier attempt. The attempt's last
`import_report` event carries its final status. A safe restore and the
candidate import inside it share one report, which a later promotion updates in
place. Dump folders imported by earlier versions keep their single
`_tunnelforge_import_report.json`; it is still read, and the job list and the
Import dialog link to the report of their own run.

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
On PostgreSQL, anything that DROP TABLE would refuse over is also found first:
a table the account neither owns nor owns the schema of, a view, a target-only
FK child, and any other `pg_depend` dependent, such as a default or view that
uses a sequence a selected table owns, a column of a selected table's row type,
an inheriting table, or a policy or function body naming the table. Tables are
dropped one by one in dependency order, so a dependent that is itself a selected
table counts only when its table is dropped after the one it depends on.

### Preview (`dump.import_preview`)

`dump.import_preview` takes the `dump.import` payload without `mode` and reports
what a replace import would do. It changes nothing: it writes no import report,
creates no probe tables and does not read chunk data. `service.hello` lists the
command, and the application refuses to preview with a core that does not, since
an older core ignores unknown `dump.import` fields.

The result lists the dump tables that would be replaced (`replace`) or created
(`create`) in the order the import takes them, target tables left alone
(`untouched`), kept target-only children (`kept_children`), FKs to parents
outside the dump and whether the target has them (`external_parents`), view
changes (`views`: `replace`, `create`, `may_not_replace` for views the account
may not drop: on MySQL under another DEFINER when the account lacks SYSTEM_USER,
on PostgreSQL owned by a role the account cannot act as, `may_break` for MySQL
target-only views over replaced tables, from
`information_schema.VIEW_TABLE_USAGE`), the triggers that disappear with replaced
tables (`triggers_dropped`), and the dump's `strict_export`, `snapshot_policy`
and `source_timezone`. When the namespace is missing, every table is under
`create` and `target.can_create` says whether the account may create it (`null`
when its grants cannot tell, for example with roles or partial revokes); the
replace import creates it.

`blockers` (`{code, table, detail}`) are what the import refuses before its first
DROP: `dump_invalid`, `untranslatable_table`, `spatial_unsupported`,
`temporal_unsupported`, `no_tables`, the PostgreSQL
dependents above (`drop_privilege_missing`, `dependent_view`,
`target_only_fk_child`, `dependent_object`), `check_name_collision`,
`name_collision`, `create_privilege_missing`, on MySQL `drop_privilege_missing`
when the account has no database-wide DROP grant (the DDL probe drops its own
tables), and on PostgreSQL `external_parent_missing` (MySQL adds such an FK with
`foreign_key_checks=0`). `copy_blockers` are what `safe` refuses on top of that
(`legacy_bit_unverifiable`, `cross_engine`, and `partial_table_filter` when the
request carries a `tables` filter; a partial export is not refused), plus, for an
existing same-engine target, the target checks `safe` runs before making a copy
(see below); when those checks cannot run, one `copy_checks_unavailable` item
takes their place and the rest of the preview is still returned. `warnings`
(`{code, detail}`) map the manifest's texts to `unsynchronized_snapshot`,
`views_outside_snapshot`, `omitted_object`, `visibility_unproven` or
`source_dump_warning`, and add
`not_strict`, `legacy_timezone`, `legacy_dump`, `create_privilege_unproven`,
`drop_privilege_unproven` and `view_dependencies_unknown`.

### Before and after a run in the Import dialog

Every run (also "같은 설정으로 다시 Import") starts with `dump.import_preview`; a
preview that fails, is refused, or returns anything but a `dry_run: true` result
with `replace`, `create` and `blockers` lists starts no import (an error without
a code shows one plain sentence and keeps the raw text behind "자세히"). While
the preview runs, the start and rerun buttons are off and a second start is
ignored; an answer from an earlier preview is dropped. Closing the window or
stopping during the preview stops it without asking, and an answer that arrives
after that starts nothing. The one confirmation, the plan dialog, appears only
when the run would overwrite at least one existing table or view (user decision
F12, on every environment), when the preview has a blocker, or when it warns
that the imported data itself may be off: triggers that disappear, views the
account may not replace or that may break, FKs whose parent is not in the dump
(worded apart when the parent is missing from the target too), a dump without
one point in time (`snapshot_policy` `mysql_parallel_unsynchronized_snapshots`
or the legacy `mysql_parallel_no_backup_lock_consistent_snapshot`), or a legacy
dump without a chosen timezone. Otherwise the import starts at once. When the
dialog appears, a production profile must type the DB (PostgreSQL: schema) name;
staging and unclassified profiles start on its Cancel button; development gets
no colored warning. The dialog names the environment in Korean (운영, 스테이징,
개발, 미분류), one warning line (덮어쓴 테이블은 되돌릴 수 없습니다; with
verify-in-copy, 교체 전 테이블은 백업 DB(PostgreSQL: 백업 스키마)에 남습니다; with
only existing views replaced, 기존 뷰 N개를 바꾸고, 나머지는 새로 만듭니다; with
nothing to overwrite, 새로 만들기만 합니다), the target (with the profile name in
front of a tunnel's local `host:port`) and "(새로 만듭니다)" when it is missing,
the settings in use, the tables to overwrite (with verify-in-copy: to swap),
create and leave as they are (each list opens from its count and scrolls), the
warnings in Korean, FK parents added at export (only those that are also in the
dump; the rest as not in the dump) and the `postgres` fallback, and behind
"자세히" the kept target-only FKs, the view counts and the core's own text. A
blocker, also one the application cannot read, disables its start button, and
the run never starts while one is listed; `copy_blockers` count only with
verify-in-copy on, followed by a line that turning it off lets the run go ahead.

Success opens no window. The status line and the log say "덤프 테이블 N개를
넣었습니다" (N counts every dump table, overwritten or new), "새 DB(스키마)를
만들어 넣었습니다: {name}", or after a swap "교체했습니다. 이전 테이블은 백업
DB(스키마) {backup}에 있습니다". A verify-in-copy run that verifies its copy but
cannot swap it (`ready_for_review`, a promotion plan that is blocked or fails,
or a swap the core refused with the original unchanged) shows one window: the
copy it verified, the reasons in Korean (those the core gave only as text are
in the run log on screen), that the original is unchanged, a "복원 백업 관리"
button and Close. "복원 백업 관리" in the window, and the same button under the
table list after any run that made a copy, open backup management with this
session's credentials. Only a swap whose outcome is unknown shows a separate
warning. The stop question names what a stop leaves behind: overwritten tables
stay and the table in progress may be empty (default), the original is unchanged
(loading the copy), or the swap's outcome may become unknown (swapping); the
read-only checks stop without asking. The job list labels the run from its
result: `Import · 덤프 테이블 교체`, plus ` · 복사본 검증` as soon as the core made
a copy (also on the running record, so a run the app never finished still opens
backup management), ` · 새 DB 생성` (PostgreSQL: ` · 새 스키마 생성`) when the run
created the database, and ` · 대상 {name}` for another name; records with
` · 복사본 검증`, and older ones that start with "안전 복원" or "덮어쓰기", open
backup management. Without a saved profile (an Import opened from the main
menu) or saved credentials, the job list asks for the DB account instead of
refusing.

### Failure texts

Core failures and preview items carry stable codes: the leading `code:` of an
error (`code: table: ...` when it names a table), an error event's `error_code`
or `(error_code=...)`, and a preview item's `code`. A failed table is
`load_failed: table: ...` whether its DROP, its CREATE or its row load failed; a
row-load error that already has a code (`export_invalid` for a bad chunk) keeps
it. The application words each code as one plain Korean sentence
(`CORE_CODE_SENTENCES` in `rust_dump_exporter.py`), prefixed with the table name
when there is one; a test scans the core's import sources and fails on a code
without a sentence. A code is taken only at the start of a failure text (after
the importer's own prefix) or after `; additionally: `, never from a path or
server text inside it. When the core joins several failures, each code is
worded, and a text that says the Import report could not be persisted adds that
the report was not saved.

The Import failure window shows those sentences (after a failed table, as the
cause above the table counts) and keeps the core's own text behind its "자세히"
button, which opens it beside the window. Without a known code, a failed table
is reported as an unclassified cause with the text behind "자세히"; any other
failure text is shown as it is.

A failed or refused `safe` restore arrives as a `success: false` result with
status `failed_original_untouched`, not as an error. Its `error` and `blockers`
are passed through as the failure message (its `message` instead when the final
report could not be written, since only `message` says so), so the window names
the core's reason. When the copy was already made, the window names it and says
it was left in place; without an existing target the copy is the requested
target itself and may be only partly loaded. A restore that stopped before
making a copy (`copy_refused`, below) is shown instead as the `copy_refused`
sentence followed by one sentence per `copy_blockers` code with the objects it
names; the core's own text opens from "자세히", and a code without a sentence
gets a general line that points to the report. The other refusals before a
copy are coded as well: `dump_invalid`, `spatial_unsupported` (checked after the
copy refusals, so a cross-engine dump is refused as such) and `no_tables`.

### Safe restoration and destination names

If the requested destination does not exist, use the source name or the user's
explicit destination and create it exclusively. If it exists, restore into a
separate candidate first and verify it; the dialog swaps it in when its
promotion plan is clean and otherwise stops, keeping the original and the
candidate. Another name may be entered before preparation ("다른 DB 이름으로
넣기"). An existing namespace is never silently reused as a staging area.
PostgreSQL stays in the selected database.
Preparation requires permission to create a database (MySQL) or schema
(PostgreSQL), plus the privileges needed to load the declared objects. A denied
creation leaves the existing destination unchanged.

Verification includes streamed SHA-256 content digests, not just row counts.
Preparation failures retain the candidate for inspection and preserve the
existing namespace. Unsupported replacement plans leave the verified candidate
available without changing the original. Partial table filters and cross-engine
safe restoration are currently refused; use the explicit migration workflow for
cross-engine changes.

Into an existing namespace, `safe` stops before it makes a copy (and before it
reads the chunks) when the later swap could not go through. The result is
`failed_original_untouched` with `candidate_created: false`,
`cutover_pending: false`, an `error` that starts with `copy_refused:`,
`error_code: "copy_refused"` and `copy_blockers` (`{code, table, detail}`), the
same list `dump.import_preview` returns. A `tables` filter stops the run before
the dump is read (`partial_table_filter`); `legacy_bit_unverifiable` and
`cross_engine` stop it from the dump alone; the rest come from the target:

| Code | Stops when |
| --- | --- |
| `copy_create_privilege_missing` | MySQL: the grants do not show that the account can create `tf_restore_*`, `tf_backup_*` and `tf_promote_*` databases (unreadable grants count as missing); PostgreSQL: it has no CREATE on the database. |
| `metadata_visibility_unproven` | MySQL: no direct global SELECT, SHOW VIEW and PROCESS grants (roles and partial revokes count as unproven). |
| `target_routines`, `target_events` | The target namespace has stored routines (PostgreSQL: functions) or MySQL events; the swap does not move them and cannot prove they still work afterwards. |
| `target_triggers` | MySQL: a dump table has a trigger in the target; PostgreSQL: any table in the schema has one. |
| `linked_target_only` | A table or view outside the dump is linked to a dump table or view: an FK in either direction (from the target catalog or the dump's own FK definitions) or a view reading one, in this or another schema. On PostgreSQL, a dump view whose current definition reads a table outside the dump in the same schema does not count: the swap replaces that view in place and leaves the table alone. |
| `external_parent_missing` | A dump table's own FK names a parent that is neither in the dump nor in the target. |
| `saved_view_alias` | The linked object is an old view definition an earlier MySQL swap kept (`tf_p_<nonce>_oldv<i>`); clean it up first (`target: "view_aliases"` below). |
| `previous_copy_leftover` | The linked object is in a `tf_restore_*`, `tf_backup_*` or `tf_promote_*` namespace an earlier restore left, e.g. the views a PostgreSQL candidate keeps after its swap (they stay bound to the tables it moved in); clean up that candidate or backup first. |
| `view_dependencies_unknown` | MySQL older than 8.0.13: views over the target cannot be listed. |

A linked table or view is never rebuilt from its SHOW CREATE text to keep it
(TF-STATUS-173): the run stops instead. Tables and views outside the dump that are
not linked to it do not block: the swap leaves them and their other objects
where they are (PostgreSQL re-creates only the dump's views; a trigger on any
of its tables still stops the run, see `target_triggers`), so such a restore
reaches `ready_for_switch`. As a second guard, `dump.promote`
runs the same target checks again when it plans and before it confirms (links
to the restore's own copy aside): a plan is `blocked` with the same
`copy_blockers`, and a confirm whose target changed since review is refused. It
also refuses a plan that would rebuild a table or view outside the dump (MySQL)
or move a target-only table's FK to the new parent (PostgreSQL), and a stored
plan that would. A request with `verify_only: true` (the scheduled restore
rehearsal, which only verifies and cleans up the copy) skips these target checks;
its report records `verify_only: true` and `dump.promote` refuses it.

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

The Core command `restore.backups` (UI: "복원 백업 관리" in the Import dialog after a
run that made a copy, or the job list's on such a record) works from the
safe-restore reports (every attempt's, and the single report of earlier
versions) and promotion journals stored in the dump directories passed as
`input_dirs`; a namespace that only follows the `tf_backup_` naming is listed as
unproven and is never deleted.

| Action | Effect |
| --- | --- |
| `list` | Journal status, backup namespace, ownership proof, table count and estimated rows, cutover verdict, candidate namespace. MySQL saved view aliases are reported as "not views over backup data". |
| `reconcile` | Compares an unknown/pending journal with live identities (MySQL InnoDB table ids, PostgreSQL relation OIDs): `promoted`, `not_promoted` or `undeterminable`. Report-only; retry and cleanup stay blocked unless the result is `promoted`. |
| `cleanup_plan` | Read-only preview of exactly what would be dropped, with blockers. |
| `cleanup_apply` | Requires `confirmed: true` and the digest of a plan reviewed just before; every check is re-derived from the live objects, then one owned namespace (or, for `view_aliases`, the listed views) is dropped. |

Cleanup of a backup requires all of: recorded ownership (identities match the
journal and the namespace holds exactly the recorded tables), no object outside it
that references it (foreign keys, views, column defaults, inheritance, routine
text), no change since the cutover (definition and content fingerprints recorded
right after the commit; a journal without a fingerprint is refused), and a
confirmed promotion. MySQL additionally needs proven global SELECT/SHOW
VIEW/PROCESS visibility. A candidate (`tf_restore_<id>` only, never the restore
destination itself) is removable when unpromoted and still proven identical to its
verification; after a failed restore (no plan or attempt recorded), when it holds
only the dump's tables and views and leftover `tf_probe_` tables (any routine,
event or trigger, or on PostgreSQL any function, explicit type or unowned
sequence, blocks it); or, after
promotion, when it holds only objects the promotion plan
recorded (PostgreSQL: the candidate's own views; MySQL: the recorded replacement
views, after the recorded temporary `tf_promote_` clone database, which is a separate
`target: "clone"` cleanup, is gone). After a confirmed MySQL promotion,
`target: "view_aliases"` drops the saved old-view aliases (`tf_p_<nonce>_oldv<i>`)
the journal records, while each still holds the definition it saved and no view
uses it and no routine, trigger or event mentions it; the active views are not
touched. Original objects and namespaces without a journal are never dropped.

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
- `replace`/`recreate` (the Import dialog's default) are **not one atomic transaction**. MySQL DDL commits,
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
  | `parallel_strict` (default when the field is absent) | BACKUP_ADMIN, RELOAD (or FLUSH_TABLES) | `LOCK INSTANCE FOR BACKUP` first, then `FLUSH TABLES WITH READ LOCK` within a 2-second limit; all workers open one shared consistent InnoDB snapshot and the global read lock is released right after, while DDL stays blocked until the export ends. Taking the backup lock first means an account without BACKUP_ADMIN is refused before FTWRL has blocked every write on the server. | `mysql_shared_consistent_snapshot`, `strict_export=true` |
  | `parallel_table_lock` | LOCK TABLES, SELECT | `LOCK TABLES ... READ` on exactly the exported tables (2-second wait limit) while every worker opens its consistent snapshot, then `UNLOCK TABLES` before any data is read. Writes to those tables wait during that moment. Same point-in-time guarantee as strict; DDL changes are checked after the dump. | `mysql_table_lock_shared_snapshot`, `strict_export=true` |
  | `parallel_unsynchronized` | none beyond SELECT | Explicit opt-in. Each worker takes its own snapshot without any lock, so cross-table and cross-chunk point-in-time consistency is **not** guaranteed. | `mysql_parallel_unsynchronized_snapshots`, `strict_export=false`, persistent warning; Import reports it again |
  | `single_connection` | none beyond SELECT | One connection, one consistent snapshot, one worker. | `mysql_single_connection_consistent_snapshot`, `strict_export=true` |
  | `parallel_no_backup_lock` | none beyond SELECT | Kept for API compatibility; runs like `single_connection`. | same as `single_connection` |
  | `auto` | any | The core picks: `parallel_strict`, then `parallel_table_lock`, then `mysql_snapshot_fallback` (`single_connection`, the default, or `parallel_unsynchronized` when the user explicitly gave up point-in-time consistency). See below. | the method that ran; never `auto` |

- `auto` moves to the next method only when a later one can still work: a missing
  privilege (1044/1045/1142/1227), a backup lock the server does not know
  (1064/1235, on `LOCK INSTANCE` only), a connection limit (1040/1203/1226), or a
  lock not granted within 2 seconds (1205, the watchdog's KILL, or 1213 when the
  lock wait was picked as a deadlock victim; TF-STATUS-185).
  A snapshot connection refused by the account's own limit (1203/1226) is tried once
  more after 250 ms, because the sessions the previous method just closed can still
  count against it for a moment.
  A lock timeout therefore falls to the table lock and then to one connection,
  never on its own to the unsynchronized mode. Any other error fails the export.
  When the backup lock was taken and only FTWRL failed, the table-lock method keeps
  it (leaving LOCK TABLES mode releases it, so it is taken again right after
  `UNLOCK TABLES`; DDL in that moment is caught by the drift check). Every other
  failed attempt releases it. No method needs a privilege dialog.
- The worker count is `max(1, min(threads, max_user_connections - own_sessions - 1))`
  when `@@session.max_user_connections` is set; `own_sessions` are the account's
  rows in `information_schema.PROCESSLIST` (no PROCESS privilege needed, matched by
  user name: `USER()` up to its last `@`, since a name may hold `@` itself). This
  applies to every MySQL mode.
- `service.hello` lists `dump.run.mysql_snapshot_auto` when `dump.run` takes
  `auto`. The application checks it before sending `auto` and otherwise stops with
  a Korean sentence asking for the core shipped with the app, instead of an older
  core's `unsupported mysql_snapshot_mode: auto`.
- The method that ran drives the worker count, the phase events, the manifest
  policy and the result. The `dump.run` result and the snapshot phase event carry
  `mysql_snapshot_mode` (resolved), `worker_count` and `snapshot_fallbacks`
  (`[{mode, reason}]`, `reason` one of `privilege`, `unsupported`,
  `connection_limit`, `lock_timeout`); the application builds its completion line
  from them, for example `같은 시점으로 내보냄 · 동시 작업 8개 (잠금 권한이 없어 테이블만
  잠깐 잠금)`. The wording promises no DDL protection for the table lock or one
  connection.

  The Export dialog and scheduled backups send `auto` (the dialog with the
  fallback its Advanced setting picks, below); neither asks about privileges. A
  missing privilege only moves the core on to the next method, and the dialog's
  completion window and log name the method that ran. A caller that sends no
  mode still gets `parallel_strict`.
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

## Export dialog

The Export dialog has one default behavior and an Advanced panel that starts
closed; the line beside its "고급 설정 열기" button lists every setting that is on
or not at its default. There are no scope or snapshot choices.

- **One table list.** It is always shown (checkboxes only, no row selection) and
  starts with every table checked. All checked sends a full export
  (`dump.run` without `tables`: tables and their triggers plus views, routines and events); unchecking
  any table sends exactly the checked tables, and one line under the list says
  that views, stored procedures, functions and events are then left out
  (`일부 테이블만 내보낼 때는 테이블과 그 트리거만 담습니다. 뷰·저장 프로시저·함수·이벤트는 빠집니다.`;
  PostgreSQL leaves out "·이벤트"). A table the user unchecked that the FK toggle
  checks again as a parent keeps the run a table export, even when every box ends
  up checked. "다시 열기" from the job list with `scope: full` checks everything
  again; with `scope: tables` it checks the tables recorded for that run.
- **Method.** The dialog always sends `mysql_snapshot_mode: "auto"` with
  `mysql_snapshot_fallback` (`single_connection`, or `parallel_unsynchronized`
  with the toggle below), so it never asks about privileges and never retries on
  its own. A lock not granted in time (TF-STATUS-185) falls to the table lock and
  then to one connection, never to the unsynchronized method unless the user
  turned that on. The completion window shows the method line built from the
  result (`내보낸 방법: 같은 시점으로 내보냄 · 동시 작업 8개 (잠금 권한이 없어 테이블만
  잠깐 잠금)`); the run log and the saved log show it once, at the end of the
  result line (`✅ Export 완료: 테이블 3개, 행 100개 · 같은 시점으로 내보냄 · 동시 작업 8개`).
  The unsynchronized warning shows only when the result says
  `parallel_unsynchronized`.

| Setting | Default | Sends |
| --- | --- | --- |
| 연결된 부모 테이블 함께 담기 | off | nothing new: the dialog checks the FK parents itself (below) and sends `include_fk_parents: false` with the checked list; `true` only when it could not read the FKs, so the worker reads them again and fails the run if it still cannot. A full export (every table checked by the user) is not affected. |
| 시점 일관성 포기하고 더 빠르게 | off | `mysql_snapshot_fallback: "parallel_unsynchronized"`: only when neither shared lock works. Hidden for PostgreSQL. The summary line says `시점 일관성 포기 허용`. |
| 담을 수 없는 객체는 빼고 내보내기 | off | `allow_incomplete: true` from the start, so the omittable objects below are left out without asking. A table that is not InnoDB (MyISAM, MEMORY, ...) is then left out entirely, structure and data; the tooltip says so. |
| 동시 작업 수 / 압축 | 8 / 압축(zstd) | `threads` / `compression` (압축 안 함 = `none`). |
| 폴더 이름 직접 정하기 | off | the typed folder name, or `export_{time}` when it is left empty (the summary line says `폴더 이름(비우면 export_시각)`); off, the folder is `{connection}_{DB}_{time}`. |

The settings are remembered when a run starts (`rust_dump_export_threads`,
`_compression`, `_include_fk_parents`, `_folder_mode`, `_folder_manual_name`); the
table selection is not, and neither are "시점 일관성 포기하고 더 빠르게" and "담을 수
없는 객체는 빼고 내보내기": like Import's other database name and time zone, a value
turned on once must not make a later backup less consistent or less complete
without being chosen again. A value of the wrong type is ignored. The earlier
"자동 지정 / 수동 지정" folder setting keeps
working: a saved `rust_dump_export_folder_mode: "manual"` turns "폴더 이름 직접
정하기" on with the saved name, and the old name/schema/timestamp switches
(`rust_dump_export_folder_use_*`) are no longer read.

**FK parents.** `RustDumpExporter.fetch_fk_parents(schema)` reads the FKs with one
`schema.inspect` call, which the dialog makes only once a table is unchecked, on
the UI thread under a wait cursor, and keeps per schema; a failed read is not
kept, so the next change of the list reads again. The pure
`fk_parent_closure(picks, deps)` and `fk_parents_outside(picks, deps)` run on
every change of the list, whatever the toggle says. Only parents that are tables
of the same schema count.

- Toggle off: nothing is checked for the user. When a checked table references a
  table that is not checked, one line under the list names them and the toggle
  (`고른 테이블 1개의 연결된 부모 테이블이 체크되어 있지 않습니다 (orders → customers).
  고른 테이블만 내보냅니다. 함께 담으려면 고급 설정에서 '연결된 부모 테이블 함께 담기'를
  켜세요.`; past three pairs, `외 N개`), and `_export_metadata.json` records
  `fk_parents_outside_dump: [{child, constraint, parent}]` (`null` when the FKs
  could not be read; the line then says so).
- Toggle on: the parents a checked table needs, transitively, are checked and
  disabled (tooltip "체크한 테이블이 이 테이블을 참조해 자동으로 담았습니다. 참조하는
  테이블의 체크를 풀면 다시 고를 수 있습니다.", item text unchanged) while a checked
  table needs them, with a line `연결된 부모 테이블 자동 추가 N개: ...`. Unchecking the
  child releases them back to unchecked; "전체 선택" makes them the user's own picks,
  so unchecking a child later keeps them. When the FKs could not be read, the line
  says they are checked again when the export starts, and the run sends
  `include_fk_parents: true`. The run otherwise sends exactly the
  checked list with `include_fk_parents: false` and `auto_added_tables`, which the
  metadata keeps as `added_fk_tables` (only names that are in the dump). The job
  record's re-run tables hold only the user's own picks.
- `include_fk_parents` defaults to `false` in `RustDumpExporter.export_tables`,
  the module-level `export_tables` and the export worker, and is no longer kept
  in job records (`_RERUN_KEYS`), so an old record's value is dropped. Scheduled
  table backups pass `include_fk_parents=True` explicitly and, like scheduled full
  backups, `mysql_snapshot_mode="auto"` (fallback `single_connection`).

**Refusals.** `describe_export_refusal(objects)` words each `kind:table:name`
code as `테이블 {T}: {reason}` (routines, events and materialized views by their
own name); an unknown code becomes `테이블 {T}: 이 객체는 담을 수 없습니다`. The codes
themselves open from "자세히". Names sit in parentheses so the Korean particle
follows the noun (`트리거(trg)는`, `인덱스(idx)는`, `외래 키(fk)가 다른 DB나 스키마의
테이블을 가리켜`); a table that is not InnoDB reads `MyISAM 엔진이라 다른 테이블과 같은
시점으로 읽을 수 없습니다. 빼고 내보내면 이 테이블이 통째로 빠집니다`. An omittable
refusal (`bypassable: true`) asks once ("덤프에 담을 수 없는 객체가 있습니다"), only
while "담을 수 없는 객체는 빼고 내보내기" is off, and names that toggle. A lossy one
(`bypassable: false`) has only Close: the core lists the lossy definitions
(`LOSSY_REFUSAL_KINDS`, the same six kinds as `dump.rs`) and then every omittable
object in scope, so the window puts the lossy ones under "덤프에 그대로 담을 수 없는
테이블 정의가 있어 Export를 시작하지 않았습니다." and the rest under "그 밖에 빼고 내보낼
수 있는 객체:", and ends with "목록에서 이 테이블의 체크를 풀면 나머지 테이블은 내보낼 수
있습니다." (on a full export, followed by the partial-export line above). Neither
lands in the generic "Export 실패" window or sends an error report. The job record
and the saved log keep the sentences (`담을 수 없는 객체가 있어 Export를 시작하지
않았습니다. 테이블 orders: ...`, up to 10 plus `외 N개`), never the core's English text,
which goes to the app log only; a declined omittable refusal is `cancelled`, a lossy
one `failed`. A run that left objects out (manifest `Object not exported` warnings)
is recorded as `partial` and says so ("일부 객체를 빼고 내보냈습니다(불완전 Export).
... 빠진 객체는 실행 로그의 'Export 경고' 줄에서 볼 수 있습니다."); with the toggle on
but nothing to leave out, the export is complete.

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
with detected unrepresentable custom-type/advanced-index/default/
cross-schema-FK semantics are refused before export rather than producing a file
that appears complete. PostgreSQL ALWAYS identities and custom/shared sequence
semantics are also refused. Ordinary identity restoration uses `MAX(id)+1`, not
the source's unused/deleted allocation gaps.

### MySQL triggers (format 7, replace mode)

The manifest's `stored_programs` holds each exported table's triggers. For a
TRIGGER, `definition` is the exact `information_schema.TRIGGERS.ACTION_STATEMENT`
suffix of `SHOW CREATE TRIGGER`'s named `SQL Original Statement` column. Capture
fails closed if the body cannot be proven; refusal includes a per-object reason.
Timing, event and positive ACTION_ORDER are structured fields, validated before
SQL construction. Non-UTF-8 client charsets require an ASCII body and ASCII
header identifiers, since the connection sends UTF-8 bytes. The exporter
recaptures stored programs at its MySQL DDL drift check and refuses a changed set or
context. Only exported InnoDB tables' triggers survive scope filtering.

Replace import loads data with no dump triggers installed, completes each table's
indexes/FKs, then immediately creates that table's triggers in ACTION_ORDER.
Thus INSERT/LOAD does not fire the triggers, and a failure on a later table leaves
earlier tables protected. Each table opens a new, timeout-tuned trigger connection
after its load; the probe connection is closed before loading begins. The import
journal tracks created and dropped tables, records `triggers_created`, each
failed program, and `tables_lost_triggers` if an affected table's triggers could not
be restored. Replace remains non-atomic: the current table can be empty or lack
its triggers after an error. A target-only trigger on a replaced table disappears
with DROP TABLE; the plan lists those the dump will not recreate.

CREATE uses rebuilt, quoted names and validated timing/event, with no DEFINER or
FOLLOWS/PRECEDES. Each CREATE and its pre-change probe use a dedicated connection
with CLIENT_MULTI_STATEMENTS disabled and the source sql_mode, client charset and
connection collation. The importing account becomes the definer; changed definers
are a must-see plan line. A changed database collation is reported, never altered.
A dump contains executable trigger code and should come from a trusted source;
the single-statement boundary prevents appended SQL chains, not a malicious body.

Before replacement, owned table probes test every selected trigger's creation.
A same-named trigger on any other target table (including another dumped table) blocks with
`trigger_name_conflict`. A source-schema qualifier in a body blocks imports into
a different schema with `program_source_schema_reference`; qualifiers are not
rewritten. Preview checks TRIGGER grants, @@log_bin,
@@log_bin_trust_function_creators and SUPER. Error 1419 is reported separately as
`trigger_binlog_privilege`: set log_bin_trust_function_creators=1 or use an account
with SUPER before retrying.

Advanced **트리거 없이 테이블만 가져오기** is off by default and never persisted.
It sends `skip_triggers=true` to preview and import, skips only the dump triggers,
and reports every omitted trigger separately from restoration failures. Existing
triggers on replaced target tables are also removed. This toggle is disabled for
PostgreSQL targets and verify-in-copy mode. Cross-engine imports and table-subset imports
also report every skipped trigger. All three cases require seeing the plan even
when no target table is overwritten. Verify-in-copy (including scheduled
rehearsals) refuses a dump containing stored programs before making a copy with
`dump_programs_unsupported_in_copy`, including when skip_triggers is set. Existing
format 5/6 dumps without stored programs follow their previous paths.

### MySQL routines and events (format 7, full replace import)

Full MySQL exports capture PROCEDURE, FUNCTION and EVENT definitions via the named
SHOW CREATE columns, including sql_mode, client charset, connection/database
collation, definer, event time_zone and I_S.EVENTS.STATUS. Table-subset exports
include selected-table triggers and omit schema-wide routines, events and views;
the Export scope note explains this. Captured routines/events are recaptured at
the DDL drift check. Missing/NULL routine bodies, unproven headers and non-UTF-8
non-ASCII definitions are refused with per-object reasons. SHOW_ROUTINE or the
owning account is needed for unreadable bodies. A same-named procedure/function
pair is held only if both were captured. Headers require quoted backtick names.

After each table's data/FKs/triggers and row-count verification, full replace
imports create routines, then views (which may call functions), then events.
Each CREATE uses a timeout-tuned single-statement connection and the object's
source session context. Routine SQL SECURITY DEFINER/INVOKER is preserved;
DEFINER is omitted, so the importing account owns the new object. The plan names
changed definers as original -> importing account in a must-see line. Changes in
database collation and events using SYSTEM time_zone are reported. Objects absent
from the dump are left alone. Cross-engine and table-subset imports report every
skipped routine/event in a must-see plan line. The trigger-skip toggle does not
skip routines or events.

Routine probes run before existing tables change. A function rejected by binary
log checks (1418/1419) is skipped with routine_binlog_unsafe and a partial result;
its existing target function is kept. Other probe errors stop before replacement.
Probe cleanup failures name leftover objects and the needed ALTER ROUTINE grant;
no existing table is replaced after such a failure. Events are never probed.
Replacing a routine can remove its routine-level EXECUTE/ALTER ROUTINE grants
(mysql.procs_priv), even after previous-definition recovery. A must-see plan line
names the replaced routines; regrant application permissions after replacement.
This warning is also retained in the result. Database/global grants are separate.

Every object's metadata lookup/SHOW CREATE starts with SET NAMES utf8mb4 and
neutral sql_mode/time_zone. Source session settings are applied before DROP.
A failed SET leaves the target object intact. Preview reports a bad event time
zone as a named warning and continues inspecting other objects. Routine/event
phases and per-object progress are emitted to the UI.

Preview checks CREATE ROUTINE/ALTER ROUTINE/EVENT grants, reports possible
SYSTEM_USER definer replacement restrictions, and warns about binlog function
checks. Under binlog with trust=0, 1418 can affect SUPER accounts too; the plan
warns about function creation checks regardless of SUPER (1419 additionally
depends on privileges). Runtime probes remain authoritative.

Event headers always CREATE with DISABLE, including definitions captured as
DISABLE ON SLAVE (8.0) or DISABLE ON REPLICA (8.4). After creation, ALTER EVENT ENABLE runs only if a same-named
target event was ENABLED, or a new event was ENABLED at the source. A target's
DISABLED/SLAVESIDE_DISABLED/REPLICA_SIDE_DISABLED state stays disabled. Enabled events with past AT or
STARTS schedules are named and counted in a must-see plan line. Source-schema
body references block a different-schema import before DROP, including routines
and events; the importer never rewrites those references or enables such an event.

Before replacing a same-named routine/event, the importer saves its SHOW CREATE
and session context. If replacement CREATE fails, it attempts to recreate the
saved target definition on the same connection. The report includes
previous_definition, previous_restored and restore_error, plus the replacement
failure. Recovery first replays the target's exact status/DEFINER without ALTER
EVENT, which would transfer ownership. If specifying the old DEFINER fails with
1227, it retries without DEFINER while preserving SQL SECURITY and the target's
original status clause; previous_definer_preserved=false and a named warning
report that ownership changed. The plan warns about this risk. Known missing
CREATE privileges, unusable session settings, or a previous function that cannot
pass binlog checks leave the target object untouched rather than risk its loss. Data remains committed and status is partial. SHOW WARNINGS after every
CREATE is attached to the named object's report and surfaced in Import results.
A past one-shot event MySQL immediately removes (1588) is a CREATE failure:
it is never reported as imported, the result is partial, and the saved target
definition is recovered when available. Replace is
not atomic; previous-definition recovery can itself fail and is reported.
Verify-in-copy and scheduled rehearsals still refuse all stored programs before
creating a copy. Legacy format 5/6 and trigger-only format 7 paths stay supported.

### Object support table (Export)

Only the scope below is officially supported. MySQL triggers are preserved in
format 7, including full-export routines and events in MySQL replace mode. Live-verified on MySQL 8.0.46 / 8.4.11 and
PostgreSQL 13.23 / 18.4 (`live_mysql_programs`, `live_export_contract`,
`unsupported_objects_are_refused_before_any_file_and_opt_in_proceeds`): the
preserved core, triggers, routines, events, materialized views, MEMORY tables,
default refusal, opt-in and a strict export/import round trip. The lossy-definition
rows (unproven generated columns, CHECK, defaults, advanced indexes, cross-schema FKs)
come from the existing inspection and its earlier fixtures. Other versions are not
claimed.

| Object | MySQL | PostgreSQL |
| --- | --- | --- |
| Tables, rows, PK/UNIQUE/secondary indexes, supported FKs, comments, supported defaults | Preserved | Preserved |
| Views (full export only) | Preserved as separately captured definitions; persistent warning, `strict_export=false` | Same |
| INVISIBLE columns | Preserved with explicit read/load/INSERT column lists (format 6) | Non-MySQL targets: refused before DROP on import/migration |
| Generated columns | Proven MySQL expressions preserved (format 6); replace and safe restore recalculate values | Non-MySQL targets: refused before DROP on import/migration; PostgreSQL export remains refused |
| Unsafe CHECK, unsupported defaults, advanced indexes (functional/descending/FULLTEXT/SPATIAL/partial/non-btree), cross-schema FKs | Refused before export; **not waivable** | Same, plus custom types/ALWAYS identity/custom sequences |
| Triggers on an exported table | Preserved after body verification (format 7, replace mode) | Refused before export; waivable |
| Routines (functions/procedures), events | Preserved in full format 7 exports and full MySQL replace imports after header/context validation; failed capture is refused with a reason and is waivable | Refused before full export; waivable (no events) |
| Materialized views | n/a | Refused before full export; waivable |
| Non-InnoDB tables (MyISAM, MEMORY, ...) in the exported scope | Refused before export; waivable | n/a |

INVISIBLE columns (MySQL 8.0.23+) retain their visibility through format 6,
including invisible generated columns. Reads and loads name these columns
explicitly; `SELECT *` does not include them. Safe verification also compares
visibility. Dumps containing invisible columns require a MySQL target.

### Generated columns (MySQL -> MySQL)

Virtual and stored columns are captured only when the decoded generation expression
passes the expression boundary check and appears verbatim in that column's own
SHOW CREATE TABLE line. The manifest stores the decoded expression without the
outer AS parentheses; parentheses belonging to the expression remain. Unproven
columns remain refused (`generated_column_unproven`, not waivable). This includes
literal emoji when SHOW CREATE prints them as `????` (observed on 8.0/8.4).
PostgreSQL export and imports/migrations across engines refuse generated columns
before DROP.

Data chunks retain generated values. Import skips those fields in LOAD DATA and
INSERT, so MySQL recalculates them. Format 6 readers preserve the expression,
VIRTUAL/STORED kind, indexes and supported FKs. Dumps without generated/invisible
columns or stored programs retain the previous format versions. Formats 1-6 remain readable.

Before creating a MySQL dump directory, Export also checks that every table it
will export can generate same-engine import DDL. Failure refuses even an
incomplete opt-in with `table_definition_unrestorable` and a per-table reason.
ON UPDATE metadata captures only the timestamp function token (including its
precision); an INVISIBLE flag in EXTRA is kept separately.

Import uses strict SQL mode. Rows originally accepted under a loose mode may fail
when a STORED expression is recalculated (for example a truncated CAST). A replace
failure can occur after that table was dropped/recreated; use safe restore to keep
the original untouched. Safe verification compares the recalculated values with
the dump, so expression output differences between server versions stop the switch
with a content mismatch. Replace mode reports recalculation errors but does not
compare recalculated generated values with the values in the dump; safe mode does.

Default behavior: when the selected scope contains any refused object, `dump.run`
fails before the target directory or any dump file is created, with
`error_code: "unsupported_objects"`, `objects` (one `kind:table:name` entry per
object), `bypassable`, and a `reasons` map for unproven generated columns and
triggers. Only the
waivable objects can be omitted, and only by
the explicit choice "Export without these objects (incomplete)" (`allow_incomplete:
true` in the `dump.run` payload; the dialog words the objects, up to 10 inline plus
a count of the rest, before asking, or sends it from the start when its Advanced
setting "담을 수 없는 객체는 빼고 내보내기" is on; see Export dialog above). The structure and data of all remaining tables
are still exported; only the listed objects are left out. That choice
keeps `strict_export=false` and adds a persistent `Object not exported: ...`
warning per object. Only objects inside the export scope get that warning:
triggers of unselected tables and, for a table subset, routines, events and
materialized views do not mark the dump incomplete. Lossy table definitions
(`bypassable: false`) cannot be waived. Scheduled backups have no such prompt and therefore fail with the
refusal instead of silently producing an incomplete backup. Import behavior is
unchanged. Objects this inspection does not detect (for example MySQL
partitioning, PostgreSQL extensions, rules or privileges) are not reported.

MySQL lists triggers, routines, events, tables and columns only to accounts
allowed to see them, so a narrow account would get a dump that looks complete
while it lacks them (B1); a replace import of a table dumped without some columns
would recreate it without them. Before export the core reads the account's own
grants once (`SHOW GRANTS`): TRIGGER and SELECT for every export, and for a full
export also SHOW_ROUTINE, global SELECT, or EXECUTE / CREATE ROUTINE / ALTER
ROUTINE (routines), and EVENT. A full export needs TRIGGER and SELECT on the whole
database (the table list); a table subset may also hold them on each exported
table (`GRANT SELECT ON db.t`), but a column grant (`GRANT SELECT (id) ON db.t`)
never counts, since it shows only those columns. Anything not proven, grants
through roles, a partial revoke of the privilege on the export database (one on
another database, such as `mysql.*`, does not count), or a failed check sets
`strict_export=false` with the persistent warning `The export account's
privileges do not prove it sees every object, so some may be missing: ...`, which
Import reports as `visibility_unproven`. PostgreSQL is not probed yet.

The application shows each manifest warning of a finished export as its Korean
sentence (the same one Import uses for that code, with the omitted object's name
for `omitted_object`); the core's own text stays in the raw output and the log.

A successful table transfer does not certify complete database backup fidelity.
No new signing/notarization requirement or external database dump tool is
introduced.

See [verification evidence](export_import_verification_2026-09-28.md) for fixtures
and results. Keep a recoverable target backup before destructive replacement.
