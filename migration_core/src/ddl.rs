use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Write;

use crate::*;

/// MySQL collation/식별자 이름의 최대 길이. MySQL 식별자는 64자를 넘지 못하므로,
/// 그보다 긴 collation 문자열은 변조된 값으로 보고 fail-closed로 거부한다.
const MYSQL_IDENTIFIER_MAX_LEN: usize = 64;

/// 검증 대상 컬럼 타입 문자열의 최대 길이. 정상 타입 선언은 이보다 훨씬 짧으므로,
/// 초과하는 값은 주입 시도로 보고 파싱 전에 fail-closed로 거부한다.
const MAX_COLUMN_TYPE_LEN: usize = 512;

fn schema_string_literal(engine: &str, value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('\'', "''");
    format!("{}'{escaped}'", if engine == "postgresql" { "E" } else { "" })
}

pub(crate) fn supported_mysql_default_expression(value: &str) -> bool {
    let value=value.trim().to_ascii_uppercase();
    matches!(value.as_str(), "UUID()" | "(UUID())" | "NOW()") || is_safe_temporal_default_expression(&value)
}

/// Keep a CHECK inside its single expression boundary. The target server probe
/// validates expression syntax/functions; this rejects statement/DDL escapes.
pub(crate) fn is_safe_check_expression(expression: &str) -> bool {
    let bytes=expression.as_bytes(); let mut i=0; let mut depth=0_u32;
    if expression.trim().is_empty() { return false; }
    while i<bytes.len() {
        match bytes[i] {
            b'\'' | b'"' | b'`' => {
                let quote=bytes[i]; i+=1; let mut closed=false;
                while i<bytes.len() {
                    // ANSI_QUOTES changes double quotes to identifiers. Reject
                    // the ambiguous escape form rather than disagree with it.
                    if bytes[i]==b'\\' && quote==b'"' { return false; }
                    if bytes[i]==b'\\' && quote!=b'`' { i+=2; }
                    else if bytes[i]==quote {
                        i+=1;
                        if bytes.get(i)==Some(&quote) { i+=1; } else { closed=true; break; }
                    } else { i+=1; }
                }
                if !closed { return false; }
            }
            b'(' => { depth+=1; i+=1; }
            b')' => { if depth==0 { return false; } depth-=1; i+=1; }
            b';' | b'#' | b'@' | 0 => return false,
            b'-' if bytes.get(i+1)==Some(&b'-') => return false,
            b'/' if bytes.get(i+1)==Some(&b'*') => return false,
            b',' if depth==0 => return false,
            byte if byte.is_ascii_alphabetic() || byte==b'_' => {
                let start=i; i+=1;
                while i<bytes.len() && (bytes[i].is_ascii_alphanumeric() || matches!(bytes[i],b'_'|b'$') || bytes[i]>=128) { i+=1; }
                if matches!(expression[start..i].to_ascii_uppercase().as_str(), "SELECT"|"INSERT"|"UPDATE"|"DELETE"|"DROP"|"ALTER"|"CREATE"|"CONSTRAINT"|"COMMENT"|"REFERENCES"|"UNION"|"RETURNING"|"INTO"|"OUTFILE"|"DUMPFILE") { return false; }
            }
            _ => i+=1,
        }
    }
    depth==0
}

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

