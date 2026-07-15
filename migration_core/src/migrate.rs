use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

use crate::*;

pub(crate) fn readiness(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "readiness",
        "direction readiness checks started",
    )];

    let endpoints = match readiness_endpoints(&request.payload) {
        Ok(endpoints) => endpoints,
        Err(err) => {
            events.push(json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            }));
            return events;
        }
    };

    let mut directions = Vec::new();
    for (source, target) in endpoints {
        events.push(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "readiness",
            "message": format!("checking {} -> {}", source.engine, target.engine)
        }));
        let result = direction_readiness(&request.payload, &source, &target);
        for issue in result
            .get("issues")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            events.push(json!({
                "event": "issue",
                "request_id": request.request_id,
                "issue": issue
            }));
        }
        directions.push(result);
    }

    let success = directions.iter().all(|direction| {
        direction
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    });

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "readiness",
        "success": success,
        "directions": directions
    }));
    events
}

fn readiness_endpoints(payload: &Value) -> Result<Vec<(Endpoint, Endpoint)>, String> {
    let source = payload
        .get("source")
        .ok_or_else(|| "source endpoint is required".to_string())
        .and_then(endpoint_from_value)?;
    let target = payload
        .get("target")
        .ok_or_else(|| "target endpoint is required".to_string())
        .and_then(endpoint_from_value)?;
    if source.engine == target.engine {
        return Err(
            "readiness requires one MySQL endpoint and one PostgreSQL endpoint".to_string(),
        );
    }
    if !is_supported_direction(&source.engine, &target.engine) {
        return Err(format!(
            "unsupported readiness endpoints: {} -> {}",
            source.engine, target.engine
        ));
    }
    Ok(vec![(source.clone(), target.clone()), (target, source)])
}

fn direction_readiness(payload: &Value, source: &Endpoint, target: &Endpoint) -> Value {
    let direction = format!("{}_to_{}", source.engine, target.engine);
    match inspect_live(source) {
        Ok(inspection) => {
            let check_payload = json!({
                "source_engine": source.engine,
                "target_engine": target.engine,
                "source": source,
                "target": target,
                "schema": inspection.schema,
                "unsupported_objects": inspection.unsupported_objects,
                "execution_options": parse_options(payload)
            });
            let mut issues = preflight_issues(&check_payload);
            issues.extend(live_preflight_issues(&check_payload));
            json!({
                "direction": direction,
                "source_engine": source.engine,
                "target_engine": target.engine,
                "success": !issues.iter().any(|issue| issue.blocking),
                "table_count": check_payload["schema"]["tables"].as_array().map(Vec::len).unwrap_or(0),
                "unsupported_object_count": check_payload["unsupported_objects"].as_array().map(Vec::len).unwrap_or(0),
                "issues": issues
            })
        }
        Err(err) => json!({
            "direction": direction,
            "source_engine": source.engine,
            "target_engine": target.engine,
            "success": false,
            "table_count": 0,
            "unsupported_object_count": 0,
            "issues": [{
                "severity": "error",
                "location": "source",
                "message": err,
                "suggestion": "Check the source database connection and permissions.",
                "blocking": true
            }]
        }),
    }
}

pub(crate) fn guide(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(
        request,
        "guide",
        "direction migration guide generation started",
    )];

    let endpoints = match readiness_endpoints(&request.payload) {
        Ok(endpoints) => endpoints,
        Err(err) => {
            events.push(json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            }));
            return events;
        }
    };

    let mut directions = Vec::new();
    for (source, target) in endpoints {
        events.push(json!({
            "event": "phase",
            "request_id": request.request_id,
            "phase": "guide",
            "message": format!("building detailed guide for {} -> {}", source.engine, target.engine)
        }));
        let result = direction_guide(&request.payload, &source, &target);
        for issue in result
            .get("issues")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            events.push(json!({
                "event": "issue",
                "request_id": request.request_id,
                "issue": issue
            }));
        }
        directions.push(result);
    }

    let success = directions.iter().all(|direction| {
        direction
            .get("success")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    });

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "guide",
        "success": success,
        "directions": directions
    }));
    events
}

fn direction_guide(payload: &Value, source: &Endpoint, target: &Endpoint) -> Value {
    let direction = format!("{}_to_{}", source.engine, target.engine);
    match inspect_live(source) {
        Ok(inspection) => {
            let schema = inspection.schema;
            let check_payload = json!({
                "source_engine": source.engine,
                "target_engine": target.engine,
                "source": source,
                "target": target,
                "schema": schema,
                "unsupported_objects": inspection.unsupported_objects,
                "execution_options": parse_options(payload)
            });
            let mut issues = preflight_issues(&check_payload);
            issues.extend(live_preflight_issues(&check_payload));
            let row_limit = guide_row_limit(payload);
            let mut table_guides = Vec::new();
            match LiveAdapter::connect(source) {
                Ok(mut source_adapter) => {
                    table_guides = build_table_guides(
                        &schema,
                        &mut source_adapter,
                        &source.engine,
                        &target.engine,
                        row_limit,
                        &mut issues,
                    );
                }
                Err(err) => issues.push(MigrationIssue {
                    issue_type: None,
                    severity: "error".to_string(),
                    location: "source".to_string(),
                    message: err,
                    suggestion: "Check source database connection before generating row guide."
                        .to_string(),
                    blocking: true,
                    table_name: None,
                    column_name: None,
                }),
            }

            let create_table_sql =
                match generate_schema_ddl(&schema, &source.engine, &target.engine) {
                    Ok(ddl) => ddl,
                    Err(err) => {
                        issues.push(MigrationIssue {
                            issue_type: None,
                            severity: "error".to_string(),
                            location: "schema".to_string(),
                            message: err,
                            suggestion:
                                "Reject or fix the invalid table collation before migrating."
                                    .to_string(),
                            blocking: true,
                            table_name: None,
                            column_name: None,
                        });
                        Vec::new()
                    }
                };

            json!({
                "direction": direction,
                "source_engine": source.engine,
                "target_engine": target.engine,
                "success": !issues.iter().any(|issue| issue.blocking),
                "issues": issues,
                "guide": {
                    "method": [
                        "1. Review blocking issues and warnings.",
                        "2. Execute create_table_sql on an empty target.",
                        "3. Stream table rows in the listed order.",
                        "4. Execute sequence_reset_sql, index_sql, and foreign_key_sql after data load.",
                        "5. Run full verify and inspect mismatches before cutover."
                    ],
                    "row_sample_limit": row_limit,
                    "create_table_sql": create_table_sql,
                    "sequence_reset_sql": generate_sequence_reset_ddl(&schema, &target.engine),
                    "post_data_sql": generate_post_data_ddl(&schema, &target.engine),
                    "unsupported_objects": check_payload["unsupported_objects"].clone(),
                    "tables": table_guides
                }
            })
        }
        Err(err) => json!({
            "direction": direction,
            "source_engine": source.engine,
            "target_engine": target.engine,
            "success": false,
            "issues": [{
                "severity": "error",
                "location": "source",
                "message": err,
                "suggestion": "Check the source database connection and permissions.",
                "blocking": true
            }],
            "guide": {
                "method": ["Fix source connection and run guide again."],
                "row_sample_limit": guide_row_limit(payload),
                "create_table_sql": [],
                "sequence_reset_sql": [],
                "post_data_sql": [],
                "unsupported_objects": [],
                "tables": []
            }
        }),
    }
}

fn guide_row_limit(payload: &Value) -> usize {
    payload
        .get("guide_options")
        .and_then(|options| options.get("row_limit"))
        .and_then(Value::as_u64)
        .map(|value| value.clamp(1, 1000) as usize)
        .unwrap_or(5)
}

