use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::Instant;

use crate::*;
use mysql::{prelude::Queryable, LocalInfileHandler};

/// MySQL `LOAD DATA LOCAL`이 비활성화됐을 때 반환되는 에러 코드(ERROR 3948) 매칭 토큰.
const MYSQL_ERR_LOCAL_INFILE_DISABLED: &str = "3948";
/// import 세션 튜닝에서 상향하는 net_read/net_write 타임아웃(초).
const MYSQL_IMPORT_NET_TIMEOUT_SECS: u32 = 600;
/// import 세션 튜닝에서 상향하는 wait_timeout(초).
const MYSQL_IMPORT_WAIT_TIMEOUT_SECS: u32 = 28800;

#[derive(Debug)]
enum DumpImportError {
    Domain(String),
    Emit(ProtocolEmitError),
}

impl From<String> for DumpImportError {
    fn from(value: String) -> Self {
        Self::Domain(value)
    }
}

impl From<ProtocolEmitError> for DumpImportError {
    fn from(value: ProtocolEmitError) -> Self {
        Self::Emit(value)
    }
}

type DumpImportResult<T> = Result<T, DumpImportError>;

fn emit_import_event<F, R>(
    emit: &mut F,
    event: Value,
    side_effect_started: bool,
) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    emit(event).into_protocol_emit_result().map_err(|error| {
        if side_effect_started {
            error.after_side_effect()
        } else {
            error
        }
    })
}

pub(crate) fn dump_import_streaming<F, R>(request: &Request, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut side_effect_started = false;
    emit_import_event(
        &mut emit,
        json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump_import",
        "message": "dump import started"
        }),
        side_effect_started,
    )?;

    match dump_import(request, &mut emit, &mut side_effect_started) {
        Ok(result) => emit_import_event(&mut emit, result, side_effect_started),
        Err(DumpImportError::Emit(error)) => Err(error),
        Err(DumpImportError::Domain(err)) => emit_import_event(
            &mut emit,
            json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
            }),
            side_effect_started,
        ),
    }
}

fn dump_import<F, R>(
    request: &Request,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<Value>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let endpoint = request_endpoint(request)?;
    let input_dir = request
        .payload
        .get("input_dir")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "dump.import requires input_dir".to_string())?;
    let mode = request
        .payload
        .get("mode")
        .or_else(|| request.payload.get("import_mode"))
        .and_then(Value::as_str)
        .unwrap_or("replace");
    if !matches!(mode, "replace" | "merge" | "recreate") {
        return Err(format!("unsupported dump import mode: {mode}").into());
    }

    let input_path = Path::new(input_dir);
    let manifest = read_dump_manifest(input_path)?;
    if manifest.format != "tunnelforge-dump" || !matches!(manifest.format_version, 1 | 2) {
        return Err("unsupported dump manifest format".to_string().into());
    }
    let data_format = manifest.data_format.to_ascii_lowercase();
    if !matches!(data_format.as_str(), "jsonl" | "tsv") {
        return Err(format!("unsupported dump data_format: {data_format}").into());
    }
    let compression = manifest.compression.to_ascii_lowercase();
    if !matches!(compression.as_str(), "none" | "zstd") {
        return Err(format!("unsupported dump compression: {compression}").into());
    }

    let selected_tables = string_list(request.payload.get("tables"));
    let selected: BTreeSet<String> = selected_tables.into_iter().collect();
    let threads = request
        .payload
        .get("threads")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or(DEFAULT_DUMP_THREADS)
        .max(1);
    let mysql_local_infile_policy = mysql_local_infile_policy_from_payload(&request.payload)?;
    let timezone_sql =
        validated_timezone_sql(request.payload.get("timezone_sql").and_then(Value::as_str))?;
    let tables: Vec<DumpTableManifest> = manifest
        .tables
        .iter()
        .filter(|table| selected.is_empty() || selected.contains(&table.name))
        .cloned()
        .collect();
    if tables.is_empty() {
        return Err("dump.import found no tables to import".to_string().into());
    }

    let strict_manifest = request
        .payload
        .get("strict_manifest")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let manifest_warnings = validate_dump_import_manifest_strictness(&tables, strict_manifest)?;
    let tables = dependency_ordered_dump_tables(&manifest.schema, tables);
    validate_dump_manifest_chunks(input_path, &tables, &data_format, &compression)?;
    for warning in &manifest_warnings {
        emit_import_event(
            emit,
            json!({
            "event": "warning",
            "request_id": request.request_id,
            "phase": "dump_import_manifest",
            "classification": "legacy_dump",
            "message": warning
            }),
            *side_effect_started,
        )?;
    }
    let mut adapter = LiveAdapter::connect(&endpoint)?;
    if let Some(sql) = timezone_sql.as_deref() {
        *side_effect_started = true;
        adapter.execute_sql(sql)?;
    }
    let local_infile_restore = prepare_mysql_local_infile_policy(
        &mut adapter,
        &endpoint,
        mysql_local_infile_policy,
        request.request_id.clone(),
        emit,
        side_effect_started,
    )?;
    let table_total = tables.len();
    let overall_rows_total = tables.iter().map(|table| table.rows).sum::<u64>();
    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;
    let mut imported_rows_by_table: BTreeMap<String, u64> = BTreeMap::new();

    *side_effect_started = true;
    set_mysql_import_session_tuning(&mut adapter, false)?;

    let target_schema = endpoint_schema(&endpoint);
    prepare_import_target(
        mode,
        &tables,
        &mut adapter,
        &target_schema,
        side_effect_started,
    )?;

    let import_result = (|| -> DumpImportResult<()> {
        for (index, table_manifest) in tables.iter().enumerate() {
            let table = manifest
                .schema
                .tables
                .iter()
                .find(|table| table.name == table_manifest.name)
                .ok_or_else(|| format!("manifest schema missing table {}", table_manifest.name))?;
            emit_import_event(
                emit,
                json!({
                "event": "table_progress",
                "request_id": request.request_id,
                "table": table.name,
                "status": "importing",
                "current": index + 1,
                "total": table_total
                }),
                *side_effect_started,
            )?;
            // replace/recreate의 DROP은 루프 진입 전에 일괄(자식 우선)로 끝냈다.
            // 여기서는 생성과 적재만 수행한다.
            let ddl = generate_table_ddl(table, &manifest.source_engine, adapter.engine())
                .ok_or_else(|| format!("cannot generate DDL for table {}", table.name))?;
            *side_effect_started = true;
            adapter
                .create_table(table, &ddl)
                .map_err(|err| dump_import_ddl_error("create_table", &table.name, &err))?;

            let (table_rows, table_chunks) = import_table_rows(
                &endpoint,
                &mut adapter,
                input_path,
                table,
                table_manifest,
                &data_format,
                &compression,
                threads,
                request.request_id.clone(),
                rows_imported,
                overall_rows_total,
                emit,
                side_effect_started,
            )?;
            rows_imported += table_rows;
            chunks_imported += table_chunks;
            imported_rows_by_table.insert(table.name.clone(), table_rows);
            emit_import_event(
                emit,
                json!({
                "event": "table_progress",
                "request_id": request.request_id,
                "table": table.name,
                "status": "completed",
                "current": index + 1,
                "total": table_total
                }),
                *side_effect_started,
            )?;
        }
        Ok(())
    })();
    match import_result {
        Err(DumpImportError::Emit(error)) => {
            *side_effect_started = true;
            let _ = set_mysql_import_session_tuning(&mut adapter, true);
            let _ = restore_mysql_local_infile_value(&mut adapter, local_infile_restore);
            return Err(DumpImportError::Emit(error));
        }
        import_result => {
            *side_effect_started = true;
            let restore_result = set_mysql_import_session_tuning(&mut adapter, true);
            let local_infile_restore_result = restore_mysql_local_infile_policy(
                &mut adapter,
                local_infile_restore,
                request.request_id.clone(),
                emit,
                side_effect_started,
            );
            restore_result?;
            local_infile_restore_result?;
            import_result?;
        }
    }

    finalize_dump_import(
        &mut adapter,
        &manifest,
        &tables,
        &imported_rows_by_table,
        input_dir,
        request.request_id.clone(),
        mode,
        &selected,
        strict_manifest,
        &manifest_warnings,
        rows_imported,
        chunks_imported,
        table_total,
        emit,
        side_effect_started,
    )
}

