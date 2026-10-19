fn rvf_displaced(restore_id: &str, plan: &PromotionPlan) -> String {
    let digest = hex::encode(Sha256::digest(format!("{restore_id}|{}", plan.backup_database).as_bytes()));
    format!("tf_backup_rb_{}", &digest[..16])
}
fn rvf_journal(dir: &Path, id: &str, original: &Endpoint, candidate: &Endpoint, p: &PromotionPlan, result: &Value) -> std::path::PathBuf {
    let input = dir.join("input");
    let attempt = input.join(format!(".tunnelforge_promotion_plan_{id}")).join("attempt");
    std::fs::create_dir_all(&attempt).unwrap();
    let stored = json!({"restore_id":id,"restore_digest":"x","original_target":crate::safe_promotion::public_target(original),
        "candidate_target":crate::safe_promotion::public_target(candidate),
        "engine_plan":{"engine":"mysql","plan":serde_json::to_value(p).unwrap()},"attempt_nonce":p.nonce});
    write_dump_import_report(&attempt, &json!({"status":result["status"],"original_unchanged":result["original_unchanged"],"plan_digest":"x","stored_plan":stored,"result":result})).unwrap();
    input
}
fn rvf_rollback_dir(input: &Path, id: &str, displaced: &str) -> std::path::PathBuf {
    let dir = input.join(format!(".tunnelforge_promotion_plan_{id}")).join(format!("rollback-{displaced}"));
    std::fs::create_dir(&dir).unwrap(); dir
}
fn rvf_backups(input: &Path, id: &str, endpoint: &Endpoint, action: &str, extra: Value) -> Value {
    let mut payload = json!({"action":action,"restore_id":id,"input_dirs":[input.to_string_lossy()],"connection":serde_json::to_value(endpoint).unwrap()});
    payload.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
    let mut out = Value::Null;
    crate::safe_backups::handle(&Request { command: "restore.backups".into(), request_id: None, payload }, |v| out = v);
    out
}
fn rvf_triggers(db: &mut Db, database: &str) -> Vec<String> {
    db.exec("SELECT CONCAT(EVENT_OBJECT_TABLE,'.',TRIGGER_NAME) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? ORDER BY 1", (database,)).unwrap()
}
#[test]
fn safe_promote_rvf_a0_rollback_connection_lost_after_detach_recovers_on_fresh_connection() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvfa0", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted");
    let input = rvf_journal(&dir, "rvfa0", &original, &candidate, &p, &result);
    let displaced = rvf_displaced("rvfa0", &p);
    let journal_dir = rvf_rollback_dir(&input, "rvfa0", &displaced);
    let before = rvf_triggers(&mut conn, &original.database);
    let recovered = rollback_apply_with_hook(&original, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &journal_dir, |phase, db| {
        if phase == "before_rename" {
            let id: u64 = db.query_first("SELECT CONNECTION_ID()").map_err(|e| e.to_string())?.unwrap();
            connect(&original)?.query_drop(format!("KILL {id}")).map_err(|e| e.to_string())?;
            std::thread::sleep(std::time::Duration::from_millis(300));
            return Err("connection lost before RENAME".into());
        }
        Ok(())
    }).unwrap();
    let after = rvf_triggers(&mut conn, &original.database);
    let retry = rvf_backups(&input, "rvfa0", &original, "rollback_plan", json!({}));
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    assert!(before.contains(&"parent.guard".to_string()));
    assert_eq!(after,before);
    assert_eq!(recovered["status"], "failed_triggers_recreated");
    assert!(recovered["trigger_recovery_error"].is_null());
    assert_eq!(recovered["triggers_recreated"], true);
    assert!(recovered["program_warnings"].to_string().contains("rollback_triggers_recreated"));
    assert_eq!(retry["can_rollback"], true,"{retry:#}");
    assert_eq!(retry["blockers"],json!([]));
}

#[test]
fn safe_promote_rvf_a1_rebuilt_original_before_detach_fails_without_changing_original() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut seen = Vec::new();
    for case in ["control", "rebuild_at_validated"] {
        let Some((original, candidate, mut conn, dir)) = fixture() else { return };
        let p = plan(&original, &candidate, "rvfa1", &[]).unwrap();
        let before_id = table_id(&mut conn, &original.database, "parent").unwrap();
        let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _| {
            if phase == "validated" {
                if case == "control" { return Err("injected".into()) }
                connect(&original)?.query_drop(format!("ALTER TABLE {} FORCE", qualified(&original.database, "parent"))).map_err(|e| e.to_string())?;
            }
            Ok(())
        }).unwrap();
        let after_id = table_id(&mut conn, &original.database, "parent").unwrap();
        let present = tables(&mut conn, &p.original_database).unwrap();
        let hidden_left = p.hidden_views.values().filter(|n| present.contains_key(*n)).count();
        let clone_left = database_exists(&mut conn, &p.clone_database).unwrap();
        let backup_left = database_exists(&mut conn, &p.backup_database).unwrap();
        let verdict = inspect_backup(&original, &p, false, None).unwrap()["verdict"].clone();
        seen.push((case, before_id != after_id, result["status"].clone(), result["original_unchanged"].clone(), hidden_left, clone_left, backup_left, verdict));
        cleanup(&mut conn, &p, &dir);
    }
    let (_, _, status, unchanged, hidden, clone, backup, _) = seen[0].clone();
    assert_eq!((status, unchanged, hidden, clone, backup), (json!("failed_original_unchanged"), json!(true), 0, false, false));
    let (_, rebuilt, status, unchanged, hidden, clone, backup, verdict) = seen[1].clone();
    assert!(rebuilt);
    assert_eq!(status, "failed_original_unchanged");
    assert_eq!(unchanged,true);
    assert_eq!((hidden,clone,backup),(0,false,false));
    assert_eq!(verdict, "undeterminable");
}


