use migration_core::{
    handle_request, handle_request_streaming, Endpoint, LiveAdapter, MigrationAdapter, Request,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn run(command: &str, payload: Value) -> Vec<Value> {
    handle_request(Request {
        command: command.into(),
        request_id: None,
        payload,
    })
}
fn endpoint(engine: &str) -> Option<Endpoint> {
    let prefix = if engine == "mysql" {
        "TF_MYSQL"
    } else {
        "TF_POSTGRES"
    };
    Some(Endpoint {
        engine: engine.into(),
        host: std::env::var(format!("{prefix}_HOST")).ok()?,
        port: if engine == "mysql" { 3306 } else { 5432 },
        user: if engine == "mysql" {
            "root"
        } else {
            "postgres"
        }
        .into(),
        password: "tf_local_test".into(),
        database: "tf_test".into(),
        schema: None,
        tls: Default::default(),
    })
}
fn unique() -> String {
    format!(
        "tf_safe_{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}

#[test]
fn safe_restore_stages_verified_candidate_and_never_changes_original_on_failure() {
    for engine in ["mysql", "postgresql"] {
        let Some(base) = endpoint(engine) else {
            continue;
        };
        let mut admin = LiveAdapter::connect(&base).unwrap();
        for defect in ["none", "ddl", "data", "view", "orphan", "target_only"] {
            let name = unique();
            admin
                .execute_sql(&format!(
                    "CREATE {} {name}",
                    if engine == "mysql" {
                        "DATABASE"
                    } else {
                        "SCHEMA"
                    }
                ))
                .unwrap();
            let mut original = base.clone();
            if engine == "mysql" {
                original.database = name.clone();
            } else {
                original.schema = Some(name.clone());
            }
            let mut old = LiveAdapter::connect(&original).unwrap();
            old.execute_sql("CREATE TABLE parents(id INT PRIMARY KEY, value VARCHAR(40))")
                .unwrap();
            old.execute_sql("CREATE TABLE children(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_parent FOREIGN KEY(parent_id) REFERENCES parents(id))").unwrap();
            old.execute_sql("INSERT INTO parents VALUES(1,'backup')")
                .unwrap();
            old.execute_sql("INSERT INTO children VALUES(1,1)").unwrap();
            old.execute_sql("CREATE VIEW parent_view AS SELECT id,value FROM parents")
                .unwrap();
            let output = std::env::temp_dir().join(unique());
            let exported = run(
                "dump.run",
                json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}),
            );
            assert!(
                !exported.iter().any(|e| e["event"] == "error"),
                "{exported:?}"
            );
            let manifest_path = output.join("_tunnelforge_dump.json");
            let mut manifest: Value =
                serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
            match defect {
                "ddl" => {
                    let table = manifest["schema"]["tables"]
                        .as_array_mut()
                        .unwrap()
                        .iter_mut()
                        .find(|t| t["name"] == "parents")
                        .unwrap();
                    table["columns"][1]["type"] = json!(if engine == "mysql" {
                        "DECIMAL(100,2)"
                    } else {
                        "NUMERIC(1001,2)"
                    });
                }
                "view" => {
                    manifest["views"][0]["definition"] =
                        json!("CREATE VIEW parent_view AS SELECT missing_column FROM parents")
                }
                "data" | "orphan" => {
                    let table_name = if defect == "data" {
                        "parents"
                    } else {
                        "children"
                    };
                    let table = manifest["tables"]
                        .as_array_mut()
                        .unwrap()
                        .iter_mut()
                        .find(|t| t["name"] == table_name)
                        .unwrap();
                    let bytes = if defect == "data" {
                        b"{\"id\":\"bad\",\"value\":\"backup\"}\n".as_slice()
                    } else {
                        b"{\"id\":1,\"parent_id\":999}\n".as_slice()
                    };
                    std::fs::write(
                        output
                            .join(table["path"].as_str().unwrap())
                            .join("chunk_000001.jsonl"),
                        bytes,
                    )
                    .unwrap();
                    table["chunk_sha256"]["chunk_000001.jsonl"] =
                        json!(format!("{:x}", Sha256::digest(bytes)));
                }
                "target_only" => old
                    .execute_sql("CREATE TABLE outside_dump(id INT PRIMARY KEY)")
                    .unwrap(),
                _ => {}
            }
            std::fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
            old.execute_sql("UPDATE parents SET value='original-live'")
                .unwrap();
            let mut events = Vec::new();
            let mut reads_during_stage = 0;
            handle_request_streaming(
                Request {
                    command: "dump.import".into(),
                    request_id: None,
                    payload: if defect == "none" { json!({"connection":original,"target":original,"input_dir":output,"threads":1}) } else if defect == "target_only" { json!({"connection":original,"target":original,"input_dir":output,"import_mode":"safe","threads":1}) } else { json!({"connection":original,"target":original,"input_dir":output,"mode":"safe","threads":1}) },
                },
                |event| {
                    if event["event"] == "safe_restore_target" || event["event"] == "table_progress"
                    {
                        assert_eq!(old.row_count("parents").unwrap(), 1);
                        assert_eq!(old.row_count("children").unwrap(), 1);
                        assert_eq!(old.row_count("parent_view").unwrap(), 1);
                        reads_during_stage += 1;
                    }
                    events.push(event);
                },
            );
            let result = events
                .iter()
                .find(|e| e["event"] == "result")
                .unwrap_or_else(|| panic!("{engine}/{defect}: {events:?}"));
            assert_eq!(result["original_unchanged"], true);
            assert_eq!(result["cutover_pending"], true);
            assert!(reads_during_stage > 0);
            let mut candidate = original.clone();
            candidate.database = result["candidate_target"]["database"]
                .as_str()
                .unwrap()
                .into();
            candidate.schema = result["candidate_target"]["schema"]
                .as_str()
                .map(str::to_string);
            assert!(candidate.database != original.database || candidate.schema != original.schema);
            let old_rows = run(
                "query.execute",
                json!({"connection":original,"sql":"SELECT value FROM parents WHERE id=1"}),
            );
            assert!(old_rows
                .iter()
                .any(|e| e["rows"] == json!([{"value":"original-live"}])));
            assert!(
                old.execute_sql("INSERT INTO children VALUES(2,999)")
                    .is_err(),
                "original FK changed"
            );
            if defect == "none" || defect == "target_only" {
                assert_eq!(result["success"], true, "{engine}/{defect}: {result:?}");
                assert_eq!(result["verified"], true);
                // An unlinked target-only table is left alone by the swap and blocks nothing.
                assert_eq!(result["status"], "ready_for_switch", "{engine}/{defect}: {result:?}");
                let mut fresh = LiveAdapter::connect(&candidate).unwrap();
                assert_eq!(fresh.row_count("parents").unwrap(), 1);
                assert_eq!(fresh.row_count("children").unwrap(), 1);
                assert_eq!(fresh.row_count("parent_view").unwrap(), 1);
                let candidate_view = run(
                    "query.execute",
                    json!({"connection":candidate,"sql":"SELECT value FROM parent_view WHERE id=1"}),
                );
                assert!(
                    candidate_view
                        .iter()
                        .any(|e| e["rows"] == json!([{"value":"backup"}])),
                    "{engine}: candidate view still reads the original namespace"
                );
            } else {
                assert_eq!(result["success"], false, "{engine}/{defect}: {result:?}");
                assert_eq!(result["status"], "failed_original_untouched");
                assert_eq!(result["candidate_retained"], true);
                if defect == "orphan" && engine == "mysql" {
                    let error = result["error"].as_str().unwrap();
                    assert!(error.starts_with("candidate_fk_orphans:"), "{error}");
                    let fields = error.split_once("fields=").unwrap().1;
                    let fields: Value = serde_json::from_str(fields).unwrap();
                    assert_eq!(fields["foreign_key"], "fk_parent");
                    assert_eq!(fields["child_table"], "children");
                    assert_eq!(fields["row_count"], 1);
                    assert_eq!(fields["snapshot_policy"], manifest["snapshot_policy"]);
                }
            }
            let report: Value = serde_json::from_slice(
                &std::fs::read(result["report_path"].as_str().unwrap()).unwrap(),
            )
            .unwrap();
            assert_eq!(report["original_unchanged"], true);
            assert_eq!(report["status"], result["status"]);
            assert!(!report.to_string().contains("tf_local_test"));
            if engine == "mysql" {
                admin
                    .execute_sql(&format!("DROP DATABASE {}", candidate.database))
                    .unwrap();
                admin.execute_sql(&format!("DROP DATABASE {name}")).unwrap();
            } else {
                admin
                    .execute_sql(&format!(
                        "DROP SCHEMA {} CASCADE",
                        candidate.schema.unwrap()
                    ))
                    .unwrap();
                admin
                    .execute_sql(&format!("DROP SCHEMA {name} CASCADE"))
                    .unwrap();
            }
            std::fs::remove_dir_all(output).unwrap();
        }
    }
}

#[test]
fn safe_restore_uses_requested_name_when_destination_namespace_is_absent() {
    for engine in ["mysql", "postgresql"] {
        let Some(base) = endpoint(engine) else {
            continue;
        };
        let mut admin = LiveAdapter::connect(&base).unwrap();
        let source_name = unique();
        let requested_name = unique();
        admin
            .execute_sql(&format!(
                "CREATE {} {source_name}",
                if engine == "mysql" {
                    "DATABASE"
                } else {
                    "SCHEMA"
                }
            ))
            .unwrap();
        let mut source = base.clone();
        let mut target = base.clone();
        if engine == "mysql" {
            source.database = source_name.clone();
            target.database = requested_name.clone();
        } else {
            source.schema = Some(source_name.clone());
            target.schema = Some(requested_name.clone());
        }
        let mut source_db = LiveAdapter::connect(&source).unwrap();
        source_db
            .execute_sql("CREATE TABLE items(id INT PRIMARY KEY)")
            .unwrap();
        source_db
            .execute_sql("INSERT INTO items VALUES(7)")
            .unwrap();
        let output = std::env::temp_dir().join(unique());
        assert!(!run("dump.run",json!({"source":source,"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"})).iter().any(|e|e["event"]=="error"));
        let events = run(
            "dump.import",
            json!({"target":target,"input_dir":output,"mode":"safe"}),
        );
        let result = events.iter().find(|e| e["event"] == "result").unwrap();
        if result["candidate_created"] == true {
            let mut candidate = LiveAdapter::connect(&target).unwrap();
            assert_eq!(candidate.row_count("items").unwrap(), 1);
            admin
                .execute_sql(&format!(
                    "DROP {} {}{}",
                    if engine == "mysql" {
                        "DATABASE"
                    } else {
                        "SCHEMA"
                    },
                    requested_name,
                    if engine == "mysql" { "" } else { " CASCADE" }
                ))
                .unwrap();
        }
        assert_eq!(source_db.row_count("items").unwrap(), 1);
        admin
            .execute_sql(&format!(
                "DROP {} {}{}",
                if engine == "mysql" {
                    "DATABASE"
                } else {
                    "SCHEMA"
                },
                source_name,
                if engine == "mysql" { "" } else { " CASCADE" }
            ))
            .unwrap();
        std::fs::remove_dir_all(output).unwrap();
        assert_eq!(result["success"], true, "{engine}: {result:?}");
        assert_eq!(result["status"], "completed_new_target");
        assert_eq!(result["cutover_pending"], false);
        assert_eq!(result["namespace_existed"], false);
        assert_eq!(
            result["candidate_target"][if engine == "mysql" {
                "database"
            } else {
                "schema"
            }],
            requested_name
        );
    }
}

#[test]
fn safe_restore_rejects_view_target_outside_candidate_before_creation() {
    for engine in ["mysql", "postgresql"] {
        let Some(base) = endpoint(engine) else { continue };
        let mut admin = LiveAdapter::connect(&base).unwrap();
        let name = unique();
        admin.execute_sql(&format!("CREATE {} {name}", if engine == "mysql" { "DATABASE" } else { "SCHEMA" })).unwrap();
        let mut original = base.clone();
        if engine == "mysql" { original.database = name.clone(); } else { original.schema = Some(name.clone()); }
        let mut old = LiveAdapter::connect(&original).unwrap();
        old.execute_sql("CREATE TABLE items(id INT PRIMARY KEY)").unwrap();
        old.execute_sql("INSERT INTO items VALUES(1)").unwrap();
        old.execute_sql("CREATE VIEW view_original AS SELECT id FROM items").unwrap();
        let output = std::env::temp_dir().join(unique());
        assert!(!run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"})).iter().any(|event|event["event"]=="error"));
        let manifest_path = output.join("_tunnelforge_dump.json");
        let mut manifest: Value = serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
        manifest["source_schema"] = json!("different_export_namespace");
        manifest["views"][0]["definition"] = json!(format!("CREATE OR REPLACE VIEW {name}.view_original AS SELECT id FROM {name}.items WHERE id=999"));
        std::fs::write(manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        let events = run("dump.import", json!({"target":original,"input_dir":output,"mode":"safe"}));
        let result = events.iter().find(|event|event["event"]=="result").unwrap();
        let old_view_rows = old.row_count("view_original").unwrap();
        if result["candidate_created"] == true {
            let candidate_name = result["candidate_target"][if engine=="mysql" {"database"} else {"schema"}].as_str().unwrap();
            assert!(candidate_name.starts_with("tf_restore_") && candidate_name != name);
            admin.execute_sql(&format!("DROP {} {}{}", if engine=="mysql" {"DATABASE"} else {"SCHEMA"}, candidate_name, if engine=="mysql" {""} else {" CASCADE"})).unwrap();
        }
        admin.execute_sql(&format!("DROP {} {}{}", if engine=="mysql" {"DATABASE"} else {"SCHEMA"}, name, if engine=="mysql" {""} else {" CASCADE"})).unwrap();
        std::fs::remove_dir_all(output).unwrap();
        assert_eq!(old_view_rows, 1, "{engine}: the candidate import rewrote an original view");
        assert_eq!(result["success"], false);
        assert_eq!(result["candidate_created"], false);
        assert!(result["message"].as_str().unwrap().contains("view_target_invalid"));
    }
}

#[test]
fn mysql_safe_restore_preserves_security_words_in_view_names_and_body_literals() {
    let Some(base) = endpoint("mysql") else { return };
    let mut admin = LiveAdapter::connect(&base).unwrap();
    let name = unique();
    admin.execute_sql(&format!("CREATE DATABASE {name}")).unwrap();
    let mut source = base.clone(); source.database = name.clone();
    let mut db = LiveAdapter::connect(&source).unwrap();
    db.execute_sql("CREATE TABLE items(id INT PRIMARY KEY)").unwrap();
    db.execute_sql("INSERT INTO items VALUES(1)").unwrap();
    db.execute_sql("CREATE VIEW `SQL SECURITY DEFINER` AS SELECT 'SQL SECURITY DEFINER' AS policy_text, 'definer=body_value' AS owner_text FROM items").unwrap();
    db.execute_sql("CREATE VIEW legacy_view AS SELECT 'SQL SECURITY DEFINER' AS policy_text, 'definer=body_value' AS owner_text FROM items").unwrap();
    let output = std::env::temp_dir().join(unique());
    assert!(!run("dump.run", json!({"source":source,"output_dir":output,"data_format":"jsonl","threads":1,"mysql_snapshot_mode":"single_connection"})).iter().any(|event|event["event"]=="error"));
    let path = output.join("_tunnelforge_dump.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let legacy = manifest["views"].as_array_mut().unwrap().iter_mut().find(|view|view["name"]=="legacy_view").unwrap();
    legacy["definition"] = json!("CREATE VIEW legacy_view AS SELECT 'SQL SECURITY DEFINER' AS policy_text, 'definer=body_value' AS owner_text FROM items");
    std::fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let events = run("dump.import", json!({"target":source,"input_dir":output,"mode":"safe"}));
    let result = events.iter().find(|event|event["event"]=="result").unwrap();
    assert_eq!(result["success"],true,"{result:?}");
    let mut candidate = source.clone();
    candidate.database = result["candidate_target"]["database"].as_str().unwrap().into();
    candidate.schema = Some(candidate.database.clone());
    assert!(candidate.database.starts_with("tf_restore_") && candidate.database != source.database);
    for view in ["SQL SECURITY DEFINER", "legacy_view"] {
        let rows=run("query.execute",json!({"connection":candidate,"sql":format!("SELECT policy_text,owner_text FROM `{view}`")}));
        assert!(rows.iter().any(|event|event["rows"]==json!([{"policy_text":"SQL SECURITY DEFINER","owner_text":"definer=body_value"}])));
    }
    admin.execute_sql(&format!("DROP DATABASE {}",candidate.database)).unwrap();
    admin.execute_sql(&format!("DROP DATABASE {name}")).unwrap();
    std::fs::remove_dir_all(output).unwrap();
}

fn namespace_kind(engine: &str) -> &'static str {
    if engine == "mysql" { "DATABASE" } else { "SCHEMA" }
}

fn drop_namespace(admin: &mut LiveAdapter, engine: &str, name: &str) {
    let cascade = if engine == "mysql" { "" } else { " CASCADE" };
    admin.execute_sql(&format!("DROP {} IF EXISTS {name}{cascade}", namespace_kind(engine))).unwrap();
}

/// A new namespace holding `parents` (one row), and its dump. Returns (admin, original, name, dump).
fn exported_namespace(engine: &str) -> Option<(LiveAdapter, Endpoint, String, std::path::PathBuf)> {
    let base = endpoint(engine)?;
    let mut admin = LiveAdapter::connect(&base).unwrap();
    let name = unique();
    admin.execute_sql(&format!("CREATE {} {name}", namespace_kind(engine))).unwrap();
    let mut original = base.clone();
    if engine == "mysql" {
        original.database = name.clone();
    } else {
        original.schema = Some(name.clone());
    }
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TABLE parents(id INT PRIMARY KEY, value VARCHAR(40))").unwrap();
    db.execute_sql("INSERT INTO parents VALUES(1,'dump')").unwrap();
    let output = std::env::temp_dir().join(unique());
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:?}");
    Some((admin, original, name, output))
}

fn safe_import(original: &Endpoint, output: &std::path::Path) -> Value {
    let events = run("dump.import", json!({"target":original,"input_dir":output,"mode":"safe","threads":1}));
    events.into_iter().find(|e| e["event"] == "result").expect("result event")
}

fn first_value(endpoint: &Endpoint, sql: &str) -> Value {
    let events = run("query.execute", json!({"connection":endpoint,"sql":sql}));
    let rows = events.iter().find_map(|e| e["rows"].as_array().cloned()).unwrap_or_else(|| panic!("{sql}: {events:?}"));
    rows[0].as_object().unwrap().values().next().unwrap().clone()
}

fn safe_import_with(original: &Endpoint, output: &std::path::Path, extra: Value) -> Value {
    let mut payload = json!({"target":original,"input_dir":output,"mode":"safe","threads":1});
    payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
    run("dump.import", payload).into_iter().find(|e| e["event"] == "result").expect("result event")
}

fn promote_plan(original: &Endpoint, restored: &Value) -> Value {
    run("dump.promote", json!({"endpoint":original,"action":"plan","report_path":restored["report_path"],"restore_id":restored["restore_id"]}))
        .into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap()
}

/// Whether `blockers` hold `code` for `table`.
fn stops(blockers: &Value, code: &str, table: &str) -> bool {
    blockers.as_array().into_iter().flatten().any(|item| item["code"] == code && item["table"] == table)
}

/// A run that stopped before making a copy: nothing was named or created.
fn assert_stopped_before_copy(result: &Value, case: &str) {
    assert_eq!(result["status"], "failed_original_untouched", "{case}: {result}");
    assert_eq!(result["candidate_created"], false, "{case}: {result}");
    assert_eq!(result["error_code"], "copy_refused", "{case}: {result}");
    assert_eq!(result["cutover_pending"], false, "{case}: {result}");
    // The run stops before it names its restore, so no copy can exist.
    assert!(result.get("restore_id").is_none(), "{case}: {result}");
}

#[test]
fn unlinked_target_only_reaches_ready_for_switch() {
    for engine in ["mysql", "postgresql"] {
        let Some((mut admin, original, name, output)) = exported_namespace(engine) else { continue };
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY, note VARCHAR(20))").unwrap();
        db.execute_sql("INSERT INTO notes VALUES(1,'kept')").unwrap();
        db.execute_sql("CREATE VIEW notes_view AS SELECT id, note FROM notes").unwrap();
        let result = safe_import(&original, &output);
        if let Some(candidate) = result["candidate_target"][if engine == "mysql" { "database" } else { "schema" }].as_str() {
            if candidate != name {
                drop_namespace(&mut admin, engine, candidate);
            }
        }
        drop_namespace(&mut admin, engine, &name);
        std::fs::remove_dir_all(output).unwrap();
        assert_eq!(result["success"], true, "{engine}: {result}");
        assert_eq!(result["status"], "ready_for_switch", "{engine}: {result}");
        assert_eq!(result["ready_for_switch"], true, "{engine}: {result}");
        assert_eq!(result["blockers"], json!([]), "{engine}: {result}");
        assert_eq!(result["target_only_tables"], json!(["notes"]), "{engine}: {result}");
        assert_eq!(result["target_only_views"], json!(["notes_view"]), "{engine}: {result}");
    }
}

#[test]
fn linked_target_only_table_stops_before_copy() {
    for engine in ["mysql", "postgresql"] {
        for link in ["child_fk", "view", "dump_child_fk"] {
            let Some((mut admin, original, name, output)) = exported_namespace(engine) else { continue };
            let mut db = LiveAdapter::connect(&original).unwrap();
            let outside = match link {
                // A target-only table referencing a dump table.
                "child_fk" => {
                    db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_notes FOREIGN KEY(parent_id) REFERENCES parents(id))").unwrap();
                    db.execute_sql("INSERT INTO notes VALUES(1,1)").unwrap();
                    "notes"
                }
                // A target-only view reading a dump table.
                "view" => {
                    db.execute_sql("CREATE VIEW parent_notes AS SELECT id FROM parents").unwrap();
                    "parent_notes"
                }
                // A dump table referencing a target table the dump does not have.
                _ => {
                    db.execute_sql("CREATE TABLE owners(id INT PRIMARY KEY)").unwrap();
                    db.execute_sql("ALTER TABLE parents ADD COLUMN owner_id INT").unwrap();
                    db.execute_sql("ALTER TABLE parents ADD CONSTRAINT fk_owner FOREIGN KEY(owner_id) REFERENCES owners(id)").unwrap();
                    "owners"
                }
            };
            let preview = run("dump.import_preview", json!({"target":original,"input_dir":output,"threads":1}));
            let preview = preview.into_iter().find(|e| e["event"] == "result").unwrap_or_else(|| panic!("{engine}/{link}: no preview result"));
            let result = safe_import(&original, &output);
            let rows = db.row_count("parents").unwrap();
            drop_namespace(&mut admin, engine, &name);
            std::fs::remove_dir_all(output).unwrap();
            assert!(stops(&preview["copy_blockers"], "linked_target_only", outside), "{engine}/{link}: preview {preview}");
            assert_eq!(result["success"], false, "{engine}/{link}: {result}");
            assert_stopped_before_copy(&result, &format!("{engine}/{link}"));
            assert!(stops(&result["copy_blockers"], "linked_target_only", outside), "{engine}/{link}: {result}");
            assert_eq!(rows, 1, "{engine}/{link}: the original changed");
        }
    }
}

