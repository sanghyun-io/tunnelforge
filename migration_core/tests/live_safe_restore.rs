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

fn restore_namespaces(engine: &str, endpoint: &Endpoint) -> Value {
    let sql = if engine == "mysql" {
        "SELECT COUNT(*) AS n FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'tf\\_restore\\_%'"
    } else {
        "SELECT COUNT(*)::text AS n FROM pg_namespace WHERE nspname LIKE 'tf\\_restore\\_%'"
    };
    first_value(endpoint, sql)
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
            let copies_before = restore_namespaces(engine, &original);
            let preview = run("dump.import_preview", json!({"target":original,"input_dir":output,"threads":1}));
            let preview = preview.into_iter().find(|e| e["event"] == "result").unwrap_or_else(|| panic!("{engine}/{link}: no preview result"));
            let result = safe_import(&original, &output);
            let copies_after = restore_namespaces(engine, &original);
            let rows = db.row_count("parents").unwrap();
            drop_namespace(&mut admin, engine, &name);
            std::fs::remove_dir_all(output).unwrap();
            let stops = |blockers: &Value| blockers.as_array().into_iter().flatten()
                .any(|item| item["code"] == "linked_target_only" && item["table"] == outside);
            assert!(stops(&preview["copy_blockers"]), "{engine}/{link}: preview {preview}");
            assert_eq!(result["success"], false, "{engine}/{link}: {result}");
            assert_eq!(result["status"], "failed_original_untouched", "{engine}/{link}: {result}");
            assert_eq!(result["candidate_created"], false, "{engine}/{link}: {result}");
            assert_eq!(result["error_code"], "copy_refused", "{engine}/{link}: {result}");
            assert!(stops(&result["copy_blockers"]), "{engine}/{link}: {result}");
            assert_eq!(copies_before, copies_after, "{engine}/{link}: a copy was made");
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
