use migration_core::{handle_request, handle_request_streaming, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};
use std::fs;
use std::io::Read;

fn endpoints() -> Vec<Endpoint> {
    [("mysql", "TF_MYSQL_HOST", 3306, "root"), ("postgresql", "TF_POSTGRES_HOST", 5432, "postgres")]
        .into_iter().map(|(engine, host, port, user)| Endpoint {
            engine: engine.into(), host: std::env::var(host).expect("live database host required"),
            port, user: user.into(), password: "tf_local_test".into(), database: "tf_test".into(), schema: None,
            tls: Default::default(),
        }).filter(|endpoint| std::env::var("TF_EXPORT_ENGINE").map(|engine| engine == endpoint.engine).unwrap_or(true)).collect()
}

fn run(endpoint: &Endpoint, command: &str, mut payload: Value) -> Value {
    payload["endpoint"] = serde_json::to_value(endpoint).unwrap();
    let events = handle_request(Request { command: command.into(), request_id: None, payload });
    assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    events.into_iter().find(|e| e["event"] == "result").expect("result event")
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn nullable_unique_export_preserves_every_row() {
    for endpoint in endpoints() {
        let name = format!("tf_export_nullable_{}", std::process::id());
        let mut adapter = LiveAdapter::connect(&endpoint).unwrap();
        adapter.execute_sql(&format!("CREATE TABLE {name} (id INT UNIQUE, value VARCHAR(20))")).unwrap();
        adapter.execute_sql(&format!("INSERT INTO {name} VALUES (NULL, 'null-a'), (NULL, 'null-b'), (1, 'one'), (2, 'two')")).unwrap();
        for compression in ["none", "zstd"] {
            let dir = std::env::temp_dir().join(format!("{name}_{}_{compression}", endpoint.engine));
            run(&endpoint, "dump.run", json!({"tables": [name], "output_dir": dir,
                "chunk_size": 1, "data_format": "jsonl", "compression": compression,
                "mysql_snapshot_mode": "single_connection"}));
            let manifest: Value = serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
            let table = &manifest["tables"][0];
            let mut values = Vec::new();
            for chunk in table["chunk_sha256"].as_object().unwrap().keys() {
                let bytes = fs::read(dir.join(table["path"].as_str().unwrap()).join(chunk)).unwrap();
                let mut text = String::new();
                if compression == "zstd" {
                    zstd::stream::read::Decoder::new(bytes.as_slice()).unwrap().read_to_string(&mut text).unwrap();
                } else { text = String::from_utf8(bytes).unwrap(); }
                for line in text.lines() {
                    values.push(serde_json::from_str::<Value>(line).unwrap()["value"].as_str().unwrap().to_string());
                }
            }
            values.sort();
            assert_eq!(values, ["null-a", "null-b", "one", "two"], "{}", endpoint.engine);
            fs::remove_dir_all(dir).unwrap();
        }
        adapter.execute_sql(&format!("DROP TABLE {name}")).unwrap();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn export_import_format_matrix_preserves_edge_values() {
    for endpoint in endpoints() {
        let prefix = format!("tf_export_matrix_{}", std::process::id());
        let mut adapter = LiveAdapter::connect(&endpoint).unwrap();
        let tables: Vec<_> = ["typed", "heap", "empty", "json_heap"].iter().map(|suffix| format!("{prefix}_{suffix}")).collect();
        let (blob, bytes, stamp, enum_type) = if endpoint.engine == "mysql" {
            ("BLOB", "X'00ff5c09'", "TIMESTAMP(6)", "ENUM('MixedCase','other')")
        } else { ("BYTEA", "decode('00ff5c09', 'hex')", "TIMESTAMPTZ(6)", "TEXT") };
        adapter.execute_sql(if endpoint.engine == "mysql" { "SET SESSION time_zone='+00:00'" } else { "SET TIME ZONE 'UTC'" }).unwrap();
        adapter.execute_sql(&format!("CREATE TABLE {} (id INT PRIMARY KEY, body {blob}, moment {stamp}, document JSON, status {enum_type}, amount DECIMAL(30,10))", tables[0])).unwrap();
        adapter.execute_sql(&format!("INSERT INTO {} VALUES (1, {bytes}, '2026-09-28 01:02:03.123456', '{{\"name\":\"한글\",\"null\":null}}', 'MixedCase', 12345678901234567890.1234567890), (2, NULL, NULL, NULL, NULL, NULL)", tables[0])).unwrap();
        let longtext = if endpoint.engine == "mysql" { "LONGTEXT" } else { "TEXT" };
        adapter.execute_sql(&format!("CREATE TABLE {} (value {longtext})", tables[1])).unwrap();
        adapter.execute_sql(&format!("INSERT INTO {} VALUES (''), (NULL), ('line\nnext\ttab'), (REPEAT('x', 300000))", tables[1])).unwrap();
        adapter.execute_sql(&format!("CREATE TABLE {} (id INT PRIMARY KEY)", tables[2])).unwrap();
        adapter.execute_sql(&format!("CREATE TABLE {} (document JSON)", tables[3])).unwrap();
        adapter.execute_sql(&format!("INSERT INTO {} VALUES ('{{\"x\":1}}'), ('{{\"x\":1}}'), (NULL)", tables[3])).unwrap();
        let expected: Vec<_> = tables.iter().map(|table| {
            let result = run(&endpoint, "query.execute", json!({"sql": format!("SELECT * FROM {table}")}));
            let mut rows: Vec<_> = result["rows"].as_array().unwrap().iter().map(|row| row.to_string()).collect();
            rows.sort(); rows
        }).collect();
        for format in ["jsonl", "tsv"] {
            for compression in ["none", "zstd"] {
                let dir = std::env::temp_dir().join(format!("{prefix}_{}_{format}_{compression}", endpoint.engine));
                let exported = run(&endpoint, "dump.run", json!({"tables": tables, "output_dir": dir,
                    "threads": 8, "chunk_size": 1, "data_format": format, "compression": compression,
                    "mysql_snapshot_mode": "parallel_no_backup_lock"}));
                assert_eq!(exported["rows_dumped"], 9);
                let manifest: Value = serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
                assert_eq!(manifest["source_timezone"], "UTC");
                assert_eq!(manifest["source_schema"], if endpoint.engine == "mysql" { "tf_test" } else { "public" });
                run(&endpoint, "dump.import", json!({"tables": tables, "input_dir": dir, "mode": "replace", "threads": 2, "strict_manifest": true}));
                for (table, expected) in tables.iter().zip(&expected) {
                    let result = run(&endpoint, "query.execute", json!({"sql": format!("SELECT * FROM {table}")}));
                    let mut rows: Vec<_> = result["rows"].as_array().unwrap().iter().map(|row| row.to_string()).collect();
                    rows.sort();
                    assert!(&rows == expected, "row values differ: {} {table} {format} {compression}", endpoint.engine);
                }
                fs::remove_dir_all(dir).unwrap();
            }
        }
        for table in tables { adapter.execute_sql(&format!("DROP TABLE {table}")).unwrap(); }
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn limited_reader_exports_without_locks_and_uses_utc() {
    for endpoint in endpoints() {
        let name = format!("tf_export_reader_{}", std::process::id());
        let table = format!("{name}_table");
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        if endpoint.engine == "mysql" {
            admin.execute_sql("SET SESSION time_zone='+00:00'").unwrap();
            admin.execute_sql(&format!("CREATE TABLE {table} (id INT PRIMARY KEY, moment TIMESTAMP(6))")).unwrap();
            admin.execute_sql(&format!("INSERT INTO {table} VALUES (1, '2026-09-28 01:02:03.123456')")).unwrap();
            admin.execute_sql(&format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
            admin.execute_sql(&format!("GRANT SELECT ON tf_test.{table} TO '{name}'@'%'")).unwrap();
        } else {
            admin.execute_sql(&format!("CREATE TABLE {table} (id INT PRIMARY KEY, moment TIMESTAMPTZ(6)); INSERT INTO {table} VALUES (1, '2026-09-28 01:02:03.123456+00'); CREATE ROLE {name} LOGIN PASSWORD 'tf_local_test'; ALTER ROLE {name} SET timezone='Asia/Seoul'; GRANT SELECT ON {table} TO {name}")).unwrap();
        }
        let reader = Endpoint { user: name.clone(), ..endpoint.clone() };
        let dir = std::env::temp_dir().join(format!("{name}_{}", endpoint.engine));
        let mut payload = json!({"endpoint": reader, "tables": [table], "output_dir": dir,
            "data_format": "jsonl", "compression": "none", "threads": 8});
        if endpoint.engine == "mysql" {
            let events = handle_request(Request { command: "dump.run".into(), request_id: None, payload: payload.clone() });
            assert!(events.iter().any(|event| event["event"] == "error" && event["message"].as_str().unwrap_or_default().contains("MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED")), "{events:#?}");
            assert!(!dir.join("_tunnelforge_dump.json").exists());
            payload["mysql_snapshot_mode"] = json!("parallel_no_backup_lock");
        }
        let result = run(&reader, "dump.run", payload);
        assert_eq!(result["rows_dumped"], 1);
        let manifest: Value = serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
        assert_eq!(manifest["source_timezone"], "UTC");
        let table_manifest = &manifest["tables"][0];
        let chunk = table_manifest["chunk_sha256"].as_object().unwrap().keys().next().unwrap();
        let text = fs::read_to_string(dir.join(table_manifest["path"].as_str().unwrap()).join(chunk)).unwrap();
        assert!(text.contains("01:02:03.123456"), "non-UTC exported timestamp: {text}");
        if endpoint.engine == "mysql" {
            admin.execute_sql(&format!("DROP TABLE {table}")).unwrap();
            admin.execute_sql(&format!("DROP USER '{name}'@'%'")).unwrap();
        } else {
            admin.execute_sql(&format!("DROP TABLE {table}; DROP ROLE {name}")).unwrap();
        }
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn lossy_selected_schema_is_rejected_before_success_manifest() {
    for endpoint in endpoints() {
        let name = format!("tf_export_generated_{}", std::process::id());
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        admin.execute_sql(&format!("CREATE TABLE {name} (id INT PRIMARY KEY, derived INT GENERATED ALWAYS AS (id + 1) STORED)")).unwrap();
        let dir = std::env::temp_dir().join(format!("{name}_{}", endpoint.engine));
        let events = handle_request(Request { command: "dump.run".into(), request_id: None,
            payload: json!({"endpoint": endpoint, "tables": [name], "output_dir": dir}) });
        assert!(events.iter().any(|event| event["event"] == "error" && event["message"].as_str().unwrap_or_default().contains("generated_column")), "{events:#?}");
        assert!(!dir.join("_tunnelforge_dump.json").exists());
        admin.execute_sql(&format!("DROP TABLE {name}")).unwrap();
        if dir.exists() { fs::remove_dir_all(dir).unwrap(); }
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn mysql_concurrent_insert_preserves_snapshot_without_allocator_drift_error() {
    let Some(endpoint) = endpoints().into_iter().find(|endpoint| endpoint.engine == "mysql") else { return; };
    let name = format!("tf_export_allocator_{}", std::process::id());
    let mut writer = LiveAdapter::connect(&endpoint).unwrap();
    writer.execute_sql(&format!("CREATE TABLE {name} (id BIGINT PRIMARY KEY AUTO_INCREMENT, note VARCHAR(20)) AUTO_INCREMENT=10000")).unwrap();
    writer.execute_sql(&format!("INSERT INTO {name} (note) VALUES ('first'), ('second'), ('third')")).unwrap();
    let dir = std::env::temp_dir().join(&name);
    let mut inserted = false;
    let mut events = Vec::new();
    handle_request_streaming(Request { command: "dump.run".into(), request_id: None,
        payload: json!({"endpoint": endpoint, "tables": [name], "output_dir": dir,
            "chunk_size": 1, "threads": 1, "mysql_snapshot_mode": "single_connection",
            "data_format": "jsonl", "compression": "none"}) }, |event| {
        if !inserted && event["event"] == "row_progress" {
            writer.execute_sql(&format!("INSERT INTO {name} (note) VALUES ('later')")).unwrap();
            inserted = true;
        }
        events.push(event);
    });
    assert!(inserted);
    assert!(!events.iter().any(|event| event["event"] == "error"), "{events:#?}");
    let manifest: Value = serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
    let captured_counter = manifest["schema"]["tables"][0]["auto_increment"].as_u64().expect("allocator must be captured");
    assert_eq!(captured_counter, 10003, "capture must retain the initial inspection value");
    assert_eq!(writer.row_count(&name).unwrap(), 4, "concurrent INSERT must commit outside the snapshot");
    let table = &manifest["tables"][0];
    assert_eq!(table["rows"], 3);
    let mut values = Vec::new();
    for chunk in table["chunk_sha256"].as_object().unwrap().keys() {
        let text = fs::read_to_string(dir.join(table["path"].as_str().unwrap()).join(chunk)).unwrap();
        for line in text.lines() {
            values.push(serde_json::from_str::<Value>(line).unwrap()["note"].as_str().unwrap().to_string());
        }
    }
    values.sort();
    assert_eq!(values, ["first", "second", "third"]);
    writer.execute_sql(&format!("DROP TABLE {name}")).unwrap();
    fs::remove_dir_all(dir).unwrap();
}
