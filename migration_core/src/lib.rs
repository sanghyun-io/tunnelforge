use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use mysql::{prelude::Queryable, LocalInfileHandler};
use postgres::{error::SqlState, NoTls};

const MYSQL_INSERT_FALLBACK_BATCH_ROWS: usize = 500;
const MYSQL_INSERT_FALLBACK_BATCH_BYTES: usize = 4 * 1024 * 1024;
const MYSQL_DUMP_TARGET_BYTES_PER_CHUNK: u64 = 64_000_000;
const MYSQL_PK_RANGE_MAX_SPAN_TO_ROW_RATIO: u128 = 8;
const MYSQL_DUMP_ZSTD_LEVEL: i32 = 1;
const DUMP_DIR_MARKER: &str = ".tunnelforge_dump_dir";

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
    pub severity: String,
    pub location: String,
    pub message: String,
    pub suggestion: String,
    pub blocking: bool,
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

enum DumpTableEvent {
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
struct DumpRange {
    chunk_index: u64,
    start: i128,
    end: i128,
}

enum DumpRangeEvent {
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

enum DumpGlobalEvent {
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
enum DumpGlobalWorkKind {
    MysqlRange {
        table_path: String,
        pk_column: String,
        range: DumpRange,
    },
    WholeTable,
}

#[derive(Debug, Clone)]
struct DumpGlobalWorkItem {
    table_index: usize,
    table: NormalizedTable,
    kind: DumpGlobalWorkKind,
}

struct DumpGlobalTableState {
    table_path: String,
    rows_total: u64,
    rows_dumped: u64,
    chunks_total: u64,
    chunks_done: u64,
    avg_row_bytes: u64,
    work_ms: u64,
    chunk_sha256: BTreeMap<String, String>,
    manifest: Option<DumpTableManifest>,
}

enum ImportChunkEvent {
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

fn mysql_opts(endpoint: &Endpoint) -> mysql::OptsBuilder {
    mysql::OptsBuilder::new()
        .ip_or_hostname(Some(endpoint.host.clone()))
        .tcp_port(endpoint.port)
        .user(Some(endpoint.user.clone()))
        .pass(Some(endpoint.password.clone()))
        .db_name(Some(endpoint.database.clone()))
}

fn postgres_config(endpoint: &Endpoint) -> postgres::Config {
    let mut config = postgres::Config::new();
    config
        .host(&endpoint.host)
        .port(endpoint.port)
        .user(&endpoint.user)
        .password(&endpoint.password)
        .dbname(&endpoint.database);
    config
}

fn endpoint_schema(endpoint: &Endpoint) -> String {
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

fn prepare_target_schema(target: &mut LiveAdapter, endpoint: &Endpoint) -> Result<(), String> {
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

fn default_mode() -> String {
    "create_only".to_string()
}

fn default_chunk_size() -> usize {
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

fn dump_manifest_consistency_metadata(threads: usize) -> (String, bool, Vec<String>) {
    if threads > 1 {
        (
            "non_consistent_parallel".to_string(),
            false,
            vec!["parallel export did not prove a shared consistent snapshot".to_string()],
        )
    } else {
        ("connection_consistent".to_string(), true, Vec::new())
    }
}

pub struct CoreService {
    connections: BTreeMap<String, LiveAdapter>,
    next_connection_sequence: u64,
}

impl CoreService {
    pub fn new() -> Self {
        Self {
            connections: BTreeMap::new(),
            next_connection_sequence: 1,
        }
    }

    pub fn handle_request_streaming<F: FnMut(Value)>(&mut self, request: Request, emit: F) {
        match request.command.as_str() {
            "connection.open" => emit_all_events(self.connection_open(&request), emit),
            "connection.close" => emit_all_events(self.connection_close(&request), emit),
            "query.execute" => emit_all_events(self.query_execute(&request), emit),
            "service.shutdown" => {
                self.connections.clear();
                emit_all_events(service_shutdown(&request), emit);
            }
            _ => handle_request_streaming(request, emit),
        }
    }

    fn connection_open(&mut self, request: &Request) -> Vec<Value> {
        let endpoint = match request_endpoint(request) {
            Ok(endpoint) => endpoint,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        };
        match LiveAdapter::connect(&endpoint) {
            Ok(adapter) => {
                let id = unique_connection_id(&endpoint, self.next_connection_sequence);
                self.next_connection_sequence = self.next_connection_sequence.saturating_add(1);
                self.connections.insert(id.clone(), adapter);
                vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "connection.open",
                    "success": true,
                    "connection_id": id,
                    "engine": endpoint.engine
                })]
            }
            Err(err) => vec![json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "connection.open",
                "success": false,
                "engine": endpoint.engine,
                "message": redact_endpoint_secret(&err, &endpoint)
            })],
        }
    }

    fn connection_close(&mut self, request: &Request) -> Vec<Value> {
        let connection_id = request
            .payload
            .get("connection_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let removed = self.connections.remove(connection_id).is_some();
        vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.close",
            "success": true,
            "closed": removed,
            "connection_id": connection_id
        })]
    }

    fn query_execute(&mut self, request: &Request) -> Vec<Value> {
        if let Some(connection_id) = request.payload.get("connection_id").and_then(Value::as_str) {
            let sql = request
                .payload
                .get("sql")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if sql.is_empty() {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": "query.execute requires sql"
                })];
            }
            let Some(adapter) = self.connections.get_mut(connection_id) else {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": format!("unknown connection_id: {connection_id}")
                })];
            };
            let params = query_params(&request.payload);
            let bound_sql = bind_query_params(sql, &params);
            return match execute_query_adapter(adapter, &bound_sql) {
                Ok(rows) => query_result_events(request, rows),
                Err(err) => vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })],
            };
        }
        query_execute(request)
    }
}

impl Default for CoreService {
    fn default() -> Self {
        Self::new()
    }
}

pub fn handle_line(line: &str) -> Vec<Value> {
    match serde_json::from_str::<Request>(line) {
        Ok(request) => handle_request(request),
        Err(err) => vec![json!({
            "event": "error",
            "message": format!("invalid request JSON: {err}")
        })],
    }
}

pub fn handle_line_streaming<F: FnMut(Value)>(line: &str, mut emit: F) {
    match serde_json::from_str::<Request>(line) {
        Ok(request) => handle_request_streaming(request, emit),
        Err(err) => emit(json!({
            "event": "error",
            "message": format!("invalid request JSON: {err}")
        })),
    }
}

pub fn handle_request(request: Request) -> Vec<Value> {
    let mut events = Vec::new();
    handle_request_streaming(request, |event| events.push(event));
    events
}

pub fn handle_request_streaming<F: FnMut(Value)>(request: Request, mut emit: F) {
    match request.command.as_str() {
        "service.hello" => emit_all_events(service_hello(&request), emit),
        "service.shutdown" => emit_all_events(service_shutdown(&request), emit),
        "connection.test" => emit_all_events(connection_test(&request), emit),
        "connection.open" => emit_all_events(connection_open(&request), emit),
        "connection.close" => emit_all_events(connection_close(&request), emit),
        "schema.list" => emit_all_events(schema_list(&request), emit),
        "schema.inspect" => emit_all_events(alias_events(&request, "inspect"), emit),
        "schema.diff" => emit_all_events(schema_diff(&request), emit),
        "query.execute" => emit_all_events(query_execute(&request), emit),
        "query.cancel" => emit_all_events(query_cancel(&request), emit),
        "dump.run" => dump_run_streaming(&request, emit),
        "dump.import" => dump_import_streaming(&request, emit),
        "migration.plan" => emit_all_events(alias_events(&request, "plan"), emit),
        "migration.verify" => emit_all_events(alias_events(&request, "verify"), emit),
        "migration.resume" => emit_all_events(alias_events(&request, "resume"), emit),
        "migration.cleanup" => {
            let alias = Request {
                command: "cleanup".to_string(),
                request_id: request.request_id.clone(),
                payload: request.payload.clone(),
            };
            let command = request.command.clone();
            cleanup_streaming(&alias, |event| {
                emit(rewrite_result_command(event, &command))
            });
        }
        "migration.run" => {
            let alias = Request {
                command: "migrate".to_string(),
                request_id: request.request_id.clone(),
                payload: request.payload.clone(),
            };
            let command = request.command.clone();
            migrate_streaming(&alias, |event| {
                emit(rewrite_result_command(event, &command))
            });
        }
        "oneclick.run" => oneclick_run_streaming(&request, emit),
        "oneclick.preflight" => emit_all_events(oneclick_preflight(&request), emit),
        "oneclick.analyze" => emit_all_events(oneclick_analyze(&request), emit),
        "oneclick.recommend" => emit_all_events(oneclick_recommend(&request), emit),
        "oneclick.apply_fixes" => emit_all_events(oneclick_apply_fixes(&request), emit),
        "oneclick.validate" => emit_all_events(oneclick_validate(&request), emit),
        "oneclick.report" => emit_all_events(oneclick_report(&request), emit),
        "job.cancel" => emit_all_events(job_cancel(&request), emit),
        "inspect" => emit_all_events(inspect(&request), emit),
        "preflight" => preflight_streaming(&request, emit),
        "readiness" => emit_all_events(readiness(&request), emit),
        "guide" => emit_all_events(guide(&request), emit),
        "plan" => emit_all_events(plan(&request), emit),
        "migrate" => migrate_streaming(&request, emit),
        "verify" => emit_all_events(verify(&request), emit),
        "resume" => emit_all_events(resume(&request), emit),
        "cleanup" => cleanup_streaming(&request, emit),
        other => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": format!("unknown command: {other}")
        })),
    }
}

fn emit_all_events<F: FnMut(Value)>(events: Vec<Value>, mut emit: F) {
    for event in events {
        emit(event);
    }
}

fn alias_events(request: &Request, legacy_command: &str) -> Vec<Value> {
    let legacy = Request {
        command: legacy_command.to_string(),
        request_id: request.request_id.clone(),
        payload: request.payload.clone(),
    };
    handle_request(legacy)
        .into_iter()
        .map(|event| rewrite_result_command(event, &request.command))
        .collect()
}

fn rewrite_result_command(mut event: Value, command: &str) -> Value {
    if event.get("event") == Some(&json!("result")) {
        if let Value::Object(object) = &mut event {
            object.insert("command".to_string(), Value::String(command.to_string()));
        }
    }
    event
}

fn service_hello(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "service.hello",
        "success": true,
        "service": "tunnelforge-core",
        "protocol_version": 1,
        "capabilities": [
            "connection.open",
            "connection.close",
            "connection.test",
            "schema.list",
            "schema.inspect",
            "schema.diff",
            "query.execute",
            "query.cancel",
            "dump.run",
            "dump.import",
            "migration.plan",
            "migration.run",
            "migration.verify",
            "migration.resume",
            "oneclick.run",
            "oneclick.preflight",
            "oneclick.analyze",
            "oneclick.recommend",
            "oneclick.apply_fixes",
            "oneclick.validate",
            "oneclick.report",
            "job.cancel",
            "service.shutdown"
        ]
    })]
}

fn service_shutdown(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "service.shutdown",
        "success": true
    })]
}

fn connection_test(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    match LiveAdapter::connect(&endpoint) {
        Ok(_) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.test",
            "success": true,
            "engine": endpoint.engine,
            "message": "connection successful"
        })],
        Err(err) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.test",
            "success": false,
            "engine": endpoint.engine,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn connection_open(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    match LiveAdapter::connect(&endpoint) {
        Ok(_) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.open",
            "success": true,
            "connection_id": connection_id(&endpoint),
            "engine": endpoint.engine
        })],
        Err(err) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.open",
            "success": false,
            "engine": endpoint.engine,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn connection_close(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "connection.close",
        "success": true,
        "connection_id": request.payload.get("connection_id").cloned().unwrap_or(Value::Null)
    })]
}

fn schema_list(request: &Request) -> Vec<Value> {
    if let Ok(endpoint) = request_endpoint(request) {
        match inspect_live(&endpoint) {
            Ok(result) => {
                let tables: Vec<String> = result
                    .schema
                    .tables
                    .iter()
                    .map(|table| table.name.clone())
                    .collect();
                return vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "schema.list",
                    "success": true,
                    "engine": endpoint.engine,
                    "schema": endpoint_schema(&endpoint),
                    "tables": tables
                })];
            }
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": redact_endpoint_secret(&err, &endpoint)
                })]
            }
        }
    }

    let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
    let tables: Vec<String> = schema
        .tables
        .iter()
        .map(|table| table.name.clone())
        .collect();
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "schema.list",
        "success": true,
        "tables": tables
    })]
}

fn schema_diff(request: &Request) -> Vec<Value> {
    let source_schema = if request.payload.get("source_schema").is_some() {
        parse_schema(&request.payload["source_schema"]).unwrap_or_default()
    } else if request.payload.get("source").is_some() {
        match request
            .payload
            .get("source")
            .map(endpoint_from_value)
            .transpose()
            .and_then(|endpoint| {
                endpoint
                    .map(|endpoint| inspect_live(&endpoint).map(|result| result.schema))
                    .unwrap_or_else(|| Ok(NormalizedSchema::default()))
            }) {
            Ok(schema) => schema,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        }
    } else {
        NormalizedSchema::default()
    };
    let target_schema = if request.payload.get("target_schema").is_some() {
        parse_schema(&request.payload["target_schema"]).unwrap_or_default()
    } else if request.payload.get("target").is_some() {
        match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
            .and_then(|endpoint| {
                endpoint
                    .map(|endpoint| inspect_live(&endpoint).map(|result| result.schema))
                    .unwrap_or_else(|| Ok(NormalizedSchema::default()))
            }) {
            Ok(schema) => schema,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        }
    } else {
        NormalizedSchema::default()
    };

    let differences = normalized_schema_diff(&source_schema, &target_schema);
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "schema.diff",
        "success": differences.is_empty(),
        "differences": differences
    })]
}

fn query_execute(request: &Request) -> Vec<Value> {
    if let Some(rows) = request.payload.get("rows") {
        return vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "query.execute",
            "success": true,
            "rows": rows,
            "rows_affected": 0
        })];
    }

    let sql = request
        .payload
        .get("sql")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if sql.is_empty() {
        return vec![json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "query.execute requires sql"
        })];
    }
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    let params = query_params(&request.payload);
    let bound_sql = bind_query_params(sql, &params);
    match execute_query_live(&endpoint, &bound_sql) {
        Ok(rows) => query_result_events(request, rows),
        Err(err) => vec![json!({
            "event": "error",
            "request_id": request.request_id,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn query_result_events(request: &Request, rows: Vec<Value>) -> Vec<Value> {
    let stream_rows = request
        .payload
        .get("stream_rows")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !stream_rows {
        return vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "query.execute",
            "success": true,
            "rows": rows,
            "rows_affected": 0
        })];
    }

    let batch_size = request
        .payload
        .get("row_batch_size")
        .and_then(Value::as_u64)
        .unwrap_or(500)
        .max(1) as usize;
    let total = rows.len();
    let mut events = Vec::new();
    for (index, chunk) in rows.chunks(batch_size).enumerate() {
        events.push(json!({
            "event": "row_batch",
            "request_id": request.request_id,
            "command": "query.execute",
            "batch_index": index,
            "rows": chunk,
            "total": total
        }));
    }
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "query.execute",
        "success": true,
        "rows": [],
        "rows_streamed": total,
        "rows_affected": 0
    }));
    events
}

fn query_cancel(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "query.cancel",
        "success": true,
        "cancelled": false,
        "message": "No asynchronous query is registered for this JSONL worker",
        "job_id": request.payload.get("job_id").cloned().unwrap_or(Value::Null)
    })]
}

fn job_cancel(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "job.cancel",
        "success": true,
        "cancelled": false,
        "message": "UI workers cancel long-running Rust jobs by terminating the isolated core process",
        "job_id": request.payload.get("job_id").cloned().unwrap_or(Value::Null)
    })]
}

fn dump_run_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump",
        "message": "dump started"
    }));

    match dump_run(request, |event| emit(event)) {
        Ok(result) => emit(result),
        Err(err) => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct DumpTableStats {
    rows: u64,
    avg_row_bytes: u64,
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
struct DumpTablePerfProfile {
    avg_row_bytes: u64,
    chunk_rows: usize,
    rows_per_second: u64,
    duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DumpWorkPlanItem {
    table: String,
    chunk_index: Option<u64>,
}

fn global_dump_work_plan(
    tables: &[NormalizedTable],
    range_chunks: &BTreeMap<String, u64>,
) -> Vec<DumpWorkPlanItem> {
    let mut plan = Vec::new();
    let max_chunks = range_chunks.values().copied().max().unwrap_or(0);
    for chunk_index in 1..=max_chunks {
        for table in tables {
            if let Some(chunks) = range_chunks.get(&table.name).copied() {
                if chunk_index <= chunks {
                    plan.push(DumpWorkPlanItem {
                        table: table.name.clone(),
                        chunk_index: Some(chunk_index),
                    });
                }
            }
        }
        if chunk_index == 1 {
            for table in tables {
                if !range_chunks.contains_key(&table.name) {
                    plan.push(DumpWorkPlanItem {
                        table: table.name.clone(),
                        chunk_index: None,
                    });
                }
            }
        }
    }
    if max_chunks == 0 {
        for table in tables {
            plan.push(DumpWorkPlanItem {
                table: table.name.clone(),
                chunk_index: None,
            });
        }
    }
    plan
}

fn global_dump_work_plan_for_ranges(
    tables: &[NormalizedTable],
    range_chunks: &BTreeMap<String, Vec<DumpRange>>,
) -> Vec<DumpWorkPlanItem> {
    let range_counts = range_chunks
        .iter()
        .map(|(table, ranges)| (table.clone(), ranges.len() as u64))
        .collect::<BTreeMap<_, _>>();
    global_dump_work_plan(tables, &range_counts)
}

fn dump_table_stats(
    endpoint: &Endpoint,
    tables: &[NormalizedTable],
) -> BTreeMap<String, DumpTableStats> {
    let mut counts = BTreeMap::new();
    if endpoint.engine != "mysql" || tables.is_empty() {
        return counts;
    }
    let mut conn = match LiveAdapter::connect(endpoint) {
        Ok(LiveAdapter::MySql(conn)) => conn,
        _ => return counts,
    };
    let schema_name = endpoint_schema(endpoint);
    let table_names = tables
        .iter()
        .map(|table| sql_literal(&Value::String(table.name.clone())))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0), COALESCE(AVG_ROW_LENGTH, 0) FROM information_schema.tables WHERE TABLE_SCHEMA = {} AND TABLE_NAME IN ({})",
        sql_literal(&Value::String(schema_name)),
        table_names
    );
    let Ok(rows) = conn.query::<(String, u64, u64), _>(sql) else {
        return counts;
    };
    for (table, rows, avg_row_bytes) in rows {
        counts.insert(
            table,
            DumpTableStats {
                rows,
                avg_row_bytes,
            },
        );
    }
    counts
}

fn dump_perf_profile_path() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .or_else(|| std::env::var_os("APPDATA"))
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .map(|base| base.join("TunnelForge").join("dump_perf_profile.json"))
}

fn dump_profile_key(
    endpoint: &Endpoint,
    table: &str,
    data_format: &str,
    compression: &str,
) -> String {
    format!(
        "{}:{}:{}:{}:{}",
        endpoint.engine, endpoint.database, table, data_format, compression
    )
}

fn load_dump_perf_profiles() -> BTreeMap<String, DumpTablePerfProfile> {
    let Some(path) = dump_perf_profile_path() else {
        return BTreeMap::new();
    };
    let Ok(bytes) = fs::read(path) else {
        return BTreeMap::new();
    };
    serde_json::from_slice(&bytes).unwrap_or_default()
}

fn save_dump_perf_profiles(profiles: &BTreeMap<String, DumpTablePerfProfile>) {
    let Some(path) = dump_perf_profile_path() else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(bytes) = serde_json::to_vec_pretty(profiles) {
        let _ = fs::write(path, bytes);
    }
}

fn dump_plan_event(
    request_id: Option<String>,
    tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
) -> Value {
    let rows_total = tables
        .iter()
        .map(|table| row_counts.get(&table.name).copied().unwrap_or(0))
        .sum::<u64>();
    json!({
        "event": "dump_plan",
        "request_id": request_id,
        "tables_total": tables.len(),
        "rows_total": rows_total,
        "tables": tables.iter().map(|table| {
            json!({
                "name": table.name,
                "rows": row_counts.get(&table.name).copied().unwrap_or(0)
            })
        }).collect::<Vec<_>>()
    })
}

fn dump_schedule_event(
    request_id: Option<String>,
    scheduled_tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
    limits: DumpParallelLimits,
    threads: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    scheduler: &str,
) -> Value {
    let chunk_size = chunk_size.max(1) as u64;
    json!({
        "event": "dump_schedule",
        "request_id": request_id,
        "threads": threads,
        "table_workers": limits.table_workers,
        "range_workers_per_table": limits.range_workers_per_table,
        "chunk_size": chunk_size,
        "data_format": data_format,
        "compression": compression,
        "scheduler": scheduler,
        "scheduled_tables": scheduled_tables.iter().take(12).map(|table| {
            let rows = row_counts.get(&table.name).copied().unwrap_or(0);
            json!({
                "name": table.name,
                "rows": rows,
                "estimated_chunks": rows.saturating_add(chunk_size - 1) / chunk_size
            })
        }).collect::<Vec<_>>()
    })
}

fn dump_import_row_progress_event(
    request_id: Option<String>,
    table: &str,
    table_rows_done: u64,
    table_rows_total: u64,
    overall_rows_before: u64,
    overall_rows_total: u64,
    chunk_rows: u64,
    chunks_done: Option<u64>,
    chunks_total: Option<u64>,
    chunk_index: Option<u64>,
    load_ms: Option<u64>,
    strategy: &str,
) -> Value {
    let raw_overall_rows_done = overall_rows_before.saturating_add(table_rows_done);
    let overall_rows_done = if overall_rows_total > 0 {
        raw_overall_rows_done.min(overall_rows_total)
    } else {
        raw_overall_rows_done
    };
    let mut event = json!({
        "event": "row_progress",
        "request_id": request_id,
        "table": table,
        "rows": table_rows_done,
        "total": table_rows_total,
        "table_rows_done": table_rows_done,
        "table_rows_total": table_rows_total,
        "overall_rows_done": overall_rows_done,
        "overall_rows_total": overall_rows_total,
        "chunk_rows": chunk_rows,
        "strategy": strategy
    });

    if let Value::Object(fields) = &mut event {
        if let Some(value) = chunks_done {
            fields.insert("chunks_done".to_string(), json!(value));
        }
        if let Some(value) = chunks_total {
            fields.insert("chunks_total".to_string(), json!(value));
        }
        if let Some(value) = chunk_index {
            fields.insert("chunk_index".to_string(), json!(value));
        }
        if let Some(value) = load_ms {
            fields.insert("load_ms".to_string(), json!(value));
        }
    }

    event
}

fn dump_run<F: FnMut(Value)>(request: &Request, mut emit: F) -> Result<Value, String> {
    let endpoint = request_endpoint(request)?;
    let output_dir = request
        .payload
        .get("output_dir")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "dump.run requires output_dir".to_string())?;
    let chunk_size = request
        .payload
        .get("chunk_size")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or_else(default_chunk_size)
        .max(1);
    let threads = request
        .payload
        .get("threads")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or(8)
        .max(1);
    let overwrite = request
        .payload
        .get("overwrite")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let selected_tables = string_list(request.payload.get("tables"));
    let data_format = request
        .payload
        .get("data_format")
        .and_then(Value::as_str)
        .unwrap_or("tsv")
        .to_ascii_lowercase();
    if !matches!(data_format.as_str(), "jsonl" | "tsv") {
        return Err(format!("unsupported dump data_format: {data_format}"));
    }
    let compression = request
        .payload
        .get("compression")
        .and_then(Value::as_str)
        .unwrap_or("zstd")
        .to_ascii_lowercase();
    if !matches!(compression.as_str(), "none" | "zstd") {
        return Err(format!("unsupported dump compression: {compression}"));
    }

    let output_path = Path::new(output_dir);
    prepare_dump_output_dir(output_path, overwrite)?;

    // 부분 export(tables 지정) 시에는 View가 참조하는 base table이 빠질 수 있으므로 View를 수집하지 않는다.
    let full_export = selected_tables.is_empty();
    let inspection = inspect_live(&endpoint)?;
    let mut schema = inspection.schema;
    if !selected_tables.is_empty() {
        let selected: BTreeSet<String> = selected_tables.into_iter().collect();
        schema.tables.retain(|table| selected.contains(&table.name));
    }
    schema = dependency_ordered_schema(&schema);
    if schema.tables.is_empty() {
        return Err("dump.run found no tables to export".to_string());
    }

    let table_stats = dump_table_stats(&endpoint, &schema.tables);
    let row_counts = table_stats
        .iter()
        .map(|(table, stats)| (table.clone(), stats.rows))
        .collect::<BTreeMap<_, _>>();
    let range_eligible_tables = schema
        .tables
        .iter()
        .filter(|table| single_numeric_primary_key(table).is_some())
        .map(|table| table.name.clone())
        .collect::<BTreeSet<_>>();
    let avg_row_lengths = table_stats
        .iter()
        .filter(|(table, _)| range_eligible_tables.contains(*table))
        .map(|(table, stats)| (table.clone(), stats.avg_row_bytes))
        .collect::<BTreeMap<_, _>>();
    emit(dump_plan_event(
        request.request_id.clone(),
        &schema.tables,
        &row_counts,
    ));

    let table_total = schema.tables.len();
    let parallel_limits = adaptive_dump_parallel_limits_with_avg(
        threads,
        table_total,
        chunk_size,
        &row_counts,
        &avg_row_lengths,
    );
    let export_tables = if threads > 1 && table_total > 1 {
        dump_schedule_order(&schema.tables, &row_counts)
    } else {
        schema.tables.clone()
    };
    emit(dump_schedule_event(
        request.request_id.clone(),
        &export_tables,
        &row_counts,
        parallel_limits,
        threads,
        chunk_size,
        &data_format,
        &compression,
        if endpoint.engine == "mysql" && threads > 1 && table_total > 1 {
            "global_chunk"
        } else {
            "table_parallel"
        },
    ));
    let (table_manifests, total_rows, total_chunks) =
        if endpoint.engine == "mysql" && threads > 1 && table_total == 1 {
            match dump_single_mysql_table_parallel(
                &endpoint,
                output_path,
                &export_tables[0],
                chunk_size,
                &data_format,
                &compression,
                parallel_limits.range_workers_per_table,
                request.request_id.clone(),
                |event| emit(event),
            )? {
                Some(result) => result,
                None => {
                    let mut adapter = LiveAdapter::connect(&endpoint)?;
                    dump_tables_sequential(
                        &mut adapter,
                        output_path,
                        &export_tables,
                        chunk_size,
                        &data_format,
                        &compression,
                        request.request_id.clone(),
                        |event| emit(event),
                    )?
                }
            }
        } else if endpoint.engine == "mysql" && threads > 1 && table_total > 1 {
            dump_tables_global_mysql(
                &endpoint,
                output_path,
                &export_tables,
                chunk_size,
                &data_format,
                &compression,
                threads,
                request.request_id.clone(),
                |event| emit(event),
            )?
        } else if threads > 1 && table_total > 1 {
            dump_tables_parallel(
                &endpoint,
                output_path,
                &export_tables,
                chunk_size,
                &data_format,
                &compression,
                parallel_limits.table_workers,
                parallel_limits.range_workers_per_table,
                request.request_id.clone(),
                |event| emit(event),
            )?
        } else {
            let mut adapter = LiveAdapter::connect(&endpoint)?;
            dump_tables_sequential(
                &mut adapter,
                output_path,
                &export_tables,
                chunk_size,
                &data_format,
                &compression,
                request.request_id.clone(),
                |event| emit(event),
            )?
        };

    // View 정의 수집 (전체 export 시에만). 실패해도 테이블 덤프는 유효하므로 fatal로 보지 않는다.
    let views = if full_export {
        match collect_views(&endpoint) {
            Ok(views) => views,
            Err(err) => {
                emit(json!({
                    "event": "phase",
                    "request_id": request.request_id,
                    "phase": "dump",
                    "message": format!("View 정의 수집 실패 (테이블 덤프는 정상): {err}"),
                }));
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };
    let views_count = views.len();
    let (snapshot_policy, strict_export, manifest_warnings) =
        dump_manifest_consistency_metadata(threads);

    let manifest = DumpManifest {
        format: "tunnelforge-dump".to_string(),
        format_version: if data_format == "jsonl" { 1 } else { 2 },
        data_format,
        compression,
        source_engine: endpoint.engine.clone(),
        database: endpoint.database.clone(),
        schema,
        snapshot_policy,
        strict_export,
        manifest_warnings,
        chunk_size,
        created_unix_seconds: current_unix_seconds(),
        tables: table_manifests,
        views,
    };
    write_dump_manifest(output_path, &manifest)?;

    Ok(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "dump.run",
        "success": true,
        "output_dir": output_dir,
        "format": manifest.format,
        "format_version": manifest.format_version,
        "compression": manifest.compression,
        "snapshot_policy": manifest.snapshot_policy,
        "strict_export": manifest.strict_export,
        "manifest_warnings": manifest.manifest_warnings,
        "tables": manifest.tables.len(),
        "views": views_count,
        "rows_dumped": total_rows,
        "chunks_dumped": total_chunks,
        "manifest": "_tunnelforge_dump.json"
    }))
}

fn prepare_dump_output_dir(output_path: &Path, overwrite: bool) -> Result<(), String> {
    if output_path.as_os_str().is_empty() || output_path.parent().is_none() {
        return Err("refusing to use unsafe dump output_dir".to_string());
    }
    if output_path.exists() {
        let mut entries = fs::read_dir(output_path)
            .map_err(|err| format!("failed to inspect dump output_dir: {err}"))?;
        let is_empty = entries.next().is_none();
        if !is_empty {
            if !overwrite {
                return Err("dump output_dir already exists and is not empty".to_string());
            }
            if !has_tunnelforge_dump_marker(output_path) {
                return Err(
                    "refusing to overwrite output_dir without TunnelForge dump marker".to_string(),
                );
            }
            remove_dump_output_dir(output_path)?;
        }
    }
    fs::create_dir_all(output_path)
        .map_err(|err| format!("failed to create dump output_dir: {err}"))
}

