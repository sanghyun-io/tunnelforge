//! PR5: full fidelity, replacement recovery and pre-change function probes on 8.0/8.4.
use migration_core::{handle_request,handle_request_streaming,Endpoint,LiveAdapter,MigrationAdapter,Request};
use mysql::prelude::Queryable;
use serde_json::{json,Value};
use std::path::PathBuf;

fn endpoint(engine: &str) -> Option<Endpoint> {
    let prefix=if engine=="mysql" {"TF_MYSQL"} else {"TF_POSTGRES"};
    let host=std::env::var(format!("{prefix}_HOST")).ok();
    assert!(host.is_some() || std::env::var_os("TF_LIVE_REQUIRED").is_none(),"{prefix}_HOST missing");
    Some(Endpoint { engine:engine.into(),host:host?,port:std::env::var(format!("{prefix}_PORT")).ok().and_then(|s|s.parse().ok()).unwrap_or(if engine=="mysql" {3306} else {5432}),
        user:std::env::var(format!("{prefix}_USER")).ok()?,password:std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),database:std::env::var(format!("{prefix}_DATABASE")).ok()?,schema:None,tls:Default::default() })
}
fn conn(e: &Endpoint) -> mysql::PooledConn { let LiveAdapter::MySql(c)=LiveAdapter::connect(e).unwrap() else {unreachable!()}; c }
fn events(command: &str,payload:Value) -> Vec<Value> { handle_request(Request {command:command.into(),request_id:None,payload}) }
fn outcome(es: Vec<Value>) -> Value {
    assert!(!es.iter().any(|e|e["event"]=="error"),"{es:#?}");
    es.into_iter().find(|e|e["event"]=="result").expect("result")
}
fn success(es:Vec<Value>) -> Value { let v=outcome(es); assert_eq!(v["success"],true,"{v:#?}"); v }
struct Sandbox { endpoint:Endpoint, admin:mysql::PooledConn, databases:Vec<String>, output:PathBuf, trust:bool, scheduler:String }
impl Sandbox {
    fn new() -> Option<Self> {
        let mut endpoint=endpoint("mysql")?; let mut admin=conn(&endpoint);
        let (trust,scheduler)=admin.query_first("SELECT @@log_bin_trust_function_creators,@@event_scheduler").unwrap().unwrap();
        admin.query_drop("SET GLOBAL log_bin_trust_function_creators=1; SET GLOBAL event_scheduler=OFF").unwrap();
        let name=format!("tf_pr5_{}_{}",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        admin.query_drop(format!("CREATE DATABASE `{name}` CHARACTER SET utf8mb4")).unwrap(); endpoint.database=name.clone();
        Some(Self { endpoint,admin,databases:vec![name.clone()],output:std::env::temp_dir().join(name),trust,scheduler })
    }
    fn other(&mut self,create:bool) -> Endpoint {
        let mut e=self.endpoint.clone(); e.database.push_str(&format!("_{}",self.databases.len()));
        if create { self.admin.query_drop(format!("CREATE DATABASE `{}` CHARACTER SET utf8mb4",e.database)).unwrap(); }
        self.databases.push(e.database.clone()); e
    }
    fn dump(&self) -> Value { success(events("dump.run",json!({"source":self.endpoint,"output_dir":self.output,"data_format":"jsonl","compression":"none","mysql_snapshot_mode":"single_connection","chunk_size":1,"threads":1}))) }
    fn payload(&self,e:&Endpoint) -> Value { json!({"target":e,"input_dir":self.output,"mode":"replace","threads":1}) }
    fn manifest(&self) -> Value { serde_json::from_slice(&std::fs::read(self.output.join("_tunnelforge_dump.json")).unwrap()).unwrap() }
    fn save(&self,v:&Value) { std::fs::write(self.output.join("_tunnelforge_dump.json"),serde_json::to_vec_pretty(v).unwrap()).unwrap(); }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        for name in &self.databases { let _=self.admin.query_drop(format!("DROP DATABASE IF EXISTS `{name}`")); }
        let _=self.admin.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}; SET GLOBAL event_scheduler={}",u8::from(self.trust),self.scheduler));
        let _=std::fs::remove_dir_all(&self.output);
    }
}
fn table(db:&mut mysql::PooledConn) { db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY,v INT); INSERT INTO sample VALUES(1,42),(2,43)").unwrap(); }
fn status(db:&mut mysql::PooledConn,name:&str) -> String { db.exec_first("SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME=?",(name,)).unwrap().unwrap() }

#[test]
fn full_export_all_tables_to_fresh_schema_restores_generated_triggers_routines_events_and_view() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint);
    db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY,v INT,g INT GENERATED ALWAYS AS(v*2) STORED); INSERT INTO sample(id,v) VALUES(1,42),(2,43)").unwrap();
    db.query_drop("SET SESSION sql_mode='PIPES_AS_CONCAT,NO_BACKSLASH_ESCAPES', collation_connection='utf8mb4_bin',time_zone='+09:00'").unwrap();
    db.query_drop("CREATE FUNCTION twice(x INT) RETURNS INT DETERMINISTIC SQL SECURITY DEFINER RETURN x*2; CREATE FUNCTION coin() RETURNS DOUBLE NOT DETERMINISTIC RETURN RAND()").unwrap();
    db.query_drop("CREATE PROCEDURE bump(IN n INT) SQL SECURITY INVOKER BEGIN UPDATE sample SET v=v+n; SELECT '한글' AS label; END").unwrap();
    db.query_drop("CREATE TRIGGER guard_delete BEFORE DELETE ON sample FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='protected'").unwrap();
    db.query_drop("CREATE VIEW totals AS SELECT id,twice(v) AS total FROM sample").unwrap();
    db.query_drop("CREATE EVENT enabled_job ON SCHEDULE EVERY 1 DAY STARTS '2000-01-01 00:00:00' ENABLE DO UPDATE sample SET v=v+1; CREATE EVENT disabled_job ON SCHEDULE EVERY 1 DAY DISABLE DO UPDATE sample SET v=v+2; CREATE EVENT future_job ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 1 DAY ON COMPLETION PRESERVE ENABLE DO UPDATE sample SET v=v+3").unwrap();
    let exported=s.dump(); assert_eq!(exported["format_version"],7); assert_eq!(exported["tables"],1); assert_eq!(exported["triggers"],1); assert_eq!(exported["routines"],3); assert_eq!(exported["events"],3);
    let target=s.other(false); let preview=success(events("dump.import_preview",s.payload(&target)));
    assert_eq!(preview["events_enabled"],json!(["enabled_job"]),"{preview}");
    let stream=events("dump.import",s.payload(&target));
    for phase in ["dump_import_routines","dump_import_events"] {
        assert!(stream.iter().any(|e|e["event"]=="phase" && e["phase"]==phase && e["current"].as_u64().is_some() && e["name"].as_str().is_some()),"{stream:#?}");
    }
    let imported=success(stream); assert_eq!(imported["programs_imported"].as_array().unwrap().len(),7);
    let mut fresh=conn(&target);
    assert_eq!(fresh.query::<(i32,i32,i32),_>("SELECT id,v,g FROM sample ORDER BY id").unwrap(),vec![(1,42,84),(2,43,86)]);
    assert_eq!(fresh.query::<i32,_>("SELECT total FROM totals ORDER BY id").unwrap(),vec![84,86]);
    assert_eq!(fresh.query_first::<f64,_>("SELECT coin()").unwrap().unwrap()>=0.0,true);
    assert!(matches!(fresh.query_drop("DELETE FROM sample"),Err(mysql::Error::MySqlError(e)) if e.code==1644));
    fresh.query_drop("CALL bump(2)").unwrap(); assert_eq!(fresh.query_first::<i32,_>("SELECT v FROM sample WHERE id=1").unwrap(),Some(44));
    assert_eq!(status(&mut fresh,"enabled_job"),"ENABLED"); assert_eq!(status(&mut fresh,"disabled_job"),"DISABLED"); assert_eq!(status(&mut fresh,"future_job"),"ENABLED");
    let security:Vec<(String,String)>=fresh.query("SELECT ROUTINE_NAME,SECURITY_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() ORDER BY ROUTINE_NAME").unwrap();
    assert!(security.contains(&("twice".into(),"DEFINER".into())) && security.contains(&("bump".into(),"INVOKER".into())));
    let context:Vec<(String,String,String)>=fresh.query("SELECT SQL_MODE,CHARACTER_SET_CLIENT,COLLATION_CONNECTION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE()").unwrap();
    assert!(context.iter().all(|(mode,charset,collation)|mode.contains("PIPES_AS_CONCAT") && mode.contains("NO_BACKSLASH_ESCAPES") && charset=="utf8mb4" && collation=="utf8mb4_bin"));
    assert_eq!(fresh.query_first::<String,_>("SELECT TIME_ZONE FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='enabled_job'").unwrap(),Some("+09:00".into()));
    let without_trigger=s.other(false); let mut payload=s.payload(&without_trigger); payload["skip_triggers"]=json!(true);
    let imported=success(events("dump.import",payload)); assert_eq!(imported["programs_imported"].as_array().unwrap().len(),6); assert_eq!(imported["programs_skipped_toggle"].as_array().unwrap().len(),1);
    conn(&without_trigger).query_drop("DELETE FROM sample").unwrap();
}

