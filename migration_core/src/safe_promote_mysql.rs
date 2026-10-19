//! Guarded same-name MySQL promotion with journaled stored-program recovery.
//! Original foreign keys are never dropped.
use crate::*;
use mysql::prelude::Queryable;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

type Db = mysql::PooledConn;
type ProgramName = (String, String, Option<String>);
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct NativeTable {
    name: String,
    ddl: String,
    static_digest: String,
    table_id: u64,
    columns: Vec<String>,
    normalized: NormalizedTable,
    content_digest: Option<Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct NativeView {
    name: String,
    ddl: String,
    dependencies: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct ForeignKey {
    child: String,
    parent: String,
    name: String,
    columns: Vec<String>,
    parent_columns: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct PromotionPlan {
    pub restore_id: String,
    pub digest: String,
    pub original_database: String,
    pub candidate_database: String,
    host: String,
    port: u16,
    server_uuid: String,
    nonce: String,
    pub backup_database: String,
    pub clone_database: String,
    original_tables: BTreeMap<String, NativeTable>,
    candidate_tables: BTreeMap<String, NativeTable>,
    original_views: BTreeMap<String, NativeView>,
    replacement_views: BTreeMap<String, NativeView>,
    original_fks: Vec<ForeignKey>,
    candidate_fks: Vec<ForeignKey>,
    hidden_views: BTreeMap<String, String>,
    saved_views: BTreeMap<String, String>,
    view_order: Vec<String>,
    pub warnings: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    original_programs: Vec<NormalizedStoredProgram>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    original_program_inventory: Vec<ProgramName>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    promoted_programs: Vec<NormalizedStoredProgram>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    import_definer: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    original_trigger_definitions: BTreeMap<String, String>,
}
fn q(name: &str) -> String {
    quote_ident("mysql", name)
}
fn qualified(db: &str, name: &str) -> String {
    format!("{}.{}", q(db), q(name))
}
fn error(context: &str, err: impl std::fmt::Display) -> String {
    format!("safe_promotion: {context}: {err}")
}
fn hash<T: Serialize>(value: &T) -> Result<String, String> {
    Ok(hex::encode(Sha256::digest(
        serde_json::to_vec(value).map_err(|e| e.to_string())?,
    )))
}
fn connect(endpoint: &Endpoint) -> Result<Db, String> {
    if endpoint.engine != "mysql" {
        return Err("safe promotion supports MySQL only".into());
    }
    let mut conn = mysql_single_statement_conn(endpoint).map_err(|e| error("connect", e))?;
    for sql in ["SET SESSION lock_wait_timeout=2","SET SESSION innodb_lock_wait_timeout=2","SET SESSION information_schema_stats_expiry=0","SET SESSION autocommit=1","SET SESSION time_zone='+00:00'","SET SESSION sql_mode=CONCAT_WS(',', 'STRICT_ALL_TABLES','NO_AUTO_VALUE_ON_ZERO', REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode,'NO_BACKSLASH_ESCAPES',''),'STRICT_TRANS_TABLES',''),'ANSI_QUOTES',''),'NO_ZERO_DATE',''),'NO_ZERO_IN_DATE',''))"] {
        conn.query_drop(sql).map_err(|e|error("configure session",e))?;
    }
    raise_mysql_max_error_count(&mut conn).map_err(|e| error("configure session", e))?;
    // SHOW CREATE VIEW omits schema qualifiers for the current default schema.
    // Use a neutral namespace so every native dependency is explicit.
    conn.query_drop("USE information_schema")
        .map_err(|e| e.to_string())?;
    Ok(conn)
}

pub(crate) fn has_complete_metadata_grants(grants: &[String]) -> bool {
    let mut privileges = BTreeSet::new();
    for grant in grants {
        let statement = grant.trim().to_ascii_uppercase();
        if statement.starts_with("REVOKE ") {
            return false;
        }
        let Some(body) = statement.strip_prefix("GRANT ") else {
            continue;
        };
        // Role expansion/partial revokes need an effective-privilege evaluator.
        // The guarded path accepts direct global visibility proof only.
        if !body.contains(" ON ") {
            return false;
        }
        let Some((names, _)) = body.split_once(" ON *.* TO ") else {
            continue;
        };
        if names == "ALL PRIVILEGES" {
            privileges.extend(["SELECT", "SHOW VIEW", "PROCESS"].map(str::to_string));
        } else {
            for name in names.split(',').map(str::trim) {
                if matches!(name, "SELECT" | "SHOW VIEW" | "PROCESS") {
                    privileges.insert(name.to_string());
                }
            }
        }
    }
    ["SELECT", "SHOW VIEW", "PROCESS"]
        .iter()
        .all(|name| privileges.contains(*name))
}
fn prove_metadata_visibility(conn: &mut Db) -> Result<(), String> {
    let grants: Vec<String> = conn
        .query("SHOW GRANTS FOR CURRENT_USER")
        .map_err(|e| error("metadata visibility proof", e))?;
    if !has_complete_metadata_grants(&grants) {
        return Err("guarded promotion requires proven direct global SELECT, SHOW VIEW and PROCESS visibility without roles/partial revokes; database-scoped permissions cannot certify external dependencies. The verified candidate remains usable as a separate namespace.".into());
    }
    Ok(())
}
fn database_exists(conn: &mut Db, db: &str) -> Result<bool, String> {
    conn.exec_first::<u64, _, _>(
        "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=?",
        (db,),
    )
    .map(|n| n == Some(1))
    .map_err(|e| e.to_string())
}
fn tables(conn: &mut Db, db: &str) -> Result<BTreeMap<String, String>, String> {
    conn.exec::<(String,String),_,_>("SELECT TABLE_NAME,COALESCE(ENGINE,'VIEW') FROM information_schema.TABLES WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME",(db,)).map(|rows|rows.into_iter().collect()).map_err(|e|error("list tables",e))
}
fn table_id(conn: &mut Db, db: &str, name: &str) -> Result<u64, String> {
    optional_table_id(conn,db,name)?.ok_or_else(||format!("safe_promotion: no unambiguous InnoDB identity for {db}.{name}; partitioned/encoded names are unsupported"))
}
fn optional_table_id(conn: &mut Db, db: &str, name: &str) -> Result<Option<u64>, String> {
    conn.exec_first(
        "SELECT TABLE_ID FROM information_schema.INNODB_TABLES WHERE NAME=?",
        (format!("{db}/{name}"),),
    )
    .map_err(|e| {
        error(
            "InnoDB identity lookup requires PROCESS metadata privilege",
            e,
        )
    })
}
fn content_digest(conn: &mut Db, db: &str, table: &NormalizedTable) -> Result<Value, String> {
    let sql = format!(
        "SELECT {} FROM {}",
        legacy_projected_text_columns_sql("mysql", table),
        qualified(db, &table.name)
    );
    crate::import::safe_restore_digest::mysql_table_content_digest(conn, table, &sql)
}
fn static_ddl(ddl: &str) -> String {
    // Only the volatile allocator option is excluded. Quoted comments/defaults
    // containing AUTO_INCREMENT are untouched by the token scanner.
    let tokens = tokens(ddl);
    let mut replacements = Vec::new();
    let mut depth = 0_i32;
    for i in 0..tokens.len() {
        if !tokens[i].quoted {
            if tokens[i].word == "(" {
                depth += 1;
            } else if tokens[i].word == ")" {
                depth -= 1;
            }
        }
        let Some(window) = tokens.get(i..i + 3) else {
            continue;
        };
        if depth == 0
            && !window[0].quoted
            && window[0].word.eq_ignore_ascii_case("AUTO_INCREMENT")
            && window[1].word == "="
            && window[2].word.bytes().all(|b| b.is_ascii_digit())
        {
            replacements.push((window[2].start, window[2].end, "<allocator>".to_string()));
        }
    }
    replace_ranges(ddl, replacements)
}
fn inspect_table(
    conn: &mut Db,
    db: &str,
    name: &str,
    with_checksum: bool,
) -> Result<NativeTable, String> {
    let (_, ddl): (String, String) = conn
        .query_first(format!("SHOW CREATE TABLE {}", qualified(db, name)))
        .map_err(|e| error("SHOW CREATE TABLE", e))?
        .ok_or("table disappeared")?;
    let columns_all:Vec<(String,String,String)>=conn.exec("SELECT COLUMN_NAME,COLUMN_TYPE,EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION",(db,name)).map_err(|e|e.to_string())?;
    let columns = columns_all
        .iter()
        .filter(|(_, _, extra)| {
            !extra.contains("VIRTUAL GENERATED") && !extra.contains("STORED GENERATED")
        })
        .map(|(name, _, _)| name.clone())
        .collect::<Vec<_>>();
    let mut normalized:NormalizedTable=serde_json::from_value(json!({"name":name,"columns":columns_all.iter().map(|(name,kind,_)|json!({"name":name,"type":kind})).collect::<Vec<_>>()})).map_err(|e|e.to_string())?;
    normalized.auto_increment = conn.exec_first::<Option<u64>,_,_>(
        "SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=?",(db,name)
    ).map_err(|e|e.to_string())?.flatten();
    let digest = if with_checksum {
        Some(content_digest(conn, db, &normalized)?)
    } else {
        None
    };
    Ok(NativeTable {
        name: name.into(),
        static_digest: hash(&static_ddl(&ddl))?,
        ddl,
        table_id: table_id(conn, db, name)?,
        columns,
        normalized,
        content_digest: digest,
    })
}
fn foreign_keys(
    conn: &mut Db,
    db: &str,
) -> Result<(Vec<ForeignKey>, Vec<(String, String, String, String)>), String> {
    let rows:Vec<(String,String,String,String,String,String,String)>=conn.exec(
        "SELECT TABLE_SCHEMA,TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_SCHEMA,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_NAME IS NOT NULL AND (TABLE_SCHEMA=? OR REFERENCED_TABLE_SCHEMA=?) ORDER BY TABLE_SCHEMA,TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION",(db,db)).map_err(|e|e.to_string())?;
    let mut local: BTreeMap<(String, String), ForeignKey> = BTreeMap::new();
    let mut external = Vec::new();
    for (child_db, child, name, column, parent_db, parent, parent_column) in rows {
        if child_db != db || parent_db != db {
            external.push((child_db, child, parent_db, parent));
            continue;
        }
        let fk = local
            .entry((child.clone(), name.clone()))
            .or_insert_with(|| ForeignKey {
                child,
                parent,
                name,
                columns: vec![],
                parent_columns: vec![],
            });
        fk.columns.push(column);
        fk.parent_columns.push(parent_column);
    }
    Ok((local.into_values().collect(), external))
}
fn inspect_views(
    conn: &mut Db,
    db: &str,
) -> Result<(BTreeMap<String, NativeView>, Vec<(String, String, String)>), String> {
    let names:Vec<String>=conn.exec("SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA=? ORDER BY TABLE_NAME",(db,)).map_err(|e|e.to_string())?;
    let dependencies:Vec<(String,String,String)>=conn.exec("SELECT VIEW_NAME,TABLE_SCHEMA,TABLE_NAME FROM information_schema.VIEW_TABLE_USAGE WHERE VIEW_SCHEMA=? ORDER BY VIEW_NAME,TABLE_SCHEMA,TABLE_NAME",(db,)).map_err(|e|e.to_string())?;
    let mut result = BTreeMap::new();
    let mut external = Vec::new();
    for name in names {
        let row: mysql::Row = conn
            .query_first(format!("SHOW CREATE VIEW {}", qualified(db, &name)))
            .map_err(|e| e.to_string())?
            .ok_or("view disappeared")?;
        let ddl: String = row.get(1).ok_or("view definition inaccessible")?;
        let mut used = Vec::new();
        for (view, table_db, table) in &dependencies {
            if view == &name {
                if table_db != db {
                    external.push((name.clone(), table_db.clone(), table.clone()));
                } else {
                    used.push(table.clone());
                }
            }
        }
        result.insert(
            name.clone(),
            NativeView {
                name,
                ddl,
                dependencies: used,
            },
        );
    }
    Ok((result, external))
}
fn reject_unsupported(conn: &mut Db, db: &str, names: &BTreeSet<String>) -> Result<(), String> {
    let kinds = tables(conn, db)?;
    for name in names {
        if kinds.get(name).map(String::as_str) != Some("InnoDB") {
            return Err(format!(
                "safe_promotion: {db}.{name} is not an InnoDB base table"
            ));
        }
    }
    let partitions:Vec<String>=conn.exec("SELECT DISTINCT TABLE_NAME FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA=? AND PARTITION_NAME IS NOT NULL",(db,)).map_err(|e|e.to_string())?;
    if partitions.iter().any(|table| names.contains(table)) {
        return Err(
            "safe_promotion: partitioned affected tables are outside the identity guard".into(),
        );
    }
    Ok(())
}
#[derive(Clone, Debug)]
struct Token {
    start: usize,
    end: usize,
    word: String,
    identifier: bool,
    quoted: bool,
}
fn tokens(sql: &str) -> Vec<Token> {
    let b = sql.as_bytes();
    let mut i = 0;
    let mut out = vec![];
    while i < b.len() {
        if b[i].is_ascii_whitespace() {
            i += 1;
            continue;
        }
        let start = i;
        if matches!(b[i], b'\'' | b'"' | b'`') {
            let quote = b[i];
            i += 1;
            let mut word = Vec::new();
            while i < b.len() {
                if b[i] == b'\\' && quote != b'`' {
                    word.push(b[i]);
                    i += 1;
                    if i < b.len() {
                        word.push(b[i]);
                        i += 1;
                    }
                } else if b[i] == quote {
                    i += 1;
                    if b.get(i) == Some(&quote) {
                        word.push(quote);
                        i += 1;
                    } else {
                        break;
                    }
                } else {
                    word.push(b[i]);
                    i += 1;
                }
            }
            out.push(Token {
                start,
                end: i,
                word: if quote == b'`' {
                    String::from_utf8_lossy(&word).into_owned()
                } else {
                    String::new()
                },
                identifier: quote == b'`',
                quoted: true,
            });
        } else if b[i].is_ascii_alphanumeric() || b[i] == b'_' || b[i] >= 128 {
            i += 1;
            while i < b.len()
                && (b[i].is_ascii_alphanumeric() || matches!(b[i], b'_' | b'$') || b[i] >= 128)
            {
                i += 1;
            }
            out.push(Token {
                start,
                end: i,
                word: sql[start..i].to_string(),
                identifier: true,
                quoted: false,
            });
        } else {
            i += 1;
            out.push(Token {
                start,
                end: i,
                word: sql[start..i].into(),
                identifier: false,
                quoted: false,
            });
        }
    }
    out
}
fn replace_ranges(sql: &str, mut changes: Vec<(usize, usize, String)>) -> String {
    changes.sort_by_key(|(s, _, _)| *s);
    let mut out = String::new();
    let mut at = 0;
    for (start, end, text) in changes {
        if start < at {
            continue;
        }
        out.push_str(&sql[at..start]);
        out.push_str(&text);
        at = end;
    }
    out.push_str(&sql[at..]);
    out
}
fn qualified_end(ts: &[Token], start: usize) -> Option<(usize, String, Option<String>)> {
    let first = ts.get(start)?;
    if !first.identifier {
        return None;
    }
    if ts.get(start + 1).is_some_and(|t| t.word == ".") {
        let second = ts.get(start + 2)?;
        if !second.identifier {
            return None;
        }
        Some((start + 2, second.word.clone(), Some(first.word.clone())))
    } else {
        Some((start, first.word.clone(), None))
    }
}
fn rewrite_table(
    table: &NativeTable,
    from: &str,
    to: &str,
    locations: &BTreeMap<String, String>,
) -> Result<String, String> {
    let ts = tokens(&table.ddl);
    let table_kw = ts
        .iter()
        .position(|t| !t.quoted && t.word.eq_ignore_ascii_case("TABLE"))
        .ok_or("invalid native CREATE TABLE")?;
    let (end, _, _) = qualified_end(&ts, table_kw + 1).ok_or("invalid native table identifier")?;
    let mut changes = vec![(
        ts[table_kw + 1].start,
        ts[end].end,
        qualified(to, &table.name),
    )];
    for i in 0..ts.len() {
        if !ts[i].quoted && ts[i].word.eq_ignore_ascii_case("REFERENCES") {
            let (end, name, schema) = qualified_end(&ts, i + 1).ok_or("invalid native FK")?;
            if schema.as_deref().is_some_and(|s| s != from) {
                return Err("external native FK rejected".into());
            }
            let db = locations
                .get(&name)
                .ok_or("FK dependency outside promotion closure")?;
            changes.push((ts[i + 1].start, ts[end].end, qualified(db, &name)));
        }
    }
    Ok(replace_ranges(&table.ddl, changes))
}
fn rewrite_view(
    view: &NativeView,
    from: &str,
    to: &str,
    new_name: &str,
    locations: &BTreeMap<String, String>,
) -> Result<String, String> {
    let ts = tokens(&view.ddl);
    let k = ts
        .iter()
        .position(|t| !t.quoted && t.word.eq_ignore_ascii_case("VIEW"))
        .ok_or("invalid native CREATE VIEW")?;
    let (end, _, _) = qualified_end(&ts, k + 1).ok_or("invalid view name")?;
    let mut changes = vec![(ts[k + 1].start, ts[end].end, qualified(to, new_name))];
    let start = end + 1;
    let mut seen = BTreeSet::new();
    for i in start..ts.len().saturating_sub(2) {
        if ts[i].identifier && ts[i].word == from && ts[i + 1].word == "." && ts[i + 2].identifier {
            let object = &ts[i + 2].word;
            let db = locations.get(object).ok_or_else(|| {
                format!(
                    "view {} has an unmodeled qualified dependency {object}",
                    view.name
                )
            })?;
            let column_ref = ts.get(i + 3).is_some_and(|t| t.word == ".");
            let mut before = i;
            while before > 0 && ts[before - 1].word == "(" {
                before -= 1;
            }
            let table_ref = before > 0
                && !ts[before - 1].quoted
                && matches!(
                    ts[before - 1].word.to_ascii_uppercase().as_str(),
                    "FROM" | "JOIN" | "STRAIGHT_JOIN"
                );
            if !column_ref && !table_ref {
                return Err(format!("view {} uses an ambiguous qualifier; guarded promotion requires explicit FROM/JOIN dependency form",view.name));
            }
            changes.push((ts[i].start, ts[i].end, q(db)));
            seen.insert(object.clone());
        }
    }
    if view.dependencies.iter().any(|name| !seen.contains(name)) {
        return Err(format!(
            "view {} dependencies cannot be completely rewritten",
            view.name
        ));
    }
    Ok(replace_ranges(&view.ddl, changes))
}
fn view_order(views: &BTreeMap<String, NativeView>) -> Result<Vec<String>, String> {
    let mut remaining = views.keys().cloned().collect::<BTreeSet<_>>();
    let mut order = vec![];
    while !remaining.is_empty() {
        let ready = remaining
            .iter()
            .filter(|name| {
                views[*name]
                    .dependencies
                    .iter()
                    .all(|dep| !remaining.contains(dep))
            })
            .cloned()
            .collect::<Vec<_>>();
        if ready.is_empty() {
            return Err("view dependency cycle".into());
        }
        for name in ready {
            remaining.remove(&name);
            order.push(name);
        }
    }
    Ok(order)
}

fn view_select(ddl: &str) -> Result<&str, String> {
    let ts = tokens(ddl);
    let view = ts
        .iter()
        .position(|t| !t.quoted && t.word.eq_ignore_ascii_case("VIEW"))
        .ok_or("native view keyword missing")?;
    let body = ts
        .iter()
        .skip(view + 1)
        .find(|t| !t.quoted && t.word.eq_ignore_ascii_case("AS"))
        .ok_or("native view SELECT missing")?;
    let mut end = ddl.len();
    if ts.len() >= 4
        && ts[ts.len() - 1].word.eq_ignore_ascii_case("OPTION")
        && ts[ts.len() - 2].word.eq_ignore_ascii_case("CHECK")
    {
        let mut before = ts.len() - 3;
        if matches!(
            ts[before].word.to_ascii_uppercase().as_str(),
            "LOCAL" | "CASCADED"
        ) {
            before = before.saturating_sub(1);
        }
        if ts[before].word.eq_ignore_ascii_case("WITH") {
            end = ts[before].start;
        }
    }
    Ok(ddl[body.end..end].trim())
}
fn prevalidate_final_views(conn: &mut Db, plan: &PromotionPlan) -> Result<(), String> {
    let locations = plan
        .original_tables
        .keys()
        .chain(plan.candidate_tables.keys())
        .chain(plan.replacement_views.keys())
        .map(|name| (name.clone(), plan.original_database.clone()))
        .collect();
    for name in &plan.view_order {
        let view = &plan.replacement_views[name];
        let from = if plan.original_views.get(name) == Some(view) {
            &plan.original_database
        } else {
            &plan.candidate_database
        };
        let routines:Option<u64>=conn.exec_first("SELECT COUNT(*) FROM information_schema.VIEW_ROUTINE_USAGE WHERE TABLE_SCHEMA=? AND TABLE_NAME=?",(from,name)).map_err(|e|error("view routine dependency coverage",e))?;
        if routines != Some(0) {
            return Err(format!(
                "view {name} invokes stored routines; guarded promotion cannot cover their effects"
            ));
        }
        let definition = rewrite_view(view, from, &plan.original_database, name, &locations)?;
        conn.query_drop(format!("EXPLAIN {}", view_select(&definition)?))
            .map_err(|e| {
                error(
                    "final view cannot be prepared against current original names/shapes",
                    e,
                )
            })?;
    }
    Ok(())
}

fn program_inventory(conn: &mut Db, database: &str) -> Result<Vec<ProgramName>, String> {
    let mut names = crate::mysql_programs::target_trigger_names(conn, database)?.into_iter()
        .map(|(table,name)| ("TRIGGER".into(),name,Some(table))).collect::<Vec<_>>();
    names.extend(crate::mysql_schema_programs::target_schema_programs(conn, database)?.into_iter()
        .map(|(kind,name,_,_)| (kind,name,None)));
    names.sort();
    reset_session(conn)?;
    Ok(names)
}

pub(crate) fn capture_affected_programs(conn: &mut Db, database: &str, affected_tables: &BTreeSet<String>, schema_programs: &[NormalizedStoredProgram]) -> Result<Vec<NormalizedStoredProgram>, String> {
    let schema = NormalizedSchema { tables: affected_tables.iter()
        .map(|name| serde_json::from_value(json!({"name":name,"columns":[]})).unwrap()).collect() };
    let (mut programs, reasons) = crate::mysql_programs::collect_trigger_programs(conn, database, &schema, false)?;
    if !reasons.is_empty() { return Err(format!("stored program definitions cannot be saved: {reasons:?}")) }
    for (kind,name,_,_) in crate::mysql_schema_programs::target_schema_programs(conn, database)? {
        if !schema_programs.iter().any(|p| p.kind == kind && p.name.eq_ignore_ascii_case(&name)) { continue }
        let program = crate::mysql_schema_programs::read_schema_program(conn, database, &kind, &name)?;
        crate::mysql_schema_programs::schema_program_sql(&program, &name, Some(database))?;
        programs.push(program);
    }
    programs.sort_by(|a,b| (&a.kind, &a.name).cmp(&(&b.kind, &b.name)));
    reset_session(conn)?;
    Ok(programs)
}

fn final_program_inventory(plan: &PromotionPlan) -> Vec<ProgramName> {
    let mut names = plan.original_program_inventory.iter().filter(|(kind,name,table)| {
        if kind == "TRIGGER" { !table.as_ref().is_some_and(|t| plan.candidate_tables.contains_key(t)) }
        else { !plan.promoted_programs.iter().any(|p| &p.kind == kind && p.name.eq_ignore_ascii_case(name)) }
    }).cloned().collect::<Vec<_>>();
    names.extend(plan.promoted_programs.iter().map(|p| (p.kind.clone(),p.name.clone(),p.table.clone())));
    names.sort(); names
}

#[cfg(test)]
fn capture_programs(conn: &mut Db, database: &str) -> Result<Vec<NormalizedStoredProgram>, String> {
    let schema = NormalizedSchema { tables: tables(conn, database)?.into_iter().filter(|(_, kind)| kind != "VIEW")
        .map(|(name, _)| serde_json::from_value(json!({"name":name,"columns":[]})).unwrap()).collect() };
    let (mut programs, mut reasons) = crate::mysql_programs::collect_trigger_programs(conn, database, &schema, false)?;
    let (schema_programs, failures) = crate::mysql_schema_programs::collect_schema_programs(conn, database)?;
    programs.extend(schema_programs); reasons.extend(failures);
    if !reasons.is_empty() { return Err(format!("stored program definitions cannot be saved: {reasons:?}")) }
    programs.sort_by(|a,b| (&a.kind, &a.name).cmp(&(&b.kind, &b.name)));
    reset_session(conn)?;
    Ok(programs)
}

fn same_program(a: &NormalizedStoredProgram, b: &NormalizedStoredProgram) -> bool {
    a.kind == b.kind && a.name.eq_ignore_ascii_case(&b.name)
}

fn final_programs(plan: &PromotionPlan) -> Vec<NormalizedStoredProgram> {
    let mut programs = plan.original_programs.iter().filter(|p| {
        if p.kind == "TRIGGER" { !p.table.as_ref().is_some_and(|t| plan.candidate_tables.contains_key(t)) }
        else { !plan.promoted_programs.iter().any(|new| same_program(p, new)) }
    }).cloned().collect::<Vec<_>>();
    for p in &plan.promoted_programs {
        let mut p = p.clone();
        if p.kind == "EVENT" {
            let previous = plan.original_programs.iter().find(|old| same_program(old, &p));
            p.status = Some(if crate::mysql_schema_programs::event_enabled(p.status.as_deref(), previous.and_then(|p| p.status.as_deref())) { "ENABLED" } else { "DISABLED" }.into());
        }
        if plan.import_definer.is_some() { p.definer = plan.import_definer.clone(); }
        programs.push(p);
    }
    programs
}

fn program_matches(actual: &NormalizedStoredProgram, expected: &NormalizedStoredProgram) -> bool {
    if !same_program(actual, expected) || actual.definer != expected.definer || actual.table != expected.table || actual.timing != expected.timing
        || actual.event != expected.event || actual.sql_mode != expected.sql_mode
        || actual.character_set_client != expected.character_set_client || actual.collation_connection != expected.collation_connection
        || actual.time_zone != expected.time_zone || actual.status != expected.status { return false }
    if actual.kind == "TRIGGER" { actual.definition == expected.definition && actual.action_order == expected.action_order }
    else { crate::mysql_schema_programs::schema_program_sql(actual, &expected.name, None).ok()
        == crate::mysql_schema_programs::schema_program_sql(expected, &expected.name, None).ok() }
}

// Only explicit recreation warnings authorize a changed execution account.
fn apply_program_definers(programs: &mut [NormalizedStoredProgram], warnings: &[Value]) {
    for p in programs {
        for w in warnings {
            if matches!(w["code"].as_str(),Some("program_restore_definer_changed" | "program_definer_replaced"))
                && w["name"] == p.name && w["source"].as_str() == p.definer.as_deref()
                && w["kind"].as_str().is_none_or(|kind|kind==p.kind) {
                if let Some(target) = w["target"].as_str() { p.definer = Some(target.into()); }
            }
        }
    }
}

pub(crate) fn with_program_warnings(plan: &PromotionPlan, warnings: &[Value]) -> PromotionPlan {
    let mut plan = plan.clone();
    apply_program_definers(&mut plan.original_programs,&warnings.iter().filter(|w|w["code"]=="program_restore_definer_changed").cloned().collect::<Vec<_>>());
    apply_program_definers(&mut plan.promoted_programs,warnings);
    plan
}

fn matches_recorded_program(conn: &mut Db, database: &str, actual: &NormalizedStoredProgram, expected: &[NormalizedStoredProgram]) -> Result<bool, String> {
    for p in expected.iter().filter(|p| same_program(actual, p)) {
        if program_matches(actual, p) { return Ok(true) }
        if p.kind == "EVENT" && p.status != actual.status {
            crate::mysql_schema_programs::set_program_context(conn, p)?;
            let state = crate::mysql_schema_programs::event_state(conn, database, &p.name);
            reset_session(conn)?;
            let (status, completed) = state?;
            if crate::mysql_schema_programs::event_state_matches(&status, completed, p.status.as_deref().unwrap_or_default()) {
                let mut p = p.clone(); p.status = actual.status.clone();
                if program_matches(actual, &p) { return Ok(true) }
            }
        }
    }
    Ok(false)
}

fn missing_programs(conn: &mut Db, database: &str, expected: &[NormalizedStoredProgram]) -> Result<Vec<String>, String> {
    missing_recorded_programs(conn,database,expected,false)
}

fn missing_recorded_programs(conn: &mut Db, database: &str, expected: &[NormalizedStoredProgram], completed: bool) -> Result<Vec<String>,String> {
    let affected_tables = expected.iter().filter_map(|p| p.table.clone()).collect();
    let actual = capture_affected_programs(conn, database, &affected_tables, expected)?;
    let mut missing = Vec::new();
    for p in expected {
        let matched = match actual.iter().find(|a| same_program(a,p)) {
            Some(a) => matches_recorded_program(conn, database, a, std::slice::from_ref(p))?,
            None => completed && crate::mysql_schema_programs::recorded_event_completed(conn,p)?,
        };
        if !matched { missing.push(crate::mysql_programs::program_id(p)); }
    }
    missing.extend(actual.iter().filter(|p| !expected.iter().any(|e| same_program(e, p)))
        .map(|p| format!("unexpected:{}", crate::mysql_programs::program_id(p))));
    Ok(missing)
}

fn program_differences(conn: &mut Db, plan: &PromotionPlan, promoted: bool) -> Result<Vec<String>, String> {
    let expected = if promoted { final_programs(plan) } else { plan.original_programs.clone() };
    let mut missing = missing_programs(conn, &plan.original_database, &expected)?;
    let wanted = if promoted { final_program_inventory(plan) } else { plan.original_program_inventory.clone() };
    let actual = program_inventory(conn, &plan.original_database)?;
    let id = |(kind,name,table): &ProgramName| if kind == "TRIGGER" { format!("trigger:{}:{name}",table.as_deref().unwrap_or_default()) }
        else { format!("{}:{name}",if kind == "EVENT" { "event" } else { "routine" }) };
    missing.extend(wanted.iter().filter(|name| !actual.contains(name)).map(id));
    missing.extend(actual.iter().filter(|name| !wanted.contains(name)).map(|name|format!("unexpected:{}",id(name))));
    missing.sort(); missing.dedup();
    Ok(missing)
}

fn restored_program_differences(conn: &mut Db, plan: &PromotionPlan) -> Result<Vec<String>,String> {
    let mut missing = missing_programs(conn,&plan.original_database,&plan.original_programs)?;
    let actual = program_inventory(conn,&plan.original_database)?;
    for p in plan.promoted_programs.iter().filter(|p|!plan.original_programs.iter().any(|old|same_program(old,p))) {
        if actual.iter().any(|(kind,name,_)|kind==&p.kind && name.eq_ignore_ascii_case(&p.name)) {
            missing.push(format!("unexpected:{}",crate::mysql_programs::program_id(p)));
        }
    }
    missing.sort(); missing.dedup(); Ok(missing)
}

fn reset_session(conn: &mut Db) -> Result<(), String> {
    crate::mysql_schema_programs::reset_program_context(conn)?;
    conn.query_drop("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_AUTO_VALUE_ON_ZERO', foreign_key_checks=0")
        .map_err(|e| e.to_string())
}

fn drop_triggers(conn: &mut Db, database: &str, programs: &[NormalizedStoredProgram]) -> Result<(), String> {
    reset_session(conn)?;
    for p in programs.iter().filter(|p| p.kind == "TRIGGER") {
        conn.query_drop(format!("DROP TRIGGER IF EXISTS {}", qualified(database, &p.name))).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn moved_original_triggers(plan: &PromotionPlan) -> Vec<NormalizedStoredProgram> {
    plan.original_programs.iter().filter(|p| p.kind == "TRIGGER" && p.table.as_ref().is_some_and(|t| plan.original_tables.contains_key(t))).cloned().collect()
}

fn attach_triggers(conn: &mut Db, plan: &PromotionPlan, originals: bool, warnings: &mut Vec<Value>) -> Result<(), String> {
    conn.query_drop(format!("USE {}", q(&plan.original_database))).map_err(|e| e.to_string())?;
    let tables = if originals { plan.original_tables.keys().cloned().collect() } else { active_names(plan) };
    let programs = if originals { plan.original_programs.clone() } else { final_programs(plan) };
    attach_trigger_set(conn, plan, &programs, &tables, originals, warnings, &mut |_, _| Ok(()))
}

fn attach_trigger_set(conn: &mut Db, plan: &PromotionPlan, programs: &[NormalizedStoredProgram], tables: &BTreeSet<String>, originals: bool, warnings: &mut Vec<Value>, hook: &mut impl FnMut(&str, &mut Db) -> Result<(), String>) -> Result<(), String> {
    conn.query_drop(format!("USE {}", q(&plan.original_database))).map_err(|e|e.to_string())?;
    for p in crate::mysql_programs::selected_triggers(programs, tables) {
        let preserved = originals || plan.original_programs.iter().any(|old|program_matches(p,old)) || !plan.candidate_tables.contains_key(p.table.as_ref().unwrap());
        if preserved {
            crate::mysql_schema_programs::set_program_context(conn, p)?;
            let sql = plan.original_trigger_definitions.get(&p.name).ok_or("saved original trigger definition missing")?;
            match conn.query_drop(sql) {
                Ok(()) => {}
                Err(mysql::Error::MySqlError(err)) if err.code == 1227 => {
                    let sql = crate::mysql_programs::program_create_sql(p,&p.name,p.table.as_deref().unwrap())?;
                    conn.query_drop(sql).map_err(|e|e.to_string())?;
                    let current: Option<String> = conn.query_first("SELECT CURRENT_USER()").map_err(|e|e.to_string())?;
                    warnings.push(json!({"name":p.name,"kind":p.kind,"code":"program_restore_definer_changed","source":p.definer,"target":current,"previous_definer_preserved":false}));
                }
                Err(err) => return Err(err.to_string()),
            }
        } else {
            let source = plan.promoted_programs.iter().find(|source|same_program(p,source)).unwrap_or(p);
            warnings.extend(crate::mysql_programs::create_trigger(conn, source, &p.name, p.table.as_deref().unwrap())?);
        }
    }
    reset_session(conn)?;
    // No writes after attachment: a trigger may reference a table outside the WRITE lock.
    hook("before_trigger_verification", conn)?;
    let mut expected = programs.iter().filter(|p| p.kind == "TRIGGER" && p.table.as_ref().is_some_and(|t| tables.contains(t))).cloned().collect::<Vec<_>>();
    apply_program_definers(&mut expected,warnings);
    let affected = expected.iter().filter_map(|p| p.table.clone()).collect();
    let actual = capture_affected_programs(conn, &plan.original_database, &affected, &[])?.into_iter().filter(|p| p.kind == "TRIGGER").collect::<Vec<_>>();
    if actual != expected && expected.iter().any(|p| !actual.iter().any(|a| program_matches(a,p))) {
        return Err("trigger definitions could not be verified after attachment".into());
    }
    Ok(())
}

// Recovery attempts every DROP and still recreates the definitions when one DROP fails.
// The caller must own WRITE locks on all affected tables.
fn recover_triggers(conn: &mut Db, plan: &PromotionPlan, programs: &[NormalizedStoredProgram], state: &mut Value) -> Result<usize, String> {
    reset_session(conn)?;
    let mut errors = Vec::new();
    for p in programs.iter().filter(|p| p.kind == "TRIGGER") {
        if let Err(err) = conn.query_drop(format!("DROP TRIGGER IF EXISTS {}",qualified(&plan.original_database,&p.name))) { errors.push(err.to_string()); }
    }
    let tables = programs.iter().filter_map(|p|p.table.clone()).collect();
    let mut warnings = state["program_warnings"].as_array().cloned().unwrap_or_default();
    let actual = match capture_affected_programs(conn,&plan.original_database,&tables,&[]) {
        Ok(actual) => actual,
        Err(err) => { errors.push(err); Vec::new() }
    };
    let originals = programs.iter().all(|p|plan.original_programs.iter().any(|old|program_matches(p,old)));
    let mut attachment_errors = Vec::new();
    let mut recreated = 0;
    for p in crate::mysql_programs::selected_triggers(programs,&tables) {
        if actual.iter().any(|a|program_matches(a,p)) { continue }
        let tables = p.table.iter().cloned().collect();
        match attach_trigger_set(conn,plan,std::slice::from_ref(p),&tables,originals,&mut warnings,&mut |_,_|Ok(())) {
            Ok(()) => recreated += 1,
            Err(err) => attachment_errors.push(err),
        }
    }
    let result = if attachment_errors.is_empty() { Ok(recreated) } else { Err(attachment_errors.join("; ")) };
    state["program_warnings"] = json!(warnings);
    state["trigger_recovery_drop_errors"] = json!(errors);
    result
}

fn classify_programs(conn: &mut Db, plan: &PromotionPlan, status: String) -> String {
    if plan.original_programs.is_empty() && plan.promoted_programs.is_empty() && plan.original_program_inventory.is_empty() { return status }
    if status == "failed_original_unchanged" {
        let expected = moved_original_triggers(plan);
        if expected.is_empty() { return status }
        let affected = expected.iter().filter_map(|p| p.table.clone()).collect();
        return match capture_affected_programs(conn, &plan.original_database, &affected, &[]) {
            Ok(actual) if expected.iter().all(|p| actual.iter().any(|a| program_matches(a, p))) => status,
            _ => "failed_original_triggers_missing".into(),
        }
    }
    if status != "promoted" { return status }
    match program_differences(conn, plan, true) {
        Ok(missing) if missing.is_empty() => status,
        _ => "promoted_incomplete".into(),
    }
}

pub(crate) fn plan(
    original: &Endpoint,
    candidate: &Endpoint,
    restore_id: &str,
    programs: &[NormalizedStoredProgram],
) -> Result<PromotionPlan, String> {
    if original.engine != "mysql"
        || candidate.engine != "mysql"
        || original.host != candidate.host
        || original.port != candidate.port
        || original.database == candidate.database
    {
        return Err(
            "safe_promotion: distinct same-server MySQL original/candidate required".into(),
        );
    }
    for namespace in [&original.database, &candidate.database] {
        if ["mysql", "sys", "information_schema", "performance_schema"]
            .iter()
            .any(|name| namespace.eq_ignore_ascii_case(name))
        {
            return Err("system namespaces cannot be promotion targets".into());
        }
    }
    let mut conn = connect(original)?;
    prove_metadata_visibility(&mut conn)?;
    if !database_exists(&mut conn, &original.database)?
        || !database_exists(&mut conn, &candidate.database)?
    {
        return Err(
            "safe_promotion: both original and verified candidate must already exist".into(),
        );
    }
    let original_objects = tables(&mut conn, &original.database)?;
    let candidate_objects = tables(&mut conn, &candidate.database)?;
    let replaced = candidate_objects
        .iter()
        .filter(|(_, kind)| kind.as_str() != "VIEW")
        .map(|(name, _)| name.clone())
        .collect::<BTreeSet<_>>();
    if replaced.is_empty() {
        return Err("empty candidate".into());
    }
    for name in &replaced {
        if original_objects
            .get(name)
            .is_some_and(|kind| kind == "VIEW")
        {
            return Err("table/view kind collision".into());
        }
    }
    let (ofks, external) = foreign_keys(&mut conn, &original.database)?;
    let (cfks, c_external) = foreign_keys(&mut conn, &candidate.database)?;
    if !c_external.is_empty() {
        return Err("candidate has cross-schema foreign-key dependencies".into());
    }
    let (oviews, oexternal) = inspect_views(&mut conn, &original.database)?;
    let (cviews, cexternal) = inspect_views(&mut conn, &candidate.database)?;
    if !cexternal.is_empty() {
        return Err("candidate view has external dependencies".into());
    }
    let mut closure = replaced.clone();
    let mut affected_views = cviews.keys().cloned().collect::<BTreeSet<_>>();
    loop {
        let old = (closure.len(), affected_views.len());
        for fk in &ofks {
            if closure.contains(&fk.child) || closure.contains(&fk.parent) {
                closure.insert(fk.child.clone());
                closure.insert(fk.parent.clone());
            }
        }
        for (name, view) in &oviews {
            if affected_views.contains(name)
                || view
                    .dependencies
                    .iter()
                    .any(|dep| closure.contains(dep) || affected_views.contains(dep))
            {
                affected_views.insert(name.clone());
                // A preserved dependent view can join another target-only base
                // table. Include that data and its FK component in the fence.
                for dep in &view.dependencies {
                    if oviews.contains_key(dep) {
                        affected_views.insert(dep.clone());
                    } else {
                        closure.insert(dep.clone());
                    }
                }
            }
        }
        if old == (closure.len(), affected_views.len()) {
            break;
        }
    }
    if external.iter().any(|(cdb, c, pdb, p)| {
        cdb == &original.database && closure.contains(c)
            || pdb == &original.database && closure.contains(p)
    }) {
        return Err(
            "affected cross-schema foreign keys require a separately coordinated migration".into(),
        );
    }
    let existing = closure
        .iter()
        .filter(|name| original_objects.contains_key(*name))
        .cloned()
        .collect::<BTreeSet<_>>();
    reject_unsupported(&mut conn, &original.database, &existing)?;
    reject_unsupported(&mut conn, &candidate.database, &replaced)?;
    let original_views = oviews
        .into_iter()
        .filter(|(name, _)| affected_views.contains(name))
        .collect::<BTreeMap<_, _>>();
    if oexternal
        .iter()
        .any(|(name, _, _)| original_views.contains_key(name))
    {
        return Err("affected original view has cross-schema dependencies".into());
    }
    let mut replacement_views = original_views.clone();
    replacement_views.extend(cviews);
    for (name, view) in &replacement_views {
        if original_objects
            .get(name)
            .is_some_and(|kind| kind != "VIEW")
        {
            return Err("view/table kind collision".into());
        }
        for dep in &view.dependencies {
            if !closure.contains(dep) && !replacement_views.contains_key(dep) {
                return Err(format!("view {name} references uncovered object {dep}"));
            }
        }
    }
    let mut old = BTreeMap::new();
    for name in existing {
        old.insert(
            name.clone(),
            inspect_table(&mut conn, &original.database, &name, false)?,
        );
    }
    let mut new = BTreeMap::new();
    for name in replaced {
        new.insert(
            name.clone(),
            inspect_table(&mut conn, &candidate.database, &name, true)?,
        );
    }
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos()
        .to_string();
    let mut hidden = BTreeMap::new();
    let mut saved = BTreeMap::new();
    for (index, name) in replacement_views.keys().enumerate() {
        let alias = format!("tf_p_{nonce}_v{index}");
        let backup = format!("tf_p_{nonce}_oldv{index}");
        if original_objects.contains_key(&alias) || original_objects.contains_key(&backup) {
            return Err("owned view name collision".into());
        }
        hidden.insert(name.clone(), alias);
        if original_views.contains_key(name) {
            saved.insert(name.clone(), backup);
        }
    }
    let original_program_inventory = program_inventory(&mut conn, &original.database)?;
    let original_programs = capture_affected_programs(&mut conn, &original.database, &old.keys().cloned().collect(), programs)?;
    let unsafe_functions = crate::import::safe_restore::target_function_refusals(&mut conn,&original_programs)?;
    if !unsafe_functions.is_empty() { return Err(format!("target_routine_binlog_unsafe: {unsafe_functions:?}")) }
    if !program_inventory(&mut conn, &candidate.database)?.is_empty() {
        return Err("candidate must not contain stored programs".into());
    }
    let target_triggers = crate::mysql_programs::target_trigger_names(&mut conn, &original.database)?;
    let fold = mysql_folds_table_names(&mut conn)?;
    let target_triggers = target_triggers.into_iter().filter(|(table,_)|!old.keys().any(|name|if fold {name.eq_ignore_ascii_case(table)} else {name==table})).collect::<Vec<_>>();
    let selected = programs.iter().filter(|p| p.kind == "TRIGGER").collect::<Vec<_>>();
    let conflicts = crate::mysql_programs::trigger_name_conflicts(&target_triggers, &selected, fold);
    if !conflicts.is_empty() { return Err(format!("trigger_name_conflict: {}", conflicts.join(", "))) }
    let current_definer: Option<String> = conn.query_first("SELECT CURRENT_USER()").map_err(|e|e.to_string())?;
    let promoted_programs = programs.to_vec();
    let mut original_trigger_definitions = BTreeMap::new();
    for p in original_programs.iter().filter(|p| p.kind == "TRIGGER" && p.table.as_ref().is_some_and(|t| old.contains_key(t))) {
        let row: mysql::Row = conn.query_first(format!("SHOW CREATE TRIGGER {}", qualified(&original.database, &p.name)))
            .map_err(|e| e.to_string())?.ok_or("original trigger disappeared")?;
        original_trigger_definitions.insert(p.name.clone(), row.get("SQL Original Statement").ok_or("original trigger definition unreadable")?);
    }
    let mut result = PromotionPlan {
        restore_id: restore_id.into(), digest: String::new(), host: original.host.clone(), port: original.port,
        original_database: original.database.clone(), candidate_database: candidate.database.clone(),
        server_uuid: conn.query_first("SELECT @@server_uuid").map_err(|e|e.to_string())?.ok_or("server UUID unavailable")?,
        backup_database: format!("tf_backup_{nonce}"), clone_database: format!("tf_promote_{nonce}"), nonce,
        original_tables: old, candidate_tables: new,
        original_programs, original_program_inventory, promoted_programs, import_definer: if programs.is_empty() {None} else {current_definer}, original_trigger_definitions,
        original_fks: ofks.into_iter().filter(|f|closure.contains(&f.child)).collect(), candidate_fks: cfks,
        view_order: view_order(&replacement_views)?, original_views, replacement_views,
        hidden_views: hidden, saved_views: saved,
        warnings: vec![
            "Existing replaced-table writes before the cutover are intentionally superseded by the restored candidate.".into(),
            "The backup namespace contains only replaced tables. Unrelated original tables remain active at their existing names.".into(),
            "Saved old-view aliases retain definitions in the original namespace and still reference active final table names. They are recovery-definition artifacts, not views over backup data; native definitions are recorded in the promotion journal.".into(),
            "A retained backup does not include application writes accepted after promotion; rollback must not discard those writes.".into(),
        ],
    };
    verify_inventory(&mut conn, &result, false)?;
    prevalidate_final_views(&mut conn, &result)?;
    result.digest = hash(&result)?;
    Ok(result)
}

fn plan_digest(plan: &PromotionPlan) -> Result<String, String> {
    let mut copy = plan.clone();
    copy.digest.clear();
    hash(&copy)
}
fn program_summary(plan: &PromotionPlan) -> Value {
    let ids = |programs: Vec<&NormalizedStoredProgram>| programs.into_iter().map(crate::mysql_programs::program_id).collect::<Vec<_>>();
    let created = ids(plan.promoted_programs.iter().filter(|p| p.kind == "TRIGGER" || !plan.original_programs.iter().any(|old| same_program(old,p))).collect());
    let replaced = ids(plan.promoted_programs.iter().filter(|p| p.kind != "TRIGGER" && plan.original_programs.iter().any(|old| same_program(old,p))).collect());
    let kept = plan.original_program_inventory.iter().filter(|(kind,name,_)| kind != "TRIGGER" && !plan.promoted_programs.iter().any(|p| &p.kind == kind && p.name.eq_ignore_ascii_case(name)))
        .map(|(kind,name,_)| format!("{}:{name}",if kind == "EVENT" {"event"} else {"routine"})).collect::<Vec<_>>();
    let dropped = plan.original_programs.iter().filter(|p| p.kind == "TRIGGER" && p.table.as_ref().is_some_and(|t| plan.candidate_tables.contains_key(t))
        && !plan.promoted_programs.iter().any(|new| same_program(new,p)))
        .map(|p| json!({"table":p.table,"name":p.name})).collect::<Vec<_>>();
    json!({"programs":{"create":created,"replace":replaced},"programs_kept":kept,"triggers_dropped":dropped})
}

pub(crate) fn summary(plan: &PromotionPlan) -> Value {
    let normalize = |table: &NativeTable, from: &str| {
        let locations = plan
            .original_tables
            .keys()
            .chain(plan.candidate_tables.keys())
            .map(|name| (name.clone(), "namespace".to_string()))
            .collect();
        rewrite_table(table, from, "namespace", &locations)
            .map(|sql| static_ddl(&sql))
            .unwrap_or_else(|_| static_ddl(&table.ddl))
    };
    let changed = plan
        .candidate_tables
        .iter()
        .filter_map(|(name, new)| {
            plan.original_tables
                .get(name)
                .filter(|old| {
                    normalize(old, &plan.original_database)
                        != normalize(new, &plan.candidate_database)
                })
                .map(|_| name.clone())
        })
        .collect::<Vec<_>>();
    let mut row_counts = serde_json::Map::new();
    let mut data_changed = Vec::new();
    let mut data_not_compared = Vec::new();
    for (name, new) in &plan.candidate_tables {
        let old = plan.original_tables.get(name);
        let original_count = if let Some(old) = old {
            old.content_digest
                .as_ref()
                .and_then(|d| d.get("rows"))
                .cloned()
                .unwrap_or(Value::Null)
        } else {
            json!(0)
        };
        let candidate_count = new
            .content_digest
            .as_ref()
            .and_then(|d| d.get("rows"))
            .cloned()
            .unwrap_or(Value::Null);
        row_counts.insert(
            name.clone(),
            json!({"original":original_count,"candidate":candidate_count}),
        );
        match (
            old.and_then(|old| old.content_digest.as_ref()),
            new.content_digest.as_ref(),
        ) {
            (Some(before), Some(after)) => {
                if before != after {
                    data_changed.push(name.clone());
                }
            }
            (None, Some(after)) if old.is_none() => {
                if after["rows"].as_u64() != Some(0) {
                    data_changed.push(name.clone());
                }
            }
            _ => data_not_compared.push(name.clone()),
        }
    }
    json!({"selected_tables":plan.candidate_tables.keys().collect::<Vec<_>>(),
        "target_only_tables_preserved":plan.original_tables.keys().filter(|name|!plan.candidate_tables.contains_key(*name)).collect::<Vec<_>>(),
        "new_tables":plan.candidate_tables.keys().filter(|name|!plan.original_tables.contains_key(*name)).collect::<Vec<_>>(),
        "views_replaced":plan.replacement_views.keys().collect::<Vec<_>>(),"schema_changed_tables":changed,
        "row_counts":row_counts,"data_changed_tables":data_changed,"data_not_compared":data_not_compared,
        "backup_namespace":plan.backup_database,"backup_view_aliases":plan.saved_views,
        "lock_acquisition_timeout_seconds":2,"write_pause":"Readers and writers of the replaced tables and views wait under WRITE locks while the candidate is checked once more, then one atomic RENAME switches names.","warnings":plan.warnings})
}
fn assert_plan_binding(
    original: &Endpoint,
    candidate: &Endpoint,
    plan: &PromotionPlan,
    confirmed: &str,
) -> Result<(), String> {
    if confirmed != plan.digest || plan_digest(plan)? != plan.digest {
        return Err("promotion confirmation digest does not match the stored plan".into());
    }
    if original.host != plan.host
        || original.port != plan.port
        || original.database != plan.original_database
        || candidate.host != plan.host
        || candidate.port != plan.port
        || candidate.database != plan.candidate_database
        || original.engine != "mysql"
        || candidate.engine != "mysql"
    {
        return Err("promotion endpoint binding changed".into());
    }
    if !plan.nonce.bytes().all(|b| b.is_ascii_digit())
        || plan.backup_database != format!("tf_backup_{}", plan.nonce)
        || plan.clone_database != format!("tf_promote_{}", plan.nonce)
    {
        return Err("invalid owned promotion namespace".into());
    }
    Ok(())
}
fn verify_inventory(conn: &mut Db, plan: &PromotionPlan, check_data: bool) -> Result<(), String> {
    prove_metadata_visibility(conn)?;
    if program_inventory(conn, &plan.original_database)? != plan.original_program_inventory
        || capture_affected_programs(conn, &plan.original_database, &plan.original_tables.keys().cloned().collect(), &plan.original_programs)? != plan.original_programs {
        return Err("original stored programs changed after confirmation".into());
    }
    if !program_inventory(conn, &plan.candidate_database)?.is_empty() {
        return Err("candidate must not contain stored programs".into());
    }
    let uuid: Option<String> = conn
        .query_first("SELECT @@server_uuid")
        .map_err(|e| e.to_string())?;
    if uuid.as_deref() != Some(&plan.server_uuid) {
        return Err("server identity changed".into());
    }
    let objects = tables(conn, &plan.original_database)?;
    for (name, expected) in &plan.original_tables {
        let actual = inspect_table(conn, &plan.original_database, name, false)?;
        if actual.table_id != expected.table_id || actual.static_digest != expected.static_digest {
            return Err(format!(
                "original schema changed after confirmation: {name}"
            ));
        }
    }
    for (name, expected) in &plan.candidate_tables {
        if !plan.original_tables.contains_key(name) && objects.contains_key(name) {
            return Err(format!("new destination object appeared: {name}"));
        }
        let actual = inspect_table(conn, &plan.candidate_database, name, check_data)?;
        if actual.table_id != expected.table_id
            || actual.static_digest != expected.static_digest
            || actual.normalized.auto_increment != expected.normalized.auto_increment
            || check_data && actual.content_digest != expected.content_digest
        {
            return Err(format!(
                "verified candidate changed after confirmation: {name}"
            ));
        }
    }
    let (fks, external) = foreign_keys(conn, &plan.original_database)?;
    let affected = plan
        .original_tables
        .keys()
        .chain(plan.candidate_tables.keys())
        .collect::<BTreeSet<_>>();
    if fks
        .iter()
        .any(|fk| affected.contains(&fk.parent) && !affected.contains(&fk.child))
    {
        return Err("new inbound FK outside the confirmed closure".into());
    }
    let local = fks
        .into_iter()
        .filter(|fk| affected.contains(&fk.child))
        .collect::<Vec<_>>();
    if local != plan.original_fks {
        return Err("original FK dependency graph changed".into());
    }
    if external.iter().any(|(cdb, c, pdb, p)| {
        cdb == &plan.original_database && affected.contains(c)
            || pdb == &plan.original_database && affected.contains(p)
    }) {
        return Err("new cross-schema FK dependency".into());
    }
    let (candidate_fks, candidate_external) = foreign_keys(conn, &plan.candidate_database)?;
    if candidate_fks != plan.candidate_fks
        || candidate_external
            .iter()
            .any(|(db, child, parent_db, parent)| {
                db != &plan.clone_database
                    || !plan.original_tables.contains_key(child)
                    || plan.candidate_tables.contains_key(child)
                    || parent_db != &plan.candidate_database
                    || !plan.candidate_tables.contains_key(parent)
            })
    {
        return Err("candidate FK dependency graph changed".into());
    }
    let original_names = plan.original_tables.keys().cloned().collect();
    let candidate_names = plan.candidate_tables.keys().cloned().collect();
    reject_unsupported(conn, &plan.original_database, &original_names)?;
    reject_unsupported(conn, &plan.candidate_database, &candidate_names)?;
    let (views, external) = inspect_views(conn, &plan.original_database)?;
    for (name, expected) in &plan.original_views {
        if views.get(name) != Some(expected) {
            return Err(format!("original view changed: {name}"));
        }
    }
    for (name, view) in &views {
        if !plan.original_views.contains_key(name)
            && !plan.hidden_views.values().any(|n| n == name)
            && view.dependencies.iter().any(|d| {
                affected.contains(d)
                    || plan.original_views.contains_key(d)
                    || plan.replacement_views.contains_key(d)
            })
        {
            return Err(format!("new dependent view appeared: {name}"));
        }
    }
    if external
        .iter()
        .any(|(name, _, _)| plan.original_views.contains_key(name))
    {
        return Err("external view dependency changed".into());
    }
    let cross:Vec<(String,String)>=conn.exec("SELECT VIEW_SCHEMA,VIEW_NAME FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA=? AND VIEW_SCHEMA<>?",(&plan.original_database,&plan.original_database)).map_err(|e|e.to_string())?;
    if !cross.is_empty() {
        return Err("views in other schemas reference the original namespace; guarded promotion cannot cover them".into());
    }
    let cross_candidate:Option<u64>=conn.exec_first("SELECT COUNT(*) FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA=? AND VIEW_SCHEMA NOT IN (?,?)",(&plan.candidate_database,&plan.candidate_database,&plan.clone_database)).map_err(|e|e.to_string())?;
    if cross_candidate != Some(0) {
        return Err("new external view references the verified candidate".into());
    }
    let collisions:Vec<(String,String,String,String,String)>=conn.exec(
        "SELECT a.TABLE_SCHEMA,a.TABLE_NAME,b.TABLE_SCHEMA,b.TABLE_NAME,a.CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS a JOIN information_schema.TABLE_CONSTRAINTS b ON a.CONSTRAINT_NAME=b.CONSTRAINT_NAME AND a.CONSTRAINT_TYPE=b.CONSTRAINT_TYPE AND (a.TABLE_SCHEMA<b.TABLE_SCHEMA OR (a.TABLE_SCHEMA=b.TABLE_SCHEMA AND a.TABLE_NAME<b.TABLE_NAME)) WHERE a.CONSTRAINT_TYPE IN ('CHECK','FOREIGN KEY') AND a.TABLE_SCHEMA IN (?,?) AND b.TABLE_SCHEMA IN (?,?)",
        (&plan.original_database,&plan.candidate_database,&plan.original_database,&plan.candidate_database),
    ).map_err(|e|error("final constraint namespace collision check",e))?;
    let retained = |db: &str, table: &str| {
        db == plan.candidate_database && plan.candidate_tables.contains_key(table)
            || db == plan.original_database && (!plan.candidate_tables.contains_key(table))
    };
    if let Some((_, a, _, b, name)) = collisions
        .into_iter()
        .find(|(adb, a, bdb, b, _)| retained(adb, a) && retained(bdb, b))
    {
        return Err(format!(
            "final schema constraint name collision: {name} on {a} and {b}"
        ));
    }
    Ok(())
}
fn create_owned_database(conn: &mut Db, name: &str, original: &str) -> Result<(), String> {
    let (charset,collation):(String,String)=conn.exec_first("SELECT DEFAULT_CHARACTER_SET_NAME,DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=?",(original,)).map_err(|e|e.to_string())?.ok_or("original namespace disappeared")?;
    conn.query_drop(format!(
        "CREATE DATABASE {} CHARACTER SET {} COLLATE {}",
        q(name),
        q(&charset),
        q(&collation)
    ))
    .map_err(|e| error("create owned namespace (never reuse an existing name)", e))
}
fn warning_free(conn: &mut Db, sql: &str) -> Result<(), String> {
    conn.query_drop(sql).map_err(|e| e.to_string())?;
    check_warnings(conn)
}
fn check_warnings(conn: &mut Db) -> Result<(), String> {
    let count: Option<u64> = conn
        .query_first("SHOW COUNT(*) WARNINGS")
        .map_err(|e| e.to_string())?;
    if count.unwrap_or(0) > 0 {
        let warnings: Vec<(String, u32, String)> =
            conn.query("SHOW WARNINGS").map_err(|e| e.to_string())?;
        return Err(format!(
            "staged clone produced warnings; original remains unchanged: {warnings:?}"
        ));
    }
    Ok(())
}
fn schema_locations(plan: &PromotionPlan) -> BTreeMap<String, String> {
    plan.original_tables
        .keys()
        .chain(plan.candidate_tables.keys())
        .map(|name| {
            (
                name.clone(),
                if plan.candidate_tables.contains_key(name) {
                    plan.candidate_database.clone()
                } else {
                    plan.clone_database.clone()
                },
            )
        })
        .collect()
}
fn final_fks(plan: &PromotionPlan) -> Vec<ForeignKey> {
    let mut result = plan.candidate_fks.clone();
    result.extend(
        plan.original_fks
            .iter()
            .filter(|fk| !plan.candidate_tables.contains_key(&fk.child))
            .cloned(),
    );
    result
}
fn validate_fks(
    conn: &mut Db,
    plan: &PromotionPlan,
    locations: &BTreeMap<String, String>,
) -> Result<(), String> {
    for (i, fk) in final_fks(plan).iter().enumerate() {
        let child = locations.get(&fk.child).ok_or("child outside closure")?;
        let parent = locations.get(&fk.parent).ok_or("parent outside closure")?;
        let ca = format!("tf_fc{i}");
        let pa = format!("tf_fp{i}");
        let join = fk
            .columns
            .iter()
            .zip(&fk.parent_columns)
            .map(|(c, p)| format!("{}.{}={}.{}", q(&ca), q(c), q(&pa), q(p)))
            .collect::<Vec<_>>()
            .join(" AND ");
        let present = fk
            .columns
            .iter()
            .map(|c| format!("{}.{} IS NOT NULL", q(&ca), q(c)))
            .collect::<Vec<_>>()
            .join(" AND ");
        let sql=format!("SELECT COUNT(*) FROM {} AS {} LEFT JOIN {} AS {} ON {join} WHERE {present} AND {}.{} IS NULL",qualified(child,&fk.child),q(&ca),qualified(parent,&fk.parent),q(&pa),q(&pa),q(fk.parent_columns.first().ok_or("empty FK")?));
        let count: Option<u64> = conn
            .query_first(sql)
            .map_err(|e| error("validate staged FK graph", e))?;
        if count != Some(0) {
            return Err(format!(
                "rows are incompatible with their restored parent: {}.{}",
                fk.child, fk.name
            ));
        }
    }
    Ok(())
}
fn rename_sql(plan: &PromotionPlan) -> String {
    let mut renames = Vec::new();
    for name in plan.original_tables.keys() {
        renames.push(format!(
            "{} TO {}",
            qualified(&plan.original_database, name),
            qualified(&plan.backup_database, name)
        ));
    }
    for (name, db) in schema_locations(plan) {
        renames.push(format!(
            "{} TO {}",
            qualified(&db, &name),
            qualified(&plan.original_database, &name)
        ));
    }
    for (name, saved) in &plan.saved_views {
        renames.push(format!(
            "{} TO {}",
            qualified(&plan.original_database, name),
            qualified(&plan.original_database, saved)
        ));
    }
    for (name, hidden) in &plan.hidden_views {
        renames.push(format!(
            "{} TO {}",
            qualified(&plan.original_database, hidden),
            qualified(&plan.original_database, name)
        ));
    }
    format!("RENAME TABLE {}", renames.join(", "))
}
fn classify(
    conn: &mut Db,
    plan: &PromotionPlan,
    new_ids: &BTreeMap<String, u64>,
    cutover_session: u64,
) -> String {
    // A lost client response can leave RENAME running on the server. Do not
    // classify the pre-rename identities as a failure while that session exists.
    let mut ended = false;
    for _ in 0..40 {
        match conn.exec_first::<u64, _, _>(
            "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID=?",
            (cutover_session,),
        ) {
            Ok(Some(0)) => {
                ended = true;
                break;
            }
            Ok(_) => std::thread::sleep(std::time::Duration::from_millis(50)),
            Err(_) => return "cutover_unknown".into(),
        }
    }
    if !ended {
        return "cutover_unknown".into();
    }
    let old_ids = plan
        .original_tables
        .iter()
        .map(|(name, table)| (name.clone(), table.table_id))
        .collect();
    let status = classify_table_ids(&old_ids, new_ids, |backup, name| {
        optional_table_id(
            conn,
            if backup {
                &plan.backup_database
            } else {
                &plan.original_database
            },
            name,
        )
    });
    status
}
fn classify_table_ids<F: FnMut(bool, &str) -> Result<Option<u64>, String>>(
    old_ids: &BTreeMap<String, u64>,
    new_ids: &BTreeMap<String, u64>,
    mut lookup: F,
) -> String {
    if new_ids.is_empty() {
        return "cutover_unknown".into();
    }
    let mut old_intact = true;
    let mut promoted = true;
    for (name, id) in new_ids {
        let current = match lookup(false, name) {
            Ok(id) => id,
            Err(_) => return "cutover_unknown".into(),
        };
        if current != Some(*id) {
            promoted = false;
        }
        if let Some(old) = old_ids.get(name) {
            if current != Some(*old) {
                old_intact = false;
            }
        } else if current.is_some() {
            old_intact = false;
        }
    }
    for (name, old) in old_ids {
        let backup = match lookup(true, name) {
            Ok(id) => id,
            Err(_) => return "cutover_unknown".into(),
        };
        if backup != Some(*old) {
            promoted = false;
        }
    }
    if promoted {
        "promoted".into()
    } else if old_intact {
        "failed_original_unchanged".into()
    } else {
        "cutover_unknown".into()
    }
}

pub(crate) fn promote(
    original: &Endpoint,
    candidate: &Endpoint,
    plan: &PromotionPlan,
    confirmed_digest: &str,
    report_dir: &Path,
) -> Result<Value, String> {
    promote_with_hook(
        original,
        candidate,
        plan,
        confirmed_digest,
        report_dir,
        |_, _| Ok(()),
    )
}
fn promote_with_hook<F: FnMut(&str, &mut Db) -> Result<(), String>>(
    original: &Endpoint,
    candidate: &Endpoint,
    plan: &PromotionPlan,
    confirmed_digest: &str,
    report_dir: &Path,
    mut hook: F,
) -> Result<Value, String> {
    assert_plan_binding(original, candidate, plan, confirmed_digest)?;
    let journal_dir = report_dir.join(format!("_tunnelforge_promotion_{}", plan.nonce));
    std::fs::create_dir(&journal_dir).map_err(|e| {
        error(
            "create unique promotion journal; inspect prior attempts before retry",
            e,
        )
    })?;
    let report_path = dump_import_report_path(&journal_dir)?.display().to_string();
    let mut state = json!({"success":false,"status":"preparing","restore_id":plan.restore_id,"plan_digest":plan.digest,"original_database":plan.original_database,"candidate_database":plan.candidate_database,"backup_database":plan.backup_database,"backup_namespace":plan.backup_database,"clone_database":plan.clone_database,"backup_views":plan.saved_views,"original_unchanged":true,"report_path":report_path,"phase":"validation","plan":plan,"warnings":plan.warnings,"automatic_rollback":false});
    for (key, value) in program_summary(plan).as_object().unwrap() { state[key] = value.clone(); }
    write_dump_import_report(&journal_dir, &state)?;
    let mut conn = connect(original)?;
    let mut created_backup = false;
    let mut created_clone = false;
    let mut hidden_created = Vec::new();
    let mut locked = false;
    let mut triggers_detaching = false;
    let mut rename_attempted = false;
    let mut rename_committed = false;
    let mut rename_rejected = false;
    let mut recovered_locked = false;
    let mut new_ids = BTreeMap::new();
    let cutover_session: u64 = conn
        .query_first("SELECT CONNECTION_ID()")
        .map_err(|e| e.to_string())?
        .ok_or("cutover session unavailable")?;
    state["cutover_session_id"] = json!(cutover_session);
    let outcome = (|| -> Result<(), String> {
        verify_inventory(&mut conn, plan, true)?;
        prevalidate_final_views(&mut conn, plan)?;
        hook("validated", &mut conn)?;
        state["phase"] = json!("creating_owned_namespaces");
        write_dump_import_report(&journal_dir, &state)?;
        create_owned_database(&mut conn, &plan.backup_database, &plan.original_database)?;
        created_backup = true;
        create_owned_database(&mut conn, &plan.clone_database, &plan.original_database)?;
        created_clone = true;
        let mut locations = schema_locations(plan);
        for name in plan.replacement_views.keys() {
            locations.insert(name.clone(), plan.clone_database.clone());
        }
        conn.query_drop("SET SESSION foreign_key_checks=0")
            .map_err(|e| e.to_string())?;
        for (name, table) in &plan.original_tables {
            if !plan.candidate_tables.contains_key(name) {
                warning_free(
                    &mut conn,
                    &rewrite_table(
                        table,
                        &plan.original_database,
                        &plan.clone_database,
                        &locations,
                    )?,
                )?;
            }
        }
        for name in &plan.view_order {
            let view = &plan.replacement_views[name];
            let from = if plan.original_views.get(name) == Some(view) {
                &plan.original_database
            } else {
                &plan.candidate_database
            };
            warning_free(
                &mut conn,
                &rewrite_view(view, from, &plan.clone_database, name, &locations)?,
            )?;
            conn.query_drop(format!(
                "SELECT * FROM {} LIMIT 0",
                qualified(&plan.clone_database, name)
            ))
            .map_err(|e| error("candidate view definition is not preparable", e))?;
        }
        let final_locations = locations
            .keys()
            .map(|name| (name.clone(), plan.original_database.clone()))
            .collect();
        for name in &plan.view_order {
            let view = &plan.replacement_views[name];
            let from = if plan.original_views.get(name) == Some(view) {
                &plan.original_database
            } else {
                &plan.candidate_database
            };
            conn.query_drop(rewrite_view(
                view,
                from,
                &plan.original_database,
                &plan.hidden_views[name],
                &final_locations,
            )?)
            .map_err(|e| e.to_string())?;
            hidden_created.push(plan.hidden_views[name].clone());
            check_warnings(&mut conn)?;
            conn.query_drop(format!(
                "SELECT * FROM {} LIMIT 0",
                qualified(&plan.original_database, &plan.hidden_views[name])
            ))
            .map_err(|e| {
                error(
                    "final view is not preparable against the existing schema",
                    e,
                )
            })?;
        }
        verify_inventory(&mut conn, plan, true)?;
        let mut locks = BTreeSet::new();
        for name in plan.original_tables.keys() {
            locks.insert(format!(
                "{} WRITE",
                qualified(&plan.original_database, name)
            ));
        }
        for (name, db) in schema_locations(plan) {
            locks.insert(format!("{} WRITE", qualified(&db, &name)));
        }
        for name in plan.original_views.keys().chain(plan.hidden_views.values()) {
            locks.insert(format!(
                "{} WRITE",
                qualified(&plan.original_database, name)
            ));
        }
        let base_locations = schema_locations(plan);
        for (i, fk) in final_fks(plan).iter().enumerate() {
            let child = base_locations.get(&fk.child).ok_or("uncovered FK child")?;
            let parent = base_locations
                .get(&fk.parent)
                .ok_or("uncovered FK parent")?;
            locks.insert(format!(
                "{} AS {} READ",
                qualified(child, &fk.child),
                q(&format!("tf_fc{i}"))
            ));
            locks.insert(format!(
                "{} AS {} READ",
                qualified(parent, &fk.parent),
                q(&format!("tf_fp{i}"))
            ));
        }
        state["phase"] = json!("acquiring_write_locks");
        write_dump_import_report(&journal_dir, &state)?;
        hook("before_lock", &mut conn)?;
        conn.query_drop(format!(
            "LOCK TABLES {}",
            locks.into_iter().collect::<Vec<_>>().join(", ")
        ))
        .map_err(|e| error("bounded WRITE lock acquisition (2 seconds)", e))?;
        locked = true;
        hook("locked", &mut conn)?;
        verify_inventory(&mut conn, plan, true)?;
        state["phase"] = json!("refreshing_target_only_rows");
        write_dump_import_report(&journal_dir, &state)?;
        for (name, table) in &plan.original_tables {
            if plan.candidate_tables.contains_key(name) {
                continue;
            }
            warning_free(
                &mut conn,
                &format!("DELETE FROM {}", qualified(&plan.clone_database, name)),
            )?;
            if table.columns.is_empty() {
                return Err("target-only table has no writable columns".into());
            }
            let columns = table
                .columns
                .iter()
                .map(|n| q(n))
                .collect::<Vec<_>>()
                .join(", ");
            warning_free(
                &mut conn,
                &format!(
                    "INSERT INTO {} ({columns}) SELECT {columns} FROM {}",
                    qualified(&plan.clone_database, name),
                    qualified(&plan.original_database, name)
                ),
            )?;
            let counter:Option<u64>=conn.exec_first::<Option<u64>,_,_>("SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=? AND TABLE_NAME=?",(&plan.original_database,name)).map_err(|e|e.to_string())?.flatten();
            if let Some(counter) = counter {
                warning_free(
                    &mut conn,
                    &format!(
                        "ALTER TABLE {} AUTO_INCREMENT={counter}",
                        qualified(&plan.clone_database, name)
                    ),
                )?;
            }
            if content_digest(&mut conn, &plan.original_database, &table.normalized)?
                != content_digest(&mut conn, &plan.clone_database, &table.normalized)?
            {
                return Err(format!("target-only clone value checksum mismatch: {name}"));
            }
        }
        validate_fks(&mut conn, plan, &base_locations)?;
        for (name, db) in &base_locations {
            new_ids.insert(name.clone(), table_id(&mut conn, db, name)?);
        }
        state["new_table_ids"] = json!(new_ids);
        state["phase"] = json!("cutover_started");
        state["status"] = json!("cutover_unknown");
        state["original_unchanged"] = Value::Null;
        write_dump_import_report(&journal_dir, &state)?;
        // Definitions are already durable in the plan before the first trigger is detached.
        state["phase"] = json!("detaching_triggers");
        write_dump_import_report(&journal_dir, &state)?;
        triggers_detaching = true;
        drop_triggers(&mut conn, &plan.original_database, &moved_original_triggers(plan))?;
        state["phase"] = json!("triggers_detached");
        write_dump_import_report(&journal_dir, &state)?;
        hook("before_rename", &mut conn)?;
        rename_attempted = true;
        if let Err(err) = conn.query_drop(rename_sql(plan)) {
            rename_rejected = matches!(err,mysql::Error::MySqlError(_));
            if matches!(&err,mysql::Error::MySqlError(e) if e.code==1435) { return Err(error("atomic rename: an account with TRIGGER privilege is required to move tables with hidden triggers",err)) }
            return Err(error("atomic rename", err));
        }
        rename_committed = true;
        state["status"] = json!("promoted_incomplete");
        state["success"] = json!(false);
        state["original_unchanged"] = json!(false);
        state["phase"] = json!("promoted_locked");
        write_dump_import_report(&journal_dir, &state)?;
        hook("after_rename", &mut conn)?;
        let mut warnings = Vec::new();
        let attached = attach_trigger_set(&mut conn, plan, &final_programs(plan), &active_names(plan), false, &mut warnings, &mut hook);
        state["program_warnings"] = json!(warnings);
        attached?;
        state["phase"] = json!("triggers_attached");
        write_dump_import_report(&journal_dir, &state)?;
        hook("triggers_attached", &mut conn)?;
        conn.query_drop("UNLOCK TABLES").map_err(|e| e.to_string())?;
        locked = false;
        conn.query_drop(format!("USE {}", q(&plan.original_database))).map_err(|e| e.to_string())?;
        let mut imported = Vec::new();
        for events in [false, true] {
            for p in plan.promoted_programs.iter().filter(|p| p.kind != "TRIGGER" && (p.kind == "EVENT") == events) {
                state["pending_program"] = json!({"kind":p.kind,"name":p.name});
                write_dump_import_report(&journal_dir, &state)?;
                match crate::mysql_schema_programs::replace_schema_program(&mut conn, &plan.original_database, p, || {}) {
                    Ok((notes, created)) => { warnings.extend(notes); imported.push(created); }
                    Err(failure) => {
                        warnings.extend(failure["warnings"].as_array().into_iter().flatten().cloned());
                        warnings.extend(failure["restore_warnings"].as_array().into_iter().flatten().cloned());
                        state["program_warnings"] = json!(warnings);
                        state["programs_failed"] = json!([failure]);
                        return Err("Some stored programs could not be restored. Check the saved report for the missing objects.".into());
                    }
                }
                state["programs_imported"] = json!(imported);
                state["program_warnings"] = json!(warnings);
                state["pending_program"] = Value::Null;
                write_dump_import_report(&journal_dir, &state)?;
            }
        }
        let effective = with_program_warnings(plan,&warnings);
        let missing = program_differences(&mut conn, &effective, true)?;
        if !missing.is_empty() {
            state["missing_programs"] = json!(missing); return Err("Stored program restoration could not be verified.".into())
        }
        state["phase"] = json!("programs_replaced");
        state["status"] = json!("promoted");
        state["success"] = json!(true);
        write_dump_import_report(&journal_dir, &state)?;
        hook("programs_replaced", &mut conn)?;
        Ok(())
    })();
    if outcome.is_err() && locked && triggers_detaching && !rename_committed && (!rename_attempted || rename_rejected)
        && !moved_original_triggers(plan).is_empty() && conn.query_first::<u8,_>("SELECT 1").ok().flatten()==Some(1) {
        let recovery = hook("before_original_trigger_recovery", &mut conn)
            .and_then(|_|recover_triggers(&mut conn,plan,&moved_original_triggers(plan),&mut state));
        recovered_locked = true;
        if let Err(err) = recovery { state["trigger_recovery_error"] = json!(err); }
    }
    if locked {
        let _ = conn.query_drop("UNLOCK TABLES");
    }
    drop(conn);
    if let Err(failure) = outcome {
        let mut cleanup = connect(original).ok();
        let mut status = if !triggers_detaching {
            "failed_original_unchanged".into()
        } else if rename_committed {
            cleanup.as_mut().map(|db| classify_programs(db, plan, "promoted".into())).unwrap_or_else(|| "promoted_incomplete".into())
        } else if rename_attempted {
            cleanup
                .as_mut()
                .map(|db| classify(db, plan, &new_ids, cutover_session))
                .unwrap_or_else(|| "cutover_unknown".into())
        } else {
            cleanup.as_mut().map(|db| {
                if plan.original_tables.iter().all(|(name,t)| optional_table_id(db, &plan.original_database, name).ok().flatten() == Some(t.table_id)) { "failed_original_unchanged".into() } else { "cutover_unknown".into() }
            })
                .unwrap_or_else(|| if !triggers_detaching || plan.original_trigger_definitions.is_empty() { "failed_original_unchanged".into() } else { "failed_original_triggers_missing".into() })
        };
        if let Some(db) = cleanup.as_mut() {
            if status == "failed_original_unchanged" && triggers_detaching && !recovered_locked && !moved_original_triggers(plan).is_empty() {
                let originals = moved_original_triggers(plan);
                let recovery = (|| -> Result<(), String> {
                    hook("before_original_trigger_recovery", db)?;
                    let locks = plan.original_tables.keys().map(|name| format!("{} WRITE", qualified(&plan.original_database,name))).collect::<Vec<_>>();
                    if !locks.is_empty() { db.query_drop(format!("LOCK TABLES {}",locks.join(", "))).map_err(|e|e.to_string())?; }
                    let result = recover_triggers(db, plan, &originals, &mut state);
                    let _ = db.query_drop("UNLOCK TABLES");
                    result.map(|_|())
                })();
                if let Err(err) = recovery { state["trigger_recovery_error"] = json!(err); }
            }
            let effective = with_program_warnings(plan,state["program_warnings"].as_array().map(Vec::as_slice).unwrap_or_default());
            if triggers_detaching { status = classify_programs(db, &effective, status); }
        }
        if let Some(db) = cleanup.as_mut().filter(|_|triggers_detaching) {
            let effective = with_program_warnings(plan,state["program_warnings"].as_array().map(Vec::as_slice).unwrap_or_default());
            match program_differences(db, &effective, matches!(status.as_str(), "promoted" | "promoted_incomplete")) {
                Ok(missing) => state["missing_programs"] = json!(missing),
                Err(err) => state["program_verification_error"] = json!(err),
            }
        }
        state["status"] = json!(status);
        state["success"] = json!(status == "promoted");
        state["error"] = json!(redact_endpoint_secret(&failure, original));
        state["original_unchanged"] = if status == "failed_original_unchanged" {
            json!(true)
        } else if matches!(status.as_str(), "promoted" | "promoted_incomplete" | "failed_original_triggers_missing") {
            json!(false)
        } else {
            Value::Null
        };
        if matches!(status.as_str(), "failed_original_unchanged" | "failed_original_triggers_missing") {
            let mut cleanup_errors = Vec::new();
            if let Some(db) = cleanup.as_mut() {
                for name in hidden_created.iter().rev() {
                    if let Err(e) = db.query_drop(format!(
                        "DROP VIEW {}",
                        qualified(&plan.original_database, name)
                    )) {
                        cleanup_errors.push(e.to_string());
                    }
                }
                if created_clone {
                    if let Err(e) =
                        db.query_drop(format!("DROP DATABASE {}", q(&plan.clone_database)))
                    {
                        cleanup_errors.push(e.to_string());
                    }
                }
                if created_backup {
                    if let Err(e) =
                        db.query_drop(format!("DROP DATABASE {}", q(&plan.backup_database)))
                    {
                        cleanup_errors.push(e.to_string());
                    }
                }
            } else {
                cleanup_errors.push(
                    "owned preparatory objects retained: cleanup connection unavailable".into(),
                );
            }
            state["cleanup_errors"] = json!(cleanup_errors);
        }
    } else {
        state["phase"] = json!("completed");
    }
    state["message"]=json!(match state["status"].as_str() { Some("promoted")=>"Verified candidate promoted at the original database name. Original tables are retained in the recorded backup namespace.",Some("failed_original_unchanged")=>"Promotion did not change original tables or their foreign keys. The verified candidate remains available.",Some("promoted_incomplete")=>"Tables were promoted, but stored programs are missing. Check the report before recovery.",Some("failed_original_triggers_missing")=>"Tables were not promoted, but original triggers could not be restored. Recover them from the definitions saved in the report.",_=>"Cutover outcome is unknown. Retain all recorded namespaces and inspect table identities before any retry or recovery." });
    state["interrupted_attempt_policy"]=json!("A checkpoint at cutover_started is UNKNOWN after process loss. Compare recorded InnoDB table identities at original/candidate/backup locations; never retry or roll back blindly.");
    if matches!(state["status"].as_str(), Some("promoted" | "promoted_incomplete")) {
        // Retention evidence for TF-STATUS-119: content/definition digests of the
        // backup tables right after the cutover, so a later cleanup can prove the
        // backup was not modified.
        match record_backup_fingerprint(original, plan) {
            Ok(fingerprint) => state["backup_fingerprint"] = fingerprint,
            Err(err) => state["backup_fingerprint_warning"] = json!(err),
        }
    }
    if let Err(err) = write_dump_import_report(&journal_dir, &state) {
        state["journal_error"] = json!(err);
    }
    Ok(state)
}

fn record_backup_fingerprint(original: &Endpoint, plan: &PromotionPlan) -> Result<Value, String> {
    let mut conn = connect(original)?;
    let mut out = serde_json::Map::new();
    for name in plan.original_tables.keys() {
        let table = inspect_table(&mut conn, &plan.backup_database, name, true)?;
        out.insert(name.clone(), json!({"static_digest": table.static_digest, "content_digest": table.content_digest}));
    }
    Ok(Value::Object(out))
}

// ---------------------------------------------------------------------------
// Backup lifecycle (TF-STATUS-119): read-only inspection and explicit drop of the
// owned backup namespace. Ownership is proven by the InnoDB table ids the
// promotion journal recorded for the original tables.
// ---------------------------------------------------------------------------

fn lifecycle_connect(endpoint: &Endpoint, plan: &PromotionPlan) -> Result<Db, String> {
    if endpoint.host != plan.host || endpoint.port != plan.port {
        return Err("backup lifecycle endpoint does not match the promotion journal".into());
    }
    connect(endpoint)
}

/// Objects and counts of one namespace: (exists, base tables, views).
pub(crate) fn namespace_counts(endpoint: &Endpoint, namespace: &str) -> Result<(bool, u64, u64), String> {
    let mut conn = connect(endpoint)?;
    if !database_exists(&mut conn, namespace)? {
        return Ok((false, 0, 0));
    }
    let present = tables(&mut conn, namespace)?;
    let views = present.values().filter(|engine| engine.as_str() == "VIEW").count() as u64;
    Ok((true, present.len() as u64 - views, views))
}

/// Foreign keys, views and routines outside `namespace` that mention it.
pub(crate) fn external_references(endpoint: &Endpoint, namespace: &str) -> Result<Vec<String>, String> {
    let mut conn = connect(endpoint)?;
    prove_metadata_visibility(&mut conn)?;
    let mut found = Vec::new();
    let fks: Vec<(String, String, String)> = conn.exec(
        "SELECT DISTINCT TABLE_SCHEMA,TABLE_NAME,CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_SCHEMA=? AND TABLE_SCHEMA<>?",
        (namespace, namespace),
    ).map_err(|e| error("external foreign key scan", e))?;
    found.extend(fks.into_iter().map(|(s, t, c)| format!("foreign_key:{s}.{t}:{c}")));
    let views: Vec<(String, String)> = conn.exec(
        "SELECT DISTINCT VIEW_SCHEMA,VIEW_NAME FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA=? AND VIEW_SCHEMA<>?",
        (namespace, namespace),
    ).map_err(|e| error("external view scan", e))?;
    found.extend(views.into_iter().map(|(s, v)| format!("view:{s}.{v}")));
    // Routine/trigger/event bodies cannot be resolved structurally; a textual
    // mention is treated as a reference (fail closed).
    let needle = format!("%{namespace}%");
    for (sql, kind) in [
        ("SELECT ROUTINE_SCHEMA,ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA<>? AND ROUTINE_DEFINITION LIKE ?", "routine"),
        ("SELECT TRIGGER_SCHEMA,TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA<>? AND ACTION_STATEMENT LIKE ?", "trigger"),
        ("SELECT EVENT_SCHEMA,EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA<>? AND EVENT_DEFINITION LIKE ?", "event"),
    ] {
        let rows: Vec<(String, String)> = conn.exec(sql, (namespace, &needle)).map_err(|e| error("routine text scan", e))?;
        found.extend(rows.into_iter().map(|(s, n)| format!("{kind}_text:{s}.{n}")));
    }
    Ok(found)
}

/// Inspect the promotion backup database. `deep` reads every row (digests, exact
/// counts, metadata-visibility proof, external references); otherwise only
/// catalog identities and estimates are used.
#[cfg(test)]
pub(crate) fn inspect_backup(endpoint: &Endpoint, plan: &PromotionPlan, deep: bool, journal_fingerprint: Option<&Value>) -> Result<Value, String> {
    inspect_backup_recorded(endpoint,plan,deep,journal_fingerprint,false)
}

pub(crate) fn inspect_backup_recorded(endpoint: &Endpoint, plan: &PromotionPlan, deep: bool, journal_fingerprint: Option<&Value>, completed: bool) -> Result<Value,String> {
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let mut blockers: Vec<String> = Vec::new();
    let server_uuid: String = conn.query_first("SELECT @@server_uuid").map_err(|e| e.to_string())?.unwrap_or_default();
    let same_server = server_uuid == plan.server_uuid;
    if !same_server {
        blockers.push("the server identity differs from the one recorded by the promotion journal".into());
    }
    let backup = plan.backup_database.as_str();
    let exists = database_exists(&mut conn, backup)?;
    let old_ids: BTreeMap<String, u64> = plan.original_tables.iter().map(|(n, t)| (n.clone(), t.table_id)).collect();
    let mut result = json!({"namespace": backup, "exists": exists, "engine": "mysql",
        "ownership": "missing", "contents_exact": false, "verdict": "undeterminable",
        "tables": [], "rows_estimated": !deep, "external_references": [], "unchanged": null,
        "saved_view_aliases": plan.saved_views,
        "saved_view_alias_note": "Saved view aliases keep old definitions but reference the active table names; they are not views over backup data."});
    if !same_server {
        result["blockers"] = json!(blockers);
        return Ok(result);
    }
    // Verdict of the cutover itself, from InnoDB identities.
    let new_ids: BTreeMap<String, u64> = plan.candidate_tables.iter().map(|(n, t)| (n.clone(), t.table_id)).collect();
    let verdict = classify_table_ids(&old_ids, &new_ids, |in_backup, name| {
        let db = if in_backup { &plan.backup_database } else { &plan.original_database };
        optional_table_id(&mut conn, db, name)
    });
    let verdict = if completed { verdict } else { classify_programs(&mut conn,plan,verdict) };
    if matches!(verdict.as_str(), "promoted_incomplete" | "failed_original_triggers_missing") {
        match program_differences(&mut conn,plan,verdict=="promoted_incomplete") {
            Ok(missing) => result["missing_programs"] = json!(missing),
            Err(err) => result["program_verification_error"] = json!(err),
        }
    }
    result["verdict"] = json!(match verdict.as_str() {
        "promoted" => "promoted",
        "promoted_incomplete" => "promoted_incomplete",
        "failed_original_triggers_missing" => "failed_original_triggers_missing",
        "failed_original_unchanged" => "not_promoted",
        _ => "undeterminable",
    });
    if !exists {
        blockers.push("backup namespace does not exist".into());
        result["blockers"] = json!(blockers);
        return Ok(result);
    }
    let present = tables(&mut conn, backup)?;
    let expected: BTreeSet<&String> = old_ids.keys().collect();
    let actual: BTreeSet<&String> = present.keys().collect();
    let exact = expected == actual && present.values().all(|engine| engine != "VIEW");
    result["contents_exact"] = json!(exact);
    if !exact {
        blockers.push("backup namespace does not contain exactly the recorded original tables (unknown or missing objects)".into());
    }
    let mut owned = exact;
    for (name, id) in &old_ids {
        if optional_table_id(&mut conn, backup, name)? != Some(*id) {
            owned = false;
        }
    }
    result["ownership"] = json!(if owned { "proven" } else { "unproven" });
    if !owned {
        blockers.push("ownership is not proven: table identities differ from the promotion journal".into());
    }
    let mut table_rows = Vec::new();
    let mut unchanged = owned;
    if deep && exact {
        if let Err(problem) = prove_metadata_visibility(&mut conn) {
            blockers.push(problem);
        }
        let recorded = journal_fingerprint.and_then(Value::as_object);
        if recorded.is_none() {
            unchanged = false;
            blockers.push("the journal has no content fingerprint (written by an older version); an unmodified backup cannot be proven".into());
        }
        for name in plan.original_tables.keys() {
            let now = inspect_table(&mut conn, backup, name, true)?;
            if let Some(recorded) = recorded {
                let now_json = json!({"static_digest": now.static_digest, "content_digest": now.content_digest});
                if recorded.get(name) != Some(&now_json) {
                    unchanged = false;
                    blockers.push(format!("table {name} changed after promotion (definition or content differs from the journal)"));
                }
            }
            let rows: Option<u64> = conn.query_first(format!("SELECT COUNT(*) FROM {}", qualified(backup, name))).map_err(|e| e.to_string())?;
            table_rows.push(json!({"name": name, "rows": rows.unwrap_or(0)}));
        }
        result["unchanged"] = json!(unchanged);
        for (sql, what) in [
            ("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=?", "triggers"),
            ("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=?", "routines"),
            ("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?", "events"),
        ] {
            let count: Option<u64> = conn.exec_first(sql, (backup,)).map_err(|e| e.to_string())?;
            if count.unwrap_or(0) > 0 {
                blockers.push(format!("backup namespace contains {what}, which the journal does not cover"));
            }
        }
    } else {
        let estimates: Vec<(String, Option<u64>)> = conn.exec("SELECT TABLE_NAME,TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA=?", (backup,)).map_err(|e| e.to_string())?;
        table_rows = estimates.into_iter().map(|(name, rows)| json!({"name": name, "rows": rows.unwrap_or(0)})).collect();
    }
    result["tables"] = json!(table_rows);
    if deep {
        drop(conn);
        let references = external_references(endpoint, backup)?;
        if !references.is_empty() {
            blockers.push(format!("objects outside the backup still reference it: {}", references.join(", ")));
        }
        result["external_references"] = json!(references);
    }
    result["blockers"] = json!(blockers);
    Ok(result)
}

/// Drop one owned namespace. Callers must have verified ownership, contents and
/// references immediately before.
pub(crate) fn drop_namespace(endpoint: &Endpoint, namespace: &str) -> Result<(), String> {
    let mut conn = connect(endpoint)?;
    conn.query_drop(format!("DROP DATABASE {}", q(namespace))).map_err(|e| error("drop owned namespace", e))
}

/// Backup namespaces on the server that follow the promotion naming but are not
/// (or no longer) covered by any journal the caller supplied.
pub(crate) fn backup_named_namespaces(endpoint: &Endpoint) -> Result<Vec<String>, String> {
    let mut conn = connect(endpoint)?;
    conn.query("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'tf\\_backup\\_%' ORDER BY SCHEMA_NAME").map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Guided recovery (TF-STATUS-119): the inverse of the promotion. One atomic
// multi-object RENAME puts the retained original tables back and moves the
// active closure into a new owned backup namespace (nothing is deleted).
// Supported only for promotions without views and only while every recorded
// proof still holds; everything else is refused.
// ---------------------------------------------------------------------------

/// Names of the tables that are active at the original name after the promotion.
fn active_names(plan: &PromotionPlan) -> BTreeSet<String> {
    schema_locations(plan).into_keys().collect()
}

/// Checks that must hold both when the plan is reviewed and again under the write
/// locks. Returns blockers (empty = recoverable) and, for the preview, table rows.
fn rollback_blockers(
    conn: &mut Db,
    plan: &PromotionPlan,
    fingerprint: Option<&Value>,
    new_ids: Option<&Value>,
    displaced: Option<&str>,
    rows_out: Option<&mut Vec<Value>>,
) -> Result<Vec<String>, String> {
    let mut blockers = Vec::new();
    if !plan.replacement_views.is_empty() || !plan.original_views.is_empty() || !plan.saved_views.is_empty() {
        blockers.push("this promotion involved views; guided recovery of views is not supported".into());
    }
    let uuid: Option<String> = conn.query_first("SELECT @@server_uuid").map_err(|e| e.to_string())?;
    if uuid.as_deref() != Some(&plan.server_uuid) {
        blockers.push("the server identity differs from the promotion journal".into());
        return Ok(blockers);
    }
    if let Err(problem) = prove_metadata_visibility(conn) {
        blockers.push(problem);
    }
    if let Some(displaced) = displaced {
        if database_exists(conn, displaced)? {
            blockers.push(format!("the recovery backup namespace {displaced} already exists"));
        }
    }
    let Some(fingerprint) = fingerprint.and_then(Value::as_object) else {
        blockers.push("the journal has no backup fingerprint (written by an older version)".into());
        return Ok(blockers);
    };
    let Some(new_ids) = new_ids.and_then(Value::as_object) else {
        blockers.push("the journal has no record of the promoted table identities".into());
        return Ok(blockers);
    };
    let backup = plan.backup_database.as_str();
    let original = plan.original_database.as_str();
    // The retained originals: exactly the recorded tables, same identities, unmodified.
    let present = tables(conn, backup)?;
    if present.keys().collect::<BTreeSet<_>>() != plan.original_tables.keys().collect::<BTreeSet<_>>()
        || present.values().any(|engine| engine == "VIEW")
    {
        blockers.push("the backup namespace does not contain exactly the recorded original tables".into());
        return Ok(blockers);
    }
    for (name, table) in &plan.original_tables {
        if optional_table_id(conn, backup, name)? != Some(table.table_id) {
            blockers.push(format!("backup table {name} is not the recorded original (identity differs)"));
            continue;
        }
        let now = inspect_table(conn, backup, name, true)?;
        let now_json = json!({"static_digest": now.static_digest, "content_digest": now.content_digest});
        if fingerprint.get(name) != Some(&now_json) {
            blockers.push(format!("backup table {name} changed after promotion"));
        }
    }
    // The active tables: the promoted identities with exactly the promoted content.
    let active = active_names(plan);
    let listed = tables(conn, original)?;
    let mut rows = Vec::new();
    for name in &active {
        if !listed.contains_key(name) {
            blockers.push(format!("active table {name} is missing"));
            continue;
        }
        if new_ids.get(name).and_then(Value::as_u64) != optional_table_id(conn, original, name)? {
            blockers.push(format!("active table {name} is not the promoted table (identity differs)"));
            continue;
        }
        // A target-only clone was verified identical to its (now backed-up) original under the cutover locks.
        let baseline = baseline_digest(plan, fingerprint, name);
        let normalized = plan.candidate_tables.get(name).or_else(|| plan.original_tables.get(name)).map(|t| &t.normalized);
        let now = content_digest(conn, original, normalized.ok_or("table shape missing")?)?;
        if baseline.as_ref() != Some(&now) {
            blockers.push(format!("active table {name} was modified after promotion; recovery would discard those writes"));
        }
        let count: Option<u64> = conn.query_first(format!("SELECT COUNT(*) FROM {}", qualified(original, name))).map_err(|e| e.to_string())?;
        rows.push(json!({"name": name, "rows": count.unwrap_or(0)}));
    }
    // Dependencies: nothing outside the closure may depend on the active tables, and the
    // foreign-key graph inside it must be the promoted one.
    let (fks, external) = foreign_keys(conn, original)?;
    if external.iter().any(|(cdb, c, pdb, p)| (cdb == original && active.contains(c)) || (pdb == original && active.contains(p))) {
        blockers.push("cross-namespace foreign keys involve the active tables".into());
    }
    if fks.iter().any(|fk| active.contains(&fk.parent) && !active.contains(&fk.child)) {
        blockers.push("tables outside the promoted set now reference the active tables".into());
    }
    let mut local: Vec<ForeignKey> = fks.into_iter().filter(|fk| active.contains(&fk.child)).collect();
    let mut expected = final_fks(plan);
    local.sort_by(|a, b| (&a.child, &a.name).cmp(&(&b.child, &b.name)));
    expected.sort_by(|a, b| (&a.child, &a.name).cmp(&(&b.child, &b.name)));
    if local != expected {
        blockers.push("the foreign-key graph of the active tables changed after promotion".into());
    }
    let views: Option<u64> = conn.exec_first("SELECT COUNT(*) FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA=? OR VIEW_SCHEMA=?", (original, original)).map_err(|e| e.to_string())?;
    if views.unwrap_or(0) > 0 {
        blockers.push("views now depend on tables in the original namespace".into());
    }
    if !plan.original_trigger_definitions.is_empty() {
        blockers.extend(crate::import::safe_restore::target_trigger_refusals(conn,original)?.into_iter().map(|(code,name,message)|format!("{code}: {}: {message}",name.unwrap_or_default())));
    }
    for (needed,privilege) in [
        (plan.original_programs.iter().chain(&plan.promoted_programs).any(|p|matches!(p.kind.as_str(),"FUNCTION" | "PROCEDURE")),"CREATE ROUTINE"),
        (plan.promoted_programs.iter().any(|p|matches!(p.kind.as_str(),"FUNCTION" | "PROCEDURE")),"ALTER ROUTINE"),
        (plan.original_programs.iter().chain(&plan.promoted_programs).any(|p|p.kind=="EVENT"),"EVENT"),
    ] {
        if needed && crate::import::import_preview::mysql_grants_allow(conn,privilege,Some(original))==Some(false) {
            blockers.push(format!("{privilege} is required to restore saved programs"));
        }
    }
    let mut recreate = Vec::new();
    for previous in plan.original_programs.iter().filter(|p|p.kind=="FUNCTION") {
        let actual = crate::mysql_schema_programs::read_schema_program(conn,original,"FUNCTION",&previous.name);
        if !actual.as_ref().is_ok_and(|actual|crate::mysql_schema_programs::schema_program_matches(actual,previous)) { recreate.push(previous.clone()); }
    }
    blockers.extend(crate::import::safe_restore::target_function_refusals(conn,&recreate)?.into_iter().map(|(code,name,message)|format!("{code}: {}: {message}",name.unwrap_or_default())));
    reset_session(conn)?;
    let expected = final_programs(plan);
    let scopes = expected.iter().chain(&plan.original_programs).cloned().collect::<Vec<_>>();
    let affected = active_names(plan);
    for actual in capture_affected_programs(conn, original, &affected, &scopes)? {
        let previous_pending = plan.promoted_programs.iter().any(|p| same_program(&actual, p))
            && matches_recorded_program(conn, original, &actual, &plan.original_programs)?;
        if !matches_recorded_program(conn, original, &actual, &expected)? && !previous_pending {
            blockers.push(format!("A recorded stored program changed after promotion: {}", crate::mysql_programs::program_id(&actual)));
        }
    }
    if !program_inventory(conn, backup)?.is_empty() {
        blockers.push("The backup database contains unexpected stored programs.".into());
    }
    if let Some(out) = rows_out {
        *out = rows;
    }
    Ok(blockers)
}

/// Read-only review of the recovery: what returns, what is displaced, blockers.
pub(crate) fn rollback_plan(
    endpoint: &Endpoint,
    plan: &PromotionPlan,
    fingerprint: Option<&Value>,
    new_ids: Option<&Value>,
    displaced: &str,
) -> Result<Value, String> {
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let mut rows = Vec::new();
    let blockers = rollback_blockers(&mut conn, plan, fingerprint, new_ids, Some(displaced), Some(&mut rows))?;
    let restore: Vec<Value> = plan.original_tables.keys().map(|n| json!({"name": n})).collect();
    Ok(json!({"engine": "mysql", "blockers": blockers, "displace": rows, "restore": restore,
        "displaced_backup": displaced, "backup_namespace": plan.backup_database,
        "note": "The active tables are moved into a new retained backup namespace; nothing is deleted."}))
}

/// Executes the recovery. Everything is re-verified under WRITE locks; the single
/// RENAME either happens completely or not at all.
pub(crate) fn rollback_apply(
    endpoint: &Endpoint,
    plan: &PromotionPlan,
    fingerprint: Option<&Value>,
    new_ids: Option<&Value>,
    displaced: &str,
    journal_dir: &Path,
) -> Result<Value, String> {
    rollback_apply_with_hook(endpoint, plan, fingerprint, new_ids, displaced, journal_dir, |_,_|Ok(()))
}

fn rollback_apply_with_hook(mut_endpoint: &Endpoint, plan: &PromotionPlan, fingerprint: Option<&Value>, new_ids: Option<&Value>, displaced: &str, journal_dir: &Path, mut hook: impl FnMut(&str, &mut Db) -> Result<(), String>) -> Result<Value, String> {
    let endpoint = mut_endpoint;
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let blockers = rollback_blockers(&mut conn, plan, fingerprint, new_ids, Some(displaced), None)?;
    if !blockers.is_empty() {
        return Err(format!("recovery is blocked: {}", blockers.join("; ")));
    }
    let original = plan.original_database.as_str();
    let backup = plan.backup_database.as_str();
    let active = active_names(plan);
    let scopes = final_programs(plan).into_iter().chain(plan.original_programs.clone()).collect::<Vec<_>>();
    let start_programs = capture_affected_programs(&mut conn, original, &active, &scopes)?;
    let start_triggers = start_programs.iter().filter(|p|p.kind == "TRIGGER").cloned().collect::<Vec<_>>();
    let session: u64 = conn.query_first("SELECT CONNECTION_ID()").map_err(|e|e.to_string())?.ok_or("missing connection id")?;
    let old_ids: BTreeMap<String, u64> = plan.original_tables.iter().map(|(n, t)| (n.clone(), t.table_id)).collect();
    let new_ids_map: BTreeMap<String, u64> = new_ids.and_then(Value::as_object).into_iter().flatten().filter_map(|(k, v)| v.as_u64().map(|id| (k.clone(), id))).collect();
    let displaced_digests: BTreeMap<String, Value> = match fingerprint.and_then(Value::as_object) {
        Some(fp) => active.iter().filter_map(|n| baseline_digest(plan, fp, n).map(|d| (n.clone(), d))).collect(),
        None => BTreeMap::new(),
    };
    let mut state = json!({"success": false, "status": "preparing", "restore_id": plan.restore_id,
        "original_database": original, "backup_database": backup, "displaced_backup": displaced,
        "displaced_tables": new_ids_map, "displaced_digests": displaced_digests, "restored_tables": old_ids, "phase": "verified"});
    write_dump_import_report(journal_dir, &state)?;
    let mut locked = false;
    let mut created = false;
    let mut renamed = false;
    let mut rename_attempted = false;
    let mut rename_rejected = false;
    let mut recovered_locked = false;
    let mut triggers_detaching = false;
    let mut triggers_recreated = false;
    let outcome = (|| -> Result<(), String> {
        hook("before_create", &mut conn)?;
        create_owned_database(&mut conn, displaced, original)?;
        created = true;
        conn.query_drop("SET SESSION foreign_key_checks=0").map_err(|e| e.to_string())?;
        let mut locks = BTreeSet::new();
        for name in &active {
            locks.insert(format!("{} WRITE", qualified(original, name)));
        }
        for name in plan.original_tables.keys() {
            locks.insert(format!("{} WRITE", qualified(backup, name)));
        }
        for (i, fk) in final_fks(plan).iter().enumerate() {
            locks.insert(format!("{} AS {} READ", qualified(original, &fk.child), q(&format!("tf_fc{i}"))));
            locks.insert(format!("{} AS {} READ", qualified(original, &fk.parent), q(&format!("tf_fp{i}"))));
        }
        let restored_offset = final_fks(plan).len();
        for (i, fk) in plan.original_fks.iter().enumerate() {
            locks.insert(format!("{} AS {} READ", qualified(backup, &fk.child), q(&format!("tf_fc{}", restored_offset + i))));
            locks.insert(format!("{} AS {} READ", qualified(backup, &fk.parent), q(&format!("tf_fp{}", restored_offset + i))));
        }
        hook("before_lock", &mut conn)?;
        conn.query_drop(format!("LOCK TABLES {}", locks.into_iter().collect::<Vec<_>>().join(", ")))
            .map_err(|e| error("bounded WRITE lock acquisition (2 seconds)", e))?;
        locked = true;
        hook("locked", &mut conn)?;
        // Re-verify under the locks (content cannot change any more).
        let blockers = rollback_blockers(&mut conn, plan, fingerprint, new_ids, None, None)?;
        if !blockers.is_empty() {
            return Err(format!("recovery is blocked under lock: {}", blockers.join("; ")));
        }
        let mut renames = Vec::new();
        for name in &active {
            renames.push(format!("{} TO {}", qualified(original, name), qualified(displaced, name)));
        }
        for name in plan.original_tables.keys() {
            renames.push(format!("{} TO {}", qualified(backup, name), qualified(original, name)));
        }
        state["phase"] = json!("detaching_triggers");
        write_dump_import_report(journal_dir, &state)?;
        triggers_detaching = true;
        drop_triggers(&mut conn, original, &start_triggers)?;
        state["phase"] = json!("triggers_detached");
        write_dump_import_report(journal_dir, &state)?;
        state["phase"] = json!("rename_started");
        state["status"] = json!("rollback_unknown");
        write_dump_import_report(journal_dir, &state)?;
        hook("before_rename", &mut conn)?;
        rename_attempted = true;
        if let Err(err) = conn.query_drop(format!("RENAME TABLE {}", renames.join(", "))) {
            rename_rejected = matches!(err,mysql::Error::MySqlError(_));
            return Err(error("atomic recovery rename", err));
        }
        renamed = true;
        state["phase"] = json!("tables_restored");
        write_dump_import_report(journal_dir, &state)?;
        let mut warnings = state["program_warnings"].as_array().cloned().unwrap_or_default();
        let attached = attach_triggers(&mut conn, plan, true, &mut warnings);
        state["program_warnings"] = json!(warnings);
        attached?;
        state["phase"] = json!("triggers_attached");
        write_dump_import_report(journal_dir, &state)?;
        conn.query_drop("UNLOCK TABLES").map_err(|e| e.to_string())?;
        locked = false;
        conn.query_drop(format!("USE {}", q(original))).map_err(|e| e.to_string())?;
        for p in plan.promoted_programs.iter().filter(|p| p.kind != "TRIGGER") {
            state["pending_program"] = json!({"kind":p.kind,"name":p.name});
            write_dump_import_report(journal_dir, &state)?;
            if let Some(previous) = plan.original_programs.iter().find(|old| same_program(old,p)) {
                let mut restored = json!({});
                let result = crate::mysql_schema_programs::restore_schema_program(&mut conn, original, previous, &mut restored);
                restored["kind"] = json!(previous.kind); restored["name"] = json!(previous.name);
                restored["previous_restored"] = json!(result.is_ok());
                if let Err(err) = result {
                    restored["restore_error"] = json!(err);
                    let mut failed = state["programs_failed"].as_array().cloned().unwrap_or_default();
                    failed.push(restored.clone()); state["programs_failed"] = json!(failed);
                }
                let mut warnings = state["program_warnings"].as_array().cloned().unwrap_or_default();
                warnings.extend(restored["restore_warnings"].as_array().into_iter().flatten().cloned());
                state["program_warnings"] = json!(warnings);
                let mut results = state["programs_restored"].as_array().cloned().unwrap_or_default();
                results.push(restored); state["programs_restored"] = json!(results);
                write_dump_import_report(journal_dir,&state)?;
            } else {
                reset_session(&mut conn)?;
                if let Err(err) = conn.query_drop(format!("DROP {} IF EXISTS {}", p.kind, qualified(original, &p.name))) {
                    let mut failed = state["programs_failed"].as_array().cloned().unwrap_or_default();
                    failed.push(json!({"kind":p.kind,"name":p.name,"error":err.to_string()})); state["programs_failed"] = json!(failed);
                    write_dump_import_report(journal_dir,&state)?;
                }
            }
        }
        if let Some(failure) = state["programs_failed"].as_array().and_then(|items|items.first()).cloned() {
            state["pending_program"] = json!({"kind":failure["kind"],"name":failure["name"]});
            let error = failure["restore_error"].as_str().or_else(||failure["error"].as_str()).unwrap_or("stored program recovery failed");
            state["error"] = json!(redact_endpoint_secret(error,endpoint));
        } else {
            state["pending_program"] = Value::Null;
        }
        state["phase"] = json!("programs_restored");
        write_dump_import_report(journal_dir, &state)?;
        Ok(())
    })();
    if locked && outcome.is_err() && triggers_detaching && !renamed && (!rename_attempted || rename_rejected)
        && conn.query_first::<u8,_>("SELECT 1").ok().flatten()==Some(1) {
        recovered_locked = true;
        match recover_triggers(&mut conn, plan, &start_triggers, &mut state) {
            Ok(count) => triggers_recreated = count > 0,
            Err(err) => state["trigger_recovery_error"] = json!(err),
        }
    }
    if locked { let _ = conn.query_drop("UNLOCK TABLES"); }
    drop(conn);
    let mut verify = connect(endpoint).ok();
    let status = match verify.as_mut() {
        None => "rollback_unknown".to_string(),
        Some(db) => {
            if triggers_detaching {
                let mut ended = false;
                for _ in 0..40 {
                    if db.exec_first::<u64,_,_>("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID=?",(session,)).ok().flatten() == Some(0) { ended = true; break; }
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
                if !ended { state["session_outcome_unknown"] = json!(true); }
            }
            // Classify by identity: a lost reply must not be read as "nothing happened".
            let id_of = |db: &mut Db, ns: &str, n: &str| optional_table_id(db, ns, n).ok().flatten();
            let restored = old_ids.iter().all(|(n, id)| id_of(db, original, n) == Some(*id));
            let displaced_ok = new_ids_map.iter().all(|(n, id)| id_of(db, displaced, n) == Some(*id));
            let intact = new_ids_map.iter().all(|(n, id)| id_of(db, original, n) == Some(*id));
            if state["session_outcome_unknown"] == true { "rollback_unknown".into() }
            else if restored && displaced_ok {
                let effective = with_program_warnings(plan,state["program_warnings"].as_array().map(Vec::as_slice).unwrap_or_default());
                match restored_program_differences(db, &effective) {
                    Ok(missing) => { state["missing_programs"] = json!(missing); if missing.is_empty() { "rolled_back".into() } else { "rollback_incomplete".into() } }
                    Err(err) => { state["program_verification_error"] = json!(err); "rollback_incomplete".into() }
                }
            } else if intact && !renamed {
                if triggers_detaching && !recovered_locked && !start_triggers.is_empty() {
                    let locks = active.iter().map(|n|format!("{} WRITE",qualified(original,n))).collect::<Vec<_>>();
                    let result = db.query_drop(format!("LOCK TABLES {}",locks.join(", "))).map_err(|e|e.to_string()).and_then(|_|recover_triggers(db,plan,&start_triggers,&mut state));
                    let _ = db.query_drop("UNLOCK TABLES");
                    match result {
                        Ok(count) => triggers_recreated = count > 0,
                        Err(err) => state["trigger_recovery_error"] = json!(err),
                    }
                }
                if missing_programs(db, original, &start_programs).is_ok_and(|m|m.is_empty()) {
                    if triggers_recreated { "failed_triggers_recreated".into() } else { "failed_no_change".into() }
                } else { "rollback_unknown".into() }
            } else {
                "rollback_unknown".to_string()
            }
        }
    };
    state["triggers_recreated"] = json!(triggers_recreated);
    if triggers_recreated {
        let mut warnings = state["program_warnings"].as_array().cloned().unwrap_or_default();
        warnings.push(json!({"code":"rollback_triggers_recreated","message":"Recovery recreated the starting triggers; their creation metadata may differ."}));
        state["program_warnings"] = json!(warnings);
    }
    if matches!(status.as_str(), "failed_no_change" | "failed_triggers_recreated") && created {
        if let Some(db) = verify.as_mut() {
            let _ = db.query_drop(format!("DROP DATABASE {}", q(displaced)));
        }
    }
    state["status"] = json!(status);
    state["success"] = json!(status == "rolled_back");
    state["phase"] = json!(if status == "rolled_back" { "completed" } else { "finished" });
    if let Err(failure) = &outcome {
        state["error"] = json!(redact_endpoint_secret(failure, endpoint));
    }
    if let Err(err) = write_dump_import_report(journal_dir, &state) {
        state["journal_error"] = json!(err);
    }
    Ok(state)
}

pub(crate) fn inspect_rollback(endpoint: &Endpoint, plan: &PromotionPlan, record: &Value) -> Result<Option<Vec<String>>,String> {
    let mut conn = lifecycle_connect(endpoint,plan)?;
    if !plan.original_tables.iter().all(|(name,t)|optional_table_id(&mut conn,&plan.original_database,name).ok().flatten()==Some(t.table_id)) { return Ok(None) }
    if record["status"]=="rolled_back" { return Ok(Some(Vec::new())) }
    let Some(displaced) = record["displaced_backup"].as_str() else { return Ok(None) };
    let Some(ids) = record["displaced_tables"].as_object() else { return Ok(None) };
    if !ids.iter().all(|(name,id)|optional_table_id(&mut conn,displaced,name).ok().flatten()==id.as_u64()) { return Ok(None) }
    let effective = with_program_warnings(plan,record["program_warnings"].as_array().map(Vec::as_slice).unwrap_or_default());
    restored_program_differences(&mut conn,&effective).map(Some)
}

/// Content digest a promoted table must still have: the verified candidate digest, or
/// (for a target-only clone) the digest of its now-retained original.
fn baseline_digest(plan: &PromotionPlan, fingerprint: &serde_json::Map<String, Value>, name: &str) -> Option<Value> {
    match plan.candidate_tables.get(name) {
        Some(candidate) => candidate.content_digest.clone(),
        None => fingerprint.get(name).map(|f| f["content_digest"].clone()),
    }
}

/// Objects left in the candidate database after a promotion that the plan does not
/// record (recorded leftovers are only the candidate's own views).
pub(crate) fn candidate_unrecorded(endpoint: &Endpoint, plan: &PromotionPlan) -> Result<Vec<String>, String> {
    let mut conn = lifecycle_connect(endpoint, plan)?;
    if !database_exists(&mut conn, &plan.candidate_database)? {
        return Ok(vec![]);
    }
    let present = tables(&mut conn, &plan.candidate_database)?;
    Ok(present
        .into_iter()
        .filter(|(name, engine)| !(engine == "VIEW" && plan.replacement_views.contains_key(name)))
        .map(|(name, _)| name)
        .collect())
}

/// The retained namespace a recovery created: ownership by recorded table identities,
/// exact contents, unchanged content, no outside references.
pub(crate) fn inspect_displaced(
    endpoint: &Endpoint,
    plan: &PromotionPlan,
    record: &Value,
    deep: bool,
) -> Result<Value, String> {
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let namespace = record["displaced_backup"].as_str().unwrap_or("").to_string();
    let ids: BTreeMap<String, u64> = record["displaced_tables"].as_object().into_iter().flatten().filter_map(|(k, v)| v.as_u64().map(|id| (k.clone(), id))).collect();
    let digests = record["displaced_digests"].as_object().cloned().unwrap_or_default();
    let exists = !namespace.is_empty() && database_exists(&mut conn, &namespace)?;
    let mut blockers: Vec<String> = Vec::new();
    let mut result = json!({"namespace": namespace, "exists": exists, "engine": "mysql", "ownership": "missing",
        "contents_exact": false, "tables": [], "external_references": [], "unchanged": null, "rows_estimated": !deep});
    if !exists {
        blockers.push("recovery backup namespace does not exist".into());
        result["blockers"] = json!(blockers);
        return Ok(result);
    }
    let present = tables(&mut conn, &namespace)?;
    let exact = present.keys().collect::<BTreeSet<_>>() == ids.keys().collect::<BTreeSet<_>>() && present.values().all(|e| e != "VIEW");
    result["contents_exact"] = json!(exact);
    let mut owned = exact && !ids.is_empty();
    for (name, id) in &ids {
        if optional_table_id(&mut conn, &namespace, name)? != Some(*id) {
            owned = false;
        }
    }
    result["ownership"] = json!(if owned { "proven" } else { "unproven" });
    if !owned {
        blockers.push("ownership is not proven: the recovery backup does not hold exactly the recorded tables".into());
    }
    let mut rows_out = Vec::new();
    let mut unchanged = owned;
    if deep && owned {
        if let Err(problem) = prove_metadata_visibility(&mut conn) {
            blockers.push(problem);
        }
        for name in ids.keys() {
            let shape = plan.candidate_tables.get(name).or_else(|| plan.original_tables.get(name)).map(|t| &t.normalized).ok_or("table shape missing")?;
            if digests.get(name) != Some(&content_digest(&mut conn, &namespace, shape)?) {
                unchanged = false;
                blockers.push(format!("table {name} changed after the recovery"));
            }
            let rows: Option<u64> = conn.query_first(format!("SELECT COUNT(*) FROM {}", qualified(&namespace, name))).map_err(|e| e.to_string())?;
            rows_out.push(json!({"name": name, "rows": rows.unwrap_or(0)}));
        }
        result["unchanged"] = json!(unchanged);
        drop(conn);
        let references = external_references(endpoint, &namespace)?;
        if !references.is_empty() {
            blockers.push(format!("objects outside the backup still reference it: {}", references.join(", ")));
        }
        result["external_references"] = json!(references);
    }
    result["tables"] = json!(rows_out);
    result["blockers"] = json!(blockers);
    Ok(result)
}

/// The promotion's temporary clone database: (exists, objects the plan does not record).
/// Only the replacement views prepared there are recorded; every table was renamed out.
pub(crate) fn clone_unrecorded(endpoint: &Endpoint, plan: &PromotionPlan) -> Result<(bool, Vec<String>), String> {
    let mut conn = lifecycle_connect(endpoint, plan)?;
    if !database_exists(&mut conn, &plan.clone_database)? {
        return Ok((false, vec![]));
    }
    let present = tables(&mut conn, &plan.clone_database)?;
    Ok((true, present
        .into_iter()
        .filter(|(name, engine)| !(engine == "VIEW" && plan.replacement_views.contains_key(name)))
        .map(|(name, _)| name)
        .collect()))
}

/// Tables and views outside the dump that this plan would rebuild from their SHOW CREATE text
/// because an FK or a view links them to the dump (TF-STATUS-173). `target_only_views` are the
/// original's views the dump does not have, as the restore listed them.
pub(crate) fn rebuilt_outside_dump(plan: &PromotionPlan, target_only_views: &BTreeSet<String>) -> Vec<String> {
    let views = plan.replacement_views.keys().filter(|name| target_only_views.contains(*name));
    plan.original_tables.keys().filter(|name| !plan.candidate_tables.contains_key(*name)).chain(views).cloned().collect()
}

/// The saved old-view aliases (`tf_p_<nonce>_oldv<i>`) still in the original database, and what
/// stops their removal: an alias must still hold the view definition it saved, and nothing may use it.
pub(crate) fn saved_view_aliases(endpoint: &Endpoint, plan: &PromotionPlan) -> Result<(Vec<String>, Vec<String>), String> {
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let uuid: Option<String> = conn.query_first("SELECT @@server_uuid").map_err(|e| e.to_string())?;
    if uuid.as_deref() != Some(plan.server_uuid.as_str()) {
        return Ok((Vec::new(), vec!["the server identity differs from the promotion journal".into()]));
    }
    let mut blockers = Vec::new();
    if let Err(problem) = prove_metadata_visibility(&mut conn) {
        blockers.push(problem);
    }
    let database = &plan.original_database;
    let present = tables(&mut conn, database)?;
    let mut aliases = Vec::new();
    for (view, alias) in &plan.saved_views {
        match present.get(alias).map(String::as_str) {
            None => continue,
            Some("VIEW") => {}
            Some(_) => {
                blockers.push(format!("{alias} is no longer a view"));
                continue;
            }
        }
        let row: Option<mysql::Row> = conn.query_first(format!("SHOW CREATE VIEW {}", qualified(database, alias))).map_err(|e| e.to_string())?;
        let current = row.and_then(|row| row.get::<String, _>(1));
        let saved = plan.original_views.get(view).map(|saved| saved.ddl.as_str());
        let same = match (current.as_deref().map(view_select), saved.map(view_select)) {
            (Some(Ok(now)), Some(Ok(then))) => now == then,
            _ => false,
        };
        if !same {
            blockers.push(format!("{alias} no longer holds the saved definition of {view}"));
        }
        let users: Vec<(String, String)> = conn.exec(
            "SELECT VIEW_SCHEMA, VIEW_NAME FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA=? AND TABLE_NAME=?", (database, alias),
        ).map_err(|e| error("saved view alias use scan", e))?;
        blockers.extend(users.into_iter().map(|(schema, user)| format!("view {schema}.{user} uses {alias}")));
        // Routine, trigger and event bodies cannot be resolved structurally; a mention counts.
        for (sql, kind) in [
            ("SELECT ROUTINE_SCHEMA, ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_DEFINITION LIKE ?", "routine"),
            ("SELECT TRIGGER_SCHEMA, TRIGGER_NAME FROM information_schema.TRIGGERS WHERE ACTION_STATEMENT LIKE ?", "trigger"),
            ("SELECT EVENT_SCHEMA, EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_DEFINITION LIKE ?", "event"),
        ] {
            let rows: Vec<(String, String)> = conn.exec(sql, (format!("%{alias}%"),)).map_err(|e| error("saved view alias text scan", e))?;
            blockers.extend(rows.into_iter().map(|(schema, name)| format!("{kind} {schema}.{name} mentions {alias}")));
        }
        aliases.push(alias.clone());
    }
    Ok((aliases, blockers))
}

/// Drop saved view aliases the caller re-checked with `saved_view_aliases` just before.
pub(crate) fn drop_saved_view_aliases(endpoint: &Endpoint, plan: &PromotionPlan, aliases: &[String]) -> Result<(), String> {
    if aliases.is_empty() || aliases.iter().any(|alias| !plan.saved_views.values().any(|saved| saved == alias)) {
        return Err("only saved view aliases the promotion journal records can be dropped".into());
    }
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let names = aliases.iter().map(|alias| qualified(&plan.original_database, alias)).collect::<Vec<_>>();
    conn.query_drop(format!("DROP VIEW {}", names.join(", "))).map_err(|e| error("drop saved view aliases", e))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{mpsc, Mutex};
    static LIVE_LOCK: Mutex<()> = Mutex::new(());
    include!("../tests/support/safe_promote_round2.rs");

    #[test]
    fn safe_promote_rollback_lock_failure_leaves_active_triggers_untouched() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // holder: which table another session keeps open (SR metadata lock) during recovery.
        let mut report = Vec::new();
        for holder in ["original_parent", "backup_parent"] {
            let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
            // A second active-table trigger, ordered before the dump trigger on `parent`.
            conn.query_drop(format!("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.score=NEW.score+1",
                qualified(&original.database,"child_guard"),qualified(&original.database,"child"))).unwrap();
            let p=plan(&original,&candidate,"rollback_lock",&programs).unwrap();
            let result=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
            let triggers=|db:&mut Db|->Vec<(String,String,String)>{ db.exec(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, CONCAT(CREATED,'|',DEFINER) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? ORDER BY TRIGGER_NAME",
                (&original.database,)).unwrap() };
            let before=triggers(&mut conn);
            let recovery_dir=dir.join("recovery"); std::fs::create_dir(&recovery_dir).unwrap();
            let displaced=format!("tf_backup_recovery_{}",p.nonce);
            let reviewed=rollback_plan(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced).unwrap();
            let held_db = if holder=="original_parent" { &p.original_database } else { &p.backup_database };
            let mut reader=connect(&original).unwrap();
            reader.query_drop("BEGIN").unwrap();
            reader.query_drop(format!("SELECT * FROM {} LIMIT 1",qualified(held_db,"parent"))).unwrap();
            std::thread::sleep(std::time::Duration::from_millis(30));
            let recovered=rollback_apply(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced,&recovery_dir).unwrap();
            reader.query_drop("ROLLBACK").unwrap();
            drop(reader);
            let after=triggers(&mut conn);
            eprintln!("HOLDER={holder}\nPROMOTE_STATUS={}\nREVIEW_BLOCKERS={}\nRECOVERED_STATUS={}\nRECOVERED_ERROR={}\nTRIGGER_RECOVERY_ERROR={}\nBEFORE={before:?}\nAFTER={after:?}",
                result["status"],reviewed["blockers"],recovered["status"],recovered["error"],recovered["trigger_recovery_error"]);
            report.push((holder,result["status"].clone(),reviewed["blockers"].clone(),recovered["status"].clone(),before,after));
            let _=conn.query_drop(format!("DROP DATABASE IF EXISTS {}",q(&displaced)));
            cleanup(&mut conn,&p,&dir);
        }
        // Expected: a recovery that never acquired its locks touches nothing.
        for (holder,promoted,blockers,status,before,after) in report {
            assert_eq!(promoted,"promoted","{holder}");
            assert_eq!(blockers,json!([]),"{holder}");
            assert_eq!(after,before,"{holder}: active triggers were dropped/recreated although the recovery failed before detaching them");
            assert_eq!(status,"failed_no_change","{holder}");
        }
    }
    #[test]
    fn safe_promote_killed_cutover_session_recovers_original_triggers() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // ok_after_kill=true: RENAME is sent on the killed connection (lost-connection RENAME).
        // ok_after_kill=false: the connection dies before RENAME is sent (recovery runs on the dead conn).
        for ok_after_kill in [true, false] {
            let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
            conn.query_drop(format!("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW FOLLOWS guard SET NEW.note=CONCAT(NEW.note,'!')",qualified(&original.database,"a_later"),qualified(&original.database,"parent"))).unwrap();
            let before=capture_programs(&mut conn,&original.database).unwrap();
            let old_parent=table_id(&mut conn,&original.database,"parent").unwrap();
            let p=plan(&original,&candidate,"killed_cutover",&programs).unwrap();
            let mut killed=None;
            let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|phase,db| {
                if phase=="before_rename" {
                    let id:u64=db.query_first("SELECT CONNECTION_ID()").map_err(|e|e.to_string())?.unwrap();
                    let mut killer=connect(&original)?;
                    killer.query_drop(format!("KILL {id}")).map_err(|e|e.to_string())?;
                    killed=Some(id);
                    std::thread::sleep(std::time::Duration::from_millis(300));
                    if !ok_after_kill { return Err("connection lost before RENAME".into()) }
                }
                Ok(())
            }).unwrap();
            let after=capture_programs(&mut conn,&original.database).unwrap();
            let guard_present=after.iter().any(|x|x.kind=="TRIGGER"&&x.name=="guard");
            let parent_now=table_id(&mut conn,&original.database,"parent").unwrap();
            eprintln!("RV204 ok_after_kill={ok_after_kill} killed={killed:?} status={} original_unchanged={} error={} trigger_recovery_error={} missing_programs={} parent_id_before={old_parent} parent_id_now={parent_now} guard_present={guard_present} programs_before={} programs_after={}",
                result["status"],result["original_unchanged"],result["error"],result["trigger_recovery_error"],result["missing_programs"],before.len(),after.len());
            let inspected=inspect_backup(&original,&p,false,None).unwrap();
            eprintln!("RV204 ok_after_kill={ok_after_kill} inspect_backup verdict={}",inspected["verdict"]);
            assert!(killed.is_some());
            assert_eq!(parent_now,old_parent,"original table was never renamed");
            assert!(p.original_trigger_definitions.contains_key("guard"),"saved definition is available");
            assert_eq!(result["status"],"failed_original_unchanged","{result:#}");
            assert!(guard_present,"original trigger must be recovered using a fresh connection");
            assert_eq!(after,before,"recovery must preserve definitions and ACTION_ORDER, rather than name order");
            assert_eq!(inspected["verdict"],"not_promoted");
            cleanup(&mut conn,&p,&dir);
        }
    }
    #[test]
    fn safe_promote_lost_cleanup_connection_before_detach_reports_unchanged() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
        let before=capture_programs(&mut conn,&original.database).unwrap();
        let user=format!("tf_rv_{}",&original.database[16..35]);
        conn.query_drop(format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_rv_pw'")).unwrap();
        conn.query_drop(format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%' WITH GRANT OPTION")).unwrap();
        let (mut o,mut c)=(original.clone(),candidate.clone());
        for e in [&mut o,&mut c] { e.user=user.clone(); e.password="tf_rv_pw".into(); }
        let p=plan(&o,&c,"cleanup_conn_lost",&programs).unwrap();
        assert!(!moved_original_triggers(&p).is_empty());
        let lock_sql=format!("ALTER USER '{user}'@'%' ACCOUNT LOCK");
        // Fail before any trigger is detached, and make every later connect(original) fail.
        let result=promote_with_hook(&o,&c,&p,&p.digest,&dir,|phase,db| {
            if phase=="before_lock" { db.query_drop(&lock_sql).map_err(|e|e.to_string())?; Err("injected before lock".into()) } else { Ok(()) }
        }).unwrap();
        let after=capture_programs(&mut conn,&original.database).unwrap();
        eprintln!("RV204 status={} original_unchanged={} cleanup_errors={} error={}",result["status"],result["original_unchanged"],result.get("cleanup_errors").cloned().unwrap_or(Value::Null),result["error"]);
        eprintln!("RV204 triggers_unchanged={}",after==before);
        conn.query_drop(format!("DROP USER '{user}'@'%'")).unwrap();
        cleanup(&mut conn,&p,&dir);
        assert_eq!(after,before,"original programs must be untouched");
        assert_eq!(result["status"],"failed_original_unchanged","{result:#}");
        assert_eq!(result["original_unchanged"],true);
    }
    #[test]
    fn safe_promote_incomplete_recovery_lock_timeout_is_failed_no_change() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
        let p=plan(&original,&candidate,"rv_incomplete",&programs).unwrap();
        // Triggers attached, routines/events not yet replaced -> promoted_incomplete with original p/f/e.
        let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|at,_|if at=="triggers_attached" {Err("injected".into())} else {Ok(())}).unwrap();
        assert_eq!(result["status"],"promoted_incomplete","{result:#}");
        let displaced=format!("tf_backup_recovery_{}",p.nonce);
        let reviewed=rollback_plan(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced).unwrap();
        assert_eq!(reviewed["blockers"],json!([]),"{reviewed:#}");
        let snapshot=|conn:&mut Db| -> (Vec<NormalizedStoredProgram>, Vec<(String,Option<u64>)>) {
            let programs=capture_programs(conn,&original.database).unwrap();
            let ids=tables(conn,&original.database).unwrap().into_keys().map(|n|{let id=optional_table_id(conn,&original.database,&n).unwrap();(n,id)}).collect();
            (programs,ids)
        };
        let before=snapshot(&mut conn);
        // Session 2: an app transaction that has read the active table holds its metadata lock.
        let mut other=connect(&original).unwrap();
        other.query_drop("START TRANSACTION").unwrap();
        let _:Option<u64>=other.query_first(format!("SELECT COUNT(*) FROM {}",qualified(&original.database,"parent"))).unwrap();
        let recovery_dir=dir.join("recovery"); std::fs::create_dir(&recovery_dir).unwrap();
        let recovered=rollback_apply(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced,&recovery_dir).unwrap();
        other.query_drop("ROLLBACK").unwrap(); drop(other);
        let after=snapshot(&mut conn);
        let displaced_left=database_exists(&mut conn,&displaced).unwrap();
        let again=rollback_plan(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced).unwrap();
        let missing=missing_programs(&mut conn,&original.database,&final_programs(&p)).unwrap();
        eprintln!("RV status={} error={} trigger_recovery_error={} programs_unchanged={} ids_unchanged={} displaced_left={displaced_left} missing_vs_final={missing:?} retry_blockers={}",
            recovered["status"],recovered["error"],recovered["trigger_recovery_error"],after.0==before.0,after.1==before.1,again["blockers"]);
        let _=conn.query_drop(format!("DROP DATABASE IF EXISTS {}",q(&displaced)));
        cleanup(&mut conn,&p,&dir);
        assert!(recovered["error"].as_str().unwrap_or("").contains("lock"),"{recovered:#}");
        assert_eq!(after,before,"the failed recovery changed nothing");
        assert_eq!(recovered["status"],"failed_no_change","{recovered:#}");
    }
    #[test]
    fn safe_promote_triggers_missing_cleans_preparatory_objects() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        for recovery_fails in [false, true] {
            let Some((original,candidate,mut conn,dir))=fixture() else { return };
            for (db, note) in [(&original.database,"old"),(&candidate.database,"dump")] {
                conn.query_drop(format!("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note='{note}'",qualified(db,"guard"),qualified(db,"parent"))).unwrap();
            }
            let programs=capture_programs(&mut conn,&candidate.database).unwrap();
            for p in &programs { conn.query_drop(format!("DROP {} {}",p.kind,qualified(&candidate.database,&p.name))).unwrap(); }
            let p=plan(&original,&candidate,"rv11",&programs).unwrap();
            assert!(!p.hidden_views.is_empty(), "fixture must prepare hidden views");
            let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|phase,_| {
                if phase=="before_rename" || (recovery_fails && phase=="before_original_trigger_recovery") { Err("injected".into()) } else { Ok(()) }
            }).unwrap();
            let status=result["status"].as_str().unwrap().to_string();
            let present=tables(&mut conn,&p.original_database).unwrap();
            let hidden_left=p.hidden_views.values().filter(|n|present.contains_key(*n)).cloned().collect::<Vec<_>>();
            let clone_left=database_exists(&mut conn,&p.clone_database).unwrap();
            let backup_left=database_exists(&mut conn,&p.backup_database).unwrap();
            let tables_unchanged=p.original_tables.iter().all(|(n,t)|table_id(&mut conn,&p.original_database,n).unwrap()==t.table_id);
            eprintln!("RV11 recovery_fails={recovery_fails} status={status} tables_unchanged={tables_unchanged} hidden_left={hidden_left:?} clone_left={clone_left} backup_left={backup_left} cleanup_errors={}",result["cleanup_errors"]);
            assert!(tables_unchanged);
            if !recovery_fails {
                assert_eq!(status,"failed_original_unchanged","{result:#}");
                assert!(hidden_left.is_empty() && !clone_left && !backup_left);
                cleanup(&mut conn,&p,&dir);
                continue;
            }
            assert_eq!(status,"failed_original_triggers_missing","{result:#}");
            // Session-default replay keeps the verdict stuck (context mismatch); replay with the saved context as recovery does.
            conn.query_drop(format!("USE {}",q(&p.original_database))).unwrap();
            conn.query_drop(result["plan"]["original_trigger_definitions"]["guard"].as_str().unwrap()).unwrap();
            eprintln!("RV11 verdict_after_plain_replay={}",inspect_backup(&original,&p,false,None).unwrap()["verdict"]);
            drop_triggers(&mut conn,&p.original_database,&moved_original_triggers(&p)).unwrap();
            attach_triggers(&mut conn,&p,true,&mut Vec::new()).unwrap();
            let verdict=inspect_backup(&original,&p,false,None).unwrap()["verdict"].clone();
            eprintln!("RV11 verdict_after_context_replay={verdict}");
            // Attempt journal as safe_promotion::finish_attempt leaves it.
            let input=dir.join("input");
            let attempt=input.join(".tunnelforge_promotion_plan_rv11").join("attempt");
            std::fs::create_dir_all(&attempt).unwrap();
            let stored=json!({"restore_id":"rv11","restore_digest":"x","original_target":crate::safe_promotion::public_target(&original),
                "candidate_target":crate::safe_promotion::public_target(&candidate),
                "engine_plan":{"engine":"mysql","plan":serde_json::to_value(&p).unwrap()},"attempt_nonce":p.nonce});
            write_dump_import_report(&attempt,&json!({"status":status,"original_unchanged":false,"plan_digest":"x","stored_plan":stored,"result":result})).unwrap();
            let call=|payload:Value| { let mut out=Value::Null; crate::safe_backups::handle(&Request{command:"restore.backups".into(),request_id:None,payload},|v|out=v); out };
            let body=|action:&str,target:&str| json!({"action":action,"target":target,"restore_id":"rv11","input_dirs":[input.to_string_lossy()],"connection":serde_json::to_value(&original).unwrap()});
            let mut allowed=Vec::new();
            for target in ["clone","backup","view_aliases","candidate"] {
                let r=call(body("cleanup_plan",target));
                eprintln!("RV11 cleanup_plan {target}: event={} can_cleanup={} blockers={} message={}",r["event"],r["can_cleanup"],r["blockers"],r["message"]);
                allowed.push(r["can_cleanup"]==true);
            }
            let rb=call(body("rollback_plan",""));
            eprintln!("RV11 rollback_plan: event={} can_rollback={} blockers={} message={}",rb["event"],rb["can_rollback"],rb["blockers"],rb["message"]);
            let rc=call(body("reconcile",""));
            eprintln!("RV11 reconcile: conclusion={} cleanup_allowed={}",rc["conclusion"],rc["cleanup_allowed"]);
            assert_eq!(verdict,"not_promoted");
            assert!(hidden_left.is_empty() && !clone_left && !backup_left, "preparatory objects must be cleaned");
            // The synthetic journal has no verified import report; that independent guard still applies.
            let candidate_cleanup=call(body("cleanup_plan","candidate"));
            assert!(!candidate_cleanup["blockers"].to_string().contains("only a verified, unpromoted candidate"), "{candidate_cleanup:#}");
            cleanup(&mut conn,&p,&dir);
        }
    }
    #[test]
    fn safe_promote_foreign_definer_original_trigger_without_set_user_id() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some(host) = std::env::var("TF_PROMOTE_MYSQL_HOST").ok() else { return };
        let admin_ep = Endpoint { engine: "mysql".into(), host, port: 3306, user: "root".into(), password: "tf_local_test".into(),
            database: "tf_test".into(), schema: None, tls: Default::default() };
        let mut admin = connect(&admin_ep).unwrap();
        let (log_bin, trust): (i64, i64) = admin.query_first("SELECT @@GLOBAL.log_bin, @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap();
        struct Restore { ep: Endpoint, trust: i64, users: Vec<String> }
        impl Drop for Restore { fn drop(&mut self) { if let Ok(mut db) = connect(&self.ep) {
            let _ = db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}", self.trust));
            for u in &self.users { let _ = db.query_drop(format!("DROP USER IF EXISTS {}@'%'", q(u))); }
        } } }
        let nonce = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let (app, op) = (format!("tf_fd_app_{nonce}"), format!("tf_fd_op_{nonce}"));
        let _restore = Restore { ep: admin_ep.clone(), trust, users: vec![app.clone(), op.clone()] };
        // Isolate 1227 from the 1419 binlog rule.
        admin.query_drop("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
        admin.query_drop(format!("CREATE USER {}@'%' IDENTIFIED BY 'tf_local_app' ACCOUNT LOCK", q(&app))).unwrap();
        admin.query_drop(format!("CREATE USER {}@'%' IDENTIFIED BY 'tf_local_op'", q(&op))).unwrap();
        admin.query_drop(format!("GRANT SELECT, SHOW VIEW, PROCESS, CREATE ON *.* TO {}@'%'", q(&op))).unwrap();
        admin.query_drop(format!("GRANT ALL PRIVILEGES ON `tf\\_%`.* TO {}@'%'", q(&op))).unwrap();
        let grants: Vec<String> = admin.query(format!("SHOW GRANTS FOR {}@'%'", q(&op))).unwrap();
        let mut report = Vec::new();
        for case in ["control", "rollback", "before_rename", "closure"] {
            let (original, candidate, mut root, dir) = fixture().unwrap();
            let mut o = original.clone(); o.user = op.clone(); o.password = "tf_local_op".into();
            let mut c = candidate.clone(); c.user = op.clone(); c.password = "tf_local_op".into();
            let mut planned: Option<PromotionPlan> = None;
            let outcome = (|| -> Result<Value, String> {
                // Guided recovery refuses promotions with views; drop them as program_fixture does.
                for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
                    root.query_drop(format!("DROP VIEW {}", qualified(db, view))).map_err(|e| e.to_string())?;
                }
                let table = if case == "closure" { "child" } else { "parent" };
                let body = format!("TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET @tf_fd=1", qualified(&original.database, "fd_guard"), qualified(&original.database, table));
                if case == "control" { connect(&o)?.query_drop(format!("CREATE {body}")).map_err(|e| e.to_string())?; }
                else { root.query_drop(format!("CREATE DEFINER={}@'%' {body}", q(&app))).map_err(|e| e.to_string())?; }
                let p = plan(&o, &c, "foreign_definer", &[])?;
                planned = Some(p.clone());
                let saved = p.original_trigger_definitions.get("fd_guard").cloned();
                let result = if case == "before_rename" {
                    promote_with_hook(&o, &c, &p, &p.digest, &dir, |at, _| if at == "before_rename" { Err("injected".into()) } else { Ok(()) })?
                } else { promote(&o, &c, &p, &p.digest, &dir)? };
                let displaced = format!("tf_backup_recovery_{}", p.nonce);
                let (mut blockers, mut recovered) = (Value::Null, Value::Null);
                if matches!(case, "control" | "rollback") {
                    let recovery_dir = dir.join("recovery"); std::fs::create_dir(&recovery_dir).map_err(|e| e.to_string())?;
                    blockers = rollback_plan(&o, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced)?["blockers"].clone();
                    recovered = rollback_apply(&o, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &recovery_dir)
                        .unwrap_or_else(|e| json!({"call_error": e}));
                }
                let present: Option<u64> = root.exec_first("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? AND TRIGGER_NAME='fd_guard'", (&original.database,)).map_err(|e| e.to_string())?;
                Ok(json!({"case": case, "saved_definition": saved, "promote_status": result["status"], "promote_error": result.get("error"),
                    "trigger_recovery_error": result.get("trigger_recovery_error"), "missing_programs": result.get("missing_programs"),
                    "rollback_blockers": blockers, "rollback_status": recovered.get("status"), "rollback_error": recovered.get("error"),
                    "rollback_call_error": recovered.get("call_error"), "fd_guard_present": present}))
            })();
            if let Some(p) = &planned {
                let _ = root.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&format!("tf_backup_recovery_{}", p.nonce))));
                cleanup(&mut root, p, &dir);
            } else {
                for db in [&original.database, &candidate.database] { let _ = root.query_drop(format!("DROP DATABASE IF EXISTS {}", q(db))); }
                let _ = std::fs::remove_dir_all(&dir);
            }
            report.push(outcome.unwrap_or_else(|e| json!({"case": case, "test_error": e})));
        }
        eprintln!("FD_REPORT log_bin={log_bin} trust_before={trust} grants={grants:?}\n{}", serde_json::to_string_pretty(&report).unwrap());
        let r = |case: &str| report.iter().find(|v| v["case"] == case).unwrap().clone();
        assert_eq!(r("control")["rollback_status"], "rolled_back");
        assert_eq!(r("control")["fd_guard_present"], 1);
        let rb = r("rollback");
        assert!(rb["saved_definition"].as_str().unwrap().contains(&format!("DEFINER=`{app}`")));
        assert_eq!(rb["promote_status"], "promoted");
        assert_eq!(rb["rollback_blockers"], json!([]));
        assert_eq!(rb["rollback_status"], "rolled_back");
        assert!(rb["rollback_error"].is_null());
        assert_eq!(rb["fd_guard_present"], 1);
        assert_eq!(r("before_rename")["promote_status"], "failed_original_unchanged");
        assert!(r("before_rename")["trigger_recovery_error"].is_null());
        assert_eq!(r("before_rename")["fd_guard_present"], 1);
        assert_eq!(r("closure")["promote_status"], "promoted");
        assert_eq!(r("closure")["fd_guard_present"], 1);
    }
    #[test]
    fn safe_promote_rollback_records_changed_definer_of_routine_and_event() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
        let pid=std::process::id();
        let (owner,importer)=(format!("rv9_owner_{pid}"),format!("rv9_imp_{pid}"));
        struct Users { endpoint:Endpoint, names:Vec<String> }
        impl Drop for Users { fn drop(&mut self) { if let Ok(mut db)=connect(&self.endpoint) { for n in &self.names { let _=db.query_drop(format!("DROP USER IF EXISTS '{n}'@'%'")); } } } }
        let admin=Endpoint{database:"tf_test".into(),..original.clone()};
        let _users=Users{endpoint:admin.clone(),names:vec![owner.clone(),importer.clone()]};
        // A non-SUPER account can create triggers/functions with binary logging only under this setting.
        struct Trust { endpoint:Endpoint, previous:i64 }
        impl Drop for Trust { fn drop(&mut self) { let _=connect(&self.endpoint).and_then(|mut db|db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}",self.previous)).map_err(|e|e.to_string())); } }
        let _trust=Trust{endpoint:admin.clone(),previous:conn.query_first("SELECT @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap()};
        conn.query_drop("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
        for sql in [
            format!("CREATE USER '{owner}'@'%' IDENTIFIED BY 'tf_local_test'"),
            format!("CREATE USER '{importer}'@'%' IDENTIFIED BY 'tf_local_test'"),
            // Global metadata visibility the guarded path requires; no SUPER/SET_USER_ID/SET_ANY_DEFINER.
            format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{importer}'@'%'"),
            format!("GRANT ALL ON `tf\\_%`.* TO '{importer}'@'%'"),
            // Replaced objects: trigger/function owned by the importer itself (root-owned ones need
            // SYSTEM_USER to replace), procedure/event owned by a foreign non-SYSTEM_USER account.
            format!("DROP TRIGGER {}",qualified(&original.database,"guard")),
            format!("CREATE DEFINER='{importer}'@'%' TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note='old'",qualified(&original.database,"guard"),qualified(&original.database,"parent")),
            format!("DROP FUNCTION {}",qualified(&original.database,"f")),
            format!("CREATE DEFINER='{importer}'@'%' FUNCTION {}() RETURNS INT DETERMINISTIC RETURN 1",qualified(&original.database,"f")),
            format!("DROP EVENT {}",qualified(&original.database,"e")),
            format!("CREATE DEFINER='{owner}'@'%' EVENT {} ON SCHEDULE AT '2099-01-01' ON COMPLETION PRESERVE DISABLE DO SET @tf_note='old'",qualified(&original.database,"e")),
            format!("DROP PROCEDURE {}",qualified(&original.database,"p")),
            format!("CREATE DEFINER='{owner}'@'%' PROCEDURE {}() SQL SECURITY DEFINER SELECT 'old'",qualified(&original.database,"p")),
        ] { conn.query_drop(sql).unwrap(); }
        let account=Endpoint{user:importer.clone(),password:"tf_local_test".into(),..original.clone()};
        let account_candidate=Endpoint{user:importer.clone(),password:"tf_local_test".into(),..candidate.clone()};
        let p=plan(&account,&account_candidate,"definer",&programs).unwrap();
        let result=promote(&account,&account_candidate,&p,&p.digest,&dir).unwrap();
        let recovery_dir=dir.join("recovery"); std::fs::create_dir(&recovery_dir).unwrap();
        let displaced=format!("tf_backup_recovery_{}",p.nonce);
        let recovered=rollback_apply(&account,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced,&recovery_dir);
        let ev:Option<String>=conn.exec_first("SELECT DEFINER FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? AND EVENT_NAME='e'",(&original.database,)).unwrap();
        let rt:Option<(String,String)>=conn.exec_first("SELECT DEFINER,SECURITY_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=? AND ROUTINE_NAME='p'",(&original.database,)).unwrap();
        let journals:String=std::fs::read_dir(&recovery_dir).unwrap().filter_map(|e|std::fs::read_to_string(e.ok()?.path()).ok()).collect();
        eprintln!("RV9 promote status={} program_warnings={}",result["status"],result["program_warnings"]);
        eprintln!("RV9 recovered={recovered:#?}");
        eprintln!("RV9 event definer after recovery={ev:?}; routine (definer,security)={rt:?}");
        eprintln!("RV9 journal mentions program_restore_definer_changed: {}; previous_definer_preserved: {}",journals.contains("program_restore_definer_changed"),journals.contains("previous_definer_preserved"));
        let _=conn.query_drop(format!("DROP DATABASE IF EXISTS {}",q(&displaced)));
        cleanup(&mut conn,&p,&dir);
        assert_eq!(result["status"],"promoted","{result:#}");
        let recovered=recovered.unwrap();
        assert_eq!(recovered["status"],"rolled_back","{recovered:#}");
        assert_eq!(ev,Some(format!("{importer}@%")),"event definer was not changed by recovery");
        assert_eq!(rt,Some((format!("{importer}@%"),"DEFINER".into())),"routine definer was not changed by recovery");
        // The defect: the change is recorded neither in the result nor in the rollback journal.
        assert!(recovered.to_string().contains("program_restore_definer_changed"),"rollback result has no definer-change warning: {recovered:#}");
        assert!(journals.contains("program_restore_definer_changed"),"rollback journal has no definer-change warning");
    }
    #[test]
    fn safe_promote_foreign_definer_original_trigger_survives_recovery() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let mut outcomes = Vec::new();
        for phase in ["before_rename", "rollback_apply"] {
            let Some((original, candidate, mut conn, dir)) = fixture() else { return };
            // Isolate the DEFINER rule from the 1419 binlog rule (RDS-style servers run with this on).
            let trust: String = conn.query_first("SELECT CAST(@@GLOBAL.log_bin_trust_function_creators AS CHAR)").unwrap().unwrap();
            conn.query_drop("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
            for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
                conn.query_drop(format!("DROP VIEW {}", qualified(db, view))).unwrap();
            }
            let n = &original.database[original.database.len() - 10..original.database.len() - 2];
            let (legacy, app) = (format!("tf_lg_{n}"), format!("tf_app_{n}"));
            conn.query_drop(format!("CREATE USER '{legacy}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
            conn.query_drop(format!("GRANT SELECT, UPDATE, TRIGGER ON {}.* TO '{legacy}'@'%'", q(&original.database))).unwrap();
            conn.query_drop(format!("CREATE DEFINER='{legacy}'@'%' TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note=CONCAT(NEW.note,'!')",
                qualified(&original.database, "legacy_bu"), qualified(&original.database, "parent"))).unwrap();
            conn.query_drop(format!("CREATE USER '{app}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
            conn.query_drop(format!("GRANT ALL PRIVILEGES ON *.* TO '{app}'@'%'")).unwrap();
            let mut revoked = Vec::new();
            for privilege in ["SUPER", "SET_USER_ID", "SET_ANY_DEFINER", "ALLOW_NONEXISTENT_DEFINER", "SYSTEM_USER"] {
                if conn.query_drop(format!("REVOKE {privilege} ON *.* FROM '{app}'@'%'")).is_ok() { revoked.push(privilege); }
            }
            let as_app = |e: &Endpoint| Endpoint { user: app.clone(), ..e.clone() };
            let (o, c) = (as_app(&original), as_app(&candidate));
            let p = plan(&o, &c, "foreign_definer", &[]).unwrap();
            let saved = p.original_trigger_definitions.get("legacy_bu").cloned().unwrap_or_default();
            let mut displaced = None;
            let result = if phase == "before_rename" {
                promote_with_hook(&o, &c, &p, &p.digest, &dir, |at, _| if at == "before_rename" { Err("injected".into()) } else { Ok(()) }).unwrap()
            } else {
                let promoted = promote(&o, &c, &p, &p.digest, &dir).unwrap();
                assert_eq!(promoted["status"], "promoted", "{promoted:#}");
                let recovery_dir = dir.join("recovery");
                std::fs::create_dir(&recovery_dir).unwrap();
                let name = format!("tf_backup_recovery_{}", p.nonce);
                let reviewed = rollback_plan(&o, &p, promoted.get("backup_fingerprint"), promoted.get("new_table_ids"), &name).unwrap();
                eprintln!("REVIEW rollback_plan blockers={}", reviewed["blockers"]);
                displaced = Some(name.clone());
                rollback_apply(&o, &p, promoted.get("backup_fingerprint"), promoted.get("new_table_ids"), &name, &recovery_dir).unwrap()
            };
            let live: Option<String> = conn.exec_first("SELECT DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? AND TRIGGER_NAME='legacy_bu'", (&original.database,)).unwrap();
            eprintln!("REVIEW {phase}: revoked={revoked:?} saved={saved:?}\n  status={} error={} trigger_recovery_error={}\n  legacy_bu live definer={live:?}",
                result["status"], result["error"], result["trigger_recovery_error"]);
            outcomes.push((phase, result["status"].as_str().unwrap_or_default().to_string(), live));
            if let Some(name) = displaced { conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&name))).unwrap(); }
            conn.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={trust}")).unwrap();
            for user in [&legacy, &app] { conn.query_drop(format!("DROP USER '{user}'@'%'")).unwrap(); }
            cleanup(&mut conn, &p, &dir);
        }
        for (phase, status, live) in outcomes {
            assert!(live.is_some(), "{phase}: original trigger legacy_bu lost, status {status}");
            assert!(matches!(status.as_str(), "failed_original_unchanged" | "rolled_back"), "{phase}: {status}");
        }
    }
#[test]
fn safe_promote_completed_lifecycle_ignores_unrelated_program_changes() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let verdict=|o:&Endpoint,p:&PromotionPlan,r:&Value| { let v=inspect_backup_recorded(o,p,false,r.get("backup_fingerprint"),true).unwrap(); eprintln!("RV12 {}",v["missing_programs"]); v["verdict"].as_str().unwrap().to_string() };
    // A: plan with programs; unrelated additions / DISABLE after a clean promoted.
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
    let p=plan(&original,&candidate,"rv12a",&programs).unwrap();
    let r=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
    assert_eq!(r["status"],"promoted","{r:#}");
    let mut seen=vec![("baseline",verdict(&original,&p,&r))];
    conn.query_drop(format!("CREATE PROCEDURE {}() SELECT 1",qualified(&original.database,"extra_p"))).unwrap();
    seen.push(("new_procedure",verdict(&original,&p,&r)));
    conn.query_drop(format!("DROP PROCEDURE {}",qualified(&original.database,"extra_p"))).unwrap();
    conn.query_drop(format!("CREATE TRIGGER {} AFTER INSERT ON {} FOR EACH ROW SET @x=1",qualified(&original.database,"extra_guard"),qualified(&original.database,"unrelated"))).unwrap();
    seen.push(("new_trigger_unrelated_table",verdict(&original,&p,&r)));
    conn.query_drop(format!("DROP TRIGGER {}",qualified(&original.database,"extra_guard"))).unwrap();
    seen.push(("restored",verdict(&original,&p,&r)));
    conn.query_drop(format!("ALTER EVENT {} DISABLE",qualified(&original.database,"new_e"))).unwrap();
    seen.push(("alter_event_disable",verdict(&original,&p,&r)));
    cleanup(&mut conn,&p,&dir);
    // B: dump one-time NOT PRESERVE event fires and the server drops it.
    let Some((original,candidate,mut conn,dir,mut programs))=program_fixture() else { return };
    let scheduler:String=conn.query_first("SELECT @@event_scheduler").unwrap().unwrap();
    struct Scheduler { endpoint:Endpoint, previous:String }
    impl Drop for Scheduler { fn drop(&mut self) { let _=connect(&self.endpoint).and_then(|mut db|db.query_drop(format!("SET GLOBAL event_scheduler={}",self.previous)).map_err(|e|e.to_string())); } }
    let _scheduler=Scheduler{endpoint:original.clone(),previous:scheduler};
    conn.query_drop("SET GLOBAL event_scheduler=ON").unwrap();
    let event=programs.iter_mut().find(|p|p.name=="new_e").unwrap();
    conn.exec_drop("SET SESSION time_zone=?",(&event.time_zone,)).unwrap();
    let at:String=conn.query_first("SELECT DATE_FORMAT(NOW()+INTERVAL 8 SECOND,'%Y-%m-%d %H:%i:%s')").unwrap().unwrap();
    reset_session(&mut conn).unwrap();
    event.definition=format!("CREATE EVENT `new_e` ON SCHEDULE AT '{at}' ON COMPLETION NOT PRESERVE ENABLE DO SET @tf_finished=1");
    let p=plan(&original,&candidate,"rv12b",&programs).unwrap();
    let r=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
    assert_eq!(r["status"],"promoted","{r:#}");
    seen.push(("not_preserve_before_fire",verdict(&original,&p,&r)));
    let mut gone=false;
    for _ in 0..60 {
        let n:Option<u64>=conn.exec_first("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? AND EVENT_NAME='new_e'",(&original.database,)).unwrap();
        if n==Some(0) { gone=true; break }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    assert!(gone,"event did not fire");
    seen.push(("not_preserve_after_fire",verdict(&original,&p,&r)));
    cleanup(&mut conn,&p,&dir);
    // C: plan without programs (early return) with the same unrelated addition.
    let Some((original,candidate,mut conn,dir))=fixture() else { return };
    let p=plan(&original,&candidate,"rv12c",&[]).unwrap();
    let r=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
    assert_eq!(r["status"],"promoted","{r:#}");
    conn.query_drop(format!("CREATE PROCEDURE {}() SELECT 1",qualified(&original.database,"extra_p"))).unwrap();
    seen.push(("no_program_plan_new_procedure",verdict(&original,&p,&r)));
    cleanup(&mut conn,&p,&dir);
    eprintln!("RV12 RESULT {seen:?}");
    let get=|k:&str| seen.iter().find(|(n,_)|*n==k).unwrap().1.clone();
    assert_eq!(get("baseline"),"promoted");
    assert_eq!(get("restored"),"promoted");
    assert_eq!(get("not_preserve_before_fire"),"promoted");
    assert_eq!(get("no_program_plan_new_procedure"),"promoted");
    // Finding: these flip to promoted_incomplete.
    assert_eq!(get("alter_event_disable"),"promoted");
    for k in ["new_procedure","new_trigger_unrelated_table","not_preserve_after_fire"] {
        assert_eq!(get(k),"promoted","{k}: {seen:?}");
    }
}

    #[test]
    fn safe_promote_checks_created_trigger_order_before_unlocking() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e|e.into_inner());
        let Some((original,candidate,mut conn,dir,mut programs))=program_fixture() else {return};
        let mut later = programs.iter().find(|p|p.kind=="TRIGGER" && p.name=="guard").unwrap().clone();
        later.name="guard_later".into(); later.action_order=Some(2); programs.push(later);
        let p=plan(&original,&candidate,"order_check",&programs).unwrap();
        let mut verified=false;
        let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|phase,db| {
            if phase=="before_trigger_verification" {
                let trigger=programs.iter().find(|p|p.kind=="TRIGGER" && p.name=="guard").unwrap();
                db.query_drop(format!("DROP TRIGGER {}",qualified(&original.database,"guard"))).map_err(|e|e.to_string())?;
                crate::mysql_programs::create_trigger(db,trigger,"guard","parent")?;
            }
            if phase=="triggers_attached" {verified=true;}
            Ok(())
        }).unwrap();
        assert!(!verified,"mismatching ACTION_ORDER reached triggers_attached");
        assert_eq!(result["status"],"promoted_incomplete","{result:#}");
        assert_eq!(result["phase"],"promoted_locked","{result:#}");
        assert!(result["error"].as_str().unwrap().contains("verified after attachment"));
        cleanup(&mut conn,&p,&dir);
    }

    fn program_fixture() -> Option<(Endpoint, Endpoint, Db, std::path::PathBuf, Vec<NormalizedStoredProgram>)> {
        let (original, candidate, mut conn, dir) = fixture()?;
        for (db, view) in [(&original.database,"child_view"),(&original.database,"v"),(&candidate.database,"v")] {
            conn.query_drop(format!("DROP VIEW {}",qualified(db,view))).unwrap();
        }
        for (db, note) in [(&original.database,"old"),(&candidate.database,"dump")] {
            conn.query_drop(format!("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note='{note}'",qualified(db,"guard"),qualified(db,"parent"))).unwrap();
            conn.query_drop(format!("CREATE PROCEDURE {}() SELECT '{note}'",qualified(db,"p"))).unwrap();
            conn.query_drop(format!("CREATE FUNCTION {}() RETURNS INT DETERMINISTIC RETURN {}",qualified(db,"f"),if note=="old" {1} else {2})).unwrap();
            conn.query_drop(format!("CREATE EVENT {} ON SCHEDULE AT '2099-01-01' ON COMPLETION PRESERVE DISABLE DO SET @tf_note='{note}'",qualified(db,"e"))).unwrap();
        }
        conn.query_drop(format!("CREATE PROCEDURE {}() SELECT 'keep'",qualified(&original.database,"kept"))).unwrap();
        conn.query_drop(format!("CREATE EVENT {} ON SCHEDULE AT '2099-01-01' DISABLE DO SET @tf_note='keep'",qualified(&original.database,"kept_e"))).unwrap();
        conn.query_drop(format!("CREATE TRIGGER {} BEFORE INSERT ON {} FOR EACH ROW SET NEW.id=NEW.id",qualified(&original.database,"kept_guard"),qualified(&original.database,"unrelated"))).unwrap();
        conn.query_drop(format!("CREATE PROCEDURE {}() SELECT 'new'",qualified(&candidate.database,"new_p"))).unwrap();
        conn.query_drop(format!("CREATE EVENT {} ON SCHEDULE AT '2099-01-01' ENABLE DO SET @tf_note='new'",qualified(&candidate.database,"new_e"))).unwrap();
        let programs = capture_programs(&mut conn,&candidate.database).unwrap();
        for p in &programs { conn.query_drop(format!("DROP {} {}",p.kind,qualified(&candidate.database,&p.name))).unwrap(); }
        Some((original,candidate,conn,dir,programs))
    }

    #[test]
    fn safe_promote_carries_original_and_dump_triggers_and_recovers_programs() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
        let before=capture_programs(&mut conn,&original.database).unwrap();
        let p=plan(&original,&candidate,"programs",&programs).unwrap();
        let result=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
        assert_eq!(result["status"],"promoted","{result:#}");
        assert!(missing_programs(&mut conn,&original.database,&final_programs(&p)).unwrap().is_empty());
        assert!(capture_programs(&mut conn,&candidate.database).unwrap().is_empty());
        let recovery_dir=dir.join("recovery"); std::fs::create_dir(&recovery_dir).unwrap();
        let displaced=format!("tf_backup_recovery_{}",p.nonce);
        let reviewed=rollback_plan(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced).unwrap();
        assert_eq!(reviewed["blockers"],json!([]),"{reviewed:#}");
        let recovered=rollback_apply(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced,&recovery_dir).unwrap();
        assert_eq!(recovered["status"],"rolled_back","{recovered:#}");
        assert_eq!(capture_programs(&mut conn,&original.database).unwrap(),before);
        conn.query_drop(format!("DROP DATABASE {}",q(&displaced))).unwrap();
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn safe_promote_trigger_failures_before_and_after_rename_are_classified() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        for phase in ["before_rename","after_rename"] {
            let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
            let before=capture_programs(&mut conn,&original.database).unwrap();
            let p=plan(&original,&candidate,"failure_programs",&programs).unwrap();
            let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|at,_|if at==phase {Err("injected".into())} else {Ok(())}).unwrap();
            assert_eq!(result["status"],if phase=="before_rename" {"failed_original_unchanged"} else {"promoted_incomplete"},"{result:#}");
            if phase=="before_rename" { assert_eq!(capture_programs(&mut conn,&original.database).unwrap(),before); }
            else {
                assert_eq!(result["success"],false);
                assert!(result["missing_programs"].as_array().unwrap().iter().any(|id|id=="trigger:parent:guard"));
                let backup=inspect_backup(&original,&p,false,result.get("backup_fingerprint")).unwrap();
                assert_eq!(backup["verdict"],"promoted_incomplete");
                let recovery_dir=dir.join("recovery"); std::fs::create_dir(&recovery_dir).unwrap();
                let displaced=format!("tf_backup_recovery_{}",p.nonce);
                let recovered=rollback_apply(&original,&p,result.get("backup_fingerprint"),result.get("new_table_ids"),&displaced,&recovery_dir).unwrap();
                assert_eq!(recovered["status"],"rolled_back","{recovered:#}");
                conn.query_drop(format!("DROP DATABASE {}",q(&displaced))).unwrap();
            }
            cleanup(&mut conn,&p,&dir);
        }
    }

    #[test]
    fn safe_promote_failed_original_trigger_recovery_keeps_saved_definitions() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
        let p=plan(&original,&candidate,"missing_original",&programs).unwrap();
        let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|phase,_| {
            if matches!(phase,"before_rename" | "before_original_trigger_recovery") { Err("injected".into()) } else { Ok(()) }
        }).unwrap();
        assert_eq!(result["status"],"failed_original_triggers_missing","{result:#}");
        assert_eq!(result["original_unchanged"],false);
        assert!(result["plan"]["original_trigger_definitions"]["guard"].as_str().unwrap().contains("SET NEW.note='old'"));
        let inspected=inspect_backup(&original,&p,false,None).unwrap();
        assert_eq!(inspected["verdict"],"failed_original_triggers_missing");
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn safe_promote_completed_preserved_event_is_still_a_complete_promotion() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,mut programs))=program_fixture() else { return };
        let scheduler:String=conn.query_first("SELECT @@event_scheduler").unwrap().unwrap();
        struct Scheduler { endpoint:Endpoint, previous:String }
        impl Drop for Scheduler { fn drop(&mut self) { let _=connect(&self.endpoint).and_then(|mut db|db.query_drop(format!("SET GLOBAL event_scheduler={}",self.previous)).map_err(|e|e.to_string())); } }
        let _scheduler=Scheduler{endpoint:original.clone(),previous:scheduler};
        conn.query_drop("SET GLOBAL event_scheduler=ON").unwrap();
        let event=programs.iter_mut().find(|p|p.name=="new_e").unwrap();
        event.definition="CREATE EVENT `new_e` ON SCHEDULE AT '2000-01-01 00:00:00' ON COMPLETION PRESERVE ENABLE DO SET @tf_finished=1".into();
        let p=plan(&original,&candidate,"completed_event",&programs).unwrap();
        let result=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
        assert_eq!(result["status"],"promoted","{result:#}");
        for _ in 0..100 {
            let (state,completed)=crate::mysql_schema_programs::event_state(&mut conn,&original.database,"new_e").unwrap();
            if state=="DISABLED" && completed { break }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let (state,completed)=crate::mysql_schema_programs::event_state(&mut conn,&original.database,"new_e").unwrap();
        assert_eq!((state.as_str(),completed),("DISABLED",true));
        let inspected=inspect_backup(&original,&p,false,result.get("backup_fingerprint")).unwrap();
        assert_eq!(inspected["verdict"],"promoted","{inspected:#}");
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn safe_promote_preserves_target_only_programs_without_replaying_their_bodies() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,programs))=program_fixture() else { return };
        conn.query_drop(format!("USE {}",q(&original.database))).unwrap();
        conn.query_drop("CREATE PROCEDURE opaque() SELECT /*!80000 1+ */ 2").unwrap();
        conn.query_drop("SET SESSION character_set_client=latin1, collation_connection=latin1_swedish_ci").unwrap();
        conn.query_drop("CREATE TRIGGER opaque_guard BEFORE INSERT ON unrelated FOR EACH ROW SET NEW.id=NEW.id+LENGTH('é')").unwrap();
        reset_session(&mut conn).unwrap();
        let before:Option<mysql::Row>=conn.query_first(format!("SHOW CREATE TRIGGER {}",qualified(&original.database,"opaque_guard"))).unwrap();
        let before:String=before.unwrap().get("SQL Original Statement").unwrap();
        assert!(capture_programs(&mut conn,&original.database).is_err());
        let p=plan(&original,&candidate,"opaque_kept",&programs).unwrap();
        assert!(!p.original_programs.iter().any(|p|matches!(p.name.as_str(),"opaque" | "opaque_guard")));
        let result=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
        assert_eq!(result["status"],"promoted","{result:#}");
        let after:Option<mysql::Row>=conn.query_first(format!("SHOW CREATE TRIGGER {}",qualified(&original.database,"opaque_guard"))).unwrap();
        assert_eq!(after.unwrap().get::<String,_>("SQL Original Statement").unwrap(),before);
        assert!(result["programs_kept"].as_array().unwrap().contains(&json!("routine:opaque")));
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn safe_promote_trigger_name_collision_with_target_only_table_refused() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir,mut programs))=program_fixture() else { return };
        let p=plan(&original,&candidate,"collision_cleanup",&[]).unwrap();
        programs.iter_mut().find(|p|p.kind=="TRIGGER").unwrap().name="kept_guard".into();
        assert!(plan(&original,&candidate,"collision",&programs).unwrap_err().starts_with("trigger_name_conflict:"));
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn safe_promote_old_journal_roundtrip_preserves_digest() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir))=fixture() else { return };
        let p=plan(&original,&candidate,"old_journal",&[]).unwrap();
        let value=serde_json::to_value(&p).unwrap();
        for field in ["original_programs","original_program_inventory","promoted_programs","original_trigger_definitions","import_definer"] { assert!(value.get(field).is_none()); }
        let loaded:PromotionPlan=serde_json::from_value(value.clone()).unwrap();
        assert_eq!(plan_digest(&loaded).unwrap(),p.digest);
        assert_eq!(serde_json::to_value(loaded).unwrap(),value);
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn safe_promote_lock_tables_allows_trigger_ddl_after_rename() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original,candidate,mut conn,dir))=fixture() else { return };
        let p=plan(&original,&candidate,"trigger_spike",&[]).unwrap();
        create_owned_database(&mut conn,&p.backup_database,&original.database).unwrap();
        conn.query_drop(format!("CREATE TRIGGER {} BEFORE INSERT ON {} FOR EACH ROW SET NEW.note='old'",qualified(&original.database,"spike"),qualified(&original.database,"parent"))).unwrap();
        conn.query_drop(format!("LOCK TABLES {} WRITE, {} WRITE",qualified(&original.database,"parent"),qualified(&candidate.database,"parent"))).unwrap();
        conn.query_drop(format!("DROP TRIGGER {}",qualified(&original.database,"spike"))).unwrap();
        conn.query_drop(format!("RENAME TABLE {} TO {}, {} TO {}",qualified(&original.database,"parent"),qualified(&p.backup_database,"parent"),qualified(&candidate.database,"parent"),qualified(&original.database,"parent"))).unwrap();
        conn.query_drop(format!("CREATE TRIGGER {} BEFORE INSERT ON {} FOR EACH ROW SET NEW.note='new'",qualified(&original.database,"spike"),qualified(&original.database,"parent"))).unwrap();
        conn.query_drop(format!("INSERT INTO {}(id,code) VALUES(202,'b')",qualified(&original.database,"parent"))).unwrap();
        let note:Option<String>=conn.query_first(format!("SELECT note FROM {} WHERE id=202",qualified(&original.database,"parent"))).unwrap(); assert_eq!(note.as_deref(),Some("new"));
        let mut other=connect(&original).unwrap(); other.query_drop("SET SESSION lock_wait_timeout=1").unwrap();
        for db in [&original.database,&p.backup_database] {
            assert!(matches!(other.query_drop(format!("UPDATE {} SET note='blocked'",qualified(db,"parent"))),Err(mysql::Error::MySqlError(e)) if e.code==1205));
        }
        conn.query_drop(format!("DROP TRIGGER {}",qualified(&original.database,"spike"))).unwrap();
        conn.query_drop(format!("CREATE TRIGGER {} AFTER INSERT ON {} FOR EACH ROW INSERT INTO {} VALUES(NEW.id)",qualified(&original.database,"spike"),qualified(&original.database,"parent"),qualified(&original.database,"unrelated"))).unwrap();
        assert!(matches!(conn.query_drop(format!("INSERT INTO {}(id,code) VALUES(203,'c')",qualified(&original.database,"parent"))),Err(mysql::Error::MySqlError(e)) if e.code==1100));
        conn.query_drop("UNLOCK TABLES").unwrap();
        cleanup(&mut conn,&p,&dir);
    }

    #[test]
    fn promotion_digest_reads_geometry_values() {
        let Some(host) = std::env::var("TF_PROMOTE_MYSQL_HOST").ok() else { return };
        let endpoint = Endpoint { engine: "mysql".into(), host, port: 3306, user: "root".into(), password: "tf_local_test".into(),
            database: "tf_test".into(), schema: None, tls: Default::default() };
        let mut conn = connect(&endpoint).unwrap();
        let name = format!("tf_promote_geo_{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        conn.query_drop(format!("CREATE TABLE {} (id INT PRIMARY KEY, g GEOMETRY)", qualified("tf_test", &name))).unwrap();
        // 1.0 encodes as ...F03F, which is not UTF-8 when read raw.
        conn.query_drop(format!("INSERT INTO {} VALUES (1, ST_GeomFromText('POINT(1 1.5)'))", qualified("tf_test", &name))).unwrap();
        let table = NormalizedTable {
            name: name.clone(),
            columns: vec![
                NormalizedColumn { name: "id".into(), type_name: "int".into(), default_value: None, nullable: false, primary_key: true, unique: false, comment: None, default_is_expression: false, on_update: None, generated: None, invisible: false },
                NormalizedColumn { name: "g".into(), type_name: "geometry".into(), default_value: None, nullable: true, primary_key: false, unique: false, comment: None, default_is_expression: false, on_update: None, generated: None, invisible: false },
            ],
            indexes: Vec::new(), foreign_keys: Vec::new(), table_collation: None, auto_increment: None, comment: None, checks: Vec::new(),
        };
        let digest = content_digest(&mut conn, "tf_test", &table);
        conn.query_drop(format!("DROP TABLE {}", qualified("tf_test", &name))).unwrap();
        assert!(digest.is_ok(), "{digest:?}");
    }
    fn fixture() -> Option<(Endpoint, Endpoint, Db, std::path::PathBuf)> {
        let host = std::env::var("TF_PROMOTE_MYSQL_HOST").ok()?;
        let mut original = Endpoint {
            engine: "mysql".into(),
            host,
            port: 3306,
            user: "root".into(),
            password: "tf_local_test".into(),
            database: "tf_test".into(),
            schema: None,
            tls: Default::default(),
        };
        let mut conn = connect(&original).unwrap();
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        original.database = format!("tf_promote_test_{nonce}_o");
        let mut candidate = original.clone();
        candidate.database = format!("tf_promote_test_{nonce}_c");
        for (db, note, id) in [
            (&original.database, "old", 1),
            (&candidate.database, "new", 101),
        ] {
            conn.query_drop(format!("CREATE DATABASE {}", q(db)))
                .unwrap();
            conn.query_drop(format!("CREATE TABLE {} (id int NOT NULL AUTO_INCREMENT PRIMARY KEY,code varchar(20) UNIQUE,note varchar(40),previous_code varchar(20),CONSTRAINT parent_prev FOREIGN KEY(previous_code) REFERENCES {}(code))",qualified(db,"parent"),qualified(db,"parent"))).unwrap();
            conn.query_drop(format!(
                "INSERT INTO {}(id,code,note) VALUES({id},'a','{note}')",
                qualified(db, "parent")
            ))
            .unwrap();
            conn.query_drop(format!(
                "CREATE VIEW {} AS SELECT note,id FROM {}",
                qualified(db, "v"),
                qualified(db, "parent")
            ))
            .unwrap();
        }
        conn.query_drop(format!("CREATE TABLE {} (id int NOT NULL AUTO_INCREMENT PRIMARY KEY,parent_code varchar(20),choice enum('A','B') NOT NULL DEFAULT 'A',score int DEFAULT 7,score_twice int GENERATED ALWAYS AS (score*2) STORED,CONSTRAINT child_parent FOREIGN KEY(parent_code) REFERENCES {}(code)) COMMENT='keep child'",qualified(&original.database,"child"),qualified(&original.database,"parent"))).unwrap();
        conn.query_drop(format!(
            "INSERT INTO {}(id,parent_code) VALUES(1,'a')",
            qualified(&original.database, "child")
        ))
        .unwrap();
        let mode: String = conn
            .query_first("SELECT @@SESSION.sql_mode")
            .unwrap()
            .unwrap();
        conn.query_drop("SET SESSION sql_mode=REPLACE(@@SESSION.sql_mode,'STRICT_ALL_TABLES','')")
            .unwrap();
        conn.query_drop(format!(
            "INSERT INTO {}(id,parent_code,choice) VALUES(0,'a','')",
            qualified(&original.database, "child")
        ))
        .unwrap();
        conn.exec_drop("SET SESSION sql_mode=?", (mode,)).unwrap();
        conn.query_drop(format!(
            "CREATE VIEW {} AS SELECT p.note,c.id FROM {} p JOIN {} c ON p.code=c.parent_code",
            qualified(&original.database, "child_view"),
            qualified(&original.database, "parent"),
            qualified(&original.database, "child")
        ))
        .unwrap();
        conn.query_drop(format!(
            "CREATE TABLE {} (id int PRIMARY KEY)",
            qualified(&original.database, "unrelated")
        ))
        .unwrap();
        conn.query_drop(format!(
            "INSERT INTO {} VALUES(42)",
            qualified(&original.database, "unrelated")
        ))
        .unwrap();
        let dir = std::env::temp_dir().join(format!("tf-promote-test-{nonce}"));
        std::fs::create_dir(&dir).unwrap();
        Some((original, candidate, conn, dir))
    }
    fn cleanup(conn: &mut Db, plan: &PromotionPlan, dir: &Path) {
        let _ = conn.query_drop("UNLOCK TABLES");
        let _ = conn.query_drop("SET SESSION foreign_key_checks=0");
        for db in [
            &plan.original_database,
            &plan.candidate_database,
            &plan.clone_database,
            &plan.backup_database,
        ] {
            assert!(db.starts_with("tf_"));
            conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(db)))
                .unwrap();
        }
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn mysql_guarded_promotion_preserves_child_views_and_routes_queued_writer() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original, candidate, mut admin, dir)) = fixture() else {
            return;
        };
        let p = plan(&original, &candidate, "fixture", &[]).unwrap();
        assert!(p.original_tables.contains_key("child"));
        assert!(!p.original_tables.contains_key("unrelated"));
        let unrelated = table_id(&mut admin, &original.database, "unrelated").unwrap();
        let (sent, received) = mpsc::channel();
        let mut worker = None;
        let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _| {
            if phase == "locked" {
                let target = original.clone();
                let sent = sent.clone();
                worker = Some(std::thread::spawn(move || {
                    let mut c = connect(&target).unwrap();
                    c.query_drop("SET SESSION lock_wait_timeout=15").unwrap();
                    let result = c
                        .query_drop(format!(
                            "INSERT INTO {}(id,parent_code) VALUES(2,'a')",
                            qualified(&target.database, "child")
                        ))
                        .map_err(|e| e.to_string());
                    sent.send(result).unwrap();
                }));
                std::thread::sleep(std::time::Duration::from_millis(100));
                assert!(received.try_recv().is_err());
            }
            if phase == "before_rename" {
                assert!(received.try_recv().is_err());
            }
            Ok(())
        })
        .unwrap();
        if let Some(worker) = worker {
            worker.join().unwrap();
        }
        assert_eq!(result["status"], "promoted", "{result:#}");
        assert!(received
            .recv_timeout(std::time::Duration::from_secs(2))
            .unwrap()
            .is_ok());
        let rows: Vec<(String, u64)> = admin
            .query(format!(
                "SELECT note,id FROM {} ORDER BY id",
                qualified(&original.database, "child_view")
            ))
            .unwrap();
        assert_eq!(
            rows,
            vec![("new".into(), 0), ("new".into(), 1), ("new".into(), 2)]
        );
        let backup: u64 = admin
            .query_first(format!(
                "SELECT COUNT(*) FROM {}",
                qualified(&p.backup_database, "child")
            ))
            .unwrap()
            .unwrap();
        assert_eq!(backup, 2);
        assert_eq!(
            table_id(&mut admin, &original.database, "unrelated").unwrap(),
            unrelated
        );
        let values: (u64, u64) = admin
            .query_first(format!(
                "SELECT choice+0,score_twice FROM {} WHERE id=0",
                qualified(&original.database, "child")
            ))
            .unwrap()
            .unwrap();
        assert_eq!(values, (0, 14));
        cleanup(&mut admin, &p, &dir);
    }
    #[test]
    fn mysql_guarded_promotion_rename_failure_and_disconnect_leave_original_fk_intact() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        for failure in ["rename_collision", "disconnect"] {
            let Some((original, candidate, mut admin, dir)) = fixture() else {
                return;
            };
            let p = plan(&original, &candidate, "failure_fixture", &[]).unwrap();
            let id = table_id(&mut admin, &original.database, "parent").unwrap();
            let mut reached = false;
            let result =
                promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, conn| {
                    if phase == "before_rename" {
                        reached = true;
                        if failure == "rename_collision" {
                            admin
                                .query_drop(format!(
                                    "CREATE TABLE {}(id int)",
                                    qualified(&p.backup_database, "parent")
                                ))
                                .map_err(|e| e.to_string())?;
                        } else {
                            let id: Option<u64> = conn
                                .query_first("SELECT CONNECTION_ID()")
                                .map_err(|e| e.to_string())?;
                            admin
                                .query_drop(format!("KILL CONNECTION {}", id.unwrap()))
                                .map_err(|e| e.to_string())?;
                            return Err("injected cutover connection loss before swap".into());
                        }
                    }
                    Ok(())
                })
                .unwrap();
            assert!(reached, "fault hook was not reached: {result:#}");
            assert_eq!(result["status"], "failed_original_unchanged", "{result:#}");
            assert_eq!(
                table_id(&mut admin, &original.database, "parent").unwrap(),
                id
            );
            assert!(admin
                .query_drop(format!(
                    "INSERT INTO {}(id,parent_code) VALUES(99,'missing')",
                    qualified(&original.database, "child")
                ))
                .is_err());
            cleanup(&mut admin, &p, &dir);
        }
    }

    #[test]
    fn mysql_guarded_promotion_rejects_candidate_drift_and_new_dependencies() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        for defect in [
            "candidate_data",
            "candidate_counter",
            "incoming_fk",
            "dependent_view",
            "routine",
            "event",
        ] {
            let Some((original, candidate, mut admin, dir)) = fixture() else {
                return;
            };
            let p = plan(&original, &candidate, "drift", &[]).unwrap();
            let original_id = table_id(&mut admin, &original.database, "parent").unwrap();
            if defect == "candidate_data" {
                admin
                    .query_drop(format!(
                        "UPDATE {} SET note='tampered'",
                        qualified(&candidate.database, "parent")
                    ))
                    .unwrap();
            }
            if defect == "candidate_counter" {
                admin
                    .query_drop(format!(
                        "ALTER TABLE {} AUTO_INCREMENT=9001",
                        qualified(&candidate.database, "parent")
                    ))
                    .unwrap();
            }
            if defect == "incoming_fk" {
                admin.query_drop(format!("ALTER TABLE {} ADD COLUMN code varchar(20),ADD CONSTRAINT new_ref FOREIGN KEY(code) REFERENCES {}(code)",qualified(&original.database,"unrelated"),qualified(&original.database,"parent"))).unwrap();
            }
            let result =
                promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _| {
                    if phase == "validated" && defect == "dependent_view" {
                        admin
                            .query_drop(format!(
                                "CREATE VIEW {} AS SELECT note FROM {}",
                                qualified(&original.database, "late_view"),
                                qualified(&original.database, "parent")
                            ))
                            .map_err(|e| e.to_string())?;
                    }
                    if phase=="validated"&&defect=="routine" {
                        admin.query_drop(format!("CREATE PROCEDURE {}() SELECT 1",qualified(&original.database,"late_routine"))).map_err(|e|e.to_string())?;
                    }
                    if phase=="validated"&&defect=="event" {
                        admin.query_drop(format!("CREATE EVENT {} ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 1 DAY DO SET @tf_probe=1",qualified(&original.database,"late_event"))).map_err(|e|e.to_string())?;
                    }
                    Ok(())
                })
                .unwrap();
            assert_eq!(
                result["status"], "failed_original_unchanged",
                "{defect}: {result:#}"
            );
            assert_eq!(
                table_id(&mut admin, &original.database, "parent").unwrap(),
                original_id
            );
            let expected = match defect {
                "candidate_data" | "candidate_counter" => "candidate changed",
                "incoming_fk" => "inbound FK",
                "routine" | "event" => "stored programs changed",
                _ => "dependent view",
            };
            assert!(
                result["error"].as_str().unwrap().contains(expected),
                "{result:#}"
            );
            cleanup(&mut admin, &p, &dir);
        }
    }
    #[test]
    fn mysql_guarded_promotion_crash_checkpoint_and_known_commit_are_distinguished() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        for phase_to_fail in ["before_rename", "after_rename"] {
            let Some((original, candidate, mut admin, dir)) = fixture() else {
                return;
            };
            let p = plan(&original, &candidate, "crash", &[]).unwrap();
            if phase_to_fail == "before_rename" {
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _| {
                        if phase == phase_to_fail {
                            panic!("injected process-stop unwind before atomic swap");
                        }
                        Ok(())
                    })
                }));
                assert!(result.is_err());
                assert_eq!(
                    table_id(&mut admin, &original.database, "parent").unwrap(),
                    p.original_tables["parent"].table_id
                );
                assert!(admin
                    .query_drop(format!(
                        "INSERT INTO {}(id,parent_code) VALUES(99,'missing')",
                        qualified(&original.database, "child")
                    ))
                    .is_err());
                let path = dir
                    .join(format!("_tunnelforge_promotion_{}", p.nonce))
                    .join("_tunnelforge_import_report.json");
                let journal: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
                assert_eq!(journal["status"], "cutover_unknown");
                assert!(journal["new_table_ids"].is_object());
            } else {
                let result =
                    promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _| {
                        if phase == phase_to_fail {
                            return Err(
                                "injected observer failure after acknowledged rename".into()
                            );
                        }
                        Ok(())
                    })
                    .unwrap();
                assert_eq!(result["status"], "promoted", "{result:#}");
                assert_eq!(
                    table_id(&mut admin, &original.database, "parent").unwrap(),
                    p.candidate_tables["parent"].table_id
                );
            }
            cleanup(&mut admin, &p, &dir);
        }
    }
    #[test]
    fn mysql_guarded_promotion_bounded_lock_and_unpreparable_view_guard() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original, candidate, mut admin, dir)) = fixture() else {
            return;
        };
        let p = plan(&original, &candidate, "locks", &[]).unwrap();
        let mut lock_attempted = false;
        let result =
            promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, conn| {
                if phase == "before_lock" {
                    let timeouts: Option<(u64, u64)> = conn
                        .query_first(
                            "SELECT @@SESSION.lock_wait_timeout,@@SESSION.innodb_lock_wait_timeout",
                        )
                        .map_err(|e| e.to_string())?;
                    assert_eq!(timeouts, Some((2, 2)));
                    lock_attempted = true;
                }
                if phase == "validated" {
                    admin
                        .query_drop(format!(
                            "LOCK TABLES {} READ,{} READ",
                            qualified(&original.database, "parent"),
                            qualified(&original.database, "child")
                        ))
                        .map_err(|e| e.to_string())?;
                }
                Ok(())
            })
            .unwrap();
        admin.query_drop("UNLOCK TABLES").unwrap();
        assert_eq!(result["status"], "failed_original_unchanged", "{result:#}");
        assert!(lock_attempted, "lock guard was not reached: {result:#}");
        assert_eq!(result["phase"], "acquiring_write_locks");
        assert!(
            result["error"].as_str().unwrap().contains("1205"),
            "expected server-side lock timeout: {result:#}"
        );
        admin
            .query_drop(format!(
                "ALTER TABLE {} ADD COLUMN new_only int",
                qualified(&candidate.database, "parent")
            ))
            .unwrap();
        admin
            .query_drop(format!(
                "CREATE OR REPLACE VIEW {} AS SELECT new_only FROM {}",
                qualified(&candidate.database, "v"),
                qualified(&candidate.database, "parent")
            ))
            .unwrap();
        let error = plan(&original, &candidate, "unpreparable", &[]).unwrap_err();
        assert!(error.contains("cannot be prepared"), "{error}");
        cleanup(&mut admin, &p, &dir);
    }

    #[test]
    fn mysql_guarded_promotion_covers_nested_views_and_additional_target_only_bases() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original, candidate, mut admin, dir)) = fixture() else {
            return;
        };
        admin
            .query_drop(format!(
                "CREATE TABLE {}(code varchar(20) PRIMARY KEY,label varchar(30))",
                qualified(&original.database, "dimension")
            ))
            .unwrap();
        admin
            .query_drop(format!(
                "INSERT INTO {} VALUES('a','retained')",
                qualified(&original.database, "dimension")
            ))
            .unwrap();
        admin
            .query_drop(format!(
                "CREATE VIEW {} AS SELECT p.note,d.label FROM {} p JOIN {} d ON p.code=d.code",
                qualified(&original.database, "joined_extra"),
                qualified(&original.database, "parent"),
                qualified(&original.database, "dimension")
            ))
            .unwrap();
        admin
            .query_drop(format!(
                "CREATE VIEW {} AS SELECT note,label FROM {}",
                qualified(&original.database, "nested_extra"),
                qualified(&original.database, "joined_extra")
            ))
            .unwrap();
        let p = plan(&original, &candidate, "nested", &[]).unwrap();
        assert!(p.original_tables.contains_key("dimension"));
        assert!(p.replacement_views.contains_key("nested_extra"));
        assert!(!p.original_tables.contains_key("unrelated"));
        let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
        assert_eq!(result["status"], "promoted", "{result:#}");
        let row: Option<(String, String)> = admin
            .query_first(format!(
                "SELECT note,label FROM {}",
                qualified(&original.database, "nested_extra")
            ))
            .unwrap();
        assert_eq!(row, Some(("new".into(), "retained".into())));
        cleanup(&mut admin, &p, &dir);
    }

    #[test]
    fn metadata_visibility_requires_direct_global_grants_without_role_or_revoke_ambiguity() {
        assert!(has_complete_metadata_grants(&[
            "GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO `operator`@`%`".into()
        ]));
        assert!(has_complete_metadata_grants(&[
            "GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION".into()
        ]));
        assert!(!has_complete_metadata_grants(&[
            "GRANT ALL PRIVILEGES ON `app`.* TO `operator`@`%`".into(),
            "GRANT PROCESS ON *.* TO `operator`@`%`".into()
        ]));
        assert!(!has_complete_metadata_grants(&[
            "GRANT `admin_role`@`%` TO `operator`@`%`".into()
        ]));
        assert!(!has_complete_metadata_grants(&[
            "GRANT ALL PRIVILEGES ON *.* TO `root`@`%`".into(),
            "REVOKE SELECT ON `hidden`.* FROM `root`@`%`".into()
        ]));
    }
    #[test]
    fn mysql_guarded_promotion_rejects_operator_with_hidden_external_fk_metadata() {
        let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let Some((original, candidate, mut admin, dir)) = fixture() else {
            return;
        };
        let p = plan(&original, &candidate, "visibility", &[]).unwrap();
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let user = format!("tf_pv_{nonce}");
        let external = format!("{}_x", original.database);
        admin
            .query_drop(format!("CREATE DATABASE {}", q(&external)))
            .unwrap();
        admin.query_drop(format!("CREATE TABLE {}(id int PRIMARY KEY,code varchar(20),CONSTRAINT outside_fk FOREIGN KEY(code) REFERENCES {}(code))",qualified(&external,"outside_child"),qualified(&original.database,"parent"))).unwrap();
        admin
            .query_drop(format!(
                "CREATE USER {}@'%' IDENTIFIED BY 'tf_local_visibility'",
                q(&user)
            ))
            .unwrap();
        for db in [&original.database, &candidate.database] {
            admin
                .query_drop(format!(
                    "GRANT ALL PRIVILEGES ON {}.* TO {}@'%'",
                    q(db),
                    q(&user)
                ))
                .unwrap();
        }
        admin
            .query_drop(format!(
                "GRANT PROCESS,SESSION_VARIABLES_ADMIN ON *.* TO {}@'%'",
                q(&user)
            ))
            .unwrap();
        let mut restricted = original.clone();
        restricted.user = user.clone();
        restricted.password = "tf_local_visibility".into();
        let mut restricted_candidate = candidate.clone();
        restricted_candidate.user = user.clone();
        restricted_candidate.password = restricted.password.clone();
        let mut limited = connect(&restricted).unwrap();
        let sql="SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=? AND REFERENCED_TABLE_SCHEMA=?";
        let hidden: Option<u64> = limited
            .exec_first(sql, (&external, &original.database))
            .unwrap();
        let visible: Option<u64> = admin
            .exec_first(sql, (&external, &original.database))
            .unwrap();
        let refused = plan(&restricted, &restricted_candidate, "visibility_guard", &[]);
        let intact = table_id(&mut admin, &original.database, "parent").unwrap()
            == p.original_tables["parent"].table_id;
        drop(limited);
        admin
            .query_drop(format!("DROP USER {}@'%'", q(&user)))
            .unwrap();
        admin
            .query_drop(format!("DROP DATABASE {}", q(&external)))
            .unwrap();
        cleanup(&mut admin, &p, &dir);
        assert_eq!(hidden, Some(0));
        assert_eq!(visible, Some(1));
        assert!(refused.unwrap_err().contains("global SELECT"));
        assert!(intact);
    }
    #[test]
    fn identity_read_errors_are_unknown_even_when_original_namespace_was_empty() {
        let old = BTreeMap::new();
        let new = BTreeMap::from([("parent".to_string(), 42)]);
        assert_eq!(
            classify_table_ids(&old, &new, |_, _| Err("metadata privilege lost".into())),
            "cutover_unknown"
        );
        assert_eq!(
            classify_table_ids(&old, &new, |_, _| Ok(None)),
            "failed_original_unchanged"
        );
        assert_eq!(
            classify_table_ids(&old, &new, |_, _| Ok(Some(42))),
            "promoted"
        );
        assert_eq!(
            classify_table_ids(&old, &BTreeMap::new(), |_, _| Ok(None)),
            "cutover_unknown"
        );
    }
    #[test]
    fn plans_name_the_tables_and_views_outside_the_dump_they_would_rebuild() {
        let table = |name: &str| json!({"name": name, "ddl": "", "static_digest": "", "table_id": 1, "columns": [], "normalized": {"name": name}, "content_digest": null});
        let view = |ddl: &str| json!({"name": "v", "ddl": ddl, "dependencies": []});
        let plan: PromotionPlan = serde_json::from_value(json!({
            "restore_id": "r", "digest": "", "original_database": "o", "candidate_database": "c", "host": "h", "port": 1,
            "server_uuid": "u", "nonce": "1", "backup_database": "tf_backup_1", "clone_database": "tf_promote_1",
            "original_tables": {"parent": table("parent"), "child": table("child")}, "candidate_tables": {"parent": table("parent")},
            "original_views": {"dump_view": view("old"), "child_view": view("kept"), "version": view("select '1.2'")},
            // A dump view with no table references has the same definition in the original and the copy.
            "replacement_views": {"dump_view": view("new"), "child_view": view("kept"), "version": view("select '1.2'")},
            "original_fks": [], "candidate_fks": [], "hidden_views": {}, "saved_views": {}, "view_order": [], "warnings": [],
        })).unwrap();
        let target_only_views = ["child_view".to_string(), "unlinked_view".to_string()].into_iter().collect();
        assert_eq!(rebuilt_outside_dump(&plan, &target_only_views), ["child", "child_view"]);
    }

    #[test]
    fn native_rewriters_preserve_quoted_literals_and_reject_ambiguous_view_qualifiers() {
        let table=NativeTable{name:"child".into(),ddl:"CREATE TABLE `child` (`note` varchar(40) DEFAULT 'src.parent', CONSTRAINT `fk` FOREIGN KEY (`id`) REFERENCES `parent` (`id`))".into(),static_digest:String::new(),table_id:1,columns:vec![],normalized:serde_json::from_value(json!({"name":"child"})).unwrap(),content_digest:None};
        let locations = BTreeMap::from([("parent".into(), "candidate".into())]);
        let ddl = rewrite_table(&table, "src", "clone", &locations).unwrap();
        assert!(ddl.contains("DEFAULT 'src.parent'"));
        assert!(ddl.contains("REFERENCES `candidate`.`parent`"));
        assert_eq!(
            static_ddl("CREATE TABLE t(a int) AUTO_INCREMENT=12 COMMENT='AUTO_INCREMENT=42'"),
            "CREATE TABLE t(a int) AUTO_INCREMENT=<allocator> COMMENT='AUTO_INCREMENT=42'"
        );
    }
}