fn remove_dump_output_dir(output_path: &Path) -> Result<(), String> {
    let confirmed_dump_dir = output_path;
    fs::remove_dir_all(confirmed_dump_dir)
        .map_err(|err| format!("failed to clear dump output_dir: {err}"))
}

fn has_tunnelforge_dump_marker(output_path: &Path) -> bool {
    let marker_path = output_path.join(DUMP_DIR_MARKER);
    let manifest_path = output_path.join("_tunnelforge_dump.json");
    let Ok(marker_file) = File::open(marker_path) else {
        return false;
    };
    let marker_ok = serde_json::from_reader::<_, Value>(marker_file)
        .ok()
        .and_then(|value| {
            value
                .get("format")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .as_deref()
        == Some("tunnelforge-dump-dir");
    if !marker_ok {
        return false;
    }
    let Ok(manifest_file) = File::open(manifest_path) else {
        return false;
    };
    serde_json::from_reader::<_, Value>(manifest_file)
        .ok()
        .and_then(|value| {
            value
                .get("format")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .as_deref()
        == Some("tunnelforge-dump")
}

fn dump_tables_sequential<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    output_path: &Path,
    tables: &[NormalizedTable],
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let mut manifests = Vec::new();
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let table_total = tables.len();

    for (index, table) in tables.iter().enumerate() {
        let (manifest, rows, chunks) = dump_one_table(
            adapter,
            output_path,
            table,
            index,
            table_total,
            chunk_size,
            data_format,
            compression,
            request_id.clone(),
            |event| emit(event),
        )?;
        manifests.push(manifest);
        total_rows += rows;
        total_chunks += chunks;
    }

    Ok((manifests, total_rows, total_chunks))
}

fn bounded_dump_chunk_limit(total_rows: u64, rows_dumped: u64, chunk_size: usize) -> Option<usize> {
    if total_rows > 0 && rows_dumped >= total_rows {
        return None;
    }
    let limit = chunk_size.max(1);
    if total_rows == 0 {
        return Some(limit);
    }
    Some(limit.min((total_rows - rows_dumped) as usize))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DumpParallelLimits {
    table_workers: usize,
    range_workers_per_table: usize,
}

impl DumpParallelLimits {
    #[cfg(test)]
    fn estimated_mysql_connections(&self) -> usize {
        self.table_workers * (self.range_workers_per_table + 1)
    }
}

fn dump_parallel_limits(threads: usize, table_total: usize) -> DumpParallelLimits {
    let thread_budget = threads.max(1);
    let table_workers = if table_total <= 1 {
        1
    } else if table_total <= thread_budget {
        table_total
    } else {
        (thread_budget / 4).max(1).min(table_total)
    };
    let range_workers_per_table = (thread_budget / table_workers).max(1);
    DumpParallelLimits {
        table_workers,
        range_workers_per_table,
    }
}

#[cfg(test)]
fn adaptive_dump_parallel_limits(
    threads: usize,
    table_total: usize,
    chunk_size: usize,
    row_counts: &BTreeMap<String, u64>,
) -> DumpParallelLimits {
    adaptive_dump_parallel_limits_with_avg(
        threads,
        table_total,
        chunk_size,
        row_counts,
        &BTreeMap::new(),
    )
}

fn adaptive_dump_parallel_limits_with_avg(
    threads: usize,
    table_total: usize,
    chunk_size: usize,
    row_counts: &BTreeMap<String, u64>,
    avg_row_lengths: &BTreeMap<String, u64>,
) -> DumpParallelLimits {
    let baseline = dump_parallel_limits(threads, table_total);
    let thread_budget = threads.max(1);
    if table_total <= 1 || row_counts.is_empty() {
        return baseline;
    }
    let fallback_chunk_size = chunk_size.max(1);
    let heavy_tables = row_counts
        .iter()
        .filter(|(table, rows)| {
            let effective_chunk_size = mysql_range_chunk_size_for_avg_row(
                fallback_chunk_size,
                avg_row_lengths.get(*table).copied().unwrap_or(0),
            ) as u64;
            rows.saturating_add(effective_chunk_size - 1) / effective_chunk_size
                >= (thread_budget as u64).saturating_mul(2)
        })
        .count();
    let max_estimated_chunks = row_counts
        .iter()
        .map(|(table, rows)| {
            let effective_chunk_size = mysql_range_chunk_size_for_avg_row(
                fallback_chunk_size,
                avg_row_lengths.get(table).copied().unwrap_or(0),
            ) as u64;
            rows.saturating_add(effective_chunk_size - 1) / effective_chunk_size
        })
        .max()
        .unwrap_or(0);
    if heavy_tables > 1 {
        return baseline;
    }
    if max_estimated_chunks >= (thread_budget as u64).saturating_mul(2) {
        return baseline;
    }
    baseline
}

fn dump_schedule_order(
    tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
) -> Vec<NormalizedTable> {
    let mut indexed = tables
        .iter()
        .cloned()
        .enumerate()
        .collect::<Vec<(usize, NormalizedTable)>>();
    indexed.sort_by(|(left_index, left), (right_index, right)| {
        row_counts
            .get(&right.name)
            .copied()
            .unwrap_or(0)
            .cmp(&row_counts.get(&left.name).copied().unwrap_or(0))
            .then_with(|| left_index.cmp(right_index))
    });
    indexed.into_iter().map(|(_, table)| table).collect()
}

fn dump_tables_parallel<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    tables: &[NormalizedTable],
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    table_threads: usize,
    range_threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let table_total = tables.len();
    let max_threads = table_threads.max(1).min(table_total);
    let mut pending = (0..table_total).collect::<VecDeque<_>>();
    let mut active = 0_usize;
    let mut completed = 0_usize;
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let mut first_error: Option<String> = None;
    let mut manifests: Vec<Option<DumpTableManifest>> = vec![None; table_total];
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<DumpTableEvent>();

    while active < max_threads {
        if let Some(index) = pending.pop_front() {
            handles.push(spawn_dump_table_worker(
                endpoint.clone(),
                output_path.to_path_buf(),
                tables[index].clone(),
                index,
                table_total,
                chunk_size,
                data_format.to_string(),
                compression.to_string(),
                range_threads,
                request_id.clone(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed < table_total && active > 0 {
        match receiver.recv() {
            Ok(DumpTableEvent::Progress(event)) => emit(event),
            Ok(DumpTableEvent::Done {
                index,
                manifest,
                rows,
                chunks,
            }) => {
                manifests[index] = Some(manifest);
                total_rows += rows;
                total_chunks += chunks;
                completed += 1;
                active = active.saturating_sub(1);
                if let Some(next_index) = pending.pop_front() {
                    handles.push(spawn_dump_table_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        tables[next_index].clone(),
                        next_index,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        range_threads,
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpTableEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed += 1;
                active = active.saturating_sub(1);
                if let Some(next_index) = pending.pop_front() {
                    handles.push(spawn_dump_table_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        tables[next_index].clone(),
                        next_index,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        range_threads,
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Err(_) => break,
        }
    }

    for handle in handles {
        let _ = handle.join();
    }
    if let Some(err) = first_error {
        return Err(err);
    }

    Ok((
        manifests
            .into_iter()
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| "parallel dump did not produce all table manifests".to_string())?,
        total_rows,
        total_chunks,
    ))
}

fn dump_tables_global_mysql<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    tables: &[NormalizedTable],
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(Vec<DumpTableManifest>, u64, u64), String> {
    let table_total = tables.len();
    let mut conn = match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => {
            return Err("global mysql dump requires mysql endpoint".to_string())
        }
    };
    let profiles = load_dump_perf_profiles();
    let mut ranges_by_table = BTreeMap::<String, Vec<DumpRange>>::new();
    let mut states = Vec::<DumpGlobalTableState>::new();

    for (index, table) in tables.iter().enumerate() {
        let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
        let table_dir = output_path.join(&table_path);
        fs::create_dir_all(&table_dir)
            .map_err(|err| format!("failed to create dump table dir: {err}"))?;
        let table_row_count = conn
            .query_first::<u64, _>(count_sql("mysql", &table.name))
            .map(|count| count.unwrap_or(0))
            .unwrap_or(0);
        let mut chunks_total = 0_u64;
        let avg_row_bytes = mysql_table_avg_row_length(&mut conn, endpoint, &table.name);
        if let Some(pk_column) = single_numeric_primary_key(table) {
            let profile_key = dump_profile_key(endpoint, &table.name, data_format, compression);
            let range_chunk_size = learned_mysql_range_chunk_size(
                chunk_size,
                avg_row_bytes,
                profiles.get(&profile_key),
            );
            if let Some((min_key, max_key)) =
                mysql_numeric_min_max(&mut conn, &table.name, pk_column)?
            {
                if should_use_pk_range_dump_for_span(
                    table,
                    table_row_count,
                    range_chunk_size,
                    min_key,
                    max_key,
                ) {
                    let ranges = pk_ranges(min_key, max_key, table_row_count, range_chunk_size);
                    chunks_total = ranges.len() as u64;
                    ranges_by_table.insert(table.name.clone(), ranges);
                    emit(json!({
                        "event": "table_progress",
                        "request_id": request_id,
                        "table": table.name,
                        "status": "dumping",
                        "current": index + 1,
                        "total": table_total,
                        "strategy": "global_pk_range_parallel",
                        "range_chunk_size": range_chunk_size,
                        "target_bytes_per_chunk": MYSQL_DUMP_TARGET_BYTES_PER_CHUNK,
                        "avg_row_bytes": avg_row_bytes
                    }));
                }
            }
        }
        states.push(DumpGlobalTableState {
            table_path,
            rows_total: table_row_count,
            rows_dumped: 0,
            chunks_total,
            chunks_done: 0,
            avg_row_bytes,
            work_ms: 0,
            chunk_sha256: BTreeMap::new(),
            manifest: None,
        });
    }

    let plan = global_dump_work_plan_for_ranges(tables, &ranges_by_table);
    let table_index_by_name = tables
        .iter()
        .enumerate()
        .map(|(index, table)| (table.name.clone(), index))
        .collect::<BTreeMap<_, _>>();
    let mut pending = VecDeque::<DumpGlobalWorkItem>::new();
    for item in plan {
        let Some(&table_index) = table_index_by_name.get(&item.table) else {
            continue;
        };
        let table = tables[table_index].clone();
        let kind = if let Some(chunk_index) = item.chunk_index {
            let Some(pk_column) = single_numeric_primary_key(&table) else {
                continue;
            };
            let Some(ranges) = ranges_by_table.get(&table.name) else {
                continue;
            };
            let Some(range) = ranges.get((chunk_index - 1) as usize).cloned() else {
                continue;
            };
            DumpGlobalWorkKind::MysqlRange {
                table_path: states[table_index].table_path.clone(),
                pk_column: pk_column.to_string(),
                range,
            }
        } else {
            DumpGlobalWorkKind::WholeTable
        };
        pending.push_back(DumpGlobalWorkItem {
            table_index,
            table,
            kind,
        });
    }

    let work_total = pending.len();
    if work_total == 0 {
        return Ok((Vec::new(), 0, 0));
    }
    let max_threads = threads.max(1).min(work_total);
    let mut active = 0_usize;
    let mut completed_work = 0_usize;
    let mut first_error: Option<String> = None;
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<DumpGlobalEvent>();

    while active < max_threads {
        if let Some(work) = pending.pop_front() {
            handles.push(spawn_dump_global_worker(
                endpoint.clone(),
                output_path.to_path_buf(),
                work,
                table_total,
                chunk_size,
                data_format.to_string(),
                compression.to_string(),
                request_id.clone(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed_work < work_total && active > 0 {
        match receiver.recv() {
            Ok(DumpGlobalEvent::Progress(event)) => emit(event),
            Ok(DumpGlobalEvent::RangeDone {
                table_index,
                chunk_index,
                rows,
                stream_ms,
                range_start,
                range_end,
                checksum,
            }) => {
                let table = &tables[table_index];
                let state = &mut states[table_index];
                state.rows_dumped += rows;
                state.chunks_done += 1;
                state.work_ms = state.work_ms.saturating_add(stream_ms.max(1));
                state.chunk_sha256.insert(
                    dump_chunk_name(chunk_index, data_format, compression),
                    checksum,
                );
                completed_work += 1;
                active = active.saturating_sub(1);
                emit(json!({
                    "event": "row_progress",
                    "request_id": request_id,
                    "table": table.name,
                    "rows": state.rows_dumped,
                    "total": state.rows_total,
                    "chunk_rows": rows,
                    "chunks_done": state.chunks_done,
                    "chunks_total": state.chunks_total,
                    "stream_ms": stream_ms,
                    "chunk_index": chunk_index,
                    "range_start": range_start,
                    "range_end": range_end,
                    "strategy": "global_pk_range_parallel"
                }));
                if state.chunks_done == state.chunks_total {
                    state.manifest = Some(DumpTableManifest {
                        name: table.name.clone(),
                        path: state.table_path.clone(),
                        rows: state.rows_dumped,
                        chunks: state.chunks_done,
                        chunk_sha256: state.chunk_sha256.clone(),
                    });
                    emit(json!({
                        "event": "table_progress",
                        "request_id": request_id,
                        "table": table.name,
                        "status": "completed",
                        "current": table_index + 1,
                        "total": table_total,
                        "strategy": "global_pk_range_parallel"
                    }));
                }
                if let Some(work) = pending.pop_front() {
                    handles.push(spawn_dump_global_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        work,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpGlobalEvent::TableDone {
                index,
                manifest,
                rows,
                chunks,
                duration_ms,
            }) => {
                let state = &mut states[index];
                state.rows_dumped = rows;
                state.chunks_done = chunks;
                state.chunks_total = chunks;
                state.work_ms = duration_ms.max(1);
                state.manifest = Some(manifest);
                completed_work += 1;
                active = active.saturating_sub(1);
                if let Some(work) = pending.pop_front() {
                    handles.push(spawn_dump_global_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        work,
                        table_total,
                        chunk_size,
                        data_format.to_string(),
                        compression.to_string(),
                        request_id.clone(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpGlobalEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed_work += 1;
                active = active.saturating_sub(1);
            }
            Err(_) => break,
        }
    }

    for handle in handles {
        let _ = handle.join();
    }
    if let Some(err) = first_error {
        return Err(err);
    }

    let mut profiles = profiles;
    for (index, table) in tables.iter().enumerate() {
        let state = &states[index];
        if state.rows_dumped > 0 {
            let duration_ms = state.work_ms.max(1);
            let rows_per_second = state.rows_dumped.saturating_mul(1000) / duration_ms;
            profiles.insert(
                dump_profile_key(endpoint, &table.name, data_format, compression),
                DumpTablePerfProfile {
                    avg_row_bytes: state.avg_row_bytes,
                    chunk_rows: if state.chunks_done > 0 {
                        (state.rows_dumped / state.chunks_done).max(1) as usize
                    } else {
                        chunk_size
                    },
                    rows_per_second,
                    duration_ms,
                },
            );
        }
    }
    save_dump_perf_profiles(&profiles);

    let manifests = states
        .into_iter()
        .map(|state| state.manifest)
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| "global dump did not produce all table manifests".to_string())?;
    let total_rows = manifests.iter().map(|table| table.rows).sum();
    let total_chunks = manifests.iter().map(|table| table.chunks).sum();
    Ok((manifests, total_rows, total_chunks))
}

fn dump_single_mysql_table_parallel<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<Option<(Vec<DumpTableManifest>, u64, u64)>, String> {
    Ok(dump_mysql_table_parallel_ranges(
        endpoint,
        output_path,
        table,
        0,
        1,
        chunk_size,
        data_format,
        compression,
        threads,
        request_id,
        |event| emit(event),
    )?
    .map(|(manifest, rows, chunks)| (vec![manifest], rows, chunks)))
}

fn dump_mysql_table_parallel_ranges<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<Option<(DumpTableManifest, u64, u64)>, String> {
    let Some(pk_column) = single_numeric_primary_key(table) else {
        return Ok(None);
    };

    let mut conn = match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => return Ok(None),
    };
    let table_row_count = conn
        .query_first::<u64, _>(count_sql("mysql", &table.name))
        .map(|count| count.unwrap_or(0))
        .unwrap_or(0);
    let avg_row_bytes = mysql_table_avg_row_length(&mut conn, endpoint, &table.name);
    let range_chunk_size = mysql_range_chunk_size_for_avg_row(chunk_size, avg_row_bytes);
    if !should_use_pk_range_dump(table, table_row_count, range_chunk_size) {
        return Ok(None);
    }
    let Some((min_key, max_key)) = mysql_numeric_min_max(&mut conn, &table.name, pk_column)? else {
        return Ok(None);
    };
    if !should_use_pk_range_dump_for_span(
        table,
        table_row_count,
        range_chunk_size,
        min_key,
        max_key,
    ) {
        return Ok(None);
    }

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "dumping",
        "current": index + 1,
        "total": table_total,
        "strategy": "pk_range_parallel",
        "range_chunk_size": range_chunk_size,
        "target_bytes_per_chunk": MYSQL_DUMP_TARGET_BYTES_PER_CHUNK,
        "avg_row_bytes": avg_row_bytes
    }));

    let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
    let table_dir = output_path.join(&table_path);
    fs::create_dir_all(&table_dir)
        .map_err(|err| format!("failed to create dump table dir: {err}"))?;
    let ranges = pk_ranges(min_key, max_key, table_row_count, range_chunk_size);
    let total_ranges = ranges.len();
    let max_threads = threads.max(1).min(total_ranges.max(1));
    let mut pending = ranges.into_iter().collect::<VecDeque<_>>();
    let mut active = 0_usize;
    let mut completed = 0_usize;
    let mut rows_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();
    let mut first_error: Option<String> = None;
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<DumpRangeEvent>();

    while active < max_threads {
        if let Some(range) = pending.pop_front() {
            handles.push(spawn_mysql_range_worker(
                endpoint.clone(),
                output_path.to_path_buf(),
                table.clone(),
                table_path.clone(),
                pk_column.to_string(),
                range,
                data_format.to_string(),
                compression.to_string(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed < total_ranges && active > 0 {
        match receiver.recv() {
            Ok(DumpRangeEvent::Done {
                chunk_index,
                rows,
                stream_ms,
                range_start,
                range_end,
                checksum,
            }) => {
                rows_dumped += rows;
                completed += 1;
                active = active.saturating_sub(1);
                chunk_sha256.insert(
                    dump_chunk_name(chunk_index, data_format, compression),
                    checksum,
                );
                emit(json!({
                    "event": "row_progress",
                    "request_id": request_id,
                    "table": table.name,
                    "rows": rows_dumped,
                    "total": table_row_count,
                    "chunk_rows": rows,
                    "chunks_done": completed,
                    "chunks_total": total_ranges,
                    "stream_ms": stream_ms,
                    "chunk_index": chunk_index,
                    "range_start": range_start,
                    "range_end": range_end,
                    "strategy": "pk_range_parallel"
                }));
                if let Some(range) = pending.pop_front() {
                    handles.push(spawn_mysql_range_worker(
                        endpoint.clone(),
                        output_path.to_path_buf(),
                        table.clone(),
                        table_path.clone(),
                        pk_column.to_string(),
                        range,
                        data_format.to_string(),
                        compression.to_string(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(DumpRangeEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed += 1;
                active = active.saturating_sub(1);
            }
            Err(_) => break,
        }
    }

    for handle in handles {
        let _ = handle.join();
    }
    if let Some(err) = first_error {
        return Err(err);
    }

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "completed",
        "current": index + 1,
        "total": table_total,
        "strategy": "pk_range_parallel"
    }));

    Ok(Some((
        DumpTableManifest {
            name: table.name.clone(),
            path: table_path,
            rows: rows_dumped,
            chunks: total_ranges as u64,
            chunk_sha256,
        },
        rows_dumped,
        total_ranges as u64,
    )))
}

fn spawn_mysql_range_worker(
    endpoint: Endpoint,
    output_path: std::path::PathBuf,
    table: NormalizedTable,
    table_path: String,
    pk_column: String,
    range: DumpRange,
    data_format: String,
    compression: String,
    sender: mpsc::Sender<DumpRangeEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = dump_mysql_range_chunk(
            &endpoint,
            &output_path,
            &table,
            &table_path,
            &pk_column,
            &range,
            &data_format,
            &compression,
        );
        match result {
            Ok((rows, stream_ms, checksum)) => {
                let _ = sender.send(DumpRangeEvent::Done {
                    chunk_index: range.chunk_index,
                    rows,
                    stream_ms,
                    range_start: range.start.to_string(),
                    range_end: range.end.to_string(),
                    checksum,
                });
            }
            Err(err) => {
                let _ = sender.send(DumpRangeEvent::Error(err));
            }
        }
    })
}

fn spawn_dump_global_worker(
    endpoint: Endpoint,
    output_path: PathBuf,
    work: DumpGlobalWorkItem,
    table_total: usize,
    chunk_size: usize,
    data_format: String,
    compression: String,
    request_id: Option<String>,
    sender: mpsc::Sender<DumpGlobalEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || match work.kind {
        DumpGlobalWorkKind::MysqlRange {
            table_path,
            pk_column,
            range,
        } => {
            let result = dump_mysql_range_chunk(
                &endpoint,
                &output_path,
                &work.table,
                &table_path,
                &pk_column,
                &range,
                &data_format,
                &compression,
            );
            match result {
                Ok((rows, stream_ms, checksum)) => {
                    let _ = sender.send(DumpGlobalEvent::RangeDone {
                        table_index: work.table_index,
                        chunk_index: range.chunk_index,
                        rows,
                        stream_ms,
                        range_start: range.start.to_string(),
                        range_end: range.end.to_string(),
                        checksum,
                    });
                }
                Err(err) => {
                    let _ = sender.send(DumpGlobalEvent::Error(err));
                }
            }
        }
        DumpGlobalWorkKind::WholeTable => {
            let result = (|| {
                let mut adapter = LiveAdapter::connect(&endpoint)?;
                let started = Instant::now();
                dump_one_table(
                    &mut adapter,
                    &output_path,
                    &work.table,
                    work.table_index,
                    table_total,
                    chunk_size,
                    &data_format,
                    &compression,
                    request_id,
                    |event| {
                        let _ = sender.send(DumpGlobalEvent::Progress(event));
                    },
                )
                .map(|(manifest, rows, chunks)| {
                    (
                        manifest,
                        rows,
                        chunks,
                        started.elapsed().as_millis().max(1) as u64,
                    )
                })
            })();
            match result {
                Ok((manifest, rows, chunks, duration_ms)) => {
                    let _ = sender.send(DumpGlobalEvent::TableDone {
                        index: work.table_index,
                        manifest,
                        rows,
                        chunks,
                        duration_ms,
                    });
                }
                Err(err) => {
                    let _ = sender.send(DumpGlobalEvent::Error(err));
                }
            }
        }
    })
}

fn dump_mysql_range_chunk(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    table_path: &str,
    pk_column: &str,
    range: &DumpRange,
    data_format: &str,
    compression: &str,
) -> Result<(u64, u64, String), String> {
    let mut conn = match LiveAdapter::connect(endpoint)? {
        LiveAdapter::MySql(conn) => conn,
        LiveAdapter::PostgreSql(_) => {
            return Err("pk range dump requires mysql endpoint".to_string())
        }
    };
    let chunk_path = output_path.join(table_path).join(dump_chunk_name(
        range.chunk_index,
        data_format,
        compression,
    ));
    let columns = column_names(table);
    let sql = select_chunk_text_range_sql("mysql", table, pk_column, range.start, range.end);
    let stream_started = Instant::now();
    let result = conn
        .query_iter(sql)
        .map_err(|err| format!("mysql range select chunk error: {err}"))?;
    let mut rows = 0_u64;
    {
        let mut file = open_dump_writer(&chunk_path, compression)?;
        for row in result {
            let row = row.map_err(|err| format!("mysql dump row error: {err}"))?;
            if data_format == "tsv" {
                write_mysql_text_row_tsv(&mut file, row)?;
            } else {
                let row_json = mysql_row_to_json(&columns, row);
                write_dump_row(&mut file, table, &row_json, data_format)?;
            }
            rows += 1;
        }
    }
    let checksum = sha256_file(&chunk_path)?;
    Ok((rows, stream_started.elapsed().as_millis() as u64, checksum))
}

fn spawn_dump_table_worker(
    endpoint: Endpoint,
    output_path: std::path::PathBuf,
    table: NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: String,
    compression: String,
    range_threads: usize,
    request_id: Option<String>,
    sender: mpsc::Sender<DumpTableEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = (|| {
            if endpoint.engine == "mysql" {
                if let Some(result) = dump_mysql_table_parallel_ranges(
                    &endpoint,
                    &output_path,
                    &table,
                    index,
                    table_total,
                    chunk_size,
                    &data_format,
                    &compression,
                    range_threads,
                    request_id.clone(),
                    |event| {
                        let _ = sender.send(DumpTableEvent::Progress(event));
                    },
                )? {
                    return Ok(result);
                }
            }
            let mut adapter = LiveAdapter::connect(&endpoint)?;
            dump_one_table(
                &mut adapter,
                &output_path,
                &table,
                index,
                table_total,
                chunk_size,
                &data_format,
                &compression,
                request_id,
                |event| {
                    let _ = sender.send(DumpTableEvent::Progress(event));
                },
            )
        })();
        match result {
            Ok((manifest, rows, chunks)) => {
                let _ = sender.send(DumpTableEvent::Done {
                    index,
                    manifest,
                    rows,
                    chunks,
                });
            }
            Err(err) => {
                let _ = sender.send(DumpTableEvent::Error(err));
            }
        }
    })
}

fn dump_one_table<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(DumpTableManifest, u64, u64), String> {
    if let LiveAdapter::MySql(conn) = adapter {
        return dump_one_mysql_table(
            conn,
            output_path,
            table,
            index,
            table_total,
            chunk_size,
            data_format,
            compression,
            request_id,
            emit,
        );
    }

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "dumping",
        "current": index + 1,
        "total": table_total
    }));
    let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
    let table_dir = output_path.join(&table_path);
    fs::create_dir_all(&table_dir)
        .map_err(|err| format!("failed to create dump table dir: {err}"))?;

    let table_row_count = adapter.row_count(&table.name).unwrap_or(0) as u64;
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut last_key: Option<String> = None;
    let mut offset = 0_usize;
    let mut rows_dumped = 0_u64;
    let mut chunks_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();

    loop {
        let Some(read_limit) = bounded_dump_chunk_limit(table_row_count, rows_dumped, chunk_size)
        else {
            break;
        };
        let read_started = Instant::now();
        let rows = if use_keyset {
            adapter.read_rows_after_key(table, &key_columns, last_key.as_deref(), read_limit)?
        } else {
            adapter.read_rows(table, offset, read_limit)?
        };
        let read_ms = read_started.elapsed().as_millis() as u64;
        if rows.is_empty() {
            break;
        }
        chunks_dumped += 1;
        let chunk_name = dump_chunk_name(chunks_dumped, data_format, compression);
        let write_started = Instant::now();
        let checksum = write_dump_rows(
            &table_dir.join(&chunk_name),
            table,
            &rows,
            data_format,
            compression,
        )?;
        chunk_sha256.insert(chunk_name, checksum);
        let write_ms = write_started.elapsed().as_millis() as u64;

        let copied_now = rows.len();
        rows_dumped += copied_now as u64;
        if use_keyset {
            last_key = rows.last().and_then(|row| row_key_token(row, &key_columns));
        } else {
            offset += copied_now;
        }

        emit(json!({
            "event": "row_progress",
            "request_id": request_id,
            "table": table.name,
            "rows": rows_dumped,
            "total": table_row_count,
            "chunk_rows": copied_now,
            "read_ms": read_ms,
            "write_ms": write_ms
        }));
    }

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "completed",
        "current": index + 1,
        "total": table_total
    }));

    Ok((
        DumpTableManifest {
            name: table.name.clone(),
            path: table_path,
            rows: rows_dumped,
            chunks: chunks_dumped,
            chunk_sha256,
        },
        rows_dumped,
        chunks_dumped,
    ))
}

fn dump_one_mysql_table<F: FnMut(Value)>(
    conn: &mut mysql::PooledConn,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    compression: &str,
    request_id: Option<String>,
    mut emit: F,
) -> Result<(DumpTableManifest, u64, u64), String> {
    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "dumping",
        "current": index + 1,
        "total": table_total
    }));
    let table_path = format!("{:04}_{}", index + 1, safe_dump_component(&table.name));
    let table_dir = output_path.join(&table_path);
    fs::create_dir_all(&table_dir)
        .map_err(|err| format!("failed to create dump table dir: {err}"))?;

    let table_row_count = conn
        .query_first::<u64, _>(count_sql("mysql", &table.name))
        .map(|count| count.unwrap_or(0))
        .unwrap_or(0);
    let columns = column_names(table);
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut last_key: Option<String> = None;
    let mut offset = 0_usize;
    let mut rows_dumped = 0_u64;
    let mut chunks_dumped = 0_u64;
    let mut chunk_sha256 = BTreeMap::new();

    loop {
        let Some(read_limit) = bounded_dump_chunk_limit(table_row_count, rows_dumped, chunk_size)
        else {
            break;
        };
        chunks_dumped += 1;
        let chunk_name = dump_chunk_name(chunks_dumped, data_format, compression);
        let chunk_path = table_dir.join(&chunk_name);

        let stream_started = Instant::now();
        let last_values = last_key.as_deref().and_then(decode_key_token);
        let sql = if use_keyset {
            select_chunk_text_after_key_sql(
                "mysql",
                table,
                &key_columns,
                last_values.as_deref(),
                read_limit,
            )
        } else {
            select_chunk_text_sql("mysql", table, &key_columns)
        };
        let sql = if use_keyset {
            sql
        } else {
            sql.replacen('?', &(read_limit as u64).to_string(), 1)
                .replacen('?', &(offset as u64).to_string(), 1)
        };
        let result = if use_keyset {
            conn.query_iter(sql)
                .map_err(|err| format!("mysql keyset select chunk error: {err}"))?
        } else {
            conn.query_iter(sql)
                .map_err(|err| format!("mysql select chunk error: {err}"))?
        };

        let mut chunk_rows = 0_usize;
        let mut next_key: Option<String> = None;
        {
            let mut file = open_dump_writer(&chunk_path, compression)?;
            for row in result {
                let row = row.map_err(|err| format!("mysql dump row error: {err}"))?;
                if data_format == "tsv" && !use_keyset {
                    write_mysql_text_row_tsv(&mut file, row)?;
                } else {
                    let row_json = mysql_row_to_json(&columns, row);
                    if use_keyset {
                        next_key = row_key_token(&row_json, &key_columns);
                    }
                    write_dump_row(&mut file, table, &row_json, data_format)?;
                }
                chunk_rows += 1;
            }
        }
        let stream_ms = stream_started.elapsed().as_millis() as u64;

        if chunk_rows == 0 {
            fs::remove_file(&chunk_path).ok();
            chunks_dumped -= 1;
            break;
        }
        let checksum = sha256_file(&chunk_path)?;
        chunk_sha256.insert(chunk_name, checksum);

        rows_dumped += chunk_rows as u64;
        if use_keyset {
            last_key = next_key;
        } else {
            offset += chunk_rows;
        }

        emit(json!({
            "event": "row_progress",
            "request_id": request_id,
            "table": table.name,
            "rows": rows_dumped,
            "total": table_row_count,
            "chunk_rows": chunk_rows,
            "stream_ms": stream_ms
        }));
    }

    emit(json!({
        "event": "table_progress",
        "request_id": request_id,
        "table": table.name,
        "status": "completed",
        "current": index + 1,
        "total": table_total
    }));

    Ok((
        DumpTableManifest {
            name: table.name.clone(),
            path: table_path,
            rows: rows_dumped,
            chunks: chunks_dumped,
            chunk_sha256,
        },
        rows_dumped,
        chunks_dumped,
    ))
}

fn dump_import_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump_import",
        "message": "dump import started"
    }));

    match dump_import(request, |event| emit(event)) {
        Ok(result) => emit(result),
        Err(err) => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
}

fn dump_import<F: FnMut(Value)>(request: &Request, mut emit: F) -> Result<Value, String> {
    let endpoint = request_endpoint(request)?;
    let input_dir = request
        .payload
        .get("input_dir")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "dump.import requires input_dir".to_string())?;
    let mode = request
        .payload
        .get("mode")
        .or_else(|| request.payload.get("import_mode"))
        .and_then(Value::as_str)
        .unwrap_or("replace");
    if !matches!(mode, "replace" | "merge" | "recreate") {
        return Err(format!("unsupported dump import mode: {mode}"));
    }

    let input_path = Path::new(input_dir);
    let manifest = read_dump_manifest(input_path)?;
    if manifest.format != "tunnelforge-dump" || !matches!(manifest.format_version, 1 | 2) {
        return Err("unsupported dump manifest format".to_string());
    }
    let data_format = manifest.data_format.to_ascii_lowercase();
    if !matches!(data_format.as_str(), "jsonl" | "tsv") {
        return Err(format!("unsupported dump data_format: {data_format}"));
    }
    let compression = manifest.compression.to_ascii_lowercase();
    if !matches!(compression.as_str(), "none" | "zstd") {
        return Err(format!("unsupported dump compression: {compression}"));
    }

    let selected_tables = string_list(request.payload.get("tables"));
    let selected: BTreeSet<String> = selected_tables.into_iter().collect();
    let threads = request
        .payload
        .get("threads")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or(8)
        .max(1);
    let mysql_local_infile_policy = mysql_local_infile_policy_from_payload(&request.payload)?;
    let timezone_sql =
        validated_timezone_sql(request.payload.get("timezone_sql").and_then(Value::as_str))?;
    let tables: Vec<DumpTableManifest> = manifest
        .tables
        .iter()
        .filter(|table| selected.is_empty() || selected.contains(&table.name))
        .cloned()
        .collect();
    if tables.is_empty() {
        return Err("dump.import found no tables to import".to_string());
    }

    let strict_manifest = request
        .payload
        .get("strict_manifest")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let manifest_warnings = validate_dump_import_manifest_strictness(&tables, strict_manifest)?;
    let tables = dependency_ordered_dump_tables(&manifest.schema, tables);
    validate_dump_manifest_chunks(input_path, &tables, &data_format, &compression)?;
    for warning in &manifest_warnings {
        emit(json!({
            "event": "warning",
            "request_id": request.request_id,
            "phase": "dump_import_manifest",
            "classification": "legacy_dump",
            "message": warning
        }));
    }
    let mut adapter = LiveAdapter::connect(&endpoint)?;
    if let Some(sql) = timezone_sql.as_deref() {
        adapter.execute_sql(sql)?;
    }
    let local_infile_restore = prepare_mysql_local_infile_policy(
        &mut adapter,
        &endpoint,
        mysql_local_infile_policy,
        request.request_id.clone(),
        &mut emit,
    )?;
    let table_total = tables.len();
    let overall_rows_total = tables.iter().map(|table| table.rows).sum::<u64>();
    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;
    let mut imported_rows_by_table: BTreeMap<String, u64> = BTreeMap::new();

    set_mysql_import_session_tuning(&mut adapter, false)?;
    let import_result = (|| -> Result<(), String> {
        for (index, table_manifest) in tables.iter().enumerate() {
            let table = manifest
                .schema
                .tables
                .iter()
                .find(|table| table.name == table_manifest.name)
                .ok_or_else(|| format!("manifest schema missing table {}", table_manifest.name))?;
            emit(json!({
                "event": "table_progress",
                "request_id": request.request_id,
                "table": table.name,
                "status": "importing",
                "current": index + 1,
                "total": table_total
            }));
            let table_rows_before = rows_imported;

            if matches!(mode, "replace" | "recreate") {
                adapter
                    .execute_sql(&drop_table_sql(adapter.engine(), &table.name))
                    .map_err(|err| dump_import_ddl_error("drop_table", &table.name, &err))?;
            }
            let ddl = generate_table_ddl(table, &manifest.source_engine, adapter.engine())
                .ok_or_else(|| format!("cannot generate DDL for table {}", table.name))?;
            adapter
                .create_table(table, &ddl)
                .map_err(|err| dump_import_ddl_error("create_table", &table.name, &err))?;

            if data_format == "tsv" && !has_binary_columns(table) {
                if let LiveAdapter::MySql(conn) = &mut adapter {
                    let (rows, chunks) = import_mysql_tsv_table(
                        &endpoint,
                        conn,
                        input_path,
                        table,
                        table_manifest,
                        &compression,
                        threads,
                        request.request_id.clone(),
                        rows_imported,
                        overall_rows_total,
                        |event| emit(event),
                    )?;
                    rows_imported += rows;
                    chunks_imported += chunks;
                    imported_rows_by_table.insert(table.name.clone(), rows);
                    emit(json!({
                        "event": "table_progress",
                        "request_id": request.request_id,
                        "table": table.name,
                        "status": "completed",
                        "current": index + 1,
                        "total": table_total
                    }));
                    continue;
                }
            }

            for chunk_index in 1..=table_manifest.chunks {
                let chunk_path = dump_manifest_chunk_path(
                    input_path,
                    &table_manifest.path,
                    chunk_index,
                    &data_format,
                    &compression,
                )?;
                let rows = read_dump_rows(&chunk_path, table, &data_format, &compression)?;
                let row_count = rows.len();
                adapter.insert_rows(table, rows)?;
                rows_imported += row_count as u64;
                chunks_imported += 1;
                let table_rows_done = rows_imported.saturating_sub(table_rows_before);
                emit(dump_import_row_progress_event(
                    request.request_id.clone(),
                    &table.name,
                    table_rows_done,
                    table_manifest.rows,
                    table_rows_before,
                    overall_rows_total,
                    row_count as u64,
                    Some(chunk_index),
                    Some(table_manifest.chunks),
                    Some(chunk_index),
                    None,
                    "insert_rows",
                ));
            }

            let table_rows_imported = rows_imported.saturating_sub(table_rows_before);
            imported_rows_by_table.insert(table.name.clone(), table_rows_imported);
            emit(json!({
                "event": "table_progress",
                "request_id": request.request_id,
                "table": table.name,
                "status": "completed",
                "current": index + 1,
                "total": table_total
            }));
        }
        Ok(())
    })();
    let restore_result = set_mysql_import_session_tuning(&mut adapter, true);
    let local_infile_restore_result = restore_mysql_local_infile_policy(
        &mut adapter,
        local_infile_restore,
        request.request_id.clone(),
        &mut emit,
    );
    import_result?;
    restore_result?;
    local_infile_restore_result?;
    let target_engine = adapter.engine().to_string();
    if should_apply_post_load_ddl(mode) {
        emit(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "dump_import_post_load",
            "message": "현재 단계: 인덱스/FK 생성 중 - 데이터 Import는 완료, 후처리 진행 중",
            "strategy": "post_load_ddl"
        }));
        apply_post_load_ddl(&mut adapter, &manifest.schema, &target_engine)?;
    } else {
        emit(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "dump_import_post_load",
            "message": post_load_ddl_skip_message(mode),
            "strategy": "existing_schema"
        }));
    }
    verify_imported_row_counts(&tables, &imported_rows_by_table)?;
    verify_target_row_counts(&mut adapter, &tables, mode)?;

    // View 생성 (best-effort). 데이터는 이미 커밋되었으므로 View 실패가 전체 import를 무효화하지 않는다.
    // 전체 import(테이블 부분 선택 없음)일 때만 시도한다 — 부분 import면 View가 참조하는 base table이 없을 수 있다.
    let view_outcome = if selected.is_empty() && !manifest.views.is_empty() {
        import_views(
            &mut adapter,
            &manifest,
            &target_engine,
            mode,
            request.request_id.clone(),
            &mut emit,
        )
    } else {
        ViewImportOutcome::default()
    };
    let import_report = json!({
        "success": true,
        "mode": mode,
        "tables": table_total,
        "rows_imported": rows_imported,
        "chunks_imported": chunks_imported,
        "imported_rows_by_table": imported_rows_by_table,
        "verification": {
            "row_counts": "passed",
            "strict_manifest": strict_manifest,
            "warnings": manifest_warnings
        },
        "views_imported": view_outcome.imported,
        "views_failed": view_outcome.failed,
        "views_skipped_cross_engine": view_outcome.skipped_cross_engine
    });
    write_dump_import_report(input_path, &import_report)?;
    let import_report_path = dump_import_report_path(input_path)?;

    Ok(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "dump.import",
        "success": true,
        "input_dir": input_dir,
        "mode": mode,
        "tables": table_total,
        "rows_imported": rows_imported,
        "chunks_imported": chunks_imported,
        "verification": import_report["verification"].clone(),
        "import_report": import_report_path.display().to_string(),
        "views_imported": import_report["views_imported"].clone(),
        "views_failed": import_report["views_failed"].clone(),
        "views_skipped_cross_engine": import_report["views_skipped_cross_engine"].clone()
    }))
}

