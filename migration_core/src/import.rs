use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self};
use std::io::BufRead;
use std::path::Path;
use std::sync::mpsc;
use std::thread;
use std::time::Instant;

use mysql::{prelude::Queryable, LocalInfileHandler};
use crate::*;

#[path = "safe_restore.rs"]
pub(crate) mod safe_restore;
#[path = "safe_restore_digest.rs"]
pub(crate) mod safe_restore_digest;

/// MySQL `LOAD DATA LOCAL`이 비활성화됐을 때 반환되는 에러 코드(ERROR 3948) 매칭 토큰.
const MYSQL_ERR_LOCAL_INFILE_DISABLED: &str = "3948";
/// import 세션 튜닝에서 상향하는 net_read/net_write 타임아웃(초).
const MYSQL_IMPORT_NET_TIMEOUT_SECS: u32 = 600;
/// import 세션 튜닝에서 상향하는 wait_timeout(초).
const MYSQL_IMPORT_WAIT_TIMEOUT_SECS: u32 = 28800;
const MYSQL_IMPORT_MAX_WARNINGS: usize = 65_535;

pub(crate) fn dump_import_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump_import",
        "message": "dump import started"
    }));

    match dump_import(request, |event| emit(event)) {
        Ok(result) => emit(result),
        Err(err) => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
}

/// Validate the complete metadata before selecting tables or touching the target.
fn validate_import_metadata(
    manifest: &DumpManifest,
    selected: &BTreeSet<String>,
    target_engine: &str,
) -> Result<BTreeMap<String, String>, String> {
    let invalid = |message: String| classified_import_error("export_invalid", &message, None);
    let mut names = BTreeSet::new();
    let mut paths = BTreeSet::new();
    for table in &manifest.tables {
        if !names.insert(table.name.as_str()) {
            return Err(invalid(format!("duplicate table {}", table.name)));
        }
        let path = Path::new(&table.path).components()
            .filter(|component| !matches!(component, std::path::Component::CurDir))
            .collect::<std::path::PathBuf>();
        if !paths.insert(path) {
            return Err(invalid(format!("duplicate chunk path {}", table.path)));
        }
        if table.rows > 0 && table.chunks == 0 {
            return Err(invalid(format!("table {} has rows but no chunks", table.name)));
        }
    }
    for name in selected {
        if !names.contains(name.as_str()) {
            return Err(invalid(format!("unknown selected table {name}")));
        }
    }
    let mut schema = BTreeMap::new();
    for table in &manifest.schema.tables {
        if schema.insert(table.name.as_str(), table).is_some() {
            return Err(invalid(format!("duplicate schema table {}", table.name)));
        }
    }
    if selected.is_empty() && manifest.source_engine == target_engine {
        for view in &manifest.views {
            let sanitized = sanitize_view_definition(&view.definition, manifest.source_schema.as_deref().unwrap_or(&manifest.database), target_engine);
            validate_import_view_target(&sanitized, &view.name, target_engine)?;
        }
    }
    let mut ddls = BTreeMap::new();
    for table in &manifest.tables {
        let definition = schema.get(table.name.as_str())
            .ok_or_else(|| invalid(format!("manifest schema missing table {}", table.name)))?;
        if selected.is_empty() || selected.contains(&table.name) {
            if definition.columns.is_empty() {
                return Err(invalid(format!("table {} has no columns", table.name)));
            }
            let mut column_names = BTreeSet::new();
            for column in &definition.columns {
                if !column_names.insert(&column.name) {
                    return Err(invalid(format!("duplicate column {} in table {}", column.name, table.name)));
                }
            }
            validate_target_foreign_key_actions(definition, target_engine).map_err(&invalid)?;
            let ddl = generate_table_ddl(definition, &manifest.source_engine, target_engine)
                .ok_or_else(|| invalid(format!("cannot generate DDL for table {}", table.name)))?;
            ddls.insert(table.name.clone(), ddl);
        }
    }
    Ok(ddls)
}

fn source_dump_warnings(manifest: &DumpManifest) -> Vec<String> {
    let mut warnings = manifest.manifest_warnings.iter()
        .map(|warning| format!("source dump: {warning}")).collect::<Vec<_>>();
    if manifest.snapshot_policy == "mysql_parallel_no_backup_lock_consistent_snapshot" {
        warnings.push("legacy source dump used independent snapshots; cross-table and cross-chunk point-in-time consistency is not guaranteed, regardless of its strict_export flag".into());
    }
    if !manifest.strict_export {
        warnings.push("source dump is not marked strict; its recorded fidelity and consistency limitations remain after import".into());
    }
    if manifest.source_timezone.is_none() {
        warnings.push("legacy source dump does not record its source timezone; timestamp interpretation follows the selected import timezone or target server default".into());
    }
    warnings
}

struct ImportJournal {
    input_path: std::path::PathBuf,
    target: Value,
    mode: String,
    phase: String,
    planned: Vec<String>,
    load_attempted: BTreeSet<String>,
    dropped: Vec<String>,
    loaded: Vec<String>,
    current_table: Option<String>,
    pending_drop: Option<String>,
    dropped_foreign_keys: Vec<Value>,
    rows_imported: u64,
    chunks_imported: u64,
    safe_context: Option<Value>,
}

impl ImportJournal {
    fn report(&self, status: &str, error: Option<&str>) -> Value {
        let failed = if error.is_some() { self.current_table.iter().cloned().collect::<Vec<_>>() } else { Vec::new() };
        let unattempted = self.planned.iter().filter(|name| !self.load_attempted.contains(*name) && !failed.contains(*name)).collect::<Vec<_>>();
        let mut report = json!({
            "success": false, "status": status, "mode": self.mode, "target": self.target,
            "phase": self.phase, "planned_tables": self.planned,
            "dropped_tables": self.dropped, "data_loaded_tables": self.loaded,
            "dropped_not_restored_tables": self.dropped.iter().filter(|name| !self.loaded.contains(*name)).collect::<Vec<_>>(),
            "dropped_foreign_keys": self.dropped_foreign_keys,
            "failed_tables": failed, "unattempted_tables": unattempted,
            "pending_drop_table": self.pending_drop,
            "drop_outcome_unknown": self.pending_drop.is_some(),
            "current_table": self.current_table, "error": error,
            "rows_imported": self.rows_imported, "chunks_imported": self.chunks_imported,
            "post_load_completed": false,
            "data_imported": !self.planned.is_empty() && self.loaded.len() == self.planned.len(),
            "atomic_import": false,
            "partial_data_may_exist": self.current_table.is_some() && self.phase == "dump_import_data",
            "updated_unix_seconds": current_unix_seconds(),
        });
        if let Some(context) = &self.safe_context {
            for (key, value) in context.as_object().into_iter().flatten() { report[key] = value.clone(); }
            report["mode"] = json!("safe");
            report["candidate_import_mode"] = json!(self.mode);
            report["status"] = json!(if status == "failed" { "failed_original_untouched" } else { "staging" });
        }
        report
    }

    fn checkpoint(&self) -> Result<(), String> {
        write_dump_import_report(&self.input_path, &self.report("running", None))
    }
}

fn dump_import<F: FnMut(Value)>(request: &Request, mut emit: F) -> Result<Value, String> {
    let mode = request.payload.get("mode").or_else(|| request.payload.get("import_mode"))
        .map(|value| value.as_str().ok_or("dump import mode must be a string")).transpose()?.unwrap_or("safe");
    if mode == "safe" { return safe_restore::run(request, emit); }
    dump_import_with_context(request, None, |event| emit(event))
}

fn dump_import_with_context<F: FnMut(Value)>(request: &Request, safe_context: Option<Value>, mut emit: F) -> Result<Value, String> {
    let endpoint = request_endpoint(request)?;
    let input_dir = request.payload.get("input_dir").and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty()).ok_or("dump.import requires input_dir")?;
    let mode = request.payload.get("mode").or_else(|| request.payload.get("import_mode"))
        .and_then(Value::as_str).unwrap_or("replace");
    let mut journal = ImportJournal {
        input_path: Path::new(input_dir).to_path_buf(),
        target: json!({"engine": endpoint.engine, "host": endpoint.host, "port": endpoint.port,
            "database": endpoint.database, "schema": endpoint_schema(&endpoint)}),
        mode: mode.into(), phase: "dump_import_validation".into(), planned: Vec::new(),
        load_attempted: BTreeSet::new(), dropped: Vec::new(), loaded: Vec::new(),
        current_table: None, pending_drop: None, dropped_foreign_keys: Vec::new(), rows_imported: 0, chunks_imported: 0,
        safe_context,
    };
    // Replacing a prior success report is a prerequisite for starting a new attempt.
    journal.checkpoint()?;
    let report_path = dump_import_report_path(&journal.input_path)?.display().to_string();
    emit(json!({"event":"import_report", "request_id":request.request_id, "status":"running", "report_path":report_path, "target":journal.target}));
    let result = dump_import_attempt(request, &mut journal, |event| emit(event))
        .map_err(|error| redact_endpoint_secret(&error, &endpoint));
    let mut report = match &result {
        Ok(result) => {
            let mut report = journal.report(result["status"].as_str().unwrap_or("completed"), None);
            for key in ["success", "status", "data_imported", "message", "mode", "tables", "rows_imported", "chunks_imported", "imported_rows_by_table", "verification", "views_imported", "views_failed", "views_skipped_cross_engine"] {
                if let Some(value) = result.get(key) { report[key] = value.clone(); }
            }
            report["post_load_completed"] = result["success"].clone();
            report
        }
        Err(error) => journal.report("failed", Some(error)),
    };
    if journal.safe_context.is_some() && result.is_ok() {
        report["status"] = json!("staging");
        report["success"] = json!(false);
        report["candidate_import_completed"] = result.as_ref().unwrap()["success"].clone();
        report["mode"] = json!("safe");
    }
    if let Err(error) = write_dump_import_report(&journal.input_path, &report) {
        emit(json!({"event":"import_report", "request_id":request.request_id, "status":"failed", "report_path":report_path, "report_write_failed":true, "message":error}));
        return Err(match result { Err(original) => format!("{original}; failure report could not be persisted: {error}"), Ok(_) => format!("Import data was applied, but its completion report could not be persisted: {error}") });
    }
    let mut summary = json!({"event":"import_report", "request_id":request.request_id, "status":report["status"], "report_path":report_path});
    for key in ["phase", "dropped_tables", "dropped_not_restored_tables", "failed_tables", "unattempted_tables", "dropped_foreign_keys", "drop_outcome_unknown", "post_load_completed"] {
        summary[key] = report[key].clone();
    }
    emit(summary);
    result
}

