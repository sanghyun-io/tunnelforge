"""메인 UI 윈도우"""
import sys
import os
from datetime import datetime, timezone
from typing import Optional
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QMessageBox, QSystemTrayIcon,
                             QMenu, QApplication, QDialog)
from PyQt6.QtCore import pyqtSlot, QTimer, Qt, QMetaObject, Q_ARG
from PyQt6.QtGui import QAction, QIcon

from src.ui.styles import ButtonStyles, LabelStyles, get_full_app_style
from src.ui.theme_manager import ThemeManager
from src.ui.trust_prompts import TrustPrompter
from src.ui.dialogs.preselected_connect_dialog import start_tunnel_with_progress
from src.core.connection_trust import insecure_connection_warning
from src.core.db_core_service import normalize_db_engine
from src.ui.themes import ThemeColors
from src.ui.widgets.tunnel_tree import TunnelTreeWidget
from src.ui.dialogs.group_dialog import create_group_dialog, edit_group_dialog
from src.ui.workers.connection_test_worker import ConnectionTestWorker, TestType
from src.ui.workers.db_connection_worker import has_active_connection_workers
from src.ui.dialogs.progress_dialog import TestProgressDialog
from src.ui.controllers import TrayController, TunnelActionsController, WizardLauncher
from src.ui.dialogs.migration_dialogs import has_active_detached_migration_workers
from src.ui.dialogs.oneclick_migration_dialog import (
    has_active_detached_oneclick_workers,
)
from src.ui.dialogs.explain_plan_dialog import has_active_explain_workers
from src.core.logger import get_logger
from src.core.platform_integration import restore_window_to_front
from src.core.resources import app_icon_path
from src.core.i18n import tr
from src.core.error_report_consent import ConsentPolicy
from src.ui.dialogs.error_reporting_consent_dialog import ErrorReportingConsentDialog

logger = get_logger('main_window')
# 예약 백업만 노출한다. 예약 SQL 실행은 무인 쓰기 위험(운영 읽기 전용 정책과 충돌) 때문에 지원하지 않는다.
SCHEDULE_FEATURE_ENABLED = True
ERROR_REPORTING_INITIAL_DELAY_MS = 500
ERROR_REPORTING_RETRY_DELAY_MS = 500


def _utc_now():
    return datetime.now(timezone.utc)


from src.ui.dialogs.settings import CloseConfirmDialog, SettingsDialog
from src.ui.dialogs.settings_update_helpers import UpdateCheckerThread
from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog
from src.ui.dialogs.diff_dialog import SchemaDiffDialog
from src.core.tunnel_monitor import TunnelMonitor, TunnelState
from src.core.mysql_login_path import MysqlLoginPathManager


