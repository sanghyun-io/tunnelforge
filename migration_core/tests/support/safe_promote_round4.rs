// ---------------------------------------------------------------- round-4 live reproductions (rvh)
fn rvh_save(name: &str, value: &Value) {
    if let Ok(dir) = std::env::var("RVH_EVIDENCE") {
        let _ = std::fs::create_dir_all(&dir);
        std::fs::write(std::path::Path::new(&dir).join(name), serde_json::to_vec_pretty(value).unwrap()).unwrap();
    }
}
fn rvh_table_id(db: &mut Db, database: &str, table: &str) -> Option<u64> {
    db.exec_first("SELECT TABLE_ID FROM information_schema.INNODB_TABLES WHERE NAME=?", (format!("{database}/{table}"),)).unwrap()
}

/// claim [0]: rolled_back, then the app rebuilds an original table (ALTER TABLE ... FORCE).
#[test]
fn safe_promote_rvh0_rolled_back_then_rebuilt_original() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvh0", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted", "{r:#}");
    let input = rvf_journal(&dir, "rvh0", &original, &candidate, &p, &r);
    let review = rvf_backups(&input, "rvh0", &original, "rollback_plan", json!({}));
    assert_eq!(review["can_rollback"], true, "{review:#}");
    let applied = rvf_backups(&input, "rvh0", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    let list0 = rvf_backups(&input, "rvh0", &original, "list", json!({}));
    let cd0 = rvf_backups(&input, "rvh0", &original, "cleanup_plan", json!({"target":"displaced"}));
    let cb0 = rvf_backups(&input, "rvh0", &original, "cleanup_plan", json!({"target":"backup"}));
    let id_before = rvh_table_id(&mut conn, &original.database, "parent");
    conn.query_drop(format!("ALTER TABLE {} FORCE", qualified(&original.database, "parent"))).unwrap();
    let id_after = rvh_table_id(&mut conn, &original.database, "parent");
    let list1 = rvf_backups(&input, "rvh0", &original, "list", json!({}));
    let rc1 = rvf_backups(&input, "rvh0", &original, "reconcile", json!({}));
    let cd1 = rvf_backups(&input, "rvh0", &original, "cleanup_plan", json!({"target":"displaced"}));
    let cb1 = rvf_backups(&input, "rvh0", &original, "cleanup_plan", json!({"target":"backup"}));
    rvh_save("rvh0.json", &json!({"applied":applied,"list0":list0,"cd0":cd0,"cb0":cb0,"id_before":id_before,"id_after":id_after,"list1":list1,"rc1":rc1,"cd1":cd1,"cb1":cb1}));
    let _ = applied["displaced_backup"].as_str().map(|d| conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(d))));
    cleanup(&mut conn, &p, &dir);
    let e0 = &list0["backups"][0]; let e1 = &list1["backups"][0];
    eprintln!("RVH0 applied.status={} recorded_restored={}\nBEFORE rebuild: list verdict={} displaced?={} cd0.event={} cd0.can_cleanup={} cd0.blockers={} cb0.can_cleanup={} cb0.blockers={}\nparent TABLE_ID {id_before:?} -> {id_after:?}\nAFTER rebuild: list verdict={} displaced?={} keys={:?}\nreconcile conclusion={} message={}\ncd1={}\ncb1.event={} cb1.can_cleanup={} cb1.blockers={}",
        applied["status"], applied["restored_tables"], e0["backup"]["verdict"], e0.get("displaced").is_some(), cd0["event"], cd0["can_cleanup"], cd0["blockers"], cb0["can_cleanup"], cb0["blockers"],
        e1["backup"]["verdict"], e1.get("displaced").is_some(), e1.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()),
        rc1["conclusion"], rc1["message"], cd1, cb1["event"], cb1["can_cleanup"], cb1["blockers"]);
    // A completed recovery remains authoritative after application maintenance.
    assert_eq!(applied["status"], "rolled_back");
    assert_eq!(cd0["can_cleanup"], true, "{cd0:#}");
    assert_eq!(cb0["can_cleanup"], true, "{cb0:#}");
    assert_ne!(id_before, id_after);
    assert!(e1.get("displaced").is_some(), "{e1:#}");
    assert_eq!(cd1["event"], "result", "{cd1:#}");
    assert_eq!(cd1["can_cleanup"],true);
    assert_eq!(e1["backup"]["verdict"],"rolled_back");
    assert!(!list1["unproven_namespaces"].as_array().unwrap().iter().any(|n|n["namespace"]==applied["displaced_backup"]));
    assert_eq!(cb1["can_cleanup"], true, "{cb1:#}");
    assert_eq!(cb1["blockers"],json!([]));
}