fn dump_import_attempt<F: FnMut(Value)>(request: &Request, journal: &mut ImportJournal, mut emit: F) -> Result<Value, String> {
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
        return Err(format!("unsupported dump import mode: {mode}"));
    }

    let input_path = Path::new(input_dir);
    let manifest = read_dump_manifest(input_path)?;
    if manifest.format != "tunnelforge-dump" || !matches!(manifest.format_version, 1 | 2 | 3 | 4) {
        return Err("unsupported dump manifest format".to_string());
    }
    let legacy_bit_text = manifest.format_version < 4 && manifest.source_engine == "mysql";
    let data_format = manifest.data_format.to_ascii_lowercase();
    if !matches!(data_format.as_str(), "jsonl" | "tsv") {
        return Err(format!("unsupported dump data_format: {data_format}"));
    }
    let compression = manifest.compression.to_ascii_lowercase();
    if !matches!(compression.as_str(), "none" | "zstd") {
        return Err(format!("unsupported dump compression: {compression}"));
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
    let timezone_sql = import_timezone_sql(&request.payload, manifest.source_timezone.as_deref(), &endpoint.engine)?;
    let table_ddls = validate_import_metadata(&manifest, &selected, &endpoint.engine)?;
    let tables: Vec<DumpTableManifest> = manifest
        .tables
        .iter()
        .filter(|table| selected.is_empty() || selected.contains(&table.name))
        .cloned()
        .collect();
    if tables.is_empty() {
        return Err("dump.import found no tables to import".to_string());
    }

    let strict_manifest = request
        .payload
        .get("strict_manifest")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let mut manifest_warnings = validate_dump_import_manifest_strictness(&tables, strict_manifest, &data_format, &compression)?;
    for warning in source_dump_warnings(&manifest) {
        if !manifest_warnings.contains(&warning) { manifest_warnings.push(warning); }
    }
    let tables = dependency_ordered_dump_tables(&manifest.schema, tables);
    journal.planned = tables.iter().map(|table| table.name.clone()).collect();
    journal.checkpoint()?;
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump_import_validation",
        "message": "Import 데이터 검증 중: 대상 테이블 변경 전에 청크 무결성과 행 데이터를 확인합니다.",
        "tables": tables.len(),
    }));
    validate_dump_manifest_chunks(input_path, &tables, &data_format, &compression, &manifest.schema)?;
    for warning in &manifest_warnings {
        emit(json!({
            "event": "warning",
            "request_id": request.request_id,
            "phase": "dump_import_manifest",
            "classification": "dump_manifest",
            "message": warning
        }));
    }
    let selected_table_names = tables
        .iter()
        .map(|table| table.name.as_str())
        .collect::<BTreeSet<_>>();
    let import_schema = NormalizedSchema {
        tables: manifest
            .schema
            .tables
            .iter()
            .filter(|table| selected_table_names.contains(table.name.as_str()))
            .cloned()
            .collect(),
    };
    let mut adapter = LiveAdapter::connect(&endpoint)?;
    let local_infile_restore = prepare_mysql_local_infile_policy(
        &mut adapter,
        &endpoint,
        mysql_local_infile_policy,
        request.request_id.clone(),
        &mut emit,
    )?;
    let table_total = tables.len();
    let overall_rows_total = tables.iter().map(|table| table.rows).sum::<u64>();
    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;
    let mut imported_rows_by_table: BTreeMap<String, u64> = BTreeMap::new();

    let import_result = (|| -> Result<(), String> {
        set_mysql_import_session_tuning(&mut adapter, false)?;
        if mode == "merge" && endpoint.engine == "mysql" {
            adapter.execute_sql("SET SESSION foreign_key_checks=1")?;
        }
        if let Some(sql) = timezone_sql.as_deref() {
            adapter.execute_sql(sql)?;
        }
        if endpoint.engine == "postgresql" {
            adapter.execute_sql("SET DateStyle = 'ISO, YMD'")?;
        }

        let target_schema = endpoint_schema(&endpoint);
        journal.phase = "dump_import_ddl_preflight".into();
        journal.checkpoint()?;
        emit(json!({"event":"phase", "request_id":request.request_id, "phase":journal.phase,
            "message":"서버에서 테이블 생성 정의를 검증 중입니다. 기존 대상 테이블은 아직 변경하지 않습니다."}));
        if matches!(mode, "replace" | "recreate") {
            validate_postgres_drop_dependencies(&mut adapter, &target_schema, &tables)?;
        }
        validate_target_check_names(&mut adapter, &target_schema, &import_schema)?;
        probe_import_ddl(&mut adapter, &import_schema, &manifest.source_engine, journal)?;
        journal.current_table = None;
        journal.phase = "dump_import_prepare".into();
        journal.checkpoint()?;
        emit(json!({"event":"phase", "request_id":request.request_id, "phase":journal.phase,
            "message":if mode == "merge" { "검증 완료: 기존 테이블을 유지하며 데이터 적재를 준비합니다." } else { "검증 완료: 선택한 대상 테이블 교체를 준비합니다." },
            "report_path":dump_import_report_path(input_path)?.display().to_string()}));
        prepare_import_target(
            mode,
            &tables,
            &import_schema,
            &mut adapter,
            &target_schema,
        )?;

        let planned_set = tables.iter().map(|table| table.name.clone()).collect::<BTreeSet<_>>();
        for (index, table_manifest) in tables.iter().enumerate() {
            let table = manifest
                .schema
                .tables
                .iter()
                .find(|table| table.name == table_manifest.name)
                .ok_or_else(|| format!("manifest schema missing table {}", table_manifest.name))?;
            emit(json!({
                "event": "table_progress",
                "request_id": request.request_id,
                "table": table.name,
                "status": "importing",
                "current": index + 1,
                "total": table_total
            }));
            let ddl = &table_ddls[&table.name];
            journal.current_table = Some(table.name.clone());
            journal.load_attempted.insert(table.name.clone());
            journal.phase = "dump_import_create".into();
            journal.checkpoint()?;
            if matches!(mode, "replace" | "recreate") {
                // mysqldump처럼 테이블마다 DROP 직후 CREATE한다. 실패해도 이 테이블만
                // 비고, 아직 차례가 오지 않은 테이블은 원본 데이터를 유지한다.
                replace_target_table(&mut adapter, &target_schema, &table.name, &planned_set,
                    journal, request.request_id.clone(), &mut emit)?;
                adapter
                    .create_new_table(ddl)
                    .map_err(|err| dump_import_ddl_error("create_table", &table.name, &err))?;
            } else {
                adapter
                    .create_table(table, ddl)
                    .map_err(|err| dump_import_ddl_error("create_table", &table.name, &err))?;
            }

            journal.phase = "dump_import_data".into();
            journal.checkpoint()?;
            let (table_rows, table_chunks) = import_table_rows(
                &endpoint,
                &mut adapter,
                input_path,
                table,
                table_manifest,
                &data_format,
                &compression,
                mode,
                legacy_bit_text,
                timezone_sql.as_deref(),
                threads,
                request.request_id.clone(),
                rows_imported,
                overall_rows_total,
                |event| {
                    if event.get("event").and_then(Value::as_str) == Some("row_progress") {
                        if let Some(rows) = event.get("table_rows_done").and_then(Value::as_u64) { journal.rows_imported = rows_imported + rows; }
                        if let Some(chunks) = event.get("chunks_done").and_then(Value::as_u64) { journal.chunks_imported = chunks_imported + chunks; }
                    }
                    emit(event);
                },
            )?;
            rows_imported += table_rows;
            chunks_imported += table_chunks;
            imported_rows_by_table.insert(table.name.clone(), table_rows);
            journal.loaded.push(table.name.clone());
            journal.rows_imported = rows_imported;
            journal.chunks_imported = chunks_imported;
            journal.current_table = None;
            journal.checkpoint()?;
            emit(json!({
                "event": "table_progress",
                "request_id": request.request_id,
                "table": table.name,
                "status": "completed",
                "current": index + 1,
                "total": table_total
            }));
        }
        Ok(())
    })();
    let restore_result = set_mysql_import_session_tuning(&mut adapter, true);
    let local_infile_restore_result = restore_mysql_local_infile_policy(
        &mut adapter,
        &endpoint,
        local_infile_restore,
        request.request_id.clone(),
        &mut emit,
    );
    let errors = [import_result, restore_result, local_infile_restore_result]
        .into_iter().filter_map(Result::err).collect::<Vec<_>>();
    if !errors.is_empty() {
        return Err(errors.join("; additionally: "));
    }

    journal.phase = "dump_import_post_load".into();
    journal.current_table = None;
    journal.checkpoint()?;
    finalize_dump_import(
        &mut adapter,
        &manifest,
        &import_schema,
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
        |event| {
            if let Some(phase) = event.get("phase").and_then(Value::as_str) { journal.phase = phase.into(); }
            emit(event);
        },
    )
}

