//! 업그레이드 호환성 이슈 자동 수정 계획 (읽기 전용).
//!
//! - `upgrade.fix_plan`: 이슈별 수정 옵션(SQL 포함, 날짜 수정의 예상 영향 행 수 포함)과
//!   문자셋 수정 대상 테이블 목록(FK 부모/자식, 연쇄 건너뛰기)을 만든다.
//! - `upgrade.charset_sql`: 고른 테이블의 FK 안전 문자셋 변환 SQL(FK DROP → CONVERT → FK ADD).
//!
//! 예전 Python SmartFixGenerator / CharsetFixPlanBuilder / FKSafeCharsetChanger 를 옮겼다.
//! SQL 은 텍스트로만 만들며 실행하지 않는다.
use crate::*;
use mysql::prelude::Queryable;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub const DEFAULT_TARGET_CHARSET: &str = "utf8mb4";
pub const DEFAULT_TARGET_COLLATION: &str = "utf8mb4_unicode_ci";

fn q(name: &str) -> String {
    format!("`{}`", name.replace('`', "``"))
}

fn qualified(schema: &str, table: &str) -> String {
    format!("{}.{}", q(schema), q(table))
}

#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct FixOptionOut {
    pub strategy: String,
    pub label: String,
    pub description: String,
    pub sql_template: Option<String>,
    pub requires_input: bool,
    pub input_label: Option<String>,
    pub input_default: Option<String>,
    pub is_recommended: bool,
    /// dry-run 예상 영향 행 수 (날짜 UPDATE 만 계산, 그 외 None)
    pub estimated_rows: Option<u64>,
}

fn option(strategy: &str, label: &str, description: impl Into<String>, sql: Option<String>) -> FixOptionOut {
    FixOptionOut {
        strategy: strategy.into(),
        label: label.into(),
        description: description.into(),
        sql_template: sql,
        ..Default::default()
    }
}

#[derive(Debug, Clone, Default)]
pub struct FixIssueIn {
    pub issue_type: String,
    pub location: String,
    pub table_name: Option<String>,
    pub column_name: Option<String>,
    pub description: String,
}

impl FixIssueIn {
    fn from_value(value: &Value) -> Self {
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
        Self {
            issue_type: text("issue_type").unwrap_or_default(),
            location: text("location").unwrap_or_default(),
            table_name: text("table_name").filter(|s| !s.is_empty()),
            column_name: text("column_name").filter(|s| !s.is_empty()),
            description: text("description").unwrap_or_default(),
        }
    }
}

/// FK 하나 (복합 FK 는 컬럼을 ORDINAL_POSITION 순서로 묶는다)
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FkDefinition {
    pub constraint_name: String,
    pub table_name: String,
    pub columns: Vec<String>,
    pub ref_table: String,
    pub ref_columns: Vec<String>,
    pub on_delete: String,
    pub on_update: String,
}

impl FkDefinition {
    pub fn drop_sql(&self, schema: &str) -> String {
        format!("ALTER TABLE {} DROP FOREIGN KEY {};", qualified(schema, &self.table_name), q(&self.constraint_name))
    }
    pub fn add_sql(&self, schema: &str) -> String {
        let list = |names: &[String]| names.iter().map(|n| q(n)).collect::<Vec<_>>().join(", ");
        format!(
            "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {} ON UPDATE {};",
            qualified(schema, &self.table_name),
            q(&self.constraint_name),
            list(&self.columns),
            q(&self.ref_table),
            list(&self.ref_columns),
            self.on_delete,
            self.on_update
        )
    }
}

/// FK 관계 그래프 (자식 → 부모). BTree 를 써서 순서를 결정적으로 만든다.
#[derive(Debug, Clone, Default)]
pub struct FkGraph {
    parents: BTreeMap<String, BTreeSet<String>>,
    children: BTreeMap<String, BTreeSet<String>>,
}

impl FkGraph {
    pub fn from_definitions(fks: &[FkDefinition]) -> Self {
        let mut graph = Self::default();
        for fk in fks {
            graph.parents.entry(fk.table_name.clone()).or_default().insert(fk.ref_table.clone());
            graph.children.entry(fk.ref_table.clone()).or_default().insert(fk.table_name.clone());
        }
        graph
    }

