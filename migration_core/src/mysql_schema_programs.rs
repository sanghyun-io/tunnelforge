//! Schema-level programs: retain source context, rebuild only the object header.
use crate::*;
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

struct Token { start: usize, end: usize, text: String, quote: u8 }

fn tokens(sql: &str, no_escape: bool) -> Result<Vec<Token>, String> {
    let b = sql.as_bytes();
    let mut out = Vec::new(); let mut i = 0;
    while i < b.len() {
        if b[i].is_ascii_whitespace() { i += 1; continue }
        if b[i]==b'#' || (b[i..].starts_with(b"--") && b.get(i+2).is_some_and(u8::is_ascii_whitespace)) {
            i += sql[i..].find('\n').unwrap_or(b.len()-i); continue;
        }
        if b[i..].starts_with(b"/*") {
            i += 2; i += sql[i..].find("*/").ok_or("program_definition_invalid: unterminated comment")?+2; continue;
        }
        let start = i; let mut quote = 0;
        if matches!(b[i], b'\'' | b'"' | b'`') {
            quote = b[i]; i += 1; let mut closed = false;
            while i < b.len() {
                if b[i] == b'\\' && quote != b'`' && !no_escape { i += 2; }
                else if b[i] == quote {
                    i += 1;
                    if b.get(i) == Some(&quote) { i += 1; } else { closed = true; break }
                } else { i += 1; }
            }
            if !closed { return Err("program_definition_invalid: unterminated quote".into()) }
        } else if b[i].is_ascii_alphanumeric() || b[i] == b'_' || b[i] >= 128 {
            i += 1;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || matches!(b[i], b'_' | b'$') || b[i] >= 128) { i += 1; }
        } else { i += 1; }
        let raw = &sql[start..i];
        let text = if quote != 0 { raw[1..raw.len()-1].replace(&format!("{}{}",quote as char,quote as char), &(quote as char).to_string()) } else { raw.to_string() };
        out.push(Token { start, end:i, text, quote });
    }
    Ok(out)
}

fn keyword(t: &Token, word: &str) -> bool { t.quote == 0 && t.text.eq_ignore_ascii_case(word) }

/// Only backtick object identifiers from SHOW CREATE are accepted. DEFINER is never replayed.
fn suffix_start(p: &NormalizedStoredProgram, schema: Option<&str>) -> Result<(Vec<Token>, usize), String> {
    let invalid = || format!("program_definition_invalid: {}", p.name);
    if !matches!(p.kind.as_str(), "PROCEDURE" | "FUNCTION" | "EVENT") || p.name.is_empty()
        || p.table.is_some() || p.sql_mode.is_none() || p.character_set_client.is_none() || p.collation_connection.is_none() {
        return Err(invalid())
    }
    if !matches!(p.character_set_client.as_deref(), Some("utf8" | "utf8mb3" | "utf8mb4")) && !p.definition.is_ascii() {
        return Err(format!("program_charset_unsupported: {}",p.name))
    }
    let ts = tokens(&p.definition, p.sql_mode.as_deref().unwrap().split(',').any(|s| s == "NO_BACKSLASH_ESCAPES"))?;
    let mut i = 0;
    let take = |i: &mut usize, word: &str| -> Result<(),String> {
        if !ts.get(*i).is_some_and(|t| keyword(t,word)) { return Err(invalid()) }
        *i += 1; Ok(())
    };
    take(&mut i,"CREATE")?;
    if ts.get(i).is_some_and(|t| keyword(t,"DEFINER")) {
        i += 1; take(&mut i,"=")?;
        if !ts.get(i).is_some_and(|t| t.quote == b'`' || t.quote == b'\'') { return Err(invalid()) }
        i += 1; take(&mut i,"@")?;
        if !ts.get(i).is_some_and(|t| t.quote == b'`' || t.quote == b'\'') { return Err(invalid()) }
        i += 1;
    }
    take(&mut i,&p.kind)?;
    let first = ts.get(i).ok_or_else(invalid)?;
    if first.quote != b'`' { return Err(invalid()) }
    if ts.get(i+1).is_some_and(|t| keyword(t,".")) {
        if schema.is_some_and(|s| s != first.text) { return Err(invalid()) }
        i += 2;
    }
    let name = ts.get(i).ok_or_else(invalid)?;
    if name.quote != b'`' || name.text != p.name { return Err(invalid()) }
    let end = name.end;
    if p.kind == "EVENT" {
        if p.time_zone.is_none() || !matches!(p.status.as_deref(), Some("ENABLED" | "DISABLED" | "SLAVESIDE_DISABLED" | "REPLICA_SIDE_DISABLED")) { return Err(invalid()) }
        if !ts.get(i+1).is_some_and(|t| keyword(t,"ON")) || !ts.get(i+2).is_some_and(|t| keyword(t,"SCHEDULE")) { return Err(invalid()) }
    } else if !ts.get(i+1).is_some_and(|t| keyword(t,"(")) { return Err(invalid()) }
    Ok((ts,end))
}

