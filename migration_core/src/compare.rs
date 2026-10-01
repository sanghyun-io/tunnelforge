use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::Write;

use crate::*;

pub(crate) fn normalize_rows_for_table(table: &NormalizedTable, rows: &[Value]) -> Vec<Value> {
    rows.iter()
        .map(|row| normalize_row_for_table(table, row))
        .collect()
}

pub(crate) fn normalize_row_for_table(table: &NormalizedTable, row: &Value) -> Value {
    match row {
        Value::Object(object) => {
            let mut normalized = Map::new();
            for column in &table.columns {
                normalized.insert(
                    column.name.clone(),
                    normalize_value_for_type(&column.type_name, object.get(&column.name)),
                );
            }
            Value::Object(normalized)
        }
        _ => row.clone(),
    }
}

pub fn normalize_value_for_type(source_type: &str, value: Option<&Value>) -> Value {
    let Some(value) = value else {
        return Value::Null;
    };
    if value.is_null() {
        return Value::Null;
    }
    let source_type = source_type.to_ascii_lowercase();
    if source_type == "boolean" || source_type == "bool" || source_type.starts_with("tinyint(1)") {
        let text = match value {
            Value::Bool(value) => {
                if *value {
                    "true".to_string()
                } else {
                    "false".to_string()
                }
            }
            Value::Number(value) => value.to_string(),
            Value::String(value) => value.trim().to_ascii_lowercase(),
            _ => value.to_string().to_ascii_lowercase(),
        };
        if matches!(text.as_str(), "1" | "true" | "t" | "yes" | "on") {
            return Value::Bool(true);
        }
        if matches!(text.as_str(), "0" | "false" | "f" | "no" | "off") {
            return Value::Bool(false);
        }
    }
    if is_binary_type(&source_type) {
        if let Value::String(text) = value {
            return Value::String(text.to_ascii_lowercase());
        }
    }
    if is_decimal_type(&source_type) {
        if let Some(text) = scalar_text(value) {
            return Value::String(normalize_decimal_text(&text));
        }
    }
    if is_date_type(&source_type) {
        if let Some(text) = scalar_text(value) {
            return Value::String(normalize_date_text(&text));
        }
    }
    if is_time_type(&source_type) {
        if let Some(text) = scalar_text(value) {
            return Value::String(normalize_time_text(&text));
        }
    }
    if is_timestamp_type(&source_type) {
        if let Some(text) = scalar_text(value) {
            return Value::String(normalize_timestamp_text(&text));
        }
    }
    if let Value::String(text) = value {
        if text.contains('\0') && !is_binary_type(&source_type) {
            return Value::String(sanitize_postgresql_text(text));
        }
    }
    value.clone()
}

fn scalar_text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        Value::Null => None,
        _ => None,
    }
}

fn normalize_date_text(text: &str) -> String {
    let text = text.trim();
    if text.len() >= 10
        && text.as_bytes().get(4) == Some(&b'-')
        && text.as_bytes().get(7) == Some(&b'-')
    {
        text[..10].to_string()
    } else {
        text.to_string()
    }
}

fn normalize_time_text(text: &str) -> String {
    trim_fractional_seconds(text.trim())
}

fn normalize_timestamp_text(text: &str) -> String {
    let text = text.trim().replace('T', " ");
    let text = trim_fractional_seconds(&text);
    strip_zero_utc_suffix(&text).to_string()
}

fn trim_fractional_seconds(text: &str) -> String {
    let Some(dot_index) = text.find('.') else {
        return text.to_string();
    };
    let digit_end = text[dot_index + 1..]
        .find(|character: char| !character.is_ascii_digit())
        .map(|offset| dot_index + 1 + offset)
        .unwrap_or(text.len());
    let fraction = &text[dot_index + 1..digit_end];
    let trimmed_fraction = fraction.trim_end_matches('0');
    let suffix = &text[digit_end..];
    if trimmed_fraction.is_empty() {
        format!("{}{}", &text[..dot_index], suffix)
    } else {
        format!("{}.{}{}", &text[..dot_index], trimmed_fraction, suffix)
    }
}

fn strip_zero_utc_suffix(text: &str) -> &str {
    for suffix in ["+00:00", "+00", "Z", "z"] {
        if let Some(stripped) = text.strip_suffix(suffix) {
            return stripped.trim_end();
        }
    }
    text
}

