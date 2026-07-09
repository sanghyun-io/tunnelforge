use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

use mysql::prelude::Queryable;
use crate::*;
use crate::schema::error_event;

pub(crate) fn preflight_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(
        request,
        "preflight",
        "preflight checks started",
    ));
    let mut issues = preflight_issues(&request.payload);
    emit(phase_event(
        request,
        "preflight",
        "schema compatibility checks completed",
    ));
    emit(phase_event(request, "preflight", "checking target state"));
    issues.extend(live_preflight_issues(&request.payload));
    emit(phase_event(
        request,
        "preflight",
        "target state checks completed",
    ));

    for issue in &issues {
        emit(json!({
            "event": "issue",
            "request_id": request.request_id,
            "issue": issue
        }));
    }

    emit(phase_event(request, "preflight", "preflight result ready"));
    emit(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "preflight",
        "success": !issues.iter().any(|issue| issue.blocking),
        "issues": issues
    }));
}

pub(crate) fn oneclick_run_streaming<F: FnMut(Value)>(request: &Request, mut emit: F) {
    emit(phase_event(
        request,
        "preflight",
        "one-click preflight started",
    ));
    emit(oneclick_progress_event(request, 5, "Pre-flight started"));
    let state = match oneclick_preflight_state(request) {
        Ok(state) => state,
        Err(err) => {
            emit(error_event(request, err));
            return;
        }
    };
    emit(oneclick_preflight_event(request, &state));
    emit(oneclick_progress_event(request, 20, "Pre-flight completed"));
    let mut run_issues = state.issues.clone();
    let payload_issue_offset = run_issues.len();
    run_issues.extend(oneclick_payload_issues(&request.payload));
    if run_issues.iter().any(|issue| issue.blocking) {
        emit(oneclick_final_result(
            request,
            &state.schema_name,
            false,
            &run_issues,
            &run_issues,
            vec!["Pre-flight blocked execution.".to_string()],
        ));
        return;
    }

    emit(phase_event(
        request,
        "analysis",
        "one-click analysis started",
    ));
    let analysis = oneclick_analysis_summary(&state.inspection, &run_issues);
    emit(json!({
        "event": "analysis",
        "request_id": request.request_id,
        "summary": analysis
    }));
    emit(oneclick_progress_event(request, 40, "Analysis completed"));

    emit(phase_event(
        request,
        "recommendation",
        "one-click recommendations ready",
    ));
    let charset_contracts = oneclick_charset_contracts_by_issue_index_with_offset(
        &request.payload,
        payload_issue_offset,
    );
    let recommendations =
        oneclick_recommendations(&run_issues, &state.schema_name, &charset_contracts);
    let recommendation_summary = oneclick_recommendation_summary(&recommendations);
    emit(json!({
        "event": "execution_plan",
        "request_id": request.request_id,
        "steps": recommendations,
        "summary": recommendation_summary
    }));
    emit(oneclick_progress_event(
        request,
        55,
        "Recommendations completed",
    ));

    emit(phase_event(
        request,
        "execution",
        "one-click execution started",
    ));
    let dry_run = request
        .payload
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let plan_payload = json!({
        "schema": state.schema_name,
        "steps": recommendations
    });
    let apply_plan = oneclick_apply_actions(&plan_payload);
    let (
        success_count,
        fail_count,
        skip_count,
        disallowed_fix_attempts,
        applied_fixes,
        execution_log,
    ) = if dry_run {
        (
            0usize,
            0usize,
            apply_plan.actions.len() + apply_plan.skipped,
            apply_plan.disallowed,
            Vec::new(),
            vec!["DRY-RUN: no database changes were executed.".to_string()],
        )
    } else if !apply_plan.disallowed.is_empty() {
        (
            0,
            apply_plan.disallowed.len(),
            apply_plan.skipped,
            apply_plan.disallowed,
            Vec::new(),
            vec!["Disallowed One-Click automatic fix attempt blocked.".to_string()],
        )
    } else if apply_plan.actions.is_empty() {
        (
            0,
            0,
            apply_plan.skipped,
            Vec::new(),
            Vec::new(),
            vec!["No automatic Rust Core fixes are currently required.".to_string()],
        )
    } else {
        match LiveAdapter::connect(&state.endpoint) {
            Ok(mut adapter) => {
                let outcome = oneclick_execute_apply_plan(&apply_plan, &mut adapter);
                (
                    outcome.success_count,
                    outcome.fail_count,
                    apply_plan.skipped,
                    Vec::new(),
                    outcome.applied_fixes,
                    outcome.log,
                )
            }
            Err(err) => (
                0,
                apply_plan.actions.len(),
                apply_plan.skipped,
                Vec::new(),
                Vec::new(),
                vec![format!(
                    "FAILED: unable to connect for One-Click fixes: {err}"
                )],
            ),
        }
    };
    let execution_success = fail_count == 0 && disallowed_fix_attempts.is_empty();
    let report_execution_log = execution_log.clone();
    let report_fail_count = fail_count;
    let report_disallowed_count = disallowed_fix_attempts.len();
    let report_applied_count = applied_fixes.len();
    let execution_message = if dry_run {
        "Execution completed"
    } else if execution_success {
        "Execution completed"
    } else {
        "Execution completed with errors"
    };
    emit(json!({
        "event": "execution",
        "request_id": request.request_id,
        "dry_run": dry_run,
        "success_count": success_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "disallowed_fix_attempts": disallowed_fix_attempts,
        "applied_fixes": applied_fixes,
        "log": execution_log
    }));
    emit(oneclick_progress_event(request, 80, execution_message));

    emit(phase_event(
        request,
        "validation",
        "one-click validation started",
    ));
    let validation_issues = match inspect_live(&state.endpoint) {
        Ok(inspection) => oneclick_issues_from_inspection(&inspection),
        Err(err) => vec![MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "validation".to_string(),
            message: err,
            suggestion: "Check the database connection and rerun validation.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        }],
    };
    let validation_success = validation_issues.is_empty()
        && execution_success
        && report_fail_count == 0
        && report_disallowed_count == 0;
    emit(json!({
        "event": "validation",
        "request_id": request.request_id,
        "all_fixed": validation_success,
        "remaining_issues": validation_issues.clone(),
        "applied_fix_count": report_applied_count
    }));
    emit(oneclick_progress_event(
        request,
        100,
        "Validation completed",
    ));
    emit(oneclick_final_result(
        request,
        &state.schema_name,
        validation_success,
        &run_issues,
        &validation_issues,
        report_execution_log,
    ));
}

pub(crate) fn oneclick_preflight(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "preflight",
        "one-click preflight started",
    )];
    match oneclick_preflight_state(request) {
        Ok(state) => {
            events.push(oneclick_preflight_event(request, &state));
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.preflight",
                "success": !state.issues.iter().any(|issue| issue.blocking),
                "schema": state.schema_name,
                "checks": state.checks,
                "issues": state.issues
            }));
        }
        Err(err) => events.push(error_event(request, err)),
    }
    events
}

