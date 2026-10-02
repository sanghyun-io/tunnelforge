//! Prepare and verify an isolated candidate. This module never promotes or drops
//! the original namespace, and retains owned candidates after every failure.
use crate::*;
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

fn public_target(endpoint: &Endpoint) -> Value {
    json!({"engine":endpoint.engine,"host":endpoint.host,"port":endpoint.port,
        "database":endpoint.database,"schema":endpoint_schema(endpoint)})
}

pub(crate) struct VerifiedSafeRestorePlan {
    pub restore_id: String,
    pub plan_digest: String,
    pub original: Endpoint,
    pub candidate: Endpoint,
    pub report: Value,
    pub input_path: std::path::PathBuf,
}

fn plan_digest(report: &Value) -> String {
    let bound = json!({"restore_id":report["restore_id"],"original_target":report["original_target"],
        "candidate_target":report["candidate_target"],"namespace_existed":report["namespace_existed"],
        "dump_manifest_sha256":report["dump_manifest_sha256"],"effective_timezone_sql":report["effective_timezone_sql"],"verification":report["verification"]});
    format!("{:x}", Sha256::digest(bound.to_string().as_bytes()))
}

/// Local reports are evidence, not authorization. Promotion must independently
/// bind server identity/state and require confirmation of its own current plan.
pub(crate) fn load_verified_plan(
    report_path: &Path,
    credentials: &Endpoint,
) -> Result<VerifiedSafeRestorePlan, String> {
    let report: Value =
        serde_json::from_slice(&std::fs::read(report_path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
    if report["mode"] != "safe"
        || report["verified"] != true
        || report["success"] != true
        || report["candidate_created"] != true
        || !matches!(
            report["status"].as_str(),
            Some("ready_for_switch" | "ready_for_review" | "completed_new_target")
        )
    {
        return Err("safe restore report is not a verified preparation plan".into());
    }
    if report["verification"]["proof_version"] != 2
        || !report["verification"]["actual_schema_sha256"].is_string()
        || !report["verification"]["unsupported_inventory_sha256"].is_string()
        || !report["verification"]["view_definition_sha256"].is_object()
    {
        return Err("safe restore report lacks complete schema/view proof; prepare the candidate again with the current version".into());
    }
    if report["original_target"] != public_target(credentials) {
        return Err("safe restore plan does not match the supplied original endpoint".into());
    }
    let saved_digest = report["plan_digest"]
        .as_str()
        .ok_or("safe restore plan digest missing")?;
    if saved_digest != plan_digest(&report) {
        return Err("safe restore plan digest mismatch".into());
    }
    let mut candidate = credentials.clone();
    let target = &report["candidate_target"];
    if target["engine"] != credentials.engine
        || target["host"] != credentials.host
        || target["port"] != credentials.port
    {
        return Err("safe restore candidate belongs to a different server endpoint".into());
    }
    candidate.database = target["database"]
        .as_str()
        .ok_or("candidate database missing")?
        .into();
    candidate.schema = target["schema"].as_str().map(str::to_string);
    if candidate.engine == "mysql"
        && candidate.schema.as_deref() != Some(candidate.database.as_str())
    {
        return Err("candidate MySQL database/schema identity mismatch".into());
    }
    if candidate.engine == "postgresql" && candidate.database != credentials.database {
        return Err("candidate PostgreSQL database changed".into());
    }
    let restore_id = report["restore_id"]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or("restore id missing")?
        .to_string();
    let existed = report["namespace_existed"]
        .as_bool()
        .ok_or("namespace existence evidence missing")?;
    if report["cutover_pending"] != existed
        || report["namespace_created"] != true
        || report["original_unchanged"] != true
    {
        return Err("safe restore namespace ownership evidence is inconsistent".into());
    }
    if existed
        && (endpoint_schema(&candidate) != format!("tf_restore_{restore_id}")
            || public_target(&candidate) == public_target(credentials))
    {
        return Err("candidate namespace does not match its owned restore identifier".into());
    }
    if !existed && public_target(&candidate) != public_target(credentials) {
        return Err("new destination identity changed".into());
    }
    let input_path = report_path
        .parent()
        .ok_or("safe restore report directory missing")?
        .to_path_buf();
    if report["dump_manifest_sha256"] != sha256_file(&input_path.join("_tunnelforge_dump.json"))? {
        return Err("dump manifest changed after safe preparation".into());
    }
    Ok(VerifiedSafeRestorePlan {
        restore_id,
        plan_digest: saved_digest.into(),
        original: credentials.clone(),
        candidate,
        report,
        input_path,
    })
}

pub(crate) fn reverify_candidate(plan: &VerifiedSafeRestorePlan) -> Result<Value, String> {
    if plan.report["dump_manifest_sha256"]
        != sha256_file(&plan.input_path.join("_tunnelforge_dump.json"))?
    {
        return Err("dump manifest changed after safe preparation".into());
    }
    let manifest = read_dump_manifest(&plan.input_path)?;
    let timezone = super::validated_timezone_sql(plan.report["effective_timezone_sql"].as_str())?;
    let verified = verify_candidate(
        &plan.candidate,
        &manifest,
        &plan.input_path,
        timezone.as_deref(),
        &mut |_, _, _| {},
    )?;
    if verified != plan.report["verification"] {
        return Err("candidate verification changed since safe preparation".into());
    }
    Ok(verified)
}

pub(super) fn run<F: FnMut(Value)>(request: &Request, mut emit: F) -> Result<Value, String> {
    let mut original = request_endpoint(request)?;
    if original.engine == "mysql" {
        original.schema = Some(original.database.clone());
    }
    let input = request
        .payload
        .get("input_dir")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or("dump.import requires input_dir")?;
    let path = Path::new(input);
    let report_path = dump_import_report_path(path)?.display().to_string();
    let mut state = json!({"success":false,"mode":"safe","status":"staging","phase":"safe_restore_validation",
        "original_target":public_target(&original),"candidate_target":null,
        "original_unchanged":true,"cutover_pending":true,"ready_for_switch":false,
        "verified":false,"candidate_created":false,"candidate_retained":false,
        "automatic_cutover":false,"report_path":report_path,
        "interrupted_attempt_policy":"Original namespace remains unchanged. Inspect the recorded candidate; no automatic cleanup or promotion is performed."});
    if let Err(error) = write_dump_import_report(path, &state) {
        state["success"] = json!(false);
        state["status"] = json!("failed_original_untouched");
        state["ready_for_switch"] = json!(false);
        state["report_write_failed"] = json!(true);
        state["message"] = json!(format!(
            "Safe restore did not start because its report could not be created: {error}"
        ));
        let mut event = state.clone();
        event["event"] = json!("safe_restore_failed");
        event["request_id"] = json!(request.request_id);
        emit(event);
        state["event"] = json!("result");
        state["request_id"] = json!(request.request_id);
        state["command"] = json!("dump.import");
        return Ok(state);
    }
    let outcome = (|| -> Result<Value, String> {
        if !string_list(request.payload.get("tables")).is_empty() {
            return Err(
                "safe restore requires the complete dump; partial table filters are not supported"
                    .into(),
            );
        }
        let manifest = read_dump_manifest(path)?;
        state["dump_manifest_sha256"] = json!(sha256_file(&path.join("_tunnelforge_dump.json"))?);
        if manifest.format != "tunnelforge-dump" || !matches!(manifest.format_version, 1 | 2 | 3 | 4) {
            return Err("unsupported dump manifest format".into());
        }
        if manifest.format_version < 4 && manifest.source_engine == "mysql"
            && manifest.schema.tables.iter().any(|table| table.columns.iter().any(|column| mysql_bit_width(&column.type_name).is_some()))
        {
            return Err("safe restore cannot verify BIT columns from a dump written before format version 4 (their values were stored as raw bytes); re-export the database with this version, or use the advanced replace import".into());
        }
        if manifest.source_engine != original.engine {
            return Err("safe restore currently verifies same-engine dumps only; use the explicit cross-engine migration workflow".into());
        }
        if manifest.tables.is_empty() {
            return Err("safe restore found no tables".into());
        }
        let format = manifest.data_format.to_ascii_lowercase();
        let compression = manifest.compression.to_ascii_lowercase();
        if !matches!(format.as_str(), "jsonl" | "tsv")
            || !matches!(compression.as_str(), "none" | "zstd")
        {
            return Err("unsupported dump format or compression".into());
        }
        super::validate_import_metadata(&manifest, &BTreeSet::new(), &original.engine)?;
        let timezone = super::import_timezone_sql(
            &request.payload,
            manifest.source_timezone.as_deref(),
            &original.engine,
        )?;
        state["effective_timezone_sql"] = json!(timezone);
        let strict = request
            .payload
            .get("strict_manifest")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        validate_dump_import_manifest_strictness(&manifest.tables, strict, &format, &compression)?;
        emit(
            json!({"event":"phase","request_id":request.request_id,"phase":"safe_restore_validation",
            "message":"안전 복원: 원본을 유지한 채 덤프 무결성을 검증합니다."}),
        );
        validate_dump_manifest_chunks(
            path,
            &manifest.tables,
            &format,
            &compression,
            &manifest.schema,
        )?;

        let mut maintenance = original.clone();
        if original.engine == "mysql" {
            maintenance.database = "information_schema".into();
            maintenance.schema = None;
        }
        let mut admin = LiveAdapter::connect(&maintenance)?;
        let namespace_exists = match &mut admin {
            LiveAdapter::MySql(conn) => {
                conn.exec_first::<u64, _, _>(
                    "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=?",
                    (&original.database,),
                )
                .map_err(|e| e.to_string())?
                .unwrap_or(0)
                    > 0
            }
            LiveAdapter::PostgreSql(client) => client
                .query_one(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)",
                    &[&endpoint_schema(&original)],
                )
                .map_err(|e| e.to_string())?
                .get::<_, bool>(0),
        };
        state["namespace_existed"] = json!(namespace_exists);
        state["cutover_pending"] = json!(namespace_exists);
        let original_inspection = if namespace_exists {
            inspect_live(&original)?
        } else {
            InspectionResult::default()
        };
        let original_views = if namespace_exists {
            collect_views(&original)?
        } else {
            Vec::new()
        };
        let declared = manifest
            .tables
            .iter()
            .map(|t| t.name.as_str())
            .collect::<BTreeSet<_>>();
        let declared_views = manifest
            .views
            .iter()
            .map(|v| v.name.as_str())
            .collect::<BTreeSet<_>>();
        let target_only = original_inspection
            .schema
            .tables
            .iter()
            .filter(|t| !declared.contains(t.name.as_str()))
            .map(|t| t.name.clone())
            .collect::<Vec<_>>();
        let target_views = original_views
            .iter()
            .filter(|v| !declared_views.contains(v.name.as_str()))
            .map(|v| v.name.clone())
            .collect::<Vec<_>>();
        let incoming=original_inspection.schema.tables.iter().filter(|t|!declared.contains(t.name.as_str()))
            .flat_map(|table|table.foreign_keys.iter().filter(|fk|declared.contains(fk.referenced_table.as_str()))
                .map(move|fk|json!({"table":table.name,"constraint":fk.name,"referenced_table":fk.referenced_table}))).collect::<Vec<_>>();
        let mut blockers = Vec::new();
        if !target_only.is_empty() {
            blockers.push("The original namespace contains tables absent from this dump; preserve them before switching.".to_string());
        }
        if !target_views.is_empty() {
            blockers.push("The original namespace contains views absent from this dump; review their dependencies before switching.".to_string());
        }
        let unrepresented = original_inspection
            .unsupported_objects
            .iter()
            .filter(|object| !object.to_ascii_lowercase().starts_with("view:"))
            .cloned()
            .collect::<Vec<_>>();
        if !unrepresented.is_empty() {
            blockers.push("Original database objects outside the declared dump model require a preservation review before switching.".into());
        }
        state["target_only_tables"] = json!(target_only);
        state["target_only_views"] = json!(target_views);
        state["incoming_foreign_keys"] = json!(incoming);
        state["unrepresented_original_objects"] = json!(unrepresented);
        state["blockers"] = json!(blockers);

        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_nanos();
        let restore_id = format!("{nonce}_{}", std::process::id());
        state["restore_id"] = json!(restore_id);
        let name = if namespace_exists {
            format!("tf_restore_{restore_id}")
        } else {
            endpoint_schema(&original)
        };
        let mut candidate = original.clone();
        if original.engine == "mysql" {
            candidate.database = name.clone();
            candidate.schema = Some(name.clone());
        } else {
            candidate.schema = Some(name.clone());
        }
        state["candidate_target"] = public_target(&candidate);
        state["phase"] = json!("safe_restore_create_candidate");
        state["candidate_creation_outcome_unknown"] = json!(true);
        write_dump_import_report(path, &state)?;
        create_candidate(&mut admin, &original, &name)?;
        state["namespace_created"] = json!(true);
        state["candidate_created"] = json!(true);
        state["candidate_retained"] = json!(true);
        state["candidate_creation_outcome_unknown"] = json!(false);
        state["phase"] = json!("safe_restore_import_candidate");
        write_dump_import_report(path, &state)?;
        let mut target_event = state.clone();
        target_event["event"] = json!("safe_restore_target");
        target_event["request_id"] = json!(request.request_id);
        emit(target_event);

        let mut payload = request.payload.clone();
        let fields = payload.as_object_mut().ok_or("invalid import payload")?;
        // request_endpoint accepts several aliases in priority order. Remove ALL
        // originals so none can redirect candidate import back to the live DB.
        for key in ["connection", "endpoint", "source", "target"] {
            fields.remove(key);
        }
        fields.insert(
            "target".into(),
            serde_json::to_value(&candidate).map_err(|e| e.to_string())?,
        );
        fields.insert("mode".into(), json!("replace"));
        fields.remove("import_mode");
        fields.remove("tables");
        let context = json!({"original_target":public_target(&original),"candidate_target":public_target(&candidate),
            "original_unchanged":true,"cutover_pending":namespace_exists,"candidate_created":true,"candidate_retained":true,
            "namespace_existed":namespace_exists,"namespace_created":true,"restore_id":restore_id,
            "automatic_cutover":false,"interrupted_attempt_policy":state["interrupted_attempt_policy"]});
        let imported = super::dump_import_with_context(
            &Request {
                command: "dump.import".into(),
                request_id: request.request_id.clone(),
                payload,
            },
            Some(context),
            |event| emit(event),
        )?;
        if let Ok(bytes) = std::fs::read(dump_import_report_path(path)?) {
            if let Ok(report) = serde_json::from_slice::<Value>(&bytes) {
                state["candidate_journal"] = report;
            }
        }
        state["candidate_import"] = imported.clone();
        if imported.get("success").and_then(Value::as_bool) != Some(true) {
            return Err(imported
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("Candidate import did not complete")
                .into());
        }
        state["phase"] = json!("safe_restore_verify_candidate");
        write_dump_import_report(path, &state)?;
        emit(
            json!({"event":"phase","request_id":request.request_id,"phase":state["phase"],
            "message":"후보 데이터베이스의 실제 행 수, 테이블 구조, 인덱스와 외래 키를 검증합니다. 원본은 그대로 유지됩니다."}),
        );
        let verification = verify_candidate(
            &candidate,
            &manifest,
            path,
            timezone.as_deref(),
            &mut |table, current, total| {
                emit(
                    json!({"event":"phase","request_id":request.request_id,"phase":"safe_restore_verify_table",
                "table":table,"current":current,"total":total,
                "message":format!("후보 테이블 데이터 무결성 검증 중 ({current}/{total})")}),
                );
            },
        )?;
        state["verification"] = verification;
        state["verified"] = json!(true);
        state["success"] = json!(true);
        state["ready_for_switch"] = json!(namespace_exists && blockers.is_empty());
        state["status"] = json!(if !namespace_exists {
            "completed_new_target"
        } else if blockers.is_empty() {
            "ready_for_switch"
        } else {
            "ready_for_review"
        });
        state["phase"] = json!("safe_restore_verified");
        state["message"] = json!(if !namespace_exists {
            "Requested new namespace restored and verified. No existing namespace was replaced."
        } else if blockers.is_empty() {
            "Candidate verified. Original database remains unchanged; application connection switch is pending."
        } else {
            "Candidate verified, but original-only objects require review before switching. Original database remains unchanged."
        });
        state["rows_imported"] = imported["rows_imported"].clone();
        state["tables"] = imported["tables"].clone();
        state["plan_digest"] = json!(plan_digest(&state));
        Ok(state.clone())
    })();
    if let Err(error) = outcome {
        // Preserve the inner journal's mutation evidence and failure phase.
        if state["candidate_created"] == true && state.get("candidate_journal").is_none() {
            if let Ok(bytes) = std::fs::read(dump_import_report_path(path)?) {
                if let Ok(report) = serde_json::from_slice::<Value>(&bytes) {
                    state["candidate_journal"] = report;
                }
            }
        }
        state["success"] = json!(false);
        state["status"] = json!("failed_original_untouched");
        state["message"] = json!(format!(
            "Safe restore failed; original database was not changed. {}",
            redact_endpoint_secret(&error, &original)
        ));
        state["error"] = json!(redact_endpoint_secret(&error, &original));
    }
    if let Err(error) = write_dump_import_report(path, &state) {
        let original_error = state["error"]
            .as_str()
            .unwrap_or("Candidate preparation finished");
        let message = format!("{original_error}; final safe restore report could not be persisted: {error}. Original namespace was not changed.");
        state["success"] = json!(false);
        state["status"] = json!("failed_original_untouched");
        state["ready_for_switch"] = json!(false);
        state["report_write_failed"] = json!(true);
        state["message"] = json!(redact_endpoint_secret(&message, &original));
    }
    let mut event = state.clone();
    event["request_id"] = json!(request.request_id);
    event["event"] = json!(if state["success"] == true {
        "safe_restore_ready"
    } else {
        "safe_restore_failed"
    });
    emit(event);
    state["event"] = json!("result");
    state["request_id"] = json!(request.request_id);
    state["command"] = json!("dump.import");
    Ok(state)
}

fn create_candidate(
    adapter: &mut LiveAdapter,
    original: &Endpoint,
    name: &str,
) -> Result<(), String> {
    if let LiveAdapter::MySql(conn) = adapter {
        let defaults:Option<(String,String)>=conn.exec_first("SELECT DEFAULT_CHARACTER_SET_NAME,DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=?",(&original.database,)).map_err(|e|e.to_string())?;
        let (charset, collation) = match defaults {
            Some(value) => value,
            None => conn
                .query_first("SELECT @@character_set_server,@@collation_server")
                .map_err(|e| e.to_string())?
                .ok_or("server charset metadata unavailable")?,
        };
        conn.query_drop(format!(
            "CREATE DATABASE {} CHARACTER SET {} COLLATE {}",
            quote_ident("mysql", name),
            quote_ident("mysql", &charset),
            quote_ident("mysql", &collation)
        ))
        .map_err(|e| {
            format!("safe candidate creation failed (existing namespaces are never reused): {e}")
        })
    } else {
        adapter.execute_sql(&format!(
            "CREATE SCHEMA {}",
            quote_ident("postgresql", name)
        ))
    }
}

fn count(adapter: &mut LiveAdapter, sql: &str) -> Result<u64, String> {
    match adapter {
        LiveAdapter::MySql(conn) => conn
            .query_first::<u64, _>(sql)
            .map_err(|e| e.to_string())?
            .ok_or("verification count missing".into()),
        LiveAdapter::PostgreSql(client) => client
            .query_one(sql, &[])
            .map(|row| row.get::<_, i64>(0) as u64)
            .map_err(|e| e.to_string()),
    }
}

fn verify_candidate(
    endpoint: &Endpoint,
    manifest: &DumpManifest,
    input_path: &Path,
    timezone: Option<&str>,
    progress: &mut dyn FnMut(&str, usize, usize),
) -> Result<Value, String> {
    let inspection = inspect_live(endpoint)?;
    let mut unsupported = inspection.unsupported_objects;
    unsupported.sort();
    unsupported.dedup();
    let rejected = unsupported
        .iter()
        .filter(|object| !object.starts_with("view:"))
        .collect::<Vec<_>>();
    if !rejected.is_empty() {
        return Err(format!(
            "candidate verification found unsupported objects: {}",
            rejected.into_iter().cloned().collect::<Vec<_>>().join(", ")
        ));
    }
    let mut actual = inspection.schema;
    actual
        .tables
        .sort_by(|left, right| left.name.cmp(&right.name));
    for table in &mut actual.tables {
        table
            .indexes
            .sort_by(|left, right| left.name.cmp(&right.name));
        table
            .foreign_keys
            .sort_by(|left, right| left.name.cmp(&right.name));
        table
            .checks
            .sort_by(|left, right| left.name.cmp(&right.name));
    }
    let actual_schema_sha256 = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&actual).map_err(|error| error.to_string())?)
    );
    let unsupported_inventory_sha256 = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&unsupported).map_err(|error| error.to_string())?)
    );
    verify_schema(&manifest.schema, &actual, &endpoint.engine)?;
    let mut adapter = LiveAdapter::connect(endpoint)?;
    let mut counts = BTreeMap::new();
    let mut content_digests = BTreeMap::new();
    let mut foreign_keys_checked = 0;
    for (index, table) in manifest.tables.iter().enumerate() {
        progress(&table.name, index + 1, manifest.tables.len());
        let actual = count(
            &mut adapter,
            &format!(
                "SELECT COUNT(*) FROM {}",
                quote_ident(&endpoint.engine, &table.name)
            ),
        )?;
        if actual != table.rows {
            return Err(format!(
                "candidate verification row count mismatch for {}: expected {}, actual {actual}",
                table.name, table.rows
            ));
        }
        counts.insert(table.name.clone(), actual);
        let definition = manifest
            .schema
            .tables
            .iter()
            .find(|t| t.name == table.name)
            .ok_or("candidate schema definition missing")?;
        let digest = super::safe_restore_digest::verify_table_content(
            endpoint,
            definition,
            table,
            input_path,
            &manifest.data_format,
            &manifest.compression,
            timezone,
        )?;
        content_digests.insert(table.name.clone(), digest);
    }
    for table in &manifest.schema.tables {
        for fk in &table.foreign_keys {
            if fk.columns.is_empty() || fk.columns.len() != fk.referenced_columns.len() {
                return Err(format!("invalid FK column shape in {}", table.name));
            }
            let q = |name: &str| quote_ident(&endpoint.engine, name);
            let join = fk
                .columns
                .iter()
                .zip(&fk.referenced_columns)
                .map(|(c, p)| format!("c.{}=p.{}", q(c), q(p)))
                .collect::<Vec<_>>()
                .join(" AND ");
            let present = fk
                .columns
                .iter()
                .map(|c| format!("c.{} IS NOT NULL", q(c)))
                .collect::<Vec<_>>()
                .join(" AND ");
            let sql=format!("SELECT COUNT(*) FROM {} c LEFT JOIN {} p ON {join} WHERE {present} AND p.{} IS NULL",q(&table.name),q(&fk.referenced_table),q(&fk.referenced_columns[0]));
            let orphans = count(&mut adapter, &sql)?;
            if orphans > 0 {
                return Err(format!(
                    "candidate verification found {orphans} orphan rows for FK {} on {}",
                    fk.name, table.name
                ));
            }
            foreign_keys_checked += 1;
        }
    }
    let views = collect_views(endpoint)?;
    let expected_views = manifest
        .views
        .iter()
        .map(|v| v.name.as_str())
        .collect::<BTreeSet<_>>();
    let actual_views = views
        .iter()
        .map(|v| v.name.as_str())
        .collect::<BTreeSet<_>>();
    if expected_views != actual_views {
        return Err("candidate verification view names/count mismatch".into());
    }
    // Bind the server-deparsed definitions actually prepared, not a comparison
    // with source formatting. Keep every byte, including literals and security
    // clauses: sanitizing this proof would mask later DEFINER/expression changes.
    let view_definition_sha256 = views
        .iter()
        .map(|view| {
            (
                view.name.clone(),
                format!("{:x}", Sha256::digest(view.definition.as_bytes())),
            )
        })
        .collect::<BTreeMap<_, _>>();
    if let LiveAdapter::MySql(conn) = &mut adapter {
        let definer_views: Option<u64> = conn.exec_first(
            "SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=? AND SECURITY_TYPE<>'INVOKER'",
            (&endpoint.database,),
        ).map_err(|error| error.to_string())?;
        if definer_views.unwrap_or(0) > 0 {
            return Err("candidate verification requires SQL SECURITY INVOKER views".into());
        }
    }
    // A successful CREATE VIEW can still refer back to the live namespace. The
    // candidate must not masquerade as an isolated copy while reading old data.
    let external_dependencies: u64 = match &mut adapter {
        LiveAdapter::MySql(conn)=>conn.exec_first(
            "SELECT COUNT(*) FROM information_schema.VIEW_TABLE_USAGE WHERE VIEW_SCHEMA=? AND TABLE_SCHEMA<>? AND TABLE_SCHEMA NOT IN ('mysql','sys','information_schema','performance_schema')",
            (&endpoint.database,&endpoint.database),
        ).map_err(|e|e.to_string())?.unwrap_or(0),
        LiveAdapter::PostgreSql(client)=>client.query_one(
            "SELECT COUNT(*) FROM pg_rewrite r JOIN pg_class v ON v.oid=r.ev_class JOIN pg_namespace vn ON vn.oid=v.relnamespace JOIN pg_depend d ON d.objid=r.oid AND d.classid='pg_rewrite'::regclass JOIN pg_class referenced ON referenced.oid=d.refobjid JOIN pg_namespace rn ON rn.oid=referenced.relnamespace WHERE vn.nspname=$1 AND v.relkind='v' AND d.refclassid='pg_class'::regclass AND rn.nspname<>$1 AND rn.nspname NOT IN ('pg_catalog','information_schema')",
            &[&endpoint_schema(endpoint)],
        ).map_err(|e|e.to_string())?.get::<_,i64>(0) as u64,
    };
    if external_dependencies > 0 {
        return Err("candidate verification found views depending on another namespace; isolation must be resolved before switching".into());
    }
    Ok(
        json!({"proof_version":2,"actual_schema_sha256":actual_schema_sha256,
        "unsupported_inventory_sha256":unsupported_inventory_sha256,"view_definition_sha256":view_definition_sha256,
        "actual_row_counts":counts,"content_digests":content_digests,"schema":"declared_columns_indexes_foreign_keys_verified",
        "foreign_keys_data_checked":foreign_keys_checked,"views_verified":views.len(),
        "warnings":super::source_dump_warnings(manifest),"scope":"Declared dump fields; legacy metadata omissions cannot prove original feature fidelity."}),
    )
}

