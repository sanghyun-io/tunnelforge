from src.ui.dialogs.sql_editor_editability import (
    analyze_query_editability,
    build_primary_key_query,
    quote_editor_identifier,
)


def test_analyze_query_editability_accepts_simple_qualified_select():
    assert analyze_query_editability(
        '/* lead */ SELECT id, name FROM "public"."users" WHERE id = 1;'
    ) == {"schema": "public", "table": "users"}


def test_analyze_query_editability_rejects_complex_selects():
    assert analyze_query_editability("SELECT * FROM users u JOIN teams t ON t.id = u.team_id") is None
    assert analyze_query_editability("SELECT COUNT(*) FROM users") is None
    assert analyze_query_editability("SELECT * FROM users, teams") is None


def test_quote_editor_identifier_uses_engine_specific_quotes():
    assert quote_editor_identifier("mysql", "a`b") == "`a``b`"
    assert quote_editor_identifier("postgresql", 'a"b') == '"a""b"'


def test_build_primary_key_query_preserves_engine_specific_sql():
    mysql_schema_sql = build_primary_key_query("mysql", has_schema=True)
    mysql_database_sql = build_primary_key_query("mysql", has_schema=False)
    postgres_sql = build_primary_key_query("postgresql", has_schema=True)

    assert "TABLE_SCHEMA=%s" in mysql_schema_sql
    assert "TABLE_SCHEMA=DATABASE()" in mysql_database_sql
    assert "information_schema.table_constraints" in postgres_sql
    assert "kcu.ordinal_position" in postgres_sql
