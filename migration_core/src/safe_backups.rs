//! TF-STATUS-119: backup/candidate lifecycle after safe promotion.
//!
//! `restore.backups` actions: `list`, `reconcile`, `cleanup_plan`, `cleanup_apply`.
//! Journals (the promotion attempt journal and the safe-restore report kept in each
//! dump directory) are the only source of ownership. A namespace that merely follows
//! the naming convention is listed as unproven and is never deleted.
use crate::safe_promotion::{prior_attempt, public_target, EnginePlan, StoredPlan};
use crate::*;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

const PLAN_DIR_PREFIX: &str = ".tunnelforge_promotion_plan_";

struct Entry {
    input_dir: PathBuf,
    restore_id: String,
    report: Option<Value>,
    stored: Option<StoredPlan>,
    attempt: Option<Value>,
}

impl Entry {
    fn journal_status(&self) -> String {
        match &self.attempt {
            Some(attempt) => attempt["status"].as_str().unwrap_or("pending").to_string(),
            None if self.stored.is_some() => "planned".into(),
            None => "not_attempted".into(),
        }
    }

    fn fingerprint(&self) -> Option<Value> {
        self.attempt.as_ref().and_then(|a| a["result"]["backup_fingerprint"].as_object()).map(|m| Value::Object(m.clone()))
    }
}

fn payload_dirs(request: &Request) -> Result<Vec<PathBuf>, String> {
    let dirs: Vec<PathBuf> = request
        .payload
        .get("input_dirs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
        .collect();
    if dirs.is_empty() {
        return Err("restore.backups requires input_dirs (dump directories that hold the restore journals)".into());
    }
    Ok(dirs)
}

fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

/// Journals of one dump directory: the safe-restore report and every promotion plan/attempt.
fn discover_dir(dir: &Path, entries: &mut Vec<Entry>) {
    let report = read_json(&dump_import_report_path(dir).unwrap_or_default())
        .filter(|report| report["mode"] == "safe" && report["candidate_created"] == true);
    if let Some(report) = &report {
        if let Some(id) = report["restore_id"].as_str() {
            let mut entry = Entry { input_dir: dir.into(), restore_id: id.into(), report: Some(report.clone()), stored: None, attempt: None };
            if let Ok(plan_dir) = plan_dir_for(dir, id) {
                entry.attempt = prior_attempt(&plan_dir).ok().flatten();
                entry.stored = entry
                    .attempt
                    .as_ref()
                    .and_then(|a| serde_json::from_value(a["stored_plan"].clone()).ok())
                    .or_else(|| read_json(&dump_import_report_path(&plan_dir).unwrap_or_default()).and_then(|v| serde_json::from_value(v).ok()));
            }
            entries.push(entry);
            return;
        }
    }
    // Promotion journals without a readable restore report still identify backups.
    let Ok(read) = fs::read_dir(dir) else { return };
    for item in read.flatten() {
        let name = item.file_name().to_string_lossy().to_string();
        let Some(id) = name.strip_prefix(PLAN_DIR_PREFIX) else { continue };
        let Ok(plan_dir) = plan_dir_for(dir, id) else { continue };
        let attempt = prior_attempt(&plan_dir).ok().flatten();
        let stored = attempt
            .as_ref()
            .and_then(|a| serde_json::from_value(a["stored_plan"].clone()).ok())
            .or_else(|| read_json(&dump_import_report_path(&plan_dir).unwrap_or_default()).and_then(|v| serde_json::from_value(v).ok()));
        entries.push(Entry { input_dir: dir.into(), restore_id: id.into(), report: None, stored, attempt });
    }
}

fn plan_dir_for(dir: &Path, restore_id: &str) -> Result<PathBuf, String> {
    if restore_id.is_empty() || !restore_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') {
        return Err("invalid restore identifier".into());
    }
    let path = dir.join(format!("{PLAN_DIR_PREFIX}{restore_id}"));
    let metadata = fs::symlink_metadata(&path).map_err(|e| e.to_string())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("promotion plan directory must be an ordinary directory".into());
    }
    Ok(path)
}

fn discover(request: &Request) -> Result<Vec<Entry>, String> {
    let mut entries = Vec::new();
    for dir in payload_dirs(request)? {
        discover_dir(&dir, &mut entries);
    }
    Ok(entries)
}

