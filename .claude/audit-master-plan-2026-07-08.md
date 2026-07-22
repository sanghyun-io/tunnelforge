I’ll stay read-only and produce the implementation plan document only. I’m loading the planning instructions and the current status handoff first so the plan aligns with the repo’s orchestration rules.I’m using the `plan` and `writing-plans` skills. The user’s read-only constraint overrides the skill’s default “save plan to disk” behavior, so I’ll keep the deliverable entirely in the response.I’m checking the repository’s current file/test layout now. The audit gives enough findings to plan directly, but the work packages need realistic test ownership and no same-round source/test overlap.The status file confirms the current baseline is Rust Core ownership of DB operations, and it also shows prior cleanup has repeatedly removed Python mutation paths. I’m using that as a hard sequencing rule: contract/helpers first, UI consumers next, then dead-code deletion after live callers are detached.The Rust side currently derives column names from the first returned row, so 0-row result metadata cannot be supplied without changing the JSON result shape. I’ll make that a Round 1 contract WP and put all SQL-editor classification work behind it.I’ve got the key contracts and the current dead-code exports. I’m now structuring the plan by file ownership: one WP owns each large file or tightly-coupled module group, with later rounds reserved for deletions and consumers that depend on earlier contracts.## Cross-cutting Directives

Use these as binding contracts for every WP.

1. **Execution model**
   - Each WP runs in its own git worktree from `main`.
   - WPs inside the same round may run in parallel.
   - If an agent discovers it must touch a source/test file outside its WP list, it must stop and request rescheduling. Do not “just touch it”.
   - No version bumps in any PR.
   - No direct Python DB driver hot paths may be introduced. DB operations remain owned by `tunnelforge-core`.

2. **Regression gate for every WP**
   - `python -m pytest`
   - `python -m py_compile <all touched Python files>`
   - Rust-touching WPs additionally run:
     - `cargo test --manifest-path migration_core\Cargo.toml`
     - `cargo build --manifest-path migration_core\Cargo.toml --release`
   - Expected full-suite baseline: `1876 passed, 0 failed`.

3. **Unified query classification**
   - Create one canonical helper in `src/core/sql_query_classifier.py`.
   - All consumers must use it: `src/core/db_core_service.py`, `src/core/scheduler.py`, `src/ui/dialogs/sql_editor_dialog.py`, and any SQL worker code.
   - Semantics:
     - Strip leading whitespace, BOM, `-- ...`, `# ...`, `/* ... */`, and MySQL version comments before classification.
     - Preserve statement body for execution; classifier only inspects.
     - Recognize row-returning statements: `SELECT`, `WITH ... SELECT`, `SHOW`, `DESC`, `DESCRIBE`, `EXPLAIN`, `VALUES`, `TABLE`, and `CALL` as “may return rows”.
     - `0-row SELECT` is still row-returning.
     - CTEs must inspect the first top-level command after the CTE list. `WITH ... SELECT` returns rows; data-changing CTEs are classified as mutating and row-returning only if they have `RETURNING`.
     - Existing `LIMIT` detection uses `\bLIMIT\b`, not substring `" LIMIT "`.
     - DDL implicit-commit class for MySQL includes at minimum: `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `RENAME`, `CREATE INDEX`, `DROP INDEX`, `GRANT`, `REVOKE`, `LOCK TABLES`, `UNLOCK TABLES`, `ANALYZE TABLE`, `OPTIMIZE TABLE`, `REPAIR TABLE`.

4. **RustDbCursor.description contract**
   - Preferred fix: extend Rust JSONL `query.execute` result with `columns: []`.
   - `migration_core/src/lib.rs::QueryExecutionResult` becomes `{ rows, columns, rows_affected }`.
   - MySQL path must collect result-set column metadata before row conversion, even when zero rows are returned.
   - PostgreSQL path must prepare/query in a way that exposes statement columns independent of row count.
   - Streaming query mode must emit column metadata before row batches and include it in the final result.
   - Python `RustDbCursor.description`:
     - `None` means non-row-returning statement.
     - Non-`None` list means row-returning statement, including empty result sets.
   - Fallback if Rust metadata is not feasible in one PR: Python may set `description=[]` for classified row-returning statements, but all consumers must use `description is not None`, never truthiness.

5. **SQL editor MySQL DDL UX**
   - Picked design: DDL is allowed in transaction mode only with an explicit confirmation if pending DML exists.
   - Message must state that MySQL DDL causes implicit commit and previous pending changes cannot be rolled back.
   - On confirmed successful DDL:
     - Mark earlier pending DML as `auto_committed_by_ddl`.
     - Clear `pending_queries`.
     - Add the DDL as committed, not rollbackable.
     - Disable rollback for those entries.

6. **PostgreSQL aborted transaction UX**
   - Picked design: fail-fast rollback.
   - In transaction mode, on the first PostgreSQL statement error:
     - Immediately rollback the transaction.
     - Mark earlier pending entries `rolled_back_due_to_error`.
     - Clear `pending_queries`.
     - Disable commit until the user reruns statements.
   - Do not allow COMMIT on a known-aborted PostgreSQL transaction.

7. **Threading policy**
   - Ban new `QApplication.processEvents()` polling loops for DB work.
   - Replace existing DB-work polling loops with worker + signal patterns.
   - Ban `QThread.terminate()` for threads that may hold `DbCoreFacade` locks.
   - Cancellation semantics:
     - If Rust protocol supports cancel, request cancel and wait.
     - If no protocol cancel exists, UI must say “cannot interrupt this phase” and either disable cancel or detach safely while showing that work continues.
     - For long dump/import, use a dedicated Rust core process so cancellation can terminate that process without poisoning the shared facade.

8. **Dead-code removal list**
   - Remove or trim:
     - `src/core/connection_pool.py`
     - connection-pool settings tab in `src/ui/dialogs/settings.py`
     - `PreflightChecker` from `src/core/migration_preflight.py`; keep only small dataclasses if still needed.
     - `AutoRecommendationEngine` and `DEFAULT_RECOMMENDATION_RULES` from `src/core/migration_auto_recommend.py`
     - `MigrationStateTracker` persistence/resume machinery from `src/core/migration_state_tracker.py`; keep/move only `MigrationPhase` if still imported.
     - `TwoPassAnalyzer` and `EnhancedDumpFileAnalyzer` from `src/core/migration_analyzer.py`
     - dead non-dry-run rollback machinery in `src/core/migration_fix_wizard.py`
     - analysis half of `PostMigrationValidator`; extract live HTML/JSON report rendering to a connector-free module.
     - `ReportExporter` / `src/core/migration_report.py` unless a live UI caller is wired.
     - `SQLTransactionWorker` and `_is_modification_query` from `src/ui/dialogs/sql_editor_dialog.py`
     - `src/ui/workers/metadata_worker.py`
     - retired dry-run false branches in `src/ui/workers/migration_worker.py` and `src/ui/workers/fix_wizard_worker.py`
   - Remove matching `src/core/__init__.py` and `src/ui/workers/__init__.py` exports.
   - Delete or rewrite tests that only keep retired code alive.

---

## Round 1

### WP-1.1 db-core-jsonl-query-contract

**Branch:** `fix/audit-r1-db-core-jsonl-query-contract`  
**Size:** L

**Findings covered**
- `src/core/db_core_service.py:125`
- `src/core/db_core_service.py:188`
- `src/core/db_core_service.py:568`
- `src/core/db_core_service.py:661`
- `src/core/db_core_service.py:683`
- `src/core/db_core_service.py:711`
- `src/core/db_core_service.py:712`

**Files expected touched**
- `src/core/db_core_service.py`
- `src/core/sql_query_classifier.py`
- `migration_core/src/lib.rs`
- `migration_core/tests/jsonl_cli.rs`
- `tests/test_db_core_service.py`

**Guidance**
- Add the canonical query classifier and replace `query_returns_rows`.
- Extend Rust `QueryExecutionResult` with `columns`.
- Populate `RustDbCursor.description` from `columns`; use `None` only for non-row statements.
- Drain Rust core `stderr` continuously with a bounded tail.
- Protect `shutdown()` under `_lock`.
- Delete `bind_sql_params()` and `sql_literal()`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/db_core_service.py src/core/sql_query_classifier.py tests/test_db_core_service.py`
- `cargo test --manifest-path migration_core\Cargo.toml`
- `cargo build --manifest-path migration_core\Cargo.toml --release`

