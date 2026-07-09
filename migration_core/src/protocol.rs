use serde_json::{json, Value};
use std::collections::BTreeMap;

use crate::*;

pub struct CoreService {
    connections: BTreeMap<String, LiveAdapter>,
    next_connection_sequence: u64,
}

impl CoreService {
    pub fn new() -> Self {
        Self {
            connections: BTreeMap::new(),
            next_connection_sequence: 1,
        }
    }

    pub fn handle_request_streaming<F: FnMut(Value)>(&mut self, request: Request, emit: F) {
        match request.command.as_str() {
            "connection.open" => emit_all_events(self.connection_open(&request), emit),
            "connection.close" => emit_all_events(self.connection_close(&request), emit),
            "query.execute" => emit_all_events(self.query_execute(&request), emit),
            "service.shutdown" => {
                self.connections.clear();
                emit_all_events(service_shutdown(&request), emit);
            }
            _ => handle_request_streaming(request, emit),
        }
    }

    fn connection_open(&mut self, request: &Request) -> Vec<Value> {
        let endpoint = match request_endpoint(request) {
            Ok(endpoint) => endpoint,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        };
        match LiveAdapter::connect(&endpoint) {
            Ok(adapter) => {
                let id = unique_connection_id(&endpoint, self.next_connection_sequence);
                self.next_connection_sequence = self.next_connection_sequence.saturating_add(1);
                self.connections.insert(id.clone(), adapter);
                vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "connection.open",
                    "success": true,
                    "connection_id": id,
                    "engine": endpoint.engine
                })]
            }
            Err(err) => vec![json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "connection.open",
                "success": false,
                "engine": endpoint.engine,
                "message": redact_endpoint_secret(&err, &endpoint)
            })],
        }
    }

    fn connection_close(&mut self, request: &Request) -> Vec<Value> {
        let connection_id = request
            .payload
            .get("connection_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let removed = self.connections.remove(connection_id).is_some();
        vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.close",
            "success": true,
            "closed": removed,
            "connection_id": connection_id
        })]
    }

    fn query_execute(&mut self, request: &Request) -> Vec<Value> {
        if let Some(connection_id) = request.payload.get("connection_id").and_then(Value::as_str) {
            let sql = request
                .payload
                .get("sql")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if sql.is_empty() {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": "query.execute requires sql"
                })];
            }
            let Some(adapter) = self.connections.get_mut(connection_id) else {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": format!("unknown connection_id: {connection_id}")
                })];
            };
            let params = query_params(&request.payload);
            let bound_sql = bind_query_params(sql, &params);
            return match execute_query_adapter(adapter, &bound_sql) {
                Ok(result) => query_result_events(request, result),
                Err(err) => vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })],
            };
        }
        query_execute(request)
    }
}

impl Default for CoreService {
    fn default() -> Self {
        Self::new()
    }
}

pub fn handle_line(line: &str) -> Vec<Value> {
    match serde_json::from_str::<Request>(line) {
        Ok(request) => handle_request(request),
        Err(err) => vec![json!({
            "event": "error",
            "message": format!("invalid request JSON: {err}")
        })],
    }
}

pub fn handle_line_streaming<F: FnMut(Value)>(line: &str, mut emit: F) {
    match serde_json::from_str::<Request>(line) {
        Ok(request) => handle_request_streaming(request, emit),
        Err(err) => emit(json!({
            "event": "error",
            "message": format!("invalid request JSON: {err}")
        })),
    }
}

pub fn handle_request(request: Request) -> Vec<Value> {
    let mut events = Vec::new();
    handle_request_streaming(request, |event| events.push(event));
    events
}

