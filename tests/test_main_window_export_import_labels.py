import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.ui import main_window
from src.core.scheduler import BackupScheduler
from src.core.tunnel_monitor import TunnelMonitor
from src.ui.controllers.tray_controller import TrayController
from src.ui.main_window import TunnelManagerUI


app = QApplication.instance() or QApplication(sys.argv)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 자동 시작(터널 미연결 시 자동/확인 후 연결) 경로가 있는 메서드들.
# 모두 _ensure_tunnel_running 헬퍼 하나로 라우팅되어야 한다.
AUTO_START_METHODS = (
    "_on_tree_db_connect",
    "open_sql_editor",
    "_context_rust_core_export",
    "_context_rust_core_import",
    "_context_orphan_check",
)


class _TrustPathConfigManager:
    def __init__(self, events):
        self.events = events

    def get_tunnel_credentials(self, tunnel_id):
        self.events.append("credentials")
        return "db-user", "db-password"


class _TrustPathWindow:
    def __init__(self, events):
        self.events = events
        self.engine = MagicMock()
        self.config_mgr = _TrustPathConfigManager(events)
        self._wizard_launcher = MagicMock()
        for action in ("start_export", "start_import", "start_orphan_check"):
            self._wizard_launcher._launch_rust_dump_wizard.configure_mock()

    def _require_db_credentials(self, tunnel):
        return TunnelManagerUI._require_db_credentials(self, tunnel)

    def _ensure_tunnel_running(self, tunnel, *, prompt=False):
        self.events.append("tunnel")
        return True


def _invoke_interactive_path(window, method_name, tunnel):
    getattr(TunnelManagerUI, method_name)(window, tunnel)


