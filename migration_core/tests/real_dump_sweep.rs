//! Local pre-release sweep of Export/Import over a real dump. The dump stays on the machine; it
//! never goes into the repository or CI (CI runs the anonymized `live_real_schema` instead).
//! Copy the dump to a scratch folder (the import writes its report there), then for example:
//!
//! ```text
//! TF_SWEEP_MYSQL_HOST=tf-mysql TF_SWEEP_DUMP=/sweep/full TF_SWEEP_LABEL=my84 TF_SWEEP_FULL=1 \
//!   cargo test --release --test real_dump_sweep -- --nocapture
//! ```
//!
//! It prints one `SWEEP` line per scenario and does not fail on the first problem, so one run
//! shows everything: fresh replace, replace with target-only children, database-scoped account,
//! safe restore and switch, export and re-import (with per-table checksums when TF_SWEEP_FULL is set),
//! and the same dump into PostgreSQL when TF_SWEEP_PG_HOST is set.
use migration_core::{handle_request, Endpoint, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::time::Instant;

fn run(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request { command: command.into(), request_id: None, payload })
}

fn result(events: &[Value]) -> Option<&Value> {
    events.iter().rev().find(|event| event["event"] == "result")
}

fn report(name: &str, started: Instant, events: &[Value]) -> bool {
    let error = events.iter().find(|event| event["event"] == "error").map(|event| event["message"].as_str().unwrap_or("").to_string());
    let ok = error.is_none() && result(events).is_some_and(|value| value["success"] == true);
    let detail = match (&error, result(events)) {
        (Some(error), _) => error.chars().take(1500).collect::<String>(),
        (None, Some(value)) => format!("status={} rows={} views_failed={} error={}", value["status"], value["rows_imported"], value["views_failed"], value["error"]),
        _ => "no result".into(),
    };
    println!("SWEEP {name} ok={ok} secs={:.0} {detail}", started.elapsed().as_secs_f64());
    ok
}

fn tag() -> u128 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos() % 1_000_000_000
}

fn import(target: &Endpoint, dir: &str, mode: &str) -> Vec<Value> {
    run("dump.import", json!({"target": target, "input_dir": dir, "mode": mode, "threads": 4, "strict_manifest": true, "use_source_timezone": true}))
}

