//! Opt-in live checks for TF-STATUS-112 (query cancel / timeout / streaming / limits).
//!
//! Skipped unless `TF_QUERY_LIVE_MYSQL_*` / `TF_QUERY_LIVE_PG_*` are set
//! (`HOST`, `PORT`, `USER`, `PASSWORD`, `DATABASE`). Use disposable containers only.
//! Drives the real `tunnelforge-core` binary over JSONL, so concurrency is exercised end to end.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

fn helper_binary() -> String {
    std::env::var("CARGO_BIN_EXE_tunnelforge-core").unwrap_or_else(|_| {
        let mut path = std::env::current_exe().unwrap();
        path.pop();
        if path.ends_with("deps") {
            path.pop();
        }
        path.push(if cfg!(windows) { "tunnelforge-core.exe" } else { "tunnelforge-core" });
        path.to_string_lossy().to_string()
    })
}

fn endpoint(prefix: &str, engine: &str) -> Option<Value> {
    let get = |key: &str| std::env::var(format!("{prefix}_{key}")).ok();
    Some(json!({
        "engine": engine,
        "host": get("HOST")?,
        "port": get("PORT")?.parse::<u16>().ok()?,
        "user": get("USER")?,
        "password": get("PASSWORD").unwrap_or_default(),
        "database": get("DATABASE")?,
    }))
}

struct Core {
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<Value>,
    buffer: Vec<Value>,
    seq: u64,
}

impl Core {
    fn start() -> Self {
        let mut child = Command::new(helper_binary())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { return };
                let value: Value = serde_json::from_str(&line).expect("every stdout line must be valid JSON");
                if tx.send(value).is_err() {
                    return;
                }
            }
        });
        Self { child, stdin, rx, buffer: Vec::new(), seq: 0 }
    }

    fn send(&mut self, command: &str, payload: Value) -> String {
        self.seq += 1;
        let id = format!("t-{}", self.seq);
        let line = json!({"command": command, "request_id": id, "payload": payload});
        writeln!(self.stdin, "{line}").unwrap();
        self.stdin.flush().unwrap();
        id
    }

    /// Events for `id` up to and including its result/error. Other requests' events are kept.
    fn finish(&mut self, id: &str, timeout: Duration) -> Vec<Value> {
        let deadline = Instant::now() + timeout;
        let mut events = Vec::new();
        loop {
            let mut rest = Vec::new();
            let mut done = false;
            for event in std::mem::take(&mut self.buffer) {
                if !done && event["request_id"] == id {
                    done = matches!(event["event"].as_str(), Some("result") | Some("error"));
                    events.push(event);
                } else {
                    rest.push(event);
                }
            }
            self.buffer = rest;
            if done {
                return events;
            }
            let left = deadline.saturating_duration_since(Instant::now());
            match self.rx.recv_timeout(left) {
                Ok(event) => self.buffer.push(event),
                Err(RecvTimeoutError::Timeout) => panic!("timed out waiting for {id}; got {events:?}"),
                Err(err) => panic!("core stdout closed: {err:?}"),
            }
        }
    }

    fn call(&mut self, command: &str, payload: Value) -> Value {
        let id = self.send(command, payload);
        self.finish(&id, Duration::from_secs(60)).pop().unwrap()
    }

    fn open(&mut self, endpoint: &Value) -> String {
        let result = self.call("connection.open", endpoint.clone());
        assert_eq!(result["success"], true, "{result}");
        result["connection_id"].as_str().unwrap().to_string()
    }

    fn open_prepared(&mut self, engine: &Engine) -> String {
        let conn = self.open(&engine.endpoint);
        for sql in engine.setup {
            let result = self.query(&conn, sql);
            assert_eq!(result["success"], true, "{result}");
        }
        conn
    }

    fn query(&mut self, conn: &str, sql: &str) -> Value {
        self.call("query.execute", json!({"connection_id": conn, "sql": sql}))
    }

    fn scalar(&mut self, conn: &str, sql: &str) -> String {
        let result = self.query(conn, sql);
        assert_eq!(result["success"], true, "{result}");
        let row = result["rows"][0].as_object().unwrap();
        match row.values().next().unwrap() {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        }
    }
}

impl Drop for Core {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

struct Engine {
    name: &'static str,
    endpoint: Value,
    sleep_60: &'static str,
    /// SQL returning 1 when a `sleep_60` statement is still executing on the server.
    still_running: &'static str,
    /// Wide row generator, `n` rows.
    big: fn(u64) -> String,
    create_table: &'static str,
    /// Idempotent statements run once per connection before the volume tests.
    setup: &'static [&'static str],
}

