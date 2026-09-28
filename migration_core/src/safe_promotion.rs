//! Consent and persisted-plan boundary shared by engine-specific safe cutovers.
use crate::import::safe_restore::{
    load_verified_plan, reverify_candidate, VerifiedSafeRestorePlan,
};
use crate::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize)]
#[serde(tag = "engine", content = "plan")]
enum EnginePlan {
    #[serde(rename = "mysql")]
    MySql(crate::safe_promote_mysql::PromotionPlan),
    #[serde(rename = "postgresql")]
    Postgres(crate::safe_promote_postgres::PromotionPlan),
}

impl EnginePlan {
    fn engine_digest(&self) -> &str {
        match self {
            Self::MySql(plan) => &plan.digest,
            Self::Postgres(plan) => &plan.plan_digest,
        }
    }

    fn backup_namespace(&self) -> &str {
        match self {
            Self::MySql(plan) => &plan.backup_database,
            Self::Postgres(plan) => &plan.backup_schema,
        }
    }

    fn summary(&self) -> Value {
        match self {
            Self::MySql(plan) => crate::safe_promote_mysql::summary(plan),
            Self::Postgres(plan) => crate::safe_promote_postgres::summary(plan),
        }
    }
}

#[derive(Serialize, Deserialize)]
struct StoredPlan {
    restore_id: String,
    restore_digest: String,
    original_target: Value,
    candidate_target: Value,
    engine_plan: EnginePlan,
    attempt_nonce: String,
}

fn public_target(endpoint: &Endpoint) -> Value {
    json!({"engine": endpoint.engine, "host": endpoint.host, "port": endpoint.port,
        "database": endpoint.database, "schema": endpoint_schema(endpoint)})
}

fn digest(plan: &StoredPlan) -> Result<String, String> {
    serde_json::to_vec(plan)
        .map(|bytes| hex::encode(Sha256::digest(bytes)))
        .map_err(|error| format!("cannot encode promotion plan: {error}"))
}

fn validate_prior_outcome(report: &Value) -> Result<(), String> {
    if let Some(previous) = report.get("promotion").filter(|value| !value.is_null()) {
        if previous["status"] != "failed_original_unchanged"
            || previous["original_unchanged"] != true
        {
            return Err("prior promotion is pending, committed or uncertain; reconcile its recorded engine identities before another plan or retry".into());
        }
    }
    Ok(())
}

