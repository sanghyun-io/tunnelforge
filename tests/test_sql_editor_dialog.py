import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog


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
        assert dialog.message_text.maximumHeight() == 68
        assert dialog.btn_toggle_message.text() == "▶ 메시지"
    finally:
        close_dialog(dialog)


def test_message_panel_toggle_changes_height(monkeypatch):
    dialog = make_dialog(monkeypatch)
    try:
        dialog._toggle_message_panel()
        assert dialog._message_collapsed is False
        assert dialog.message_text.maximumHeight() == 220
        assert dialog.btn_toggle_message.text() == "▼ 메시지"

        dialog._toggle_message_panel()
        assert dialog._message_collapsed is True
        assert dialog.message_text.maximumHeight() == 68
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
            return ["tf_target"]

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
        )
        assert dialog.db_combo.findText("tf_target") >= 0
        assert connector.disconnected is True
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
        )
    finally:
        close_dialog(dialog)
