use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use mysql::prelude::Queryable;
use crate::*;

/// dump.run / dump.import 요청에 threads가 명시되지 않았을 때 사용하는 기본 워커 수.
pub(crate) const DEFAULT_DUMP_THREADS: usize = 8;
const MYSQL_GLOBAL_READ_LOCK_TIMEOUT: Duration = Duration::from_secs(2);

fn mysql_snapshot_worker_setup_sql() -> [&'static str; 2] {
    [
        "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ]
}

fn mysql_snapshot_coordinator_sql() -> [&'static str; 4] {
    [
        "SET SESSION lock_wait_timeout=2",
        "FLUSH TABLES WITH READ LOCK",
        "LOCK INSTANCE FOR BACKUP",
        "UNLOCK TABLES",
    ]
}

struct MysqlSharedSnapshot {
    coordinator: mysql::PooledConn,
    workers: Vec<mysql::PooledConn>,
    setup_ms: u64,
}

impl MysqlSharedSnapshot {
    fn acquire(endpoint: &Endpoint, worker_count: usize) -> Result<Self, String> {
        let started = Instant::now();
        let mut coordinator = mysql_connection(endpoint)?;
        let mut control = mysql_connection(endpoint)?;
        let mut workers = (0..worker_count.max(1))
            .map(|_| mysql_connection(endpoint))
            .collect::<Result<Vec<_>, _>>()?;
        let [isolation_sql, snapshot_sql] = mysql_snapshot_worker_setup_sql();
        for worker in &mut workers {
            worker
                .query_drop(isolation_sql)
                .map_err(|err| format!("mysql snapshot isolation setup failed: {err}"))?;
        }

        let [timeout_sql, global_lock_sql, backup_lock_sql, unlock_tables_sql] =
            mysql_snapshot_coordinator_sql();
        coordinator
            .query_drop(timeout_sql)
            .map_err(|err| format!("mysql snapshot lock timeout setup failed: {err}"))?;
        let connection_id = coordinator
            .query_first::<u64, _>("SELECT CONNECTION_ID()")
            .map_err(|err| format!("mysql snapshot coordinator id failed: {err}"))?
            .ok_or_else(|| "mysql snapshot coordinator id was empty".to_string())?;
        let (cancel_tx, cancel_rx) = mpsc::channel::<()>();
        let watchdog = thread::spawn(move || {
            if cancel_rx.recv_timeout(MYSQL_GLOBAL_READ_LOCK_TIMEOUT).is_err() {
                let _ = control.query_drop(format!("KILL QUERY {connection_id}"));
            }
        });
        let global_lock_result = coordinator.query_drop(global_lock_sql);
        let _ = cancel_tx.send(());
        let _ = watchdog.join();
        global_lock_result.map_err(|err| {
            format!(
                "mysql consistent snapshot could not acquire the brief global read lock within 2 seconds: {err}"
            )
        })?;

        let setup_result = (|| -> Result<(), String> {
            for worker in &mut workers {
                worker.query_drop(snapshot_sql).map_err(|err| {
                    format!("mysql worker consistent snapshot start failed: {err}")
                })?;
            }
            coordinator.query_drop(backup_lock_sql).map_err(|err| {
                format!(
                    "mysql backup lock failed; BACKUP_ADMIN is required for a safe online export: {err}"
                )
            })?;
            coordinator
                .query_drop(unlock_tables_sql)
                .map_err(|err| format!("mysql global read lock release failed: {err}"))?;
            Ok(())
        })();
        if let Err(err) = setup_result {
            let _ = coordinator.query_drop("UNLOCK TABLES");
            let _ = coordinator.query_drop("UNLOCK INSTANCE");
            for worker in &mut workers {
                let _ = worker.query_drop("ROLLBACK");
            }
            return Err(err);
        }

        Ok(Self {
            coordinator,
            workers,
            setup_ms: started.elapsed().as_millis().max(1) as u64,
        })
    }

    fn take_workers(&mut self) -> Vec<mysql::PooledConn> {
        std::mem::take(&mut self.workers)
    }

    fn validate_transactional_tables(
        &mut self,
        endpoint: &Endpoint,
        tables: &[NormalizedTable],
    ) -> Result<(), String> {
        let worker = self
            .workers
            .first_mut()
            .ok_or_else(|| "mysql snapshot worker was not initialized".to_string())?;
        let table_names = tables
            .iter()
            .map(|table| sql_literal(&Value::String(table.name.clone())))
            .collect::<Vec<_>>()
            .join(", ");
        let rows = worker
            .query::<(String, String), _>(format!(
                "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = {} AND TABLE_NAME IN ({})",
                sql_literal(&Value::String(endpoint_schema(endpoint))),
                table_names
            ))
            .map_err(|err| format!("mysql snapshot table-engine inspection failed: {err}"))?;
        let offenders = non_transactional_mysql_tables(&rows);
        if offenders.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "mysql online consistent export requires InnoDB tables; convert or separately handle: {}",
                offenders.join(", ")
            ))
        }
    }
}

fn non_transactional_mysql_tables(rows: &[(String, String)]) -> Vec<String> {
    let mut offenders = rows
        .iter()
        .filter(|(_, engine)| !engine.eq_ignore_ascii_case("innodb"))
        .map(|(table, engine)| format!("{table} ({engine})"))
        .collect::<Vec<_>>();
    offenders.sort();
    offenders
}

impl Drop for MysqlSharedSnapshot {
    fn drop(&mut self) {
        for worker in &mut self.workers {
            let _ = worker.query_drop("ROLLBACK");
        }
        let _ = self.coordinator.query_drop("UNLOCK TABLES");
        let _ = self.coordinator.query_drop("UNLOCK INSTANCE");
    }
}

fn mysql_connection(endpoint: &Endpoint) -> Result<mysql::PooledConn, String> {
    match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => Ok(conn),
        LiveAdapter::PostgreSql(_) => Err("mysql connection requires mysql endpoint".to_string()),
    }
}

pub(crate) fn dump_run_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump",
        "message": "dump started"
    }));

    match dump_run(request, |event| emit(event)) {
        Ok(result) => emit(result),
        Err(err) => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct DumpTablePerfProfile {
    pub(crate) avg_row_bytes: u64,
    pub(crate) chunk_rows: usize,
    pub(crate) rows_per_second: u64,
    pub(crate) duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DumpWorkPlanItem {
    table: String,
    chunk_index: Option<u64>,
}

fn global_dump_work_plan(
    tables: &[NormalizedTable],
    range_chunks: &BTreeMap<String, u64>,
) -> Vec<DumpWorkPlanItem> {
    let mut plan = Vec::new();
    let max_chunks = range_chunks.values().copied().max().unwrap_or(0);
    for chunk_index in 1..=max_chunks {
        for table in tables {
            if let Some(chunks) = range_chunks.get(&table.name).copied() {
                if chunk_index <= chunks {
                    plan.push(DumpWorkPlanItem {
                        table: table.name.clone(),
                        chunk_index: Some(chunk_index),
                    });
                }
            }
        }
        if chunk_index == 1 {
            for table in tables {
                if !range_chunks.contains_key(&table.name) {
                    plan.push(DumpWorkPlanItem {
                        table: table.name.clone(),
                        chunk_index: None,
                    });
                }
            }
        }
    }
    if max_chunks == 0 {
        for table in tables {
            plan.push(DumpWorkPlanItem {
                table: table.name.clone(),
                chunk_index: None,
            });
        }
    }
    plan
}

fn global_dump_work_plan_for_ranges(
    tables: &[NormalizedTable],
    range_chunks: &BTreeMap<String, Vec<DumpRange>>,
) -> Vec<DumpWorkPlanItem> {
    let range_counts = range_chunks
        .iter()
        .map(|(table, ranges)| (table.clone(), ranges.len() as u64))
        .collect::<BTreeMap<_, _>>();
    global_dump_work_plan(tables, &range_counts)
}

fn dump_table_row_counts(
    endpoint: &Endpoint,
    tables: &[NormalizedTable],
) -> BTreeMap<String, u64> {
    let mut counts = BTreeMap::new();
    if endpoint.engine != "mysql" || tables.is_empty() {
        return counts;
    }
    let mut conn = match LiveAdapter::connect(endpoint) {
        Ok(LiveAdapter::MySql(conn)) => conn,
        _ => return counts,
    };
    let schema_name = endpoint_schema(endpoint);
    let table_names = tables
        .iter()
        .map(|table| sql_literal(&Value::String(table.name.clone())))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0) FROM information_schema.tables WHERE TABLE_SCHEMA = {} AND TABLE_NAME IN ({})",
        sql_literal(&Value::String(schema_name)),
        table_names
    );
    let Ok(rows) = conn.query::<(String, u64), _>(sql) else {
        return counts;
    };
    for (table, rows) in rows {
        counts.insert(table, rows);
    }
    counts
}

