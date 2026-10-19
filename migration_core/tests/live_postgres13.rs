//! Version-boundary cases run separately: TF_POSTGRES13_HOST is mandatory in live CI.
#[path="support/postgres_objects.rs"]
mod support;
use support::*;
use serde_json::json;

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
