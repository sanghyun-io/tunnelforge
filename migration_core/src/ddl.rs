use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Write;

use crate::*;

pub(crate) fn read_engine(payload: &Value, key: &str) -> String {
    payload
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase()
}

pub(crate) fn is_supported_direction(source: &str, target: &str) -> bool {
    matches!(
        (source, target),
        ("mysql", "postgresql") | ("postgresql", "mysql")
    )
}

pub(crate) fn unsupported_objects(payload: &Value) -> Vec<String> {
    payload
        .get("unsupported_objects")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(ToString::to_string)
                .collect()
        })
        .unwrap_or_default()
}

pub fn generate_schema_ddl(
    schema: &NormalizedSchema,
    source: &str,
    target: &str,
) -> Result<Vec<String>, String> {
    // generate_table_ddl이 None을 주는 경우(변조 매니페스트의 유효하지 않은 table_collation 등)를
    // filter_map으로 조용히 누락하면, 미리보기/계획에서 테이블이 사라지고 migrate 경로에서는
    // ddl 인덱스가 어긋난다. 누락 대신 구조화된 에러로 전파해 fail-closed로 만든다.
    schema
        .tables
        .iter()
        .map(|table| {
            generate_table_ddl(table, source, target).ok_or_else(|| {
                format!(
                    "cannot generate DDL for table `{}` (invalid table collation?)",
                    table.name
                )
            })
        })
        .collect()
}

pub fn generate_post_data_ddl(schema: &NormalizedSchema, target: &str) -> Vec<String> {
    if target.is_empty() {
        return Vec::new();
    }
    let mut ddl = Vec::new();
    for table in &schema.tables {
        for index in &table.indexes {
            if index.columns.is_empty() {
                continue;
            }
            let unique = if index.unique { "UNIQUE " } else { "" };
            let columns = index
                .columns
                .iter()
                .map(|column| quote_ident(target, column))
                .collect::<Vec<_>>()
                .join(", ");
            ddl.push(format!(
                "CREATE {}INDEX {} ON {} ({});",
                unique,
                quote_ident(target, &index.name),
                quote_ident(target, &table.name),
                columns
            ));
        }
    }
    for table in &schema.tables {
        for fk in &table.foreign_keys {
            if fk.columns.is_empty() || fk.referenced_columns.is_empty() {
                continue;
            }
            let columns = fk
                .columns
                .iter()
                .map(|column| quote_ident(target, column))
                .collect::<Vec<_>>()
                .join(", ");
            let referenced_columns = fk
                .referenced_columns
                .iter()
                .map(|column| quote_ident(target, column))
                .collect::<Vec<_>>()
                .join(", ");
            ddl.push(format!(
                "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({});",
                quote_ident(target, &table.name),
                quote_ident(target, &fk.name),
                columns,
                quote_ident(target, &fk.referenced_table),
                referenced_columns
            ));
        }
    }
    ddl
}

pub fn generate_sequence_reset_ddl(schema: &NormalizedSchema, target: &str) -> Vec<String> {
    if target != "postgresql" {
        return Vec::new();
    }
    let mut ddl = Vec::new();
    for table in &schema.tables {
        for column in &table.columns {
            if is_auto_increment_type(&column.type_name) {
                ddl.push(format!(
                    "SELECT setval(pg_get_serial_sequence('{}', '{}'), COALESCE((SELECT MAX({}) FROM {}), 0) + 1, false);",
                    table.name.replace('\'', "''"),
                    column.name.replace('\'', "''"),
                    quote_ident(target, &column.name),
                    quote_ident(target, &table.name)
                ));
            }
        }
    }
    ddl
}

pub(crate) fn should_apply_post_load_ddl(mode: &str) -> bool {
    matches!(mode, "replace" | "recreate")
}

pub(crate) fn post_load_ddl_skip_message(mode: &str) -> String {
    format!("skipping post-load DDL for {mode} import; existing objects must already match")
}

pub(crate) fn apply_post_load_ddl<A: MigrationAdapter>(
    target: &mut A,
    schema: &NormalizedSchema,
    target_engine: &str,
) -> Result<(), String> {
    validate_foreign_key_column_compatibility(schema)?;
    for sql in generate_sequence_reset_ddl(schema, target_engine) {
        target
            .execute_sql(&sql)
            .map_err(|err| post_load_ddl_error(&sql, &err))?;
    }

    // MySQL: post-load 인덱스/FK DDL을 foreign_key_checks=0 상태에서 실행한다.
    //
    // 소스(Prod) 데이터에 원래부터 고아 레코드가 있을 수 있다(예: 삭제된 user를 참조하는
    // is_read_comment 행). foreign_key_checks=1 상태에서 ADD FOREIGN KEY를 하면 MySQL이
    // 기존 행을 검증해 ERROR 1452로 실패한다. mysqldump가 복원 전체를 FOREIGN_KEY_CHECKS=0
    // 으로 감싸는 것과 동일하게, 여기서도 checks를 꺼서 FK를 생성한다. FK 제약 자체는 정상
    // 등록되며, 이후 INSERT/UPDATE에는 그대로 강제된다 — 생성 시점의 기존 고아만 예외로 남는다
    // (소스 상태를 그대로 재현). 인덱스 생성은 checks와 무관하므로 함께 감싸도 결과가 같다.
    let is_mysql = target_engine == "mysql";
    if is_mysql {
        target
            .execute_sql("SET SESSION foreign_key_checks=0")
            .map_err(|err| post_load_ddl_error("SET SESSION foreign_key_checks=0", &err))?;
    }
    let ddl_result = (|| -> Result<(), String> {
        for sql in generate_post_data_ddl(schema, target_engine) {
            target
                .execute_sql(&sql)
                .map_err(|err| post_load_ddl_error(&sql, &err))?;
        }
        Ok(())
    })();
    if is_mysql {
        // 성공/실패 무관하게 복원한다(세션 상태 leak 방지). 복원 실패는 원 DDL 에러를
        // 가리지 않도록 best-effort로 무시한다.
        let _ = target.execute_sql("SET SESSION foreign_key_checks=1");
    }
    ddl_result
}

fn post_load_ddl_error(sql: &str, err: &str) -> String {
    let message = if is_mysql_table_full_error(err) {
        format!(
            "post-load DDL failed while executing {sql}: {err}; target MySQL storage or temporary table space is full. Increase target disk space, tmpdir capacity, or innodb_temp_data_file_path before retrying the import."
        )
    } else {
        format!("post-load DDL failed while executing {sql}: {err}")
    };
    classified_import_error("post_load_validation_failed", &message, None)
}

fn is_mysql_table_full_error(err: &str) -> bool {
    let normalized = err.to_ascii_lowercase();
    normalized.contains("error 1114")
        || normalized.contains("the table") && normalized.contains("is full")
}

pub fn count_sql(engine: &str, table: &str) -> String {
    format!(
        "SELECT COUNT(*) AS row_count FROM {}",
        quote_ident(engine, table)
    )
}

pub fn select_chunk_sql(
    engine: &str,
    table: &str,
    columns: &[String],
    key_columns: &[String],
) -> String {
    let projected_columns = columns
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    let order_columns: Vec<String> = if key_columns.is_empty() {
        columns.to_vec()
    } else {
        key_columns.to_vec()
    };
    let order_by = order_columns
        .iter()
        .map(|column| quote_column_ref(engine, table, column))
        .collect::<Vec<_>>()
        .join(", ");

    let limit_placeholder = if engine == "postgresql" { "$1" } else { "?" };
    let offset_placeholder = if engine == "postgresql" { "$2" } else { "?" };

    format!(
        "SELECT {} FROM {} ORDER BY {} LIMIT {} OFFSET {}",
        projected_columns,
        quote_ident(engine, table),
        order_by,
        limit_placeholder,
        offset_placeholder
    )
}

