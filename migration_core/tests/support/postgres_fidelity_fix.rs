use super::support::*;
use migration_core::{Endpoint, LiveAdapter, MigrationAdapter};
use serde_json::json;
use mysql::prelude::Queryable;

struct Roles { endpoint:Endpoint, names:Vec<String> }
impl Drop for Roles {
    fn drop(&mut self) {
        let mut admin=db(&self.endpoint);
        for name in &self.names { let _=admin.batch_execute(&format!("DROP OWNED BY {name}; DROP ROLE {name}")); }
    }
}

#[test]
fn pg_fix_rls_copy_sources_fail_closed_but_editor_reads_stay_filtered() {
    let Some(s)=Sandbox::new() else {return};
    let schema=s.source.schema.as_deref().unwrap();
    let reader=format!("{schema}_r");let owner=format!("{schema}_o");
    let _roles=Roles {endpoint:s.source.clone(),names:vec![reader.clone(),owner.clone()]};
    let mut admin=db(&s.source);
    admin.batch_execute(&format!("CREATE ROLE {reader} LOGIN PASSWORD 'tf_local_test'; CREATE ROLE {owner} LOGIN PASSWORD 'tf_local_test'; GRANT USAGE ON SCHEMA {schema} TO {reader},{owner}; GRANT CREATE ON SCHEMA {schema} TO {owner}; CREATE TABLE sample(id int PRIMARY KEY); INSERT INTO sample VALUES(1),(2),(3); ALTER TABLE sample ENABLE ROW LEVEL SECURITY; CREATE POLICY p ON sample FOR SELECT USING(id=1); GRANT SELECT ON sample TO {reader}")).unwrap();
    let restricted=Endpoint {user:reader,..s.source.clone()};
    let model=migration_core::inspect_live(&s.source).unwrap().schema;
    assert_eq!(LiveAdapter::connect(&restricted).unwrap().row_count("sample").unwrap(),1);
    for actor in [&restricted,&Endpoint {user:owner.clone(),..s.source.clone()}] {
        if actor.user==owner {admin.batch_execute(&format!("ALTER TABLE sample OWNER TO {owner}; ALTER TABLE sample FORCE ROW LEVEL SECURITY")).unwrap();}
        let failed=response("dump.run",json!({"source":actor,"output_dir":s.dir,"allow_incomplete":true,"data_format":"jsonl","compression":"none"}));
        assert_eq!(failed["error_code"],"row_security_blocks_read","{failed:#}");
        assert!(!s.dir.exists(),"RLS must stop before creating the dump directory");
        let mut source=LiveAdapter::connect_source(actor).unwrap();
        assert!(source.row_count("sample").unwrap_err().starts_with("row_security_blocks_read:"));
        assert!(source.read_rows(&model.tables[0],0,1).unwrap_err().starts_with("row_security_blocks_read:"));
        assert!(source.read_rows_after_key(&model.tables[0],&["id".into()],None,1).unwrap_err().starts_with("row_security_blocks_read:"));
    }
    let full=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"allow_incomplete":true,"data_format":"jsonl","compression":"none"}));
    assert_eq!(full["rows_dumped"],3);
    // Actual migration, verification, and resumption must use the source-only setting too.
    let host=std::env::var("TF_MYSQL_HOST").expect("MySQL is required for the source-read regression");
    let base=Endpoint {engine:"mysql".into(),host,port:3306,user:"root".into(),password:"tf_local_test".into(),database:"tf_test".into(),schema:None,tls:Default::default()};
    let name=s.source.schema.clone().unwrap();
    let mut root=LiveAdapter::connect(&base).unwrap();root.execute_sql(&format!("CREATE DATABASE {name}")).unwrap();
    let target=Endpoint {database:name.clone(),..base};
    let mut target_db=LiveAdapter::connect(&target).unwrap();target_db.execute_sql("CREATE TABLE sample(id int PRIMARY KEY); INSERT INTO sample VALUES(91)").unwrap();
    let payload=json!({"source":restricted,"target":target,"source_engine":"postgresql","target_engine":"mysql","schema":model});
    let mut destructive=payload.clone();destructive["execution_options"]=json!({"cleanup_before_migrate":true});
    let failed=response("migrate",destructive);
    assert!(failed.to_string().contains("row_security_blocks_read:"),"{failed:#}");
    let LiveAdapter::MySql(conn)=&mut target_db else {unreachable!()};
    assert_eq!(conn.query_first::<i32,_>("SELECT id FROM sample").unwrap(),Some(91));
    let verified=response("verify",payload.clone());
    assert_eq!(verified["success"],false,"{verified:#}");
    assert!(verified.to_string().contains("row_security_blocks_read:"));
    target_db.execute_sql("DROP TABLE sample").unwrap();
    let mut initial=payload.clone();initial["source"]=json!(s.source);initial["execution_options"]=json!({"chunk_size":1,"cancel_after_chunks":1});
    let cancelled=response("migrate",initial);
    assert!(cancelled["state"].is_object(),"{cancelled:#}");
    let mut resumed=payload;resumed["state"]=cancelled["state"].clone();
    let failed=response("migrate",resumed);
    root.execute_sql(&format!("DROP DATABASE {name}")).unwrap();
    assert!(failed.to_string().contains("row_security_blocks_read:"),"{failed:#}");
}

