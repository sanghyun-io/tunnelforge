//! TF-STATUS-119 live verification: promotion -> list -> reconcile -> cleanup, and
//! the cases where cleanup must refuse. Opt-in (`#[ignore]`); needs disposable
//! `TF_MYSQL_HOST` / `TF_POSTGRES_HOST` tf_test databases (root / postgres).

use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

fn endpoints() -> Vec<Endpoint> {
    [("mysql", "TF_MYSQL_HOST", 3306, "root"), ("postgresql", "TF_POSTGRES_HOST", 5432, "postgres")]
        .into_iter()
        .map(|(engine, host, port, user)| Endpoint {
            engine: engine.into(),
            host: std::env::var(host).expect("live database host required"),
            port,
            user: user.into(),
            password: "tf_local_test".into(),
            database: "tf_test".into(),
            schema: None,
            tls: Default::default(),
        })
        .filter(|endpoint| std::env::var("TF_EXPORT_ENGINE").map(|engine| engine == endpoint.engine).unwrap_or(true))
        .collect()
}

fn unique(prefix: &str) -> String {
    format!("{prefix}_{}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos())
}

fn call(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request { command: command.into(), request_id: None, payload })
}

fn result(events: Vec<Value>) -> Value {
    if let Some(error) = events.iter().find(|e| e["event"] == "error") {
        panic!("unexpected error: {error}");
    }
    events.into_iter().find(|e| e["event"] == "result").expect("result event")
}

fn error(events: Vec<Value>) -> String {
    events.iter().find(|e| e["event"] == "error").map(|e| e["message"].as_str().unwrap_or_default().to_string()).unwrap_or_else(|| panic!("expected an error: {events:?}"))
}

fn backups(original: &Endpoint, dir: &PathBuf, action: &str, extra: Value) -> Vec<Value> {
    let mut payload = json!({"endpoint": original, "action": action, "input_dirs": [dir]});
    for (key, value) in extra.as_object().unwrap() {
        payload[key.as_str()] = value.clone();
    }
    call("restore.backups", payload)
}

struct Fixture {
    engine: String,
    admin: LiveAdapter,
    original: Endpoint,
    name: String,
    dir: PathBuf,
    restore_id: String,
    report_path: PathBuf,
}

impl Fixture {
    fn mysql(&self) -> bool {
        self.engine == "mysql"
    }

    fn drop_ns(&mut self, namespace: &str) {
        let sql = if self.mysql() { format!("DROP DATABASE IF EXISTS {namespace}") } else { format!("DROP SCHEMA IF EXISTS {namespace} CASCADE") };
        self.admin.execute_sql(&sql).unwrap();
    }

    fn create_ns(&mut self, namespace: &str) {
        let sql = if self.mysql() { format!("CREATE DATABASE {namespace}") } else { format!("CREATE SCHEMA {namespace}") };
        self.admin.execute_sql(&sql).unwrap();
    }

    fn scalar(&self, sql: &str) -> String {
        let evs = call("query.execute", json!({"connection": self.original, "sql": sql}));
        let rows = result(evs)["rows"].clone();
        rows[0].as_object().unwrap().values().next().unwrap().to_string().trim_matches('"').to_string()
    }

    fn exists(&self, namespace: &str) -> bool {
        let sql = if self.mysql() {
            format!("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{namespace}'")
        } else {
            format!("SELECT COUNT(*) FROM pg_namespace WHERE nspname='{namespace}'")
        };
        self.scalar(&sql) == "1"
    }

    fn list(&self) -> Value {
        result(backups(&self.original, &self.dir, "list", json!({})))
    }

    fn backup_namespace(&self) -> String {
        let listed = self.list();
        let entry = listed["backups"].as_array().unwrap().iter().find(|b| b["restore_id"] == self.restore_id.as_str()).unwrap_or_else(|| panic!("{listed}"));
        entry["backup"]["namespace"].as_str().unwrap().to_string()
    }

    fn plan_dir(&self) -> PathBuf {
        self.dir.join(format!(".tunnelforge_promotion_plan_{}", self.restore_id))
    }

    fn cleanup_plan(&self, target: &str) -> Value {
        result(backups(&self.original, &self.dir, "cleanup_plan", json!({"restore_id": self.restore_id, "target": target})))
    }

