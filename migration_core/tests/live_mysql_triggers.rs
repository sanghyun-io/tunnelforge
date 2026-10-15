//! Format 7 replace-mode contract on disposable MySQL 8.0/8.4 and PostgreSQL.
use migration_core::{handle_request, handle_request_streaming, Endpoint, LiveAdapter, MigrationAdapter, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::path::PathBuf;

fn endpoint(engine: &str) -> Option<Endpoint> {
    let prefix = if engine == "mysql" { "TF_MYSQL" } else { "TF_POSTGRES" };
    let host = std::env::var(format!("{prefix}_HOST")).ok();
    assert!(host.is_some() || std::env::var_os("TF_LIVE_REQUIRED").is_none(), "{prefix}_HOST missing");
    Some(Endpoint {
        engine: engine.into(), host: host?,
        port: std::env::var(format!("{prefix}_PORT")).ok().and_then(|s| s.parse().ok()).unwrap_or(if engine == "mysql" { 3306 } else { 5432 }),
        user: std::env::var(format!("{prefix}_USER")).ok()?, password: std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),
        database: std::env::var(format!("{prefix}_DATABASE")).ok()?, schema: None, tls: Default::default(),
    })
}
fn events(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request { command:command.into(), request_id:None, payload })
}
fn success(events: Vec<Value>) -> Value {
    assert!(!events.iter().any(|e| e["event"]=="error"), "{events:#?}");
    let result = events.into_iter().find(|e| e["event"]=="result").expect("result");
    assert_eq!(result["success"], true, "{result:#?}");
    result
}
fn refused(events: &[Value], code: &str) {
    assert!(events.iter().any(|e| e["event"]=="error" && e["message"].as_str().is_some_and(|s| s.contains(code))), "{events:#?}");
}
fn conn(endpoint: &Endpoint) -> mysql::PooledConn {
    let LiveAdapter::MySql(conn) = LiveAdapter::connect(endpoint).unwrap() else { unreachable!() }; conn
}
struct Sandbox {
    endpoint: Endpoint, admin: LiveAdapter, output: PathBuf, databases: Vec<String>,
}
impl Sandbox {
    fn new() -> Option<Self> {
        let mut endpoint = endpoint("mysql")?;
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        let name = format!("tf_tr_{}_{}",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        admin.execute_sql(&format!("CREATE DATABASE `{name}` CHARACTER SET utf8mb4")).unwrap();
        endpoint.database = name.clone();
        Some(Self { endpoint, admin, output:std::env::temp_dir().join(&name), databases:vec![name] })
    }
    fn dump(&self, format: &str, tables: Value) -> Value {
        success(events("dump.run",self.dump_payload(format,tables)))
    }
    fn dump_payload(&self, format: &str, tables: Value) -> Value {
        json!({"source":self.endpoint,"output_dir":self.output,"data_format":format,"compression":"none","tables":tables,
            "threads":1,"chunk_size":1,"mysql_snapshot_mode":"single_connection"})
    }
    fn payload(&self) -> Value { json!({"target":self.endpoint,"input_dir":self.output,"mode":"replace","threads":1}) }
    fn manifest(&self) -> Value { serde_json::from_slice(&std::fs::read(self.output.join("_tunnelforge_dump.json")).unwrap()).unwrap() }
    fn save(&self, m: &Value) { std::fs::write(self.output.join("_tunnelforge_dump.json"),serde_json::to_vec_pretty(m).unwrap()).unwrap(); }
    fn other(&mut self) -> Endpoint {
        let mut target = self.endpoint.clone(); target.database.push_str("_copy");
        self.admin.execute_sql(&format!("CREATE DATABASE `{}` CHARACTER SET utf8mb4",target.database)).unwrap();
        self.databases.push(target.database.clone()); target
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        for name in &self.databases { let _ = self.admin.execute_sql(&format!("DROP DATABASE IF EXISTS `{name}`")); }
        let _ = std::fs::remove_dir_all(&self.output);
    }
}
fn seed(db: &mut mysql::PooledConn) {
    db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY,v INT)").unwrap();
    db.query_drop("INSERT INTO sample VALUES(1,42),(2,43)").unwrap();
    db.query_drop("CREATE TRIGGER guard_delete BEFORE DELETE ON sample FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='protected'").unwrap();
}
fn metadata(db: &mut mysql::PooledConn) -> Vec<(String,String,String,String,u32,String,String,String,String,String)> {
    db.query("SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_ORDER,ACTION_STATEMENT,SQL_MODE,CHARACTER_SET_CLIENT,COLLATION_CONNECTION,DATABASE_COLLATION FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() ORDER BY 2,3,4,5").unwrap()
}