pub fn handle_request_streaming<F: FnMut(Value)>(request: Request, mut emit: F) {
    match request.command.as_str() {
        "service.hello" => emit_all_events(service_hello(&request), emit),
        "service.shutdown" => emit_all_events(service_shutdown(&request), emit),
        "connection.test" => emit_all_events(connection_test(&request), emit),
        "connection.open" => emit_all_events(connection_open(&request), emit),
        "connection.close" => emit_all_events(connection_close(&request), emit),
        "schema.list" => emit_all_events(schema_list(&request), emit),
        "schema.inspect" => emit_all_events(alias_events(&request, "inspect"), emit),
        "schema.diff" => emit_all_events(schema_diff(&request), emit),
        "query.execute" => emit_all_events(query_execute(&request), emit),
        "query.cancel" => emit_all_events(query_cancel(&request), emit),
        "dump.run" => dump_run_streaming(&request, emit),
        "dump.import" => dump_import_streaming(&request, emit),
        "migration.plan" => emit_all_events(alias_events(&request, "plan"), emit),
        "migration.verify" => emit_all_events(alias_events(&request, "verify"), emit),
        "migration.resume" => emit_all_events(alias_events(&request, "resume"), emit),
        "migration.cleanup" => {
            let alias = Request {
                command: "cleanup".to_string(),
                request_id: request.request_id.clone(),
                payload: request.payload.clone(),
            };
            let command = request.command.clone();
            cleanup_streaming(&alias, |event| {
                emit(rewrite_result_command(event, &command))
            });
        }
        "migration.run" => {
            let alias = Request {
                command: "migrate".to_string(),
                request_id: request.request_id.clone(),
                payload: request.payload.clone(),
            };
            let command = request.command.clone();
            migrate_streaming(&alias, |event| {
                emit(rewrite_result_command(event, &command))
            });
        }
        "oneclick.run" => oneclick_run_streaming(&request, emit),
        "oneclick.preflight" => emit_all_events(oneclick_preflight(&request), emit),
        "oneclick.analyze" => emit_all_events(oneclick_analyze(&request), emit),
        "oneclick.recommend" => emit_all_events(oneclick_recommend(&request), emit),
        "oneclick.derive_charset_contracts" => {
            emit_all_events(oneclick_derive_charset_contracts(&request), emit)
        }
        "oneclick.apply_fixes" => emit_all_events(oneclick_apply_fixes(&request), emit),
        "oneclick.validate" => emit_all_events(oneclick_validate(&request), emit),
        "oneclick.report" => emit_all_events(oneclick_report(&request), emit),
        "job.cancel" => emit_all_events(job_cancel(&request), emit),
        "inspect" => emit_all_events(inspect(&request), emit),
        "preflight" => preflight_streaming(&request, emit),
        "readiness" => emit_all_events(readiness(&request), emit),
        "guide" => emit_all_events(guide(&request), emit),
        "plan" => emit_all_events(plan(&request), emit),
        "migrate" => migrate_streaming(&request, emit),
        "verify" => emit_all_events(verify(&request), emit),
        "resume" => emit_all_events(resume(&request), emit),
        "cleanup" => cleanup_streaming(&request, emit),
        other => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": format!("unknown command: {other}")
        })),
    }
}

fn emit_all_events<F: FnMut(Value)>(events: Vec<Value>, mut emit: F) {
    for event in events {
        emit(event);
    }
}

fn alias_events(request: &Request, legacy_command: &str) -> Vec<Value> {
    let legacy = Request {
        command: legacy_command.to_string(),
        request_id: request.request_id.clone(),
        payload: request.payload.clone(),
    };
    handle_request(legacy)
        .into_iter()
        .map(|event| rewrite_result_command(event, &request.command))
        .collect()
}

fn rewrite_result_command(mut event: Value, command: &str) -> Value {
    if event.get("event") == Some(&json!("result")) {
        if let Value::Object(object) = &mut event {
            object.insert("command".to_string(), Value::String(command.to_string()));
        }
    }
    event
}

fn service_hello(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "service.hello",
        "success": true,
        "service": "tunnelforge-core",
        "protocol_version": 1,
        "capabilities": [
            "connection.open",
            "connection.close",
            "connection.test",
            "schema.list",
            "schema.inspect",
            "schema.diff",
            "query.execute",
            "query.cancel",
            "dump.run",
            "dump.import",
            "migration.plan",
            "migration.run",
            "migration.verify",
            "migration.resume",
            "oneclick.run",
            "oneclick.preflight",
            "oneclick.analyze",
            "oneclick.recommend",
            "oneclick.derive_charset_contracts",
            "oneclick.apply_fixes",
            "oneclick.validate",
            "oneclick.report",
            "job.cancel",
            "service.shutdown"
        ]
    })]
}

fn service_shutdown(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "service.shutdown",
        "success": true
    })]
}

