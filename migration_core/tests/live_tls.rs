//! Opt-in live TLS trust tests (TF-STATUS-110).
//!
//! Skipped unless `TF_TLS_TEST_CERT_DIR` is set. Servers and certificates come from
//! `scripts/tls_live_env.sh` (`certs`, `up-pg`, `up-mysql`); the tests hot-swap the
//! server certificate through that script, so they must not run concurrently.
//!
//!   TF_TLS_TEST_CERT_DIR=... TF_TLS_TEST_ENV_SCRIPT=scripts/tls_live_env.sh \
//!   TF_TLS_TEST_PG_PORT=25432 TF_TLS_TEST_MYSQL_PORT=23306 \
//!   cargo test --test live_tls -- --test-threads=1

use migration_core::{handle_request, Request};
use serde_json::{json, Value};
use std::process::Command;
use std::sync::Mutex;

static SERIAL: Mutex<()> = Mutex::new(());

struct Env {
    certs: String,
    script: String,
    pg_port: u16,
    mysql_port: u16,
}

/// The CI live gate sets TF_LIVE_REQUIRED so a missing environment fails instead
/// of passing silently.
fn require_env<T>(value: Option<T>, what: &str) -> Option<T> {
    if value.is_none() && std::env::var_os("TF_LIVE_REQUIRED").is_some() {
        panic!("TF_LIVE_REQUIRED is set but {what} is missing");
    }
    value
}

fn env() -> Option<Env> {
    let certs = std::env::var("TF_TLS_TEST_CERT_DIR").ok()?;
    Some(Env {
        certs,
        script: std::env::var("TF_TLS_TEST_ENV_SCRIPT").unwrap_or_else(|_| "scripts/tls_live_env.sh".into()),
        pg_port: std::env::var("TF_TLS_TEST_PG_PORT").ok().and_then(|v| v.parse().ok()).unwrap_or(25432),
        mysql_port: std::env::var("TF_TLS_TEST_MYSQL_PORT").ok().and_then(|v| v.parse().ok()).unwrap_or(23306),
    })
}

impl Env {
    fn swap(&self, target: &str, scenario: &str) {
        // On Windows plain `bash` may be WSL; point TF_TLS_TEST_BASH at Git Bash there.
        let bash = std::env::var("TF_TLS_TEST_BASH").unwrap_or_else(|_| "bash".into());
        let status = Command::new(bash)
            .args([&self.script, "cert", target, scenario])
            .env("TF_TLS_CERT_DIR", &self.certs)
            .status()
            .expect("bash available");
        assert!(status.success(), "cert swap {target}/{scenario} failed");
    }

    fn ca(&self) -> String {
        format!("{}/ca.pem", self.certs)
    }

    fn payload(&self, engine: &str, mode: &str, ca: bool, server_name: Option<&str>) -> Value {
        let (port, user, db) = if engine == "postgresql" {
            (self.pg_port, "postgres", "postgres")
        } else {
            (self.mysql_port, "root", "tfdb")
        };
        let mut tls = json!({ "mode": mode });
        if ca {
            tls["ca_file"] = json!(self.ca());
        }
        if let Some(name) = server_name {
            tls["server_name"] = json!(name);
        }
        json!({"engine": engine, "host": "127.0.0.1", "port": port, "user": user,
               "password": "tfpass", "database": db, "tls": tls})
    }
}

/// (success, error_code, message)
fn open(payload: Value) -> (bool, Option<String>, String) {
    let events = handle_request(Request { command: "connection.open".into(), request_id: None, payload });
    let result = events.iter().find(|e| e["event"] == "result").expect("result event");
    (
        result["success"] == true,
        result.get("error_code").and_then(Value::as_str).map(String::from),
        result["message"].as_str().unwrap_or("").to_string(),
    )
}

fn expect_ok(label: &str, r: (bool, Option<String>, String)) {
    assert!(r.0, "{label}: expected success, got code={:?} msg={}", r.1, r.2);
    println!("PASS {label}: connected");
}