#[test]
fn triggers_round_trip_all_events_order_context_and_unfired_load() {
    let Some(s) = Sandbox::new() else { return };
    let mut db = conn(&s.endpoint);
    seed(&mut db);
    db.query_drop("CREATE TABLE audit(v INT)").unwrap();
    db.query_drop("SET SESSION sql_mode='ANSI_QUOTES,NO_BACKSLASH_ESCAPES', character_set_client='utf8mb4', collation_connection='utf8mb4_bin'").unwrap();
    db.query_drop("CREATE TRIGGER bi_z BEFORE INSERT ON sample FOR EACH ROW SET NEW.v=NEW.v+1").unwrap();
    db.query_drop("CREATE TRIGGER bi_a BEFORE INSERT ON sample FOR EACH ROW SET NEW.v=NEW.v*2").unwrap();
    db.query_drop("CREATE TRIGGER ai AFTER INSERT ON sample FOR EACH ROW BEGIN INSERT INTO audit VALUES(NEW.v); INSERT INTO audit VALUES(NEW.id); END").unwrap();
    db.query_drop("CREATE TRIGGER bu BEFORE UPDATE ON sample FOR EACH ROW SET NEW.v=NEW.v+3").unwrap();
    db.query_drop("CREATE TRIGGER au AFTER UPDATE ON sample FOR EACH ROW INSERT INTO audit VALUES(NEW.v)").unwrap();
    db.query_drop("CREATE TRIGGER ad AFTER DELETE ON sample FOR EACH ROW INSERT INTO audit VALUES(OLD.v)").unwrap();
    let expected = metadata(&mut db);
    let exported = s.dump("tsv",Value::Null);
    assert_eq!(exported["format_version"],7); assert_eq!(exported["triggers"],7); assert_eq!(exported["strict_export"],true);
    let manifest = s.manifest();
    for p in manifest["stored_programs"].as_array().unwrap() {
        let original: mysql::Row = db.query_first(format!("SHOW CREATE TRIGGER `{}`",p["name"].as_str().unwrap())).unwrap().unwrap();
        assert!(original.get::<String,_>("SQL Original Statement").unwrap().ends_with(p["definition"].as_str().unwrap()));
    }
    for _ in 0..2 {
        let imported = success(events("dump.import",s.payload()));
        assert_eq!(imported["programs_imported"].as_array().unwrap().len(),7);
        assert_eq!(metadata(&mut db),expected);
        assert_eq!(db.query::<(i32,i32),_>("SELECT * FROM sample ORDER BY id").unwrap(),vec![(1,42),(2,43)]);
        assert_eq!(db.query_first::<u64,_>("SELECT COUNT(*) FROM audit").unwrap(),Some(0));
        assert!(matches!(db.query_drop("DELETE FROM sample"),Err(mysql::Error::MySqlError(e)) if e.code==1644));
        db.query_drop("INSERT INTO sample VALUES(3,4)").unwrap();
        assert_eq!(db.query_first::<i32,_>("SELECT v FROM sample WHERE id=3").unwrap(),Some(10));
        db.query_drop("UPDATE sample SET v=20 WHERE id=3").unwrap();
        assert_eq!(db.query_first::<i32,_>("SELECT v FROM sample WHERE id=3").unwrap(),Some(23));
        // AFTER DELETE also fires once the guard is explicitly removed.
        db.query_drop("DROP TRIGGER guard_delete").unwrap();
        db.query_drop("DELETE FROM sample WHERE id=3").unwrap();
        assert_eq!(db.query::<i32,_>("SELECT v FROM audit").unwrap(),vec![10,3,23,23]);
        // Restore the guard so the second replacement exercises an existing protected target.
        db.query_drop("CREATE TRIGGER guard_delete BEFORE DELETE ON sample FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='protected'").unwrap();
    }
    let current: String = db.query_first("SELECT CURRENT_USER()").unwrap().unwrap();
    assert!(db.query::<String,_>("SELECT DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()").unwrap().iter().all(|d| d==&current));
}

