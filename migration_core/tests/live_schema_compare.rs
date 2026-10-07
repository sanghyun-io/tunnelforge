//! schema.compare 실 MySQL 검증: 차이를 찾고, 생성한 동기화 SQL 을 타겟에 실행하면
//! 다시 비교했을 때 차이가 없어야 한다.
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

fn compare(source: &Endpoint, target: &Endpoint) -> Value {
    let events = handle_request(Request {
        command: "schema.compare".into(),
        request_id: Some("cmp".into()),
        payload: json!({"source": source, "target": target, "level": "strict", "exact_row_counts": true}),
    });
    assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    assert!(events.iter().any(|e| e["event"] == "progress"));
    events.into_iter().find(|e| e["event"] == "result").unwrap()
}

/// 주석 줄과 줄 끝 주석을 빼고 문장 단위로 나눈다.
fn statements(sql: &str) -> Vec<String> {
    let cleaned: Vec<&str> = sql
        .lines()
        .filter(|line| !line.trim_start().starts_with("--"))
        .map(|line| match line.find("; --") {
            Some(end) => &line[..=end],
            None => line,
        })
        .collect();
    cleaned
        .join("\n")
        .split(";\n")
        .map(|s| s.trim().trim_end_matches(';').trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

fn table<'a>(result: &'a Value, name: &str) -> &'a Value {
    result["tables"].as_array().unwrap().iter().find(|t| t["name"] == name).unwrap_or_else(|| panic!("{name} missing"))
}

#[test]
fn mysql_schema_compare_finds_differences_and_its_sync_sql_converges() {
    let Some(root) = endpoint("tf_test") else { return };
    let opts = mysql::OptsBuilder::new()
        .ip_or_hostname(Some(root.host.clone()))
        .user(Some(root.user.clone()))
        .pass(Some(root.password.clone()));
    let mut conn = mysql::Pool::new(opts).unwrap().get_conn().unwrap();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let (src, tgt) = (format!("tf_cmp_src_{stamp}"), format!("tf_cmp_tgt_{stamp}"));
    for db in [&src, &tgt] {
        conn.query_drop(format!("CREATE DATABASE `{db}`")).unwrap();
    }

    conn.query_drop(format!("USE `{src}`")).unwrap();
    for ddl in [
        "CREATE TABLE parent (id INT PRIMARY KEY)",
        "CREATE TABLE child (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            parent_id INT,
            name VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT 'it''s' COMMENT 'a ''q''',
            created DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            body TEXT,
            total INT AS (CHAR_LENGTH(name)) STORED,
            KEY ix_name (name(10)),
            FULLTEXT KEY ft_body (body),
            CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent (id) ON DELETE CASCADE
        )",
        "CREATE TABLE only_src (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (b, a))",
        "CREATE TABLE pk_change (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (a, b))",
        "INSERT INTO parent VALUES (1), (2)",
    ] {
        conn.query_drop(ddl).unwrap();
    }

    conn.query_drop(format!("USE `{tgt}`")).unwrap();
    for ddl in [
        "CREATE TABLE parent (id INT PRIMARY KEY)",
        "CREATE TABLE child (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            parent_id INT,
            name VARCHAR(20) NOT NULL,
            created DATETIME,
            KEY ix_name_old (name(10)),
            CONSTRAINT fk_old FOREIGN KEY (parent_id) REFERENCES parent (id) ON DELETE CASCADE
        )",
        "CREATE TABLE only_tgt (id INT PRIMARY KEY)",
        "CREATE TABLE pk_change (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (a))",
    ] {
        conn.query_drop(ddl).unwrap();
    }

    let (source, target) = (endpoint(&src).unwrap(), endpoint(&tgt).unwrap());
    let first = compare(&source, &target);
    assert_eq!(first["success"], true);
    assert_eq!(table(&first, "only_src")["diff_type"], "added");
    assert_eq!(table(&first, "only_tgt")["diff_type"], "removed");
    assert_eq!(table(&first, "parent")["diff_type"], "unchanged");
    assert_eq!(table(&first, "parent")["row_count_source"], 2);
    let child = table(&first, "child");
    assert_eq!(child["diff_type"], "modified");
    let index = child["indexes"].as_array().unwrap().iter().find(|i| i["name"] == "ix_name").unwrap();
    assert_eq!((index["diff_type"].as_str(), index["old_name"].as_str()), (Some("renamed"), Some("ix_name_old")));
    let fk = child["foreign_keys"].as_array().unwrap().iter().find(|f| f["name"] == "fk_child_parent").unwrap();
    assert_eq!(fk["diff_type"], "renamed");
    assert!(first["summary"]["critical"].as_u64().unwrap() > 0);
    let pk = table(&first, "pk_change")["indexes"].as_array().unwrap().iter().find(|i| i["name"] == "PRIMARY").unwrap().clone();
    assert_eq!((pk["diff_type"].as_str(), pk["severity"].as_str()), (Some("modified"), Some("critical")));

    let sync_sql = first["sync_sql"].as_str().unwrap();
    for statement in statements(sync_sql) {
        conn.query_drop(&statement).unwrap_or_else(|err| panic!("sync statement failed: {err}\n{statement}\n--- full script ---\n{sync_sql}"));
    }

    let second = compare(&source, &target);
    let leftovers: Vec<&Value> = second["tables"].as_array().unwrap().iter().filter(|t| t["diff_type"] != "unchanged").collect();
    assert!(leftovers.is_empty(), "differences remain after sync: {leftovers:#?}\n--- script ---\n{sync_sql}");
    assert_eq!(second["summary"], json!({"critical": 0, "warning": 0, "info": 0}));

    for db in [&src, &tgt] {
        conn.query_drop(format!("DROP DATABASE `{db}`")).unwrap();
    }
}

#[test]
fn mysql_schema_compare_fails_instead_of_reporting_an_empty_schema() {
    let Some(mut missing) = endpoint("tf_test") else { return };
    missing.password = "definitely-wrong-password".into();
    let ok = endpoint("tf_test").unwrap();
    let events = handle_request(Request {
        command: "schema.compare".into(),
        request_id: None,
        payload: json!({"source": missing, "target": ok}),
    });
    assert!(events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    assert!(!events.iter().any(|e| e["event"] == "result"));
}
