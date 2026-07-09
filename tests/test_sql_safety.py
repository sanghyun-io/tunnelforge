from src.core.sql_safety import find_dangerous_sql_warnings


def test_warns_for_drop_and_truncate_in_first_seen_order_without_duplicates():
    sql = """
    DROP TABLE old_users;
    SELECT 1;
    truncate table audit_log;
    DROP TABLE old_users;
    """

    assert find_dangerous_sql_warnings(sql) == [
        "DROP 문은 데이터를 완전히 삭제합니다!",
        "TRUNCATE는 테이블의 모든 데이터를 삭제합니다!",
    ]


def test_warns_for_delete_without_where_but_not_with_where():
    sql = """
    DELETE FROM sessions;
    DELETE FROM logs WHERE created_at < NOW();
    """

    assert find_dangerous_sql_warnings(sql) == [
        "DELETE에 WHERE 절이 없어 전체 데이터가 삭제됩니다!",
    ]


def test_warns_for_update_without_where_per_statement():
    sql = """
    UPDATE accounts SET balance = 0;
    SELECT * FROM accounts WHERE id = 1;
    """

    assert find_dangerous_sql_warnings(sql) == [
        "UPDATE에 WHERE 절이 없어 전체 데이터가 수정됩니다!",
    ]


def test_does_not_warn_for_update_with_where():
    sql = "UPDATE accounts SET balance = 0 WHERE id = 1;"

    assert find_dangerous_sql_warnings(sql) == []


def test_empty_sql_has_no_warnings():
    assert find_dangerous_sql_warnings("") == []
