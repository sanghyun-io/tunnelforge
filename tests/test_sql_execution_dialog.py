import os
import sys
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ui.dialogs import test_dialogs as sql_execution_module
from src.ui.dialogs.test_dialogs import SQLExecutionDialog, TestProgressDialog


app = QApplication.instance() or QApplication(sys.argv)
ORIGINAL_REFRESH_DATABASES = SQLExecutionDialog.refresh_databases


def test_progress_dialog_is_not_a_pytest_test_class():
    assert TestProgressDialog.__test__ is False


def _ssh_config():
    return {
        "id": "ssh-1",
        "name": "SSH",
        "connection_mode": "ssh_tunnel",
        "remote_host": "db.internal",
        "remote_port": 3306,
        "local_port": 3307,
        "db_engine": "mysql",
    }


def _make_dialog(monkeypatch, ensure_result=True):
    monkeypatch.setattr(
        sql_execution_module,
        "ensure_ssh_host_trusted",
        MagicMock(return_value=ensure_result),
        raising=False,
    )
    monkeypatch.setattr(SQLExecutionDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("db-user", "db-password")
    engine = MagicMock()
    engine.is_running.return_value = False
    return SQLExecutionDialog(None, _ssh_config(), config_manager, engine), config_manager, engine


@pytest.mark.parametrize(
    "failure_mode",
    ("unknown_declined", "changed", "cancelled", "approval_race"),
)
def test_sql_execution_constructor_fails_closed_before_credentials(
    monkeypatch, failure_mode
):
    ensure = MagicMock(return_value=False, name=failure_mode)
    monkeypatch.setattr(
        sql_execution_module, "ensure_ssh_host_trusted", ensure, raising=False
    )
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.side_effect = AssertionError(
        "blocked dialog must not decrypt credentials"
    )
    engine = MagicMock()

    dialog = SQLExecutionDialog(None, _ssh_config(), config_manager, engine)
    try:
        ensure.assert_called_once_with(dialog, engine, dialog.config)
        config_manager.get_tunnel_credentials.assert_not_called()
        engine.create_temp_tunnel.assert_not_called()
        assert dialog.btn_execute.isEnabled() is False
    finally:
        dialog.close()


def test_sql_execution_constructor_approves_before_credentials(monkeypatch):
    events = []
    monkeypatch.setattr(
        sql_execution_module,
        "ensure_ssh_host_trusted",
        lambda *args: events.append("trust") or True,
        raising=False,
    )
    monkeypatch.setattr(SQLExecutionDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.side_effect = (
        lambda tunnel_id: events.append("credentials") or ("db-user", "db-password")
    )
    engine = MagicMock()

    dialog = SQLExecutionDialog(None, _ssh_config(), config_manager, engine)
    try:
        assert events[:2] == ["trust", "credentials"]
    finally:
        dialog.close()


@pytest.mark.parametrize(
    "method_name",
    ("_update_execute_button", "refresh_databases", "execute_sql"),
)
def test_sql_execution_actions_fail_before_credentials_temp_tunnel_or_worker(
    monkeypatch, method_name
):
    dialog, config_manager, engine = _make_dialog(monkeypatch, ensure_result=True)
    try:
        dialog.sql_file = "query.sql"
        config_manager.reset_mock()
        engine.reset_mock()
        monkeypatch.setattr(
            sql_execution_module,
            "ensure_ssh_host_trusted",
            MagicMock(return_value=False),
            raising=False,
        )

        if method_name == "refresh_databases":
            ORIGINAL_REFRESH_DATABASES(dialog)
        else:
            getattr(dialog, method_name)()

        config_manager.get_tunnel_credentials.assert_not_called()
        engine.create_temp_tunnel.assert_not_called()
        assert dialog.worker is None
    finally:
        dialog.close()


def test_sql_execution_direct_mode_bypasses_ssh_probe(monkeypatch):
    monkeypatch.setattr(SQLExecutionDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("db-user", "db-password")
    engine = MagicMock()
    config = {
        "id": "direct-1",
        "name": "Direct",
        "connection_mode": "direct",
        "remote_host": "127.0.0.1",
        "remote_port": 3306,
    }

    dialog = SQLExecutionDialog(None, config, config_manager, engine)
    try:
        engine.inspect_ssh_server.assert_not_called()
        config_manager.get_tunnel_credentials.assert_called()
        engine.create_temp_tunnel.assert_not_called()
    finally:
        dialog.close()