def test_main_window_export_import_labels_match_rust_core_implementation():
    source = (PROJECT_ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

    stale_shell_terms = [
        "Shell Export",
        "Shell Import",
        "_context_shell_export",
        "_context_shell_import",
    ]

    for term in stale_shell_terms:
        assert term not in source

    assert "Rust DB Core Export" in source
    assert "Rust DB Core Import" in source
    assert "_context_rust_core_export" in source
    assert "_context_rust_core_import" in source


def test_tree_orphan_check_signal_is_wired_to_context_handler():
    # 고아 레코드 분석 트리 진입점이 복원되어 _context_orphan_check로 연결되는지.
    source = (PROJECT_ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

    assert "tunnel_orphan_check.connect(self._on_tree_orphan_check)" in source
    handler = inspect.getsource(TunnelManagerUI._on_tree_orphan_check)
    assert "_context_orphan_check" in handler


# --- 1. 자동시작 경로가 _ensure_tunnel_running 래퍼를 거치는지 ---

def test_auto_start_paths_route_through_ensure_tunnel_running():
    for name in AUTO_START_METHODS:
        source = inspect.getsource(getattr(TunnelManagerUI, name))
        assert "_ensure_tunnel_running" in source, f"{name}가 _ensure_tunnel_running을 사용하지 않음"
        assert ".engine.start_tunnel(" not in source, f"{name}가 engine.start_tunnel을 직접 호출함"


@pytest.mark.parametrize("method_name", AUTO_START_METHODS)
@pytest.mark.parametrize(
    "failure_mode",
    ("unknown_declined", "changed", "cancelled", "approval_race"),
)
def test_main_window_interactive_paths_fail_before_credentials_or_actions(
    monkeypatch, method_name, failure_mode
):
    events = []
    window = _TrustPathWindow(events)
    tunnel = {
        "id": "ssh-1",
        "name": "SSH",
        "connection_mode": "ssh_tunnel",
    }

    def reject_trust(parent, engine, config):
        events.append(f"trust:{failure_mode}")
        return False

    class BlockedDialog:
        def __init__(self, *args, **kwargs):
            events.append("dialog")
            self.radio_tunnel = MagicMock()
            self.combo_tunnel = MagicMock()
            self.combo_tunnel.count.return_value = 0

        def on_mode_changed(self):
            pass

        def exec(self):
            pass

    monkeypatch.setattr(main_window, "ensure_ssh_host_trusted", reject_trust)
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.DBConnectionDialog", BlockedDialog)
    monkeypatch.setattr(main_window, "SQLEditorDialog", BlockedDialog)
    window._wizard_launcher._launch_rust_dump_wizard.side_effect = (
        lambda *args: events.append("wizard")
    )

    _invoke_interactive_path(window, method_name, tunnel)

    assert events == [f"trust:{failure_mode}"]
    window._wizard_launcher._launch_rust_dump_wizard.assert_not_called()


@pytest.mark.parametrize("method_name", AUTO_START_METHODS)
def test_main_window_interactive_paths_approve_before_credentials_and_action(
    monkeypatch, method_name
):
    events = []
    window = _TrustPathWindow(events)
    tunnel = {
        "id": "ssh-1",
        "name": "SSH",
        "connection_mode": "ssh_tunnel",
    }

    monkeypatch.setattr(
        main_window,
        "ensure_ssh_host_trusted",
        lambda *args: events.append("trust") or True,
    )

    class FakeRadio:
        def setChecked(self, checked):
            pass

    class FakeCombo:
        def count(self):
            return 0

    class FakeDBConnectionDialog:
        def __init__(self, *args, **kwargs):
            self.radio_tunnel = FakeRadio()
            self.combo_tunnel = FakeCombo()
            events.append("dialog")

        def on_mode_changed(self):
            pass

        def exec(self):
            pass

    class FakeSQLEditorDialog:
        def __init__(self, *args, **kwargs):
            events.append("dialog")

        def exec(self):
            pass

    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.DBConnectionDialog", FakeDBConnectionDialog
    )
    monkeypatch.setattr(main_window, "SQLEditorDialog", FakeSQLEditorDialog)
    window._wizard_launcher._launch_rust_dump_wizard.side_effect = (
        lambda *args: events.append("wizard")
    )

    _invoke_interactive_path(window, method_name, tunnel)

    assert events[:3] == ["trust", "credentials", "tunnel"]
    assert events[3] in ("dialog", "wizard")


def test_main_window_direct_path_bypasses_ssh_probe_and_continues():
    events = []
    window = _TrustPathWindow(events)
    window._wizard_launcher._launch_rust_dump_wizard.side_effect = (
        lambda *args: events.append("wizard")
    )
    tunnel = {
        "id": "direct-1",
        "name": "Direct",
        "connection_mode": "direct",
    }

    TunnelManagerUI._context_rust_core_export(window, tunnel)

    window.engine.inspect_ssh_server.assert_not_called()
    assert events == ["credentials", "tunnel", "wizard"]


# --- 2. _ensure_tunnel_running이 start_tunnel에 위임하는지 ---

class _DummyEngine:
    def __init__(self, running=False):
        self._running = running

    def is_running(self, tunnel_id):
        return self._running


class _DummyEnsureWindow:
    """_ensure_tunnel_running 위임 확인용 최소 더미 (실제 QMainWindow 생성 없이 테스트)."""

    def __init__(self, running=False):
        self.engine = _DummyEngine(running)
        self.started_with = []

    def start_tunnel(self, tunnel):
        self.started_with.append(tunnel)
        return True


def test_ensure_tunnel_running_delegates_to_start_tunnel_without_prompt():
    dummy = _DummyEnsureWindow(running=False)
    tunnel = {"id": "t1", "name": "터널1", "connection_mode": "ssh_tunnel"}

    result = TunnelManagerUI._ensure_tunnel_running(dummy, tunnel, prompt=False)

    assert result is True
    assert dummy.started_with == [tunnel]


def test_ensure_tunnel_running_prompt_decline_skips_start(monkeypatch):
    dummy = _DummyEnsureWindow(running=False)
    tunnel = {"id": "t1", "name": "터널1", "connection_mode": "ssh_tunnel"}

    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *args, **kwargs: main_window.QMessageBox.StandardButton.No,
    )

    result = TunnelManagerUI._ensure_tunnel_running(dummy, tunnel, prompt=True)

    assert result is False
    assert dummy.started_with == []


def test_ensure_tunnel_running_direct_mode_always_true():
    dummy = _DummyEnsureWindow(running=False)
    tunnel = {"id": "t1", "name": "직접연결", "connection_mode": "direct"}

    result = TunnelManagerUI._ensure_tunnel_running(dummy, tunnel, prompt=True)

    assert result is True
    assert dummy.started_with == []


# --- 3. 연결 테스트가 스레드(worker) 기반으로 동작하는지 ---

