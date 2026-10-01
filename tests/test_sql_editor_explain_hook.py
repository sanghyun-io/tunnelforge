"""TF-STATUS-133: execution plan action of the SQL editor."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.ui.dialogs import sql_editor_dialog as editor_module
from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog

app = QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def dialog(monkeypatch):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("u", "p")
    engine = MagicMock()
    engine.is_running.return_value = True
    engine.get_connection_info.return_value = ("127.0.0.1", 3307)
    shown = SQLEditorDialog(
        None,
        {"id": "t", "name": "t", "connection_mode": "direct", "environment": "development",
         "remote_host": "127.0.0.1", "remote_port": 3306},
        config_manager, engine,
    )
    yield shown
    for index in range(shown.editor_tabs.count()):
        tab = shown.editor_tabs.widget(index)
        if tab:
            tab.is_modified = False
    shown.close()


@pytest.fixture
def boxes(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(a[2]))
    return shown


@pytest.fixture
def opened(monkeypatch):
    calls = []

    class Stub:
        def exec(self):
            calls.append("exec")

    def fake(parent, facade, connection_id, sql, timeout_ms=None):
        calls.append((parent, facade, connection_id, sql, timeout_ms))
        return Stub()

    monkeypatch.setattr(editor_module, "show_explain_plan", fake)
    return calls


def _connected(dialog, monkeypatch):
    facade = object()
    dialog.db_connection = SimpleNamespace(facade=facade, connection_id="conn-9", open=True)
    monkeypatch.setattr(dialog, "_ensure_connection", lambda: (True, None))
    return facade


def _place_cursor(dialog, text, position=None, select=None):
    dialog.editor.setPlainText(text)
    cursor = dialog.editor.textCursor()
    if select is not None:
        cursor.setPosition(select[0])
        cursor.setPosition(select[1], QTextCursor.MoveMode.KeepAnchor)
    else:
        cursor.setPosition(len(text) if position is None else position)
    dialog.editor.setTextCursor(cursor)


def test_action_button_and_shortcut_exist(dialog):
    assert dialog.btn_explain.text().endswith("실행 계획") and "Ctrl+E" in dialog.btn_explain.toolTip()
    assert dialog.shortcut_explain.key().toString() == "Ctrl+E"
    # no other shortcut of the window uses Ctrl+E
    others = [s for s in dialog.findChildren(type(dialog.shortcut_explain)) if s is not dialog.shortcut_explain]
    assert all(s.key().toString() != "Ctrl+E" for s in others)


def test_statement_at_cursor_is_explained_on_the_editor_session(dialog, monkeypatch, opened):
    facade = _connected(dialog, monkeypatch)
    text = "SELECT 1;\nSELECT * FROM t WHERE a = 2;"
    _place_cursor(dialog, text, position=text.index("WHERE"))
    dialog.query_timeout_spin.setValue(5)
    dialog.show_execution_plan()
    parent, used_facade, connection_id, sql, timeout_ms = opened[0]
    assert parent is dialog and used_facade is facade and connection_id == "conn-9"
    assert sql == "SELECT * FROM t WHERE a = 2" and timeout_ms == 5000
    assert opened[1] == "exec"


def test_selection_wins_over_cursor_and_no_timeout_by_default(dialog, monkeypatch, opened):
    _connected(dialog, monkeypatch)
    text = "SELECT 1;\nSELECT 2;"
    _place_cursor(dialog, text, select=(0, len("SELECT 1")))
    dialog.show_execution_plan()
    assert opened[0][3] == "SELECT 1" and opened[0][4] is None


def test_multiple_statements_ask_for_a_single_one(dialog, monkeypatch, opened, boxes):
    _connected(dialog, monkeypatch)
    text = "SELECT 1;\nSELECT 2;"
    _place_cursor(dialog, text, select=(0, len(text)))
    dialog.show_execution_plan()
    assert opened == [] and boxes and "한 문장" in boxes[0]


def test_empty_editor_shows_a_status_message(dialog, monkeypatch, opened, boxes):
    _connected(dialog, monkeypatch)
    _place_cursor(dialog, "   \n  ")
    dialog.show_execution_plan()
    assert opened == [] and boxes == []
    assert "쿼리가 없습니다" in dialog.status_bar.currentMessage()


def test_connection_failure_is_explained_and_nothing_opens(dialog, monkeypatch, opened, boxes):
    monkeypatch.setattr(dialog, "_ensure_connection", lambda: (False, "자격 증명이 없습니다"))
    _place_cursor(dialog, "SELECT 1")
    dialog.show_execution_plan()
    assert opened == [] and boxes and "자격 증명" in boxes[0]


def test_not_available_while_a_query_runs(dialog, monkeypatch, opened, boxes):
    _connected(dialog, monkeypatch)
    _place_cursor(dialog, "SELECT 1")
    dialog._query_executing = True
    dialog.show_execution_plan()
    assert opened == [] and boxes and "실행 중" in boxes[0]
    dialog._query_executing = False


def test_action_is_disabled_with_the_other_run_actions_while_executing(dialog):
    dialog._set_executing_state(True)
    try:
        assert not dialog.btn_explain.isEnabled() and not dialog.shortcut_explain.isEnabled()
    finally:
        dialog._set_executing_state(False)
    assert dialog.btn_explain.isEnabled() and dialog.shortcut_explain.isEnabled()