    pub fn parents(&self, table: &str) -> BTreeSet<String> {
        self.parents.get(table).cloned().unwrap_or_default()
    }

    pub fn children(&self, table: &str) -> BTreeSet<String> {
        self.children.get(table).cloned().unwrap_or_default()
    }

    /// FK 로 (방향 무관) 연결된 모든 테이블 (start 제외)
    pub fn related(&self, start: &str) -> BTreeSet<String> {
        let mut visited = BTreeSet::from([start.to_string()]);
        let mut queue = VecDeque::from([start.to_string()]);
        while let Some(current) = queue.pop_front() {
            for next in self.parents(&current).into_iter().chain(self.children(&current)) {
                if visited.insert(next.clone()) {
                    queue.push_back(next);
                }
            }
        }
        visited.remove(start);
        visited
    }

    /// 부모 먼저(Kahn). 순환이 있으면 남은 테이블을 이름 순서로 뒤에 붙인다.
    pub fn topological_order(&self, tables: &BTreeSet<String>) -> Vec<String> {
        let mut in_degree: BTreeMap<&String, usize> =
            tables.iter().map(|t| (t, self.parents(t).iter().filter(|p| tables.contains(*p) && *p != t).count())).collect();
        let mut queue: VecDeque<&String> = in_degree.iter().filter(|(_, d)| **d == 0).map(|(t, _)| *t).collect();
        let mut order = Vec::new();
        while let Some(current) = queue.pop_front() {
            order.push(current.clone());
            for child in self.children(current) {
                if let Some((key, degree)) = in_degree.iter_mut().find(|(t, _)| ***t == child) {
                    if child != *current && *degree > 0 {
                        *degree -= 1;
                        if *degree == 0 {
                            queue.push_back(*key);
                        }
                    }
                }
            }
        }
        for table in tables {
            if !order.contains(table) {
                order.push(table.clone());
            }
        }
        order
    }