/// claim [1]: rollback_incomplete, then an unsavable (latin1) trigger appears on parent.
#[test]
fn safe_promote_rvh1_rollback_incomplete_keeps_its_row_and_blocks_cleanup_on_inspection_error() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvh1", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted", "{r:#}");
    let input = rvf_journal(&dir, "rvh1", &original, &candidate, &p, &r);
    let review = rvf_backups(&input, "rvh1", &original, "rollback_plan", json!({}));
    assert_eq!(review["can_rollback"], true, "{review:#}");
    let mut app = connect(&original).unwrap();
    app.query_drop("START TRANSACTION").unwrap();
    let _: Option<i64> = app.query_first(format!("SELECT {}()", qualified(&original.database, "f"))).unwrap();
    let applied = rvf_backups(&input, "rvh1", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    app.query_drop("ROLLBACK").unwrap();
    drop(app);
    let list0 = rvf_backups(&input, "rvh1", &original, "list", json!({}));
    conn.query_drop(format!("USE {}", q(&original.database))).unwrap();
    conn.query_drop("SET SESSION character_set_client=latin1, collation_connection=latin1_swedish_ci").unwrap();
    conn.query_drop("CREATE TRIGGER app_note BEFORE INSERT ON parent FOR EACH ROW SET NEW.note=CONCAT(NEW.note,'é')").unwrap();
    reset_session(&mut conn).unwrap();
    let list1 = rvf_backups(&input, "rvh1", &original, "list", json!({}));
    let rc1 = rvf_backups(&input, "rvh1", &original, "reconcile", json!({}));
    let cb1 = rvf_backups(&input, "rvh1", &original, "cleanup_plan", json!({"target":"backup"}));
    let cd1 = rvf_backups(&input, "rvh1", &original, "cleanup_plan", json!({"target":"displaced"}));
    rvh_save("rvh1.json", &json!({"applied":applied,"list0":list0,"list1":list1,"rc1":rc1,"cb1":cb1,"cd1":cd1}));
    let _ = applied["displaced_backup"].as_str().map(|d| conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(d))));
    cleanup(&mut conn, &p, &dir);
    let e0 = &list0["backups"][0]; let e1 = &list1["backups"][0];
    eprintln!("RVH1 applied.status={} pending={} missing={}\nlist0 keys={:?} verdict={} missing={} pending={}\nlist1 keys={:?} verdict={} inspection_error={} backup={}\nreconcile conclusion={} evidence.verdict={} evidence.program_verification_error={}\ncleanup_plan backup={}\ncleanup_plan displaced={}",
        applied["status"], applied["pending_program"], applied["missing_programs"],
        e0.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()), e0["backup"]["verdict"], e0["backup"]["missing_programs"], e0["backup"]["pending_program"],
        e1.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()), e1["backup"]["verdict"], e1["inspection_error"], e1["backup"],
        rc1["conclusion"], rc1["evidence"]["verdict"], rc1["evidence"]["program_verification_error"], cb1, cd1);
    assert_eq!(applied["status"], "rollback_incomplete", "{applied:#}");
    assert_eq!(e0["backup"]["verdict"], "rollback_incomplete");
    // inspect_backup alone keeps the row (reconcile proves it)...
    assert_eq!(rc1["evidence"]["verdict"], "rollback_incomplete", "{rc1:#}");
    assert!(rc1["evidence"]["program_verification_error"].as_str().unwrap_or("").contains("cannot be saved"), "{rc1:#}");
    assert!(e1.get("inspection_error").is_none(),"{e1:#}");
    assert_eq!(e1["backup"]["verdict"],"rollback_incomplete");
    assert_eq!(e1["backup"]["missing_programs"],applied["missing_programs"]);
    assert_eq!(e1["backup"]["pending_program"],applied["pending_program"]);
    assert!(e1.get("rollbacks").is_some() && e1.get("candidate").is_some() && e1.get("clone").is_some(),"{e1:#}");
    for response in [&cb1,&cd1] {
        assert_eq!(response["event"],"result","{response:#}");
        assert_eq!(response["can_cleanup"],false);
        assert!(response["blockers"].to_string().contains("cannot be saved"),"{response:#}");
    }
}