fn canonical_sql(value: &str) -> String {
    let mut result = String::new();
    let mut quote = None;
    for ch in value.chars() {
        if let Some(delimiter) = quote {
            result.push(ch);
            if ch == delimiter {
                quote = None;
            }
        } else if ch == '\'' || ch == '"' || ch == '`' {
            quote = Some(ch);
            result.push(ch);
        } else if !ch.is_whitespace() {
            result.extend(ch.to_lowercase());
        }
    }
    result
}

fn canonical_type(value: &str, engine: &str) -> String {
    let mut value = canonical_sql(value);
    if engine == "mysql" {
        for integer in [
            "tinyint",
            "smallint",
            "mediumint",
            "bigint",
            "int",
            "integer",
        ] {
            let prefix = format!("{integer}(");
            if value.starts_with(&prefix) {
                if let Some(end) = value.find(')') {
                    if value[prefix.len()..end].chars().all(|c| c.is_ascii_digit()) {
                        value.replace_range(integer.len()..=end, "");
                    }
                }
                break;
            }
        }
    }
    value
}

fn canonical_default(value: Option<&str>) -> Option<String> {
    value.map(|value| {
        let value = value.trim();
        let value = if value.starts_with('\'') {
            value.rfind('\'').map(|end| &value[..=end]).unwrap_or(value)
        } else {
            value
        };
        canonical_sql(value).replace("current_timestamp()", "current_timestamp")
    })
}

