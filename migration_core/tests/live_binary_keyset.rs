//! Binary primary keys must advance every keyset cursor (dump, migration copy, verification).
//! The keyset token is the HEX text of the key; the next-page predicate has to decode it back to bytes.
//! Requires disposable MySQL and PostgreSQL (TF_MYSQL_* / TF_POSTGRES_*); skipped otherwise.
use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

const ROW_COUNT: usize = 7;
const CHUNK_SIZE: usize = 3;
const PATH_TIMEOUT_SECS: u64 = 40;

fn endpoint(prefix: &str, engine: &str, port: u16) -> Option<Endpoint> {
    Some(Endpoint {
        engine: engine.into(),
        host: std::env::var(format!("{prefix}_HOST")).ok()?,
        port: std::env::var(format!("{prefix}_PORT")).ok().and_then(|v| v.parse().ok()).unwrap_or(port),
        user: std::env::var(format!("{prefix}_USER")).ok()?,
        password: std::env::var(format!("{prefix}_PASSWORD")).unwrap_or_default(),
        database: std::env::var(format!("{prefix}_DATABASE")).ok()?,
        schema: None,
        tls: Default::default(),
    })
}

/// Runs a request on a worker thread so a cursor that never advances is reported as LOOP instead of hanging.
fn request_with_timeout(command: &str, payload: Value) -> Result<Value, String> {
    let (sender, receiver) = mpsc::channel();
    let command = command.to_string();
    std::thread::spawn(move || {
        let events = handle_request(Request { command, request_id: None, payload });
        let _ = sender.send(events);
    });
    let events = receiver
        .recv_timeout(Duration::from_secs(PATH_TIMEOUT_SECS))
        .map_err(|_| format!("LOOP (no result within {PATH_TIMEOUT_SECS}s)"))?;
    if let Some(error) = events.iter().find(|event| event["event"] == "error") {
        return Err(format!("ERROR {}", error["message"]));
    }
    events
        .into_iter()
        .find(|event| event["event"] == "result")
        .ok_or_else(|| "no result event".to_string())
}

fn binary_key(index: usize) -> String {
    // First bytes straddle the ASCII/raw ordering differences (0x00, 0x41 'A', 0x7e, 0x80, 0xc3, 0xff).
    let first = [0x01u8, 0x10, 0x41, 0x7e, 0x80, 0xc3, 0xff][index];
    format!("{first:02x}{:02x}00ff5c09a1b2c3d4e5f60718293a", index as u8 + 0x20)
}

struct Fixture {
    table: String,
    columns: Value,
    create_sql: String,
    insert_sql: String,
}

fn fixtures(engine: &str, suffix: u128) -> Vec<Fixture> {
    let mysql = engine == "mysql";
    let literal = |hex: &str| if mysql { format!("X'{hex}'") } else { format!("decode('{hex}','hex')") };
    let bin16 = if mysql { "BINARY(16)" } else { "BYTEA" };
    let rows = |extra: &dyn Fn(usize) -> String| {
        (0..ROW_COUNT).map(|i| format!("({})", extra(i))).collect::<Vec<_>>().join(",")
    };
    let mut result = Vec::new();

    let single = format!("tf_bk_single_{suffix}");
    result.push(Fixture {
        columns: json!([
            {"name": "id", "type": if mysql { "binary(16)" } else { "bytea" }, "nullable": false, "primary_key": true},
            {"name": "label", "type": "varchar(32)", "nullable": true}]),
        create_sql: format!("CREATE TABLE {single} (id {bin16} PRIMARY KEY, label VARCHAR(32))"),
        insert_sql: format!("INSERT INTO {single} VALUES {}", rows(&|i| format!("{}, 'row{i}'", literal(&binary_key(i))))),
        table: single,
    });

    let composite = format!("tf_bk_comp_{suffix}");
    result.push(Fixture {
        columns: json!([
            {"name": "grp", "type": "int", "nullable": false, "primary_key": true},
            {"name": "id", "type": if mysql { "binary(16)" } else { "bytea" }, "nullable": false, "primary_key": true},
            {"name": "label", "type": "varchar(32)", "nullable": true}]),
        create_sql: format!("CREATE TABLE {composite} (grp INT NOT NULL, id {bin16} NOT NULL, label VARCHAR(32), PRIMARY KEY (grp, id))"),
        insert_sql: format!("INSERT INTO {composite} VALUES {}", rows(&|i| format!("{}, {}, 'row{i}'", i / 4, literal(&binary_key(i))))),
        table: composite,
    });

    let integer = format!("tf_bk_int_{suffix}");
    result.push(Fixture {
        columns: json!([
            {"name": "id", "type": "int", "nullable": false, "primary_key": true},
            {"name": "label", "type": "varchar(32)", "nullable": true}]),
        create_sql: format!("CREATE TABLE {integer} (id INT PRIMARY KEY, label VARCHAR(32))"),
        insert_sql: format!("INSERT INTO {integer} VALUES {}", rows(&|i| format!("{}, 'row{i}'", i + 1))),
        table: integer,
    });

    if mysql {
        let varbin = format!("tf_bk_varbin_{suffix}");
        result.push(Fixture {
            columns: json!([
                {"name": "id", "type": "varbinary(20)", "nullable": false, "primary_key": true},
                {"name": "label", "type": "varchar(32)", "nullable": true}]),
            create_sql: format!("CREATE TABLE {varbin} (id VARBINARY(20) PRIMARY KEY, label VARCHAR(32))"),
            insert_sql: format!("INSERT INTO {varbin} VALUES {}", rows(&|i| format!("{}, 'row{i}'", literal(&binary_key(i)[..(8 + i * 2)])))),
            table: varbin,
        });
    }
    result
}

