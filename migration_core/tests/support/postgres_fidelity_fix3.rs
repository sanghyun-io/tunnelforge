use super::support::*;
use migration_core::{Endpoint,LiveAdapter,MigrationAdapter};
use serde_json::json;
use std::io::{Read,Write};
use std::net::{Shutdown,TcpListener,TcpStream};
use std::sync::{Arc,Mutex,atomic::{AtomicBool,AtomicUsize,Ordering::SeqCst}};

// Count protocol Parse/Query messages, not TCP bursts or elapsed time. A source unit
// adds exactly two wire batches (BEGIN + local settings, COMMIT), targets add none.
struct Proxy {
    port:u16, statements:Arc<AtomicUsize>, batches:Arc<Mutex<Vec<String>>>,
    stop:Arc<AtomicBool>, listener:Option<std::thread::JoinHandle<()>>,
}
impl Proxy {
    fn new(endpoint:&Endpoint, wrong_path:Option<String>) -> Self {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let port=listener.local_addr().unwrap().port();
        let statements=Arc::new(AtomicUsize::new(0));let batches=Arc::new(Mutex::new(Vec::new()));
        let stop=Arc::new(AtomicBool::new(false));
        let (counter,sqls,ending)=(statements.clone(),batches.clone(),stop.clone());
        let remote=(endpoint.host.clone(),endpoint.port);
        let thread=std::thread::spawn(move||for incoming in listener.incoming() {
            if ending.load(SeqCst) {break}
            let Ok(mut client)=incoming else {break};
            let mut server=TcpStream::connect((remote.0.as_str(),remote.1)).unwrap();
            client.set_nodelay(true).unwrap();server.set_nodelay(true).unwrap();
            let (mut from_server,mut to_client)=(server.try_clone().unwrap(),client.try_clone().unwrap());
            std::thread::spawn(move||{let _=std::io::copy(&mut from_server,&mut to_client);let _=to_client.shutdown(Shutdown::Both);});
            let (counter,sqls,wrong_path)=(counter.clone(),sqls.clone(),wrong_path.clone());
            std::thread::spawn(move||{
                let mut forward=||->std::io::Result<()> {
                    // Startup (and optional SSLRequest) have a length but no type byte.
                    loop {
                        let mut length=[0;4];client.read_exact(&mut length)?;
                        let size=u32::from_be_bytes(length) as usize;
                        let mut body=vec![0;size-4];client.read_exact(&mut body)?;
                        server.write_all(&length)?;server.write_all(&body)?;
                        if size!=8 || body!=80877103_u32.to_be_bytes() {break}
                    }
                    loop {
                        let mut kind=[0];client.read_exact(&mut kind)?;
                        let mut length=[0;4];client.read_exact(&mut length)?;
                        let mut body=vec![0;u32::from_be_bytes(length) as usize-4];client.read_exact(&mut body)?;
                        if matches!(kind[0],b'P'|b'Q') {counter.fetch_add(1,SeqCst);}
                        if kind[0]==b'Q' {
                            let mut sql=String::from_utf8(body[..body.len()-1].to_vec()).unwrap();
                            if let Some(path)=&wrong_path {
                                if sql.starts_with("SET standard_conforming_strings=on; SET search_path TO ") {
                                    sql.push_str(&format!("; SET search_path TO \"{path}\""));
                                }
                            }
                            sqls.lock().unwrap().push(sql.clone());
                            body=sql.into_bytes();body.push(0);
                        }
                        server.write_all(&kind)?;server.write_all(&((body.len()+4) as u32).to_be_bytes())?;server.write_all(&body)?;
                    }
                };
                let _=forward();let _=server.shutdown(Shutdown::Both);
            });
        });
        Self {port,statements,batches,stop,listener:Some(thread)}
    }
    fn endpoint(&self, endpoint:&Endpoint) -> Endpoint {Endpoint {host:"127.0.0.1".into(),port:self.port,..endpoint.clone()}}
    fn measure<T>(&self, call:impl FnOnce()->T) -> (T,usize) {
        let before=self.statements.load(SeqCst);let value=call();(value,self.statements.load(SeqCst)-before)
    }
}
impl Drop for Proxy {
    fn drop(&mut self) {
        self.stop.store(true,SeqCst);let _=TcpStream::connect(("127.0.0.1",self.port));
        let _=self.listener.take().unwrap().join();
    }
}

