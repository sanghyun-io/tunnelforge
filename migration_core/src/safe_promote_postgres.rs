//! Transactional PostgreSQL cutover; original relations remain in an owned backup.
use crate::*;
use postgres::{Client, GenericClient};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::Path;

#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct PromotionPlan {
    pub plan_digest: String,
    pub original_schema: String,
    pub candidate_schema: String,
    pub backup_schema: String,
    pub restore_id: String,
    attempt_nonce: String,
    endpoint: Value,
    inventory: Vec<Value>,
    dependencies: Value,
    incoming: Vec<IncomingKey>,
}

#[derive(Clone, Serialize, Deserialize)]
struct IncomingKey {
    table: String,
    name: String,
    definition: String,
    comment: Option<String>,
}

fn q(name: &str) -> String {
    quote_ident("postgresql", name)
}
fn qualified(schema: &str, name: &str) -> String {
    format!("{}.{}", q(schema), q(name))
}
fn literal(text: &str) -> String {
    format!("E'{}'", text.replace('\\', "\\\\").replace('\'', "''"))
}
fn endpoint_identity(endpoint: &Endpoint) -> Value {
    json!({"engine":endpoint.engine,"host":endpoint.host,"port":endpoint.port,"database":endpoint.database,"user":endpoint.user})
}
fn connect(endpoint: &Endpoint) -> Result<Client, String> {
    connect_postgres(endpoint).map_err(|e| e.to_string())
}
fn digest(plan: &PromotionPlan) -> Result<String, String> {
    let mut copy = plan.clone();
    copy.plan_digest.clear();
    Ok(hex::encode(Sha256::digest(
        serde_json::to_vec(&copy).map_err(|e| e.to_string())?,
    )))
}
fn inventory_digest(plan: &PromotionPlan) -> Result<String, String> {
    let mut copy = plan.clone();
    copy.attempt_nonce.clear();
    digest(&copy)
}
fn objects(db: &mut impl GenericClient, schemas: &[String]) -> Result<Vec<Value>, String> {
    db.query(r#"SELECT jsonb_build_object('schema',n.nspname,'name',c.relname,'oid',c.oid::bigint,
      'kind',c.relkind::text,'owner',pg_get_userbyid(c.relowner),'acl',c.relacl::text,
      'options',c.reloptions,'rls',c.relrowsecurity,'partition',c.relispartition,
      'comment',obj_description(c.oid,'pg_class'),'persistence',c.relpersistence::text,
      'definition',CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,false) ELSE NULL END,
      'columns',(SELECT jsonb_agg(jsonb_build_object('name',a.attname,'type',a.atttypid::bigint,
        'mod',a.atttypmod,'null',a.attnotnull,'acl',a.attacl::text,'identity',a.attidentity::text,
        'default',pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum) FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),
      'constraints',(SELECT jsonb_agg(jsonb_build_object('name',k.conname,'def',pg_get_constraintdef(k.oid,false),
        'comment',obj_description(k.oid,'pg_constraint')) ORDER BY k.conname) FROM pg_constraint k WHERE k.conrelid=c.oid),
      'index',CASE WHEN c.relkind='i' THEN pg_get_indexdef(c.oid) ELSE NULL END,
      'sequence',(SELECT to_jsonb(s) FROM pg_sequence s WHERE s.seqrelid=c.oid))::text
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname=ANY($1) ORDER BY n.nspname,c.relname"#,&[&schemas]).map_err(|e|e.to_string())?
      .into_iter().map(|row|serde_json::from_str(row.get::<_,&str>(0)).map_err(|e|e.to_string())).collect()
}
fn name(object: &Value) -> &str {
    object["name"].as_str().unwrap_or("")
}
fn namespace(object: &Value) -> &str {
    object["schema"].as_str().unwrap_or("")
}
fn tables<'a>(inventory: &'a [Value], schema: &str) -> Vec<&'a Value> {
    inventory
        .iter()
        .filter(|o| namespace(o) == schema && o["kind"] == "r")
        .collect()
}
fn view_shape(object: &Value) -> Value {
    json!(object["columns"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|c| json!([c["name"], c["type"], c["mod"]]))
        .collect::<Vec<_>>())
}
fn dependencies(db: &mut impl GenericClient, schemas: &[String]) -> Result<Value, String> {
    let text:String=db.query_one(r#"WITH owned AS (SELECT c.oid,c.reltype FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY($1))
      SELECT COALESCE(jsonb_agg(to_jsonb(d) ORDER BY d.classid,d.objid,d.objsubid,d.refclassid,d.refobjid,d.refobjsubid,d.deptype),'[]'::jsonb)::text
      FROM pg_depend d WHERE (d.refclassid='pg_class'::regclass AND d.refobjid IN(SELECT oid FROM owned))
      OR (d.refclassid='pg_type'::regclass AND d.refobjid IN(SELECT reltype FROM owned))
      OR (d.classid='pg_class'::regclass AND d.objid IN(SELECT oid FROM owned))"#,&[&schemas]).map_err(|e|e.to_string())?.get(0);
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

pub(crate) fn summary(planned: &PromotionPlan) -> Value {
    let selected: BTreeSet<_> = tables(&planned.inventory, &planned.candidate_schema)
        .iter()
        .map(|o| name(o).to_string())
        .collect();
    let extra: Vec<_> = tables(&planned.inventory, &planned.original_schema)
        .iter()
        .filter(|o| !selected.contains(name(o)))
        .map(|o| name(o).to_string())
        .collect();
    let changed: Vec<_> = tables(&planned.inventory, &planned.candidate_schema)
        .iter()
        .filter(|new| {
            planned
                .inventory
                .iter()
                .find(|old| namespace(old) == planned.original_schema && name(old) == name(new))
                .is_some_and(|old| {
                    old["columns"] != new["columns"] || old["constraints"] != new["constraints"]
                })
        })
        .map(|o| name(o).to_string())
        .collect();
    let views: BTreeSet<_> = planned
        .inventory
        .iter()
        .filter(|o| o["kind"] == "v")
        .map(|o| name(o).to_string())
        .collect();
    json!({"selected_tables":selected,"target_only_tables_preserved":extra,"views_replaced":views,
        "backup_namespace":planned.backup_schema,"schema_changed_tables":changed,"data_comparison":"not_compared",
        "warnings":["Application writes are blocked while candidate content is reverified and the transaction commits.",
                    "Original and candidate row values have not been compared; matching schema does not mean matching data.",
                    "Original tables remain in the backup schema; no automatic backup deletion or retry is performed."]})
}

pub(crate) fn plan(
    original: &Endpoint,
    candidate: &Endpoint,
    restore_id: &str,
) -> Result<PromotionPlan, String> {
    let from = endpoint_schema(original);
    let staged = endpoint_schema(candidate);
    if original.engine != "postgresql"
        || endpoint_identity(original) != endpoint_identity(candidate)
        || from == staged
        || restore_id.is_empty()
        || !restore_id.bytes().all(|b| b.is_ascii_digit() || b == b'_')
        || staged != format!("tf_restore_{restore_id}")
        || from.starts_with("pg_")
        || from == "information_schema"
    {
        return Err(
            "promotion requires an owned PostgreSQL candidate on the original database".into(),
        );
    }
    let mut db = connect(original)?;
    // One catalog snapshot binds guards and the dependency fingerprint together.
    db.batch_execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL search_path TO pg_catalog; SET LOCAL statement_timeout='15s'").map_err(|e|e.to_string())?;
    let schemas = vec![from.clone(), staged.clone()];
    let inventory = objects(&mut db, &schemas)?;
    let current: String = db
        .query_one("SELECT current_user", &[])
        .map_err(|e| e.to_string())?
        .get(0);
    for object in &inventory {
        if !matches!(object["kind"].as_str(), Some("r" | "v" | "i" | "S"))
            || object["owner"] != current
            || !object["acl"].is_null()
            || !object["options"].is_null()
            || object["rls"] == true
            || object["partition"] == true
            || object["persistence"] != "p"
            || object["columns"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|c| !c["acl"].is_null())
        {
            return Err(format!("promotion cannot preserve ownership, grants, policy or specialized relation {}.{}; retain the verified candidate namespace",namespace(object),name(object)));
        }
    }
    let unsupported:bool=db.query_one(r#"SELECT
      EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY($1)) OR
      EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY($1) AND NOT t.tgisinternal) OR
      EXISTS(SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY($1) AND r.rulename<>'_RETURN') OR
      EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_inherits i ON i.inhparent=c.oid OR i.inhrelid=c.oid WHERE n.nspname=ANY($1)) OR
      EXISTS(SELECT 1 FROM pg_depend d JOIN pg_class c ON d.refclassid='pg_type'::regclass AND d.refobjid=c.reltype
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY($1) AND d.deptype='n') OR
      EXISTS(SELECT 1 FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace
        WHERE n.nspname=ANY($1) AND s.relkind='S' AND NOT EXISTS
        (SELECT 1 FROM pg_depend own WHERE own.classid='pg_class'::regclass AND own.objid=s.oid
         AND own.refclassid='pg_class'::regclass AND own.deptype IN ('a','i'))) OR
      EXISTS(SELECT 1 FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace
        JOIN pg_depend own ON own.classid='pg_class'::regclass AND own.objid=s.oid AND own.refclassid='pg_class'::regclass AND own.deptype IN ('a','i')
        JOIN pg_depend ref ON ref.refclassid='pg_class'::regclass AND ref.refobjid=s.oid AND ref.classid='pg_attrdef'::regclass
        JOIN pg_attrdef a ON a.oid=ref.objid WHERE n.nspname=ANY($1) AND s.relkind='S' AND a.adrelid<>own.refobjid) OR
      EXISTS(SELECT 1 FROM pg_depend d JOIN pg_class c ON c.oid=d.objid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE d.classid='pg_class'::regclass AND d.deptype='e' AND n.nspname=ANY($1))"#,&[&schemas]).map_err(|e|e.to_string())?.get(0);
    if unsupported {
        return Err("promotion rejects routines, triggers, rules, inheritance, row-type dependencies, unowned/shared sequences and extension-owned objects; retain the candidate namespace".into());
    }
    let selected: BTreeSet<String> = tables(&inventory, &staged)
        .iter()
        .map(|o| name(o).to_string())
        .collect();
    if selected.is_empty() {
        return Err("candidate has no tables".into());
    }
    for table in &selected {
        if inventory
            .iter()
            .any(|o| namespace(o) == from && name(o) == table && o["kind"] != "r")
        {
            return Err(format!(
                "original name {table} belongs to a non-table object"
            ));
        }
    }
    // Reject dependencies whose binding cannot be rebuilt inside these namespaces.
    let external:bool=db.query_one(r#"SELECT
      EXISTS(SELECT 1 FROM pg_rewrite r JOIN pg_class v ON v.oid=r.ev_class JOIN pg_namespace vn ON vn.oid=v.relnamespace
        JOIN pg_depend d ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid AND d.refclassid='pg_class'::regclass
        JOIN pg_class t ON t.oid=d.refobjid JOIN pg_namespace tn ON tn.oid=t.relnamespace
        WHERE (tn.nspname=ANY($1) AND (NOT vn.nspname=ANY($1) OR v.relkind<>'v'))
           OR (vn.nspname=ANY($1) AND NOT tn.nspname=ANY($1) AND tn.nspname NOT IN ('pg_catalog','information_schema'))) OR
      EXISTS(SELECT 1 FROM pg_depend d JOIN pg_class t ON
        (d.refclassid='pg_class'::regclass AND d.refobjid=t.oid) OR (d.refclassid='pg_type'::regclass AND d.refobjid=t.reltype)
        JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=ANY($1) AND d.classid='pg_proc'::regclass)"#,&[&schemas]).map_err(|e|e.to_string())?.get(0);
    if external {
        return Err(
            "promotion rejects external view/materialized-view/routine dependencies".into(),
        );
    }
    let mut incoming = Vec::new();
    for row in db.query(r#"SELECT cn.nspname,c.relname,pn.nspname,p.relname,k.conname,pg_get_constraintdef(k.oid,false),
      obj_description(k.oid,'pg_constraint'),k.convalidated FROM pg_constraint k
      JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace cn ON cn.oid=c.relnamespace
      JOIN pg_class p ON p.oid=k.confrelid JOIN pg_namespace pn ON pn.oid=p.relnamespace
      WHERE k.contype='f' AND (cn.nspname=ANY($1) OR pn.nspname=ANY($1)) ORDER BY cn.nspname,c.relname,k.conname"#,&[&schemas]).map_err(|e|e.to_string())? {
        let child_schema:String=row.get(0);let child:String=row.get(1);let parent_schema:String=row.get(2);let parent:String=row.get(3);
        if child_schema!=parent_schema || !row.get::<_,bool>(7) {
            return Err("promotion rejects cross-schema or NOT VALID foreign keys".into());
        }
        if child_schema==from && selected.contains(&child) && !selected.contains(&parent) {
            return Err("original backup would retain an outgoing FK to a live target-only table".into());
        }
        if child_schema==from && !selected.contains(&child) && selected.contains(&parent) {
            incoming.push(IncomingKey{table:child,name:row.get(4),definition:row.get(5),comment:row.get(6)});
        }
    }
    for view in inventory
        .iter()
        .filter(|o| namespace(o) == staged && o["kind"] == "v")
    {
        if let Some(old) = inventory
            .iter()
            .find(|o| namespace(o) == from && name(o) == name(view))
        {
            if old["kind"] != "v" || view_shape(old) != view_shape(view) {
                return Err(format!(
                    "view {} changes its existing column shape",
                    name(view)
                ));
            }
        }
    }
    let backup_schema = format!("tf_backup_{restore_id}");
    if backup_schema.len() > 63 {
        return Err("backup namespace identifier is too long".into());
    }
    let dependencies = dependencies(&mut db, &schemas)?;
    let attempt_nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos()
        .to_string();
    let mut planned = PromotionPlan {
        plan_digest: String::new(),
        original_schema: from,
        candidate_schema: staged,
        backup_schema,
        restore_id: restore_id.into(),
        attempt_nonce,
        endpoint: endpoint_identity(original),
        inventory,
        dependencies,
        incoming,
    };
    planned.plan_digest = digest(&planned)?;
    db.batch_execute("COMMIT").map_err(|e| e.to_string())?;
    Ok(planned)
}

pub(crate) fn promote(
    original: &Endpoint,
    candidate: &Endpoint,
    planned: &PromotionPlan,
    confirmed_digest: &str,
    report_dir: &Path,
) -> Result<Value, String> {
    if confirmed_digest != planned.plan_digest
        || digest(planned)? != planned.plan_digest
        || planned.endpoint != endpoint_identity(original)
        || endpoint_schema(original) != planned.original_schema
        || endpoint_schema(candidate) != planned.candidate_schema
    {
        return Err("promotion confirmation does not match the reviewed plan".into());
    }
    let fresh = plan(original, candidate, &planned.restore_id)?;
    if inventory_digest(&fresh)? != inventory_digest(planned)? {
        return Err("promotion inventory changed after review".into());
    }
    let verified = crate::import::safe_restore::load_verified_plan(
        &dump_import_report_path(report_dir)?,
        original,
    )?;
    if verified.restore_id != planned.restore_id || verified.candidate != *candidate {
        return Err("verified candidate differs from promotion plan".into());
    }
    if !planned
        .attempt_nonce
        .bytes()
        .all(|byte| byte.is_ascii_digit())
        || planned.attempt_nonce.is_empty()
    {
        return Err("invalid promotion attempt nonce".into());
    }
    let journal_dir = report_dir.join(format!(
        "promotion-postgres-{}-{}",
        planned.restore_id, planned.attempt_nonce
    ));
    std::fs::create_dir(&journal_dir)
        .map_err(|e| format!("cannot create fresh promotion journal: {e}"))?;
    let mut state = json!({"success":false,"status":"preparing","phase":"lock_and_verify","original_unchanged":true,
        "backup_namespace":planned.backup_schema,"original_schema":planned.original_schema,"candidate_schema":planned.candidate_schema,
        "plan_digest":planned.plan_digest,"report_path":dump_import_report_path(&journal_dir)?.display().to_string()});
    write_dump_import_report(&journal_dir, &state)?;
    let mut db = connect(original)?;
    db.query_one(
        "SELECT set_config('application_name',$1,false)",
        &[&format!("tf_promote_{}", planned.restore_id)],
    )
    .map_err(|e| e.to_string())?;
    let mut tx = db.transaction().map_err(|e| e.to_string())?;
    let outcome = (|| -> Result<(), String> {
        tx.batch_execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='30s'; SET LOCAL search_path TO pg_catalog").map_err(|e|e.to_string())?;
        let locked: Vec<String> = planned
            .inventory
            .iter()
            .filter(|o| matches!(o["kind"].as_str(), Some("r" | "v")))
            .map(|o| qualified(namespace(o), name(o)))
            .collect();
        tx.batch_execute(&format!(
            "LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE",
            locked.join(",")
        ))
        .map_err(|e| e.to_string())?;
        crate::import::safe_restore::reverify_candidate(&verified)?;
        tx.batch_execute(&format!(
            "LOCK TABLE {} IN ACCESS EXCLUSIVE MODE",
            locked.join(",")
        ))
        .map_err(|e| e.to_string())?;
        let schemas = [
            planned.original_schema.clone(),
            planned.candidate_schema.clone(),
        ];
        if objects(&mut tx, &schemas)? != planned.inventory
            || dependencies(&mut tx, &schemas)? != planned.dependencies
        {
            return Err("promotion inventory changed before cutover".into());
        }
        state["phase"] = json!("transactional_cutover");
        write_dump_import_report(&journal_dir, &state)?;
        tx.batch_execute(&format!(
            "CREATE SCHEMA {}; REVOKE ALL ON SCHEMA {} FROM PUBLIC",
            q(&planned.backup_schema),
            q(&planned.backup_schema)
        ))
        .map_err(|e| e.to_string())?;
        for table in tables(&planned.inventory, &planned.candidate_schema) {
            if planned.inventory.iter().any(|o| {
                namespace(o) == planned.original_schema
                    && name(o) == name(table)
                    && o["kind"] == "r"
            }) {
                tx.batch_execute(&format!(
                    "ALTER TABLE {} SET SCHEMA {}",
                    qualified(&planned.original_schema, name(table)),
                    q(&planned.backup_schema)
                ))
                .map_err(|e| e.to_string())?;
            }
        }
        for table in tables(&planned.inventory, &planned.candidate_schema) {
            tx.batch_execute(&format!(
                "ALTER TABLE {} SET SCHEMA {}",
                qualified(&planned.candidate_schema, name(table)),
                q(&planned.original_schema)
            ))
            .map_err(|e| e.to_string())?;
        }
        for key in &planned.incoming {
            let table = qualified(&planned.original_schema, &key.table);
            tx.batch_execute(&format!(
                "ALTER TABLE {table} DROP CONSTRAINT {}; ALTER TABLE {table} ADD CONSTRAINT {} {}",
                q(&key.name),
                q(&key.name),
                key.definition
            ))
            .map_err(|e| e.to_string())?;
            if let Some(comment) = &key.comment {
                tx.batch_execute(&format!(
                    "COMMENT ON CONSTRAINT {} ON {table} IS {}",
                    q(&key.name),
                    literal(comment)
                ))
                .map_err(|e| e.to_string())?;
            }
        }
        tx.batch_execute(&format!(
            "SET LOCAL search_path TO {},pg_catalog",
            q(&planned.original_schema)
        ))
        .map_err(|e| e.to_string())?;
        // Existing views retain OIDs, ownership, grants and comments. Candidate
        // definitions are deparsed by PostgreSQL and namespace-stripped lexically.
        let source_views: BTreeSet<_> = planned
            .inventory
            .iter()
            .filter(|o| namespace(o) == planned.candidate_schema && o["kind"] == "v")
            .map(name)
            .collect();
        let mut views: Vec<&Value> = planned
            .inventory
            .iter()
            .filter(|o| {
                o["kind"] == "v"
                    && (namespace(o) == planned.candidate_schema
                        || (namespace(o) == planned.original_schema
                            && !source_views.contains(name(o))))
            })
            .collect();
        while !views.is_empty() {
            let mut pending = Vec::new();
            let mut last_error = String::new();
            for view in &views {
                tx.batch_execute("SAVEPOINT tf_view_rebind")
                    .map_err(|e| e.to_string())?;
                let definition = sanitize_view_definition(
                    view["definition"]
                        .as_str()
                        .ok_or("view definition missing")?,
                    namespace(view),
                    "postgresql",
                );
                match tx.batch_execute(&format!(
                    "CREATE OR REPLACE VIEW {} AS {definition}",
                    qualified(&planned.original_schema, name(view))
                )) {
                    Ok(()) => {
                        tx.batch_execute("RELEASE SAVEPOINT tf_view_rebind")
                            .map_err(|e| e.to_string())?;
                    }
                    Err(error) => {
                        last_error = error.to_string();
                        tx.batch_execute("ROLLBACK TO SAVEPOINT tf_view_rebind; RELEASE SAVEPOINT tf_view_rebind").map_err(|e|e.to_string())?;
                        pending.push(*view);
                    }
                }
            }
            if pending.len() == views.len() {
                return Err(format!("view rebinding failed: {last_error}"));
            }
            views = pending;
        }
        Ok(())
    })();
    match outcome {
        Err(error) => {
            let rollback = tx.rollback();
            state["status"] = json!(if rollback.is_ok() {
                "failed_original_unchanged"
            } else {
                "cutover_unknown"
            });
            state["original_unchanged"] = json!(rollback.is_ok());
            state["message"] = json!(redact_endpoint_secret(&error, original));
        }
        Ok(()) => {
            state["phase"] = json!("commit_requested");
            state["status"] = json!("cutover_unknown");
            state["original_unchanged"] = json!(false);
            if let Err(error) = write_dump_import_report(&journal_dir, &state) {
                let _ = tx.rollback();
                return Err(error);
            }
            match tx.commit() {
                Ok(()) => {
                    state["success"] = json!(true);
                    state["status"] = json!("promoted");
                }
                Err(error) => {
                    state["message"] = json!(redact_endpoint_secret(&error.to_string(), original));
                    if let Ok(mut retry) = connect(original) {
                        if let Ok(actual) = objects(
                            &mut retry,
                            &[
                                planned.original_schema.clone(),
                                planned.candidate_schema.clone(),
                                planned.backup_schema.clone(),
                            ],
                        ) {
                            let moved = tables(&planned.inventory, &planned.candidate_schema)
                                .iter()
                                .all(|t| {
                                    actual.iter().any(|a| {
                                        namespace(a) == planned.original_schema
                                            && a["oid"] == t["oid"]
                                    })
                                });
                            let selected: BTreeSet<_> =
                                tables(&planned.inventory, &planned.candidate_schema)
                                    .iter()
                                    .map(|t| name(t).to_string())
                                    .collect();
                            let backed_up = tables(&planned.inventory, &planned.original_schema)
                                .iter()
                                .filter(|t| selected.contains(name(t)))
                                .all(|t| {
                                    actual.iter().any(|a| {
                                        namespace(a) == planned.backup_schema
                                            && a["oid"] == t["oid"]
                                    })
                                });
                            let untouched = planned
                                .inventory
                                .iter()
                                .filter(|t| t["kind"] == "r")
                                .all(|t| {
                                    actual.iter().any(|a| {
                                        namespace(a) == namespace(t) && a["oid"] == t["oid"]
                                    })
                                });
                            if moved && backed_up {
                                state["success"] = json!(true);
                                state["status"] = json!("promoted");
                            } else if untouched {
                                state["status"] = json!("failed_original_unchanged");
                                state["original_unchanged"] = json!(true);
                            }
                        }
                    }
                }
            }
        }
    }
    if state["status"] == "promoted" {
        // Retention evidence for TF-STATUS-119: lets a later cleanup prove the
        // backup tables were not modified after this cutover.
        match record_backup_fingerprint(original, planned) {
            Ok(fingerprint) => state["backup_fingerprint"] = fingerprint,
            Err(error) => state["backup_fingerprint_warning"] = json!(error),
        }
    }
    state["phase"] = json!(match state["status"].as_str() {
        Some("promoted") => "completed",
        Some("failed_original_unchanged") => "rolled_back",
        _ => "reconciliation_required",
    });
    if state["success"] == true {
        state["message"]=json!("Verified candidate promoted transactionally; original tables retained in the backup schema.");
    }
    state["interrupted_attempt_policy"]=json!("Never retry blindly after commit_requested. Reconcile recorded relation OIDs at original/candidate/backup schemas; backups are retained.");
    if let Err(error) = write_dump_import_report(&journal_dir, &state) {
        state["journal_error"] = json!(error);
    }
    Ok(state)
}

// ---------------------------------------------------------------------------
// Backup lifecycle (TF-STATUS-119): read-only inspection and explicit drop of the
// owned backup schema. Ownership is proven by the relation OIDs the promotion
// plan recorded for the original tables.
// ---------------------------------------------------------------------------

fn lifecycle_connect(endpoint: &Endpoint, plan: &PromotionPlan) -> Result<Client, String> {
    if plan.endpoint["engine"] != endpoint.engine
        || plan.endpoint["host"] != endpoint.host
        || plan.endpoint["port"] != endpoint.port
        || plan.endpoint["database"] != endpoint.database
    {
        return Err("backup lifecycle endpoint does not match the promotion journal".into());
    }
    let mut db = connect(endpoint)?;
    db.batch_execute("SET DateStyle='ISO, YMD'; SET TIME ZONE 'UTC'; SET extra_float_digits=3")
        .map_err(|e| e.to_string())?;
    Ok(db)
}

fn schema_exists(db: &mut Client, schema: &str) -> Result<bool, String> {
    db.query_one("SELECT count(*) FROM pg_namespace WHERE nspname=$1", &[&schema])
        .map(|row| row.get::<_, i64>(0) == 1)
        .map_err(|e| e.to_string())
}

/// Objects and counts of one schema: (exists, base tables, views).
pub(crate) fn namespace_counts(endpoint: &Endpoint, namespace: &str) -> Result<(bool, u64, u64), String> {
    let mut db = connect(endpoint)?;
    if !schema_exists(&mut db, namespace)? {
        return Ok((false, 0, 0));
    }
    let row = db.query_one(
        "SELECT count(*) FILTER (WHERE c.relkind IN ('r','p')), count(*) FILTER (WHERE c.relkind IN ('v','m')) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1",
        &[&namespace],
    ).map_err(|e| e.to_string())?;
    Ok((true, row.get::<_, i64>(0) as u64, row.get::<_, i64>(1) as u64))
}

/// Foreign keys, views/rules, column defaults and inheritance links outside
/// `namespace` that depend on relations inside it.
pub(crate) fn external_references(endpoint: &Endpoint, namespace: &str) -> Result<Vec<String>, String> {
    let mut db = connect(endpoint)?;
    let mut found = Vec::new();
    for row in db.query(
        "SELECT ns.nspname||'.'||child.relname, c.conname FROM pg_constraint c JOIN pg_class ref ON ref.oid=c.confrelid JOIN pg_namespace rn ON rn.oid=ref.relnamespace JOIN pg_class child ON child.oid=c.conrelid JOIN pg_namespace ns ON ns.oid=child.relnamespace WHERE c.contype='f' AND rn.nspname=$1 AND ns.nspname<>$1",
        &[&namespace],
    ).map_err(|e| e.to_string())? {
        found.push(format!("foreign_key:{}:{}", row.get::<_, String>(0), row.get::<_, String>(1)));
    }
    for row in db.query(
        "SELECT DISTINCT vn.nspname||'.'||v.relname FROM pg_depend d JOIN pg_rewrite r ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid JOIN pg_class v ON v.oid=r.ev_class JOIN pg_namespace vn ON vn.oid=v.relnamespace JOIN pg_class dep ON d.refclassid='pg_class'::regclass AND d.refobjid=dep.oid JOIN pg_namespace dn ON dn.oid=dep.relnamespace WHERE dn.nspname=$1 AND vn.nspname<>$1",
        &[&namespace],
    ).map_err(|e| e.to_string())? {
        found.push(format!("view:{}", row.get::<_, String>(0)));
    }
    for row in db.query(
        "SELECT DISTINCT tn.nspname||'.'||t.relname FROM pg_depend d JOIN pg_attrdef ad ON d.classid='pg_attrdef'::regclass AND d.objid=ad.oid JOIN pg_class t ON t.oid=ad.adrelid JOIN pg_namespace tn ON tn.oid=t.relnamespace JOIN pg_class dep ON d.refclassid='pg_class'::regclass AND d.refobjid=dep.oid JOIN pg_namespace dn ON dn.oid=dep.relnamespace WHERE dn.nspname=$1 AND tn.nspname<>$1",
        &[&namespace],
    ).map_err(|e| e.to_string())? {
        found.push(format!("column_default:{}", row.get::<_, String>(0)));
    }
    for row in db.query(
        "SELECT DISTINCT cn.nspname||'.'||ch.relname FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent JOIN pg_namespace pn ON pn.oid=p.relnamespace JOIN pg_class ch ON ch.oid=i.inhrelid JOIN pg_namespace cn ON cn.oid=ch.relnamespace WHERE pn.nspname=$1 AND cn.nspname<>$1",
        &[&namespace],
    ).map_err(|e| e.to_string())? {
        found.push(format!("inheritance:{}", row.get::<_, String>(0)));
    }
    Ok(found)
}

/// Order-independent content fingerprint of one table: row count and a sum of
/// per-row hashes. Used to prove that a retained backup was not modified.
pub(crate) fn table_fingerprint(db: &mut Client, schema: &str, table: &str) -> Result<Value, String> {
    let row = db.query_one(
        &format!(
            "SELECT count(*)::text, COALESCE(sum(('x'||substr(md5(t::text),1,16))::bit(64)::bigint::numeric),0)::text FROM {} t",
            qualified(schema, table)
        ),
        &[],
    ).map_err(|e| e.to_string())?;
    Ok(json!({"rows": row.get::<_, String>(0), "hash_sum": row.get::<_, String>(1)}))
}

/// Fingerprints of every table that was moved into the backup schema; recorded in
/// the journal right after a successful commit.
fn record_backup_fingerprint(original: &Endpoint, planned: &PromotionPlan) -> Result<Value, String> {
    let mut db = lifecycle_connect(original, planned)?;
    let mut out = serde_json::Map::new();
    for object in moved_tables(planned) {
        out.insert(name(object).to_string(), table_fingerprint(&mut db, &planned.backup_schema, name(object))?);
    }
    let actual = objects(&mut db, std::slice::from_ref(&planned.backup_schema))?;
    Ok(json!({"tables": out, "structure": structure_digest(&actual, &planned.backup_schema)?}))
}

fn moved_tables(plan: &PromotionPlan) -> Vec<&Value> {
    let selected: BTreeSet<&str> = tables(&plan.inventory, &plan.candidate_schema).iter().map(|t| name(t)).collect();
    tables(&plan.inventory, &plan.original_schema).into_iter().filter(|t| selected.contains(name(t))).collect()
}

/// Digest of every object's catalog definition inside one schema.
fn structure_digest(actual: &[Value], schema: &str) -> Result<String, String> {
    let objects: Vec<&Value> = actual.iter().filter(|a| namespace(a) == schema).collect();
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(&objects).map_err(|e| e.to_string())?)))
}

