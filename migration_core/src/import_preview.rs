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

/// What the dump alone decides, before the target is read.
struct DumpFindings {
    /// The selected tables, in the order the import replaces them.
    tables: Vec<DumpTableManifest>,
    blockers: Vec<Value>,
    warnings: Vec<Value>,
    copy_blockers: Vec<Value>,
}

fn dump_findings(manifest: &DumpManifest, selected: &BTreeSet<String>, engine: &str, strict: bool) -> Result<DumpFindings, String> {
    let (data_format, compression) = super::supported_dump_format(manifest)?;
    let mut blockers = Vec::new();
    let mut warnings = super::coded_source_dump_warnings(manifest).into_iter()
        .map(|(code, detail)| warning(code, detail)).collect::<Vec<_>>();
    if let Err(err) = super::check_mysql_spatial_import(manifest, selected, engine) {
        blockers.push(issue("spatial_unsupported", None, err));
    }
    match super::import_table_ddls(manifest, selected, engine) {
        Ok((_, untranslatable)) => blockers.extend(untranslatable.into_iter()
            .map(|(table, reason)| issue("untranslatable_table", Some(table.as_str()), reason))),
        Err(err) => blockers.push(issue("dump_invalid", None, err)),
    }
    let tables = manifest.tables.iter()
        .filter(|table| selected.is_empty() || selected.contains(&table.name)).cloned().collect::<Vec<_>>();
    if tables.is_empty() {
        blockers.push(issue("no_tables", None, "dump.import found no tables to import"));
    }
    match validate_dump_import_manifest_strictness(&tables, strict, &data_format, &compression) {
        Ok(legacy) => warnings.extend(legacy.into_iter().map(|detail| warning("legacy_dump", detail))),
        Err(err) => blockers.push(issue("dump_invalid", None, err)),
    }
    for (table, problem) in super::cross_engine_temporal_refusals(manifest, &tables, engine) {
        blockers.push(issue("temporal_unsupported", Some(table.as_str()), problem));
    }
    let copy_blockers = super::safe_restore::copy_refusals(manifest, engine, !selected.is_empty()).into_iter()
        .map(|(code, table, detail)| issue(code, table.as_deref(), detail)).collect::<Vec<_>>();
    Ok(DumpFindings { tables: dependency_ordered_dump_tables(&manifest.schema, tables), blockers, warnings, copy_blockers })
}

