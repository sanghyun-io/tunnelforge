import pytest

from src.core.sql_statement_parser import (
    find_sql_statement_at_position,
    parse_sql_statement_ranges,
    parse_sql_statements,
    read_dollar_quote,
)


@pytest.mark.parametrize("first", [
    "SELECT 1 /* outer /* inner */ ; still outer */",
    "SELECT '{\"a\":1}'::jsonb #> '{a}'",
    "SELECT 5 # 3",
    "SELECT 'path\\'",
    'SELECT "path\\"',
    "SELECT column$tag$ FROM example",
    "SELECT E'escaped\\\';still string'",
    "SELECT 'doubled'';quote'",
    'SELECT "doubled"";identifier"',
])
def test_postgresql_statement_boundaries(first):
    sql = first + "; SELECT 2;"
    assert parse_sql_statements(sql, dialect="postgresql") == [first, "SELECT 2"]
    ranges = parse_sql_statement_ranges(sql, dialect="postgresql")
    assert all(sql[item.start:item.end] == item.text for item in ranges)
    assert find_sql_statement_at_position(sql, sql.index("SELECT 2"), dialect="postgresql") == "SELECT 2"


def test_mysql_default_preserves_hash_comments_and_backslash_strings():
    first = "SELECT 'escaped\\\';still string' # comment;\n"
    assert parse_sql_statements(first + "; SELECT 2;") == [first.strip(), "SELECT 2"]


def test_mysql_double_dash_requires_whitespace():
    assert parse_sql_statements("SELECT 1--2; SELECT 3;") == ["SELECT 1--2", "SELECT 3"]


def test_sql_statement_parser_preserves_semicolons_in_literals_and_comments():
    sql = """
    -- comment; ignored
    SELECT 'a;b';
    /* block; comment */
    UPDATE logs SET message = "x;y";
    """

    assert parse_sql_statements(sql) == [
        "-- comment; ignored\n    SELECT 'a;b'",
        '/* block; comment */\n    UPDATE logs SET message = "x;y"',
    ]


def test_sql_statement_parser_supports_client_delimiters():
    sql = """
    DELIMITER //
    CREATE PROCEDURE p()
    BEGIN
        SELECT 'a;b';
    END//
    DELIMITER ;
    SELECT 1;
    """

    assert parse_sql_statements(sql) == [
        "CREATE PROCEDURE p()\n    BEGIN\n        SELECT 'a;b';\n    END",
        "SELECT 1",
    ]


def test_sql_statement_parser_supports_mysql_dollar_delimiter():
    sql = """
    DELIMITER $$
    CREATE PROCEDURE p()
    BEGIN
        SELECT 'a;b';
    END$$
    DELIMITER ;
    SELECT 1;
    """

    assert parse_sql_statements(sql) == [
        "CREATE PROCEDURE p()\n    BEGIN\n        SELECT 'a;b';\n    END",
        "SELECT 1",
    ]


def test_find_sql_statement_at_position_supports_mysql_dollar_delimiter():
    sql = """
    DELIMITER $$
    CREATE PROCEDURE p()
    BEGIN
        SELECT 'a;b';
    END$$
    DELIMITER ;
    SELECT 1;
    """

    procedure = "CREATE PROCEDURE p()\n    BEGIN\n        SELECT 'a;b';\n    END"

    assert find_sql_statement_at_position(sql, sql.index("SELECT 'a;b'")) == procedure
    assert find_sql_statement_at_position(sql, sql.rindex("SELECT 1")) == "SELECT 1"


def test_sql_statement_parser_supports_postgresql_dollar_quotes():
    sql = """
    CREATE FUNCTION f() RETURNS void AS $body$
    BEGIN
        RAISE NOTICE 'a;b';
    END
    $body$ LANGUAGE plpgsql;
    SELECT 1;
    """

    assert parse_sql_statements(sql) == [
        "CREATE FUNCTION f() RETURNS void AS $body$\n    BEGIN\n        RAISE NOTICE 'a;b';\n    END\n    $body$ LANGUAGE plpgsql",
        "SELECT 1",
    ]


def test_dollar_quote_reader_fails_closed_for_out_of_range_starts():
    sql = "$body$"

    assert read_dollar_quote("", 0) == ""
    assert read_dollar_quote(sql, -1) == ""
    assert read_dollar_quote(sql, len(sql)) == ""
    assert read_dollar_quote("", 0) == ""
    assert read_dollar_quote(sql, -1) == ""
    assert read_dollar_quote(sql, len(sql)) == ""


def test_dollar_quote_reader_fails_closed_for_none_sql_text():
    assert read_dollar_quote(None, 0) == ""
    assert read_dollar_quote(None, 0) == ""


def test_literal_and_comment_mask_marks_strings_and_comments_but_not_identifiers():
    from src.core.sql_statement_parser import literal_and_comment_mask

    sql = "SELECT `t`.a, 'x -- y' FROM t -- c\n/* b */ WHERE z = \"q\""
    masked = "".join("#" if m else ch for ch, m in zip(sql, literal_and_comment_mask(sql)))
    assert masked.startswith("SELECT `t`.a, ######## FROM t ")
    assert "WHERE z = ###" in masked and "/* b */" not in masked

    pg = "SELECT \"Drop\", $f$ DROP $f$"
    pg_masked = "".join("#" if m else ch for ch, m in zip(pg, literal_and_comment_mask(pg, "postgresql")))
    assert pg_masked.startswith('SELECT "Drop", ') and "DROP" not in pg_masked.split(",", 1)[1]


def test_dangerous_query_check_is_not_hidden_by_comment_markers_inside_strings():
    from src.core.production_guard import ProductionGuard

    assert ProductionGuard.is_dangerous_query("SELECT '--'; DROP TABLE t") == (True, "DROP")
    assert ProductionGuard.is_dangerous_query("SELECT 'drop table' -- DELETE") == (False, None)