    fn finish(mut self) {
        let names: Vec<String> = ["backup", "candidate"].iter().filter_map(|_| None::<String>).collect();
        let _ = names;
        let listed = self.list();
        let mut namespaces = vec![self.name.clone(), format!("tf_restore_{}", self.restore_id)];
        for entry in listed["backups"].as_array().unwrap() {
            for key in ["backup", "displaced", "clone"] {
                if let Some(ns) = entry[key]["namespace"].as_str() {
                    namespaces.push(ns.to_string());
                }
            }
        }
        for namespace in namespaces {
            self.drop_ns(&namespace);
        }
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Original namespace with two tables and a view, exported, restored safely into a
/// candidate, then promoted (unless `promote` is false).
fn fixture(base: &Endpoint, promote: bool) -> Fixture {
    fixture_with(base, promote, true, false)
}

/// `view`: the original also has a view; `target_only`: a table outside the dump that
/// references `parents` exists in the original (it is preserved by the promotion).
fn fixture_with(base: &Endpoint, promote: bool, view: bool, target_only: bool) -> Fixture {
    let engine = base.engine.clone();
    let name = unique("tf_life");
    let mut admin = LiveAdapter::connect(base).unwrap();
    let mysql = engine == "mysql";
    admin.execute_sql(&if mysql { format!("CREATE DATABASE {name}") } else { format!("CREATE SCHEMA {name}") }).unwrap();
    let mut original = base.clone();
    if mysql {
        original.database = name.clone();
    } else {
        original.schema = Some(name.clone());
    }
    let mut old = LiveAdapter::connect(&original).unwrap();
    old.execute_sql("CREATE TABLE parents(id INT PRIMARY KEY, value VARCHAR(40))").unwrap();
    old.execute_sql("CREATE TABLE children(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_parent FOREIGN KEY(parent_id) REFERENCES parents(id))").unwrap();
    old.execute_sql("INSERT INTO parents VALUES(1,'backup'),(2,'backup2')").unwrap();
    old.execute_sql("INSERT INTO children VALUES(1,1)").unwrap();
    if view {
        old.execute_sql("CREATE VIEW parent_view AS SELECT id,value FROM parents").unwrap();
    }
    let dir = std::env::temp_dir().join(unique("tf_life_dump"));
    let exported = call("dump.run", json!({"source": original, "output_dir": dir, "data_format": "jsonl", "compression": "none", "threads": 1, "mysql_snapshot_mode": "single_connection"}));
    let _ = result(exported);
    old.execute_sql("UPDATE parents SET value='original-live'").unwrap();
    if target_only {
        old.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_notes FOREIGN KEY(parent_id) REFERENCES parents(id))").unwrap();
        old.execute_sql("INSERT INTO notes VALUES(1,1),(2,2)").unwrap();
    }
    let restored = result(call("dump.import", json!({"connection": original, "target": original, "input_dir": dir, "mode": "safe", "threads": 1})));
    assert_eq!(restored["success"], true, "{restored}");
    let restore_id = restored["restore_id"].as_str().unwrap().to_string();
    let report_path = dir.join("_tunnelforge_import_report.json");
    if promote {
        let planned = result(call("dump.promote", json!({"endpoint": original, "action": "plan", "report_path": report_path, "restore_id": restore_id})));
        assert_eq!(planned["can_promote"], true, "{planned}");
        let done = result(call("dump.promote", json!({"endpoint": original, "action": "confirm", "overwrite_confirmed": true,
            "report_path": report_path, "restore_id": restore_id, "plan_digest": planned["plan_digest"]})));
        assert_eq!(done["status"], "promoted", "{done}");
    }
    Fixture { engine, admin, original, name, dir, restore_id, report_path }
}

fn blockers(plan: &Value) -> String {
    plan["blockers"].to_string()
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn promoted_backup_is_listed_reconciled_and_cleaned_only_after_confirmation() {
    for base in endpoints() {
        let fx = fixture(&base, true);
        let listed = fx.list();
        let entry = listed["backups"].as_array().unwrap().iter().find(|b| b["restore_id"] == fx.restore_id.as_str()).unwrap().clone();
        assert_eq!(entry["journal_status"], "promoted", "{entry}");
        assert_eq!(entry["backup"]["ownership"], "proven", "{entry}");
        assert_eq!(entry["backup"]["contents_exact"], true, "{entry}");
        assert_eq!(entry["backup"]["verdict"], "promoted", "{entry}");
        assert_eq!(entry["backup"]["tables"].as_array().unwrap().len(), 2, "{entry}");
        if fx.mysql() {
            assert!(entry["backup"]["saved_view_alias_note"].as_str().unwrap().contains("not views over backup data"));
        }
        let backup = entry["backup"]["namespace"].as_str().unwrap().to_string();

        let reconciled = result(backups(&fx.original, &fx.dir, "reconcile", json!({"restore_id": fx.restore_id})));
        assert_eq!(reconciled["conclusion"], "promoted", "{reconciled}");
        assert_eq!(reconciled["retry_allowed"], false);

        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], true, "{}: {plan}", fx.engine);
        assert_eq!(plan["will_delete"], json!([backup]));
        let total: u64 = plan["tables"].as_array().unwrap().iter().map(|t| t["rows"].as_u64().unwrap()).sum();
        assert_eq!(total, 3, "{plan}");

        // Confirmation and a matching reviewed plan are both mandatory.
        let missing = error(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "plan_digest": plan["plan_digest"]})));
        assert!(missing.contains("confirmation"), "{missing}");
        let stale = error(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "plan_digest": "0000", "confirmed": true})));
        assert!(stale.contains("review a fresh plan"), "{stale}");
        assert!(fx.exists(&backup), "backup must survive refused applies");

        let applied = result(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "plan_digest": plan["plan_digest"], "confirmed": true})));
        assert_eq!(applied["status"], "cleaned", "{applied}");
        assert!(!fx.exists(&backup));
        // The promoted (active) data is untouched.
        assert_eq!(fx.scalar("SELECT COUNT(*) FROM parents WHERE value LIKE 'backup%'"), "2");
        assert_eq!(fx.scalar("SELECT COUNT(*) FROM children"), "1");
        // Listing still explains the restore, now without a backup.
        assert_eq!(fx.list()["backups"][0]["backup"]["exists"], false);
        fx.finish();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn cleanup_refuses_external_references_post_promotion_changes_and_lost_ownership() {
    for base in endpoints() {
        // (1) an object outside the backup references it
        let mut fx = fixture(&base, true);
        let backup = fx.backup_namespace();
        let outside = unique("tf_life_ext");
        fx.create_ns(&outside);
        fx.admin.execute_sql(&format!("CREATE TABLE {outside}.refs(id INT PRIMARY KEY, pid INT, CONSTRAINT fk_ext FOREIGN KEY(pid) REFERENCES {backup}.parents(id))")).unwrap();
        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], false, "{plan}");
        assert!(blockers(&plan).contains("foreign_key"), "{}", blockers(&plan));
        let refused = error(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "plan_digest": plan["plan_digest"], "confirmed": true})));
        assert!(refused.contains("blocked"), "{refused}");
        assert!(fx.exists(&backup));
        // Removing the reference makes the same backup eligible again.
        fx.drop_ns(&outside);
        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], true, "{plan}");
        fx.finish();

        // (2) content changed after promotion
        let mut fx = fixture(&base, true);
        let backup = fx.backup_namespace();
        fx.admin.execute_sql(&format!("INSERT INTO {backup}.parents VALUES(99,'late write')")).unwrap();
        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], false, "{plan}");
        assert!(blockers(&plan).contains("changed after promotion"), "{}", blockers(&plan));
        assert!(fx.exists(&backup));
        fx.finish();

        // (3) a same-named table replaced inside the backup: ownership is no longer proven
        let mut fx = fixture(&base, true);
        let backup = fx.backup_namespace();
        fx.admin.execute_sql(&format!("DROP TABLE {backup}.children")).unwrap();
        fx.admin.execute_sql(&format!("CREATE TABLE {backup}.children(id INT PRIMARY KEY, parent_id INT)")).unwrap();
        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], false, "{plan}");
        assert!(fx.exists(&backup));
        fx.finish();

        // (4) an unknown object inside the backup is never swept away
        let mut fx = fixture(&base, true);
        let backup = fx.backup_namespace();
        fx.admin.execute_sql(&format!("CREATE TABLE {backup}.somebody_elses(id INT)")).unwrap();
        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], false, "{plan}");
        assert!(fx.exists(&backup));
        fx.finish();

        // (5) a namespace that only follows the naming convention has no owner
        let mut fx = fixture(&base, true);
        let lookalike = "tf_backup_1234567890";
        fx.create_ns(lookalike);
        let listed = fx.list();
        assert!(listed["unproven_namespaces"].as_array().unwrap().iter().any(|n| n["namespace"] == lookalike), "{listed}");
        let unknown = error(backups(&fx.original, &fx.dir, "cleanup_plan", json!({"restore_id": "not-a-restore", "target": "backup"})));
        assert!(unknown.contains("no journal"), "{unknown}");
        assert!(fx.exists(lookalike));
        fx.drop_ns(lookalike);
        fx.finish();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn unknown_outcome_journal_is_reconciled_against_live_objects() {
    for base in endpoints() {
        // Journal lost its final result, but the cutover did happen.
        let mut fx = fixture(&base, true);
        let attempt = fx.plan_dir().join("attempt").join("_tunnelforge_import_report.json");
        let mut journal: Value = serde_json::from_slice(&std::fs::read(&attempt).unwrap()).unwrap();
        journal["status"] = json!("cutover_unknown");
        journal["original_unchanged"] = Value::Null;
        std::fs::write(&attempt, serde_json::to_vec_pretty(&journal).unwrap()).unwrap();
        let reconciled = result(backups(&fx.original, &fx.dir, "reconcile", json!({"restore_id": fx.restore_id})));
        assert_eq!(reconciled["journal_status"], "cutover_unknown");
        assert_eq!(reconciled["conclusion"], "promoted", "{reconciled}");
        assert_eq!(reconciled["retry_allowed"], false);

        // Live objects that match neither state: undeterminable, cleanup blocked.
        fx.admin.execute_sql(&format!("DROP TABLE {}.children", fx.name)).unwrap();
        fx.admin.execute_sql(&format!("DROP TABLE {}.parents{}", fx.name, if fx.mysql() { "" } else { " CASCADE" })).unwrap();
        let reconciled = result(backups(&fx.original, &fx.dir, "reconcile", json!({"restore_id": fx.restore_id})));
        assert_eq!(reconciled["conclusion"], "undeterminable", "{reconciled}");
        assert_eq!(reconciled["cleanup_allowed"], false);
        let plan = fx.cleanup_plan("backup");
        assert_eq!(plan["can_cleanup"], false, "{plan}");
        assert!(fx.exists(&fx.backup_namespace()));
        fx.finish();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn unpromoted_candidate_is_listed_and_cleanup_never_touches_the_destination_or_original() {
    for base in endpoints() {
        let mut fx = fixture(&base, false);
        let candidate = format!("tf_restore_{}", fx.restore_id);
        let listed = fx.list();
        let entry = &listed["backups"][0];
        assert_eq!(entry["candidate"]["namespace"], candidate.as_str(), "{listed}");
        assert_eq!(entry["candidate"]["exists"], true);
        assert!(entry.get("backup").is_none() || entry["backup"].is_null(), "{entry}");
        let plan = fx.cleanup_plan("candidate");
        assert_eq!(plan["can_cleanup"], true, "{plan}");
        assert_eq!(plan["will_delete"], json!([candidate]));
        // A tampered candidate is not proven to still be the verified copy.
        fx.admin.execute_sql(&format!("INSERT INTO {candidate}.parents VALUES(77,'tamper')")).unwrap();
        let tampered = fx.cleanup_plan("candidate");
        assert_eq!(tampered["can_cleanup"], false, "{tampered}");
        assert!(fx.exists(&candidate));
        fx.admin.execute_sql(&format!("DELETE FROM {candidate}.parents WHERE id=77")).unwrap();
        let plan = fx.cleanup_plan("candidate");
        assert_eq!(plan["can_cleanup"], true, "{plan}");
        let applied = result(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "target": "candidate", "plan_digest": plan["plan_digest"], "confirmed": true})));
        assert_eq!(applied["status"], "cleaned", "{applied}");
        assert!(!fx.exists(&candidate));
        // The live original and its rows are untouched.
        assert!(fx.exists(&fx.name.clone()));
        assert_eq!(fx.scalar("SELECT COUNT(*) FROM parents WHERE value='original-live'"), "2");
        let _ = &fx.report_path;
        fx.finish();
    }
}

