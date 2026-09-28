import pytest

from src.core.sql_statement_parser import (
    find_sql_statement_at_position,
    parse_sql_statement_ranges,
    parse_sql_statements,
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
