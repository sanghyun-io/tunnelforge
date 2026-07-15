from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import main
import pytest
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import QApplication, QMainWindow

from src.core.db_core_service import DbCoreOutcome, DbCoreRequestKind, DbCoreServiceError
from src.core.error_report_consent import PromptOutcome
from src.ui.main_window import TunnelManagerUI


APP = QApplication.instance() or QApplication([])


class _Policy:
    def __init__(self, claim_id="claim-1"):
        self.claim_id = claim_id
        self.calls = []

    def claim_prompt(self, now):
        self.calls.append(("claim", now))
        return self.claim_id

    def record_outcome(self, claim_id, outcome, now, suppress=False):
        self.calls.append(("record", claim_id, outcome, now, suppress))

    def release_prompt_claim(self, claim_id, now):
        self.calls.append(("release", claim_id, now))


class _FailingRecordPolicy(_Policy):
    def record_outcome(self, claim_id, outcome, now, suppress=False):
        super().record_outcome(claim_id, outcome, now, suppress=suppress)
        raise OSError("settings write failed")


class _Timer:
    def __init__(self):
        self.active = False
        self.starts = []
        self.stop_count = 0

    def start(self, delay):
        self.active = True
        self.starts.append(delay)

    def stop(self):
        self.active = False
        self.stop_count += 1

    def isActive(self):
        return self.active

    def fire(self, window):
        self.active = False
        TunnelManagerUI._maybe_show_error_reporting_consent(window)


class _ConsentDialog:
    events = []

    def __init__(self, parent):
        self.parent = parent
        self.events.append("dialog")

    def exec(self):
        self.events.append("exec")

    def get_outcome(self):
        return PromptOutcome.LATER, True


class _Window:
    def __init__(self, policy, visible=True, minimized=False, active_work=False):
        self._error_reporting_consent_policy = policy
        self._error_reporting_prompt_timer = _Timer()
        self._error_reporting_prompt_scheduled = False
        self._error_reporting_prompt_running = False
        self._error_reporting_prompt_shown = False
        self._error_reporting_prompt_shutdown = False
        self.visible = visible
        self.minimized = minimized
        self.active_work = active_work

    def isVisible(self):
        return self.visible

    def isMinimized(self):
        return self.minimized

    def _has_active_database_operation(self):
        return self.active_work

    def _schedule_error_reporting_prompt(self, delay_ms):
        return TunnelManagerUI._schedule_error_reporting_prompt(self, delay_ms)

    def _stop_error_reporting_prompt_for_shutdown(self):
        return TunnelManagerUI._stop_error_reporting_prompt_for_shutdown(self)

    def prepare_for_shutdown(self):
        return TunnelManagerUI.prepare_for_shutdown(self)

    def _release_error_reporting_prompt_claim(self, claim_id):
        return TunnelManagerUI._release_error_reporting_prompt_claim(self, claim_id)


def test_consent_claim_precedes_dialog_and_uses_exact_token_for_outcome(monkeypatch):
    policy = _Policy()
    window = _Window(policy)
    _ConsentDialog.events = []
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ConsentDialog)
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    monkeypatch.setattr("src.ui.main_window._utc_now", lambda: now)

    TunnelManagerUI._maybe_show_error_reporting_consent(window)

    assert policy.calls == [
        ("claim", now),
        ("record", "claim-1", PromptOutcome.LATER, now, True),
    ]
    assert _ConsentDialog.events == ["dialog", "exec"]
    assert window._error_reporting_prompt_shown is True
    assert window._error_reporting_prompt_running is False


def test_hidden_or_minimized_callback_clears_scheduled_without_claiming(monkeypatch):
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ConsentDialog)

    for visible, minimized in ((False, False), (True, True)):
        policy = _Policy()
        window = _Window(policy, visible=visible, minimized=minimized)
        window._error_reporting_prompt_scheduled = True

        TunnelManagerUI._maybe_show_error_reporting_consent(window)

        assert policy.calls == []
        assert window._error_reporting_prompt_scheduled is False


