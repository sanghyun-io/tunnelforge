//! TF-STATUS-209: PostgreSQL key rules (NULLS NOT DISTINCT, DEFERRABLE, btree EXCLUDE) and keys read
//! by a SELECT-only role.
use super::support::*;
use migration_core::{Endpoint, LiveAdapter, MigrationAdapter};
use mysql::prelude::Queryable;
use serde_json::json;
use std::collections::BTreeMap;

struct Roles { endpoint:Endpoint, names:Vec<String> }
impl Drop for Roles {
    fn drop(&mut self) {
        let mut admin=db(&self.endpoint);
        for name in &self.names { let _=admin.batch_execute(&format!("DROP OWNED BY {name}; DROP ROLE {name}")); }
    }
}

/// Every index definition (schema stripped) and every FK, EXCLUDE or deferrable constraint definition.
fn key_definitions(client:&mut postgres::Client) -> BTreeMap<String,String> {
    client.query("SELECT i.relname::text, regexp_replace(pg_get_indexdef(i.oid),' ON [^ ]+\\.',' ON ') FROM pg_index ix JOIN pg_class i ON i.oid=ix.indexrelid WHERE i.relnamespace=current_schema()::regnamespace \
                  UNION ALL SELECT 'constraint '||c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid \
                  WHERE t.relnamespace=current_schema()::regnamespace AND (c.contype IN ('f','x') OR c.condeferrable)",&[]).unwrap()
        .into_iter().map(|row|(row.get(0),row.get(1))).collect()
}

fn sqlstate(client:&mut postgres::Client, sql:&str) -> String {
    client.batch_execute(sql).unwrap_err().code().unwrap().code().to_string()
}

#[test]
fn pg_key_rules_survive_replace_and_safe() {
    let Some(s)=Sandbox::new() else {return};
    let mut source=db(&s.source);
    source.batch_execute("CREATE TABLE p(id int PRIMARY KEY, d int, n int, a int, b int, \
        CONSTRAINT dk UNIQUE(d) DEFERRABLE INITIALLY DEFERRED, CONSTRAINT nk UNIQUE NULLS NOT DISTINCT (n), \
        CONSTRAINT ex EXCLUDE USING btree (b WITH =) DEFERRABLE); \
        CREATE UNIQUE INDEX u ON p(a) NULLS NOT DISTINCT; \
        CREATE TABLE c(id int PRIMARY KEY, pid int, CONSTRAINT c_parent FOREIGN KEY(pid) REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED); \
        CREATE TABLE q(id int PRIMARY KEY, code text UNIQUE); \
        INSERT INTO p VALUES(1,1,NULL,NULL,1); INSERT INTO c VALUES(1,1); INSERT INTO q VALUES(1,'a')").unwrap();
    let exported=s.dump();
    assert_eq!(exported["format_version"],8,"{exported:#}");
    assert_eq!(exported["strict_export"],true,"{exported:#}");
    let manifest=s.manifest();
    let table=|name:&str| manifest["schema"]["tables"].as_array().unwrap().iter().find(|t|t["name"]==name).unwrap().clone();
    let index=|t:&serde_json::Value,name:&str| t["indexes"].as_array().unwrap().iter().find(|i|i["name"]==name).unwrap().clone();
    let p=table("p");
    assert_eq!(index(&p,"dk")["deferrable"],"INITIALLY DEFERRED");
    assert_eq!(index(&p,"nk")["nulls_not_distinct"],true);
    assert_eq!(index(&p,"u")["nulls_not_distinct"],true);
    assert_eq!(index(&p,"ex")["exclusion"],true);
    assert_eq!(index(&p,"ex")["deferrable"],"INITIALLY IMMEDIATE");
    assert_eq!(table("c")["foreign_keys"][0]["deferrable"],"INITIALLY DEFERRED");
    let plain=index(&table("q"),"q_code_key");
    assert!(plain.get("nulls_not_distinct").is_none() && plain.get("exclusion").is_none() && plain.get("deferrable").is_none(),"{plain:#}");

    run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    let mut target=db(&s.target);
    assert_eq!(key_definitions(&mut source),key_definitions(&mut target));
    // Each row differs from row 1 everywhere except the column under test.
    assert_eq!(sqlstate(&mut target,"INSERT INTO p(id,n,a,b) VALUES(10,10,NULL,10)"),"23505","NULLS NOT DISTINCT kept");
    assert_eq!(sqlstate(&mut target,"INSERT INTO p(id,n,a,b) VALUES(11,11,11,1)"),"23P01","EXCLUDE kept");
    target.batch_execute("BEGIN; INSERT INTO c VALUES(2,20); INSERT INTO p(id,n,a,b) VALUES(20,20,20,20); COMMIT").unwrap();
    target.batch_execute("BEGIN; INSERT INTO p VALUES(30,1,30,30,30); UPDATE p SET d=2 WHERE id=30; COMMIT").unwrap();

    // The target now holds the same rules (and q's plain UNIQUE constraint): a safe restore must verify.
    target.batch_execute("DELETE FROM c WHERE id=2; DELETE FROM p WHERE id IN (20,30)").unwrap();
    let safe=run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"safe"}));
    assert_eq!(safe["ready_for_switch"],true,"{safe:#}");
    let candidate=safe["candidate_target"]["schema"].as_str().unwrap().to_string();
    target.batch_execute(&format!("DROP SCHEMA \"{candidate}\" CASCADE")).unwrap();
}

