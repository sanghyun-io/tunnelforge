//! `query.explain` (TF-STATUS-133): execution-plan lookup on top of the session query machinery.
//!
//! The command is translated into one `EXPLAIN` statement and run through the normal
//! `query.execute` path, so sessions, cancel, timeouts, read-only enforcement and TLS all behave
//! exactly as for a query. Plain `EXPLAIN` does not run the statement. `ANALYZE` does, so it is
//! only allowed on plain read-only statements (never on data changes or DDL) and must be requested
//! explicitly with `"analyze": true`.

use serde_json::{json, Value};

use crate::query_guard::split_statements;
use crate::*;

pub(crate) const EXPLAIN_REFUSED_CODE: &str = "explain_refused";

/// Statement kinds the server can `EXPLAIN` without running them.
const EXPLAINABLE: [&str; 8] = ["select", "insert", "replace", "update", "delete", "merge", "with", "values"];
/// Statements that only read; the sole kinds `ANALYZE` (which really executes) accepts.
const ANALYZABLE: [&str; 3] = ["select", "with", "values"];
/// Words that make a "read-only looking" statement change data or leave side effects when run.
const SIDE_EFFECT_WORDS: [&str; 14] = [
    "insert", "update", "delete", "merge", "replace", "into", "nextval", "setval", "set_config",
    "pg_advisory_lock", "pg_advisory_xact_lock", "pg_notify", "dblink_exec", "get_lock",
];

pub(crate) struct ExplainPlan {
    pub(crate) sql: String,
    pub(crate) meta: Value,
}

fn words(statement: &str) -> Vec<String> {
    statement
        .to_ascii_lowercase()
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .filter(|w| !w.is_empty())
        .map(str::to_string)
        .collect()
}

/// The `EXPLAIN` statement for `sql`, or the reason it is refused.
pub(crate) fn build_explain(engine: &str, sql: &str, analyze: bool) -> Result<ExplainPlan, String> {
    let statements = split_statements(sql);
    if statements.len() != 1 {
        return Err("실행 계획은 한 번에 하나의 문장만 조회할 수 있습니다".to_string());
    }
    let statement = statements[0].trim();
    let words = words(statement);
    let first = words.first().map(String::as_str).unwrap_or("");
    if first == "explain" {
        return Err("이미 EXPLAIN으로 시작하는 문장입니다. 분석할 쿼리만 입력하세요".to_string());
    }
    let allowed = if analyze { &ANALYZABLE[..] } else { &EXPLAINABLE[..] };
    if !allowed.contains(&first) {
        return Err(if analyze {
            format!(
                "ANALYZE는 쿼리를 실제로 실행하므로 읽기 전용 조회(SELECT/WITH/VALUES)에만 사용할 수 있습니다 (입력: {})",
                first.to_uppercase()
            )
        } else {
            format!("이 문장({})은 실행 계획을 조회할 수 없습니다", first.to_uppercase())
        });
    }
    if analyze {
        if let Some(word) = words.iter().find(|w| SIDE_EFFECT_WORDS.contains(&w.as_str())) {
            return Err(format!(
                "ANALYZE는 쿼리를 실제로 실행합니다. 데이터 변경 또는 부수 효과가 있을 수 있는 `{}`이(가) 포함되어 거부했습니다",
                word.to_uppercase()
            ));
        }
        let locks = words
            .windows(2)
            .any(|w| w[0] == "for" && matches!(w[1].as_str(), "update" | "share" | "no" | "key"))
            || words.windows(3).any(|w| w[0] == "lock" && w[1] == "in" && w[2] == "share");
        if locks {
            return Err("ANALYZE는 쿼리를 실제로 실행하므로 행 잠금(FOR UPDATE/SHARE)을 거는 쿼리는 거부합니다".to_string());
        }
    }
    let (sql, format) = match (engine, analyze) {
        ("mysql", false) => (format!("EXPLAIN FORMAT=JSON {statement}"), "json"),
        // MySQL has no JSON form for EXPLAIN ANALYZE; the tree text is the only output.
        ("mysql", true) => (format!("EXPLAIN ANALYZE {statement}"), "tree"),
        ("postgresql", false) => (format!("EXPLAIN (FORMAT JSON) {statement}"), "json"),
        ("postgresql", true) => (format!("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}"), "json"),
        (other, _) => return Err(format!("unsupported engine: {other}")),
    };
    Ok(ExplainPlan { sql, meta: json!({"engine": engine, "analyze": analyze, "format": format}) })
}

fn refusal(request: &Request, message: String) -> Value {
    json!({"event": "error", "request_id": request.request_id,
           "error_code": EXPLAIN_REFUSED_CODE, "message": message})
}

/// `query.explain` -> equivalent `query.execute` request plus the metadata to tag the result with.
pub(crate) fn rewrite(request: &Request, engine: &str) -> Result<(Request, Value), Value> {
    let sql = request.payload.get("sql").and_then(Value::as_str).unwrap_or("").trim();
    if sql.is_empty() {
        return Err(json!({"event": "error", "request_id": request.request_id,
                          "message": "query.explain requires sql"}));
    }
    let analyze = request.payload.get("analyze").and_then(Value::as_bool).unwrap_or(false);
    let plan = build_explain(engine, sql, analyze).map_err(|message| refusal(request, message))?;
    let mut payload = request.payload.clone();
    payload["sql"] = json!(plan.sql);
    // The plan is one small row: never stream or truncate it.
    if let Some(object) = payload.as_object_mut() {
        for key in ["stream_rows", "max_rows", "max_bytes", "output"] {
            object.remove(key);
        }
    }
    let rewritten = Request { command: "query.execute".to_string(), request_id: request.request_id.clone(), payload };
    Ok((rewritten, plan.meta))
}