#[test]
fn unlinked_target_only_table_survives_unrebuilt() {
    for engine in ["mysql", "postgresql"] {
        let Some((mut admin, original, name, output)) = exported_namespace(engine) else { continue };
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY, note VARCHAR(20))").unwrap();
        db.execute_sql("INSERT INTO notes VALUES(1,'kept'),(2,'also kept')").unwrap();
        db.execute_sql("UPDATE parents SET value='live'").unwrap();
        let identity = if engine == "mysql" {
            format!("SELECT CAST(TABLE_ID AS CHAR) AS id FROM information_schema.INNODB_TABLES WHERE NAME='{name}/notes'")
        } else {
            format!("SELECT '{name}.notes'::regclass::oid::text AS id")
        };
        let before = first_value(&original, &identity);
        let restored = safe_import(&original, &output);
        let promote = |extra: Value| {
            let mut payload = json!({"endpoint":original,"report_path":restored["report_path"],"restore_id":restored["restore_id"]});
            payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
            run("dump.promote", payload).into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap()
        };
        let planned = promote(json!({"action":"plan"}));
        let done = promote(json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
        let after = first_value(&original, &identity);
        let kept = db.row_count("notes").unwrap();
        let value = first_value(&original, "SELECT value FROM parents WHERE id=1");
        let mut leftovers = vec![format!("tf_restore_{}", restored["restore_id"].as_str().unwrap_or_default())];
        for key in ["backup_namespace", "clone_database"] {
            if let Some(namespace) = done[key].as_str() {
                leftovers.push(namespace.to_string());
            }
        }
        for namespace in leftovers.iter().chain([&name]) {
            assert!(namespace.starts_with("tf_"), "{namespace}");
            drop_namespace(&mut admin, engine, namespace);
        }
        std::fs::remove_dir_all(output).unwrap();
        assert_eq!(restored["status"], "ready_for_switch", "{engine}: {restored}");
        assert_eq!(planned["can_promote"], true, "{engine}: {planned}");
        assert_eq!(done["status"], "promoted", "{engine}: {done}");
        assert_eq!(value, "dump", "{engine}: the dump table was not swapped in");
        assert_eq!(before, after, "{engine}: the target-only table was rebuilt");
        assert_eq!(kept, 2, "{engine}: the target-only table lost rows");
    }
}

