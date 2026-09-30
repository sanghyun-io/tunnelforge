use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::*;
use mysql::prelude::Queryable;

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

// Scan only the original SQL: inserted values must never become placeholders.
fn bind_query_params(
    sql: &str,
    params: &[Value],
    engine: &str,
    backslash_escapes: bool,
    ansi_quotes: bool,
) -> Result<String, String> {
    if params.is_empty() {
        return Ok(sql.to_string());
    }
    let bytes = sql.as_bytes();
    let mut out = String::with_capacity(sql.len());
    let mut used = vec![false; params.len()];
    let mut sequential = 0;
    let mut i = 0;
    while i < bytes.len() {
        let start = i;
        if bytes[i..].starts_with(b"/*") {
            i += 2;
            let mut depth = 1;
            while i < bytes.len() && depth > 0 {
                if engine == "postgresql" && bytes[i..].starts_with(b"/*") {
                    depth += 1;
                    i += 2;
                } else if bytes[i..].starts_with(b"*/") {
                    depth -= 1;
                    i += 2;
                } else {
                    i += 1;
                }
            }
        } else if (bytes[i..].starts_with(b"--")
            && (engine == "postgresql"
                || bytes
                    .get(i + 2)
                    .map(|b| b.is_ascii_whitespace() || b.is_ascii_control())
                    .unwrap_or(true)))
            || (engine == "mysql" && bytes[i] == b'#')
        {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
        } else if matches!(bytes[i], b'\'' | b'"') || (engine == "mysql" && bytes[i] == b'`') {
            let quote = bytes[i];
            let escaped = (backslash_escapes
                && (quote == b'\'' || (engine == "mysql" && !ansi_quotes && quote == b'"')))
                || (engine == "postgresql"
                    && quote == b'\''
                    && i > 0
                    && matches!(bytes[i - 1], b'e' | b'E'));
            i += 1;
            while i < bytes.len() {
                if escaped && bytes[i] == b'\\' {
                    i = (i + 2).min(bytes.len());
                } else if bytes[i] == quote {
                    i += 1;
                    if i < bytes.len() && bytes[i] == quote {
                        i += 1;
                    } else {
                        break;
                    }
                } else {
                    i += 1;
                }
            }
        } else if engine == "postgresql" && bytes[i] == b'$' && dollar_quote_end(sql, i).is_some() {
            i = dollar_quote_end(sql, i).unwrap();
        } else {
            let mut parameter = None;
            if bytes[i..].starts_with(b"%s") {
                parameter = Some(sequential);
                sequential += 1;
                i += 2;
            } else if bytes[i] == b'$'
                && i + 1 < bytes.len()
                && bytes[i + 1].is_ascii_digit()
                && (i == 0
                    || !(bytes[i - 1].is_ascii_alphanumeric()
                        || matches!(bytes[i - 1], b'_' | b'$')
                        || bytes[i - 1] >= 128))
            {
                i += 1;
                let digit_start = i;
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                }
                parameter = Some(
                    sql[digit_start..i]
                        .parse::<usize>()
                        .ok()
                        .and_then(|n| n.checked_sub(1))
                        .ok_or("invalid SQL parameter index")?,
                );
            }
            if let Some(index) = parameter {
                let value = params
                    .get(index)
                    .ok_or_else(|| format!("missing SQL parameter {}", index + 1))?;
                used[index] = true;
                out.push_str(&sql_json_literal(value, engine)?);
                continue;
            }
            i += sql[i..].chars().next().unwrap().len_utf8();
        }
        out.push_str(&sql[start..i]);
    }
    if !used.last().copied().unwrap_or(true) {
        return Err("unused SQL parameters".to_string());
    }
    Ok(out)
}

