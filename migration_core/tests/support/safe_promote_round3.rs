
// ---------------------------------------------------------------- round-3 live reproductions (rvg)
fn rvg_save(name: &str, value: &Value) {
    if let Ok(dir) = std::env::var("RVG_EVIDENCE") {
        let _ = std::fs::create_dir_all(&dir);
        std::fs::write(std::path::Path::new(&dir).join(name), serde_json::to_vec_pretty(value).unwrap()).unwrap();
    }
}

/// [0] rollback: RENAME rejected (1050), SELECT 1 succeeds, the connection is then lost inside the
/// locked recover_triggers (killed while it waits for the backup lock). recovered_locked=true blocks
/// the fresh-connection recovery.
#[test]
fn safe_promote_rvg0_rollback_lost_connection_in_locked_recovery() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvg0r", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted");
    let input = rvf_journal(&dir, "rvg0r", &original, &candidate, &p, &result);
    let displaced = rvf_displaced("rvg0r", &p);
    let journal_dir = rvf_rollback_dir(&input, "rvg0r", &displaced);
    let before = rvf_triggers(&mut conn, &original.database);
    let (locked_tx, locked_rx) = mpsc::channel::<()>();
    let mut killer: Option<std::thread::JoinHandle<Vec<String>>> = None;
    let ep = original.clone();
    let recovered = rollback_apply_with_hook(&original, &p, result.get("backup_fingerprint"), result.get("new_table_ids"), &displaced, &journal_dir, |phase, db| {
        if phase == "before_rename" {
            let id: u64 = db.query_first("SELECT CONNECTION_ID()").map_err(|e| e.to_string())?.unwrap();
            // The server rejects the RENAME (1050): the recovery target name is taken.
            connect(&ep)?.query_drop(format!("CREATE TABLE {} (x INT)", qualified(&displaced, "parent"))).map_err(|e| e.to_string())?;
            let (ep, tx) = (ep.clone(), locked_tx.clone());
            killer = Some(std::thread::spawn(move || {
                let mut log = Vec::new();
                let mut backup = connect(&ep).unwrap();
                backup.query_drop("LOCK INSTANCE FOR BACKUP").unwrap();
                tx.send(()).unwrap();
                let mut watch = connect(&ep).unwrap();
                let mut last = String::new();
                let mut killed = false;
                for _ in 0..1000 {
                    let row: Option<(Option<String>, Option<String>)> = watch.exec_first("SELECT STATE, INFO FROM information_schema.PROCESSLIST WHERE ID=?", (id,)).unwrap();
                    let seen = format!("{row:?}");
                    if seen != last { log.push(seen.clone()); last = seen; }
                    if let Some((Some(state), info)) = row {
                        let info = info.unwrap_or_default();
                        if state.contains("backup lock") && !info.starts_with("RENAME") {
                            watch.query_drop(format!("KILL {id}")).unwrap();
                            log.push(format!("KILLED during: {info}"));
                            killed = true;
                            break;
                        }
                    }
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                if !killed { log.push("never killed".into()); }
                std::thread::sleep(std::time::Duration::from_millis(300));
                backup.query_drop("UNLOCK INSTANCE").unwrap();
                log
            }));
            locked_rx.recv_timeout(std::time::Duration::from_secs(10)).map_err(|e| e.to_string())?;
        }
        Ok(())
    }).unwrap();
    let log = killer.take().unwrap().join().unwrap();
    let after = rvf_triggers(&mut conn, &original.database);
    let retry = rvf_backups(&input, "rvg0r", &original, "rollback_plan", json!({}));
    rvg_save("rvg0_rollback.json", &json!({"recovered": recovered, "log": log, "before": before, "after": after, "retry": retry}));
    let _ = conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(&displaced)));
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVG0R log={log:#?}\nstatus={} error={} trigger_recovery_error={} triggers_recreated={}\nbefore={before:?}\nafter={after:?}\nretry.can_rollback={} retry.blockers={}",
        recovered["status"], recovered["error"], recovered["trigger_recovery_error"], recovered["triggers_recreated"], retry["can_rollback"], retry["blockers"]);
    // Losing the locked recovery session must fall through to fresh-connection recovery.
    assert!(log.iter().any(|l| l.starts_with("KILLED during")), "{log:?}");
    assert!(recovered["error"].as_str().unwrap_or("").contains("atomic recovery rename: MySqlError"), "{recovered:#}"); // server-rejected RENAME (1205 under the backup lock, or 1050)
    assert!(recovered["trigger_recovery_error"].is_string(), "{recovered:#}");
    assert_eq!(recovered["status"], "failed_triggers_recreated", "{recovered:#}");
    assert!(before.contains(&"parent.guard".to_string()));
    assert!(after.contains(&"parent.guard".to_string()));
    assert_eq!(retry["can_rollback"], true, "{retry:#}");
}

