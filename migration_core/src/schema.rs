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

pub(crate) fn validate_target_check_names(adapter: &mut LiveAdapter, target_schema: &str, schema: &NormalizedSchema) -> Result<(), String> {
    let LiveAdapter::MySql(conn) = adapter else { return Ok(()); };
    let selected: std::collections::BTreeSet<_> = schema.tables.iter().map(|table| table.name.as_str()).collect();
    let mut names = std::collections::BTreeSet::new();
    for table in &schema.tables {
        for check in &table.checks {
            if !names.insert(check.name.to_lowercase()) { return Err(format!("import_plan_invalid: duplicate schema-scoped CHECK name {}",check.name)); }
            let owners: Vec<String> = conn.exec(
                "SELECT TABLE_NAME FROM information_schema.table_constraints WHERE CONSTRAINT_SCHEMA=? AND CONSTRAINT_NAME=? AND CONSTRAINT_TYPE='CHECK'",
                (target_schema,&check.name),
            ).map_err(|err| format!("CHECK name collision preflight failed: {err}"))?;
            if let Some(owner) = owners.iter().find(|owner| !selected.contains(owner.as_str())) {
                return Err(format!("import_plan_invalid: CHECK {} already belongs to unselected target table {owner}",check.name));
            }
        }
    }
    Ok(())
}

/// per-table 스키마 inspect 를 엔진 독립적으로 수행하기 위한 어댑터.
/// 드라이버 API 차이(mysql exec_map 클로저 vs postgres client.query + row.get 인덱스)는
/// 각 impl 안에 캡슐화하고, inspect_generic 은 table_names → columns/keys/foreign_keys/indexes
/// → apply_key_flags/group_indexes/group_foreign_keys → NormalizedTable push 의 5단계 시퀀스만
/// 담당한다. DB 연결/쿼리는 tunnelforge-core 소유 그대로 유지한다.
trait InspectAdapter {
    fn enrich_table(&mut self, _schema: &str, _table: &mut NormalizedTable) -> Result<(), String> { Ok(()) }
    fn table_names(&mut self, schema: &str) -> Result<Vec<(String, Option<String>)>, String>;
    fn columns(&mut self, schema: &str, table: &str) -> Result<Vec<NormalizedColumn>, String>;
    fn keys(&mut self, schema: &str, table: &str) -> Result<Vec<(String, String)>, String>;
    fn foreign_keys(
        &mut self,
        schema: &str,
        table: &str,
    ) -> Result<Vec<(String, String, String, String, String, String)>, String>;
    fn indexes(
        &mut self,
        schema: &str,
        table: &str,
    ) -> Result<Vec<(String, String, Option<u32>, bool)>, String>;
    fn unsupported_objects(&mut self, schema: &str) -> Result<Vec<String>, String>;
}

fn inspect_generic<A: InspectAdapter>(
    adapter: &mut A,
    schema_name: &str,
) -> Result<InspectionResult, String> {
    let table_names = adapter.table_names(schema_name)?;
    let mut tables = Vec::new();

    for (table_name, table_collation) in table_names {
        let columns = adapter.columns(schema_name, &table_name)?;
        let keys = adapter.keys(schema_name, &table_name)?;
        let foreign_key_rows = adapter.foreign_keys(schema_name, &table_name)?;
        let index_rows = adapter.indexes(schema_name, &table_name)?;
        let mut table = NormalizedTable {
            name: table_name,
            columns: apply_key_flags(columns, &keys),
            indexes: group_indexes(index_rows),
            foreign_keys: group_foreign_keys(foreign_key_rows)?,
            table_collation: table_collation.filter(|value| !value.trim().is_empty()),
            auto_increment: None,
            comment: None,
            checks: Vec::new(),
        };
        adapter.enrich_table(schema_name, &mut table)?;
        tables.push(table);
    }

    let unsupported_objects = adapter.unsupported_objects(schema_name)?;

    Ok(InspectionResult {
        schema: NormalizedSchema { tables },
        unsupported_objects,
    })
}

struct MysqlInspectAdapter {
    conn: mysql::PooledConn,
}