fn build_table_guides<A: MigrationAdapter>(
    schema: &NormalizedSchema,
    source: &mut A,
    source_engine: &str,
    target_engine: &str,
    row_limit: usize,
    issues: &mut Vec<MigrationIssue>,
) -> Vec<Value> {
    let mut tables = Vec::new();
    for table in &schema.tables {
        let row_count = match source.row_count(&table.name) {
            Ok(count) => count,
            Err(err) => {
                issues.push(MigrationIssue {
                    issue_type: None,
                    severity: "error".to_string(),
                    location: table.name.clone(),
                    message: err,
                    suggestion: "Check table read permissions.".to_string(),
                    blocking: true,
                    table_name: None,
                    column_name: None,
                });
                0
            }
        };
        let rows = match source.read_rows(table, 0, row_limit) {
            Ok(rows) => rows,
            Err(err) => {
                issues.push(MigrationIssue {
                    issue_type: None,
                    severity: "error".to_string(),
                    location: table.name.clone(),
                    message: err,
                    suggestion: "Check table read permissions.".to_string(),
                    blocking: true,
                    table_name: None,
                    column_name: None,
                });
                Vec::new()
            }
        };
        let columns = table
            .columns
            .iter()
            .map(|column| {
                json!({
                    "name": &column.name,
                    "source_type": &column.type_name,
                    "target_type": map_type(source_engine, target_engine, &strip_generation_marker(&column.type_name)),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                    "unique": column.unique,
                    "default": &column.default_value,
                    "auto_increment": is_auto_increment_type(&column.type_name)
                })
            })
            .collect::<Vec<_>>();
        let insert_example_sql = if rows.is_empty() {
            String::new()
        } else {
            insert_rows_literal_sql_for_table(target_engine, table, &rows)
        };
        tables.push(json!({
            "table": &table.name,
            "row_count": row_count,
            "sample_truncated": row_count > rows.len(),
            "columns": columns,
            "row_samples": rows,
            "insert_example_sql": insert_example_sql,
            "copy_method": format!("Stream rows in chunks and use target {} INSERT batches generated from canonical row values.", target_engine)
        }));
    }
    tables
}

pub(crate) fn plan(request: &Request) -> Vec<Value> {
    let source = read_engine(&request.payload, "source_engine");
    let target = read_engine(&request.payload, "target_engine");
    let schema =
        dependency_ordered_schema(&parse_schema(&request.payload["schema"]).unwrap_or_default());
    let ddl = match generate_schema_ddl(&schema, &source, &target) {
        Ok(ddl) => ddl,
        Err(err) => {
            return vec![
                phase_event(request, "plan", "migration plan generation started"),
                json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                }),
            ];
        }
    };
    let table_order = table_dependency_order(&schema);
    let tables = plan_table_summaries(request, &schema);

    vec![
        phase_event(request, "plan", "migration plan generation started"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "plan",
            "success": true,
            "plan": {
                "ddl": ddl,
                "tables": tables,
                "table_order": table_order,
                "execution_options": parse_options(&request.payload)
            }
        }),
    ]
}

fn plan_table_summaries(request: &Request, schema: &NormalizedSchema) -> Vec<Value> {
    let mut rows_by_table = BTreeMap::<String, usize>::new();
    if let Some(source_data) = request.payload.get("source_data") {
        let source = MemoryAdapter::from_value(Some(source_data));
        for table in &schema.tables {
            rows_by_table.insert(table.name.clone(), source.row_count(&table.name));
        }
    } else if let Some(source_value) = request.payload.get("source") {
        if let Ok(source_endpoint) = endpoint_from_value(source_value) {
            if let Ok(mut source) = LiveAdapter::connect(&source_endpoint) {
                for table in &schema.tables {
                    if let Ok(rows) = source.row_count(&table.name) {
                        rows_by_table.insert(table.name.clone(), rows);
                    }
                }
            }
        }
    }

    schema
        .tables
        .iter()
        .map(|table| {
            json!({
                "name": table.name,
                "estimated_rows": rows_by_table.get(&table.name).copied().unwrap_or(0)
            })
        })
        .collect()
}

/// payload에서 필수 endpoint(`source`/`target`)를 해석한다. 키가 없으면 패닉 대신 Err를
/// 반환하고, endpoint_from_value의 파싱 오류도 그대로 Err로 전달한다. 호출부(migrate/verify)는
/// 반환된 Err를 각자의 error 이벤트 방식(emit vs events.push)으로 처리한다.
fn required_endpoint(payload: &Value, key: &str) -> Result<Endpoint, String> {
    match payload.get(key).map(endpoint_from_value).transpose() {
        Ok(Some(endpoint)) => Ok(endpoint),
        Ok(None) => Err(format!("{key} endpoint is missing")),
        Err(err) => Err(err),
    }
}

pub(crate) fn migrate_streaming<F, R>(request: &Request, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut side_effect_started = false;
    emit_event(
        &mut emit,
        phase_event(request, "migrate", "migration started"),
        side_effect_started,
    )?;
    if request.payload.get("source").is_some() && request.payload.get("target").is_some() {
        let schema = dependency_ordered_schema(
            &parse_schema(&request.payload["schema"]).unwrap_or_default(),
        );
        let options = parse_options(&request.payload);
        let resume_state = request
            .payload
            .get("state")
            .and_then(|value| serde_json::from_value::<ResumeState>(value.clone()).ok());
        let source_endpoint = match required_endpoint(&request.payload, "source") {
            Ok(endpoint) => endpoint,
            Err(err) => {
                emit_event(
                    &mut emit,
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                    side_effect_started,
                )?;
                return Ok(());
            }
        };
        let target_endpoint = match required_endpoint(&request.payload, "target") {
            Ok(endpoint) => endpoint,
            Err(err) => {
                emit_event(
                    &mut emit,
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                    side_effect_started,
                )?;
                return Ok(());
            }
        };

        match (
            LiveAdapter::connect(&source_endpoint),
            LiveAdapter::connect(&target_endpoint),
        ) {
            (Ok(mut source), Ok(mut target)) => {
                if target_endpoint.engine == "postgresql" {
                    side_effect_started = true;
                }
                if let Err(err) = prepare_target_schema(&mut target, &target_endpoint) {
                    emit_event(
                        &mut emit,
                        json!({"event": "error", "request_id": request.request_id, "message": err}),
                        side_effect_started,
                    )?;
                    return Ok(());
                }
                if options.cleanup_before_migrate {
                    if let Err(err) = cleanup_target_tables(
                        &schema,
                        &mut target,
                        &target_endpoint.engine,
                        &mut emit,
                        request,
                        &mut side_effect_started,
                    ) {
                        match err {
                            CleanupControlError::Emit(err) => return Err(err),
                            CleanupControlError::Domain(err) => {
                                emit_event(
                                    &mut emit,
                                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                                    side_effect_started,
                                )?;
                                return Ok(());
                            }
                        }
                    }
                }
                let mut checkpoint =
                    |event: Value| emit(add_request_id(event, &request.request_id));
                let result = migrate_with_adapters_reporting(
                    &schema,
                    &options,
                    resume_state.as_ref(),
                    &mut source,
                    &mut target,
                    &source_endpoint.engine,
                    &target_endpoint.engine,
                    &mut checkpoint,
                    &mut side_effect_started,
                )?;
                emit_event(
                    &mut emit,
                    json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "migrate",
                    "success": result.success,
                    "cancelled": !result.success && options.cancel_after_chunks.is_some() && result.issues.is_empty(),
                    "rows_copied": result.rows_copied,
                    "chunks_copied": result.chunks_copied,
                    "state": result.state,
                    "issues": result.issues
                    }),
                    side_effect_started,
                )?;
            }
            (Err(err), _) | (_, Err(err)) => {
                emit_event(
                    &mut emit,
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                    side_effect_started,
                )?;
            }
        }
        return Ok(());
    }

    if request.payload.get("source_data").is_none() {
        emit_event(
            &mut emit,
            json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "live data streaming is not implemented in this helper build"
            }),
            side_effect_started,
        )?;
        return Ok(());
    }

    let schema =
        dependency_ordered_schema(&parse_schema(&request.payload["schema"]).unwrap_or_default());
    let options = parse_options(&request.payload);
    let resume_state = request
        .payload
        .get("state")
        .and_then(|value| serde_json::from_value::<ResumeState>(value.clone()).ok());
    let source = MemoryAdapter::from_value(request.payload.get("source_data"));
    let mut target = MemoryAdapter::from_value(request.payload.get("target_data"));
    let mut source = source.clone();
    let mut checkpoint = |event: Value| emit(add_request_id(event, &request.request_id));
    let result = migrate_with_adapters_reporting(
        &schema,
        &options,
        resume_state.as_ref(),
        &mut source,
        &mut target,
        "",
        "",
        &mut checkpoint,
        &mut side_effect_started,
    )?;

    for table in &schema.tables {
        emit_event(
            &mut emit,
            json!({
            "event": "table_progress",
            "request_id": request.request_id,
            "table": table.name,
            "status": if result.state.tables.iter().any(|state| state.table == table.name && state.completed) { "completed" } else { "pending" }
            }),
            side_effect_started,
        )?;
    }

    emit_event(
        &mut emit,
        json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "migrate",
        "success": result.success,
        "cancelled": !result.success && options.cancel_after_chunks.is_some() && result.issues.is_empty(),
        "rows_copied": result.rows_copied,
        "chunks_copied": result.chunks_copied,
        "state": result.state,
        "issues": result.issues,
        "target_data": target.rows
        }),
        side_effect_started,
    )?;
    Ok(())
}