#[test]
fn target_event_state_wins_and_same_named_routine_is_replaced_while_absent_objects_survive() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1; CREATE EVENT job ON SCHEDULE EVERY 1 DAY STARTS '2000-01-01' ENABLE DO SET @a=1; CREATE EVENT off_job ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    let target=s.other(true); let mut fresh=conn(&target);
    fresh.query_drop("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 9; CREATE PROCEDURE kept() SELECT 5; CREATE EVENT kept ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=2; CREATE EVENT job ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=2; CREATE EVENT off_job ON SCHEDULE EVERY 1 DAY STARTS '2000-01-01' ENABLE DO SET @a=2").unwrap();
    let preview=success(events("dump.import_preview",s.payload(&target))); assert_eq!(preview["events_enabled"],json!(["off_job"])); assert_eq!(preview["programs"]["replace"].as_array().unwrap().len(),3);
    success(events("dump.import",s.payload(&target)));
    assert_eq!(fresh.query_first::<i32,_>("SELECT f()").unwrap(),Some(1)); assert_eq!(status(&mut fresh,"job"),"DISABLED"); assert_eq!(status(&mut fresh,"off_job"),"ENABLED"); assert_eq!(status(&mut fresh,"kept"),"DISABLED"); fresh.query_drop("CALL kept()").unwrap();
}

#[test]
fn failed_replacement_recreates_saved_target_function_on_the_same_connection() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db); db.query_drop("CREATE FUNCTION f() RETURNS INT RETURN 1").unwrap(); s.dump();
    let target=s.other(true); let mut fresh=conn(&target); fresh.query_drop("CREATE FUNCTION f() RETURNS INT DETERMINISTIC SQL SECURITY INVOKER RETURN 9").unwrap();
    let mut injected=false; let mut output=Vec::new();
    handle_request_streaming(Request {command:"dump.import".into(),request_id:None,payload:s.payload(&target)},|e| {
        if !injected && e["event"]=="row_progress" { db.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap(); injected=true; }
        output.push(e);
    });
    assert!(injected); let restored=outcome(output); assert_eq!(restored["status"],"partial"); assert_eq!(restored["data_imported"],true);
    assert_eq!(restored["programs_failed"][0]["previous_restored"],true,"{restored:#?}"); assert!(restored["programs_failed"][0]["previous_definition"].as_str().unwrap().contains("RETURN 9"));
    assert_eq!(fresh.query_first::<i32,_>("SELECT f()").unwrap(),Some(9));
    assert_eq!(fresh.query_first::<String,_>("SELECT SECURITY_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='f'").unwrap(),Some("INVOKER".into()));
}