fn find<'a>(entries: &'a [Entry], request: &Request) -> Result<&'a Entry, String> {
    let id = request.payload.get("restore_id").and_then(Value::as_str).ok_or("restore_id is required")?;
    let mut matches = entries.iter().filter(|entry| entry.restore_id == id);
    let first = matches.next().ok_or("no journal for this restore_id was found in input_dirs")?;
    if matches.next().is_some() {
        return Err("restore_id is ambiguous across input_dirs".into());
    }
    Ok(first)
}

/// Every engine call names its namespaces explicitly, so the caller's own
/// connection (whose default database is known to exist) is used unchanged.
fn endpoint_for(credentials: &Endpoint, _target: &Value) -> Endpoint {
    credentials.clone()
}

fn same_server(credentials: &Endpoint, target: &Value) -> bool {
    target["engine"] == credentials.engine && target["host"] == credentials.host && target["port"] == credentials.port
}

fn candidate_namespace(entry: &Entry) -> Option<(String, Value)> {
    let target = entry.stored.as_ref().map(|s| s.candidate_target.clone()).or_else(|| entry.report.as_ref().map(|r| r["candidate_target"].clone()))?;
    let name = if target["engine"] == "mysql" { target["database"].as_str() } else { target["schema"].as_str() }?.to_string();
    Some((name, target))
}

fn original_target(entry: &Entry) -> Option<Value> {
    entry.stored.as_ref().map(|s| s.original_target.clone()).or_else(|| entry.report.as_ref().map(|r| r["original_target"].clone()))
}

fn inspect_backup(entry: &Entry, credentials: &Endpoint, deep: bool) -> Result<Option<Value>, String> {
    let (Some(stored), Some(original)) = (&entry.stored, original_target(entry)) else { return Ok(None) };
    if !same_server(credentials, &original) {
        return Ok(None);
    }
    let endpoint = endpoint_for(credentials, &original);
    let fingerprint = entry.fingerprint();
    match &stored.engine_plan {
        EnginePlan::MySql(plan) => crate::safe_promote_mysql::inspect_backup(&endpoint, plan, deep, fingerprint.as_ref()).map(Some),
        EnginePlan::Postgres(plan) => crate::safe_promote_postgres::inspect_backup(&endpoint, plan, deep, fingerprint.as_ref()).map(Some),
    }
}

fn counts(credentials: &Endpoint, _target: &Value, namespace: &str) -> Result<(bool, u64, u64), String> {
    let endpoint = credentials.clone();
    if credentials.engine == "mysql" {
        crate::safe_promote_mysql::namespace_counts(&endpoint, namespace)
    } else {
        crate::safe_promote_postgres::namespace_counts(&endpoint, namespace)
    }
}

fn created_unix(entry: &Entry) -> Option<u64> {
    let nonce = entry.stored.as_ref().map(|s| s.attempt_nonce.clone())?;
    nonce.parse::<u128>().ok().map(|ns| (ns / 1_000_000_000) as u64)
}

fn entry_json(entry: &Entry, credentials: &Endpoint) -> Result<Value, String> {
    let mut out = json!({
        "restore_id": entry.restore_id,
        "input_dir": entry.input_dir,
        "journal_status": entry.journal_status(),
        "created_unix_seconds": created_unix(entry),
        "original_target": original_target(entry),
        "engine": credentials.engine,
    });
    if let Some(backup) = inspect_backup(entry, credentials, false)? {
        out["backup"] = backup;
    }
    if let Some((name, target)) = candidate_namespace(entry) {
        if same_server(credentials, &target) {
            let (exists, tables, views) = counts(credentials, &target, &name)?;
            let owned = name == format!("tf_restore_{}", entry.restore_id);
            out["candidate"] = json!({"namespace": name, "exists": exists, "tables": tables, "views": views,
                "ownership": if owned { "journal_named" } else { "destination_not_a_candidate" },
                "note": if owned { Value::Null } else { json!("This is the restore destination itself, not a temporary candidate; it is never deleted here.") }});
        }
    }
    Ok(out)
}