fn normalize_decimal_text(text: &str) -> String {
    let text = text.trim();
    if text.is_empty() || text.contains('e') || text.contains('E') {
        return text.to_ascii_lowercase();
    }

    let (negative, unsigned) = if let Some(rest) = text.strip_prefix('-') {
        (true, rest)
    } else if let Some(rest) = text.strip_prefix('+') {
        (false, rest)
    } else {
        (false, text)
    };

    let (integer, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    let integer = integer.trim_start_matches('0');
    let integer = if integer.is_empty() { "0" } else { integer };
    let fraction = fraction.trim_end_matches('0');

    let normalized = if fraction.is_empty() {
        integer.to_string()
    } else {
        format!("{integer}.{fraction}")
    };

    if normalized == "0" {
        normalized
    } else if negative {
        format!("-{normalized}")
    } else {
        normalized
    }
}

pub(crate) fn key_columns(table: &NormalizedTable) -> Vec<String> {
    let primary_keys: Vec<String> = table
        .columns
        .iter()
        .filter(|column| column.primary_key)
        .map(|column| column.name.clone())
        .collect();
    if !primary_keys.is_empty() {
        return primary_keys;
    }
    table
        .columns
        .iter()
        .filter(|column| column.unique)
        .map(|column| column.name.clone())
        .collect()
}

pub(crate) fn column_names(table: &NormalizedTable) -> Vec<String> {
    table
        .columns
        .iter()
        .map(|column| column.name.clone())
        .collect()
}

pub(crate) fn row_key_token(row: &Value, key_columns: &[String]) -> Option<String> {
    let object = row.as_object()?;
    let values: Option<Vec<String>> = key_columns
        .iter()
        .map(|column| object.get(column).and_then(scalar_text))
        .collect();
    values.map(|values| serde_json::to_string(&values).unwrap_or_default())
}

/// Defensive check for keyset pagination: the last key of a page must be strictly greater than the previous
/// page's last key. A cursor that repeats or rewinds (the failure mode of a mis-typed key comparison) would
/// otherwise silently duplicate and drop rows, because the callers stop on a row count, not on the key.
/// Integer and binary key parts are ordered exactly; other types (text collations, decimals, uuid) cannot be
/// ordered here, so for them only an unchanged key is rejected. Returns the new cursor token.
pub(crate) fn advance_keyset_cursor(
    table: &NormalizedTable,
    key_columns: &[String],
    previous: Option<&str>,
    next: Option<String>,
) -> Result<String, String> {
    let next = next.ok_or_else(|| format!("keyset cursor for {} could not read the key of the last row", table.name))?;
    let Some(previous) = previous else { return Ok(next) };
    let (Some(before), Some(after)) = (decode_key_token(previous), decode_key_token(&next)) else {
        return Ok(next);
    };
    for (index, column) in key_columns.iter().enumerate() {
        let (Some(left), Some(right)) = (before.get(index), after.get(index)) else { break };
        if left == right {
            continue;
        }
        let type_name = table
            .columns
            .iter()
            .find(|candidate| &candidate.name == column)
            .map(|candidate| candidate.type_name.to_ascii_lowercase())
            .unwrap_or_default();
        let ordering = if is_binary_type(&type_name) {
            hex_bytes(left).zip(hex_bytes(right)).map(|(a, b)| a.cmp(&b))
        } else if type_name.contains("int") {
            left.parse::<i128>().ok().zip(right.parse::<i128>().ok()).map(|(a, b)| a.cmp(&b))
        } else {
            None
        };
        return match ordering {
            Some(std::cmp::Ordering::Greater) => Err(keyset_regression_message(table)),
            _ => Ok(next),
        };
    }
    Err(keyset_regression_message(table))
}

fn keyset_regression_message(table: &NormalizedTable) -> String {
    format!(
        "keyset cursor for {} did not advance (the next page's last key is not greater than the previous one); stopping to avoid duplicated or skipped rows",
        table.name
    )
}

fn hex_bytes(text: &str) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    (0..text.len()).step_by(2).map(|index| u8::from_str_radix(text.get(index..index + 2)?, 16).ok()).collect()
}

