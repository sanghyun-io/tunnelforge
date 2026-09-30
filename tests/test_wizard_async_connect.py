"""TF-STATUS-120: preselected-tunnel wizard connect runs off the GUI thread."""
import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import sip
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from src.core.ssh_trust import HostKeyPrompt
from src.ui import trust_prompts
from src.ui.dialogs import db_dialogs
from src.ui.dialogs.preselected_connect_dialog import PreselectedConnectDialog
from src.ui.workers.db_connection_worker import has_active_connection_workers

app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    shown = []
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, lambda *args, m=method: shown.append((m, args[2])))
    return shown


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    assert predicate()


class BlockingConnector:
    def __init__(self, success=True, on_connect=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.success = success
        self.on_connect = on_connect
        self.connect_thread = None
        self.close_thread = None

    def connect(self):
        self.connect_thread = threading.get_ident()
        self.started.set()
        if self.on_connect:
            self.on_connect()
        assert self.release.wait(3), "test connection was not released"
        return self.success, "boom" if not self.success else "ok"

    def disconnect(self):
        self.close_thread = threading.get_ident()
        self.closed.set()


def _wizard(monkeypatch, connector):
    monkeypatch.setattr(db_dialogs, "MySQLConnector", lambda *args: connector)
    tunnel = {"id": "t1", "name": "n", "connection_mode": "direct", "remote_host": "h",
              "remote_port": 3306, "db_engine": "mysql"}
    config = SimpleNamespace(get_tunnel_credentials=lambda tid: ("user", "pw"))
    return db_dialogs.RustDumpWizard(QWidget(), SimpleNamespace(), config, tunnel)


def test_wizard_connect_keeps_gui_responsive_and_returns_connector(monkeypatch):
    connector = BlockingConnector()
    wizard = _wizard(monkeypatch, connector)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)

    def release_when_ticking():
        if connector.started.is_set() and len(ticks) >= 3:
            connector.release.set()
        else:
            QTimer.singleShot(5, release_when_ticking)

    QTimer.singleShot(0, release_when_ticking)
    try:
        result, info = wizard._connect_preselected_tunnel()
    finally:
        timer.stop()
    assert result is connector and info == "n_user"
    assert connector.connect_thread != threading.get_ident()
    assert len(ticks) >= 3
    pump_until(lambda: not has_active_connection_workers())
    assert not connector.closed.is_set()  # ownership moved to the wizard, not closed


def test_cancel_discards_late_success_and_closes_in_background(monkeypatch, boxes):
    connector = BlockingConnector()
    wizard = _wizard(monkeypatch, connector)

    def cancel_then_release():
        if not connector.started.is_set():
            QTimer.singleShot(5, cancel_then_release)
            return
        for widget in app.topLevelWidgets():
            if isinstance(widget, PreselectedConnectDialog):
                widget.reject()
        connector.release.set()

    QTimer.singleShot(0, cancel_then_release)
    assert wizard._connect_preselected_tunnel() == (None, None)
    pump_until(lambda: connector.closed.is_set() and not has_active_connection_workers())
    assert connector.close_thread != threading.get_ident()
    assert boxes == []  # cancelling is silent


def test_failed_connect_reports_error_and_cleans_up(monkeypatch, boxes):
    connector = BlockingConnector(success=False)
    connector.release.set()
    wizard = _wizard(monkeypatch, connector)
    assert wizard._connect_preselected_tunnel() == (None, None)
    pump_until(lambda: connector.closed.is_set() and not has_active_connection_workers())
    assert boxes and boxes[0][0] == "critical" and "boom" in boxes[0][1]


def test_destroyed_dialog_does_not_adopt_result(monkeypatch):
    connector = BlockingConnector()
    dialog = PreselectedConnectDialog(None, connector)
    dialog.start()
    pump_until(connector.started.is_set)
    sip.delete(dialog)
    connector.release.set()
    pump_until(lambda: connector.closed.is_set() and not has_active_connection_workers())
    assert connector.close_thread != threading.get_ident()


def test_pending_result_counts_as_active_until_claimed(monkeypatch):
    connector = BlockingConnector()
    connector.release.set()
    dialog = PreselectedConnectDialog(None, connector)
    dialog.start()
    assert has_active_connection_workers()  # from start, including the queued-completion window
    pump_until(lambda: dialog.connector is not None)
    pump_until(lambda: not has_active_connection_workers())


def test_trust_prompt_from_connect_thread_is_shown_on_gui_thread(monkeypatch):
    seen = {}
    parent = QWidget()
    prompter = trust_prompts.TrustPrompter(parent)

    def fake_confirm(_parent, prompt):
        seen["thread"] = threading.current_thread()
        return True

    monkeypatch.setattr(trust_prompts, "confirm_host_key", fake_confirm)
    answers = []
    connector = BlockingConnector(
        on_connect=lambda: answers.append(prompter.confirm_host_key(HostKeyPrompt("h", 22, "ssh-ed25519", "SHA256:x"))))
    connector.release.set()
    dialog = PreselectedConnectDialog(parent, connector)
    dialog.start()
    QTimer.singleShot(0, lambda: None)
    assert dialog.exec() == QDialog.DialogCode.Accepted  # no deadlock while the wait dialog is modal
    assert answers == [True]
    assert seen["thread"] is threading.main_thread()
    pump_until(lambda: not has_active_connection_workers())