impl InspectAdapter for MysqlInspectAdapter {
    fn enrich_table(&mut self, schema: &str, table: &mut NormalizedTable) -> Result<(), String> {
        let options: Option<(Option<u64>, String)> = self.conn.exec_first(
            "SELECT AUTO_INCREMENT,TABLE_COMMENT FROM information_schema.tables WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", (schema,&table.name)
        ).map_err(|err| format!("mysql table options inspect error: {err}"))?;
        if let Some((counter, comment)) = options {
            table.auto_increment = counter;
            table.comment = if comment.is_empty() { None } else { Some(comment) };
        }
        let checks: Vec<(String,String,String)> = self.conn.exec(
            "SELECT tc.CONSTRAINT_NAME,cc.CHECK_CLAUSE,tc.ENFORCED FROM information_schema.table_constraints tc JOIN information_schema.check_constraints cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA=? AND tc.TABLE_NAME=? AND tc.CONSTRAINT_TYPE='CHECK' ORDER BY tc.CONSTRAINT_NAME",
            (schema,&table.name)
        ).map_err(|err| format!("mysql CHECK inspect error: {err}"))?;
        table.checks = checks.into_iter().map(|(name,expression,enforced)| NormalizedCheck {name,expression,enforced:enforced=="YES"}).collect();
        let visibility: Vec<(String,String)> = self.conn.exec(
            "SELECT DISTINCT INDEX_NAME,IS_VISIBLE FROM information_schema.statistics WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", (schema,&table.name)
        ).map_err(|err| format!("mysql index visibility inspect error: {err}"))?;
        for index in &mut table.indexes {
            index.visible = visibility.iter().find(|(name,_)| name==&index.name).map(|(_,value)| value=="YES");
        }
        Ok(())
    }
    fn table_names(&mut self, schema: &str) -> Result<Vec<(String, Option<String>)>, String> {
        self.conn
            .exec_map(
                inspect_tables_sql("mysql"),
                (schema,),
                |(table_name, table_collation): (String, Option<String>)| {
                    (table_name, table_collation)
                },
            )
            .map_err(|err| format!("mysql table inspect error: {err}"))
    }

    fn columns(&mut self, schema: &str, table: &str) -> Result<Vec<NormalizedColumn>, String> {
        self.conn
            .exec_map(
                inspect_columns_sql("mysql"),
                (schema, table),
                |(
                    name,
                    type_name,
                    character_set,
                    collation,
                    is_nullable,
                    default_value,
                    extra,
                    column_comment,
                ): (
                    String,
                    String,
                    Option<String>,
                    Option<String>,
                    String,
                    Option<String>,
                    String,
                    String,
                )| {
                    let type_name =
                        mysql_type_with_character_options(&type_name, character_set, collation);
                    let on_update = extra.to_ascii_lowercase().find("on update ")
                        .map(|start| extra[start + "on update ".len()..].trim().to_string());
                    // MySQL exposes literal strings without SQL quotes. Preserve empty
                    // strings, the word null, and function-looking text as literals.
                    let default_value = default_value.map(|value| {
                        if type_name.starts_with("timestamp") || type_name.starts_with("datetime")
                            || type_name.starts_with("time(") || extra.contains("DEFAULT_GENERATED") {
                            value
                        } else if type_name.starts_with("varchar") || type_name.starts_with("char")
                            || type_name.starts_with("enum") || type_name.starts_with("set")
                            || type_name.contains("text") {
                            format!("'{}'", value.replace('\'', "''"))
                        } else { value }
                    });
                    NormalizedColumn {
                        name,
                        type_name: with_auto_increment_marker(&type_name, &extra),
                        default_value,
                        nullable: is_nullable.eq_ignore_ascii_case("YES"),
                        primary_key: false,
                        unique: false,
                        comment: if column_comment.is_empty() { None } else { Some(column_comment) },
                        default_is_expression: extra.contains("DEFAULT_GENERATED"),
                        on_update,
                    }
                },
            )
            .map_err(|err| format!("mysql column inspect error: {err}"))
    }