pub(crate) fn keyset_start_index(rows: &[Value], key_columns: &[String], last_key: Option<&str>) -> usize {
    let Some(last_key) = last_key else {
        return 0;
    };
    for (index, row) in rows.iter().enumerate() {
        let Some(token) = row_key_token(row, key_columns) else {
            continue;
        };
        if token == last_key {
            return index + 1;
        }
    }
    for (index, row) in rows.iter().enumerate() {
        let Some(token) = row_key_token(row, key_columns) else {
            continue;
        };
        if token.as_str() > last_key {
            return index;
        }
    }
    rows.len()
}

pub(crate) fn decode_key_token(token: &str) -> Option<Vec<String>> {
    if let Ok(values) = serde_json::from_str::<Vec<String>>(token) {
        return Some(values);
    }
    Some(vec![token.to_string()])
}

pub(crate) fn mysql_row_to_json(columns: &[String], row: mysql::Row) -> Value {
    let values = row.unwrap();
    let mut object = Map::new();
    for (index, column) in columns.iter().enumerate() {
        let value = values.get(index).cloned().unwrap_or(mysql::Value::NULL);
        object.insert(column.clone(), mysql_value_to_json(value));
    }
    Value::Object(object)
}

fn mysql_value_to_json(value: mysql::Value) -> Value {
    match value {
        mysql::Value::NULL => Value::Null,
        mysql::Value::Bytes(value) => Value::String(String::from_utf8_lossy(&value).to_string()),
        mysql::Value::Int(value) => json!(value.to_string()),
        mysql::Value::UInt(value) => json!(value.to_string()),
        mysql::Value::Float(value) => json!(value.to_string()),
        mysql::Value::Double(value) => json!(value.to_string()),
        mysql::Value::Date(year, month, day, hour, minute, second, micros) => {
            if hour == 0 && minute == 0 && second == 0 && micros == 0 {
                json!(format!("{year:04}-{month:02}-{day:02}"))
            } else {
                json!(format!(
                    "{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{:06}",
                    micros
                ))
            }
        }
        mysql::Value::Time(negative, days, hours, minutes, seconds, micros) => {
            let sign = if negative { "-" } else { "" };
            json!(format!(
                "{sign}{days} {hours:02}:{minutes:02}:{seconds:02}.{:06}",
                micros
            ))
        }
    }
}

pub(crate) fn write_mysql_text_row_tsv<W: Write>(writer: &mut W, row: mysql::Row) -> Result<(), String> {
    let values = row.unwrap();
    for (index, value) in values.into_iter().enumerate() {
        if index > 0 {
            writer
                .write_all(b"\t")
                .map_err(|err| format!("failed to write dump row: {err}"))?;
        }
        let field = mysql_value_to_tsv_field(value);
        writer
            .write_all(field.as_bytes())
            .map_err(|err| format!("failed to write dump row: {err}"))?;
    }
    writer
        .write_all(b"\n")
        .map_err(|err| format!("failed to write dump row: {err}"))
}

fn mysql_value_to_tsv_field(value: mysql::Value) -> String {
    match value {
        mysql::Value::NULL => "\\N".to_string(),
        mysql::Value::Bytes(value) => escape_tsv_text(&String::from_utf8_lossy(&value)),
        mysql::Value::Int(value) => value.to_string(),
        mysql::Value::UInt(value) => value.to_string(),
        mysql::Value::Float(value) => value.to_string(),
        mysql::Value::Double(value) => value.to_string(),
        mysql::Value::Date(year, month, day, hour, minute, second, micros) => {
            let text = if hour == 0 && minute == 0 && second == 0 && micros == 0 {
                format!("{year:04}-{month:02}-{day:02}")
            } else {
                format!(
                    "{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{:06}",
                    micros
                )
            };
            escape_tsv_text(&text)
        }
        mysql::Value::Time(negative, days, hours, minutes, seconds, micros) => {
            let sign = if negative { "-" } else { "" };
            escape_tsv_text(&format!(
                "{sign}{days} {hours:02}:{minutes:02}:{seconds:02}.{:06}",
                micros
            ))
        }
    }
}

pub(crate) fn postgres_row_to_json(columns: &[String], row: &postgres::Row) -> Value {
    let mut object = Map::new();
    for (index, column) in columns.iter().enumerate() {
        let value: Option<String> = row.get(index);
        object.insert(
            column.clone(),
            value.map(Value::String).unwrap_or(Value::Null),
        );
    }
    Value::Object(object)
}

