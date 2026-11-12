Revised spec: TunnelForge Import/Export, one default behavior plus Advanced toggles

HEAD is b57a4ab. Nothing in the repo was changed. Before revising, I re-checked every blocker and major finding against the source, plus most of the minor ones.

**Changelog (review → spec)**
- **[B1, M-PR order] PR order changed.** The verify-in-copy stops (C6) and per-attempt reports (C7) now land before the Import UI PR. Without that, the toggle could rebuild linked target-only tables from SHOW CREATE (`safe_promote_mysql.rs:197-201`, `:1414-1440`), and later replace runs could overwrite the record of a kept copy (`dump_format.rs:602-606`).
- **[B2] C4 resolves the snapshot mode before using it.** The manifest's consistency fields (`dump.rs:764-767` → `adapters.rs:932-943`), the worker count (`dump.rs:930-936`) and the acquire call (`dump.rs:954-964`) all read the *requested* mode today. With `auto` that would write a false consistency claim into the dump.
- **[B3] The preview gets its own command, `dump.import_preview`, with a capability gate.** Routing goes by command name only (`protocol.rs:329`). `dump_import` ignores fields it does not know (`import.rs:188-192`). So a core built before the preview would run a real replace if it received `dry_run`.
- **[M] The 4-button review window is gone.** Unlinked target-only tables and views no longer count as blockers (`safe_restore.rs:320-325`). Anything still not clean ends in one sentence and a Close button. Deleted: the "검증된 새 대상 사용" path, `_confirm_promotion`, `_confirm_promotion_guard` (`db_import_dialog.py:1936-1990`). F4 is reworded.
- **[M] Three leftover safe-restore buttons are deleted** (`db_import_dialog.py:861-868`, `:889-891`). Backup management stays in the job list (`main_window.py:700-726`).
- **[M] Raw English core text no longer reaches users.** The preview and failures return structured codes, Python maps each to a Korean sentence, and the raw text sits behind "자세히". Core strings that mention modes which no longer exist are rewritten.
- **[M] Export scope radios are deleted.** The table list is always shown and starts fully checked; all checked means a full export. This differs from audit 6-2 item 1, so it is listed as F14.
- **[M] Missing FK parents are reported when the toggle is off**, and also written to the export metadata. They appear again in the Import preview as `external_parents`.
- **[M] Old records and the API can no longer bring the silent FK auto-add back.** `include_fk_parents` leaves `_RERUN_KEYS`. The API defaults change to False. Scheduled table backups now pass True explicitly, and whether they should is F13.
- **[M] Only the thread count is remembered.** One line next to the closed Advanced button lists every toggle that is on. A required name field moves onto the main target line.
- **[M] The timezone override is limited to legacy dumps and never remembered.** Current dumps are written in a UTC session (`dump.rs:62`, `:798`), so any override shifts TIMESTAMP values.
- **[M] The preview adds MySQL views that may break and PostgreSQL dependents that would stop the run.** The real run's pre-DROP check covers the same PostgreSQL dependents.
- **[M] Job labels are built from the result, not the toggles.** Every user-visible "안전 복원" string is listed for rewrite. Backup matching uses a marker.
- **Minor findings, all adopted:** the plan dialog's warning line and environment labels; deleting `_confirm_production_guard`; plain-Korean draft strings; migrating the folder-naming settings; FK parents disabled instead of re-checked; `import_mode` required; no Import success modal; C2 journals the creation honestly; snapshot errors 1203/1064/1235 move to the next method; the orphan-row warning keys on `snapshot_policy`; `added_fk_tables` is cross-checked against the dump's tables; PR1's fallthrough goes to `safe`; the PostgreSQL target line shows database plus schema; the failure-text fix happens at `rust_dump_exporter.py:648-653`.
- **Line fixes:** the table list's MultiSelection is at `db_export_dialog.py:520` (not 517), and "전환 완료" is `db_import_dialog.py:1897`.

**Status.** The redesign has not shipped. Steps 1-2 are released (v2.17.2, v2.17.3), and steps 3-7 are pending (`docs/current_status.md:2916`, `:3108-3113`). The labels below are working names (`tmp-debug/export_import_audit_20261013.md:214-247`); final Korean copy is F11.

---

## A. Import

**Default behavior.**
- Every table in the dump goes into the dump's original namespace, which is shown on screen and created if it is missing (C2).
- Each table is handled one at a time, mysqldump-style: DROP, CREATE from the dump, load, then that table's own indexes and FKs.
- Tables missing from the target are created. Tables not in the dump are left alone. The run stops at the first error.
- The target line differs by engine:
  - MySQL: `host:port / DB`.
  - PostgreSQL: `host:port / DB / schema`. When the connection has no database, the core falls back to `postgres` (`rust_dump_exporter.py:209-218`). The plan dialog then says so in a warning line (F16).
