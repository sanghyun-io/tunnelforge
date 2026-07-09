use serde_json::{json, Value};
use std::collections::BTreeMap;

use mysql::prelude::Queryable;
use postgres::NoTls;
use crate::*;
use crate::query::skip_sql_comment;

pub(crate) fn normalized_schema_diff(source: &NormalizedSchema, target: &NormalizedSchema) -> Vec<Value> {
    let source_tables: BTreeMap<String, &NormalizedTable> = source
        .tables
        .iter()
        .map(|table| (table.name.clone(), table))
        .collect();
    let target_tables: BTreeMap<String, &NormalizedTable> = target
        .tables
        .iter()
        .map(|table| (table.name.clone(), table))
        .collect();
    let mut differences = Vec::new();

    for table_name in source_tables.keys() {
        if !target_tables.contains_key(table_name) {
            differences.push(json!({
                "kind": "missing_table",
                "side": "target",
                "table": table_name
            }));
        }
    }
    for table_name in target_tables.keys() {
        if !source_tables.contains_key(table_name) {
            differences.push(json!({
                "kind": "extra_table",
                "side": "target",
                "table": table_name
            }));
        }
    }

    for (table_name, source_table) in &source_tables {
        let Some(target_table) = target_tables.get(table_name) else {
            continue;
        };
        // 테이블 레벨 collation 비교. 양쪽이 모두 Some(둘 다 MySQL에서 inspect)일 때만 비교하여
        // cross-engine(PostgreSQL은 table_collation=None) 비교로 인한 오탐을 피한다.
        if let (Some(source_collation), Some(target_collation)) =
            (&source_table.table_collation, &target_table.table_collation)
        {
            if source_collation != target_collation {
                differences.push(json!({
                    "kind": "table_collation_mismatch",
                    "table": table_name,
                    "source_collation": source_collation,
                    "target_collation": target_collation
                }));
            }
        }
        let source_columns: BTreeMap<String, &NormalizedColumn> = source_table
            .columns
            .iter()
            .map(|column| (column.name.clone(), column))
            .collect();
        let target_columns: BTreeMap<String, &NormalizedColumn> = target_table
            .columns
            .iter()
            .map(|column| (column.name.clone(), column))
            .collect();

        for column_name in source_columns.keys() {
            if !target_columns.contains_key(column_name) {
                differences.push(json!({
                    "kind": "missing_column",
                    "side": "target",
                    "table": table_name,
                    "column": column_name
                }));
            }
        }
        for column_name in target_columns.keys() {
            if !source_columns.contains_key(column_name) {
                differences.push(json!({
                    "kind": "extra_column",
                    "side": "target",
                    "table": table_name,
                    "column": column_name
                }));
            }
        }
        for (column_name, source_column) in &source_columns {
            let Some(target_column) = target_columns.get(column_name) else {
                continue;
            };
            if source_column.type_name != target_column.type_name {
                differences.push(json!({
                    "kind": "type_mismatch",
                    "table": table_name,
                    "column": column_name,
                    "source_type": source_column.type_name,
                    "target_type": target_column.type_name
                }));
            }
            if source_column.nullable != target_column.nullable {
                differences.push(json!({
                    "kind": "nullable_mismatch",
                    "table": table_name,
                    "column": column_name,
                    "source_nullable": source_column.nullable,
                    "target_nullable": target_column.nullable
                }));
            }
        }
    }

    differences
}

/// 스트리밍/배치 응답에서 공통으로 쓰이는 `error` 이벤트 리터럴을 생성한다.
/// `json!({"event":"error","request_id":request.request_id,"message":message})` 와
/// 바이트 단위로 동일한 payload 를 반환한다.
pub(crate) fn error_event(request: &Request, message: impl Into<String>) -> Value {
    json!({
        "event": "error",
        "request_id": request.request_id,
        "message": message.into()
    })
}

pub(crate) fn inspect(request: &Request) -> Vec<Value> {
    if let Some(endpoint) = request
        .payload
        .get("source")
        .and_then(|value| endpoint_from_value(value).ok())
    {
        let mut events = vec![phase_event(request, "inspect", "schema inspection started")];
        match inspect_live(&endpoint) {
            Ok(result) => events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "inspect",
                "success": true,
                "schema": result.schema,
                "unsupported_objects": result.unsupported_objects
            })),
            Err(err) => events.push(error_event(request, err)),
        }
        return events;
    }

    vec![
        phase_event(request, "inspect", "schema inspection started"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "inspect",
            "success": true,
            "schema": request.payload.get("schema").cloned().unwrap_or_else(|| json!({"tables": []})),
            "unsupported_objects": request.payload.get("unsupported_objects").cloned().unwrap_or_else(|| json!([]))
        }),
    ]
}

