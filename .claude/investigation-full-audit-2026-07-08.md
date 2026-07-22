# TunnelForge 전수조사 보고서 (2026-07-08)

- 조사 방식: 12개 영역 병렬 정밀 감사 + HIGH 발견사항 적대적 검증 (35 에이전트)
- 발견: 총 159건 → 확정 154건 (HIGH 6 / MED 92 / LOW 56), 기각 5건

## 영역별 총평

### sql-editor (15건)
The SQL editor is a 3,869-line god-file mixing widgets, background workers, transaction management, and ad-hoc SQL classification heuristics. The Rust-core boundary itself is respected (all DB I/O goes through the RustDbConnection/RustDbCursor facade shim), but read-vs-write query classification is implemented four different ways (description truthiness, query_returns_rows, startswith checks, _get_query_type) and they disagree on 0-row results, leading comments, CTEs, and DDL — misrouting results in the default transaction mode. The transaction lifecycle (pending_queries, cell edits, temp tunnels, one persistent connection pinned to the first-selected database) has multiple state-desync paths, and the synchronous _execute_sql loop pumping QApplication.processEvents() is the largest structural hazard: it lets commit/rollback/re-execution/close re-enter mid-query. A fully dead legacy worker (SQLTransactionWorker) still carries copies of the live bugs.

### db-dialogs (13건)
db_dialogs.py is a 3.7k-line god-file holding four dialogs plus a wizard, and its two big dialogs (RustDumpExportDialog / RustDumpImportDialog) are copy-paste twins that have visibly diverged: fixes landed in one copy only (close-guard while worker runs, raw-log credential sanitization, running-status log labels), which is where most real defects live. The Qt threading model is mostly sound (QThread workers + queued signals), but there are concrete lifecycle gaps: the import dialog can be closed mid-import leaving a hidden running import, the production guard is bypassed on the manual-connection path, and orphan analysis still runs blocking DB queries on the GUI thread with processEvents. The Rust-core architecture baseline is respected — connectors are facade shims over tunnelforge-core and dump/import goes through the JSONL commands; no direct Python DB-driver hot paths were found in this file.

### core-db (15건)
The Rust-core baseline is respected in this area — no direct Python DB-driver paths remain; everything routes through the tunnelforge-core JSONL facade. However, the compatibility shim layer around it carries real correctness debt: the shared SQL statement parser breaks on the most common MySQL `DELIMITER $$` pattern, there are two parallel connector stacks (RustDbConnector vs MySQLConnector) that duplicate metadata logic and each spawn private Rust subprocesses, and an entire connection-pool subsystem is dead in production while still surfaced in the settings UI. The JSONL client itself works but has hang/robustness gaps (undrained stderr pipe, no request timeout, unlocked shutdown race).

### migration-core-a (12건)
The migration analyzer/fix-wizard core is functionally solid for its current read-only role: the Rust-core mutation-ownership baseline is properly enforced (every mutation entry point raises RuntimeError, all DB access goes through the MySQLConnector shim), and the FK graph/topological-sort logic is correct. The main health problems are (1) a few genuine correctness bugs in generated fix SQL — the DECIMAL precision user input is silently ignored, column definitions with quoted defaults or expression defaults produce invalid DDL, and step de-duplication by location string can silently drop a selected fix from the batch results; (2) analyzer heuristics that produce false positives (substring function matching flags any routine touching a `password` column as using a removed function) or can never fire (TIMESTAMP 2038 scan); and (3) a substantial amount of retired execution/rollback machinery (non-dry-run branches, pre-state capture, recovery SQL, the entire TwoPassAnalyzer subsystem) that is unreachable in production but still documented and tested as if live, which is the biggest maintenance trap for anyone extending these files.

### migration-core-b (19건)
The MySQL 8.4 upgrade-checker layer (migration_parsers/constants/rules) is the weakest part of this area: multiple regex rules were verified empirically to flood false "error" findings on any ordinary dump (trailing-space/control-char identifier checks fire on the text BETWEEN identifiers), one live-DB check is broken by MySQL string-escape semantics, and the CREATE TABLE parser fabricates phantom indexes from PRIMARY KEY/FOREIGN KEY lines. schema_diff.py is structurally cleaner and its rename-detection is well done, but its sync-script generator emits invalid SQL for the very common DEFAULT CURRENT_TIMESTAMP case, and it duplicates a Rust-core `schema.diff` facade command that is never called. Live-DB rules do route through the connector shim (consistent with the Rust-core baseline), so the problems here are report correctness and rule-table drift rather than architecture violations.

### migration-core-c (12건)
This area is largely retired legacy from the pre-Rust-core architecture: the One-Click migration workflow is now fully owned by tunnelforge-core (facade.run_oneclick), yet PreflightChecker, AutoRecommendationEngine, MigrationStateTracker, ReportExporter, and the analysis half of PostMigrationValidator remain exported from src/core/__init__.py and kept green by dedicated test suites despite having zero production callers — the exact 'retired helper left behind' pattern CLAUDE.md forbids. Most critically for the audit brief, the resume machinery is never wired, so no migration state is ever persisted and cross-session resume cannot occur; the tracker also carries latent collision/cache-divergence bugs that would surface if it were wired. The genuinely live surface is small (MigrationPhase constants and the HTML/JSON report exporters) and its main defect is unescaped DB-derived strings in the HTML report. No Qt-threading hazards or direct Python DB-driver violations were found in these files — all SQL goes through the Rust-core connector shim.

### migration-ui-a (11건)
Both dialogs are functionally rich but carry post-refactor debris and several thread-lifecycle rough edges. cross_engine_migration_dialog.py is the healthier of the two: worker signal wiring is mostly correct and defensively written (wait-before-null, re-entrancy guards), but it rebuilds payloads from live, un-disabled UI inputs during runs, string-matches Rust core error messages for control flow, and retains dead command branches and orphan buttons from the pre-wizard UI. fix_wizard_dialog.py has a genuine wizard-navigation state bug that can present stale charset ALTER SQL to the user, plus ~250 lines of unreachable FK-cascade machinery left over from the CharsetFixPage refactor, and a close path that can terminate a thread while it holds the shared DbCoreFacade lock. Architecture-wise the legacy fix wizard correctly enforces dry-run-only (worker raises on dry_run=False), so the Rust-core ownership baseline is respected, though GUI-thread synchronous DB queries remain.

### migration-ui-b (15건)
The migration UI layer correctly delegates DB work to the Rust core via the connector shim (no direct-driver hot paths found), but the One-Click dialog carries significant dead scaffolding from the Rust-core migration: the plan-confirmation gate, preflight/analysis stack pages, and the threading.Event pause mechanism are all disconnected, and cancellation is UI-only while the Rust core keeps executing real DDL. Thread lifecycle handling is the weakest area across all three files — QThread.terminate() on workers blocked inside the facade's lock-holding request() can permanently deadlock the owning connector, and SchemaDiffDialog neither stops nor waits for its compare thread on close while yanking its connectors. diff_dialog additionally leaks per-connector Rust core subprocesses on repeated compares and blocks the GUI thread with synchronous connects. Several smaller correctness bugs (broken markdown-to-HTML conversion, stale preflight name mapping, unreachable confirmation block) round out a component that works on the happy path but degrades badly on cancel/close/re-run paths.

### main-ui (10건)
The main-ui layer respects the Rust-core baseline (all DB access observed goes through RustDbConnector/DbCoreFacade shims; no direct Python driver hot paths), and cross-thread monitor callbacks are correctly marshalled to the UI thread via QMetaObject queued invocation. The main problems are lifecycle/state-consistency debris from the QTableWidget-to-QTreeWidget migration (a lost sectionResized connection so column layout never persists, a dead table-era context-menu method that would crash if rewired, a legacy connection-pool settings tab that nothing populates anymore) plus one deterministic crash path in the tunnel-test dialogs (QThread garbage-collected when the progress dialog is closed with Esc mid-test). Additionally, the tunnel monitor triggers a full tree rebuild every 5 seconds per active tunnel, and several auto-start code paths bypass the start_tunnel wrapper, letting mysql login-path state diverge from actual tunnel state.

### infra-core (17건)
The passive infrastructure modules (platform_paths, resources, logger, single_instance, constants, platform_integration, update_checker/downloader) are clean and well-factored. The serious problems concentrate in two places: (1) src/core/scheduler.py, where the scheduled-task feature is substantially broken in practice — auto-starting a tunnel always throws TypeError, scheduled SQL tasks always use an empty password, execution runs under a long-held lock or directly on the Qt UI thread, and completion callbacks touch the tray icon from a worker thread; and (2) src/core/config_manager.py, where non-atomic writes, unsynchronized multi-thread read-modify-write, and a restore flow that can delete the backup being restored create genuine config-loss scenarios. tunnel_monitor's latency/health-check subsystem is dead code due to config-key mismatches (db_username vs db_user) layered on a no-op ping shim, meaning the monitoring UI never shows real DB health. No violations of the Rust-core architecture baseline were found in this area — all DB access goes through the Rust facade.

### export-workers (12건)
The export/import path itself honors the Rust-core baseline well: RustDumpExporter/Importer and all connector shims route through the tunnelforge-core JSONL facade, with no direct Python DB-driver hot paths found. The main weaknesses are (1) QThread lifecycle discipline — the dump worker has no cancellation at all, several dialogs drop or replace references to still-running QThreads (crash risk), and the scheduler is bridged synchronously into the GUI thread; (2) a broken status contract between RustDumpImporter and the import dialog that makes the whole "retry failed tables" feature unreachable; and (3) retired legacy left behind (an entire unused worker module, a duplicated FK-closure implementation kept only for tests). The schedule dialog additionally has two user-facing correctness bugs (dead enabled-checkbox, always-firing UPDATE danger warning).

### cross-cutting (8건)
Cross-cutting health is better than the exception counts suggest: layering is clean (src/core never imports src/ui), the Rust-core baseline holds (zero direct Python DB-driver imports, zero legacy dump-tool references, all DB access via the DbCoreFacade/RustDbConnection shim), there is no TODO/FIXME debt, and the i18n key dictionaries are internally consistent (ko/en symmetric, all 54 tr() keys present). The two systemic risks are (1) a lingering "block the main thread and pump QApplication.processEvents" pattern in dialogs instead of QThread workers, which creates re-entrancy windows during live DB transactions, and (2) the runtime i18n monkey-patch layer that rewrites arbitrary Korean strings flowing through Qt setters, including identity-bearing user data like schema names. One genuine cross-thread UI call exists on the scheduled-backup notification path.

## HIGH (6건)

### [db-dialogs] src/ui/dialogs/db_dialogs.py:3252 — concurrency ✅검증확정
- **요약**: RustDumpImportDialog.closeEvent accepts close unconditionally while the import worker is still running, unlike the export dialog which blocks close (line 1856-1868).
- **실패 시나리오**: User starts a large replace/recreate import, clicks '닫기' mid-run: the dialog closes but the RustDumpWorker QThread keeps importing invisibly (dialog stays alive as a child of the main window). The user, believing the import stopped, reopens the import wizard and starts a second import into the same schema -> two concurrent Rust import runs race on the same schema's DDL/data (corruption). If the app is quit while the hidden import runs, the QThread is destroyed while running -> Qt fatal abort and a half-written schema.
- **수정 힌트**: Mirror the export dialog: in closeEvent, if self.worker and self.worker.isRunning(), warn and event.ignore() (or implement a real cancel path via the Rust core).
- **검증**: Verified in src/ui/dialogs/db_dialogs.py: RustDumpImportDialog.closeEvent (3252-3255) unconditionally accepts close and disconnects the connector with no worker.isRunning() guard, while the export dialog's closeEvent (1856-1871) blocks close during a run — confirming the missing guard is an omission. The path is reachable: the 닫기 button (2303, wired to self.close at 2311) is NOT disabled by set_ui

### [migration-ui-b] src/ui/dialogs/oneclick_migration_dialog.py:72 — correctness ✅검증확정
- **요약**: cancel() never cancels the Rust core one-click workflow; it only suppresses UI events, so real DDL execution continues after the user cancels.
- **실패 시나리오**: User unchecks Dry-run, checks backup-confirmed, starts migration, then clicks 취소 and confirms. cancel() sets _is_cancelled, which makes _handle_core_event drop all further events, but run() is blocked inside connection.facade.run_oneclick() (DbCoreServiceClient.request has no cancellation path — it loops on stdout.readline until 'result'). The Rust core keeps ALTERing tables to InnoDB while the UI shows frozen progress; minutes later the finished signal fires and a success result screen appears, contradicting the cancel the user believes happened.
- **수정 힌트**: Either implement a real cancellation command in the JSONL protocol (e.g., oneclick.cancel forwarded to the core) or remove the cancel button for the run phase and state clearly that the workflow cannot be interrupted.
- **검증**: Verified end-to-end. cancel() (oneclick_migration_dialog.py:72-75) only sets _is_cancelled (read solely in _handle_core_event:111 to drop UI events) and sets _execution_gate, which is dead code — no .wait() exists anywhere and _on_start_execution_confirmed is never connected to a signal. run() (line 95) is blocked in DbCoreServiceClient.request (db_core_service.py:139-176), a lock-held loop on std

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2319 — correctness ✅검증확정
- **요약**: _ensure_connection reuses the persistent connection without checking that its database/schema still matches the current db_combo selection, so after switching databases in the combo all queries silently run against the previously selected database.
- **실패 시나리오**: User opens the editor with schema A selected, runs one query (connection created for A), then picks schema B in the combo. Metadata, schema tree and validation all switch to B, but `_ensure_connection` returns early because db_connection.open is True; `UPDATE users SET ...` executes against schema A — writes hit the wrong database while the UI claims B.
- **수정 힌트**: Store the database/schema used at connect time; on combo change either close/reconnect the persistent connection (warn if pending changes exist) or call connection.select_db().
- **검증**: Confirmed by direct code trace. _ensure_connection (sql_editor_dialog.py:2319) early-returns whenever self.db_connection.open is True; RustDbConnection.open (db_core_service.py:561-575) is a boolean only flipped by close(). The persistent connection captures db_combo.currentText() once at creation (lines 2336-2347). The combo's only signal handler _on_schema_changed (wired at 1716, defined at 3587

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2520 — correctness ✅검증확정
- **요약**: DDL statements (CREATE/DROP/ALTER/TRUNCATE) are funneled into the pending_queries rollback model, but MySQL DDL causes an implicit commit, so the tx panel promises a rollback that cannot happen and silently commits earlier pending DML.
- **실패 시나리오**: User runs `UPDATE t SET ...` (pending), then `CREATE INDEX ...`. MySQL implicitly commits the UPDATE and the DDL. The panel shows '미커밋 변경: 2건'; clicking '↩️ 롤백' reports '롤백 완료! (쿼리 2건 취소됨)' and marks history rolled_back, but both changes are permanently committed — the user believes destructive changes were undone.
- **수정 힌트**: Detect implicit-commit statement types for MySQL; on execution, flush/clear pending_queries with an explicit message ('DDL로 인해 이전 변경 자동 커밋됨') instead of adding them to the rollback list.
- **검증**: CONFIRMED — every link in the claimed failure chain traces on the default code path.

1) DDL enters the pending/rollback model: In `_execute_sql` (src/ui/dialogs/sql_editor_dialog.py:2405), transaction mode is the DEFAULT (`auto_commit_check.setChecked(False)`, line 1782). Any statement with `db_cursor.description is None` takes the "수정 쿼리" branch and is appended to `pending_queries` at line 2520 

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2954 — correctness ✅검증확정
- **요약**: _do_commit assumes COMMIT success means changes were applied; on PostgreSQL, any earlier failed statement aborts the transaction and COMMIT silently acts as ROLLBACK, yet the UI reports '커밋 완료' and history entries are marked 'committed'.
- **실패 시나리오**: PostgreSQL, transaction mode: statements 1-2 succeed (pending), statement 3 errors (caught at line 2531, loop continues), statements 4-5 fail with 'current transaction is aborted'. User clicks 커밋: PG accepts COMMIT on an aborted txn without error (it rolls back), commit() returns normally, message says '커밋 완료! (쿼리 2건 적용됨)' and history is marked committed — but nothing was applied. The generic except path at line 2975 has the mirror problem: after a failed commit it rolls back but leaves pending_queries intact, so a retry commits an empty transaction and again reports success.
- **수정 힌트**: Track statement errors within the transaction; for PG, after any error force rollback (or use per-statement savepoints) and require re-run before allowing commit; clear/refresh pending state in the commit-exception path.
- **검증**: Confirmed end-to-end. (1) src/ui/dialogs/sql_editor_dialog.py:2353 sets autocommit(False), which for PostgreSQL sends BEGIN (src/core/db_core_service.py:591-611), so the persistent session runs in an explicit transaction. (2) In _execute_sql, per-statement errors are caught at line 2531 and merely logged — no rollback, no aborted-txn flag, pending_queries (statements 1-2) untouched; on PG the serv

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:3092 — concurrency ✅검증확정
- **요약**: _execute_query_in_thread pumps QApplication.processEvents() in a loop while a background thread executes on the shared connection, allowing full re-entrancy: F5/Ctrl+Enter shortcuts, commit/rollback buttons, and dialog close all remain live mid-query.
- **실패 시나리오**: (a) During a long query, F5 fires execute_all_queries; the guard at line 2407 only checks self.worker (unused in transaction mode), so _execute_sql re-enters, runs a nested batch on the same connection, then _set_executing_state(False) re-enables buttons and kills the timer while the outer batch is still running. (b) btn_rollback stays enabled if pending_queries is non-empty, so the user can issue ROLLBACK between the outer loop's statements. (c) Closing the dialog inside processEvents runs _close_db_connection, sets db_connection=None, and the resumed outer loop then dies per-statement with AttributeError recorded as query errors after the dialog is gone.
- **수정 힌트**: Move transaction-mode execution into a QThread worker with signals (like the autocommit path) or at minimum add a re-entrancy flag checked by _execute_sql/_do_commit/_do_rollback/closeEvent and disable the shortcuts while executing.
- **검증**: Verified against src/ui/dialogs/sql_editor_dialog.py. Line 3091-3093 does pump bare QApplication.processEvents() (all events, input included) while a daemon thread executes on the shared connection. (a) CONFIRMED: F5/Ctrl+Return are standalone QShortcuts on the dialog (2050-2059), not tied to the buttons that _set_executing_state disables (3055-3056); the only re-entrancy guard (2407) checks self.

## MED (92건)

### [core-db] src/core/connection_pool.py:18 — dead-code
- **요약**: The entire connection-pool subsystem (ConnectionPool, ConnectionPoolRegistry, PooledConnection, PooledMySQLConnector, get_pooled_connector) is never instantiated by production code — only by tests — yet it is still surfaced as a Settings tab that can only ever show 'no active pools'.
- **실패 시나리오**: Grep shows PooledMySQLConnector/get_pooled_connector have zero production callers; settings.py only reads registry stats (always empty) and offers a 'close all pools' button that never has anything to close. ~500 lines of pool + registry + cleaner-thread code and its test suite are maintained for a feature no user path reaches, and the dormant code carries latent bugs (get_or_create_pool at line 347 keys pools without the password, so a pool created with a wrong password would be reused forever after the user corrects it; validation relies on the no-op ping below).
- **수정 힌트**: Either retire the module plus the Settings pool tab (consistent with the Rust-core baseline where pooling belongs in tunnelforge-core), or actually wire PooledMySQLConnector into the connectors that would benefit and fix the pool-key/ping issues first.