struct User { endpoint:Endpoint,name:String }
impl Drop for User { fn drop(&mut self) { let _=conn(&self.endpoint).query_drop(format!("DROP USER IF EXISTS '{}'@'%'",self.name)); } }
#[test]
fn binlog_function_probe_skips_only_rejected_functions_and_keeps_existing_target_function() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE FUNCTION f() RETURNS INT RETURN 1; CREATE PROCEDURE p() SELECT 2").unwrap(); s.dump();
    let target=s.other(true); let mut fresh=conn(&target); fresh.query_drop("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 9").unwrap();
    let log:bool=db.query_first("SELECT @@log_bin").unwrap().unwrap(); assert!(log || std::env::var_os("TF_LIVE_REQUIRED").is_none(),"binary log required"); if !log { return }
    let name=format!("tf_pr5_user_{}",std::process::id()); let _user=User {endpoint:s.endpoint.clone(),name:name.clone()};
    db.query_drop(format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,ALTER,INDEX,REFERENCES,CREATE ROUTINE,ALTER ROUTINE,EXECUTE ON `{}`.* TO '{name}'@'%'; SET GLOBAL log_bin_trust_function_creators=0",target.database)).unwrap();
    let account=Endpoint {user:name,password:"tf_local_test".into(),..target.clone()};
    let imported=outcome(events("dump.import",s.payload(&account))); assert_eq!(imported["status"],"partial"); assert_eq!(imported["data_imported"],true);
    assert!(imported["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]=="routine_binlog_unsafe" && w["name"]=="f"),"{imported}");
    assert_eq!(fresh.query_first::<i32,_>("SELECT f()").unwrap(),Some(9)); fresh.query_drop("CALL p()").unwrap();
    assert_eq!(fresh.query_first::<u64,_>("SELECT COUNT(*) FROM sample").unwrap(),Some(2));
}

#[test]
fn inaccessible_routine_body_is_refused_with_reason_and_same_name_pair_is_not_partly_held() {
    let Some(s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE PROCEDURE pair() SELECT 1; CREATE FUNCTION pair() RETURNS INT DETERMINISTIC RETURN 2").unwrap();
    let name=format!("tf_pr5_read_{}",std::process::id()); let _user=User{endpoint:s.endpoint.clone(),name:name.clone()};
    db.query_drop(format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'; GRANT SELECT,EXECUTE ON `{}`.* TO '{name}'@'%'; DROP PROCEDURE pair; CREATE DEFINER='{name}'@'%' PROCEDURE pair() SELECT 1",s.endpoint.database)).unwrap();
    let account=Endpoint{user:name,password:"tf_local_test".into(),..s.endpoint.clone()};
    let output=events("dump.run",json!({"source":account,"output_dir":s.output,"mysql_snapshot_mode":"single_connection"}));
    let refusal=output.iter().find(|e|e["error_code"]=="unsupported_objects").expect("refusal");
    assert_eq!(refusal["reasons"]["routine:pair"],"routine_body_unreadable","{output:#?}"); assert!(!s.output.exists());
}

#[test]
fn routine_and_event_source_qualifiers_refuse_before_changes_and_partial_import_skips_them() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop(format!("CREATE PROCEDURE p() SELECT * FROM `{}`.sample; CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO UPDATE `{}`.sample SET v=0",s.endpoint.database,s.endpoint.database)).unwrap(); s.dump();
    let target=s.other(true); let mut fresh=conn(&target); table(&mut fresh); fresh.query_drop("UPDATE sample SET v=99").unwrap();
    let preview=success(events("dump.import_preview",s.payload(&target))); assert_eq!(preview["blockers"].as_array().unwrap().iter().filter(|b|b["code"]=="program_source_schema_reference").count(),2);
    let output=events("dump.import",s.payload(&target)); assert!(output.iter().any(|e|e["event"]=="error")); assert_eq!(fresh.query_first::<i32,_>("SELECT v FROM sample LIMIT 1").unwrap(),Some(99));
    let mut payload=s.payload(&target); payload["tables"]=json!(["sample"]); let skipped=success(events("dump.import",payload)); assert_eq!(skipped["programs_skipped_partial"].as_array().unwrap().len(),2);
    let dir=s.output.with_extension("partial");
    let exported=success(events("dump.run",json!({"source":s.endpoint,"output_dir":dir,"tables":["sample"],"mysql_snapshot_mode":"single_connection"}))); assert_eq!(exported["routines"],0); assert_eq!(exported["events"],0);
    let m:Value=serde_json::from_slice(&std::fs::read(dir.join("_tunnelforge_dump.json")).unwrap()).unwrap(); assert!(m.get("stored_programs").is_none()); std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn schema_programs_skip_on_postgres_and_safe_refuses_before_copy() {
    let (Some(s),Some(mut pg))=(Sandbox::new(),endpoint("postgresql")) else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE PROCEDURE p() SELECT 1; CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    let schema=s.endpoint.database.clone(); pg.schema=Some(schema.clone());
    let mut admin=LiveAdapter::connect(&pg).unwrap(); admin.execute_sql(&format!("CREATE SCHEMA \"{schema}\"")).unwrap();
    let preview=success(events("dump.import_preview",s.payload(&pg))); assert_eq!(preview["programs_skipped_cross_engine"].as_array().unwrap().len(),2);
    let imported=success(events("dump.import",s.payload(&pg))); assert_eq!(imported["programs_skipped_cross_engine"].as_array().unwrap().len(),2);
    let mut payload=s.payload(&s.endpoint); payload["mode"]=json!("safe"); let refused=outcome(events("dump.import",payload)); assert_eq!(refused["status"],"failed_original_untouched"); assert_eq!(refused["copy_blockers"][0]["code"],"dump_programs_unsupported_in_copy");
    admin.execute_sql(&format!("DROP SCHEMA \"{schema}\" CASCADE")).unwrap();
}

#[test]
fn event_warnings_and_replica_disabled_state_are_reported_without_running_a_probe_event() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 1 DAY ON COMPLETION PRESERVE ENABLE DO SET @a=1").unwrap(); s.dump();
    let mut m=s.manifest(); let p=&mut m["stored_programs"][0]; let sql=p["definition"].as_str().unwrap();
    let start=sql.find(" AT '").unwrap()+5; let end=start+sql[start..].find('\'').unwrap();
    let mut sql=sql.to_string(); sql.replace_range(start..end,"2000-01-01 00:00:00"); sql=sql.replace(" ENABLE "," DISABLE ON SLAVE "); p["definition"]=json!(sql); p["status"]=json!("SLAVESIDE_DISABLED"); s.save(&m);
    let target=s.other(true); let restored=success(events("dump.import",s.payload(&target))); assert_eq!(status(&mut conn(&target),"e"),"DISABLED");
    assert!(restored["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]=="program_event_system_timezone"));
    let p=&mut m["stored_programs"][0]; p["definition"]=json!(p["definition"].as_str().unwrap().replace("ON COMPLETION PRESERVE","ON COMPLETION NOT PRESERVE")); p["status"]=json!("DISABLED"); s.save(&m);
    let other=s.other(true); let warned=outcome(events("dump.import",s.payload(&other)));
    assert_eq!(warned["status"],"partial");
    assert_eq!(warned["programs_failed"][0]["code"],"program_event_dropped");
    assert!(warned["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]==1588),"{warned}");
}

#[test]
fn definer_change_names_routines_and_events_and_drift_is_fatal() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    let name=format!("tf_pr5_owner_{}",std::process::id()); let _user=User{endpoint:s.endpoint.clone(),name:name.clone()};
    db.query_drop(format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'; CREATE DEFINER='{name}'@'%' PROCEDURE p() SQL SECURITY DEFINER SELECT 1; CREATE DEFINER='{name}'@'%' EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1")).unwrap(); s.dump();
    let target=s.other(true); let preview=success(events("dump.import_preview",s.payload(&target)));
    let change=preview["warnings"].as_array().unwrap().iter().find(|w|w["code"]=="program_definer_replaced").unwrap()["detail"].as_str().unwrap(); assert!(change.contains("p (") && change.contains("e (") && change.contains(&name) && change.contains("root@%"),"{change}");
    success(events("dump.import",s.payload(&target))); let mut fresh=conn(&target); assert_eq!(fresh.query_first::<String,_>("SELECT SECURITY_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE()").unwrap(),Some("DEFINER".into()));
    std::fs::remove_dir_all(&s.output).unwrap(); let mut mutated=false; let mut output=Vec::new();
    handle_request_streaming(Request {command:"dump.run".into(),request_id:None,payload:json!({"source":s.endpoint,"output_dir":s.output,"mysql_snapshot_mode":"single_connection"})},|e| { if !mutated && e["phase"]=="snapshot" && e.get("worker_count").is_some() { db.query_drop("DROP PROCEDURE p; CREATE PROCEDURE p() SELECT 9").unwrap(); mutated=true; } output.push(e); });
    assert!(mutated); assert!(output.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap().contains("stored programs changed")),"{output:#?}");
}

struct AutoPrivileges { endpoint:Endpoint, previous:bool }
impl Drop for AutoPrivileges { fn drop(&mut self) { let _=conn(&self.endpoint).query_drop(format!("SET GLOBAL automatic_sp_privileges={}",u8::from(self.previous))); } }
#[test]
fn probe_cleanup_failure_names_leftover_and_refuses_before_any_table_changes() {
    let Some(s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db); db.query_drop("CREATE PROCEDURE p() SELECT 1").unwrap(); s.dump();
    let previous:bool=db.query_first("SELECT @@automatic_sp_privileges").unwrap().unwrap(); let _auto=AutoPrivileges{endpoint:s.endpoint.clone(),previous};
    let name=format!("tf_pr5_cleanup_{}",std::process::id()); let _user=User{endpoint:s.endpoint.clone(),name:name.clone()};
    db.query_drop(format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,ALTER,INDEX,REFERENCES,CREATE ROUTINE ON `{}`.* TO '{name}'@'%'; SET GLOBAL automatic_sp_privileges=0; UPDATE sample SET v=99",s.endpoint.database)).unwrap();
    let account=Endpoint{user:name,password:"tf_local_test".into(),..s.endpoint.clone()};
    let output=events("dump.import",s.payload(&account)); assert!(output.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap().contains("program_probe_cleanup_failed")),"{output:#?}");
    let report=output.iter().rev().find(|e|e["event"]=="import_report").unwrap(); assert_eq!(report["dropped_tables"],json!([]));
    let leftover=report["program_warnings"].as_array().unwrap().iter().find(|w|w["code"]=="program_probe_cleanup_failed").unwrap()["name"].as_str().unwrap();
    assert!(leftover.starts_with("tf_probe_")); assert_eq!(db.query_first::<i32,_>("SELECT v FROM sample LIMIT 1").unwrap(),Some(99));
    let count:Option<u64>=db.exec_first("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME=?",(leftover,)).unwrap(); assert_eq!(count,Some(1));
}

#[test]
fn event_create_failure_restores_target_definition_and_disabled_state() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SET @original=1").unwrap(); s.dump();
    let mut m=s.manifest(); let p=&mut m["stored_programs"][0]; p["definition"]=json!(p["definition"].as_str().unwrap().replace("SET @original=1","BOGUS SQL")); s.save(&m);
    let target=s.other(true); let mut fresh=conn(&target); fresh.query_drop("CREATE EVENT e ON SCHEDULE EVERY 2 DAY DISABLE DO SET @previous=9").unwrap();
    let restored=outcome(events("dump.import",s.payload(&target))); assert_eq!(restored["status"],"partial"); assert_eq!(restored["programs_failed"][0]["previous_restored"],true,"{restored}");
    assert!(restored["programs_failed"][0]["previous_definition"].as_str().unwrap().contains("@previous")); assert_eq!(status(&mut fresh,"e"),"DISABLED");
}

#[test]
fn same_named_procedure_function_pair_with_in_out_and_reads_sql_data_roundtrips_and_chains_are_refused() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE PROCEDURE pair(IN x INT, OUT y INT) BEGIN SET y=x+1; SELECT y; END; CREATE FUNCTION pair(x INT) RETURNS INT DETERMINISTIC READS SQL DATA RETURN x+(SELECT MAX(v) FROM sample)").unwrap(); s.dump();
    let target=s.other(true); success(events("dump.import",s.payload(&target))); let mut fresh=conn(&target);
    fresh.query_drop("CALL pair(3,@answer)").unwrap(); assert_eq!(fresh.query_first::<i32,_>("SELECT @answer").unwrap(),Some(4)); assert_eq!(fresh.query_first::<i32,_>("SELECT pair(3)").unwrap(),Some(46));
    let mut partial=s.payload(&target); partial["tables"]=json!(["sample"]); let preview=success(events("dump.import_preview",partial)); assert_eq!(preview["programs_skipped_partial"].as_array().unwrap().len(),2);
    let mut m=s.manifest(); let p=m["stored_programs"].as_array_mut().unwrap().iter_mut().find(|p|p["kind"]=="PROCEDURE").unwrap();
    p["definition"]=json!(format!("{}; DROP TABLE sample",p["definition"].as_str().unwrap())); s.save(&m); fresh.query_drop("UPDATE sample SET v=99").unwrap();
    let output=events("dump.import",s.payload(&target)); assert!(output.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap().contains("program_probe_failed")),"{output:#?}");
    assert_eq!(fresh.query_first::<i32,_>("SELECT v FROM sample LIMIT 1").unwrap(),Some(99));
}

// Cross-review reproductions, adapted into permanent regression tests.
fn definer(db:&mut mysql::PooledConn,name:&str) -> Option<String> { db.exec_first("SELECT DEFINER FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME=?",(name,)).unwrap() }
fn bogus_event_dump(s:&Sandbox) {
    let mut m=s.manifest(); let p=&mut m["stored_programs"][0];
    p["definition"]=json!(p["definition"].as_str().unwrap().replace("SET @original=1","BOGUS SQL")); s.save(&m);
}

#[test]
fn regression_replica_disabled_event_exports_and_restores_disabled() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT r ON SCHEDULE EVERY 1 DAY DISABLE ON SLAVE DO SET @a=1").unwrap();
    let st=status(&mut db,"r"); assert!(matches!(st.as_str(),"SLAVESIDE_DISABLED" | "REPLICA_SIDE_DISABLED"));
    let out=events("dump.run",json!({"source":s.endpoint,"output_dir":s.output,"data_format":"jsonl","compression":"none","mysql_snapshot_mode":"single_connection"}));
    let refusal=out.iter().find(|e|e["error_code"]=="unsupported_objects").cloned();
    assert!(refusal.is_none(),"replica-disabled event refused: {refusal:#?}");
    let target=s.other(true); success(events("dump.import",s.payload(&target))); assert_eq!(status(&mut conn(&target),"r"),"DISABLED");
}

#[test]
fn regression_target_replica_disabled_same_named_event_is_replaced() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT r ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    let target=s.other(true); let mut fresh=conn(&target);
    fresh.query_drop("CREATE EVENT r ON SCHEDULE EVERY 2 DAY DISABLE ON SLAVE DO SET @a=2").unwrap();
    let v=outcome(events("dump.import",s.payload(&target)));
    assert_eq!(v["status"],"completed","{v:#}");
}

#[test]
fn regression_failed_event_replacement_restores_foreign_definer_event_without_set_user_id() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SET @original=1").unwrap(); s.dump(); bogus_event_dump(&s);
    let target=s.other(true); let pid=std::process::id();
    let owner=format!("rv_owner_{pid}"); let importer=format!("rv_imp_{pid}");
    let _o=User{endpoint:s.endpoint.clone(),name:owner.clone()}; let _i=User{endpoint:s.endpoint.clone(),name:importer.clone()};
    db.query_drop(format!("CREATE USER '{owner}'@'%' IDENTIFIED BY 'tf_local_test'; CREATE USER '{importer}'@'%' IDENTIFIED BY 'tf_local_test'; GRANT ALL ON `{}`.* TO '{importer}'@'%'",target.database)).unwrap();
    let mut fresh=conn(&target); fresh.query_drop(format!("CREATE DEFINER='{owner}'@'%' EVENT e ON SCHEDULE EVERY 2 DAY DISABLE DO SET @previous=9")).unwrap();
    let account=Endpoint{user:importer,password:"tf_local_test".into(),..target.clone()};
    let preview=success(events("dump.import_preview",s.payload(&account)));
    assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="program_restore_definer_may_change"));
    let v=outcome(events("dump.import",s.payload(&account)));
    assert_eq!(v["programs_failed"][0]["previous_restored"],true,"{:#}",v["programs_failed"]);
    assert!(definer(&mut fresh,"e").is_some(),"target event e is gone");
    assert_eq!(v["programs_failed"][0]["previous_definer_preserved"],false);
    assert!(v["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]=="program_restore_definer_changed"));

}

#[test]
fn regression_restored_enabled_event_keeps_its_original_definer() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SET @original=1").unwrap(); s.dump(); bogus_event_dump(&s);
    let target=s.other(true); let owner=format!("rv_owner2_{}",std::process::id()); let _o=User{endpoint:s.endpoint.clone(),name:owner.clone()};
    db.query_drop(format!("CREATE USER '{owner}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    let mut fresh=conn(&target); fresh.query_drop(format!("CREATE DEFINER='{owner}'@'%' EVENT e ON SCHEDULE EVERY 2 DAY ENABLE DO SET @previous=9")).unwrap();
    let v=outcome(events("dump.import",s.payload(&target))); assert_eq!(v["programs_failed"][0]["previous_restored"],true,"{v:#}");
    assert_eq!(status(&mut fresh,"e"),"ENABLED");
    assert_eq!(definer(&mut fresh,"e"),Some(format!("{owner}@%")),"restore changed the definer");
}

#[test]
fn regression_replacing_routine_keeps_or_reports_routine_level_grants() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE PROCEDURE p() SELECT 1").unwrap(); s.dump();
    let target=s.other(true); let app=format!("rv_app_{}",std::process::id()); let _a=User{endpoint:s.endpoint.clone(),name:app.clone()};
    let mut fresh=conn(&target);
    fresh.query_drop(format!("CREATE PROCEDURE p() SELECT 2; CREATE USER '{app}'@'%' IDENTIFIED BY 'x'; GRANT EXECUTE ON PROCEDURE `{}`.p TO '{app}'@'%'",target.database)).unwrap();
    let preview=success(events("dump.import_preview",s.payload(&target)));
    let v=success(events("dump.import",s.payload(&target)));
    let grants:Option<u64>=db.exec_first("SELECT COUNT(*) FROM mysql.procs_priv WHERE Db=? AND User=?",(&target.database,&app)).unwrap();
    assert_eq!(grants,Some(0));
    assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="routine_grants_reset"));
    assert!(v["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]=="routine_grants_reset"));
}

#[test]
fn regression_past_one_shot_event_replacing_target_event_keeps_previous_definition() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 1 DAY ON COMPLETION NOT PRESERVE DISABLE DO SET @a=1").unwrap(); s.dump();
    let mut m=s.manifest(); let p=&mut m["stored_programs"][0]; let mut sql=p["definition"].as_str().unwrap().to_string();
    let start=sql.find(" AT '").unwrap()+5; let end=start+sql[start..].find('\'').unwrap(); sql.replace_range(start..end,"2000-01-01 00:00:00"); p["definition"]=json!(sql); s.save(&m);
    let target=s.other(true); let mut fresh=conn(&target); fresh.query_drop("CREATE EVENT e ON SCHEDULE EVERY 2 DAY DISABLE DO SET @previous=9").unwrap();
    let v=outcome(events("dump.import",s.payload(&target)));
    assert_eq!(v["status"],"partial");
    assert!(v["programs_imported"].as_array().unwrap().is_empty());
    assert_eq!(v["programs_failed"][0]["previous_restored"],true);
    assert!(v["programs_failed"][0]["previous_definition"].as_str().unwrap().contains("@previous=9"));
    assert!(definer(&mut fresh,"e").is_some());
}