#[test]
fn target_objects_the_swap_refuses_stop_before_copy() {
    for engine in ["mysql", "postgresql"] {
        let mysql = engine == "mysql";
        let cases: &[&str] = if mysql { &["trigger", "routine", "event"] } else { &["trigger", "unlinked_trigger", "routine"] };
        for case in cases {
            let Some((mut admin, original, name, output)) = exported_namespace(engine) else { continue };
            let mut db = LiveAdapter::connect(&original).unwrap();
            let expected: Vec<(&str, &str)> = match (*case, mysql) {
                ("trigger", true) => {
                    db.execute_sql("CREATE TRIGGER parents_bi BEFORE INSERT ON parents FOR EACH ROW SET NEW.value = NEW.value").unwrap();
                    vec![("target_triggers", "parents")]
                }
                ("trigger", false) | ("unlinked_trigger", false) => {
                    let table = if *case == "trigger" { "parents" } else {
                        // Not linked to the dump; the PostgreSQL swap still refuses any trigger in the schema.
                        db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY)").unwrap();
                        "notes"
                    };
                    db.execute_sql("CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'").unwrap();
                    db.execute_sql(&format!("CREATE TRIGGER {table}_bi BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION touch()")).unwrap();
                    vec![("target_triggers", table), ("target_routines", "touch")]
                }
                ("routine", true) => {
                    db.execute_sql("CREATE PROCEDURE noop() SELECT 1").unwrap();
                    vec![("target_routines", "noop")]
                }
                ("routine", false) => {
                    db.execute_sql("CREATE FUNCTION answer() RETURNS int LANGUAGE sql AS 'SELECT 42'").unwrap();
                    vec![("target_routines", "answer")]
                }
                _ => {
                    db.execute_sql("CREATE EVENT tick ON SCHEDULE EVERY 1 DAY DISABLE DO SELECT 1").unwrap();
                    vec![("target_events", "tick")]
                }
            };
            let result = safe_import(&original, &output);
            if mysql {
                assert_eq!(result["status"], "ready_for_switch", "{result:#}");
                let candidate = result["candidate_target"]["database"].as_str().unwrap();
                let review = promote_plan(&original, &result);
                assert_eq!(review["can_promote"], true, "{review:#}");
                if *case == "trigger" { assert_eq!(result["triggers_dropped"][0]["name"], "parents_bi"); }
                else { assert!(!result["programs_kept"].as_array().unwrap().is_empty()); }
                drop_namespace(&mut admin, engine, candidate);
            }
            drop_namespace(&mut admin, engine, &name);
            std::fs::remove_dir_all(output).unwrap();
            if mysql { continue }
            assert_stopped_before_copy(&result, &format!("{engine}/{case}"));
            for (code, table) in expected {
                assert!(stops(&result["copy_blockers"], code, table), "{engine}/{case}: {code} {table}: {result}");
            }
        }
    }
}