### [core-db] src/core/db_connector.py:107 — architecture
- **요약**: MySQLConnector (and PostgresConnector:17, ConnectionPool:57) construct a private DbCoreFacade() per instance instead of get_shared_db_core_facade(), so every connector instance lazily spawns its own long-lived tunnelforge-core subprocess that is never shut down.
- **실패 시나리오**: Each connection-test click (ConnectionTestWorkerAsync creates a MySQLConnector), each export/diff/migration dialog (db_dialogs.py:3306, diff_dialog.py:537/593/601 create up to 3 connectors), spawns another tunnelforge-core.exe on first request. disconnect() only closes the DB connection, never the client process; only the shared facade has an atexit shutdown. Over a long session dozens of Rust core processes accumulate (visible in Task Manager, each holding memory and pipes) until GC happens to close the Popen pipes or the app exits.
- **수정 힌트**: Use get_shared_db_core_facade() in MySQLConnector, PostgresConnector, and ConnectionPool (as RustDbConnector already does), or add explicit client.shutdown() to connector disposal.

### [core-db] src/core/db_connector.py:403 — correctness
- **요약**: get_create_table_statement/get_table_data/get_row_count call connection.select_db(schema) without updating self.database, permanently switching the connection's current DB while all cache keys and 'current database' logic still use the stale value.
- **실패 시나리오**: Connector opened on DB 'A'. Diff/migration code calls get_create_table_statement(table, schema='B') — the connection now sits on 'B' forever. A later get_tables() (schema=None) runs 'SHOW TABLES' against 'B' but returns it as A's table list AND caches it in the shared global cache under key 'host:port:tables:A'. All subsequent get_tables() calls for A (from any connector to the same host:port) return B's tables. get_table_columns() similarly resolves target_schema to the stale self.database.
- **수정 힌트**: Either restore the original database after the temporary switch, or update self.database when select_db is issued (as use_database() does), and key the metadata cache by the actually-selected schema.

### [core-db] src/core/db_core_service.py:125 — concurrency
- **요약**: The Rust core's stderr is a PIPE that is never drained during normal operation (only read once stdout hits EOF), so a chatty child can fill the ~64KB pipe buffer, block on stderr writes, and deadlock every DB operation.
- **실패 시나리오**: tunnelforge-core emits warnings/trace lines to stderr over a long session (e.g., during a large dump with many per-table warnings). Once the OS stderr pipe buffer fills, the Rust process blocks on write and stops producing stdout lines; DbCoreServiceClient.request() blocks forever in readline() at line 163 while holding self._lock, so every other DB call in the app queues behind it — a permanent hang with no timeout and no error.
- **수정 힌트**: Redirect stderr to a file / DEVNULL, or spawn a daemon drain thread per process that reads stderr continuously (keeping a bounded tail for error reporting). Consider a request timeout as well.

### [core-db] src/core/db_core_service.py:388 — duplication
- **요약**: RustDbConnector re-implements the same metadata surface as MySQLConnector (get_schemas, schema_exists, get_tables, get_db_version, get_column_names) with subtly divergent behavior, leaving two parallel connector stacks in live use.
- **실패 시나리오**: RustDbConnector auto-connects on demand and hardcodes its own system-schema exclusion set {'information_schema','performance_schema','mysql','sys'}, while MySQLConnector returns [] when not connected and uses the shared SYSTEM_SCHEMAS constant plus TTL caching. Both are used in production (RustDbConnector: main_window/scheduler/sql_editor/test_worker; MySQLConnector: dialogs/migration/exporter), so any fix to schema listing, version parsing, or column lookup must be made twice and the two paths already drift (e.g., cache behavior, NDBINFO exclusion, empty-vs-error semantics).
- **수정 힌트**: Collapse to a single connector implementation (RustDbConnector on the shared facade), have MySQLConnector delegate to it, and source system-schema exclusions from the one constants module.

### [core-db] src/core/db_core_service.py:711 — correctness
- **요약**: query_returns_rows() classifies by raw-prefix keyword only, so row-returning statements starting with comments ('-- x\nSELECT', '/*hint*/ SELECT'), parentheses, or non-listed verbs (CALL, VALUES, TABLE) are misclassified as DML, corrupting RustDbCursor.rowcount and downstream branching.
- **실패 시나리오**: In the SQL editor, a comment-prefixed SELECT fails the query_returns_rows() check at sql_editor_dialog.py:808, falls to the cursor path, and its rowcount is set to rows_affected (0 for SELECT) instead of the fetched row count; with zero result rows description stays None so the editor takes the DML branch — it issues an unintended COMMIT and reports '0 rows affected' instead of rendering an empty result grid with column headers.
- **수정 힌트**: Strip leading comments (and an optional '(' ) before the prefix check, and add CALL/VALUES/TABLE; or better, classify from the Rust core's response (presence of a result set) rather than re-parsing SQL in Python.

### [core-db] src/core/sql_statement_parser.py:124 — correctness ✅검증확정
- **요약**: Dollar-quote detection runs before custom-delimiter matching, so scripts using 'DELIMITER $$' are never split: the terminating $$ is consumed as a dollar-quote opener and the rest of the script is swallowed into one statement.
- **실패 시나리오**: User runs the canonical MySQL script 'DELIMITER $$\nCREATE PROCEDURE p() BEGIN ... END$$\nDELIMITER ;\nSELECT 1;' via the SQL editor (sql_editor_dialog.py:2791), test_worker, or scheduler. At 'END$$', read_dollar_quote() at line 124 matches '$$' (empty tag is valid) and enters dollar-quote mode before the delimiter check at line 155 ever runs; everything up to the next '$$' (or EOF) is treated as quoted text. The whole remainder — including 'DELIMITER ;' and following statements — is flushed as a single garbled statement, which the server rejects (or executes wrongly).
- **수정 힌트**: Check `sql_text.startswith(delimiter, i)` BEFORE read_dollar_quote() whenever a non-default delimiter is active (or at least when the delimiter starts with '$'). Dollar-quote scanning should only apply under the default ';' delimiter / PostgreSQL context.
- **검증**: Confirmed by direct code trace and live reproduction. In src/core/sql_statement_parser.py, read_dollar_quote() at line 124 runs before the custom-delimiter check at line 155, and read_dollar_quote (lines 167-182) accepts an empty tag, so '$$' always matches as a dollar-quote opener. Consequently, after 'DELIMITER $$' (explicitly supported at lines 81-84 sets delimiter='$$'), the delimiter can neve

### [core-db] src/core/sql_validator.py:100 — correctness
- **요약**: SchemaMetadataProvider.get_metadata(schema) caches a single _metadata object with no schema in the cache key, so a call for schema B silently returns schema A's cached tables/columns; it is also racy when ValidationWorker and MetadataLoadWorker run concurrently.
- **실패 시나리오**: validator.validate(sql, 'db2') after any prior get_metadata('db1') returns db1's table set — every table in the query gets a false 'does not exist' ERROR. The primary consumer (sql_editor_dialog) papers over this by calling invalidate() on schema switch and poking provider._metadata directly (line 3671), but any validate/get_completions call arriving between invalidate() and load completion triggers a synchronous rebuild on the ValidationWorker QThread using the same MySQLConnector the MetadataLoadWorker is concurrently using.
- **수정 힌트**: Key the cache by schema (dict schema->SchemaMetadata), and give the provider an explicit set_metadata(schema, metadata) API instead of external _metadata assignment.

### [core-db] src/core/sql_validator.py:270 — correctness
- **요약**: SQLValidator has no awareness of CTE names (WITH ... AS) or temporary tables, so valid queries like 'WITH cte AS (...) SELECT * FROM cte' get a red ERROR underline claiming the table does not exist.
- **실패 시나리오**: User types a perfectly valid CTE query in the SQL editor; _validate_tables matches 'FROM cte', metadata.has_table('cte') is False, and an IssueSeverity.ERROR is emitted — the editor shows a false 'table does not exist' error on correct SQL, training users to ignore the validator.
- **수정 힌트**: Pre-scan the SQL for 'WITH <name> AS' / ', <name> AS (' names and exclude them (plus derived-table aliases) from table-existence validation, or downgrade unknown-name findings to WARNING when a WITH clause is present.

### [cross-cutting] src/core/i18n.py:1507 — correctness
- **요약**: The i18n monkey-patch translates ALL string args of QComboBox.addItem/insertItem, corrupting identity-bearing user data (schema/database names) that code later reads back via currentText() and sends to Rust core.
- **실패 시나리오**: English UI mode (current_language()=='en') with a Hangul-named schema, e.g. a MySQL database named '백업' or '로그': db_dialogs.py:1265 combo_schema.addItem(schema) passes through translate_text, and the word table (i18n.py:1232, '백업': 'Backup') rewrites the item text; do_export then reads combo_schema.currentText() (db_dialogs.py:1324) and asks Rust core to dump schema 'Backup', which does not exist -> export fails or targets the wrong schema. Same pattern for import target schema (db_dialogs.py:2357 -> 2594), migration analyzer (migration_dialogs.py:525 -> 565), and schema diff (diff_dialog.py:550 -> 566). Tunnel combos are safe only because they use addItem(text, id) + currentData.
- **수정 힌트**: Do not patch QComboBox.addItem/insertItem/addItems (data-bearing widgets); translate only known chrome strings there, or have the dialogs store the raw identifier in item userData and read currentData() instead of currentText().

### [cross-cutting] src/ui/dialogs/db_dialogs.py:3124 — duplication
- **요약**: RustDumpImportDialog defines _get_import_mode_text twice; the second definition silently shadows the first, whose divergent (i18n-covered) UI labels are dead code.
- **실패 시나리오**: Both call sites (line 2610 confirmation dialog, line 3102 GitHub issue context) resolve to the line-3124 version, so the line-2572 version returning '전체 교체 Import'/'완전 재생성 Import'/'증분 Import (병합)' — the exact strings registered in the i18n dictionaries — is unreachable. A maintainer editing the 2572 copy sees no effect (classic shadowed-def trap), and in English mode the surviving 3124 strings ('merge (기존 데이터 유지)' etc.) are not whole-string dictionary entries, so they get mangled by word-level substitution in the confirmation dialog.
- **수정 힌트**: Delete one definition (keep a single source of truth for mode text); also remove the duplicated self.radio_recreate.setEnabled(enabled) at lines 2527-2528 while there.

### [cross-cutting] src/ui/dialogs/db_dialogs.py:3576 — concurrency
- **요약**: Schema-wide orphan-record analysis and report export run synchronously on the main thread, pumping QApplication.processEvents() from the progress callback (re-entrancy window; 10 processEvents sites exist across src/ui).
- **실패 시나리오**: User starts orphan analysis on a large schema: ForeignKeyResolver.find_orphan_records (line 3578) executes many queries on the GUI thread; each progress_cb pumps the event loop (3576), during which only analyze_btn is disabled (3564) — the user can close the dialog (destroying self.progress_label used by the still-running callback -> RuntimeError: wrapped C/C++ object deleted) or trigger the report export (3718-3724) which starts a SECOND resolver run re-entrantly over the same connector. Related single-shot freeze sites: main_window.py:449/482 (SSH/DB test blocks the GUI for the full connect timeout), db_dialogs.py:2434 (dump-folder compatibility scan), sql_editor_dialog.py:2261 (database list fetch).
- **수정 힌트**: Move resolver runs into a QThread worker emitting progress signals (pattern already exists in rust_dump_worker.py / metadata_worker.py); remove the processEvents pumps.

### [cross-cutting] src/ui/dialogs/sql_editor_dialog.py:3092 — concurrency
- **요약**: Transaction-mode query execution runs a raw thread per query and busy-waits with QApplication.processEvents(), leaving commit/rollback/close re-entrant against the same live DB connection.
- **실패 시나리오**: User runs a long DML in transaction mode (auto-commit off) with a prior pending change: _set_executing_state (line 3053) disables only the two execute buttons, so btn_commit/btn_rollback stay enabled (enabled at 2753-2754 whenever pending queries exist). While _execute_query_in_thread spins processEvents (3091-3093), the user clicks Rollback: _do_rollback runs re-entrantly, blocks on the facade lock held by the worker, and rolls back AFTER the in-flight DML completes; control then returns to the loop at 2508-2526 which appends the already-rolled-back query to pending_queries as an uncommitted change. UI now claims '미커밋 변경: 1건'; the user commits an empty transaction believing the DML applied — silent data loss from the user's perspective. Closing the dialog mid-run similarly tears down db_connection (closeEvent -> _cleanup at 3043-3050) while the worker thread still uses the cursor.
- **수정 힌트**: Move transaction-mode execution into a proper QThread worker with signals (the auto-commit path already uses one), and disable commit/rollback/close while a query is executing.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:1325 — correctness
- **요약**: do_export reads the output folder from the read-only preview field, which is only regenerated on schema/naming changes — so consecutive exports in one dialog session reuse the identical timestamped folder and overwrite/merge dumps.
- **실패 시나리오**: User exports partial tables A (folder ...\conn_schema_20260708_101500), then changes the table selection to set B (table selection does NOT trigger _update_output_dir_preview) and clicks Export again: the exporter os.makedirs(exist_ok=True) into the same folder, overwriting run-A files and leaving stale run-A table files mixed with run-B metadata. A later import of that folder restores a mixture of two different exports.
- **수정 힌트**: Regenerate the output dir (fresh timestamp) at the start of do_export, or refresh the preview in on_finished and on table-selection changes.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:2639 — correctness
- **요약**: do_import's fresh-run reset block clears logs/results but never resets import_table_rows_done, import_table_rows_total, table_chunk_progress, or dump_metadata, so a second import in the same dialog computes progress from the previous run's row counters.
- **실패 시나리오**: User completes import A, picks a different dump folder, and runs import B in the same dialog. When the Rust core does not emit overall_rows_done/total, on_detail_progress (lines 2789-2791) sums stale run-A done/total values with run-B's, so the progress bar/percent starts inflated (can show near-100% immediately) or is permanently wrong; on_metadata_analyzed refreshes totals but never the stale done values, and table_chunk_progress/dump_metadata show run-A sizes/chunks against run-B tables.
- **수정 힌트**: Inside the `if not retry_tables:` block, also clear import_table_rows_done, import_table_rows_total, table_chunk_progress, and dump_metadata.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:2958 — security
- **요약**: Import on_raw_output appends every raw Rust JSONL line verbatim and unbounded to log_entries, while the export counterpart deliberately strips 'password'/'credentials' keys (lines 1658-1659) and never persists raw lines.
- **실패 시나리오**: Any core event or error line that echoes connection parameters (the export author explicitly anticipated password/credentials keys in telemetry) is written verbatim into log_entries and then into the user-saved import log file on disk -> credential leak in a plaintext .txt. Independently, a long import emits thousands of row_progress/chunk JSONL lines that all accumulate in the in-memory list and bloat the saved log, unlike the visible log which is capped at 500 items.
- **수정 힌트**: Apply the same sanitization as export (parse JSON, pop password/credentials) before persisting, and persist only the formatted visible summaries (or cap/skip raw telemetry lines in log_entries).

### [db-dialogs] src/ui/dialogs/db_dialogs.py:3111 — concurrency
- **요약**: self._github_worker is overwritten on every failure report; GitHubReportWorker is an unparented QThread, so overwriting the reference while a previous report is still in-flight garbage-collects a running QThread.
- **실패 시나리오**: Import fails -> GitHub report worker starts an HTTP call (seconds on a slow network). User immediately retries the failed tables and the retry fails fast (SQL error within 1-2s) -> _report_error_to_github reassigns self._github_worker, dropping the only Python reference to the still-running first worker -> 'QThread: Destroyed while thread is still running' -> hard abort of the whole app. Same pattern in the export dialog at line 1690.
- **수정 힌트**: Keep workers in a list pruned on their finished signal, or skip a new report while one isRunning(), or parent the worker to the dialog.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:3124 — duplication
- **요약**: _get_import_mode_text is defined twice in RustDumpImportDialog (lines 2572 and 3124); the second definition silently overrides the first, making the first dead code and changing the text shown in the production-guard confirmation.
- **실패 시나리오**: The ProductionGuard details at line 2610 were written against the first definition ('전체 교체 Import' / '증분 Import (병합)'), but at runtime the later definition wins, so the dangerous-operation confirmation shows 'replace (기존 테이블 삭제)' style text instead. Any future edit to the 2572 copy is a no-op, a classic maintenance trap — a developer 'fixing' the visible text there will see no effect.
- **수정 힌트**: Delete one definition; if two formats are needed, give them distinct names (e.g., _import_mode_label vs _import_mode_key).

### [db-dialogs] src/ui/dialogs/db_dialogs.py:3578 — concurrency
- **요약**: OrphanRecordDialog.start_analysis runs ForeignKeyResolver.find_orphan_records synchronously on the GUI thread, issuing one COUNT(*) LEFT JOIN per FK relation, with QApplication.processEvents only between relations.
- **실패 시나리오**: On a schema with large tables or unindexed FK columns, a single COUNT(*) LEFT JOIN can run for minutes; the entire app freezes ('응답 없음' on Windows) with no cancel path since processEvents only pumps between queries. Clicking 닫기 during processEvents queues an accept that hides the dialog while the loop keeps hammering the DB. This contradicts the QThread-worker pattern used by every other long operation in this file.
- **수정 힌트**: Move the analysis into a QThread worker (like RustDumpWorker) emitting progress signals; same for export_report at line 3720 which re-runs the full analysis synchronously.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:3736 — error-handling
- **요약**: OrphanRecordDialog.closeEvent deliberately skips connector.disconnect() ('외부에서 관리' comment), but the external owner RustDumpWizard.start_orphan_check (line 3427) never disconnects either, leaking a live DB session per orphan-check run.
- **실패 시나리오**: Each '고아 레코드 분석' invocation opens a fresh connector through the Rust facade and never closes it; repeated checks accumulate open connections against the tunneled DB until app exit — on servers with low max_connections (e.g., the documented 30-connection PostgreSQL), this exhausts connection slots and subsequent tunnel/DB operations start failing. Export/Import dialogs close their connector in closeEvent; only this flow leaks.
- **수정 힌트**: Disconnect in RustDumpWizard.start_orphan_check after orphan_dialog.exec() (it created the connector), or have the dialog own and close it like the other two dialogs.

### [export-workers] src/core/scheduler.py:397 — concurrency
- **요약**: _run_loop holds self._lock for the entire task execution (a full Rust dump or SQL batch), and ScheduleListDialog's add/edit/delete handlers call add_schedule/update_schedule/remove_schedule which acquire the same lock on the GUI thread.
- **실패 시나리오**: A scheduled backup of a large schema is running in the background thread (lock held for many minutes). User opens 스케줄 작업 관리 and clicks 저장 on a new schedule -> scheduler.add_schedule blocks on self._lock -> the GUI thread freezes ('응답 없음') until the backup completes.
- **수정 힌트**: Take the lock only to snapshot due schedules and to update state; execute the task itself outside the lock.

