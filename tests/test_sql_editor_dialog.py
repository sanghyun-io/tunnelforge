import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.core.sql_validator import SchemaMetadata
from src.ui.dialogs.sql_editor_dialog import (
    LARGE_SQL_RENDER_LIMIT_BYTES,
    SQLEditorDialog,
    SQLEditorTab,
    format_metadata_db_version,
)


app = QApplication.instance() or QApplication(sys.argv)


def make_dialog(monkeypatch):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)

    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("testuser", "testpass")

    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 3307)

    return SQLEditorDialog(
        None,
        {
            "id": "test-tunnel",
            "name": "테스트 터널",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
        },
        config_manager,
        tunnel_engine,
    )


def close_dialog(dialog):
    for i in range(dialog.editor_tabs.count()):
        tab = dialog.editor_tabs.widget(i)
        if tab:
            tab.is_modified = False
    dialog.close()


def test_message_panel_is_separate_from_result_tabs(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        assert dialog.result_tabs.count() == 0
        assert dialog.result_tabs.indexOf(dialog.message_text) == -1
        assert dialog._message_collapsed is True
        assert dialog.message_text.isHidden()
        assert not dialog.message_summary.isHidden()
        assert dialog.btn_toggle_message.text() == "실행 로그 펼치기"
    finally:
        close_dialog(dialog)


def test_split_queries_preserves_comments_dollar_quotes_and_delimiters():
    sql = """-- comment; ignored
SELECT 'a;b';
CREATE FUNCTION f() RETURNS void AS $body$
BEGIN
    RAISE NOTICE 'x;y';
END
$body$ LANGUAGE plpgsql;
DELIMITER //
CREATE PROCEDURE p()
BEGIN
    SELECT 'c;d';
END//
DELIMITER ;
SELECT 1;"""

    assert SQLEditorDialog._split_queries(None, sql) == [
        "-- comment; ignored\nSELECT 'a;b'",
        "CREATE FUNCTION f() RETURNS void AS $body$\n"
        "BEGIN\n"
        "    RAISE NOTICE 'x;y';\n"
        "END\n"
        "$body$ LANGUAGE plpgsql",
        "CREATE PROCEDURE p()\nBEGIN\n    SELECT 'c;d';\nEND",
        "SELECT 1",
    ]


def test_get_query_at_cursor_uses_statement_parser_ranges(monkeypatch):
    dialog = make_dialog(monkeypatch)
    sql = """SELECT 1;
CREATE FUNCTION f() RETURNS void AS $body$
BEGIN
    RAISE NOTICE 'x;y';
END
$body$ LANGUAGE plpgsql;
SELECT 2;"""
    try:
        dialog.editor.setPlainText(sql)
        cursor = dialog.editor.textCursor()
        cursor.setPosition(sql.index("RAISE NOTICE"))
        dialog.editor.setTextCursor(cursor)

        assert dialog._get_query_at_cursor() == (
            "CREATE FUNCTION f() RETURNS void AS $body$\n"
            "BEGIN\n"
            "    RAISE NOTICE 'x;y';\n"
            "END\n"
            "$body$ LANGUAGE plpgsql"
        )
    finally:
        close_dialog(dialog)


def test_message_panel_toggle_changes_height(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._toggle_message_panel()
        assert dialog._message_collapsed is False
        assert not dialog.message_text.isHidden()
        assert dialog.message_text.maximumHeight() == 220
        assert dialog.btn_toggle_message.text() == "실행 로그 접기"

        dialog._toggle_message_panel()
        assert dialog._message_collapsed is True
        assert dialog.message_text.isHidden()
    finally:
        close_dialog(dialog)


def test_query_result_collapses_log_and_updates_summary(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._toggle_message_panel()

        dialog._on_query_result(0, ["id"], [[1], [2]], "", 2, 0.125)

        assert dialog._message_collapsed is True
        assert dialog.message_text.isHidden()
        assert "2행 반환" in dialog.message_summary.text()
        assert "0.125초" in dialog.message_summary.text()
    finally:
        close_dialog(dialog)


def test_postgresql_header_labels_selector_as_schema(monkeypatch):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()

    dialog = SQLEditorDialog(
        None,
        {
            "id": "pg-test",
            "name": "PostgreSQL 테스트",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 35432,
            "db_engine": "postgresql",
            "default_database": "tf_target",
        },
        config_manager,
        tunnel_engine,
    )
    try:
        assert dialog.db_selector_label.text() == "📂 Schema:"
    finally:
        close_dialog(dialog)


def test_result_tabs_can_be_deleted_and_cleared(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._add_result_table(["id"], [[1]], 0.01)
        dialog._add_result_table(["id"], [[2]], 0.01)

        assert dialog.result_tabs.count() == 2
        assert dialog.result_tabs.tabText(0).startswith("결과 1")
        assert dialog.result_tabs.tabText(1).startswith("결과 2")

        dialog.close_result_tab(0)
        assert dialog.result_tabs.count() == 1
        assert dialog.result_tabs.tabText(0).startswith("결과 2")

        dialog._clear_result_tabs()
        assert dialog.result_tabs.count() == 0

        dialog._add_result_table(["id"], [[3]], 0.01)
        assert dialog.result_tabs.tabText(0).startswith("결과 1")
    finally:
        close_dialog(dialog)


def test_refresh_databases_uses_configured_engine(monkeypatch):
    refresh_databases = SQLEditorDialog.refresh_databases

    class FakeConnector:
        def __init__(self):
            self.disconnected = False

        def connect(self):
            return True, "ok"

        def get_schemas(self):
            return ["public"]

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()
    connector = FakeConnector()
    created = {}

    dialog = SQLEditorDialog(
        None,
        {
            "id": "pg-test",
            "name": "PostgreSQL 테스트",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 35432,
            "db_engine": "postgresql",
            "default_database": "tf_target",
        },
        config_manager,
        tunnel_engine,
    )
    try:
        dialog.refresh_databases = refresh_databases.__get__(dialog, SQLEditorDialog)
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 35432, None, None))
        def create_connector(*args):
            created["args"] = args
            return connector

        dialog._create_db_connector = MagicMock(side_effect=create_connector)
        dialog._load_metadata = MagicMock()

        dialog.refresh_databases()

        assert created["args"] == (
            "127.0.0.1",
            35432,
            "postgres",
            "tunnelpass",
            "tf_target",
            "public",
        )
        assert dialog.db_combo.findText("public") >= 0
        assert connector.disconnected is True
    finally:
        close_dialog(dialog)


def test_postgresql_query_connection_uses_database_and_selected_schema(monkeypatch):
    class FakeCursor:
        def execute(self, sql):
            self.sql = sql

    class FakeConnection:
        open = False

        def __init__(self):
            self.autocommit_value = None
            self.cursor_obj = FakeCursor()

        def autocommit(self, value):
            self.autocommit_value = value

        def cursor(self):
            return self.cursor_obj

    class FakeConnector:
        def __init__(self):
            self.connection = FakeConnection()

        def connect(self):
            self.connection.open = True
            return True, "ok"

    dialog = make_dialog(monkeypatch)
    connector = FakeConnector()
    created = {}
    try:
        dialog.config.update(
            {
                "db_engine": "postgresql",
                "remote_port": 35432,
                "default_database": "tf_target",
                "default_schema": None,
            }
        )
        dialog.db_combo.clear()
        dialog.db_combo.addItem("public")
        dialog.db_combo.setCurrentText("public")
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 35432, None, None))
        dialog._create_db_connector = MagicMock(
            side_effect=lambda *args: created.setdefault("args", args) and connector
        )

        success, error = dialog._ensure_connection()

        assert success is True
        assert error is None
        assert created["args"] == (
            "127.0.0.1",
            35432,
            "testuser",
            "testpass",
            "tf_target",
            "public",
        )
    finally:
        close_dialog(dialog)