/// replace/recreate 모드일 때 import 전에 대상 테이블을 재생성 가능한 상태로 만든다.
///
/// (1) Surviving-FK preflight (MySQL 전용, abort): import set 밖의 타겟 테이블이
///     대상 테이블을 참조하는 FK를 갖고 있으면, 부모 재생성 시 그 살아있는 자식 FK가
///     새 부모와 (charset/collation) 호환되지 않아 ERROR 3780이 난다. 타겟을 손대지
///     않고 명확한 에러로 차단한다.
///
/// (2) Drop-all-then-create-all 순서: import set 내부의 모든 대상 테이블을 자식 우선
///     (역의존성) 순서로 먼저 DROP한 뒤 루프에서 생성한다. 이렇게 하지 않고 테이블별로
///     즉시 DROP→CREATE 하면, 부모를 재생성하는 시점에 아직 DROP되지 않은 자식의 FK가
///     살아 있어 동일한 ERROR 3780을 유발한다.
///
/// merge 모드에서는 아무것도 하지 않는다.
fn prepare_import_target(
    mode: &str,
    tables: &[DumpTableManifest],
    adapter: &mut LiveAdapter,
    target_schema: &str,
    side_effect_started: &mut bool,
) -> DumpImportResult<()> {
    if !matches!(mode, "replace" | "recreate") {
        return Ok(());
    }
    let import_set: BTreeSet<String> = tables.iter().map(|table| table.name.clone()).collect();
    preflight_surviving_referencing_fks(adapter, target_schema, &import_set)?;

    // tables는 parent-first(dependency order)이므로 rev()는 child-first가 된다.
    // foreign_key_checks=0이 이미 켜져 있어 역순 DROP은 안전하다.
    for table_manifest in tables.iter().rev() {
        *side_effect_started = true;
        adapter
            .execute_sql(&drop_table_sql(adapter.engine(), &table_manifest.name))
            .map_err(|err| dump_import_ddl_error("drop_table", &table_manifest.name, &err))?;
    }
    Ok(())
}

/// 단일 테이블의 데이터를 적재한다. MySQL TSV fast-path(LOAD DATA / 병렬 / fallback)와
/// 엔진 무관 generic 청크 INSERT 경로를 분기하고, 이 테이블에 적재한 (rows, chunks)를 반환한다.
/// 테이블 생성(DDL)과 진행률 table_progress 이벤트는 호출자가 담당한다.
fn import_table_rows<F, R>(
    endpoint: &Endpoint,
    adapter: &mut LiveAdapter,
    input_path: &Path,
    table: &NormalizedTable,
    table_manifest: &DumpTableManifest,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<(u64, u64)>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    if data_format == "tsv" && !has_binary_columns(table) {
        if let LiveAdapter::MySql(conn) = adapter {
            let chunk_ctx = MysqlImportChunkContext {
                table,
                table_manifest,
                compression,
                request_id: request_id.clone(),
                overall_rows_before,
                overall_rows_total,
            };
            return import_mysql_tsv_table(
                endpoint,
                conn,
                input_path,
                threads,
                &chunk_ctx,
                emit,
                side_effect_started,
            );
        }
    }

    let mut table_rows = 0_u64;
    let mut table_chunks = 0_u64;
    for chunk_index in 1..=table_manifest.chunks {
        let chunk_path = dump_manifest_chunk_path(
            input_path,
            &table_manifest.path,
            chunk_index,
            data_format,
            compression,
        )?;
        let rows = read_dump_rows(&chunk_path, table, data_format, compression)?;
        let row_count = rows.len() as u64;
        *side_effect_started = true;
        adapter.insert_rows(table, rows)?;
        table_rows += row_count;
        table_chunks += 1;
        emit_import_event(
            emit,
            dump_import_row_progress_event(
            request_id.clone(),
            &table.name,
            table_rows,
            table_manifest.rows,
            overall_rows_before,
            overall_rows_total,
            row_count,
            ChunkProgress {
                chunks_done: Some(chunk_index),
                chunks_total: Some(table_manifest.chunks),
                chunk_index: Some(chunk_index),
                load_ms: None,
            },
            "insert_rows",
            ),
            *side_effect_started,
        )?;
    }
    Ok((table_rows, table_chunks))
}