fn emit_event<F, R>(emit: &mut F, event: Value, side_effect_started: bool) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    emit(event).into_protocol_emit_result().map_err(|err| {
        if side_effect_started {
            err.after_side_effect()
        } else {
            err
        }
    })
}

fn add_request_id(mut event: Value, request_id: &Option<String>) -> Value {
    if let Value::Object(object) = &mut event {
        object.insert(
            "request_id".to_string(),
            request_id
                .as_ref()
                .map(|value| Value::String(value.clone()))
                .unwrap_or(Value::Null),
        );
    }
    event
}

pub(crate) fn verify(request: &Request) -> Vec<Value> {
    let mut events = vec![phase_event(request, "verify", "verification started")];
    if request.payload.get("source").is_some() && request.payload.get("target").is_some() {
        let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
        let source_endpoint = match required_endpoint(&request.payload, "source") {
            Ok(endpoint) => endpoint,
            Err(err) => {
                events.push(
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                );
                return events;
            }
        };
        let target_endpoint = match required_endpoint(&request.payload, "target") {
            Ok(endpoint) => endpoint,
            Err(err) => {
                events.push(
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                );
                return events;
            }
        };
        let options = parse_options(&request.payload);
        match (
            LiveAdapter::connect(&source_endpoint),
            LiveAdapter::connect(&target_endpoint),
        ) {
            (Ok(mut source), Ok(mut target)) => {
                let mut emit =
                    |event: Value| events.push(add_request_id(event, &request.request_id));
                let mismatches = verify_with_adapters_reporting(
                    &schema,
                    &mut source,
                    &mut target,
                    options.chunk_size,
                    &mut emit,
                )
                .expect("infallible verify reporter cannot fail");
                events.push(json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "verify",
                    "success": mismatches.is_empty(),
                    "mismatches": mismatches
                }));
            }
            (Err(err), _) | (_, Err(err)) => {
                events.push(
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                );
            }
        }
        return events;
    }

    if request.payload.get("source_data").is_some() && request.payload.get("target_data").is_some()
    {
        let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
        let mut source = MemoryAdapter::from_value(request.payload.get("source_data"));
        let mut target = MemoryAdapter::from_value(request.payload.get("target_data"));
        let mut emit = |event: Value| events.push(add_request_id(event, &request.request_id));
        let mismatches =
            verify_with_adapters_reporting(&schema, &mut source, &mut target, 1000, &mut emit)
                .expect("infallible verify reporter cannot fail");
        events.push(json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "verify",
            "success": mismatches.is_empty(),
            "mismatches": mismatches
        }));
        return events;
    }

    let source_rows = request
        .payload
        .pointer("/source_rows")
        .and_then(Value::as_array);
    let target_rows = request
        .payload
        .pointer("/target_rows")
        .and_then(Value::as_array);
    if source_rows.is_none() || target_rows.is_none() {
        events.push(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "verification requires source_rows and target_rows payloads in this helper build"
        }));
        return events;
    }
    let mismatches = compare_digest_rows(source_rows.unwrap(), target_rows.unwrap());

    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "verify",
        "success": mismatches.is_empty(),
        "mismatches": mismatches
    }));
    events
}

pub(crate) fn resume(request: &Request) -> Vec<Value> {
    let state = request
        .payload
        .get("state")
        .and_then(|value| serde_json::from_value::<ResumeState>(value.clone()).ok());
    let next_table = state.as_ref().and_then(next_table_to_copy);

    vec![
        phase_event(request, "resume", "resume state loaded"),
        json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "resume",
            "success": state.is_some(),
            "next_table": next_table
        }),
    ]
}

pub(crate) fn cleanup_streaming<F, R>(request: &Request, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut side_effect_started = false;
    emit_event(
        &mut emit,
        phase_event(request, "cleanup", "failed migration cleanup started"),
        side_effect_started,
    )?;
    let schema =
        dependency_ordered_schema(&parse_schema(&request.payload["schema"]).unwrap_or_default());
    let target_engine = read_engine(&request.payload, "target_engine");
    let mut dropped_tables = Vec::new();

    if request.payload.get("target").is_some() {
        let target_endpoint = match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
        {
            Ok(Some(endpoint)) => endpoint,
            Ok(None) => unreachable!(),
            Err(err) => {
                emit_event(
                    &mut emit,
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                    side_effect_started,
                )?;
                return Ok(());
            }
        };
        let mut target = match LiveAdapter::connect(&target_endpoint) {
            Ok(target) => target,
            Err(err) => {
                emit_event(
                    &mut emit,
                    json!({"event": "error", "request_id": request.request_id, "message": err}),
                    side_effect_started,
                )?;
                return Ok(());
            }
        };
        match cleanup_target_tables(
            &schema,
            &mut target,
            &target_endpoint.engine,
            &mut emit,
            request,
            &mut side_effect_started,
        ) {
            Ok(tables) => dropped_tables.extend(tables),
            Err(err) => match err {
                CleanupControlError::Emit(err) => return Err(err),
                CleanupControlError::Domain(err) => {
                    emit_event(
                        &mut emit,
                        json!({"event": "error", "request_id": request.request_id, "message": err}),
                        side_effect_started,
                    )?;
                    return Ok(());
                }
            },
        }
    } else {
        dropped_tables.extend(schema.tables.iter().rev().map(|table| table.name.clone()));
    }

    emit_event(
        &mut emit,
        phase_event(request, "cleanup", "failed migration cleanup completed"),
        side_effect_started,
    )?;
    emit_event(
        &mut emit,
        json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "cleanup",
        "success": true,
        "target_engine": target_engine,
        "dropped_tables": dropped_tables
        }),
        side_effect_started,
    )?;
    Ok(())
}

enum CleanupControlError {
    Emit(ProtocolEmitError),
    Domain(String),
}

fn cleanup_target_tables<F, R>(
    schema: &NormalizedSchema,
    target: &mut LiveAdapter,
    target_engine: &str,
    emit: &mut F,
    request: &Request,
    side_effect_started: &mut bool,
) -> Result<Vec<String>, CleanupControlError>
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut dropped_tables = Vec::new();
    for table in schema.tables.iter().rev() {
        emit_event(
            emit,
            json!({
            "event": "table_progress",
            "request_id": request.request_id,
            "table": table.name,
            "status": "dropping"
            }),
            *side_effect_started,
        )
        .map_err(CleanupControlError::Emit)?;
        *side_effect_started = true;
        target
            .execute_sql(&drop_table_sql(target_engine, &table.name))
            .map_err(|err| {
                CleanupControlError::Domain(format!(
                    "cleanup drop table {} failed: {err}",
                    table.name
                ))
            })?;
        dropped_tables.push(table.name.clone());
    }
    Ok(dropped_tables)
}

pub(crate) fn phase_event(request: &Request, phase: &str, message: &str) -> Value {
    json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": phase,
        "message": message
    })
}

/// Stable, machine-readable issue code for a non-empty create_only target table.
/// The PyQt UI matches on this code (never on `message`/`location` substrings) to
/// decide whether to offer the "clean up target before migrate" workflow.
const ISSUE_TARGET_NOT_EMPTY: &str = "target_not_empty";

fn target_not_empty_issue(location: String) -> MigrationIssue {
    MigrationIssue {
        issue_type: Some(ISSUE_TARGET_NOT_EMPTY.to_string()),
        severity: "error".to_string(),
        location,
        message: "target table is not empty".to_string(),
        suggestion: "Use an empty target table or run with a non-create_only mode.".to_string(),
        blocking: true,
        table_name: None,
        column_name: None,
    }
}