fn list(request: &Request, credentials: &Endpoint) -> Result<Value, String> {
    let entries = discover(request)?;
    let mut items = Vec::new();
    let mut skipped = 0_u64;
    for entry in &entries {
        match original_target(entry) {
            Some(target) if same_server(credentials, &target) => items.push(entry_json(entry, credentials)?),
            _ => skipped += 1,
        }
    }
    // Namespaces that follow the backup naming but have no journal in input_dirs.
    let known: std::collections::BTreeSet<String> = items.iter().filter_map(|i| i["backup"]["namespace"].as_str().map(str::to_string)).collect();
    let named = if credentials.engine == "mysql" {
        crate::safe_promote_mysql::backup_named_namespaces(credentials)?
    } else {
        crate::safe_promote_postgres::backup_named_namespaces(credentials)?
    };
    let unproven: Vec<Value> = named.into_iter().filter(|n| !known.contains(n)).map(|n| json!({"namespace": n, "ownership": "unproven",
        "note": "No promotion journal for this namespace was found in input_dirs; it is never deleted by TunnelForge."})).collect();
    Ok(json!({"success": true, "backups": items, "unproven_namespaces": unproven, "skipped_other_server": skipped}))
}

fn reconcile(request: &Request, credentials: &Endpoint) -> Result<Value, String> {
    let entries = discover(request)?;
    let entry = find(&entries, request)?;
    let backup = inspect_backup(entry, credentials, false)?.ok_or("this restore has no promotion journal on this server")?;
    let status = entry.journal_status();
    let verdict = backup["verdict"].as_str().unwrap_or("undeterminable").to_string();
    let (conclusion, message) = match (status.as_str(), verdict.as_str()) {
        ("promoted", "promoted") => ("promoted", "The journal and the live objects agree: the cutover took effect."),
        (_, "promoted") => ("promoted", "The live objects show the cutover took effect although the journal did not record a final result."),
        (_, "not_promoted") => ("not_promoted", "The live objects show the original tables are intact and the cutover did not take effect."),
        _ => ("undeterminable", "The live objects do not match either the pre- or post-cutover state. Retry and cleanup stay blocked."),
    };
    Ok(json!({"success": true, "restore_id": entry.restore_id, "journal_status": status, "conclusion": conclusion,
        "message": message, "evidence": backup,
        "retry_allowed": false,
        "cleanup_allowed": conclusion == "promoted",
        "note": "Reconciliation is report-only: it never changes the journal or any database object."}))
}