#[test]
fn pg_unkeepable_key_rules_refused() {
    let Some(s)=Sandbox::new() else {return};
    db(&s.source).batch_execute("CREATE TABLE r(id int, span int4range, CONSTRAINT r_pk PRIMARY KEY(id) DEFERRABLE, \
        CONSTRAINT no_overlap EXCLUDE USING gist (span WITH &&)); CREATE TABLE other(id int PRIMARY KEY)").unwrap();
    for allow in [false,true] {
        let refused=response("dump.run",json!({"source":s.source,"output_dir":s.dir,"allow_incomplete":allow}));
        assert_eq!(refused["event"],"error","{refused:#}");
        assert_eq!(refused["bypassable"],false);
        assert_eq!(refused["reasons"],json!({
            "key_rule:r:r_pk":"deferrable_primary_key_unsupported",
            "key_rule:r:no_overlap":"exclusion_constraint_unsupported"
        }));
        assert!(!refused.to_string().contains("unsupported_index:r:no_overlap"),"EXCLUDE listed once");
    }
    let other=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["other"]}));
    assert_eq!(other["format_version"],3);
}

#[test]
fn pg_select_only_role_keeps_keys_and_sees_triggers() {
    let Some(s)=Sandbox::new() else {return};
    let schema=s.source.schema.clone().unwrap();
    let reader=format!("{schema}_r");
    let _roles=Roles {endpoint:s.source.clone(),names:vec![reader.clone()]};
    db(&s.source).batch_execute(&format!("CREATE TABLE k(id int PRIMARY KEY, code text UNIQUE); \
        CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$; \
        CREATE TRIGGER t BEFORE INSERT ON k FOR EACH ROW EXECUTE FUNCTION touch(); \
        CREATE ROLE {reader} LOGIN PASSWORD 'tf_local_test'; GRANT USAGE ON SCHEMA {schema} TO {reader}; GRANT SELECT ON k TO {reader}")).unwrap();
    let restricted=Endpoint {user:reader,..s.source.clone()};
    let inspected=migration_core::inspect_live(&restricted).unwrap();
    let table=&inspected.schema.tables[0];
    assert!(table.columns.iter().any(|c|c.name=="id" && c.primary_key),"PK hidden from a SELECT-only role: {table:?}");
    assert!(table.columns.iter().any(|c|c.name=="code" && c.unique));
    assert!(inspected.unsupported_objects.contains(&"trigger:k:t".to_string()),"{:?}",inspected.unsupported_objects);
    let exported=run("dump.run",json!({"source":restricted,"output_dir":s.dir,"allow_incomplete":true,"data_format":"jsonl","compression":"none"}));
    assert!(exported["manifest_warnings"].to_string().contains("trigger:k:t"),"{exported:#}");
    assert_eq!(s.manifest()["schema"]["tables"][0]["columns"][0]["primary_key"],true);
}