#[test]
fn regression_nondeterministic_function_with_super_preview_warns() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE FUNCTION f() RETURNS INT RETURN 1").unwrap(); s.dump();
    let log:bool=db.query_first("SELECT @@log_bin").unwrap().unwrap(); assert!(log || std::env::var_os("TF_LIVE_REQUIRED").is_none(),"binary log required"); if !log { return }
    db.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    let target=s.other(true);
    let preview=success(events("dump.import_preview",s.payload(&target)));
    let v=outcome(events("dump.import",s.payload(&target)));
    assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="routine_binlog_unsafe"),"preview silent but runtime skipped");
    assert_eq!(v["status"],"partial");
    assert!(v["program_warnings"].as_array().unwrap().iter().any(|w|w["code"]=="routine_binlog_unsafe" && w["name"]=="f"));
}

#[test]
fn recovery_keeps_the_targets_replica_state_and_definer_verbatim() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SET @original=1").unwrap(); s.dump(); bogus_event_dump(&s);
    let target=s.other(true); let mut fresh=conn(&target);
    let owner=format!("tf_replica_owner_{}",std::process::id()); let _owner=User{endpoint:s.endpoint.clone(),name:owner.clone()};
    db.query_drop(format!("CREATE USER '{owner}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    fresh.query_drop(format!("CREATE DEFINER='{owner}'@'%' EVENT e ON SCHEDULE EVERY 2 DAY DISABLE ON SLAVE DO SET @previous=9")).unwrap();
    let before=status(&mut fresh,"e");
    let v=outcome(events("dump.import",s.payload(&target))); assert_eq!(v["programs_failed"][0]["previous_restored"],true,"{v}");
    assert_eq!(status(&mut fresh,"e"),before); assert_eq!(definer(&mut fresh,"e"),Some(format!("{owner}@%")));
}

#[test]
fn each_object_resets_charset_before_reading_unicode_target_metadata() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("SET NAMES latin1; CREATE PROCEDURE a() SELECT 1; SET NAMES utf8mb4; CREATE PROCEDURE `한글`() SELECT 2").unwrap(); s.dump();
    let mut target=s.endpoint.clone(); target.database.push_str("_한글"); s.databases.push(target.database.clone());
    db.query_drop(format!("CREATE DATABASE `{}` CHARACTER SET utf8mb4",target.database)).unwrap();
    let mut fresh=conn(&target); fresh.query_drop("SET NAMES latin1; CREATE PROCEDURE a() SELECT 9; SET NAMES utf8mb4; CREATE PROCEDURE `한글`() SELECT 9").unwrap();
    let v=success(events("dump.import",s.payload(&target))); assert_eq!(v["programs_imported"].as_array().unwrap().len(),2);
    let row:mysql::Row=fresh.query_first("SHOW CREATE PROCEDURE `한글`").unwrap().unwrap(); assert!(row.get::<String,_>("Create Procedure").unwrap().contains("SELECT 2"));
    assert_eq!(fresh.query_first::<u64,_>("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE()").unwrap(),Some(2));
}