    fn keys(&mut self, schema: &str, table: &str) -> Result<Vec<(String, String)>, String> {
        self.conn
            .exec_map(
                inspect_keys_sql("mysql"),
                (schema, table),
                |(name, constraint_type): (String, String)| (name, constraint_type),
            )
            .map_err(|err| format!("mysql key inspect error: {err}"))
    }

    fn foreign_keys(
        &mut self,
        schema: &str,
        table: &str,
    ) -> Result<Vec<(String, String, String, String, String, String)>, String> {
        self.conn
            .exec_map(
                inspect_foreign_keys_sql("mysql"),
                (schema, table),
                |(name, column, referenced_table, referenced_column, on_delete, on_update): (
                    String, String, String, String, String, String,
                )| { (name, column, referenced_table, referenced_column, on_delete, on_update) },
            )
            .map_err(|err| format!("mysql FK inspect error: {err}"))
    }

    fn indexes(
        &mut self,
        schema: &str,
        table: &str,
    ) -> Result<Vec<(String, String, Option<u32>, bool)>, String> {
        self.conn
            .exec_map(
                inspect_indexes_sql("mysql"),
                (schema, table),
                |(name, column, sub_part, is_unique): (String, String, Option<u32>, u8)| {
                    (name, column, sub_part, is_unique == 1)
                },
            )
            .map_err(|err| format!("mysql index inspect error: {err}"))
    }

    fn unsupported_objects(&mut self, schema: &str) -> Result<Vec<String>, String> {
        inspect_mysql_unsupported_objects(&mut self.conn, schema)
    }
}

fn inspect_mysql(endpoint: &Endpoint) -> Result<InspectionResult, String> {
    let schema_name = endpoint_schema(endpoint);
    let opts = mysql_opts(endpoint);
    let pool = mysql::Pool::new(opts).map_err(|err| format!("mysql pool error: {err}"))?;
    let mut conn = pool
        .get_conn()
        .map_err(|err| format!("mysql connection error: {err}"))?;
    // Allocator values must be observed now, not served from MySQL's default
    // 24-hour information_schema statistics cache.
    conn.query_drop("SET SESSION information_schema_stats_expiry=0")
        .map_err(|err| format!("mysql fresh metadata session error: {err}"))?;
    let mut adapter = MysqlInspectAdapter { conn };
    inspect_generic(&mut adapter, &schema_name)
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
    let columns: Vec<(String, String, String, Option<String>, String)> = conn.exec(
        "SELECT TABLE_NAME, COLUMN_NAME, EXTRA, COLUMN_DEFAULT, DATA_TYPE FROM information_schema.columns WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME, ORDINAL_POSITION",
        (database,),
    ).map_err(|err| format!("mysql column fidelity inspect error: {err}"))?;
    for (table, column, extra, default_value, _data_type) in columns {
        if extra.contains("VIRTUAL GENERATED") || extra.contains("STORED GENERATED") {
            objects.push(format!("generated_column:{table}:{column}"));
        }
        if extra.contains("DEFAULT_GENERATED") {
            if let Some(value) = default_value {
                if !supported_mysql_default_expression(&value) {
                    objects.push(format!("unsupported_default:{table}:{column}"));
                }
            }
        }
    }
    let checks: Vec<(String, String, String)> = conn.exec(
        "SELECT tc.TABLE_NAME,tc.CONSTRAINT_NAME,cc.CHECK_CLAUSE FROM information_schema.table_constraints tc JOIN information_schema.check_constraints cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA=? AND tc.CONSTRAINT_TYPE='CHECK'",
        (database,),
    ).map_err(|err| format!("mysql check inspect error: {err}"))?;
    objects.extend(checks.into_iter().filter(|(_,_,expression)| !is_safe_check_expression(expression)).map(|(table, name,_)| format!("check_constraint:{table}:{name}")));
    let indexes: Vec<(String, String)> = conn.exec(
        "SELECT DISTINCT TABLE_NAME, INDEX_NAME FROM information_schema.statistics WHERE TABLE_SCHEMA=? AND (COLUMN_NAME IS NULL OR COLLATION='D' OR INDEX_TYPE NOT IN ('BTREE','HASH'))",
        (database,),
    ).map_err(|err| format!("mysql advanced index inspect error: {err}"))?;
    objects.extend(indexes.into_iter().map(|(table, name)| format!("unsupported_index:{table}:{name}")));
    let foreign_keys: Vec<(String, String)> = conn.exec(
        "SELECT DISTINCT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.key_column_usage WHERE TABLE_SCHEMA=? AND REFERENCED_TABLE_SCHEMA IS NOT NULL AND REFERENCED_TABLE_SCHEMA<>TABLE_SCHEMA",
        (database,),
    ).map_err(|err| format!("mysql cross-schema FK inspect error: {err}"))?;
    objects.extend(foreign_keys.into_iter().map(|(table, name)| format!("cross_schema_fk:{table}:{name}")));
    Ok(objects)
}