fn expect_code(label: &str, r: (bool, Option<String>, String), code: &str) {
    assert!(!r.0, "{label}: expected failure");
    assert_eq!(r.1.as_deref(), Some(code), "{label}: wrong code, msg={}", r.2);
    println!("PASS {label}: blocked with {code}");
}

fn run_engine(env: &Env, engine: &str, target: &str) {
    // good certificate (SAN tf-db.test + 127.0.0.1)
    env.swap(target, "good");
    expect_ok(&format!("{engine} disable"), open(env.payload(engine, "disable", false, None)));
    expect_ok(&format!("{engine} verify_full ca+ip"), open(env.payload(engine, "verify_full", true, None)));
    expect_ok(&format!("{engine} verify_full server_name (tunnel style)"),
        open(env.payload(engine, "verify_full", true, Some("tf-db.test"))));
    expect_code(&format!("{engine} verify_full wrong server_name"),
        open(env.payload(engine, "verify_full", true, Some("other.test"))), "tls_verification_failed");
    expect_ok(&format!("{engine} verify_ca ignores wrong server_name"),
        open(env.payload(engine, "verify_ca", true, Some("other.test"))));
    expect_code(&format!("{engine} verify_full without trusted CA"),
        open(env.payload(engine, "verify_full", false, None)), "tls_verification_failed");

    // host-name mismatch: certificate only names other.test
    env.swap(target, "wrongname-only");
    expect_code(&format!("{engine} verify_full name mismatch"),
        open(env.payload(engine, "verify_full", true, None)), "tls_verification_failed");
    expect_ok(&format!("{engine} verify_ca allows name mismatch"),
        open(env.payload(engine, "verify_ca", true, None)));

    // expired certificate blocks both verify modes
    env.swap(target, if target == "mysql" { "expired-live" } else { "expired" });
    expect_code(&format!("{engine} verify_full expired"),
        open(env.payload(engine, "verify_full", true, None)), "tls_verification_failed");
    expect_code(&format!("{engine} verify_ca expired"),
        open(env.payload(engine, "verify_ca", true, None)), "tls_verification_failed");

    // certificate signed by a CA we do not trust
    env.swap(target, "untrusted");
    expect_code(&format!("{engine} verify_full untrusted CA"),
        open(env.payload(engine, "verify_full", true, None)), "tls_verification_failed");

    env.swap(target, "good");
}

#[test]
fn postgres_tls_policy_against_live_server() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let Some(env) = require_env(env(), "TF_TLS_TEST_CERT_DIR") else { return };
    run_engine(&env, "postgresql", "pg");

    // server without TLS: verified modes must fail closed, disable keeps working
    env.swap("pg", "none");
    expect_code("postgresql verify_full on non-TLS server",
        open(env.payload("postgresql", "verify_full", true, None)), "tls_unavailable");
    expect_ok("postgresql disable on non-TLS server", open(env.payload("postgresql", "disable", false, None)));
    env.swap("pg", "good");
}

#[test]
fn mysql_tls_policy_against_live_server() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let Some(env) = require_env(env(), "TF_TLS_TEST_CERT_DIR") else { return };
    run_engine(&env, "mysql", "mysql");
}

/// Needs the server restarted without TLS: `tls_live_env.sh up-mysql-nossl`.
#[test]
fn mysql_verified_tls_fails_closed_without_server_tls() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let Some(env) = require_env(env(), "TF_TLS_TEST_CERT_DIR") else { return };
    if std::env::var("TF_TLS_TEST_MYSQL_NOSSL").is_err() {
        return;
    }
    expect_code("mysql verify_full on non-TLS server",
        open(env.payload("mysql", "verify_full", true, None)), "tls_unavailable");
    expect_ok("mysql disable on non-TLS server", open(env.payload("mysql", "disable", false, None)));
}