fn cleanup_plan(request: &Request, credentials: &Endpoint) -> Result<Value, String> {
    let entries = discover(request)?;
    let entry = find(&entries, request)?;
    let target = request.payload.get("target").and_then(Value::as_str).unwrap_or("backup");
    let status = entry.journal_status();
    let mut blockers: Vec<String> = Vec::new();
    let (namespace, tables, notes): (String, Vec<Value>, Vec<String>);
    let mut engine_endpoint = credentials.clone();
    match target {
        "backup" => {
            let backup = inspect_backup(entry, credentials, true)?.ok_or("this restore has no promotion journal on this server")?;
            blockers.extend(backup["blockers"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_string));
            let verdict = backup["verdict"].as_str().unwrap_or("undeterminable");
            if status != "promoted" && verdict != "promoted" {
                blockers.push(format!("promotion outcome is not confirmed (journal {status}, live objects {verdict}); reconcile first"));
            }
            if backup["ownership"] != "proven" {
                blockers.push("ownership of the backup namespace is not proven".into());
            }
            if backup["unchanged"] != true {
                blockers.push("the backup is not proven unchanged since promotion".into());
            }
            namespace = backup["namespace"].as_str().unwrap_or("").to_string();
            if !namespace.starts_with("tf_backup_") {
                blockers.push("namespace does not follow the owned backup naming".into());
            }
            tables = backup["tables"].as_array().cloned().unwrap_or_default();
            notes = if credentials.engine == "mysql" && backup["saved_view_aliases"].as_object().is_some_and(|m| !m.is_empty()) {
                vec!["Saved view aliases live in the original database and are not deleted by this cleanup; they are not views over backup data.".into()]
            } else {
                vec![]
            };
            if let Some(original) = original_target(entry) {
                engine_endpoint = endpoint_for(credentials, &original);
            }
        }
        "candidate" => {
            let (name, candidate) = candidate_namespace(entry).ok_or("no candidate namespace recorded for this restore")?;
            namespace = name.clone();
            if !same_server(credentials, &candidate) {
                blockers.push("the candidate belongs to a different server".into());
            }
            if name != format!("tf_restore_{}", entry.restore_id) {
                blockers.push("this namespace is the restore destination itself, not an owned temporary candidate".into());
            }
            let (exists, table_count, view_count) = counts(credentials, &candidate, &name)?;
            if !exists {
                blockers.push("candidate namespace does not exist".into());
            }
            if table_count + view_count > 0 {
                // A staged candidate still holds data: only an unpromoted, still-verified copy may go.
                let verdict = inspect_backup(entry, credentials, false)?
                    .map(|b| b["verdict"].as_str().unwrap_or("undeterminable").to_string())
                    .unwrap_or_else(|| "none".into());
                let unpromoted = matches!(verdict.as_str(), "none" | "not_promoted")
                    && !matches!(status.as_str(), "promoted" | "cutover_unknown" | "pending");
                if !unpromoted {
                    blockers.push(format!("the candidate still holds data and the promotion state is {status}/{verdict}; only a verified, unpromoted candidate can be discarded"));
                }
                let report_path = dump_import_report_path(&entry.input_dir)?;
                match crate::import::safe_restore::load_verified_plan(&report_path, credentials)
                    .and_then(|verified| crate::import::safe_restore::reverify_candidate(&verified))
                {
                    Ok(_) => {}
                    Err(error) => blockers.push(format!("candidate is not proven unchanged since verification: {error}")),
                }
            }
            let references = if exists {
                let endpoint = endpoint_for(credentials, &candidate);
                if credentials.engine == "mysql" {
                    crate::safe_promote_mysql::external_references(&endpoint, &name)?
                } else {
                    crate::safe_promote_postgres::external_references(&endpoint, &name)?
                }
            } else {
                vec![]
            };
            if !references.is_empty() {
                blockers.push(format!("objects outside the candidate still reference it: {}", references.join(", ")));
            }
            tables = vec![json!({"name": "(tables)", "rows": table_count}), json!({"name": "(views)", "rows": view_count})];
            notes = vec![];
            engine_endpoint = endpoint_for(credentials, &candidate);
        }
        other => return Err(format!("unsupported cleanup target: {other}")),
    }
    let digest_input = json!({"restore_id": entry.restore_id, "target": target, "namespace": namespace, "tables": tables,
        "journal_status": status, "engine": credentials.engine,
        "host": credentials.host, "port": credentials.port, "blockers": blockers});
    let plan_digest = hex::encode(Sha256::digest(digest_input.to_string().as_bytes()));
    let _ = engine_endpoint;
    Ok(json!({"success": true, "can_cleanup": blockers.is_empty(), "target": target, "restore_id": entry.restore_id,
        "namespace": namespace, "tables": tables, "will_delete": [namespace], "blockers": blockers, "notes": notes,
        "plan_digest": plan_digest, "confirmation_required": true,
        "message": "Original and unproven objects are never deleted. Nothing has been changed."}))
}

fn cleanup_apply(request: &Request, credentials: &Endpoint) -> Result<Value, String> {
    if request.payload.get("confirmed") != Some(&Value::Bool(true)) {
        return Err("cleanup requires explicit confirmation (confirmed: true)".into());
    }
    let confirmed = request.payload.get("plan_digest").and_then(Value::as_str).unwrap_or("");
    // Everything is re-derived from the live objects immediately before the drop.
    let fresh = cleanup_plan(request, credentials)?;
    if confirmed.is_empty() || fresh["plan_digest"] != confirmed {
        return Err("the objects changed since the cleanup plan was reviewed; review a fresh plan".into());
    }
    if fresh["can_cleanup"] != true {
        return Err(format!("cleanup is blocked: {}", fresh["blockers"]));
    }
    let entries = discover(request)?;
    let entry = find(&entries, request)?;
    let namespace = fresh["namespace"].as_str().ok_or("namespace missing")?.to_string();
    let target = fresh["target"].as_str().unwrap_or("backup");
    let endpoint = match target {
        "candidate" => endpoint_for(credentials, &candidate_namespace(entry).ok_or("candidate missing")?.1),
        _ => endpoint_for(credentials, &original_target(entry).ok_or("original target missing")?),
    };
    if credentials.engine == "mysql" {
        crate::safe_promote_mysql::drop_namespace(&endpoint, &namespace)?;
    } else {
        crate::safe_promote_postgres::drop_namespace(&endpoint, &namespace)?;
    }
    let record = json!({"restore_id": entry.restore_id, "target": target, "namespace": namespace,
        "plan_digest": confirmed, "dropped": true,
        "target_public": public_target(&endpoint)});
    let mut result = json!({"success": true, "status": "cleaned", "target": target, "namespace": namespace,
        "restore_id": entry.restore_id, "message": "The verified namespace was dropped. Original and other objects were not touched."});
    if let Ok(plan_dir) = plan_dir_for(&entry.input_dir, &entry.restore_id) {
        let path = plan_dir.join(format!("cleanup_{target}.json"));
        if let Err(error) = fs::write(&path, serde_json::to_vec_pretty(&record).unwrap_or_default()) {
            result["journal_warning"] = json!(error.to_string());
        }
    }
    Ok(result)
}