    /// table 을 건너뛰면 함께 건너뛰어야 하는 대상 테이블 (자식과 부모 양쪽으로 전파, table 제외)
    pub fn cascade_skip(&self, table: &str, targets: &BTreeSet<String>) -> BTreeSet<String> {
        let mut visited = BTreeSet::from([table.to_string()]);
        let mut queue = VecDeque::from([table.to_string()]);
        let mut skipped = BTreeSet::new();
        while let Some(current) = queue.pop_front() {
            for next in self.children(&current).into_iter().chain(self.parents(&current)) {
                if targets.contains(&next) && visited.insert(next.clone()) {
                    skipped.insert(next.clone());
                    queue.push_back(next);
                }
            }
        }
        skipped
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CharsetSqlParts {
    pub drop_fks: Vec<String>,
    pub alter_tables: Vec<String>,
    pub add_fks: Vec<String>,
    pub full_sql: Vec<String>,
    pub fk_count: usize,
    pub table_count: usize,
}

/// FK 안전 문자셋 변환: 대상 테이블에 걸린(참조하거나 참조받는) FK 를 모두 DROP → 부모 먼저 CONVERT → 원래 정의대로 ADD.
pub fn charset_sql_parts(schema: &str, tables: &BTreeSet<String>, fks: &[FkDefinition], charset: &str, collation: &str) -> CharsetSqlParts {
    if tables.is_empty() {
        return CharsetSqlParts {
            drop_fks: vec![],
            alter_tables: vec![],
            add_fks: vec![],
            full_sql: vec!["-- 변경할 테이블이 없습니다.".into()],
            fk_count: 0,
            table_count: 0,
        };
    }
    let related: Vec<&FkDefinition> = fks.iter().filter(|fk| tables.contains(&fk.table_name) || tables.contains(&fk.ref_table)).collect();
    let graph = FkGraph::from_definitions(fks);
    let ordered = graph.topological_order(tables);
    let drop_fks: Vec<String> = related.iter().map(|fk| fk.drop_sql(schema)).collect();
    let add_fks: Vec<String> = related.iter().map(|fk| fk.add_sql(schema)).collect();
    let alter_tables: Vec<String> = ordered
        .iter()
        .map(|t| format!("ALTER TABLE {} CONVERT TO CHARACTER SET {charset} COLLATE {collation};", qualified(schema, t)))
        .collect();
    let mut full_sql = vec!["-- ===== Phase 1: FK 임시 DROP =====".to_string()];
    if drop_fks.is_empty() {
        full_sql.push("-- (연관 FK 없음)".into());
    } else {
        full_sql.extend(drop_fks.iter().cloned());
    }
    full_sql.push(String::new());
    full_sql.push("-- ===== Phase 2: Charset 변경 (부모 먼저) =====".into());
    full_sql.extend(alter_tables.iter().cloned());
    full_sql.push(String::new());
    full_sql.push("-- ===== Phase 3: FK 재생성 =====".into());
    if add_fks.is_empty() {
        full_sql.push("-- (재생성할 FK 없음)".into());
    } else {
        full_sql.extend(add_fks.iter().cloned());
    }
    CharsetSqlParts { fk_count: related.len(), table_count: ordered.len(), drop_fks, alter_tables, add_fks, full_sql }
}

// ---------------------------------------------------------------------------
// 수정 옵션
// ---------------------------------------------------------------------------

/// 날짜 수정 UPDATE 들의 WHERE 절 (세 옵션 공유)
fn invalid_date_where(column: &str) -> String {
    let c = q(column);
    format!("WHERE {c} = '0000-00-00'\n   OR {c} = '0000-00-00 00:00:00'\n   OR (MONTH({c}) = 0 OR DAY({c}) = 0);")
}

pub fn skip_option() -> FixOptionOut {
    option("skip", "건너뛰기", "이 이슈는 수정하지 않고 넘어갑니다.", None)
}

fn default_options(issue: &FixIssueIn) -> Vec<FixOptionOut> {
    vec![option(
        "manual",
        "수동 처리",
        "이 이슈는 자동 수정이 지원되지 않습니다. 수동으로 처리하세요.",
        Some(format!("-- 수동 처리 필요: {}", issue.description)),
    )]
}

/// 날짜 이슈 옵션. nullable 이면 NULL 변경을 권장하고, 아니면 1970-01-01 을 권장한다.
pub fn invalid_date_options(schema: &str, table: &str, column: &str, nullable: bool, estimated_rows: Option<u64>) -> Vec<FixOptionOut> {
    let target = qualified(schema, table);
    let c = q(column);
    let where_clause = invalid_date_where(column);
    let mut options = Vec::new();
    if nullable {
        let mut to_null = option(
            "date_to_null",
            "NULL로 변경 (권장)",
            "0000-00-00 값을 NULL로 변경합니다.",
            Some(format!("UPDATE {target}\nSET {c} = NULL\n{where_clause}")),
        );
        to_null.is_recommended = true;
        to_null.estimated_rows = estimated_rows;
        options.push(to_null);
    }
    let mut to_min = option(
        "date_to_min",
        "1970-01-01로 변경",
        "0000-00-00 값을 Unix 시작일(1970-01-01)로 변경합니다.",
        Some(format!("UPDATE {target}\nSET {c} = '1970-01-01'\n{where_clause}")),
    );
    to_min.is_recommended = !nullable;
    to_min.estimated_rows = estimated_rows;
    options.push(to_min);
    let mut custom = option(
        "date_to_custom",
        "사용자 지정 날짜",
        "원하는 날짜로 직접 지정합니다.",
        Some(format!("UPDATE {target}\nSET {c} = '{{custom_date}}'\n{where_clause}")),
    );
    custom.requires_input = true;
    custom.input_label = Some("변경할 날짜 (YYYY-MM-DD)".into());
    custom.input_default = Some("2000-01-01".into());
    custom.estimated_rows = estimated_rows;
    options.push(custom);
    options
}

pub fn static_options(schema: &str, issue: &FixIssueIn) -> Vec<FixOptionOut> {
    match issue.issue_type.as_str() {
        "zerofill_usage" => vec![option(
            "manual",
            "수동 처리",
            "ZEROFILL은 deprecated됩니다. 애플리케이션에서 LPAD() 함수로 포맷팅 처리를 권장합니다.\n예: SELECT LPAD(column, 5, '0') FROM table;",
            Some("-- ZEROFILL 제거 후 LPAD() 함수로 애플리케이션에서 포맷팅 처리".into()),
        )],
        "float_precision" => match (&issue.table_name, &issue.column_name) {
            (Some(table), Some(column)) => {
                let base = format!("ALTER TABLE {} MODIFY COLUMN {}", qualified(schema, table), q(column));
                let mut to_float = option("manual", "FLOAT로 변경", "정밀도 구문을 제거하고 FLOAT 타입으로 변경합니다.", Some(format!("{base} FLOAT;")));
                to_float.is_recommended = true;
                let mut to_decimal = option(
                    "manual",
                    "DECIMAL로 변경",
                    "정확한 소수점 연산이 필요하면 DECIMAL을 사용합니다.",
                    Some(format!("{base} DECIMAL({{precision}});")),
                );
                to_decimal.requires_input = true;
                to_decimal.input_label = Some("DECIMAL 정밀도 (M,D)".into());
                to_decimal.input_default = Some("10,2".into());
                vec![to_float, to_decimal]
            }
            _ => default_options(issue),
        },
        "int_display_width" => {
            let mut ignore = option(
                "skip",
                "무시 (권장)",
                "INT 표시 너비는 MySQL 8.4에서 자동으로 무시됩니다.\n별도 수정 없이 사용해도 영향이 없습니다.",
                None,
            );
            ignore.is_recommended = true;
            vec![ignore]
        }
        "enum_empty_value" => vec![option(
            "manual",
            "수동 처리",
            "ENUM 정의에서 빈 문자열('')을 제거해야 합니다.\n먼저 데이터를 정리한 후 ENUM 정의를 변경하세요.",
            Some("-- ENUM 정의에서 빈 문자열('') 제거 및 데이터 정제 필요".into()),
        )],
        "deprecated_engine" => {
            let table = issue.table_name.clone().or_else(|| issue.location.split('.').nth(1).map(str::to_string));
            match table {
                Some(table) => {
                    let mut innodb = option(
                        "manual",
                        "InnoDB로 변경",
                        "테이블 엔진을 InnoDB로 변경합니다.",
                        Some(format!("ALTER TABLE {} ENGINE=InnoDB;", qualified(schema, &table))),
                    );
                    innodb.is_recommended = true;
                    vec![innodb]
                }
                None => default_options(issue),
            }
        }
        _ => default_options(issue),
    }
}

// ---------------------------------------------------------------------------
// 조회 + protocol
// ---------------------------------------------------------------------------

pub fn load_fk_definitions(conn: &mut mysql::PooledConn, schema: &str) -> Result<Vec<FkDefinition>, String> {
    let rows: Vec<(String, String, String, String, String, String, String)> = conn
        .exec(
            "SELECT kcu.CONSTRAINT_NAME, kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME, \
             rc.DELETE_RULE, rc.UPDATE_RULE \
             FROM information_schema.KEY_COLUMN_USAGE kcu \
             JOIN information_schema.REFERENTIAL_CONSTRAINTS rc \
               ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND rc.TABLE_NAME = kcu.TABLE_NAME \
             JOIN information_schema.TABLES tc ON tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA AND tc.TABLE_NAME = kcu.TABLE_NAME \
             JOIN information_schema.TABLES tp ON tp.TABLE_SCHEMA = kcu.TABLE_SCHEMA AND tp.TABLE_NAME = kcu.REFERENCED_TABLE_NAME \
             WHERE kcu.TABLE_SCHEMA = ? AND kcu.REFERENCED_TABLE_NAME IS NOT NULL \
               AND tc.TABLE_TYPE = 'BASE TABLE' AND tp.TABLE_TYPE = 'BASE TABLE' \
             ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION",
            (schema,),
        )
        .map_err(|e| format!("foreign key query failed: {e}"))?;
    let mut fks: Vec<FkDefinition> = Vec::new();
    for (constraint_name, table_name, column, ref_table, ref_column, on_delete, on_update) in rows {
        match fks.iter_mut().find(|fk| fk.table_name == table_name && fk.constraint_name == constraint_name) {
            Some(fk) => {
                fk.columns.push(column);
                fk.ref_columns.push(ref_column);
            }
            None => fks.push(FkDefinition {
                constraint_name,
                table_name,
                columns: vec![column],
                ref_table,
                ref_columns: vec![ref_column],
                on_delete,
                on_update,
            }),
        }
    }
    Ok(fks)
}

fn request_endpoint(request: &Request, command: &str) -> Result<Endpoint, String> {
    let value = request.payload.get("connection").ok_or_else(|| "missing connection endpoint".to_string())?;
    let endpoint = endpoint_from_value(value)?;
    if endpoint.engine != "mysql" {
        return Err(format!("{command} supports MySQL only"));
    }
    Ok(endpoint)
}

fn string_set(value: Option<&Value>) -> BTreeSet<String> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect()).unwrap_or_default()
}