- The dialog sends `mode="replace"`.
- `import_mode` becomes required, with no default:
  - importer: keyword-only argument (`rust_dump_exporter.py:572`, `:753`)
  - worker: `self.kwargs['import_mode']` (`rust_dump_worker.py:182`)
  - The core's `"safe"` fallback stays, for old requests only (`import.rs:189-191`).
  - `restore_rehearsal.py:177` already passes `safe`.

**Advanced panel.**
- It reuses the "고급 설정 열기/닫기" pattern (`cross_engine_migration_dialog.py:421-423`, `:791-794`) and starts closed.
- Next to the button, one line lists every toggle that is on or forced, e.g. `켜진 고급 설정: 다른 DB 이름(abc)`.

| Working label | Default | Payload | Replaces |
|---|---|---|---|
| 다른 DB 이름으로 넣기 (PostgreSQL: 다른 스키마 이름으로 넣기) | off | **Off:** target = `dump_original_namespace` (`rust_dump_exporter.py:72-79`), shown read-only. **On:** the name goes through `target_schema` (MySQL → `target.database`, PostgreSQL → `target.schema`; `:209-218`, `:611`). | `chk_use_original` (583-586). The forced-off logic (1142-1150) is inverted: when the namespace is unknown or the dump comes from another engine, the name is forced and the editable field moves onto the **main target line**, not the hidden panel. |
| 먼저 복사본에서 검증하고 교체 | off | `mode="safe"` → plan → auto-promote when clean (`_take_auto_overwrite` 1919-1929) | `radio_safe`, `radio_overwrite`, "덮어쓰기 확인" (1230-1238), "안전 복원 검증 완료" (1742) |
| 타임존 직접 정하기 | off: use the dump's timezone | Enabled only when the manifest has no `source_timezone` (legacy dump; warning at `import.rs:127-129`). Choices: 한국 시간(+09:00) / 세계 표준시(UTC) send `timezone_sql` (330-346); 서버 기본값 sends `use_source_timezone=False` (1358). With a current dump the toggle is disabled with one line: "이 덤프는 시간대가 기록되어 있어 그대로 씁니다" (F15). | four `radio_tz_*` (619-630) |
| 동시 작업 수 | 8 (1-16) | `threads` | `spin_threads` (605-608) |

**Combinations.**
- All four toggles are independent and every combination is valid, so nothing greys out another.
- Name plus verify-in-copy means a safe restore against that name. If the name exists: copy, verify, swap. If it is missing: the core creates it directly and returns `completed_new_target` (`safe_restore.rs:347-351`, `:438-447`).

**Impossible cases come from the dump or the target, never from toggles.**
- **Cross-engine dump:** verify-in-copy is disabled, with one line saying why (`safe_restore.rs:220-222`).
- **Everything else:** preview blockers. The start button is disabled. When a blocker exists only because verify-in-copy is on (the C6 `copy_blockers`), the dialog adds "'복사본에서 검증'을 끄면 진행할 수 있습니다". Blockers include:
  - untranslatable tables
  - PostgreSQL dependents (views, target-only FK children, and any other `pg_depend` dependent; C3)
  - name and CHECK-name collisions
  - BIT columns in a format < 4 dump with verify-in-copy on
  - a missing CREATE DATABASE/SCHEMA privilege when the target is missing
  - a missing PostgreSQL parent in `external_parents`

**Removed:**
- **Radios and their group:** `radio_merge/replace/recreate/safe/overwrite` and `btn_import_mode` (644-703).
- **Mode code and pre-run confirmations:** the `_get_selected_import_mode` fallthrough (1152-1161); `_confirm_production_guard` (1174-1188) and its call (1219); "기존 대상 삭제 확인" (1221-1229); "덮어쓰기 확인" (1230-1238).
- **Safe-restore leftovers:** the review machinery `_review_promotion_plan`, `_confirm_promotion`, `_confirm_promotion_guard` and the `use_candidate` path (1936-1990); the buttons `btn_copy_restore_target`, `btn_review_restore`, `btn_manage_backups` with `copy_restore_target`, `review_restore_target`, `open_backup_lifecycle` (861-868, 889-891, 1813-1827). Backups are managed from the job list (`main_window.py:700-726`).
- **Styling and dead code:** the orange "고급:" style; `check_timezone_support` (1106-1118); `do_retry`/`retry_tables` (1195-1197, 1992-1997).
- **Core:** `recreate` stays as an alias; `merge` is rejected (C1).

**The single confirmation: `ImportPlanDialog(SchemaConfirmDialog)`** (`production_guard.py:51`), fed by C3.
- **Header and warning line:**
  - Environment names in Korean (운영/스테이징/개발/미분류), replacing `ENV_LABELS` (`production_guard.py:65-69`).
  - The inherited "되돌릴 수 없습니다" (`:108`) is overridden:
    - N>0, toggle off: "덮어쓴 테이블은 되돌릴 수 없습니다"
    - N=0: "새로 만들기만 합니다"
    - toggle on: "교체 전 테이블은 백업 DB에 남습니다"
