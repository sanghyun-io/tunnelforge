use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use mysql::prelude::Queryable;
use postgres::{error::SqlState, NoTls};

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
    pub source_engine: String,
    pub database: String,
    pub schema: NormalizedSchema,
    pub chunk_size: usize,
    pub created_unix_seconds: u64,
    pub tables: Vec<DumpTableManifest>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DumpTableManifest {
    pub name: String,
    pub path: String,
    pub rows: u64,
    pub chunks: u64,
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
                        Err(format!("postgresql count error: {err}"))
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
                    Err(format!("postgresql create table error: {err}"))
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
                    .map_err(|err| format!("postgresql select chunk error: {err}"))?;
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
                let rows = client
                    .query(&sql, &[])
                    .map_err(|err| format!("postgresql keyset select chunk error: {err}"))?;
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
        let engine = self.engine();
        let sql = insert_rows_literal_sql_for_table(engine, table, &rows);
        match self {
            Self::MySql(conn) => conn
                .query_drop(sql)
                .map_err(|err| format!("mysql insert error: {err}")),
            Self::PostgreSql(client) => client
                .batch_execute(&sql)
                .map_err(|err| format!("postgresql insert error: {err}")),
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
                .map_err(|err| format!("postgresql SQL execution error: {err}")),
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
    5000
}

pub struct CoreService {
    connections: BTreeMap<String, LiveAdapter>,
}

impl CoreService {
    pub fn new() -> Self {
        Self {
            connections: BTreeMap::new(),
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
        let id = connection_id(&endpoint);
        match LiveAdapter::connect(&endpoint) {
            Ok(adapter) => {
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
        "job.cancel" => emit_all_events(job_cancel(&request), emit),
        "inspect" => emit_all_events(inspect(&request), emit),
        "preflight" => emit_all_events(preflight(&request), emit),
        "readiness" => emit_all_events(readiness(&request), emit),
        "guide" => emit_all_events(guide(&request), emit),
        "plan" => emit_all_events(plan(&request), emit),
        "migrate" => migrate_streaming(&request, emit),
        "verify" => emit_all_events(verify(&request), emit),
        "resume" => emit_all_events(resume(&request), emit),
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
    let overwrite = request
        .payload
        .get("overwrite")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let selected_tables = string_list(request.payload.get("tables"));

    let output_path = Path::new(output_dir);
    if output_path.exists() {
        if overwrite {
            fs::remove_dir_all(output_path)
                .map_err(|err| format!("failed to clear dump output_dir: {err}"))?;
        } else {
            let mut entries = fs::read_dir(output_path)
                .map_err(|err| format!("failed to inspect dump output_dir: {err}"))?;
            if entries.next().is_some() {
                return Err("dump output_dir already exists and is not empty".to_string());
            }
        }
    }
    fs::create_dir_all(output_path)
        .map_err(|err| format!("failed to create dump output_dir: {err}"))?;

    let inspection = inspect_live(&endpoint)?;
    let mut schema = inspection.schema;
    if !selected_tables.is_empty() {
        let selected: BTreeSet<String> = selected_tables.into_iter().collect();
        schema.tables.retain(|table| selected.contains(&table.name));
    }
    if schema.tables.is_empty() {
        return Err("dump.run found no tables to export".to_string());
    }

    let mut adapter = LiveAdapter::connect(&endpoint)?;
    let mut table_manifests = Vec::new();
    let mut total_rows = 0_u64;
    let mut total_chunks = 0_u64;
    let table_total = schema.tables.len();

    for (index, table) in schema.tables.iter().enumerate() {
        emit(json!({
            "event": "table_progress",
            "request_id": request.request_id,
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

        loop {
            let rows = if use_keyset {
                adapter.read_rows_after_key(table, &key_columns, last_key.as_deref(), chunk_size)?
            } else {
                adapter.read_rows(table, offset, chunk_size)?
            };
            if rows.is_empty() {
                break;
            }
            chunks_dumped += 1;
            let chunk_name = format!("chunk_{chunks_dumped:06}.jsonl");
            write_jsonl_rows(&table_dir.join(&chunk_name), &rows)?;

            let copied_now = rows.len();
            rows_dumped += copied_now as u64;
            total_rows += copied_now as u64;
            total_chunks += 1;
            if use_keyset {
                last_key = rows.last().and_then(|row| row_key_token(row, &key_columns));
            } else {
                offset += copied_now;
            }

            emit(json!({
                "event": "row_progress",
                "request_id": request.request_id,
                "table": table.name,
                "rows": rows_dumped,
                "total": table_row_count
            }));
        }

        table_manifests.push(DumpTableManifest {
            name: table.name.clone(),
            path: table_path,
            rows: rows_dumped,
            chunks: chunks_dumped,
        });
        emit(json!({
            "event": "table_progress",
            "request_id": request.request_id,
            "table": table.name,
            "status": "completed",
            "current": index + 1,
            "total": table_total
        }));
    }

    let manifest = DumpManifest {
        format: "tunnelforge-dump".to_string(),
        format_version: 1,
        source_engine: endpoint.engine.clone(),
        database: endpoint.database.clone(),
        schema,
        chunk_size,
        created_unix_seconds: current_unix_seconds(),
        tables: table_manifests,
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
        "tables": manifest.tables.len(),
        "rows_dumped": total_rows,
        "chunks_dumped": total_chunks,
        "manifest": "_tunnelforge_dump.json"
    }))
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
    if manifest.format != "tunnelforge-dump" || manifest.format_version != 1 {
        return Err("unsupported dump manifest format".to_string());
    }

    let selected_tables = string_list(request.payload.get("tables"));
    let selected: BTreeSet<String> = selected_tables.into_iter().collect();
    let tables: Vec<DumpTableManifest> = manifest
        .tables
        .iter()
        .filter(|table| selected.is_empty() || selected.contains(&table.name))
        .cloned()
        .collect();
    if tables.is_empty() {
        return Err("dump.import found no tables to import".to_string());
    }

    let mut adapter = LiveAdapter::connect(&endpoint)?;
    let table_total = tables.len();
    let mut rows_imported = 0_u64;
    let mut chunks_imported = 0_u64;

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

        if matches!(mode, "replace" | "recreate") {
            adapter.execute_sql(&drop_table_sql(adapter.engine(), &table.name))?;
        }
        let ddl = generate_table_ddl(table, &manifest.source_engine, adapter.engine())
            .ok_or_else(|| format!("cannot generate DDL for table {}", table.name))?;
        adapter.create_table(table, &ddl)?;

        for chunk_index in 1..=table_manifest.chunks {
            let chunk_path = input_path
                .join(&table_manifest.path)
                .join(format!("chunk_{chunk_index:06}.jsonl"));
            let rows = read_jsonl_rows(&chunk_path)?;
            let row_count = rows.len();
            adapter.insert_rows(table, rows)?;
            rows_imported += row_count as u64;
            chunks_imported += 1;
            emit(json!({
                "event": "row_progress",
                "request_id": request.request_id,
                "table": table.name,
                "rows": rows_imported,
                "total": table_manifest.rows
            }));
        }

        emit(json!({
            "event": "table_progress",
            "request_id": request.request_id,
            "table": table.name,
            "status": "completed",
            "current": index + 1,
            "total": table_total
        }));
    }

    Ok(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "dump.import",
        "success": true,
        "input_dir": input_dir,
        "mode": mode,
        "tables": table_total,
        "rows_imported": rows_imported,
        "chunks_imported": chunks_imported
    }))
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
        other => format!("'{}'", other.to_string().replace('\\', "\\\\").replace('\'', "''")),
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
        let columns: Vec<NormalizedColumn> = conn
            .exec_map(
                inspect_columns_sql("mysql"),
                (&schema_name, &table_name),
                |(name, type_name, is_nullable, default_value, extra): (
                    String,
                    String,
                    String,
                    Option<String>,
                    String,
                )| {
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

fn preflight(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "preflight",
        "preflight checks started",
    )];
    let mut issues = preflight_issues(&request.payload);
    issues.extend(live_preflight_issues(&request.payload));

    for issue in &issues {
        events.push(json!({
            "event": "issue",
            "request_id": request.request_id,
            "issue": issue
        }));
    }

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "preflight",
        "success": !issues.iter().any(|issue| issue.blocking),
        "issues": issues
    }));
    events
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
    let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
    let ddl = generate_schema_ddl(&schema, &source, &target);
    let table_order: Vec<String> = schema
        .tables
        .iter()
        .map(|table| table.name.clone())
        .collect();

    vec![
        phase_event(request, "plan", "migration plan generation started"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "plan",
            "success": true,
            "plan": {
                "ddl": ddl,
                "table_order": table_order,
                "execution_options": parse_options(&request.payload)
            }
        }),
    ]
}