/// Inspect the promotion backup schema. `deep` reads every row (fingerprints,
/// exact counts, external references); otherwise only catalog identities are used.
/// `journal_fingerprint` is the value recorded when the journal was written.
pub(crate) fn inspect_backup(
    endpoint: &Endpoint,
    plan: &PromotionPlan,
    deep: bool,
    journal_fingerprint: Option<&Value>,
) -> Result<Value, String> {
    let mut db = lifecycle_connect(endpoint, plan)?;
    let backup = plan.backup_schema.as_str();
    let mut blockers: Vec<String> = Vec::new();
    let exists = schema_exists(&mut db, backup)?;
    let mut result = json!({"namespace": backup, "exists": exists, "engine": "postgresql",
        "ownership": "missing", "contents_exact": false, "verdict": "undeterminable",
        "tables": [], "rows_estimated": !deep, "external_references": [], "unchanged": null});
    let actual = objects(&mut db, &[plan.original_schema.clone(), plan.candidate_schema.clone(), backup.to_string()])?;
    let moved = moved_tables(plan);
    let candidate_tables = tables(&plan.inventory, &plan.candidate_schema);
    let in_schema = |schema: &str, oid: &Value| actual.iter().any(|a| namespace(a) == schema && a["oid"] == *oid);
    let promoted = !moved.is_empty() || !candidate_tables.is_empty();
    let all_moved_in_backup = moved.iter().all(|t| in_schema(backup, &t["oid"]));
    let all_candidates_active = candidate_tables.iter().all(|t| in_schema(&plan.original_schema, &t["oid"]));
    let untouched = plan.inventory.iter().filter(|o| o["kind"] == "r").all(|o| in_schema(namespace(o), &o["oid"]));
    result["verdict"] = json!(if promoted && all_moved_in_backup && all_candidates_active {
        "promoted"
    } else if untouched {
        "not_promoted"
    } else {
        "undeterminable"
    });
    if !exists {
        blockers.push("backup schema does not exist".into());
        result["blockers"] = json!(blockers);
        return Ok(result);
    }
    // Everything in the backup schema must be an object the journal recorded.
    let allowed: BTreeSet<String> = plan.inventory.iter()
        .filter(|o| namespace(o) == plan.original_schema)
        .map(|o| o["oid"].to_string())
        .collect();
    let present: Vec<&Value> = actual.iter().filter(|a| namespace(a) == backup).collect();
    let foreign: Vec<String> = present.iter().filter(|a| !allowed.contains(&a["oid"].to_string())).map(|a| name(a).to_string()).collect();
    let missing: Vec<String> = moved.iter().filter(|t| !in_schema(backup, &t["oid"])).map(|t| name(t).to_string()).collect();
    let exact = foreign.is_empty() && missing.is_empty() && !moved.is_empty();
    result["contents_exact"] = json!(exact);
    result["ownership"] = json!(if exact { "proven" } else { "unproven" });
    if !foreign.is_empty() {
        blockers.push(format!("backup schema contains objects the journal does not record: {}", foreign.join(", ")));
    }
    if !missing.is_empty() {
        blockers.push(format!("recorded backup tables are missing: {}", missing.join(", ")));
    }
    let mut table_rows = Vec::new();
    let mut unchanged = exact;
    let mut estimates: Vec<(String, i64)> = Vec::new();
    if !deep {
        for row in db.query("SELECT c.relname, c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relkind='r' ORDER BY c.relname", &[&backup]).map_err(|e| e.to_string())? {
            estimates.push((row.get(0), row.get(1)));
        }
    }
    table_rows.extend(estimates.into_iter().map(|(n, r)| json!({"name": n, "rows": r.max(0)})));
    if deep && exact {
        // Data: fingerprints must match those recorded right after the commit.
        let structure_now = structure_digest(&actual, backup)?;
        match journal_fingerprint.and_then(|f| f["tables"].as_object()) {
            None => {
                unchanged = false;
                blockers.push("the journal has no content fingerprint (written by an older version); an unmodified backup cannot be proven".into());
            }
            Some(expected) => {
                if journal_fingerprint.and_then(|f| f["structure"].as_str()) != Some(structure_now.as_str()) {
                    unchanged = false;
                    blockers.push("the definition of the backup objects changed after promotion".into());
                }
                for table in &moved {
                    let now = table_fingerprint(&mut db, backup, name(table))?;
                    if expected.get(name(table)) != Some(&now) {
                        unchanged = false;
                        blockers.push(format!("table {} content changed after promotion", name(table)));
                    }
                    let rows = now["rows"].as_str().and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
                    table_rows.push(json!({"name": name(table), "rows": rows}));
                }
            }
        }
        result["unchanged"] = json!(unchanged);
        drop(db);
        let references = external_references(endpoint, backup)?;
        if !references.is_empty() {
            blockers.push(format!("objects outside the backup still reference it: {}", references.join(", ")));
        }
        result["external_references"] = json!(references);
    }
    result["tables"] = json!(table_rows);
    result["blockers"] = json!(blockers);
    Ok(result)
}