def test_active_database_operation_reschedules_until_idle_then_prompts_once(monkeypatch):
    policy = _Policy()
    window = _Window(policy, active_work=True)
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ConsentDialog)

    window._schedule_error_reporting_prompt(500)
    window._error_reporting_prompt_timer.fire(window)

    assert policy.calls == []
    assert len(window._error_reporting_prompt_timer.starts) == 2
    retry_delay = window._error_reporting_prompt_timer.starts[-1]
    assert 0 < retry_delay <= 1000
    assert window._error_reporting_prompt_scheduled is True

    window.active_work = False
    window._error_reporting_prompt_timer.fire(window)
    window._error_reporting_prompt_timer.fire(window)

    assert [call[0] for call in policy.calls] == ["claim", "record"]
    assert len(window._error_reporting_prompt_timer.starts) == 2


def test_consent_does_nothing_when_atomic_claim_is_ineligible(monkeypatch):
    policy = _Policy(claim_id=None)
    _ConsentDialog.events = []
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ConsentDialog)

    TunnelManagerUI._maybe_show_error_reporting_consent(_Window(policy))

    assert [call[0] for call in policy.calls] == ["claim"]
    assert _ConsentDialog.events == []


class _VisibleWindow(TunnelManagerUI):
    def __init__(self, policy=None):
        QMainWindow.__init__(self)
        self._error_reporting_consent_policy = policy or _Policy()
        self._error_reporting_prompt_timer = _Timer()
        self._error_reporting_prompt_scheduled = False
        self._error_reporting_prompt_running = False
        self._error_reporting_prompt_shown = False
        self._error_reporting_prompt_shutdown = False
        self.visible = True
        self.minimized = False
        self.active_work = False

    def _apply_column_ratios(self):
        pass

    def _schedule_repaint(self):
        pass

    def isVisible(self):
        return self.visible

    def isMinimized(self):
        return self.minimized

    def _has_active_database_operation(self):
        return self.active_work


def test_prompt_timer_is_parented_single_shot_and_tracks_all_lifecycle_states():
    window = _VisibleWindow()

    TunnelManagerUI._init_error_reporting_prompt_lifecycle(window)

    assert window._error_reporting_prompt_timer.parent() is window
    assert window._error_reporting_prompt_timer.isSingleShot() is True
    assert window._error_reporting_prompt_scheduled is False
    assert window._error_reporting_prompt_running is False
    assert window._error_reporting_prompt_shown is False
    assert window._error_reporting_prompt_shutdown is False
    window.deleteLater()


def test_first_visible_show_event_starts_one_500ms_timer(monkeypatch):
    repaint_timers = []
    monkeypatch.setattr(
        "src.ui.main_window.QTimer.singleShot",
        lambda delay, callback: repaint_timers.append(delay),
    )
    window = _VisibleWindow()

    window.showEvent(QShowEvent())
    window.showEvent(QShowEvent())

    assert window._error_reporting_prompt_timer.starts == [500]
    assert repaint_timers == [50, 50]


def test_restore_after_hidden_callback_causes_exactly_one_eventual_prompt(monkeypatch):
    policy = _Policy()
    window = _VisibleWindow(policy)
    monkeypatch.setattr("src.ui.main_window.QTimer.singleShot", lambda *args: None)
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ConsentDialog)

    window.showEvent(QShowEvent())
    window.visible = False
    window._error_reporting_prompt_timer.fire(window)
    assert window._error_reporting_prompt_scheduled is False

    window.visible = True
    window.showEvent(QShowEvent())
    window.showEvent(QShowEvent())
    window._error_reporting_prompt_timer.fire(window)
    window.showEvent(QShowEvent())

    assert window._error_reporting_prompt_timer.starts == [500, 500]
    assert [call[0] for call in policy.calls] == ["claim", "record"]


def test_running_state_blocks_reentrant_prompt(monkeypatch):
    policy = _Policy()
    window = _Window(policy)

    class _ReentrantDialog(_ConsentDialog):
        def exec(self):
            TunnelManagerUI._maybe_show_error_reporting_consent(self.parent)
            super().exec()

    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ReentrantDialog)

    TunnelManagerUI._maybe_show_error_reporting_consent(window)

    assert [call[0] for call in policy.calls] == ["claim", "record"]