fn inspect_mysql_deprecated_engines_sql() -> &'static str {
    "SELECT TABLE_NAME, ENGINE FROM information_schema.tables WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' AND ENGINE IN ('MyISAM') ORDER BY TABLE_NAME"
}

struct PostgresInspectAdapter {
    client: postgres::Client,
}

impl InspectAdapter for PostgresInspectAdapter {
    fn table_names(&mut self, schema: &str) -> Result<Vec<(String, Option<String>)>, String> {
        let rows = self
            .client
            .query(inspect_tables_sql("postgresql"), &[&schema])
            .map_err(|err| format!("postgresql table inspect error: {err}"))?;
        Ok(rows
            .into_iter()
            .map(|row| {
                let table_name: String = row.get(0);
                // PostgreSQL 은 table-level collation 을 노출하지 않으므로 항상 None.
                (table_name, None)
            })
            .collect())
    }

    fn columns(&mut self, schema: &str, table: &str) -> Result<Vec<NormalizedColumn>, String> {
        let column_rows = self
            .client
            .query(inspect_columns_sql("postgresql"), &[&schema, &table])
            .map_err(|err| format!("postgresql column inspect error: {err}"))?;
        Ok(column_rows
            .into_iter()
            .map(|column| {
                let name: String = column.get(0);
                let is_nullable: String = column.get(2);
                let column_default: Option<String> = column.get(6);
                let is_identity: String = column.get(7);
                let type_name: String = column.get(8);
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
                    comment: None,
                    default_is_expression: false,
                    on_update: None,
                }
            })
            .collect())
    }

    fn keys(&mut self, schema: &str, table: &str) -> Result<Vec<(String, String)>, String> {
        let key_rows = self
            .client
            .query(inspect_keys_sql("postgresql"), &[&schema, &table])
            .map_err(|err| format!("postgresql key inspect error: {err}"))?;
        Ok(key_rows
            .into_iter()
            .map(|row| {
                let name: String = row.get(0);
                let constraint_type: String = row.get(1);
                (name, constraint_type)
            })
            .collect())
    }

    fn foreign_keys(
        &mut self,
        schema: &str,
        table: &str,
    ) -> Result<Vec<(String, String, String, String, String, String)>, String> {
        let foreign_key_rows = self
            .client
            .query(inspect_foreign_keys_sql("postgresql"), &[&schema, &table])
            .map_err(|err| format!("postgresql FK inspect error: {err}"))?;
        Ok(foreign_key_rows
            .into_iter()
            .map(|row| {
                let name: String = row.get(0);
                let column: String = row.get(1);
                let referenced_table: String = row.get(2);
                let referenced_column: String = row.get(3);
                let on_delete: String = row.get(4);
                let on_update: String = row.get(5);
                (name, column, referenced_table, referenced_column, on_delete, on_update)
            })
            .collect())
    }

    fn indexes(
        &mut self,
        schema: &str,
        table: &str,
    ) -> Result<Vec<(String, String, Option<u32>, bool)>, String> {
        let index_rows = self
            .client
            .query(inspect_indexes_sql("postgresql"), &[&schema, &table])
            .map_err(|err| format!("postgresql index inspect error: {err}"))?;
        Ok(index_rows
            .into_iter()
            .map(|row| {
                let name: String = row.get(0);
                let column: String = row.get(1);
                let is_unique: bool = row.get(2);
                // postgresql은 MySQL식 prefix 인덱스가 없으므로 항상 full(None).
                (name, column, None, is_unique)
            })
            .collect())
    }

    fn unsupported_objects(&mut self, schema: &str) -> Result<Vec<String>, String> {
        inspect_postgresql_unsupported_objects(&mut self.client, schema)
    }
}

