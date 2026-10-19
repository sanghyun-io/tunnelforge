// ---------------------------------------------------------------- round-5 live reproductions (rvi)
// Included by the existing MySQL promotion test module.
fn rvi_save(name: &str, value: &Value) {
    if let Ok(dir) = std::env::var("RVI_EVIDENCE") {
        let _ = std::fs::create_dir_all(&dir);
        std::fs::write(std::path::Path::new(&dir).join(name), serde_json::to_vec_pretty(value).unwrap()).unwrap();
    }
}

/// claim [1]: RENAME committed, then creating a dump trigger fails (server error 1359 forced by a
/// same-named trigger created under the cutover lock) -> promoted_incomplete payload for the Python check.
#[test]
fn safe_promote_rvi1_trigger_create_failure_after_rename_payload() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvi1", &programs).unwrap();
    let db_name = original.database.clone();
    let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, db| {
        if phase == "after_rename" {
            db.query_drop(format!("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note='squatter'",
                qualified(&db_name, "guard"), qualified(&db_name, "parent"))).map_err(|e| e.to_string())?;
        }
        Ok(())
    }).unwrap();
    rvi_save("rvi1_live.json", &result);
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVI1 status={} success={} original_unchanged={} error_code={}\nmessage={}\nerror={}\nmissing={}",
        result["status"], result["success"], result["original_unchanged"], result["error_code"], result["message"], result["error"], result["missing_programs"]);
    assert_eq!(result["status"], "promoted_incomplete", "{result:#}");
    assert!(result["error"].as_str().unwrap().starts_with("program_create_failed: "), "{result:#}");
}