fn target_charset(payload: &Value) -> (String, String) {
    let text = |key: &str, default: &str| payload.get(key).and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or(default).to_string();
    (text("charset", DEFAULT_TARGET_CHARSET), text("collation", DEFAULT_TARGET_COLLATION))
}

fn fix_plan(request: &Request) -> Result<Value, String> {
    let endpoint = request_endpoint(request, "upgrade.fix_plan")?;
    let schema = endpoint_schema(&endpoint);
    let mut conn = mysql_conn(&endpoint)?;
    // 0000-00-00 비교를 포함한 COUNT 가 strict mode 에서 실패하지 않도록 이 세션만 완화한다(읽기 전용).
    conn.query_drop("SET SESSION sql_mode = ''").map_err(|e| format!("sql_mode relax failed: {e}"))?;

    let issues: Vec<FixIssueIn> = request
        .payload
        .get("issues")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(FixIssueIn::from_value).collect())
        .unwrap_or_default();
    let mut steps = Vec::new();
    for (index, issue) in issues.iter().enumerate() {
        let mut options = match (issue.issue_type.as_str(), &issue.table_name, &issue.column_name) {
            ("invalid_date", Some(table), Some(column)) => {
                let nullable: Option<String> = conn
                    .exec_first(
                        "SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?",
                        (&schema, table, column),
                    )
                    .map_err(|e| format!("column query failed: {e}"))?;
                let c = q(column);
                let estimate: Option<u64> = conn
                    .query_first(format!(
                        "SELECT COUNT(*) FROM {} WHERE {c} = '0000-00-00' OR {c} = '0000-00-00 00:00:00' OR (MONTH({c}) = 0 OR DAY({c}) = 0)",
                        qualified(&schema, table)
                    ))
                    .ok()
                    .flatten();
                invalid_date_options(&schema, table, column, nullable.as_deref() == Some("YES"), estimate)
            }
            ("invalid_date", _, _) => default_options(issue),
            _ => static_options(&schema, issue),
        };
        options.push(skip_option());
        steps.push(json!({"issue_index": index, "options": options}));
    }

    let original: BTreeSet<String> = string_set(request.payload.get("charset_tables"));
    let mut charset_tables = Vec::new();
    let mut cascade_skip = BTreeMap::new();
    let mut plan_fks: Vec<FkDefinition> = Vec::new();
    if !original.is_empty() {
        let fks = load_fk_definitions(&mut conn, &schema)?;
        let graph = FkGraph::from_definitions(&fks);
        let mut all: BTreeSet<String> = original.clone();
        for table in &original {
            all.extend(graph.related(table));
        }
        let charsets: Vec<(String, Option<String>, Option<String>)> = conn
            .exec(
                "SELECT T.TABLE_NAME, CCSA.CHARACTER_SET_NAME, T.TABLE_COLLATION FROM information_schema.TABLES T \
                 LEFT JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY CCSA ON CCSA.COLLATION_NAME = T.TABLE_COLLATION \
                 WHERE T.TABLE_SCHEMA = ?",
                (&schema,),
            )
            .map_err(|e| format!("table charset query failed: {e}"))?;
        for table in graph.topological_order(&all) {
            let (charset, collation) = charsets
                .iter()
                .find(|(name, _, _)| name == &table)
                .map(|(_, cs, co)| (cs.clone().unwrap_or_else(|| "utf8mb3".into()), co.clone().unwrap_or_else(|| "utf8mb3_general_ci".into())))
                .unwrap_or_else(|| ("utf8mb3".into(), "utf8mb3_general_ci".into()));
            cascade_skip.insert(table.clone(), graph.cascade_skip(&table, &all).into_iter().collect::<Vec<_>>());
            charset_tables.push(json!({
                "table_name": table,
                "current_charset": charset,
                "current_collation": collation,
                "fk_parents": graph.parents(&table).into_iter().collect::<Vec<_>>(),
                "fk_children": graph.children(&table).into_iter().collect::<Vec<_>>(),
                "is_original_issue": original.contains(&table),
            }));
        }
        // 화면이 "선택한 테이블에 걸린 FK 수"를 계산할 수 있도록 계획 대상에 걸린 FK 를 함께 준다.
        plan_fks = fks.into_iter().filter(|fk| all.contains(&fk.table_name) || all.contains(&fk.ref_table)).collect();
    }
    Ok(json!({"schema": schema, "steps": steps, "charset_tables": charset_tables, "cascade_skip": cascade_skip, "foreign_keys": plan_fks}))
}

