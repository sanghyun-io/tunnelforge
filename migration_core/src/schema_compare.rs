//! `schema.compare`: 두 MySQL 스키마를 비교해 차이, 심각도, 동기화 SQL을 만든다.
//!
//! 덤프/마이그레이션용 `inspect`는 타입에 charset을 합치는 등 값을 정규화하므로,
//! 비교는 information_schema 원본 값을 그대로 읽는 전용 조회를 쓴다.
use crate::*;
use mysql::prelude::Queryable;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CompareLevel {
    Quick,
    Standard,
    Strict,
}

impl CompareLevel {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "quick" => Ok(Self::Quick),
            "standard" | "" => Ok(Self::Standard),
            "strict" => Ok(Self::Strict),
            other => Err(format!("unknown compare level: {other}")),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CmpColumn {
    pub name: String,
    pub column_type: String,
    pub nullable: bool,
    pub default: Option<String>,
    pub extra: String,
    pub key: String,
    pub charset: String,
    pub collation: String,
    pub comment: String,
    pub generation_expression: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CmpIndexPart {
    /// 컬럼 이름. 함수 기반 인덱스 파트는 빈 문자열이다.
    pub column: String,
    pub sub_part: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CmpIndex {
    pub name: String,
    pub parts: Vec<CmpIndexPart>,
    pub unique: bool,
    pub index_type: String,
}

impl CmpIndex {
    fn is_primary(&self) -> bool {
        self.name.eq_ignore_ascii_case("PRIMARY")
    }
    fn is_functional(&self) -> bool {
        self.parts.iter().any(|part| part.column.is_empty())
    }
    fn parts_text(&self) -> String {
        let parts: Vec<String> = self.parts.iter().map(part_text).collect();
        format!("({})", parts.join(", "))
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CmpForeignKey {
    pub name: String,
    pub columns: Vec<String>,
    pub ref_table: String,
    pub ref_columns: Vec<String>,
    pub on_delete: String,
    pub on_update: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CmpTable {
    pub name: String,
    pub columns: Vec<CmpColumn>,
    pub indexes: Vec<CmpIndex>,
    pub foreign_keys: Vec<CmpForeignKey>,
    pub engine: String,
    pub collation: String,
    pub row_count: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Info,
    Warning,
    Critical,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DiffType {
    Added,
    Removed,
    Modified,
    Renamed,
    Unchanged,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FieldChange {
    pub field: String,
    pub from: String,
    pub to: String,
    pub text: String,
}

fn change(field: &str, label: &str, from: impl Into<String>, to: impl Into<String>) -> FieldChange {
    let (from, to) = (from.into(), to.into());
    FieldChange { field: field.into(), text: format!("{label} {from} → {to}"), from, to }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ColumnDiff {
    pub name: String,
    pub diff_type: DiffType,
    pub severity: Option<Severity>,
    pub changes: Vec<FieldChange>,
    pub source: Option<CmpColumn>,
    pub target: Option<CmpColumn>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntityDiff<T> {
    pub name: String,
    pub diff_type: DiffType,
    pub severity: Option<Severity>,
    pub changes: Vec<FieldChange>,
    /// RENAMED 일 때 타겟 쪽 이전 이름
    pub old_name: Option<String>,
    pub source: Option<T>,
    pub target: Option<T>,
}

pub type IndexDiff = EntityDiff<CmpIndex>;
pub type ForeignKeyDiff = EntityDiff<CmpForeignKey>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TableDiff {
    pub name: String,
    pub diff_type: DiffType,
    pub severity: Option<Severity>,
    pub row_count_source: u64,
    pub row_count_target: u64,
    pub source: Option<CmpTable>,
    pub target: Option<CmpTable>,
    pub columns: Vec<ColumnDiff>,
    pub indexes: Vec<IndexDiff>,
    pub foreign_keys: Vec<ForeignKeyDiff>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct SeveritySummary {
    pub critical: u64,
    pub warning: u64,
    pub info: u64,
}

impl SeveritySummary {
    fn count(&mut self, severity: Option<Severity>) {
        match severity {
            Some(Severity::Critical) => self.critical += 1,
            Some(Severity::Warning) => self.warning += 1,
            Some(Severity::Info) => self.info += 1,
            None => {}
        }
    }
}

// ---------------------------------------------------------------------------
// 비교
// ---------------------------------------------------------------------------

/// MySQL 8.0 이 붙이는 DEFAULT_GENERATED 는 5.7 과의 거짓 diff 를 만들므로 비교 전에 뺀다.
fn normalize_extra(extra: &str) -> String {
    extra
        .split_whitespace()
        .filter(|word| !word.eq_ignore_ascii_case("DEFAULT_GENERATED"))
        .collect::<Vec<_>>()
        .join(" ")
}

fn default_text(value: &Option<String>) -> String {
    value.clone().unwrap_or_else(|| "None".to_string())
}

fn compare_columns(source: &[CmpColumn], target: &[CmpColumn], level: CompareLevel) -> Vec<ColumnDiff> {
    let source_map: BTreeMap<String, &CmpColumn> = source.iter().map(|c| (c.name.to_lowercase(), c)).collect();
    let target_map: BTreeMap<String, &CmpColumn> = target.iter().map(|c| (c.name.to_lowercase(), c)).collect();
    let names: BTreeSet<&String> = source_map.keys().chain(target_map.keys()).collect();
    names
        .into_iter()
        .map(|key| match (source_map.get(key), target_map.get(key)) {
            (Some(src), None) => column_diff(src.name.clone(), DiffType::Added, vec![], Some(src), None),
            (None, Some(tgt)) => column_diff(tgt.name.clone(), DiffType::Removed, vec![], None, Some(tgt)),
            (Some(src), Some(tgt)) => {
                let changes = column_changes(src, tgt, level);
                let kind = if changes.is_empty() { DiffType::Unchanged } else { DiffType::Modified };
                column_diff(src.name.clone(), kind, changes, Some(src), Some(tgt))
            }
            (None, None) => unreachable!(),
        })
        .collect()
}

fn column_diff(
    name: String,
    diff_type: DiffType,
    changes: Vec<FieldChange>,
    source: Option<&&CmpColumn>,
    target: Option<&&CmpColumn>,
) -> ColumnDiff {
    ColumnDiff {
        name,
        diff_type,
        severity: None,
        changes,
        source: source.map(|c| (*c).clone()),
        target: target.map(|c| (*c).clone()),
    }
}

fn column_changes(src: &CmpColumn, tgt: &CmpColumn, level: CompareLevel) -> Vec<FieldChange> {
    let mut changes = Vec::new();
    if !src.column_type.eq_ignore_ascii_case(&tgt.column_type) {
        changes.push(change("type", "타입:", &src.column_type, &tgt.column_type));
    }
    if level != CompareLevel::Quick {
        if src.nullable != tgt.nullable {
            let label = |nullable: bool| if nullable { "NULL" } else { "NOT NULL" };
            changes.push(change("nullable", "Nullable:", label(src.nullable), label(tgt.nullable)));
        }
        if src.default != tgt.default {
            changes.push(change("default", "Default:", default_text(&src.default), default_text(&tgt.default)));
        }
        let (src_extra, tgt_extra) = (normalize_extra(&src.extra), normalize_extra(&tgt.extra));
        if !src_extra.eq_ignore_ascii_case(&tgt_extra) {
            changes.push(change("extra", "Extra:", src_extra, tgt_extra));
        }
    }
    if level == CompareLevel::Strict {
        // 한쪽만 값이 있는 경우(문자열이 아닌 컬럼 등)는 비교하지 않는다.
        if !src.charset.is_empty() && !tgt.charset.is_empty() && !src.charset.eq_ignore_ascii_case(&tgt.charset) {
            changes.push(change("charset", "Charset:", &src.charset, &tgt.charset));
        }
        if !src.collation.is_empty()
            && !tgt.collation.is_empty()
            && !src.collation.eq_ignore_ascii_case(&tgt.collation)
        {
            changes.push(change("collation", "Collation:", &src.collation, &tgt.collation));
        }
    }
    changes
}

fn part_text(part: &CmpIndexPart) -> String {
    let column = if part.column.is_empty() { "<expression>" } else { &part.column };
    match part.sub_part {
        Some(length) => format!("{column}({length})"),
        None => column.to_string(),
    }
}

fn index_content_key(index: &CmpIndex) -> String {
    let parts: Vec<String> = index.parts.iter().map(|p| part_text(p).to_lowercase()).collect();
    format!("{}|{}|{}", parts.join(","), index.unique, index.index_type.to_uppercase())
}

fn fk_content_key(fk: &CmpForeignKey) -> String {
    let lower = |values: &[String]| values.iter().map(|v| v.to_lowercase()).collect::<Vec<_>>().join(",");
    format!(
        "{}|{}|{}|{}|{}",
        lower(&fk.columns),
        fk.ref_table.to_lowercase(),
        lower(&fk.ref_columns),
        fk.on_delete.to_uppercase(),
        fk.on_update.to_uppercase()
    )
}

fn index_changes(src: &CmpIndex, tgt: &CmpIndex) -> Vec<FieldChange> {
    let mut changes = Vec::new();
    if src.parts != tgt.parts {
        changes.push(change("columns", "컬럼:", src.parts_text(), tgt.parts_text()));
    }
    if src.unique != tgt.unique {
        changes.push(change("unique", "Unique:", src.unique.to_string(), tgt.unique.to_string()));
    }
    if !src.index_type.eq_ignore_ascii_case(&tgt.index_type) {
        changes.push(change("index_type", "Type:", &src.index_type, &tgt.index_type));
    }
    changes
}

fn fk_changes(src: &CmpForeignKey, tgt: &CmpForeignKey) -> Vec<FieldChange> {
    let mut changes = Vec::new();
    if src.ref_table != tgt.ref_table {
        changes.push(change("ref_table", "참조 테이블:", &src.ref_table, &tgt.ref_table));
    }
    if src.columns != tgt.columns {
        changes.push(change("columns", "컬럼:", src.columns.join(", "), tgt.columns.join(", ")));
    }
    if src.ref_columns != tgt.ref_columns {
        changes.push(change("ref_columns", "참조 컬럼:", src.ref_columns.join(", "), tgt.ref_columns.join(", ")));
    }
    if src.on_delete != tgt.on_delete {
        changes.push(change("on_delete", "ON DELETE:", &src.on_delete, &tgt.on_delete));
    }
    if src.on_update != tgt.on_update {
        changes.push(change("on_update", "ON UPDATE:", &src.on_update, &tgt.on_update));
    }
    changes
}

trait Named: Clone {
    fn name(&self) -> &str;
}
impl Named for CmpIndex {
    fn name(&self) -> &str {
        &self.name
    }
}
impl Named for CmpForeignKey {
    fn name(&self) -> &str {
        &self.name
    }
}

/// 이름 매칭 → 내용이 같은 미매칭 쌍은 RENAMED → 나머지는 ADDED/REMOVED.
fn compare_named<T: Named>(
    source: &[T],
    target: &[T],
    content_key: fn(&T) -> String,
    changes_of: fn(&T, &T) -> Vec<FieldChange>,
) -> Vec<EntityDiff<T>> {
    let source_map: BTreeMap<String, &T> = source.iter().map(|e| (e.name().to_lowercase(), e)).collect();
    let target_map: BTreeMap<String, &T> = target.iter().map(|e| (e.name().to_lowercase(), e)).collect();
    let entity = |name: &str, diff_type, changes, old_name, src: Option<&T>, tgt: Option<&T>| EntityDiff {
        name: name.to_string(),
        diff_type,
        severity: None,
        changes,
        old_name,
        source: src.cloned(),
        target: tgt.cloned(),
    };
    let mut diffs = Vec::new();
    for (key, src) in &source_map {
        if let Some(tgt) = target_map.get(key) {
            let changes = changes_of(src, tgt);
            let kind = if changes.is_empty() { DiffType::Unchanged } else { DiffType::Modified };
            diffs.push(entity(src.name(), kind, changes, None, Some(*src), Some(*tgt)));
        }
    }
    let mut unmatched_target: Vec<&T> =
        target_map.iter().filter(|(key, _)| !source_map.contains_key(*key)).map(|(_, t)| *t).collect();
    let mut added = Vec::new();
    for (_, src) in source_map.iter().filter(|(key, _)| !target_map.contains_key(*key)) {
        let wanted = content_key(src);
        if let Some(position) = unmatched_target.iter().position(|tgt| content_key(tgt) == wanted) {
            let tgt = unmatched_target.remove(position);
            let text = format!("이름 변경: {} → {}", tgt.name(), src.name());
            let changes = vec![FieldChange {
                field: "name".into(),
                from: tgt.name().into(),
                to: src.name().into(),
                text,
            }];
            diffs.push(entity(src.name(), DiffType::Renamed, changes, Some(tgt.name().to_string()), Some(*src), Some(tgt)));
        } else {
            added.push(*src);
        }
    }
    for src in added {
        diffs.push(entity(src.name(), DiffType::Added, vec![], None, Some(src), None));
    }
    for tgt in unmatched_target {
        diffs.push(entity(tgt.name(), DiffType::Removed, vec![], None, None, Some(tgt)));
    }
    diffs
}

pub fn compare_table(source: &CmpTable, target: &CmpTable, level: CompareLevel) -> TableDiff {
    let columns = compare_columns(&source.columns, &target.columns, level);
    let (indexes, foreign_keys) = if level == CompareLevel::Quick {
        (Vec::new(), Vec::new())
    } else {
        (
            compare_named(&source.indexes, &target.indexes, index_content_key, index_changes),
            compare_named(&source.foreign_keys, &target.foreign_keys, fk_content_key, fk_changes),
        )
    };
    let changed = columns.iter().any(|d| d.diff_type != DiffType::Unchanged)
        || indexes.iter().any(|d| d.diff_type != DiffType::Unchanged)
        || foreign_keys.iter().any(|d| d.diff_type != DiffType::Unchanged);
    TableDiff {
        name: source.name.clone(),
        diff_type: if changed { DiffType::Modified } else { DiffType::Unchanged },
        severity: None,
        row_count_source: source.row_count,
        row_count_target: target.row_count,
        source: Some(source.clone()),
        target: Some(target.clone()),
        columns,
        indexes,
        foreign_keys,
    }
}

pub fn compare_schemas(source: &[CmpTable], target: &[CmpTable], level: CompareLevel) -> Vec<TableDiff> {
    let source_map: BTreeMap<&str, &CmpTable> = source.iter().map(|t| (t.name.as_str(), t)).collect();
    let target_map: BTreeMap<&str, &CmpTable> = target.iter().map(|t| (t.name.as_str(), t)).collect();
    let names: BTreeSet<&str> = source_map.keys().chain(target_map.keys()).copied().collect();
    names
        .into_iter()
        .map(|name| match (source_map.get(name), target_map.get(name)) {
            (Some(src), Some(tgt)) => compare_table(src, tgt, level),
            (Some(src), None) => table_only(src, DiffType::Added),
            (None, Some(tgt)) => table_only(tgt, DiffType::Removed),
            (None, None) => unreachable!(),
        })
        .collect()
}

fn table_only(table: &CmpTable, diff_type: DiffType) -> TableDiff {
    let added = diff_type == DiffType::Added;
    TableDiff {
        name: table.name.clone(),
        diff_type,
        severity: None,
        row_count_source: if added { table.row_count } else { 0 },
        row_count_target: if added { 0 } else { table.row_count },
        source: added.then(|| table.clone()),
        target: (!added).then(|| table.clone()),
        columns: Vec::new(),
        indexes: Vec::new(),
        foreign_keys: Vec::new(),
    }
}

// ---------------------------------------------------------------------------
// 심각도
// ---------------------------------------------------------------------------

const INTEGER_TYPES: [&str; 6] = ["tinyint", "smallint", "mediumint", "int", "integer", "bigint"];

fn strip_parenthesized(value: &str, digits_only: bool) -> String {
    let mut out = String::new();
    let mut chars = value.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '(' {
            let inner: String = chars.by_ref().take_while(|c| *c != ')').collect();
            if digits_only && !(!inner.is_empty() && inner.chars().all(|c| c.is_ascii_digit())) {
                out.push('(');
                out.push_str(&inner);
                out.push(')');
            }
        } else {
            out.push(ch);
        }
    }
    out.trim().to_lowercase()
}

/// int(11) 과 int 처럼 정수 타입의 표시 폭만 다른지.
pub fn is_display_width_only_diff(source: &str, target: &str) -> bool {
    let (src, tgt) = (strip_parenthesized(source, true), strip_parenthesized(target, true));
    src == tgt && src.split_whitespace().next().is_some_and(|base| INTEGER_TYPES.contains(&base))
}

fn type_change_severity(source: &str, target: &str) -> Severity {
    if is_display_width_only_diff(source, target) {
        Severity::Info
    } else if strip_parenthesized(source, false) != strip_parenthesized(target, false) {
        Severity::Critical
    } else {
        Severity::Warning
    }
}

fn column_severity(diff: &ColumnDiff) -> Option<Severity> {
    match diff.diff_type {
        DiffType::Unchanged => None,
        DiffType::Added | DiffType::Removed => Some(Severity::Critical),
        _ => diff
            .changes
            .iter()
            .map(|c| match c.field.as_str() {
                "type" => type_change_severity(&c.from, &c.to),
                "extra" if c.text.to_lowercase().contains("auto_increment") => Severity::Critical,
                _ => Severity::Warning,
            })
            .max(),
    }
}

fn index_severity(diff: &IndexDiff) -> Option<Severity> {
    match diff.diff_type {
        DiffType::Unchanged => None,
        DiffType::Renamed => Some(Severity::Info),
        _ if diff.name.eq_ignore_ascii_case("PRIMARY") => Some(Severity::Critical),
        _ => Some(Severity::Warning),
    }
}

fn fk_severity(diff: &ForeignKeyDiff) -> Option<Severity> {
    match diff.diff_type {
        DiffType::Unchanged => None,
        DiffType::Renamed => Some(Severity::Info),
        _ => Some(Severity::Warning),
    }
}

pub fn classify(diffs: &mut [TableDiff]) -> SeveritySummary {
    let mut summary = SeveritySummary::default();
    for table in diffs.iter_mut() {
        table.severity = matches!(table.diff_type, DiffType::Added | DiffType::Removed).then_some(Severity::Critical);
        summary.count(table.severity);
        for column in &mut table.columns {
            column.severity = column_severity(column);
            summary.count(column.severity);
        }
        for index in &mut table.indexes {
            index.severity = index_severity(index);
            summary.count(index.severity);
        }
        for fk in &mut table.foreign_keys {
            fk.severity = fk_severity(fk);
            summary.count(fk.severity);
        }
    }
    summary
}

// ---------------------------------------------------------------------------
// 동기화 SQL
// ---------------------------------------------------------------------------

fn q(name: &str) -> String {
    format!("`{}`", name.replace('`', "``"))
}

fn sql_string(value: &str) -> String {
    format!("'{}'", value.replace('\\', "\\\\").replace('\'', "''"))
}

/// COLUMN_DEFAULT 가 식(CURRENT_TIMESTAMP, b'0', (expr) 등)인지. 식이면 따옴표 없이 쓴다.
fn default_is_expression(column: &CmpColumn, value: &str) -> bool {
    let upper = value.trim().to_uppercase();
    let timestamp_fn = ["CURRENT_TIMESTAMP", "NOW", "LOCALTIME", "LOCALTIMESTAMP"].iter().any(|name| {
        upper == *name
            || (upper.starts_with(&format!("{name}("))
                && upper.ends_with(')')
                && upper[name.len() + 1..upper.len() - 1].chars().all(|c| c.is_ascii_digit()))
    });
    timestamp_fn
        || upper == "NULL"
        || column.extra.to_uppercase().contains("DEFAULT_GENERATED")
        || (upper.starts_with("B'") && upper.ends_with('\''))
        || (upper.starts_with('(') && upper.ends_with(')'))
}

fn column_definition(column: &CmpColumn) -> String {
    let mut parts = vec![q(&column.name), column.column_type.clone()];
    let lowered_type = column.column_type.to_lowercase();
    if !column.charset.is_empty() && !lowered_type.contains("character set") {
        parts.push(format!("CHARACTER SET {}", column.charset));
        if !column.collation.is_empty() {
            parts.push(format!("COLLATE {}", column.collation));
        }
    }
    let extra = normalize_extra(&column.extra);
    if !column.generation_expression.is_empty() {
        let kind = if extra.to_uppercase().contains("STORED") { "STORED" } else { "VIRTUAL" };
        parts.push(format!("GENERATED ALWAYS AS ({}) {kind}", column.generation_expression));
    }
    parts.push(if column.nullable { "NULL" } else { "NOT NULL" }.to_string());
    if column.generation_expression.is_empty() {
        if let Some(default) = &column.default {
            if default_is_expression(column, default) {
                parts.push(format!("DEFAULT {default}"));
            } else {
                parts.push(format!("DEFAULT {}", sql_string(default)));
            }
        }
        if !extra.is_empty() {
            parts.push(extra);
        }
    }
    if !column.comment.is_empty() {
        parts.push(format!("COMMENT {}", sql_string(&column.comment)));
    }
    parts.join(" ")
}

fn index_parts_sql(index: &CmpIndex) -> String {
    let parts: Vec<String> = index
        .parts
        .iter()
        .map(|part| match part.sub_part {
            Some(length) => format!("{}({length})", q(&part.column)),
            None => q(&part.column),
        })
        .collect();
    format!("({})", parts.join(", "))
}

fn index_definition(index: &CmpIndex) -> String {
    let kind = index.index_type.to_uppercase();
    if index.is_primary() {
        format!("PRIMARY KEY {}", index_parts_sql(index))
    } else if kind == "FULLTEXT" || kind == "SPATIAL" {
        format!("{kind} INDEX {} {}", q(&index.name), index_parts_sql(index))
    } else {
        let unique = if index.unique { "UNIQUE " } else { "" };
        let using = if kind.is_empty() { String::new() } else { format!(" USING {kind}") };
        format!("{unique}INDEX {} {}{using}", q(&index.name), index_parts_sql(index))
    }
}

fn fk_definition(fk: &CmpForeignKey) -> String {
    let list = |names: &[String]| names.iter().map(|n| q(n)).collect::<Vec<_>>().join(", ");
    format!(
        "CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {} ON UPDATE {}",
        q(&fk.name),
        list(&fk.columns),
        q(&fk.ref_table),
        list(&fk.ref_columns),
        fk.on_delete,
        fk.on_update
    )
}

fn charset_of_collation(collation: &str) -> &str {
    collation.split('_').next().unwrap_or("")
}

fn create_table_sql(schema: &str, table: &CmpTable) -> String {
    let mut defs: Vec<String> = table.columns.iter().map(|c| format!("    {}", column_definition(c))).collect();
    if let Some(primary) = table.indexes.iter().find(|i| i.is_primary()) {
        defs.push(format!("    {}", index_definition(primary)));
    }
    for index in table.indexes.iter().filter(|i| !i.is_primary()) {
        if index.is_functional() {
            defs.push(format!("    -- 함수 기반 인덱스 {}: 수동으로 만들어야 합니다", q(&index.name)));
        } else {
            defs.push(format!("    {}", index_definition(index)));
        }
    }
    // FK 는 참조 테이블이 모두 만들어진 뒤 FK 추가 단계에서 한 번만 만든다.
    let mut options = Vec::new();
    if !table.engine.is_empty() {
        options.push(format!("ENGINE={}", table.engine));
    }
    if !table.collation.is_empty() {
        options.push(format!("DEFAULT CHARSET={}", charset_of_collation(&table.collation)));
        options.push(format!("COLLATE={}", table.collation));
    }
    let body = join_defs(&defs);
    format!("CREATE TABLE {}.{} (\n{}\n) {};", q(schema), q(&table.name), body, options.join(" "))
}

/// 주석 줄 뒤에는 쉼표를 붙이지 않는다.
fn join_defs(defs: &[String]) -> String {
    let real: Vec<usize> = defs.iter().enumerate().filter(|(_, d)| !d.trim_start().starts_with("--")).map(|(i, _)| i).collect();
    let last_real = real.last().copied();
    defs.iter()
        .enumerate()
        .map(|(i, d)| if Some(i) != last_real && !d.trim_start().starts_with("--") { format!("{d},") } else { d.clone() })
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn generate_sync_sql(diffs: &[TableDiff], target_schema: &str) -> String {
    let alter = |table: &str, clause: String| format!("ALTER TABLE {}.{} {clause}", q(target_schema), q(table));
    let mut fk_drops = Vec::new();
    let mut table_drops = Vec::new();
    let mut creates = Vec::new();
    let mut alters = Vec::new();
    let mut fk_adds = Vec::new();

    for diff in diffs {
        match diff.diff_type {
            DiffType::Removed => {
                if let Some(target) = &diff.target {
                    for fk in &target.foreign_keys {
                        fk_drops.push(alter(&diff.name, format!("DROP FOREIGN KEY {};", q(&fk.name))));
                    }
                }
                table_drops.push(format!("DROP TABLE IF EXISTS {}.{};", q(target_schema), q(&diff.name)));
            }
            DiffType::Added => {
                if let Some(source) = &diff.source {
                    creates.push(create_table_sql(target_schema, source));
                    for fk in &source.foreign_keys {
                        fk_adds.push(alter(&diff.name, format!("ADD {};", fk_definition(fk))));
                    }
                }
            }
            DiffType::Modified => {
                for fk in &diff.foreign_keys {
                    match fk.diff_type {
                        DiffType::Removed | DiffType::Modified => {
                            fk_drops.push(alter(&diff.name, format!("DROP FOREIGN KEY {};", q(&fk.name))));
                        }
                        DiffType::Renamed => {
                            if let Some(old) = &fk.old_name {
                                fk_drops.push(alter(&diff.name, format!("DROP FOREIGN KEY {}; -- renamed → {}", q(old), fk.name)));
                            }
                        }
                        _ => {}
                    }
                    if matches!(fk.diff_type, DiffType::Added | DiffType::Modified | DiffType::Renamed) {
                        if let Some(source) = &fk.source {
                            fk_adds.push(alter(&diff.name, format!("ADD {};", fk_definition(source))));
                        }
                    }
                }
                alters.extend(table_alters(diff, &alter));
            }
            _ => {}
        }
    }

    let mut lines = vec![
        "-- =======================================================".to_string(),
        "-- 스키마 동기화 스크립트".to_string(),
        format!("-- 타겟: {target_schema}"),
        "-- 주의: 실행 전 반드시 백업을 수행하세요!".to_string(),
        "-- =======================================================".to_string(),
        String::new(),
        "SET FOREIGN_KEY_CHECKS = 0;".to_string(),
        String::new(),
    ];
    for (title, section) in [
        ("-- FK 삭제", fk_drops),
        ("-- 테이블 삭제", table_drops),
        ("-- 테이블 생성", creates),
        ("-- 컬럼/인덱스 변경", alters),
        ("-- FK 추가", fk_adds),
    ] {
        if !section.is_empty() {
            lines.push(title.to_string());
            lines.extend(section);
            lines.push(String::new());
        }
    }
    lines.push("SET FOREIGN_KEY_CHECKS = 1;".to_string());
    lines.push(String::new());
    lines.push("-- 스크립트 끝".to_string());
    lines.join("\n")
}

/// 수정된 테이블의 ALTER 문. 인덱스/PK 삭제 → 컬럼 변경 → 인덱스/PK 추가 순서라
/// 지울 컬럼에 걸린 인덱스나 새 컬럼에 거는 인덱스도 실패하지 않는다.
fn table_alters(diff: &TableDiff, alter: &dyn Fn(&str, String) -> String) -> Vec<String> {
    let mut drops = Vec::new();
    let mut columns = Vec::new();
    let mut adds = Vec::new();
    let pk_warning = "-- 주의: PRIMARY KEY 변경입니다. AUTO_INCREMENT 컬럼이나 이 키를 참조하는 FK 가 있으면 실패할 수 있습니다.";

    for column in &diff.columns {
        match (column.diff_type, &column.source) {
            (DiffType::Added, Some(source)) => columns.push(alter(&diff.name, format!("ADD COLUMN {};", column_definition(source)))),
            (DiffType::Removed, _) => columns.push(alter(&diff.name, format!("DROP COLUMN {};", q(&column.name)))),
            (DiffType::Modified, Some(source)) => {
                columns.push(alter(&diff.name, format!("MODIFY COLUMN {};", column_definition(source))))
            }
            _ => {}
        }
    }
    for index in &diff.indexes {
        let source = index.source.as_ref();
        if index.name.eq_ignore_ascii_case("PRIMARY") {
            match (index.diff_type, source) {
                (DiffType::Added, Some(src)) => {
                    adds.push(pk_warning.to_string());
                    adds.push(alter(&diff.name, format!("ADD {};", index_definition(src))));
                }
                (DiffType::Removed, _) => {
                    drops.push(pk_warning.to_string());
                    drops.push(alter(&diff.name, "DROP PRIMARY KEY;".to_string()));
                }
                (DiffType::Modified, Some(src)) => {
                    drops.push(pk_warning.to_string());
                    drops.push(alter(&diff.name, format!("DROP PRIMARY KEY, ADD {};", index_definition(src))));
                }
                _ => {}
            }
            continue;
        }
        if source.is_some_and(CmpIndex::is_functional) && index.diff_type != DiffType::Renamed {
            adds.push(format!("-- 함수 기반 인덱스 {}: 수동으로 처리해야 합니다", q(&index.name)));
            continue;
        }
        match (index.diff_type, source) {
            (DiffType::Added, Some(src)) => adds.push(alter(&diff.name, format!("ADD {};", index_definition(src)))),
            (DiffType::Removed, _) => drops.push(alter(&diff.name, format!("DROP INDEX {};", q(&index.name)))),
            (DiffType::Modified, Some(src)) => {
                drops.push(alter(&diff.name, format!("DROP INDEX {};", q(&index.name))));
                adds.push(alter(&diff.name, format!("ADD {};", index_definition(src))));
            }
            (DiffType::Renamed, _) => {
                if let Some(old) = &index.old_name {
                    adds.push(alter(&diff.name, format!("RENAME INDEX {} TO {};", q(old), q(&index.name))));
                }
            }
            _ => {}
        }
    }
    drops.into_iter().chain(columns).chain(adds).collect()
}

// ---------------------------------------------------------------------------
// MySQL 조회
// ---------------------------------------------------------------------------

type ColumnRow = (String, String, String, String, Option<String>, String, String, Option<String>, Option<String>, String, Option<String>);

/// information_schema 원본 값으로 비교용 스키마를 읽는다. 쿼리가 하나라도 실패하면
/// 빈 결과 대신 오류를 돌려준다(빈 결과는 "모든 테이블 삭제" 동기화 SQL 로 이어진다).
pub fn extract_mysql_tables(
    conn: &mut mysql::PooledConn,
    schema: &str,
    exact_row_counts: bool,
) -> Result<Vec<CmpTable>, String> {
    // TABLE_ROWS 추정치를 최신으로 (5.7/MariaDB 에는 없는 변수라 실패해도 무시)
    let _ = conn.query_drop("SET SESSION information_schema_stats_expiry=0");
    let tables: Vec<(String, Option<String>, Option<String>, Option<u64>)> = conn
        .exec(
            "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION, TABLE_ROWS FROM information_schema.TABLES \
             WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
            (schema,),
        )
        .map_err(|err| format!("table list query failed: {err}"))?;
    let mut by_name: BTreeMap<String, CmpTable> = tables
        .into_iter()
        .map(|(name, engine, collation, rows)| {
            let table = CmpTable {
                name: name.clone(),
                engine: engine.unwrap_or_default(),
                collation: collation.unwrap_or_default(),
                row_count: rows.unwrap_or(0),
                ..Default::default()
            };
            (name, table)
        })
        .collect();

    let columns: Vec<ColumnRow> = conn
        .exec(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_KEY, \
             CHARACTER_SET_NAME, COLLATION_NAME, COLUMN_COMMENT, GENERATION_EXPRESSION \
             FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (schema,),
        )
        .map_err(|err| format!("column query failed: {err}"))?;
    for (table, name, column_type, nullable, default, extra, key, charset, collation, comment, generation) in columns {
        if let Some(entry) = by_name.get_mut(&table) {
            entry.columns.push(CmpColumn {
                name,
                column_type,
                nullable: nullable.eq_ignore_ascii_case("YES"),
                default,
                extra,
                key,
                charset: charset.unwrap_or_default(),
                collation: collation.unwrap_or_default(),
                comment,
                generation_expression: generation.unwrap_or_default(),
            });
        }
    }

    let statistics: Vec<(String, String, i64, Option<String>, Option<u64>, String)> = conn
        .exec(
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SUB_PART, INDEX_TYPE \
             FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = ? \
             ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
            (schema,),
        )
        .map_err(|err| format!("index query failed: {err}"))?;
    for (table, name, non_unique, column, sub_part, index_type) in statistics {
        let Some(entry) = by_name.get_mut(&table) else { continue };
        let part = CmpIndexPart { column: column.unwrap_or_default(), sub_part };
        match entry.indexes.iter_mut().find(|index| index.name == name) {
            Some(index) => index.parts.push(part),
            None => entry.indexes.push(CmpIndex { name, parts: vec![part], unique: non_unique == 0, index_type }),
        }
    }

    let fks: Vec<(String, String, String, String, String, String, String)> = conn
        .exec(
            "SELECT kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, \
             kcu.REFERENCED_COLUMN_NAME, rc.DELETE_RULE, rc.UPDATE_RULE \
             FROM information_schema.KEY_COLUMN_USAGE kcu \
             JOIN information_schema.REFERENTIAL_CONSTRAINTS rc \
               ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME \
              AND rc.TABLE_NAME = kcu.TABLE_NAME \
             WHERE kcu.TABLE_SCHEMA = ? AND kcu.REFERENCED_TABLE_NAME IS NOT NULL \
             ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION",
            (schema,),
        )
        .map_err(|err| format!("foreign key query failed: {err}"))?;
    for (table, name, column, ref_table, ref_column, on_delete, on_update) in fks {
        let Some(entry) = by_name.get_mut(&table) else { continue };
        match entry.foreign_keys.iter_mut().find(|fk| fk.name == name) {
            Some(fk) => {
                fk.columns.push(column);
                fk.ref_columns.push(ref_column);
            }
            None => entry.foreign_keys.push(CmpForeignKey {
                name,
                columns: vec![column],
                ref_table,
                ref_columns: vec![ref_column],
                on_delete,
                on_update,
            }),
        }
    }

    if exact_row_counts {
        for table in by_name.values_mut() {
            let count: Option<u64> = conn
                .query_first(format!("SELECT COUNT(*) FROM {}.{}", q(schema), q(&table.name)))
                .map_err(|err| format!("row count failed for {}: {err}", table.name))?;
            table.row_count = count.unwrap_or(0);
        }
    }
    Ok(by_name.into_values().collect())
}

// ---------------------------------------------------------------------------
// protocol
// ---------------------------------------------------------------------------

fn compare_endpoint(payload: &Value, key: &str) -> Result<Endpoint, String> {
    let value = payload.get(key).ok_or_else(|| format!("missing {key} endpoint"))?;
    let endpoint = endpoint_from_value(value)?;
    if endpoint.engine != "mysql" {
        return Err("schema.compare currently supports MySQL only".to_string());
    }
    Ok(endpoint)
}

/// `schema.compare` 요청 처리. progress 이벤트를 보내고 result 또는 error 로 끝난다.
pub fn schema_compare<F: FnMut(Value)>(request: &Request, mut emit: F) {
    let request_id = request.request_id.clone();
    let mut progress = |phase: &str, message: &str| {
        emit(json!({"event": "progress", "request_id": request_id, "command": "schema.compare", "phase": phase, "message": message}))
    };
    let result = (|| -> Result<Value, String> {
        let payload = &request.payload;
        let level = CompareLevel::parse(payload.get("level").and_then(Value::as_str).unwrap_or(""))?;
        let exact = payload.get("exact_row_counts").and_then(Value::as_bool).unwrap_or(false);
        let source = compare_endpoint(payload, "source")?;
        let target = compare_endpoint(payload, "target")?;
        let (source_schema, target_schema) = (endpoint_schema(&source), endpoint_schema(&target));

        progress("source", "소스 스키마 추출 중...");
        let mut source_conn = mysql_conn(&source)?;
        let source_version: String = source_conn.query_first("SELECT VERSION()").map_err(|e| e.to_string())?.unwrap_or_default();
        let source_tables = extract_mysql_tables(&mut source_conn, &source_schema, exact)
            .map_err(|err| format!("source schema: {err}"))?;
        drop(source_conn);

        progress("target", "타겟 스키마 추출 중...");
        let mut target_conn = mysql_conn(&target)?;
        let target_version: String = target_conn.query_first("SELECT VERSION()").map_err(|e| e.to_string())?.unwrap_or_default();
        let target_tables = extract_mysql_tables(&mut target_conn, &target_schema, exact)
            .map_err(|err| format!("target schema: {err}"))?;
        drop(target_conn);

        progress("compare", "스키마 비교 중...");
        let mut diffs = compare_schemas(&source_tables, &target_tables, level);
        let summary = classify(&mut diffs);
        let sync_sql = generate_sync_sql(&diffs, &target_schema);
        Ok(json!({
            "level": level,
            "source_version": source_version,
            "target_version": target_version,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "row_counts_exact": exact,
            "tables": diffs,
            "summary": summary,
            "sync_sql": sync_sql,
        }))
    })();
    match result {
        Ok(Value::Object(mut fields)) => {
            fields.insert("event".into(), json!("result"));
            fields.insert("request_id".into(), json!(request.request_id));
            fields.insert("command".into(), json!("schema.compare"));
            fields.insert("success".into(), json!(true));
            emit(Value::Object(fields));
        }
        Ok(_) => unreachable!(),
        Err(message) => emit(json!({"event": "error", "request_id": request.request_id, "command": "schema.compare", "message": message})),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn col(name: &str, column_type: &str) -> CmpColumn {
        CmpColumn { name: name.into(), column_type: column_type.into(), nullable: true, ..Default::default() }
    }

    fn index(name: &str, columns: &[&str], unique: bool) -> CmpIndex {
        CmpIndex {
            name: name.into(),
            parts: columns.iter().map(|c| CmpIndexPart { column: (*c).into(), sub_part: None }).collect(),
            unique,
            index_type: "BTREE".into(),
        }
    }

    fn fk(name: &str, column: &str, ref_table: &str) -> CmpForeignKey {
        CmpForeignKey {
            name: name.into(),
            columns: vec![column.into()],
            ref_table: ref_table.into(),
            ref_columns: vec!["id".into()],
            on_delete: "RESTRICT".into(),
            on_update: "RESTRICT".into(),
        }
    }

    fn table(name: &str, columns: Vec<CmpColumn>) -> CmpTable {
        CmpTable { name: name.into(), columns, engine: "InnoDB".into(), collation: "utf8mb4_0900_ai_ci".into(), ..Default::default() }
    }

    fn single_column_diff(src: CmpColumn, tgt: CmpColumn, level: CompareLevel) -> ColumnDiff {
        let mut diffs = compare_schemas(&[table("t", vec![src])], &[table("t", vec![tgt])], level);
        classify(&mut diffs);
        diffs.remove(0).columns.remove(0)
    }

    #[test]
    fn tables_only_on_one_side_are_added_or_removed_and_critical() {
        let mut diffs = compare_schemas(&[table("a", vec![])], &[table("b", vec![])], CompareLevel::Standard);
        let summary = classify(&mut diffs);
        assert_eq!(diffs.iter().map(|d| (d.name.as_str(), d.diff_type)).collect::<Vec<_>>(), vec![("a", DiffType::Added), ("b", DiffType::Removed)]);
        assert!(diffs.iter().all(|d| d.severity == Some(Severity::Critical)));
        assert_eq!(summary.critical, 2);
    }

    #[test]
    fn unchanged_tables_have_no_severity() {
        let mut diffs = compare_schemas(&[table("t", vec![col("id", "int")])], &[table("t", vec![col("id", "int")])], CompareLevel::Standard);
        let summary = classify(&mut diffs);
        assert_eq!(diffs[0].diff_type, DiffType::Unchanged);
        assert_eq!(diffs[0].severity, None);
        assert_eq!(summary, SeveritySummary::default());
    }

    #[test]
    fn columns_match_case_insensitively() {
        let diff = single_column_diff(col("ID", "int"), col("id", "int"), CompareLevel::Standard);
        assert_eq!(diff.diff_type, DiffType::Unchanged);
    }

    #[test]
    fn column_type_severity_rules() {
        let sev = |a: &str, b: &str| single_column_diff(col("c", a), col("c", b), CompareLevel::Standard).severity;
        assert_eq!(sev("varchar(10)", "int"), Some(Severity::Critical));
        assert_eq!(sev("int(11)", "int"), Some(Severity::Info));
        assert_eq!(sev("bigint(20)", "bigint"), Some(Severity::Info));
        assert_eq!(sev("tinyint(4)", "tinyint"), Some(Severity::Info));
        assert_eq!(sev("varchar(10)", "varchar(20)"), Some(Severity::Warning));
        assert!(is_display_width_only_diff("int(10) unsigned", "int unsigned"));
        assert!(!is_display_width_only_diff("varchar(10)", "varchar(20)"));
        assert!(!is_display_width_only_diff("decimal(10,2)", "decimal(12,2)"));
    }

    #[test]
    fn nullable_default_and_auto_increment_severity() {
        let mut not_null = col("c", "int");
        not_null.nullable = false;
        assert_eq!(single_column_diff(col("c", "int"), not_null, CompareLevel::Standard).severity, Some(Severity::Warning));
        let mut with_default = col("c", "int");
        with_default.default = Some("0".into());
        assert_eq!(single_column_diff(with_default, col("c", "int"), CompareLevel::Standard).severity, Some(Severity::Warning));
        let mut auto = col("c", "int");
        auto.extra = "auto_increment".into();
        assert_eq!(single_column_diff(auto, col("c", "int"), CompareLevel::Standard).severity, Some(Severity::Critical));
    }

    #[test]
    fn default_generated_marker_is_not_a_difference() {
        let mut mysql80 = col("created", "timestamp");
        mysql80.extra = "DEFAULT_GENERATED on update CURRENT_TIMESTAMP".into();
        let mut mysql57 = col("created", "timestamp");
        mysql57.extra = "on update CURRENT_TIMESTAMP".into();
        assert_eq!(single_column_diff(mysql80, mysql57, CompareLevel::Standard).diff_type, DiffType::Unchanged);
    }

    #[test]
    fn compare_levels() {
        let mut src = col("name", "varchar(10)");
        src.nullable = false;
        src.collation = "utf8mb4_bin".into();
        src.charset = "utf8mb4".into();
        let mut tgt = col("name", "varchar(10)");
        tgt.collation = "utf8mb4_general_ci".into();
        tgt.charset = "utf8mb4".into();
        assert_eq!(single_column_diff(src.clone(), tgt.clone(), CompareLevel::Quick).diff_type, DiffType::Unchanged);
        let standard = single_column_diff(src.clone(), tgt.clone(), CompareLevel::Standard);
        assert_eq!(standard.changes.iter().map(|c| c.field.as_str()).collect::<Vec<_>>(), vec!["nullable"]);
        let strict = single_column_diff(src, tgt, CompareLevel::Strict);
        assert_eq!(strict.changes.iter().map(|c| c.field.as_str()).collect::<Vec<_>>(), vec!["nullable", "collation"]);

        let mut a = table("t", vec![col("id", "int")]);
        a.indexes.push(index("ix", &["id"], false));
        let b = table("t", vec![col("id", "int")]);
        assert!(compare_table(&a, &b, CompareLevel::Quick).indexes.is_empty());
        assert_eq!(compare_table(&a, &b, CompareLevel::Standard).indexes[0].diff_type, DiffType::Added);
    }

    #[test]
    fn index_and_fk_renames_are_detected_and_info() {
        let mut src = table("t", vec![col("a", "int"), col("b", "int")]);
        src.indexes.push(index("ix_new", &["a"], false));
        src.foreign_keys.push(fk("fk_new", "b", "p"));
        let mut tgt = table("t", vec![col("a", "int"), col("b", "int")]);
        tgt.indexes.push(index("ix_old", &["a"], false));
        tgt.foreign_keys.push(fk("fk_old", "b", "p"));
        let mut diffs = vec![compare_table(&src, &tgt, CompareLevel::Standard)];
        classify(&mut diffs);
        let (idx, f) = (&diffs[0].indexes[0], &diffs[0].foreign_keys[0]);
        assert_eq!((idx.diff_type, idx.old_name.as_deref(), idx.severity), (DiffType::Renamed, Some("ix_old"), Some(Severity::Info)));
        assert_eq!((f.diff_type, f.old_name.as_deref(), f.severity), (DiffType::Renamed, Some("fk_old"), Some(Severity::Info)));
    }

    #[test]
    fn primary_key_change_is_critical_and_other_index_or_fk_changes_warn() {
        let mut src = table("t", vec![col("a", "int"), col("b", "int")]);
        src.indexes.push(index("PRIMARY", &["a", "b"], true));
        src.indexes.push(index("ix", &["a"], true));
        src.foreign_keys.push(fk("fk", "a", "p"));
        let mut tgt = table("t", vec![col("a", "int"), col("b", "int")]);
        tgt.indexes.push(index("PRIMARY", &["a"], true));
        tgt.indexes.push(index("ix", &["a"], false));
        let mut changed_fk = fk("fk", "a", "p");
        changed_fk.on_delete = "CASCADE".into();
        tgt.foreign_keys.push(changed_fk);
        let mut diffs = vec![compare_table(&src, &tgt, CompareLevel::Standard)];
        let summary = classify(&mut diffs);
        let sev = |name: &str| diffs[0].indexes.iter().find(|i| i.name == name).unwrap().severity;
        assert_eq!(sev("PRIMARY"), Some(Severity::Critical));
        assert_eq!(sev("ix"), Some(Severity::Warning));
        assert_eq!(diffs[0].foreign_keys[0].severity, Some(Severity::Warning));
        assert_eq!((summary.critical, summary.warning), (1, 2));
    }

    #[test]
    fn column_definitions_quote_literals_and_keep_expressions() {
        let mut text = col("note", "varchar(20)");
        text.default = Some("it's \\ ok".into());
        text.charset = "utf8mb4".into();
        text.collation = "utf8mb4_bin".into();
        text.comment = "a 'quote'".into();
        assert_eq!(
            column_definition(&text),
            "`note` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL DEFAULT 'it''s \\\\ ok' COMMENT 'a ''quote'''"
        );
        let mut ts = col("created", "datetime(3)");
        ts.nullable = false;
        ts.default = Some("CURRENT_TIMESTAMP(3)".into());
        ts.extra = "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(3)".into();
        assert_eq!(column_definition(&ts), "`created` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) on update CURRENT_TIMESTAMP(3)");
        let mut generated = col("total", "int");
        generated.generation_expression = "(`a` + `b`)".into();
        generated.extra = "STORED GENERATED".into();
        assert_eq!(column_definition(&generated), "`total` int GENERATED ALWAYS AS ((`a` + `b`)) STORED NULL");
    }

    #[test]
    fn index_definitions_use_valid_syntax() {
        let mut ft = index("ft", &["body"], false);
        ft.index_type = "FULLTEXT".into();
        assert_eq!(index_definition(&ft), "FULLTEXT INDEX `ft` (`body`)");
        let mut prefix = index("ix", &["name"], true);
        prefix.parts[0].sub_part = Some(10);
        assert_eq!(index_definition(&prefix), "UNIQUE INDEX `ix` (`name`(10)) USING BTREE");
        assert_eq!(index_definition(&index("PRIMARY", &["b", "a"], true)), "PRIMARY KEY (`b`, `a`)");
    }

    #[test]
    fn create_table_keeps_pk_order_collation_and_adds_fks_once() {
        let mut src = table("child", vec![col("a", "int"), col("b", "int")]);
        src.indexes.push(index("PRIMARY", &["b", "a"], true));
        src.foreign_keys.push(fk("fk_parent", "a", "parent"));
        let mut diffs = compare_schemas(&[src], &[], CompareLevel::Standard);
        classify(&mut diffs);
        let sql = generate_sync_sql(&diffs, "app");
        assert!(sql.contains("    PRIMARY KEY (`b`, `a`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;"), "{sql}");
        assert_eq!(sql.matches("FOREIGN KEY").count(), 1, "{sql}");
        assert!(sql.contains("ALTER TABLE `app`.`child` ADD CONSTRAINT `fk_parent`"));
    }

    #[test]
    fn primary_key_change_generates_sql_with_warning() {
        let mut src = table("t", vec![col("a", "int"), col("b", "int")]);
        src.indexes.push(index("PRIMARY", &["a", "b"], true));
        let mut tgt = table("t", vec![col("a", "int"), col("b", "int")]);
        tgt.indexes.push(index("PRIMARY", &["a"], true));
        let sql = generate_sync_sql(&[compare_table(&src, &tgt, CompareLevel::Standard)], "app");
        assert!(sql.contains("-- 주의: PRIMARY KEY 변경입니다."));
        assert!(sql.contains("ALTER TABLE `app`.`t` DROP PRIMARY KEY, ADD PRIMARY KEY (`a`, `b`);"), "{sql}");
    }

    #[test]
    fn alters_drop_indexes_before_columns_and_add_indexes_after() {
        let mut src = table("t", vec![col("keep", "int"), col("fresh", "int")]);
        src.indexes.push(index("ix_fresh", &["fresh"], false));
        let mut tgt = table("t", vec![col("keep", "int"), col("gone", "int")]);
        tgt.indexes.push(index("ix_gone", &["gone"], false));
        let sql = generate_sync_sql(&[compare_table(&src, &tgt, CompareLevel::Standard)], "app");
        let pos = |needle: &str| sql.find(needle).unwrap_or_else(|| panic!("{needle} missing in {sql}"));
        assert!(pos("DROP INDEX `ix_gone`") < pos("DROP COLUMN `gone`"));
        assert!(pos("ADD COLUMN `fresh`") < pos("ADD INDEX `ix_fresh`"));
    }

    #[test]
    fn identifiers_with_backticks_are_escaped() {
        let sql = generate_sync_sql(&compare_schemas(&[], &[table("we`ird", vec![])], CompareLevel::Standard), "a`pp");
        assert!(sql.contains("DROP TABLE IF EXISTS `a``pp`.`we``ird`;"), "{sql}");
    }

    #[test]
    fn unknown_level_and_non_mysql_endpoints_are_rejected() {
        assert!(CompareLevel::parse("deep").is_err());
        let request = Request {
            command: "schema.compare".into(),
            request_id: Some("r".into()),
            payload: json!({
                "source": {"engine": "postgresql", "host": "h", "port": 5432, "user": "u", "password": "p", "database": "d"},
                "target": {"engine": "postgresql", "host": "h", "port": 5432, "user": "u", "password": "p", "database": "d"}
            }),
        };
        let mut events = Vec::new();
        schema_compare(&request, |event| events.push(event));
        assert_eq!(events.last().unwrap()["event"], "error");
        assert!(events.last().unwrap()["message"].as_str().unwrap().contains("MySQL only"));
    }
}