#[test]
fn real_dump_sweep_mysql() {
    let Ok(host) = std::env::var("TF_SWEEP_MYSQL_HOST") else { return };
    let dir = std::env::var("TF_SWEEP_DUMP").unwrap();
    let label = std::env::var("TF_SWEEP_LABEL").unwrap_or_default();
    let full = std::env::var("TF_SWEEP_FULL").is_ok();
    let tag = tag();
    let (a, b, user) = (format!("tf_sweep_a_{tag}"), format!("tf_sweep_b_{tag}"), format!("tf_sweep_u_{tag}"));
    let mut root = mysql::Pool::new(mysql::OptsBuilder::new().ip_or_hostname(Some(&host)).user(Some("root")).pass(Some("tf_local_test")).db_name(Some("tf_test")))
        .unwrap().get_conn().unwrap();
    let before: Vec<String> = root.query("SHOW DATABASES").unwrap();
    let ep = |db: &str, user: &str| Endpoint { engine: "mysql".into(), host: host.clone(), port: 3306, user: user.into(), password: "tf_local_test".into(), database: db.into(), schema: None, tls: Default::default() };
    root.query_drop(format!("CREATE DATABASE {a}")).unwrap();

    if report(&format!("{label} fresh_replace"), Instant::now(), &import(&ep(&a, "root"), &dir, "replace")) {
        // Target-only children on single-column primary keys, typed like the parent.
        let parents: Vec<(String, String, String, Option<String>, Option<String>)> = root.exec(
            "SELECT k.TABLE_NAME, k.COLUMN_NAME, c.COLUMN_TYPE, c.CHARACTER_SET_NAME, c.COLLATION_NAME FROM information_schema.KEY_COLUMN_USAGE k \
             JOIN information_schema.COLUMNS c ON c.TABLE_SCHEMA=k.TABLE_SCHEMA AND c.TABLE_NAME=k.TABLE_NAME AND c.COLUMN_NAME=k.COLUMN_NAME \
             WHERE k.TABLE_SCHEMA=? AND k.CONSTRAINT_NAME='PRIMARY' AND k.TABLE_NAME IN (SELECT TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE \
             WHERE TABLE_SCHEMA=? AND CONSTRAINT_NAME='PRIMARY' GROUP BY TABLE_NAME HAVING COUNT(*)=1) ORDER BY k.TABLE_NAME LIMIT 60",
            (&a, &a)).unwrap();
        for (index, (table, column, column_type, charset, collation)) in parents.iter().enumerate() {
            let text = match (charset, collation) { (Some(cs), Some(co)) => format!(" CHARACTER SET {cs} COLLATE {co}"), _ => String::new() };
            root.query_drop(format!("CREATE TABLE {a}.zz_child_{index} (id INT PRIMARY KEY, ref {column_type}{text} NULL, CONSTRAINT zz_fk_{index} FOREIGN KEY (ref) REFERENCES {a}.`{table}` (`{column}`))")).unwrap();
        }
        report(&format!("{label} replace_with_{}_target_only_children", parents.len()), Instant::now(), &import(&ep(&a, "root"), &dir, "replace"));
        let kept: u64 = root.exec_first("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=? AND CONSTRAINT_NAME LIKE 'zz\\_fk\\_%'", (&a,)).unwrap().unwrap();
        println!("SWEEP {label} target_only_child_fks_kept={kept}/{}", parents.len());

        root.query_drop(format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
        root.query_drop(format!("GRANT ALL ON {a}.* TO '{user}'@'%'")).unwrap();
        report(&format!("{label} scoped_account_replace (views owned by root are expected to be reported)"), Instant::now(), &import(&ep(&a, &user), &dir, "replace"));

        let safe = import(&ep(&a, "root"), &dir, "safe");
        if report(&format!("{label} safe_restore"), Instant::now(), &safe) {
            let value = result(&safe).unwrap().clone();
            let promote = |action: &str, extra: Value| {
                let mut payload = json!({"action": action, "restore_id": value["restore_id"], "report_path": value["report_path"], "target": ep(&a, "root")});
                payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
                run("dump.promote", payload)
            };
            let plan = promote("plan", json!({}));
            if report(&format!("{label} promote_plan"), Instant::now(), &plan) {
                let digest = result(&plan).unwrap()["plan_digest"].clone();
                report(&format!("{label} promote_confirm"), Instant::now(), &promote("confirm", json!({"plan_digest": digest, "overwrite_confirmed": true})));
            }
        }

        let out = std::env::temp_dir().join(format!("tf_sweep_out_{tag}"));
        let exported = run("dump.run", json!({"source": ep(&a, "root"), "output_dir": out, "data_format": "tsv", "compression": "zstd", "threads": 4}));
        if report(&format!("{label} export_default"), Instant::now(), &exported) {
            root.query_drop(format!("CREATE DATABASE {b}")).unwrap();
            if report(&format!("{label} reimport_into_empty"), Instant::now(), &import(&ep(&b, "root"), out.to_str().unwrap(), "replace")) && full {
                let tables: Vec<String> = root.exec("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_TYPE='BASE TABLE' AND TABLE_NAME NOT LIKE 'zz\\_child\\_%'", (&a,)).unwrap();
                let mismatched = tables.iter().filter(|table| {
                    let left: Option<(String, Option<u64>)> = root.query_first(format!("CHECKSUM TABLE {a}.`{table}`")).unwrap();
                    let right: Option<(String, Option<u64>)> = root.query_first(format!("CHECKSUM TABLE {b}.`{table}`")).unwrap();
                    left.map(|value| value.1) != right.map(|value| value.1)
                }).cloned().collect::<Vec<_>>();
                println!("SWEEP {label} checksum_mismatches={} {:?}", mismatched.len(), mismatched.iter().take(20).collect::<Vec<_>>());
            }
        }
        let _ = std::fs::remove_dir_all(out);
    }

    // Only this sweep's databases and the namespaces its safe restore created.
    let after: Vec<String> = root.query("SHOW DATABASES").unwrap();
    for name in after.iter().filter(|name| !before.contains(name)) {
        if *name == a || *name == b || ["tf_restore_", "tf_backup_", "tf_promote_"].iter().any(|prefix| name.starts_with(prefix)) {
            root.query_drop(format!("DROP DATABASE IF EXISTS `{name}`")).unwrap();
        }
    }
    let _ = root.query_drop(format!("DROP USER IF EXISTS '{user}'@'%'"));
}

#[test]
fn real_dump_sweep_postgres() {
    let Ok(host) = std::env::var("TF_SWEEP_PG_HOST") else { return };
    let dir = std::env::var("TF_SWEEP_DUMP").unwrap();
    let label = std::env::var("TF_SWEEP_LABEL").unwrap_or_default();
    let schema = format!("tf_sweep_{}", tag());
    let mut admin = postgres::Config::new().host(&host).user("postgres").password("tf_local_test").dbname("tf_test").connect(postgres::NoTls).unwrap();
    admin.batch_execute(&format!("CREATE SCHEMA {schema}")).unwrap();
    let ep = Endpoint { engine: "postgresql".into(), host: host.clone(), port: 5432, user: "postgres".into(), password: "tf_local_test".into(), database: "tf_test".into(), schema: Some(schema.clone()), tls: Default::default() };
    if report(&format!("{label} pg_fresh_replace"), Instant::now(), &import(&ep, &dir, "replace")) {
        report(&format!("{label} pg_second_replace"), Instant::now(), &import(&ep, &dir, "replace"));
    }
    admin.batch_execute(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
}