pub fn initial_state(schema: &NormalizedSchema) -> ResumeState {
    let schema = dependency_ordered_schema(schema);
    ResumeState {
        direction: "".to_string(),
        current_phase: "data".to_string(),
        tables: schema
            .tables
            .iter()
            .map(|table| ResumeTableState {
                table: table.name.clone(),
                completed: false,
                last_key: None,
                rows_copied: 0,
            })
            .collect(),
    }
}

pub fn canonical_row(row: &Map<String, Value>) -> String {
    let mut parts = Vec::new();
    let ordered: BTreeMap<_, _> = row.iter().collect();
    for (key, value) in ordered {
        parts.push(format!("{key}={}", canonical_value(value)));
    }
    parts.join("\x1f")
}

pub fn canonical_value(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(v) => format!("bool:{v}"),
        Value::Number(v) => format!("num:{v}"),
        Value::String(v) => format!("str:{}", v.replace('\\', "\\\\").replace('\x1f', "\\u001f")),
        Value::Array(items) => {
            let values: Vec<_> = items.iter().map(canonical_value).collect();
            format!("array:[{}]", values.join(","))
        }
        Value::Object(object) => format!("object:{{{}}}", canonical_row(object)),
    }
}

pub fn row_digest(row: &Map<String, Value>) -> String {
    let mut hasher = Sha256::new();
    hasher.update(canonical_row(row).as_bytes());
    hex::encode(hasher.finalize())
}

pub fn compare_digest_rows(source: &[Value], target: &[Value]) -> Vec<Value> {
    let source_counts = digest_counts(source);
    let target_counts = digest_counts(target);
    let mut mismatches = Vec::new();

    for (digest, source_count) in &source_counts {
        let target_count = target_counts.get(digest).copied().unwrap_or(0);
        if *source_count != target_count {
            mismatches.push(json!({
                "digest": digest,
                "source_count": source_count,
                "target_count": target_count
            }));
        }
    }
    for (digest, target_count) in &target_counts {
        if !source_counts.contains_key(digest) {
            mismatches.push(json!({
                "digest": digest,
                "source_count": 0,
                "target_count": target_count
            }));
        }
    }

    mismatches
}

fn digest_counts(rows: &[Value]) -> BTreeMap<String, u64> {
    let mut counts = BTreeMap::new();
    for row in rows {
        if let Value::Object(object) = row {
            *counts.entry(row_digest(object)).or_insert(0) += 1;
        }
    }
    counts
}

pub fn next_table_to_copy(state: &ResumeState) -> Option<String> {
    state
        .tables
        .iter()
        .find(|table| !table.completed)
        .map(|table| table.table.clone())
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::json;
    
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::{schema};

    #[test]
    fn sql_builder_uses_keyed_chunk_order() {
        let columns = vec!["id".to_string(), "name".to_string()];
        let key_columns = vec!["id".to_string()];
        assert_eq!(
            select_chunk_sql("postgresql", "users", &columns, &key_columns),
            "SELECT \"id\", \"name\" FROM \"users\" ORDER BY \"users\".\"id\" LIMIT $1 OFFSET $2"
        );
    }

    #[test]
    fn text_chunk_sql_casts_values_to_portable_text() {
        let key_columns = vec!["id".to_string()];
        assert_eq!(
            select_chunk_text_sql("postgresql", &schema().tables[0], &key_columns),
            "SELECT \"id\"::text AS \"id\", \"name\"::text AS \"name\" FROM \"users\" ORDER BY \"id\" LIMIT $1 OFFSET $2"
        );
        assert_eq!(
            select_chunk_text_sql("mysql", &schema().tables[0], &key_columns),
            "SELECT `id`, `name` FROM `users` ORDER BY `id` LIMIT ? OFFSET ?"
        );
    }

    #[test]
    fn canonical_row_orders_columns() {
        let a = json!({"b": 2, "a": "x"});
        let b = json!({"a": "x", "b": 2});
        assert_eq!(
            canonical_row(a.as_object().unwrap()),
            canonical_row(b.as_object().unwrap())
        );
        assert_eq!(
            row_digest(a.as_object().unwrap()),
            row_digest(b.as_object().unwrap())
        );
    }

    #[test]
    fn digest_compare_counts_duplicates() {
        let source = vec![json!({"id": 1}), json!({"id": 1}), json!({"id": 2})];
        let target = vec![json!({"id": 1}), json!({"id": 2})];
        let mismatches = compare_digest_rows(&source, &target);
        assert_eq!(mismatches.len(), 1);
        assert_eq!(mismatches[0]["source_count"], 2);
        assert_eq!(mismatches[0]["target_count"], 1);
    }

    #[test]
    fn resume_finds_first_incomplete_table() {
        let state = ResumeState {
            direction: "mysql_to_postgresql".to_string(),
            current_phase: "data".to_string(),
            tables: vec![
                ResumeTableState {
                    table: "users".to_string(),
                    completed: true,
                    last_key: Some("10".to_string()),
                    rows_copied: 10,
                },
                ResumeTableState {
                    table: "orders".to_string(),
                    completed: false,
                    last_key: None,
                    rows_copied: 0,
                },
            ],
        };

        assert_eq!(next_table_to_copy(&state), Some("orders".to_string()));
    }
}