#[test]
fn bad_event_timezone_warns_in_preview_and_preserves_target_before_drop() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT bad ON SCHEDULE EVERY 1 DAY STARTS '2000-01-01' ENABLE DO SET @a=1; CREATE EVENT good ON SCHEDULE EVERY 1 DAY STARTS '2000-01-01' ENABLE DO SET @a=1").unwrap(); s.dump();
    let mut m=s.manifest(); m["stored_programs"].as_array_mut().unwrap().iter_mut().find(|p|p["name"]=="bad").unwrap()["time_zone"]=json!("Missing/Zone"); s.save(&m);
    let target=s.other(true); let mut fresh=conn(&target); fresh.query_drop("CREATE EVENT bad ON SCHEDULE EVERY 2 DAY ENABLE DO SET @previous=9").unwrap();
    let before:mysql::Row=fresh.query_first("SHOW CREATE EVENT bad").unwrap().unwrap(); let before:String=before.get("Create Event").unwrap();
    let preview=success(events("dump.import_preview",s.payload(&target))); assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="program_session_unavailable")); assert_eq!(preview["events_enabled"],json!(["good"]));
    let absent=s.other(false); let preview=success(events("dump.import_preview",s.payload(&absent))); assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="program_session_unavailable"));
    let v=outcome(events("dump.import",s.payload(&target))); assert_eq!(v["status"],"partial"); assert_eq!(v["programs_failed"][0]["target_unchanged"],true);
    let after:mysql::Row=fresh.query_first("SHOW CREATE EVENT bad").unwrap().unwrap(); assert_eq!(after.get::<String,_>("Create Event").unwrap(),before); assert_eq!(status(&mut fresh,"good"),"ENABLED");
}

#[test]
fn unprovable_function_recovery_leaves_foreign_owned_target_untouched() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap(); s.dump();
    let target=s.other(true); let mut fresh=conn(&target); fresh.query_drop("CREATE FUNCTION f() RETURNS INT RETURN 9").unwrap();
    let name=format!("tf_unrestorable_{}",std::process::id()); let _user=User{endpoint:s.endpoint.clone(),name:name.clone()};
    db.query_drop(format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'; GRANT ALL ON `{}`.* TO '{name}'@'%'",target.database)).unwrap();
    let account=Endpoint{user:name,password:"tf_local_test".into(),..target.clone()}; let mut output=Vec::new(); let mut injected=false;
    handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&account)},|e| {
        if !injected && e["event"]=="row_progress" { db.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap(); injected=true; }
        output.push(e);
    });
    let v=outcome(output); assert!(injected); assert_eq!(v["status"],"partial"); assert_eq!(v["programs_failed"][0]["target_unchanged"],true); assert_eq!(v["programs_failed"][0]["code"],"program_restore_unavailable");
    assert_eq!(fresh.query_first::<i32,_>("SELECT f()").unwrap(),Some(9));
}

#[test]
fn tampered_event_headers_cannot_escape_the_target_or_hide_enabled_creation() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    let victim=s.other(true); let target=s.other(true); let mut fresh=conn(&target); table(&mut fresh); fresh.query_drop("UPDATE sample SET v=99").unwrap();
    let original=s.manifest();
    for definition in [
        format!("CREATE EVENT `{}` #\rON SCHEDULE AT '2000-01-01 00:00:00' DISABLE DO SELECT 1 #\n.`ev` ON SCHEDULE EVERY 1 DAY ENABLE DO SET @a=1",victim.database),
        format!("CREATE EVENT `{}` -- \rON SCHEDULE AT '2000-01-01 00:00:00' DISABLE DO SELECT 1 #\n.`ev` ON SCHEDULE EVERY 1 DAY ENABLE DO SET @a=1",victim.database),
        format!("CREATE EVENT `{}` /*!.`leaked`*/ ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1",victim.database),
        format!("CREATE EVENT `{}` /*M!.`leaked`*/ ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1",victim.database),
        format!("CREATE EVENT `{}` ON SCHEDULE EVERY 1 DAY /*! ENABLE COMMENT '*/ DISABLE /*' */ DO SET @a=1",victim.database),
        format!("CREATE EVENT `{}` ON SCHEDULE EVERY 1 DAY ENABLE --\u{1}DISABLE\nDO SET @a=1",victim.database),
    ] {
        let mut m=original.clone(); m["stored_programs"][0]["name"]=json!(victim.database); m["stored_programs"][0]["definition"]=json!(definition); s.save(&m);
        let preview=success(events("dump.import_preview",s.payload(&target))); assert!(!preview["blockers"].as_array().unwrap().is_empty());
        let output=events("dump.import",s.payload(&target)); assert!(output.iter().any(|e|e["event"]=="error"));
        assert_eq!(fresh.query_first::<i32,_>("SELECT v FROM sample LIMIT 1").unwrap(),Some(99));
        let count:Option<u64>=s.admin.exec_first("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?",(&victim.database,)).unwrap(); assert_eq!(count,Some(0));
    }
}

