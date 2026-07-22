# TunnelForge Clean Code 전수조사 (2026-07-09)

> **조사 방식**: Workflow 기반 19개 그룹 병렬 조사(Survey) → 그룹별 재검증(Verify) 2단계.
> **대상**: Python UI/core 약 44,600줄(13개 그룹) + Rust `migration_core/src/lib.rs` 17,006줄(5개 라인청크 + 구조개관 1개).
> **범위 구분**: 이전 감사(`.claude/investigation-full-audit-2026-07-08.md`)는 정합성·스레딩 버그 154건을 다루고 전량 해결됨(`audit-resolution-2026-07-08.md`). 본 조사는 **가독성/유지보수성(Clean Code) 관점만** 다루며 정합성 이슈는 재론하지 않음.
> **검증 결과**: 최초 발견 255건 중 검증 단계에서 기각(verified=false)된 건 **0건** — 전량 실제 코드에서 재확인됨.

---

## 요약

| 심각도 | 건수 |
|---|---|
| HIGH | 40 |
| MEDIUM | 134 |
| LOW | 81 |
| **합계** | **255** |

### 카테고리별 분포

| 카테고리 | 건수 |
|---|---|
| 중복 (duplication) | 82 |
| 매직값 (magic-value) | 31 |
| 거대함수 (long-function) | 29 |
| 죽은코드 (dead-code) | 26 |
| SRP위반 (srp-violation) | 15 |
| 파라미터과다 (large-param-list) | 11 |
| 갓클래스 (god-class) | 10 |
| 기타 (other) | 10 |
| 오래된주석 (stale-comment) | 9 |
| 에러처리불일치 (inconsistent-error-handling) | 8 |
| 네이밍 (poor-naming) | 8 |
| 갓파일 (god-file) | 7 |
| 추상화수준혼재 (inconsistent-abstraction) | 6 |
| 깊은중첩 (deep-nesting) | 3 |

### 발견 집중 파일 Top 20

| 파일 | 건수 |
|---|---|
| `migration_core/src/lib.rs` | 34 |
| `src/core/migration_analyzer.py` | 10 |
| `src/ui/dialogs/db_export_dialog.py` | 10 |
| `src/ui/dialogs/migration_dialogs.py` | 10 |
| `src/core/migration_fix_wizard.py` | 9 |
| `src/core/migration_rules/data_rules.py` | 7 |
| `src/ui/dialogs/sql_editor_dialog.py` | 7 |
| `src/ui/dialogs/db_import_dialog.py` | 7 |
| `src/core/i18n.py` | 6 |
| `src/core/migration_rules/schema_rules.py` | 6 |
| `src/exporters/rust_dump_exporter.py` | 6 |
| `src/ui/main_window.py` | 6 |
| `src/core/sql_validator.py` | 5 |
| `src/core/scheduler.py` | 5 |
| `src/ui/dialogs/cross_engine_migration_dialog.py` | 5 |
| `src/ui/dialogs/diff_dialog.py` | 5 |
| `src/ui/dialogs/settings.py` | 5 |
| `src/core/db_core_service.py` | 4 |
| `src/core/cross_engine_migration.py` | 4 |
| `src/ui/dialogs/cross_engine_migration_endpoint_form.py` | 4 |

---

## HIGH 심각도 (40건) — 상세

가장 먼저 손대야 할 항목. 대부분 '갓파일/갓클래스'와 그로 인한 파생 문제(중복, 거대함수, 깊은 중첩).