def test_refresh_databases_passes_mysql_default_database(monkeypatch):
    refresh_databases = SQLEditorDialog.refresh_databases

    class FakeConnector:
        def connect(self):
            return True, "ok"

        def get_schemas(self):
            return ["tf_source84"]

        def disconnect(self):
            pass

    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("root", "tunnelpass")
    tunnel_engine = MagicMock()
    created = {}

    dialog = SQLEditorDialog(
        None,
        {
            "id": "mysql84-test",
            "name": "MySQL 8.4 테스트",
            "connection_mode": "direct",
            "remote_host": "127.0.0.1",
            "remote_port": 33406,
            "db_engine": "mysql",
            "default_database": "tf_source84",
        },
        config_manager,
        tunnel_engine,
    )
    try:
        dialog.refresh_databases = refresh_databases.__get__(dialog, SQLEditorDialog)
        dialog._resolve_db_target = MagicMock(return_value=("127.0.0.1", 33406, None, None))
        dialog._create_db_connector = MagicMock(
            side_effect=lambda *args: created.setdefault("args", args) and FakeConnector()
        )
        dialog._load_metadata = MagicMock()

        dialog.refresh_databases()

        assert created["args"] == (
            "127.0.0.1",
            33406,
            "root",
            "tunnelpass",
            "tf_source84",
            "",
        )
    finally:
        close_dialog(dialog)


