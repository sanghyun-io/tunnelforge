//! Opt-in live checks for TF-STATUS-128 (read-only production sessions).
//!
//! Skipped unless `TF_QUERY_LIVE_MYSQL_*` / `TF_QUERY_LIVE_PG_*` are set. Disposable containers only.
//! Every statement the server does not stop by itself must be refused by the core
//! (`error_code: read_only_session`); data must be identical before and after.

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


struct Case {
    engine: &'static str,
    endpoint: Value,
    setup: Vec<&'static str>,
    /// (label, statement): refused with read_only_session and no effect on the data.
    refused: Vec<(&'static str, &'static str)>,
    /// Statements that must keep working in a read-only session.
    allowed: Vec<&'static str>,
    sleep: &'static str,
    /// Observable state of everything the refused list could have touched.
    state_sql: &'static str,
}

fn cases() -> Vec<Case> {
    let mut out = Vec::new();
    if let Some(endpoint) = endpoint("TF_QUERY_LIVE_MYSQL", "mysql") {
        out.push(Case {
            engine: "mysql",
            endpoint,
            setup: vec![
                "SET GLOBAL log_bin_trust_function_creators = 1",
                "DROP TABLE IF EXISTS ro_new", "DROP TABLE IF EXISTS t3", "DROP VIEW IF EXISTS v",
                "DROP DATABASE IF EXISTS ro_db", "DROP TABLE IF EXISTS t", "DROP TABLE IF EXISTS t2",
                "DROP PROCEDURE IF EXISTS p_ins", "DROP PROCEDURE IF EXISTS p_flip", "DROP FUNCTION IF EXISTS f_ins",
                "CREATE TABLE t (id INT PRIMARY KEY, v INT) ENGINE=InnoDB",
                "INSERT INTO t VALUES (1,1),(2,2)",
                "CREATE TABLE t2 (id INT)",
                "CREATE PROCEDURE p_ins() BEGIN INSERT INTO t VALUES (100,1); END",
                "CREATE PROCEDURE p_flip() BEGIN SET SESSION TRANSACTION READ WRITE; END",
                "CREATE FUNCTION f_ins() RETURNS INT MODIFIES SQL DATA BEGIN INSERT INTO t VALUES (101,1); RETURN 1; END",
            ],
            refused: vec![
                ("INSERT", "INSERT INTO t VALUES (3,3)"),
                ("UPDATE", "UPDATE t SET v = 9 WHERE id = 1"),
                ("DELETE", "DELETE FROM t WHERE id = 2"),
                ("REPLACE", "REPLACE INTO t VALUES (1, 77)"),
                ("TRUNCATE", "TRUNCATE TABLE t2"),
                ("CREATE TABLE", "CREATE TABLE ro_new (id INT)"),
                ("ALTER TABLE", "ALTER TABLE t ADD COLUMN w INT"),
                ("CREATE INDEX", "CREATE INDEX ix_v ON t (v)"),
                ("RENAME TABLE", "RENAME TABLE t2 TO t3"),
                ("DROP TABLE", "DROP TABLE t2"),
                ("CREATE DATABASE", "CREATE DATABASE ro_db"),
                ("CREATE VIEW", "CREATE VIEW v AS SELECT 1 AS x"),
                ("CREATE TEMPORARY TABLE", "CREATE TEMPORARY TABLE tmp1 (id INT)"),
                ("OPTIMIZE TABLE", "OPTIMIZE TABLE t"),
                ("CALL proc that inserts", "CALL p_ins()"),
                ("CALL proc that flips READ WRITE (core)", "CALL p_flip()"),
                ("function that inserts", "SELECT f_ins() AS x"),
                ("LOCK TABLES WRITE (core)", "LOCK TABLES t WRITE"),
                ("SET GLOBAL (core)", "SET GLOBAL max_connections = 77"),
                ("CREATE USER", "CREATE USER 'ro_probe'@'%' IDENTIFIED BY 'x'"),
                ("GRANT (core or server)", "GRANT SELECT ON tfdb.* TO 'root'@'localhost'"),
                ("SELECT INTO OUTFILE (core)", "SELECT * FROM t INTO OUTFILE '/tmp/ro_probe.txt'"),
                ("SET TRANSACTION READ WRITE (core)", "SET SESSION TRANSACTION READ WRITE"),
                ("START TRANSACTION READ WRITE (core)", "START TRANSACTION READ WRITE"),
                ("SET tx_read_only=0 (core)", "SET SESSION tx_read_only = 0"),
                ("multi-statement bypass (core)", "SELECT 1; SET SESSION TRANSACTION READ WRITE"),
            ],
            allowed: vec!["SELECT COUNT(*) AS c FROM t", "EXPLAIN SELECT * FROM t", "SHOW TABLES", "ANALYZE TABLE t"],
            sleep: "SELECT SLEEP(30) AS s",
            state_sql: "SELECT CONCAT((SELECT COUNT(*) FROM t), '/', (SELECT SUM(v) FROM t), '/', \
                (SELECT GROUP_CONCAT(table_name ORDER BY table_name) FROM information_schema.tables WHERE table_schema = DATABASE()), '/', \
                (SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'ro_db'), '/', \
                (SELECT COUNT(*) FROM mysql.user WHERE user = 'ro_probe'), '/', @@global.max_connections) AS state",
        });
    }
    if let Some(endpoint) = endpoint("TF_QUERY_LIVE_PG", "postgresql") {
        out.push(Case {
            engine: "postgresql",
            endpoint,
            setup: vec![
                "DROP TABLE IF EXISTS ro_new", "DROP TABLE IF EXISTS t3", "DROP VIEW IF EXISTS v", "DROP TABLE IF EXISTS t",
                "DROP TABLE IF EXISTS t2", "DROP SEQUENCE IF EXISTS sq", "DROP FUNCTION IF EXISTS f_ins()",
                "CREATE TABLE t (id INT PRIMARY KEY, v INT)", "INSERT INTO t VALUES (1,1),(2,2)",
                "CREATE TABLE t2 (id INT)", "CREATE SEQUENCE sq",
                "CREATE FUNCTION f_ins() RETURNS INT LANGUAGE sql AS 'INSERT INTO t VALUES (101,1) RETURNING 1'",
            ],
            refused: vec![
                ("INSERT", "INSERT INTO t VALUES (3,3)"),
                ("UPDATE", "UPDATE t SET v = 9 WHERE id = 1"),
                ("DELETE", "DELETE FROM t WHERE id = 2"),
                ("TRUNCATE", "TRUNCATE t2"),
                ("CREATE TABLE", "CREATE TABLE ro_new (id INT)"),
                ("ALTER TABLE", "ALTER TABLE t ADD COLUMN w INT"),
                ("CREATE INDEX", "CREATE INDEX ix_v ON t (v)"),
                ("ALTER TABLE RENAME", "ALTER TABLE t2 RENAME TO t3"),
                ("DROP TABLE", "DROP TABLE t2"),
                ("CREATE DATABASE", "CREATE DATABASE ro_db"),
                ("CREATE VIEW", "CREATE VIEW v AS SELECT 1 AS x"),
                ("CREATE TEMP TABLE", "CREATE TEMP TABLE tmp1 (id INT)"),
                ("nextval", "SELECT nextval('sq')"),
                ("setval", "SELECT setval('sq', 50)"),
                ("COMMENT ON", "COMMENT ON TABLE t IS 'x'"),
                ("DO block that inserts", "DO $$ BEGIN INSERT INTO t VALUES (100,1); END $$"),
                ("function that inserts", "SELECT f_ins()"),
                ("CREATE ROLE", "CREATE ROLE ro_probe"),
                ("VACUUM (core)", "VACUUM t"),
                ("REINDEX (core)", "REINDEX TABLE t"),
                ("lo_create (core)", "SELECT lo_create(0)"),
                ("ALTER SYSTEM (core)", "ALTER SYSTEM SET work_mem = '8MB'"),
                ("SET TRANSACTION READ WRITE (core)", "SET TRANSACTION READ WRITE"),
                ("SET CHARACTERISTICS READ WRITE (core)", "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE"),
                ("SET default_transaction_read_only (core)", "SET default_transaction_read_only = off"),
                ("set_config bypass (core)", "SELECT set_config('default_transaction_read_only','off',false)"),
                ("BEGIN READ WRITE (core)", "BEGIN READ WRITE"),
                ("RESET ALL (core)", "RESET ALL"),
                ("DISCARD ALL (core)", "DISCARD ALL"),
                ("multi-statement bypass (core)", "SELECT 1; SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE"),
            ],
            allowed: vec!["SELECT COUNT(*) AS c FROM t", "EXPLAIN SELECT * FROM t", "SHOW transaction_read_only", "ANALYZE t"],
            sleep: "SELECT pg_sleep(30) AS s",
            state_sql: "SELECT (SELECT COUNT(*) FROM t) || '/' || (SELECT SUM(v) FROM t) || '/' || \
                (SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public') || '/' || \
                (SELECT COUNT(*) FROM pg_roles WHERE rolname = 'ro_probe') || '/' || \
                (SELECT is_called::text FROM sq) || '/' || (SELECT COUNT(*) FROM pg_database WHERE datname = 'ro_db') || '/' || \
                (SELECT COUNT(*) FROM pg_largeobject_metadata) || '/' || current_setting('work_mem') AS state",
        });
    }
    out
}

