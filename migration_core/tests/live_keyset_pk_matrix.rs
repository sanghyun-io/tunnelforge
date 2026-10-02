//! Keyset cursors must page through every primary-key type exactly once (TF-STATUS-137 follow-up).
//! Each case dumps with a tiny chunk size (many keyset pages) and compares the rows with a
//! single-page dump of the same table, then copies and verifies the table across engines.
//! Requires disposable MySQL and PostgreSQL (TF_MYSQL_* / TF_POSTGRES_*); skipped otherwise,
//! but TF_LIVE_REQUIRED turns a missing environment into a failure.
use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};
use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

const CHUNK_SIZE: usize = 2;
const PATH_TIMEOUT_SECS: u64 = 60;

fn endpoint(prefix: &str, engine: &str, port: u16) -> Option<Endpoint> {
    Some(Endpoint {
        engine: engine.into(),
        host: std::env::var(format!("{prefix}_HOST")).ok()?,
        port: std::env::var(format!("{prefix}_PORT")).ok().and_then(|v| v.parse().ok()).unwrap_or(port),
        user: std::env::var(format!("{prefix}_USER")).ok()?,
        password: std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),
        database: std::env::var(format!("{prefix}_DATABASE")).ok()?,
        schema: None,
        tls: Default::default(),
    })
}

fn request_with_timeout(command: &str, payload: Value) -> Result<Value, String> {
    let (sender, receiver) = mpsc::channel();
    let command = command.to_string();
    std::thread::spawn(move || {
        let events = handle_request(Request { command, request_id: None, payload });
        let _ = sender.send(events);
    });
    let events = receiver
        .recv_timeout(Duration::from_secs(PATH_TIMEOUT_SECS))
        .map_err(|_| format!("LOOP (no result within {PATH_TIMEOUT_SECS}s)"))?;
    if let Some(error) = events.iter().find(|event| event["event"] == "error") {
        return Err(format!("ERROR {}", error["message"]));
    }
    events.into_iter().find(|event| event["event"] == "result").ok_or_else(|| "no result event".to_string())
}

fn data_lines(dir: &Path) -> Vec<String> {
    let mut lines = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(path) = stack.pop() {
        for entry in std::fs::read_dir(&path).unwrap().flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("jsonl")
                && path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("chunk_"))
            {
                lines.extend(std::fs::read_to_string(&path).unwrap().lines().map(str::to_string));
            }
        }
    }
    lines.sort();
    lines
}

fn count_rows(endpoint: &Endpoint, table: &str) -> Result<u64, String> {
    let result = request_with_timeout("query.execute", json!({"connection": endpoint, "sql": format!("SELECT COUNT(*) AS n FROM {table}")}))?;
    result["rows"][0]["n"].to_string().trim_matches('"').parse().map_err(|e| format!("count parse: {e}"))
}

/// One key type. `values` are SQL row tuples; `migrate` says whether the column types map to the
/// other engine (cases that do not map are still covered by dump.run). PostgreSQL TEXT/UUID/REAL/
/// TIMESTAMPTZ keys do not map to MySQL keys yet (separate type-mapping limit).
struct Case {
    name: &'static str,
    ddl: &'static str,
    columns: Value,
    values: Vec<String>,
    migrate: bool,
    /// Cross-engine verify pages both sides with the source cursor; text keys under different
    /// collations misalign the pages (false mismatches, follow-up), and MySQL FLOAT/DOUBLE still map
    /// to PostgreSQL TEXT (type-mapping follow-up), so those cases skip verify.
    verify: bool,
}

fn col(name: &str, ty: &str, pk: bool) -> Value {
    json!({"name": name, "type": ty, "nullable": !pk, "primary_key": pk})
}

