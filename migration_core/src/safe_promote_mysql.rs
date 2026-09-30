//! Guarded same-name MySQL promotion. Only one statement changes original
//! objects: a multi-object RENAME. Original foreign keys are never dropped.
use crate::*;
use mysql::prelude::Queryable;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

type Db = mysql::PooledConn;
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
    let pool = mysql::Pool::new(mysql_opts(endpoint)).map_err(|e| error("connect", e))?;
    let mut conn = pool.get_conn().map_err(|e| error("connect", e))?;
    for sql in ["SET SESSION lock_wait_timeout=2","SET SESSION innodb_lock_wait_timeout=2","SET SESSION information_schema_stats_expiry=0","SET SESSION autocommit=1","SET SESSION time_zone='+00:00'","SET SESSION sql_mode=CONCAT_WS(',', 'STRICT_ALL_TABLES','NO_AUTO_VALUE_ON_ZERO', REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(@@SESSION.sql_mode,'NO_BACKSLASH_ESCAPES',''),'STRICT_TRANS_TABLES',''),'ANSI_QUOTES',''),'NO_ZERO_DATE',''),'NO_ZERO_IN_DATE',''))","SET SESSION max_error_count=65535"] {
        conn.query_drop(sql).map_err(|e|error("configure session",e))?;
    }
    // SHOW CREATE VIEW omits schema qualifiers for the current default schema.
    // Use a neutral namespace so every native dependency is explicit.
    conn.query_drop("USE information_schema")
        .map_err(|e| e.to_string())?;
    Ok(conn)
}