fn engines() -> Vec<Engine> {
    let mut out = Vec::new();
    if let Some(endpoint) = endpoint("TF_QUERY_LIVE_MYSQL", "mysql") {
        out.push(Engine {
            name: "mysql",
            endpoint,
            sleep_60: "SELECT SLEEP(60) AS tf_marker",
            still_running: "SELECT COUNT(*) FROM information_schema.PROCESSLIST \
                            WHERE INFO LIKE 'SELECT SLEEP(60)%' AND ID <> CONNECTION_ID()",
            big: |n| format!("SELECT a.id AS n, CONCAT('row-', b.id) AS c FROM tf_b_seq a JOIN tf_b_seq b ON b.id <= a.id LIMIT {n}"),
            setup: &[
                "SET SESSION cte_max_recursion_depth = 10000",
                "CREATE TABLE IF NOT EXISTS tf_b_seq (id INT PRIMARY KEY)",
                "INSERT IGNORE INTO tf_b_seq WITH RECURSIVE s(id) AS (SELECT 1 UNION ALL SELECT id + 1 FROM s WHERE id < 5000) SELECT id FROM s",
            ],
            create_table: "CREATE TABLE IF NOT EXISTS tf_b_txn (id INT) ENGINE=InnoDB",
        });
    }
    if let Some(endpoint) = endpoint("TF_QUERY_LIVE_PG", "postgresql") {
        out.push(Engine {
            name: "postgresql",
            endpoint,
            sleep_60: "SELECT pg_sleep(60) AS tf_marker",
            still_running: "SELECT COUNT(*) FROM pg_stat_activity \
                            WHERE state = 'active' AND query LIKE '%pg_sleep(60)%' AND query NOT LIKE '%pg_stat_activity%' AND pid <> pg_backend_pid()",
            big: |n| format!("SELECT g AS n, 'row-' || g AS c FROM generate_series(1, {n}) g"),
            create_table: "CREATE TABLE IF NOT EXISTS tf_b_txn (id INT)",
            setup: &[],
        });
    }
    out
}

fn skip_if_none(engines: &[Engine]) -> bool {
    // The CI live gate sets TF_LIVE_REQUIRED: both engines must be configured there.
    if std::env::var_os("TF_LIVE_REQUIRED").is_some() {
        assert_eq!(engines.len(), 2, "TF_LIVE_REQUIRED is set but TF_QUERY_LIVE_MYSQL_* / TF_QUERY_LIVE_PG_* are incomplete");
    }
    if engines.is_empty() {
        eprintln!("skipped: TF_QUERY_LIVE_MYSQL_* / TF_QUERY_LIVE_PG_* not set");
    }
    engines.is_empty()
}

#[test]
fn cancel_kills_server_query_and_session_stays_usable() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let observer = core.open(&engine.endpoint);
        let id = core.send(
            "query.execute",
            json!({"connection_id": conn, "sql": engine.sleep_60, "job_id": "sleep-1", "stream_rows": true}),
        );
        std::thread::sleep(Duration::from_millis(1500));
        assert_eq!(core.scalar(&observer, engine.still_running), "1", "{} sleeper not running", engine.name);

        let started = Instant::now();
        let cancel = core.call("query.cancel", json!({"job_id": "sleep-1"}));
        assert_eq!(cancel["cancelled"], true, "{cancel}");
        assert_eq!(cancel["server_cancel_sent"], true, "{cancel}");
        let events = core.finish(&id, Duration::from_secs(15));
        let last = events.last().unwrap();
        assert_eq!(last["error_code"], "query_cancelled", "{} {last}", engine.name);
        assert_eq!(last["cancelled"], true);
        eprintln!("{}: cancel round trip {:?}", engine.name, started.elapsed());

        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(core.scalar(&observer, engine.still_running), "0", "{} server query survived cancel", engine.name);
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1", "{} session unusable after cancel", engine.name);
    }
}

#[test]
fn second_query_on_busy_connection_is_rejected() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let id = core.send("query.execute", json!({"connection_id": conn, "sql": engine.sleep_60, "job_id": "busy-1"}));
        std::thread::sleep(Duration::from_millis(500));
        let busy = core.query(&conn, "SELECT 1 AS one");
        assert_eq!(busy["event"], "error", "{busy}");
        assert_eq!(busy["error_code"], "connection_busy", "{busy}");
        core.call("query.cancel", json!({"job_id": "busy-1"}));
        core.finish(&id, Duration::from_secs(15));
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1");
    }
}

