//! Export/Import over the shape of a real production schema (311 tables, 11 views), anonymized
//! with `scripts/anonymize_dump_schema.py`. Hand-made test schemas missed every Export/Import bug
//! users hit on 2026-10-13; this fixture carries the real mix of AUTO_INCREMENT keys, collations,
//! composite keys, FKs, CHECKs and views. Schema only: every table has zero rows.
use migration_core::{handle_request, Endpoint, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

const FIXTURE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/real_schema/_tunnelforge_dump.json");

fn run(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request { command: command.into(), request_id: None, payload })
}

fn result(events: &[Value]) -> &Value {
    events.iter().rev().find(|event| event["event"] == "result" || event["event"] == "error").unwrap()
}

fn assert_clean(events: &[Value], step: &str) {
    let last = result(events);
    assert!(last["event"] == "result" && last["success"] == true, "{step}: {last}");
    assert_eq!(last["views_failed"].as_array().map_or(0, Vec::len), 0, "{step}: {last}");
}

fn unique(prefix: &str) -> String {
    format!("{prefix}_{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos() % 1_000_000_000_000)
}

/// The import writes its report next to the dump, so each run gets its own copy.
fn fixture_copy() -> PathBuf {
    let dir = std::env::temp_dir().join(unique("tf_real_schema"));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::copy(FIXTURE, dir.join("_tunnelforge_dump.json")).unwrap();
    dir
}

fn import(target: &Endpoint, dir: &Path, mode: &str) -> Vec<Value> {
    run("dump.import", json!({"target": target, "input_dir": dir, "mode": mode, "threads": 4, "strict_manifest": true, "use_source_timezone": true}))
}

fn mysql_port() -> u16 {
    std::env::var("TF_MYSQL_PORT").ok().and_then(|port| port.parse().ok()).unwrap_or(3306)
}

fn mysql(host: &str, user: &str, database: &str) -> Endpoint {
    Endpoint { engine: "mysql".into(), host: host.into(), port: mysql_port(), user: user.into(), password: "tf_local_test".into(), database: database.into(), schema: None, tls: Default::default() }
}

fn connect(host: &str) -> mysql::PooledConn {
    mysql::Pool::new(mysql::OptsBuilder::new().ip_or_hostname(Some(host)).tcp_port(mysql_port()).user(Some("root")).pass(Some("tf_local_test")).db_name(Some("tf_test")))
        .unwrap().get_conn().unwrap()
}

#[test]
fn mysql_real_schema_replace_overwrite_children_safe_restore_and_reexport() {
    let Ok(host) = std::env::var("TF_MYSQL_HOST") else { return };
    let mut root = connect(&host);
    let before: Vec<String> = root.query("SHOW DATABASES").unwrap();
    let (db, copy) = (unique("tf_real"), unique("tf_real_copy"));
    root.query_drop(format!("CREATE DATABASE {db}")).unwrap();
    let dir = fixture_copy();
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        assert_clean(&import(&mysql(&host, "root", &db), &dir, "replace"), "fresh replace");

        // Safe restore into the existing database, then switch.
        let safe = import(&mysql(&host, "root", &db), &dir, "safe");
        let restored = result(&safe).clone();
        assert!(restored["success"] == true && restored["verified"] == true, "safe restore: {restored}");
        let promote = |action: &str, extra: Value| {
            let mut payload = json!({"action": action, "restore_id": restored["restore_id"], "report_path": restored["report_path"], "target": mysql(&host, "root", &db)});
            payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
            run("dump.promote", payload)
        };
        let plan = promote("plan", json!({}));
        let plan = result(&plan).clone();
        assert!(plan["success"] == true && plan["can_promote"] == true, "promote plan: {plan}");
        let confirm = promote("confirm", json!({"plan_digest": plan["plan_digest"], "overwrite_confirmed": true}));
        assert_eq!(result(&confirm)["status"], "promoted", "{}", result(&confirm));

        // Target-only children on every single-column primary key, typed like the parent.
        let parents: Vec<(String, String, String, Option<String>, Option<String>)> = root.exec(
            "SELECT k.TABLE_NAME, k.COLUMN_NAME, c.COLUMN_TYPE, c.CHARACTER_SET_NAME, c.COLLATION_NAME \
             FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.COLUMNS c \
               ON c.TABLE_SCHEMA=k.TABLE_SCHEMA AND c.TABLE_NAME=k.TABLE_NAME AND c.COLUMN_NAME=k.COLUMN_NAME \
             WHERE k.TABLE_SCHEMA=? AND k.CONSTRAINT_NAME='PRIMARY' AND k.TABLE_NAME IN \
               (SELECT TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=? AND CONSTRAINT_NAME='PRIMARY' GROUP BY TABLE_NAME HAVING COUNT(*)=1)",
            (&db, &db)).unwrap();
        assert!(parents.len() > 200, "fixture should have hundreds of single-key tables");
        for (index, (table, column, column_type, charset, collation)) in parents.iter().enumerate() {
            let text = match (charset, collation) { (Some(cs), Some(co)) => format!(" CHARACTER SET {cs} COLLATE {co}"), _ => String::new() };
            root.query_drop(format!("CREATE TABLE {db}.zz_child_{index} (id INT PRIMARY KEY, ref {column_type}{text} NULL, CONSTRAINT zz_fk_{index} FOREIGN KEY (ref) REFERENCES {db}.`{table}` (`{column}`))")).unwrap();
        }
        assert_clean(&import(&mysql(&host, "root", &db), &dir, "replace"), "replace with target-only children");
        let kept: u64 = root.exec_first("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=? AND CONSTRAINT_NAME LIKE 'zz\\_fk\\_%'", (&db,)).unwrap().unwrap();
        assert_eq!(kept as usize, parents.len(), "target-only children must keep their FKs");

        // The children are linked to the dump, so a safe restore stops before making a copy.
        let linked = import(&mysql(&host, "root", &db), &dir, "safe");
        let linked = result(&linked).clone();
        assert!(linked["status"] == "failed_original_untouched" && linked["candidate_created"] == false, "linked safe restore: {linked}");
        let stops = |code: &str, prefix: &str| linked["copy_blockers"].as_array().into_iter().flatten()
            .filter(|item| item["code"] == code && item["table"].as_str().is_some_and(|name| name.starts_with(prefix))).count();
        assert_eq!(stops("linked_target_only", "zz_child_"), parents.len(), "{linked}");
        // The first switch kept the old view definitions under alias names; they stop it too.
        assert!(stops("saved_view_alias", "tf_p_") > 0, "{linked}");

        // Export what was imported and load it into an empty database.
        let out = std::env::temp_dir().join(unique("tf_real_export"));
        let exported = run("dump.run", json!({"source": mysql(&host, "root", &db), "output_dir": out, "threads": 4}));
        assert!(result(&exported)["success"] == true, "export: {}", result(&exported));
        root.query_drop(format!("CREATE DATABASE {copy}")).unwrap();
        assert_clean(&import(&mysql(&host, "root", &copy), &out, "replace"), "re-import of the export");
        let _ = std::fs::remove_dir_all(out);
    }));
    // Only this test's databases and the restore/backup namespaces safe restore created for it.
    let after: Vec<String> = root.query("SHOW DATABASES").unwrap();
    let ours = |name: &str| name == db || name == copy || ["tf_restore_", "tf_backup_", "tf_promote_"].iter().any(|prefix| name.starts_with(prefix));
    for name in after.iter().filter(|name| !before.contains(name) && ours(name)) {
        root.query_drop(format!("DROP DATABASE IF EXISTS `{name}`")).unwrap();
    }
    let _ = std::fs::remove_dir_all(dir);
    if let Err(panic) = outcome { std::panic::resume_unwind(panic); }
}