fn prior_attempt(directory: &Path) -> Result<Option<Value>, String> {
    let path = directory.join("attempt");
    match fs::symlink_metadata(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        _ => return Err("promotion attempt must be an ordinary owned directory".into()),
    }
    let report = dump_import_report_path(&path)?;
    match fs::read(report) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|e| format!("invalid prior attempt journal: {e}")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

fn check_attempt(directory: &Path) -> Result<Option<Value>, String> {
    if fs::symlink_metadata(directory.join("active-attempt")).is_ok() {
        return Err("promotion dispatch is in flight or interrupted; reconcile the persisted attempt before retry".into());
    }
    let prior = prior_attempt(directory)?;
    if let Some(previous) = &prior {
        validate_prior_outcome(&json!({"promotion":previous}))?;
    }
    Ok(prior)
}

fn claim_attempt(directory: &Path, plan_digest: &str, stored: &Value) -> Result<(), String> {
    if let Some(previous) = check_attempt(directory)? {
        if previous["plan_digest"] == plan_digest {
            return Err("this plan was already attempted; review a fresh plan before retry".into());
        }
    }
    let active = directory.join("active-attempt");
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&active)
        .map_err(|e| {
            format!("cannot claim promotion dispatch (another attempt may be active): {e}")
        })?;
    let state = json!({"status":"pending","original_unchanged":null,"plan_digest":plan_digest,"stored_plan":stored});
    file.write_all(&serde_json::to_vec(&state).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    file.sync_all().map_err(|e| e.to_string())?;
    let attempts = directory.join("attempt");
    if !attempts.exists() {
        fs::create_dir(&attempts).map_err(|e| e.to_string())?;
    }
    // The active marker is intentionally retained on checkpoint failure: no
    // engine was dispatched, but a crash must never look like an unused plan.
    write_dump_import_report(&attempts, &state)?;
    #[cfg(unix)]
    fs::File::open(directory)
        .and_then(|file| file.sync_all())
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn finish_attempt(directory: &Path, result: &Value) -> Result<(), String> {
    let mut state = prior_attempt(directory)?.ok_or("promotion attempt journal missing")?;
    state["status"] = result["status"].clone();
    state["original_unchanged"] = result["original_unchanged"].clone();
    state["result"] = result.clone();
    write_dump_import_report(&directory.join("attempt"), &state)?;
    if result["status"] == "failed_original_unchanged" && result["original_unchanged"] == true {
        fs::remove_file(directory.join("active-attempt")).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn check_user_namespace(endpoint: &Endpoint) -> Result<(), String> {
    let database = endpoint.database.to_ascii_lowercase();
    let schema = endpoint_schema(endpoint).to_ascii_lowercase();
    let forbidden = match endpoint.engine.as_str() {
        "mysql" => ["mysql", "sys", "information_schema", "performance_schema"]
            .contains(&database.as_str()),
        "postgresql" => {
            schema == "information_schema"
                || schema.starts_with("pg_")
                || ["template0", "template1"].contains(&database.as_str())
        }
        _ => return Err("unsupported promotion engine".into()),
    };
    if forbidden {
        return Err("safe promotion refuses system namespaces".into());
    }
    Ok(())
}

fn plan_directory(report: &Path, restore_id: &str, create: bool) -> Result<PathBuf, String> {
    if restore_id.is_empty()
        || restore_id.len() > 96
        || !restore_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        return Err("invalid safe restore identifier".into());
    }
    let parent = report.parent().ok_or("report directory is missing")?;
    let path = parent.join(format!(".tunnelforge_promotion_plan_{restore_id}"));
    if create {
        match fs::create_dir(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(format!("cannot create promotion plan directory: {error}")),
        }
    }
    let metadata = fs::symlink_metadata(&path)
        .map_err(|error| format!("cannot inspect promotion plan directory: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("promotion plan directory must be an ordinary directory".into());
    }
    Ok(path)
}

fn load_restore(
    request: &Request,
    credentials: &Endpoint,
) -> Result<(PathBuf, VerifiedSafeRestorePlan), String> {
    let report_path = PathBuf::from(
        request
            .payload
            .get("report_path")
            .and_then(Value::as_str)
            .filter(|path| !path.is_empty())
            .ok_or("safe restore report_path is required")?,
    );
    let verified = load_verified_plan(&report_path, credentials)?;
    validate_prior_outcome(&verified.report)?;
    if request.payload.get("restore_id").and_then(Value::as_str)
        != Some(verified.restore_id.as_str())
    {
        return Err("restore_id does not match the verified restore report".into());
    }
    if verified.report.get("cutover_pending") != Some(&Value::Bool(true)) {
        return Err("this restore has no pending cutover".into());
    }
    check_user_namespace(&verified.original)?;
    check_user_namespace(&verified.candidate)?;
    Ok((report_path, verified))
}

fn run<F: FnMut(Value)>(request: &Request, emit: &mut F) -> Result<Value, String> {
    let action = request
        .payload
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !matches!(action, "plan" | "confirm") {
        return Err("dump.promote action must be plan or confirm".into());
    }
    if action == "confirm" && request.payload.get("overwrite_confirmed") != Some(&Value::Bool(true))
    {
        return Err("safe promotion requires explicit overwrite confirmation".into());
    }
    let credentials = request_endpoint(request)?;
    check_user_namespace(&credentials)?;
    let (report_path, verified) = load_restore(request, &credentials)?;
    let directory = plan_directory(&report_path, &verified.restore_id, true)?;
    check_attempt(&directory)?;
    let original = public_target(&verified.original);
    let candidate = public_target(&verified.candidate);
    emit(json!({"event":"phase", "request_id":request.request_id,
        "phase":"safe_promotion_validation", "message":"Rechecking the verified restore and destination before cutover"}));
    reverify_candidate(&verified)?;

    if action == "plan" {
        let engine_plan = match verified.original.engine.as_str() {
            "mysql" => crate::safe_promote_mysql::plan(
                &verified.original,
                &verified.candidate,
                &verified.restore_id,
            )
            .map(EnginePlan::MySql),
            "postgresql" => crate::safe_promote_postgres::plan(
                &verified.original,
                &verified.candidate,
                &verified.restore_id,
            )
            .map(EnginePlan::Postgres),
            _ => return Err("unsupported promotion engine".into()),
        };
        let engine_plan = match engine_plan {
            Ok(plan) => plan,
            Err(blocker) => {
                return Ok(
                    json!({"success":true,"can_promote":false,"status":"blocked",
                "original_unchanged":true,"cutover_pending":true,
                "original_target":original,"candidate_target":candidate,
                "blockers":[redact_endpoint_secret(&blocker, &credentials)]}),
                )
            }
        };
        let backup = engine_plan.backup_namespace().to_string();
        let summary = engine_plan.summary();
        let stored = StoredPlan {
            restore_id: verified.restore_id.clone(),
            restore_digest: verified.plan_digest.clone(),
            original_target: original.clone(),
            candidate_target: candidate.clone(),
            engine_plan,
            attempt_nonce: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|e| e.to_string())?
                .as_nanos()
                .to_string(),
        };
        let plan_digest = digest(&stored)?;
        let directory = plan_directory(&report_path, &verified.restore_id, true)?;
        write_dump_import_report(
            &directory,
            &serde_json::to_value(&stored).map_err(|error| error.to_string())?,
        )?;
        Ok(json!({"success":true,"can_promote":true,"status":"planned",
            "plan_digest":plan_digest,"restore_id":verified.restore_id,
            "original_target":original,"candidate_target":candidate,"backup_namespace":backup,
            "original_unchanged":true,"cutover_pending":true,"blockers":[],
            "summary":summary}))
    } else {
        let directory = plan_directory(&report_path, &verified.restore_id, false)?;
        let file = dump_import_report_path(&directory)?;
        let metadata = fs::symlink_metadata(&file).map_err(|error| error.to_string())?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            return Err("promotion plan must be an ordinary file".into());
        }
        let stored: StoredPlan =
            serde_json::from_reader(fs::File::open(file).map_err(|error| error.to_string())?)
                .map_err(|error| format!("invalid promotion plan: {error}"))?;
        if stored.restore_id != verified.restore_id
            || stored.restore_digest != verified.plan_digest
            || stored.original_target != original
            || stored.candidate_target != candidate
        {
            return Err("promotion plan no longer matches the verified restore".into());
        }
        let confirmed = request
            .payload
            .get("plan_digest")
            .and_then(Value::as_str)
            .unwrap_or("");
        if confirmed.is_empty() || confirmed != digest(&stored)? {
            return Err("promotion plan changed; review and confirm a fresh plan".into());
        }
        let report_dir = report_path.parent().ok_or("report directory is missing")?;
        emit(json!({"event":"phase","request_id":request.request_id,
            "phase":"safe_promotion_cutover","message":"Executing the confirmed guarded cutover; inspect its journal if the connection is interrupted"}));
        let engine_digest = stored.engine_plan.engine_digest();
        claim_attempt(
            &directory,
            confirmed,
            &serde_json::to_value(&stored).map_err(|e| e.to_string())?,
        )?;
        // Engine Err is restricted to failures before original cutover. Engine
        // code must return structured promoted/unknown once commit was attempted.
        let outcome = match &stored.engine_plan {
            EnginePlan::MySql(plan) => crate::safe_promote_mysql::promote(
                &verified.original,
                &verified.candidate,
                plan,
                engine_digest,
                report_dir,
            ),
            EnginePlan::Postgres(plan) => crate::safe_promote_postgres::promote(
                &verified.original,
                &verified.candidate,
                plan,
                engine_digest,
                report_dir,
            ),
        };
        let mut result = outcome.unwrap_or_else(|error| {
            json!({"success":false,"status":"failed_original_unchanged",
            "original_unchanged":true,"message":redact_endpoint_secret(&error,&credentials)})
        });
        if let Err(error) = finish_attempt(&directory, &result) {
            result["attempt_journal_warning"] = json!(error);
        }
        result["original_target"] = original.clone();
        result["candidate_target"] = candidate;
        result["restore_id"] = json!(verified.restore_id);
        if result.get("backup_namespace").is_none() {
            result["backup_namespace"] = json!(stored.engine_plan.backup_namespace());
        }
        if result.get("success") == Some(&Value::Bool(true)) {
            result["status"] = json!("promoted");
            result["original_unchanged"] = json!(false);
            result["cutover_pending"] = json!(false);
            result["active_target"] = original;
        }
        // Retain the original verification proof and attach the cutover outcome.
        let mut report = verified.report.clone();
        report["promotion"] = result.clone();
        if result.get("success") == Some(&Value::Bool(true)) {
            report["cutover_pending"] = json!(false);
            report["status"] = json!("promoted");
            report["original_unchanged"] = json!(false);
        }
        if let Err(error) = write_dump_import_report(report_dir, &report) {
            result["report_warning"] = json!(format!(
                "Cutover outcome known, but restore report update failed: {error}"
            ));
        }
        Ok(result)
    }
}

pub(crate) fn handle<F: FnMut(Value)>(request: &Request, mut emit: F) {
    match run(request, &mut emit) {
        Ok(mut result) => {
            result["event"] = json!("result");
            result["command"] = json!("dump.promote");
            result["request_id"] = json!(request.request_id);
            result["action"] = request
                .payload
                .get("action")
                .cloned()
                .unwrap_or(Value::Null);
            emit(result);
        }
        Err(error) => {
            let message = request_endpoint(request)
                .map(|endpoint| redact_endpoint_secret(&error, &endpoint))
                .unwrap_or(error);
            emit(
                json!({"event":"error","command":"dump.promote","request_id":request.request_id,"message":message}),
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uncertain_or_pending_promotion_blocks_fresh_plans() {
        for status in ["preparing", "pending", "cutover_unknown", "promoted"] {
            assert!(
                validate_prior_outcome(&json!({"promotion":{"status":status}})).is_err(),
                "accepted {status}"
            );
        }
        assert!(validate_prior_outcome(&json!({})).is_ok());
        assert!(validate_prior_outcome(
            &json!({"promotion":{"status":"failed_original_unchanged","original_unchanged":true}})
        )
        .is_ok());
        assert!(validate_prior_outcome(
            &json!({"promotion":{"status":"failed_original_unchanged","original_unchanged":false}})
        )
        .is_err());
    }

    #[test]
    fn concurrent_or_crashed_dispatch_cannot_claim_another_attempt() {
        let directory = std::env::temp_dir().join(format!(
            "tf_attempt_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&directory).unwrap();
        claim_attempt(&directory, "first", &json!({"engine":"mysql"})).unwrap();
        let result = claim_attempt(&directory, "fresh-plan", &json!({"engine":"mysql"}));
        fs::remove_dir_all(&directory).unwrap();
        assert!(
            result.is_err(),
            "fresh plan bypassed persistent in-flight marker"
        );
    }

    #[test]
    fn proven_rollback_consumes_old_digest_but_allows_fresh_confirmation() {
        let directory = std::env::temp_dir().join(format!(
            "tf_retry_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&directory).unwrap();
        claim_attempt(&directory, "first", &json!({"engine":"postgresql"})).unwrap();
        finish_attempt(
            &directory,
            &json!({"status":"failed_original_unchanged","original_unchanged":true}),
        )
        .unwrap();
        assert!(claim_attempt(&directory, "first", &json!({})).is_err());
        claim_attempt(
            &directory,
            "fresh-confirmation",
            &json!({"engine":"postgresql"}),
        )
        .unwrap();
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn unknown_outcome_retains_immutable_plan_proof_and_blocks_replanning() {
        let directory = std::env::temp_dir().join(format!(
            "tf_unknown_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&directory).unwrap();
        let proof = json!({"engine":"mysql","table_ids":[123,456]});
        claim_attempt(&directory, "confirmed", &proof).unwrap();
        finish_attempt(
            &directory,
            &json!({"status":"cutover_unknown","original_unchanged":null}),
        )
        .unwrap();
        assert!(check_attempt(&directory).is_err());
        let persisted = prior_attempt(&directory).unwrap().unwrap();
        assert_eq!(persisted["stored_plan"], proof);
        assert!(claim_attempt(&directory, "fresh", &json!({})).is_err());
        fs::remove_dir_all(directory).unwrap();
    }
}
