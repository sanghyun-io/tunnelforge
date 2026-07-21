use migration_core::{handle_request, Endpoint, Request};
use mysql::prelude::Queryable;
use serde_json::{json, Value};
use std::env;
use std::sync::{Mutex, MutexGuard};

static LIVE_DB_TEST_LOCK: Mutex<()> = Mutex::new(());

fn live_db_test_guard() -> MutexGuard<'static, ()> {
    LIVE_DB_TEST_LOCK.lock().unwrap_or_else(|err| err.into_inner())
}

fn endpoint(prefix: &str, default_port: u16, engine: &str) -> Option<Endpoint> {
    let host = env::var(format!("{prefix}_HOST")).ok()?;
    let user = env::var(format!("{prefix}_USER")).ok()?;
    let database = env::var(format!("{prefix}_DATABASE")).ok()?;
    let password = env::var(format!("{prefix}_PASSWORD")).unwrap_or_default();
    let port = env::var(format!("{prefix}_PORT"))
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(default_port);

    Some(Endpoint {
        engine: engine.to_string(),
        host,
        port,
        user,
        password,
        database,
        schema: None,
    })
}

fn test_endpoints() -> Option<(Endpoint, Endpoint)> {
    Some((
        endpoint("TF_MYSQL", 3306, "mysql")?,
        endpoint("TF_POSTGRES", 5432, "postgresql")?,
    ))
}

fn unique_table(prefix: &str) -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis();
    format!("{prefix}_{now}")
}

fn mysql_conn(endpoint: &Endpoint) -> mysql::PooledConn {
    let opts = mysql::OptsBuilder::new()
        .ip_or_hostname(Some(endpoint.host.clone()))
        .tcp_port(endpoint.port)
        .user(Some(endpoint.user.clone()))
        .pass(Some(endpoint.password.clone()))
        .db_name(Some(endpoint.database.clone()));
    mysql::Pool::new(opts).unwrap().get_conn().unwrap()
}

fn postgres_client(endpoint: &Endpoint) -> postgres::Client {
    let mut config = postgres::Config::new();
    config
        .host(&endpoint.host)
        .port(endpoint.port)
        .user(&endpoint.user)
        .password(&endpoint.password)
        .dbname(&endpoint.database);
    config.connect(postgres::NoTls).unwrap()
}

fn result_payload(events: Vec<Value>) -> Value {
    events
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap_or_else(|| panic!("missing result event"))
}

fn endpoint_json(endpoint: &Endpoint) -> Value {
    json!({
        "engine": endpoint.engine,
        "host": endpoint.host,
        "port": endpoint.port,
        "user": endpoint.user,
        "password": endpoint.password,
        "database": endpoint.database,
        "schema": endpoint.schema
    })
}

fn postgres_index_exists(client: &mut postgres::Client, table: &str, index: &str) -> bool {
    client
        .query_one(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = $1 AND indexname = $2)",
            &[&table, &index],
        )
        .map(|row| row.get::<_, bool>(0))
        .unwrap_or(false)
}

fn mysql_index_exists(
    conn: &mut mysql::PooledConn,
    database: &str,
    table: &str,
    index: &str,
) -> bool {
    conn.exec_first::<u64, _, _>(
        "SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_NAME = ?",
        (database, table, index),
    )
    .unwrap_or(Some(0))
    .unwrap_or(0)
        > 0
}

fn mysql_table_engine(conn: &mut mysql::PooledConn, database: &str, table: &str) -> Option<String> {
    conn.exec_first::<String, _, _>(
        "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        (database, table),
    )
    .unwrap()
}

fn mysql_table_charset_collation(
    conn: &mut mysql::PooledConn,
    database: &str,
    table: &str,
) -> Option<(String, String)> {
    conn.exec_first::<(String, String), _, _>(
        "SELECT ccsa.CHARACTER_SET_NAME, t.TABLE_COLLATION \
         FROM information_schema.TABLES t \
         JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY ccsa \
         ON t.TABLE_COLLATION = ccsa.COLLATION_NAME \
         WHERE t.TABLE_SCHEMA = ? AND t.TABLE_NAME = ?",
        (database, table),
    )
    .unwrap()
}

