use migration_core::{handle_request, inspect_live, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};

fn request(command: &str, payload: Value) -> Value {
    let events = handle_request(Request { command: command.into(), request_id: None, payload });
    assert!(!events.iter().any(|event| event["event"] == "error"), "{events:#?}");
    events.into_iter().find(|event| event["event"] == "result").unwrap()
}

#[test]
fn foreign_key_actions_and_composite_columns_survive_dump_import_when_env_is_configured() {
    for (prefix, engine, port) in [("TF_MYSQL", "mysql", 3306), ("TF_POSTGRES", "postgresql", 5432)] {
        let Ok(host) = std::env::var(format!("{prefix}_HOST")) else { continue };
        let endpoint = Endpoint {
            engine: engine.into(), host,
            port: std::env::var(format!("{prefix}_PORT")).ok().and_then(|s| s.parse().ok()).unwrap_or(port),
            user: std::env::var(format!("{prefix}_USER")).unwrap(),
            password: std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),
            database: std::env::var(format!("{prefix}_DATABASE")).unwrap(), schema: None,
        };
        let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let parent = format!("tf_fk_p_{suffix}");
        let child = format!("tf_fk_c_{suffix}");
        let mut adapter = LiveAdapter::connect(&endpoint).unwrap();
        adapter.execute_sql(&format!("CREATE TABLE {parent} (a INTEGER, b INTEGER, PRIMARY KEY (a,b))")).unwrap();
        adapter.execute_sql(&format!("CREATE TABLE {child} (id INTEGER PRIMARY KEY, pa INTEGER, pb INTEGER, CONSTRAINT fk_{suffix} FOREIGN KEY (pa,pb) REFERENCES {parent} (a,b) ON DELETE CASCADE ON UPDATE SET NULL)")).unwrap();
        adapter.execute_sql(&format!("INSERT INTO {parent} VALUES (1,2),(3,4)")).unwrap();
        adapter.execute_sql(&format!("INSERT INTO {child} VALUES (1,1,2),(2,3,4)")).unwrap();
        let inspected = inspect_live(&endpoint).unwrap();
        let fk = &inspected.schema.tables.iter().find(|t| t.name == child).unwrap().foreign_keys[0];
        assert_eq!(fk.columns, ["pa", "pb"]);
        assert_eq!(fk.referenced_columns, ["a", "b"]);
        assert_eq!(fk.on_delete.as_ref().unwrap().as_sql(), "CASCADE");
        assert_eq!(fk.on_update.as_ref().unwrap().as_sql(), "SET NULL");
        let dir = std::env::temp_dir().join(format!("tf-fk-actions-{suffix}"));
        request("dump.run", json!({"source": endpoint, "tables": [parent, child], "output_dir": dir, "data_format": "jsonl", "threads": 1}));
        request("dump.import", json!({"target": endpoint, "input_dir": dir, "mode": "replace", "threads": 1}));
        adapter.execute_sql(&format!("DELETE FROM {parent} WHERE a=1")).unwrap();
        assert_eq!(adapter.row_count(&child).unwrap(), 1, "{engine} ON DELETE CASCADE");
        adapter.execute_sql(&format!("UPDATE {parent} SET a=5 WHERE a=3")).unwrap();
        let rows = request("query.execute", json!({"connection": endpoint, "sql": format!("SELECT pa,pb FROM {child} WHERE id=2")}));
        assert_eq!(rows["rows"], json!([{"pa": null, "pb": null}]), "{engine} ON UPDATE SET NULL");
        adapter.execute_sql(&format!("DROP TABLE {child}")).unwrap();
        adapter.execute_sql(&format!("DROP TABLE {parent}")).unwrap();
        std::fs::remove_dir_all(dir).unwrap();
    }
}