pub(crate) fn validate_target_foreign_key_actions(table: &NormalizedTable, target: &str) -> Result<(), String> {
    if target != "mysql" {
        if !table.checks.is_empty() { return Err(format!("cannot translate MySQL CHECK expressions for table {} to {target}", table.name)); }
        if table.indexes.iter().any(|index| index.visible == Some(false)) { return Err(format!("cannot preserve invisible indexes for table {} on {target}",table.name)); }
        for column in &table.columns {
            if column.default_is_expression && column.default_value.as_deref().is_some_and(|value| value.trim().eq_ignore_ascii_case("uuid()") || value.trim().eq_ignore_ascii_case("(uuid())")) {
                return Err(format!("cannot translate MySQL UUID() expression default for {}.{} to {target}",table.name,column.name));
            }
        }
    }
    for check in &table.checks {
        if !is_safe_check_expression(&check.expression) { return Err(format!("unsafe CHECK expression in table {} constraint {}",table.name,check.name)); }
    }
    for column in &table.columns {
        if column.default_is_expression && column.default_value.as_deref().is_some_and(|value| !supported_mysql_default_expression(value)) {
            return Err(format!("unsupported expression default for {}.{}",table.name,column.name));
        }
    }
    if target.eq_ignore_ascii_case("mysql") {
        for fk in &table.foreign_keys {
            if [fk.on_delete.as_ref(), fk.on_update.as_ref()].into_iter().flatten()
                .any(|action| action.as_sql() == "SET DEFAULT")
            {
                return Err(format!("MySQL does not support SET DEFAULT foreign key actions: table {} constraint {}", table.name, fk.name));
            }
        }
    }
    Ok(())
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
            validate_target_foreign_key_actions(table, target)?;
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
            // MySQL must expose referenced unique keys at CREATE TABLE time,
            // even while foreign_key_checks=0 and target-only children survive.
            if target == "mysql" && index.unique { continue; }
            let unique = if index.unique { "UNIQUE " } else if index.spatial && target == "mysql" { "SPATIAL " } else { "" };
            let columns = index
                .columns
                .iter()
                .enumerate()
                .map(|(i, column)| {
                    let ident = quote_ident(target, column);
                    // MySQL prefix 인덱스(col(255))는 prefix 길이를 보존한다. postgresql은 prefix
                    // 인덱스 개념이 없어 full 컬럼으로 둔다. 구 덤프(column_prefixes 없음)는
                    // get(i)=None으로 full 처리되어 기존 동작과 동일하다.
                    // MySQL reports SUB_PART 32 for SPATIAL keys, but they take no prefix.
                    match index.column_prefixes.get(i).copied().flatten() {
                        Some(prefix) if target == "mysql" && !index.spatial => format!("{}({})", ident, prefix),
                        _ => ident,
                    }
                })
                .collect::<Vec<_>>()
                .join(", ");
            let visibility = if target == "mysql" && index.visible == Some(false) { " INVISIBLE" } else { "" };
            ddl.push(format!(
                "CREATE {}INDEX {} ON {} ({}){};",
                unique,
                quote_ident(target, &index.name),
                quote_ident(target, &table.name),
                columns,
                visibility
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
            let mut actions = String::new();
            if let Some(action) = &fk.on_delete {
                actions.push_str(&format!(" ON DELETE {}", action.as_sql()));
            }
            if let Some(action) = &fk.on_update {
                actions.push_str(&format!(" ON UPDATE {}", action.as_sql()));
            }
            ddl.push(format!(
                "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}){};",
                quote_ident(target, &table.name),
                quote_ident(target, &fk.name),
                columns,
                quote_ident(target, &fk.referenced_table),
                referenced_columns,
                actions
            ));
        }
    }
    if target == "postgresql" {
        for table in &schema.tables {
            if let Some(comment) = &table.comment { ddl.push(format!("COMMENT ON TABLE {} IS {};",quote_ident(target,&table.name),schema_string_literal(target,comment))); }
            for column in &table.columns {
                if let Some(comment) = &column.comment { ddl.push(format!("COMMENT ON COLUMN {}.{} IS {};",quote_ident(target,&table.name),quote_ident(target,&column.name),schema_string_literal(target,comment))); }
            }
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
                    "SELECT setval(pg_get_serial_sequence({}, {}), GREATEST(COALESCE((SELECT MAX({}) FROM {}), 0) + 1, {}), false);",
                    schema_string_literal(target, &quote_ident(target, &table.name)),
                    schema_string_literal(target, &column.name),
                    quote_ident(target, &column.name),
                    quote_ident(target, &table.name),
                    table.auto_increment.unwrap_or(1)
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

/// 청크 SELECT의 텍스트 컬럼 프로젝션 절을 생성한다. 바이너리 컬럼은 hex로 인코딩하고
/// (postgresql=encode, 그 외=HEX), 나머지는 엔진별로 text/CAST로 정규화하여 JSONL 직렬화가
/// 안전한 문자열이 되도록 한다. 세 select_chunk_text_* 함수가 동일 프로젝션을 공유한다.
pub(crate) fn projected_text_columns_sql(engine: &str, table: &NormalizedTable) -> String {
    projected_columns_sql(engine, table, true)
}

/// Projection used before MySQL BIT/FLOAT were read exactly. Safe-promotion journals store content
/// digests computed with it, so live-to-live digest checks keep using it (both sides read alike).
pub(crate) fn legacy_projected_text_columns_sql(engine: &str, table: &NormalizedTable) -> String {
    projected_columns_sql(engine, table, false)
}

fn projected_columns_sql(engine: &str, table: &NormalizedTable, exact_mysql_values: bool) -> String {
    table
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
            } else if engine == "mysql" && is_mysql_spatial_type(&column.type_name) {
                // The text protocol returns geometry as raw SRID+WKB bytes (lossy as UTF-8). Hex of the
                // internal format round-trips through X'..' like mysqldump --hex-blob, SRID included.
                // The legacy projection takes it too: its digests rejected raw geometry as invalid UTF-8,
                // so no stored digest depends on the old form.
                format!(
                    "HEX({}) AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if let (true, Some(width)) = (exact_mysql_values && engine == "mysql", mysql_bit_width(&column.type_name)) {
                // The text protocol returns BIT as raw bytes (lossy as UTF-8). Zero-padded binary digits
                // are exact and match PostgreSQL's bit::text, so cross-engine values compare equal.
                format!(
                    "LPAD(BIN({}), {width}, '0') AS {}",
                    quote_ident(engine, &column.name),
                    quote_ident(engine, &column.name)
                )
            } else if exact_mysql_values && engine == "mysql" && column.type_name.trim().to_ascii_lowercase().starts_with("float") {
                // The text protocol renders FLOAT with 6 significant digits; as a double it round-trips
                // exactly, so dumps keep the stored value and keyset cursors stay exact.
                format!(
                    "({} + 0e0) AS {}",
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
        .join(", ")
}

pub fn select_chunk_text_sql(
    engine: &str,
    table: &NormalizedTable,
    key_columns: &[String],
) -> String {
    let columns = column_names(table);
    let projected_columns = projected_text_columns_sql(engine, table);
    let order_columns: Vec<String> = if key_columns.is_empty() {
        columns
    } else {
        key_columns.to_vec()
    };
    // MySQL sorts NULL first; PostgreSQL sorts it last. Offset pages compared across engines
    // (verify, keyless copy) must line up, so PostgreSQL uses MySQL's NULL placement.
    let null_order = if engine == "postgresql" && key_columns.is_empty() { " NULLS FIRST" } else { "" };
    let order_by = order_columns
        .iter()
        .map(|column| format!("{}{null_order}", quote_ident(engine, column)))
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

/// Rows whose key equals one of `keys`, with no ORDER BY: verify looks target rows up by the source
/// page's keys, so the two engines' collation orders never have to agree.
pub fn select_chunk_text_by_keys_sql(engine: &str, table: &NormalizedTable, key_columns: &[String], keys: &[Vec<String>]) -> String {
    let terms = keys.iter().map(|values| {
        let parts = key_columns.iter().zip(values).map(|(column, value)| keyset_term(engine, table, column, value, false)).collect::<Vec<_>>();
        format!("({})", parts.join(" AND "))
    }).collect::<Vec<_>>();
    format!(
        "SELECT {} FROM {} WHERE {}",
        projected_text_columns_sql(engine, table),
        quote_ident(engine, &table.name),
        if terms.is_empty() { "1 = 0".to_string() } else { terms.join(" OR ") }
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
    let projected_columns = projected_text_columns_sql(engine, table);
    let order_by = key_columns
        .iter()
        .map(|column| quote_column_ref(engine, &table.name, column))
        .collect::<Vec<_>>()
        .join(", ");
    let where_clause = if let Some(values) = last_key_values {
        let predicates = keyset_predicates(engine, table, key_columns, values);
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
    let projected_columns = projected_text_columns_sql(engine, table);
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

/// Literal for a keyset comparison value. The cursor token is the text the SELECT projected, which for
/// binary columns is the HEX of the bytes (`projected_text_columns_sql`). Comparing that text to the raw
/// binary column compares bytes against ASCII hex digits, so the cursor skips and repeats rows (or stops
/// advancing); binary keys must be decoded back to bytes.
/// One keyset term (`column = value` or `column > value`) that orders exactly like `ORDER BY column`.
/// MySQL types a comparison by its operands, not by the column alone:
/// - a quoted text literal is parsed with backslash escapes, so `a\b` becomes another value;
/// - ENUM orders by index in `ORDER BY` but compares as a string against a quoted label, so the
///   `>` term lists the labels after the cursor (an IN list keeps the index range usable);
/// - SET orders by its bitmask.
/// FLOAT keys are exact because the projection reads them as doubles (`projected_text_columns_sql`).
fn keyset_term(engine: &str, table: &NormalizedTable, column: &str, value: &str, greater: bool) -> String {
    let column_ref = quote_column_ref(engine, &table.name, column);
    let op = if greater { ">" } else { "=" };
    let text = Value::String(value.to_string());
    let Some(found) = table.columns.iter().find(|candidate| candidate.name == column) else {
        return format!("{column_ref} {op} {}", sql_literal(&text));
    };
    if is_binary_type(&found.type_name) {
        return format!("{column_ref} {op} {}", sql_literal_for_column(engine, &found.type_name, &text));
    }
    if engine != "mysql" {
        // Same value conversion as the copy (tinyint(1) -> boolean, NUL stripped), so a key
        // lookup on the target matches the stored row.
        return format!("{column_ref} {op} {}", sql_literal_for_column(engine, &found.type_name, &text));
    }
    let lowered = found.type_name.trim().to_ascii_lowercase();
    let base = lowered.split(['(', ' ']).next().unwrap_or("");
    match base {
        "enum" => {
            if let Some(labels) = crate::import::mysql_enum_labels(&found.type_name) {
                if let Some(position) = labels.iter().position(|label| label == value) {
                    if !greater {
                        return format!("{column_ref} = {}", mysql_text_literal(value));
                    }
                    let later = labels[position + 1..].iter().map(|label| mysql_text_literal(label)).collect::<Vec<_>>();
                    return if later.is_empty() {
                        "FALSE".to_string()
                    } else {
                        format!("{column_ref} IN ({})", later.join(", "))
                    };
                }
            }
        }
        "set" => {
            // ponytail: SET keys page by bitmask expression (no index range); SET primary keys are rare.
            let members = crate::import::mysql_enum_labels(&format!("enum{}", &found.type_name.trim()[3..]));
            if let Some(members) = members {
                let mask = value.split(',').filter(|item| !item.is_empty()).try_fold(0u64, |mask, item| {
                    members.iter().position(|member| member == item).map(|bit| mask | (1u64 << bit))
                });
                if let Some(mask) = mask {
                    return format!("({column_ref}+0) {op} {mask}");
                }
            }
        }
        "char" | "varchar" | "tinytext" | "text" | "mediumtext" | "longtext" | "character" => {
            return format!("{column_ref} {op} {}", mysql_text_literal(value));
        }
        // PostgreSQL `bit varying` lands in a MySQL text column; only real BIT compares as an integer.
        "bit" if mysql_bit_width(&found.type_name).is_some() => {
            if let Ok(number) = u64::from_str_radix(value, 2) {
                return format!("{column_ref} {op} {number}");
            }
        }
        "bit" => return format!("{column_ref} {op} {}", mysql_text_literal(value)),
        _ => {}
    }
    // A PostgreSQL timestamptz cursor compared on a MySQL DATETIME (cross-engine verify) drops the
    // UTC offset the same way the copy does.
    format!("{column_ref} {op} {}", sql_literal_for_column(engine, &found.type_name, &text))
}

/// A sql_mode-independent text literal (no backslash processing). It is only coercible, so MySQL
/// converts it to the column's character set and compares in the column's collation, as `ORDER BY` does.
fn mysql_text_literal(value: &str) -> String {
    format!("_utf8mb4 X'{}'", hex::encode_upper(value.as_bytes()))
}

fn keyset_predicates(
    engine: &str,
    table: &NormalizedTable,
    key_columns: &[String],
    values: &[String],
) -> Vec<String> {
    let pair_count = key_columns.len().min(values.len());
    let mut predicates = Vec::new();
    for index in 0..pair_count {
        let mut parts = Vec::new();
        for previous in 0..index {
            parts.push(keyset_term(engine, table, &key_columns[previous], &values[previous], false));
        }
        parts.push(keyset_term(engine, table, &key_columns[index], &values[index], true));
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

/// `INSERT INTO t (cols) VALUES (...), (...)`를 조립하는 공통 헬퍼.
/// row가 Object가 아니면 모든 컬럼을 NULL로, Object면 컬럼명으로 값을 조회해
/// `literal_for(컬럼명, 값)`으로 SQL 리터럴을 만든다. 값이 없으면 Value::Null로 대체한다.
fn insert_values_sql(
    engine: &str,
    table: &str,
    column_names: &[&str],
    rows: &[Value],
    literal_for: impl Fn(&str, &Value) -> String,
) -> String {
    let column_sql = column_names
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    let values_sql = rows
        .iter()
        .map(|row| {
            let values = column_names
                .iter()
                .map(|column| match row {
                    Value::Object(object) => {
                        literal_for(column, object.get(*column).unwrap_or(&Value::Null))
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

pub fn insert_rows_literal_sql(
    engine: &str,
    table: &str,
    columns: &[String],
    rows: &[Value],
) -> String {
    let column_names: Vec<&str> = columns.iter().map(String::as_str).collect();
    insert_values_sql(engine, table, &column_names, rows, |_column, value| {
        sql_literal(value)
    })
}

pub fn insert_rows_literal_sql_for_table(
    target_engine: &str,
    table: &NormalizedTable,
    rows: &[Value],
) -> String {
    let column_names: Vec<&str> = table.columns.iter().map(|column| column.name.as_str()).collect();
    let column_types: BTreeMap<&str, &str> = table
        .columns
        .iter()
        .map(|column| (column.name.as_str(), column.type_name.as_str()))
        .collect();
    insert_values_sql(target_engine, &table.name, &column_names, rows, |column, value| {
        let source_type = column_types.get(column).copied().unwrap_or("");
        sql_literal_for_column(target_engine, source_type, value)
    })
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
        if let Some(flag) = tinyint_flag(&text) {
            text = flag.to_string();
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

/// MySQL TINYINT(1) holds -128..127; any nonzero value is true, as MySQL itself treats it.
fn tinyint_flag(text: &str) -> Option<bool> {
    if text.eq_ignore_ascii_case("true") {
        return Some(true);
    }
    if text.eq_ignore_ascii_case("false") {
        return Some(false);
    }
    text.trim().parse::<i64>().ok().map(|number| number != 0)
}

fn csv_quote(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

pub(crate) fn sanitize_postgresql_text(value: &str) -> String {
    value.replace('\0', "")
}

pub fn sql_literal_for_column(target_engine: &str, source_type: &str, value: &Value) -> String {
    if let Value::String(text) = value {
        if target_engine == "mysql" {
            let lowered = source_type.trim().to_ascii_lowercase();
            if lowered.starts_with("timestamptz") || (lowered.starts_with("timestamp") && lowered.contains("with time zone")) {
                // Sessions are UTC (migrate, dump), so the offset is +00; DATETIME stores the bare UTC value.
                for suffix in ["+00:00", "+00", "Z"] {
                    if let Some(utc) = text.strip_suffix(suffix) {
                        return sql_literal(&Value::String(utc.to_string()));
                    }
                }
            }
        }
        if target_engine == "mysql"
            && mysql_bit_width(source_type).is_some()
            && !text.is_empty()
            && text.bytes().all(|byte| byte == b'0' || byte == b'1')
        {
            return format!("b'{text}'");
        }
        if target_engine == "mysql"
            && is_mysql_spatial_type(source_type)
            && !text.is_empty()
            && text.len() % 2 == 0
            && text.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            // PostgreSQL point/polygon text is never bare hex, so only MySQL geometry hex lands here.
            return format!("X'{text}'");
        }
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
            if let Some(flag) = tinyint_flag(text) {
                return flag.to_string().to_ascii_uppercase();
            }
        }
        if target_engine == "postgresql" {
            return sql_literal(&Value::String(sanitize_postgresql_text(text)));
        }
        return mysql_or_generic_literal(target_engine, &source_type, value);
    }
    mysql_or_generic_literal(target_engine, source_type, value)
}

/// mysql 타겟이면 JSON 타입은 `_utf8mb4'...'` 도입자를 붙이고, 그 외 mysql 값은 mysql
/// 이스케이프 규칙으로, mysql이 아니면 범용 SQL 리터럴로 변환한다.
/// sql_literal_for_column의 String/비-String 경로가 공유하던 3분기 로직을 통합한 헬퍼다.
fn mysql_or_generic_literal(target_engine: &str, source_type: &str, value: &Value) -> String {
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
    // BIT digits and geometry hex must be written as literals, not loaded as text, so those tables
    // take the literal path too.
    table.columns.iter().any(|column| {
        is_binary_type(&column.type_name)
            || mysql_bit_width(&column.type_name).is_some()
            || is_mysql_spatial_type(&column.type_name)
    })
}

/// MySQL spatial column types; `point srid 4326` carries the column SRID attribute. PostgreSQL has
/// its own `point`/`polygon`, so callers apply this to MySQL data only.
pub(crate) fn is_mysql_spatial_type(type_name: &str) -> bool {
    let lowered = type_name.trim().to_ascii_lowercase();
    let base = lowered.split([' ', '(']).next().unwrap_or("");
    matches!(base, "geometry" | "point" | "linestring" | "polygon" | "multipoint" | "multilinestring"
        | "multipolygon" | "geometrycollection" | "geomcollection")
}

/// Width of a `bit` / `bit(n)` column (both engines report this spelling); `None` for other types,
/// including PostgreSQL `bit varying`.
pub(crate) fn mysql_bit_width(type_name: &str) -> Option<u32> {
    let lowered = type_name.trim().to_ascii_lowercase();
    let rest = lowered.strip_prefix("bit")?;
    let rest = rest.trim_start();
    if rest.is_empty() || rest.starts_with("unsigned") {
        return Some(1);
    }
    let (digits, tail) = rest.strip_prefix('(')?.split_once(')')?;
    let tail = tail.trim();
    if !(tail.is_empty() || tail == "unsigned") {
        return None;
    }
    digits.trim().parse::<u32>().ok().filter(|width| (1..=64).contains(width))
}

/// Legacy (format_version < 4) MySQL dumps stored BIT cells as the raw bytes read as text. Bytes that
/// were valid UTF-8 (e.g. BIT(1) flags) are recoverable; a replacement character means the value was lost.
pub(crate) fn legacy_mysql_bit_digits(text: &str, width: u32) -> Result<String, String> {
    if text.contains('\u{fffd}') {
        // ponytail: a genuine 0xEFBFBD byte run is indistinguishable from lossy decoding here.
        return Err("value may have been corrupted by a previous export (BIT bytes >= 0x80); re-export with this version".into());
    }
    let value = text.as_bytes().iter().try_fold(0u128, |acc, byte| {
        acc.checked_mul(256).map(|shifted| shifted | u128::from(*byte))
    }).ok_or("BIT value wider than 64 bits")?;
    if width < 128 && value >> width != 0 {
        return Err(format!("BIT value does not fit in bit({width})"));
    }
    Ok(format!("{value:0width$b}", width = width as usize))
}

fn temporal_base(type_name: &str) -> String {
    let lowered = type_name.trim().to_ascii_lowercase();
    if lowered.ends_with("[]") {
        // Arrays move as their text form (LONGTEXT); their elements are not checked here.
        return String::new();
    }
    if lowered.starts_with("time") && lowered.contains("with time zone") && !lowered.starts_with("timestamp") {
        return "timetz".to_string();
    }
    lowered.split([' ', '(']).next().unwrap_or("").to_string()
}

/// Why a temporal value cannot be stored by the other engine, or `None` when it can. MySQL keeps
/// zero/partial dates and TIME up to 838:59:59; PostgreSQL keeps infinity, BC dates and years past
/// 9999. Nothing converts these silently: callers refuse before writing.
pub(crate) fn temporal_value_problem(source_engine: &str, source_type: &str, text: &str) -> Option<&'static str> {
    let base = temporal_base(source_type);
    let text = text.trim();
    if source_engine == "mysql" {
        if matches!(base.as_str(), "date" | "datetime" | "timestamp") {
            let mut parts = text.get(..10)?.split('-').map(|part| part.parse::<u32>().ok());
            let (year, month, day) = (parts.next()??, parts.next()??, parts.next()??);
            if year == 0 || month == 0 || day == 0 {
                return Some("a zero or partial date (year, month or day 0)");
            }
            // ALLOW_INVALID_DATES keeps days such as 2024-02-30.
            let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
            let days = match month { 2 if leap => 29, 2 => 28, 4 | 6 | 9 | 11 => 30, _ => 31 };
            return (month > 12 || day > days).then_some("an invalid date (day past the end of the month)");
        }
        if base == "time" {
            if text.starts_with('-') {
                return Some("a negative TIME");
            }
            let (hours, rest) = text.split_once(':')?;
            let hours = hours.parse::<u32>().ok()?;
            let past_midnight = hours == 24 && rest.bytes().any(|byte| matches!(byte, b'1'..=b'9'));
            return (hours > 24 || past_midnight).then_some("a TIME beyond 24:00:00");
        }
    } else if source_engine == "postgresql" && matches!(base.as_str(), "date" | "timestamp") {
        if text.contains("infinity") {
            return Some("an infinite date");
        }
        if text.ends_with(" BC") {
            return Some("a BC date");
        }
        let year = text.split('-').next().unwrap_or("");
        return (year.len() > 4).then_some("a year after 9999");
    }
    None
}

/// SQL condition (on the source) matching the values `temporal_value_problem` refuses.
pub(crate) fn temporal_scan_condition(source_engine: &str, source_type: &str, column_ref: &str) -> Option<String> {
    match (source_engine, temporal_base(source_type).as_str()) {
        ("mysql", "date" | "datetime" | "timestamp") => Some(format!("(YEAR({column_ref}) = 0 OR MONTH({column_ref}) = 0 OR DAY({column_ref}) = 0 OR DAY({column_ref}) > DAY(LAST_DAY({column_ref})))")),
        ("mysql", "time") => Some(format!("({column_ref} < '00:00:00' OR {column_ref} > '24:00:00')")),
        ("postgresql", "date" | "timestamp") => Some(format!("(NOT isfinite({column_ref}) OR extract(year from {column_ref}) NOT BETWEEN 1 AND 9999)")),
        _ => None,
    }
}

/// Column types the other engine has no column for: blocking (no lossless mapping) or a warning
/// (the value moves as text). PostgreSQL `time with time zone` would lose its offset in MySQL TIME.
pub(crate) fn temporal_type_problem(source_engine: &str, source_type: &str) -> Option<(bool, &'static str)> {
    match (source_engine, temporal_base(source_type).as_str()) {
        ("postgresql", "timetz") => Some((true, "time with time zone has no MySQL type that keeps the offset")),
        ("postgresql", "interval") => Some((false, "interval is copied as its text form into a MySQL text column")),
        _ => None,
    }
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

/// Null/Bool/Number의 공통 SQL 리터럴화를 담당하고, 문자열/배열/객체는 주어진 `escape`
/// 클로저로 이스케이프한다. sql_literal(작은따옴표 doubling)과 mysql_sql_literal(mysql
/// 이스케이프)이 동일한 Null/Bool/Number arm을 공유하도록 통합한 헬퍼다.
fn generic_sql_literal(value: &Value, escape: impl Fn(&str) -> String) -> String {
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
        Value::String(value) => escape(value),
        Value::Array(_) | Value::Object(_) => escape(&value.to_string()),
    }
}

pub fn sql_literal(value: &Value) -> String {
    generic_sql_literal(value, |text| format!("'{}'", text.replace('\'', "''")))
}

fn mysql_sql_literal(value: &Value) -> String {
    generic_sql_literal(value, mysql_string_literal)
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
        "SELECT c.column_name, c.data_type, c.is_nullable, c.character_maximum_length, c.numeric_precision, c.numeric_scale, c.column_default, c.is_identity, pg_catalog.format_type(a.atttypid, a.atttypmod) FROM information_schema.columns c JOIN pg_catalog.pg_namespace n ON n.nspname=c.table_schema JOIN pg_catalog.pg_class t ON t.relnamespace=n.oid AND t.relname=c.table_name JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attname=c.column_name WHERE c.table_schema = $1 AND c.table_name = $2 ORDER BY c.ordinal_position"
    } else {
        "SELECT COLUMN_NAME AS column_name, COLUMN_TYPE AS data_type, CHARACTER_SET_NAME AS character_set, COLLATION_NAME AS collation, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default, EXTRA AS extra, COLUMN_COMMENT AS column_comment FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ORDINAL_POSITION"
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
        "SELECT c.conname, a.attname, parent.relname, pa.attname, CASE c.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END, CASE c.confupdtype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END FROM pg_constraint c JOIN pg_class child ON child.oid = c.conrelid JOIN pg_namespace n ON n.oid = child.relnamespace JOIN pg_class parent ON parent.oid = c.confrelid JOIN unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(child_num, parent_num, ord) ON TRUE JOIN pg_attribute a ON a.attrelid = child.oid AND a.attnum = k.child_num JOIN pg_attribute pa ON pa.attrelid = parent.oid AND pa.attnum = k.parent_num WHERE n.nspname = $1 AND child.relname = $2 AND c.contype = 'f' ORDER BY c.conname, k.ord"
    } else {
        "SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.DELETE_RULE, r.UPDATE_RULE FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND r.TABLE_NAME = k.TABLE_NAME WHERE k.TABLE_SCHEMA = ? AND k.TABLE_NAME = ? AND k.REFERENCED_TABLE_NAME IS NOT NULL ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
    }
}

pub fn inspect_indexes_sql(engine: &str) -> &'static str {
    if engine == "postgresql" {
        "SELECT i.relname AS index_name, a.attname AS column_name, ix.indisunique AS is_unique FROM pg_class t JOIN pg_index ix ON t.oid = ix.indrelid JOIN pg_class i ON i.oid = ix.indexrelid JOIN pg_namespace n ON n.oid = t.relnamespace JOIN unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum WHERE n.nspname = $1 AND t.relname = $2 AND NOT ix.indisprimary ORDER BY i.relname, k.ord"
    } else {
        "SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name, SUB_PART AS sub_part, CASE WHEN NON_UNIQUE = 0 THEN 1 ELSE 0 END AS is_unique FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_NAME <> 'PRIMARY' AND COLUMN_NAME IS NOT NULL ORDER BY INDEX_NAME, SEQ_IN_INDEX"
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

pub(crate) fn group_indexes(
    rows: Vec<(String, String, Option<u32>, bool)>,
) -> Vec<NormalizedIndex> {
    let mut grouped: BTreeMap<String, NormalizedIndex> = BTreeMap::new();
    for (name, column, sub_part, unique) in rows {
        let index = grouped
            .entry(name.clone())
            .or_insert_with(|| NormalizedIndex {
                name,
                columns: Vec::new(),
                column_prefixes: Vec::new(),
                unique,
                visible: None, spatial: false,
            });
        index.unique = index.unique || unique;
        index.columns.push(column);
        index.column_prefixes.push(sub_part);
    }
    grouped.into_values().collect()
}

pub(crate) fn group_foreign_keys(rows: Vec<(String, String, String, String, String, String)>) -> Result<Vec<NormalizedForeignKey>, String> {
    let mut grouped: BTreeMap<String, NormalizedForeignKey> = BTreeMap::new();
    for (name, column, referenced_table, referenced_column, on_delete, on_update) in rows {
        let on_delete = serde_json::from_value::<ForeignKeyAction>(serde_json::json!(on_delete))
            .map_err(|err| format!("invalid FK delete action: {err}"))?;
        let on_update = serde_json::from_value::<ForeignKeyAction>(serde_json::json!(on_update))
            .map_err(|err| format!("invalid FK update action: {err}"))?;
        let fk = grouped
            .entry(name.clone())
            .or_insert_with(|| NormalizedForeignKey {
                name,
                columns: Vec::new(),
                referenced_table,
                referenced_columns: Vec::new(),
                on_delete: Some(on_delete),
                on_update: Some(on_update),
            });
        fk.columns.push(column);
        fk.referenced_columns.push(referenced_column);
    }
    Ok(grouped.into_values().collect())
}

pub(crate) fn generate_table_ddl(table: &NormalizedTable, source: &str, target: &str) -> Option<String> {
    validate_target_foreign_key_actions(table,target).ok()?;
    let (mut lines, primary_keys) = column_ddl_lines(table, source, target)?;
    if !primary_keys.is_empty() {
        lines.push(format!("  PRIMARY KEY ({})", primary_keys.join(", ")));
    }
    if target == "mysql" {
        for index in table.indexes.iter().filter(|index| index.unique && !index.columns.is_empty()) {
            let columns = index.columns.iter().enumerate().map(|(position, column)| {
                let name = quote_ident(target, column);
                match index.column_prefixes.get(position).copied().flatten() {
                    Some(prefix) => format!("{name}({prefix})"), None => name,
                }
            }).collect::<Vec<_>>().join(", ");
            let visibility = if index.visible == Some(false) { " INVISIBLE" } else { "" };
            lines.push(format!("  UNIQUE KEY {} ({columns}){visibility}", quote_ident(target, &index.name)));
        }
        for check in &table.checks {
            lines.push(format!("  CONSTRAINT {} CHECK ({}) {}",quote_ident(target,&check.name),check.expression,if check.enforced { "ENFORCED" } else { "NOT ENFORCED" }));
        }
    }
    let mut table_suffix = mysql_table_collation_suffix(source, target, table)?;
    if target == "mysql" {
        if let Some(counter) = table.auto_increment { table_suffix.push_str(&format!(" AUTO_INCREMENT={counter}")); }
        if let Some(comment) = &table.comment { table_suffix.push_str(&format!(" COMMENT={}",schema_string_literal(target,comment))); }
    }
    Some(format!(
        "CREATE TABLE {} (\n{}\n){};",
        quote_ident(target, &table.name),
        lines.join(",\n"),
        table_suffix
    ))
}

/// 각 컬럼의 DDL 라인과 primary key 컬럼 목록을 만든다. 최종 DDL에 들어갈 타입 문자열
/// (mapped_type)을 is_safe_column_type로 검증하고, 위반 시 fail-closed로 None을 반환해
/// 컬럼 정의 탈출(CTAS/추가 컬럼 주입)을 차단한다.
fn column_ddl_lines(
    table: &NormalizedTable,
    source: &str,
    target: &str,
) -> Option<(Vec<String>, Vec<String>)> {
    let mut lines = Vec::new();
    let mut primary_keys = Vec::new();
    let mapped_types = column_target_types(table, source, target);

    for (column, (_, mapped_type)) in table.columns.iter().zip(mapped_types) {
        let auto_increment = is_auto_increment_type(&column.type_name);
        // 최종 DDL에 들어가는 타입 문자열(mapped_type)을 검증한다. same-engine은 원문이 그대로 들어가고,
        // cross-engine도 map_type이 varchar/decimal/numeric 등에서 원문을 대문자화만 해 통과시키므로
        // (예: `varchar(45), evil int` -> `VARCHAR(45), EVIL INT`), 변환 후 값을 검증해야 same/cross-engine
        // 모든 경로에서 컬럼 정의 탈출(CTAS/추가 컬럼 주입)을 fail-closed로 막을 수 있다.
        if !is_safe_column_type(&mapped_type) {
            return None;
        }
        let default_sql = if auto_increment {
            String::new()
        } else if column.default_is_expression && column.default_value.as_deref().is_some_and(|value| value.trim().trim_matches(['(',')']).eq_ignore_ascii_case("uuid")) {
            if target != "mysql" { return None; }
            " DEFAULT (UUID())".to_string()
        } else {
            if source == "postgresql" && target != "postgresql" && column.default_value.as_deref()
                .map(|value| value.eq_ignore_ascii_case("gen_random_uuid()") || value.to_ascii_uppercase().starts_with("ARRAY["))
                .unwrap_or(false) { return None; }
            default_clause(target, column.default_value.as_deref(), &column.type_name)
        };
        let on_update_sql = if let Some(value) = &column.on_update {
            let value = value.to_ascii_uppercase();
            if target != "mysql" || !is_safe_temporal_default_expression(&value) { return None; }
            format!(" ON UPDATE {value}")
        } else { String::new() };
        let null_sql = if column.nullable { "" } else { " NOT NULL" };
        let generation_sql = if auto_increment && target == "postgresql" {
            table.auto_increment.map(|counter| format!(" GENERATED BY DEFAULT AS IDENTITY (START WITH {counter})")).unwrap_or_else(|| " GENERATED BY DEFAULT AS IDENTITY".to_string())
        } else if auto_increment && target == "mysql" {
            " AUTO_INCREMENT".to_string()
        } else {
            String::new()
        };
        let comment_sql = if target == "mysql" { column.comment.as_ref().map(|comment| format!(" COMMENT {}",schema_string_literal(target,comment))).unwrap_or_default() } else { String::new() };
        lines.push(format!(
            "  {} {}{}{}{}{}{}",
            quote_ident(target, &column.name),
            mapped_type,
            generation_sql,
            default_sql,
            on_update_sql,
            null_sql,
            comment_sql
        ));
        if column.primary_key {
            primary_keys.push(quote_ident(target, &column.name));
        }
    }

    Some((lines, primary_keys))
}

/// same-engine(MySQL→MySQL) 일 때만 테이블 레벨 `COLLATE=...` 접미사를 만든다.
/// cross-engine이나 PostgreSQL 타겟은 빈 문자열을 반환하고, 변조된 collation 값은
/// fail-closed로 None을 반환한다.
fn mysql_table_collation_suffix(
    source: &str,
    target: &str,
    table: &NormalizedTable,
) -> Option<String> {
    // 테이블 레벨 기본 collation 재현은 같은 엔진(MySQL→MySQL)에서만 한다.
    // cross-engine이나 PostgreSQL 타겟에는 테이블 레벨 DEFAULT COLLATE 개념이 없어 붙이면 오류가 난다.
    // COLLATE만 지정해도 MySQL이 해당 collation의 charset을 자동 결정하므로 charset은 별도로 방출하지 않는다.
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
                Some(format!(" COLLATE={collation}"))
            }
            Some(_) => None,
            None => Some(String::new()),
        }
    } else {
        Some(String::new())
    }
}

/// dump 매니페스트에서 온 collation 문자열이 MySQL collation 식별자로 안전한지 검사한다.
/// 영숫자와 밑줄로만 이루어진 1~64자만 허용하여, 공백/괄호/세미콜론/등호 등을 통한
/// CTAS·table_options SQL 주입을 fail-closed로 차단한다.
fn is_valid_mysql_collation_ident(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MYSQL_IDENTIFIER_MAX_LEN
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// base 식별자를 파싱한다: ascii 알파벳으로 시작해 이후 영숫자/밑줄. 성공 시 진행된 index를,
/// 첫 글자가 알파벳이 아니면 None을 반환한다.
fn parse_base_ident(bytes: &[u8], mut i: usize) -> Option<usize> {
    if i >= bytes.len() || !bytes[i].is_ascii_alphabetic() {
        return None;
    }
    i += 1;
    while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
        i += 1;
    }
    Some(i)
}

/// enum/set의 따옴표 문자열 리스트를 파싱한다: qstr (',' qstr)*. `i`는 첫 여는 따옴표를 가리켜야 한다.
/// 백슬래시를 포함한 값과 닫히지 않은 문자열은 fail-closed로 거부한다(어느 MySQL 이스케이프
/// 모드에서도 validator와 서버의 따옴표 경계가 어긋나지 않도록).
fn parse_quoted_string_list(bytes: &[u8], mut i: usize) -> Option<usize> {
    loop {
        if i >= bytes.len() || bytes[i] != b'\'' {
            return None;
        }
        i += 1;
        loop {
            if i >= bytes.len() {
                return None; // 닫히지 않은 문자열
            }
            match bytes[i] {
                b'\\' => return None,
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
    Some(i)
}

/// 숫자 리스트를 파싱한다: digits (',' digits)*. 숫자가 하나도 없으면 None을 반환한다.
fn parse_numeric_list(bytes: &[u8], mut i: usize) -> Option<usize> {
    loop {
        let numeric_list_start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            i += 1;
        }
        if i == numeric_list_start {
            return None;
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
    Some(i)
}

/// 선택적 인자 그룹 '(' ... ')'를 파싱한다. `i`는 여는 괄호를 가리켜야 한다.
/// 내부는 따옴표 문자열 리스트(enum/set) 또는 숫자 리스트다.
fn parse_arg_group(bytes: &[u8], i: usize) -> Option<usize> {
    let mut i = i + 1;
    while i < bytes.len() && bytes[i] == b' ' {
        i += 1;
    }
    i = if i < bytes.len() && bytes[i] == b'\'' {
        parse_quoted_string_list(bytes, i)?
    } else {
        parse_numeric_list(bytes, i)?
    };
    while i < bytes.len() && bytes[i] == b' ' {
        i += 1;
    }
    if i >= bytes.len() || bytes[i] != b')' {
        return None;
    }
    Some(i + 1)
}

/// 하나의 후행 modifier 단어와 그 인자를 파싱한다. `i`는 단어 첫 글자(비공백)를 가리켜야 한다.
///   modifier = unsigned | zerofill | precision | varying [ '(' digits ')' ]
///            | (with|without) time zone | (charset|collate) <ident>
///            | character set <ident>
/// 허용되지 않는 단어나 비단어 문자는 None으로 거부한다.
fn parse_modifier_word(s: &str, bytes: &[u8], i: usize) -> Option<usize> {
    let modifier_word_start = i;
    let mut i = i;
    while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
        i += 1;
    }
    if i == modifier_word_start {
        return None; // 비단어 문자(괄호/세미콜론/등호 등) → 거부
    }
    let word = s[modifier_word_start..i].to_ascii_lowercase();
    match word.as_str() {
        // PostgreSQL 원형 다단어 타입 꼬리 허용: `double precision`, `bit/character varying`,
        // `timestamp/time with|without time zone`. same-engine PostgreSQL에서는 map_type이
        // 원문 type_name을 그대로 반환하므로, 이 타입들을 거부하면 정상 import가 깨진다(fidelity).
        "unsigned" | "zerofill" | "precision" => Some(i),
        "srid" => {
            // MySQL 8 column SRID attribute: `point srid 4326`.
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
            let digits_start = i;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
            (i > digits_start).then_some(i)
        }
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
                let varying_length_start = i;
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                }
                if i == varying_length_start {
                    return None;
                }
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                if i >= bytes.len() || bytes[i] != b')' {
                    return None;
                }
                i += 1;
            }
            Some(i)
        }
        "with" | "without" => {
            for expected in ["time", "zone"] {
                while i < bytes.len() && bytes[i] == b' ' {
                    i += 1;
                }
                let time_zone_word_start = i;
                while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
                    i += 1;
                }
                if !s[time_zone_word_start..i].eq_ignore_ascii_case(expected) {
                    return None;
                }
            }
            Some(i)
        }
        "charset" | "collate" => {
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
            let charset_or_collate_value_start = i;
            while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                i += 1;
            }
            if i == charset_or_collate_value_start {
                return None;
            }
            Some(i)
        }
        "character" => {
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
            let set_keyword_start = i;
            while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
                i += 1;
            }
            if !s[set_keyword_start..i].eq_ignore_ascii_case("set") {
                return None;
            }
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
            let character_set_name_start = i;
            while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                i += 1;
            }
            if i == character_set_name_start {
                return None;
            }
            Some(i)
        }
        _ => None,
    }
}

/// dump 매니페스트에서 온 MySQL 컬럼 타입 문자열이 안전한 문법인지 검사한다.
/// 같은 엔진(MySQL→MySQL) import 시 type_name은 검증 없이 그대로 CREATE TABLE 컬럼 정의에 들어가므로,
/// 변조된 값(`int) AS (SELECT ...`, `int, evil int`, `int; ...` 등)이 컬럼 정의를 탈출하지 못하게 막는다.
/// 허용 문법: <base ident> [ '(' (숫자리스트 | 따옴표문자열리스트) ')' ] [ modifier ]*
///   modifier = unsigned | zerofill | (character set | charset | collate) <ident>
/// enum('a','b'), decimal(10,2), varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin 등 정상 타입은 통과한다.
/// enum/set 값 리스트는 따옴표 문자열이라 단순 식별자 allowlist로는 걸러낼 수 없어 구조적으로 파싱한다.
fn is_safe_column_type(type_name: &str) -> bool {
    let mut s = type_name.trim();
    while let Some(base) = s.strip_suffix("[]") { s = base.trim_end(); }
    if s.is_empty() || s.len() > MAX_COLUMN_TYPE_LEN {
        return false;
    }
    let bytes = s.as_bytes();

    // 1) base 식별자
    let Some(mut i) = parse_base_ident(bytes, 0) else {
        return false;
    };

    // 2) 선택적 인자 그룹 '(' ... ')'
    if i < bytes.len() && bytes[i] == b'(' {
        match parse_arg_group(bytes, i) {
            Some(next) => i = next,
            None => return false,
        }
    }

    // 3) 후행 modifier들 (공백 구분)
    loop {
        while i < bytes.len() && bytes[i] == b' ' {
            i += 1;
        }
        if i >= bytes.len() {
            break;
        }
        match parse_modifier_word(s, bytes, i) {
            Some(next) => i = next,
            None => return false,
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
    if upper == "NOW()" { return "CURRENT_TIMESTAMP".to_string(); }
    if target == "postgresql" && (upper == "GEN_RANDOM_UUID()" || is_safe_numeric_array_default(value)) {
        return value.to_string();
    }
    if target == "postgresql" && source_type.starts_with("tinyint(1)") {
        if matches!(value, "1") || value.eq_ignore_ascii_case("true") {
            return "TRUE".to_string();
        }
        if matches!(value, "0") || value.eq_ignore_ascii_case("false") {
            return "FALSE".to_string();
        }
    }
    if let Some(width) = mysql_bit_width(&source_type) {
        let digits = value.trim_matches('\'');
        let digits = digits.strip_prefix("b'").or_else(|| digits.strip_prefix("B'")).unwrap_or(digits).trim_end_matches('\'');
        if !digits.is_empty() && digits.len() <= width as usize && digits.bytes().all(|byte| byte == b'0' || byte == b'1') {
            // MySQL reads a quoted '0101' as bytes; PostgreSQL bit(n) does not pad shorter literals.
            let padded = format!("{digits:0>width$}", width = width as usize);
            return if target == "mysql" { format!("b'{padded}'") } else { format!("B'{padded}'") };
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
    if is_safe_temporal_default_expression(&upper)
        || matches!(upper.as_str(), "TRUE" | "FALSE")
        || value.parse::<f64>().is_ok()
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
    if target == "postgresql" && inner.contains('\\') {
        format!("E'{}'", inner.replace('\\', "\\\\").replace('\'', "''"))
    } else if target == "postgresql" {
        format!("'{}'", inner.replace('\'', "''"))
    } else {
        format!("'{}'", inner.replace('\\', "\\\\").replace('\'', "''"))
    }
}

fn is_safe_numeric_array_default(value: &str) -> bool {
    let Some(body) = value.strip_prefix("ARRAY[").and_then(|s| s.strip_suffix(']')) else { return false; };
    !body.is_empty() && body.split(',').all(|item| {
        let item = item.trim();
        matches!(item.to_ascii_uppercase().as_str(), "NULL" | "TRUE" | "FALSE")
            || (!item.is_empty() && item.bytes().all(|b| b.is_ascii_digit() || matches!(b, b'.' | b'-' | b'+' | b'e' | b'E')) && item.parse::<f64>().is_ok())
    })
}

pub(crate) fn supported_postgresql_default(value: &str) -> bool {
    let value = strip_postgresql_type_cast(value);
    let upper = value.to_ascii_uppercase();
    if matches!(upper.as_str(), "NULL" | "TRUE" | "FALSE" | "NOW()" | "GEN_RANDOM_UUID()")
        || is_safe_temporal_default_expression(&upper) || is_safe_numeric_array_default(value)
        || value.parse::<f64>().is_ok() { return true; }
    if !value.starts_with('\'') { return false; }
    // A single SQL string token, with doubled quotes, is safe to re-escape.
    let bytes = value.as_bytes(); let mut i = 1;
    while i < bytes.len() {
        if bytes[i] == b'\'' {
            if bytes.get(i+1) == Some(&b'\'') { i += 2; continue; }
            return i + 1 == bytes.len();
        }
        i += 1;
    }
    false
}

fn is_safe_temporal_default_expression(value: &str) -> bool {
    if matches!(
        value,
        "CURRENT_TIMESTAMP" | "CURRENT_DATE" | "CURRENT_TIME"
    ) {
        return true;
    }
    if value == "CURRENT_DATE()" {
        return true;
    }
    for function in ["CURRENT_TIMESTAMP", "CURRENT_TIME"] {
        let Some(arguments) = value.strip_prefix(function) else {
            continue;
        };
        if arguments == "()" {
            return true;
        }
        if arguments.len() == 3
            && arguments.starts_with('(')
            && arguments.ends_with(')')
            && matches!(arguments.as_bytes()[1], b'0'..=b'6')
        {
            return true;
        }
    }
    false
}

fn strip_postgresql_type_cast(value: &str) -> &str {
    let bytes = value.as_bytes();
    let mut quoted = false;
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'\'' {
            if quoted && bytes.get(i+1) == Some(&b'\'') { i += 2; continue; }
            quoted = !quoted;
        } else if !quoted && bytes[i..].starts_with(b"::") { return value[..i].trim(); }
        i += 1;
    }
    value.trim()
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

/// MySQL -> PostgreSQL. Keeps value range (UNSIGNED widens), precision and keyability; only
/// types without a PostgreSQL equivalent (ENUM, SET, spatial) fall back to TEXT.
fn map_mysql_to_postgres(ty: &str) -> String {
    let stripped = strip_mysql_character_options(ty);
    let ty = stripped.trim();
    let unsigned = ty.split_whitespace().any(|word| word == "unsigned");
    // UNSIGNED/ZEROFILL are MySQL-only modifiers; the range is carried by the widened type.
    let base_with_args = ty.split_whitespace().next().unwrap_or("");
    let base = base_with_args.split('(').next().unwrap_or("");
    if ty.starts_with("tinyint(1)") || ty == "boolean" || ty == "bool" {
        "BOOLEAN".to_string()
    } else if base == "bigint" {
        if unsigned { "NUMERIC(20,0)".to_string() } else { "BIGINT".to_string() }
    } else if base == "int" || base == "integer" {
        if unsigned { "BIGINT".to_string() } else { "INTEGER".to_string() }
    } else if base == "mediumint" {
        "INTEGER".to_string()
    } else if base == "smallint" {
        if unsigned { "INTEGER".to_string() } else { "SMALLINT".to_string() }
    } else if base == "tinyint" || base == "year" {
        "SMALLINT".to_string()
    } else if base == "float" || base == "double" || base == "real" {
        // FLOAT is read as an exact double (projected_text_columns_sql), so DOUBLE PRECISION keeps it.
        "DOUBLE PRECISION".to_string()
    } else if base == "decimal" || base == "numeric" || base == "dec" || base == "fixed" {
        format!("NUMERIC{}", &base_with_args[base.len()..]).to_ascii_uppercase()
    } else if base == "varchar" {
        base_with_args.to_ascii_uppercase()
    } else if base == "char" {
        // MySQL returns CHAR without trailing padding; VARCHAR keeps that value and the length.
        let args = &base_with_args[base.len()..];
        // CHAR(0) is legal in MySQL; PostgreSQL VARCHAR needs a length of at least 1.
        if args == "(0)" { "VARCHAR(1)".to_string() } else { format!("VARCHAR{args}").to_ascii_uppercase() }
    } else if ty == "date" {
        "DATE".to_string()
    } else if ty.starts_with("datetime") {
        temporal_type_with_precision("TIMESTAMP", ty, "")
    } else if ty.starts_with("timestamp") {
        temporal_type_with_precision("TIMESTAMPTZ", ty, "")
    } else if ty.starts_with("time") {
        temporal_type_with_precision("TIME", ty, "")
    } else if ty.starts_with("json") {
        "JSONB".to_string()
    } else if ty.contains("blob") || ty.contains("binary") {
        "BYTEA".to_string()
    } else if let Some(width) = mysql_bit_width(ty) {
        format!("BIT({width})")
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

/// PostgreSQL -> MySQL. Unbounded text and binary use the LONG variants (MySQL TEXT/BLOB stop at
/// 64 KB); `column_ddl_lines` narrows them when the column is part of a key, which MySQL cannot
/// index without a length.
fn map_postgres_to_mysql(ty: &str) -> String {
    if ty.ends_with("[]") {
        // Arrays arrive as PostgreSQL array text (`{1,2}`).
        return "LONGTEXT".to_string();
    }
    if ty == "bigint" || ty == "bigserial" {
        "BIGINT".to_string()
    } else if ty == "integer" || ty == "int" || ty == "serial" {
        "INT".to_string()
    } else if ty == "smallint" || ty == "smallserial" {
        "SMALLINT".to_string()
    } else if ty == "real" {
        // Widened: MySQL FLOAT reads back as its exact double, which would not equal PostgreSQL's
        // shortest real text; DOUBLE stores and prints the same digits.
        "DOUBLE".to_string()
    } else if ty == "double precision" {
        "DOUBLE".to_string()
    } else if ty == "boolean" || ty == "bool" {
        "TINYINT(1)".to_string()
    } else if ty == "uuid" {
        "CHAR(36)".to_string()
    } else if ty == "character varying" || ty == "varchar" || ty == "text" {
        "LONGTEXT".to_string()
    } else if ty.starts_with("character varying") {
        ty.replacen("character varying", "VARCHAR", 1)
            .to_ascii_uppercase()
    } else if ty.starts_with("varchar") {
        ty.to_ascii_uppercase()
    } else if let Some(length) = ty.strip_prefix("character(").and_then(|rest| rest.strip_suffix(')')).and_then(|n| n.parse::<u32>().ok()) {
        if length <= 255 { format!("CHAR({length})") } else { format!("VARCHAR({length})") }
    } else if ty == "date" {
        "DATE".to_string()
    } else if ty == "time" || ty.starts_with("time ") || ty.starts_with("time(") {
        temporal_type_with_precision("TIME", ty, "(6)")
    } else if ty.starts_with("timestamp") {
        temporal_type_with_precision("DATETIME", ty, "(6)")
    } else if ty == "jsonb" || ty == "json" {
        "JSON".to_string()
    } else if ty == "bytea" {
        "LONGBLOB".to_string()
    } else if ty == "numeric" || ty == "decimal" {
        // An unconstrained numeric would become MySQL DECIMAL(10,0) and silently drop fractions.
        "DECIMAL(65,30)".to_string()
    } else if ty.starts_with("numeric") || ty.starts_with("decimal") {
        ty.replacen("numeric", "DECIMAL", 1).to_ascii_uppercase()
    } else if ty == "inet" || ty == "cidr" {
        "VARCHAR(43)".to_string()
    } else if ty == "macaddr" || ty == "macaddr8" {
        "VARCHAR(23)".to_string()
    } else if let Some(width) = mysql_bit_width(ty) {
        format!("BIT({width})")
    } else {
        "LONGTEXT".to_string()
    }
}

/// `(source type, target DDL type)` per column, in column order. Cross-engine key columns are
/// adjusted so the key can exist: MySQL cannot index TEXT/BLOB without a length (ERROR 1170), so
/// they become VARCHAR/VARBINARY sized to fit the 3072-byte InnoDB key (utf8mb4) and a longer value
/// fails on insert (strict sessions). PostgreSQL identity columns must be integers and a NUMERIC
/// FK cannot reference one, so identity and FK-child BIGINT UNSIGNED stay BIGINT (a value above
/// 2^63-1 then fails instead of being altered); a BIGINT FK may still reference a NUMERIC key.
pub(crate) fn column_target_types(table: &NormalizedTable, source: &str, target: &str) -> Vec<(String, String)> {
    let key_widths = key_column_widths(table);
    let fk_columns = table.foreign_keys.iter().flat_map(|foreign_key| foreign_key.columns.iter()).collect::<Vec<_>>();
    table.columns.iter().map(|column| {
        let stripped = strip_generation_marker(&column.type_name);
        let mut mapped = map_type(source, target, &stripped);
        let key_width = key_widths.get(&column.name).copied();
        if source != target && target == "mysql" {
            if let Some(width) = key_width {
                let length = (768 / width.max(1)).min(255);
                match mapped.as_str() {
                    "LONGTEXT" | "TEXT" => mapped = format!("VARCHAR({length})"),
                    "LONGBLOB" | "BLOB" => mapped = format!("VARBINARY({length})"),
                    _ => {}
                }
            }
        }
        if source != target && target == "postgresql" && mapped == "NUMERIC(20,0)"
            && (fk_columns.contains(&&column.name) || is_auto_increment_type(&column.type_name)) {
            mapped = "BIGINT".to_string();
        }
        (stripped, mapped)
    }).collect()
}

/// Key columns -> column count of the widest key (PK, unique, index or FK) they belong to.
fn key_column_widths(table: &NormalizedTable) -> BTreeMap<String, usize> {
    let mut keys = vec![table.columns.iter().filter(|column| column.primary_key).map(|column| column.name.clone()).collect::<Vec<_>>()];
    keys.extend(table.columns.iter().filter(|column| column.unique).map(|column| vec![column.name.clone()]));
    keys.extend(table.indexes.iter().map(|index| index.columns.clone()));
    keys.extend(table.foreign_keys.iter().map(|foreign_key| foreign_key.columns.clone()));
    let mut widths = BTreeMap::new();
    for key in keys {
        for name in &key {
            let width = widths.entry(name.clone()).or_insert(0);
            *width = (*width).max(key.len());
        }
    }
    widths
}

fn temporal_type_with_precision(base: &str, source_type: &str, default_precision: &str) -> String {
    let precision = source_type.find('(').and_then(|start| source_type[start..].find(')').map(|end| &source_type[start..start+end+1]))
        .filter(|value| value.len() == 3 && matches!(value.as_bytes()[1], b'0'..=b'6'))
        .unwrap_or(default_precision);
    format!("{base}{precision}")
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn check_expression_validation_preserves_one_expression_boundary() {
        for expression in ["`quantity` > 0", "((quantity > 0) and (quantity < 10))", "label <> 'semi;colon'", "length(label) <= 80", "label in ('a','b')", "`select` <> 0"] {
            assert!(is_safe_check_expression(expression), "{expression}");
        }
        for expression in ["", "quantity > 0); DROP TABLE important; --", "quantity > 0) COMMENT 'x'", "quantity > 0 /* comment */", "quantity > 0 -- comment", "SELECT secret FROM other", "quantity > 0, injected int", "((quantity > 0)", "\"ambiguous\\\"identifier\"=1", "@session_variable=1"] {
            assert!(!is_safe_check_expression(expression), "{expression}");
        }
        assert!(supported_mysql_default_expression("uuid()"));
        assert!(supported_mysql_default_expression("CURRENT_TIMESTAMP(3)"));
        assert!(!supported_mysql_default_expression("custom_function()"));
        assert!(!supported_mysql_default_expression("uuid()); DROP TABLE important"));
    }

    #[test]
    fn mysql_specific_metadata_fails_cross_engine_preflight_explicitly() {
        for (metadata, expected) in [
            (serde_json::json!({"checks":[{"name":"positive","expression":"value>0","enforced":true}]}), "CHECK"),
            (serde_json::json!({"indexes":[{"name":"hidden","columns":["value"],"visible":false}]}), "invisible"),
            (serde_json::json!({"columns":[{"name":"value","type":"varchar(36)","default":"uuid()","default_is_expression":true}]}), "UUID()"),
        ] {
            let mut table=serde_json::json!({"name":"sample","columns":[{"name":"value","type":"int"}]});
            table.as_object_mut().unwrap().extend(metadata.as_object().unwrap().clone());
            let schema: NormalizedSchema=serde_json::from_value(serde_json::json!({"tables":[table]})).unwrap();
            let error=generate_schema_ddl(&schema,"mysql","postgresql").unwrap_err();
            assert!(error.contains(expected),"{error}");
        }
        let schema: NormalizedSchema=serde_json::from_value(serde_json::json!({"tables":[{
            "name":"sample","columns":[{"name":"value","type":"varchar(36)","default":"custom_function()","default_is_expression":true}]
        }]})).unwrap();
        assert!(generate_schema_ddl(&schema,"mysql","mysql").unwrap_err().contains("unsupported expression default"));
    }
    
    
    use serde_json::json;
    
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::{RecordingAdapter, schema, single_pk_table_with_collation};

    /// mysql JSON 컬럼 리터럴 테스트가 공유하는 fixture. `ai_phase1_cache.result_json`에
    /// 유니코드 + 백슬래시 이스케이프가 섞인 JSON을 insert하는 SQL을 만든다.
    fn ai_phase1_cache_json_insert_sql() -> String {
        let table = NormalizedTable {
            name: "ai_phase1_cache".to_string(),
            columns: vec![NormalizedColumn {
                name: "result_json".to_string(),
                type_name: "json".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
                comment: None,
                default_is_expression: false,
                on_update: None,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
        };
        let json_text = r#"{"facts":[{"content":"문서 제목은 \"工伤管理表\"로 표기되어 있다."}]}"#;
        insert_rows_literal_sql_for_table("mysql", &table, &[json!({"result_json": json_text})])
    }

    const AI_PHASE1_CACHE_JSON_INSERT_SQL: &str =
        r#"INSERT INTO `ai_phase1_cache` (`result_json`) VALUES (_utf8mb4'{"facts":[{"content":"문서 제목은 \\"工伤管理表\\"로 표기되어 있다."}]}')"#;

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
            "DATETIME(6)"
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                            comment: None,
                            default_is_expression: false,
                            on_update: None,
                        },
                        NormalizedColumn {
                            name: "user_id".to_string(),
                            type_name: "int".to_string(),
                            default_value: None,
                            nullable: false,
                            primary_key: false,
                            unique: false,
                            comment: None,
                            default_is_expression: false,
                            on_update: None,
                        },
                    ],
                    indexes: vec![NormalizedIndex {
                        name: "idx_orders_user_id".to_string(),
                        columns: vec!["user_id".to_string()],
                        column_prefixes: vec![None],
                        unique: false,
                        visible: None, spatial: false,
                    }],
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "fk_orders_users".to_string(),
                        columns: vec!["user_id".to_string()],
                        referenced_table: "users".to_string(),
                        referenced_columns: vec!["id".to_string()],
                        on_delete: None,
                        on_update: None,
                    }],
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "cr_industry_map_ibfk_1".to_string(),
                        columns: vec!["brief_slug".to_string()],
                        referenced_table: "cr_industry_briefs".to_string(),
                        referenced_columns: vec!["slug".to_string()],
                        on_delete: None,
                        on_update: None,
                    }],
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: vec![NormalizedIndex {
                        name: "ux_cr_industry_briefs_slug".to_string(),
                        columns: vec!["slug".to_string()],
                        column_prefixes: vec![None],
                        unique: true,
                        visible: None, spatial: false,
                    }],
                    foreign_keys: Vec::new(),
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
                },
            ],
        };

        let create = generate_schema_ddl(&schema, "mysql", "mysql").unwrap();
        assert!(create.iter().any(|sql| sql.contains("UNIQUE KEY `ux_cr_industry_briefs_slug`")));
        let ddl = generate_post_data_ddl(&schema, "mysql");
        assert!(!ddl.iter().any(|sql| sql.contains("ux_cr_industry_briefs_slug")));
        assert!(ddl.iter().any(|sql| sql.contains("cr_industry_map_ibfk_1")));

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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_orders_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    column_prefixes: vec![None],
                    unique: false,
                    visible: None, spatial: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "is_read_comment_ibfk_1".to_string(),
                        columns: vec!["user_id".to_string()],
                        referenced_table: "users".to_string(),
                        referenced_columns: vec!["id".to_string()],
                        on_delete: None,
                        on_update: None,
                    }],
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_orders_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    column_prefixes: vec![None],
                    unique: false,
                    visible: None, spatial: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_orders_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    column_prefixes: vec![None],
                    unique: false,
                    visible: None, spatial: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    }],
                    indexes: vec![NormalizedIndex {
                        name: "idx_df_evaluation_results_audit_category_code".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        column_prefixes: vec![None],
                        unique: false,
                        visible: None, spatial: false,
                    }],
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "df_evaluation_results_ibfk_3".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        referenced_table: "audit_category".to_string(),
                        referenced_columns: vec!["code".to_string()],
                        on_delete: None,
                        on_update: None,
                    }],
                    table_collation: None,
                    auto_increment: None,
                    comment: None,
                    checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: vec![NormalizedIndex {
                    name: "idx_login_attempts_user_id".to_string(),
                    columns: vec!["user_id".to_string()],
                    column_prefixes: vec![None],
                    unique: false,
                    visible: None, spatial: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
            "SELECT setval(pg_get_serial_sequence(E'\"users\"', E'id'), GREATEST(COALESCE((SELECT MAX(\"id\") FROM \"users\"), 0) + 1, 1), false);"
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
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    },
                    NormalizedColumn {
                        name: "enabled".to_string(),
                        type_name: "tinyint(1)".to_string(),
                        default_value: Some("1".to_string()),
                        nullable: false,
                        primary_key: false,
                        unique: false,
                        comment: None,
                        default_is_expression: false,
                        on_update: None,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                },
                NormalizedColumn {
                    name: "payload".to_string(),
                    type_name: "blob".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
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
            "TIME(6)"
        );
        assert_eq!(
            map_type("postgresql", "mysql", "timestamp without time zone"),
            "DATETIME(6)"
        );
        assert_eq!(
            map_type("postgresql", "mysql", "timestamp with time zone"),
            "DATETIME(6)"
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
        // JSON 내부 백슬래시 이스케이프가 mysql 리터럴에서 이중 백슬래시로 보존되는지 검증.
        assert_eq!(
            ai_phase1_cache_json_insert_sql(),
            AI_PHASE1_CACHE_JSON_INSERT_SQL
        );
    }

    #[test]
    fn mysql_json_literal_uses_utf8mb4_introducer_for_unicode_json_text() {
        // 유니코드 JSON 텍스트에 _utf8mb4 도입자가 붙는지 검증.
        assert_eq!(
            ai_phase1_cache_json_insert_sql(),
            AI_PHASE1_CACHE_JSON_INSERT_SQL
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
                comment: None,
                default_is_expression: false,
                on_update: None,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
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
                comment: None,
                default_is_expression: false,
                on_update: None,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
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
        assert!(inspect_foreign_keys_sql("postgresql").contains("c.contype = 'f'"));
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
            comment: None,
            default_is_expression: false,
            on_update: None,
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
            comment: None,
            default_is_expression: false,
            on_update: None,
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
    fn generate_table_ddl_preserves_mysql_fractional_current_timestamp_default() {
        let table = NormalizedTable {
            name: "dm_data_phase".to_string(),
            columns: vec![NormalizedColumn {
                name: "created_at".to_string(),
                type_name: "timestamp(6)".to_string(),
                default_value: Some("CURRENT_TIMESTAMP(6)".to_string()),
                nullable: false,
                primary_key: false,
                unique: false,
                comment: None,
                default_is_expression: false,
                on_update: None,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
        };

        let ddl = generate_table_ddl(&table, "mysql", "mysql").expect("valid MySQL DDL");

        assert!(
            ddl.contains("`created_at` timestamp(6) DEFAULT CURRENT_TIMESTAMP(6) NOT NULL"),
            "fractional temporal default must remain an expression: {ddl}"
        );
        assert!(
            !ddl.contains("'CURRENT_TIMESTAMP(6)'"),
            "fractional temporal default must not become a string literal: {ddl}"
        );
    }

    #[test]
    fn map_default_literal_keeps_unsafe_temporal_expressions_quoted() {
        for value in [
            "CURRENT_TIMESTAMP(7)",
            "CURRENT_TIMESTAMP(06)",
            "CURRENT_TIMESTAMP(-1)",
            "CURRENT_TIMESTAMP(6); DROP TABLE users",
        ] {
            let mapped = map_default_literal("mysql", value, "timestamp(6)");
            assert!(
                mapped.starts_with('\'') && mapped.ends_with('\''),
                "unsafe temporal expression must remain a string literal: {value:?} -> {mapped}"
            );
        }
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
                "CASCADE".to_string(),
                "SET NULL".to_string(),
            ),
            (
                "fk_order_items_order".to_string(),
                "order_id".to_string(),
                "orders".to_string(),
                "id".to_string(),
                "CASCADE".to_string(),
                "SET NULL".to_string(),
            ),
        ]).unwrap();

        assert_eq!(keys.len(), 1);
        assert_eq!(keys[0].columns, vec!["tenant_id", "order_id"]);
        assert_eq!(keys[0].referenced_columns, vec!["tenant_id", "id"]);
    }

    #[test]
    fn foreign_key_actions_survive_manifest_and_ddl() {
        let fk: NormalizedForeignKey = serde_json::from_value(json!({
            "name": "fk_child_parent", "columns": ["parent_id"],
            "referenced_table": "parent", "referenced_columns": ["id"],
            "on_delete": "CASCADE", "on_update": "SET NULL"
        })).unwrap();
        let schema = NormalizedSchema { tables: vec![crate::adapters::test_support::empty_table("child", vec![fk])] };
        for engine in ["mysql", "postgresql"] {
            let ddl = generate_post_data_ddl(&schema, engine);
            assert!(ddl.last().unwrap().contains("ON DELETE CASCADE ON UPDATE SET NULL"));
        }
        let invalid = serde_json::from_value::<NormalizedForeignKey>(json!({
            "name": "fk", "referenced_table": "parent", "on_delete": "CASCADE; DROP TABLE parent"
        }));
        assert!(invalid.is_err());
        let legacy: NormalizedForeignKey = serde_json::from_value(json!({
            "name": "fk", "referenced_table": "parent"
        })).unwrap();
        assert_eq!(legacy.on_delete, None);
        assert_eq!(legacy.on_update, None);
        for action in ["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"] {
            let parsed: ForeignKeyAction = serde_json::from_value(json!(action)).unwrap();
            assert_eq!(parsed.as_sql(), action);
            assert_eq!(serde_json::to_value(parsed).unwrap(), json!(action));
        }
    }

    #[test]
    fn mysql_set_default_foreign_keys_are_blocked_before_planning() {
        for field in ["on_delete", "on_update"] {
            let mut schema = schema();
            let mut fk = json!({"name": "fk_default", "columns": ["id"],
                "referenced_table": "users", "referenced_columns": ["id"]});
            fk[field] = json!("SET DEFAULT");
            schema.tables[0].foreign_keys.push(serde_json::from_value(fk).unwrap());
            let result = generate_schema_ddl(&schema, "postgresql", "mysql");
            assert!(result.is_err(), "accepted {field} SET DEFAULT for MySQL");
            assert!(result.unwrap_err().contains("SET DEFAULT"));
            assert!(generate_schema_ddl(&schema, "mysql", "postgresql").is_ok());
            let payload = json!({"source_engine": "postgresql", "target_engine": "mysql", "schema": schema});
            assert!(preflight_issues(&payload).iter().any(|issue| issue.blocking && issue.message.contains("SET DEFAULT")));
        }
    }

    #[test]
    fn group_indexes_preserves_column_order_and_unique_flag() {
        let indexes = group_indexes(vec![
            (
                "idx_users_name_email".to_string(),
                "name".to_string(),
                Some(10),
                false,
            ),
            (
                "idx_users_name_email".to_string(),
                "email".to_string(),
                None,
                false,
            ),
            ("ux_users_slug".to_string(), "slug".to_string(), None, true),
        ]);

        assert_eq!(indexes.len(), 2);
        assert_eq!(indexes[0].columns, vec!["name", "email"]);
        // prefix 길이(SUB_PART)가 컬럼과 병렬로 보존되어야 한다.
        assert_eq!(indexes[0].column_prefixes, vec![Some(10), None]);
        assert!(!indexes[0].unique);
        assert!(indexes[1].unique);
    }

    #[test]
    fn post_load_ddl_preserves_mysql_index_prefix_length() {
        // filename(255) 같은 prefix 인덱스가 full 컬럼이 아니라 col(255)로 재생성되어야 한다.
        // (varchar(2083) utf8mb4를 full로 인덱싱하면 ERROR 1071: max key length 3072 초과)
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "attachment_storage".to_string(),
                columns: Vec::new(),
                indexes: vec![NormalizedIndex {
                    name: "filename".to_string(),
                    columns: vec!["filename".to_string()],
                    column_prefixes: vec![Some(255)],
                    unique: false,
                    visible: None, spatial: false,
                }],
                foreign_keys: Vec::new(),
                table_collation: None,
                auto_increment: None,
                comment: None,
                checks: Vec::new(),
            }],
        };
        let ddl = generate_post_data_ddl(&schema, "mysql");
        assert!(
            ddl.iter()
                .any(|s| s == "CREATE INDEX `filename` ON `attachment_storage` (`filename`(255));"),
            "prefix index must be recreated as col(255), got: {ddl:?}"
        );
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

#[cfg(test)]
mod binary_keyset_tests {
    use super::*;

    fn column(name: &str, type_name: &str) -> NormalizedColumn {
        NormalizedColumn {
            name: name.to_string(),
            type_name: type_name.to_string(),
            default_value: None,
            nullable: false,
            primary_key: true,
            unique: false,
            comment: None,
            default_is_expression: false,
            on_update: None,
        }
    }

    fn table(columns: Vec<NormalizedColumn>) -> NormalizedTable {
        NormalizedTable {
            name: "events".to_string(),
            columns,
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
        }
    }

    fn after_key(engine: &str, table: &NormalizedTable, keys: &[&str], values: &[&str]) -> String {
        let keys: Vec<String> = keys.iter().map(|key| key.to_string()).collect();
        let values: Vec<String> = values.iter().map(|value| value.to_string()).collect();
        select_chunk_text_after_key_sql(engine, table, &keys, Some(&values), 10)
    }

    #[test]
    fn binary_key_cursor_decodes_the_hex_token_back_to_bytes() {
        let mysql_table = table(vec![column("id", "binary(16)")]);
        let sql = after_key("mysql", &mysql_table, &["id"], &["A1B2C3D4E5F60718293A4B5C6D7E8F90"]);
        assert!(sql.contains("`events`.`id` > X'A1B2C3D4E5F60718293A4B5C6D7E8F90'"), "{sql}");
        assert!(!sql.contains("> 'A1B2"), "a text literal compares ASCII digits with raw bytes: {sql}");

        let varbinary = table(vec![column("id", "varbinary(20)")]);
        assert!(after_key("mysql", &varbinary, &["id"], &["00FF"]).contains("> X'00FF'"));

        let pg_table = table(vec![column("id", "bytea")]);
        let sql = after_key("postgresql", &pg_table, &["id"], &["a1b2c3"]);
        assert!(sql.contains("\"events\".\"id\" > decode('a1b2c3', 'hex')"), "{sql}");
    }

    #[test]
    fn composite_keys_decode_only_the_binary_part() {
        let mixed = table(vec![column("grp", "int"), column("id", "binary(16)")]);
        let sql = after_key("mysql", &mixed, &["grp", "id"], &["3", "FF00"]);
        assert!(sql.contains("(`events`.`grp` > '3') OR (`events`.`grp` = '3' AND `events`.`id` > X'FF00')"), "{sql}");
        let sql = after_key("postgresql", &table(vec![column("id", "bytea"), column("seq", "bigint")]), &["id", "seq"], &["ab", "9"]);
        assert!(sql.contains("(\"events\".\"id\" > decode('ab', 'hex')) OR (\"events\".\"id\" = decode('ab', 'hex') AND \"events\".\"seq\" > '9')"), "{sql}");
    }

    #[test]
    fn non_binary_key_cursor_is_unchanged() {
        let ints = table(vec![column("id", "bigint")]);
        assert!(after_key("mysql", &ints, &["id"], &["42"]).contains("`events`.`id` > '42'"));
        let text = table(vec![column("code", "varchar(20)")]);
        assert!(after_key("postgresql", &text, &["code"], &["it's"]).contains("\"events\".\"code\" > 'it''s'"));
    }

    #[test]
    fn mysql_text_keys_use_an_escape_free_literal() {
        // A quoted literal would read `a\b` as `a` + backspace (or not close at a trailing `\`).
        let text = table(vec![column("code", "varchar(20)")]);
        let sql = after_key("mysql", &text, &["code"], &["a\\b"]);
        assert!(sql.contains("`events`.`code` > _utf8mb4 X'615C62'"), "{sql}");
        let latin = table(vec![column("code", "varchar(20) character set latin1")]);
        assert!(after_key("mysql", &latin, &["code"], &["x\\"]).contains("> _utf8mb4 X'785C'"));
    }

    #[test]
    fn mysql_enum_and_set_keys_compare_by_index_like_order_by() {
        let enums = table(vec![column("k", "enum('zulu','alpha','it''s')")]);
        let sql = after_key("mysql", &enums, &["k"], &["zulu"]);
        assert!(sql.contains("`events`.`k` IN (_utf8mb4 X'616C706861', _utf8mb4 X'69742773')"), "{sql}");
        assert!(after_key("mysql", &enums, &["k"], &["it's"]).contains("(FALSE)"));
        let composite = table(vec![column("k", "enum('zulu','alpha')"), column("id", "bigint")]);
        let sql = after_key("mysql", &composite, &["k", "id"], &["zulu", "7"]);
        assert!(sql.contains("`events`.`k` = _utf8mb4 X'7A756C75' AND `events`.`id` > '7'"), "{sql}");
        let sets = table(vec![column("k", "set('a','b','c')")]);
        assert!(after_key("mysql", &sets, &["k"], &["a,c"]).contains("(`events`.`k`+0) > 5"));
        // An unknown label falls back to the plain literal instead of guessing an index.
        assert!(after_key("mysql", &enums, &["k"], &["nope"]).contains("`events`.`k` > 'nope'"));
    }

    #[test]
    fn mysql_float_keys_are_read_as_exact_doubles() {
        let floats = table(vec![column("k", "float")]);
        let sql = after_key("mysql", &floats, &["k"], &["0.10000000149011612"]);
        assert!(sql.contains("(`k` + 0e0) AS `k`"), "{sql}");
        assert!(sql.contains("`events`.`k` > '0.10000000149011612'"), "{sql}");
        let pg = table(vec![column("k", "real")]);
        assert!(after_key("postgresql", &pg, &["k"], &["0.1"]).contains("\"events\".\"k\" > '0.1'"));
    }

    #[test]
    fn mysql_bit_columns_are_read_as_padded_digits_and_written_as_bit_literals() {
        let bits = table(vec![column("k", "bit(8)"), column("flag", "bit")]);
        let sql = after_key("mysql", &bits, &["k"], &["10000000"]);
        assert!(sql.contains("LPAD(BIN(`k`), 8, '0') AS `k`"), "{sql}");
        assert!(sql.contains("LPAD(BIN(`flag`), 1, '0') AS `flag`"), "{sql}");
        assert!(sql.contains("`events`.`k` > 128"), "{sql}");
        assert_eq!(sql_literal_for_column("mysql", "bit(8)", &Value::String("10000000".into())), "b'10000000'");
        assert_eq!(sql_literal_for_column("postgresql", "bit(8)", &Value::String("10000000".into())), "'10000000'");
        assert!(has_binary_columns(&bits));
        assert_eq!(mysql_bit_width("bit"), Some(1));
        assert_eq!(mysql_bit_width("bit(64)"), Some(64));
        assert_eq!(mysql_bit_width("bit varying(8)"), None);
        assert_eq!(mysql_bit_width("bit(8)[]"), None);
        assert_eq!(map_type("postgresql", "mysql", "bit(8)[]"), "LONGTEXT");
        assert_eq!(map_default_literal("mysql", "'00000101'::\"bit\"", "bit(8)"), "b'00000101'");
        assert_eq!(map_default_literal("postgresql", "b'101'", "bit(8)"), "B'00000101'");
        assert_eq!(map_default_literal("postgresql", "b'0'", "bit(1)"), "B'0'");
        assert!(legacy_projected_text_columns_sql("mysql", &bits).contains("`k`") && !legacy_projected_text_columns_sql("mysql", &bits).contains("BIN("));
        assert_eq!(map_type("mysql", "postgresql", "bit(8)"), "BIT(8)");
        assert_eq!(map_type("postgresql", "mysql", "bit(8)"), "BIT(8)");
    }

    #[test]
    fn legacy_bit_cells_are_recovered_unless_already_corrupted() {
        assert_eq!(legacy_mysql_bit_digits("\u{1}", 1).unwrap(), "1");
        assert_eq!(legacy_mysql_bit_digits("\u{0}", 1).unwrap(), "0");
        assert_eq!(legacy_mysql_bit_digits("\u{0}A", 16).unwrap(), "0000000001000001");
        assert!(legacy_mysql_bit_digits("\u{fffd}", 8).is_err());
        assert!(legacy_mysql_bit_digits("\u{2}", 1).is_err());
    }

    #[test]
    fn cross_engine_types_keep_range_precision_and_keyability() {
        for (mysql, pg) in [
            ("tinyint(4)", "SMALLINT"), ("tinyint unsigned", "SMALLINT"), ("tinyint(1)", "BOOLEAN"),
            ("smallint", "SMALLINT"), ("smallint unsigned", "INTEGER"), ("mediumint unsigned", "INTEGER"),
            ("int unsigned", "BIGINT"), ("bigint unsigned", "NUMERIC(20,0)"), ("bigint", "BIGINT"),
            ("float", "DOUBLE PRECISION"), ("double", "DOUBLE PRECISION"), ("year", "SMALLINT"),
            ("decimal(10,2) unsigned zerofill", "NUMERIC(10,2)"), ("char(36)", "VARCHAR(36)"),
            ("enum('a','b')", "TEXT"),
        ] {
            assert_eq!(map_type("mysql", "postgresql", mysql), pg, "{mysql}");
        }
        for (pg, mysql) in [
            ("smallint", "SMALLINT"), ("real", "DOUBLE"), ("double precision", "DOUBLE"), ("uuid", "CHAR(36)"),
            ("text", "LONGTEXT"), ("character varying", "LONGTEXT"), ("character(3)", "CHAR(3)"),
            ("numeric", "DECIMAL(65,30)"), ("numeric(12,4)", "DECIMAL(12,4)"), ("integer[]", "LONGTEXT"),
            ("character varying(255)[]", "LONGTEXT"), ("inet", "VARCHAR(43)"), ("bytea", "LONGBLOB"),
        ] {
            assert_eq!(map_type("postgresql", "mysql", pg), mysql, "{pg}");
        }
        // Key columns cannot be TEXT/BLOB in MySQL (ERROR 1170).
        let mut keyed = table(vec![column("code", "text"), column("blob_key", "bytea")]);
        keyed.columns[1].primary_key = false;
        keyed.indexes.push(NormalizedIndex { name: "uq_blob".into(), columns: vec!["blob_key".into()], column_prefixes: vec![None], unique: true, visible: None, spatial: false });
        let ddl = generate_table_ddl(&keyed, "postgresql", "mysql").unwrap();
        assert!(ddl.contains("`code` VARCHAR(255) NOT NULL"), "{ddl}");
        assert!(ddl.contains("`blob_key` VARBINARY(255)"), "{ddl}");
        // A four-column text key must fit 3072 bytes (ERROR 1071 otherwise).
        let wide = table(vec![column("a", "text"), column("b", "text"), column("c", "text"), column("d", "text")]);
        let ddl = generate_table_ddl(&wide, "postgresql", "mysql").unwrap();
        assert_eq!(ddl.matches("VARCHAR(192)").count(), 4, "{ddl}");
        // PostgreSQL identity columns must be integers.
        let ids = table(vec![column("id", "bigint unsigned auto_increment")]);
        let ddl = generate_table_ddl(&ids, "mysql", "postgresql").unwrap();
        assert!(ddl.contains("\"id\" BIGINT GENERATED BY DEFAULT AS IDENTITY"), "{ddl}");
        assert_eq!(map_type("mysql", "postgresql", "char(0)"), "VARCHAR(1)");
        assert_eq!(copy_csv_field_for_column("postgresql", "tinyint(1)", &Value::String("2".into())), "\"true\"");
        assert_eq!(sql_literal_for_column("postgresql", "tinyint(1)", &Value::String("-1".into())), "TRUE");
    }

    #[test]
    fn mysql_spatial_values_round_trip_as_hex_of_the_internal_format() {
        let mut geo = table(vec![column("id", "int"), column("pos", "point srid 4326")]);
        geo.columns[1].primary_key = false;
        assert!(projected_text_columns_sql("mysql", &geo).contains("HEX(`pos`) AS `pos`"));
        // Safe-promotion digests read geometry the same way; PostgreSQL point stays text.
        assert!(legacy_projected_text_columns_sql("mysql", &geo).contains("HEX(`pos`) AS `pos`"));
        assert!(projected_text_columns_sql("postgresql", &geo).contains("\"pos\"::text"));
        let hex = Value::String("E6100000010100000000000000000000000000000000000000".into());
        assert_eq!(sql_literal_for_column("mysql", "point srid 4326", &hex), "X'E6100000010100000000000000000000000000000000000000'");
        // PostgreSQL point text into a MySQL text column stays a string.
        assert_eq!(sql_literal_for_column("mysql", "point", &Value::String("(1,2)".into())), "'(1,2)'");
        assert!(has_binary_columns(&geo));
        assert!(is_safe_column_type("point srid 4326") && is_safe_column_type("geometry"));
        assert!(!is_safe_column_type("point srid") && !is_safe_column_type("point srid x"));
        let ddl = generate_table_ddl(&geo, "mysql", "mysql").unwrap();
        assert!(ddl.contains("`pos` point srid 4326 NOT NULL"), "{ddl}");
        geo.indexes.push(NormalizedIndex { name: "sp_pos".into(), columns: vec!["pos".into()], column_prefixes: vec![Some(32)], unique: false, visible: None, spatial: true });
        let post = generate_post_data_ddl(&NormalizedSchema { tables: vec![geo] }, "mysql");
        assert!(post.iter().any(|sql| sql == "CREATE SPATIAL INDEX `sp_pos` ON `events` (`pos`);"), "{post:?}");
    }

    #[test]
    fn timestamptz_values_lose_the_utc_offset_for_mysql_datetime() {
        let utc = Value::String("2026-11-01 05:30:00.123+00".into());
        assert_eq!(sql_literal_for_column("mysql", "timestamp with time zone", &utc), "'2026-11-01 05:30:00.123'");
        assert_eq!(sql_literal_for_column("postgresql", "timestamp with time zone", &utc), "'2026-11-01 05:30:00.123+00'");
    }

    #[test]
    fn temporal_values_the_other_engine_cannot_store_are_named() {
        for (engine, ty, text, refused) in [
            ("mysql", "date", "0000-00-00", true), ("mysql", "datetime(6)", "2024-00-15 10:00:00", true),
            ("mysql", "timestamp", "2024-01-15 00:00:00", false), ("mysql", "date", "0000-01-01", true),
            ("mysql", "time", "838:59:59", true), ("mysql", "time(3)", "-01:00:00.000", true),
            ("mysql", "time", "24:00:00", false), ("mysql", "time", "24:00:00.5", true), ("mysql", "time", "23:59:59", false),
            ("postgresql", "date", "infinity", true), ("postgresql", "timestamp(3) with time zone", "-infinity", true),
            ("postgresql", "date", "0044-03-15 BC", true), ("postgresql", "timestamp without time zone", "12000-01-01 00:00:00", true),
            ("postgresql", "date", "2024-02-29", false), ("postgresql", "time without time zone", "24:00:00", false),
            ("mysql", "varchar(10)", "0000-00-00", false), ("mysql", "date", "2024-02-30", true),
            ("mysql", "date", "2024-02-29", false), ("mysql", "date", "2023-02-29", true),
            ("postgresql", "timestamp without time zone[]", "{infinity}", false),
        ] {
            assert_eq!(temporal_value_problem(engine, ty, text).is_some(), refused, "{engine} {ty} {text}");
        }
        assert!(temporal_scan_condition("mysql", "datetime(3)", "`d`").unwrap().contains("MONTH(`d`) = 0"));
        assert!(temporal_scan_condition("postgresql", "timestamp(3) with time zone", "\"d\"").unwrap().contains("isfinite"));
        assert!(temporal_scan_condition("postgresql", "time without time zone", "\"t\"").is_none());
        assert!(temporal_scan_condition("postgresql", "timestamp(3) with time zone[]", "\"a\"").is_none());
        assert_eq!(temporal_type_problem("postgresql", "time with time zone"), Some((true, "time with time zone has no MySQL type that keeps the offset")));
        assert!(temporal_type_problem("postgresql", "interval").is_some_and(|(blocking, _)| !blocking));
        assert!(temporal_type_problem("postgresql", "timestamp with time zone").is_none());
    }

    #[test]
    fn a_key_column_missing_from_the_table_falls_back_to_a_text_literal() {
        let ints = table(vec![column("id", "bigint")]);
        assert!(after_key("mysql", &ints, &["other"], &["1"]).contains("`events`.`other` > '1'"));
    }

    #[test]
    fn key_lookups_use_the_literal_the_copy_stored() {
        let keys = vec![vec!["0101".to_string()]];
        // PostgreSQL bit varying is text in MySQL: compare as text, not as the integer 5.
        let varbit = table(vec![column("k", "bit varying(8)")]);
        assert!(select_chunk_text_by_keys_sql("mysql", &varbit, &["k".to_string()], &keys).contains("`events`.`k` = "));
        assert!(!select_chunk_text_by_keys_sql("mysql", &varbit, &["k".to_string()], &keys).contains("= 5"));
        let bit = table(vec![column("k", "bit(8)")]);
        assert!(select_chunk_text_by_keys_sql("mysql", &bit, &["k".to_string()], &keys).contains("`events`.`k` = 5"));
        // MySQL tinyint(1) 2 was copied to PostgreSQL BOOLEAN as TRUE.
        let flag = table(vec![column("k", "tinyint(1)")]);
        assert!(select_chunk_text_by_keys_sql("postgresql", &flag, &["k".to_string()], &[vec!["2".to_string()]]).contains("= TRUE"));
        assert!(select_chunk_text_by_keys_sql("mysql", &flag, &["k".to_string()], &[]).ends_with("WHERE 1 = 0"));
    }
}