/// [0] promote: RENAME rejected (1050), SELECT 1 succeeds, connection killed at the start of the
/// locked recovery. recovered_locked=true skips the fresh-connection recovery.
#[test]
fn safe_promote_rvg0_promote_lost_connection_in_locked_recovery() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvg0p", &programs).unwrap();
    let before = rvf_triggers(&mut conn, &original.database);
    let backup_db = p.backup_database.clone();
    let mut calls = Vec::new();
    let result = promote_with_hook(&original, &candidate, &p, &p.digest, &dir, |phase, db| {
        if phase == "before_rename" {
            connect(&original)?.query_drop(format!("CREATE TABLE {}(x INT)", qualified(&backup_db, "parent"))).map_err(|e| e.to_string())?;
        }
        if phase == "before_original_trigger_recovery" {
            calls.push(phase.to_string());
            if calls.len() == 1 {
                let id: u64 = db.query_first("SELECT CONNECTION_ID()").map_err(|e| e.to_string())?.unwrap();
                connect(&original)?.query_drop(format!("KILL {id}")).map_err(|e| e.to_string())?;
                std::thread::sleep(std::time::Duration::from_millis(300));
            }
        }
        Ok(())
    }).unwrap();
    let after = rvf_triggers(&mut conn, &original.database);
    rvg_save("rvg0_promote.json", &json!({"result": result, "before": before, "after": after, "recovery_calls": calls}));
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVG0P status={} error={} trigger_recovery_error={} calls={calls:?}\nbefore={before:?}\nafter={after:?}",
        result["status"], result["error"], result["trigger_recovery_error"]);
    assert!(result["error"].as_str().unwrap_or("").contains("1050"), "{result:#}");
    assert!(result["trigger_recovery_error"].is_string(), "{result:#}");
    assert_eq!(calls.len(), 2, "fresh-connection recovery must run");
    assert_eq!(result["status"], "failed_original_unchanged", "{result:#}");
    assert!(before.contains(&"parent.guard".to_string()));
    assert!(after.contains(&"parent.guard".to_string()));
}

