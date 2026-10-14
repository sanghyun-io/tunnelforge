//! `dump.import_preview`: what a replace import of this dump would do to the target. Read-only: it
//! writes no report or journal, creates no probe tables and never reads chunk data.
use crate::*;
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::Path;

pub(crate) fn handle<F: FnMut(Value)>(request: &Request, mut emit: F) {
    let result = request_endpoint(request)
        .and_then(|endpoint| preview(request, &endpoint).map_err(|err| redact_endpoint_secret(&err, &endpoint)));
    match result {
        Ok(result) => emit(result),
        Err(err) => emit(json!({"event": "error", "request_id": request.request_id, "message": err})),
    }
}

fn issue(code: &str, table: Option<&str>, detail: impl Into<String>) -> Value {
    json!({"code": code, "table": table, "detail": detail.into()})
}

fn warning(code: &str, detail: impl Into<String>) -> Value {
    json!({"code": code, "detail": detail.into()})
}

/// What the target namespace holds.
#[derive(Default)]
struct Target {
    /// MySQL `lower_case_table_names` folds names, so they compare without case.
    fold: bool,
    /// Base tables, each with whether it is a PostgreSQL partition.
    tables: Vec<(String, bool)>,
    /// `(name, DEFINER or owner)`.
    views: Vec<(String, String)>,
    /// `(table, trigger)`.
    triggers: Vec<(String, String)>,
}

