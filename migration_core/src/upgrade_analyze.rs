//! `upgrade.analyze`: MySQL 8.0 → 8.4 업그레이드 호환성 분석 (읽기 전용).
//!
//! 예전 Python MigrationAnalyzer 의 15단계(고아 레코드 + 호환성 검사 14종)를 그대로 옮겼다.
//! 진행 메시지도 같은 문구로 보낸다. DB 를 바꾸지 않으며, 정리/수정 SQL 은 텍스트로만 만든다.
use crate::*;
use mysql::prelude::Queryable;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::time::Instant;

const NEW_RESERVED_KEYWORDS_84: [&str; 4] = ["MANUAL", "PARALLEL", "QUALIFY", "TABLESAMPLE"];
const RESERVED_KEYWORDS_80: [&str; 22] = [
    "CUME_DIST", "DENSE_RANK", "EMPTY", "EXCEPT", "FIRST_VALUE", "GROUPING", "GROUPS", "JSON_TABLE", "LAG",
    "LAST_VALUE", "LATERAL", "LEAD", "NTH_VALUE", "NTILE", "OF", "OVER", "PERCENT_RANK", "RANK", "RECURSIVE",
    "ROW_NUMBER", "SYSTEM", "WINDOW",
];
/// 제거됨(8.4/8.0) + 8.4 deprecated. 순서는 Python ALL_REMOVED_FUNCTIONS 와 같다.
const ALL_REMOVED_FUNCTIONS: [&str; 10] = [
    "PASSWORD", "ENCRYPT", "ENCODE", "DECODE", "DES_ENCRYPT", "DES_DECRYPT", "OLD_PASSWORD", "MASTER_POS_WAIT",
    "FOUND_ROWS", "SQL_CALC_FOUND_ROWS",
];
const DEPRECATED_ONLY_FUNCTIONS: [&str; 3] = ["MASTER_POS_WAIT", "FOUND_ROWS", "SQL_CALC_FOUND_ROWS"];
const OBSOLETE_SQL_MODES: [&str; 11] = [
    "DB2", "MAXDB", "MSSQL", "MYSQL323", "MYSQL40", "ORACLE", "POSTGRESQL", "NO_FIELD_OPTIONS", "NO_KEY_OPTIONS",
    "NO_TABLE_OPTIONS", "NO_AUTO_CREATE_USER",
];
/// (엔진, severity, suggestion) - Python ENGINE_POLICIES
const ENGINE_POLICIES: [(&str, &str, &str); 9] = [
    ("MyISAM", "warning", "InnoDB로 변환 권장 (트랜잭션/FK 지원)"),
    ("ARCHIVE", "warning", "InnoDB로 변환 권장"),
    ("BLACKHOLE", "info", "테스트/복제용 엔진 - 필요시 유지"),
    ("FEDERATED", "warning", "MySQL 8.4에서 제거 예정"),
    ("MERGE", "error", "MySQL 8.4에서 제거됨 - InnoDB 파티셔닝으로 대체"),
    ("MEMORY", "info", "임시 테이블용으로는 유지 가능"),
    ("EXAMPLE", "warning", "예제/스텁 엔진 - 운영 환경에서는 InnoDB로 변경 권장"),
    ("NDB", "warning", "NDB Cluster 전용 엔진 - 단일 인스턴스 환경에서는 지원되지 않음"),
    ("CSV", "info", "로그/내보내기 용도로는 유지 가능, 일반 테이블은 InnoDB 권장"),
];
const LARGE_TABLE_ROW_THRESHOLD: u64 = 500_000;
const SIZE_INFO_LOG_THRESHOLD: u64 = 100_000;
const ORPHAN_SAMPLE_LIMIT: usize = 5;

#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct UpgradeIssue {
    pub issue_type: String,
    pub severity: String,
    pub location: String,
    pub description: String,
    pub suggestion: String,
    pub table_name: Option<String>,
    pub column_name: Option<String>,
    pub fix_query: Option<String>,
}

/// FK 의 컬럼 한 줄 (Python ForeignKeyInfo 와 같은 모양, 복합 FK 는 컬럼 수만큼)
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FkColumnRelation {
    pub constraint_name: String,
    pub child_table: String,
    pub child_column: String,
    pub parent_table: String,
    pub parent_column: String,
    pub on_delete: String,
    pub on_update: String,
}