fn connection_test(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    match LiveAdapter::connect(&endpoint) {
        Ok(_) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.test",
            "success": true,
            "engine": endpoint.engine,
            "message": "connection successful"
        })],
        Err(err) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.test",
            "success": false,
            "engine": endpoint.engine,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn connection_open(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    match LiveAdapter::connect(&endpoint) {
        Ok(_) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.open",
            "success": true,
            "connection_id": connection_id(&endpoint),
            "engine": endpoint.engine
        })],
        Err(err) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.open",
            "success": false,
            "engine": endpoint.engine,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn connection_close(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "connection.close",
        "success": true,
        "connection_id": request.payload.get("connection_id").cloned().unwrap_or(Value::Null)
    })]
}

fn schema_list(request: &Request) -> Vec<Value> {
    if let Ok(endpoint) = request_endpoint(request) {
        match inspect_live(&endpoint) {
            Ok(result) => {
                let tables: Vec<String> = result
                    .schema
                    .tables
                    .iter()
                    .map(|table| table.name.clone())
                    .collect();
                return vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "schema.list",
                    "success": true,
                    "engine": endpoint.engine,
                    "schema": endpoint_schema(&endpoint),
                    "tables": tables
                })];
            }
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": redact_endpoint_secret(&err, &endpoint)
                })]
            }
        }
    }

    let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
    let tables: Vec<String> = schema
        .tables
        .iter()
        .map(|table| table.name.clone())
        .collect();
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "schema.list",
        "success": true,
        "tables": tables
    })]
}

fn schema_diff(request: &Request) -> Vec<Value> {
    let source_schema = if request.payload.get("source_schema").is_some() {
        parse_schema(&request.payload["source_schema"]).unwrap_or_default()
    } else if request.payload.get("source").is_some() {
        match request
            .payload
            .get("source")
            .map(endpoint_from_value)
            .transpose()
            .and_then(|endpoint| {
                endpoint
                    .map(|endpoint| inspect_live(&endpoint).map(|result| result.schema))
                    .unwrap_or_else(|| Ok(NormalizedSchema::default()))
            }) {
            Ok(schema) => schema,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        }
    } else {
        NormalizedSchema::default()
    };
    let target_schema = if request.payload.get("target_schema").is_some() {
        parse_schema(&request.payload["target_schema"]).unwrap_or_default()
    } else if request.payload.get("target").is_some() {
        match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
            .and_then(|endpoint| {
                endpoint
                    .map(|endpoint| inspect_live(&endpoint).map(|result| result.schema))
                    .unwrap_or_else(|| Ok(NormalizedSchema::default()))
            }) {
            Ok(schema) => schema,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        }
    } else {
        NormalizedSchema::default()
    };

    let differences = normalized_schema_diff(&source_schema, &target_schema);
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "schema.diff",
        "success": differences.is_empty(),
        "differences": differences
    })]
}

fn query_execute(request: &Request) -> Vec<Value> {
    if let Some(rows) = request.payload.get("rows") {
        let columns = request
            .payload
            .get("columns")
            .cloned()
            .unwrap_or_else(|| json!(memory_test_columns_from_rows(rows)));
        return vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "query.execute",
            "success": true,
            "rows": rows,
            "columns": columns,
            "rows_affected": 0
        })];
    }

    let sql = request
        .payload
        .get("sql")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if sql.is_empty() {
        return vec![json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "query.execute requires sql"
        })];
    }
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    let params = query_params(&request.payload);
    let bound_sql = bind_query_params(sql, &params);
    match execute_query_live(&endpoint, &bound_sql) {
        Ok(result) => query_result_events(request, result),
        Err(err) => vec![json!({
            "event": "error",
            "request_id": request.request_id,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn memory_test_columns_from_rows(rows: &Value) -> Vec<String> {
    rows.as_array()
        .and_then(|items| items.first())
        .and_then(Value::as_object)
        .map(|object| object.keys().cloned().collect())
        .unwrap_or_default()
}

pub(crate) struct QueryExecutionResult {
    pub(crate) rows: Vec<Value>,
    pub(crate) columns: Vec<String>,
    pub(crate) rows_affected: u64,
}

fn query_result_events(request: &Request, result: QueryExecutionResult) -> Vec<Value> {
    let stream_rows = request
        .payload
        .get("stream_rows")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !stream_rows {
        return vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "query.execute",
            "success": true,
            "rows": result.rows,
            "columns": result.columns,
            "rows_affected": result.rows_affected
        })];
    }

    let batch_size = request
        .payload
        .get("row_batch_size")
        .and_then(Value::as_u64)
        .unwrap_or(500)
        .max(1) as usize;
    let total = result.rows.len();
    let mut events = vec![json!({
        "event": "columns",
        "request_id": request.request_id,
        "command": "query.execute",
        "columns": result.columns.clone()
    })];
    for (index, chunk) in result.rows.chunks(batch_size).enumerate() {
        events.push(json!({
            "event": "row_batch",
            "request_id": request.request_id,
            "command": "query.execute",
            "batch_index": index,
            "rows": chunk,
            "total": total
        }));
    }
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "query.execute",
        "success": true,
        "rows": [],
        "columns": result.columns,
        "rows_streamed": total,
        "rows_affected": result.rows_affected
    }));
    events
}

