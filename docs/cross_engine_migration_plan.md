# MySQL <-> PostgreSQL Migration Plan

## Objective

TunnelForge must run MySQL <-> PostgreSQL migration inside the app, with preflight, schema conversion, chunked data copy, resume, full row verification, cancellation, and user-visible reports.

## Success Criteria

1. The app can migrate MySQL to PostgreSQL and PostgreSQL to MySQL without requiring an external migration tool.
2. The Rust helper owns heavy work: inspect, preflight, plan, migrate, verify, resume.
3. Python/PyQt owns UI, cancellation, progress display, and report export.
4. Default mode is `create_only`; target tables that already contain data block execution.
5. Data moves in chunks, default `chunk_size` is 5000 rows.
6. Interrupted table-level work can resume from saved state.
7. Verification compares every table count and every row:
   - key-based cell comparison when PK or unique key exists
   - canonical SHA-256 row digest with duplicate counts when no key exists
8. Unsupported objects such as views, triggers, routines, users, and grants are warning/report-only in v1.
9. Tests exist before implementation for every non-trivial rule.

## TDD Scenario Matrix

| Scenario | Test Layer | Expected Behavior |
| --- | --- | --- |
| MySQL -> PostgreSQL type mapping | Rust unit | DDL maps MySQL column types to valid PostgreSQL types. |
| PostgreSQL -> MySQL type mapping | Rust unit | DDL maps PostgreSQL column types to valid MySQL types. |
| bidirectional readiness check | Rust/Python unit + optional integration | One app action checks MySQL -> PostgreSQL and PostgreSQL -> MySQL readiness separately. |
| detailed per-direction guide | Rust/Python unit + optional integration | Each direction returns DDL, follow-up SQL, column mappings, row value samples, and target INSERT examples. |
| generated key preservation | Rust unit + optional integration | MySQL AUTO_INCREMENT and PostgreSQL identity/serial columns migrate and continue generating IDs after data copy. |
| default value preservation | Rust unit + optional integration | Common string, numeric, boolean, and temporal defaults are emitted in target DDL and work for post-migration inserts. |
| temporal type round trip | Rust unit/integration | DATE, TIME, DATETIME/TIMESTAMP values compare equal across engine text formats. |
| decimal text equivalence | Rust unit/integration | DECIMAL/NUMERIC values compare by numeric text meaning, not display scale. |
| create_only target not empty | Rust unit/integration | Preflight/executor returns blocking issue and no rows are copied. |
| live create_only preflight | optional integration | Preflight checks the real target database and blocks non-empty target tables before execution. |
| chunked copy | Rust unit | Executor reads source rows by chunk and writes all rows to target. |
| cancellation between chunks | Rust unit | Executor stops after current checkpoint and returns resumable state. |
| resume after partial table | Rust unit | Executor skips completed rows/tables and continues from checkpoint. |
| large chunk resume | Rust unit | 12,037 rows resume correctly after two 5,000-row chunks. |
| streaming checkpoint events | Rust/Python unit | Helper emits chunk progress before final result and UI saves checkpoint state for cancel/resume. |
| keyed verification mismatch | Rust unit | Reports table, key, column, source value, and target value sample. |
| no-key verification mismatch | Rust unit | Reports digest and duplicate count mismatch. |
| helper JSONL progress | Python unit | Worker converts helper events to Qt signals. |
| helper binary JSONL smoke | Rust integration | Built helper binary accepts stdin JSONL and emits result JSONL. |
| helper process failure | Python unit | Worker emits failed/finished without freezing UI. |
| dialog export/report state | Python unit | Report/export controls enable only after result and save a text report. |
| dialog resume state | Python unit | Saved migration state is loaded back into a resume migrate command. |
| destructive execution confirmation | Python unit | Migrate execution is blocked until the user confirms target DB changes. |
| unsupported object reporting | Rust/Python unit + optional integration | Views/triggers/routines are report-only warnings; users/grants are explicit report-only warnings. |
| real DB round trip | optional integration | Minimal schema with boolean, binary, decimal, temporal, and index migrates both directions and verifies cleanly. |

## Work Plan

### Phase 1: Core Contract

- Split Rust helper into a library plus thin JSONL binary.
- Define normalized schema, table, column, index, constraint, issue, plan, and resume state models.
- Implement SQL-safe identifier quoting and cross-engine type mapping.
- Keep current JSONL protocol stable.

### Phase 2: Deterministic Executor

- Add a database adapter trait.
- Implement in-memory adapter tests for preflight, create_only, chunk copy, cancellation, resume, and verification.
- Make `migrate` command use the executor for test payloads, while live DB payloads fail with a clear missing-adapter error until Phase 3 is complete.

### Phase 3: Live Adapters

- Add MySQL and PostgreSQL adapter implementations in Rust.
- Implement schema inspection from information schema/catalogs.
- Implement DDL execution, prepared inserts, chunked selects, and count queries.
- Add optional real DB integration tests gated by environment variables.

### Phase 4: App Workflow

- Replace manual normalized schema JSON entry with inspect -> plan -> confirm -> migrate -> verify flow.
- Persist helper resume state under TunnelForge app data.
- Add report export for issues, plan DDL, progress, and verification mismatches.
- Wire cancellation to helper process and state checkpoint.

### Phase 5: Completion Audit

- Run Rust unit tests, Python unit tests, helper JSONL smoke tests, and optional real DB tests.
- Verify both directions against the success criteria above.
- Confirm packaging builds and includes `tunnelforge-core.exe`.

## Rust Core Service Transition Audit Notes

- The app DB hot path uses the Rust JSONL service through `src/core/db_core_service.py`.
- `src/core/db_connector.py`, `src/core/postgres_connector.py`, `src/core/connection_pool.py`, DB connection tests, SQL editor execution, schema diff, and cross-engine migration workers are expected to call the Rust facade instead of opening Python DB driver connections directly.
- `src/exporters/rust_dump_exporter.py` routes export/import through the Rust DB Core dump protocol; there is no external dump tool exception in the active DB operation path.
- `tunnel-manager.spec` includes the `tunnelforge-core` binary. On Windows the `.exe` suffix is used; on macOS/Linux the suffix is omitted.
- `scripts/build-installer.ps1` is the Windows installer build path and builds/checks the Rust DB core service before packaging.

## Optional Live Test Environment

The Rust integration test `migration_core/tests/live_roundtrip.rs` runs only when both endpoint groups are configured:

- `TF_MYSQL_HOST`, `TF_MYSQL_PORT`, `TF_MYSQL_USER`, `TF_MYSQL_PASSWORD`, `TF_MYSQL_DATABASE`
- `TF_POSTGRES_HOST`, `TF_POSTGRES_PORT`, `TF_POSTGRES_USER`, `TF_POSTGRES_PASSWORD`, `TF_POSTGRES_DATABASE`

When these variables are absent, the tests return without touching a database. When configured, the tests create temporary tables, run MySQL -> PostgreSQL and PostgreSQL -> MySQL migrations through the helper core, verify all rows, then drop the temporary tables.