/// 고아 레코드 판정 단위: FK 하나 (복합 FK 는 컬럼을 묶어서 판정)
#[derive(Debug, Clone, PartialEq)]
pub struct ForeignKeyGroup {
    pub constraint_name: String,
    pub child_table: String,
    pub parent_table: String,
    pub child_columns: Vec<String>,
    pub parent_columns: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CleanupSql {
    pub delete: String,
    pub set_null: String,
    pub count: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OrphanRecordOut {
    pub constraint_name: String,
    pub child_table: String,
    pub child_column: String,
    pub parent_table: String,
    pub parent_column: String,
    pub child_columns: Vec<String>,
    pub parent_columns: Vec<String>,
    pub orphan_count: u64,
    pub sample_values: Vec<Value>,
    pub cleanup_sql: CleanupSql,
}

#[derive(Debug, Clone, Copy)]
pub struct AnalyzeOptions {
    pub orphans: bool,
    pub charset: bool,
    pub keywords: bool,
    pub routines: bool,
    pub sql_mode: bool,
    pub auth_plugins: bool,
    pub zerofill: bool,
    pub float_precision: bool,
    pub fk_name_length: bool,
    pub invalid_dates: bool,
    pub year2: bool,
    pub deprecated_engines: bool,
    pub enum_empty: bool,
    pub timestamp_range: bool,
    pub int_display_width: bool,
}

impl AnalyzeOptions {
    fn from_payload(payload: &Value) -> Self {
        let flag = |key: &str| payload.get("options").and_then(|o| o.get(key)).and_then(Value::as_bool).unwrap_or(true);
        Self {
            orphans: flag("check_orphans"),
            charset: flag("check_charset"),
            keywords: flag("check_keywords"),
            routines: flag("check_routines"),
            sql_mode: flag("check_sql_mode"),
            auth_plugins: flag("check_auth_plugins"),
            zerofill: flag("check_zerofill"),
            float_precision: flag("check_float_precision"),
            fk_name_length: flag("check_fk_name_length"),
            invalid_dates: flag("check_invalid_dates"),
            year2: flag("check_year2"),
            deprecated_engines: flag("check_deprecated_engines"),
            enum_empty: flag("check_enum_empty"),
            timestamp_range: flag("check_timestamp_range"),
            int_display_width: flag("check_int_display_width"),
        }
    }
}

/// information_schema.COLUMNS 한 줄
#[derive(Debug, Clone, Default)]
pub struct ColumnFact {
    pub table: String,
    pub column: String,
    pub data_type: String,
    pub column_type: String,
    pub charset: Option<String>,
    pub base_table: bool,
}

#[derive(Debug, Clone, Default)]
pub struct TableFact {
    pub name: String,
    pub engine: Option<String>,
    pub collation: Option<String>,
    pub rows: u64,
}

fn q(name: &str) -> String {
    format!("`{}`", name.replace('`', "``"))
}

fn qualified(schema: &str, table: &str) -> String {
    format!("{}.{}", q(schema), q(table))
}

fn issue(issue_type: &str, severity: &str, location: String, description: String, suggestion: &str) -> UpgradeIssue {
    UpgradeIssue {
        issue_type: issue_type.into(),
        severity: severity.into(),
        location,
        description,
        suggestion: suggestion.into(),
        ..Default::default()
    }
}

fn is_word_char(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
}

/// `\bNAME\s*\(` : 식별자 경계에서 시작하는 함수 호출인지 (AES_ENCRYPT 안의 ENCRYPT 는 제외).
pub fn calls_function(definition_upper: &str, name: &str) -> bool {
    let mut search_from = 0;
    while let Some(offset) = definition_upper[search_from..].find(name) {
        let start = search_from + offset;
        let end = start + name.len();
        let boundary_before = definition_upper[..start].chars().next_back().is_none_or(|ch| !is_word_char(ch));
        let after = definition_upper[end..].trim_start();
        if boundary_before && after.starts_with('(') {
            return true;
        }
        search_from = end;
    }
    false
}

/// `^name(` 뒤에 `digits[,digits])` 가 오는지. digits_pair=true 면 `(M,D)` 만 인정한다.
fn has_numeric_args(column_type: &str, names: &[&str], digits_pair: bool) -> bool {
    let lower = column_type.to_lowercase();
    names.iter().any(|name| {
        let Some(rest) = lower.strip_prefix(&format!("{name}(")) else { return false };
        let Some(close) = rest.find(')') else { return false };
        let inner = &rest[..close];
        let parts: Vec<&str> = inner.split(',').collect();
        let numeric = |p: &str| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit());
        if digits_pair {
            parts.len() == 2 && parts.iter().all(|p| numeric(p))
        } else {
            parts.len() == 1 && numeric(parts[0])
        }
    })
}

// ---------------------------------------------------------------------------
// 순수 검사 (스냅샷 → 이슈). 진행 로그는 호출 쪽에서 보낸다.
// ---------------------------------------------------------------------------

pub fn check_charset(schema: &str, tables: &[TableFact], columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    let mut issues = Vec::new();
    for table in tables {
        let collation = table.collation.clone().unwrap_or_default();
        let lower = collation.to_lowercase();
        if lower.starts_with("utf8_") || lower.starts_with("utf8mb3_") {
            issues.push(issue(
                "charset_issue",
                "warning",
                format!("{schema}.{}", table.name),
                format!("테이블이 utf8mb3 collation 사용 중: {collation}"),
                "ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            ));
        }
    }
    for column in columns.iter().filter(|c| c.base_table) {
        let charset = column.charset.clone().unwrap_or_default();
        if charset == "utf8" || charset == "utf8mb3" {
            issues.push(issue(
                "charset_issue",
                "warning",
                format!("{schema}.{}.{}", column.table, column.column),
                format!("컬럼이 utf8mb3 사용 중: {charset}"),
                "ALTER TABLE ... MODIFY COLUMN ... CHARACTER SET utf8mb4",
            ));
        }
    }
    issues
}

pub fn check_reserved_keywords(schema: &str, tables: &[TableFact], columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    let reserved = |name: &str| {
        let upper = name.to_uppercase();
        RESERVED_KEYWORDS_80.contains(&upper.as_str()) || NEW_RESERVED_KEYWORDS_84.contains(&upper.as_str())
    };
    let mut issues = Vec::new();
    for table in tables.iter().filter(|t| reserved(&t.name)) {
        issues.push(issue(
            "reserved_keyword",
            "error",
            format!("{schema}.{}", table.name),
            format!("테이블명 '{}'이 MySQL 8.4 예약어와 충돌", table.name),
            "테이블명을 백틱으로 감싸거나 이름 변경 필요",
        ));
    }
    for column in columns.iter().filter(|c| reserved(&c.column)) {
        issues.push(issue(
            "reserved_keyword",
            "warning",
            format!("{schema}.{}.{}", column.table, column.column),
            format!("컬럼명 '{}'이 MySQL 8.4 예약어와 충돌", column.column),
            "컬럼 참조 시 백틱(`) 사용 필요",
        ));
    }
    issues
}

pub fn check_routines(schema: &str, routines: &[(String, String, String)]) -> Vec<UpgradeIssue> {
    let mut issues = Vec::new();
    for (name, kind, definition) in routines {
        let upper = definition.to_uppercase();
        for function in ALL_REMOVED_FUNCTIONS {
            if calls_function(&upper, function) {
                let deprecated_only = DEPRECATED_ONLY_FUNCTIONS.contains(&function);
                let (severity, label) = if deprecated_only { ("warning", "deprecated") } else { ("error", "removed") };
                issues.push(issue(
                    "deprecated_function",
                    severity,
                    format!("{kind} {schema}.{name}"),
                    format!("{label} 함수 '{function}' 사용 중"),
                    &format!("'{function}' 함수를 대체 함수로 변경 필요"),
                ));
            }
        }
    }
    issues
}

pub fn check_sql_mode(sql_mode: &str) -> Vec<UpgradeIssue> {
    sql_mode
        .split(',')
        .map(str::trim)
        .filter(|mode| OBSOLETE_SQL_MODES.contains(mode))
        .map(|mode| {
            issue(
                "sql_mode_issue",
                "warning",
                "@@sql_mode".into(),
                format!("deprecated SQL 모드 '{mode}' 사용 중"),
                &format!("sql_mode에서 '{mode}' 제거 필요"),
            )
        })
        .collect()
}

pub fn check_auth_plugins(users: &[(String, String, String)]) -> Vec<UpgradeIssue> {
    users
        .iter()
        .filter_map(|(user, host, plugin)| {
            let location = format!("'{user}'@'{host}'");
            match plugin.as_str() {
                "mysql_native_password" => Some(issue(
                    "auth_plugin_issue",
                    "error",
                    location,
                    "mysql_native_password 인증 사용 (8.4에서 기본 비활성화)".into(),
                    "ALTER USER ... IDENTIFIED WITH caching_sha2_password",
                )),
                "sha256_password" => Some(issue(
                    "auth_plugin_issue",
                    "warning",
                    location,
                    "sha256_password 인증 사용 (deprecated)".into(),
                    "ALTER USER ... IDENTIFIED WITH caching_sha2_password 권장",
                )),
                "authentication_fido" | "authentication_fido_client" => Some(issue(
                    "auth_plugin_issue",
                    "error",
                    location,
                    format!("{plugin} 플러그인 사용 (8.4에서 제거됨)"),
                    "authentication_webauthn 또는 다른 인증 방식으로 변경 필요",
                )),
                _ => None,
            }
        })
        .collect()
}

fn column_location(schema: &str, column: &ColumnFact) -> String {
    format!("{schema}.{}.{}", column.table, column.column)
}

fn with_column(mut issue: UpgradeIssue, column: &ColumnFact, fix_query: Option<String>) -> UpgradeIssue {
    issue.table_name = Some(column.table.clone());
    issue.column_name = Some(column.column.clone());
    issue.fix_query = fix_query;
    issue
}

pub fn check_zerofill(schema: &str, columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    columns
        .iter()
        .filter(|c| c.column_type.to_lowercase().contains("zerofill"))
        .map(|c| {
            issue(
                "zerofill_usage",
                "warning",
                column_location(schema, c),
                format!("ZEROFILL 속성 사용: {}", c.column_type),
                "ZEROFILL은 deprecated됨, 애플리케이션에서 LPAD() 등으로 처리 권장",
            )
        })
        .collect()
}

pub fn check_float_precision(schema: &str, columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    columns
        .iter()
        .filter(|c| matches!(c.data_type.as_str(), "float" | "double") && has_numeric_args(&c.column_type, &["float", "double"], true))
        .map(|c| {
            issue(
                "float_precision",
                "warning",
                column_location(schema, c),
                format!("FLOAT/DOUBLE 정밀도 구문 사용: {}", c.column_type),
                "FLOAT(M,D) 구문은 deprecated됨, FLOAT 또는 DECIMAL(M,D) 사용 권장",
            )
        })
        .collect()
}

pub fn check_fk_name_length(schema: &str, groups: &[ForeignKeyGroup]) -> Vec<UpgradeIssue> {
    groups
        .iter()
        .filter(|fk| fk.constraint_name.chars().count() > 64)
        .map(|fk| {
            issue(
                "fk_name_length",
                "error",
                format!("{schema}.{}.{}", fk.child_table, fk.constraint_name),
                format!("FK 이름이 64자 초과: {}자", fk.constraint_name.chars().count()),
                "FK 이름을 64자 이하로 변경 필요 (8.4 제한)",
            )
        })
        .collect()
}

pub fn check_year2(schema: &str, columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    columns
        .iter()
        .filter(|c| c.column_type.eq_ignore_ascii_case("year(2)"))
        .map(|c| {
            let fix = format!("ALTER TABLE {} MODIFY {} YEAR;", qualified(schema, &c.table), q(&c.column));
            with_column(
                issue("year2_type", "error", column_location(schema, c), "YEAR(2) 타입 사용 - MySQL 8.0에서 제거됨".into(), "YEAR(4) 또는 YEAR로 변경 필요"),
                c,
                Some(fix),
            )
        })
        .collect()
}

pub fn check_deprecated_engines(schema: &str, tables: &[TableFact]) -> Vec<UpgradeIssue> {
    tables
        .iter()
        .filter_map(|table| {
            let engine = table.engine.as_deref()?;
            let (_, severity, suggestion) = ENGINE_POLICIES.iter().find(|(name, _, _)| *name == engine)?;
            let mut found = issue(
                "deprecated_engine",
                severity,
                format!("{schema}.{}", table.name),
                format!("deprecated 스토리지 엔진: {engine}"),
                suggestion,
            );
            found.table_name = Some(table.name.clone());
            found.fix_query = (engine != "MEMORY").then(|| format!("ALTER TABLE {} ENGINE=InnoDB;", qualified(schema, &table.name)));
            Some(found)
        })
        .collect()
}

pub fn check_enum_empty(schema: &str, columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    columns
        .iter()
        .filter(|c| c.data_type == "enum" && c.column_type.contains("''"))
        .map(|c| {
            with_column(
                issue("enum_empty_value", "warning", column_location(schema, c), "ENUM에 빈 문자열('') 정의됨".into(), "빈 문자열 대신 NULL 허용 또는 명시적 값 사용 권장"),
                c,
                None,
            )
        })
        .collect()
}

pub fn check_timestamp_range(schema: &str, columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    columns
        .iter()
        .filter(|c| c.data_type == "timestamp")
        .map(|c| {
            let fix = format!("ALTER TABLE {} MODIFY {} DATETIME;", qualified(schema, &c.table), q(&c.column));
            with_column(
                issue(
                    "timestamp_range",
                    "warning",
                    column_location(schema, c),
                    "TIMESTAMP 컬럼은 2038년 범위 제한이 있습니다".into(),
                    "2038년 이후 값이 필요한 컬럼은 DATETIME으로 변경을 검토하세요",
                ),
                c,
                Some(fix),
            )
        })
        .collect()
}

pub fn check_int_display_width(schema: &str, columns: &[ColumnFact]) -> Vec<UpgradeIssue> {
    const INTS: [&str; 5] = ["tinyint", "smallint", "mediumint", "int", "bigint"];
    columns
        .iter()
        .filter(|c| {
            INTS.contains(&c.data_type.as_str())
                && has_numeric_args(&c.column_type, &INTS, false)
                && !(c.data_type == "tinyint" && c.column_type.to_lowercase().starts_with("tinyint(1)"))
        })
        .map(|c| {
            issue(
                "int_display_width",
                "info",
                column_location(schema, c),
                format!("INT 표시 너비 사용: {}", c.column_type),
                "표시 너비는 deprecated됨, 8.4에서 자동 무시됨 (영향 최소)",
            )
        })
        .collect()
}

fn invalid_date_predicate(column: &str, data_type: &str) -> String {
    let c = q(column);
    let zero = if data_type == "date" { "'0000-00-00'" } else { "'0000-00-00 00:00:00'" };
    format!("{c} = {zero} OR ({c} IS NOT NULL AND (MONTH({c}) = 0 OR DAY({c}) = 0))")
}

pub fn invalid_date_issue(schema: &str, table: &str, column: &str, count: u64) -> UpgradeIssue {
    let c = q(column);
    UpgradeIssue {
        issue_type: "invalid_date".into(),
        severity: "error".into(),
        location: format!("{schema}.{table}.{column}"),
        description: format!("잘못된 날짜값 {}개 발견 (0000-00-00 등)", thousands(count)),
        suggestion: "NULL로 변경하거나 유효한 날짜로 수정 필요 (8.4 NO_ZERO_DATE)".into(),
        table_name: Some(table.into()),
        column_name: Some(column.into()),
        fix_query: Some(format!(
            "UPDATE {} SET {c} = NULL WHERE {c} = '0000-00-00' OR MONTH({c}) = 0 OR DAY({c}) = 0;",
            qualified(schema, table)
        )),
    }
}

fn thousands(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

// ---------------------------------------------------------------------------
// FK / 고아 레코드
// ---------------------------------------------------------------------------

pub fn build_fk_tree(relations: &[FkColumnRelation]) -> BTreeMap<String, Vec<String>> {
    let mut tree: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for fk in relations {
        let children = tree.entry(fk.parent_table.clone()).or_default();
        if !children.contains(&fk.child_table) {
            children.push(fk.child_table.clone());
        }
    }
    tree
}

/// 고아 = FK 컬럼이 모두 NOT NULL 인데 일치하는 부모 행이 없는 자식 행 (MySQL MATCH SIMPLE 과 같은 판정).
fn orphan_from_and_where(schema: &str, fk: &ForeignKeyGroup, large: bool) -> String {
    let child = qualified(schema, &fk.child_table);
    let parent = qualified(schema, &fk.parent_table);
    let not_null = fk.child_columns.iter().map(|c| format!("c.{} IS NOT NULL", q(c))).collect::<Vec<_>>().join(" AND ");
    let join = fk
        .child_columns
        .iter()
        .zip(&fk.parent_columns)
        .map(|(c, p)| format!("p.{} = c.{}", q(p), q(c)))
        .collect::<Vec<_>>()
        .join(" AND ");
    if large {
        format!("FROM {child} c WHERE {not_null} AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join})")
    } else {
        let first_parent = q(&fk.parent_columns[0]);
        format!("FROM {child} c LEFT JOIN {parent} p ON {join} WHERE {not_null} AND p.{first_parent} IS NULL")
    }
}

fn not_exists_where(schema: &str, fk: &ForeignKeyGroup) -> String {
    let parent = qualified(schema, &fk.parent_table);
    let not_null = fk.child_columns.iter().map(|c| format!("c.{} IS NOT NULL", q(c))).collect::<Vec<_>>().join("\n    AND ");
    let join = fk
        .child_columns
        .iter()
        .zip(&fk.parent_columns)
        .map(|(c, p)| format!("p.{} = c.{}", q(p), q(c)))
        .collect::<Vec<_>>()
        .join(" AND ");
    format!("WHERE {not_null}\n    AND NOT EXISTS (\n        SELECT 1 FROM {parent} AS p\n        WHERE {join}\n    )")
}

pub fn cleanup_sql(schema: &str, fk: &ForeignKeyGroup) -> CleanupSql {
    let child = qualified(schema, &fk.child_table);
    let where_clause = not_exists_where(schema, fk);
    let set_null = fk.child_columns.iter().map(|c| format!("c.{} = NULL", q(c))).collect::<Vec<_>>().join(", ");
    CleanupSql {
        delete: format!("DELETE c FROM {child} AS c\n{where_clause}"),
        set_null: format!("UPDATE {child} AS c\nSET {set_null}\n{where_clause}"),
        count: format!("SELECT COUNT(*) AS cnt FROM {child} AS c\n{where_clause}"),
    }
}

fn group_foreign_keys(rows: &[(String, String, String, String, String)]) -> Vec<ForeignKeyGroup> {
    let mut groups: Vec<ForeignKeyGroup> = Vec::new();
    for (table, constraint, column, ref_table, ref_column) in rows {
        match groups.iter_mut().find(|g| &g.child_table == table && &g.constraint_name == constraint) {
            Some(group) => {
                group.child_columns.push(column.clone());
                group.parent_columns.push(ref_column.clone());
            }
            None => groups.push(ForeignKeyGroup {
                constraint_name: constraint.clone(),
                child_table: table.clone(),
                parent_table: ref_table.clone(),
                child_columns: vec![column.clone()],
                parent_columns: vec![ref_column.clone()],
            }),
        }
    }
    groups
}

fn sample_value(row: mysql::Row) -> Value {
    let values: Vec<Value> = row
        .unwrap()
        .into_iter()
        .map(|value| match value {
            mysql::Value::NULL => Value::Null,
            mysql::Value::Int(v) => json!(v),
            mysql::Value::UInt(v) => json!(v),
            mysql::Value::Float(v) => json!(v),
            mysql::Value::Double(v) => json!(v),
            mysql::Value::Bytes(bytes) => Value::String(String::from_utf8_lossy(&bytes).into_owned()),
            other => Value::String(other.as_sql(true).trim_matches('\'').to_string()),
        })
        .collect();
    if values.len() == 1 {
        values.into_iter().next().unwrap()
    } else {
        Value::String(format!("({})", values.iter().map(|v| v.to_string().trim_matches('"').to_string()).collect::<Vec<_>>().join(", ")))
    }
}

// ---------------------------------------------------------------------------
// 실행
// ---------------------------------------------------------------------------

struct Ctx<'a, F: FnMut(Value)> {
    request_id: Option<String>,
    emit: &'a mut F,
}

impl<F: FnMut(Value)> Ctx<'_, F> {
    fn log(&mut self, message: impl Into<String>) {
        let message = message.into();
        (self.emit)(json!({"event": "progress", "request_id": self.request_id, "command": "upgrade.analyze", "message": message}));
    }

    fn summary(&mut self, found: usize, found_message: String, clean_message: &str) {
        if found > 0 {
            self.log(found_message);
        } else {
            self.log(clean_message);
        }
    }
}

fn load_snapshot(conn: &mut mysql::PooledConn, schema: &str) -> Result<(Vec<TableFact>, Vec<ColumnFact>), String> {
    let tables: Vec<(String, Option<String>, Option<String>, Option<u64>)> = conn
        .exec(
            "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION, TABLE_ROWS FROM information_schema.TABLES \
             WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
            (schema,),
        )
        .map_err(|e| format!("table list query failed: {e}"))?;
    let tables: Vec<TableFact> = tables
        .into_iter()
        .map(|(name, engine, collation, rows)| TableFact { name, engine, collation, rows: rows.unwrap_or(0) })
        .collect();
    let columns: Vec<(String, String, String, String, Option<String>, String)> = conn
        .exec(
            "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.COLUMN_TYPE, c.CHARACTER_SET_NAME, t.TABLE_TYPE \
             FROM information_schema.COLUMNS c JOIN information_schema.TABLES t \
               ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME \
             WHERE c.TABLE_SCHEMA = ? ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION",
            (schema,),
        )
        .map_err(|e| format!("column query failed: {e}"))?;
    let columns = columns
        .into_iter()
        .map(|(table, column, data_type, column_type, charset, table_type)| ColumnFact {
            table,
            column,
            data_type: data_type.to_lowercase(),
            column_type,
            charset,
            base_table: table_type == "BASE TABLE",
        })
        .collect();
    Ok((tables, columns))
}

fn load_foreign_keys(conn: &mut mysql::PooledConn, schema: &str) -> Result<(Vec<FkColumnRelation>, Vec<ForeignKeyGroup>), String> {
    let rows: Vec<(String, String, String, String, String, String, String, u64)> = conn
        .exec(
            "SELECT kcu.CONSTRAINT_NAME, kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, \
             kcu.REFERENCED_COLUMN_NAME, rc.DELETE_RULE, rc.UPDATE_RULE, kcu.ORDINAL_POSITION \
             FROM information_schema.KEY_COLUMN_USAGE kcu \
             JOIN information_schema.REFERENTIAL_CONSTRAINTS rc \
               ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME \
              AND rc.TABLE_NAME = kcu.TABLE_NAME \
             WHERE kcu.TABLE_SCHEMA = ? AND kcu.REFERENCED_TABLE_NAME IS NOT NULL \
             ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION",
            (schema,),
        )
        .map_err(|e| format!("foreign key query failed: {e}"))?;
    let grouped = group_foreign_keys(
        &rows.iter().map(|r| (r.1.clone(), r.0.clone(), r.2.clone(), r.3.clone(), r.4.clone())).collect::<Vec<_>>(),
    );
    // FK 관계 목록/트리는 예전처럼 (자식 테이블, 자식 컬럼) 순서
    let mut relations: Vec<FkColumnRelation> = rows
        .into_iter()
        .map(|(constraint_name, child_table, child_column, parent_table, parent_column, on_delete, on_update, _)| FkColumnRelation {
            constraint_name,
            child_table,
            child_column,
            parent_table,
            parent_column,
            on_delete,
            on_update,
        })
        .collect();
    relations.sort_by(|a, b| (a.child_table.as_str(), a.child_column.as_str()).cmp(&(b.child_table.as_str(), b.child_column.as_str())));
    Ok((relations, grouped))
}

fn find_orphans<F: FnMut(Value)>(
    ctx: &mut Ctx<'_, F>,
    conn: &mut mysql::PooledConn,
    schema: &str,
    groups: &[ForeignKeyGroup],
    tables: &[TableFact],
) -> Vec<OrphanRecordOut> {
    ctx.log("🔍 고아 레코드 탐지 중...");
    let rows_of = |name: &str| tables.iter().find(|t| t.name == name).map(|t| t.rows).unwrap_or(0);
    let mut orphans = Vec::new();
    for (i, fk) in groups.iter().enumerate() {
        let (child_rows, parent_rows) = (rows_of(&fk.child_table), rows_of(&fk.parent_table));
        let large = child_rows > LARGE_TABLE_ROW_THRESHOLD || parent_rows > LARGE_TABLE_ROW_THRESHOLD;
        let size_info = if child_rows > SIZE_INFO_LOG_THRESHOLD || parent_rows > SIZE_INFO_LOG_THRESHOLD {
            format!(" [자식:{}행, 부모:{}행]", thousands(child_rows), thousands(parent_rows))
        } else {
            String::new()
        };
        let (child_cols, parent_cols) = (fk.child_columns.join(", "), fk.parent_columns.join(", "));
        ctx.log(format!(
            "  검사 중: {}.{} → {}.{} ({}/{}){size_info}",
            fk.child_table,
            child_cols,
            fk.parent_table,
            parent_cols,
            i + 1,
            groups.len()
        ));
        if large {
            ctx.log("    📊 대용량 테이블 - 최적화 쿼리 사용");
        }
        let from_where = orphan_from_and_where(schema, fk, large);
        let started = Instant::now();
        let count: Result<Option<u64>, _> = conn.query_first(format!("SELECT COUNT(*) {from_where}"));
        let count = match count {
            Ok(value) => value.unwrap_or(0),
            Err(err) => {
                ctx.log(format!("    ❌ 검사 실패: {}.{} - {err}", fk.child_table, child_cols));
                continue;
            }
        };
        let elapsed = started.elapsed().as_secs_f64();
        if elapsed > 3.0 {
            ctx.log(format!("    ⏱️ 쿼리 소요시간: {elapsed:.1}초"));
        }
        if count == 0 {
            continue;
        }
        let select = fk.child_columns.iter().map(|c| format!("c.{}", q(c))).collect::<Vec<_>>().join(", ");
        let samples: Vec<Value> = conn
            .query::<mysql::Row, _>(format!("SELECT DISTINCT {select} {from_where} LIMIT {ORPHAN_SAMPLE_LIMIT}"))
            .map(|rows| rows.into_iter().map(sample_value).collect())
            .unwrap_or_default();
        orphans.push(OrphanRecordOut {
            constraint_name: fk.constraint_name.clone(),
            child_table: fk.child_table.clone(),
            child_column: child_cols.clone(),
            parent_table: fk.parent_table.clone(),
            parent_column: parent_cols.clone(),
            child_columns: fk.child_columns.clone(),
            parent_columns: fk.parent_columns.clone(),
            orphan_count: count,
            sample_values: samples,
            cleanup_sql: cleanup_sql(schema, fk),
        });
        ctx.log(format!("    ⚠️ 고아 레코드 발견: {count}개"));
    }
    orphans
}

fn check_invalid_dates<F: FnMut(Value)>(
    ctx: &mut Ctx<'_, F>,
    conn: &mut mysql::PooledConn,
    schema: &str,
    columns: &[ColumnFact],
) -> Vec<UpgradeIssue> {
    ctx.log("🔍 0000-00-00 날짜값 확인 중...");
    let mut date_columns: Vec<&ColumnFact> =
        columns.iter().filter(|c| c.base_table && matches!(c.data_type.as_str(), "date" | "datetime" | "timestamp")).collect();
    date_columns.sort_by(|a, b| (a.table.as_str(), a.column.as_str()).cmp(&(b.table.as_str(), b.column.as_str())));
    if date_columns.is_empty() {
        ctx.log("  ✅ DATE/DATETIME 컬럼 없음");
        return Vec::new();
    }
    ctx.log(format!("  DATE/DATETIME 컬럼 {}개 검사 중...", date_columns.len()));
    let mut issues = Vec::new();
    let mut checked = 0;
    for column in date_columns {
        let sql = format!(
            "SELECT COUNT(*) FROM {} WHERE {}",
            qualified(schema, &column.table),
            invalid_date_predicate(&column.column, &column.data_type)
        );
        match conn.query_first::<u64, _>(sql) {
            Ok(count) => {
                let count = count.unwrap_or(0);
                if count > 0 {
                    issues.push(invalid_date_issue(schema, &column.table, &column.column, count));
                    ctx.log(format!("    ⚠️ {}.{}: 잘못된 날짜 {}개", column.table, column.column, thousands(count)));
                }
                checked += 1;
            }
            Err(err) => {
                let text: String = err.to_string().chars().take(50).collect();
                ctx.log(format!("    ⏭️ {}.{} 검사 스킵: {text}", column.table, column.column));
            }
        }
    }
    if issues.is_empty() {
        ctx.log(format!("  ✅ 잘못된 날짜값 없음 ({checked}개 컬럼 검사)"));
    } else {
        ctx.log(format!("  ⚠️ 잘못된 날짜값 {}개 컬럼에서 발견", issues.len()));
    }
    issues
}

fn run_analysis<F: FnMut(Value)>(ctx: &mut Ctx<'_, F>, endpoint: &Endpoint, options: AnalyzeOptions) -> Result<Value, String> {
    let schema = endpoint_schema(endpoint);
    ctx.log(format!("📊 스키마 '{schema}' 분석 시작..."));
    let mut conn = mysql_conn(endpoint)?;
    // 세션 기본 sql_mode(=서버 설정)를 완화 전에 읽어 둔다. 완화한 뒤 읽으면 항상 빈 값이다.
    let original_sql_mode: String = conn.query_first("SELECT @@SESSION.sql_mode").map_err(|e| e.to_string())?.unwrap_or_default();
    // COLUMN_DEFAULT 에 '0000-00-00' 이 있으면 NO_ZERO_DATE 가 1525 오류를 내므로 분석 세션만 완화한다(읽기 전용).
    conn.query_drop("SET SESSION sql_mode = ''").map_err(|e| format!("sql_mode relax failed: {e}"))?;

    let (tables, columns) = load_snapshot(&mut conn, &schema)?;
    let (relations, groups) = load_foreign_keys(&mut conn, &schema)?;
    let fk_tree = build_fk_tree(&relations);
    ctx.log(format!("  테이블 수: {}, FK 관계: {}", tables.len(), relations.len()));

    let total_steps = 15;
    let mut orphans = Vec::new();
    if options.orphans && !relations.is_empty() {
        ctx.log(format!("📌 [1/{total_steps}] 고아 레코드 검사 시작..."));
        orphans = find_orphans(ctx, &mut conn, &schema, &groups, &tables);
        ctx.log(format!("✅ [1/{total_steps}] 고아 레코드 검사 완료 (발견: {}건)", orphans.len()));
    }

    let mut issues: Vec<UpgradeIssue> = Vec::new();
    let step = |ctx: &mut Ctx<'_, F>, no: usize, enabled: bool, label: &str| {
        if enabled {
            ctx.log(format!("📌 [{no}/{total_steps}] {label}"));
        }
        enabled
    };

    if step(ctx, 2, options.charset, "문자셋 이슈 검사...") {
        ctx.log("🔍 문자셋 이슈 확인 중...");
        let found = check_charset(&schema, &tables, &columns);
        ctx.summary(found.len(), format!("  ⚠️ 문자셋 이슈 {}개 발견", found.len()), "  ✅ 문자셋 이슈 없음");
        issues.extend(found);
    }
    if step(ctx, 3, options.keywords, "예약어 충돌 검사...") {
        ctx.log("🔍 예약어 충돌 확인 중...");
        let found = check_reserved_keywords(&schema, &tables, &columns);
        ctx.summary(found.len(), format!("  ⚠️ 예약어 충돌 {}개 발견", found.len()), "  ✅ 예약어 충돌 없음");
        issues.extend(found);
    }
    if step(ctx, 4, options.routines, "저장 프로시저/함수 검사...") {
        ctx.log("🔍 저장 프로시저/함수 검사 중...");
        let routines: Vec<(String, String, String)> = conn
            .exec(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION FROM information_schema.ROUTINES \
                 WHERE ROUTINE_SCHEMA = ? AND ROUTINE_DEFINITION IS NOT NULL",
                (&schema,),
            )
            .map_err(|e| format!("routine query failed: {e}"))?;
        let found = check_routines(&schema, &routines);
        ctx.summary(found.len(), format!("  ⚠️ deprecated 함수 사용 {}개 발견", found.len()), "  ✅ deprecated 함수 없음");
        issues.extend(found);
    }
    if step(ctx, 5, options.sql_mode, "SQL 모드 검사...") {
        ctx.log("🔍 SQL 모드 확인 중...");
        let found = check_sql_mode(&original_sql_mode);
        ctx.summary(found.len(), format!("  ⚠️ deprecated SQL 모드 {}개 발견", found.len()), "  ✅ SQL 모드 정상");
        issues.extend(found);
    }
    if step(ctx, 6, options.auth_plugins, "인증 플러그인 검사...") {
        ctx.log("🔍 인증 플러그인 확인 중...");
        let users: Result<Vec<(String, String, String)>, _> = conn.query(
            "SELECT User, Host, plugin FROM mysql.user WHERE plugin IN \
             ('mysql_native_password', 'sha256_password', 'authentication_fido', 'authentication_fido_client')",
        );
        match users {
            Ok(users) => {
                let found = check_auth_plugins(&users);
                ctx.summary(found.len(), format!("  ⚠️ 인증 플러그인 이슈 {}개 발견", found.len()), "  ✅ 인증 플러그인 정상");
                issues.extend(found);
            }
            // mysql.user 는 권한이 필요하다. 읽지 못해도 분석 전체를 실패시키지 않는다.
            Err(err) => ctx.log(format!("  ⚠️ 인증 플러그인 확인 실패: {err}")),
        }
    }
    let column_checks: [(usize, bool, &str, &str, fn(&str, &[ColumnFact]) -> Vec<UpgradeIssue>, &str, &str); 6] = [
        (7, options.zerofill, "ZEROFILL 속성 검사...", "🔍 ZEROFILL 속성 확인 중...", check_zerofill, "ZEROFILL 사용", "  ✅ ZEROFILL 사용 없음"),
        (8, options.float_precision, "FLOAT(M,D) 구문 검사...", "🔍 FLOAT/DOUBLE 정밀도 구문 확인 중...", check_float_precision, "FLOAT/DOUBLE 정밀도 구문", "  ✅ FLOAT/DOUBLE 구문 정상"),
        (11, options.year2, "YEAR(2) 타입 검사...", "🔍 YEAR(2) 타입 확인 중...", check_year2, "YEAR(2) 타입", "  ✅ YEAR(2) 타입 없음"),
        (13, options.enum_empty, "ENUM 빈 문자열 검사...", "🔍 ENUM 빈 문자열 확인 중...", check_enum_empty, "ENUM 빈 문자열", "  ✅ ENUM 빈 문자열 없음"),
        (14, options.timestamp_range, "TIMESTAMP 범위 검사...", "🔍 TIMESTAMP 범위 확인 중...", check_timestamp_range, "TIMESTAMP 범위 제한 컬럼", "  ✅ TIMESTAMP 컬럼 없음"),
        (15, options.int_display_width, "INT 표시 너비 검사...", "🔍 INT 표시 너비 확인 중...", check_int_display_width, "INT 표시 너비", "  ✅ INT 표시 너비 없음"),
    ];
    let run_column_check = |ctx: &mut Ctx<'_, F>, issues: &mut Vec<UpgradeIssue>, index: usize| {
        let (no, enabled, label, start, check, noun, clean) = column_checks[index];
        if step(ctx, no, enabled, label) {
            ctx.log(start);
            let found = check(&schema, &columns);
            let found_message = if no == 15 {
                format!("  ℹ️ {noun} {}개 발견 (경미)", found.len())
            } else {
                format!("  ⚠️ {noun} {}개 발견", found.len())
            };
            ctx.summary(found.len(), found_message, clean);
            issues.extend(found);
        }
    };
    run_column_check(ctx, &mut issues, 0);
    run_column_check(ctx, &mut issues, 1);
    if step(ctx, 9, options.fk_name_length, "FK 이름 길이 검사...") {
        ctx.log("🔍 FK 이름 길이 확인 중...");
        let found = check_fk_name_length(&schema, &groups);
        ctx.summary(found.len(), format!("  ⚠️ FK 이름 길이 초과 {}개 발견", found.len()), "  ✅ FK 이름 길이 정상");
        issues.extend(found);
    }
    if step(ctx, 10, options.invalid_dates, "0000-00-00 날짜값 검사...") {
        issues.extend(check_invalid_dates(ctx, &mut conn, &schema, &columns));
    }
    run_column_check(ctx, &mut issues, 2);
    if step(ctx, 12, options.deprecated_engines, "deprecated 스토리지 엔진 검사...") {
        ctx.log("🔍 deprecated 스토리지 엔진 확인 중...");
        let found = check_deprecated_engines(&schema, &tables);
        ctx.summary(found.len(), format!("  ⚠️ deprecated 엔진 {}개 발견", found.len()), "  ✅ deprecated 엔진 없음");
        issues.extend(found);
    }
    run_column_check(ctx, &mut issues, 3);
    run_column_check(ctx, &mut issues, 4);
    run_column_check(ctx, &mut issues, 5);