fn dollar_quote_end(sql: &str, start: usize) -> Option<usize> {
    if start > 0
        && (sql.as_bytes()[start - 1].is_ascii_alphanumeric()
            || matches!(sql.as_bytes()[start - 1], b'_' | b'$')
            || sql.as_bytes()[start - 1] >= 128)
    {
        return None;
    }
    let tail = &sql[start + 1..];
    let end = tail.find('$')?;
    let tag = &tail[..end];
    if !tag.is_empty()
        && (!tag.as_bytes()[0].is_ascii_alphabetic()
            && tag.as_bytes()[0] != b'_'
            && tag.as_bytes()[0] < 128
            || !tag
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b >= 128))
    {
        return None;
    }
    let delimiter = &sql[start..start + end + 2];
    let body = start + delimiter.len();
    Some(
        sql[body..]
            .find(delimiter)
            .map(|offset| body + offset + delimiter.len())
            .unwrap_or(sql.len()),
    )
}

fn sql_json_literal(value: &Value, engine: &str) -> Result<String, String> {
    Ok(match value {
        Value::Null => "NULL".to_string(),
        Value::Bool(item) => if *item { "TRUE" } else { "FALSE" }.to_string(),
        Value::Number(item) => item.to_string(),
        other => {
            let text = match other {
                Value::String(text) => text.clone(),
                other => other.to_string(),
            };
            if engine == "mysql" {
                // Hex avoids both backslash and NO_BACKSLASH_ESCAPES SQL modes.
                format!("CONVERT(X'{}' USING utf8mb4)", hex::encode(text.as_bytes()))
            } else {
                if text.contains('\0') {
                    return Err("PostgreSQL text parameters cannot contain NUL".to_string());
                }
                if text.contains('\\') {
                    format!("E'{}'", text.replace('\\', "\\\\").replace('\'', "''"))
                } else {
                    format!("'{}'", text.replace('\'', "''"))
                }
            }
        }
    })
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

pub(crate) fn execute_query_live(
    endpoint: &Endpoint,
    sql: &str,
    params: &[Value],
) -> Result<QueryExecutionResult, String> {
    let mut adapter = LiveAdapter::connect(endpoint)?;
    execute_query_adapter(&mut adapter, sql, params)
}

// The JSONL row object needs unique keys. Reserve original aliases before
// suffixing duplicates so a real "x (2)" column can never be overwritten.
fn unique_query_columns(columns: Vec<String>) -> Vec<String> {
    let reserved: std::collections::HashSet<_> = columns.iter().cloned().collect();
    let mut used = std::collections::HashSet::new();
    columns
        .into_iter()
        .map(|name| {
            if used.insert(name.clone()) {
                return name;
            }
            let mut suffix = 2;
            loop {
                let candidate = format!("{name} ({suffix})");
                if !reserved.contains(&candidate) && used.insert(candidate.clone()) {
                    return candidate;
                }
                suffix += 1;
            }
        })
        .collect()
}

pub(crate) fn execute_query_adapter(
    adapter: &mut LiveAdapter,
    sql: &str,
    params: &[Value],
) -> Result<QueryExecutionResult, String> {
    match adapter {
        LiveAdapter::MySql(conn) => {
            let mode = if params.is_empty() {
                String::new()
            } else {
                conn.query_first::<String, _>("SELECT @@SESSION.sql_mode")
                    .map_err(|err| format!("mysql SQL mode error: {err}"))?
                    .unwrap_or_default()
            };
            let backslash_escapes = !mode.split(',').any(|mode| mode == "NO_BACKSLASH_ESCAPES");
            let ansi_quotes = mode.split(',').any(|mode| mode == "ANSI_QUOTES");
            let sql = bind_query_params(sql, params, "mysql", backslash_escapes, ansi_quotes)?;
            let mut result = conn
                .query_iter(sql)
                .map_err(|err| format!("mysql query error: {err}"))?;
            let columns: Vec<String> = result
                .columns()
                .as_ref()
                .iter()
                .map(|column| column.name_str().to_string())
                .collect();
            let columns = unique_query_columns(columns);
            let rows_affected = result.affected_rows();
            let mut rows = Vec::new();
            // Consume only the first result set: subsequent procedure result sets may
            // have different columns and cannot share this protocol's one schema.
            let mut first_set = true;
            while let Some(set) = result.iter() {
                for row in set {
                    let row = row.map_err(|err| format!("mysql query error: {err}"))?;
                    if first_set {
                        rows.push(mysql_row_to_json(&columns, row));
                    }
                }
                first_set = false;
            }
            Ok(QueryExecutionResult {
                rows,
                columns,
                rows_affected,
            })
        }
        LiveAdapter::PostgreSql(client) => {
            let backslash_escapes = if params.is_empty() {
                false
            } else {
                let row = client
                    .query_one("SHOW standard_conforming_strings", &[])
                    .map_err(|err| format!("postgresql SQL mode error: {err}"))?;
                row.get::<_, String>(0) == "off"
            };
            let sql = bind_query_params(sql, params, "postgresql", backslash_escapes, false)?;
            // Preparation supplies names/types even for empty results and rejects
            // multiple statements. Execute the original SQL, including SHOW,
            // EXPLAIN, DML RETURNING, and data-changing CTEs, without a SELECT wrapper.
            let statement = client
                .prepare(&sql)
                .map_err(|err| format!("postgresql query error: {err}"))?;
            let columns: Vec<String> = statement
                .columns()
                .iter()
                .map(|column| column.name().to_string())
                .collect();
            let columns = unique_query_columns(columns);
            let messages = client
                .simple_query(&sql)
                .map_err(|err| format!("postgresql query error: {err}"))?;
            let mut rows = Vec::new();
            let mut rows_affected = 0;
            for message in messages {
                match message {
                    postgres::SimpleQueryMessage::Row(row) => {
                        let mut object = serde_json::Map::new();
                        for (index, column) in statement.columns().iter().enumerate() {
                            let value = row
                                .get(index)
                                .map(|text| postgres_text_value(text, column.type_()))
                                .transpose()?
                                .unwrap_or(Value::Null);
                            object.insert(columns[index].clone(), value);
                        }
                        rows.push(Value::Object(object));
                    }
                    postgres::SimpleQueryMessage::CommandComplete(count) => rows_affected = count,
                    _ => {}
                }
            }
            Ok(QueryExecutionResult {
                rows,
                columns,
                rows_affected,
            })
        }
    }
}

fn postgres_text_value(text: &str, typ: &postgres::types::Type) -> Result<Value, String> {
    use postgres::types::{Kind, Type};
    // Catalog vectors are array types whose text format is whitespace separated.
    if *typ == Type::INT2_VECTOR || *typ == Type::OID_VECTOR {
        let element = if *typ == Type::INT2_VECTOR {
            &Type::INT2
        } else {
            &Type::OID
        };
        return text
            .split_whitespace()
            .map(|item| postgres_text_value(item, element))
            .collect::<Result<Vec<_>, _>>()
            .map(Value::Array);
    }
    if let Kind::Domain(base) = typ.kind() {
        return postgres_text_value(text, base);
    }
    if let Kind::Composite(fields) = typ.kind() {
        let body = text
            .strip_prefix('(')
            .and_then(|s| s.strip_suffix(')'))
            .ok_or("invalid PostgreSQL composite result")?;
        let bytes = body.as_bytes();
        let mut position = 0;
        let mut object = serde_json::Map::new();
        for (index, field) in fields.iter().enumerate() {
            let quoted = bytes.get(position) == Some(&b'"');
            if quoted {
                position += 1;
            }
            let mut value = Vec::new();
            while position < bytes.len() {
                let byte = bytes[position];
                if byte == b'\\' {
                    position += 1;
                    value.push(
                        *bytes
                            .get(position)
                            .ok_or("invalid PostgreSQL composite escape")?,
                    );
                    position += 1;
                } else if quoted && byte == b'"' {
                    position += 1;
                    if bytes.get(position) == Some(&b'"') {
                        value.push(b'"');
                        position += 1;
                    } else {
                        break;
                    }
                } else if !quoted && byte == b',' {
                    break;
                } else {
                    value.push(byte);
                    position += 1;
                }
            }
            let value = if !quoted && value.is_empty() {
                Value::Null
            } else {
                let value = String::from_utf8(value)
                    .map_err(|err| format!("invalid PostgreSQL composite text: {err}"))?;
                postgres_text_value(&value, field.type_())?
            };
            object.insert(field.name().to_string(), value);
            if index + 1 < fields.len() {
                if bytes.get(position) != Some(&b',') {
                    return Err("invalid PostgreSQL composite delimiter".to_string());
                }
                position += 1;
            }
        }
        return Ok(Value::Object(object));
    }
    if let Kind::Array(element) = typ.kind() {
        // PostgreSQL may prefix an array with explicit lower bounds, e.g. [0:1]=.
        let start = text.find('{').ok_or("invalid PostgreSQL array result")?;
        let mut position = start;
        return postgres_array_value(text, &mut position, element);
    }
    Ok(match *typ {
        Type::BOOL => Value::Bool(text == "t"),
        Type::INT2 | Type::INT4 | Type::INT8 | Type::FLOAT4 | Type::FLOAT8 | Type::NUMERIC => {
            // NaN and infinities are strings in PostgreSQL row_to_json as well.
            serde_json::from_str::<Value>(text)
                .ok()
                .filter(Value::is_number)
                .unwrap_or_else(|| Value::String(text.to_string()))
        }
        Type::JSON | Type::JSONB => serde_json::from_str(text)
            .map_err(|err| format!("invalid PostgreSQL JSON result: {err}"))?,
        // Types without JSON primitives use the server's native display text.
        // This includes temporal values (respecting DateStyle) and anonymous
        // RECORDs, whose field types are absent from the result metadata.
        _ => Value::String(text.to_string()),
    })
}

fn postgres_array_value(
    text: &str,
    position: &mut usize,
    element: &postgres::types::Type,
) -> Result<Value, String> {
    let bytes = text.as_bytes();
    // BOX is the built-in exception to PostgreSQL's comma array delimiter.
    let delimiter = if *element == postgres::types::Type::BOX {
        b';'
    } else {
        b','
    };
    if bytes.get(*position) != Some(&b'{') {
        return Err("invalid PostgreSQL array result".to_string());
    }
    *position += 1;
    let mut values = Vec::new();
    while *position < bytes.len() && bytes[*position] != b'}' {
        let value = if bytes[*position] == b'{' {
            postgres_array_value(text, position, element)?
        } else {
            let quoted = bytes[*position] == b'"';
            if quoted {
                *position += 1;
            }
            let mut value = Vec::new();
            while *position < bytes.len() {
                let byte = bytes[*position];
                if byte == b'\\' {
                    *position += 1;
                    value.push(
                        *bytes
                            .get(*position)
                            .ok_or("invalid PostgreSQL array escape")?,
                    );
                    *position += 1;
                } else if quoted && byte == b'"' {
                    *position += 1;
                    break;
                } else if !quoted && (byte == delimiter || byte == b'}') {
                    break;
                } else {
                    value.push(byte);
                    *position += 1;
                }
            }
            let value = String::from_utf8(value)
                .map_err(|err| format!("invalid PostgreSQL array text: {err}"))?;
            if !quoted && value == "NULL" {
                Value::Null
            } else {
                postgres_text_value(&value, element)?
            }
        };
        values.push(value);
        match bytes.get(*position) {
            Some(byte) if *byte == delimiter => *position += 1,
            Some(b'}') => break,
            _ => return Err("invalid PostgreSQL array delimiter".to_string()),
        }
    }
    if bytes.get(*position) != Some(&b'}') {
        return Err("unterminated PostgreSQL array result".to_string());
    }
    *position += 1;
    Ok(Value::Array(values))
}

/// SQL 주석 스캐너: `bytes[i]` 에서 시작하는 주석을 감지하면 그 주석 토큰 바로 다음
/// 인덱스를 반환하고, 주석 시작이 아니면 `None` 을 반환한다.
///
/// - 라인 주석(`--`, `allow_hash` 시 `#`): 종료 개행 `'\n'` 의 인덱스(개행 미소비).
///   개행이 없으면 `len`.
/// - 블록 주석(`/* */`): 닫는 `*/` 바로 다음 인덱스. 닫힘이 없으면 기존 산술상 `len+1`.
///
/// 반환 인덱스와 스캔 산술은 두 호출부(schema::mysql_definition_has_residual_definer,
/// schema::validate_single_view_statement)의
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

#[cfg(test)]
mod tests {
    use super::*;

    use serde_json::json;

    fn bind_query_params(sql: &str, params: &[Value]) -> String {
        super::bind_query_params(sql, params, "postgresql", false, false).unwrap()
    }

    fn execute_query_adapter(
        adapter: &mut LiveAdapter,
        sql: &str,
    ) -> Result<QueryExecutionResult, String> {
        super::execute_query_adapter(adapter, sql, &[])
    }

    #[test]
    fn query_binding_does_not_rewrite_literals_comments_or_inserted_values() {
        let sql = bind_query_params(
            "SELECT '$1 %s', \"$2\", $$body $1 %s$$, $tag$ $2 $tag$, %s, $2 -- $1 %s\n/* $2 */",
            &[json!("$2 %s"), json!(9)],
        );
        assert_eq!(
            sql,
            "SELECT '$1 %s', \"$2\", $$body $1 %s$$, $tag$ $2 $tag$, '$2 %s', 9 -- $1 %s\n/* $2 */"
        );
    }

    #[test]
    fn query_binding_matches_complete_numbered_placeholder() {
        let params: Vec<Value> = (101..=110).map(|n| json!(n)).collect();
        assert_eq!(
            bind_query_params("SELECT $10, $1, $10", &params),
            "SELECT 110, 101, 110"
        );
    }

    #[test]
    fn query_binding_respects_engine_quoting_and_reports_missing_values() {
        let params = [json!("a\\b' $2 %s")];
        assert_eq!(
            super::bind_query_params("SELECT %s", &params, "postgresql", false, false).unwrap(),
            "SELECT E'a\\\\b'' $2 %s'"
        );
        for backslash_escapes in [true, false] {
            assert_eq!(
                super::bind_query_params(
                    "SELECT %s, `col%s` # %s",
                    &params,
                    "mysql",
                    backslash_escapes,
                    false
                )
                .unwrap(),
                "SELECT CONVERT(X'615c6227202432202573' USING utf8mb4), `col%s` # %s"
            );
        }
        assert_eq!(
            super::bind_query_params("SELECT 1--1, %s", &[json!(7)], "mysql", true, false).unwrap(),
            "SELECT 1--1, 7"
        );
        assert_eq!(
            super::bind_query_params(
                "SELECT E'escaped\\' %s', %s",
                &[json!(7)],
                "postgresql",
                false,
                false
            )
            .unwrap(),
            "SELECT E'escaped\\' %s', 7"
        );
        assert_eq!(
            super::bind_query_params(
                "SELECT 'backslash\\', %s",
                &[json!(7)],
                "postgresql",
                false,
                false
            )
            .unwrap(),
            "SELECT 'backslash\\', 7"
        );
        assert_eq!(
            super::bind_query_params(
                "SELECT /* outer /* %s */ %s */ %s",
                &[json!(7)],
                "postgresql",
                false,
                false
            )
            .unwrap(),
            "SELECT /* outer /* %s */ %s */ 7"
        );
        assert_eq!(
            super::bind_query_params(
                "SELECT $한글$ %s $한글$, %s",
                &[json!(7)],
                "postgresql",
                false,
                false
            )
            .unwrap(),
            "SELECT $한글$ %s $한글$, 7"
        );
        assert!(
            super::bind_query_params("SELECT $2", &[json!(7)], "postgresql", false, false).is_err()
        );
        assert!(super::bind_query_params(
            "SELECT %s",
            &[json!(7), json!(8)],
            "postgresql",
            false,
            false
        )
        .is_err());
        assert!(
            super::bind_query_params("SELECT $0", &[json!(7)], "postgresql", false, false).is_err()
        );
    }

    #[test]
    fn mysql_query_statements_and_parameters_when_env_is_configured() {
        let Ok(url) = std::env::var("TF_QUERY_MYSQL_URL") else {
            return;
        };
        let pool = mysql::Pool::new(mysql::Opts::from_url(&url).unwrap()).unwrap();
        let mut adapter = LiveAdapter::MySql(pool.get_conn().unwrap());
        execute_query_adapter(
            &mut adapter,
            "CREATE TEMPORARY TABLE tf_query_regression (id int)",
        )
        .unwrap();
        let inserted = execute_query_adapter(
            &mut adapter,
            "INSERT INTO tf_query_regression VALUES (1), (2)",
        )
        .unwrap();
        assert_eq!(inserted.rows_affected, 2);
        let duplicate =
            execute_query_adapter(&mut adapter, "SELECT 1 AS x, 2 AS x, 3 AS `x (2)`").unwrap();
        assert_eq!(duplicate.columns, vec!["x", "x (3)", "x (2)"]);
        assert_eq!(
            duplicate.rows,
            vec![json!({"x":"1", "x (3)":"2", "x (2)":"3"})]
        );
        let selected = execute_query_adapter(
            &mut adapter,
            "WITH data AS (SELECT id FROM tf_query_regression) SELECT id FROM data ORDER BY id",
        )
        .unwrap();
        assert_eq!(selected.rows, vec![json!({"id":"1"}), json!({"id":"2"})]);
        let empty = execute_query_adapter(
            &mut adapter,
            "SELECT id FROM tf_query_regression WHERE false",
        )
        .unwrap();
        assert_eq!(empty.columns, vec!["id"]);
        assert!(empty.rows.is_empty());
        for setting in ["", "NO_BACKSLASH_ESCAPES"] {
            execute_query_adapter(&mut adapter, &format!("SET SESSION sql_mode='{setting}'"))
                .unwrap();
            let text = "한글\\b' $2 %s; DROP TABLE tf_query_regression; --\0";
            let result =
                super::execute_query_adapter(&mut adapter, "SELECT %s AS v", &[json!(text)])
                    .unwrap();
            assert_eq!(result.rows, vec![json!({"v":text})]);
        }
        execute_query_adapter(&mut adapter, "SET SESSION sql_mode='ANSI_QUOTES'").unwrap();
        let result = super::execute_query_adapter(
            &mut adapter,
            "SELECT 1 AS \"name\\\", %s AS v",
            &[json!(7)],
        )
        .unwrap();
        assert_eq!(result.rows, vec![json!({"name\\":"1", "v":"7"})]);
    }

    #[test]
    fn postgres_query_statements_and_types_when_env_is_configured() {
        let Ok(url) = std::env::var("TF_QUERY_POSTGRES_URL") else {
            return;
        };
        let client = postgres::Client::connect(&url, postgres::NoTls).unwrap();
        let mut adapter = LiveAdapter::PostgreSql(client);
        let duplicate =
            execute_query_adapter(&mut adapter, "SELECT 1 AS x, 2 AS x, 3 AS \"x (2)\"").unwrap();
        assert_eq!(duplicate.columns, vec!["x", "x (3)", "x (2)"]);
        assert_eq!(duplicate.rows, vec![json!({"x":1, "x (3)":2, "x (2)":3})]);
        for sql in ["SHOW server_version", "EXPLAIN SELECT 1"] {
            let result = execute_query_adapter(&mut adapter, sql).unwrap();
            assert!(!result.columns.is_empty(), "{sql}");
            assert!(!result.rows.is_empty(), "{sql}");
        }
        execute_query_adapter(
            &mut adapter,
            "CREATE TEMP TABLE tf_query_regression (id int)",
        )
        .unwrap();
        let inserted = execute_query_adapter(
            &mut adapter,
            "INSERT INTO tf_query_regression VALUES (1) RETURNING id",
        )
        .unwrap();
        assert_eq!(inserted.rows, vec![json!({"id": 1})]);
        let cte = execute_query_adapter(&mut adapter, "WITH changed AS (UPDATE tf_query_regression SET id=2 RETURNING id) SELECT * FROM changed").unwrap();
        assert_eq!(cte.rows, vec![json!({"id": 2})]);
        let composite = execute_query_adapter(
            &mut adapter,
            "SELECT t AS item FROM tf_query_regression AS t",
        )
        .unwrap();
        assert_eq!(composite.rows, vec![json!({"item":{"id":2}})]);
        for expression in [
            "ARRAY[box(point(1,2),point(3,4)),box(point(5,6),point(7,8))]",
            "'1 2'::int2vector",
            "'23 25'::oidvector",
        ] {
            let result = execute_query_adapter(
                &mut adapter,
                &format!("SELECT {expression} AS actual, to_json({expression}) AS expected"),
            )
            .unwrap();
            assert_eq!(
                result.rows[0]["actual"], result.rows[0]["expected"],
                "{expression}"
            );
        }
        let native_text = execute_query_adapter(
            &mut adapter,
            "SELECT timestamp '2026-01-02 03:04:05' AS stamp, ROW(1,'x'::text) AS anonymous",
        )
        .unwrap();
        assert_eq!(
            native_text.rows,
            vec![json!({"stamp":"2026-01-02 03:04:05", "anonymous":"(1,x)"})]
        );
        let values = execute_query_adapter(&mut adapter, "SELECT 42::bigint AS n, true AS b, NULL::int AS nil, 12.50::numeric AS decimal, '{\"k\":[1]}'::jsonb AS doc, ARRAY[1,NULL,3] AS arr, ARRAY['NULL','a,b',''] AS texts").unwrap();
        assert_eq!(
            values.rows,
            vec![
                json!({"n":42,"b":true,"nil":null,"decimal":12.5,"doc":{"k":[1]},"arr":[1,null,3],"texts":["NULL","a,b",""]})
            ]
        );
        let empty = execute_query_adapter(
            &mut adapter,
            "SELECT id FROM tf_query_regression WHERE false",
        )
        .unwrap();
        assert_eq!(empty.columns, vec!["id"]);
        assert!(empty.rows.is_empty());
        for setting in ["on", "off"] {
            execute_query_adapter(
                &mut adapter,
                &format!("SET standard_conforming_strings={setting}"),
            )
            .unwrap();
            let text = "a\\b' $2 %s; DROP TABLE tf_query_regression; --";
            let result =
                super::execute_query_adapter(&mut adapter, "SELECT %s::text AS v", &[json!(text)])
                    .unwrap();
            assert_eq!(result.rows, vec![json!({"v": text})]);
        }
        let nested = execute_query_adapter(
            &mut adapter,
            "SELECT ARRAY[[1,2],[3,4]] AS grid, ARRAY['a,b','quote\"','한글'] AS text_array",
        )
        .unwrap();
        assert_eq!(
            nested.rows,
            vec![json!({"grid":[[1,2],[3,4]],"text_array":["a,b","quote\"","한글"]})]
        );
    }

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
            tls: Default::default(),
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
            tls: Default::default(),
        };

        let first = unique_connection_id(&endpoint, 1);
        let second = unique_connection_id(&endpoint, 2);

        assert_ne!(first, second);
        assert!(first.starts_with(&connection_id(&endpoint)));
        assert!(second.starts_with(&connection_id(&endpoint)));
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
        );

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