pub(crate) fn handle<F: FnMut(Value)>(request: &Request, mut emit: F) {
    let action = request.payload.get("action").and_then(Value::as_str).unwrap_or("").to_string();
    let outcome = request_endpoint(request).and_then(|credentials| {
        if credentials.engine != "mysql" && credentials.engine != "postgresql" {
            return Err("unsupported engine".into());
        }
        match action.as_str() {
            "list" => list(request, &credentials),
            "reconcile" => reconcile(request, &credentials),
            "cleanup_plan" => cleanup_plan(request, &credentials),
            "cleanup_apply" => cleanup_apply(request, &credentials),
            _ => Err("restore.backups action must be list, reconcile, cleanup_plan or cleanup_apply".into()),
        }
        .map_err(|error| redact_endpoint_secret(&error, &credentials))
    });
    match outcome {
        Ok(mut result) => {
            result["event"] = json!("result");
            result["command"] = json!("restore.backups");
            result["action"] = json!(action);
            result["request_id"] = json!(request.request_id);
            emit(result);
        }
        Err(message) => emit(json!({"event": "error", "command": "restore.backups", "request_id": request.request_id, "message": message})),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("tf_backups_{name}_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn restore_identifiers_cannot_escape_the_journal_directory() {
        let dir = temp("ids");
        for bad in ["", "../x", "a/b", "a b", "a\\b"] {
            assert!(plan_dir_for(&dir, bad).is_err(), "accepted {bad:?}");
        }
        assert!(plan_dir_for(&dir, "missing-id").is_err());
        fs::create_dir(dir.join(format!("{PLAN_DIR_PREFIX}ok_1"))).unwrap();
        assert!(plan_dir_for(&dir, "ok_1").is_ok());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn journal_status_reflects_attempt_state_only() {
        let entry = |attempt: Option<Value>| Entry { input_dir: PathBuf::new(), restore_id: "r".into(), report: None, stored: None, attempt };
        assert_eq!(entry(None).journal_status(), "not_attempted");
        assert_eq!(entry(Some(json!({"status": "cutover_unknown"}))).journal_status(), "cutover_unknown");
        assert_eq!(entry(Some(json!({}))).journal_status(), "pending");
    }

    #[test]
    fn discovery_needs_input_dirs_and_ignores_directories_without_journals() {
        let request = Request { command: "restore.backups".into(), request_id: None, payload: json!({}) };
        assert!(payload_dirs(&request).is_err());
        let dir = temp("empty");
        let mut entries = Vec::new();
        discover_dir(&dir, &mut entries);
        assert!(entries.is_empty());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn unknown_actions_and_missing_confirmation_are_rejected_before_any_connection() {
        let mut events = Vec::new();
        handle(&Request { command: "restore.backups".into(), request_id: None,
            payload: json!({"action": "drop_everything", "endpoint": {"engine": "mysql", "host": "127.0.0.1", "port": 1, "user": "u", "password": "p", "database": "d"}, "input_dirs": ["x"]}) },
            |event| events.push(event));
        assert!(events[0]["message"].as_str().unwrap().contains("action must be"), "{events:?}");
        let mut events = Vec::new();
        handle(&Request { command: "restore.backups".into(), request_id: None,
            payload: json!({"action": "cleanup_apply", "restore_id": "r", "plan_digest": "d", "endpoint": {"engine": "mysql", "host": "127.0.0.1", "port": 1, "user": "u", "password": "p", "database": "d"}, "input_dirs": ["x"]}) },
            |event| events.push(event));
        assert!(events[0]["message"].as_str().unwrap().contains("confirmation"), "{events:?}");
    }
}