### [export-workers] src/exporters/rust_dump_exporter.py:347 — concurrency
- **요약**: RustDumpExporter/Importer default to the shared DbCoreFacade whose DbCoreServiceClient.request() holds a single lock for the entire request (db_core_service.py:153-176); a running dump.run/dump.import therefore holds the lock for minutes-to-hours and every other shared-facade consumer blocks silently.
- **실패 시나리오**: User starts a large export via RustDumpWorker. They then run a connection test or SQL file execution (RustDbConnector/create_rust_db_connector also default to the shared facade) — the worker blocks at '🔌 Rust DB Core 연결 중...' with no feedback until the export completes; a concurrently scheduled backup thread also stalls. There is no timeout and no message explaining the wait.
- **수정 힌트**: Give long-running dump/import operations a dedicated DbCoreFacade/DbCoreServiceClient instance (own core process), or add a per-request queue with progress feedback instead of a monolithic lock.

### [export-workers] src/exporters/rust_dump_exporter.py:713 — correctness
- **요약**: import_results statuses can never be 'error' (Rust core only emits completed/dumping/importing/pending/verifying/dropping table_progress statuses, and a failing import raises DbCoreServiceError leaving tables at pending/loading/done), but the import dialog's retry feature keys on status == 'error' — so the retry-failed-tables UI is dead code.
- **실패 시나리오**: A dump.import fails on one table; facade.import_dump raises, import_dump returns (False, msg, results) with statuses 'done'/'loading'/'pending'. In db_dialogs.py:3007-3014 failed_tables = [status == 'error'] is always empty, so btn_retry/btn_select_failed never appear and the user has no way to retry only the failed tables. The success path (lines 696-697) additionally stomps all event-derived statuses to 'done'.
- **수정 힌트**: In the DbCoreServiceError/Exception handlers, mark non-'done' tables as {'status': 'error', 'message': str(exc)} before returning; align the dialog's expected vocabulary with emit_core_event.

### [export-workers] src/ui/dialogs/schedule_dialog.py:136 — correctness
- **요약**: The 'UPDATE without WHERE' danger regex (r'\bUPDATE\s+\w+\s+SET\s+.*?(?:;|$)(?!.*WHERE)') places the negative lookahead after the lazy .*? has already consumed the WHERE clause, so it fires on every UPDATE statement — verified empirically: 'UPDATE users SET active=0 WHERE id=3;' matches.
- **실패 시나리오**: User schedules a perfectly safe 'UPDATE t SET x=1 WHERE id=5;' — the editor permanently shows '⚠️ UPDATE에 WHERE 절이 없어 전체 데이터가 수정됩니다!' and _save forces a scary Yes/No confirmation. Users learn to click through the warning, defeating the real protection for genuinely unbounded UPDATEs.
- **수정 힌트**: Match per-statement and test the WHERE inside the statement body, e.g. r'\bUPDATE\s+\w+\s+SET\s+[^;]*(?:;|$)' then reject if re.search(r'\bWHERE\b', stmt) is present, or use the existing sql_statement_parser to split statements first.

### [export-workers] src/ui/dialogs/schedule_dialog.py:880 — correctness ✅검증확정
- **요약**: The '활성화' checkbox in the schedule table is user-toggleable (default QTableWidgetItem flags include ItemIsUserCheckable, and check toggling bypasses NoEditTriggers) but no itemChanged handler exists and scheduler.set_enabled() is never called from the UI, so toggling it changes nothing.
- **실패 시나리오**: User unchecks 활성화 on a scheduled nightly 'DELETE FROM logs ...' SQL job and closes the dialog believing it is disabled. The ScheduleConfig still has enabled=True, and the scheduler thread runs the destructive SQL at the next cron tick. Any refresh silently reverts the checkbox.
- **수정 힌트**: Either connect table.itemChanged to scheduler.set_enabled(schedule_id, state) + _save_schedules, or strip ItemIsUserCheckable to make the column read-only.
- **검증**: Confirmed by direct code trace. (1) schedule_dialog.py:879-884 creates the 활성화 checkbox via bare QTableWidgetItem() + setCheckState() — default Qt flags include ItemIsUserCheckable|ItemIsEnabled and no setFlags() strips them, so the checkbox is user-toggleable. (2) NoEditTriggers (line 751) does not block check-state toggling: QStyledItemDelegate::editorEvent handles the indicator click before edi

### [export-workers] src/ui/dialogs/sql_editor_dialog.py:3759 — concurrency
- **요약**: ValidationWorker/AutoCompleteWorker/MetadataLoadWorker references are replaced with new instances while the previous thread may still be running — cancel() only sets a flag and never wait()s — so the old running QThread can be garbage-collected mid-run and destroyed while running.
- **실패 시나리오**: User types quickly in the SQL editor with a large schema loaded: _on_validation_requested (line 3759) and _on_autocomplete_requested (line 3798) each drop the previous still-running worker; while validator.validate() is executing, the old QThread's refcount reaches 0 -> Qt destroys a running thread -> intermittent hard crash of the editor. metadata_worker at line 3645 is replaced without even a cancel/isRunning check.
- **수정 힌트**: Keep cancelled workers in a pending list until their finished signal fires (connect finished -> deleteLater + list removal), or call cancel() then wait() before replacing.

### [export-workers] src/ui/dialogs/tunnel_config.py:400 — concurrency
- **요약**: ConnectionTestWorker instances are held only in a local variable across dialog.exec(); if the TestProgressDialog is dismissed while the test is still running, the last Python reference to the running QThread is dropped and the C++ QThread is destroyed while running (hard crash).
- **실패 시나리오**: User presses Escape on TestProgressDialog (close button is hidden but ESC still triggers QDialog.reject) during a slow bastion SSH handshake; exec() returns, _test_tunnel/_test_db_only/_test_integrated return, the local 'worker' refcount hits 0 while _test_db is still inside Paramiko -> 'QThread: Destroyed while thread is still running' and the app aborts. Same pattern at lines 458 and 505.
- **수정 힌트**: Store the worker on self, block ESC/reject while running (override reject or keyPressEvent), and only release the reference after the built-in QThread.finished (not the shadowed custom signal) has fired.

### [export-workers] src/ui/workers/rust_dump_worker.py:23 — concurrency
- **요약**: RustDumpWorker has no cancellation mechanism whatsoever (no cancel(), and the underlying blocking JSONL request cannot be aborted), while the export dialog's closeEvent (db_dialogs.py:1857) unconditionally blocks closing whenever worker.isRunning().
- **실패 시나리오**: User starts an export of the wrong multi-hour schema, or the SSH tunnel/core process stalls mid-dump so facade.run_dump blocks forever in readline(). The user cannot cancel the operation and cannot close the export dialog (closeEvent ignores forever); the only recourse is killing the whole application.
- **수정 힌트**: Add a cancel path: run dump requests on a dedicated client whose process can be terminated on cancel (like CrossEngineMigrationWorker.cancel does), and let closeEvent offer cancel-and-close.

### [infra-core] src/core/config_manager.py:103 — error-handling ✅검증확정
- **요약**: save_config writes config.json in place with open('w') (no temp-file + os.replace), so a crash/power loss mid-write corrupts the config, and load_config silently degrades it to an empty tunnel list that then rotates away the good backups.
- **실패 시나리오**: Process is killed (or disk full) during json.dump -> truncated config.json. On next launch load_config hits JSONDecodeError, logs, and returns {'tunnels': []}. The app runs 'empty'; any subsequent set_app_setting/save_config first backs up the corrupt file and after MAX_BACKUPS=5 more saves (scheduler _save_schedules alone saves on every trigger) all 5 healthy backups are purged -> permanent loss of all tunnel definitions and encrypted credentials.
- **수정 힌트**: Write to config.json.tmp then os.replace(); on load failure, surface the error to the user and/or auto-restore the newest valid backup instead of returning an empty config.
- **검증**: Fully traced and confirmed. (1) src/core/config_manager.py:103-104 writes config.json in place with open('w')+json.dump — no temp file, no os.replace, no fsync — so open() truncates first and any crash/power-loss/disk-full mid-write leaves a truncated file. (2) load_config (lines 91-96) catches bare Exception and silently returns {"tunnels": []}; _ensure_config_exists only checks file existence, a

