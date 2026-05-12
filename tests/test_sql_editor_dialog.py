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