pub fn preflight_issues(payload: &Value) -> Vec<MigrationIssue> {
    let source = read_engine(payload, "source_engine");
    let target = read_engine(payload, "target_engine");
    let mut issues = Vec::new();

    if source.is_empty() || target.is_empty() {
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "connection".to_string(),
            message: "source_engine and target_engine are required".to_string(),
            suggestion: "Provide mysql or postgresql for both endpoints.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        });
    } else if source == target {
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "direction".to_string(),
            message: "cross-engine migration requires different source and target engines"
                .to_string(),
            suggestion: "Choose mysql -> postgresql or postgresql -> mysql.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        });
    } else if !is_supported_direction(&source, &target) {
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: "direction".to_string(),
            message: format!("unsupported direction: {source} -> {target}"),
            suggestion: "v1 supports mysql <-> postgresql only.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        });
    } else {
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "warning".to_string(),
            location: "users_grants".to_string(),
            message: "database users and grants are report-only in cross-engine v1".to_string(),
            suggestion: "Recreate users, roles, and grants manually after validating table data."
                .to_string(),
            blocking: false,
            table_name: None,
            column_name: None,
        });
    }

    for object_name in unsupported_objects(payload) {
        issues.push(MigrationIssue {
            issue_type: None,
            severity: "warning".to_string(),
            location: object_name,
            message: "object is report-only in cross-engine v1".to_string(),
            suggestion: "Review and recreate this object manually after table data is moved."
                .to_string(),
            blocking: false,
            table_name: None,
            column_name: None,
        });
    }

    let options = parse_options(payload);
    if options.mode == "create_only" {
        let target = MemoryAdapter::from_value(payload.get("target_data"));
        if let Ok(schema) = parse_schema(&payload["schema"]) {
            for table in &schema.tables {
                if target.row_count(&table.name) > 0 {
                    issues.push(target_not_empty_issue(table.name.clone()));
                }
            }
        }
    }

    issues
}

pub(crate) fn live_preflight_issues(payload: &Value) -> Vec<MigrationIssue> {
    if payload.get("target").is_none() {
        return Vec::new();
    }
    let options = parse_options(payload);
    if options.mode != "create_only" {
        return Vec::new();
    }
    let Ok(schema) = parse_schema(&payload["schema"]) else {
        return Vec::new();
    };
    let target_endpoint = match payload.get("target").map(endpoint_from_value).transpose() {
        Ok(Some(endpoint)) => endpoint,
        Ok(None) => return Vec::new(),
        Err(err) => {
            return vec![MigrationIssue {
                issue_type: None,
                severity: "error".to_string(),
                location: "target".to_string(),
                message: err,
                suggestion: "Check the target endpoint settings.".to_string(),
                blocking: true,
                table_name: None,
                column_name: None,
            }];
        }
    };
    let mut target = match LiveAdapter::connect(&target_endpoint) {
        Ok(target) => target,
        Err(err) => {
            return vec![MigrationIssue {
                issue_type: None,
                severity: "error".to_string(),
                location: "target".to_string(),
                message: err,
                suggestion: "Check the target database connection.".to_string(),
                blocking: true,
                table_name: None,
                column_name: None,
            }];
        }
    };
    if options.cleanup_before_migrate {
        return vec![MigrationIssue {
            issue_type: None,
            severity: "warning".to_string(),
            location: "target".to_string(),
            message: "target cleanup is planned before migration".to_string(),
            suggestion:
                "Review the plan and start DB migration only when target cleanup is intended."
                    .to_string(),
            blocking: false,
            table_name: None,
            column_name: None,
        }];
    }
    create_only_issues_with_adapter(&schema, &options, &mut target)
}

pub fn migrate_memory(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    resume_state: Option<&ResumeState>,
    source: &MemoryAdapter,
    target: &mut MemoryAdapter,
) -> MigrationResult {
    let mut source = source.clone();
    migrate_with_adapters(schema, options, resume_state, &mut source, target, "", "")
}

pub fn migrate_with_adapters<S: MigrationAdapter, T: MigrationAdapter>(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    resume_state: Option<&ResumeState>,
    source: &mut S,
    target: &mut T,
    source_engine: &str,
    target_engine: &str,
) -> MigrationResult {
    let mut side_effect_started = false;
    migrate_with_adapters_reporting(
        schema,
        options,
        resume_state,
        source,
        target,
        source_engine,
        target_engine,
        &mut |_| {},
        &mut side_effect_started,
    )
    .expect("infallible migration reporter cannot fail")
}

fn migrate_with_adapters_reporting<S, T, F, R>(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    resume_state: Option<&ResumeState>,
    source: &mut S,
    target: &mut T,
    source_engine: &str,
    target_engine: &str,
    on_event: &mut F,
    side_effect_started: &mut bool,
) -> Result<MigrationResult, ProtocolEmitError>
where
    S: MigrationAdapter,
    T: MigrationAdapter,
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let blocking_issues = create_only_issues_with_adapter(schema, options, target);
    if !blocking_issues.is_empty() {
        return Ok(MigrationResult {
            success: false,
            rows_copied: 0,
            chunks_copied: 0,
            state: initial_state(schema),
            issues: blocking_issues,
        });
    }

    let ordered_schema = dependency_ordered_schema(schema);
    let mut state = resume_state
        .cloned()
        .unwrap_or_else(|| initial_state(&ordered_schema));
    let mut rows_copied = 0;
    let mut chunks_copied = 0;
    let chunk_size = options.chunk_size.max(1);
    let ddl = if source_engine.is_empty() || target_engine.is_empty() {
        Vec::new()
    } else {
        match generate_schema_ddl(&ordered_schema, source_engine, target_engine) {
            Ok(ddl) => ddl,
            Err(err) => {
                let location = ordered_schema
                    .tables
                    .first()
                    .map(|table| table.name.as_str())
                    .unwrap_or("schema_ddl");
                return Ok(migration_error_result(
                    state,
                    rows_copied,
                    chunks_copied,
                    location,
                    err,
                ));
            }
        }
    };

    for (table_index, table) in ordered_schema.tables.iter().enumerate() {
        let state_index = state
            .tables
            .iter()
            .position(|candidate| candidate.table == table.name);
        let Some(state_index) = state_index else {
            continue;
        };
        if state.tables[state_index].completed {
            continue;
        }

        let table_ddl = ddl.get(table_index).map(String::as_str).unwrap_or("");
        match copy_table_rows(
            table,
            table_ddl,
            &mut state,
            state_index,
            source,
            target,
            options,
            chunk_size,
            &mut rows_copied,
            &mut chunks_copied,
            on_event,
            side_effect_started,
        ) {
            Ok(()) => {}
            Err(TableCopyControl::Error(err)) => {
                return Ok(migration_error_result(
                    state,
                    rows_copied,
                    chunks_copied,
                    &table.name,
                    err,
                ));
            }
            Err(TableCopyControl::Cancelled) => {
                return Ok(MigrationResult {
                    success: false,
                    rows_copied,
                    chunks_copied,
                    state,
                    issues: Vec::new(),
                });
            }
            Err(TableCopyControl::Emit(err)) => return Err(err),
        }
    }

    state.current_phase = "completed".to_string();
    if !target_engine.is_empty() {
        *side_effect_started = true;
    }
    if let Err(err) = apply_post_load_ddl(target, &ordered_schema, target_engine) {
        let location = ordered_schema
            .tables
            .first()
            .map(|table| table.name.as_str())
            .unwrap_or("post_data_ddl");
        return Ok(migration_error_result(
            state,
            rows_copied,
            chunks_copied,
            location,
            err,
        ));
    }
    Ok(MigrationResult {
        success: true,
        rows_copied,
        chunks_copied,
        state,
        issues: Vec::new(),
    })
}

/// 한 테이블의 복사 흐름을 나타내는 내부 제어 신호. create/read/insert 오류(Error)나
/// cancel_after_chunks 도달(Cancelled) 시 상위 함수가 최종 MigrationResult를 조립하도록 위임한다.
/// (state를 소유한 상위에서 결과를 만들어야 하므로 여기서는 MigrationResult를 직접 반환하지 않는다.)
enum TableCopyControl {
    Error(String),
    Cancelled,
    Emit(ProtocolEmitError),
}