pub(crate) fn schema_program_sql(p: &NormalizedStoredProgram, name: &str, schema: Option<&str>) -> Result<String,String> {
    let (ts,end) = suffix_start(p,schema)?;
    let mut suffix = p.definition[end..].to_string();
    if p.kind == "EVENT" {
        let do_at = ts.iter().position(|t| t.start >= end && keyword(t,"DO")).ok_or("program_definition_invalid: event body missing")?;
        let states = ts.iter().enumerate().filter(|(_,t)| t.start >= end && t.start < ts[do_at].start && (keyword(t,"ENABLE") || keyword(t,"DISABLE"))).collect::<Vec<_>>();
        if states.len() != 1 { return Err("program_definition_invalid: event status missing or duplicated".into()) }
        let (i,state) = states[0];
        let mut state_end = state.end;
        if keyword(state,"DISABLE") && ts.get(i+1).is_some_and(|t| keyword(t,"ON")) && ts.get(i+2).is_some_and(|t| keyword(t,"SLAVE") || keyword(t,"REPLICA")) { state_end = ts[i+2].end; }
        suffix.replace_range(state.start-end..state_end-end,"DISABLE");
    }
    Ok(format!("CREATE {} {}{}",p.kind,quote_ident("mysql",name),suffix))
}

pub(crate) fn schema_program_body(p: &NormalizedStoredProgram, schema: &str) -> Result<String,String> {
    let (ts,end) = suffix_start(p,Some(schema))?;
    let start = if p.kind == "EVENT" { ts.iter().find(|t| t.start >= end && keyword(t,"DO")).ok_or("program_definition_invalid: event body missing")?.end } else { end };
    Ok(p.definition[start..].to_string())
}

pub(crate) fn schema_program_id(p: &NormalizedStoredProgram) -> String {
    format!("{}:{}",if p.kind == "EVENT" { "event" } else { "routine" },p.name)
}

pub(crate) fn read_schema_program(conn: &mut mysql::PooledConn, schema: &str, kind: &str, name: &str) -> Result<NormalizedStoredProgram,String> {
    if !matches!(kind,"PROCEDURE" | "FUNCTION" | "EVENT") { return Err("program_definition_invalid: kind".into()) }
    reset_program_context(conn)?;
    let row: mysql::Row = conn.query_first(format!("SHOW CREATE {kind} {}.{}",quote_ident("mysql",schema),quote_ident("mysql",name)))
        .map_err(|err| format!("program_body_unreadable: {name}: {err}"))?.ok_or("program_body_unreadable: missing SHOW CREATE")?;
    let field = |key: &str| row.get::<Option<String>,_>(key).flatten();
    let key = match kind { "PROCEDURE" => "Create Procedure", "FUNCTION" => "Create Function", _ => "Create Event" };
    let definition = field(key).ok_or_else(|| format!("{}: {name}",if kind=="EVENT" {"program_body_unreadable"} else {"routine_body_unreadable"}))?;
    let (catalog,name_col) = if kind == "EVENT" { ("EVENTS","EVENT_NAME") } else { ("ROUTINES","ROUTINE_NAME") };
    let filter = if kind == "EVENT" { String::new() } else { format!(" AND ROUTINE_TYPE='{kind}'") };
    let definer: Option<String> = conn.exec_first(format!("SELECT DEFINER FROM information_schema.{catalog} WHERE {name_col}=? AND {}_SCHEMA=?{filter}",if kind=="EVENT" {"EVENT"} else {"ROUTINE"}), (name,schema)).map_err(|e|e.to_string())?;
    let status = if kind == "EVENT" { conn.exec_first("SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? AND EVENT_NAME=?",(schema,name)).map_err(|e|e.to_string())? } else { None };
    Ok(NormalizedStoredProgram { kind:kind.into(),name:name.into(),table:None,timing:None,event:None,action_order:None,
        definition, sql_mode:field("sql_mode"), character_set_client:field("character_set_client"), collation_connection:field("collation_connection"),
        database_collation:field("Database Collation"),time_zone:field("time_zone"),definer,status })
}