fn dump_perf_profile_path() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .or_else(|| std::env::var_os("APPDATA"))
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .map(|base| base.join("TunnelForge").join("dump_perf_profile.json"))
}

fn dump_profile_key(
    endpoint: &Endpoint,
    table: &str,
    data_format: &str,
    compression: &str,
) -> String {
    format!(
        "{}:{}:{}:{}:{}",
        endpoint.engine, endpoint.database, table, data_format, compression
    )
}

fn load_dump_perf_profiles() -> BTreeMap<String, DumpTablePerfProfile> {
    let Some(path) = dump_perf_profile_path() else {
        return BTreeMap::new();
    };
    let Ok(bytes) = fs::read(path) else {
        return BTreeMap::new();
    };
    serde_json::from_slice(&bytes).unwrap_or_default()
}

fn save_dump_perf_profiles(profiles: &BTreeMap<String, DumpTablePerfProfile>) {
    let Some(path) = dump_perf_profile_path() else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(bytes) = serde_json::to_vec_pretty(profiles) {
        let _ = fs::write(path, bytes);
    }
}

fn dump_plan_event(
    request_id: Option<String>,
    tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
) -> Value {
    let rows_total = tables
        .iter()
        .map(|table| row_counts.get(&table.name).copied().unwrap_or(0))
        .sum::<u64>();
    json!({
        "event": "dump_plan",
        "request_id": request_id,
        "tables_total": tables.len(),
        "rows_total": rows_total,
        "tables": tables.iter().map(|table| {
            json!({
                "name": table.name,
                "rows": row_counts.get(&table.name).copied().unwrap_or(0)
            })
        }).collect::<Vec<_>>()
    })
}

fn dump_schedule_event(
    request_id: Option<String>,
    scheduled_tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
    limits: DumpParallelLimits,
    threads: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    scheduler: &str,
) -> Value {
    let chunk_size = chunk_size.max(1) as u64;
    json!({
        "event": "dump_schedule",
        "request_id": request_id,
        "threads": threads,
        "table_workers": limits.table_workers,
        "range_workers_per_table": limits.range_workers_per_table,
        "chunk_size": chunk_size,
        "data_format": data_format,
        "compression": compression,
        "scheduler": scheduler,
        "scheduled_tables": scheduled_tables.iter().take(12).map(|table| {
            let rows = row_counts.get(&table.name).copied().unwrap_or(0);
            json!({
                "name": table.name,
                "rows": rows,
                "estimated_chunks": rows.saturating_add(chunk_size - 1) / chunk_size
            })
        }).collect::<Vec<_>>()
    })
}

/// dump.import 진행률 이벤트의 청크 관련 필드 묶음.
///
/// 이전에는 네 개의 위치 인자 `Option<u64>`가 나란히 전달돼 호출부에서
/// `chunks_done`과 `chunk_index`가 같은 값을 두 번 넘기는 등 의미가 불명확했다.
/// named field 로 묶어 각 값의 역할을 호출부에서 자기문서화한다.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct ChunkProgress {
    pub(crate) chunks_done: Option<u64>,
    pub(crate) chunks_total: Option<u64>,
    pub(crate) chunk_index: Option<u64>,
    pub(crate) load_ms: Option<u64>,
}

pub(crate) fn dump_import_row_progress_event(
    request_id: Option<String>,
    table: &str,
    table_rows_done: u64,
    table_rows_total: u64,
    overall_rows_before: u64,
    overall_rows_total: u64,
    chunk_rows: u64,
    chunk_progress: ChunkProgress,
    strategy: &str,
) -> Value {
    let raw_overall_rows_done = overall_rows_before.saturating_add(table_rows_done);
    let overall_rows_done = if overall_rows_total > 0 {
        raw_overall_rows_done.min(overall_rows_total)
    } else {
        raw_overall_rows_done
    };
    let mut event = json!({
        "event": "row_progress",
        "request_id": request_id,
        "table": table,
        "rows": table_rows_done,
        "total": table_rows_total,
        "table_rows_done": table_rows_done,
        "table_rows_total": table_rows_total,
        "overall_rows_done": overall_rows_done,
        "overall_rows_total": overall_rows_total,
        "chunk_rows": chunk_rows,
        "strategy": strategy
    });

    if let Value::Object(fields) = &mut event {
        if let Some(value) = chunk_progress.chunks_done {
            fields.insert("chunks_done".to_string(), json!(value));
        }
        if let Some(value) = chunk_progress.chunks_total {
            fields.insert("chunks_total".to_string(), json!(value));
        }
        if let Some(value) = chunk_progress.chunk_index {
            fields.insert("chunk_index".to_string(), json!(value));
        }
        if let Some(value) = chunk_progress.load_ms {
            fields.insert("load_ms".to_string(), json!(value));
        }
    }

    event
}

/// dump.run 요청 payload에서 파싱·검증한 옵션 묶음.
struct DumpRunOptions {
    output_dir: String,
    chunk_size: usize,
    threads: usize,
    overwrite: bool,
    selected_tables: Vec<String>,
    data_format: String,
    compression: String,
}

fn parse_dump_run_options(request: &Request) -> Result<DumpRunOptions, String> {
    let output_dir = request
        .payload
        .get("output_dir")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "dump.run requires output_dir".to_string())?
        .to_string();
    let chunk_size = request
        .payload
        .get("chunk_size")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or_else(default_chunk_size)
        .max(1);
    let threads = request
        .payload
        .get("threads")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or(DEFAULT_DUMP_THREADS)
        .max(1);
    let overwrite = request
        .payload
        .get("overwrite")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let selected_tables = string_list(request.payload.get("tables"));
    let data_format = request
        .payload
        .get("data_format")
        .and_then(Value::as_str)
        .unwrap_or("tsv")
        .to_ascii_lowercase();
    if !matches!(data_format.as_str(), "jsonl" | "tsv") {
        return Err(format!("unsupported dump data_format: {data_format}"));
    }
    let compression = request
        .payload
        .get("compression")
        .and_then(Value::as_str)
        .unwrap_or("zstd")
        .to_ascii_lowercase();
    if !matches!(compression.as_str(), "none" | "zstd") {
        return Err(format!("unsupported dump compression: {compression}"));
    }
    Ok(DumpRunOptions {
        output_dir,
        chunk_size,
        threads,
        overwrite,
        selected_tables,
        data_format,
        compression,
    })
}

/// engine/threads/table_total로 결정되는 덤프 실행 전략.
enum DumpStrategy {
    GlobalMysql,
    TableParallel,
    Sequential,
}

impl DumpStrategy {
    fn scheduler_label(&self) -> &'static str {
        match self {
            DumpStrategy::GlobalMysql => "global_chunk",
            _ => "table_parallel",
        }
    }
}

fn select_dump_strategy(engine: &str, threads: usize, table_total: usize) -> DumpStrategy {
    if engine == "mysql" {
        DumpStrategy::GlobalMysql
    } else if threads > 1 && table_total > 1 {
        DumpStrategy::TableParallel
    } else {
        DumpStrategy::Sequential
    }
}