@pytest.mark.parametrize("failure_stage", ["construct", "exec", "outcome"])
def test_ui_failure_releases_exact_claim_without_crashing(monkeypatch, failure_stage):
    policy = _Policy()
    window = _Window(policy)

    class _FailingDialog(_ConsentDialog):
        def __init__(self, parent):
            if failure_stage == "construct":
                raise RuntimeError("construct failed")
            super().__init__(parent)

        def exec(self):
            if failure_stage == "exec":
                raise RuntimeError("exec failed")
            super().exec()

        def get_outcome(self):
            if failure_stage == "outcome":
                raise RuntimeError("outcome failed")
            return super().get_outcome()

    logged = []
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _FailingDialog)
    monkeypatch.setattr("src.ui.main_window.logger.exception", lambda message: logged.append(message))

    TunnelManagerUI._maybe_show_error_reporting_consent(window)

    assert [call[0] for call in policy.calls] == ["claim", "release"]
    assert policy.calls[-1][1] == "claim-1"
    assert window._error_reporting_prompt_running is False
    assert window._error_reporting_prompt_shown is False
    assert logged


def test_shutdown_during_modal_releases_claim_without_reading_or_recording_outcome(monkeypatch):
    policy = _Policy()
    window = _Window(policy)

    class _ShutdownDialog(_ConsentDialog):
        def exec(self):
            self.parent._stop_error_reporting_prompt_for_shutdown()

        def get_outcome(self):
            raise AssertionError("outcome must not be read after shutdown")

    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ShutdownDialog)

    TunnelManagerUI._maybe_show_error_reporting_consent(window)

    assert [call[0] for call in policy.calls] == ["claim", "release"]
    assert policy.calls[-1][1] == "claim-1"
    assert window._error_reporting_prompt_shutdown is True


def test_qapplication_quit_during_fresh_modal_releases_claim_without_default_later():
    script = textwrap.dedent(
        """
        import json
        import os

        os.environ['QT_QPA_PLATFORM'] = 'offscreen'

        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow
        import src.ui.main_window as main_window

        events = []

        class Policy:
            def claim_prompt(self, now):
                events.append('claim')
                return 'claim-1'

            def release_prompt_claim(self, claim_id, now):
                events.append(['release', claim_id])

            def record_outcome(self, *args, **kwargs):
                events.append('record')

        class ConsentDialog(QDialog):
            def __init__(self, parent):
                super().__init__(parent)
                QTimer.singleShot(0, QApplication.instance().quit)

            def exec(self):
                events.append('exec-enter')
                result = super().exec()
                events.append('exec-return')
                return result

            def get_outcome(self):
                events.append('outcome')
                return main_window.PromptOutcome.LATER, False

        class Window(main_window.TunnelManagerUI):
            def __init__(self):
                QMainWindow.__init__(self)
                self._error_reporting_consent_policy = Policy()
                self._init_error_reporting_prompt_lifecycle()

            def isVisible(self):
                return True

            def isMinimized(self):
                return False

            def _has_active_database_operation(self):
                return False

            def prepare_for_shutdown(self):
                events.append('shutdown')
                super().prepare_for_shutdown()

        app = QApplication([])
        window = Window()
        main_window.ErrorReportingConsentDialog = ConsentDialog
        app.aboutToQuit.connect(window.prepare_for_shutdown)
        QTimer.singleShot(0, window._maybe_show_error_reporting_consent)
        app.exec()
        print(json.dumps({
            'events': events,
            'shutdown': window._error_reporting_prompt_shutdown,
        }))
        """
    )
    completed = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        'events': [
            'claim',
            'exec-enter',
            'shutdown',
            'exec-return',
            ['release', 'claim-1'],
        ],
        'shutdown': True,
    }


def test_public_shutdown_hook_is_idempotent():
    window = _Window(_Policy())

    window.prepare_for_shutdown()
    window.prepare_for_shutdown()

    assert window._error_reporting_prompt_shutdown is True
    assert window._error_reporting_prompt_timer.stop_count == 1


def test_record_failure_is_logged_once_without_release_or_duplicate_write(monkeypatch):
    policy = _FailingRecordPolicy()
    window = _Window(policy)
    logged = []
    monkeypatch.setattr("src.ui.main_window.ErrorReportingConsentDialog", _ConsentDialog)
    monkeypatch.setattr("src.ui.main_window.logger.exception", lambda message: logged.append(message))

    TunnelManagerUI._maybe_show_error_reporting_consent(window)

    assert [call[0] for call in policy.calls] == ["claim", "record"]
    assert window._error_reporting_prompt_shown is True
    assert window._error_reporting_prompt_running is False
    assert logged == ["오류 보고 동의 결과 저장 실패"]