pub fn endpoint_from_value(value: &Value) -> Result<Endpoint, String> {
    let endpoint: Endpoint =
        serde_json::from_value(value.clone()).map_err(|err| format!("invalid endpoint: {err}"))?;
    if endpoint.engine != "mysql" && endpoint.engine != "postgresql" {
        return Err(format!("unsupported endpoint engine: {}", endpoint.engine));
    }
    if endpoint.host.trim().is_empty()
        || endpoint.user.trim().is_empty()
        || endpoint.database.trim().is_empty()
    {
        return Err("endpoint host, user, and database are required".to_string());
    }
    Ok(endpoint)
}

pub fn inspect_live(endpoint: &Endpoint) -> Result<InspectionResult, String> {
    match endpoint.engine.as_str() {
        "mysql" => inspect_mysql(endpoint),
        "postgresql" => inspect_postgresql(endpoint),
        other => Err(format!("unsupported endpoint engine: {other}")),
    }
}

fn inspect_mysql(endpoint: &Endpoint) -> Result<InspectionResult, String> {
    let schema_name = endpoint_schema(endpoint);
    let opts = mysql_opts(endpoint);
    let pool = mysql::Pool::new(opts).map_err(|err| format!("mysql pool error: {err}"))?;
    let mut conn = pool
        .get_conn()
        .map_err(|err| format!("mysql connection error: {err}"))?;
    let table_names: Vec<(String, Option<String>)> = conn
        .exec_map(
            inspect_tables_sql("mysql"),
            (&schema_name,),
            |(table_name, table_collation): (String, Option<String>)| (table_name, table_collation),
        )
        .map_err(|err| format!("mysql table inspect error: {err}"))?;
    let mut tables = Vec::new();

    for (table_name, table_collation) in table_names {
        let columns: Vec<NormalizedColumn> =
            conn
                .exec_map(
                    inspect_columns_sql("mysql"),
                    (&schema_name, &table_name),
                    |(
                        name,
                        type_name,
                        character_set,
                        collation,
                        is_nullable,
                        default_value,
                        extra,
                    ): (
                        String,
                        String,
                        Option<String>,
                        Option<String>,
                        String,
                        Option<String>,
                        String,
                    )| {
                        let type_name =
                            mysql_type_with_character_options(&type_name, character_set, collation);
                        NormalizedColumn {
                            name,
                            type_name: with_auto_increment_marker(&type_name, &extra),
                            default_value,
                            nullable: is_nullable.eq_ignore_ascii_case("YES"),
                            primary_key: false,
                            unique: false,
                        }
                    },
                )
                .map_err(|err| format!("mysql column inspect error: {err}"))?;
        let keys: Vec<(String, String)> = conn
            .exec_map(
                inspect_keys_sql("mysql"),
                (&schema_name, &table_name),
                |(name, constraint_type): (String, String)| (name, constraint_type),
            )
            .map_err(|err| format!("mysql key inspect error: {err}"))?;
        let foreign_key_rows: Vec<(String, String, String, String)> = conn
            .exec_map(
                inspect_foreign_keys_sql("mysql"),
                (&schema_name, &table_name),
                |(name, column, referenced_table, referenced_column): (
                    String,
                    String,
                    String,
                    String,
                )| (name, column, referenced_table, referenced_column),
            )
            .map_err(|err| format!("mysql FK inspect error: {err}"))?;
        let index_rows: Vec<(String, String, bool)> = conn
            .exec_map(
                inspect_indexes_sql("mysql"),
                (&schema_name, &table_name),
                |(name, column, is_unique): (String, String, u8)| (name, column, is_unique == 1),
            )
            .map_err(|err| format!("mysql index inspect error: {err}"))?;
        tables.push(NormalizedTable {
            name: table_name,
            columns: apply_key_flags(columns, &keys),
            indexes: group_indexes(index_rows),
            foreign_keys: group_foreign_keys(foreign_key_rows),
            table_collation: table_collation.filter(|value| !value.trim().is_empty()),
        });
    }

    let unsupported_objects = inspect_mysql_unsupported_objects(&mut conn, &schema_name)?;

    Ok(InspectionResult {
        schema: NormalizedSchema { tables },
        unsupported_objects,
    })
}