/// View 정의 수집(전체 export 시) + DumpManifest 조립 및 파일 기록. (manifest, view 개수)를 반환한다.
fn finalize_dump_manifest<F: FnMut(Value)>(
    endpoint: &Endpoint,
    schema: NormalizedSchema,
    table_manifests: Vec<DumpTableManifest>,
    options: &DumpRunOptions,
    output_path: &Path,
    full_export: bool,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(DumpManifest, usize), String> {
    // View 정의 수집 (전체 export 시에만). 실패해도 테이블 덤프는 유효하므로 fatal로 보지 않는다.
    let views = if full_export {
        match collect_views(endpoint) {
            Ok(views) => views,
            Err(err) => {
                emit(json!({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "dump",
                    "message": format!("View 정의 수집 실패 (테이블 덤프는 정상): {err}"),
                }));
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };
    let views_count = views.len();
    let (snapshot_policy, strict_export, manifest_warnings) =
        dump_manifest_consistency_metadata(&endpoint.engine, options.threads);

    let manifest = DumpManifest {
        format: "tunnelforge-dump".to_string(),
        format_version: if options.data_format == "jsonl" { 1 } else { 2 },
        data_format: options.data_format.clone(),
        compression: options.compression.clone(),
        source_engine: endpoint.engine.clone(),
        database: endpoint.database.clone(),
        schema,
        snapshot_policy,
        strict_export,
        manifest_warnings,
        chunk_size: options.chunk_size,
        created_unix_seconds: current_unix_seconds(),
        tables: table_manifests,
        views,
    };
    write_dump_manifest(output_path, &manifest)?;
    Ok((manifest, views_count))
}

fn dump_run<F: FnMut(Value)>(request: &Request, mut emit: F) -> Result<Value, String> {
    let endpoint = request_endpoint(request)?;
    let options = parse_dump_run_options(request)?;

    let output_path = Path::new(&options.output_dir);
    prepare_dump_output_dir(output_path, options.overwrite)?;

    let mut mysql_snapshot = if endpoint.engine == "mysql" {
        emit(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "snapshot",
            "message": "공유 일관 스냅샷 준비 중 (쓰기 잠금은 최대 2초 내 획득 후 즉시 해제)"
        }));
        let snapshot = MysqlSharedSnapshot::acquire(&endpoint, options.threads)?;
        emit(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "snapshot",
            "message": format!(
                "MySQL 공유 일관 스냅샷 준비 완료: {}개 워커, {} ms (일반 쓰기 가능, DDL만 export 종료까지 제한)",
                snapshot.workers.len(), snapshot.setup_ms
            ),
            "snapshot_policy": "mysql_shared_consistent_snapshot",
            "worker_count": snapshot.workers.len(),
            "setup_ms": snapshot.setup_ms
        }));
        Some(snapshot)
    } else {
        None
    };

    // 부분 export(tables 지정) 시에는 View가 참조하는 base table이 빠질 수 있으므로 View를 수집하지 않는다.
    let full_export = options.selected_tables.is_empty();
    let inspection = inspect_live(&endpoint)?;
    let mut schema = inspection.schema;
    if !options.selected_tables.is_empty() {
        let selected: BTreeSet<String> = options.selected_tables.iter().cloned().collect();
        schema.tables.retain(|table| selected.contains(&table.name));
    }
    schema = dependency_ordered_schema(&schema);
    if schema.tables.is_empty() {
        return Err("dump.run found no tables to export".to_string());
    }
    if let Some(snapshot) = mysql_snapshot.as_mut() {
        snapshot.validate_transactional_tables(&endpoint, &schema.tables)?;
    }

    let row_counts = dump_table_row_counts(&endpoint, &schema.tables);
    emit(dump_plan_event(
        request.request_id.clone(),
        &schema.tables,
        &row_counts,
    ));

    let table_total = schema.tables.len();
    let parallel_limits = dump_parallel_limits(options.threads, table_total);
    let strategy = select_dump_strategy(&endpoint.engine, options.threads, table_total);
    let export_tables = if options.threads > 1 && table_total > 1 {
        dump_schedule_order(&schema.tables, &row_counts)
    } else {
        schema.tables.clone()
    };
    emit(dump_schedule_event(
        request.request_id.clone(),
        &export_tables,
        &row_counts,
        parallel_limits,
        options.threads,
        options.chunk_size,
        &options.data_format,
        &options.compression,
        strategy.scheduler_label(),
    ));
    let ctx = DumpJobContext {
        endpoint: endpoint.clone(),
        output_path: output_path.to_path_buf(),
        chunk_size: options.chunk_size,
        data_format: options.data_format.clone(),
        compression: options.compression.clone(),
        request_id: request.request_id.clone(),
    };
    let (table_manifests, total_rows, total_chunks) = match strategy {
        DumpStrategy::GlobalMysql => {
            let workers = mysql_snapshot
                .as_mut()
                .ok_or_else(|| "mysql snapshot session was not initialized".to_string())?
                .take_workers();
            dump_tables_global_mysql(&ctx, &export_tables, workers, |event| emit(event))?
        }
        DumpStrategy::TableParallel => dump_tables_parallel(
            &ctx,
            &export_tables,
            parallel_limits.table_workers,
            parallel_limits.range_workers_per_table,
            |event| emit(event),
        )?,
        DumpStrategy::Sequential => {
            let mut adapter = LiveAdapter::connect(&endpoint)?;
            dump_tables_sequential(&mut adapter, &ctx, &export_tables, |event| emit(event))?
        }
    };

    let (manifest, views_count) = finalize_dump_manifest(
        &endpoint,
        schema,
        table_manifests,
        &options,
        output_path,
        full_export,
        request.request_id.clone(),
        |event| emit(event),
    )?;

    Ok(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "dump.run",
        "success": true,
        "output_dir": options.output_dir,
        "format": manifest.format,
        "format_version": manifest.format_version,
        "compression": manifest.compression,
        "snapshot_policy": manifest.snapshot_policy,
        "strict_export": manifest.strict_export,
        "manifest_warnings": manifest.manifest_warnings,
        "tables": manifest.tables.len(),
        "views": views_count,
        "rows_dumped": total_rows,
        "chunks_dumped": total_chunks,
        "manifest": "_tunnelforge_dump.json"
    }))
}

fn prepare_dump_output_dir(output_path: &Path, overwrite: bool) -> Result<(), String> {
    if output_path.as_os_str().is_empty() || output_path.parent().is_none() {
        return Err("refusing to use unsafe dump output_dir".to_string());
    }
    if output_path.exists() {
        let mut entries = fs::read_dir(output_path)
            .map_err(|err| format!("failed to inspect dump output_dir: {err}"))?;
        let is_empty = entries.next().is_none();
        if !is_empty {
            if !overwrite {
                return Err("dump output_dir already exists and is not empty".to_string());
            }
            if !has_tunnelforge_dump_marker(output_path) {
                return Err(
                    "refusing to overwrite output_dir without TunnelForge dump marker".to_string(),
                );
            }
            remove_dump_output_dir(output_path)?;
        }
    }
    fs::create_dir_all(output_path)
        .map_err(|err| format!("failed to create dump output_dir: {err}"))
}

fn remove_dump_output_dir(output_path: &Path) -> Result<(), String> {
    let confirmed_dump_dir = output_path;
    fs::remove_dir_all(confirmed_dump_dir)
        .map_err(|err| format!("failed to clear dump output_dir: {err}"))
}