#[test]
fn mysql_real_schema_replace_twice_with_a_database_scoped_account() {
    let Ok(host) = std::env::var("TF_MYSQL_HOST") else { return };
    let mut root = connect(&host);
    let (db, user) = (unique("tf_real_scoped"), "tf_real_scoped");
    for sql in [
        format!("DROP USER IF EXISTS '{user}'@'%'"),
        format!("CREATE DATABASE {db}"),
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT ALL ON {db}.* TO '{user}'@'%'"),
    ] { root.query_drop(sql).unwrap(); }
    let dir = fixture_copy();
    let first = import(&mysql(&host, user, &db), &dir, "replace");
    let second = import(&mysql(&host, user, &db), &dir, "replace");
    root.query_drop(format!("DROP DATABASE {db}")).unwrap();
    root.query_drop(format!("DROP USER '{user}'@'%'")).unwrap();
    let _ = std::fs::remove_dir_all(dir);
    assert_clean(&first, "scoped fresh replace");
    assert_clean(&second, "scoped overwrite");
}

#[test]
fn postgres_real_schema_lists_every_table_it_cannot_take_before_any_change() {
    let Ok(host) = std::env::var("TF_POSTGRES_HOST") else { return };
    let schema = unique("tf_real_pg");
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}")).unwrap();
    let target = Endpoint { engine: "postgresql".into(), host: host.clone(), port: 5432, user: "postgres".into(), password: "tf_local_test".into(), database: "tf_test".into(), schema: Some(schema.clone()), tls: Default::default() };
    let dir = fixture_copy();
    let imported = import(&target, &dir, "replace");
    let tables: i64 = admin.query_one("SELECT COUNT(*) FROM pg_tables WHERE schemaname=$1", &[&schema]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    let _ = std::fs::remove_dir_all(dir);
    let error = result(&imported)["message"].as_str().unwrap_or_default().to_string();
    // 2 MySQL ON UPDATE columns and 4 MySQL CHECK expressions have no PostgreSQL translation yet.
    assert!(error.contains("6 tables cannot be created on postgresql"), "{error}");
    assert!(error.contains("ON UPDATE") && error.contains("CHECK"), "{error}");
    assert_eq!(tables, 0, "nothing may be created before the refusal");
}