#[test]
fn schedule_expressions_are_conservatively_visible_in_the_import_plan() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY STARTS '2099-01-01' ENABLE DO SET @a=1").unwrap(); s.dump();
    let mut m=s.manifest(); let p=&mut m["stored_programs"][0];
    p["definition"]=json!(p["definition"].as_str().unwrap().replace("2099-01-01 00:00:00'","2099-01-01 00:00:00' - INTERVAL 100 YEAR")); s.save(&m);
    let target=s.other(true); let preview=success(events("dump.import_preview",s.payload(&target)));
    assert_eq!(preview["events_enabled"],json!(["e"]));
}

#[test]
fn direct_import_without_event_privilege_stops_before_any_table_drop() {
    let Some(s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    let name=format!("tf_event_denied_{}",std::process::id()); let _user=User{endpoint:s.endpoint.clone(),name:name.clone()};
    db.query_drop(format!("CREATE USER '{name}'@'%' IDENTIFIED BY 'tf_local_test'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,ALTER,INDEX,REFERENCES ON `{}`.* TO '{name}'@'%'",s.endpoint.database)).unwrap();
    let account=Endpoint{user:name,password:"tf_local_test".into(),..s.endpoint.clone()};
    let output=events("dump.import",s.payload(&account)); assert!(output.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap().contains("program_privilege_missing")),"{output:#?}");
    let report=output.iter().rev().find(|e|e["event"]=="import_report").unwrap(); assert_eq!(report["dropped_tables"],json!([]));
}

#[test]
fn pending_probe_prefix_is_persisted_before_create_and_cleared_after_cleanup() {
    let Some(s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db); db.query_drop("CREATE PROCEDURE p() SELECT 1").unwrap(); s.dump();
    let mut seen=false; let mut output=Vec::new();
    handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&s.endpoint)},|e| {
        if e["event"]=="probe_checkpoint" {
            let prefix=e["prefix"].as_str().unwrap(); let report:Value=serde_json::from_slice(&std::fs::read(e["report_path"].as_str().unwrap()).unwrap()).unwrap();
            assert!(report["probe_left_behind"].as_array().unwrap().contains(&json!(prefix)));
            let count:Option<u64>=db.exec_first("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME LIKE ?",(format!("{prefix}%"),)).unwrap(); assert_eq!(count,Some(0)); seen=true;
        }
        output.push(e);
    });
    assert!(seen); let v=success(output); let report:Value=serde_json::from_slice(&std::fs::read(v["import_report"].as_str().unwrap()).unwrap()).unwrap(); assert_eq!(report["probe_left_behind"],json!([]));
}

fn kill_target_connections(admin:&mut mysql::PooledConn, database:&str) {
    let ids:Vec<u64>=admin.exec("SELECT ID FROM information_schema.PROCESSLIST WHERE DB=?",(database,)).unwrap();
    for id in ids { let _=admin.query_drop(format!("KILL CONNECTION {id}")); }
}

#[test]
fn post_create_disconnect_reports_the_new_disabled_event_left_in_target() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SET @a=1").unwrap(); s.dump();
    let target=s.other(true); let mut output=Vec::new(); let mut killed=false;
    handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&target)},|e| {
        if e["event"]=="program_created" && e["kind"]=="EVENT" && !killed { kill_target_connections(&mut db,&target.database); killed=true; }
        output.push(e);
    });
    let v=outcome(output); assert!(killed); assert_eq!(v["status"],"partial");
    assert_eq!(v["programs_failed"][0]["code"],"program_finalize_failed"); assert_eq!(v["programs_failed"][0]["object_left"],true); assert_eq!(v["programs_failed"][0]["enabled"],Value::Null);
    assert_eq!(status(&mut conn(&target),"e"),"DISABLED");
}

#[test]
fn post_create_disconnect_keeps_the_created_trigger_in_the_journal() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE TRIGGER guard BEFORE DELETE ON sample FOR EACH ROW SIGNAL SQLSTATE '45000'").unwrap(); s.dump();
    let target=s.other(true); let mut output=Vec::new(); let mut killed=false;
    handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&target)},|e| {
        if e["event"]=="program_created" && e["kind"]=="TRIGGER" && !killed { kill_target_connections(&mut db,&target.database); killed=true; }
        output.push(e);
    });
    assert!(killed); let report=output.iter().rev().find(|e|e["event"]=="import_report").unwrap();
    assert_eq!(report["triggers_created"],json!([{"name":"guard","table":"sample"}])); assert_eq!(report["tables_lost_triggers"],json!([]));
    assert_eq!(report["programs_failed"][0]["object_left"],true);
    assert!(matches!(conn(&target).query_drop("DELETE FROM sample"),Err(mysql::Error::MySqlError(e)) if e.code==1644));
}


#[test]
fn cr_line_comments_cannot_hide_disabled_state_for_new_or_replaced_events() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    let original=s.manifest();
    for existing in [false,true] {
        let target=s.other(true); let mut fresh=conn(&target); table(&mut fresh);
        fresh.query_drop("UPDATE sample SET v=99").unwrap();
        if existing { fresh.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @previous=1").unwrap(); }
        for comment in ["-- x","#x"] {
            let mut m=original.clone();
            m["stored_programs"][0]["definition"]=json!(format!("CREATE EVENT `e` ON SCHEDULE EVERY 1 DAY {comment}\rDISABLE\nDO SET @a=1")); s.save(&m);
            let preview=success(events("dump.import_preview",s.payload(&target)));
            assert!(!preview["blockers"].as_array().unwrap().is_empty(),"{preview}");
            let output=events("dump.import",s.payload(&target));
            assert!(output.iter().any(|e|e["event"]=="error" && e.to_string().contains("program_definition_invalid")),"{output:#?}");
            assert_eq!(fresh.query_first::<i32,_>("SELECT v FROM sample LIMIT 1").unwrap(),Some(99));
            if existing { assert_eq!(status(&mut fresh,"e"),"DISABLED"); }
            else { assert_eq!(fresh.query_first::<u64,_>("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE()").unwrap(),Some(0)); }
        }
    }
}

#[test]
fn event_state_is_read_back_and_corrected_if_changed_at_create_boundary() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @a=1").unwrap(); s.dump();
    for intended_enabled in [false,true] {
        let target=s.other(true);
        if intended_enabled { conn(&target).query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY ENABLE DO SET @old=1").unwrap(); }
        let mut changed=false; let mut output=Vec::new();
        handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&target)},|e| {
            if e["event"]=="program_created" && e["kind"]=="EVENT" {
                let mut other=conn(&target); assert_eq!(status(&mut other,"e"),"DISABLED");
                other.query_drop("ALTER EVENT e ENABLE").unwrap(); changed=true;
            }
            output.push(e);
        });
        let v=success(output); assert!(changed);
        let expected=if intended_enabled {"ENABLED"} else {"DISABLED"};
        assert_eq!(status(&mut conn(&target),"e"),expected);
        let imported=v["programs_imported"].as_array().unwrap().iter().find(|p|p["kind"]=="EVENT").unwrap();
        assert_eq!(imported["status"],expected); assert_eq!(imported["enabled"],intended_enabled);
    }
}