- **Target line**, plus "(새로 만듭니다)" when `target.exists=false`.
- **Active toggles**, one line.
- **Counts with expandable name lists:** 덮어쓸 N / 새로 만들 M / 건드리지 않을 K.
- **Warnings, with codes mapped to Korean:**
  - triggers that will vanish, as `table.trigger`
  - TF-182 `may_not_replace` views
  - MySQL views that may stop working (C3)
  - `external_parents`: "부모 행과 맞는지 확인하지 않습니다"
  - a no-point-in-time warning only when `snapshot_policy` is `mysql_parallel_unsynchronized_snapshots` or legacy `mysql_parallel_no_backup_lock_consistent_snapshot` (`import.rs:118-123`); TF-187 wording is F3
  - the reasons for `strict_export=false`, listed separately
  - FK parents added at Export (`_export_metadata.json` `added_fk_tables`, `rust_dump_exporter.py:483`), shown only if they also appear in the dump's tables; the rest are listed as "코어가 제외함"
  - the legacy-dump timezone choice
  - the PostgreSQL `postgres` fallback
- **Behind "자세히":** kept target-only children `child.constraint → parent`, view replace/create counts, raw core detail.
- **Dropped:** the "부분 Import는 뷰를 건드리지 않음" line (it only applies with a `tables` filter, which the UI never sends: `import.rs:77`, `:1125`).
- **Blockers** disable execute.
- **Environments:** production requires typing the name; staging and unknown get Cancel as the default button; development gets no colored warning (F12).

**When dialogs appear.**
- The plan dialog appears exactly once per run, including "같은 설정으로 다시 Import" (797).
- Before it, only the input validation warnings appear (1201-1216).
- Success shows no modal. The status line and log carry one sentence per result:
  - `replaced` → "덤프 테이블 N개를 교체했습니다"
  - `completed_new_target` → "새 DB {name}을 만들어 넣었습니다"
  - `promoted` → "교체했습니다. 이전 테이블은 {backup_namespace}에 있습니다" (replaces the modal at 1751, the box at 1742 and the JSON at 1897)
- When verify-in-copy does not end clean, one stop-and-explain box appears with a Close button only: "복사본 {candidate}에 넣고 검증했지만 {reason} 때문에 교체하지 않았습니다. 원본은 그대로입니다. 복사본은 작업 목록 > 백업 관리에서 정리할 수 있습니다." F3 covers which reasons remain after C6.

**Failure windows.**
- With `error_count > 0`, show the first cause as a mapped sentence (1772-1781).
- Safe-restore failures arrive as `success:false` results, not errors (`safe_restore.rs:467-469`). So the pass-through fix goes in `rust_dump_exporter.py:648-653`: for `failed_original_untouched`, return the mapped `error_code`/`message` and `blockers`. The guard at `db_import_dialog.py:1665-1672` stays, for missing verification evidence only.
- C2 namespaces that were created but are now empty are named in the failure window.

**Remembered settings.**
- `_PERSISTED_IMPORT_OPTIONS = ("rust_dump_import_threads",)`, saved through `set_app_settings` (`config_manager.py:712`).
- Not persisted: name, name toggle, verify-in-copy (F5); timezone (it shifts data, see the changelog).

**Job and backup labels.** Built from the **result status**, not the toggles.
- Base: `Import · 덤프 테이블 교체`.
- Suffix ` · 복사본 검증` only when a copy or backup was actually made (promoted, stopped with a kept copy).
- Suffix ` · 새 DB 생성` for `completed_new_target`.
- Suffix ` · 대상 {name}` when the name toggle was on.
- `BACKUP_MODE_PREFIXES` (`job_list_dialog.py:54`) becomes `BACKUP_MODE_MARKERS = ("안전 복원", "덮어쓰기", "· 복사본 검증")`, matched with `in` (old labels start with the first two).

**User-visible "안전 복원" strings to rewrite:**
- `db_import_dialog.py`: 592, 1672, 1760-1770, 1817, 1827
- `rust_dump_exporter.py`: 653, 664
- `safe_restore.rs`: 248
- Kept only in `BACKUP_MODE_MARKERS`.

---

## B. Export

**Default behavior.**
- The table list is always shown and starts fully checked. All checked sends a full export. Unchecking any table sends a partial export, and one line under the list says "뷰·루틴·이벤트는 담지 않습니다".
- Deleted:
  - `radio_full`, `radio_partial`, `btn_type_group` (449-466)
  - the privilege dialogs (1186-1236) and the retry branch (1348-1362)
  - the "자동 지정"/"수동 지정:" radios, `_on_naming_mode_changed`, and the three auto-naming checkboxes (580-604, 860)
- The list becomes checkbox-only: `NoSelection` (520).
- A rerun with `scope=full` checks everything.
- The method used is shown as one line in the log and the completion window (audit 6-2 item 2), e.g. `같은 시점으로 내보냄 · 동시 작업 8개 (전체 잠금 권한이 없어 테이블만 잠깐 잠금)`. The wording does not promise DDL protection for table lock or single connection.
- Objects the dump cannot keep stop the export before anything is written.