fn has_complete_metadata_grants(grants: &[String]) -> bool {
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
        projected_text_columns_sql("mysql", table),
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
    let triggers: Vec<String> = conn
        .exec(
            "SELECT EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=?",
            (db,),
        )
        .map_err(|e| e.to_string())?;
    if triggers.iter().any(|table| names.contains(table)) {
        return Err(
            "safe_promotion: affected tables with triggers cannot be moved between schemas".into(),
        );
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

pub(crate) fn plan(
    original: &Endpoint,
    candidate: &Endpoint,
    restore_id: &str,
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
    let mut result = PromotionPlan {
        restore_id: restore_id.into(), digest: String::new(), host: original.host.clone(), port: original.port,
        original_database: original.database.clone(), candidate_database: candidate.database.clone(),
        server_uuid: conn.query_first("SELECT @@server_uuid").map_err(|e|e.to_string())?.ok_or("server UUID unavailable")?,
        backup_database: format!("tf_backup_{nonce}"), clone_database: format!("tf_promote_{nonce}"), nonce,
        original_tables: old, candidate_tables: new,
        original_fks: ofks.into_iter().filter(|f|closure.contains(&f.child)).collect(), candidate_fks: cfks,
        view_order: view_order(&replacement_views)?, original_views, replacement_views,
        hidden_views: hidden, saved_views: saved,
        warnings: vec![
            "Existing replaced-table writes before the cutover are intentionally superseded by the restored candidate.".into(),
            "Target-only tables in the affected dependency component are copied while WRITE locks pause their readers and writers.".into(),
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
        "lock_acquisition_timeout_seconds":2,"write_pause":"Affected readers and writers wait while target-only rows are refreshed and verified, then one atomic RENAME switches names.","warnings":plan.warnings})
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
    for database in [&plan.original_database, &plan.candidate_database] {
        let routines: Option<u64> = conn
            .exec_first(
                "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=?",
                (database,),
            )
            .map_err(|e| e.to_string())?;
        let events: Option<u64> = conn
            .exec_first(
                "SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?",
                (database,),
            )
            .map_err(|e| e.to_string())?;
        if routines.unwrap_or(0) > 0 || events.unwrap_or(0) > 0 {
            return Err(format!("guarded promotion cannot prove stored routine/event compatibility in {database}: {} routines, {} events; retain the verified candidate as a separate namespace",routines.unwrap_or(0),events.unwrap_or(0)));
        }
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
                "target-only rows are incompatible with restored parent: {}.{}",
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
    classify_table_ids(&old_ids, new_ids, |backup, name| {
        optional_table_id(
            conn,
            if backup {
                &plan.backup_database
            } else {
                &plan.original_database
            },
            name,
        )
    })
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
    write_dump_import_report(&journal_dir, &state)?;
    let mut conn = connect(original)?;
    let mut created_backup = false;
    let mut created_clone = false;
    let mut hidden_created = Vec::new();
    let mut locked = false;
    let mut rename_attempted = false;
    let mut rename_committed = false;
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
        hook("before_rename", &mut conn)?;
        rename_attempted = true;
        conn.query_drop(rename_sql(plan))
            .map_err(|e| error("atomic rename", e))?;
        rename_committed = true;
        state["status"] = json!("promoted");
        state["success"] = json!(true);
        state["original_unchanged"] = json!(false);
        state["phase"] = json!("promoted_locked");
        write_dump_import_report(&journal_dir, &state)?;
        hook("after_rename", &mut conn)?;
        Ok(())
    })();
    if locked {
        let _ = conn.query_drop("UNLOCK TABLES");
    }
    drop(conn);
    if let Err(failure) = outcome {
        let mut cleanup = connect(original).ok();
        let status = if rename_committed {
            "promoted".into()
        } else if rename_attempted {
            cleanup
                .as_mut()
                .map(|db| classify(db, plan, &new_ids, cutover_session))
                .unwrap_or_else(|| "cutover_unknown".into())
        } else {
            "failed_original_unchanged".into()
        };
        state["status"] = json!(status);
        state["success"] = json!(status == "promoted");
        state["error"] = json!(redact_endpoint_secret(&failure, original));
        state["original_unchanged"] = if status == "failed_original_unchanged" {
            json!(true)
        } else if status == "promoted" {
            json!(false)
        } else {
            Value::Null
        };
        if status == "failed_original_unchanged" {
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
    state["message"]=json!(match state["status"].as_str() { Some("promoted")=>"Verified candidate promoted at the original database name. Original tables are retained in the recorded backup namespace.",Some("failed_original_unchanged")=>"Promotion did not change original tables or their foreign keys. The verified candidate remains available.",_=>"Cutover outcome is unknown. Retain all recorded namespaces and inspect table identities before any retry or recovery." });
    state["interrupted_attempt_policy"]=json!("A checkpoint at cutover_started is UNKNOWN after process loss. Compare recorded InnoDB table identities at original/candidate/backup locations; never retry or roll back blindly.");
    if state["status"] == "promoted" {
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
pub(crate) fn inspect_backup(endpoint: &Endpoint, plan: &PromotionPlan, deep: bool, journal_fingerprint: Option<&Value>) -> Result<Value, String> {
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
    result["verdict"] = json!(match verdict.as_str() {
        "promoted" => "promoted",
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
    for database in [original, backup] {
        for (sql, what) in [
            ("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=?", "routines"),
            ("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?", "events"),
            ("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=?", "triggers"),
        ] {
            let count: Option<u64> = conn.exec_first(sql, (database,)).map_err(|e| e.to_string())?;
            if count.unwrap_or(0) > 0 {
                blockers.push(format!("{database} contains {what}, which recovery cannot certify"));
            }
        }
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
    let mut conn = lifecycle_connect(endpoint, plan)?;
    let blockers = rollback_blockers(&mut conn, plan, fingerprint, new_ids, Some(displaced), None)?;
    if !blockers.is_empty() {
        return Err(format!("recovery is blocked: {}", blockers.join("; ")));
    }
    let original = plan.original_database.as_str();
    let backup = plan.backup_database.as_str();
    let active = active_names(plan);
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
    let outcome = (|| -> Result<(), String> {
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
        conn.query_drop(format!("LOCK TABLES {}", locks.into_iter().collect::<Vec<_>>().join(", ")))
            .map_err(|e| error("bounded WRITE lock acquisition (2 seconds)", e))?;
        locked = true;
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
        state["phase"] = json!("rename_started");
        state["status"] = json!("rollback_unknown");
        write_dump_import_report(journal_dir, &state)?;
        conn.query_drop(format!("RENAME TABLE {}", renames.join(", "))).map_err(|e| error("atomic recovery rename", e))?;
        renamed = true;
        Ok(())
    })();
    if locked {
        let _ = conn.query_drop("UNLOCK TABLES");
    }
    let mut verify = connect(endpoint).ok();
    let status = match verify.as_mut() {
        None => "rollback_unknown".to_string(),
        Some(db) => {
            // Classify by identity: a lost reply must not be read as "nothing happened".
            let id_of = |db: &mut Db, ns: &str, n: &str| optional_table_id(db, ns, n).ok().flatten();
            let restored = old_ids.iter().all(|(n, id)| id_of(db, original, n) == Some(*id));
            let displaced_ok = new_ids_map.iter().all(|(n, id)| id_of(db, displaced, n) == Some(*id));
            let intact = new_ids_map.iter().all(|(n, id)| id_of(db, original, n) == Some(*id));
            if restored && displaced_ok {
                "rolled_back".to_string()
            } else if intact && !renamed {
                "failed_no_change".to_string()
            } else {
                "rollback_unknown".to_string()
            }
        }
    };
    if status == "failed_no_change" && created {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{mpsc, Mutex};
    static LIVE_LOCK: Mutex<()> = Mutex::new(());
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
        let p = plan(&original, &candidate, "fixture").unwrap();
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
            let p = plan(&original, &candidate, "failure_fixture").unwrap();
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
            let p = plan(&original, &candidate, "drift").unwrap();
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
                "routine" | "event" => "routine/event compatibility",
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
            let p = plan(&original, &candidate, "crash").unwrap();
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
        let p = plan(&original, &candidate, "locks").unwrap();
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
        let error = plan(&original, &candidate, "unpreparable").unwrap_err();
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
        let p = plan(&original, &candidate, "nested").unwrap();
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
        let p = plan(&original, &candidate, "visibility").unwrap();
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
        let refused = plan(&restricted, &restricted_candidate, "visibility_guard");
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
