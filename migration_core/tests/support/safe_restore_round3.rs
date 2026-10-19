/// [1] failed_original_unchanged after the original was rebuilt: a new plan is allowed, but reconcile says
/// retry stays blocked.
#[test]
fn rvg1_rebuilt_original_allows_candidate_cleanup_and_fresh_plan() {
    let Some((mut admin, original, name, output)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("UPDATE parents SET value='live'").unwrap();
    let restored = import(&original, &output, json!({}));
    let planned = promote(&original, &restored, json!({"action":"plan"}));
    let id_sql = format!("SELECT CAST(TABLE_ID AS CHAR) AS id FROM information_schema.INNODB_TABLES WHERE NAME='{name}/parents'");
    let before = first_value(&original, &id_sql);
    db.execute_sql("ALTER TABLE parents FORCE").unwrap();
    let after = first_value(&original, &id_sql);
    let done = promote(&original, &restored, json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
    db.execute_sql("ALTER TABLE parents FORCE").unwrap(); // App rebuild after the recorded failure.
    let again = promote(&original, &restored, json!({"action":"plan"}));
    let rc = result(run("restore.backups", json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":"reconcile"})));
    let listed = result(run("restore.backups", json!({"endpoint":original,"input_dirs":[output],"action":"list"})));
    let cand = result(run("restore.backups", json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":"cleanup_plan","target":"candidate"})));
    save_json("rvg1.json", &json!({"done":done,"again":again,"reconcile":rc,"list":listed,"candidate_cleanup":cand}));
    let latest=promote(&original,&restored,json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":again["plan_digest"]}));
    assert_eq!(latest["status"],"promoted","{latest:#}");
    let reconciled=result(run("restore.backups",json!({"endpoint":original,"input_dirs":[output],"restore_id":restored["restore_id"],"action":"reconcile"})));
    assert_eq!(reconciled["conclusion"],"promoted");
    assert_eq!(reconciled["retry_allowed"],false);
    save_json("rvg1_latest_attempt.json",&reconciled);
    for ns in [latest["backup_namespace"].as_str(),latest["clone_database"].as_str()].into_iter().flatten() {drop_db(&mut admin,ns);}

    for ns in [candidate_of(&restored), done["backup_namespace"].as_str().map(str::to_string), done["clone_database"].as_str().map(str::to_string), Some(name.clone())].into_iter().flatten() { drop_db(&mut admin, &ns); }
    std::fs::remove_dir_all(output).unwrap();
    eprintln!("RVG1 table_id {before} -> {after}; done.status={} original_unchanged={}\nagain.can_promote={} again.blockers={}\nreconcile conclusion={} retry_allowed={} message={}\nlist verdict={}\ncandidate cleanup can_cleanup={} blockers={}",
        done["status"], done["original_unchanged"], again["can_promote"], again["blockers"], rc["conclusion"], rc["retry_allowed"], rc["message"],
        listed["backups"][0]["backup"]["verdict"], cand["can_cleanup"], cand["blockers"]);
    assert_ne!(before, after);
    assert_eq!(done["status"], "failed_original_unchanged");
    assert_eq!(again["can_promote"], true, "{again:#}");
    assert_eq!(rc["conclusion"], "not_promoted");
    assert_eq!(rc["retry_allowed"],true);
    assert!(!rc["message"].as_str().unwrap().contains("Retry and cleanup stay blocked"));
    assert!(rc["evidence"]["note"].as_str().unwrap().contains("identities changed"));
    assert_eq!(cand["can_cleanup"], true);
}

/// [2] hidden target trigger: RENAME 1435 -> failed_original_unchanged; TRIGGER advice only in `error`.
#[test]
fn rvg2_hidden_trigger_failure_carries_actionable_code() {
    let Some((mut admin,original,name,root))=exported() else{return};
    let user=format!("rvg2_{}",&name[name.len()-8..]); let _users=Users(vec![user.clone()]);
    for sql in [format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),format!("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, LOCK TABLES, REFERENCES, CREATE VIEW, SHOW VIEW ON `{name}`.* TO '{user}'@'%'"),format!("GRANT ALL ON `tf\\_restore\\_%`.* TO '{user}'@'%'"),format!("GRANT ALL ON `tf\\_backup\\_%`.* TO '{user}'@'%'"),format!("GRANT ALL ON `tf\\_promote\\_%`.* TO '{user}'@'%'")]{admin.execute_sql(&sql).unwrap();}
    admin.execute_sql(&format!("CREATE TRIGGER `{name}`.hidden_guard BEFORE INSERT ON `{name}`.parents FOR EACH ROW SET NEW.value='guard'")).unwrap();
    let ep=Endpoint{user,..original.clone()};
    let restored=import(&ep,&root,json!({}));
    let planned=promote(&ep,&restored,json!({"action":"plan"}));
    let done=promote(&ep,&restored,json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
    save_json("rvg2_hidden_trigger_done.json",&done);
    let triggers = first_value(&original,"SELECT CAST(COUNT(*) AS CHAR) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()");
    for db in [candidate_of(&restored),done["backup_namespace"].as_str().map(str::to_string),done["clone_database"].as_str().map(str::to_string),Some(name)] .into_iter().flatten(){drop_db(&mut admin,&db);}
    std::fs::remove_dir_all(root).unwrap();
    eprintln!("RVG2 restored={} plan.can_promote={} done.status={}\nmessage={}\nerror={}\ntriggers left={triggers}", restored["status"], planned["can_promote"], done["status"], done["message"], done["error"]);
    assert_eq!(done["status"],"failed_original_unchanged","{done:#}");
    assert!(done["error"].as_str().unwrap().contains("TRIGGER privilege"));
    assert!(done["message"].as_str().unwrap().contains("target_trigger_hidden:"));
    assert_eq!(done["error_code"],"target_trigger_hidden");
}

/// [3] scheduled rehearsal (verify_only) of a dump whose view calls a dump function: stopped before copy.
#[test]
fn rvg3_rehearsal_result_for_view_calling_dump_function() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE FUNCTION f() RETURNS INT DETERMINISTIC RETURN 1").unwrap();
    db.execute_sql("CREATE VIEW v AS SELECT f() AS x").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP VIEW v").unwrap();
    db.execute_sql("DROP FUNCTION f").unwrap();
    let rehearsal = import(&original, &output, json!({"verify_only":true}));
    save_json("rvg3_rehearsal.json", &rehearsal);
    for c in [candidate_of(&rehearsal)].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
    drop_db(&mut admin, &name);
    std::fs::remove_dir_all(root).unwrap();
    eprintln!("RVG3 rehearsal status={} copy_blockers={}", rehearsal["status"], rehearsal["copy_blockers"]);
    assert_eq!(rehearsal["status"], "failed_original_untouched");
    assert!(codes(&rehearsal["copy_blockers"]).iter().any(|c| c.starts_with("view_calls_dump_routine")));
}

/// [8]/[9] root account, dump trigger trg moved from children to parents in the target: the safe preview has
/// only a direct_blockers trigger_name_conflict and the core completes the safe import and promotion.
#[test]
fn rvg8_safe_preview_with_only_direct_trigger_conflict_is_accepted_by_core() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TABLE children(id INT PRIMARY KEY)").unwrap();
    db.execute_sql("CREATE TRIGGER trg BEFORE INSERT ON children FOR EACH ROW SET NEW.id=NEW.id").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP TRIGGER trg").unwrap();
    db.execute_sql("CREATE TRIGGER trg BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=NEW.value").unwrap();
    let safe = preview(&original, &output, json!({"mode":"safe"}));
    save_json("rvg8_safe_preview_root.json", &safe);
    let restored = import(&original, &output, json!({}));
    let planned = promote(&original, &restored, json!({"action":"plan"}));
    let done = promote(&original, &restored, json!({"action":"confirm","overwrite_confirmed":true,"plan_digest":planned["plan_digest"]}));
    let on = first_value(&original, "SELECT GROUP_CONCAT(CONCAT(EVENT_OBJECT_TABLE,'.',TRIGGER_NAME)) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()");
    for ns in [candidate_of(&restored), done["backup_namespace"].as_str().map(str::to_string), done["clone_database"].as_str().map(str::to_string), Some(name.clone())].into_iter().flatten() { drop_db(&mut admin, &ns); }
    std::fs::remove_dir_all(root).unwrap();
    eprintln!("RVG8 preview blockers={} copy_blockers={} direct_blockers={}\nimport={} plan.can_promote={} done={} triggers={on}",
        safe["blockers"], safe["copy_blockers"], safe["direct_blockers"], restored["status"], planned["can_promote"], done["status"]);
    assert_eq!(safe["blockers"], json!([]));
    assert_eq!(safe["copy_blockers"], json!([]));
    assert!(codes(&safe["direct_blockers"]).iter().any(|c| c.starts_with("trigger_name_conflict")));
    assert_eq!(restored["status"], "ready_for_switch", "{restored:#}");
    assert_eq!(done["status"], "promoted", "{done:#}");
    assert_eq!(on, "children.trg");
}