    ctx.log("✅ 분석 완료");
    ctx.log(format!("  - 고아 레코드: {}개 FK 관계에서 발견", orphans.len()));
    ctx.log(format!("  - 호환성 이슈: {}개", issues.len()));
    Ok(json!({
        "schema": schema,
        "total_tables": tables.len(),
        "total_fk_relations": relations.len(),
        "fk_relations": relations,
        "fk_tree": fk_tree,
        "orphan_records": orphans,
        "issues": issues,
    }))
}

/// `upgrade.analyze` 요청 처리. progress 이벤트를 보내고 result 또는 error 로 끝난다.
pub fn upgrade_analyze<F: FnMut(Value)>(request: &Request, mut emit: F) {
    let result = (|| -> Result<Value, String> {
        let value = request
            .payload
            .get("connection")
            .or_else(|| request.payload.get("source"))
            .ok_or_else(|| "missing connection endpoint".to_string())?;
        let endpoint = endpoint_from_value(value)?;
        if endpoint.engine != "mysql" {
            return Err("upgrade.analyze supports MySQL only".to_string());
        }
        let options = AnalyzeOptions::from_payload(&request.payload);
        let mut ctx = Ctx { request_id: request.request_id.clone(), emit: &mut emit };
        run_analysis(&mut ctx, &endpoint, options)
    })();
    match result {
        Ok(Value::Object(mut fields)) => {
            fields.insert("event".into(), json!("result"));
            fields.insert("request_id".into(), json!(request.request_id));
            fields.insert("command".into(), json!("upgrade.analyze"));
            fields.insert("success".into(), json!(true));
            emit(Value::Object(fields));
        }
        Ok(_) => unreachable!(),
        Err(message) => emit(json!({"event": "error", "request_id": request.request_id, "command": "upgrade.analyze", "message": message})),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn col(table: &str, column: &str, data_type: &str, column_type: &str) -> ColumnFact {
        ColumnFact {
            table: table.into(),
            column: column.into(),
            data_type: data_type.into(),
            column_type: column_type.into(),
            charset: None,
            base_table: true,
        }
    }

    fn types(issues: &[UpgradeIssue]) -> Vec<(&str, &str, &str)> {
        issues.iter().map(|i| (i.issue_type.as_str(), i.severity.as_str(), i.location.as_str())).collect()
    }

    #[test]
    fn function_calls_respect_identifier_boundaries() {
        assert!(calls_function("SELECT PASSWORD('X')", "PASSWORD"));
        assert!(calls_function("SET @A = ENCRYPT  ('X')", "ENCRYPT"));
        assert!(!calls_function("SELECT AES_ENCRYPT('X','K')", "ENCRYPT"));
        assert!(!calls_function("SELECT PASSWORD_HASH FROM T", "PASSWORD"));
        assert!(!calls_function("SELECT USER_PASSWORD FROM T", "PASSWORD"));
    }

    #[test]
    fn routine_severity_distinguishes_removed_and_deprecated() {
        let routines = vec![("p".to_string(), "PROCEDURE".to_string(), "begin select password('x'); select found_rows(); end".to_string())];
        assert_eq!(
            types(&check_routines("app", &routines)),
            vec![("deprecated_function", "error", "PROCEDURE app.p"), ("deprecated_function", "warning", "PROCEDURE app.p")]
        );
    }

    #[test]
    fn column_type_checks() {
        let columns = vec![
            col("t", "z", "int", "int(10) unsigned zerofill"),
            col("t", "f", "float", "float(7,2)"),
            col("t", "f2", "float", "float"),
            col("t", "y", "year", "year(2)"),
            col("t", "e", "enum", "enum('','a')"),
            col("t", "ts", "timestamp", "timestamp"),
            col("t", "flag", "tinyint", "tinyint(1)"),
            col("t", "n", "bigint", "bigint(20)"),
            col("t", "plain", "int", "int"),
        ];
        assert_eq!(types(&check_zerofill("app", &columns)), vec![("zerofill_usage", "warning", "app.t.z")]);
        assert_eq!(types(&check_float_precision("app", &columns)), vec![("float_precision", "warning", "app.t.f")]);
        assert_eq!(check_year2("app", &columns)[0].fix_query.as_deref(), Some("ALTER TABLE `app`.`t` MODIFY `y` YEAR;"));
        assert_eq!(types(&check_enum_empty("app", &columns)), vec![("enum_empty_value", "warning", "app.t.e")]);
        assert_eq!(check_timestamp_range("app", &columns)[0].fix_query.as_deref(), Some("ALTER TABLE `app`.`t` MODIFY `ts` DATETIME;"));
        let widths = check_int_display_width("app", &columns);
        assert_eq!(types(&widths), vec![("int_display_width", "info", "app.t.z"), ("int_display_width", "info", "app.t.n")]);
    }

    #[test]
    fn charset_keywords_engines_and_modes() {
        let tables = vec![
            TableFact { name: "rank".into(), engine: Some("MyISAM".into()), collation: Some("utf8mb3_general_ci".into()), rows: 0 },
            TableFact { name: "mem".into(), engine: Some("MEMORY".into()), collation: Some("utf8mb4_bin".into()), rows: 0 },
        ];
        let mut c = col("rank", "window", "varchar", "varchar(10)");
        c.charset = Some("utf8".into());
        let mut view_col = col("v", "x", "varchar", "varchar(10)");
        view_col.charset = Some("utf8mb3".into());
        view_col.base_table = false;
        let columns = vec![c, view_col];
        assert_eq!(
            types(&check_charset("app", &tables, &columns)),
            vec![("charset_issue", "warning", "app.rank"), ("charset_issue", "warning", "app.rank.window")]
        );
        assert_eq!(
            types(&check_reserved_keywords("app", &tables, &columns)),
            vec![("reserved_keyword", "error", "app.rank"), ("reserved_keyword", "warning", "app.rank.window")]
        );
        let engines = check_deprecated_engines("app", &tables);
        assert_eq!(engines[0].fix_query.as_deref(), Some("ALTER TABLE `app`.`rank` ENGINE=InnoDB;"));
        assert_eq!((engines[1].severity.as_str(), engines[1].fix_query.as_deref()), ("info", None));
        assert_eq!(check_sql_mode("STRICT_TRANS_TABLES,MYSQL40, NO_AUTO_CREATE_USER").len(), 2);
        let users = vec![("app".into(), "%".into(), "mysql_native_password".into()), ("old".into(), "h".into(), "sha256_password".into())];
        assert_eq!(types(&check_auth_plugins(&users)), vec![("auth_plugin_issue", "error", "'app'@'%'"), ("auth_plugin_issue", "warning", "'old'@'h'")]);
    }

    #[test]
    fn invalid_date_issue_text_and_fix() {
        let found = invalid_date_issue("app", "t", "d", 12345);
        assert_eq!(found.description, "잘못된 날짜값 12,345개 발견 (0000-00-00 등)");
        assert_eq!(
            found.fix_query.as_deref(),
            Some("UPDATE `app`.`t` SET `d` = NULL WHERE `d` = '0000-00-00' OR MONTH(`d`) = 0 OR DAY(`d`) = 0;")
        );
    }

    #[test]
    fn composite_fk_orphans_are_judged_per_constraint() {
        let rows = vec![
            ("child".to_string(), "fk_pair".to_string(), "a".to_string(), "parent".to_string(), "x".to_string()),
            ("child".to_string(), "fk_pair".to_string(), "b".to_string(), "parent".to_string(), "y".to_string()),
            ("child".to_string(), "fk_other".to_string(), "c".to_string(), "other".to_string(), "id".to_string()),
        ];
        let groups = group_foreign_keys(&rows);
        assert_eq!(groups.len(), 2);
        assert_eq!((groups[0].child_columns.clone(), groups[0].parent_columns.clone()), (vec!["a".to_string(), "b".to_string()], vec!["x".to_string(), "y".to_string()]));
        let sql = cleanup_sql("app", &groups[0]);
        assert!(sql.delete.contains("c.`a` IS NOT NULL\n    AND c.`b` IS NOT NULL"), "{}", sql.delete);
        assert!(sql.delete.contains("WHERE p.`x` = c.`a` AND p.`y` = c.`b`"), "{}", sql.delete);
        assert!(sql.set_null.contains("SET c.`a` = NULL, c.`b` = NULL"));
        assert!(sql.count.starts_with("SELECT COUNT(*) AS cnt FROM `app`.`child` AS c\nWHERE"));
        assert!(orphan_from_and_where("app", &groups[0], false).contains("LEFT JOIN `app`.`parent` p ON p.`x` = c.`a` AND p.`y` = c.`b`"));
        assert!(orphan_from_and_where("app", &groups[0], true).contains("NOT EXISTS"));
    }

    #[test]
    fn fk_tree_and_name_length() {
        let rel = |child: &str, parent: &str| FkColumnRelation {
            constraint_name: format!("fk_{child}"),
            child_table: child.into(),
            child_column: "pid".into(),
            parent_table: parent.into(),
            parent_column: "id".into(),
            on_delete: "RESTRICT".into(),
            on_update: "RESTRICT".into(),
        };
        let tree = build_fk_tree(&[rel("a", "p"), rel("b", "p"), rel("a", "p")]);
        assert_eq!(tree.get("p").unwrap(), &vec!["a".to_string(), "b".to_string()]);
        let long = ForeignKeyGroup {
            constraint_name: "x".repeat(65),
            child_table: "t".into(),
            parent_table: "p".into(),
            child_columns: vec!["c".into()],
            parent_columns: vec!["id".into()],
        };
        assert_eq!(check_fk_name_length("app", &[long])[0].description, "FK 이름이 64자 초과: 65자");
    }

    #[test]
    fn options_default_to_enabled() {
        let options = AnalyzeOptions::from_payload(&json!({"options": {"check_orphans": false}}));
        assert!(!options.orphans && options.charset && options.int_display_width);
    }
}