#[test]
fn name_conflict_is_visible_in_preview_and_refuses_unchanged() {
    let Some(s) = Sandbox::new() else { return }; let mut db = conn(&s.endpoint); seed(&mut db); s.dump("jsonl",Value::Null);
    db.query_drop("DROP TRIGGER guard_delete").unwrap();
    db.query_drop("CREATE TABLE kept(id INT)").unwrap(); db.query_drop("INSERT INTO kept VALUES(99)").unwrap();
    db.query_drop("CREATE TRIGGER guard_delete BEFORE DELETE ON kept FOR EACH ROW SIGNAL SQLSTATE '45000'").unwrap();
    db.query_drop("UPDATE sample SET v=99").unwrap();
    let preview = success(events("dump.import_preview",s.payload()));
    assert!(preview["blockers"].as_array().unwrap().iter().any(|b| b["code"]=="trigger_name_conflict"),"{preview}");
    refused(&events("dump.import",s.payload()),"trigger_name_conflict");
    assert_eq!(db.query::<i32,_>("SELECT v FROM sample ORDER BY id").unwrap(),vec![99,99]);
    assert_eq!(db.query_first::<i32,_>("SELECT id FROM kept").unwrap(),Some(99));
}

#[test]
fn source_qualified_body_blocks_different_schema_before_changes() {
    let Some(mut s) = Sandbox::new() else { return }; let mut db = conn(&s.endpoint); seed(&mut db);
    db.query_drop("CREATE TABLE audit(v INT)").unwrap();
    db.query_drop(format!("CREATE TRIGGER ai AFTER INSERT ON sample FOR EACH ROW INSERT INTO `{}`.audit VALUES(NEW.v)",s.endpoint.database)).unwrap();
    s.dump("jsonl",Value::Null); let target=s.other(); let mut copy=conn(&target);
    copy.query_drop("CREATE TABLE sample(id INT PRIMARY KEY,v INT)").unwrap(); copy.query_drop("INSERT INTO sample VALUES(99,99)").unwrap();
    let mut payload=s.payload(); payload["target"]=json!(target);
    let preview=success(events("dump.import_preview",payload.clone()));
    assert!(preview["blockers"].as_array().unwrap().iter().any(|b| b["code"]=="program_source_schema_reference"),"{preview}");
    refused(&events("dump.import",payload.clone()),"program_source_schema_reference");
    assert_eq!(copy.query_first::<i32,_>("SELECT id FROM sample").unwrap(),Some(99));
    assert_eq!(db.query_first::<u64,_>("SELECT COUNT(*) FROM audit").unwrap(),Some(0));
    payload["skip_triggers"]=json!(true);
    let imported=success(events("dump.import",payload));
    assert_eq!(imported["programs_skipped_toggle"].as_array().unwrap().len(),2);
    assert!(metadata(&mut copy).is_empty());
}