#[derive(Debug, Default)]
struct ViewImportOutcome {
    imported: Vec<String>,
    failed: Vec<Value>,
    skipped_cross_engine: Vec<String>,
}

/// manifest의 View들을 대상 DB에 생성한다.
/// - source/target 엔진이 다르면 정의 SQL이 호환되지 않으므로 전부 skip.
/// - View 간 의존성 순서 문제를 fixpoint 재시도 루프로 해결한다.
/// - 각 View 실패는 non-fatal: 결과에 모아 보고만 한다.
fn import_views<A: MigrationAdapter, F: FnMut(Value)>(
    adapter: &mut A,
    manifest: &DumpManifest,
    target_engine: &str,
    mode: &str,
    request_id: Option<String>,
    mut emit: F,
) -> ViewImportOutcome {
    let mut outcome = ViewImportOutcome::default();

    if manifest.source_engine != target_engine {
        outcome.skipped_cross_engine = manifest.views.iter().map(|v| v.name.clone()).collect();
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": format!(
                "크로스 엔진 import: View {}개는 정의 비호환으로 건너뜁니다 ({} -> {})",
                outcome.skipped_cross_engine.len(),
                manifest.source_engine,
                target_engine
            ),
        }));
        return outcome;
    }

    // 정화 + 단일 CREATE VIEW 문 검증. 검증 실패한 정의는 실행하지 않고 즉시 failed로 보고한다.
    // (변조된 manifest가 multi-statement SQL 체인을 심는 것을 차단 — 특히 PostgreSQL batch_execute 경로)
    let mut pending: Vec<(String, String)> = Vec::with_capacity(manifest.views.len());
    let mut validated_names: Vec<&str> = Vec::with_capacity(manifest.views.len());
    for view in &manifest.views {
        let sanitized =
            sanitize_view_definition(&view.definition, &manifest.database, target_engine);
        // shape 검증(단일 CREATE ... VIEW 문) + MySQL DEFINER/SQL SECURITY 잔존 fail-closed.
        let validation = validate_single_view_statement(&sanitized).and_then(|()| {
            if target_engine == "mysql" && mysql_definition_has_residual_definer(&sanitized) {
                Err("residual DEFINER/SQL SECURITY DEFINER clause after sanitization".to_string())
            } else {
                Ok(())
            }
        });
        match validation {
            Ok(()) => {
                validated_names.push(&view.name);
                pending.push((view.name.clone(), sanitized));
            }
            Err(reason) => {
                outcome
                    .failed
                    .push(json!({ "name": view.name, "error": format!("rejected: {reason}") }));
                emit(json!({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "dump_import",
                    "message": format!("View '{}' 거부됨 (안전하지 않은 정의): {reason}", view.name),
                }));
            }
        }
    }

    // replace/recreate 모드면 기존 View를 먼저 정리한다 (테이블이 아닌 View 전용 DROP).
    // 검증을 통과한 View만 DROP 대상으로 삼는다.
    if matches!(mode, "replace" | "recreate") {
        for name in &validated_names {
            let _ = adapter.execute_sql(&drop_view_sql(target_engine, name));
        }
    }

    // fixpoint 루프: 한 바퀴에 하나도 성공하지 못하면 중단한다.
    let mut last_errors: BTreeMap<String, String> = BTreeMap::new();
    loop {
        let mut progressed = false;
        let mut still_pending: Vec<(String, String)> = Vec::new();
        for (name, sql) in pending.drain(..) {
            match adapter.execute_sql(&sql) {
                Ok(()) => {
                    progressed = true;
                    last_errors.remove(&name);
                    outcome.imported.push(name.clone());
                    emit(json!({
                        "event": "table_progress",
                        "request_id": request_id,
                        "table": name,
                        "status": "completed",
                        "kind": "view"
                    }));
                }
                Err(err) => {
                    last_errors.insert(name.clone(), err);
                    still_pending.push((name, sql));
                }
            }
        }
        pending = still_pending;
        if pending.is_empty() || !progressed {
            break;
        }
    }

    for (name, _sql) in pending {
        let error = last_errors
            .get(&name)
            .cloned()
            .unwrap_or_else(|| "unknown error".to_string());
        outcome.failed.push(json!({ "name": name, "error": error }));
    }

    if !outcome.failed.is_empty() {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": format!(
                "View {}개 생성 성공, {}개 실패 (데이터 import는 정상 완료)",
                outcome.imported.len(),
                outcome.failed.len()
            ),
        }));
    }

    outcome
}

fn import_mysql_tsv_table<F: FnMut(Value)>(
    endpoint: &Endpoint,
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    table: &NormalizedTable,
    table_manifest: &DumpTableManifest,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
    mut emit: F,
) -> Result<(u64, u64), String> {
    if !mysql_local_infile_enabled(conn) {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile is disabled; using safe Rust INSERT fallback",
            "strategy": "insert_fallback",
            "performance": "safe_fallback"
        }));
        return import_mysql_tsv_table_insert_fallback(
            conn,
            input_path,
            table,
            table_manifest,
            compression,
            request_id,
            overall_rows_before,
            overall_rows_total,
            emit,
        );
    }

    if threads > 1 && table_manifest.chunks > 1 {
        let result = import_mysql_tsv_table_parallel(
            endpoint,
            input_path,
            table,
            table_manifest,
            compression,
            threads,
            request_id.clone(),
            overall_rows_before,
            overall_rows_total,
            |event| emit(event),
        );
        return match result {
            Ok(result) => Ok(result),
            Err(err) if is_mysql_local_infile_disabled_error(&err) => {
                emit(json!({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "dump_import",
                    "message": "MySQL LOAD DATA LOCAL is disabled; using safe Rust INSERT fallback",
                    "strategy": "insert_fallback",
                    "performance": "safe_fallback"
                }));
                import_mysql_tsv_table_insert_fallback(
                    conn,
                    input_path,
                    table,
                    table_manifest,
                    compression,
                    request_id,
                    overall_rows_before,
                    overall_rows_total,
                    emit,
                )
            }
            Err(err) => Err(err),
        };
    }

    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;
    for chunk_index in 1..=table_manifest.chunks {
        let chunk_path = dump_manifest_chunk_path(
            input_path,
            &table_manifest.path,
            chunk_index,
            "tsv",
            compression,
        )?;
        let started = Instant::now();
        let rows = match load_mysql_tsv_chunk(conn, table, &chunk_path, compression) {
            Ok(rows) => rows,
            Err(err) if is_mysql_local_infile_disabled_error(&err) => {
                emit(json!({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "dump_import",
                    "message": "MySQL LOAD DATA LOCAL is disabled; using safe Rust INSERT fallback",
                    "strategy": "insert_fallback",
                    "performance": "safe_fallback"
                }));
                return import_mysql_tsv_table_insert_fallback(
                    conn,
                    input_path,
                    table,
                    table_manifest,
                    compression,
                    request_id,
                    overall_rows_before,
                    overall_rows_total,
                    emit,
                );
            }
            Err(err) => return Err(err),
        };
        rows_imported += rows;
        chunks_imported += 1;
        emit(dump_import_row_progress_event(
            request_id.clone(),
            &table.name,
            rows_imported,
            table_manifest.rows,
            overall_rows_before,
            overall_rows_total,
            rows,
            Some(chunks_imported),
            Some(table_manifest.chunks),
            Some(chunk_index),
            Some(started.elapsed().as_millis() as u64),
            "load_data_local_infile",
        ));
    }
    Ok((rows_imported, chunks_imported))
}

fn mysql_local_infile_enabled(conn: &mut mysql::PooledConn) -> bool {
    mysql_local_infile_value(conn)
        .map(|value| mysql_bool_value_enabled(&value))
        .unwrap_or(true)
}

fn mysql_local_infile_value(conn: &mut mysql::PooledConn) -> Option<String> {
    conn.query_first::<(String, String), _>("SHOW VARIABLES LIKE 'local_infile'")
        .ok()
        .flatten()
        .map(|(_, value)| value)
}

fn mysql_bool_value_enabled(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "on" | "1" | "true" | "yes"
    )
}

fn mysql_set_global_local_infile_sql(enabled: bool) -> &'static str {
    if enabled {
        "SET GLOBAL local_infile = 1"
    } else {
        "SET GLOBAL local_infile = 0"
    }
}

fn mysql_local_infile_policy_from_payload(payload: &Value) -> Result<&str, String> {
    let policy = payload
        .get("mysql_local_infile_policy")
        .and_then(Value::as_str)
        .unwrap_or("fallback");
    if matches!(policy, "fallback" | "temporary_global") {
        Ok(policy)
    } else {
        Err(format!("unsupported mysql_local_infile_policy: {policy}"))
    }
}

fn prepare_mysql_local_infile_policy<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    endpoint: &Endpoint,
    policy: &str,
    request_id: Option<String>,
    emit: &mut F,
) -> Result<Option<String>, String> {
    if policy != "temporary_global" {
        return Ok(None);
    }
    let previous = {
        let LiveAdapter::MySql(conn) = adapter else {
            return Ok(None);
        };
        let previous = mysql_local_infile_value(conn).unwrap_or_else(|| "ON".to_string());
        if mysql_bool_value_enabled(&previous) {
            emit(json!({
                "event": "phase",
                "request_id": request_id,
                "phase": "dump_import",
                "message": "MySQL local_infile is already enabled; using fast LOAD DATA LOCAL import",
                "strategy": "load_data_local_infile",
                "performance": "fast_path"
            }));
            return Ok(None);
        }

        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile is disabled; trying temporary SET GLOBAL local_infile=ON",
            "strategy": "temporary_local_infile",
            "performance": "fast_path_attempt"
        }));

        if let Err(err) = conn.query_drop(mysql_set_global_local_infile_sql(true)) {
            emit(json!({
                "event": "phase",
                "request_id": request_id,
                "phase": "dump_import",
                "message": format!("MySQL local_infile temporary enable failed: {err}; using safe Rust INSERT fallback"),
                "strategy": "insert_fallback",
                "performance": "safe_fallback"
            }));
            return Ok(None);
        }
        previous
    };

    if let Err(err) = LiveAdapter::connect(endpoint).map(|new_adapter| *adapter = new_adapter) {
        if let LiveAdapter::MySql(conn) = adapter {
            let _ = conn.query_drop(mysql_set_global_local_infile_sql(mysql_bool_value_enabled(
                &previous,
            )));
        }
        return Err(err);
    }
    let enabled = match adapter {
        LiveAdapter::MySql(conn) => mysql_local_infile_enabled(conn),
        LiveAdapter::PostgreSql(_) => false,
    };
    if enabled {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile temporarily enabled; using fast LOAD DATA LOCAL import",
            "strategy": "load_data_local_infile",
            "performance": "fast_path"
        }));
    } else {
        emit(json!({
            "event": "phase",
            "request_id": request_id,
            "phase": "dump_import",
            "message": "MySQL local_infile temporary enable did not take effect; using safe Rust INSERT fallback",
            "strategy": "insert_fallback",
            "performance": "safe_fallback"
        }));
    }
    Ok(Some(previous))
}

fn restore_mysql_local_infile_policy<F: FnMut(Value)>(
    adapter: &mut LiveAdapter,
    previous: Option<String>,
    request_id: Option<String>,
    emit: &mut F,
) -> Result<(), String> {
    let Some(previous) = previous else {
        return Ok(());
    };
    let enabled = mysql_bool_value_enabled(&previous);
    let LiveAdapter::MySql(conn) = adapter else {
        return Ok(());
    };
    conn.query_drop(mysql_set_global_local_infile_sql(enabled))
        .map_err(|err| {
            format!("mysql local_infile restore failed; previous value was {previous}: {err}")
        })?;
    emit(json!({
        "event": "phase",
        "request_id": request_id,
        "phase": "dump_import",
        "message": format!("MySQL local_infile restored to {previous}"),
        "strategy": "temporary_local_infile_restore"
    }));
    Ok(())
}

fn is_mysql_local_infile_disabled_error(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("3948")
        || lower.contains("loading local data is disabled")
        || lower.contains("local infile")
            && (lower.contains("disabled") || lower.contains("not allowed"))
}

fn validated_timezone_sql(value: Option<&str>) -> Result<Option<String>, String> {
    let Some(sql) = value.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let normalized = sql.to_ascii_lowercase();
    if normalized.contains(';')
        || normalized.contains("--")
        || normalized.contains("/*")
        || normalized.contains("*/")
        || normalized.contains('\0')
    {
        return Err(
            "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone is allowed"
                .to_string(),
        );
    }

    let Some(after_set) = normalized.strip_prefix("set") else {
        return Err(
            "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone is allowed"
                .to_string(),
        );
    };
    let Some(after_session) = after_set.trim_start().strip_prefix("session") else {
        return Err(
            "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone is allowed"
                .to_string(),
        );
    };
    let Some(after_variable) = after_session.trim_start().strip_prefix("time_zone") else {
        return Err(
            "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone is allowed"
                .to_string(),
        );
    };
    let Some(value) = after_variable.trim_start().strip_prefix('=') else {
        return Err(
            "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone is allowed"
                .to_string(),
        );
    };
    let value = value.trim();
    if value.is_empty() || !is_safe_timezone_literal(value) {
        return Err(
            "import_plan_invalid: unsupported timezone_sql; only SET SESSION time_zone is allowed"
                .to_string(),
        );
    }

    Ok(Some(sql.to_string()))
}

fn is_safe_timezone_literal(value: &str) -> bool {
    let value = if value.starts_with('\'') && value.ends_with('\'') && value.len() >= 2 {
        &value[1..value.len() - 1]
    } else {
        value
    };
    !value.is_empty()
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '+' | '-' | '_' | ':' | '/'))
}

fn mysql_import_session_tuning_sql(restore: bool) -> Vec<String> {
    if restore {
        vec![
            "SET SESSION sql_mode=DEFAULT".to_string(),
            "SET SESSION unique_checks=1".to_string(),
            "SET SESSION foreign_key_checks=1".to_string(),
        ]
    } else {
        vec![
            "SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', ''), 'NO_ZERO_IN_DATE', ''), 'NO_ZERO_DATE', ''), 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','), ',,', ','))".to_string(),
            "SET SESSION foreign_key_checks=0".to_string(),
            "SET SESSION unique_checks=0".to_string(),
        ]
    }
}

fn set_mysql_import_session_tuning(adapter: &mut LiveAdapter, restore: bool) -> Result<(), String> {
    if !matches!(adapter, LiveAdapter::MySql(_)) {
        return Ok(());
    }
    for sql in mysql_import_session_tuning_sql(restore) {
        adapter.execute_sql(&sql)?;
    }
    Ok(())
}

fn import_mysql_tsv_table_insert_fallback<F: FnMut(Value)>(
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    table: &NormalizedTable,
    table_manifest: &DumpTableManifest,
    compression: &str,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
    mut emit: F,
) -> Result<(u64, u64), String> {
    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;
    for chunk_index in 1..=table_manifest.chunks {
        let chunk_path = dump_manifest_chunk_path(
            input_path,
            &table_manifest.path,
            chunk_index,
            "tsv",
            compression,
        )?;
        let started = Instant::now();
        let rows = insert_mysql_tsv_chunk_with_batches(conn, table, &chunk_path, compression)
            .map_err(|err| {
                format!(
                    "mysql insert fallback error for table {} chunk {}: {err}",
                    table.name, chunk_index
                )
            })?;
        rows_imported += rows;
        chunks_imported += 1;
        emit(dump_import_row_progress_event(
            request_id.clone(),
            &table.name,
            rows_imported,
            table_manifest.rows,
            overall_rows_before,
            overall_rows_total,
            rows,
            Some(chunks_imported),
            Some(table_manifest.chunks),
            Some(chunk_index),
            Some(started.elapsed().as_millis() as u64),
            "insert_fallback",
        ));
    }
    Ok((rows_imported, chunks_imported))
}

fn insert_mysql_tsv_chunk_with_batches(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    chunk_path: &Path,
    compression: &str,
) -> Result<u64, String> {
    stream_tsv_rows_in_batches(
        chunk_path,
        table,
        compression,
        MYSQL_INSERT_FALLBACK_BATCH_ROWS,
        MYSQL_INSERT_FALLBACK_BATCH_BYTES,
        |rows| {
            conn.query_drop(insert_rows_literal_sql_for_table("mysql", table, rows))
                .map_err(|err| err.to_string())
        },
    )
}

fn import_mysql_tsv_table_parallel<F: FnMut(Value)>(
    endpoint: &Endpoint,
    input_path: &Path,
    table: &NormalizedTable,
    table_manifest: &DumpTableManifest,
    compression: &str,
    threads: usize,
    request_id: Option<String>,
    overall_rows_before: u64,
    overall_rows_total: u64,
    mut emit: F,
) -> Result<(u64, u64), String> {
    let max_threads = threads.max(1).min(table_manifest.chunks as usize);
    let mut pending = adaptive_import_chunk_order(input_path, table_manifest, "tsv", compression);
    let mut active = 0_usize;
    let mut completed = 0_u64;
    let mut rows_imported = 0_u64;
    let mut first_error: Option<String> = None;
    let mut handles = Vec::new();
    let (sender, receiver) = mpsc::channel::<ImportChunkEvent>();

    while active < max_threads {
        if let Some(chunk_index) = pending.pop_front() {
            handles.push(spawn_mysql_import_chunk_worker(
                endpoint.clone(),
                input_path.to_path_buf(),
                table.clone(),
                table_manifest.path.clone(),
                chunk_index,
                compression.to_string(),
                sender.clone(),
            ));
            active += 1;
        } else {
            break;
        }
    }

    while completed < table_manifest.chunks && active > 0 {
        match receiver.recv() {
            Ok(ImportChunkEvent::Done {
                chunk_index,
                rows,
                load_ms,
            }) => {
                rows_imported += rows;
                completed += 1;
                active = active.saturating_sub(1);
                emit(dump_import_row_progress_event(
                    request_id.clone(),
                    &table.name,
                    rows_imported,
                    table_manifest.rows,
                    overall_rows_before,
                    overall_rows_total,
                    rows,
                    Some(completed),
                    Some(table_manifest.chunks),
                    Some(chunk_index),
                    Some(load_ms),
                    "parallel_load_data_local_infile",
                ));
                if let Some(next_chunk) = pending.pop_front() {
                    handles.push(spawn_mysql_import_chunk_worker(
                        endpoint.clone(),
                        input_path.to_path_buf(),
                        table.clone(),
                        table_manifest.path.clone(),
                        next_chunk,
                        compression.to_string(),
                        sender.clone(),
                    ));
                    active += 1;
                }
            }
            Ok(ImportChunkEvent::Error(err)) => {
                first_error.get_or_insert(err);
                completed += 1;
                active = active.saturating_sub(1);
            }
            Err(_) => break,
        }
    }

    for handle in handles {
        let _ = handle.join();
    }
    if let Some(err) = first_error {
        return Err(err);
    }
    Ok((rows_imported, completed))
}