#[test]
fn timeout_cancels_on_the_server() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let observer = core.open(&engine.endpoint);
        let started = Instant::now();
        let result = core.call("query.execute", json!({"connection_id": conn, "sql": engine.sleep_60, "timeout_ms": 800}));
        assert_eq!(result["error_code"], "query_timeout", "{result}");
        assert_eq!(result["timed_out"], true);
        assert!(started.elapsed() < Duration::from_secs(10), "{:?}", started.elapsed());
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(core.scalar(&observer, engine.still_running), "0");
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1");
    }
}

#[test]
fn streaming_emits_first_batch_early_and_truncates_at_limit() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open_prepared(&engine);
        let started = Instant::now();
        let id = core.send(
            "query.execute",
            json!({
                "connection_id": conn, "sql": (engine.big)(20_000_000), "stream_rows": true,
                "row_batch_size": 500, "max_rows": 100_000
            }),
        );
        let events = core.finish(&id, Duration::from_secs(120));
        let total = started.elapsed();
        let batches = events.iter().filter(|e| e["event"] == "row_batch").count();
        let rows: usize = events
            .iter()
            .filter(|e| e["event"] == "row_batch")
            .map(|e| e["rows"].as_array().unwrap().len())
            .sum();
        let result = events.last().unwrap();
        assert_eq!(result["success"], true, "{result}");
        assert_eq!(result["truncated"], true);
        assert_eq!(result["truncated_by"], "rows");
        assert_eq!(rows, 100_000);
        assert_eq!(result["rows_streamed"], 100_000);
        assert!(batches >= 200);
        assert_eq!(events[0]["event"], "columns");
        eprintln!("{}: 100k of 20M rows in {:?} ({} batches)", engine.name, total, batches);
        assert!(total < Duration::from_secs(60), "server was not stopped after truncation: {total:?}");
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1", "{} session unusable after truncation", engine.name);
    }
}

#[test]
fn byte_limit_truncates() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open_prepared(&engine);
        let result = core.call(
            "query.execute",
            json!({"connection_id": conn, "sql": (engine.big)(100_000), "max_bytes": 20_000}),
        );
        assert_eq!(result["truncated_by"], "bytes", "{}", result["message"]);
        let rows = result["rows"].as_array().unwrap().len();
        assert!(rows > 100 && rows < 2_000, "{rows}");
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1");
    }
}

#[test]
fn cancel_inside_manual_transaction_never_commits_or_rolls_back_silently() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let observer = core.open(&engine.endpoint);
        assert_eq!(core.query(&conn, engine.create_table)["success"], true);
        core.query(&conn, "DELETE FROM tf_b_txn");
        let begin = if engine.name == "mysql" { "START TRANSACTION" } else { "BEGIN" };
        assert_eq!(core.query(&conn, begin)["success"], true);
        assert_eq!(core.query(&conn, "INSERT INTO tf_b_txn VALUES (1)")["success"], true);

        let id = core.send("query.execute", json!({"connection_id": conn, "sql": engine.sleep_60, "job_id": "txn-1"}));
        std::thread::sleep(Duration::from_millis(800));
        core.call("query.cancel", json!({"job_id": "txn-1"}));
        let result = core.finish(&id, Duration::from_secs(15)).pop().unwrap();
        assert_eq!(result["error_code"], "query_cancelled");
        eprintln!("{}: in_transaction after cancel = {}", engine.name, result["in_transaction"]);
        if engine.name == "postgresql" {
            assert_eq!(result["in_transaction"], true, "{result}");
        }
        // Not committed by the cancel: invisible to another session.
        assert_eq!(core.scalar(&observer, "SELECT COUNT(*) FROM tf_b_txn"), "0");
        if engine.name == "mysql" {
            // MySQL keeps the transaction open with our earlier insert intact.
            assert_eq!(core.scalar(&conn, "SELECT COUNT(*) FROM tf_b_txn"), "1");
        }
        // The client decides; an explicit ROLLBACK still works on the same session.
        assert_eq!(core.query(&conn, "ROLLBACK")["success"], true);
        assert_eq!(core.scalar(&conn, "SELECT COUNT(*) FROM tf_b_txn"), "0");
        core.query(&conn, "DROP TABLE tf_b_txn");
    }
}