#[test]
fn safe_promote_rvf_a2_refused_rename_recreates_triggers_and_allows_retry() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvfa2", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted");
    let input = rvf_journal(&dir, "rvfa2", &original, &candidate, &p, &result);
    let first = rvf_backups(&input, "rvfa2", &original, "rollback_plan", json!({}));
    let displaced = rvf_displaced("rvfa2", &p);
    let journal_dir = rvf_rollback_dir(&input, "rvfa2", &displaced);
    let before = rvf_triggers(&mut conn, &original.database);
    let recovered = rollback_apply_with_hook(&original, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &journal_dir, |phase, _| {
        if phase == "before_rename" {
            // 복구 RENAME 대상 이름을 차지해 서버가 RENAME을 거부(1050)하게 한다
            connect(&original)?.query_drop(format!("CREATE TABLE {} (x INT)", qualified(&displaced, "parent"))).map_err(|e| e.to_string())?;
        }
        Ok(())
    }).unwrap();
    let after = rvf_triggers(&mut conn, &original.database);
    let displaced_left = database_exists(&mut conn, &displaced).unwrap();
    let retry = rvf_backups(&input, "rvfa2", &original, "rollback_plan", json!({}));
    let forced = rvf_backups(&input, "rvfa2", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":retry["plan_digest"]}));
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    assert_eq!(first["can_rollback"], true);
    assert!(recovered["error"].as_str().unwrap_or("").contains("1050"));
    assert_eq!(after, before);
    assert_eq!(recovered["status"], "failed_triggers_recreated");
    assert!(!displaced_left);
    assert_eq!(retry["can_rollback"], true,"{retry:#}");
    assert_eq!(retry["blockers"],json!([]));
    assert_eq!(forced["status"], "rolled_back","{forced:#}");
}

#[test]
fn safe_promote_rvf_a3_refused_rename_recovers_before_unlock() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvfa3", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted");
    let input = rvf_journal(&dir, "rvfa3", &original, &candidate, &p, &result);
    let displaced = rvf_displaced("rvfa3", &p);
    let journal_dir = rvf_rollback_dir(&input, "rvfa3", &displaced);
    let before = rvf_triggers(&mut conn, &original.database);
    let (done_tx, done_rx) = mpsc::channel::<Result<(), String>>();
    let mut writer: Option<std::thread::JoinHandle<Option<Db>>> = None;
    let ep = original.clone();
    let recovered = rollback_apply_with_hook(&original, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &journal_dir, |phase, _| {
        if phase == "before_rename" {
            let (ep, done_tx) = (ep.clone(), done_tx.clone());
            writer = Some(std::thread::spawn(move || {
                let mut app = connect(&ep).ok()?;
                app.query_drop("SET SESSION lock_wait_timeout=60").ok()?;
                app.query_drop("START TRANSACTION").ok()?;
                let r = app.query_drop(format!("UPDATE {} SET note='app' WHERE id=101", qualified(&ep.database, "parent"))).map_err(|e| e.to_string());
                let _ = done_tx.send(r);
                Some(app)
            }));
            std::thread::sleep(std::time::Duration::from_millis(300));
            connect(&original)?.query_drop(format!("CREATE TABLE {} (x INT)", qualified(&displaced, "parent"))).map_err(|e| e.to_string())?;
        }
        Ok(())
    }).unwrap();
    let write = done_rx.recv_timeout(std::time::Duration::from_secs(30)).unwrap();
    let mut app = writer.take().unwrap().join().unwrap().unwrap();
    let after_in_tx = rvf_triggers(&mut conn, &original.database);
    app.query_drop("COMMIT").unwrap();
    drop(app);
    let note: Option<String> = conn.query_first(format!("SELECT note FROM {} WHERE id=101", qualified(&original.database, "parent"))).unwrap();
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    assert!(before.contains(&"parent.guard".to_string()));
    assert!(write.is_ok());
    assert_eq!(note.as_deref(), Some("dump")); // guard(BEFORE UPDATE SET NEW.note='dump')가 있었다면 'dump'
    assert!(recovered["trigger_recovery_error"].is_null(),"{recovered:#}");
    assert_eq!(after_in_tx,before);
    assert_eq!(recovered["status"], "failed_triggers_recreated");
    assert_eq!(recovered["triggers_recreated"], true);
    assert!(recovered["program_warnings"].to_string().contains("rollback_triggers_recreated"));
}
#[test]
fn safe_promote_rvf_a4_promote_skips_relock_without_original_triggers() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir)) = fixture() else { return };
    let p = plan(&original, &candidate, "rvfa4", &[]).unwrap();
    assert!(moved_original_triggers(&p).is_empty());
    let (tx, rx) = mpsc::channel::<Option<mysql::PooledConn>>();
    let ep = original.clone();
    let mut reader = None;
    let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, _| {
        if phase == "before_rename" {
            let (ep, tx) = (ep.clone(), tx.clone());
            reader = Some(std::thread::spawn(move || {
                let app = (|| -> Option<mysql::PooledConn> {
                    let mut app = connect(&ep).ok()?;
                    app.query_drop("SET SESSION lock_wait_timeout=60").ok()?;
                    app.query_drop("START TRANSACTION").ok()?;
                    let _: Option<u64> = app.query_first(format!("SELECT COUNT(*) FROM {}", qualified(&ep.database, "parent"))).ok()?;
                    Some(app)
                })();
                let _ = tx.send(app);
            }));
            std::thread::sleep(std::time::Duration::from_millis(300));
            return Err("injected before rename".into());
        }
        Ok(())
    }).unwrap();
    let app = rx.recv_timeout(std::time::Duration::from_secs(30)).unwrap();
    reader.take().unwrap().join().unwrap();
    drop(app);
    cleanup(&mut conn, &p, &dir);
    assert_eq!(result["status"], "failed_original_unchanged");
    assert!(result["trigger_recovery_error"].is_null(),"{result:#}");
}

