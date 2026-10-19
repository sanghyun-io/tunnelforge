//! Format 7 trigger bodies are cross-checked against SHOW CREATE, never parsed out of a header.
use crate::*;
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) fn program_id(p: &NormalizedStoredProgram) -> String {
    if p.kind != "TRIGGER" { return crate::mysql_schema_programs::schema_program_id(p) }
    format!("trigger:{}:{}", p.table.as_deref().unwrap_or_default(), p.name)
}

fn supported_program_charset(p: &NormalizedStoredProgram) -> bool {
    matches!(p.character_set_client.as_deref(), Some("utf8" | "utf8mb3" | "utf8mb4"))
        || (p.definition.is_ascii() && p.name.is_ascii() && p.table.as_deref().is_some_and(str::is_ascii))
}

pub(crate) fn validate_trigger(p: &NormalizedStoredProgram) -> Result<(), String> {
    if p.kind != "TRIGGER" || !matches!(p.timing.as_deref(), Some("BEFORE" | "AFTER"))
        || !matches!(p.event.as_deref(), Some("INSERT" | "UPDATE" | "DELETE"))
        || p.table.as_deref().is_none_or(str::is_empty) || p.name.is_empty()
        || p.action_order.unwrap_or(0) == 0 || p.definition.trim().is_empty()
        || p.sql_mode.is_none() || p.character_set_client.is_none() || p.collation_connection.is_none()
    {
        return Err(format!("trigger_definition_invalid: {}", p.name));
    }
    if !supported_program_charset(p) {
        return Err(format!("program_charset_unsupported: {}", p.name));
    }
    Ok(())
}

pub(crate) fn program_create_sql(p: &NormalizedStoredProgram, name: &str, table: &str) -> Result<String, String> {
    validate_trigger(p)?;
    Ok(format!("CREATE TRIGGER {} {} {} ON {} FOR EACH ROW {}",
        quote_ident("mysql", name), p.timing.as_deref().unwrap(), p.event.as_deref().unwrap(),
        quote_ident("mysql", table), p.definition))
}

fn proven_body(body: &str, original: &str) -> bool {
    !body.trim().is_empty() && original.ends_with(body)
}

pub(crate) fn collect_mysql_stored_programs(endpoint: &Endpoint, schema: &NormalizedSchema, full_export: bool)
    -> Result<(Vec<NormalizedStoredProgram>, BTreeMap<String, String>), String>
{
    if endpoint.engine != "mysql" { return Ok(Default::default()) }
    let LiveAdapter::MySql(mut conn) = LiveAdapter::connect(endpoint)? else { unreachable!() };
    let (mut programs, mut reasons) = collect_trigger_programs(&mut conn, &endpoint.database, schema, true)?;
    if full_export {
        let (captured, failures) = crate::mysql_schema_programs::collect_schema_programs(&mut conn, &endpoint.database)?;
        programs.extend(captured); reasons.extend(failures);
    }
    Ok((programs, reasons))
}