pub(crate) fn oneclick_analyze(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "analysis",
        "one-click analysis started",
    )];
    match oneclick_preflight_state(request) {
        Ok(state) => {
            let summary = oneclick_analysis_summary(&state.inspection, &state.issues);
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.analyze",
                "success": true,
                "schema": state.schema_name,
                "summary": summary,
                "issues": state.issues
            }));
        }
        Err(err) => events.push(error_event(request, err)),
    }
    events
}

pub(crate) fn oneclick_recommend(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "recommendation",
        "one-click recommendation started",
    )];
    let schema = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("");
    let issues = oneclick_payload_issues(&request.payload);
    let charset_contracts = oneclick_charset_contracts_by_issue_index(&request.payload);
    let recommendations = oneclick_recommendations(&issues, schema, &charset_contracts);
    let summary = oneclick_recommendation_summary(&recommendations);
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.recommend",
        "success": true,
        "steps": recommendations,
        "summary": summary
    }));
    events
}

pub(crate) fn oneclick_derive_charset_contracts(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "recommendation",
        "one-click charset contract derivation started",
    )];
    let mut schema_name = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let target_charset = request
        .payload
        .get("target_charset")
        .and_then(Value::as_str)
        .unwrap_or("utf8mb4");
    let target_collation = request
        .payload
        .get("target_collation")
        .and_then(Value::as_str)
        .unwrap_or("utf8mb4_0900_ai_ci");
    let mut issues = oneclick_payload_issues(&request.payload);
    let mut table_facts = oneclick_charset_table_facts_from_payload(&request.payload);
    let mut fk_facts = oneclick_charset_fk_facts_from_payload(&request.payload);
    if table_facts.is_empty() && oneclick_has_endpoint_payload(&request.payload) {
        match oneclick_endpoint(request).and_then(|(endpoint, endpoint_schema)| {
            let facts = oneclick_live_charset_facts(&endpoint, &endpoint_schema)?;
            Ok((endpoint_schema, facts))
        }) {
            Ok((endpoint_schema, (live_table_facts, live_fk_facts))) => {
                schema_name = endpoint_schema;
                table_facts = live_table_facts;
                fk_facts = live_fk_facts;
                if issues.is_empty() {
                    issues = oneclick_synthetic_charset_issues_from_facts(
                        &schema_name,
                        &table_facts,
                        &fk_facts,
                        target_charset,
                        target_collation,
                    );
                }
            }
            Err(err) => {
                events.push(error_event(request, err));
                return events;
            }
        }
    }
    let contracts = oneclick_derive_charset_contracts_from_facts(
        &issues,
        &schema_name,
        &table_facts,
        &fk_facts,
        target_charset,
        target_collation,
    );
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.derive_charset_contracts",
        "success": true,
        "schema": schema_name,
        "issues": issues,
        "contracts": contracts,
        "summary": {
            "total_issues": issues.len(),
            "derived_contracts": contracts.len(),
            "manual_review": issues.len().saturating_sub(contracts.len())
        }
    }));
    events
}

pub(crate) fn oneclick_apply_fixes(request: &Request) -> Vec<Value> {
    let dry_run = request
        .payload
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let mut events = vec![phase_event(
        request,
        "execution",
        "one-click apply fixes started",
    )];
    if dry_run {
        let preview = oneclick_dry_run_preview_fixes(&request.payload);
        events.push(json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "oneclick.apply_fixes",
            "success": true,
            "dry_run": true,
            "success_count": 0,
            "fail_count": 0,
            "skip_count": preview.skipped,
            "disallowed_fix_attempts": preview.disallowed,
            "applied_fixes": [],
            "planned_fixes": preview.planned_fixes,
            "log": ["DRY-RUN: no database changes were executed."]
        }));
        return events;
    }

    let plan = oneclick_apply_actions(&request.payload);
    if !plan.disallowed.is_empty() {
        events.push(json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "oneclick.apply_fixes",
            "success": false,
            "dry_run": false,
            "success_count": 0,
            "fail_count": plan.disallowed.len(),
            "skip_count": plan.skipped,
            "disallowed_fix_attempts": plan.disallowed,
            "applied_fixes": [],
            "log": ["Disallowed One-Click automatic fix attempt blocked."]
        }));
        return events;
    }

    if plan.actions.is_empty() {
        events.push(json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "oneclick.apply_fixes",
            "success": true,
            "dry_run": false,
            "success_count": 0,
            "fail_count": 0,
            "skip_count": plan.skipped,
            "disallowed_fix_attempts": [],
            "applied_fixes": [],
            "log": ["No automatic Rust Core fixes are currently required."]
        }));
        return events;
    }

    let (endpoint, _) = match oneclick_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            events.push(error_event(request, err));
            return events;
        }
    };
    if endpoint.engine != "mysql" {
        events.push(error_event(
            request,
            "oneclick.apply_fixes currently supports MySQL engine fixes only",
        ));
        return events;
    }
    let mut adapter = match LiveAdapter::connect(&endpoint) {
        Ok(adapter) => adapter,
        Err(err) => {
            events.push(error_event(request, err));
            return events;
        }
    };
    let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.apply_fixes",
        "success": outcome.fail_count == 0,
        "dry_run": false,
        "success_count": outcome.success_count,
        "fail_count": outcome.fail_count,
        "skip_count": plan.skipped,
        "disallowed_fix_attempts": [],
        "applied_fixes": outcome.applied_fixes,
        "log": outcome.log
    }));
    events
}

pub(crate) fn oneclick_validate(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "validation",
        "one-click validation started",
    )];
    match oneclick_endpoint(request) {
        Ok((endpoint, schema_name)) => {
            let issues = match inspect_live(&endpoint) {
                Ok(inspection) => oneclick_issues_from_inspection(&inspection),
                Err(err) => vec![MigrationIssue {
                    issue_type: None,
                    severity: "error".to_string(),
                    location: "validation".to_string(),
                    message: err,
                    suggestion: "Check the database connection and rerun validation.".to_string(),
                    blocking: true,
                    table_name: None,
                    column_name: None,
                }],
            };
            events.push(json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "oneclick.validate",
                "success": issues.is_empty(),
                "schema": schema_name,
                "remaining_issues": issues,
                "all_fixed": issues.is_empty()
            }));
        }
        Err(err) => events.push(error_event(request, err)),
    }
    events
}