/// [4] rollback preview blocker text for the FUNCTION binlog pre-check (non-SUPER account).
#[test]
fn safe_promote_rvg4_rollback_binlog_blocker_text() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir)) = fixture() else { return };
    for (db, view) in [(&original.database, "child_view"), (&original.database, "v"), (&candidate.database, "v")] {
        conn.query_drop(format!("DROP VIEW {}", qualified(db, view))).unwrap();
    }
    for (db, body) in [(&original.database, "DETERMINISTIC RETURN 1"), (&candidate.database, "DETERMINISTIC RETURN 2")] {
        conn.query_drop(format!("CREATE FUNCTION {}() RETURNS INT {body}", qualified(db, "f"))).unwrap();
    }
    let programs = capture_programs(&mut conn, &candidate.database).unwrap();
    for p in &programs { conn.query_drop(format!("DROP {} {}", p.kind, qualified(&candidate.database, &p.name))).unwrap(); }
    let (log_bin, trust): (i64, i64) = conn.query_first("SELECT @@GLOBAL.log_bin, @@GLOBAL.log_bin_trust_function_creators").unwrap().unwrap();
    struct Restore { endpoint: Endpoint, trust: i64, user: String }
    impl Drop for Restore { fn drop(&mut self) { if let Ok(mut db) = connect(&self.endpoint) {
        let _ = db.query_drop(format!("SET GLOBAL log_bin_trust_function_creators={}", self.trust));
        let _ = db.query_drop(format!("DROP USER IF EXISTS '{}'@'%'", self.user));
    } } }
    let user = format!("rvg4_rb_{}", std::process::id());
    let _restore = Restore { endpoint: Endpoint { database: "tf_test".into(), ..original.clone() }, trust, user: user.clone() };
    conn.query_drop("SET GLOBAL log_bin_trust_function_creators=0").unwrap();
    let p = plan(&original, &candidate, "rvg4", &programs).unwrap();
    let result = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(result["status"], "promoted", "{result:#}");
    let input = rvf_journal(&dir, "rvg4", &original, &candidate, &p, &result);
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_%`.* TO '{user}'@'%'"),
    ] { conn.query_drop(sql).unwrap(); }
    let rb = Endpoint { user: user.clone(), password: "tf_local_test".into(), ..original.clone() };
    let super_grant: Vec<String> = connect(&rb).unwrap().query("SHOW GRANTS").unwrap();
    let reviewed = rvf_backups(&input, "rvg4", &rb, "rollback_plan", json!({}));
    rvg_save("rvg4_rollback_plan.json", &reviewed);
    cleanup(&mut conn, &p, &dir);
    eprintln!("RVG4 log_bin={log_bin} trust(now)=0 grants={super_grant:?}\nreviewed={reviewed:#}");
    assert_eq!(log_bin, 1);
    assert_eq!(reviewed["can_rollback"], false, "{reviewed:#}");
    assert!(reviewed["blockers"].as_array().unwrap().iter().any(|b| b.as_str().unwrap().starts_with("rollback_routine_binlog_unsafe: f: ")), "{reviewed:#}");
}

/// [5] a rollback_incomplete record stays rollback_incomplete after the program is recreated from the journal.
#[test]
fn safe_promote_rvg5_repaired_programs_complete_rollback_and_allow_cleanup() {
    let _guard = LIVE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let Some((original, candidate, mut conn, dir, programs)) = program_fixture() else { return };
    let p = plan(&original, &candidate, "rvg5", &programs).unwrap();
    let r = promote(&original, &candidate, &p, &p.digest, &dir).unwrap();
    assert_eq!(r["status"], "promoted", "{r:#}");
    let input = rvf_journal(&dir, "rvg5", &original, &candidate, &p, &r);
    let review = rvf_backups(&input, "rvg5", &original, "rollback_plan", json!({}));
    assert_eq!(review["can_rollback"], true, "{review:#}");
    // An application transaction keeps the MDL of f: DROP FUNCTION f waits and fails with 1205.
    let mut app = connect(&original).unwrap();
    app.query_drop("START TRANSACTION").unwrap();
    let _: Option<i64> = app.query_first(format!("SELECT {}()", qualified(&original.database, "f"))).unwrap();
    let applied = rvf_backups(&input, "rvg5", &original, "rollback_apply", json!({"confirmed":true,"plan_digest":review["plan_digest"]}));
    app.query_drop("ROLLBACK").unwrap();
    drop(app);
    let listed0 = rvf_backups(&input, "rvg5", &original, "list", json!({}));
    // The user recreates f from the definition saved in the promotion journal.
    let previous = p.original_programs.iter().find(|x| x.kind == "FUNCTION" && x.name == "f").unwrap().clone();
    let mut note = json!({});
    conn.query_drop(format!("USE {}", q(&original.database))).unwrap();
    crate::mysql_schema_programs::restore_schema_program(&mut conn, &original.database, &previous, &mut note).unwrap();
    reset_session(&mut conn).unwrap();
    let f_now: Option<i64> = conn.query_first(format!("SELECT {}()", qualified(&original.database, "f"))).unwrap();
    let listed1 = rvf_backups(&input, "rvg5", &original, "list", json!({}));
    let rc1 = rvf_backups(&input, "rvg5", &original, "reconcile", json!({}));
    for target in ["backup","displaced"] {
        let review = rvf_backups(&input,"rvg5",&original,"cleanup_plan",json!({"target":target}));
        assert_eq!(review["can_cleanup"],true,"{review:#}");
    }
    rvg_save("rvg5.json", &json!({"applied": applied, "listed0": listed0, "listed1": listed1, "rc1": rc1}));
    let _ = applied["displaced_backup"].as_str().map(|d| conn.query_drop(format!("DROP DATABASE IF EXISTS {}", q(d))));
    cleanup(&mut conn, &p, &dir);
    let b0 = &listed0["backups"][0]["backup"];
    let b1 = &listed1["backups"][0]["backup"];
    eprintln!("RVG5 applied.status={} pending={} missing={} error={}\nlist0 verdict={} missing={}\nf()={f_now:?}\nlist1 verdict={} missing={}\nreconcile conclusion={} message={} evidence.missing={}",
        applied["status"], applied["pending_program"], applied["missing_programs"], applied["error"], b0["verdict"], b0["missing_programs"],
        b1["verdict"], b1["missing_programs"], rc1["conclusion"], rc1["message"], rc1["evidence"]["missing_programs"]);
    assert_eq!(applied["status"], "rollback_incomplete", "{applied:#}");
    assert_eq!(b0["verdict"], "rollback_incomplete");
    assert_eq!(f_now, Some(1));
    assert_eq!(b1["missing_programs"], json!([]));
    assert_eq!(b1["verdict"], "rolled_back");
    assert_eq!(rc1["conclusion"], "rolled_back");
    assert!(!rc1["message"].as_str().unwrap().contains("some programs still need"));
    assert_eq!(rc1["evidence"]["missing_programs"], json!([]));
}

