use super::*;
fn result(events: Vec<Value>) -> Value { events.into_iter().find(|e|e["event"]=="result" || e["event"]=="error").expect("result or error") }
fn exported() -> Option<(LiveAdapter,Endpoint,String,std::path::PathBuf)> {exported_namespace("mysql")}
fn dump(endpoint: &Endpoint, dir: &std::path::Path) { let r=result(run("dump.run",json!({"source":endpoint,"output_dir":dir,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}))); assert_ne!(r["event"],"error","{r:#}"); }
fn import(endpoint: &Endpoint, dir: &std::path::Path, extra: Value) -> Value {let mut p=json!({"target":endpoint,"input_dir":dir,"mode":"safe","threads":1});p.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());result(run("dump.import",p))}
fn preview(endpoint: &Endpoint, dir: &std::path::Path, extra: Value) -> Value {let mut p=json!({"target":endpoint,"input_dir":dir});p.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());result(run("dump.import_preview",p))}
fn promote(endpoint: &Endpoint, restored: &Value, extra: Value) -> Value {let mut p=json!({"endpoint":endpoint,"report_path":restored["report_path"],"restore_id":restored["restore_id"]});p.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());result(run("dump.promote",p))}
fn candidate_of(r: &Value) -> Option<String> {r["candidate_target"]["database"].as_str().map(str::to_string)}
fn drop_db(admin: &mut LiveAdapter,name: &str) {drop_namespace(admin,"mysql",name);}
fn codes(items: &Value) -> Vec<String> {items.as_array().into_iter().flatten().map(|v|format!("{}:{}",v["code"].as_str().unwrap_or_default(),v["table"].as_str().unwrap_or_default())).collect()}
fn save_json(name: &str,value: &Value) {if let Ok(dir)=std::env::var("TF_ROUND2_EVIDENCE") {std::fs::write(std::path::Path::new(&dir).join(name),serde_json::to_vec_pretty(value).unwrap()).unwrap();}}
struct Users(Vec<String>);
impl Drop for Users {fn drop(&mut self) {if let Some(ep)=endpoint("mysql") {if let Ok(mut admin)=LiveAdapter::connect(&ep) {for name in &self.0 {let _=admin.execute_sql(&format!("DROP USER IF EXISTS '{name}'@'%'"));}}}}}
struct Trust {admin:LiveAdapter,previous:String}
impl Drop for Trust {fn drop(&mut self){let _=self.admin.execute_sql(&format!("SET GLOBAL log_bin_trust_function_creators={}",self.previous));}}
fn trust_guard(admin: &mut LiveAdapter,value:u8) -> Trust {let ep=endpoint("mysql").unwrap();let previous=first_value(&ep,"SELECT CAST(@@GLOBAL.log_bin_trust_function_creators AS CHAR) AS n").as_str().unwrap().to_string();admin.execute_sql(&format!("SET GLOBAL log_bin_trust_function_creators={value}")).unwrap();Trust{admin:LiveAdapter::connect(&ep).unwrap(),previous}}