/// claim [2]: rollback_incomplete with a leftover dump event; an account without EVENT on the schema
/// cannot see it, so the incomplete record and protected displaced backup must be retained.
#[test]
fn safe_promote_rvh2_hidden_leftover_event_keeps_recorded_incomplete_state() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir)) = fixture() else { return };
    for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
        conn.query_drop(format!("DROP VIEW {}", qualified(db, view))).unwrap();
    }
    conn.query_drop(format!("CREATE EVENT {} ON SCHEDULE EVERY 1 HOUR STARTS '2099-01-01' ENABLE DO SET @tf_rvh=1", qualified(&candidate.database, "e"))).unwrap();
    let programs = capture_programs(&mut conn, &candidate.database).unwrap();
    for x in &programs { conn.query_drop(format!("DROP {} {}", x.kind, qualified(&candidate.database, &x.name))).unwrap(); }
    assert!(capture_programs(&mut conn, &original.database).unwrap().is_empty());
    let p = plan(&original, &candidate, "rvh2", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted", "{r:#}");
    let input = rvf_journal(&dir, "rvh2", &original, &candidate, &p, &r);
    let review = rvf_backups(&input, "rvh2", &original, "rollback_plan", json!({}));
    assert_eq!(review["can_rollback"], true, "{review:#}");
    // An application transaction holds the event's MDL (SHOW CREATE EVENT): DROP EVENT e fails with 1205.
    let mut app = connect(&original).unwrap();
    app.query_drop("START TRANSACTION").unwrap();
    let _: Vec<mysql::Row> = app.query(format!("SHOW CREATE EVENT {}", qualified(&original.database, "e"))).unwrap();
    let applied = rvf_backups(&input, "rvh2", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    app.query_drop("ROLLBACK").unwrap();
    drop(app);
    let user = format!("rvh2_{}", std::process::id());
    struct DropUser(Endpoint, String);
    impl Drop for DropUser { fn drop(&mut self) { if let Ok(mut db) = connect(&self.0) { let _ = db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'", self.1)); } } }
    let _u = DropUser(Endpoint { database: "tf_test".into(), ..original.clone() }, user.clone());
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),
        format!("GRANT DROP ON `tf\\_backup\\_rb\\_%`.* TO '{user}'@'%'"),
    ] { conn.query_drop(sql).unwrap(); }
    let other = Endpoint { user: user.clone(), password: "tf_local_test".into(), ..original.clone() };
    let grants: Vec<String> = connect(&other).unwrap().query("SHOW GRANTS").unwrap();
    let seen_by_other: u64 = connect(&other).unwrap().exec_first("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?", (&original.database,)).unwrap().unwrap();
    let seen_by_root: u64 = conn.exec_first("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?", (&original.database,)).unwrap().unwrap();
    let list_root = rvf_backups(&input, "rvh2", &original, "list", json!({}));
    let list_other = rvf_backups(&input, "rvh2", &other, "list", json!({}));
    let rc_other = rvf_backups(&input, "rvh2", &other, "reconcile", json!({}));
    let cd_root = rvf_backups(&input, "rvh2", &original, "cleanup_plan", json!({"target":"displaced"}));
    let cd_other = rvf_backups(&input, "rvh2", &other, "cleanup_plan", json!({"target":"displaced"}));
    let ca_other = rvf_backups(&input, "rvh2", &other, "cleanup_apply", json!({"target":"displaced","confirmed":true,"plan_digest":cd_other["plan_digest"]}));
    let displaced = applied["displaced_backup"].as_str().unwrap_or("").to_string();
    let displaced_exists: u64 = conn.exec_first("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=?", (&displaced,)).unwrap().unwrap();
    let event_after: Option<(String, String)> = conn.exec_first("SELECT EVENT_NAME, STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=?", (&original.database,)).unwrap();
    rvh_save("rvh2.json", &json!({"applied":applied,"grants":grants,"seen_by_other":seen_by_other,"seen_by_root":seen_by_root,"list_root":list_root,"list_other":list_other,"rc_other":rc_other,"cd_root":cd_root,"cd_other":cd_other,"ca_other":ca_other,"displaced_exists_after":displaced_exists,"event_after":event_after}));
    conn.query_drop(format!("DROP EVENT {}",qualified(&original.database,"e"))).unwrap();
    let still_unproven=rvf_backups(&input,"rvh2",&other,"list",json!({}));
    assert_eq!(still_unproven["backups"][0]["backup"]["verdict"],"rollback_incomplete");
    conn.query_drop(format!("GRANT EVENT ON {}.* TO '{user}'@'%'",q(&original.database))).unwrap();
    let now_proven=rvf_backups(&input,"rvh2",&other,"list",json!({}));
    assert_eq!(now_proven["backups"][0]["backup"]["verdict"],"rolled_back","{now_proven:#}");
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    let br = &list_root["backups"][0]["backup"]; let bo = &list_other["backups"][0]["backup"];
    eprintln!("RVH2 applied.status={} pending={} missing={} error={}\nother grants={grants:?}\nI_S.EVENTS rows root={seen_by_root} other={seen_by_other}\nlist(root) verdict={} missing={} pending={}\nlist(other) verdict={} missing={} pending={} displaced?={}\nreconcile(other) conclusion={} message={}\ncleanup_plan displaced root: can_cleanup={} blockers={}\ncleanup_plan displaced other: can_cleanup={} blockers={}\ncleanup_apply other: {}\ndisplaced exists after={displaced_exists} event in original after={event_after:?}",
        applied["status"], applied["pending_program"], applied["missing_programs"], applied["error"],
        br["verdict"], br["missing_programs"], br["pending_program"], bo["verdict"], bo["missing_programs"], bo["pending_program"], list_other["backups"][0].get("displaced").is_some(),
        rc_other["conclusion"], rc_other["message"], cd_root["can_cleanup"], cd_root["blockers"], cd_other["can_cleanup"], cd_other["blockers"], ca_other);
    assert_eq!(applied["status"], "rollback_incomplete", "{applied:#}");
    assert_eq!(applied["missing_programs"], json!(["unexpected:event:e"]));
    assert_eq!(seen_by_root, 1);
    assert_eq!(seen_by_other, 0);
    assert_eq!(br["verdict"], "rollback_incomplete");
    assert_eq!(bo["verdict"], "rollback_incomplete", "{bo:#}");
    assert_eq!(bo["pending_program"],applied["pending_program"]);
    assert_eq!(bo["missing_programs"],applied["missing_programs"]);
    assert!(bo["note"].as_str().unwrap().contains("cannot see all stored programs"));
    assert_eq!(rc_other["conclusion"], "rollback_incomplete");
    assert_eq!(cd_other["can_cleanup"], false, "{cd_other:#}");
    assert_eq!(ca_other["event"],"error","{ca_other:#}");
    assert_eq!(displaced_exists,1);
    assert_eq!(event_after.map(|e| e.0), Some("e".to_string()));
}

/// claim [4] (C): busy table -> LOCK TABLES WRITE times out (2 s) -> failed_original_unchanged.
#[test]
fn safe_promote_rvh4c_busy_lock_failure_result_shape() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvh4c", &programs).unwrap();
    let mut app: Option<Db> = None;
    let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _db| {
        if phase == "before_lock" {
            let mut a = connect(&original)?;
            a.query_drop("START TRANSACTION").map_err(|e| e.to_string())?;
            let _: Option<u64> = a.query_first(format!("SELECT COUNT(*) FROM {}", qualified(&original.database, "parent"))).map_err(|e| e.to_string())?;
            app = Some(a);
        }
        Ok(())
    }).unwrap();
    if let Some(mut a) = app.take() { let _ = a.query_drop("ROLLBACK"); }
    rvh_save("rvh4c.json", &result);
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVH4C status={} original_unchanged={} error_code={}\nmessage={}\nerror={}", result["status"], result["original_unchanged"], result["error_code"], result["message"], result["error"]);
    assert_eq!(result["status"], "failed_original_unchanged", "{result:#}");
    assert!(result["error"].as_str().unwrap().starts_with("safe_promotion: bounded WRITE lock acquisition"), "{result:#}");
}

/// claim [4] (B): connection lost before the RENAME reply, ids intact, the fresh-connection relock times out
/// -> failed_original_triggers_missing.
#[test]
fn safe_promote_rvh4b_lost_rename_failed_relock_result_shape() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvh4b", &programs).unwrap();
    let mut app: Option<Db> = None;
    let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, db| {
        if phase == "before_rename" {
            let id: u64 = db.query_first("SELECT CONNECTION_ID()").map_err(|e| e.to_string())?.unwrap();
            connect(&original)?.query_drop(format!("KILL {id}")).map_err(|e| e.to_string())?;
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
        if phase == "before_original_trigger_recovery" {
            let mut a = connect(&original)?;
            a.query_drop("START TRANSACTION").map_err(|e| e.to_string())?;
            let _: Option<u64> = a.query_first(format!("SELECT COUNT(*) FROM {}", qualified(&original.database, "parent"))).map_err(|e| e.to_string())?;
            app = Some(a);
        }
        Ok(())
    }).unwrap();
    if let Some(mut a) = app.take() { let _ = a.query_drop("ROLLBACK"); }
    let triggers = rvf_triggers(&mut conn, &original.database);
    rvh_save("rvh4b.json", &result);
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVH4B status={} original_unchanged={} error_code={}\nmessage={}\nerror={}\ntrigger_recovery_error={}\ntriggers now={triggers:?}", result["status"], result["original_unchanged"], result["error_code"], result["message"], result["error"], result["trigger_recovery_error"]);
    assert_eq!(result["status"], "failed_original_triggers_missing", "{result:#}");
    assert!(result["error"].as_str().unwrap().starts_with("safe_promotion: atomic rename"), "{result:#}");
    assert!(!triggers.contains(&"parent.guard".to_string()));
}