fn mysql_cases() -> Vec<Case> {
    let label = |i: usize| format!("'r{i}'");
    vec![
        Case { name: "text_ai_ci", ddl: "k VARCHAR(32) COLLATE utf8mb4_0900_ai_ci PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "varchar(32)", true), col("v", "varchar(8)", false)]),
            // backslashes, a quote, case/accents and trailing space under a NO PAD collation
            values: ["'a'", "'B '", "'\\u{00e1}x'", "'a\\\\b'", "'x\\\\'", "'\\\\''z'", "'it''s'", "'zz'"]
                .iter().enumerate().map(|(i, k)| format!("({}, {})", k.replace("\\u{00e1}", "\u{00e1}"), label(i))).collect(),
            migrate: true, verify: false },
        Case { name: "text_general_ci", ddl: "k VARCHAR(32) COLLATE utf8mb4_general_ci PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "varchar(32)", true), col("v", "varchar(8)", false)]),
            values: ["'apple'", "'Banana'", "'cherry'", "'Date'", "'egg'", "'Fig'", "'grape'"]
                .iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "uuid_char", ddl: "k CHAR(36) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "char(36)", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('{:08x}-0000-4000-8000-{:012x}', {})", 0xffff_fff0u32 - i as u32 * 0x1111_1111, i, label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "decimal_wide", ddl: "k DECIMAL(38,0) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "decimal(38,0)", true), col("v", "varchar(8)", false)]),
            values: (1..=7).map(|i| format!("(1000000000000000000000000000000{i}, {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "decimal_frac", ddl: "k DECIMAL(30,20) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "decimal(30,20)", true), col("v", "varchar(8)", false)]),
            values: (1..=7).map(|i| format!("(1.0000000000000000000{i}, {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "float", ddl: "k FLOAT PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "float", true), col("v", "varchar(8)", false)]),
            values: ["0.1", "0.2", "0.3", "0.7", "1.1", "3.3", "-0.1"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: true },
        // More than 6 significant digits: the text protocol would render these alike.
        Case { name: "float_precise", ddl: "k FLOAT PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "float", true), col("v", "varchar(8)", false)]),
            values: ["1.2345678", "1.2345679", "1.234569", "1234567", "1234568", "1234569", "16777215"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: false, verify: false },
        // A leading ENUM key with a second column: equality on the label, IN for later labels.
        Case { name: "enum_composite", ddl: "k ENUM('zulu','alpha','mike') NOT NULL, id INT NOT NULL, v VARCHAR(8), PRIMARY KEY (k, id)",
            columns: json!([col("k", "enum('zulu','alpha','mike')", true), col("id", "int", true), col("v", "varchar(8)", false)]),
            values: ["'zulu', 2", "'zulu', 1", "'alpha', 9", "'alpha', 3", "'mike', 5", "'mike', 4", "'zulu', 3"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "double", ddl: "k DOUBLE PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "double", true), col("v", "varchar(8)", false)]),
            values: ["0.1", "0.2", "0.30000000000000004", "1e-300", "1.7976931348623157e308", "-2.5", "3.141592653589793"]
                .iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: false },
        Case { name: "datetime6", ddl: "k DATETIME(6) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "datetime(6)", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('2026-03-08 01:59:59.99999{i}', {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "timestamp3", ddl: "k TIMESTAMP(3) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "timestamp(3)", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('2026-11-01 05:30:00.00{i}', {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "date", ddl: "k DATE PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "date", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('{}-02-28', {})", 1970 + i * 9, label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "bigint_unsigned", ddl: "k BIGINT UNSIGNED PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "bigint unsigned", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("({}, {})", 18446744073709551609u64 + i as u64, label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "bigint_2p53", ddl: "k BIGINT PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "bigint", true), col("v", "varchar(8)", false)]),
            values: (1..=7).map(|i| format!("({}, {})", 9007199254740992i64 + i, label(i as usize))).collect(),
            migrate: true, verify: true },
        Case { name: "int_negative", ddl: "k INT PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "int", true), col("v", "varchar(8)", false)]),
            values: [-2147483648i64, -100, -1, 0, 1, 100, 2147483647].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "enum_order", ddl: "k ENUM('zulu','alpha','mike','bravo','yankee','charlie','xray') PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "enum('zulu','alpha','mike','bravo','yankee','charlie','xray')", true), col("v", "varchar(8)", false)]),
            values: ["'zulu'", "'alpha'", "'mike'", "'bravo'", "'yankee'", "'charlie'", "'xray'"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "bit8", ddl: "k BIT(8) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "bit(8)", true), col("v", "varchar(8)", false)]),
            values: [0x01, 0x41, 0x7f, 0x80, 0xc3, 0xfe, 0xff].iter().enumerate().map(|(i, k)| format!("(b'{k:08b}', {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "composite_text_decimal", ddl: "a VARCHAR(16) COLLATE utf8mb4_0900_ai_ci NOT NULL, b DECIMAL(30,20) NOT NULL, v VARCHAR(8), PRIMARY KEY (a, b)",
            columns: json!([col("a", "varchar(16)", true), col("b", "decimal(30,20)", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('{}', 1.0000000000000000000{}, {})", if i < 4 { "g\\\\1" } else { "G2" }, i, label(i))).collect(),
            migrate: true, verify: false },
        Case { name: "nullable_unique", ddl: "k VARCHAR(16) NULL UNIQUE, v VARCHAR(8)",
            columns: json!([{"name": "k", "type": "varchar(16)", "nullable": true, "unique": true}, col("v", "varchar(8)", false)]),
            values: ["NULL", "NULL", "NULL", "'a'", "'b'", "'c'", "'d'"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: true },
    ]
}

fn postgres_cases() -> Vec<Case> {
    let label = |i: usize| format!("'r{i}'");
    vec![
        Case { name: "text_icu", ddl: "k TEXT COLLATE \"en-US-x-icu\" PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "text", true), col("v", "varchar(8)", false)]),
            values: ["'a'", "'B'", "'\u{00e1}x'", "'a\\b'", "'x\\'", "'it''s'", "'_z'", "'Zz'"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "numeric_wide", ddl: "k NUMERIC(38,0) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "numeric(38,0)", true), col("v", "varchar(8)", false)]),
            values: (1..=7).map(|i| format!("(1000000000000000000000000000000{i}, {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "real", ddl: "k REAL PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "real", true), col("v", "varchar(8)", false)]),
            values: ["0.1", "0.2", "0.3", "0.7", "1.1", "3.3", "-0.1"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "timestamptz6", ddl: "k TIMESTAMPTZ(6) PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "timestamp with time zone", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('2026-11-01 05:30:00.00000{i}+00', {})", label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "bigint_extremes", ddl: "k BIGINT PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "bigint", true), col("v", "varchar(8)", false)]),
            values: [i64::MIN, -9007199254740993, -1, 0, 9007199254740993, 9007199254740994, i64::MAX].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: true },
        Case { name: "uuid", ddl: "k UUID PRIMARY KEY, v VARCHAR(8)",
            columns: json!([col("k", "uuid", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('{:08x}-0000-4000-8000-{:012x}', {})", 0xffff_fff0u32 - i as u32 * 0x1111_1111, i, label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "composite_text_numeric", ddl: "a TEXT COLLATE \"en-US-x-icu\" NOT NULL, b NUMERIC(30,20) NOT NULL, v VARCHAR(8), PRIMARY KEY (a, b)",
            columns: json!([col("a", "text", true), col("b", "numeric(30,20)", true), col("v", "varchar(8)", false)]),
            values: (0..7).map(|i| format!("('{}', 1.0000000000000000000{}, {})", if i < 4 { "g\\1" } else { "G2" }, i, label(i))).collect(),
            migrate: false, verify: false },
        Case { name: "nullable_unique", ddl: "k VARCHAR(16) NULL UNIQUE, v VARCHAR(8)",
            columns: json!([{"name": "k", "type": "varchar(16)", "nullable": true, "unique": true}, col("v", "varchar(8)", false)]),
            values: ["NULL", "NULL", "NULL", "'a'", "'b'", "'c'", "'d'"].iter().enumerate().map(|(i, k)| format!("({k}, {})", label(i))).collect(),
            migrate: true, verify: true },
    ]
}

fn dump_lines(source: &Endpoint, table: &str, dir: &Path, threads: usize, chunk_size: usize) -> Result<Vec<String>, String> {
    let _ = std::fs::remove_dir_all(dir);
    request_with_timeout("dump.run", json!({
        "source": source, "tables": [table], "output_dir": dir, "threads": threads,
        "chunk_size": chunk_size, "data_format": "jsonl", "compression": "none"}))?;
    let lines = data_lines(dir);
    let _ = std::fs::remove_dir_all(dir);
    Ok(lines)
}

#[test]
fn every_primary_key_type_pages_each_row_exactly_once_when_configured() {
    let (Some(mysql), Some(postgres)) = (endpoint("TF_MYSQL", "mysql", 3306), endpoint("TF_POSTGRES", "postgresql", 5432)) else {
        assert!(std::env::var_os("TF_LIVE_REQUIRED").is_none(), "TF_LIVE_REQUIRED is set but TF_MYSQL_* / TF_POSTGRES_* are missing");
        eprintln!("skipping keyset PK matrix: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };
    let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let mut report: Vec<String> = Vec::new();
    let mut failures = 0usize;
    let mut record = |path: String, outcome: Result<String, String>| match outcome {
        Ok(note) => report.push(format!("OK    {path} {note}")),
        Err(note) => {
            failures += 1;
            report.push(format!("FAIL  {path} {note}"));
        }
    };

    for (source, other, cases) in [(&mysql, &postgres, mysql_cases()), (&postgres, &mysql, postgres_cases())] {
        let mut source_db = LiveAdapter::connect(source).unwrap();
        let mut other_db = LiveAdapter::connect(other).unwrap();
        for case in cases {
            let table = format!("tf_kpm_{}_{suffix}", case.name);
            let path = format!("{} {}", source.engine, case.name);
            if let Err(error) = source_db.execute_sql(&format!("CREATE TABLE {table} ({})", case.ddl))
                .and_then(|_| source_db.execute_sql(&format!("INSERT INTO {table} VALUES {}", case.values.join(","))))
            {
                record(format!("setup    {path}"), Err(error));
                let _ = source_db.execute_sql(&format!("DROP TABLE IF EXISTS {table}"));
                continue;
            }
            let expected = case.values.len();

            // A single page is the reference: no cursor continuation is involved.
            let dir = std::env::temp_dir().join(format!("tf-kpm-{suffix}-{}", case.name));
            let reference = dump_lines(source, &table, &dir, 1, 1_000_000);
            match &reference {
                Ok(lines) if lines.len() == expected => {}
                Ok(lines) => record(format!("dump.ref {path}"), Err(format!("single-page dump wrote {} of {expected} rows", lines.len()))),
                Err(error) => record(format!("dump.ref {path}"), Err(error.clone())),
            }
            if let Ok(reference) = &reference {
                for threads in [1, 2] {
                    let outcome = dump_lines(source, &table, &dir, threads, CHUNK_SIZE).and_then(|lines| {
                        if &lines == reference {
                            Ok(format!("rows={}", lines.len()))
                        } else {
                            let missing = reference.iter().filter(|l| !lines.contains(l)).count();
                            let extra = lines.len() as i64 - reference.len() as i64 + missing as i64;
                            Err(format!("rows={} missing={missing} duplicated_or_extra={extra}", lines.len()))
                        }
                    });
                    record(format!("dump.run {path} threads={threads}"), outcome);
                }
            }

            if case.migrate {
                let payload = json!({
                    "source_engine": source.engine, "target_engine": other.engine,
                    "source": source, "target": other,
                    "schema": {"tables": [{"name": table, "columns": case.columns}]},
                    "execution_options": {"mode": "create_only", "chunk_size": CHUNK_SIZE}});
                let migrated = request_with_timeout("migrate", payload.clone()).and_then(|result| {
                    let copied = count_rows(other, &table)?;
                    if result["success"] == true && result["rows_copied"] == json!(expected) && copied == expected as u64 {
                        Ok(format!("rows_copied={expected}"))
                    } else {
                        Err(format!("success={} rows_copied={} target_rows={copied} issues={}", result["success"], result["rows_copied"], result["issues"]))
                    }
                });
                let copied = migrated.is_ok();
                record(format!("migrate  {path} -> {}", other.engine), migrated);
                if copied && case.verify {
                    let verified = request_with_timeout("verify", payload).and_then(|result| {
                        if result["success"] == true { Ok("verified".to_string()) } else { Err(format!("mismatches={}", result["mismatches"])) }
                    });
                    record(format!("verify   {path} -> {}", other.engine), verified);
                }
                let _ = other_db.execute_sql(&format!("DROP TABLE IF EXISTS {table}"));
            }
            let _ = source_db.execute_sql(&format!("DROP TABLE IF EXISTS {table}"));
        }
    }
    println!("{}", report.join("\n"));
    assert_eq!(failures, 0, "keyset primary-key regressions:\n{}", report.join("\n"));
}