fn skip_if_none(cases: &[Case]) -> bool {
    // The CI live gate sets TF_LIVE_REQUIRED: both engines must be configured there.
    if std::env::var_os("TF_LIVE_REQUIRED").is_some() {
        assert_eq!(cases.len(), 2, "TF_LIVE_REQUIRED is set but TF_QUERY_LIVE_MYSQL_* / TF_QUERY_LIVE_PG_* are incomplete");
    }
    if cases.is_empty() {
        eprintln!("skipped: TF_QUERY_LIVE_MYSQL_* / TF_QUERY_LIVE_PG_* not set");
    }
    cases.is_empty()
}

fn open_with(core: &mut Core, endpoint: &Value, read_only: bool) -> String {
    let mut payload = endpoint.clone();
    if read_only {
        payload["read_only"] = json!(true);
    }
    // Same shape as the Python facade: {"connection": {...endpoint, "read_only": true}}.
    let result = core.call("connection.open", json!({"connection": payload}));
    assert_eq!(result["success"], true, "{result}");
    result["connection_id"].as_str().unwrap().to_string()
}

#[test]
fn read_only_session_refuses_every_write_and_bypass_and_changes_nothing() {
    let cases = cases();
    if skip_if_none(&cases) {
        return;
    }
    for case in cases {
        let mut core = Core::start();
        let rw = open_with(&mut core, &case.endpoint, false);
        for sql in &case.setup {
            let result = core.query(&rw, sql);
            assert_eq!(result["success"], true, "{} setup {sql}: {result}", case.engine);
        }
        let before = core.scalar(&rw, case.state_sql);

        let ro = open_with(&mut core, &case.endpoint, true);
        for (label, sql) in &case.refused {
            let result = core.query(&ro, sql);
            assert_eq!(result["event"], "error", "{} {label} was not refused: {result}", case.engine);
            assert_eq!(result["error_code"], "read_only_session", "{} {label}: {result}", case.engine);
        }
        for sql in &case.allowed {
            let result = core.query(&ro, sql);
            assert_eq!(result["success"], true, "{} allowed {sql}: {result}", case.engine);
        }
        // Still read-only after the whole battery (no bypass left the mode switched off).
        let result = core.query(&ro, "INSERT INTO t VALUES (999, 9)");
        assert_eq!(result["error_code"], "read_only_session", "{} {result}", case.engine);
        assert_eq!(core.scalar(&rw, case.state_sql), before, "{} data/schema/settings changed", case.engine);

        // A manual transaction of a read-only session stays read-only and can still end normally.
        let begin = if case.engine == "mysql" { "START TRANSACTION" } else { "BEGIN" };
        assert_eq!(core.query(&ro, begin)["success"], true);
        let result = core.query(&ro, "UPDATE t SET v = 5");
        assert_eq!(result["error_code"], "read_only_session", "{result}");
        assert_eq!(core.query(&ro, "ROLLBACK")["success"], true);

        // Unlocking = a new read-write session; reopening read-only refuses again.
        let unlocked = open_with(&mut core, &case.endpoint, false);
        assert_eq!(core.query(&unlocked, "INSERT INTO t VALUES (500, 5)")["success"], true);
        assert_eq!(core.scalar(&rw, "SELECT COUNT(*) FROM t WHERE id = 500"), "1");
        let reopened = open_with(&mut core, &case.endpoint, true);
        assert_eq!(core.query(&reopened, "DELETE FROM t WHERE id = 500")["error_code"], "read_only_session");
        assert_eq!(core.scalar(&rw, "SELECT COUNT(*) FROM t WHERE id = 500"), "1");
        core.query(&rw, "DELETE FROM t WHERE id = 500");
        eprintln!("{}: {} refused statements, {} allowed, data unchanged, unlock/relock OK", case.engine, case.refused.len(), case.allowed.len());
    }
}