pub(crate) fn collect_trigger_programs(conn: &mut mysql::PooledConn, database: &str, schema: &NormalizedSchema, prove_definition: bool)
    -> Result<(Vec<NormalizedStoredProgram>, BTreeMap<String, String>), String>
{
    let rows: Vec<mysql::Row> = conn.exec(
        "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER, ACTION_STATEMENT, SQL_MODE, CHARACTER_SET_CLIENT, COLLATION_CONNECTION, DATABASE_COLLATION, DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? ORDER BY EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER",
        (database,),
    ).map_err(|err| format!("trigger capture failed: {err}"))?;
    let mut programs = Vec::new();
    let mut reasons = BTreeMap::new();
    for row in rows {
        let field = |key: &str| row.get::<String, _>(key);
        let table = field("EVENT_OBJECT_TABLE").ok_or("trigger table unreadable")?;
        if !schema.tables.iter().any(|t| t.name == table) { continue }
        let name = field("TRIGGER_NAME").ok_or("trigger name unreadable")?;
        let p = NormalizedStoredProgram {
            kind: "TRIGGER".into(), name, table: Some(table), timing: field("ACTION_TIMING"), event: field("EVENT_MANIPULATION"),
            action_order: row.get("ACTION_ORDER"), definition: field("ACTION_STATEMENT").unwrap_or_default(),
            sql_mode: field("SQL_MODE"), character_set_client: field("CHARACTER_SET_CLIENT"),
            collation_connection: field("COLLATION_CONNECTION"), database_collation: field("DATABASE_COLLATION"),
            definer: field("DEFINER"), time_zone: None, status: None,
        };
        let original: Option<mysql::Row> = if prove_definition { conn.query_first(format!("SHOW CREATE TRIGGER {}.{}",
            quote_ident("mysql", database), quote_ident("mysql", &p.name)))
            .map_err(|err| format!("SHOW CREATE TRIGGER {} failed: {err}", p.name))? } else { None };
        let original = original.and_then(|r| r.get::<String, _>("SQL Original Statement"));
        let reason = if prove_definition && !original.as_deref().is_some_and(|sql| proven_body(&p.definition, sql)) {
            Some("trigger_body_unproven")
        } else if !supported_program_charset(&p) {
            Some("program_charset_unsupported")
        } else if validate_trigger(&p).is_err() {
            Some("trigger_definition_invalid")
        } else { None };
        if let Some(reason) = reason { reasons.insert(program_id(&p), reason.into()); }
        else { programs.push(p); }
    }
    Ok((programs, reasons))
}

/// Conservative trip-wire, including whitespace/comments between a qualifier and its dot.
pub(crate) fn references_source_schema(body: &str, schema: &str) -> bool {
    let body = body.to_lowercase();
    let schema = schema.to_lowercase();
    for qualifier in [quote_ident("mysql", &schema), format!("\"{}\"", schema.replace('"', "\"\"")), schema.clone()] {
        for (start, _) in body.match_indices(&qualifier) {
            if qualifier == schema && body[..start].chars().next_back()
                .is_some_and(|c| c.is_alphanumeric() || matches!(c, '_' | '$' | '.')) { continue }
            let mut tail = &body[start + qualifier.len()..];
            loop {
                tail = tail.trim_start();
                if tail.starts_with('.') { return true }
                if let Some(rest) = tail.strip_prefix("/*") {
                    let Some(end) = rest.find("*/") else { return true };
                    tail = &rest[end + 2..];
                } else if tail.starts_with('#') || tail.starts_with("--") {
                    let Some(end) = tail.find('\n') else { break };
                    tail = &tail[end + 1..];
                } else if let Some(rest) = tail.strip_prefix("*/") {
                    // Also trip on qualifiers in MySQL executable comments.
                    tail = rest;
                } else { break }
            }
        }
    }
    false
}

pub(crate) fn selected_triggers<'a>(programs: &'a [NormalizedStoredProgram], tables: &BTreeSet<String>) -> Vec<&'a NormalizedStoredProgram> {
    let mut selected = programs.iter().filter(|p| p.table.as_ref().is_some_and(|t| tables.contains(t))).collect::<Vec<_>>();
    selected.sort_by_key(|p| (&p.table, &p.timing, &p.event, p.action_order));
    selected
}

pub(crate) fn target_trigger_names(conn: &mut mysql::PooledConn, schema: &str) -> Result<Vec<(String, String)>, String> {
    conn.exec("SELECT EVENT_OBJECT_TABLE, TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? ORDER BY 1,2", (schema,))
        .map_err(|err| format!("target trigger inspection failed: {err}"))
}

pub(crate) fn trigger_name_conflicts(target: &[(String, String)], programs: &[&NormalizedStoredProgram], folds: bool) -> Vec<String> {
    let key = |s: &str| if folds { s.to_lowercase() } else { s.to_string() };
    target.iter().filter(|(table, name)| programs.iter().any(|p| p.kind=="TRIGGER" && p.name.to_lowercase() == name.to_lowercase()
        && p.table.as_deref().is_none_or(|dump_table| key(dump_table) != key(table))))
        .map(|(table, name)| format!("{table}.{name}")).collect()
}