fn charset_sql(request: &Request) -> Result<Value, String> {
    let endpoint = request_endpoint(request, "upgrade.charset_sql")?;
    let schema = endpoint_schema(&endpoint);
    let tables = string_set(request.payload.get("tables"));
    let (charset, collation) = target_charset(&request.payload);
    let fks = if tables.is_empty() { Vec::new() } else { load_fk_definitions(&mut mysql_conn(&endpoint)?, &schema)? };
    serde_json::to_value(charset_sql_parts(&schema, &tables, &fks, &charset, &collation)).map_err(|e| e.to_string())
}

fn finish<F: FnMut(Value)>(request: &Request, command: &str, result: Result<Value, String>, mut emit: F) {
    match result {
        Ok(Value::Object(mut fields)) => {
            fields.insert("event".into(), json!("result"));
            fields.insert("request_id".into(), json!(request.request_id));
            fields.insert("command".into(), json!(command));
            fields.insert("success".into(), json!(true));
            emit(Value::Object(fields));
        }
        Ok(_) => unreachable!(),
        Err(message) => emit(json!({"event": "error", "request_id": request.request_id, "command": command, "message": message})),
    }
}

pub fn upgrade_fix_plan<F: FnMut(Value)>(request: &Request, emit: F) {
    finish(request, "upgrade.fix_plan", fix_plan(request), emit);
}

