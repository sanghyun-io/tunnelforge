"""앱 종료 시 실행 중인 DB 작업이 있으면 확인을 받고, '아니오'면 종료하지 않는다."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QDialog

from src.ui import main_window

_app = QApplication.instance() or QApplication([])


def _no_detached_work(monkeypatch):
    monkeypatch.setattr(main_window, "has_active_connection_workers", lambda: False)
    monkeypatch.setattr(main_window, "has_active_detached_migration_workers", lambda: False)
    monkeypatch.setattr(main_window, "has_active_detached_oneclick_workers", lambda: False)
    monkeypatch.setattr(main_window, "has_active_explain_workers", lambda: False)


def test_running_work_lists_open_dialog_workers_detached_jobs_and_scheduled_backups(monkeypatch):
    _no_detached_work(monkeypatch)
    dialog = QDialog()
    dialog.setWindowTitle("Import")
    dialog.worker = SimpleNamespace(isRunning=lambda: True)
    dialog.show()
    try:
        monkeypatch.setattr(main_window, "has_active_explain_workers", lambda: True)
        scheduler = SimpleNamespace(has_active_jobs=lambda: True)
        assert main_window.running_background_work(scheduler) == ["Import", "실행 계획(EXPLAIN)", "예약 백업"]
        dialog.worker = SimpleNamespace(isRunning=lambda: False)
        monkeypatch.setattr(main_window, "has_active_explain_workers", lambda: False)
        assert main_window.running_background_work(SimpleNamespace(has_active_jobs=lambda: False)) == []
    finally:
        dialog.close()


def test_close_app_stops_when_the_user_declines_and_quits_when_confirmed(monkeypatch):
    _no_detached_work(monkeypatch)
    monkeypatch.setattr(main_window, "running_background_work", lambda scheduler=None: ["Export"])
    window = SimpleNamespace(prepare_for_shutdown=MagicMock())

    monkeypatch.setattr(main_window, "confirm_quit_with_running_work", lambda parent, running: False)
    assert main_window.TunnelManagerUI.close_app(window) is False
    window.prepare_for_shutdown.assert_not_called()

    monkeypatch.setattr(main_window, "confirm_quit_with_running_work", lambda parent, running: True)
    window = SimpleNamespace(
        prepare_for_shutdown=MagicMock(), _start_background=False,
        config_mgr=MagicMock(), engine=MagicMock(active_tunnels={}), tray_icon=MagicMock(),
    )
    monkeypatch.setattr(main_window.QApplication, "instance", lambda: SimpleNamespace(quit=lambda: None))
    main_window.TunnelManagerUI.close_app(window)
    window.prepare_for_shutdown.assert_called_once()


def test_scheduler_reports_queued_or_running_jobs():
    from src.core.scheduler import BackupScheduler

    scheduler = BackupScheduler.__new__(BackupScheduler)
    scheduler._lock = __import__("threading").Lock()
    scheduler._active_schedule_ids = set()
    assert scheduler.has_active_jobs() is False
    scheduler._active_schedule_ids.add("nightly")
    assert scheduler.has_active_jobs() is True