fn attempt_path(fx: &Fixture) -> PathBuf {
    fx.plan_dir().join("attempt").join("_tunnelforge_import_report.json")
}

fn rollback_plan(fx: &Fixture) -> Value {
    result(backups(&fx.original, &fx.dir, "rollback_plan", json!({"restore_id": fx.restore_id})))
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn supported_recovery_restores_the_retained_original_and_keeps_the_displaced_tables() {
    for base in endpoints() {
        for target_only in [false, true] {
            let mut fx = fixture_with(&base, true, false, target_only);
            // Promoted state: the candidate content is active, the original values are retained.
            assert_eq!(fx.scalar("SELECT value FROM parents WHERE id=1"), "backup");
            let backup = fx.backup_namespace();
            let plan = rollback_plan(&fx);
            assert_eq!(plan["can_rollback"], true, "{}: {plan}", fx.engine);
            assert!(plan["displace"].as_array().unwrap().iter().any(|t| t["name"] == "parents" && t["rows"] == 2), "{plan}");
            let displaced = plan["displaced_backup"].as_str().unwrap().to_string();
            assert!(!fx.exists(&displaced));

            let missing = error(backups(&fx.original, &fx.dir, "rollback_apply", json!({"restore_id": fx.restore_id, "plan_digest": plan["plan_digest"]})));
            assert!(missing.contains("confirmation"), "{missing}");
            let stale = error(backups(&fx.original, &fx.dir, "rollback_apply", json!({"restore_id": fx.restore_id, "plan_digest": "00", "confirmed": true})));
            assert!(stale.contains("review a fresh plan"), "{stale}");
            assert_eq!(fx.scalar("SELECT value FROM parents WHERE id=1"), "backup", "refused applies must not change anything");

            let done = result(backups(&fx.original, &fx.dir, "rollback_apply", json!({"restore_id": fx.restore_id, "plan_digest": plan["plan_digest"], "confirmed": true})));
            assert_eq!(done["status"], "rolled_back", "{}: {done}", fx.engine);
            assert_eq!(fx.scalar("SELECT value FROM parents WHERE id=1"), "original-live");
            assert_eq!(fx.scalar("SELECT COUNT(*) FROM children"), "1");
            let broken_fk = fx.admin.execute_sql(&format!("INSERT INTO {}.children VALUES(9,999)", fx.name));
            assert!(broken_fk.is_err(), "the restored foreign key must still be enforced");
            if target_only {
                let orphan = fx.admin.execute_sql(&format!("INSERT INTO {}.notes VALUES(9,999)", fx.name));
                assert!(orphan.is_err(), "the target-only table must reference the restored parent");
                assert_eq!(fx.scalar("SELECT COUNT(*) FROM notes"), "2");
            }
            // The tables that were active are retained, owned and listed; nothing was deleted.
            assert!(fx.exists(&displaced));
            let listed = fx.list();
            let entry = listed["backups"].as_array().unwrap().iter().find(|b| b["restore_id"] == fx.restore_id.as_str()).unwrap().clone();
            assert_eq!(entry["rollbacks"][0]["status"], "rolled_back", "{entry}");
            assert_eq!(entry["displaced"]["ownership"], "proven", "{entry}");
            // A second recovery is refused.
            let again = rollback_plan(&fx);
            assert_eq!(again["can_rollback"], false, "{again}");

            // Cleanup of the recovery leftovers works only through the verified plans.
            let shell = fx.cleanup_plan("backup");
            assert_eq!(shell["can_cleanup"], true, "{shell}");
            let displaced_plan = fx.cleanup_plan("displaced");
            assert_eq!(displaced_plan["can_cleanup"], true, "{displaced_plan}");
            assert_eq!(displaced_plan["will_delete"], json!([displaced]));
            let applied = result(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "target": "displaced", "plan_digest": displaced_plan["plan_digest"], "confirmed": true})));
            assert_eq!(applied["status"], "cleaned");
            assert!(!fx.exists(&displaced));
            let applied = result(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "target": "backup", "plan_digest": shell["plan_digest"], "confirmed": true})));
            assert_eq!(applied["status"], "cleaned");
            assert!(!fx.exists(&backup));
            assert_eq!(fx.scalar("SELECT value FROM parents WHERE id=1"), "original-live");
            fx.finish();
        }
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn recovery_is_refused_when_any_proof_fails() {
    for base in endpoints() {
        // (1) writes accepted by the active tables after the promotion would be discarded
        let mut fx = fixture_with(&base, true, false, false);
        fx.admin.execute_sql(&format!("INSERT INTO {}.parents VALUES(50,'new write')", fx.name)).unwrap();
        let plan = rollback_plan(&fx);
        assert_eq!(plan["can_rollback"], false, "{plan}");
        assert!(blockers(&plan).contains("modified after promotion"), "{}", blockers(&plan));
        let refused = error(backups(&fx.original, &fx.dir, "rollback_apply", json!({"restore_id": fx.restore_id, "plan_digest": plan["plan_digest"], "confirmed": true})));
        assert!(refused.contains("blocked"), "{refused}");
        assert_eq!(fx.scalar("SELECT COUNT(*) FROM parents WHERE id=50"), "1");
        fx.finish();

        // (2) the retained backup was modified
        let mut fx = fixture_with(&base, true, false, false);
        let backup = fx.backup_namespace();
        fx.admin.execute_sql(&format!("UPDATE {backup}.parents SET value='tampered'")).unwrap();
        let plan = rollback_plan(&fx);
        assert_eq!(plan["can_rollback"], false, "{plan}");
        assert!(blockers(&plan).contains("changed after promotion"), "{}", blockers(&plan));
        fx.finish();

        // (3) promotions that involved views are not supported
        let fx = fixture_with(&base, true, true, false);
        let plan = rollback_plan(&fx);
        assert_eq!(plan["can_rollback"], false, "{plan}");
        assert!(blockers(&plan).contains("views"), "{}", blockers(&plan));
        fx.finish();

        // (4) a journal without the promotion fingerprints cannot prove anything
        let fx = fixture_with(&base, true, false, false);
        let attempt = attempt_path(&fx);
        let mut journal: Value = serde_json::from_slice(&std::fs::read(&attempt).unwrap()).unwrap();
        journal["result"]["backup_fingerprint"] = Value::Null;
        std::fs::write(&attempt, serde_json::to_vec_pretty(&journal).unwrap()).unwrap();
        let plan = rollback_plan(&fx);
        assert_eq!(plan["can_rollback"], false, "{plan}");
        assert!(blockers(&plan).contains("fingerprint"), "{}", blockers(&plan));
        fx.finish();

        // (5) an unconfirmed outcome (undeterminable live objects) blocks recovery
        let mut fx = fixture_with(&base, true, false, false);
        let attempt = attempt_path(&fx);
        let mut journal: Value = serde_json::from_slice(&std::fs::read(&attempt).unwrap()).unwrap();
        journal["status"] = json!("cutover_unknown");
        std::fs::write(&attempt, serde_json::to_vec_pretty(&journal).unwrap()).unwrap();
        fx.admin.execute_sql(&format!("DROP TABLE {}.children", fx.name)).unwrap();
        fx.admin.execute_sql(&format!("DROP TABLE {}.parents{}", fx.name, if fx.mysql() { "" } else { " CASCADE" })).unwrap();
        let plan = rollback_plan(&fx);
        assert_eq!(plan["can_rollback"], false, "{plan}");
        assert!(blockers(&plan).contains("not confirmed"), "{}", blockers(&plan));
        fx.finish();
    }
}

