//! upgrade.fix_plan / upgrade.charset_sql 실 MySQL 검증: 계획이 FK 연관 테이블까지 잡고,
//! FK 안전 문자셋 변환 SQL 을 실행하면 모든 테이블이 utf8mb4 가 되고 FK 가 그대로 남는다.
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

fn run(command: &str, payload: Value) -> Value {
    let events = handle_request(Request { command: command.into(), request_id: None, payload });
    assert!(!events.iter().any(|e| e["event"] == "error"), "{events:#?}");
    events.into_iter().find(|e| e["event"] == "result").unwrap()
}

#[test]
fn mysql_fix_plan_and_fk_safe_charset_sql_converge() {
    let Some(root) = endpoint("tf_test") else { return };
    let opts = mysql::OptsBuilder::new()
        .ip_or_hostname(Some(root.host.clone()))
        .user(Some(root.user.clone()))
        .pass(Some(root.password.clone()));
    let mut conn = mysql::Pool::new(opts).unwrap().get_conn().unwrap();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    let db = format!("tf_fix_{stamp}");
    conn.query_drop(format!("CREATE DATABASE `{db}`")).unwrap();
    conn.query_drop(format!("USE `{db}`")).unwrap();
    conn.query_drop("SET SESSION sql_mode = ''").unwrap();
    for ddl in [
        "CREATE TABLE parent (code VARCHAR(10) PRIMARY KEY) CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci",
        "CREATE TABLE child (id INT PRIMARY KEY, code VARCHAR(10), d DATE NULL, \
         CONSTRAINT fk_code FOREIGN KEY (code) REFERENCES parent (code) ON DELETE CASCADE) CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci",
        "INSERT INTO parent VALUES ('a')",
        "INSERT INTO child VALUES (1, 'a', '0000-00-00'), (2, 'a', '2024-01-01')",
    ] {
        conn.query_drop(ddl).unwrap_or_else(|err| panic!("{err}: {ddl}"));
    }
    let ep = endpoint(&db).unwrap();

    let plan = run(
        "upgrade.fix_plan",
        json!({
            "connection": ep,
            "issues": [
                {"issue_type": "invalid_date", "location": format!("{db}.child.d"), "table_name": "child", "column_name": "d", "description": "x"},
                {"issue_type": "deprecated_engine", "location": format!("{db}.child"), "table_name": "child", "description": "y"},
            ],
            "charset_tables": ["child"],
        }),
    );
    let date_options = plan["steps"][0]["options"].as_array().unwrap();
    assert_eq!(date_options[0]["strategy"], "date_to_null", "nullable column recommends NULL");
    assert_eq!(date_options[0]["estimated_rows"], 1);
    assert_eq!(date_options.last().unwrap()["strategy"], "skip");
    let tables = plan["charset_tables"].as_array().unwrap();
    let names: Vec<&str> = tables.iter().map(|t| t["table_name"].as_str().unwrap()).collect();
    assert_eq!(names, vec!["parent", "child"], "FK-related parent is included, parents first");
    assert_eq!(tables[0]["is_original_issue"], false);
    assert_eq!(tables[0]["current_charset"], "utf8mb3");
    assert_eq!(plan["cascade_skip"]["child"], json!(["parent"]));
    assert_eq!(plan["foreign_keys"][0]["constraint_name"], "fk_code");

    let parts = run("upgrade.charset_sql", json!({"connection": ep, "tables": ["parent", "child"]}));
    assert_eq!(parts["fk_count"], 1);
    for statement in parts["full_sql"].as_array().unwrap().iter().filter_map(Value::as_str) {
        if statement.is_empty() || statement.starts_with("--") {
            continue;
        }
        conn.query_drop(statement).unwrap_or_else(|err| panic!("{err}: {statement}"));
    }
    let collations: Vec<String> = conn
        .exec("SELECT TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME", (&db,))
        .unwrap();
    assert_eq!(collations, vec!["utf8mb4_unicode_ci".to_string(), "utf8mb4_unicode_ci".to_string()]);
    let fk_rule: String = conn
        .exec_first(
            "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = ? AND CONSTRAINT_NAME = 'fk_code'",
            (&db,),
        )
        .unwrap()
        .expect("foreign key re-created");
    assert_eq!(fk_rule, "CASCADE");

    conn.query_drop(format!("DROP DATABASE `{db}`")).unwrap();
}