/// import 데이터 적재 완료 후의 마무리 단계: post-load DDL(인덱스/FK) 적용,
/// 적재 행수 검증, View 생성(best-effort), 리포트 기록 후 최종 result JSON을 만든다.
fn finalize_dump_import<F, R>(
    adapter: &mut LiveAdapter,
    manifest: &DumpManifest,
    tables: &[DumpTableManifest],
    imported_rows_by_table: &BTreeMap<String, u64>,
    input_dir: &str,
    request_id: Option<String>,
    mode: &str,
    selected: &BTreeSet<String>,
    strict_manifest: bool,
    manifest_warnings: &[String],
    rows_imported: u64,
    chunks_imported: u64,
    table_total: usize,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<Value>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let input_path = Path::new(input_dir);
    let target_engine = adapter.engine().to_string();
    if should_apply_post_load_ddl(mode) {
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import_post_load",
            "message": "현재 단계: 인덱스/FK 생성 중 - 데이터 Import는 완료, 후처리 진행 중",
            "strategy": "post_load_ddl"
            }),
            *side_effect_started,
        )?;
        *side_effect_started = true;
        apply_post_load_ddl(adapter, &manifest.schema, &target_engine)?;
    } else {
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import_post_load",
            "message": post_load_ddl_skip_message(mode),
            "strategy": "existing_schema"
            }),
            *side_effect_started,
        )?;
    }
    // import가 실제로 적재한 행 수가 덤프와 일치하는지만 검증한다(적재 정확성).
    // 타겟 DB를 다시 세는 검증(verify_target_row_counts)은 하지 않는다 — 타겟이
    // 살아있는 DB면 import 동안 외부 write(예: login_attempts에 새 로그인 시도)로
    // row 수가 정상적으로 달라질 수 있어, 정확 일치를 요구하면 오탐으로 실패한다.
    // (foreign_key_checks=0/unique_checks=0으로 관용 적재하는 정책과도 일관.)
    verify_imported_row_counts(tables, imported_rows_by_table)?;

    // View 생성 (best-effort). 데이터는 이미 커밋되었으므로 View 실패가 전체 import를 무효화하지 않는다.
    // 전체 import(테이블 부분 선택 없음)일 때만 시도한다 — 부분 import면 View가 참조하는 base table이 없을 수 있다.
    let view_outcome = if selected.is_empty() && !manifest.views.is_empty() {
        import_views(
            adapter,
            manifest,
            &target_engine,
            mode,
            request_id.clone(),
            emit,
            side_effect_started,
        )?
    } else {
        ViewImportOutcome::default()
    };
    let import_report = json!({
        "success": true,
        "mode": mode,
        "tables": table_total,
        "rows_imported": rows_imported,
        "chunks_imported": chunks_imported,
        "imported_rows_by_table": imported_rows_by_table,
        "verification": {
            "row_counts": "passed",
            "strict_manifest": strict_manifest,
            "warnings": manifest_warnings
        },
        "views_imported": view_outcome.imported,
        "views_failed": view_outcome.failed,
        "views_skipped_cross_engine": view_outcome.skipped_cross_engine
    });
    *side_effect_started = true;
    write_dump_import_report(input_path, &import_report)?;
    let import_report_path = dump_import_report_path(input_path)?;

    Ok(json!({
        "event": "result",
        "request_id": request_id,
        "command": "dump.import",
        "success": true,
        "input_dir": input_dir,
        "mode": mode,
        "tables": table_total,
        "rows_imported": rows_imported,
        "chunks_imported": chunks_imported,
        "verification": import_report["verification"].clone(),
        "import_report": import_report_path.display().to_string(),
        "views_imported": import_report["views_imported"].clone(),
        "views_failed": import_report["views_failed"].clone(),
        "views_skipped_cross_engine": import_report["views_skipped_cross_engine"].clone()
    }))
}

#[derive(Debug, Default)]
struct ViewImportOutcome {
    imported: Vec<String>,
    failed: Vec<Value>,
    skipped_cross_engine: Vec<String>,
}

/// manifest의 View들을 대상 DB에 생성한다.
/// - source/target 엔진이 다르면 정의 SQL이 호환되지 않으므로 전부 skip.
/// - View 간 의존성 순서 문제를 fixpoint 재시도 루프로 해결한다.
/// - 각 View 실패는 non-fatal: 결과에 모아 보고만 한다.
fn import_views<A: MigrationAdapter, F, R>(
    adapter: &mut A,
    manifest: &DumpManifest,
    target_engine: &str,
    mode: &str,
    request_id: Option<String>,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<ViewImportOutcome>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut outcome = ViewImportOutcome::default();

    if manifest.source_engine != target_engine {
        outcome.skipped_cross_engine = manifest.views.iter().map(|v| v.name.clone()).collect();
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": format!(
                "크로스 엔진 import: View {}개는 정의 비호환으로 건너뜁니다 ({} -> {})",
                outcome.skipped_cross_engine.len(),
                manifest.source_engine,
                target_engine
            ),
            }),
            *side_effect_started,
        )?;
        return Ok(outcome);
    }

    // 정화 + 단일 CREATE VIEW 문 검증. 검증 실패한 정의는 실행하지 않고 즉시 failed로 보고한다.
    // (변조된 manifest가 multi-statement SQL 체인을 심는 것을 차단 — 특히 PostgreSQL batch_execute 경로)
    let mut pending: Vec<(String, String)> = Vec::with_capacity(manifest.views.len());
    let mut validated_names: Vec<&str> = Vec::with_capacity(manifest.views.len());
    for view in &manifest.views {
        let sanitized =
            sanitize_view_definition(&view.definition, &manifest.database, target_engine);
        // shape 검증(단일 CREATE ... VIEW 문) + MySQL DEFINER/SQL SECURITY 잔존 fail-closed.
        let validation = validate_single_view_statement(&sanitized).and_then(|()| {
            if target_engine == "mysql" && mysql_definition_has_residual_definer(&sanitized) {
                Err("residual DEFINER/SQL SECURITY DEFINER clause after sanitization".to_string())
            } else {
                Ok(())
            }
        });
        match validation {
            Ok(()) => {
                validated_names.push(&view.name);
                pending.push((view.name.clone(), sanitized));
            }
            Err(reason) => {
                outcome
                    .failed
                    .push(json!({ "name": view.name, "error": format!("rejected: {reason}") }));
                emit_import_event(
                    emit,
                    json!({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "dump_import",
                    "message": format!("View '{}' 거부됨 (안전하지 않은 정의): {reason}", view.name),
                    }),
                    *side_effect_started,
                )?;
            }
        }
    }

    // replace/recreate 모드면 기존 View를 먼저 정리한다 (테이블이 아닌 View 전용 DROP).
    // 검증을 통과한 View만 DROP 대상으로 삼는다.
    if matches!(mode, "replace" | "recreate") {
        for name in &validated_names {
            *side_effect_started = true;
            let _ = adapter.execute_sql(&drop_view_sql(target_engine, name));
        }
    }

    // fixpoint 루프: 한 바퀴에 하나도 성공하지 못하면 중단한다.
    let mut last_errors: BTreeMap<String, String> = BTreeMap::new();
    loop {
        let mut progressed = false;
        let mut still_pending: Vec<(String, String)> = Vec::new();
        for (name, sql) in pending.drain(..) {
            *side_effect_started = true;
            match adapter.execute_sql(&sql) {
                Ok(()) => {
                    progressed = true;
                    last_errors.remove(&name);
                    outcome.imported.push(name.clone());
                    emit_import_event(
                        emit,
                        json!({
                        "event": "table_progress",
                        "request_id": request_id,
                        "table": name,
                        "status": "completed",
                        "kind": "view"
                        }),
                        *side_effect_started,
                    )?;
                }
                Err(err) => {
                    last_errors.insert(name.clone(), err);
                    still_pending.push((name, sql));
                }
            }
        }
        pending = still_pending;
        if pending.is_empty() || !progressed {
            break;
        }
    }

    for (name, _sql) in pending {
        let error = last_errors
            .get(&name)
            .cloned()
            .unwrap_or_else(|| "unknown error".to_string());
        outcome.failed.push(json!({ "name": name, "error": error }));
    }

    if !outcome.failed.is_empty() {
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": format!(
                "View {}개 생성 성공, {}개 실패 (데이터 import는 정상 완료)",
                outcome.imported.len(),
                outcome.failed.len()
            ),
            }),
            *side_effect_started,
        )?;
    }

    Ok(outcome)
}

