//! Version-boundary cases run separately: TF_POSTGRES13_HOST is mandatory in live CI.
#[path="support/postgres_objects.rs"]
mod support;
use support::*;
use serde_json::json;

#[test]
fn pg13_json_identifier_imports_into_pg18_replace_and_safe() {
    let Some(old)=endpoint("TF_POSTGRES13") else {return};
    let Some(new)=endpoint("TF_POSTGRES") else {return};
    let source=Sandbox::on(old);let target=Sandbox::on(new);
    db(&source.source).batch_execute("CREATE TABLE doc(id int PRIMARY KEY,json jsonb,name text GENERATED ALWAYS AS(json->>'name') STORED); INSERT INTO doc(id,json) VALUES(1,'{\"name\":\"source\"}')").unwrap();
    assert_eq!(source.dump()["format_version"],6);
    assert!(!source.manifest()["schema"]["tables"][0]["columns"][2]["generated"]["expression"].as_str().unwrap().contains("\"json\""));
    db(&target.target).batch_execute("CREATE TABLE doc(id int PRIMARY KEY,json jsonb,name text GENERATED ALWAYS AS(json->>'name') STORED); INSERT INTO doc(id,json) VALUES(91,'{\"name\":\"old\"}')").unwrap();
    run("dump.import",json!({"target":target.target,"input_dir":source.dir,"mode":"replace"}));
    assert_eq!(db(&target.target).query_one("SELECT name FROM doc",&[]).unwrap().get::<_,String>(0),"source");
    let safe=run("dump.import",json!({"target":target.target,"input_dir":source.dir,"mode":"safe"}));
    assert_eq!(safe["ready_for_switch"],true,"{safe:#}");
    assert!(!safe["program_warnings"].to_string().contains("generated_values_recomputed"));
    let candidate=safe["candidate_target"]["schema"].as_str().unwrap();
    let mut admin=db(&target.target);
    let name:String=admin.query_one(&format!("SELECT name FROM \"{candidate}\".doc"),&[]).unwrap().get(0);
    admin.batch_execute(&format!("DROP SCHEMA \"{candidate}\" CASCADE")).unwrap();
    assert_eq!(name,"source");
}

#[test]
fn pg_virtual_into_old_target_refused() {
    let Some(base)=endpoint("TF_POSTGRES13") else { return };
    let s=Sandbox::on(base);
    db(&s.source).batch_execute("CREATE TABLE sample(id int PRIMARY KEY,g int GENERATED ALWAYS AS(id+1) STORED); INSERT INTO sample(id) VALUES(1)").unwrap();
    s.dump(); s.sentinel();
    // The existing format represents VIRTUAL by stored=false; no PG18 source is needed here.
    let mut manifest=s.manifest(); manifest["schema"]["tables"][0]["columns"][1]["generated"]["stored"]=json!(false); s.write_manifest(&manifest);
    let version:String=db(&s.target).query_one("SHOW server_version_num",&[]).unwrap().get(0);
    assert!(version.parse::<u32>().unwrap()<180000,"this binary requires an old PostgreSQL target");
    let refused=response("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    assert!(refused.to_string().contains("generated_virtual_unsupported_on_target:"),"{refused:#}");
    s.assert_untouched();
    let preview=run("dump.import_preview",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    assert!(preview["blockers"].as_array().unwrap().iter().any(|blocker|blocker["code"]=="generated_virtual_unsupported_on_target"),"{preview:#}");
}

#[test]
fn pg_nulls_not_distinct_into_old_target_refused() {
    let Some(base)=endpoint("TF_POSTGRES13") else { return };
    let s=Sandbox::on(base);
    db(&s.source).batch_execute("CREATE TABLE a(id int PRIMARY KEY, v int); CREATE UNIQUE INDEX a_v ON a(v); CREATE TABLE b(id int PRIMARY KEY); INSERT INTO a VALUES(1,1); INSERT INTO b VALUES(1)").unwrap();
    s.dump(); s.sentinel();
    // PostgreSQL 13 cannot make the rule, so the dump is edited the way a PG15+ source would write it.
    let mut manifest=s.manifest(); manifest["format_version"]=json!(8);
    let a=manifest["schema"]["tables"].as_array_mut().unwrap().iter_mut().find(|t|t["name"]=="a").unwrap();
    a["indexes"][0]["nulls_not_distinct"]=json!(true);
    s.write_manifest(&manifest);
    let refused=response("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    assert!(refused.to_string().contains("nulls_not_distinct_unsupported_on_target:"),"{refused:#}");
    s.assert_untouched();
    for mode in ["replace","safe"] {
        let preview=run("dump.import_preview",json!({"target":s.target,"input_dir":s.dir,"mode":mode}));
        assert!(preview["blockers"].as_array().unwrap().iter().any(|b|b["code"]=="nulls_not_distinct_unsupported_on_target"),"{preview:#}");
    }
    let copies="SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'tf_restore_%'";
    let before:i64=db(&s.target).query_one(copies,&[]).unwrap().get(0);
    let safe=response("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"safe"}));
    assert_eq!(safe["error_code"],"copy_refused","{safe:#}");
    assert!(safe.to_string().contains("nulls_not_distinct_unsupported_on_target"),"{safe:#}");
    assert_eq!(db(&s.target).query_one(copies,&[]).unwrap().get::<_,i64>(0),before,"no copy schema may be created");
    run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace","tables":["b"]}));
}

#[test]
fn pg13_key_rules_round_trip() {
    let Some(base)=endpoint("TF_POSTGRES13") else { return };
    let s=Sandbox::on(base);
    let definitions="SELECT conname::text, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid WHERE t.relnamespace=current_schema()::regnamespace AND (c.contype IN ('f','x') OR c.condeferrable) ORDER BY 1";
    let mut source=db(&s.source);
    source.batch_execute("CREATE TABLE p(id int PRIMARY KEY, d int, b int, CONSTRAINT dk UNIQUE(d) DEFERRABLE, CONSTRAINT ex EXCLUDE USING btree (b WITH =)); \
        CREATE TABLE c(id int PRIMARY KEY, pid int, CONSTRAINT c_parent FOREIGN KEY(pid) REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED); INSERT INTO p VALUES(1,1,1); INSERT INTO c VALUES(1,1)").unwrap();
    assert_eq!(s.dump()["format_version"],8);
    run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    let read=|client:&mut postgres::Client| client.query(definitions,&[]).unwrap().into_iter().map(|row|(row.get::<_,String>(0),row.get::<_,String>(1))).collect::<Vec<_>>();
    let expected=read(&mut source);
    assert_eq!(expected.len(),3,"{expected:?}");
    assert_eq!(read(&mut db(&s.target)),expected);
}

#[test]
fn pg13_stored_generated_columns_round_trip() {
    let Some(base)=endpoint("TF_POSTGRES13") else { return };
    let s=Sandbox::on(base);
    db(&s.source).batch_execute("CREATE TABLE sample(id int PRIMARY KEY,g int GENERATED ALWAYS AS(id+1) STORED NOT NULL); INSERT INTO sample(id) VALUES(1)").unwrap();
    assert_eq!(s.dump()["format_version"],6);
    run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    let mut target=db(&s.target);
    assert_eq!(target.query_one("SELECT g FROM sample",&[]).unwrap().get::<_,i32>(0),2);
    target.batch_execute("UPDATE sample SET id=4").unwrap();
    assert_eq!(target.query_one("SELECT g FROM sample",&[]).unwrap().get::<_,i32>(0),5);
}
