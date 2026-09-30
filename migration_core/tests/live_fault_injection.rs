//! TF-STATUS-105 / 108 live fault verification.
//!
//! Opt-in (`#[ignore]`): needs disposable `TF_MYSQL_HOST` / `TF_POSTGRES_HOST`
//! `tf_test` databases (same environment as the other live gates). The MySQL
//! admin account must be `root` because the tests create and drop a restricted
//! account. Import faults are injected by an in-process TCP proxy that severs
//! established connections, or discards server replies (statement delivered,
//! acknowledgement lost) after a byte threshold.

use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

fn endpoints() -> Vec<Endpoint> {
    [("mysql", "TF_MYSQL_HOST", 3306, "root"), ("postgresql", "TF_POSTGRES_HOST", 5432, "postgres")]
        .into_iter()
        .map(|(engine, host, port, user)| Endpoint {
            engine: engine.into(),
            host: std::env::var(host).expect("live database host required"),
            port,
            user: user.into(),
            password: "tf_local_test".into(),
            database: "tf_test".into(),
            schema: None,
            tls: Default::default(),
        })
        .filter(|endpoint| std::env::var("TF_EXPORT_ENGINE").map(|engine| engine == endpoint.engine).unwrap_or(true))
        .collect()
}

fn events(endpoint: &Endpoint, command: &str, mut payload: Value) -> Vec<Value> {
    payload["endpoint"] = serde_json::to_value(endpoint).unwrap();
    handle_request(Request { command: command.into(), request_id: None, payload })
}

fn error_message(events: &[Value]) -> Option<String> {
    events.iter().find(|e| e["event"] == "error").map(|e| e["message"].as_str().unwrap_or_default().to_string())
}

fn ok(events: Vec<Value>) -> Value {
    assert!(error_message(&events).is_none(), "{events:#?}");
    events.into_iter().find(|e| e["event"] == "result").expect("result event")
}

fn scalar(endpoint: &Endpoint, sql: &str) -> i64 {
    try_scalar(endpoint, sql).unwrap_or_else(|| panic!("scalar {sql}"))
}

fn try_scalar(endpoint: &Endpoint, sql: &str) -> Option<i64> {
    let evs = events(endpoint, "query.execute", json!({"sql": sql}));
    if error_message(&evs).is_some() {
        if std::env::var("TF_FAULT_DEBUG").is_ok() {
            eprintln!("try_scalar {sql}: {evs:?}");
        }
        return None;
    }
    let result = evs.into_iter().find(|e| e["event"] == "result").unwrap();
    if std::env::var("TF_FAULT_DEBUG").is_ok() {
        eprintln!("try_scalar {sql}: {result}");
    }
    let cell = result["rows"][0].as_object().and_then(|row| row.values().next()).unwrap_or(&Value::Null);
    cell.as_i64().or_else(|| cell.as_str().and_then(|s| s.parse().ok()))
}

// ---------------------------------------------------------------- proxy

#[derive(Default)]
struct ProxyState {
    conns: Mutex<Vec<TcpStream>>,
    c2s_bytes: AtomicU64,
    /// Fault when this many client->server bytes have passed (0 = never).
    threshold: AtomicU64,
    /// true: discard server replies and then cut (ack lost); false: cut at once.
    drop_replies: AtomicBool,
    discard: AtomicBool,
    fired: AtomicBool,
}

impl ProxyState {
    fn sever_all(&self) {
        for stream in self.conns.lock().unwrap().drain(..) {
            let _ = stream.shutdown(Shutdown::Both);
        }
    }

    fn fire(self: &Arc<Self>) {
        if self.fired.swap(true, Ordering::SeqCst) {
            return;
        }
        if self.drop_replies.load(Ordering::SeqCst) {
            self.discard.store(true, Ordering::SeqCst);
            let state = self.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(300));
                state.sever_all();
                // The fault hits established connections only; new ones see a healthy server.
                state.discard.store(false, Ordering::SeqCst);
            });
        } else {
            self.sever_all();
        }
    }
}

struct Proxy {
    port: u16,
    state: Arc<ProxyState>,
}

