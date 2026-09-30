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


# ---- tunnel start (TF-STATUS-120 follow-up) ---------------------------------------------

from src.ui.dialogs.preselected_connect_dialog import TunnelStartDialog, start_tunnel_with_progress


class BlockingEngine:
    def __init__(self, success=True, running=False, on_start=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = []
        self.success = success
        self.running = running
        self.on_start = on_start
        self.start_calls = 0
        self.start_thread = None

    def is_running(self, tunnel_id):
        return self.running

    def start_tunnel(self, config, check_port=True):
        self.start_calls += 1
        self.start_thread = threading.get_ident()
        self.started.set()
        if self.on_start:
            self.on_start()
        assert self.release.wait(3), "tunnel start was not released"
        return self.success, "ok" if self.success else "ssh failed"

    def stop_tunnel(self, tunnel_id):
        self.stopped.append(tunnel_id)
        return True


CONFIG = {"id": "t1", "name": "tunnel-one"}


def _cancel_dialog_when_started(engine, then_release=True):
    def step():
        if not engine.started.is_set():
            QTimer.singleShot(5, step)
            return
        for widget in app.topLevelWidgets():
            if isinstance(widget, TunnelStartDialog):
                widget.reject()
        if then_release:
            engine.release.set()

    QTimer.singleShot(0, step)


def test_tunnel_start_keeps_gui_responsive(monkeypatch):
    engine = BlockingEngine()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)

    def release():
        if engine.started.is_set() and len(ticks) >= 3:
            engine.release.set()
        else:
            QTimer.singleShot(5, release)

    QTimer.singleShot(0, release)
    try:
        assert start_tunnel_with_progress(QWidget(), engine, CONFIG) == (True, "ok")
    finally:
        timer.stop()
    assert engine.start_thread != threading.get_ident() and len(ticks) >= 3
    pump_until(lambda: not has_active_connection_workers())


def test_tunnel_start_failure_is_returned_not_raised():
    engine = BlockingEngine(success=False)
    engine.release.set()
    assert start_tunnel_with_progress(QWidget(), engine, CONFIG) == (False, "ssh failed")


def test_cancelled_tunnel_start_discards_result_and_closes_late_tunnel():
    engine = BlockingEngine()
    _cancel_dialog_when_started(engine)
    assert start_tunnel_with_progress(QWidget(), engine, CONFIG) is None
    pump_until(lambda: engine.stopped == ["t1"] and not has_active_connection_workers())


def test_cancel_does_not_stop_a_tunnel_that_was_already_running():
    engine = BlockingEngine(running=True)
    _cancel_dialog_when_started(engine)
    assert start_tunnel_with_progress(QWidget(), engine, CONFIG) is None
    pump_until(lambda: not has_active_connection_workers())
    assert engine.stopped == []


def test_second_start_is_refused_while_cancelled_attempt_is_cleaning_up():
    engine = BlockingEngine()
    _cancel_dialog_when_started(engine, then_release=False)
    assert start_tunnel_with_progress(QWidget(), engine, CONFIG) is None
    ok, message = start_tunnel_with_progress(QWidget(), engine, CONFIG)
    assert ok is False and engine.start_calls == 1
    engine.release.set()
    pump_until(lambda: not has_active_connection_workers())
    engine.release.set()
    assert start_tunnel_with_progress(QWidget(), engine, CONFIG) == (True, "ok")


def test_destroyed_tunnel_dialog_closes_late_tunnel():
    engine = BlockingEngine()
    dialog = TunnelStartDialog(None, engine, CONFIG)
    dialog.start()
    pump_until(engine.started.is_set)
    sip.delete(dialog)
    engine.release.set()
    pump_until(lambda: engine.stopped == ["t1"] and not has_active_connection_workers())


def test_ssh_trust_prompt_during_tunnel_start_is_shown_on_gui_thread(monkeypatch):
    seen = {}
    parent = QWidget()
    prompter = trust_prompts.TrustPrompter(parent)
    monkeypatch.setattr(trust_prompts, "confirm_host_key",
                        lambda _p, prompt: seen.setdefault("thread", threading.current_thread()) and True)
    answers = []
    engine = BlockingEngine(on_start=lambda: answers.append(
        prompter.confirm_host_key(HostKeyPrompt("h", 22, "ssh-ed25519", "SHA256:x"))))
    engine.release.set()
    assert start_tunnel_with_progress(parent, engine, CONFIG) == (True, "ok")
    assert answers == [True] and seen["thread"] is threading.main_thread()


def test_main_window_start_tunnel_is_silent_on_cancel(monkeypatch, boxes):
    from src.ui import main_window

    engine = BlockingEngine()
    messages = []
    dummy = SimpleNamespace(
        engine=engine,
        statusBar=lambda: SimpleNamespace(showMessage=messages.append),
        refresh_table=lambda: None,
    )
    monkeypatch.setattr(main_window, "start_tunnel_with_progress", lambda *a, **k: None)
    assert main_window.TunnelManagerUI.start_tunnel(dummy, CONFIG) is False
    assert boxes == [] and any("취소" in m for m in messages)