#[test]
fn source_wire_batches_are_old_statement_count_plus_two_target_reads_unchanged() {
    let Some(s)=Sandbox::new() else {return};
    for endpoint in [&s.source,&s.target] {db(endpoint).batch_execute("CREATE TABLE t(id int PRIMARY KEY); INSERT INTO t VALUES(1),(2),(3)").unwrap();}
    let table=migration_core::inspect_live(&s.source).unwrap().schema.tables.remove(0);
    let proxy=Proxy::new(&s.source,None);
    let mut source=LiveAdapter::connect_source(&proxy.endpoint(&s.source)).unwrap();
    let mut target=LiveAdapter::connect(&proxy.endpoint(&s.target)).unwrap();
    let keys=vec!["id".into()];let values=vec![vec!["1".into()]];
    for mode in 0..4 {
        let (_,plain)=proxy.measure(||match mode {
            0=>target.row_count("t").unwrap(),
            1=>target.read_rows(&table,0,1).unwrap().len(),
            2=>target.read_rows_after_key(&table,&keys,None,1).unwrap().len(),
            _=>target.read_rows_by_keys(&table,&keys,&values).unwrap().len(),
        });
        let (_,guarded)=proxy.measure(||match mode {
            0=>source.row_count("t").unwrap(),
            1=>source.read_rows(&table,0,1).unwrap().len(),
            2=>source.read_rows_after_key(&table,&keys,None,1).unwrap().len(),
            _=>source.read_rows_by_keys(&table,&keys,&values).unwrap().len(),
        });
        assert_eq!(plain,1,"target read {mode} must send only its original statement");
        assert_eq!(guarded,plain+2,"source read {mode} exceeded its wire-batch budget");
    }
    let batches=proxy.batches.lock().unwrap();
    let setups=batches.iter().filter(|sql|sql.starts_with("BEGIN READ ONLY")).collect::<Vec<_>>();
    assert_eq!(setups.len(),4);
    assert!(setups.iter().all(|sql|sql.contains("SET LOCAL row_security = off; SET LOCAL search_path TO")));
}

#[test]
fn source_and_dump_read_the_requested_schema_after_session_path_changes() {
    let Some(s)=Sandbox::new() else {return};
    db(&s.source).batch_execute("CREATE TABLE orders(id int PRIMARY KEY); INSERT INTO orders VALUES(11),(12)").unwrap();
    db(&s.target).batch_execute("CREATE TABLE orders(id int PRIMARY KEY); INSERT INTO orders VALUES(91),(92),(93)").unwrap();
    let table=migration_core::inspect_live(&s.source).unwrap().schema.tables.remove(0);
    let mut source=LiveAdapter::connect_source(&s.source).unwrap();
    source.execute_sql(&format!("SET search_path TO \"{}\"",s.target.schema.as_deref().unwrap())).unwrap();
    assert_eq!(source.row_count("orders").unwrap(),2);
    let rows=source.read_rows(&table,0,10).unwrap();assert_eq!(rows[0]["id"],"11");
    let LiveAdapter::PostgreSql(mut client)=source.into_inner() else {unreachable!()};
    assert_eq!(client.query_one("SELECT count(*) FROM orders",&[]).unwrap().get::<_,i64>(0),3,"LOCAL path must restore the session setting");
    // Change the actual dump client's session SET after connect, simulating a
    // transaction pooler handing out a backend with another tenant's search_path.
    let proxy=Proxy::new(&s.source,s.target.schema.clone());
    let result=run("dump.run",json!({"source":proxy.endpoint(&s.source),"output_dir":s.dir,"data_format":"jsonl","compression":"none"}));
    assert_eq!(result["rows_dumped"],2);
    run("dump.import",json!({"target":s.target,"input_dir":s.dir,"mode":"replace"}));
    let ids=db(&s.target).query("SELECT id FROM orders ORDER BY id",&[]).unwrap().iter().map(|row|row.get::<_,i32>(0)).collect::<Vec<_>>();
    assert_eq!(ids,vec![11,12]);
    assert!(proxy.batches.lock().unwrap().iter().any(|sql|sql.starts_with("BEGIN ISOLATION LEVEL REPEATABLE READ") && sql.contains("SET LOCAL search_path TO")));
}

#[test]
fn bypass_rls_without_select_is_an_ordinary_permission_error_on_both_sides() {
    let Some(s)=Sandbox::new() else {return};
    let name=format!("{}_bypass",s.source.schema.as_deref().unwrap());
    struct Role(Endpoint,String);
    impl Drop for Role {fn drop(&mut self){let _=db(&self.0).batch_execute(&format!("DROP OWNED BY {}; DROP ROLE {}",self.1,self.1));}}
    let _role=Role(s.source.clone(),name.clone());
    db(&s.source).batch_execute(&format!("CREATE ROLE {name} LOGIN BYPASSRLS PASSWORD 'tf_local_test'; GRANT USAGE ON SCHEMA \"{}\" TO {name}; CREATE TABLE audit(id int); ALTER TABLE audit ENABLE ROW LEVEL SECURITY; CREATE POLICY p ON audit USING(true); GRANT INSERT ON audit TO {name}",s.source.schema.as_deref().unwrap())).unwrap();
    let reader=Endpoint {user:name,..s.source.clone()};
    let table=migration_core::inspect_live(&s.source).unwrap().schema.tables.remove(0);
    let raw=db(&reader).query("SELECT * FROM audit",&[]).unwrap_err();
    assert_eq!(raw.as_db_error().unwrap().routine(),Some("aclcheck_error"));
    let mut source=LiveAdapter::connect_source(&reader).unwrap();
    let mut target=LiveAdapter::connect(&reader).unwrap();
    for error in [source.row_count("audit").unwrap_err(),source.read_rows(&table,0,1).unwrap_err(),target.row_count("audit").unwrap_err(),target.read_rows_by_keys(&table,&[],&[]).unwrap_err()] {
        assert!(error.contains("42501") && !error.contains("row_security_blocks_read"),"{error}");
    }
    let failed=response("dump.run",json!({"source":reader,"output_dir":s.dir,"tables":["audit"],"allow_incomplete":true}));
    assert!(failed["message"].as_str().unwrap().contains("42501"),"{failed:#}");
    assert_ne!(failed["error_code"],"row_security_blocks_read");assert!(!s.dir.exists());
}