pub(crate) fn oneclick_report(request: &Request) -> Vec<Value> {
    let schema = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("");
    let success = request
        .payload
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let pre_issues = oneclick_payload_issues(&request.payload);
    let remaining_issues = request
        .payload
        .get("remaining_issues")
        .and_then(Value::as_array)
        .map(|issues| {
            issues
                .iter()
                .filter_map(|issue| serde_json::from_value::<MigrationIssue>(issue.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.report",
        "success": success,
        "report": oneclick_report_value(schema, success, &pre_issues, &remaining_issues, Vec::new())
    })]
}

#[derive(Debug, Clone)]
struct OneClickState {
    endpoint: Endpoint,
    schema_name: String,
    inspection: InspectionResult,
    checks: Vec<Value>,
    issues: Vec<MigrationIssue>,
}

fn oneclick_preflight_state(request: &Request) -> Result<OneClickState, String> {
    let (endpoint, schema_name) = oneclick_endpoint(request)?;
    let mut checks = Vec::new();
    let mut issues = Vec::new();
    if endpoint.engine != "mysql" {
        checks.push(oneclick_check(
            "MySQL engine",
            false,
            "error",
            "One-Click migration currently supports MySQL endpoints only.",
        ));
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "connection".to_string(),
            message: "One-Click migration currently supports MySQL endpoints only.".to_string(),
            suggestion: "Use Cross-Engine Migration for PostgreSQL workflows.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        });
    } else {
        checks.push(oneclick_check(
            "MySQL engine",
            true,
            "info",
            "MySQL endpoint confirmed.",
        ));
    }

    if request
        .payload
        .get("backup_confirmed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        checks.push(oneclick_check(
            "Backup status",
            true,
            "info",
            "Backup confirmation was provided.",
        ));
    } else {
        checks.push(oneclick_check(
            "Backup status",
            false,
            "warning",
            "Backup confirmation was not provided.",
        ));
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "warning".to_string(),
            location: "backup".to_string(),
            message: "Backup confirmation was not provided.".to_string(),
            suggestion: "Confirm a restorable backup before running destructive fixes.".to_string(),
            blocking: false,
            table_name: None,
            column_name: None,
        });
    }

    match inspect_live(&endpoint) {
        Ok(inspection) => {
            checks.push(oneclick_check(
                "Schema inspect",
                true,
                "info",
                &format!("Inspected {} table(s).", inspection.schema.tables.len()),
            ));
            issues.extend(oneclick_issues_from_inspection(&inspection));
            Ok(OneClickState {
                endpoint,
                schema_name,
                inspection,
                checks,
                issues,
            })
        }
        Err(err) => {
            checks.push(oneclick_check("Schema inspect", false, "error", &err));
            issues.push(MigrationIssue {
                issue_type: None,
                severity: "error".to_string(),
                location: "schema".to_string(),
                message: err,
                suggestion: "Check database connection, schema, and inspection permissions."
                    .to_string(),
                blocking: true,
                table_name: None,
                column_name: None,
            });
            Ok(OneClickState {
                endpoint,
                schema_name,
                inspection: InspectionResult::default(),
                checks,
                issues,
            })
        }
    }
}

fn oneclick_endpoint(request: &Request) -> Result<(Endpoint, String), String> {
    let mut endpoint = request_endpoint(request)?;
    let schema_name = request
        .payload
        .get("schema")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|schema| !schema.is_empty())
        .map(ToString::to_string)
        .unwrap_or_else(|| endpoint_schema(&endpoint));
    if endpoint.engine == "mysql" {
        endpoint.database = schema_name.clone();
        endpoint.schema = None;
    } else {
        endpoint.schema = Some(schema_name.clone());
    }
    Ok((endpoint, schema_name))
}

fn oneclick_check(name: &str, passed: bool, severity: &str, message: &str) -> Value {
    json!({
        "name": name,
        "passed": passed,
        "severity": severity,
        "message": message
    })
}

fn oneclick_issues_from_inspection(inspection: &InspectionResult) -> Vec<MigrationIssue> {
    inspection
        .unsupported_objects
        .iter()
        .map(|object| MigrationIssue {
            issue_type: oneclick_deprecated_engine_marker(object)
                .map(|_| "deprecated_engine".to_string()),
            severity: "warning".to_string(),
            location: oneclick_deprecated_engine_marker(object)
                .map(|(table, _)| table.clone())
                .unwrap_or_else(|| object.clone()),
            message: oneclick_deprecated_engine_marker(object)
                .map(|(table, engine)| {
                    format!("Deprecated storage engine detected on table {table}: {engine}")
                })
                .unwrap_or_else(|| format!("Unsupported object detected: {object}")),
            suggestion: oneclick_deprecated_engine_marker(object)
                .map(|_| "Convert the table to InnoDB.".to_string())
                .unwrap_or_else(|| {
                    "Review this object manually before promoting One-Click migration.".to_string()
                }),
            blocking: false,
            table_name: oneclick_deprecated_engine_marker(object).map(|(table, _)| table),
            column_name: None,
        })
        .collect()
}

fn oneclick_deprecated_engine_marker(object: &str) -> Option<(String, String)> {
    let mut parts = object.splitn(3, ':');
    match (parts.next(), parts.next(), parts.next()) {
        (Some("deprecated_engine"), Some(table), Some(engine))
            if !table.trim().is_empty() && !engine.trim().is_empty() =>
        {
            Some((table.trim().to_string(), engine.trim().to_string()))
        }
        _ => None,
    }
}