fn has_tunnelforge_dump_marker(output_path: &Path) -> bool {
    let marker_path = output_path.join(DUMP_DIR_MARKER);
    let manifest_path = output_path.join("_tunnelforge_dump.json");
    let Ok(marker_file) = File::open(marker_path) else {
        return false;
    };
    let marker_ok = serde_json::from_reader::<_, Value>(marker_file)
        .ok()
        .and_then(|value| {
            value
                .get("format")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .as_deref()
        == Some("tunnelforge-dump-dir");
    if !marker_ok {
        return false;
    }
    let Ok(manifest_file) = File::open(manifest_path) else {
        return false;
    };
    serde_json::from_reader::<_, Value>(manifest_file)
        .ok()
        .and_then(|value| {
            value
                .get("format")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .as_deref()
        == Some("tunnelforge-dump")
}

fn dump_tables_sequential<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    ctx: &DumpJobContext,
    tables: &[NormalizedTable],
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let mut manifests = Vec::new();
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let table_total = tables.len();

    for (index, table) in tables.iter().enumerate() {
        let (manifest, rows, chunks) =
            dump_one_table(adapter, ctx, table, index, table_total, |event| emit(event))?;
        manifests.push(manifest);
        total_rows += rows;
        total_chunks += chunks;
    }

    Ok((manifests, total_rows, total_chunks))
}

fn bounded_dump_chunk_limit(total_rows: u64, rows_dumped: u64, chunk_size: usize) -> Option<usize> {
    if total_rows > 0 && rows_dumped >= total_rows {
        return None;
    }
    let limit = chunk_size.max(1);
    if total_rows == 0 {
        return Some(limit);
    }
    Some(limit.min((total_rows - rows_dumped) as usize))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DumpParallelLimits {
    table_workers: usize,
    range_workers_per_table: usize,
}

impl DumpParallelLimits {
    #[cfg(test)]
    fn estimated_mysql_connections(&self) -> usize {
        self.table_workers * (self.range_workers_per_table + 1)
    }
}

/// dump.run 한 번에 걸쳐 고정되는 공통 파라미터 묶음.
///
/// endpoint/output_path/chunk_size/data_format/compression/request_id는 모든 테이블·
/// 청크 덤프 경로가 동일하게 참조하던 값이라, 개별 인자로 6개씩 관통시키는 대신
/// 하나의 컨텍스트로 전달한다. 동기 경로는 `&DumpJobContext`로, 스레드 워커는
/// clone 한 owned 값으로 넘긴다.
#[derive(Clone)]
struct DumpJobContext {
    endpoint: Endpoint,
    output_path: PathBuf,
    chunk_size: usize,
    data_format: String,
    compression: String,
    request_id: Option<String>,
}

fn dump_parallel_limits(threads: usize, table_total: usize) -> DumpParallelLimits {
    let thread_budget = threads.max(1);
    let table_workers = if table_total <= 1 {
        1
    } else if table_total <= thread_budget {
        table_total
    } else {
        (thread_budget / 4).max(1).min(table_total)
    };
    let range_workers_per_table = (thread_budget / table_workers).max(1);
    DumpParallelLimits {
        table_workers,
        range_workers_per_table,
    }
}

fn dump_schedule_order(
    tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
) -> Vec<NormalizedTable> {
    let mut indexed = tables
        .iter()
        .cloned()
        .enumerate()
        .collect::<Vec<(usize, NormalizedTable)>>();
    indexed.sort_by(|(left_index, left), (right_index, right)| {
        row_counts
            .get(&right.name)
            .copied()
            .unwrap_or(0)
            .cmp(&row_counts.get(&left.name).copied().unwrap_or(0))
            .then_with(|| left_index.cmp(right_index))
    });
    indexed.into_iter().map(|(_, table)| table).collect()
}

/// `run_bounded_pool`의 on_event 클로저가 반환하는, 워커 슬롯에 대한 지시.
enum PoolAction {
    /// 슬롯을 소비하지 않는 이벤트(진행률 pass-through 등). active/completed 불변.
    KeepGoing,
    /// 워커 하나가 종료. 슬롯을 반환하고 pending에서 다음 작업을 리필한다.
    Advance,
    /// 워커 하나가 종료. 슬롯은 반환하되 리필하지 않는다(에러 확산 중단 경로).
    AdvanceNoRefill,
}

/// bounded worker-pool 디스패치 루프. `max_workers`개까지 워커를 채운 뒤, 각 이벤트를
/// `on_event`에 넘긴다. on_event가 반환한 `PoolAction`에 따라 슬롯을 관리한다:
/// KeepGoing은 슬롯 불변, Advance는 슬롯 반환 후 리필, AdvanceNoRefill은 반환만 한다.
/// 이벤트별 emit·상태 누적·first_error 캡처는 전적으로 on_event가 담당해 각 호출자의
/// bookkeeping을 그대로 보존한다. 종료 시 모든 워커 핸들을 join한다.
fn run_bounded_pool<Item, Event>(
    mut pending: VecDeque<Item>,
    max_workers: usize,
    receiver: &mpsc::Receiver<Event>,
    mut spawn: impl FnMut(Item) -> thread::JoinHandle<()>,
    mut on_event: impl FnMut(Event) -> PoolAction,
) {
    let total = pending.len();
    let mut handles = Vec::new();
    let mut active = 0_usize;
    let mut completed = 0_usize;

    while active < max_workers {
        if let Some(item) = pending.pop_front() {
            handles.push(spawn(item));
            active += 1;
        } else {
            break;
        }
    }

    while completed < total && active > 0 {
        match receiver.recv() {
            Ok(event) => match on_event(event) {
                PoolAction::KeepGoing => {}
                PoolAction::Advance => {
                    completed += 1;
                    active = active.saturating_sub(1);
                    if let Some(item) = pending.pop_front() {
                        handles.push(spawn(item));
                        active += 1;
                    }
                }
                PoolAction::AdvanceNoRefill => {
                    completed += 1;
                    active = active.saturating_sub(1);
                }
            },
            Err(_) => break,
        }
    }

    for handle in handles {
        let _ = handle.join();
    }
}

fn dump_tables_parallel<F: FnMut(Value)>(
    ctx: &DumpJobContext,
    tables: &[NormalizedTable],
    table_threads: usize,
    range_threads: usize,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let table_total = tables.len();
    let max_threads = table_threads.max(1).min(table_total);
    let pending = (0..table_total).collect::<VecDeque<_>>();
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let mut first_error: Option<String> = None;
    let mut manifests: Vec<Option<DumpTableManifest>> = vec![None; table_total];
    let (sender, receiver) = mpsc::channel::<DumpTableEvent>();

    run_bounded_pool(
        pending,
        max_threads,
        &receiver,
        |index| {
            spawn_dump_table_worker(
                ctx.clone(),
                tables[index].clone(),
                index,
                table_total,
                range_threads,
                sender.clone(),
            )
        },
        |event| match event {
            DumpTableEvent::Progress(event) => {
                emit(event);
                PoolAction::KeepGoing
            }
            DumpTableEvent::Done {
                index,
                manifest,
                rows,
                chunks,
            } => {
                manifests[index] = Some(manifest);
                total_rows += rows;
                total_chunks += chunks;
                PoolAction::Advance
            }
            // 에러가 나도 나머지 테이블 워커는 계속 리필해 진행한다(기존 동작 보존).
            DumpTableEvent::Error(err) => {
                first_error.get_or_insert(err);
                PoolAction::Advance
            }
        },
    );

    if let Some(err) = first_error {
        return Err(err);
    }

    Ok((
        manifests
            .into_iter()
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| "parallel dump did not produce all table manifests".to_string())?,
        total_rows,
        total_chunks,
    ))
}

fn dump_tables_global_mysql<F: FnMut(Value)>(
    ctx: &DumpJobContext,
    tables: &[NormalizedTable],
    mut workers: Vec<mysql::PooledConn>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let table_total = tables.len();
    let mut conn = workers
        .pop()
        .ok_or_else(|| "global mysql dump requires at least one snapshot worker".to_string())?;
    let profiles = load_dump_perf_profiles();
    let mut ranges_by_table = BTreeMap::<String, Vec<DumpRange>>::new();
    let mut states = Vec::<DumpGlobalTableState>::new();

    for (index, table) in tables.iter().enumerate() {
        let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
        let table_dir = ctx.output_path.join(&table_path);
        fs::create_dir_all(&table_dir)
            .map_err(|err| format!("failed to create dump table dir: {err}"))?;
        let table_row_count = conn
            .query_first::<u64, _>(count_sql("mysql", &table.name))
            .map(|count| count.unwrap_or(0))
            .unwrap_or(0);
        let mut chunks_total = 0_u64;
        let avg_row_bytes = mysql_table_avg_row_length(&mut conn, &ctx.endpoint, &table.name);
        if let Some(pk_column) = single_numeric_primary_key(table) {
            let profile_key =
                dump_profile_key(&ctx.endpoint, &table.name, &ctx.data_format, &ctx.compression);
            let range_chunk_size = learned_mysql_range_chunk_size(
                ctx.chunk_size,
                avg_row_bytes,
                profiles.get(&profile_key),
            );
            if let Some((min_key, max_key)) =
                mysql_numeric_min_max(&mut conn, &table.name, pk_column)?
            {
                if should_use_pk_range_dump_for_span(
                    table,
                    table_row_count,
                    range_chunk_size,
                    min_key,
                    max_key,
                ) {
                    let ranges = pk_ranges(min_key, max_key, table_row_count, range_chunk_size);
                    chunks_total = ranges.len() as u64;
                    ranges_by_table.insert(table.name.clone(), ranges);
                    emit(json!({
                        "event": "table_progress",
                        "request_id": ctx.request_id,
                        "table": table.name,
                        "status": "dumping",
                        "current": index + 1,
                        "total": table_total,
                        "strategy": "global_pk_range_parallel",
                        "range_chunk_size": range_chunk_size,
                        "target_bytes_per_chunk": MYSQL_DUMP_TARGET_BYTES_PER_CHUNK,
                        "avg_row_bytes": avg_row_bytes
                    }));
                }
            }
        }
        states.push(DumpGlobalTableState {
            table_path,
            rows_total: table_row_count,
            rows_dumped: 0,
            chunks_total,
            chunks_done: 0,
            avg_row_bytes,
            work_ms: 0,
            chunk_sha256: BTreeMap::new(),
            manifest: None,
        });
    }
    workers.push(conn);

    let plan = global_dump_work_plan_for_ranges(tables, &ranges_by_table);
    let table_index_by_name = tables
        .iter()
        .enumerate()
        .map(|(index, table)| (table.name.clone(), index))
        .collect::<BTreeMap<_, _>>();
    let mut pending = VecDeque::<DumpGlobalWorkItem>::new();
    for item in plan {
        let Some(&table_index) = table_index_by_name.get(&item.table) else {
            continue;
        };
        let table = tables[table_index].clone();
        let kind = if let Some(chunk_index) = item.chunk_index {
            let Some(pk_column) = single_numeric_primary_key(&table) else {
                continue;
            };
            let Some(ranges) = ranges_by_table.get(&table.name) else {
                continue;
            };
            let Some(range) = ranges.get((chunk_index - 1) as usize).cloned() else {
                continue;
            };
            DumpGlobalWorkKind::MysqlRange {
                table_path: states[table_index].table_path.clone(),
                pk_column: pk_column.to_string(),
                range,
            }
        } else {
            DumpGlobalWorkKind::WholeTable
        };
        pending.push_back(DumpGlobalWorkItem {
            table_index,
            table,
            kind,
        });
    }

    let work_total = pending.len();
    if work_total == 0 {
        return Ok((Vec::new(), 0, 0));
    }
    let max_threads = workers.len().max(1).min(work_total);
    let mut first_error: Option<String> = None;
    let (sender, receiver) = mpsc::channel::<DumpGlobalEvent>();
    let pending = Arc::new(Mutex::new(pending));
    let mut handles = Vec::with_capacity(max_threads);
    for conn in workers.into_iter().take(max_threads) {
        let pending = Arc::clone(&pending);
        let sender = sender.clone();
        let ctx = ctx.clone();
        handles.push(thread::spawn(move || {
            let mut adapter = LiveAdapter::MySql(conn);
            loop {
                let work = pending.lock().ok().and_then(|mut queue| queue.pop_front());
                let Some(work) = work else { break };
                run_dump_global_work(
                    &mut adapter,
                    &ctx,
                    work,
                    table_total,
                    &sender,
                );
            }
            let _ = adapter.execute_sql("ROLLBACK");
        }));
    }
    drop(sender);

    let mut completed = 0_usize;
    while completed < work_total {
        let event = receiver
            .recv()
            .map_err(|_| "mysql snapshot worker pool stopped unexpectedly".to_string())?;
        match event {
            DumpGlobalEvent::Progress(event) => {
                emit(event);
            }
            DumpGlobalEvent::RangeDone {
                table_index,
                chunk_index,
                rows,
                stream_ms,
                range_start,
                range_end,
                checksum,
            } => {
                let table = &tables[table_index];
                let state = &mut states[table_index];
                state.rows_dumped += rows;
                state.chunks_done += 1;
                state.work_ms = state.work_ms.saturating_add(stream_ms.max(1));
                state.chunk_sha256.insert(
                    dump_chunk_name(chunk_index, &ctx.data_format, &ctx.compression),
                    checksum,
                );
                emit(json!({
                    "event": "row_progress",
                    "request_id": ctx.request_id,
                    "table": table.name,
                    "rows": state.rows_dumped,
                    "total": state.rows_total,
                    "chunk_rows": rows,
                    "chunks_done": state.chunks_done,
                    "chunks_total": state.chunks_total,
                    "stream_ms": stream_ms,
                    "chunk_index": chunk_index,
                    "range_start": range_start,
                    "range_end": range_end,
                    "strategy": "global_pk_range_parallel"
                }));
                if state.chunks_done == state.chunks_total {
                    state.manifest = Some(DumpTableManifest {
                        name: table.name.clone(),
                        path: state.table_path.clone(),
                        rows: state.rows_dumped,
                        chunks: state.chunks_done,
                        chunk_sha256: state.chunk_sha256.clone(),
                    });
                    emit(json!({
                        "event": "table_progress",
                        "request_id": ctx.request_id,
                        "table": table.name,
                        "status": "completed",
                        "current": table_index + 1,
                        "total": table_total,
                        "strategy": "global_pk_range_parallel"
                    }));
                }
                completed += 1;
            }
            DumpGlobalEvent::TableDone {
                index,
                manifest,
                rows,
                chunks,
                duration_ms,
            } => {
                let state = &mut states[index];
                state.rows_dumped = rows;
                state.chunks_done = chunks;
                state.chunks_total = chunks;
                state.work_ms = duration_ms.max(1);
                state.manifest = Some(manifest);
                completed += 1;
            }
            DumpGlobalEvent::Error(err) => {
                first_error.get_or_insert(err);
                completed += 1;
            }
        }
    }
    for handle in handles {
        let _ = handle.join();
    }
    if let Some(err) = first_error {
        return Err(err);
    }

    let mut profiles = profiles;
    for (index, table) in tables.iter().enumerate() {
        let state = &states[index];
        if state.rows_dumped > 0 {
            let duration_ms = state.work_ms.max(1);
            let rows_per_second = state.rows_dumped.saturating_mul(1000) / duration_ms;
            profiles.insert(
                dump_profile_key(&ctx.endpoint, &table.name, &ctx.data_format, &ctx.compression),
                DumpTablePerfProfile {
                    avg_row_bytes: state.avg_row_bytes,
                    chunk_rows: if state.chunks_done > 0 {
                        (state.rows_dumped / state.chunks_done).max(1) as usize
                    } else {
                        ctx.chunk_size
                    },
                    rows_per_second,
                    duration_ms,
                },
            );
        }
    }
    save_dump_perf_profiles(&profiles);

    let manifests = states
        .into_iter()
        .map(|state| state.manifest)
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| "global dump did not produce all table manifests".to_string())?;
    let total_rows = manifests.iter().map(|table| table.rows).sum();
    let total_chunks = manifests.iter().map(|table| table.chunks).sum();
    Ok((manifests, total_rows, total_chunks))
}

fn dump_mysql_table_parallel_ranges<F: FnMut(Value)>(
    ctx: &DumpJobContext,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    threads: usize,
    mut emit: F,
) -> Result<Option<(DumpTableManifest, u64, u64)>, String> {
    let Some(pk_column) = single_numeric_primary_key(table) else {
        return Ok(None);
    };

    let mut conn = match LiveAdapter::connect(&ctx.endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => return Ok(None),
    };
    let table_row_count = conn
        .query_first::<u64, _>(count_sql("mysql", &table.name))
        .map(|count| count.unwrap_or(0))
        .unwrap_or(0);
    let avg_row_bytes = mysql_table_avg_row_length(&mut conn, &ctx.endpoint, &table.name);
    let range_chunk_size = mysql_range_chunk_size_for_avg_row(ctx.chunk_size, avg_row_bytes);
    if !should_use_pk_range_dump(table, table_row_count, range_chunk_size) {
        return Ok(None);
    }
    let Some((min_key, max_key)) = mysql_numeric_min_max(&mut conn, &table.name, pk_column)? else {
        return Ok(None);
    };
    if !should_use_pk_range_dump_for_span(
        table,
        table_row_count,
        range_chunk_size,
        min_key,
        max_key,
    ) {
        return Ok(None);
    }

    emit(json!({
        "event": "table_progress",
        "request_id": ctx.request_id,
        "table": table.name,
        "status": "dumping",
        "current": index + 1,
        "total": table_total,
        "strategy": "pk_range_parallel",
        "range_chunk_size": range_chunk_size,
        "target_bytes_per_chunk": MYSQL_DUMP_TARGET_BYTES_PER_CHUNK,
        "avg_row_bytes": avg_row_bytes
    }));

    let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
    let table_dir = ctx.output_path.join(&table_path);
    fs::create_dir_all(&table_dir)
        .map_err(|err| format!("failed to create dump table dir: {err}"))?;
    let ranges = pk_ranges(min_key, max_key, table_row_count, range_chunk_size);
    let total_ranges = ranges.len();
    let max_threads = threads.max(1).min(total_ranges.max(1));
    let pending = ranges.into_iter().collect::<VecDeque<_>>();
    let mut rows_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();
    let mut first_error: Option<String> = None;
    // chunks_done는 기존 `completed`와 동일하게 매 종료 이벤트(Done+Error)마다 증가하며
    // row_progress의 "chunks_done" 필드에 쓰인다. 풀 루프 카운터는 run_bounded_pool가 관리한다.
    let mut chunks_done = 0_u64;
    let (sender, receiver) = mpsc::channel::<DumpRangeEvent>();

    run_bounded_pool(
        pending,
        max_threads,
        &receiver,
        |range| {
            spawn_mysql_range_worker(
                ctx.endpoint.clone(),
                ctx.output_path.clone(),
                table.clone(),
                table_path.clone(),
                pk_column.to_string(),
                range,
                ctx.data_format.clone(),
                ctx.compression.clone(),
                sender.clone(),
            )
        },
        |event| match event {
            DumpRangeEvent::Done {
                chunk_index,
                rows,
                stream_ms,
                range_start,
                range_end,
                checksum,
            } => {
                rows_dumped += rows;
                chunks_done += 1;
                chunk_sha256.insert(
                    dump_chunk_name(chunk_index, &ctx.data_format, &ctx.compression),
                    checksum,
                );
                emit(json!({
                    "event": "row_progress",
                    "request_id": ctx.request_id,
                    "table": table.name,
                    "rows": rows_dumped,
                    "total": table_row_count,
                    "chunk_rows": rows,
                    "chunks_done": chunks_done,
                    "chunks_total": total_ranges,
                    "stream_ms": stream_ms,
                    "chunk_index": chunk_index,
                    "range_start": range_start,
                    "range_end": range_end,
                    "strategy": "pk_range_parallel"
                }));
                PoolAction::Advance
            }
            // range 워커 에러는 리필하지 않는다(기존 동작 보존). chunks_done는 계속 카운트.
            DumpRangeEvent::Error(err) => {
                first_error.get_or_insert(err);
                chunks_done += 1;
                PoolAction::AdvanceNoRefill
            }
        },
    );
    if let Some(err) = first_error {
        return Err(err);
    }

    emit(json!({
        "event": "table_progress",
        "request_id": ctx.request_id,
        "table": table.name,
        "status": "completed",
        "current": index + 1,
        "total": table_total,
        "strategy": "pk_range_parallel"
    }));

    Ok(Some((
        DumpTableManifest {
            name: table.name.clone(),
            path: table_path,
            rows: rows_dumped,
            chunks: total_ranges as u64,
            chunk_sha256,
        },
        rows_dumped,
        total_ranges as u64,
    )))
}

fn spawn_mysql_range_worker(
    endpoint: Endpoint,
    output_path: std::path::PathBuf,
    table: NormalizedTable,
    table_path: String,
    pk_column: String,
    range: DumpRange,
    data_format: String,
    compression: String,
    sender: mpsc::Sender<DumpRangeEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = dump_mysql_range_chunk(
            &endpoint,
            &output_path,
            &table,
            &table_path,
            &pk_column,
            &range,
            &data_format,
            &compression,
        );
        match result {
            Ok((rows, stream_ms, checksum)) => {
                let _ = sender.send(DumpRangeEvent::Done {
                    chunk_index: range.chunk_index,
                    rows,
                    stream_ms,
                    range_start: range.start.to_string(),
                    range_end: range.end.to_string(),
                    checksum,
                });
            }
            Err(err) => {
                let _ = sender.send(DumpRangeEvent::Error(err));
            }
        }
    })
}