fn data_lines(dir: &Path, extension: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(path) = stack.pop() {
        for entry in std::fs::read_dir(&path).unwrap().flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some(extension)
                && path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("chunk_"))
            {
                lines.extend(std::fs::read_to_string(&path).unwrap().lines().map(str::to_string));
            }
        }
    }
    lines
}

fn count_rows(endpoint: &Endpoint, table: &str) -> Result<u64, String> {
    let result = request_with_timeout("query.execute", json!({"connection": endpoint, "sql": format!("SELECT COUNT(*) AS n FROM {table}")}))?;
    result["rows"][0]["n"].to_string().trim_matches('"').parse().map_err(|e| format!("count parse: {e}"))
}

#[test]
fn binary_primary_keys_advance_every_keyset_cursor_when_configured() {
    let (Some(mysql), Some(postgres)) = (endpoint("TF_MYSQL", "mysql", 3306), endpoint("TF_POSTGRES", "postgresql", 5432)) else {
        eprintln!("skipping binary keyset regression: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };
    let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let mut report: Vec<String> = Vec::new();
    let mut failures = 0usize;
    let mut record = |path: String, outcome: Result<String, String>| match outcome {
        Ok(note) => report.push(format!("OK    {path} {note}")),
        Err(note) => {
            failures += 1;
            report.push(format!("FAIL  {path} {note}"));
        }
    };

    for (source, other) in [(&mysql, &postgres), (&postgres, &mysql)] {
        let mut source_db = LiveAdapter::connect(source).unwrap();
        let mut other_db = LiveAdapter::connect(other).unwrap();
        for fixture in fixtures(&source.engine, suffix) {
            source_db.execute_sql(&fixture.create_sql).unwrap();
            source_db.execute_sql(&fixture.insert_sql).unwrap();
            let name = fixture.table.clone();

            // dump.run: every format and thread mode must write each row exactly once.
            for (format, extension) in [("jsonl", "jsonl"), ("tsv", "tsv")] {
                for threads in [1, 2] {
                    let dir = std::env::temp_dir().join(format!("tf-bk-{suffix}-{name}-{format}-{threads}"));
                    let outcome = request_with_timeout("dump.run", json!({
                        "source": source, "tables": [name], "output_dir": dir, "threads": threads,
                        "chunk_size": CHUNK_SIZE, "data_format": format, "compression": "none"}))
                    .and_then(|result| {
                        let lines = data_lines(&dir, extension);
                        let distinct: BTreeSet<&String> = lines.iter().collect();
                        if lines.len() == ROW_COUNT && distinct.len() == ROW_COUNT {
                            let _ = &result;
                            Ok(format!("rows={}", lines.len()))
                        } else {
                            Err(format!("rows written={} distinct={} (expected {ROW_COUNT}: {} missing)", lines.len(), distinct.len(), ROW_COUNT.saturating_sub(distinct.len())))
                        }
                    });
                    record(format!("dump.run {} {name} {format} threads={threads}", source.engine), outcome);
                    let _ = std::fs::remove_dir_all(&dir);
                }
            }

            // A binary key cannot become a MySQL key without a prefix length (separate schema-mapping limit),
            // so PostgreSQL BYTEA keys are covered by dump.run and by the verify of the MySQL -> PostgreSQL copy.
            if source.engine == "postgresql" && !name.contains("_int_") {
                let _ = other_db.execute_sql(&format!("DROP TABLE IF EXISTS {name}"));
                source_db.execute_sql(&format!("DROP TABLE IF EXISTS {name}")).unwrap();
                continue;
            }
            // migrate (copy through read_rows_after_key) + verify (digest through read_rows_after_key on both sides)
            let payload = json!({
                "source_engine": source.engine, "target_engine": other.engine,
                "source": source, "target": other,
                "schema": {"tables": [{"name": name, "columns": fixture.columns}]},
                "execution_options": {"mode": "create_only", "chunk_size": CHUNK_SIZE}});
            let migrated = request_with_timeout("migrate", payload.clone()).and_then(|result| {
                if result["success"] == true && result["rows_copied"] == json!(ROW_COUNT) {
                    count_rows(other, &name).and_then(|n| if n == ROW_COUNT as u64 { Ok(format!("rows_copied={ROW_COUNT}")) } else { Err(format!("target has {n} rows")) })
                } else {
                    Err(format!("success={} rows_copied={} issues={}", result["success"], result["rows_copied"], result["issues"]))
                }
            });
            let copied = migrated.is_ok();
            record(format!("migrate {} -> {} {name}", source.engine, other.engine), migrated);
            if copied {
                let verified = request_with_timeout("verify", payload).and_then(|result| {
                    if result["success"] == true { Ok("verified".to_string()) } else { Err(format!("mismatches={}", result["mismatches"])) }
                });
                record(format!("verify  {} -> {} {name}", source.engine, other.engine), verified);
            }
            let _ = other_db.execute_sql(&format!("DROP TABLE IF EXISTS {name}"));
            source_db.execute_sql(&format!("DROP TABLE IF EXISTS {name}")).unwrap();
        }
    }
    println!("{}", report.join("\n"));
    assert_eq!(failures, 0, "binary keyset regressions:\n{}", report.join("\n"));
}
