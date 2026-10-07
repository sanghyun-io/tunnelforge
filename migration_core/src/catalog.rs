//! catalog.query: UI 가 쓰는 작은 메타데이터 조회를 열린 세션(connection_id) 위에서 실행한다.
//!
//! kind 별 결과는 모두 문자열 목록(`values`)이다.
//! - schemas        : 사용자 스키마 (MySQL 은 시스템 스키마 제외, PostgreSQL 은 information_schema/pg_* 제외)
//! - schema_exists  : name 이 있으면 [name], 없으면 []
//! - columns        : schema.table 의 컬럼 이름 (정의 순서)
//! - version        : [서버 버전 문자열]
//! - databases      : PostgreSQL 접속 가능한 데이터베이스 (MySQL 은 오류)
//! - named_timezone : MySQL time_zone_name 에 name 이 있으면 [name] (PostgreSQL 은 오류)
use crate::adapters::LiveAdapter;
use mysql::prelude::Queryable;
use serde_json::Value;

const MYSQL_SYSTEM_SCHEMAS: &[&str] = &["information_schema", "mysql", "performance_schema", "sys", "ndbinfo"];

fn text<'a>(payload: &'a Value, key: &str) -> Result<&'a str, String> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("catalog.query requires {key}"))
}

fn mysql_strings(conn: &mut mysql::PooledConn, sql: &str, params: Vec<&str>) -> Result<Vec<String>, String> {
    conn.exec_map(sql, params, |value: String| value)
        .map_err(|err| format!("mysql catalog query error: {err}"))
}

fn postgres_strings(client: &mut postgres::Client, sql: &str, params: &[&str]) -> Result<Vec<String>, String> {
    let params: Vec<&(dyn postgres::types::ToSql + Sync)> = params.iter().map(|value| value as _).collect();
    let rows = client
        .query(sql, &params)
        .map_err(|err| format!("postgresql catalog query error: {err}"))?;
    Ok(rows.iter().map(|row| row.get::<_, String>(0)).collect())
}

pub(crate) fn catalog_values(adapter: &mut LiveAdapter, payload: &Value) -> Result<Vec<String>, String> {
    let kind = text(payload, "kind")?;
    match adapter {
        LiveAdapter::MySql(conn) => match kind {
            "schemas" => Ok(mysql_strings(conn, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME", vec![])?
                .into_iter()
                .filter(|name| !MYSQL_SYSTEM_SCHEMAS.contains(&name.to_lowercase().as_str()))
                .collect()),
            // SHOW DATABASES LIKE 는 `_`/`%` 를 와일드카드로 해석하므로 정확히 비교한다.
            "schema_exists" => mysql_strings(
                conn,
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = ?",
                vec![text(payload, "name")?],
            ),
            "columns" => mysql_strings(
                conn,
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                vec![text(payload, "schema")?, text(payload, "table")?],
            ),
            "version" => mysql_strings(conn, "SELECT VERSION()", vec![]),
            "named_timezone" => mysql_strings(
                conn,
                "SELECT Name FROM mysql.time_zone_name WHERE Name = ? LIMIT 1",
                vec![text(payload, "name")?],
            ),
            other => Err(format!("catalog.query kind '{other}' is not supported for mysql")),
        },
        LiveAdapter::PostgreSql(client) => match kind {
            // `pg_%` 의 `_` 는 와일드카드라 `pgadmin` 같은 사용자 스키마까지 숨기므로 escape 한다.
            "schemas" => postgres_strings(
                client,
                "SELECT schema_name::text FROM information_schema.schemata \
                 WHERE schema_name <> 'information_schema' AND schema_name NOT LIKE 'pg!_%' ESCAPE '!' ORDER BY 1",
                &[],
            ),
            "schema_exists" => postgres_strings(
                client,
                "SELECT schema_name::text FROM information_schema.schemata WHERE schema_name = $1",
                &[text(payload, "name")?],
            ),
            "columns" => postgres_strings(
                client,
                "SELECT column_name::text FROM information_schema.columns \
                 WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
                &[text(payload, "schema")?, text(payload, "table")?],
            ),
            "version" => postgres_strings(client, "SELECT version()", &[]),
            "databases" => postgres_strings(
                client,
                "SELECT datname::text FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY 1",
                &[],
            ),
            other => Err(format!("catalog.query kind '{other}' is not supported for postgresql")),
        },
    }
}