pub(crate) fn collect_schema_programs(conn: &mut mysql::PooledConn, schema: &str) -> Result<(Vec<NormalizedStoredProgram>,BTreeMap<String,String>),String> {
    let mut names: Vec<(String,String)> = conn.exec("SELECT ROUTINE_TYPE,ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=? ORDER BY ROUTINE_NAME,ROUTINE_TYPE",(schema,)).map_err(|e|e.to_string())?;
    let events: Vec<String> = conn.exec("SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? ORDER BY EVENT_NAME",(schema,)).map_err(|e|e.to_string())?;
    names.extend(events.into_iter().map(|name| ("EVENT".into(),name)));
    let mut programs = Vec::new(); let mut reasons = BTreeMap::new();
    for (kind,name) in names {
        let id = format!("{}:{name}",if kind=="EVENT" {"event"} else {"routine"});
        match read_schema_program(conn,schema,&kind,&name).and_then(|p| schema_program_sql(&p,&name,Some(schema)).map(|_|p)) {
            Ok(p) => programs.push(p),
            Err(err) => { reasons.insert(id,err.split(':').next().unwrap().into()); }
        }
    }
    // ROUTINE_TYPE is absent from legacy inspection IDs: hold a name only as a complete pair.
    programs.retain(|p| !reasons.contains_key(&schema_program_id(p)));
    Ok((programs,reasons))
}

pub(crate) fn target_schema_programs(conn: &mut mysql::PooledConn, schema: &str) -> Result<Vec<(String,String,String,Option<String>)>,String> {
    reset_program_context(conn)?;
    let rows: Vec<(String,String,String)> = conn.exec("SELECT ROUTINE_TYPE,ROUTINE_NAME,DEFINER FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=? ORDER BY 1,2",(schema,)).map_err(|e|e.to_string())?;
    let mut out = rows.into_iter().map(|(k,n,d)|(k,n,d,None)).collect::<Vec<_>>();
    let rows: Vec<(String,String,String)> = conn.exec("SELECT EVENT_NAME,DEFINER,STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? ORDER BY 1",(schema,)).map_err(|e|e.to_string())?;
    out.extend(rows.into_iter().map(|(n,d,s)|("EVENT".into(),n,d,Some(s)))); Ok(out)
}

pub(crate) fn event_enabled(source: Option<&str>, target: Option<&str>) -> bool { target.or(source) == Some("ENABLED") }

pub(crate) fn event_due_now(conn: &mut mysql::PooledConn, p: &NormalizedStoredProgram, target: Option<&str>) -> Result<bool,String> {
    if !event_enabled(p.status.as_deref(),target) { return Ok(false) }
    let (ts,end) = suffix_start(p,None)?;
    let do_at = ts.iter().find(|t| t.start >= end && keyword(t,"DO")).ok_or("program_definition_invalid: event body")?.start;
    let at = ts.iter().enumerate().find(|(_,t)| t.start >= end && t.start < do_at && (keyword(t,"AT") || keyword(t,"STARTS")));
    let Some((i,_)) = at else { return Ok(true) };
    let Some(stamp) = ts.get(i+1).filter(|t| t.quote == b'\'') else { return Ok(true) };
    conn.exec_drop("SET SESSION time_zone=?",(&p.time_zone,)).map_err(|e|e.to_string())?;
    let due: Option<bool> = conn.exec_first("SELECT CAST(? AS DATETIME) <= CURRENT_TIMESTAMP",(&stamp.text,)).map_err(|e|e.to_string())?;
    Ok(due.unwrap_or(true))
}