def test_active_operation_guard_covers_modal_and_both_detached_worker_paths(monkeypatch):
    monkeypatch.setattr("src.ui.main_window.QApplication.activeModalWidget", lambda: None)
    monkeypatch.setattr("src.ui.main_window.has_active_detached_migration_workers", lambda: False)
    monkeypatch.setattr("src.ui.main_window.has_active_detached_oneclick_workers", lambda: False)
    assert TunnelManagerUI._has_active_database_operation(object()) is False

    monkeypatch.setattr("src.ui.main_window.has_active_detached_migration_workers", lambda: True)
    assert TunnelManagerUI._has_active_database_operation(object()) is True
    monkeypatch.setattr("src.ui.main_window.has_active_detached_migration_workers", lambda: False)
    monkeypatch.setattr("src.ui.main_window.has_active_detached_oneclick_workers", lambda: True)
    assert TunnelManagerUI._has_active_database_operation(object()) is True
    monkeypatch.setattr("src.ui.main_window.has_active_detached_oneclick_workers", lambda: False)
    monkeypatch.setattr("src.ui.main_window.QApplication.activeModalWidget", lambda: object())
    assert TunnelManagerUI._has_active_database_operation(object()) is True


class _ShutdownTarget:
    def __init__(self):
        self._start_background = False
        self.shutdown_calls = 0
        self.config_mgr = type("Config", (), {
            "save_active_tunnels": lambda self, ids: None,
        })()
        self.engine = type("Engine", (), {
            "active_tunnels": {},
            "stop_all": lambda self: None,
        })()
        self.tray_icon = type("Tray", (), {"hide": lambda self: None})()
        self.deleted = False

    def prepare_for_shutdown(self):
        self.shutdown_calls += 1

    def _stop_error_reporting_prompt_for_shutdown(self):
        raise AssertionError('shutdown must use the public hook')

    def deleteLater(self):
        self.deleted = True


def test_close_app_and_smoke_dispose_stop_prompt_lifecycle(monkeypatch):
    app = type("App", (), {"quit": lambda self: None})()
    monkeypatch.setattr("src.ui.main_window.QApplication.instance", lambda: app)

    close_target = _ShutdownTarget()
    TunnelManagerUI.close_app(close_target)
    assert close_target.shutdown_calls == 1

    smoke_target = _ShutdownTarget()
    TunnelManagerUI.dispose_for_smoke_check(smoke_target)
    assert smoke_target.shutdown_calls == 1
    assert smoke_target.deleted is True


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class _App:
    last_instance = None
    emit_about_to_quit = False
    exec_error = None
    exec_result = 0

    def __init__(self, argv):
        type(self).last_instance = self
        self.aboutToQuit = _Signal()

    def setWindowIcon(self, icon):
        pass

    def setQuitOnLastWindowClosed(self, value):
        pass

    def exec(self):
        if self.emit_about_to_quit:
            self.aboutToQuit.emit()
        if self.exec_error is not None:
            raise self.exec_error
        return self.exec_result


class _Config:
    def get_config_path(self):
        return "config.json"

    def get_app_setting(self, key, default=None):
        return default

    def set_app_setting(self, key, value):
        pass


class _Guard:
    secondary = False

    def __init__(self, parent=None):
        self.is_secondary = self.secondary
        self.activation_requested = _Signal()

    @staticmethod
    def notify_existing_instance():
        return True

    def close(self):
        pass


def _install_main_startup_fakes(monkeypatch, window_class):
    _App.emit_about_to_quit = False
    _App.exec_error = None
    _App.exec_result = 0
    _Guard.secondary = False
    monkeypatch.setattr(main, "_load_qapplication_class", lambda: _App)
    monkeypatch.setattr(main, "_load_qicon_class", lambda: lambda path: path)
    monkeypatch.setattr(main, "_load_config_manager_class", lambda: _Config)
    monkeypatch.setattr(main, "_load_tunnel_engine_class", lambda: object)
    monkeypatch.setattr(main, "_load_tunnel_manager_ui_class", lambda: window_class)
    monkeypatch.setattr("src.core.single_instance.SingleInstanceGuard", _Guard)
    monkeypatch.setattr("src.core.i18n.configure_language", lambda *args: "ko")
    monkeypatch.setattr("src.core.i18n.install_qt_i18n", lambda: None)
    monkeypatch.setattr(main.sys, "argv", ["TunnelForge"])


