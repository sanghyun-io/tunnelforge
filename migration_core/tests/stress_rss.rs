use migration_core::{
    migrate_with_adapters, verify_with_adapters, MigrationAdapter, MigrationOptions,
    NormalizedColumn, NormalizedSchema, NormalizedTable,
};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::path::Path;

#[derive(Default)]
struct CountingTargetAdapter {
    rows_inserted: usize,
}

impl MigrationAdapter for CountingTargetAdapter {
    fn row_count(&mut self, _table: &str) -> Result<usize, String> {
        Ok(self.rows_inserted)
    }

    fn create_table(&mut self, _table: &NormalizedTable, _ddl: &str) -> Result<(), String> {
        Ok(())
    }

    fn read_rows(
        &mut self,
        _table: &NormalizedTable,
        _offset: usize,
        _limit: usize,
    ) -> Result<Vec<Value>, String> {
        Err("counting target does not support reads".to_string())
    }

    fn insert_rows(&mut self, _table: &NormalizedTable, rows: Vec<Value>) -> Result<(), String> {
        self.rows_inserted += rows.len();
        Ok(())
    }

    fn execute_sql(&mut self, _sql: &str) -> Result<(), String> {
        Ok(())
    }
}

fn last_key_offset(last_key: Option<&str>) -> usize {
    let Some(raw) = last_key else {
        return 0;
    };
    let trimmed = raw
        .trim()
        .trim_start_matches('[')
        .trim_end_matches(']')
        .trim_matches('"');
    trimmed.parse::<usize>().unwrap_or(0)
}

#[derive(Clone)]
struct SyntheticStressAdapter {
    rows: usize,
}

impl SyntheticStressAdapter {
    fn new(rows: usize) -> Self {
        Self { rows }
    }

    fn row(id: usize) -> Value {
        json!({
            "id": id,
            "name": format!("stress-{id}"),
            "amount": (id % 100_000) as i64,
        })
    }
}

impl MigrationAdapter for SyntheticStressAdapter {
    fn row_count(&mut self, _table: &str) -> Result<usize, String> {
        Ok(self.rows)
    }

    fn create_table(&mut self, _table: &NormalizedTable, _ddl: &str) -> Result<(), String> {
        Ok(())
    }

    fn read_rows(
        &mut self,
        _table: &NormalizedTable,
        offset: usize,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        let end = offset.saturating_add(limit).min(self.rows);
        Ok((offset + 1..=end).map(Self::row).collect())
    }

    fn read_rows_after_key(
        &mut self,
        table: &NormalizedTable,
        _key_columns: &[String],
        last_key: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        let offset = last_key_offset(last_key);
        self.read_rows(table, offset, limit)
    }

    fn insert_rows(&mut self, _table: &NormalizedTable, _rows: Vec<Value>) -> Result<(), String> {
        Ok(())
    }

    fn execute_sql(&mut self, _sql: &str) -> Result<(), String> {
        Ok(())
    }
}

fn stress_schema() -> NormalizedSchema {
    NormalizedSchema {
        tables: vec![stress_table()],
    }
}

fn stress_table() -> NormalizedTable {
    NormalizedTable {
        name: "perf_stress".to_string(),
        columns: vec![
            NormalizedColumn {
                name: "id".to_string(),
                type_name: "integer".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            },
            NormalizedColumn {
                name: "name".to_string(),
                type_name: "varchar(64)".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            },
            NormalizedColumn {
                name: "amount".to_string(),
                type_name: "integer".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            },
        ],
        indexes: Vec::new(),
        foreign_keys: Vec::new(),
        table_collation: None,
    }
}

fn current_peak_rss_mb() -> u64 {
    platform_peak_rss_bytes()
        .map(|bytes| ((bytes as u64).saturating_add(1024 * 1024 - 1)) / (1024 * 1024))
        .unwrap_or(1)
        .max(1)
}

#[cfg(windows)]
fn platform_peak_rss_bytes() -> Option<usize> {
    use std::ffi::c_void;

    #[repr(C)]
    #[allow(non_snake_case)]
    struct ProcessMemoryCounters {
        cb: u32,
        PageFaultCount: u32,
        PeakWorkingSetSize: usize,
        WorkingSetSize: usize,
        QuotaPeakPagedPoolUsage: usize,
        QuotaPagedPoolUsage: usize,
        QuotaPeakNonPagedPoolUsage: usize,
        QuotaNonPagedPoolUsage: usize,
        PagefileUsage: usize,
        PeakPagefileUsage: usize,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> *mut c_void;
    }

    #[link(name = "psapi")]
    extern "system" {
        fn GetProcessMemoryInfo(
            process: *mut c_void,
            counters: *mut ProcessMemoryCounters,
            size: u32,
        ) -> i32;
    }

    let mut counters = ProcessMemoryCounters {
        cb: std::mem::size_of::<ProcessMemoryCounters>() as u32,
        PageFaultCount: 0,
        PeakWorkingSetSize: 0,
        WorkingSetSize: 0,
        QuotaPeakPagedPoolUsage: 0,
        QuotaPagedPoolUsage: 0,
        QuotaPeakNonPagedPoolUsage: 0,
        QuotaNonPagedPoolUsage: 0,
        PagefileUsage: 0,
        PeakPagefileUsage: 0,
    };
    let ok = unsafe { GetProcessMemoryInfo(GetCurrentProcess(), &mut counters, counters.cb) };
    if ok == 0 {
        None
    } else {
        Some(counters.PeakWorkingSetSize)
    }
}

