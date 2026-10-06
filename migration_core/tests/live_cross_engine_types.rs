//! Cross-engine migration keeps value range, precision and keyability (TF-STATUS-148).
//! MySQL FLOAT/DOUBLE/SMALLINT/YEAR used to become PostgreSQL TEXT, UNSIGNED integers overflowed,
//! PostgreSQL TEXT/UUID keys could not be created in MySQL and TIMESTAMPTZ values were rejected.
//! Requires disposable MySQL and PostgreSQL (TF_MYSQL_* / TF_POSTGRES_*); skipped otherwise,
//! but TF_LIVE_REQUIRED turns a missing environment into a failure.
use migration_core::{handle_request, Endpoint, LiveAdapter, MigrationAdapter, Request};
use serde_json::{json, Value};

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

fn run(command: &str, payload: Value) -> Result<Value, String> {
    let events = handle_request(Request { command: command.into(), request_id: None, payload });
    if let Some(error) = events.iter().find(|event| event["event"] == "error") {
        return Err(error["message"].to_string());
    }
    events.into_iter().find(|event| event["event"] == "result").ok_or_else(|| "no result".into())
}

fn first_row(endpoint: &Endpoint, sql: &str) -> Value {
    run("query.execute", json!({"connection": endpoint, "sql": sql})).map(|result| result["rows"][0].clone()).unwrap_or(Value::Null)
}

fn col(name: &str, ty: &str, pk: bool) -> Value {
    json!({"name": name, "type": ty, "nullable": !pk, "primary_key": pk})
}

/// migrate (create_only) + verify; returns failures.
fn migrate_and_verify(source: &Endpoint, target: &Endpoint, table: &str, columns: Value, extra: Value) -> Vec<String> {
    let mut schema_table = json!({"name": table, "columns": columns});
    for (key, value) in extra.as_object().into_iter().flatten() {
        schema_table[key] = value.clone();
    }
    migrate_tables(source, target, table, json!([schema_table]))
}

fn migrate_tables(source: &Endpoint, target: &Endpoint, table: &str, tables: Value) -> Vec<String> {
    let payload = json!({"source_engine": source.engine, "target_engine": target.engine, "source": source, "target": target,
        "schema": {"tables": tables}, "execution_options": {"mode": "create_only", "chunk_size": 2}});
    let mut failures = Vec::new();
    match run("migrate", payload.clone()) {
        Ok(result) if result["success"] == true => match run("verify", payload) {
            Ok(result) if result["success"] == true => {}
            other => failures.push(format!("verify {} -> {} {table}: {other:?}", source.engine, target.engine)),
        },
        other => failures.push(format!("migrate {} -> {} {table}: {other:?}", source.engine, target.engine)),
    }
    failures
}

/// Locations of the blocking issues `preflight` reports for one table.
fn preflight_refusals(source: &Endpoint, target: &Endpoint, table: &str, columns: Value) -> Vec<String> {
    let payload = json!({"source_engine": source.engine, "target_engine": target.engine, "source": source, "target": target,
        "schema": {"tables": [{"name": table, "columns": columns}]}, "execution_options": {"mode": "create_only", "chunk_size": 2}});
    let result = run("preflight", payload).unwrap_or(Value::Null);
    result["issues"].as_array().into_iter().flatten()
        .filter(|issue| issue["blocking"] == true)
        .filter_map(|issue| issue["location"].as_str().map(str::to_string))
        .collect()
}