fn inspect_postgresql(endpoint: &Endpoint) -> Result<InspectionResult, String> {
    let schema_name = endpoint_schema(endpoint);
    let mut client = postgres_config(endpoint)
        .connect(NoTls)
        .map_err(|err| format!("postgresql connection error: {err}"))?;
    client.batch_execute("SET DateStyle = 'ISO, YMD'; SET TIME ZONE 'UTC'")
        .map_err(|err| format!("postgresql metadata session setup error: {err}"))?;
    let mut adapter = PostgresInspectAdapter { client };
    inspect_generic(&mut adapter, &schema_name)
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

    let columns = client.query(
        "SELECT c.table_name, c.column_name, c.column_default, c.is_identity, COALESCE(to_jsonb(a)->>'attgenerated',''), typ.typtype::text, ns.nspname, elem.typtype::text, ens.nspname, c.identity_generation FROM information_schema.columns c JOIN pg_namespace n ON n.nspname=c.table_schema JOIN pg_class t ON t.relnamespace=n.oid AND t.relname=c.table_name JOIN pg_attribute a ON a.attrelid=t.oid AND a.attname=c.column_name JOIN pg_type typ ON typ.oid=a.atttypid JOIN pg_namespace ns ON ns.oid=typ.typnamespace LEFT JOIN pg_type elem ON elem.oid=typ.typelem LEFT JOIN pg_namespace ens ON ens.oid=elem.typnamespace WHERE c.table_schema=$1 ORDER BY c.table_name,c.ordinal_position",
        &[&schema_name],
    ).map_err(|err| format!("postgresql column fidelity inspect error: {err}"))?;
    for row in columns {
        let table: String = row.get(0); let column: String = row.get(1);
        let default_value: Option<String> = row.get(2); let identity: String = row.get(3);
        let generated: String = row.get(4); let kind: String = row.get(5); let namespace: String = row.get(6);
        let element_kind: Option<String> = row.get(7); let element_namespace: Option<String> = row.get(8);
        let identity_generation: Option<String> = row.get(9);
        if identity_generation.as_deref() == Some("ALWAYS") { objects.push(format!("unsupported_default:{table}:{column}")); }
        if !generated.is_empty() { objects.push(format!("generated_column:{table}:{column}")); }
        if namespace != "pg_catalog" || matches!(kind.as_str(), "d" | "e" | "c")
            || element_namespace.as_deref().is_some_and(|namespace| namespace != "pg_catalog")
            || element_kind.as_deref().is_some_and(|kind| matches!(kind, "d" | "e" | "c")) {
            objects.push(format!("custom_type:{table}:{column}"));
        }
        if let Some(value) = default_value {
            if identity != "YES" && !value.starts_with("nextval(") && !supported_postgresql_default(&value) {
                objects.push(format!("unsupported_default:{table}:{column}"));
            }
        }
    }
    for row in client.query(
        "SELECT t.relname,a.attname,s.seqstart,s.seqincrement,s.seqmin,s.seqmax,s.seqcycle,pg_catalog.format_type(a.atttypid,a.atttypmod),s.seqrelid IS NOT NULL FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_attribute a ON a.attrelid=t.oid LEFT JOIN pg_attrdef d ON d.adrelid=t.oid AND d.adnum=a.attnum LEFT JOIN pg_sequence s ON s.seqrelid=pg_get_serial_sequence(format('%I.%I',n.nspname,t.relname),a.attname)::regclass WHERE n.nspname=$1 AND a.attnum>0 AND NOT a.attisdropped AND (a.attidentity<>'' OR pg_get_expr(d.adbin,d.adrelid) LIKE 'nextval(%')",
        &[&schema_name],
    ).map_err(|err| format!("postgresql sequence fidelity inspect error: {err}"))? {
        let table: String=row.get(0); let column: String=row.get(1);
        let start: Option<i64>=row.get(2); let increment: Option<i64>=row.get(3);
        let min: Option<i64>=row.get(4); let max: Option<i64>=row.get(5); let cycle: Option<bool>=row.get(6);
        let type_name: String=row.get(7); let owned: bool=row.get(8);
        let expected_max=match type_name.as_str() { "smallint"=>i16::MAX as i64,"integer"=>i32::MAX as i64,_=>i64::MAX };
        if !owned || start != Some(1) || increment != Some(1) || min != Some(1) || max != Some(expected_max) || cycle != Some(false) {
            objects.push(format!("unsupported_default:{table}:{column}"));
        }
    }
    for row in client.query(
        "SELECT t.relname,c.conname FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=$1 AND c.contype='c'",
        &[&schema_name],
    ).map_err(|err| format!("postgresql check inspect error: {err}"))? {
        objects.push(format!("check_constraint:{}:{}", row.get::<_,String>(0), row.get::<_,String>(1)));
    }
    for row in client.query(
        "SELECT t.relname,i.relname FROM pg_index ix JOIN pg_class t ON t.oid=ix.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_class i ON i.oid=ix.indexrelid JOIN pg_am am ON am.oid=i.relam WHERE n.nspname=$1 AND (ix.indexprs IS NOT NULL OR ix.indpred IS NOT NULL OR am.amname<>'btree' OR NOT (0=ALL(ix.indoption)) OR COALESCE((to_jsonb(ix)->>'indnkeyatts')::int,ix.indnatts)<>ix.indnatts)",
        &[&schema_name],
    ).map_err(|err| format!("postgresql advanced index inspect error: {err}"))? {
        objects.push(format!("unsupported_index:{}:{}", row.get::<_,String>(0), row.get::<_,String>(1)));
    }
    for row in client.query(
        "SELECT child.relname,c.conname FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid JOIN pg_namespace n ON n.oid=child.relnamespace JOIN pg_class parent ON parent.oid=c.confrelid WHERE n.nspname=$1 AND c.contype='f' AND parent.relnamespace<>child.relnamespace",
        &[&schema_name],
    ).map_err(|err| format!("postgresql cross-schema FK inspect error: {err}"))? {
        objects.push(format!("cross_schema_fk:{}:{}", row.get::<_,String>(0), row.get::<_,String>(1)));
    }

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
    client.batch_execute("SET DateStyle = 'ISO, YMD'; SET TIME ZONE 'UTC'")
        .map_err(|err| format!("postgresql view metadata session setup error: {err}"))?;
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
        sql = strip_view_namespace(&sql, source_schema, engine);
    }
    sql
}