**Snapshot order (MySQL; PostgreSQL unchanged).** Python sends `mysql_snapshot_mode="auto"`.
1. `LOCK INSTANCE FOR BACKUP`, then FTWRL → `parallel_strict` (B13).
2. If step 1 fails with a privilege error (1044/1045/1142/1227), or with 1064/1235 on `LOCK INSTANCE`, or with 1203: use `LOCK TABLES … READ` → `parallel_table_lock`. If the backup lock had succeeded and only FTWRL was denied, keep the backup lock.
3. If that fails: `single_connection`. With the toggle on: `parallel_unsynchronized`.
4. A lock timeout (1205, or the KILL at `dump.rs:91-110`) is F7. Until it is decided, the export fails with a readable sentence.
5. Workers = `max(1, min(threads, max_user_connections − own_sessions − 1))`. `own_sessions` is the account's own rows in `information_schema.PROCESSLIST`, which needs no PROCESS privilege. `# ponytail: user-name match, refine per host if it misfires`.

**Advanced panel:**

| Working label | Default | Payload |
|---|---|---|
| 연결된 부모 테이블 함께 담기 | **off** (today on, 526-527) | see below; ignored when all tables are checked |
| 시점 일관성 포기하고 빠르게 | off | `mysql_snapshot_fallback="parallel_unsynchronized"` (step 3 only; F7). Hidden for PostgreSQL. |
| 담을 수 없는 객체는 빼고 내보내기 | off | `allow_incomplete=true`. The prompt (1251-1283) appears only while this is off. Lossy refusals are never bypassable. |
| 동시 작업 수 / 압축 | 8 / 압축(zstd) | `threads` / `compression`. Labels are "압축 안 함 / 압축(zstd)" (545-547). |
| 폴더 이름 직접 정하기 | off: name + DB + time, fixed | Migration: saved `rust_dump_export_folder_mode='manual'` → toggle on with the saved name. The `use_*` keys are dropped (832-858). The changelog says auto-naming is now fixed. |

**FK parents.**
- Split `_resolve_required_tables_from_rust_schema` (`rust_dump_exporter.py:228-264`) into:
  - `fetch_fk_parents(schema)`: one cached `inspect_schema` call
  - pure `fk_parent_closure(picks, deps)`
- The closure runs on every `itemChanged`, whatever the toggle says.
- **Toggle off, parents missing:** one line under the list, e.g. `고른 테이블 2개가 목록에 없는 테이블을 참조합니다 (orders → customers)`. Nothing is auto-checked. `_write_metadata` records `fk_parents_outside_dump:[{child,constraint,parent}]` (470-483).
- **Toggle on:** parents are checked and **disabled**, with the tooltip "연결된 테이블이라 자동으로 담음" and a counter "자동 추가 N개". Item text stays unchanged (`get_selected_tables()` reads `item.text()`, 996-1000). Unchecking the child releases the parent (F6).
- **Run:** send exactly the checked list with `include_fk_parents=False`, plus `auto_added_tables` for the `added_fk_tables` metadata.
- **API defaults change to False:** `rust_dump_exporter.py:420`, `:729`; `rust_dump_worker.py:158`.
  - `backup_task_executor.py:112-117` passes `include_fk_parents=True` explicitly until F13 is answered.
  - `include_fk_parents` leaves `_RERUN_KEYS` (`job_history.py:60`). Old records' value is ignored (`db_export_dialog.py:942-943` is deleted).

**Readable refusals.**
- Add a pure `describe_export_refusal(objects) -> list[str]` that turns the core's `kind:table:name` codes (`schema.rs:437-503`, `:673-762`) into "테이블 {T}: {reason}".
- An unknown code becomes "테이블 {T}: 이 객체는 담을 수 없습니다", with the code behind "자세히".

| Kind | Bypassable | Codes |
|---|---|---|
| Can be left out | yes | `trigger`, `deprecated_engine`, `non_transactional_table`; for a full export also `routine`, `event`, `materialized_view` |
| Definitions that would be lost | no | `generated_column`, `check_constraint`, `unsupported_default`, `custom_type`, `unsupported_index`, `cross_schema_fk` (`dump.rs:813`) |

- Lossy refusals no longer land in the generic "Export 실패" box (1433). They get the same sentences, no proceed button, and "이 테이블을 빼면 진행할 수 있습니다" (one click: F8).
- Other plain strings: "Schema:" (474) becomes "DB:". "manifest" in `_INCOMPLETE_EXPORT_WARNING` (70-74) becomes "결과 경고". `_SNAPSHOT_LABELS` (`job_recording.py:30-35`) is rewritten in the same plain wording.

---

## C. Rust core changes