fn preview(request: &Request, endpoint: &Endpoint) -> Result<Value, String> {
    let input_dir = request.payload.get("input_dir").and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty()).ok_or("dump.import_preview requires input_dir")?;
    let manifest = read_dump_manifest(Path::new(input_dir))?;
    let engine = endpoint.engine.as_str();
    let selected: BTreeSet<String> = string_list(request.payload.get("tables")).into_iter().collect();
    let strict = request.payload.get("strict_manifest").and_then(Value::as_bool).unwrap_or(true);
    let DumpFindings { tables, mut blockers, mut warnings, mut copy_blockers } = dump_findings(&manifest, &selected, engine, strict)?;
    // A timezone the import would refuse is a bad request, not a property of the target.
    super::import_timezone_sql(&request.payload, manifest.source_timezone.as_deref(), engine)?;
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

    let schema = endpoint_schema(endpoint);
    let mut admin = super::safe_restore::maintenance_connection(endpoint)?;
    let exists = super::safe_restore::target_namespace_exists(&mut admin, endpoint)?;
    let can_create = if exists { None } else { can_create_namespace(&mut admin, endpoint)? };
    // A cross-engine dump is already refused as a whole.
    if exists && manifest.source_engine == engine {
        // Only verify-in-copy needs these checks, so a failed one does not fail the preview.
        match super::safe_restore::target_copy_refusals(&mut admin, endpoint, &manifest) {
            Ok(refusals) => copy_blockers.extend(refusals.into_iter().map(|(code, table, detail)| issue(code, table.as_deref(), detail))),
            Err(error) => copy_blockers.push(issue("copy_checks_unavailable", None, error)),
        }
    }
    drop(admin);
    if !exists {
        // The replace import creates a missing namespace, so only the privilege matters.
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
            // DROP VIEW, like DROP TABLE, needs the view's or the schema's owner.
            may_not_replace = client.query(
                "SELECT c.relname, pg_get_userbyid(c.relowner)::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname=ANY($2::text[]) AND c.relkind IN ('v','m') AND NOT pg_has_role(c.relowner,'USAGE') AND NOT pg_has_role(n.nspowner,'USAGE') ORDER BY 1",
                &[&schema, &views_replace],
            ).map_err(|err| format!("view owner inspection failed: {err}"))?
                .iter().map(|row| json!({"name": row.get::<_, String>(0), "definer": row.get::<_, String>(1)})).collect();
        }
        kept_children = super::surviving_foreign_keys(adapter, &schema, &import_schema)?.into_iter()
            .map(|fk| json!({"table": fk.child, "constraint": fk.constraint, "parent": fk.parent})).collect();
        for (code, table, detail) in target_check_name_problems(adapter, &schema, &import_schema)? {
            blockers.push(issue(code, Some(table.as_str()), detail));
        }
        for (name, kind) in super::non_table_name_collisions(adapter, &schema, &import_schema)? {
            blockers.push(issue("name_collision", Some(name.as_str()), kind));
        }
        if let LiveAdapter::MySql(conn) = adapter {
            // The DDL probe drops tables of its own, which only a database-wide DROP grant covers.
            match mysql_grants_allow(conn, "DROP", Some(schema.as_str())) {
                Some(true) => {}
                Some(false) => blockers.push(issue("drop_privilege_missing", None,
                    format!("this account has no DROP privilege on the database {schema}"))),
                None => warnings.push(warning("drop_privilege_unproven",
                    format!("this account's grants do not show whether it can drop tables in {schema}"))),
            }
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

pub(crate) fn mysql_grants_allow(conn: &mut mysql::PooledConn, privilege: &str, database: Option<&str>) -> Option<bool> {
    let grants: Vec<String> = conn.query("SHOW GRANTS FOR CURRENT_USER").ok()?;
    // With partial_revokes on, `%` and `_` in database grants are plain characters. Servers
    // before 8.0.16 have no such variable and always treat them as wildcards.
    let wildcards = conn.query_first::<i64, _>("SELECT @@global.partial_revokes").ok().flatten() != Some(1);
    let folds = mysql_folds_table_names(conn).ok()?;
    let fold = |text: &str| if folds { text.to_lowercase() } else { text.to_string() };
    let grants = grants.iter().map(|grant| fold(grant)).collect::<Vec<_>>();
    grants_allow(&grants, privilege, database.map(fold).as_deref(), wildcards)
}

/// Whether SHOW GRANTS gives `privilege` on `*.*`, or on a database pattern matching `database`.
/// None when a partial revoke, or a role whose grants are not listed, could change the answer.
fn grants_allow(grants: &[String], privilege: &str, database: Option<&str>, wildcards: bool) -> Option<bool> {
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
            scope.strip_suffix(".*").is_some_and(|pattern| grant_pattern_matches(pattern, database, wildcards))
        });
        granted |= applies && upper[6..on].split(',').map(str::trim).any(|name| name == "ALL PRIVILEGES" || name == privilege);
    }
    if granted { Some(true) } else if roles { None } else { Some(false) }
}