fn strip_view_namespace(sql: &str, source: &str, engine: &str) -> String {
    let bytes=sql.as_bytes();let mut out=String::with_capacity(sql.len());let mut i=0;
    let mut relation_expected=false;let mut from_clause=false;let mut scopes=Vec::new();
    while i<bytes.len() {
        if let Some(end)=skip_sql_comment(bytes,i,engine=="mysql") {out.push_str(&sql[i..end]);i=end;continue;}
        if bytes[i]==b'\'' {
            let start=i;i+=1;
            while i<bytes.len() {
                if bytes[i]==b'\\' {i=(i+2).min(bytes.len());}
                else if bytes[i]==b'\'' {i+=1;if bytes.get(i)==Some(&b'\'') {i+=1;}else{break;}}
                else{i+=1;}
            }
            out.push_str(&sql[start..i]);continue;
        }
        if engine=="postgresql" && bytes[i]==b'$' {
            let mut end=i+1;
            while end<bytes.len() && (bytes[end].is_ascii_alphanumeric()||bytes[end]==b'_') {end+=1;}
            if bytes.get(end)==Some(&b'$') {
                let delimiter=&sql[i..=end];
                if let Some(close)=sql[end+1..].find(delimiter) {
                    let finish=end+1+close+delimiter.len();out.push_str(&sql[i..finish]);i=finish;continue;
                }
            }
        }
        let start=i;
        let quoted_identifier=matches!(bytes[i],b'"'|b'`');
        let identifier=if bytes[i]==b'"' || bytes[i]==b'`' {
            let quote=bytes[i];i+=1;let mut name=Vec::new();
            while i<bytes.len() {
                if bytes[i]==quote {i+=1;if bytes.get(i)==Some(&quote) {name.push(quote);i+=1;}else{break;}}
                else{name.push(bytes[i]);i+=1;}
            }
            String::from_utf8(name).ok()
        } else if bytes[i].is_ascii_alphabetic() || bytes[i]==b'_' || bytes[i]>=128 {
            i+=1;while i<bytes.len()&&(bytes[i].is_ascii_alphanumeric()||matches!(bytes[i],b'_'|b'$')||bytes[i]>=128){i+=1;}
            let name=&sql[start..i];Some(if engine=="postgresql" {name.to_ascii_lowercase()}else{name.to_string()})
        } else {None};
        if let Some(identifier)=identifier {
            let mut dot=i;while dot<bytes.len()&&bytes[dot].is_ascii_whitespace(){dot+=1;}
            let mut after_object=dot.saturating_add(1);
            while after_object<bytes.len()&&bytes[after_object].is_ascii_whitespace(){after_object+=1;}
            if let Some(quote @ (b'"'|b'`'))=bytes.get(after_object).copied() {
                after_object+=1;
                while after_object<bytes.len() {
                    if bytes[after_object]==quote {after_object+=1;if bytes.get(after_object)==Some(&quote){after_object+=1;}else{break;}}
                    else{after_object+=1;}
                }
            } else {
                while after_object<bytes.len()&&(bytes[after_object].is_ascii_alphanumeric()||matches!(bytes[after_object],b'_'|b'$')||bytes[after_object]>=128){after_object+=1;}
            }
            while after_object<bytes.len()&&bytes[after_object].is_ascii_whitespace(){after_object+=1;}
            let qualified_column=bytes.get(after_object)==Some(&b'.');
            if identifier==source && bytes.get(dot)==Some(&b'.') && (relation_expected||qualified_column) {i=dot+1;}else{out.push_str(&sql[start..i]);}
            relation_expected=false;
            if !quoted_identifier {
                match identifier.to_ascii_lowercase().as_str() {
                    "from"|"join"=>{relation_expected=true;from_clause=true;}
                    "view"=>relation_expected=true,
                    "select"|"where"|"group"|"having"|"order"|"limit"|"union"|"except"|"intersect"|"window"=>from_clause=false,
                    _=>{}
                }
            }
        } else {
            let ch=sql[i..].chars().next().unwrap();out.push(ch);i+=ch.len_utf8();
            match ch {
                '('=>{scopes.push(from_clause);from_clause=false;relation_expected=false;}
                ')'=>{from_clause=scopes.pop().unwrap_or(false);relation_expected=false;}
                ','=>relation_expected=from_clause,
                _=>{}
            }
        }
    }
    out
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

/// A dump view may only replace its declared name in the connected namespace.
/// Namespace-qualified targets must have been removed by source sanitization;
/// accepting any remaining qualifier would let a candidate modify the live DB.
pub(crate) fn validate_import_view_target(sql: &str, expected: &str, engine: &str) -> Result<(), String> {
    let invalid=|reason:&str|format!("view_target_invalid: {expected}: {reason}");
    validate_single_view_statement(sql).map_err(|error|invalid(&error))?;
    let bytes=sql.as_bytes();let mut i=0;
    let trivia=|mut position:usize|->Result<usize,String> {
        loop {
            while bytes.get(position).is_some_and(u8::is_ascii_whitespace) {position+=1;}
            if bytes.get(position..).is_some_and(|tail|tail.starts_with(b"/*!")||tail.starts_with(b"/*M!")||tail.starts_with(b"/*m!")) {
                return Err(invalid("executable comments are not allowed in the view target"));
            }
            match skip_sql_comment(bytes,position,engine=="mysql") {Some(end)=>position=end,None=>return Ok(position)}
        }
    };
    let quoted=|start:usize|->Result<(String,usize),String> {
        let delimiter=bytes[start];let mut position=start+1;let mut name=Vec::new();
        while position<bytes.len() {
            let byte=bytes[position];position+=1;
            if byte==delimiter {
                if bytes.get(position)==Some(&delimiter) {name.push(delimiter);position+=1;}
                else {return String::from_utf8(name).map(|name|(name,position)).map_err(|_|invalid("invalid identifier encoding"));}
            } else {name.push(byte);}
        }
        Err(invalid("unterminated quoted identifier"))
    };
    loop {
        i=trivia(i)?;
        if i>=bytes.len() {return Err(invalid("VIEW keyword missing"));}
        if matches!(bytes[i],b'\''|b'"'|b'`') {i=quoted(i)?.1;continue;}
        if bytes[i].is_ascii_alphabetic()||bytes[i]==b'_' {
            let start=i;i+=1;
            while bytes.get(i).is_some_and(|byte|byte.is_ascii_alphanumeric()||*byte==b'_') {i+=1;}
            if sql[start..i].eq_ignore_ascii_case("view") {break;}
        } else {i+=sql[i..].chars().next().unwrap().len_utf8();}
    }
    i=trivia(i)?;
    let (name,end)=match bytes.get(i) {
        Some(b'"'|b'`')=>quoted(i)?,
        Some(_)=>{
            let start=i;
            while bytes.get(i).is_some_and(|byte|byte.is_ascii_alphanumeric()||matches!(*byte,b'_'|b'$')||*byte>=128) {i+=1;}
            if i==start {return Err(invalid("view name missing"));}
            let name=&sql[start..i];
            (if engine=="postgresql" {name.to_ascii_lowercase()}else{name.to_string()},i)
        }
        None=>return Err(invalid("view name missing")),
    };
    let after=trivia(end)?;
    if bytes.get(after)==Some(&b'.') {return Err(invalid("namespace-qualified view targets are forbidden"));}
    if name!=expected {return Err(invalid("CREATE VIEW target does not match its manifest name"));}
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
    fn sanitize_view_namespace_preserves_literals_and_handles_unquoted_pg_names() {
        let sql = "CREATE VIEW v AS SELECT 'app.items' AS literal, $$app.items$$ AS body FROM app.items -- app.items\n";
        let out = sanitize_view_definition(sql, "app", "postgresql");
        assert!(out.contains("FROM items"));
        assert!(out.contains("'app.items'"));
        assert!(out.contains("$$app.items$$"));
        assert!(out.contains("-- app.items"));
        let mysql = sanitize_view_definition("CREATE VIEW v AS SELECT '`app`.items' AS literal FROM `app`.`items`", "app", "mysql");
        assert!(mysql.contains("'`app`.items'"));
        assert!(mysql.contains("FROM `items`"));
        let correlated=sanitize_view_definition("CREATE VIEW v AS SELECT (SELECT app.value FROM aux LIMIT 1) FROM app.items AS app", "app", "postgresql");
        assert!(correlated.contains("SELECT app.value FROM aux"));
        assert!(correlated.contains("FROM items AS app"));
    }

    #[test]
    fn import_view_target_accepts_quoted_dots_but_rejects_qualified_targets() {
        assert!(validate_import_view_target("CREATE VIEW \"a.b\" AS SELECT 1", "a.b", "postgresql").is_ok());
        assert!(validate_import_view_target("CREATE VIEW `a``b.c` AS SELECT 1", "a`b.c", "mysql").is_ok());
        assert!(validate_import_view_target("CREATE VIEW v (id) AS SELECT 1", "v", "postgresql").is_ok());
        for sql in ["CREATE VIEW other.v AS SELECT 1", "CREATE OR REPLACE VIEW \"other\" . /* spacing */ \"v\" AS SELECT 1", "CREATE VIEW v /*! .outside */ AS SELECT 1", "CREATE VIEW another AS SELECT 1"] {
            assert!(validate_import_view_target(sql,"v","mysql").is_err(),"accepted {sql}");
        }
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