/// MySQL TSV import 경로(fast-path / parallel / insert fallback)가 공통으로
/// 전달받던 6개 인자 클러스터를 묶는다. 테이블 정의·매니페스트·압축·요청 ID·
/// 전체 진행률 기준선을 한 번에 관통시켜 시그니처 부풀림을 줄인다.
#[derive(Clone)]
struct MysqlImportChunkContext<'a> {
    table: &'a NormalizedTable,
    table_manifest: &'a DumpTableManifest,
    compression: &'a str,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
}

fn import_mysql_tsv_table<F, R>(
    endpoint: &Endpoint,
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    threads: usize,
    ctx: &MysqlImportChunkContext,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<(u64, u64)>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    if !mysql_local_infile_enabled(conn) {
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": ctx.request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile is disabled; using safe Rust INSERT fallback",
            "strategy": "insert_fallback",
            "performance": "safe_fallback"
            }),
            *side_effect_started,
        )?;
        return import_mysql_tsv_table_insert_fallback(
            conn,
            input_path,
            ctx,
            emit,
            side_effect_started,
        );
    }

    if threads > 1 && ctx.table_manifest.chunks > 1 {
        let result = import_mysql_tsv_table_parallel(
            endpoint,
            input_path,
            threads,
            ctx,
            emit,
            side_effect_started,
        );
        return match result {
            Ok(result) => Ok(result),
            Err(DumpImportError::Domain(err)) if is_mysql_local_infile_disabled_error(&err) => {
                emit_import_event(
                    emit,
                    json!({
                    "event": "phase",
                    "request_id": ctx.request_id,
                    "phase": "dump_import",
                    "message": "MySQL LOAD DATA LOCAL is disabled; using safe Rust INSERT fallback",
                    "strategy": "insert_fallback",
                    "performance": "safe_fallback"
                    }),
                    *side_effect_started,
                )?;
                import_mysql_tsv_table_insert_fallback(
                    conn,
                    input_path,
                    ctx,
                    emit,
                    side_effect_started,
                )
            }
            Err(err) => Err(err),
        };
    }

    // 테이블 재시작 안전망: 청크 단위 재접속 재시도(load_chunk_with_reconnect)가 최종
    // 실패해도, transient 끊김이면 이 테이블을 TRUNCATE 후 첫 청크부터 한 번 더 재적재한다.
    // TRUNCATE가 "서버 OK 직후 클라 수신 직전" 좁은 창의 부분 커밋/중복 잔여 위험까지 제거한다.
    // (replace/recreate는 대상 테이블을 미리 일괄 DROP하므로 이 재시작이 안전하다.)
    const MAX_TABLE_ATTEMPTS: u32 = 2;
    let mut table_attempt: u32 = 0;
    loop {
        table_attempt += 1;
        let mut rows_imported = 0_u64;
        let mut chunks_imported = 0_u64;
        let mut retryable_table_error: Option<String> = None;

        for chunk_index in 1..=ctx.table_manifest.chunks {
            let chunk_path = dump_manifest_chunk_path(
                input_path,
                &ctx.table_manifest.path,
                chunk_index,
                "tsv",
                ctx.compression,
            )?;
            let started = Instant::now();
            *side_effect_started = true;
            let rows = match load_chunk_with_reconnect(
                endpoint,
                conn,
                ctx.table,
                &chunk_path,
                ctx.compression,
            ) {
                Ok(rows) => rows,
                Err(err) if is_mysql_local_infile_disabled_error(&err) => {
                    emit_import_event(
                        emit,
                        json!({
                        "event": "phase",
                        "request_id": ctx.request_id,
                        "phase": "dump_import",
                        "message": "MySQL LOAD DATA LOCAL is disabled; using safe Rust INSERT fallback",
                        "strategy": "insert_fallback",
                        "performance": "safe_fallback"
                        }),
                        *side_effect_started,
                    )?;
                    return import_mysql_tsv_table_insert_fallback(
                        conn,
                        input_path,
                        ctx,
                        emit,
                        side_effect_started,
                    );
                }
                Err(err) if is_transient_disconnect_error(&err) => {
                    // 청크 재접속 재시도로도 복구 안 된 지속적 끊김.
                    retryable_table_error = Some(err);
                    break;
                }
                Err(err) => return Err(err.into()),
            };
            rows_imported += rows;
            chunks_imported += 1;
            emit_import_event(
                emit,
                dump_import_row_progress_event(
                ctx.request_id.clone(),
                &ctx.table.name,
                rows_imported,
                ctx.table_manifest.rows,
                ctx.overall_rows_before,
                ctx.overall_rows_total,
                rows,
                ChunkProgress {
                    chunks_done: Some(chunks_imported),
                    chunks_total: Some(ctx.table_manifest.chunks),
                    chunk_index: Some(chunk_index),
                    load_ms: Some(started.elapsed().as_millis() as u64),
                },
                "load_data_local_infile",
                ),
                *side_effect_started,
            )?;
        }

        match retryable_table_error {
            None => return Ok((rows_imported, chunks_imported)),
            Some(err) => {
                if table_attempt >= MAX_TABLE_ATTEMPTS {
                    return Err(err.into());
                }
                emit_import_event(
                    emit,
                    json!({
                    "event": "phase",
                    "request_id": ctx.request_id,
                    "phase": "dump_import",
                    "message": format!(
                        "연결 끊김으로 테이블 [{}] 재시작 (TRUNCATE 후 재적재)",
                        ctx.table.name
                    ),
                    "strategy": "table_restart"
                    }),
                    *side_effect_started,
                )?;
                // 재접속 후 TRUNCATE. 새 세션은 튜닝이 초기화되므로 튜닝 적용된 커넥션으로 교체.
                *conn = connect_tuned_mysql_import_conn(endpoint)?;
                *side_effect_started = true;
                conn.query_drop(format!(
                    "TRUNCATE TABLE {}",
                    quote_ident("mysql", &ctx.table.name)
                ))
                .map_err(|truncate_err| {
                    format!("mysql table restart truncate error: {truncate_err}")
                })?;
                // 루프 상단으로 → 첫 청크부터 재적재.
            }
        }
    }
}

fn mysql_local_infile_enabled(conn: &mut mysql::PooledConn) -> bool {
    mysql_local_infile_value(conn)
        .map(|value| mysql_bool_value_enabled(&value))
        .unwrap_or(true)
}

fn mysql_local_infile_value(conn: &mut mysql::PooledConn) -> Option<String> {
    conn.query_first::<(String, String), _>("SHOW VARIABLES LIKE 'local_infile'")
        .ok()
        .flatten()
        .map(|(_, value)| value)
}

fn mysql_bool_value_enabled(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "on" | "1" | "true" | "yes"
    )
}

fn mysql_set_global_local_infile_sql(enabled: bool) -> &'static str {
    if enabled {
        "SET GLOBAL local_infile = 1"
    } else {
        "SET GLOBAL local_infile = 0"
    }
}

