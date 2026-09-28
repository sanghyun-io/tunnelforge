import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from src.ui.dialogs.db_connection_dialog import DBConnectionDialog


app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, lambda *args: None)


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    assert predicate()


class BlockingConnector:
    def __init__(self, success=True):
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.success = success
        self.connect_thread = None
        self.close_thread = None

    def connect(self):
        self.connect_thread = threading.get_ident()
        self.started.set()
        assert self.release.wait(2), "test connection was not released"
        return self.success, "connection result"

    def disconnect(self):
        self.close_thread = threading.get_ident()
        self.closed.set()


@pytest.mark.parametrize("test_only", [False, True])
def test_connection_wait_keeps_gui_responsive_and_rejects_duplicate_start(monkeypatch, test_only):
    connector = BlockingConnector()
    dialog = DBConnectionDialog()
    dialog.input_user.setText("fixture_user")
    factory = MagicMock(return_value=("mysql", connector))
    monkeypatch.setattr(dialog, "_build_connector_or_raise", factory)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(5)
    try:
        start = dialog.test_connection if test_only else dialog.do_connect
        start()
        pump_until(lambda: connector.started.is_set() and len(ticks) >= 3)
        assert connector.connect_thread != threading.get_ident()
        start()
        assert factory.call_count == 1
        assert not dialog.input_user.isEnabled()
        connector.release.set()
        if test_only:
            pump_until(lambda: dialog.btn_connect.isEnabled())
            assert connector.closed.is_set()
            assert connector.close_thread != threading.get_ident()
            assert dialog.get_connector() is None
        else:
            pump_until(lambda: dialog.result() == QDialog.DialogCode.Accepted)
            assert dialog.get_connector() is connector
            assert not connector.closed.is_set()
    finally:
        connector.release.set()
        timer.stop()
        dialog.reject()
        app.processEvents()


@pytest.mark.parametrize("destroy", [False, True])
def test_cancel_or_destroy_does_not_adopt_late_success_and_closes_in_background(monkeypatch, destroy):
    from PyQt6 import sip
    from src.ui.workers.db_connection_worker import has_active_connection_workers

    connector = BlockingConnector()
    dialog = DBConnectionDialog()
    dialog.input_user.setText("fixture_user")
    monkeypatch.setattr(dialog, "_build_connector_or_raise", lambda *args: ("mysql", connector))
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.do_connect()
    pump_until(connector.started.is_set)
    if destroy:
        sip.delete(dialog)
    else:
        dialog.reject()
        assert dialog.result() == QDialog.DialogCode.Rejected
    assert has_active_connection_workers()
    connector.release.set()
    pump_until(lambda: connector.closed.is_set() and not has_active_connection_workers())
    assert not accepted
    assert connector.close_thread != threading.get_ident()


def test_finished_but_unclaimed_connection_is_closed_after_cancel(monkeypatch):
    from src.ui.workers.db_connection_worker import has_active_connection_workers

    connector = BlockingConnector()
    dialog = DBConnectionDialog()
    dialog.input_user.setText("fixture_user")
    monkeypatch.setattr(dialog, "_build_connector_or_raise", lambda *args: ("mysql", connector))
    dialog.do_connect()
    worker = dialog._connection_worker
    connector.release.set()
    assert worker.wait(2000)  # Do not deliver the queued GUI completion yet.
    assert has_active_connection_workers()  # The unclaimed result still needs cleanup.
    dialog.reject()
    pump_until(lambda: connector.closed.is_set() and not has_active_connection_workers())
    assert dialog.get_connector() is None
    assert connector.close_thread != threading.get_ident()


def test_main_connection_test_delivers_result_via_result_signal(monkeypatch):
    from src.ui import main_window

    class Worker(QThread):
        progress = pyqtSignal(str)
        test_finished = pyqtSignal(bool, str)

        def __init__(self, *args):
            super().__init__()

        def run(self):
            self.test_finished.emit(True, "verified")

    class Progress(QDialog):
        def __init__(self, *args):
            super().__init__()

        def update_progress(self, *args):
            pass

    received = []
    qt_errors = []
    monkeypatch.setattr("sys.excepthook", lambda *args: qt_errors.append(args))
    window = SimpleNamespace(engine=None, config_mgr=None, statusBar=lambda: MagicMock())
    window._on_connection_test_finished = lambda dialog, name, success, message: (
        received.append((success, message)), dialog.accept()
    )
    monkeypatch.setattr(main_window, "ConnectionTestWorker", Worker)
    monkeypatch.setattr(main_window, "TestProgressDialog", Progress)
    QTimer.singleShot(100, lambda: [w.reject() for w in app.topLevelWidgets() if isinstance(w, Progress)])
    main_window.TunnelManagerUI._run_connection_test(window, {"name": "fixture"}, None, "test")
    pump_until(lambda: not any(w.isRunning() for w in getattr(window, "_connection_test_workers", ())))
    assert received == [(True, "verified")]
    assert not qt_errors


def test_failed_connection_cleans_up_and_allows_retry(monkeypatch):
    connector = BlockingConnector(success=False)
    connector.release.set()
    dialog = DBConnectionDialog()
    dialog.input_user.setText("fixture_user")
    monkeypatch.setattr(dialog, "_build_connector_or_raise", lambda *args: ("postgresql", connector))
    dialog.do_connect()
    pump_until(lambda: dialog._connection_worker is None)
    assert connector.closed.is_set()
    assert connector.close_thread != threading.get_ident()
    assert dialog.get_connector() is None
    assert dialog.btn_connect.isEnabled()
    dialog.reject()


def test_application_exit_waits_for_queued_connection_cleanup(monkeypatch):
    from src.ui import main_window

    monkeypatch.setattr(main_window, "has_active_connection_workers", lambda: True)
    window = SimpleNamespace(prepare_for_shutdown=MagicMock())
    assert main_window.TunnelManagerUI.close_app(window) is False
    window.prepare_for_shutdown.assert_not_called()


def test_window_close_is_ignored_when_connection_cleanup_prevents_exit():
    from src.ui.main_window import TunnelManagerUI

    config = MagicMock()
    config.get_app_setting.return_value = "exit"
    window = SimpleNamespace(_save_column_ratios=MagicMock(),
                             engine=SimpleNamespace(active_tunnels={}),
                             config_mgr=config, close_app=lambda: False)
    event = MagicMock()
    TunnelManagerUI.closeEvent(window, event)
    event.ignore.assert_called_once()