### WP-1.2 sql-statement-parser-delimiter

**Branch:** `fix/audit-r1-sql-statement-parser-delimiter`  
**Size:** S

**Findings covered**
- `src/core/sql_statement_parser.py:124`

**Files expected touched**
- `src/core/sql_statement_parser.py`
- `tests/test_sql_execution_worker.py`

**Guidance**
- When a custom delimiter is active, check delimiter match before dollar-quote detection.
- Restrict PostgreSQL dollar-quote scanning to default delimiter / PostgreSQL contexts.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/sql_statement_parser.py tests/test_sql_execution_worker.py`

### WP-1.3 config-manager-atomicity

**Branch:** `fix/audit-r1-config-manager-atomicity`  
**Size:** M

**Findings covered**
- `src/core/config_manager.py:103`
- `src/core/config_manager.py:181`
- `src/core/config_manager.py:364`

**Files expected touched**
- `src/core/config_manager.py`
- `tests/test_config_manager.py`

**Guidance**
- Add a process-wide lock around load/modify/save.
- Save through temp file + `os.replace`.
- Validate restore target before backup rotation can delete it.
- On load corruption, restore newest valid backup or surface error instead of silently returning empty config.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/config_manager.py tests/test_config_manager.py`

### WP-1.4 i18n-safe-widget-translation

**Branch:** `fix/audit-r1-i18n-safe-widget-translation`  
**Size:** M

**Findings covered**
- `src/core/i18n.py:1066`
- `src/core/i18n.py:1507`

**Files expected touched**
- `src/core/i18n.py`
- `tests/test_i18n.py`

**Guidance**
- Stop monkey-patching `QComboBox.addItem`, `insertItem`, and `addItems` for arbitrary strings.
- Deduplicate conflicting English phrase keys.
- Add tests proving schema/database names are preserved exactly.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/i18n.py tests/test_i18n.py`

### WP-1.5 migration-constants-policy

**Branch:** `fix/audit-r1-migration-constants-policy`  
**Size:** M

**Findings covered**
- `src/core/migration_constants.py:262`
- `src/core/migration_constants.py:372`
- `src/core/migration_constants.py:477`
- `src/core/migration_constants.py:547`

**Files expected touched**
- `src/core/migration_constants.py`
- `src/core/migration_rules/storage_rules.py`
- `src/core/migration_parsers.py`
- `tests/test_migration_constants.py`
- `tests/test_migration_rules.py`

**Guidance**
- Restrict identifier regex checks to identifier contexts, not raw dump text.
- Make storage engine rules consume `ENGINE_POLICIES`.
- Remove unused enum values and stale docstring claims.
- Deduplicate removed-function constants here so later rule WPs consume clean data.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/migration_constants.py src/core/migration_rules/storage_rules.py src/core/migration_parsers.py tests/test_migration_constants.py tests/test_migration_rules.py`

### WP-1.6 production-guard-message

**Branch:** `fix/audit-r1-production-guard-message`  
**Size:** S

**Findings covered**
- `src/core/production_guard.py:336`

**Files expected touched**
- `src/core/production_guard.py`
- `tests/test_production_guard.py`