def test_connection_tests_are_threaded():
    source = (PROJECT_ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "QApplication.processEvents()" not in source

    test_conn_source = inspect.getsource(TunnelManagerUI._on_tree_test_connection)
    assert "_run_connection_test" in test_conn_source
    assert "TestType.TUNNEL_ONLY" in test_conn_source

    direct_conn_source = inspect.getsource(TunnelManagerUI._test_direct_connection)
    assert "_run_connection_test" in direct_conn_source
    assert "TestType.DB_ONLY" in direct_conn_source


def test_main_window_connection_test_uses_result_signal_and_checks_trust_first(
    monkeypatch,
):
    events = []
    workers = []

    class FakeSignal:
        def __init__(self):
            self.connect = MagicMock()

    class FakeWorker:
        def __init__(self, *args):
            events.append("worker")
            self.progress = FakeSignal()
            self.test_finished = FakeSignal()
            self.finished = FakeSignal()
            self.start = MagicMock(side_effect=lambda: events.append("start"))
            self.deleteLater = MagicMock()
            workers.append(self)

    class FakeDialog:
        def __init__(self, *args):
            self.update_progress = MagicMock()

        def exec(self):
            events.append("exec")

    class DummyWindow:
        def __init__(self):
            self.engine = object()
            self.config_mgr = object()
            self._status_bar = MagicMock()

        def statusBar(self):
            return self._status_bar

        def _on_connection_test_finished(self, *args):
            pass

    dummy = DummyWindow()
    config = {"id": "ssh", "name": "SSH", "connection_mode": "ssh_tunnel"}

    def ensure(parent, engine, checked_config):
        events.append("trust")
        assert parent is dummy
        assert engine is dummy.engine
        assert checked_config is config
        return True

    monkeypatch.setattr(main_window, "ConnectionTestWorker", FakeWorker)
    monkeypatch.setattr(main_window, "TestProgressDialog", FakeDialog)
    monkeypatch.setattr(
        main_window, "ensure_ssh_host_trusted", ensure, raising=False
    )

    TunnelManagerUI._run_connection_test(
        dummy, config, main_window.TestType.TUNNEL_ONLY, "SSH test"
    )

    worker = workers[0]
    assert events == ["trust", "worker", "start", "exec"]
    assert worker.test_finished.connect.call_count == 1
    assert worker.finished.connect.call_count == 1
    assert worker.finished.connect.call_args.args[0] is worker.deleteLater


def test_main_window_manual_start_requires_ssh_trust(monkeypatch):
    class DummyWindow:
        def __init__(self):
            self.engine = MagicMock()
            self.engine.start_tunnel.return_value = (True, "ok")
            self._status_bar = MagicMock()
            self.tray_icon = MagicMock()
            self._register_login_path = MagicMock()
            self.refresh_table = MagicMock()

        def statusBar(self):
            return self._status_bar

    dummy = DummyWindow()
    config = {"id": "ssh", "name": "SSH", "connection_mode": "ssh_tunnel"}
    ensure = MagicMock(return_value=False)
    monkeypatch.setattr(
        main_window, "ensure_ssh_host_trusted", ensure, raising=False
    )

    assert TunnelManagerUI.start_tunnel(dummy, config) is False
    ensure.assert_called_once_with(dummy, dummy.engine, config)
    dummy.engine.start_tunnel.assert_not_called()


def test_background_tunnel_paths_remain_noninteractive():
    methods = (
        TunnelManagerUI._auto_connect_tunnels,
        BackupScheduler._resolve_connection,
        TunnelMonitor._reconnect_after_delay,
    )
    for method in methods:
        source = inspect.getsource(method)
        assert "ensure_ssh_host_trusted" not in source
        assert "QMessageBox" not in source


# --- 4. 조용한 리로드(_reload_and_refresh) ---

class _DummyConfigManager:
    def __init__(self, tunnels):
        self._tunnels = tunnels

    def load_config(self):
        return {"tunnels": self._tunnels}


class _DummyReloadWindow:
    def __init__(self):
        self.config_mgr = _DummyConfigManager([{"id": "t1", "name": "T1"}])
        self.config_data = {}
        self.tunnels = []
        self.refresh_called = 0

    def refresh_table(self):
        self.refresh_called += 1

    def _reload_and_refresh(self):
        # reload_config()가 self._reload_and_refresh()를 호출하므로,
        # 더미에도 실제 구현으로 위임하는 메서드가 필요하다.
        TunnelManagerUI._reload_and_refresh(self)


def test_reload_and_refresh_is_silent(monkeypatch):
    dummy = _DummyReloadWindow()
    info_calls = []
    monkeypatch.setattr(
        main_window.QMessageBox, "information", lambda *a, **k: info_calls.append(a)
    )

    TunnelManagerUI._reload_and_refresh(dummy)

    assert dummy.tunnels == [{"id": "t1", "name": "T1"}]
    assert dummy.refresh_called == 1
    assert info_calls == []


def test_reload_config_still_shows_notification(monkeypatch):
    dummy = _DummyReloadWindow()
    info_calls = []
    monkeypatch.setattr(
        main_window.QMessageBox, "information", lambda *a, **k: info_calls.append(a)
    )

    TunnelManagerUI.reload_config(dummy)

    assert dummy.refresh_called == 1
    assert len(info_calls) == 1


def test_group_and_dnd_methods_use_silent_reload():
    for name in ("_on_tunnel_moved", "add_group_dialog", "_edit_group_dialog", "_delete_group"):
        source = inspect.getsource(getattr(TunnelManagerUI, name))
        assert "_reload_and_refresh()" in source, f"{name}가 _reload_and_refresh를 호출하지 않음"
        assert "self.reload_config()" not in source, f"{name}가 여전히 reload_config를 직접 호출함"


# --- 5. 스케줄러 완료 콜백의 UI 스레드 마샬링 ---

def test_on_backup_complete_marshals_to_ui_thread():
    source = inspect.getsource(TunnelManagerUI._on_backup_complete)
    assert "QMetaObject.invokeMethod" in source
    assert "_show_backup_complete_notification" in source
    assert "tray_icon.showMessage" not in source


def test_show_backup_complete_notification_shows_tray_message():
    source = inspect.getsource(TrayController._notify_backup_result)
    assert "tray_icon.showMessage" in source


# --- 6. 하트비트 갱신이 해당 터널 행만 갱신하는지 ---

class _DummyTunnelTree:
    def __init__(self):
        self.updated = []
        self.buttons_set = []

    def update_tunnel_status(self, tunnel_id, is_active):
        self.updated.append((tunnel_id, is_active))

    def set_power_button(self, tunnel_id, button):
        self.buttons_set.append((tunnel_id, button))


class _DummyHeartbeatEngine:
    def is_running(self, tunnel_id):
        return tunnel_id == "t1"


class _DummyHeartbeatWindow:
    def __init__(self):
        self.tunnels = [{"id": "t1", "name": "T1"}, {"id": "t2", "name": "T2"}]
        self.engine = _DummyHeartbeatEngine()
        self.tunnel_tree = _DummyTunnelTree()
        self.repaint_calls = 0

    def _build_power_button(self, tunnel, is_active):
        return f"button-{tunnel['id']}-{is_active}"

    def _schedule_repaint(self):
        self.repaint_calls += 1

    def refresh_table(self):
        raise AssertionError("heartbeat 경로에서 전체 refresh_table이 호출되면 안 됨")


def test_update_tunnel_status_ui_updates_only_target_tunnel():
    dummy = _DummyHeartbeatWindow()

    TunnelManagerUI._update_tunnel_status_ui(dummy, "t1")

    assert dummy.tunnel_tree.updated == [("t1", True)]
    assert dummy.tunnel_tree.buttons_set == [("t1", "button-t1-True")]
    assert dummy.repaint_calls == 1


def test_update_tunnel_status_ui_missing_tunnel_is_noop():
    dummy = _DummyHeartbeatWindow()

    TunnelManagerUI._update_tunnel_status_ui(dummy, "missing-id")

    assert dummy.tunnel_tree.updated == []
    assert dummy.tunnel_tree.buttons_set == []
    assert dummy.repaint_calls == 0


# --- 7. 테이블 시대 죽은 코드 삭제 ---

def test_dead_table_era_code_removed():
    source = (PROJECT_ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
    for marker in (
        "def show_context_menu",
        "rowAt(",
        "def run_sql_file",
        "SQL_FILE_EXECUTION_FEATURE_ENABLED",
    ):
        assert marker not in source, f"삭제되어야 할 죽은 코드가 남아있음: {marker}"


# --- 8. 컬럼 리사이즈 시그널 연결 ---

def test_column_resize_signal_is_connected():
    source = (PROJECT_ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "header().sectionResized.connect(self._on_column_resized)" in source