fn oneclick_payload_issues(payload: &Value) -> Vec<MigrationIssue> {
    payload
        .get("issues")
        .and_then(Value::as_array)
        .map(|issues| {
            issues
                .iter()
                .filter_map(|issue| serde_json::from_value::<MigrationIssue>(issue.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default()
}

fn oneclick_preflight_event(request: &Request, state: &OneClickState) -> Value {
    json!({
        "event": "preflight",
        "request_id": request.request_id,
        "schema": state.schema_name,
        "passed": !state.issues.iter().any(|issue| issue.blocking),
        "checks": state.checks,
        "issues": state.issues
    })
}

fn oneclick_analysis_summary(inspection: &InspectionResult, issues: &[MigrationIssue]) -> Value {
    json!({
        "total_issues": issues.len(),
        "auto_fixable": 0,
        "manual_review": issues.len(),
        "table_count": inspection.schema.tables.len(),
        "unsupported_object_count": inspection.unsupported_objects.len()
    })
}

fn oneclick_recommendations(
    issues: &[MigrationIssue],
    schema: &str,
    charset_contracts: &BTreeMap<usize, Value>,
) -> Vec<Value> {
    issues
        .iter()
        .enumerate()
        .map(|(index, issue)| {
            let selected_option =
                oneclick_auto_fix_option(issue, schema, charset_contracts.get(&index))
                    .unwrap_or_else(|| oneclick_manual_option(issue));
            json!({
                "issue_index": index,
                "issue_type": issue.issue_type.clone().unwrap_or_else(|| "unknown".to_string()),
                "location": issue.location,
                "table_name": issue.table_name,
                "column_name": issue.column_name,
                "description": issue.message,
                "selected_option": selected_option
            })
        })
        .collect()
}

fn oneclick_recommendation_summary(steps: &[Value]) -> Value {
    let auto_fixable = steps
        .iter()
        .filter(|step| {
            step.get("selected_option")
                .and_then(|option| option.get("strategy"))
                .and_then(Value::as_str)
                .map(|strategy| strategy != "manual" && strategy != "skip")
                .unwrap_or(false)
        })
        .count();
    json!({
        "total_issues": steps.len(),
        "auto_fixable": auto_fixable,
        "manual_review": steps.len().saturating_sub(auto_fixable),
        "skip_recommended": 0
    })
}

fn oneclick_manual_option(issue: &MigrationIssue) -> Value {
    json!({
        "strategy": "manual",
        "label": "Manual review",
        "description": issue.suggestion,
        "sql_template": ""
    })
}

fn oneclick_auto_fix_option(
    issue: &MigrationIssue,
    schema: &str,
    charset_contract: Option<&Value>,
) -> Option<Value> {
    match issue.issue_type.as_deref() {
        Some("deprecated_engine") => {
            let table = issue.table_name.as_deref()?.trim();
            if table.is_empty() || schema.trim().is_empty() {
                return None;
            }
            Some(json!({
                "strategy": "engine_innodb",
                "label": "Convert table to InnoDB",
                "description": "Convert this deprecated storage engine table to InnoDB.",
                "sql_template": format!(
                    "ALTER TABLE {}.{} ENGINE=InnoDB;",
                    quote_ident("mysql", schema.trim()),
                    quote_ident("mysql", table),
                )
            }))
        }
        Some("charset_issue") => charset_contract.and_then(|contract| {
            oneclick_charset_fk_safe_option_from_payload(contract, schema).ok()
        }),
        _ => None,
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
struct OneClickCharsetTableFact {
    table: String,
    charset: String,
    collation: String,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
struct OneClickCharsetFkFact {
    table: String,
    referenced_table: String,
}

fn oneclick_charset_table_facts_from_payload(payload: &Value) -> Vec<OneClickCharsetTableFact> {
    payload
        .get("table_facts")
        .and_then(Value::as_array)
        .map(|facts| {
            facts
                .iter()
                .filter_map(|fact| {
                    serde_json::from_value::<OneClickCharsetTableFact>(fact.clone()).ok()
                })
                .collect()
        })
        .unwrap_or_default()
}

fn oneclick_charset_fk_facts_from_payload(payload: &Value) -> Vec<OneClickCharsetFkFact> {
    payload
        .get("foreign_key_facts")
        .and_then(Value::as_array)
        .map(|facts| {
            facts
                .iter()
                .filter_map(|fact| {
                    serde_json::from_value::<OneClickCharsetFkFact>(fact.clone()).ok()
                })
                .collect()
        })
        .unwrap_or_default()
}

fn oneclick_has_endpoint_payload(payload: &Value) -> bool {
    ["connection", "endpoint", "source", "target"]
        .iter()
        .any(|key| payload.get(*key).is_some())
}

fn oneclick_live_charset_facts(
    endpoint: &Endpoint,
    schema: &str,
) -> Result<(Vec<OneClickCharsetTableFact>, Vec<OneClickCharsetFkFact>), String> {
    if endpoint.engine != "mysql" {
        return Err("oneclick.derive_charset_contracts currently supports MySQL only".to_string());
    }
    let opts = mysql_opts(endpoint);
    let pool = mysql::Pool::new(opts).map_err(|err| format!("mysql pool error: {err}"))?;
    let mut conn = pool
        .get_conn()
        .map_err(|err| format!("mysql connection error: {err}"))?;
    let table_facts = conn
        .exec_map(
            "SELECT t.TABLE_NAME, ccsa.CHARACTER_SET_NAME, t.TABLE_COLLATION \
             FROM information_schema.TABLES t \
             JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY ccsa \
             ON t.TABLE_COLLATION = ccsa.COLLATION_NAME \
             WHERE t.TABLE_SCHEMA = ? AND t.TABLE_TYPE = 'BASE TABLE' \
             ORDER BY t.TABLE_NAME",
            (schema,),
            |(table, charset, collation): (String, String, String)| OneClickCharsetTableFact {
                table,
                charset,
                collation,
            },
        )
        .map_err(|err| format!("mysql charset fact inspect error: {err}"))?;
    let fk_facts = conn
        .exec_map(
            "SELECT TABLE_NAME, REFERENCED_TABLE_NAME \
             FROM information_schema.KEY_COLUMN_USAGE \
             WHERE TABLE_SCHEMA = ? AND REFERENCED_TABLE_NAME IS NOT NULL \
             GROUP BY TABLE_NAME, REFERENCED_TABLE_NAME \
             ORDER BY TABLE_NAME, REFERENCED_TABLE_NAME",
            (schema,),
            |(table, referenced_table): (String, String)| OneClickCharsetFkFact {
                table,
                referenced_table,
            },
        )
        .map_err(|err| format!("mysql charset FK fact inspect error: {err}"))?;
    Ok((table_facts, fk_facts))
}

fn oneclick_synthetic_charset_issues_from_facts(
    schema: &str,
    table_facts: &[OneClickCharsetTableFact],
    fk_facts: &[OneClickCharsetFkFact],
    target_charset: &str,
    target_collation: &str,
) -> Vec<MigrationIssue> {
    let mut seen_groups = BTreeSet::new();
    let mut issues = Vec::new();
    for fact in table_facts {
        let candidate = MigrationIssue {
            issue_type: Some("charset_issue".to_string()),
            severity: "warning".to_string(),
            location: format!("{schema}.{}", fact.table),
            message: "Table uses a legacy charset/collation.".to_string(),
            suggestion: "Convert table charset/collation after FK-safe review.".to_string(),
            blocking: false,
            table_name: Some(fact.table.clone()),
            column_name: None,
        };
        let contracts = oneclick_derive_charset_contracts_from_facts(
            std::slice::from_ref(&candidate),
            schema,
            table_facts,
            fk_facts,
            target_charset,
            target_collation,
        );
        let Some(contract) = contracts.first() else {
            continue;
        };
        let tables = contract
            .get("tables")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(ToString::to_string)
            .collect::<Vec<_>>();
        if tables.is_empty() {
            continue;
        }
        let group_key = tables.join("\0");
        if !seen_groups.insert(group_key) {
            continue;
        }
        let table_name = contract
            .get("fk_order")
            .and_then(Value::as_array)
            .and_then(|values| values.first())
            .and_then(Value::as_str)
            .unwrap_or(&fact.table)
            .to_string();
        issues.push(MigrationIssue {
            location: format!("{schema}.{table_name}"),
            table_name: Some(table_name),
            ..candidate
        });
    }
    issues
}

fn oneclick_derive_charset_contracts_from_facts(
    issues: &[MigrationIssue],
    schema: &str,
    table_facts: &[OneClickCharsetTableFact],
    fk_facts: &[OneClickCharsetFkFact],
    target_charset: &str,
    target_collation: &str,
) -> Vec<Value> {
    let Ok(schema) = oneclick_safe_charset_identifier(schema, "schema") else {
        return Vec::new();
    };
    let Ok(target_charset) = oneclick_safe_charset_token(Some(target_charset), "target_charset")
    else {
        return Vec::new();
    };
    let Ok(target_collation) =
        oneclick_safe_charset_token(Some(target_collation), "target_collation")
    else {
        return Vec::new();
    };
    let table_by_name = table_facts
        .iter()
        .map(|fact| (fact.table.as_str(), fact))
        .collect::<BTreeMap<_, _>>();

    issues
        .iter()
        .enumerate()
        .filter_map(|(issue_index, issue)| {
            if issue.issue_type.as_deref() != Some("charset_issue") || issue.blocking {
                return None;
            }
            let table = issue.table_name.as_deref()?.trim();
            oneclick_safe_charset_identifier(table, "table").ok()?;
            let closure = oneclick_charset_fk_closure(table, &table_by_name, fk_facts)?;
            let fk_order = oneclick_charset_fk_order(&closure, fk_facts)?;
            let mut before_charset: Option<&str> = None;
            let mut before_collation: Option<&str> = None;
            for table in &fk_order {
                let fact = *table_by_name.get(table.as_str())?;
                oneclick_safe_charset_identifier(&fact.table, "table").ok()?;
                let charset = fact.charset.trim();
                let collation = fact.collation.trim();
                if charset.is_empty() || collation.is_empty() {
                    return None;
                }
                if charset.eq_ignore_ascii_case(&target_charset)
                    && collation.eq_ignore_ascii_case(&target_collation)
                {
                    return None;
                }
                match (before_charset, before_collation) {
                    (Some(existing_charset), Some(existing_collation))
                        if !charset.eq_ignore_ascii_case(existing_charset)
                            || !collation.eq_ignore_ascii_case(existing_collation) =>
                    {
                        return None;
                    }
                    (None, None) => {
                        before_charset = Some(charset);
                        before_collation = Some(collation);
                    }
                    _ => {}
                }
            }

            let rollback_sql = fk_order
                .iter()
                .rev()
                .map(|table| {
                    let fact = *table_by_name.get(table.as_str())?;
                    Some(format!(
                        "ALTER TABLE {}.{} CONVERT TO CHARACTER SET {} COLLATE {};",
                        quote_ident("mysql", &schema),
                        quote_ident("mysql", table),
                        fact.charset.trim(),
                        fact.collation.trim()
                    ))
                })
                .collect::<Option<Vec<_>>>()?;

            Some(json!({
                "issue_index": issue_index,
                "tables": fk_order,
                "fk_order": fk_order,
                "target_charset": target_charset,
                "target_collation": target_collation,
                "rollback_sql": rollback_sql
            }))
        })
        .collect()
}

fn oneclick_charset_fk_closure(
    seed_table: &str,
    table_by_name: &BTreeMap<&str, &OneClickCharsetTableFact>,
    fk_facts: &[OneClickCharsetFkFact],
) -> Option<BTreeSet<String>> {
    table_by_name.get(seed_table)?;
    let mut closure = BTreeSet::from([seed_table.to_string()]);
    loop {
        let before_len = closure.len();
        for fk in fk_facts {
            if closure.contains(&fk.table) || closure.contains(&fk.referenced_table) {
                table_by_name.get(fk.table.as_str())?;
                table_by_name.get(fk.referenced_table.as_str())?;
                oneclick_safe_charset_identifier(&fk.table, "table").ok()?;
                oneclick_safe_charset_identifier(&fk.referenced_table, "table").ok()?;
                closure.insert(fk.table.clone());
                closure.insert(fk.referenced_table.clone());
            }
        }
        if closure.len() == before_len {
            return Some(closure);
        }
    }
}

fn oneclick_charset_fk_order(
    closure: &BTreeSet<String>,
    fk_facts: &[OneClickCharsetFkFact],
) -> Option<Vec<String>> {
    let mut remaining = closure.clone();
    let mut ordered = Vec::new();
    while !remaining.is_empty() {
        let next = remaining.iter().find_map(|table| {
            let has_unresolved_parent = fk_facts.iter().any(|fk| {
                fk.table == *table
                    && closure.contains(&fk.referenced_table)
                    && remaining.contains(&fk.referenced_table)
            });
            if has_unresolved_parent {
                None
            } else {
                Some(table.clone())
            }
        })?;
        remaining.remove(&next);
        ordered.push(next);
    }
    Some(ordered)
}

fn oneclick_charset_contracts_by_issue_index(payload: &Value) -> BTreeMap<usize, Value> {
    oneclick_charset_contracts_by_issue_index_with_offset(payload, 0)
}

fn oneclick_charset_contracts_by_issue_index_with_offset(
    payload: &Value,
    offset: usize,
) -> BTreeMap<usize, Value> {
    payload
        .get("charset_contracts")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|contract| {
            let index = contract.get("issue_index").and_then(Value::as_u64)? as usize;
            Some((index + offset, contract.clone()))
        })
        .collect()
}

fn oneclick_charset_fk_safe_option_from_payload(
    payload: &Value,
    schema: &str,
) -> Result<Value, String> {
    let schema = oneclick_safe_charset_identifier(schema, "schema")?;
    let tables = oneclick_required_string_list(payload.get("tables"), "tables")?;
    let fk_order = oneclick_required_string_list(payload.get("fk_order"), "fk_order")?;
    let target_charset = oneclick_safe_charset_token(
        payload.get("target_charset").and_then(Value::as_str),
        "target_charset",
    )?;
    let target_collation = oneclick_safe_charset_token(
        payload.get("target_collation").and_then(Value::as_str),
        "target_collation",
    )?;
    let rollback_sql = oneclick_required_string_list(payload.get("rollback_sql"), "rollback_sql")?;

    if tables.is_empty() {
        return Err("tables must not be empty".to_string());
    }
    if rollback_sql.is_empty() {
        return Err("rollback_sql must not be empty".to_string());
    }
    for table in &tables {
        oneclick_safe_charset_identifier(table, "table")?;
    }
    for table in &fk_order {
        oneclick_safe_charset_identifier(table, "fk_order")?;
    }
    let table_set: BTreeSet<_> = tables.iter().cloned().collect();
    let fk_order_set: BTreeSet<_> = fk_order.iter().cloned().collect();
    if table_set != fk_order_set {
        return Err("fk_order must cover the same tables as tables".to_string());
    }

    let sql = fk_order
        .iter()
        .map(|table| {
            format!(
                "ALTER TABLE {}.{} CONVERT TO CHARACTER SET {} COLLATE {};",
                quote_ident("mysql", &schema),
                quote_ident("mysql", table),
                target_charset,
                target_collation
            )
        })
        .collect::<Vec<_>>();

    Ok(json!({
        "strategy": "charset_collation_fk_safe",
        "label": "Convert table charset/collation with FK-safe ordering",
        "description": "Convert the FK-connected table set to the explicit target charset/collation.",
        "tables": tables,
        "fk_order": fk_order,
        "target_charset": target_charset,
        "target_collation": target_collation,
        "sql": sql,
        "rollback_sql": rollback_sql
    }))
}

fn oneclick_required_string_list(
    value: Option<&Value>,
    label: &str,
) -> Result<Vec<String>, String> {
    let Some(values) = value.and_then(Value::as_array) else {
        return Err(format!("{label} must be an array"));
    };
    values
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::trim)
                .filter(|text| !text.is_empty())
                .map(ToString::to_string)
                .ok_or_else(|| format!("{label} must contain non-empty strings"))
        })
        .collect()
}

fn oneclick_safe_charset_identifier(value: &str, label: &str) -> Result<String, String> {
    let value = value.trim();
    if !value.starts_with("tf_oneclick_")
        || !value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
    {
        return Err(format!("{label} must use a safe tf_oneclick_ identifier"));
    }
    Ok(value.to_string())
}

fn oneclick_safe_charset_token(value: Option<&str>, label: &str) -> Result<String, String> {
    let Some(value) = value.map(str::trim).filter(|value| !value.is_empty()) else {
        return Err(format!("{label} is required"));
    };
    if !value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
    {
        return Err(format!("{label} must be a safe token"));
    }
    Ok(value.to_string())
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OneClickApplyAction {
    issue_type: String,
    strategy: String,
    schema: String,
    table: String,
    sql: String,
    tables: Vec<String>,
    fk_order: Vec<String>,
    sql_statements: Vec<String>,
    rollback_sql: Vec<String>,
    target_charset: Option<String>,
    target_collation: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OneClickApplyPlan {
    actions: Vec<OneClickApplyAction>,
    skipped: usize,
    disallowed: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OneClickApplyOutcome {
    success_count: usize,
    fail_count: usize,
    log: Vec<String>,
    applied_fixes: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OneClickDryRunPreview {
    planned_fixes: Vec<Value>,
    skipped: usize,
    disallowed: Vec<String>,
}

fn oneclick_apply_actions(payload: &Value) -> OneClickApplyPlan {
    let schema = payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let mut actions = Vec::new();
    let mut skipped = 0usize;
    let mut disallowed = Vec::new();

    for step in payload
        .get("steps")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let issue_type = step
            .get("issue_type")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let selected = step.get("selected_option").unwrap_or(&Value::Null);
        let strategy = selected
            .get("strategy")
            .and_then(Value::as_str)
            .unwrap_or("manual");

        if strategy == "manual" || strategy == "skip" {
            skipped += 1;
            continue;
        }
        if issue_type == "charset_issue" && strategy == "charset_collation_fk_safe" {
            match oneclick_charset_fk_safe_option_from_payload(selected, schema) {
                Ok(option) => {
                    let tables = oneclick_required_string_list(option.get("tables"), "tables")
                        .unwrap_or_default();
                    let fk_order =
                        oneclick_required_string_list(option.get("fk_order"), "fk_order")
                            .unwrap_or_default();
                    let sql_statements =
                        oneclick_required_string_list(option.get("sql"), "sql").unwrap_or_default();
                    let rollback_sql =
                        oneclick_required_string_list(option.get("rollback_sql"), "rollback_sql")
                            .unwrap_or_default();
                    actions.push(OneClickApplyAction {
                        issue_type: issue_type.to_string(),
                        strategy: strategy.to_string(),
                        schema: schema.to_string(),
                        table: tables.first().cloned().unwrap_or_default(),
                        sql: sql_statements.first().cloned().unwrap_or_default(),
                        tables,
                        fk_order,
                        sql_statements,
                        rollback_sql,
                        target_charset: option
                            .get("target_charset")
                            .and_then(Value::as_str)
                            .map(ToString::to_string),
                        target_collation: option
                            .get("target_collation")
                            .and_then(Value::as_str)
                            .map(ToString::to_string),
                    });
                }
                Err(_) => disallowed.push(format!("{issue_type}:{strategy}")),
            }
            continue;
        }
        if issue_type != "deprecated_engine" || strategy != "engine_innodb" {
            disallowed.push(format!("{issue_type}:{strategy}"));
            continue;
        }

        let Some(table) = oneclick_apply_step_table(step, schema) else {
            skipped += 1;
            continue;
        };
        let sql = format!(
            "ALTER TABLE {}.{} ENGINE=InnoDB;",
            quote_ident("mysql", schema),
            quote_ident("mysql", &table),
        );
        if selected
            .get("sql_template")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|template| !template.is_empty() && *template != sql)
            .is_some()
        {
            disallowed.push(format!("{issue_type}:{strategy}:sql_mismatch"));
            continue;
        }
        actions.push(OneClickApplyAction {
            issue_type: issue_type.to_string(),
            strategy: strategy.to_string(),
            schema: schema.to_string(),
            table: table.clone(),
            sql: sql.clone(),
            tables: vec![table],
            fk_order: Vec::new(),
            sql_statements: vec![sql],
            rollback_sql: Vec::new(),
            target_charset: None,
            target_collation: None,
        });
    }

    OneClickApplyPlan {
        actions,
        skipped,
        disallowed,
    }
}

fn oneclick_dry_run_preview_fixes(payload: &Value) -> OneClickDryRunPreview {
    let schema = payload
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let mut planned_fixes = Vec::new();
    let mut skipped = 0usize;
    let mut disallowed = Vec::new();

    for step in payload
        .get("steps")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let issue_type = step
            .get("issue_type")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let selected = step.get("selected_option").unwrap_or(&Value::Null);
        let strategy = selected
            .get("strategy")
            .and_then(Value::as_str)
            .unwrap_or("manual");

        if strategy == "manual" || strategy == "skip" {
            skipped += 1;
            continue;
        }
        if issue_type == "charset_issue" && strategy == "charset_collation_fk_safe" {
            match oneclick_charset_fk_safe_option_from_payload(selected, schema) {
                Ok(mut plan) => {
                    if let Some(object) = plan.as_object_mut() {
                        object.insert("issue_type".to_string(), json!("charset_issue"));
                        object.insert("schema".to_string(), json!(schema));
                        object.insert("dry_run".to_string(), json!(true));
                        object.insert("success".to_string(), json!(false));
                    }
                    planned_fixes.push(plan);
                }
                Err(_) => disallowed.push(format!("{issue_type}:{strategy}")),
            }
            continue;
        }
        if issue_type == "deprecated_engine" && strategy == "engine_innodb" {
            let Some(table) = oneclick_apply_step_table(step, schema) else {
                skipped += 1;
                continue;
            };
            planned_fixes.push(json!({
                "issue_type": "deprecated_engine",
                "strategy": "engine_innodb",
                "schema": schema,
                "table": table,
                "sql": format!(
                    "ALTER TABLE {}.{} ENGINE=InnoDB;",
                    quote_ident("mysql", schema),
                    quote_ident("mysql", &table),
                ),
                "dry_run": true,
                "success": false
            }));
            continue;
        }
        disallowed.push(format!("{issue_type}:{strategy}"));
    }

    OneClickDryRunPreview {
        planned_fixes,
        skipped,
        disallowed,
    }
}

fn oneclick_execute_apply_plan<A: MigrationAdapter>(
    plan: &OneClickApplyPlan,
    adapter: &mut A,
) -> OneClickApplyOutcome {
    let mut success_count = 0usize;
    let mut fail_count = 0usize;
    let mut log = Vec::new();
    let mut applied_fixes = Vec::new();

    for action in &plan.actions {
        let mut action_error = None;
        for sql in &action.sql_statements {
            match adapter.execute_sql(sql) {
                Ok(()) => {
                    log.push(format!("APPLIED: {sql}"));
                }
                Err(err) => {
                    log.push(format!("FAILED: {sql}: {err}"));
                    action_error = Some(err);
                    break;
                }
            }
        }

        if let Some(err) = action_error {
            fail_count += 1;
            applied_fixes.push(oneclick_applied_fix_payload(action, false, Some(&err)));
        } else {
            success_count += 1;
            applied_fixes.push(oneclick_applied_fix_payload(action, true, None));
        }
    }

    OneClickApplyOutcome {
        success_count,
        fail_count,
        log,
        applied_fixes,
    }
}

fn oneclick_applied_fix_payload(
    action: &OneClickApplyAction,
    success: bool,
    error: Option<&str>,
) -> Value {
    if action.issue_type == "charset_issue" && action.strategy == "charset_collation_fk_safe" {
        let mut payload = json!({
            "issue_type": action.issue_type,
            "strategy": action.strategy,
            "schema": action.schema,
            "tables": action.tables,
            "target_charset": action.target_charset,
            "target_collation": action.target_collation,
            "sql": action.sql_statements,
            "rollback_sql": action.rollback_sql,
            "fk_order": action.fk_order,
            "success": success
        });
        if let Some(error) = error {
            payload["error"] = json!(error);
        }
        return payload;
    }

    let mut payload = json!({
        "issue_type": action.issue_type,
        "strategy": action.strategy,
        "schema": action.schema,
        "table": action.table,
        "sql": action.sql,
        "success": success,
        "rows_affected": 0
    });
    if let Some(error) = error {
        payload["error"] = json!(error);
    }
    payload
}

fn oneclick_apply_step_table(step: &Value, schema: &str) -> Option<String> {
    if schema.trim().is_empty() {
        return None;
    }
    if let Some(table) = step
        .get("table_name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|table| !table.is_empty())
    {
        return Some(table.to_string());
    }

    let location = step.get("location").and_then(Value::as_str)?.trim();
    let mut parts = location.split('.');
    match (parts.next(), parts.next(), parts.next()) {
        (Some(location_schema), Some(table), None)
            if location_schema == schema && !table.is_empty() =>
        {
            Some(table.to_string())
        }
        _ => None,
    }
}

fn oneclick_progress_event(request: &Request, percent: u64, message: &str) -> Value {
    json!({
        "event": "progress",
        "request_id": request.request_id,
        "percent": percent,
        "message": message
    })
}

fn oneclick_final_result(
    request: &Request,
    schema: &str,
    success: bool,
    pre_issues: &[MigrationIssue],
    remaining_issues: &[MigrationIssue],
    execution_log: Vec<String>,
) -> Value {
    json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "oneclick.run",
        "success": success,
        "report": oneclick_report_value(schema, success, pre_issues, remaining_issues, execution_log)
    })
}

fn oneclick_report_value(
    schema: &str,
    success: bool,
    pre_issues: &[MigrationIssue],
    remaining_issues: &[MigrationIssue],
    execution_log: Vec<String>,
) -> Value {
    json!({
        "schema": schema,
        "started_at": current_unix_seconds().to_string(),
        "completed_at": current_unix_seconds().to_string(),
        "pre_issue_count": pre_issues.len(),
        "post_issue_count": remaining_issues.len(),
        "fixed_issues": [],
        "remaining_issues": remaining_issues,
        "new_issues": [],
        "success": success,
        "execution_log": execution_log,
        "duration_seconds": 0.0
    })
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::json;
    use std::collections::BTreeMap;
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::RecordingAdapter;

    #[test]
    fn oneclick_issues_classify_deprecated_engine_marker_as_auto_fixable() {
        let inspection = InspectionResult {
            schema: NormalizedSchema {
                tables: vec![NormalizedTable {
                    name: "legacy_table".to_string(),
                    columns: Vec::new(),
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                }],
            },
            unsupported_objects: vec!["deprecated_engine:legacy_table:MyISAM".to_string()],
        };

        let issues = oneclick_issues_from_inspection(&inspection);
        let charset_contracts = BTreeMap::new();
        let recommendations = oneclick_recommendations(&issues, "app", &charset_contracts);
        let summary = oneclick_recommendation_summary(&recommendations);

        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].issue_type.as_deref(), Some("deprecated_engine"));
        assert_eq!(issues[0].table_name.as_deref(), Some("legacy_table"));
        assert_eq!(summary["auto_fixable"], 1);
        assert_eq!(
            recommendations[0]["selected_option"]["strategy"],
            "engine_innodb"
        );
    }

    #[test]
    fn oneclick_live_inspection_does_not_synthesize_int_display_width_skip() {
        let inspection = InspectionResult {
            schema: NormalizedSchema {
                tables: vec![NormalizedTable {
                    name: "orders".to_string(),
                    columns: Vec::new(),
                    indexes: Vec::new(),
                    foreign_keys: Vec::new(),
                    table_collation: None,
                }],
            },
            unsupported_objects: vec!["int_display_width:orders.id".to_string()],
        };

        let issues = oneclick_issues_from_inspection(&inspection);
        let charset_contracts = BTreeMap::new();
        let recommendations = oneclick_recommendations(&issues, "app", &charset_contracts);
        let summary = oneclick_recommendation_summary(&recommendations);

        assert_eq!(issues.len(), 1);
        assert_ne!(issues[0].issue_type.as_deref(), Some("int_display_width"));
        assert_eq!(summary["auto_fixable"], 0);
        assert_eq!(summary["skip_recommended"], 0);
        assert_eq!(recommendations[0]["selected_option"]["strategy"], "manual");
    }

    #[test]
    fn oneclick_charset_contract_builds_fk_safe_option() {
        let option = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap();

        assert_eq!(option["strategy"], "charset_collation_fk_safe");
        assert_eq!(option["target_charset"], "utf8mb4");
        assert_eq!(option["target_collation"], "utf8mb4_0900_ai_ci");
        assert_eq!(
            option["tables"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            option["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            option["sql"],
            json!([
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            ])
        );
        assert_eq!(option["rollback_sql"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn oneclick_derives_charset_contract_from_safe_fk_facts() {
        let issues = vec![MigrationIssue {
            issue_type: Some("charset_issue".to_string()),
            severity: "warning".to_string(),
            location: "tf_oneclick_charset.tf_oneclick_parent".to_string(),
            message: "Table uses a legacy charset.".to_string(),
            suggestion: "Convert table charset/collation after FK-safe review.".to_string(),
            blocking: false,
            table_name: Some("tf_oneclick_parent".to_string()),
            column_name: None,
        }];
        let table_facts = vec![
            OneClickCharsetTableFact {
                table: "tf_oneclick_parent".to_string(),
                charset: "utf8mb3".to_string(),
                collation: "utf8mb3_general_ci".to_string(),
            },
            OneClickCharsetTableFact {
                table: "tf_oneclick_child".to_string(),
                charset: "utf8mb3".to_string(),
                collation: "utf8mb3_general_ci".to_string(),
            },
        ];
        let fk_facts = vec![OneClickCharsetFkFact {
            table: "tf_oneclick_child".to_string(),
            referenced_table: "tf_oneclick_parent".to_string(),
        }];

        let contracts = oneclick_derive_charset_contracts_from_facts(
            &issues,
            "tf_oneclick_charset",
            &table_facts,
            &fk_facts,
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        );

        assert_eq!(contracts.len(), 1);
        assert_eq!(contracts[0]["issue_index"], 0);
        assert_eq!(
            contracts[0]["tables"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            contracts[0]["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(contracts[0]["target_charset"], "utf8mb4");
        assert_eq!(contracts[0]["target_collation"], "utf8mb4_0900_ai_ci");
        assert_eq!(
            contracts[0]["rollback_sql"],
            json!([
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
            ])
        );
    }

    #[test]
    fn oneclick_derives_no_charset_contract_for_unsafe_or_incomplete_facts() {
        let issues = vec![MigrationIssue {
            issue_type: Some("charset_issue".to_string()),
            severity: "warning".to_string(),
            location: "prod.customer".to_string(),
            message: "Table uses a legacy charset.".to_string(),
            suggestion: "Convert table charset/collation after FK-safe review.".to_string(),
            blocking: false,
            table_name: Some("customer".to_string()),
            column_name: None,
        }];
        let table_facts = vec![OneClickCharsetTableFact {
            table: "customer".to_string(),
            charset: "utf8mb3".to_string(),
            collation: "utf8mb3_general_ci".to_string(),
        }];

        let contracts = oneclick_derive_charset_contracts_from_facts(
            &issues,
            "prod",
            &table_facts,
            &[],
            "utf8mb4",
            "utf8mb4_0900_ai_ci",
        );

        assert!(contracts.is_empty());
    }

    #[test]
    fn oneclick_charset_contract_rejects_missing_target() {
        let err = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent"],
                "fk_order": ["tf_oneclick_parent"],
                "target_charset": "utf8mb4",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap_err();

        assert!(err.contains("target_collation"));
    }

    #[test]
    fn oneclick_charset_contract_rejects_incomplete_fk_order() {
        let err = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                "fk_order": ["tf_oneclick_parent"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap_err();

        assert!(err.contains("fk_order"));
    }

    #[test]
    fn oneclick_charset_contract_rejects_unsafe_schema_or_table() {
        let unsafe_schema = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["tf_oneclick_parent"],
                "fk_order": ["tf_oneclick_parent"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "prod",
        )
        .unwrap_err();
        assert!(unsafe_schema.contains("schema"));

        let unsafe_table = oneclick_charset_fk_safe_option_from_payload(
            &json!({
                "tables": ["users"],
                "fk_order": ["users"],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    "ALTER TABLE `tf_oneclick_charset`.`users` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                ]
            }),
            "tf_oneclick_charset",
        )
        .unwrap_err();
        assert!(unsafe_table.contains("table"));
    }

    #[test]
    fn oneclick_apply_actions_accepts_only_engine_innodb_steps() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "app",
            "steps": [{
                "issue_type": "deprecated_engine",
                "location": "app.legacy_table",
                "selected_option": {
                    "strategy": "engine_innodb",
                    "sql_template": "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
                }
            }, {
                "issue_type": "charset_issue",
                "location": "app.users.name",
                "selected_option": {
                    "strategy": "manual",
                    "sql_template": ""
                }
            }]
        }));

        assert_eq!(plan.actions.len(), 1);
        assert_eq!(plan.skipped, 1);
        assert_eq!(plan.disallowed.len(), 0);
        assert_eq!(plan.actions[0].issue_type, "deprecated_engine");
        assert_eq!(plan.actions[0].strategy, "engine_innodb");
        assert_eq!(plan.actions[0].schema, "app");
        assert_eq!(plan.actions[0].table, "legacy_table");
        assert_eq!(
            plan.actions[0].sql,
            "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
        );
    }

    #[test]
    fn oneclick_apply_plan_executes_engine_innodb_sql() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "app",
            "steps": [{
                "issue_type": "deprecated_engine",
                "location": "app.legacy_table",
                "selected_option": {
                    "strategy": "engine_innodb",
                    "sql_template": "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
                }
            }]
        }));
        let mut adapter = RecordingAdapter::default();

        let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);

        assert_eq!(
            adapter.executed_sql,
            vec!["ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"]
        );
        assert_eq!(outcome.success_count, 1);
        assert_eq!(outcome.fail_count, 0);
        assert_eq!(outcome.applied_fixes.len(), 1);
        assert_eq!(outcome.applied_fixes[0]["strategy"], "engine_innodb");
        assert_eq!(outcome.applied_fixes[0]["success"], true);
    }

    #[test]
    fn oneclick_apply_plan_executes_charset_sql_in_fk_order_with_rollback_metadata() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "tf_oneclick_charset",
            "steps": [{
                "issue_type": "charset_issue",
                "location": "tf_oneclick_charset.tf_oneclick_parent",
                "table_name": "tf_oneclick_parent",
                "selected_option": {
                    "strategy": "charset_collation_fk_safe",
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                    ],
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                    ]
                }
            }]
        }));
        let mut adapter = RecordingAdapter::default();

        let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);

        assert_eq!(
            adapter.executed_sql,
            vec![
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            ]
        );
        assert_eq!(outcome.success_count, 1);
        assert_eq!(outcome.fail_count, 0);
        assert_eq!(outcome.applied_fixes[0]["issue_type"], "charset_issue");
        assert_eq!(
            outcome.applied_fixes[0]["strategy"],
            "charset_collation_fk_safe"
        );
        assert_eq!(
            outcome.applied_fixes[0]["tables"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(
            outcome.applied_fixes[0]["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
        assert_eq!(outcome.applied_fixes[0]["target_charset"], "utf8mb4");
        assert_eq!(
            outcome.applied_fixes[0]["target_collation"],
            "utf8mb4_0900_ai_ci"
        );
        assert_eq!(
            outcome.applied_fixes[0]["rollback_sql"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(outcome.applied_fixes[0]["success"], true);
    }

    #[test]
    fn oneclick_apply_plan_reports_charset_failure_with_rollback_metadata() {
        let plan = oneclick_apply_actions(&json!({
            "schema": "tf_oneclick_charset",
            "steps": [{
                "issue_type": "charset_issue",
                "location": "tf_oneclick_charset.tf_oneclick_parent",
                "table_name": "tf_oneclick_parent",
                "selected_option": {
                    "strategy": "charset_collation_fk_safe",
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                    ],
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                    ]
                }
            }]
        }));
        let mut adapter = RecordingAdapter {
            fail_sql_contains: Some("tf_oneclick_child".to_string()),
            ..RecordingAdapter::default()
        };

        let outcome = oneclick_execute_apply_plan(&plan, &mut adapter);

        assert_eq!(
            adapter.executed_sql,
            vec!["ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"]
        );
        assert_eq!(outcome.success_count, 0);
        assert_eq!(outcome.fail_count, 1);
        assert!(outcome
            .log
            .iter()
            .any(|line| line.contains("tf_oneclick_child") && line.contains("FAILED")));
        assert_eq!(outcome.applied_fixes[0]["success"], false);
        assert!(outcome.applied_fixes[0]["error"]
            .as_str()
            .unwrap()
            .contains("SQL execution error"));
        assert_eq!(
            outcome.applied_fixes[0]["rollback_sql"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }
}