**C1. Remove merge (`import.rs`, `ddl.rs`).**
- Validate the mode in `dump_import` (188-192), before the report checkpoint at :212. Today an unknown mode fails at :261, after the report was written.
- Accept only `safe|replace|recreate`. `merge` gets a removal message and is never aliased to replace.
- Delete:
  - :376-378, :394-396, :439-443
  - `prepare_import_target` (701-722)
  - 1097-1114
  - the merge arm at :1768
- Simplify:
  - `with_post_load` is always true (:399)
  - the message at :404 loses its merge variant
  - `may_restart_import_table` (1348) becomes `attempts < 2`
  - `ddl.rs:284-290` becomes constant or is removed
- Keep `LiveAdapter::create_table` (`migrate.rs:1356`).

**C2. Create a missing target** (in `dump_import_attempt`, after chunk validation at :337 and before `LiveAdapter::connect` at :360).
- Open a maintenance connection as in `safe_restore.rs:258-263`; check SCHEMATA or `pg_namespace`.
- Rename `create_candidate` (500-530) to `pub(crate) create_namespace(..., context: &str)`. The "existing namespaces are never reused" text (`:520-522`) moves to the safe-restore caller.
- Journal order:
  - `namespace_creation_outcome_unknown=true` and `target_change{action:"create_database"}` before the CREATE
  - `namespace_created=true` only after it succeeds
- No auto-DROP on later failure. The result carries `namespace_created`, and the failure window names it.
- A missing PostgreSQL *database* still fails.

**C3. Preview: new command `dump.import_preview`** (`migration_core/src/import_preview.rs`).
- Routed at `protocol.rs:329` and listed in the `service.hello` capabilities (`protocol.rs:420+`; test at `:827`).
- The payload is the import payload **without `mode`**.
- It never:
  - writes a report or `journal.checkpoint`
  - runs `probe_import_ddl` (541)
  - runs `validate_dump_manifest_chunks*`