fn inspect_mysql_unsupported_objects(
    conn: &mut mysql::PooledConn,
    database: &str,
) -> Result<Vec<String>, String> {
    let mut objects = Vec::new();
    let deprecated_engines: Vec<String> = conn
        .exec_map(
            inspect_mysql_deprecated_engines_sql(),
            (database,),
            |(table, engine): (String, String)| format!("deprecated_engine:{table}:{engine}"),
        )
        .map_err(|err| format!("mysql deprecated engine inspect error: {err}"))?;
    objects.extend(deprecated_engines);
    let views: Vec<String> = conn
        .exec_map(
            "SELECT TABLE_NAME FROM information_schema.views WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME",
            (database,),
            |name: String| format!("view:{name}"),
        )
        .map_err(|err| format!("mysql view inspect error: {err}"))?;
    objects.extend(views);
    let triggers: Vec<String> = conn
        .exec_map(
            "SELECT TRIGGER_NAME FROM information_schema.triggers WHERE TRIGGER_SCHEMA = ? ORDER BY TRIGGER_NAME",
            (database,),
            |name: String| format!("trigger:{name}"),
        )
        .map_err(|err| format!("mysql trigger inspect error: {err}"))?;
    objects.extend(triggers);
    let routines: Vec<String> = conn
        .exec_map(
            "SELECT ROUTINE_NAME FROM information_schema.routines WHERE ROUTINE_SCHEMA = ? ORDER BY ROUTINE_NAME",
            (database,),
            |name: String| format!("routine:{name}"),
        )
        .map_err(|err| format!("mysql routine inspect error: {err}"))?;
    objects.extend(routines);
    Ok(objects)
}

fn inspect_mysql_deprecated_engines_sql() -> &'static str {
    "SELECT TABLE_NAME, ENGINE FROM information_schema.tables WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' AND ENGINE IN ('MyISAM') ORDER BY TABLE_NAME"
}

fn inspect_postgresql(endpoint: &Endpoint) -> Result<InspectionResult, String> {
    let schema_name = endpoint_schema(endpoint);
    let mut client = postgres_config(endpoint)
        .connect(NoTls)
        .map_err(|err| format!("postgresql connection error: {err}"))?;
    let table_rows = client
        .query(inspect_tables_sql("postgresql"), &[&schema_name])
        .map_err(|err| format!("postgresql table inspect error: {err}"))?;
    let mut tables = Vec::new();

    for row in table_rows {
        let table_name: String = row.get(0);
        let column_rows = client
            .query(
                inspect_columns_sql("postgresql"),
                &[&schema_name, &table_name],
            )
            .map_err(|err| format!("postgresql column inspect error: {err}"))?;
        let columns = column_rows
            .into_iter()
            .map(|column| {
                let name: String = column.get(0);
                let data_type: String = column.get(1);
                let is_nullable: String = column.get(2);
                let max_length: Option<i32> = column.get(3);
                let numeric_precision: Option<i32> = column.get(4);
                let numeric_scale: Option<i32> = column.get(5);
                let column_default: Option<String> = column.get(6);
                let is_identity: String = column.get(7);
                let type_name = postgresql_column_type(
                    &data_type,
                    max_length,
                    numeric_precision,
                    numeric_scale,
                );
                NormalizedColumn {
                    name,
                    type_name: with_postgresql_identity_marker(
                        &type_name,
                        column_default.as_deref(),
                        &is_identity,
                    ),
                    default_value: normalize_postgresql_default(
                        column_default.as_deref(),
                        &is_identity,
                    ),
                    nullable: is_nullable.eq_ignore_ascii_case("YES"),
                    primary_key: false,
                    unique: false,
                }
            })
            .collect();
        let key_rows = client
            .query(inspect_keys_sql("postgresql"), &[&schema_name, &table_name])
            .map_err(|err| format!("postgresql key inspect error: {err}"))?;
        let keys = key_rows
            .into_iter()
            .map(|row| {
                let name: String = row.get(0);
                let constraint_type: String = row.get(1);
                (name, constraint_type)
            })
            .collect::<Vec<_>>();
        let foreign_key_rows = client
            .query(
                inspect_foreign_keys_sql("postgresql"),
                &[&schema_name, &table_name],
            )
            .map_err(|err| format!("postgresql FK inspect error: {err}"))?;
        let foreign_keys = group_foreign_keys(
            foreign_key_rows
                .into_iter()
                .map(|row| {
                    let name: String = row.get(0);
                    let column: String = row.get(1);
                    let referenced_table: String = row.get(2);
                    let referenced_column: String = row.get(3);
                    (name, column, referenced_table, referenced_column)
                })
                .collect(),
        );
        let index_rows = client
            .query(
                inspect_indexes_sql("postgresql"),
                &[&schema_name, &table_name],
            )
            .map_err(|err| format!("postgresql index inspect error: {err}"))?;
        let indexes = group_indexes(
            index_rows
                .into_iter()
                .map(|row| {
                    let name: String = row.get(0);
                    let column: String = row.get(1);
                    let is_unique: bool = row.get(2);
                    (name, column, is_unique)
                })
                .collect(),
        );
        tables.push(NormalizedTable {
            name: table_name,
            columns: apply_key_flags(columns, &keys),
            indexes,
            foreign_keys,
            table_collation: None,
        });
    }

    let unsupported_objects = inspect_postgresql_unsupported_objects(&mut client, &schema_name)?;

    Ok(InspectionResult {
        schema: NormalizedSchema { tables },
        unsupported_objects,
    })
}