fn probe_import_ddl(
    adapter: &mut LiveAdapter, schema: &NormalizedSchema, source_engine: &str, journal: &mut ImportJournal,
) -> Result<(), String> {
    let nonce = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .map_err(|err| format!("cannot allocate DDL probe name: {err}"))?.as_nanos();
    for (index, table) in schema.tables.iter().enumerate() {
        journal.current_table = Some(table.name.clone());
        journal.checkpoint()?;
        let mut probe = table.clone();
        probe.name = format!("tf_probe_{}_{nonce}_{index}", std::process::id());
        for (check_index, check) in probe.checks.iter_mut().enumerate() {
            check.name = format!("{}_c{check_index}", probe.name);
        }
        let ddl = generate_table_ddl(&probe, source_engine, adapter.engine())
            .ok_or_else(|| format!("DDL probe generation failed for {}", table.name))?;
        // Raw execution must fail on collisions; create_table intentionally treats
        // existing tables as valid during merge and cannot establish ownership.
        let drop_sql = drop_table_sql(adapter.engine(), &probe.name);
        execute_owned_ddl_probe(&ddl, &drop_sql, &table.name, &probe.name, |sql| adapter.execute_sql(sql))?;
    }
    Ok(())
}

fn execute_owned_ddl_probe(
    create_sql: &str, drop_sql: &str, original_name: &str, probe_name: &str,
    mut execute: impl FnMut(&str) -> Result<(), String>,
) -> Result<(), String> {
    execute(create_sql).map_err(|err| classified_import_error(
        "ddl_preflight_failed", &format!("server rejected CREATE TABLE before target replacement: {err}"), Some(original_name),
    ))?;
    execute(drop_sql).map_err(|err| classified_import_error(
        "ddl_probe_cleanup_failed", &format!("owned probe {probe_name} could not be removed; original target tables were not changed: {err}"), Some(original_name),
    ))
}

/// replace/recreate 모드일 때 대상 테이블을 바꾸기 전에 막을 수 있는 실패를 먼저 거부한다.
///
/// Surviving-FK preflight (MySQL 전용): import set 밖의 타겟 테이블과 그 FK는 그대로 둔다.
/// 새 부모 정의의 타입/charset/collation/생성 시점 키가 기존 FK 계약과 달라지는 경우만
/// 타겟을 손대기 전에 명확한 에러로 차단한다. 실제 DROP은 import 루프에서 테이블마다
/// CREATE 직전에 수행한다(`replace_target_table`).
///
/// merge 모드에서는 기존 MySQL 대상의 트랜잭션 지원 여부만 확인한다.
fn prepare_import_target(
    mode: &str,
    tables: &[DumpTableManifest],
    import_schema: &NormalizedSchema,
    adapter: &mut LiveAdapter,
    target_schema: &str,
) -> Result<(), String> {
    if !matches!(mode, "replace" | "recreate") {
        if let LiveAdapter::MySql(conn) = adapter {
            for table in tables {
                let engine: Option<String> = conn.exec_first(
                    "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=?",
                    (target_schema, &table.name),
                ).map_err(|err| format!("mysql merge engine preflight failed: {err}"))?;
                if engine.is_some_and(|engine| !engine.eq_ignore_ascii_case("InnoDB")) {
                    return Err(format!("import_plan_invalid: merge requires an InnoDB target table for safe rollback: {}", table.name));
                }
            }
        }
        return Ok(());
    }
    // Target-only children are outside the user's replacement set. Refuse an
    // incompatible parent definition before dropping any selected table.
    let incompatible_fks =
        detect_incompatible_surviving_fks(adapter, target_schema, import_schema)?;
    if !incompatible_fks.is_empty() {
        return Err(classified_import_error(
            "incompatible_surviving_fk",
            &format!("target-only foreign keys are incompatible with replacement tables; target tables were not changed: {}", incompatible_fks.join(", ")),
            None,
        ));
    }

    Ok(())
}

/// 테이블 하나를 교체 가능한 상태로 만든다. import set 안의 다른(아직 원본인) 테이블이 이
/// 테이블을 참조하는 FK만 먼저 떼고 테이블을 DROP한다. 그 자식들도 차례가 오면 교체되고
/// post-load가 덤프 정의로 FK를 다시 만든다. 떼지 않으면 부모를 다시 만들 때 원본 자식의
/// FK가 다시 묶이며 ERROR 3780/6125(MySQL)나 DROP 거부(PostgreSQL)가 난다.
fn replace_target_table<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    target_schema: &str,
    table: &str,
    planned: &BTreeSet<String>,
    journal: &mut ImportJournal,
    request_id: Option<String>,
    emit: &mut F,
) -> Result<(), String> {
    if !import_target_table_exists(adapter, target_schema, table)? {
        return Ok(());
    }
    let report_path = dump_import_report_path(&journal.input_path)?.display().to_string();
    for (child, constraint) in incoming_import_set_foreign_keys(adapter, target_schema, table, planned)? {
        let engine = adapter.engine();
        let drop_keyword = if engine == "mysql" { "FOREIGN KEY" } else { "CONSTRAINT" };
        let sql = format!("ALTER TABLE {} DROP {drop_keyword} {}", quote_ident(engine, &child), quote_ident(engine, &constraint));
        adapter.execute_sql(&sql)
            .map_err(|err| dump_import_ddl_error("drop_foreign_key", &format!("{child}.{constraint}"), &err))?;
        journal.dropped_foreign_keys.push(json!({"table": child, "constraint": constraint, "referenced_table": table}));
        journal.checkpoint()?;
        emit(json!({"event":"target_change", "request_id":request_id, "phase":journal.phase,
            "action":"drop_foreign_key", "status":"completed", "table":child, "constraint":constraint,
            "report_path":report_path}));
    }
    journal.pending_drop = Some(table.to_string());
    journal.checkpoint()?;
    adapter
        .execute_sql(&drop_table_sql(adapter.engine(), table))
        .map_err(|err| dump_import_ddl_error("drop_table", table, &err))?;
    journal.dropped.push(table.to_string());
    journal.pending_drop = None;
    journal.checkpoint()?;
    emit(json!({"event":"target_change", "request_id":request_id, "phase":journal.phase,
        "action":"drop_table", "status":"completed", "table":table, "report_path":report_path}));
    Ok(())
}

/// `parent`를 참조하는 같은 스키마 import set 테이블의 FK `(child, constraint)` 목록.
/// 자기 참조 FK는 테이블과 함께 사라지므로 제외한다.
fn incoming_import_set_foreign_keys(
    adapter: &mut LiveAdapter, schema: &str, parent: &str, planned: &BTreeSet<String>,
) -> Result<Vec<(String, String)>, String> {
    let mut fold_case = false;
    let rows: Vec<(String, String)> = match adapter {
        LiveAdapter::MySql(conn) => {
            fold_case = mysql_folds_table_names(conn)?;
            conn.exec(
                "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS \
                 WHERE CONSTRAINT_SCHEMA=? AND UNIQUE_CONSTRAINT_SCHEMA=? AND REFERENCED_TABLE_NAME=? AND TABLE_NAME<>? \
                 ORDER BY TABLE_NAME, CONSTRAINT_NAME",
                (schema, schema, parent, parent),
            ).map_err(|err| format!("target foreign key inspection failed: {err}"))?
        }
        LiveAdapter::PostgreSql(client) => client.query(
            "SELECT child.relname::text, c.conname::text FROM pg_constraint c \
             JOIN pg_class child ON child.oid=c.conrelid JOIN pg_namespace cn ON cn.oid=child.relnamespace \
             JOIN pg_class parent ON parent.oid=c.confrelid JOIN pg_namespace pn ON pn.oid=parent.relnamespace \
             WHERE c.contype='f' AND c.conparentid=0 AND pn.nspname=$1 AND cn.nspname=$1 AND parent.relname=$2 AND child.oid<>parent.oid \
             ORDER BY 1, 2",
            &[&schema, &parent],
        ).map_err(|err| format!("target foreign key inspection failed: {err}"))?
            .iter().map(|row| (row.get(0), row.get(1))).collect(),
    };
    // Partition clones (conparentid<>0) go away with their root constraint.
    Ok(rows.into_iter().filter(|(child, _)| {
        planned.contains(child) || fold_case && planned.iter().any(|name| name.eq_ignore_ascii_case(child))
    }).collect())
}