#[test]
#[ignore = "requires disposable TF_MYSQL_HOST and TF_POSTGRES_HOST tf_test databases"]
fn promoted_candidate_leftovers_are_cleaned_only_when_recorded() {
    for base in endpoints() {
        let mut fx = fixture_with(&base, true, true, false);
        let candidate = format!("tf_restore_{}", fx.restore_id);
        if fx.mysql() {
            // MySQL also leaves the promotion's temporary clone database; it references the candidate.
            let blocked = fx.cleanup_plan("candidate");
            assert_eq!(blocked["can_cleanup"], false, "{blocked}");
            let clone = fx.cleanup_plan("clone");
            assert_eq!(clone["can_cleanup"], true, "{clone}");
            let applied = result(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "target": "clone", "plan_digest": clone["plan_digest"], "confirmed": true})));
            assert_eq!(applied["status"], "cleaned", "{applied}");
        }
        let plan = fx.cleanup_plan("candidate");
        // Leftover views recorded in the promotion plan are the candidate's own.
        assert_eq!(plan["can_cleanup"], true, "{}: {plan}", fx.engine);
        // Anything the journal does not record blocks it.
        fx.admin.execute_sql(&format!("CREATE TABLE {candidate}.stray(id INT)")).unwrap();
        let plan = fx.cleanup_plan("candidate");
        assert_eq!(plan["can_cleanup"], false, "{plan}");
        assert!(blockers(&plan).contains("stray"), "{}", blockers(&plan));
        fx.admin.execute_sql(&format!("DROP TABLE {candidate}.stray")).unwrap();
        let plan = fx.cleanup_plan("candidate");
        let applied = result(backups(&fx.original, &fx.dir, "cleanup_apply", json!({"restore_id": fx.restore_id, "target": "candidate", "plan_digest": plan["plan_digest"], "confirmed": true})));
        assert_eq!(applied["status"], "cleaned", "{applied}");
        assert!(!fx.exists(&candidate));
        fx.finish();
    }
}