**Guidance**
- Build staging message in a variable before optional details append.
- Add regression for empty `details`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/production_guard.py tests/test_production_guard.py`

---

## Round 2

### WP-2.1 sql-editor-execution-classification

**Branch:** `fix/audit-r2-sql-editor-execution-classification`  
**Size:** L

**Findings covered**
- `src/ui/dialogs/sql_editor_dialog.py:820`
- `src/ui/dialogs/sql_editor_dialog.py:880`
- `src/ui/dialogs/sql_editor_dialog.py:2319`
- `src/ui/dialogs/sql_editor_dialog.py:2495`
- `src/ui/dialogs/sql_editor_dialog.py:2520`
- `src/ui/dialogs/sql_editor_dialog.py:2592`
- `src/ui/dialogs/sql_editor_dialog.py:2808`
- `src/ui/dialogs/sql_editor_dialog.py:2832`
- `src/ui/dialogs/sql_editor_dialog.py:2954`
- `src/ui/dialogs/sql_editor_dialog.py:3092`
- `src/ui/dialogs/sql_editor_dialog.py:3112`
- `src/ui/dialogs/sql_editor_dialog.py:3281`
- `src/ui/dialogs/sql_editor_dialog.py:3637`
- `src/ui/dialogs/sql_editor_dialog.py:3759`
- `src/ui/dialogs/sql_editor_dialog.py:3811`
- `src/ui/dialogs/sql_editor_dialog.py:3822`

**Files expected touched**
- `src/ui/dialogs/sql_editor_dialog.py`
- `tests/test_sql_editor_dialog.py`

**Guidance**
- Depends on WP-1.1.
- Replace all local query type helpers with `src.core.sql_query_classifier`.
- Fix persistent connection database/schema mismatch by tracking connected schema and reconnecting on combo change with pending-change guard.
- Replace transaction-mode raw thread + `processEvents()` with `QThread` worker signals.
- Implement MySQL DDL implicit-commit UX from cross-cutting directive.
- Implement PostgreSQL fail-fast rollback from cross-cutting directive.
- Separate persistent transaction tunnel handle from per-autocommit temp tunnel.
- Keep cancelled validation/autocomplete/metadata workers referenced until `finished`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/sql_editor_dialog.py tests/test_sql_editor_dialog.py`

### WP-2.2 scheduler-core-execution

**Branch:** `fix/audit-r2-scheduler-core-execution`  
**Size:** L

**Findings covered**
- `src/core/scheduler.py:155`
- `src/core/scheduler.py:380`
- `src/core/scheduler.py:397` duplicate findings
- `src/core/scheduler.py:438`
- `src/core/scheduler.py:451`
- `src/core/scheduler.py:691`
- `src/core/scheduler.py:710`
- `src/core/scheduler.py:809`

**Files expected touched**
- `src/core/scheduler.py`
- `tests/test_scheduler.py`

