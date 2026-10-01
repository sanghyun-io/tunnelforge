//! Cancellable, streaming, limit-aware execution of `query.execute` on a session connection.
//!
//! The JSONL main loop keeps reading stdin while a query runs on a worker thread, so
//! `query.cancel` can be handled mid-query. Rows are emitted as they are fetched.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::BufRead;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::*;
use mysql::prelude::Queryable;

pub type Emitter = Arc<dyn Fn(Value) + Send + Sync>;
pub(crate) type Jobs = Arc<Mutex<HashMap<String, Arc<JobCtl>>>>;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Out-of-band server-side cancel for the query currently running on a session.
pub(crate) enum Canceller {
    MySql { endpoint: Endpoint, thread_id: u32 },
    PostgreSql { token: postgres::CancelToken, endpoint: Endpoint },
}

impl Canceller {
    fn for_adapter(adapter: &LiveAdapter, endpoint: &Endpoint) -> Self {
        match adapter {
            LiveAdapter::MySql(conn) => Self::MySql {
                endpoint: endpoint.clone(),
                thread_id: conn.connection_id(),
            },
            LiveAdapter::PostgreSql(client) => Self::PostgreSql {
                token: client.cancel_token(),
                endpoint: endpoint.clone(),
            },
        }
    }

    fn send(&self) -> Result<(), String> {
        match self {
            Self::MySql { endpoint, thread_id } => {
                let mut conn = mysql::Conn::new(mysql_opts(endpoint)).map_err(|err| {
                    redact_endpoint_secret(&format!("mysql cancel connection error: {err}"), endpoint)
                })?;
                conn.query_drop(format!("KILL QUERY {thread_id}"))
                    .map_err(|err| format!("mysql KILL QUERY error: {err}"))
            }
            Self::PostgreSql { token, endpoint } => cancel_postgres_query(token, endpoint)
                .map_err(|err| redact_endpoint_secret(&format!("postgresql cancel error: {err}"), endpoint)),
        }
    }
}

/// One `connection.open` session. The adapter mutex is held by the running query.
pub(crate) struct Session {
    adapter: Mutex<LiveAdapter>,
    canceller: Canceller,
    running: Mutex<Option<String>>,
    read_only: bool,
}

impl Session {
    pub(crate) fn new(adapter: LiveAdapter, endpoint: &Endpoint, read_only: bool) -> Self {
        let canceller = Canceller::for_adapter(&adapter, endpoint);
        Self {
            adapter: Mutex::new(adapter),
            canceller,
            running: Mutex::new(None),
            read_only,
        }
    }

    pub(crate) fn read_only(&self) -> bool {
        self.read_only
    }
}

pub(crate) struct JobCtl {
    session: Arc<Session>,
    cancelled: AtomicBool,
    timed_out: AtomicBool,
    server_cancel_sent: AtomicBool,
    finished: AtomicBool,
}

impl JobCtl {
    fn new(session: Arc<Session>) -> Self {
        Self {
            session,
            cancelled: AtomicBool::new(false),
            timed_out: AtomicBool::new(false),
            server_cancel_sent: AtomicBool::new(false),
            finished: AtomicBool::new(false),
        }
    }

    fn stopped(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst) || self.timed_out.load(Ordering::SeqCst)
    }

    /// Returns Ok(false) when the job already finished (nothing to cancel).
    pub(crate) fn cancel(&self, timeout: bool) -> Result<bool, String> {
        if self.finished.load(Ordering::SeqCst) {
            return Ok(false);
        }
        let flag = if timeout { &self.timed_out } else { &self.cancelled };
        flag.store(true, Ordering::SeqCst);
        self.session.canceller.send()?;
        self.server_cancel_sent.store(true, Ordering::SeqCst);
        Ok(true)
    }

    pub(crate) fn session(&self) -> &Arc<Session> {
        &self.session
    }
}

pub(crate) struct QuerySpec {
    pub(crate) request_id: Option<String>,
    pub(crate) job_id: String,
    pub(crate) sql: String,
    pub(crate) params: Vec<Value>,
    pub(crate) stream: bool,
    pub(crate) batch_size: usize,
    pub(crate) timeout_ms: Option<u64>,
    pub(crate) max_rows: Option<u64>,
    pub(crate) max_bytes: Option<u64>,
    pub(crate) output: Option<OutputSpec>,
}