fn adaptive_import_chunk_order(
    input_path: &Path,
    table_manifest: &DumpTableManifest,
    data_format: &str,
    compression: &str,
) -> VecDeque<u64> {
    let mut chunks = (1..=table_manifest.chunks)
        .map(|chunk_index| {
            let path = dump_manifest_chunk_path(
                input_path,
                &table_manifest.path,
                chunk_index,
                data_format,
                compression,
            );
            let bytes = path
                .ok()
                .and_then(|path| fs::metadata(path).ok())
                .map(|metadata| metadata.len())
                .unwrap_or(0);
            (chunk_index, bytes)
        })
        .collect::<Vec<_>>();
    chunks.sort_by(|(left_index, left_bytes), (right_index, right_bytes)| {
        right_bytes
            .cmp(left_bytes)
            .then_with(|| left_index.cmp(right_index))
    });
    chunks
        .into_iter()
        .map(|(chunk_index, _)| chunk_index)
        .collect()
}

fn spawn_mysql_import_chunk_worker(
    endpoint: Endpoint,
    input_path: std::path::PathBuf,
    table: NormalizedTable,
    table_path: String,
    chunk_index: u64,
    compression: String,
    sender: mpsc::Sender<ImportChunkEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let result = (|| {
            let mut conn = match LiveAdapter::connect(&endpoint)? {
                LiveAdapter::MySql(conn) => conn,
                LiveAdapter::PostgreSql(_) => {
                    return Err("mysql TSV import requires mysql endpoint".to_string())
                }
            };
            let chunk_path = dump_manifest_chunk_path(
                &input_path,
                &table_path,
                chunk_index,
                "tsv",
                &compression,
            )?;
            let started = Instant::now();
            let rows = load_mysql_tsv_chunk(&mut conn, &table, &chunk_path, &compression)?;
            Ok((rows, started.elapsed().as_millis() as u64))
        })();
        match result {
            Ok((rows, load_ms)) => {
                let _ = sender.send(ImportChunkEvent::Done {
                    chunk_index,
                    rows,
                    load_ms,
                });
            }
            Err(err) => {
                let _ = sender.send(ImportChunkEvent::Error(err));
            }
        }
    })
}

fn load_mysql_tsv_chunk(
    conn: &mut mysql::PooledConn,
    table: &NormalizedTable,
    chunk_path: &Path,
    compression: &str,
) -> Result<u64, String> {
    let path = chunk_path.to_path_buf();
    let compression = compression.to_string();
    conn.set_local_infile_handler(Some(LocalInfileHandler::new(move |_, stream| {
        let mut reader = open_dump_reader(&path, &compression)
            .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err))?;
        std::io::copy(&mut reader, stream)?;
        Ok(())
    })));
    let sql = load_data_local_infile_sql("mysql", table, "tunnelforge_chunk");
    let result = conn
        .query_drop(sql)
        .map(|_| conn.affected_rows())
        .map_err(|err| format!("mysql LOAD DATA error: {err}"));
    conn.set_local_infile_handler(None);
    result
}

pub fn load_data_local_infile_sql(
    engine: &str,
    table: &NormalizedTable,
    file_name: &str,
) -> String {
    let columns = column_names(table)
        .iter()
        .map(|column| quote_ident(engine, column))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "LOAD DATA LOCAL INFILE {} INTO TABLE {} CHARACTER SET utf8mb4 FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' ({})",
        sql_literal(&Value::String(file_name.to_string())),
        quote_ident(engine, &table.name),
        columns
    )
}

fn request_endpoint(request: &Request) -> Result<Endpoint, String> {
    for key in ["connection", "endpoint", "source", "target"] {
        if let Some(value) = request.payload.get(key) {
            return endpoint_from_value(value);
        }
    }
    endpoint_from_value(&request.payload)
}

fn query_params(payload: &Value) -> Vec<Value> {
    payload
        .get("params")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn bind_query_params(sql: &str, params: &[Value]) -> String {
    if params.is_empty() {
        return sql.to_string();
    }
    let mut rendered = sql.to_string();
    for (index, value) in params.iter().enumerate() {
        let literal = sql_json_literal(value);
        rendered = rendered.replacen("%s", &literal, 1);
        rendered = rendered.replace(&format!("${}", index + 1), &literal);
    }
    rendered
}

fn sql_json_literal(value: &Value) -> String {
    match value {
        Value::Null => "NULL".to_string(),
        Value::Bool(item) => {
            if *item {
                "TRUE".to_string()
            } else {
                "FALSE".to_string()
            }
        }
        Value::Number(item) => item.to_string(),
        Value::String(item) => format!("'{}'", item.replace('\\', "\\\\").replace('\'', "''")),
        other => format!(
            "'{}'",
            other.to_string().replace('\\', "\\\\").replace('\'', "''")
        ),
    }
}

fn connection_id(endpoint: &Endpoint) -> String {
    let mut hasher = Sha256::new();
    hasher.update(endpoint.engine.as_bytes());
    hasher.update(endpoint.host.as_bytes());
    hasher.update(endpoint.port.to_string().as_bytes());
    hasher.update(endpoint.user.as_bytes());
    hasher.update(endpoint.database.as_bytes());
    hasher.update(endpoint_schema(endpoint).as_bytes());
    format!("conn-{}", hex::encode(&hasher.finalize()[..8]))
}

fn unique_connection_id(endpoint: &Endpoint, sequence: u64) -> String {
    format!("{}-{}", connection_id(endpoint), sequence)
}

fn redact_endpoint_secret(message: &str, endpoint: &Endpoint) -> String {
    if endpoint.password.is_empty() {
        message.to_string()
    } else {
        message.replace(&endpoint.password, "***")
    }
}

fn execute_query_live(endpoint: &Endpoint, sql: &str) -> Result<Vec<Value>, String> {
    let mut adapter = LiveAdapter::connect(endpoint)?;
    execute_query_adapter(&mut adapter, sql)
}

fn execute_query_adapter(adapter: &mut LiveAdapter, sql: &str) -> Result<Vec<Value>, String> {
    let returns_rows = query_returns_rows(sql);
    match adapter {
        LiveAdapter::MySql(conn) => {
            if !returns_rows {
                conn.query_drop(sql)
                    .map_err(|err| format!("mysql SQL execution error: {err}"))?;
                return Ok(Vec::new());
            }
            let rows: Vec<mysql::Row> = conn
                .query(sql)
                .map_err(|err| format!("mysql query error: {err}"))?;
            let columns: Vec<String> = rows
                .first()
                .map(|row| {
                    row.columns_ref()
                        .iter()
                        .map(|column| column.name_str().to_string())
                        .collect()
                })
                .unwrap_or_default();
            Ok(rows
                .into_iter()
                .map(|row| mysql_row_to_json(&columns, row))
                .collect())
        }
        LiveAdapter::PostgreSql(client) => {
            if !returns_rows {
                client
                    .batch_execute(sql)
                    .map_err(|err| format!("postgresql SQL execution error: {err}"))?;
                return Ok(Vec::new());
            }
            let trimmed = sql.trim().trim_end_matches(';');
            let wrapped = format!("SELECT row_to_json(_tf_row)::text FROM ({trimmed}) AS _tf_row");
            let rows = client
                .query(&wrapped, &[])
                .map_err(|err| format!("postgresql query error: {err}"))?;
            let mut values = Vec::new();
            for row in rows {
                let text: Option<String> = row.get(0);
                let value = text
                    .and_then(|item| serde_json::from_str::<Value>(&item).ok())
                    .unwrap_or(Value::Null);
                values.push(value);
            }
            Ok(values)
        }
    }
}

fn query_returns_rows(sql: &str) -> bool {
    let lower = sql.trim_start().to_ascii_lowercase();
    ["select", "with", "show", "desc", "describe", "explain"]
        .iter()
        .any(|prefix| lower.starts_with(prefix))
}

fn normalized_schema_diff(source: &NormalizedSchema, target: &NormalizedSchema) -> Vec<Value> {
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

fn inspect(request: &Request) -> Vec<Value> {
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
            Err(err) => events.push(json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })),
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
    let table_names: Vec<String> = conn
        .exec_map(
            inspect_tables_sql("mysql"),
            (&schema_name,),
            |table_name: String| table_name,
        )
        .map_err(|err| format!("mysql table inspect error: {err}"))?;
    let mut tables = Vec::new();

    for table_name in table_names {
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
fn collect_views(endpoint: &Endpoint) -> Result<Vec<NormalizedView>, String> {
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
fn sanitize_view_definition(definition: &str, source_schema: &str, engine: &str) -> String {
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

fn drop_view_sql(engine: &str, view: &str) -> String {
    format!("DROP VIEW IF EXISTS {}", quote_ident(engine, view))
}

/// sanitize 후에도 MySQL 정의에 `DEFINER=` 또는 `SQL SECURITY DEFINER`가 남아있는지 검사한다.
/// 정상 경로(`SHOW CREATE VIEW`의 대문자/단일공백 정규화 출력)는 sanitize가 모두 처리하므로
/// 여기서 잔존이 감지된다는 것은 탭/주석을 끼운 비정규(변조 의심) 정의라는 뜻 → fail-closed로 거부한다.
fn mysql_definition_has_residual_definer(sql: &str) -> bool {
    // 주석(-- 라인, /* */ 블록)을 공백으로 치환하고, 모든 공백류를 단일 공백으로 정규화한 검사용 사본.
    let mut cleaned = String::with_capacity(sql.len());
    let bytes = sql.as_bytes();
    let len = bytes.len();
    let mut i = 0;
    while i < len {
        if bytes[i] == b'-' && i + 1 < len && bytes[i + 1] == b'-' {
            i += 2;
            while i < len && bytes[i] != b'\n' {
                i += 1;
            }
            cleaned.push(' ');
        } else if bytes[i] == b'/' && i + 1 < len && bytes[i + 1] == b'*' {
            i += 2;
            while i + 1 < len && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                i += 1;
            }
            i += 2;
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
fn validate_single_view_statement(sql: &str) -> Result<(), String> {
    let trimmed = sql.trim();
    if trimmed.is_empty() {
        return Err("empty view definition".to_string());
    }

    // 문자열 리터럴('...'), 식별자 인용(`...` / "..."), 주석(-- , /* */) 바깥의 세미콜론을 찾는다.
    let bytes = trimmed.as_bytes();
    let mut i = 0;
    let len = bytes.len();
    while i < len {
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
            b'-' if i + 1 < len && bytes[i + 1] == b'-' => {
                // 라인 주석
                i += 2;
                while i < len && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            b'/' if i + 1 < len && bytes[i + 1] == b'*' => {
                // 블록 주석
                i += 2;
                while i + 1 < len && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i += 2;
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

fn preflight_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(
        request,
        "preflight",
        "preflight checks started",
    ));
    let mut issues = preflight_issues(&request.payload);
    emit(phase_event(
        request,
        "preflight",
        "schema compatibility checks completed",
    ));
    emit(phase_event(request, "preflight", "checking target state"));
    issues.extend(live_preflight_issues(&request.payload));
    emit(phase_event(
        request,
        "preflight",
        "target state checks completed",
    ));

    for issue in &issues {
        emit(json!({
            "event": "issue",
            "request_id": request.request_id,
            "issue": issue
        }));
    }

    emit(phase_event(request, "preflight", "preflight result ready"));
    emit(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "preflight",
        "success": !issues.iter().any(|issue| issue.blocking),
        "issues": issues
    }));
}

fn oneclick_run_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(
        request,
        "preflight",
        "one-click preflight started",
    ));
    emit(oneclick_progress_event(request, 5, "Pre-flight started"));
    let state = match oneclick_preflight_state(request) {
        Ok(state) => state,
        Err(err) => {
            emit(json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            }));
            return;
        }
    };
    emit(oneclick_preflight_event(request, &state));
    emit(oneclick_progress_event(request, 20, "Pre-flight completed"));
    if state.issues.iter().any(|issue| issue.blocking) {
        emit(oneclick_final_result(
            request,
            &state.schema_name,
            false,
            &state.issues,
            &state.issues,
            vec!["Pre-flight blocked execution.".to_string()],
        ));
        return;
    }

    emit(phase_event(
        request,
        "analysis",
        "one-click analysis started",
    ));
    let analysis = oneclick_analysis_summary(&state.inspection, &state.issues);
    emit(json!({
        "event": "analysis",
        "request_id": request.request_id,
        "summary": analysis
    }));
    emit(oneclick_progress_event(request, 40, "Analysis completed"));

    emit(phase_event(
        request,
        "recommendation",
        "one-click recommendations ready",
    ));
    let recommendations = oneclick_recommendations(&state.issues);
    let recommendation_summary = oneclick_recommendation_summary(&recommendations);
    emit(json!({
        "event": "execution_plan",
        "request_id": request.request_id,
        "steps": recommendations,
        "summary": recommendation_summary
    }));
    emit(oneclick_progress_event(
        request,
        55,
        "Recommendations completed",
    ));

    emit(phase_event(
        request,
        "execution",
        "one-click execution started",
    ));
    let dry_run = request
        .payload
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let execution_log = if dry_run {
        vec!["DRY-RUN: no database changes were executed.".to_string()]
    } else {
        vec!["No automatic Rust Core fixes are currently required.".to_string()]
    };
    emit(json!({
        "event": "execution",
        "request_id": request.request_id,
        "dry_run": dry_run,
        "success_count": 0,
        "fail_count": 0,
        "skip_count": state.issues.len(),
        "log": execution_log
    }));
    emit(oneclick_progress_event(request, 80, "Execution completed"));

    emit(phase_event(
        request,
        "validation",
        "one-click validation started",
    ));
    let validation_issues = match inspect_live(&state.endpoint) {
        Ok(inspection) => oneclick_issues_from_inspection(&inspection),
        Err(err) => vec![MigrationIssue {
            severity: "error".to_string(),
            location: "validation".to_string(),
            message: err,
            suggestion: "Check the database connection and rerun validation.".to_string(),
            blocking: true,
        }],
    };
    let validation_success = validation_issues.is_empty();
    emit(json!({
        "event": "validation",
        "request_id": request.request_id,
        "all_fixed": validation_success,
        "remaining_issues": validation_issues.clone()
    }));
    emit(oneclick_progress_event(
        request,
        100,
        "Validation completed",
    ));
    emit(oneclick_final_result(
        request,
        &state.schema_name,
        validation_success,
        &state.issues,
        &validation_issues,
        execution_log,
    ));
}

fn oneclick_preflight(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "preflight",
        "one-click preflight started",
    )];
    match oneclick_preflight_state(request) {
        Ok(state) => {
            events.push(oneclick_preflight_event(request, &state));
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.preflight",
                "success": !state.issues.iter().any(|issue| issue.blocking),
                "schema": state.schema_name,
                "checks": state.checks,
                "issues": state.issues
            }));
        }
        Err(err) => events.push(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
    events
}

fn oneclick_analyze(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "analysis",
        "one-click analysis started",
    )];
    match oneclick_preflight_state(request) {
        Ok(state) => {
            let summary = oneclick_analysis_summary(&state.inspection, &state.issues);
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.analyze",
                "success": true,
                "schema": state.schema_name,
                "summary": summary,
                "issues": state.issues
            }));
        }
        Err(err) => events.push(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
    events
}

fn oneclick_recommend(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "recommendation",
        "one-click recommendation started",
    )];
    let issues = oneclick_payload_issues(&request.payload);
    let recommendations = oneclick_recommendations(&issues);
    let summary = oneclick_recommendation_summary(&recommendations);
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.recommend",
        "success": true,
        "steps": recommendations,
        "summary": summary
    }));
    events
}

fn oneclick_apply_fixes(request: &Request) -> Vec<Value> {
    let dry_run = request
        .payload
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    vec![
        phase_event(request, "execution", "one-click apply fixes started"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "oneclick.apply_fixes",
            "success": true,
            "dry_run": dry_run,
            "success_count": 0,
            "fail_count": 0,
            "skip_count": request.payload.get("steps").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            "log": [if dry_run {
                "DRY-RUN: no database changes were executed."
            } else {
                "No automatic Rust Core fixes are currently required."
            }]
        }),
    ]
}

fn oneclick_validate(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "validation",
        "one-click validation started",
    )];
    match oneclick_endpoint(request) {
        Ok((endpoint, schema_name)) => {
            let issues = match inspect_live(&endpoint) {
                Ok(inspection) => oneclick_issues_from_inspection(&inspection),
                Err(err) => vec![MigrationIssue {
                    severity: "error".to_string(),
                    location: "validation".to_string(),
                    message: err,
                    suggestion: "Check the database connection and rerun validation.".to_string(),
                    blocking: true,
                }],
            };
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.validate",
                "success": issues.is_empty(),
                "schema": schema_name,
                "remaining_issues": issues,
                "all_fixed": issues.is_empty()
            }));
        }
        Err(err) => events.push(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": err
        })),
    }
    events
}

fn oneclick_report(request: &Request) -> Vec<Value> {
    let schema = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("");
    let success = request
        .payload
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let pre_issues = oneclick_payload_issues(&request.payload);
    let remaining_issues = request
        .payload
        .get("remaining_issues")
        .and_then(Value::as_array)
        .map(|issues| {
            issues
                .iter()
                .filter_map(|issue| serde_json::from_value::<MigrationIssue>(issue.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.report",
        "success": success,
        "report": oneclick_report_value(schema, success, &pre_issues, &remaining_issues, Vec::new())
    })]
}

#[derive(Debug, Clone)]
struct OneClickState {
    endpoint: Endpoint,
    schema_name: String,
    inspection: InspectionResult,
    checks: Vec<Value>,
    issues: Vec<MigrationIssue>,
}

fn oneclick_preflight_state(request: &Request) -> Result<OneClickState, String> {
    let (endpoint, schema_name) = oneclick_endpoint(request)?;
    let mut checks = Vec::new();
    let mut issues = Vec::new();
    if endpoint.engine != "mysql" {
        checks.push(oneclick_check(
            "MySQL engine",
            false,
            "error",
            "One-Click migration currently supports MySQL endpoints only.",
        ));
        issues.push(MigrationIssue {
            severity: "error".to_string(),
            location: "connection".to_string(),
            message: "One-Click migration currently supports MySQL endpoints only.".to_string(),
            suggestion: "Use Cross-Engine Migration for PostgreSQL workflows.".to_string(),
            blocking: true,
        });
    } else {
        checks.push(oneclick_check(
            "MySQL engine",
            true,
            "info",
            "MySQL endpoint confirmed.",
        ));
    }

    if request
        .payload
        .get("backup_confirmed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        checks.push(oneclick_check(
            "Backup status",
            true,
            "info",
            "Backup confirmation was provided.",
        ));
    } else {
        checks.push(oneclick_check(
            "Backup status",
            false,
            "warning",
            "Backup confirmation was not provided.",
        ));
        issues.push(MigrationIssue {
            severity: "warning".to_string(),
            location: "backup".to_string(),
            message: "Backup confirmation was not provided.".to_string(),
            suggestion: "Confirm a restorable backup before running destructive fixes.".to_string(),
            blocking: false,
        });
    }

    match inspect_live(&endpoint) {
        Ok(inspection) => {
            checks.push(oneclick_check(
                "Schema inspect",
                true,
                "info",
                &format!("Inspected {} table(s).", inspection.schema.tables.len()),
            ));
            issues.extend(oneclick_issues_from_inspection(&inspection));
            Ok(OneClickState {
                endpoint,
                schema_name,
                inspection,
                checks,
                issues,
            })
        }
        Err(err) => {
            checks.push(oneclick_check("Schema inspect", false, "error", &err));
            issues.push(MigrationIssue {
                severity: "error".to_string(),
                location: "schema".to_string(),
                message: err,
                suggestion: "Check database connection, schema, and inspection permissions."
                    .to_string(),
                blocking: true,
            });
            Ok(OneClickState {
                endpoint,
                schema_name,
                inspection: InspectionResult::default(),
                checks,
                issues,
            })
        }
    }
}

fn oneclick_endpoint(request: &Request) -> Result<(Endpoint, String), String> {
    let mut endpoint = request_endpoint(request)?;
    let schema_name = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|schema| !schema.is_empty())
        .map(ToString::to_string)
        .unwrap_or_else(|| endpoint_schema(&endpoint));
    if endpoint.engine == "mysql" {
        endpoint.database = schema_name.clone();
        endpoint.schema = None;
    } else {
        endpoint.schema = Some(schema_name.clone());
    }
    Ok((endpoint, schema_name))
}

fn oneclick_check(name: &str, passed: bool, severity: &str, message: &str) -> Value {
    json!({
        "name": name,
        "passed": passed,
        "severity": severity,
        "message": message
    })
}

fn oneclick_issues_from_inspection(inspection: &InspectionResult) -> Vec<MigrationIssue> {
    inspection
        .unsupported_objects
        .iter()
        .map(|object| MigrationIssue {
            severity: "warning".to_string(),
            location: object.clone(),
            message: format!("Unsupported object detected: {object}"),
            suggestion: "Review this object manually before promoting One-Click migration."
                .to_string(),
            blocking: false,
        })
        .collect()
}

fn oneclick_payload_issues(payload: &Value) -> Vec<MigrationIssue> {
    payload
        .get("issues")
        .and_then(Value::as_array)
        .map(|issues| {
            issues
                .iter()
                .filter_map(|issue| serde_json::from_value::<MigrationIssue>(issue.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default()
}

fn oneclick_preflight_event(request: &Request, state: &OneClickState) -> Value {
    json!({
        "event": "preflight",
        "request_id": request.request_id,
        "schema": state.schema_name,
        "passed": !state.issues.iter().any(|issue| issue.blocking),
        "checks": state.checks,
        "issues": state.issues
    })
}

fn oneclick_analysis_summary(inspection: &InspectionResult, issues: &[MigrationIssue]) -> Value {
    json!({
        "total_issues": issues.len(),
        "auto_fixable": 0,
        "manual_review": issues.len(),
        "table_count": inspection.schema.tables.len(),
        "unsupported_object_count": inspection.unsupported_objects.len()
    })
}

fn oneclick_recommendations(issues: &[MigrationIssue]) -> Vec<Value> {
    issues
        .iter()
        .enumerate()
        .map(|(index, issue)| {
            json!({
                "issue_index": index,
                "location": issue.location,
                "description": issue.message,
                "selected_option": {
                    "strategy": "manual",
                    "label": "Manual review",
                    "description": issue.suggestion,
                    "sql_template": ""
                }
            })
        })
        .collect()
}

fn oneclick_recommendation_summary(steps: &[Value]) -> Value {
    json!({
        "total_issues": steps.len(),
        "auto_fixable": 0,
        "manual_review": steps.len(),
        "skip_recommended": 0
    })
}

fn oneclick_progress_event(request: &Request, percent: u64, message: &str) -> Value {
    json!({
        "event": "progress",
        "request_id": request.request_id,
        "percent": percent,
        "message": message
    })
}

fn oneclick_final_result(
    request: &Request,
    schema: &str,
    success: bool,
    pre_issues: &[MigrationIssue],
    remaining_issues: &[MigrationIssue],
    execution_log: Vec<String>,
) -> Value {
    json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.run",
        "success": success,
        "report": oneclick_report_value(schema, success, pre_issues, remaining_issues, execution_log)
    })
}

fn oneclick_report_value(
    schema: &str,
    success: bool,
    pre_issues: &[MigrationIssue],
    remaining_issues: &[MigrationIssue],
    execution_log: Vec<String>,
) -> Value {
    json!({
        "schema": schema,
        "started_at": current_unix_seconds().to_string(),
        "completed_at": current_unix_seconds().to_string(),
        "pre_issue_count": pre_issues.len(),
        "post_issue_count": remaining_issues.len(),
        "fixed_issues": [],
        "remaining_issues": remaining_issues,
        "new_issues": [],
        "success": success,
        "execution_log": execution_log,
        "duration_seconds": 0.0
    })
}

fn readiness(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "readiness",
        "direction readiness checks started",
    )];

    let endpoints = match readiness_endpoints(&request.payload) {
        Ok(endpoints) => endpoints,
        Err(err) => {
            events.push(json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            }));
            return events;
        }
    };

    let mut directions = Vec::new();
    for (source, target) in endpoints {
        events.push(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "readiness",
            "message": format!("checking {} -> {}", source.engine, target.engine)
        }));
        let result = direction_readiness(&request.payload, &source, &target);
        for issue in result
            .get("issues")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            events.push(json!({
                "event": "issue",
                "request_id": request.request_id,
                "issue": issue
            }));
        }
        directions.push(result);
    }

    let success = directions.iter().all(|direction| {
        direction
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    });

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "readiness",
        "success": success,
        "directions": directions
    }));
    events
}

fn readiness_endpoints(payload: &Value) -> Result<Vec<(Endpoint, Endpoint)>, String> {
    let source = payload
        .get("source")
        .ok_or_else(|| "source endpoint is required".to_string())
        .and_then(endpoint_from_value)?;
    let target = payload
        .get("target")
        .ok_or_else(|| "target endpoint is required".to_string())
        .and_then(endpoint_from_value)?;
    if source.engine == target.engine {
        return Err(
            "readiness requires one MySQL endpoint and one PostgreSQL endpoint".to_string(),
        );
    }
    if !is_supported_direction(&source.engine, &target.engine) {
        return Err(format!(
            "unsupported readiness endpoints: {} -> {}",
            source.engine, target.engine
        ));
    }
    Ok(vec![(source.clone(), target.clone()), (target, source)])
}

fn direction_readiness(payload: &Value, source: &Endpoint, target: &Endpoint) -> Value {
    let direction = format!("{}_to_{}", source.engine, target.engine);
    match inspect_live(source) {
        Ok(inspection) => {
            let check_payload = json!({
                "source_engine": source.engine,
                "target_engine": target.engine,
                "source": source,
                "target": target,
                "schema": inspection.schema,
                "unsupported_objects": inspection.unsupported_objects,
                "execution_options": parse_options(payload)
            });
            let mut issues = preflight_issues(&check_payload);
            issues.extend(live_preflight_issues(&check_payload));
            json!({
                "direction": direction,
                "source_engine": source.engine,
                "target_engine": target.engine,
                "success": !issues.iter().any(|issue| issue.blocking),
                "table_count": check_payload["schema"]["tables"].as_array().map(Vec::len).unwrap_or(0),
                "unsupported_object_count": check_payload["unsupported_objects"].as_array().map(Vec::len).unwrap_or(0),
                "issues": issues
            })
        }
        Err(err) => json!({
            "direction": direction,
            "source_engine": source.engine,
            "target_engine": target.engine,
            "success": false,
            "table_count": 0,
            "unsupported_object_count": 0,
            "issues": [{
                "severity": "error",
                "location": "source",
                "message": err,
                "suggestion": "Check the source database connection and permissions.",
                "blocking": true
            }]
        }),
    }
}

fn guide(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "guide",
        "direction migration guide generation started",
    )];

    let endpoints = match readiness_endpoints(&request.payload) {
        Ok(endpoints) => endpoints,
        Err(err) => {
            events.push(json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            }));
            return events;
        }
    };

    let mut directions = Vec::new();
    for (source, target) in endpoints {
        events.push(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "guide",
            "message": format!("building detailed guide for {} -> {}", source.engine, target.engine)
        }));
        let result = direction_guide(&request.payload, &source, &target);
        for issue in result
            .get("issues")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            events.push(json!({
                "event": "issue",
                "request_id": request.request_id,
                "issue": issue
            }));
        }
        directions.push(result);
    }

    let success = directions.iter().all(|direction| {
        direction
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    });

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "guide",
        "success": success,
        "directions": directions
    }));
    events
}

fn direction_guide(payload: &Value, source: &Endpoint, target: &Endpoint) -> Value {
    let direction = format!("{}_to_{}", source.engine, target.engine);
    match inspect_live(source) {
        Ok(inspection) => {
            let schema = inspection.schema;
            let check_payload = json!({
                "source_engine": source.engine,
                "target_engine": target.engine,
                "source": source,
                "target": target,
                "schema": schema,
                "unsupported_objects": inspection.unsupported_objects,
                "execution_options": parse_options(payload)
            });
            let mut issues = preflight_issues(&check_payload);
            issues.extend(live_preflight_issues(&check_payload));
            let row_limit = guide_row_limit(payload);
            let mut table_guides = Vec::new();
            match LiveAdapter::connect(source) {
                Ok(mut source_adapter) => {
                    table_guides = build_table_guides(
                        &schema,
                        &mut source_adapter,
                        &source.engine,
                        &target.engine,
                        row_limit,
                        &mut issues,
                    );
                }
                Err(err) => issues.push(MigrationIssue {
                    severity: "error".to_string(),
                    location: "source".to_string(),
                    message: err,
                    suggestion: "Check source database connection before generating row guide."
                        .to_string(),
                    blocking: true,
                }),
            }

            json!({
                "direction": direction,
                "source_engine": source.engine,
                "target_engine": target.engine,
                "success": !issues.iter().any(|issue| issue.blocking),
                "issues": issues,
                "guide": {
                    "method": [
                        "1. Review blocking issues and warnings.",
                        "2. Execute create_table_sql on an empty target.",
                        "3. Stream table rows in the listed order.",
                        "4. Execute sequence_reset_sql, index_sql, and foreign_key_sql after data load.",
                        "5. Run full verify and inspect mismatches before cutover."
                    ],
                    "row_sample_limit": row_limit,
                    "create_table_sql": generate_schema_ddl(&schema, &source.engine, &target.engine),
                    "sequence_reset_sql": generate_sequence_reset_ddl(&schema, &target.engine),
                    "post_data_sql": generate_post_data_ddl(&schema, &target.engine),
                    "unsupported_objects": check_payload["unsupported_objects"].clone(),
                    "tables": table_guides
                }
            })
        }
        Err(err) => json!({
            "direction": direction,
            "source_engine": source.engine,
            "target_engine": target.engine,
            "success": false,
            "issues": [{
                "severity": "error",
                "location": "source",
                "message": err,
                "suggestion": "Check the source database connection and permissions.",
                "blocking": true
            }],
            "guide": {
                "method": ["Fix source connection and run guide again."],
                "row_sample_limit": guide_row_limit(payload),
                "create_table_sql": [],
                "sequence_reset_sql": [],
                "post_data_sql": [],
                "unsupported_objects": [],
                "tables": []
            }
        }),
    }
}

