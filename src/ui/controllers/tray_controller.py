"""System tray behavior for the main window."""

import os

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from src.core.resources import app_icon_path
from src.core.i18n import tr


class TrayController:
    """Owns system tray setup and schedule tray actions."""

    def __init__(self, window, schedule_feature_enabled: bool):
        self._window = window
        self._schedule_feature_enabled = schedule_feature_enabled

    def init_tray(self):
        """시스템 트레이 아이콘 설정"""
        window = self._window
        window.tray_icon = QSystemTrayIcon(window)
        icon_path = str(app_icon_path())
        if os.path.exists(icon_path):
            window.tray_icon.setIcon(QIcon(icon_path))

        tray_menu = QMenu()
        show_action = QAction("열기", window)
        show_action.triggered.connect(window.show)

        if self._schedule_feature_enabled:
            window.schedule_menu = tray_menu.addMenu("")
            window.schedule_manage_action = QAction(window)
            window.schedule_manage_action.triggered.connect(window._open_schedule_dialog)
            window.schedule_menu.addAction(window.schedule_manage_action)

            window.schedule_menu.addSeparator()

            window._schedule_run_menu = window.schedule_menu.addMenu("")
            self._update_schedule_run_menu()

            tray_menu.addSeparator()

        window.show_action = show_action
        window.quit_action = QAction(window)
        window.quit_action.triggered.connect(window.close_app)

        tray_menu.addAction(window.show_action)
        tray_menu.addAction(window.quit_action)
        window._apply_language()

        window.tray_icon.setContextMenu(tray_menu)
        window.tray_icon.activated.connect(window._on_tray_activated)
        window.tray_icon.show()

    def _on_tray_activated(self, reason):
        """트레이 아이콘 클릭 시"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._window.bring_to_front()

    def _update_schedule_run_menu(self):
        """즉시 실행 메뉴 업데이트"""
        window = self._window
        if not hasattr(window, "_schedule_run_menu") or not window.scheduler:
            return

        window._schedule_run_menu.clear()

        schedules = window.scheduler.get_schedules()
        if not schedules:
            no_schedule_action = QAction("(스케줄 없음)", window)
            no_schedule_action.setEnabled(False)
            window._schedule_run_menu.addAction(no_schedule_action)
            return

        for schedule in schedules:
            action = QAction(schedule.name, window)
            action.setData(schedule.id)
            action.triggered.connect(
                lambda checked, sid=schedule.id: window._run_schedule_now(sid)
            )
            window._schedule_run_menu.addAction(action)

    def _run_schedule_now(self, schedule_id: str):
        """스케줄 즉시 실행"""
        window = self._window
        if not window.scheduler:
            return

        schedule = window.scheduler.get_schedule(schedule_id)
        if not schedule:
            return

        success, message = window.scheduler.run_now(schedule_id)
        self._notify_backup_result(
            schedule.name,
            success,
            message,
            success_title="백업 완료",
            failure_title="백업 실패",
        )

    def _notify_backup_result(
        self,
        schedule_name: str,
        success: bool,
        message: str,
        *,
        success_title: str,
        failure_title: str,
    ):
        if success:
            self._window.tray_icon.showMessage(
                success_title,
                f"{schedule_name} 백업이 완료되었습니다.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        else:
            self._window.tray_icon.showMessage(
                failure_title,
                f"{schedule_name}: {message}",
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
