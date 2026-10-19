    // ------------------------------------------------------------ round-5 live reproduction (rvi claim [2])
    // Included by the existing PostgreSQL promotion test module.
    /// The full dump.promote confirm pipeline on PostgreSQL; the promoting backend is terminated while it
    /// waits for its table lock (stand-in for a dropped tunnel), so the transaction and its rollback fail.
    #[test]
    fn postgres_rvi2_terminated_cutover_payload() {
        let Some(base) = endpoint() else { return };
        let (original, candidate, prepared, dir) = fixture(&base);
        let restore_id = prepared["restore_id"].as_str().unwrap().to_string();
        let planned = run("dump.promote", json!({"endpoint":original,"action":"plan","report_path":prepared["report_path"],"restore_id":restore_id}));
        let mut reader = connect(&original).unwrap();
        let mut held = reader.transaction().unwrap();
        held.batch_execute(&format!("LOCK TABLE {} IN ACCESS SHARE MODE", qualified(original.schema.as_deref().unwrap(), "items"))).unwrap();
        let payload = json!({"endpoint":original,"action":"confirm","report_path":prepared["report_path"],"restore_id":restore_id,
            "plan_digest":planned["plan_digest"],"overwrite_confirmed":true});
        let worker = std::thread::spawn(move || handle_request(Request { command: "dump.promote".into(), request_id: None, payload }));
        let mut admin = connect(&base).unwrap();
        let application = format!("tf_promote_{restore_id}");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        let mut killed = false;
        while std::time::Instant::now() < deadline {
            if let Some(row) = admin.query_opt("SELECT pid FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock'", &[&application]).unwrap() {
                let pid: i32 = row.get(0);
                killed = admin.query_one("SELECT pg_terminate_backend($1)", &[&pid]).unwrap().get(0);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        held.rollback().unwrap();
        let events = worker.join().unwrap();
        let result = events.iter().find(|e| e["event"] == "result" || e["event"] == "error").cloned().unwrap();
        if let Ok(dir) = std::env::var("RVI_EVIDENCE") {
            let _ = std::fs::create_dir_all(&dir);
            std::fs::write(std::path::Path::new(&dir).join("rvi2_pg_live.json"), serde_json::to_vec_pretty(&result).unwrap()).unwrap();
        }
        let journal:Value=serde_json::from_slice(&std::fs::read(result["report_path"].as_str().unwrap()).unwrap()).unwrap();
        assert_eq!(journal["message"],crate::safe_promotion::CUTOVER_UNKNOWN_MESSAGE);
        assert_eq!(journal["error"],result["error"]);
        let attempt=crate::safe_promotion::prior_attempt(&dir.join(format!(".tunnelforge_promotion_plan_{restore_id}"))).unwrap().unwrap();
        assert_eq!(attempt["result"]["error"],result["error"]);
        let listed=run("restore.backups",json!({"endpoint":base,"input_dirs":[dir],"action":"list"}));
        assert!(listed["backups"].as_array().unwrap().iter().any(|e|e["restore_id"]==restore_id && e["journal_status"]=="cutover_unknown"),"{listed:#}");
        let reconciled=run("restore.backups",json!({"endpoint":base,"input_dirs":[dir],"restore_id":restore_id,"action":"reconcile"}));
        assert_eq!(reconciled["success"],true);
        cleanup(&base, &original, &candidate, planned["backup_namespace"].as_str(), &dir);
        eprintln!("RVI2 killed={killed} event={} status={} original_unchanged={} error={} error_code={}\nmessage={}",
            result["event"], result["status"], result["original_unchanged"], result["error"], result["error_code"], result["message"]);
        assert!(killed);
        assert_eq!(result["event"], "result", "{result:#}");
        assert_eq!(result["status"],"cutover_unknown","{result:#}");
        assert_eq!(result["message"],crate::safe_promotion::CUTOVER_UNKNOWN_MESSAGE);
        assert!(result["error"].as_str().is_some_and(|e|!e.is_empty() && e!=crate::safe_promotion::CUTOVER_UNKNOWN_MESSAGE));
    }
