//! catalog.query 실 DB 검증 (MySQL `TF_MYSQL_HOST`, PostgreSQL `TF_POSTGRES_HOST`, 없으면 건너뜀):
//! 열린 세션에서 스키마/컬럼/버전/DB 목록을 읽고, 와일드카드 문자가 든 이름을 정확히 비교한다.
use migration_core::{CoreService, Request};
use serde_json::{json, Value};

fn endpoint(engine: &str, host_var: &str, user: &str) -> Option<Value> {
    Some(json!({
        "engine": engine,
        "host": std::env::var(host_var).ok()?,
        "port": if engine == "mysql" { 3306 } else { 5432 },
        "user": user,
        "password": "tf_local_test",
        "database": "tf_test",
    }))
}

fn call(service: &mut CoreService, command: &str, payload: Value) -> Result<Value, String> {
    let mut events = Vec::new();
    service.handle_request_streaming(Request { command: command.into(), request_id: None, payload }, |event| events.push(event));
    match events.into_iter().find(|event| event["event"] == "result" || event["event"] == "error") {
        Some(event) if event["event"] == "result" => Ok(event),
        Some(event) => Err(event["message"].as_str().unwrap_or("").to_string()),
        None => Err("no result".into()),
    }
}

fn values(service: &mut CoreService, connection_id: &str, extra: Value) -> Vec<String> {
    let mut payload = json!({"connection_id": connection_id});
    payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
    let result = call(service, "catalog.query", payload).unwrap_or_else(|err| panic!("{err}"));
    serde_json::from_value(result["values"].clone()).unwrap()
}

fn sql(service: &mut CoreService, connection_id: &str, statement: &str) {
    call(service, "query.execute", json!({"connection_id": connection_id, "sql": statement})).unwrap_or_else(|err| panic!("{err}: {statement}"));
}

#[test]
fn mysql_catalog_reads_metadata_on_a_session() {
    let Some(ep) = endpoint("mysql", "TF_MYSQL_HOST", "root") else { return };
    let mut service = CoreService::new();
    let id = call(&mut service, "connection.open", json!({"connection": ep})).unwrap()["connection_id"].as_str().unwrap().to_string();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let schema = format!("tfacat_{stamp}");
    sql(&mut service, &id, &format!("CREATE DATABASE `{schema}`"));
    sql(&mut service, &id, &format!("CREATE TABLE `{schema}`.t (b INT, a INT)"));

    let schemas = values(&mut service, &id, json!({"kind": "schemas"}));
    assert!(schemas.contains(&schema) && schemas.contains(&"tf_test".to_string()));
    assert!(!schemas.iter().any(|name| name == "mysql" || name == "information_schema" || name == "sys"));
    assert_eq!(values(&mut service, &id, json!({"kind": "schema_exists", "name": schema})), vec![schema.clone()]);
    // `tf_cat_` 는 LIKE 로는 `tfacat_` 에 걸리지만, 정확히 같은 이름만 존재로 본다.
    let lookalike = format!("tf_cat_{stamp}");
    assert!(values(&mut service, &id, json!({"kind": "schema_exists", "name": lookalike})).is_empty());
    assert_eq!(values(&mut service, &id, json!({"kind": "columns", "schema": schema, "table": "t"})), vec!["b", "a"]);
    assert!(values(&mut service, &id, json!({"kind": "version"}))[0].starts_with('8'));
    // 타임존 테이블이 비어 있을 수 있으므로 오류 없이 응답하는지만 본다.
    values(&mut service, &id, json!({"kind": "named_timezone", "name": "Asia/Seoul"}));
    let unsupported = call(&mut service, "catalog.query", json!({"connection_id": id, "kind": "databases"}));
    assert!(unsupported.unwrap_err().contains("not supported"));
    assert!(call(&mut service, "catalog.query", json!({"connection_id": "missing", "kind": "schemas"})).is_err());

    sql(&mut service, &id, &format!("DROP DATABASE `{schema}`"));
}

#[test]
fn postgres_catalog_reads_metadata_on_a_session() {
    let Some(ep) = endpoint("postgresql", "TF_POSTGRES_HOST", "postgres") else { return };
    let mut service = CoreService::new();
    let id = call(&mut service, "connection.open", json!({"connection": ep})).unwrap()["connection_id"].as_str().unwrap().to_string();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    // `pgx...` 는 `pg_%` 와일드카드에 걸려 예전 Python 조회에서는 숨겨졌다.
    let schema = format!("pgx_cat_{stamp}");
    sql(&mut service, &id, &format!("CREATE SCHEMA \"{schema}\""));
    sql(&mut service, &id, &format!("CREATE TABLE \"{schema}\".t (b INT, a INT)"));

    let schemas = values(&mut service, &id, json!({"kind": "schemas"}));
    assert!(schemas.contains(&schema) && schemas.contains(&"public".to_string()));
    assert!(!schemas.iter().any(|name| name == "pg_catalog" || name == "information_schema"));
    assert_eq!(values(&mut service, &id, json!({"kind": "schema_exists", "name": schema})), vec![schema.clone()]);
    assert!(values(&mut service, &id, json!({"kind": "schema_exists", "name": "no_such_schema"})).is_empty());
    assert_eq!(values(&mut service, &id, json!({"kind": "columns", "schema": schema, "table": "t"})), vec!["b", "a"]);
    assert!(values(&mut service, &id, json!({"kind": "version"}))[0].starts_with("PostgreSQL"));
    assert!(values(&mut service, &id, json!({"kind": "databases"})).contains(&"tf_test".to_string()));

    sql(&mut service, &id, &format!("DROP SCHEMA \"{schema}\" CASCADE"));
}