impl Proxy {
    fn start(upstream_host: String, upstream_port: u16) -> Proxy {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let state = Arc::new(ProxyState::default());
        let accept_state = state.clone();
        thread::spawn(move || {
            for client in listener.incoming().flatten() {
                let Ok(server) = TcpStream::connect((upstream_host.as_str(), upstream_port)) else { continue };
                {
                    let mut conns = accept_state.conns.lock().unwrap();
                    conns.push(client.try_clone().unwrap());
                    conns.push(server.try_clone().unwrap());
                }
                let (mut c_read, mut s_write) = (client.try_clone().unwrap(), server.try_clone().unwrap());
                let state = accept_state.clone();
                thread::spawn(move || {
                    let mut buf = [0u8; 16384];
                    while let Ok(n) = c_read.read(&mut buf) {
                        if n == 0 || s_write.write_all(&buf[..n]).is_err() {
                            break;
                        }
                        let total = state.c2s_bytes.fetch_add(n as u64, Ordering::SeqCst) + n as u64;
                        let threshold = state.threshold.load(Ordering::SeqCst);
                        if threshold > 0 && total >= threshold {
                            state.fire();
                        }
                    }
                    let _ = s_write.shutdown(Shutdown::Both);
                });
                let (mut s_read, mut c_write) = (server, client);
                let state = accept_state.clone();
                thread::spawn(move || {
                    let mut buf = [0u8; 16384];
                    while let Ok(n) = s_read.read(&mut buf) {
                        if n == 0 {
                            break;
                        }
                        if state.discard.load(Ordering::SeqCst) {
                            continue;
                        }
                        if c_write.write_all(&buf[..n]).is_err() {
                            break;
                        }
                    }
                    let _ = c_write.shutdown(Shutdown::Both);
                });
            }
        });
        Proxy { port, state }
    }

    fn endpoint(&self, base: &Endpoint) -> Endpoint {
        Endpoint { host: "127.0.0.1".into(), port: self.port, ..base.clone() }
    }
}

// ---------------------------------------------------------------- helpers

fn read_rows(dir: &std::path::Path, table: &Value) -> Vec<Value> {
    let mut rows = Vec::new();
    for chunk in table["chunk_sha256"].as_object().unwrap().keys() {
        let text = fs::read_to_string(dir.join(table["path"].as_str().unwrap()).join(chunk)).unwrap();
        rows.extend(text.lines().map(|line| serde_json::from_str::<Value>(line).unwrap()));
    }
    rows
}

fn num(value: &Value) -> i64 {
    value.as_i64().or_else(|| value.as_str().and_then(|s| s.parse().ok())).unwrap_or_else(|| panic!("not a number: {value}"))
}

fn manifest(dir: &std::path::Path) -> Value {
    serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap()
}

// ---------------------------------------------------------------- 105

/// Two tables always change together in one transaction. A single point-in-time
/// snapshot therefore has identical id sets and value sums in both.
struct PairWriter {
    stop: Arc<AtomicBool>,
    handle: Option<thread::JoinHandle<u64>>,
}

impl PairWriter {
    fn start(endpoint: Endpoint, a: String, b: String, seeded: i64) -> PairWriter {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let handle = thread::spawn(move || {
            let mysql = endpoint.engine == "mysql";
            let mut adapter = LiveAdapter::connect(&endpoint).unwrap();
            let (begin, commit) = if mysql { ("START TRANSACTION", "COMMIT") } else { ("BEGIN", "COMMIT") };
            let max_id = scalar(&endpoint, &format!("SELECT MAX(id) FROM {a}"));
            let (mut next, mut commits) = (max_id + 1, 0u64);
            while !flag.load(Ordering::SeqCst) {
                let target = 1 + (commits as i64 * 7) % seeded;
                adapter.execute_sql(begin).unwrap();
                for table in [&a, &b] {
                    adapter.execute_sql(&format!("INSERT INTO {table} (id, v) VALUES ({next}, 1)")).unwrap();
                    adapter.execute_sql(&format!("UPDATE {table} SET v = v + 1 WHERE id = {target}")).unwrap();
                }
                adapter.execute_sql(commit).unwrap();
                next += 1;
                commits += 1;
            }
            commits
        });
        PairWriter { stop, handle: Some(handle) }
    }