fn inspect_postgresql_unsupported_objects(
    client: &mut postgres::Client,
    schema_name: &str,
) -> Result<Vec<String>, String> {
    let mut objects = Vec::new();
    let views = client
        .query(
            "SELECT table_name FROM information_schema.views WHERE table_schema = $1 ORDER BY table_name",
            &[&schema_name],
        )
        .map_err(|err| format!("postgresql view inspect error: {err}"))?;
    objects.extend(
        views
            .into_iter()
            .map(|row| format!("view:{}", row.get::<_, String>(0))),
    );

    let triggers = client
        .query(
            "SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE trigger_schema = $1 ORDER BY trigger_name",
            &[&schema_name],
        )
        .map_err(|err| format!("postgresql trigger inspect error: {err}"))?;
    objects.extend(
        triggers
            .into_iter()
            .map(|row| format!("trigger:{}", row.get::<_, String>(0))),
    );

    let routines = client
        .query(
            "SELECT routine_name FROM information_schema.routines WHERE routine_schema = $1 ORDER BY routine_name",
            &[&schema_name],
        )
        .map_err(|err| format!("postgresql routine inspect error: {err}"))?;
    objects.extend(
        routines
            .into_iter()
            .map(|row| format!("routine:{}", row.get::<_, String>(0))),
    );

    Ok(objects)
}

/// 원본 DB의 View 정의를 수집한다. 전체 export 시에만 호출된다.
/// MySQL은 `SHOW CREATE VIEW`, PostgreSQL은 `pg_get_viewdef`를 사용한다.
pub(crate) fn collect_views(endpoint: &Endpoint) -> Result<Vec<NormalizedView>, String> {
    match endpoint.engine.as_str() {
        "mysql" => collect_mysql_views(endpoint),
        "postgresql" => collect_postgresql_views(endpoint),
        other => Err(format!("unsupported endpoint engine: {other}")),
    }
}

fn collect_mysql_views(endpoint: &Endpoint) -> Result<Vec<NormalizedView>, String> {
    let schema_name = endpoint_schema(endpoint);
    let opts = mysql_opts(endpoint);
    let pool = mysql::Pool::new(opts).map_err(|err| format!("mysql pool error: {err}"))?;
    let mut conn = pool
        .get_conn()
        .map_err(|err| format!("mysql connection error: {err}"))?;
    let view_names: Vec<String> = conn
        .exec_map(
            "SELECT TABLE_NAME FROM information_schema.views WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME",
            (&schema_name,),
            |name: String| name,
        )
        .map_err(|err| format!("mysql view list error: {err}"))?;

    let mut views = Vec::with_capacity(view_names.len());
    for name in view_names {
        // SHOW CREATE VIEW `name` → (View, Create View, character_set_client, collation_connection)
        let create_sql = format!("SHOW CREATE VIEW {}", quote_ident("mysql", &name));
        let row: Option<mysql::Row> = conn
            .query_first(create_sql)
            .map_err(|err| format!("mysql SHOW CREATE VIEW error for {name}: {err}"))?;
        let definition = row
            .as_ref()
            .and_then(|row| row.get::<String, _>(1))
            .ok_or_else(|| format!("mysql SHOW CREATE VIEW returned no definition for {name}"))?;
        views.push(NormalizedView { name, definition });
    }
    Ok(views)
}

fn collect_postgresql_views(endpoint: &Endpoint) -> Result<Vec<NormalizedView>, String> {
    let schema_name = endpoint_schema(endpoint);
    let mut client = postgres_config(endpoint)
        .connect(NoTls)
        .map_err(|err| format!("postgresql connection error: {err}"))?;
    let rows = client
        .query(
            "SELECT table_name, pg_get_viewdef(format('%I.%I', table_schema, table_name)::regclass, true) \
             FROM information_schema.views WHERE table_schema = $1 ORDER BY table_name",
            &[&schema_name],
        )
        .map_err(|err| format!("postgresql view list error: {err}"))?;
    let mut views = Vec::with_capacity(rows.len());
    for row in rows {
        let name: String = row.get(0);
        let body: String = row.get(1);
        // pg_get_viewdef는 본문(SELECT ...)만 반환하므로 CREATE 문으로 감싼다.
        let definition = format!(
            "CREATE OR REPLACE VIEW {} AS\n{}",
            quote_ident("postgresql", &name),
            body
        );
        views.push(NormalizedView { name, definition });
    }
    Ok(views)
}