**Guidance**
- Snapshot due schedules under lock; execute outside lock.
- Route `run_now` through the same serialized background execution path.
- Extract one `_resolve_connection(schedule)` for backup and SQL.
- Use decrypted credentials.
- Use canonical classifier / `cursor.description is not None` for scheduled SQL result output.
- Accept cron DOW `7` as Sunday.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/scheduler.py tests/test_scheduler.py`

### WP-2.3 migration-parser-analyzer-correctness

**Branch:** `fix/audit-r2-migration-parser-analyzer-correctness`  
**Size:** L

**Findings covered**
- `src/core/migration_analyzer.py:501`
- `src/core/migration_analyzer.py:563`
- `src/core/migration_analyzer.py:632`
- `src/core/migration_analyzer.py:1053`
- `src/core/migration_analyzer.py:1213`
- `src/core/migration_parsers.py:64`
- `src/core/migration_parsers.py:174`
- `src/core/migration_parsers.py:178`
- `src/core/migration_parsers.py:244`

**Files expected touched**
- `src/core/migration_analyzer.py`
- `src/core/migration_parsers.py`
- `tests/test_migration_analyzer.py`
- `tests/test_migration_parsers.py`

**Guidance**
- Exact unique index matching for FK validation; prefix coverage must not satisfy uniqueness.
- Parse table definitions with quote-aware comma/paren scanning.
- Skip `PRIMARY KEY`, `FOREIGN KEY`, and `CONSTRAINT` before applying index regex.
- Fix routine removed-function matching to `\bFUNC\s*\(`.
- Store cleanup target metadata instead of reparsing SQL text.
- Either wire `check_int_display_width` into live analysis or delete corresponding fix support consistently.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/migration_analyzer.py src/core/migration_parsers.py tests/test_migration_analyzer.py tests/test_migration_parsers.py`

### WP-2.4 migration-rule-correctness

**Branch:** `fix/audit-r2-migration-rule-correctness`  
**Size:** M

**Findings covered**
- `src/core/migration_rules/data_rules.py:79`
- `src/core/migration_rules/data_rules.py:175`
- `src/core/migration_rules/data_rules.py:489`
- `src/core/migration_rules/data_rules.py:700`
- `src/core/migration_rules/data_rules.py:766`
- `src/core/migration_rules/schema_rules.py:434`
- `src/core/migration_rules/schema_rules.py:574`

**Files expected touched**
- `src/core/migration_rules/data_rules.py`
- `src/core/migration_rules/schema_rules.py`
- `tests/test_migration_rules.py`

**Guidance**
- Reuse parser body extraction for enum checks.
- Downgrade unknown-column timestamp data findings or correlate against parsed TIMESTAMP columns.
- Fix generated-column function matching to call-boundary regex.
- On `mysql.user` permission failure, emit one info issue and skip definer comparison.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/migration_rules/data_rules.py src/core/migration_rules/schema_rules.py tests/test_migration_rules.py`

### WP-2.5 migration-fix-core-dryrun-sql

**Branch:** `fix/audit-r2-migration-fix-core-dryrun-sql`  
**Size:** L

**Findings covered**
- `src/core/migration_fix_wizard.py:251`
- `src/core/migration_fix_wizard.py:455`
- `src/core/migration_fix_wizard.py:1122`
- `src/core/migration_fix_wizard.py:1210`
- `src/core/migration_fix_wizard.py:1326`
- `src/core/migration_fix_wizard.py:1748`

**Files expected touched**
- `src/core/migration_fix_wizard.py`
- `tests/test_migration_fix_wizard.py`

**Guidance**
- Escape defaults using shared formatting helpers.
- Fix DECIMAL precision placeholder.
- Remove unreachable mutation/rollback/session guard machinery.
- Deduplicate batch steps by step identity or `(location, issue_type, strategy)`.
- Use dry-run FK summary text in dry-run results.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/migration_fix_wizard.py tests/test_migration_fix_wizard.py`

### WP-2.6 schema-diff-owner

**Branch:** `fix/audit-r2-schema-diff-owner`  
**Size:** M

**Findings covered**
- `src/core/schema_diff.py:92`
- `src/core/schema_diff.py:465`
- `src/core/schema_diff.py:1212`

**Files expected touched**
- `src/core/schema_diff.py`
- `tests/test_schema_diff.py`

**Guidance**
- Normalize away `DEFAULT_GENERATED`.
- Pick Python comparator as owner for now; remove or mark unused Rust facade method in a later Rust-contract WP only if needed.
- Generate composite PK order from index metadata, not column ordinal scan.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/schema_diff.py tests/test_schema_diff.py`

### WP-2.7 tunnel-monitor-health

**Branch:** `fix/audit-r2-tunnel-monitor-health`  
**Size:** M

**Findings covered**
- `src/core/tunnel_monitor.py:277`
- `src/core/tunnel_monitor.py:332`
- `src/core/tunnel_monitor.py:446`

**Files expected touched**
- `src/core/tunnel_monitor.py`
- `tests/test_tunnel_monitor.py`

**Guidance**
- Measure latency outside `_lock`.
- Use real config keys `db_user` and encrypted password, or delete health-connection machinery if still unreachable.
- Lock reconnect-state mutations.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/tunnel_monitor.py tests/test_tunnel_monitor.py`

### WP-2.8 rust-dump-facade-and-import-status

**Branch:** `fix/audit-r2-rust-dump-facade-and-import-status`  
**Size:** M

**Findings covered**
- `src/exporters/rust_dump_exporter.py:321`
- `src/exporters/rust_dump_exporter.py:347`
- `src/exporters/rust_dump_exporter.py:713`

**Files expected touched**
- `src/exporters/rust_dump_exporter.py`
- `tests/test_rust_dump_exporter.py`

**Guidance**
- Long dump/import must use dedicated `DbCoreFacade` / client process.
- On import exception, mark all non-done table results as `error`.
- Remove plaintext `get_uri()` and dead `ForeignKeyResolver.resolve_required_tables`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/exporters/rust_dump_exporter.py tests/test_rust_dump_exporter.py`

### WP-2.9 cross-engine-worker-stderr

**Branch:** `fix/audit-r2-cross-engine-worker-stderr`  
**Size:** S

**Findings covered**
- `src/ui/workers/cross_engine_migration_worker.py:60`

**Files expected touched**
- `src/ui/workers/cross_engine_migration_worker.py`
- `tests/test_cross_engine_migration_worker.py`

**Guidance**
- Drain helper `stderr` concurrently or redirect to temp file.
- Preserve redacted stderr tail for final error display.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/workers/cross_engine_migration_worker.py tests/test_cross_engine_migration_worker.py`

### WP-2.10 connector-facade-unification

**Branch:** `fix/audit-r2-connector-facade-unification`  
**Size:** M

**Findings covered**
- `src/core/db_connector.py:107`
- `src/core/db_connector.py:403`
- `src/core/db_core_service.py:388`
- `src/core/db_core_service.py:471`

**Files expected touched**
- `src/core/db_connector.py`
- `src/core/postgres_connector.py`
- `src/core/db_core_service.py`
- `tests/test_db_connector.py`
- `tests/test_db_core_service.py`

**Guidance**
- Use shared facade for connector instances unless a long-running dedicated process is explicitly required.
- Make temporary schema switches restore original DB or update connector state/cache keys.
- Log metadata exceptions; do not silently flatten auth/tunnel failures into empty results.
- Reduce duplicated metadata methods by delegating to one connector surface.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/db_connector.py src/core/postgres_connector.py src/core/db_core_service.py tests/test_db_connector.py tests/test_db_core_service.py`

### WP-2.11 sql-validator-cache-cte

**Branch:** `fix/audit-r2-sql-validator-cache-cte`  
**Size:** M

**Findings covered**
- `src/core/sql_validator.py:100`
- `src/core/sql_validator.py:270`
- `src/core/sql_validator.py:628`

**Files expected touched**
- `src/core/sql_validator.py`
- `tests/test_sql_validator.py`

**Guidance**
- Cache metadata by schema.
- Add explicit `set_metadata(schema, metadata)`.
- Recognize CTE names and derived aliases before table-existence checks.
- Extract shared alias parser for validator and completer.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/sql_validator.py tests/test_sql_validator.py`

---

## Round 3

### WP-3.1 dump-dialog-import-orphan-threading

**Branch:** `fix/audit-r3-dump-dialog-import-orphan-threading`  
**Size:** L

**Findings covered**
- `src/ui/dialogs/db_dialogs.py:1325`
- `src/ui/dialogs/db_dialogs.py:2287`
- `src/ui/dialogs/db_dialogs.py:2406`
- `src/ui/dialogs/db_dialogs.py:2528`
- `src/ui/dialogs/db_dialogs.py:2639`
- `src/ui/dialogs/db_dialogs.py:2958`
- `src/ui/dialogs/db_dialogs.py:3111`
- `src/ui/dialogs/db_dialogs.py:3124` duplicate findings
- `src/ui/dialogs/db_dialogs.py:3176`
- `src/ui/dialogs/db_dialogs.py:3252`
- `src/ui/dialogs/db_dialogs.py:3576`
- `src/ui/dialogs/db_dialogs.py:3578`
- `src/ui/dialogs/db_dialogs.py:3736`
- `src/ui/workers/rust_dump_worker.py:23`

**Files expected touched**
- `src/ui/dialogs/db_dialogs.py`
- `src/ui/workers/rust_dump_worker.py`
- `tests/test_db_dialogs.py`

**Guidance**
- Mirror export close guard in import dialog or implement real cancel.
- Add Rust dump/import cancel using dedicated process from WP-2.8.
- Sanitize raw import logs.
- Keep GitHub report workers referenced until finished.
- Move orphan analysis/export into a worker thread; no `processEvents()`.
- Disconnect orphan-check connector after dialog closes.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/db_dialogs.py src/ui/workers/rust_dump_worker.py tests/test_db_dialogs.py`

### WP-3.2 oneclick-dialog-contract

**Branch:** `fix/audit-r3-oneclick-dialog-contract`  
**Size:** L

**Findings covered**
- `src/ui/dialogs/oneclick_migration_dialog.py:53`
- `src/ui/dialogs/oneclick_migration_dialog.py:72`
- `src/ui/dialogs/oneclick_migration_dialog.py:255`
- `src/ui/dialogs/oneclick_migration_dialog.py:328`
- `src/ui/dialogs/oneclick_migration_dialog.py:983`
- `src/ui/dialogs/oneclick_migration_dialog.py:1021`

**Files expected touched**
- `src/ui/dialogs/oneclick_migration_dialog.py`
- `tests/test_oneclick_rust_core_gate.py`
- `tests/test_oneclick_readiness_docs.py`

**Guidance**
- Remove cancel during non-interruptible Rust run phase, or wire real Rust cancel before showing it.
- Never call `terminate()` on facade-blocked worker.
- Drive preflight rows dynamically from Rust event names.
- Either wire the execution-plan pause end-to-end or delete the disconnected gate/widget path. Preferred: delete retired gate unless Rust adds a pause point.
- Rename custom `finished` signal to avoid shadowing `QThread.finished`.
- Delete `_create_empty_report` and `_pre_issues`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/oneclick_migration_dialog.py tests/test_oneclick_rust_core_gate.py tests/test_oneclick_readiness_docs.py`

### WP-3.3 cross-engine-dialog-state

**Branch:** `fix/audit-r3-cross-engine-dialog-state`  
**Size:** M

**Findings covered**
- `src/ui/dialogs/cross_engine_migration_dialog.py:721`
- `src/ui/dialogs/cross_engine_migration_dialog.py:838`
- `src/ui/dialogs/cross_engine_migration_dialog.py:993`
- `src/ui/dialogs/cross_engine_migration_dialog.py:1055`
- `src/ui/dialogs/cross_engine_migration_dialog.py:1112`

**Files expected touched**
- `src/ui/dialogs/cross_engine_migration_dialog.py`
- `migration_core/src/lib.rs`
- `tests/test_cross_engine_migration_dialog.py`
- `tests/test_cross_engine_migration_protocol.py`

**Guidance**
- Add stable Rust issue code such as `target_not_empty`; UI must not substring-match English messages.
- Capture worker-start payload/state key once.
- Dispatch pending chained command from `_on_finished`, not `QTimer.singleShot`.
- Remove unreachable cleanup command/orphan buttons or fully wire them. Preferred: remove.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py tests/test_cross_engine_migration_protocol.py`
- `cargo test --manifest-path migration_core\Cargo.toml`

### WP-3.4 diff-dialog-lifecycle

**Branch:** `fix/audit-r3-diff-dialog-lifecycle`  
**Size:** M

**Findings covered**
- `src/ui/dialogs/diff_dialog.py:537`
- `src/ui/dialogs/diff_dialog.py:593`
- `src/ui/dialogs/diff_dialog.py:967`
- `src/ui/dialogs/diff_dialog.py:975`

**Files expected touched**
- `src/ui/dialogs/diff_dialog.py`
- `tests/test_diff_dialog.py`

**Guidance**
- Load schemas in worker thread.
- Disconnect previous connectors before a new compare.
- Capture compared schema names at compare start.
- On close, request cancellation/wait before disconnecting connectors.
- Rename custom compare signal if still shadowing `QThread.finished`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/diff_dialog.py tests/test_diff_dialog.py`

### WP-3.5 fix-wizard-ui-threading

**Branch:** `fix/audit-r3-fix-wizard-ui-threading`  
**Size:** M

**Findings covered**
- `src/ui/dialogs/fix_wizard_dialog.py:106`
- `src/ui/dialogs/fix_wizard_dialog.py:341`
- `src/ui/dialogs/fix_wizard_dialog.py:484`
- `src/ui/dialogs/fix_wizard_dialog.py:717`
- `src/ui/dialogs/fix_wizard_dialog.py:1013`

**Files expected touched**
- `src/ui/dialogs/fix_wizard_dialog.py`
- `src/ui/workers/fix_wizard_worker.py`
- `tests/test_fix_wizard_dialog.py`

**Guidance**
- Add cooperative cancel flag; remove `terminate()`.
- Clear charset state when charset issues are deselected.
- Move FK stats/preview DB queries off UI thread or cache once.
- Remove unreachable FK-cascade UI machinery.
- Make `isComplete` match visible checked rows only.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/fix_wizard_dialog.py src/ui/workers/fix_wizard_worker.py tests/test_fix_wizard_dialog.py`

### WP-3.6 migration-dialogs-rendering-threading

**Branch:** `fix/audit-r3-migration-dialogs-rendering-threading`  
**Size:** M

**Findings covered**
- `src/ui/dialogs/migration_dialogs.py:94`
- `src/ui/dialogs/migration_dialogs.py:792`
- `src/ui/dialogs/migration_dialogs.py:811`
- `src/ui/dialogs/migration_dialogs.py:954`
- `src/ui/dialogs/migration_dialogs.py:1495`

**Files expected touched**
- `src/ui/dialogs/migration_dialogs.py`
- `tests/test_migration_worker.py`

**Guidance**
- Remove `terminate()` fallback.
- Render FK tree from worker result, including cycle-only tables.
- Fix markdown conversion or render escaped plain text.
- Delete unreachable non-dry-run confirmation block.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/migration_dialogs.py tests/test_migration_worker.py`

### WP-3.7 schedule-dialog-ui

**Branch:** `fix/audit-r3-schedule-dialog-ui`  
**Size:** S

**Findings covered**
- `src/ui/dialogs/schedule_dialog.py:136`
- `src/ui/dialogs/schedule_dialog.py:880`

**Files expected touched**
- `src/ui/dialogs/schedule_dialog.py`
- `tests/test_scheduler.py`

**Guidance**
- Split SQL statements with shared parser before UPDATE danger checks.
- Either wire checkbox toggles to `scheduler.set_enabled()` or make column read-only. Preferred: wire it.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/schedule_dialog.py tests/test_scheduler.py`

### WP-3.8 main-window-state-thread-ui

**Branch:** `fix/audit-r3-main-window-state-thread-ui`  
**Size:** L

**Findings covered**
- `src/ui/main_window.py:394`
- `src/ui/main_window.py:451`
- `src/ui/main_window.py:818`
- `src/ui/main_window.py:966`
- `src/ui/main_window.py:999`
- `src/ui/main_window.py:1089`
- `src/ui/main_window.py:1181`

**Files expected touched**
- `src/ui/main_window.py`
- `src/ui/widgets/tunnel_tree.py`
- `tests/test_main_window_export_import_labels.py`
- `tests/test_tunnel_tree.py`

**Guidance**
- Route all auto-start paths through one wrapper that registers login-path state.
- Use existing connection-test worker/progress dialog for tree context test.
- Make config reload silent for CRUD/DnD paths.
- Marshal scheduler completion notification to UI thread.
- Update only affected tunnel row on monitor heartbeat.
- Connect `sectionResized`.
- Delete table-era `show_context_menu`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/main_window.py src/ui/widgets/tunnel_tree.py tests/test_main_window_export_import_labels.py tests/test_tunnel_tree.py`

### WP-3.9 tunnel-config-thread-refactor

**Branch:** `fix/audit-r3-tunnel-config-thread-refactor`  
**Size:** S

**Findings covered**
- `src/ui/dialogs/tunnel_config.py:400`
- `src/ui/dialogs/tunnel_config.py:433`

**Files expected touched**
- `src/ui/dialogs/tunnel_config.py`
- `src/ui/workers/test_worker.py`
- `tests/test_tunnel_config_dialog.py`
- `tests/test_connection_test_worker.py`

**Guidance**
- Store running worker on dialog.
- Block Esc/reject while test is running.
- Release worker reference only after built-in `QThread.finished`.
- Hoist duplicate temp credential manager.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/dialogs/tunnel_config.py src/ui/workers/test_worker.py tests/test_tunnel_config_dialog.py tests/test_connection_test_worker.py`

### WP-3.10 github-reporter-retry

**Branch:** `fix/audit-r3-github-reporter-retry`  
**Size:** S

**Findings covered**
- `src/core/github_issue_reporter.py:440`

**Files expected touched**
- `src/core/github_issue_reporter.py`
- `tests/test_github_issue_reporter.py`

**Guidance**
- Inspect returned status/message for auth failures and force one header refresh + retry.
- Do not rely on unreachable `RequestException`.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/github_issue_reporter.py tests/test_github_issue_reporter.py`

---

## Round 4

### WP-4.1 retired-migration-core-removal

**Branch:** `fix/audit-r4-retired-migration-core-removal`  
**Size:** L

**Findings covered**
- `src/core/migration_analyzer.py:1656`
- `src/core/migration_auto_recommend.py:254`
- `src/core/migration_auto_recommend.py:420`
- `src/core/migration_preflight.py:65`
- `src/core/migration_preflight.py:274`
- `src/core/migration_preflight.py:432`
- `src/core/migration_report.py:17`
- `src/core/migration_state_tracker.py:59`
- `src/core/migration_state_tracker.py:79`
- `src/core/migration_state_tracker.py:227`
- `src/core/migration_validator.py:79`
- `src/core/migration_validator.py:255`
- `src/core/migration_validator.py:482`

**Files expected touched**
- `src/core/__init__.py`
- `src/core/migration_analyzer.py`
- `src/core/migration_auto_recommend.py` delete
- `src/core/migration_preflight.py` trim/delete checker
- `src/core/migration_report.py` delete
- `src/core/migration_state_tracker.py` trim/delete tracker
- `src/core/migration_validator.py` trim/delete analysis half
- `src/core/migration_report_renderer.py` create if needed
- `src/ui/dialogs/oneclick_migration_dialog.py`
- affected tests:
  - `tests/test_migration_analyzer.py`
  - `tests/test_migration_auto_recommend.py` delete/rewrite
  - `tests/test_migration_mapping_coverage.py` rewrite
  - `tests/test_migration_preflight.py` delete/rewrite
  - `tests/test_migration_report.py` delete/rewrite
  - `tests/test_migration_state_tracker.py` delete/rewrite
  - `tests/test_oneclick_rust_core_gate.py`

**Guidance**
- Extract live report export into a connector-free renderer and escape all HTML fields/log lines.
- Remove retired exports from `src/core/__init__.py`.
- Delete tests whose only purpose is keeping retired code alive.
- Keep `MigrationPhase` only if still used; otherwise delete.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/__init__.py src/core/migration_analyzer.py src/core/migration_preflight.py src/core/migration_validator.py src/core/migration_report_renderer.py src/ui/dialogs/oneclick_migration_dialog.py`

### WP-4.2 connection-pool-settings-removal

**Branch:** `fix/audit-r4-connection-pool-settings-removal`  
**Size:** M

**Findings covered**
- `src/core/connection_pool.py:18`
- `src/core/connection_pool.py:102`
- `src/ui/dialogs/settings.py:161`
- `src/ui/dialogs/settings.py:260`

**Files expected touched**
- `src/core/connection_pool.py` delete
- `src/core/db_connector.py`
- `src/ui/dialogs/settings.py`
- `src/core/i18n.py`
- `tests/test_connection_pool.py` delete
- `tests/test_settings_update_actions.py`
- `tests/test_settings_update_launch.py`

**Guidance**
- Remove pool registry UI and connector imports.
- Fix theme preview/save/reject semantics while editing settings.
- Remove dead i18n keys for pool tab if unused.

**Verification**
- `python -m pytest`
- `python -m py_compile src/core/db_connector.py src/ui/dialogs/settings.py src/core/i18n.py tests/test_settings_update_actions.py tests/test_settings_update_launch.py`

### WP-4.3 retired-worker-cleanup

**Branch:** `fix/audit-r4-retired-worker-cleanup`  
**Size:** M

**Findings covered**
- `src/ui/workers/metadata_worker.py:12`
- `src/ui/workers/migration_worker.py:123`

**Files expected touched**
- `src/ui/workers/metadata_worker.py` delete
- `src/ui/workers/__init__.py`
- `src/ui/workers/migration_worker.py`
- `src/ui/workers/fix_wizard_worker.py`
- `tests/test_migration_worker.py`

**Guidance**
- Remove metadata worker exports.
- Remove dry-run false branches and misleading `[실행]` labels.
- Keep only dry-run/manual SQL worker semantics.

**Verification**
- `python -m pytest`
- `python -m py_compile src/ui/workers/__init__.py src/ui/workers/migration_worker.py src/ui/workers/fix_wizard_worker.py tests/test_migration_worker.py`

---

## Merge Order & Review Protocol

1. Merge all Round 1 WPs first.
   - Reviewer re-verifies:
     - `python -m pytest`
     - `cargo test --manifest-path migration_core\Cargo.toml`
     - SQL classifier tests for comments, CTEs, 0-row SELECT, DDL, SHOW/DESC/EXPLAIN.
     - `RustDbCursor.description is not None` for empty row-returning results.

2. Merge Round 2 WPs after rebasing on Round 1.
   - Recommended order:
     1. WP-2.10 connector unification
     2. WP-2.1 SQL editor
     3. Remaining independent core WPs
   - Reviewer re-verifies:
     - SQL editor wrong-schema regression.
     - MySQL DDL implicit-commit UX.
     - PostgreSQL aborted transaction rollback UX.
     - Scheduler no longer freezes UI via direct `run_now`.
     - Full `python -m pytest`.

3. Merge Round 3 WPs after rebasing on Round 2.
   - Recommended order:
     1. WP-3.1 dump/import dialogs
     2. WP-3.2 oneclick dialog
     3. WP-3.3 cross-engine dialog
     4. Remaining UI lifecycle WPs
   - Reviewer re-verifies:
     - No `QThread.terminate()` remains in changed dialogs.
     - Import dialog cannot close invisibly during active import.
     - OneClick cancel UX is truthful.
     - Main-window and scheduler callbacks are marshalled to UI thread.

4. Merge Round 4 WPs last.
   - Recommended order:
     1. WP-4.1 migration retired-code removal
     2. WP-4.2 connection-pool/settings removal
     3. WP-4.3 worker cleanup
   - Reviewer re-verifies:
     - No production imports of deleted modules.
     - `src/core/__init__.py` and `src/ui/workers/__init__.py` exports are clean.
     - Tests do not preserve retired subsystems just to keep coverage green.
     - Full `python -m pytest`.

5. `docs/current_status.md`
   - To avoid parallel merge conflicts, WP agents should not all edit it independently.
   - The round integrator updates `docs/current_status.md` once after each round merge with:
     - WP IDs merged.
     - Verification commands and results.
     - Remaining open audit WPs.
   - This preserves the repo handoff contract without making every parallel branch fight over the same file.

---

## Finding Coverage Matrix

| Finding | WP |
|---|---|
| `src/ui/dialogs/db_dialogs.py:3252` | WP-3.1 |
| `src/ui/dialogs/oneclick_migration_dialog.py:72` | WP-3.2 |
| `src/ui/dialogs/sql_editor_dialog.py:2319` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:2520` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:2954` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:3092` high | WP-2.1 |
| `src/core/config_manager.py:103` | WP-1.3 |
| `src/core/config_manager.py:181` | WP-1.3 |
| `src/core/config_manager.py:364` | WP-1.3 |
| `src/core/connection_pool.py:18` | WP-4.2 |
| `src/core/db_connector.py:107` | WP-2.10 |
| `src/core/db_connector.py:403` | WP-2.10 |
| `src/core/db_core_service.py:125` | WP-1.1 |
| `src/core/db_core_service.py:188` | WP-1.1 |
| `src/core/db_core_service.py:388` | WP-2.10 |
| `src/core/db_core_service.py:471` | WP-2.10 |
| `src/core/db_core_service.py:568` | WP-1.1 |
| `src/core/db_core_service.py:661` | WP-1.1 |
| `src/core/db_core_service.py:683` | WP-1.1 |
| `src/core/db_core_service.py:711` | WP-1.1 |
| `src/core/db_core_service.py:712` | WP-1.1 |
| `src/core/github_issue_reporter.py:440` | WP-3.10 |
| `src/core/i18n.py:1066` | WP-1.4 |
| `src/core/i18n.py:1507` | WP-1.4 |
| `src/core/migration_analyzer.py:501` | WP-2.3 |
| `src/core/migration_analyzer.py:563` | WP-2.3 |
| `src/core/migration_analyzer.py:632` | WP-2.3 |
| `src/core/migration_analyzer.py:1053` | WP-2.3 |
| `src/core/migration_analyzer.py:1213` | WP-2.3 |
| `src/core/migration_analyzer.py:1656` | WP-4.1 |
| `src/core/migration_auto_recommend.py:254` | WP-4.1 |
| `src/core/migration_auto_recommend.py:420` | WP-4.1 |
| `src/core/migration_constants.py:262` | WP-1.5 |
| `src/core/migration_constants.py:372` | WP-1.5 |
| `src/core/migration_constants.py:477` | WP-1.5 |
| `src/core/migration_constants.py:547` | WP-1.5 |
| `src/core/migration_fix_wizard.py:251` | WP-2.5 |
| `src/core/migration_fix_wizard.py:455` | WP-2.5 |
| `src/core/migration_fix_wizard.py:1122` | WP-2.5 |
| `src/core/migration_fix_wizard.py:1210` | WP-2.5 |
| `src/core/migration_fix_wizard.py:1326` | WP-2.5 |
| `src/core/migration_fix_wizard.py:1748` | WP-2.5 |
| `src/core/migration_parsers.py:64` | WP-2.3 |
| `src/core/migration_parsers.py:174` | WP-2.3 |
| `src/core/migration_parsers.py:178` | WP-2.3 |
| `src/core/migration_parsers.py:244` | WP-2.3 |
| `src/core/migration_preflight.py:65` | WP-4.1 |
| `src/core/migration_preflight.py:274` | WP-4.1 |
| `src/core/migration_preflight.py:432` | WP-4.1 |
| `src/core/migration_report.py:17` | WP-4.1 |
| `src/core/migration_rules/data_rules.py:79` | WP-2.4 |
| `src/core/migration_rules/data_rules.py:175` | WP-2.4 |
| `src/core/migration_rules/data_rules.py:489` | WP-2.4 |
| `src/core/migration_rules/data_rules.py:700` | WP-2.4 |
| `src/core/migration_rules/data_rules.py:766` | WP-2.4 |
| `src/core/migration_rules/schema_rules.py:434` | WP-2.4 |
| `src/core/migration_rules/schema_rules.py:574` | WP-2.4 |
| `src/core/migration_state_tracker.py:59` | WP-4.1 |
| `src/core/migration_state_tracker.py:79` | WP-4.1 |
| `src/core/migration_state_tracker.py:227` | WP-4.1 |
| `src/core/migration_validator.py:79` | WP-4.1 |
| `src/core/migration_validator.py:255` | WP-4.1 |
| `src/core/migration_validator.py:482` | WP-4.1 |
| `src/core/production_guard.py:336` | WP-1.6 |
| `src/core/scheduler.py:155` | WP-2.2 |
| `src/core/scheduler.py:380` | WP-2.2 |
| `src/core/scheduler.py:397` first | WP-2.2 |
| `src/core/scheduler.py:397` second | WP-2.2 |
| `src/core/scheduler.py:438` | WP-2.2 |
| `src/core/scheduler.py:451` | WP-2.2 |
| `src/core/scheduler.py:691` | WP-2.2 |
| `src/core/scheduler.py:710` | WP-2.2 |
| `src/core/scheduler.py:809` | WP-2.2 |
| `src/core/schema_diff.py:92` | WP-2.6 |
| `src/core/schema_diff.py:465` | WP-2.6 |
| `src/core/schema_diff.py:1212` | WP-2.6 |
| `src/core/sql_statement_parser.py:124` | WP-1.2 |
| `src/core/sql_validator.py:100` | WP-2.11 |
| `src/core/sql_validator.py:270` | WP-2.11 |
| `src/core/sql_validator.py:628` | WP-2.11 |
| `src/core/tunnel_monitor.py:277` | WP-2.7 |
| `src/core/tunnel_monitor.py:332` | WP-2.7 |
| `src/core/tunnel_monitor.py:446` | WP-2.7 |
| `src/exporters/rust_dump_exporter.py:321` | WP-2.8 |
| `src/exporters/rust_dump_exporter.py:347` | WP-2.8 |
| `src/exporters/rust_dump_exporter.py:713` | WP-2.8 |
| `src/ui/dialogs/cross_engine_migration_dialog.py:721` | WP-3.3 |
| `src/ui/dialogs/cross_engine_migration_dialog.py:838` | WP-3.3 |
| `src/ui/dialogs/cross_engine_migration_dialog.py:993` | WP-3.3 |
| `src/ui/dialogs/cross_engine_migration_dialog.py:1055` | WP-3.3 |
| `src/ui/dialogs/cross_engine_migration_dialog.py:1112` | WP-3.3 |
| `src/ui/dialogs/db_dialogs.py:1325` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:2287` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:2406` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:2528` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:2639` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:2958` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3111` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3124` first | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3124` second | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3176` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3576` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3578` | WP-3.1 |
| `src/ui/dialogs/db_dialogs.py:3736` | WP-3.1 |
| `src/ui/dialogs/diff_dialog.py:537` | WP-3.4 |
| `src/ui/dialogs/diff_dialog.py:593` | WP-3.4 |
| `src/ui/dialogs/diff_dialog.py:967` | WP-3.4 |
| `src/ui/dialogs/diff_dialog.py:975` | WP-3.4 |
| `src/ui/dialogs/fix_wizard_dialog.py:106` | WP-3.5 |
| `src/ui/dialogs/fix_wizard_dialog.py:341` | WP-3.5 |
| `src/ui/dialogs/fix_wizard_dialog.py:484` | WP-3.5 |
| `src/ui/dialogs/fix_wizard_dialog.py:717` | WP-3.5 |
| `src/ui/dialogs/fix_wizard_dialog.py:1013` | WP-3.5 |
| `src/ui/dialogs/migration_dialogs.py:94` | WP-3.6 |
| `src/ui/dialogs/migration_dialogs.py:792` | WP-3.6 |
| `src/ui/dialogs/migration_dialogs.py:811` | WP-3.6 |
| `src/ui/dialogs/migration_dialogs.py:954` | WP-3.6 |
| `src/ui/dialogs/migration_dialogs.py:1495` | WP-3.6 |
| `src/ui/dialogs/oneclick_migration_dialog.py:53` | WP-3.2 |
| `src/ui/dialogs/oneclick_migration_dialog.py:255` | WP-3.2 |
| `src/ui/dialogs/oneclick_migration_dialog.py:328` | WP-3.2 |
| `src/ui/dialogs/oneclick_migration_dialog.py:983` | WP-3.2 |
| `src/ui/dialogs/oneclick_migration_dialog.py:1021` | WP-3.2 |
| `src/ui/dialogs/schedule_dialog.py:136` | WP-3.7 |
| `src/ui/dialogs/schedule_dialog.py:880` | WP-3.7 |
| `src/ui/dialogs/settings.py:161` | WP-4.2 |
| `src/ui/dialogs/settings.py:260` | WP-4.2 |
| `src/ui/dialogs/sql_editor_dialog.py:2495` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:2808` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:2832` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:3092` medium duplicate | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:3112` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:3759` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:3811` | WP-2.1 |
| `src/ui/dialogs/sql_editor_dialog.py:3822` | WP-2.1 |
| `src/ui/dialogs/tunnel_config.py:400` | WP-3.9 |
| `src/ui/dialogs/tunnel_config.py:433` | WP-3.9 |
| `src/ui/main_window.py:394` | WP-3.8 |
| `src/ui/main_window.py:451` | WP-3.8 |
| `src/ui/main_window.py:818` | WP-3.8 |
| `src/ui/main_window.py:966` | WP-3.8 |
| `src/ui/main_window.py:999` | WP-3.8 |
| `src/ui/main_window.py:1089` | WP-3.8 |
| `src/ui/main_window.py:1181` | WP-3.8 |
| `src/ui/workers/cross_engine_migration_worker.py:60` | WP-2.9 |
| `src/ui/workers/metadata_worker.py:12` | WP-4.3 |
| `src/ui/workers/migration_worker.py:123` | WP-4.3 |
| `src/ui/workers/rust_dump_worker.py:23` | WP-3.1 |

No WONTFIX items.