pub(crate) fn reset_program_context(conn: &mut mysql::PooledConn) -> Result<(),String> {
    conn.query_drop("SET NAMES utf8mb4").map_err(|e|e.to_string())?;
    conn.query_drop("SET SESSION sql_mode='', time_zone='+00:00'").map_err(|e|e.to_string())
}

pub(crate) fn set_program_context(conn: &mut mysql::PooledConn, p: &NormalizedStoredProgram) -> Result<(),String> {
    reset_program_context(conn)?;
    conn.exec_drop("SET SESSION sql_mode=?, character_set_client=?, collation_connection=?",(&p.sql_mode,&p.character_set_client,&p.collation_connection)).map_err(|e|e.to_string())?;
    if p.kind == "EVENT" { conn.exec_drop("SET SESSION time_zone=?",(&p.time_zone,)).map_err(|e|e.to_string())?; }
    Ok(())
}

fn create_schema_program(conn: &mut mysql::PooledConn, p: &NormalizedStoredProgram, name: &str) -> Result<Vec<Value>,String> {
    set_program_context(conn,p)?;
    create_schema_program_in_context(conn,p,name)
}

fn create_schema_program_in_context(conn: &mut mysql::PooledConn, p: &NormalizedStoredProgram, name: &str) -> Result<Vec<Value>,String> {
    conn.query_drop(schema_program_sql(p,name,None)?).map_err(|e| match &e {
        mysql::Error::MySqlError(e) if routine_binlog_error(e.code) && p.kind=="FUNCTION" => format!("routine_binlog_unsafe: {e}"),
        _ => format!("program_create_failed: {e}")
    })?;
    let warnings: Vec<(String,u32,String)> = conn.query("SHOW WARNINGS").map_err(|e|e.to_string())?;
    let mut out = warnings.into_iter().map(|(level,code,message)| json!({"name":p.name,"kind":p.kind,"level":level,"code":code,"message":message})).collect::<Vec<_>>();
    let current: Option<String> = conn.query_first("SELECT CURRENT_USER()").map_err(|e|e.to_string())?;
    if p.definer != current { out.push(json!({"name":p.name,"kind":p.kind,"code":"program_definer_replaced","source":p.definer,"target":current})); }
    let collation: Option<String> = conn.query_first("SELECT @@collation_database").map_err(|e|e.to_string())?;
    if p.database_collation.is_some() && p.database_collation != collation { out.push(json!({"name":p.name,"code":"program_database_collation_changed","source":p.database_collation,"target":collation})); }
    if p.time_zone.as_deref()==Some("SYSTEM") { out.push(json!({"name":p.name,"code":"program_event_system_timezone"})); }
    Ok(out)
}

pub(crate) fn function_binlog_preview_blocked(p: &NormalizedStoredProgram, log_bin: bool, trusted: bool, super_grant: Option<bool>) -> bool {
    if !log_bin || trusted { return false }
    if super_grant!=Some(true) { return true }
    let Ok((ts,end))=suffix_start(p,None) else { return true };
    let mut depth=0; let mut arguments_end=None;
    for (i,t) in ts.iter().enumerate().filter(|(_,t)|t.start>=end) {
        if keyword(t,"(") { depth+=1; }
        if keyword(t,")") { depth-=1; if depth==0 { arguments_end=Some(i+1); break } }
    }
    let Some(start)=arguments_end else { return true };
    let header=ts[start..].iter().take_while(|t|!keyword(t,"BEGIN") && !keyword(t,"RETURN")).collect::<Vec<_>>();
    let deterministic=header.iter().enumerate().any(|(i,t)|keyword(t,"DETERMINISTIC") && (i==0 || !keyword(header[i-1],"NOT")));
    let no_sql=header.windows(2).any(|w|keyword(w[0],"NO") && keyword(w[1],"SQL"));
    let reads=header.windows(3).any(|w|keyword(w[0],"READS") && keyword(w[1],"SQL") && keyword(w[2],"DATA"));
    !(deterministic || no_sql || reads)
}