fn mysql_local_infile_policy_from_payload(payload: &Value) -> Result<&str, String> {
    let policy = payload
        .get("mysql_local_infile_policy")
        .and_then(Value::as_str)
        .unwrap_or("fallback");
    if matches!(policy, "fallback" | "temporary_global") {
        Ok(policy)
    } else {
        Err(format!("unsupported mysql_local_infile_policy: {policy}"))
    }
}

fn prepare_mysql_local_infile_policy<F, R>(
    adapter: &mut LiveAdapter,
    endpoint: &Endpoint,
    policy: &str,
    request_id: Option<String>,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<Option<String>>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    if policy != "temporary_global" {
        return Ok(None);
    }
    let previous = {
        let LiveAdapter::MySql(conn) = adapter else {
            return Ok(None);
        };
        let previous = mysql_local_infile_value(conn).unwrap_or_else(|| "ON".to_string());
        if mysql_bool_value_enabled(&previous) {
            emit_import_event(
                emit,
                json!({
                "event": "phase",
                "request_id": request_id,
                "phase": "dump_import",
                "message": "MySQL local_infile is already enabled; using fast LOAD DATA LOCAL import",
                "strategy": "load_data_local_infile",
                "performance": "fast_path"
                }),
                *side_effect_started,
            )?;
            return Ok(None);
        }

        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile is disabled; trying temporary SET GLOBAL local_infile=ON",
            "strategy": "temporary_local_infile",
            "performance": "fast_path_attempt"
            }),
            *side_effect_started,
        )?;

        *side_effect_started = true;
        if let Err(err) = conn.query_drop(mysql_set_global_local_infile_sql(true)) {
            emit_import_event(
                emit,
                json!({
                "event": "phase",
                "request_id": request_id,
                "phase": "dump_import",
                "message": format!("MySQL local_infile temporary enable failed: {err}; using safe Rust INSERT fallback"),
                "strategy": "insert_fallback",
                "performance": "safe_fallback"
                }),
                *side_effect_started,
            )?;
            return Ok(None);
        }
        previous
    };

    if let Err(err) = LiveAdapter::connect(endpoint).map(|new_adapter| *adapter = new_adapter) {
        if let LiveAdapter::MySql(conn) = adapter {
            *side_effect_started = true;
            let _ = conn.query_drop(mysql_set_global_local_infile_sql(mysql_bool_value_enabled(
                &previous,
            )));
        }
        return Err(err.into());
    }
    let enabled = match adapter {
        LiveAdapter::MySql(conn) => mysql_local_infile_enabled(conn),
        LiveAdapter::PostgreSql(_) => false,
    };
    if enabled {
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile temporarily enabled; using fast LOAD DATA LOCAL import",
            "strategy": "load_data_local_infile",
            "performance": "fast_path"
            }),
            *side_effect_started,
        )?;
    } else {
        emit_import_event(
            emit,
            json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile temporary enable did not take effect; using safe Rust INSERT fallback",
            "strategy": "insert_fallback",
            "performance": "safe_fallback"
            }),
            *side_effect_started,
        )?;
    }
    Ok(Some(previous))
}

fn restore_mysql_local_infile_policy<F, R>(
    adapter: &mut LiveAdapter,
    previous: Option<String>,
    request_id: Option<String>,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<()>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    if previous.is_some() {
        *side_effect_started = true;
    }
    let Some(previous) = restore_mysql_local_infile_value(adapter, previous)? else {
        return Ok(());
    };
    emit_import_event(
        emit,
        json!({
        "event": "phase",
        "request_id": request_id,
        "phase": "dump_import",
        "message": format!("MySQL local_infile restored to {previous}"),
        "strategy": "temporary_local_infile_restore"
        }),
        *side_effect_started,
    )?;
    Ok(())
}

fn restore_mysql_local_infile_value(
    adapter: &mut LiveAdapter,
    previous: Option<String>,
) -> Result<Option<String>, String> {
    let Some(previous) = previous else {
        return Ok(None);
    };
    let enabled = mysql_bool_value_enabled(&previous);
    let LiveAdapter::MySql(conn) = adapter else {
        return Ok(None);
    };
    conn.query_drop(mysql_set_global_local_infile_sql(enabled))
        .map_err(|err| {
            format!("mysql local_infile restore failed; previous value was {previous}: {err}")
        })?;
    Ok(Some(previous))
}

fn is_mysql_local_infile_disabled_error(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains(MYSQL_ERR_LOCAL_INFILE_DISABLED)
        || lower.contains("loading local data is disabled")
        || lower.contains("local infile")
            && (lower.contains("disabled") || lower.contains("not allowed"))
}

fn validated_timezone_sql(value: Option<&str>) -> Result<Option<String>, String> {
    let Some(sql) = value.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let invalid_message = "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone or SET TIME ZONE is allowed";
    let normalized = sql.to_ascii_lowercase();
    if normalized.contains(';')
        || normalized.contains("--")
        || normalized.contains("/*")
        || normalized.contains("*/")
        || normalized.contains('\0')
    {
        return Err(invalid_message.to_string());
    }

    let Some(after_set) = normalized.strip_prefix("set") else {
        return Err(invalid_message.to_string());
    };

    let after_set = after_set.trim_start();
    let value = if let Some(after_session) = after_set.strip_prefix("session") {
        let Some(after_variable) = after_session.trim_start().strip_prefix("time_zone") else {
            return Err(invalid_message.to_string());
        };
        let Some(value) = after_variable.trim_start().strip_prefix('=') else {
            return Err(invalid_message.to_string());
        };
        value
    } else if let Some(after_time) = after_set.strip_prefix("time") {
        let Some(value) = after_time.trim_start().strip_prefix("zone") else {
            return Err(invalid_message.to_string());
        };
        value
    } else {
        return Err(invalid_message.to_string());
    };

    let value = value.trim();
    if value.is_empty() || !is_safe_timezone_literal(value) {
        return Err(invalid_message.to_string());
    }

    Ok(Some(sql.to_string()))
}

fn is_safe_timezone_literal(value: &str) -> bool {
    let value = if value.starts_with('\'') && value.ends_with('\'') && value.len() >= 2 {
        &value[1..value.len() - 1]
    } else {
        value
    };
    !value.is_empty()
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '+' | '-' | '_' | ':' | '/'))
}

fn mysql_import_session_tuning_sql(restore: bool) -> Vec<String> {
    if restore {
        vec![
            "SET SESSION sql_mode=DEFAULT".to_string(),
            "SET SESSION unique_checks=1".to_string(),
            "SET SESSION foreign_key_checks=1".to_string(),
        ]
        // net_read_timeout / net_write_timeout / wait_timeout은 복원하지 않는다.
        // 세션 스코프 변수이고 이 커넥션은 import 종료 후 닫히는 1회용이라 세션 종료로
        // 자동 소멸한다. 또한 원래 글로벌 기본값을 알 수 없어 되돌릴 대상이 애매하다.
    } else {
        vec![
            "SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', ''), 'NO_ZERO_IN_DATE', ''), 'NO_ZERO_DATE', ''), 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','), ',,', ','))".to_string(),
            "SET SESSION foreign_key_checks=0".to_string(),
            "SET SESSION unique_checks=0".to_string(),
            // 서버 측 세션 idle/전송 타임아웃 상향 — 대량 청크 전송 중 서버가
            // net_read/net_write_timeout(기본 30/60s)이나 wait_timeout으로 먼저
            // 연결을 끊는 것을 방어한다. keepalive(mysql_opts)와 이중 방어.
            format!("SET SESSION net_read_timeout = {MYSQL_IMPORT_NET_TIMEOUT_SECS}"),
            format!("SET SESSION net_write_timeout = {MYSQL_IMPORT_NET_TIMEOUT_SECS}"),
            format!("SET SESSION wait_timeout = {MYSQL_IMPORT_WAIT_TIMEOUT_SECS}"),
        ]
    }
}