pub(crate) fn trigger_binlog_blocked(log_bin: bool, trusted: bool, super_grant: Option<bool>) -> bool {
    log_bin && !trusted && super_grant == Some(false)
}

pub(crate) fn trigger_create_error(err: &mysql::Error) -> String {
    let code = if matches!(err, mysql::Error::MySqlError(e) if e.code == 1419) { "trigger_binlog_privilege" } else { "program_create_failed" };
    if code == "trigger_binlog_privilege" {
        format!("{code}: 트리거를 만들려면 log_bin_trust_function_creators=1 설정 또는 SUPER 권한이 필요합니다. 트리거를 제외하려면 '트리거 없이 테이블만 가져오기'를 켜세요. {err}")
    } else { format!("{code}: {err}") }
}

pub(crate) fn create_trigger(conn: &mut mysql::PooledConn, p: &NormalizedStoredProgram, name: &str, table: &str) -> Result<Vec<Value>, String> {
    create_trigger_with_checkpoint(conn,p,name,table,||Ok(()))
}

pub(crate) fn create_trigger_with_checkpoint<F: FnMut()->Result<(),String>>(conn: &mut mysql::PooledConn, p: &NormalizedStoredProgram, name: &str, table: &str, mut created: F) -> Result<Vec<Value>,String> {
    let sql = program_create_sql(p, name, table)?;
    conn.exec_drop("SET SESSION sql_mode=?, character_set_client=?, collation_connection=?",
        (&p.sql_mode, &p.character_set_client, &p.collation_connection)).map_err(|err| trigger_create_error(&err))?;
    conn.query_drop(sql).map_err(|err| trigger_create_error(&err))?;
    created()?;
    let warnings: Vec<(String, u32, String)> = conn.query("SHOW WARNINGS").map_err(|err| trigger_create_error(&err))?;
    let mut warnings = warnings.into_iter().map(|(level, code, message)| json!({"name":p.name,"level":level,"code":code,"message":message})).collect::<Vec<_>>();
    let current: Option<String> = conn.query_first("SELECT CURRENT_USER()").map_err(|err| trigger_create_error(&err))?;
    if p.definer != current {
        warnings.push(json!({"name":p.name,"code":"program_definer_replaced","source":p.definer,"target":current}));
    }
    let collation: Option<String> = conn.query_first("SELECT @@collation_database").map_err(|err| trigger_create_error(&err))?;
    if p.database_collation.is_some() && p.database_collation != collation {
        warnings.push(json!({"name":p.name,"code":"program_database_collation_changed","source":p.database_collation,"target":collation}));
    }
    Ok(warnings)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_suffix_and_qualifier_tripwire_fail_closed() {
        assert!(proven_body("BEGIN SET NEW.v=1; END", "CREATE TRIGGER t BEFORE INSERT ON x FOR EACH ROW BEGIN SET NEW.v=1; END"));
        assert!(!proven_body("SET NEW.v=2", "CREATE TRIGGER t BEFORE INSERT ON x FOR EACH ROW SET NEW.v=1"));
        assert!(!proven_body("", "CREATE TRIGGER t"));
        for body in ["INSERT INTO `Src` . `audit` VALUES(1)", "INSERT INTO Src /*comment*/ . audit VALUES(1)", "SELECT \"Src\".audit",
            "INSERT INTO Src -- comment\n .audit VALUES(1)", "INSERT INTO `Src` #comment\n .audit VALUES(1)", "SELECT /*! `Src` */ .audit"] {
            assert!(references_source_schema(body, "src"), "{body}");
        }
        assert!(!references_source_schema("SET NEW.v=1", "src"));
        for body in ["SELECT myapp.t", "SET NEW.url='x.app.example.com'", "SELECT other_app.t", "SELECT app2.t"] {
            assert!(!references_source_schema(body, "app"), "{body}");
        }
        assert!(references_source_schema("SELECT app . t", "app"));
        assert!(references_source_schema("SELECT `app` . `t`", "app"));
    }
    #[test]
    fn binlog_and_server_error_classification() {
        for (log, trusted, grant, blocked) in [
            (false,false,None,false), (false,false,Some(false),false), (false,false,Some(true),false),
            (false,true,None,false), (false,true,Some(false),false), (false,true,Some(true),false),
            (true,false,None,false), (true,false,Some(false),true), (true,false,Some(true),false),
            (true,true,None,false), (true,true,Some(false),false), (true,true,Some(true),false),
        ] { assert_eq!(trigger_binlog_blocked(log,trusted,grant), blocked); }
        let error = mysql::Error::MySqlError(mysql::MySqlError { state:"HY000".into(), message:"denied".into(), code:1419 });
        assert!(trigger_create_error(&error).starts_with("trigger_binlog_privilege:"));
        let error = mysql::Error::MySqlError(mysql::MySqlError { state:"HY000".into(), message:"already exists".into(), code:1359 });
        assert!(trigger_create_error(&error).starts_with("program_create_failed:"));
    }

    fn trigger() -> NormalizedStoredProgram {
        serde_json::from_value(json!({"kind":"TRIGGER","name":"odd`name","table":"t`x","timing":"BEFORE","event":"INSERT",
            "action_order":1,"definition":"BEGIN SET NEW.v=1; SET NEW.v=2; END","sql_mode":"","character_set_client":"utf8mb4",
            "collation_connection":"utf8mb4_bin","definer":"other@host"})).unwrap()
    }

    #[test]
    fn structured_header_is_quoted_and_whitelisted_before_sql() {
        let p = trigger();
        assert_eq!(program_create_sql(&p, &p.name, p.table.as_deref().unwrap()).unwrap(),
            "CREATE TRIGGER `odd``name` BEFORE INSERT ON `t``x` FOR EACH ROW BEGIN SET NEW.v=1; SET NEW.v=2; END");
        for (key, value) in [("kind",json!("EVENT")),("timing",json!("BEFORE INSERT ON victim")),("event",json!("INSERT; DROP TABLE victim")),
            ("table",Value::Null),("action_order",json!(0)),("sql_mode",Value::Null),("character_set_client",Value::Null),
            ("collation_connection",Value::Null),("definition",json!(""))] {
            let mut tampered = serde_json::to_value(&p).unwrap(); tampered[key] = value;
            let p = serde_json::from_value(tampered).unwrap();
            assert!(program_create_sql(&p,"probe","probe_table").is_err(),"{key}");
        }
        let mut non_utf = p; non_utf.character_set_client = Some("latin1".into());
        assert!(validate_trigger(&non_utf).is_ok()); non_utf.definition = "SET NEW.v='한글'".into();
        assert!(validate_trigger(&non_utf).is_err());
        assert!(references_source_schema("INSERT INTO `my db` . t VALUES(1)", "my db"));
        assert!(references_source_schema("SELECT \"my\"\"db\" . t", "my\"db"));
        let mut non_utf = trigger(); non_utf.character_set_client = Some("latin1".into()); non_utf.name = "한글".into();
        assert!(validate_trigger(&non_utf).is_err());
    }

    #[test]
    fn selected_trigger_order_held_ids_and_conflicts_follow_tables() {
        let schema = crate::adapters::test_support::schema();
        let table = &schema.tables[0].name;
        let mut first = trigger(); first.table = Some(table.clone()); first.name = "z".into();
        let mut second = first.clone(); second.action_order = Some(2); second.name = "a".into();
        let mut other = first.clone(); other.table = Some("outside".into()); other.name = "other".into();
        let programs = vec![second,other,first];
        let tables = BTreeSet::from([table.clone()]);
        let selected = selected_triggers(&programs,&tables);
        assert_eq!(selected.iter().map(|p|p.name.as_str()).collect::<Vec<_>>(),["z","a"]);
        let held = crate::dump_format::held_object_ids(&schema,&programs);
        assert_eq!(held,BTreeSet::from([format!("trigger:{table}:a"),format!("trigger:{table}:z")]));
        assert_eq!(trigger_name_conflicts(&[("kept".into(),"A".into())],&selected,false),["kept.A"]);
        assert!(trigger_name_conflicts(&[(table.clone(),"a".into())],&selected,false).is_empty());
    }
}