/// create_table 후 keyset/offset 페이지네이션으로 한 테이블의 행을 청크 단위로 복사하고
/// state와 rows_copied/chunks_copied를 갱신하며 progress 이벤트를 emit한다. 정상 완료 시 Ok(()),
/// 오류/취소 시 상위가 처리하도록 Err(TableCopyControl)을 반환한다.
#[allow(clippy::too_many_arguments)]
fn copy_table_rows<S, T, F, R>(
    table: &NormalizedTable,
    table_ddl: &str,
    state: &mut ResumeState,
    state_index: usize,
    source: &mut S,
    target: &mut T,
    options: &MigrationOptions,
    chunk_size: usize,
    rows_copied: &mut u64,
    chunks_copied: &mut usize,
    on_event: &mut F,
    side_effect_started: &mut bool,
) -> Result<(), TableCopyControl>
where
    S: MigrationAdapter,
    T: MigrationAdapter,
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    *side_effect_started = true;
    if let Err(err) = target.create_table(table, table_ddl) {
        return Err(TableCopyControl::Error(err));
    }
    let total_rows = source.row_count(&table.name).ok();
    let key_columns = key_columns(table);
    let use_keyset = !key_columns.is_empty();
    let mut offset = if use_keyset {
        0
    } else {
        state.tables[state_index].rows_copied as usize
    };
    let mut last_key = if use_keyset {
        state.tables[state_index].last_key.clone()
    } else {
        None
    };
    loop {
        let rows = match if use_keyset {
            source.read_rows_after_key(table, &key_columns, last_key.as_deref(), chunk_size)
        } else {
            source.read_rows(table, offset, chunk_size)
        } {
            Ok(rows) => rows,
            Err(err) => return Err(TableCopyControl::Error(err)),
        };
        if rows.is_empty() {
            state.tables[state_index].completed = true;
            state.tables[state_index].last_key = None;
            emit_event(
                on_event,
                json!({
                "event": "table_progress",
                "table": table.name,
                "status": "completed",
                "state": &state
                }),
                *side_effect_started,
            )
            .map_err(TableCopyControl::Emit)?;
            break;
        }

        let copied_now = rows.len();
        let next_key = if use_keyset {
            rows.last().and_then(|row| row_key_token(row, &key_columns))
        } else {
            None
        };
        *side_effect_started = true;
        if let Err(err) = target.insert_rows(table, rows) {
            return Err(TableCopyControl::Error(err));
        }
        if use_keyset {
            state.tables[state_index].rows_copied += copied_now as u64;
            state.tables[state_index].last_key = next_key.clone();
            last_key = next_key;
        } else {
            offset += copied_now;
            state.tables[state_index].rows_copied = offset as u64;
            state.tables[state_index].last_key = Some(offset.to_string());
        }
        *rows_copied += copied_now as u64;
        *chunks_copied += 1;
        emit_event(
            on_event,
            json!({
            "event": "row_progress",
            "table": table.name,
            "rows": state.tables[state_index].rows_copied,
            "total": total_rows,
            "state": &state
            }),
            *side_effect_started,
        )
        .map_err(TableCopyControl::Emit)?;

        if options
            .cancel_after_chunks
            .is_some_and(|limit| *chunks_copied >= limit)
        {
            return Err(TableCopyControl::Cancelled);
        }
    }
    Ok(())
}

fn migration_error_result(
    state: ResumeState,
    rows_copied: u64,
    chunks_copied: usize,
    location: &str,
    err: String,
) -> MigrationResult {
    MigrationResult {
        success: false,
        rows_copied,
        chunks_copied,
        state,
        issues: vec![MigrationIssue {
            issue_type: None,
            severity: "error".to_string(),
            location: location.to_string(),
            message: err,
            suggestion: "Resolve the database error and resume the migration.".to_string(),
            blocking: true,
            table_name: None,
            column_name: None,
        }],
    }
}

fn create_only_issues_with_adapter<T: MigrationAdapter>(
    schema: &NormalizedSchema,
    options: &MigrationOptions,
    target: &mut T,
) -> Vec<MigrationIssue> {
    if options.mode != "create_only" {
        return Vec::new();
    }
    let mut issues = Vec::new();
    for table in &schema.tables {
        match target.row_count(&table.name) {
            Ok(count) if count > 0 => issues.push(target_not_empty_issue(table.name.clone())),
            Err(err) => issues.push(MigrationIssue {
                issue_type: None,
                severity: "error".to_string(),
                location: table.name.clone(),
                message: err,
                suggestion: "Check target connectivity and permissions.".to_string(),
                blocking: true,
                table_name: None,
                column_name: None,
            }),
            _ => {}
        }
    }
    issues
}

pub fn verify_memory(
    schema: &NormalizedSchema,
    source: &MemoryAdapter,
    target: &MemoryAdapter,
) -> Vec<Value> {
    let mut mismatches = Vec::new();
    for table in &schema.tables {
        let source_rows = source.rows.get(&table.name).cloned().unwrap_or_default();
        let target_rows = target.rows.get(&table.name).cloned().unwrap_or_default();

        if source_rows.len() != target_rows.len() {
            mismatches.push(json!({
                "table": table.name,
                "kind": "count",
                "source_count": source_rows.len(),
                "target_count": target_rows.len()
            }));
        }

        let key_columns = key_columns(table);
        if key_columns.is_empty() {
            for mismatch in compare_typed_digest_rows(table, &source_rows, &target_rows) {
                mismatches.push(with_table(&table.name, mismatch));
            }
        } else {
            mismatches.extend(compare_typed_ordered_keyed_rows(
                table,
                &key_columns,
                &source_rows,
                &target_rows,
            ));
        }
    }
    mismatches
}

pub fn verify_with_adapters<S: MigrationAdapter, T: MigrationAdapter>(
    schema: &NormalizedSchema,
    source: &mut S,
    target: &mut T,
    chunk_size: usize,
) -> Vec<Value> {
    let mut emit = |_event: Value| {};
    verify_with_adapters_reporting(schema, source, target, chunk_size, &mut emit)
        .expect("infallible verify reporter cannot fail")
}

fn verify_with_adapters_reporting<S, T, F, R>(
    schema: &NormalizedSchema,
    source: &mut S,
    target: &mut T,
    chunk_size: usize,
    emit: &mut F,
) -> Result<Vec<Value>, ProtocolEmitError>
where
    S: MigrationAdapter,
    T: MigrationAdapter,
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut mismatches = Vec::new();
    let chunk_size = chunk_size.max(1);
    for table in &schema.tables {
        emit_event(
            emit,
            json!({
            "event": "table_progress",
            "table": table.name,
            "status": "verifying"
            }),
            false,
        )?;
        let source_count = match source.row_count(&table.name) {
            Ok(count) => count,
            Err(err) => {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "error",
                    "side": "source",
                    "message": err
                }));
                continue;
            }
        };
        let target_count = match target.row_count(&table.name) {
            Ok(count) => count,
            Err(err) => {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "error",
                    "side": "target",
                    "message": err
                }));
                continue;
            }
        };
        let total_rows = source_count.max(target_count);
        emit_event(
            emit,
            json!({
            "event": "row_progress",
            "table": table.name,
            "rows": 0,
            "total": total_rows
            }),
            false,
        )?;
        if source_count != target_count {
            mismatches.push(json!({
                "table": table.name,
                "kind": "count",
                "source_count": source_count,
                "target_count": target_count
            }));
        }

        let key_columns = key_columns(table);
        let table_mismatches = if key_columns.is_empty() {
            verify_table_by_digest(source, target, table, chunk_size, total_rows, emit)?
        } else {
            verify_table_by_keyset(
                source,
                target,
                table,
                &key_columns,
                chunk_size,
                total_rows,
                emit,
            )?
        };
        mismatches.extend(table_mismatches);
    }
    Ok(mismatches)
}