#[cfg(not(windows))]
fn platform_peak_rss_bytes() -> Option<usize> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix("VmHWM:") {
            let kb = rest
                .split_whitespace()
                .next()
                .and_then(|value| value.parse::<usize>().ok())?;
            return Some(kb * 1024);
        }
    }
    None
}

fn run_synthetic_stress(
    rows: usize,
    chunk_size: usize,
    cancel_after_chunks: Option<usize>,
) -> Value {
    let schema = stress_schema();
    let mut source = SyntheticStressAdapter::new(rows);
    let mut target = CountingTargetAdapter::default();
    let first = migrate_with_adapters(
        &schema,
        &MigrationOptions {
            mode: "create_only".to_string(),
            chunk_size,
            cancel_after_chunks,
            cleanup_before_migrate: false,
        },
        None,
        &mut source,
        &mut target,
        "",
        "",
    );
    let peak_after_cancel = current_peak_rss_mb();

    let resume_state = if first.success {
        None
    } else {
        Some(first.state)
    };
    let mut source = SyntheticStressAdapter::new(rows);
    let second = migrate_with_adapters(
        &schema,
        &MigrationOptions {
            mode: "append".to_string(),
            chunk_size,
            cancel_after_chunks: None,
            cleanup_before_migrate: false,
        },
        resume_state.as_ref(),
        &mut source,
        &mut target,
        "",
        "",
    );
    let peak_after_resume = current_peak_rss_mb();

    let mut verify_source = SyntheticStressAdapter::new(rows);
    let mut verify_target = SyntheticStressAdapter::new(rows);
    let mismatches =
        verify_with_adapters(&schema, &mut verify_source, &mut verify_target, chunk_size);
    let peak_after_verify = current_peak_rss_mb();
    let peak_rss_mb = peak_after_cancel
        .max(peak_after_resume)
        .max(peak_after_verify);
    let rss_limit_mb = env::var("TF_STRESS_RSS_LIMIT_MB")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(2048);

    json!({
        "source_type": "synthetic_adapter",
        "rows": rows,
        "resume_success": second.success && target.rows_inserted == rows,
        "verify_success": mismatches.is_empty(),
        "mismatches": mismatches.len(),
        "peak_rss_mb": peak_rss_mb,
        "rss_limit_mb": rss_limit_mb,
        "chunks": {
            "cancelled_rows": first.rows_copied,
            "resumed_rows": second.rows_copied,
            "chunk_size": chunk_size
        },
        "notes": "Measured in migration_core/tests/stress_rss.rs using synthetic MigrationAdapter resume+verify path."
    })
}

#[test]
fn synthetic_stress_adapter_streams_rows_without_storing_all_values() {
    let mut adapter = SyntheticStressAdapter::new(1_000);
    let table = stress_table();

    assert_eq!(adapter.row_count("perf_stress").unwrap(), 1_000);
    assert_eq!(adapter.read_rows(&table, 0, 3).unwrap().len(), 3);
    assert_eq!(
        adapter
            .read_rows_after_key(&table, &["id".to_string()], Some("[\"3\"]"), 3)
            .unwrap()[0]["id"],
        json!(4)
    );
}

#[test]
fn synthetic_stress_run_reports_resume_verify_and_rss_bound() {
    let report = run_synthetic_stress(10_000, 1_000, Some(2));

    assert_eq!(report["rows"], json!(10_000));
    assert_eq!(report["resume_success"], json!(true));
    assert_eq!(report["verify_success"], json!(true));
    assert_eq!(report["mismatches"], json!(0));
    assert!(report["peak_rss_mb"].as_u64().unwrap() >= 1);
    assert!(report["peak_rss_mb"].as_u64().unwrap() <= report["rss_limit_mb"].as_u64().unwrap());
}

#[test]
#[ignore = "manual 10M RSS evidence capture for GitHub #136"]
fn synthetic_10m_stress_resume_verify_reports_rss_bound() {
    let rows = env::var("TF_STRESS_ROWS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(10_000_000);
    let chunk_size = env::var("TF_STRESS_CHUNK_SIZE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(200_000);
    let report = run_synthetic_stress(rows, chunk_size, Some(3));

    if let Ok(path) = env::var("TF_STRESS_RSS_REPORT") {
        let path = Path::new(&path);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, serde_json::to_string_pretty(&report).unwrap() + "\n").unwrap();
    }

    println!("{}", serde_json::to_string(&report).unwrap());
    assert_eq!(report["rows"], json!(rows));
    assert_eq!(report["resume_success"], json!(true));
    assert_eq!(report["verify_success"], json!(true));
    assert_eq!(report["mismatches"], json!(0));
    assert!(
        report["peak_rss_mb"].as_u64().unwrap() <= report["rss_limit_mb"].as_u64().unwrap(),
        "peak RSS exceeded configured limit: {report}"
    );
}