#[test]
fn long_enum_and_set_definitions_export_and_import_with_values_intact() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint);
    let labels=(0..40).map(|i|format!("선택항목_{i:03}_long_value")).collect::<Vec<_>>();
    let list=labels.iter().map(|v|format!("'{v}'")).collect::<Vec<_>>().join(",");
    assert!(list.len()>512);
    db.query_drop(format!("CREATE TABLE sample(id INT PRIMARY KEY,e ENUM({list}),s SET({list}))")).unwrap();
    let set_value=format!("{},{}",labels[0],labels[39]);
    db.exec_drop("INSERT INTO sample VALUES(1,?,?)",(&labels[39],&set_value)).unwrap();
    s.dump(); let target=s.other(false); success(events("dump.import",s.payload(&target)));
    let mut fresh=conn(&target);
    let row:Option<(String,String)>=fresh.query_first("SELECT e,s FROM sample").unwrap();
    assert_eq!(row,Some((labels[39].clone(),set_value)));
    let types=|db:&mut mysql::PooledConn| db.query::<String,_>("SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sample' AND COLUMN_NAME IN ('e','s') ORDER BY COLUMN_NAME").unwrap();
    assert_eq!(types(&mut db),types(&mut fresh));
    assert!(types(&mut fresh).iter().all(|t|t.len()>512));
}

fn wait_for_preserved_completion(db:&mut mysql::PooledConn) {
    for _ in 0..100 {
        let done:Option<bool>=db.query_first("SELECT STATUS='DISABLED' AND LAST_EXECUTED IS NOT NULL FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='e'").unwrap();
        let rows:Option<u64>=db.query_first("SELECT COUNT(*) FROM audit").unwrap();
        if done==Some(true) && rows==Some(1) { return }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    panic!("preserved event did not complete exactly once");
}

#[test]
fn due_preserved_events_complete_once_without_failed_import_or_replacement_rollback() {
    for schedule in ["AT '2000-01-01 00:00:00'", "EVERY 1 DAY STARTS '2000-01-01 00:00:00' ENDS '2000-01-02 00:00:00'"] {
        let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint);
        db.query_drop("CREATE TABLE audit(id INT)").unwrap();
        db.query_drop(format!("CREATE EVENT e ON SCHEDULE {schedule} ON COMPLETION PRESERVE DISABLE DO INSERT INTO audit VALUES(1)")).unwrap();
        db.query_drop("ALTER EVENT e ENABLE").unwrap(); assert_eq!(status(&mut db,"e"),"ENABLED"); s.dump();
        for existing in [false,true] { for complete_before_read in [false,true] {
            db.query_drop("SET GLOBAL event_scheduler=OFF").unwrap();
            let target=s.other(true);
            if existing { conn(&target).query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY STARTS '2099-01-01' ENABLE DO SET @old=1").unwrap(); }
            let preview=success(events("dump.import_preview",s.payload(&target)));
            assert!(preview["events_enabled"].as_array().unwrap().contains(&json!("e")),"{preview}");
            db.query_drop("SET GLOBAL event_scheduler=ON").unwrap();
            let mut output=Vec::new(); let mut injected=false;
            handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&target)},|e| {
                if complete_before_read && e["event"]=="program_created" && e["kind"]=="EVENT" {
                    let mut fresh=conn(&target); fresh.query_drop("ALTER EVENT e ENABLE").unwrap();
                    wait_for_preserved_completion(&mut fresh); injected=true;
                }
                output.push(e);
            });
            let v=success(output); assert_eq!(injected,complete_before_read);
            assert!(v["programs_failed"].as_array().unwrap().is_empty(),"{v}");
            let mut fresh=conn(&target); wait_for_preserved_completion(&mut fresh);
            let body:Option<String>=fresh.query_first("SELECT EVENT_DEFINITION FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='e'").unwrap();
            assert!(body.unwrap().contains("INSERT INTO audit"),"old definition was restored");
            let imported=v["programs_imported"].as_array().unwrap().iter().find(|p|p["kind"]=="EVENT").unwrap();
            if complete_before_read { assert_eq!(imported["status"],"DISABLED"); assert_eq!(imported["enabled"],false); }
        }}
    }
}