pub(crate) fn routine_binlog_error(code: u16) -> bool { matches!(code,1418 | 1419) }

pub(crate) fn probe_routines(conn: &mut mysql::PooledConn, programs: &[NormalizedStoredProgram], prefix: &str, skipped: &mut BTreeSet<(String,String)>, warnings: &mut Vec<Value>) -> Result<(),String> {
    for (i,p) in programs.iter().enumerate().filter(|(_,p)| matches!(p.kind.as_str(),"PROCEDURE" | "FUNCTION")) {
        let name = format!("{prefix}_r{i}");
        let result = create_schema_program(conn,p,&name);
        if result.as_ref().is_err_and(|e|e.starts_with("routine_binlog_unsafe:")) {
            skipped.insert((p.kind.clone(),p.name.clone()));
            warnings.push(json!({"name":p.name,"kind":p.kind,"code":"routine_binlog_unsafe"}));
            continue;
        }
        {
            if let Err(err) = conn.query_drop(format!("DROP {} IF EXISTS {}",p.kind,quote_ident("mysql",&name))) {
                warnings.push(json!({"name":name,"kind":p.kind,"code":"program_probe_cleanup_failed","message":err.to_string()}));
                return Err(format!("program_probe_cleanup_failed: {name}: {err}"))
            }
        }
        result.map_err(|e|format!("program_probe_failed: {}: {}",p.name,e.trim_start_matches("program_create_failed: ")))?;
    }
    Ok(())
}

/// A rollback replays the target status verbatim; ALTER EVENT would change its definer.
fn restore_schema_program(conn: &mut mysql::PooledConn, schema: &str, previous: &NormalizedStoredProgram, failure: &mut Value) -> Result<(),String> {
    set_program_context(conn,previous)?;
    conn.query_drop(format!("DROP {} IF EXISTS {}",previous.kind,quote_ident("mysql",&previous.name))).map_err(|e|e.to_string())?;
    let current:Option<String>=conn.query_first("SELECT CURRENT_USER()").map_err(|e|e.to_string())?;
    match conn.query_drop(&previous.definition) {
        Ok(()) => { failure["previous_definer_preserved"]=json!(true); }
        Err(mysql::Error::MySqlError(err)) if err.code==1227 => {
            let (_,end)=suffix_start(previous,Some(schema))?;
            let sql=format!("CREATE {} {}{}",previous.kind,quote_ident("mysql",&previous.name),&previous.definition[end..]);
            conn.query_drop(sql).map_err(|e|e.to_string())?;
            failure["previous_definer_preserved"]=json!(false);
            let mut warnings=failure["restore_warnings"].as_array().cloned().unwrap_or_default();
            warnings.push(json!({"name":previous.name,"kind":previous.kind,"code":"program_restore_definer_changed","source":previous.definer,"target":current}));
            failure["restore_warnings"]=json!(warnings);
        }
        Err(err) => return Err(err.to_string()),
    }
    let restored_warnings:Vec<(String,u32,String)>=conn.query("SHOW WARNINGS").map_err(|e|e.to_string())?;
    let mut warnings=failure["restore_warnings"].as_array().cloned().unwrap_or_default();
    warnings.extend(restored_warnings.iter().map(|(level,code,message)|json!({"name":previous.name,"kind":previous.kind,"level":level,"code":code,"message":message})));
    failure["restore_warnings"]=json!(warnings);
    if restored_warnings.iter().any(|(_,code,_)|*code==1588) { return Err("program_event_dropped: previous event disappeared during recovery".into()) }
    Ok(())
}