/// key column이 없는 테이블을 digest 카운트 비교로 검증한다. source/target의 행 다이제스트
/// 빈도를 비교해 불일치를 만들고, 완료 시 row_progress(total) + table_progress(completed)를 emit한다.
/// 카운트 수집 오류가 나면 그 오류만 담아 반환하고 완료 이벤트는 emit하지 않는다.
fn verify_table_by_digest<S, T, F, R>(
    source: &mut S,
    target: &mut T,
    table: &NormalizedTable,
    chunk_size: usize,
    total_rows: usize,
    emit: &mut F,
) -> Result<Vec<Value>, ProtocolEmitError>
where
    S: MigrationAdapter,
    T: MigrationAdapter,
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut mismatches = Vec::new();
    let source_counts = match digest_counts_for_adapter(source, table, chunk_size) {
        Ok(counts) => counts,
        Err(err) => {
            mismatches.push(json!({
                "table": table.name,
                "kind": "error",
                "side": "source",
                "message": err
            }));
            return Ok(mismatches);
        }
    };
    let target_counts = match digest_counts_for_adapter(target, table, chunk_size) {
        Ok(counts) => counts,
        Err(err) => {
            mismatches.push(json!({
                "table": table.name,
                "kind": "error",
                "side": "target",
                "message": err
            }));
            return Ok(mismatches);
        }
    };
    for mismatch in compare_digest_counts(&source_counts, &target_counts) {
        mismatches.push(with_table(&table.name, mismatch));
    }
    emit_event(
        emit,
        json!({
        "event": "row_progress",
        "table": table.name,
        "rows": total_rows,
        "total": total_rows
        }),
        false,
    )?;
    emit_event(
        emit,
        json!({
        "event": "table_progress",
        "table": table.name,
        "status": "completed"
        }),
        false,
    )?;
    Ok(mismatches)
}

/// key column이 있는 테이블을 keyset 페이지네이션으로 행 단위 비교한다. 청크마다 양측을
/// 읽어 typed 비교하고 row_progress를 emit하며, 마지막에 table_progress(completed)를 emit한다.
/// 읽기 오류가 나면 그 오류를 담고 루프를 종료한다(완료 이벤트는 그대로 emit).
fn verify_table_by_keyset<S, T, F, R>(
    source: &mut S,
    target: &mut T,
    table: &NormalizedTable,
    key_columns: &[String],
    chunk_size: usize,
    total_rows: usize,
    emit: &mut F,
) -> Result<Vec<Value>, ProtocolEmitError>
where
    S: MigrationAdapter,
    T: MigrationAdapter,
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let mut mismatches = Vec::new();
    let mut verified_rows = 0usize;
    let mut last_key: Option<String> = None;
    loop {
        let source_rows = match source.read_rows_after_key(
            table,
            key_columns,
            last_key.as_deref(),
            chunk_size,
        ) {
            Ok(rows) => rows,
            Err(err) => {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "error",
                    "side": "source",
                    "message": err
                }));
                break;
            }
        };
        let target_rows = match target.read_rows_after_key(
            table,
            key_columns,
            last_key.as_deref(),
            chunk_size,
        ) {
            Ok(rows) => rows,
            Err(err) => {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "error",
                    "side": "target",
                    "message": err
                }));
                break;
            }
        };
        if source_rows.is_empty() && target_rows.is_empty() {
            break;
        }
        mismatches.extend(compare_typed_keyed_rows(
            table,
            key_columns,
            &source_rows,
            &target_rows,
        ));
        verified_rows += source_rows.len().max(target_rows.len());
        emit_event(
            emit,
            json!({
            "event": "row_progress",
            "table": table.name,
            "rows": verified_rows.min(total_rows),
            "total": total_rows
            }),
            false,
        )?;
        let next_key = source_rows
            .last()
            .or_else(|| target_rows.last())
            .and_then(|row| row_key_token(row, key_columns));
        if next_key.is_none() || next_key == last_key {
            break;
        }
        last_key = next_key;
    }
    emit_event(
        emit,
        json!({
        "event": "table_progress",
        "table": table.name,
        "status": "completed"
        }),
        false,
    )?;
    Ok(mismatches)
}

fn digest_counts_for_adapter<A: MigrationAdapter>(
    adapter: &mut A,
    table: &NormalizedTable,
    chunk_size: usize,
) -> Result<BTreeMap<String, u64>, String> {
    let mut counts = BTreeMap::new();
    let mut offset = 0;
    loop {
        let rows = adapter.read_rows(table, offset, chunk_size)?;
        if rows.is_empty() {
            break;
        }
        for row in normalize_rows_for_table(table, &rows) {
            if let Value::Object(object) = row {
                *counts.entry(row_digest(&object)).or_insert(0) += 1;
            }
        }
        offset += rows.len();
    }
    Ok(counts)
}

fn with_table(table: &str, mismatch: Value) -> Value {
    let mut object = mismatch.as_object().cloned().unwrap_or_default();
    object.insert("table".to_string(), json!(table));
    object.insert("kind".to_string(), json!("digest"));
    Value::Object(object)
}

pub fn compare_keyed_rows(
    table: &str,
    key_columns: &[String],
    source_rows: &[Value],
    target_rows: &[Value],
) -> Vec<Value> {
    let source_index = keyed_index(key_columns, source_rows);
    let target_index = keyed_index(key_columns, target_rows);
    let mut mismatches = Vec::new();

    for (key, source_row) in &source_index {
        let Some(target_row) = target_index.get(key) else {
            mismatches.push(json!({
                "table": table,
                "kind": "missing_target",
                "key": key
            }));
            continue;
        };
        let source_object = source_row.as_object().cloned().unwrap_or_default();
        let target_object = target_row.as_object().cloned().unwrap_or_default();
        let mut columns = BTreeSet::new();
        columns.extend(source_object.keys().cloned());
        columns.extend(target_object.keys().cloned());

        for column in columns {
            let left = source_object.get(&column).unwrap_or(&Value::Null);
            let right = target_object.get(&column).unwrap_or(&Value::Null);
            if canonical_value(left) != canonical_value(right) {
                mismatches.push(json!({
                    "table": table,
                    "kind": "cell",
                    "key": key,
                    "column": column,
                    "source": left,
                    "target": right
                }));
            }
        }
    }
    for key in target_index.keys() {
        if !source_index.contains_key(key) {
            mismatches.push(json!({
                "table": table,
                "kind": "extra_target",
                "key": key
            }));
        }
    }
    mismatches
}

fn compare_digest_counts(
    source_counts: &BTreeMap<String, u64>,
    target_counts: &BTreeMap<String, u64>,
) -> Vec<Value> {
    let mut mismatches = Vec::new();

    for (digest, source_count) in source_counts {
        let target_count = target_counts.get(digest).copied().unwrap_or(0);
        if *source_count != target_count {
            mismatches.push(json!({
                "digest": digest,
                "source_count": source_count,
                "target_count": target_count
            }));
        }
    }
    for (digest, target_count) in target_counts {
        if !source_counts.contains_key(digest) {
            mismatches.push(json!({
                "digest": digest,
                "source_count": 0,
                "target_count": target_count
            }));
        }
    }

    mismatches
}

pub fn compare_typed_keyed_rows(
    table: &NormalizedTable,
    key_columns: &[String],
    source_rows: &[Value],
    target_rows: &[Value],
) -> Vec<Value> {
    let source_rows = normalize_rows_for_table(table, source_rows);
    let target_rows = normalize_rows_for_table(table, target_rows);
    compare_keyed_rows(&table.name, key_columns, &source_rows, &target_rows)
}

fn compare_typed_ordered_keyed_rows(
    table: &NormalizedTable,
    key_columns: &[String],
    source_rows: &[Value],
    target_rows: &[Value],
) -> Vec<Value> {
    if source_rows.len() != target_rows.len() {
        return compare_typed_keyed_rows(table, key_columns, source_rows, target_rows);
    }

    let mut mismatches = Vec::new();
    for (source_row, target_row) in source_rows.iter().zip(target_rows) {
        let source_row = normalize_row_for_table(table, source_row);
        let target_row = normalize_row_for_table(table, target_row);
        let source_key = row_key_token(&source_row, key_columns);
        let target_key = row_key_token(&target_row, key_columns);
        if source_key != target_key {
            return compare_typed_keyed_rows(table, key_columns, source_rows, target_rows);
        }
        let source_object = source_row.as_object().cloned().unwrap_or_default();
        let target_object = target_row.as_object().cloned().unwrap_or_default();
        let key = source_key.unwrap_or_default();
        for column in &table.columns {
            let left = source_object.get(&column.name).unwrap_or(&Value::Null);
            let right = target_object.get(&column.name).unwrap_or(&Value::Null);
            if canonical_value(left) != canonical_value(right) {
                mismatches.push(json!({
                    "table": table.name,
                    "kind": "cell",
                    "key": key,
                    "column": column.name,
                    "source": left,
                    "target": right
                }));
            }
        }
    }
    mismatches
}

