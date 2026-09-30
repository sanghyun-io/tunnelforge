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