#[test]
fn read_only_session_keeps_cancel_streaming_and_export_working() {
    let cases = cases();
    if skip_if_none(&cases) {
        return;
    }
    for case in cases {
        let mut core = Core::start();
        let rw = open_with(&mut core, &case.endpoint, false);
        for sql in &case.setup {
            core.query(&rw, sql);
        }
        let ro = open_with(&mut core, &case.endpoint, true);

        // cancel a running query on the read-only session
        let id = core.send("query.execute", json!({"connection_id": ro, "sql": case.sleep, "job_id": "ro-1"}));
        std::thread::sleep(Duration::from_millis(700));
        core.call("query.cancel", json!({"job_id": "ro-1"}));
        let result = core.finish(&id, Duration::from_secs(15)).pop().unwrap();
        assert_eq!(result["error_code"], "query_cancelled", "{} {result}", case.engine);
        assert_eq!(core.scalar(&ro, "SELECT 1 AS one"), "1");

        // streaming + file export from the read-only session
        let events = {
            let id = core.send("query.execute", json!({"connection_id": ro, "sql": "SELECT id, v FROM t ORDER BY id", "stream_rows": true}));
            core.finish(&id, Duration::from_secs(30))
        };
        assert_eq!(events.last().unwrap()["success"], true);
        assert!(events.iter().any(|e| e["event"] == "row_batch"));
        let dir = std::env::temp_dir().join("tf_ro_live");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("ro_{}.csv", case.engine));
        let _ = std::fs::remove_file(&path);
        let id = core.send(
            "query.execute",
            json!({"connection_id": ro, "sql": "SELECT id, v FROM t ORDER BY id", "output": {"path": path.to_string_lossy()}}),
        );
        let result = core.finish(&id, Duration::from_secs(30)).pop().unwrap();
        assert_eq!(result["success"], true, "{} {result}", case.engine);
        assert_eq!(result["rows_written"], 2);
        let _ = std::fs::remove_file(&path);
    }
}