/// claim [0]: rollback_incomplete (f's restore hits 1205), the user recreates f, then OPTIMIZE TABLE
/// rebuilds parent. Expected: a note/blocker saying the originals were rebuilt and cannot be re-checked.
#[test]
fn safe_promote_rvi0_incomplete_recovery_then_rebuilt_original_explains_why() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvi0", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted", "{r:#}");
    let input = rvf_journal(&dir, "rvi0", &original, &candidate, &p, &r);
    let review = rvf_backups(&input, "rvi0", &original, "rollback_plan", json!({}));
    assert_eq!(review["can_rollback"], true, "{review:#}");
    let mut app = connect(&original).unwrap();
    app.query_drop("START TRANSACTION").unwrap();
    let _: Option<i64> = app.query_first(format!("SELECT {}()", qualified(&original.database, "f"))).unwrap();
    let applied = rvf_backups(&input, "rvi0", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    app.query_drop("ROLLBACK").unwrap();
    drop(app);
    let list0 = rvf_backups(&input, "rvi0", &original, "list", json!({}));
    let f_before: Vec<(String, String)> = conn.exec("SELECT ROUTINE_NAME, ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=? AND ROUTINE_NAME='f'", (&original.database,)).unwrap();
    // The user recreates f from its saved original definition (the journal's plan.original_programs entry),
    // using the same restore routine the guided recovery uses.
    let saved_f = p.original_programs.iter().find(|x| x.kind == "FUNCTION" && x.name == "f").unwrap().clone();
    let mut restore_note = json!({});
    conn.query_drop(format!("USE {}", q(&original.database))).unwrap();
    crate::mysql_schema_programs::restore_schema_program(&mut conn, &original.database, &saved_f, &mut restore_note).unwrap();
    reset_session(&mut conn).unwrap();
    let f_now: Option<i64> = conn.query_first(format!("SELECT {}()", qualified(&original.database, "f"))).unwrap();
    // Control: with intact original ids the recreated f is recognised.
    let control = match inspect_rollback(&original, &p, &applied).unwrap() { RollbackInspection::Programs(m) => format!("Programs({m:?})"), RollbackInspection::OriginalIdsChanged => "OriginalIdsChanged".into(), RollbackInspection::ProgramsNotVisible => "ProgramsNotVisible".into() };
    eprintln!("RVI0 control inspect_rollback (ids intact, f restored)={control} restore_note={restore_note}");
    let list_fixed = rvf_backups(&input, "rvi0", &original, "list", json!({}));
    let id_before = rvh_table_id(&mut conn, &original.database, "parent");
    let optimize: Vec<mysql::Row> = conn.query(format!("OPTIMIZE TABLE {}", qualified(&original.database, "parent"))).unwrap();
    let id_after = rvh_table_id(&mut conn, &original.database, "parent");
    let direct = inspect_rollback(&original, &p, &applied).unwrap();
    let direct_name = match direct { RollbackInspection::OriginalIdsChanged => "OriginalIdsChanged", RollbackInspection::ProgramsNotVisible => "ProgramsNotVisible", RollbackInspection::Programs(_) => "Programs" };
    let list1 = rvf_backups(&input, "rvi0", &original, "list", json!({}));
    let rc1 = rvf_backups(&input, "rvi0", &original, "reconcile", json!({}));
    let cd1 = rvf_backups(&input, "rvi0", &original, "cleanup_plan", json!({"target":"displaced"}));
    let cb1 = rvf_backups(&input, "rvi0", &original, "cleanup_plan", json!({"target":"backup"}));
    rvi_save("rvi0.json", &json!({"applied":applied,"list0":list0,"f_before_fix":format!("{f_before:?}"),"f_now":f_now,"list_fixed":list_fixed,
        "id_before":id_before,"id_after":id_after,"optimize_rows":optimize.len(),"inspect_rollback":direct_name,"list1":list1,"rc1":rc1,"cd1":cd1,"cb1":cb1}));
    let _ = applied["displaced_backup"].as_str().map(|d| conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(d))));
    cleanup(&mut conn, &p, &dir);
    let (e0, ef, e1) = (&list0["backups"][0]["backup"], &list_fixed["backups"][0]["backup"], &list1["backups"][0]["backup"]);
    eprintln!("RVI0 applied.status={} pending={} missing={} error={}\nf before fix={f_before:?} f() now={f_now:?}\nlist0: verdict={} missing={} pending={} note={}\nlist_fixed (ids intact): verdict={} missing={} pending={} note={}\nparent TABLE_ID {id_before:?} -> {id_after:?} (OPTIMIZE rows={}) inspect_rollback={direct_name}\nlist1 (after OPTIMIZE): verdict={} missing={} pending={} note={} keys={:?}\nreconcile: conclusion={} message={} evidence.note={}\ncleanup displaced: can_cleanup={} blockers={}\ncleanup backup: can_cleanup={} blockers={}",
        applied["status"], applied["pending_program"], applied["missing_programs"], applied["error"],
        e0["verdict"], e0["missing_programs"], e0["pending_program"], e0["note"],
        ef["verdict"], ef["missing_programs"], ef["pending_program"], ef["note"], optimize.len(),
        e1["verdict"], e1["missing_programs"], e1["pending_program"], e1["note"], e1.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()),
        rc1["conclusion"], rc1["message"], rc1["evidence"]["note"], cd1["can_cleanup"], cd1["blockers"], cb1["can_cleanup"], cb1["blockers"]);
    assert_eq!(applied["status"], "rollback_incomplete", "{applied:#}");
    assert_eq!(f_now, Some(1));
    assert_eq!(ef["verdict"], "rolled_back", "control: recreated f must be recognised while ids are intact {ef:#}");
    assert_ne!(id_before, id_after);
    assert_eq!(direct_name, "OriginalIdsChanged");
    // EXPECTED (claim): the stale recorded state must come with a reason.
    assert_eq!(e1["verdict"],"rollback_incomplete");
    assert_eq!(e1["missing_programs"],applied["missing_programs"]);
    assert_eq!(e1["pending_program"],applied["pending_program"]);
    assert_eq!(e1["note"],ROLLBACK_IDS_CHANGED_NOTE,"{e1:#}");
    assert_eq!(rc1["evidence"]["note"],ROLLBACK_IDS_CHANGED_NOTE,"{rc1:#}");
    for blocked in [&cd1,&cb1] {
        assert_eq!(blocked["can_cleanup"],false);
        assert!(blocked["blockers"].as_array().unwrap().contains(&json!(ROLLBACK_IDS_CHANGED_NOTE)),"{blocked:#}");
    }
}