### `migration_core/src/lib.rs:1-17006` — 17,006-line single-file god-module holding the entire tunnelforge-core service (protocol, adapters, dump, import, inspection, wizard, migration, DDL gen, and tests)
- **카테고리**: 갓파일
- **문제**: Re-verified directly against the file: `wc -l migration_core/src/lib.rs` confirms 17,006 lines. Structural greps confirm the claimed shape almost exactly: `^(pub )?struct ` = 35 matches, `^(pub )?enum ` = 6 matches, `^impl ` = 7 matches (MemoryAdapter, MigrationAdapter for MemoryAdapter, LiveAdapter, MigrationAdapter for LiveAdapter, CoreService, Default for CoreService, DumpParallelLimits), and `^(pub )?mod ` has exactly one hit: `mod tests {` at line 11689 — there are zero other module boundaries in the file. A full listing of every `fn`/`pub fn`/`async fn` (610 matches total) confirms every one of the 11 cluster boundaries claimed in the original finding lines up almost line-for-line with real function groupings, e.g.: MigrationAdapter trait + Memory/LiveAdapter impls end at `prepare_target_schema` (668) with the last adapter-cluster helper (`dump_manifest_consistency_metadata`) at 717, matching the claimed ~280-728 boundary; the JSONL dispatch cluster runs from `impl CoreService` (734) through `job_cancel` (1332), matching ~729-1343; the dump engine runs from `dump_run_streaming` (1344) through `dump_one_mysql_table` (3066-3216), matching ~1344-3215; the import engine runs 3216 (`dump_import_streaming`) through `load_data_local_infile_sql` (4436), matching ~3216-4453; the OneClick wizard cluster (5442-7137) contains **exactly 42** `oneclick_*`-prefixed functions, confirming the '42 oneclick_* functions' claim precisely; and the row-normalization/digest cluster is genuinely split across two ranges (8791-9146 and 11613-11688) exactly as claimed, with `canonical_row`/`canonical_value`/`row_digest`/`compare_digest_rows`/`digest_counts`/`next_table_to_copy` sitting immediately before `mod tests` at 11689. The only imprecision found: the finding's fn-count arithmetic ('347 outside mod tests, 220 test fns inside') doesn't fully reconcile against the raw 610-match total (roughly 379 matches before line 11689, ~223 after) — likely because the original count filtered out trait-signature-only lines or non-`#[test]` helper fns (e.g. `schema()`, `empty_table()`, `fk()`, and a mock `MigrationAdapter` impl at lines 11692-11785 that live inside `mod tests` but aren't themselves test cases). This is a cosmetic slip in an illustrative statistic, not a defect in the core finding. Additionally confirmed: many test functions call *private* (non-`pub`) helpers directly — e.g. `strip_mysql_definer` (private, defined at 5236) and `sanitize_view_definition` (private, defined at 5200) are called from tests at lines 16746, 16755, 16762, 16769, 16779, 16909, 16918, 16990 — which matters for the recommendation below.
- **권고**: Confirmed and unchanged from the original recommendation: split `migration_core/src/lib.rs` into a `migration_core/src/` module tree along the verified cluster boundaries — `adapters.rs` (1-728), `protocol.rs` (729-1343), `dump.rs` (1344-3215, likely warranting its own `dump/` subdir split into `dump/schedule.rs` + `dump/mysql.rs` + `dump/writer.rs` given its ~1870-line size), `import.rs` (3216-4453), `query.rs` (4454-4657), `schema.rs` (4658-5441), `oneclick.rs` (5442-7137, the verified 42-function wizard), `migrate.rs` (7138-8790), `compare.rs` (8791-9146 + 11613-11688), `dump_format.rs` (9147-10098), and `ddl.rs` (10099-11612). Keep `lib.rs` as a thin `pub mod` re-export root. One correction to the original recommendation: do NOT plan to extract `mod tests` to a `migration_core/tests/` integration-test crate — verified that tests call private (non-`pub`) functions directly (`strip_mysql_definer`, `sanitize_view_definition`, and likely others such as `tsv_field`/`mysql_value_to_json`), so an integration-test crate (which only sees the public API) would fail to compile without first widening those functions to `pub(crate)`. Instead, co-locate each `#[cfg(test)] mod tests { ... }` block inside its corresponding new module file (e.g. move the view-sanitization tests at 16744-16995 into `schema.rs`, the dump-manifest tests at 12598-13905ish into `dump_format.rs`/`dump.rs`), which is both idiomatic Rust and naturally partitions the 220+ test functions along the same cluster lines already established. Execute as an incremental, mechanical move+re-export refactor (no logic changes), verifying `cargo build`/`cargo test` after each module extraction — this pairs well with the project's already-active decomposition effort visible in recent commits (e.g. 'refactor(schema-diff)', 'refactor(migration-analyzer)', 'refactor(migration-fix-wizard)') for the Python side of the same codebase.
- **검증 메모**: Independently re-derived via wc -l and targeted Grep over the actual file (struct/enum/impl/mod counts, full fn listing with line numbers) rather than trusting the original claim. Every cluster boundary, the 35/6/7 struct/enum/impl counts, the single 'mod tests' declaration, and the exact '42 oneclick_* functions' claim all check out against the real file. Only correction made: the recommendation's suggestion to optionally extract tests to a separate integration-test crate is not viable as stated (tests call private functions) and has been narrowed to the co-location approach; the fn-count arithmetic (347/220 vs. the raw 610 total) is a minor, non-material approximation in the original finding. Severity of high is appropriate given the file's role as the entire DB-migration service backend and the fact this is the largest and clearly worst-offending file in the repo by an order of magnitude.

### `migration_core/src/lib.rs:1620-1859` — dump_run mixes option parsing, schema prep, strategy selection, and manifest writing in one ~240-line function
- **카테고리**: 거대함수
- **문제**: Verified exact function boundaries (1620-1859, 240 lines). Confirmed six distinct phases in sequence: (1) option parsing/validation lines 1621-1665; (2) output dir prep line 1667-1668; (3) schema inspect/filter/dependency-order lines 1670-1681; (4) row/byte stats + adaptive parallel limits lines 1683-1732; (5) four-way strategy dispatch via nested if/else-if on `(endpoint.engine, threads, table_total)` at lines 1733-1800, calling `dump_single_mysql_table_parallel`, `dump_tables_global_mysql`, `dump_tables_parallel`, and `dump_tables_sequential` (the last used twice, once as a fallback inside branch 1 and once as the final else); (6) view collection + `DumpManifest` assembly/write + result JSON at lines 1802-1858.
- **권고**: Split into focused helpers: `fn parse_dump_run_options(request: &Request) -> Result<DumpRunOptions, String>` for the option-parsing block (1621-1665); `fn select_dump_strategy(engine: &str, threads: usize, table_total: usize) -> DumpStrategy` returning an enum consumed by a single match at the current 1733-1800 call site; and `fn finalize_dump_manifest(endpoint, schema, table_manifests, ..) -> Result<(DumpManifest, usize), String>` for the view-collection + manifest-write tail (1802-1839). Keep `dump_run` as a short orchestrator (~40-60 lines) that calls these in sequence and builds the final result JSON.
- **검증 메모**: Confirmed accurate as written; no correction needed.

### `migration_core/src/lib.rs:2019-2061` — adaptive_dump_parallel_limits_with_avg always returns the non-adaptive baseline
- **카테고리**: 죽은코드
- **문제**: Verified by re-reading lines 2019-2061. `baseline = dump_parallel_limits(threads, table_total)` is computed at line 2026. There are exactly three exit points after that: the early return at 2028-2030 (`table_total <= 1 || row_counts.is_empty()`), the early return at 2054-2056 (`heavy_tables > 1`), the early return at 2057-2059 (`max_estimated_chunks >= thread_budget*2`), and the trailing bare `baseline` at line 2060. Every single one of these returns `baseline` verbatim — there is no branch anywhere in the function that returns anything else. The `heavy_tables` computation (2032-2042, iterating `row_counts` and calling `mysql_range_chunk_size_for_avg_row` per table) and `max_estimated_chunks` computation (2043-2053, same iteration + `.max()`) are therefore fully dead work on every call. I additionally checked the test suite (lines 13387-13500): five tests exist that pass different `row_counts`/`avg_row_lengths` combinations expecting to exercise 'adaptive' behavior (e.g. `adaptive_dump_limits_prioritize_range_workers_for_heavy_chunked_tables`, `adaptive_dump_limits_use_byte_chunks_for_wide_tables`), but every asserted `(table_workers, range_workers_per_table)` pair exactly matches what `dump_parallel_limits(threads, table_total)` alone would produce — meaning the tests unknowingly validate dead code and would not catch a regression in the intended adaptive logic even if someone tried to add it.
- **권고**: Either implement real adaptive behavior — e.g. when `heavy_tables <= 1` and `max_estimated_chunks < thread_budget*2` (i.e. exactly one/zero dominant heavy tables), return `DumpParallelLimits { table_workers: 1, range_workers_per_table: thread_budget }` to devote full range-parallelism to the single heavy table instead of falling through to `baseline` — or delete the dead computation entirely: remove `adaptive_dump_parallel_limits_with_avg`'s `heavy_tables`/`max_estimated_chunks` logic (and the `#[cfg(test)]` `adaptive_dump_parallel_limits` wrapper) and have `dump_run` (line 1706) call `dump_parallel_limits` directly. Whichever path is chosen, add a unit test asserting a case where the adaptive result differs from `dump_parallel_limits(threads, table_total)` to prevent this dead-code regression from reappearing silently.
- **검증 메모**: Confirmed accurate as written; no correction needed. Severity high is justified — this masks an intended parallelism-tuning safeguard for large/heavy dumps behind ~40 lines of dead computation, and 5 tests currently give false confidence that adaptive behavior is exercised.

### `migration_core/src/lib.rs:2107-2199` — Bounded worker-pool dispatch loop duplicated near-verbatim in three dump functions
- **카테고리**: 중복
- **문제**: Verified by re-reading dump_tables_parallel (2083-2199, dispatch loop 2107-2198), dump_tables_global_mysql (2201-2465ish, dispatch loop 2330-2456), and dump_mysql_table_parallel_ranges (2526-2679, dispatch loop 2598-2670). All three independently declare `pending: VecDeque`, `active`, `completed`(_work), `first_error: Option<String>`, `handles: Vec<JoinHandle<_>>`, an `mpsc::channel`, a priming loop (`while active < max_threads { ... }`) that pops from `pending` and spawns, and a `recv()`-driven event loop that on Done/Error decrements `active`, records the first error, and refills from `pending`. The `dump_tables_global_mysql` copy additionally confirmed to differ slightly as described — it mutates `state.chunks_done`/`state.rows_dumped` in the `RangeDone` arm (2360-2404) inline per-table, rather than a flat counter, illustrating the copies already drifting.
- **권고**: Extract a generic bounded worker-pool helper, e.g. `fn run_bounded_pool<T, E>(work: VecDeque<T>, max_workers: usize, spawn: impl Fn(T, mpsc::Sender<E>) -> thread::JoinHandle<()>, mut on_event: impl FnMut(E) -> PoolAction) -> Result<(), String>` (with `PoolAction::Continue | PoolAction::Fatal(String)`), and have `dump_tables_parallel`, `dump_tables_global_mysql`, and `dump_mysql_table_parallel_ranges` supply only their event type and per-event handling closure. This removes roughly 150 duplicated lines of channel/queue bookkeeping.
- **검증 메모**: Confirmed accurate; line range correctly anchors the shared dispatch-loop portion of dump_tables_parallel (whose full function body starts at line 2083) rather than the whole function — this is intentional per the finding's own breakdown of the three ranges and is not an error.

### `migration_core/src/lib.rs:3234-3533` — dump_import is a ~300-line function combining validation, DDL sequencing, two import strategies, and reporting
- **카테고리**: 거대함수
- **문제**: Verified exact function boundaries (3234-3533, 300 lines) — the largest function checked in this pass. Confirmed: payload/manifest validation (mode, data_format, compression, strict_manifest) lines 3235-3293; dependency ordering + chunk validation 3294-3304; MySQL FK preflight + child-first drop-all for replace/recreate at lines 3335-3347 (matching cited range); per-table loop with a fast MySQL TSV path (`import_mysql_tsv_table`, 3375-3403) vs. a generic chunk-by-chunk `insert_rows` path with per-chunk progress events (3405-3433); session-tuning restore, post-load DDL (3458-3476), row-count verification (3482), view import (3486-3497), and report serialization/write (3498-3533).
- **권고**: Extract at least: `fn prepare_import_target(mode, tables, adapter, target_schema) -> Result<(), String>` for the FK-preflight + drop-all-then-create-all sequencing (3335-3347); `fn import_table_rows(adapter, table, table_manifest, data_format, compression, threads, ...) -> Result<(u64, u64), String>` for the per-table TSV-fast-path-vs-generic-chunk branch (3369-3445); and `fn finalize_dump_import(...) -> Result<Value, String>` for post-load DDL + verification + view import + report writing (3458-3533), leaving `dump_import` as a thin sequence of these calls plus its top validation block.
- **검증 메모**: Confirmed accurate as written; no correction needed.

### `migration_core/src/lib.rs:3669-4369` — MySQL import chunk functions each take 7-11 positional parameters, with a shared parameter cluster repeated verbatim across most of them
- **카테고리**: 파라미터과다
- **문제**: Confirmed: `import_mysql_tsv_table` (line 3669) takes 11 params: `endpoint, conn, input_path, table, table_manifest, compression, threads, request_id, overall_rows_before, overall_rows_total, emit`. `import_mysql_tsv_table_insert_fallback` (line 4135) takes 9: `conn, input_path, table, table_manifest, compression, request_id, overall_rows_before, overall_rows_total, emit`. `import_mysql_tsv_table_parallel` (line 4203) takes 10: `endpoint, input_path, table, table_manifest, compression, threads, request_id, overall_rows_before, overall_rows_total, emit`. These three share the identical 6-param cluster `table, table_manifest, compression, request_id, overall_rows_before, overall_rows_total` verbatim. `spawn_mysql_import_chunk_worker` (line 4330-4369) takes 7 params (`endpoint, input_path, table, table_path, chunk_index, compression, sender`) but only shares 2 of the 6 (`table`, `compression`) with the other three -- it receives `table_path: String` (derived from `table_manifest.path`) rather than the full `table_manifest`, and does not take `request_id`/`overall_rows_before`/`overall_rows_total` at all, so the original claim that 'all sharing the identical 6-parameter cluster' applies to all four functions is not accurate for the worker. The cited comment at lines 4341-4342 ('워커가 튜닝 없이 연결해 fk_checks/timeout이 누락돼 있었다') is real and confirms a duplicated-setup bug already occurred once in this area.
- **권고**: Introduce a `struct MysqlImportChunkContext<'a> { table: &'a NormalizedTable, table_manifest: &'a DumpTableManifest, compression: &'a str, request_id: Option<String>, overall_rows_before: u64, overall_rows_total: u64 }` (Clone-able) and thread it through `import_mysql_tsv_table`, `import_mysql_tsv_table_insert_fallback`, and `import_mysql_tsv_table_parallel` (the three functions that genuinely share all 6 fields). For `spawn_mysql_import_chunk_worker`, keep its narrower signature as-is or introduce a separate smaller struct (e.g. `MysqlImportWorkerContext { table, compression }`) rather than forcing it to take the full context -- do not claim it needs the same 6-field struct.
- **검증 메모**: Parameter counts and the shared cluster among the first three functions are confirmed accurate. Corrected: spawn_mysql_import_chunk_worker does NOT share the full 6-parameter cluster as originally claimed (only 2 of 6 fields overlap), so the recommendation was adjusted to not force that function into the same context struct. Also corrected line_end from 4338 (mid-signature) to 4369 (actual end of spawn_mysql_import_chunk_worker's body) since 4338 only covered the function's opening signature line.

### `migration_core/src/lib.rs:5480-5693` — oneclick_run_streaming is a 214-line orchestrator mixing preflight, analysis, recommendation, a 4-branch dry-run/execute decision returning a 6-tuple, validation, and final reporting
- **카테고리**: 거대함수
- **문제**: Confirmed by direct read (lines 5480-5693, 214 lines). The function inlines the entire One-Click pipeline: preflight state handling (5487-5513), analysis summary (5515-5526), recommendation generation (5528-5550), then an if/else-if/else chain (5567-5625) that computes `(success_count, fail_count, skip_count, disallowed_fix_attempts, applied_fixes, execution_log)` as a bare unnamed 6-tuple across four different scenarios (dry_run at 5574, disallowed fixes at 5583, empty actions at 5592, real execute via `LiveAdapter::connect` at 5601 with its own nested Ok/Err), followed by validation (5651-5679, itself duplicating the fallback-issue block from finding #1) and final result assembly (5685-5692). Each phase is a genuinely distinct responsibility, and the 6-element unnamed tuple returned from four separate branches is easy to get subtly wrong (miscount, wrong order) if a new branch or field is ever added.
- **권고**: Split into phase functions (e.g. `oneclick_run_preflight_phase`, `oneclick_run_execution_phase`, `oneclick_run_validation_phase`), each returning a small typed struct instead of a raw tuple -- reuse/extend the existing `OneClickApplyOutcome` struct (already defined at line 6783 with `success_count, fail_count, log, applied_fixes`; would need `skip_count` and `disallowed_fix_attempts` added) for the execution phase's return type, and have `oneclick_run_streaming` just sequence the phase calls and emit events.
- **검증 메모**: Confirmed accurate: function spans 5480-5693 (214 lines, close to the originally cited 213), the described 4-branch tuple computation at 5567-5625 matches exactly, and OneClickApplyOutcome (line 6783) exists and is a concrete, reusable target for the recommendation.

### `migration_core/src/lib.rs:10336-10527` — Text-column projection SQL (binary hex-encode / postgres cast / mysql passthrough) is copy-pasted verbatim in 3 functions
- **카테고리**: 중복
- **문제**: Confirmed by direct read: `select_chunk_text_sql` (10336-10397), `select_chunk_text_after_key_sql` (10399-10473), and `select_chunk_text_range_sql` (10475-10527) each contain a byte-for-byte identical 28-line `.map(|column| { if is_binary_type(...) && engine == "postgresql" { encode(...) } else if is_binary_type(...) { HEX(...) } else if engine == "postgresql" { ...::text } else if engine == "mysql" { quote_ident(...) } else { CAST(... AS CHAR) } })` closure. This is correctness-relevant logic (per-engine/per-type SQL casting for cross-engine dump), not cosmetic duplication -- a future fix (e.g. adding a new binary/geometry type, or a postgres version quirk) risks being applied to only 1 or 2 of the 3 copies.
- **권고**: Extract `fn projected_text_columns_sql(engine: &str, table: &NormalizedTable) -> String` containing the column-projection match (reusing existing `is_binary_type` at line 10825 and `quote_ident` at line 11494), and have all three functions call it before appending their own WHERE/ORDER BY/LIMIT clauses. Removes ~84 duplicated lines and guarantees single-point maintenance for type-casting rules.

### `migration_core/src/lib.rs:11119-11316` — is_safe_column_type is a ~198-line hand-rolled byte-level parser doing many distinct jobs in one function
- **카테고리**: 거대함수
- **문제**: Re-read and confirmed exactly as described: fn is_safe_column_type(type_name: &str) -> bool (lines 11119-11316) is a mini recursive-descent parser over a raw mutable byte index `i`: (1) base identifier scan (11128-11134), (2) an optional '(' ... ')' argument group that branches into a quoted string list for enum/set values with its own escape-handling inner loop (11142-11182) vs a numeric list (11184-11205), and (3) a trailing modifier loop (11216-11313) with per-keyword sub-parsers for `varying` (optional length group, 11237-11262), `with`/`without` ... `time zone` (11263-11276), `charset`/`collate` (11277-11288), and `character set` (11289-11310). Nesting reaches function -> `if '('` (11137) -> `if quote` (11142) -> `loop` (11144) -> inner `loop` (11149) -> `match` (11158), i.e. 4-5 levels deep. Per the comment at 11112-11118 this is the fail-closed guard against DDL/column-definition injection, so its correctness is security-critical, yet the size/nesting make it hard to review or safely modify.
- **권고**: Split into small, independently testable parser helpers, e.g. `fn parse_base_ident(bytes: &[u8], i: usize) -> Option<usize>`, `fn parse_quoted_string_list(bytes: &[u8], i: usize) -> Option<usize>`, `fn parse_numeric_list(bytes: &[u8], i: usize) -> Option<usize>`, `fn parse_modifier_word(s: &str, i: usize) -> Option<(&str, usize)>`, plus a small dispatch loop over modifiers, each returning the advanced index (or None on failure) so `is_safe_column_type` becomes a short orchestration function.

### `scripts/smart_release.py:59-132` — smart_release.py reimplements scripts/versioning.py instead of importing it, and the copies have drifted
- **카테고리**: 중복
- **문제**: Confirmed: `scripts/versioning.py`'s own module docstring states it holds functions 'shared' between `bump_version.py` and `smart_release.py`, and `bump_version.py` does import from it (`from versioning import read_version, write_version, sync_pyproject, sync_installer, bump_version`, line 84-90). `smart_release.py`, however, has zero references to `versioning`/`sync_pyproject`/`sync_installer` (confirmed via grep) and instead redefines `compare_versions` (59-70), `get_local_version` (73-79, duplicate of `read_version` but returns a tuple with file content too), `bump_version` (113-122, duplicate of versioning.py's but with NO `bump_type` validation and no try/except around a malformed version string — versioning.py's raises `ValueError` in both cases via lines 135-144), and `update_version_file` (125-132, duplicate of `write_version` but using bare `re.sub` with no check that a substitution actually happened via `count`, so it silently no-ops on a malformed file instead of raising like `write_version` does via its `re.subn`+count check). `smart_release.py` also never calls `sync_pyproject`/`sync_installer`, so a release performed via this documented '긴급 fallback' manual path leaves `pyproject.toml` and `installer/TunnelForge.iss` version fields out of sync with `src/version.py`.
- **권고**: Import and reuse `compare_versions`, `bump_version`, `read_version`, `write_version`, `sync_pyproject`, and `sync_installer` from `scripts/versioning.py` inside `smart_release.py`, deleting the duplicated re-implementations; this removes the validation/robustness drift and gives the manual fallback path the same pyproject/installer sync guarantee as the automated path.

### `src/core/config_manager.py:87-808` — ConfigManager mixes file I/O, encryption, settings, credentials, tunnel state, and full tunnel-group CRUD
- **카테고리**: 갓클래스
- **문제**: Confirmed by direct read: `class ConfigManager` spans exactly lines 87-808 (the entire rest of the file). It genuinely owns 6 separable concerns: (1) atomic file I/O + backup/restore/rotation (_write_config_atomic_unlocked L144, _create_backup L274, _cleanup_old_backups L303, restore_backup L372), (2) stale-snapshot merge logic (_merge_snapshot_changes L201, _merge_setting_values L188), (3) import/export validation (_validate_import_data L451, _validate_port L441, export_config L409, import_config L501), (4) app settings/timeouts (get_app_setting L552, get_network_timeout_check/download L557/565, set_app_setting L573), (5) credential decryption (get_tunnel_credentials L589, encryptor property L582) + active-tunnel state (save_active_tunnels L600, get_last_active_tunnels L608), and (6) tunnel group management (get_groups L617 through save_group_collapsed_state L789-808, ~192 lines) which is a distinct feature.
- **권고**: Extract lines 617-808 (get_groups, add_group, update_group, delete_group, move_tunnel_to_group, get_tunnel_group, save_group_collapsed_state) into a new `TunnelGroupManager` class in `src/core/group_manager.py`, constructed with a `ConfigManager` instance so it reuses load_config/save_config. Keep ConfigManager scoped to file lifecycle + the settings/credential accessors that are genuinely about the config file.
- **검증 메모**: Line range and all cited method names/line numbers verified exact against the file. Severity (high) is justified: 722-line class with 6 genuinely independent responsibilities, one of which (group CRUD) has essentially no coupling to config-file mechanics beyond calling load/save.

### `src/core/cross_engine_migration.py:269-375` — render_result_report nests ~8-9 levels deep while rendering 5 unrelated report sections in one function
- **카테고리**: 깊은중첩
- **문제**: Confirmed this 107-line function (exact span 269-375) handles issues, mismatches, plan/DDL, direction readiness, and per-table migration-guide rendering inline. Counting indentation in the directions branch: if isinstance(directions, list) (318, level 1) -> for direction in directions (321, level 2) -> if isinstance(direction, dict) (322, level 3) -> if isinstance(guide, dict) (331, level 4) -> if isinstance(tables, list) (347, level 5) -> for table in tables (348, level 6) -> if isinstance(columns, list) (355, level 7) -> for column in columns (356, level 8) -> if isinstance(column, dict) (357, level 9). The sibling row_samples loop nests similarly. This matches the finding's claim precisely.
- **권고**: Extract one helper per report section (_render_issues, _render_mismatches, _render_plan, _render_direction_guide, _render_table_guide), each taking its own sub-payload and returning List[str], using guard clauses (if not isinstance(x, dict): continue) to keep each helper to 2-3 nesting levels; render_result_report then just calls them in sequence and concatenates.
- **검증 메모**: Nesting depth independently recounted from indentation and confirmed to match the ~8-9 level claim; line range exact.

### `src/core/db_core_service.py:1-782` — db_core_service.py bundles three distinct architectural layers in one 782-line file
- **카테고리**: 갓파일
- **문제**: Confirmed: file is exactly 782 lines. Layer (a) subprocess transport: DbCoreServiceClient (L110-253), _format_error_event (L25-42). Layer (b) product facade: DbCoreFacade (L256-422) + get_shared_db_core_facade/shutdown_shared_db_core_facade singleton (L424-447). Layer (c) DB-API-compatibility shim: RustDbConnector (L450-615), create_rust_db_connector (L617-638), RustDbConnection (L641-718), RustDbCursor (L721-777), quote_mysql_ident (L780-781). These have different audiences (subprocess plumbing vs. UI/worker call sites vs. legacy cursor-shaped call sites).
- **권고**: Split into `db_core_client.py` (DbCoreServiceClient, _format_error_event, parse_db_version_tuple/normalize_db_engine/default_database_for_engine helpers), `db_core_facade.py` (DbEndpoint, DbCoreFacade, shared-facade singleton), and `db_core_dbapi_shim.py` (RustDbConnector, RustDbConnection, RustDbCursor, quote_mysql_ident, create_rust_db_connector). Re-export from db_core_service.py during the transition to avoid breaking the many existing import sites (db_connector.py, postgres_connector.py, scheduler.py, tunnel_monitor.py, etc.).
- **검증 메모**: Line boundaries for each layer verified exact against the file content.

### `src/core/i18n.py:1-1627` — i18n.py bundles three unrelated systems into one 1627-line module
- **카테고리**: 갓파일
- **문제**: Confirmed by direct read. The module's docstring (line 1) claims it is a 'Small runtime i18n layer for user-facing app chrome', but it actually contains three independent, large subsystems: (1) a structured key->string lookup system (DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, _TRANSLATIONS, tr(), language_label, normalize_language, detect_system_language, configure_language, installer-hint helpers — lines 1-250), (2) a bespoke Korean->English machine-translation approximation engine built from four separate literal tables (_EN_TEXT_TRANSLATIONS at line 253 with ~122 entries, _EN_PHRASE_TRANSLATIONS at line 407 with ~771 entries, _EN_REGEX_TRANSLATIONS at line 1181 with ~52 regex pairs, _EN_WORD_TRANSLATIONS at line 1236 with ~107 single-word entries) plus translate_text() and Korean grammar-particle-stripping regexes (lines 253-1389), and (3) a global PyQt6 monkey-patching system (install_qt_i18n, patch_init/patch_method/patch_all_string_args_method helpers, plus 8 manually written patch blocks for QTreeWidget/QTableWidget/QToolTip/QMenu/QMessageBox/QFileDialog) that rewrites method behavior on roughly 26 distinct Qt widget/class targets at import time (lines 1392-1627). These three concerns have almost no shared code path and different failure modes (data-lookup bugs vs. regex/grammar bugs vs. Qt monkey-patch bugs), yet anyone touching any one of them has to load and reason about the entire 1627-line file.
- **권고**: Split into a package: src/core/i18n/keys.py (DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, _TRANSLATIONS, tr, normalize_language, detect_system_language, configure_language, installer_language_hint_path/read_installer_language_hint/consume_installer_language_hint/language_from_args), src/core/i18n/legacy_translate.py (the four _EN_*_TRANSLATIONS tables, _has_hangul, translate_text, _translate_sequence), and src/core/i18n/qt_hooks.py (install_qt_i18n and all patch_* helpers, including the 8 inline monkey-patch blocks). Keep src/core/i18n.py (or an __init__.py) as a thin re-export shim so existing `from src.core.i18n import tr` call sites keep working.
- **검증 메모**: Structure and 3-way split claim confirmed by reading the full file. Corrected two factual inaccuracies from the original finding: (1) file is 1627 physical lines, not 1628 (confirmed via wc -l and direct read of the last line, 'return True'). (2) Entry-count estimates were substantially wrong and are corrected above: _EN_TEXT_TRANSLATIONS has ~122 entries (not ~500), _EN_PHRASE_TRANSLATIONS has ~771 entries (not ~250) — these two were effectively swapped/inflated in the original — _EN_REGEX_TRANSLATIONS has ~52 pairs (not ~30), _EN_WORD_TRANSLATIONS has ~107 entries (close to the original's ~100, no material change). Also corrected 'about ~15 Qt widget classes' to ~26 distinct classes/targets, counted directly from the QtGui/QtWidgets imports and patch_init/patch_method/patch_all_string_args_method/manual-block call sites in install_qt_i18n. None of these corrections change the core verdict — the god-file split recommendation is sound and actionable as originally proposed.

### `src/core/migration_analyzer.py:170-1277` — MigrationAnalyzer mixes FK analysis, 14 unrelated MySQL-8.4 compatibility checks, cleanup-SQL generation, and tree visualization
- **카테고리**: 갓클래스
- **문제**: Confirmed by direct read of lines 170-1277. The class owns: (1) FK metadata/orphan analysis (get_foreign_keys:201, build_fk_tree:239, find_orphan_records:262, get_fk_visualization:1239), (2) exactly 14 independent MySQL 8.0->8.4 compatibility scans (check_charset_issues:363, check_reserved_keywords:417, check_deprecated_in_routines:461, check_sql_modes:504, check_auth_plugins:832, check_zerofill_columns:885, check_float_precision:915, check_fk_name_length:947, check_invalid_date_values:978, check_int_display_width:1059, check_year2_type:1091, check_deprecated_engines:1124, check_enum_empty_value:1162, check_timestamp_range:1195), (3) cleanup SQL generation/execution (generate_cleanup_sql:536, execute_cleanup:585), and (4) ASCII FK-tree rendering (get_fk_visualization:1239). Four genuinely different reasons to change (new 8.4 rule, orphan-detection strategy, cleanup semantics, visualization formatting) all touch this single >1100-line class.
- **권고**: Split into collaborators composed by a thin MigrationAnalyzer facade: ForeignKeyAnalyzer (get_foreign_keys, build_fk_tree, find_orphan_records, get_fk_visualization), MySQLUpgradeCompatibilityChecker (all 14 check_* methods, ideally as a list of small Check objects/strategies so adding a rule doesn't touch the class), and OrphanCleanupPlanner (generate_cleanup_sql, execute_cleanup). analyze_schema becomes orchestration only.

### `src/core/migration_fix_wizard.py:1-1472` — Single 1472-line file bundles five unrelated responsibilities
- **카테고리**: 갓파일
- **문제**: Confirmed by direct read of the full file. Five independently substantial classes live in one module: SmartFixGenerator (lines 30-438, per-issue-type fix-option business rules), CollationFKGraphBuilder (441-633, generic BFS/topological-sort graph algorithm over FK relationships), FKSafeCharsetChanger (636-837 — note: the original finding said 636-796, which stops before execute_safe_charset_change; the class actually continues through line 837), BatchFixExecutor (840-1287, dry-run batch orchestration with its own clustering/merging), and CharsetFixPlanBuilder (1290-1438, a higher-level FK-graph planning facade). Each has its own lazy-init/caching pattern for a CollationFKGraphBuilder instance (see the related duplication finding), confirming they are only loosely coupled via that one shared dependency, not tightly integrated. This matches the shape of god-files the project has already split elsewhere (PRs #212-#215).
- **권고**: Split into: migration_fix_option_generator.py (SmartFixGenerator), migration_fk_graph.py (CollationFKGraphBuilder), migration_fk_safe_charset.py (FKSafeCharsetChanger), migration_batch_fix_executor.py (BatchFixExecutor), migration_charset_fix_plan.py (CharsetFixPlanBuilder). Keep migration_fix_wizard.py as a thin facade re-exporting create_wizard_steps() for backward compatibility, mirroring the split already done for migration_fix_models.py / migration_rollback_sql_generator.py.
- **검증 메모**: Confirmed accurate; only correction is the FKSafeCharsetChanger end line (837, not 796 — the original range omitted execute_safe_charset_change). Severity high is justified given five distinct, substantial, independently-changing concerns in one file.

### `src/core/migration_fix_wizard.py:941-1194` — BatchFixExecutor.execute_batch is a ~254-line function doing four distinct jobs inline
- **카테고리**: 거대함수
- **문제**: Confirmed by direct read: execute_batch (941-1194, exactly 254 lines) sequentially (a) FK-orders steps via _sort_steps_by_fk_order (982), (b) clusters and batch-processes COLLATION_FK_SAFE steps into per-schema/per-cluster frozensets with nested loops (993-1045), (c) groups and merges COLLATION_SINGLE column-level steps into per-table ALTER statements with its own dict grouping and 2-phase bookkeeping (1047-1116), (d) runs a third per-step loop handling SKIP/MANUAL/normal execution with independent logging/aggregation (1118-1184), and (e) assembles BatchExecutionResult (1186-1194). Each phase maintains independent local state (fk_safe_processed, merged_steps, results/success_count/fail_count/skip_count/total_affected) that must stay consistent across the whole function.
- **권고**: Extract three private helpers: _execute_fk_safe_clusters(steps) -> Tuple[List[FixExecutionResult], Set[int]], _execute_collation_single_merges(steps) -> Tuple[List[FixExecutionResult], Set[int]], and _execute_remaining_steps(steps, already_handled_ids) -> List[FixExecutionResult]. Have execute_batch call these in sequence, concatenate results, and compute aggregate counts/BatchExecutionResult from the combined list.
- **검증 메모**: Line range and phase breakdown confirmed exactly against source. High severity is warranted given the length and the amount of independently-tracked local state.

### `src/core/migration_rules/data_rules.py:17-18` — Module docstring documents D12/D13 (FK 2-Pass checks) that have no implementation anywhere in this file
- **카테고리**: 오래된주석
- **문제**: Confirmed by reading the full docstring (lines 1-19, listing '13개 규칙 구현' with D01-D13) and grepping the entire file for FK/foreign-key-related terms — the only hits are the two docstring lines themselves (17-18). Cross-checked all `def check_*` methods (61, 104, 127, 344, 419, 460, 542, 600, 654, 726, 826, 948) and the three `check_all_*` dispatch methods (1017, 1030, 1038): only D01-D11 are wired up. There is no FK-uniqueness or FK-target-existence logic anywhere in this file or its call graph.
- **권고**: Remove D12 and D13 from the docstring and correct '13개 규칙 구현' to '11개 규칙 구현 (D01-D11)' until FK 2-pass validation is actually implemented (either in this file or in a dedicated module, e.g. a new `fk_rules.py`, whose location should then be cross-referenced from this docstring once it exists).
- **검증 메모**: Confirmed accurate as described. Kept at high severity: this is a DB-migration-safety tool, and a false claim of FK-reference coverage could lead a user to believe referential-integrity risks are already screened for an 8.0->8.4 upgrade when they are not.

### `src/core/migration_rules/schema_rules.py:5-19` — Module docstring claims '36개 규칙 구현' including S19-S22, none of which exist in this file
- **카테고리**: 오래된주석
- **문제**: Confirmed by reading the full docstring (lines 1-28) and grepping the entire 850-line file (case-insensitive) for TRIGGER/EVENT/SPATIAL/JSON_TABLE — the only matches are the docstring lines themselves (17-19). Cross-checked all 22 `def check_*` methods: they jump directly from `check_blob_text_default` (S18, line 517) to `check_mysql_schema_conflict` (S23, line 540) with nothing in between implementing S19-S22.
- **권고**: Remove S19-S20 (trigger/event syntax), S21 (spatial type changes), and S22 (JSON_TABLE syntax) from the docstring, and correct the '36개 규칙 구현' count to reflect what is actually implemented (S01-S09, S16-S18, S23-S31 — 22 checks), or implement the missing S19-S22 rules and wire them into `check_all_live_db`/`check_all_sql_content` if trigger/event/spatial/JSON_TABLE coverage is actually required for this migration path.
- **검증 메모**: Confirmed accurate. Kept at high severity for the same reason as the D12/D13 finding: overclaiming compatibility-check coverage in a migration-safety tool risks a user shipping an 8.0->8.4 upgrade with undetected trigger/event/spatial/JSON_TABLE incompatibilities.

### `src/core/scheduler.py:242-1078` — BackupScheduler mixes scheduling engine, backup export, ad-hoc SQL execution, result serialization, and retention policy in one 1078-line file
- **카테고리**: 갓파일
- **문제**: Confirmed by reading the full class body (lines 242-1078, end of file). BackupScheduler owns: cron/queue scheduling mechanics (start/stop/_run_loop/_execution_worker_loop/_ensure_execution_thread, lines 311-531), shared connection resolution (_resolve_connection, line 533), full DB backup export via RustDumpExporter (_execute_backup + _cleanup_old_backups, lines 583-706), arbitrary multi-statement SQL execution (_execute_sql_query/_execute_single_query, lines 773-938), result serialization to CSV/JSON (_save_as_csv/_save_as_json, lines 982-1016), a second independent retention/cleanup routine (_cleanup_old_results, lines 1018-1077), and its own text-log persistence (_log_backup/get_backup_logs, lines 708-767). None of these depend on each other's internals.
- **권고**: Split into cooperating collaborators: keep BackupScheduler as a thin orchestrator over the cron/queue mechanics, and extract BackupTaskExecutor (_execute_backup + _cleanup_old_backups), SqlQueryTaskExecutor (_execute_sql_query/_execute_single_query/_save_as_csv/_save_as_json/_cleanup_old_results), and an ExecutionLogWriter (_log_backup/get_backup_logs) into their own modules/classes composed via dependency injection.
- **검증 메모**: Re-read lines 242-1078; class runs to end of file exactly as described. All named methods and their line ranges confirmed accurate. Severity 'high' is justified given the size (~836 lines) and genuinely unrelated concerns bundled together.

### `src/core/schema_comparator.py:202-403` — _compare_indexes and _compare_foreign_keys implement the same ~100-line algorithm twice
- **카테고리**: 중복
- **문제**: Confirmed by reading lines 202-403 in full. SchemaComparator._compare_indexes (202-300) and _compare_foreign_keys (302-403) both: (1) build lower-cased name->object maps, (2) match by exact name and diff attributes into MODIFIED/UNCHANGED, (3) index unmatched-by-name items by a content key (_index_content_key at 187-189 / _fk_content_key at 192-200) to detect RENAMED entries via identical 'first unclaimed candidate wins' logic, (4) fall back to ADDED for unmatched source and REMOVED for unmatched target, with identical sorted() iteration order throughout. Only the field names diffed (columns/unique vs ref_table/columns/on_delete/on_update) and the Korean message text differ.
- **권고**: Extract a generic private helper `_compare_named_entities(source_map, target_map, content_key_fn, diff_fields_fn, diff_ctor)` in SchemaComparator that runs the 3-stage match/rename/added-removed algorithm once. `diff_fields_fn(src, tgt) -> List[str]` supplies the entity-specific difference messages; `diff_ctor` builds the IndexDiff/ForeignKeyDiff. _compare_indexes and _compare_foreign_keys become ~15-line wrappers passing IndexInfo/ForeignKeyInfo-specific closures.

### `src/core/schema_extractor.py:24-248` — Every query method silently swallows all exceptions into a log line and an empty/default result
- **카테고리**: 에러처리불일치
- **문제**: Confirmed by reading lines 24-248 in full. `extract_table_schema` (24-63), `extract_all_tables` (65-95), `_get_columns` (97-132), `_get_indexes` (134-167), `_get_foreign_keys` (169-214), `_get_table_options` (216-237), and `_get_row_count` (239-248) each wrap their query in `try: ... except Exception as e: logger.error(...); return <empty list/dict/None/default tuple>`. A connection failure, a permissions error, and a table that genuinely has zero columns/indexes/FKs all produce the exact same empty-collection result, so `extract_table_schema`/`extract_all_tables` callers cannot distinguish 'no FKs' from 'the FK query failed'.
- **권고**: Re-raise (or return a sentinel/Result-like wrapper distinguishing error from genuinely-empty) from the innermost `_get_columns`/`_get_indexes`/`_get_foreign_keys`/`_get_table_options`/`_get_row_count` helpers, so `extract_table_schema` can decide whether to treat a failure as 'no data' or propagate it to the caller/UI instead of silently producing a schema that looks valid but is incomplete.
- **검증 메모**: Severity corrected from medium to high: this tool's output (TableSchema) directly drives generate_sync_script's ALTER/CREATE/FK statements. If _get_foreign_keys silently swallows a real query failure and returns [], the generated sync script will omit a real foreign key with no error surfaced anywhere — a silent data-integrity/correctness defect in a migration tool, not just a logging-hygiene nit.

### `src/core/schema_severity_classifier.py:88-145` — Severity classification silently depends on hard-coded diff-message prefixes defined in a different file
- **카테고리**: 매직값
- **문제**: Confirmed by reading both files. `_classify_column` (88-100) matches `d.startswith("타입:")` / `"Nullable:"` / `"Default:"` / `"Extra:"` / `"Charset:"`/`"Collation:"`, and `_classify_extra_change` (140-145) checks `'auto_increment' in diff_text.lower()`. These exact literals are produced only in schema_comparator.py's `_compare_columns`: `f"타입: {src.data_type} → {tgt.data_type}"` (line 143), `f"Nullable: ..."` (150), `f"Default: ..."` (153), `f"Extra: ..."` (158), `f"Charset: ..."` (163), `f"Collation: ..."` (166) — verified verbatim match. No shared enum/constant ties the two files; if the producer text changes, `_classify_column` falls through to `else: sev = DiffSeverity.WARNING` (99-100) with no error, silently downgrading what should be a CRITICAL type/auto_increment change in a schema-migration tool where severity drives go/no-go decisions.
- **권고**: Add a `field` (e.g. enum `DiffField.TYPE/NULLABLE/DEFAULT/EXTRA/CHARSET/COLLATION`) to each difference entry — either promote ColumnDiff.differences from List[str] to List[a small dataclass with field+text], or add a parallel `difference_fields: List[DiffField]` — set by schema_comparator.py's _compare_columns when it builds each difference, and have `_classify_column` switch on the structured field instead of parsing text. Keep the Korean/English string only for display.

### `src/core/schema_sync_script_generator.py:12-173` — generate_sync_script is a 160-line, 5-phase god-method with 4-level nesting
- **카테고리**: 거대함수
- **문제**: Confirmed by reading lines 12-173 in full. SyncScriptGenerator.generate_sync_script inlines five phases: FK drops (33-61), table drops (63-72), table creates (74-85), column/index ALTERs (87-144, nesting `for diff -> if MODIFIED -> for col_diff/idx_diff -> if/elif diff_type` 4 levels deep), and FK adds (146-167). Each phase builds its own local list (fk_drops, table_drops, table_creates, alter_statements, fk_adds) before being joined into `lines` at the end.
- **권고**: Split into `_generate_fk_drops(diffs, target_schema)`, `_generate_table_drops(diffs, target_schema)`, `_generate_table_creates(diffs, target_schema)`, `_generate_alter_statements(diffs, target_schema)`, `_generate_fk_adds(diffs, target_schema)`, each returning a List[str] of SQL lines. `generate_sync_script` becomes an orchestrator calling each phase and joining results with section headers, reducing nesting to 2 levels per phase.

### `src/core/sql_validator.py:1-869` — sql_validator.py bundles two unrelated product features (validation, autocomplete) plus shared SQL-parsing utilities
- **카테고리**: 갓파일
- **문제**: Confirmed: file is exactly 869 lines. Contains standalone parsing utilities (extract_cte_names L109, extract_derived_table_aliases L169, extract_table_aliases L200, _read_identifier L39, _skip_balanced_parentheses L67, _normalize_identifier L32), schema-metadata cache (SchemaMetadata L256, SchemaMetadataProvider L305), SQLValidator (L389-697, ~309 lines), and SQLAutoCompleter (L700-869, ~169 lines) which is a different feature (editor autocomplete) reusing the same parsing helpers.
- **권고**: Split into `sql_identifier_utils.py` (extract_cte_names/extract_derived_table_aliases/extract_table_aliases/_read_identifier/_skip_balanced_parentheses/_normalize_identifier/ALIAS_STOP_WORDS), `sql_validator.py` (SQLValidator, ValidationIssue, IssueSeverity, SchemaMetadata, SchemaMetadataProvider), and `sql_autocompleter.py` (SQLAutoCompleter). Both consumers import the shared parsing module.
- **검증 메모**: Line boundaries verified exact (SQLValidator class body 389-697, SQLAutoCompleter 700-869).

### `src/exporters/rust_dump_exporter.py:1-922` — rust_dump_exporter.py bundles 6 unrelated responsibilities in one 922-line module
- **카테고리**: 갓파일
- **문제**: Confirmed by reading the full 922-line file. Contents: (1) path-safety helpers `_safe_dump_child_dir`/`_safe_dump_child_file` (23-49); (2) Rust-core install checking `RustDumpChecker` (93-127); (3) a direct-SQL orphan-record analysis subsystem `OrphanRecordInfo`/`ForeignKeyResolver` (130-324) using `MySQLConnector` directly, unrelated to Rust dump/import; (4) dump/import orchestration `RustDumpExporter`/`RustDumpImporter` (327-725); (5) UI progress-metadata helper `TableProgressTracker` (728-761); (6) event dispatcher `emit_core_event` plus 4 legacy module-level functions `check_rust_dump`/`export_schema`/`export_tables`/`import_dump` (764-921). File length confirmed at exactly 922 lines.
- **권고**: Split into: `src/core/foreign_key_resolver.py` (move OrphanRecordInfo + ForeignKeyResolver — direct-SQL analysis unrelated to Rust dump), `src/exporters/dump_progress.py` (move TableProgressTracker + emit_core_event), and keep `rust_dump_exporter.py` focused on RustDumpConfig/RustDumpChecker/RustDumpExporter/RustDumpImporter plus the path-safety helpers they use directly.

### `src/ui/dialogs/cross_engine_migration_dialog.py:43-1499` — CrossEngineMigrationDialog mixes widget construction, wizard state machine, and Rust-payload interpretation in one 1450-line class
- **카테고리**: 갓클래스
- **문제**: Confirmed by direct read of the full file (1509 lines total). The class spans exactly lines 43-1499 (closeEvent's `a0.accept()` on 1499 is the last statement before the module-level `CrossEngineMigrationWizard` class at 1504). Four responsibility clusters are all present and correctly located: (1) UI construction in `_setup_ui` (180-475, confirmed) plus the 83-line inline stylesheet in `_apply_wizard_style` (96-178, confirmed) -- one correction: the class builds 7 QGroupBoxes (option_group, schema_group, execution_group, safety_group, action_group, plan_group, verify_group), not '~10' as originally stated; the ~20-28 `.connect(...)` signal wiring count in `_setup_ui` is accurate. (2) Wizard step machine: `step_ids`(66-73), `_step_completed`(85-91), `_next_enabled_for_current_step`(770-785), `_show_step`(840-851), `_go_next_step`(858-868) all confirmed present. (3) Rust-payload text formatting duplicating `render_result_report`: `_schema_summary_text`(491-516), `_plan_summary_text`(544-575), `_verification_result_text`(593-644), `_update_migration_result_summary`(1334-1389) all confirmed at the exact stated line ranges, each hand-building strings from raw payload dicts. (4) Worker lifecycle: `_start_command`(923-956), `_start_command_with_payload`(958-982), `_on_result`(990-1046), `_on_finished`(1081-1118) all confirmed.
- **권고**: Split into (a) a WizardStepController owning step_ids/_step_completed/navigation (_show_step, _go_next_step, _go_previous_step, _next_enabled_for_current_step, _refresh_navigation_state), (b) a result-presentation module co-located with render_result_report in src/core/cross_engine_migration.py that owns _schema_summary_text/_plan_summary_text/_verification_result_text/_update_migration_result_summary's formatting logic, and (c) keep CrossEngineMigrationDialog as a thin view wiring the two together plus widget creation and worker signal plumbing.
- **검증 메모**: Confirmed accurate; only correction is the QGroupBox count (7 actual vs '~10' claimed). Severity 'high' is justified given the confirmed 1450+ line span with four genuinely separable, actively-coupled responsibility clusters.

### `src/ui/dialogs/db_export_dialog.py:168-1461` — RustDumpExportDialog mixes UI construction, filesystem-path business logic, worker orchestration, GitHub reporting and file I/O in one 1300-line class
- **카테고리**: SRP위반
- **문제**: Confirmed: class spans lines 168-1461 (file ends at 1463). Verified it contains init_ui (~390 lines), output-folder naming/uniqueness logic (_generate_output_dir/_unique_output_dir, including path-traversal-safe join logic), Rust worker construction/telemetry parsing (do_export, on_detail_progress, on_raw_output), GitHub issue auto-reporting (_report_error_to_github/_on_github_report_finished), and log-file serialization, all as methods of the same QDialog subclass.
- **권고**: Split into: (1) a thin QDialog that only wires widgets to signals, (2) an `ExportOutputPathResolver` (folder naming/uniqueness/safe-join), (3) an `ExportTelemetryReport` module (summaries + save_log content generation), and (4) a shared GitHub-reporting helper (see the separate GitHub-reporting duplication finding). The dialog should call these collaborators instead of implementing them inline.

### `src/ui/dialogs/db_import_dialog.py:222-1667` — RustDumpImportDialog mixes UI construction, timezone-detection SQL policy, production-guard checks, retry orchestration, GitHub reporting and log file I/O in one 1450-line class
- **카테고리**: SRP위반
- **문제**: Confirmed: class spans lines 222-1667 (file's last line, closeEvent ends there). It contains init_ui (~400 lines, 267-671), MySQL timezone-compensation SQL decisions (do_import 1058-1092, check_timezone_support 899-913), ProductionGuard invocation (do_import 968-983), MySQL 8.4 compatibility analysis via DumpFileAnalyzer (_run_upgrade_check 795-829), retry-table selection (select_failed_tables 1517-1522, do_retry 1524+), GitHub error reporting (_report_error_to_github 1474-1504), and full import-log serialization (save_log).
- **권고**: Extract an `ImportTimezoneResolver` (SQL decision logic), keep ProductionGuard invocation but isolate it behind a `_confirm_production_import()` method, and reuse the same GitHub-reporting/telemetry helpers proposed for db_export_dialog.py so the dialog is left with widget wiring only.
- **검증 메모**: Corrected line_end from 1668 to 1667 - the file has exactly 1667 lines and the class (via closeEvent) ends on the last line.

### `src/ui/dialogs/migration_dialogs.py:146-1391` — MigrationAnalyzerDialog mixes UI, SQL generation, file I/O, and worker orchestration in one ~1250-line class
- **카테고리**: 갓클래스
- **문제**: Re-read confirms all cited responsibilities are present in this single class: 5 tab builders (init_ui 224-401, init_overview_tab 402-426, init_orphans_tab 427-529, init_compatibility_tab 531-597, init_fk_tree_tab 598-617, init_log_tab 619-639), worker lifecycle management in closeEvent (171-222) using module-level _detach_workers_until_finished, raw SQL text building in _generate_orphan_select_query (981-990) and SQL dispatch in on_orphan_selected (950-975), JSON/.sql/.txt persistence (_auto_save_result 1267-1283, save_analysis_result 1285-1317, _save_result_directly 1319-1343, load_analysis_result 1345-1390, export_orphan_queries 1019-1068, save_log 656-667), and the AUTO_FIXABLE_TYPES domain-classification constant (1159-1167). Counted ~37 methods on the class (excluding the property), consistent with the '~35 methods' claim.
- **권고**: Split into: (1) a thin MigrationAnalyzerDialog that only builds widgets and wires signals, (2) a MigrationResultStore/AnalysisResultRepository for save/load/auto-save JSON logic (_auto_save_result, save_analysis_result, _save_result_directly, load_analysis_result, export_orphan_queries), (3) move _generate_orphan_select_query and AUTO_FIXABLE_TYPES into src/core/migration_analyzer.py next to generate_cleanup_sql so the domain module is the single source of truth for 'what SQL do we generate' and 'what is auto-fixable'.

### `src/ui/dialogs/migration_dialogs.py:871-889` — IssueType -> Korean display-name dict is independently re-implemented 5 times across 4 files, with drifted text
- **카테고리**: 중복
- **문제**: Confirmed all 5 dict definitions and the specific drift: migration_dialogs.py:871-889 (filter_compatibility_issues) has RESERVED_KEYWORD='예약어', CHARSET_ISSUE='문자셋', ZEROFILL_USAGE='ZEROFILL 속성'. migration_manual_guide_dialog.py:199-205 (populate_issues) has RESERVED_KEYWORD='예약어 충돌' (confirmed drift). fix_wizard_issue_selection_page.py:103-114 (populate_table) has CHARSET_ISSUE='문자셋', ZEROFILL_USAGE='ZEROFILL' (confirmed drift vs '문자셋 이슈'/'ZEROFILL 속성' elsewhere). fix_wizard_option_page.py:59-68 (BatchOptionDialog.init_ui) has CHARSET_ISSUE='문자셋 이슈', ZEROFILL_USAGE='ZEROFILL 속성'. fix_wizard_option_page.py:362-370 (FixOptionPage.show_current_issue) has a third variant with extra qualifiers: INVALID_DATE='잘못된 날짜 (0000-00-00)', FLOAT_PRECISION='FLOAT 정밀도 구문', DEPRECATED_ENGINE='deprecated 스토리지 엔진', ENUM_EMPTY_VALUE='ENUM 빈 문자열'. All cited drift examples check out exactly.
- **권고**: Create one canonical `ISSUE_TYPE_DISPLAY_NAMES: Dict[IssueType, str]` constant (e.g. in src/core/migration_constants.py) covering every IssueType, and have all 4 files import and use it instead of maintaining separate ad-hoc dicts. Reconcile the drifted labels as part of the consolidation.

### `src/ui/dialogs/schedule_dialog.py:164-464` — ScheduleEditDialog._setup_ui constructs the entire dialog (task-type selector, basic info, backup page, SQL page, schedule tabs, enable checkbox, buttons) in one ~300-line method
- **카테고리**: 거대함수
- **문제**: Confirmed exact: _setup_ui runs lines 164-464 (301 lines, immediately followed by _connect_signals at 466). Within it: task-type radio group (172-188), basic-info form (190-207), a QStackedWidget with a full backup-settings page (209-246) and a full SQL-editor page including result-format/output/timeout/retention fields (248-324), a schedule-settings QTabWidget with simple (daily/weekly/monthly/hourly with conditional day/dow/minute/time widgets, 332-412) and advanced (cron, 414-441) tabs, plus the final enable checkbox and Save/Cancel buttons (446-464). This is the file's largest method by far.
- **권고**: Extract per-section builders (_build_task_type_group(), _build_basic_info_group(), _build_backup_page(), _build_sql_page(), _build_schedule_group()) each returning a widget, with _setup_ui reduced to assembling them into the top-level layout.
- **검증 메모**: Line range, length, and section breakdown confirmed exact via Read; no changes needed.

### `src/ui/dialogs/settings.py:42-1079` — SettingsDialog owns 6 unrelated subsystems in a single ~1038-line, 31-method class
- **카테고리**: 갓클래스
- **문제**: Confirmed by reading the full file: the class body runs exactly from line 42 (`class SettingsDialog(QDialog):`) to line 1079 (EOF) and contains 31 method definitions (including `__init__`/`init_ui`; 29 if you exclude those two orchestration methods). It genuinely mixes: general app settings (language/close-behavior/theme persistence, lines 91-357), GitHub App auto-report config + a live connection test (`_check_github_app`, `_test_github_connection`, lines 1015-1078), config backup/export/import file I/O (`_refresh_backup_list`/`_restore_selected_backup`/`_export_config`/`_import_config`, lines 359-441), a log viewer with level filtering and file clearing (`_create_log_tab`...`_clear_log_file`, lines 442-568), and a full self-update pipeline: check -> background download via QThread worker -> progress reporting -> installer launch via `subprocess.Popen` with active-tunnel warning (`_check_for_updates`...`_launch_installer`, lines 805-1006). None of these concerns depend on each other but all share one class's state.
- **권고**: Split into per-tab composer classes (e.g. GeneralSettingsTab, LogViewerTab, AboutUpdateTab), each owning its own widgets and emitting signals; reduce SettingsDialog to composing the three tabs plus Save/Cancel wiring. Move the update check/download/install orchestration into a dedicated UpdateManager service that SettingsDialog only observes.
- **검증 메모**: Re-read src/ui/dialogs/settings.py in full. Line range and the 6-subsystem description are accurate. Corrected the method count from 'the finding didn't specify' to the actual 31 (29 excluding __init__/init_ui) since the title needed a concrete number to stay honest.

### `src/ui/dialogs/settings.py:67-909` — 12 inline QPushButton stylesheets in settings.py duplicate each other and, in 2 cases, near-duplicate src/ui/styles.ButtonStyles
- **카테고리**: 중복
- **문제**: Verified each cited block against src/ui/styles.py. Only 2 of the 12 are close matches to existing ButtonStyles constants, and even those are NOT byte-for-byte identical: btn_save (lines 67-73) matches ButtonStyles.PRIMARY (styles.py:27-34) except it omits PRIMARY's `QPushButton:disabled` rule; btn_cancel (77-83) matches ButtonStyles.SECONDARY (styles.py:37-44) with the same omission. The other ~10 blocks (btn_test 200-208, btn_restore 259-266, btn_export 271-278, btn_import 283-290, btn_refresh_log 466-473, btn_open_log_folder 479-486, btn_clear_log 492-499, btn_check_update 601-609, btn_download 644-652, btn_cancel_download 657-664, install-button 902-909) do NOT match any existing ButtonStyles constant (different hex shades, paddings, font-sizes, disabled handling) — but several are byte-for-byte identical to EACH OTHER within this same file: btn_export (271-278) == btn_refresh_log (466-473) [blue #3498db]; btn_import (283-290) == btn_open_log_folder (479-486) [gray #95a5a6]; btn_clear_log (492-499) == btn_cancel_download (657-664) [red #e74c3c]. So the real duplication is 3 undocumented intra-file style families plus 2 near-duplicates of the centralized constants — not '12 duplicates of ButtonStyles' as originally framed.
- **권고**: Replace btn_save/btn_cancel with ButtonStyles.PRIMARY/SECONDARY directly (the added :disabled rule is a harmless improvement). Add 3 new named constants to src/ui/styles.py's ButtonStyles class to cover the intra-file duplicate families: e.g. INFO_SMALL (blue #3498db, no disabled rule, font-size 11px) for export/refresh-log, SECONDARY_SMALL (gray #95a5a6 bg, white text) for import/open-log-folder (btn_test needs the same base plus its own min-height:26px), and DANGER_SMALL (red #e74c3c, no disabled rule) for clear-log/cancel-download. Add a new INSTALL constant (purple #9b59b6) for the 902-909 block. Then reference the constants from all call sites instead of inline setStyleSheet calls.
- **검증 메모**: Read src/ui/dialogs/settings.py and src/ui/styles.py side by side and diffed every cited line range. All 11 line citations in the original finding were accurate down to the exact line, but the claim that all ~12 blocks 'duplicate constants already defined in ButtonStyles' was overstated — only 2 are near-duplicates (and not byte-identical), while the rest duplicate each other, not the centralized module. Rewrote detail/recommendation to reflect the actual duplication structure; kept severity high since a dozen near-identical inline stylesheets in one file is still a real, sizeable maintenance problem.

### `src/ui/dialogs/sql_editor_dialog.py:125-2608` — SQLEditorDialog is a 2,608-line god-class mixing UI, connection/transaction management, and raw SQL business logic
- **카테고리**: SRP위반
- **문제**: Confirmed by direct read. The single class SQLEditorDialog spans line 125 to end-of-file (file is 2608 lines total, not 2609 as originally stated — off-by-one). `grep -n "^    def "` finds 90 class-body method definitions, matching the '~90 methods' claim. Verified all cited methods exist and do exactly what was described: `_resolve_db_target` (line 182), `_ensure_connection` (860) and `_close_db_connection` (1661) own temp-tunnel/connector lifecycle; `pending_queries`, `_do_commit` (1508), `_do_rollback` (1611), `_on_postgres_transaction_rolled_back` (1114) implement a hand-rolled transactional state machine; `_analyze_query_editability` (1833-1888, 56 lines) is pure regex-based SQL text analysis (JOIN/UNION/GROUP BY/HAVING/DISTINCT/aggregate-function rejection); `_fetch_primary_keys` (1890-1941) runs raw `information_schema`/`key_column_usage` SQL straight off `self.db_connection.cursor()` with engine-specific branches for postgres vs mysql; `_execute_cell_edits_in_txn` (2072-2121) hand-builds UPDATE statements with manual identifier quoting via `_quote_editor_identifier`. This directly contradicts the project's CLAUDE.md mandate that DB auth/schema/SQL execution should run through the Rust `tunnelforge-core` sidecar while Python stays UI/orchestration only.
- **권고**: Split into at least three collaborators the dialog composes rather than implements: (1) a connection/transaction controller (e.g. `SqlEditorTransactionSession`) owning temp-tunnel lifecycle, `pending_queries`, commit/rollback, and postgres-rollback-on-error handling; (2) a `ResultEditabilityAnalyzer` module owning `_analyze_query_editability`, `_fetch_primary_keys`, and `_execute_cell_edits_in_txn` (ideally routed through tunnelforge-core rather than raw cursor SQL, per CLAUDE.md's Rust-core mandate); (3) leave SQLEditorDialog as thin UI glue delegating to both. This also makes the PK/editability logic unit-testable without a QApplication.
- **검증 메모**: Accurate and well-supported. Only correction: file is 2608 lines, not 2609 (line_end should be 2608). Severity 'high' is justified given the direct contradiction with the project's stated Rust-core architecture mandate plus the sheer scope (90 methods, 5+ distinct responsibility clusters).

### `src/ui/dialogs/test_dialogs.py:158-283` — refresh_databases() and execute_sql() independently re-implement the same direct/running-tunnel/temp-tunnel host-port resolution
- **카테고리**: 중복
- **문제**: Confirmed exact: refresh_databases (lines 173-185) and execute_sql (lines 236-250) both branch on `is_direct` -> `self.engine.is_running(tid)` -> else create a temp tunnel, with nearly identical bodies; they differ only in whether progress lines are appended and whether the temp server handle is a local variable (refresh_databases) or self.temp_server (execute_sql). A fix to one copy (e.g. how create_temp_tunnel failure is handled or how the temp server is cleaned up) can easily be applied to only one method and silently miss the other.
- **권고**: Extract a shared `_resolve_connection(self) -> tuple[str, int, Optional[Any]]` returning (host, port, temp_server_or_None) with its own progress-message side effects, and have both callers use it.
- **검증 메모**: Both cited sub-ranges confirmed exact and near-identical via Read; no changes needed.

### `src/ui/main_window.py` — TunnelManagerUI is a ~1,220-line god-class owning every app concern
- **카테고리**: 갓클래스
- **문제**: Verified against the current file (1282 lines total). A single QMainWindow subclass implements: window/tray init (init_ui 124-211, init_tray 239-276), tunnel CRUD (add_tunnel_dialog/edit_tunnel_dialog/duplicate_tunnel/delete_tunnel, 579-663), group CRUD (add_group_dialog/_edit_group_dialog/_delete_group, 526-576), connection lifecycle and auto-connect (_ensure_tunnel_running/start_tunnel/stop_tunnel/_auto_connect_tunnels, 715-806, 1131-1184), mysql_config_editor login-path registration (_register_login_path/_remove_login_path, 761-805), DB dialogs/SQL editor/export/import/orphan-check/migration wizards (680-712, 1196-1281), scheduled-backup menu plumbing (896-987, gated by SCHEDULE_FEATURE_ENABLED=False at line 23), tunnel-monitor status callbacks (993-1046), schema-diff dialog (1052-1060), column-width persistence (1062-1118), startup update-checker wiring (1120-1129, 1186-1194), and window repaint scheduling (1091-1103). All sub-ranges checked line-by-line and match the described method boundaries exactly. Note: the scheduled-backup block is currently dead in production (SCHEDULE_FEATURE_ENABLED is hardcoded False), which slightly narrows the 'everything touches this class' argument, but the remaining concerns (CRUD, connection lifecycle, wizards, monitoring, column persistence, tray, update-checker) are all live code and still justify the god-class classification.
- **권고**: Split into composed collaborators owned by a slimmer TunnelManagerUI: a TunnelActionsController (CRUD + start/stop/auto-connect + login-path registration), a WizardLauncher (export/import/migration/schema-diff/orphan-check entry points), and a TrayController (tray icon, notifications, schedule submenu). Keep TunnelManagerUI responsible only for layout/widget wiring and delegating signals to these collaborators.
- **검증 메모**: Confirmed accurate; all cited line ranges match the file exactly. Added the caveat that the schedule-menu block is currently unreachable because SCHEDULE_FEATURE_ENABLED=False, but this doesn't change the overall verdict.

### `src/ui/styles.py:23-705` — Two competing styling systems coexist; the actively-used one is not theme-aware
- **카테고리**: 중복
- **문제**: Confirmed via repo-wide grep: `TableStyles`, `TabStyles`, `DialogStyles`, `ProgressStyles`, `TextEditStyles`, `GroupBoxStyles`, `InputStyles`, `Colors`, `apply_button_style`, `apply_label_style`, `get_dynamic_button_style`, and `get_dynamic_label_style` have zero references anywhere in the repo outside of styles.py itself. Meanwhile the hardcoded, non-theme-aware `ButtonStyles`/`LabelStyles` (23-153) ARE actively used via `setStyleSheet(ButtonStyles.PRIMARY)` etc. in `main_window.py`, `dialogs/tunnel_config.py`, and `dialogs/group_dialog.py` (confirmed by grep for `ButtonStyles\.` / `LabelStyles\.`). Because a widget's own `setStyleSheet()` wins over the app-wide stylesheet from `get_full_app_style()` (confirmed: `get_full_app_style`, lines 708-753, wires in dynamic input/table/tab/list/scrollbar/progress/groupbox styles but never button or label styles), every button/label styled this way is permanently pinned to the light-theme hex values and never updates on dark-mode switch.
- **권고**: Migrate the three call-site files off `ButtonStyles`/`LabelStyles` onto the already-written `get_dynamic_button_style(variant)`/`get_dynamic_label_style(variant)`, then delete the now-fully-dead `TableStyles`/`TabStyles`/`DialogStyles`/`ProgressStyles`/`TextEditStyles`/`Colors`/`apply_button_style`/`apply_label_style`, and finally `ButtonStyles`/`LabelStyles`/`InputStyles`/`GroupBoxStyles` once migration is complete.
- **검증 메모**: Core claim fully confirmed. One correction: the finding lists `widgets/tunnel_tree.py` as one of the four active call sites of ButtonStyles, but that file only has a dead/unused `from src.ui.styles import ButtonStyles` import (line 14) with zero actual `ButtonStyles.X` usages — only 3 files (main_window.py, tunnel_config.py, group_dialog.py) actually call it. This doesn't weaken the finding (if anything it adds one more piece of dead code to clean up).

### `src/ui/workers/migration_worker.py:16-83` — MigrationAnalyzerWorker constructor takes 16 parameters, 14 of them boolean flags mirrored three times
- **카테고리**: 파라미터과다
- **문제**: Corrected count from the original finding: `__init__` (16-36) accepts `connector`, `schema`, plus 14 individual `check_*` boolean flags (check_orphans, check_charset, check_keywords, check_routines, check_sql_mode, check_auth_plugins, check_zerofill, check_float_precision, check_fk_name_length, check_invalid_dates, check_year2, check_deprecated_engines, check_enum_empty, check_timestamp_range) — 16 parameters total, not 17/15 as originally stated. Each is assigned to `self.x = x` one line at a time (39-55), then forwarded again by name, one per line, to `analyzer.analyze_schema(...)` in `run()` (62-79). The same 14 identifiers are typed out three separate times (constructor parameter, `self.` attribute, forwarded keyword argument), so adding a 15th check requires editing all three places plus every call site that constructs this worker.
- **권고**: Bundle the 14 `check_*` flags into a single `MigrationCheckOptions` dataclass (or plain dict) and accept one `options: MigrationCheckOptions` parameter instead; `run()` can then forward it as `analyzer.analyze_schema(self.schema, **asdict(self.options))` or pass the object straight through if `analyze_schema` is updated to accept it.
- **검증 메모**: Original finding miscounted the parameters: actual count is 16 total parameters (connector + schema + 14 check_* flags), not 17 with 15 booleans. Corrected numbers in title/detail/recommendation; severity (high) remains justified given the triple-duplication maintenance burden.

### `src/ui/workers/test_worker.py:52-230` — _test_db and _test_integrated duplicate ~80% of their connection-resolution and cleanup logic
- **카테고리**: 중복
- **문제**: Confirmed: both `_test_db` (52-141) and `_test_integrated` (143-230) implement the identical three-way connection resolution — direct mode uses `remote_host`/`remote_port`; an already-running tunnel uses `engine.get_connection_info`; otherwise a bastion-reachability check (`test_target_reachable_from_bastion`) followed by `create_temp_tunnel`/`get_temp_tunnel_port` — and the identical `finally` cleanup (disconnect connector, `close_temp_tunnel`, emit `test_finished`). The two methods differ mainly in progress-message wording and result-string assembly.
- **권고**: Extract a shared `_resolve_connection(self) -> tuple[str, int, temp_server | None]` (or a small context manager wrapping temp-tunnel creation/cleanup) used by both `_test_db` and `_test_integrated`, leaving each method with only its own result-formatting logic.

---

## MEDIUM 심각도 (134건) — 요약

파일별로 묶어서 제시. 개별 상세는 `.claude/investigation-clean-code-audit-2026-07-09.json`의 원본 데이터를 참고.

#### `main.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 99 | 중복 | Five identical lazy-import trampoline functions whose names shadow the classes they return | Replace the five functions with one helper, e.g. `def _lazy_class(module_path: str, name: str)`, called as `_lazy_class("PyQt6.QtWidgets", "QApplication")`; if kept as separate named functions, rename them to avoid shadowing the class names (e.g. `get_qapplication_class`). |

#### `migration_core/src/lib.rs` (16건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 2526 | 파라미터과다 | Dump pipeline functions repeat the same 9-11 positional parameters across many call sites | Introduce a `DumpJobContext` (or `DumpOptions`) struct — placed near the existing `DumpParallelLimits` struct — bundling `endpoint: Endpoint`, `output_path: PathBuf`, `chunk_size: usize`, `data_format: String`, `compression: String`, and `request_id: Option<String>`, and pass it by shared reference (`&DumpJobContext`) instead of six separate positional args. This cuts `spawn_dump_table_worker`, `dump_one_table`, and `dump_mysql_table_parallel_ranges` down to 5-6 parameters (context + table-specific args) and removes the same-typed-argument ordering risk at the three duplicated call sites in `dump_tables_parallel`. |
| 2945 | 중복 | dump_one_table and dump_one_mysql_table duplicate table-dir/progress-event scaffolding around different row-fetch strategies | Factor the shared scaffolding into a helper, e.g. `fn run_table_dump_loop<F>(table: &NormalizedTable, index: usize, table_total: usize, output_path: &Path, request_id: Option<String>, emit: &mut F, mut fetch_next_chunk: impl FnMut(u64) -> Result<Option<ChunkOutcome>, String>) -> Result<(DumpTableManifest, u64, u64), String>` (where `ChunkOutcome { rows: u64, chunk_name: String, checksum: String }`), and have both `dump_one_table` and `dump_one_mysql_table` supply only their chunk-fetch closure. |
| 4612 | 중복 | Three independent hand-rolled SQL comment scanners on the View-definition sanitization/validation path, with inconsistent comment-dialect coverage | Extract a single `fn skip_sql_comment(bytes: &[u8], i: usize) -> Option<usize>` (returning the index just past a comment if `bytes[i..]` starts one, recognizing `--`, `#`, and `/* */` consistently) and have all three call sites (`strip_leading_comments_and_parens`, `mysql_definition_has_residual_definer`, `validate_single_view_statement`) use it, so 'what counts as a comment' is defined once. |
| 4828 | 중복 | inspect_mysql and inspect_postgresql duplicate the same 5-step per-table inspection sequence for each engine | Introduce a `trait InspectAdapter { fn table_names(&mut self, schema: &str) -> Result<Vec<(String, Option<String>)>, String>; fn columns(&mut self, schema: &str, table: &str) -> Result<Vec<NormalizedColumn>, String>; fn keys(...); fn foreign_keys(...); fn indexes(...); }`, implement it once for a MySQL wrapper (over `mysql::PooledConn`) and once for a PostgreSQL wrapper (over `postgres::Client`), and factor the shared per-table loop into one generic `fn inspect_generic<A: InspectAdapter>(adapter: &mut A, schema: &str) -> Result<InspectionResult, String>` that both `inspect_mysql` and `inspect_postgresql` call after constructing their respective adapter. |
| 5656 | 중복 | Identical 10-line MigrationIssue-for-validation-failure block duplicated verbatim in oneclick_run_streaming and oneclick_validate | Extract `fn issues_from_inspect_result(result: Result<InspectionResult, String>) -> Vec<MigrationIssue>` that performs the Ok -> oneclick_issues_from_inspection / Err -> single validation-error MigrationIssue mapping, and call it from both `oneclick_run_streaming` (replacing lines 5656-5668) and `oneclick_validate` (replacing lines 5976-5988). |
| 6797 | 중복 | oneclick_apply_actions and oneclick_dry_run_preview_fixes duplicate the same per-step classification logic, and the real-apply path has an extra sql_template validation the preview path lacks | Extract a shared classifier, e.g. `fn classify_oneclick_step(step: &Value, schema: &str) -> OneClickStepClassification` returning an enum (`Skip`, `Disallowed(String)`, `Charset(Value)`, `Engine{table, sql}`) that performs the sql_template check as part of the Engine branch, and have both `oneclick_apply_actions` and `oneclick_dry_run_preview_fixes` build their respective outputs (real `OneClickApplyAction` vs. preview JSON with `dry_run: true`) from the same classification result so the sql_template validation can't silently diverge between preview and real execution again. |
| 7599 | 중복 | Identical source/target endpoint-resolution boilerplate (including a defensive unreachable!()) repeated 4 times across migrate_streaming and verify | Extract a shared `fn required_endpoint(payload: &Value, key: &str) -> Result<Endpoint, String>` that does the `.get(key)` + `endpoint_from_value` + `?` chain internally, turning the 'missing key' case into a real `Err(...)` instead of `unreachable!()` (removing the panic-path landmine entirely). Callers then do `let source_endpoint = match required_endpoint(&request.payload, "source") { Ok(e) => e, Err(err) => { emit(...); return; } };`, cutting 4 near-identical ~13-line blocks to 4 one-line calls plus their (already-differing) error-reporting arms. |
| 8167 | 거대함수 | migrate_with_adapters_reporting mixes DDL generation, dependency-ordered per-table copy loop, keyset/offset pagination selection, and cancellation logic in one ~168-line function | Extract the per-table body (create table + keyset/offset decision + chunked read/insert loop + progress emission, lines 8217-8310) into a helper such as `fn copy_table_rows<S: MigrationAdapter, T: MigrationAdapter>(table, table_ddl, state, state_index, source, target, chunk_size, cancel_after_chunks, rows_copied, chunks_copied, on_event) -> Result<TableCopyOutcome, MigrationResult>`. `migrate_with_adapters_reporting` then reads as: validate -> build ddl -> for each table call copy_table_rows -> apply_post_load_ddl. |
| 8435 | 거대함수 | verify_with_adapters_reporting mixes row-count checks, digest-based comparison, and keyset-ordered row comparison in one ~168-line function | Split into two named helpers, `fn verify_table_by_digest(source, target, table, chunk_size, emit) -> Vec<Value>` and `fn verify_table_by_keyset(source, target, table, key_columns, chunk_size, total_rows, emit) -> Vec<Value>`, each returning mismatches, and have the outer loop do the count check then dispatch based on `key_columns.is_empty()`. Turns the 168-line function into a short dispatcher plus two independently testable strategies. |
| 10580 | 중복 | insert_rows_literal_sql and insert_rows_literal_sql_for_table duplicate the same INSERT-building logic | Extract a shared private helper `fn insert_values_sql(rows: &[Value], column_names: &[&str], literal_for: impl Fn(&str, &Value) -> String) -> String` and have both public functions build their column-name list plus a closure over `sql_literal`/`sql_literal_for_column`, delegating row/value formatting and the final `INSERT INTO ... VALUES ...` assembly to the shared helper. |
| 10737 | SRP위반 | MySQL and PostgreSQL value/DDL logic is interleaved via repeated engine-name string comparisons instead of per-engine dispatch | Introduce a small dialect abstraction, e.g. `trait SqlDialect { fn literal_for_column(&self, source_type: &str, value: &Value) -> String; fn csv_field(&self, source_type: &str, value: &Value) -> String; fn default_literal(&self, default_value: &str, source_type: &str) -> String; }` with `MysqlDialect`/`PostgresqlDialect` impls, and have `copy_csv_field_for_column`, `sql_literal_for_column`, and `map_default_literal` become thin callers that pick the dialect once via `target_engine` and delegate. This is a design-clarity improvement rather than a bug fix, so it's reasonable to treat as optional/medium priority rather than blocking. |
| 10775 | 중복 | sql_literal_for_column repeats the identical mysql-json/mysql/fallback branch twice in the same function | Factor the shared tail into `fn mysql_or_generic_literal(target_engine: &str, source_type: &str, value: &Value) -> String { if target_engine == "mysql" && is_json_type(source_type) { return mysql_json_literal(value); } if target_engine == "mysql" { return mysql_sql_literal(value); } sql_literal(value) }`, and call it once at the end of the String-specific branch (after the postgres-only special cases) and once for the non-String fallback, removing the duplicated 3-line block at 10811-10816. |
| 11023 | 거대함수 | generate_table_ddl mixes column-DDL building, primary-key collection, and MySQL collation-suffix security validation in one function | Split into `fn column_ddl_lines(table: &NormalizedTable, source: &str, target: &str) -> Option<(Vec<String>, Vec<String>)>` (DDL lines + primary key column list, covering job 1+2) and `fn mysql_table_collation_suffix(source: &str, target: &str, table: &NormalizedTable) -> Option<String>` (covering job 3), then have generate_table_ddl call both and assemble the final CREATE TABLE string — keeping the security-relevant is_safe_column_type/is_valid_mysql_collation_ident checks each in a small, focused unit. |
| 11185 | 네이밍 | Cryptic single/double-letter index variables (ds, ws, ws2, is2, ss) reused for different meanings inside is_safe_column_type | Rename to intention-revealing, scope-specific names, e.g. `numeric_list_start` (11186), `varying_length_start` (11247), `modifier_word_start` (11223), `time_zone_word_start` (11268), `charset_or_collate_value_start` (11281), `set_keyword_start` (11293), `character_set_name_start` (11303). If the is_safe_column_type split from the long-function finding is done first, most of these become locals inside small dedicated parsers and naturally get clearer names without needing abbreviations. |
| 14112 | 중복 | Widespread copy-pasted `NormalizedTable`/`NormalizedColumn`/`DumpManifest` struct literals instead of shared test builders | Add two small test-only builder functions near the existing `empty_table`/`single_pk_table_with_collation` helpers: `fn single_numeric_pk_table(name: &str, type_name: &str) -> NormalizedTable` (covers the four bigint-PK cases at 14113-14194 and the mysql/postgres auto-increment pair at 14953-14984 by parameterizing name+type_name) and `fn sample_dump_manifest(views: Vec<NormalizedView>) -> DumpManifest` (covers the 16856-16900 pair by parameterizing only `views`, defaulting the other 12 fields to the values already used at 16858-16871). Update the affected ~8 tests to call these builders instead of repeating the full literal, shrinking each test to the 1-2 lines that differ. |
| 15940 | 중복 | Two tests with different names are byte-for-byte identical (zero differentiated coverage) | Either delete `mysql_json_literal_uses_utf8mb4_introducer_for_unicode_json_text` as a pure duplicate, or rewrite its body to use a distinct `json_text` with no embedded backslash/quote (e.g. `r#"{"facts":[{"content":"문서 제목은 표기되어 있다"}]}"#"` or plain ASCII like `r#"{"a":1}"#`) so the assertion actually proves the `_utf8mb4` introducer is applied even when there is nothing to escape -- which is what the test name claims to verify. |

#### `scripts/bump_version.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 121 | 중복 | Repeated try/except/print pattern for syncing version.py, pyproject.toml, and the installer script | Factor out a helper, e.g. `_apply_sync(sync_fn, path: Path, new_version: str, label: str, required: bool) -> int / None`, that performs the optional existence check, try/except, and OK/ERROR/SKIP logging once, and call it three times from `main()`. |

#### `src/core/config_manager.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 573 | 중복 | Read-modify-write pattern (`with _CONFIG_LOCK: load_config() -> mutate -> save_config()`) repeated verbatim 7 times | Add a private helper `_mutate_config(self, mutator: Callable[[dict], T]) -> T` doing `with _CONFIG_LOCK: config = self.load_config(); result = mutator(config); self.save_config(config); return result`, and have all 7 call sites pass a small closure instead of repeating the lock/load/save scaffolding. |

#### `src/core/cross_engine_migration.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 1 | SRP위반 | Module docstring promises only wire-format isolation, but the file also owns executable discovery, disk persistence, and report rendering | Split per concern: keep enums/dataclasses/parse/build (DatabaseEngine, MigrationDirection, MigrationIssue, HelperEvent, parse_helper_event, build_helper_request) in this file; move executable discovery to cross_engine_executable.py; move resume-state persistence to cross_engine_state.py; move render_result_report into src/core/migration_report_renderer.py, which already exists in this repo as the connector-free renderer for MigrationReport. |

#### `src/core/db_connector.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 165 | 중복 | MySQLConnector re-implements schema listing/existence queries that its own RustDbConnector delegate already provides | Have MySQLConnector.get_schemas/schema_exists call self._delegate.get_schemas()/self._delegate.schema_exists() (wrapping only the TTL cache around the delegate call), and give PostgresConnector a RustDbConnector delegate the same way MySQLConnector does, instead of hand-rolling the information_schema.schemata query a third time. |
| 300 | 중복 | MySQLConnector.get_db_version manually re-parses version strings instead of reusing parse_db_version_tuple | Replace the body of MySQLConnector.get_db_version with `return parse_db_version_tuple(self.get_db_version_string())`, importing `parse_db_version_tuple` from `src.core.db_core_service`. |

#### `src/core/db_core_service.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 453 | 파라미터과다 | RustDbConnector.__init__ takes 7 loose primitive params that are immediately repackaged into the DbEndpoint it already has | Change RustDbConnector.__init__ to accept `endpoint: DbEndpoint` (plus `facade`) directly, and have create_rust_db_connector build the DbEndpoint once (it already computes resolved_engine/default_database_for_engine) and pass it straight through. |

#### `src/core/github_app_auth.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 248 | 에러처리불일치 | get_installation_token() reports failures via print() instead of the shared logger used elsewhere in the codebase | Add `from src.core.logger import get_logger; logger = get_logger(__name__)` to this module and replace the print() call with `logger.error(...)`, matching the pattern used throughout the rest of src/core. |
| 322 | SRP위반 | Build-time secret-embedding tooling (_obfuscate/_deobfuscate/generate_embedded_code) is bundled into the runtime auth class | Move the obfuscation codec (_obfuscate/_deobfuscate) and generate_embedded_code into a separate small module (e.g. `scripts/github_app_secret_codec.py`) used only by the build pipeline, and have GitHubAppAuth import just the deobfuscate function it needs at runtime. |

#### `src/core/github_issue_reporter.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 23 | 갓클래스 | GitHubIssueReporter combines error-text sanitization/parsing, markdown report generation, similarity-based dedup, and a GitHub API client with retry logic | Split into an `ErrorSummaryBuilder` (pure functions: sanitize/extract_core_error/generate_issue_body/generate_fingerprint/summarize_error, no network dependency) and a `GitHubIssueClient` (find_similar_issue/create_issue/add_comment/report_error, taking a pre-built summary dict), composed by whatever currently instantiates GitHubIssueReporter. |
| 383 | 기타 | add_comment() re-derives the raw error text by string-splitting the rendered markdown body instead of reusing already-available data | Have summarize_error() also store the raw/sanitized full message in the returned dict (e.g. `summary['full_message'] = sanitized_message`), and have add_comment build its preview from `summary['full_message'][:1000]` directly, removing the fragile split-based re-parsing entirely. |

#### `src/core/i18n.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 1351 | 추상화수준혼재 | translate_text() mixes dictionary lookups with generic Korean-grammar regex surgery in one function | Break the function into named steps that mirror the strategies: _lookup_exact(value), _apply_regex_pairs(value), _apply_phrase_substitutions(value), _apply_word_substitutions(value), _strip_korean_particles(value). Have translate_text() become a short pipeline that calls each in order and returns early on the first two. This makes each strategy independently testable and makes the particle-stripping regexes (the riskiest, most surprising part) visible as a distinct, nameable operation instead of being buried at the tail of a generic-sounding function. |
| 1371 | 기타 | translate_text() re-sorts ~878 dictionary entries on every single call that reaches this code path | Precompute the sorted views once at module load time, e.g. `_SORTED_PHRASE_TRANSLATIONS = tuple(sorted(_EN_PHRASE_TRANSLATIONS.items(), key=lambda kv: len(kv[0]), reverse=True))` and the word-table equivalent, and iterate those precomputed tuples inside translate_text() instead of calling sorted() on the raw dicts each time. |
| 1517 | 중복 | Eight near-identical hand-rolled monkey-patch blocks in install_qt_i18n | Extract a single generic helper, e.g. `_patch_free_function(container, name, positional_indices=(), kwarg_names=(), translate_all_args=False)`, that covers the wrapped-guard + closure + reassignment logic once, and have all 8 call sites (plus patch_method/patch_all_string_args_method) call it with different arguments instead of re-implementing the scaffolding. |

#### `src/core/migration_analyzer.py` (4건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 179 | 중복 | MySQL 8.4 reserved-keyword list is hand-duplicated instead of reusing the single source of truth | Delete MigrationAnalyzer.NEW_RESERVED_KEYWORDS entirely; import and use ALL_RESERVED_KEYWORDS from migration_constants.py directly in both check_reserved_keywords (migration_analyzer.py:422) and _analyze_sql_file (migration_dump_analyzer.py:258), removing the local `from src.core.migration_analyzer import MigrationAnalyzer` import in migration_dump_analyzer.py:140. |
| 290 | 중복 | find_orphan_records duplicates the NOT-EXISTS-vs-LEFT-JOIN branch twice (count query and sample query) | Extract a private helper, e.g. `_build_orphan_query(schema, fk, is_large, select_expr, limit=None)`, that returns the correct SQL text for either strategy, and call it once for the COUNT(*) form and once (with a LIMIT and DISTINCT column) for the sample form. |
| 639 | 파라미터과다 | analyze_schema / _analyze_schema_impl take 16 parameters, 15 of them boolean flags, duplicated across two signatures | Bundle the flags into a single SchemaCheckOptions dataclass (or a Set[str]/FrozenSet of enabled check names defaulting to 'all') and change both signatures to analyze_schema(schema: str, options: SchemaCheckOptions = SchemaCheckOptions()), eliminating the 15-parameter pass-through duplication between the two methods. |
| 885 | 중복 | Every MySQL-8.4 compatibility check repeats the same log/query/build-issues/log-summary boilerplate | Introduce a small declarative spec per check, e.g. a dataclass CheckSpec(query, issue_type, severity, describe_fn, suggest_fn), and a single `_run_column_scan(schema, spec)` helper that all these methods call (or are replaced by), so a new 8.4 rule is one new spec entry instead of a new ~15-line method. |

#### `src/core/migration_constants.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 433 | SRP위반 | A module documented as pure 'constants' contains two non-trivial parsing/matcher classes | Move `_IdentifierIssuePattern` and `_ContextualDotPattern` (and the module-level instances DOLLAR_SIGN_PATTERN, TRAILING_SPACE_PATTERN, CONTROL_CHAR_PATTERN, INVALID_57_NAME_MULTIPLE_DOTS_PATTERN that depend on them) into a new module, e.g. migration_identifier_matchers.py, keeping migration_constants.py limited to the plain data its docstring promises. |

#### `src/core/migration_dump_analyzer.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 130 | 거대함수 | _analyze_sql_file performs 8 unrelated compatibility scans inline in one ~155-line function | Split into one small private helper per check (e.g. `_check_zerofill(content, file_name)`, `_check_auth_plugins(content, file_name)`, ...), each returning List[CompatibilityIssue], with `_analyze_sql_file` reduced to reading the file, calling each helper, and concatenating results. |

#### `src/core/migration_fix_generator.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 18 | 중복 | FixQueryGenerator duplicates SmartFixGenerator's fix logic for 7 overlapping IssueTypes, but it currently has zero production callers — the real user-facing duplication is between MigrationAnalyzer's own inline fix_query strings and SmartFixGenerator | First confirm intent: if FixQueryGenerator has no planned caller, delete it as dead legacy code (consistent with this codebase's stated pattern of removing vestigial pre-Rust-Core Python logic, e.g. AutoRecommendationEngine per tests/test_migration_mapping_coverage.py's docstring) along with its two dedicated test files. If it is meant to eventually back a fix_query display surface, do not maintain a third copy — have MigrationAnalyzer's issue-detection methods (migration_analyzer.py lines 1041/1114/1152/1229) delegate to SmartFixGenerator(connector, schema).get_fix_options(issue) for the 7 overlapping IssueTypes instead of building their own inline SQL, and drop FixQueryGenerator's duplicate handlers for those 7 types. |

#### `src/core/migration_fix_wizard.py` (5건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 44 | 중복 | Identical lazy FK-graph-builder init/cache logic repeated verbatim in four classes | Add a shared helper, e.g. a `CollationFKGraphBuilder.get_or_create(connector, schema, cache: dict)` classmethod or a module-level `get_or_build_fk_graph(cache_holder, connector, schema)` function, and have all four classes call it instead of reimplementing the lazy-init. |
| 236 | 매직값 | Target charset/collation "utf8mb4"/"utf8mb4_unicode_ci" hardcoded as literals in 8+ places | Define DEFAULT_TARGET_CHARSET = "utf8mb4" and DEFAULT_TARGET_COLLATION = "utf8mb4_unicode_ci" once in migration_fix_models.py (no existing constant found there via grep) and import/reuse them everywhere instead of literals, including as default parameter values for generate_safe_charset_sql/execute_safe_charset_change/generate_fix_sql. |
| 840 | 오래된주석 | BatchFixExecutor class docstring claims transactional execution and FK_CHECKS wrapping it no longer does | Rewrite the docstring to state this class only produces a dry-run/estimate preview (real execution is owned by Rust Core; dry_run=False raises), and remove the FOREIGN_KEY_CHECKS bullet since that behavior belongs to SmartFixGenerator, not BatchFixExecutor. |
| 888 | 에러처리불일치 | Broad except-Exception blocks silently swallow both expected and unexpected failures | Narrow the except clauses to specific expected failure types where possible, and at minimum use logging.exception (not just str(e)) so failures are diagnosable. For the sql_mode restore specifically, emit a warning log if restoration fails, since a silently-altered session mode can affect correctness of later dry-run estimates in the same connection. |
| 1328 | 중복 | Table charset/collation lookup query duplicated near-identically in two files | Extract one shared function, e.g. `get_table_charset(connector, schema, table) -> Tuple[str, str]` in migration_fix_models.py next to `_format_default_sql_clause`, and have both call sites use it (RollbackSQLGenerator can keep its own dict-shaped cache wrapper around the shared call). |

#### `src/core/migration_parsers.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 738 | 중복 | extract_create_table_statements / extract_create_user_statements / extract_grant_statements are structurally identical | Replace the three methods with a single private helper `_extract_statements(self, pattern: re.Pattern, content: str) -> List[str]`, and hoist the three regexes into precompiled class-level constants (e.g. `_CREATE_TABLE_STMT_PATTERN`, `_CREATE_USER_STMT_PATTERN`, `_GRANT_STMT_PATTERN`) so each public method becomes a one-line call. |

#### `src/core/migration_rollback_sql_generator.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 180 | 거대함수 | generate_rollback_sql mixes 4 unrelated rollback strategies in one ~146-line function | Extract one private helper per strategy: _rollback_date(step), _rollback_collation_single(step, original_state), _rollback_collation_fk(step, original_state, all_pre_states), each returning its own SQL string; have generate_rollback_sql become a short dispatcher on strategy plus the shared location-parsing preamble. |

#### `src/core/migration_rules/data_rules.py` (5건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 166 | SRP위반 | Hand-rolled SQL tokenizer (quote/paren state machine) embedded as private methods of DataIntegrityRules | Move `_find_statement_end`, `_iter_create_table_statements`, `_iter_values_rows`, and `_split_sql_values` out of `DataIntegrityRules` into `migration_parsers.py` as module-level functions or a small `SqlStatementScanner` class, placed near `CreateTableParser` since `_iter_create_table_statements` already delegates parsing to it. `DataIntegrityRules.check_enum_numeric_index` should then import and call the shared tokenizer instead of owning it. |
| 344 | 깊은중첩 | check_enum_numeric_index nests 5 levels deep with flag-based loop breaking | Extract the innermost row/column scanning into a helper, e.g. `_find_numeric_enum_value(self, values: list, enum_col_indices: list, cols: list) -> Optional[Tuple[str, str]]` returning the first `(column_name, value)` hit or `None`. Call it from a flat `for row_body in self._iter_values_rows(values_segment): result = self._find_numeric_enum_value(self._split_sql_values(row_body), enum_col_indices, cols); if result: ...; break`, removing the boolean flag and dropping nesting from 5 levels to 2-3. |
| 542 | 중복 | Four file-scanning rule checks (D06/D07/D08/D11) duplicate the same read-loop/truncation/magic-number scaffolding | Promote `max_lines`/`max_samples` to class constants (e.g. `_MAX_SCAN_LINES = 10000`, `_MAX_SAMPLE_VALUES = 3`) matching the existing `_MAX_COLUMNS_TO_CHECK` convention, and extract a shared `_scan_file_lines(self, file_path, mode, per_line_check)` template method owning the loop/truncation/exception/SCAN_TRUNCATED-issue boilerplate. Each of the four `check_*` methods then supplies only its line-matching predicate and issue-construction callback. |
| 726 | 중복 | check_latin1_non_ascii and check_zerofill_data_dependency duplicate the same batch-scan/groupby/partial-scan skeleton almost line-for-line | Extract the shared skeleton into a private helper `_batch_scan_columns(self, schema, columns, *, build_query, issue_type, severity, describe)` that owns the partial-scan cap, `groupby`/`_table_key` grouping, per-table query execution/exception handling, and the partial-scan info issue. Both `check_latin1_non_ascii` and `check_zerofill_data_dependency` would then only supply their column-selection query and a small per-column SQL-fragment/issue-description callback. |
| 1009 | 에러처리불일치 | check_invalid_datetime silently swallows file-read errors while its three sibling scan functions surface them as an issue | Add an info-severity `CompatibilityIssue` (using `issue_type=IssueType.INVALID_DATE`, matching the pattern used in the other three functions) to check_invalid_datetime's except block, e.g. `description=f"DATETIME 스캔 미완료: {str(e)[:80]}"`, so all four D06/D07/D08/D11 file scans consistently surface read failures. |

#### `src/core/migration_rules/schema_rules.py` (4건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 14 | 오래된주석 | Rule ID 'S16' is documented for two different, unrelated checks in two different modules | Renumber the non-InnoDB-engine-FK check (storage_rules.py, introduced later per the '이슈 #63' annotation) to a distinct unused ID such as S32, keeping S16 for the original generated-column-function rule which was documented first in the S01-S31 schema catalogue. |
| 61 | 갓클래스 | SchemaRules bundles ~29 methods / 850 lines spanning at least five unrelated rule families | Split `SchemaRules` into cohesive sub-modules under `migration_rules/`, e.g. `identifier_rules.py` (S07-S09, S27, S30, S31), `index_charset_rules.py` (S02-S04, including `calculate_column_byte_size`), `definer_rules.py` (S23-S25, including `_fetch_existing_definers_or_issue`), and `syntax_rules.py` (S05, S06, S16-S18, S28, S29, including `_matches_sql_function_call`), each exposing its own `check_all_live_db`/`check_all_sql_content`, composed by a thin facade or directly by the caller — mirroring how `migration_rules/__init__.py` already composes `StorageRules`, `SchemaRules`, `DataIntegrityRules`. |
| 64 | 중복 | Identical progress-logging boilerplate and 'summary log' pattern duplicated verbatim across all three rule classes | Introduce a shared base class (e.g. `ProgressLoggingRuleBase` in a new `migration_rules/_base.py`) providing `__init__`, `set_progress_callback`, `_log`, and a `_log_summary(issues, item_label)` helper encapsulating the 'if issues: warn else: success' pattern. Have `SchemaRules`, `StorageRules`, and `DataIntegrityRules` inherit from it, replacing ~14 duplicated 3-4 line blocks with single calls. |
| 123 | 중복 | Identical 3-line 'extract source line around match' snippet copy-pasted 7 times | Extract a helper `_extract_source_line(content: str, match: re.Match) -> str` defined once on `SchemaRules` (or as a shared module-level function if `DataIntegrityRules`/`StorageRules` need it too), and have all 7 call sites use it instead of re-deriving line boundaries inline. |

#### `src/core/migration_rules/storage_rules.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 17 | 죽은코드 | INVALID_ENGINE_FK_PATTERN imported but never used; check_invalid_engine_fk reimplements its own inline regex instead | Either delete the unused `INVALID_ENGINE_FK_PATTERN` import, or extend `INVALID_ENGINE_FK_PATTERN` in migration_constants.py to also capture the table name (e.g. add a named group `(?P<table>\w+)` after `CREATE\s+TABLE\s+`?`) so `check_invalid_engine_fk` can adopt the shared constant directly via `match.group('table')`, eliminating the private duplicate and making this the single source of truth as the file's existing `_engine_policy` docstring already promises for `ENGINE_POLICIES`. |

#### `src/core/platform_integration.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 140 | SRP위반 | StartupRegistrar implements two unrelated OS-specific startup mechanisms (Windows registry Run key and macOS LaunchAgent plist) in a single class | Split into per-platform strategy objects (WindowsStartupRegistrar, MacOSStartupRegistrar) behind a common is_registered/set_registered interface, with a thin StartupRegistrar facade picking the strategy by platform_name. |

#### `src/core/platform_paths.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 24 | 중복 | Windows and macOS base-directory resolution is copy-pasted verbatim across app_support_dir, data_dir, and log_dir | Extract a shared helper `_platform_base_dir(system, home_path, env, xdg_env_var, xdg_default_subdir) -> Path` encapsulating the common Windows/Darwin logic, parameterized only by the Linux-specific XDG variable name/subpath; have app_support_dir/data_dir/log_dir each call it. |

#### `src/core/production_guard.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 76 | 거대함수 | SchemaConfirmDialog._init_ui builds the entire dialog (header, labels, input, buttons, all with inline QSS) in one 131-line method | Break _init_ui into focused builders, e.g. `_build_header_frame()`, `_build_details_section(details)`, `_build_schema_input_section()`, `_build_button_row()`, each returning a widget/layout that _init_ui assembles. |

#### `src/core/scheduler.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 654 | 중복 | Retention-cleanup algorithm duplicated between _cleanup_old_backups and _cleanup_old_results | Extract a shared helper `_select_paths_for_retention(entries: List[Tuple[str, datetime]], retention_days: int, retention_count: int) -> List[str]` that both methods call after building their own (path, timestamp) list; keep only the listing/deletion side effects in each caller. |
| 708 | 네이밍 | _log_backup is used to log both backup runs and unrelated SQL-query task runs | Rename to `_log_execution` (update docstring and log-file naming/variable names accordingly) so the method's name matches its actual scope of use across both task types. |
| 885 | 네이밍 | Variable named 'engine' actually holds an endpoint object, then reused to derive 'engine_name' | Rename the intermediate variable to `endpoint` and/or thread `resolved.engine` (already computed in _execute_sql_query) into _execute_single_query as a parameter instead of re-deriving it from the connector's internals. |

#### `src/core/schema_diff_models.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 79 | 중복 | No shared identifier-quoting helper; backtick-wrapping logic is hand-rolled in 3 dataclasses | Add module-level helpers `_quote_ident(name: str) -> str: return f"`{name}`"` and `_quote_idents(names: List[str]) -> str: return ", ".join(_quote_ident(n) for n in names)` next to `_normalize_column_extra` in schema_diff_models.py, and use them from all three `to_sql_definition` methods (and from schema_sync_script_generator.py's own inline identifier quoting) instead of each call site re-implementing it. |

#### `src/core/schema_extractor.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 65 | 기타 | N+1 query pattern per table, plus a redundant full-table-scan COUNT(*) where an approximate count is already available | Add `TABLE_ROWS` to the SELECT in `_get_table_options` (line 219) and drop the separate `_get_row_count` full scan where an exact count isn't required for a diff summary. For columns/indexes/FKs, consider fetching all rows for the whole schema once (drop the per-table `TABLE_NAME = %s` filter, group by table name in Python) instead of once per table, cutting 1 + 5N round trips to a small constant number. |

#### `src/core/schema_sync_script_generator.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 40 | 중복 | The `ALTER TABLE `{target_schema}`.`{diff.table_name}`` prefix is re-typed 13 times | Add `def _alter_table(target_schema: str, table_name: str, clause: str) -> str: return f"ALTER TABLE `{target_schema}`.`{table_name}` {clause};"` as a module-level helper or SyncScriptGenerator method, and call it at all 13 sites (lines 40, 47, 53, 95, 100, 105, 116, 120, 127, 132, 137, 152, 160) with just the suffix clause as an argument. |
| 111 | 매직값 | The literal 'PRIMARY' index name is checked with inconsistent case-sensitivity across 3 files | Define `PRIMARY_KEY_INDEX_NAME = "PRIMARY"` and `def is_primary_key_index(name: str) -> bool: return name.upper() == PRIMARY_KEY_INDEX_NAME` in schema_diff_models.py alongside `_normalize_column_extra`, and use both at all 4 call sites (schema_sync_script_generator.py:111, :191, schema_diff_models.py:115, schema_severity_classifier.py:157) instead of ad-hoc literal comparisons. |

#### `src/core/sql_history.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 36 | 에러처리불일치 | sql_history.py never uses the shared logger and silently swallows corrupted/unwritable history with no trace | Import `get_logger('sql_history')` as other core modules do, log a warning/error in _load_history's except-branch before returning [], and replace the bare print() in _save_history with logger.error(...). |

#### `src/core/sql_validator.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 406 | 중복 | SQLValidator keeps its own private SYSTEM_SCHEMAS list instead of importing the shared constant, and it has already drifted | Delete SQLValidator.SYSTEM_SCHEMAS and import SYSTEM_SCHEMAS from src.core.constants instead (adding NDBINFO to the central constant if it should be excluded from schema listings too), establishing one source of truth for system-schema exclusion. |
| 595 | 중복 | _validate_version_compatibility contains two near-identical scan loops for keywords vs. functions | Extract a shared helper `_flag_unsupported_items(sql, items, pattern_fn, message_fn, string_regions, line_offsets, major, minor)` and call it twice (once for MYSQL8_KEYWORDS, once for MYSQL8_FUNCTIONS), removing the duplicated loop body. |
| 727 | 거대함수 | SQLAutoCompleter.get_completions inlines context detection, table completion, two column-completion branches, and keyword/function completion | Extract `_complete_tables(metadata, prefix)`, `_complete_columns_for_table(sql, metadata, target_table, prefix)`, `_complete_columns_from_from_clause(sql, metadata, prefix)`, and `_complete_keywords_and_functions(prefix)` helpers; have get_completions dispatch to them based on context. |

#### `src/core/tunnel_engine.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 112 | 중복 | SSHTunnelForwarder construction plus connection-log building repeated 3 times | Extract a private helper `_build_forwarder(config, local_bind_address, set_keepalive=None) -> SSHTunnelForwarder` that resolves the shared kwargs from `config` once, reused by all three call sites; factor the repeated connection-log accumulation into a small helper/class. |

#### `src/core/tunnel_monitor.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 81 | 갓클래스 | TunnelMonitor combines state-machine monitoring, DB health-check connection management, and auto-reconnect orchestration | Extract a `TunnelHealthChecker` (owns _health_connections, _measure_latency, _create_health_connection, _get_health_credentials) and a `TunnelReconnector` (owns _attempt_reconnect and its backoff schedule) as separate collaborators composed by TunnelMonitor, leaving TunnelMonitor responsible only for state transitions and the event/callback log. |
| 549 | 추상화수준혼재 | Nested reconnect() closure mixes low-level threading/sleep with high-level reconnect business logic and recurses on itself | Pull the closure out into a named method, e.g. `_reconnect_after_delay(self, tunnel_id, delay, status)`, invoked via `threading.Thread(target=self._reconnect_after_delay, args=(tunnel_id, delay, status), daemon=True).start()`; this makes the recursive scheduling explicit and testable in isolation. |

#### `src/core/update_downloader.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 204 | 매직값 | download_installer() hardcodes timeout=30 instead of using the class's configurable timeout | Either reuse `self.timeout` for the download request too, or — if a genuinely different timeout is intended for streaming downloads — add an explicit named class constant (e.g. `DOWNLOAD_CONNECT_TIMEOUT = 30`) with a comment explaining why it differs from the API timeout, instead of an unexplained bare literal. |

#### `src/exporters/rust_dump_exporter.py` (4건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 189 | 중복 | ForeignKeyResolver rebuilds the same orphan JOIN/WHERE SQL fragment 4 times | Extract `_orphan_join_where(schema, table, column, ref_table, ref_column) -> str` returning just the `FROM ... LEFT JOIN ... WHERE ...` fragment (as a private method on ForeignKeyResolver), and have `generate_orphan_query`, the count/sample queries inside `find_orphan_records`, and `get_all_orphan_queries` all build their SELECT clause around that one shared fragment. |
| 330 | 중복 | RustDumpExporter and RustDumpImporter duplicate __init__ and _endpoint verbatim | Introduce `class _RustDumpClientBase: def __init__(self, config, facade=None): self.config = config; self.facade = facade if facade is not None else DbCoreFacade(); self._owns_facade = facade is None` plus a shared `_endpoint(self, schema)`, and have RustDumpExporter/RustDumpImporter inherit from it instead of repeating both methods. |
| 629 | 파라미터과다 | Callback-parameter soup: 13 params with 7 separate Optional[Callable] slots | Bundle the callbacks into `@dataclass class DumpEventCallbacks: progress: Optional[Callable]=None; table_progress: Optional[Callable]=None; detail: Optional[Callable]=None; table_status: Optional[Callable]=None; raw_output: Optional[Callable]=None; metadata: Optional[Callable]=None; table_chunk_progress: Optional[Callable]=None`, and pass one `callbacks: Optional[DumpEventCallbacks] = None` through `_run_rust_dump`, `export_full_schema`, `export_tables`, and `import_dump` instead of 5-7 individual optional callables per signature. |
| 764 | 거대함수 | emit_core_event mixes high-level event dispatch with low-level per-event-type arithmetic | Extract one private handler per event type (`_handle_dump_plan_event`, `_handle_dump_schedule_event`, `_handle_phase_event`, `_handle_table_progress_event`, `_handle_row_progress_event`) each taking the raw event dict plus the relevant callbacks, and have `emit_core_event` become a plain `{event_type: handler}` dispatch table so each handler (including the percent/rows-sec math) can be read and tested independently. |

#### `src/ui/dialogs/cross_engine_migration_dialog.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 805 | 중복 | Next/Previous button enable-state logic is re-derived identically in three separate methods | Delete the redundant inline block in `_show_step` (lines 844-849) since the trailing `self._refresh_navigation_state()` call on line 850 already does the same work. Replace lines 1217-1220 in `_set_running` with a single trailing call to `self._refresh_navigation_state()` instead of a third copy of the condition. |
| 990 | 거대함수 | _on_result is a 57-line dispatcher handling 7 unrelated command types inline | Introduce a per-command handler map, e.g. `handlers = {'plan': self._handle_plan_result, 'verify': self._handle_verify_result, 'preflight': self._handle_preflight_result, 'migrate': self._handle_migrate_result, 'readiness': self._handle_readiness_result, 'guide': self._handle_guide_result}`, and dispatch via `handlers.get(payload.get('command'), self._handle_generic_result)(payload)`, moving each branch body (990-1046) into its own small method. |

#### `src/ui/dialogs/cross_engine_migration_endpoint_form.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 43 | 매직값 | Postgres/MySQL defaults ("public", "postgres", 3306, 5432) are hard-coded in five+ separate spots with no shared constant | Add shared constants in src/core/cross_engine_migration.py: `DEFAULT_MYSQL_PORT = 3306`, `DEFAULT_POSTGRESQL_PORT = 5432`, `DEFAULT_POSTGRESQL_SCHEMA = "public"`, `DEFAULT_POSTGRESQL_DATABASE = "postgres"`, and import them from both cross_engine_migration_endpoint_form.py (lines 43, 174, 201, 210, 287, 289) and cross_engine_migration_dialog.py:1253. |
| 182 | 깊은중첩 | _apply_tunnel_data runs two only-loosely-parallel POSTGRESQL/default_schema condition chains that are easy to get out of sync | Extract a pure helper `_resolve_default_database_and_schema(engine, default_database, default_schema) -> tuple[str, str]` that computes both fields from one set of conditions in one place, then assign `input_database`/`input_schema` in `_apply_tunnel_data` from its two return values. |
| 290 | 파라미터과다 | make_connection_payload(...) is called with 7 positional primitive arguments | Call with explicit keyword arguments at the existing call site (lines 290-298): `make_connection_payload(engine=self.engine(), host=self.input_host.text().strip(), port=self.input_port.value(), user=self.input_user.text().strip(), password=self.input_password.text(), database=database, schema=schema)`, or have make_connection_payload (src/core/cross_engine_migration.py:238) accept a small ConnectionPayloadInput dataclass built once in this method. |

#### `src/ui/dialogs/db_connection_dialog.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 160 | 중복 | _on_tunnel_selected and on_mode_changed duplicate the 'apply tunnel data to form fields' logic, with inconsistent defensiveness | Extract a single `_apply_tunnel_data(self, tunnel_data: dict) -> None` method containing the guarded field-population logic (including the 'host'/'port' presence check), and call it from both _on_tunnel_selected and on_mode_changed. |
| 212 | 중복 | test_connection and do_connect duplicate field-reading, validation, connector-creation and error handling | Extract `_read_connection_fields() -> tuple` and `_build_connector_or_raise() -> connector`, and have test_connection/do_connect call these shared helpers, keeping only the success-path behavior distinct. |

#### `src/ui/dialogs/db_dialogs.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 9 | 기타 | db_dialogs.py imports ~15 names it never uses itself; some are load-bearing test re-exports, others are pure dead imports | Delete the 7 dead imports outright (cap_incomplete_export_percent, next_export_percent, export_overall_percent, format_export_row_labels, format_export_table_status, format_export_visible_telemetry, _build_orphan_queries_sql - verify with a grep before deleting). For the remaining genuinely re-exported names, add an explicit `__all__` plus a one-line comment ('re-exported for backward-compatible test imports'), or better, change tests/test_db_import_dialog.py and tests/test_db_orphan_dialog.py to import directly from db_import_dialog.py / db_orphan_dialog.py. |
| 98 | 중복 | RustDumpWizard.start_export/start_import/start_orphan_check repeat the same connector-acquisition block three times | Extract a private helper `_resolve_connector(self, need_connection_info: bool = False) -> tuple[connector/None, str/None]` encapsulating the preselected-tunnel vs. dialog branching, and have all three start_* methods call it. |

#### `src/ui/dialogs/db_export_dialog.py` (7건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 212 | 거대함수 | init_ui is a ~390-line method building the whole dialog in one block | Break into private builder methods returning widgets/groupboxes, e.g. `_build_status_group()`, `_build_export_type_group()`, `_build_schema_section()`, `_build_output_folder_group()`, `_build_progress_section()`, `_build_button_row()`, and have init_ui assemble them into the splitter/layout. |
| 604 | 중복 | Collapsible-config-panel logic (toggle/collapse/expand + splitter ratios) is duplicated verbatim in db_import_dialog.py | Extract a small mixin/base class (e.g. `CollapsibleConfigDialog`) providing these three methods plus the `splitter`/`config_container`/`btn_collapse` attribute contract, and have both RustDumpExportDialog and RustDumpImportDialog inherit/compose it. |
| 645 | 추상화수준혼재 | Directory-traversal-safe path construction is buried as nested closures inside a Qt dialog method | Move safe_component/safe_join into a standalone, importable function (e.g. `src/core/path_safety.py: safe_output_dir(base_dir, folder_name)`) with its own unit tests, and have the dialog call it with plain strings. |
| 877 | 거대함수 | do_export does validation, path generation, state reset, config building and worker wiring in one ~130-line method | Extract `_reset_export_state()`, `_resolve_output_dir(schema)`, and `_build_worker(schema, output_dir)` so do_export reads as a short sequence of named steps. |
| 972 | 중복 | RustDumpConfig construction from a connector, including magic fallback values, is duplicated with db_import_dialog.py | Add a factory function, e.g. `build_rust_dump_config(connector) -> RustDumpConfig` in rust_dump_exporter.py, using the existing DEFAULT_MYSQL_PORT/DEFAULT_LOCAL_HOST constants from src/core/constants.py (plus new named constants for the 'root'/'mysql' fallbacks), and call it from both dialogs. |
| 1186 | 중복 | Table-status icon map is redefined locally instead of being a shared constant | Hoist a single module-level `TABLE_STATUS_ICONS = {...}` constant (in a shared UI-constants module) and reuse it in all three locations instead of re-declaring the dict per method. |
| 1252 | 중복 | GitHub error-reporting boilerplate duplicated with db_import_dialog.py | Factor a shared helper, e.g. `report_error_to_github(dialog, error_type, message, context)` in `src/ui/workers/github_worker.py`, that both dialogs call, or a small `GithubReportingMixin` providing `_report_error_to_github`/`_on_github_report_finished`/`_github_workers`. |

#### `src/ui/dialogs/db_import_dialog.py` (5건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 267 | 거대함수 | init_ui is a ~400-line method building the whole import dialog in one block | Split into private builder methods (`_build_status_group()`, `_build_input_dir_group()`, `_build_upgrade_check_group()`, `_build_schema_group()`, `_build_timezone_group()`, `_build_import_mode_group()`, `_build_progress_section()`), mirroring the recommendation for db_export_dialog.py's init_ui. |
| 899 | 에러처리불일치 | Some exception handlers silently swallow failures while sibling methods surface them to the user | At minimum log the swallowed exception (e.g. `logger.debug(...)`) before returning the fallback value in check_timezone_support and _get_dump_schema_name, so silent failures are diagnosable; consider narrowing the `except Exception` to the specific expected exception types where feasible. |
| 946 | 추상화수준혼재 | do_import embeds raw timezone SQL strings and production-guard business rules directly inside a Qt dialog method | Extract `_resolve_timezone_sql(engine: str, mode: str) -> Optional[str]` as a standalone, testable function (module-level or in a `timezone_policy.py`), and extract `_confirm_production_guard(input_dir, target_schema) -> bool` so do_import reads as: validate -> confirm production -> resolve timezone -> build config -> start worker. |
| 1216 | 중복 | Byte-to-MB/GB size formatting is copy-pasted three times in the same file | Extract a helper `_format_bytes(size_bytes: int) -> str` and call it from all three sites (and from db_export_dialog.py if it ever needs the same formatting). |
| 1382 | 중복 | Failed/done-table counting over self.import_results is reimplemented five times with inconsistent filtering | Add one helper, e.g. `_table_results(self) -> dict[str, dict]` returning import_results with 'fk_restore' excluded and non-dict values filtered out, plus `_count_by_status(results, status)`, and use them consistently at all five call sites. |

#### `src/ui/dialogs/diff_dialog.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 38 | 갓클래스 | SchemaDiffDialog owns UI layout, raw DB-connector lifecycle, thread orchestration, and result-tree rendering all in one class | Extract a small SchemaCompareSession helper (e.g. in a new src/ui/dialogs/diff_dialog_session.py) that owns _source_connector/_target_connector creation, teardown, and the 'clear stale connectors before a new compare' logic currently duplicated at lines 337-348, 378-389, and 746-759, so SchemaDiffDialog only calls session.start_compare(...) / session.close() and focuses on presentation. |
| 337 | 중복 | Source/target connector disconnect-and-clear block is copy-pasted 3 times | Extract a single `_disconnect_connectors(self)` method (clearing both `_source_connector` and `_target_connector` with try/except/None-out) and call it from all three sites: the `_start_compare` pre-cleanup (337-348), its exception handler (378-389), and `closeEvent` (746-759). |
| 536 | 중복 | Column/index/FK diff-rendering loops in _display_results are near-identical copy-paste blocks | Extract one helper, e.g. `_add_diff_child_item(self, parent_item, kind_prefix, diff, name_attr)` called from all three loops (536-549, 552-571, 574-593), so a new diff kind or a label/severity formatting change only needs to be made in one place. |

#### `src/ui/dialogs/fix_wizard_charset_page.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 378 | 중복 | nextId() branches on has_charset_issues() but both branches resolve identically based only on has_other_issues() | Collapse to: `return self.wizard_dialog.option_page_id if self.wizard_dialog.has_other_issues() else self.wizard_dialog.preview_page_id`, removing the redundant `has_charset_issues()` branch entirely (or add a comment explaining why it's intentionally kept if some future differentiation is planned). |

#### `src/ui/dialogs/fix_wizard_execution_page.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 207 | 중복 | Duck-typed 'CombinedExecutionResult vs BatchExecutionResult' branch is duplicated between ExecutionPage and PreviewPage | Give both result classes a common method/property, e.g. `summary() -> ExecutionSummary(total, success, fail, affected_rows)`, so both wizard pages call `result.summary()` instead of each re-implementing the `hasattr` branch and field extraction. |
| 254 | 네이밍 | Local variable rollback_dir shadows the imported rollback_dir() function | Rename the local variable, e.g. `rollback_dir_path = self._get_rollback_dir()`, to avoid colliding with the imported `rollback_dir` function name. |

#### `src/ui/dialogs/fix_wizard_option_page.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 191 | 오래된주석 | FixOptionPage docstring advertises FK-tree visualization and auto-include features that the code explicitly says were removed | Update the class docstring to drop the 3 stale bullet points (FK Tree visualization, FK auto-include, auto-included-table skip navigation) and describe only what the page currently does (per-issue option selection + batch-apply dialog), to avoid a future reader believing those features still exist. |

#### `src/ui/dialogs/fix_wizard_preview_page.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 92 | SRP위반 | generate_sql_preview() performs SQL templating/deduplication business logic inside a QWizardPage | Move the placeholder substitution (`sql.replace('{custom_date}', ...)`) and the dedup-by-hash logic into a `FixWizardStep`/`migration_fix_wizard` helper, e.g. `step.rendered_sql()` and `render_all_steps_sql(steps)`, so the wizard page only formats/concatenates already-rendered strings for display. |

#### `src/ui/dialogs/migration_dialogs.py` (6건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 224 | 거대함수 | init_ui() is a ~180-line function building two full option rows, three buttons with inline stylesheets, and the tab widget | Split init_ui into smaller helpers mirroring the existing tab-init pattern, e.g. `_build_schema_row()`, `_build_basic_check_options()`, `_build_upgrade_checker_options()`, `_build_action_buttons()`, each returning a layout/widget to add to the top-level layout. |
| 303 | 중복 | Inline QPushButton stylesheet blocks are copy-pasted 7 times in this file (and again per-file in the rest of the fix-wizard/oneclick dialogs) | Extract a helper, e.g. `make_action_button(text, bg, hover, disabled='#bdc3c7') -> QPushButton` in a shared ui/styles.py or ui/widgets.py, and use it at every one of these call sites (treat btn_close as a separate `make_secondary_button` variant since it lacks bold/disabled styling). This turns ~10 seven/eight-line QSS blocks into one-line calls and removes an entire class of copy/paste color-typo risk. |
| 714 | 파라미터과다 | MigrationAnalyzerWorker is constructed with 16 individual keyword arguments, 14 of them booleans | Introduce an `AnalysisOptions` dataclass (or a dict built once from the checkbox states) with named fields, and pass a single `options=AnalysisOptions(...)` argument to the worker. This also makes it obvious which checks are user-configurable vs always-on. |
| 924 | 중복 | FK-tree traversal (root-finding, cycle detection, rendered-set bookkeeping) is implemented twice for two output formats | Extract a single generator, e.g. `iter_fk_tree(fk_tree) -> Iterator[tuple[table, depth, is_cycle, is_last]]`, that performs the root-finding/cycle-detection/rendered-tracking once, and have both `_format_fk_tree_text` and `update_fk_tree` consume it to build their respective output (text lines vs QTreeWidgetItem). |
| 981 | SRP위반 | Dialog directly builds raw SQL text for orphan-record SELECT queries instead of delegating to the analyzer/core module | Add a `MigrationAnalyzer.generate_orphan_select_sql(orphan, schema)` (or equivalent Rust-core call) alongside the existing `generate_cleanup_sql`, and have the dialog call that instead of building SQL text itself. Keeps all SQL-shape knowledge in one module. |
| 1159 | 중복 | AUTO_FIXABLE_TYPES set is verbatim duplicated in a second file | Move the set to a single shared location (e.g. `MigrationAnalyzer.AUTO_FIXABLE_ISSUE_TYPES` or a constant in src/core/migration_constants.py) and have both migration_dialogs.py and fix_wizard_issue_selection_page.py import it instead of redefining it. |

#### `src/ui/dialogs/schedule_dialog.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 132 | SRP위반 | Dangerous-SQL-pattern detection (DROP/TRUNCATE/DELETE-without-WHERE/UPDATE-without-WHERE) is embedded as regex logic directly in the dialog class | Move DANGER_PATTERNS and the per-statement danger-checking logic into a small src/core module (e.g. src/core/sql_safety.py) with a pure function `find_dangerous_sql_warnings(sql_text: str) -> list[str]`; have the dialog just call it and update the warning label. |
| 610 | 거대함수 | _save() mixes validation branching for two task types with a ~20-argument ScheduleConfig construction in one 110-line method | Split into _validate_and_build_sql_task() and _validate_and_build_backup_task(), each returning a small struct of just the fields relevant to that task type, then assemble the common ScheduleConfig fields once in _save(). Consider grouping SQL-only and backup-only fields into nested sub-dataclasses on ScheduleConfig itself to reduce the flat parameter count. |

#### `src/ui/dialogs/settings.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 5 | 죽은코드 | dataclass, Optional, QCursor, QThread, pyqtSignal, and UpdatePackageActionText are imported but never used | Remove the 5 unused imports and collapse the stray blank block (lines 34-41) to a single blank line; add flake8/ruff F401 to CI so future extractions don't leave orphaned imports. |
| 91 | 거대함수 | _create_general_tab builds 7 independent setting groups inline in one 267-line method | Extract one private builder per QGroupBox (_build_language_group(), _build_close_behavior_group(), _build_theme_group(), _build_github_group(), _build_backup_group(), _build_reconnect_group(), _build_startup_group()), each returning a widget; have _create_general_tab just assemble the returned widgets into the tab layout. |
| 948 | SRP위반 | _launch_installer reaches directly into main_window.engine.active_tunnels/tunnel_configs instead of using an accessor | Add a small accessor on MainWindow (e.g. get_active_tunnel_names() -> list[str]) and have SettingsDialog call that instead of reaching into engine.active_tunnels/tunnel_configs directly. |

#### `src/ui/dialogs/sql_editor_dialog.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 233 | 거대함수 | init_ui is a single 355-line method building the entire dialog layout inline | Break init_ui into per-region builder methods (`_build_connection_bar`, `_build_toolbar`, `_build_editor_panel`, `_build_result_panel`, `_build_transaction_panel`, `_build_status_bar`) that each return a widget/layout, and keep init_ui as a short method that assembles them. Move the repeated QSS blocks into module-level constants (e.g. `PRIMARY_BUTTON_QSS`, `TX_PANEL_QSS`) to shrink the method further. |
| 1508 | 거대함수 | _do_commit mixes guard-checks, per-schema danger confirmation, transactional SQL execution, error rollback, and history bookkeeping in one method | Extract the schema-grouping + ProductionGuard confirmation loop into `_confirm_cell_edit_commit(table_edits) -> bool`, and the try/except commit body into `_apply_commit(table_edits) -> CommitResult` (success/failed-rows), leaving _do_commit as: guard checks -> confirm -> apply -> report. |

#### `src/ui/dialogs/sql_editor_workers.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 33 | 파라미터과다 | SQLQueryWorker.__init__ takes 8 primitive connection parameters instead of a connection-info object | Introduce a small `@dataclass ConnectionParams(engine, host, port, user, password, database=None, schema=None)` and have create_sql_editor_connector, SQLQueryWorker, SQLTransactionExecutionWorker, and SQLEditorDialog._create_db_connector all accept/pass a single ConnectionParams instance instead of the individual fields. |
| 108 | 중복 | Cursor-to-row-list conversion logic is duplicated verbatim between SQLQueryWorker and SQLTransactionExecutionWorker | Extract a module-level helper `_rows_from_cursor(cursor) -> tuple[list[str], list[list]]` in sql_editor_workers.py and call it from both worker classes. |

#### `src/ui/dialogs/test_dialogs.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 186 | SRP위반 | refresh_databases() runs DB connector business logic synchronously inside the dialog, unlike execute_sql() which delegates to a worker | Move the schema-listing call behind a small worker/thread (mirroring SQLExecutionWorker) or a src/core helper invoked asynchronously, so all DB access in this dialog goes through one consistent path. |
| 211 | 에러처리불일치 | Broad except-Exception blocks only surface str(e) in the dialog's transient text box, with no logger call | Add `from src.core.logger import get_logger` and `logger = get_logger(__name__)` at module scope (consistent with the rest of the dialogs package) and call logger.exception(...) alongside the existing UI message in both except blocks. |

#### `src/ui/dialogs/tunnel_config.py` (3건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 81 | 거대함수 | TunnelConfigDialog.init_ui builds all 7 form sections in one ~210-line method | Extract one builder per visually-marked section (e.g. _build_bastion_section(form_layout), _build_target_db_section(form_layout), _build_auth_section(form_layout)), mirroring the existing header labels. |
| 200 | 매직값 | Environment combo box relies on two hand-maintained, position-coupled index<->string dicts instead of QComboBox item data | Rebuild combo_environment with addItem(label, value) pairs (value=None/'production'/'staging'/'development') and read/write via findData()/currentData(), exactly like combo_db_engine — this removes both magic dicts entirely. |
| 340 | 에러처리불일치 | _available_tunnels silently swallows any exception from config_mgr.load_config() and returns [] | Log the exception (logger.exception("failed to load tunnel list for bastion templates")) before returning [], using the same get_logger pattern already established in schedule_dialog.py and tunnel_status_dialog.py (add `from src.core.logger import get_logger` and `logger = get_logger(__name__)` at module scope in tunnel_config.py, which currently has neither). |

#### `src/ui/dialogs/tunnel_status_dialog.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 216 | 죽은코드 | type_colors dict is computed and stored in _color but the value is never used (suppressed with # noqa: F841) | Either wire type_colors into type_item.setForeground(QColor(type_colors.get(event.event_type, "#000000"))) now, or delete the dead dict, the _color variable, and the noqa suppression until the feature is actually implemented. |

#### `src/ui/main_window.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 383 | 중복 | Identical DB-credential-missing guard block repeated 6 times (not 5) | Extract a `_require_db_credentials(self, tunnel) -> tuple[str, str] / None` helper that shows the warning and returns None on failure; call it from all six sites (including `_test_direct_connection`) instead of repeating the check. |
| 680 | 중복 | Five near-identical 'build RustDumpWizard then call one action' blocks | Add one private helper, e.g. `_launch_rust_dump_wizard(self, action: str, tunnel: dict / None = None)`, that builds the wizard once and dispatches via `getattr(wizard, action)()`, and call it from all five entry points. |

#### `src/ui/widgets/tunnel_tree.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 297 | 거대함수 | _show_context_menu builds two unrelated menus in one method | Split into `_build_group_context_menu(menu, group_id)` and `_build_tunnel_context_menu(menu, tunnel_data)`, leaving `_show_context_menu` as a thin dispatcher that resolves `item_type` and delegates. |
| 404 | SRP위반 | Widget bypasses its own signal pattern to reach config_mgr via Qt parent-chain walk | Add a `group_collapsed_changed(group_id: str, collapsed: bool)` signal, emit it from `_on_item_expanded`/`_on_item_collapsed` instead of calling `_save_collapsed_state` directly, and have `TunnelManagerUI` (which already owns `config_mgr`) persist it in `_connect_tree_signals`. |

#### `src/ui/workers/fix_wizard_worker.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 74 | 죽은코드 | FixWizardWorker repeats the same vestigial dry_run pattern as CleanupWorker | Apply the same fix as `CleanupWorker` (drop the vestigial `dry_run` parameter) in both files together, since they now share the exact same guard-then-ignore shape and should be cleaned up consistently. |

#### `src/ui/workers/migration_worker.py` (1건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 89 | 죽은코드 | CleanupWorker's dry_run parameter is accepted, validated, then ignored | Since real execution is permanently disabled per the Rust Core migration, drop the `dry_run` parameter entirely (constructor takes no such argument) and document/rename the class as preview-only, rather than keeping a parameter whose only effect is asserting its own value. |

#### `src/ui/workers/rust_dump_worker.py` (2건)
| 위치 | 카테고리 | 제목 | 권고 |
|---|---|---|---|
| 58 | 거대함수 | run() inlines three structurally different task branches | Split into `_run_export_schema()`, `_run_export_tables()`, and `_run_import()` private methods; keep `run()` as a short dispatcher on `self.task_type` plus the shared cancel/exception handling in the try/except/finally. |
| 70 | 중복 | Identical progress-callback closures redefined in all three run() branches | Hoist these as bound methods on the class (e.g. `self._on_detail`, `self._on_table_status`, `self._on_raw_output`, `self._on_metadata`, `self._on_table_chunk_progress`), defined once, and pass them by reference from every branch instead of re-declaring closures. |

---

## LOW 심각도 (81건) — 목록

사소한 지적. 여유 있을 때 정리 대상.

#### `migration_core/src/lib.rs` (9건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 1568 | 파라미터과다 | dump_import_row_progress_event's 12-parameter, same-typed-Option signature invites argument-reuse mistakes |
| 1640 | 매직값 | Dump/import thread-count default '8' is a bare repeated literal instead of a named constant |
| 4001 | 매직값 | Bare MySQL error-code and session-timeout literals embedded directly in strings instead of named constants |
| 4783 | 중복 | Identical ad-hoc {"event": "error", "request_id": ..., "message": err} JSON literal repeated across at least 6 functions |
| 7026 | 기타 | One-click fix payload shape is chosen via a raw string-literal comparison instead of a typed variant |
| 8336 | 중복 | migration_error_result requires a full &NormalizedTable just to read its .name, forcing 2 dummy-table fabrications in migrate_with_adapters_reporting |
| 9373 | 매직값 | Unexplained 4_096 byte threshold in learned_mysql_range_chunk_size |
| 10859 | 중복 | sql_literal and mysql_sql_literal duplicate four of five match arms |
| 11104 | 매직값 | Unexplained length bounds (64, 512) in security-critical identifier/type validators |

#### `src/core/config_manager.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 63 | 매직값 | Windows FILE_ATTRIBUTE_HIDDEN flag is an unexplained literal 0x02 |

#### `src/core/cross_engine_migration.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 285 | 네이밍 | Local variable `issues` is reused for two semantically different lists in the same function |
| 300 | 매직값 | Mismatch-list truncation limit "50" hardcoded twice |

#### `src/core/db_core_service.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 546 | 중복 | DbEndpoint is manually reconstructed field-by-field instead of using dataclasses.replace |
| 736 | 죽은코드 | RustDbCursor.execute branches on a facade capability that every real facade in this codebase has, and its fallback references a non-existent attribute |

#### `src/core/github_issue_reporter.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 101 | 매직값 | Several unrelated string-truncation lengths (80, 100, 1000, 2000) are scattered as bare literals |

#### `src/core/i18n.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 1 | 오래된주석 | Module docstring 'Small runtime i18n layer' no longer describes the file |
| 1347 | 매직값 | Hangul Unicode block boundaries are unexplained literals duplicated in two places |

#### `src/core/logger.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 173 | 죽은코드 | Unreachable ERROR branch in filter_log_by_level |

#### `src/core/migration_analyzer.py` (5건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 13 | 죽은코드 | Unused `Path` import and a redundant unused local `datetime` import |
| 266 | 매직값 | Two different unexplained 'large table' thresholds (500000 vs 100000) control related decisions |
| 585 | 죽은코드 | execute_cleanup has a dead `if dry_run:` guard left over from the Rust-core migration, and the docstring never mentions the new raise |
| 753 | 매직값 | Hardcoded '[N/15]' step counter repeated literally 15 times |
| 1280 | 오래된주석 | Leftover section-header comment for a 'dump file analyzer' that no longer lives in this file |

#### `src/core/migration_constants.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 302 | 기타 | Type annotation uses builtin `any` instead of `typing.Any` |

#### `src/core/migration_dump_analyzer.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 249 | 추상화수준혼재 | Two regex patterns are defined ad-hoc inline instead of following the codebase's convention of centralizing patterns in migration_constants.py |

#### `src/core/migration_fix_generator.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 182 | 기타 | Redundant local `import re` shadowing the existing module-level import |

#### `src/core/migration_fix_wizard.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 166 | 중복 | Invalid-date WHERE clause duplicated verbatim three times in one function |
| 1196 | 죽은코드 | _execute_single is an intentional fail-closed guard, not vestigial dead code as originally claimed — but rollback_sql is genuinely a dead always-empty literal |

#### `src/core/migration_rollback_sql_generator.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 371 | 죽은코드 | FixWizardStep.included_by is never assigned anywhere, making its skip-check permanently unreachable — but its would-be duplicate-rollback scenario is already covered by an independent table-level dedup mechanism |

#### `src/core/migration_rules/data_rules.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 22 | 죽은코드 | Unused import: `from dataclasses import dataclass` |

#### `src/core/migration_rules/schema_rules.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 226 | 매직값 | Hardcoded '16' byte estimate for DECIMAL columns lacks a named constant |

#### `src/core/mysql_login_path.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 104 | 매직값 | AES block size 128 repeated as a bare literal in both padder and unpadder construction |
| 222 | 오래된주석 | is_available()'s docstring claims the result is always True, contradicting its own except-branch |

#### `src/core/oneclick_log.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 13 | 죽은코드 | Unused import: typing.Optional |

#### `src/core/production_guard.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 20 | 죽은코드 | Unused import QFont |
| 124 | 매직값 | Hardcoded hex color literals scattered outside the ENV_COLORS lookup table |

#### `src/core/scheduler.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 23 | 죽은코드 | Unused import DEFAULT_MYSQL_PORT |

#### `src/core/schema_severity_classifier.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 106 | 죽은코드 | _classify_type_change accepts an unused diff_text parameter |
| 174 | 매직값 | Severity-order lookup table is rebuilt on every call instead of being a class constant like the sibling _INTEGER_TYPES |

#### `src/core/single_instance.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 86 | 매직값 | Unnamed timeout/poll-interval literals in notify_existing_instance |

#### `src/core/sql_history.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 97 | 중복 | The 'match entry by id-or-timestamp' fallback lookup is re-implemented three times |
| 204 | 파라미터과다 | SQLHistory.search_advanced takes 7 filter parameters |

#### `src/core/sql_statement_parser.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 35 | 거대함수 | parse_sql_statement_ranges is a single ~135-line state machine handling six distinct scanning rules inline |

#### `src/core/sql_validator.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 293 | 매직값 | Fuzzy-match cutoff 0.5 is repeated as an unnamed literal |

#### `src/core/tunnel_engine.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 77 | 네이밍 | Parameter/dict key 'tid' is abbreviated inconsistently with 'tunnel_id' used elsewhere in the codebase |

#### `src/core/tunnel_monitor.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 537 | 매직값 | Reconnect backoff schedule is an inline unexplained literal list |

#### `src/exporters/rust_dump_exporter.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 20 | 매직값 | threads=8 default repeated 7 times with no named constant, unlike the sibling compression default |

#### `src/ui/dialogs/cross_engine_migration_dialog.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 363 | 네이밍 | btn_run_plan/btn_run_verify are aliases of btn_plan/btn_verify, giving the same widgets two names |
| 1124 | 매직값 | Three different hard-coded thread-wait timeouts (5000/3000/1000 ms) with no shared constant or documented rationale |

#### `src/ui/dialogs/cross_engine_migration_endpoint_form.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 219 | 죽은코드 | _detect_engine(self, host, port, config) never uses its host/port parameters |

#### `src/ui/dialogs/db_export_dialog.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 642 | 중복 | Redundant local re-imports of os/datetime/Path that shadow already-imported module-level names |
| 1238 | 매직값 | Log-line cap of 500 hard-coded four times across two files with no named constant |

#### `src/ui/dialogs/db_import_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 1199 | 죽은코드 | status_colors dict and _color variable are computed but never used |

#### `src/ui/dialogs/diff_dialog.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 7 | 죽은코드 | math, random, and QApplication are imported but never used in this file |
| 677 | 기타 | Diff-kind detection via hasattr() chains instead of isinstance/an explicit type tag |

#### `src/ui/dialogs/fix_wizard_charset_page.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 186 | 중복 | '원본 이슈' / 'FK 연관' tag labels duplicate an identical stylesheet block save for one color |

#### `src/ui/dialogs/fix_wizard_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 20 | 죽은코드 | FixWizardWorker, CharsetTableInfo and BatchOptionDialog are imported but never referenced |

#### `src/ui/dialogs/fix_wizard_execution_page.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 267 | 기타 | _rollback_sql_content is never declared in __init__ and is only discoverable via scattered hasattr checks |

#### `src/ui/dialogs/fix_wizard_option_page.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 498 | 죽은코드 | isComplete() always returns True — the conditional branch is pointless |

#### `src/ui/dialogs/group_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 29 | 매직값 | Default group color literal '#3498db' is duplicated inline in the same expression |

#### `src/ui/dialogs/migration_dialogs.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 10 | 죽은코드 | Several imports are unused (re, html, QFont, QLineEdit, QSpinBox, QListWidget, QListWidgetItem, QMenu, QSplitter) |
| 821 | 매직값 | Orphan-count color thresholds (1000, 100) are unexplained magic numbers |

#### `src/ui/dialogs/oneclick_migration_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 11 | 죽은코드 | QScrollArea and QColor are imported but never used |

#### `src/ui/dialogs/schedule_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 511 | 중복 | _browse_result_output_dir and _browse_output_dir are near-identical directory-picker methods |

#### `src/ui/dialogs/sql_editor_code_editor.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 403 | 중복 | SQLEditorTab.set_content() and load_file() duplicate the same 'apply text to editor' sequence |

#### `src/ui/dialogs/sql_editor_dialog.py` (4건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 72 | 오래된주석 | Leftover empty section-divider comments still reference classes that were extracted to other files |
| 992 | 매직값 | SQL preview truncation length is a different unexplained magic number at every call site |
| 1211 | 매직값 | Result-table column width cap (400), row height (28), and elapsed-timer interval (100ms) are unnamed literals |
| 1588 | 중복 | Pending-change summary ('쿼리 N건, 셀 편집 N건') is built identically in _do_commit and _do_rollback |

#### `src/ui/dialogs/sql_editor_highlighters.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 152 | 기타 | IssueSeverity is imported inside the per-issue for-loop instead of at module scope |

#### `src/ui/dialogs/sql_editor_history_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 218 | 중복 | load_history() and _do_search() duplicate the list-reset sequence |

#### `src/ui/dialogs/tunnel_config.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 457 | 중복 | _test_db_only and _test_integrated repeat the same encryptor-lookup + _TempCredentials + dialog.exec() sequence |

#### `src/ui/dialogs/tunnel_status_dialog.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 111 | 중복 | Max-reconnect-attempts QSpinBox (range 1-20, default 5) is built almost identically here and in settings.py |

#### `src/ui/main_window.py` (3건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 337 | 추상화수준혼재 | refresh_table mixes an extracted helper with inlined widget construction at the same level |
| 932 | 중복 | Success/failure tray-notification branching duplicated across two methods |
| 1062 | 매직값 | Column count hardcoded as literal 7 instead of derived |

#### `src/ui/workers/cross_engine_migration_worker.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 103 | 거대함수 | run() mixes process-lifecycle plumbing with protocol-event dispatch |

#### `src/ui/workers/migration_worker.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 5 | 죽은코드 | Three of four imported symbols are never used |

#### `src/ui/workers/test_worker.py` (1건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 262 | 네이밍 | SQLExecutionWorker (arbitrary SQL-file execution) lives in a module named for connection testing |

#### `src/ui/workers/validation_worker.py` (2건)
| 위치 | 카테고리 | 제목 |
|---|---|---|
| 7 | 죽은코드 | Unused typing import |
| 48 | 중복 | Identical cancel()/_cancelled scaffolding repeated across three worker classes |

---

## 조사 그룹별 원시 발견 건수

| 그룹 | 종류 | 발견 건수 |
|---|---|---|
| core-db-sql | py | 18 |
| core-tunnel-infra | py | 23 |
| core-i18n-logging-update | py | 9 |
| core-migration-analysis | py | 15 |
| core-migration-fix | py | 17 |
| core-migration-rules | py | 14 |
| core-schema-export | py | 16 |
| ui-sql-editor | py | 12 |
| ui-db-dialogs | py | 21 |
| ui-migration-dialogs | py | 20 |
| ui-cross-engine-diff | py | 14 |
| ui-settings-schedule-tunnel | py | 19 |
| ui-shell-workers-misc | py | 23 |
| rust-lib-1 | rust | 8 |
| rust-lib-2 | rust | 8 |
| rust-lib-3 | rust | 7 |
| rust-lib-4 | rust | 8 |
| rust-lib-5 | rust | 2 |
| rust-structure-overview | rust-overview | 1 |