#[test]
fn pg_plain_keys_keep_format_3_and_tampered_rules_refused() {
    let Some(s)=Sandbox::new() else {return};
    db(&s.source).batch_execute("CREATE TABLE parent(id int PRIMARY KEY, code text UNIQUE); CREATE TABLE sample(id int PRIMARY KEY, v int, pid int REFERENCES parent(id)); \
        CREATE INDEX sample_v ON sample(v); INSERT INTO parent VALUES(1,'a'); INSERT INTO sample VALUES(1,1,1)").unwrap();
    assert_eq!(s.dump()["format_version"],3);
    let raw=std::fs::read_to_string(s.dir.join("_tunnelforge_dump.json")).unwrap();
    for key in ["nulls_not_distinct","exclusion","deferrable"] { assert!(!raw.contains(key),"{key} in a plain dump"); }
    s.sentinel();
    let mut manifest=s.manifest();
    let sample=manifest["schema"]["tables"].as_array_mut().unwrap().iter_mut().find(|t|t["name"]=="sample").unwrap();
    sample["indexes"][0]["deferrable"]=json!("INITIALLY DEFERRED");
    s.write_manifest(&manifest);
    let refused=response("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    assert!(refused.to_string().contains("export_invalid:"),"{refused:#}");
    assert!(refused.to_string().contains("invalid key rule on index sample_v"),"{refused:#}");
    s.assert_untouched();
}

#[test]
fn pg_old_dumps_in_safe_restore() {
    let Some(s)=Sandbox::new() else {return};
    let ddl="CREATE TABLE p(id int PRIMARY KEY); CREATE TABLE c(id int PRIMARY KEY, pid int, CONSTRAINT c_parent FOREIGN KEY(pid) REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED); \
        CREATE TABLE q(id int PRIMARY KEY, code text UNIQUE); INSERT INTO p VALUES(1); INSERT INTO c VALUES(1,1); INSERT INTO q VALUES(1,'a')";
    db(&s.source).batch_execute(ddl).unwrap();
    db(&s.target).batch_execute(ddl).unwrap();
    assert_eq!(s.dump()["format_version"],8);
    // A dump from before format 8 carries no deferral: the live rule would be weakened, so review.
    let mut manifest=s.manifest();
    manifest["format_version"]=json!(3);
    for table in manifest["schema"]["tables"].as_array_mut().unwrap() {
        for fk in table["foreign_keys"].as_array_mut().unwrap() { fk.as_object_mut().unwrap().remove("deferrable"); }
    }
    s.write_manifest(&manifest);
    let mut target=db(&s.target);
    let safe=run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"safe"}));
    if let Some(candidate)=safe["candidate_target"]["schema"].as_str() { target.batch_execute(&format!("DROP SCHEMA \"{candidate}\" CASCADE")).unwrap(); }
    assert_eq!(safe["status"],"ready_for_review","{safe:#}");
    assert_eq!(safe["unrepresented_original_objects"],json!(["key_rule:c:c_parent"]),"{safe:#}");
    // A plain format-3 dump of a table with a UNIQUE constraint verifies (it returns as a unique index).
    std::fs::remove_dir_all(&s.dir).unwrap();
    let plain=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["q"],"data_format":"jsonl","compression":"none"}));
    assert_eq!(plain["format_version"],3);
    let safe=run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"safe"}));
    if let Some(candidate)=safe["candidate_target"]["schema"].as_str() { target.batch_execute(&format!("DROP SCHEMA \"{candidate}\" CASCADE")).unwrap(); }
    assert_eq!(safe["ready_for_switch"],true,"{safe:#}");
}

#[test]
fn pg_nulls_not_distinct_primary_key_is_not_a_deferrable_one() {
    // PostgreSQL 15 lets ADD PRIMARY KEY USING INDEX take a NULLS NOT DISTINCT index; 16+ refuses it,
    // so the catalog state such a database keeps is set directly (postgres is a superuser in CI).
    let Some(s)=Sandbox::new() else {return};
    let mut source=db(&s.source);
    source.batch_execute("CREATE TABLE t(id int, CONSTRAINT t_pk PRIMARY KEY(id)); INSERT INTO t VALUES(1); \
        UPDATE pg_index SET indnullsnotdistinct=true WHERE indexrelid='t_pk'::regclass").unwrap();
    let exported=s.dump();
    assert_eq!(exported["format_version"],3,"{exported:#}");
}

