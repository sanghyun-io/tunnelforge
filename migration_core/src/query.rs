use serde_json::Value;
use sha2::{Digest, Sha256};

use mysql::prelude::Queryable;
use crate::*;

pub(crate) fn request_endpoint(request: &Request) -> Result<Endpoint, String> {
    for key in ["connection", "endpoint", "source", "target"] {
        if let Some(value) = request.payload.get(key) {
            return endpoint_from_value(value);
        }
    }
    endpoint_from_value(&request.payload)
}

pub(crate) fn query_params(payload: &Value) -> Vec<Value> {
    payload
        .get("params")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

pub(crate) fn bind_query_params(sql: &str, params: &[Value]) -> String {
    if params.is_empty() {
        return sql.to_string();
    }
    let mut rendered = sql.to_string();
    for (index, value) in params.iter().enumerate() {
        let literal = sql_json_literal(value);
        rendered = rendered.replacen("%s", &literal, 1);
        rendered = rendered.replace(&format!("${}", index + 1), &literal);
    }
    rendered
}

fn sql_json_literal(value: &Value) -> String {
    match value {
        Value::Null => "NULL".to_string(),
        Value::Bool(item) => {
            if *item {
                "TRUE".to_string()
            } else {
                "FALSE".to_string()
            }
        }
        Value::Number(item) => item.to_string(),
        Value::String(item) => format!("'{}'", item.replace('\\', "\\\\").replace('\'', "''")),
        other => format!(
            "'{}'",
            other.to_string().replace('\\', "\\\\").replace('\'', "''")
        ),
    }
}

pub(crate) fn connection_id(endpoint: &Endpoint) -> String {
    let mut hasher = Sha256::new();
    hasher.update(endpoint.engine.as_bytes());
    hasher.update(endpoint.host.as_bytes());
    hasher.update(endpoint.port.to_string().as_bytes());
    hasher.update(endpoint.user.as_bytes());
    hasher.update(endpoint.database.as_bytes());
    hasher.update(endpoint_schema(endpoint).as_bytes());
    format!("conn-{}", hex::encode(&hasher.finalize()[..8]))
}

pub(crate) fn unique_connection_id(endpoint: &Endpoint, sequence: u64) -> String {
    format!("{}-{}", connection_id(endpoint), sequence)
}

pub(crate) fn redact_endpoint_secret(message: &str, endpoint: &Endpoint) -> String {
    if endpoint.password.is_empty() {
        message.to_string()
    } else {
        message.replace(&endpoint.password, "***")
    }
}

pub(crate) fn execute_query_live(endpoint: &Endpoint, sql: &str) -> Result<QueryExecutionResult, String> {
    let mut adapter = LiveAdapter::connect(endpoint)?;
    execute_query_adapter(&mut adapter, sql)
}

pub(crate) fn execute_query_adapter(
    adapter: &mut LiveAdapter,
    sql: &str,
) -> Result<QueryExecutionResult, String> {
    let returns_rows = query_returns_rows(sql);
    match adapter {
        LiveAdapter::MySql(conn) => {
            if !returns_rows {
                conn.query_drop(sql)
                    .map_err(|err| format!("mysql SQL execution error: {err}"))?;
                return Ok(QueryExecutionResult {
                    rows: Vec::new(),
                    columns: Vec::new(),
                    rows_affected: conn.affected_rows(),
                });
            }
            let result = conn
                .query_iter(sql)
                .map_err(|err| format!("mysql query error: {err}"))?;
            // Read column metadata before consuming rows so 0-row result sets still carry columns.
            let columns: Vec<String> = result
                .columns()
                .as_ref()
                .iter()
                .map(|column| column.name_str().to_string())
                .collect();
            let mut rows = Vec::new();
            for row in result {
                rows.push(row.map_err(|err| format!("mysql query error: {err}"))?);
            }
            Ok(QueryExecutionResult {
                rows: rows
                    .into_iter()
                    .map(|row| mysql_row_to_json(&columns, row))
                    .collect(),
                columns,
                rows_affected: 0,
            })
        }
        LiveAdapter::PostgreSql(client) => {
            if !returns_rows {
                let rows_affected = client
                    .execute(sql, &[])
                    .map_err(|err| format!("postgresql SQL execution error: {err}"))?;
                return Ok(QueryExecutionResult {
                    rows: Vec::new(),
                    columns: Vec::new(),
                    rows_affected,
                });
            }
            let trimmed = sql.trim().trim_end_matches(';');
            // Prepare the original statement for column metadata; 0-row results still carry it.
            let statement = client
                .prepare(trimmed)
                .map_err(|err| format!("postgresql query error: {err}"))?;
            let columns: Vec<String> = statement
                .columns()
                .iter()
                .map(|column| column.name().to_string())
                .collect();
            let wrapped = format!("SELECT row_to_json(_tf_row)::text FROM ({trimmed}) AS _tf_row");
            let rows = client
                .query(&wrapped, &[])
                .map_err(|err| format!("postgresql query error: {err}"))?;
            let mut values = Vec::new();
            for row in rows {
                let text: Option<String> = row.get(0);
                let value = text
                    .and_then(|item| serde_json::from_str::<Value>(&item).ok())
                    .unwrap_or(Value::Null);
                values.push(value);
            }
            Ok(QueryExecutionResult {
                rows: values,
                columns,
                rows_affected: 0,
            })
        }
    }
}

/// SQL 주석 스캐너: `bytes[i]` 에서 시작하는 주석을 감지하면 그 주석 토큰 바로 다음
/// 인덱스를 반환하고, 주석 시작이 아니면 `None` 을 반환한다.
///
/// - 라인 주석(`--`, `allow_hash` 시 `#`): 종료 개행 `'\n'` 의 인덱스(개행 미소비).
///   개행이 없으면 `len`.
/// - 블록 주석(`/* */`): 닫는 `*/` 바로 다음 인덱스. 닫힘이 없으면 기존 산술상 `len+1`.
///
/// 반환 인덱스와 스캔 산술은 세 호출부(query::strip_leading_comments_and_parens,
/// schema::mysql_definition_has_residual_definer, schema::validate_single_view_statement)의
/// 기존 수제 스캐너와 바이트 단위로 일치한다. `#` 인식은 `allow_hash` 로만 켜지므로,
/// `allow_hash=false` 호출부(View 정의 검증기)는 지금처럼 `#` 을 리터럴로 취급한다.
pub(crate) fn skip_sql_comment(bytes: &[u8], i: usize, allow_hash: bool) -> Option<usize> {
    let len = bytes.len();
    if i >= len {
        return None;
    }
    if bytes[i] == b'-' && i + 1 < len && bytes[i + 1] == b'-' {
        let mut j = i + 2;
        while j < len && bytes[j] != b'\n' {
            j += 1;
        }
        return Some(j);
    }
    if allow_hash && bytes[i] == b'#' {
        let mut j = i + 1;
        while j < len && bytes[j] != b'\n' {
            j += 1;
        }
        return Some(j);
    }
    if bytes[i] == b'/' && i + 1 < len && bytes[i + 1] == b'*' {
        let mut j = i + 2;
        while j + 1 < len && !(bytes[j] == b'*' && bytes[j + 1] == b'/') {
            j += 1;
        }
        return Some(j + 2);
    }
    None
}

fn strip_leading_comments_and_parens(sql: &str) -> &str {
    let mut text = sql.trim_start();
    loop {
        if let Some(end) = skip_sql_comment(text.as_bytes(), 0, true) {
            // 기존 &str 의미 보존: 주석 끝부터 다시 잘라내고 선행 공백을 trim_start 한다.
            // 닫히지 않은 주석(end == len 또는 len+1)은 get 으로 안전하게 "" 로 수렴한다.
            text = text.get(end..).unwrap_or("").trim_start();
            continue;
        }
        if let Some(rest) = text.strip_prefix('(') {
            text = rest.trim_start();
            continue;
        }
        return text;
    }
}

fn leading_sql_keyword(sql: &str) -> String {
    let text = strip_leading_comments_and_parens(sql);
    let end = text
        .find(|ch: char| !(ch.is_ascii_alphabetic() || ch == '_'))
        .unwrap_or(text.len());
    text[..end].to_ascii_lowercase()
}

fn query_returns_rows(sql: &str) -> bool {
    let keyword = leading_sql_keyword(sql);
    ["select", "with", "show", "desc", "describe", "explain", "call", "values", "table"]
        .contains(&keyword.as_str())
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::json;
    
    
    
    
    
    
    
    
    
    

    #[test]
    fn endpoint_error_redaction_removes_password_value() {
        let endpoint = Endpoint {
            engine: "mysql".to_string(),
            host: "db.local".to_string(),
            port: 3306,
            user: "app".to_string(),
            password: "super-secret-password".to_string(),
            database: "prod".to_string(),
            schema: None,
        };

        let message = redact_endpoint_secret(
            "access denied for app using super-secret-password",
            &endpoint,
        );

        assert!(!message.contains("super-secret-password"));
        assert!(message.contains("***"));
    }

    #[test]
    fn query_param_binding_is_owned_by_core_protocol() {
        let sql = bind_query_params(
            "SELECT * FROM users WHERE id = %s AND name = $2",
            &[json!(7), json!("O'Reilly")],
        );

        assert_eq!(
            sql,
            "SELECT * FROM users WHERE id = 7 AND name = 'O''Reilly'"
        );
    }

    #[test]
    fn stateful_connection_ids_are_unique_for_same_endpoint() {
        let endpoint = Endpoint {
            engine: "mysql".to_string(),
            host: "127.0.0.1".to_string(),
            port: 3306,
            user: "root".to_string(),
            password: "secret".to_string(),
            database: "app".to_string(),
            schema: None,
        };

        let first = unique_connection_id(&endpoint, 1);
        let second = unique_connection_id(&endpoint, 2);

        assert_ne!(first, second);
        assert!(first.starts_with(&connection_id(&endpoint)));
        assert!(second.starts_with(&connection_id(&endpoint)));
    }

    #[test]
    fn query_returns_rows_skips_leading_comments_and_parentheses() {
        for sql in [
            "-- x\nSELECT 1",
            "# x\nSELECT 1",
            "/*x*/ SELECT 1",
            "(SELECT 1)",
            "VALUES (1)",
            "TABLE users",
            "CALL proc()",
        ] {
            assert!(query_returns_rows(sql), "expected rows for: {sql}");
        }

        assert!(!query_returns_rows("/*x*/ UPDATE users SET name='a'"));
    }

    #[test]
    fn core_service_reports_unknown_connection_for_stateful_query() {
        let mut service = CoreService::new();
        let mut events = Vec::new();
        service.handle_request_streaming(
            Request {
                command: "query.execute".to_string(),
                request_id: Some("query-1".to_string()),
                payload: json!({"connection_id": "missing", "sql": "SELECT 1"}),
            },
            |event| events.push(event),
        )
        .expect("unknown connection errors should emit successfully");

        assert_eq!(events[0]["event"], "error");
        assert_eq!(events[0]["request_id"], "query-1");
        assert!(events[0]["message"]
            .as_str()
            .unwrap()
            .contains("unknown connection_id"));
    }

    #[test]
    fn endpoint_from_value_validates_required_fields() {
        let endpoint = endpoint_from_value(&json!({
            "engine": "mysql",
            "host": "127.0.0.1",
            "port": 3306,
            "user": "root",
            "password": "secret",
            "database": "app"
        }))
        .unwrap();

        assert_eq!(endpoint.engine, "mysql");
        assert_eq!(endpoint.port, 3306);
        assert!(endpoint_from_value(&json!({
            "engine": "sqlite",
            "host": "127.0.0.1",
            "port": 1,
            "user": "u",
            "password": "",
            "database": "d"
        }))
        .is_err());
    }
}