fn preview(request: &Request, endpoint: &Endpoint) -> Result<Value, String> {
    let input_dir = request.payload.get("input_dir").and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty()).ok_or("dump.import_preview requires input_dir")?;
    let manifest = read_dump_manifest(Path::new(input_dir))?;
    let (data_format, compression) = super::supported_dump_format(&manifest)?;
    let engine = endpoint.engine.as_str();
    // A timezone the import would refuse is a bad request, not a property of the target.
    super::import_timezone_sql(&request.payload, manifest.source_timezone.as_deref(), engine)?;
    let selected: BTreeSet<String> = string_list(request.payload.get("tables")).into_iter().collect();

    let mut blockers = Vec::new();
    let mut warnings = super::coded_source_dump_warnings(&manifest).into_iter()
        .map(|(code, detail)| warning(code, detail)).collect::<Vec<_>>();
    if let Err(err) = super::check_mysql_spatial_import(&manifest, &selected, engine) {
        blockers.push(issue("spatial_unsupported", None, err));
    }
    match super::import_table_ddls(&manifest, &selected, engine) {
        Ok((_, untranslatable)) => blockers.extend(untranslatable.into_iter()
            .map(|(table, reason)| issue("untranslatable_table", Some(table.as_str()), reason))),
        Err(err) => blockers.push(issue("dump_invalid", None, err)),
    }
    let tables = manifest.tables.iter()
        .filter(|table| selected.is_empty() || selected.contains(&table.name)).cloned().collect::<Vec<_>>();
    if tables.is_empty() {
        blockers.push(issue("no_tables", None, "dump.import found no tables to import"));
    }
    let strict = request.payload.get("strict_manifest").and_then(Value::as_bool).unwrap_or(true);
    match validate_dump_import_manifest_strictness(&tables, strict, &data_format, &compression) {
        Ok(legacy) => warnings.extend(legacy.into_iter().map(|detail| warning("legacy_dump", detail))),
        Err(err) => blockers.push(issue("dump_invalid", None, err)),
    }
    for (table, problem) in super::cross_engine_temporal_refusals(&manifest, &tables, engine) {
        blockers.push(issue("temporal_unsupported", Some(table.as_str()), problem));
    }
    let names = tables.iter().map(|table| table.name.clone()).collect::<Vec<_>>();
    let import_schema = NormalizedSchema {
        tables: manifest.schema.tables.iter().filter(|table| names.contains(&table.name)).cloned().collect(),
    };
    // Only a whole same-engine import replaces views (finalize_dump_import).
    let dump_views = if selected.is_empty() && manifest.source_engine == engine {
        manifest.views.iter().map(|view| view.name.clone()).collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let mut copy_blockers = super::safe_restore::copy_refusals(&manifest, engine).into_iter()
        .map(|(code, detail)| issue(code, None, detail)).collect::<Vec<_>>();
    if !selected.is_empty() {
        copy_blockers.push(issue("partial_dump", None, "safe restore requires the complete dump; partial table filters are not supported"));
    }

    let schema = endpoint_schema(endpoint);
    let mut admin = super::safe_restore::maintenance_connection(endpoint)?;
    let exists = super::safe_restore::target_namespace_exists(&mut admin, endpoint)?;
    let can_create = if exists { None } else { can_create_namespace(&mut admin, endpoint)? };
    drop(admin);
    if !exists {
        let kind = if engine == "mysql" { "database" } else { "schema" };
        match can_create {
            Some(false) => blockers.push(issue("create_privilege_missing", None,
                format!("the {kind} {schema} does not exist and this account cannot create it"))),
            None => warnings.push(warning("create_privilege_unproven",
                format!("the {kind} {schema} does not exist; this account's grants do not show whether it can create it"))),
            Some(true) => {}
        }
    }
    let mut adapter = if exists { Some(LiveAdapter::connect(endpoint)?) } else { None };
    let target = match &mut adapter {
        Some(adapter) => inspect_target(adapter, &schema)?,
        None => Target::default(),
    };

    let fold = target.fold;
    let key = move |name: &str| if fold { name.to_lowercase() } else { name.to_string() };
    let existing = target.tables.iter().map(|(name, _)| key(name)).collect::<BTreeSet<_>>();
    let dumped = names.iter().map(|name| key(name)).collect::<BTreeSet<_>>();
    let (replace, create): (Vec<String>, Vec<String>) = names.iter().cloned().partition(|name| existing.contains(&key(name)));
    let replaced = replace.iter().map(|name| key(name)).collect::<BTreeSet<_>>();
    let untouched = target.tables.iter().filter(|(name, partition)| !partition && !dumped.contains(&key(name)))
        .map(|(name, _)| name.clone()).collect::<Vec<_>>();
    let triggers_dropped = target.triggers.iter().filter(|(table, _)| replaced.contains(&key(table)))
        .map(|(table, name)| json!({"table": table, "name": name})).collect::<Vec<_>>();
    let target_views = target.views.iter().map(|(name, _)| key(name)).collect::<BTreeSet<_>>();
    let (views_replace, views_create): (Vec<String>, Vec<String>) = dump_views.into_iter().partition(|name| target_views.contains(&key(name)));
    let replaced_views = views_replace.iter().map(|name| key(name)).collect::<BTreeSet<_>>();

    let mut external_parents = Vec::new();
    for table in &import_schema.tables {
        for fk in table.foreign_keys.iter().filter(|fk| !dumped.contains(&key(&fk.referenced_table))) {
            let exists_in_target = existing.contains(&key(&fk.referenced_table));
            // MySQL adds the FK with foreign_key_checks=0; PostgreSQL refuses it after the DROP.
            if !exists_in_target && engine == "postgresql" {
                blockers.push(issue("external_parent_missing", Some(table.name.as_str()),
                    format!("{}.{} references {}, which the target does not have", table.name, fk.name, fk.referenced_table)));
            }
            external_parents.push(json!({"child": table.name, "constraint": fk.name, "parent": fk.referenced_table, "exists_in_target": exists_in_target}));
        }
    }

    let mut kept_children = Vec::new();
    let mut may_not_replace = Vec::new();
    let mut may_break = Vec::new();
    if let Some(adapter) = &mut adapter {
        // The same checks the import runs before its first DROP, collected instead of raised.
        if let LiveAdapter::PostgreSql(client) = adapter {
            for (code, table, detail) in super::postgres_drop_dependents(client, &schema, &names)? {
                blockers.push(issue(code, Some(table.as_str()), detail));
            }
        }
        kept_children = super::surviving_foreign_keys(adapter, &schema, &import_schema)?.into_iter()
            .map(|fk| json!({"table": fk.child, "constraint": fk.constraint, "parent": fk.parent})).collect();
        if let Err(err) = validate_target_check_names(adapter, &schema, &import_schema) {
            blockers.push(issue("check_name_collision", None, err));
        }
        for (name, kind) in super::non_table_name_collisions(adapter, &schema, &import_schema)? {
            blockers.push(issue("name_collision", Some(name.as_str()), kind));
        }
        if let LiveAdapter::MySql(conn) = adapter {
            // TF-182: DROP VIEW fails with ERROR 1227 when the view's DEFINER has SYSTEM_USER and
            // this account does not. Which definers have it is not visible, so any other one may.
            if !replaced_views.is_empty() && mysql_grants_allow(conn, "SYSTEM_USER", None) != Some(true) {
                let current: String = conn.query_first("SELECT CURRENT_USER()")
                    .map_err(|err| format!("current account inspection failed: {err}"))?.unwrap_or_default();
                may_not_replace = target.views.iter()
                    .filter(|(name, definer)| replaced_views.contains(&key(name)) && *definer != current)
                    .map(|(name, definer)| json!({"name": name, "definer": definer})).collect();
            }
            let usage: Result<Vec<(String, String, String)>, _> = conn.exec(
                "SELECT VIEW_SCHEMA, VIEW_NAME, TABLE_NAME FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA=? ORDER BY 1, 2, 3",
                (&schema,),
            );
            match usage {
                Ok(rows) => for (view_schema, view, table) in rows {
                    let same_schema = view_schema == schema;
                    if replaced.contains(&key(&table)) && !(same_schema && replaced_views.contains(&key(&view))) {
                        let view = if same_schema { view } else { format!("{view_schema}.{view}") };
                        may_break.push(json!({"view": view, "table": table}));
                    }
                },
                Err(err) => warnings.push(warning("view_dependencies_unknown",
                    format!("views over replaced tables could not be listed (needs MySQL 8.0.13 or later): {err}"))),
            }
        }
    }

    Ok(json!({
        "event": "result", "request_id": request.request_id, "command": "dump.import_preview",
        "success": true, "dry_run": true,
        "target": {"engine": engine, "database": endpoint.database, "schema": schema, "exists": exists, "can_create": can_create},
        "replace": replace, "create": create, "untouched": untouched,
        "kept_children": kept_children, "external_parents": external_parents,
        "views": {"replace": views_replace, "create": views_create, "may_not_replace": may_not_replace, "may_break": may_break},
        "triggers_dropped": triggers_dropped, "blockers": blockers, "warnings": warnings,
        "strict_export": manifest.strict_export, "snapshot_policy": manifest.snapshot_policy,
        "source_timezone": manifest.source_timezone, "copy_blockers": copy_blockers,
    }))
}