impl QuerySpec {
    pub(crate) fn from_request(request: &Request, job_id: String, sql: String) -> Result<Self, String> {
        let p = &request.payload;
        let num = |key: &str| p.get(key).and_then(Value::as_u64).filter(|n| *n > 0);
        Ok(Self {
            request_id: request.request_id.clone(),
            job_id,
            sql,
            params: query_params(p),
            stream: p.get("stream_rows").and_then(Value::as_bool).unwrap_or(false),
            batch_size: p
                .get("row_batch_size")
                .and_then(Value::as_u64)
                .unwrap_or(500)
                .max(1) as usize,
            timeout_ms: num("timeout_ms"),
            max_rows: num("max_rows"),
            max_bytes: num("max_bytes"),
            output: OutputSpec::from_payload(p)?,
        })
    }
}

/// Collects/streams rows and enforces `max_rows` / `max_bytes`.
struct Sink<'a> {
    emit: &'a Emitter,
    spec: &'a QuerySpec,
    batch: Vec<Value>,
    batch_index: u64,
    all: Vec<Value>,
    streamed: u64,
    bytes: u64,
    last_flush: Instant,
    truncated_by: Option<&'static str>,
    out: Option<OutputWriter>,
    io_error: Option<String>,
    /// Per column: value is raw binary (MySQL BLOB/BINARY/BIT, PostgreSQL bytea).
    binary: Vec<bool>,
    last_progress: Instant,
}

impl<'a> Sink<'a> {
    fn new(emit: &'a Emitter, spec: &'a QuerySpec) -> Self {
        Self {
            emit,
            spec,
            batch: Vec::new(),
            batch_index: 0,
            all: Vec::new(),
            streamed: 0,
            bytes: 0,
            last_flush: Instant::now(),
            truncated_by: None,
            out: None,
            io_error: None,
            binary: Vec::new(),
            last_progress: Instant::now(),
        }
    }

    /// Binary encoding when the result goes to a file (values are then kept exact, as text).
    fn export_encoding(&self) -> Option<BinaryEncoding> {
        self.spec.output.as_ref().map(|output| output.binary)
    }

    fn begin(&mut self, columns: &[String]) -> Result<(), String> {
        if let Some(output) = &self.spec.output {
            self.out = Some(OutputWriter::create(output, columns)?);
            return Ok(());
        }
        if self.spec.stream {
            (self.emit)(json!({
                "event": "columns",
                "request_id": self.spec.request_id,
                "command": "query.execute",
                "job_id": self.spec.job_id,
                "columns": columns
            }));
        }
        Ok(())
    }

    /// Returns false when a limit stopped the fetch (the row was not accepted).
    fn push(&mut self, row: Value) -> bool {
        if self.spec.max_rows.is_some_and(|max| self.streamed >= max) {
            self.truncated_by = Some("rows");
            return false;
        }
        if let Some(max) = self.spec.max_bytes {
            let len = row.to_string().len() as u64;
            // Always accept the first row so a single huge row still yields a result.
            if self.streamed > 0 && self.bytes + len > max {
                self.truncated_by = Some("bytes");
                return false;
            }
            self.bytes += len;
        }
        if let Some(out) = self.out.as_mut() {
            if let Err(err) = out.write_row(&row) {
                self.io_error = Some(err);
                return false;
            }
            self.streamed += 1;
            if self.last_progress.elapsed() >= Duration::from_millis(500) {
                self.last_progress = Instant::now();
                (self.emit)(json!({
                    "event": "progress", "request_id": self.spec.request_id,
                    "command": "query.execute", "job_id": self.spec.job_id,
                    "rows_written": out.rows, "bytes_written": out.bytes
                }));
            }
            return true;
        }
        self.streamed += 1;
        if !self.spec.stream {
            self.all.push(row);
            return true;
        }
        self.batch.push(row);
        if self.batch.len() >= self.spec.batch_size || self.last_flush.elapsed() >= Duration::from_millis(250) {
            self.flush();
        }
        true
    }