fn query_cancel(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "query.cancel",
        "success": true,
        "cancelled": false,
        "message": "No asynchronous query is registered for this JSONL worker",
        "job_id": request.payload.get("job_id").cloned().unwrap_or(Value::Null)
    })]
}

fn job_cancel(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "job.cancel",
        "success": true,
        "cancelled": false,
        "message": "UI workers cancel long-running Rust jobs by terminating the isolated core process",
        "job_id": request.payload.get("job_id").cloned().unwrap_or(Value::Null)
    })]
}


#[cfg(test)]
mod tests {
    use super::*;
    
    
    use serde_json::{json, Value};
    
    
    
    
    
    
    
    
    
    use crate::adapters::test_support::{schema};

    #[test]
    fn service_hello_advertises_core_protocol() {
        let result = handle_request(Request {
            command: "service.hello".to_string(),
            request_id: Some("hello-1".to_string()),
            payload: json!({}),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["request_id"], "hello-1");
        assert_eq!(result["command"], "service.hello");
        assert_eq!(result["service"], "tunnelforge-core");
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("migration.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("dump.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("dump.import")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.derive_charset_contracts")));
    }

    #[test]
    fn oneclick_recommend_contract_returns_manual_steps() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-1".to_string()),
            payload: json!({
                "issues": [{
                    "severity": "warning",
                    "location": "backup",
                    "message": "Backup confirmation was not provided.",
                    "suggestion": "Confirm backup.",
                    "blocking": false
                }]
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "oneclick.recommend");
        assert_eq!(result["success"], true);
        assert_eq!(result["summary"]["manual_review"], 1);
        assert_eq!(result["steps"][0]["selected_option"]["strategy"], "manual");
    }

    #[test]
    fn oneclick_recommend_classifies_deprecated_engine_as_auto_fixable() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-auto-1".to_string()),
            payload: json!({
                "schema": "app",
                "issues": [{
                    "issue_type": "deprecated_engine",
                    "severity": "warning",
                    "location": "app.legacy_table",
                    "table_name": "legacy_table",
                    "message": "Deprecated storage engine detected.",
                    "suggestion": "Convert the table to InnoDB.",
                    "blocking": false
                }, {
                    "issue_type": "zerofill_usage",
                    "severity": "warning",
                    "location": "app.orders.code",
                    "table_name": "orders",
                    "column_name": "code",
                    "message": "ZEROFILL usage detected.",
                    "suggestion": "Handle display padding in the application.",
                    "blocking": false
                }]
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "oneclick.recommend");
        assert_eq!(result["success"], true);
        assert_eq!(result["summary"]["total_issues"], 2);
        assert_eq!(result["summary"]["auto_fixable"], 1);
        assert_eq!(result["summary"]["manual_review"], 1);
        assert_eq!(result["steps"][0]["issue_type"], "deprecated_engine");
        assert_eq!(result["steps"][0]["table_name"], "legacy_table");
        assert_eq!(
            result["steps"][0]["selected_option"]["strategy"],
            "engine_innodb"
        );
        assert_eq!(
            result["steps"][0]["selected_option"]["sql_template"],
            "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
        );
        assert_eq!(result["steps"][1]["selected_option"]["strategy"], "manual");
    }

    #[test]
    fn oneclick_recommend_gates_charset_auto_fix_on_complete_contract() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-charset-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "issues": [{
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": false
                }],
                "charset_contracts": [{
                    "issue_index": 0,
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                    ]
                }]
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["summary"]["auto_fixable"], 1);
        assert_eq!(result["summary"]["manual_review"], 0);
        assert_eq!(
            result["steps"][0]["selected_option"]["strategy"],
            "charset_collation_fk_safe"
        );
        assert_eq!(
            result["steps"][0]["selected_option"]["sql"],
            json!([
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            ])
        );
    }

    #[test]
    fn oneclick_recommend_keeps_charset_manual_without_complete_contract() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-charset-manual-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "issues": [{
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": false
                }]
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["summary"]["auto_fixable"], 0);
        assert_eq!(result["summary"]["manual_review"], 1);
        assert_eq!(result["steps"][0]["selected_option"]["strategy"], "manual");
    }

    #[test]
    fn oneclick_derive_charset_contracts_command_returns_contracts_from_safe_facts() {
        let events = handle_request(Request {
            command: "oneclick.derive_charset_contracts".to_string(),
            request_id: Some("derive-charset-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "issues": [{
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": false
                }],
                "table_facts": [{
                    "table": "tf_oneclick_parent",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci"
                }, {
                    "table": "tf_oneclick_child",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci"
                }],
                "foreign_key_facts": [{
                    "table": "tf_oneclick_child",
                    "referenced_table": "tf_oneclick_parent"
                }]
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "oneclick.derive_charset_contracts");
        assert_eq!(result["success"], true);
        assert_eq!(result["contracts"].as_array().unwrap().len(), 1);
        assert_eq!(
            result["contracts"][0]["fk_order"],
            json!(["tf_oneclick_parent", "tf_oneclick_child"])
        );
    }

    #[test]
    fn oneclick_apply_fixes_defaults_to_dry_run() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-1".to_string()),
            payload: json!({"steps": [{"location": "backup"}]}),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "oneclick.apply_fixes");
        assert_eq!(result["success"], true);
        assert_eq!(result["dry_run"], true);
        assert_eq!(result["skip_count"], 1);
    }

    #[test]
    fn oneclick_apply_fixes_dry_run_previews_charset_plan_without_executing_sql() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-charset-dry-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "dry_run": true,
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
            }),
        });
        let result = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["success"], true);
        assert_eq!(result["dry_run"], true);
        assert_eq!(result["disallowed_fix_attempts"], json!([]));
        assert_eq!(
            result["planned_fixes"][0]["strategy"],
            "charset_collation_fk_safe"
        );
        assert_eq!(result["planned_fixes"][0]["success"], false);
        assert_eq!(result["planned_fixes"][0]["dry_run"], true);
        assert_eq!(result["applied_fixes"], json!([]));
    }

    #[test]
    fn oneclick_apply_fixes_real_charset_requires_endpoint() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-charset-real-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "dry_run": false,
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
            }),
        });
        let error = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("error")))
            .unwrap();

        assert_eq!(error["request_id"], "oneclick-apply-charset-real-1");
        assert!(error["message"]
            .as_str()
            .unwrap()
            .contains("invalid endpoint"));
    }

    #[test]
    fn oneclick_apply_fixes_real_engine_innodb_requires_endpoint() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-real-1".to_string()),
            payload: json!({
                "schema": "app",
                "dry_run": false,
                "steps": [{
                    "issue_type": "deprecated_engine",
                    "location": "app.legacy_table",
                    "selected_option": {
                        "strategy": "engine_innodb",
                        "sql_template": "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
                    }
                }]
            }),
        });
        let error = events
            .into_iter()
            .find(|event| event.get("event") == Some(&json!("error")))
            .unwrap();

        assert_eq!(error["request_id"], "oneclick-apply-real-1");
        assert!(error["message"]
            .as_str()
            .unwrap()
            .contains("invalid endpoint"));
    }

    #[test]
    fn schema_diff_reports_table_column_and_type_differences() {
        let result = handle_request(Request {
            command: "schema.diff".to_string(),
            request_id: None,
            payload: json!({
                "source_schema": schema(),
                "target_schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "bigint", "nullable": false}]
                    }, {
                        "name": "audit",
                        "columns": []
                    }]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        let differences = result["differences"].as_array().unwrap();
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "missing_column" && diff["column"] == "name"));
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "extra_table" && diff["table"] == "audit"));
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "type_mismatch" && diff["column"] == "id"));
    }

    #[test]
    fn query_execute_accepts_memory_rows_for_contract_tests() {
        let result = handle_request(Request {
            command: "query.execute".to_string(),
            request_id: None,
            payload: json!({"rows": [{"id": 1, "name": "alpha"}]}),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["command"], "query.execute");
        assert_eq!(result["rows"][0]["name"], "alpha");
    }

    #[test]
    fn query_result_streams_row_batches_when_requested() {
        let events = query_result_events(
            &Request {
                command: "query.execute".to_string(),
                request_id: Some("query-1".to_string()),
                payload: json!({"stream_rows": true, "row_batch_size": 1}),
            },
            QueryExecutionResult {
                rows: vec![json!({"id": 1}), json!({"id": 2})],
                columns: vec!["id".to_string()],
                rows_affected: 0,
            },
        );

        assert_eq!(events[0]["event"], "columns");
        assert_eq!(events[0]["columns"], json!(["id"]));
        assert_eq!(events[1]["event"], "row_batch");
        assert_eq!(events[1]["rows"][0]["id"], 1);
        assert_eq!(events[2]["event"], "row_batch");
        assert_eq!(events[3]["event"], "result");
        assert_eq!(events[3]["rows_streamed"], 2);
        assert_eq!(events[3]["columns"], json!(["id"]));
    }

    #[test]
    fn query_result_includes_non_row_rows_affected() {
        let events = query_result_events(
            &Request {
                command: "query.execute".to_string(),
                request_id: Some("query-1".to_string()),
                payload: json!({}),
            },
            QueryExecutionResult {
                rows: Vec::new(),
                columns: Vec::new(),
                rows_affected: 7,
            },
        );

        assert_eq!(events[0]["event"], "result");
        assert_eq!(events[0]["rows_affected"], 7);
        assert_eq!(events[0]["rows"], json!([]));
        assert_eq!(events[0]["columns"], json!([]));
    }

    #[test]
    fn migrate_command_emits_chunk_checkpoints_before_result() {
        let events = handle_request(Request {
            command: "migrate".to_string(),
            request_id: Some("req-1".to_string()),
            payload: json!({
                "schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }]
                },
                "execution_options": {"mode": "append", "chunk_size": 1},
                "source_data": {"users": [{"id": 1}, {"id": 2}]},
                "target_data": {}
            }),
        });

        let first_row_progress = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("row_progress")))
            .unwrap();
        let result = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert!(first_row_progress < result);
        assert_eq!(events[first_row_progress]["request_id"], "req-1");
        assert!(events[first_row_progress]["state"].is_object());
    }

    #[test]
    fn preflight_emits_actionable_phase_events_before_result() {
        let events = handle_request(Request {
            command: "preflight".to_string(),
            request_id: Some("preflight-1".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {"tables": []},
                "options": {"mode": "append"}
            }),
        });

        let phase_messages: Vec<&str> = events
            .iter()
            .filter(|event| event.get("event") == Some(&json!("phase")))
            .filter_map(|event| event.get("message").and_then(Value::as_str))
            .collect();
        let result_index = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();
        let last_phase_index = events
            .iter()
            .rposition(|event| event.get("event") == Some(&json!("phase")))
            .unwrap();

        assert!(phase_messages.contains(&"preflight checks started"));
        assert!(phase_messages.contains(&"schema compatibility checks completed"));
        assert!(phase_messages.contains(&"target state checks completed"));
        assert!(phase_messages.contains(&"preflight result ready"));
        assert!(last_phase_index < result_index);
    }

    #[test]
    fn cleanup_command_drops_target_tables_in_reverse_dependency_order() {
        let events = handle_request(Request {
            command: "cleanup".to_string(),
            request_id: Some("cleanup-1".to_string()),
            payload: json!({
                "target_engine": "postgresql",
                "schema": {
                    "tables": [
                        {
                            "name": "parents",
                            "columns": [{"name": "id", "type": "int", "primary_key": true}]
                        },
                        {
                            "name": "children",
                            "columns": [
                                {"name": "id", "type": "int", "primary_key": true},
                                {"name": "parent_id", "type": "int"}
                            ],
                            "foreign_keys": [{
                                "name": "children_parent_id_fk",
                                "columns": ["parent_id"],
                                "referenced_table": "parents",
                                "referenced_columns": ["id"]
                            }]
                        }
                    ]
                }
            }),
        });

        let result = events
            .iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "cleanup");
        assert_eq!(result["success"], true);
        assert_eq!(result["dropped_tables"], json!(["children", "parents"]));
    }
}