pub fn select_chunk_text_sql(
    engine: &str,
    table: &NormalizedTable,
    key_columns: &[String],
) -> String {
    let columns = column_names(table);
    let projected_columns = table
        .columns
        .iter()
        .map(|column| {
            if is_binary_type(&column.type_name) && engine == "postgresql" {
                format!(
                    "encode({}, 'hex') AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if is_binary_type(&column.type_name) {
                format!(
                    "HEX({}) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if engine == "postgresql" {
                format!(
                    "{}::text AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if engine == "mysql" {
                quote_ident(engine, &column.name)
            } else {
                format!(
                    "CAST({} AS CHAR) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let order_columns: Vec<String> = if key_columns.is_empty() {
        columns
    } else {
        key_columns.to_vec()
    };
    let order_by = order_columns
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    let limit_placeholder = if engine == "postgresql" { "$1" } else { "?" };
    let offset_placeholder = if engine == "postgresql" { "$2" } else { "?" };

    format!(
        "SELECT {} FROM {} ORDER BY {} LIMIT {} OFFSET {}",
        projected_columns,
        quote_ident(engine, &table.name),
        order_by,
        limit_placeholder,
        offset_placeholder
    )
}

pub fn select_chunk_text_after_key_sql(
    engine: &str,
    table: &NormalizedTable,
    key_columns: &[String],
    last_key_values: Option<&[String]>,
    limit: usize,
) -> String {
    let columns = column_names(table);
    let projected_columns = table
        .columns
        .iter()
        .map(|column| {
            if is_binary_type(&column.type_name) && engine == "postgresql" {
                format!(
                    "encode({}, 'hex') AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if is_binary_type(&column.type_name) {
                format!(
                    "HEX({}) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if engine == "postgresql" {
                format!(
                    "{}::text AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if engine == "mysql" {
                quote_ident(engine, &column.name)
            } else {
                format!(
                    "CAST({} AS CHAR) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let order_by = key_columns
        .iter()
        .map(|column| quote_column_ref(engine, &table.name, column))
        .collect::<Vec<_>>()
        .join(", ");
    let where_clause = if let Some(values) = last_key_values {
        let predicates = keyset_predicates(engine, &table.name, key_columns, values);
        if predicates.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", predicates.join(" OR "))
        }
    } else {
        String::new()
    };

    format!(
        "SELECT {} FROM {}{} ORDER BY {} LIMIT {}",
        projected_columns,
        quote_ident(engine, &table.name),
        where_clause,
        if order_by.is_empty() {
            columns
                .iter()
                .map(|column| quote_column_ref(engine, &table.name, column))
                .collect::<Vec<_>>()
                .join(", ")
        } else {
            order_by
        },
        limit
    )
}

pub fn select_chunk_text_range_sql(
    engine: &str,
    table: &NormalizedTable,
    key_column: &str,
    start: i128,
    end: i128,
) -> String {
    let projected_columns = table
        .columns
        .iter()
        .map(|column| {
            if is_binary_type(&column.type_name) && engine == "postgresql" {
                format!(
                    "encode({}, 'hex') AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if is_binary_type(&column.type_name) {
                format!(
                    "HEX({}) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if engine == "postgresql" {
                format!(
                    "{}::text AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if engine == "mysql" {
                quote_ident(engine, &column.name)
            } else {
                format!(
                    "CAST({} AS CHAR) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let key_ref = quote_column_ref(engine, &table.name, key_column);
    format!(
        "SELECT {} FROM {} WHERE {} >= {} AND {} <= {} ORDER BY {}",
        projected_columns,
        quote_ident(engine, &table.name),
        key_ref,
        start,
        key_ref,
        end,
        key_ref
    )
}

fn keyset_predicates(
    engine: &str,
    table: &str,
    key_columns: &[String],
    values: &[String],
) -> Vec<String> {
    let pair_count = key_columns.len().min(values.len());
    let mut predicates = Vec::new();
    for index in 0..pair_count {
        let mut parts = Vec::new();
        for previous in 0..index {
            parts.push(format!(
                "{} = {}",
                quote_column_ref(engine, table, &key_columns[previous]),
                sql_literal(&Value::String(values[previous].clone()))
            ));
        }
        parts.push(format!(
            "{} > {}",
            quote_column_ref(engine, table, &key_columns[index]),
            sql_literal(&Value::String(values[index].clone()))
        ));
        predicates.push(format!("({})", parts.join(" AND ")));
    }
    predicates
}

pub fn insert_sql(engine: &str, table: &str, columns: &[String]) -> String {
    let column_sql = columns
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    let placeholders = (1..=columns.len())
        .map(|index| {
            if engine == "postgresql" {
                format!("${index}")
            } else {
                "?".to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "INSERT INTO {} ({}) VALUES ({})",
        quote_ident(engine, table),
        column_sql,
        placeholders
    )
}

pub fn insert_rows_literal_sql(
    engine: &str,
    table: &str,
    columns: &[String],
    rows: &[Value],
) -> String {
    let column_sql = columns
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    let values_sql = rows
        .iter()
        .map(|row| {
            let values = columns
                .iter()
                .map(|column| match row {
                    Value::Object(object) => {
                        sql_literal(object.get(column).unwrap_or(&Value::Null))
                    }
                    _ => "NULL".to_string(),
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!("({values})")
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "INSERT INTO {} ({}) VALUES {}",
        quote_ident(engine, table),
        column_sql,
        values_sql
    )
}

pub fn insert_rows_literal_sql_for_table(
    target_engine: &str,
    table: &NormalizedTable,
    rows: &[Value],
) -> String {
    let columns = column_names(table);
    let column_sql = columns
        .iter()
        .map(|column| quote_ident(target_engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    let values_sql = rows
        .iter()
        .map(|row| {
            let values = table
                .columns
                .iter()
                .map(|column| match row {
                    Value::Object(object) => sql_literal_for_column(
                        target_engine,
                        &column.type_name,
                        object.get(&column.name).unwrap_or(&Value::Null),
                    ),
                    _ => "NULL".to_string(),
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!("({values})")
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "INSERT INTO {} ({}) VALUES {}",
        quote_ident(target_engine, &table.name),
        column_sql,
        values_sql
    )
}

pub(crate) fn copy_rows_to_postgres(
    client: &mut postgres::Client,
    table: &NormalizedTable,
    rows: &[Value],
) -> Result<(), String> {
    let sql = copy_rows_csv_sql("postgresql", table);
    let mut writer = client
        .copy_in(&sql)
        .map_err(|err| format_postgres_error("postgresql copy start error", &err))?;
    for row in rows {
        let line = copy_csv_line_for_table("postgresql", table, row);
        writer
            .write_all(line.as_bytes())
            .map_err(|err| format!("postgresql copy write error: {err}"))?;
    }
    writer
        .finish()
        .map(|_| ())
        .map_err(|err| format_postgres_error("postgresql copy finish error", &err))
}

pub(crate) fn format_postgres_error(context: &str, err: &postgres::Error) -> String {
    let mut parts = vec![format!("{context}: {err}")];
    if let Some(db_error) = err.as_db_error() {
        parts.push(format!("code={}", db_error.code().code()));
        parts.push(format!("message={}", db_error.message()));
        if let Some(detail) = db_error.detail() {
            parts.push(format!("detail={detail}"));
        }
        if let Some(hint) = db_error.hint() {
            parts.push(format!("hint={hint}"));
        }
        if let Some(where_) = db_error.where_() {
            parts.push(format!("context={where_}"));
        }
        if let Some(table) = db_error.table() {
            parts.push(format!("table={table}"));
        }
        if let Some(column) = db_error.column() {
            parts.push(format!("column={column}"));
        }
        if let Some(constraint) = db_error.constraint() {
            parts.push(format!("constraint={constraint}"));
        }
    }
    parts.join("; ")
}

pub fn copy_rows_csv_sql(target_engine: &str, table: &NormalizedTable) -> String {
    let columns = column_names(table)
        .iter()
        .map(|column| quote_ident(target_engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "COPY {} ({}) FROM STDIN WITH (FORMAT csv, NULL '\\N')",
        quote_ident(target_engine, &table.name),
        columns
    )
}

pub fn copy_csv_line_for_table(
    target_engine: &str,
    table: &NormalizedTable,
    row: &Value,
) -> String {
    let fields = table
        .columns
        .iter()
        .map(|column| match row {
            Value::Object(object) => copy_csv_field_for_column(
                target_engine,
                &column.type_name,
                object.get(&column.name).unwrap_or(&Value::Null),
            ),
            _ => "\\N".to_string(),
        })
        .collect::<Vec<_>>()
        .join(",");
    format!("{fields}\n")
}

pub fn copy_csv_field_for_column(target_engine: &str, source_type: &str, value: &Value) -> String {
    if value.is_null() {
        return "\\N".to_string();
    }
    let mut text = match value {
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => value.clone(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
        Value::Null => unreachable!(),
    };

    let source_type = source_type.to_ascii_lowercase();
    if target_engine == "postgresql" && source_type.starts_with("tinyint(1)") {
        if text == "1" || text.eq_ignore_ascii_case("true") {
            text = "true".to_string();
        } else if text == "0" || text.eq_ignore_ascii_case("false") {
            text = "false".to_string();
        }
    }
    if target_engine == "postgresql" && is_binary_type(&source_type) {
        text = format!("\\x{}", text.trim());
    }
    if target_engine == "postgresql" && !is_binary_type(&source_type) {
        text = sanitize_postgresql_text(&text);
    }

    csv_quote(&text)
}

fn csv_quote(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

pub(crate) fn sanitize_postgresql_text(value: &str) -> String {
    value.replace('\0', "")
}

pub fn sql_literal_for_column(target_engine: &str, source_type: &str, value: &Value) -> String {
    if let Value::String(text) = value {
        let source_type = source_type.to_ascii_lowercase();
        if is_binary_type(&source_type) {
            let hex = text.trim();
            if target_engine == "postgresql" {
                return format!("decode('{}', 'hex')", hex.replace('\'', "''"));
            }
            return format!("X'{}'", hex.replace('\'', "''"));
        }
        if target_engine == "mysql" && matches!(source_type.as_str(), "boolean" | "bool") {
            if text.eq_ignore_ascii_case("true") {
                return "1".to_string();
            }
            if text.eq_ignore_ascii_case("false") {
                return "0".to_string();
            }
        }
        if target_engine == "postgresql" && source_type.starts_with("tinyint(1)") {
            if text == "1" || text.eq_ignore_ascii_case("true") {
                return "TRUE".to_string();
            }
            if text == "0" || text.eq_ignore_ascii_case("false") {
                return "FALSE".to_string();
            }
        }
        if target_engine == "postgresql" {
            return sql_literal(&Value::String(sanitize_postgresql_text(text)));
        }
        if target_engine == "mysql" && is_json_type(&source_type) {
            return mysql_json_literal(value);
        }
        if target_engine == "mysql" {
            return mysql_sql_literal(value);
        }
    }
    if target_engine == "mysql" && is_json_type(source_type) {
        return mysql_json_literal(value);
    }
    if target_engine == "mysql" {
        return mysql_sql_literal(value);
    }
    sql_literal(value)
}

fn is_json_type(type_name: &str) -> bool {
    let type_name = type_name.trim().to_ascii_lowercase();
    type_name == "json" || type_name.starts_with("json ")
}

pub fn is_binary_type(type_name: &str) -> bool {
    let type_name = type_name.to_ascii_lowercase();
    type_name.contains("blob")
        || type_name.contains("binary")
        || type_name == "bytea"
        || type_name.starts_with("varbinary")
}

pub(crate) fn has_binary_columns(table: &NormalizedTable) -> bool {
    table
        .columns
        .iter()
        .any(|column| is_binary_type(&column.type_name))
}

pub fn is_decimal_type(type_name: &str) -> bool {
    let type_name = type_name.trim().to_ascii_lowercase();
    type_name.starts_with("decimal") || type_name.starts_with("numeric")
}

pub fn is_date_type(type_name: &str) -> bool {
    type_name.trim().eq_ignore_ascii_case("date")
}

pub fn is_time_type(type_name: &str) -> bool {
    let type_name = type_name.trim().to_ascii_lowercase();
    type_name == "time" || type_name.starts_with("time ") || type_name.starts_with("time(")
}

pub fn is_timestamp_type(type_name: &str) -> bool {
    let type_name = type_name.trim().to_ascii_lowercase();
    type_name.starts_with("datetime") || type_name.starts_with("timestamp")
}

pub fn sql_literal(value: &Value) -> String {
    match value {
        Value::Null => "NULL".to_string(),
        Value::Bool(value) => {
            if *value {
                "TRUE".to_string()
            } else {
                "FALSE".to_string()
            }
        }
        Value::Number(value) => value.to_string(),
        Value::String(value) => format!("'{}'", value.replace('\'', "''")),
        Value::Array(_) | Value::Object(_) => {
            format!("'{}'", value.to_string().replace('\'', "''"))
        }
    }
}

fn mysql_sql_literal(value: &Value) -> String {
    match value {
        Value::Null => "NULL".to_string(),
        Value::Bool(value) => {
            if *value {
                "TRUE".to_string()
            } else {
                "FALSE".to_string()
            }
        }
        Value::Number(value) => value.to_string(),
        Value::String(value) => mysql_string_literal(value),
        Value::Array(_) | Value::Object(_) => mysql_string_literal(&value.to_string()),
    }
}

fn mysql_json_literal(value: &Value) -> String {
    match value {
        Value::Null => "NULL".to_string(),
        Value::String(value) => mysql_utf8mb4_string_literal(value),
        Value::Array(_) | Value::Object(_) => mysql_utf8mb4_string_literal(&value.to_string()),
        Value::Bool(_) | Value::Number(_) => mysql_utf8mb4_string_literal(&value.to_string()),
    }
}

fn mysql_utf8mb4_string_literal(value: &str) -> String {
    format!("_utf8mb4{}", mysql_string_literal(value))
}

fn mysql_string_literal(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "''"))
}

pub fn inspect_tables_sql(engine: &str) -> &'static str {
    if engine == "postgresql" {
        "SELECT table_name FROM information_schema.tables WHERE table_schema = $1 AND table_type = 'BASE TABLE' ORDER BY table_name"
    } else {
        "SELECT TABLE_NAME AS table_name, TABLE_COLLATION AS table_collation FROM information_schema.tables WHERE table_schema = ? AND table_type = 'BASE TABLE' ORDER BY TABLE_NAME"
    }
}

pub fn inspect_columns_sql(engine: &str) -> &'static str {
    if engine == "postgresql" {
        "SELECT column_name, data_type, is_nullable, character_maximum_length, numeric_precision, numeric_scale, column_default, is_identity FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position"
    } else {
        "SELECT COLUMN_NAME AS column_name, COLUMN_TYPE AS data_type, CHARACTER_SET_NAME AS character_set, COLLATION_NAME AS collation, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default, EXTRA AS extra FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ORDINAL_POSITION"
    }
}

pub fn postgresql_column_type(
    data_type: &str,
    max_length: Option<i32>,
    numeric_precision: Option<i32>,
    numeric_scale: Option<i32>,
) -> String {
    match data_type {
        "character varying" => max_length
            .map(|length| format!("varchar({length})"))
            .unwrap_or_else(|| "varchar".to_string()),
        "character" => max_length
            .map(|length| format!("char({length})"))
            .unwrap_or_else(|| "char".to_string()),
        "numeric" | "decimal" => match (numeric_precision, numeric_scale) {
            (Some(precision), Some(scale)) => format!("numeric({precision},{scale})"),
            (Some(precision), None) => format!("numeric({precision})"),
            _ => data_type.to_string(),
        },
        _ => data_type.to_string(),
    }
}

pub fn inspect_keys_sql(engine: &str) -> &'static str {
    if engine == "postgresql" {
        "SELECT kcu.column_name, tc.constraint_type FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_schema = kcu.constraint_schema AND tc.constraint_name = kcu.constraint_name WHERE tc.table_schema = $1 AND tc.table_name = $2 AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') ORDER BY tc.constraint_type, kcu.ordinal_position"
    } else {
        "SELECT kcu.COLUMN_NAME AS column_name, tc.CONSTRAINT_TYPE AS constraint_type FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.KEY_COLUMN_USAGE kcu ON tc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc.TABLE_NAME = kcu.TABLE_NAME WHERE tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ? AND tc.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE') ORDER BY tc.CONSTRAINT_TYPE, kcu.ORDINAL_POSITION"
    }
}

pub fn inspect_foreign_keys_sql(engine: &str) -> &'static str {
    if engine == "postgresql" {
        "SELECT tc.constraint_name, kcu.column_name, ccu.table_name AS referenced_table, ccu.column_name AS referenced_column FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_schema = kcu.constraint_schema AND tc.constraint_name = kcu.constraint_name JOIN information_schema.constraint_column_usage ccu ON tc.constraint_schema = ccu.constraint_schema AND tc.constraint_name = ccu.constraint_name WHERE tc.table_schema = $1 AND tc.table_name = $2 AND tc.constraint_type = 'FOREIGN KEY' ORDER BY tc.constraint_name, kcu.ordinal_position"
    } else {
        "SELECT CONSTRAINT_NAME AS constraint_name, COLUMN_NAME AS column_name, REFERENCED_TABLE_NAME AS referenced_table, REFERENCED_COLUMN_NAME AS referenced_column FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION"
    }
}

pub fn inspect_indexes_sql(engine: &str) -> &'static str {
    if engine == "postgresql" {
        "SELECT i.relname AS index_name, a.attname AS column_name, ix.indisunique AS is_unique FROM pg_class t JOIN pg_index ix ON t.oid = ix.indrelid JOIN pg_class i ON i.oid = ix.indexrelid JOIN pg_namespace n ON n.oid = t.relnamespace JOIN unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum WHERE n.nspname = $1 AND t.relname = $2 AND NOT ix.indisprimary ORDER BY i.relname, k.ord"
    } else {
        "SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name, CASE WHEN NON_UNIQUE = 0 THEN 1 ELSE 0 END AS is_unique FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_NAME <> 'PRIMARY' ORDER BY INDEX_NAME, SEQ_IN_INDEX"
    }
}

pub(crate) fn apply_key_flags(
    mut columns: Vec<NormalizedColumn>,
    keys: &[(String, String)],
) -> Vec<NormalizedColumn> {
    for column in &mut columns {
        for (key_column, constraint_type) in keys {
            if key_column == &column.name {
                if constraint_type.eq_ignore_ascii_case("PRIMARY KEY") {
                    column.primary_key = true;
                } else if constraint_type.eq_ignore_ascii_case("UNIQUE") {
                    column.unique = true;
                }
            }
        }
    }
    columns
}

pub(crate) fn group_indexes(rows: Vec<(String, String, bool)>) -> Vec<NormalizedIndex> {
    let mut grouped: BTreeMap<String, NormalizedIndex> = BTreeMap::new();
    for (name, column, unique) in rows {
        let index = grouped
            .entry(name.clone())
            .or_insert_with(|| NormalizedIndex {
                name,
                columns: Vec::new(),
                unique,
            });
        index.unique = index.unique || unique;
        index.columns.push(column);
    }
    grouped.into_values().collect()
}

pub(crate) fn group_foreign_keys(rows: Vec<(String, String, String, String)>) -> Vec<NormalizedForeignKey> {
    let mut grouped: BTreeMap<String, NormalizedForeignKey> = BTreeMap::new();
    for (name, column, referenced_table, referenced_column) in rows {
        let fk = grouped
            .entry(name.clone())
            .or_insert_with(|| NormalizedForeignKey {
                name,
                columns: Vec::new(),
                referenced_table,
                referenced_columns: Vec::new(),
            });
        fk.columns.push(column);
        fk.referenced_columns.push(referenced_column);
    }
    grouped.into_values().collect()
}

pub(crate) fn generate_table_ddl(table: &NormalizedTable, source: &str, target: &str) -> Option<String> {
    let mut lines = Vec::new();
    let mut primary_keys = Vec::new();

    for column in &table.columns {
        let auto_increment = is_auto_increment_type(&column.type_name);
        let stripped = strip_generation_marker(&column.type_name);
        let mapped_type = map_type(source, target, &stripped);
        // 최종 DDL에 들어가는 타입 문자열(mapped_type)을 검증한다. same-engine은 원문이 그대로 들어가고,
        // cross-engine도 map_type이 varchar/decimal/numeric 등에서 원문을 대문자화만 해 통과시키므로
        // (예: `varchar(45), evil int` -> `VARCHAR(45), EVIL INT`), 변환 후 값을 검증해야 same/cross-engine
        // 모든 경로에서 컬럼 정의 탈출(CTAS/추가 컬럼 주입)을 fail-closed로 막을 수 있다.
        if !is_safe_column_type(&mapped_type) {
            return None;
        }
        let default_sql = if auto_increment {
            String::new()
        } else {
            default_clause(target, column.default_value.as_deref(), &column.type_name)
        };
        let null_sql = if column.nullable { "" } else { " NOT NULL" };
        let generation_sql = if auto_increment && target == "postgresql" {
            " GENERATED BY DEFAULT AS IDENTITY"
        } else if auto_increment && target == "mysql" {
            " AUTO_INCREMENT"
        } else {
            ""
        };
        lines.push(format!(
            "  {} {}{}{}{}",
            quote_ident(target, &column.name),
            mapped_type,
            generation_sql,
            default_sql,
            null_sql
        ));
        if column.primary_key {
            primary_keys.push(quote_ident(target, &column.name));
        }
    }

    if !primary_keys.is_empty() {
        lines.push(format!("  PRIMARY KEY ({})", primary_keys.join(", ")));
    }

    // 테이블 레벨 기본 collation 재현은 같은 엔진(MySQL→MySQL)에서만 한다.
    // cross-engine이나 PostgreSQL 타겟에는 테이블 레벨 DEFAULT COLLATE 개념이 없어 붙이면 오류가 난다.
    // COLLATE만 지정해도 MySQL이 해당 collation의 charset을 자동 결정하므로 charset은 별도로 방출하지 않는다.
    let table_suffix =
        if source.eq_ignore_ascii_case(target) && target.eq_ignore_ascii_case("mysql") {
            match table
                .table_collation
                .as_deref()
                .map(str::trim)
                .filter(|collation| !collation.is_empty())
            {
                // dump 매니페스트는 변조 가능한 파일이므로, collation 값을 그대로 DDL에 끼우면
                // `utf8mb4_bin AS SELECT ...`(CTAS) 나 `utf8mb4_bin ENGINE=MyISAM` 같은
                // 테이블 옵션/구문 주입이 가능하다(SQL injection). MySQL collation 식별자 형태만
                // 허용하고, 위반 시 fail-closed로 DDL 생성을 거부한다(import 중단).
                Some(collation) if is_valid_mysql_collation_ident(collation) => {
                    format!(" COLLATE={collation}")
                }
                Some(_) => return None,
                None => String::new(),
            }
        } else {
            String::new()
        };

    Some(format!(
        "CREATE TABLE {} (\n{}\n){};",
        quote_ident(target, &table.name),
        lines.join(",\n"),
        table_suffix
    ))
}

/// dump 매니페스트에서 온 collation 문자열이 MySQL collation 식별자로 안전한지 검사한다.
/// 영숫자와 밑줄로만 이루어진 1~64자만 허용하여, 공백/괄호/세미콜론/등호 등을 통한
/// CTAS·table_options SQL 주입을 fail-closed로 차단한다.
fn is_valid_mysql_collation_ident(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// dump 매니페스트에서 온 MySQL 컬럼 타입 문자열이 안전한 문법인지 검사한다.
/// 같은 엔진(MySQL→MySQL) import 시 type_name은 검증 없이 그대로 CREATE TABLE 컬럼 정의에 들어가므로,
/// 변조된 값(`int) AS (SELECT ...`, `int, evil int`, `int; ...` 등)이 컬럼 정의를 탈출하지 못하게 막는다.
/// 허용 문법: <base ident> [ '(' (숫자리스트 | 따옴표문자열리스트) ')' ] [ modifier ]*
///   modifier = unsigned | zerofill | (character set | charset | collate) <ident>
/// enum('a','b'), decimal(10,2), varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin 등 정상 타입은 통과한다.
/// enum/set 값 리스트는 따옴표 문자열이라 단순 식별자 allowlist로는 걸러낼 수 없어 구조적으로 파싱한다.
fn is_safe_column_type(type_name: &str) -> bool {
    let s = type_name.trim();
    if s.is_empty() || s.len() > 512 {
        return false;
    }
    let bytes = s.as_bytes();
    let mut i = 0usize;

    // 1) base 식별자: ascii 알파벳으로 시작, 이후 영숫자/밑줄
    if !bytes[i].is_ascii_alphabetic() {
        return false;
    }
    i += 1;
    while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
        i += 1;
    }

    // 2) 선택적 인자 그룹 '(' ... ')'
    if i < bytes.len() && bytes[i] == b'(' {
        i += 1;
        while i < bytes.len() && bytes[i] == b' ' {
            i += 1;
        }
        if i < bytes.len() && bytes[i] == b'\'' {
            // 따옴표 문자열 리스트 (enum/set): qstr (',' qstr)*
            loop {
                if i >= bytes.len() || bytes[i] != b'\'' {
                    return false;
                }
                i += 1;
                loop {
                    if i >= bytes.len() {
                        return false; // 닫히지 않은 문자열
                    }
                    // enum/set 문자열 값에 백슬래시가 있으면 fail-closed로 거부한다.
                    // 백슬래시를 일반 문자로 취급하면 기본 MySQL 모드(백슬래시=이스케이프)에서 validator와
                    // 서버의 따옴표 경계가 어긋나 우회되고(예: enum('a\', ') , evil int -- ')), 반대로
                    // 이스케이프로 취급하면 NO_BACKSLASH_ESCAPES 모드에서 어긋난다. 어느 모드에서도 안전하도록
                    // 백슬래시를 포함한 값 자체를 거부한다(정상 enum/set 값에 백슬래시는 실사용상 드묾).
                    match bytes[i] {
                        b'\\' => return false,
                        b'\'' => {
                            if i + 1 < bytes.len() && bytes[i + 1] == b'\'' {
                                i += 2; // '' 이스케이프된 따옴표
                            } else {
                                i += 1; // 닫는 따옴표
                                break;
                            }
                        }
                        _ => i += 1,
                    }
                }
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                if i < bytes.len() && bytes[i] == b',' {
                    i += 1;
                    while i < bytes.len() && bytes[i] == b' ' {
                        i += 1;
                    }
                    continue;
                }
                break;
            }
        } else {
            // 숫자 리스트: digits (',' digits)*
            loop {
                let ds = i;
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                }
                if i == ds {
                    return false;
                }
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                if i < bytes.len() && bytes[i] == b',' {
                    i += 1;
                    while i < bytes.len() && bytes[i] == b' ' {
                        i += 1;
                    }
                    continue;
                }
                break;
            }
        }
        while i < bytes.len() && bytes[i] == b' ' {
            i += 1;
        }
        if i >= bytes.len() || bytes[i] != b')' {
            return false;
        }
        i += 1;
    }

    // 3) 후행 modifier들 (공백 구분)
    loop {
        while i < bytes.len() && bytes[i] == b' ' {
            i += 1;
        }
        if i >= bytes.len() {
            break;
        }
        let ws = i;
        while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
            i += 1;
        }
        if i == ws {
            return false; // 비단어 문자(괄호/세미콜론/등호 등) → 거부
        }
        let word = s[ws..i].to_ascii_lowercase();
        match word.as_str() {
            "unsigned" | "zerofill" => {}
            // PostgreSQL 원형 다단어 타입 꼬리 허용: `double precision`, `bit/character varying`,
            // `timestamp/time with|without time zone`. same-engine PostgreSQL에서는 map_type이
            // 원문 type_name을 그대로 반환하므로, 이 타입들을 거부하면 정상 import가 깨진다(fidelity).
            "precision" => {}
            "varying" => {
                // character varying(255) / bit varying(8) — 선택적 길이 인자를 허용한다.
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                if i < bytes.len() && bytes[i] == b'(' {
                    i += 1;
                    while i < bytes.len() && bytes[i] == b' ' {
                        i += 1;
                    }
                    let ds = i;
                    while i < bytes.len() && bytes[i].is_ascii_digit() {
                        i += 1;
                    }
                    if i == ds {
                        return false;
                    }
                    while i < bytes.len() && bytes[i] == b' ' {
                        i += 1;
                    }
                    if i >= bytes.len() || bytes[i] != b')' {
                        return false;
                    }
                    i += 1;
                }
            }
            "with" | "without" => {
                for expected in ["time", "zone"] {
                    while i < bytes.len() && bytes[i] == b' ' {
                        i += 1;
                    }
                    let ws2 = i;
                    while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
                        i += 1;
                    }
                    if !s[ws2..i].eq_ignore_ascii_case(expected) {
                        return false;
                    }
                }
            }
            "charset" | "collate" => {
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                let is2 = i;
                while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                    i += 1;
                }
                if i == is2 {
                    return false;
                }
            }
            "character" => {
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                let ss = i;
                while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
                    i += 1;
                }
                if !s[ss..i].eq_ignore_ascii_case("set") {
                    return false;
                }
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                let is2 = i;
                while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                    i += 1;
                }
                if i == is2 {
                    return false;
                }
            }
            _ => return false,
        }
    }

    true
}

fn default_clause(target: &str, default_value: Option<&str>, source_type: &str) -> String {
    let Some(default_value) = default_value
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        return String::new();
    };
    if default_value.eq_ignore_ascii_case("null") {
        return String::new();
    }
    format!(
        " DEFAULT {}",
        map_default_literal(target, default_value, source_type)
    )
}

fn map_default_literal(target: &str, default_value: &str, source_type: &str) -> String {
    let value = strip_postgresql_type_cast(default_value.trim());
    let upper = value.to_ascii_uppercase();
    let source_type = source_type.to_ascii_lowercase();
    if target == "postgresql" && source_type.starts_with("tinyint(1)") {
        if matches!(value, "1") || value.eq_ignore_ascii_case("true") {
            return "TRUE".to_string();
        }
        if matches!(value, "0") || value.eq_ignore_ascii_case("false") {
            return "FALSE".to_string();
        }
    }
    if target == "mysql" && matches!(source_type.as_str(), "boolean" | "bool") {
        if value.eq_ignore_ascii_case("true") {
            return "1".to_string();
        }
        if value.eq_ignore_ascii_case("false") {
            return "0".to_string();
        }
    }
    if matches!(
        upper.as_str(),
        "CURRENT_TIMESTAMP" | "CURRENT_DATE" | "CURRENT_TIME" | "TRUE" | "FALSE"
    ) || value.parse::<f64>().is_ok()
    {
        if target == "mysql" && upper == "TRUE" {
            return "1".to_string();
        }
        if target == "mysql" && upper == "FALSE" {
            return "0".to_string();
        }
        return upper;
    }
    // bit 리터럴 b'0101'은 정확한 형태(0/1로만 채워지고 정상적으로 닫힌 경우)만 그대로 통과시킨다.
    // 변조된 `b'0') AS (SELECT ...`처럼 닫는 따옴표 없이 컬럼 정의를 탈출하는 값은 여기서 걸러져
    // 아래 문자열 재이스케이프 경로로 떨어진다.
    if let Some(bits) = value
        .strip_prefix("b'")
        .and_then(|rest| rest.strip_suffix('\''))
    {
        if !bits.is_empty() && bits.bytes().all(|b| b == b'0' || b == b'1') {
            return value.to_string();
        }
    }
    // 그 외에는 항상 하나의 안전한 문자열 리터럴로 재이스케이프한다. 이미 '...'로 감싼 값도 그대로
    // 통과시키지 않고 dequote 후 재이스케이프하여 `'x', evil int, y varchar(1) DEFAULT 'z'` 같은
    // 컬럼 정의 주입을 차단한다(변조 매니페스트 대비).
    let inner = if value.len() >= 2 && value.starts_with('\'') && value.ends_with('\'') {
        value[1..value.len() - 1].replace("''", "'")
    } else {
        value.to_string()
    };
    format!("'{}'", inner.replace('\\', "\\\\").replace('\'', "''"))
}

fn strip_postgresql_type_cast(value: &str) -> &str {
    value
        .split_once("::")
        .map(|(literal, _)| literal)
        .unwrap_or(value)
        .trim()
}

pub(crate) fn with_auto_increment_marker(type_name: &str, extra: &str) -> String {
    if extra.to_ascii_lowercase().contains("auto_increment") {
        format!("{type_name} auto_increment")
    } else {
        type_name.to_string()
    }
}

pub(crate) fn mysql_type_with_character_options(
    type_name: &str,
    character_set: Option<String>,
    collation: Option<String>,
) -> String {
    let mut enriched = type_name.trim().to_string();
    let lower = enriched.to_ascii_lowercase();
    if let Some(character_set) = character_set.filter(|value| !value.trim().is_empty()) {
        if !lower.contains(" character set ") && !lower.contains(" charset ") {
            enriched.push_str(" CHARACTER SET ");
            enriched.push_str(character_set.trim());
        }
    }
    let lower = enriched.to_ascii_lowercase();
    if let Some(collation) = collation.filter(|value| !value.trim().is_empty()) {
        if !lower.contains(" collate ") {
            enriched.push_str(" COLLATE ");
            enriched.push_str(collation.trim());
        }
    }
    enriched
}

#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct MysqlCharacterFidelity {
    pub(crate) character_set: Option<String>,
    pub(crate) collation: Option<String>,
}

pub(crate) fn mysql_character_fidelity(type_name: &str) -> MysqlCharacterFidelity {
    MysqlCharacterFidelity {
        character_set: mysql_type_option_value(type_name, "character set")
            .or_else(|| mysql_type_option_value(type_name, "charset")),
        collation: mysql_type_option_value(type_name, "collate"),
    }
}

fn mysql_type_option_value(type_name: &str, option: &str) -> Option<String> {
    let lower = type_name.to_ascii_lowercase();
    let start = lower.find(option)? + option.len();
    let value = type_name[start..].trim_start();
    let value = value
        .split(|character: char| character.is_whitespace() || character == ',' || character == ')')
        .find(|part| !part.is_empty())?;
    Some(value.trim_matches('`').to_string())
}

pub(crate) fn with_postgresql_identity_marker(
    type_name: &str,
    column_default: Option<&str>,
    is_identity: &str,
) -> String {
    let default_uses_sequence = column_default
        .map(|value| value.to_ascii_lowercase().contains("nextval("))
        .unwrap_or(false);
    if is_identity.eq_ignore_ascii_case("YES") || default_uses_sequence {
        format!("{type_name} identity")
    } else {
        type_name.to_string()
    }
}

pub(crate) fn normalize_postgresql_default(column_default: Option<&str>, is_identity: &str) -> Option<String> {
    if is_identity.eq_ignore_ascii_case("YES") {
        return None;
    }
    let default_value = column_default?.trim();
    if default_value.to_ascii_lowercase().contains("nextval(") {
        return None;
    }
    Some(strip_postgresql_type_cast(default_value).to_string())
}

pub(crate) fn is_auto_increment_type(type_name: &str) -> bool {
    let type_name = type_name.to_ascii_lowercase();
    type_name.contains("auto_increment")
        || type_name.contains(" identity")
        || type_name == "serial"
        || type_name == "bigserial"
}

pub(crate) fn strip_generation_marker(type_name: &str) -> String {
    let mut cleaned = type_name.to_string();
    for marker in [" auto_increment", " identity"] {
        cleaned = cleaned.replace(marker, "");
    }
    cleaned
}

pub(crate) fn quote_ident(engine: &str, ident: &str) -> String {
    if engine == "postgresql" {
        format!("\"{}\"", ident.replace('"', "\"\""))
    } else {
        format!("`{}`", ident.replace('`', "``"))
    }
}

fn quote_column_ref(engine: &str, table: &str, column: &str) -> String {
    format!(
        "{}.{}",
        quote_ident(engine, table),
        quote_ident(engine, column)
    )
}

pub(crate) fn drop_table_sql(engine: &str, table: &str) -> String {
    format!("DROP TABLE IF EXISTS {}", quote_ident(engine, table))
}

pub fn map_type(source: &str, target: &str, type_name: &str) -> String {
    let trimmed_type = type_name.trim();
    let source = source.to_ascii_lowercase();
    let target = target.to_ascii_lowercase();
    if source == target {
        return trimmed_type.to_string();
    }

    let ty = trimmed_type.to_ascii_lowercase();
    if source == "mysql" && target == "postgresql" {
        map_mysql_to_postgres(&ty)
    } else if source == "postgresql" && target == "mysql" {
        map_postgres_to_mysql(&ty)
    } else {
        trimmed_type.to_string()
    }
}

fn map_mysql_to_postgres(ty: &str) -> String {
    let stripped = strip_mysql_character_options(ty);
    let ty = stripped.trim();
    if ty.starts_with("bigint") {
        "BIGINT".to_string()
    } else if ty.starts_with("int") || ty.starts_with("integer") {
        "INTEGER".to_string()
    } else if ty.starts_with("tinyint(1)") || ty == "boolean" || ty == "bool" {
        "BOOLEAN".to_string()
    } else if ty.starts_with("varchar") {
        ty.to_ascii_uppercase()
    } else if ty == "date" {
        "DATE".to_string()
    } else if ty.starts_with("datetime") {
        "TIMESTAMP".to_string()
    } else if ty.starts_with("timestamp") {
        "TIMESTAMPTZ".to_string()
    } else if ty.starts_with("time") {
        "TIME".to_string()
    } else if ty.starts_with("json") {
        "JSONB".to_string()
    } else if ty.contains("blob") || ty.contains("binary") {
        "BYTEA".to_string()
    } else if ty.starts_with("decimal") {
        ty.to_ascii_uppercase()
    } else {
        "TEXT".to_string()
    }
}

fn strip_mysql_character_options(type_name: &str) -> String {
    let mut kept = Vec::new();
    let mut tokens = type_name.split_whitespace().peekable();
    while let Some(token) = tokens.next() {
        if token.eq_ignore_ascii_case("character")
            && tokens
                .peek()
                .is_some_and(|next| next.eq_ignore_ascii_case("set"))
        {
            tokens.next();
            tokens.next();
            continue;
        }
        if token.eq_ignore_ascii_case("charset") || token.eq_ignore_ascii_case("collate") {
            tokens.next();
            continue;
        }
        kept.push(token);
    }
    kept.join(" ")
}

fn map_postgres_to_mysql(ty: &str) -> String {
    if ty == "bigint" || ty == "bigserial" {
        "BIGINT".to_string()
    } else if ty == "integer" || ty == "int" || ty == "serial" {
        "INT".to_string()
    } else if ty == "boolean" || ty == "bool" {
        "TINYINT(1)".to_string()
    } else if ty.starts_with("character varying") {
        ty.replacen("character varying", "VARCHAR", 1)
            .to_ascii_uppercase()
    } else if ty.starts_with("varchar") {
        ty.to_ascii_uppercase()
    } else if ty == "date" {
        "DATE".to_string()
    } else if ty == "time" || ty.starts_with("time ") || ty.starts_with("time(") {
        "TIME".to_string()
    } else if ty.starts_with("timestamp") {
        "DATETIME".to_string()
    } else if ty == "jsonb" || ty == "json" {
        "JSON".to_string()
    } else if ty == "bytea" {
        "LONGBLOB".to_string()
    } else if ty.starts_with("numeric") || ty.starts_with("decimal") {
        ty.replacen("numeric", "DECIMAL", 1).to_ascii_uppercase()
    } else {
        "TEXT".to_string()
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::json;
    
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::{RecordingAdapter, schema, single_pk_table_with_collation};

    #[test]
    fn post_load_ddl_policy_applies_for_recreated_targets_only() {
        assert!(should_apply_post_load_ddl("replace"));
        assert!(should_apply_post_load_ddl("recreate"));
        assert!(!should_apply_post_load_ddl("merge"));
    }

    #[test]
    fn merge_import_does_not_claim_post_load_ddl_phase() {
        assert_eq!(
            post_load_ddl_skip_message("merge"),
            "skipping post-load DDL for merge import; existing objects must already match"
        );
    }

    #[test]
    fn maps_mysql_types_to_postgres() {
        assert_eq!(map_type("mysql", "postgresql", "int(11)"), "INTEGER");
        assert_eq!(map_type("mysql", "postgresql", "tinyint(1)"), "BOOLEAN");
        assert_eq!(map_type("mysql", "postgresql", "json"), "JSONB");
        assert_eq!(map_type("mysql", "postgresql", "datetime"), "TIMESTAMP");
    }

    #[test]
    fn maps_postgres_types_to_mysql() {
        assert_eq!(map_type("postgresql", "mysql", "integer"), "INT");
        assert_eq!(map_type("postgresql", "mysql", "boolean"), "TINYINT(1)");
        assert_eq!(map_type("postgresql", "mysql", "jsonb"), "JSON");
        assert_eq!(
            map_type("postgresql", "mysql", "timestamp with time zone"),
            "DATETIME"
        );
    }

    #[test]
    fn preserves_native_type_literals_for_same_engine_imports() {
        assert_eq!(
            map_type("mysql", "mysql", " enum('HIGH','MEDIUM','LOW') "),
            "enum('HIGH','MEDIUM','LOW')"
        );
        assert_eq!(
            map_type("postgresql", "postgresql", "character varying(16)"),
            "character varying(16)"
        );
    }

    #[test]
    fn mysql_to_postgres_type_mapping_strips_mysql_character_options() {
        assert_eq!(
            map_type(
                "mysql",
                "postgresql",
                "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci",
            ),
            "VARCHAR(45)"
        );
    }

    #[test]
    fn mysql_column_inspection_captures_character_metadata() {
        let sql = inspect_columns_sql("mysql");

        assert!(sql.contains("CHARACTER_SET_NAME"));
        assert!(sql.contains("COLLATION_NAME"));
    }

    #[test]
    fn mysql_to_mysql_ddl_preserves_enum_literal_case() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "df_evaluations_norm".to_string(),
                columns: vec![NormalizedColumn {
                    name: "importance".to_string(),
                    type_name: "enum('HIGH','MEDIUM','LOW')".to_string(),
                    default_value: Some("MEDIUM".to_string()),
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };

        assert_eq!(
            generate_schema_ddl(&schema, "mysql", "mysql").unwrap()[0],
            "CREATE TABLE `df_evaluations_norm` (\n  `importance` enum('HIGH','MEDIUM','LOW') DEFAULT 'MEDIUM' NOT NULL\n);"
        );
    }

    #[test]
    fn generates_create_table_ddl() {
        let ddl = generate_schema_ddl(&schema(), "mysql", "postgresql").unwrap();
        assert_eq!(ddl.len(), 1);
        assert!(ddl[0].contains("CREATE TABLE \"users\""));
        assert!(ddl[0].contains("\"id\" INTEGER NOT NULL"));
        assert!(ddl[0].contains("PRIMARY KEY (\"id\")"));
    }

    #[test]
    fn generates_post_data_index_and_fk_ddl() {
        let schema = NormalizedSchema {
            tables: vec![
                NormalizedTable {
                    name: "users".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                },
                NormalizedTable {
                    name: "orders".to_string(),
                    columns: vec![
                        NormalizedColumn {
                            name: "id".to_string(),
                            type_name: "int".to_string(),
                            default_value: None,
                            nullable: false,
                            primary_key: true,
                            unique: false,
                        },
                        NormalizedColumn {
                            name: "user_id".to_string(),
                            type_name: "int".to_string(),
                            default_value: None,
                            nullable: false,
                            primary_key: false,
                            unique: false,
                        },
                    ],
                    indexes: vec![NormalizedIndex {
                        name: "idx_orders_user_id".to_string(),
                        columns: vec!["user_id".to_string()],
                        unique: false,
                    }],
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "fk_orders_users".to_string(),
                        columns: vec!["user_id".to_string()],
                        referenced_table: "users".to_string(),
                        referenced_columns: vec!["id".to_string()],
                    }],
                    table_collation: None,
                },
            ],
        };

        let ddl = generate_post_data_ddl(&schema, "postgresql");

        assert_eq!(
            ddl[0],
            "CREATE INDEX \"idx_orders_user_id\" ON \"orders\" (\"user_id\");"
        );
        assert_eq!(
            ddl[1],
            "ALTER TABLE \"orders\" ADD CONSTRAINT \"fk_orders_users\" FOREIGN KEY (\"user_id\") REFERENCES \"users\" (\"id\");"
        );
    }

    #[test]
    fn post_data_ddl_applies_all_indexes_before_any_foreign_keys() {
        let schema = NormalizedSchema {
            tables: vec![
                NormalizedTable {
                    name: "cr_industry_map".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "brief_slug".to_string(),
                        type_name: "varchar(64)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "cr_industry_map_ibfk_1".to_string(),
                        columns: vec!["brief_slug".to_string()],
                        referenced_table: "cr_industry_briefs".to_string(),
                        referenced_columns: vec!["slug".to_string()],
                    }],
                    table_collation: None,
                },
                NormalizedTable {
                    name: "cr_industry_briefs".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "slug".to_string(),
                        type_name: "varchar(64)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: true,
                    }],
                    indexes: vec![NormalizedIndex {
                        name: "ux_cr_industry_briefs_slug".to_string(),
                        columns: vec!["slug".to_string()],
                        unique: true,
                    }],
                    foreign_keys: Vec::new(),
                    table_collation: None,
                },
            ],
        };

        let ddl = generate_post_data_ddl(&schema, "mysql");
        let parent_unique_index = ddl
            .iter()
            .position(|sql| sql.contains("ux_cr_industry_briefs_slug"))
            .unwrap();
        let child_foreign_key = ddl
            .iter()
            .position(|sql| sql.contains("cr_industry_map_ibfk_1"))
            .unwrap();

        assert!(parent_unique_index < child_foreign_key);
    }

    #[test]
    fn post_load_ddl_applies_secondary_indexes_after_import_data() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "orders".to_string(),
                columns: vec![NormalizedColumn {
                    name: "user_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_orders_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    unique: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut adapter = RecordingAdapter::default();

        apply_post_load_ddl(&mut adapter, &schema, "mysql").unwrap();

        // MySQL post-load DDL은 foreign_key_checks=0으로 감싸 실행된다(고아 허용).
        assert_eq!(
            adapter.executed_sql,
            vec![
                "SET SESSION foreign_key_checks=0".to_string(),
                "CREATE INDEX `idx_orders_user_id` ON `orders` (`user_id`);".to_string(),
                "SET SESSION foreign_key_checks=1".to_string(),
            ]
        );
    }

    #[test]
    fn post_load_ddl_mysql_wraps_fk_ddl_with_checks_disabled() {
        // 부모/자식 + FK가 있는 스키마에서, FK ALTER가 foreign_key_checks=0 구간 안에서
        // 실행되는지 검증한다(소스에 고아가 있어도 1452 없이 FK 생성).
        let schema = NormalizedSchema {
            tables: vec![
                NormalizedTable {
                    name: "users".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                },
                NormalizedTable {
                    name: "is_read_comment".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "user_id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "is_read_comment_ibfk_1".to_string(),
                        columns: vec!["user_id".to_string()],
                        referenced_table: "users".to_string(),
                        referenced_columns: vec!["id".to_string()],
                    }],
                    table_collation: None,
                },
            ],
        };
        let mut adapter = RecordingAdapter::default();

        apply_post_load_ddl(&mut adapter, &schema, "mysql").unwrap();

        assert_eq!(
            adapter.executed_sql.first().map(String::as_str),
            Some("SET SESSION foreign_key_checks=0")
        );
        assert_eq!(
            adapter.executed_sql.last().map(String::as_str),
            Some("SET SESSION foreign_key_checks=1")
        );
        // FK ALTER가 두 SET 사이에 존재한다.
        let fk_idx = adapter
            .executed_sql
            .iter()
            .position(|sql| {
                sql.contains("ADD CONSTRAINT") && sql.contains("is_read_comment_ibfk_1")
            })
            .expect("FK ALTER present");
        assert!(fk_idx > 0 && fk_idx < adapter.executed_sql.len() - 1);
    }

    #[test]
    fn post_load_ddl_restores_fk_checks_on_ddl_error() {
        // post-load DDL 중간에 실패해도 foreign_key_checks=1 복원이 실행되고, 원 에러가 전파된다.
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "orders".to_string(),
                columns: vec![NormalizedColumn {
                    name: "user_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_orders_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    unique: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut adapter = RecordingAdapter {
            fail_sql_contains: Some("CREATE INDEX".to_string()),
            ..RecordingAdapter::default()
        };

        let err = apply_post_load_ddl(&mut adapter, &schema, "mysql").unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        // checks=0으로 열었고, 실패했어도 checks=1 복원이 마지막에 실행됐다.
        assert_eq!(
            adapter.executed_sql.first().map(String::as_str),
            Some("SET SESSION foreign_key_checks=0")
        );
        assert_eq!(
            adapter.executed_sql.last().map(String::as_str),
            Some("SET SESSION foreign_key_checks=1")
        );
    }

    #[test]
    fn post_load_ddl_postgres_does_not_toggle_fk_checks() {
        // PostgreSQL 타겟에는 MySQL 전용 foreign_key_checks SET 문이 나오지 않는다.
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "orders".to_string(),
                columns: vec![NormalizedColumn {
                    name: "user_id".to_string(),
                    type_name: "integer".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_orders_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    unique: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut adapter = RecordingAdapter::default();

        apply_post_load_ddl(&mut adapter, &schema, "postgresql").unwrap();

        assert!(adapter
            .executed_sql
            .iter()
            .all(|sql| !sql.contains("foreign_key_checks")));
    }

    #[test]
    fn post_load_ddl_rejects_incompatible_fk_collation_before_sql_execution() {
        let schema = NormalizedSchema {
            tables: vec![
                NormalizedTable {
                    name: "audit_category".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                            .to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                },
                NormalizedTable {
                    name: "df_evaluation_results".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "audit_category_code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                            .to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    }],
                    indexes: vec![NormalizedIndex {
                        name: "idx_df_evaluation_results_audit_category_code".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        unique: false,
                    }],
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "df_evaluation_results_ibfk_3".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        referenced_table: "audit_category".to_string(),
                        referenced_columns: vec!["code".to_string()],
                    }],
                    table_collation: None,
                },
            ],
        };
        let mut adapter = RecordingAdapter::default();

        let err = apply_post_load_ddl(&mut adapter, &schema, "mysql").unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(adapter.executed_sql.is_empty());
    }

    #[test]
    fn post_load_ddl_errors_include_classification_and_sql_context() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "login_attempts".to_string(),
                columns: vec![NormalizedColumn {
                    name: "user_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_login_attempts_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    unique: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut adapter = RecordingAdapter {
            fail_sql_contains: Some("idx_login_attempts_user_id".to_string()),
            ..RecordingAdapter::default()
        };

        let err = apply_post_load_ddl(&mut adapter, &schema, "mysql").unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("CREATE INDEX `idx_login_attempts_user_id`"));
        assert!(err.contains("ERROR 1114"));
    }

    #[test]
    fn post_load_ddl_mysql_table_full_error_includes_storage_guidance() {
        let err = post_load_ddl_error(
            "ALTER TABLE `login_attempts` ADD INDEX `idx_user_id` (`user_id`)",
            "mysql SQL execution error: ERROR 1114 (HY000): The table '#sql-1cbc_17b' is full",
        );

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("ERROR 1114"));
        assert!(err.contains("target MySQL storage or temporary table space is full"));
        assert!(err.contains("tmpdir"));
        assert!(err.contains("innodb_temp_data_file_path"));
    }

    #[test]
    fn auto_increment_columns_generate_identity_or_auto_increment_ddl() {
        let mysql_schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "users".to_string(),
                columns: vec![NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "int(11) auto_increment".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let postgresql_schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "users".to_string(),
                columns: vec![NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "integer identity".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };

        assert_eq!(
            generate_schema_ddl(&mysql_schema, "mysql", "postgresql").unwrap()[0],
            "CREATE TABLE \"users\" (\n  \"id\" INTEGER GENERATED BY DEFAULT AS IDENTITY NOT NULL,\n  PRIMARY KEY (\"id\")\n);"
        );
        assert_eq!(
            generate_schema_ddl(&postgresql_schema, "postgresql", "mysql").unwrap()[0],
            "CREATE TABLE `users` (\n  `id` INT AUTO_INCREMENT NOT NULL,\n  PRIMARY KEY (`id`)\n);"
        );
        assert_eq!(
            generate_sequence_reset_ddl(&mysql_schema, "postgresql")[0],
            "SELECT setval(pg_get_serial_sequence('users', 'id'), COALESCE((SELECT MAX(\"id\") FROM \"users\"), 0) + 1, false);"
        );
    }

    #[test]
    fn column_defaults_are_mapped_between_engines() {
        let mysql_schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "users".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "status".to_string(),
                        type_name: "varchar(16)".to_string(),
                        default_value: Some("new".to_string()),
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "enabled".to_string(),
                        type_name: "tinyint(1)".to_string(),
                        default_value: Some("1".to_string()),
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let postgresql_schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "users".to_string(),
                columns: vec![NormalizedColumn {
                    name: "enabled".to_string(),
                    type_name: "boolean".to_string(),
                    default_value: Some("true".to_string()),
                    nullable: false,
                    primary_key: false,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };

        assert_eq!(
            generate_schema_ddl(&mysql_schema, "mysql", "postgresql").unwrap()[0],
            "CREATE TABLE \"users\" (\n  \"status\" VARCHAR(16) DEFAULT 'new' NOT NULL,\n  \"enabled\" BOOLEAN DEFAULT TRUE NOT NULL\n);"
        );
        assert_eq!(
            generate_schema_ddl(&postgresql_schema, "postgresql", "mysql").unwrap()[0],
            "CREATE TABLE `users` (\n  `enabled` TINYINT(1) DEFAULT 1 NOT NULL\n);"
        );
    }

    #[test]
    fn postgresql_text_output_removes_mysql_nul_bytes() {
        assert_eq!(
            copy_csv_field_for_column("postgresql", "varchar(255)", &json!("ab\0cd")),
            "\"abcd\""
        );
        assert_eq!(
            sql_literal_for_column("postgresql", "text", &json!("ab\0cd")),
            "'abcd'"
        );
    }

    #[test]
    fn sql_builder_quotes_and_uses_engine_placeholders() {
        let columns = vec!["id".to_string(), "name".to_string()];
        assert_eq!(
            count_sql("postgresql", "users"),
            "SELECT COUNT(*) AS row_count FROM \"users\""
        );
        assert_eq!(
            insert_sql("postgresql", "users", &columns),
            "INSERT INTO \"users\" (\"id\", \"name\") VALUES ($1, $2)"
        );
        assert_eq!(
            insert_sql("mysql", "users", &columns),
            "INSERT INTO `users` (`id`, `name`) VALUES (?, ?)"
        );
    }

    #[test]
    fn text_range_sql_filters_by_numeric_primary_key() {
        let table = schema().tables[0].clone();

        assert_eq!(
            select_chunk_text_range_sql("mysql", &table, "id", 101, 200),
            "SELECT `id`, `name` FROM `users` WHERE `users`.`id` >= 101 AND `users`.`id` <= 200 ORDER BY `users`.`id`"
        );
    }

    #[test]
    fn binary_columns_are_selected_as_hex_and_inserted_as_binary_literals() {
        let table = NormalizedTable {
            name: "files".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
                NormalizedColumn {
                    name: "payload".to_string(),
                    type_name: "blob".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert_eq!(
            select_chunk_text_sql("mysql", &table, &["id".to_string()]),
            "SELECT `id`, HEX(`payload`) AS `payload` FROM `files` ORDER BY `id` LIMIT ? OFFSET ?"
        );
        assert_eq!(
            select_chunk_text_sql("postgresql", &table, &["id".to_string()]),
            "SELECT \"id\"::text AS \"id\", encode(\"payload\", 'hex') AS \"payload\" FROM \"files\" ORDER BY \"id\" LIMIT $1 OFFSET $2"
        );
        assert_eq!(
            insert_rows_literal_sql_for_table(
                "postgresql",
                &table,
                &[json!({"id": "1", "payload": "0001ff"})]
            ),
            "INSERT INTO \"files\" (\"id\", \"payload\") VALUES ('1', decode('0001ff', 'hex'))"
        );
    }

    #[test]
    fn temporal_types_are_mapped_between_mysql_and_postgresql() {
        assert_eq!(map_type("mysql", "postgresql", "date"), "DATE");
        assert_eq!(map_type("mysql", "postgresql", "time"), "TIME");
        assert_eq!(map_type("mysql", "postgresql", "datetime"), "TIMESTAMP");
        assert_eq!(map_type("mysql", "postgresql", "timestamp"), "TIMESTAMPTZ");
        assert_eq!(map_type("postgresql", "mysql", "date"), "DATE");
        assert_eq!(
            map_type("postgresql", "mysql", "time without time zone"),
            "TIME"
        );
        assert_eq!(
            map_type("postgresql", "mysql", "timestamp without time zone"),
            "DATETIME"
        );
        assert_eq!(
            map_type("postgresql", "mysql", "timestamp with time zone"),
            "DATETIME"
        );
    }

    #[test]
    fn literal_insert_sql_escapes_values() {
        let columns = vec!["id".to_string(), "name".to_string()];
        let sql = insert_rows_literal_sql(
            "postgresql",
            "users",
            &columns,
            &[json!({"id": 1, "name": "O'Reilly"})],
        );

        assert_eq!(
            sql,
            "INSERT INTO \"users\" (\"id\", \"name\") VALUES (1, 'O''Reilly')"
        );
    }

    #[test]
    fn mysql_json_literal_insert_preserves_json_escape_backslashes() {
        let table = NormalizedTable {
            name: "ai_phase1_cache".to_string(),
            columns: vec![NormalizedColumn {
                name: "result_json".to_string(),
                type_name: "json".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };
        let json_text = r#"{"facts":[{"content":"문서 제목은 \"工伤管理表\"로 표기되어 있다."}]}"#;

        let sql = insert_rows_literal_sql_for_table(
            "mysql",
            &table,
            &[json!({"result_json": json_text})],
        );

        assert_eq!(
            sql,
            r#"INSERT INTO `ai_phase1_cache` (`result_json`) VALUES (_utf8mb4'{"facts":[{"content":"문서 제목은 \\"工伤管理表\\"로 표기되어 있다."}]}')"#
        );
    }

    #[test]
    fn mysql_json_literal_uses_utf8mb4_introducer_for_unicode_json_text() {
        let table = NormalizedTable {
            name: "ai_phase1_cache".to_string(),
            columns: vec![NormalizedColumn {
                name: "result_json".to_string(),
                type_name: "json".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };
        let json_text = r#"{"facts":[{"content":"문서 제목은 \"工伤管理表\"로 표기되어 있다."}]}"#;

        let sql = insert_rows_literal_sql_for_table(
            "mysql",
            &table,
            &[json!({"result_json": json_text})],
        );

        assert_eq!(
            sql,
            r#"INSERT INTO `ai_phase1_cache` (`result_json`) VALUES (_utf8mb4'{"facts":[{"content":"문서 제목은 \\"工伤管理表\\"로 표기되어 있다."}]}')"#
        );
    }

    #[test]
    fn table_literal_insert_converts_boolean_text_between_engines() {
        let pg_schema = NormalizedTable {
            name: "flags".to_string(),
            columns: vec![NormalizedColumn {
                name: "enabled".to_string(),
                type_name: "boolean".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };
        let mysql_schema = NormalizedTable {
            name: "flags".to_string(),
            columns: vec![NormalizedColumn {
                name: "enabled".to_string(),
                type_name: "tinyint(1)".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
        };

        assert_eq!(
            insert_rows_literal_sql_for_table(
                "mysql",
                &pg_schema,
                &[json!({"enabled": "true"}), json!({"enabled": "false"})]
            ),
            "INSERT INTO `flags` (`enabled`) VALUES (1), (0)"
        );
        assert_eq!(
            insert_rows_literal_sql_for_table(
                "postgresql",
                &mysql_schema,
                &[json!({"enabled": "1"}), json!({"enabled": "0"})]
            ),
            "INSERT INTO \"flags\" (\"enabled\") VALUES (TRUE), (FALSE)"
        );
    }

    #[test]
    fn inspect_sql_targets_information_schema() {
        assert!(inspect_tables_sql("mysql").contains("information_schema.tables"));
        assert!(inspect_columns_sql("postgresql").contains("information_schema.columns"));
        assert!(inspect_columns_sql("postgresql").contains("character_maximum_length"));
        assert!(inspect_keys_sql("mysql").contains("KEY_COLUMN_USAGE"));
        assert!(inspect_foreign_keys_sql("postgresql").contains("FOREIGN KEY"));
        assert!(inspect_indexes_sql("postgresql").contains("pg_index"));
    }

    #[test]
    fn inspect_tables_sql_mysql_reads_table_collation() {
        // 같은 엔진 dump에서 테이블 기본 collation을 재현하려면 조사 쿼리가 TABLE_COLLATION을 읽어야 한다.
        assert!(inspect_tables_sql("mysql").contains("TABLE_COLLATION"));
        // PostgreSQL은 테이블 레벨 collation 개념이 없으므로 컬럼을 추가하지 않는다.
        assert!(!inspect_tables_sql("postgresql")
            .to_ascii_uppercase()
            .contains("TABLE_COLLATION"));
    }

    #[test]
    fn generate_table_ddl_emits_table_collation_same_engine_mysql() {
        let table = single_pk_table_with_collation(Some("utf8mb4_unicode_ci"));
        let ddl = generate_table_ddl(&table, "mysql", "mysql").expect("ddl");
        assert!(
            ddl.trim_end().ends_with(") COLLATE=utf8mb4_unicode_ci;"),
            "same-engine MySQL DDL should carry the table collation suffix: {ddl}"
        );
    }

    #[test]
    fn generate_table_ddl_omits_table_collation_cross_engine() {
        let table = single_pk_table_with_collation(Some("utf8mb4_unicode_ci"));
        let ddl = generate_table_ddl(&table, "mysql", "postgresql").expect("ddl");
        assert!(
            !ddl.to_ascii_uppercase().contains("COLLATE"),
            "cross-engine DDL must not emit a table collation: {ddl}"
        );
    }

    #[test]
    fn generate_table_ddl_omits_table_collation_for_same_engine_postgres() {
        // PostgreSQL→PostgreSQL도 테이블 레벨 COLLATE를 붙이지 않는다(MySQL 전용 표현).
        let table = single_pk_table_with_collation(Some("utf8mb4_unicode_ci"));
        let ddl = generate_table_ddl(&table, "postgresql", "postgresql").expect("ddl");
        assert!(!ddl.to_ascii_uppercase().contains("COLLATE"), "{ddl}");
    }

    #[test]
    fn generate_table_ddl_rejects_injection_via_table_collation() {
        // 변조된 매니페스트가 collation 자리에 SQL을 주입하면 fail-closed로 DDL 생성을 거부한다.
        for payload in [
            "utf8mb4_unicode_ci AS SELECT id, email FROM users",
            "utf8mb4_bin ENGINE=MyISAM",
            "foo; DROP TABLE users",
            "utf8mb4_bin)",
            "utf8mb4 bin",
            "utf8mb4_bin,ROW_FORMAT=DYNAMIC",
            "utf8mb4_bin`",
        ] {
            let table = single_pk_table_with_collation(Some(payload));
            assert!(
                generate_table_ddl(&table, "mysql", "mysql").is_none(),
                "malicious collation must fail-closed (no DDL): {payload:?}"
            );
        }
    }

    #[test]
    fn generate_table_ddl_accepts_real_mysql8_collation() {
        let table = single_pk_table_with_collation(Some("utf8mb4_0900_ai_ci"));
        let ddl = generate_table_ddl(&table, "mysql", "mysql").expect("valid collation");
        assert!(
            ddl.trim_end().ends_with(") COLLATE=utf8mb4_0900_ai_ci;"),
            "{ddl}"
        );
    }

    #[test]
    fn is_valid_mysql_collation_ident_accepts_names_and_rejects_injection() {
        assert!(is_valid_mysql_collation_ident("utf8mb4_0900_ai_ci"));
        assert!(is_valid_mysql_collation_ident("latin1_swedish_ci"));
        assert!(is_valid_mysql_collation_ident(&"a".repeat(64)));
        assert!(!is_valid_mysql_collation_ident(""));
        assert!(!is_valid_mysql_collation_ident("has space"));
        assert!(!is_valid_mysql_collation_ident("semi;colon"));
        assert!(!is_valid_mysql_collation_ident("paren)"));
        assert!(!is_valid_mysql_collation_ident("eq=sign"));
        assert!(!is_valid_mysql_collation_ident(&"a".repeat(65)));
    }

    #[test]
    fn is_safe_column_type_accepts_normal_types() {
        for ok in [
            "int",
            "bigint unsigned",
            "varchar(255)",
            "decimal(10,2)",
            "tinyint(1)",
            "enum('a','b','c')",
            "set('x','y')",
            "timestamp",
            "datetime(6)",
            "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin",
            "int unsigned zerofill",
            "enum('a,b','c''d')",
            "char(1) charset ascii",
            "timestamp with time zone",
            "timestamp without time zone",
            "time with time zone",
            "double precision",
            "character varying(255)",
            "bit varying(8)",
        ] {
            assert!(is_safe_column_type(ok), "should accept: {ok}");
        }
    }

    #[test]
    fn is_safe_column_type_rejects_injection() {
        for bad in [
            "int) AS (SELECT user FROM mysql.user",
            "int, evil int",
            "int; DROP TABLE users",
            "varchar(45) COLLATE utf8mb4_bin; --",
            "int) ENGINE=MyISAM",
            "enum('a') , x int",
            "int /* c */",
            "varchar(45) CHARACTER SET utf8mb4, y int",
            "",
            "int(",
            "'quoted'",
            "int)",
            "enum('unterminated",
            "enum('a\\', evil int) -- ')",
            "int with evil",
            "timestamp with evil zone",
            "enum('a\\', ') , injected_col INT, -- ')",
            "enum('x\\y')",
        ] {
            assert!(!is_safe_column_type(bad), "should reject: {bad}");
        }
    }

    #[test]
    fn generate_table_ddl_rejects_injection_via_type_name() {
        let mut table = single_pk_table_with_collation(None);
        table.columns.push(NormalizedColumn {
            name: "c".to_string(),
            type_name: "int) AS (SELECT user, authentication_string FROM mysql.user".to_string(),
            default_value: None,
            nullable: true,
            primary_key: false,
            unique: false,
        });
        assert!(
            generate_table_ddl(&table, "mysql", "mysql").is_none(),
            "malicious type_name must fail-closed (no DDL)"
        );
    }

    #[test]
    fn generate_table_ddl_rejects_cross_engine_type_injection() {
        // cross-engine에서도 map_type이 varchar/decimal을 원문 대문자화만 해 통과시키므로,
        // 변환 후(mapped_type) 검증이 컬럼 정의 탈출을 막아야 한다.
        let mut table = single_pk_table_with_collation(None);
        table.columns.push(NormalizedColumn {
            name: "c".to_string(),
            type_name: "varchar(45), evil int".to_string(),
            default_value: None,
            nullable: true,
            primary_key: false,
            unique: false,
        });
        assert!(
            generate_table_ddl(&table, "postgresql", "mysql").is_none(),
            "cross-engine (pg->mysql) varchar injection must fail-closed"
        );
        assert!(
            generate_table_ddl(&table, "mysql", "postgresql").is_none(),
            "cross-engine (mysql->pg) varchar injection must fail-closed"
        );
    }

    #[test]
    fn map_default_literal_neutralizes_quoted_injection_and_preserves_normal() {
        // 컬럼 정의 주입 시도는 하나의 문자열 리터럴로 감싸져 바깥으로 토큰이 새지 않아야 한다.
        let out = map_default_literal("mysql", "'x', evil int", "varchar(10)");
        assert!(out.starts_with('\'') && out.ends_with('\''), "{out}");
        // well-formed 문자열 리터럴이면 작은따옴표 개수가 짝수(내부는 모두 '' 이스케이프).
        assert_eq!(out.matches('\'').count() % 2, 0, "unbalanced quotes: {out}");
        // 정상 값 보존
        assert_eq!(
            map_default_literal("mysql", "'MEDIUM'", "enum('x')"),
            "'MEDIUM'"
        );
        assert_eq!(
            map_default_literal("mysql", "MEDIUM", "varchar(10)"),
            "'MEDIUM'"
        );
        assert_eq!(
            map_default_literal("mysql", "'a''b'", "varchar(10)"),
            "'a''b'"
        );
        // 정상 bit 리터럴은 그대로 통과
        assert_eq!(map_default_literal("mysql", "b'0101'", "bit(4)"), "b'0101'");
        // 변조 bit 리터럴(닫히지 않음)은 문자열로 중화
        let bad_bit = map_default_literal("mysql", "b'0') AS (SELECT 1", "bit(1)");
        assert!(
            bad_bit.starts_with('\'') && bad_bit.ends_with('\''),
            "{bad_bit}"
        );
    }

    #[test]
    fn generate_schema_ddl_errors_on_invalid_table_collation() {
        // 유효하지 않은(변조된) table_collation은 조용히 누락되지 않고 에러로 전파되어야 한다.
        let schema = NormalizedSchema {
            tables: vec![single_pk_table_with_collation(Some(
                "utf8mb4_bin AS SELECT 1",
            ))],
        };
        assert!(generate_schema_ddl(&schema, "mysql", "mysql").is_err());
    }

    #[test]
    fn generate_table_ddl_omits_collation_when_absent() {
        let table = single_pk_table_with_collation(None);
        let ddl = generate_table_ddl(&table, "mysql", "mysql").expect("ddl");
        assert!(
            !ddl.contains("COLLATE="),
            "no collation info means no COLLATE clause: {ddl}"
        );
    }

    #[test]
    fn group_foreign_keys_preserves_composite_column_order() {
        let keys = group_foreign_keys(vec![
            (
                "fk_order_items_order".to_string(),
                "tenant_id".to_string(),
                "orders".to_string(),
                "tenant_id".to_string(),
            ),
            (
                "fk_order_items_order".to_string(),
                "order_id".to_string(),
                "orders".to_string(),
                "id".to_string(),
            ),
        ]);

        assert_eq!(keys.len(), 1);
        assert_eq!(keys[0].columns, vec!["tenant_id", "order_id"]);
        assert_eq!(keys[0].referenced_columns, vec!["tenant_id", "id"]);
    }

    #[test]
    fn group_indexes_preserves_column_order_and_unique_flag() {
        let indexes = group_indexes(vec![
            (
                "idx_users_name_email".to_string(),
                "name".to_string(),
                false,
            ),
            (
                "idx_users_name_email".to_string(),
                "email".to_string(),
                false,
            ),
            ("ux_users_slug".to_string(), "slug".to_string(), true),
        ]);

        assert_eq!(indexes.len(), 2);
        assert_eq!(indexes[0].columns, vec!["name", "email"]);
        assert!(!indexes[0].unique);
        assert!(indexes[1].unique);
    }

    #[test]
    fn postgresql_column_type_preserves_length_and_precision() {
        assert_eq!(
            postgresql_column_type("character varying", Some(64), None, None),
            "varchar(64)"
        );
        assert_eq!(
            postgresql_column_type("numeric", None, Some(10), Some(2)),
            "numeric(10,2)"
        );
        assert_eq!(
            postgresql_column_type("boolean", None, None, None),
            "boolean"
        );
    }

    #[test]
    fn inspect_result_propagates_unsupported_objects_for_preflight() {
        let events = handle_request(Request {
            command: "inspect".to_string(),
            request_id: Some("req-1".to_string()),
            payload: json!({
                "schema": {"tables": []},
                "unsupported_objects": ["view:active_users", "trigger:users_audit"]
            }),
        });
        let result = events
            .iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(
            result["unsupported_objects"],
            json!(["view:active_users", "trigger:users_audit"])
        );
    }

    #[test]
    fn preflight_reports_unsupported_objects_as_non_blocking_warnings() {
        let issues = preflight_issues(&json!({
            "source_engine": "mysql",
            "target_engine": "postgresql",
            "schema": {"tables": []},
            "unsupported_objects": ["view:active_users"]
        }));

        let unsupported = issues
            .iter()
            .find(|issue| issue.location == "view:active_users")
            .unwrap();
        assert_eq!(unsupported.severity, "warning");
        assert!(!unsupported.blocking);
        assert!(issues.iter().any(|issue| issue.location == "users_grants"));
    }

    #[test]
    fn apply_key_flags_marks_primary_and_unique_columns() {
        let mut columns = schema().tables[0].columns.clone();
        for column in &mut columns {
            column.primary_key = false;
            column.unique = false;
        }

        let columns = apply_key_flags(
            columns,
            &[
                ("id".to_string(), "PRIMARY KEY".to_string()),
                ("name".to_string(), "UNIQUE".to_string()),
            ],
        );

        assert!(columns
            .iter()
            .any(|column| column.name == "id" && column.primary_key));
        assert!(columns
            .iter()
            .any(|column| column.name == "name" && column.unique));
    }
}