#[test]
fn multiple_statements_are_rejected() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let result = core.query(&conn, "SELECT 1 AS a; SELECT 2 AS b");
        assert_eq!(result["error_code"], "multiple_result_sets_unsupported", "{} {result}", engine.name);
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1");
    }
}

#[cfg(windows)]
fn rss_kb(pid: u32) -> Option<u64> {
    let out = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    let field = text.split("\",\"").nth(4)?.trim_matches(|c: char| !c.is_ascii_digit() && c != ',');
    field.replace(',', "").parse().ok()
}

#[cfg(not(windows))]
fn rss_kb(pid: u32) -> Option<u64> {
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    status
        .lines()
        .find(|l| l.starts_with("VmRSS:"))?
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
}

/// Streaming keeps core memory flat: 3M rows would be hundreds of MB if collected first.
#[test]
fn unbounded_stream_keeps_core_memory_flat() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open_prepared(&engine);
        let pid = core.child.id();
        let baseline = rss_kb(pid).unwrap_or(0);
        let id = core.send(
            "query.execute",
            json!({"connection_id": conn, "sql": (engine.big)(3_000_000), "stream_rows": true}),
        );
        let started = Instant::now();
        let mut peak = baseline;
        let mut first_batch = None;
        let mut rows = 0usize;
        loop {
            match core.rx.recv_timeout(Duration::from_secs(120)) {
                Ok(event) if event["request_id"] == id => {
                    if event["event"] == "row_batch" {
                        first_batch.get_or_insert(started.elapsed());
                        rows += event["rows"].as_array().unwrap().len();
                        if rows % 50_000 < 500 {
                            peak = peak.max(rss_kb(pid).unwrap_or(0));
                        }
                    } else if event["event"] == "result" || event["event"] == "error" {
                        assert_eq!(event["success"], true, "{event}");
                        break;
                    }
                }
                Ok(_) => {}
                Err(err) => panic!("{err:?}"),
            }
        }
        eprintln!(
            "{}: {rows} rows in {:?}, first batch {:?}, RSS baseline {} KiB peak {} KiB",
            engine.name,
            started.elapsed(),
            first_batch,
            baseline,
            peak
        );
        assert_eq!(rows, 3_000_000);
        assert!(peak < 200 * 1024, "core RSS grew to {peak} KiB while streaming");
    }
}

// ------------------------------------------------------------------ result export to file

fn vals_sql(engine: &Engine) -> Vec<&'static str> {
    if engine.name == "mysql" {
        vec![
            "DROP TABLE IF EXISTS tf_b_vals",
            "CREATE TABLE tf_b_vals (id INT PRIMARY KEY, s VARCHAR(100), n DECIMAL(30,10), b VARBINARY(16), \
             t DATETIME(6), d DATE) DEFAULT CHARSET=utf8mb4",
            "INSERT INTO tf_b_vals VALUES \
             (1, '', 12345678901234567890.1234567890, x'00ff10ab', '2024-02-29 23:59:59.123456', '2024-02-29'), \
             (2, NULL, NULL, NULL, NULL, NULL), \
             (3, CONCAT('a,b \"q\"', CHAR(10), 'line2 한글 ✓ 😀'), -0.0000000001, x'', '2024-01-01 00:00:00', '2024-01-01'), \
             (4, '=1+1', 0.5, x'41', NULL, NULL), \
             (5, '-abc', -5, NULL, NULL, NULL)",
        ]
    } else {
        vec![
            "DROP TABLE IF EXISTS tf_b_vals",
            "CREATE TABLE tf_b_vals (id INT PRIMARY KEY, s TEXT, n NUMERIC(30,10), b BYTEA, t TIMESTAMP(6), d DATE)",
            "INSERT INTO tf_b_vals VALUES \
             (1, '', 12345678901234567890.1234567890, decode('00ff10ab','hex'), '2024-02-29 23:59:59.123456', '2024-02-29'), \
             (2, NULL, NULL, NULL, NULL, NULL), \
             (3, E'a,b \"q\"\\nline2 한글 ✓ 😀', -0.0000000001, decode('','hex'), '2024-01-01 00:00:00', '2024-01-01'), \
             (4, '=1+1', 0.5, decode('41','hex'), NULL, NULL), \
             (5, '-abc', -5, NULL, NULL, NULL)",
        ]
    }
}

