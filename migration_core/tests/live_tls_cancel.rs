//! Opt-in: query.cancel must still work when the connection is verify_full TLS
//! (PostgreSQL out-of-band cancel and the MySQL KILL QUERY side connection both reuse the TLS policy).
//!
//! Needs `scripts/tls_live_env.sh` (certs, up-pg, up-mysql, cert <target> good) and TF_TLS_TEST_CERT_DIR:
//!   TF_TLS_TEST_CERT_DIR=... cargo test --test live_tls_cancel -- --nocapture

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

struct Core {
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<Value>,
    buffer: Vec<Value>,
    seq: u64,
}

impl Core {
    fn start() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_tunnelforge-core"))
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
                if tx.send(serde_json::from_str(&line).unwrap()).is_err() {
                    return;
                }
            }
        });
        Self { child, stdin, rx, buffer: Vec::new(), seq: 0 }
    }

    fn send(&mut self, command: &str, payload: Value) -> String {
        self.seq += 1;
        let id = format!("t-{}", self.seq);
        writeln!(self.stdin, "{}", json!({"command": command, "request_id": id, "payload": payload})).unwrap();
        self.stdin.flush().unwrap();
        id
    }

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
            match self.rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
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

    fn scalar(&mut self, conn: &str, sql: &str) -> String {
        let result = self.call("query.execute", json!({"connection_id": conn, "sql": sql}));
        assert_eq!(result["success"], true, "{result}");
        match result["rows"][0].as_object().unwrap().values().next().unwrap() {
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

fn check(engine: &str, port: u16, user: &str, database: &str, sleep_sql: &str, still_running: &str) {
    let Ok(certs) = std::env::var("TF_TLS_TEST_CERT_DIR") else { return };
    let endpoint = json!({
        "engine": engine, "host": "127.0.0.1", "port": port, "user": user, "password": "tfpass",
        "database": database,
        "tls": {"mode": "verify_full", "ca_file": format!("{certs}/ca.pem")},
    });
    let mut core = Core::start();
    let open = |core: &mut Core| {
        let result = core.call("connection.open", endpoint.clone());
        assert_eq!(result["success"], true, "{engine}: {result}");
        result["connection_id"].as_str().unwrap().to_string()
    };
    let conn = open(&mut core);
    let observer = open(&mut core);
    let id = core.send(
        "query.execute",
        json!({"connection_id": conn, "sql": sleep_sql, "job_id": "tls-sleep", "stream_rows": true}),
    );
    std::thread::sleep(Duration::from_millis(1500));
    assert_eq!(core.scalar(&observer, still_running), "1", "{engine}: sleeper not running over TLS");

    let cancel = core.call("query.cancel", json!({"job_id": "tls-sleep"}));
    assert_eq!(cancel["cancelled"], true, "{cancel}");
    assert_eq!(cancel["server_cancel_sent"], true, "{engine}: {cancel}");
    let last = core.finish(&id, Duration::from_secs(15)).pop().unwrap();
    assert_eq!(last["error_code"], "query_cancelled", "{engine}: {last}");

    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(core.scalar(&observer, still_running), "0", "{engine}: server query survived cancel");
    assert_eq!(core.scalar(&conn, "SELECT 1 AS one"), "1", "{engine}: session unusable after cancel");
    eprintln!("PASS {engine}: verify_full TLS query cancelled on the server, session still usable");
}

#[test]
fn postgres_cancel_over_verify_full_tls() {
    check(
        "postgresql", 25432, "postgres", "postgres",
        "SELECT pg_sleep(30) AS tf_marker",
        "SELECT COUNT(*) FROM pg_stat_activity WHERE state = 'active' AND query LIKE '%pg_sleep(30)%' \
         AND query NOT LIKE '%pg_stat_activity%' AND pid <> pg_backend_pid()",
    );
}

#[test]
fn mysql_cancel_over_verify_full_tls() {
    check(
        "mysql", 23306, "root", "tfdb",
        "SELECT SLEEP(30) AS tf_marker",
        "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE INFO LIKE 'SELECT SLEEP(30)%' AND ID <> CONNECTION_ID()",
    );
}