pub fn upgrade_charset_sql<F: FnMut(Value)>(request: &Request, emit: F) {
    finish(request, "upgrade.charset_sql", charset_sql(request), emit);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fk(name: &str, child: &str, parent: &str) -> FkDefinition {
        FkDefinition {
            constraint_name: name.into(),
            table_name: child.into(),
            columns: vec!["pid".into()],
            ref_table: parent.into(),
            ref_columns: vec!["id".into()],
            on_delete: "CASCADE".into(),
            on_update: "RESTRICT".into(),
        }
    }

    fn set(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn sample() -> Vec<FkDefinition> {
        // grand <- parent <- child, parent <- sibling, other (unrelated)
        vec![fk("fk_parent", "parent", "grand"), fk("fk_child", "child", "parent"), fk("fk_sibling", "sibling", "parent")]
    }

    #[test]
    fn graph_related_order_and_cascade() {
        let graph = FkGraph::from_definitions(&sample());
        assert_eq!(graph.related("child"), set(&["grand", "parent", "sibling"]));
        assert!(graph.related("other").is_empty());
        let order = graph.topological_order(&set(&["child", "grand", "parent", "sibling"]));
        let pos = |t: &str| order.iter().position(|x| x == t).unwrap();
        assert!(pos("grand") < pos("parent") && pos("parent") < pos("child") && pos("parent") < pos("sibling"));
        // child 를 건너뛰면 부모(parent) 와 그 연결(grand, sibling) 까지 전파된다
        assert_eq!(graph.cascade_skip("child", &set(&["child", "grand", "parent", "sibling"])), set(&["grand", "parent", "sibling"]));
        assert_eq!(graph.cascade_skip("child", &set(&["child", "sibling"])), BTreeSet::new());
        assert_eq!(graph.children("parent"), set(&["child", "sibling"]));
    }

    #[test]
    fn topological_order_survives_cycles() {
        let graph = FkGraph::from_definitions(&[fk("a_b", "a", "b"), fk("b_a", "b", "a"), fk("self", "c", "c")]);
        let order = graph.topological_order(&set(&["a", "b", "c"]));
        assert_eq!(order.len(), 3);
        assert!(order.contains(&"a".to_string()) && order.contains(&"c".to_string()));
    }

    #[test]
    fn charset_sql_drops_converts_parents_first_and_recreates_fks() {
        let parts = charset_sql_parts("app", &set(&["child", "parent"]), &sample(), DEFAULT_TARGET_CHARSET, DEFAULT_TARGET_COLLATION);
        assert_eq!(parts.fk_count, 3, "every FK touching a target table is dropped and re-added");
        assert_eq!(parts.table_count, 2);
        assert_eq!(parts.alter_tables[0], "ALTER TABLE `app`.`parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;");
        assert!(parts.drop_fks.contains(&"ALTER TABLE `app`.`child` DROP FOREIGN KEY `fk_child`;".to_string()));
        assert!(parts.add_fks.contains(
            &"ALTER TABLE `app`.`child` ADD CONSTRAINT `fk_child` FOREIGN KEY (`pid`) REFERENCES `parent` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT;".to_string()
        ));
        assert_eq!(parts.full_sql[0], "-- ===== Phase 1: FK 임시 DROP =====");
        let empty = charset_sql_parts("app", &BTreeSet::new(), &sample(), "utf8mb4", "x");
        assert_eq!(empty.full_sql, vec!["-- 변경할 테이블이 없습니다.".to_string()]);
    }

    #[test]
    fn fk_sql_escapes_backticks_in_names() {
        let fk = FkDefinition {
            constraint_name: "fk`x".into(),
            table_name: "we`ird".into(),
            columns: vec!["a`b".into()],
            ref_table: "p".into(),
            ref_columns: vec!["id".into()],
            on_delete: "RESTRICT".into(),
            on_update: "RESTRICT".into(),
        };
        assert!(fk.drop_sql("app").contains("`fk``x`"));
        let add = fk.add_sql("app");
        assert!(add.contains("`a``b`") && add.contains("`we``ird`"));
    }

    #[test]
    fn date_options_depend_on_nullability() {
        let nullable = invalid_date_options("app", "t", "d", true, Some(4));
        assert_eq!(nullable.iter().map(|o| o.strategy.as_str()).collect::<Vec<_>>(), vec!["date_to_null", "date_to_min", "date_to_custom"]);
        assert!(nullable[0].is_recommended && !nullable[1].is_recommended);
        assert_eq!(nullable[0].estimated_rows, Some(4));
        assert!(nullable[0].sql_template.as_deref().unwrap().starts_with("UPDATE `app`.`t`\nSET `d` = NULL\nWHERE `d` = '0000-00-00'"));
        assert!(nullable[2].sql_template.as_deref().unwrap().contains("SET `d` = '{custom_date}'"));
        let not_null = invalid_date_options("app", "t", "d", false, None);
        assert_eq!(not_null[0].strategy, "date_to_min");
        assert!(not_null[0].is_recommended);
    }

    #[test]
    fn static_options_per_issue_type() {
        let issue = |issue_type: &str| FixIssueIn {
            issue_type: issue_type.into(),
            location: "app.t.c".into(),
            table_name: Some("t".into()),
            column_name: Some("c".into()),
            description: "desc".into(),
        };
        let float = static_options("app", &issue("float_precision"));
        assert_eq!(float[0].sql_template.as_deref(), Some("ALTER TABLE `app`.`t` MODIFY COLUMN `c` FLOAT;"));
        assert_eq!(float[1].input_default.as_deref(), Some("10,2"));
        assert_eq!(static_options("app", &issue("int_display_width"))[0].strategy, "skip");
        let mut engine = issue("deprecated_engine");
        engine.table_name = None;
        assert_eq!(static_options("app", &engine)[0].sql_template.as_deref(), Some("ALTER TABLE `app`.`t` ENGINE=InnoDB;"));
        assert_eq!(static_options("app", &issue("reserved_keyword"))[0].sql_template.as_deref(), Some("-- 수동 처리 필요: desc"));
        assert_eq!(skip_option().label, "건너뛰기");
    }
}