fn run_dump_global_work(
    adapter: &mut LiveAdapter,
    ctx: &DumpJobContext,
    work: DumpGlobalWorkItem,
    table_total: usize,
    sender: &mpsc::Sender<DumpGlobalEvent>,
) {
    match work.kind {
        DumpGlobalWorkKind::MysqlRange {
            table_path,
            pk_column,
            range,
        } => {
            let result = match adapter {
                LiveAdapter::MySql(conn) => dump_mysql_range_chunk_on_conn(
                    conn,
                    &ctx.output_path,
                    &work.table,
                    &table_path,
                    &pk_column,
                    &range,
                    &ctx.data_format,
                    &ctx.compression,
                ),
                LiveAdapter::PostgreSql(_) => {
                    Err("global mysql worker received a PostgreSQL connection".to_string())
                }
            };
            match result {
                Ok((rows, stream_ms, checksum)) => {
                    let _ = sender.send(DumpGlobalEvent::RangeDone {
                        table_index: work.table_index,
                        chunk_index: range.chunk_index,
                        rows,
                        stream_ms,
                        range_start: range.start.to_string(),
                        range_end: range.end.to_string(),
                        checksum,
                    });
                }
                Err(err) => {
                    let _ = sender.send(DumpGlobalEvent::Error(err));
                }
            }
        }
        DumpGlobalWorkKind::WholeTable => {
            let started = Instant::now();
            let result = dump_one_table(
                adapter,
                ctx,
                &work.table,
                work.table_index,
                table_total,
                |event| {
                    let _ = sender.send(DumpGlobalEvent::Progress(event));
                },
            )
            .map(|(manifest, rows, chunks)| {
                (
                    manifest,
                    rows,
                    chunks,
                    started.elapsed().as_millis().max(1) as u64,
                )
            });
            match result {
                Ok((manifest, rows, chunks, duration_ms)) => {
                    let _ = sender.send(DumpGlobalEvent::TableDone {
                        index: work.table_index,
                        manifest,
                        rows,
                        chunks,
                        duration_ms,
                    });
                }
                Err(err) => {
                    let _ = sender.send(DumpGlobalEvent::Error(err));
                }
            }
        }
    }
}