class TunnelManagerUI(QMainWindow):
    def __init__(self, config_manager, tunnel_engine, start_background=True):
        logger.info("UI 초기화 시작...")
        super().__init__()
        self.config_mgr = config_manager
        self.engine = tunnel_engine
        self._start_background = start_background

        # SSH 호스트 키 확인 / 개인키 비밀번호 입력 대화상자를 GUI 스레드에서 띄우는 다리 (TF-STATUS-110)
        self._trust_prompter = TrustPrompter(self)
        self._trust_prompter.install(self.engine)

        # 설정 로드
        self.config_data = self.config_mgr.load_config()
        self.tunnels = self.config_data.get('tunnels', [])

        self._update_checker_thread = None
        self._pending_update_version = None  # 트레이 업데이트 알림 클릭 시 정보 탭을 열기 위함
        self._error_reporting_consent_policy = ConsentPolicy(self.config_mgr)
        self._init_error_reporting_prompt_lifecycle()

        # MySQL 로그인 경로 매니저 초기화
        self._login_path_mgr = MysqlLoginPathManager()

        self._wizard_launcher = WizardLauncher(self)
        self._tray_controller = TrayController(self, SCHEDULE_FEATURE_ENABLED)
        self._tunnel_actions_controller = TunnelActionsController(self)

        # ThemeManager 초기화
        self._init_theme_manager()

        # Scheduled backup (backup tasks only; scheduled SQL execution stays unsupported).
        self.scheduler = None
        if SCHEDULE_FEATURE_ENABLED:
            from src.core.scheduler import BackupScheduler

            self.scheduler = BackupScheduler(config_manager, tunnel_engine)
            self.scheduler.add_callback(self._on_backup_complete)
            if self._start_background:
                self.scheduler.start()

        # TunnelMonitor 초기화
        self._last_tunnel_health = {}  # tunnel_id -> None/'reconnecting'/'error' (실패 알림 중복 방지)
        self.tunnel_monitor = TunnelMonitor(tunnel_engine, config_manager)
        self.tunnel_monitor.add_callback(self._on_tunnel_status_changed)
        if self._start_background:
            self.tunnel_monitor.start_monitoring()

        self.init_ui()
        self.init_tray()
        if self._start_background:
            self._check_update_on_startup()
            # 이전 세션 크래시 등으로 남은 로그인 경로 정리 후 자동 연결
            self._login_path_mgr.cleanup_all_tf_paths()
            self._auto_connect_tunnels()
        logger.info("UI 초기화 완료")

    def _init_theme_manager(self):
        """ThemeManager 초기화 및 테마 적용"""
        theme_mgr = ThemeManager.instance()
        theme_mgr.set_config_manager(self.config_mgr)
        theme_mgr.theme_changed.connect(self._on_theme_changed)
        theme_mgr.load_saved_theme()

    def _on_theme_changed(self, colors: ThemeColors):
        """테마 변경 시 UI 업데이트"""
        # 앱 전체 스타일 적용
        app = QApplication.instance()
        if app:
            app.setStyleSheet(get_full_app_style(colors))
        logger.info(f"테마 변경됨: {ThemeManager.instance().current_theme_type.value}")

    def init_ui(self):
        self.setWindowTitle("TunnelForge")
        self.setGeometry(100, 100, 950, 600)

        # 창 아이콘 설정
        icon_path = str(app_icon_path())
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # 메인 위젯 설정
        central_widget = QWidget()
        central_widget.setAutoFillBackground(True)
        central_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # --- 상단 헤더 ---
        header_layout = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setStyleSheet(LabelStyles.TITLE)

        # [그룹 추가] 버튼
        self.btn_add_group = QPushButton()
        self.btn_add_group.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_add_group.clicked.connect(self.add_group_dialog)

        # [연결 추가] 버튼 - Primary 스타일 (중앙화)
        self.btn_add_tunnel = QPushButton()
        self.btn_add_tunnel.setStyleSheet(ButtonStyles.PRIMARY)
        self.btn_add_tunnel.clicked.connect(self.add_tunnel_dialog)

        # [스키마 비교] 버튼 - Secondary 스타일
        self.btn_schema_diff = QPushButton()
        self.btn_schema_diff.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_schema_diff.clicked.connect(self._open_schema_diff_dialog)

        # [마이그레이션 분석] 버튼 - Secondary 스타일
        self.btn_migration = QPushButton()
        self.btn_migration.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_migration.clicked.connect(self.open_migration_analyzer)

        # [DB 전환] 버튼 - Secondary 스타일
        self.btn_db_transition = QPushButton()
        self.btn_db_transition.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_db_transition.clicked.connect(self.open_cross_engine_migration)

        # [스케줄] 버튼 - Secondary 스타일
        self.btn_schedule = QPushButton()
        self.btn_schedule.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_schedule.clicked.connect(self._open_schedule_dialog)
        self.btn_schedule.setVisible(SCHEDULE_FEATURE_ENABLED)

        # [설정] 버튼 - Secondary 스타일 (중앙화)
        self.btn_settings = QPushButton()
        self.btn_settings.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_settings.clicked.connect(lambda: self.open_settings_dialog())

        header_layout.addWidget(self.title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_add_group)
        header_layout.addWidget(self.btn_add_tunnel)
        header_layout.addWidget(self.btn_schema_diff)
        header_layout.addWidget(self.btn_migration)
        header_layout.addWidget(self.btn_db_transition)
        header_layout.addWidget(self.btn_schedule)
        # [작업 목록] Export/Import/전환/이관 실행 기록
        self.btn_job_list = QPushButton()
        self.btn_job_list.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_job_list.clicked.connect(self.open_job_list_dialog)
        header_layout.addWidget(self.btn_job_list)
        # [복구된 SQL] 삭제된 프로필에서 남은 작업 공간이 있을 때만 보인다 (읽기 전용 목록)
        self.btn_recovered_sql = QPushButton()
        self.btn_recovered_sql.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_recovered_sql.clicked.connect(self.open_recovered_sql_dialog)
        self.btn_recovered_sql.setVisible(False)
        header_layout.addWidget(self.btn_recovered_sql)
        header_layout.addWidget(self.btn_settings)
        layout.addLayout(header_layout)

        # --- 트리 위젯 설정 (터널 그룹핑 지원) ---
        self.tunnel_tree = TunnelTreeWidget(self)
        self.tunnel_tree.viewport().setAttribute(Qt.WidgetAttribute.WA_StaticContents, False)

        # 기본 열 비율 설정
        self._default_column_ratios = [0.05, 0.20, 0.08, 0.25, 0.12, 0.10, 0.20]
        self._column_ratios = self._load_column_ratios()
        self._resizing_columns = False

        # 시그널 연결
        self._connect_tree_signals()

        layout.addWidget(self.tunnel_tree)

        # 호환성을 위해 table 변수 유지
        self.table = self.tunnel_tree

        self._apply_language()

        self.refresh_table()
        self._refresh_recovered_sql_button()
        from src.core.job_history import sweep_interrupted
        sweep_interrupted()  # 이전 실행에서 작업 도중 종료된 기록을 '중단됨'으로 표시

    def _icon_text(self, icon: str, key: str) -> str:
        return f"{icon} {tr(key)}" if icon else tr(key)

    def _apply_language(self):
        self.title_label.setText(self._icon_text("📡", "main.title"))
        self.btn_add_group.setText(self._icon_text("📁", "main.add_group"))
        self.btn_add_tunnel.setText(self._icon_text("➕", "main.add_tunnel"))
        self.btn_schema_diff.setText(self._icon_text("🔀", "main.schema_diff"))
        self.btn_migration.setText(self._icon_text("🔄", "main.migration"))
        self.btn_db_transition.setText(tr("main.db_transition"))
        self.btn_schedule.setText(self._icon_text("📅", "main.schedule"))
        self.btn_settings.setText(self._icon_text("⚙️", "main.settings"))
        self.btn_job_list.setText("📋 작업 목록")
        self.btn_recovered_sql.setText("📝 복구된 SQL")
        self.statusBar().showMessage(tr("app.ready"))
        if hasattr(self, "tunnel_tree"):
            self.tunnel_tree.apply_language()
        if hasattr(self, "show_action"):
            self.show_action.setText(tr("main.open"))
        if hasattr(self, "quit_action"):
            self.quit_action.setText(tr("main.quit"))
        if hasattr(self, "schedule_menu"):
            self.schedule_menu.setTitle(self._icon_text("📅", "main.schedule_backup"))
        if hasattr(self, "schedule_manage_action"):
            self.schedule_manage_action.setText(tr("main.schedule_manage"))
        if hasattr(self, "_schedule_run_menu"):
            self._schedule_run_menu.setTitle(tr("main.run_now"))

    def init_tray(self):
        """시스템 트레이 아이콘 설정"""
        self._tray_controller.init_tray()

    def bring_to_front(self):
        """숨김/최소화 상태의 메인 창을 전면으로 복원합니다."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()

        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()
        self._force_windows_foreground()
        self.refresh_table()

    def _force_windows_foreground(self):
        if sys.platform != 'win32':
            return

        if not restore_window_to_front(int(self.winId())):
            logger.debug("창 전면 이동 실패")

    def _on_tray_activated(self, reason):
        """트레이 아이콘 클릭 시"""
        self._tray_controller._on_tray_activated(reason)

    def _on_tray_message_clicked(self):
        """트레이 알림 클릭 시 창을 열고, 업데이트 알림이었다면 설정 > 정보 탭을 연다."""
        self.bring_to_front()
        if self._pending_update_version:
            self._pending_update_version = None
            self.open_settings_dialog(about=True)

    def _connect_tree_signals(self):
        """트리 위젯 시그널 연결"""
        self.tunnel_tree.tunnel_edit_requested.connect(self.edit_tunnel_dialog)
        self.tunnel_tree.tunnel_delete_requested.connect(self.delete_tunnel)
        self.tunnel_tree.tunnel_db_connect.connect(self._on_tree_db_connect)
        self.tunnel_tree.tunnel_sql_editor.connect(self._on_tree_sql_editor)
        self.tunnel_tree.tunnel_export.connect(self._on_tree_export)
        self.tunnel_tree.tunnel_import.connect(self._on_tree_import)
        self.tunnel_tree.tunnel_orphan_check.connect(self._on_tree_orphan_check)
        self.tunnel_tree.tunnel_test.connect(self._on_tree_test_connection)
        self.tunnel_tree.tunnel_duplicate.connect(self.duplicate_tunnel)
        self.tunnel_tree.group_connect_all.connect(self._connect_all_in_group)
        self.tunnel_tree.group_disconnect_all.connect(self._disconnect_all_in_group)
        self.tunnel_tree.group_edit_requested.connect(self._edit_group_dialog)
        self.tunnel_tree.group_delete_requested.connect(self._delete_group)
        self.tunnel_tree.tunnel_moved_to_group.connect(self._on_tunnel_moved)
        self.tunnel_tree.tunnel_move_to_new_group.connect(self._move_tunnel_to_new_group)
        self.tunnel_tree.group_collapsed_changed.connect(
            lambda gid, collapsed: self.config_mgr.save_group_collapsed_state(gid, collapsed)
        )
        self.tunnel_tree.header().sectionResized.connect(self._on_column_resized)

    def _build_power_button(self, tunnel: dict, is_active: bool) -> QPushButton:
        """터널 활성 상태에 맞는 전원 버튼을 생성한다."""
        btn_power = QPushButton(tr("common.stop") if is_active else tr("common.start"))
        if is_active:
            btn_power.setStyleSheet(ButtonStyles.DANGER)
            btn_power.clicked.connect(lambda checked, t=tunnel: self.stop_tunnel(t))
        else:
            btn_power.setStyleSheet(ButtonStyles.SUCCESS)
            btn_power.clicked.connect(lambda checked, t=tunnel: self.start_tunnel(t))
        return btn_power

    def _build_manage_buttons(self, tunnel: dict) -> QWidget:
        container = QWidget()
        h_box = QHBoxLayout(container)
        h_box.setContentsMargins(2, 2, 2, 2)
        h_box.setSpacing(3)

        btn_edit = QPushButton(tr("common.edit"))
        btn_edit.setStyleSheet(ButtonStyles.EDIT)
        btn_edit.clicked.connect(lambda checked, t=tunnel: self.edit_tunnel_dialog(t))
        h_box.addWidget(btn_edit)
        # 삭제는 오클릭 방지를 위해 우클릭 메뉴에만 둔다

        return container

    def refresh_table(self):
        """설정 데이터와 현재 터널 상태를 기반으로 트리를 갱신합니다."""
        # 그룹 및 순서 데이터 로드
        groups = self.config_mgr.get_groups()
        ungrouped_order = self.config_data.get('ungrouped_order', [])

        # 트리 위젯에 데이터 로드
        self.tunnel_tree.load_data(self.tunnels, groups, ungrouped_order)

        # 각 터널의 상태 업데이트 및 버튼 설정
        for tunnel in self.tunnels:
            tid = tunnel.get('id')
            if not tid:
                continue

            is_active = self.engine.is_running(tid)

            # 상태 업데이트
            self._apply_tunnel_status(tid, is_active)

            # 전원 버튼 생성
            self.tunnel_tree.set_power_button(tid, self._build_power_button(tunnel, is_active))

            # 관리 버튼 그룹 생성
            self.tunnel_tree.set_tunnel_buttons(tid, self._build_manage_buttons(tunnel))

        self._schedule_repaint()

    # --- 트리 위젯 시그널 핸들러 ---
    def _on_tree_db_connect(self, tunnel):
        """트리에서 DB 연결 요청 - DB 연결 다이얼로그 열기"""
        if self._require_db_credentials(tunnel) is None:
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        if not self._ensure_tunnel_running(tunnel, prompt=True):
            return

        # DB 연결 다이얼로그 열기
        from src.ui.dialogs.db_dialogs import DBConnectionDialog
        dialog = DBConnectionDialog(self, tunnel_engine=self.engine, config_manager=self.config_mgr)
        # 터널 모드 선택 및 해당 터널 선택
        dialog.radio_tunnel.setChecked(True)
        dialog.on_mode_changed()
        # 해당 터널 찾아서 선택
        for i in range(dialog.combo_tunnel.count()):
            data = dialog.combo_tunnel.itemData(i)
            if data and data.get('tunnel_id') == tunnel['id']:
                dialog.combo_tunnel.setCurrentIndex(i)
                break
        dialog.exec()

    def _on_tree_sql_editor(self, tunnel):
        """트리에서 SQL 에디터 요청"""
        self.open_sql_editor(tunnel)

    def _on_tree_export(self, tunnel):
        """트리에서 Export 요청"""
        self._context_rust_core_export(tunnel)

    def _on_tree_import(self, tunnel):
        """트리에서 Import 요청"""
        self._context_rust_core_import(tunnel)

    def _on_tree_orphan_check(self, tunnel):
        """트리에서 고아 레코드 분석 요청"""
        self._context_orphan_check(tunnel)

    def _on_tree_test_connection(self, tunnel):
        """트리에서 연결 테스트 요청"""
        is_direct = tunnel.get('connection_mode') == 'direct'
        tunnel_name = tunnel.get('name', '알 수 없음')

        # 직접 연결 모드인 경우 DB 연결 테스트
        if is_direct:
            self._test_direct_connection(tunnel)
            return

        # SSH 터널 모드: 터널 연결 테스트
        if self.engine.is_running(tunnel['id']):
            # 이미 실행 중이면 성공
            QMessageBox.information(
                self, "연결 테스트",
                f"✅ '{tunnel_name}' 터널이 이미 연결되어 있습니다."
            )
            return

        # 임시 터널로 연결 테스트 (실제 터널은 시작하지 않음, 백그라운드 스레드)
        self._run_connection_test(tunnel, TestType.TUNNEL_ONLY, f"터널 테스트 - {tunnel_name}")

    def _test_direct_connection(self, tunnel):
        """직접 연결 모드 테스트"""
        tunnel_name = tunnel.get('name', '알 수 없음')

        if self._require_db_credentials(tunnel) is None:
            return

        engine = tunnel.get('db_engine') or 'mysql'
        if engine not in ('mysql', 'postgresql'):
            QMessageBox.warning(
                self, "연결 테스트",
                f"❌ '{tunnel_name}' DB Engine이 설정되어 있지 않습니다.\n\n연결 설정에서 MySQL 또는 PostgreSQL을 먼저 선택해주세요."
            )
            self.statusBar().showMessage(f"연결 테스트 중단: {tunnel_name}")
            return

        self._run_connection_test(tunnel, TestType.DB_ONLY, f"DB 인증 테스트 - {tunnel_name}")

    def _run_connection_test(self, tunnel: dict, test_type: TestType, title: str):
        """연결 테스트를 백그라운드 스레드에서 실행하고 진행 다이얼로그로 결과를 보여준다."""
        tunnel_name = tunnel.get("name", "알 수 없음")
        self.statusBar().showMessage(f"연결 테스트 중: {tunnel_name}...")

        dialog = TestProgressDialog(self, title)
        worker = ConnectionTestWorker(test_type, tunnel, self.engine, self.config_mgr, self)
        if not hasattr(self, "_connection_test_workers"):
            self._connection_test_workers = set()
        workers = self._connection_test_workers
        workers.add(worker)
        worker.progress.connect(dialog.update_progress)
        worker.test_finished.connect(
            lambda success, message: self._on_connection_test_finished(dialog, tunnel_name, success, message)
        )
        worker.finished.connect(lambda: workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        dialog.exec()

    def _on_connection_test_finished(self, dialog, tunnel_name: str, success: bool, message: str):
        """연결 테스트 완료 시 진행 다이얼로그에 결과를 표시한다."""
        dialog.show_result(success, message)
        self.statusBar().showMessage(
            f"연결 성공: {tunnel_name}" if success else f"연결 실패: {tunnel_name}"
        )

    def _connect_all_in_group(self, group_id: str):
        """그룹 내 모든 터널 연결"""
        groups = self.config_mgr.get_groups()
        for group in groups:
            if group['id'] == group_id:
                for tunnel_id in group.get('tunnel_ids', []):
                    tunnel = next((t for t in self.tunnels if t['id'] == tunnel_id), None)
                    if tunnel and not self.engine.is_running(tunnel_id):
                        self.start_tunnel(tunnel)
                break

    def _disconnect_all_in_group(self, group_id: str):
        """그룹 내 모든 터널 해제"""
        groups = self.config_mgr.get_groups()
        for group in groups:
            if group['id'] == group_id:
                for tunnel_id in group.get('tunnel_ids', []):
                    tunnel = next((t for t in self.tunnels if t['id'] == tunnel_id), None)
                    if tunnel and self.engine.is_running(tunnel_id):
                        self.stop_tunnel(tunnel)
                break

    def _on_tunnel_moved(self, tunnel_id: str, group_id: str):
        """터널이 그룹으로 이동됨"""
        target_group = group_id if group_id else None
        success, msg = self.config_mgr.move_tunnel_to_group(tunnel_id, target_group)
        if success:
            self._reload_and_refresh()
        else:
            logger.warning(f"터널 이동 실패: {msg}")

    def _move_tunnel_to_new_group(self, tunnel_id: str):
        """새 그룹을 만들고 터널을 그 그룹으로 이동"""
        accepted, result = create_group_dialog(self)
        if not (accepted and result):
            return
        success, msg, group_id = self.config_mgr.add_group(result['name'], result['color'])
        if not success:
            QMessageBox.warning(self, "그룹 생성 실패", msg)
            return
        self._on_tunnel_moved(tunnel_id, group_id)

    # --- 그룹 관리 ---
    def add_group_dialog(self):
        """그룹 추가 다이얼로그"""
        accepted, result = create_group_dialog(self)
        if accepted and result:
            success, msg, group_id = self.config_mgr.add_group(
                result['name'],
                result['color']
            )
            if success:
                self.statusBar().showMessage(f"✅ {msg}")
                self._reload_and_refresh()
            else:
                QMessageBox.warning(self, "그룹 생성 실패", msg)

    def _edit_group_dialog(self, group_id: str):
        """그룹 수정 다이얼로그"""
        groups = self.config_mgr.get_groups()
        group_data = next((g for g in groups if g['id'] == group_id), None)
        if not group_data:
            return

        accepted, result = edit_group_dialog(self, group_data)
        if accepted and result:
            success, msg = self.config_mgr.update_group(group_id, result)
            if success:
                self.statusBar().showMessage(f"✅ {msg}")
                self._reload_and_refresh()
            else:
                QMessageBox.warning(self, "그룹 수정 실패", msg)

    def _delete_group(self, group_id: str):
        """그룹 삭제"""
        groups = self.config_mgr.get_groups()
        group = next((g for g in groups if g['id'] == group_id), None)
        if not group:
            return

        reply = QMessageBox.question(
            self, "그룹 삭제",
            f"'{group['name']}' 그룹을 삭제하시겠습니까?\n\n"
            f"그룹에 속한 터널은 '그룹 없음'으로 이동됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            success, msg = self.config_mgr.delete_group(group_id)
            if success:
                self.statusBar().showMessage(f"✅ {msg}")
                self._reload_and_refresh()
            else:
                QMessageBox.warning(self, "그룹 삭제 실패", msg)

    # --- 기능 로직 ---
    def add_tunnel_dialog(self):
        """연결 추가 팝업"""
        self._tunnel_actions_controller.add_tunnel_dialog()

    def edit_tunnel_dialog(self, tunnel):
        """연결 수정 팝업"""
        self._tunnel_actions_controller.edit_tunnel_dialog(tunnel)

    def duplicate_tunnel(self, tunnel):
        """연결 설정 복사하여 새로 만들기"""
        self._tunnel_actions_controller.duplicate_tunnel(tunnel)

    def delete_tunnel(self, tunnel):
        """연결 삭제"""
        self._tunnel_actions_controller.delete_tunnel(tunnel)

    def _process_credentials(self, tunnel_data: dict) -> dict:
        """비밀번호 암호화 처리"""
        return self._tunnel_actions_controller._process_credentials(tunnel_data)

    def save_and_refresh(self):
        """변경사항을 JSON 파일에 저장하고 테이블 새로고침 (기존 설정 보존)"""
        self._tunnel_actions_controller.save_and_refresh()

    def open_settings_dialog(self, about: bool = False):
        """설정 다이얼로그 열기 (about=True면 정보 탭에서 시작)"""
        dialog = SettingsDialog(self, config_manager=self.config_mgr)
        if about:
            dialog.tabs.setCurrentIndex(dialog.tabs.count() - 1)  # 정보 탭은 마지막 탭
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_language()
            self.refresh_table()
        self._refresh_recovered_sql_button()

    def open_job_list_dialog(self):
        """작업 목록 (TF-STATUS-132)"""
        from src.core.job_history import make_history
        from src.ui.dialogs.job_list_dialog import JobListDialog
        JobListDialog(make_history(), reopen=self.reopen_job_dialog, parent=self).exec()

    def reopen_job_dialog(self, record):
        """작업 목록에서 원래 대화상자를 연다. Export 만 이전 설정으로 화면을 채우고, 나머지는 설정 없이 연다
        (실행 때마다 사전 검증을 새로 거치며 Import/전환/이관의 부분 재시도는 하지 않는다)."""
        from src.core import job_history as jh
        tunnel = next((t for t in self.config_mgr.load_config().get('tunnels', [])
                       if record.profile_id and t.get('id') == record.profile_id), None)
        if record.kind in (jh.KIND_EXPORT_FULL, jh.KIND_EXPORT_TABLES):
            self._wizard_launcher.open_rust_dump_export(tunnel, record.rerun)
        elif record.kind in (jh.KIND_IMPORT, jh.KIND_PROMOTE):
            self._wizard_launcher.open_rust_dump_import(tunnel)
        elif record.kind in (jh.KIND_SCHEDULED_BACKUP, jh.KIND_RESTORE_REHEARSAL):
            self._open_schedule_dialog()
        else:
            self._wizard_launcher.open_cross_engine_migration()

    def _known_profile_ids(self):
        return [t.get('id') for t in self.config_mgr.load_config().get('tunnels', []) if t.get('id')]

    def _refresh_recovered_sql_button(self):
        """고아 SQL 작업 공간(삭제된 프로필)이 있을 때만 '복구된 SQL' 버튼을 보인다."""
        try:
            from src.core.workspace_store import WorkspaceStore
            from src.ui.dialogs.recovered_sql_dialog import find_orphans
            self.btn_recovered_sql.setVisible(bool(find_orphans(WorkspaceStore(), self._known_profile_ids())))
        except Exception:
            self.btn_recovered_sql.setVisible(False)

    def open_recovered_sql_dialog(self):
        from src.core.workspace_store import WorkspaceStore
        from src.ui.dialogs.recovered_sql_dialog import RecoveredSqlDialog
        RecoveredSqlDialog(WorkspaceStore(), self._known_profile_ids(), self).exec()
        self._refresh_recovered_sql_button()

    def open_rust_dump_export(self):
        """Rust DB Core Export 마법사 열기 (병렬 처리)"""
        self._wizard_launcher.open_rust_dump_export()

    def open_rust_dump_import(self):
        """Rust DB Core Import 마법사 열기"""
        self._wizard_launcher.open_rust_dump_import()

    def open_migration_analyzer(self):
        """마이그레이션 분석기 열기"""
        self._wizard_launcher.open_migration_analyzer()

    def open_cross_engine_migration(self):
        """DB 전환 마법사 열기"""
        self._wizard_launcher.open_cross_engine_migration()

    # --- 기존 터널링 로직 ---
    def _ensure_tunnel_running(self, tunnel: dict, *, prompt: bool = False) -> bool:
        """터널이 실행 중인지 확인하고, 필요하면 (선택적으로 확인 후) 시작한다.

        직접 연결 모드는 항상 True. 자동 시작이 필요한 모든 경로는 이 헬퍼를 거쳐야 한다.
        """
        if tunnel.get('connection_mode') == 'direct':
            return True
        tunnel_id = tunnel.get('id')
        if self.engine.is_running(tunnel_id):
            return True

        if prompt:
            reply = QMessageBox.question(
                self, "터널 연결",
                f"'{tunnel['name']}' 터널이 연결되어 있지 않습니다.\n터널을 시작하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

        return bool(self.start_tunnel(tunnel))

    def start_tunnel(self, tunnel_config):
        self.statusBar().showMessage(f"연결 시도 중: {tunnel_config['name']}...")
        # SSH 접속(호스트 키 확인/개인키 비밀번호 포함)은 백그라운드에서 수행한다 (TF-STATUS-120).
        outcome = start_tunnel_with_progress(self, self.engine, tunnel_config)
        if outcome is None:
            self.statusBar().showMessage(f"연결 취소: {tunnel_config['name']}")
            self.refresh_table()
            return False
        success, msg = outcome

        if success:
            tls_warning = insecure_connection_warning(tunnel_config)
            if tls_warning:
                # 검증 없는 연결은 연결할 때마다 다시 알린다 (자동 승격/무시 없음)
                self.statusBar().showMessage(f"연결 성공: {tunnel_config['name']} - ⚠ {tls_warning}")
                logger.warning(f"Unverified DB TLS connection: {tunnel_config['name']}")
            else:
                self.statusBar().showMessage(f"연결 성공: {tunnel_config['name']}")
            self._pending_update_version = None  # 업데이트 외 알림 클릭이 정보 탭을 열지 않도록
            self.tray_icon.showMessage("TunnelForge", f"{tunnel_config['name']} 연결되었습니다.", QSystemTrayIcon.MessageIcon.Information, 2000)
            self._register_login_path(tunnel_config)
        else:
            self.statusBar().showMessage(f"연결 실패: {msg}")
            QMessageBox.critical(self, "연결 오류", f"터널 연결에 실패했습니다.\n\n원인: {msg}")

        self.refresh_table()
        return success

    def stop_tunnel(self, tunnel_config):
        tid = tunnel_config['id']
        # engine stop 전에 실제 활성 포트 저장 (stop 후엔 engine에서 조회 불가)
        _, active_port = self.engine.get_connection_info(tid)
        self.engine.stop_tunnel(tid)
        self._remove_login_path(tunnel_config, active_port)
        self.statusBar().showMessage(f"연결 종료: {tunnel_config['name']}")
        self.refresh_table()

    def _register_login_path(self, tunnel_config):
        """터널 연결 후 mysql_config_editor에 로그인 경로 등록"""
        if not self._login_path_mgr.is_available():
            return

        tid = tunnel_config['id']
        user, password = self.config_mgr.get_tunnel_credentials(tid)
        if not user or not password:
            return

        host, port = self.engine.get_connection_info(tid)
        if not port:
            return
        if not host:
            host = '127.0.0.1'

        ok, result = self._login_path_mgr.register(port, host, user, password)
        if ok:
            logger.info(f"로그인 경로 등록 완료: {result}")
        else:
            logger.warning(f"로그인 경로 등록 실패: {result}")

    def _remove_login_path(self, tunnel_config, active_port=None):
        """터널 종료 후 mysql_config_editor에서 로그인 경로 제거.

        active_port: stop_tunnel() 전에 engine.get_connection_info()로 가져온 실제 포트.
                     전달되면 tunnel_config 기반 폴백보다 우선 사용.
        """
        if not self._login_path_mgr.is_available():
            return

        if active_port:
            port = active_port
        elif tunnel_config.get('connection_mode') == 'direct':
            # stop 후엔 engine에서 조회 불가 — tunnel_config에서 직접 읽음
            port = int(tunnel_config.get('remote_port', 0))
        else:
            port = int(tunnel_config.get('local_port', 0))

        if not port:
            return

        ok, result = self._login_path_mgr.remove(port)
        if not ok:
            logger.warning(f"로그인 경로 제거 실패: {result}")

    def _reload_and_refresh(self):
        """설정을 다시 불러와 트리를 갱신한다 (알림 없이 조용히 처리)."""
        self.config_data = self.config_mgr.load_config()
        self.tunnels = self.config_data.get('tunnels', [])
        self.refresh_table()

    def reload_config(self):
        self._reload_and_refresh()
        QMessageBox.information(self, "알림", "설정 파일을 다시 불러왔습니다.")

    def showEvent(self, event):
        """창 표시 시 초기 열 비율 적용"""
        super().showEvent(event)
        QTimer.singleShot(50, self._apply_column_ratios)
        self._schedule_repaint()
        self._schedule_error_reporting_prompt(ERROR_REPORTING_INITIAL_DELAY_MS)

    def _init_error_reporting_prompt_lifecycle(self):
        self._error_reporting_prompt_scheduled = False
        self._error_reporting_prompt_running = False
        self._error_reporting_prompt_shown = False
        self._error_reporting_prompt_shutdown = False
        self._error_reporting_prompt_timer = QTimer(self)
        self._error_reporting_prompt_timer.setSingleShot(True)
        self._error_reporting_prompt_timer.timeout.connect(
            self._maybe_show_error_reporting_consent
        )

    def _schedule_error_reporting_prompt(self, delay_ms):
        if (
            self._error_reporting_prompt_shutdown
            or self._error_reporting_prompt_running
            or self._error_reporting_prompt_shown
            or self._error_reporting_prompt_scheduled
        ):
            return
        try:
            if self._error_reporting_prompt_timer.isActive():
                self._error_reporting_prompt_scheduled = True
                return
            bounded_delay = max(1, min(int(delay_ms), 1000))
            self._error_reporting_prompt_scheduled = True
            self._error_reporting_prompt_timer.start(bounded_delay)
        except (RuntimeError, TypeError, ValueError):
            self._error_reporting_prompt_scheduled = False

    def prepare_for_shutdown(self):
        """Stop consent presentation before the application event loop exits."""
        self._stop_error_reporting_prompt_for_shutdown()

    def _stop_error_reporting_prompt_for_shutdown(self):
        if self._error_reporting_prompt_shutdown:
            return
        self._error_reporting_prompt_shutdown = True
        self._error_reporting_prompt_scheduled = False
        try:
            self._error_reporting_prompt_timer.stop()
        except (AttributeError, RuntimeError):
            pass

    def _has_active_database_operation(self):
        """Avoid prompting over a modal or a detached DB operation."""
        return (
            QApplication.activeModalWidget() is not None
            or has_active_detached_migration_workers()
            or has_active_detached_oneclick_workers()
            or has_active_explain_workers()
        )

    def _maybe_show_error_reporting_consent(self):
        """Claim and present the local-only consent dialog once it is safe."""
        self._error_reporting_prompt_scheduled = False
        try:
            if (
                self._error_reporting_prompt_shutdown
                or self._error_reporting_prompt_running
                or self._error_reporting_prompt_shown
            ):
                return
            if (
                not self.isVisible()
                or self.isMinimized()
            ):
                return
        except RuntimeError:
            return

        if self._has_active_database_operation():
            self._schedule_error_reporting_prompt(ERROR_REPORTING_RETRY_DELAY_MS)
            return

        self._error_reporting_prompt_running = True
        try:
            try:
                claim_id = self._error_reporting_consent_policy.claim_prompt(
                    _utc_now()
                )
            except Exception:
                logger.exception("오류 보고 동의 프롬프트 claim 실패")
                return
            if claim_id is None:
                return

            try:
                dialog = ErrorReportingConsentDialog(self)
            except Exception:
                logger.exception("오류 보고 동의 대화상자 생성 실패")
                self._release_error_reporting_prompt_claim(claim_id)
                return

            self._error_reporting_prompt_shown = True
            try:
                dialog.exec()
            except Exception:
                logger.exception("오류 보고 동의 대화상자 실행 실패")
                self._error_reporting_prompt_shown = False
                self._release_error_reporting_prompt_claim(claim_id)
                return

            if self._error_reporting_prompt_shutdown:
                self._error_reporting_prompt_shown = False
                self._release_error_reporting_prompt_claim(claim_id)
                return

            try:
                outcome, suppress = dialog.get_outcome()
            except Exception:
                logger.exception("오류 보고 동의 결과 조회 실패")
                self._error_reporting_prompt_shown = False
                self._release_error_reporting_prompt_claim(claim_id)
                return

            if self._error_reporting_prompt_shutdown:
                self._error_reporting_prompt_shown = False
                self._release_error_reporting_prompt_claim(claim_id)
                return

            try:
                self._error_reporting_consent_policy.record_outcome(
                    claim_id,
                    outcome,
                    _utc_now(),
                    suppress=suppress,
                )
            except Exception:
                logger.exception("오류 보고 동의 결과 저장 실패")
        finally:
            self._error_reporting_prompt_running = False

    def _release_error_reporting_prompt_claim(self, claim_id):
        try:
            self._error_reporting_consent_policy.release_prompt_claim(
                claim_id,
                _utc_now(),
            )
        except Exception:
            logger.exception("오류 보고 동의 프롬프트 claim 복원 실패")

    def resizeEvent(self, event):
        """창 크기 변경 시 열 비율 유지"""
        super().resizeEvent(event)
        self._apply_column_ratios()
        self._schedule_repaint()

    def closeEvent(self, event):
        """닫기 버튼 클릭 시"""
        # 열 비율 저장
        self._save_column_ratios()

        # 시스템 종료 시 활성 터널 목록이 유실되지 않도록 항상 먼저 저장
        active_ids = list(self.engine.active_tunnels.keys())
        self.config_mgr.save_active_tunnels(active_ids)

        close_action = self.config_mgr.get_app_setting('close_action', 'ask')

        if close_action == 'ask':
            # 다이얼로그 표시
            dialog = CloseConfirmDialog(self)
            if dialog.exec():
                action, remember = dialog.get_result()
                if remember:
                    self.config_mgr.set_app_setting('close_action', action)

                if action == 'minimize':
                    self.hide()
                    event.ignore()
                else:
                    if self.close_app() is False:
                        event.ignore()
            else:
                event.ignore()  # 취소
        elif close_action == 'minimize':
            self.hide()
            event.ignore()
        else:  # 'exit'
            if self.close_app() is False:
                event.ignore()

    def close_app(self):
        """진짜 종료"""
        if has_active_connection_workers() or any(
            worker.isRunning() for worker in getattr(self, "_connection_test_workers", ())
        ):
            QMessageBox.information(
                self, "연결 정리 중", "연결 요청을 정리하고 있습니다. 잠시 후 다시 종료해주세요."
            )
            return False
        self.prepare_for_shutdown()
        # 현재 활성화된 터널 ID 목록 저장 (다음 시작 시 자동 연결용)
        active_ids = list(self.engine.active_tunnels.keys())
        self.config_mgr.save_active_tunnels(active_ids)

        # 스케줄러 중지
        if self._start_background and hasattr(self, 'scheduler') and self.scheduler:
            self.scheduler.stop()

        # 터널 모니터 중지
        if self._start_background and hasattr(self, 'tunnel_monitor') and self.tunnel_monitor:
            self.tunnel_monitor.stop_monitoring()

        if self._start_background:
            self._login_path_mgr.cleanup_all_tf_paths()
        self.engine.stop_all()
        self.tray_icon.hide()
        # 모든 창 닫고 종료
        app = QApplication.instance()
        if app:
            app.quit()
        else:
            sys.exit(0)

    def dispose_for_smoke_check(self):
        """Dispose UI objects created by startup smoke checks without user-state writes."""
        self.prepare_for_shutdown()
        if hasattr(self, 'tray_icon') and self.tray_icon:
            self.tray_icon.hide()
        self.deleteLater()

    # =========================================================================
    # 스케줄 백업 관련 메서드
    # =========================================================================

    def _open_schedule_dialog(self):
        """스케줄 관리 다이얼로그 열기"""
        if not SCHEDULE_FEATURE_ENABLED or not self.scheduler:
            return

        # 터널 목록 준비
        tunnel_list = [(t['id'], t['name']) for t in self.tunnels]

        from src.ui.dialogs.schedule_dialog import ScheduleListDialog

        tunnel_engines = {t['id']: normalize_db_engine(t.get('db_engine'), t.get('remote_port')) for t in self.tunnels}
        tunnel_environments = {t['id']: t.get('environment') for t in self.tunnels}
        dialog = ScheduleListDialog(self, self.scheduler, tunnel_list, tunnel_engines, tunnel_environments)
        dialog.schedule_changed.connect(self._update_schedule_run_menu)
        dialog.exec()

    def _update_schedule_run_menu(self):
        """즉시 실행 메뉴 업데이트"""
        self._tray_controller._update_schedule_run_menu()

    def _run_schedule_now(self, schedule_id: str):
        """스케줄 즉시 실행"""
        self._tray_controller._run_schedule_now(schedule_id)

    def _on_backup_complete(self, schedule_name: str, success: bool, message: str):
        """백업 완료 콜백 (스케줄러 스레드에서 호출될 수 있어 UI 스레드로 마샬링)"""
        QMetaObject.invokeMethod(
            self,
            "_show_backup_complete_notification",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, schedule_name),
            Q_ARG(bool, success),
            Q_ARG(str, message),
        )

    @pyqtSlot(str, bool, str)
    def _show_backup_complete_notification(self, schedule_name: str, success: bool, message: str):
        """UI 스레드에서 백업 완료/실패 트레이 알림을 표시한다."""
        self._tray_controller._notify_backup_result(
            schedule_name,
            success,
            message,
            success_title="스케줄 백업 완료",
            failure_title="스케줄 백업 실패",
        )

    # =========================================================================
    # 터널 모니터링 관련 메서드
    # =========================================================================

    def _on_tunnel_status_changed(self, tunnel_id: str, status):
        """터널 상태 변경 콜백"""
        # UI 스레드에서 안전하게 갱신
        QMetaObject.invokeMethod(
            self, "_update_tunnel_status_ui",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, tunnel_id)
        )

    @pyqtSlot(str)
    def _update_tunnel_status_ui(self, tunnel_id: str):
        """UI에서 터널 상태 업데이트 (해당 터널 행만 갱신, 전체 refresh 없음)"""
        if not hasattr(self, 'tunnel_tree'):
            return

        tunnel = next((t for t in self.tunnels if t.get('id') == tunnel_id), None)
        if not tunnel:
            return

        is_active = self.engine.is_running(tunnel_id)
        health = self._apply_tunnel_status(tunnel_id, is_active)
        self.tunnel_tree.set_power_button(tunnel_id, self._build_power_button(tunnel, is_active))
        self._schedule_repaint()

        # 자동 재연결까지 실패해 ERROR로 끝난 순간 한 번만 알린다
        previous = self._last_tunnel_health.get(tunnel_id)
        self._last_tunnel_health[tunnel_id] = health
        if health == "error" and previous != "error":
            status = self.tunnel_monitor.get_status(tunnel_id)
            message = f"❌ '{tunnel.get('name', tunnel_id)}' 터널 연결 실패: {status.error_message or ''}"
            self.statusBar().showMessage(message, 10000)
            if hasattr(self, 'tray_icon'):
                self._pending_update_version = None  # 업데이트 외 알림 클릭이 정보 탭을 열지 않도록
                self.tray_icon.showMessage(
                    "터널 연결 실패", message, QSystemTrayIcon.MessageIcon.Warning, 5000
                )

    def _apply_tunnel_status(self, tunnel_id: str, is_active: bool):
        """엔진 실행 여부 + 모니터 상태(재연결 중/실패)를 행 상태 아이콘에 반영한다."""
        health, tooltip = None, ""
        monitor = getattr(self, 'tunnel_monitor', None)
        if not is_active and monitor:
            status = monitor.get_status(tunnel_id)
            if status.state == TunnelState.RECONNECTING:
                health = "reconnecting"
                tooltip = f"재연결 중 ({status.reconnect_count}/{monitor.get_max_reconnect_attempts()})"
            elif status.state == TunnelState.ERROR:
                health = "error"
                tooltip = f"연결 실패: {status.error_message or ''}"
        self.tunnel_tree.update_tunnel_status(tunnel_id, is_active, health, tooltip)
        return health

    # =========================================================================
    # 스키마 비교 관련 메서드
    # =========================================================================

    def _open_schema_diff_dialog(self):
        """스키마 비교 다이얼로그 열기"""
        dialog = SchemaDiffDialog(
            self,
            tunnels=self.tunnels,
            tunnel_engine=self.engine,
            config_manager=self.config_mgr
        )
        dialog.exec()

    def _load_column_ratios(self):
        """저장된 열 비율 로드 (없으면 기본값)"""
        ratios = self.config_mgr.get_app_setting('ui_column_ratios')
        if ratios and len(ratios) == len(self._default_column_ratios):
            return ratios
        return self._default_column_ratios.copy()

    def _save_column_ratios(self):
        """열 비율을 설정에 저장"""
        self.config_mgr.set_app_setting('ui_column_ratios', self._column_ratios)

    def _apply_column_ratios(self):
        """현재 비율을 테이블 너비에 적용"""
        if self._resizing_columns:
            return

        self._resizing_columns = True
        try:
            # 테이블 가용 너비 계산 (스크롤바, 테두리 등 제외)
            available_width = self.table.viewport().width()
            if available_width <= 0:
                return

            for i, ratio in enumerate(self._column_ratios):
                width = int(available_width * ratio)
                self.table.setColumnWidth(i, max(width, 30))  # 최소 30px
        finally:
            self._resizing_columns = False

    def _schedule_repaint(self):
        """resize/refresh 직후 남는 이전 프레임 잔상을 제거한다."""
        QTimer.singleShot(0, self._force_repaint)

    def _force_repaint(self):
        """메인 영역과 트리 viewport를 명시적으로 다시 그린다."""
        central = self.centralWidget()
        if central:
            central.update()
        if hasattr(self, 'tunnel_tree'):
            self.tunnel_tree.viewport().update()
            self.tunnel_tree.update()
        self.update()

    def _on_column_resized(self, index, old_size, new_size):
        """사용자가 열 너비를 조정했을 때 비율 업데이트"""
        if self._resizing_columns:
            return

        # 현재 전체 너비로 비율 재계산
        total_width = sum(self.table.columnWidth(i) for i in range(self.table.columnCount()))
        if total_width <= 0:
            return

        self._column_ratios = [
            self.table.columnWidth(i) / total_width
            for i in range(self.table.columnCount())
        ]

    def _check_update_on_startup(self):
        """앱 시작 시 업데이트 확인 (백그라운드)"""
        # 자동 업데이트 확인 설정 확인
        if not self.config_mgr.get_app_setting('auto_update_check', True):
            return

        # 백그라운드 스레드에서 확인
        self._update_checker_thread = UpdateCheckerThread(config_manager=self.config_mgr)
        self._update_checker_thread.update_checked.connect(self._on_startup_update_checked)
        self._update_checker_thread.start()

    def _on_startup_update_checked(self, needs_update, latest_version, download_url, _error):
        # 시작 시 확인 실패는 조용히 무시한다 (앱 실행에 영향 없음)
        if needs_update and latest_version and download_url:
            self._on_startup_update_available(latest_version, download_url)

    def _auto_connect_tunnels(self):
        """앱 시작 시 이전에 활성화되어 있던 터널 자동 연결"""
        # 자동 연결 설정 확인
        if not self.config_mgr.get_app_setting('auto_reconnect', True):
            return

        last_active = self.config_mgr.get_last_active_tunnels()
        if not last_active:
            return

        logger.info(f"이전 세션 터널 자동 연결 시도: {len(last_active)}개")

        connected = []
        skipped = []

        for tid in last_active:
            # 터널 설정 찾기
            tunnel = next((t for t in self.tunnels if t.get('id') == tid), None)
            if not tunnel:
                logger.warning(f"터널 설정을 찾을 수 없음: {tid}")
                continue

            # 연결 시도
            outcome = start_tunnel_with_progress(self, self.engine, tunnel, check_port=True)
            success, msg = outcome if outcome is not None else (False, "cancelled")
            if success:
                connected.append(tunnel['name'])
                logger.info(f"자동 연결 성공: {tunnel['name']}")
                self._register_login_path(tunnel)
            else:
                skipped.append((tunnel['name'], msg))
                logger.warning(f"자동 연결 스킵: {tunnel['name']} - {msg}")

        # 테이블 갱신
        self.refresh_table()

        # 결과 알림
        if connected or skipped:
            msg_parts = []
            if connected:
                msg_parts.append(f"✅ 연결됨: {', '.join(connected)}")
            if skipped:
                skip_msgs = [f"{name} ({reason})" for name, reason in skipped]
                msg_parts.append(f"⚠️ 스킵: {', '.join(skip_msgs)}")

            self.statusBar().showMessage(" | ".join(msg_parts), 5000)

            # 트레이 알림 (연결된 터널이 있는 경우만)
            if connected:
                self._pending_update_version = None  # 업데이트 외 알림 클릭이 정보 탭을 열지 않도록
                self.tray_icon.showMessage(
                    "자동 연결 완료",
                    f"{len(connected)}개 터널 연결됨" + (f", {len(skipped)}개 스킵" if skipped else ""),
                    QSystemTrayIcon.MessageIcon.Information,
                    3000
                )

    def _on_startup_update_available(self, latest_version: str, download_url: str):
        """시작 시 업데이트 발견 시 트레이 알림 (클릭하면 설정 > 정보 탭)"""
        self._pending_update_version = latest_version
        self.tray_icon.showMessage(
            "업데이트 사용 가능",
            f"새로운 버전 {latest_version}이 사용 가능합니다.\n이 알림을 클릭하면 다운로드 화면을 엽니다.",
            QSystemTrayIcon.MessageIcon.Information,
            5000  # 5초 동안 표시
        )

    def _require_db_credentials(self, tunnel) -> Optional[tuple[str, str]]:
        user, password = self.config_mgr.get_tunnel_credentials(tunnel['id'])
        if not user:
            QMessageBox.warning(
                self, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
            )
            return None
        return user, password

    def open_sql_editor(self, tunnel):
        """SQL 에디터 다이얼로그 열기"""
        if self._require_db_credentials(tunnel) is None:
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        if not self._ensure_tunnel_running(tunnel, prompt=True):
            return

        dialog = SQLEditorDialog(self, tunnel, self.config_mgr, self.engine)
        dialog.exec()

    def _context_rust_core_export(self, tunnel):
        """특정 터널용 Rust DB Core Export - 인증정보 자동 사용"""
        if self._require_db_credentials(tunnel) is None:
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        if not self._ensure_tunnel_running(tunnel, prompt=False):
            return

        self._wizard_launcher._launch_rust_dump_wizard("start_export", tunnel)

    def _context_rust_core_import(self, tunnel):
        """특정 터널용 Rust DB Core Import - 인증정보 자동 사용"""
        if self._require_db_credentials(tunnel) is None:
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        if not self._ensure_tunnel_running(tunnel, prompt=False):
            return

        self._wizard_launcher._launch_rust_dump_wizard("start_import", tunnel)

    def _context_orphan_check(self, tunnel):
        """특정 터널용 고아 레코드 분석 - 인증정보 자동 사용"""
        if self._require_db_credentials(tunnel) is None:
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        if not self._ensure_tunnel_running(tunnel, prompt=False):
            return

        self._wizard_launcher._launch_rust_dump_wizard("start_orphan_check", tunnel)