const VALS_QUERY: &str = "SELECT id, s, n, b, t, d FROM tf_b_vals ORDER BY id";

/// Minimal RFC 4180 reader: an empty unquoted field is NULL, `""` is the empty string.
fn parse_csv(data: &str) -> Vec<Vec<Option<String>>> {
    let mut rows = Vec::new();
    let mut row: Vec<Option<String>> = Vec::new();
    let mut field = String::new();
    let mut quoted = false;
    let mut in_quotes = false;
    let mut chars = data.chars().peekable();
    while let Some(c) = chars.next() {
        if in_quotes {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    field.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(c);
            }
            continue;
        }
        match c {
            '"' => {
                in_quotes = true;
                quoted = true;
            }
            ',' | '\r' | '\n' => {
                if c == '\r' {
                    chars.next(); // the \n
                }
                row.push(if field.is_empty() && !quoted { None } else { Some(std::mem::take(&mut field)) });
                quoted = false;
                if c != ',' {
                    rows.push(std::mem::take(&mut row));
                }
            }
            other => field.push(other),
        }
    }
    rows
}

fn partial_of(path: &std::path::Path) -> std::path::PathBuf {
    let mut partial = path.to_path_buf().into_os_string();
    partial.push(".partial");
    partial.into()
}

fn export_path(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join("tf_query_export_live");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(name);
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_file(partial_of(&path));
    path
}

fn export(core: &mut Core, conn: &str, sql: &str, output: Value) -> Vec<Value> {
    let id = core.send("query.execute", json!({"connection_id": conn, "sql": sql, "output": output}));
    core.finish(&id, Duration::from_secs(300))
}

#[test]
fn export_preserves_values_in_csv_and_jsonl() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        for sql in vals_sql(&engine) {
            let result = core.query(&conn, sql);
            assert_eq!(result["success"], true, "{} {result}", engine.name);
        }
        let s = |v: &str| Some(v.to_string());
        let expected: Vec<Vec<Option<String>>> = vec![
            vec![s("1"), s(""), s("12345678901234567890.1234567890"), s("00ff10ab"), s("2024-02-29 23:59:59.123456"), s("2024-02-29")],
            vec![s("2"), None, None, None, None, None],
            // MySQL renders DATETIME(6) with all six fractional digits; the server text is kept as is.
            vec![
                s("3"), s("a,b \"q\"\nline2 한글 ✓ 😀"), s("-0.0000000001"), s(""),
                s(if engine.name == "mysql" { "2024-01-01 00:00:00.000000" } else { "2024-01-01 00:00:00" }),
                s("2024-01-01"),
            ],
            vec![s("4"), s("=1+1"), s("0.5000000000"), s("41"), None, None],
            vec![s("5"), s("-abc"), s("-5.0000000000"), None, None, None],
        ];

        // CSV, guard off + BOM: exact values.
        let path = export_path(&format!("vals_{}.csv", engine.name));
        let events = export(
            &mut core,
            &conn,
            VALS_QUERY,
            json!({"path": path.to_string_lossy(), "format": "csv", "bom": true, "formula_guard": false}),
        );
        let result = events.last().unwrap();
        assert_eq!(result["success"], true, "{} {result}", engine.name);
        assert_eq!(result["rows_written"], 5);
        assert_eq!(result["output_path"], path.to_string_lossy().as_ref());
        assert!(!partial_of(&path).exists());
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(&bytes[..3], b"\xEF\xBB\xBF");
        let table = parse_csv(std::str::from_utf8(&bytes[3..]).unwrap());
        assert_eq!(table[0], ["id", "s", "n", "b", "t", "d"].map(|c| Some(c.to_string())));
        assert_eq!(&table[1..], &expected[..], "{}", engine.name);

        // CSV with the default guard: formulas escaped, plain numbers untouched.
        let path = export_path(&format!("vals_guard_{}.csv", engine.name));
        let events = export(&mut core, &conn, VALS_QUERY, json!({"path": path.to_string_lossy()}));
        assert_eq!(events.last().unwrap()["success"], true);
        let data = std::fs::read(&path).unwrap();
        assert_ne!(&data[..3], b"\xEF\xBB\xBF", "BOM is opt-in");
        let table = parse_csv(std::str::from_utf8(&data).unwrap());
        assert_eq!(table[4][1], s("'=1+1"));
        assert_eq!(table[5][1], s("'-abc"));
        assert_eq!(table[5][2], s("-5.0000000000"));

        // JSON Lines: NULL vs "" and exact decimal text.
        let path = export_path(&format!("vals_{}.jsonl", engine.name));
        let output = json!({"path": path.to_string_lossy(), "format": "jsonl", "binary": "base64"});
        let events = export(&mut core, &conn, VALS_QUERY, output.clone());
        assert_eq!(events.last().unwrap()["success"], true);
        let text = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<Value> = text.lines().map(|l| serde_json::from_str(l).unwrap()).collect();
        assert_eq!(lines.len(), 5);
        assert_eq!(lines[0]["s"], "");
        assert_eq!(lines[1]["s"], Value::Null);
        assert_eq!(lines[0]["n"], "12345678901234567890.1234567890");
        assert_eq!(lines[0]["b"], "AP8Qqw==");
        assert_eq!(lines[2]["s"], "a,b \"q\"\nline2 한글 ✓ 😀");
        eprintln!("{}: value round trip OK (csv guard off/on, jsonl)", engine.name);

        // An existing file is never silently replaced.
        let events = export(&mut core, &conn, VALS_QUERY, output);
        assert_eq!(events.last().unwrap()["event"], "error");
        assert!(events.last().unwrap()["message"].as_str().unwrap().contains("already exists"));
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1", "session usable after refused export");
        core.query(&conn, "DROP TABLE tf_b_vals");
    }
}

