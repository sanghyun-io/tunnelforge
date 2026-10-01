//! Read-only session guard (TF-STATUS-128).
//!
//! A session opened with `read_only: true` is switched to a read-only transaction mode by the
//! server (MySQL `SET SESSION TRANSACTION READ ONLY`, PostgreSQL `SET SESSION CHARACTERISTICS AS
//! TRANSACTION READ ONLY`). The server rejects data writes; this module additionally refuses,
//! before they are sent, the statements that would switch that mode off again. It is defence in
//! depth against accidental or plain bypasses (it cannot stop deliberately obfuscated dynamic SQL;
//! real separation of duties is a database account without write privileges).

use serde_json::Value;

use crate::*;

pub(crate) const READ_ONLY_ERROR_CODE: &str = "read_only_session";

/// Server-side refusal of a write inside a read-only transaction (PostgreSQL 25006, MySQL 1792).
pub(crate) fn is_read_only_violation(message: &str) -> bool {
    let lowered = message.to_ascii_lowercase();
    lowered.contains("read-only transaction") || lowered.contains("read only transaction")
}

/// Switch a fresh session to server-enforced read-only mode.
pub(crate) fn apply_read_only(adapter: &mut LiveAdapter) -> Result<(), String> {
    use mysql::prelude::Queryable;
    match adapter {
        LiveAdapter::MySql(conn) => conn
            .query_drop("SET SESSION TRANSACTION READ ONLY")
            .map_err(|err| format!("mysql read-only session error: {err}")),
        LiveAdapter::PostgreSql(client) => client
            .batch_execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            .map_err(|err| format_postgres_error("postgresql read-only session error", &err)),
    }
}

pub(crate) fn read_only_error_event(request_id: &Option<String>, message: &str) -> Value {
    serde_json::json!({
        "event": "error",
        "request_id": request_id,
        "error_code": READ_ONLY_ERROR_CODE,
        "message": message,
    })
}

/// Returns the reason when `sql` (any statement of a multi-statement text) would turn read-only
/// mode off. Only the statement shapes below are inspected.
pub(crate) fn read_only_bypass(sql: &str) -> Option<String> {
    for statement in split_statements(sql) {
        if let Some(reason) = check_statement(&statement) {
            return Some(reason);
        }
    }
    None
}

const GUC_NAMES: [&str; 3] = ["default_transaction_read_only", "transaction_read_only", "tx_read_only"];

fn check_statement(statement: &str) -> Option<String> {
    let flat = statement.to_ascii_lowercase();
    let words: Vec<&str> = flat
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .filter(|w| !w.is_empty())
        .collect();
    let first = *words.first()?;
    let has_pair = |a: &str, b: &str| words.windows(2).any(|w| w[0] == a && w[1] == b);

    let writes_back = has_pair("read", "write");
    if writes_back && matches!(first, "set" | "start" | "begin" | "do" | "call" | "execute" | "prepare" | "alter") {
        return Some("읽기 전용 세션에서는 READ WRITE 트랜잭션을 시작하거나 설정할 수 없습니다".to_string());
    }
    let mentions_guc = GUC_NAMES.iter().any(|name| words.contains(name));
    if mentions_guc {
        let reads_only = first == "show" || (first == "select" && !words.contains(&"set_config"));
        if !reads_only {
            return Some("읽기 전용 세션의 읽기 전용 설정은 변경할 수 없습니다".to_string());
        }
    }
    if words.contains(&"set_config") && words.contains(&"transaction") {
        return Some("읽기 전용 세션의 트랜잭션 설정은 변경할 수 없습니다".to_string());
    }
    // Statements the server does not treat as data writes but that must not run in a read-only
    // window: procedures may switch the mode back off from inside, locks block other sessions,
    // global/system settings and maintenance change the server, file output leaves the database.
    if first == "call" {
        return Some("프로시저 호출(CALL)은 읽기 전용 세션에서 실행할 수 없습니다 (프로시저가 읽기 전용 설정을 바꿀 수 있음)".to_string());
    }
    if first == "lock" {
        return Some("LOCK은 다른 세션을 막으므로 읽기 전용 세션에서 실행할 수 없습니다".to_string());
    }
    if first == "set" && words.iter().any(|w| matches!(*w, "global" | "persist" | "persist_only")) {
        return Some("서버 전역 설정은 읽기 전용 세션에서 변경할 수 없습니다".to_string());
    }
    if (first == "alter" && words.get(1) == Some(&"system")) || matches!(first, "vacuum" | "reindex" | "cluster") {
        return Some("서버 설정 변경/유지보수 명령은 읽기 전용 세션에서 실행할 수 없습니다".to_string());
    }
    if words.iter().any(|w| matches!(*w, "outfile" | "dumpfile")) {
        return Some("서버 파일로 쓰는 쿼리는 읽기 전용 세션에서 실행할 수 없습니다".to_string());
    }
    if words.iter().any(|w| {
        matches!(*w, "lo_create" | "lo_import" | "lo_export" | "lo_unlink" | "lo_put" | "lo_from_bytea" | "lowrite" | "lo_truncate")
    }) {
        return Some("라지 오브젝트 쓰기 함수는 읽기 전용 세션에서 실행할 수 없습니다".to_string());
    }
    if first == "copy" && words.contains(&"program") {
        return Some("COPY ... PROGRAM은 읽기 전용 세션에서 실행할 수 없습니다".to_string());
    }
    if (first == "reset" && words.get(1) == Some(&"all")) || (first == "discard" && words.get(1) == Some(&"all")) {
        return Some("RESET ALL / DISCARD ALL은 읽기 전용 설정을 지우므로 읽기 전용 세션에서 실행할 수 없습니다".to_string());
    }
    None
}