fn set_mysql_import_session_tuning(adapter: &mut LiveAdapter, restore: bool) -> Result<(), String> {
    if !matches!(adapter, LiveAdapter::MySql(_)) {
        return Ok(());
    }
    for sql in mysql_import_session_tuning_sql(restore) {
        adapter.execute_sql(&sql)?;
    }
    Ok(())
}

/// import용 세션 튜닝(fk/unique/sql_mode + timeout)이 적용된 MySQL 커넥션을 생성한다.
///
/// 새 세션은 항상 튜닝이 초기화되므로, connect 직후 반드시 튜닝을 재적용한다.
/// 병렬 워커 생성부와 청크 재접속 재시도부에서 공용으로 사용한다 — 그 전에는 병렬
/// 워커가 어떤 세션 튜닝도 하지 않아 fk_checks/timeout이 누락돼 있었다.
fn connect_tuned_mysql_import_conn(endpoint: &Endpoint) -> Result<mysql::PooledConn, String> {
    let mut adapter = LiveAdapter::connect(endpoint)?;
    set_mysql_import_session_tuning(&mut adapter, false)?;
    match adapter {
        LiveAdapter::MySql(conn) => Ok(conn),
        _ => Err("mysql import: unexpected adapter kind".to_string()),
    }
}

/// 커넥션 끊김/네트워크성 transient 에러인지 판정한다.
///
/// 이 에러들만 재접속 재시도 대상이다. 데이터/스키마 에러(1452/3780/1062 등)나
/// local_infile 비활성(3948)은 절대 포함하지 않는다 — 그런 에러를 재시도하면
/// 무한 반복하거나 다른 fallback 경로를 우회하게 된다.
fn is_transient_disconnect_error(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("server disconnected")
        || lower.contains("gone away") // MySQL 2006
        || lower.contains("lost connection") // MySQL 2013
        || lower.contains("broken pipe")
        || lower.contains("connection reset")
        || lower.contains("connection aborted")
        || lower.contains("packets out of order")
        || lower.contains("unexpected end of file")
        || lower.contains("unexpectedeof")
        || lower.contains("timed out")
        || lower.contains("connection refused") // 재접속 시 서버 재기동 대기
}

fn import_mysql_tsv_table_insert_fallback<F, R>(
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    ctx: &MysqlImportChunkContext,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<(u64, u64)>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;
    for chunk_index in 1..=ctx.table_manifest.chunks {
        let chunk_path = dump_manifest_chunk_path(
            input_path,
            &ctx.table_manifest.path,
            chunk_index,
            "tsv",
            ctx.compression,
        )?;
        let started = Instant::now();
        *side_effect_started = true;
        let rows =
            insert_mysql_tsv_chunk_with_batches(conn, ctx.table, &chunk_path, ctx.compression)
            .map_err(|err| {
                format!(
                    "mysql insert fallback error for table {} chunk {}: {err}",
                    ctx.table.name, chunk_index
                )
            })?;
        rows_imported += rows;
        chunks_imported += 1;
        emit_import_event(
            emit,
            dump_import_row_progress_event(
            ctx.request_id.clone(),
            &ctx.table.name,
            rows_imported,
            ctx.table_manifest.rows,
            ctx.overall_rows_before,
            ctx.overall_rows_total,
            rows,
            ChunkProgress {
                chunks_done: Some(chunks_imported),
                chunks_total: Some(ctx.table_manifest.chunks),
                chunk_index: Some(chunk_index),
                load_ms: Some(started.elapsed().as_millis() as u64),
            },
            "insert_fallback",
            ),
            *side_effect_started,
        )?;
    }
    Ok((rows_imported, chunks_imported))
}

fn insert_mysql_tsv_chunk_with_batches(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    chunk_path: &Path,
    compression: &str,
) -> Result<u64, String> {
    stream_tsv_rows_in_batches(
        chunk_path,
        table,
        compression,
        MYSQL_INSERT_FALLBACK_BATCH_ROWS,
        MYSQL_INSERT_FALLBACK_BATCH_BYTES,
        |rows| {
            conn.query_drop(insert_rows_literal_sql_for_table("mysql", table, rows))
                .map_err(|err| err.to_string())
        },
    )
}