fn dump_mysql_range_chunk_on_conn(
    conn: &mut mysql::PooledConn,
    output_path: &Path,
    table: &NormalizedTable,
    table_path: &str,
    pk_column: &str,
    range: &DumpRange,
    data_format: &str,
    compression: &str,
) -> Result<(u64, u64, String), String> {
    let chunk_path = output_path.join(table_path).join(dump_chunk_name(
        range.chunk_index,
        data_format,
        compression,
    ));
    let columns = column_names(table);
    let sql = select_chunk_text_range_sql("mysql", table, pk_column, range.start, range.end);
    let stream_started = Instant::now();
    let result = conn
        .query_iter(sql)
        .map_err(|err| format!("mysql range select chunk error: {err}"))?;
    let mut rows = 0_u64;
    {
        let mut file = open_dump_writer(&chunk_path, compression)?;
        for row in result {
            let row = row.map_err(|err| format!("mysql dump row error: {err}"))?;
            if data_format == "tsv" {
                write_mysql_text_row_tsv(&mut file, row)?;
            } else {
                let row_json = mysql_row_to_json(&columns, row);
                write_dump_row(&mut file, table, &row_json, data_format)?;
            }
            rows += 1;
        }
    }
    let checksum = sha256_file(&chunk_path)?;
    Ok((rows, stream_started.elapsed().as_millis() as u64, checksum))
}

fn dump_mysql_range_chunk(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    table_path: &str,
    pk_column: &str,
    range: &DumpRange,
    data_format: &str,
    compression: &str,
) -> Result<(u64, u64, String), String> {
    let mut conn = match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => {
            return Err("pk range dump requires mysql endpoint".to_string())
        }
    };
    dump_mysql_range_chunk_on_conn(
        &mut conn,
        output_path,
        table,
        table_path,
        pk_column,
        range,
        data_format,
        compression,
    )
}

fn spawn_dump_table_worker(
    ctx: DumpJobContext,
    table: NormalizedTable,
    index: usize,
    table_total: usize,
    range_threads: usize,
    sender: mpsc::Sender<DumpTableEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = (|| {
            if ctx.endpoint.engine == "mysql" {
                if let Some(result) = dump_mysql_table_parallel_ranges(
                    &ctx,
                    &table,
                    index,
                    table_total,
                    range_threads,
                    |event| {
                        let _ = sender.send(DumpTableEvent::Progress(event));
                    },
                )? {
                    return Ok(result);
                }
            }
            let mut adapter = LiveAdapter::connect(&ctx.endpoint)?;
            dump_one_table(
                &mut adapter,
                &ctx,
                &table,
                index,
                table_total,
                |event| {
                    let _ = sender.send(DumpTableEvent::Progress(event));
                },
            )
        })();
        match result {
            Ok((manifest, rows, chunks)) => {
                let _ = sender.send(DumpTableEvent::Done {
                    index,
                    manifest,
                    rows,
                    chunks,
                });
            }
            Err(err) => {
                let _ = sender.send(DumpTableEvent::Error(err));
            }
        }
    })
}

/// `run_table_dump_loop`가 한 청크를 기록한 결과. rows는 이 청크의 행 수,
/// chunk_name은 sha256 맵의 키, checksum은 청크 파일 해시.
struct ChunkOutcome {
    rows: u64,
    chunk_name: String,
    checksum: String,
}