    fn flush(&mut self) {
        if self.batch.is_empty() {
            return;
        }
        (self.emit)(json!({
            "event": "row_batch",
            "request_id": self.spec.request_id,
            "command": "query.execute",
            "job_id": self.spec.job_id,
            "batch_index": self.batch_index,
            "rows": std::mem::take(&mut self.batch),
            "total": self.streamed
        }));
        self.batch_index += 1;
        self.last_flush = Instant::now();
    }
}

enum RunError {
    Message(String),
    MultipleResultSets,
    /// The session already has an open transaction, so a read-only one cannot be started safely.
    InTransaction(String),
}

/// A file export re-runs a user query, so it must never change data: the server enforces it.
/// MySQL `SET TRANSACTION` fails inside an open transaction, which also protects a caller's
/// manual transaction from the implicit commit of `START TRANSACTION`.
fn begin_read_only(adapter: &mut LiveAdapter) -> Result<(), RunError> {
    match adapter {
        LiveAdapter::MySql(conn) => {
            conn.query_drop("SET TRANSACTION READ ONLY").map_err(|err| {
                RunError::InTransaction(format!("read-only export could not start: {err}"))
            })?;
            conn.query_drop("START TRANSACTION READ ONLY")
                .map_err(|err| RunError::Message(format!("mysql read-only transaction error: {err}")))
        }
        LiveAdapter::PostgreSql(client) => {
            if probe_in_transaction_client(client) == Some(true) {
                return Err(RunError::InTransaction(
                    "read-only export cannot run inside an open transaction".to_string(),
                ));
            }
            client
                .batch_execute("BEGIN TRANSACTION READ ONLY")
                .map_err(|err| RunError::Message(format!("postgresql read-only transaction error: {err}")))
        }
    }
}

/// Never commits: the read-only transaction is always rolled back.
fn end_read_only(adapter: &mut LiveAdapter) {
    let _ = match adapter {
        LiveAdapter::MySql(conn) => conn.query_drop("ROLLBACK").map_err(|e| e.to_string()),
        LiveAdapter::PostgreSql(client) => client.batch_execute("ROLLBACK").map_err(|e| e.to_string()),
    };
}


struct Outcome {
    columns: Vec<String>,
    rows_affected: u64,
}

/// Stops the server-side work of a query whose remaining rows will never be read.
///
/// Abandoning a result makes the driver drain it, and that drain can take as long as the query
/// itself. The cancel therefore has to be in flight *while* the drain runs: `stop` is signalled
/// before the drain starts and the cancel is repeated until the fetch ends (measured: a cancel
/// that lands while the server is blocked writing to a reader that paused is not honoured, and a
/// 12M-row MySQL scan ran to completion). A repeat on an idle session is a no-op.
fn cancel_until_done(ctl: &JobCtl, stop: mpsc::Receiver<()>) {
    if stop.recv().is_err() {
        return;
    }
    loop {
        if ctl.session.canceller.send().is_ok() {
            ctl.server_cancel_sent.store(true, Ordering::SeqCst);
        }
        if !matches!(stop.recv_timeout(Duration::from_millis(300)), Err(mpsc::RecvTimeoutError::Timeout)) {
            return;
        }
    }
}

fn run_streaming(
    adapter: &mut LiveAdapter,
    spec: &QuerySpec,
    ctl: &JobCtl,
    sink: &mut Sink,
) -> Result<Outcome, RunError> {
    std::thread::scope(|scope| {
        let (stop, stop_rx) = mpsc::channel::<()>();
        scope.spawn(move || cancel_until_done(ctl, stop_rx));
        // `stop` drops when the fetch (including any drain) is over, which ends the cancel loop.
        fetch_rows(adapter, spec, ctl, sink, &stop)
    })
}