fn import_target_table_exists(adapter: &mut LiveAdapter, schema: &str, table: &str) -> Result<bool, String> {
    match adapter {
        LiveAdapter::MySql(conn) => conn.exec_first::<u64, _, _>(
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND TABLE_TYPE='BASE TABLE'",
            (schema, table),
        ).map(|count| count.unwrap_or(0) > 0).map_err(|err| format!("target table inspection failed: {err}")),
        LiveAdapter::PostgreSql(client) => client.query_one(
            "SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname=$2 AND c.relkind IN ('r','p'))",
            &[&schema, &table],
        ).map(|row| row.get::<_, bool>(0)).map_err(|err| format!("target table inspection failed: {err}")),
    }
}

fn validate_postgres_drop_dependencies(adapter: &mut LiveAdapter, schema: &str, tables: &[DumpTableManifest]) -> Result<(), String> {
    let LiveAdapter::PostgreSql(client)=adapter else { return Ok(()); };
    let names=tables.iter().map(|table|table.name.clone()).collect::<Vec<_>>();
    let fail=|detail:String|classified_import_error("target_dependency_preflight_failed",&format!("PostgreSQL target dependencies prevent replacement; no target tables were changed. Use safe restore. {detail}"),None);
    let views=client.query(
        "SELECT DISTINCT vn.nspname,v.relname FROM pg_depend d JOIN pg_rewrite r ON r.oid=d.objid JOIN pg_class v ON v.oid=r.ev_class JOIN pg_namespace vn ON vn.oid=v.relnamespace JOIN pg_class source ON source.oid=d.refobjid JOIN pg_namespace sn ON sn.oid=source.relnamespace WHERE d.classid='pg_rewrite'::regclass AND d.refclassid='pg_class'::regclass AND v.relkind IN ('v','m') AND v.oid<>source.oid AND sn.nspname=$1 AND source.relname=ANY($2::text[])",
        &[&schema,&names],
    ).map_err(|err|fail(format!("Dependency inspection failed: {err}")))?;
    if !views.is_empty() {
        let names=views.iter().map(|row|format!("{}.{}",row.get::<_,String>(0),row.get::<_,String>(1))).collect::<Vec<_>>();
        return Err(fail(format!("Dependent views: {}",names.join(", "))));
    }
    let planned=tables.iter().map(|table|table.name.as_str()).collect::<BTreeSet<_>>();
    let keys=client.query(
        "SELECT child_ns.nspname,child.relname,parent.relname,c.conname FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace JOIN pg_class parent ON parent.oid=c.confrelid JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace WHERE c.contype='f' AND parent_ns.nspname=$1 AND parent.relname=ANY($2::text[])",
        &[&schema,&names],
    ).map_err(|err|fail(format!("Foreign key inspection failed: {err}")))?;
    for row in keys {
        let child_schema:String=row.get(0);let child:String=row.get(1);let parent:String=row.get(2);let constraint:String=row.get(3);
        // Import-set children lose this FK just before the parent is replaced
        // (replace_target_table); only target-only children would block DROP.
        if child_schema!=schema || !planned.contains(child.as_str()) {
            return Err(fail(format!("Target-only foreign key {child_schema}.{child}.{constraint} references {parent}")));
        }
    }
    Ok(())
}

/// 단일 테이블의 데이터를 적재한다. MySQL TSV fast-path(LOAD DATA / 병렬 / fallback)와
/// 엔진 무관 generic 청크 INSERT 경로를 분기하고, 이 테이블에 적재한 (rows, chunks)를 반환한다.
/// 테이블 생성(DDL)과 진행률 table_progress 이벤트는 호출자가 담당한다.
/// Rewrites BIT cells of a pre-version-4 MySQL dump (raw bytes read as text) into binary digits.
fn convert_legacy_bit_cells(table: &NormalizedTable, rows: &mut [Value]) -> Result<(), String> {
    let bits = table.columns.iter()
        .filter_map(|column| mysql_bit_width(&column.type_name).map(|width| (column.name.as_str(), width)))
        .collect::<Vec<_>>();
    if bits.is_empty() {
        return Ok(());
    }
    for row in rows.iter_mut() {
        let Some(object) = row.as_object_mut() else { continue };
        for (name, width) in &bits {
            if let Some(Value::String(text)) = object.get(*name) {
                let digits = legacy_mysql_bit_digits(text, *width)
                    .map_err(|err| classified_import_error("load_failed", &format!("BIT column {name}: {err}"), Some(&table.name)))?;
                object.insert((*name).to_string(), Value::String(digits));
            }
        }
    }
    Ok(())
}