/// 단일 테이블 덤프의 공통 스캐폴딩: dumping/completed table_progress 이벤트,
/// `{index+1:04}_{safe_name}` 디렉토리 생성, 청크 누적(rows/chunks/sha256), manifest 조립.
///
/// 엔진별로 다른 청크 read+write 로직은 `fetch_next_chunk` 클로저가 공급한다.
/// 클로저는 (다음 청크 번호, 지금까지 누적 rows, table_dir, emit)을 받아 한 청크를
/// 파일로 기록하고 `ChunkOutcome`을 반환하거나, 더 이상 쓸 데이터가 없으면 `None`을
/// 반환한다. row_progress 이벤트는 청크 필드가 엔진마다 달라 클로저가 직접 emit한다.
fn run_table_dump_loop<F: FnMut(Value)>(
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    output_path: &Path,
    request_id: Option<String>,
    mut emit: F,
    mut fetch_next_chunk: impl FnMut(
        u64,
        u64,
        &Path,
        &mut dyn FnMut(Value),
    ) -> Result<Option<ChunkOutcome>, String>,
) -> Result<(DumpTableManifest, u64, u64), String> {
    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "dumping",
        "current": index + 1,
        "total": table_total
    }));
    let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
    let table_dir = output_path.join(&table_path);
    fs::create_dir_all(&table_dir)
        .map_err(|err| format!("failed to create dump table dir: {err}"))?;

    let mut rows_dumped = 0_u64;
    let mut chunks_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();

    loop {
        match fetch_next_chunk(chunks_dumped + 1, rows_dumped, &table_dir, &mut emit)? {
            Some(outcome) => {
                chunk_sha256.insert(outcome.chunk_name, outcome.checksum);
                rows_dumped += outcome.rows;
                chunks_dumped += 1;
            }
            None => break,
        }
    }

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "completed",
        "current": index + 1,
        "total": table_total
    }));

    Ok((
        DumpTableManifest {
            name: table.name.clone(),
            path: table_path,
            rows: rows_dumped,
            chunks: chunks_dumped,
            chunk_sha256,
        },
        rows_dumped,
        chunks_dumped,
    ))
}

fn dump_one_table<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    ctx: &DumpJobContext,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    emit: F,
) -> Result<(DumpTableManifest, u64, u64), String> {
    if let LiveAdapter::MySql(conn) = adapter {
        return dump_one_mysql_table(conn, ctx, table, index, table_total, emit);
    }

    let table_row_count = adapter.row_count(&table.name).unwrap_or(0) as u64;
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut last_key: Option<String> = None;
    let mut offset = 0_usize;

    run_table_dump_loop(
        table,
        index,
        table_total,
        &ctx.output_path,
        ctx.request_id.clone(),
        emit,
        |chunk_number: u64,
         rows_dumped_before: u64,
         table_dir: &Path,
         emit: &mut dyn FnMut(Value)|
         -> Result<Option<ChunkOutcome>, String> {
            let Some(read_limit) =
                bounded_dump_chunk_limit(table_row_count, rows_dumped_before, ctx.chunk_size)
            else {
                return Ok(None);
            };
            let read_started = Instant::now();
            let rows = if use_keyset {
                adapter.read_rows_after_key(table, &key_columns, last_key.as_deref(), read_limit)?
            } else {
                adapter.read_rows(table, offset, read_limit)?
            };
            let read_ms = read_started.elapsed().as_millis() as u64;
            if rows.is_empty() {
                return Ok(None);
            }
            let chunk_name = dump_chunk_name(chunk_number, &ctx.data_format, &ctx.compression);
            let write_started = Instant::now();
            let checksum = write_dump_rows(
                &table_dir.join(&chunk_name),
                table,
                &rows,
                &ctx.data_format,
                &ctx.compression,
            )?;
            let write_ms = write_started.elapsed().as_millis() as u64;

            let copied_now = rows.len();
            if use_keyset {
                last_key = rows.last().and_then(|row| row_key_token(row, &key_columns));
            } else {
                offset += copied_now;
            }

            emit(json!({
                "event": "row_progress",
                "request_id": ctx.request_id,
                "table": table.name,
                "rows": rows_dumped_before + copied_now as u64,
                "total": table_row_count,
                "chunk_rows": copied_now,
                "read_ms": read_ms,
                "write_ms": write_ms
            }));
            Ok(Some(ChunkOutcome {
                rows: copied_now as u64,
                chunk_name,
                checksum,
            }))
        },
    )
}

