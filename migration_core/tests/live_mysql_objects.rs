//! MySQL single-statement connections for stored programs (TF-STATUS-203).
//! Requires a disposable MySQL (TF_MYSQL_*); TF_LIVE_REQUIRED makes missing config fail.
use migration_core::{mysql_single_statement_conn, Endpoint, LiveAdapter};
use mysql::prelude::Queryable;

fn endpoint() -> Option<Endpoint> {
    Some(Endpoint {
        engine: "mysql".into(),
        host: std::env::var("TF_MYSQL_HOST").ok()?,
        port: std::env::var("TF_MYSQL_PORT")
            .ok()
            .map(|v| v.parse().expect("invalid TF_MYSQL_PORT"))
            .unwrap_or(3306),
        user: std::env::var("TF_MYSQL_USER").ok()?,
        password: std::env::var("TF_MYSQL_PASSWORD").unwrap_or_default(),
        database: std::env::var("TF_MYSQL_DATABASE").ok()?,
        schema: None,
        tls: Default::default(),
    })
}

/// Every first column of every result set the query returns.
fn first_values(conn: &mut mysql::PooledConn, sql: &str) -> Result<Vec<i64>, mysql::Error> {
    let mut result = conn.query_iter(sql)?;
    let mut values = Vec::new();
    while let Some(set) = result.iter() {
        for row in set {
            values.push(row?.get::<i64, _>(0).unwrap());
        }
    }
    Ok(values)
}

fn object_count(conn: &mut mysql::PooledConn, catalog: &str, kind: &str, name: &str) -> u64 {
    conn.exec_first(
        format!("SELECT COUNT(*) FROM information_schema.{catalog} WHERE {kind}_SCHEMA = DATABASE() AND {kind}_NAME = ?"),
        (name,),
    )
    .unwrap()
    .unwrap()
}

fn assert_syntax_error(result: Result<(), mysql::Error>) {
    assert!(
        matches!(&result, Err(mysql::Error::MySqlError(err)) if err.code == 1064),
        "expected the server to reject a chained query: {result:?}"
    );
}

#[test]
fn single_statement_connection_rejects_chained_statements() {
    let Some(endpoint) = endpoint() else {
        assert!(
            std::env::var_os("TF_LIVE_REQUIRED").is_none(),
            "TF_LIVE_REQUIRED is set but TF_MYSQL_* is missing"
        );
        eprintln!("skipping MySQL object tests: TF_MYSQL_* is not configured");
        return;
    };
    let suffix = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let table = format!("tf_obj_x_{suffix}");
    let attempted_table = format!("tf_obj_x2_{suffix}");
    let body = format!("tf_obj_p_{suffix}");
    let chained = format!("tf_obj_p2_{suffix}");
    let trigger = format!("tf_obj_tr_{suffix}");
    let LiveAdapter::MySql(mut normal) = LiveAdapter::connect(&endpoint).unwrap() else {
        unreachable!()
    };
    let mut single = mysql_single_statement_conn(&endpoint).unwrap();
    single
        .query_drop(format!("CREATE TABLE {table} (id INT PRIMARY KEY)"))
        .unwrap();

    // Inner semicolons are part of one CREATE; MULTI_RESULTS must still allow CALL.
    single
        .query_drop(format!("CREATE PROCEDURE {body}() BEGIN SELECT 1; SELECT 2; END"))
        .unwrap();
    assert_eq!(object_count(&mut normal, "ROUTINES", "ROUTINE", &body), 1);
    assert_eq!(first_values(&mut single, &format!("CALL {body}()")).unwrap(), vec![1, 2]);
    single
        .query_drop(format!("CREATE TRIGGER {trigger} BEFORE INSERT ON {table} FOR EACH ROW BEGIN SET NEW.id = NEW.id + 1; SET NEW.id = NEW.id * 2; END"))
        .unwrap();
    single.query_drop(format!("INSERT INTO {table} VALUES (4)")).unwrap();
    assert_eq!(first_values(&mut single, &format!("SELECT id FROM {table}")).unwrap(), vec![10]);

    // The server rejects the entire chain, including the first CREATE.
    assert_syntax_error(single.query_drop(format!(
        "CREATE TABLE {attempted_table} (id INT); DROP TABLE {table}"
    )));
    assert_eq!(object_count(&mut normal, "TABLES", "TABLE", &attempted_table), 0);
    assert_eq!(object_count(&mut normal, "TABLES", "TABLE", &table), 1);
    assert_syntax_error(single.query_drop(format!(
        "CREATE PROCEDURE {chained}() SELECT 1; DROP TABLE {table}"
    )));
    assert_eq!(object_count(&mut normal, "ROUTINES", "ROUTINE", &chained), 0);
    assert_eq!(object_count(&mut normal, "TABLES", "TABLE", &table), 1);
    assert_eq!(first_values(&mut single, "SELECT 3").unwrap(), vec![3]);

    // The flag is per connection: regular connections still accept SELECT and DDL chains.
    assert_eq!(first_values(&mut normal, "SELECT 1; SELECT 2").unwrap(), vec![1, 2]);
    normal
        .query_drop(format!("CREATE TABLE {attempted_table} (id INT); DROP TABLE {attempted_table}"))
        .unwrap();
    assert_eq!(object_count(&mut normal, "TABLES", "TABLE", &attempted_table), 0);

    normal.query_drop(format!("DROP PROCEDURE {body}")).unwrap();
    normal.query_drop(format!("DROP TABLE {table}")).unwrap();
}
