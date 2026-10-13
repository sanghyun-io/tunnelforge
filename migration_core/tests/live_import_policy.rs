use migration_core::{handle_request, Endpoint, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::sync::Mutex;

static MYSQL_GLOBAL_LOCK: Mutex<()> = Mutex::new(());

fn endpoint() -> Option<Endpoint> {
    Some(Endpoint {
        engine: "mysql".into(), host: std::env::var("TF_MYSQL_HOST").ok()?,
        port: std::env::var("TF_MYSQL_PORT").ok().and_then(|port| port.parse().ok()).unwrap_or(3306),
        user: "root".into(), password: "tf_local_test".into(), database: "tf_test".into(), schema: None,
        tls: Default::default(),
    })
}

fn connect(endpoint: &Endpoint) -> mysql::PooledConn {
    mysql::Pool::new(mysql::OptsBuilder::new().ip_or_hostname(Some(&endpoint.host)).tcp_port(endpoint.port)
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
fn mysql_scoped_account_imports_more_legacy_enum_zero_cells_than_its_warning_cap() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut root = connect(&endpoint);
    let original = root.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
    let mut global = GlobalSettings { conn: connect(&endpoint), local_infile: original };
    let (db, user) = (unique("tf_scoped_enum"), "tf_import_scoped_enum");
    for sql in [
        format!("DROP USER IF EXISTS '{user}'@'%'"),
        format!("CREATE DATABASE {db}"),
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT ALL ON {db}.* TO '{user}'@'%'"),
        format!("CREATE TABLE {db}.legacy (id INT PRIMARY KEY, a ENUM('x','y') NOT NULL, b ENUM('x','y') NOT NULL, c ENUM('x','y') NOT NULL)"),
        "SET SESSION sql_mode=''".to_string(),
        format!("INSERT INTO {db}.legacy VALUES {}", (1..=600).map(|id| format!("({id},'','','')")).collect::<Vec<_>>().join(",")),
        "SET SESSION sql_mode=DEFAULT".to_string(),
    ] { root.query_drop(sql).unwrap(); }
    let source = Endpoint { database: db.clone(), ..endpoint.clone() };
    let scoped = Endpoint { user: user.into(), database: db.clone(), ..endpoint.clone() };
    let cap: u64 = connect(&scoped).query_first("SELECT @@SESSION.max_error_count").unwrap().unwrap();
    assert!(cap < 1800, "1800 ENUM-zero cells must exceed the scoped account's warning cap ({cap})");
    for (format, local_infile) in [("jsonl", 0), ("tsv", 0), ("tsv", 1)] {
        global.conn.query_drop(format!("SET GLOBAL local_infile={local_infile}")).unwrap();
        let output = std::env::temp_dir().join(unique("tf_scoped_enum"));
        let exported = run("dump.run", json!({"source": source, "output_dir": output, "data_format": format, "chunk_size": 10000, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
        assert!(success(&exported), "{exported:?}");
        root.query_drop(format!("DELETE FROM {db}.legacy")).unwrap();
        let imported = run("dump.import", json!({"target": scoped, "input_dir": output, "mode": "replace", "threads": 1}));
        let (rows, zero): (u64, u64) = root.query_first(format!("SELECT COUNT(*), SUM(a+0=0 AND b+0=0 AND c+0=0) FROM {db}.legacy")).unwrap().unwrap();
        std::fs::remove_dir_all(output).unwrap();
        assert!(success(&imported), "{format} local_infile={local_infile}: {imported:?}");
        assert_eq!((rows, zero), (600, 600), "{format} local_infile={local_infile}");
    }
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    root.query_drop(format!("DROP USER '{user}'@'%'")).unwrap();
}

#[test]
fn mysql_import_works_for_an_account_without_session_variables_admin() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut root = connect(&endpoint);
    let (db, user) = (unique("tf_scoped"), "tf_import_scoped");
    for sql in [
        format!("DROP USER IF EXISTS '{user}'@'%'"),
        format!("CREATE DATABASE {db}"),
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        // Database privileges only, like a typical application account.
        format!("GRANT ALL ON {db}.* TO '{user}'@'%'"),
        format!("CREATE TABLE {db}.parent (id INT PRIMARY KEY, label VARCHAR(20))"),
        format!("CREATE TABLE {db}.child (id INT PRIMARY KEY, parent_id INT, FOREIGN KEY (parent_id) REFERENCES {db}.parent (id))"),
        format!("INSERT INTO {db}.parent VALUES (1,'a'),(2,'b')"),
        format!("INSERT INTO {db}.child VALUES (10,1),(11,2)"),
    ] { root.query_drop(sql).unwrap(); }
    let source = Endpoint { database: db.clone(), ..endpoint.clone() };
    let scoped = Endpoint { user: user.into(), database: db.clone(), ..endpoint.clone() };
    assert!(connect(&scoped).query_drop("SET SESSION max_error_count=65535").is_err(), "the account must lack SESSION_VARIABLES_ADMIN");
    let output = std::env::temp_dir().join(unique("tf_scoped"));
    let exported = run("dump.run", json!({"source": source, "output_dir": output, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
    assert!(success(&exported), "{exported:?}");
    for mode in ["replace", "recreate", "merge"] {
        root.query_drop(format!("DELETE FROM {db}.child")).unwrap();
        root.query_drop(format!("DELETE FROM {db}.parent")).unwrap();
        let imported = run("dump.import", json!({"target": scoped, "input_dir": output, "mode": mode, "threads": 2}));
        assert!(success(&imported), "{mode}: {imported:?}");
        let rows: Vec<(u64, u64)> = root.query(format!("SELECT c.id, p.id FROM {db}.child c JOIN {db}.parent p ON p.id = c.parent_id ORDER BY c.id")).unwrap();
        assert_eq!(rows, vec![(10, 1), (11, 2)], "{mode}");
    }
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    root.query_drop(format!("DROP USER '{user}'@'%'")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
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
        tls: Default::default(),
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
        tls: Default::default(),
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
fn mysql_replace_keeps_target_only_child_of_an_auto_increment_parent() {
    // The dump writes the parent key as `int auto_increment`; the target reports `int`.
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let parent = unique("tf_fk_ai_parent");
    let child = unique("tf_fk_ai_child");
    conn.query_drop(format!("CREATE TABLE {parent}(id INT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {parent} VALUES(1)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_fk_ai"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[parent],"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id INT, FOREIGN KEY(parent_id) REFERENCES {parent}(id)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {child} VALUES(7,1)")).unwrap();
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let child_fks = conn.query_first::<u64,_>(format!("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='{child}' AND REFERENCED_TABLE_NAME='{parent}'"));
    conn.query_drop(format!("DROP TABLE {child}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {parent}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(success(&imported), "{imported:?}");
    assert_eq!(child_fks.ok().flatten(), Some(1), "target-only child lost its foreign key");
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
    let untouched = conn.query_first::<u64,_>(format!("SELECT value FROM {} WHERE id=1", tables[2]));
    for table in &tables { conn.query_drop(format!("DROP TABLE IF EXISTS {table}")).unwrap(); }
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(report["success"],false);
    assert_eq!(report["status"],"failed");
    assert_eq!(report["phase"],"dump_import_data");
    // mysqldump처럼 테이블마다 DROP→CREATE: 실패 이후 테이블은 원본을 유지한다.
    assert_eq!(report["dropped_tables"],json!([tables[0],tables[1]]));
    assert_eq!(report["dropped_not_restored_tables"],json!([tables[1]]));
    assert_eq!(untouched.ok().flatten(),Some(17),"a table after the failure lost its original data");
    assert_eq!(report["data_loaded_tables"],json!([tables[0]]));
    assert_eq!(report["failed_tables"],json!([tables[1]]));
    assert_eq!(report["unattempted_tables"],json!([tables[2]]));
    assert_eq!(report["rows_imported"],1);
    assert_eq!(report["target"]["engine"],"mysql");
    assert!(report["error"].as_str().unwrap().contains("not-an-integer"));
    assert!(!report.to_string().contains("tf_local_test"));
    assert_eq!(imported.iter().filter(|event|event["event"]=="target_change" && event["status"]=="completed").count(),2);
    let summary = imported.iter().filter(|event|event["event"]=="import_report").last().unwrap();
    assert_eq!(summary["dropped_not_restored_tables"],json!([tables[1]]));
    assert_eq!(summary["unattempted_tables"],json!([tables[2]]));
}

#[test]
fn mysql_replace_refuses_surviving_fk_bound_to_post_load_index_before_any_drop() {
    // MySQL creates non-unique indexes after the data load, so re-creating the
    // parent would fail (ERROR 1822/6125) right after it was dropped.
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let parent = unique("tf_fk_nonunique_parent");
    let child = unique("tf_fk_nonunique_child");
    conn.query_drop(format!("CREATE TABLE {parent}(id INT PRIMARY KEY, code VARCHAR(20) NOT NULL, KEY idx_code(code)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {parent} VALUES(1,'a')")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_fk_nonunique"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[parent],"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    // MySQL 8.4 refuses new FKs on non-unique keys by default; 8.0 has no such variable.
    let _ = conn.query_drop("SET SESSION restrict_fk_on_non_standard_key=OFF");
    conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, code VARCHAR(20), CONSTRAINT fk_{child} FOREIGN KEY(code) REFERENCES {parent}(code)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {child} VALUES(7,'a')")).unwrap();
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let report: Value = serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_import_report.json")).unwrap()).unwrap();
    let parent_row = conn.query_first::<String,_>(format!("SELECT code FROM {parent} WHERE id=1"));
    conn.query_drop(format!("DROP TABLE {child}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {parent}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(parent_row.ok().flatten().as_deref(), Some("a"), "parent was dropped before the import failed");
    assert_eq!(report["dropped_tables"], json!([]));
    let error = imported.iter().find(|event| event["event"] == "error").unwrap();
    assert!(error["message"].as_str().unwrap().contains("incompatible_surviving_fk"), "{error}");
}

#[test]
fn mysql_replace_rebinds_import_set_child_after_parent_key_type_change() {
    // Before per-table replace, re-creating the parent while the original child
    // FK (INT) was alive raised ERROR 3780; the in-set FK is now dropped first.
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let parent = unique("tf_rebind_parent");
    let child = unique("tf_rebind_child");
    conn.query_drop(format!("CREATE TABLE {parent}(id BIGINT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id BIGINT, CONSTRAINT fk_{child} FOREIGN KEY(parent_id) REFERENCES {parent}(id)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {parent} VALUES(5000000000)")).unwrap();
    conn.query_drop(format!("INSERT INTO {child} VALUES(1,5000000000)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_rebind"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[parent,child],"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    conn.query_drop(format!("DROP TABLE {child}")).unwrap();
    conn.query_drop(format!("DROP TABLE {parent}")).unwrap();
    conn.query_drop(format!("CREATE TABLE {parent}(id INT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_{child} FOREIGN KEY(parent_id) REFERENCES {parent}(id)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {parent} VALUES(1)")).unwrap();
    conn.query_drop(format!("INSERT INTO {child} VALUES(9,1)")).unwrap();
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let joined = conn.query_first::<u64,_>(format!("SELECT COUNT(*) FROM {child} c JOIN {parent} p ON c.parent_id=p.id WHERE p.id=5000000000"));
    let fks = conn.query_first::<u64,_>(format!("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='{child}' AND REFERENCED_TABLE_NAME='{parent}'"));
    conn.query_drop(format!("DROP TABLE IF EXISTS {child}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {parent}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(success(&imported), "{imported:?}");
    assert_eq!(joined.ok().flatten(), Some(1));
    assert_eq!(fks.ok().flatten(), Some(1), "dump FK was not recreated");
    let dropped_fk = imported.iter().any(|event| event["event"] == "target_change" && event["action"] == "drop_foreign_key" && event["table"] == child.as_str());
    assert!(dropped_fk, "the original in-set child FK was not dropped before replacing its parent");
}

#[test]
fn postgres_replace_drops_import_set_child_fk_before_parent() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_pg_per_table");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default() };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.parent(id INT PRIMARY KEY); CREATE TABLE {schema}.child(id INT PRIMARY KEY, parent_id INT CONSTRAINT fk_child_parent REFERENCES {schema}.parent(id)); INSERT INTO {schema}.parent VALUES(1); INSERT INTO {schema}.child VALUES(10,1)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_pg_per_table"));
    assert!(success(&run("dump.run",json!({"source":endpoint,"output_dir":output,"data_format":"jsonl","threads":1}))));
    admin.batch_execute(&format!("INSERT INTO {schema}.parent VALUES(2); INSERT INTO {schema}.child VALUES(20,2)")).unwrap();
    let imported = run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    let rows:i64 = admin.query_one(&format!("SELECT COUNT(*) FROM {schema}.child c JOIN {schema}.parent p ON p.id=c.parent_id"),&[]).unwrap().get(0);
    let fks:i64 = admin.query_one("SELECT COUNT(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE c.contype='f' AND n.nspname=$1",&[&schema]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(success(&imported), "{imported:?}");
    assert_eq!(rows,1);
    assert_eq!(fks,1);
    assert!(imported.iter().any(|event| event["event"]=="target_change" && event["action"]=="drop_foreign_key" && event["table"]=="child"));
}

#[test]
fn postgres_replace_failure_keeps_unreached_child_and_reports_its_dropped_fk() {
    use sha2::{Digest, Sha256};
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_pg_per_table_fail");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default() };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.parent(id INT PRIMARY KEY); CREATE TABLE {schema}.child(id INT PRIMARY KEY, parent_id INT CONSTRAINT fk_child_parent REFERENCES {schema}.parent(id)); INSERT INTO {schema}.parent VALUES(1); INSERT INTO {schema}.child VALUES(10,1)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_pg_per_table_fail"));
    assert!(success(&run("dump.run",json!({"source":endpoint,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1}))));
    let path = output.join("_tunnelforge_dump.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let table = manifest["tables"].as_array_mut().unwrap().iter_mut().find(|table| table["name"]=="parent").unwrap();
    let bytes = b"{\"id\":\"not-an-integer\"}\n";
    std::fs::write(output.join(table["path"].as_str().unwrap()).join("chunk_000001.jsonl"), bytes).unwrap();
    table["chunk_sha256"]["chunk_000001.jsonl"] = json!(format!("{:x}", Sha256::digest(bytes)));
    std::fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let imported = run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    let child_rows:i64 = admin.query_one(&format!("SELECT COUNT(*) FROM {schema}.child"),&[]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(child_rows, 1, "the unreached child lost its original rows");
    let summary = imported.iter().filter(|event| event["event"]=="import_report").last().unwrap();
    assert_eq!(summary["dropped_not_restored_tables"], json!(["parent"]));
    assert_eq!(summary["unattempted_tables"], json!(["child"]));
    let dropped = &summary["dropped_foreign_keys"][0];
    assert_eq!(summary["dropped_foreign_keys"].as_array().unwrap().len(), 1);
    assert_eq!((&dropped["table"], &dropped["constraint"], &dropped["referenced_table"]), (&json!("child"), &json!("fk_child_parent"), &json!("parent")));
    assert!(dropped["definition"].as_str().unwrap().contains("REFERENCES"), "{dropped}");
    // The parent itself failed, so there is nothing to link the child to again.
    assert_eq!(summary["restored_foreign_keys"], json!([]));
}

#[test]
fn postgres_post_load_failure_persists_restored_data_and_incomplete_finalization() {
    use sha2::{Digest, Sha256};
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_postload_report");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default() };
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

/// Rewrites one table's only chunk so that table fails, then returns the replace import events.
fn import_with_rewritten_chunk(target: &Endpoint, output: &std::path::Path, table_name: &str, bytes: &[u8]) -> Vec<Value> {
    use sha2::{Digest, Sha256};
    let path = output.join("_tunnelforge_dump.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let table = manifest["tables"].as_array_mut().unwrap().iter_mut().find(|table| table["name"] == table_name).unwrap();
    std::fs::write(output.join(table["path"].as_str().unwrap()).join("chunk_000001.jsonl"), bytes).unwrap();
    table["chunk_sha256"]["chunk_000001.jsonl"] = json!(format!("{:x}", Sha256::digest(bytes)));
    std::fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    run("dump.import", json!({"target": target, "input_dir": output, "mode": "replace", "threads": 1}))
}

fn import_with_duplicate_third_table(target: &Endpoint, output: &std::path::Path) -> Vec<Value> {
    import_with_rewritten_chunk(target, output, "t3_items", b"{\"id\":1,\"child_id\":10,\"code\":7}\n{\"id\":2,\"child_id\":11,\"code\":7}\n")
}

fn import_report(imported: &[Value]) -> &Value {
    imported.iter().filter(|event| event["event"] == "import_report").last().unwrap()
}

fn assert_failed_on(imported: &[Value], table: &str) {
    assert!(!success(imported), "{imported:?}");
    assert_eq!(import_report(imported)["failed_tables"], json!([table]), "{}", import_report(imported));
}

fn assert_failed_on_third_table(imported: &[Value]) {
    assert_failed_on(imported, "t3_items");
}

#[test]
fn mysql_replace_failure_leaves_earlier_tables_with_their_indexes_and_foreign_keys() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut root = connect(&endpoint);
    let db = unique("tf_per_table");
    for sql in [
        format!("CREATE DATABASE {db}"),
        format!("CREATE TABLE {db}.t1_parent (id INT PRIMARY KEY)"),
        format!("CREATE TABLE {db}.t2_child (id INT PRIMARY KEY, parent_id INT, KEY idx_t2_parent (parent_id), CONSTRAINT fk_t2_parent FOREIGN KEY (parent_id) REFERENCES {db}.t1_parent (id))"),
        // t3 references t2 so it is replaced last. MySQL creates unique keys with the table,
        // so duplicate rows fail while loading t3.
        format!("CREATE TABLE {db}.t3_items (id INT PRIMARY KEY, child_id INT, code INT, UNIQUE KEY uq_t3_code (code), CONSTRAINT fk_t3_child FOREIGN KEY (child_id) REFERENCES {db}.t2_child (id))"),
        format!("INSERT INTO {db}.t1_parent VALUES (1),(2)"),
        format!("INSERT INTO {db}.t2_child VALUES (10,1),(11,2)"),
        format!("INSERT INTO {db}.t3_items VALUES (1,10,7),(2,11,8)"),
    ] { root.query_drop(sql).unwrap(); }
    let target = Endpoint { database: db.clone(), ..endpoint.clone() };
    let output = std::env::temp_dir().join(unique("tf_per_table"));
    assert!(success(&run("dump.run", json!({"source": target, "output_dir": output, "data_format": "jsonl", "compression": "none", "threads": 1, "mysql_snapshot_mode": "single_connection"}))));
    let imported = import_with_duplicate_third_table(&target, &output);
    let index: u64 = root.query_first(format!("SELECT COUNT(*) FROM information_schema.statistics WHERE TABLE_SCHEMA='{db}' AND TABLE_NAME='t2_child' AND INDEX_NAME='idx_t2_parent'")).unwrap().unwrap();
    let fk: u64 = root.query_first(format!("SELECT COUNT(*) FROM information_schema.referential_constraints WHERE CONSTRAINT_SCHEMA='{db}' AND CONSTRAINT_NAME='fk_t2_parent'")).unwrap().unwrap();
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_failed_on_third_table(&imported);
    assert_eq!((index, fk), (1, 1), "the replaced child must keep its index and foreign key");
}

#[test]
fn postgres_replace_failure_leaves_earlier_tables_with_their_indexes_and_foreign_keys() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_pg_per_table_done");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default() };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    // t3 references t2 so it is replaced last. PostgreSQL builds the unique index after
    // t3's rows, so t3 fails while finishing.
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.t1_parent(id INT PRIMARY KEY); CREATE TABLE {schema}.t2_child(id INT PRIMARY KEY, parent_id INT CONSTRAINT fk_t2_parent REFERENCES {schema}.t1_parent(id)); CREATE INDEX idx_t2_parent ON {schema}.t2_child(parent_id); CREATE TABLE {schema}.t3_items(id INT PRIMARY KEY, child_id INT CONSTRAINT fk_t3_child REFERENCES {schema}.t2_child(id), code INT CONSTRAINT uq_t3_code UNIQUE); INSERT INTO {schema}.t1_parent VALUES(1),(2); INSERT INTO {schema}.t2_child VALUES(10,1),(11,2); INSERT INTO {schema}.t3_items VALUES(1,10,7),(2,11,8)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_pg_per_table_done"));
    assert!(success(&run("dump.run", json!({"source": endpoint, "output_dir": output, "data_format": "jsonl", "compression": "none", "threads": 1}))));
    let imported = import_with_duplicate_third_table(&endpoint, &output);
    let index: i64 = admin.query_one("SELECT COUNT(*) FROM pg_indexes WHERE schemaname=$1 AND indexname='idx_t2_parent'", &[&schema]).unwrap().get(0);
    let fk: i64 = admin.query_one("SELECT COUNT(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE c.contype='f' AND n.nspname=$1 AND c.conname='fk_t2_parent'", &[&schema]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_failed_on_third_table(&imported);
    assert_eq!((index, fk), (1, 1), "the replaced child must keep its index and foreign key");
}

/// t1_parent and t2_fail have no dependencies, so the order is t1, t2, t3_child: replacing t1
/// drops the original t3_child's FK, then t2_fail fails before t3_child is reached.
const UNREACHED_CHILD_FAIL: &[u8] = b"{\"id\":1,\"code\":7}\n{\"id\":2,\"code\":7}\n";

fn assert_unreached_child_keeps_its_fk(imported: &[Value], fk_count: i64, child_rows: i64) {
    assert_failed_on(imported, "t2_fail");
    let report = import_report(imported);
    assert_eq!(report["unattempted_tables"], json!(["t3_child"]), "{report}");
    assert_eq!(report["restored_foreign_keys"], json!(["t3_child.fk_t3_parent"]), "{report}");
    assert_eq!((fk_count, child_rows), (1, 2), "the never-reached child must keep its FK and rows");
}

#[test]
fn mysql_replace_failure_re_adds_fks_of_children_it_never_reached() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut root = connect(&endpoint);
    let db = unique("tf_unreached");
    for sql in [
        format!("CREATE DATABASE {db}"),
        format!("CREATE TABLE {db}.t1_parent (id INT PRIMARY KEY)"),
        format!("CREATE TABLE {db}.t2_fail (id INT PRIMARY KEY, code INT, UNIQUE KEY uq_t2_code (code))"),
        format!("CREATE TABLE {db}.t3_child (id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_t3_parent FOREIGN KEY (parent_id) REFERENCES {db}.t1_parent (id) ON DELETE CASCADE)"),
        format!("INSERT INTO {db}.t1_parent VALUES (1),(2)"),
        format!("INSERT INTO {db}.t2_fail VALUES (1,7),(2,8)"),
        format!("INSERT INTO {db}.t3_child VALUES (10,1),(11,2)"),
    ] { root.query_drop(sql).unwrap(); }
    let target = Endpoint { database: db.clone(), ..endpoint.clone() };
    let output = std::env::temp_dir().join(unique("tf_unreached"));
    assert!(success(&run("dump.run", json!({"source": target, "output_dir": output, "data_format": "jsonl", "compression": "none", "threads": 1, "mysql_snapshot_mode": "single_connection"}))));
    let imported = import_with_rewritten_chunk(&target, &output, "t2_fail", UNREACHED_CHILD_FAIL);
    let fk: u64 = root.query_first(format!("SELECT COUNT(*) FROM information_schema.referential_constraints WHERE CONSTRAINT_SCHEMA='{db}' AND CONSTRAINT_NAME='fk_t3_parent' AND DELETE_RULE='CASCADE'")).unwrap().unwrap();
    let rows: u64 = root.query_first(format!("SELECT COUNT(*) FROM {db}.t3_child")).unwrap().unwrap();
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_unreached_child_keeps_its_fk(&imported, fk as i64, rows as i64);
}

#[test]
fn postgres_replace_failure_re_adds_fks_of_children_it_never_reached() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_pg_unreached");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default() };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.t1_parent(id INT PRIMARY KEY); CREATE TABLE {schema}.t2_fail(id INT PRIMARY KEY, code INT CONSTRAINT uq_t2_code UNIQUE); CREATE TABLE {schema}.t3_child(id INT PRIMARY KEY, parent_id INT CONSTRAINT fk_t3_parent REFERENCES {schema}.t1_parent(id) ON DELETE CASCADE); INSERT INTO {schema}.t1_parent VALUES(1),(2); INSERT INTO {schema}.t2_fail VALUES(1,7),(2,8); INSERT INTO {schema}.t3_child VALUES(10,1),(11,2)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_pg_unreached"));
    assert!(success(&run("dump.run", json!({"source": endpoint, "output_dir": output, "data_format": "jsonl", "compression": "none", "threads": 1}))));
    let imported = import_with_rewritten_chunk(&endpoint, &output, "t2_fail", UNREACHED_CHILD_FAIL);
    let fk: i64 = admin.query_one("SELECT COUNT(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE c.contype='f' AND n.nspname=$1 AND c.conname='fk_t3_parent' AND c.confdeltype='c'", &[&schema]).unwrap().get(0);
    let rows: i64 = admin.query_one(&format!("SELECT COUNT(*) FROM {schema}.t3_child"), &[]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_unreached_child_keeps_its_fk(&imported, fk, rows);
}

#[test]
fn postgres_legacy_replace_refuses_dependent_views_before_any_table_drop() {
    let Ok(host)=std::env::var("TF_POSTGRES_HOST") else{return};
    let schema=unique("tf_pg_dependency");
    let endpoint=Endpoint{engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default()};
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

fn assert_refused_before_any_drop(imported: &[Value], needle: &str) {
    assert!(!success(imported));
    let error = imported.iter().find(|event| event["event"] == "error").unwrap()["message"].as_str().unwrap();
    assert!(error.contains("ddl_preflight_failed") && error.contains(needle), "{error}");
    assert_eq!(import_report(imported)["dropped_tables"], json!([]));
}

fn mysql_probe_tables(conn: &mut mysql::PooledConn, db: &str) -> u64 {
    conn.query_first(format!("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='{db}' AND TABLE_NAME LIKE 'tf\\_probe\\_%'")).unwrap().unwrap()
}

#[test]
fn mysql_replace_without_references_privilege_refuses_before_any_drop() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut root = connect(&endpoint);
    let (db, user) = (unique("tf_noref"), "tf_import_noref");
    for sql in [
        format!("DROP USER IF EXISTS '{user}'@'%'"),
        format!("CREATE DATABASE {db}"),
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT ALL ON {db}.* TO '{user}'@'%'"),
        format!("REVOKE REFERENCES ON {db}.* FROM '{user}'@'%'"),
        format!("CREATE TABLE {db}.parent (id INT PRIMARY KEY, label VARCHAR(10))"),
        format!("CREATE TABLE {db}.child (id INT PRIMARY KEY, parent_id INT, FOREIGN KEY (parent_id) REFERENCES {db}.parent (id))"),
        format!("INSERT INTO {db}.parent VALUES (1,'dumped')"),
        format!("INSERT INTO {db}.child VALUES (10,1)"),
    ] { root.query_drop(sql).unwrap(); }
    let source = Endpoint { database: db.clone(), ..endpoint.clone() };
    let scoped = Endpoint { user: user.into(), database: db.clone(), ..endpoint.clone() };
    let output = std::env::temp_dir().join(unique("tf_noref"));
    let exported = run("dump.run", json!({"source": source, "output_dir": output, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
    assert!(success(&exported), "{exported:?}");
    root.query_drop(format!("UPDATE {db}.parent SET label='kept'")).unwrap();
    let imported = run("dump.import", json!({"target": scoped, "input_dir": output, "mode": "replace", "threads": 1}));
    let label = root.query_first::<String, _>(format!("SELECT label FROM {db}.parent WHERE id=1")).unwrap();
    let probes = mysql_probe_tables(&mut root, &db);
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    root.query_drop(format!("DROP USER '{user}'@'%'")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_refused_before_any_drop(&imported, "FOREIGN KEY");
    assert_eq!(label.as_deref(), Some("kept"), "a table was replaced before the refusal");
    assert_eq!(probes, 0);
}

#[test]
fn mysql_replace_refuses_a_foreign_key_to_an_incompatible_parent_outside_the_dump() {
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let parent = unique("tf_out_parent");
    let child = unique("tf_out_child");
    conn.query_drop(format!("CREATE TABLE {parent}(id INT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id INT, label VARCHAR(10), CONSTRAINT fk_{child} FOREIGN KEY(parent_id) REFERENCES {parent}(id)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {parent} VALUES(1)")).unwrap();
    conn.query_drop(format!("INSERT INTO {child} VALUES(7,1,'dumped')")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_out_parent"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[child],"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    // The parent stays outside the dump and its key becomes BIGINT, which the dumped INT FK cannot reference.
    for sql in [
        format!("DROP TABLE {child}"), format!("DROP TABLE {parent}"),
        format!("CREATE TABLE {parent}(id BIGINT PRIMARY KEY) ENGINE=InnoDB"), format!("INSERT INTO {parent} VALUES(1)"),
        format!("CREATE TABLE {child}(id INT PRIMARY KEY, parent_id INT, label VARCHAR(10)) ENGINE=InnoDB"),
        format!("INSERT INTO {child} VALUES(7,1,'kept')"),
    ] { conn.query_drop(sql).unwrap(); }
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let label = conn.query_first::<String, _>(format!("SELECT label FROM {child} WHERE id=7")).unwrap();
    let probes = mysql_probe_tables(&mut conn, "tf_test");
    conn.query_drop(format!("DROP TABLE IF EXISTS {child}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {parent}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_refused_before_any_drop(&imported, &format!("FOREIGN KEY fk_{child}"));
    assert_eq!(label.as_deref(), Some("kept"), "the child was replaced before the refusal");
    assert_eq!(probes, 0);
}

#[test]
fn mysql_replace_refuses_a_view_with_a_dumped_table_name_before_any_drop() {
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let first = unique("tf_view_first");
    let second = unique("tf_view_second");
    conn.query_drop(format!("CREATE TABLE {first}(id INT PRIMARY KEY, label VARCHAR(10)) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("CREATE TABLE {second}(id INT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {first} VALUES(1,'dumped')")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_view_name"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[first.clone(), second.clone()],"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    conn.query_drop(format!("UPDATE {first} SET label='kept'")).unwrap();
    conn.query_drop(format!("DROP TABLE {second}")).unwrap();
    conn.query_drop(format!("CREATE VIEW {second} AS SELECT id FROM {first}")).unwrap();
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let label = conn.query_first::<String, _>(format!("SELECT label FROM {first} WHERE id=1")).unwrap();
    conn.query_drop(format!("DROP VIEW IF EXISTS {second}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {second}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {first}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_refused_before_any_drop(&imported, &format!("{second} (VIEW)"));
    assert_eq!(label.as_deref(), Some("kept"), "an earlier table was replaced before the refusal");
}

#[test]
fn postgres_replace_refuses_an_incompatible_outside_parent_and_a_same_name_view_before_any_drop() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_pg_predrop");
    let endpoint = Endpoint { engine:"postgresql".into(),host:host.clone(),port:5432,user:"postgres".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:Some(schema.clone()), tls: Default::default() };
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.parent(id INT PRIMARY KEY); CREATE TABLE {schema}.child(id INT PRIMARY KEY, parent_id INT CONSTRAINT fk_child_parent REFERENCES {schema}.parent(id), label TEXT); CREATE TABLE {schema}.other(id INT PRIMARY KEY); INSERT INTO {schema}.parent VALUES(1); INSERT INTO {schema}.child VALUES(7,1,'dumped')")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_pg_predrop"));
    assert!(success(&run("dump.run",json!({"source":endpoint,"tables":["child","other"],"output_dir":output,"data_format":"jsonl","threads":1}))));
    // The parent outside the dump changes its key to TEXT, which the dumped INT FK cannot reference.
    admin.batch_execute(&format!("UPDATE {schema}.child SET label='kept'; ALTER TABLE {schema}.child DROP CONSTRAINT fk_child_parent; ALTER TABLE {schema}.parent ALTER COLUMN id TYPE TEXT")).unwrap();
    let fk_refusal = run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    admin.batch_execute(&format!("ALTER TABLE {schema}.parent ALTER COLUMN id TYPE INT USING id::int; DROP TABLE {schema}.other; CREATE VIEW {schema}.other AS SELECT 1 AS id")).unwrap();
    let view_refusal = run("dump.import",json!({"target":endpoint,"input_dir":output,"mode":"replace"}));
    let label: String = admin.query_one(&format!("SELECT label FROM {schema}.child WHERE id=7"),&[]).unwrap().get(0);
    let probes: i64 = admin.query_one("SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname LIKE 'tf\\_probe\\_%'",&[&schema]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert_refused_before_any_drop(&fk_refusal, "FOREIGN KEY fk_child_parent");
    assert_refused_before_any_drop(&view_refusal, "other (VIEW)");
    assert_eq!(label, "kept", "the child was replaced before a refusal");
    assert_eq!(probes, 0);
}

#[test]
fn mysql_scoped_replace_reports_a_view_it_cannot_replace_and_leaves_it() {
    let _guard = MYSQL_GLOBAL_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let Some(endpoint) = endpoint() else { return };
    let mut root = connect(&endpoint);
    let (db, user) = (unique("tf_view_owner"), "tf_import_view_owner");
    for sql in [
        format!("DROP USER IF EXISTS '{user}'@'%'"),
        format!("CREATE DATABASE {db}"),
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT ALL ON {db}.* TO '{user}'@'%'"),
        format!("CREATE TABLE {db}.items (id INT PRIMARY KEY)"),
        format!("INSERT INTO {db}.items VALUES (1)"),
        // Owned by root (SYSTEM_USER), as when an administrator set the database up.
        format!("CREATE SQL SECURITY INVOKER VIEW {db}.item_ids AS SELECT id FROM {db}.items"),
    ] { root.query_drop(sql).unwrap(); }
    let source = Endpoint { database: db.clone(), ..endpoint.clone() };
    let scoped = Endpoint { user: user.into(), database: db.clone(), ..endpoint.clone() };
    let output = std::env::temp_dir().join(unique("tf_view_owner"));
    let exported = run("dump.run", json!({"source": source, "output_dir": output, "threads": 1, "mysql_snapshot_mode": "single_connection"}));
    assert!(success(&exported), "{exported:?}");
    let imported = run("dump.import", json!({"target": scoped, "input_dir": output, "mode": "replace", "threads": 1}));
    let views: u64 = root.exec_first("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=? AND TABLE_NAME='item_ids'", (&db,)).unwrap().unwrap();
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    root.query_drop(format!("DROP USER '{user}'@'%'")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    let result = imported.iter().rev().find(|event| event["event"] == "result").unwrap();
    let failed = result["views_failed"].as_array().unwrap();
    assert_eq!(failed.len(), 1, "{result}");
    assert_eq!(failed[0]["name"], "item_ids");
    let error = failed[0]["error"].as_str().unwrap();
    assert!(error.contains("could not be replaced") && error.contains("SYSTEM_USER"), "{error}");
    assert_eq!(views, 1, "the existing view must stay");
}

#[test]
fn mysql_replace_refuses_a_kept_child_whose_parent_column_is_not_in_the_dump() {
    let Some(endpoint) = endpoint() else { return };
    let mut conn = connect(&endpoint);
    let parent = unique("tf_absent_parent");
    let child = unique("tf_absent_child");
    conn.query_drop(format!("CREATE TABLE {parent}(id INT PRIMARY KEY) ENGINE=InnoDB")).unwrap();
    conn.query_drop(format!("INSERT INTO {parent} VALUES(1)")).unwrap();
    let output = std::env::temp_dir().join(unique("tf_absent"));
    let exported = run("dump.run", json!({"source":endpoint,"tables":[parent],"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(success(&exported), "{exported:?}");
    // The target's parent grew a key column the dump does not have, and a kept child uses it.
    conn.query_drop(format!("ALTER TABLE {parent} ADD COLUMN legacy INT NULL, ADD UNIQUE KEY uq_legacy (legacy)")).unwrap();
    conn.query_drop(format!("UPDATE {parent} SET legacy=7")).unwrap();
    conn.query_drop(format!("CREATE TABLE {child}(id INT PRIMARY KEY, legacy_ref INT, CONSTRAINT fk_{child} FOREIGN KEY(legacy_ref) REFERENCES {parent}(legacy)) ENGINE=InnoDB")).unwrap();
    let imported = run("dump.import", json!({"target":endpoint,"input_dir":output,"mode":"replace","threads":1}));
    let legacy = conn.query_first::<u64,_>(format!("SELECT legacy FROM {parent} WHERE id=1"));
    conn.query_drop(format!("DROP TABLE {child}")).unwrap();
    conn.query_drop(format!("DROP TABLE IF EXISTS {parent}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
    assert!(!success(&imported));
    assert_eq!(legacy.ok().flatten(), Some(7), "the parent was replaced before the refusal");
    let error = imported.iter().find(|event| event["event"] == "error").unwrap();
    assert!(error["message"].as_str().unwrap().contains("incompatible_surviving_fk"), "{error}");
}