/// import 시점에 View 정의 SQL을 정화한다.
/// - MySQL `DEFINER=...` 절 제거 (대상 서버에 해당 유저가 없으면 view가 깨짐)
/// - `SQL SECURITY DEFINER` → `SQL SECURITY INVOKER` (case-insensitive)
/// - 원본 schema 한정자(`source_db`.) 제거 (대상 schema가 다를 수 있음)
///
/// SQL 키워드는 대소문자를 구분하지 않으므로 DEFINER/SQL SECURITY 처리는 case-insensitive로 수행한다.
pub(crate) fn sanitize_view_definition(definition: &str, source_schema: &str, engine: &str) -> String {
    let mut sql = definition.to_string();
    if engine == "mysql" {
        sql = strip_mysql_definer(&sql);
        sql = replace_ignore_ascii_case(&sql, "SQL SECURITY DEFINER", "SQL SECURITY INVOKER");
    }
    if !source_schema.trim().is_empty() {
        // `source_db`.`obj` → `obj`  (quote_ident 기준 인용 문자 사용)
        let quoted_db = quote_ident(engine, source_schema);
        sql = sql.replace(&format!("{quoted_db}."), "");
    }
    sql
}

/// `needle`(ASCII 대소문자 무시)을 모두 `replacement`로 치환한다.
/// `needle` 안의 내부 공백은 정확히 한 칸으로 가정한다 (SQL 키워드 정규형).
fn replace_ignore_ascii_case(haystack: &str, needle: &str, replacement: &str) -> String {
    if needle.is_empty() {
        return haystack.to_string();
    }
    let lower_haystack = haystack.to_ascii_lowercase();
    let lower_needle = needle.to_ascii_lowercase();
    let mut result = String::with_capacity(haystack.len());
    let mut cursor = 0;
    while let Some(rel) = lower_haystack[cursor..].find(&lower_needle) {
        let start = cursor + rel;
        result.push_str(&haystack[cursor..start]);
        result.push_str(replacement);
        cursor = start + needle.len();
    }
    result.push_str(&haystack[cursor..]);
    result
}

/// `CREATE ALGORITHM=... DEFINER=\`u\`@\`h\` SQL SECURITY ... VIEW` 에서 DEFINER 절만 제거한다.
/// `DEFINER=` 키워드 매칭은 case-insensitive로 수행한다.
fn strip_mysql_definer(sql: &str) -> String {
    let lower = sql.to_ascii_lowercase();
    let Some(start) = lower.find("definer=") else {
        return sql.to_string();
    };
    // DEFINER= 다음부터 공백을 만나기 전까지가 한 토큰 (`user`@`host` 또는 CURRENT_USER 등).
    // 백틱 안에 공백이 들어갈 수 있으므로 백틱 균형을 추적한다.
    let bytes = sql.as_bytes();
    let mut idx = start + "DEFINER=".len();
    let mut in_backtick = false;
    while idx < bytes.len() {
        let ch = bytes[idx];
        if ch == b'`' {
            in_backtick = !in_backtick;
        } else if ch == b' ' && !in_backtick {
            break;
        }
        idx += 1;
    }
    // start 직전의 공백 하나도 함께 제거하여 "CREATE  SQL SECURITY" 처럼 이중 공백이 남지 않게 한다.
    let prefix_end = sql[..start].trim_end().len();
    // idx 위치의 공백은 남겨 토큰 구분을 유지한다.
    let mut result = String::with_capacity(sql.len());
    result.push_str(&sql[..prefix_end]);
    result.push_str(&sql[idx..]);
    result
}

pub(crate) fn drop_view_sql(engine: &str, view: &str) -> String {
    format!("DROP VIEW IF EXISTS {}", quote_ident(engine, view))
}

/// sanitize 후에도 MySQL 정의에 `DEFINER=` 또는 `SQL SECURITY DEFINER`가 남아있는지 검사한다.
/// 정상 경로(`SHOW CREATE VIEW`의 대문자/단일공백 정규화 출력)는 sanitize가 모두 처리하므로
/// 여기서 잔존이 감지된다는 것은 탭/주석을 끼운 비정규(변조 의심) 정의라는 뜻 → fail-closed로 거부한다.
pub(crate) fn mysql_definition_has_residual_definer(sql: &str) -> bool {
    // 주석(-- 라인, /* */ 블록)을 공백으로 치환하고, 모든 공백류를 단일 공백으로 정규화한 검사용 사본.
    let mut cleaned = String::with_capacity(sql.len());
    let bytes = sql.as_bytes();
    let len = bytes.len();
    let mut i = 0;
    while i < len {
        // allow_hash=false: '#' 은 주석이 아니라 리터럴로 취급(더 보수적, fail-closed 보존).
        if let Some(end) = skip_sql_comment(bytes, i, false) {
            i = end;
            cleaned.push(' ');
        } else {
            cleaned.push(bytes[i] as char);
            i += 1;
        }
    }
    let normalized = cleaned
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    // "definer =" (공백 포함) 및 "definer=" 모두 잡기 위해 공백 제거 사본도 확인.
    let no_space = normalized.replace(' ', "");
    no_space.contains("definer=") || normalized.contains("sql security definer")
}