#[test]
fn export_cancel_and_timeout_never_leave_a_final_file() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open_prepared(&engine);

        // Cancel mid-export: no final file, partial deleted by default.
        let path = export_path(&format!("cancel_{}.csv", engine.name));
        let id = core.send(
            "query.execute",
            json!({
                "connection_id": conn, "sql": (engine.big)(20_000_000), "job_id": "exp-1",
                "output": {"path": path.to_string_lossy()}
            }),
        );
        std::thread::sleep(Duration::from_millis(1500));
        assert!(partial_of(&path).exists(), "data goes to .partial while running");
        assert!(!path.exists());
        core.call("query.cancel", json!({"job_id": "exp-1"}));
        let result = core.finish(&id, Duration::from_secs(60)).pop().unwrap();
        assert_eq!(result["error_code"], "query_cancelled", "{result}");
        assert_eq!(result["output_path"], Value::Null);
        if engine.name == "mysql" {
            // PostgreSQL may still be materializing generate_series() at this point.
            assert!(result["rows_written"].as_u64().unwrap() > 0);
        }
        assert!(!path.exists() && !partial_of(&path).exists(), "{} cancelled export left files", engine.name);

        // Timeout with keep_partial: partial is kept and reported, final never appears.
        let path = export_path(&format!("timeout_{}.csv", engine.name));
        let id = core.send(
            "query.execute",
            json!({
                "connection_id": conn, "sql": (engine.big)(20_000_000), "timeout_ms": 700,
                "output": {"path": path.to_string_lossy(), "keep_partial": true}
            }),
        );
        let result = core.finish(&id, Duration::from_secs(60)).pop().unwrap();
        assert_eq!(result["error_code"], "query_timeout", "{result}");
        let partial = result["partial_path"].as_str().expect("kept partial path is reported");
        assert!(partial.ends_with(".partial") && std::path::Path::new(partial).exists());
        assert_eq!(result["output_path"], Value::Null);
        assert!(!path.exists());
        let _ = std::fs::remove_file(partial);
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1");
    }
}

#[test]
fn export_failure_midway_leaves_no_final_file_and_bad_path_is_an_error() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines.iter().filter(|e| e.name == "postgresql") {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let path = export_path("midway_error.csv");
        // Fails at row 3, after rows 1-2 were already written to the partial file.
        let events = export(
            &mut core,
            &conn,
            "SELECT 1 / (3 - g) AS x FROM generate_series(1, 10) g",
            json!({"path": path.to_string_lossy()}),
        );
        assert_eq!(events.last().unwrap()["event"], "error", "{:?}", events.last());
        assert!(!path.exists() && !partial_of(&path).exists());
    }
    for engine in &engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let missing = std::env::temp_dir().join("tf_query_export_live").join("no_such_dir").join("x.csv");
        let events = export(&mut core, &conn, "SELECT 1 AS one", json!({"path": missing.to_string_lossy()}));
        assert_eq!(events.last().unwrap()["event"], "error");
        assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1");
    }
}