/// A database grant pattern: backquoted, `\` escapes, and `%` and `_` are wildcards unless
/// partial_revokes makes them plain characters.
fn grant_pattern_matches(pattern: &str, name: &str, wildcards: bool) -> bool {
    fn matches(pattern: &[char], name: &[char], wildcards: bool) -> bool {
        match pattern {
            [] => name.is_empty(),
            ['%', rest @ ..] if wildcards => (0..=name.len()).any(|skip| matches(rest, &name[skip..], wildcards)),
            ['_', rest @ ..] if wildcards => !name.is_empty() && matches(rest, &name[1..], wildcards),
            ['\\', literal, rest @ ..] | [literal, rest @ ..] => name.first() == Some(literal) && matches(rest, &name[1..], wildcards),
        }
    }
    let pattern = pattern.strip_prefix('`').and_then(|inner| inner.strip_suffix('`'))
        .map(|inner| inner.replace("``", "`")).unwrap_or_else(|| pattern.to_string());
    matches(&pattern.chars().collect::<Vec<_>>(), &name.chars().collect::<Vec<_>>(), wildcards)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[test]
    fn grants_decide_create_and_system_user_or_say_they_cannot() {
        let grants = |lines: &[&str]| lines.iter().map(|line| line.to_string()).collect::<Vec<_>>();
        let global = grants(&["GRANT SELECT, CREATE, DROP ON *.* TO `u`@`%`"]);
        assert_eq!(grants_allow(&global, "CREATE", Some("anything"), true), Some(true));
        assert_eq!(grants_allow(&global, "SYSTEM_USER", None, true), Some(false));
        let scoped = grants(&["GRANT USAGE ON *.* TO `u`@`%`", "GRANT ALL PRIVILEGES ON `app\\_%`.* TO `u`@`%`"]);
        assert_eq!(grants_allow(&scoped, "CREATE", Some("app_new"), true), Some(true));
        assert_eq!(grants_allow(&scoped, "CREATE", Some("appXnew"), true), Some(false), "an escaped _ is literal");
        assert_eq!(grants_allow(&scoped, "CREATE", Some("other"), true), Some(false));
        assert_eq!(grants_allow(&scoped, "SYSTEM_USER", None, true), Some(false), "a database grant never gives a global privilege");
        assert_eq!(grants_allow(&grants(&["GRANT CREATE VIEW, CREATE ROUTINE ON *.* TO `u`@`%`"]), "CREATE", Some("app"), true), Some(false));
        assert_eq!(grants_allow(&grants(&["GRANT APPLICATION_PASSWORD_ADMIN,SYSTEM_USER ON *.* TO `root`@`%`"]), "SYSTEM_USER", None, true), Some(true));
        assert_eq!(grants_allow(&grants(&["GRANT USAGE ON *.* TO `u`@`%`", "GRANT `dba`@`%` TO `u`@`%`"]), "CREATE", Some("app"), true), None);
        assert_eq!(grants_allow(&grants(&["GRANT CREATE ON *.* TO `u`@`%`", "REVOKE CREATE ON `app`.* FROM `u`@`%`"]), "CREATE", Some("app"), true), None);
        assert_eq!(grants_allow(&grants(&["GRANT DROP ON `app`.`orders` TO `u`@`%`"]), "DROP", Some("app"), true), Some(false), "a table grant is not database-wide");
        assert!(grant_pattern_matches("`a``b`", "a`b", true));
        assert!(grant_pattern_matches("`tf_test`", "tf-test", true), "an unescaped _ matches any character, as in MySQL");
    }

    #[test]
    fn partial_revokes_make_grant_wildcards_plain_characters() {
        let grants = vec!["GRANT ALL PRIVILEGES ON `app_%`.* TO `u`@`%`".to_string()];
        assert_eq!(grants_allow(&grants, "CREATE", Some("app_new"), true), Some(true));
        assert_eq!(grants_allow(&grants, "CREATE", Some("app_new"), false), Some(false));
        assert_eq!(grants_allow(&grants, "CREATE", Some("app_%"), false), Some(true));
        assert!(grant_pattern_matches("`app\\_%`", "app_%", false), "an escape still names the character itself");
        assert!(!grant_pattern_matches("`tf_test`", "tf-test", false));
    }

    #[test]
    fn manifest_warnings_get_stable_codes() {
        let (_, _, unsynchronized) = dump_manifest_consistency_metadata("mysql", "mysql_parallel_unsynchronized_snapshots");
        let mut cases = unsynchronized.into_iter().map(|text| (text, "unsynchronized_snapshot")).collect::<Vec<_>>();
        cases.extend([
            // Written by exports before the shared-lock wording.
            (format!("{WARNING_UNSYNCHRONIZED_SNAPSHOTS}; cross-table and cross-chunk consistency is not guaranteed."), "unsynchronized_snapshot"),
            (format!("{WARNING_VIEWS_OUTSIDE_SNAPSHOT}; their consistency with that snapshot is not guaranteed."), "views_outside_snapshot"),
            (format!("{WARNING_OBJECT_NOT_EXPORTED} trigger:orders:trg"), "omitted_object"),
            (format!("{WARNING_NON_TRANSACTIONAL_EXCLUDED} logs"), "omitted_object"),
            (format!("{WARNING_VISIBILITY_UNPROVEN} triggers (TRIGGER)"), "visibility_unproven"),
            ("View definitions could not be collected: denied".to_string(), "source_dump_warning"),
        ]);
        assert_eq!(cases.len(), 7);
        for (text, code) in cases {
            assert_eq!(super::super::manifest_warning_code(&text), code, "{text}");
        }
    }

    fn users() -> NormalizedTable {
        crate::adapters::test_support::schema().tables.remove(0)
    }

    fn with_column(mut table: NormalizedTable, name: &str, type_name: &str) -> NormalizedTable {
        let mut column = table.columns[1].clone();
        column.name = name.into();
        column.type_name = type_name.into();
        table.columns.push(column);
        table
    }

    fn manifest(source_engine: &str, format_version: u32, tables: Vec<NormalizedTable>) -> DumpManifest {
        DumpManifest {
            format: "tunnelforge-dump".into(), format_version, data_format: "jsonl".into(), compression: "none".into(),
            source_engine: source_engine.into(), database: "app".into(), source_schema: Some("app".into()), source_timezone: Some("UTC".into()),
            snapshot_policy: "mysql_single_connection_consistent_snapshot".into(), strict_export: true, manifest_warnings: vec![],
            chunk_size: 100, created_unix_seconds: 1, views: vec![],
            tables: tables.iter().map(|table| DumpTableManifest {
                name: table.name.clone(), path: table.name.clone(), rows: 0, chunks: 0, chunk_sha256: BTreeMap::new(),
            }).collect(),
            schema: NormalizedSchema { tables },
        }
    }

    fn codes(items: &[Value]) -> Vec<&str> {
        items.iter().map(|item| item["code"].as_str().unwrap()).collect()
    }

    fn findings(manifest: &DumpManifest, selected: &[&str], engine: &str, strict: bool) -> DumpFindings {
        let selected = selected.iter().map(|name| name.to_string()).collect();
        dump_findings(manifest, &selected, engine, strict).unwrap()
    }

    #[test]
    fn a_clean_same_engine_dump_has_no_findings() {
        let found = findings(&manifest("mysql", 5, vec![users()]), &[], "mysql", true);
        assert_eq!(found.blockers, Vec::<Value>::new());
        assert_eq!(found.warnings, Vec::<Value>::new());
        assert_eq!(found.copy_blockers, Vec::<Value>::new());
    }

    #[test]
    fn cross_engine_dumps_report_what_the_other_engine_cannot_take() {
        let found = findings(&manifest("postgresql", 5, vec![with_column(users(), "starts", "time with time zone")]), &[], "mysql", true);
        let temporal = found.blockers.iter().find(|item| item["code"] == "temporal_unsupported").unwrap_or_else(|| panic!("{:?}", found.blockers));
        assert_eq!(temporal["table"], "users");
        assert_eq!(codes(&found.copy_blockers), ["cross_engine"]);

        let mut on_update = with_column(users(), "touched", "timestamp");
        on_update.columns[2].on_update = Some("CURRENT_TIMESTAMP".into());
        on_update.name = "audit".into();
        let found = findings(&manifest("mysql", 5, vec![with_column(users(), "spot", "point"), on_update]), &[], "postgresql", true);
        assert!(codes(&found.blockers).contains(&"spatial_unsupported"), "{:?}", found.blockers);
        assert!(found.blockers.iter().any(|item| item["code"] == "untranslatable_table" && item["table"] == "audit"), "{:?}", found.blockers);
        assert_eq!(codes(&found.copy_blockers), ["cross_engine"]);
    }

    #[test]
    fn legacy_dumps_report_their_limits() {
        let mut legacy = manifest("mysql", 3, vec![with_column(users(), "flags", "bit(8)")]);
        legacy.source_timezone = None;
        legacy.strict_export = false;
        legacy.snapshot_policy = "mysql_parallel_unsynchronized_snapshots".into();
        legacy.tables[0].rows = 1;
        legacy.tables[0].chunks = 1;
        let found = findings(&legacy, &[], "mysql", false);
        assert_eq!(found.blockers, Vec::<Value>::new());
        assert_eq!(codes(&found.warnings), ["unsynchronized_snapshot", "not_strict", "legacy_timezone", "legacy_dump"]);
        assert_eq!(codes(&found.copy_blockers), ["legacy_bit_unverifiable"]);
        // A strict import refuses the missing chunk digests outright.
        assert_eq!(codes(&findings(&legacy, &[], "mysql", true).blockers), ["dump_invalid"]);
    }

    #[test]
    fn table_filters_and_empty_selections_are_reported() {
        let mut parent = users();
        parent.name = "parent".into();
        let mut child = users();
        child.name = "child".into();
        child.foreign_keys.push(crate::adapters::test_support::fk("fk_child_parent", "parent"));
        let dump = manifest("mysql", 5, vec![child, parent]);
        let whole = findings(&dump, &[], "mysql", true);
        assert_eq!(whole.tables.iter().map(|table| table.name.as_str()).collect::<Vec<_>>(), ["parent", "child"], "the order the import replaces them");
        let filtered = findings(&dump, &["child"], "mysql", true);
        assert_eq!(codes(&filtered.copy_blockers), ["partial_table_filter"]);
        assert_eq!(filtered.tables.len(), 1);
        assert_eq!(codes(&findings(&dump, &["ghost"], "mysql", true).blockers), ["dump_invalid", "no_tables"]);
        assert_eq!(codes(&findings(&manifest("mysql", 5, vec![]), &[], "mysql", true).blockers), ["no_tables"]);
    }

    #[test]
    fn preview_never_writes_a_report_or_reads_chunk_data() {
        let dir = std::env::temp_dir().join(format!("tunnelforge-preview-{}-{}", std::process::id(), current_unix_seconds()));
        std::fs::create_dir_all(&dir).unwrap();
        // The chunk is declared but absent: reading it would fail before the target is reached.
        let mut manifest = manifest("mysql", 3, vec![users()]);
        manifest.data_format = "tsv".into();
        manifest.tables[0].rows = 1;
        manifest.tables[0].chunks = 1;
        manifest.tables[0].chunk_sha256 = BTreeMap::from([("chunk_000001.tsv".into(), "0".repeat(64))]);
        write_dump_manifest(&dir, &manifest).unwrap();
        let payload = json!({"input_dir": dir.to_string_lossy(), "target": {"engine": "mysql", "host": "127.0.0.1", "port": 1, "user": "root", "password": "secret-value", "database": "app"}});
        let mut events = Vec::new();
        handle(&Request { command: "dump.import_preview".into(), request_id: Some("p-1".into()), payload }, |event| events.push(event));
        let report_written = !dump_import_report_paths(&dir).is_empty();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(events.len(), 1, "{events:?}");
        assert_eq!(events[0]["event"], "error");
        assert_eq!(events[0]["request_id"], "p-1");
        let message = events[0]["message"].as_str().unwrap();
        assert!(!message.contains("chunk"), "{message}");
        assert!(!report_written, "a preview must not write an import report");
    }
}