#[test]
fn second_table_load_failure_keeps_first_guard_and_reports_trigger_losses() {
    let Some(s) = Sandbox::new() else { return }; let mut db=conn(&s.endpoint);
    for table in ["a","b"] {
        db.query_drop(format!("CREATE TABLE {table}(id INT PRIMARY KEY)")).unwrap(); db.query_drop(format!("INSERT INTO {table} VALUES(1)")).unwrap();
        db.query_drop(format!("CREATE TRIGGER {table}_guard BEFORE DELETE ON {table} FOR EACH ROW SIGNAL SQLSTATE '45000'")).unwrap();
    }
    s.dump("jsonl",Value::Null); let m=s.manifest();
    let b=m["tables"].as_array().unwrap().iter().find(|t| t["name"]=="b").unwrap();
    let chunk=b["chunk_sha256"].as_object().unwrap().keys().next().unwrap(); let path=s.output.join(b["path"].as_str().unwrap()).join(chunk);
    let mut output=Vec::new();
    handle_request_streaming(Request { command:"dump.import".into(),request_id:None,payload:s.payload() }, |e| {
        if e["event"]=="table_progress" && e["table"]=="b" && e["status"]=="importing" { std::fs::write(&path,b"broken\n").unwrap(); }
        output.push(e);
    });
    assert!(output.iter().any(|e| e["event"]=="error"),"{output:#?}");
    assert!(matches!(db.query_drop("DELETE FROM a"),Err(mysql::Error::MySqlError(e)) if e.code==1644));
    assert_eq!(metadata(&mut db).iter().map(|r|r.0.as_str()).collect::<Vec<_>>(),vec!["a_guard"]);
    let report_event=output.iter().rev().find(|e| e["event"]=="import_report").unwrap();
    assert_eq!(report_event["tables_lost_triggers"],json!(["b"]));
    assert_eq!(report_event["triggers_created"],json!([{"table":"a","name":"a_guard"}]));
    let report: Value=serde_json::from_slice(&std::fs::read(report_event["report_path"].as_str().unwrap()).unwrap()).unwrap();
    assert_eq!(report["tables_lost_triggers"],json!(["b"]));
}

#[test]
fn tampered_body_probe_rejects_chains_and_syntax_before_drop() {
    let Some(s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint); seed(&mut db); s.dump("jsonl",Value::Null);
    let original=s.manifest(); db.query_drop("UPDATE sample SET v=99").unwrap();
    for body in ["BEGIN SET NEW.v=1; END; DROP TABLE sample", "this is not SQL"] {
        let mut m=original.clone(); m["stored_programs"][0]["definition"]=json!(body); s.save(&m);
        refused(&events("dump.import",s.payload()),"program_probe_failed");
        assert_eq!(db.query::<i32,_>("SELECT v FROM sample").unwrap(),vec![99,99]); assert_eq!(metadata(&mut db).len(),1);
        assert_eq!(db.query_first::<u64,_>("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'tf_probe_%'").unwrap(),Some(0));
    }
}

#[test]
fn partial_export_import_and_explicit_toggle_report_every_skipped_trigger() {
    let Some(s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint); seed(&mut db);
    db.query_drop("CREATE TABLE other(id INT)").unwrap(); db.query_drop("CREATE TRIGGER other_guard BEFORE DELETE ON other FOR EACH ROW SIGNAL SQLSTATE '45000'").unwrap();
    s.dump("jsonl",json!(["sample"])); assert_eq!(s.manifest()["stored_programs"].as_array().unwrap().len(),1);
    let mut payload=s.payload(); payload["skip_triggers"]=json!(true);
    let preview=success(events("dump.import_preview",payload.clone())); assert_eq!(preview["programs_skipped_toggle"],json!(["trigger:sample:guard_delete"]));
    assert_eq!(preview["triggers_dropped"].as_array().unwrap().len(),1);
    let imported=success(events("dump.import",payload)); assert_eq!(imported["programs_skipped_toggle"],json!(["trigger:sample:guard_delete"]));
    assert_eq!(metadata(&mut db)[0].0,"other_guard"); db.query_drop("DELETE FROM sample").unwrap();
    // Full dump, then import only one table: report the other table's trigger.
    std::fs::remove_dir_all(&s.output).unwrap(); seed_after_delete(&mut db); s.dump("jsonl",Value::Null);
    let mut payload=s.payload(); payload["tables"]=json!(["sample"]);
    let preview=success(events("dump.import_preview",payload.clone())); assert_eq!(preview["programs_skipped_partial"],json!(["trigger:other:other_guard"]));
    let imported=success(events("dump.import",payload)); assert_eq!(imported["programs_skipped_partial"],json!(["trigger:other:other_guard"]));
    assert_eq!(metadata(&mut db).len(),2);
}
fn seed_after_delete(db: &mut mysql::PooledConn) {
    db.query_drop("INSERT INTO sample VALUES(1,42)").unwrap();
    db.query_drop("CREATE TRIGGER guard_delete BEFORE DELETE ON sample FOR EACH ROW SIGNAL SQLSTATE '45000'").unwrap();
}