/// View 정의가 단일 `CREATE [OR REPLACE] VIEW ...` 문인지 가볍게 검증한다.
///
/// 주목적은 PostgreSQL `batch_execute` 경로다 — 이 드라이버는 세미콜론으로 구분된
/// multi-statement를 모두 실행하므로, 변조된 manifest가 `CREATE VIEW x AS ...; DROP TABLE y; GRANT ...`
/// 같은 SQL 체인을 심으면 그대로 실행된다. MySQL `query_drop`은 기본적으로 multi-statement를
/// 거부하지만, 일관성과 방어를 위해 양쪽 엔진 모두에 동일한 shape 검증을 적용한다.
///
/// 허용: 문자열 리터럴/식별자/주석 바깥의 세미콜론이 끝에만(또는 없음) 존재하고,
/// 첫 유효 토큰이 `CREATE`인 경우. 그 외(추가 statement, CREATE 아닌 시작)는 거부한다.
pub(crate) fn validate_single_view_statement(sql: &str) -> Result<(), String> {
    let trimmed = sql.trim();
    if trimmed.is_empty() {
        return Err("empty view definition".to_string());
    }

    // 문자열 리터럴('...'), 식별자 인용(`...` / "..."), 주석(-- , /* */) 바깥의 세미콜론을 찾는다.
    let bytes = trimmed.as_bytes();
    let mut i = 0;
    let len = bytes.len();
    while i < len {
        // 주석(-- , /* */) 은 공유 스캐너로 스킵한다. allow_hash=false 로 '#' 은 리터럴 취급.
        // 기존 루프의 trailing `i += 1` 을 보존하기 위해 end + 1 로 재개한다.
        if let Some(end) = skip_sql_comment(bytes, i, false) {
            i = end + 1;
            continue;
        }
        let ch = bytes[i];
        match ch {
            b'\'' => {
                // 작은따옴표 문자열 — '' escape 처리
                i += 1;
                while i < len {
                    if bytes[i] == b'\'' {
                        if i + 1 < len && bytes[i + 1] == b'\'' {
                            i += 2;
                            continue;
                        }
                        break;
                    }
                    i += 1;
                }
            }
            b'"' => {
                i += 1;
                while i < len {
                    if bytes[i] == b'"' {
                        if i + 1 < len && bytes[i + 1] == b'"' {
                            i += 2;
                            continue;
                        }
                        break;
                    }
                    i += 1;
                }
            }
            b'`' => {
                i += 1;
                while i < len && bytes[i] != b'`' {
                    i += 1;
                }
            }
            b';' => {
                // 끝에 오는 세미콜론(뒤에 공백만 남음)은 허용, 그 외는 추가 statement로 간주.
                let rest = trimmed[i + 1..].trim();
                if rest.is_empty() {
                    break;
                }
                return Err("view definition contains multiple statements".to_string());
            }
            _ => {}
        }
        i += 1;
    }

    // CREATE [OR REPLACE] [TEMP|TEMPORARY] [ALGORITHM=..] [DEFINER=..] [SQL SECURITY ..] VIEW 형태인지 확인.
    // 단순히 첫 토큰이 CREATE 인 것만으로는 부족하다 — CREATE USER / CREATE TABLE AS SELECT 같은
    // 단일 statement도 통과해버리므로, 반드시 view-modifier 뒤에 VIEW 키워드가 와야 한다.
    let tokens: Vec<&str> = trimmed
        .split(|c: char| c.is_whitespace())
        .filter(|t| !t.is_empty())
        .collect();
    let mut iter = tokens.iter();
    match iter.next() {
        Some(tok) if tok.eq_ignore_ascii_case("create") => {}
        Some(tok) => {
            return Err(format!(
                "view definition must start with CREATE, got: {tok}"
            ))
        }
        None => return Err("empty view definition".to_string()),
    }
    // CREATE 와 VIEW 사이에 올 수 있는 view-modifier 토큰만 허용한다.
    // (sanitize 후 DEFINER 절은 제거되지만, 다른 형태를 대비해 보수적으로 허용 목록을 둔다)
    let mut saw_view = false;
    while let Some(tok) = iter.next() {
        if tok.eq_ignore_ascii_case("view") {
            saw_view = true;
            break;
        }
        let lower = tok.to_ascii_lowercase();
        let allowed = lower == "or"
            || lower == "replace"
            || lower == "temp"
            || lower == "temporary"
            || lower == "recursive"
            || lower == "security"
            || lower == "invoker"
            || lower == "definer"
            || lower == "undefined"
            || lower == "merge"
            || lower == "temptable"
            || lower == "sql"
            || lower.starts_with("algorithm=")
            || lower.starts_with("definer=")
            || lower == "=";
        if !allowed {
            return Err(format!(
                "view definition must be CREATE ... VIEW, unexpected token before VIEW: {tok}"
            ));
        }
    }
    if !saw_view {
        return Err("view definition must contain the VIEW keyword".to_string());
    }
    Ok(())
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    
    
    
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::{single_pk_table_with_collation};

    #[test]
    fn normalized_schema_diff_reports_table_collation_mismatch() {
        let src = NormalizedSchema {
            tables: vec![single_pk_table_with_collation(Some("utf8mb4_general_ci"))],
        };
        let tgt = NormalizedSchema {
            tables: vec![single_pk_table_with_collation(Some("utf8mb4_unicode_ci"))],
        };
        let diffs = normalized_schema_diff(&src, &tgt);
        assert!(
            diffs
                .iter()
                .any(|d| d["kind"] == "table_collation_mismatch"),
            "{diffs:?}"
        );
    }

    #[test]
    fn normalized_schema_diff_ignores_table_collation_when_one_side_none() {
        // cross-engine(한쪽 table_collation=None)에서는 collation 비교로 오탐을 내지 않는다.
        let src = NormalizedSchema {
            tables: vec![single_pk_table_with_collation(Some("utf8mb4_general_ci"))],
        };
        let tgt = NormalizedSchema {
            tables: vec![single_pk_table_with_collation(None)],
        };
        let diffs = normalized_schema_diff(&src, &tgt);
        assert!(
            !diffs
                .iter()
                .any(|d| d["kind"] == "table_collation_mismatch"),
            "{diffs:?}"
        );
    }

    #[test]
    fn mysql_deprecated_engine_sql_targets_table_engines() {
        let sql = inspect_mysql_deprecated_engines_sql();

        assert!(sql.contains("information_schema.tables"));
        assert!(sql.contains("ENGINE"));
        assert!(sql.contains("MyISAM"));
    }

    #[test]
    fn strip_mysql_definer_removes_definer_clause() {
        let sql = "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`localhost` SQL SECURITY DEFINER VIEW `v` AS select 1";
        let stripped = strip_mysql_definer(sql);
        assert!(!stripped.contains("DEFINER="));
        assert!(stripped.contains("CREATE ALGORITHM=UNDEFINED"));
        assert!(stripped.contains("SQL SECURITY DEFINER VIEW `v` AS select 1"));
    }

    #[test]
    fn strip_mysql_definer_handles_current_user_form() {
        let sql = "CREATE DEFINER=CURRENT_USER VIEW `v` AS select 1";
        let stripped = strip_mysql_definer(sql);
        assert_eq!(stripped, "CREATE VIEW `v` AS select 1");
    }

    #[test]
    fn strip_mysql_definer_noop_without_definer() {
        let sql = "CREATE VIEW `v` AS select 1";
        assert_eq!(strip_mysql_definer(sql), sql);
    }

    #[test]
    fn sanitize_view_definition_strips_definer_security_and_source_schema() {
        let sql = "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`localhost` SQL SECURITY DEFINER \
                   VIEW `ref_vendor_codes_view` AS select * from `dataflare`.`vendor_codes`";
        let out = sanitize_view_definition(sql, "dataflare", "mysql");
        assert!(!out.contains("DEFINER="));
        assert!(out.contains("SQL SECURITY INVOKER"));
        assert!(!out.contains("`dataflare`."));
        assert!(out.contains("from `vendor_codes`"));
    }

    #[test]
    fn sanitize_view_definition_postgresql_strips_source_schema_only() {
        let sql = "CREATE OR REPLACE VIEW \"v\" AS SELECT * FROM \"app\".\"users\"";
        let out = sanitize_view_definition(sql, "app", "postgresql");
        // PG는 DEFINER/SQL SECURITY 처리를 하지 않는다.
        assert!(out.contains("SELECT * FROM \"users\""));
        assert!(!out.contains("\"app\"."));
    }

    #[test]
    fn drop_view_sql_uses_drop_view() {
        assert_eq!(drop_view_sql("mysql", "v"), "DROP VIEW IF EXISTS `v`");
        assert_eq!(
            drop_view_sql("postgresql", "v"),
            "DROP VIEW IF EXISTS \"v\""
        );
    }

    #[test]
    fn strip_mysql_definer_is_case_insensitive() {
        let sql = "create definer=`root`@`localhost` sql security definer view `v` as select 1";
        let stripped = strip_mysql_definer(sql);
        assert!(!stripped.to_ascii_lowercase().contains("definer="));
        assert!(stripped.contains("sql security definer view `v` as select 1"));
    }

    #[test]
    fn sanitize_view_definition_lowercase_security_clause_becomes_invoker() {
        // 변조/비정규 정의: 소문자 sql security definer 도 INVOKER 로 바뀌어야 한다.
        let sql = "CREATE sql security definer VIEW `leak` AS SELECT 1";
        let out = sanitize_view_definition(sql, "", "mysql");
        assert!(out.contains("SQL SECURITY INVOKER"));
        assert!(!out.to_ascii_lowercase().contains("security definer"));
    }

    #[test]
    fn replace_ignore_ascii_case_replaces_all_case_variants() {
        let out = replace_ignore_ascii_case("a FOO b foo c FoO", "foo", "X");
        assert_eq!(out, "a X b X c X");
    }

    #[test]
    fn validate_single_view_statement_accepts_plain_create_view() {
        assert!(validate_single_view_statement("CREATE VIEW `v` AS SELECT 1").is_ok());
        assert!(
            validate_single_view_statement("create or replace view \"v\" as select 1;").is_ok()
        );
    }

    #[test]
    fn validate_single_view_statement_rejects_multi_statement() {
        let sql = "CREATE VIEW \"v\" AS SELECT 1; DROP TABLE customers";
        let err = validate_single_view_statement(sql).unwrap_err();
        assert!(err.contains("multiple statements"));
    }

    #[test]
    fn validate_single_view_statement_rejects_non_create_start() {
        let err = validate_single_view_statement("DROP TABLE customers").unwrap_err();
        assert!(err.contains("must start with CREATE"));
    }

    #[test]
    fn validate_single_view_statement_rejects_create_non_view() {
        // CREATE 로 시작하지만 VIEW가 아닌 단일 statement는 거부해야 한다.
        assert!(validate_single_view_statement("CREATE USER attacker IDENTIFIED BY 'p'").is_err());
        assert!(
            validate_single_view_statement("CREATE TABLE stolen AS SELECT * FROM secrets").is_err()
        );
        let err = validate_single_view_statement("CREATE DATABASE evil").unwrap_err();
        assert!(err.contains("VIEW"));
    }

    #[test]
    fn validate_single_view_statement_accepts_view_with_modifiers() {
        // MySQL SHOW CREATE VIEW 정규 출력(정화 후) 형태
        assert!(validate_single_view_statement(
            "CREATE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `v` AS select 1"
        )
        .is_ok());
        assert!(validate_single_view_statement("CREATE OR REPLACE VIEW \"v\" AS SELECT 1").is_ok());
    }

    #[test]
    fn mysql_residual_definer_detects_tab_and_comment_variants() {
        // sanitize가 놓칠 수 있는 비정규 변형들 — fail-closed로 거부되어야 한다.
        assert!(mysql_definition_has_residual_definer(
            "CREATE SQL\tSECURITY\tDEFINER VIEW `v` AS SELECT 1"
        ));
        assert!(mysql_definition_has_residual_definer(
            "CREATE SQL/**/SECURITY/**/DEFINER VIEW `v` AS SELECT 1"
        ));
        assert!(mysql_definition_has_residual_definer(
            "CREATE DEFINER = `root`@`localhost` VIEW `v` AS SELECT 1"
        ));
    }

    #[test]
    fn mysql_residual_definer_clean_after_sanitize_is_false() {
        // 정상 정의를 sanitize 하면 잔존 DEFINER가 없어야 한다.
        let sql = "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`localhost` SQL SECURITY DEFINER \
                   VIEW `v` AS SELECT 1";
        let sanitized = sanitize_view_definition(sql, "", "mysql");
        assert!(!mysql_definition_has_residual_definer(&sanitized));
    }

    #[test]
    fn validate_single_view_statement_allows_semicolon_inside_string_literal() {
        // SELECT 본문의 문자열 리터럴 안 세미콜론은 statement 구분자가 아니다.
        let sql = "CREATE VIEW `v` AS SELECT 'a;b' AS s";
        assert!(validate_single_view_statement(sql).is_ok());
    }

    #[test]
    fn validate_single_view_statement_ignores_semicolon_in_comment() {
        let sql = "CREATE VIEW `v` AS SELECT 1 -- drop; me\n";
        assert!(validate_single_view_statement(sql).is_ok());
    }
}