#[test]
fn mysql_dump_import_preserves_compatible_target_only_fk_table_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some(mysql_endpoint) = endpoint("TF_MYSQL", 3306, "mysql") else {
        eprintln!("skipping MySQL dump/import FK preservation: TF_MYSQL_* is not configured");
        return;
    };
    let parent = unique_table("tf_dump_parent");
    let child = unique_table("tf_target_only_child");
    let dump_dir = std::env::temp_dir().join(unique_table("tf_mysql_dump"));
    let mut mysql = mysql_conn(&mysql_endpoint);
    mysql
        .query_drop(format!(
            "CREATE TABLE `{parent}` (`id` BIGINT UNSIGNED NOT NULL PRIMARY KEY, `value` VARCHAR(32) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
        ))
        .unwrap();
    mysql
        .query_drop(format!("INSERT INTO `{parent}` VALUES (1, 'from_dump')"))
        .unwrap();

    let export = result_payload(handle_request(Request {
        command: "dump.run".to_string(),
        request_id: None,
        payload: json!({
            "source": endpoint_json(&mysql_endpoint),
            "output_dir": dump_dir.to_string_lossy(),
            "threads": 8,
            "chunk_size": 1000,
            "data_format": "tsv",
            "compression": "none",
            "tables": [parent]
        }),
    }));
    assert_eq!(export["success"], true);
    assert_eq!(export["snapshot_policy"], "mysql_shared_consistent_snapshot");
    assert_eq!(export["strict_export"], true);

    mysql
        .query_drop(format!(
            "CREATE TABLE `{child}` (`id` BIGINT UNSIGNED NOT NULL PRIMARY KEY, `parent_id` BIGINT UNSIGNED NOT NULL, CONSTRAINT `fk_target_only_parent` FOREIGN KEY (`parent_id`) REFERENCES `{parent}` (`id`)) ENGINE=InnoDB"
        ))
        .unwrap();
    mysql
        .query_drop(format!("INSERT INTO `{child}` VALUES (1, 1)"))
        .unwrap();
    mysql
        .query_drop(format!("UPDATE `{parent}` SET `value`='changed' WHERE `id`=1"))
        .unwrap();

    let import = result_payload(handle_request(Request {
        command: "dump.import".to_string(),
        request_id: None,
        payload: json!({
            "target": endpoint_json(&mysql_endpoint),
            "input_dir": dump_dir.to_string_lossy(),
            "mode": "replace",
            "threads": 8,
            "strict_manifest": true
        }),
    }));
    assert_eq!(import["success"], true);
    assert_eq!(
        mysql
            .query_first::<String, _>(format!("SELECT `value` FROM `{parent}` WHERE `id`=1"))
            .unwrap()
            .as_deref(),
        Some("from_dump")
    );
    assert_eq!(
        mysql
            .query_first::<u64, _>(format!("SELECT COUNT(*) FROM `{child}`"))
            .unwrap(),
        Some(1)
    );
    assert_eq!(
        mysql
            .exec_first::<u64, _, _>(
                "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = ? AND TABLE_NAME = ? AND CONSTRAINT_NAME = 'fk_target_only_parent'",
                (&mysql_endpoint.database, &child),
            )
            .unwrap(),
        Some(1)
    );

    mysql.query_drop("SET SESSION foreign_key_checks=0").unwrap();
    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{child}`, `{parent}`"))
        .unwrap();
    mysql.query_drop("SET SESSION foreign_key_checks=1").unwrap();
    std::fs::remove_dir_all(&dump_dir).unwrap();
}

fn unsupported_objects_from_inspect(endpoint: &Endpoint) -> Vec<String> {
    let inspect = result_payload(handle_request(Request {
        command: "inspect".to_string(),
        request_id: None,
        payload: json!({"source": endpoint_json(endpoint)}),
    }));
    inspect["unsupported_objects"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|value| value.as_str().map(ToString::to_string))
        .collect()
}

#[test]
fn live_inspect_reports_views_as_unsupported_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some((mysql_endpoint, postgres_endpoint)) = test_endpoints() else {
        eprintln!("skipping live inspect: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };

    let mysql_table = unique_table("tf_mysql_view_base");
    let mysql_view = unique_table("tf_mysql_view");
    let pg_table = unique_table("tf_pg_view_base");
    let pg_view = unique_table("tf_pg_view");
    let mut mysql = mysql_conn(&mysql_endpoint);
    let mut postgres = postgres_client(&postgres_endpoint);

    mysql
        .query_drop(format!("DROP VIEW IF EXISTS `{mysql_view}`"))
        .unwrap();
    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{mysql_table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP VIEW IF EXISTS \"{pg_view}\""))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{pg_table}\""))
        .unwrap();

    mysql
        .query_drop(format!(
            "CREATE TABLE `{mysql_table}` (`id` INT PRIMARY KEY)"
        ))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE VIEW `{mysql_view}` AS SELECT `id` FROM `{mysql_table}`"
        ))
        .unwrap();
    postgres
        .batch_execute(&format!(
            "CREATE TABLE \"{pg_table}\" (\"id\" INTEGER PRIMARY KEY);
             CREATE VIEW \"{pg_view}\" AS SELECT \"id\" FROM \"{pg_table}\";"
        ))
        .unwrap();

    let mysql_objects = unsupported_objects_from_inspect(&mysql_endpoint);
    let pg_objects = unsupported_objects_from_inspect(&postgres_endpoint);

    assert!(mysql_objects.contains(&format!("view:{mysql_view}")));
    assert!(pg_objects.contains(&format!("view:{pg_view}")));

    mysql
        .query_drop(format!("DROP VIEW IF EXISTS `{mysql_view}`"))
        .unwrap();
    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{mysql_table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP VIEW IF EXISTS \"{pg_view}\""))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{pg_table}\""))
        .unwrap();
}

#[test]
fn live_preflight_blocks_non_empty_target_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some((_mysql_endpoint, postgres_endpoint)) = test_endpoints() else {
        eprintln!("skipping live preflight: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };

    let table = unique_table("tf_preflight_target");
    let mut postgres = postgres_client(&postgres_endpoint);
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{table}\""))
        .unwrap();
    postgres
        .batch_execute(&format!(
            "CREATE TABLE \"{table}\" (\"id\" INTEGER PRIMARY KEY);
             INSERT INTO \"{table}\" (\"id\") VALUES (1);"
        ))
        .unwrap();

    let result = result_payload(handle_request(Request {
        command: "preflight".to_string(),
        request_id: None,
        payload: json!({
            "source_engine": "mysql",
            "target_engine": "postgresql",
            "target": endpoint_json(&postgres_endpoint),
            "schema": {
                "tables": [{
                    "name": table,
                    "columns": [{"name": "id", "type": "int", "primary_key": true}]
                }]
            },
            "execution_options": {"mode": "create_only"}
        }),
    }));

    assert_eq!(result["success"], false);
    assert!(result["issues"].as_array().unwrap().iter().any(|issue| {
        issue["blocking"] == true && issue["message"] == "target table is not empty"
    }));

    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{table}\""))
        .unwrap();
}

#[test]
fn live_readiness_reports_each_direction_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some((mysql_endpoint, postgres_endpoint)) = test_endpoints() else {
        eprintln!("skipping live readiness: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };

    let mysql_table = unique_table("tf_ready_mysql");
    let pg_table = unique_table("tf_ready_pg");
    let mut mysql = mysql_conn(&mysql_endpoint);
    let mut postgres = postgres_client(&postgres_endpoint);

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{mysql_table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{pg_table}\""))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE TABLE `{mysql_table}` (`id` INT PRIMARY KEY)"
        ))
        .unwrap();
    postgres
        .batch_execute(&format!(
            "CREATE TABLE \"{pg_table}\" (\"id\" INTEGER PRIMARY KEY)"
        ))
        .unwrap();

    let result = result_payload(handle_request(Request {
        command: "readiness".to_string(),
        request_id: None,
        payload: json!({
            "source": endpoint_json(&mysql_endpoint),
            "target": endpoint_json(&postgres_endpoint),
            "execution_options": {"mode": "append"}
        }),
    }));

    assert_eq!(result["command"], "readiness");
    let directions = result["directions"].as_array().unwrap();
    assert_eq!(directions.len(), 2);
    assert!(directions
        .iter()
        .any(|direction| direction["direction"] == "mysql_to_postgresql"));
    assert!(directions
        .iter()
        .any(|direction| direction["direction"] == "postgresql_to_mysql"));
    assert!(directions
        .iter()
        .all(|direction| direction["table_count"].as_u64().unwrap_or(0) >= 1));

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{mysql_table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{pg_table}\""))
        .unwrap();
}

#[test]
fn oneclick_run_live_engine_innodb_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some(mysql_endpoint) = endpoint("TF_MYSQL", 3306, "mysql") else {
        eprintln!("skipping oneclick live apply: TF_MYSQL_* is not configured");
        return;
    };

    let table = unique_table("tf_oneclick_engine");
    let mut mysql = mysql_conn(&mysql_endpoint);
    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE TABLE `{table}` (`id` INT PRIMARY KEY, `name` VARCHAR(32) NOT NULL) ENGINE=MyISAM"
        ))
        .unwrap();
    assert_eq!(
        mysql_table_engine(&mut mysql, &mysql_endpoint.database, &table).as_deref(),
        Some("MyISAM")
    );

    let result = result_payload(handle_request(Request {
        command: "oneclick.run".to_string(),
        request_id: None,
        payload: json!({
            "connection": endpoint_json(&mysql_endpoint),
            "schema": mysql_endpoint.database,
            "dry_run": false,
            "backup_confirmed": true
        }),
    }));

    assert_eq!(result["success"], true);
    assert_eq!(
        mysql_table_engine(&mut mysql, &mysql_endpoint.database, &table).as_deref(),
        Some("InnoDB")
    );

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
}

#[test]
fn oneclick_run_live_charset_contract_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some(base_endpoint) = endpoint("TF_MYSQL", 3306, "mysql") else {
        eprintln!("skipping oneclick charset live apply: TF_MYSQL_* is not configured");
        return;
    };

    let schema = "tf_oneclick_run_charset";
    let parent = unique_table("tf_oneclick_parent");
    let child = unique_table("tf_oneclick_child");
    let mut admin = mysql_conn(&base_endpoint);
    admin
        .query_drop(format!("DROP DATABASE IF EXISTS `{schema}`"))
        .unwrap();
    admin
        .query_drop(format!(
            "CREATE DATABASE `{schema}` CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci"
        ))
        .unwrap();

    let mut charset_endpoint = base_endpoint.clone();
    charset_endpoint.database = schema.to_string();
    let mut mysql = mysql_conn(&charset_endpoint);
    mysql
        .query_drop(format!(
            "CREATE TABLE `{parent}` (`id` INT NOT NULL PRIMARY KEY, `name` VARCHAR(32) NOT NULL) \
             ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci"
        ))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE TABLE `{child}` (`id` INT NOT NULL PRIMARY KEY, `parent_id` INT NOT NULL, \
             `name` VARCHAR(32) NOT NULL, CONSTRAINT `fk_child_parent` FOREIGN KEY (`parent_id`) \
             REFERENCES `{parent}` (`id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci"
        ))
        .unwrap();

    assert_eq!(
        mysql_table_charset_collation(&mut mysql, schema, &parent).as_ref(),
        Some(&(String::from("utf8mb3"), String::from("utf8mb3_general_ci")))
    );

    let result = result_payload(handle_request(Request {
        command: "oneclick.run".to_string(),
        request_id: None,
        payload: json!({
            "connection": endpoint_json(&charset_endpoint),
            "schema": schema,
            "dry_run": false,
            "backup_confirmed": true,
            "issues": [{
                "issue_type": "charset_issue",
                "severity": "warning",
                "location": format!("{schema}.{parent}"),
                "table_name": parent,
                "message": "Table uses a legacy charset.",
                "suggestion": "Convert table charset/collation after FK-safe review.",
                "blocking": false
            }],
            "charset_contracts": [{
                "issue_index": 0,
                "tables": [parent, child],
                "fk_order": [parent, child],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "rollback_sql": [
                    format!("ALTER TABLE `{schema}`.`{child}` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"),
                    format!("ALTER TABLE `{schema}`.`{parent}` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;")
                ]
            }]
        }),
    }));

    assert_eq!(result["success"], true);
    assert_eq!(
        mysql_table_charset_collation(&mut mysql, schema, &parent).as_ref(),
        Some(&(String::from("utf8mb4"), String::from("utf8mb4_0900_ai_ci")))
    );
    assert_eq!(
        mysql_table_charset_collation(&mut mysql, schema, &child).as_ref(),
        Some(&(String::from("utf8mb4"), String::from("utf8mb4_0900_ai_ci")))
    );

    admin
        .query_drop(format!("DROP DATABASE IF EXISTS `{schema}`"))
        .unwrap();
}

