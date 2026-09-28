use migration_core::{handle_request, Endpoint, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::sync::Mutex;

static MYSQL_GLOBAL_LOCK: Mutex<()> = Mutex::new(());

fn endpoint() -> Option<Endpoint> {
    Some(Endpoint {
        engine: "mysql".into(), host: std::env::var("TF_MYSQL_HOST").ok()?, port: 3306,
        user: "root".into(), password: "tf_local_test".into(), database: "tf_test".into(), schema: None,
    })
}

fn connect(endpoint: &Endpoint) -> mysql::PooledConn {
    mysql::Pool::new(mysql::OptsBuilder::new().ip_or_hostname(Some(&endpoint.host))
        .user(Some(&endpoint.user)).pass(Some(&endpoint.password)).db_name(Some(&endpoint.database)))
        .unwrap().get_conn().unwrap()
}

struct GlobalSettings {
    conn: mysql::PooledConn,
    local_infile: u64,
}
impl Drop for GlobalSettings {
    fn drop(&mut self) {
        let _ = self.conn.query_drop(format!("SET GLOBAL local_infile={}", self.local_infile));
    }
}

fn run(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request { command: command.into(), request_id: None, payload })
}

fn success(events: &[Value]) -> bool {
    events.iter().any(|event| event["event"] == "result" && event["success"] == true)
        && !events.iter().any(|event| event["event"] == "error")
}