fn guide_row_limit(payload: &Value) -> usize {
    payload
        .get("guide_options")
        .and_then(|options| options.get("row_limit"))
        .and_then(Value::as_u64)
        .map(|value| value.clamp(1, 1000) as usize)
        .unwrap_or(5)
}

fn build_table_guides<A: MigrationAdapter>(
    schema: &NormalizedSchema,
    source: &mut A,
    source_engine: &str,
    target_engine: &str,
    row_limit: usize,
    issues: &mut Vec<MigrationIssue>,
) -> Vec<Value> {
    let mut tables = Vec::new();
    for table in &schema.tables {
        let row_count = match source.row_count(&table.name) {
            Ok(count) => count,
            Err(err) => {
                issues.push(MigrationIssue {
                    severity: "error".to_string(),
                    location: table.name.clone(),
                    message: err,
                    suggestion: "Check table read permissions.".to_string(),
                    blocking: true,
                });
                0
            }
        };
        let rows = match source.read_rows(table, 0, row_limit) {
            Ok(rows) => rows,
            Err(err) => {
                issues.push(MigrationIssue {
                    severity: "error".to_string(),
                    location: table.name.clone(),
                    message: err,
                    suggestion: "Check table read permissions.".to_string(),
                    blocking: true,
                });
                Vec::new()
            }
        };
        let columns = table
            .columns
            .iter()
            .map(|column| {
                json!({
                    "name": &column.name,
                    "source_type": &column.type_name,
                    "target_type": map_type(source_engine, target_engine, &strip_generation_marker(&column.type_name)),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                    "unique": column.unique,
                    "default": &column.default_value,
                    "auto_increment": is_auto_increment_type(&column.type_name)
                })
            })
            .collect::<Vec<_>>();
        let insert_example_sql = if rows.is_empty() {
            String::new()
        } else {
            insert_rows_literal_sql_for_table(target_engine, table, &rows)
        };
        tables.push(json!({
            "table": &table.name,
            "row_count": row_count,
            "sample_truncated": row_count > rows.len(),
            "columns": columns,
            "row_samples": rows,
            "insert_example_sql": insert_example_sql,
            "copy_method": format!("Stream rows in chunks and use target {} INSERT batches generated from canonical row values.", target_engine)
        }));
    }
    tables
}

fn plan(request: &Request) -> Vec<Value> {
    let source = read_engine(&request.payload, "source_engine");
    let target = read_engine(&request.payload, "target_engine");
    let schema =
        dependency_ordered_schema(&parse_schema(&request.payload["schema"]).unwrap_or_default());
    let ddl = generate_schema_ddl(&schema, &source, &target);
    let table_order = table_dependency_order(&schema);
    let tables = plan_table_summaries(request, &schema);

    vec![
        phase_event(request, "plan", "migration plan generation started"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "plan",
            "success": true,
            "plan": {
                "ddl": ddl,
                "tables": tables,
                "table_order": table_order,
                "execution_options": parse_options(&request.payload)
            }
        }),
    ]
}

fn plan_table_summaries(request: &Request, schema: &NormalizedSchema) -> Vec<Value> {
    let mut rows_by_table = BTreeMap::<String, usize>::new();
    if let Some(source_data) = request.payload.get("source_data") {
        let source = MemoryAdapter::from_value(Some(source_data));
        for table in &schema.tables {
            rows_by_table.insert(table.name.clone(), source.row_count(&table.name));
        }
    } else if let Some(source_value) = request.payload.get("source") {
        if let Ok(source_endpoint) = endpoint_from_value(source_value) {
            if let Ok(mut source) = LiveAdapter::connect(&source_endpoint) {
                for table in &schema.tables {
                    if let Ok(rows) = source.row_count(&table.name) {
                        rows_by_table.insert(table.name.clone(), rows);
                    }
                }
            }
        }
    }

    schema
        .tables
        .iter()
        .map(|table| {
            json!({
                "name": table.name,
                "estimated_rows": rows_by_table.get(&table.name).copied().unwrap_or(0)
            })
        })
        .collect()
}

fn migrate_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(request, "migrate", "migration started"));
    if request.payload.get("source").is_some() && request.payload.get("target").is_some() {
        let schema = dependency_ordered_schema(
            &parse_schema(&request.payload["schema"]).unwrap_or_default(),
        );
        let options = parse_options(&request.payload);
        let resume_state = request
            .payload
            .get("state")
            .and_then(|value| serde_json::from_value::<ResumeState>(value.clone()).ok());
        let source_endpoint = match request
            .payload
            .get("source")
            .map(endpoint_from_value)
            .transpose()
        {
            Ok(Some(endpoint)) => endpoint,
            Ok(None) => unreachable!(),
            Err(err) => {
                emit(json!({"event": "error", "request_id": request.request_id, "message": err}));
                return;
            }
        };
        let target_endpoint = match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
        {
            Ok(Some(endpoint)) => endpoint,
            Ok(None) => unreachable!(),
            Err(err) => {
                emit(json!({"event": "error", "request_id": request.request_id, "message": err}));
                return;
            }
        };

        match (
            LiveAdapter::connect(&source_endpoint),
            LiveAdapter::connect(&target_endpoint),
        ) {
            (Ok(mut source), Ok(mut target)) => {
                if let Err(err) = prepare_target_schema(&mut target, &target_endpoint) {
                    emit(
                        json!({"event": "error", "request_id": request.request_id, "message": err}),
                    );
                    return;
                }
                if options.cleanup_before_migrate {
                    if let Err(err) = cleanup_target_tables(
                        &schema,
                        &mut target,
                        &target_endpoint.engine,
                        &mut emit,
                        request,
                    ) {
                        emit(
                            json!({"event": "error", "request_id": request.request_id, "message": err}),
                        );
                        return;
                    }
                }
                let mut checkpoint =
                    |event: Value| emit(add_request_id(event, &request.request_id));
                let result = migrate_with_adapters_reporting(
                    &schema,
                    &options,
                    resume_state.as_ref(),
                    &mut source,
                    &mut target,
                    &source_endpoint.engine,
                    &target_endpoint.engine,
                    &mut checkpoint,
                );
                emit(json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "migrate",
                    "success": result.success,
                    "cancelled": !result.success && options.cancel_after_chunks.is_some() && result.issues.is_empty(),
                    "rows_copied": result.rows_copied,
                    "chunks_copied": result.chunks_copied,
                    "state": result.state,
                    "issues": result.issues
                }));
            }
            (Err(err), _) | (_, Err(err)) => {
                emit(json!({"event": "error", "request_id": request.request_id, "message": err}));
            }
        }
        return;
    }

    if request.payload.get("source_data").is_none() {
        emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "live data streaming is not implemented in this helper build"
        }));
        return;
    }

    let schema =
        dependency_ordered_schema(&parse_schema(&request.payload["schema"]).unwrap_or_default());
    let options = parse_options(&request.payload);
    let resume_state = request
        .payload
        .get("state")
        .and_then(|value| serde_json::from_value::<ResumeState>(value.clone()).ok());
    let source = MemoryAdapter::from_value(request.payload.get("source_data"));
    let mut target = MemoryAdapter::from_value(request.payload.get("target_data"));
    let mut source = source.clone();
    let mut checkpoint = |event: Value| emit(add_request_id(event, &request.request_id));
    let result = migrate_with_adapters_reporting(
        &schema,
        &options,
        resume_state.as_ref(),
        &mut source,
        &mut target,
        "",
        "",
        &mut checkpoint,
    );

    for table in &schema.tables {
        emit(json!({
            "event": "table_progress",
            "request_id": request.request_id,
            "table": table.name,
            "status": if result.state.tables.iter().any(|state| state.table == table.name && state.completed) { "completed" } else { "pending" }
        }));
    }

    emit(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "migrate",
        "success": result.success,
        "cancelled": !result.success && options.cancel_after_chunks.is_some() && result.issues.is_empty(),
        "rows_copied": result.rows_copied,
        "chunks_copied": result.chunks_copied,
        "state": result.state,
        "issues": result.issues,
        "target_data": target.rows
    }));
}

fn add_request_id(mut event: Value, request_id: &Option<String>) -> Value {
    if let Value::Object(object) = &mut event {
        object.insert(
            "request_id".to_string(),
            request_id
                .as_ref()
                .map(|value| Value::String(value.clone()))
                .unwrap_or(Value::Null),
        );
    }
    event
}

fn verify(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(request, "verify", "verification started")];
    if request.payload.get("source").is_some() && request.payload.get("target").is_some() {
        let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
        let source_endpoint = match request
            .payload
            .get("source")
            .map(endpoint_from_value)
            .transpose()
        {
            Ok(Some(endpoint)) => endpoint,
            Ok(None) => unreachable!(),
            Err(err) => {
                events.push(
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                );
                return events;
            }
        };
        let target_endpoint = match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
        {
            Ok(Some(endpoint)) => endpoint,
            Ok(None) => unreachable!(),
            Err(err) => {
                events.push(
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                );
                return events;
            }
        };
        let options = parse_options(&request.payload);
        match (
            LiveAdapter::connect(&source_endpoint),
            LiveAdapter::connect(&target_endpoint),
        ) {
            (Ok(mut source), Ok(mut target)) => {
                let mut emit =
                    |event: Value| events.push(add_request_id(event, &request.request_id));
                let mismatches = verify_with_adapters_reporting(
                    &schema,
                    &mut source,
                    &mut target,
                    options.chunk_size,
                    &mut emit,
                );
                events.push(json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "verify",
                    "success": mismatches.is_empty(),
                    "mismatches": mismatches
                }));
            }
            (Err(err), _) | (_, Err(err)) => {
                events.push(
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                );
            }
        }
        return events;
    }

    if request.payload.get("source_data").is_some() && request.payload.get("target_data").is_some()
    {
        let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
        let mut source = MemoryAdapter::from_value(request.payload.get("source_data"));
        let mut target = MemoryAdapter::from_value(request.payload.get("target_data"));
        let mut emit = |event: Value| events.push(add_request_id(event, &request.request_id));
        let mismatches =
            verify_with_adapters_reporting(&schema, &mut source, &mut target, 1000, &mut emit);
        events.push(json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "verify",
            "success": mismatches.is_empty(),
            "mismatches": mismatches
        }));
        return events;
    }

    let source_rows = request
        .payload
        .pointer("/source_rows")
        .and_then(Value::as_array);
    let target_rows = request
        .payload
        .pointer("/target_rows")
        .and_then(Value::as_array);
    if source_rows.is_none() || target_rows.is_none() {
        events.push(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "verification requires source_rows and target_rows payloads in this helper build"
        }));
        return events;
    }
    let mismatches = compare_digest_rows(source_rows.unwrap(), target_rows.unwrap());

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "verify",
        "success": mismatches.is_empty(),
        "mismatches": mismatches
    }));
    events
}

fn resume(request: &Request) -> Vec<Value> {
    let state = request
        .payload
        .get("state")
        .and_then(|value| serde_json::from_value::<ResumeState>(value.clone()).ok());
    let next_table = state.as_ref().and_then(next_table_to_copy);

    vec![
        phase_event(request, "resume", "resume state loaded"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "resume",
            "success": state.is_some(),
            "next_table": next_table
        }),
    ]
}

fn cleanup_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(
        request,
        "cleanup",
        "failed migration cleanup started",
    ));
    let schema =
        dependency_ordered_schema(&parse_schema(&request.payload["schema"]).unwrap_or_default());
    let target_engine = read_engine(&request.payload, "target_engine");
    let mut dropped_tables = Vec::new();

    if request.payload.get("target").is_some() {
        let target_endpoint = match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
        {
            Ok(Some(endpoint)) => endpoint,
            Ok(None) => unreachable!(),
            Err(err) => {
                emit(json!({"event": "error", "request_id": request.request_id, "message": err}));
                return;
            }
        };
        let mut target = match LiveAdapter::connect(&target_endpoint) {
            Ok(target) => target,
            Err(err) => {
                emit(json!({"event": "error", "request_id": request.request_id, "message": err}));
                return;
            }
        };
        match cleanup_target_tables(
            &schema,
            &mut target,
            &target_endpoint.engine,
            &mut emit,
            request,
        ) {
            Ok(tables) => dropped_tables.extend(tables),
            Err(err) => {
                emit(json!({"event": "error", "request_id": request.request_id, "message": err}));
                return;
            }
        }
    } else {
        dropped_tables.extend(schema.tables.iter().rev().map(|table| table.name.clone()));
    }

    emit(phase_event(
        request,
        "cleanup",
        "failed migration cleanup completed",
    ));
    emit(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "cleanup",
        "success": true,
        "target_engine": target_engine,
        "dropped_tables": dropped_tables
    }));
}

fn cleanup_target_tables<F: FnMut(Value)>(
    schema: &NormalizedSchema,
    target: &mut LiveAdapter,
    target_engine: &str,
    emit: &mut F,
    request: &Request,
) -> Result<Vec<String>, String> {
    let mut dropped_tables = Vec::new();
    for table in schema.tables.iter().rev() {
        emit(json!({
            "event": "table_progress",
            "request_id": request.request_id,
            "table": table.name,
            "status": "dropping"
        }));
        target
            .execute_sql(&drop_table_sql(target_engine, &table.name))
            .map_err(|err| format!("cleanup drop table {} failed: {err}", table.name))?;
        dropped_tables.push(table.name.clone());
    }
    Ok(dropped_tables)
}

fn phase_event(request: &Request, phase: &str, message: &str) -> Value {
    json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": phase,
        "message": message
    })
}

pub fn preflight_issues(payload: &Value) -> Vec<MigrationIssue> {
    let source = read_engine(payload, "source_engine");
    let target = read_engine(payload, "target_engine");
    let mut issues = Vec::new();

    if source.is_empty() || target.is_empty() {
        issues.push(MigrationIssue {
            severity: "error".to_string(),
            location: "connection".to_string(),
            message: "source_engine and target_engine are required".to_string(),
            suggestion: "Provide mysql or postgresql for both endpoints.".to_string(),
            blocking: true,
        });
    } else if source == target {
        issues.push(MigrationIssue {
            severity: "error".to_string(),
            location: "direction".to_string(),
            message: "cross-engine migration requires different source and target engines"
                .to_string(),
            suggestion: "Choose mysql -> postgresql or postgresql -> mysql.".to_string(),
            blocking: true,
        });
    } else if !is_supported_direction(&source, &target) {
        issues.push(MigrationIssue {
            severity: "error".to_string(),
            location: "direction".to_string(),
            message: format!("unsupported direction: {source} -> {target}"),
            suggestion: "v1 supports mysql <-> postgresql only.".to_string(),
            blocking: true,
        });
    } else {
        issues.push(MigrationIssue {
            severity: "warning".to_string(),
            location: "users_grants".to_string(),
            message: "database users and grants are report-only in cross-engine v1".to_string(),
            suggestion: "Recreate users, roles, and grants manually after validating table data."
                .to_string(),
            blocking: false,
        });
    }

    for object_name in unsupported_objects(payload) {
        issues.push(MigrationIssue {
            severity: "warning".to_string(),
            location: object_name,
            message: "object is report-only in cross-engine v1".to_string(),
            suggestion: "Review and recreate this object manually after table data is moved."
                .to_string(),
            blocking: false,
        });
    }

    let options = parse_options(payload);
    if options.mode == "create_only" {
        let target = MemoryAdapter::from_value(payload.get("target_data"));
        if let Ok(schema) = parse_schema(&payload["schema"]) {
            for table in &schema.tables {
                if target.row_count(&table.name) > 0 {
                    issues.push(MigrationIssue {
                        severity: "error".to_string(),
                        location: table.name.clone(),
                        message: "target table is not empty".to_string(),
                        suggestion: "Use an empty target table or run with a non-create_only mode."
                            .to_string(),
                        blocking: true,
                    });
                }
            }
        }
    }

    issues
}

fn live_preflight_issues(payload: &Value) -> Vec<MigrationIssue> {
    if payload.get("target").is_none() {
        return Vec::new();
    }
    let options = parse_options(payload);
    if options.mode != "create_only" {
        return Vec::new();
    }
    let Ok(schema) = parse_schema(&payload["schema"]) else {
        return Vec::new();
    };
    let target_endpoint = match payload.get("target").map(endpoint_from_value).transpose() {
        Ok(Some(endpoint)) => endpoint,
        Ok(None) => return Vec::new(),
        Err(err) => {
            return vec![MigrationIssue {
                severity: "error".to_string(),
                location: "target".to_string(),
                message: err,
                suggestion: "Check the target endpoint settings.".to_string(),
                blocking: true,
            }];
        }
    };
    let mut target = match LiveAdapter::connect(&target_endpoint) {
        Ok(target) => target,
        Err(err) => {
            return vec![MigrationIssue {
                severity: "error".to_string(),
                location: "target".to_string(),
                message: err,
                suggestion: "Check the target database connection.".to_string(),
                blocking: true,
            }];
        }
    };
    if options.cleanup_before_migrate {
        return vec![MigrationIssue {
            severity: "warning".to_string(),
            location: "target".to_string(),
            message: "target cleanup is planned before migration".to_string(),
            suggestion:
                "Review the plan and start DB migration only when target cleanup is intended."
                    .to_string(),
            blocking: false,
        }];
    }
    create_only_issues_with_adapter(&schema, &options, &mut target)
}

pub fn migrate_memory(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    resume_state: Option<&ResumeState>,
    source: &MemoryAdapter,
    target: &mut MemoryAdapter,
) -> MigrationResult {
    let mut source = source.clone();
    migrate_with_adapters(schema, options, resume_state, &mut source, target, "", "")
}

pub fn migrate_with_adapters<S: MigrationAdapter, T: MigrationAdapter>(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    resume_state: Option<&ResumeState>,
    source: &mut S,
    target: &mut T,
    source_engine: &str,
    target_engine: &str,
) -> MigrationResult {
    migrate_with_adapters_reporting(
        schema,
        options,
        resume_state,
        source,
        target,
        source_engine,
        target_engine,
        &mut |_| {},
    )
}

fn migrate_with_adapters_reporting<S: MigrationAdapter, T: MigrationAdapter, F: FnMut(Value)>(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    resume_state: Option<&ResumeState>,
    source: &mut S,
    target: &mut T,
    source_engine: &str,
    target_engine: &str,
    on_event: &mut F,
) -> MigrationResult {
    let blocking_issues = create_only_issues_with_adapter(schema, options, target);
    if !blocking_issues.is_empty() {
        return MigrationResult {
            success: false,
            rows_copied: 0,
            chunks_copied: 0,
            state: initial_state(schema),
            issues: blocking_issues,
        };
    }

    let ordered_schema = dependency_ordered_schema(schema);
    let mut state = resume_state
        .cloned()
        .unwrap_or_else(|| initial_state(&ordered_schema));
    let mut rows_copied = 0;
    let mut chunks_copied = 0;
    let chunk_size = options.chunk_size.max(1);
    let ddl = if source_engine.is_empty() || target_engine.is_empty() {
        Vec::new()
    } else {
        generate_schema_ddl(&ordered_schema, source_engine, target_engine)
    };

    for (table_index, table) in ordered_schema.tables.iter().enumerate() {
        let state_index = state
            .tables
            .iter()
            .position(|candidate| candidate.table == table.name);
        let Some(state_index) = state_index else {
            continue;
        };
        if state.tables[state_index].completed {
            continue;
        }

        let table_ddl = ddl.get(table_index).map(String::as_str).unwrap_or("");
        if let Err(err) = target.create_table(table, table_ddl) {
            return migration_error_result(state, rows_copied, chunks_copied, table, err);
        }
        let total_rows = source.row_count(&table.name).ok();
        let key_columns = key_columns(table);
        let use_keyset = !key_columns.is_empty();
        let mut offset = if use_keyset {
            0
        } else {
            state.tables[state_index].rows_copied as usize
        };
        let mut last_key = if use_keyset {
            state.tables[state_index].last_key.clone()
        } else {
            None
        };
        loop {
            let rows = match if use_keyset {
                source.read_rows_after_key(table, &key_columns, last_key.as_deref(), chunk_size)
            } else {
                source.read_rows(table, offset, chunk_size)
            } {
                Ok(rows) => rows,
                Err(err) => {
                    return migration_error_result(state, rows_copied, chunks_copied, table, err)
                }
            };
            if rows.is_empty() {
                state.tables[state_index].completed = true;
                state.tables[state_index].last_key = None;
                on_event(json!({
                    "event": "table_progress",
                    "table": table.name,
                    "status": "completed",
                    "state": &state
                }));
                break;
            }

            let copied_now = rows.len();
            let next_key = if use_keyset {
                rows.last().and_then(|row| row_key_token(row, &key_columns))
            } else {
                None
            };
            if let Err(err) = target.insert_rows(table, rows) {
                return migration_error_result(state, rows_copied, chunks_copied, table, err);
            }
            if use_keyset {
                state.tables[state_index].rows_copied += copied_now as u64;
                state.tables[state_index].last_key = next_key.clone();
                last_key = next_key;
            } else {
                offset += copied_now;
                state.tables[state_index].rows_copied = offset as u64;
                state.tables[state_index].last_key = Some(offset.to_string());
            }
            rows_copied += copied_now as u64;
            chunks_copied += 1;
            on_event(json!({
                "event": "row_progress",
                "table": table.name,
                "rows": state.tables[state_index].rows_copied,
                "total": total_rows,
                "state": &state
            }));

            if options
                .cancel_after_chunks
                .is_some_and(|limit| chunks_copied >= limit)
            {
                return MigrationResult {
                    success: false,
                    rows_copied,
                    chunks_copied,
                    state,
                    issues: Vec::new(),
                };
            }
        }
    }

    state.current_phase = "completed".to_string();
    if let Err(err) = apply_post_load_ddl(target, &ordered_schema, target_engine) {
        let table = ordered_schema
            .tables
            .first()
            .cloned()
            .unwrap_or(NormalizedTable {
                name: "post_data_ddl".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            });
        return migration_error_result(state, rows_copied, chunks_copied, &table, err);
    }
    MigrationResult {
        success: true,
        rows_copied,
        chunks_copied,
        state,
        issues: Vec::new(),
    }
}

fn migration_error_result(
    state: ResumeState,
    rows_copied: u64,
    chunks_copied: usize,
    table: &NormalizedTable,
    err: String,
) -> MigrationResult {
    MigrationResult {
        success: false,
        rows_copied,
        chunks_copied,
        state,
        issues: vec![MigrationIssue {
            severity: "error".to_string(),
            location: table.name.clone(),
            message: err,
            suggestion: "Resolve the database error and resume the migration.".to_string(),
            blocking: true,
        }],
    }
}

fn create_only_issues_with_adapter<T: MigrationAdapter>(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    target: &mut T,
) -> Vec<MigrationIssue> {
    if options.mode != "create_only" {
        return Vec::new();
    }
    let mut issues = Vec::new();
    for table in &schema.tables {
        match target.row_count(&table.name) {
            Ok(count) if count > 0 => issues.push(MigrationIssue {
                severity: "error".to_string(),
                location: table.name.clone(),
                message: "target table is not empty".to_string(),
                suggestion: "Use an empty target table or run with a non-create_only mode."
                    .to_string(),
                blocking: true,
            }),
            Err(err) => issues.push(MigrationIssue {
                severity: "error".to_string(),
                location: table.name.clone(),
                message: err,
                suggestion: "Check target connectivity and permissions.".to_string(),
                blocking: true,
            }),
            _ => {}
        }
    }
    issues
}

pub fn verify_memory(
    schema: &NormalizedSchema,
    source: &MemoryAdapter,
    target: &MemoryAdapter,
) -> Vec<Value> {
    let mut mismatches = Vec::new();
    for table in &schema.tables {
        let source_rows = source.rows.get(&table.name).cloned().unwrap_or_default();
        let target_rows = target.rows.get(&table.name).cloned().unwrap_or_default();

        if source_rows.len() != target_rows.len() {
            mismatches.push(json!({
                "table": table.name,
                "kind": "count",
                "source_count": source_rows.len(),
                "target_count": target_rows.len()
            }));
        }

        let key_columns = key_columns(table);
        if key_columns.is_empty() {
            for mismatch in compare_typed_digest_rows(table, &source_rows, &target_rows) {
                mismatches.push(with_table(&table.name, mismatch));
            }
        } else {
            mismatches.extend(compare_typed_ordered_keyed_rows(
                table,
                &key_columns,
                &source_rows,
                &target_rows,
            ));
        }
    }
    mismatches
}

pub fn verify_with_adapters<S: MigrationAdapter, T: MigrationAdapter>(
    schema: &NormalizedSchema,
    source: &mut S,
    target: &mut T,
    chunk_size: usize,
) -> Vec<Value> {
    let mut emit = |_event: Value| {};
    verify_with_adapters_reporting(schema, source, target, chunk_size, &mut emit)
}

fn verify_with_adapters_reporting<S: MigrationAdapter, T: MigrationAdapter, F: FnMut(Value)>(
    schema: &NormalizedSchema,
    source: &mut S,
    target: &mut T,
    chunk_size: usize,
    emit: &mut F,
) -> Vec<Value> {
    let mut mismatches = Vec::new();
    let chunk_size = chunk_size.max(1);
    for table in &schema.tables {
        emit(json!({
            "event": "table_progress",
            "table": table.name,
            "status": "verifying"
        }));
        let source_count = match source.row_count(&table.name) {
            Ok(count) => count,
            Err(err) => {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "error",
                    "side": "source",
                    "message": err
                }));
                continue;
            }
        };
        let target_count = match target.row_count(&table.name) {
            Ok(count) => count,
            Err(err) => {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "error",
                    "side": "target",
                    "message": err
                }));
                continue;
            }
        };
        let total_rows = source_count.max(target_count);
        let mut verified_rows = 0usize;
        emit(json!({
            "event": "row_progress",
            "table": table.name,
            "rows": verified_rows,
            "total": total_rows
        }));
        if source_count != target_count {
            mismatches.push(json!({
                "table": table.name,
                "kind": "count",
                "source_count": source_count,
                "target_count": target_count
            }));
        }

        let key_columns = key_columns(table);
        if key_columns.is_empty() {
            let source_counts = match digest_counts_for_adapter(source, table, chunk_size) {
                Ok(counts) => counts,
                Err(err) => {
                    mismatches.push(json!({
                        "table": table.name,
                        "kind": "error",
                        "side": "source",
                        "message": err
                    }));
                    continue;
                }
            };
            let target_counts = match digest_counts_for_adapter(target, table, chunk_size) {
                Ok(counts) => counts,
                Err(err) => {
                    mismatches.push(json!({
                        "table": table.name,
                        "kind": "error",
                        "side": "target",
                        "message": err
                    }));
                    continue;
                }
            };
            for mismatch in compare_digest_counts(&source_counts, &target_counts) {
                mismatches.push(with_table(&table.name, mismatch));
            }
            verified_rows = total_rows;
            emit(json!({
                "event": "row_progress",
                "table": table.name,
                "rows": verified_rows,
                "total": total_rows
            }));
            emit(json!({
                "event": "table_progress",
                "table": table.name,
                "status": "completed"
            }));
            continue;
        }

        let mut last_key: Option<String> = None;
        loop {
            let source_rows = match source.read_rows_after_key(
                table,
                &key_columns,
                last_key.as_deref(),
                chunk_size,
            ) {
                Ok(rows) => rows,
                Err(err) => {
                    mismatches.push(json!({
                        "table": table.name,
                        "kind": "error",
                        "side": "source",
                        "message": err
                    }));
                    break;
                }
            };
            let target_rows = match target.read_rows_after_key(
                table,
                &key_columns,
                last_key.as_deref(),
                chunk_size,
            ) {
                Ok(rows) => rows,
                Err(err) => {
                    mismatches.push(json!({
                        "table": table.name,
                        "kind": "error",
                        "side": "target",
                        "message": err
                    }));
                    break;
                }
            };
            if source_rows.is_empty() && target_rows.is_empty() {
                break;
            }
            mismatches.extend(compare_typed_keyed_rows(
                table,
                &key_columns,
                &source_rows,
                &target_rows,
            ));
            verified_rows += source_rows.len().max(target_rows.len());
            emit(json!({
                "event": "row_progress",
                "table": table.name,
                "rows": verified_rows.min(total_rows),
                "total": total_rows
            }));
            let next_key = source_rows
                .last()
                .or_else(|| target_rows.last())
                .and_then(|row| row_key_token(row, &key_columns));
            if next_key.is_none() || next_key == last_key {
                break;
            }
            last_key = next_key;
        }
        emit(json!({
            "event": "table_progress",
            "table": table.name,
            "status": "completed"
        }));
    }
    mismatches
}

fn digest_counts_for_adapter<A: MigrationAdapter>(
    adapter: &mut A,
    table: &NormalizedTable,
    chunk_size: usize,
) -> Result<BTreeMap<String, u64>, String> {
    let mut counts = BTreeMap::new();
    let mut offset = 0;
    loop {
        let rows = adapter.read_rows(table, offset, chunk_size)?;
        if rows.is_empty() {
            break;
        }
        for row in normalize_rows_for_table(table, &rows) {
            if let Value::Object(object) = row {
                *counts.entry(row_digest(&object)).or_insert(0) += 1;
            }
        }
        offset += rows.len();
    }
    Ok(counts)
}

fn with_table(table: &str, mismatch: Value) -> Value {
    let mut object = mismatch.as_object().cloned().unwrap_or_default();
    object.insert("table".to_string(), json!(table));
    object.insert("kind".to_string(), json!("digest"));
    Value::Object(object)
}