fn migrate_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(request, "migrate", "migration started"));
    if request.payload.get("source").is_some() && request.payload.get("target").is_some() {
        let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
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

    let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
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
                let mismatches =
                    verify_with_adapters(&schema, &mut source, &mut target, options.chunk_size);
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
        let source = MemoryAdapter::from_value(request.payload.get("source_data"));
        let target = MemoryAdapter::from_value(request.payload.get("target_data"));
        let mismatches = verify_memory(&schema, &source, &target);
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

    let mut state = resume_state
        .cloned()
        .unwrap_or_else(|| initial_state(schema));
    let mut rows_copied = 0;
    let mut chunks_copied = 0;
    let chunk_size = options.chunk_size.max(1);
    let ddl = if source_engine.is_empty() || target_engine.is_empty() {
        Vec::new()
    } else {
        generate_schema_ddl(schema, source_engine, target_engine)
    };

    for (table_index, table) in schema.tables.iter().enumerate() {
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
    for sql in generate_sequence_reset_ddl(schema, target_engine) {
        if let Err(err) = target.execute_sql(&sql) {
            let table = schema.tables.first().cloned().unwrap_or(NormalizedTable {
                name: "sequence_reset".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            });
            return migration_error_result(state, rows_copied, chunks_copied, &table, err);
        }
    }
    for sql in generate_post_data_ddl(schema, target_engine) {
        if let Err(err) = target.execute_sql(&sql) {
            let table = schema.tables.first().cloned().unwrap_or(NormalizedTable {
                name: "post_data_ddl".to_string(),
                columns: Vec::new(),
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            });
            return migration_error_result(state, rows_copied, chunks_copied, &table, err);
        }
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
    let mut mismatches = Vec::new();
    let chunk_size = chunk_size.max(1);
    for table in &schema.tables {
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
            let next_key = source_rows
                .last()
                .or_else(|| target_rows.last())
                .and_then(|row| row_key_token(row, &key_columns));
            if next_key.is_none() || next_key == last_key {
                break;
            }
            last_key = next_key;
        }
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

fn current_unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn write_dump_manifest(output_path: &Path, manifest: &DumpManifest) -> Result<(), String> {
    let path = output_path.join("_tunnelforge_dump.json");
    let file =
        File::create(&path).map_err(|err| format!("failed to create dump manifest: {err}"))?;
    serde_json::to_writer_pretty(file, manifest)
        .map_err(|err| format!("failed to write dump manifest: {err}"))
}

fn read_dump_manifest(input_path: &Path) -> Result<DumpManifest, String> {
    let path = input_path.join("_tunnelforge_dump.json");
    let file = File::open(&path).map_err(|err| format!("failed to open dump manifest: {err}"))?;
    serde_json::from_reader(file).map_err(|err| format!("failed to parse dump manifest: {err}"))
}

fn write_jsonl_rows(path: &Path, rows: &[Value]) -> Result<(), String> {
    let mut file =
        File::create(path).map_err(|err| format!("failed to create dump chunk: {err}"))?;
    for row in rows {
        serde_json::to_writer(&mut file, row)
            .map_err(|err| format!("failed to encode dump row: {err}"))?;
        file.write_all(b"\n")
            .map_err(|err| format!("failed to write dump row: {err}"))?;
    }
    Ok(())
}

fn read_jsonl_rows(path: &Path) -> Result<Vec<Value>, String> {
    let file = File::open(path).map_err(|err| format!("failed to open dump chunk: {err}"))?;
    let reader = BufReader::new(file);
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
    }
    sql_literal(value)
}

pub fn is_binary_type(type_name: &str) -> bool {
    let type_name = type_name.to_ascii_lowercase();
    type_name.contains("blob")
        || type_name.contains("binary")
        || type_name == "bytea"
        || type_name.starts_with("varbinary")
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
        "SELECT COLUMN_NAME AS column_name, COLUMN_TYPE AS data_type, IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default, EXTRA AS extra FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ORDINAL_POSITION"
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
    let ty = type_name.trim().to_ascii_lowercase();
    if source == "mysql" && target == "postgresql" {
        map_mysql_to_postgres(&ty)
    } else if source == "postgresql" && target == "mysql" {
        map_postgres_to_mysql(&ty)
    } else {
        ty
    }
}

fn map_mysql_to_postgres(ty: &str) -> String {
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
    }

    #[test]
    fn dump_manifest_and_jsonl_rows_roundtrip() {
        let dir =
            std::env::temp_dir().join(format!("tunnelforge-dump-test-{}", current_unix_seconds()));
        fs::create_dir_all(&dir).unwrap();

        let manifest = DumpManifest {
            format: "tunnelforge-dump".to_string(),
            format_version: 1,
            source_engine: "mysql".to_string(),
            database: "app".to_string(),
            schema: schema(),
            chunk_size: 1000,
            created_unix_seconds: 1,
            tables: vec![DumpTableManifest {
                name: "users".to_string(),
                path: "0001_users".to_string(),
                rows: 2,
                chunks: 1,
            }],
        };
        write_dump_manifest(&dir, &manifest).unwrap();
        assert_eq!(read_dump_manifest(&dir).unwrap(), manifest);

        let table_dir = dir.join("0001_users");
        fs::create_dir_all(&table_dir).unwrap();
        let rows = vec![json!({"id": 1}), json!({"id": 2})];
        let chunk_path = table_dir.join("chunk_000001.jsonl");
        write_jsonl_rows(&chunk_path, &rows).unwrap();
        assert_eq!(read_jsonl_rows(&chunk_path).unwrap(), rows);

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

        assert_eq!(sql, "SELECT * FROM users WHERE id = 7 AND name = 'O''Reilly'");
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
            "SELECT CAST(`id` AS CHAR) AS `id`, CAST(`name` AS CHAR) AS `name` FROM `users` ORDER BY `id` LIMIT ? OFFSET ?"
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
        };

        assert_eq!(
            select_chunk_text_sql("mysql", &table, &["id".to_string()]),
            "SELECT CAST(`id` AS CHAR) AS `id`, HEX(`payload`) AS `payload` FROM `files` ORDER BY `id` LIMIT ? OFFSET ?"
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
}
