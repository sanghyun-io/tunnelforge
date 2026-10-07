//! upgrade.analyze 실 MySQL 검증: 호환성 이슈와 (복합 FK 포함) 고아 레코드를 찾고,
//! 함께 돌려주는 정리용 COUNT SQL 이 실제로 같은 건수를 센다.
use migration_core::{handle_request, Endpoint, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};

fn endpoint(database: &str) -> Option<Endpoint> {
    Some(Endpoint {
        engine: "mysql".into(),
        host: std::env::var("TF_MYSQL_HOST").ok()?,
        port: 3306,
        user: std::env::var("TF_MYSQL_USER").unwrap_or_else(|_| "root".into()),
        password: std::env::var("TF_MYSQL_PASSWORD").unwrap_or_else(|_| "tf_local_test".into()),
        database: database.into(),
        schema: None,
        tls: Default::default(),
    })
}

fn has_issue(result: &Value, issue_type: &str, location: &str) -> bool {
    result["issues"].as_array().unwrap().iter().any(|i| i["issue_type"] == issue_type && i["location"] == location)
}

#[test]
fn mysql_upgrade_analyze_reports_issues_and_composite_fk_orphans() {
    let Some(root) = endpoint("tf_test") else { return };
    let opts = mysql::OptsBuilder::new()
        .ip_or_hostname(Some(root.host.clone()))
        .user(Some(root.user.clone()))
        .pass(Some(root.password.clone()));
    let mut conn = mysql::Pool::new(opts).unwrap().get_conn().unwrap();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let db = format!("tf_upg_{stamp}");
    conn.query_drop(format!("CREATE DATABASE `{db}`")).unwrap();
    conn.query_drop(format!("USE `{db}`")).unwrap();
    conn.query_drop("SET SESSION sql_mode = ''").unwrap();
    conn.query_drop("SET SESSION foreign_key_checks = 0").unwrap();
    for ddl in [
        "CREATE TABLE `rank` (id INT PRIMARY KEY, `window` VARCHAR(10)) ENGINE=MyISAM CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci",
        "CREATE TABLE odd_types (id INT PRIMARY KEY, z INT(6) UNSIGNED ZEROFILL, f FLOAT(7,2), e ENUM('', 'a'), ts TIMESTAMP NULL, d DATE)",
        "INSERT INTO odd_types VALUES (1, 1, 1.5, 'a', NULL, '0000-00-00'), (2, 2, 2.5, '', NULL, '2024-01-01')",
        "CREATE TABLE parent (x INT NOT NULL, y INT NOT NULL, PRIMARY KEY (x, y))",
        "CREATE TABLE child (id INT PRIMARY KEY, a INT, b INT, CONSTRAINT fk_pair FOREIGN KEY (a, b) REFERENCES parent (x, y))",
        "INSERT INTO parent VALUES (1, 1), (2, 9)",
        // (1,9): a=1 과 b=9 는 각각 부모에 있지만 쌍으로는 없다 → 고아. (NULL,9) 는 MySQL 이 검사하지 않는다.
        "INSERT INTO child VALUES (1, 1, 1), (2, 1, 9), (3, NULL, 9)",
        "CREATE PROCEDURE uses_found_rows() BEGIN SELECT SQL_CALC_FOUND_ROWS id FROM parent LIMIT 1; SELECT FOUND_ROWS(); END",
    ] {
        conn.query_drop(ddl).unwrap_or_else(|err| panic!("{err}: {ddl}"));
    }

    let events = handle_request(Request {
        command: "upgrade.analyze".into(),
        request_id: Some("upg".into()),
        payload: json!({"connection": endpoint(&db).unwrap()}),
    });
    assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    let progress: Vec<&str> = events.iter().filter(|e| e["event"] == "progress").filter_map(|e| e["message"].as_str()).collect();
    assert!(progress.contains(&"📌 [1/15] 고아 레코드 검사 시작..."), "{progress:#?}");
    assert!(progress.contains(&"✅ 분석 완료"));
    let result = events.into_iter().find(|e| e["event"] == "result").unwrap();

    assert_eq!(result["total_tables"], 4);
    assert_eq!(result["total_fk_relations"], 2);
    assert_eq!(result["fk_tree"]["parent"], json!(["child"]));
    for (issue_type, location) in [
        ("charset_issue", format!("{db}.rank")),
        ("charset_issue", format!("{db}.rank.window")),
        ("reserved_keyword", format!("{db}.rank")),
        ("reserved_keyword", format!("{db}.rank.window")),
        ("deprecated_engine", format!("{db}.rank")),
        ("zerofill_usage", format!("{db}.odd_types.z")),
        ("float_precision", format!("{db}.odd_types.f")),
        ("enum_empty_value", format!("{db}.odd_types.e")),
        ("timestamp_range", format!("{db}.odd_types.ts")),
        ("invalid_date", format!("{db}.odd_types.d")),
        ("deprecated_function", format!("PROCEDURE {db}.uses_found_rows")),
    ] {
        assert!(has_issue(&result, issue_type, &location), "missing {issue_type} at {location}: {:#}", result["issues"]);
    }

    let orphans = result["orphan_records"].as_array().unwrap();
    assert_eq!(orphans.len(), 1, "{orphans:#?}");
    let orphan = &orphans[0];
    assert_eq!(orphan["orphan_count"], 1);
    assert_eq!(orphan["child_columns"], json!(["a", "b"]));
    assert_eq!(orphan["child_column"], "a, b");
    assert_eq!(orphan["sample_values"], json!(["(1, 9)"]));
    let count: u64 = conn.query_first(orphan["cleanup_sql"]["count"].as_str().unwrap()).unwrap().unwrap();
    assert_eq!(count, 1, "cleanup COUNT must match the detected orphans");

    conn.query_drop(format!("DROP DATABASE `{db}`")).unwrap();
}

#[test]
fn mysql_upgrade_analyze_fails_on_connection_errors() {
    let Some(mut bad) = endpoint("tf_test") else { return };
    bad.password = "definitely-wrong-password".into();
    let events = handle_request(Request {
        command: "upgrade.analyze".into(),
        request_id: None,
        payload: json!({"connection": bad}),
    });
    assert!(events.iter().any(|e| e["event"] == "error"), "{events:#?}");
}
