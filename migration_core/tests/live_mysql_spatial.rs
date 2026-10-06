//! MySQL spatial values must survive dump -> import exactly, SRID and SPATIAL index included, and
//! must never be silently converted across engines. The text protocol returns geometry as raw
//! SRID+WKB bytes, which dumps and migrations used to store as lossy text.
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

fn query(endpoint: &Endpoint, sql: &str) -> Vec<Value> {
    run("query.execute", json!({"connection": endpoint, "sql": sql})).unwrap()["rows"].as_array().cloned().unwrap_or_default()
}

/// HEX of the internal format covers SRID and coordinates bit for bit.
fn snapshot(endpoint: &Endpoint, table: &str) -> Vec<Value> {
    query(endpoint, &format!("SELECT id, HEX(g) AS g, HEX(p) AS p, HEX(poly) AS poly FROM {table} ORDER BY id"))
}

#[test]
fn mysql_spatial_values_round_trip_exactly_when_configured() {
    let (Some(mysql), Some(postgres)) = (endpoint("TF_MYSQL", "mysql", 3306), endpoint("TF_POSTGRES", "postgresql", 5432)) else {
        assert!(std::env::var_os("TF_LIVE_REQUIRED").is_none(), "TF_LIVE_REQUIRED is set but TF_MYSQL_* / TF_POSTGRES_* are missing");
        eprintln!("skipping MySQL spatial regression: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };
    let suffix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let table = format!("tf_geo_{suffix}");
    let plain = format!("tf_geo_plain_{suffix}");
    let mut db = LiveAdapter::connect(&mysql).unwrap();
    db.execute_sql(&format!("CREATE TABLE {table} (id INT PRIMARY KEY, g GEOMETRY NULL, p POINT NOT NULL SRID 4326, \
        poly POLYGON NULL, SPATIAL INDEX sp_p (p))")).unwrap();
    db.execute_sql(&format!("INSERT INTO {table} VALUES \
        (1, ST_GeomFromText('POINT(1 2)'), ST_GeomFromText('POINT(37.5665 126.978)', 4326), ST_GeomFromText('POLYGON((0 0,10 0,10 10,0 0))')), \
        (2, ST_GeomFromText('LINESTRING(0 0, 1.25 -3.5)', 3857), ST_GeomFromText('POINT(-33.8688 151.2093)', 4326), NULL), \
        (3, NULL, ST_GeomFromText('POINT(0 0)', 4326), NULL)")).unwrap();
    db.execute_sql(&format!("CREATE TABLE {plain} (id INT PRIMARY KEY, v VARCHAR(8))")).unwrap();
    db.execute_sql(&format!("INSERT INTO {plain} VALUES (1, 'a'), (2, 'b')")).unwrap();
    let expected = snapshot(&mysql, &table);
    let mut failures = Vec::new();

    for format in ["jsonl", "tsv"] {
        let dir = std::env::temp_dir().join(format!("tf-geo-{suffix}-{format}"));
        let dumped = run("dump.run", json!({"source": &mysql, "tables": [&table, &plain], "output_dir": dir, "threads": 1,
            "chunk_size": 2, "data_format": format, "compression": "none"}));
        if let Err(error) = dumped {
            failures.push(format!("dump {format}: {error}"));
            continue;
        }
        let manifest_path = dir.join("_tunnelforge_dump.json");
        let mut manifest: Value = serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
        if manifest["format_version"] != 5 {
            failures.push(format!("dump {format}: format_version {} (spatial dumps must be marked 5)", manifest["format_version"]));
        }
        match run("dump.import", json!({"target": &mysql, "input_dir": dir, "mode": "replace", "threads": 1})) {
            Ok(_) => {
                let restored = snapshot(&mysql, &table);
                if restored != expected {
                    failures.push(format!("import {format}: {restored:?} != {expected:?}"));
                }
                let create = query(&mysql, &format!("SHOW CREATE TABLE {table}"));
                let ddl = create[0]["Create Table"].as_str().unwrap_or_default().to_string();
                if !ddl.contains("SRID 4326") || !ddl.contains("SPATIAL KEY `sp_p`") {
                    failures.push(format!("import {format}: SRID or SPATIAL index lost: {ddl}"));
                }
            }
            Err(error) => failures.push(format!("import {format}: {error}")),
        }
        // A spatial dump never goes into PostgreSQL, and nothing there is touched.
        match run("dump.import", json!({"target": &postgres, "input_dir": dir, "mode": "replace", "threads": 1})) {
            Err(error) if error.contains("spatial") => {}
            other => failures.push(format!("cross-engine import {format} was not refused: {other:?}")),
        }
        // Dumps written before version 5 stored the bytes as lossy text: refused before any change.
        manifest["format_version"] = json!(3);
        std::fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        match run("dump.import", json!({"target": &mysql, "input_dir": dir, "mode": "replace", "threads": 1})) {
            Err(error) if error.contains("re-export") => {
                if snapshot(&mysql, &table) != expected {
                    failures.push(format!("legacy import {format} changed the table before refusing"));
                }
            }
            other => failures.push(format!("legacy spatial dump {format} was not refused: {other:?}")),
        }
        // Leaving the spatial table out of the selection keeps the rest of the dump usable.
        for target in [&mysql, &postgres] {
            if let Err(error) = run("dump.import", json!({"target": target, "input_dir": dir, "mode": "replace", "threads": 1, "tables": [&plain]})) {
                failures.push(format!("partial import {format} into {} without the spatial table: {error}", target.engine));
            }
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    // Cross-engine migration refuses spatial columns instead of copying mojibake.
    let payload = json!({"source_engine":"mysql","target_engine":"postgresql","source":&mysql,"target":&postgres,
        "schema":{"tables":[{"name": &table, "columns": [{"name":"id","type":"int","nullable":false,"primary_key":true},
            {"name":"g","type":"geometry","nullable":true},{"name":"p","type":"point srid 4326","nullable":false},
            {"name":"poly","type":"polygon","nullable":true}]}]},"execution_options":{"mode":"create_only","chunk_size":2}});
    match run("migrate", payload) {
        Ok(result) if result["success"] == true => failures.push(format!("migrate copied spatial columns: {result}")),
        Ok(result) if !result.to_string().contains("unsupported_spatial_type") => failures.push(format!("migrate refused for another reason: {result}")),
        Ok(_) => {}
        Err(error) if error.contains("spatial") => {}
        Err(error) => failures.push(format!("migrate failed for another reason: {error}")),
    }
    let exists = query(&postgres, &format!("SELECT to_regclass('{table}') IS NOT NULL AS present"));
    if exists[0]["present"] == true || exists[0]["present"] == "true" || exists[0]["present"] == "t" {
        failures.push("migrate created the PostgreSQL table despite the refusal".into());
    }

    // PostgreSQL built-in point/polygon move as text and verify reads that text, not geometry hex.
    let mut pg = LiveAdapter::connect(&postgres).unwrap();
    let shapes = format!("tf_geo_pg_{suffix}");
    pg.execute_sql(&format!("CREATE TABLE {shapes} (id INT PRIMARY KEY, pos POINT, area POLYGON)")).unwrap();
    pg.execute_sql(&format!("INSERT INTO {shapes} VALUES (1, '(1,2)', '((0,0),(1,1),(1,0))'), (2, NULL, NULL), (3, '(-1.5,2.25)', NULL)")).unwrap();
    let payload = json!({"source_engine":"postgresql","target_engine":"mysql","source":&postgres,"target":&mysql,
        "schema":{"tables":[{"name": &shapes, "columns": [{"name":"id","type":"integer","nullable":false,"primary_key":true},
            {"name":"pos","type":"point","nullable":true},{"name":"area","type":"polygon","nullable":true}]}]},
        "execution_options":{"mode":"create_only","chunk_size":2}});
    match run("migrate", payload.clone()) {
        Ok(result) if result["success"] == true => match run("verify", payload) {
            Ok(result) if result["success"] == true => {}
            other => failures.push(format!("verify PostgreSQL point -> MySQL: {other:?}")),
        },
        other => failures.push(format!("migrate PostgreSQL point -> MySQL: {other:?}")),
    }

    for name in [&table, &plain, &shapes] {
        let _ = pg.execute_sql(&format!("DROP TABLE IF EXISTS {name}"));
        let _ = db.execute_sql(&format!("DROP TABLE IF EXISTS {name}"));
    }
    assert!(failures.is_empty(), "MySQL spatial regressions:\n{}", failures.join("\n"));
}
