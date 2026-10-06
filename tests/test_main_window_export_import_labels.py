import inspect
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.ui import main_window
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

    def update_tunnel_status(self, tunnel_id, is_active, health=None, tooltip=""):
        self.updated.append((tunnel_id, is_active))
        self.health = (health, tooltip)

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
        self._last_tunnel_health = {}

    _apply_tunnel_status = TunnelManagerUI._apply_tunnel_status

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


# --- 9. 연결 UX: 터널 상태(재연결/실패), 삭제 확인, 트레이 ---

from types import SimpleNamespace

from PyQt6.QtWidgets import QMessageBox, QSystemTrayIcon

from src.core.tunnel_monitor import TunnelState, TunnelStatus
from src.ui.controllers.tunnel_actions_controller import TunnelActionsController


class _DummyMonitor:
    def __init__(self, status):
        self.status = status

    def get_status(self, tunnel_id):
        return self.status

    def get_max_reconnect_attempts(self):
        return 5


class _Recorder:
    def __init__(self):
        self.messages = []

    def showMessage(self, *args):
        self.messages.append(args)


def _health_window(status):
    dummy = _DummyHeartbeatWindow()
    dummy.tunnel_monitor = _DummyMonitor(status)
    dummy._status_bar = _Recorder()
    dummy.statusBar = lambda: dummy._status_bar
    dummy.tray_icon = _Recorder()
    return dummy


def test_reconnecting_tunnel_shows_yellow_state_with_attempt_count():
    status = TunnelStatus(tunnel_id="t2", state=TunnelState.RECONNECTING, reconnect_count=2)
    dummy = _health_window(status)

    TunnelManagerUI._update_tunnel_status_ui(dummy, "t2")

    assert dummy.tunnel_tree.health == ("reconnecting", "재연결 중 (2/5)")
    assert dummy.tray_icon.messages == []


def test_tunnel_error_notifies_status_bar_and_tray_once():
    status = TunnelStatus(tunnel_id="t2", state=TunnelState.ERROR, error_message="최대 재연결 시도 횟수 초과")
    dummy = _health_window(status)

    TunnelManagerUI._update_tunnel_status_ui(dummy, "t2")
    TunnelManagerUI._update_tunnel_status_ui(dummy, "t2")

    assert dummy.tunnel_tree.health[0] == "error"
    assert "최대 재연결" in dummy.tunnel_tree.health[1]
    assert len(dummy.tray_icon.messages) == 1
    assert dummy.tray_icon.messages[0][2] == QSystemTrayIcon.MessageIcon.Warning
    assert len(dummy._status_bar.messages) == 1


def test_running_tunnel_ignores_stale_monitor_error():
    status = TunnelStatus(tunnel_id="t1", state=TunnelState.ERROR, error_message="old")
    dummy = _health_window(status)

    TunnelManagerUI._update_tunnel_status_ui(dummy, "t1")

    assert dummy.tunnel_tree.health == (None, "")


def test_row_manage_buttons_have_no_delete_button():
    source = inspect.getsource(TunnelManagerUI._build_manage_buttons)
    assert "delete_tunnel" not in source
    assert 'tr("common.delete")' not in source


def _delete_window(schedules):
    saved = []
    window = SimpleNamespace(
        engine=SimpleNamespace(is_running=lambda tid: False),
        scheduler=SimpleNamespace(get_schedules=lambda: schedules),
        tunnels=[{"id": "t1", "name": "운영"}],
    )
    controller = TunnelActionsController(window)
    controller.save_and_refresh = lambda: saved.append(True)
    return window, controller, saved


def test_delete_confirm_defaults_to_no_and_mentions_schedules(monkeypatch):
    calls = []

    def fake_question(*args):
        calls.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    schedules = [SimpleNamespace(tunnel_id="t1"), SimpleNamespace(tunnel_id="t1"), SimpleNamespace(tunnel_id="x")]
    window, controller, saved = _delete_window(schedules)

    controller.delete_tunnel(window.tunnels[0])

    _, _, message, _, default_button = calls[0]
    assert default_button == QMessageBox.StandardButton.No
    assert "예약 백업이 2개" in message
    assert saved == [] and len(window.tunnels) == 1


def test_group_delete_confirm_defaults_to_no():
    source = inspect.getsource(TunnelManagerUI._delete_group).replace("\r\n", "\n")
    assert "QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,\n            QMessageBox.StandardButton.No," in source


def test_tray_single_click_opens_window(monkeypatch):
    opened = []
    window = SimpleNamespace(bring_to_front=lambda: opened.append(True))
    controller = TrayController(window, False)
    monkeypatch.setattr("src.ui.controllers.tray_controller.sys.platform", "win32")

    controller._on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    controller._on_tray_activated(QSystemTrayIcon.ActivationReason.Context)

    assert opened == [True]


def test_update_balloon_click_opens_about_tab_once():
    calls = []
    window = SimpleNamespace(
        tray_icon=_Recorder(),
        _pending_update_version=None,
        bring_to_front=lambda: calls.append("front"),
        open_settings_dialog=lambda about=False: calls.append(("settings", about)),
    )

    TunnelManagerUI._on_startup_update_available(window, "9.9.9", "https://example.invalid")
    TunnelManagerUI._on_tray_message_clicked(window)
    TunnelManagerUI._on_tray_message_clicked(window)

    assert calls == ["front", ("settings", True), "front"]


def test_tray_message_click_is_wired():
    source = inspect.getsource(TrayController.init_tray)
    assert "messageClicked.connect(window._on_tray_message_clicked)" in source