#[test]
fn recovery_reports_the_actual_definer_after_reenabling_a_past_preserved_event() {
    let Some(mut s)=Sandbox::new() else {return}; let mut db=conn(&s.endpoint); table(&mut db);
    db.query_drop("CREATE EVENT e ON SCHEDULE EVERY 1 DAY DISABLE DO SET @original=1").unwrap(); s.dump();
    let mut m=s.manifest(); m["stored_programs"][0]["definition"]=json!("CREATE EVENT `e` ON SCHEDULE AT '2000-01-01' ON COMPLETION NOT PRESERVE ENABLE DO SET @a=1"); m["stored_programs"][0]["status"]=json!("ENABLED"); s.save(&m);
    let target=s.other(true); let owner=format!("lr_owner_{}",std::process::id()); let _owner=User{endpoint:s.endpoint.clone(),name:owner.clone()};
    db.query_drop(format!("CREATE USER '{owner}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    let mut fresh=conn(&target);
    fresh.query_drop(format!("CREATE DEFINER='{owner}'@'%' EVENT e ON SCHEDULE AT '2000-01-01' ON COMPLETION PRESERVE ENABLE DO SET @previous=1")).unwrap();
    fresh.query_drop(format!("ALTER DEFINER='{owner}'@'%' EVENT e ENABLE")).unwrap();
    assert_eq!(status(&mut fresh,"e"),"ENABLED"); assert_eq!(definer(&mut fresh,"e"),Some(format!("{owner}@%")));
    let v=outcome(events("dump.import",s.payload(&target)));
    let failed=&v["programs_failed"][0]; assert_eq!(failed["previous_restored"],true,"{v}"); assert_eq!(failed["previous_definer_preserved"],false,"{v}");
    let actual=definer(&mut fresh,"e").unwrap(); assert_ne!(actual,format!("{owner}@%"));
    let warning=v["program_warnings"].as_array().unwrap().iter().find(|w|w["code"]=="program_restore_definer_changed").unwrap();
    assert_eq!(warning["source"],format!("{owner}@%")); assert_eq!(warning["target"],actual);
    assert_eq!(failed["status"],status(&mut fresh,"e"));
}

// This is a finite grammar mutation set, not random fuzzing. The production preview,
// probe and single-statement CREATE paths are used; the checkpoint observes objects
// before post-CREATE reconciliation can conceal a lexer/rewrite mismatch.
#[test]
fn deterministic_header_variants_match_mysql_creation_or_fail_closed() {
    let Some(mut s)=Sandbox::new() else {return}; let mut admin=conn(&s.endpoint); table(&mut admin); s.dump();
    let original=s.manifest(); let target=s.other(true); let victim=s.other(true);
    let mut observer=conn(&target);
    admin.query_drop(format!("CREATE TABLE `{}`.sentinel(id INT)",victim.database)).unwrap();
    let current:String=observer.query_first("SELECT CURRENT_USER()").unwrap().unwrap();
    let qi=|name:&str|format!("`{}`",name.replace('`',"``"));
    let source=qi(&s.endpoint.database);
    let fragments=["/* note */ ","# note\n","# note\r\n","# note\r","# note","#\tnote\n","#\rnote\n","#\nnote\n",
        "-- note\n","--\tnote\n","--\rnote\n","-- \rnote\n","-- note\r\n","-- note\r","-- note",
        "--\nnote\n","--note\n","--\u{b}note\n","--\u{c}note\n","--\u{1}note\n",
        "/*! ENABLE */ ","/*!80000 ENABLE */ ","/*!00000 ENABLE */ ","/*M! ENABLE */ ","/*+ hint */ ","/* unterminated"];
    let spaces=[" ","\t","\n","\r\n","\u{c}","\u{b}","\u{a0}","\u{3000}"];
    let mut total=0; let mut refused=0; let mut created=0; let mut server_rejected=0;
    for mode in ["","NO_BACKSLASH_ESCAPES","ANSI_QUOTES","ANSI_QUOTES,NO_BACKSLASH_ESCAPES"] {
        for kind in ["PROCEDURE","FUNCTION","EVENT"] {
            let tail=match kind {
                "PROCEDURE"=>"() SQL SECURITY INVOKER COMMENT 'literal' SELECT 1",
                "FUNCTION"=>"() RETURNS INT DETERMINISTIC SQL SECURITY INVOKER COMMENT 'literal' RETURN 1",
                _=>"ON SCHEDULE EVERY 1 DAY STARTS '2099-01-01' ON COMPLETION PRESERVE ENABLE COMMENT 'literal' DO SET @header_test=1",
            };
            let base=format!("CREATE DEFINER=`not_importer`@`%` {kind} {source} . `p` {tail}");
            let mut positions=vec!["CREATE ".to_string(),"DEFINER=".into(),"`not_importer`".into(),"@".into(),"`%` ".into(),format!("{kind} "),format!("{source} "),". ".into(),"`p` ".into()];
            if kind=="EVENT" { positions.extend(["ON ","SCHEDULE ","STARTS ","'2099-01-01' ","ON COMPLETION ","COMPLETION ","PRESERVE ","ENABLE ","DO "].map(str::to_string)); }
            let mut cases=vec![("baseline".to_string(),base.clone(),"p".to_string())];
            for (i,fragment) in fragments.iter().enumerate() {
                for index in [i%positions.len(),(i+positions.len()/2)%positions.len()] {
                    let needle=&positions[index];
                    cases.push((format!("comment-{i}-position-{index}"),base.replacen(needle,&format!("{needle}{fragment}"),1),"p".into()));
                }
            }
            for (index,needle) in positions.iter().enumerate() {
                for (i,space) in spaces.iter().enumerate() {
                    cases.push((format!("space-{i}-position-{index}"),base.replacen(needle,&format!("{}{space}",needle.trim_end()),1),"p".into()));
                }
            }
            for (i,literal) in ["'a\\'b'","'a''b'","\"a\\\"b\"","\"a\"\"b\"","'a\\\\b'","'/* ! */ # -- . ` data'","'line\r\nbreak'"].iter().enumerate() {
                cases.push((format!("literal-{i}"),base.replace("'literal'",literal),"p".into()));
                cases.push((format!("definer-quote-{i}"),base.replace("`not_importer`",literal),"p".into()));
            }
            for (i,name) in ["p`tick","p.dot","p'quote","p\"quote","p# -- /*data*/","한글_p"].iter().enumerate() {
                cases.push((format!("identifier-{i}"),base.replace("`p`",&qi(name)),name.to_string()));
            }
            for quoted in ["'p'","\"p\"","`p` . `outside`"] {
                cases.push((format!("name-{quoted}"),base.replace("`p`",quoted),"p".into()));
            }
            cases.push(("single-statement".into(),format!("{base}; INSERT INTO `{}`.sentinel VALUES(1)",victim.database),"p".into()));
            cases.push(("second-object".into(),format!("{base}; CREATE TABLE `{}`.second_object(id INT)",victim.database),"p".into()));
            if kind=="EVENT" {
                cases.push(("disabled-header".into(),base.replace(" ENABLE "," DISABLE "),"p".into()));
                for comment in ["#","-- "] {
                    cases.push((format!("hidden-cr-{comment}"),format!("CREATE EVENT `p` ON SCHEDULE EVERY 1 DAY {comment}x\rDISABLE\nDO SET @header_test=1"),"p".into()));
                    cases.push((format!("hidden-qualifier-{comment}"),format!("CREATE EVENT {} {comment}\rON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1 #\n.`p` ON SCHEDULE EVERY 1 DAY ENABLE DO SET @header_test=1",qi(&victim.database)),victim.database.clone()));
                }
            }
            if kind=="EVENT" && mode.is_empty() {
                let sql="CREATE EVENT `p` ON SCHEDULE EVERY '1\\' DAY STARTS '2099-01-01' ENABLE #fake' DISABLE\nDO SET @header_test=1";
                for label in ["mode-case-lower","mode-case-mixed","mode-case-spaced","mode-case-canonical"] {
                    cases.push((label.into(),sql.into(),"p".into()));
                }
            }
            for (label,definition,name) in cases {
                total+=1; assert!(total<=2300,"mutation set exceeded its CI budget");
                let mode=match label.as_str() { "mode-case-lower"=>"no_backslash_escapes", "mode-case-mixed"=>"Ansi_Quotes,no_Backslash_Escapes", "mode-case-spaced"=>" NO_BACKSLASH_ESCAPES ", "mode-case-canonical"=>"NO_BACKSLASH_ESCAPES", _=>mode };
                let mut program=json!({"kind":kind,"name":name,"definition":definition,"sql_mode":mode,"character_set_client":"utf8mb4","collation_connection":"utf8mb4_bin","definer":"not_importer@%"});
                if kind=="EVENT" { program["time_zone"]=json!("+00:00"); program["status"]=json!("DISABLED"); }
                let mut m=original.clone(); m["format_version"]=json!(7); m["stored_programs"]=json!([program]); s.save(&m);
                let preview=success(events("dump.import_preview",s.payload(&target)));
                let accepted=preview["blockers"].as_array().unwrap().is_empty();
                let mut at_create=false; let mut output=Vec::new();
                handle_request_streaming(Request{command:"dump.import".into(),request_id:None,payload:s.payload(&target)},|e| {
                    if e["event"]=="program_created" {
                        assert!(accepted,"rejected header executed: {kind}/{mode}/{label}: {definition:?}");
                        assert_eq!(e["name"],name); at_create=true;
                        if kind=="EVENT" { assert_eq!(status(&mut observer,&name),"DISABLED","{mode}/{label}: {definition:?}"); }
                    }
                    output.push(e);
                });
                let objects:Vec<(String,String,String,String)>=observer.exec("SELECT ROUTINE_SCHEMA,ROUTINE_NAME,ROUTINE_TYPE,DEFINER FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA IN (?,?,?) UNION ALL SELECT EVENT_SCHEMA,EVENT_NAME,'EVENT',DEFINER FROM information_schema.EVENTS WHERE EVENT_SCHEMA IN (?,?,?) UNION ALL SELECT TRIGGER_SCHEMA,TRIGGER_NAME,'TRIGGER',DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA IN (?,?,?)",(&s.endpoint.database,&target.database,&victim.database,&s.endpoint.database,&target.database,&victim.database,&s.endpoint.database,&target.database,&victim.database)).unwrap();
                if !objects.is_empty() {
                    assert!(accepted && at_create,"unreported object: {kind}/{mode}/{label}: {definition:?} {objects:?} {output:#?}");
                    assert_eq!(objects,vec![(target.database.clone(),name.clone(),kind.into(),current.clone())],"{mode}/{label}: {definition:?}");
                    created+=1;
                    assert_eq!(output.iter().find(|e|e["event"]=="result").unwrap()["success"],true,"{output:#?}");
                    observer.query_drop(format!("DROP {kind} {}",qi(&name))).unwrap();
                } else {
                    assert!(!at_create,"successful CREATE left no object: {mode}/{label}");
                    assert!(output.iter().any(|e|e["event"]=="error" || (e["event"]=="result" && e["success"]==false)),"silent loss: {output:#?}");
                    if accepted { server_rejected+=1; } else { refused+=1; }
                }
                let sentinel:Option<u64>=observer.query_first(format!("SELECT COUNT(*) FROM `{}`.sentinel",victim.database)).unwrap(); assert_eq!(sentinel,Some(0),"second statement ran: {definition:?}");
                let tables:Vec<(String,String)>=observer.exec("SELECT TABLE_SCHEMA,TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA IN (?,?,?) ORDER BY TABLE_SCHEMA,TABLE_NAME",(&s.endpoint.database,&target.database,&victim.database)).unwrap();
                let mut expected=vec![(s.endpoint.database.clone(),"sample".into()),(target.database.clone(),"sample".into()),(victim.database.clone(),"sentinel".into())]; expected.sort();
                assert_eq!(tables,expected,"unexpected table or view: {definition:?}");
            }
        }
    }
    assert!(created>200 && refused>100 && server_rejected>0,"vacuous differential test");
    println!("header differential: total={total}, created={created}, refused={refused}, server_rejected={server_rejected}");
}