pub fn compare_keyed_rows(
    table: &str,
    key_columns: &[String],
    source_rows: &[Value],
    target_rows: &[Value],
) -> Vec<Value> {
    let source_index = keyed_index(key_columns, source_rows);
    let target_index = keyed_index(key_columns, target_rows);
    let mut mismatches = Vec::new();

    for (key, source_row) in &source_index {
        let Some(target_row) = target_index.get(key) else {
            mismatches.push(json!({
                "table": table,
                "kind": "missing_target",
                "key": key
            }));
            continue;
        };
        let source_object = source_row.as_object().cloned().unwrap_or_default();
        let target_object = target_row.as_object().cloned().unwrap_or_default();
        let mut columns = BTreeSet::new();
        columns.extend(source_object.keys().cloned());
        columns.extend(target_object.keys().cloned());

        for column in columns {
            let left = source_object.get(&column).unwrap_or(&Value::Null);
            let right = target_object.get(&column).unwrap_or(&Value::Null);
            if canonical_value(left) != canonical_value(right) {
                mismatches.push(json!({
                    "table": table,
                    "kind": "cell",
                    "key": key,
                    "column": column,
                    "source": left,
                    "target": right
                }));
            }
        }
    }
    for key in target_index.keys() {
        if !source_index.contains_key(key) {
            mismatches.push(json!({
                "table": table,
                "kind": "extra_target",
                "key": key
            }));
        }
    }
    mismatches
}

fn compare_digest_counts(
    source_counts: &BTreeMap<String, u64>,
    target_counts: &BTreeMap<String, u64>,
) -> Vec<Value> {
    let mut mismatches = Vec::new();

    for (digest, source_count) in source_counts {
        let target_count = target_counts.get(digest).copied().unwrap_or(0);
        if *source_count != target_count {
            mismatches.push(json!({
                "digest": digest,
                "source_count": source_count,
                "target_count": target_count
            }));
        }
    }
    for (digest, target_count) in target_counts {
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

pub fn compare_typed_keyed_rows(
    table: &NormalizedTable,
    key_columns: &[String],
    source_rows: &[Value],
    target_rows: &[Value],
) -> Vec<Value> {
    let source_rows = normalize_rows_for_table(table, source_rows);
    let target_rows = normalize_rows_for_table(table, target_rows);
    compare_keyed_rows(&table.name, key_columns, &source_rows, &target_rows)
}

fn compare_typed_ordered_keyed_rows(
    table: &NormalizedTable,
    key_columns: &[String],
    source_rows: &[Value],
    target_rows: &[Value],
) -> Vec<Value> {
    if source_rows.len() != target_rows.len() {
        return compare_typed_keyed_rows(table, key_columns, source_rows, target_rows);
    }

    let mut mismatches = Vec::new();
    for (source_row, target_row) in source_rows.iter().zip(target_rows) {
        let source_row = normalize_row_for_table(table, source_row);
        let target_row = normalize_row_for_table(table, target_row);
        let source_key = row_key_token(&source_row, key_columns);
        let target_key = row_key_token(&target_row, key_columns);
        if source_key != target_key {
            return compare_typed_keyed_rows(table, key_columns, source_rows, target_rows);
        }
        let source_object = source_row.as_object().cloned().unwrap_or_default();
        let target_object = target_row.as_object().cloned().unwrap_or_default();
        let key = source_key.unwrap_or_default();
        for column in &table.columns {
            let left = source_object.get(&column.name).unwrap_or(&Value::Null);
            let right = target_object.get(&column.name).unwrap_or(&Value::Null);
            if canonical_value(left) != canonical_value(right) {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "cell",
                    "key": key,
                    "column": column.name,
                    "source": left,
                    "target": right
                }));
            }
        }
    }
    mismatches
}

fn keyed_index(key_columns: &[String], rows: &[Value]) -> BTreeMap<String, Value> {
    let mut index = BTreeMap::new();
    for row in rows {
        if let Value::Object(object) = row {
            let key = key_columns
                .iter()
                .map(|column| canonical_value(object.get(column).unwrap_or(&Value::Null)))
                .collect::<Vec<_>>()
                .join("|");
            index.insert(key, row.clone());
        }
    }
    index
}

pub fn compare_typed_digest_rows(
    table: &NormalizedTable,
    source: &[Value],
    target: &[Value],
) -> Vec<Value> {
    let source = normalize_rows_for_table(table, source);
    let target = normalize_rows_for_table(table, target);
    compare_digest_rows(&source, &target)
}

fn normalize_rows_for_table(table: &NormalizedTable, rows: &[Value]) -> Vec<Value> {
    rows.iter()
        .map(|row| normalize_row_for_table(table, row))
        .collect()
}