fn import_mysql_tsv_table_parallel<F, R>(
    endpoint: &Endpoint,
    input_path: &Path,
    threads: usize,
    ctx: &MysqlImportChunkContext,
    emit: &mut F,
    side_effect_started: &mut bool,
) -> DumpImportResult<(u64, u64)>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let max_threads = threads.max(1).min(ctx.table_manifest.chunks as usize);
    let mut pending =
        adaptive_import_chunk_order(input_path, ctx.table_manifest, "tsv", ctx.compression);
    let mut active = 0_usize;
    let mut completed = 0_u64;
    let mut rows_imported = 0_u64;
    let mut first_error: Option<String> = None;
    let mut emit_error: Option<ProtocolEmitError> = None;
    let mut handles = Vec::new();
    let aborted = Arc::new(AtomicBool::new(false));
    let (sender, receiver) = mpsc::channel::<ImportChunkEvent>();

    while active < max_threads {
        if let Some(chunk_index) = pending.pop_front() {
            *side_effect_started = true;
            handles.push(spawn_mysql_import_chunk_worker(
                endpoint.clone(),
                input_path.to_path_buf(),
                ctx.table.clone(),
                ctx.table_manifest.path.clone(),
                chunk_index,
                ctx.compression.to_string(),
                sender.clone(),
                aborted.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed < ctx.table_manifest.chunks && active > 0 {
        match receiver.recv() {
            Ok(ImportChunkEvent::Done {
                chunk_index,
                rows,
                load_ms,
            }) => {
                rows_imported += rows;
                completed += 1;
                active = active.saturating_sub(1);
                if let Err(error) = emit_import_event(
                    emit,
                    dump_import_row_progress_event(
                    ctx.request_id.clone(),
                    &ctx.table.name,
                    rows_imported,
                    ctx.table_manifest.rows,
                    ctx.overall_rows_before,
                    ctx.overall_rows_total,
                    rows,
                    ChunkProgress {
                        chunks_done: Some(completed),
                        chunks_total: Some(ctx.table_manifest.chunks),
                        chunk_index: Some(chunk_index),
                        load_ms: Some(load_ms),
                    },
                    "parallel_load_data_local_infile",
                    ),
                    *side_effect_started,
                ) {
                    aborted.store(true, Ordering::Release);
                    emit_error = Some(error);
                    break;
                }
                if let Some(next_chunk) = pending.pop_front() {
                    *side_effect_started = true;
                    handles.push(spawn_mysql_import_chunk_worker(
                        endpoint.clone(),
                        input_path.to_path_buf(),
                        ctx.table.clone(),
                        ctx.table_manifest.path.clone(),
                        next_chunk,
                        ctx.compression.to_string(),
                        sender.clone(),
                        aborted.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(ImportChunkEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed += 1;
                active = active.saturating_sub(1);
            }
            Err(_) => break,
        }
    }

    for handle in handles {
        let _ = handle.join();
    }
    if let Some(error) = emit_error {
        return Err(DumpImportError::Emit(error));
    }
    if let Some(err) = first_error {
        return Err(err.into());
    }
    Ok((rows_imported, completed))
}

fn adaptive_import_chunk_order(
    input_path: &Path,
    table_manifest: &DumpTableManifest,
    data_format: &str,
    compression: &str,
) -> VecDeque<u64> {
    let mut chunks = (1..=table_manifest.chunks)
        .map(|chunk_index| {
            let path = dump_manifest_chunk_path(
                input_path,
                &table_manifest.path,
                chunk_index,
                data_format,
                compression,
            );
            let bytes = path
                .ok()
                .and_then(|path| fs::metadata(path).ok())
                .map(|metadata| metadata.len())
                .unwrap_or(0);
            (chunk_index, bytes)
        })
        .collect::<Vec<_>>();
    chunks.sort_by(|(left_index, left_bytes), (right_index, right_bytes)| {
        right_bytes
            .cmp(left_bytes)
            .then_with(|| left_index.cmp(right_index))
    });
    chunks
        .into_iter()
        .map(|(chunk_index, _)| chunk_index)
        .collect()
}

fn spawn_mysql_import_chunk_worker(
    endpoint: Endpoint,
    input_path: std::path::PathBuf,
    table: NormalizedTable,
    table_path: String,
    chunk_index: u64,
    compression: String,
    sender: mpsc::Sender<ImportChunkEvent>,
    aborted: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = (|| {
            if aborted.load(Ordering::Acquire) {
                return Err("parallel import aborted after protocol emitter failure".to_string());
            }
            // 워커 커넥션에도 세션 튜닝(fk/unique/sql_mode + timeout)을 적용한다.
            // 이전에는 워커가 튜닝 없이 연결해 fk_checks/timeout이 누락돼 있었다.
            let mut conn = connect_tuned_mysql_import_conn(&endpoint)?;
            let chunk_path = dump_manifest_chunk_path(
                &input_path,
                &table_path,
                chunk_index,
                "tsv",
                &compression,
            )?;
            if aborted.load(Ordering::Acquire) {
                return Err("parallel import aborted after protocol emitter failure".to_string());
            }
            let started = Instant::now();
            let rows =
                load_chunk_with_reconnect(&endpoint, &mut conn, &table, &chunk_path, &compression)?;
            Ok((rows, started.elapsed().as_millis() as u64))
        })();
        match result {
            Ok((rows, load_ms)) => {
                let _ = sender.send(ImportChunkEvent::Done {
                    chunk_index,
                    rows,
                    load_ms,
                });
            }
            Err(err) => {
                let _ = sender.send(ImportChunkEvent::Error(err));
            }
        }
    })
}

/// 청크 LOAD DATA를 transient 끊김에 한해 재접속 후 재시도한다.
///
/// - transient 끊김(server disconnected 등)일 때만 backoff 후 새 커넥션으로 재시도.
/// - 데이터/설정 에러(1452/3780/local_infile disabled 등)는 재시도하지 않고 즉시 전파.
/// - `*conn`을 새 커넥션으로 교체하므로, 호출자는 이 청크 이후에도 같은 conn을 계속 쓴다.
///
/// 멱등성: `LOAD DATA`는 InnoDB + autocommit=1에서 단일 statement = 단일 트랜잭션이며,
/// statement 완결(서버 OK) 전에 끊기면 서버가 롤백하므로 재시도가 이론상 안전하다.
/// replace/recreate는 대상 테이블을 미리 일괄 DROP(fresh)하므로 재적재 시 중복 위험이
/// 구조적으로 낮다. 순차 경로는 상위에 테이블 재시작(truncate) 안전망을 둔다.
fn load_chunk_with_reconnect(
    endpoint: &Endpoint,
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    chunk_path: &Path,
    compression: &str,
) -> Result<u64, String> {
    const MAX_ATTEMPTS: u32 = 3;
    let backoffs = [
        std::time::Duration::from_millis(500),
        std::time::Duration::from_secs(1),
        std::time::Duration::from_secs(2),
    ];
    let mut attempt: u32 = 0;
    loop {
        match load_mysql_tsv_chunk(conn, table, chunk_path, compression) {
            Ok(rows) => return Ok(rows),
            Err(err) => {
                attempt += 1;
                let retryable = is_transient_disconnect_error(&err)
                    && !is_mysql_local_infile_disabled_error(&err);
                if !retryable || attempt >= MAX_ATTEMPTS {
                    return Err(err);
                }
                std::thread::sleep(backoffs[(attempt - 1) as usize]);
                // 재접속 + 세션 튜닝 재적용(새 세션은 튜닝이 초기화됨).
                *conn = connect_tuned_mysql_import_conn(endpoint)?;
            }
        }
    }
}

fn load_mysql_tsv_chunk(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    chunk_path: &Path,
    compression: &str,
) -> Result<u64, String> {
    let path = chunk_path.to_path_buf();
    let compression = compression.to_string();
    conn.set_local_infile_handler(Some(LocalInfileHandler::new(move |_, stream| {
        let mut reader = open_dump_reader(&path, &compression)
            .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err))?;
        std::io::copy(&mut reader, stream)?;
        Ok(())
    })));
    let sql = load_data_local_infile_sql("mysql", table, "tunnelforge_chunk");
    let result = conn
        .query_drop(sql)
        .map(|_| conn.affected_rows())
        .map_err(|err| format!("mysql LOAD DATA error: {err}"));
    conn.set_local_infile_handler(None);
    result
}

pub fn load_data_local_infile_sql(
    engine: &str,
    table: &NormalizedTable,
    file_name: &str,
) -> String {
    let columns = column_names(table)
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "LOAD DATA LOCAL INFILE {} INTO TABLE {} CHARACTER SET utf8mb4 FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' ({})",
        sql_literal(&Value::String(file_name.to_string())),
        quote_ident(engine, &table.name),
        columns
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::fs::{self};
    
    use crate::adapters::test_support::schema;

    #[test]
    fn mysql_dump_import_defaults_to_safe_local_infile_policy() {
        assert_eq!(
            mysql_local_infile_policy_from_payload(&json!({})).unwrap(),
            "fallback"
        );
        assert_eq!(
            mysql_local_infile_policy_from_payload(&json!({
                "mysql_local_infile_policy": "temporary_global"
            }))
            .unwrap(),
            "temporary_global"
        );
        assert!(mysql_local_infile_policy_from_payload(&json!({
            "mysql_local_infile_policy": "always"
        }))
        .is_err());
    }

    #[test]
    fn import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms() {
        assert_eq!(
            validated_timezone_sql(Some("SET SESSION time_zone = '+09:00'")).unwrap(),
            Some("SET SESSION time_zone = '+09:00'".to_string())
        );
        assert_eq!(
            validated_timezone_sql(Some("SET TIME ZONE '+09:00'")).unwrap(),
            Some("SET TIME ZONE '+09:00'".to_string())
        );
        assert_eq!(validated_timezone_sql(None).unwrap(), None);
        assert_eq!(validated_timezone_sql(Some("   ")).unwrap(), None);
        assert!(validated_timezone_sql(Some("DROP DATABASE prod")).is_err());
        assert!(
            validated_timezone_sql(Some("SET SESSION time_zone = '+09:00'; DROP TABLE users"))
                .is_err()
        );
        assert!(
            validated_timezone_sql(Some("SET SESSION time_zone = '+09:00' -- trailing")).is_err()
        );
        assert!(validated_timezone_sql(Some("SET TIME ZONE '+09:00' -- trailing")).is_err());
        assert!(validated_timezone_sql(Some("SET GLOBAL time_zone = '+09:00'")).is_err());
    }

    #[test]
    fn local_infile_disabled_error_is_detected_for_fallback_import() {
        assert!(is_mysql_local_infile_disabled_error(
            "mysql LOAD DATA error: MySqlError { ERROR 3948 (42000): Loading local data is disabled; this must be enabled on both the client and server sides }"
        ));
        assert!(!is_mysql_local_infile_disabled_error(
            "mysql LOAD DATA error: duplicate key"
        ));
    }

    #[test]
    fn mysql_local_infile_boolean_values_and_set_sql_are_stable() {
        assert!(mysql_bool_value_enabled("ON"));
        assert!(mysql_bool_value_enabled("1"));
        assert!(mysql_bool_value_enabled(" yes "));
        assert!(!mysql_bool_value_enabled("OFF"));
        assert!(!mysql_bool_value_enabled("0"));
        assert_eq!(
            mysql_set_global_local_infile_sql(true),
            "SET GLOBAL local_infile = 1"
        );
        assert_eq!(
            mysql_set_global_local_infile_sql(false),
            "SET GLOBAL local_infile = 0"
        );
    }

    #[test]
    fn adaptive_import_chunk_order_prefers_larger_chunk_files() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-import-order-test-{}",
            current_unix_seconds()
        ));
        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        fs::write(table_dir.join("chunk_000001.tsv"), b"1\n").unwrap();
        fs::write(table_dir.join("chunk_000002.tsv"), vec![b'x'; 1024]).unwrap();
        fs::write(table_dir.join("chunk_000003.tsv"), vec![b'y'; 64]).unwrap();
        let manifest = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 3,
            chunks: 3,
            chunk_sha256: BTreeMap::new(),
        };

        assert_eq!(
            adaptive_import_chunk_order(&dir, &manifest, "tsv", "none"),
            vec![2, 3, 1]
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn load_data_sql_uses_local_infile_and_tsv_options() {
        let table = schema().tables[0].clone();

        assert_eq!(
            load_data_local_infile_sql("mysql", &table, "chunk.tsv"),
            "LOAD DATA LOCAL INFILE 'chunk.tsv' INTO TABLE `users` CHARACTER SET utf8mb4 FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' (`id`, `name`)"
        );
    }

    #[test]
    fn mysql_dump_import_uses_fast_session_tuning_statements() {
        assert_eq!(
            mysql_import_session_tuning_sql(false),
            vec![
                "SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', ''), 'NO_ZERO_IN_DATE', ''), 'NO_ZERO_DATE', ''), 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','), ',,', ','))".to_string(),
                "SET SESSION foreign_key_checks=0".to_string(),
                "SET SESSION unique_checks=0".to_string(),
                "SET SESSION net_read_timeout = 600".to_string(),
                "SET SESSION net_write_timeout = 600".to_string(),
                "SET SESSION wait_timeout = 28800".to_string(),
            ]
        );
        // 복원 분기에는 timeout SET을 넣지 않는다(세션 종료로 자동 소멸).
        assert_eq!(
            mysql_import_session_tuning_sql(true),
            vec![
                "SET SESSION sql_mode=DEFAULT".to_string(),
                "SET SESSION unique_checks=1".to_string(),
                "SET SESSION foreign_key_checks=1".to_string(),
            ]
        );
    }

    #[test]
    fn mysql_dump_import_uses_fallback_when_local_infile_is_disabled() {
        assert!(is_mysql_local_infile_disabled_error(
            "ERROR 3948 (42000): Loading local data is disabled"
        ));
    }

    #[test]
    fn transient_disconnect_errors_are_retryable() {
        for msg in [
            "mysql LOAD DATA error: IoError { server disconnected }",
            "ERROR 2006 (HY000): MySQL server has gone away",
            "ERROR 2013 (HY000): Lost connection to MySQL server during query",
            "Broken pipe (os error 32)",
            "Connection reset by peer",
            "Packets out of order",
            "operation timed out",
            "Connection refused (os error 111)",
        ] {
            assert!(
                is_transient_disconnect_error(msg),
                "expected transient: {msg}"
            );
        }
    }

    #[test]
    fn data_and_schema_errors_are_not_retryable() {
        // 재시도하면 안 되는 에러들(무한 반복/우회 방지). 특히 1452/3780/1062/3948.
        for msg in [
            "ERROR 1452 (23000): Cannot add or update a child row: a foreign key constraint fails",
            "Referencing column 'x' and referenced column 'y' in foreign key constraint are incompatible", // 3780
            "ERROR 1062 (23000): Duplicate entry '1' for key 'PRIMARY'",
            "ERROR 3948 (42000): Loading local data is disabled",
            "ERROR 1054 (42S22): Unknown column 'foo' in 'field list'",
        ] {
            assert!(
                !is_transient_disconnect_error(msg),
                "expected NOT transient: {msg}"
            );
        }
    }

    #[test]
    fn import_streaming_emit_failure_stops_before_followup_or_side_effect() {
        let request = Request {
            command: "dump.import".to_string(),
            request_id: Some("import-emit-failure".to_string()),
            payload: json!({}),
        };
        let mut calls = 0;

        let error = dump_import_streaming(&request, |_event| {
            calls += 1;
            Err(ProtocolEmitError::io("broken import emitter"))
        })
        .expect_err("import emitter failure must propagate");

        assert_eq!(calls, 1);
        assert!(!error.side_effect_started());
    }

    #[test]
    fn import_emit_failure_after_side_effect_is_marked_indeterminate() {
        let error = emit_import_event(
            &mut |_event| Err(ProtocolEmitError::io("broken import emitter")),
            json!({"event": "result"}),
            true,
        )
        .expect_err("post-side-effect failure must propagate");

        assert!(error.side_effect_started());
    }
}
