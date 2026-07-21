use crate::*;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

use mysql::prelude::Queryable;
use postgres::{error::SqlState, NoTls};

pub(crate) const MYSQL_INSERT_FALLBACK_BATCH_ROWS: usize = 500;
pub(crate) const MYSQL_INSERT_FALLBACK_BATCH_BYTES: usize = 4 * 1024 * 1024;
pub(crate) const MYSQL_DUMP_TARGET_BYTES_PER_CHUNK: u64 = 64_000_000;
/// 순차 MySQL 덤프에서 단일 result set(청크)당 절대 행수 상한.
///
/// InnoDB의 `AVG_ROW_LENGTH`는 off-page(overflow) 저장되는 대형 TEXT/JSON/BLOB의
/// 실제 바이트를 과소계상한다(main page 위주 집계). 바이트 목표만 믿고 청크 크기를
/// 정하면 이런 wide 테이블에서 한 result set가 과대해져 MySQL 스트리밍 프로토콜
/// 코덱이 크래시(`CodecError: bytes remaining on stream`)할 수 있다. 이 상한이
/// avg가 0/과소계상이어도 청크 행 수를 물리적으로 묶어 크래시를 원천 차단한다.
pub(crate) const MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK: usize = 2_000;
pub(crate) const MYSQL_PK_RANGE_MAX_SPAN_TO_ROW_RATIO: u128 = 8;
pub(crate) const MYSQL_DUMP_ZSTD_LEVEL: i32 = 1;
pub(crate) const DUMP_DIR_MARKER: &str = ".tunnelforge_dump_dir";