fn import_table_rows<F: FnMut(Value)>(
    endpoint: &Endpoint,
    adapter: &mut LiveAdapter,
    input_path: &Path,
    table: &NormalizedTable,
    table_manifest: &DumpTableManifest,
    data_format: &str,
    compression: &str,
    mode: &str,
    legacy_bit_text: bool,
    timezone_sql: Option<&str>,
    threads: usize,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
    mut emit: F,
) -> Result<(u64, u64), String> {
    if data_format == "tsv" && !has_binary_columns(table) {
        if let LiveAdapter::MySql(conn) = adapter {
            let chunk_ctx = MysqlImportChunkContext {
                table,
                table_manifest,
                compression,
                mode,
                timezone_sql,
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
                |event| emit(event),
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
        let mut rows = read_dump_rows(&chunk_path, table, data_format, compression)?;
        if legacy_bit_text {
            convert_legacy_bit_cells(table, &mut rows)?;
        }
        let row_count = rows.len() as u64;
        if let LiveAdapter::MySql(conn) = adapter {
            let enum_zero = enum_zero_cells(table, &rows);
            if enum_zero.len() > MYSQL_IMPORT_MAX_WARNINGS {
                for batch in rows.chunks(enum_warning_batch_rows(table)) {
                    let cells = enum_zero_cells(table, batch);
                    execute_mysql_import_statement(conn, &insert_rows_literal_sql_for_table("mysql", table, batch), table, &cells)?;
                }
            } else {
                execute_mysql_import_statement(conn, &insert_rows_literal_sql_for_table("mysql", table, &rows), table, &enum_zero)?;
            }
        } else {
            adapter.insert_rows(table, rows)?;
        }
        table_rows += row_count;
        table_chunks += 1;
        emit(dump_import_row_progress_event(
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
        ));
    }
    Ok((table_rows, table_chunks))
}

/// import 데이터 적재 완료 후의 마무리 단계: post-load DDL(인덱스/FK) 적용,
/// 적재 행수 검증, View 생성(best-effort), 리포트 기록 후 최종 result JSON을 만든다.
fn finalize_dump_import<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    manifest: &DumpManifest,
    import_schema: &NormalizedSchema,
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
    mut emit: F,
) -> Result<Value, String> {
    let input_path = Path::new(input_dir);
    let target_engine = adapter.engine().to_string();
    if should_apply_post_load_ddl(mode) {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import_post_load",
            "message": "현재 단계: 인덱스/FK 생성 중 - 데이터 Import는 완료, 후처리 진행 중",
            "strategy": "post_load_ddl"
        }));
        apply_post_load_ddl(adapter, import_schema, &target_engine)?;
    } else {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import_post_load",
            "message": post_load_ddl_skip_message(mode),
            "strategy": "existing_schema"
        }));
    }
    // import가 실제로 적재한 행 수가 덤프와 일치하는지만 검증한다(적재 정확성).
    // 타겟 DB를 다시 세는 검증(verify_target_row_counts)은 하지 않는다 — 타겟이
    // 살아있는 DB면 import 동안 외부 write(예: login_attempts에 새 로그인 시도)로
    // row 수가 정상적으로 달라질 수 있어, 정확 일치를 요구하면 오탐으로 실패한다.
    // (foreign_key_checks=0/unique_checks=0으로 관용 적재하는 정책과도 일관.)
    verify_imported_row_counts(tables, imported_rows_by_table)?;

    // Table data is already committed. View failures produce a partial result,
    // without suggesting that the restored rows were rolled back.
    // 전체 import(테이블 부분 선택 없음)일 때만 시도한다 — 부분 import면 View가 참조하는 base table이 없을 수 있다.
    let view_outcome = if selected.is_empty() && !manifest.views.is_empty() {
        import_views(
            adapter,
            manifest,
            &target_engine,
            mode,
            request_id.clone(),
            &mut emit,
        )
    } else {
        ViewImportOutcome::default()
    };
    let import_complete = view_outcome.failed.is_empty();
    let status = if import_complete { "completed" } else { "partial" };
    let message = if import_complete {
        "Import completed".to_string()
    } else {
        format!("Table data imported, but {} view(s) failed to restore; imported rows remain committed", view_outcome.failed.len())
    };
    let import_report = json!({
        "success": import_complete,
        "status": status,
        "data_imported": true,
        "message": message,
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
    let import_report_path = dump_import_report_path(input_path)?;

    Ok(json!({
        "event": "result",
        "request_id": request_id,
        "command": "dump.import",
        "success": import_complete,
        "status": status,
        "data_imported": true,
        "message": import_report["message"].clone(),
        "input_dir": input_dir,
        "mode": mode,
        "tables": table_total,
        "rows_imported": rows_imported,
        "chunks_imported": chunks_imported,
        "imported_rows_by_table": imported_rows_by_table,
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
fn import_views<A: MigrationAdapter, F: FnMut(Value)>(
    adapter: &mut A,
    manifest: &DumpManifest,
    target_engine: &str,
    mode: &str,
    request_id: Option<String>,
    mut emit: F,
) -> ViewImportOutcome {
    let mut outcome = ViewImportOutcome::default();

    if manifest.source_engine != target_engine {
        outcome.skipped_cross_engine = manifest.views.iter().map(|v| v.name.clone()).collect();
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import_views",
            "message": format!(
                "크로스 엔진 import: View {}개는 정의 비호환으로 건너뜁니다 ({} -> {})",
                outcome.skipped_cross_engine.len(),
                manifest.source_engine,
                target_engine
            ),
        }));
        return outcome;
    }

    // 정화 + 단일 CREATE VIEW 문 검증. 검증 실패한 정의는 실행하지 않고 즉시 failed로 보고한다.
    // (변조된 manifest가 multi-statement SQL 체인을 심는 것을 차단 — 특히 PostgreSQL batch_execute 경로)
    let mut pending: Vec<(String, String)> = Vec::with_capacity(manifest.views.len());
    let mut validated_names: Vec<&str> = Vec::with_capacity(manifest.views.len());
    for view in &manifest.views {
        let sanitized =
            sanitize_view_definition(&view.definition, manifest.source_schema.as_deref().unwrap_or(&manifest.database), target_engine);
        // shape 검증(단일 CREATE ... VIEW 문) + MySQL DEFINER/SQL SECURITY 잔존 fail-closed.
        let validation = validate_import_view_target(&sanitized, &view.name, target_engine).and_then(|()| {
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
                emit(json!({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "dump_import_views",
                    "message": format!("View '{}' 거부됨 (안전하지 않은 정의): {reason}", view.name),
                }));
            }
        }
    }

    // replace/recreate 모드면 기존 View를 먼저 정리한다 (테이블이 아닌 View 전용 DROP).
    // 검증을 통과한 View만 DROP 대상으로 삼는다.
    if matches!(mode, "replace" | "recreate") {
        for name in &validated_names {
            let _ = adapter.execute_sql(&drop_view_sql(target_engine, name));
        }
    }

    // fixpoint 루프: 한 바퀴에 하나도 성공하지 못하면 중단한다.
    let mut last_errors: BTreeMap<String, String> = BTreeMap::new();
    loop {
        let mut progressed = false;
        let mut still_pending: Vec<(String, String)> = Vec::new();
        for (name, sql) in pending.drain(..) {
            match adapter.execute_sql(&sql) {
                Ok(()) => {
                    progressed = true;
                    last_errors.remove(&name);
                    outcome.imported.push(name.clone());
                    emit(json!({
                        "event": "table_progress",
                        "request_id": request_id,
                        "table": name,
                        "status": "completed",
                        "kind": "view"
                    }));
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
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import_views",
            "message": format!(
                "View {}개 생성 성공, {}개 실패 (데이터 import는 정상 완료)",
                outcome.imported.len(),
                outcome.failed.len()
            ),
        }));
    }

    outcome
}

/// MySQL TSV import 경로(fast-path / parallel / insert fallback)가 공통으로
/// 전달받던 6개 인자 클러스터를 묶는다. 테이블 정의·매니페스트·압축·요청 ID·
/// 전체 진행률 기준선을 한 번에 관통시켜 시그니처 부풀림을 줄인다.
#[derive(Clone)]
struct MysqlImportChunkContext<'a> {
    mode: &'a str,
    timezone_sql: Option<&'a str>,
    table: &'a NormalizedTable,
    table_manifest: &'a DumpTableManifest,
    compression: &'a str,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
}

fn may_restart_import_table(mode: &str, completed_attempts: u32) -> bool {
    matches!(mode, "replace" | "recreate") && completed_attempts < 2
}

fn import_mysql_tsv_table<F: FnMut(Value)>(
    endpoint: &Endpoint,
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    threads: usize,
    ctx: &MysqlImportChunkContext,
    mut emit: F,
) -> Result<(u64, u64), String> {
    if !mysql_local_infile_enabled(conn) {
        emit(json!({
            "event": "phase",
            "request_id": ctx.request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile is disabled; using safe Rust INSERT fallback",
            "strategy": "insert_fallback",
            "performance": "safe_fallback"
        }));
        return import_mysql_tsv_table_insert_fallback(conn, input_path, ctx, emit);
    }

    if threads > 1 && ctx.table_manifest.chunks > 1 {
        // Other workers may already have committed. Never replay all chunks after
        // a worker failure, including local_infile being disabled mid-import.
        return import_mysql_tsv_table_parallel(endpoint, input_path, threads, ctx, emit);
    }

    // A disconnect can hide a committed LOAD DATA. Only a fresh replacement table
    // can be cleared and replayed safely; merge must preserve pre-existing rows.
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
            let rows = match load_mysql_tsv_chunk(
                conn,
                ctx.table,
                &chunk_path,
                ctx.compression,
            ) {
                Ok(rows) => rows,
                Err(err) if chunks_imported == 0 && is_mysql_local_infile_disabled_error(&err) => {
                    emit(json!({
                        "event": "phase",
                        "request_id": ctx.request_id,
                        "phase": "dump_import",
                        "message": "MySQL LOAD DATA LOCAL is disabled; using safe Rust INSERT fallback",
                        "strategy": "insert_fallback",
                        "performance": "safe_fallback"
                    }));
                    return import_mysql_tsv_table_insert_fallback(conn, input_path, ctx, emit);
                }
                Err(err) if is_transient_disconnect_error(&err) => {
                    // 청크 재접속 재시도로도 복구 안 된 지속적 끊김.
                    retryable_table_error = Some(err);
                    break;
                }
                Err(err) => return Err(err),
            };
            rows_imported += rows;
            chunks_imported += 1;
            emit(dump_import_row_progress_event(
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
            ));
        }

        match retryable_table_error {
            None => return Ok((rows_imported, chunks_imported)),
            Some(err) => {
                if !may_restart_import_table(ctx.mode, table_attempt) {
                    return Err(err);
                }
                emit(json!({
                    "event": "phase",
                    "request_id": ctx.request_id,
                    "phase": "dump_import",
                    "message": format!(
                        "연결 끊김으로 테이블 [{}] 재시작 (TRUNCATE 후 재적재)",
                        ctx.table.name
                    ),
                    "strategy": "table_restart"
                }));
                // 재접속 후 TRUNCATE. 새 세션은 튜닝이 초기화되므로 튜닝 적용된 커넥션으로 교체.
                *conn = connect_tuned_mysql_import_conn(endpoint, ctx.timezone_sql, ctx.mode)?;
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

fn prepare_mysql_local_infile_policy<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    endpoint: &Endpoint,
    policy: &str,
    request_id: Option<String>,
    emit: &mut F,
) -> Result<Option<String>, String> {
    if policy != "temporary_global" {
        return Ok(None);
    }
    let previous = {
        let LiveAdapter::MySql(conn) = adapter else {
            return Ok(None);
        };
        let previous = mysql_local_infile_value(conn).unwrap_or_else(|| "ON".to_string());
        if mysql_bool_value_enabled(&previous) {
            emit(json!({
                "event": "phase",
                "request_id": request_id,
                "phase": "dump_import",
                "message": "MySQL local_infile is already enabled; using fast LOAD DATA LOCAL import",
                "strategy": "load_data_local_infile",
                "performance": "fast_path"
            }));
            return Ok(None);
        }

        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile is disabled; trying temporary SET GLOBAL local_infile=ON",
            "strategy": "temporary_local_infile",
            "performance": "fast_path_attempt"
        }));

        if let Err(err) = conn.query_drop(mysql_set_global_local_infile_sql(true)) {
            if is_transient_disconnect_error(&err.to_string()) {
                let cleanup = restore_mysql_local_infile_policy(
                    adapter, endpoint, Some(previous), request_id, emit,
                );
                return Err(match cleanup {
                    Ok(()) => format!("mysql local_infile enable outcome unknown: {err}"),
                    Err(cleanup_err) => format!("mysql local_infile enable outcome unknown: {err}; additionally: {cleanup_err}"),
                });
            }
            emit(json!({
                "event": "phase",
                "request_id": request_id,
                "phase": "dump_import",
                "message": format!("MySQL local_infile temporary enable failed: {err}; using safe Rust INSERT fallback"),
                "strategy": "insert_fallback",
                "performance": "safe_fallback"
            }));
            return Ok(None);
        }
        previous
    };

    if let Err(err) = LiveAdapter::connect(endpoint).map(|new_adapter| *adapter = new_adapter) {
        let cleanup = restore_mysql_local_infile_policy(adapter, endpoint, Some(previous), request_id, emit);
        return Err(match cleanup {
            Ok(()) => err,
            Err(cleanup_err) => format!("{err}; additionally: {cleanup_err}"),
        });
    }
    let enabled = match adapter {
        LiveAdapter::MySql(conn) => mysql_local_infile_enabled(conn),
        LiveAdapter::PostgreSql(_) => false,
    };
    if enabled {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile temporarily enabled; using fast LOAD DATA LOCAL import",
            "strategy": "load_data_local_infile",
            "performance": "fast_path"
        }));
    } else {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile temporary enable did not take effect; using safe Rust INSERT fallback",
            "strategy": "insert_fallback",
            "performance": "safe_fallback"
        }));
    }
    Ok(Some(previous))
}