fn inspect_target(adapter: &mut LiveAdapter, schema: &str) -> Result<Target, String> {
    let inspect_error = |err: &dyn std::fmt::Display| format!("target inspection failed: {err}");
    match adapter {
        LiveAdapter::MySql(conn) => Ok(Target {
            fold: mysql_folds_table_names(conn)?,
            tables: conn.exec::<String, _, _>(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_TYPE='BASE TABLE' ORDER BY 1", (schema,),
            ).map_err(|err| inspect_error(&err))?.into_iter().map(|name| (name, false)).collect(),
            views: conn.exec("SELECT TABLE_NAME, DEFINER FROM information_schema.VIEWS WHERE TABLE_SCHEMA=? ORDER BY 1", (schema,))
                .map_err(|err| inspect_error(&err))?,
            triggers: conn.exec("SELECT EVENT_OBJECT_TABLE, TRIGGER_NAME FROM information_schema.TRIGGERS WHERE EVENT_OBJECT_SCHEMA=? ORDER BY 1, 2", (schema,))
                .map_err(|err| inspect_error(&err))?,
        }),
        LiveAdapter::PostgreSql(client) => {
            let mut query = |sql: &str| client.query(sql, &[&schema]).map_err(|err| inspect_error(&err));
            Ok(Target {
                fold: false,
                tables: query("SELECT c.relname, c.relispartition FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relkind IN ('r','p') ORDER BY 1")?
                    .iter().map(|row| (row.get(0), row.get(1))).collect(),
                views: query("SELECT c.relname, pg_get_userbyid(c.relowner)::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relkind IN ('v','m') ORDER BY 1")?
                    .iter().map(|row| (row.get(0), row.get(1))).collect(),
                triggers: query("SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND NOT t.tgisinternal ORDER BY 1, 2")?
                    .iter().map(|row| (row.get(0), row.get(1))).collect(),
            })
        }
    }
}