pub(crate) fn preserves_foreign_definer(conn: &mut mysql::PooledConn) -> bool {
    ["SUPER","SET_ANY_DEFINER","SET_USER_ID"].iter().any(|privilege|crate::import::import_preview::mysql_grants_allow(conn,privilege,None)==Some(true))
}

pub(crate) fn replace_schema_program(conn: &mut mysql::PooledConn, schema: &str, p: &NormalizedStoredProgram) -> Result<(Vec<Value>,Value),Value> {
    let unchanged = |err:String| json!({"kind":p.kind,"name":p.name,"code":"program_restore_unavailable","error":err,"target_unchanged":true});
    reset_program_context(conn).map_err(unchanged)?;
    let target = target_schema_programs(conn,schema).map_err(unchanged)?;
    let previous = if let Some((_,name,_,_)) = target.iter().find(|(k,n,_,_)| k==&p.kind && n.eq_ignore_ascii_case(&p.name)) {
        Some(read_schema_program(conn,schema,&p.kind,name).and_then(|previous|schema_program_sql(&previous,&previous.name,Some(schema)).map(|_|previous)).map_err(unchanged)?)
    } else { None };
    if let Some(previous)=&previous {
        set_program_context(conn,previous).map_err(unchanged)?;
        let privilege=if p.kind=="EVENT" { "EVENT" } else { "CREATE ROUTINE" };
        if crate::import::import_preview::mysql_grants_allow(conn,privilege,Some(schema))==Some(false) {
            return Err(unchanged(format!("{privilege} is required to recreate the previous definition")))
        }
        // Functions that cannot pass binlog checks cannot be recovered either.
        if p.kind=="FUNCTION" {
            let nonce=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|e|unchanged(e.to_string()))?.as_nanos();
            let mut skipped=BTreeSet::new(); let mut warnings=Vec::new();
            let probe=probe_routines(conn,std::slice::from_ref(previous),&format!("tf_probe_restore_{}_{nonce}",std::process::id()),&mut skipped,&mut warnings);
            if probe.is_err() || !skipped.is_empty() {
                let mut failure=unchanged(probe.err().unwrap_or_else(||"previous function fails binary log checks".into()));
                failure["warnings"]=json!(warnings); failure["previous_definition"]=json!(previous.definition); return Err(failure)
            }
        }
    }
    // All source session settings must succeed while the target is still intact.
    set_program_context(conn,p).map_err(unchanged)?;
    let enable = p.kind=="EVENT" && event_enabled(p.status.as_deref(),previous.as_ref().and_then(|p|p.status.as_deref()));
    conn.query_drop(format!("DROP {} IF EXISTS {}",p.kind,quote_ident("mysql",&p.name))).map_err(|e|unchanged(e.to_string()))?;
    let mut warnings = Vec::new();
    if previous.is_some() && p.kind!="EVENT" { warnings.push(json!({"name":p.name,"kind":p.kind,"code":"routine_grants_reset"})); }
    let result = create_schema_program_in_context(conn,p,&p.name).and_then(|created_warnings| {
        warnings.extend(created_warnings);
        if warnings.iter().any(|w|w["code"]==1588) { return Err("program_event_dropped: event was immediately removed by the server".into()) }
        if enable { conn.query_drop(format!("ALTER EVENT {} ENABLE",quote_ident("mysql",&p.name))).map_err(|e|e.to_string())?; }
        Ok(())
    });
    match result {
        Ok(()) => Ok((warnings,json!({"kind":p.kind,"name":p.name,"enabled":enable}))),
        Err(err) => {
            let code=if err.starts_with("routine_binlog_unsafe:") {"routine_binlog_unsafe"} else if err.starts_with("program_event_dropped:") {"program_event_dropped"} else {"program_create_failed"};
            let mut failure = json!({"kind":p.kind,"name":p.name,"code":code,"error":err,"warnings":warnings,"previous_definition":previous.as_ref().map(|p|&p.definition)});
            if let Some(previous) = previous {
                let restore=restore_schema_program(conn,schema,&previous,&mut failure);
                failure["previous_restored"] = json!(restore.is_ok());
                failure["restore_error"] = json!(restore.err());
            }
            Err(failure)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn program(kind: &str, definition: &str) -> NormalizedStoredProgram {
        serde_json::from_value(json!({"kind":kind,"name":"p","definition":definition,"sql_mode":"","character_set_client":"utf8mb4","collation_connection":"utf8mb4_bin","time_zone":"+00:00","status":"ENABLED"})).unwrap()
    }
    #[test]
    fn binlog_preview_accounts_for_1418_even_with_super_and_1419_without_it() {
        for (declaration,super_grant,blocked) in [
            ("",Some(true),true), ("NOT DETERMINISTIC",Some(true),true),
            ("DETERMINISTIC",Some(true),false), ("NO SQL",Some(true),false), ("READS SQL DATA",Some(true),false),
            ("NOT DETERMINISTIC NO SQL",Some(true),false), ("COMMENT 'DETERMINISTIC'",Some(true),true),
            ("DETERMINISTIC",Some(false),true), ("READS SQL DATA",None,true),
        ] {
            let p=program("FUNCTION",&format!("CREATE FUNCTION `p`(x INT) RETURNS INT {declaration} RETURN x"));
            assert_eq!(function_binlog_preview_blocked(&p,true,false,super_grant),blocked,"{declaration}");
            assert!(!function_binlog_preview_blocked(&p,false,false,super_grant));
            assert!(!function_binlog_preview_blocked(&p,true,true,super_grant));
        }
    }

    #[test]
    fn headers_drop_only_definer_and_object_qualifier() {
        let p=program("FUNCTION","CREATE DEFINER=`old`@`%` FUNCTION `src`.`p`(x INT) RETURNS INT SQL SECURITY DEFINER RETURN x+1");
        assert_eq!(schema_program_sql(&p,"new",Some("src")).unwrap(),"CREATE FUNCTION `new`(x INT) RETURNS INT SQL SECURITY DEFINER RETURN x+1");
        assert!(schema_program_sql(&p,"new",Some("other")).is_err());
        for sql in ["CREATE FUNCTION other() RETURNS INT RETURN 1","CREATE PROCEDURE `p`() SELECT 1","CREATE FUNCTION `q`() RETURNS INT RETURN 1"] { assert!(schema_program_sql(&program("FUNCTION",sql),"p",None).is_err()); }
        let p=program("PROCEDURE","CREATE PROCEDURE `p`() BEGIN /* ' ignored */ SELECT '한글'; END");
        assert!(schema_program_sql(&p,"p",None).is_ok());
        let mut p=p; p.character_set_client=Some("latin1".into());
        assert!(schema_program_sql(&p,"p",None).unwrap_err().starts_with("program_charset_unsupported:"));
        assert!(schema_program_sql(&program("FUNCTION","CREATE FUNCTION \"p\"() RETURNS INT RETURN 1"),"p",None).is_err());
    }
    #[test]
    fn event_headers_disable_replica_and_enabled_states_without_editing_body() {
        for status in ["ENABLE","DISABLE","DISABLE ON SLAVE","DISABLE ON REPLICA"] {
            let p=program("EVENT",&format!("CREATE DEFINER=`a`@`b` EVENT `p` ON SCHEDULE EVERY 1 DAY STARTS '2000-01-01' {status} DO BEGIN SET @s='ENABLE'; END"));
            let sql=schema_program_sql(&p,"new",None).unwrap();
            assert!(sql.contains("'2000-01-01' DISABLE DO BEGIN SET @s='ENABLE'; END"));
        }
        for (source,target,enabled) in [("ENABLED",None,true),("DISABLED",None,false),("ENABLED",Some("DISABLED"),false),("DISABLED",Some("ENABLED"),true),("ENABLED",Some("SLAVESIDE_DISABLED"),false),("ENABLED",Some("REPLICA_SIDE_DISABLED"),false)] { assert_eq!(event_enabled(Some(source),target),enabled); }
        for (code,blocked) in [(1418,true),(1419,true),(1064,false),(1227,false)] { assert_eq!(routine_binlog_error(code),blocked); }
    }
}