#[test]
fn oneclick_derive_charset_contracts_live_facts_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some(base_endpoint) = endpoint("TF_MYSQL", 3306, "mysql") else {
        eprintln!("skipping oneclick charset live derivation: TF_MYSQL_* is not configured");
        return;
    };

    let schema = "tf_oneclick_derive_charset";
    let parent = unique_table("tf_oneclick_parent");
    let child = unique_table("tf_oneclick_child");
    let mut admin = mysql_conn(&base_endpoint);
    admin
        .query_drop(format!("DROP DATABASE IF EXISTS `{schema}`"))
        .unwrap();
    admin
        .query_drop(format!(
            "CREATE DATABASE `{schema}` CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci"
        ))
        .unwrap();

    let mut charset_endpoint = base_endpoint.clone();
    charset_endpoint.database = schema.to_string();
    let mut mysql = mysql_conn(&charset_endpoint);
    mysql
        .query_drop(format!(
            "CREATE TABLE `{parent}` (`id` INT NOT NULL PRIMARY KEY, `name` VARCHAR(32) NOT NULL) \
             ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci"
        ))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE TABLE `{child}` (`id` INT NOT NULL PRIMARY KEY, `parent_id` INT NOT NULL, \
             `name` VARCHAR(32) NOT NULL, CONSTRAINT `fk_child_parent_derive` FOREIGN KEY (`parent_id`) \
             REFERENCES `{parent}` (`id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci"
        ))
        .unwrap();

    let result = result_payload(handle_request(Request {
        command: "oneclick.derive_charset_contracts".to_string(),
        request_id: None,
        payload: json!({
            "connection": endpoint_json(&charset_endpoint),
            "schema": schema,
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci"
        }),
    }));

    assert_eq!(result["success"], true);
    assert_eq!(result["issues"].as_array().unwrap().len(), 1);
    assert_eq!(result["issues"][0]["issue_type"], "charset_issue");
    assert_eq!(result["issues"][0]["table_name"], parent);
    assert_eq!(result["contracts"].as_array().unwrap().len(), 1);
    assert_eq!(result["contracts"][0]["tables"], json!([parent, child]));
    assert_eq!(
        result["contracts"][0]["rollback_sql"]
            .as_array()
            .unwrap()
            .len(),
        2
    );

    admin
        .query_drop(format!("DROP DATABASE IF EXISTS `{schema}`"))
        .unwrap();
}

#[test]
fn live_guide_includes_row_values_and_sql_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some((mysql_endpoint, postgres_endpoint)) = test_endpoints() else {
        eprintln!("skipping live guide: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };

    let table = unique_table("tf_guide_mysql");
    let mut mysql = mysql_conn(&mysql_endpoint);
    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE TABLE `{table}` (`id` INT PRIMARY KEY, `name` VARCHAR(32) NOT NULL)"
        ))
        .unwrap();
    mysql
        .query_drop(format!(
            "INSERT INTO `{table}` (`id`, `name`) VALUES (1, 'alpha')"
        ))
        .unwrap();

    let result = result_payload(handle_request(Request {
        command: "guide".to_string(),
        request_id: None,
        payload: json!({
            "source": endpoint_json(&mysql_endpoint),
            "target": endpoint_json(&postgres_endpoint),
            "execution_options": {"mode": "append"},
            "guide_options": {"row_limit": 1}
        }),
    }));

    let mysql_to_pg = result["directions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|direction| direction["direction"] == "mysql_to_postgresql")
        .unwrap();
    let guide_table = mysql_to_pg["guide"]["tables"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["table"] == table)
        .unwrap();
    assert_eq!(guide_table["row_samples"][0]["name"], "alpha");
    assert!(guide_table["insert_example_sql"]
        .as_str()
        .unwrap()
        .contains("INSERT INTO"));

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
}

#[test]
fn mysql_to_postgresql_live_roundtrip_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some((mysql_endpoint, postgres_endpoint)) = test_endpoints() else {
        eprintln!("skipping live roundtrip: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };

    let table = unique_table("tf_mysql_pg");
    let mut mysql = mysql_conn(&mysql_endpoint);
    let mut postgres = postgres_client(&postgres_endpoint);

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{table}\""))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE TABLE `{table}` (`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, `name` VARCHAR(64), `status` VARCHAR(16) NOT NULL DEFAULT 'new', `enabled` TINYINT(1) NOT NULL DEFAULT 1, `payload` BLOB NOT NULL, `amount` DECIMAL(12,4) NOT NULL, `event_date` DATE NOT NULL, `event_time` TIME NOT NULL, `created_at` DATETIME NOT NULL)"
        ))
        .unwrap();
    mysql
        .query_drop(format!(
            "CREATE INDEX `idx_{table}_name` ON `{table}` (`name`)"
        ))
        .unwrap();
    mysql
        .query_drop(format!(
            "INSERT INTO `{table}` (`id`, `name`, `enabled`, `payload`, `amount`, `event_date`, `event_time`, `created_at`) VALUES (1, 'alpha', 1, X'0001FF', 1.2300, '2026-05-14', '09:08:07', '2026-05-14 09:08:07'), (2, 'beta', 0, X'CAFE', -0.5000, '2026-05-15', '10:11:12', '2026-05-15 10:11:12')"
        ))
        .unwrap();

    let schema = json!({
        "tables": [{
            "name": table,
            "columns": [
                {"name": "id", "type": "int(11) auto_increment", "nullable": false, "primary_key": true},
                {"name": "name", "type": "varchar(64)", "nullable": true},
                {"name": "status", "type": "varchar(16)", "default": "new", "nullable": false},
                {"name": "enabled", "type": "tinyint(1)", "default": "1", "nullable": false},
                {"name": "payload", "type": "blob", "nullable": false},
                {"name": "amount", "type": "decimal(12,4)", "nullable": false},
                {"name": "event_date", "type": "date", "nullable": false},
                {"name": "event_time", "type": "time", "nullable": false},
                {"name": "created_at", "type": "datetime", "nullable": false}
            ],
            "indexes": [
                {"name": format!("idx_{table}_name"), "columns": ["name"], "unique": false}
            ]
        }]
    });
    let payload = json!({
        "source_engine": "mysql",
        "target_engine": "postgresql",
        "source": endpoint_json(&mysql_endpoint),
        "target": endpoint_json(&postgres_endpoint),
        "schema": schema,
        "execution_options": {"mode": "create_only", "chunk_size": 1}
    });

    let migrate = result_payload(handle_request(Request {
        command: "migrate".to_string(),
        request_id: None,
        payload: payload.clone(),
    }));
    assert_eq!(migrate["success"], true);
    assert_eq!(migrate["rows_copied"], 2);

    let verify = result_payload(handle_request(Request {
        command: "verify".to_string(),
        request_id: None,
        payload,
    }));
    assert_eq!(verify["success"], true);
    assert!(postgres_index_exists(
        &mut postgres,
        &table,
        &format!("idx_{table}_name")
    ));
    postgres
        .execute(
            &format!(
                "INSERT INTO \"{table}\" (\"name\", \"enabled\", \"payload\", \"amount\", \"event_date\", \"event_time\", \"created_at\") VALUES ('gamma', TRUE, decode('beef', 'hex'), 3.0000, '2026-05-16', '11:12:13', '2026-05-16 11:12:13')"
            ),
            &[],
        )
        .unwrap();
    let next_id: i32 = postgres
        .query_one(
            &format!("SELECT \"id\" FROM \"{table}\" WHERE \"name\" = 'gamma'"),
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(next_id, 3);
    let status: String = postgres
        .query_one(
            &format!("SELECT \"status\" FROM \"{table}\" WHERE \"name\" = 'gamma'"),
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(status, "new");

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{table}\""))
        .unwrap();
}