#[test]
fn cross_engine_migration_keeps_types_when_configured() {
    let (Some(mysql), Some(postgres)) = (endpoint("TF_MYSQL", "mysql", 3306), endpoint("TF_POSTGRES", "postgresql", 5432)) else {
        assert!(std::env::var_os("TF_LIVE_REQUIRED").is_none(), "TF_LIVE_REQUIRED is set but TF_MYSQL_* / TF_POSTGRES_* are missing");
        eprintln!("skipping cross-engine type regression: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };
    let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let mut my = LiveAdapter::connect(&mysql).unwrap();
    let mut pg = LiveAdapter::connect(&postgres).unwrap();
    let mut failures = Vec::new();

    // MySQL -> PostgreSQL
    let m = format!("tf_xtypes_my_{suffix}");
    my.execute_sql(&format!("CREATE TABLE {m} (id BIGINT UNSIGNED PRIMARY KEY, ti TINYINT, si SMALLINT UNSIGNED, mi MEDIUMINT, iu INT UNSIGNED, \
        f FLOAT, d DOUBLE, y YEAR, amount DECIMAL(10,2) UNSIGNED, c CHAR(5), ts TIMESTAMP(3) NULL, en ENUM('a','b'))")).unwrap();
    my.execute_sql("SET SESSION time_zone = '+00:00'").unwrap();
    my.execute_sql(&format!("INSERT INTO {m} VALUES \
        (18446744073709551615, -128, 65535, -8388608, 4294967295, 0.1, 1e300, 2024, 99999999.99, 'abc', '2026-11-01 05:30:00.123', 'b'), \
        (1, 127, 0, 8388607, 0, -3.25, -0.5, 1901, 0.01, '', NULL, 'a')")).unwrap();
    let my_columns = json!([col("id", "bigint unsigned", true), col("ti", "tinyint", false), col("si", "smallint unsigned", false),
        col("mi", "mediumint", false), col("iu", "int unsigned", false), col("f", "float", false), col("d", "double", false),
        col("y", "year", false), col("amount", "decimal(10,2) unsigned", false), col("c", "char(5)", false),
        col("ts", "timestamp(3)", false), col("en", "enum('a','b')", false)]);
    failures.extend(migrate_and_verify(&mysql, &postgres, &m, my_columns, json!({})));
    let types = run("query.execute", json!({"connection": &postgres, "sql": format!(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{m}' ORDER BY ordinal_position")}));
    if let Ok(types) = types {
        let got = types["rows"].as_array().unwrap().iter().map(|row| format!("{}={}", row["column_name"].as_str().unwrap_or(""), row["data_type"].as_str().unwrap_or(""))).collect::<Vec<_>>().join(",");
        for expected in ["id=numeric", "ti=smallint", "si=integer", "mi=integer", "iu=bigint", "f=double precision", "d=double precision", "y=smallint"] {
            if !got.contains(expected) {
                failures.push(format!("MySQL -> PostgreSQL type {expected} missing: {got}"));
            }
        }
    }
    let ts = first_row(&postgres, &format!("SELECT ts::text AS ts FROM {m} WHERE id = 18446744073709551615"));
    if ts["ts"] != "2026-11-01 05:30:00.123+00" {
        failures.push(format!("MySQL TIMESTAMP moved in PostgreSQL: {ts}"));
    }

    // PostgreSQL -> MySQL (text and uuid keys, timestamptz, real, unconstrained numeric, arrays, inet)
    let p = format!("tf_xtypes_pg_{suffix}");
    pg.execute_sql(&format!("CREATE TABLE {p} (id UUID PRIMARY KEY, code TEXT NOT NULL UNIQUE, s SMALLINT, r REAL, dp DOUBLE PRECISION, \
        n NUMERIC, note TEXT, ts TIMESTAMPTZ(3), ip INET, tags INTEGER[])")).unwrap();
    pg.execute_sql(&format!("INSERT INTO {p} VALUES \
        ('00000000-0000-4000-8000-000000000001', 'alpha', -32768, 0.1, 1e300, 12.5, repeat('x', 70000), '2026-11-01 05:30:00.123+00', '10.0.0.1', '{{1,2}}'), \
        ('00000000-0000-4000-8000-000000000002', 'beta', 32767, -2.5, -0.25, 0.000000000000000000000000000001, NULL, NULL, NULL, NULL)")).unwrap();
    let pg_columns = json!([col("id", "uuid", true), {"name": "code", "type": "text", "nullable": false, "unique": true},
        col("s", "smallint", false), col("r", "real", false), col("dp", "double precision", false), col("n", "numeric", false),
        col("note", "text", false), col("ts", "timestamp(3) with time zone", false), col("ip", "inet", false), col("tags", "integer[]", false)]);
    failures.extend(migrate_and_verify(&postgres, &mysql, &p, pg_columns,
        json!({"indexes": [{"name": format!("{p}_code_key"), "columns": ["code"], "unique": true}]})));
    let row = first_row(&mysql, &format!("SELECT CAST(ts AS CHAR) AS ts, CHAR_LENGTH(note) AS note_len FROM {p} WHERE code = 'alpha'"));
    if row["ts"] != "2026-11-01 05:30:00.123" || row["note_len"].to_string().trim_matches('"') != "70000" {
        failures.push(format!("PostgreSQL -> MySQL values: {row}"));
    }

    // MySQL -> PostgreSQL: UNSIGNED identity stays an integer identity; TINYINT(1) 2, ZEROFILL and YEAR '0000' verify.
    let ai = format!("tf_xtypes_ai_{suffix}");
    my.execute_sql(&format!("CREATE TABLE {ai} (id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, flag TINYINT(1), zf INT(4) UNSIGNED ZEROFILL, y YEAR)")).unwrap();
    my.execute_sql(&format!("INSERT INTO {ai} (flag, zf, y) VALUES (2, 7, '0000'), (0, 1234, 2024)")).unwrap();
    // Children: one references the identity (BIGINT), one the NUMERIC(20,0) key of {m}.
    let child = format!("tf_xtypes_child_{suffix}");
    my.execute_sql(&format!("CREATE TABLE {child} (cid INT PRIMARY KEY, ai_id BIGINT UNSIGNED, m_id BIGINT UNSIGNED, \
        FOREIGN KEY (ai_id) REFERENCES {ai} (id), FOREIGN KEY (m_id) REFERENCES {m} (id))")).unwrap();
    my.execute_sql(&format!("INSERT INTO {child} VALUES (1, 1, 1), (2, 2, NULL)")).unwrap();
    let fk = |name: &str, column: &str, parent: &str| json!({"name": name, "columns": [column], "referenced_table": parent, "referenced_columns": ["id"]});
    failures.extend(migrate_tables(&mysql, &postgres, &ai, json!([
        {"name": ai, "columns": [col("id", "bigint unsigned auto_increment", true), col("flag", "tinyint(1)", false),
            col("zf", "int(4) unsigned zerofill", false), col("y", "year", false)]},
        {"name": child, "columns": [col("cid", "int", true), col("ai_id", "bigint unsigned", false), col("m_id", "bigint unsigned", false)],
            "foreign_keys": [fk(&format!("{child}_ai"), "ai_id", &ai), fk(&format!("{child}_m"), "m_id", &m)]},
    ])));

    // PostgreSQL -> MySQL: a four-column text key fits the 3072-byte limit.
    let wide = format!("tf_xtypes_wide_{suffix}");
    pg.execute_sql(&format!("CREATE TABLE {wide} (a TEXT, b TEXT, c TEXT, d TEXT, PRIMARY KEY (a, b, c, d))")).unwrap();
    pg.execute_sql(&format!("INSERT INTO {wide} VALUES ('x', 'y', 'z', 'w'), (repeat('a', 192), 'b', 'c', 'd')")).unwrap();
    failures.extend(migrate_and_verify(&postgres, &mysql, &wide, json!([col("a", "text", true), col("b", "text", true),
        col("c", "text", true), col("d", "text", true)]), json!({})));

    // PostgreSQL NaN has no MySQL DOUBLE value: the migration must fail, not store 0.
    let nan = format!("tf_xtypes_nan_{suffix}");
    pg.execute_sql(&format!("CREATE TABLE {nan} (id INTEGER PRIMARY KEY, v DOUBLE PRECISION)")).unwrap();
    pg.execute_sql(&format!("INSERT INTO {nan} VALUES (1, 'NaN')")).unwrap();
    if migrate_and_verify(&postgres, &mysql, &nan, json!([col("id", "integer", true), col("v", "double precision", false)]), json!({})).is_empty() {
        failures.push(format!("PostgreSQL NaN reached MySQL: {}", first_row(&mysql, &format!("SELECT v FROM {nan}"))));
    }

    // Temporal values the other engine cannot store are refused by preflight, before any DDL.
    let zero = format!("tf_xtypes_zero_{suffix}");
    my.execute_sql("SET SESSION sql_mode = 'ALLOW_INVALID_DATES'").unwrap();
    my.execute_sql(&format!("CREATE TABLE {zero} (id INT PRIMARY KEY, d DATE, dt DATETIME, t TIME, inv DATE)")).unwrap();
    my.execute_sql(&format!("INSERT INTO {zero} VALUES (1, '0000-00-00', '2024-00-15 00:00:00', '100:00:00', NULL), (2, '2024-01-15', '2024-01-15 00:00:00', '12:34:56', '2024-02-30')")).unwrap();
    let zero_columns = json!([col("id", "int", true), col("d", "date", false), col("dt", "datetime", false), col("t", "time", false), col("inv", "date", false)]);
    let refused = preflight_refusals(&mysql, &postgres, &zero, zero_columns.clone());
    for column in ["d", "dt", "t", "inv"] {
        if !refused.iter().any(|location| location == &format!("{zero}.{column}")) {
            failures.push(format!("MySQL {zero}.{column} with an unstorable value was not refused: {refused:?}"));
        }
    }
    // A cross-engine dump.import refuses the same values before the target changes.
    let dir = std::env::temp_dir().join(format!("tf-xtypes-zero-{suffix}"));
    if let Err(error) = run("dump.run", json!({"source": &mysql, "tables": [&zero], "output_dir": dir, "threads": 1, "data_format": "tsv", "compression": "none"})) {
        failures.push(format!("dump of zero dates: {error}"));
    } else {
        match run("dump.import", json!({"target": &postgres, "input_dir": dir, "mode": "replace", "threads": 1})) {
            Err(error) if error.contains("cannot store") => {
                if first_row(&postgres, &format!("SELECT to_regclass('{zero}') IS NOT NULL AS present"))["present"] == true {
                    failures.push("dump.import created the table before refusing zero dates".into());
                }
            }
            other => failures.push(format!("dump.import of zero dates into PostgreSQL was not refused: {other:?}")),
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
    // Keyless tables read through the text protocol: ordinary TIME and midnight DATETIME verify.
    let keyless = format!("tf_xtypes_keyless_{suffix}");
    my.execute_sql(&format!("CREATE TABLE {keyless} (d DATE, dt DATETIME(3), t TIME(3))")).unwrap();
    my.execute_sql(&format!("INSERT INTO {keyless} VALUES ('2024-01-15', '2024-01-15 00:00:00', '12:34:56.500'), ('2024-01-16', '2024-01-16 10:00:00.250', '00:00:00'), (NULL, NULL, '23:59:59')")).unwrap();
    failures.extend(migrate_and_verify(&mysql, &postgres, &keyless, json!([col("d", "date", false), col("dt", "datetime(3)", false), col("t", "time(3)", false)]), json!({})));

    let inf = format!("tf_xtypes_inf_{suffix}");
    pg.execute_sql(&format!("CREATE TABLE {inf} (id INT PRIMARY KEY, ts TIMESTAMPTZ, d DATE, tz TIMETZ, iv INTERVAL)")).unwrap();
    pg.execute_sql(&format!("INSERT INTO {inf} VALUES (1, 'infinity', '0044-03-15 BC', '12:00:00+09', '1 day'), (2, '2024-01-15 00:00:00+00', '2024-01-15', NULL, NULL)")).unwrap();
    let refused = preflight_refusals(&postgres, &mysql, &inf, json!([col("id", "integer", true), col("ts", "timestamp with time zone", false),
        col("d", "date", false), col("tz", "time with time zone", false), col("iv", "interval", false)]));
    for column in ["ts", "d", "tz"] {
        if !refused.iter().any(|location| location == &format!("{inf}.{column}")) {
            failures.push(format!("PostgreSQL {inf}.{column} was not refused: {refused:?}"));
        }
    }
    if refused.iter().any(|location| location == &format!("{inf}.iv")) {
        failures.push("PostgreSQL interval was refused instead of warned".into());
    }
    // dump.import refuses the timetz column type too, before the target changes.
    let dir = std::env::temp_dir().join(format!("tf-xtypes-inf-{suffix}"));
    if let Err(error) = run("dump.run", json!({"source": &postgres, "tables": [&inf], "output_dir": dir, "threads": 1, "data_format": "jsonl", "compression": "none"})) {
        failures.push(format!("dump of timetz table: {error}"));
    } else {
        match run("dump.import", json!({"target": &mysql, "input_dir": dir, "mode": "replace", "threads": 1})) {
            Err(error) if error.contains("time with time zone") => {}
            other => failures.push(format!("dump.import of timetz into MySQL was not refused: {other:?}")),
        }
    }
    let _ = std::fs::remove_dir_all(&dir);

    for (adapter, tables) in [(&mut my, [&child, &m, &p, &ai, &wide, &nan, &zero, &keyless, &inf]), (&mut pg, [&child, &m, &p, &ai, &wide, &nan, &zero, &keyless, &inf])] {
        for table in tables {
            let _ = adapter.execute_sql(&format!("DROP TABLE IF EXISTS {table}"));
        }
    }
    assert!(failures.is_empty(), "cross-engine type regressions:\n{}", failures.join("\n"));
}
