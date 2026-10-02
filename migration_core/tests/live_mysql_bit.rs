//! MySQL BIT values must survive dump -> import and cross-engine migration exactly (TF-STATUS-147).
//! The text protocol returns BIT as raw bytes; bytes >= 0x80 used to become U+FFFD in dumps.
//! Requires disposable MySQL and PostgreSQL (TF_MYSQL_* / TF_POSTGRES_*); skipped otherwise,
//! but TF_LIVE_REQUIRED turns a missing environment into a failure.
use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};

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

fn run(command: &str, payload: Value) -> Result<Value, String> {
    let events = handle_request(Request { command: command.into(), request_id: None, payload });
    if let Some(error) = events.iter().find(|event| event["event"] == "error") {
        return Err(error["message"].to_string());
    }
    events.into_iter().find(|event| event["event"] == "result").ok_or_else(|| "no result".into())
}

fn query(endpoint: &Endpoint, sql: &str) -> Vec<Value> {
    run("query.execute", json!({"connection": endpoint, "sql": sql})).unwrap()["rows"].as_array().cloned().unwrap_or_default()
}

/// BIN()/HEX() snapshot of every value, compared before and after each path.
fn snapshot(endpoint: &Endpoint, table: &str) -> Vec<Value> {
    query(endpoint, &format!("SELECT id, BIN(b1) AS b1, BIN(b8) AS b8, HEX(b64) AS b64 FROM {table} ORDER BY id"))
}

#[test]
fn mysql_bit_values_round_trip_exactly_when_configured() {
    let (Some(mysql), Some(postgres)) = (endpoint("TF_MYSQL", "mysql", 3306), endpoint("TF_POSTGRES", "postgresql", 5432)) else {
        assert!(std::env::var_os("TF_LIVE_REQUIRED").is_none(), "TF_LIVE_REQUIRED is set but TF_MYSQL_* / TF_POSTGRES_* are missing");
        eprintln!("skipping MySQL BIT regression: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };
    let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let table = format!("tf_bit_{suffix}");
    let keyed = format!("tf_bitkey_{suffix}");
    let mut db = LiveAdapter::connect(&mysql).unwrap();
    db.execute_sql(&format!("CREATE TABLE {table} (id INT PRIMARY KEY, b1 BIT(1), b8 BIT(8), b64 BIT(64))")).unwrap();
    db.execute_sql(&format!("INSERT INTO {table} VALUES (1, b'0', b'00000000', 0), (2, b'1', b'10000000', 9223372036854775808), \
        (3, b'1', b'11111111', 18446744073709551615), (4, NULL, b'01000001', 1), (5, b'0', NULL, NULL)")).unwrap();
    db.execute_sql(&format!("CREATE TABLE {keyed} (k BIT(8) PRIMARY KEY, v VARCHAR(8))")).unwrap();
    db.execute_sql(&format!("INSERT INTO {keyed} VALUES (b'00000001','a'),(b'01000001','b'),(b'01111111','c'),(b'10000000','d'),(b'11000011','e'),(b'11111110','f'),(b'11111111','g')")).unwrap();
    let expected = snapshot(&mysql, &table);
    let mut failures = Vec::new();

    for format in ["jsonl", "tsv"] {
        let dir = std::env::temp_dir().join(format!("tf-bit-{suffix}-{format}"));
        let dumped = run("dump.run", json!({"source": &mysql, "tables": [&table, &keyed], "output_dir": dir, "threads": 1,
            "chunk_size": 2, "data_format": format, "compression": "none"}));
        if let Err(error) = dumped {
            failures.push(format!("dump {format}: {error}"));
            continue;
        }
        let manifest: Value = serde_json::from_slice(&std::fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
        if manifest["format_version"] != 4 {
            failures.push(format!("dump {format}: format_version {} (BIT dumps must be marked 4)", manifest["format_version"]));
        }
        match run("dump.import", json!({"target": &mysql, "input_dir": dir, "mode": "replace", "threads": 1})) {
            Ok(_) => {
                let restored = snapshot(&mysql, &table);
                if restored != expected {
                    failures.push(format!("import {format}: {restored:?} != {expected:?}"));
                }
                let keys = query(&mysql, &format!("SELECT COUNT(*) AS n, COUNT(DISTINCT k) AS d FROM {keyed}"));
                if keys[0]["n"].to_string().trim_matches('"') != "7" || keys[0]["d"].to_string().trim_matches('"') != "7" {
                    failures.push(format!("import {format}: BIT-keyed rows {:?}", keys));
                }
            }
            Err(error) => failures.push(format!("import {format}: {error}")),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    for name in [&table, &keyed] {
        let columns = if name == &table {
            json!([{"name":"id","type":"int","nullable":false,"primary_key":true},{"name":"b1","type":"bit(1)","nullable":true},
                   {"name":"b8","type":"bit(8)","nullable":true},{"name":"b64","type":"bit(64)","nullable":true}])
        } else {
            json!([{"name":"k","type":"bit(8)","nullable":false,"primary_key":true},{"name":"v","type":"varchar(8)","nullable":true}])
        };
        let payload = json!({"source_engine":"mysql","target_engine":"postgresql","source":&mysql,"target":&postgres,
            "schema":{"tables":[{"name": name, "columns": columns}]},"execution_options":{"mode":"create_only","chunk_size":2}});
        match run("migrate", payload.clone()) {
            Ok(result) if result["success"] == true => {
                match run("verify", payload) {
                    Ok(result) if result["success"] == true => {}
                    other => failures.push(format!("verify {name}: {other:?}")),
                }
            }
            other => failures.push(format!("migrate {name}: {other:?}")),
        }
        let mut pg = LiveAdapter::connect(&postgres).unwrap();
        let _ = pg.execute_sql(&format!("DROP TABLE IF EXISTS {name}"));
    }
    db.execute_sql(&format!("DROP TABLE IF EXISTS {table}")).unwrap();
    db.execute_sql(&format!("DROP TABLE IF EXISTS {keyed}")).unwrap();
    assert!(failures.is_empty(), "MySQL BIT regressions:\n{}", failures.join("\n"));
}