/// Splits on `;` outside quotes, comments and PostgreSQL dollar quotes. Comments are dropped,
/// quoted text is kept (a `set_config('default_transaction_read_only', ...)` argument matters).
fn split_statements(sql: &str) -> Vec<String> {
    let bytes = sql.as_bytes();
    let mut statements = Vec::new();
    let mut current = String::new();
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i];
        match c {
            b'\'' | b'"' | b'`' => {
                let quote = c;
                let start = i;
                i += 1;
                while i < bytes.len() {
                    if bytes[i] == b'\\' && quote != b'`' {
                        i += 2;
                        continue;
                    }
                    if bytes[i] == quote {
                        if bytes.get(i + 1) == Some(&quote) {
                            i += 2;
                            continue;
                        }
                        break;
                    }
                    i += 1;
                }
                i = (i + 1).min(bytes.len());
                current.push_str(&sql[start..i]);
            }
            b'-' if bytes.get(i + 1) == Some(&b'-') => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
                current.push(' ');
            }
            b'#' => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
                current.push(' ');
            }
            b'/' if bytes.get(i + 1) == Some(&b'*') => {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i = (i + 2).min(bytes.len());
                current.push(' ');
            }
            b'$' => {
                // $tag$ ... $tag$ (tag may be empty); a lone `$1` placeholder is not a quote.
                let tag_end = bytes[i + 1..].iter().position(|b| !(b.is_ascii_alphanumeric() || *b == b'_'));
                match tag_end {
                    Some(offset) if bytes[i + 1 + offset] == b'$' && !bytes[i + 1].is_ascii_digit() => {
                        let tag = &sql[i..i + offset + 2];
                        let body_start = i + offset + 2;
                        let end = sql[body_start..].find(tag).map(|p| body_start + p + tag.len()).unwrap_or(bytes.len());
                        current.push_str(&sql[i..end]);
                        i = end;
                    }
                    _ => {
                        current.push('$');
                        i += 1;
                    }
                }
            }
            b';' => {
                if !current.trim().is_empty() {
                    statements.push(std::mem::take(&mut current));
                }
                current.clear();
                i += 1;
            }
            _ => {
                // Copy whole UTF-8 characters.
                let ch_len = utf8_len(c);
                current.push_str(&sql[i..(i + ch_len).min(bytes.len())]);
                i += ch_len;
            }
        }
    }
    if !current.trim().is_empty() {
        statements.push(current);
    }
    statements
}

fn utf8_len(first: u8) -> usize {
    match first {
        0x00..=0x7F => 1,
        0xC0..=0xDF => 2,
        0xE0..=0xEF => 3,
        _ => 4,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bypass_statements_are_refused() {
        for sql in [
            "SET SESSION TRANSACTION READ WRITE",
            "set transaction read write",
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
            "START TRANSACTION READ WRITE",
            "BEGIN ISOLATION LEVEL READ COMMITTED, READ WRITE",
            "begin  transaction\n read   write",
            "SET default_transaction_read_only = off",
            "SET SESSION transaction_read_only = 0",
            "SET @@session.tx_read_only = 0",
            "SET GLOBAL transaction_read_only = OFF",
            "RESET default_transaction_read_only",
            "SELECT set_config('default_transaction_read_only', 'off', false)",
            "SELECT 1; SET SESSION TRANSACTION READ WRITE",
            "/* x */ SET /* y */ SESSION TRANSACTION READ WRITE",
            "DO $$ BEGIN PERFORM set_config('transaction_read_only','off',false); END $$",
            "RESET ALL",
            "DISCARD ALL",
            "CALL p_flip()",
            "LOCK TABLES t WRITE",
            "SET GLOBAL max_connections = 10",
            "SET PERSIST max_connections = 10",
            "ALTER SYSTEM SET work_mem = '8MB'",
            "VACUUM FULL t",
            "REINDEX TABLE t",
            "SELECT * FROM t INTO OUTFILE '/tmp/x'",
            "SELECT lo_create(0)",
            "COPY (SELECT 1) TO PROGRAM 'id'",
        ] {
            assert!(read_only_bypass(sql).is_some(), "{sql}");
        }
    }

    #[test]
    fn normal_statements_pass() {
        for sql in [
            "SELECT 1",
            "SELECT 'READ WRITE' AS note",
            "SELECT * FROM t WHERE a = 'x; SET SESSION TRANSACTION READ WRITE'",
            "SHOW transaction_read_only",
            "SELECT @@transaction_read_only",
            "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
            "BEGIN",
            "START TRANSACTION READ ONLY",
            "-- SET SESSION TRANSACTION READ WRITE\nSELECT 1",
            "EXPLAIN SELECT 1",
            "SELECT $1, $$a;b$$",
            "INSERT INTO t VALUES ('a')",
            "SELECT '한글'; SELECT 2",
            "ANALYZE t",
            "SET SESSION sql_mode = ''",
            "SELECT 'CALL x' AS note",
            "SHOW GLOBAL STATUS",
        ] {
            assert!(read_only_bypass(sql).is_none(), "{sql}");
        }
    }

    #[test]
    fn violation_messages_are_recognised() {
        assert!(is_read_only_violation("db error: ERROR: cannot execute INSERT in a read-only transaction"));
        assert!(is_read_only_violation("ERROR 1792 (25006): Cannot execute statement in a READ ONLY transaction."));
        assert!(!is_read_only_violation("syntax error"));
    }
}