- Blockers come from these existing checks, returned as `{code, table?, detail}`:
  - `validate_import_metadata` (44)
  - `check_mysql_spatial_import`
  - the temporal check (extract 322-333)
  - `validate_postgres_drop_dependencies` (905), extended with a generic `pg_depend` scan for dependents outside the import set (owned sequences used by other tables' defaults, etc.). The real run's pre-DROP check uses the same extension.
  - `validate_target_check_names` (`schema.rs:185`)
  - `refuse_non_table_name_collisions` (874)
  - a missing CREATE DATABASE/SCHEMA privilege when the target is missing
- Collected (not raised):
  - replace vs create (`import_target_table_exists`, 859)
  - kept children (`surviving_foreign_keys`, 564)
- New read-only queries:
  - target base tables not in the dump
  - triggers on replaced tables
  - TF-182 views (`DEFINER ≠ CURRENT_USER()` without SYSTEM_USER; verify with the 182 account)
  - MySQL target-only views over replaced tables, from `information_schema.VIEW_TABLE_USAGE` (8.0.13+)
  - `external_parents` from the dump's own FK definitions, with `exists_in_target`
- When the namespace is missing it uses the C2 maintenance connection, and every table goes to `create`.
- Warnings are `{code, detail}`. The core maps its own manifest warning texts to codes (`unsynchronized_snapshot`, `views_outside_snapshot`, `omitted_object`, `visibility_unproven`); anything else becomes `source_dump_warning`.
- Result shape:
  ```
  {dry_run:true, target:{engine,database,schema,exists,can_create},
   replace:[], create:[], untouched:[], kept_children:[{table,constraint,parent}],
   external_parents:[{child,constraint,parent,exists_in_target}],
   views:{replace:[],create:[],may_not_replace:[{name,definer}],may_break:[{view,table}]},
   triggers_dropped:[{table,name}], blockers:[{code,table,detail}], warnings:[{code,detail}],
   strict_export:bool, snapshot_policy:str, source_timezone:str|null, copy_blockers:[]}
  ```
- **Core string rewrites** (no mode names left): `import.rs:907` loses "Use safe restore."; `safe_restore.rs:218` loses "or use the advanced replace import"; `safe_restore.rs:248` is reworded.

**C4. Automatic Export snapshot (`dump.rs`).**
- `parse_dump_run_options` (669-683) parses into `MysqlSnapshotRequest::{Fixed(MysqlSnapshotMode), Auto{fallback}}`. The field `mysql_snapshot_fallback` is `single_connection` (default) or `parallel_unsynchronized`. An absent field still means `Fixed(ParallelStrict)` (F13).
- `MysqlSnapshotMode` has no `Auto` variant, so `policy_label`/`forces_single_thread` can never see it.
- `MysqlSharedSnapshot::acquire` (135-213): backup lock first, FTWRL second. Reorder `mysql_snapshot_coordinator_sql` (68-75). Release the backup lock on every error path, except when a table-lock retry is going to keep it.
- `dump_run_with_refusal` (930-964):
  - The loop yields `(resolved: MysqlSnapshotMode, snapshot)`, computing each attempt's threads from the mode being tried.
  - `effective_threads`, both phase events, `finalize_dump_manifest(resolved.policy_label())` (764-767) and the result all use `resolved`.
  - Errors 1044/1045/1142/1227 (`dump.rs:112-122`) and 1203 advance the loop; 1064/1235 advance it only for `LOCK INSTANCE`.
  - The timeout branch (162-173) is F7.
- The clamp reads `@@session.max_user_connections` plus the account's own PROCESSLIST rows.
- Added to the result and the phase event: `mysql_snapshot_mode` (resolved), `worker_count`, `snapshot_fallbacks:[{mode,reason}]`.

**C5. Export visibility and warnings (`dump.rs`, `schema.rs`).**
- B1, before ~896: probe TRIGGER, SHOW_ROUTINE or global SELECT, EVENT, and schema SELECT. If any is unproven, or a probe fails, set `strict_export=false` and add a coded manifest warning (764-775). Verify live against the audit's 7 accounts.
- `export_warnings` (~900-903) is scoped through `scoped_omitted_objects`/`scoped_unrepresentable` (815-834).

**C6. Verify-in-copy stops before loading (`safe_restore.rs`, `safe_promotion.rs`).**
- Before `create_candidate`, check:
  - CREATE DATABASE, and the global SELECT/SHOW VIEW/PROCESS from `safe_promote_mysql.rs:126` (this also covers the late `blocked` at `safe_promotion.rs:261-270`)
  - routines and events
  - target triggers
  - target-only tables or views **linked** to dump tables by FK or view (rule 3, TF-STATUS-173)
- Any of these stops the run with a coded reason. Tables are never rebuilt from SHOW CREATE.
- The "absent from this dump" blockers (`safe_restore.rs:320-325`) are removed for **unlinked** target-only tables and views. Promotion already leaves those in place (only the `existing` closure enters `original_tables`, `safe_promote_mysql.rs:732-737`). So the rule-3 case reaches `ready_for_switch`.
- The same checks are exposed as the preview's `copy_blockers`.
- Cleanup covers `tf_promote_*`, `tf_backup_*`, `tf_p_*_oldv*` and failed `tf_restore_*`.

**C7. One report per attempt (B8).**
- `dump_import_report_path` (`dump_format.rs:602-606`) writes one file per attempt.
- `discover_dir` (`safe_backups.rs:82-111`) scans every report and plan directory.

---

## D. Python changes per file, and tests

**`src/ui/dialogs/db_import_dialog.py`:**
- Replace 579-703 with the target line (holding the name field when it is forced) and the Advanced panel with its summary line.
- Add `_import_request() -> dict`, used by `do_import`, the logs and the job label.
- `do_import`: validate → preview capability check → `RustDumpWorker("import_preview")` → `ImportPlanDialog` → import worker.
- Update `set_ui_enabled` (1083-1104).
- Persist `_PERSISTED_IMPORT_OPTIONS`.
- Status-line success; stop-and-explain box; first-cause failure.
- Delete everything in A's "Removed" list.

**Other files:**
- **`import_plan_dialog.py` (new).**
- **`rust_dump_worker.py`:** task `import_preview`; `import_mode` required; `include_fk_parents` default False.
- **`rust_dump_exporter.py`:**
  - `_import_payload()` (614-625) shared with a new `preview_import()`, which drops `mode`
  - `preview_import()` refuses unless the hello capabilities include `dump.import_preview` (same pattern as `:168-169`)
  - pass-through at 648-653
  - a code → Korean map for preview, import and safe errors (with the `_remember_error_code` codes)
  - `"auto"` in `MYSQL_SNAPSHOT_MODES` (31-37)
  - success suffix (347-361) built from `worker_count`/`snapshot_fallbacks`
  - FK resolver split; `fk_parents_outside_dump` and `auto_added_tables` in metadata
  - `describe_export_refusal`
  - defaults at 420/572/729/753
  - marker helpers (46-66) deleted once unused
- **`db_export_dialog.py`:** everything in B; delete `_create_mysql_privilege_dialog`, `_prompt_mysql_privilege_action`, `_retry_with_snapshot_mode`, `_MYSQL_PRIVILEGE_CHOICES`, `_MYSQL_RETRY_MESSAGES` (54-68, 1186-1249) and 1348-1362; `_build_worker` sends `auto` plus the fallback; `_snapshot_mode` comes from the result.
- **`job_recording.py`:** `_export_mode` from the result and the checked state (40); rerun `tables` holds only the user's own picks; Import label from the result status; `_SNAPSHOT_LABELS` rewritten.
- **`job_history.py:60`:** drop `include_fk_parents`.
- **`job_list_dialog.py:54-60`:** `BACKUP_MODE_MARKERS`, matched with `in`.
- **`backup_task_executor.py:112-117`:** pass `include_fk_parents=True`.
- **`production_guard.py`:** a Korean environment-label map for the plan dialog.
- **`legacy_translate.py`:** add every new string (`tests/test_i18n.py:255`); remove 176, 184, 211, 215, 803, 218-219, 224, 259-265.
- **Docs:** `export_import_policy.md` :32-38, :76-82, :189-198; `current_status.md` rows 179/185/187 and the order at :3108-3113, updated with this PR order.

**Tests to delete or rewrite.** Same as the previous spec, plus the following.
- `tests/test_db_import_dialog.py`:
  - :25 now expects only the plan dialog (no guard) for staging/unknown
  - :278 expects `replace`
  - delete :1566 and :1595
  - :1624 becomes `test_import_default_is_replace_and_has_no_mode_radios`
  - :1682 uses the toggle
  - :1706: a decline in the plan dialog starts no import worker
  - :1436 and :264 are rewritten
- `tests/test_dump_promotion_ui.py`:
  - :128 becomes `test_verify_in_copy_runs_safe_restore_after_one_confirmation`
  - :209 becomes `test_advanced_toggles_are_locked_while_running`
  - delete :178
  - every test that asserts the 4-button review becomes a stop-and-explain test
  - 147-206 set the toggle instead of a radio
- `tests/test_job_list.py`: :543 new label; :572 old and new markers; :133 label from the result.
- `tests/test_db_export_dialog.py`: delete :157, :178, :202, :222, :239, :265, :279, :296; keep :313 and :329, driven by the result; rewrite the scope-radio and rerun-FK tests.
- `tests/test_rust_dump_exporter.py`: delete :223-274 and :352; :725-734 loses `merge`.
- Rust:
  - delete `import.rs:2272` and `ddl.rs:2263-2274`
  - `live_import_policy.rs`: delete :287; drop merge from the loops at 183/204 and 251/273
  - `live_import_policy.rs` :81/:106 and :154/:173: a new over-type fixture, so the coercion guard (`import.rs:959`, `:2081`) keeps live coverage
  - `live_fault_injection.rs`: :410-419, :479-483, :513-521

**Tests to add.**
- Python, Import:
  - `test_import_toggle_payloads` (parametrized)
  - `test_import_plan_dialog_lists_replace_create_untouched_and_warnings`
  - `test_import_plan_dialog_production_requires_typed_name`
  - `test_import_plan_dialog_uses_korean_env_and_correct_warning_line`
  - `test_import_plan_blocker_disables_start`
  - `test_import_preview_failure_never_starts_import`
  - `test_preview_refused_without_core_capability_starts_no_worker`
  - `test_preview_payload_is_import_payload_without_mode`
  - `test_staging_shows_only_plan_dialog`
  - `test_import_failure_window_shows_first_cause`
  - `test_safe_restore_failure_passes_core_reason_through`
  - `test_verify_in_copy_not_clean_shows_stop_and_close_only`
  - `test_import_success_has_no_modal`
  - `test_only_threads_are_remembered`
  - `test_advanced_summary_lists_active_toggles`
  - `test_forced_name_field_is_on_main_line`
  - `test_timezone_toggle_disabled_for_dump_with_source_timezone`
  - `test_core_error_codes_map_to_korean`
  - `test_job_label_from_result_status`
- Python, Export:
  - `test_export_sends_auto_snapshot_mode`
  - `test_export_completion_names_the_snapshot_method`
  - `test_unsynchronized_toggle_sets_fallback`
  - `test_fk_parent_toggle_defaults_off`
  - `test_fk_toggle_off_shows_missing_parent_notice_and_checks_nothing`
  - `test_fk_toggle_on_disables_auto_added_parents`
  - `test_export_sends_exact_checked_list`
  - `test_rerun_ignores_old_include_fk_parents`
  - `test_all_checked_sends_full_scope`
  - `test_allow_incomplete_toggle_skips_prompt`
  - `test_refusal_text_names_table_and_reason`
  - `test_lossy_refusal_offers_no_proceed`
  - `test_table_list_is_checkbox_only`
  - `test_manual_folder_name_setting_migrates`
- Rust, Import:
  - `merge_mode_is_rejected_before_any_report_is_written`
  - `preview_is_a_separate_command_advertised_by_hello`
  - `preview_leaves_target_unchanged` (live MySQL+PG: checksums, no report, no `tf_probe_*`)
  - `preview_classifies_replace_create_untouched`
  - `preview_reports_triggers_on_replaced_tables`
  - `preview_flags_views_a_scoped_account_cannot_replace`
  - `preview_lists_mysql_views_over_replaced_tables`
  - `preview_and_replace_refuse_pg_non_view_dependents`
  - `preview_reports_external_parents`
  - `replace_into_missing_database_creates_it` (MySQL) and `…schema…` (PG)
  - `failed_create_is_journaled_as_outcome_unknown`
- Rust, Export:
  - `auto_snapshot_falls_back_to_table_lock_without_global_privileges`
  - `auto_snapshot_falls_back_to_single_connection_without_lock_tables`
  - `auto_snapshot_takes_backup_lock_before_ftwrl` (SQL order plus a live RELOAD-only account)
  - `auto_to_unsynchronized_writes_unsynchronized_policy_and_not_strict`
  - `auto_to_single_connection_uses_one_worker`
  - `manifest_never_records_auto`
  - `too_many_connections_advances_chain`
  - `auto_snapshot_clamps_workers_to_max_user_connections`
  - `export_without_trigger_privilege_is_not_strict`
  - `export_warnings_exclude_out_of_scope_objects`
- Rust, C6:
  - `unlinked_target_only_table_survives_unrebuilt` (audit :299)
  - `linked_target_only_table_stops_before_copy`
  - `unlinked_target_only_reaches_ready_for_switch`

---

## E. PR order

Each PR can merge on its own and updates `current_status.md` and the policy doc.

1. **Remove merge.** C1, plus `radio_merge` (666-673, 699, 1089, 1161, 1169). `_get_selected_import_mode` falls through to `"safe"`, with a test.
2. **Create a missing DB/schema on replace.** C2 and its live tests.
3. **Import preview.** C3 as `dump.import_preview`, plus `preview_import()`, the capability gate and the worker task. Not wired to the UI.
4. **Error codes and failure texts.** Core string rewrites, the code → Korean map, the first cause, the safe pass-through at `rust_dump_exporter.py:648-653`.
5. **Per-attempt reports.** C7, plus job records pointing at their own report.
6. **Verify-in-copy stop and cleanup.** C6, the removal of the unlinked target-only blocker, and `copy_blockers`. This changes today's safe radio, which only becomes stricter.
7. **Import default + toggles.** Section A. Depends on 1-6.
8. **Export auto snapshot in the core.** C4 and `"auto"` in the exporter. The dialog still sends strict. The timeout branch waits for F7.
9. **Export visibility (B1) and scoped warnings.** C5.
10. **Export dialog.** Section B. Depends on 8.

---

## F. Open questions for the user

1. Core `mode=merge` is rejected, never aliased (old Import records reopen a blank dialog, `main_window.py:736-737`). Confirm.
2. "다른 DB 이름으로 넣기" pointing at an existing DB follows the normal rule. Confirm.
3. After C6, which plan blockers can still stop a verify-in-copy run (orphan rows found in verification, concurrent changes), and what are the TF-187 wording and options?
4. With unlinked target-only tables no longer blocking, a partial dump goes through verify-in-copy unless its tables are linked to target-only tables, in which case it stops. Import's own `tables` filter refusal (`safe_restore.rs:203-208`) is unreachable from the new UI. Is stopping the linked case the right call?
5. Persistence: the spec keeps only threads. Should the name or verify-in-copy be remembered too? Not remembering the timezone departs from audit 6-1 item 5 ("고른 값은 다음에도 기억"), because it shifts data. Confirm.
6. Should the user be able to uncheck an auto-added parent? Interim behavior: disabled while a checked child needs it.
7. On a lock timeout (TF-185): retry, or drop to the next method? Should "시점 일관성 포기" apply only when the export would fall to one connection?
8. Should Export support the definitions it refuses today? Is "exclude this table" one click? Should `ON UPDATE`/CHECK be translated to PostgreSQL (status `:2912`)?
9. Should target triggers be recreated (mysqldump does) or only warned about?
10. PostgreSQL replace refuses on dependents, so a daily PostgreSQL refresh fails from its second run. What should happen?
11. Final Korean copy for every toggle, the confirmation, the refusals and the error-code sentences. The drafts above follow the plain-Korean rule.
12. Development has no guard today (`production_guard.py:9`, `:30`, `:384`). Does it need the plan dialog at all, or only when something would be overwritten?
13. Scheduled backups (`backup_task_executor.py:112-123`): should they use `"auto"`? Should scheduled table backups keep adding FK parents (now explicit True)?
14. The Export scope radios are replaced by "all checked = full DB", which departs from audit 6-2 item 1. Confirm.
15. The timezone override is offered only for legacy dumps with no recorded `source_timezone`. Is there a real need to override a current dump?
16. PostgreSQL connection with no database (falls back to `postgres`): warn (spec) or block?
---

## Decisions (user, 2026-10-14)

- F12 confirmation: show the pre-run list only when the run would overwrite at least one existing table; otherwise start directly.
- F5 persistence: remember every toggle state (threads and verify-in-copy); never the DB name or timezone.
- F14 export scope: one table list, all checked at first; all checked = full export.
- F7 lock timeout: fall to the next consistent method (table lock, then single connection); never fall automatically to the unsynchronized mode.
- Defaults taken by Claude for the rest: F1 reject `mode=merge` with a clear message; F2 normal rule; F4 stop the linked case; F6 auto-added parents disabled while needed; F13 schedules use `auto` and keep FK parents explicitly; F15 timezone override only for legacy dumps; F16 warn; F8-F10 deferred as separate issues.