fn unique(prefix: &str) -> String {
    format!("{prefix}_{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos())
}

#[test]
fn mysql_import_preserves_zero_auto_increment_values() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    let mut failures = Vec::new();
    for legacy_enum in [false, true] {
        for (format, local_infile, threads) in [("jsonl", 0, 1), ("tsv", 0, 1), ("tsv", 1, 1), ("tsv", 1, 2)] {
            global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
            let table = unique("tf_zero_identity");
            global.conn.query_drop(format!("CREATE TABLE {table}(id INT AUTO_INCREMENT PRIMARY KEY, choice ENUM('alpha','beta') NOT NULL) ENGINE=InnoDB")).unwrap();
            global.conn.query_drop("SET SESSION sql_mode='NO_AUTO_VALUE_ON_ZERO'").unwrap();
            let choice = if legacy_enum { "''" } else { "'alpha'" };
            global.conn.query_drop(format!("INSERT INTO {table} VALUES (0,{choice}),(5,'beta'),(11,'alpha')")).unwrap();
            global.conn.query_drop("SET SESSION sql_mode=DEFAULT").unwrap();
            let expected: Vec<(u64,String,u64)> = global.conn.query(format!("SELECT id,choice,choice+0 FROM {table} ORDER BY id")).unwrap();
            assert_eq!(expected[0].0, 0);
            let output = std::env::temp_dir().join(unique("tf_zero_identity"));
            let exported = run("dump.run", json!({"source": endpoint, "tables": [table], "output_dir": output, "data_format": format, "compression": "none", "chunk_size": 1, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
            assert!(success(&exported), "{exported:?}");
            let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": "replace", "threads": threads}));
            let rows: Vec<(u64,String,u64)> = global.conn.query(format!("SELECT id,choice,choice+0 FROM {table} ORDER BY id")).unwrap();
            if !success(&imported) || rows != expected {
                failures.push(format!("{format} local_infile={local_infile} threads={threads} legacy_enum={legacy_enum}: success={} rows={rows:?}", success(&imported)));
            }
            global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
            std::fs::remove_dir_all(output).unwrap();
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn mysql_import_rejects_silent_value_coercion() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    let mut failures = Vec::new();
    for (source_type, target_type, value, sentinel) in [
        ("VARCHAR(20)", "VARCHAR(3)", "'long value'", "'old'"),
        ("DECIMAL(8,3)", "DECIMAL(8,1)", "12.345", "0"),
        ("INT", "TINYINT", "1000", "0"),
        ("ENUM('A','B')", "ENUM('A')", "'B'", "'A'"),
    ] {
    for (format, local_infile, threads) in [("jsonl", 0, 1), ("tsv", 0, 1), ("tsv", 1, 1), ("tsv", 1, 2)] {
        global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
        let table = unique("tf_coercion");
        global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, value {source_type}) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {table} VALUES (1,{value}),(2,{value})")).unwrap();
        let output = std::env::temp_dir().join(unique("tf_coercion"));
        let exported = run("dump.run", json!({"source": endpoint, "tables": [table], "output_dir": output, "data_format": format, "compression": "none", "chunk_size": 1, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
        assert!(success(&exported), "{exported:?}");
        global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
        global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, value {target_type}) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {table} VALUES (99,{sentinel})")).unwrap();
        let expected: Vec<(u64, String)> = global.conn.query(format!("SELECT id,CAST(value AS CHAR) FROM {table} ORDER BY id")).unwrap();
        let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": "merge", "threads": threads}));
        let rows: Vec<(u64, String)> = global.conn.query(format!("SELECT id,CAST(value AS CHAR) FROM {table} ORDER BY id")).unwrap();
        if success(&imported) || rows != expected {
            failures.push(format!("{source_type}->{target_type} {format} local_infile={local_infile} threads={threads}: success={} rows={rows:?}", success(&imported)));
        }
        global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
        std::fs::remove_dir_all(output).unwrap();
    }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn mysql_import_preserves_legacy_enum_zero_storage_values() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    let mut failures = Vec::new();
    for (format, local_infile, threads, zero_count, chunk_size) in [
        ("jsonl", 0, 1, 130, 70), ("tsv", 0, 1, 130, 70),
        ("tsv", 1, 1, 130, 70), ("tsv", 1, 2, 130, 70),
        ("jsonl", 0, 1, 65536, 70000), ("tsv", 1, 1, 65536, 70000),
    ] {
        global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
        let table = unique("tf_legacy_enum");
        global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, value ENUM('alpha','beta') NOT NULL) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop("SET SESSION sql_mode=''").unwrap();
        let zero_rows = (1..=zero_count).map(|id| format!("({id},'')")).collect::<Vec<_>>().join(",");
        global.conn.query_drop(format!("INSERT INTO {table} VALUES {zero_rows},({},'beta')", zero_count + 1)).unwrap();
        global.conn.query_drop("SET SESSION sql_mode=DEFAULT").unwrap();
        let expected: Vec<(u64,String,u64)> = global.conn.query(format!("SELECT id,value,value+0 FROM {table} ORDER BY id")).unwrap();
        let output = std::env::temp_dir().join(unique("tf_legacy_enum"));
        let exported = run("dump.run", json!({"source": endpoint, "tables": [table], "output_dir": output, "data_format": format, "compression": "zstd", "chunk_size": chunk_size, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
        assert!(success(&exported), "{exported:?}");
        let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": "replace", "threads": threads}));
        let rows: Vec<(u64,String,u64)> = global.conn.query(format!("SELECT id,value,value+0 FROM {table} ORDER BY id")).unwrap();
        if !success(&imported) || rows != expected {
            failures.push(format!("{format} local_infile={local_infile} threads={threads}: success={} row_count={}", success(&imported), rows.len()));
        }
        global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
        std::fs::remove_dir_all(output).unwrap();
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn mysql_legacy_enum_compatibility_never_allows_unrelated_coercion() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    for (format, local_infile) in [("jsonl", 0), ("tsv", 0), ("tsv", 1)] {
        global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
        let table = unique("tf_enum_mixed");
        global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, choice ENUM('alpha','beta') NOT NULL, amount DECIMAL(8,3)) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop("SET SESSION sql_mode=''").unwrap();
        global.conn.query_drop(format!("INSERT INTO {table} VALUES(1,'',12.345),(2,'beta',10.123)")).unwrap();
        global.conn.query_drop("SET SESSION sql_mode=DEFAULT").unwrap();
        let output = std::env::temp_dir().join(unique("tf_enum_mixed"));
        let exported = run("dump.run", json!({"source": endpoint, "tables": [table], "output_dir": output, "data_format": format, "compression": "none", "chunk_size": 100, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
        assert!(success(&exported), "{exported:?}");
        global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
        global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, choice ENUM('alpha','beta') NOT NULL, amount DECIMAL(8,1)) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {table} VALUES(99,'alpha',1.0)")).unwrap();
        let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": "merge", "threads": 1}));
        let ids: Vec<u64> = global.conn.query(format!("SELECT id FROM {table} ORDER BY id")).unwrap();
        global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
        std::fs::remove_dir_all(output).unwrap();
        assert!(!success(&imported), "{format}: unrelated decimal truncation was accepted");
        assert_eq!(ids, [99], "{format}: mixed warning statement was not rolled back");
    }
}

#[test]
fn mysql_import_modes_preserve_values_across_load_and_insert_paths() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    for mode in ["replace", "recreate", "merge"] {
        for (format, local_infile, threads) in [("jsonl", 0, 1), ("tsv", 0, 1), ("tsv", 1, 1), ("tsv", 1, 2)] {
            global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
            let table = unique("tf_modes");
            global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, value VARCHAR(100)) ENGINE=InnoDB")).unwrap();
            global.conn.query_drop(format!("INSERT INTO {table} VALUES (1,'한글 😀'),(2,'')")).unwrap();
            let output = std::env::temp_dir().join(unique("tf_modes"));
            let exported = run("dump.run", json!({"source": endpoint, "tables": [table], "output_dir": output, "data_format": format, "compression": "zstd", "chunk_size": 1, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
            assert!(success(&exported), "{exported:?}");
            global.conn.query_drop(format!("DELETE FROM {table}")).unwrap();
            global.conn.query_drop(format!("INSERT INTO {table} VALUES (99,'old')")).unwrap();
            let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": mode, "threads": threads}));
            assert!(success(&imported), "{mode} {format}: {imported:?}");
            let rows: Vec<(u64, String)> = global.conn.query(format!("SELECT id,value FROM {table} ORDER BY id")).unwrap();
            let mut expected = vec![(1, "한글 😀".into()), (2, "".into())];
            if mode == "merge" { expected.push((99, "old".into())); }
            assert_eq!(rows, expected, "{mode} {format} local_infile={local_infile} threads={threads}");
            global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
            std::fs::remove_dir_all(output).unwrap();
        }
    }
}

#[test]
fn mysql_merge_enforces_existing_foreign_keys() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    let mut failures = Vec::new();
    for (format, local_infile, threads) in [("jsonl", 0, 1), ("tsv", 0, 1), ("tsv", 1, 1), ("tsv", 1, 2)] {
        global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
        let parent = unique("tf_merge_parent");
        let child = unique("tf_merge_child");
        global.conn.query_drop(format!("CREATE TABLE {parent}(id INT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id INT, FOREIGN KEY(parent_id) REFERENCES {parent}(id)) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {parent} VALUES(1),(2),(99)")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {child} VALUES(1,1),(2,2)")).unwrap();
        let output = std::env::temp_dir().join(unique("tf_merge_fk"));
        let exported = run("dump.run", json!({"source": endpoint, "tables": [child], "output_dir": output, "data_format": format, "compression": "none", "chunk_size": 1, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
        assert!(success(&exported), "{exported:?}");
        global.conn.query_drop(format!("DELETE FROM {child}")).unwrap();
        global.conn.query_drop(format!("DELETE FROM {parent} WHERE id<>99")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {child} VALUES(99,99)")).unwrap();
        let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": "merge", "threads": threads}));
        let rows: Vec<(u64,u64)> = global.conn.query(format!("SELECT id,parent_id FROM {child} ORDER BY id")).unwrap();
        if success(&imported) || rows != vec![(99,99)] {
            failures.push(format!("{format} local_infile={local_infile} threads={threads}: success={} rows={rows:?}", success(&imported)));
        }
        global.conn.query_drop(format!("DROP TABLE {child}")).unwrap();
        global.conn.query_drop(format!("DROP TABLE {parent}")).unwrap();
        std::fs::remove_dir_all(output).unwrap();
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn mysql_import_timezone_reaches_reconnected_and_parallel_sessions() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let original = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn, local_infile: original };
    let mut failures = Vec::new();
    for (local_infile, threads, policy) in [(1, 1, "fallback"), (1, 2, "fallback"), (0, 1, "temporary_global")] {
        global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
        let table = unique("tf_timezone");
        global.conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, stamp TIMESTAMP) ENGINE=InnoDB")).unwrap();
        global.conn.query_drop(format!("INSERT INTO {table} VALUES (1,'2026-01-02 12:34:56'),(2,'2026-01-02 12:34:56')")).unwrap();
        let original_stamp: u64 = global.conn.query_first(format!("SELECT UNIX_TIMESTAMP(stamp) FROM {table} WHERE id=1")).unwrap().unwrap();
        let output = std::env::temp_dir().join(unique("tf_timezone"));
        let exported = run("dump.run", json!({"source": endpoint, "tables": [table], "output_dir": output, "data_format": "tsv", "compression": "none", "chunk_size": 1, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
        assert!(success(&exported), "{exported:?}");
        let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode": "replace", "threads": threads, "mysql_local_infile_policy": policy, "timezone_sql": "SET SESSION time_zone = '+09:00'"}));
        assert!(success(&imported), "{imported:?}");
        let stamps: Vec<u64> = global.conn.query(format!("SELECT UNIX_TIMESTAMP(stamp) FROM {table} ORDER BY id")).unwrap();
        if stamps != vec![original_stamp - 32400; 2] {
            failures.push(format!("local_infile={local_infile} threads={threads} policy={policy}: expected {} got {stamps:?}", original_stamp - 32400));
        }
        global.conn.query_drop(format!("DROP TABLE {table}")).unwrap();
        std::fs::remove_dir_all(output).unwrap();
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn postgres_manifest_timezone_overrides_target_role_default_without_changing_wall_time() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let source_schema = unique("tf_tz_source");
    let target_schema = unique("tf_tz_target");
    let role = unique("tf_tz_role");
    let source = Endpoint {
        engine: "postgresql".into(), host: host.clone(), port: 5432,
        user: "postgres".into(), password: "tf_local_test".into(), database: "tf_test".into(),
        schema: Some(source_schema.clone()),
    };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test")
        .dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!(
        "CREATE ROLE {role} LOGIN PASSWORD 'tf_local_test'; ALTER ROLE {role} SET timezone='Asia/Seoul';
         CREATE SCHEMA {source_schema}; CREATE SCHEMA {target_schema} AUTHORIZATION {role};
         CREATE TABLE {source_schema}.temporal_values(id INT PRIMARY KEY, stamp TIMESTAMPTZ, wall TIMESTAMP);
         INSERT INTO {source_schema}.temporal_values VALUES(1,'2026-01-02 12:34:56+00','2026-01-02 12:34:56')"
    )).unwrap();
    let target = Endpoint { user: role.clone(), schema: Some(target_schema.clone()), ..source.clone() };
    let output = std::env::temp_dir().join(unique("tf_pg_timezone"));
    let exported = run("dump.run", json!({"source": source, "output_dir": output, "tables": ["temporal_values"], "data_format": "tsv", "compression": "zstd", "threads": 2}));
    assert!(success(&exported), "{exported:?}");
    let manifest: Value = serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_dump.json")).unwrap()).unwrap();
    assert_eq!(manifest["source_timezone"], "UTC");
    let imported = run("dump.import", json!({"target": target, "input_dir": output, "mode": "replace"}));
    let rows = if success(&imported) {
        Some(admin.query_one(&format!("SELECT EXTRACT(EPOCH FROM stamp)::BIGINT,wall::text FROM {target_schema}.temporal_values"), &[]).unwrap())
    } else { None };
    admin.batch_execute(&format!("DROP SCHEMA {target_schema} CASCADE; DROP SCHEMA {source_schema} CASCADE; DROP ROLE {role}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(success(&imported), "{imported:?}");
    let row = rows.unwrap();
    assert_eq!(row.get::<_, i64>(0), 1767357296);
    assert_eq!(row.get::<_, String>(1), "2026-01-02 12:34:56");
}

#[test]
fn postgres_failed_view_reports_partial_import_after_table_data_is_restored() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_partial_view");
    let endpoint = Endpoint {
        engine: "postgresql".into(), host: host.clone(), port: 5432,
        user: "postgres".into(), password: "tf_local_test".into(), database: "tf_test".into(),
        schema: Some(schema.clone()),
    };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test")
        .dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!(
        "CREATE SCHEMA {schema}; CREATE TABLE {schema}.items(id INT PRIMARY KEY, value TEXT);
         INSERT INTO {schema}.items VALUES(1,'restored')"
    )).unwrap();
    let output = std::env::temp_dir().join(unique("tf_partial_view"));
    let exported = run("dump.run", json!({"source": endpoint, "output_dir": output, "data_format": "jsonl", "compression": "none", "threads": 1}));
    assert!(success(&exported), "{exported:?}");
    let manifest_path = output.join("_tunnelforge_dump.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
    manifest["views"] = json!([{"name":"broken_view", "definition":"CREATE VIEW broken_view AS SELECT absent_column FROM items"}]);
    std::fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    admin.batch_execute(&format!("UPDATE {schema}.items SET value='changed'")).unwrap();
    let imported = run("dump.import", json!({"target": endpoint, "input_dir": output, "mode":"replace"}));
    let result = imported.iter().find(|event| event["event"] == "result").unwrap();
    let report: Value = serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_import_report.json")).unwrap()).unwrap();
    let value: String = admin.query_one(&format!("SELECT value FROM {schema}.items WHERE id=1"), &[]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_eq!(value, "restored");
    for payload in [result, &report] {
        assert_eq!(payload["success"], false);
        assert_eq!(payload["status"], "partial");
        assert_eq!(payload["data_imported"], true);
        assert_eq!(payload["rows_imported"], 1);
        assert_eq!(payload["views_failed"].as_array().unwrap().len(), 1);
        assert!(payload["message"].as_str().unwrap().contains("view"));
    }
}

#[test]
fn mysql_incompatible_surviving_foreign_key_refuses_before_replacing_parent() {
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    for mode in ["replace", "recreate"] {
        let parent = unique("tf_fk_keep_parent");
        let child = unique("tf_fk_keep_child");
        conn.query_drop(format!("CREATE TABLE {parent}(id INT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
        conn.query_drop(format!("INSERT INTO {parent} VALUES(1)")).unwrap();
        let output = std::env::temp_dir().join(unique("tf_fk_keep"));
        let exported = run("dump.run", json!({"source": endpoint, "tables":[parent], "output_dir":output, "data_format":"jsonl", "threads":1, "mysql_snapshot_mode":"single_connection"}));
        assert!(success(&exported), "{exported:?}");
        conn.query_drop(format!("DROP TABLE {parent}")).unwrap();
        conn.query_drop(format!("CREATE TABLE {parent}(id BIGINT UNSIGNED PRIMARY KEY) ENGINE=InnoDB")).unwrap();
        conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id BIGINT UNSIGNED, FOREIGN KEY(parent_id) REFERENCES {parent}(id)) ENGINE=InnoDB")).unwrap();
        conn.query_drop(format!("INSERT INTO {parent} VALUES(99)")).unwrap();
        conn.query_drop(format!("INSERT INTO {child} VALUES(7,99)")).unwrap();
        let imported = run("dump.import", json!({"target":endpoint, "input_dir":output, "mode":mode, "threads":1}));
        let parent_row = conn.query_first::<u64,_>(format!("SELECT id FROM {parent}"));
        let valid_child = conn.query_first::<u64,_>(format!("SELECT COUNT(*) FROM {child} c JOIN {parent} p ON c.parent_id=p.id WHERE c.id=7"));
        conn.query_drop(format!("DROP TABLE {child}")).unwrap();
        conn.query_drop(format!("DROP TABLE IF EXISTS {parent}")).unwrap();
        std::fs::remove_dir_all(output).unwrap();
        assert!(!success(&imported));
        assert_eq!(parent_row.ok().flatten(), Some(99), "{mode}: original target parent was changed");
        assert_eq!(valid_child.ok().flatten(), Some(1), "{mode}: target-only child FK was broken");
        let error = imported.iter().find(|event| event["event"] == "error").unwrap();
        assert!(error["message"].as_str().unwrap().contains("incompatible_surviving_fk"));
    }
}

#[test]
fn mysql_server_ddl_probe_rejects_before_original_table_is_dropped() {
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let table = unique("tf_ddl_probe");
    conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, value INT) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {table} VALUES(1,17)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_ddl_probe"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[table],"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    let path = output.join("_tunnelforge_dump.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    manifest["schema"]["tables"][0]["columns"][1]["type"] = json!("DECIMAL(100,2)");
    std::fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    std::fs::write(output.join("_tunnelforge_import_report.json"), b"{\"success\":true}").unwrap();
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    let original = conn.query_first::<u64,_>(format!("SELECT value FROM {table} WHERE id=1"));
    let report: Value = serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_import_report.json")).unwrap()).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {table}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(original.ok().flatten(), Some(17), "server-invalid DDL destroyed original target");
    assert_eq!(report["success"], false);
    assert_eq!(report["phase"], "dump_import_ddl_preflight");
    assert_eq!(report["dropped_tables"], json!([]));
}

#[test]
fn mysql_import_failure_persists_confirmed_changes_and_unattempted_tables() {
    use sha2::{Digest, Sha256};
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let suffix = unique("tf_journal");
    let tables = [format!("a_{suffix}"),format!("b_{suffix}"),format!("c_{suffix}")];
    for table in &tables {
        conn.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY, value INT) ENGINE=InnoDB")).unwrap();
        conn.query_drop(format!("INSERT INTO {table} VALUES(1,17)")).unwrap();
    }
    let output = std::env::temp_dir().join(unique("tf_journal"));
    assert!(success(&run("dump.run",json!({"source":endpoint,"tables":tables,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}))));
    let path = output.join("_tunnelforge_dump.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let table = manifest["tables"].as_array_mut().unwrap().iter_mut().find(|table|table["name"]==tables[1]).unwrap();
    let chunk = output.join(table["path"].as_str().unwrap()).join("chunk_000001.jsonl");
    let bytes = b"{\"id\":1,\"value\":\"not-an-integer\"}\n";
    std::fs::write(chunk,bytes).unwrap();
    table["chunk_sha256"]["chunk_000001.jsonl"] = json!(format!("{:x}",Sha256::digest(bytes)));
    std::fs::write(path,serde_json::to_vec(&manifest).unwrap()).unwrap();
    std::fs::write(output.join("_tunnelforge_import_report.json"), b"{\"success\":true}").unwrap();
    let imported = run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let report: Value = serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_import_report.json")).unwrap()).unwrap();
    for table in &tables { conn.query_drop(format!("DROP TABLE IF EXISTS {table}")).unwrap(); }
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(report["success"],false);
    assert_eq!(report["status"],"failed");
    assert_eq!(report["phase"],"dump_import_data");
    assert_eq!(report["dropped_tables"].as_array().unwrap().len(),3);
    assert_eq!(report["data_loaded_tables"],json!([tables[0]]));
    assert_eq!(report["failed_tables"],json!([tables[1]]));
    assert_eq!(report["unattempted_tables"],json!([tables[2]]));
    assert_eq!(report["rows_imported"],1);
    assert_eq!(report["target"]["engine"],"mysql");
    assert!(report["error"].as_str().unwrap().contains("not-an-integer"));
    assert!(!report.to_string().contains("tf_local_test"));
    assert_eq!(imported.iter().filter(|event|event["event"]=="target_change" && event["status"]=="completed").count(),3);
}

#[test]
fn postgres_post_load_failure_persists_restored_data_and_incomplete_finalization() {
    use sha2::{Digest, Sha256};
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_postload_report");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()) };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.items(id INT PRIMARY KEY, value INT UNIQUE); INSERT INTO {schema}.items VALUES(1,17),(2,18)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_postload_report"));
    assert!(success(&run("dump.run",json!({"source":endpoint,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1}))));
    let path=output.join("_tunnelforge_dump.json");
    let mut manifest:Value=serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let table=&mut manifest["tables"][0];
    let bytes=b"{\"id\":1,\"value\":17}\n{\"id\":2,\"value\":17}\n";
    std::fs::write(output.join(table["path"].as_str().unwrap()).join("chunk_000001.jsonl"),bytes).unwrap();
    table["chunk_sha256"]["chunk_000001.jsonl"]=json!(format!("{:x}",Sha256::digest(bytes)));
    std::fs::write(path,serde_json::to_vec(&manifest).unwrap()).unwrap();
    let imported=run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    let report:Value=serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_import_report.json")).unwrap()).unwrap();
    let rows:i64=admin.query_one(&format!("SELECT COUNT(*) FROM {schema}.items"),&[]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(rows,2);
    assert_eq!(report["success"],false);
    assert_eq!(report["phase"],"dump_import_post_load");
    assert_eq!(report["data_loaded_tables"],json!(["items"]));
    assert_eq!(report["rows_imported"],2);
    assert_eq!(report["post_load_completed"],false);
    assert_eq!(report["unattempted_tables"],json!([]));
    assert!(report["error"].as_str().unwrap().contains("post_load"));
}

#[test]
fn postgres_legacy_replace_refuses_dependent_views_before_any_table_drop() {
    let Ok(host)=std::env::var("TF_POSTGRES_HOST") else{return};
    let schema=unique("tf_pg_dependency");
    let endpoint=Endpoint{engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone())};
    let mut admin=postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.a_protected(id INT PRIMARY KEY); CREATE TABLE {schema}.z_independent(id INT PRIMARY KEY); INSERT INTO {schema}.a_protected VALUES(7); INSERT INTO {schema}.z_independent VALUES(11); CREATE VIEW {schema}.v_protected AS SELECT * FROM {schema}.a_protected")).unwrap();
    let output=std::env::temp_dir().join(unique("tf_pg_dependency"));
    assert!(success(&run("dump.run",json!({"source":endpoint,"output_dir":output,"data_format":"jsonl","threads":1}))));
    let imported=run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    let preserved=admin.query_one(&format!("SELECT id FROM {schema}.z_independent"),&[]).ok().map(|row|row.get::<_,i32>(0));
    let view_value:i32=admin.query_one(&format!("SELECT id FROM {schema}.v_protected"),&[]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(preserved,Some(11),"an unrelated table was dropped before discovering the view dependency");
    assert_eq!(view_value,7);
    assert!(!imported.iter().any(|event|event["event"]=="target_change"));
    assert!(imported.iter().any(|event|event["message"].as_str().is_some_and(|message|message.contains("target_dependency_preflight_failed"))));
}