### [infra-core] src/core/config_manager.py:181 — correctness ✅검증확정
- **요약**: restore_backup calls _create_backup() before reading the selected backup, and the resulting cleanup can delete the exact backup file being restored, destroying it and failing the restore.
- **실패 시나리오**: 5 backups exist (steady state, MAX_BACKUPS=5). User picks the oldest one in the restore UI. restore_backup -> _create_backup() creates a 6th -> _cleanup_old_backups deletes backups[5:] which is precisely the user-selected file -> line 184 open(backup_path) raises FileNotFoundError -> '복원 중 오류 발생' AND the backup the user was trying to recover is permanently gone.
- **수정 힌트**: Read/validate the backup content into memory (or copy it aside) before calling _create_backup, or exclude the restore target from cleanup.
- **검증**: Confirmed by direct code trace. In src/core/config_manager.py, restore_backup (line 165) checks existence of the selected backup (line 176) and then calls _create_backup() at line 181 BEFORE reading it. _create_backup copies the current config to config.backup.<now>.json (always lexicographically/chronologically newest) and calls _cleanup_old_backups, which deletes backups[MAX_BACKUPS:] (line 131,

### [infra-core] src/core/config_manager.py:364 — concurrency
- **요약**: ConfigManager has no locking, and set_app_setting/save_config do unsynchronized read-modify-write of the whole config file from three threads (UI, scheduler _save_schedules, tunnel monitor set_auto_reconnect), causing lost updates.
- **실패 시나리오**: Scheduler thread fires a schedule: load_config (snapshot A) ... set_app_setting('schedules', ...) saves A+schedules. Concurrently the user adds a new tunnel on the UI thread saving snapshot B. Whichever write lands last silently discards the other's change - e.g., the just-created tunnel disappears from config.json after the 03:00 backup completes.
- **수정 힌트**: Add a process-wide threading.Lock around load/modify/save in ConfigManager (or cache config in memory behind the lock) so read-modify-write is atomic.

### [infra-core] src/core/production_guard.py:336 — correctness
- **요약**: In the STAGING confirmation QMessageBox, implicit f-string concatenation binds tighter than the trailing ternary, so when details is empty the entire message body (operation + schema name) collapses to an empty string.
- **실패 시나리오**: confirm_dangerous_operation(tunnel_config, '데이터 Import', 'prod_db') is called with default details='' on a staging tunnel. Python parses the argument as (f'<b>...' f'...' f'{details}') if details else '' -> text=''. The Yes/No dialog shows an empty body: the user must approve a destructive import without seeing which schema is targeted.
- **수정 힌트**: Build the message in a variable first: msg = f'...schema...'; if details: msg += details; pass msg to QMessageBox.warning.

### [infra-core] src/core/scheduler.py:380 — concurrency
- **요약**: run_now performs the full dump/SQL export synchronously, and the UI calls it directly on the Qt main thread (main_window.py:945, schedule_dialog.py:970) despite the comment '백그라운드에서 실행', freezing the GUI for the whole export.
- **실패 시나리오**: User clicks '즉시 실행' on a schedule that exports a multi-GB schema -> RustDumpExporter runs on the UI thread -> window becomes 'Not Responding' for the entire export; tray/menu/tunnel monitoring UI updates stall.
- **수정 힌트**: Run run_now via a QThread worker (project already has rust_dump_worker patterns) and report completion through a signal.

### [infra-core] src/core/scheduler.py:397 — concurrency
- **요약**: _run_loop holds self._lock for the entire duration of a backup/SQL execution, blocking UI-thread schedule mutations for minutes; meanwhile run_now executes the same schedules with no lock at all.
- **실패 시나리오**: A scheduled export of a large schema runs for 10 minutes inside 'with self._lock'. The user opens the schedule dialog and clicks save -> update_schedule blocks on the same lock -> Qt UI freezes until the backup finishes. Conversely run_now (line 380) mutates schedule.last_run and runs _execute_task concurrently with _run_loop without any lock, allowing double-execution of the same schedule.
- **수정 힌트**: Only hold the lock to snapshot due schedules; execute tasks outside the lock, and route run_now through the same serialized execution path.

### [infra-core] src/core/scheduler.py:451 — correctness
- **요약**: The 'if not conn_info' guard can never fire because get_connection_info always returns a 2-tuple (possibly (None, None)), and the isinstance(conn_info, dict) branches (lines 473, 701) are dead code for an API that only returns tuples.
- **실패 시나리오**: If tunnel_configs momentarily lacks the tunnel (e.g., monitor's reconnect thread ran stop_tunnel between the is_running check and here), conn_info=(None, None) passes the truthy check, host=None falls back to 127.0.0.1 and port to 3306 -> the backup/SQL silently targets the wrong endpoint (or fails with a misleading auth/connect error) instead of the intended '연결 정보를 가져올 수 없습니다' error.
- **수정 힌트**: Check 'host, port = conn_info; if host is None or port is None: error'; delete the dict branches copied from a different API.

### [infra-core] src/core/scheduler.py:809 — correctness
- **요약**: _execute_single_query classifies statements by startswith('SELECT'), so WITH...SELECT (CTE), SHOW, EXPLAIN etc. are treated as DML: results are discarded, commit() is issued, and the schedule reports success without producing the expected result file.
- **실패 시나리오**: User schedules 'WITH recent AS (SELECT ...) SELECT * FROM recent' with result_format=csv. is_select=False -> the rows are never fetched or saved; connection.commit() runs; task logs 'SQL 실행 완료 ... N행 영향' with no CSV. The scheduled report silently yields nothing while showing 성공.
- **수정 힌트**: Detect result sets via cursor.description (non-None => save rows) instead of prefix matching.

### [infra-core] src/core/tunnel_monitor.py:277 — concurrency
- **요약**: _check_all_tunnels performs blocking DB connect/ping work (_measure_latency -> _create_health_connection -> connector.connect()) while holding self._lock, which the UI thread also takes via get_status/get_all_statuses (polled every 1s by TunnelStatusDialog).
- **실패 시나리오**: Currently masked by the dead db_username lookup (line 332 finding), but as soon as credentials resolve: DB behind the tunnel goes down -> each 5s monitor cycle blocks inside the lock for up to the Rust core connect timeout; the status dialog's 1s QTimer calls get_all_statuses on the UI thread and blocks on the same RLock -> GUI freezes for seconds on every monitor cycle.
- **수정 힌트**: Measure latency outside the lock and only re-acquire it to write results into TunnelStatus.

### [infra-core] src/core/tunnel_monitor.py:332 — dead-code
- **요약**: Latency health-check reads config keys 'db_username'/'db_password' that no code ever writes (tunnel configs store 'db_user'/'db_password_encrypted'), so latency measurement and the whole _health_connections machinery are permanently unreachable.
- **실패 시나리오**: For every tunnel, db_username is '' at line 332 -> early return -1 at line 339. TunnelStatusDialog always shows '-' latency; _create_health_connection, _cleanup_health_connection and the health-connection cache are dead code. Additionally, even if the keys matched, RustDbConnection.ping (db_core_service.py:568) is a local no-op that never contacts the DB, so the 'measured' latency would be meaningless (~0ms) and a dead DB would never be detected.
- **수정 힌트**: Read db_user + decrypt db_password_encrypted via ConfigManager (or inject credentials when the tunnel starts), and implement a real ping/SELECT 1 through the Rust core; otherwise delete the dead machinery.

### [infra-core] src/ui/main_window.py:966 — concurrency
- **요약**: _on_backup_complete is invoked on the BackupScheduler background thread (scheduler.py:410 _notify_callbacks) and directly calls QSystemTrayIcon.showMessage, a GUI call from a non-GUI thread.
- **실패 시나리오**: A scheduled backup finishes at 03:00 -> scheduler thread calls the callback -> tray_icon.showMessage executes off the Qt main thread. Qt GUI classes are not thread-safe: this can crash intermittently or silently drop the notification depending on platform/timing. The sibling callback _on_tunnel_status_changed (line 984) correctly marshals via QMetaObject.invokeMethod QueuedConnection, showing the intended pattern was known but not applied here.
- **수정 힌트**: Marshal to the UI thread (queued signal or QMetaObject.invokeMethod) before touching tray_icon, mirroring _on_tunnel_status_changed.

### [main-ui] src/ui/dialogs/settings.py:161 — dead-code
- **요약**: The '연결 풀' settings tab monitors src.core.connection_pool, but nothing in the codebase ever populates the registry - PooledMySQLConnector/get_pooled_connector have zero callers - so the tab is permanently empty and its '모든 연결 종료' button is a no-op.
- **실패 시나리오**: Repo-wide grep shows PooledMySQLConnector is only defined, never instantiated; get_or_create_pool is only called from its __init__. Under the Rust-core baseline all connections are owned by tunnelforge-core, so every user who opens Settings -> 연결 풀 sees an eternally empty table and a kill button that does nothing - dead legacy UI from the pre-Rust-core pooling era that also keeps the retired connection_pool module alive.
- **수정 힌트**: Remove _create_pool_tab/_refresh_pool_status/_close_all_pools and the tab registration (or repoint it at real Rust-core session stats if that telemetry exists).

### [main-ui] src/ui/main_window.py:394 — correctness
- **요약**: Five auto-start paths call self.engine.start_tunnel() directly, bypassing the start_tunnel() wrapper, so _register_login_path() is never invoked - mysql login-path state diverges from actual tunnel state depending on which UI path started the tunnel.
- **실패 시나리오**: User invokes 'DB 연결' (line 394), SQL editor (1232), context Export (1276), Import (1304), or orphan check (1332) on a stopped tunnel and confirms auto-start. The tunnel starts via engine.start_tunnel() but the mysql_config_editor login path is never registered, so the login-path feature silently does not work for that session; later stop_tunnel() attempts to remove a login path that was never registered. Tunnels started via the power button behave differently from tunnels started via these flows - inconsistent lifecycle side effects for the same logical operation.
- **수정 힌트**: Extract a single _ensure_tunnel_running(tunnel) helper that delegates to self.start_tunnel() (which registers the login path and refreshes), and use it in all five call sites.

### [main-ui] src/ui/main_window.py:451 — performance
- **요약**: Tree context-menu '연결 테스트' runs engine.test_connection() synchronously on the UI thread (with a QApplication.processEvents band-aid), freezing the whole UI for the duration of the SSH handshake/timeout, duplicating the existing threaded ConnectionTestWorker.
- **실패 시나리오**: User right-clicks a tunnel whose bastion is unreachable and picks 연결 테스트: the full SSH connect attempt (TCP timeout, tens of seconds) blocks the Qt event loop - window goes 'Not Responding', tray and other tunnels' buttons are dead. Same pattern in _test_direct_connection (line 497) which blocks on connector.connect(). Meanwhile TunnelConfigDialog already does exactly this test via ConnectionTestWorker(TestType.TUNNEL_ONLY)+TestProgressDialog in a background thread - the logic is duplicated in a worse, blocking form.
- **수정 힌트**: Route _on_tree_test_connection/_test_direct_connection through ConnectionTestWorker + TestProgressDialog, and remove the processEvents calls.

### [main-ui] src/ui/main_window.py:818 — clean-code
- **요약**: reload_config() unconditionally pops a modal QMessageBox ('설정 파일을 다시 불러왔습니다.') and is called from every group CRUD and drag-and-drop move, interrupting the user with a modal dialog on each drag.
- **실패 시나리오**: User drags a tunnel row into a group -> tunnel_moved_to_group -> _on_tunnel_moved (line 551) -> reload_config() -> modal info box appears after every single drag. Same for add_group_dialog (566), _edit_group_dialog (582), _delete_group (604). The popup was designed for an explicit manual 'reload' action; reusing reload_config as the refresh primitive turned it into a per-operation modal interruption.
- **수정 힌트**: Split into a silent _reload_and_refresh() used by group/D&D handlers; keep the message box only for an explicit user-invoked reload action (or drop it entirely).

### [main-ui] src/ui/main_window.py:999 — performance
- **요약**: Every TunnelMonitor heartbeat triggers a full refresh_table() tree rebuild - the monitor notifies unconditionally every 5s per active tunnel (tunnel_monitor.py:301), not only on state change.
- **실패 시나리오**: With one connected tunnel, _check_all_tunnels calls _notify_callbacks every 5s regardless of change -> queued _update_tunnel_status_ui -> refresh_table() clears and rebuilds the entire tree, destroys and recreates all power/edit/delete button widgets, and wipes the current selection. User selects a row or is mid-drag when the 5s tick lands: selection vanishes / the dragged item is deleted under the cursor and the drop is silently ignored (dropEvent's currentItem is gone). With N tunnels this is N full rebuilds per cycle - constant widget churn the code already fights with _schedule_repaint anti-ghosting hacks.
- **수정 힌트**: In _update_tunnel_status_ui, only call tunnel_tree.update_tunnel_status(tunnel_id, engine.is_running(...)) plus power-button label/style update; or have TunnelMonitor notify only on state transitions.

### [main-ui] src/ui/main_window.py:1089 — correctness
- **요약**: _on_column_resized is never connected to the tree header's sectionResized signal, so user column-width adjustments are never captured, get reverted on any window resize, and are never persisted.
- **실패 시나리오**: User drags a column boundary in the tunnel tree. _column_ratios is never updated (no header().sectionResized.connect(self._on_column_resized) anywhere in the repo). On the next window resizeEvent, _apply_column_ratios() (line 1057) snaps all columns back to the stale stored ratios, and _save_column_ratios() on close persists only the originally loaded values - the user's layout silently reverts every session. The handler exists solely as dead residue of the table->tree migration; TunnelTreeWidget.set_column_ratios (tunnel_tree.py:87) is likewise never called.
- **수정 힌트**: In init_ui: self.tunnel_tree.header().sectionResized.connect(self._on_column_resized); delete the unused set_column_ratios.

### [migration-core-a] src/core/migration_analyzer.py:501 — correctness
- **요약**: check_deprecated_in_routines uses plain substring matching (`if func in definition`) against the uppercased routine body, causing error-severity false positives for very common identifiers and double-counting.
- **실패 시나리오**: ALL_REMOVED_FUNCTIONS includes 'PASSWORD', 'ENCRYPT', 'ENCODE', 'DECODE' (migration_constants.py lines 103-111). Any stored procedure that references a column named `password` or calls the perfectly valid AES_ENCRYPT()/AES_DECRYPT() is flagged as "removed 함수 'PASSWORD'/'ENCRYPT' 사용 중" with severity=error, polluting the migration report and pushing users toward unnecessary 'fixes'. Also, a routine using SQL_CALC_FOUND_ROWS matches both 'FOUND_ROWS' and 'SQL_CALC_FOUND_ROWS' substrings and is reported twice.
- **수정 힌트**: Match with a word-boundary regex requiring a following '(' for functions, e.g. re.search(rf"\\b{func}\\s*\\(", definition), and skip shorter names already covered by a longer match.

### [migration-core-a] src/core/migration_analyzer.py:1656 — dead-code
- **요약**: TwoPassAnalyzer and EnhancedDumpFileAnalyzer (~480 lines, lines 1611-2095) have no production callers — the UI uses the base DumpFileAnalyzer — leaving the advertised '2-Pass 분석 아키텍처' unreachable.
- **실패 시나리오**: src/ui/dialogs/db_dialogs.py:2437 instantiates DumpFileAnalyzer(), never EnhancedDumpFileAnalyzer/TwoPassAnalyzer (grep shows only tests reference them). The FK cross-validation (pass2_5), rule-module integration, and the RULES_AVAILABLE/PARSERS_AVAILABLE/FIX_GENERATOR_AVAILABLE ImportError-swallowing flags (lines 39-57) exist only inside this dead subsystem; the module docstring and tests suggest the feature works, but users never get 2-pass FK validation, and maintenance effort continues to be spent on unreachable code.
- **수정 힌트**: Either wire EnhancedDumpFileAnalyzer into the dump-analysis dialog (and replace the silent ImportError fallbacks with logged hard failures for these first-party modules), or delete the subsystem and its tests.

### [migration-core-a] src/core/migration_fix_wizard.py:251 — correctness
- **요약**: _get_column_definition emits DEFAULT values without escaping single quotes and appends the raw EXTRA column verbatim, producing syntactically invalid MODIFY COLUMN SQL for common column definitions.
- **실패 시나리오**: A utf8mb3 varchar column with DEFAULT "O'Brien" gets the charset fix option SQL `... DEFAULT 'O'Brien' ...` (unescaped quote -> MySQL error 1064 when the user runs it). Similarly, a column with an expression default has EXTRA='DEFAULT_GENERATED' in INFORMATION_SCHEMA; line 256 appends it verbatim, yielding `... DEFAULT (expr) DEFAULT_GENERATED` which is invalid SQL. RollbackSQLGenerator already has correct implementations (_format_default_clause escapes quotes at line 1700, _format_extra_clause filters EXTRA at line 1704) but this code path does not use them.
- **수정 힌트**: Reuse RollbackSQLGenerator._format_default_clause/_format_extra_clause (or extract them to module-level helpers) inside _get_column_definition.

### [migration-core-a] src/core/migration_fix_wizard.py:455 — correctness ✅검증확정
- **요약**: DECIMAL fix option's sql_template hard-codes DECIMAL(10,2) and lacks the {precision} placeholder, so the user's precision input is silently ignored.
- **실패 시나리오**: User picks 'DECIMAL로 변경' for a FLOAT(M,D) issue and enters '16,6' in the input field (input_label='DECIMAL 정밀도 (M,D)'). generate_sql (line 531) and execute_batch (line 1393) both do sql.replace('{precision}', user_input), but the template is the literal string 'ALTER TABLE ... DECIMAL(10,2);' with no placeholder, so the generated/previewed DDL is always DECIMAL(10,2). If the user copies and runs that SQL, values needing 16,6 precision are silently rounded/truncated to 2 decimal places.
- **수정 힌트**: Change the template to f"... MODIFY COLUMN `{column}` DECIMAL({{precision}});" so the replace() calls actually substitute the user input; optionally validate the 'M,D' format.
- **검증**: Confirmed by direct code inspection. src/core/migration_fix_wizard.py:455 hard-codes DECIMAL(10,2) in the f-string template with no {precision} placeholder (compare the correct '{{custom_date}}' escape at line 306), while setting requires_input=True with input_label='DECIMAL 정밀도 (M,D)'. Grep shows {precision} exists ONLY in the three replacement call sites (migration_fix_wizard.py:531, :1393, fix_

### [migration-core-a] src/core/migration_fix_wizard.py:1122 — dead-code
- **요약**: BatchFixExecutor retains a large block of unreachable mutation/rollback machinery behind the unconditional dry-run-only guard, including a no-op _session_guard whose docstring falsely claims it restores session state.
- **실패 시나리오**: execute_batch raises RuntimeError for dry_run=False (line 1122), so every `not dry_run` branch below is dead: the fk_changer.execute_safe_charset_change(dry_run=False) call (1197), the merge fallback (1276-1294), _execute_fk_safe_charset_change (1505), and _execute_single (1498, itself an unconditional raise). pre_states is always {} (1139) and _capture_pre_states (1440) is never called anywhere, so rollback generation at 1420 (`if not dry_run and pre_states`) can never run; FKSafeCharsetChanger._build_recovery_sql (935) is likewise uncalled, and fix_wizard_worker.py lines 120-124 read a 'recovery_sql' key that can never be produced. _session_guard (1087-1101) is `yield` only, yet its docstring claims sql_mode/FOREIGN_KEY_CHECKS restore; original_sql_mode is fetched via a real DB round-trip at line 1143 and never used. Anyone re-enabling execution later will trust the documented-but-nonexistent session restore and rollback capture.
- **수정 힌트**: Delete the dead non-dry-run branches, _capture_pre_states, _execute_single/_execute_fk_safe_charset_change, _build_recovery_sql, the unused original_sql_mode fetch, and _session_guard (or make its docstring say it is a retired no-op); this aligns with the Rust-core mutation ownership baseline.

### [migration-core-a] src/core/migration_fix_wizard.py:1326 — correctness
- **요약**: execute_batch de-duplicates processed steps by location string (fk_safe_processed / merged_locations), so a different-issue-type step sharing the same 'schema.table' location is silently dropped from the batch.
- **실패 시나리오**: A MyISAM table with utf8mb3 collation yields two issues at identical location 'schema.foo': DEPRECATED_ENGINE (from check_deprecated_engines, line 1142 of migration_analyzer.py) and table-level CHARSET_ISSUE (line 404). User selects 'InnoDB로 변경' for the engine and 'FK 안전 변경' for the charset. The charset step is handled in the FK-safe batch and 'schema.foo' is added to fk_safe_processed (line 1206); the main loop then hits the engine step and `step.location in fk_safe_processed` skips it with no FixExecutionResult. The engine fix disappears from the results/preview and success_count+fail_count+skip_count no longer equals total_steps in BatchExecutionResult shown to the user.
- **수정 힌트**: Track processed steps by object identity (id(step)) or by (location, issue_type/strategy) instead of the bare location string.

### [migration-core-b] src/core/migration_constants.py:477 — correctness ✅검증확정
- **요약**: TRAILING_SPACE_PATTERN and CONTROL_CHAR_PATTERN (and DOLLAR_SIGN_PATTERN) match the text BETWEEN two identifiers, not identifier contents, flooding false error issues on every normal dump.
- **실패 시나리오**: Verified: content "CREATE TABLE `t` (\n  `id` int NOT NULL,\n  `name` varchar(255) DEFAULT NULL\n)" run through SchemaRules.check_all_sql_content produces 4 false 'error' issues (2 trailing_space_name for '` int NOT NULL,\n  `', 2 control_char_name because the gap contains \n). Any multi-column table yields ~1 error per column pair, burying real findings. DOLLAR_SIGN_PATTERN similarly fires on data like DEFAULT '$0.00' between identifiers.
- **수정 힌트**: Anchor the regex to backtick-delimited identifier spans only, e.g. parse identifiers with a proper tokenizer or use `(?<=`)([^`\n]*?)(?=`)` on known identifier positions (CREATE TABLE / column definitions), or at minimum exclude newlines and require the match to start right after CREATE/KEY/CONSTRAINT contexts.
- **검증**: Defect confirmed exactly as claimed: TRAILING_SPACE_PATTERN (`[^`]*\s+`, migration_constants.py:477), CONTROL_CHAR_PATTERN (line 480), and DOLLAR_SIGN_PATTERN (line 474) have no identifier-boundary awareness and match the span between one identifier's closing backtick and the next identifier's opening backtick. Empirically reproduced with the exact claimed input: the 2-column CREATE TABLE yields 2

### [migration-core-b] src/core/migration_constants.py:547 — correctness
- **요약**: INVALID_57_NAME_MULTIPLE_DOTS_PATTERN scans the whole dump (including INSERT string data) for `word..word`, so ordinary text data produces error-severity 'invalid identifier' findings.
- **실패 시나리오**: Verified: INSERT INTO t VALUES ('see notes..thanks') matches 'notes..thanks' and check_invalid_57_name_multiple_dots reports an error issue. Data like 'rows 1..10', double-dot typos, or URLs with '..' in any text column falsely fail the identifier check.
- **수정 힌트**: Restrict the pattern to identifier contexts (after FROM/JOIN/INTO/TABLE/REFERENCES keywords) or run it only on DDL statements extracted by the parser, not raw dump content.

### [migration-core-b] src/core/migration_parsers.py:64 — correctness
- **요약**: ParsedIndex.covers_columns treats a leftmost PREFIX match as 'covered', so a UNIQUE(a,b) index 'covers' (a); the identical logic is duplicated verbatim in migration_analyzer.TableIndexInfo (line 1625) where _is_valid_fk_reference uses it to validate FK uniqueness — yielding false negatives for FK_NON_UNIQUE_REF.
- **실패 시나리오**: Referenced table has UNIQUE KEY (tenant_id, code); an FK references (tenant_id) only. covers_columns(['tenant_id']) compares against columns[:1] and returns True, so _is_valid_fk_reference accepts it even though tenant_id alone is not unique — the FK_NON_UNIQUE_REF error the rule exists to catch is silently missed.
- **수정 힌트**: For uniqueness validation require exact column-set equality (len(cols) == len(self.columns) and equal); keep a separate 'is_prefix_covered' helper if prefix semantics are needed elsewhere. Remove one of the two duplicate method definitions.

### [migration-core-b] src/core/migration_parsers.py:174 — correctness
- **요약**: INDEX_PATTERN also matches the 'KEY (...)' substring inside PRIMARY KEY and FOREIGN KEY clauses, so every PK and FK adds a phantom non-unique index to ParsedTable.indexes.
- **실패 시나리오**: Verified: parsing a standard dump table with PRIMARY KEY (`id`), KEY `idx_user`, and CONSTRAINT ... FOREIGN KEY (`user_id`) yields indexes [PRIMARY, idx_0(['id']), idx_user, idx_2(['user_id'])] — idx_0/idx_2 do not exist. Any consumer counting/matching indexes (e.g. TwoPassAnalyzer index collection feeding FK cross-validation) operates on fabricated data.
- **수정 힌트**: Add a negative lookbehind/guard so KEY is not preceded by PRIMARY or FOREIGN (e.g. r'(?<!PRIMARY\s)(?<!FOREIGN\s)' is fragile with \s; better: parse per split definition and skip defs starting with PRIMARY/FOREIGN/CONSTRAINT before applying INDEX_PATTERN, mirroring _parse_columns' skip list).

### [migration-core-b] src/core/migration_parsers.py:178 — correctness
- **요약**: INDEX_PATTERN's column capture `\(\s*([^)]+)\s*\)` stops at the first ')' so prefix indexes like KEY `idx` (`name`(10)) parse to a garbage column name and prefix_lengths is never populated.
- **실패 시나리오**: Verified: CREATE TABLE with KEY `idx_n` (`name`(10)) parses to columns=['name`(10'], prefix_lengths=[None]. The _parse_index_columns prefix branch (line 397) is unreachable for real input because the closing paren it requires was consumed by the outer pattern; index-column comparisons and prefix-length-based checks silently operate on wrong names.
- **수정 힌트**: Use a balanced-paren scan for the index column list (reuse _split_definitions-style depth tracking) instead of [^)]+, then _parse_index_columns' existing prefix regex will work.

### [migration-core-b] src/core/migration_parsers.py:244 — correctness
- **요약**: _extract_body's paren-depth scan (and _split_definitions' comma split) is unaware of string literals, so an unbalanced ')' or a comma inside a COMMENT/DEFAULT string truncates or mis-splits the table body, silently dropping columns.
- **실패 시나리오**: Verified: CREATE TABLE `t` (`a` int COMMENT '50%) discount', `b` int NOT NULL, `c` varchar(10)) parses to columns ['a'] only — b and c vanish without any warning, so downstream column-level checks and FK/index cross-validation run against an incomplete table model.
- **수정 힌트**: Track quote state ('...' with '' and \' escapes) in both _extract_body and _split_definitions so parens/commas inside string literals are ignored.

### [migration-core-b] src/core/migration_rules/data_rules.py:175 — correctness
- **요약**: check_enum_numeric_index's CREATE TABLE regex `\((.+?)\)\s*(?:ENGINE|DEFAULT|;)` truncates the table body at the first ')' followed by DEFAULT, so ENUM columns after any `varchar(N) DEFAULT ...` column are never registered — the check silently returns nothing.
- **실패 시나리오**: Verified: table with `memo varchar(255) DEFAULT NULL` before `status enum(...)`, followed by INSERT ... VALUES (1,'x',2), captures body only up to 'varchar(255' and reports 0 issues (false negative). Additionally the VALUES scan `rest[:5000]` (line 206) crosses statement boundaries, so parenthesized rows of the NEXT statement can be attributed to this INSERT's column layout.
- **수정 힌트**: Extract the CREATE body with balanced-paren scanning (as CreateTableParser._extract_body does) and bound the VALUES scan at the statement's terminating semicolon.

### [migration-core-b] src/core/migration_rules/data_rules.py:489 — correctness
- **요약**: check_timestamp_range flags ANY quoted datetime with year <1970 or >2038 as an error-severity TIMESTAMP range violation, but data files cannot distinguish DATETIME columns, so ubiquitous sentinel values like '9999-12-31 23:59:59' are falsely reported.
- **실패 시나리오**: Verified: a TSV row containing '9999-12-31 23:59:59' (standard no-expiry sentinel, valid DATETIME) produces an 'error' issue 'TIMESTAMP 범위 초과 값'. Dumps of tables using end-date sentinels or historical dates (birthdays before 1970) get error-severity noise that can scare users off a safe migration.
- **수정 힌트**: Downgrade to warning/info with wording that the column type is unknown, or correlate with Pass-1 parsed column types (only flag values in columns declared TIMESTAMP).

### [migration-core-b] src/core/migration_rules/schema_rules.py:434 — correctness
- **요약**: check_generated_column_functions uses bare substring matching (`func in expression`) and iterates ALL_REMOVED_FUNCTIONS which contains 6 duplicated entries (16 items, 10 unique), producing false positives and duplicated issues.
- **실패 시나리오**: Verified: GENERATED ALWAYS AS (price * shift_rate) flags 'IF' (substring of SHIFT) as an 8.4 behavior-change warning; GENERATED ALWAYS AS (password_hash) yields TWO identical 'removed function PASSWORD' errors because PASSWORD appears in both REMOVED_FUNCTIONS_84 and REMOVED_FUNCTIONS_80X (migration_constants.py:114). Expressions with ifnull() also get flagged for both IF and IFNULL.
- **수정 힌트**: Match with word boundary + call parens: re.search(rf'\b{func}\s*\(', expression); dedupe ALL_REMOVED_FUNCTIONS via tuple(dict.fromkeys(...)) in migration_constants.py.

### [migration-core-b] src/core/migration_rules/schema_rules.py:574 — error-handling
- **요약**: check_routine_definer_missing (and check_view_definer_missing at line 614) swallow the mysql.user query failure and fall back to an empty user set, causing EVERY routine/view to be flagged 'Definer가 존재하지 않음' when the account lacks SELECT on mysql.user.
- **실패 시나리오**: A non-admin migration user (the common case) cannot read mysql.user → except Exception: existing_users = set() → all definers compare as missing → warning spam that misleads the user into 'fixing' healthy definers. The identical users-query block is also copy-pasted between the two methods.
- **수정 힌트**: On mysql.user failure, skip the check and emit a single info issue ('definer 검증 불가: 권한 부족') instead of comparing against an empty set; extract the shared user-fetch into a helper.

### [migration-core-b] src/core/schema_diff.py:92 — correctness ✅검증확정
- **요약**: ColumnInfo.to_sql_definition appends INFORMATION_SCHEMA EXTRA verbatim; MySQL 8 reports EXTRA='DEFAULT_GENERATED' for DEFAULT CURRENT_TIMESTAMP columns, producing invalid SQL in the generated sync script.
- **실패 시나리오**: Table with `created_at datetime DEFAULT CURRENT_TIMESTAMP` diffed against a target: SyncScriptGenerator (used live by diff_dialog.py:968) emits `MODIFY COLUMN `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP DEFAULT_GENERATED;` — running the script fails with a syntax error. Cross-version diffs (5.7 EXTRA='' vs 8.0 'DEFAULT_GENERATED') also produce spurious MODIFIED column diffs.
- **수정 힌트**: Strip 'DEFAULT_GENERATED' from EXTRA before emitting/comparing (keep 'on update CURRENT_TIMESTAMP' and 'auto_increment'); normalize EXTRA in _get_columns.
- **검증**: Confirmed. schema_diff.py:337 stores INFORMATION_SCHEMA EXTRA verbatim (extra=row['EXTRA'] or '') and to_sql_definition (lines 91-92) appends it unfiltered. MySQL 8 reports EXTRA='DEFAULT_GENERATED' for DEFAULT CURRENT_TIMESTAMP columns — the repo itself acknowledges this: migration_fix_wizard.py:1704-1717 (_format_extra_clause) explicitly strips DEFAULT_GENERATED and whitelists only AUTO_INCREMEN

### [migration-core-b] src/core/schema_diff.py:465 — duplication
- **요약**: SchemaComparator/SyncScriptGenerator fully reimplement schema diffing in Python while the Rust-core facade exposes a `schema.diff` command (db_core_service.py:231) that is never called anywhere — two diff implementations that can drift.
- **실패 시나리오**: A diff-semantics fix (e.g. rename detection or severity rules) lands in the Rust core's schema.diff but the UI keeps using the Python comparator (diff_dialog.py:21-24), so users see stale/inconsistent results; conversely the dead facade method rots untested. This is exactly the drift the Rust-core-owns-DB-ops baseline is meant to prevent.
- **수정 힌트**: Either route diff_dialog through DbCoreFacade.schema_diff and reduce Python to presentation/severity, or delete the unused facade method and document the Python comparator as the single owner.

### [migration-core-b] src/core/schema_diff.py:1212 — correctness
- **요약**: _generate_create_table builds PRIMARY KEY column order from column ORDINAL_POSITION (col.key == 'PRI') instead of the index's SEQ_IN_INDEX order, producing a wrong composite-PK order in generated CREATE TABLE statements.
- **실패 시나리오**: Source table declares PRIMARY KEY (`code`, `tenant_id`) where tenant_id precedes code in column order; the sync script emits PRIMARY KEY (`tenant_id`, `code`). Uniqueness is preserved but the clustered index leading column changes, silently degrading queries that relied on the original leftmost prefix.
- **수정 힌트**: Use the PRIMARY entry from table.indexes (already ordered by SEQ_IN_INDEX in _get_indexes) instead of scanning col.key, and drop the separate pk_cols path.

### [migration-core-c] src/core/migration_auto_recommend.py:254 — dead-code
- **요약**: AutoRecommendationEngine (and the ~200-line DEFAULT_RECOMMENDATION_RULES table) is never instantiated by any production code — recommendation selection now happens inside the Rust core One-Click workflow — but it remains exported and test-maintained.
- **실패 시나리오**: Grep across src/ shows the only references are the class definition, the src/core/__init__.py:28 export, and tests/test_migration_auto_recommend.py. When a new IssueType gets different recommended handling in Rust core, this Python table silently diverges; anyone updating recommendations here (the obvious-looking place) changes nothing user-visible, and the dead rule table plus its risk-score logic must still be kept in sync with migration_fix_wizard/migration_constants to keep imports and tests passing.
- **수정 힌트**: Delete the module and its test file, or clearly quarantine it as reference data consumed by the Rust core build; remove the __init__ export either way.

### [migration-core-c] src/core/migration_preflight.py:65 — dead-code
- **요약**: PreflightChecker (raw-SQL preflight over the connector shim) has no production caller — preflight is owned by Rust core via facade.run_oneclick — yet the class stays exported in src/core/__init__.py and maintained by tests, guaranteeing drift from the Rust implementation.
- **실패 시나리오**: oneclick_migration_dialog.py imports only the PreflightResult/CheckResult/CheckSeverity dataclasses to render Rust core events; the checker's SHOW GRANTS/SHOW PROCESSLIST/information_schema logic (with its known bugs, see separate findings) never executes in the app. PreflightResult.estimated_time (line 45) is never populated by any production path and estimate_time() is exercised only by tests. Future maintainers reading src/core/__init__.py:27 will reasonably assume this is the live preflight and fix bugs in the wrong place while the Rust-core behavior silently differs.
- **수정 힌트**: Split the event-rendering dataclasses (PreflightResult/CheckResult/CheckSeverity) into a small module kept for the dialog, and delete PreflightChecker + tests, per the CLAUDE.md rule against retired legacy paths.

### [migration-core-c] src/core/migration_state_tracker.py:59 — dead-code
- **요약**: The entire MigrationStateTracker persistence/resume machinery is unwired: no production code ever calls create_state/mark_step_completed/can_resume/list_incomplete_migrations, so migration state is never persisted and cross-session resume cannot happen.
- **실패 시나리오**: User's One-Click migration is interrupted mid-EXECUTION (app crash/power loss). On restart nothing reads or offers the tracker state because no state file was ever written; oneclick_migration_dialog.py:23-24 imports MigrationStateTracker/MigrationState/get_state_tracker but only ever uses MigrationPhase (the in-dialog 'resume_execution' at line 77 is just an in-session threading.Event gate). Meanwhile the 421-line module plus tests/test_migration_state_tracker.py (~750 lines) are maintained for code that can never run, and it duplicates resume ownership that CLAUDE.md assigns to Rust core.
- **수정 힌트**: Either wire the tracker into the oneclick worker (create_state at start, mark_step_completed per step, startup can_resume prompt) or delete the module + its test file and drop the unused imports in oneclick_migration_dialog.py; keep only MigrationPhase if that constant set is still needed.

### [migration-core-c] src/core/migration_validator.py:79 — dead-code
- **요약**: PostMigrationValidator's analysis half — validate(), _issue_key(), check_data_integrity(), quick_validate(), _get_analyzer() — has no production caller; the class is only ever instantiated as PostMigrationValidator(None) to call the two report-export methods.
- **실패 시나리오**: oneclick_migration_dialog.py:597/614 constructs the validator with connector=None purely for export_report_html/export_report_json (which never touch self.connector). If anyone ever calls validate() or check_data_integrity() on such an instance it crashes inside MigrationAnalyzer with a None connector. The dead validation logic (including the broken quick_validate, see separate finding) is maintained and re-verified by tests while the real post-migration validation runs in Rust core. Mixed concerns: one class is half retired analyzer, half live report renderer.
- **수정 힌트**: Extract export_report_html/export_report_json (plus the MigrationReport dataclass) into a report-rendering module with no connector dependency; delete the unreachable validation methods.

### [migration-ui-a] src/ui/dialogs/cross_engine_migration_dialog.py:721 — architecture
- **요약**: _is_target_non_empty_issue drives the safety-step gating and cleanup-option visibility by substring-matching English fragments ('not empty', 'non-empty', 'existing') of human-readable Rust core messages, a fragile cross-component contract.
- **실패 시나리오**: tunnelforge-core rewords or localizes the target-not-empty preflight message (e.g., 'target schema already contains 12 tables' without the word 'existing', or a Korean message). _update_target_safety_from_issues then returns False: the '고급 설정' cleanup panel never appears, the user is told there is no target-blocking issue while preflight keeps failing, and the cleanup-before-migrate recovery path becomes unreachable from the UI.
- **수정 힌트**: Have the Rust core emit a stable machine-readable issue code (e.g., code='target_not_empty') and match on that instead of message text.

### [migration-ui-a] src/ui/dialogs/cross_engine_migration_dialog.py:993 — correctness
- **요약**: _save_checkpoint (and _on_result's migrate branch at line 1000) rebuild the resume-state key from live UI via self._payload() instead of the payload the worker was started with; inputs are not disabled during runs, so mid-run edits cause key drift or an uncaught ValueError that silently stops checkpointing.
- **실패 시나리오**: During a long migrate, the user edits the advanced schema JSON (txt_schema is editable while running) into invalid JSON: every checkpoint signal raises ValueError inside the slot and no resume state is saved; after a failure, '중단 지점부터 재개' reports no saved state. Alternatively editing endpoint/schema fields mid-run changes state_key_from_payload's key, so the checkpoint is saved under a key the later resume lookup never finds. Also each checkpoint re-parses the full schema JSON on the main thread (per-row-progress cost on large schemas).
- **수정 힌트**: Capture the payload/state key once in _start_command_with_payload and reuse it for checkpoints and result-state saves; optionally disable endpoint forms and txt_schema while a worker runs.

### [migration-ui-a] src/ui/dialogs/cross_engine_migration_dialog.py:1055 — concurrency
- **요약**: The _pending_after_inspect chain is scheduled with QTimer.singleShot(0) from _on_result, which can fire before the worker's finished signal is delivered (the worker emits result, then still reads to EOF/wait()/stderr before emitting finished), so _start_command sees worker.isRunning() and drops the pending command with a spurious modal warning.
- **실패 시나리오**: Schema JSON is empty and a command that requires it (e.g., guide/preflight via a code path or future button) auto-triggers inspect. Inspect's result event arrives and the 0ms timer fires while the inspect thread is still tearing down; _start_command pops '이미 실행 중인 작업이 있습니다' and _pending_after_inspect was already cleared at line 1054, so the originally requested command silently never runs.
- **수정 힌트**: Move the pending-command dispatch into _on_finished (after self.worker = None), mirroring how the full-workflow chain is dispatched.

### [migration-ui-a] src/ui/dialogs/fix_wizard_dialog.py:106 — concurrency
- **요약**: closeEvent's worker.quit() is a no-op (FixWizardWorker.run has no event loop and no cancel flag), so wait(3000) always times out mid-run and terminate() can kill the thread while it holds DbCoreFacade._lock, deadlocking every later DB call through the shared connector.
- **실패 시나리오**: User closes the wizard while a dry-run is executing. quit() does nothing, wait(3000) elapses (3s UI freeze), terminate() strikes while the worker is inside a facade request (db_core_service.py:153 'with self._lock'). threading.Lock held by a killed thread is never released; the connector was passed in from the parent UI, so the next schema/SQL operation anywhere in the app blocks forever.
- **수정 힌트**: Add a cooperative cancel flag to FixWizardWorker checked between steps, and avoid terminate(); or give the worker its own connector/facade instance so a kill cannot poison the shared one.

### [migration-ui-a] src/ui/dialogs/fix_wizard_dialog.py:484 — correctness ✅검증확정
- **요약**: CharsetFixPage.initializePage returns early when charset issues were deselected, leaving stale table_infos that validatePage then saves into charset_tables_to_fix, so the SQL preview/dry-run includes charset ALTER statements for tables the user explicitly removed.
- **실패 시나리오**: User selects charset issues on page 1 -> Next (CharsetFixPage populates table_infos) -> Back -> unchecks all charset issues -> Next. initializePage hits 'if not has_charset_issues(): return' without clearing table_infos; validatePage (line 753) computes charset_tables_to_fix from the stale list; PreviewPage.generate_sql_preview (line 1614) emits Part 1 FK-safe charset SQL for those tables and the dry-run runs against them. The wizard hands the user wrong ALTER TABLE ... CONVERT TO CHARACTER SET SQL for manual execution — data-change risk.
- **수정 힌트**: In initializePage's early-return branch (and/or validatePage), clear self.table_infos, the checkbox widgets, and wizard_dialog.charset_tables_to_fix when has_charset_issues() is False.
- **검증**: Fully traced and confirmed in src/ui/dialogs/fix_wizard_dialog.py. (1) Reachable path: IssueSelectionPage has no nextId(), so CharsetFixPage is always shown next (pages added in order, lines 123-127); after Back + deselecting charset issues (keeping >=1 other issue so Next stays enabled), IssueSelectionPage.validatePage sets charset_issues=[] and charset_plan_builder=None (lines 363-381). (2) The 

### [migration-ui-a] src/ui/dialogs/fix_wizard_dialog.py:717 — performance
- **요약**: CharsetFixPage.update_stats runs a synchronous INFORMATION_SCHEMA FK join query (FKSafeCharsetChanger.get_related_fks) on the GUI thread on every checkbox toggle/select-all, and PreviewPage.generate_sql_preview (line 1626) similarly runs DB queries during initializePage, freezing the UI.
- **실패 시나리오**: Schema with many tables over a slow SSH tunnel: each checkbox click blocks the event loop for the duration of the information_schema join. If the user navigates Back during a running dry-run and toggles a checkbox, the GUI-thread query additionally blocks on DbCoreFacade._lock until the worker's in-flight request completes, freezing the whole window.
- **수정 힌트**: Debounce/update stats from cached FK data collected once by CharsetFixPlanBuilder, or move the FK count query into a small worker.

### [migration-ui-a] src/ui/workers/cross_engine_migration_worker.py:60 — concurrency
- **요약**: The helper process is spawned with stderr=subprocess.PIPE but stderr is only read after stdout EOF (line 105), so a chatty helper can fill the ~64KB stderr pipe buffer and deadlock both processes mid-migration.
- **실패 시나리오**: tunnelforge-core logs verbose diagnostics to stderr during a long migrate; once the stderr pipe buffer fills, the core blocks on stderr write and stops producing stdout events; the worker blocks forever on 'for line in self._process.stdout'. The dialog shows the operation as running indefinitely with no failure signal until the user guesses to press 취소.
- **수정 힌트**: Drain stderr concurrently (reader thread) or pass stderr=subprocess.DEVNULL / redirect to a temp file and read it after exit.

### [migration-ui-b] src/ui/dialogs/diff_dialog.py:537 — performance
- **요약**: _load_schemas performs a synchronous MySQLConnector connect + get_schemas on the GUI thread on every tunnel-combo change (and twice at dialog open), each spawning a fresh tunnelforge-core subprocess.
- **실패 시나리오**: A tunnel is 'running' but the remote DB behind it is slow or hung: selecting that tunnel in the combo blocks the event loop for the full connect timeout (multi-second UI freeze, window shows not-responding). Every combo flick also pays process-spawn + connect latency.
- **수정 힌트**: Load schemas in a small QThread/worker (pattern already exists for the compare itself) and reuse a connector per tunnel instead of one per call.

### [migration-ui-b] src/ui/dialogs/diff_dialog.py:593 — performance
- **요약**: _start_compare overwrites _source_connector/_target_connector on every compare without disconnecting the previous ones, leaking open DB connections and their per-connector tunnelforge-core subprocesses (each MySQLConnector creates its own DbCoreFacade/DbCoreServiceClient which spawns a dedicated core process).
- **실패 시나리오**: User runs compare 5 times in one dialog session: 8 stale connectors with open server-side connections and up to 8 orphaned tunnelforge-core processes accumulate (released only nondeterministically by GC or at app exit); closeEvent only disconnects the last pair.
- **수정 힌트**: Disconnect existing connectors at the top of _start_compare before creating new ones (and consider a per-connector facade shutdown on disconnect).

### [migration-ui-b] src/ui/dialogs/diff_dialog.py:967 — correctness
- **요약**: _generate_script reads the target schema from the still-editable combo instead of the schema actually used for the comparison, so the generated DDL can target a different schema than the diffs describe.
- **실패 시나리오**: User compares dev_a → prod_a, then (combos are re-enabled in _on_compare_finished) switches the target schema combo to prod_b and clicks '동기화 스크립트 생성': the script prefixes prod_b with ALTER/CREATE statements derived from prod_a's diff — executing it would corrupt the wrong schema.
- **수정 힌트**: Capture source/target schema names at compare start (alongside _diffs) and use the stored value in _generate_script; or disable schema combos until a new compare invalidates the result.

### [migration-ui-b] src/ui/dialogs/diff_dialog.py:975 — concurrency
- **요약**: closeEvent disconnects both connectors while SchemaCompareThread may still be actively using them, and never cancels/waits for the thread.
- **실패 시나리오**: User closes the dialog mid-compare on a large schema: disconnect() → connection.close() → facade request blocks on the client lock until the thread's in-flight query completes, freezing the GUI thread inside closeEvent; once closed, the thread's next query fails with connection-not-found, firing error → _on_compare_error pops a '비교 오류' message box for a dialog the user already closed, and the thread keeps running in the background.
- **수정 힌트**: In closeEvent, if _compare_thread.isRunning(), request cancellation / wait for it (as the other dialogs at least attempt), and only disconnect connectors after the thread has stopped.

### [migration-ui-b] src/ui/dialogs/migration_dialogs.py:94 — concurrency
- **요약**: closeEvent falls back to worker.terminate() for analysis/cleanup workers blocked in the facade request; the killed thread leaves the connector's client lock held, and MigrationWizard.start's finally connector.disconnect() then blocks the UI thread forever.
- **실패 시나리오**: User closes the analyzer dialog while a long analysis of a large schema is running and confirms; quit() is a no-op, wait(3000) expires, terminate() kills the thread inside 'with self._lock'. Control returns to MigrationWizard.start whose finally calls connector.disconnect() → facade request blocks on the orphaned lock → the app hangs permanently on close.
- **수정 힌트**: Do not terminate threads blocked in facade requests; detach and let them finish (or add protocol cancellation), and perform the final disconnect from a background thread or via the worker's finished signal.

### [migration-ui-b] src/ui/dialogs/migration_dialogs.py:811 — performance
- **요약**: update_fk_tree re-instantiates MigrationAnalyzer and calls get_fk_visualization(schema) synchronously on the UI thread, re-running FK queries against the live DB even though the worker already delivered result.fk_tree.
- **실패 시나리오**: On a schema with many FKs, the GUI freezes at the end of every analysis while the FK info is re-queried. Worse, load_analysis_result() also calls this: loading a saved JSON from another server/schema queries the *current* connection for that schema name, so the ASCII pane can show 'FK 관계가 없습니다' (or a different server's tree) directly contradicting the loaded fk_tree rendered above it.
- **수정 힌트**: Build the ASCII visualization from the fk_tree dict already passed in (pure function), removing both the UI-thread DB call and the loaded-result inconsistency.

### [migration-ui-b] src/ui/dialogs/migration_dialogs.py:1495 — correctness
- **요약**: Markdown-to-HTML conversion is broken: content.replace('**', '<b>').replace('**', '</b>') — the first replace consumes every '**', so the second is a no-op and no </b> is ever produced; non-'sql' code fences also both map to '</pre>'.
- **실패 시나리오**: User opens 수동 처리 가이드 and selects the auth-plugin issue: every bold marker becomes an unclosed <b>, so the rest of the guide renders entirely bold, and the plain ``` fence around the my.cnf snippet becomes a stray closing </pre>, producing visibly mangled guide text.
- **수정 힌트**: Use a real minimal converter (regex pairing \*\*(.+?)\*\* -> <b>\1</b>, and pair fences) or render plain text.

### [migration-ui-b] src/ui/dialogs/oneclick_migration_dialog.py:328 — correctness
- **요약**: PreflightWidget.update_result maps legacy Python checker names ('권한 검사', '디스크 공간 검사', ...) but the Rust core emits check names 'MySQL engine', 'Backup status', 'Schema inspect' (migration_core/src/lib.rs oneclick_preflight_state), so no check row would ever update from '⏳'.
- **실패 시나리오**: Currently masked because the preflight page is never shown (stack stays on execution_widget), but as soon as the wizard pages are re-wired, every preflight row stays as pending spinner regardless of results; the widget also hardcodes 5 checks (permissions/disk/connections/backup/version) that the core no longer performs.
- **수정 힌트**: Drive the check rows dynamically from the names in the core's preflight event instead of a hardcoded Korean-name mapping tied to the retired Python PreflightChecker.

### [migration-ui-b] src/ui/dialogs/oneclick_migration_dialog.py:983 — concurrency
- **요약**: closeEvent calls worker.terminate() on a thread that is blocked in DbCoreServiceClient.request() while holding the client's threading.Lock; termination leaves the lock held forever, permanently deadlocking the connector shared with the parent MigrationAnalyzerDialog.
- **실패 시나리오**: User closes the dialog mid-migration and confirms. quit() is a no-op (run() has no event loop), wait(3000) times out because request() is blocked on readline(), terminate() kills the thread mid-'with self._lock'. The lock is never released, so the next facade call on the same connector — e.g., re-running analysis in MigrationAnalyzerDialog or MigrationWizard.start's finally connector.disconnect() — blocks the GUI thread forever, freezing the app. The Rust core also keeps executing the migration.
- **수정 힌트**: Never terminate() a thread blocked in the facade; instead detach (finished-signal cleanup) and let it complete, or add protocol-level cancellation. Same pattern exists at src/ui/dialogs/migration_dialogs.py:94.

### [migration-ui-b] src/ui/dialogs/oneclick_migration_dialog.py:1021 — correctness ✅검증확정
- **요약**: The Phase-3 execution-plan confirmation gate is completely disconnected: start_requested is never connected to _on_start_execution_confirmed, the stack never shows execution_plan_widget (nor preflight/analysis widgets), and the worker's _execution_gate is never waited on — so non-dry-run execution proceeds without the designed plan-review pause.
- **실패 시나리오**: User runs with Dry-run unchecked. execution_plan_ready fires (docstring: 'Phase 3 완료 후 일시 정지'), _on_execution_plan_ready only writes a log line, and the Rust core immediately continues into real DDL execution. The '실행 계획 확인' screen with the '▶ 실행 시작' button is unreachable, so the user never reviews or approves which tables will be altered before they are altered.
- **수정 힌트**: Connect execution_plan_widget.start_requested to _on_start_execution_confirmed, switch the stack to the plan widget on execution_plan_ready, and make the workflow actually pause (gate.wait() plus a core-side pause point), or delete ExecutionPlanWidget/_execution_gate/resume_execution/_on_start_execution_confirmed as retired design.
- **검증**: Every factual claim verified against the code. (1) execution_plan_widget.start_requested is never connected to _on_start_execution_confirmed — start_migration() connects only worker signals (oneclick_migration_dialog.py:945-951); repo-wide grep confirms no other connection, so line 1021 is dead code. (2) stack.setCurrentWidget is only ever called with execution_widget and result_widget; the '실행 계획

### [sql-editor] src/core/db_core_service.py:712 — correctness
- **요약**: query_returns_rows uses lstrip().startswith(...) so any statement beginning with a SQL comment is classified as non-row-returning; parse_sql_statements preserves leading comments in statement text, making this a common case.
- **실패 시나리오**: `-- check\nSELECT ...` returning 0 rows: query_returns_rows False -> description None -> the sql editor's write branch adds it to pending_queries ('미커밋 변경 1건'), same user-visible bug as the empty-description case via a different route. In SQLQueryWorker the same query bypasses the streaming path (sql_editor_dialog.py:808) and, at 0 rows, is reported as '0행 영향받음' with connection.commit() called. rowcount semantics also flip (row count vs rows_affected).
- **수정 힌트**: Strip leading `--`, `#`, and `/* */` comments before the startswith check (share one classify helper with the dialog).

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2495 — correctness ✅검증확정
- **요약**: 0-row SELECT/SHOW/DESCRIBE/EXPLAIN is misclassified as a write query because `if db_cursor.description:` is falsy for the empty-list description that RustDbCursor.execute (db_core_service.py:661) sets for row-returning queries with 0 rows.
- **실패 시나리오**: In default (non-autocommit) mode, run `SELECT * FROM t WHERE 1=0` or `SHOW TABLES` on an empty schema: description=[] -> falsy -> write branch at line 2508 runs; the read query is appended to pending_queries (line 2520), history saved as status='pending', tx panel shows '미커밋 변경: 1건' with commit/rollback enabled, and no result grid with column headers is shown. Sibling copy of the same check exists at line 942 (dead SQLTransactionWorker).
- **수정 힌트**: Branch on `db_cursor.description is not None` (or on query_returns_rows), and long-term have the Rust core return column names so description is never an empty list for row-returning queries.
- **검증**: Confirmed by full code trace. RustDbCursor.execute (src/core/db_core_service.py:658-663) sets description=[] (empty list) for row-returning queries (query_returns_rows: select/with/show/desc/describe/explain, line 711-713) that return 0 rows, since the facade's execute_on_connection_result returns rows=[] and no column metadata. At src/ui/dialogs/sql_editor_dialog.py:2495 the check is `if db_curso

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2808 — correctness
- **요약**: _apply_limit only applies the auto-LIMIT to queries that literally start with SELECT, so CTE queries (WITH ... SELECT) and comment-prefixed SELECTs silently escape the row cap.
- **실패 시나리오**: User keeps the default LIMIT 1000 and runs `WITH big AS (SELECT * FROM events) SELECT * FROM big` on a 10M-row table: no LIMIT is appended, all rows are fetched and materialized into a QTableWidget on the GUI thread, freezing or OOM-ing the app — precisely what the LIMIT feature exists to prevent.
- **수정 힌트**: Treat WITH (and comment-prefixed SELECT/WITH) as limitable, or apply the cap client-side by truncating fetched rows with a warning.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2832 — correctness
- **요약**: _apply_limit detects an existing LIMIT only via the literal substring ' LIMIT ' (space-delimited), so a LIMIT preceded by a newline or tab is missed and a second LIMIT is appended, producing a syntax error.
- **실패 시나리오**: Common formatted query `SELECT *\nFROM t\nLIMIT 10` -> ' LIMIT ' not found in clean_query (newline before LIMIT) -> becomes `... LIMIT 10 LIMIT 1000` -> engine syntax error on a query that was valid. Also, a trailing line comment (`SELECT 1 -- note`) swallows the appended LIMIT into the comment, silently disabling the row cap.
- **수정 힌트**: Use a regex like re.search(r'\bLIMIT\b', clean_query) on the string-stripped text, and append LIMIT on a new line (or strip trailing line comments first).

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:3112 — correctness
- **요약**: self.temp_server is shared between the persistent transaction connection and autocommit runs: _cleanup() closes it after any autocommit worker finishes, which can sever the tunnel under the live transaction connection, and _resolve_db_target(keep_temp_tunnel=True) overwrites it, leaking earlier tunnels.
- **실패 시나리오**: Tunnel not running; transaction-mode query creates temp tunnel T1 (temp_server=T1) with the persistent connection over it and uncommitted pending queries. User later starts the main tunnel, checks 자동 커밋, runs a query (uses the running tunnel, temp_server untouched) -> _on_finished -> _cleanup closes T1 -> the persistent connection dies with uncommitted work; db_connection.open still reads True so _ensure_connection keeps returning the dead connection. In the overwrite variant, T1 simply leaks forever (never closed even in closeEvent).
- **수정 힌트**: Give the persistent connection its own tunnel handle separate from per-run temp tunnels; close each temp tunnel where it was created.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:3811 — concurrency
- **요약**: closeEvent accepts the close with SQLQueryWorker still running — no cancel/wait — then _cleanup() closes the temp tunnel out from under the mid-batch worker; the QThread can also be destroyed while running when the dialog is garbage-collected.
- **실패 시나리오**: User runs a 10-statement autocommit batch, closes the dialog mid-run and confirms: _cleanup closes the tunnel, remaining statements fail invisibly (dialog gone) leaving the batch half-applied with no report; if the dialog object is released after exec() returns, Qt aborts with 'QThread: Destroyed while thread is still running'.
- **수정 힌트**: On confirmed close, request worker interruption and wait() (or at least worker.wait() before _cleanup), and only tear down the tunnel after the thread has stopped.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:3822 — correctness
- **요약**: Uncommitted cell edits are silently destroyed: closeEvent's loss warning only covers pending_queries and modified editor tabs, and _clear_result_tabs (called unconditionally by F5 at line 2458) and close_result_tab drop tabs holding pending_edits without any confirmation.
- **실패 시나리오**: User edits 20 cells in a result grid (tx panel shows '셀 편집 20건'), then presses F5 to run another query — _clear_result_tabs removes the tab and all edits vanish without prompt. Similarly, closing the dialog with only cell edits (no pending DML) shows no warning at all; the edits are lost.
- **수정 힌트**: Include _collect_all_pending_edits() in the closeEvent warning list and prompt before _clear_result_tabs/close_result_tab when the affected tabs have pending_edits.

## LOW (56건)

### [core-db] src/core/db_core_service.py:188 — concurrency
- **요약**: DbCoreServiceClient.shutdown() mutates self._process = None outside self._lock, racing with concurrent request() calls that can respawn a process mid-shutdown and then crash on the None handle.
- **실패 시나리오**: User closes the app (atexit fires shutdown_shared_db_core_facade) while a QThread worker is issuing a request. After shutdown's service.shutdown turn releases the lock, the worker's request() acquires it, sees poll() != None, start()s a fresh Rust process and begins a request; shutdown's finally block then sets self._process = None, and the worker dies with AttributeError/AssertionError while the freshly spawned tunnelforge-core is orphaned.
- **수정 힌트**: Hold self._lock while swapping/clearing self._process in shutdown(), and have request() operate on a local process reference captured under the lock.

### [core-db] src/core/db_core_service.py:471 — error-handling
- **요약**: RustDbConnector's metadata methods (get_schemas:471, get_column_names:526, get_db_version_string:501, schema_exists:446) swallow all exceptions and return empty defaults, making auth/tunnel failures indistinguishable from genuinely empty results.
- **실패 시나리오**: A user's DB password expires mid-session; the scheduler/SQL-editor schema pickers silently show empty schema lists and validators skip validation ('no metadata'), with no error surfaced anywhere and nothing logged (unlike MySQLConnector, which at least logger.error()s). The user sees a mysteriously empty UI instead of an authentication error.
- **수정 힌트**: Log the exception at minimum, and consider letting connection-level errors (DbCoreServiceError) propagate to callers that can surface them, keeping empty-return only for 'object not found' cases.

### [core-db] src/core/db_core_service.py:568 — correctness
- **요약**: RustDbConnection.ping() only checks the local 'open' flag and never round-trips to the server, so every health check built on it (pool _validate_connection, MySQLConnector.is_connected) reports stale/dead connections as healthy.
- **실패 시나리오**: SSH tunnel restarts or the server drops the session; conn.open is still True so ping() succeeds. If the pool were ever enabled, _validate_connection would hand the dead connection out and the caller's first query fails with a confusing Rust-core error instead of the pool transparently replacing the connection.
- **수정 힌트**: Implement ping as a cheap round-trip (e.g., facade.execute_on_connection(id, 'SELECT 1')) or add a dedicated connection.ping command to the Rust core.

### [core-db] src/core/db_core_service.py:661 — correctness
- **요약**: RustDbCursor sets description=[] (falsy) for a row-returning query with zero rows, diverging from DB-API where a SELECT always yields non-empty column descriptions; 'if cursor.description:' consumers treat empty result sets as DML.
- **실패 시나리오**: A zero-row SELECT executed through a cursor path guarded by 'if cursor.description:' (sql_editor_dialog.py:832/942/2495, scheduler.py:832) falls into the DML branch: no column headers are shown / columns=[] is exported, and in the editor path connection.commit() runs on a plain SELECT. The Rust protocol cannot supply column names when zero rows return, so consumers silently lose the distinction between 'empty result set' and 'statement without results'.
- **수정 힌트**: Have the Rust core return column metadata alongside rows (even for empty results) and populate description from it; short of that, use None-vs-list consistently and audit consumers to test 'description is not None'.

### [core-db] src/core/db_core_service.py:683 — dead-code
- **요약**: bind_sql_params()/sql_literal() are defined but have zero callers anywhere in the repo, and they embody the client-side string-interpolation SQL binding the Rust-core baseline forbids (sql_literal also mis-escapes backslashes for PostgreSQL standard_conforming_strings, and sequential '%s' replacement corrupts queries when a bound value itself contains '%s').
- **실패 시나리오**: Dead today, but the first developer who reaches for bind_sql_params("SELECT %s, %s", ["a%sb", "x"]) gets "SELECT 'a'x'b', %s" — the second parameter is substituted inside the first literal — and PostgreSQL backslash values are silently doubled. The Rust core already does real parameter binding, so this helper is a loaded footgun.
- **수정 힌트**: Delete bind_sql_params and sql_literal (keep quote_mysql_ident which select_db uses).

### [core-db] src/core/sql_validator.py:628 — duplication
- **요약**: _extract_table_aliases is copy-pasted verbatim in SQLValidator (line 435) and SQLAutoCompleter (line 628) within the same file — same regex, same keyword stop-list, maintained twice.
- **실패 시나리오**: A fix to alias extraction (e.g., adding USING/ORDER keywords or supporting quoted identifiers with spaces) applied to one copy leaves validation and autocompletion disagreeing about which alias maps to which table, producing inconsistent squiggles vs completions for the same query.
- **수정 힌트**: Extract a module-level extract_table_aliases(sql, metadata) function and call it from both classes.

### [cross-cutting] src/core/i18n.py:1066 — clean-code
- **요약**: _EN_PHRASE_TRANSLATIONS contains 6 duplicate literal keys, 3 with conflicting values where the later entry silently wins.
- **실패 시나리오**: '항목' (line 780 'Item' vs 1066 'items'), '사용 중' (980 'In Use' vs 1098 'in use'), '자동 커밋' (486 'Auto Commit' vs 1067 'auto commit'): in English mode, table headers and checkbox labels sourced from the first entries render with the later lowercase/plural variants (e.g. the migration-analyzer overview column header shows 'items' instead of 'Item'). Three more dups ('체크 해제 시...', '백업 목록', '기본 스키마') are same-value noise that masks real conflicts in review.
- **수정 힌트**: Deduplicate the dict (an AST duplicate-key check in CI would keep it clean) and pick one casing per phrase.

### [cross-cutting] src/core/tunnel_monitor.py:446 — concurrency
- **요약**: _attempt_reconnect mutates shared TunnelStatus fields (state, reconnect_count, error_message at lines 433-446) without self._lock, while other threads read/write the same object under the lock.
- **실패 시나리오**: Monitor loop thread runs _attempt_reconnect while a spawned reconnect thread (line 505) concurrently updates the same status under the lock (483-497) and the GUI thread reads it via get_status()/get_all_statuses(): torn state such as state=RECONNECTING with reconnect_count already reset, or a lost increment causing one extra reconnect attempt beyond _max_reconnect_attempts. Impact is limited to status display and retry accounting, but the mixed locked/unlocked discipline makes future edits error-prone.
- **수정 힌트**: Wrap the mutations at lines 431-451 in `with self._lock:` (the lock is not held by callers on the monitor-loop path, and the recursive call at line 495 would need the lock released first or an RLock).

### [cross-cutting] src/ui/dialogs/sql_editor_dialog.py:3637 — error-handling
- **요약**: Swallowed-error sweep: 35 `except Exception: pass` sites across src (238 broad handlers in 49 files); most are cleanup paths, but a few drop user-visible failures silently — e.g. metadata connector connect failure returns with no UI feedback.
- **실패 시나리오**: In _reload_metadata, `success, _ = connector.connect(); if not success: return` (3636-3638) silently aborts: autocomplete/validation metadata never loads and the user gets no message explaining why (validation_label is only updated on the success path at 3651 or on exceptions at 3654). Hotspot files: sql_editor_dialog.py (26 broad handlers, 9 pass), db_connector.py (16, 4 pass — all disconnect/cleanup), db_dialogs.py (11). scheduler.py's 15 handlers all log, so scheduled-job failures are NOT silently lost. No bare `except:` exists anywhere in src.
- **수정 힌트**: For the metadata path, surface the connect failure in validation_label/message_text; during refactors, replace cleanup `except Exception: pass` with `logger.debug(...)` so real teardown failures remain observable.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:2287 — clean-code
- **요약**: btn_import.clicked.connect(self.do_import) passes Qt's checked=False into the retry_tables parameter, so normal imports run with retry_tables=False (a bool) instead of None.
- **실패 시나리오**: Currently harmless because every consumer uses truthiness (`if not retry_tables:` at 2639, `if retry_tables:` in rust_dump_exporter.py:653/679), but any future change to `is not None` semantics (e.g., treating an empty list as 'retry nothing') silently breaks the primary import path; the type annotation (list = None) also lies about what actually arrives.
- **수정 힌트**: Connect via lambda: self.btn_import.clicked.connect(lambda: self.do_import()) or add a dedicated slot.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:2406 — correctness
- **요약**: The MySQL 8.4 compatibility check only runs from the browse button; the prefilled default dump dir (_load_default_input_dir) and manually typed paths never get checked, despite the label promising automatic checking on selection.
- **실패 시나리오**: User opens the import dialog, the last dump dir is auto-filled, and they click Import directly: the advisory compatibility scan never runs, the status label still reads 'Dump 폴더를 선택하면 자동 검사됩니다', and known-incompatible dumps proceed without the warning the feature was built to give — the most common path (re-importing the last dump) is exactly the one that skips the check.
- **수정 힌트**: Call _run_upgrade_check when _load_default_input_dir sets a valid path and on input_dir.editingFinished.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:2528 — dead-code
- **요약**: set_ui_enabled contains a duplicated consecutive line self.radio_recreate.setEnabled(enabled) (lines 2527 and 2528).
- **실패 시나리오**: No functional impact today, but the duplicate strongly suggests a widget was meant to be listed and got copy-pasted over; a future widget added to this state machine may be silently missed the same way (note chk_use_original-driven combo state is already handled specially just above).
- **수정 힌트**: Delete the duplicate line.

### [db-dialogs] src/ui/dialogs/db_dialogs.py:3176 — clean-code
- **요약**: Import save_log labels an in-progress log 'failed' (import_success is None -> falsy), whereas the export copy was fixed to emit 'running' for the None case (lines 1758-1761) — another export/import copy divergence.
- **실패 시나리오**: User saves the log while an import is still running (button becomes enabled via _add_log paths): the file is named import_log_<schema>_failed_<ts>.txt and later triage misreads a healthy in-progress run as a failure.
- **수정 힌트**: Port the export dialog's three-state handling (running/success/failed) to the import save_log.

### [export-workers] src/exporters/rust_dump_exporter.py:321 — duplication
- **요약**: ForeignKeyResolver.resolve_required_tables is dead in production (only tests call it) and duplicates the FK transitive-closure algorithm reimplemented in RustDumpExporter._resolve_required_tables_from_rust_schema (lines 384-395), which is what export_tables actually uses; CLAUDE.md still documents ForeignKeyResolver as the partial-export mechanism. RustDumpConfig.get_uri (line 81) is likewise unused and returns the password in plaintext.
- **실패 시나리오**: A future fix to FK-parent resolution (e.g., handling schema-qualified referenced tables) gets applied to ForeignKeyResolver.resolve_required_tables because docs point there, passes its unit tests, and silently never affects real exports which use the Rust-schema variant.
- **수정 힌트**: Remove resolve_required_tables (and get_uri), update tests to target _resolve_required_tables_from_rust_schema, and correct the CLAUDE.md description.

### [export-workers] src/ui/workers/metadata_worker.py:12 — dead-code
- **요약**: The entire metadata_worker.py module (MetadataWorker, BatchMetadataWorker, ConnectionTestWorkerAsync — 198 lines) is defined and re-exported in workers/__init__.py but never instantiated anywhere in production code.
- **실패 시나리오**: Maintenance cost only: the module carries stale patterns (BatchMetadataWorker emits all_loaded with a partial dict after stop(); ConnectionTestWorkerAsync builds a MySQLConnector directly) that a future developer may copy believing it is the sanctioned worker pattern.
- **수정 힌트**: Delete the module and its __init__.py exports, or wire it into the dialogs that were meant to use it.

### [export-workers] src/ui/workers/migration_worker.py:123 — dead-code
- **요약**: CleanupWorker's constructor raises unless dry_run=True, yet run() still carries both branches of the retired execution mode ('[실행]' labels, dry_run conditionals at lines 123/129/145); FixWizardWorker has the same guarded-but-kept legacy at fix_wizard_worker.py:97.
- **실패 시나리오**: Retired legacy per the Rust-core baseline: a reader (or a hasty revert) sees the '[실행]' path and dry_run parameter and assumes Python-side DB mutation is still a supported mode, reintroducing the forbidden direct-mutation path the constructor guard was meant to retire.
- **수정 힌트**: Drop the dry_run parameter and the '[실행]' branches entirely; hardcode dry-run semantics in these workers since mutations are owned by Rust core.

### [infra-core] src/core/connection_pool.py:102 — correctness
- **요약**: ConnectionPool._validate_connection relies on RustDbConnection.ping, which never contacts the database (it only raises if the local 'open' flag is False), so pool validation and idle-death detection are no-ops and dead server-side connections are handed out as valid.
- **실패 시나리오**: MySQL server restarts (or wait_timeout kills the session). Pooled RustDbConnection still has open=True -> ping 'succeeds' -> get_connection returns the dead connection -> the caller's first query fails with a connection error the pool was supposed to prevent. Also _create_connection hardcodes engine='mysql' (line 82), so the pool can never serve PostgreSQL endpoints.
- **수정 힌트**: Implement a real liveness check (SELECT 1 via the facade) in _validate_connection, and parameterize the engine.

### [infra-core] src/core/github_issue_reporter.py:440 — dead-code
- **요약**: The 401/403 token-refresh retry in report_error is unreachable: find_similar_issue, create_issue, and add_comment all catch requests.RequestException internally and return status tuples, so no RequestException ever propagates to report_error.
- **실패 시나리오**: A GitHubIssueReporter instance is kept past the 1-hour installation-token expiry; create_issue gets a 401, catches it, and returns (False, '이슈 생성 실패: 401...'). report_error's except requests.RequestException block (and the force refresh) never executes, so the documented '4. 401/403 시 토큰 갱신 후 1회 재시도' behavior does not exist.
- **수정 힌트**: Either let the low-level helpers re-raise auth errors, or inspect the returned message/status and trigger _refresh_headers_if_needed(force=True) + one retry in _do_report.

### [infra-core] src/core/scheduler.py:155 — correctness
- **요약**: CronParser silently drops day-of-week value 7 (the ubiquitous cron alias for Sunday) because parse_field range-filters to 0-6, so an enabled schedule with '* * * * 7' computes no next_run and never fires without any error.
- **실패 시나리오**: User enters '0 3 * * 7' expecting Sunday 03:00 (valid in vixie-cron). parse_field returns [] -> get_next_run scans 366 days and returns None -> schedule stays enabled with stale/empty next_run and is skipped forever by _run_loop line 402; no warning is shown. (Also note: day-of-month and day-of-week are combined with AND, whereas standard cron uses OR when both are restricted.)
- **수정 힌트**: Map 7 -> 0 for the DOW field and surface a validation error in the schedule dialog when a field parses to an empty set.

### [infra-core] src/core/scheduler.py:438 — correctness ✅검증확정
- **요약**: Scheduled tasks pass a string tunnel_id to TunnelEngine.start_tunnel which requires a config dict, so auto-starting a tunnel always crashes with TypeError.
- **실패 시나리오**: A backup schedule fires at 03:00 while its tunnel is disconnected. _execute_backup calls self.tunnel_engine.start_tunnel(schedule.tunnel_id); start_tunnel does tid = config['id'] on a str -> TypeError('string indices must be integers'), caught by the outer except and reported as '백업 오류: string indices...'. Every scheduled backup/SQL task whose tunnel is not already connected fails. Same defect at line 684 in _execute_sql_query. Every other caller (main_window.py:747 etc.) passes the full config dict.
- **수정 힌트**: Look up the tunnel config dict (e.g., from config_manager.load_config()['tunnels'] or tunnel_engine.tunnel_configs) and pass it to start_tunnel; add a test that runs a schedule with a stopped tunnel.
- **검증**: Code defect confirmed exactly as described: scheduler.py:438 (_execute_backup) and scheduler.py:684 (_execute_sql_query) pass schedule.tunnel_id (declared str at scheduler.py:39) to TunnelEngine.start_tunnel, which does tid = config['id'] at tunnel_engine.py:87 -> TypeError('string indices must be integers') on a str, caught by the outer except at scheduler.py:548 and reported as '백업 오류: ...'. All

### [infra-core] src/core/scheduler.py:691 — duplication
- **요약**: _execute_backup and _execute_sql_query contain ~50 copy-pasted lines of tunnel-start / conn_info / credential resolution that have already diverged (the SQL copy skips get_tunnel_credentials, producing the HIGH empty-password bug).
- **실패 시나리오**: Any future fix to connection resolution (e.g., the tunnel_id/start_tunnel bug, or the (None, None) guard) must be applied in two places; the existing divergence shows one copy gets missed, reintroducing auth/connect failures in whichever task type wasn't touched.
- **수정 힌트**: Extract a single _resolve_connection(schedule) -> (host, port, user, password, engine) helper used by both task types.

### [infra-core] src/core/scheduler.py:710 — correctness ✅검증확정
- **요약**: _execute_sql_query resolves the DB password from config.get('db_password'), a key that never exists in tunnel configs (stored key is 'db_password_encrypted'), so scheduled SQL tasks always connect with an empty password.
- **실패 시나리오**: User creates an SQL-query schedule against a password-protected MySQL/PostgreSQL DB. Tunnel configs only ever contain db_user + db_password_encrypted (main_window.py:683-691). Line 710 yields password='' -> connector.connect() fails with access denied for every run. _execute_backup (lines 462-471) correctly decrypts via config_manager.get_tunnel_credentials; the SQL path copy diverged and skipped it. Also config.get('db_username') at 704/709 never exists.
- **수정 힌트**: Use config_manager.get_tunnel_credentials(schedule.tunnel_id) in _execute_sql_query exactly like _execute_backup, or extract one shared credential-resolution helper.
- **검증**: The technical mechanics of the finding are CONFIRMED end-to-end; the only material mitigation is that the entire scheduler feature is currently disabled behind a hardcoded feature flag, which downgrades present-day severity.

Confirmed claims:
1. src/core/scheduler.py:710 reads exactly `password = config.get('db_password') or ''` (and line 705 in the dict branch likewise). No call to `config_manag

### [main-ui] src/ui/dialogs/settings.py:260 — correctness
- **요약**: Theme combo's currentIndexChanged is connected before setCurrentIndex, so merely opening Settings re-applies and re-saves the theme, and because _on_theme_changed persists immediately (set_theme save=True), pressing 취소 does not revert a previewed theme.
- **실패 시나리오**: User with Dark theme opens Settings: setCurrentIndex(index) at line 272 fires _on_theme_changed -> set_theme(DARK, save=True) -> full app stylesheet reapplied and config rewritten on every dialog open. User then previews Light and clicks 취소: theme stays Light and is already saved to disk - Cancel does not cancel, contradicting the dialog's Save/Cancel semantics.
- **수정 힌트**: Connect currentIndexChanged after the initial setCurrentIndex, preview with set_theme(save=False), persist in save_settings, and restore the original theme on reject().

### [main-ui] src/ui/dialogs/tunnel_config.py:433 — duplication
- **요약**: Identical TempConfigManager class is defined twice, inline inside _test_db_only (line 433) and _test_integrated (line 481).
- **실패 시나리오**: A future change to credential-resolution behavior (e.g. decrypt failure handling) gets applied to one copy only; the two test flows then resolve credentials differently and diverge silently - classic copy-paste maintenance trap in security-adjacent code.
- **수정 힌트**: Hoist a single module-level _TempCredentials class (or a small closure/factory) used by both tests.

### [main-ui] src/ui/main_window.py:1181 — dead-code
- **요약**: show_context_menu is unreachable table-era legacy: it is never connected (the tree has its own _show_context_menu), calls self.table.rowAt() which does not exist on QTreeWidget, and indexes self.tunnels by visual row which is wrong under grouping.
- **실패 시나리오**: No current caller, but anyone re-wiring context menus to this method gets an immediate AttributeError (QTreeWidget has no rowAt), and even if fixed, self.tunnels[row] maps visual tree rows (including group headers) onto the flat tunnel list, opening edit/delete/export against the wrong tunnel. run_sql_file (line 1243) reachable only from here behind a disabled flag is likewise dead.
- **수정 힌트**: Delete show_context_menu (and consider removing the self.table alias plus the dead run_sql_file/SQL_FILE_EXECUTION_FEATURE_ENABLED path).

### [migration-core-a] src/core/migration_analyzer.py:563 — correctness
- **요약**: Orphan cleanup SQL uses `NOT IN (SELECT parent_col ...)` which is not NULL-safe, while detection uses NULL-safe NOT EXISTS/LEFT JOIN, so detection and cleanup can disagree.
- **실패 시나리오**: The FK-referenced parent column is a nullable UNIQUE key containing at least one NULL row. find_orphan_records reports N orphans (LEFT JOIN/NOT EXISTS semantics), but the generated DELETE/SET_NULL uses `child_col NOT IN (SELECT parent_col ...)` which evaluates to UNKNOWN for every row once the subquery contains a NULL — the dry-run count in execute_cleanup shows 0 and the statement, if run, changes nothing, contradicting the reported orphan_count without explanation.
- **수정 힌트**: Generate cleanup SQL with the same NOT EXISTS predicate used for detection, or add `AND parent_col IS NOT NULL` inside the subquery.

### [migration-core-a] src/core/migration_analyzer.py:632 — correctness
- **요약**: execute_cleanup dry-run derives the table name with naive `split('UPDATE')[1].split('SET')[0]` string surgery, which breaks for table names containing the uppercase substring 'SET' (or 'FROM' in the DELETE branch).
- **실패 시나리오**: A SET_NULL cleanup on a table named `SETTINGS` or `ASSETS`: action.sql is "UPDATE `sch`.`ASSETS`\nSET `col` = NULL\nWHERE ..."; split('SET')[0] yields "`sch`.`AS", producing a malformed COUNT query. connector.execute raises, and since execute_cleanup has no try/except the exception propagates to the worker, aborting the dry-run for that action with a confusing SQL syntax error.
- **수정 힌트**: Store schema/table on CleanupAction when it is generated (they are known at generate_cleanup_sql time) instead of re-parsing the SQL text.

### [migration-core-a] src/core/migration_analyzer.py:1053 — dead-code
- **요약**: check_int_display_width is defined but never called — analyze_schema's 14-check pipeline omits it, so live analysis never emits INT_DISPLAY_WIDTH issues even though the fix wizard ships a dedicated handler for them.
- **실패 시나리오**: SmartFixGenerator registers _get_int_display_width_options for IssueType.INT_DISPLAY_WIDTH (migration_fix_wizard.py line 157), but grep shows no caller of check_int_display_width anywhere in src/, so that wizard path is unreachable from live-DB analysis; future maintainers will assume the check runs because both the method and its UI handler exist.
- **수정 힌트**: Either add it to _analyze_schema_impl behind a check_int_display_width flag (it is info-severity, cheap INFORMATION_SCHEMA query) or delete both the check and the wizard handler.

### [migration-core-a] src/core/migration_analyzer.py:1213 — correctness
- **요약**: check_timestamp_range looks for TIMESTAMP values greater than '2038-01-19 03:14:07', but MySQL TIMESTAMP columns physically cannot store values beyond that UTC limit, so the check is a near-guaranteed no-op that reports '✅ TIMESTAMP 범위 정상'.
- **실패 시나리오**: A schema whose application logic will overflow TIMESTAMP after 2038 is scanned: since out-of-range values were never storable, COUNT(*) is always 0 (except a narrow session-timezone display window east of UTC), the check finds nothing, and the report gives false confidence that the 2038 problem was assessed. The full-table scan cost per TIMESTAMP column is paid for nothing.
- **수정 힌트**: Either drop the data scan and instead flag TIMESTAMP columns themselves (schema-level advisory to migrate to DATETIME), or check values approaching the limit (e.g. > '2037-01-01').

### [migration-core-a] src/core/migration_fix_wizard.py:1210 — correctness
- **요약**: In dry-run mode the FK-safe batch stores per-step result message 'FK 안전 변경 완료 (배치)', discarding the DRY-RUN summary in fk_msg, so the results table implies the change was actually executed.
- **실패 시나리오**: User runs the wizard preview (the only mode still allowed). For FK-safe charset steps, fk_success is always True in the dry_run branch (line 1190), so every step's FixExecutionResult.message reads 'FK 안전 변경 완료 (배치)' and the log prints '✅ 클러스터 완료', while other step types correctly show '[DRY-RUN] ...' messages. A user scanning the results can believe the charset conversion already happened.
- **수정 힌트**: Use fk_msg (which already contains 'DRY-RUN: N개 FK, M개 테이블 변경 예정') as the success message when dry_run is True.

### [migration-core-a] src/core/migration_fix_wizard.py:1748 — duplication
- **요약**: RollbackSQLGenerator._get_fk_sql_for_tables re-implements FKSafeCharsetChanger.get_related_fks + FKDefinition SQL generation as a divergent copy that lacks the BASE TABLE (VIEW-exclusion) join filter added to the original.
- **실패 시나리오**: The two FK enumerations already disagree: get_related_fks (lines 788-797) filters t_child/t_parent to TABLE_TYPE='BASE TABLE', while the copy at 1758-1776 does not, and it groups by table.constraint instead of constraint alone. The next fix to FK SQL generation (e.g., schema-qualified REFERENCES, new join condition) will land in one copy and silently miss the other, making rollback FK SQL differ from the forward FK SQL it must mirror.
- **수정 힌트**: Have RollbackSQLGenerator reuse FKSafeCharsetChanger.get_related_fks / FKDefinition.get_drop_sql/get_add_sql instead of the private duplicate (or delete it together with the dead rollback path).

### [migration-core-b] src/core/migration_constants.py:262 — clean-code
- **요약**: The comment claims ENGINE_POLICIES is the 'single source' shared with storage_rules.py, but storage_rules.check_deprecated_engines never imports it and hardcodes severity='warning' — so MERGE is reported as warning there while ENGINE_POLICIES rates it 'error'; CSV/EXAMPLE/NDB listed in STORAGE_ENGINE_STATUS have no policy entry at all.
- **실패 시나리오**: A MERGE-engine table is reported with 'warning' severity from the live-DB storage rule path but 'error' from the analyzer path using ENGINE_POLICIES (migration_analyzer.py:1135), giving contradictory reports for the same table; adding a new deprecated engine to one table silently leaves the other stale.
- **수정 힌트**: Make storage_rules read severity/suggestion from ENGINE_POLICIES (with a default for unlisted engines) and derive STORAGE_ENGINE_STATUS['deprecated'] from ENGINE_POLICIES keys.

### [migration-core-b] src/core/migration_constants.py:372 — dead-code
- **요약**: IssueType.TRIGGER_OLD_SYNTAX and EVENT_OLD_SYNTAX are never referenced by any rule, while schema_rules.py's module docstring advertises S14-S15/S19-S22 checks that do not exist in that class, and migration_parsers.py's docstring (lines 8-9) promises ConfigFileParser/DumpMetadataParser classes that exist nowhere in the repo.
- **실패 시나리오**: A maintainer auditing rule coverage against the docstrings concludes trigger/event syntax and config/metadata parsing are implemented and skips adding them; the unused enum members suggest coverage that was never built.
- **수정 힌트**: Delete the unused enum members and stale docstring lines, or implement the promised checks; keep docstrings enumerating only rules actually present in the module.

### [migration-core-b] src/core/migration_rules/data_rules.py:79 — correctness
- **요약**: check_enum_empty_value_definition's condition `"''" in column_type or ", ''" in ... or ",''" in ...` — the first term subsumes the others (dead conditions) and also matches MySQL's escaped quotes inside enum values, falsely flagging enums containing apostrophes.
- **실패 시나리오**: COLUMN_TYPE enum('don''t','other') (MySQL renders an embedded apostrophe as '') contains "''" → false error 'ENUM에 빈 문자열 정의됨' for a perfectly valid enum. Verified the condition evaluates True for this input.
- **수정 힌트**: Reuse _extract_enum_elements (which already handles '' escapes correctly) and flag only when an extracted element == ''.

### [migration-core-b] src/core/migration_rules/data_rules.py:700 — performance
- **요약**: check_zerofill_data_dependency's batch query appends LIMIT 100 to a GROUP-BY-less aggregate (which returns one row regardless), so the intended row-scan bound does nothing and each ZEROFILL table gets a full scan with per-row CAST.
- **실패 시나리오**: A 100M-row table with a ZEROFILL column causes a full table scan computing LENGTH(CAST(col AS CHAR)) for every row during the compatibility check, stalling the migration analysis (the progress callback shows the step hanging) despite the LIMIT suggesting bounded work.
- **수정 힌트**: Bound the scan in a subquery: SELECT MAX(...) FROM (SELECT col FROM t LIMIT 100000) sub, or use WHERE ... LIMIT 1 existence probes per column.

### [migration-core-b] src/core/migration_rules/data_rules.py:766 — correctness
- **요약**: check_invalid_datetime counts the same zero-date twice: once via the per-line INVALID_DATE/DATETIME check and again via the INVALID_DATE_VALUES_PATTERN finditer loop, inflating the reported count.
- **실패 시나리오**: A line containing one '0000-00-00' increments invalid_count at line 767 and again at line 775 (the value matches '0000-\d{2}-\d{2}' too), so the report claims twice as many bad rows as exist, undermining trust in the counts.
- **수정 힌트**: Use a single pattern set per line (e.g. only the finditer loop over a combined pattern) or dedupe matched spans per line.

### [migration-core-c] src/core/migration_auto_recommend.py:420 — correctness
- **요약**: _is_column_nullable treats an empty query result as 'NOT NULL' and caches it, but MySQLConnector.execute swallows all exceptions and returns [] (db_connector.py:284-286), so a transient query failure is indistinguishable from a real NOT NULL column.
- **실패 시나리오**: During recommendation the nullability lookup hits a dropped connection → execute returns [] → is_nullable=False is cached for the column → _recommend_invalid_date picks DATE_TO_MIN (overwrite invalid dates with a sentinel value) instead of DATE_TO_NULL for a genuinely nullable column, silently changing data semantics. Latent — the engine currently has no production caller.
- **수정 힌트**: Distinguish 'no row' from query failure (have the lookup raise or return None on error) and fall back to the safe is_recommended/default option instead of assuming NOT NULL.

### [migration-core-c] src/core/migration_preflight.py:274 — correctness
- **요약**: check_active_connections claims to exclude the current connection (comment at line 271) but never filters by CONNECTION_ID(), so the checker's own session counts as an active connection whenever its database equals the target schema.
- **실패 시나리오**: Connector is opened with database=<schema> (the normal case); SHOW PROCESSLIST returns the checker's own row with db=<schema> and Command='Query' (it is executing the PROCESSLIST query, not sleeping), so the filter at line 274 matches it and the check always reports '활성 연결 1개 발견' with a spurious warning even on an otherwise idle server. Latent only — the class currently has no production caller.
- **수정 힌트**: Fetch CONNECTION_ID() first and add p.get('Id') != current_id to the filter.

### [migration-core-c] src/core/migration_preflight.py:432 — correctness
- **요약**: Grant parsing produces false positives: the 'ALL PRIVILEGES' branch (line 432) ignores the ON target so a grant on a different schema satisfies the check for any schema, and the substring test 'priv in grant_upper' (lines 438-440, 445-447) lets 'ALTER ROUTINE' satisfy the required 'ALTER' privilege.
- **실패 시나리오**: If this checker is ever re-wired: a user with GRANT ALL PRIVILEGES ON `otherdb`.* (but read-only on the target schema), or with only ALTER ROUTINE on the target, passes the permission preflight; the migration then dies midway on a denied ALTER TABLE after some DDL has already been applied. Currently unreachable in production (PreflightChecker is dead code) but kept green by tests, so the bug will survive until someone reuses the class.
- **수정 힌트**: Scope the ALL PRIVILEGES branch to ON *.* or the target schema, and tokenize the privilege list (split on commas) instead of substring matching.

### [migration-core-c] src/core/migration_report.py:17 — dead-code
- **요약**: ReportExporter has no production caller: its only consumer is MigrationAnalyzer.export_report (migration_analyzer.py:2087), which itself is never invoked from any UI or worker code.
- **실패 시나리오**: The 372-line exporter (JSON/CSV/upgrade-check/SQL/HTML) plus tests/test_migration_report.py are maintained while the shipped report path is the Rust-core-generated report rendered via PostMigrationValidator's exporters; format changes made here never reach users, and its export_html shares the unescaped-interpolation flaw noted for migration_validator.py.
- **수정 힌트**: Confirm no external/CLI entry point uses it, then remove it together with MigrationAnalyzer.export_report, or wire it into the analyzer dialog's export button if that was the intent.

### [migration-core-c] src/core/migration_state_tracker.py:79 — correctness
- **요약**: Schema-to-filename sanitization maps every non-[alnum_-] character to '_', so distinct schemas collide onto the same state file (e.g. 'a.b' vs 'a_b'; also 'DB' vs 'db' on Windows' case-insensitive filesystem).
- **실패 시나리오**: If the tracker is ever wired: migrations of schemas 'shop.v2' and 'shop_v2' (or 'Shop' and 'shop' on Windows) read/write migration_state_shop_v2.json interchangeably — resume for one schema silently loads the other's completed_steps/pending_steps and would skip steps that were never executed on that schema.
- **수정 힌트**: Append a short hash of the raw schema name to the sanitized filename to guarantee uniqueness.

### [migration-core-c] src/core/migration_state_tracker.py:227 — correctness
- **요약**: mark_step_completed (and update_phase/set_error) mutate the cached MigrationState object in place before save_state; if the disk write fails, the method returns False but the in-memory cache permanently retains the unpersisted mutation.
- **실패 시나리오**: Disk full or file locked during save: save_state logs the error and returns False, but load_state (cache-first, line 124) keeps returning the mutated object, so get_progress reports the step as completed for the rest of the session while the on-disk state does not — memory and disk silently diverge, and after a restart the resume bookkeeping contradicts what the UI showed. save_state also stamps last_updated (line 97) even on failure. Latent — the tracker is currently unwired.
- **수정 힌트**: Mutate a copy (dataclasses.replace) and only install it into the cache after the file write succeeds, or roll back the mutation when save_state returns False.

### [migration-core-c] src/core/migration_validator.py:255 — security
- **요약**: export_report_html interpolates DB-derived strings (location, description, suggestion at lines 252-259, and the execution log at line 391) into the HTML report without any escaping — and this path IS production-reachable via the oneclick dialog's HTML download button.
- **실패 시나리오**: A table/column name, enum value, or error message coming back from the migrated schema contains markup, e.g. a column literally named <img src=x onerror=alert(1)> or a log line with <script>. oneclick_migration_dialog.py:598 writes the report; the user double-clicks the .html file and the injected markup renders/executes in their browser, or at minimum corrupts the report layout. The same unescaped pattern exists in migration_report.py export_html (lines 307-313), though that exporter currently has no production caller.
- **수정 힌트**: Run every interpolated issue field and log line through html.escape() in both exporters.

### [migration-core-c] src/core/migration_validator.py:482 — correctness
- **요약**: quick_validate's pass condition `current_count == 0 or expected_fixes > 0` is tautologically true whenever any fixes were expected, so validation 'passes' even when every issue remains unfixed.
- **실패 시나리오**: expected_fixes=10, migration actually fixed nothing, re-analysis finds current_issue_count=10 → validation_passed=True because expected_fixes > 0. Any future caller relying on this quick check would report success on a completely failed migration. No production caller today (dead method), but the logic is plainly inverted from its intent.
- **수정 힌트**: Something like validation_passed = (current_count == 0) or (pre_count - current_count >= expected_fixes); or delete the method with the rest of the dead validation half.

### [migration-ui-a] src/ui/dialogs/cross_engine_migration_dialog.py:838 — clean-code
- **요약**: _next_hint_text's generic 'step complete, you can move to the next step' branch shadows the execute-step hint: when approval is typed but migration has not run, the hint claims navigation while the Next button actually launches the destructive DB migration with no further confirmation dialog.
- **실패 시나리오**: On the execute step the user types the target schema name; _next_enabled_for_current_step() becomes True via _can_start_migration_from_execute_step, so the hint reads '현재 단계가 완료되었습니다. 다음 단계로 이동할 수 있습니다.' A user trusting the hint clicks Next expecting navigation, and because _confirm_migration_execution returns True immediately when approval matches, the migration starts against the target DB without any confirmation prompt.
- **수정 힌트**: Check the execute-step migration-pending state before the generic completed branch, and word the hint as 'DB 변경 실행 버튼을 누르면 전환이 시작됩니다.'

### [migration-ui-a] src/ui/dialogs/cross_engine_migration_dialog.py:1112 — dead-code
- **요약**: The finished_command == 'cleanup' branches (lines 1112-1128) and _reset_command_ui's 'cleanup' handling are unreachable: no code path ever starts command='cleanup' (btn_cleanup_failed only toggles chk_cleanup_before_migrate), and FULL_MIGRATION_WORKFLOW never includes it; likewise btn_preflight/btn_readiness (line 357-358) and btn_migrate are created and wired but never added to any layout, so they are permanently invisible orphan widgets whose enabled-state is still maintained.
- **실패 시나리오**: A maintainer 'fixing' cleanup UX edits the dead _on_finished branch and ships a change that can never execute; or wires new behavior to btn_migrate believing it is the visible execute button (the real trigger is btn_next in _go_next_step), producing changes that never surface in the UI.
- **수정 힌트**: Delete the cleanup command branches or actually implement _start_command('cleanup'); remove the orphan buttons or add them to a layout.

### [migration-ui-a] src/ui/dialogs/fix_wizard_dialog.py:341 — correctness
- **요약**: IssueSelectionPage.isComplete counts checked-but-filter-hidden rows while update_count and validatePage exclude them, so the Next button can stay enabled when the visible selection count is 0 and the wizard proceeds with an empty issue set.
- **실패 시나리오**: User checks only warning-severity issues, then unchecks the 'Warning' filter: the label shows '선택: 0개' but Next remains enabled; validatePage collects an empty selection, charset/other issues are both empty, and the user walks through empty wizard pages ending in '(실행할 SQL이 없습니다)'.
- **수정 힌트**: Make isComplete use the same visible-and-checked predicate as update_count/validatePage.

### [migration-ui-a] src/ui/dialogs/fix_wizard_dialog.py:1013 — dead-code
- **요약**: FixOptionPage's entire FK-cascade/auto-include machinery is unreachable: _fk_graph_builder is only ever assigned None (so _update_fk_tree never shows), and COLLATION_FK_CASCADE options are only generated for CHARSET_ISSUE which never reaches this page since create_wizard_steps receives only other_issues after the CharsetFixPage refactor.
- **실패 시나리오**: included_by is always None for every step, so _mark_related_tables_as_included/_unmark_included_tables, IncludedTablesDialog, btn_show_included, the included-skip navigation in prev_issue/next_issue, and the '자동 포함' progress text (~250 lines) can never execute; the FK tree widget never displays. Maintainers reading this page reason about behaviors that cannot occur, and behavior drift in this dead code goes untested forever.
- **수정 힌트**: Remove the FK-cascade auto-include paths, the fk_tree widgets, and the CollationFKGraphBuilder import from this page; keep only plain per-issue option selection.

### [migration-ui-b] src/ui/dialogs/migration_dialogs.py:792 — correctness
- **요약**: FK tree rendering only starts from root tables (keys minus all children), so tables that participate only in cycles — including the very common self-referencing FK (e.g., employee.manager_id → employee.id) — are silently omitted from the tree widget.
- **실패 시나리오**: Schema whose only FK is self-referential: fk_tree = {'A': ['A']}, all_children = {'A'}, root_tables = ∅ → the FK tab shows an empty tree while the overview reports 1 FK relation, confusing the user.
- **수정 힌트**: After rendering roots, render any fk_tree keys not yet visited as additional top-level items (cycle entry points).

### [migration-ui-b] src/ui/dialogs/migration_dialogs.py:954 — dead-code
- **요약**: The '실제 실행 시 확인' confirmation block (lines 953-964) is unreachable: execute_cleanup already returns early at lines 940-946 whenever dry_run is False.
- **실패 시나리오**: No runtime failure today, but when someone re-enables real cleanup for the Rust core they may assume the double-confirmation still guards execution while restructuring the early return, silently losing the irreversible-action prompt.
- **수정 힌트**: Delete the unreachable block or restructure so the disabled-execution gate and the confirmation prompt are one explicit code path.

### [migration-ui-b] src/ui/dialogs/oneclick_migration_dialog.py:53 — clean-code
- **요약**: Custom signal 'finished = pyqtSignal(bool, object)' shadows QThread's built-in finished signal (same in diff_dialog.py:36 SchemaCompareThread), a classic PyQt trap.
- **실패 시나리오**: Anyone later connecting worker.finished expecting thread-completion semantics (e.g., worker.finished.connect(worker.deleteLater)) gets the custom signal, which is not emitted when the thread is terminated or exits abnormally — cleanup silently never runs; conversely the built-in no-arg emission is invisible to slots expecting (bool, object).
- **수정 힌트**: Rename to a distinct signal (e.g., migration_finished / compare_finished).

### [migration-ui-b] src/ui/dialogs/oneclick_migration_dialog.py:255 — dead-code
- **요약**: _create_empty_report() is never called and _pre_issues (line 69) is assigned but never read — leftovers from the pre-Rust-core Python workflow.
- **실패 시나리오**: No runtime failure; misleads maintainers into thinking the Python side still builds reports/issues, increasing the chance someone reintroduces Python-side migration logic against the Rust-core baseline.
- **수정 힌트**: Delete _create_empty_report and _pre_issues.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:820 — correctness
- **요약**: The streaming SELECT path derives columns from the first row, so a 0-row SELECT emits columns=[] and _on_query_result's `elif columns:` (line 2849) routes it to the write-result branch — autocommit-mode empty SELECTs display as '0행 영향받음' with no result grid or headers.
- **실패 시나리오**: Autocommit mode, `SELECT * FROM t WHERE id=-1`: no batches arrive, columns=[], user sees '✅ 쿼리 1: 0행 영향받음' as if it were an UPDATE, and cannot see the table's column headers to refine the query.
- **수정 힌트**: Have the Rust core streaming protocol deliver column metadata independent of rows (or fall back to a describe call), and branch _on_query_result on 'is row-returning' rather than columns truthiness.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:880 — dead-code
- **요약**: SQLTransactionWorker (lines 880-1034) is never instantiated anywhere in the repo — 155 lines of dead legacy including its own _get_query_type (line 1006, duplicating SQLEditorDialog._get_query_type at 2598), a busy-wait commit loop, and a copy of the description-truthiness bug (line 942); _is_modification_query (line 2878) is likewise never called.
- **실패 시나리오**: No runtime failure, but real maintenance cost: the seed misclassification bug exists in three copies (942, 832, 2495); anyone fixing one from a grep hit can land the fix in the dead class and believe the live default-mode path is fixed.
- **수정 힌트**: Delete SQLTransactionWorker and _is_modification_query; keep a single shared query-classification helper.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:2592 — correctness
- **요약**: Autocommit mode records the entire SQL text into history as a single success entry (success=True, 0 rows) before execution even starts, so the history feature shows successes for batches that entirely failed.
- **실패 시나리오**: User runs 3 queries in autocommit mode and all fail with syntax errors; the HistoryDialog nevertheless lists the batch with a success icon, and per-query outcomes/errors from _on_query_result are never recorded — history search by '실패만' misses these runs.
- **수정 힌트**: Record per-query history from _on_query_result (mirroring the transaction path) instead of pre-logging the whole text as success.

### [sql-editor] src/ui/dialogs/sql_editor_dialog.py:3281 — error-handling
- **요약**: _fetch_primary_keys uses MySQL-only INFORMATION_SCHEMA.COLUMNS.COLUMN_KEY and swallows the resulting error with a blanket `except Exception: return []`, so on PostgreSQL cell editing is silently and permanently disabled with no diagnostic; the edit UPDATE builder (line 3430) also emits backtick-quoted MySQL identifiers only.
- **실패 시나리오**: PostgreSQL connection: every editability probe raises (COLUMN_KEY column does not exist), is swallowed, and every result grid becomes '읽기 전용' with no hint why; if PK detection were ever fixed for PG, the backtick-quoted UPDATE statements would then fail at commit.
- **수정 힌트**: Branch PK lookup per engine (pg_index/information_schema.key_column_usage for PG), quote identifiers via the existing _quote_editor_identifier, and log the swallowed exception.

## 검증에서 기각된 발견 (5건)

- src/ui/dialogs/db_dialogs.py:2600 — ProductionGuard confirmation runs only when self.tunnel_config is set, but the wizard passes tunnel_config only on the preselected-tunnel path, so imports started from the menu (DBConnectionDialog) bypass the production guard entirely.
  - 기각 사유: The code mechanism is correctly described: db_dialogs.py:2600 gates ProductionGuard on `if self.tunnel_config:`, and RustDumpWizard.start_import (db_dialogs.py:3389) passes tunnel_config=self.preselected_tunnel, so the DBConnectionDialog branch yields tunnel_config=None and skips the guard; DBConnec
- src/core/migration_rules/data_rules.py:574 — check_latin1_non_ascii builds REGEXP '[^\x00-\x7F]' with a single backslash; MySQL string-literal parsing drops the backslash, so the server sees the class [^x00-x7F], which matches spaces and punctuation.
  - 기각 사유: The escaping analysis in the finding is technically correct: data_rules.py:574/579 emit REGEXP '[^\x00-\x7F]' with a single backslash (Python '\\x00' -> runtime '\x00'), MySQL string-literal parsing drops the unrecognized '\x' escape, and the regex engine receives [^x00-x7F], whose negated class {x,
- src/ui/dialogs/tunnel_config.py:400 — ConnectionTestWorker QThread is held only in a local variable and the modal TestProgressDialog can be dismissed with Esc mid-test, causing the running QThread to be garbage-collected and the app to crash.
  - 기각 사유: Premises verified: tunnel_config.py:399-404/458-462/505-509 do hold a parentless ConnectionTestWorker (QThread subclass, test_worker.py:18) only in a local variable, and TestProgressDialog (test_dialogs.py:332) only strips WindowCloseButtonHint without overriding keyPressEvent/reject, so Esc does re
- src/ui/dialogs/schedule_dialog.py:970 — '즉시 실행' calls scheduler.run_now() synchronously on the GUI thread, which runs a full Rust dump export (or the scheduled SQL batch) inline and freezes the entire UI for its duration.
  - 기각 사유: The technical analysis is accurate: schedule_dialog.py:970 calls scheduler.run_now() directly in the button-click slot, and the chain run_now -> _execute_task -> _execute_backup (scheduler.py:370/382/424) -> RustDumpExporter.export_full_schema -> DbCoreFacade.run_dump -> DbCoreServiceClient.request 
- src/ui/main_window.py:966 — _on_backup_complete calls tray_icon.showMessage directly from the BackupScheduler background thread, violating Qt's GUI-thread-only rule.
  - 기각 사유: The finding's mechanical description is accurate but the path is unreachable in the current codebase, so the claimed failure scenario cannot occur.

What checks out: `_on_backup_complete` (src/ui/main_window.py:963-978) does call `self.tray_icon.showMessage()` directly with no thread marshaling; it 