fn restore_mysql_local_infile_policy<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    endpoint: &Endpoint,
    previous: Option<String>,
    request_id: Option<String>,
    emit: &mut F,
) -> Result<(), String> {
    let Some(previous) = previous else {
        return Ok(());
    };
    let enabled = mysql_bool_value_enabled(&previous);
    let LiveAdapter::MySql(conn) = adapter else {
        return Ok(());
    };
    let sql = mysql_set_global_local_infile_sql(enabled);
    if let Err(first_error) = conn.query_drop(sql) {
        // Cleanup may reconnect, but must never replay an import statement whose
        // commit outcome became unknown when the original connection died.
        let recovery = (|| {
            let mut cleanup_adapter = LiveAdapter::connect(endpoint)?;
            cleanup_adapter.execute_sql(sql)
        })();
        recovery.map_err(|recovery_error| format!(
            "mysql local_infile restore failed; previous value was {previous}: {first_error}; recovery failed: {recovery_error}"
        ))?;
    }
    emit(json!({
        "event": "phase",
        "request_id": request_id,
        "phase": "dump_import",
        "message": format!("MySQL local_infile restored to {previous}"),
        "strategy": "temporary_local_infile_restore"
    }));
    Ok(())
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

fn import_timezone_sql(payload: &Value, source_timezone: Option<&str>, target: &str) -> Result<Option<String>, String> {
    let explicit = validated_timezone_sql(payload.get("timezone_sql").and_then(Value::as_str))?;
    if explicit.is_some() || payload.get("use_source_timezone").and_then(Value::as_bool) == Some(false) {
        return Ok(explicit);
    }
    match source_timezone {
        None => Ok(None),
        Some(timezone) if timezone.eq_ignore_ascii_case("UTC") => Ok(Some(
            if target == "mysql" { "SET SESSION time_zone = '+00:00'" }
            else { "SET TIME ZONE 'UTC'" }.into()
        )),
        Some(timezone) => Err(format!("export_invalid: unsupported source_timezone {timezone}")),
    }
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
            "SET SESSION sql_mode = CONCAT_WS(',', 'STRICT_ALL_TABLES', 'NO_AUTO_VALUE_ON_ZERO', TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', ''), 'NO_ZERO_IN_DATE', ''), 'NO_ZERO_DATE', ''), 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','), ',,', ',')))".to_string(),
            "SET SESSION foreign_key_checks=0".to_string(),
            "SET SESSION unique_checks=1".to_string(),
            "SET SESSION default_storage_engine='InnoDB'".to_string(),
            "SET SESSION max_error_count=65535".to_string(),
            "SET SESSION lc_messages='en_US'".to_string(),
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
fn connect_tuned_mysql_import_conn(endpoint: &Endpoint, timezone_sql: Option<&str>, mode: &str) -> Result<mysql::PooledConn, String> {
    let mut adapter = LiveAdapter::connect(endpoint)?;
    set_mysql_import_session_tuning(&mut adapter, false)?;
    if mode == "merge" { adapter.execute_sql("SET SESSION foreign_key_checks=1")?; }
    if let Some(sql) = timezone_sql { adapter.execute_sql(sql)?; }
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

fn import_mysql_tsv_table_insert_fallback<F: FnMut(Value)>(
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    ctx: &MysqlImportChunkContext,
    mut emit: F,
) -> Result<(u64, u64), String> {
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
        let rows = insert_mysql_tsv_chunk_with_batches(conn, ctx.table, &chunk_path, ctx.compression)
            .map_err(|err| {
                format!(
                    "mysql insert fallback error for table {} chunk {}: {err}",
                    ctx.table.name, chunk_index
                )
            })?;
        rows_imported += rows;
        chunks_imported += 1;
        emit(dump_import_row_progress_event(
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
        ));
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
        enum_warning_batch_rows(table),
        MYSQL_INSERT_FALLBACK_BATCH_BYTES,
        |rows| {
            let enum_zero = enum_zero_cells(table, rows);
            execute_mysql_import_statement(conn, &insert_rows_literal_sql_for_table("mysql", table, rows), table, &enum_zero)
                .map(|_| ())
        },
    )
}

fn import_mysql_tsv_table_parallel<F: FnMut(Value)>(
    endpoint: &Endpoint,
    input_path: &Path,
    threads: usize,
    ctx: &MysqlImportChunkContext,
    mut emit: F,
) -> Result<(u64, u64), String> {
    let max_threads = threads.max(1).min(ctx.table_manifest.chunks as usize);
    let mut pending =
        adaptive_import_chunk_order(input_path, ctx.table_manifest, "tsv", ctx.compression);
    let mut active = 0_usize;
    let mut completed = 0_u64;
    let mut rows_imported = 0_u64;
    let mut first_error: Option<String> = None;
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<ImportChunkEvent>();

    while active < max_threads {
        if let Some(chunk_index) = pending.pop_front() {
            handles.push(spawn_mysql_import_chunk_worker(
                endpoint.clone(),
                input_path.to_path_buf(),
                ctx.table.clone(),
                ctx.table_manifest.path.clone(),
                chunk_index,
                ctx.compression.to_string(),
                ctx.timezone_sql.map(str::to_string),
                ctx.mode.to_string(),
                sender.clone(),
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
                emit(dump_import_row_progress_event(
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
                ));
                if let Some(next_chunk) = next_import_chunk_after_completion(&mut pending, first_error.is_some()) {
                    handles.push(spawn_mysql_import_chunk_worker(
                        endpoint.clone(),
                        input_path.to_path_buf(),
                        ctx.table.clone(),
                        ctx.table_manifest.path.clone(),
                        next_chunk,
                        ctx.compression.to_string(),
                        ctx.timezone_sql.map(str::to_string),
                        ctx.mode.to_string(),
                        sender.clone(),
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
    if let Some(err) = first_error {
        return Err(err);
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

fn next_import_chunk_after_completion(pending: &mut VecDeque<u64>, failed: bool) -> Option<u64> {
    if failed { None } else { pending.pop_front() }
}

fn spawn_mysql_import_chunk_worker(
    endpoint: Endpoint,
    input_path: std::path::PathBuf,
    table: NormalizedTable,
    table_path: String,
    chunk_index: u64,
    compression: String,
    timezone_sql: Option<String>,
    mode: String,
    sender: mpsc::Sender<ImportChunkEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = (|| {
            // 워커 커넥션에도 세션 튜닝(fk/unique/sql_mode + timeout)을 적용한다.
            // 이전에는 워커가 튜닝 없이 연결해 fk_checks/timeout이 누락돼 있었다.
            let mut conn = connect_tuned_mysql_import_conn(&endpoint, timezone_sql.as_deref(), &mode)?;
            let chunk_path = dump_manifest_chunk_path(
                &input_path,
                &table_path,
                chunk_index,
                "tsv",
                &compression,
            )?;
            let started = Instant::now();
            let rows =
                load_mysql_tsv_chunk(&mut conn, &table, &chunk_path, &compression)?;
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

fn load_mysql_tsv_chunk(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    chunk_path: &Path,
    compression: &str,
) -> Result<u64, String> {
    let enum_columns = table.columns.iter().enumerate()
        .filter(|(_, column)| is_legacy_enum_zero_type(&column.type_name))
        .collect::<Vec<_>>();
    let mut enum_zero = BTreeSet::new();
    if !enum_columns.is_empty() {
        for (index, line) in open_dump_reader(chunk_path, compression)?.lines().enumerate() {
            let line = line.map_err(|err| format!("cannot read ENUM import values: {err}"))?;
            let fields = line.split('\t').collect::<Vec<_>>();
            for (column_index, column) in &enum_columns {
                if fields.get(*column_index) == Some(&"") {
                    enum_zero.insert((column.name.clone(), index as u64 + 1));
                }
            }
        }
    }
    if enum_zero.len() > MYSQL_IMPORT_MAX_WARNINGS {
        // Diagnostics cannot describe more warnings than max_error_count. Split
        // before executing LOAD DATA, so every accepted warning stays inspectable.
        return insert_mysql_tsv_chunk_with_batches(conn, table, chunk_path, compression);
    }
    let path = chunk_path.to_path_buf();
    let compression = compression.to_string();
    conn.set_local_infile_handler(Some(LocalInfileHandler::new(move |_, stream| {
        let mut reader = open_dump_reader(&path, &compression)
            .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err))?;
        std::io::copy(&mut reader, stream)?;
        Ok(())
    })));
    let sql = load_data_local_infile_sql("mysql", table, "tunnelforge_chunk");
    let result = execute_mysql_import_statement(conn, &sql, table, &enum_zero)
        .map_err(|err| format!("mysql LOAD DATA error: {err}"));
    conn.set_local_infile_handler(None);
    result
}

/// LOAD DATA LOCAL can downgrade coercions and duplicate keys to warnings even
/// in strict SQL mode. Commit only statements that stored every value faithfully.
pub(crate) fn mysql_enum_labels(type_name: &str) -> Option<Vec<String>> {
    let value = type_name.trim();
    if !value.get(..4)?.eq_ignore_ascii_case("enum") { return None; }
    let mut chars = value[4..].trim_start().strip_prefix('(')?.chars().peekable();
    let mut labels = Vec::new();
    loop {
        while chars.peek().is_some_and(|ch| ch.is_whitespace()) { chars.next(); }
        if chars.next()? != '\'' { return None; }
        let mut label = String::new();
        loop {
            match chars.next()? {
                '\'' if chars.peek() == Some(&'\'') => { chars.next(); label.push('\''); }
                '\'' => break,
                '\\' => label.push(chars.next()?),
                ch => label.push(ch),
            }
        }
        labels.push(label);
        while chars.peek().is_some_and(|ch| ch.is_whitespace()) { chars.next(); }
        match chars.next()? {
            ',' => continue,
            ')' => return Some(labels),
            _ => return None,
        }
    }
}

fn is_legacy_enum_zero_type(type_name: &str) -> bool {
    mysql_enum_labels(type_name).is_some_and(|labels| !labels.iter().any(String::is_empty))
}

fn enum_warning_batch_rows(table: &NormalizedTable) -> usize {
    let columns = table.columns.iter().filter(|column| is_legacy_enum_zero_type(&column.type_name)).count().max(1);
    MYSQL_INSERT_FALLBACK_BATCH_ROWS.min((MYSQL_IMPORT_MAX_WARNINGS / columns).max(1))
}

fn enum_zero_cells(table: &NormalizedTable, rows: &[Value]) -> BTreeSet<(String, u64)> {
    let mut cells = BTreeSet::new();
    for column in &table.columns {
        if !is_legacy_enum_zero_type(&column.type_name) { continue; }
        for (index, row) in rows.iter().enumerate() {
            if row.get(&column.name).and_then(Value::as_str) == Some("") {
                cells.insert((column.name.clone(), index as u64 + 1));
            }
        }
    }
    cells
}

fn enum_zero_warning_allowlist(
    conn: &mut mysql::PooledConn, table: &NormalizedTable, cells: &BTreeSet<(String, u64)>,
) -> Result<BTreeSet<String>, String> {
    if cells.is_empty() { return Ok(BTreeSet::new()); }
    let target_types: Vec<(String, String)> = conn.exec(
        "SELECT COLUMN_NAME,COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?",
        (&table.name,),
    ).map_err(|err| format!("ENUM target contract check failed: {err}"))?;
    let mut allowed = BTreeSet::new();
    for (name, row) in cells {
        let source = table.columns.iter().find(|column| column.name == *name)
            .and_then(|column| mysql_enum_labels(&column.type_name));
        let target = target_types.iter().find(|(column, _)| column == name)
            .and_then(|(_, type_name)| mysql_enum_labels(type_name));
        if source.is_some() && source == target {
            allowed.insert(format!("Data truncated for column '{name}' at row {row}"));
        }
    }
    Ok(allowed)
}

fn only_expected_enum_zero_warnings(
    count: u64, details: &[(String, u32, String)], allowed: &BTreeSet<String>,
) -> bool {
    // SHOW WARNINGS can be capped by max_error_count. Never accept unseen warnings.
    if count != details.len() as u64 { return false; }
    let mut remaining = allowed.clone();
    details.iter().all(|(_, code, message)| *code == 1265 && remaining.remove(message))
}

fn execute_mysql_import_statement(
    conn: &mut mysql::PooledConn,
    sql: &str,
    table: &NormalizedTable,
    enum_zero: &BTreeSet<(String, u64)>,
) -> Result<u64, String> {
    let allowed_warnings = enum_zero_warning_allowlist(conn, table, enum_zero)?;
    conn.query_drop("START TRANSACTION").map_err(|err| err.to_string())?;
    let mut saved_mode: Option<String> = None;
    let result = (|| {
        if !allowed_warnings.is_empty() {
            saved_mode = Some(conn.query_first("SELECT @@SESSION.sql_mode")
                .map_err(|err| err.to_string())?
                .ok_or_else(|| "cannot read SQL mode for legacy ENUM import".to_string())?);
            // ENUM index 0 is a real legacy storage value. Permit its insertion,
            // then reject every warning outside the exact source-cell allowlist.
            conn.query_drop("SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','))")
                .map_err(|err| err.to_string())?;
        }
        conn.query_drop(sql).map_err(|err| err.to_string())?;
        let rows = conn.affected_rows();
        let warnings: u64 = conn.query_first("SHOW COUNT(*) WARNINGS")
            .map_err(|err| err.to_string())?.unwrap_or(0);
        if warnings > 0 {
            let details: Vec<(String, u32, String)> = conn.query("SHOW WARNINGS")
                .map_err(|err| err.to_string())?;
            if !only_expected_enum_zero_warnings(warnings, &details, &allowed_warnings) {
                return Err(format!("import data validation failed: {warnings} MySQL warnings: {}",
                    details.iter().take(3).map(|(_, code, message)| format!("{code}: {message}"))
                        .collect::<Vec<_>>().join("; ")));
            }
        }
        Ok(rows)
    })();
    let restore = if let Some(mode) = saved_mode {
        conn.exec_drop("SET SESSION sql_mode=?", (mode,)).map_err(|err| format!("SQL mode restore failed: {err}"))
    } else { Ok(()) };
    let result = match (result, restore) {
        (Ok(rows), Ok(())) => conn.query_drop("COMMIT").map(|_| rows).map_err(|err| err.to_string()),
        (Err(error), Ok(())) | (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(restore)) => Err(format!("{error}; {restore}")),
    };
    if let Err(error) = result {
        return match conn.query_drop("ROLLBACK") {
            Ok(()) => Err(error),
            Err(rollback) => Err(format!("{error}; rollback failed: {rollback}")),
        };
    }
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

    #[test]
    #[ignore = "requires a disposable MySQL server via TF_MYSQL_HOST"]
    fn mysql_cleanup_restores_global_after_import_connection_dies() {
        let endpoint = Endpoint {
            engine: "mysql".into(), host: std::env::var("TF_MYSQL_HOST").unwrap(), port: 3306,
            user: "root".into(), password: "tf_local_test".into(), database: "tf_test".into(), schema: None,
            tls: Default::default(),
        };
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        let LiveAdapter::MySql(admin_conn) = &mut admin else { unreachable!() };
        let original = mysql_local_infile_value(admin_conn).unwrap();
        admin_conn.query_drop("SET GLOBAL local_infile=0").unwrap();
        let mut adapter = LiveAdapter::connect(&endpoint).unwrap();
        let previous = prepare_mysql_local_infile_policy(&mut adapter, &endpoint, "temporary_global", None, &mut |_| {}).unwrap();
        let LiveAdapter::MySql(conn) = &mut adapter else { unreachable!() };
        let id: u64 = conn.query_first("SELECT CONNECTION_ID()").unwrap().unwrap();
        admin_conn.query_drop(format!("KILL CONNECTION {id}")).unwrap();
        let result = restore_mysql_local_infile_policy(&mut adapter, &endpoint, previous, None, &mut |_| {});
        let restored = !mysql_local_infile_enabled(admin_conn);
        admin_conn.query_drop(mysql_set_global_local_infile_sql(mysql_bool_value_enabled(&original))).unwrap();
        assert!(result.is_ok(), "{result:?}");
        assert!(restored, "temporary GLOBAL local_infile was left enabled");
    }

    #[test]
    fn merge_never_restarts_by_truncating_existing_rows() {
        assert!(!may_restart_import_table("merge", 1));
        assert!(may_restart_import_table("replace", 1));
        assert!(may_restart_import_table("recreate", 1));
        assert!(!may_restart_import_table("replace", 2));
    }

    #[test]
    fn parallel_import_stops_dispatching_after_first_worker_error() {
        let mut pending = VecDeque::from([3, 4, 5]);
        assert_eq!(next_import_chunk_after_completion(&mut pending, false), Some(3));
        // Worker 1 fails; worker 2 completes afterwards. It must not start chunk 4.
        assert_eq!(next_import_chunk_after_completion(&mut pending, true), None);
        assert_eq!(pending, VecDeque::from([4, 5]));
    }

    #[test]
    fn ddl_probe_never_cleans_up_a_collision_and_surfaces_owned_cleanup_failure() {
        let mut statements = Vec::new();
        let collision = execute_owned_ddl_probe("CREATE TABLE probe", "DROP TABLE probe", "original", "probe", |sql| {
            statements.push(sql.to_string());
            Err("table already exists".into())
        });
        assert!(collision.unwrap_err().contains("ddl_preflight_failed"));
        assert_eq!(statements, ["CREATE TABLE probe"]);
        statements.clear();
        let cleanup = execute_owned_ddl_probe("CREATE TABLE probe", "DROP TABLE probe", "original", "probe", |sql| {
            statements.push(sql.to_string());
            if sql.starts_with("DROP") { Err("DROP denied".into()) } else { Ok(()) }
        });
        assert!(cleanup.unwrap_err().contains("ddl_probe_cleanup_failed"));
        assert_eq!(statements, ["CREATE TABLE probe", "DROP TABLE probe"]);
    }

    #[test]
    fn legacy_enum_warning_exception_requires_exact_source_cell_and_complete_warnings() {
        let message = "Data truncated for column 'choice' at row 2".to_string();
        let allowed = BTreeSet::from([message.clone()]);
        let expected = vec![("Warning".into(), 1265, message.clone())];
        assert!(only_expected_enum_zero_warnings(1, &expected, &allowed));
        assert!(!only_expected_enum_zero_warnings(2, &expected, &allowed));
        for (code, message) in [
            (1265, "Data truncated for column 'choice' at row 3"),
            (1265, "Data truncated for column 'amount' at row 2"),
            (1366, "Incorrect double value: '' for column 'amount' at row 2"),
        ] {
            assert!(!only_expected_enum_zero_warnings(1, &[("Warning".into(), code, message.into())], &allowed));
        }
        assert!(!only_expected_enum_zero_warnings(2, &[expected[0].clone(), expected[0].clone()], &allowed));
        assert!(is_legacy_enum_zero_type("enum('alpha','b''eta') CHARACTER SET utf8mb4"));
        assert!(!is_legacy_enum_zero_type("enum('alpha','')"));
        assert!(!is_legacy_enum_zero_type("FLOAT"));
        let mut table = schema().tables[0].clone();
        table.columns[1].type_name = "enum('alpha','beta')".into();
        assert!(enum_zero_cells(&table, &[json!({"name": "unsupported"})]).is_empty());
        assert_eq!(enum_zero_cells(&table, &[json!({"name": ""})]), BTreeSet::from([("name".into(), 1)]));
    }

    #[test]
    fn import_preserves_source_fidelity_warnings_and_corrects_legacy_snapshot_claim() {
        let mut manifest: DumpManifest = serde_json::from_value(json!({
            "format":"tunnelforge-dump", "format_version":2, "source_engine":"mysql",
            "database":"test", "schema":{"tables":[]}, "chunk_size":100,
            "created_unix_seconds":1, "tables":[], "strict_export":true,
            "snapshot_policy":"mysql_parallel_no_backup_lock_consistent_snapshot",
            "manifest_warnings":["Object not exported: trigger"],
        })).unwrap();
        let warnings = source_dump_warnings(&manifest);
        assert!(warnings.iter().any(|warning| warning.contains("Object not exported")));
        assert!(warnings.iter().any(|warning| warning.contains("independent snapshots")));
        assert!(warnings.iter().any(|warning| warning.contains("source timezone")));
        manifest.snapshot_policy = "mysql_single_connection_consistent_snapshot".into();
        manifest.source_timezone = Some("UTC".into());
        manifest.strict_export = false;
        assert!(source_dump_warnings(&manifest).iter().any(|warning| warning.contains("not marked strict")));
        manifest.strict_export = true;
        manifest.manifest_warnings.clear();
        assert!(source_dump_warnings(&manifest).is_empty());
    }

    #[test]
    fn invalid_import_metadata_is_rejected_before_connect() {
        for defect in ["wrong_checksum", "missing_schema", "duplicate_schema", "duplicate_table", "duplicate_path", "unknown_selection", "invalid_ddl", "rows_without_chunks", "malformed_payload", "wrong_row_count", "unsupported_fk_action"] {
            let dir = std::env::temp_dir().join(format!("tunnelforge-preflight-{}-{defect}", std::process::id()));
            fs::create_dir_all(dir.join("users")).unwrap();
            fs::write(dir.join("users/chunk_000001.tsv"), b"1\tAlice\n").unwrap();
            let mut manifest = DumpManifest {
                format: "tunnelforge-dump".into(), format_version: 2,
                data_format: "tsv".into(), compression: "none".into(),
                source_engine: "mysql".into(), database: "app".into(),
                source_schema: None,
                source_timezone: None,
                schema: schema(), snapshot_policy: "connection_consistent".into(),
                strict_export: true, manifest_warnings: vec![], chunk_size: 100,
                created_unix_seconds: 1, views: vec![],
                tables: vec![DumpTableManifest { name: "users".into(), path: "users".into(), rows: 0, chunks: 0, chunk_sha256: BTreeMap::new() }],
            };
            let expected = match defect {
                "wrong_checksum" => {
                    manifest.tables[0].chunks = 1;
                    manifest.tables[0].chunk_sha256.insert("wrong.tsv".into(), "0".repeat(64));
                    "chunk_sha256"
                }
                "missing_schema" => { manifest.schema.tables.clear(); "missing table" }
                "duplicate_schema" => { manifest.schema.tables.push(manifest.schema.tables[0].clone()); "duplicate schema" }
                "duplicate_table" => { manifest.tables.push(manifest.tables[0].clone()); "duplicate table" }
                "duplicate_path" => {
                    let mut other = manifest.tables[0].clone(); other.name = "other".into(); manifest.tables.push(other);
                    let mut other = manifest.schema.tables[0].clone(); other.name = "other".into(); manifest.schema.tables.push(other);
                    "duplicate chunk path"
                }
                "unknown_selection" => "unknown selected table",
                "unsupported_fk_action" => {
                    manifest.schema.tables[0].foreign_keys.push(NormalizedForeignKey {
                        name: "fk_default".into(), columns: vec!["id".into()],
                        referenced_table: "parent".into(), referenced_columns: vec!["id".into()],
                        on_delete: Some(ForeignKeyAction::SetDefault), on_update: None,
                    });
                    "SET DEFAULT"
                }
                "invalid_ddl" => { manifest.schema.tables[0].columns[0].type_name = "int); DROP TABLE users; --".into(); "cannot generate DDL" }
                "rows_without_chunks" => { manifest.tables[0].rows = 1; "rows but no chunks" }
                "malformed_payload" | "wrong_row_count" => {
                    manifest.data_format = "jsonl".into();
                    let path = dir.join("users/chunk_000001.jsonl");
                    fs::write(&path, if defect == "malformed_payload" { "{broken\n" } else { "{\"id\":1,\"name\":\"Alice\"}\n" }).unwrap();
                    manifest.tables[0].chunks = 1;
                    manifest.tables[0].rows = 2;
                    manifest.tables[0].chunk_sha256.insert("chunk_000001.jsonl".into(), sha256_file(&path).unwrap());
                    "export_invalid"
                }
                _ => unreachable!(),
            };
            write_dump_manifest(&dir, &manifest).unwrap();
            let mut payload = json!({ "input_dir": dir.to_string_lossy(), "mode": "replace", "target": { "engine": "mysql", "host": "127.0.0.1", "port": 1, "user": "root", "password": "", "database": "app" } });
            if defect == "unknown_selection" { payload["tables"] = json!(["users", "missing"]); }
            let err = dump_import(&Request { command: "dump.import".into(), request_id: None, payload }, |_| {}).unwrap_err();
            fs::remove_dir_all(&dir).unwrap();
            assert!(err.contains(expected), "{defect}: expected {expected}, got {err}");
        }
    }
    
    
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::fs::{self};
    
    
    
    
    
    
    
    use crate::adapters::test_support::{schema};

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
    fn import_timezone_prefers_explicit_override_then_manifest_then_server_default() {
        assert_eq!(import_timezone_sql(&json!({}), Some("UTC"), "mysql").unwrap(), Some("SET SESSION time_zone = '+00:00'".into()));
        assert_eq!(import_timezone_sql(&json!({}), Some("UTC"), "postgresql").unwrap(), Some("SET TIME ZONE 'UTC'".into()));
        assert_eq!(import_timezone_sql(&json!({"timezone_sql": "SET SESSION time_zone = '+09:00'"}), Some("UTC"), "mysql").unwrap(), Some("SET SESSION time_zone = '+09:00'".into()));
        assert_eq!(import_timezone_sql(&json!({"use_source_timezone": false}), Some("UTC"), "mysql").unwrap(), None);
        assert_eq!(import_timezone_sql(&json!({}), None, "mysql").unwrap(), None);
        assert!(import_timezone_sql(&json!({}), Some("unrecognized"), "mysql").is_err());
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
                "SET SESSION sql_mode = CONCAT_WS(',', 'STRICT_ALL_TABLES', 'NO_AUTO_VALUE_ON_ZERO', TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', ''), 'NO_ZERO_IN_DATE', ''), 'NO_ZERO_DATE', ''), 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','), ',,', ',')))".to_string(),
                "SET SESSION foreign_key_checks=0".to_string(),
                "SET SESSION unique_checks=1".to_string(),
            "SET SESSION default_storage_engine='InnoDB'".to_string(),
            "SET SESSION max_error_count=65535".to_string(),
            "SET SESSION lc_messages='en_US'".to_string(),
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
}