fn fetch_rows(
    adapter: &mut LiveAdapter,
    spec: &QuerySpec,
    ctl: &JobCtl,
    sink: &mut Sink,
    stop: &mpsc::Sender<()>,
) -> Result<Outcome, RunError> {
    match adapter {
        LiveAdapter::MySql(conn) => {
            let sql = mysql_bound_sql(conn, &spec.sql, &spec.params).map_err(RunError::Message)?;
            let mut result = conn
                .query_iter(sql)
                .map_err(|err| RunError::Message(format!("mysql query error: {err}")))?;
            let columns = unique_query_columns(
                result
                    .columns()
                    .as_ref()
                    .iter()
                    .map(|column| column.name_str().to_string())
                    .collect(),
            );
            let rows_affected = result.affected_rows();
            sink.binary = result.columns().as_ref().iter().map(mysql_column_is_binary).collect();
            if let Err(err) = sink.begin(&columns) {
                let _ = stop.send(());
                drop(result);
                return Err(RunError::Message(err));
            }
            let export_binary = sink.export_encoding().filter(|_| sink.binary.iter().any(|b| *b));
            let mut set_index = 0;
            let mut multi = false;
            let mut fetch_error = None;
            'sets: while let Some(set) = result.iter() {
                if set_index == 0 {
                    for row in set {
                        if ctl.stopped() {
                            let _ = stop.send(());
                            break 'sets;
                        }
                        match row {
                            Ok(row) => {
                                let json = match export_binary {
                                    Some(encoding) => mysql_export_row(&columns, &sink.binary, encoding, row),
                                    None => mysql_row_to_json(&columns, row),
                                };
                                if !sink.push(json) {
                                    // Must precede the drop of `set`, which drains the rest.
                                    let _ = stop.send(());
                                    break 'sets;
                                }
                            }
                            Err(err) => {
                                fetch_error = Some(format!("mysql query error: {err}"));
                                break 'sets;
                            }
                        }
                    }
                } else if !set.columns().as_ref().is_empty() {
                    multi = true;
                    break;
                }
                set_index += 1;
            }
            drop(result);
            if multi {
                return Err(RunError::MultipleResultSets);
            }
            if let Some(message) = fetch_error {
                if sink.truncated_by.is_none() {
                    return Err(RunError::Message(message));
                }
            }
            Ok(Outcome { columns, rows_affected })
        }
        LiveAdapter::PostgreSql(client) => {
            let sql = pg_bound_sql(client, &spec.sql, &spec.params).map_err(RunError::Message)?;
            let statement = client.prepare(&sql).map_err(|err| {
                if err
                    .as_db_error()
                    .is_some_and(|db| db.message().contains("multiple commands"))
                {
                    RunError::MultipleResultSets
                } else {
                    RunError::Message(format_postgres_error("postgresql query error", &err))
                }
            })?;
            let columns = unique_query_columns(
                statement.columns().iter().map(|column| column.name().to_string()).collect(),
            );
            let types: Vec<postgres::types::Type> =
                statement.columns().iter().map(|column| column.type_().clone()).collect();
            sink.binary = types.iter().map(|ty| *ty == postgres::types::Type::BYTEA).collect();
            sink.begin(&columns).map_err(RunError::Message)?;
            let export = sink.export_encoding();

            if statement.columns().is_empty() || !copy_wrappable(&sql) {
                // ponytail: SHOW/EXPLAIN/no-row statements cannot be wrapped in COPY, so they
                // are collected, not streamed; upgrade path is a cursor-based fetch.
                let messages = client
                    .simple_query(&sql)
                    .map_err(|err| RunError::Message(format_postgres_error("postgresql query error", &err)))?;
                let mut rows_affected = 0;
                for message in messages {
                    match message {
                        postgres::SimpleQueryMessage::Row(row) => {
                            let mut object = serde_json::Map::new();
                            for (index, ty) in types.iter().enumerate() {
                                let value = row
                                    .get(index)
                                    .map(|text| pg_value(text, ty, export))
                                    .transpose()
                                    .map_err(RunError::Message)?
                                    .unwrap_or(Value::Null);
                                object.insert(columns[index].clone(), value);
                            }
                            if !sink.push(Value::Object(object)) {
                                break;
                            }
                        }
                        postgres::SimpleQueryMessage::CommandComplete(count) => rows_affected = count,
                        _ => {}
                    }
                }
                return Ok(Outcome { columns, rows_affected });
            }

            let copy_sql = format!("COPY (\n{}\n) TO STDOUT", sql.trim_end().trim_end_matches(';'));
            let mut reader = client
                .copy_out(&copy_sql)
                .map_err(|err| RunError::Message(format_postgres_error("postgresql query error", &err)))?;
            let mut line = Vec::new();
            let mut halted = false;
            let mut fetch_error = None;
            loop {
                line.clear();
                match reader.read_until(b'\n', &mut line) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(err) => {
                        // The driver wraps its error in io::Error; keep the server's own message.
                        fetch_error = Some(
                            match err.get_ref().and_then(|inner| inner.downcast_ref::<postgres::Error>()) {
                                Some(pg) => format_postgres_error("postgresql query error", pg),
                                None => format!("postgresql query error: {err}"),
                            },
                        );
                        break;
                    }
                }
                if ctl.stopped() {
                    halted = true;
                    let _ = stop.send(());
                    break;
                }
                let row = parse_copy_line(&line, &columns, &types, export).map_err(RunError::Message)?;
                if !sink.push(row) {
                    halted = true;
                    let _ = stop.send(());
                    break;
                }
            }
            if halted {
                // Drain so the connection is clean for the next statement; the cancel is in flight.
                let _ = std::io::copy(&mut reader, &mut std::io::sink());
            }
            drop(reader);
            if let Some(message) = fetch_error {
                return Err(RunError::Message(message));
            }
            Ok(Outcome { columns, rows_affected: sink.streamed })
        }
    }
}