/// Millions of rows go to disk with flat core memory and an atomic, complete final file.
#[test]
fn export_large_result_is_streamed_with_flat_memory() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open_prepared(&engine);
        let pid = core.child.id();
        let baseline = rss_kb(pid).unwrap_or(0);
        let path = export_path(&format!("big_{}.csv", engine.name));
        let id = core.send(
            "query.execute",
            json!({"connection_id": conn, "sql": (engine.big)(3_000_000), "output": {"path": path.to_string_lossy()}}),
        );
        let started = Instant::now();
        let mut peak = baseline;
        let mut progress_events = 0;
        let result = loop {
            match core.rx.recv_timeout(Duration::from_secs(300)) {
                Ok(event) if event["request_id"] == id => match event["event"].as_str() {
                    Some("progress") => {
                        progress_events += 1;
                        peak = peak.max(rss_kb(pid).unwrap_or(0));
                    }
                    Some("result") | Some("error") => break event,
                    _ => {}
                },
                Ok(_) => {}
                Err(err) => panic!("{err:?}"),
            }
        };
        assert_eq!(result["success"], true, "{result}");
        assert_eq!(result["rows_written"], 3_000_000);
        let size = std::fs::metadata(&path).unwrap().len();
        assert_eq!(result["bytes_written"], size);
        assert!(!partial_of(&path).exists());
        let lines = std::fs::read(&path).unwrap().iter().filter(|b| **b == b'\n').count();
        assert_eq!(lines, 3_000_001, "header + rows");
        eprintln!(
            "{}: exported 3M rows ({} MiB) in {:?}, {} progress events, RSS baseline {} KiB peak {} KiB",
            engine.name, size >> 20, started.elapsed(), progress_events, baseline, peak
        );
        assert!(progress_events > 0);
        assert!(peak < 100 * 1024, "core RSS grew to {peak} KiB while exporting");
        let _ = std::fs::remove_file(&path);
    }
}

/// A file export re-runs the query, so the server must refuse anything that writes.
#[test]
fn export_runs_read_only_and_never_changes_data() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        let observer = core.open(&engine.endpoint);
        let mut writers: Vec<(&str, &str)> = Vec::new();
        let setup: Vec<&str> = if engine.name == "postgresql" {
            writers.push(("DELETE FROM tf_b_del RETURNING id", "tf_b_del"));
            writers.push(("SELECT nextval('tf_b_sq') AS v", "seq"));
            vec![
                "DROP TABLE IF EXISTS tf_b_del",
                "CREATE TABLE tf_b_del (id INT)",
                "INSERT INTO tf_b_del VALUES (1),(2),(3)",
                "DROP SEQUENCE IF EXISTS tf_b_sq",
                "CREATE SEQUENCE tf_b_sq",
            ]
        } else {
            writers.push(("SELECT tf_b_ins() AS v", "tf_b_fx"));
            vec![
                "DROP TABLE IF EXISTS tf_b_del",
                "CREATE TABLE tf_b_del (id INT) ENGINE=InnoDB",
                "INSERT INTO tf_b_del VALUES (1),(2),(3)",
                "DROP TABLE IF EXISTS tf_b_fx",
                "CREATE TABLE tf_b_fx (id INT) ENGINE=InnoDB",
                "SET GLOBAL log_bin_trust_function_creators = 1",
                "DROP FUNCTION IF EXISTS tf_b_ins",
                "CREATE FUNCTION tf_b_ins() RETURNS INT MODIFIES SQL DATA BEGIN INSERT INTO tf_b_fx VALUES (1); RETURN 1; END",
            ]
        };
        for sql in setup {
            let result = core.query(&conn, sql);
            assert_eq!(result["success"], true, "{} {sql} {result}", engine.name);
        }

        for (sql, watched) in writers {
            let path = export_path(&format!("ro_{}.csv", engine.name));
            let events = export(&mut core, &conn, sql, json!({"path": path.to_string_lossy()}));
            let last = events.last().unwrap();
            assert_eq!(last["event"], "error", "{} {sql}: {last}", engine.name);
            assert_eq!(last["error_code"], "export_requires_read_only", "{} {sql}: {last}", engine.name);
            assert!(!path.exists() && !partial_of(&path).exists(), "{} left a file", engine.name);
            match watched {
                "seq" => assert_eq!(
                    core.scalar(&observer, "SELECT is_called FROM tf_b_sq"),
                    "false",
                    "nextval() advanced the sequence"
                ),
                table => {
                    let expected = if table == "tf_b_del" { "3" } else { "0" };
                    assert_eq!(core.scalar(&observer, &format!("SELECT COUNT(*) FROM {table}")), expected, "{} {table} changed", engine.name);
                }
            }
        }

        // The failed export must not leave the session in a read-only transaction.
        assert_eq!(core.query(&conn, "INSERT INTO tf_b_del VALUES (9)")["success"], true);
        assert_eq!(core.scalar(&observer, "SELECT COUNT(*) FROM tf_b_del"), "4");

        // Plain SELECT exports keep working.
        let path = export_path(&format!("ro_ok_{}.csv", engine.name));
        let events = export(&mut core, &conn, "SELECT id FROM tf_b_del ORDER BY id", json!({"path": path.to_string_lossy()}));
        assert_eq!(events.last().unwrap()["success"], true, "{:?}", events.last());
        assert_eq!(events.last().unwrap()["rows_written"], 4);

        // A caller's open transaction is refused (never committed or rolled back by the export).
        let begin = if engine.name == "mysql" { "START TRANSACTION" } else { "BEGIN" };
        assert_eq!(core.query(&conn, begin)["success"], true);
        assert_eq!(core.query(&conn, "INSERT INTO tf_b_del VALUES (10)")["success"], true);
        let path = export_path(&format!("ro_tx_{}.csv", engine.name));
        let events = export(&mut core, &conn, "SELECT id FROM tf_b_del", json!({"path": path.to_string_lossy()}));
        let last = events.last().unwrap();
        assert_eq!(last["error_code"], "export_session_in_transaction", "{} {last}", engine.name);
        assert!(!path.exists());
        assert_eq!(core.scalar(&conn, "SELECT COUNT(*) FROM tf_b_del"), "5", "{} txn state lost", engine.name);
        assert_eq!(core.scalar(&observer, "SELECT COUNT(*) FROM tf_b_del"), "4", "{} txn was committed", engine.name);
        assert_eq!(core.query(&conn, "ROLLBACK")["success"], true);
        assert_eq!(core.scalar(&conn, "SELECT COUNT(*) FROM tf_b_del"), "4");
        eprintln!("{}: read-only export enforced (writers refused, data/sequence unchanged, open txn protected)", engine.name);
        for sql in ["DROP TABLE IF EXISTS tf_b_del", "DROP TABLE IF EXISTS tf_b_fx"] {
            core.query(&conn, sql);
        }
    }
}