#[test]
fn rvf_i_a1_rebuild_after_plan_allows_fresh_plan_and_candidate_cleanup() {
    let Some((mut admin, original, name, output)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("UPDATE parents SET value='live'").unwrap();
    let restored = import(&original, &output, json!({}));
    let planned = promote(&original, &restored, json!({"action":"plan"}));
    let id_sql = format!("SELECT CAST(TABLE_ID AS CHAR) AS id FROM information_schema.INNODB_TABLES WHERE NAME='{name}/parents'");
    let before = first_value(&original, &id_sql);
    db.execute_sql("ALTER TABLE parents FORCE").unwrap();
    let after = first_value(&original, &id_sql);
    let done = promote(&original, &restored, json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
    let value = first_value(&original, "SELECT value FROM parents WHERE id=1");
    let again = promote(&original, &restored, json!({"action":"plan"}));
    let rc = result(run("restore.backups", json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":"reconcile"})));
    let cand = result(run("restore.backups", json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":"cleanup_plan","target":"candidate"})));
    let active = output.join(format!(".tunnelforge_promotion_plan_{}", restored["restore_id"].as_str().unwrap())).join("active-attempt").exists();
    for ns in [candidate_of(&restored), done["backup_namespace"].as_str().map(str::to_string), done["clone_database"].as_str().map(str::to_string), Some(name.clone())].into_iter().flatten() { drop_db(&mut admin, &ns); }
    std::fs::remove_dir_all(output).unwrap();
    assert_eq!(planned["can_promote"], true);
    assert_ne!(before, after);
    assert_eq!(value, "live");
    assert_eq!(done["status"], "failed_original_unchanged");
    assert_eq!(done["original_unchanged"],true);
    assert_eq!(again["can_promote"],true,"{again:#}");

    assert!(!active);
    // Old plan identities need not match an externally rebuilt table.
    assert_ne!(rc["conclusion"],"failed_original_triggers_missing");
    assert_eq!(cand["can_cleanup"], true);
}
#[test]
fn rvf_i_b7_only_safe_selected_trigger_preview_warns_definer_change() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TABLE extra(id INT PRIMARY KEY)").unwrap();
    let output = root.join("two");
    dump(&original, &output);
    let n = &name[name.len() - 9..];
    let (app, imp) = (format!("rvf7_app_{n}"), format!("rvf7_imp_{n}"));
    let _users = Users(vec![app.clone(), imp.clone()]);
    for sql in [
        format!("CREATE USER '{app}'@'%' IDENTIFIED BY 'tf_local_test' ACCOUNT LOCK"),
        format!("CREATE USER '{imp}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{imp}'@'%'"),
        format!("GRANT ALL ON `{name}`.* TO '{imp}'@'%'"),
        format!("CREATE DEFINER='{app}'@'%' TRIGGER `{name}`.guard BEFORE INSERT ON `{name}`.parents FOR EACH ROW SET NEW.value=NEW.value"),
        format!("CREATE DEFINER='{app}'@'%' TRIGGER `{name}`.extra_guard BEFORE INSERT ON `{name}`.extra FOR EACH ROW SET NEW.id=NEW.id"),
    ] { admin.execute_sql(&sql).unwrap(); }
    let imp_ep = Endpoint { user: imp.clone(), ..original.clone() };
    let whole = preview(&imp_ep, &output, json!({"mode":"replace"}));
    let partial = preview(&imp_ep, &output, json!({"mode":"replace","tables":["parents"]}));
    let safe_partial=preview(&imp_ep,&output,json!({"mode":"safe","tables":["parents"]}));
    assert!(safe_partial["warnings"].to_string().contains("TRIGGER guard"));
    assert!(!safe_partial["warnings"].to_string().contains("TRIGGER extra_guard"));
    let _replaced = import(&imp_ep, &output, json!({"mode":"replace","tables":["parents"]}));
    let guard_left = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='guard'");
    let warn = |p: &Value| p["warnings"].as_array().into_iter().flatten().filter(|w| w["code"] == "program_restore_definer_may_change").map(|w| w["names"].clone()).collect::<Vec<_>>();
    drop_db(&mut admin, &name);
    std::fs::remove_dir_all(root).unwrap();
    assert!(whole["triggers_dropped"].to_string().contains("guard"));
    assert!(warn(&whole).is_empty());
    assert!(warn(&partial).is_empty());
    assert_eq!(guard_left, "0");
}
#[test]
fn rvf_i_c8_rehearsal_checks_candidate_program_grants() {
    let mut outcomes = Vec::new();
    for (privilege, program) in [("CREATE ROUTINE", "CREATE PROCEDURE p() SELECT 1"), ("TRIGGER", "CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='g'")] {
        let Some((mut admin, original, name, root)) = exported() else { return };
        LiveAdapter::connect(&original).unwrap().execute_sql(program).unwrap();
        let output = root.join("programs");
        dump(&original, &output);
        let user = format!("rvf8_{}", &name[name.len() - 10..]);
        let _users = Users(vec![user.clone()]);
        for sql in [
            format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
            format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'"),
            format!("REVOKE {privilege} ON *.* FROM '{user}'@'%'"),
            format!("GRANT {privilege} ON `{name}`.* TO '{user}'@'%'"),
        ] { admin.execute_sql(&sql).unwrap(); }
        let restricted = Endpoint { user: user.clone(), ..original.clone() };
        let safe = import(&restricted, &output, json!({}));
        let rehearsal = import(&restricted, &output, json!({"verify_only":true}));
        let rc = candidate_of(&rehearsal);
        let left = rc.as_ref().map(|c| first_value(&original, &format!("SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{c}'")));
        for c in [candidate_of(&safe), rc].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
        drop_db(&mut admin, &name);
        std::fs::remove_dir_all(root).unwrap();
        outcomes.push((privilege, safe, rehearsal, left));
    }
    for (privilege, safe, rehearsal, left) in outcomes {
        assert_eq!(safe["error_code"], "copy_refused", "{privilege}");
        assert!(codes(&safe["copy_blockers"]).iter().any(|c| c.ends_with("tf_restore_*")));
        assert_eq!(rehearsal["status"], "failed_original_untouched");
        assert_stopped_before_copy(&rehearsal,privilege);
        assert!(left.is_none());
    }
}
#[test]
fn rvf_i_c10_target_function_binlog_rule_blocks_before_copy() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 2").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP FUNCTION f").unwrap();
    let trust = trust_guard(&mut admin, 1);
    db.execute_sql("CREATE FUNCTION f() RETURNS INT RETURN 1").unwrap();
    admin.execute_sql("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    db.execute_sql("UPDATE parents SET value='live'").unwrap();
    let binlog = first_value(&original, "SELECT CONCAT(@@log_bin,'/',@@log_bin_trust_function_creators) AS n");
    let pre = preview(&original, &output, json!({"mode":"safe"}));
    let restored = import(&original, &output, json!({}));
    assert_stopped_before_copy(&restored,"unsafe target function");
    assert!(codes(&pre["copy_blockers"]).iter().any(|c|c.starts_with("target_routine_binlog_unsafe")));
    let direct=import(&original,&output,json!({"mode":"replace"}));
    assert_eq!(direct["status"],"partial","{direct:#}");
    db.execute_sql("DROP FUNCTION f").unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    let ready=import(&original,&output,json!({}));
    assert_eq!(ready["status"],"ready_for_switch","{ready:#}");
    db.execute_sql("DROP FUNCTION f").unwrap();
    admin.execute_sql("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT RETURN 1").unwrap();
    admin.execute_sql("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    let blocked=promote(&original,&ready,json!({"action":"plan"}));
    assert_eq!(blocked["can_promote"],false,"{blocked:#}");
    assert!(blocked["copy_blockers"].to_string().contains("target_routine_binlog_unsafe"));
    drop_db(&mut admin,candidate_of(&ready).as_deref().unwrap());

    let done=Value::Null;
    let value = first_value(&original, "SELECT value FROM parents WHERE id=1");
    let f = first_value(&original, "SELECT CAST(f() AS CHAR) AS n");
    for ns in [candidate_of(&restored), done["backup_namespace"].as_str().map(str::to_string), done["clone_database"].as_str().map(str::to_string), Some(name.clone())].into_iter().flatten() { drop_db(&mut admin, &ns); }
    drop(trust);
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(binlog, "1/0");
    assert_eq!(value,"dump"); // direct import loaded data but kept the unsafe target function
    assert_eq!(f, "1");
}
#[test]
fn rvf_i_c11_promote_plan_and_confirm_need_no_candidate_program_grants() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let trust = trust_guard(&mut admin, 1);
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=NEW.value").unwrap();
    db.execute_sql("CREATE PROCEDURE p() SELECT 1").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP TRIGGER guard").unwrap();
    db.execute_sql("DROP PROCEDURE p").unwrap();
    let restored = import(&original, &output, json!({}));
    let admin_plan = promote(&original, &restored, json!({"action":"plan"}));
    let op = format!("rvf11_{}", &name[name.len() - 10..]);
    let _users = Users(vec![op.clone()]);
    for sql in [
        format!("CREATE USER '{op}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{op}'@'%'"),
        format!("GRANT ALL ON `{name}`.* TO '{op}'@'%'"),
        format!("GRANT CREATE, DROP, ALTER, INSERT, SELECT, LOCK TABLES ON `tf\\_restore\\_%`.* TO '{op}'@'%'"),
        format!("GRANT CREATE, DROP, ALTER, INSERT, SELECT, LOCK TABLES ON `tf\\_backup\\_%`.* TO '{op}'@'%'"),
        format!("GRANT CREATE, DROP, ALTER, INSERT, SELECT, LOCK TABLES ON `tf\\_promote\\_%`.* TO '{op}'@'%'"),
    ] { admin.execute_sql(&sql).unwrap(); }
    let op_ep = Endpoint { user: op.clone(), ..original.clone() };
    let op_plan = promote(&op_ep, &restored, json!({"action":"plan"}));
    let done=promote(&op_ep,&restored,json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":op_plan["plan_digest"]}));
    for ns in [candidate_of(&restored),done["backup_namespace"].as_str().map(str::to_string),done["clone_database"].as_str().map(str::to_string), Some(name.clone())].into_iter().flatten() { drop_db(&mut admin, &ns); }
    drop(trust);
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(restored["status"], "ready_for_switch");
    assert_eq!(admin_plan["can_promote"], true);
    assert_eq!(op_plan["can_promote"],true,"{op_plan:#}");
    assert_eq!(done["status"],"promoted","{done:#}");
}

#[test]
fn rvf_i_d15_failed_no_change_rollback_result() {
    use mysql::prelude::Queryable;
    let Some((mut admin, original, name, root)) = exported() else { return };
    let restored = import(&original, &root, json!({}));
    let planned = promote(&original, &restored, json!({"action":"plan"}));
    let done = promote(&original, &restored, json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
    let backups = |action: &str, extra: Value| { let mut payload = json!({"endpoint":original,"input_dirs":[root],"restore_id":restored["restore_id"],"action":action}); payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone()); result(run("restore.backups", payload)) };
    let review = backups("rollback_plan", json!({}));
    let LiveAdapter::MySql(mut other) = LiveAdapter::connect(&original).unwrap() else { unreachable!() };
    other.query_drop("START TRANSACTION").unwrap();
    let _: Vec<i64> = other.query("SELECT id FROM parents").unwrap();
    let recovered = backups("rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    other.query_drop("ROLLBACK").unwrap();
    save_json("d15_rollback_failed_no_change.json", &recovered);
    for ns in [candidate_of(&restored),done["backup_namespace"].as_str().map(str::to_string),done["clone_database"].as_str().map(str::to_string),recovered["displaced_backup"].as_str().map(str::to_string),Some(name)] .into_iter().flatten(){drop_db(&mut admin,&ns);}
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(done["status"], "promoted");
    assert_eq!(recovered["status"], "failed_no_change");
}
#[test]
fn rvf_i_g21_rehearsal_with_view_calling_dump_function() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    db.execute_sql("CREATE VIEW v AS SELECT f() AS x").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP VIEW v").unwrap();
    db.execute_sql("DROP FUNCTION f").unwrap();
    let safe = import(&original, &output, json!({}));
    let rehearsal = import(&original, &output, json!({"verify_only":true}));
    for c in [candidate_of(&safe), candidate_of(&rehearsal)].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
    drop_db(&mut admin, &name);
    std::fs::remove_dir_all(root).unwrap();
    assert!(codes(&safe["copy_blockers"]).iter().any(|c| c.starts_with("view_calls_dump_routine")));
    assert_eq!(rehearsal["status"], "failed_original_untouched");
    assert_stopped_before_copy(&rehearsal,"function view rehearsal");
    assert!(codes(&rehearsal["copy_blockers"]).iter().any(|c|c.starts_with("view_calls_dump_routine")));
}
#[test]
fn rvf_i_g22_view_literal_with_escaped_quote_allows_safe_mode() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION fn1() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    db.execute_sql("CREATE VIEW v AS SELECT 'it''s' AS label").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    let manifest: Value = serde_json::from_slice(&std::fs::read(output.join("_tunnelforge_dump.json")).unwrap()).unwrap();
    let definition = manifest["views"][0]["definition"].clone();
    let pre = preview(&original, &output, json!({"mode":"safe"}));
    let safe = import(&original, &output, json!({}));
    for c in [candidate_of(&safe)].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
    drop_db(&mut admin, &name);
    std::fs::remove_dir_all(root).unwrap();
    assert!(definition.as_str().unwrap().contains("'it\\'s'"));
    assert_eq!(safe["status"],"ready_for_switch","{safe:#}");
    assert!(codes(&pre["copy_blockers"]).is_empty(),"{pre:#}");
}

#[test]
fn rvf_i_g23_safe_preview_exposes_direct_import_trigger_conflict() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let trust = trust_guard(&mut admin, 1);
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TABLE children(id INT PRIMARY KEY)").unwrap();
    db.execute_sql("CREATE TRIGGER trg BEFORE INSERT ON children FOR EACH ROW SET NEW.id=NEW.id").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP TRIGGER trg").unwrap();
    db.execute_sql("CREATE TRIGGER trg BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=NEW.value").unwrap();
    let user = format!("rvf23_{}", &name[name.len() - 10..]);
    let _users = Users(vec![user.clone()]);
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW ON *.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `{name}`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_restore\\_%`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_backup\\_%`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_promote\\_%`.* TO '{user}'@'%'"),
    ] { admin.execute_sql(&sql).unwrap(); }
    let ep = Endpoint { user: user.clone(), ..original.clone() };
    let safe = preview(&ep, &output, json!({"mode":"safe"}));
    let replace = preview(&ep, &output, json!({"mode":"replace"}));
    let direct = import(&ep, &output, json!({"mode":"replace"}));
    save_json("g23_safe_preview.json", &safe);
    save_json("g23_replace_preview.json", &replace);
    drop_db(&mut admin, &name);
    drop(trust);
    std::fs::remove_dir_all(root).unwrap();
    assert!(!codes(&safe["blockers"]).iter().any(|c| c.starts_with("trigger_name_conflict")));
    assert!(codes(&safe["copy_blockers"]).iter().any(|c| c.starts_with("metadata_visibility_unproven")));
    assert!(codes(&replace["blockers"]).iter().any(|c| c.starts_with("trigger_name_conflict")));
    assert!(codes(&safe["direct_blockers"]).iter().any(|c|c.starts_with("trigger_name_conflict")));
    assert_ne!(direct["success"], true);
}

#[test]
fn rvf_i_g24_view_column_list_named_like_function_is_allowed() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION monthly_total() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    db.execute_sql("CREATE VIEW monthly_total (a) AS SELECT 1").unwrap();
    db.execute_sql("CREATE VIEW v2 AS WITH monthly_total(m) AS (SELECT 1) SELECT m FROM monthly_total").unwrap();
    let usage = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.VIEW_ROUTINE_USAGE WHERE TABLE_SCHEMA=DATABASE()");
    let output = root.join("programs");
    dump(&original, &output);
    let safe = import(&original, &output, json!({}));
    for c in [candidate_of(&safe)].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
    drop_db(&mut admin, &name);
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(usage, "0");
    assert_eq!(safe["status"],"ready_for_switch","{safe:#}");
}

#[test]
fn rvf_i_i25_privilege_blockers_preview_json() {
    for (privilege, program, file) in [
        ("TRIGGER", "CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='g'", "i25_no_trigger_preview.json"),
        ("CREATE ROUTINE", "CREATE PROCEDURE p() SELECT 1", "i25_no_create_routine_preview.json"),
    ] {
        let Some((mut admin, original, name, root)) = exported() else { return };
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql(program).unwrap();
        let output = root.join("programs");
        dump(&original, &output);
        db.execute_sql(if privilege == "TRIGGER" { "DROP TRIGGER guard" } else { "DROP PROCEDURE p" }).unwrap();
        let user = format!("rvf25_{}", &name[name.len() - 10..]);
        let _users = Users(vec![user.clone()]);
        for sql in [format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"), format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'"), format!("REVOKE {privilege} ON *.* FROM '{user}'@'%'")] { admin.execute_sql(&sql).unwrap(); }
        let ep = Endpoint { user: user.clone(), ..original.clone() };
        let safe = preview(&ep, &output, json!({"mode":"safe"}));
        save_json(file, &safe);
        drop_db(&mut admin, &name);
        std::fs::remove_dir_all(root).unwrap();
        assert!(codes(&safe["copy_blockers"]).iter().any(|c| c.ends_with("tf_restore_*")));
    }
}

#[test]
fn rvf_i_i28_no_trigger_grant_needed_without_any_trigger() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let trust = trust_guard(&mut admin, 0);
    let user = format!("rvf28_{}", &name[name.len() - 10..]);
    let _users = Users(vec![user.clone()]);
    let table_privileges = "SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, LOCK TABLES, REFERENCES, CREATE VIEW, SHOW VIEW";
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),
        format!("GRANT {table_privileges} ON `{name}`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_restore\\_%`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_backup\\_%`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_promote\\_%`.* TO '{user}'@'%'"),
    ] { admin.execute_sql(&sql).unwrap(); }
    let ep = Endpoint { user: user.clone(), ..original.clone() };
    let triggers = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()");
    let pre = preview(&ep, &root, json!({"mode":"safe"}));
    let safe = import(&ep, &root, json!({}));
    save_json("i28_no_trigger_grant_preview.json", &pre);
    for c in [candidate_of(&safe)].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
    drop_db(&mut admin, &name);
    drop(trust);
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(triggers, "0");
    assert_eq!(safe["status"],"ready_for_switch","{safe:#}");
    assert!(codes(&pre["copy_blockers"]).is_empty(),"{pre:#}");
}

#[test]
fn rvf_i_hidden_target_trigger_fails_atomic_rename_without_changing_original() {
    let Some((mut admin,original,name,root))=exported() else{return};
    let user=format!("rvf_hidden_{}",&name[name.len()-8..]); let _users=Users(vec![user.clone()]);
    for sql in [format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),format!("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, LOCK TABLES, REFERENCES, CREATE VIEW, SHOW VIEW ON `{name}`.* TO '{user}'@'%'"),format!("GRANT ALL ON `tf\\_restore\\_%`.* TO '{user}'@'%'"),format!("GRANT ALL ON `tf\\_backup\\_%`.* TO '{user}'@'%'"),format!("GRANT ALL ON `tf\\_promote\\_%`.* TO '{user}'@'%'")]{admin.execute_sql(&sql).unwrap();}
    admin.execute_sql(&format!("CREATE TRIGGER `{name}`.hidden_guard BEFORE INSERT ON `{name}`.parents FOR EACH ROW SET NEW.value='guard'")).unwrap();
    let ep=Endpoint{user,..original.clone()};
    let restored=import(&ep,&root,json!({}));assert_eq!(restored["status"],"ready_for_switch","{restored:#}");
    let planned=promote(&ep,&restored,json!({"action":"plan"}));assert_eq!(planned["can_promote"],true,"{planned:#}");
    let done=promote(&ep,&restored,json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
    assert_eq!(done["status"],"failed_original_unchanged","{done:#}");
    assert!(done["error"].as_str().unwrap().contains("TRIGGER privilege"));
    assert_eq!(first_value(&original,"SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()"),"1");
    assert_eq!(first_value(&original,"SELECT value FROM parents WHERE id=1"),"dump");
    for db in [candidate_of(&restored),done["backup_namespace"].as_str().map(str::to_string),done["clone_database"].as_str().map(str::to_string),Some(name)] .into_iter().flatten(){drop_db(&mut admin,&db);}
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn rvf_i_show_create_view_escaping_is_independent_of_no_backslash_mode() {
    use mysql::prelude::Queryable;
    let Some((mut admin,original,name,root))=exported() else{return};
    let LiveAdapter::MySql(mut db)=LiveAdapter::connect(&original).unwrap() else{unreachable!()};
    db.query_drop("CREATE VIEW quoted_literal AS SELECT 'it''s' AS label").unwrap();
    let mut definitions=Vec::new();
    for mode in ["", "NO_BACKSLASH_ESCAPES"] {
        db.exec_drop("SET SESSION sql_mode=?",(mode,)).unwrap();
        let row:mysql::Row=db.query_first("SHOW CREATE VIEW quoted_literal").unwrap().unwrap();
        definitions.push(row.get::<String,_>(1).unwrap());
    }
    assert_eq!(definitions[0],definitions[1]);
    assert!(definitions[0].contains("it\\'s"));
    save_json("show_create_view_escaping.json",&json!({"default":definitions[0],"no_backslash_escapes":definitions[1]}));
    drop_db(&mut admin,&name);std::fs::remove_dir_all(root).unwrap();
}

include!("safe_restore_round3.rs");