/// claim [4] (A): connection lost before the RENAME reply and the cleanup connection fails -> cutover_unknown.
#[test]
fn safe_promote_rvh4a_lost_rename_no_cleanup_connection_result_shape() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((root, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let user = format!("rvh4a_{}", std::process::id());
    struct DropUser(Endpoint, String);
    impl Drop for DropUser { fn drop(&mut self) { if let Ok(mut db) = connect(&self.0) { let _ = db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'", self.1)); } } }
    let _u = DropUser(Endpoint { database: "tf_test".into(), ..root.clone() }, user.clone());
    conn.query_drop(format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    conn.query_drop(format!("GRANT ALL PRIVILEGES ON *.* TO '{user}'@'%'")).unwrap();
    let as_user = |e: &Endpoint| Endpoint { user: user.clone(), password: "tf_local_test".into(), ..e.clone() };
    let (o, c) = (as_user(&root), as_user(&candidate));
    let p = plan(&o, &c, "rvh4a", &programs).unwrap();
    let result = promote_with_hook(&o, &c, &p, &p.digest, &dir, |phase, db| {
        if phase == "before_rename" {
            let id: u64 = db.query_first("SELECT CONNECTION_ID()").map_err(|e| e.to_string())?.unwrap();
            let mut admin = connect(&root)?;
            admin.query_drop(format!("ALTER USER '{user}'@'%' ACCOUNT LOCK")).map_err(|e| e.to_string())?;
            admin.query_drop(format!("KILL {id}")).map_err(|e| e.to_string())?;
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
        Ok(())
    }).unwrap();
    conn.query_drop(format!("ALTER USER '{user}'@'%' ACCOUNT UNLOCK")).unwrap();
    rvh_save("rvh4a.json", &result);
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVH4A status={} original_unchanged={} error_code={}\nmessage={}\nerror={}", result["status"], result["original_unchanged"], result["error_code"], result["message"], result["error"]);
    assert_eq!(result["status"], "cutover_unknown", "{result:#}");
    assert!(result["error"].as_str().unwrap().starts_with("safe_promotion: atomic rename"), "{result:#}");
}

/// claim [5]: rollback preview blocker for a function whose name contains ':'.
#[test]
fn safe_promote_rvh5_colon_function_name_rollback_blocker() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir)) = fixture() else { return };
    for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
        conn.query_drop(format!("DROP VIEW {}", qualified(db, view))).unwrap();
    }
    for (db, body) in [(&original.database, "DETERMINISTIC RETURN 1"), (&candidate.database, "DETERMINISTIC RETURN 2")] {
        conn.query_drop(format!("CREATE FUNCTION {}() RETURNS INT {body}", qualified(db, "a:b"))).unwrap();
    }
    let programs = capture_programs(&mut conn, &candidate.database).unwrap();
    for x in &programs { conn.query_drop(format!("DROP {} {}", x.kind, qualified(&candidate.database, &x.name))).unwrap(); }
    let trust: i64 = conn.query_first("SELECT @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap();
    struct Restore { endpoint: Endpoint, trust: i64, user: String }
    impl Drop for Restore { fn drop(&mut self) { if let Ok(mut db) = connect(&self.endpoint) {
        let _ = db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}", self.trust));
        let _ = db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'", self.user));
    } } }
    let user = format!("rvh5_rb_{}", std::process::id());
    let _restore = Restore { endpoint: Endpoint { database: "tf_test".into(), ..original.clone() }, trust, user: user.clone() };
    conn.query_drop("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
    let p = plan(&original, &candidate, "rvh5", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted", "{result:#}");
    let body_now: Option<i64> = conn.query_first(format!("SELECT {}()", qualified(&original.database, "a:b"))).unwrap();
    conn.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    let input = rvf_journal(&dir, "rvh5", &original, &candidate, &p, &result);
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_%`.* TO '{user}'@'%'"),
    ] { conn.query_drop(sql).unwrap(); }
    let rb = Endpoint { user: user.clone(), password: "tf_local_test".into(), ..original.clone() };
    let reviewed = rvf_backups(&input, "rvh5", &rb, "rollback_plan", json!({}));
    let trust_now: i64 = conn.query_first("SELECT @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap();
    rvh_save("rvh5_rollback_plan.json", &reviewed);
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVH5 programs={:?} a:b() after promote={body_now:?} trust(now)={trust_now}\nreviewed can_rollback={} blockers={}", programs.iter().map(|x| (&x.kind, &x.name)).collect::<Vec<_>>(), reviewed["can_rollback"], reviewed["blockers"]);
    assert_eq!(body_now, Some(2));
    assert_eq!(reviewed["can_rollback"], false, "{reviewed:#}");
    assert!(reviewed["blockers"].as_array().unwrap().iter().any(|b| b.as_str().unwrap().starts_with("rollback_routine_binlog_unsafe: a:b: ")), "{reviewed:#}");
}

#[test]
fn safe_promote_rvh_live_server_recovery_error_does_not_retry_on_a_new_connection() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else {return};
    let user=format!("rvh_live_{}",std::process::id());
    struct DropUser(Endpoint,String);
    impl Drop for DropUser {fn drop(&mut self) {if let Ok(mut db)=connect(&self.0) {let _=db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'",self.1));}}}
    let _user=DropUser(Endpoint{database:"tf_test".into(),..original.clone()},user.clone());
    for sql in [format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT ALL ON *.* TO '{user}'@'%'"),format!("REVOKE TRIGGER ON *.* FROM '{user}'@'%'"),
        format!("GRANT TRIGGER ON {}.* TO '{user}'@'%'",q(&original.database))] {conn.query_drop(sql).unwrap();}
    let as_user=|e:&Endpoint|Endpoint{user:user.clone(),password:"tf_local_test".into(),..e.clone()};
    let (o,c)=(as_user(&original),as_user(&candidate));
    let p=plan(&o,&c,"rvh_live",&programs).unwrap();
    let mut attempts=Vec::new();
    let result=promote_with_hook(&o,&c,&p,&p.digest,&dir,|phase,db| {
        if phase=="before_rename" {
            let mut admin=connect(&original)?;
            admin.query_drop(format!("CREATE TABLE {}(x INT)",qualified(&p.backup_database,"parent"))).map_err(|e|e.to_string())?;
            admin.query_drop(format!("REVOKE TRIGGER ON {}.* FROM '{user}'@'%'",q(&original.database))).map_err(|e|e.to_string())?;
        }
        if phase=="before_original_trigger_recovery" {
            attempts.push(db.query_first::<u64,_>("SELECT CONNECTION_ID()").map_err(|e|e.to_string())?.unwrap());
        }
        Ok(())
    }).unwrap();
    cleanup(&mut conn,&p,&dir);
    assert_eq!(attempts.len(),1,"a server-side recovery error on a live connection must not cause a fresh recovery: {attempts:?}, {result:#}");
    assert!(result["trigger_recovery_error"].as_str().unwrap().contains("1142"),"{result:#}");
    assert_eq!(result["status"],"failed_original_triggers_missing");
}

#[test]
fn safe_promote_rvh_rechecking_incomplete_recovery_proves_each_program_kind_visible() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else {return};
    let p=plan(&original,&candidate,"rvh_visibility",&programs).unwrap();
    let user=format!("rvh_vis_{}",std::process::id());
    struct DropUser(Endpoint,String);
    impl Drop for DropUser {fn drop(&mut self) {if let Ok(mut db)=connect(&self.0) {let _=db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'",self.1));}}}
    let _user=DropUser(Endpoint{database:"tf_test".into(),..original.clone()},user.clone());
    for sql in [format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT PROCESS,SHOW VIEW ON *.* TO '{user}'@'%'"),
        format!("GRANT SELECT ON {}.* TO '{user}'@'%'",q(&original.database))] {conn.query_drop(sql).unwrap();}
    let viewer=Endpoint{user:user.clone(),password:"tf_local_test".into(),..original.clone()};
    let mut record_only=p.clone();record_only.original_programs.clear();record_only.promoted_programs.clear();
    for record in [json!({"missing_programs":["unexpected:event:gone"]}),
        json!({"missing_programs":["trigger:parent:gone"]}),json!({"pending_program":{"kind":"FUNCTION","name":"gone"}})] {
        assert!(matches!(inspect_rollback(&viewer,&record_only,&record).unwrap(),RollbackInspection::ProgramsNotVisible));
    }
    for privilege in ["EVENT","TRIGGER"] {
        assert!(matches!(inspect_rollback(&viewer,&p,&json!({})).unwrap(),RollbackInspection::ProgramsNotVisible));
        conn.query_drop(format!("GRANT {privilege} ON {}.* TO '{user}'@'%'",q(&original.database))).unwrap();
    }
    assert!(matches!(inspect_rollback(&viewer,&p,&json!({})).unwrap(),RollbackInspection::ProgramsNotVisible));
    conn.query_drop(format!("GRANT SHOW_ROUTINE ON *.* TO '{user}'@'%'")).unwrap();
    assert!(matches!(inspect_rollback(&viewer,&p,&json!({})).unwrap(),RollbackInspection::Programs(missing) if missing.is_empty()));
    conn.query_drop(format!("REVOKE SHOW_ROUTINE ON *.* FROM '{user}'@'%'")).unwrap();
    conn.query_drop(format!("GRANT SELECT ON *.* TO '{user}'@'%'")).unwrap();
    assert!(matches!(inspect_rollback(&viewer,&p,&json!({})).unwrap(),RollbackInspection::Programs(missing) if missing.is_empty()));
    cleanup(&mut conn,&p,&dir);
}
