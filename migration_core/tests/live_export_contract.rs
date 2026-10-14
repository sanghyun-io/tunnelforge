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

fn dump_events(endpoint: &Endpoint, payload: Value) -> Vec<Value> {
    let mut payload = payload;
    payload["endpoint"] = serde_json::to_value(endpoint).unwrap();
    handle_request(Request { command: "dump.run".into(), request_id: None, payload })
}

fn refusal_objects(events: &[Value]) -> Vec<String> {
    let error = events.iter().find(|e| e["event"] == "error").unwrap_or_else(|| panic!("expected refusal: {events:#?}"));
    assert_eq!(error["error_code"], "unsupported_objects", "{error}");
    error["objects"].as_array().unwrap().iter().map(|o| o.as_str().unwrap().to_string()).collect()
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn unsupported_objects_are_refused_before_any_file_and_opt_in_proceeds() {
    for endpoint in endpoints() {
        let mysql = endpoint.engine == "mysql";
        let base = format!("tf_export_scope_{}", std::process::id());
        let (table, plain) = (format!("{base}_t"), format!("{base}_plain"));
        let (trigger, routine) = (format!("{base}_trg"), format!("{base}_fn"));
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        let cleanup = |admin: &mut LiveAdapter| {
            for sql in if mysql {
                vec![format!("DROP TRIGGER IF EXISTS {trigger}"), format!("DROP FUNCTION IF EXISTS {routine}"), format!("DROP EVENT IF EXISTS {base}_ev"), format!("DROP TABLE IF EXISTS {base}_mem"), format!("DROP TABLE IF EXISTS {table}"), format!("DROP TABLE IF EXISTS {plain}")]
            } else {
                vec![format!("DROP TABLE IF EXISTS {table} CASCADE"), format!("DROP TABLE IF EXISTS {plain} CASCADE"), format!("DROP FUNCTION IF EXISTS {routine}()"), format!("DROP MATERIALIZED VIEW IF EXISTS {base}_mv")]
            } { admin.execute_sql(&sql).unwrap(); }
        };
        cleanup(&mut admin);
        admin.execute_sql(&format!("CREATE TABLE {table} (id INT PRIMARY KEY, v INT)")).unwrap();
        admin.execute_sql(&format!("CREATE TABLE {plain} (id INT PRIMARY KEY, v INT)")).unwrap();
        admin.execute_sql(&format!("INSERT INTO {table} VALUES (1, 1), (2, 2)")).unwrap();
        admin.execute_sql(&format!("INSERT INTO {plain} VALUES (1, 10)")).unwrap();
        if mysql {
            admin.execute_sql(&format!("CREATE TRIGGER {trigger} BEFORE INSERT ON {table} FOR EACH ROW SET NEW.v = 1")).unwrap();
            admin.execute_sql(&format!("CREATE FUNCTION {routine}() RETURNS INT DETERMINISTIC RETURN 1")).unwrap();
            admin.execute_sql(&format!("CREATE EVENT {base}_ev ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1")).unwrap();
            admin.execute_sql(&format!("CREATE TABLE {base}_mem (id INT PRIMARY KEY) ENGINE=MEMORY")).unwrap();
        } else {
            admin.execute_sql(&format!("CREATE FUNCTION {routine}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.v := 1; RETURN NEW; END $$")).unwrap();
            admin.execute_sql(&format!("CREATE TRIGGER {trigger} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {routine}()")).unwrap();
            admin.execute_sql(&format!("CREATE MATERIALIZED VIEW {base}_mv AS SELECT * FROM {plain}")).unwrap();
        }
        let out = |name: &str| std::env::temp_dir().join(format!("{base}_{}_{name}", endpoint.engine));
        let single = if mysql { json!("single_connection") } else { json!("parallel_strict") };

        // Trigger on a selected table: refused before anything is written.
        let dir = out("refused");
        let events = dump_events(&endpoint, json!({"tables": [table], "output_dir": dir, "data_format": "jsonl", "mysql_snapshot_mode": single}));
        let objects = refusal_objects(&events);
        assert!(objects.contains(&format!("trigger:{table}:{trigger}")), "{objects:?}");
        assert!(!dir.exists(), "output must not be created before refusal");

        // Objects of unselected tables do not block a partial export.
        let dir = out("plain");
        let events = dump_events(&endpoint, json!({"tables": [plain], "output_dir": dir, "data_format": "jsonl", "mysql_snapshot_mode": single}));
        assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
        fs::remove_dir_all(&dir).unwrap();

        // Full export: schema-level objects are listed (never silently omitted).
        let dir = out("full");
        let events = dump_events(&endpoint, json!({"output_dir": dir, "data_format": "jsonl", "mysql_snapshot_mode": single}));
        let objects = refusal_objects(&events);
        assert!(objects.contains(&format!("routine:{routine}")), "{objects:?}");
        assert!(objects.contains(&format!("trigger:{table}:{trigger}")), "{objects:?}");
        if mysql {
            assert!(objects.contains(&format!("event:{base}_ev")), "{objects:?}");
            assert!(objects.contains(&format!("non_transactional_table:{base}_mem:MEMORY")), "{objects:?}");
        } else {
            assert!(objects.contains(&format!("materialized_view:{base}_mv")), "{objects:?}");
        }
        assert!(!dir.exists(), "output must not be created before refusal");

        // Explicit opt-in: proceeds, stays flagged as incomplete, and round-trips the table data.
        let dir = out("optin");
        let events = dump_events(&endpoint, json!({"tables": [table], "output_dir": dir, "data_format": "jsonl", "allow_incomplete": true, "mysql_snapshot_mode": single}));
        assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
        let manifest: Value = serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
        assert_eq!(manifest["strict_export"], false);
        assert!(manifest["manifest_warnings"].as_array().unwrap().iter().any(|w| w.as_str().unwrap().contains(&trigger)), "{manifest}");
        admin.execute_sql(&format!("DROP TRIGGER {trigger}{}", if mysql { String::new() } else { format!(" ON {table}") })).unwrap();
        run(&endpoint, "dump.import", json!({"tables": [table], "input_dir": dir, "mode": "replace", "threads": 1, "strict_manifest": true}));
        assert_eq!(admin.row_count(&table).unwrap(), 2);
        fs::remove_dir_all(&dir).unwrap();

        // Supported objects only: strict export succeeds without opt-in.
        if mysql {
            admin.execute_sql(&format!("DROP EVENT {base}_ev")).unwrap();
            admin.execute_sql(&format!("DROP TABLE {base}_mem")).unwrap();
            admin.execute_sql(&format!("DROP FUNCTION {routine}")).unwrap();
        } else {
            admin.execute_sql(&format!("DROP MATERIALIZED VIEW {base}_mv")).unwrap();
            admin.execute_sql(&format!("DROP FUNCTION {routine}()")).unwrap();
        }
        let dir = out("clean");
        let events = dump_events(&endpoint, json!({"output_dir": dir, "data_format": "jsonl", "mysql_snapshot_mode": single}));
        assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
        let manifest: Value = serde_json::from_slice(&fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap();
        assert_eq!(manifest["strict_export"], true, "{manifest}");
        fs::remove_dir_all(&dir).unwrap();
        cleanup(&mut admin);
    }
}

/// Drops the throwaway accounts/database/output even when an assertion fails.
struct MysqlThrowaway { admin: Endpoint, cleanup: Vec<String>, dirs: Vec<std::path::PathBuf> }

impl MysqlThrowaway {
    fn clean(&self) {
        if let Ok(mut admin) = LiveAdapter::connect(&self.admin) {
            for sql in &self.cleanup { let _ = admin.execute_sql(sql); }
        }
        for dir in &self.dirs { let _ = fs::remove_dir_all(dir); }
    }
}

impl Drop for MysqlThrowaway {
    fn drop(&mut self) { self.clean(); }
}

fn snapshot_worker_count(events: &[Value]) -> u64 {
    events.iter().find_map(|event| event["worker_count"].as_u64()).unwrap_or_else(|| panic!("{events:#?}"))
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn mysql_table_lock_snapshot_needs_only_lock_tables_and_unsynchronized_is_not_strict() {
    let Some(root) = endpoints().into_iter().find(|endpoint| endpoint.engine == "mysql") else { return; };
    let (db, locker, plain) = ("tf_export_table_lock", "tf_export_locker", "tf_export_nolock");
    let out = |name: &str| std::env::temp_dir().join(format!("{db}_{name}"));
    let guard = MysqlThrowaway {
        admin: root.clone(),
        cleanup: vec![format!("DROP USER IF EXISTS '{locker}'@'%'"), format!("DROP USER IF EXISTS '{plain}'@'%'"), format!("DROP DATABASE IF EXISTS {db}")],
        dirs: ["strict", "denied", "table_lock", "inflight", "unsynchronized"].map(out).to_vec(),
    };
    guard.clean();
    let mut admin = LiveAdapter::connect(&root).unwrap();
    for sql in [
        format!("CREATE DATABASE {db}"),
        format!("CREATE TABLE {db}.parent (id INT PRIMARY KEY, name VARCHAR(20))"),
        format!("CREATE TABLE {db}.child (id INT PRIMARY KEY, parent_id INT NOT NULL, FOREIGN KEY (parent_id) REFERENCES {db}.parent (id))"),
        format!("INSERT INTO {db}.parent VALUES (1, 'a'), (2, 'b'), (3, 'c')"),
        format!("INSERT INTO {db}.child VALUES (10, 1), (11, 1), (12, 2), (13, 3)"),
        // Minimum the dump needs plus LOCK TABLES; no RELOAD / BACKUP_ADMIN. TRIGGER, EVENT and
        // EXECUTE prove the account sees every object, so its full export stays strict (B1).
        format!("CREATE USER '{locker}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, LOCK TABLES, TRIGGER, EVENT, EXECUTE ON {db}.* TO '{locker}'@'%'"),
        format!("CREATE USER '{plain}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT ON {db}.* TO '{plain}'@'%'"),
    ] { admin.execute_sql(&sql).unwrap(); }
    let as_user = |user: &str| Endpoint { user: user.into(), database: db.into(), ..root.clone() };
    let in_db = Endpoint { database: db.into(), ..root.clone() };
    let rows = |table: &str| {
        let result = run(&in_db, "query.execute", json!({"sql": format!("SELECT * FROM {table} ORDER BY id")}));
        result["rows"].as_array().unwrap().iter().map(Value::to_string).collect::<Vec<_>>()
    };
    let expected = [rows("parent"), rows("child")];
    let error_of = |events: &[Value]| events.iter().find(|e| e["event"] == "error").map(|e| e["message"].as_str().unwrap().to_string()).unwrap_or_else(|| panic!("{events:#?}"));
    let payload = |mode: &str, dir: &str| json!({"output_dir": out(dir), "threads": 4, "chunk_size": 1, "data_format": "jsonl", "mysql_snapshot_mode": mode});

    // Strict needs global lock privileges the locker lacks; the backup lock is tried first.
    let events = dump_events(&as_user(locker), payload("parallel_strict", "strict"));
    assert!(error_of(&events).starts_with("MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:BACKUP_ADMIN:"), "{events:#?}");
    // Without LOCK TABLES the table-lock mode reports its own marker.
    let events = dump_events(&as_user(plain), payload("parallel_table_lock", "denied"));
    assert!(error_of(&events).starts_with("MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:LOCK_TABLES:"), "{events:#?}");
    assert!(!out("denied").join("_tunnelforge_dump.json").exists());

    // Table-lock mode: parallel shared snapshot, and no lock is held while the data is dumped
    // (a writer with a short lock wait would fail if the read lock were still held).
    let mut writer = LiveAdapter::connect(&in_db).unwrap();
    writer.execute_sql("SET SESSION lock_wait_timeout=5").unwrap();
    let mut inserted = false;
    let mut events = Vec::new();
    let mut request = payload("parallel_table_lock", "table_lock");
    request["endpoint"] = serde_json::to_value(as_user(locker)).unwrap();
    handle_request_streaming(Request { command: "dump.run".into(), request_id: None, payload: request }, |event| {
        if !inserted && event["event"] == "row_progress" {
            writer.execute_sql("INSERT INTO parent VALUES (4, 'later')").unwrap();
            inserted = true;
        }
        events.push(event);
    });
    assert!(inserted);
    let result = events.iter().find(|e| e["event"] == "result").unwrap_or_else(|| panic!("{events:#?}"));
    assert_eq!(result["snapshot_policy"], "mysql_table_lock_shared_snapshot");
    assert_eq!(result["strict_export"], true);
    assert_eq!(result["rows_dumped"], 7, "the later insert must stay outside the snapshot");
    assert_eq!(snapshot_worker_count(&events), 4);
    run(&in_db, "dump.import", json!({"input_dir": out("table_lock"), "mode": "replace", "threads": 2, "strict_manifest": true}));
    assert_eq!([rows("parent"), rows("child")], expected);

    // The read lock is what lines the workers up: an uncommitted write to an exported table
    // makes LOCK TABLES wait, and every snapshot opened after it commits includes its rows.
    let mut holder = LiveAdapter::connect(&in_db).unwrap();
    for sql in ["START TRANSACTION", "INSERT INTO parent VALUES (5, 'inflight')", "INSERT INTO child VALUES (14, 5)"] {
        holder.execute_sql(sql).unwrap();
    }
    let dump = {
        let (endpoint, request) = (as_user(locker), payload("parallel_table_lock", "inflight"));
        std::thread::spawn(move || dump_events(&endpoint, request))
    };
    let lock_waits = |_| {
        std::thread::sleep(std::time::Duration::from_millis(10));
        let result = run(&root, "query.execute", json!({"sql": "SELECT ID FROM performance_schema.processlist WHERE INFO LIKE 'LOCK TABLES%' AND STATE = 'Waiting for table metadata lock'"}));
        result["rows"].as_array().is_some_and(|rows| !rows.is_empty())
    };
    let waited = (0..150).any(lock_waits);
    holder.execute_sql("COMMIT").unwrap();
    let events = dump.join().unwrap();
    assert!(waited, "LOCK TABLES must wait for the in-flight writer: {events:#?}");
    let result = events.iter().find(|e| e["event"] == "result").unwrap_or_else(|| panic!("{events:#?}"));
    assert_eq!(result["rows_dumped"], 9, "the snapshot must include the writer's parent and child");

    // Explicit unsynchronized opt-in: parallel, no lock privilege, never strict.
    let events = dump_events(&as_user(plain), payload("parallel_unsynchronized", "unsynchronized"));
    let result = events.iter().find(|e| e["event"] == "result").unwrap_or_else(|| panic!("{events:#?}"));
    assert_eq!(result["snapshot_policy"], "mysql_parallel_unsynchronized_snapshots");
    assert_eq!(result["strict_export"], false);
    assert!(result["manifest_warnings"].as_array().unwrap().iter().any(|w| w.as_str().unwrap().contains("without a shared lock")), "{result}");
    assert_eq!(result["rows_dumped"], 9);
    assert_eq!(snapshot_worker_count(&events), 4);
}

// ---------------------------------------------------------------- automatic snapshot (C4) and visibility (C5)

fn mysql_root() -> Option<Endpoint> {
    endpoints().into_iter().find(|endpoint| endpoint.engine == "mysql")
}

/// A throwaway database `db` (3 parent and 4 child rows) plus `{db}_block`, with `users` created
/// before `setup` runs; the guard drops them all and the exports under `{temp}/{db}`.
fn auto_fixture(root: &Endpoint, db: &str, users: &[&str], setup: &[String]) -> MysqlThrowaway {
    let mut cleanup: Vec<String> = users.iter().map(|user| format!("DROP USER IF EXISTS '{user}'@'%'")).collect();
    cleanup.extend([format!("DROP DATABASE IF EXISTS {db}"), format!("DROP DATABASE IF EXISTS {db}_block")]);
    let guard = MysqlThrowaway { admin: root.clone(), cleanup, dirs: vec![std::env::temp_dir().join(db)] };
    guard.clean();
    let mut admin = LiveAdapter::connect(root).unwrap();
    let base = [
        format!("CREATE DATABASE {db}"),
        format!("CREATE DATABASE {db}_block"),
        format!("CREATE TABLE {db}_block.blocker (id INT PRIMARY KEY)"),
        format!("CREATE TABLE {db}.parent (id INT PRIMARY KEY, name VARCHAR(20))"),
        format!("CREATE TABLE {db}.child (id INT PRIMARY KEY, parent_id INT NOT NULL, FOREIGN KEY (parent_id) REFERENCES {db}.parent (id))"),
        format!("INSERT INTO {db}.parent VALUES (1, 'a'), (2, 'b'), (3, 'c')"),
        format!("INSERT INTO {db}.child VALUES (10, 1), (11, 1), (12, 2), (13, 3)"),
    ];
    let users = users.iter().map(|user| format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"));
    for sql in base.into_iter().chain(users).chain(setup.iter().cloned()) {
        admin.execute_sql(&sql).unwrap();
    }
    guard
}

/// The grants that prove every object of `db` visible (B1), so a full export stays strict.
fn sees_everything(user: &str, db: &str) -> String {
    format!("GRANT SELECT, TRIGGER, EVENT, EXECUTE ON {db}.* TO '{user}'@'%'")
}

fn user_in(root: &Endpoint, user: &str, db: &str) -> Endpoint {
    Endpoint { user: user.into(), database: db.into(), ..root.clone() }
}

fn auto_payload(db: &str, name: &str, extra: Value) -> Value {
    let mut payload = json!({"output_dir": std::env::temp_dir().join(db).join(name), "threads": 4,
        "chunk_size": 1, "data_format": "jsonl", "compression": "none", "mysql_snapshot_mode": "auto"});
    for (key, value) in extra.as_object().unwrap() {
        payload[key] = value.clone();
    }
    payload
}

fn result_of(events: &[Value]) -> Value {
    events.iter().find(|e| e["event"] == "result").cloned().unwrap_or_else(|| panic!("{events:#?}"))
}

/// dump.run into `{temp}/{db}/{name}`; panics unless it succeeds.
fn auto_export(endpoint: &Endpoint, db: &str, name: &str, extra: Value) -> (Vec<Value>, Value) {
    let events = dump_events(endpoint, auto_payload(db, name, extra));
    let result = result_of(&events);
    (events, result)
}

fn manifest_of(db: &str, name: &str) -> Value {
    serde_json::from_slice(&fs::read(std::env::temp_dir().join(db).join(name).join("_tunnelforge_dump.json")).unwrap()).unwrap()
}

/// The method that ran, the ones it skipped and its workers, as the result and the manifest say.
fn assert_resolved(result: &Value, manifest: &Value, mode: &str, policy: &str, fallbacks: Value, workers: u64) {
    assert_eq!(result["mysql_snapshot_mode"], mode, "{result}");
    assert_eq!(result["snapshot_policy"], policy, "{result}");
    assert_eq!(manifest["snapshot_policy"], policy, "the manifest records the method that ran");
    assert_eq!(result["snapshot_fallbacks"], fallbacks, "{result}");
    assert_eq!(result["worker_count"], workers, "{result}");
}

/// Whether a session of `user` holds LOCK INSTANCE FOR BACKUP right now.
fn holds_backup_lock(root: &Endpoint, user: &str) -> bool {
    let result = run(root, "query.execute", json!({"sql": format!(
        "SELECT m.OWNER_THREAD_ID FROM performance_schema.metadata_locks m JOIN performance_schema.threads t ON t.THREAD_ID = m.OWNER_THREAD_ID WHERE m.OBJECT_TYPE = 'BACKUP LOCK' AND t.PROCESSLIST_USER = '{user}'"
    )}));
    result["rows"].as_array().is_some_and(|rows| !rows.is_empty())
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn manifest_never_records_auto() {
    let Some(root) = mysql_root() else { return };
    let db = "tf_auto_strict";
    let _guard = auto_fixture(&root, db, &[], &[]);
    let (_, result) = auto_export(&Endpoint { database: db.into(), ..root.clone() }, db, "strict", json!({}));
    let manifest = manifest_of(db, "strict");
    assert_resolved(&result, &manifest, "parallel_strict", "mysql_shared_consistent_snapshot", json!([]), 4);
    assert_eq!(result["strict_export"], true, "{result}");
    assert!(!manifest.to_string().contains("\"auto\""), "{manifest}");
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_snapshot_falls_back_to_table_lock_without_global_privileges() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_table_lock", "tf_auto_locker");
    let _guard = auto_fixture(&root, db, &[user], &[sees_everything(user, db), format!("GRANT LOCK TABLES ON {db}.* TO '{user}'@'%'")]);
    let (_, result) = auto_export(&user_in(&root, user, db), db, "auto", json!({}));
    assert_resolved(&result, &manifest_of(db, "auto"), "parallel_table_lock", "mysql_table_lock_shared_snapshot",
        json!([{"mode": "parallel_strict", "reason": "privilege"}]), 4);
    assert_eq!(result["strict_export"], true, "{result}");
    assert_eq!(result["rows_dumped"], 7);
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_snapshot_falls_back_to_single_connection_without_lock_tables() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_single", "tf_auto_plain");
    let _guard = auto_fixture(&root, db, &[user], &[sees_everything(user, db)]);
    let (_, result) = auto_export(&user_in(&root, user, db), db, "auto", json!({}));
    assert_resolved(&result, &manifest_of(db, "auto"), "single_connection", "mysql_single_connection_consistent_snapshot",
        json!([{"mode": "parallel_strict", "reason": "privilege"}, {"mode": "parallel_table_lock", "reason": "privilege"}]), 1);
    assert_eq!(result["strict_export"], true, "one consistent transaction is a strict export: {result}");
    assert_eq!(result["rows_dumped"], 7);
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_to_single_connection_uses_one_worker() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_one_worker", "tf_auto_one");
    let _guard = auto_fixture(&root, db, &[user], &[sees_everything(user, db)]);
    let (events, result) = auto_export(&user_in(&root, user, db), db, "auto",
        json!({"threads": 8, "mysql_snapshot_fallback": "single_connection"}));
    assert_eq!(result["mysql_snapshot_mode"], "single_connection", "{result}");
    assert_eq!(result["worker_count"], 1, "{result}");
    assert_eq!(snapshot_worker_count(&events), 1, "the ready event names the resolved worker count");
    let schedule = events.iter().find(|e| e["event"] == "dump_schedule").unwrap_or_else(|| panic!("{events:#?}"));
    assert_eq!(schedule["threads"], 1, "{schedule}");
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_to_unsynchronized_writes_unsynchronized_policy_and_not_strict() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_unsync", "tf_auto_unsync");
    let _guard = auto_fixture(&root, db, &[user], &[sees_everything(user, db)]);
    let (_, result) = auto_export(&user_in(&root, user, db), db, "auto", json!({"mysql_snapshot_fallback": "parallel_unsynchronized"}));
    let manifest = manifest_of(db, "auto");
    assert_resolved(&result, &manifest, "parallel_unsynchronized", "mysql_parallel_unsynchronized_snapshots",
        json!([{"mode": "parallel_strict", "reason": "privilege"}, {"mode": "parallel_table_lock", "reason": "privilege"}]), 4);
    assert_eq!(result["strict_export"], false, "{result}");
    assert_eq!(manifest["strict_export"], false);
    assert!(result["manifest_warnings"].as_array().unwrap().iter().any(|w| w.as_str().unwrap().contains("without a shared lock")), "{result}");
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_snapshot_takes_backup_lock_before_ftwrl() {
    let Some(root) = mysql_root() else { return };
    let (db, reloader, backuper) = ("tf_auto_order", "tf_auto_reload", "tf_auto_backup");
    let mut setup = Vec::new();
    for (user, global) in [(reloader, "RELOAD"), (backuper, "BACKUP_ADMIN")] {
        setup.extend([sees_everything(user, db), format!("GRANT LOCK TABLES ON {db}.* TO '{user}'@'%'"), format!("GRANT {global} ON *.* TO '{user}'@'%'")]);
    }
    let _guard = auto_fixture(&root, db, &[reloader, backuper], &setup);
    // A write lock elsewhere on the server makes any FTWRL wait out its 2 seconds.
    let mut blocker = LiveAdapter::connect(&root).unwrap();
    blocker.execute_sql(&format!("LOCK TABLES {db}_block.blocker WRITE")).unwrap();

    // RELOAD-only (B13): the backup lock is refused before FTWRL could block the server, so the
    // skip is a privilege, not a lock timeout.
    let (_, result) = auto_export(&user_in(&root, reloader, db), db, "reload", json!({}));
    assert_resolved(&result, &manifest_of(db, "reload"), "parallel_table_lock", "mysql_table_lock_shared_snapshot",
        json!([{"mode": "parallel_strict", "reason": "privilege"}]), 4);
    blocker.execute_sql("UNLOCK TABLES").unwrap();

    // BACKUP_ADMIN-only: FTWRL is refused after the backup lock, which the table-lock method keeps.
    let mut held = false;
    let mut events = Vec::new();
    let mut request = auto_payload(db, "backup", json!({}));
    request["endpoint"] = serde_json::to_value(user_in(&root, backuper, db)).unwrap();
    handle_request_streaming(Request { command: "dump.run".into(), request_id: None, payload: request }, |event| {
        if !held && event["event"] == "row_progress" {
            held = holds_backup_lock(&root, backuper);
        }
        events.push(event);
    });
    let result = result_of(&events);
    assert_resolved(&result, &manifest_of(db, "backup"), "parallel_table_lock", "mysql_table_lock_shared_snapshot",
        json!([{"mode": "parallel_strict", "reason": "privilege"}]), 4);
    assert!(held, "the table-lock export keeps the backup lock it already had");
    assert!(!holds_backup_lock(&root, backuper), "the backup lock ends with the export");
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_snapshot_moves_on_when_the_global_lock_times_out() {
    // TF-STATUS-185: a busy server cannot give FTWRL in 2 seconds; the export takes the next
    // consistent method instead of failing.
    let Some(root) = mysql_root() else { return };
    let db = "tf_auto_busy";
    let _guard = auto_fixture(&root, db, &[], &[]);
    let mut blocker = LiveAdapter::connect(&root).unwrap();
    blocker.execute_sql(&format!("LOCK TABLES {db}_block.blocker WRITE")).unwrap();
    let mut held = false;
    let mut events = Vec::new();
    let mut request = auto_payload(db, "busy", json!({}));
    request["endpoint"] = serde_json::to_value(Endpoint { database: db.into(), ..root.clone() }).unwrap();
    handle_request_streaming(Request { command: "dump.run".into(), request_id: None, payload: request }, |event| {
        if !held && event["event"] == "row_progress" {
            held = holds_backup_lock(&root, &root.user);
        }
        events.push(event);
    });
    blocker.execute_sql("UNLOCK TABLES").unwrap();
    let result = result_of(&events);
    assert_resolved(&result, &manifest_of(db, "busy"), "parallel_table_lock", "mysql_table_lock_shared_snapshot",
        json!([{"mode": "parallel_strict", "reason": "lock_timeout"}]), 4);
    assert_eq!(result["strict_export"], true, "{result}");
    assert_eq!(result["rows_dumped"], 7);
    assert!(held, "the backup lock taken before FTWRL stays for the table-lock export");
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_snapshot_table_lock_timeout_falls_to_one_connection_never_unsynchronized() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_lock_wait", "tf_auto_waiter");
    let _guard = auto_fixture(&root, db, &[user], &[sees_everything(user, db), format!("GRANT LOCK TABLES ON {db}.* TO '{user}'@'%'")]);
    // An open write transaction makes LOCK TABLES ... READ wait out its 2 seconds.
    let mut holder = LiveAdapter::connect(&Endpoint { database: db.into(), ..root.clone() }).unwrap();
    for sql in ["START TRANSACTION", "INSERT INTO parent VALUES (9, 'open')"] {
        holder.execute_sql(sql).unwrap();
    }
    let (_, result) = auto_export(&user_in(&root, user, db), db, "auto", json!({}));
    holder.execute_sql("ROLLBACK").unwrap();
    assert_resolved(&result, &manifest_of(db, "auto"), "single_connection", "mysql_single_connection_consistent_snapshot",
        json!([{"mode": "parallel_strict", "reason": "privilege"}, {"mode": "parallel_table_lock", "reason": "lock_timeout"}]), 1);
    assert_eq!(result["strict_export"], true, "{result}");
    assert_eq!(result["rows_dumped"], 7, "the open transaction stays outside the snapshot");
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn too_many_connections_advances_chain() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_capped", "tf_auto_capped");
    // Two sessions: the shared-lock methods need a coordinator, its watchdog and a worker.
    let _guard = auto_fixture(&root, db, &[user], &[
        sees_everything(user, db),
        format!("GRANT LOCK TABLES ON {db}.* TO '{user}'@'%'"),
        format!("ALTER USER '{user}'@'%' WITH MAX_USER_CONNECTIONS 2"),
    ]);
    let (_, result) = auto_export(&user_in(&root, user, db), db, "auto", json!({}));
    assert_resolved(&result, &manifest_of(db, "auto"), "single_connection", "mysql_single_connection_consistent_snapshot",
        json!([{"mode": "parallel_strict", "reason": "connection_limit"}, {"mode": "parallel_table_lock", "reason": "connection_limit"}]), 1);
    assert_eq!(result["rows_dumped"], 7);
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn auto_snapshot_clamps_workers_to_max_user_connections() {
    let Some(root) = mysql_root() else { return };
    let (db, user) = ("tf_auto_clamped", "tf_auto_clamped");
    // Five sessions: the probe counts itself, the coordinator and its watchdog take two, three
    // are left. The strict method works first time, so no closing session races the limit.
    let _guard = auto_fixture(&root, db, &[user], &[
        sees_everything(user, db),
        format!("GRANT RELOAD, BACKUP_ADMIN ON *.* TO '{user}'@'%'"),
        format!("ALTER USER '{user}'@'%' WITH MAX_USER_CONNECTIONS 5"),
    ]);
    let (_, result) = auto_export(&user_in(&root, user, db), db, "auto", json!({"threads": 8}));
    assert_resolved(&result, &manifest_of(db, "auto"), "parallel_strict", "mysql_shared_consistent_snapshot", json!([]), 3);
    assert_eq!(result["rows_dumped"], 7);
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn export_without_trigger_privilege_is_not_strict() {
    let Some(root) = mysql_root() else { return };
    let (db, blind, seer) = ("tf_export_visibility", "tf_export_blind", "tf_export_seer");
    let _guard = auto_fixture(&root, db, &[blind, seer], &[
        format!("CREATE TRIGGER {db}.parent_trg BEFORE INSERT ON {db}.parent FOR EACH ROW SET NEW.name = UPPER(NEW.name)"),
        format!("GRANT SELECT, LOCK TABLES ON {db}.* TO '{blind}'@'%'"),
        sees_everything(seer, db),
    ]);
    let single = |tables: Value| json!({"tables": tables, "mysql_snapshot_mode": "single_connection"});
    let visibility = |result: &Value| result["manifest_warnings"].as_array().unwrap().iter()
        .filter_map(Value::as_str).find(|w| w.starts_with("The export account's privileges do not prove")).map(str::to_string);

    // The blind account cannot see the trigger, so nothing refuses; the dump must not claim completeness.
    let (_, result) = auto_export(&user_in(&root, blind, db), db, "blind_parent", single(json!(["parent"])));
    assert_eq!(result["strict_export"], false, "{result}");
    assert_eq!(manifest_of(db, "blind_parent")["strict_export"], false);
    assert!(visibility(&result).is_some_and(|w| w.contains("TRIGGER") && !w.contains("EVENT")), "{result}");
    // A full export also cannot prove routines and events.
    let (_, result) = auto_export(&user_in(&root, blind, db), db, "blind_full", single(Value::Null));
    assert!(visibility(&result).is_some_and(|w| w.contains("TRIGGER") && w.contains("routines") && w.contains("EVENT")), "{result}");
    assert_eq!(result["strict_export"], false);

    // The account that sees everything is refused for the trigger and strict without it.
    let events = dump_events(&user_in(&root, seer, db), auto_payload(db, "seer_parent", single(json!(["parent"]))));
    assert!(refusal_objects(&events).contains(&"trigger:parent:parent_trg".to_string()), "{events:#?}");
    let (_, result) = auto_export(&user_in(&root, seer, db), db, "seer_child", single(json!(["child"])));
    assert_eq!(result["strict_export"], true, "{result}");
    assert_eq!(visibility(&result), None);
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn export_warnings_exclude_out_of_scope_objects() {
    let Some(root) = mysql_root() else { return };
    let db = "tf_export_scoped";
    let _guard = auto_fixture(&root, db, &[], &[
        format!("CREATE TRIGGER {db}.parent_trg BEFORE INSERT ON {db}.parent FOR EACH ROW SET NEW.name = UPPER(NEW.name)"),
        format!("CREATE FUNCTION {db}.scoped_fn() RETURNS INT DETERMINISTIC RETURN 1"),
        format!("CREATE EVENT {db}.scoped_ev ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1"),
    ]);
    let in_db = Endpoint { database: db.into(), ..root.clone() };
    let warnings = |result: &Value| result["manifest_warnings"].as_array().unwrap().iter().map(|w| w.as_str().unwrap().to_string()).collect::<Vec<_>>();

    // The parent's trigger, the routine and the event are not part of a child-only dump.
    let (_, result) = auto_export(&in_db, db, "child", json!({"tables": ["child"]}));
    assert_eq!(warnings(&result), Vec::<String>::new());
    assert_eq!(result["strict_export"], true, "{result}");

    // Opting out of the parent's trigger warns about that trigger only.
    let (_, result) = auto_export(&in_db, db, "parent", json!({"tables": ["parent"], "allow_incomplete": true}));
    let warnings = warnings(&result);
    assert_eq!(result["strict_export"], false, "{result}");
    assert!(warnings.iter().any(|w| w.contains("trigger:parent:parent_trg")), "{warnings:?}");
    assert!(!warnings.iter().any(|w| w.contains("scoped_fn") || w.contains("scoped_ev")), "{warnings:?}");
}
