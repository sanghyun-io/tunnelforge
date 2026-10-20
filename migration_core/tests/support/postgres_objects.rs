use migration_core::{handle_request, Endpoint, Request};
use serde_json::{json, Value};
use std::path::PathBuf;

pub fn endpoint(prefix: &str) -> Option<Endpoint> {
    let host=std::env::var(format!("{prefix}_HOST")).ok();
    if host.is_none() && std::env::var("TF_LIVE_REQUIRED").as_deref()==Ok("1") {
        panic!("{prefix}_HOST is required for this PostgreSQL live binary");
    }
    Some(Endpoint { engine:"postgresql".into(), host:host?,
        port:std::env::var(format!("{prefix}_PORT")).ok().and_then(|port|port.parse().ok()).unwrap_or(5432),
        user:std::env::var(format!("{prefix}_USER")).unwrap_or_else(|_|"postgres".into()),
        password:std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_else(|_|"tf_local_test".into()),
        database:std::env::var(format!("{prefix}_DATABASE")).unwrap_or_else(|_|"tf_test".into()),
        schema:None, tls:Default::default() })
}

pub fn db(endpoint: &Endpoint) -> postgres::Client {
    let mut client=postgres::Config::new().host(&endpoint.host).port(endpoint.port).user(&endpoint.user)
        .password(&endpoint.password).dbname(&endpoint.database).connect(postgres::NoTls).unwrap();
    if let Some(schema)=&endpoint.schema {
        client.batch_execute(&format!("SET standard_conforming_strings=on; SET search_path TO \"{schema}\"")).unwrap();
    }
    client
}

pub fn response(command: &str, payload: Value) -> Value {
    let events=handle_request(Request { command:command.into(), request_id:None, payload });
    events.into_iter().find(|event|event["event"]=="result" || event["event"]=="error").unwrap()
}

pub fn run(command: &str, payload: Value) -> Value {
    let result=response(command,payload);
    assert_ne!(result["event"],"error","{result:#}");
    assert_eq!(result["success"],true,"{result:#}");
    result
}

pub struct Sandbox {
    pub source:Endpoint,
    pub target:Endpoint,
    pub dir:PathBuf,
}
impl Sandbox {
    pub fn new() -> Option<Self> { Some(Self::on(endpoint("TF_POSTGRES")?)) }
    pub fn on(base:Endpoint) -> Self {
        let nonce=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let source=format!("tf205_{nonce}"); let target=format!("{source}_target");
        db(&base).batch_execute(&format!("CREATE SCHEMA {source}; CREATE SCHEMA {target}")).unwrap();
        Self {source:Endpoint {schema:Some(source.clone()),..base.clone()},target:Endpoint {schema:Some(target),..base},dir:std::env::temp_dir().join(source)}
    }
    pub fn dump(&self) -> Value {
        run("dump.run",json!({"source":self.source,"output_dir":self.dir,"data_format":"jsonl","compression":"none","threads":1}))
    }
    pub fn manifest(&self) -> Value { serde_json::from_slice(&std::fs::read(self.dir.join("_tunnelforge_dump.json")).unwrap()).unwrap() }
    pub fn write_manifest(&self, manifest:&Value) { std::fs::write(self.dir.join("_tunnelforge_dump.json"),serde_json::to_vec(manifest).unwrap()).unwrap(); }
    pub fn sentinel(&self) { db(&self.target).batch_execute("CREATE TABLE sample(id int PRIMARY KEY); INSERT INTO sample VALUES(91); CREATE TABLE keep_me(id int); INSERT INTO keep_me VALUES(92)").unwrap(); }
    pub fn assert_untouched(&self) {
        let mut target=db(&self.target);
        assert_eq!(target.query_one("SELECT id FROM sample",&[]).unwrap().get::<_,i32>(0),91);
        assert_eq!(target.query_one("SELECT id FROM keep_me",&[]).unwrap().get::<_,i32>(0),92);
        assert_eq!(target.query_one("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname LIKE 'tf_probe_%'",&[&self.target.schema]).unwrap().get::<_,i64>(0),0);
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        db(&self.source).batch_execute(&format!("DROP SCHEMA IF EXISTS {} CASCADE; DROP SCHEMA IF EXISTS {} CASCADE",self.source.schema.as_deref().unwrap(),self.target.schema.as_deref().unwrap())).unwrap();
        if self.dir.exists() { std::fs::remove_dir_all(&self.dir).unwrap(); }
    }
}