#[cfg(test)]
mod keyset_guard_tests {
    use super::*;

    fn table(columns: &[(&str, &str)]) -> NormalizedTable {
        NormalizedTable {
            name: "events".to_string(),
            columns: columns
                .iter()
                .map(|(name, type_name)| NormalizedColumn {
                    name: name.to_string(),
                    type_name: type_name.to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                })
                .collect(),
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
            table_collation: None,
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
        }
    }

    fn advance(table: &NormalizedTable, keys: &[&str], previous: Option<&str>, next: &str) -> Result<String, String> {
        let keys: Vec<String> = keys.iter().map(|key| key.to_string()).collect();
        advance_keyset_cursor(table, &keys, previous, Some(next.to_string()))
    }

    #[test]
    fn first_page_and_a_greater_key_advance() {
        let ints = table(&[("id", "bigint")]);
        assert_eq!(advance(&ints, &["id"], None, r#"["5"]"#).unwrap(), r#"["5"]"#);
        assert!(advance(&ints, &["id"], Some(r#"["9"]"#), r#"["10"]"#).is_ok(), "numeric, not text, order for integers");
    }

    #[test]
    fn a_repeated_or_rewound_integer_key_stops_the_scan() {
        let ints = table(&[("id", "int")]);
        for next in [r#"["9"]"#, r#"["3"]"#] {
            let err = advance(&ints, &["id"], Some(r#"["9"]"#), next).unwrap_err();
            assert!(err.contains("did not advance") && err.contains("events"), "{err}");
            assert!(!err.contains('9'), "key values stay out of the message: {err}");
        }
    }

    #[test]
    fn binary_keys_compare_as_bytes_regardless_of_hex_case() {
        let binary = table(&[("id", "binary(16)")]);
        assert!(advance(&binary, &["id"], Some(r#"["0A"]"#), r#"["0b"]"#).is_ok());
        assert!(advance(&binary, &["id"], Some(r#"["ff"]"#), r#"["FF00"]"#).is_ok(), "a longer key with the same prefix sorts after");
        assert!(advance(&binary, &["id"], Some(r#"["FF00"]"#), r#"["ff"]"#).is_err(), "and the prefix sorts before it");
    }

    #[test]
    fn composite_keys_decide_on_the_first_differing_part() {
        let mixed = table(&[("grp", "int"), ("id", "bytea")]);
        assert!(advance(&mixed, &["grp", "id"], Some(r#"["1","ff"]"#), r#"["2","00"]"#).is_ok());
        assert!(advance(&mixed, &["grp", "id"], Some(r#"["1","ff"]"#), r#"["1","fe"]"#).is_err());
        assert!(advance(&mixed, &["grp", "id"], Some(r#"["1","ff"]"#), r#"["1","ff"]"#).is_err());
    }

    #[test]
    fn unorderable_types_only_reject_an_unchanged_key() {
        let text = table(&[("code", "varchar(20)")]);
        assert!(advance(&text, &["code"], Some(r#"["b"]"#), r#"["a"]"#).is_ok(), "collation order is unknown here");
        assert!(advance(&text, &["code"], Some(r#"["a"]"#), r#"["a"]"#).is_err());
    }

    #[test]
    fn an_unreadable_last_key_is_an_error() {
        let ints = table(&[("id", "int")]);
        assert!(advance_keyset_cursor(&ints, &["id".to_string()], None, None).is_err());
    }
}