#[test]
fn pg_fix_text_search_and_unreserved_column_names_round_trip() {
    let Some(s)=Sandbox::new() else {return};
    db(&s.source).batch_execute("CREATE TABLE docs(id int PRIMARY KEY, comment text, tsv tsvector GENERATED ALWAYS AS(to_tsvector('english',comment)) STORED, words text[] GENERATED ALWAYS AS(ts_lexize('english_stem'::regdictionary,comment)) STORED); INSERT INTO docs(id,comment) VALUES(1,'running')").unwrap();
    assert_eq!(s.dump()["format_version"],6);
    let done=run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    assert!(done["program_warnings"].to_string().contains("generated_values_recomputed"));
    let mut target=db(&s.target);
    assert_eq!(target.query_one("SELECT tsv::text,words::text FROM docs",&[]).unwrap().get::<_,String>(0),"'run':1");
    assert_eq!(target.query_one("SELECT words::text FROM docs",&[]).unwrap().get::<_,String>(0),"{run}");
    let safe=run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"safe"}));
    assert_eq!(safe["ready_for_switch"],true,"{safe:#}");
    assert!(!safe["program_warnings"].to_string().contains("generated_values_recomputed"));
    let candidate=safe["candidate_target"]["schema"].as_str().unwrap();
    db(&s.source).batch_execute(&format!("DROP SCHEMA \"{candidate}\" CASCADE")).unwrap();
}

#[test]
fn pg_fix_object_owners_scope_exact_names_and_keep_comment_identities() {
    let Some(s)=Sandbox::new() else {return};let mut source=db(&s.source);
    source.batch_execute("CREATE TABLE orders(id int); CREATE TABLE history(id int) PARTITION BY RANGE(id); CREATE TABLE \"orders:2024\" PARTITION OF history FOR VALUES FROM(0) TO(10)").unwrap();
    for allow in [false,true] {
        let done=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["orders"],"allow_incomplete":allow}));
        assert_eq!(done["strict_export"],true);std::fs::remove_dir_all(&s.dir).unwrap();
    }
    source.batch_execute("DROP TABLE history CASCADE; CREATE TABLE \"orders:2024\"(id int); CREATE RULE r AS ON DELETE TO \"orders:2024\" DO INSTEAD NOTHING; COMMENT ON TABLE \"orders:2024\" IS 'table'; COMMENT ON COLUMN \"orders:2024\".id IS 'column'; CREATE FUNCTION tg() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$; CREATE TRIGGER \"trg:x\" BEFORE INSERT ON \"orders:2024\" FOR EACH ROW EXECUTE FUNCTION tg(); CREATE TABLE \"items:x\"(id int CONSTRAINT \"chk:y\" CHECK(id>0))").unwrap();
    let plain=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["orders"]}));
    assert_eq!(plain["strict_export"],true);std::fs::remove_dir_all(&s.dir).unwrap();
    let refused=response("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["orders:2024"]}));
    assert_eq!(refused["event"],"error");
    assert_eq!(refused["object_tables"]["trigger:orders:2024:trg:x"],"orders:2024");
    assert_eq!(refused["object_tables"]["rule:orders:2024:r"],"orders:2024");
    let check=response("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["items:x"]}));
    assert_eq!(check["object_tables"]["check_constraint:items:x:chk:y"],"items:x");
    let bypassed=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["orders:2024"],"allow_incomplete":true}));
    assert_eq!(bypassed["strict_export"],false);
    std::fs::remove_dir_all(&s.dir).unwrap();
    source.batch_execute("ALTER TABLE orders ADD COLUMN \"TABLE\" int; COMMENT ON TABLE orders IS 'table'; COMMENT ON COLUMN orders.id IS 'id'; COMMENT ON COLUMN orders.\"TABLE\" IS 'column'").unwrap();
    let comments=run("dump.run",json!({"source":s.source,"output_dir":s.dir,"tables":["orders"]}));
    assert_eq!(comments["manifest_warnings"],json!(["comments_not_exported: comment:orders, comment:orders:TABLE, comment:orders:id"]));
    assert_eq!(comments["strict_export"],false);
    let preview=run("dump.import_preview",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    assert!(preview["warnings"].as_array().unwrap().iter().any(|w|w["code"]=="not_strict"));
}

#[test]
fn pg_fix_sequence_nondefault_range_cycle_and_type_keep_reason_code() {
    let Some(s)=Sandbox::new() else {return};
    db(&s.source).batch_execute("CREATE TABLE t(id serial PRIMARY KEY); ALTER TABLE t ALTER COLUMN id TYPE bigint; CREATE TABLE u(id integer GENERATED BY DEFAULT AS IDENTITY(MAXVALUE 1000)); CREATE TABLE w(id serial); ALTER SEQUENCE w_id_seq CYCLE").unwrap();
    let refused=response("dump.run",json!({"source":s.source,"output_dir":s.dir}));
    for id in ["unsupported_default:t:id","unsupported_default:u:id","unsupported_default:w:id"] {assert_eq!(refused["reasons"][id],"sequence_options_unsupported");}
}