#[test]
fn a_database_scoped_mysql_account_stops_before_copy() {
    let Some((mut admin, original, name, output)) = exported_namespace("mysql") else { return };
    let user = format!("tf_scope_{}", &name[name.len() - 9..]);
    admin.execute_sql(&format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    admin.execute_sql(&format!("GRANT ALL ON `{name}`.* TO '{user}'@'%'")).unwrap();
    let scoped = Endpoint { user: user.clone(), ..original.clone() };
    let result = safe_import(&scoped, &output);
    admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
    drop_namespace(&mut admin, "mysql", &name);
    std::fs::remove_dir_all(output).unwrap();
    assert_stopped_before_copy(&result, "scoped account");
    let codes = result["copy_blockers"].as_array().unwrap().iter().map(|item| item["code"].as_str().unwrap()).collect::<Vec<_>>();
    assert!(codes.contains(&"copy_create_privilege_missing") && codes.contains(&"metadata_visibility_unproven"), "{result}");
}

#[test]
fn a_rehearsal_copy_skips_the_swap_checks_and_cannot_be_promoted() {
    for engine in ["mysql", "postgresql"] {
        let Some((mut admin, original, name, output)) = exported_namespace(engine) else { continue };
        let mut db = LiveAdapter::connect(&original).unwrap();
        // Linked to the dump: a safe restore that may swap stops here.
        db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_notes FOREIGN KEY(parent_id) REFERENCES parents(id))").unwrap();
        let result = safe_import_with(&original, &output, json!({"verify_only": true}));
        let refused = promote_plan(&original, &result);
        if let Some(candidate) = result["candidate_target"][if engine == "mysql" { "database" } else { "schema" }].as_str() {
            assert!(candidate.starts_with("tf_restore_"), "{candidate}");
            drop_namespace(&mut admin, engine, candidate);
        }
        drop_namespace(&mut admin, engine, &name);
        std::fs::remove_dir_all(output).unwrap();
        assert_eq!(result["success"], true, "{engine}: {result}");
        assert_eq!(result["verified"], true, "{engine}: {result}");
        assert_eq!(result["candidate_created"], true, "{engine}: {result}");
        assert_eq!(result["verify_only"], true, "{engine}: {result}");
        assert_eq!(refused["event"], "error", "{engine}: {refused}");
        assert!(refused["message"].as_str().unwrap().contains("rehearsal"), "{engine}: {refused}");
    }
}

#[test]
fn a_link_made_after_the_copy_blocks_the_swap_plan() {
    for engine in ["mysql", "postgresql"] {
        let Some((mut admin, original, name, output)) = exported_namespace(engine) else { continue };
        let restored = safe_import(&original, &output);
        assert_eq!(restored["status"], "ready_for_switch", "{engine}: {restored}");
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY, parent_id INT, CONSTRAINT fk_notes FOREIGN KEY(parent_id) REFERENCES parents(id))").unwrap();
        let planned = promote_plan(&original, &restored);
        let candidate = restored["candidate_target"][if engine == "mysql" { "database" } else { "schema" }].as_str().unwrap().to_string();
        assert!(candidate.starts_with("tf_restore_"), "{candidate}");
        drop_namespace(&mut admin, engine, &candidate);
        drop_namespace(&mut admin, engine, &name);
        std::fs::remove_dir_all(output).unwrap();
        assert_eq!(planned["status"], "blocked", "{engine}: {planned}");
        assert_eq!(planned["can_promote"], false, "{engine}: {planned}");
        assert!(stops(&planned["copy_blockers"], "linked_target_only", "notes"), "{engine}: {planned}");
        assert!(planned["blockers"].to_string().contains("notes"), "{engine}: {planned}");
    }
}

#[test]
fn a_dump_view_now_reading_a_target_only_table_stops_only_mysql() {
    for engine in ["mysql", "postgresql"] {
        let Some(base) = endpoint(engine) else { continue };
        let mut admin = LiveAdapter::connect(&base).unwrap();
        let name = unique();
        admin.execute_sql(&format!("CREATE {} {name}", namespace_kind(engine))).unwrap();
        let mut original = base.clone();
        if engine == "mysql" {
            original.database = name.clone();
        } else {
            original.schema = Some(name.clone());
        }
        let mut db = LiveAdapter::connect(&original).unwrap();
        db.execute_sql("CREATE TABLE parents(id INT PRIMARY KEY, value VARCHAR(40))").unwrap();
        db.execute_sql("INSERT INTO parents VALUES(1,'dump')").unwrap();
        db.execute_sql("CREATE VIEW parent_ids AS SELECT id FROM parents").unwrap();
        let output = std::env::temp_dir().join(unique());
        let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
        assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:?}");
        // The target's own definition of the dump view now reads a table the dump does not have.
        db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY)").unwrap();
        db.execute_sql("CREATE OR REPLACE VIEW parent_ids AS SELECT id FROM notes").unwrap();
        let result = safe_import(&original, &output);
        if let Some(candidate) = result["candidate_target"][if engine == "mysql" { "database" } else { "schema" }].as_str() {
            if candidate != name {
                drop_namespace(&mut admin, engine, candidate);
            }
        }
        drop_namespace(&mut admin, engine, &name);
        std::fs::remove_dir_all(output).unwrap();
        if engine == "mysql" {
            // The MySQL swap would take that table along.
            assert_stopped_before_copy(&result, engine);
            assert!(stops(&result["copy_blockers"], "linked_target_only", "notes"), "{result}");
        } else {
            // PostgreSQL replaces the view in place and leaves the table alone.
            assert_eq!(result["status"], "ready_for_switch", "{result}");
        }
    }
}

