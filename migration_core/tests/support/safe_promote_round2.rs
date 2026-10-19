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
fn safe_promote_rvf_a0_rollback_connection_lost_after_detach_gets_no_fresh_recovery() {
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
fn safe_promote_rvf_a1_rebuilt_original_before_detach_is_cutover_unknown() {
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
fn safe_promote_rvf_a2_refused_rename_recreates_triggers_and_blocks_retry() {
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
fn safe_promote_rvf_a3_refused_rename_relock_window_and_timeout() {
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
fn safe_promote_rvf_a4_promote_relocks_without_original_triggers() {
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