/// None when the grants cannot say.
fn can_create_namespace(admin: &mut LiveAdapter, endpoint: &Endpoint) -> Result<Option<bool>, String> {
    match admin {
        LiveAdapter::MySql(conn) => Ok(mysql_grants_allow(conn, "CREATE", Some(endpoint.database.as_str()))),
        LiveAdapter::PostgreSql(client) => client.query_one("SELECT has_database_privilege(current_database(), 'CREATE')", &[])
            .map(|row| Some(row.get(0))).map_err(|err| format!("CREATE privilege inspection failed: {err}")),
    }
}

fn mysql_grants_allow(conn: &mut mysql::PooledConn, privilege: &str, database: Option<&str>) -> Option<bool> {
    let grants: Vec<String> = conn.query("SHOW GRANTS FOR CURRENT_USER").ok()?;
    grants_allow(&grants, privilege, database)
}

/// Whether SHOW GRANTS gives `privilege` on `*.*`, or on a database pattern matching `database`.
/// None when a partial revoke, or a role whose grants are not listed, could change the answer.
fn grants_allow(grants: &[String], privilege: &str, database: Option<&str>) -> Option<bool> {
    let (mut granted, mut roles) = (false, false);
    for grant in grants {
        let grant = grant.trim();
        let upper = grant.to_ascii_uppercase();
        if upper.starts_with("REVOKE ") {
            return None;
        }
        if !upper.starts_with("GRANT ") {
            continue;
        }
        let Some(on) = upper.find(" ON ") else {
            roles = true;
            continue;
        };
        let Some(to) = upper[on..].find(" TO ").map(|offset| on + offset) else { continue };
        let scope = grant[on + 4..to].trim();
        let applies = scope == "*.*" || database.is_some_and(|database| {
            scope.strip_suffix(".*").is_some_and(|pattern| grant_pattern_matches(pattern, database))
        });
        granted |= applies && upper[6..on].split(',').map(str::trim).any(|name| name == "ALL PRIVILEGES" || name == privilege);
    }
    if granted { Some(true) } else if roles { None } else { Some(false) }
}

