use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::Instant;

use mysql::prelude::Queryable;
use crate::*;

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

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct DumpTableStats {
    rows: u64,
    avg_row_bytes: u64,
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

fn dump_table_stats(
    endpoint: &Endpoint,
    tables: &[NormalizedTable],
) -> BTreeMap<String, DumpTableStats> {
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
        "SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0), COALESCE(AVG_ROW_LENGTH, 0) FROM information_schema.tables WHERE TABLE_SCHEMA = {} AND TABLE_NAME IN ({})",
        sql_literal(&Value::String(schema_name)),
        table_names
    );
    let Ok(rows) = conn.query::<(String, u64, u64), _>(sql) else {
        return counts;
    };
    for (table, rows, avg_row_bytes) in rows {
        counts.insert(
            table,
            DumpTableStats {
                rows,
                avg_row_bytes,
            },
        );
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

pub(crate) fn dump_import_row_progress_event(
    request_id: Option<String>,
    table: &str,
    table_rows_done: u64,
    table_rows_total: u64,
    overall_rows_before: u64,
    overall_rows_total: u64,
    chunk_rows: u64,
    chunks_done: Option<u64>,
    chunks_total: Option<u64>,
    chunk_index: Option<u64>,
    load_ms: Option<u64>,
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
        if let Some(value) = chunks_done {
            fields.insert("chunks_done".to_string(), json!(value));
        }
        if let Some(value) = chunks_total {
            fields.insert("chunks_total".to_string(), json!(value));
        }
        if let Some(value) = chunk_index {
            fields.insert("chunk_index".to_string(), json!(value));
        }
        if let Some(value) = load_ms {
            fields.insert("load_ms".to_string(), json!(value));
        }
    }

    event
}

fn dump_run<F: FnMut(Value)>(request: &Request, mut emit: F) -> Result<Value, String> {
    let endpoint = request_endpoint(request)?;
    let output_dir = request
        .payload
        .get("output_dir")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "dump.run requires output_dir".to_string())?;
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
        .unwrap_or(8)
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

    let output_path = Path::new(output_dir);
    prepare_dump_output_dir(output_path, overwrite)?;

    // 부분 export(tables 지정) 시에는 View가 참조하는 base table이 빠질 수 있으므로 View를 수집하지 않는다.
    let full_export = selected_tables.is_empty();
    let inspection = inspect_live(&endpoint)?;
    let mut schema = inspection.schema;
    if !selected_tables.is_empty() {
        let selected: BTreeSet<String> = selected_tables.into_iter().collect();
        schema.tables.retain(|table| selected.contains(&table.name));
    }
    schema = dependency_ordered_schema(&schema);
    if schema.tables.is_empty() {
        return Err("dump.run found no tables to export".to_string());
    }

    let table_stats = dump_table_stats(&endpoint, &schema.tables);
    let row_counts = table_stats
        .iter()
        .map(|(table, stats)| (table.clone(), stats.rows))
        .collect::<BTreeMap<_, _>>();
    let range_eligible_tables = schema
        .tables
        .iter()
        .filter(|table| single_numeric_primary_key(table).is_some())
        .map(|table| table.name.clone())
        .collect::<BTreeSet<_>>();
    let avg_row_lengths = table_stats
        .iter()
        .filter(|(table, _)| range_eligible_tables.contains(*table))
        .map(|(table, stats)| (table.clone(), stats.avg_row_bytes))
        .collect::<BTreeMap<_, _>>();
    emit(dump_plan_event(
        request.request_id.clone(),
        &schema.tables,
        &row_counts,
    ));

    let table_total = schema.tables.len();
    let parallel_limits = adaptive_dump_parallel_limits_with_avg(
        threads,
        table_total,
        chunk_size,
        &row_counts,
        &avg_row_lengths,
    );
    let export_tables = if threads > 1 && table_total > 1 {
        dump_schedule_order(&schema.tables, &row_counts)
    } else {
        schema.tables.clone()
    };
    emit(dump_schedule_event(
        request.request_id.clone(),
        &export_tables,
        &row_counts,
        parallel_limits,
        threads,
        chunk_size,
        &data_format,
        &compression,
        if endpoint.engine == "mysql" && threads > 1 && table_total > 1 {
            "global_chunk"
        } else {
            "table_parallel"
        },
    ));
    let (table_manifests, total_rows, total_chunks) =
        if endpoint.engine == "mysql" && threads > 1 && table_total == 1 {
            match dump_single_mysql_table_parallel(
                &endpoint,
                output_path,
                &export_tables[0],
                chunk_size,
                &data_format,
                &compression,
                parallel_limits.range_workers_per_table,
                request.request_id.clone(),
                |event| emit(event),
            )? {
                Some(result) => result,
                None => {
                    let mut adapter = LiveAdapter::connect(&endpoint)?;
                    dump_tables_sequential(
                        &mut adapter,
                        &endpoint,
                        output_path,
                        &export_tables,
                        chunk_size,
                        &data_format,
                        &compression,
                        request.request_id.clone(),
                        |event| emit(event),
                    )?
                }
            }
        } else if endpoint.engine == "mysql" && threads > 1 && table_total > 1 {
            dump_tables_global_mysql(
                &endpoint,
                output_path,
                &export_tables,
                chunk_size,
                &data_format,
                &compression,
                threads,
                request.request_id.clone(),
                |event| emit(event),
            )?
        } else if threads > 1 && table_total > 1 {
            dump_tables_parallel(
                &endpoint,
                output_path,
                &export_tables,
                chunk_size,
                &data_format,
                &compression,
                parallel_limits.table_workers,
                parallel_limits.range_workers_per_table,
                request.request_id.clone(),
                |event| emit(event),
            )?
        } else {
            let mut adapter = LiveAdapter::connect(&endpoint)?;
            dump_tables_sequential(
                &mut adapter,
                &endpoint,
                output_path,
                &export_tables,
                chunk_size,
                &data_format,
                &compression,
                request.request_id.clone(),
                |event| emit(event),
            )?
        };

    // View 정의 수집 (전체 export 시에만). 실패해도 테이블 덤프는 유효하므로 fatal로 보지 않는다.
    let views = if full_export {
        match collect_views(&endpoint) {
            Ok(views) => views,
            Err(err) => {
                emit(json!({
                    "event": "phase",
                    "request_id": request.request_id,
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
        dump_manifest_consistency_metadata(threads);

    let manifest = DumpManifest {
        format: "tunnelforge-dump".to_string(),
        format_version: if data_format == "jsonl" { 1 } else { 2 },
        data_format,
        compression,
        source_engine: endpoint.engine.clone(),
        database: endpoint.database.clone(),
        schema,
        snapshot_policy,
        strict_export,
        manifest_warnings,
        chunk_size,
        created_unix_seconds: current_unix_seconds(),
        tables: table_manifests,
        views,
    };
    write_dump_manifest(output_path, &manifest)?;

    Ok(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "dump.run",
        "success": true,
        "output_dir": output_dir,
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
    endpoint: &Endpoint,
    output_path: &Path,
    tables: &[NormalizedTable],
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let mut manifests = Vec::new();
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let table_total = tables.len();

    for (index, table) in tables.iter().enumerate() {
        let (manifest, rows, chunks) = dump_one_table(
            adapter,
            endpoint,
            output_path,
            table,
            index,
            table_total,
            chunk_size,
            data_format,
            compression,
            request_id.clone(),
            |event| emit(event),
        )?;
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

#[cfg(test)]
fn adaptive_dump_parallel_limits(
    threads: usize,
    table_total: usize,
    chunk_size: usize,
    row_counts: &BTreeMap<String, u64>,
) -> DumpParallelLimits {
    adaptive_dump_parallel_limits_with_avg(
        threads,
        table_total,
        chunk_size,
        row_counts,
        &BTreeMap::new(),
    )
}

fn adaptive_dump_parallel_limits_with_avg(
    threads: usize,
    table_total: usize,
    chunk_size: usize,
    row_counts: &BTreeMap<String, u64>,
    avg_row_lengths: &BTreeMap<String, u64>,
) -> DumpParallelLimits {
    let baseline = dump_parallel_limits(threads, table_total);
    let thread_budget = threads.max(1);
    if table_total <= 1 || row_counts.is_empty() {
        return baseline;
    }
    let fallback_chunk_size = chunk_size.max(1);
    let heavy_tables = row_counts
        .iter()
        .filter(|(table, rows)| {
            let effective_chunk_size = mysql_range_chunk_size_for_avg_row(
                fallback_chunk_size,
                avg_row_lengths.get(*table).copied().unwrap_or(0),
            ) as u64;
            rows.saturating_add(effective_chunk_size - 1) / effective_chunk_size
                >= (thread_budget as u64).saturating_mul(2)
        })
        .count();
    let max_estimated_chunks = row_counts
        .iter()
        .map(|(table, rows)| {
            let effective_chunk_size = mysql_range_chunk_size_for_avg_row(
                fallback_chunk_size,
                avg_row_lengths.get(table).copied().unwrap_or(0),
            ) as u64;
            rows.saturating_add(effective_chunk_size - 1) / effective_chunk_size
        })
        .max()
        .unwrap_or(0);
    if heavy_tables > 1 {
        return baseline;
    }
    if max_estimated_chunks >= (thread_budget as u64).saturating_mul(2) {
        return baseline;
    }
    baseline
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

fn dump_tables_parallel<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    tables: &[NormalizedTable],
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    table_threads: usize,
    range_threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let table_total = tables.len();
    let max_threads = table_threads.max(1).min(table_total);
    let mut pending = (0..table_total).collect::<VecDeque<_>>();
    let mut active = 0_usize;
    let mut completed = 0_usize;
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let mut first_error: Option<String> = None;
    let mut manifests: Vec<Option<DumpTableManifest>> = vec![None; table_total];
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<DumpTableEvent>();

    while active < max_threads {
        if let Some(index) = pending.pop_front() {
            handles.push(spawn_dump_table_worker(
                endpoint.clone(),
                output_path.to_path_buf(),
                tables[index].clone(),
                index,
                table_total,
                chunk_size,
                data_format.to_string(),
                compression.to_string(),
                range_threads,
                request_id.clone(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed < table_total && active > 0 {
        match receiver.recv() {
            Ok(DumpTableEvent::Progress(event)) => emit(event),
            Ok(DumpTableEvent::Done {
                index,
                manifest,
                rows,
                chunks,
            }) => {
                manifests[index] = Some(manifest);
                total_rows += rows;
                total_chunks += chunks;
                completed += 1;
                active = active.saturating_sub(1);
                if let Some(next_index) = pending.pop_front() {
                    handles.push(spawn_dump_table_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        tables[next_index].clone(),
                        next_index,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        range_threads,
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpTableEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed += 1;
                active = active.saturating_sub(1);
                if let Some(next_index) = pending.pop_front() {
                    handles.push(spawn_dump_table_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        tables[next_index].clone(),
                        next_index,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        range_threads,
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
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
    endpoint: &Endpoint,
    output_path: &Path,
    tables: &[NormalizedTable],
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let table_total = tables.len();
    let mut conn = match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => {
            return Err("global mysql dump requires mysql endpoint".to_string())
        }
    };
    let profiles = load_dump_perf_profiles();
    let mut ranges_by_table = BTreeMap::<String, Vec<DumpRange>>::new();
    let mut states = Vec::<DumpGlobalTableState>::new();

    for (index, table) in tables.iter().enumerate() {
        let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
        let table_dir = output_path.join(&table_path);
        fs::create_dir_all(&table_dir)
            .map_err(|err| format!("failed to create dump table dir: {err}"))?;
        let table_row_count = conn
            .query_first::<u64, _>(count_sql("mysql", &table.name))
            .map(|count| count.unwrap_or(0))
            .unwrap_or(0);
        let mut chunks_total = 0_u64;
        let avg_row_bytes = mysql_table_avg_row_length(&mut conn, endpoint, &table.name);
        if let Some(pk_column) = single_numeric_primary_key(table) {
            let profile_key = dump_profile_key(endpoint, &table.name, data_format, compression);
            let range_chunk_size = learned_mysql_range_chunk_size(
                chunk_size,
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
                        "request_id": request_id,
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
    let max_threads = threads.max(1).min(work_total);
    let mut active = 0_usize;
    let mut completed_work = 0_usize;
    let mut first_error: Option<String> = None;
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<DumpGlobalEvent>();

    while active < max_threads {
        if let Some(work) = pending.pop_front() {
            handles.push(spawn_dump_global_worker(
                endpoint.clone(),
                output_path.to_path_buf(),
                work,
                table_total,
                chunk_size,
                data_format.to_string(),
                compression.to_string(),
                request_id.clone(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed_work < work_total && active > 0 {
        match receiver.recv() {
            Ok(DumpGlobalEvent::Progress(event)) => emit(event),
            Ok(DumpGlobalEvent::RangeDone {
                table_index,
                chunk_index,
                rows,
                stream_ms,
                range_start,
                range_end,
                checksum,
            }) => {
                let table = &tables[table_index];
                let state = &mut states[table_index];
                state.rows_dumped += rows;
                state.chunks_done += 1;
                state.work_ms = state.work_ms.saturating_add(stream_ms.max(1));
                state.chunk_sha256.insert(
                    dump_chunk_name(chunk_index, data_format, compression),
                    checksum,
                );
                completed_work += 1;
                active = active.saturating_sub(1);
                emit(json!({
                    "event": "row_progress",
                    "request_id": request_id,
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
                        "request_id": request_id,
                        "table": table.name,
                        "status": "completed",
                        "current": table_index + 1,
                        "total": table_total,
                        "strategy": "global_pk_range_parallel"
                    }));
                }
                if let Some(work) = pending.pop_front() {
                    handles.push(spawn_dump_global_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        work,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpGlobalEvent::TableDone {
                index,
                manifest,
                rows,
                chunks,
                duration_ms,
            }) => {
                let state = &mut states[index];
                state.rows_dumped = rows;
                state.chunks_done = chunks;
                state.chunks_total = chunks;
                state.work_ms = duration_ms.max(1);
                state.manifest = Some(manifest);
                completed_work += 1;
                active = active.saturating_sub(1);
                if let Some(work) = pending.pop_front() {
                    handles.push(spawn_dump_global_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        work,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpGlobalEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed_work += 1;
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

    let mut profiles = profiles;
    for (index, table) in tables.iter().enumerate() {
        let state = &states[index];
        if state.rows_dumped > 0 {
            let duration_ms = state.work_ms.max(1);
            let rows_per_second = state.rows_dumped.saturating_mul(1000) / duration_ms;
            profiles.insert(
                dump_profile_key(endpoint, &table.name, data_format, compression),
                DumpTablePerfProfile {
                    avg_row_bytes: state.avg_row_bytes,
                    chunk_rows: if state.chunks_done > 0 {
                        (state.rows_dumped / state.chunks_done).max(1) as usize
                    } else {
                        chunk_size
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

fn dump_single_mysql_table_parallel<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<Option<(Vec<DumpTableManifest>, u64, u64)>, String> {
    Ok(dump_mysql_table_parallel_ranges(
        endpoint,
        output_path,
        table,
        0,
        1,
        chunk_size,
        data_format,
        compression,
        threads,
        request_id,
        |event| emit(event),
    )?
    .map(|(manifest, rows, chunks)| (vec![manifest], rows, chunks)))
}

fn dump_mysql_table_parallel_ranges<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<Option<(DumpTableManifest, u64, u64)>, String> {
    let Some(pk_column) = single_numeric_primary_key(table) else {
        return Ok(None);
    };

    let mut conn = match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => return Ok(None),
    };
    let table_row_count = conn
        .query_first::<u64, _>(count_sql("mysql", &table.name))
        .map(|count| count.unwrap_or(0))
        .unwrap_or(0);
    let avg_row_bytes = mysql_table_avg_row_length(&mut conn, endpoint, &table.name);
    let range_chunk_size = mysql_range_chunk_size_for_avg_row(chunk_size, avg_row_bytes);
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
        "request_id": request_id,
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
    let table_dir = output_path.join(&table_path);
    fs::create_dir_all(&table_dir)
        .map_err(|err| format!("failed to create dump table dir: {err}"))?;
    let ranges = pk_ranges(min_key, max_key, table_row_count, range_chunk_size);
    let total_ranges = ranges.len();
    let max_threads = threads.max(1).min(total_ranges.max(1));
    let mut pending = ranges.into_iter().collect::<VecDeque<_>>();
    let mut active = 0_usize;
    let mut completed = 0_usize;
    let mut rows_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();
    let mut first_error: Option<String> = None;
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<DumpRangeEvent>();

    while active < max_threads {
        if let Some(range) = pending.pop_front() {
            handles.push(spawn_mysql_range_worker(
                endpoint.clone(),
                output_path.to_path_buf(),
                table.clone(),
                table_path.clone(),
                pk_column.to_string(),
                range,
                data_format.to_string(),
                compression.to_string(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed < total_ranges && active > 0 {
        match receiver.recv() {
            Ok(DumpRangeEvent::Done {
                chunk_index,
                rows,
                stream_ms,
                range_start,
                range_end,
                checksum,
            }) => {
                rows_dumped += rows;
                completed += 1;
                active = active.saturating_sub(1);
                chunk_sha256.insert(
                    dump_chunk_name(chunk_index, data_format, compression),
                    checksum,
                );
                emit(json!({
                    "event": "row_progress",
                    "request_id": request_id,
                    "table": table.name,
                    "rows": rows_dumped,
                    "total": table_row_count,
                    "chunk_rows": rows,
                    "chunks_done": completed,
                    "chunks_total": total_ranges,
                    "stream_ms": stream_ms,
                    "chunk_index": chunk_index,
                    "range_start": range_start,
                    "range_end": range_end,
                    "strategy": "pk_range_parallel"
                }));
                if let Some(range) = pending.pop_front() {
                    handles.push(spawn_mysql_range_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        table.clone(),
                        table_path.clone(),
                        pk_column.to_string(),
                        range,
                        data_format.to_string(),
                        compression.to_string(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpRangeEvent::Error(err)) => {
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

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
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

fn spawn_dump_global_worker(
    endpoint: Endpoint,
    output_path: PathBuf,
    work: DumpGlobalWorkItem,
    table_total: usize,
    chunk_size: usize,
    data_format: String,
    compression: String,
    request_id: Option<String>,
    sender: mpsc::Sender<DumpGlobalEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || match work.kind {
        DumpGlobalWorkKind::MysqlRange {
            table_path,
            pk_column,
            range,
        } => {
            let result = dump_mysql_range_chunk(
                &endpoint,
                &output_path,
                &work.table,
                &table_path,
                &pk_column,
                &range,
                &data_format,
                &compression,
            );
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
            let result = (|| {
                let mut adapter = LiveAdapter::connect(&endpoint)?;
                let started = Instant::now();
                dump_one_table(
                    &mut adapter,
                    &endpoint,
                    &output_path,
                    &work.table,
                    work.table_index,
                    table_total,
                    chunk_size,
                    &data_format,
                    &compression,
                    request_id,
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
                })
            })();
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
    })
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

fn spawn_dump_table_worker(
    endpoint: Endpoint,
    output_path: std::path::PathBuf,
    table: NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: String,
    compression: String,
    range_threads: usize,
    request_id: Option<String>,
    sender: mpsc::Sender<DumpTableEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = (|| {
            if endpoint.engine == "mysql" {
                if let Some(result) = dump_mysql_table_parallel_ranges(
                    &endpoint,
                    &output_path,
                    &table,
                    index,
                    table_total,
                    chunk_size,
                    &data_format,
                    &compression,
                    range_threads,
                    request_id.clone(),
                    |event| {
                        let _ = sender.send(DumpTableEvent::Progress(event));
                    },
                )? {
                    return Ok(result);
                }
            }
            let mut adapter = LiveAdapter::connect(&endpoint)?;
            dump_one_table(
                &mut adapter,
                &endpoint,
                &output_path,
                &table,
                index,
                table_total,
                chunk_size,
                &data_format,
                &compression,
                request_id,
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

fn dump_one_table<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(DumpTableManifest, u64, u64), String> {
    if let LiveAdapter::MySql(conn) = adapter {
        return dump_one_mysql_table(
            conn,
            endpoint,
            output_path,
            table,
            index,
            table_total,
            chunk_size,
            data_format,
            compression,
            request_id,
            emit,
        );
    }

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

    let table_row_count = adapter.row_count(&table.name).unwrap_or(0) as u64;
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut last_key: Option<String> = None;
    let mut offset = 0_usize;
    let mut rows_dumped = 0_u64;
    let mut chunks_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();

    loop {
        let Some(read_limit) = bounded_dump_chunk_limit(table_row_count, rows_dumped, chunk_size)
        else {
            break;
        };
        let read_started = Instant::now();
        let rows = if use_keyset {
            adapter.read_rows_after_key(table, &key_columns, last_key.as_deref(), read_limit)?
        } else {
            adapter.read_rows(table, offset, read_limit)?
        };
        let read_ms = read_started.elapsed().as_millis() as u64;
        if rows.is_empty() {
            break;
        }
        chunks_dumped += 1;
        let chunk_name = dump_chunk_name(chunks_dumped, data_format, compression);
        let write_started = Instant::now();
        let checksum = write_dump_rows(
            &table_dir.join(&chunk_name),
            table,
            &rows,
            data_format,
            compression,
        )?;
        chunk_sha256.insert(chunk_name, checksum);
        let write_ms = write_started.elapsed().as_millis() as u64;

        let copied_now = rows.len();
        rows_dumped += copied_now as u64;
        if use_keyset {
            last_key = rows.last().and_then(|row| row_key_token(row, &key_columns));
        } else {
            offset += copied_now;
        }

        emit(json!({
            "event": "row_progress",
            "request_id": request_id,
            "table": table.name,
            "rows": rows_dumped,
            "total": table_row_count,
            "chunk_rows": copied_now,
            "read_ms": read_ms,
            "write_ms": write_ms
        }));
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

fn dump_one_mysql_table<F: FnMut(Value)>(
    conn: &mut mysql::PooledConn,
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    request_id: Option<String>,
    mut emit: F,
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

    let table_row_count = conn
        .query_first::<u64, _>(count_sql("mysql", &table.name))
        .map(|count| count.unwrap_or(0))
        .unwrap_or(0);
    // 청크당 행 수를 바이트 목표(≈64MB) + 절대 행수 상한으로 산출한다. 대형 TEXT/JSON
    // 컬럼 테이블에서 하나의 result set가 과대해져 스트리밍 코덱이 크래시하는 것을 막는다.
    // 병렬 경로와 동일한 avg-row-length 헬퍼를 재사용하며, 조회 실패/통계 부재 시
    // avg=0 → fallback(chunk_size) → 상한이 지배하도록 안전하게 degrade한다.
    let avg_row_bytes = mysql_table_avg_row_length(conn, endpoint, &table.name);
    let effective_chunk_size = sequential_mysql_chunk_size(chunk_size, avg_row_bytes);
    let columns = column_names(table);
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut last_key: Option<String> = None;
    let mut offset = 0_usize;
    let mut rows_dumped = 0_u64;
    let mut chunks_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();

    loop {
        let Some(read_limit) =
            bounded_dump_chunk_limit(table_row_count, rows_dumped, effective_chunk_size)
        else {
            break;
        };
        chunks_dumped += 1;
        let chunk_name = dump_chunk_name(chunks_dumped, data_format, compression);
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
            let mut file = open_dump_writer(&chunk_path, compression)?;
            for row in result {
                let row = row.map_err(|err| format!("mysql dump row error: {err}"))?;
                if data_format == "tsv" && !use_keyset {
                    write_mysql_text_row_tsv(&mut file, row)?;
                } else {
                    let row_json = mysql_row_to_json(&columns, row);
                    if use_keyset {
                        next_key = row_key_token(&row_json, &key_columns);
                    }
                    write_dump_row(&mut file, table, &row_json, data_format)?;
                }
                chunk_rows += 1;
            }
        }
        let stream_ms = stream_started.elapsed().as_millis() as u64;

        if chunk_rows == 0 {
            fs::remove_file(&chunk_path).ok();
            chunks_dumped -= 1;
            break;
        }
        let checksum = sha256_file(&chunk_path)?;
        chunk_sha256.insert(chunk_name, checksum);

        rows_dumped += chunk_rows as u64;
        if use_keyset {
            last_key = next_key;
        } else {
            offset += chunk_rows;
        }

        emit(json!({
            "event": "row_progress",
            "request_id": request_id,
            "table": table.name,
            "rows": rows_dumped,
            "total": table_row_count,
            "chunk_rows": chunk_rows,
            "stream_ms": stream_ms
        }));
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
    fn dump_import_row_progress_event_reports_table_and_overall_rows() {
        let event = dump_import_row_progress_event(
            Some("import-1".to_string()),
            "orders",
            25,
            100,
            1_000,
            2_000,
            25,
            Some(2),
            Some(8),
            Some(4),
            Some(500),
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
        let limits = adaptive_dump_parallel_limits(8, 3, 50_000, &counts);

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
        let mut counts = BTreeMap::new();
        counts.insert("huge".to_string(), 2_000_000);
        counts.insert("medium".to_string(), 500_000);
        counts.insert("tiny".to_string(), 10);

        let limits = adaptive_dump_parallel_limits(8, 208, 50_000, &counts);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_use_byte_chunks_for_wide_tables() {
        let mut counts = BTreeMap::new();
        counts.insert("df_subs".to_string(), 223_502);
        counts.insert("tiny".to_string(), 10);
        let mut avg_row_lengths = BTreeMap::new();
        avg_row_lengths.insert("df_subs".to_string(), 9_462);

        let limits =
            adaptive_dump_parallel_limits_with_avg(8, 208, 50_000, &counts, &avg_row_lengths);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_keep_table_parallelism_for_pathological_wide_table() {
        let mut counts = BTreeMap::new();
        counts.insert("df_subs".to_string(), 387_398);
        counts.insert("qe_view_factors_result".to_string(), 1_946_153);
        counts.insert("df_call_logs".to_string(), 1_076_142);
        let mut avg_row_lengths = BTreeMap::new();
        avg_row_lengths.insert("df_subs".to_string(), 9_462);
        avg_row_lengths.insert("qe_view_factors_result".to_string(), 128);
        avg_row_lengths.insert("df_call_logs".to_string(), 128);

        let limits =
            adaptive_dump_parallel_limits_with_avg(8, 208, 50_000, &counts, &avg_row_lengths);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_keep_multiple_heavy_tables_in_parallel() {
        let mut counts = BTreeMap::new();
        counts.insert("huge_a".to_string(), 2_000_000);
        counts.insert("huge_b".to_string(), 1_900_000);
        counts.insert("tiny".to_string(), 10);

        let limits = adaptive_dump_parallel_limits(8, 208, 50_000, &counts);

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