fn dump_one_mysql_table<F: FnMut(Value)>(
    conn: &mut mysql::PooledConn,
    ctx: &DumpJobContext,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    emit: F,
) -> Result<(DumpTableManifest, u64, u64), String> {
    let table_row_count = conn
        .query_first::<u64, _>(count_sql("mysql", &table.name))
        .map(|count| count.unwrap_or(0))
        .unwrap_or(0);
    // 청크당 행 수를 바이트 목표(≈64MB) + 절대 행수 상한으로 산출한다. 대형 TEXT/JSON
    // 컬럼 테이블에서 하나의 result set가 과대해져 스트리밍 코덱이 크래시하는 것을 막는다.
    // 병렬 경로와 동일한 avg-row-length 헬퍼를 재사용하며, 조회 실패/통계 부재 시
    // avg=0 → fallback(chunk_size) → 상한이 지배하도록 안전하게 degrade한다.
    let avg_row_bytes = mysql_table_avg_row_length(conn, &ctx.endpoint, &table.name);
    let effective_chunk_size = sequential_mysql_chunk_size(ctx.chunk_size, avg_row_bytes);
    let columns = column_names(table);
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut last_key: Option<String> = None;
    let mut offset = 0_usize;

    run_table_dump_loop(
        table,
        index,
        table_total,
        &ctx.output_path,
        ctx.request_id.clone(),
        emit,
        |chunk_number: u64,
         rows_dumped_before: u64,
         table_dir: &Path,
         emit: &mut dyn FnMut(Value)|
         -> Result<Option<ChunkOutcome>, String> {
            let Some(read_limit) =
                bounded_dump_chunk_limit(table_row_count, rows_dumped_before, effective_chunk_size)
            else {
                return Ok(None);
            };
            let chunk_name = dump_chunk_name(chunk_number, &ctx.data_format, &ctx.compression);
            let chunk_path = table_dir.join(&chunk_name);

            let stream_started = Instant::now();
            let last_values = last_key.as_deref().and_then(decode_key_token);
            let sql = if use_keyset {
                select_chunk_text_after_key_sql(
                    "mysql",
                    table,
                    &key_columns,
                    last_values.as_deref(),
                    read_limit,
                )
            } else {
                select_chunk_text_sql("mysql", table, &key_columns)
            };
            let sql = if use_keyset {
                sql
            } else {
                sql.replacen('?', &(read_limit as u64).to_string(), 1)
                    .replacen('?', &(offset as u64).to_string(), 1)
            };
            let result = if use_keyset {
                conn.query_iter(sql)
                    .map_err(|err| format!("mysql keyset select chunk error: {err}"))?
            } else {
                conn.query_iter(sql)
                    .map_err(|err| format!("mysql select chunk error: {err}"))?
            };

            let mut chunk_rows = 0_usize;
            let mut next_key: Option<String> = None;
            {
                let mut file = open_dump_writer(&chunk_path, &ctx.compression)?;
                for row in result {
                    let row = row.map_err(|err| format!("mysql dump row error: {err}"))?;
                    if ctx.data_format == "tsv" && !use_keyset {
                        write_mysql_text_row_tsv(&mut file, row)?;
                    } else {
                        let row_json = mysql_row_to_json(&columns, row);
                        if use_keyset {
                            next_key = row_key_token(&row_json, &key_columns);
                        }
                        write_dump_row(&mut file, table, &row_json, &ctx.data_format)?;
                    }
                    chunk_rows += 1;
                }
            }
            let stream_ms = stream_started.elapsed().as_millis() as u64;

            if chunk_rows == 0 {
                fs::remove_file(&chunk_path).ok();
                return Ok(None);
            }
            let checksum = sha256_file(&chunk_path)?;

            if use_keyset {
                last_key = next_key;
            } else {
                offset += chunk_rows;
            }

            emit(json!({
                "event": "row_progress",
                "request_id": ctx.request_id,
                "table": table.name,
                "rows": rows_dumped_before + chunk_rows as u64,
                "total": table_row_count,
                "chunk_rows": chunk_rows,
                "stream_ms": stream_ms
            }));
            Ok(Some(ChunkOutcome {
                rows: chunk_rows as u64,
                chunk_name,
                checksum,
            }))
        },
    )
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    
    use std::collections::BTreeMap;
    use std::fs::{self};
    
    use std::path::Path;
    
    
    
    
    
    use crate::adapters::test_support::{empty_table, schema};

    #[test]
    fn dump_overwrite_rejects_non_dump_directory() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-overwrite-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let keep_file = dir.join("keep.txt");
        fs::write(&keep_file, "keep").unwrap();

        let err = prepare_dump_output_dir(&dir, true).unwrap_err();

        assert!(err.contains("refusing to overwrite"));
        assert!(keep_file.exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_overwrite_requires_manifest_and_marker() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-marker-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("_tunnelforge_dump.json"),
            r#"{"format":"tunnelforge-dump"}"#,
        )
        .unwrap();

        let err = prepare_dump_output_dir(&dir, true).unwrap_err();

        assert!(err.contains("without TunnelForge dump marker"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_overwrite_allows_marked_dump_directory() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-overwrite-marker-ok-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: Vec::new(),
        };
        write_dump_manifest(&dir, &manifest).unwrap();
        fs::write(dir.join("old_chunk.tsv"), b"old").unwrap();

        prepare_dump_output_dir(&dir, true).unwrap();

        assert!(dir.is_dir());
        assert!(!dir.join("old_chunk.tsv").exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_output_rejects_empty_directory_path() {
        let err = prepare_dump_output_dir(Path::new(""), false).unwrap_err();

        assert!(err.contains("unsafe dump output_dir"));
    }

    #[test]
    fn dump_plan_event_reports_table_and_row_totals() {
        let schema = schema();
        let mut counts = BTreeMap::new();
        counts.insert("users".to_string(), 42_u64);

        let event = dump_plan_event(Some("req-1".to_string()), &schema.tables, &counts);

        assert_eq!(event["event"], "dump_plan");
        assert_eq!(event["request_id"], "req-1");
        assert_eq!(event["tables_total"], 1);
        assert_eq!(event["rows_total"], 42);
        assert_eq!(event["tables"][0]["name"], "users");
        assert_eq!(event["tables"][0]["rows"], 42);
    }

    #[test]
    fn mysql_snapshot_setup_uses_repeatable_read_read_only_transactions() {
        assert_eq!(
            mysql_snapshot_worker_setup_sql(),
            [
                "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
            ]
        );
    }

    #[test]
    fn mysql_snapshot_lock_sequence_releases_global_lock_before_dumping() {
        assert_eq!(
            mysql_snapshot_coordinator_sql(),
            [
                "SET SESSION lock_wait_timeout=2",
                "FLUSH TABLES WITH READ LOCK",
                "LOCK INSTANCE FOR BACKUP",
                "UNLOCK TABLES",
            ]
        );
    }

    #[test]
    fn mysql_shared_snapshot_rejects_non_transactional_tables() {
        let rows = vec![
            ("users".to_string(), "InnoDB".to_string()),
            ("legacy_cache".to_string(), "MyISAM".to_string()),
        ];

        assert_eq!(
            non_transactional_mysql_tables(&rows),
            vec!["legacy_cache (MyISAM)".to_string()]
        );
    }

    #[test]
    fn dump_import_row_progress_event_reports_table_and_overall_rows() {
        let event = dump_import_row_progress_event(
            Some("import-1".to_string()),
            "orders",
            25,
            100,
            1_000,
            2_000,
            25,
            ChunkProgress {
                chunks_done: Some(2),
                chunks_total: Some(8),
                chunk_index: Some(4),
                load_ms: Some(500),
            },
            "load_data_local_infile",
        );

        assert_eq!(event["event"], "row_progress");
        assert_eq!(event["request_id"], "import-1");
        assert_eq!(event["table"], "orders");
        assert_eq!(event["rows"], 25);
        assert_eq!(event["total"], 100);
        assert_eq!(event["table_rows_done"], 25);
        assert_eq!(event["table_rows_total"], 100);
        assert_eq!(event["overall_rows_done"], 1_025);
        assert_eq!(event["overall_rows_total"], 2_000);
        assert_eq!(event["chunk_rows"], 25);
        assert_eq!(event["chunks_done"], 2);
        assert_eq!(event["chunks_total"], 8);
        assert_eq!(event["chunk_index"], 4);
        assert_eq!(event["load_ms"], 500);
        assert_eq!(event["strategy"], "load_data_local_infile");
    }

    #[test]
    fn dump_schedule_event_reports_adaptive_workers_and_top_tables() {
        let tables = vec![
            empty_table("huge", Vec::new()),
            empty_table("medium", Vec::new()),
            empty_table("tiny", Vec::new()),
        ];
        let mut counts = BTreeMap::new();
        counts.insert("huge".to_string(), 2_000_000);
        counts.insert("medium".to_string(), 500_000);
        counts.insert("tiny".to_string(), 10);
        let limits = dump_parallel_limits(8, 3);

        let event = dump_schedule_event(
            Some("req-1".to_string()),
            &tables,
            &counts,
            limits,
            8,
            50_000,
            "tsv",
            "zstd",
            "global_chunk",
        );

        assert_eq!(event["event"], "dump_schedule");
        assert_eq!(event["scheduler"], "global_chunk");
        assert_eq!(event["compression"], "zstd");
        assert_eq!(event["table_workers"], limits.table_workers);
        assert_eq!(
            event["range_workers_per_table"],
            limits.range_workers_per_table
        );
        assert_eq!(event["scheduled_tables"][0]["name"], "huge");
        assert_eq!(event["scheduled_tables"][0]["estimated_chunks"], 40);
    }

    #[test]
    fn full_schema_dump_splits_thread_budget_between_tables_and_ranges() {
        let limits = dump_parallel_limits(16, 208);

        assert_eq!(limits.table_workers, 4);
        assert_eq!(limits.range_workers_per_table, 4);
        assert!(limits.estimated_mysql_connections() <= 20);
    }

    #[test]
    fn eight_thread_full_schema_prefers_range_parallelism_for_large_tables() {
        let limits = dump_parallel_limits(8, 208);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
        assert!(limits.estimated_mysql_connections() <= 10);
    }

    #[test]
    fn adaptive_dump_limits_prioritize_range_workers_for_heavy_chunked_tables() {
        let limits = dump_parallel_limits(8, 208);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_use_byte_chunks_for_wide_tables() {
        let limits = dump_parallel_limits(8, 208);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_keep_table_parallelism_for_pathological_wide_table() {
        let limits = dump_parallel_limits(8, 208);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_keep_multiple_heavy_tables_in_parallel() {
        let limits = dump_parallel_limits(8, 208);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn small_table_selection_keeps_range_parallelism_within_thread_budget() {
        let limits = dump_parallel_limits(16, 2);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 8);
        assert!(limits.estimated_mysql_connections() <= 18);
    }

    #[test]
    fn single_table_dump_uses_full_range_parallelism() {
        let limits = dump_parallel_limits(16, 1);

        assert_eq!(limits.table_workers, 1);
        assert_eq!(limits.range_workers_per_table, 16);
        assert!(limits.estimated_mysql_connections() <= 17);
    }

    #[test]
    fn dump_scheduler_starts_largest_estimated_tables_first() {
        let mut tables = vec![
            NormalizedTable {
                name: "tiny".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            },
            NormalizedTable {
                name: "huge".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            },
            NormalizedTable {
                name: "medium".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            },
        ];
        let mut counts = BTreeMap::new();
        counts.insert("tiny".to_string(), 10);
        counts.insert("huge".to_string(), 1_000_000);
        counts.insert("medium".to_string(), 50_000);

        tables = dump_schedule_order(&tables, &counts);

        assert_eq!(
            tables
                .iter()
                .map(|table| table.name.as_str())
                .collect::<Vec<_>>(),
            vec!["huge", "medium", "tiny"]
        );
    }

    #[test]
    fn global_dump_work_plan_mixes_range_chunks_and_whole_table_jobs() {
        let mut range_chunks = BTreeMap::new();
        range_chunks.insert("huge".to_string(), 4_u64);
        range_chunks.insert("wide".to_string(), 2_u64);
        let tables = vec![
            empty_table("huge", Vec::new()),
            empty_table("small_lookup", Vec::new()),
            empty_table("wide", Vec::new()),
        ];

        let plan = global_dump_work_plan(&tables, &range_chunks);

        assert_eq!(
            plan.iter()
                .map(|item| item.table.as_str())
                .collect::<Vec<_>>(),
            vec![
                "huge",
                "wide",
                "small_lookup",
                "huge",
                "wide",
                "huge",
                "huge"
            ]
        );
        assert_eq!(
            plan.iter().map(|item| item.chunk_index).collect::<Vec<_>>(),
            vec![Some(1), Some(1), None, Some(2), Some(2), Some(3), Some(4)]
        );
    }

    #[test]
    fn dump_chunk_limit_stops_at_initial_row_count_when_source_grows() {
        assert_eq!(bounded_dump_chunk_limit(2, 0, 50_000), Some(2));
        assert_eq!(bounded_dump_chunk_limit(2, 2, 50_000), None);
        assert_eq!(
            bounded_dump_chunk_limit(120_000, 50_000, 50_000),
            Some(50_000)
        );
        assert_eq!(
            bounded_dump_chunk_limit(120_000, 100_000, 50_000),
            Some(20_000)
        );
    }
}
