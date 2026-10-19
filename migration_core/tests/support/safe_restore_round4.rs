/// claim [6]: scheduled rehearsal (verify_only) of a dump with a trigger, non-SUPER account,
/// log_bin=ON and log_bin_trust_function_creators=0.
#[test]
fn rvh6_rehearsal_trigger_binlog_refusal() {
    let Some((mut admin, original, name, root)) = exported() else { return };
    let mut db = LiveAdapter::connect(&original).unwrap();
    db.execute_sql("CREATE TRIGGER guard BEFORE INSERT ON parents FOR EACH ROW SET NEW.value=NEW.value").unwrap();
    let output = root.join("programs");
    dump(&original, &output);
    db.execute_sql("DROP TRIGGER guard").unwrap();
    let user = format!("rvh6_{}", &name[name.len() - 8..]);
    let _users = Users(vec![user.clone()]);
    for sql in [
        format!("CREATE USER '{user}'@'%' IDENTIFIED BY 'tf_local_test'"),
        format!("GRANT SELECT, SHOW VIEW, PROCESS ON *.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `{name}`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_restore\\_%`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_backup\\_%`.* TO '{user}'@'%'"),
        format!("GRANT ALL ON `tf\\_promote\\_%`.* TO '{user}'@'%'"),
    ] { admin.execute_sql(&sql).unwrap(); }
    let _trust = trust_guard(&mut admin, 0);
    let ep = Endpoint { user: user.clone(), ..original.clone() };
    let log_bin = first_value(&ep, "SELECT CAST(@@log_bin AS CHAR) AS v");
    let trust = first_value(&ep, "SELECT CAST(@@log_bin_trust_function_creators AS CHAR) AS v");
    let grants = run("query.execute", json!({"connection":ep,"sql":"SHOW GRANTS"}));
    let rehearsal = import(&ep, &output, json!({"verify_only":true}));
    save_json("rvh6_rehearsal.json", &rehearsal);
    for c in [candidate_of(&rehearsal)].into_iter().flatten() { if c != name { drop_db(&mut admin, &c); } }
    drop_db(&mut admin, &name);
    std::fs::remove_dir_all(root).unwrap();
    eprintln!("RVH6 log_bin={log_bin} trust={trust}\ngrants={:?}\nrehearsal status={} copy_blockers={} message={}", grants.iter().filter_map(|e| e.get("rows")).collect::<Vec<_>>(), rehearsal["status"], rehearsal["copy_blockers"], rehearsal["message"]);
    assert_eq!(rehearsal["status"], "failed_original_untouched", "{rehearsal:#}");
    assert!(codes(&rehearsal["copy_blockers"]).iter().any(|c| c.starts_with("trigger_binlog_privilege")), "{rehearsal:#}");
}
