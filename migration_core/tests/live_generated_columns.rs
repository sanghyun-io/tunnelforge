//! Format 6 generated-column contract, exercised on MySQL 8.0 and 8.4 in CI.
use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn events(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request { command: command.into(), request_id: None, payload })
}
fn result(command: &str, payload: Value) -> Value {
    let events = events(command, payload);
    assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    events.into_iter().find(|e| e["event"] == "result").unwrap()
}
fn endpoint(engine: &str) -> Option<Endpoint> {
    let prefix = if engine == "mysql" { "TF_MYSQL" } else { "TF_POSTGRES" };
    let host = std::env::var(format!("{prefix}_HOST")).ok();
    assert!(host.is_some() || std::env::var_os("TF_LIVE_REQUIRED").is_none(), "{prefix}_HOST missing");
    Some(Endpoint {
        engine: engine.into(), host: host?,
        port: std::env::var(format!("{prefix}_PORT")).ok().and_then(|s| s.parse().ok()).unwrap_or(if engine == "mysql" { 3306 } else { 5432 }),
        user: std::env::var(format!("{prefix}_USER")).ok()?,
        password: std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),
        database: std::env::var(format!("{prefix}_DATABASE")).ok()?, schema: None, tls: Default::default(),
    })
}
struct LocalInfile { conn: mysql::PooledConn, previous: u8 }
impl LocalInfile {
    fn enabled(endpoint: &Endpoint) -> Self {
        let mut conn = conn(endpoint);
        let previous = conn.query_first("SELECT @@GLOBAL.local_infile").unwrap().unwrap();
        conn.query_drop("SET GLOBAL local_infile=1").unwrap();
        Self { conn, previous }
    }
}
impl Drop for LocalInfile {
    fn drop(&mut self) { let _ = self.conn.query_drop(format!("SET GLOBAL local_infile={}",self.previous)); }
}
struct Sandbox {
    endpoint: Endpoint,
    admin: LiveAdapter,
    databases: Vec<String>,
    output: PathBuf,
}
impl Sandbox {
    fn new() -> Option<Self> {
        let mut endpoint = endpoint("mysql")?;
        let mut admin = LiveAdapter::connect(&endpoint).unwrap();
        let name = format!("tf_gen_{}_{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        admin.execute_sql(&format!("CREATE DATABASE {name} CHARACTER SET utf8mb4")).unwrap();
        endpoint.database = name.clone();
        Some(Self { endpoint, admin, databases: vec![name.clone()], output: std::env::temp_dir().join(name) })
    }
    fn conn(&self) -> mysql::PooledConn {
        let LiveAdapter::MySql(conn) = LiveAdapter::connect(&self.endpoint).unwrap() else { unreachable!() };
        conn
    }
    fn dump(&self, format: &str, threads: usize) -> Value {
        result("dump.run", json!({"source":self.endpoint,"output_dir":self.output,"data_format":format,
            "compression":"none","threads":threads,"chunk_size":2,"mysql_snapshot_mode":"single_connection"}))
    }
    fn manifest(&self) -> Value {
        serde_json::from_slice(&std::fs::read(self.output.join("_tunnelforge_dump.json")).unwrap()).unwrap()
    }
    fn save(&self, manifest: &Value) {
        std::fs::write(self.output.join("_tunnelforge_dump.json"), serde_json::to_vec_pretty(manifest).unwrap()).unwrap();
    }
    fn remember_candidate(&mut self, report: &Value) -> Endpoint {
        let mut candidate = self.endpoint.clone();
        candidate.database = report["candidate_target"]["database"].as_str().unwrap().into();
        self.databases.push(candidate.database.clone());
        candidate
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        for name in &self.databases {
            let _ = self.admin.execute_sql(&format!("DROP DATABASE IF EXISTS `{name}`"));
        }
        let _ = std::fs::remove_dir_all(&self.output);
    }
}
fn conn(endpoint: &Endpoint) -> mysql::PooledConn {
    let LiveAdapter::MySql(conn) = LiveAdapter::connect(endpoint).unwrap() else { unreachable!() };
    conn
}
fn generated_metadata(conn: &mut mysql::PooledConn, table: &str) -> Vec<(String, String, String)> {
    conn.exec("SELECT COLUMN_NAME,EXTRA,GENERATION_EXPRESSION FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND GENERATION_EXPRESSION<>'' ORDER BY ORDINAL_POSITION", (table,)).unwrap()
}
fn checksum(conn: &mut mysql::PooledConn, table: &str) -> u64 {
    let row: mysql::Row = conn.query_first(format!("CHECKSUM TABLE `{table}` EXTENDED")).unwrap().unwrap();
    row.get("Checksum").expect("CHECKSUM TABLE returned NULL")
}
fn diagnostic(conn: &mut mysql::PooledConn, table: &str) -> String {
    let raw: Vec<(String, String)> = conn.exec("SELECT COLUMN_NAME,HEX(GENERATION_EXPRESSION) FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?", (table,)).unwrap();
    let create: mysql::Row = conn.query_first(format!("SHOW CREATE TABLE `{table}`")).unwrap().unwrap();
    format!("HEX(GENERATION_EXPRESSION)={raw:?}; SHOW CREATE={:?}", create.get::<String,_>("Create Table"))
}

#[test]
fn generated_columns_round_trip_replace_safe_and_promote() {
    let Some(mut s) = Sandbox::new() else { return };
    let mut db = s.conn();
    // SHOW CREATE prints an actual emoji literal as ???? on both servers. The hex
    // literal preserves the same output while allowing the definition to be proven.
    db.query_drop(r"CREATE TABLE sample(id INT PRIMARY KEY, active INT, label VARCHAR(20), v INT GENERATED ALWAYS AS (id+1) VIRTUAL, g INT GENERATED ALWAYS AS (IF(active=1,id,NULL)) STORED, text_g VARCHAR(100) GENERATED ALWAYS AS (concat(label,_utf8mb4'한글',convert(0xF09F9880 using utf8mb4),'\\','''')) STORED, UNIQUE KEY conditional_unique(g), KEY virtual_index(v))").unwrap();
    db.query_drop("INSERT INTO sample(id,active,label) VALUES(1,1,'one'),(2,0,'two'),(3,0,NULL)").unwrap();
    let info = diagnostic(&mut db, "sample");
    let metadata = generated_metadata(&mut db, "sample");
    assert_eq!(metadata[0].2, "(`id` + 1)", "{info}");
    assert!(info.contains("GENERATED ALWAYS AS ((`id` + 1))"), "{info}");
    let _infile = LocalInfile::enabled(&s.endpoint);
    let original_checksum = checksum(&mut db,"sample");
    let exported = s.dump("tsv", 1);
    assert_eq!(exported["format_version"],6, "{exported:#?}; {info}");
    let manifest = s.manifest();
    assert_eq!(manifest["schema"]["tables"][0]["columns"][3]["generated"]["expression"], "(`id` + 1)", "{info}");
    assert!(manifest["manifest_warnings"].as_array().unwrap().is_empty(), "{manifest}");
    for mode in ["replace", "safe"] {
        db.query_drop("UPDATE sample SET label='changed'").unwrap();
        let restored = result("dump.import",json!({"target":s.endpoint,"input_dir":s.output,"mode":mode,"threads":1}));
        let target = if mode == "safe" {
            assert_eq!(restored["verified"],true,"{restored:#?}; {info}");
            assert_eq!(restored["ready_for_switch"],true,"{restored:#?}");
            s.remember_candidate(&restored)
        } else { s.endpoint.clone() };
        let mut fresh = conn(&target);
        assert_eq!(generated_metadata(&mut fresh,"sample"),metadata,"{mode}; {info}");
        assert_eq!(checksum(&mut fresh,"sample"),original_checksum,"{mode}; {info}");
        fresh.query_drop("UPDATE sample SET active=1,label='updated' WHERE id=2").unwrap();
        assert_eq!(fresh.query_first::<i64,_>("SELECT g FROM sample WHERE id=2").unwrap(),Some(2));
        if mode == "safe" {
            // Undo the update so the verification proof is valid for promotion.
            fresh.query_drop("UPDATE sample SET active=0,label='two' WHERE id=2").unwrap();
            let planned = result("dump.promote",json!({"endpoint":s.endpoint,"action":"plan","report_path":restored["report_path"],"restore_id":restored["restore_id"]}));
            let done = result("dump.promote",json!({"endpoint":s.endpoint,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
            assert_eq!(done["success"],true,"{done:#?}");
            for key in ["backup_namespace","clone_database"] {
                if let Some(name) = done[key].as_str() { s.databases.push(name.into()); }
            }
            assert_eq!(checksum(&mut s.conn(),"sample"),original_checksum);
        }
    }
}

fn round_trip_load_path(format: &str, blob: bool, threads: usize) {
    let Some(s) = Sandbox::new() else { return };
    let mut db = s.conn();
    db.query_drop(format!("CREATE TABLE sample(id INT PRIMARY KEY, {} v INT GENERATED ALWAYS AS(id+1) VIRTUAL, g INT GENERATED ALWAYS AS(id*2) STORED)",if blob { "body BLOB," } else { "" })).unwrap();
    for id in 1..=9 {
        db.query_drop(format!("INSERT INTO sample(id{}) VALUES({id}{})", if blob { ",body" } else { "" }, if blob { ",X'00ff5c090a'" } else { "" })).unwrap();
    }
    let _infile = LocalInfile::enabled(&s.endpoint);
    let expected = checksum(&mut db,"sample");
    s.dump(format, threads);
    assert!(s.manifest()["tables"][0]["chunks"].as_u64().unwrap()>1);
    // Generated values must be ignored, even when the dump contains a different
    // value; safe-mode comparison of those values is separately tested below.
    let imported = events("dump.import",json!({"target":s.endpoint,"input_dir":s.output,"mode":"replace","threads":threads}));
    assert!(!imported.iter().any(|e| e["event"]=="error"),"{imported:#?}");
    let strategy = if format=="jsonl" || blob { "insert_rows" } else { "parallel_load_data_local_infile" };
    assert!(imported.iter().any(|e| e["strategy"]==strategy), "expected {strategy}: {imported:#?}");
    assert_eq!(checksum(&mut db,"sample"),expected);
}
#[test]
fn generated_columns_jsonl_insert() { round_trip_load_path("jsonl", false, 1); }
#[test]
fn generated_columns_blob_insert_fallback() { round_trip_load_path("tsv", true, 1); }
#[test]
fn generated_columns_parallel_multi_chunk_load() { round_trip_load_path("tsv", false, 3); }

#[test]
fn stored_generated_unique_keyset_and_foreign_keys() {
    let Some(s) = Sandbox::new() else { return };
    let mut db = s.conn();
    // Exercise both a generated PRIMARY KEY (numeric range splitting) and a
    // non-null generated UNIQUE key (keyset splitting without an ordinary PK).
    db.query_drop("CREATE TABLE generated_primary(id INT, g INT GENERATED ALWAYS AS(id+1) STORED, PRIMARY KEY(g))").unwrap();
    db.query_drop("CREATE TABLE parent(id INT, g INT GENERATED ALWAYS AS(id+100) STORED NOT NULL, UNIQUE KEY generated_key(g))").unwrap();
    db.query_drop("CREATE TABLE child(id INT PRIMARY KEY, g INT GENERATED ALWAYS AS(id+100) STORED, KEY(g), CONSTRAINT generated_fk FOREIGN KEY(g) REFERENCES parent(g) ON UPDATE RESTRICT ON DELETE RESTRICT)").unwrap();
    for id in 1..=9 {
        db.query_drop(format!("INSERT INTO generated_primary(id) VALUES({id}); INSERT INTO parent(id) VALUES({id}); INSERT INTO child(id) VALUES({id})")).unwrap();
    }
    let _infile = LocalInfile::enabled(&s.endpoint);
    let expected = [checksum(&mut db,"parent"),checksum(&mut db,"child"),checksum(&mut db,"generated_primary")];
    result("dump.run",json!({"source":s.endpoint,"output_dir":s.output,"data_format":"tsv","compression":"none","chunk_size":2,"threads":3}));
    assert!(s.manifest()["tables"].as_array().unwrap().iter().all(|t|t["chunks"].as_u64().unwrap()>1));
    result("dump.import",json!({"target":s.endpoint,"input_dir":s.output,"mode":"replace","threads":3}));
    assert_eq!([checksum(&mut db,"parent"),checksum(&mut db,"child"),checksum(&mut db,"generated_primary")],expected);
    assert!(db.query_drop("INSERT INTO child(id) VALUES(999)").is_err());
}

#[test]
fn undecodable_generated_expression_stays_refused() {
    let Some(s) = Sandbox::new() else { return };
    let mut db = s.conn();
    db.query_drop("CREATE TABLE sample(id INT, g VARCHAR(20) GENERATED ALWAYS AS(concat(id,_utf8mb4'😀')) STORED)").unwrap();
    let info = diagnostic(&mut db,"sample");
    let exported = events("dump.run",json!({"source":s.endpoint,"output_dir":s.output,"threads":1}));
    let refusal = exported.iter().find(|e|e["event"]=="error").unwrap_or_else(|| panic!("{exported:#?}; {info}"));
    assert_eq!(refusal["error_code"],"unsupported_objects","{info}");
    assert_eq!(refusal["reasons"]["generated_column:sample:g"],"generated_column_unproven","{info}");
    assert_eq!(refusal["bypassable"],false);
    assert!(!s.output.exists());
}

#[test]
fn generated_strict_recalculation_failure_is_reported() {
    let Some(s) = Sandbox::new() else { return };
    let mut db = s.conn();
    db.query_drop("SET SESSION sql_mode=''; CREATE TABLE sample(id INT PRIMARY KEY, raw VARCHAR(20), g INT GENERATED ALWAYS AS(CAST(raw AS SIGNED)) STORED); INSERT INTO sample(id,raw) VALUES(1,'12garbage')").unwrap();
    s.dump("jsonl",1);
    let imported = events("dump.import",json!({"target":s.endpoint,"input_dir":s.output,"mode":"replace"}));
    assert!(imported.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap_or_default().contains("Truncated")),"{imported:#?}");
    assert_eq!(db.query_first::<u64,_>("SELECT COUNT(*) FROM sample").unwrap(),Some(0));
}

#[test]
fn safe_digest_refuses_changed_generated_output_without_touching_original() {
    let Some(mut s) = Sandbox::new() else { return };
    let mut db = s.conn();
    db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY, g INT GENERATED ALWAYS AS(id+1) STORED); INSERT INTO sample(id) VALUES(1)").unwrap();
    s.dump("jsonl",1);
    let mut manifest = s.manifest();
    let table = &mut manifest["tables"][0];
    let file = table["chunk_sha256"].as_object().unwrap().keys().next().unwrap().clone();
    let path = s.output.join(table["path"].as_str().unwrap()).join(&file);
    let text = std::fs::read_to_string(&path).unwrap();
    let mut row: Value = serde_json::from_str(text.trim()).unwrap();
    row["g"] = json!(999);
    let bytes = format!("{row}\n").into_bytes();
    std::fs::write(path,&bytes).unwrap();
    table["chunk_sha256"][&file]=json!(format!("{:x}",Sha256::digest(&bytes)));
    s.save(&manifest);
    let report = result("dump.import",json!({"target":s.endpoint,"input_dir":s.output,"mode":"safe"}));
    s.remember_candidate(&report);
    assert_eq!(report["success"],false,"{report:#?}");
    assert!(report["message"].as_str().unwrap().contains("content_mismatch"),"{report:#?}");
    assert_eq!(db.query_first::<i64,_>("SELECT g FROM sample").unwrap(),Some(2));
}

#[test]
fn old_v5_dump_still_imports() {
    let Some(s) = Sandbox::new() else { return };
    let mut db = s.conn();
    db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY, note VARCHAR(20)); INSERT INTO sample VALUES(1,'v5')").unwrap();
    s.dump("jsonl",1);
    let mut manifest=s.manifest();
    manifest["format_version"]=json!(5);
    s.save(&manifest);
    db.query_drop("DELETE FROM sample").unwrap();
    result("dump.import",json!({"target":s.endpoint,"input_dir":s.output,"mode":"replace"}));
    assert_eq!(db.query_first::<String,_>("SELECT note FROM sample").unwrap(),Some("v5".into()));
}

/// Import and migration must refuse before mutating a PostgreSQL target.
#[test]
fn generated_mysql_to_postgres_is_blocked_before_drop() {
    let Some(s) = Sandbox::new() else { return };
    let Some(mut pg) = endpoint("postgresql") else { return };
    let mut db=s.conn();
    db.query_drop("CREATE TABLE sample(id INT PRIMARY KEY, g INT GENERATED ALWAYS AS(id+1) STORED); INSERT INTO sample(id) VALUES(1)").unwrap();
    s.dump("jsonl",1);
    let schema=s.endpoint.database.clone();
    let mut admin=LiveAdapter::connect(&pg).unwrap();
    admin.execute_sql(&format!("CREATE SCHEMA {schema}; CREATE TABLE {schema}.sample(id INT PRIMARY KEY); INSERT INTO {schema}.sample VALUES(91)")).unwrap();
    pg.schema=Some(schema.clone());
    let imported=events("dump.import",json!({"target":pg,"input_dir":s.output,"mode":"replace"}));
    let inspected=result("schema.inspect",json!({"source":s.endpoint}));
    let preflight=result("preflight",json!({"source_engine":"mysql","target_engine":"postgresql","schema":inspected["schema"],"unsupported_objects":inspected["unsupported_objects"]}));
    let migrated=events("migrate",json!({"source_engine":"mysql","target_engine":"postgresql",
        "source":s.endpoint,"target":pg,"schema":inspected["schema"],"unsupported_objects":inspected["unsupported_objects"],
        "execution_options":{"cleanup_before_migrate":true}}));
    let untouched=result("query.execute",json!({"connection":pg,"sql":"SELECT id FROM sample"}));
    admin.execute_sql(&format!("DROP SCHEMA {schema} CASCADE")).unwrap();
    assert!(imported.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap_or_default().contains("generated column sample.g")),"{imported:#?}");
    assert!(migrated.iter().any(|e|e["event"]=="error" && e["message"].as_str().unwrap_or_default().contains("generated column")),"{migrated:#?}");
    assert_eq!(untouched["rows"],json!([{"id":91}]));
    assert!(preflight["issues"].as_array().unwrap().iter().any(|issue|issue["blocking"]==true && issue["message"].as_str().unwrap_or_default().contains("generated column")),"{preflight:#?}");
}