#[derive(Debug, Deserialize)]
pub struct Request {
    pub command: String,
    #[serde(default)]
    pub request_id: Option<String>,
    #[serde(default)]
    pub payload: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MigrationIssue {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub issue_type: Option<String>,
    pub severity: String,
    pub location: String,
    pub message: String,
    pub suggestion: String,
    pub blocking: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub table_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub column_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResumeTableState {
    pub table: String,
    pub completed: bool,
    pub last_key: Option<String>,
    pub rows_copied: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResumeState {
    pub direction: String,
    pub current_phase: String,
    pub tables: Vec<ResumeTableState>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedSchema {
    #[serde(default)]
    pub tables: Vec<NormalizedTable>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedTable {
    pub name: String,
    #[serde(default)]
    pub columns: Vec<NormalizedColumn>,
    #[serde(default)]
    pub indexes: Vec<NormalizedIndex>,
    #[serde(default)]
    pub foreign_keys: Vec<NormalizedForeignKey>,
    /// 테이블 기본 collation(information_schema.tables.TABLE_COLLATION).
    /// 같은 엔진(MySQL→MySQL) dump/import에서 테이블 레벨 DEFAULT COLLATE를 재현하기 위해 보존한다.
    /// PostgreSQL 소스나 cross-engine에서는 None(PostgreSQL은 테이블 레벨 collation 개념이 없음).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub table_collation: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedColumn {
    pub name: String,
    #[serde(rename = "type", alias = "data_type", default)]
    pub type_name: String,
    #[serde(rename = "default", default)]
    pub default_value: Option<String>,
    #[serde(default = "default_nullable")]
    pub nullable: bool,
    #[serde(default)]
    pub primary_key: bool,
    #[serde(default)]
    pub unique: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedIndex {
    pub name: String,
    #[serde(default)]
    pub columns: Vec<String>,
    #[serde(default)]
    pub unique: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedForeignKey {
    pub name: String,
    #[serde(default)]
    pub columns: Vec<String>,
    pub referenced_table: String,
    #[serde(default)]
    pub referenced_columns: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Endpoint {
    pub engine: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
    pub database: String,
    #[serde(default)]
    pub schema: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MigrationOptions {
    #[serde(default = "default_mode")]
    pub mode: String,
    #[serde(default = "default_chunk_size")]
    pub chunk_size: usize,
    #[serde(default)]
    pub cancel_after_chunks: Option<usize>,
    #[serde(default)]
    pub cleanup_before_migrate: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MigrationResult {
    pub success: bool,
    pub rows_copied: u64,
    pub chunks_copied: usize,
    pub state: ResumeState,
    pub issues: Vec<MigrationIssue>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DumpManifest {
    pub format: String,
    pub format_version: u32,
    #[serde(default = "default_dump_data_format")]
    pub data_format: String,
    #[serde(default = "default_dump_compression")]
    pub compression: String,
    pub source_engine: String,
    pub database: String,
    pub schema: NormalizedSchema,
    #[serde(default = "default_snapshot_policy")]
    pub snapshot_policy: String,
    #[serde(default)]
    pub strict_export: bool,
    #[serde(default)]
    pub manifest_warnings: Vec<String>,
    pub chunk_size: usize,
    pub created_unix_seconds: u64,
    pub tables: Vec<DumpTableManifest>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub views: Vec<NormalizedView>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedView {
    pub name: String,
    pub definition: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DumpTableManifest {
    pub name: String,
    pub path: String,
    pub rows: u64,
    pub chunks: u64,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub chunk_sha256: BTreeMap<String, String>,
}

pub(crate) enum DumpTableEvent {
    Progress(Value),
    Done {
        index: usize,
        manifest: DumpTableManifest,
        rows: u64,
        chunks: u64,
    },
    Error(String),
}

#[derive(Debug, Clone)]
pub(crate) struct DumpRange {
    pub(crate) chunk_index: u64,
    pub(crate) start: i128,
    pub(crate) end: i128,
}

pub(crate) enum DumpRangeEvent {
    Done {
        chunk_index: u64,
        rows: u64,
        stream_ms: u64,
        range_start: String,
        range_end: String,
        checksum: String,
    },
    Error(String),
}

pub(crate) enum DumpGlobalEvent {
    Progress(Value),
    RangeDone {
        table_index: usize,
        chunk_index: u64,
        rows: u64,
        stream_ms: u64,
        range_start: String,
        range_end: String,
        checksum: String,
    },
    TableDone {
        index: usize,
        manifest: DumpTableManifest,
        rows: u64,
        chunks: u64,
        duration_ms: u64,
    },
    Error(String),
}

#[derive(Debug, Clone)]
pub(crate) enum DumpGlobalWorkKind {
    MysqlRange {
        table_path: String,
        pk_column: String,
        range: DumpRange,
    },
    WholeTable,
}

#[derive(Debug, Clone)]
pub(crate) struct DumpGlobalWorkItem {
    pub(crate) table_index: usize,
    pub(crate) table: NormalizedTable,
    pub(crate) kind: DumpGlobalWorkKind,
}

pub(crate) struct DumpGlobalTableState {
    pub(crate) table_path: String,
    pub(crate) rows_total: u64,
    pub(crate) rows_dumped: u64,
    pub(crate) chunks_total: u64,
    pub(crate) chunks_done: u64,
    pub(crate) avg_row_bytes: u64,
    pub(crate) work_ms: u64,
    pub(crate) chunk_sha256: BTreeMap<String, String>,
    pub(crate) manifest: Option<DumpTableManifest>,
}

pub(crate) enum ImportChunkEvent {
    Done {
        chunk_index: u64,
        rows: u64,
        load_ms: u64,
    },
    Error(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct InspectionResult {
    pub schema: NormalizedSchema,
    pub unsupported_objects: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct MemoryAdapter {
    pub rows: BTreeMap<String, Vec<Value>>,
    pub created_tables: Vec<String>,
}

pub trait MigrationAdapter {
    fn row_count(&mut self, table: &str) -> Result<usize, String>;
    fn create_table(&mut self, table: &NormalizedTable, ddl: &str) -> Result<(), String>;
    fn read_rows(
        &mut self,
        table: &NormalizedTable,
        offset: usize,
        limit: usize,
    ) -> Result<Vec<Value>, String>;
    fn read_rows_after_key(
        &mut self,
        table: &NormalizedTable,
        key_columns: &[String],
        _last_key: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        if key_columns.is_empty() {
            return self.read_rows(table, 0, limit);
        }
        Err("keyset reads require adapter support".to_string())
    }
    fn insert_rows(&mut self, table: &NormalizedTable, rows: Vec<Value>) -> Result<(), String>;
    fn execute_sql(&mut self, sql: &str) -> Result<(), String>;
}

impl MemoryAdapter {
    pub fn from_value(value: Option<&Value>) -> Self {
        let mut rows = BTreeMap::new();
        if let Some(Value::Object(tables)) = value {
            for (table, table_rows) in tables {
                let parsed_rows = table_rows.as_array().cloned().unwrap_or_default();
                rows.insert(table.clone(), parsed_rows);
            }
        }
        Self {
            rows,
            created_tables: Vec::new(),
        }
    }

    pub fn row_count(&self, table: &str) -> usize {
        self.rows.get(table).map(Vec::len).unwrap_or(0)
    }

    pub fn create_table(&mut self, table: &str) {
        self.rows.entry(table.to_string()).or_default();
        if !self.created_tables.iter().any(|item| item == table) {
            self.created_tables.push(table.to_string());
        }
    }

    pub fn read_rows(&self, table: &str, offset: usize, limit: usize) -> Vec<Value> {
        self.rows
            .get(table)
            .map(|rows| rows.iter().skip(offset).take(limit).cloned().collect())
            .unwrap_or_default()
    }

    pub fn insert_rows(&mut self, table: &str, rows: Vec<Value>) {
        self.rows.entry(table.to_string()).or_default().extend(rows);
    }
}

impl MigrationAdapter for MemoryAdapter {
    fn row_count(&mut self, table: &str) -> Result<usize, String> {
        Ok(MemoryAdapter::row_count(self, table))
    }

    fn create_table(&mut self, table: &NormalizedTable, _ddl: &str) -> Result<(), String> {
        self.create_table(&table.name);
        Ok(())
    }

    fn read_rows(
        &mut self,
        table: &NormalizedTable,
        offset: usize,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        Ok(MemoryAdapter::read_rows(self, &table.name, offset, limit))
    }

    fn read_rows_after_key(
        &mut self,
        table: &NormalizedTable,
        key_columns: &[String],
        last_key: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        if key_columns.is_empty() {
            return self.read_rows(
                table,
                last_key
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(0),
                limit,
            );
        }
        let rows = self.rows.get(&table.name).cloned().unwrap_or_default();
        let start = keyset_start_index(&rows, key_columns, last_key);
        Ok(rows.into_iter().skip(start).take(limit).collect())
    }

    fn insert_rows(&mut self, table: &NormalizedTable, rows: Vec<Value>) -> Result<(), String> {
        self.insert_rows(&table.name, rows);
        Ok(())
    }

    fn execute_sql(&mut self, _sql: &str) -> Result<(), String> {
        Ok(())
    }
}

pub enum LiveAdapter {
    MySql(mysql::PooledConn),
    PostgreSql(postgres::Client),
}

impl LiveAdapter {
    pub fn connect(endpoint: &Endpoint) -> Result<Self, String> {
        match endpoint.engine.as_str() {
            "mysql" => {
                let opts = mysql_opts(endpoint);
                let pool =
                    mysql::Pool::new(opts).map_err(|err| format!("mysql pool error: {err}"))?;
                let conn = pool
                    .get_conn()
                    .map_err(|err| format!("mysql connection error: {err}"))?;
                Ok(Self::MySql(conn))
            }
            "postgresql" => {
                let mut client = postgres_config(endpoint)
                    .connect(NoTls)
                    .map_err(|err| format!("postgresql connection error: {err}"))?;
                let schema = endpoint_schema(endpoint);
                client
                    .batch_execute(&format!(
                        "SET search_path TO {}",
                        quote_ident("postgresql", &schema)
                    ))
                    .map_err(|err| format!("postgresql schema selection error: {err}"))?;
                Ok(Self::PostgreSql(client))
            }
            other => Err(format!("unsupported endpoint engine: {other}")),
        }
    }

    pub fn engine(&self) -> &'static str {
        match self {
            Self::MySql(_) => "mysql",
            Self::PostgreSql(_) => "postgresql",
        }
    }
}

impl MigrationAdapter for LiveAdapter {
    fn row_count(&mut self, table: &str) -> Result<usize, String> {
        match self {
            Self::MySql(conn) => conn
                .query_first::<u64, _>(count_sql("mysql", table))
                .map(|count| count.unwrap_or(0) as usize)
                .or_else(|err| {
                    if looks_like_missing_table(&err.to_string()) {
                        Ok(0)
                    } else {
                        Err(format!("mysql count error: {err}"))
                    }
                }),
            Self::PostgreSql(client) => client
                .query_one(&count_sql("postgresql", table), &[])
                .map(|row| {
                    let count: i64 = row.get(0);
                    count as usize
                })
                .or_else(|err| {
                    if err.code() == Some(&SqlState::UNDEFINED_TABLE)
                        || looks_like_missing_table(&err.to_string())
                    {
                        Ok(0)
                    } else {
                        Err(format_postgres_error("postgresql count error", &err))
                    }
                }),
        }
    }

    fn create_table(&mut self, _table: &NormalizedTable, ddl: &str) -> Result<(), String> {
        if ddl.trim().is_empty() {
            return Ok(());
        }
        match self {
            Self::MySql(conn) => conn.query_drop(ddl).or_else(|err| {
                if looks_like_existing_table(&err.to_string()) {
                    Ok(())
                } else {
                    Err(format!("mysql create table error: {err}"))
                }
            }),
            Self::PostgreSql(client) => client.batch_execute(ddl).or_else(|err| {
                if err.code() == Some(&SqlState::DUPLICATE_TABLE)
                    || looks_like_existing_table(&err.to_string())
                {
                    Ok(())
                } else {
                    Err(format_postgres_error("postgresql create table error", &err))
                }
            }),
        }
    }

    fn read_rows(
        &mut self,
        table: &NormalizedTable,
        offset: usize,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        let columns = column_names(table);
        let key_columns = key_columns(table);
        match self {
            Self::MySql(conn) => {
                let sql = select_chunk_text_sql("mysql", table, &key_columns);
                let rows: Vec<mysql::Row> = conn
                    .exec(sql, (limit as u64, offset as u64))
                    .map_err(|err| format!("mysql select chunk error: {err}"))?;
                Ok(rows
                    .into_iter()
                    .map(|row| mysql_row_to_json(&columns, row))
                    .collect())
            }
            Self::PostgreSql(client) => {
                let sql = select_chunk_text_sql("postgresql", table, &key_columns);
                let rows = client
                    .query(&sql, &[&(limit as i64), &(offset as i64)])
                    .map_err(|err| format_postgres_error("postgresql select chunk error", &err))?;
                Ok(rows
                    .into_iter()
                    .map(|row| postgres_row_to_json(&columns, &row))
                    .collect())
            }
        }
    }

    fn read_rows_after_key(
        &mut self,
        table: &NormalizedTable,
        key_columns: &[String],
        last_key: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        if key_columns.is_empty() {
            return self.read_rows(
                table,
                last_key
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(0),
                limit,
            );
        }
        match self {
            Self::MySql(conn) => {
                let columns = column_names(table);
                let last_values = last_key.and_then(decode_key_token);
                let sql = select_chunk_text_after_key_sql(
                    "mysql",
                    table,
                    key_columns,
                    last_values.as_deref(),
                    limit,
                );
                let rows: Vec<mysql::Row> = conn
                    .query(sql)
                    .map_err(|err| format!("mysql keyset select chunk error: {err}"))?;
                Ok(rows
                    .into_iter()
                    .map(|row| mysql_row_to_json(&columns, row))
                    .collect())
            }
            Self::PostgreSql(client) => {
                let columns = column_names(table);
                let last_values = last_key.and_then(decode_key_token);
                let sql = select_chunk_text_after_key_sql(
                    "postgresql",
                    table,
                    key_columns,
                    last_values.as_deref(),
                    limit,
                );
                let rows = client.query(&sql, &[]).map_err(|err| {
                    format_postgres_error("postgresql keyset select chunk error", &err)
                })?;
                Ok(rows
                    .into_iter()
                    .map(|row| postgres_row_to_json(&columns, &row))
                    .collect())
            }
        }
    }

    fn insert_rows(&mut self, table: &NormalizedTable, rows: Vec<Value>) -> Result<(), String> {
        if rows.is_empty() {
            return Ok(());
        }
        match self {
            Self::PostgreSql(client) => copy_rows_to_postgres(client, table, &rows),
            Self::MySql(conn) => conn
                .query_drop(insert_rows_literal_sql_for_table("mysql", table, &rows))
                .map_err(|err| format!("mysql insert error: {err}")),
        }
    }

    fn execute_sql(&mut self, sql: &str) -> Result<(), String> {
        if sql.trim().is_empty() {
            return Ok(());
        }
        match self {
            Self::MySql(conn) => conn
                .query_drop(sql)
                .map_err(|err| format!("mysql SQL execution error: {err}")),
            Self::PostgreSql(client) => client
                .batch_execute(sql)
                .map_err(|err| format_postgres_error("postgresql SQL execution error", &err)),
        }
    }
}

pub(crate) fn mysql_opts(endpoint: &Endpoint) -> mysql::OptsBuilder {
    mysql::OptsBuilder::new()
        .ip_or_hostname(Some(endpoint.host.clone()))
        .tcp_port(endpoint.port)
        .user(Some(endpoint.user.clone()))
        .pass(Some(endpoint.password.clone()))
        .db_name(Some(endpoint.database.clone()))
        // TCP keepalive: 유휴 소켓에 주기적 하트비트를 흘려 SSH 터널/방화벽/LB의
        // idle-timeout으로 연결이 끊기는 것을 막는다(대량 import 중 pooled 연결이
        // 쿼리 없이 대기하는 구간 방어). 밀리초 단위, Windows 포함 전 플랫폼 지원.
        // 주의: tcp_keepalive_probe_* / tcp_user_timeout_ms는 Linux 전용 cfg라
        // Windows 빌드가 깨지므로 사용하지 않는다.
        .tcp_keepalive_time_ms(Some(10_000))
        // 재접속/최초 접속이 무한 대기하지 않도록 상한(재시도 루프에서 특히 중요).
        .tcp_connect_timeout(Some(std::time::Duration::from_secs(30)))
}

pub(crate) fn postgres_config(endpoint: &Endpoint) -> postgres::Config {
    let mut config = postgres::Config::new();
    config
        .host(&endpoint.host)
        .port(endpoint.port)
        .user(&endpoint.user)
        .password(&endpoint.password)
        .dbname(&endpoint.database);
    config
}

pub(crate) fn endpoint_schema(endpoint: &Endpoint) -> String {
    endpoint
        .schema
        .as_deref()
        .map(str::trim)
        .filter(|schema| !schema.is_empty())
        .map(ToString::to_string)
        .unwrap_or_else(|| {
            if endpoint.engine == "postgresql" {
                "public".to_string()
            } else {
                endpoint.database.clone()
            }
        })
}

pub(crate) fn prepare_target_schema(target: &mut LiveAdapter, endpoint: &Endpoint) -> Result<(), String> {
    if endpoint.engine != "postgresql" {
        return Ok(());
    }
    let schema = endpoint_schema(endpoint);
    target.execute_sql(&format!(
        "CREATE SCHEMA IF NOT EXISTS {}; SET search_path TO {};",
        quote_ident("postgresql", &schema),
        quote_ident("postgresql", &schema)
    ))
}

fn looks_like_missing_table(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("doesn't exist")
        || lower.contains("does not exist")
        || lower.contains("undefined table")
        || lower.contains("no such table")
}

fn looks_like_existing_table(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("already exists") || lower.contains("table exists") || lower.contains("1050")
}

fn default_nullable() -> bool {
    true
}

pub(crate) fn default_mode() -> String {
    "create_only".to_string()
}

pub(crate) fn default_chunk_size() -> usize {
    10_000
}

fn default_dump_data_format() -> String {
    "jsonl".to_string()
}

fn default_dump_compression() -> String {
    "none".to_string()
}

fn default_snapshot_policy() -> String {
    "unknown".to_string()
}

pub(crate) fn dump_manifest_consistency_metadata(
    engine: &str,
    _threads: usize,
) -> (String, bool, Vec<String>) {
    if engine == "mysql" {
        (
            "mysql_shared_consistent_snapshot".to_string(),
            true,
            Vec::new(),
        )
    } else {
        ("connection_consistent".to_string(), true, Vec::new())
    }
}


#[cfg(test)]
pub(crate) mod test_support {
    use super::*;
    
    use serde_json::Value;
    use std::collections::BTreeMap;

    pub(crate) fn schema() -> NormalizedSchema {
        NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "users".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int(11)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "name".to_string(),
                        type_name: "varchar(255)".to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        }
    }

    pub(crate) fn empty_table(name: &str, foreign_keys: Vec<NormalizedForeignKey>) -> NormalizedTable {
        NormalizedTable {
            name: name.to_string(),
            columns: Vec::new(),
            indexes: Vec::new(),
            foreign_keys,
            table_collation: None,
        }
    }

    pub(crate) fn fk(name: &str, referenced_table: &str) -> NormalizedForeignKey {
        NormalizedForeignKey {
            name: name.to_string(),
            columns: vec!["parent_id".to_string()],
            referenced_table: referenced_table.to_string(),
            referenced_columns: vec!["id".to_string()],
        }
    }

    #[derive(Default)]
    pub(crate) struct RecordingAdapter {
        pub(crate) executed_sql: Vec<String>,
        pub(crate) row_counts: BTreeMap<String, usize>,
        pub(crate) fail_sql_contains: Option<String>,
    }

    impl MigrationAdapter for RecordingAdapter {
        fn row_count(&mut self, table: &str) -> Result<usize, String> {
            Ok(self.row_counts.get(table).copied().unwrap_or(0))
        }

        fn create_table(&mut self, _table: &NormalizedTable, _ddl: &str) -> Result<(), String> {
            Ok(())
        }

        fn read_rows(
            &mut self,
            _table: &NormalizedTable,
            _offset: usize,
            _limit: usize,
        ) -> Result<Vec<Value>, String> {
            Ok(Vec::new())
        }

        fn insert_rows(
            &mut self,
            _table: &NormalizedTable,
            _rows: Vec<Value>,
        ) -> Result<(), String> {
            Ok(())
        }

        fn execute_sql(&mut self, sql: &str) -> Result<(), String> {
            if self
                .fail_sql_contains
                .as_ref()
                .is_some_and(|needle| sql.contains(needle))
            {
                return Err("mysql SQL execution error: ERROR 1114 (HY000): The table '#sql-1cbc_17b' is full".to_string());
            }
            self.executed_sql.push(sql.to_string());
            Ok(())
        }
    }

    #[derive(Default)]
    pub(crate) struct TrackingAdapter {
        pub(crate) rows: Vec<Value>,
        pub(crate) read_limits: Vec<usize>,
        pub(crate) read_after_limits: Vec<usize>,
        pub(crate) max_returned: usize,
    }

    impl MigrationAdapter for TrackingAdapter {
        fn row_count(&mut self, _table: &str) -> Result<usize, String> {
            Ok(self.rows.len())
        }

        fn create_table(&mut self, _table: &NormalizedTable, _ddl: &str) -> Result<(), String> {
            Ok(())
        }

        fn read_rows(
            &mut self,
            _table: &NormalizedTable,
            offset: usize,
            limit: usize,
        ) -> Result<Vec<Value>, String> {
            self.read_limits.push(limit);
            let chunk: Vec<Value> = self.rows.iter().skip(offset).take(limit).cloned().collect();
            self.max_returned = self.max_returned.max(chunk.len());
            Ok(chunk)
        }

        fn read_rows_after_key(
            &mut self,
            _table: &NormalizedTable,
            key_columns: &[String],
            last_key: Option<&str>,
            limit: usize,
        ) -> Result<Vec<Value>, String> {
            self.read_after_limits.push(limit);
            let start = keyset_start_index(&self.rows, key_columns, last_key);
            let chunk: Vec<Value> = self.rows.iter().skip(start).take(limit).cloned().collect();
            self.max_returned = self.max_returned.max(chunk.len());
            Ok(chunk)
        }

        fn insert_rows(
            &mut self,
            _table: &NormalizedTable,
            _rows: Vec<Value>,
        ) -> Result<(), String> {
            Ok(())
        }

        fn execute_sql(&mut self, _sql: &str) -> Result<(), String> {
            Ok(())
        }
    }

    pub(crate) fn single_pk_table_with_collation(table_collation: Option<&str>) -> NormalizedTable {
        NormalizedTable {
            name: "docs".to_string(),
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
            table_collation: table_collation.map(str::to_string),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    
    
    
    
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::single_pk_table_with_collation;

    #[test]
    fn endpoint_schema_uses_mysql_database_when_schema_is_empty() {
        let endpoint = Endpoint {
            engine: "mysql".to_string(),
            host: "127.0.0.1".to_string(),
            port: 3306,
            user: "root".to_string(),
            password: String::new(),
            database: "dataflare".to_string(),
            schema: Some(String::new()),
        };

        assert_eq!(endpoint_schema(&endpoint), "dataflare");
    }

    #[test]
    fn sequential_chunk_size_caps_rows_when_avg_underreports() {
        // avg=0 (통계 없음/과소계상) → byte_target=fallback(50000)이지만 hard cap이 지배.
        assert_eq!(
            sequential_mysql_chunk_size(50_000, 0),
            MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK
        );
        // avg가 작아(128) byte_target이 커도 hard cap으로 묶인다.
        assert_eq!(
            sequential_mysql_chunk_size(50_000, 128),
            MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK
        );
        // avg가 중간(9462)이라 byte_target=6764여도 hard cap(2000)이 더 작아 지배.
        assert_eq!(
            sequential_mysql_chunk_size(50_000, 9_462),
            MYSQL_DUMP_SEQUENTIAL_MAX_ROWS_PER_CHUNK
        );
    }

    #[test]
    fn zstd_dump_level_uses_fast_default() {
        assert_eq!(MYSQL_DUMP_ZSTD_LEVEL, 1);
    }

    #[test]
    fn normalized_table_deserializes_legacy_manifest_without_table_collation() {
        // 구버전 매니페스트(table_collation 필드 없음)도 역직렬화되며 None이 되어야 한다(하위호환).
        let json = r#"{"name":"legacy","columns":[],"indexes":[],"foreign_keys":[]}"#;
        let table: NormalizedTable =
            serde_json::from_str(json).expect("legacy manifest must deserialize");
        assert_eq!(table.table_collation, None);
    }

    #[test]
    fn normalized_table_skips_serializing_none_table_collation() {
        // None이면 직렬화 산출물에 table_collation 키가 빠져야 한다(skip_serializing_if로 매니페스트 노이즈 방지).
        let json = serde_json::to_string(&single_pk_table_with_collation(None)).expect("serialize");
        assert!(!json.contains("table_collation"), "{json}");
        let json_with = serde_json::to_string(&single_pk_table_with_collation(Some("utf8mb4_bin")))
            .expect("serialize");
        assert!(json_with.contains("utf8mb4_bin"), "{json_with}");
    }

    #[test]
    fn missing_table_detection_accepts_mysql_and_postgres_messages() {
        assert!(looks_like_missing_table("Table 'app.users' doesn't exist"));
        assert!(looks_like_missing_table(
            "ERROR: relation \"users\" does not exist"
        ));
        assert!(!looks_like_missing_table(
            "permission denied for table users"
        ));
    }

    #[test]
    fn existing_table_detection_accepts_mysql_and_postgres_messages() {
        assert!(looks_like_existing_table(
            "ERROR 1050 (42S01): Table 'users' already exists"
        ));
        assert!(looks_like_existing_table(
            "ERROR: relation \"users\" already exists"
        ));
        assert!(!looks_like_existing_table(
            "permission denied for table users"
        ));
    }

    #[test]
    fn dump_manifest_without_views_field_deserializes_to_empty() {
        // 기존(v1/v2) dump에는 views 필드가 없다 — serde(default)로 빈 Vec이 되어야 한다.
        let json = r#"{
            "format": "tunnelforge-dump",
            "format_version": 2,
            "data_format": "tsv",
            "compression": "zstd",
            "source_engine": "mysql",
            "database": "app",
            "schema": {"tables": []},
            "chunk_size": 50000,
            "created_unix_seconds": 1,
            "tables": []
        }"#;
        let manifest: DumpManifest = serde_json::from_str(json).expect("parse legacy manifest");
        assert!(manifest.views.is_empty());
    }

    #[test]
    fn dump_manifest_strictness_fields_default_for_legacy_json() {
        let json = r#"{
            "format": "tunnelforge-dump",
            "format_version": 2,
            "data_format": "tsv",
            "compression": "zstd",
            "source_engine": "mysql",
            "database": "app",
            "schema": {"tables": []},
            "chunk_size": 50000,
            "created_unix_seconds": 1,
            "tables": []
        }"#;

        let manifest: DumpManifest = serde_json::from_str(json).expect("parse legacy manifest");

        assert_eq!(manifest.snapshot_policy, "unknown");
        assert!(!manifest.strict_export);
        assert!(manifest.manifest_warnings.is_empty());
    }

    #[test]
    fn dump_manifest_consistency_metadata_marks_mysql_parallel_as_shared_snapshot() {
        let (snapshot_policy, strict_export, warnings) =
            dump_manifest_consistency_metadata("mysql", 8);

        assert_eq!(snapshot_policy, "mysql_shared_consistent_snapshot");
        assert!(strict_export);
        assert!(warnings.is_empty());
    }

    #[test]
    fn dump_manifest_consistency_metadata_marks_single_thread_as_strict() {
        let (snapshot_policy, strict_export, warnings) =
            dump_manifest_consistency_metadata("mysql", 1);

        assert_eq!(snapshot_policy, "mysql_shared_consistent_snapshot");
        assert!(strict_export);
        assert!(warnings.is_empty());
    }

    #[test]
    fn dump_manifest_consistency_metadata_keeps_postgres_policy_separate() {
        let (snapshot_policy, strict_export, warnings) =
            dump_manifest_consistency_metadata("postgresql", 8);

        assert_eq!(snapshot_policy, "connection_consistent");
        assert!(strict_export);
        assert!(warnings.is_empty());
    }

    #[test]
    fn dump_manifest_with_views_round_trips() {
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "zstd".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: NormalizedSchema::default(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 50000,
            created_unix_seconds: 1,
            tables: Vec::new(),
            views: vec![NormalizedView {
                name: "ref_vendor_codes_view".to_string(),
                definition: "CREATE VIEW `ref_vendor_codes_view` AS select 1".to_string(),
            }],
        };
        let json = serde_json::to_string(&manifest).expect("serialize");
        assert!(json.contains("ref_vendor_codes_view"));
        let parsed: DumpManifest = serde_json::from_str(&json).expect("parse");
        assert_eq!(parsed.views, manifest.views);
    }

    #[test]
    fn empty_views_are_skipped_in_serialization() {
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "zstd".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: NormalizedSchema::default(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 50000,
            created_unix_seconds: 1,
            tables: Vec::new(),
            views: Vec::new(),
        };
        let json = serde_json::to_string(&manifest).expect("serialize");
        // skip_serializing_if 로 빈 views는 직렬화되지 않아 기존 dump와 바이트 호환.
        assert!(!json.contains("\"views\""));
    }
}