def test_format_metadata_db_version_tolerates_empty_rust_version():
    assert format_metadata_db_version("") == "unknown"
    assert format_metadata_db_version("8.4.7") == "8.4"
    assert format_metadata_db_version((16, 2, 0)) == "16.2"


def test_metadata_loaded_does_not_crash_on_empty_version(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        metadata = SchemaMetadata()
        metadata.tables = {"users"}
        metadata.db_version = ""
        dialog._on_validation_requested = MagicMock()

        dialog._on_metadata_loaded(metadata)

        assert "1개 테이블 로드됨" in dialog.validation_label.text()
        assert "unknown" in dialog.validation_label.text()
    finally:
        close_dialog(dialog)


def test_metadata_loaded_populates_schema_tree(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog.db_combo.addItem("app")
        dialog.db_combo.addItem("archive")
        dialog.db_combo.setCurrentText("app")
        metadata = SchemaMetadata()
        metadata.tables = {"orders", "users"}
        metadata.columns = {
            "users": {"id", "email"},
            "orders": {"id", "user_id"},
        }
        metadata.db_version = (8, 4, 0)

        dialog._on_metadata_loaded(metadata)

        assert [
            dialog.schema_tree.topLevelItem(i).text(0)
            for i in range(dialog.schema_tree.topLevelItemCount())
        ] == ["app", "archive"]
        root = dialog.schema_tree.topLevelItem(0)
        assert root.text(0) == "app"
        assert [root.child(i).text(0) for i in range(root.childCount())] == [
            "orders",
            "users",
        ]
        users = root.child(1)
        assert [users.child(i).text(0) for i in range(users.childCount())] == [
            "email",
            "id",
        ]
    finally:
        close_dialog(dialog)


def test_schema_tree_table_click_inserts_quoted_table_name(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        metadata = SchemaMetadata()
        metadata.tables = {"users"}
        metadata.columns = {"users": {"id"}}
        dialog._on_metadata_loaded(metadata)
        dialog.editor.setPlainText("SELECT * FROM ")
        dialog.editor.moveCursor(dialog.editor.textCursor().MoveOperation.End)

        root = dialog.schema_tree.topLevelItem(0)
        table_item = root.child(0)
        dialog._on_schema_tree_item_clicked(table_item, 0)

        assert dialog.editor.toPlainText() == "SELECT * FROM `users` "
    finally:
        close_dialog(dialog)


def test_large_sql_file_disables_expensive_editor_features(tmp_path):
    tab = SQLEditorTab(tab_index=1)
    large_sql = "SELECT * FROM users;\n" * ((LARGE_SQL_RENDER_LIMIT_BYTES // 20) + 2000)
    file_path = tmp_path / "fixedStyle.sql"
    file_path.write_text(large_sql, encoding="utf-8")
    try:
        assert tab.load_file(str(file_path)) is True

        assert tab.editor.is_large_document_mode() is True
        assert tab.editor.highlighter.document() is None
        assert not tab.editor._validation_timer.isActive()
        assert "대용량 SQL" in tab.validation_label.text()
    finally:
        tab.close()


def test_small_content_reenables_editor_features_after_large_file(tmp_path):
    tab = SQLEditorTab(tab_index=1)
    large_sql = "SELECT * FROM users;\n" * ((LARGE_SQL_RENDER_LIMIT_BYTES // 20) + 2000)
    file_path = tmp_path / "fixedStyle.sql"
    file_path.write_text(large_sql, encoding="utf-8")
    try:
        assert tab.load_file(str(file_path)) is True

        tab.set_content("SELECT 1;")

        assert tab.editor.is_large_document_mode() is False
        assert tab.editor.highlighter.document() == tab.editor.document()
        assert tab.validation_label.text() == ""
    finally:
        tab.close()