/// Re-label the events of the underlying `query.execute` as `query.explain` results.
pub(crate) fn tag_event(mut event: Value, meta: &Value) -> Value {
    if event.get("command").and_then(Value::as_str) == Some("query.execute") {
        event["command"] = json!("query.explain");
        if event.get("event").and_then(Value::as_str) == Some("result") {
            event["explain"] = meta.clone();
        }
    }
    event
}

/// One-off (no `connection_id`) variant on an endpoint payload.
pub(crate) fn explain_stateless(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => return vec![json!({"event": "error", "request_id": request.request_id, "message": err})],
    };
    match rewrite(request, &endpoint.engine) {
        Ok((rewritten, meta)) => handle_request(rewritten)
            .into_iter()
            .map(|event| tag_event(event, &meta))
            .collect(),
        Err(event) => vec![event],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sql(engine: &str, text: &str, analyze: bool) -> Result<String, String> {
        build_explain(engine, text, analyze).map(|plan| plan.sql)
    }

    #[test]
    fn plain_explain_per_engine() {
        assert_eq!(sql("mysql", "SELECT 1;", false).unwrap(), "EXPLAIN FORMAT=JSON SELECT 1");
        assert_eq!(sql("postgresql", "select 1", false).unwrap(), "EXPLAIN (FORMAT JSON) select 1");
        // DML can be explained without running it
        assert!(sql("mysql", "DELETE FROM t WHERE id = 1", false).is_ok());
        assert!(sql("postgresql", "UPDATE t SET a = 1", false).is_ok());
    }

    #[test]
    fn analyze_form_per_engine() {
        assert_eq!(sql("mysql", "SELECT 1", true).unwrap(), "EXPLAIN ANALYZE SELECT 1");
        assert_eq!(
            sql("postgresql", "SELECT 1", true).unwrap(),
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT 1"
        );
        let plan = build_explain("mysql", "SELECT 1", true).unwrap();
        assert_eq!(plan.meta["format"], "tree");
        assert_eq!(plan.meta["analyze"], true);
    }

    #[test]
    fn analyze_refuses_anything_that_changes_data_or_locks() {
        for engine in ["mysql", "postgresql"] {
            for bad in [
                "DELETE FROM t",
                "UPDATE t SET a = 1",
                "INSERT INTO t VALUES (1)",
                "CREATE TABLE x (a int)",
                "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
                "SELECT * INTO new_t FROM t",
                "SELECT nextval('s')",
                "SELECT * FROM t FOR UPDATE",
                "SELECT * FROM t FOR NO KEY UPDATE",
            ] {
                assert!(sql(engine, bad, true).is_err(), "{engine}: {bad}");
            }
            assert!(sql(engine, "WITH x AS (SELECT 1) SELECT * FROM x", true).is_ok());
        }
    }

    #[test]
    fn rejects_multiple_statements_nested_explain_and_non_explainable() {
        assert!(sql("mysql", "SELECT 1; SELECT 2", false).is_err());
        assert!(sql("mysql", "EXPLAIN SELECT 1", false).is_err());
        assert!(sql("postgresql", "CREATE TABLE x (a int)", false).is_err());
        assert!(sql("postgresql", "DROP TABLE x", false).is_err());
        assert!(sql("mysql", "", false).is_err());
    }

    #[test]
    fn rewrite_drops_streaming_limits_and_keeps_timeout() {
        let request = Request {
            command: "query.explain".into(),
            request_id: Some("r".into()),
            payload: json!({"connection_id": "c", "sql": "SELECT 1", "max_rows": 5, "stream_rows": true, "timeout_ms": 1000}),
        };
        let (rewritten, meta) = rewrite(&request, "mysql").unwrap();
        assert_eq!(rewritten.command, "query.execute");
        assert_eq!(rewritten.payload["sql"], "EXPLAIN FORMAT=JSON SELECT 1");
        assert!(rewritten.payload.get("max_rows").is_none() && rewritten.payload.get("stream_rows").is_none());
        assert_eq!(rewritten.payload["timeout_ms"], 1000);
        assert_eq!(meta["analyze"], false);
    }

    #[test]
    fn refusals_carry_a_stable_error_code() {
        let request = Request {
            command: "query.explain".into(),
            request_id: None,
            payload: json!({"sql": "DELETE FROM t", "analyze": true}),
        };
        let event = rewrite(&request, "postgresql").unwrap_err();
        assert_eq!(event["error_code"], EXPLAIN_REFUSED_CODE);
    }

    #[test]
    fn tagging_only_relabels_query_execute_events() {
        let meta = json!({"engine": "mysql"});
        let tagged = tag_event(json!({"event": "result", "command": "query.execute"}), &meta);
        assert_eq!(tagged["command"], "query.explain");
        assert_eq!(tagged["explain"]["engine"], "mysql");
        let other = tag_event(json!({"event": "error"}), &meta);
        assert!(other.get("command").is_none());
    }
}
