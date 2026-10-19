use super::support::*;
use migration_core::{Endpoint,LiveAdapter,MigrationAdapter};
use serde_json::json;

struct Role { endpoint:Endpoint, name:String }
impl Drop for Role {
    fn drop(&mut self) { let _=db(&self.endpoint).batch_execute(&format!("DROP OWNED BY {}; DROP ROLE {}",self.name,self.name)); }
}

#[test]
fn source_read_transactions_restore_defaults_and_classify_translated_rls() {
    let Some(s)=Sandbox::new() else {return};
    let schema=s.source.schema.as_deref().unwrap();
    let name=format!("{schema}_reader");
    let _role=Role {endpoint:s.source.clone(),name:name.clone()};
    let mut admin=db(&s.source);
    admin.batch_execute(&format!("CREATE ROLE {name} LOGIN PASSWORD 'tf_local_test'; GRANT USAGE ON SCHEMA {schema} TO {name}; CREATE TABLE secured(id int PRIMARY KEY); INSERT INTO secured VALUES(1),(2),(3); ALTER TABLE secured ENABLE ROW LEVEL SECURITY; CREATE POLICY p ON secured USING(id=1); GRANT SELECT ON secured TO {name}; CREATE TABLE plain(id int); INSERT INTO plain VALUES(7); GRANT SELECT ON plain TO {name}; CREATE TABLE denied(id int)")).unwrap();
    let translated=admin.batch_execute(&format!("ALTER ROLE {name} SET lc_messages='ko_KR.UTF-8'"))
        .map(|_|true).unwrap_or_else(|err|{println!("Korean locale unavailable: {err}");false});
    println!("Korean server messages available: {translated}");
    let reader=Endpoint {user:name,..s.source.clone()};
    let model=migration_core::inspect_live(&s.source).unwrap().schema;
    let table=model.tables.iter().find(|table|table.name=="secured").unwrap();
    let mut source=LiveAdapter::connect_source(&reader).unwrap();
    assert_eq!(source.row_count("plain").unwrap(),1);
    assert!(source.row_count("secured").unwrap_err().starts_with("row_security_blocks_read:"));
    assert!(source.read_rows(table,0,1).unwrap_err().starts_with("row_security_blocks_read:"));
    assert!(source.read_rows_after_key(table,&["id".into()],None,1).unwrap_err().starts_with("row_security_blocks_read:"));
    assert!(source.read_rows_by_keys(table,&["id".into()],&[vec!["1".into()]]).unwrap_err().starts_with("row_security_blocks_read:"));
    let denied=source.row_count("denied").unwrap_err();
    assert!(denied.contains("42501") && !denied.contains("row_security_blocks_read"),"{denied}");
    assert_eq!(source.row_count("plain").unwrap(),1,"failed units must roll back");
    let options=serde_json::from_value(json!({})).unwrap();
    let mut target=migration_core::MemoryAdapter::default();
    let plain=migration_core::NormalizedSchema {tables:vec![model.tables.iter().find(|table|table.name=="plain").unwrap().clone()]};
    let migrated=migration_core::migrate_with_adapters(&plain,&options,None,&mut source,&mut target,"postgresql","mysql");
    assert!(migrated.success,"{migrated:?}");
    assert_eq!(migrated.rows_copied,1);
    let secured=migration_core::NormalizedSchema {tables:vec![table.clone()]};
    let refused=migration_core::migrate_with_adapters(&secured,&options,None,&mut source,&mut target,"postgresql","mysql");
    assert!(!refused.success && serde_json::to_string(&refused).unwrap().contains("row_security_blocks_read:"));
    let LiveAdapter::PostgreSql(mut client)=source.into_inner() else {unreachable!()};
    assert_eq!(client.query_one("SHOW row_security",&[]).unwrap().get::<_,String>(0),"on");
    assert_eq!(client.query_one("SELECT count(*) FROM secured",&[]).unwrap().get::<_,i64>(0),1);
    if translated {
        assert_eq!(client.query_one("SHOW lc_messages",&[]).unwrap().get::<_,String>(0),"ko_KR.UTF-8");
        client.batch_execute("BEGIN; SET LOCAL row_security=off").unwrap();
        let error=client.query("SELECT * FROM secured",&[]).unwrap_err();
        assert!(!error.as_db_error().unwrap().message().contains("row-level security"));
        client.batch_execute("ROLLBACK").unwrap();
    }
    let failed=response("dump.run",json!({"source":reader,"output_dir":s.dir,"tables":["secured"],"allow_incomplete":true}));
    assert_eq!(failed["error_code"],"row_security_blocks_read","{failed:#}");
    assert!(!s.dir.exists());
    // A translated temporal-value preflight must retain the same source error code.
    admin.batch_execute("ALTER TABLE secured ADD COLUMN happened timestamp").unwrap();
    let target=json!({"engine":"mysql","host":"unused","port":3306,"user":"unused","password":"dummy","database":"unused"});
    let failed=response("migrate",json!({"source":reader,"target":target,"source_engine":"postgresql","target_engine":"mysql","schema":migration_core::inspect_live(&s.source).unwrap().schema}));
    assert!(failed.to_string().contains("row_security_blocks_read:"),"{failed:#}");
}

#[test]
fn sql_json_generated_columns_round_trip_replace_and_safe() {
    let Some(s)=Sandbox::new() else {return};
    db(&s.source).batch_execute("CREATE TABLE docs(id int PRIMARY KEY,j jsonb,name text GENERATED ALWAYS AS(JSON_VALUE(j,'$.name')) STORED,tags jsonb GENERATED ALWAYS AS(JSON_QUERY(j,'$.tags')) STORED); INSERT INTO docs(id,j) VALUES(1,'{\"name\":\"kept\",\"tags\":[\"a\",\"b\"]}')").unwrap();
    assert_eq!(s.dump()["format_version"],6);
    let expected=db(&s.source).query_one("SELECT name,tags::text FROM docs",&[]).unwrap();
    run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    let actual=db(&s.target).query_one("SELECT name,tags::text FROM docs",&[]).unwrap();
    for index in 0..2 {assert_eq!(expected.get::<_,String>(index),actual.get::<_,String>(index));}
    let safe=run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"safe"}));
    assert_eq!(safe["ready_for_switch"],true,"{safe:#}");
    let candidate=safe["candidate_target"]["schema"].as_str().unwrap();
    let actual=db(&s.target).query_one(&format!("SELECT name,tags::text FROM \"{candidate}\".docs"),&[]).unwrap();
    for index in 0..2 {assert_eq!(expected.get::<_,String>(index),actual.get::<_,String>(index));}
    db(&s.target).batch_execute(&format!("DROP SCHEMA \"{candidate}\" CASCADE")).unwrap();
}