/// The core never reconnects a session: a killed read-only session fails (it is not silently
/// replaced by a writable one), and a fresh `connection.open` is read-only again. One-off endpoint
/// queries honour `read_only` too.
#[test]
fn killed_read_only_session_is_not_replaced_by_a_writable_one() {
    let cases = cases();
    if skip_if_none(&cases) {
        return;
    }
    for case in cases {
        let mut core = Core::start();
        let rw = open_with(&mut core, &case.endpoint, false);
        for sql in &case.setup {
            core.query(&rw, sql);
        }
        let before = core.scalar(&rw, "SELECT COUNT(*) FROM t");
        let ro = open_with(&mut core, &case.endpoint, true);
        let (id_sql, kill) = if case.engine == "mysql" {
            ("SELECT CONNECTION_ID() AS id", "KILL ")
        } else {
            ("SELECT pg_backend_pid() AS id", "SELECT pg_terminate_backend(")
        };
        let id = core.scalar(&ro, id_sql);
        let kill_sql = if case.engine == "mysql" { format!("{kill}{id}") } else { format!("{kill}{id})") };
        assert_eq!(core.query(&rw, &kill_sql)["success"], true);
        std::thread::sleep(Duration::from_millis(800));

        for sql in ["SELECT 1 AS one", "INSERT INTO t VALUES (777, 7)"] {
            let result = core.query(&ro, sql);
            assert_eq!(result["event"], "error", "{} killed session answered {sql}: {result}", case.engine);
        }
        assert_eq!(core.scalar(&rw, "SELECT COUNT(*) FROM t"), before, "{} write got through", case.engine);

        let reopened = open_with(&mut core, &case.endpoint, true);
        assert_eq!(core.query(&reopened, "INSERT INTO t VALUES (777, 7)")["error_code"], "read_only_session");

        // one-off endpoint query (no session): same policy
        let mut endpoint = case.endpoint.clone();
        endpoint["read_only"] = json!(true);
        let one_off = |core: &mut Core, sql: &str| core.call("query.execute", json!({"connection": endpoint, "sql": sql}));
        let result = one_off(&mut core, "INSERT INTO t VALUES (778, 7)");
        assert_eq!(result["error_code"], "read_only_session", "{} {result}", case.engine);
        let bypass = if case.engine == "mysql" { "SET SESSION TRANSACTION READ WRITE" } else { "SET default_transaction_read_only = off" };
        assert_eq!(one_off(&mut core, bypass)["error_code"], "read_only_session");
        assert_eq!(one_off(&mut core, "SELECT COUNT(*) AS c FROM t")["success"], true);
        assert_eq!(core.scalar(&rw, "SELECT COUNT(*) FROM t"), before);
        eprintln!("{}: killed session fails closed, reopen and one-off queries stay read-only", case.engine);
    }
}