/// Drop one owned schema. Callers must have verified ownership, contents and
/// references immediately before; CASCADE only removes what was just verified.
pub(crate) fn drop_namespace(endpoint: &Endpoint, namespace: &str) -> Result<(), String> {
    let mut db = connect(endpoint)?;
    db.batch_execute(&format!("SET lock_timeout='2s'; DROP SCHEMA {} CASCADE", q(namespace))).map_err(|e| e.to_string())
}

/// Backup schemas following the promotion naming, whether or not a journal covers them.
pub(crate) fn backup_named_namespaces(endpoint: &Endpoint) -> Result<Vec<String>, String> {
    let mut db = connect(endpoint)?;
    Ok(db.query("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tf\\_backup\\_%' ORDER BY nspname", &[]).map_err(|e| e.to_string())?.into_iter().map(|row| row.get(0)).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn endpoint() -> Option<Endpoint> {
        Some(Endpoint {
            engine: "postgresql".into(),
            host: std::env::var("TF_POSTGRES_HOST").ok()?,
            port: 5432,
            user: "postgres".into(),
            password: "tf_local_test".into(),
            database: "tf_test".into(),
            schema: None,
            tls: Default::default(),
        })
    }
    fn run(command: &str, payload: Value) -> Value {
        let events = handle_request(Request {
            command: command.into(),
            request_id: None,
            payload,
        });
        assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
        let result = events.into_iter().find(|e| e["event"] == "result").unwrap();
        assert_eq!(result["success"], true, "{result:#?}");
        result
    }
    fn fixture(base: &Endpoint) -> (Endpoint, Endpoint, Value, std::path::PathBuf) {
        let name = format!(
            "tf_pg_promote_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let mut original = base.clone();
        original.schema = Some(name.clone());
        let mut admin = LiveAdapter::connect(base).unwrap();
        admin.execute_sql(&format!("CREATE SCHEMA {name}")).unwrap();
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("CREATE TABLE items(id INT PRIMARY KEY, value TEXT)")
            .unwrap();
        db.execute_sql("INSERT INTO items VALUES(1,'restored')")
            .unwrap();
        db.execute_sql("CREATE VIEW source_view AS SELECT id,value FROM items")
            .unwrap();
        let dir = std::env::temp_dir().join(&name);
        run(
            "dump.run",
            json!({"source":original,"output_dir":dir,"data_format":"jsonl","compression":"none","threads":1}),
        );
        db.execute_sql("UPDATE items SET value='live'").unwrap();
        let prepared = run(
            "dump.import",
            json!({"target":original,"input_dir":dir,"mode":"safe"}),
        );
        let mut candidate = original.clone();
        candidate.schema = Some(
            prepared["candidate_target"]["schema"]
                .as_str()
                .unwrap()
                .into(),
        );
        (original, candidate, prepared, dir)
    }
    fn cleanup(
        base: &Endpoint,
        original: &Endpoint,
        candidate: &Endpoint,
        backup: Option<&str>,
        dir: &Path,
    ) {
        let mut db = LiveAdapter::connect(base).unwrap();
        for schema in [
            original.schema.as_deref(),
            candidate.schema.as_deref(),
            backup,
        ]
        .into_iter()
        .flatten()
        {
            db.execute_sql(&format!(
                "DROP SCHEMA IF EXISTS {} CASCADE",
                quote_ident("postgresql", schema)
            ))
            .unwrap();
        }
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn postgres_promotion_preserves_incoming_fk_views_and_backups_live() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("CREATE TABLE extra(id INT PRIMARY KEY, item_id INT REFERENCES items(id))")
            .unwrap();
        db.execute_sql("INSERT INTO extra VALUES(9,1)").unwrap();
        db.execute_sql("CREATE VIEW target_only_view AS SELECT value FROM items")
            .unwrap();
        db.execute_sql("COMMENT ON VIEW target_only_view IS 'retained view metadata'")
            .unwrap();
        let mut application = connect(&original).unwrap();
        let cached = application
            .prepare(&format!(
                "SELECT value FROM {}",
                qualified(original.schema.as_deref().unwrap(), "items")
            ))
            .unwrap();
        assert_eq!(
            application
                .query_one(&cached, &[])
                .unwrap()
                .get::<_, String>(0),
            "live"
        );
        let planned = plan(
            &original,
            &candidate,
            prepared["restore_id"].as_str().unwrap(),
        )
        .unwrap();
        let result = promote(&original, &candidate, &planned, &planned.plan_digest, &dir).unwrap();
        assert_eq!(result["status"], "promoted", "{result:#?}");
        let values = run(
            "query.execute",
            json!({"connection":original,"sql":"SELECT value FROM target_only_view"}),
        );
        assert_eq!(values["rows"], json!([{"value":"restored"}]));
        assert_eq!(
            application
                .query_one(&cached, &[])
                .unwrap()
                .get::<_, String>(0),
            "restored",
            "cached application SELECT must replan to promoted table"
        );
        let comment: Option<String> = application
            .query_one(
                "SELECT obj_description(to_regclass($1),'pg_class')",
                &[&qualified(
                    original.schema.as_deref().unwrap(),
                    "target_only_view",
                )],
            )
            .unwrap()
            .get(0);
        assert_eq!(comment.as_deref(), Some("retained view metadata"));
        assert_eq!(db.row_count("extra").unwrap(), 1);
        assert!(db.execute_sql("INSERT INTO extra VALUES(10,999)").is_err());
        let mut backup = original.clone();
        backup.schema = Some(planned.backup_schema.clone());
        let values = run(
            "query.execute",
            json!({"connection":backup,"sql":"SELECT value FROM items"}),
        );
        assert_eq!(values["rows"], json!([{"value":"live"}]));
        cleanup(
            &base,
            &original,
            &candidate,
            Some(&planned.backup_schema),
            &dir,
        );
    }

    #[test]
    fn postgres_promotion_fk_mismatch_rolls_back_every_table_move_live() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("INSERT INTO items VALUES(2,'new-live')")
            .unwrap();
        db.execute_sql("CREATE TABLE extra(item_id INT REFERENCES items(id))")
            .unwrap();
        db.execute_sql("INSERT INTO extra VALUES(2)").unwrap();
        let planned = plan(
            &original,
            &candidate,
            prepared["restore_id"].as_str().unwrap(),
        )
        .unwrap();
        let result = promote(&original, &candidate, &planned, &planned.plan_digest, &dir).unwrap();
        assert_eq!(result["status"], "failed_original_unchanged", "{result:#?}");
        assert_eq!(db.row_count("items").unwrap(), 2);
        assert_eq!(db.row_count("source_view").unwrap(), 2);
        assert_eq!(
            LiveAdapter::connect(&candidate)
                .unwrap()
                .row_count("items")
                .unwrap(),
            1
        );
        cleanup(
            &base,
            &original,
            &candidate,
            Some(&planned.backup_schema),
            &dir,
        );
    }

    #[test]
    fn postgres_promotion_rejects_grants_and_changed_candidate_live() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let mut old = LiveAdapter::connect(&original).unwrap();
        old.execute_sql("GRANT SELECT ON items TO PUBLIC").unwrap();
        assert!(plan(
            &original,
            &candidate,
            prepared["restore_id"].as_str().unwrap()
        )
        .is_err());
        // REVOKE leaves an explicit ACL; use a fresh preparation for content drift.
        cleanup(&base, &original, &candidate, None, &dir);
        let (original, candidate, prepared, dir) = fixture(&base);
        let planned = plan(
            &original,
            &candidate,
            prepared["restore_id"].as_str().unwrap(),
        )
        .unwrap();
        LiveAdapter::connect(&candidate)
            .unwrap()
            .execute_sql("UPDATE items SET value='tampered'")
            .unwrap();
        let result = promote(&original, &candidate, &planned, &planned.plan_digest, &dir).unwrap();
        assert_eq!(result["status"], "failed_original_unchanged", "{result:#?}");
        let values = run(
            "query.execute",
            json!({"connection":original,"sql":"SELECT value FROM items"}),
        );
        assert_eq!(values["rows"], json!([{"value":"live"}]));
        cleanup(
            &base,
            &original,
            &candidate,
            Some(&planned.backup_schema),
            &dir,
        );
    }

    #[test]
    fn postgres_promotion_blocks_inheritance_and_shared_sequence_dependencies_live() {
        let Some(base) = endpoint() else { return };
        for ddl in [
            "CREATE TABLE inherited() INHERITS(items)",
            "CREATE SEQUENCE manual_counter",
            "CREATE TABLE typed_row(payload items)",
        ] {
            let (original, candidate, prepared, dir) = fixture(&base);
            LiveAdapter::connect(&original)
                .unwrap()
                .execute_sql(ddl)
                .unwrap();
            let result = plan(
                &original,
                &candidate,
                prepared["restore_id"].as_str().unwrap(),
            );
            cleanup(&base, &original, &candidate, None, &dir);
            assert!(result.is_err(), "unsupported dependency accepted: {ddl}");
        }
    }

    #[test]
    fn postgres_promotion_terminated_transaction_leaves_originals_live() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let planned = plan(
            &original,
            &candidate,
            prepared["restore_id"].as_str().unwrap(),
        )
        .unwrap();
        let mut reader = connect(&original).unwrap();
        let mut held = reader.transaction().unwrap();
        held.batch_execute(&format!(
            "LOCK TABLE {} IN ACCESS SHARE MODE",
            qualified(&planned.original_schema, "items")
        ))
        .unwrap();
        let (a, b, p, path) = (
            original.clone(),
            candidate.clone(),
            planned.clone(),
            dir.clone(),
        );
        let worker = std::thread::spawn(move || promote(&a, &b, &p, &p.plan_digest, &path));
        let mut admin = connect(&base).unwrap();
        let application = format!("tf_promote_{}", planned.restore_id);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
        let mut killed = false;
        while std::time::Instant::now() < deadline {
            if let Some(row)=admin.query_opt("SELECT pid FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock'",&[&application]).unwrap() {
                let pid:i32=row.get(0);let terminated:bool=admin.query_one("SELECT pg_terminate_backend($1)",&[&pid]).unwrap().get(0);
                assert!(terminated);killed=true;break;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        held.rollback().unwrap();
        let result = worker.join().unwrap().unwrap();
        assert!(killed, "promotion never reached transactional lock wait");
        assert_eq!(result["success"], false, "{result:#?}");
        assert!(matches!(
            result["status"].as_str(),
            Some("cutover_unknown" | "failed_original_unchanged")
        ));
        let values = run(
            "query.execute",
            json!({"connection":original,"sql":"SELECT value FROM source_view"}),
        );
        assert_eq!(values["rows"], json!([{"value":"live"}]));
        assert_eq!(
            LiveAdapter::connect(&candidate)
                .unwrap()
                .row_count("items")
                .unwrap(),
            1
        );
        cleanup(
            &base,
            &original,
            &candidate,
            Some(&planned.backup_schema),
            &dir,
        );
    }

    #[test]
    fn postgres_promotion_detects_dependency_created_while_waiting_for_locks_live() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let planned = plan(
            &original,
            &candidate,
            prepared["restore_id"].as_str().unwrap(),
        )
        .unwrap();
        let mut reader = connect(&original).unwrap();
        let mut held = reader.transaction().unwrap();
        held.batch_execute(&format!(
            "LOCK TABLE {} IN EXCLUSIVE MODE",
            qualified(&planned.original_schema, "items")
        ))
        .unwrap();
        let (a, b, p, path) = (
            original.clone(),
            candidate.clone(),
            planned.clone(),
            dir.clone(),
        );
        let worker = std::thread::spawn(move || promote(&a, &b, &p, &p.plan_digest, &path));
        let mut admin = connect(&base).unwrap();
        let application = format!("tf_promote_{}", planned.restore_id);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
        let mut waiting = false;
        while std::time::Instant::now() < deadline {
            if admin.query_opt("SELECT pid FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock'",&[&application]).unwrap().is_some() {waiting=true;break;}
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let external = format!("tf_external_{}", planned.restore_id);
        admin
            .batch_execute(&format!(
                "CREATE SCHEMA {}; CREATE VIEW {} AS SELECT value FROM {}",
                q(&external),
                qualified(&external, "late_view"),
                qualified(&planned.original_schema, "items")
            ))
            .unwrap();
        held.rollback().unwrap();
        let result = worker.join().unwrap().unwrap();
        admin
            .batch_execute(&format!("DROP SCHEMA {} CASCADE", q(&external)))
            .unwrap();
        cleanup(
            &base,
            &original,
            &candidate,
            Some(&planned.backup_schema),
            &dir,
        );
        assert!(waiting);
        assert_eq!(
            result["status"], "failed_original_unchanged",
            "late external dependency was silently rebound to backup: {result:#?}"
        );
    }

    #[test]
    fn postgres_promotion_known_lock_timeout_allows_fresh_plan_without_reimport_live() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let restore_id = prepared["restore_id"].as_str().unwrap();
        let first = plan(&original, &candidate, restore_id).unwrap();
        let mut reader = connect(&original).unwrap();
        let mut held = reader.transaction().unwrap();
        held.batch_execute(&format!(
            "LOCK TABLE {} IN ACCESS SHARE MODE",
            qualified(&first.original_schema, "items")
        ))
        .unwrap();
        let failed = promote(&original, &candidate, &first, &first.plan_digest, &dir).unwrap();
        held.rollback().unwrap();
        assert_eq!(failed["status"], "failed_original_unchanged", "{failed:#?}");
        let journal = std::path::PathBuf::from(failed["report_path"].as_str().unwrap());
        let old_bytes = std::fs::read(&journal).unwrap();
        assert!(
            promote(&original, &candidate, &first, &first.plan_digest, &dir).is_err(),
            "consumed plan replayed"
        );
        let fresh = plan(&original, &candidate, restore_id).unwrap();
        assert_ne!(
            fresh.plan_digest, first.plan_digest,
            "fresh retry needs independent consent digest"
        );
        let completed = promote(&original, &candidate, &fresh, &fresh.plan_digest, &dir).unwrap();
        assert_eq!(completed["status"], "promoted", "{completed:#?}");
        assert_eq!(
            std::fs::read(journal).unwrap(),
            old_bytes,
            "retry overwrote prior failure journal"
        );
        cleanup(
            &base,
            &original,
            &candidate,
            Some(&fresh.backup_schema),
            &dir,
        );
    }
}