/// First keyword of the statement, skipping whitespace, comments and opening parens.
fn first_keyword(sql: &str) -> String {
    let bytes = sql.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b' ' | b'\t' | b'\r' | b'\n' | b'(' => i += 1,
            b'-' if bytes.get(i + 1) == Some(&b'-') => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            b'/' if bytes.get(i + 1) == Some(&b'*') => {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i += 2;
            }
            _ => break,
        }
    }
    sql.get(i.min(sql.len())..)
        .unwrap_or("")
        .chars()
        .take_while(|c| c.is_ascii_alphabetic())
        .collect::<String>()
        .to_ascii_lowercase()
}

fn copy_wrappable(sql: &str) -> bool {
    matches!(
        first_keyword(sql).as_str(),
        "select" | "with" | "values" | "table" | "insert" | "update" | "delete"
    )
}

/// Column value for the UI (typed JSON) or, for file export (`export` set), exact text:
/// numbers, JSON, arrays and temporal values keep the server's own text (no float rounding);
/// only booleans become JSON booleans and bytea is encoded as hex/base64.
fn pg_value(text: &str, ty: &postgres::types::Type, export: Option<BinaryEncoding>) -> Result<Value, String> {
    use postgres::types::Type;
    match export {
        None => postgres_text_value(text, ty),
        Some(encoding) => Ok(match *ty {
            Type::BOOL => Value::Bool(text == "t"),
            Type::BYTEA => Value::String(pg_bytea_text(text, encoding)),
            _ => Value::String(text.to_string()),
        }),
    }
}

fn mysql_column_is_binary(column: &mysql::Column) -> bool {
    use mysql::consts::ColumnType::*;
    // Character set 63 is "binary"; numbers and dates carry it too, so restrict by type.
    matches!(column.column_type(), MYSQL_TYPE_BIT | MYSQL_TYPE_GEOMETRY)
        || (column.character_set() == 63
            && matches!(
                column.column_type(),
                MYSQL_TYPE_TINY_BLOB
                    | MYSQL_TYPE_MEDIUM_BLOB
                    | MYSQL_TYPE_LONG_BLOB
                    | MYSQL_TYPE_BLOB
                    | MYSQL_TYPE_VAR_STRING
                    | MYSQL_TYPE_STRING
                    | MYSQL_TYPE_VARCHAR
            ))
}

/// Like `mysql_row_to_json`, but binary columns are encoded from their raw bytes (the plain
/// conversion is lossy UTF-8).
fn mysql_export_row(columns: &[String], binary: &[bool], encoding: BinaryEncoding, row: mysql::Row) -> Value {
    let raw: Vec<(usize, Option<Vec<u8>>)> = (0..columns.len())
        .filter(|index| binary.get(*index).copied().unwrap_or(false))
        .map(|index| {
            let bytes = match row.as_ref(index) {
                Some(mysql::Value::Bytes(bytes)) => Some(bytes.clone()),
                _ => None,
            };
            (index, bytes)
        })
        .collect();
    let mut json = mysql_row_to_json(columns, row);
    if let Value::Object(object) = &mut json {
        for (index, bytes) in raw {
            if let Some(bytes) = bytes {
                object.insert(columns[index].clone(), Value::String(encode_binary(&bytes, encoding)));
            }
        }
    }
    json
}