/// claim [3]: rollback_apply, RENAME refused by the server (1050) and the locked trigger recovery fails with a
/// server error (1142) on a LIVE connection -> no re-lock / second recovery on a new connection.
/// Counted server-side: performance_schema per-user statement summary (LOCK TABLES, CREATE TRIGGER).
#[test]
fn safe_promote_rvi3_rollback_live_server_recovery_error_does_not_relock() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let user = format!("rvi3_{}", std::process::id());
    struct DropUser(Endpoint, String);
    impl Drop for DropUser { fn drop(&mut self) { if let Ok(mut db) = connect(&self.0) { let _ = db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'", self.1)); } } }
    let _u = DropUser(Endpoint { database: "tf_test".into(), ..original.clone() }, user.clone());
    for sql in [format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT ALL ON *.* TO '{user}'@'%'"), format!("REVOKE TRIGGER ON *.* FROM '{user}'@'%'"),
        format!("GRANT TRIGGER ON {}.* TO '{user}'@'%'", q(&original.database))] { conn.query_drop(sql).unwrap(); }
    let rb = Endpoint { user: user.clone(), password: "tf_local_test".into(), ..original.clone() };
    let p = plan(&original, &candidate, "rvi3", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted", "{result:#}");
    let input = rvf_journal(&dir, "rvi3", &original, &candidate, &p, &result);
    let displaced = rvf_displaced("rvi3", &p);
    let journal_dir = rvf_rollback_dir(&input, "rvi3", &displaced);
    let before = rvf_triggers(&mut conn, &original.database);
    let count = |db: &mut Db, event: &str| -> (u64, u64) {
        db.exec_first("SELECT COUNT_STAR, SUM_ERRORS FROM performance_schema.events_statements_summary_by_user_by_event_name WHERE USER=? AND EVENT_NAME=?",
            (&user, event)).unwrap().unwrap_or((0, 0))
    };
    let lock0 = count(&mut conn, "statement/sql/lock_tables");
    let ct0 = count(&mut conn, "statement/sql/create_trigger");
    let mut recovery_attempts=Vec::new();
    let (ep, u, odb) = (original.clone(), user.clone(), original.database.clone());
    let disp = displaced.clone();
    let recovered = rollback_apply_with_hook(&rb, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &journal_dir, |phase, db| {
        if phase == "before_rename" {
            let mut admin = connect(&ep)?;
            admin.query_drop(format!("CREATE TABLE {} (x INT)", qualified(&disp, "parent"))).map_err(|e| e.to_string())?;
            admin.query_drop(format!("REVOKE TRIGGER ON {}.* FROM '{u}'@'%'", q(&odb))).map_err(|e| e.to_string())?;
        }
        if phase=="before_original_trigger_recovery" {
            recovery_attempts.push(db.query_first::<u64,_>("SELECT CONNECTION_ID()").map_err(|e|e.to_string())?.unwrap());
        }
        Ok(())
    }).unwrap();
    let lock1 = count(&mut conn, "statement/sql/lock_tables");
    let ct1 = count(&mut conn, "statement/sql/create_trigger");
    let after = rvf_triggers(&mut conn, &original.database);
    rvi_save("rvi3.json", &json!({"recovered":recovered,"lock_tables":[lock0,lock1],"create_trigger":[ct0,ct1],"triggers_before":before,"triggers_after":after}));
    // Remove the owned test namespaces before checking the failed-recovery evidence.
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVI3 status={} error={}\ntrigger_recovery_error={}\ntrigger_recovery_drop_errors={}\nLOCK TABLES by {user}: {lock0:?} -> {lock1:?}\nCREATE TRIGGER by {user}: {ct0:?} -> {ct1:?}\ntriggers before={before:?} after={after:?}",
        recovered["status"], recovered["error"], recovered["trigger_recovery_error"], recovered["trigger_recovery_drop_errors"]);
    assert!(recovered["error"].as_str().unwrap_or("").contains("1050"), "{recovered:#}");
    assert!(recovered["trigger_recovery_error"].as_str().unwrap_or("").contains("1142"), "{recovered:#}");
    assert_eq!(recovery_attempts.len(),1,"a live server error must use exactly one recovery attempt: {recovery_attempts:?}");
    assert_eq!(ct1.0-ct0.0,1,"only the locked recovery may attempt CREATE TRIGGER");
    assert_eq!(lock1.0 - lock0.0, 1, "a server-side recovery error on a live connection must not cause a re-lock and second recovery: {recovered:#}");
}