#[test]
fn safe_rehearsal_refuses_before_copy_even_with_skip_toggle() {
    let Some(s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint); seed(&mut db); s.dump("jsonl",Value::Null);
    let mut payload=s.payload(); payload["mode"]=json!("safe"); payload["verify_only"]=json!(true); payload["skip_triggers"]=json!(true);
    let output=events("dump.import",payload); let result=output.iter().find(|e|e["event"]=="result").unwrap();
    assert_eq!(result["status"],"failed_original_untouched"); assert_eq!(result["candidate_created"],false);
    assert!(result["copy_blockers"].as_array().unwrap().iter().any(|b|b["code"]=="dump_programs_unsupported_in_copy"),"{output:#?}");
    assert_eq!(metadata(&mut db).len(),1);
}

#[test]
fn trigger_may_reference_a_function_not_yet_created() {
    let Some(s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint); seed(&mut db);
    db.query_drop("CREATE TRIGGER future_function BEFORE UPDATE ON sample FOR EACH ROW SET NEW.v=tf_future_function(NEW.v)").unwrap();
    s.dump("jsonl",Value::Null); success(events("dump.import",s.payload())); assert_eq!(metadata(&mut db).len(),2);
}

#[test]
fn importing_account_replaces_definer_and_reports_database_collation_difference() {
    let Some(mut s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint);
    db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY,v INT)").unwrap();
    db.query_drop("INSERT INTO sample VALUES(1,42)").unwrap();
    let user=format!("tf_tr_def_{}",std::process::id());
    let trust:bool=db.query_first("SELECT @@log_bin_trust_function_creators").unwrap().unwrap();
    let _account=RestrictedAccount { endpoint:s.endpoint.clone(),name:user.clone(),trust };
    db.query_drop(format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    db.query_drop(format!("CREATE DEFINER='{user}'@'%' TRIGGER guard_delete BEFORE DELETE ON sample FOR EACH ROW SIGNAL SQLSTATE '45000'")).unwrap();
    s.dump("jsonl",Value::Null);
    let target=s.other(); let mut target_db=conn(&target);
    target_db.query_drop(format!("ALTER DATABASE `{}` COLLATE utf8mb4_bin",target.database)).unwrap();
    let mut payload=s.payload(); payload["target"]=json!(target);
    let preview=success(events("dump.import_preview",payload.clone()));
    assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="program_definer_replaced" && w["detail"].as_str().unwrap().contains(&user)),"{preview}");
    let restored=success(events("dump.import",payload));
    for code in ["program_definer_replaced","program_database_collation_changed"] {
        assert!(restored["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]==code),"{restored}");
    }
    let current:String=target_db.query_first("SELECT CURRENT_USER()").unwrap().unwrap();
    assert_eq!(target_db.query_first::<String,_>("SELECT DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()").unwrap(),Some(current));
}

#[test]
fn recapture_refuses_trigger_drift_during_export() {
    let Some(s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint); seed(&mut db);
    let mut changed=false; let mut output=Vec::new();
    handle_request_streaming(Request { command:"dump.run".into(),request_id:None,payload:s.dump_payload("jsonl",Value::Null) }, |e| {
        if !changed && e["phase"]=="snapshot" && e.get("worker_count").is_some() {
            db.query_drop("DROP TRIGGER guard_delete").unwrap();
            db.query_drop("CREATE TRIGGER guard_delete BEFORE DELETE ON sample FOR EACH ROW SET @different=1").unwrap(); changed=true;
        }
        output.push(e);
    });
    assert!(changed); refused(&output,"stored programs changed during export"); assert!(!s.output.join("_tunnelforge_dump.json").exists());
}

struct RestrictedAccount { endpoint: Endpoint, name: String, trust: bool }
impl Drop for RestrictedAccount {
    fn drop(&mut self) {
        let mut db=conn(&self.endpoint); let _=db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'",self.name));
        let _=db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}",u8::from(self.trust)));
    }
}
#[test]
fn binlog_without_super_refuses_unchanged_and_toggle_allows_tables() {
    let Some(s)=Sandbox::new() else { return }; let mut db=conn(&s.endpoint); seed(&mut db); s.dump("jsonl",Value::Null);
    let (log_bin,trust):(bool,bool)=db.query_first("SELECT @@log_bin,@@log_bin_trust_function_creators").unwrap().unwrap();
    if !log_bin { eprintln!("SKIP binlog account path: @@log_bin=0"); return }
    let user=format!("tf_tr_u_{}",std::process::id()); let _account=RestrictedAccount { endpoint:s.endpoint.clone(),name:user.clone(),trust };
    db.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    db.query_drop(format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    db.query_drop(format!("GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,ALTER,INDEX,TRIGGER,REFERENCES ON `{}`.* TO '{user}'@'%'",s.endpoint.database)).unwrap();
    let target=Endpoint { user:user.clone(),..s.endpoint.clone() }; let mut payload=s.payload(); payload["target"]=json!(target);
    db.query_drop("UPDATE sample SET v=99").unwrap();
    let preview=success(events("dump.import_preview",payload.clone()));
    assert!(preview["blockers"].as_array().unwrap().iter().any(|b|b["code"]=="trigger_binlog_privilege"),"{preview}");
    refused(&events("dump.import",payload.clone()),"trigger_binlog_privilege");
    assert_eq!(db.query::<i32,_>("SELECT v FROM sample").unwrap(),vec![99,99]); assert_eq!(metadata(&mut db).len(),1);
    db.query_drop(format!("REVOKE TRIGGER ON `{}`.* FROM '{user}'@'%'",s.endpoint.database)).unwrap();
    let preview=success(events("dump.import_preview",payload.clone())); assert!(preview["blockers"].as_array().unwrap().iter().any(|b|b["code"]=="trigger_privilege_missing"));
    payload["skip_triggers"]=json!(true); let imported=success(events("dump.import",payload));
    assert_eq!(imported["programs_skipped_toggle"],json!(["trigger:sample:guard_delete"])); assert!(metadata(&mut db).is_empty());
}

#[test]
fn mysql_to_postgres_import_reports_skipped_triggers_and_preserves_data() {
    let (Some(s),Some(mut pg))=(Sandbox::new(),endpoint("postgresql")) else { return };
    let mut db=conn(&s.endpoint); seed(&mut db); s.dump("jsonl",Value::Null);
    let schema=s.endpoint.database.clone(); pg.schema=Some(schema.clone());
    let mut admin=LiveAdapter::connect(&endpoint("postgresql").unwrap()).unwrap();
    admin.execute_sql(&format!("CREATE SCHEMA \"{schema}\"")).unwrap();
    let run=std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut payload=s.payload(); payload["target"]=json!(pg);
        let preview=success(events("dump.import_preview",payload.clone()));
        assert_eq!(preview["programs_skipped_cross_engine"],json!(["trigger:sample:guard_delete"]));
        let imported=success(events("dump.import",payload)); assert_eq!(imported["programs_skipped_cross_engine"],json!(["trigger:sample:guard_delete"]));
        let mut target=LiveAdapter::connect(&pg).unwrap(); assert_eq!(target.row_count("sample").unwrap(),2);
    }));
    admin.execute_sql(&format!("DROP SCHEMA \"{schema}\" CASCADE")).unwrap();
    if let Err(err)=run { std::panic::resume_unwind(err); }
}