/// One `COPY ... TO STDOUT` text-format line -> JSON object (same typing as simple_query text).
fn parse_copy_line(
    line: &[u8],
    columns: &[String],
    types: &[postgres::types::Type],
    export: Option<BinaryEncoding>,
) -> Result<Value, String> {
    let line = line.strip_suffix(b"\n").unwrap_or(line);
    let mut object = serde_json::Map::new();
    for (index, field) in line.split(|b| *b == b'\t').enumerate() {
        let (Some(name), Some(ty)) = (columns.get(index), types.get(index)) else {
            return Err("postgresql COPY row has more fields than columns".to_string());
        };
        let value = if field == b"\\N" {
            Value::Null
        } else {
            pg_value(&copy_unescape(field), ty, export)?
        };
        object.insert(name.clone(), value);
    }
    Ok(Value::Object(object))
}

fn copy_unescape(field: &[u8]) -> String {
    let mut out = Vec::with_capacity(field.len());
    let mut i = 0;
    while i < field.len() {
        if field[i] != b'\\' || i + 1 >= field.len() {
            out.push(field[i]);
            i += 1;
            continue;
        }
        i += 1;
        match field[i] {
            b'b' => out.push(8),
            b'f' => out.push(12),
            b'n' => out.push(b'\n'),
            b'r' => out.push(b'\r'),
            b't' => out.push(b'\t'),
            b'v' => out.push(11),
            b'0'..=b'7' => {
                let mut value = 0u32;
                let mut digits = 0;
                while digits < 3 && i < field.len() && (b'0'..=b'7').contains(&field[i]) {
                    value = value * 8 + (field[i] - b'0') as u32;
                    i += 1;
                    digits += 1;
                }
                out.push(value as u8);
                continue;
            }
            b'x' if i + 1 < field.len() && field[i + 1].is_ascii_hexdigit() => {
                let mut value = 0u32;
                let mut digits = 0;
                i += 1;
                while digits < 2 && i < field.len() && field[i].is_ascii_hexdigit() {
                    value = value * 16 + (field[i] as char).to_digit(16).unwrap();
                    i += 1;
                    digits += 1;
                }
                out.push(value as u8);
                continue;
            }
            other => out.push(other),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Known for PostgreSQL only: `now()` is the transaction start, so it differs from the
/// statement time inside an explicit transaction. A failed transaction (25P02) is still open.
fn probe_in_transaction(adapter: &mut LiveAdapter) -> Option<bool> {
    let LiveAdapter::PostgreSql(client) = adapter else {
        return None;
    };
    probe_in_transaction_client(client)
}

fn probe_in_transaction_client(client: &mut postgres::Client) -> Option<bool> {
    match client.simple_query("SELECT now() <> statement_timestamp()") {
        Ok(messages) => messages.into_iter().find_map(|message| match message {
            postgres::SimpleQueryMessage::Row(row) => row.get(0).map(|text| text == "t"),
            _ => None,
        }),
        Err(err) if err.code().is_some_and(|code| code.code() == "25P02") => Some(true),
        Err(_) => None,
    }
}

type JobRun = (Result<Outcome, RunError>, Option<&'static str>, u64, Vec<Value>, Option<bool>, Value);

/// Runs one query job to completion and emits its final event. Never panics outward.
pub(crate) fn run_job(session: Arc<Session>, ctl: Arc<JobCtl>, jobs: Jobs, spec: QuerySpec, emit: Emitter) {
    let (stop_watch, watch_rx) = mpsc::channel::<()>();
    if let Some(ms) = spec.timeout_ms {
        let ctl = ctl.clone();
        std::thread::spawn(move || {
            if let Err(mpsc::RecvTimeoutError::Timeout) = watch_rx.recv_timeout(Duration::from_millis(ms)) {
                let _ = ctl.cancel(true);
            }
        });
    }

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> JobRun {
        let mut adapter = lock(&session.adapter);
        let mut sink = Sink::new(&emit, &spec);
        let exporting = spec.output.is_some();
        let begun = if exporting { begin_read_only(&mut adapter) } else { Ok(()) };
        let began = begun.is_ok();
        let mut result = match begun {
            Ok(()) => run_streaming(&mut adapter, &spec, &ctl, &mut sink),
            Err(err) => Err(err),
        };
        sink.flush();
        if exporting && began {
            end_read_only(&mut adapter);
        }
        // A file is only ever published after a complete, error-free run.
        let mut output_info = Value::Null;
        if let Some(writer) = sink.out.take() {
            let (rows, bytes) = (writer.rows, writer.bytes);
            if let Some(err) = sink.io_error.take() {
                result = Err(RunError::Message(err));
            }
            if result.is_ok() && !ctl.stopped() {
                match writer.finish() {
                    Ok(path) => {
                        output_info = json!({
                            "output_path": path.to_string_lossy(), "rows_written": rows, "bytes_written": bytes
                        });
                    }
                    Err(err) => result = Err(RunError::Message(err)),
                }
            } else {
                let partial = writer.abort().map(|path| path.to_string_lossy().to_string());
                output_info = json!({
                    "output_path": Value::Null, "partial_path": partial,
                    "rows_written": rows, "bytes_written": bytes
                });
            }
        }
        let stopped = ctl.stopped() || sink.truncated_by.is_some() || result.is_err();
        let in_transaction = if stopped { probe_in_transaction(&mut adapter) } else { None };
        (result, sink.truncated_by, sink.streamed, std::mem::take(&mut sink.all), in_transaction, output_info)
    }));

    // Free the session before the final event so a client's next request never sees `busy`.
    ctl.finished.store(true, Ordering::SeqCst);
    drop(stop_watch);
    lock(&jobs).remove(&spec.job_id);
    *lock(&session.running) = None;

    let server_cancel_sent = ctl.server_cancel_sent.load(Ordering::SeqCst);
    let Ok((result, truncated_by, streamed, rows, in_transaction, output_info)) = outcome else {
        emit(json!({
            "event": "error", "request_id": spec.request_id, "command": "query.execute",
            "job_id": spec.job_id, "message": "query worker panicked"
        }));
        return;
    };
    let base = |mut event: Value| {
        event["request_id"] = json!(spec.request_id);
        event["command"] = json!("query.execute");
        event["job_id"] = json!(spec.job_id);
        if let (Some(target), Some(extra)) = (event.as_object_mut(), output_info.as_object()) {
            target.extend(extra.iter().map(|(k, v)| (k.clone(), v.clone())));
        }
        event
    };
    let timed_out = ctl.timed_out.load(Ordering::SeqCst);
    let cancelled = ctl.cancelled.load(Ordering::SeqCst);
    if timed_out || cancelled {
        let (code, message) = if timed_out {
            ("query_timeout", "쿼리 제한시간이 지나 서버에서 취소되었습니다")
        } else {
            ("query_cancelled", "쿼리가 사용자에 의해 취소되었습니다")
        };
        emit(base(json!({
            "event": "result", "success": false, "error_code": code, "message": message,
            "cancelled": cancelled && !timed_out, "timed_out": timed_out,
            "server_cancel_sent": server_cancel_sent, "rows_streamed": streamed,
            "in_transaction": in_transaction
        })));
        return;
    }
    match result {
        Ok(outcome) => emit(base(json!({
            "event": "result", "success": true,
            "rows": rows, "columns": outcome.columns, "rows_affected": outcome.rows_affected,
            "rows_streamed": streamed,
            "truncated": truncated_by.is_some(), "truncated_by": truncated_by,
            "cancelled": false, "timed_out": false,
            "server_cancel_sent": server_cancel_sent, "in_transaction": in_transaction
        }))),
        Err(RunError::MultipleResultSets) => emit(base(json!({
            "event": "error", "error_code": "multiple_result_sets_unsupported",
            "message": "여러 결과 집합을 반환하는 문장은 지원하지 않습니다",
            "in_transaction": in_transaction
        }))),
        Err(RunError::InTransaction(message)) => emit(base(json!({
            "event": "error", "error_code": "export_session_in_transaction", "message": message,
            "in_transaction": in_transaction
        }))),
        Err(RunError::Message(message)) if session.read_only() && is_read_only_violation(&message) => {
            emit(base(json!({
                "event": "error", "error_code": READ_ONLY_ERROR_CODE,
                "message": format!("읽기 전용 세션에서는 데이터를 변경할 수 없습니다: {message}"),
                "in_transaction": in_transaction
            })))
        }
        Err(RunError::Message(message)) if spec.output.is_some() && is_read_only_violation(&message) => {
            emit(base(json!({
                "event": "error", "error_code": "export_requires_read_only",
                "message": format!("이 쿼리는 데이터를 변경하므로 파일로 저장할 수 없습니다 (읽기 전용 트랜잭션에서 거부됨): {message}"),
                "in_transaction": in_transaction
            })))
        }
        Err(RunError::Message(message)) => emit(base(json!({
            "event": "error", "message": message, "in_transaction": in_transaction
        }))),
    }
}

/// Registers a job unless the session is busy. Err carries the error event to emit.
pub(crate) fn register_job(session: &Arc<Session>, jobs: &Jobs, job_id: &str) -> Result<Arc<JobCtl>, Value> {
    let mut running = lock(&session.running);
    if let Some(existing) = running.as_ref() {
        return Err(json!({
            "event": "error", "error_code": "connection_busy",
            "message": format!("이 연결에서 다른 쿼리가 실행 중입니다 (job_id={existing})")
        }));
    }
    let mut table = lock(jobs);
    if table.contains_key(job_id) {
        return Err(json!({"event": "error", "message": format!("duplicate job_id: {job_id}")}));
    }
    let ctl = Arc::new(JobCtl::new(session.clone()));
    table.insert(job_id.to_string(), ctl.clone());
    *running = Some(job_id.to_string());
    Ok(ctl)
}

pub(crate) fn cancel_events(request: &Request, jobs: &Jobs) -> Vec<Value> {
    let job_id = request.payload.get("job_id").and_then(Value::as_str).unwrap_or("");
    let ctl = lock(jobs).get(job_id).cloned();
    let (cancelled, sent, message) = match ctl {
        None => (false, false, Some("실행 중인 쿼리가 없습니다".to_string())),
        Some(ctl) => match ctl.cancel(false) {
            Ok(true) => (true, true, None),
            Ok(false) => (false, false, Some("쿼리가 이미 끝났습니다".to_string())),
            Err(err) => (true, false, Some(err)),
        },
    };
    vec![json!({
        "event": "result", "request_id": request.request_id, "command": "query.cancel",
        "success": true, "job_id": job_id, "cancelled": cancelled,
        "server_cancel_sent": sent, "message": message
    })]
}

pub(crate) fn new_jobs() -> Jobs {
    Arc::new(Mutex::new(HashMap::new()))
}

/// Cancels every running job on `session` (connection.close while a query runs).
pub(crate) fn cancel_session_jobs(session: &Arc<Session>, jobs: &Jobs) {
    let running: Vec<Arc<JobCtl>> = lock(jobs)
        .values()
        .filter(|ctl| Arc::ptr_eq(ctl.session(), session))
        .cloned()
        .collect();
    for ctl in running {
        let _ = ctl.cancel(false);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn copy_unescape_decodes_text_format_escapes() {
        assert_eq!(copy_unescape(b"a\\tb\\nc\\\\d\\101\\x41"), "a\tb\nc\\dAA");
    }

    #[test]
    fn first_keyword_skips_comments_and_parens() {
        assert_eq!(first_keyword("  -- x\n /* y */ (SELECT 1)"), "select");
        assert!(copy_wrappable("with a as (select 1) select * from a"));
        assert!(!copy_wrappable("SHOW server_version"));
        assert!(!copy_wrappable("EXPLAIN select 1"));
    }

    #[test]
    fn sink_truncates_by_rows_and_bytes() {
        let emit: Emitter = Arc::new(|_| {});
        let request = Request {
            command: "query.execute".into(),
            request_id: None,
            payload: json!({}),
        };
        let mut spec = QuerySpec::from_request(&request, "j".into(), "select 1".into()).unwrap();
        spec.max_rows = Some(2);
        let mut sink = Sink::new(&emit, &spec);
        assert!(sink.push(json!(1)) && sink.push(json!(2)));
        assert!(!sink.push(json!(3)));
        assert_eq!(sink.truncated_by, Some("rows"));

        spec.max_rows = None;
        spec.max_bytes = Some(5);
        let mut sink = Sink::new(&emit, &spec);
        assert!(sink.push(json!("aaaa")));
        assert!(!sink.push(json!("bbbb")));
        assert_eq!(sink.truncated_by, Some("bytes"));
    }
}
