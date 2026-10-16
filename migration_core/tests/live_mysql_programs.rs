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
    let imported=success(events("dump.import",s.payload(&target))); assert_eq!(imported["programs_imported"].as_array().unwrap().len(),7);
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
    let log:bool=db.query_first("SELECT @@log_bin").unwrap().unwrap(); if !log { eprintln!("SKIP: binary log off"); return }
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
    let other=s.other(true); let warned=success(events("dump.import",s.payload(&other)));
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