def test_consent_scheduling_is_reached_only_after_primary_instance_acceptance(monkeypatch):
    constructed = []

    class _StartupWindow:
        def __init__(self, config, engine):
            constructed.append(self)

        def show(self):
            assert self.prepare_for_shutdown in _App.last_instance.aboutToQuit.callbacks

        def bring_to_front(self):
            pass

        def prepare_for_shutdown(self):
            pass

    _install_main_startup_fakes(monkeypatch, _StartupWindow)
    monkeypatch.setattr(main, "_shutdown_shared_db_core_facade", lambda: None)

    _Guard.secondary = True
    assert main.main() == 0
    assert constructed == []

    _Guard.secondary = False
    assert main.main() == 0
    assert len(constructed) == 1
    assert constructed[0].prepare_for_shutdown in _App.last_instance.aboutToQuit.callbacks


class _LifecycleWindow:
    def __init__(self, config, engine):
        self.shutdown_calls = 0

    def show(self):
        pass

    def bring_to_front(self):
        pass

    def prepare_for_shutdown(self):
        self.shutdown_calls += 1


def test_main_normal_exit_runs_about_to_quit_and_finally_shutdown(monkeypatch):
    shutdown_calls = []
    _install_main_startup_fakes(monkeypatch, _LifecycleWindow)
    _App.emit_about_to_quit = True
    _App.exec_result = 17
    monkeypatch.setattr(
        main,
        "_shutdown_shared_db_core_facade",
        lambda: shutdown_calls.append("shutdown"),
    )

    assert main.main() == 17

    assert shutdown_calls == ["shutdown", "shutdown"]
    assert _App.last_instance.aboutToQuit.callbacks.count(
        main._shutdown_shared_db_core_facade
    ) == 1


def test_main_secondary_instance_return_runs_finally(monkeypatch):
    shutdown_calls = []
    _install_main_startup_fakes(monkeypatch, _LifecycleWindow)
    _Guard.secondary = True
    monkeypatch.setattr(
        main,
        "_shutdown_shared_db_core_facade",
        lambda: shutdown_calls.append("shutdown"),
    )

    assert main.main() == 0
    assert shutdown_calls == ["shutdown"]


def test_main_startup_failure_before_qapplication_runs_finally(monkeypatch):
    shutdown_calls = []

    def fail_qapplication_load():
        raise RuntimeError("QApplication import failed")

    monkeypatch.setattr(main, "_load_qapplication_class", fail_qapplication_load)
    monkeypatch.setattr(
        main,
        "_shutdown_shared_db_core_facade",
        lambda: shutdown_calls.append("shutdown"),
    )

    with pytest.raises(RuntimeError, match="QApplication import failed"):
        main.main()

    assert shutdown_calls == ["shutdown"]


def test_main_window_startup_failure_runs_finally(monkeypatch):
    shutdown_calls = []

    class _FailingWindow:
        def __init__(self, config, engine):
            raise RuntimeError("window failed")

    _install_main_startup_fakes(monkeypatch, _FailingWindow)
    monkeypatch.setattr(
        main,
        "_shutdown_shared_db_core_facade",
        lambda: shutdown_calls.append("shutdown"),
    )

    with pytest.raises(RuntimeError, match="window failed"):
        main.main()

    assert shutdown_calls == ["shutdown"]


def test_main_event_loop_exception_runs_finally(monkeypatch):
    shutdown_calls = []
    _install_main_startup_fakes(monkeypatch, _LifecycleWindow)
    _App.exec_error = RuntimeError("event loop failed")
    monkeypatch.setattr(
        main,
        "_shutdown_shared_db_core_facade",
        lambda: shutdown_calls.append("shutdown"),
    )

    with pytest.raises(RuntimeError, match="event loop failed"):
        main.main()

    assert shutdown_calls == ["shutdown"]


def test_main_surfaces_residual_owner_join_failure(monkeypatch):
    residual = DbCoreServiceError(
        "DB Core owner did not stop",
        code="db_core_residual_process",
        request_kind=DbCoreRequestKind.MUTATION,
        outcome=DbCoreOutcome.FAILED,
    )
    _install_main_startup_fakes(monkeypatch, _LifecycleWindow)

    def fail_shutdown():
        raise residual

    monkeypatch.setattr(main, "_shutdown_shared_db_core_facade", fail_shutdown)

    with pytest.raises(DbCoreServiceError) as raised:
        main.main()

    assert raised.value is residual