#[test]
fn safe_restore_carries_dump_triggers_routines_events_and_guided_recovery() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    for sql in [
        "CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='dump guard'",
        "CREATE TRIGGER ordered AFTER INSERT ON parents FOR EACH ROW SET @tf_first=1",
        "CREATE TRIGGER ordered_second AFTER INSERT ON parents FOR EACH ROW SET @tf_second=2",
        "CREATE PROCEDURE p() SELECT 'dump'",
        "CREATE FUNCTION f() RETURNS INT DETERMINISTIC SQL SECURITY INVOKER RETURN 2",
        "CREATE EVENT e ON SCHEDULE AT '2099-01-01' ON COMPLETION PRESERVE ENABLE DO SET @tf_event=1",
        "CREATE EVENT new_e ON SCHEDULE AT '2099-01-01' ENABLE DO SET @tf_new=1",
        "CREATE PROCEDURE new_p() SELECT 'new'",
    ] { db.execute_sql(sql).unwrap(); }
    let output=root.join("programs");
    let exported=run("dump.run",json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"]=="error"),"{exported:#?}");
    for sql in [
        "DROP TRIGGER guard", "DROP TRIGGER ordered", "DROP TRIGGER ordered_second",
        "CREATE TRIGGER original_guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='original guard'",
        "DROP PROCEDURE p", "CREATE PROCEDURE p() SELECT 'original'",
        "DROP FUNCTION f", "CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1",
        "ALTER EVENT e DISABLE", "DROP EVENT new_e", "DROP PROCEDURE new_p",
        "CREATE PROCEDURE kept_p() SELECT 'keep'", "CREATE EVENT kept_e ON SCHEDULE AT '2099-01-01' DISABLE DO SELECT 1",
        "CREATE TABLE notes(id INT PRIMARY KEY)",
        "CREATE TRIGGER kept_guard BEFORE INSERT ON notes FOR EACH ROW SET NEW.id=NEW.id",
    ] { db.execute_sql(sql).unwrap(); }
    let rehearsal=safe_import_with(&original,&output,json!({"verify_only":true}));
    assert_eq!(rehearsal["success"],true,"{rehearsal:#}");
    let rehearsal_db=rehearsal["candidate_target"]["database"].as_str().unwrap();
    assert_eq!(first_value(&original,&format!("SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.EVENTS WHERE EVENT_SCHEMA='{rehearsal_db}'")),"0");
    drop_namespace(&mut admin,"mysql",rehearsal_db);
    let user=format!("tf_priv_{}",&name[name.len()-12..]);
    admin.execute_sql(&format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    let restricted=Endpoint { user:user.clone(),..original.clone() };
    for privilege in ["TRIGGER","CREATE ROUTINE","ALTER ROUTINE","EVENT","SUPER"] {
        admin.execute_sql(&format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'")).unwrap();
        admin.execute_sql(&format!("REVOKE {privilege} ON *.* FROM '{user}'@'%'")).unwrap();
        let refused=safe_import(&restricted,&output);
        assert_stopped_before_copy(&refused,privilege);
        let code=match privilege { "TRIGGER"=>"trigger_privilege_missing", "SUPER"=>"trigger_binlog_privilege", _=>"program_privilege_missing" };
        assert!(refused["copy_blockers"].as_array().unwrap().iter().any(|b|b["code"]==code),"{privilege}: {refused:#}");
    }
    admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
    let restored=safe_import(&original,&output);
    assert_eq!(restored["status"],"ready_for_switch","{restored:#}");
    assert_eq!(restored["triggers_dropped"][0]["name"],"original_guard");
    assert!(restored["programs_kept"].as_array().unwrap().contains(&json!("routine:kept_p")));
    let candidate=restored["candidate_target"]["database"].as_str().unwrap();
    let inventory=|database:&str| format!("SELECT CAST((SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='{database}')+(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='{database}')+(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA='{database}') AS CHAR) AS n");
    assert_eq!(first_value(&original,&inventory(candidate)),"0");
    let planned=promote_plan(&original,&restored);
    assert_eq!(planned["can_promote"],true,"{planned:#}");
    let done=run("dump.promote",json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
    let done=done.iter().find(|e| e["event"]=="result").unwrap_or_else(||panic!("{done:#?}"));
    assert_eq!(done["status"],"promoted","{done:#}");
    assert_eq!(first_value(&original,"SELECT f() AS n"),"2");
    assert_eq!(first_value(&original,"SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='e'"),"DISABLED");
    assert_eq!(first_value(&original,"SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='new_e'"),"ENABLED");
    assert_eq!(first_value(&original,"SELECT GROUP_CONCAT(TRIGGER_NAME ORDER BY ACTION_ORDER) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='parents' AND ACTION_TIMING='AFTER'"),"ordered,ordered_second");
    assert_eq!(first_value(&original,&inventory(candidate)),"0");
    let backups=|action:&str,extra:Value| {
        let mut payload=json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":action});
        payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        let events=run("restore.backups",payload);
        events.iter().find(|e|e["event"]=="result").cloned().unwrap_or_else(||panic!("{events:#?}"))
    };
    let recovery=backups("rollback_plan",json!({}));
    assert_eq!(recovery["can_rollback"],true,"{recovery:#}");
    let recovered=backups("rollback_apply",json!({"confirmed":true,"plan_digest":recovery["plan_digest"]}));
    assert_eq!(recovered["status"],"rolled_back","{recovered:#}");
    assert_eq!(first_value(&original,"SELECT f() AS n"),"1");
    assert_eq!(first_value(&original,"SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='original_guard'"),"1");
    assert_eq!(first_value(&original,"SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='new_p'"),"0");
    assert_eq!(first_value(&original,"SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='new_e'"),"0");
    for namespace in [candidate,done["backup_namespace"].as_str().unwrap(),done["clone_database"].as_str().unwrap(),recovered["displaced_backup"].as_str().unwrap(),&name] {
        drop_namespace(&mut admin,"mysql",namespace);
    }
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn safe_restore_trigger_collision_is_refused_before_copy_and_skip_toggle_works() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db=LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='guard'").unwrap();
    let output=root.join("programs");
    let exported=run("dump.run",json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e|e["event"]=="error"),"{exported:#?}");
    db.execute_sql("DROP TRIGGER guard").unwrap();
    db.execute_sql("CREATE TABLE notes(id INT PRIMARY KEY)").unwrap();
    db.execute_sql("CREATE TRIGGER guard BEFORE INSERT ON notes FOR EACH ROW SET NEW.id=NEW.id").unwrap();
    let refused=safe_import(&original,&output);
    assert_stopped_before_copy(&refused,"trigger name collision");
    assert!(refused["copy_blockers"].as_array().unwrap().iter().any(|b|b["code"]=="trigger_name_conflict"));
    let skipped=safe_import_with(&original,&output,json!({"skip_triggers":true}));
    assert_eq!(skipped["status"],"ready_for_switch","{skipped:#}");
    assert_eq!(skipped["programs_skipped_toggle"],json!(["trigger:parents:guard"]));
    let plan=promote_plan(&original,&skipped);
    assert_eq!(plan["can_promote"],true,"{plan:#}");
    let done=run("dump.promote",json!({"endpoint":original,"action":"confirm","report_path":skipped["report_path"],"restore_id":skipped["restore_id"],"plan_digest":plan["plan_digest"],"overwrite_confirmed":true}));
    let done=done.iter().find(|e|e["event"]=="result").unwrap();
    assert_eq!(done["status"],"promoted","{done:#}");
    assert_eq!(first_value(&original,"SELECT EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='guard'"),"notes");
    for ns in [skipped["candidate_target"]["database"].as_str().unwrap(),done["backup_namespace"].as_str().unwrap(),done["clone_database"].as_str().unwrap(),&name] { drop_namespace(&mut admin,"mysql",ns); }
    std::fs::remove_dir_all(root).unwrap();
}


#[test]
fn safe_restore_source_schema_reference_is_checked_against_final_target_before_copy() {
    let Some((mut admin, original, name, root))=exported_namespace("mysql") else {return};
    let mut db=LiveAdapter::connect(&original).unwrap();
    db.execute_sql(&format!("CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=(SELECT value FROM `{name}`.parents WHERE id=1)")).unwrap();
    let output=root.join("programs");
    let exported=run("dump.run",json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e|e["event"]=="error"),"{exported:#?}");
    let same=safe_import(&original,&output);
    assert_eq!(same["status"],"ready_for_switch","{same:#}");
    let other_name=unique();
    admin.execute_sql(&format!("CREATE DATABASE {other_name}")).unwrap();
    let other=Endpoint{database:other_name.clone(),..original.clone()};
    let refused=safe_import(&other,&output);
    assert_stopped_before_copy(&refused,"source schema reference");
    assert_eq!(refused["copy_blockers"][0]["code"],"program_source_schema_reference");
    let skipped=safe_import_with(&other,&output,json!({"skip_triggers":true}));
    assert_eq!(skipped["status"],"ready_for_switch","{skipped:#}");
    for ns in [same["candidate_target"]["database"].as_str().unwrap(),skipped["candidate_target"]["database"].as_str().unwrap(),&other_name,&name] { drop_namespace(&mut admin,"mysql",ns); }
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn rv204_rollback_lock_timeout_does_not_drop_promoted_triggers() {
    use mysql::prelude::Queryable;
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    for sql in [
        "CREATE TABLE zz(id INT PRIMARY KEY)",
        "INSERT INTO zz VALUES(1)",
        "CREATE TRIGGER a_trg BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=NEW.value",
        "CREATE TRIGGER z_trg BEFORE INSERT ON zz FOR EACH ROW SET NEW.id=NEW.id",
    ] { db.execute_sql(sql).unwrap(); }
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    let restored = safe_import(&original, &output);
    assert_eq!(restored["status"], "ready_for_switch", "{restored:#}");
    let planned = promote_plan(&original, &restored);
    assert_eq!(planned["can_promote"], true, "{planned:#}");
    let done = run("dump.promote", json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
    let done = done.iter().find(|e| e["event"] == "result").cloned().unwrap_or_else(|| panic!("{done:#?}"));
    assert_eq!(done["status"], "promoted", "{done:#}");
    let triggers_sql = "SELECT IFNULL(GROUP_CONCAT(TRIGGER_NAME ORDER BY TRIGGER_NAME),'') AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()";
    let before = first_value(&original, triggers_sql);
    let backups = |action: &str, extra: Value| {
        let mut payload = json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":action});
        payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        let events = run("restore.backups", payload);
        events.iter().find(|e| e["event"] == "result" || e["event"] == "error").cloned().unwrap_or_else(|| panic!("{events:#?}"))
    };
    let recovery = backups("rollback_plan", json!({}));
    assert_eq!(recovery["can_rollback"], true, "{recovery:#}");
    // Session 2: an open read transaction holds a shared metadata lock on zz.
    let LiveAdapter::MySql(mut other) = LiveAdapter::connect(&original).unwrap() else { unreachable!() };
    other.query_drop("START TRANSACTION").unwrap();
    let _: Vec<i64> = other.query("SELECT id FROM zz").unwrap();
    let recovered = backups("rollback_apply", json!({"confirmed":true,"plan_digest":recovery["plan_digest"]}));
    other.query_drop("ROLLBACK").unwrap();
    drop(other);
    let after = first_value(&original, triggers_sql);
    let retry = backups("rollback_plan", json!({}));
    eprintln!("RV204 before={before} after={after}\nRV204 recovered={recovered:#}\nRV204 retry={retry:#}");
    let mut namespaces = vec![name.clone()];
    for v in [&restored["candidate_target"]["database"], &done["backup_namespace"], &done["clone_database"], &recovered["displaced_backup"]] {
        if let Some(ns) = v.as_str() { namespaces.push(ns.to_string()); }
    }
    for ns in &namespaces { drop_namespace(&mut admin, "mysql", ns); }
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(before, "a_trg,z_trg");
    assert_eq!(after, "a_trg,z_trg", "promoted trigger lost after a failed recovery lock: {recovered:#}");
    assert_eq!(recovered["status"], "failed_no_change", "{recovered:#}");
}

#[test]
fn recovery_keeps_a_previous_function_it_cannot_recreate() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 2").unwrap();
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    // Target f was made while binlog trust was on; with trust off it cannot be re-created (1418).
    let trust = first_value(&original, "SELECT CAST(@@GLOBAL.log_bin_trust_function_creators AS CHAR) AS n");
    assert_eq!(first_value(&original, "SELECT CAST(@@GLOBAL.log_bin AS CHAR) AS n"), "1");
    db.execute_sql("DROP FUNCTION f").unwrap();
    admin.execute_sql("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
    let made = db.execute_sql("CREATE FUNCTION f() RETURNS INT RETURN 1");
    admin.execute_sql("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    made.unwrap();
    let restored = safe_import(&original, &output);
    let planned = promote_plan(&original, &restored);
    let done = run("dump.promote", json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
    let done = done.iter().find(|e| e["event"] == "result" || e["event"] == "error").cloned().unwrap();
    let count_f = "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='f'";
    let f_after_promote = first_value(&original, count_f);
    let backups = |action: &str, extra: Value| {
        let mut payload = json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":action});
        payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        run("restore.backups", payload).into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap()
    };
    let recovery = backups("rollback_plan", json!({}));
    let recovered = if recovery["can_rollback"] == true { backups("rollback_apply", json!({"confirmed":true,"plan_digest":recovery["plan_digest"]})) } else { Value::Null };
    let f_after_recovery = first_value(&original, count_f);
    eprintln!("restored.status={} planned.can_promote={} done.status={} programs_failed={}", restored["status"], planned["can_promote"], done["status"], done["programs_failed"]);
    eprintln!("f_after_promote={f_after_promote} can_rollback={} recovered.status={} recovered.error={} f_after_recovery={f_after_recovery}", recovery["can_rollback"], recovered["status"], recovered["error"]);
    // Clean up before asserting.
    for ns in [restored["candidate_target"]["database"].as_str(), done["backup_namespace"].as_str(), done["clone_database"].as_str(), recovered["displaced_backup"].as_str(), Some(name.as_str())].into_iter().flatten() {
        drop_namespace(&mut admin, "mysql", ns);
    }
    admin.execute_sql(&format!("SET GLOBAL log_bin_trust_function_creators={}", trust.as_str().unwrap())).unwrap();
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(done["status"], "promoted_incomplete", "{done:#}");
    assert_eq!(done["programs_failed"][0]["code"], "program_restore_unavailable", "{done:#}");
    assert_eq!(f_after_promote, "1", "promotion left the previous f in place");
    assert_eq!(recovery["can_rollback"], true, "{recovery:#}");
    assert_eq!(f_after_recovery, "1", "recovery dropped the previous f it could not recreate: {recovered:#}");
}

#[test]
fn rv204_function_binlog_rule_without_triggers_stops_before_copy() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    let user = format!("tf_priv_{}", &name[name.len() - 12..]);
    admin.execute_sql(&format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    admin.execute_sql(&format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'")).unwrap();
    admin.execute_sql(&format!("REVOKE SUPER ON *.* FROM '{user}'@'%'")).unwrap();
    let restricted = Endpoint { user: user.clone(), ..original.clone() };
    let binlog = first_value(&restricted, "SELECT CONCAT(@@log_bin,'/',@@log_bin_trust_function_creators) AS n");
    let preview = run("dump.import_preview", json!({"target":restricted,"input_dir":output,"import_mode":"safe"}))
        .into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap();
    let safe = safe_import(&restricted, &output);
    let rehearsal = safe_import_with(&restricted, &output, json!({"verify_only":true}));
    eprintln!("RV204 binlog(log_bin/trust)={binlog}");
    eprintln!("RV204 preview copy_blockers={} blockers={} warnings={}", preview["copy_blockers"], preview["blockers"], preview["warnings"]);
    for (label, r) in [("safe", &safe), ("rehearsal", &rehearsal)] {
        eprintln!("RV204 {label}: status={} candidate_created={} error_code={} restore_id={} candidate={} rows={} candidate_status={} skipped={} msg={}",
            r["status"], r["candidate_created"], r["error_code"], r["restore_id"], r["candidate_target"]["database"],
            r["candidate_import"]["rows_imported"], r["candidate_import"]["status"],
            r["candidate_journal"]["routines_skipped"], r["message"]);
    }
    // Clean up before asserting so a failure leaves nothing behind.
    for r in [&safe, &rehearsal] {
        if let Some(candidate) = r["candidate_target"]["database"].as_str() {
            if candidate != name { drop_namespace(&mut admin, "mysql", candidate); }
        }
    }
    admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
    drop_namespace(&mut admin, "mysql", &name);
    std::fs::remove_dir_all(root).unwrap();
    assert_stopped_before_copy(&safe, "safe restore, FUNCTION only, SUPER revoked");
    assert_stopped_before_copy(&rehearsal, "rehearsal, FUNCTION only, SUPER revoked");
}
#[test]
fn rv204_target_trigger_without_super_passes_copy_checks_and_is_lost_on_recovery() {
    // Dump has no stored programs; the target's parents table gains a trigger afterwards.
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    LiveAdapter::connect(&original).unwrap()
        .execute_sql("CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='original guard'").unwrap();
    let user = format!("tf_rv204_{}", &name[name.len()-8..]);
    admin.execute_sql(&format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    admin.execute_sql(&format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'")).unwrap();
    admin.execute_sql(&format!("REVOKE SUPER ON *.* FROM '{user}'@'%'")).unwrap();
    let restricted = Endpoint { user: user.clone(), ..original.clone() };
    eprintln!("RV204 log_bin,trust={}", first_value(&original, "SELECT CONCAT(@@log_bin, ',', @@log_bin_trust_function_creators) AS n"));
    let restored = safe_import(&restricted, &root);
    eprintln!("RV204 import status={} blockers={} triggers_dropped={}", restored["status"], restored["copy_blockers"], restored["triggers_dropped"]);
    let mut done = json!({});
    let mut recovered = json!({});
    if restored["status"] == "ready_for_switch" {
        let planned = promote_plan(&restricted, &restored);
        eprintln!("RV204 plan can_promote={} blockers={}", planned["can_promote"], planned["blockers"]);
        let events = run("dump.promote", json!({"endpoint":restricted,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
        done = events.iter().find(|e| e["event"] == "result" || e["event"] == "error").cloned().unwrap();
        eprintln!("RV204 promote status={} msg={}", done["status"], done["message"]);
        let backups = |action: &str, extra: Value| {
            let mut payload = json!({"endpoint":restricted,"input_dirs":[root],"restore_id":restored["restore_id"],"action":action});
            payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
            run("restore.backups", payload).into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap()
        };
        let recovery = backups("rollback_plan", json!({}));
        eprintln!("RV204 rollback_plan can_rollback={} blockers={}", recovery["can_rollback"], recovery["blockers"]);
        recovered = backups("rollback_apply", json!({"confirmed":true,"plan_digest":recovery["plan_digest"]}));
        eprintln!("RV204 rollback_apply status={} error={} msg={}", recovered["status"], recovered["error"], recovered["message"]);
    }
    let guard = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='guard'");
    let rows = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM parents");
    eprintln!("RV204 after recovery guard_count={guard} parents_rows={rows}");
    // Clean up before asserting.
    let mut leftovers = vec![restored["candidate_target"]["database"].clone(), done["backup_namespace"].clone(), done["clone_database"].clone(), recovered["displaced_backup"].clone()];
    leftovers.push(json!(name));
    for db in leftovers.iter().filter_map(Value::as_str) { drop_namespace(&mut admin, "mysql", db); }
    admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
    std::fs::remove_dir_all(&root).unwrap();
    // Base 14c5037 refused this target before copying (target_triggers).
    assert_stopped_before_copy(&restored,"target trigger requires SUPER");
    assert!(restored["copy_blockers"].to_string().contains("target_trigger_binlog_privilege"));
    assert_eq!(guard,"1");
}
#[test]
fn rv204_program_privilege_on_original_only_does_not_fail_after_copy() {
    let cases = [
        ("EVENT", "CREATE EVENT e ON SCHEDULE AT '2099-01-01' ON COMPLETION PRESERVE DISABLE DO SET @tf_e=1"),
        ("CREATE ROUTINE", "CREATE PROCEDURE p() SELECT 1"),
        ("TRIGGER", "CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value='g'"),
    ];
    let mut outcomes = Vec::new();
    for (privilege, program) in cases {
        let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
        LiveAdapter::connect(&original).unwrap().execute_sql(program).unwrap();
        let output = root.join("programs");
        let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
        assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
        let user = format!("tf_rv_{}", &name[name.len() - 12..]);
        for sql in [
            format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
            format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'"),
            format!("REVOKE {privilege} ON *.* FROM '{user}'@'%'"),
            format!("GRANT {privilege} ON `{name}`.* TO '{user}'@'%'"),
        ] { admin.execute_sql(&sql).unwrap(); }
        let restricted = Endpoint { user: user.clone(), ..original.clone() };
        let result = safe_import(&restricted, &output);
        // Clean up before asserting.
        admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
        if let Some(candidate) = result["candidate_target"]["database"].as_str() {
            if candidate != name { drop_namespace(&mut admin, "mysql", candidate); }
        }
        drop_namespace(&mut admin, "mysql", &name);
        std::fs::remove_dir_all(root).unwrap();
        eprintln!("RV204 {privilege}: status={} candidate_created={} copy_blockers={} error_code={} error={}",
            result["status"], result["candidate_created"], result["copy_blockers"], result["error_code"], result["error"]);
        outcomes.push((privilege, result));
    }
    for (privilege, result) in &outcomes {
        if *privilege == "EVENT" { assert_eq!(result["status"],"ready_for_switch","{result:#}"); } else { assert_stopped_before_copy(result,privilege); }
    }
}
#[test]
fn rv18_unsavable_target_program_is_refused_only_after_the_copy() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE PROCEDURE p() SELECT 'dump'").unwrap();
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    db.execute_sql("DROP PROCEDURE p").unwrap();
    let mut seen = Vec::new();
    // A: dump without programs; latin1 trigger with a non-ASCII body on the replaced table.
    // B: dump with routine p; same-named latin1 target routine with a non-ASCII body.
    for (case, setup, teardown, dump) in [
        ("A trigger", "CREATE TRIGGER t_latin BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=CONCAT(NEW.value,'é')", "DROP TRIGGER t_latin", &root),
        ("B routine", "CREATE PROCEDURE p() SELECT 'é'", "DROP PROCEDURE p", &output),
    ] {
        db.execute_sql("SET NAMES latin1").unwrap();
        db.execute_sql(setup).unwrap();
        db.execute_sql("SET NAMES utf8mb4").unwrap();
        let restored = safe_import(&original, dump);
        let planned = if restored["status"] == "ready_for_switch" { promote_plan(&original, &restored) } else { json!(null) };
        if let Some(candidate) = restored["candidate_target"]["database"].as_str() {
            if candidate != name { drop_namespace(&mut admin, "mysql", candidate); }
        }
        db.execute_sql(teardown).unwrap();
        eprintln!("RV18 {case}: import status={} candidate_created={} copy_blockers={} | plan can_promote={} status={} blockers={}",
            restored["status"], restored["candidate_created"], restored["copy_blockers"], planned["can_promote"], planned["status"], planned["blockers"]);
        seen.push((case, restored, planned));
    }
    drop_namespace(&mut admin, "mysql", &name);
    std::fs::remove_dir_all(root).unwrap();
    for (case, restored, _planned) in seen {
        assert_stopped_before_copy(&restored,case);
        assert!(restored["copy_blockers"].to_string().contains("target_program_unsavable"),"{restored:#}");
    }
}
#[test]
fn rv21_binlog_unsafe_function_safe_vs_replace() {
    struct Trust(LiveAdapter);
    impl Drop for Trust { fn drop(&mut self) { let _ = self.0.execute_sql("SET GLOBAL log_bin_trust_function_creators=0"); } }
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut guard = Trust(LiveAdapter::connect(&endpoint("mysql").unwrap()).unwrap());
    guard.0.execute_sql("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT RETURN 1").unwrap();
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    db.execute_sql("DROP FUNCTION f").unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 7").unwrap();
    guard.0.execute_sql("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    assert_eq!(first_value(&original, "SELECT CAST(@@log_bin AS CHAR) AS n"), "1");

    let preview = run("dump.import_preview", json!({"target":original,"input_dir":output,"mode":"safe"}));
    let preview = preview.iter().find(|e| e["event"] == "result").cloned().unwrap_or(Value::Null);
    eprintln!("PREVIEW: warnings={} copy_blockers={} blockers={}", preview["warnings"], preview["copy_blockers"], preview["blockers"]);

    let safe = safe_import(&original, &output);
    eprintln!("SAFE: status={} candidate_created={} copy_blockers={} error_code={} message={}",
        safe["status"], safe["candidate_created"], safe["copy_blockers"], safe["error_code"], safe["message"]);
    let rehearsal = safe_import_with(&original, &output, json!({"verify_only":true}));
    eprintln!("REHEARSAL: status={} candidate_created={} copy_blockers={} message={}",
        rehearsal["status"], rehearsal["candidate_created"], rehearsal["copy_blockers"], rehearsal["message"]);
    assert_eq!(first_value(&original, "SELECT f() AS n"), "7", "safe never touched the original");

    let replace = run("dump.import", json!({"target":original,"input_dir":output,"mode":"replace","threads":1}))
        .into_iter().find(|e| e["event"] == "result").expect("replace result");
    eprintln!("REPLACE: success={} status={} message={} warnings={}",
        replace["success"], replace["status"], replace["message"], replace["program_warnings"]);
    let replace_f = first_value(&original, "SELECT f() AS n");
    eprintln!("REPLACE target f() = {replace_f}");

    let candidates = [safe["candidate_target"]["database"].as_str().map(str::to_owned), rehearsal["candidate_target"]["database"].as_str().map(str::to_owned)];
    for ns in candidates.iter().flatten() { drop_namespace(&mut admin, "mysql", ns); }
    drop_namespace(&mut admin, "mysql", &name);
    let _ = std::fs::remove_dir_all(root);

    for (case, r) in [("safe", &safe), ("rehearsal", &rehearsal)] {
        assert_stopped_before_copy(r,case);
        assert!(r["copy_blockers"].to_string().contains("copy_routine_binlog_unsafe"),"{r:#}");
    }
    assert_eq!(replace["status"], "partial", "{replace:#}");
    assert_eq!(replace_f, "7", "replace keeps the target's same-named function");
}
#[test]
fn rv22_rollback_incomplete_reports_unknown_and_no_missing_programs() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    for sql in ["CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 2",
                "CREATE EVENT new_e ON SCHEDULE AT '2099-01-01' ENABLE DO SET @tf_new=1"] { db.execute_sql(sql).unwrap(); }
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    for sql in ["DROP FUNCTION f", "CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1", "DROP EVENT new_e"] { db.execute_sql(sql).unwrap(); }
    let restored = safe_import(&original, &output);
    assert_eq!(restored["status"], "ready_for_switch", "{restored:#}");
    let planned = promote_plan(&original, &restored);
    assert_eq!(planned["can_promote"], true, "{planned:#}");
    let done = run("dump.promote", json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
    let done = done.iter().find(|e| e["event"] == "result").cloned().unwrap_or_else(|| panic!("{done:#?}"));
    assert_eq!(done["status"], "promoted", "{done:#}");
    // Recovery account: everything except CREATE ROUTINE, so DROP FUNCTION works and the re-CREATE fails.
    let user = format!("tf_rb_{}", &name[name.len() - 12..]);
    admin.execute_sql(&format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    admin.execute_sql(&format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'")).unwrap();
    admin.execute_sql(&format!("REVOKE CREATE ROUTINE ON *.* FROM '{user}'@'%'")).unwrap();
    let restricted = Endpoint { user: user.clone(), ..original.clone() };
    let backups = |action: &str, extra: Value| {
        let mut payload = json!({"endpoint":restricted,"input_dirs":[output],"restore_id":restored["restore_id"],"action":action});
        payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        let events = run("restore.backups", payload);
        events.iter().find(|e| e["event"] == "result" || e["event"] == "error").cloned().unwrap()
    };
    let recovery = backups("rollback_plan", json!({}));
    let recovered = json!({});
    let f_left = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='f'");
    let e_left = first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='new_e'");
    let tables_back = first_value(&original, "SELECT value FROM parents WHERE id=1");
    let retry = backups("rollback_plan", json!({}));
    let list = backups("list", json!({}));
    eprintln!("RV22 recovery_plan={recovery:#}\nRV22 recovered={recovered:#}\nRV22 f_left={f_left} new_e_left={e_left} parents.value={tables_back}\nRV22 retry={retry:#}\nRV22 list={list:#}");
    // cleanup before asserting
    admin.execute_sql(&format!("DROP USER '{user}'@'%'")).unwrap();
    let candidate = restored["candidate_target"]["database"].as_str().map(str::to_string);
    for ns in [candidate, done["backup_namespace"].as_str().map(str::to_string), done["clone_database"].as_str().map(str::to_string),
               recovered["displaced_backup"].as_str().map(str::to_string), Some(name.clone())].into_iter().flatten() {
        drop_namespace(&mut admin, "mysql", &ns);
    }
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(recovery["can_rollback"],false,"{recovery:#}");
    assert!(recovery["blockers"].to_string().contains("CREATE ROUTINE"));
    assert_eq!(f_left,"1");
}

#[test]
fn rv10_rollback_incomplete_is_reported_and_reconciled() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    for sql in [
        "CREATE PROCEDURE p() SELECT 'dump'",
        "CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 2",
        "CREATE EVENT new_e ON SCHEDULE AT '2099-01-01' ENABLE DO SET @tf_new=1",
    ] { db.execute_sql(sql).unwrap(); }
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    for sql in ["DROP PROCEDURE p", "CREATE PROCEDURE p() SELECT 'original'", "DROP FUNCTION f",
        "CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1", "DROP EVENT new_e"] { db.execute_sql(sql).unwrap(); }
    let restored = safe_import(&original, &output);
    assert_eq!(restored["status"], "ready_for_switch", "{restored:#}");
    let planned = promote_plan(&original, &restored);
    let done = run("dump.promote", json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
    let done = done.iter().find(|e| e["event"] == "result").cloned().unwrap_or_else(|| panic!("{done:#?}"));
    assert_eq!(done["status"], "promoted", "{done:#}");
    let backups = |action: &str, extra: Value| {
        let mut payload = json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":action});
        payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        run("restore.backups", payload).into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap()
    };
    let recovery = backups("rollback_plan", json!({}));
    assert_eq!(recovery["can_rollback"], true, "{recovery:#}");
    // An application session that used f() in an open transaction holds f's metadata lock,
    // so the post-UNLOCK program loop hits lock_wait_timeout=2 on DROP FUNCTION f.
    let mut holder = LiveAdapter::connect(&original).unwrap();
    holder.execute_sql("START TRANSACTION").unwrap();
    holder.execute_sql("SELECT f()").unwrap();
    let recovered = backups("rollback_apply", json!({"confirmed":true,"plan_digest":recovery["plan_digest"]}));
    holder.execute_sql("COMMIT").unwrap();
    let f_now = first_value(&original, "SELECT CAST(f() AS CHAR) AS n");
    let p_now = first_value(&original, "SELECT ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='p'");
    let new_e = first_value(&original, "SELECT COALESCE((SELECT STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='new_e'),'absent') AS n");
    let reconciled = backups("reconcile", json!({}));
    let listed = backups("list", json!({}));
    let second = backups("rollback_plan", json!({}));
    eprintln!("RV10 recovered={recovered:#}\nRV10 f()={f_now} p={p_now} new_e={new_e}\nRV10 reconcile={reconciled:#}\nRV10 list.verdict={} list.rollbacks={}\nRV10 second_plan.can_rollback={} blockers={}",
        listed["backups"][0]["backup"]["verdict"], listed["backups"][0]["rollbacks"], second["can_rollback"], second["blockers"]);
    for ns in [restored["candidate_target"]["database"].as_str().unwrap(), done["backup_namespace"].as_str().unwrap(),
        done["clone_database"].as_str().unwrap(), recovered["displaced_backup"].as_str().unwrap_or("tf_none"), &name] {
        drop_namespace(&mut admin, "mysql", ns);
    }
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(recovered["status"], "rollback_incomplete", "{recovered:#}");
    // Expected behaviour: a message for this status and a reconcile that reports the programs.
    assert!(!recovered["message"].as_str().unwrap().contains("outcome is unknown"), "rollback_incomplete message: {}", recovered["message"]);
    assert_eq!(reconciled["conclusion"], "rollback_incomplete", "{reconciled:#}");
    assert!(recovered["missing_programs"].to_string().contains("routine:f"));
    assert_eq!(recovered["pending_program"]["name"],"f");
    assert!(recovered["error"].as_str().unwrap().contains("1205"),"{recovered:#}");
    assert_eq!(new_e,"absent","later programs are still restored after a failure");
    assert!(p_now.as_str().unwrap().contains("original"));
    assert!(reconciled["evidence"].get("missing_programs").is_some(), "{reconciled:#}");
}
#[test]
fn rv204_promoted_verdict_survives_normal_post_cutover_program_changes() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    // App-owned one-shot event the dump does not have (default ON COMPLETION NOT PRESERVE).
    db.execute_sql("CREATE EVENT kept_once ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 40 SECOND DO SET @tf_once=1").unwrap();
    let restored = safe_import(&original, &root);
    assert_eq!(restored["status"], "ready_for_switch", "{restored:#}");
    let planned = promote_plan(&original, &restored);
    assert_eq!(planned["can_promote"], true, "{planned:#}");
    let done = run("dump.promote", json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":planned["plan_digest"],"overwrite_confirmed":true}));
    let done = done.iter().find(|e| e["event"] == "result").cloned().unwrap_or_else(|| panic!("{done:#?}"));
    assert_eq!(done["status"], "promoted", "{done:#}");
    let backups = |action: &str, extra: Value| {
        let mut payload = json!({"endpoint":original,"input_dirs":[root],"restore_id":restored["restore_id"],"action":action});
        payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        let events = run("restore.backups", payload);
        events.iter().find(|e| e["event"] == "result").cloned().unwrap_or_else(|| panic!("{events:#?}"))
    };
    let summary = |label: &str, v: &Value| eprintln!("RV204 {label}: can_cleanup={} can_rollback={} conclusion={} message={} blockers={}",
        v["can_cleanup"], v["can_rollback"], v["conclusion"], v["message"], v["blockers"]);
    let control = backups("cleanup_plan", json!({"target":"backup"}));
    summary("control cleanup (event pending)", &control);
    // App deploy adds one routine after the cutover.
    db.execute_sql("CREATE PROCEDURE app_added() SELECT 1").unwrap();
    let added = backups("cleanup_plan", json!({"target":"backup"}));
    summary("cleanup after app adds routine", &added);
    let added_rec = backups("reconcile", json!({}));
    summary("reconcile after app adds routine", &added_rec);
    eprintln!("RV204 evidence missing_programs={}", added_rec["evidence"]["missing_programs"]);
    db.execute_sql("DROP PROCEDURE app_added").unwrap();
    // The server runs the kept NOT PRESERVE event and drops it.
    let mut gone = false;
    for _ in 0..60 {
        if first_value(&original, "SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE() AND EVENT_NAME='kept_once'") == "0" { gone = true; break }
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
    eprintln!("RV204 kept_once dropped by server: {gone}");
    let after = backups("cleanup_plan", json!({"target":"backup"}));
    summary("cleanup after kept event completed", &after);
    let reconciled = backups("reconcile", json!({}));
    summary("reconcile after kept event completed", &reconciled);
    eprintln!("RV204 evidence missing_programs={}", reconciled["evidence"]["missing_programs"]);
    let recovery = backups("rollback_plan", json!({}));
    summary("rollback_plan after kept event completed", &recovery);
    for ns in [restored["candidate_target"]["database"].as_str().unwrap(), done["backup_namespace"].as_str().unwrap(), done["clone_database"].as_str().unwrap(), &name] {
        drop_namespace(&mut admin, "mysql", ns);
    }
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(control["can_cleanup"], true, "control: {control:#}");
    assert!(gone);
    assert_eq!(reconciled["conclusion"], "promoted", "BUG reproduced: completed kept event flips verdict: {reconciled:#}");
    assert_eq!(after["can_cleanup"], true, "BUG reproduced: backup cleanup blocked: {after:#}");
}

#[test]
fn repro_view_calling_dump_function_in_safe_copy() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    db.execute_sql("CREATE VIEW v AS SELECT f() AS x").unwrap();
    let view_def = first_value(&original, "SELECT VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='v'");
    let output = root.join("programs");
    let exported = run("dump.run", json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e| e["event"] == "error"), "{exported:#?}");
    // Case A: target exists but no longer has f (and v).
    db.execute_sql("DROP VIEW v").unwrap();
    db.execute_sql("DROP FUNCTION f").unwrap();
    let a = safe_import(&original, &output);
    eprintln!("VIEW_DEF={view_def}\nCASE_A={a:#}");
    let a_candidate = a["candidate_target"]["database"].as_str().map(str::to_string);
    // Case B: target still has the same f.
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    let b = safe_import(&original, &output);
    let b_candidate = b["candidate_target"]["database"].as_str().map(str::to_string);
    let b_plan = if b["status"] == "ready_for_switch" { promote_plan(&original, &b) } else { json!(null) };
    eprintln!("CASE_B={b:#}\nCASE_B_PLAN={b_plan:#}");
    // Contrast: plain replace into the original.
    db.execute_sql("DROP FUNCTION f").unwrap();
    let r = run("dump.import", json!({"target":original,"input_dir":output,"mode":"replace","threads":1}))
        .into_iter().find(|e| e["event"] == "result" || e["event"] == "error").unwrap();
    eprintln!("REPLACE={r:#}");
    for ns in [a_candidate.as_deref(), b_candidate.as_deref(), Some(name.as_str())].into_iter().flatten() {
        drop_namespace(&mut admin, "mysql", ns);
    }
    std::fs::remove_dir_all(root).unwrap();
    assert_stopped_before_copy(&a,"view calls dump function without target function");
    assert_stopped_before_copy(&b,"view calls dump function with target function");
    assert!(a["copy_blockers"].to_string().contains("view_calls_dump_routine"));
    assert_eq!(r["success"], true, "{r:#}");
}

/// A safe switch detaches triggers from both replaced tables before renaming.
#[test]
fn rv_patched_moved_trigger_promotes() {
    let Some((mut admin, original, name, root)) = exported_namespace("mysql") else { return };
    let mut db=LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TABLE children(id INT PRIMARY KEY)").unwrap();
    db.execute_sql("CREATE TRIGGER trg BEFORE INSERT ON children FOR EACH ROW SET NEW.id=NEW.id").unwrap();
    let output=root.join("programs");
    let exported=run("dump.run",json!({"source":original,"output_dir":output,"data_format":"jsonl","compression":"none","threads":1,"mysql_snapshot_mode":"single_connection"}));
    assert!(!exported.iter().any(|e|e["event"]=="error"),"{exported:#?}");
    db.execute_sql("DROP TRIGGER trg").unwrap();
    db.execute_sql("CREATE TRIGGER trg BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=NEW.value").unwrap();
    let restored=safe_import(&original,&output);
    let mut drop=vec![name.clone()];
    if let Some(c)=restored["candidate_target"]["database"].as_str() { drop.push(c.into()); }
    let mut done=Value::Null;
    if restored["status"]=="ready_for_switch" {
        let plan=promote_plan(&original,&restored);
        if plan["can_promote"]==true {
            let events=run("dump.promote",json!({"endpoint":original,"action":"confirm","report_path":restored["report_path"],"restore_id":restored["restore_id"],"plan_digest":plan["plan_digest"],"overwrite_confirmed":true}));
            done=events.iter().find(|e|e["event"]=="result"||e["event"]=="error").cloned().unwrap();
            for k in ["backup_namespace","clone_database"] { if let Some(n)=done[k].as_str() { drop.push(n.into()); } }
        }
    }
    let on=if done["status"]=="promoted" { first_value(&original,"SELECT EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='trg'") } else { Value::Null };
    for ns in &drop { drop_namespace(&mut admin,"mysql",ns); }
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(done["status"],"promoted","{done:#}");
    assert_eq!(on,"children");
}