    fn finish(mut self) -> u64 {
        self.stop.store(true, Ordering::SeqCst);
        self.handle.take().unwrap().join().unwrap()
    }
}

fn setup_pair(admin: &mut LiveAdapter, a: &str, b: &str, seeded: i64) {
    for table in [a, b] {
        admin.execute_sql(&format!("DROP TABLE IF EXISTS {table}")).unwrap();
        admin.execute_sql(&format!("CREATE TABLE {table} (id BIGINT PRIMARY KEY, v INT NOT NULL)")).unwrap();
        // Bulk seed without generate_series/recursive CTE differences: chunked INSERTs.
        let mut id = 1;
        while id <= seeded {
            let values: Vec<String> = (id..(id + 500).min(seeded + 1)).map(|i| format!("({i}, 1)")).collect();
            admin.execute_sql(&format!("INSERT INTO {table} (id, v) VALUES {}", values.join(","))).unwrap();
            id += 500;
        }
    }
}

/// Export while the pair writer commits continuously; assert one point in time.
fn export_and_assert_single_point(
    export_endpoint: &Endpoint,
    writer_endpoint: &Endpoint,
    a: &str,
    b: &str,
    extra: Value,
    label: &str,
) -> Value {
    let dir = std::env::temp_dir().join(format!("{a}_{label}"));
    let _ = fs::remove_dir_all(&dir);
    let writer = PairWriter::start(writer_endpoint.clone(), a.into(), b.into(), 5000);
    thread::sleep(Duration::from_millis(300));
    let mut payload = json!({"tables": [a, b], "output_dir": dir, "chunk_size": 100,
        "data_format": "jsonl", "compression": "none", "overwrite": true});
    for (key, value) in extra.as_object().unwrap() {
        payload[key] = value.clone();
    }
    let result = events(export_endpoint, "dump.run", payload);
    let commits = writer.finish();
    assert!(commits > 20, "{label}: writer must have committed during export (commits={commits})");
    ok(result);
    let manifest = manifest(&dir);
    let sets: Vec<BTreeMap<i64, i64>> = [a, b]
        .iter()
        .map(|name| {
            let table = manifest["tables"].as_array().unwrap().iter().find(|t| t["name"] == *name).unwrap();
            read_rows(&dir, table).iter().map(|row| (num(&row["id"]), num(&row["v"]))).collect()
        })
        .collect();
    assert_eq!(sets[0], sets[1], "{label}: tables come from different points in time (commits={commits})");
    assert!(sets[0].len() >= 5000, "{label}");
    fs::remove_dir_all(&dir).unwrap();
    manifest
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn concurrent_writes_export_is_one_point_in_time() {
    for endpoint in endpoints() {
        let (a, b) = (format!("tf_fault_a_{}", std::process::id()), format!("tf_fault_b_{}", std::process::id()));
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        setup_pair(&mut admin, &a, &b, 5000);
        if endpoint.engine == "postgresql" {
            let manifest = export_and_assert_single_point(&endpoint, &endpoint, &a, &b, json!({"threads": 8}), "pg");
            assert_eq!(manifest["snapshot_policy"], "postgresql_repeatable_read_snapshot");
            assert_eq!(manifest["strict_export"], true);
        } else {
            // Privileged account: strict shared snapshot across four workers.
            let manifest = export_and_assert_single_point(&endpoint, &endpoint, &a, &b, json!({"threads": 4, "mysql_snapshot_mode": "parallel_strict"}), "strict");
            assert_eq!(manifest["snapshot_policy"], "mysql_shared_consistent_snapshot");
            assert_eq!(manifest["strict_export"], true);
        }
        admin.execute_sql(&format!("DROP TABLE {a}")).unwrap();
        admin.execute_sql(&format!("DROP TABLE {b}")).unwrap();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST tf_test database with a root account"]
fn limited_mysql_account_export_is_refused_or_consistent_and_manifest_is_honest() {
    let Some(root) = endpoints().into_iter().find(|endpoint| endpoint.engine == "mysql") else { return };
    let user = format!("tf_fault_lim_{}", std::process::id());
    let (a, b) = (format!("tf_fault_a_{}", std::process::id()), format!("tf_fault_b_{}", std::process::id()));
    let mut admin = LiveAdapter::connect(&root).unwrap();
    setup_pair(&mut admin, &a, &b, 5000);
    admin.execute_sql(&format!("DROP USER IF EXISTS '{user}'@'%'")).unwrap();
    admin.execute_sql(&format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    admin.execute_sql(&format!("GRANT SELECT ON tf_test.* TO '{user}'@'%'")).unwrap();
    let limited = Endpoint { user: user.clone(), ..root.clone() };

    // (a) strict parallel snapshot needs global RELOAD/BACKUP_ADMIN: refused, nothing exported.
    let dir = std::env::temp_dir().join(format!("{a}_refused"));
    let _ = fs::remove_dir_all(&dir);
    let refused = events(&limited, "dump.run", json!({"tables": [a, b], "output_dir": dir, "threads": 4,
        "data_format": "jsonl", "compression": "none", "mysql_snapshot_mode": "parallel_strict"}));
    let message = error_message(&refused).unwrap_or_else(|| panic!("limited account must be refused: {refused:#?}"));
    assert!(message.contains("MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED"), "{message}");
    assert!(!dir.join("_tunnelforge_dump.json").exists(), "no success manifest after refusal");
    let _ = fs::remove_dir_all(&dir);

    // (b)/(c) explicit fallbacks are one-transaction snapshots even under concurrent writes.
    for mode in ["single_connection", "parallel_no_backup_lock"] {
        let manifest = export_and_assert_single_point(&limited, &root, &a, &b, json!({"threads": 4, "mysql_snapshot_mode": mode}), mode);
        assert_eq!(manifest["snapshot_policy"], "mysql_single_connection_consistent_snapshot", "{mode}");
        assert_eq!(manifest["strict_export"], true, "{mode}: single consistent transaction is a strict export");
        assert!(manifest["manifest_warnings"].as_array().unwrap().is_empty(), "{mode}: {manifest}");
    }

    admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
    admin.execute_sql(&format!("DROP TABLE {a}")).unwrap();
    admin.execute_sql(&format!("DROP TABLE {b}")).unwrap();
}

// ---------------------------------------------------------------- 108

const KEYED_ROWS: i64 = 30_000;
const HEAP_ROWS: i64 = 10_000;
const PRE_ROWS: i64 = 5;

fn fill(admin: &mut LiveAdapter, table: &str, from: i64, to: i64, offset: i64) {
    let mut id = from;
    while id <= to {
        let values: Vec<String> = (id..(id + 500).min(to + 1)).map(|i| format!("({}, 'payload-{i}-{}')", i + offset, "x".repeat(40))).collect();
        admin.execute_sql(&format!("INSERT INTO {table} (id, note) VALUES {}", values.join(","))).unwrap();
        id += 500;
    }
}

struct ImportFixture {
    keyed: String,
    heap: String,
    dump: std::path::PathBuf,
}

/// Exports a keyed and a keyless table, then leaves only PRE_ROWS unrelated rows in the target.
fn import_fixture(endpoint: &Endpoint, admin: &mut LiveAdapter, tag: &str) -> ImportFixture {
    let base = format!("tf_fault_imp_{tag}_{}", std::process::id());
    let (keyed, heap) = (format!("{base}_keyed"), format!("{base}_heap"));
    for table in [&keyed, &heap] {
        admin.execute_sql(&format!("DROP TABLE IF EXISTS {table}")).unwrap();
    }
    admin.execute_sql(&format!("CREATE TABLE {keyed} (id BIGINT PRIMARY KEY, note VARCHAR(100))")).unwrap();
    admin.execute_sql(&format!("CREATE TABLE {heap} (id BIGINT, note VARCHAR(100))")).unwrap();
    fill(admin, &keyed, 1, KEYED_ROWS, 0);
    fill(admin, &heap, 1, HEAP_ROWS, 0);
    let dump = std::env::temp_dir().join(format!("{base}_dump"));
    let _ = fs::remove_dir_all(&dump);
    ok(events(endpoint, "dump.run", json!({"tables": [keyed, heap], "output_dir": dump, "chunk_size": 1000,
        "data_format": "tsv", "compression": "none", "threads": 1, "mysql_snapshot_mode": "single_connection"})));
    reset_target(admin, &keyed, &heap);
    ImportFixture { keyed, heap, dump }
}

/// Target holds only rows that are not part of the dump (ids far outside its range).
fn reset_target(admin: &mut LiveAdapter, keyed: &str, heap: &str) {
    for table in [keyed, heap] {
        admin.execute_sql(&format!("DELETE FROM {table}")).unwrap();
        fill(admin, table, 1, PRE_ROWS, 5_000_000);
    }
}

fn import_report(events: &[Value]) -> Value {
    let path = events.iter().rev().find_map(|e| (e["event"] == "import_report").then(|| e["report_path"].as_str().map(str::to_string)).flatten())
        .expect("import_report event with a path");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

/// Invariants that must hold whatever moment the connection died.
fn assert_target_sound(endpoint: &Endpoint, fixture: &ImportFixture, report: &Value, merge: bool, label: &str) {
    let expect_full_on_loaded = true;
    for (table, total) in [(&fixture.keyed, KEYED_ROWS), (&fixture.heap, HEAP_ROWS)] {
        if merge {
            let pre = scalar(endpoint, &format!("SELECT COUNT(*) FROM {table} WHERE id >= 5000000"));
            assert_eq!(pre, PRE_ROWS, "{label}: {table} pre-existing rows must be preserved (no truncate)");
        }
        // replace may have dropped the table when it failed; then nothing can be checked.
        let Some(loaded) = try_scalar(endpoint, &format!("SELECT COUNT(*) FROM {table} WHERE id < 5000000")) else {
            assert!(!merge, "{label}: merge target table vanished");
            continue;
        };
        let distinct = scalar(endpoint, &format!("SELECT COUNT(DISTINCT id) FROM {table} WHERE id < 5000000"));
        assert_eq!(loaded, distinct, "{label}: {table} has duplicated rows (a chunk was replayed)");
        assert!(loaded <= total, "{label}: {table} more rows than the dump");
        let in_loaded = report["data_loaded_tables"].as_array().unwrap().iter().any(|t| t == table.as_str());
        if in_loaded && expect_full_on_loaded {
            assert_eq!(loaded, total, "{label}: {table} reported data-loaded but is incomplete");
        }
        let failed = report["failed_tables"].as_array().unwrap().iter().any(|t| t == table.as_str());
        let unattempted = report["unattempted_tables"].as_array().unwrap().iter().any(|t| t == table.as_str());
        if unattempted {
            assert_eq!(loaded, 0, "{label}: {table} reported unattempted but holds rows");
        }
        if loaded > 0 && loaded < total {
            assert!(failed && report["partial_data_may_exist"] == true, "{label}: partial {table} not reported as failed: {report}");
        }
    }
}

fn run_import_fault(endpoint: &Endpoint, mode: &str, threads: u64, drop_replies: bool, fraction: f64, tag: &str, load_data: bool) {
    let mut admin = LiveAdapter::connect(endpoint).unwrap();
    let fixture = import_fixture(endpoint, &mut admin, tag);
    let proxy = Proxy::start(endpoint.host.clone(), endpoint.port);
    let via = proxy.endpoint(endpoint);
    let payload = |extra: Value| {
        let mut p = json!({"tables": [fixture.keyed, fixture.heap], "input_dir": fixture.dump, "mode": mode,
            "threads": threads, "strict_manifest": true});
        if load_data {
            // Fast path: LOAD DATA LOCAL instead of the default INSERT fallback.
            p["mysql_local_infile_policy"] = json!("temporary_global");
        }
        for (k, v) in extra.as_object().unwrap() { p[k.as_str()] = v.clone(); }
        p
    };

    // Baseline through the proxy measures how many client bytes a clean import sends.
    let baseline = events(&via, "dump.import", payload(json!({})));
    ok(baseline);
    let full_bytes = proxy.state.c2s_bytes.load(Ordering::SeqCst);
    assert!(full_bytes > 200_000, "baseline traffic too small: {full_bytes}");
    reset_target(&mut admin, &fixture.keyed, &fixture.heap);
    proxy.state.c2s_bytes.store(0, Ordering::SeqCst);

    proxy.state.drop_replies.store(drop_replies, Ordering::SeqCst);
    proxy.state.threshold.store((full_bytes as f64 * fraction) as u64, Ordering::SeqCst);
    let faulted = events(&via, "dump.import", payload(json!({})));
    assert!(proxy.state.fired.load(Ordering::SeqCst), "fault was never injected");
    proxy.state.threshold.store(0, Ordering::SeqCst);
    thread::sleep(Duration::from_millis(800));

    let label = format!("{} {mode} threads={threads} drop_replies={drop_replies} at {fraction} load_data={load_data}", endpoint.engine);
    let report = import_report(&faulted);
    match error_message(&faulted) {
        Some(message) => {
            assert_eq!(report["status"], "failed", "{label}: {report}");
            assert_eq!(report["success"], false, "{label}");
            assert!(report["error"].as_str().is_some(), "{label}: failure must carry the error");
            eprintln!("{label}: failed as expected: {message}");
            assert_target_sound(endpoint, &fixture, &report, mode == "merge", &label);
        }
        None => {
            // Only a fresh replacement table may be cleared and replayed; the end state must be exact.
            assert_ne!(mode, "merge", "{label}: merge must not report success after losing a connection: {report}");
            for (table, total) in [(&fixture.keyed, KEYED_ROWS), (&fixture.heap, HEAP_ROWS)] {
                assert_eq!(scalar(endpoint, &format!("SELECT COUNT(*) FROM {table}")), total, "{label}: {table}");
                assert_eq!(scalar(endpoint, &format!("SELECT COUNT(DISTINCT id) FROM {table}")), total, "{label}: {table}");
            }
            eprintln!("{label}: recovered with exact rows");
        }
    }
    if load_data {
        // temporary_global must restore the server setting even after a severed import.
        assert_eq!(scalar(endpoint, "SELECT @@GLOBAL.local_infile"), 0, "{label}: local_infile was not restored");
    }
    for table in [&fixture.keyed, &fixture.heap] {
        admin.execute_sql(&format!("DROP TABLE {table}")).unwrap();
    }
    let _ = fs::remove_dir_all(&fixture.dump);
}

type Scenario = (u64, bool, f64, &'static str, bool);

fn scenarios(endpoint: &Endpoint, common: &[Scenario], mysql_load_data: &[Scenario]) -> Vec<Scenario> {
    let mut all = common.to_vec();
    if endpoint.engine == "mysql" {
        all.extend_from_slice(mysql_load_data);
    }
    all
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn merge_import_survives_connection_loss_without_duplicates_or_truncation() {
    for endpoint in endpoints() {
        let list = scenarios(
            &endpoint,
            &[(1, false, 0.45, "a", false), (1, true, 0.45, "b", false), (4, false, 0.3, "c", false), (4, true, 0.7, "d", false), (1, false, 0.85, "e", false)],
            &[(1, false, 0.45, "i", true), (1, true, 0.45, "j", true), (4, false, 0.3, "k", true), (4, true, 0.7, "l", true)],
        );
        for (threads, drop_replies, fraction, tag, load_data) in list {
            run_import_fault(&endpoint, "merge", threads, drop_replies, fraction, tag, load_data);
        }
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn replace_import_survives_connection_loss_with_exact_or_honestly_failed_result() {
    for endpoint in endpoints() {
        let list = scenarios(
            &endpoint,
            &[(1, false, 0.45, "f", false), (1, true, 0.45, "g", false), (4, false, 0.3, "h", false)],
            &[(1, false, 0.45, "m", true), (1, true, 0.45, "n", true), (4, false, 0.3, "o", true)],
        );
        for (threads, drop_replies, fraction, tag, load_data) in list {
            run_import_fault(&endpoint, "replace", threads, drop_replies, fraction, tag, load_data);
        }
    }
}