#[test]
fn postgresql_to_mysql_live_roundtrip_when_env_is_configured() {
    let _guard = live_db_test_guard();
    let Some((mysql_endpoint, postgres_endpoint)) = test_endpoints() else {
        eprintln!("skipping live roundtrip: TF_MYSQL_* and TF_POSTGRES_* are not configured");
        return;
    };

    let table = unique_table("tf_pg_mysql");
    let mut mysql = mysql_conn(&mysql_endpoint);
    let mut postgres = postgres_client(&postgres_endpoint);

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{table}\""))
        .unwrap();
    postgres
        .batch_execute(&format!(
            "CREATE TABLE \"{table}\" (\"id\" INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, \"name\" VARCHAR(64), \"status\" VARCHAR(16) NOT NULL DEFAULT 'new', \"enabled\" BOOLEAN NOT NULL DEFAULT TRUE, \"payload\" BYTEA NOT NULL, \"amount\" NUMERIC(12,4) NOT NULL, \"event_date\" DATE NOT NULL, \"event_time\" TIME NOT NULL, \"created_at\" TIMESTAMP NOT NULL);
             CREATE INDEX \"idx_{table}_name\" ON \"{table}\" (\"name\");
             INSERT INTO \"{table}\" (\"name\", \"enabled\", \"payload\", \"amount\", \"event_date\", \"event_time\", \"created_at\") VALUES ('alpha', TRUE, decode('0001ff', 'hex'), 1.2300, '2026-05-14', '09:08:07', '2026-05-14 09:08:07'), ('beta', FALSE, decode('cafe', 'hex'), -0.5000, '2026-05-15', '10:11:12', '2026-05-15 10:11:12');"
        ))
        .unwrap();

    let schema = json!({
        "tables": [{
            "name": table,
            "columns": [
                {"name": "id", "type": "integer identity", "nullable": false, "primary_key": true},
                {"name": "name", "type": "varchar(64)", "nullable": true},
                {"name": "status", "type": "varchar(16)", "default": "new", "nullable": false},
                {"name": "enabled", "type": "boolean", "default": "true", "nullable": false},
                {"name": "payload", "type": "bytea", "nullable": false},
                {"name": "amount", "type": "numeric(12,4)", "nullable": false},
                {"name": "event_date", "type": "date", "nullable": false},
                {"name": "event_time", "type": "time without time zone", "nullable": false},
                {"name": "created_at", "type": "timestamp without time zone", "nullable": false}
            ],
            "indexes": [
                {"name": format!("idx_{table}_name"), "columns": ["name"], "unique": false}
            ]
        }]
    });
    let payload = json!({
        "source_engine": "postgresql",
        "target_engine": "mysql",
        "source": endpoint_json(&postgres_endpoint),
        "target": endpoint_json(&mysql_endpoint),
        "schema": schema,
        "execution_options": {"mode": "create_only", "chunk_size": 1}
    });

    let migrate = result_payload(handle_request(Request {
        command: "migrate".to_string(),
        request_id: None,
        payload: payload.clone(),
    }));
    assert_eq!(migrate["success"], true);
    assert_eq!(migrate["rows_copied"], 2);

    let verify = result_payload(handle_request(Request {
        command: "verify".to_string(),
        request_id: None,
        payload,
    }));
    assert_eq!(verify["success"], true);
    assert!(mysql_index_exists(
        &mut mysql,
        &mysql_endpoint.database,
        &table,
        &format!("idx_{table}_name")
    ));
    mysql
        .query_drop(format!(
            "INSERT INTO `{table}` (`name`, `enabled`, `payload`, `amount`, `event_date`, `event_time`, `created_at`) VALUES ('gamma', 1, X'BEEF', 3.0000, '2026-05-16', '11:12:13', '2026-05-16 11:12:13')"
        ))
        .unwrap();
    let next_id = mysql
        .exec_first::<i32, _, _>(
            format!("SELECT `id` FROM `{table}` WHERE `name` = ?"),
            ("gamma",),
        )
        .unwrap()
        .unwrap();
    assert_eq!(next_id, 3);
    let status = mysql
        .exec_first::<String, _, _>(
            format!("SELECT `status` FROM `{table}` WHERE `name` = ?"),
            ("gamma",),
        )
        .unwrap()
        .unwrap();
    assert_eq!(status, "new");

    mysql
        .query_drop(format!("DROP TABLE IF EXISTS `{table}`"))
        .unwrap();
    postgres
        .batch_execute(&format!("DROP TABLE IF EXISTS \"{table}\""))
        .unwrap();
}
