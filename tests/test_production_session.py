"""Production read-only session policy and change review (TF-STATUS-128)."""
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from src.core.db_core_dbapi_shim import create_rust_db_connector
from src.core.db_core_facade import DbEndpoint
from src.ui.dialogs.production_session import ProductionSessionMixin, banner_content, build_commit_summary
from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog
from src.ui.dialogs.sql_editor_workers import ConnectionParams, SQLQueryWorker, connector_from_params

_app = QApplication.instance() or QApplication([])


def _endpoint(**kwargs):
    return DbEndpoint(engine="mysql", host="127.0.0.1", port=3306, user="u", password="p", database="d",
                      tls_mode="disable", **kwargs)


def test_endpoint_payload_carries_read_only_only_when_true():
    assert "read_only" not in _endpoint().to_payload()
    assert _endpoint(read_only=True).to_payload()["read_only"] is True


def test_connectors_pass_read_only_down_to_the_endpoint():
    connector = create_rust_db_connector("mysql", "127.0.0.1", 3306, "u", "p", "d", read_only=True)
    assert connector.endpoint.read_only is True
    assert create_rust_db_connector("mysql", "127.0.0.1", 3306, "u", "p", "d").endpoint.read_only is False
    params = ConnectionParams("postgresql", "127.0.0.1", 5432, "u", "p", "d", "public", read_only=True)
    assert connector_from_params(params).endpoint.read_only is True
    worker = SQLQueryWorker("127.0.0.1", 3306, "u", "p", "d", ["SELECT 1"], read_only=True)
    assert worker.params.read_only is True
    assert SQLQueryWorker("127.0.0.1", 3306, "u", "p", "d", ["SELECT 1"]).params.read_only is False


def test_commit_summary_lists_statements_and_total_rows():
    pending = [
        {"query": "UPDATE t SET a = 1\nWHERE id < 10", "type": "UPDATE", "affected": 4},
        {"query": "DELETE FROM t WHERE id = 1", "type": "DELETE", "affected": 1},
    ]
    text, total = build_commit_summary(pending)
    assert total == 5
    assert "[UPDATE] 4행 - UPDATE t SET a = 1 WHERE id < 10" in text and "[DELETE] 1행" in text
    many = [{"query": f"INSERT {i}", "type": "INSERT", "affected": 1} for i in range(20)]
    text, total = build_commit_summary(many)
    assert total == 20 and "외 5건" in text


def test_banner_states():
    text, _ = banner_content("🔴 PRODUCTION", "db:3306 / app", True, False, 0, 0)
    assert "읽기 전용" in text and "미커밋 변경 없음" in text
    text, (fg, bg) = banner_content("🔴 PRODUCTION", "db:3306 / app", True, True, 2, 1)
    assert "쓰기 해제됨" in text and "쿼리 2건" in text and "셀 편집 1건" in text and bg == "#b91c1c"
    text, _ = banner_content("🟢 DEVELOPMENT", "db:3306 / app", False, False, 0, 0)
    assert "쓰기 가능" in text


class _Window(ProductionSessionMixin):
    def __init__(self, environment):
        self.config = {"name": "prod", "environment": environment, "connection_mode": "direct",
                       "remote_host": "db", "remote_port": 3306, "default_database": "app"}
        self.pending_queries = []
        self.message_text = MagicMock()
        self.db_combo = MagicMock()
        self.db_combo.currentText.return_value = "app"
        self._close_db_connection = MagicMock()
        self._collect_all_pending_edits = MagicMock(return_value=[])
        self._database_and_schema_for_selection = lambda name: (name, "")
        self._query_executing = False


def test_only_production_profiles_are_read_only():
    assert _Window("production")._session_read_only() is True
    for environment in ("staging", "development", None):
        assert _Window(environment)._session_read_only() is False