/// `in_transaction` is a real value on both engines (MySQL reads the server's transaction flag).
#[test]
fn in_transaction_is_reported_after_success_cancel_and_rollback() {
    let engines = engines();
    if skip_if_none(&engines) {
        return;
    }
    for engine in engines {
        let mut core = Core::start();
        let conn = core.open(&engine.endpoint);
        assert_eq!(core.query(&conn, engine.create_table)["success"], true);
        let cancel_state = |core: &mut Core, job: &str| -> Value {
            let id = core.send("query.execute", json!({"connection_id": conn, "sql": engine.sleep_60, "job_id": job}));
            std::thread::sleep(Duration::from_millis(600));
            core.call("query.cancel", json!({"job_id": job}));
            core.finish(&id, Duration::from_secs(15)).pop().unwrap()["in_transaction"].clone()
        };

        // autocommit session
        let result = core.query(&conn, "SELECT 1 AS one");
        if engine.name == "mysql" {
            assert_eq!(result["in_transaction"], false, "{result}");
        }
        assert_eq!(cancel_state(&mut core, "tx-0"), json!(false), "{} idle session", engine.name);

        // open transaction with an uncommitted write
        let begin = if engine.name == "mysql" { "START TRANSACTION" } else { "BEGIN" };
        assert_eq!(core.query(&conn, begin)["success"], true);
        assert_eq!(core.query(&conn, "INSERT INTO tf_b_txn VALUES (1)")["success"], true);
        if engine.name == "mysql" {
            assert_eq!(core.query(&conn, "SELECT 1 AS one")["in_transaction"], true);
        }
        assert_eq!(cancel_state(&mut core, "tx-1"), json!(true), "{} open transaction", engine.name);

        // after rollback the session is clean again
        core.query(&conn, "ROLLBACK");
        assert_eq!(cancel_state(&mut core, "tx-2"), json!(false), "{} after rollback", engine.name);
        core.query(&conn, "DROP TABLE tf_b_txn");
        eprintln!("{}: in_transaction idle=false, open txn=true after cancel, rollback=false", engine.name);
    }
}
