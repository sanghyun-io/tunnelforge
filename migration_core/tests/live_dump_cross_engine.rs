//! Dump files, rather than the separate migration command, must preserve the
//! supported portable scalar values across engines. Requires disposable DBs.
use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};

fn call(command: &str, payload: Value) -> Value {
    let events = handle_request(Request { command: command.into(), request_id: None, payload });
    assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    events.into_iter().find(|e| e["event"] == "result").unwrap()
}

fn endpoint(prefix: &str, engine: &str, port: u16) -> Option<Endpoint> {
    Some(Endpoint {
        engine: engine.into(), host: std::env::var(format!("{prefix}_HOST")).ok()?,
        port, user: std::env::var(format!("{prefix}_USER")).ok()?,
        password: std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),
        database: std::env::var(format!("{prefix}_DATABASE")).ok()?, schema: None,
    })
}

fn canonical_rows(endpoint: &Endpoint, table: &str) -> Value {
    let binary = if endpoint.engine == "mysql" { "LOWER(HEX(body))" } else { "encode(body,'hex')" };
    let amount = if endpoint.engine == "mysql" { "CAST(amount AS CHAR)" } else { "amount::text" };
    let stamp = if endpoint.engine == "mysql" { "CAST(happened AS CHAR)" } else { "happened::text" };
    call("query.execute", json!({"connection": endpoint,
        "sql": format!("SELECT CASE WHEN flag THEN 'yes' ELSE 'no' END AS flag, {amount} AS amount, {binary} AS body, note, {stamp} AS happened FROM {table} ORDER BY id")
    }))["rows"].clone()
}

#[test]
fn portable_scalar_dump_roundtrip_across_engines_when_configured() {
    let Some(mysql) = endpoint("TF_MYSQL", "mysql", 3306) else { return };
    let Some(pg) = endpoint("TF_POSTGRES", "postgresql", 5432) else { return };
    for (source, target) in [(&pg, &mysql), (&mysql, &pg)] {
        let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let table = format!("tf_cross_{suffix}");
        let mut source_db = LiveAdapter::connect(source).unwrap();
        let mut target_db = LiveAdapter::connect(target).unwrap();
        let (binary_type, binary, stamp_type) = if source.engine == "mysql" {
            ("VARBINARY(16)", "X'00ff5c09'", "DATETIME(6)")
        } else {
            ("BYTEA", "decode('00ff5c09','hex')", "TIMESTAMP(6)")
        };
        source_db.execute_sql(&format!("CREATE TABLE {table}(id BIGINT PRIMARY KEY, flag BOOLEAN, amount DECIMAL(24,6), body {binary_type}, note TEXT, happened {stamp_type})")).unwrap();
        source_db.execute_sql(&format!("INSERT INTO {table} VALUES (1,TRUE,123456789012345678.123456,{binary},'한글 😀','2026-09-28 01:02:03.123456'),(2,FALSE,-0.000001,NULL,'',NULL),(3,TRUE,NULL,{binary},NULL,'2000-01-01 00:00:00.000001')")).unwrap();
        let expected = canonical_rows(source, &table);
        assert_eq!(expected[0]["flag"], "yes");
        assert_eq!(expected[0]["amount"], "123456789012345678.123456");
        for format in ["jsonl", "tsv"] {
            let dir = std::env::temp_dir().join(format!("tf-cross-{suffix}-{format}"));
            let exported = call("dump.run", json!({"source": source, "tables": [table], "output_dir": dir,
                "data_format": format, "compression": "zstd", "chunk_size": 1, "threads": 1}));
            assert_eq!(exported["format_version"], 3, "older importers must reject metadata they cannot preserve");
            call("dump.import", json!({"target": target, "input_dir": dir, "mode": "replace", "threads": 1}));
            assert_eq!(canonical_rows(target, &table), expected, "{} -> {} {format}", source.engine, target.engine);
            target_db.execute_sql(&format!("DROP TABLE {table}")).unwrap();
            std::fs::remove_dir_all(dir).unwrap();
        }
        source_db.execute_sql(&format!("DROP TABLE {table}")).unwrap();
    }
}