fn keyed_index(key_columns: &[String], rows: &[Value]) -> BTreeMap<String, Value> {
    let mut index = BTreeMap::new();
    for row in rows {
        if let Value::Object(object) = row {
            let key = key_columns
                .iter()
                .map(|column| canonical_value(object.get(column).unwrap_or(&Value::Null)))
                .collect::<Vec<_>>()
                .join("|");
            index.insert(key, row.clone());
        }
    }
    index
}

pub fn compare_typed_digest_rows(
    table: &NormalizedTable,
    source: &[Value],
    target: &[Value],
) -> Vec<Value> {
    let source = normalize_rows_for_table(table, source);
    let target = normalize_rows_for_table(table, target);
    compare_digest_rows(&source, &target)
}

#[cfg(test)]
mod tests {
    use super::*;
    
    use serde_json::{json, Value};
    
    use crate::adapters::test_support::{empty_table, fk, schema, TrackingAdapter};

    #[test]
    fn migration_plan_alias_preserves_service_command_name() {
        let result = handle_request(Request {
            command: "migration.plan".to_string(),
            request_id: Some("plan-1".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": schema()
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["command"], "migration.plan");
        assert_eq!(result["success"], true);
        assert_eq!(result["plan"]["table_order"], json!(["users"]));
        assert_eq!(result["plan"]["tables"][0]["name"], "users");
    }

    #[test]
    fn migration_plan_reports_tables_and_estimated_rows() {
        let result = handle_request(Request {
            command: "plan".to_string(),
            request_id: Some("plan-rows".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }, {
                        "name": "orders",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }]
                },
                "source_data": {
                    "users": [{"id": 1}, {"id": 2}],
                    "orders": [{"id": 10}]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(
            result["plan"]["tables"],
            json!([
                {"name": "users", "estimated_rows": 2},
                {"name": "orders", "estimated_rows": 1}
            ])
        );
    }

    #[test]
    fn migration_plan_reports_fk_dependency_order() {
        let result = handle_request(Request {
            command: "migration.plan".to_string(),
            request_id: Some("plan-fk".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {
                    "tables": [{
                        "name": "orders",
                        "foreign_keys": [{
                            "name": "fk_orders_users",
                            "columns": ["user_id"],
                            "referenced_table": "users",
                            "referenced_columns": ["id"]
                        }]
                    }, {
                        "name": "users"
                    }]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["plan"]["table_order"], json!(["users", "orders"]));
    }

    #[test]
    fn create_only_blocks_non_empty_target() {
        let source = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 1}]})));
        let mut target = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 9}]})));
        let result = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(!result.success);
        assert_eq!(result.rows_copied, 0);
        assert!(result
            .issues
            .iter()
            .any(|issue| issue.blocking && issue.issue_type.as_deref() == Some("target_not_empty")));
        assert_eq!(target.row_count("users"), 1);
    }

    #[test]
    fn create_only_still_requires_empty_target_without_live_cleanup() {
        let source = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 1}]})));
        let mut target = MemoryAdapter::from_value(Some(&json!({"users": [{"id": 9}]})));
        let result = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: true,
            },
            None,
            &source,
            &mut target,
        );

        assert!(!result.success);
        assert_eq!(result.rows_copied, 0);
        assert!(result
            .issues
            .iter()
            .any(|issue| issue.blocking && issue.issue_type.as_deref() == Some("target_not_empty")));
        assert_eq!(target.row_count("users"), 1);
    }

    #[test]
    fn migrates_rows_in_chunks() {
        let source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c"}]
        })));
        let mut target = MemoryAdapter::default();
        let result = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(result.success);
        assert_eq!(result.rows_copied, 3);
        assert_eq!(result.chunks_copied, 2);
        assert_eq!(target.row_count("users"), 3);
        assert!(result.state.tables[0].completed);
    }

    #[test]
    fn migration_creates_and_copies_parent_tables_before_children() {
        let schema = NormalizedSchema {
            tables: vec![
                empty_table("orders", vec![fk("fk_orders_users", "users")]),
                empty_table("users", Vec::new()),
            ],
        };
        let source = MemoryAdapter::from_value(Some(&json!({
            "orders": [{"id": 10, "parent_id": 1}],
            "users": [{"id": 1}]
        })));
        let mut target = MemoryAdapter::default();

        let result = migrate_memory(
            &schema,
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 100,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(result.success);
        assert_eq!(target.created_tables, vec!["users", "orders"]);
        assert_eq!(
            result
                .state
                .tables
                .iter()
                .map(|table| table.table.as_str())
                .collect::<Vec<_>>(),
            vec!["users", "orders"]
        );
    }

    #[test]
    fn resumes_after_partial_copy() {
        let source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1}, {"id": 2}, {"id": 3}]
        })));
        let mut target = MemoryAdapter::default();
        let first = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 2,
                cancel_after_chunks: Some(1),
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );
        assert!(!first.success);
        assert_eq!(target.row_count("users"), 2);

        let second = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            Some(&first.state),
            &source,
            &mut target,
        );
        assert!(second.success);
        assert_eq!(second.rows_copied, 1);
        assert_eq!(target.row_count("users"), 3);
    }

    #[test]
    fn resumes_large_stream_after_multiple_chunks() {
        let rows = (1..=12_037)
            .map(|id| json!({"id": id, "name": format!("user-{id}")}))
            .collect::<Vec<_>>();
        let mut source = MemoryAdapter::default();
        source.insert_rows("users", rows);
        let mut target = MemoryAdapter::default();

        let first = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "create_only".to_string(),
                chunk_size: 5_000,
                cancel_after_chunks: Some(2),
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );
        assert!(!first.success);
        assert_eq!(first.rows_copied, 10_000);
        assert_eq!(target.row_count("users"), 10_000);
        assert_eq!(first.state.tables[0].rows_copied, 10_000);

        let second = migrate_memory(
            &schema(),
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 5_000,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            Some(&first.state),
            &source,
            &mut target,
        );

        assert!(second.success);
        assert_eq!(second.rows_copied, 2_037);
        assert_eq!(second.chunks_copied, 1);
        assert_eq!(target.row_count("users"), 12_037);
        assert!(second.state.tables[0].completed);
    }

    #[test]
    fn keyed_compare_reports_cell_mismatch() {
        let mismatches = compare_keyed_rows(
            "users",
            &["id".to_string()],
            &[json!({"id": 1, "name": "source"})],
            &[json!({"id": 1, "name": "target"})],
        );
        assert_eq!(mismatches.len(), 1);
        assert_eq!(mismatches[0]["kind"], "cell");
        assert_eq!(mismatches[0]["column"], "name");
    }

    #[test]
    fn verify_with_adapters_reports_keyed_mismatch() {
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "source"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "target"}]
        })));

        let mismatches = verify_with_adapters(&schema(), &mut source, &mut target, 1);

        assert_eq!(mismatches.len(), 1);
        assert_eq!(mismatches[0]["kind"], "cell");
        assert_eq!(mismatches[0]["column"], "name");
    }

    #[test]
    fn verify_with_adapters_reports_count_mismatch() {
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": 1, "name": "a"}]
        })));

        let mismatches = verify_with_adapters(&schema(), &mut source, &mut target, 1);

        assert!(mismatches
            .iter()
            .any(|mismatch| mismatch["kind"] == "count"));
    }

    #[test]
    fn verify_with_adapters_reads_keyed_tables_in_chunks() {
        let rows: Vec<Value> = (0..5)
            .map(|id| json!({"id": id.to_string(), "name": format!("user-{id}")}))
            .collect();
        let mut source = TrackingAdapter {
            rows: rows.clone(),
            ..Default::default()
        };
        let mut target = TrackingAdapter {
            rows,
            ..Default::default()
        };

        let mismatches = verify_with_adapters(&schema(), &mut source, &mut target, 2);

        assert!(mismatches.is_empty());
        assert!(source.read_limits.is_empty());
        assert!(target.read_limits.is_empty());
        assert!(source.read_after_limits.len() > 2);
        assert!(source.read_after_limits.iter().all(|limit| *limit == 2));
        assert!(target.read_after_limits.iter().all(|limit| *limit == 2));
        assert!(source.max_returned <= 2);
        assert!(target.max_returned <= 2);
    }

    #[test]
    fn verify_command_emits_progress_before_result() {
        let events = handle_request(Request {
            command: "verify".to_string(),
            request_id: Some("verify-progress".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": schema(),
                "source_data": {"users": [{"id": 1, "name": "a"}]},
                "target_data": {"users": [{"id": 1, "name": "a"}]},
            }),
        });

        let table_progress = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("table_progress")))
            .unwrap();
        let row_progress = events
            .iter()
            .rposition(|event| event.get("event") == Some(&json!("row_progress")))
            .unwrap();
        let result = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert!(table_progress < result);
        assert!(row_progress < result);
        assert_eq!(events[row_progress]["table"], "users");
        assert_eq!(events[row_progress]["rows"], 1);
        assert_eq!(events[row_progress]["total"], 1);
        assert_eq!(events[row_progress]["request_id"], "verify-progress");
    }

    #[test]
    fn resumes_composite_key_tables_without_offset_state() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "items".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "tenant_id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "name".to_string(),
                        type_name: "varchar(32)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let source = MemoryAdapter::from_value(Some(&json!({
            "items": [
                {"tenant_id": "1", "id": "1", "name": "a"},
                {"tenant_id": "1", "id": "2", "name": "b"},
                {"tenant_id": "2", "id": "1", "name": "c"}
            ]
        })));
        let mut target = MemoryAdapter::default();
        let first = migrate_memory(
            &schema,
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 2,
                cancel_after_chunks: Some(1),
                cleanup_before_migrate: false,
            },
            None,
            &source,
            &mut target,
        );

        assert!(!first.success);
        assert_eq!(first.state.tables[0].rows_copied, 2);
        assert_eq!(
            first.state.tables[0].last_key.as_deref(),
            Some("[\"1\",\"2\"]")
        );

        let second = migrate_memory(
            &schema,
            &MigrationOptions {
                mode: "append".to_string(),
                chunk_size: 2,
                cancel_after_chunks: None,
                cleanup_before_migrate: false,
            },
            Some(&first.state),
            &source,
            &mut target,
        );

        assert!(second.success);
        assert_eq!(target.row_count("items"), 3);
        assert!(verify_memory(&schema, &source, &target).is_empty());
    }

    #[test]
    fn typed_verify_treats_boolean_text_equivalents_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "flags".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "enabled".to_string(),
                        type_name: "tinyint(1)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "flags": [{"id": 1, "enabled": "1"}, {"id": 2, "enabled": "0"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "flags": [{"id": 1, "enabled": "true"}, {"id": 2, "enabled": "false"}]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
    }

    #[test]
    fn typed_verify_treats_temporal_text_equivalents_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "events".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "event_date".to_string(),
                        type_name: "date".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "event_time".to_string(),
                        type_name: "time".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "created_at".to_string(),
                        type_name: "datetime".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "events": [{
                "id": 1,
                "event_date": "2026-05-14",
                "event_time": "09:08:07.000000",
                "created_at": "2026-05-14 09:08:07.000000"
            }]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "events": [{
                "id": 1,
                "event_date": "2026-05-14",
                "event_time": "09:08:07",
                "created_at": "2026-05-14T09:08:07"
            }]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
    }

    #[test]
    fn typed_verify_treats_decimal_text_equivalents_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "ledger".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "amount".to_string(),
                        type_name: "decimal(12,4)".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "ledger": [{"id": 1, "amount": "001.2300"}, {"id": 2, "amount": "-0.0000"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "ledger": [{"id": 1, "amount": "1.23"}, {"id": 2, "amount": "0"}]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
    }

    #[test]
    fn typed_verify_treats_postgresql_nul_sanitized_text_as_equal() {
        let schema = NormalizedSchema {
            tables: vec![NormalizedTable {
                name: "logs".to_string(),
                columns: vec![
                    NormalizedColumn {
                        name: "id".to_string(),
                        type_name: "int".to_string(),
                        default_value: None,
                        nullable: false,
                        primary_key: true,
                        unique: false,
                    },
                    NormalizedColumn {
                        name: "message".to_string(),
                        type_name: "text".to_string(),
                        default_value: None,
                        nullable: true,
                        primary_key: false,
                        unique: false,
                    },
                ],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
                table_collation: None,
            }],
        };
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "logs": [{"id": 1, "message": "before\0after"}]
        })));
        let mut target = MemoryAdapter::from_value(Some(&json!({
            "logs": [{"id": 1, "message": "beforeafter"}]
        })));

        let mismatches = verify_with_adapters(&schema, &mut source, &mut target, 10);

        assert_eq!(mismatches, Vec::<Value>::new());
    }

    #[test]
    fn preflight_reports_stable_target_not_empty_issue_type_for_non_empty_target() {
        let issues = preflight_issues(&json!({
            "source_engine": "mysql",
            "target_engine": "postgresql",
            "schema": {
                "tables": [{
                    "name": "users",
                    "columns": [
                        {"name": "id", "type": "int(11)", "nullable": false, "primary_key": true}
                    ]
                }]
            },
            "execution_options": {"mode": "create_only"},
            "target_data": {"users": [{"id": 1}]}
        }));

        let target_issue = issues
            .iter()
            .find(|issue| issue.location == "users" && issue.blocking)
            .expect("expected a blocking issue for the non-empty target table");
        assert_eq!(target_issue.issue_type.as_deref(), Some("target_not_empty"));
    }

    #[test]
    fn readiness_rejects_same_engine_endpoints() {
        let events = handle_request(Request {
            command: "readiness".to_string(),
            request_id: Some("ready-1".to_string()),
            payload: json!({
                "source": {
                    "engine": "mysql",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "root",
                    "password": "",
                    "database": "app"
                },
                "target": {
                    "engine": "mysql",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "root",
                    "password": "",
                    "database": "app2"
                }
            }),
        });

        assert!(events.iter().any(|event| {
            event.get("event") == Some(&json!("error"))
                && event["message"]
                    .as_str()
                    .unwrap_or("")
                    .contains("one MySQL endpoint and one PostgreSQL")
        }));
    }

    #[test]
    fn table_guide_includes_row_values_and_insert_sql() {
        let mut source = MemoryAdapter::from_value(Some(&json!({
            "users": [{"id": "1", "name": "alpha"}]
        })));
        let mut issues = Vec::new();

        let guides = build_table_guides(
            &schema(),
            &mut source,
            "mysql",
            "postgresql",
            5,
            &mut issues,
        );

        assert!(issues.is_empty());
        assert_eq!(guides[0]["table"], "users");
        assert_eq!(guides[0]["row_samples"][0]["name"], "alpha");
        assert_eq!(
            guides[0]["insert_example_sql"],
            "INSERT INTO \"users\" (\"id\", \"name\") VALUES ('1', 'alpha')"
        );
        assert_eq!(guides[0]["columns"][0]["target_type"], "INTEGER");
    }

    #[test]
    fn migrate_streaming_emit_failure_stops_before_followup_or_side_effect() {
        let request = Request {
            command: "migrate".to_string(),
            request_id: Some("migrate-emit-failure".to_string()),
            payload: json!({}),
        };
        let mut calls = 0;

        let error = migrate_streaming(&request, |_event| {
            calls += 1;
            Err(ProtocolEmitError::io("broken migrate emitter"))
        })
        .expect_err("migrate emitter failure must propagate");

        assert_eq!(calls, 1);
        assert!(!error.side_effect_started());
    }

    #[test]
    fn migrate_emit_failure_after_side_effect_is_marked_indeterminate() {
        let error = emit_event(
            &mut |_event| Err(ProtocolEmitError::io("broken migrate emitter")),
            json!({"event": "result"}),
            true,
        )
        .expect_err("post-side-effect failure must propagate");

        assert!(error.side_effect_started());
    }
}