fn normalize_row_for_table(table: &NormalizedTable, row: &Value) -> Value {
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

fn key_columns(table: &NormalizedTable) -> Vec<String> {
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

fn column_names(table: &NormalizedTable) -> Vec<String> {
    table
        .columns
        .iter()
        .map(|column| column.name.clone())
        .collect()
}

fn row_key_token(row: &Value, key_columns: &[String]) -> Option<String> {
    let object = row.as_object()?;
    let values: Option<Vec<String>> = key_columns
        .iter()
        .map(|column| object.get(column).and_then(scalar_text))
        .collect();
    values.map(|values| serde_json::to_string(&values).unwrap_or_default())
}

fn keyset_start_index(rows: &[Value], key_columns: &[String], last_key: Option<&str>) -> usize {
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

fn decode_key_token(token: &str) -> Option<Vec<String>> {
    if let Ok(values) = serde_json::from_str::<Vec<String>>(token) {
        return Some(values);
    }
    Some(vec![token.to_string()])
}

fn mysql_row_to_json(columns: &[String], row: mysql::Row) -> Value {
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

fn write_mysql_text_row_tsv<W: Write>(writer: &mut W, row: mysql::Row) -> Result<(), String> {
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

fn postgres_row_to_json(columns: &[String], row: &postgres::Row) -> Value {
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

pub fn table_dependency_order(schema: &NormalizedSchema) -> Vec<String> {
    let (ordered, _) = table_dependency_order_indices(schema);
    ordered
        .into_iter()
        .map(|index| schema.tables[index].name.clone())
        .collect()
}

pub fn dependency_ordered_schema(schema: &NormalizedSchema) -> NormalizedSchema {
    let (ordered, _) = table_dependency_order_indices(schema);
    NormalizedSchema {
        tables: ordered
            .into_iter()
            .map(|index| schema.tables[index].clone())
            .collect(),
    }
}

fn dependency_ordered_dump_tables(
    schema: &NormalizedSchema,
    tables: Vec<DumpTableManifest>,
) -> Vec<DumpTableManifest> {
    let mut by_name = tables
        .into_iter()
        .map(|table| (table.name.clone(), table))
        .collect::<BTreeMap<_, _>>();
    let mut ordered = Vec::new();
    for table_name in table_dependency_order(schema) {
        if let Some(table) = by_name.remove(&table_name) {
            ordered.push(table);
        }
    }
    ordered.extend(by_name.into_values());
    ordered
}

fn table_dependency_order_indices(schema: &NormalizedSchema) -> (Vec<usize>, Vec<String>) {
    let table_count = schema.tables.len();
    if table_count <= 1 {
        return ((0..table_count).collect(), Vec::new());
    }

    let table_index = schema
        .tables
        .iter()
        .enumerate()
        .map(|(index, table)| (table.name.clone(), index))
        .collect::<BTreeMap<_, _>>();
    let mut dependents = vec![Vec::<usize>::new(); table_count];
    let mut seen_edges = BTreeSet::new();
    let mut indegree = vec![0_usize; table_count];

    for (child_index, table) in schema.tables.iter().enumerate() {
        for fk in &table.foreign_keys {
            let Some(parent_index) = table_index.get(&fk.referenced_table).copied() else {
                continue;
            };
            if parent_index == child_index {
                continue;
            }
            if seen_edges.insert((parent_index, child_index)) {
                dependents[parent_index].push(child_index);
                indegree[child_index] += 1;
            }
        }
    }

    let mut ready = VecDeque::new();
    for (index, degree) in indegree.iter().enumerate() {
        if *degree == 0 {
            ready.push_back(index);
        }
    }

    let mut ordered = Vec::with_capacity(table_count);
    while let Some(index) = ready.pop_front() {
        ordered.push(index);
        dependents[index].sort_unstable();
        for child_index in &dependents[index] {
            indegree[*child_index] -= 1;
            if indegree[*child_index] == 0 {
                ready.push_back(*child_index);
            }
        }
    }

    if ordered.len() == table_count {
        return (ordered, Vec::new());
    }

    let ordered_set = ordered.iter().copied().collect::<BTreeSet<_>>();
    let cyclic = (0..table_count)
        .filter(|index| !ordered_set.contains(index))
        .map(|index| schema.tables[index].name.clone())
        .collect::<Vec<_>>();
    for index in 0..table_count {
        if !ordered_set.contains(&index) {
            ordered.push(index);
        }
    }
    (ordered, cyclic)
}

fn parse_schema(value: &Value) -> Result<NormalizedSchema, serde_json::Error> {
    serde_json::from_value(value.clone())
}

fn parse_options(payload: &Value) -> MigrationOptions {
    payload
        .get("execution_options")
        .and_then(|value| serde_json::from_value::<MigrationOptions>(value.clone()).ok())
        .unwrap_or(MigrationOptions {
            mode: default_mode(),
            chunk_size: default_chunk_size(),
            cancel_after_chunks: None,
            cleanup_before_migrate: false,
        })
}

fn string_list(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|item| !item.is_empty())
                .map(ToString::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn safe_dump_component(value: &str) -> String {
    let mut safe = String::new();
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.') {
            safe.push(ch);
        } else {
            safe.push('_');
        }
    }
    if safe.is_empty() {
        "table".to_string()
    } else {
        safe
    }
}

fn single_numeric_primary_key(table: &NormalizedTable) -> Option<&str> {
    let primary_columns = table
        .columns
        .iter()
        .filter(|column| column.primary_key)
        .collect::<Vec<_>>();
    if primary_columns.len() != 1 {
        return None;
    }
    let column = primary_columns[0];
    if is_integer_key_type(&column.type_name) {
        Some(column.name.as_str())
    } else {
        None
    }
}

fn is_integer_key_type(type_name: &str) -> bool {
    let type_name = type_name.trim().to_ascii_lowercase();
    type_name.starts_with("tinyint")
        || type_name.starts_with("smallint")
        || type_name.starts_with("mediumint")
        || type_name.starts_with("int")
        || type_name.starts_with("integer")
        || type_name.starts_with("bigint")
        || type_name.starts_with("serial")
}

fn should_use_pk_range_dump(table: &NormalizedTable, row_count: u64, chunk_size: usize) -> bool {
    let threshold = (chunk_size as u64).saturating_mul(2);
    row_count >= threshold && single_numeric_primary_key(table).is_some()
}

fn should_use_pk_range_dump_for_span(
    table: &NormalizedTable,
    row_count: u64,
    chunk_size: usize,
    min_key: i128,
    max_key: i128,
) -> bool {
    if !should_use_pk_range_dump(table, row_count, chunk_size) || min_key > max_key {
        return false;
    }

    let span = max_key.saturating_sub(min_key).saturating_add(1) as u128;
    let row_capacity = (row_count as u128).saturating_mul(MYSQL_PK_RANGE_MAX_SPAN_TO_ROW_RATIO);
    span <= row_capacity
}

fn mysql_range_chunk_size_for_avg_row(fallback_chunk_size: usize, avg_row_bytes: u64) -> usize {
    let fallback_chunk_size = fallback_chunk_size.max(1);
    if avg_row_bytes == 0 {
        return fallback_chunk_size;
    }

    let byte_target_rows =
        MYSQL_DUMP_TARGET_BYTES_PER_CHUNK.saturating_add(avg_row_bytes - 1) / avg_row_bytes;
    byte_target_rows
        .max(1)
        .min(fallback_chunk_size as u64)
        .max(1) as usize
}

fn learned_mysql_range_chunk_size(
    fallback_chunk_size: usize,
    avg_row_bytes: u64,
    profile: Option<&DumpTablePerfProfile>,
) -> usize {
    let byte_target_size = mysql_range_chunk_size_for_avg_row(fallback_chunk_size, avg_row_bytes);
    let Some(profile) = profile else {
        return byte_target_size;
    };
    if avg_row_bytes >= 4_096 && profile.chunk_rows >= byte_target_size {
        return profile.chunk_rows.max(1).min(fallback_chunk_size.max(1));
    }
    byte_target_size
}

fn mysql_table_avg_row_length(
    conn: &mut mysql::PooledConn,
    endpoint: &Endpoint,
    table: &str,
) -> u64 {
    let schema_name = endpoint_schema(endpoint);
    let sql = format!(
        "SELECT COALESCE(AVG_ROW_LENGTH, 0) FROM information_schema.tables WHERE TABLE_SCHEMA = {} AND TABLE_NAME = {}",
        sql_literal(&Value::String(schema_name)),
        sql_literal(&Value::String(table.to_string()))
    );
    conn.query_first::<u64, _>(sql).ok().flatten().unwrap_or(0)
}

fn mysql_numeric_min_max(
    conn: &mut mysql::PooledConn,
    table: &str,
    column: &str,
) -> Result<Option<(i128, i128)>, String> {
    let sql = format!(
        "SELECT CAST(MIN({}) AS CHAR), CAST(MAX({}) AS CHAR) FROM {}",
        quote_ident("mysql", column),
        quote_ident("mysql", column),
        quote_ident("mysql", table)
    );
    let result = conn
        .query_first::<(Option<String>, Option<String>), _>(sql)
        .map_err(|err| format!("mysql pk range inspect error: {err}"))?;
    let Some((Some(min), Some(max))) = result else {
        return Ok(None);
    };
    let min = min
        .parse::<i128>()
        .map_err(|err| format!("mysql pk min parse error: {err}"))?;
    let max = max
        .parse::<i128>()
        .map_err(|err| format!("mysql pk max parse error: {err}"))?;
    Ok(Some((min, max)))
}

fn pk_ranges(min_key: i128, max_key: i128, row_count: u64, chunk_size: usize) -> Vec<DumpRange> {
    let chunk_count = ((row_count as usize).saturating_add(chunk_size.saturating_sub(1))
        / chunk_size.max(1))
    .max(1);
    let span = max_key.saturating_sub(min_key).saturating_add(1);
    let width = ((span + chunk_count as i128 - 1) / chunk_count as i128).max(1);
    let mut ranges = Vec::new();
    let mut start = min_key;
    let mut chunk_index = 1_u64;
    while start <= max_key {
        let end = start.saturating_add(width - 1).min(max_key);
        ranges.push(DumpRange {
            chunk_index,
            start,
            end,
        });
        chunk_index += 1;
        if end == max_key {
            break;
        }
        start = end + 1;
    }
    ranges
}

fn current_unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn write_dump_manifest(output_path: &Path, manifest: &DumpManifest) -> Result<(), String> {
    let marker_path = output_path.join(DUMP_DIR_MARKER);
    let marker_file =
        File::create(&marker_path).map_err(|err| format!("failed to create dump marker: {err}"))?;
    serde_json::to_writer_pretty(
        marker_file,
        &json!({
            "format": "tunnelforge-dump-dir",
            "created_by": "tunnelforge-core",
            "version": 1
        }),
    )
    .map_err(|err| format!("failed to write dump marker: {err}"))?;
    let path = output_path.join("_tunnelforge_dump.json");
    let file =
        File::create(&path).map_err(|err| format!("failed to create dump manifest: {err}"))?;
    serde_json::to_writer_pretty(file, manifest)
        .map_err(|err| format!("failed to write dump manifest: {err}"))
}

fn read_dump_manifest(input_path: &Path) -> Result<DumpManifest, String> {
    let path = input_path.join("_tunnelforge_dump.json");
    let file = File::open(&path).map_err(|err| format!("failed to open dump manifest: {err}"))?;
    let manifest: DumpManifest = serde_json::from_reader(file)
        .map_err(|err| format!("failed to parse dump manifest: {err}"))?;
    for table in &manifest.tables {
        validate_dump_table_path(&table.path)?;
    }
    Ok(manifest)
}

fn validate_dump_table_path(path: &str) -> Result<(), String> {
    let table_path = Path::new(path);
    if path.trim().is_empty() || table_path.is_absolute() {
        return Err(format!("unsafe dump table path: {path}"));
    }
    for component in table_path.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            _ => return Err(format!("unsafe dump table path: {path}")),
        }
    }
    Ok(())
}

fn dump_manifest_chunk_path(
    input_path: &Path,
    table_path: &str,
    chunk_index: u64,
    data_format: &str,
    compression: &str,
) -> Result<PathBuf, String> {
    validate_dump_table_path(table_path)?;
    let base_path = fs::canonicalize(input_path)
        .map_err(|err| format!("failed to validate dump input_dir: {err}"))?;
    let raw_path =
        input_path
            .join(table_path)
            .join(dump_chunk_name(chunk_index, data_format, compression));
    let chunk_path = fs::canonicalize(&raw_path)
        .map_err(|err| format!("failed to validate dump chunk: {err}"))?;
    if !chunk_path.starts_with(&base_path) {
        return Err(format!(
            "dump chunk path is outside dump directory: {}",
            raw_path.display()
        ));
    }
    if !chunk_path.is_file() {
        return Err(format!(
            "dump chunk path is not a file: {}",
            raw_path.display()
        ));
    }
    Ok(chunk_path)
}

fn classified_import_error(code: &str, message: &str, scope: Option<&str>) -> String {
    match scope.filter(|value| !value.trim().is_empty()) {
        Some(scope) => format!("{code}: {scope}: {message}"),
        None => format!("{code}: {message}"),
    }
}

fn dump_import_ddl_error(operation: &str, table: &str, err: &str) -> String {
    classified_import_error(
        "load_failed",
        &format!("{operation} failed: {err}"),
        Some(table),
    )
}

fn validate_dump_import_manifest_strictness(
    tables: &[DumpTableManifest],
    strict: bool,
) -> Result<Vec<String>, String> {
    let mut warnings = Vec::new();
    for table in tables {
        if table.chunks > 0 && table.chunk_sha256.len() < table.chunks as usize {
            let message = if table.chunk_sha256.is_empty() {
                format!(
                    "table {} has chunks but no chunk_sha256 metadata",
                    table.name
                )
            } else {
                format!(
                    "table {} has {} chunks but only {} chunk_sha256 entries",
                    table.name,
                    table.chunks,
                    table.chunk_sha256.len()
                )
            };
            if strict {
                return Err(classified_import_error(
                    "export_invalid",
                    &format!("missing chunk_sha256; {message}"),
                    Some(&table.name),
                ));
            }
            warnings.push(format!("legacy dump: {message}"));
        }
    }
    Ok(warnings)
}

fn verify_imported_row_counts(
    tables: &[DumpTableManifest],
    imported_rows_by_table: &BTreeMap<String, u64>,
) -> Result<(), String> {
    for table in tables {
        let imported = imported_rows_by_table
            .get(&table.name)
            .copied()
            .unwrap_or(0);
        if imported != table.rows {
            return Err(classified_import_error(
                "post_load_validation_failed",
                &format!("expected {} rows, imported {}", table.rows, imported),
                Some(&table.name),
            ));
        }
    }
    Ok(())
}

fn verify_target_row_counts<A: MigrationAdapter>(
    target: &mut A,
    tables: &[DumpTableManifest],
    mode: &str,
) -> Result<(), String> {
    if !matches!(mode, "replace" | "recreate") {
        return Ok(());
    }
    for table in tables {
        let target_rows = target.row_count(&table.name).map_err(|err| {
            classified_import_error(
                "post_load_validation_failed",
                &format!("target row count verification failed: {err}"),
                Some(&table.name),
            )
        })? as u64;
        if target_rows != table.rows {
            return Err(classified_import_error(
                "post_load_validation_failed",
                &format!("expected {} rows, target has {}", table.rows, target_rows),
                Some(&table.name),
            ));
        }
    }
    Ok(())
}

fn validate_foreign_key_column_compatibility(schema: &NormalizedSchema) -> Result<(), String> {
    for table in &schema.tables {
        for fk in &table.foreign_keys {
            for (column_name, referenced_column_name) in
                fk.columns.iter().zip(fk.referenced_columns.iter())
            {
                let Some(column) = find_schema_column(schema, &table.name, column_name) else {
                    continue;
                };
                let Some(referenced_column) =
                    find_schema_column(schema, &fk.referenced_table, referenced_column_name)
                else {
                    continue;
                };

                let column_fidelity = mysql_character_fidelity(&column.type_name);
                let referenced_fidelity = mysql_character_fidelity(&referenced_column.type_name);

                if let (Some(charset), Some(referenced_charset)) = (
                    column_fidelity.character_set.as_deref(),
                    referenced_fidelity.character_set.as_deref(),
                ) {
                    if !charset.eq_ignore_ascii_case(referenced_charset) {
                        return Err(foreign_key_fidelity_error(
                            fk,
                            column_name,
                            referenced_column_name,
                            "character set",
                            charset,
                            referenced_charset,
                        ));
                    }
                }

                if let (Some(collation), Some(referenced_collation)) = (
                    column_fidelity.collation.as_deref(),
                    referenced_fidelity.collation.as_deref(),
                ) {
                    if !collation.eq_ignore_ascii_case(referenced_collation) {
                        return Err(foreign_key_fidelity_error(
                            fk,
                            column_name,
                            referenced_column_name,
                            "collation",
                            collation,
                            referenced_collation,
                        ));
                    }
                }
            }
        }
    }
    Ok(())
}

fn find_schema_column<'a>(
    schema: &'a NormalizedSchema,
    table_name: &str,
    column_name: &str,
) -> Option<&'a NormalizedColumn> {
    schema
        .tables
        .iter()
        .find(|table| table.name == table_name)
        .and_then(|table| {
            table
                .columns
                .iter()
                .find(|column| column.name == column_name)
        })
}

fn foreign_key_fidelity_error(
    fk: &NormalizedForeignKey,
    column_name: &str,
    referenced_column_name: &str,
    property: &str,
    value: &str,
    referenced_value: &str,
) -> String {
    classified_import_error(
        "post_load_validation_failed",
        &format!(
            "foreign key column {column_name} {property} {value} is incompatible with referenced column {referenced_column_name} {property} {referenced_value}"
        ),
        Some(&fk.name),
    )
}

fn dump_import_report_path(input_path: &Path) -> Result<PathBuf, String> {
    if input_path.as_os_str().is_empty() {
        return Err("cannot write import report without input_dir".to_string());
    }
    Ok(input_path.join("_tunnelforge_import_report.json"))
}

fn write_dump_import_report(input_path: &Path, report: &Value) -> Result<(), String> {
    let report_path = dump_import_report_path(input_path)?;
    let bytes = serde_json::to_vec_pretty(report)
        .map_err(|err| format!("cannot serialize import report: {err}"))?;
    fs::write(&report_path, bytes).map_err(|err| {
        format!(
            "cannot write import report {}: {err}",
            report_path.display()
        )
    })
}

fn validate_dump_manifest_chunks(
    input_path: &Path,
    tables: &[DumpTableManifest],
    data_format: &str,
    compression: &str,
) -> Result<(), String> {
    for table in tables {
        for chunk_index in 1..=table.chunks {
            let chunk_name = dump_chunk_name(chunk_index, data_format, compression);
            let chunk_path = dump_manifest_chunk_path(
                input_path,
                &table.path,
                chunk_index,
                data_format,
                compression,
            )?;
            if let Some(expected) = table.chunk_sha256.get(&chunk_name) {
                let actual = sha256_file(&chunk_path)?;
                if !expected.eq_ignore_ascii_case(&actual) {
                    return Err(format!(
                        "dump chunk checksum mismatch: {} expected {} got {}",
                        chunk_path.display(),
                        expected,
                        actual
                    ));
                }
            }
        }
    }
    Ok(())
}

fn dump_chunk_name(index: u64, data_format: &str, compression: &str) -> String {
    let extension = if data_format == "tsv" { "tsv" } else { "jsonl" };
    if compression == "zstd" {
        format!("chunk_{index:06}.{extension}.zst")
    } else {
        format!("chunk_{index:06}.{extension}")
    }
}

fn open_dump_writer(path: &Path, compression: &str) -> Result<Box<dyn Write>, String> {
    let file = File::create(path).map_err(|err| format!("failed to create dump chunk: {err}"))?;
    let writer = BufWriter::new(file);
    match compression {
        "none" => Ok(Box::new(writer)),
        "zstd" => zstd::stream::write::Encoder::new(writer, MYSQL_DUMP_ZSTD_LEVEL)
            .map(|encoder| Box::new(encoder.auto_finish()) as Box<dyn Write>)
            .map_err(|err| format!("failed to create zstd dump encoder: {err}")),
        other => Err(format!("unsupported dump compression: {other}")),
    }
}

fn open_dump_reader(path: &Path, compression: &str) -> Result<Box<dyn BufRead>, String> {
    let file = File::open(path).map_err(|err| format!("failed to open dump chunk: {err}"))?;
    match compression {
        "none" => Ok(Box::new(BufReader::new(file))),
        "zstd" => zstd::stream::read::Decoder::new(file)
            .map(|decoder| Box::new(BufReader::new(decoder)) as Box<dyn BufRead>)
            .map_err(|err| format!("failed to create zstd dump decoder: {err}")),
        other => Err(format!("unsupported dump compression: {other}")),
    }
}

fn write_dump_rows(
    path: &Path,
    table: &NormalizedTable,
    rows: &[Value],
    data_format: &str,
    compression: &str,
) -> Result<String, String> {
    if data_format == "tsv" {
        write_tsv_rows(path, table, rows, compression)
    } else {
        write_jsonl_rows(path, rows, compression)
    }
}

fn write_dump_row<W: Write>(
    writer: &mut W,
    table: &NormalizedTable,
    row: &Value,
    data_format: &str,
) -> Result<(), String> {
    if data_format == "tsv" {
        write_tsv_row(writer, table, row)
    } else {
        serde_json::to_writer(&mut *writer, row)
            .map_err(|err| format!("failed to encode dump row: {err}"))?;
        writer
            .write_all(b"\n")
            .map_err(|err| format!("failed to write dump row: {err}"))
    }
}

fn read_dump_rows(
    path: &Path,
    table: &NormalizedTable,
    data_format: &str,
    compression: &str,
) -> Result<Vec<Value>, String> {
    if data_format == "tsv" {
        read_tsv_rows(path, table, compression)
    } else {
        read_jsonl_rows(path, compression)
    }
}

fn write_jsonl_rows(path: &Path, rows: &[Value], compression: &str) -> Result<String, String> {
    {
        let mut file = open_dump_writer(path, compression)?;
        for row in rows {
            serde_json::to_writer(&mut file, row)
                .map_err(|err| format!("failed to encode dump row: {err}"))?;
            file.write_all(b"\n")
                .map_err(|err| format!("failed to write dump row: {err}"))?;
        }
    }
    sha256_file(path)
}

fn write_tsv_rows(
    path: &Path,
    table: &NormalizedTable,
    rows: &[Value],
    compression: &str,
) -> Result<String, String> {
    {
        let mut file = open_dump_writer(path, compression)?;
        for row in rows {
            write_tsv_row(&mut file, table, row)?;
        }
    }
    sha256_file(path)
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|err| format!("failed to open dump chunk: {err}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|err| format!("failed to read dump chunk: {err}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn write_tsv_row<W: Write>(
    writer: &mut W,
    table: &NormalizedTable,
    row: &Value,
) -> Result<(), String> {
    let object = row.as_object();
    for (index, column) in table.columns.iter().enumerate() {
        if index > 0 {
            writer
                .write_all(b"\t")
                .map_err(|err| format!("failed to write dump row: {err}"))?;
        }
        let value = object
            .and_then(|object| object.get(&column.name))
            .unwrap_or(&Value::Null);
        let field = tsv_field(value);
        writer
            .write_all(field.as_bytes())
            .map_err(|err| format!("failed to write dump row: {err}"))?;
    }
    writer
        .write_all(b"\n")
        .map_err(|err| format!("failed to write dump row: {err}"))
}

fn tsv_field(value: &Value) -> String {
    if value.is_null() {
        return "\\N".to_string();
    }
    let text = match value {
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => value.clone(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
        Value::Null => unreachable!(),
    };
    escape_tsv_text(&text)
}

fn escape_tsv_text(text: &str) -> String {
    text.replace('\\', "\\\\")
        .replace('\t', "\\t")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

fn read_tsv_rows(
    path: &Path,
    table: &NormalizedTable,
    compression: &str,
) -> Result<Vec<Value>, String> {
    let reader = open_dump_reader(path, compression)?;
    let mut rows = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|err| format!("failed to read dump row: {err}"))?;
        if line.is_empty() {
            continue;
        }
        rows.push(tsv_line_to_row(&line, table));
    }
    Ok(rows)
}

fn stream_tsv_rows_in_batches<F: FnMut(&[Value]) -> Result<(), String>>(
    path: &Path,
    table: &NormalizedTable,
    compression: &str,
    max_rows: usize,
    max_bytes: usize,
    mut insert_batch: F,
) -> Result<u64, String> {
    let reader = open_dump_reader(path, compression)?;
    let max_rows = max_rows.max(1);
    let max_bytes = max_bytes.max(1024);
    let mut batch = Vec::new();
    let mut batch_bytes = 0_usize;
    let mut total_rows = 0_u64;

    for line in reader.lines() {
        let line = line.map_err(|err| format!("failed to read dump row: {err}"))?;
        if line.is_empty() {
            continue;
        }
        let row_bytes = line.len() + 1;
        if !batch.is_empty() && (batch.len() >= max_rows || batch_bytes + row_bytes > max_bytes) {
            insert_batch(&batch)?;
            total_rows += batch.len() as u64;
            batch.clear();
            batch_bytes = 0;
        }
        batch.push(tsv_line_to_row(&line, table));
        batch_bytes += row_bytes;
    }

    if !batch.is_empty() {
        insert_batch(&batch)?;
        total_rows += batch.len() as u64;
    }

    Ok(total_rows)
}

fn tsv_line_to_row(line: &str, table: &NormalizedTable) -> Value {
    let columns = column_names(table);
    let fields = split_tsv_line(line);
    let mut object = Map::new();
    for (index, column) in columns.iter().enumerate() {
        let value = fields
            .get(index)
            .map(|field| unescape_tsv_field(field))
            .unwrap_or(Value::Null);
        object.insert(column.clone(), value);
    }
    Value::Object(object)
}

fn split_tsv_line(line: &str) -> Vec<String> {
    line.split('\t').map(ToString::to_string).collect()
}

fn unescape_tsv_field(field: &str) -> Value {
    if field == "\\N" {
        return Value::Null;
    }
    let mut output = String::new();
    let mut chars = field.chars();
    while let Some(ch) = chars.next() {
        if ch != '\\' {
            output.push(ch);
            continue;
        }
        match chars.next() {
            Some('t') => output.push('\t'),
            Some('n') => output.push('\n'),
            Some('r') => output.push('\r'),
            Some('\\') => output.push('\\'),
            Some(other) => {
                output.push('\\');
                output.push(other);
            }
            None => output.push('\\'),
        }
    }
    Value::String(output)
}

fn read_jsonl_rows(path: &Path, compression: &str) -> Result<Vec<Value>, String> {
    let reader = open_dump_reader(path, compression)?;
    let mut rows = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|err| format!("failed to read dump row: {err}"))?;
        if line.trim().is_empty() {
            continue;
        }
        rows.push(
            serde_json::from_str(&line)
                .map_err(|err| format!("failed to parse dump row: {err}"))?,
        );
    }
    Ok(rows)
}

fn read_engine(payload: &Value, key: &str) -> String {
    payload
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase()
}

fn is_supported_direction(source: &str, target: &str) -> bool {
    matches!(
        (source, target),
        ("mysql", "postgresql") | ("postgresql", "mysql")
    )
}

fn unsupported_objects(payload: &Value) -> Vec<String> {
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

pub fn generate_schema_ddl(schema: &NormalizedSchema, source: &str, target: &str) -> Vec<String> {
    schema
        .tables
        .iter()
        .filter_map(|table| generate_table_ddl(table, source, target))
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

fn should_apply_post_load_ddl(mode: &str) -> bool {
    matches!(mode, "replace" | "recreate")
}

fn post_load_ddl_skip_message(mode: &str) -> String {
    format!("skipping post-load DDL for {mode} import; existing objects must already match")
}

fn apply_post_load_ddl<A: MigrationAdapter>(
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
    for sql in generate_post_data_ddl(schema, target_engine) {
        target
            .execute_sql(&sql)
            .map_err(|err| post_load_ddl_error(&sql, &err))?;
    }
    Ok(())
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

fn copy_rows_to_postgres(
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

fn format_postgres_error(context: &str, err: &postgres::Error) -> String {
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

fn sanitize_postgresql_text(value: &str) -> String {
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

fn has_binary_columns(table: &NormalizedTable) -> bool {
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
        "SELECT TABLE_NAME AS table_name FROM information_schema.tables WHERE table_schema = ? AND table_type = 'BASE TABLE' ORDER BY TABLE_NAME"
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

fn apply_key_flags(
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

fn group_indexes(rows: Vec<(String, String, bool)>) -> Vec<NormalizedIndex> {
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

fn group_foreign_keys(rows: Vec<(String, String, String, String)>) -> Vec<NormalizedForeignKey> {
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

fn generate_table_ddl(table: &NormalizedTable, source: &str, target: &str) -> Option<String> {
    let mut lines = Vec::new();
    let mut primary_keys = Vec::new();

    for column in &table.columns {
        let auto_increment = is_auto_increment_type(&column.type_name);
        let mapped_type = map_type(source, target, &strip_generation_marker(&column.type_name));
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

    Some(format!(
        "CREATE TABLE {} (\n{}\n);",
        quote_ident(target, &table.name),
        lines.join(",\n")
    ))
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
    if (value.starts_with('\'') && value.ends_with('\'')) || value.starts_with("b'") {
        value.to_string()
    } else {
        format!("'{}'", value.replace('\'', "''"))
    }
}

fn strip_postgresql_type_cast(value: &str) -> &str {
    value
        .split_once("::")
        .map(|(literal, _)| literal)
        .unwrap_or(value)
        .trim()
}

fn with_auto_increment_marker(type_name: &str, extra: &str) -> String {
    if extra.to_ascii_lowercase().contains("auto_increment") {
        format!("{type_name} auto_increment")
    } else {
        type_name.to_string()
    }
}

fn mysql_type_with_character_options(
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
struct MysqlCharacterFidelity {
    character_set: Option<String>,
    collation: Option<String>,
}

fn mysql_character_fidelity(type_name: &str) -> MysqlCharacterFidelity {
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

fn with_postgresql_identity_marker(
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

fn normalize_postgresql_default(column_default: Option<&str>, is_identity: &str) -> Option<String> {
    if is_identity.eq_ignore_ascii_case("YES") {
        return None;
    }
    let default_value = column_default?.trim();
    if default_value.to_ascii_lowercase().contains("nextval(") {
        return None;
    }
    Some(strip_postgresql_type_cast(default_value).to_string())
}

fn is_auto_increment_type(type_name: &str) -> bool {
    let type_name = type_name.to_ascii_lowercase();
    type_name.contains("auto_increment")
        || type_name.contains(" identity")
        || type_name == "serial"
        || type_name == "bigserial"
}

fn strip_generation_marker(type_name: &str) -> String {
    let mut cleaned = type_name.to_string();
    for marker in [" auto_increment", " identity"] {
        cleaned = cleaned.replace(marker, "");
    }
    cleaned
}

fn quote_ident(engine: &str, ident: &str) -> String {
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

fn drop_table_sql(engine: &str, table: &str) -> String {
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

    fn schema() -> NormalizedSchema {
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
            }],
        }
    }

    fn empty_table(name: &str, foreign_keys: Vec<NormalizedForeignKey>) -> NormalizedTable {
        NormalizedTable {
            name: name.to_string(),
            columns: Vec::new(),
            indexes: Vec::new(),
            foreign_keys,
        }
    }

    fn fk(name: &str, referenced_table: &str) -> NormalizedForeignKey {
        NormalizedForeignKey {
            name: name.to_string(),
            columns: vec!["parent_id".to_string()],
            referenced_table: referenced_table.to_string(),
            referenced_columns: vec!["id".to_string()],
        }
    }

    #[derive(Default)]
    struct RecordingAdapter {
        executed_sql: Vec<String>,
        row_counts: BTreeMap<String, usize>,
        fail_sql_contains: Option<String>,
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

    #[test]
    fn service_hello_advertises_core_protocol() {
        let result = handle_request(Request {
            command: "service.hello".to_string(),
            request_id: Some("hello-1".to_string()),
            payload: json!({}),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["request_id"], "hello-1");
        assert_eq!(result["command"], "service.hello");
        assert_eq!(result["service"], "tunnelforge-core");
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("migration.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("dump.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("dump.import")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.run")));
    }

    #[test]
    fn oneclick_recommend_contract_returns_manual_steps() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-1".to_string()),
            payload: json!({
                "issues": [{
                    "severity": "warning",
                    "location": "backup",
                    "message": "Backup confirmation was not provided.",
                    "suggestion": "Confirm backup.",
                    "blocking": false
                }]
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "oneclick.recommend");
        assert_eq!(result["success"], true);
        assert_eq!(result["summary"]["manual_review"], 1);
        assert_eq!(result["steps"][0]["selected_option"]["strategy"], "manual");
    }

    #[test]
    fn oneclick_apply_fixes_defaults_to_dry_run() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-1".to_string()),
            payload: json!({"steps": [{"location": "backup"}]}),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "oneclick.apply_fixes");
        assert_eq!(result["success"], true);
        assert_eq!(result["dry_run"], true);
        assert_eq!(result["skip_count"], 1);
    }

    #[test]
    fn dump_manifest_and_jsonl_rows_roundtrip() {
        let dir =
            std::env::temp_dir().join(format!("tunnelforge-dump-test-{}", current_unix_seconds()));
        fs::create_dir_all(&dir).unwrap();

        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 1,
            data_format: "jsonl".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 2,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };
        write_dump_manifest(&dir, &manifest).unwrap();
        assert_eq!(read_dump_manifest(&dir).unwrap(), manifest);

        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        let rows = vec![json!({"id": 1}), json!({"id": 2})];
        let chunk_path = table_dir.join("chunk_000001.jsonl");
        write_jsonl_rows(&chunk_path, &rows, "none").unwrap();
        assert_eq!(read_jsonl_rows(&chunk_path, "none").unwrap(), rows);

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_rejects_table_paths_outside_dump_dir() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-traversal-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();

        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "zstd".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "../outside".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };
        write_dump_manifest(&dir, &manifest).unwrap();

        let err = read_dump_manifest(&dir).unwrap_err();

        assert!(err.contains("unsafe dump table path"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_validation_rejects_symlinked_chunk_outside_dump_dir() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-symlink-test-{}",
            current_unix_seconds()
        ));
        let outside = std::env::temp_dir().join(format!(
            "tunnelforge-dump-symlink-outside-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("chunk_000001.tsv"), b"1\toutside\n").unwrap();
        let link_dir = dir.join("0001_users");
        #[cfg(windows)]
        let link_result = std::os::windows::fs::symlink_dir(&outside, &link_dir);
        #[cfg(unix)]
        let link_result = std::os::unix::fs::symlink(&outside, &link_dir);
        if link_result.is_err() {
            fs::remove_dir_all(&dir).ok();
            fs::remove_dir_all(&outside).ok();
            return;
        }
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };

        let err = validate_dump_manifest_chunks(&dir, &manifest.tables, "tsv", "none").unwrap_err();

        assert!(err.contains("outside dump directory"));
        fs::remove_dir_all(&dir).ok();
        fs::remove_dir_all(&outside).ok();
    }

    #[test]
    fn dump_manifest_validation_rejects_missing_chunk_before_import() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-missing-chunk-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(dir.join("0001_users")).unwrap();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
        };

        let err = validate_dump_manifest_chunks(&dir, &manifest.tables, "tsv", "none").unwrap_err();

        assert!(err.contains("failed to validate dump chunk"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_validation_rejects_checksum_mismatch() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-checksum-test-{}",
            current_unix_seconds()
        ));
        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        fs::write(table_dir.join("chunk_000001.tsv"), b"1\tactual\n").unwrap();
        let mut checksums = BTreeMap::new();
        checksums.insert("chunk_000001.tsv".to_string(), "00".repeat(32));
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: checksums,
            }],
        };

        let err = validate_dump_manifest_chunks(&dir, &manifest.tables, "tsv", "none").unwrap_err();

        assert!(err.contains("checksum mismatch"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_manifest_validation_accepts_matching_chunk_checksum() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-checksum-ok-test-{}",
            current_unix_seconds()
        ));
        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        let table = schema().tables[0].clone();
        let chunk_path = table_dir.join("chunk_000001.tsv");
        let checksum = write_dump_rows(
            &chunk_path,
            &table,
            &[json!({"id": 1, "name": "actual"})],
            "tsv",
            "none",
        )
        .unwrap();
        let mut checksums = BTreeMap::new();
        checksums.insert("chunk_000001.tsv".to_string(), checksum);
        let manifest = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 1,
            chunks: 1,
            chunk_sha256: checksums,
        };

        validate_dump_manifest_chunks(&dir, &[manifest], "tsv", "none").unwrap();

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn mysql_dump_import_defaults_to_safe_local_infile_policy() {
        assert_eq!(
            mysql_local_infile_policy_from_payload(&json!({})).unwrap(),
            "fallback"
        );
        assert_eq!(
            mysql_local_infile_policy_from_payload(&json!({
                "mysql_local_infile_policy": "temporary_global"
            }))
            .unwrap(),
            "temporary_global"
        );
        assert!(mysql_local_infile_policy_from_payload(&json!({
            "mysql_local_infile_policy": "always"
        }))
        .is_err());
    }

    #[test]
    fn import_timezone_sql_accepts_session_time_zone_only() {
        assert_eq!(
            validated_timezone_sql(Some("SET SESSION time_zone = '+09:00'")).unwrap(),
            Some("SET SESSION time_zone = '+09:00'".to_string())
        );
        assert_eq!(validated_timezone_sql(None).unwrap(), None);
        assert_eq!(validated_timezone_sql(Some("   ")).unwrap(), None);
        assert!(validated_timezone_sql(Some("DROP DATABASE prod")).is_err());
        assert!(
            validated_timezone_sql(Some("SET SESSION time_zone = '+09:00'; DROP TABLE users"))
                .is_err()
        );
        assert!(
            validated_timezone_sql(Some("SET SESSION time_zone = '+09:00' -- trailing")).is_err()
        );
        assert!(validated_timezone_sql(Some("SET GLOBAL time_zone = '+09:00'")).is_err());
    }

    #[test]
    fn strict_manifest_validation_rejects_missing_chunk_checksums() {
        let table = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 10,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        };

        let err = validate_dump_import_manifest_strictness(&[table], true).unwrap_err();

        assert!(err.contains("export_invalid"));
        assert!(err.contains("users"));
        assert!(err.contains("missing chunk_sha256"));
    }

    #[test]
    fn legacy_manifest_validation_allows_missing_checksums_when_not_strict() {
        let table = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 10,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        };

        let warnings = validate_dump_import_manifest_strictness(&[table], false).unwrap();

        assert_eq!(
            warnings,
            vec!["legacy dump: table users has chunks but no chunk_sha256 metadata".to_string()]
        );
    }

    #[test]
    fn classified_import_error_formats_code_scope_and_message() {
        let err = classified_import_error(
            "import_plan_invalid",
            "full replacement worker target is unresolved",
            Some("users"),
        );

        assert_eq!(
            err,
            "import_plan_invalid: users: full replacement worker target is unresolved"
        );
    }

    #[test]
    fn dump_import_ddl_error_includes_classification_table_and_operation() {
        let err = dump_import_ddl_error("create_table", "users", "mysql create table error");

        assert_eq!(
            err,
            "load_failed: users: create_table failed: mysql create table error"
        );
    }

    #[test]
    fn dump_import_strict_manifest_rejects_missing_checksums_before_connect() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-strict-import-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = schema().tables[0].clone();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 50_000,
            created_unix_seconds: 1,
            tables: vec![DumpTableManifest {
                name: table.name,
                path: "0001_users".to_string(),
                rows: 10,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            }],
            views: Vec::new(),
        };
        write_dump_manifest(&dir, &manifest).unwrap();

        let events = handle_request(Request {
            command: "dump.import".to_string(),
            request_id: Some("strict-import".to_string()),
            payload: json!({
                "input_dir": dir.to_string_lossy(),
                "target": {
                    "engine": "mysql",
                    "host": "127.0.0.1",
                    "port": 1,
                    "user": "root",
                    "password": "",
                    "database": "app"
                }
            }),
        });

        let message = events
            .iter()
            .find(|event| event.get("event") == Some(&json!("error")))
            .and_then(|event| event.get("message"))
            .and_then(Value::as_str)
            .unwrap();
        assert!(message.contains("export_invalid"));
        assert!(message.contains("missing chunk_sha256"));

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn import_row_count_verification_rejects_missing_rows() {
        let tables = vec![DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 3,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        let mut imported = BTreeMap::new();
        imported.insert("users".to_string(), 2_u64);

        let err = verify_imported_row_counts(&tables, &imported).unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("users"));
        assert!(err.contains("expected 3 rows, imported 2"));
    }

    #[test]
    fn import_row_count_verification_accepts_matching_counts() {
        let tables = vec![DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 3,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        let mut imported = BTreeMap::new();
        imported.insert("users".to_string(), 3_u64);

        verify_imported_row_counts(&tables, &imported).unwrap();
    }

    #[test]
    fn replace_import_target_row_verification_rejects_extra_rows() {
        let tables = vec![DumpTableManifest {
            name: "login_attempts".to_string(),
            path: "0001_login_attempts".to_string(),
            rows: 77_198,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        let mut adapter = RecordingAdapter {
            row_counts: BTreeMap::from([("login_attempts".to_string(), 77_208)]),
            ..RecordingAdapter::default()
        };

        let err = verify_target_row_counts(&mut adapter, &tables, "replace").unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("login_attempts"));
        assert!(err.contains("expected 77198 rows, target has 77208"));
    }

    #[test]
    fn merge_import_target_row_verification_allows_existing_rows() {
        let tables = vec![DumpTableManifest {
            name: "login_attempts".to_string(),
            path: "0001_login_attempts".to_string(),
            rows: 77_198,
            chunks: 1,
            chunk_sha256: BTreeMap::new(),
        }];
        let mut adapter = RecordingAdapter {
            row_counts: BTreeMap::from([("login_attempts".to_string(), 77_208)]),
            ..RecordingAdapter::default()
        };

        verify_target_row_counts(&mut adapter, &tables, "merge").unwrap();
    }

    #[test]
    fn fk_schema_fidelity_rejects_incompatible_text_collations() {
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
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "df_evaluation_results_ibfk_3".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        referenced_table: "audit_category".to_string(),
                        referenced_columns: vec!["code".to_string()],
                    }],
                },
            ],
        };

        let err = validate_foreign_key_column_compatibility(&schema).unwrap_err();

        assert!(err.contains("post_load_validation_failed"));
        assert!(err.contains("df_evaluation_results_ibfk_3"));
        assert!(err.contains("audit_category_code"));
        assert!(err.contains("code"));
    }

    #[test]
    fn fk_schema_fidelity_accepts_matching_text_collations() {
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
                },
                NormalizedTable {
                    name: "df_evaluation_results".to_string(),
                    columns: vec![NormalizedColumn {
                        name: "audit_category_code".to_string(),
                        type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                            .to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    }],
                    indexes: Vec::new(),
                    foreign_keys: vec![NormalizedForeignKey {
                        name: "df_evaluation_results_ibfk_3".to_string(),
                        columns: vec!["audit_category_code".to_string()],
                        referenced_table: "audit_category".to_string(),
                        referenced_columns: vec!["code".to_string()],
                    }],
                },
            ],
        };

        validate_foreign_key_column_compatibility(&schema).unwrap();
    }

    #[test]
    fn import_report_path_lives_inside_dump_directory() {
        let dir = Path::new("C:/tmp/dump");
        let path = dump_import_report_path(dir).unwrap();

        assert!(path.ends_with("_tunnelforge_import_report.json"));
        assert!(path.starts_with(dir));
    }

    #[test]
    fn write_dump_import_report_creates_json_file() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-import-report-test-{}",
            current_unix_seconds()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        write_dump_import_report(
            &dir,
            &json!({
                "success": true,
                "verification": {"row_counts": "passed"}
            }),
        )
        .unwrap();

        let report_path = dir.join("_tunnelforge_import_report.json");
        let report_text = fs::read_to_string(&report_path).unwrap();
        assert!(report_text.contains("\"row_counts\": \"passed\""));

        fs::remove_dir_all(&dir).unwrap();
    }

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
    fn dump_overwrite_rejects_non_dump_directory() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-overwrite-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let keep_file = dir.join("keep.txt");
        fs::write(&keep_file, "keep").unwrap();

        let err = prepare_dump_output_dir(&dir, true).unwrap_err();

        assert!(err.contains("refusing to overwrite"));
        assert!(keep_file.exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_overwrite_requires_manifest_and_marker() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-marker-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("_tunnelforge_dump.json"),
            r#"{"format":"tunnelforge-dump"}"#,
        )
        .unwrap();

        let err = prepare_dump_output_dir(&dir, true).unwrap_err();

        assert!(err.contains("without TunnelForge dump marker"));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn write_dump_manifest_writes_overwrite_marker() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-write-marker-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: Vec::new(),
        };

        write_dump_manifest(&dir, &manifest).unwrap();

        assert!(dir.join(".tunnelforge_dump_dir").is_file());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_overwrite_allows_marked_dump_directory() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-dump-overwrite-marker-ok-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 2,
            data_format: "tsv".to_string(),
            compression: "none".to_string(),
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            snapshot_policy: "connection_consistent".to_string(),
            strict_export: true,
            manifest_warnings: Vec::new(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            views: Vec::new(),
            tables: Vec::new(),
        };
        write_dump_manifest(&dir, &manifest).unwrap();
        fs::write(dir.join("old_chunk.tsv"), b"old").unwrap();

        prepare_dump_output_dir(&dir, true).unwrap();

        assert!(dir.is_dir());
        assert!(!dir.join("old_chunk.tsv").exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_output_rejects_empty_directory_path() {
        let err = prepare_dump_output_dir(Path::new(""), false).unwrap_err();

        assert!(err.contains("unsafe dump output_dir"));
    }

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
    fn dump_plan_event_reports_table_and_row_totals() {
        let schema = schema();
        let mut counts = BTreeMap::new();
        counts.insert("users".to_string(), 42_u64);

        let event = dump_plan_event(Some("req-1".to_string()), &schema.tables, &counts);

        assert_eq!(event["event"], "dump_plan");
        assert_eq!(event["request_id"], "req-1");
        assert_eq!(event["tables_total"], 1);
        assert_eq!(event["rows_total"], 42);
        assert_eq!(event["tables"][0]["name"], "users");
        assert_eq!(event["tables"][0]["rows"], 42);
    }

    #[test]
    fn dump_import_row_progress_event_reports_table_and_overall_rows() {
        let event = dump_import_row_progress_event(
            Some("import-1".to_string()),
            "orders",
            25,
            100,
            1_000,
            2_000,
            25,
            Some(2),
            Some(8),
            Some(4),
            Some(500),
            "load_data_local_infile",
        );

        assert_eq!(event["event"], "row_progress");
        assert_eq!(event["request_id"], "import-1");
        assert_eq!(event["table"], "orders");
        assert_eq!(event["rows"], 25);
        assert_eq!(event["total"], 100);
        assert_eq!(event["table_rows_done"], 25);
        assert_eq!(event["table_rows_total"], 100);
        assert_eq!(event["overall_rows_done"], 1_025);
        assert_eq!(event["overall_rows_total"], 2_000);
        assert_eq!(event["chunk_rows"], 25);
        assert_eq!(event["chunks_done"], 2);
        assert_eq!(event["chunks_total"], 8);
        assert_eq!(event["chunk_index"], 4);
        assert_eq!(event["load_ms"], 500);
        assert_eq!(event["strategy"], "load_data_local_infile");
    }

    #[test]
    fn dump_schedule_event_reports_adaptive_workers_and_top_tables() {
        let tables = vec![
            empty_table("huge", Vec::new()),
            empty_table("medium", Vec::new()),
            empty_table("tiny", Vec::new()),
        ];
        let mut counts = BTreeMap::new();
        counts.insert("huge".to_string(), 2_000_000);
        counts.insert("medium".to_string(), 500_000);
        counts.insert("tiny".to_string(), 10);
        let limits = adaptive_dump_parallel_limits(8, 3, 50_000, &counts);

        let event = dump_schedule_event(
            Some("req-1".to_string()),
            &tables,
            &counts,
            limits,
            8,
            50_000,
            "tsv",
            "zstd",
            "global_chunk",
        );

        assert_eq!(event["event"], "dump_schedule");
        assert_eq!(event["scheduler"], "global_chunk");
        assert_eq!(event["compression"], "zstd");
        assert_eq!(event["table_workers"], limits.table_workers);
        assert_eq!(
            event["range_workers_per_table"],
            limits.range_workers_per_table
        );
        assert_eq!(event["scheduled_tables"][0]["name"], "huge");
        assert_eq!(event["scheduled_tables"][0]["estimated_chunks"], 40);
    }

    #[test]
    fn full_schema_dump_splits_thread_budget_between_tables_and_ranges() {
        let limits = dump_parallel_limits(16, 208);

        assert_eq!(limits.table_workers, 4);
        assert_eq!(limits.range_workers_per_table, 4);
        assert!(limits.estimated_mysql_connections() <= 20);
    }

    #[test]
    fn eight_thread_full_schema_prefers_range_parallelism_for_large_tables() {
        let limits = dump_parallel_limits(8, 208);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
        assert!(limits.estimated_mysql_connections() <= 10);
    }

    #[test]
    fn adaptive_dump_limits_prioritize_range_workers_for_heavy_chunked_tables() {
        let mut counts = BTreeMap::new();
        counts.insert("huge".to_string(), 2_000_000);
        counts.insert("medium".to_string(), 500_000);
        counts.insert("tiny".to_string(), 10);

        let limits = adaptive_dump_parallel_limits(8, 208, 50_000, &counts);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_use_byte_chunks_for_wide_tables() {
        let mut counts = BTreeMap::new();
        counts.insert("df_subs".to_string(), 223_502);
        counts.insert("tiny".to_string(), 10);
        let mut avg_row_lengths = BTreeMap::new();
        avg_row_lengths.insert("df_subs".to_string(), 9_462);

        let limits =
            adaptive_dump_parallel_limits_with_avg(8, 208, 50_000, &counts, &avg_row_lengths);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_keep_table_parallelism_for_pathological_wide_table() {
        let mut counts = BTreeMap::new();
        counts.insert("df_subs".to_string(), 387_398);
        counts.insert("qe_view_factors_result".to_string(), 1_946_153);
        counts.insert("df_call_logs".to_string(), 1_076_142);
        let mut avg_row_lengths = BTreeMap::new();
        avg_row_lengths.insert("df_subs".to_string(), 9_462);
        avg_row_lengths.insert("qe_view_factors_result".to_string(), 128);
        avg_row_lengths.insert("df_call_logs".to_string(), 128);

        let limits =
            adaptive_dump_parallel_limits_with_avg(8, 208, 50_000, &counts, &avg_row_lengths);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn adaptive_dump_limits_keep_multiple_heavy_tables_in_parallel() {
        let mut counts = BTreeMap::new();
        counts.insert("huge_a".to_string(), 2_000_000);
        counts.insert("huge_b".to_string(), 1_900_000);
        counts.insert("tiny".to_string(), 10);

        let limits = adaptive_dump_parallel_limits(8, 208, 50_000, &counts);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 4);
    }

    #[test]
    fn small_table_selection_keeps_range_parallelism_within_thread_budget() {
        let limits = dump_parallel_limits(16, 2);

        assert_eq!(limits.table_workers, 2);
        assert_eq!(limits.range_workers_per_table, 8);
        assert!(limits.estimated_mysql_connections() <= 18);
    }

    #[test]
    fn single_table_dump_uses_full_range_parallelism() {
        let limits = dump_parallel_limits(16, 1);

        assert_eq!(limits.table_workers, 1);
        assert_eq!(limits.range_workers_per_table, 16);
        assert!(limits.estimated_mysql_connections() <= 17);
    }

    #[test]
    fn dump_scheduler_starts_largest_estimated_tables_first() {
        let mut tables = vec![
            NormalizedTable {
                name: "tiny".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            },
            NormalizedTable {
                name: "huge".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            },
            NormalizedTable {
                name: "medium".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            },
        ];
        let mut counts = BTreeMap::new();
        counts.insert("tiny".to_string(), 10);
        counts.insert("huge".to_string(), 1_000_000);
        counts.insert("medium".to_string(), 50_000);

        tables = dump_schedule_order(&tables, &counts);

        assert_eq!(
            tables
                .iter()
                .map(|table| table.name.as_str())
                .collect::<Vec<_>>(),
            vec!["huge", "medium", "tiny"]
        );
    }

    #[test]
    fn global_dump_work_plan_mixes_range_chunks_and_whole_table_jobs() {
        let mut range_chunks = BTreeMap::new();
        range_chunks.insert("huge".to_string(), 4_u64);
        range_chunks.insert("wide".to_string(), 2_u64);
        let tables = vec![
            empty_table("huge", Vec::new()),
            empty_table("small_lookup", Vec::new()),
            empty_table("wide", Vec::new()),
        ];

        let plan = global_dump_work_plan(&tables, &range_chunks);

        assert_eq!(
            plan.iter()
                .map(|item| item.table.as_str())
                .collect::<Vec<_>>(),
            vec![
                "huge",
                "wide",
                "small_lookup",
                "huge",
                "wide",
                "huge",
                "huge"
            ]
        );
        assert_eq!(
            plan.iter().map(|item| item.chunk_index).collect::<Vec<_>>(),
            vec![Some(1), Some(1), None, Some(2), Some(2), Some(3), Some(4)]
        );
    }

    #[test]
    fn learned_mysql_range_chunk_size_uses_previous_faster_large_chunks_for_wide_tables() {
        let profile = DumpTablePerfProfile {
            avg_row_bytes: 9_462,
            chunk_rows: 50_000,
            rows_per_second: 1_350,
            duration_ms: 165_900,
        };

        assert_eq!(
            learned_mysql_range_chunk_size(50_000, 9_462, Some(&profile)),
            50_000
        );
    }

    #[test]
    fn local_infile_disabled_error_is_detected_for_fallback_import() {
        assert!(is_mysql_local_infile_disabled_error(
            "mysql LOAD DATA error: MySqlError { ERROR 3948 (42000): Loading local data is disabled; this must be enabled on both the client and server sides }"
        ));
        assert!(!is_mysql_local_infile_disabled_error(
            "mysql LOAD DATA error: duplicate key"
        ));
    }

    #[test]
    fn mysql_local_infile_boolean_values_and_set_sql_are_stable() {
        assert!(mysql_bool_value_enabled("ON"));
        assert!(mysql_bool_value_enabled("1"));
        assert!(mysql_bool_value_enabled(" yes "));
        assert!(!mysql_bool_value_enabled("OFF"));
        assert!(!mysql_bool_value_enabled("0"));
        assert_eq!(
            mysql_set_global_local_infile_sql(true),
            "SET GLOBAL local_infile = 1"
        );
        assert_eq!(
            mysql_set_global_local_infile_sql(false),
            "SET GLOBAL local_infile = 0"
        );
    }

    #[test]
    fn tsv_insert_fallback_streams_rows_in_limited_batches() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-tsv-fallback-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = schema().tables[0].clone();
        let path = dir.join("chunk_000001.tsv");
        write_dump_rows(
            &path,
            &table,
            &[
                json!({"id": "1", "name": "a"}),
                json!({"id": "2", "name": "b"}),
                json!({"id": "3", "name": "c"}),
            ],
            "tsv",
            "none",
        )
        .unwrap();
        let mut batches = Vec::new();

        let rows = stream_tsv_rows_in_batches(&path, &table, "none", 2, 1024, |batch| {
            batches.push(batch.len());
            Ok(())
        })
        .unwrap();

        assert_eq!(rows, 3);
        assert_eq!(batches, vec![2, 1]);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tsv_dump_rows_roundtrip_with_nulls_and_escaped_text() {
        let dir =
            std::env::temp_dir().join(format!("tunnelforge-tsv-test-{}", current_unix_seconds()));
        fs::create_dir_all(&dir).unwrap();
        let table = NormalizedTable {
            name: "notes".to_string(),
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
                    name: "body".to_string(),
                    type_name: "text".to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                },
                NormalizedColumn {
                    name: "empty".to_string(),
                    type_name: "varchar(8)".to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };
        let rows = vec![json!({"id": "1", "body": "a\tb\nc\\d", "empty": null})];
        let path = dir.join("chunk_000001.tsv");

        write_dump_rows(&path, &table, &rows, "tsv", "none").unwrap();
        assert_eq!(read_dump_rows(&path, &table, "tsv", "none").unwrap(), rows);

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn tsv_dump_rows_preserve_enum_value_case() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-tsv-enum-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = NormalizedTable {
            name: "df_evaluations_norm".to_string(),
            columns: vec![NormalizedColumn {
                name: "importance".to_string(),
                type_name: "enum('HIGH','MEDIUM','LOW')".to_string(),
                default_value: None,
                nullable: false,
                primary_key: false,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };
        let rows = vec![json!({"importance": "MEDIUM"})];
        let path = dir.join("chunk_000001.tsv");

        write_dump_rows(&path, &table, &rows, "tsv", "none").unwrap();
        assert_eq!(read_dump_rows(&path, &table, "tsv", "none").unwrap(), rows);

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn zstd_tsv_dump_rows_roundtrip_and_uses_compressed_extension() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-zstd-tsv-test-{}",
            current_unix_seconds()
        ));
        fs::create_dir_all(&dir).unwrap();
        let table = NormalizedTable {
            name: "users".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "bigint".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: true,
                },
                NormalizedColumn {
                    name: "notes".to_string(),
                    type_name: "text".to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };
        let rows = vec![
            json!({"id": "1", "notes": "hello\tworld"}),
            json!({"id": "2", "notes": "line\nbreak"}),
        ];
        let chunk_name = dump_chunk_name(1, "tsv", "zstd");
        assert_eq!(chunk_name, "chunk_000001.tsv.zst");
        let path = dir.join(chunk_name);

        write_dump_rows(&path, &table, &rows, "tsv", "zstd").unwrap();

        assert_eq!(read_dump_rows(&path, &table, "tsv", "zstd").unwrap(), rows);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_chunk_limit_stops_at_initial_row_count_when_source_grows() {
        assert_eq!(bounded_dump_chunk_limit(2, 0, 50_000), Some(2));
        assert_eq!(bounded_dump_chunk_limit(2, 2, 50_000), None);
        assert_eq!(
            bounded_dump_chunk_limit(120_000, 50_000, 50_000),
            Some(50_000)
        );
        assert_eq!(
            bounded_dump_chunk_limit(120_000, 100_000, 50_000),
            Some(20_000)
        );
    }

    #[test]
    fn adaptive_import_chunk_order_prefers_larger_chunk_files() {
        let dir = std::env::temp_dir().join(format!(
            "tunnelforge-import-order-test-{}",
            current_unix_seconds()
        ));
        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        fs::write(table_dir.join("chunk_000001.tsv"), b"1\n").unwrap();
        fs::write(table_dir.join("chunk_000002.tsv"), vec![b'x'; 1024]).unwrap();
        fs::write(table_dir.join("chunk_000003.tsv"), vec![b'y'; 64]).unwrap();
        let manifest = DumpTableManifest {
            name: "users".to_string(),
            path: "0001_users".to_string(),
            rows: 3,
            chunks: 3,
            chunk_sha256: BTreeMap::new(),
        };

        assert_eq!(
            adaptive_import_chunk_order(&dir, &manifest, "tsv", "none"),
            vec![2, 3, 1]
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dump_path_components_are_filesystem_safe() {
        assert_eq!(
            safe_dump_component("orders/detail:2026"),
            "orders_detail_2026"
        );
        assert_eq!(safe_dump_component(""), "table");
    }

    #[test]
    fn dump_import_manifest_tables_follow_fk_dependency_order() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("users", Vec::new()),
            ],
        };
        let tables = vec![
            DumpTableManifest {
                name: "orders".to_string(),
                path: "0001_orders".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            },
            DumpTableManifest {
                name: "users".to_string(),
                path: "0002_users".to_string(),
                rows: 1,
                chunks: 1,
                chunk_sha256: BTreeMap::new(),
            },
        ];

        let ordered = dependency_ordered_dump_tables(&schema, tables);

        assert_eq!(
            ordered
                .iter()
                .map(|table| table.name.as_str())
                .collect::<Vec<_>>(),
            vec!["users", "orders"]
        );
    }

    #[test]
    fn migration_plan_alias_preserves_service_command_name() {
        let result = handle_request(Request {
            command: "migration.plan".to_string(),
            request_id: Some("plan-1".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": schema()
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["command"], "migration.plan");
        assert_eq!(result["success"], true);
        assert_eq!(result["plan"]["table_order"], json!(["users"]));
        assert_eq!(result["plan"]["tables"][0]["name"], "users");
    }

    #[test]
    fn migration_plan_reports_tables_and_estimated_rows() {
        let result = handle_request(Request {
            command: "plan".to_string(),
            request_id: Some("plan-rows".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }, {
                        "name": "orders",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }]
                },
                "source_data": {
                    "users": [{"id": 1}, {"id": 2}],
                    "orders": [{"id": 10}]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(
            result["plan"]["tables"],
            json!([
                {"name": "users", "estimated_rows": 2},
                {"name": "orders", "estimated_rows": 1}
            ])
        );
    }

    #[test]
    fn dependency_order_puts_referenced_parent_tables_first() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("line_items", vec![fk("fk_line_items_orders", "orders")]),
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("audit_log", Vec::new()),
                empty_table("users", Vec::new()),
            ],
        };

        assert_eq!(
            table_dependency_order(&schema),
            vec!["audit_log", "users", "orders", "line_items"]
        );
    }

    #[test]
    fn dependency_order_keeps_all_tables_when_cycle_exists() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("a", vec![fk("fk_a_b", "b")]),
                empty_table("b", vec![fk("fk_b_a", "a")]),
                empty_table("root", Vec::new()),
            ],
        };

        let (ordered, cyclic) = table_dependency_order_indices(&schema);

        assert_eq!(
            ordered
                .into_iter()
                .map(|index| schema.tables[index].name.as_str())
                .collect::<Vec<_>>(),
            vec!["root", "a", "b"]
        );
        assert_eq!(cyclic, vec!["a", "b"]);
    }

    #[test]
    fn numeric_single_primary_key_is_parallel_dump_eligible() {
        let table = NormalizedTable {
            name: "big_items".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "bigint unsigned".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
                NormalizedColumn {
                    name: "tenant_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: false,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };

        assert_eq!(single_numeric_primary_key(&table), Some("id"));
    }

    #[test]
    fn composite_primary_key_is_not_parallel_range_eligible() {
        let table = NormalizedTable {
            name: "items".to_string(),
            columns: vec![
                NormalizedColumn {
                    name: "tenant_id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
                NormalizedColumn {
                    name: "id".to_string(),
                    type_name: "int".to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                },
            ],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };

        assert_eq!(single_numeric_primary_key(&table), None);
    }

    #[test]
    fn large_numeric_pk_table_is_range_dump_candidate() {
        let table = NormalizedTable {
            name: "events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };

        assert!(should_use_pk_range_dump(&table, 200_000, 50_000));
    }

    #[test]
    fn sparse_numeric_pk_span_falls_back_to_keyset_dump() {
        let table = NormalizedTable {
            name: "sparse_events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };

        assert!(!should_use_pk_range_dump_for_span(
            &table,
            200_000,
            50_000,
            1,
            10_000_000_000,
        ));
    }

    #[test]
    fn dense_numeric_pk_span_uses_range_dump() {
        let table = NormalizedTable {
            name: "dense_events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };

        assert!(should_use_pk_range_dump_for_span(
            &table, 200_000, 50_000, 1, 220_000,
        ));
    }

    #[test]
    fn small_numeric_pk_table_stays_whole_table_candidate() {
        let table = NormalizedTable {
            name: "small_events".to_string(),
            columns: vec![NormalizedColumn {
                name: "id".to_string(),
                type_name: "bigint".to_string(),
                default_value: None,
                nullable: false,
                primary_key: true,
                unique: false,
            }],
            indexes: Vec::new(),
            foreign_keys: Vec::new(),
        };

        assert!(!should_use_pk_range_dump(&table, 10_000, 50_000));
    }

    #[test]
    fn pk_ranges_split_numeric_span_into_contiguous_chunks() {
        let ranges = pk_ranges(1, 100, 100, 25);

        assert_eq!(ranges.len(), 4);
        assert_eq!(ranges[0].chunk_index, 1);
        assert_eq!((ranges[0].start, ranges[0].end), (1, 25));
        assert_eq!((ranges[3].start, ranges[3].end), (76, 100));
    }

    #[test]
    fn mysql_range_chunk_size_uses_byte_target_for_wide_tables() {
        let chunk_size = mysql_range_chunk_size_for_avg_row(50_000, 9_462);

        assert_eq!(chunk_size, 6_764);
        assert!(chunk_size < 50_000);
    }

    #[test]
    fn mysql_range_chunk_size_keeps_row_fallback_for_narrow_or_unknown_tables() {
        assert_eq!(mysql_range_chunk_size_for_avg_row(50_000, 0), 50_000);
        assert_eq!(mysql_range_chunk_size_for_avg_row(50_000, 128), 50_000);
    }

    #[test]
    fn zstd_dump_level_uses_fast_default() {
        assert_eq!(MYSQL_DUMP_ZSTD_LEVEL, 1);
    }

    #[test]
    fn migration_plan_reports_fk_dependency_order() {
        let result = handle_request(Request {
            command: "migration.plan".to_string(),
            request_id: Some("plan-fk".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {
                    "tables": [{
                        "name": "orders",
                        "foreign_keys": [{
                            "name": "fk_orders_users",
                            "columns": ["user_id"],
                            "referenced_table": "users",
                            "referenced_columns": ["id"]
                        }]
                    }, {
                        "name": "users"
                    }]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["plan"]["table_order"], json!(["users", "orders"]));
    }

    #[test]
    fn schema_columns_accept_data_type_alias() {
        let schema = parse_schema(&json!({
            "tables": [{
                "name": "users",
                "columns": [{"name": "id", "data_type": "int", "primary_key": true}]
            }]
        }))
        .unwrap();

        assert_eq!(schema.tables[0].columns[0].type_name, "int");
        assert!(schema.tables[0].columns[0].primary_key);
    }

    #[test]
    fn schema_diff_reports_table_column_and_type_differences() {
        let result = handle_request(Request {
            command: "schema.diff".to_string(),
            request_id: None,
            payload: json!({
                "source_schema": schema(),
                "target_schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "bigint", "nullable": false}]
                    }, {
                        "name": "audit",
                        "columns": []
                    }]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        let differences = result["differences"].as_array().unwrap();
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "missing_column" && diff["column"] == "name"));
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "extra_table" && diff["table"] == "audit"));
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "type_mismatch" && diff["column"] == "id"));
    }

    #[test]
    fn query_execute_accepts_memory_rows_for_contract_tests() {
        let result = handle_request(Request {
            command: "query.execute".to_string(),
            request_id: None,
            payload: json!({"rows": [{"id": 1, "name": "alpha"}]}),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["command"], "query.execute");
        assert_eq!(result["rows"][0]["name"], "alpha");
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
        };

        let first = unique_connection_id(&endpoint, 1);
        let second = unique_connection_id(&endpoint, 2);

        assert_ne!(first, second);
        assert!(first.starts_with(&connection_id(&endpoint)));
        assert!(second.starts_with(&connection_id(&endpoint)));
    }

    #[test]
    fn query_result_streams_row_batches_when_requested() {
        let events = query_result_events(
            &Request {
                command: "query.execute".to_string(),
                request_id: Some("query-1".to_string()),
                payload: json!({"stream_rows": true, "row_batch_size": 1}),
            },
            vec![json!({"id": 1}), json!({"id": 2})],
        );

        assert_eq!(events[0]["event"], "row_batch");
        assert_eq!(events[0]["rows"][0]["id"], 1);
        assert_eq!(events[1]["event"], "row_batch");
        assert_eq!(events[2]["event"], "result");
        assert_eq!(events[2]["rows_streamed"], 2);
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
            }],
        };

        assert_eq!(
            generate_schema_ddl(&schema, "mysql", "mysql")[0],
            "CREATE TABLE `df_evaluations_norm` (\n  `importance` enum('HIGH','MEDIUM','LOW') DEFAULT 'MEDIUM' NOT NULL\n);"
        );
    }

    #[test]
    fn generates_create_table_ddl() {
        let ddl = generate_schema_ddl(&schema(), "mysql", "postgresql");
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
            }],
        };
        let mut adapter = RecordingAdapter::default();

        apply_post_load_ddl(&mut adapter, &schema, "mysql").unwrap();

        assert_eq!(
            adapter.executed_sql,
            vec!["CREATE INDEX `idx_orders_user_id` ON `orders` (`user_id`);"]
        );
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
            }],
        };

        assert_eq!(
            generate_schema_ddl(&mysql_schema, "mysql", "postgresql")[0],
            "CREATE TABLE \"users\" (\n  \"id\" INTEGER GENERATED BY DEFAULT AS IDENTITY NOT NULL,\n  PRIMARY KEY (\"id\")\n);"
        );
        assert_eq!(
            generate_schema_ddl(&postgresql_schema, "postgresql", "mysql")[0],
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
            }],
        };

        assert_eq!(
            generate_schema_ddl(&mysql_schema, "mysql", "postgresql")[0],
            "CREATE TABLE \"users\" (\n  \"status\" VARCHAR(16) DEFAULT 'new' NOT NULL,\n  \"enabled\" BOOLEAN DEFAULT TRUE NOT NULL\n);"
        );
        assert_eq!(
            generate_schema_ddl(&postgresql_schema, "postgresql", "mysql")[0],
            "CREATE TABLE `users` (\n  `enabled` TINYINT(1) DEFAULT 1 NOT NULL\n);"
        );
    }

    #[test]
    fn create_only_blocks_non_empty_target() {
        let source = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 1}]})));
        let mut target = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 9}]})));
        let result = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(!result.success);
        assert_eq!(result.rows_copied, 0);
        assert!(result.issues.iter().any(|issue| issue.blocking));
        assert_eq!(target.row_count("users"), 1);
    }

    #[test]
    fn create_only_still_requires_empty_target_without_live_cleanup() {
        let source = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 1}]})));
        let mut target = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 9}]})));
        let result = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: true,
            },
            None,
            &source,
            &mut target,
        );

        assert!(!result.success);
        assert_eq!(result.rows_copied, 0);
        assert!(result.issues.iter().any(|issue| issue.blocking));
        assert_eq!(target.row_count("users"), 1);
    }

    #[test]
    fn migrates_rows_in_chunks() {
        let source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c"}]
        })));
        let mut target = MemoryAdapter::default();
        let result = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(result.success);
        assert_eq!(result.rows_copied, 3);
        assert_eq!(result.chunks_copied, 2);
        assert_eq!(target.row_count("users"), 3);
        assert!(result.state.tables[0].completed);
    }

    #[test]
    fn migration_creates_and_copies_parent_tables_before_children() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("users", Vec::new()),
            ],
        };
        let source = MemoryAdapter::from_value(Some(&json!({
            "orders": [{"id": 10, "parent_id": 1}],
            "users": [{"id": 1}]
        })));
        let mut target = MemoryAdapter::default();

        let result = migrate_memory(
            &schema,
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 100,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(result.success);
        assert_eq!(target.created_tables, vec!["users", "orders"]);
        assert_eq!(
            result
                .state
                .tables
                .iter()
                .map(|table| table.table.as_str())
                .collect::<Vec<_>>(),
            vec!["users", "orders"]
        );
    }

    #[test]
    fn migrate_command_emits_chunk_checkpoints_before_result() {
        let events = handle_request(Request {
            command: "migrate".to_string(),
            request_id: Some("req-1".to_string()),
            payload: json!({
                "schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }]
                },
                "execution_options": {"mode": "append", "chunk_size": 1},
                "source_data": {"users": [{"id": 1}, {"id": 2}]},
                "target_data": {}
            }),
        });

        let first_row_progress = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("row_progress")))
            .unwrap();
        let result = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert!(first_row_progress < result);
        assert_eq!(events[first_row_progress]["request_id"], "req-1");
        assert!(events[first_row_progress]["state"].is_object());
    }

    #[test]
    fn resumes_after_partial_copy() {
        let source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1}, {"id": 2}, {"id": 3}]
        })));
        let mut target = MemoryAdapter::default();
        let first = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: Some(1),
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );
        assert!(!first.success);
        assert_eq!(target.row_count("users"), 2);

        let second = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            Some(&first.state),
            &source,
            &mut target,
        );
        assert!(second.success);
        assert_eq!(second.rows_copied, 1);
        assert_eq!(target.row_count("users"), 3);
    }

    #[test]
    fn resumes_large_stream_after_multiple_chunks() {
        let rows = (1..=12_037)
            .map(|id| json!({"id": id, "name": format!("user-{id}")}))
            .collect::<Vec<_>>();
        let mut source = MemoryAdapter::default();
        source.insert_rows("users", rows);
        let mut target = MemoryAdapter::default();

        let first = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 5_000,
                cancel_after_chunks: Some(2),
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );
        assert!(!first.success);
        assert_eq!(first.rows_copied, 10_000);
        assert_eq!(target.row_count("users"), 10_000);
        assert_eq!(first.state.tables[0].rows_copied, 10_000);

        let second = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 5_000,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            Some(&first.state),
            &source,
            &mut target,
        );

        assert!(second.success);
        assert_eq!(second.rows_copied, 2_037);
        assert_eq!(second.chunks_copied, 1);
        assert_eq!(target.row_count("users"), 12_037);
        assert!(second.state.tables[0].completed);
    }

    #[test]
    fn keyed_compare_reports_cell_mismatch() {
        let mismatches = compare_keyed_rows(
            "users",
            &["id".to_string()],
            &[json!({"id": 1, "name": "source"})],
            &[json!({"id": 1, "name": "target"})],
        );
        assert_eq!(mismatches.len(), 1);
        assert_eq!(mismatches[0]["kind"], "cell");
        assert_eq!(mismatches[0]["column"], "name");
    }

    #[test]
    fn verify_with_adapters_reports_keyed_mismatch() {
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "source"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "target"}]
        })));

        let mismatches = verify_with_adapters(&schema(), &mut source, &mut target, 1);

        assert_eq!(mismatches.len(), 1);
        assert_eq!(mismatches[0]["kind"], "cell");
        assert_eq!(mismatches[0]["column"], "name");
    }

    #[test]
    fn verify_with_adapters_reports_count_mismatch() {
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "a"}]
        })));

        let mismatches = verify_with_adapters(&schema(), &mut source, &mut target, 1);

        assert!(mismatches
            .iter()
            .any(|mismatch| mismatch["kind"] == "count"));
    }

    #[derive(Default)]
    struct TrackingAdapter {
        rows: Vec<Value>,
        read_limits: Vec<usize>,
        read_after_limits: Vec<usize>,
        max_returned: usize,
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

    #[test]
    fn verify_with_adapters_reads_keyed_tables_in_chunks() {
        let rows: Vec<Value> = (0..5)
            .map(|id| json!({"id": id.to_string(), "name": format!("user-{id}")}))
            .collect();
        let mut source = TrackingAdapter {
            rows: rows.clone(),
            ..Default::default()
        };
        let mut target = TrackingAdapter {
            rows,
            ..Default::default()
        };

        let mismatches = verify_with_adapters(&schema(), &mut source, &mut target, 2);

        assert!(mismatches.is_empty());
        assert!(source.read_limits.is_empty());
        assert!(target.read_limits.is_empty());
        assert!(source.read_after_limits.len() > 2);
        assert!(source.read_after_limits.iter().all(|limit| *limit == 2));
        assert!(target.read_after_limits.iter().all(|limit| *limit == 2));
        assert!(source.max_returned <= 2);
        assert!(target.max_returned <= 2);
    }

    #[test]
    fn verify_command_emits_progress_before_result() {
        let events = handle_request(Request {
            command: "verify".to_string(),
            request_id: Some("verify-progress".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": schema(),
                "source_data": {"users": [{"id": 1, "name": "a"}]},
                "target_data": {"users": [{"id": 1, "name": "a"}]},
            }),
        });

        let table_progress = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("table_progress")))
            .unwrap();
        let row_progress = events
            .iter()
            .rposition(|event| event.get("event") == Some(&json!("row_progress")))
            .unwrap();
        let result = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert!(table_progress < result);
        assert!(row_progress < result);
        assert_eq!(events[row_progress]["table"], "users");
        assert_eq!(events[row_progress]["rows"], 1);
        assert_eq!(events[row_progress]["total"], 1);
        assert_eq!(events[row_progress]["request_id"], "verify-progress");
    }

    #[test]
    fn resumes_composite_key_tables_without_offset_state() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "items".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "tenant_id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "name".to_string(),
                        type_name: "varchar(32)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            }],
        };
        let source = MemoryAdapter::from_value(Some(&json!({
            "items": [
                {"tenant_id": "1", "id": "1", "name": "a"},
                {"tenant_id": "1", "id": "2", "name": "b"},
                {"tenant_id": "2", "id": "1", "name": "c"}
            ]
        })));
        let mut target = MemoryAdapter::default();
        let first = migrate_memory(
            &schema,
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 2,
                cancel_after_chunks: Some(1),
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(!first.success);
        assert_eq!(first.state.tables[0].rows_copied, 2);
        assert_eq!(
            first.state.tables[0].last_key.as_deref(),
            Some("[\"1\",\"2\"]")
        );

        let second = migrate_memory(
            &schema,
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            Some(&first.state),
            &source,
            &mut target,
        );

        assert!(second.success);
        assert_eq!(target.row_count("items"), 3);
        assert!(verify_memory(&schema, &source, &target).is_empty());
    }

    #[test]
    fn typed_verify_treats_boolean_text_equivalents_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "flags".to_string(),
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
                        name: "enabled".to_string(),
                        type_name: "tinyint(1)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "flags": [{"id": 1, "enabled": "1"}, {"id": 2, "enabled": "0"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "flags": [{"id": 1, "enabled": "true"}, {"id": 2, "enabled": "false"}]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
    }

    #[test]
    fn typed_verify_treats_temporal_text_equivalents_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "events".to_string(),
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
                        name: "event_date".to_string(),
                        type_name: "date".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "event_time".to_string(),
                        type_name: "time".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "created_at".to_string(),
                        type_name: "datetime".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "events": [{
                "id": 1,
                "event_date": "2026-05-14",
                "event_time": "09:08:07.000000",
                "created_at": "2026-05-14 09:08:07.000000"
            }]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "events": [{
                "id": 1,
                "event_date": "2026-05-14",
                "event_time": "09:08:07",
                "created_at": "2026-05-14T09:08:07"
            }]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
    }

    #[test]
    fn typed_verify_treats_decimal_text_equivalents_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "ledger".to_string(),
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
                        name: "amount".to_string(),
                        type_name: "decimal(12,4)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "ledger": [{"id": 1, "amount": "001.2300"}, {"id": 2, "amount": "-0.0000"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "ledger": [{"id": 1, "amount": "1.23"}, {"id": 2, "amount": "0"}]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
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
    fn typed_verify_treats_postgresql_nul_sanitized_text_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "logs".to_string(),
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
                        name: "message".to_string(),
                        type_name: "text".to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "logs": [{"id": 1, "message": "before\0after"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "logs": [{"id": 1, "message": "beforeafter"}]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
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
    fn text_range_sql_filters_by_numeric_primary_key() {
        let table = schema().tables[0].clone();

        assert_eq!(
            select_chunk_text_range_sql("mysql", &table, "id", 101, 200),
            "SELECT `id`, `name` FROM `users` WHERE `users`.`id` >= 101 AND `users`.`id` <= 200 ORDER BY `users`.`id`"
        );
    }

    #[test]
    fn load_data_sql_uses_local_infile_and_tsv_options() {
        let table = schema().tables[0].clone();

        assert_eq!(
            load_data_local_infile_sql("mysql", &table, "chunk.tsv"),
            "LOAD DATA LOCAL INFILE 'chunk.tsv' INTO TABLE `users` CHARACTER SET utf8mb4 FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' (`id`, `name`)"
        );
    }

    #[test]
    fn mysql_dump_import_uses_fast_session_tuning_statements() {
        assert_eq!(
            mysql_import_session_tuning_sql(false),
            vec![
                "SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode, 'NO_BACKSLASH_ESCAPES', ''), 'NO_ZERO_IN_DATE', ''), 'NO_ZERO_DATE', ''), 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', ''), ',,', ','), ',,', ','))".to_string(),
                "SET SESSION foreign_key_checks=0".to_string(),
                "SET SESSION unique_checks=0".to_string(),
            ]
        );
        assert_eq!(
            mysql_import_session_tuning_sql(true),
            vec![
                "SET SESSION sql_mode=DEFAULT".to_string(),
                "SET SESSION unique_checks=1".to_string(),
                "SET SESSION foreign_key_checks=1".to_string(),
            ]
        );
    }

    #[test]
    fn mysql_dump_import_uses_fallback_when_local_infile_is_disabled() {
        assert!(is_mysql_local_infile_disabled_error(
            "ERROR 3948 (42000): Loading local data is disabled"
        ));
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
    fn preflight_emits_actionable_phase_events_before_result() {
        let events = handle_request(Request {
            command: "preflight".to_string(),
            request_id: Some("preflight-1".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {"tables": []},
                "options": {"mode": "append"}
            }),
        });

        let phase_messages: Vec<&str> = events
            .iter()
            .filter(|event| event.get("event") == Some(&json!("phase")))
            .filter_map(|event| event.get("message").and_then(Value::as_str))
            .collect();
        let result_index = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();
        let last_phase_index = events
            .iter()
            .rposition(|event| event.get("event") == Some(&json!("phase")))
            .unwrap();

        assert!(phase_messages.contains(&"preflight checks started"));
        assert!(phase_messages.contains(&"schema compatibility checks completed"));
        assert!(phase_messages.contains(&"target state checks completed"));
        assert!(phase_messages.contains(&"preflight result ready"));
        assert!(last_phase_index < result_index);
    }

    #[test]
    fn cleanup_command_drops_target_tables_in_reverse_dependency_order() {
        let events = handle_request(Request {
            command: "cleanup".to_string(),
            request_id: Some("cleanup-1".to_string()),
            payload: json!({
                "target_engine": "postgresql",
                "schema": {
                    "tables": [
                        {
                            "name": "parents",
                            "columns": [{"name": "id", "type": "int", "primary_key": true}]
                        },
                        {
                            "name": "children",
                            "columns": [
                                {"name": "id", "type": "int", "primary_key": true},
                                {"name": "parent_id", "type": "int"}
                            ],
                            "foreign_keys": [{
                                "name": "children_parent_id_fk",
                                "columns": ["parent_id"],
                                "referenced_table": "parents",
                                "referenced_columns": ["id"]
                            }]
                        }
                    ]
                }
            }),
        });

        let result = events
            .iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "cleanup");
        assert_eq!(result["success"], true);
        assert_eq!(result["dropped_tables"], json!(["children", "parents"]));
    }

    #[test]
    fn readiness_rejects_same_engine_endpoints() {
        let events = handle_request(Request {
            command: "readiness".to_string(),
            request_id: Some("ready-1".to_string()),
            payload: json!({
                "source": {
                    "engine": "mysql",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "root",
                    "password": "",
                    "database": "app"
                },
                "target": {
                    "engine": "mysql",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "root",
                    "password": "",
                    "database": "app2"
                }
            }),
        });

        assert!(events.iter().any(|event| {
            event.get("event") == Some(&json!("error"))
                && event["message"]
                    .as_str()
                    .unwrap_or("")
                    .contains("one MySQL endpoint and one PostgreSQL")
        }));
    }

    #[test]
    fn table_guide_includes_row_values_and_insert_sql() {
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": "1", "name": "alpha"}]
        })));
        let mut issues = Vec::new();

        let guides = build_table_guides(
            &schema(),
            &mut source,
            "mysql",
            "postgresql",
            5,
            &mut issues,
        );

        assert!(issues.is_empty());
        assert_eq!(guides[0]["table"], "users");
        assert_eq!(guides[0]["row_samples"][0]["name"], "alpha");
        assert_eq!(
            guides[0]["insert_example_sql"],
            "INSERT INTO \"users\" (\"id\", \"name\") VALUES ('1', 'alpha')"
        );
        assert_eq!(guides[0]["columns"][0]["target_type"], "INTEGER");
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
    fn dump_manifest_consistency_metadata_marks_parallel_as_non_strict() {
        let (snapshot_policy, strict_export, warnings) = dump_manifest_consistency_metadata(8);

        assert_eq!(snapshot_policy, "non_consistent_parallel");
        assert!(!strict_export);
        assert_eq!(
            warnings,
            vec!["parallel export did not prove a shared consistent snapshot".to_string()]
        );
    }

    #[test]
    fn dump_manifest_consistency_metadata_marks_single_thread_as_strict() {
        let (snapshot_policy, strict_export, warnings) = dump_manifest_consistency_metadata(1);

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