fn action<'a>(value: &'a Option<ForeignKeyAction>, engine: &str) -> &'a str {
    match value {
        None => {
            if engine == "mysql" {
                "RESTRICT"
            } else {
                "NO ACTION"
            }
        }
        Some(ForeignKeyAction::NoAction) if engine == "mysql" => "RESTRICT",
        Some(value) => value.as_sql(),
    }
}

fn verify_schema(
    expected: &NormalizedSchema,
    actual: &NormalizedSchema,
    engine: &str,
) -> Result<(), String> {
    if expected.tables.len() != actual.tables.len() {
        return Err("candidate verification table count mismatch".into());
    }
    for table in &expected.tables {
        let target = actual
            .tables
            .iter()
            .find(|t| t.name == table.name)
            .ok_or_else(|| format!("candidate missing table {}", table.name))?;
        let mismatch = |detail: &str| {
            format!(
                "candidate schema verification failed for {}: {detail}",
                table.name
            )
        };
        if table.columns.len() != target.columns.len() {
            return Err(mismatch("column count"));
        }
        for (source, column) in table.columns.iter().zip(&target.columns) {
            if source.name != column.name
                || canonical_type(&source.type_name, engine)
                    != canonical_type(&column.type_name, engine)
                || source.nullable != column.nullable
                || source.primary_key != column.primary_key
                || source.unique && !column.unique
            {
                return Err(mismatch(&format!("column {} definition", source.name)));
            }
            if canonical_default(source.default_value.as_deref())
                != canonical_default(column.default_value.as_deref())
                || canonical_default(source.on_update.as_deref())
                    != canonical_default(column.on_update.as_deref())
            {
                return Err(mismatch(&format!(
                    "column {} default/on-update",
                    source.name
                )));
            }
            if source.comment.is_some() && source.comment != column.comment {
                return Err(mismatch("column comment"));
            }
            if source.default_is_expression && !column.default_is_expression {
                return Err(mismatch("default expression kind"));
            }
        }
        if table.table_collation.is_some() && table.table_collation != target.table_collation {
            return Err(mismatch("table collation"));
        }
        if table.comment.is_some() && table.comment != target.comment {
            return Err(mismatch("table comment"));
        }
        if let Some(next) = table.auto_increment {
            if target.auto_increment.unwrap_or(0) < next {
                return Err(mismatch("auto increment counter"));
            }
        }
        for index in &table.indexes {
            let restored = target
                .indexes
                .iter()
                .find(|i| i.name == index.name)
                .ok_or_else(|| mismatch(&format!("missing index {}", index.name)))?;
            if index.columns != restored.columns
                || index.unique != restored.unique
                || index.visible.is_some() && index.visible != restored.visible
            {
                return Err(mismatch(&format!("index {} definition", index.name)));
            }
            for position in 0..index.columns.len() {
                if index.column_prefixes.get(position).copied().flatten()
                    != restored.column_prefixes.get(position).copied().flatten()
                {
                    return Err(mismatch("index prefix"));
                }
            }
        }
        if table.foreign_keys.len() != target.foreign_keys.len() {
            return Err(mismatch("foreign key count"));
        }
        for fk in &table.foreign_keys {
            let restored = target
                .foreign_keys
                .iter()
                .find(|r| r.name == fk.name)
                .ok_or_else(|| mismatch(&format!("missing FK {}", fk.name)))?;
            if fk.columns != restored.columns
                || fk.referenced_table != restored.referenced_table
                || fk.referenced_columns != restored.referenced_columns
                || action(&fk.on_delete, engine) != action(&restored.on_delete, engine)
                || action(&fk.on_update, engine) != action(&restored.on_update, engine)
            {
                return Err(mismatch(&format!("FK {} definition", fk.name)));
            }
        }
        for check in &table.checks {
            let restored = target
                .checks
                .iter()
                .find(|r| r.name == check.name)
                .ok_or_else(|| mismatch("missing CHECK"))?;
            if canonical_sql(&check.expression) != canonical_sql(&restored.expression)
                || check.enforced != restored.enforced
            {
                return Err(mismatch("CHECK definition"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verified_plan_binds_original_namespace_candidate_and_content_proof() {
        let dir = std::env::temp_dir().join(format!(
            "tf-safe-plan-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("_tunnelforge_dump.json"), b"{}").unwrap();
        let original = Endpoint {
            engine: "mysql".into(),
            host: "127.0.0.1".into(),
            port: 3306,
            user: "user".into(),
            password: "secret-value".into(),
            database: "app".into(),
            schema: None,
            tls: Default::default(),
        };
        let candidate = Endpoint {
            database: "tf_restore_123_456".into(),
            schema: Some("tf_restore_123_456".into()),
            ..original.clone()
        };
        let mut report = json!({"mode":"safe","status":"ready_for_switch","success":true,"verified":true,
            "candidate_created":true,"namespace_created":true,"namespace_existed":true,"original_unchanged":true,"cutover_pending":true,
            "restore_id":"123_456","original_target":public_target(&original),"candidate_target":public_target(&candidate),
            "dump_manifest_sha256":sha256_file(&dir.join("_tunnelforge_dump.json")).unwrap(),
            "verification":{"proof_version":2,"actual_schema_sha256":"schema-proof","unsupported_inventory_sha256":"inventory-proof","view_definition_sha256":{},"content_digests":{"items":{"rows":1,"sum":"original-proof"}}}});
        report["plan_digest"] = json!(plan_digest(&report));
        let path = dump_import_report_path(&dir).unwrap();
        write_dump_import_report(&dir, &report).unwrap();
        let plan = load_verified_plan(&path, &original).unwrap();
        assert_eq!(plan.candidate.database, candidate.database);
        assert_eq!(plan.candidate.password, "secret-value");
        assert!(!plan.report.to_string().contains("secret-value"));
        let mut wrong = original.clone();
        wrong.database = "another_live_database".into();
        assert!(load_verified_plan(&path, &wrong).is_err());
        report["verification"]["content_digests"]["items"]["sum"] = json!("changed-proof");
        write_dump_import_report(&dir, &report).unwrap();
        assert!(load_verified_plan(&path, &original).is_err());
        report["plan_digest"] = json!(plan_digest(&report));
        report["candidate_target"]["database"] = json!("app");
        report["plan_digest"] = json!(plan_digest(&report));
        write_dump_import_report(&dir, &report).unwrap();
        assert!(load_verified_plan(&path, &original).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn declared_schema_verification_checks_keys_defaults_and_legacy_widths() {
        let expected = crate::adapters::test_support::schema();
        let mut actual = expected.clone();
        verify_schema(&expected, &actual, "mysql").unwrap();
        actual.tables[0].columns[0].primary_key = false;
        assert!(verify_schema(&expected, &actual, "mysql").is_err());
        assert_eq!(
            canonical_type("INT(11) UNSIGNED", "mysql"),
            canonical_type("int unsigned", "mysql")
        );
        assert_ne!(
            canonical_type("ENUM('UP')", "mysql"),
            canonical_type("ENUM('up')", "mysql")
        );
        assert_ne!(canonical_default(Some("'NULL'")), canonical_default(None));
        assert_ne!(
            action(&Some(ForeignKeyAction::Restrict), "postgresql"),
            action(&None, "postgresql")
        );
    }
}