def test_unlock_requires_the_schema_name_confirmation_and_reopens_the_session():
    window = _Window("production")
    with patch("src.ui.dialogs.production_session.SchemaConfirmDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
        window._unlock_writes()
    assert window._session_read_only() is True
    window._close_db_connection.assert_not_called()

    with patch("src.ui.dialogs.production_session.SchemaConfirmDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        window._unlock_writes()
        assert dialog_cls.call_args.args[3].value == "production"
        assert dialog_cls.call_args.args[2] == "app"  # the name the user has to type
    assert window._session_read_only() is False
    window._close_db_connection.assert_called_once()


def test_changing_target_or_closing_relocks_and_pending_changes_block_lock_back():
    window = _Window("production")
    window._write_unlocked = True
    window._unlock_target = ("app", "")
    window._relock_if_target_changed(("app", ""))
    assert window._write_unlocked is True
    window._relock_if_target_changed(("other", ""))
    assert window._write_unlocked is False and window._session_read_only() is True

    window._write_unlocked = True
    window.pending_queries = [{"query": "UPDATE t SET a = 1", "type": "UPDATE", "affected": 1}]
    with patch("src.ui.dialogs.production_session.QMessageBox.warning") as warning:
        window._lock_writes()
    warning.assert_called_once()
    assert window._write_unlocked is True


def test_commit_summary_confirmation_defaults_to_no():
    window = _Window("production")
    assert window._confirm_commit_summary() is True  # nothing pending
    window.pending_queries = [{"query": "DELETE FROM t", "type": "DELETE", "affected": 7}]
    with patch("src.ui.dialogs.production_session.QMessageBox.question",
               return_value=QMessageBox.StandardButton.No) as question:
        assert window._confirm_commit_summary() is False
    assert "7행" in question.call_args.args[2]
    assert question.call_args.args[4] == QMessageBox.StandardButton.No
    with patch("src.ui.dialogs.production_session.QMessageBox.question",
               return_value=QMessageBox.StandardButton.Yes):
        assert window._confirm_commit_summary() is True


def _dialog(monkeypatch, environment):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("u", "p")
    return SQLEditorDialog(
        None,
        {"id": "t", "name": "n", "connection_mode": "direct", "environment": environment,
         "remote_host": "127.0.0.1", "remote_port": 3306},
        config_manager, MagicMock(),
    )


@pytest.mark.parametrize("environment,expected", [("production", True), ("staging", False), ("development", False)])
def test_sql_editor_opens_connectors_read_only_for_production(monkeypatch, environment, expected):
    dialog = _dialog(monkeypatch, environment)
    try:
        connector = dialog._create_db_connector("127.0.0.1", 3306, "u", "p", "app", "")
        assert connector.endpoint.read_only is expected
        assert dialog.session_banner_label.text()
        assert dialog.btn_write_lock.isHidden() is (not expected)
        if environment == "production":
            assert "읽기 전용" in dialog.session_banner_label.text()
    finally:
        for index in range(dialog.editor_tabs.count()):  # no "unsaved changes" modal on close
            tab = dialog.editor_tabs.widget(index)
            if tab:
                tab.is_modified = False
        dialog.close()


# ------------------------------------------------------------------ SQL file dialog

def _file_dialog(environment):
    from src.ui.dialogs.test_dialogs import SQLExecutionDialog

    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("u", "p")
    dialog = SQLExecutionDialog(
        None,
        {"id": "t", "name": "n", "connection_mode": "direct", "environment": environment,
         "remote_host": "db", "remote_port": 3306, "default_database": "app"},
        config_manager, MagicMock(),
    )
    return dialog


def test_file_dialog_banner_and_policy_follow_the_environment():
    prod = _file_dialog("production")
    assert prod._session_read_only() is True
    assert "읽기 전용" in prod.session_banner_label.text()
    assert not prod.btn_write_lock.isHidden() or prod.btn_write_lock.isVisibleTo(prod)
    dev = _file_dialog("development")
    assert dev._session_read_only() is False
    assert "쓰기 가능" in dev.session_banner_label.text()


def test_file_dialog_unlock_is_per_window_and_relocks_on_target_change():
    dialog = _file_dialog("production")
    dialog.db_combo.addItem("app")
    dialog.db_combo.setCurrentText("app")
    with patch("src.ui.dialogs.production_session.SchemaConfirmDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        dialog._unlock_writes()
        assert dialog_cls.call_args.args[2] == "app"
    assert dialog._session_read_only() is False
    assert "쓰기 해제됨" in dialog.session_banner_label.text()
    other = _file_dialog("production")  # another window is still locked
    assert other._session_read_only() is True
    dialog.db_combo.setCurrentText("other_db")  # changing the target relocks
    assert dialog._session_read_only() is True
    assert "읽기 전용" in dialog.session_banner_label.text()


def test_file_dialog_runs_the_worker_read_only_until_unlocked(monkeypatch, tmp_path):
    dialog = _file_dialog("production")
    dialog.sql_file = str(tmp_path / "x.sql")
    dialog._resolve_connection = lambda: ("127.0.0.1", 3306, None)
    created = []

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            created.append(kwargs)
            self.progress = MagicMock()
            self.output = MagicMock()
            self.finished = MagicMock()

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr("src.ui.workers.test_worker.SQLExecutionWorker", FakeWorker)
    dialog.execute_sql()
    assert created[-1]["read_only"] is True
    dialog._write_unlocked = True
    dialog.execute_sql()
    assert created[-1]["read_only"] is False


def test_sql_file_worker_opens_its_connector_read_only(monkeypatch, tmp_path):
    from src.ui.workers.sql_execution_worker import SQLExecutionWorker

    sql_file = tmp_path / "s.sql"
    sql_file.write_text("SELECT 1;", encoding="utf-8")
    seen = {}

    def fake_create(*args, **kwargs):
        seen.update(kwargs)
        connector = MagicMock()
        connector.connect.return_value = (False, "stop here")
        return connector

    monkeypatch.setattr("src.ui.workers.sql_execution_worker.create_rust_db_connector", fake_create)
    SQLExecutionWorker(str(sql_file), "h", 1, "u", "p", "d", read_only=True).run()
    assert seen["read_only"] is True
    seen.clear()
    SQLExecutionWorker(str(sql_file), "h", 1, "u", "p", "d").run()
    assert "read_only" not in seen