/// A database grant pattern: backquoted, `%` and `_` are wildcards and `\` escapes.
fn grant_pattern_matches(pattern: &str, name: &str) -> bool {
    fn matches(pattern: &[char], name: &[char]) -> bool {
        match pattern {
            [] => name.is_empty(),
            ['%', rest @ ..] => (0..=name.len()).any(|skip| matches(rest, &name[skip..])),
            ['_', rest @ ..] => !name.is_empty() && matches(rest, &name[1..]),
            ['\\', literal, rest @ ..] | [literal, rest @ ..] => name.first() == Some(literal) && matches(rest, &name[1..]),
        }
    }
    let pattern = pattern.strip_prefix('`').and_then(|inner| inner.strip_suffix('`'))
        .map(|inner| inner.replace("``", "`")).unwrap_or_else(|| pattern.to_string());
    matches(&pattern.chars().collect::<Vec<_>>(), &name.chars().collect::<Vec<_>>())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[test]
    fn grants_decide_create_and_system_user_or_say_they_cannot() {
        let grants = |lines: &[&str]| lines.iter().map(|line| line.to_string()).collect::<Vec<_>>();
        let global = grants(&["GRANT SELECT, CREATE, DROP ON *.* TO `u`@`%`"]);
        assert_eq!(grants_allow(&global, "CREATE", Some("anything")), Some(true));
        assert_eq!(grants_allow(&global, "SYSTEM_USER", None), Some(false));
        let scoped = grants(&["GRANT USAGE ON *.* TO `u`@`%`", "GRANT ALL PRIVILEGES ON `app\\_%`.* TO `u`@`%`"]);
        assert_eq!(grants_allow(&scoped, "CREATE", Some("app_new")), Some(true));
        assert_eq!(grants_allow(&scoped, "CREATE", Some("appXnew")), Some(false), "an escaped _ is literal");
        assert_eq!(grants_allow(&scoped, "CREATE", Some("other")), Some(false));
        assert_eq!(grants_allow(&scoped, "SYSTEM_USER", None), Some(false), "a database grant never gives a global privilege");
        assert_eq!(grants_allow(&grants(&["GRANT CREATE VIEW, CREATE ROUTINE ON *.* TO `u`@`%`"]), "CREATE", Some("app")), Some(false));
        assert_eq!(grants_allow(&grants(&["GRANT APPLICATION_PASSWORD_ADMIN,SYSTEM_USER ON *.* TO `root`@`%`"]), "SYSTEM_USER", None), Some(true));
        assert_eq!(grants_allow(&grants(&["GRANT USAGE ON *.* TO `u`@`%`", "GRANT `dba`@`%` TO `u`@`%`"]), "CREATE", Some("app")), None);
        assert_eq!(grants_allow(&grants(&["GRANT CREATE ON *.* TO `u`@`%`", "REVOKE CREATE ON `app`.* FROM `u`@`%`"]), "CREATE", Some("app")), None);
        assert!(grant_pattern_matches("`a``b`", "a`b"));
        assert!(grant_pattern_matches("`tf_test`", "tf-test"), "an unescaped _ matches any character, as in MySQL");
    }

    #[test]
    fn manifest_warnings_get_stable_codes() {
        for (text, code) in [
            ("Workers used independent snapshots without a shared lock; cross-table and cross-chunk point-in-time consistency is not guaranteed.", "unsynchronized_snapshot"),
            ("View definitions were collected outside the table data snapshot; their consistency with that snapshot is not guaranteed.", "views_outside_snapshot"),
            ("Object not exported: trigger:orders:trg", "omitted_object"),
            ("Non-transactional tables excluded from export: logs", "omitted_object"),
            ("View definitions could not be collected: denied", "source_dump_warning"),
        ] {
            assert_eq!(super::super::manifest_warning_code(text), code, "{text}");
        }
    }

    #[test]
    fn preview_never_writes_a_report_or_reads_chunk_data() {
        let dir = std::env::temp_dir().join(format!("tunnelforge-preview-{}-{}", std::process::id(), current_unix_seconds()));
        std::fs::create_dir_all(&dir).unwrap();
        // The chunk is declared but absent: reading it would fail before the target is reached.
        let manifest = DumpManifest {
            format: "tunnelforge-dump".into(), format_version: 3, data_format: "tsv".into(), compression: "none".into(),
            source_engine: "mysql".into(), database: "app".into(), source_schema: Some("app".into()), source_timezone: Some("UTC".into()),
            schema: crate::adapters::test_support::schema(), snapshot_policy: "mysql_single_connection_consistent_snapshot".into(),
            strict_export: true, manifest_warnings: vec![], chunk_size: 100, created_unix_seconds: 1, views: vec![],
            tables: vec![DumpTableManifest {
                name: "users".into(), path: "users".into(), rows: 1, chunks: 1,
                chunk_sha256: BTreeMap::from([("chunk_000001.tsv".into(), "0".repeat(64))]),
            }],
        };
        write_dump_manifest(&dir, &manifest).unwrap();
        let payload = json!({"input_dir": dir.to_string_lossy(), "target": {"engine": "mysql", "host": "127.0.0.1", "port": 1, "user": "root", "password": "secret-value", "database": "app"}});
        let mut events = Vec::new();
        handle(&Request { command: "dump.import_preview".into(), request_id: Some("p-1".into()), payload }, |event| events.push(event));
        let report_written = dump_import_report_path(&dir).unwrap().exists();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(events.len(), 1, "{events:?}");
        assert_eq!(events[0]["event"], "error");
        assert_eq!(events[0]["request_id"], "p-1");
        let message = events[0]["message"].as_str().unwrap();
        assert!(!message.contains("chunk") && !message.contains("secret-value"), "{message}");
        assert!(!report_written, "a preview must not write an import report");
    }
}