#[test]
fn pg_key_rules_in_cross_engine_migrate() {
    let Some(s)=Sandbox::new() else {return};
    let host=std::env::var("TF_MYSQL_HOST").expect("MySQL is required for the cross-engine migrate regression");
    let base=Endpoint {engine:"mysql".into(),host,port:3306,user:"root".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:None,tls:Default::default()};
    let name=s.source.schema.clone().unwrap();
    let mut root=LiveAdapter::connect(&base).unwrap();
    for database in [name.clone(),format!("{name}_2")] { root.execute_sql(&format!("CREATE DATABASE {database}")).unwrap(); }
    let target=Endpoint {database:name.clone(),..base.clone()};
    let second=Endpoint {database:format!("{name}_2"),..base};
    let mut source=db(&s.source);
    source.batch_execute("CREATE TABLE p(id int PRIMARY KEY); CREATE TABLE c(id int PRIMARY KEY, pid int, CONSTRAINT c_parent FOREIGN KEY(pid) REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED); \
        INSERT INTO p VALUES(1); INSERT INTO c VALUES(1,1)").unwrap();
    let payload=|target:&Endpoint| {
        let inspected=run("inspect",json!({"source":s.source}));
        json!({"source_engine":"postgresql","target_engine":"mysql","source":s.source,"target":target,
            "schema":inspected["schema"],"unsupported_objects":inspected["unsupported_objects"]})
    };
    let blocking=|payload:&serde_json::Value| migration_core::preflight_issues(payload).into_iter().filter(|issue|issue.blocking).map(|issue|issue.location).collect::<Vec<_>>();
    // Django's DEFERRABLE foreign keys migrate (owner decision); the preflight only warns.
    let django=payload(&target);
    assert!(blocking(&django).is_empty(),"{:?}",blocking(&django));
    let migrated=response("migrate",django);
    let mut mysql=LiveAdapter::connect(&target).unwrap();
    let LiveAdapter::MySql(conn)=&mut mysql else {unreachable!()};
    let fks:Option<i64>=conn.exec_first("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=? AND CONSTRAINT_NAME='c_parent'",(name.as_str(),)).unwrap();
    // A gist EXCLUDE blocks before the target changes, and leaving its table out lets the rest go.
    source.batch_execute("CREATE TABLE r(id int PRIMARY KEY, span int4range, CONSTRAINT no_overlap EXCLUDE USING gist (span WITH &&))").unwrap();
    let excluded=payload(&second);
    let refused=response("migrate",excluded.clone());
    let mut later=LiveAdapter::connect(&second).unwrap();
    let LiveAdapter::MySql(later_conn)=&mut later else {unreachable!()};
    let created:Option<i64>=later_conn.exec_first("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=?",(format!("{name}_2"),)).unwrap();
    let mut without_r=excluded.clone();
    without_r["schema"]["tables"].as_array_mut().unwrap().retain(|table|table["name"]!="r");
    let partial=blocking(&without_r);
    for database in [name.clone(),format!("{name}_2")] { root.execute_sql(&format!("DROP DATABASE {database}")).unwrap(); }
    assert_eq!(migrated["success"],true,"{migrated:#}");
    assert_eq!(fks,Some(1));
    assert_eq!(blocking(&excluded),vec!["r.no_overlap".to_string()]);
    assert!(refused.to_string().contains("cannot keep PostgreSQL key rule r.no_overlap"),"{refused:#}");
    assert_eq!(created,Some(0),"the refusal must come before the target changes");
    assert!(partial.is_empty(),"a table left out of the migration must not block it: {partial:?}");
}

#[test]
fn pg_key_rules_into_mysql() {
    let Some(s)=Sandbox::new() else {return};
    let host=std::env::var("TF_MYSQL_HOST").expect("MySQL is required for the cross-engine key-rule regression");
    let base=Endpoint {engine:"mysql".into(),host,port:3306,user:"root".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:None,tls:Default::default()};
    let name=s.source.schema.clone().unwrap();
    let mut root=LiveAdapter::connect(&base).unwrap();
    root.execute_sql(&format!("CREATE DATABASE {name}")).unwrap();
    let target=Endpoint {database:name.clone(),..base};
    let mut source=db(&s.source);
    // Django's shape: every foreign key is DEFERRABLE INITIALLY DEFERRED.
    source.batch_execute("CREATE TABLE p(id int PRIMARY KEY); CREATE TABLE c(id int PRIMARY KEY, pid int, CONSTRAINT c_parent FOREIGN KEY(pid) REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED); \
        INSERT INTO p VALUES(1); INSERT INTO c VALUES(1,1)").unwrap();
    assert_eq!(s.dump()["format_version"],8);
    let preview=run("dump.import_preview",json!({"target":target,"input_dir":s.dir,"mode":"replace"}));
    let deferred=preview["warnings"].as_array().unwrap().iter().find(|w|w["code"]=="constraint_deferral_not_kept").cloned();
    assert_eq!(deferred.as_ref().map(|w|w["names"].clone()),Some(json!(["c.c_parent"])),"{preview:#}");
    let imported=run("dump.import",json!({"target":target,"input_dir":s.dir,"mode":"replace"}));
    assert!(imported["program_warnings"].to_string().contains("constraint_deferral_not_kept"),"{imported:#}");
    let mut mysql=LiveAdapter::connect(&target).unwrap();
    let LiveAdapter::MySql(conn)=&mut mysql else {unreachable!()};
    let fks:Option<i64>=conn.exec_first("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=? AND CONSTRAINT_NAME='c_parent'",(name.as_str(),)).unwrap();
    assert_eq!(fks,Some(1));

    source.batch_execute("CREATE UNIQUE INDEX p_one_null ON p(id) NULLS NOT DISTINCT").unwrap();
    std::fs::remove_dir_all(&s.dir).unwrap();
    s.dump();
    conn.query_drop("INSERT INTO p VALUES(91)").unwrap();
    let refused=response("dump.import",json!({"target":target,"input_dir":s.dir,"mode":"replace"}));
    let text=refused.to_string();
    let rows:Vec<i32>=conn.query("SELECT id FROM p ORDER BY id").unwrap();
    root.execute_sql(&format!("DROP DATABASE {name}")).unwrap();
    assert!(text.contains("unsupported_constraint_behavior:"),"{refused:#}");
    assert_eq!(rows,vec![1,91],"the refusal must come before any target change");
}