#[test]
fn safe_promote_rvf_b5_event_restored_with_new_definer_allows_rollback() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, mut programs)) = program_fixture() else { return };
    let pid = std::process::id();
    let (owner, importer) = (format!("rvf5_owner_{pid}"), format!("rvf5_imp_{pid}"));
    struct Users { endpoint: Endpoint, names: Vec<String> }
    impl Drop for Users { fn drop(&mut self) { if let Ok(mut db) = connect(&self.endpoint) { for n in &self.names { let _ = db.query_drop(format!("DROP USER IF EXISTS '{n}'@'%'")); } } } }
    let admin = Endpoint { database: "tf_test".into(), ..original.clone() };
    let _users = Users { endpoint: admin.clone(), names: vec![owner.clone(), importer.clone()] };
    struct Trust { endpoint: Endpoint, previous: i64 }
    impl Drop for Trust { fn drop(&mut self) { let _ = connect(&self.endpoint).and_then(|mut db| db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}", self.previous)).map_err(|e| e.to_string())); } }
    let _trust = Trust { endpoint: admin.clone(), previous: conn.query_first("SELECT @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap() };
    conn.query_drop("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
    for sql in [
        format!("CREATE USER '{owner}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("CREATE USER '{importer}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{importer}'@'%'"),
        format!("GRANT ALL ON `tf\\_%`.* TO '{importer}'@'%'"),
        format!("DROP TRIGGER {}", qualified(&original.database, "guard")),
        format!("CREATE DEFINER='{importer}'@'%' TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note='old'", qualified(&original.database, "guard"), qualified(&original.database, "parent")),
        format!("DROP FUNCTION {}", qualified(&original.database, "f")),
        format!("CREATE DEFINER='{importer}'@'%' FUNCTION {}() RETURNS INT DETERMINISTIC RETURN 1", qualified(&original.database, "f")),
        format!("DROP EVENT {}", qualified(&original.database, "e")),
        format!("CREATE DEFINER='{owner}'@'%' EVENT {} ON SCHEDULE AT '2099-01-01' ON COMPLETION PRESERVE DISABLE DO SET @tf_note='old'", qualified(&original.database, "e")),
        format!("DROP PROCEDURE {}", qualified(&original.database, "p")),
        format!("CREATE DEFINER='{owner}'@'%' PROCEDURE {}() SQL SECURITY DEFINER SELECT 'old'", qualified(&original.database, "p")),
    ] { conn.query_drop(sql).unwrap(); }
    let e = programs.iter_mut().find(|p| p.kind == "EVENT" && p.name == "e").unwrap();
    e.definition = "CREATE EVENT `e` ON SCHEDULE AT '2000-01-01 00:00:00' ON COMPLETION NOT PRESERVE DISABLE DO SET @tf_note='dump'".into();
    let account = Endpoint { user: importer.clone(), password: "tf_local_test".into(), ..original.clone() };
    let account_candidate = Endpoint { user: importer.clone(), password: "tf_local_test".into(), ..candidate.clone() };
    let p = plan(&account, &account_candidate, "rvfb5", &programs).unwrap();
    let result = promote(&account, &account_candidate, &p, &p.digest, &dir).unwrap();
    let ev: Option<String> = conn.exec_first("SELECT DEFINER FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? AND EVENT_NAME='e'", (&original.database,)).unwrap();
    let effective = with_program_warnings(&p, result["program_warnings"].as_array().map(Vec::as_slice).unwrap_or_default());
    let displaced = format!("tf_backup_recovery_{}", p.nonce);
    let reviewed = rollback_plan(&account, &effective, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced).unwrap();
    cleanup(&mut conn, &p, &dir);
    assert_eq!(result["status"], "promoted_incomplete");
    let failure = &result["programs_failed"][0];
    assert_eq!(failure["code"], "program_event_dropped");
    assert_eq!(failure["previous_restored"], true);
    assert!(failure["restore_warnings"].to_string().contains("program_restore_definer_changed"));
    assert!(result["program_warnings"].to_string().contains("program_restore_definer_changed"));
    assert_eq!(ev, Some(format!("{importer}@%")));
    assert_eq!(reviewed["blockers"],json!([]),"{reviewed:#}");
}
#[test]
fn safe_promote_rvf_c9_rollback_function_binlog_rule_is_prechecked() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir)) = fixture() else { return };
    for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
        conn.query_drop(format!("DROP VIEW {}", qualified(db, view))).unwrap();
    }
    for (db, ret) in [(&original.database, 1), (&candidate.database, 2)] {
        conn.query_drop(format!("CREATE FUNCTION {}() RETURNS INT DETERMINISTIC RETURN {ret}", qualified(db, "f"))).unwrap();
    }
    let programs = capture_programs(&mut conn, &candidate.database).unwrap();
    for p in &programs { conn.query_drop(format!("DROP {} {}", p.kind, qualified(&candidate.database, &p.name))).unwrap(); }
    let (log_bin, trust): (i64, i64) = conn.query_first("SELECT @@GLOBAL.log_bin, @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap();
    struct Restore { endpoint: Endpoint, trust: i64, user: String }
    impl Drop for Restore { fn drop(&mut self) { if let Ok(mut db) = connect(&self.endpoint) {
        let _ = db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}", self.trust));
        let _ = db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'", self.user));
    } } }
    let user = format!("rvf9_rb_{}", std::process::id());
    let _restore = Restore { endpoint: Endpoint { database: "tf_test".into(), ..original.clone() }, trust, user: user.clone() };
    conn.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    let p = plan(&original, &candidate, "rvfc9", &programs).unwrap();
    assert!(p.original_trigger_definitions.is_empty());
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted");
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_%`.* TO '{user}'@'%'"),
    ] { conn.query_drop(sql).unwrap(); }
    let rb = Endpoint { user: user.clone(), password: "tf_local_test".into(), ..original.clone() };
    let displaced = format!("tf_backup_recovery_{}", p.nonce);
    let reviewed = rollback_plan(&rb, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced).unwrap();
    let recovery_dir = dir.join("recovery"); std::fs::create_dir(&recovery_dir).unwrap();
    let recovered = rollback_apply(&rb, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &recovery_dir);
    let f_now: Option<i64> = conn.query_first(format!("SELECT {}()", qualified(&original.database, "f"))).unwrap();
    let tables_back = p.original_tables.iter().all(|(n, t)| optional_table_id(&mut conn, &original.database, n).unwrap() == Some(t.table_id));
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    let refused = recovered.unwrap_err();
    assert_eq!(log_bin, 1);
    assert!(reviewed["blockers"].to_string().contains("rollback_routine_binlog_unsafe"));
    assert!(!tables_back);
    assert!(refused.contains("recovery is blocked"));

    assert_eq!(f_now, Some(2));
}
#[test]
fn safe_promote_rvf_d12_edited_original_trigger_after_failed_attempt_allows_candidate_cleanup() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvfd12", &programs).unwrap();
    let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |at, _| if at == "before_rename" { Err("injected".into()) } else { Ok(()) }).unwrap();
    let verdict_before = inspect_backup_recorded(&original, &p, false, None,true,false).unwrap()["verdict"].clone();
    conn.query_drop(format!("DROP TRIGGER {}", qualified(&original.database, "guard"))).unwrap();
    conn.query_drop(format!("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note='edited'", qualified(&original.database, "guard"), qualified(&original.database, "parent"))).unwrap();
    let verdict_after = inspect_backup_recorded(&original, &p, false, None,true,false).unwrap()["verdict"].clone();
    let input = rvf_journal(&dir, "rvfd12", &original, &candidate, &p, &result);
    let rc = rvf_backups(&input, "rvfd12", &original, "reconcile", json!({}));
    let cp = rvf_backups(&input, "rvfd12", &original, "cleanup_plan", json!({"target":"candidate"}));
    cleanup(&mut conn, &p, &dir);
    assert_eq!(result["status"], "failed_original_unchanged");
    assert_eq!(verdict_before, "not_promoted");
    assert_eq!(verdict_after, "not_promoted");
    assert_eq!(rc["conclusion"], "not_promoted");
    assert!(!rc["message"].as_str().unwrap().contains("Recover them from their saved definitions"));
    assert_eq!(cp["can_cleanup"], false);
    assert!(!cp["blockers"].to_string().contains("only a verified, unpromoted candidate can be discarded"));
}
#[test]
fn safe_promote_rvf_d13_completed_promotion_ignores_expired_recurring_event() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, mut programs)) = program_fixture() else { return };
    let scheduler: String = conn.query_first("SELECT @@event_scheduler").unwrap().unwrap();
    struct Scheduler { endpoint: Endpoint, previous: String }
    impl Drop for Scheduler { fn drop(&mut self) { let _ = connect(&self.endpoint).and_then(|mut db| db.query_drop(format!("SET GLOBAL event_scheduler={}", self.previous)).map_err(|e| e.to_string())); } }
    let _scheduler = Scheduler { endpoint: original.clone(), previous: scheduler };
    conn.query_drop("SET GLOBAL event_scheduler=ON").unwrap();
    let event = programs.iter_mut().find(|p| p.name == "new_e").unwrap();
    conn.exec_drop("SET SESSION time_zone=?", (&event.time_zone,)).unwrap();
    let (starts, ends): (String, String) = conn.query_first("SELECT DATE_FORMAT(NOW(),'%Y-%m-%d %H:%i:%s'), DATE_FORMAT(NOW()+INTERVAL 8 SECOND,'%Y-%m-%d %H:%i:%s')").unwrap().unwrap();
    reset_session(&mut conn).unwrap();
    event.definition = format!("CREATE EVENT `new_e` ON SCHEDULE EVERY 1 SECOND STARTS '{starts}' ENDS '{ends}' ON COMPLETION NOT PRESERVE ENABLE DO SET @tf_finished=1");
    let p = plan(&original, &candidate, "rvfd13", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted");
    let input = rvf_journal(&dir, "rvfd13", &original, &candidate, &p, &r);
    let before = inspect_backup_recorded(&original, &p, false, r.get("backup_fingerprint"), true,false).unwrap();
    let mut gone = false;
    for _ in 0..80 {
        let n: Option<u64> = conn.exec_first("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? AND EVENT_NAME='new_e'", (&original.database,)).unwrap();
        if n == Some(0) { gone = true; break }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    let after = inspect_backup_recorded(&original, &p, false, r.get("backup_fingerprint"), true,false).unwrap();
    let cp = rvf_backups(&input, "rvfd13", &original, "cleanup_plan", json!({"target":"backup"}));
    let rc = rvf_backups(&input, "rvfd13", &original, "reconcile", json!({}));
    cleanup(&mut conn, &p, &dir);
    assert_eq!(before["verdict"], "promoted");
    assert!(gone);
    assert_eq!(after["verdict"], "promoted");
    assert!(after["missing_programs"].is_null());
    assert_eq!(cp["can_cleanup"], true,"{cp:#}");
    assert_eq!(rc["conclusion"], "promoted");
}
#[test]
fn safe_promote_rvf_d14_completed_event_timezone_does_not_change_backup_digest() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, mut programs)) = program_fixture() else { return };
    let scheduler: String = conn.query_first("SELECT @@event_scheduler").unwrap().unwrap();
    struct Scheduler { endpoint: Endpoint, previous: String }
    impl Drop for Scheduler { fn drop(&mut self) { let _ = connect(&self.endpoint).and_then(|mut db| db.query_drop(format!("SET GLOBAL event_scheduler={}", self.previous)).map_err(|e| e.to_string())); } }
    let _scheduler = Scheduler { endpoint: original.clone(), previous: scheduler };
    conn.query_drop("SET GLOBAL event_scheduler=ON").unwrap();
    conn.query_drop("SET SESSION time_zone='+00:00'").unwrap();
    conn.query_drop(format!("ALTER TABLE {} ADD COLUMN ts TIMESTAMP NULL", qualified(&original.database, "child"))).unwrap();
    conn.query_drop(format!("UPDATE {} SET ts='2020-01-01 00:00:00'", qualified(&original.database, "child"))).unwrap();
    let event = programs.iter_mut().find(|p| p.name == "new_e").unwrap();
    event.time_zone = Some("+09:00".into());
    conn.query_drop("SET SESSION time_zone='+09:00'").unwrap();
    let at: String = conn.query_first("SELECT DATE_FORMAT(NOW()+INTERVAL 10 SECOND,'%Y-%m-%d %H:%i:%s')").unwrap().unwrap();
    reset_session(&mut conn).unwrap();
    event.definition = format!("CREATE EVENT `new_e` ON SCHEDULE AT '{at}' ON COMPLETION NOT PRESERVE ENABLE DO SET @tf_finished=1");
    let p = plan(&original, &candidate, "rvfd14", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted");
    let input = rvf_journal(&dir, "rvfd14", &original, &candidate, &p, &r);
    let before = inspect_backup_recorded(&original, &p, true, r.get("backup_fingerprint"), true,false).unwrap();
    let mut gone = false;
    for _ in 0..80 {
        let n: Option<u64> = conn.exec_first("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=? AND EVENT_NAME='new_e'", (&original.database,)).unwrap();
        if n == Some(0) { gone = true; break }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    let after = inspect_backup_recorded(&original, &p, true, r.get("backup_fingerprint"), true,false).unwrap();
    let cp = rvf_backups(&input, "rvfd14", &original, "cleanup_plan", json!({"target":"backup"}));
    cleanup(&mut conn, &p, &dir);
    assert_eq!(before["verdict"], "promoted");
    assert_eq!(before["unchanged"], true);
    assert!(gone);
    assert_eq!(after["verdict"], "promoted");
    assert_eq!(after["unchanged"], true);
    assert_eq!(after["blockers"],json!([]));
    assert_eq!(cp["can_cleanup"], true,"{cp:#}");
}
#[test]
fn safe_promote_rvf_d17_completed_promotion_does_not_reread_unsavable_trigger() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvfd17", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted");
    let input = rvf_journal(&dir, "rvfd17", &original, &candidate, &p, &r);
    let listed_before = rvf_backups(&input, "rvfd17", &original, "list", json!({}));
    conn.query_drop(format!("USE {}", q(&original.database))).unwrap();
    conn.query_drop("SET SESSION character_set_client=latin1, collation_connection=latin1_swedish_ci").unwrap();
    conn.query_drop("CREATE TRIGGER app_note BEFORE INSERT ON parent FOR EACH ROW SET NEW.note=CONCAT(NEW.note,'é')").unwrap();
    reset_session(&mut conn).unwrap();
    let recorded = inspect_backup_recorded(&original, &p, false, r.get("backup_fingerprint"), true,false);
    let classic = inspect_backup(&original, &p, false, r.get("backup_fingerprint"));
    let listed = rvf_backups(&input, "rvfd17", &original, "list", json!({}));
    let rc = rvf_backups(&input, "rvfd17", &original, "reconcile", json!({}));
    cleanup(&mut conn, &p, &dir);
    assert_eq!(listed_before["event"], "result");
    assert_eq!(recorded.unwrap()["verdict"],"promoted");
    assert_eq!(classic.unwrap()["verdict"], "promoted_incomplete");
    assert_eq!(listed["event"], "result");
    assert_eq!(rc["event"], "result");
    assert_eq!(rc["conclusion"],"promoted");
}
#[test]
fn safe_promote_rvf_d18_rolled_back_verdict_survives_normal_program_changes() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvfd18", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted");
    let input = rvf_journal(&dir, "rvfd18", &original, &candidate, &p, &r);
    let review = rvf_backups(&input, "rvfd18", &original, "rollback_plan", json!({}));
    let applied = rvf_backups(&input, "rvfd18", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    let verdict = |_label: &str| {
        let listed = rvf_backups(&input, "rvfd18", &original, "list", json!({}));
        let rc = rvf_backups(&input, "rvfd18", &original, "reconcile", json!({}));
        let b = &listed["backups"][0]["backup"];
        (b["verdict"].clone(), b["missing_programs"].clone(), rc["conclusion"].clone())
    };
    let v0 = verdict("right after recovery");
    conn.query_drop(format!("CREATE TRIGGER {} AFTER UPDATE ON {} FOR EACH ROW SET @tf_audit=1", qualified(&original.database, "app_audit"), qualified(&original.database, "parent"))).unwrap();
    let vb = verdict("app adds a trigger");
    conn.query_drop(format!("DROP TRIGGER {}", qualified(&original.database, "app_audit"))).unwrap();
    let vb2 = verdict("trigger removed");
    conn.query_drop(format!("DROP PROCEDURE {}", qualified(&original.database, "p"))).unwrap();
    conn.query_drop(format!("CREATE PROCEDURE {}() SELECT 'v2'", qualified(&original.database, "p"))).unwrap();
    let va = verdict("app redeploys p");
    let _ = applied["displaced_backup"].as_str().map(|d| conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(d))));
    cleanup(&mut conn, &p, &dir);
    assert_eq!(applied["status"], "rolled_back");
    assert_eq!(v0.0, "rolled_back");
    assert_eq!(vb.0, "rolled_back");
    assert_eq!(vb.1,json!([]));
    assert_eq!(vb.2, "rolled_back");
    assert_eq!(vb2.0, "rolled_back");
    assert_eq!(va.0, "rolled_back");
    assert_eq!(va.1,json!([]));
    assert_eq!(va.2, "rolled_back");
}
#[test]
fn safe_promote_rvf_d19_displaced_cleanup_preserves_rolled_back_verdict() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir)) = fixture() else { return };
    let trust: String = conn.query_first("SELECT CAST(@@GLOBAL.log_bin_trust_function_creators AS CHAR)").unwrap().unwrap();
    conn.query_drop("SET GLOBAL log_bin_trust_function_creators=1").unwrap();
    for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
        conn.query_drop(format!("DROP VIEW {}", qualified(db, view))).unwrap();
    }
    let n = &original.database[original.database.len() - 10..original.database.len() - 2];
    let (legacy, app) = (format!("rvf_lg_{n}"), format!("rvf_app_{n}"));
    conn.query_drop(format!("CREATE USER '{legacy}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    conn.query_drop(format!("GRANT SELECT, UPDATE, TRIGGER ON {}.* TO '{legacy}'@'%'", q(&original.database))).unwrap();
    conn.query_drop(format!("CREATE DEFINER='{legacy}'@'%' TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW SET NEW.note=CONCAT(NEW.note,'!')", qualified(&original.database, "legacy_bu"), qualified(&original.database, "parent"))).unwrap();
    conn.query_drop(format!("CREATE USER '{app}'@'%' IDENTIFIED BY 'tf_local_test'")).unwrap();
    conn.query_drop(format!("GRANT ALL PRIVILEGES ON *.* TO '{app}'@'%'")).unwrap();
    for privilege in ["SUPER", "SET_USER_ID", "SET_ANY_DEFINER", "ALLOW_NONEXISTENT_DEFINER", "SYSTEM_USER"] { let _ = conn.query_drop(format!("REVOKE {privilege} ON *.* FROM '{app}'@'%'")); }
    let as_app = |e: &Endpoint| Endpoint { user: app.clone(), ..e.clone() };
    let (o, c) = (as_app(&original), as_app(&candidate));
    let p = plan(&o, &c, "rvfd19", &[]).unwrap();
    let r = promote(&o, &c, &p, &p.digest, &dir).unwrap();
    let input = rvf_journal(&dir, "rvfd19", &o, &c, &p, &r);
    let review = rvf_backups(&input, "rvfd19", &o, "rollback_plan", json!({}));
    let applied = rvf_backups(&input, "rvfd19", &o, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    let definer: Option<String> = conn.exec_first("SELECT DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=? AND TRIGGER_NAME='legacy_bu'", (&original.database,)).unwrap();
    let list0 = rvf_backups(&input, "rvfd19", &o, "list", json!({}));
    let cpd = rvf_backups(&input, "rvfd19", &o, "cleanup_plan", json!({"target":"displaced"}));
    let cad = rvf_backups(&input, "rvfd19", &o, "cleanup_apply", json!({"target":"displaced","confirmed":true,"plan_digest":cpd["plan_digest"]}));
    let list1 = rvf_backups(&input, "rvfd19", &o, "list", json!({}));
    let rc1 = rvf_backups(&input, "rvfd19", &o, "reconcile", json!({}));
    let _ = applied["displaced_backup"].as_str().map(|d| conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(d))));
    conn.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={trust}")).unwrap();
    for user in [&legacy, &app] { conn.query_drop(format!("DROP USER '{user}'@'%'")).unwrap(); }
    cleanup(&mut conn, &p, &dir);
    assert_eq!(r["status"], "promoted");
    assert_eq!(applied["status"], "rolled_back");
    assert!(applied["program_warnings"].to_string().contains("program_restore_definer_changed"));
    assert_eq!(definer, Some(format!("{app}@%")));
    assert_eq!(list0["backups"][0]["backup"]["verdict"], "rolled_back");
    assert_eq!(cad["status"], "cleaned");
    assert_eq!(list1["backups"][0]["backup"]["verdict"], "rolled_back");
    assert_eq!(rc1["conclusion"], "rolled_back");
}
#[test]
fn safe_promote_rvf_recorded_event_check_resets_session_on_success_and_error() {
    let _guard=LIVE_LOCK.lock().unwrap_or_else(|e|e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else{return};
    let p=plan(&original,&candidate,"reset_context",&programs).unwrap();
    let mut event=programs.iter().find(|p|p.kind=="EVENT").unwrap().clone();
    event.time_zone=Some("+09:00".into());
    event.definition="CREATE EVENT `e` ON SCHEDULE AT '2000-01-01' ON COMPLETION NOT PRESERVE DISABLE DO SET @tf_test=1".into();
    event.name="e".into();
    assert!(crate::mysql_schema_programs::recorded_event_completed(&mut conn,&event).unwrap());
    assert_eq!(conn.query_first::<String,_>("SELECT @@session.time_zone").unwrap().as_deref(),Some("+00:00"));
    event.definition="bad definition".into();
    conn.query_drop("SET time_zone='+09:00'").unwrap();
    assert!(crate::mysql_schema_programs::recorded_event_completed(&mut conn,&event).is_err());
    assert_eq!(conn.query_first::<String,_>("SELECT @@session.time_zone").unwrap().as_deref(),Some("+00:00"));
    cleanup(&mut conn,&p,&dir);
}

#[test]
fn safe_promote_rvf_list_keeps_other_entries_after_inspection_error() {
    let _guard=LIVE_LOCK.lock().unwrap_or_else(|e|e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else{return};
    let p=plan(&original,&candidate,"list_good",&programs).unwrap();
    let result=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
    let input=rvf_journal(&dir,"list_good",&original,&candidate,&p,&result);
    let mut bad=p.clone(); bad.restore_id="list_bad".into(); bad.port=original.port+1;
    bad.backup_database=format!("tf_backup_list_bad_{}",p.nonce);
    conn.query_drop(format!("CREATE DATABASE {}",q(&bad.backup_database))).unwrap();
    rvf_journal(&dir,"list_bad",&original,&candidate,&bad,&result);
    let listed=rvf_backups(&input,"list_good",&original,"list",json!({}));
    assert_eq!(listed["event"],"result","{listed:#}");
    assert_eq!(listed["backups"].as_array().unwrap().len(),2);
    assert!(listed["backups"].as_array().unwrap().iter().any(|b|b["inspection_error"].is_string()));
    assert!(listed["backups"].as_array().unwrap().iter().any(|b|b["backup"]["verdict"]=="promoted"));
    let failed=listed["backups"].as_array().unwrap().iter().find(|b|b["restore_id"]=="list_bad").unwrap();
    assert_eq!(failed["backup"]["namespace"],bad.backup_database);
    assert!(!listed["unproven_namespaces"].as_array().unwrap().iter().any(|b|b["namespace"]==bad.backup_database));
    conn.query_drop(format!("DROP DATABASE {}",q(&bad.backup_database))).unwrap();
    cleanup(&mut conn,&p,&dir);
}

#[test]
fn safe_promote_rvf_lost_connection_failed_relock_does_not_claim_recreated_triggers() {
    let _guard=LIVE_LOCK.lock().unwrap_or_else(|e|e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else{return};
    let p=plan(&original,&candidate,"lost_relock",&programs).unwrap();
    let r=promote(&original,&candidate,&p,&p.digest,&dir).unwrap();
    let displaced=format!("tf_backup_recovery_{}",p.nonce);
    let rd=dir.join("recovery");std::fs::create_dir(&rd).unwrap();
    let mut holder=None;
    let result=rollback_apply_with_hook(&original,&p,r.get("backup_fingerprint"),r.get("new_table_ids"),&displaced,&rd,|phase,db| {
        if phase=="before_rename" {
            let id:u64=db.query_first("SELECT CONNECTION_ID()").map_err(|e|e.to_string())?.unwrap();
            connect(&original)?.query_drop(format!("KILL {id}")).map_err(|e|e.to_string())?;
            let mut app=connect(&original)?;
            app.query_drop("START TRANSACTION").map_err(|e|e.to_string())?;
            app.query_drop(format!("SELECT * FROM {}",qualified(&original.database,"parent"))).map_err(|e|e.to_string())?;
            holder=Some(app);return Err("lost connection".into())
        }
        Ok(())
    }).unwrap();
    holder.as_mut().unwrap().query_drop("ROLLBACK").unwrap();drop(holder);
    assert_eq!(result["status"],"rollback_unknown","{result:#}");
    assert_eq!(result["triggers_recreated"],false);
    assert!(!result["program_warnings"].to_string().contains("rollback_triggers_recreated"));
    assert!(result["trigger_recovery_error"].as_str().unwrap().contains("1205"));
    conn.query_drop(format!("DROP DATABASE IF EXISTS {}",q(&displaced))).unwrap();cleanup(&mut conn,&p,&dir);
}

#[test]
fn safe_promote_rvf_rejected_promotion_keeps_original_trigger_for_queued_writer() {
    let _guard=LIVE_LOCK.lock().unwrap_or_else(|e|e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else{return};
    let p=plan(&original,&candidate,"reject_promotion",&programs).unwrap();
    let mut writer=None;
    let result=promote_with_hook(&original,&candidate,&p,&p.digest,&dir,|phase,_| {
        if phase=="before_rename" {
            let ep=original.clone();
            writer=Some(std::thread::spawn(move || { let mut app=connect(&ep).unwrap();app.query_drop("SET lock_wait_timeout=30").unwrap();app.query_drop(format!("UPDATE {} SET note='app' WHERE id=1",qualified(&ep.database,"parent"))).unwrap(); }));
            std::thread::sleep(std::time::Duration::from_millis(100));
            connect(&original)?.query_drop(format!("CREATE TABLE {}(x INT)",qualified(&p.backup_database,"parent"))).map_err(|e|e.to_string())?;
        }
        Ok(())
    }).unwrap();
    writer.unwrap().join().unwrap();
    assert_eq!(result["status"],"failed_original_unchanged","{result:#}");
    assert_eq!(conn.query_first::<String,_>(format!("SELECT note FROM {} WHERE id=1",qualified(&original.database,"parent"))).unwrap().as_deref(),Some("old"));
    cleanup(&mut conn,&p,&dir);
}

#[test]
fn safe_promote_rvf_unreadable_view_is_one_scoped_blocker() {
    let _guard=LIVE_LOCK.lock().unwrap_or_else(|e|e.into_inner());
    let Some((original,candidate,mut conn,dir,programs))=program_fixture() else{return};
    let p=plan(&original,&candidate,"bad_view",&programs).unwrap();
    let manifest:DumpManifest=serde_json::from_value(json!({"format":"tunnelforge-dump","format_version":5,"source_engine":"mysql","database":original.database,"schema":{"tables":[]},"chunk_size":1,"created_unix_seconds":0,"tables":[],"views":[{"name":"bad","definition":"SELECT 'unterminated"},{"name":"calls_f","definition":"SELECT f()"}],"stored_programs":programs})).unwrap();
    let refusals=crate::import::safe_restore::copy_program_refusals(&mut conn,&manifest,false,false).unwrap();
    assert!(refusals.iter().any(|r|r.0=="copy_checks_unavailable" && r.1.as_deref()==Some("bad")));
    assert!(refusals.iter().any(|r|r.0=="view_calls_dump_routine" && r.1.as_deref()==Some("calls_f")));
    cleanup(&mut conn,&p,&dir);
}
