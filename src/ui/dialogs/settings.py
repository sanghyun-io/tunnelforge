"""설정 관련 다이얼로그"""
import os
import subprocess
import sys
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QRadioButton, QCheckBox,
                             QButtonGroup, QGroupBox, QMessageBox, QTabWidget,
                             QWidget, QTextBrowser, QSizePolicy,
                             QComboBox, QListWidget, QListWidgetItem, QFileDialog,
                             QSpinBox, QProgressBar, QApplication)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtCore import QUrl
from src.version import __version__, __app_name__, GITHUB_OWNER, GITHUB_REPO
from src.core.update_downloader import format_size
from src.update_integrity import (
    IntegrityError,
    VerifiedLaunchFile,
    verify_file_integrity,
)
from src.core.i18n import SUPPORTED_LANGUAGES, current_language, set_language, tr, translate_text
from src.core.platform_integration import (
    StartupRegistrar,
    detached_process_kwargs,
    no_window_creation_flags,
    update_package_launch_strategy,
)
from src.ui.themes import ThemeType
from src.ui.theme_manager import ThemeManager
from src.ui.styles import ButtonStyles
from src.ui.dialogs.settings_close_confirm_dialog import CloseConfirmDialog
from src.ui.dialogs.settings_log_tab import LogViewerTab
from src.ui.dialogs.settings_update_helpers import (
    update_package_action_text,
    UpdateCheckerThread,
)


def _collect_active_tunnel_names(main_window) -> list[str]:
    if not main_window or not hasattr(main_window, 'engine'):
        return []

    active_tunnels = main_window.engine.active_tunnels
    if len(active_tunnels) == 0:
        return []

    tunnel_names = []
    for tid in active_tunnels:
        config = main_window.engine.tunnel_configs.get(tid, {})
        tunnel_names.append(config.get('name', tid))
    return tunnel_names


class SettingsDialog(QDialog):
    """앱 설정 다이얼로그"""
    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_mgr = config_manager
        self.setWindowTitle(tr("settings.title"))
        self.setMinimumSize(600, 420)
        self._update_checker_thread = None
        self._original_theme_type = ThemeManager.instance().current_theme_type
        self._theme_saved = False
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 탭 위젯 생성
        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_general_tab(), tr("settings.general"))
        self.tabs.addTab(self._create_log_tab(), tr("settings.logs"))
        self.tabs.addTab(self._create_about_tab(), tr("settings.about"))
        layout.addWidget(self.tabs)

        # 버튼
        button_layout = QHBoxLayout()
        self.btn_save = QPushButton(tr("common.save"))
        self.btn_save.setStyleSheet(ButtonStyles.PRIMARY)
        self.btn_save.clicked.connect(self.save_settings)

        self.btn_cancel = QPushButton(tr("common.cancel"))
        self.btn_cancel.setStyleSheet(ButtonStyles.SECONDARY)
        self.btn_cancel.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(self.btn_save)
        button_layout.addWidget(self.btn_cancel)
        layout.addLayout(button_layout)

    def _create_general_tab(self) -> QWidget:
        """일반 설정 탭 생성"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(self._build_language_group())
        layout.addWidget(self._build_close_behavior_group())
        layout.addWidget(self._build_theme_group())
        layout.addWidget(self._build_github_group())
        self._load_github_settings()
        layout.addWidget(self._build_backup_group())
        self._refresh_backup_list()
        layout.addWidget(self._build_reconnect_group())
        layout.addWidget(self._build_startup_group())
        layout.addStretch()

        return tab

    def _build_language_group(self) -> QGroupBox:
        language_group = QGroupBox(tr("settings.language"))
        language_layout = QVBoxLayout(language_group)
        language_row = QHBoxLayout()
        self.language_combo = QComboBox()
        for code, label in SUPPORTED_LANGUAGES.items():
            self.language_combo.addItem(label, code)
        selected_language = self.config_mgr.get_app_setting("language", current_language())
        selected_index = self.language_combo.findData(selected_language)
        if selected_index >= 0:
            self.language_combo.setCurrentIndex(selected_index)
        self.language_combo.setStyleSheet("font-size: 12px; padding: 4px; min-width: 150px;")
        language_row.addWidget(self.language_combo)
        language_row.addStretch()
        language_layout.addLayout(language_row)
        restart_note = QLabel(tr("settings.restart_note"))
        restart_note.setStyleSheet("color: gray; font-size: 11px;")
        restart_note.setWordWrap(True)
        language_layout.addWidget(restart_note)
        return language_group

    def _build_close_behavior_group(self) -> QGroupBox:
        group_box = QGroupBox(tr("settings.close_behavior"))
        group_layout = QVBoxLayout(group_box)

        self.btn_group = QButtonGroup(self)

        self.radio_ask = QRadioButton(tr("settings.ask_every_time"))
        self.btn_group.addButton(self.radio_ask)
        group_layout.addWidget(self.radio_ask)

        self.radio_minimize = QRadioButton(tr("settings.always_minimize"))
        self.btn_group.addButton(self.radio_minimize)
        group_layout.addWidget(self.radio_minimize)

        self.radio_exit = QRadioButton(tr("settings.always_exit"))
        self.btn_group.addButton(self.radio_exit)
        group_layout.addWidget(self.radio_exit)

        current_action = self.config_mgr.get_app_setting('close_action', 'ask')
        if current_action == 'minimize':
            self.radio_minimize.setChecked(True)
        elif current_action == 'exit':
            self.radio_exit.setChecked(True)
        else:  # 'ask' or default
            self.radio_ask.setChecked(True)
        return group_box

    def _build_theme_group(self) -> QGroupBox:
        theme_group = QGroupBox(tr("settings.theme"))
        theme_layout = QHBoxLayout(theme_group)

        theme_label = QLabel(tr("settings.theme_label"))
        theme_label.setStyleSheet("font-size: 12px;")
        theme_layout.addWidget(theme_label)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem(tr("settings.system_theme"), ThemeType.SYSTEM.value)
        self.theme_combo.addItem(tr("settings.light_mode"), ThemeType.LIGHT.value)
        self.theme_combo.addItem(tr("settings.dark_mode"), ThemeType.DARK.value)
        self.theme_combo.setStyleSheet("font-size: 12px; padding: 4px; min-width: 150px;")

        # 현재 테마 설정 로드 (시그널 연결 전에 인덱스를 맞춰서 다이얼로그를 여는 것만으로
        # 미리보기/저장이 트리거되지 않도록 함)
        theme_mgr = ThemeManager.instance()
        current_theme = theme_mgr.current_theme_type.value
        index = self.theme_combo.findData(current_theme)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)

        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_layout.addWidget(self.theme_combo)

        theme_layout.addStretch()

        return theme_group

    def _build_github_group(self) -> QGroupBox:
        github_group = QGroupBox(tr("settings.github_auto_report"))
        github_layout = QVBoxLayout(github_group)

        # GitHub App 설정 확인
        self._github_app_configured = self._check_github_app()

        # 자동 보고 활성화 체크박스
        self.chk_auto_report = QCheckBox("Export/Import 오류 시 자동으로 GitHub 이슈 생성")
        self.chk_auto_report.setStyleSheet("font-size: 12px;")
        github_layout.addWidget(self.chk_auto_report)

        # GitHub App 설정 상태에 따른 설명
        if self._github_app_configured:
            desc_label = QLabel(
                "✅ GitHub App이 설정되어 있습니다.\n"
                "오류 발생 시 자동으로 이슈를 생성하거나, 유사한 이슈가 있으면 코멘트를 추가합니다."
            )
            desc_label.setStyleSheet("color: #27ae60; font-size: 11px; margin-left: 20px; margin-top: 5px;")
            desc_label.setWordWrap(True)
            github_layout.addWidget(desc_label)

            # 연결 테스트 버튼
            test_layout = QHBoxLayout()
            test_layout.setContentsMargins(20, 5, 0, 0)
            btn_test = QPushButton("연결 테스트")
            btn_test.setStyleSheet(ButtonStyles.MUTED_SMALL_TALL)
            btn_test.clicked.connect(self._test_github_connection)
            test_layout.addWidget(btn_test)
            test_layout.addStretch()
            github_layout.addLayout(test_layout)
        else:
            desc_label = QLabel(
                "⚠️ GitHub App이 설정되지 않았습니다.\n"
                "환경변수 또는 내장 설정이 필요합니다. (GITHUB_APP_SETUP.md 참조)"
            )
            desc_label.setStyleSheet("color: #e74c3c; font-size: 11px; margin-left: 20px; margin-top: 5px;")
            desc_label.setWordWrap(True)
            self.chk_auto_report.setEnabled(False)
            github_layout.addWidget(desc_label)
        return github_group

    def _build_backup_group(self) -> QGroupBox:
        backup_group = QGroupBox(tr("settings.backup_restore"))
        backup_layout = QVBoxLayout(backup_group)

        # 백업 목록 라벨
        backup_list_label = QLabel("백업 목록 (최근 5개):")
        backup_list_label.setStyleSheet("font-size: 12px; margin-bottom: 5px;")
        backup_layout.addWidget(backup_list_label)

        # 백업 목록 (QListWidget)
        self.backup_list = QListWidget()
        self.backup_list.setStyleSheet("""
            QListWidget {
                font-size: 11px;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
            }
            QListWidget::item {
                padding: 5px;
            }
            QListWidget::item:selected {
                background-color: #3498db;
                color: white;
            }
        """)
        self.backup_list.setMaximumHeight(100)
        backup_layout.addWidget(self.backup_list)

        # 백업 관리 버튼들
        backup_btn_layout = QHBoxLayout()

        btn_restore = QPushButton("복원")
        btn_restore.setStyleSheet(ButtonStyles.SUCCESS_SMALL)
        btn_restore.clicked.connect(self._restore_selected_backup)
        backup_btn_layout.addWidget(btn_restore)

        btn_export = QPushButton("내보내기")
        btn_export.setStyleSheet(ButtonStyles.INFO_SMALL)
        btn_export.clicked.connect(self._export_config)
        backup_btn_layout.addWidget(btn_export)

        btn_import = QPushButton("가져오기")
        btn_import.setStyleSheet(ButtonStyles.MUTED_SMALL)
        btn_import.clicked.connect(self._import_config)
        backup_btn_layout.addWidget(btn_import)

        backup_btn_layout.addStretch()
        backup_layout.addLayout(backup_btn_layout)

        return backup_group

    def _build_reconnect_group(self) -> QGroupBox:
        reconnect_group = QGroupBox(tr("settings.reconnect"))
        reconnect_layout = QVBoxLayout(reconnect_group)

        self.chk_auto_reconnect = QCheckBox(tr("settings.auto_reconnect"))
        self.chk_auto_reconnect.setStyleSheet("font-size: 12px;")
        self.chk_auto_reconnect.setChecked(
            self.config_mgr.get_app_setting('auto_reconnect', True)
        )
        reconnect_layout.addWidget(self.chk_auto_reconnect)

        max_attempts_layout = QHBoxLayout()
        max_attempts_label = QLabel(tr("settings.max_reconnect_attempts"))
        max_attempts_label.setStyleSheet("font-size: 12px; margin-left: 20px;")
        max_attempts_layout.addWidget(max_attempts_label)

        self.spin_max_reconnect = QSpinBox()
        self.spin_max_reconnect.setRange(1, 20)
        self.spin_max_reconnect.setValue(
            self.config_mgr.get_app_setting('max_reconnect_attempts', 5)
        )
        self.spin_max_reconnect.setStyleSheet("font-size: 12px; min-width: 60px;")
        max_attempts_layout.addWidget(self.spin_max_reconnect)
        max_attempts_layout.addStretch()
        reconnect_layout.addLayout(max_attempts_layout)

        reconnect_desc = QLabel(tr("settings.reconnect_description"))
        reconnect_desc.setStyleSheet("color: gray; font-size: 11px; margin-left: 20px;")
        reconnect_desc.setWordWrap(True)
        reconnect_layout.addWidget(reconnect_desc)

        return reconnect_group

    def _build_startup_group(self) -> QGroupBox:
        startup_group = QGroupBox(tr("settings.startup"))
        startup_layout = QVBoxLayout(startup_group)

        self.chk_startup = QCheckBox(tr("settings.startup_auto"))
        self.chk_startup.setStyleSheet("font-size: 12px;")
        self.chk_startup.setChecked(self._is_startup_registered())
        startup_layout.addWidget(self.chk_startup)

        startup_desc = QLabel(tr("settings.startup_description"))
        startup_desc.setStyleSheet("color: gray; font-size: 11px; margin-left: 20px;")
        startup_desc.setWordWrap(True)
        startup_layout.addWidget(startup_desc)

        layout.addWidget(startup_group)

        # 자동 시작이 지원되지 않는 플랫폼에서는 숨김
        if not StartupRegistrar().is_supported:
            startup_group.setVisible(False)

        return startup_group

    def _refresh_backup_list(self):
        """백업 목록 새로고침"""
        self.backup_list.clear()
        backups = self.config_mgr.list_backups()

        if not backups:
            item = QListWidgetItem("(백업 없음)")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.backup_list.addItem(item)
        else:
            for filename, timestamp, size in backups:
                size_kb = size / 1024
                item_text = f"{timestamp}  ({size_kb:.1f} KB)"
                item = QListWidgetItem(item_text)
                item.setData(Qt.ItemDataRole.UserRole, filename)
                self.backup_list.addItem(item)

    def _restore_selected_backup(self):
        """선택한 백업으로 복원"""
        current_item = self.backup_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "알림", "복원할 백업을 선택하세요.")
            return

        filename = current_item.data(Qt.ItemDataRole.UserRole)
        if not filename:
            QMessageBox.warning(self, "알림", "복원할 백업을 선택하세요.")
            return

        reply = QMessageBox.question(
            self, "복원 확인",
            "선택한 백업으로 설정을 복원하시겠습니까?\n\n"
            "현재 설정은 자동으로 백업됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            success, msg = self.config_mgr.restore_backup(filename)
            if success:
                QMessageBox.information(self, "복원 완료", msg + "\n\n앱을 재시작하면 변경사항이 적용됩니다.")
                self._refresh_backup_list()
            else:
                QMessageBox.warning(self, "복원 실패", msg)

    def _export_config(self):
        """설정 내보내기"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "설정 내보내기",
            "tunnelforge_config.json",
            "JSON 파일 (*.json)"
        )

        if file_path:
            success, msg = self.config_mgr.export_config(file_path)
            if success:
                QMessageBox.information(self, "내보내기 완료", msg)
            else:
                QMessageBox.warning(self, "내보내기 실패", msg)

    def _import_config(self):
        """설정 가져오기"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "설정 가져오기",
            "",
            "JSON 파일 (*.json)"
        )

        if file_path:
            reply = QMessageBox.question(
                self, "가져오기 확인",
                "선택한 파일에서 설정을 가져오시겠습니까?\n\n"
                "현재 설정은 자동으로 백업됩니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                success, msg = self.config_mgr.import_config(file_path)
                if success:
                    QMessageBox.information(self, "가져오기 완료", msg + "\n\n앱을 재시작하면 변경사항이 적용됩니다.")
                    self._refresh_backup_list()
                else:
                    QMessageBox.warning(self, "가져오기 실패", msg)

    def _create_log_tab(self) -> QWidget:
        """로그 뷰어 탭 생성"""
        self.log_tab = LogViewerTab(self)
        return self.log_tab

    def _create_about_tab(self) -> QWidget:
        """정보 탭 생성"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 앱 정보
        info_group = QGroupBox("애플리케이션 정보")
        info_layout = QVBoxLayout(info_group)

        # 앱 이름 & 버전
        app_label = QLabel(f"<b>{__app_name__}</b>")
        app_label.setStyleSheet("font-size: 16px; margin-bottom: 5px;")
        info_layout.addWidget(app_label)

        version_label = QLabel(f"버전: {__version__}")
        version_label.setStyleSheet("font-size: 13px; color: #555;")
        info_layout.addWidget(version_label)

        layout.addWidget(info_group)

        # 업데이트 확인
        update_group = QGroupBox("업데이트")
        update_layout = QVBoxLayout(update_group)

        # 자동 업데이트 확인 체크박스
        self.chk_auto_update = QCheckBox("앱 시작 시 자동으로 업데이트 확인")
        self.chk_auto_update.setChecked(self.config_mgr.get_app_setting('auto_update_check', True))
        update_layout.addWidget(self.chk_auto_update)

        # 업데이트 확인 버튼
        btn_layout = QHBoxLayout()
        self.btn_check_update = QPushButton("업데이트 확인")
        self.btn_check_update.setStyleSheet(ButtonStyles.PRIMARY_MD)
        self.btn_check_update.clicked.connect(self._check_for_updates)
        btn_layout.addWidget(self.btn_check_update)
        btn_layout.addStretch()
        update_layout.addLayout(btn_layout)

        # 업데이트 상태 표시 (QTextBrowser로 변경 - HTML 링크 및 동적 크기 지원)
        self.update_status_label = QTextBrowser()
        self.update_status_label.setReadOnly(True)
        self.update_status_label.setOpenExternalLinks(True)
        self.update_status_label.setStyleSheet("""
            QTextBrowser {
                background-color: transparent;
                border: none;
                margin-top: 10px;
                font-size: 12px;
            }
        """)
        # 내용에 맞게 크기 자동 조정
        self.update_status_label.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.update_status_label.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.update_status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.update_status_label.setMinimumHeight(20)
        self.update_status_label.setMaximumHeight(100)
        self.update_status_label.document().contentsChanged.connect(self._adjust_update_label_height)
        update_layout.addWidget(self.update_status_label)

        # 다운로드 버튼 및 진행률 표시 (초기에는 숨김)
        self.download_widget = QWidget()
        download_layout = QVBoxLayout(self.download_widget)
        download_layout.setContentsMargins(0, 10, 0, 0)

        # 다운로드/설치 버튼 및 취소 버튼
        download_btn_layout = QHBoxLayout()
        self.btn_download = QPushButton("🔽 새 버전 다운로드")
        self.btn_download.setStyleSheet(ButtonStyles.SUCCESS_MD)
        self.btn_download.clicked.connect(self._start_download)
        download_btn_layout.addWidget(self.btn_download)

        self.btn_cancel_download = QPushButton("취소")
        self.btn_cancel_download.setStyleSheet(ButtonStyles.DANGER_MD)
        self.btn_cancel_download.clicked.connect(self._cancel_download)
        self.btn_cancel_download.hide()
        download_btn_layout.addWidget(self.btn_cancel_download)

        download_btn_layout.addStretch()
        download_layout.addLayout(download_btn_layout)

        # 진행률 바
        self.download_progress = QProgressBar()
        self.download_progress.setMinimum(0)
        self.download_progress.setMaximum(100)
        self.download_progress.setTextVisible(True)
        self.download_progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                text-align: center;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #3498db;
                border-radius: 3px;
            }
        """)
        self.download_progress.hide()
        download_layout.addWidget(self.download_progress)

        # 다운로드 상세 정보
        self.download_detail_label = QLabel("")
        self.download_detail_label.setStyleSheet("font-size: 11px; color: #555;")
        self.download_detail_label.hide()
        download_layout.addWidget(self.download_detail_label)

        self.download_widget.hide()
        update_layout.addWidget(self.download_widget)

        # 다운로드 관련 상태 변수 초기화
        self._download_worker = None
        self._downloaded_installer_path = None
        self._downloaded_installer_sha256 = None
        self._downloaded_installer_size = 0
        self._downloaded_installer_owner = None
        self._download_generation = 0
        self._download_signal_relays = {}
        self._latest_version = None

        layout.addWidget(update_group)

        # GitHub 링크
        github_group = QGroupBox("프로젝트")
        github_layout = QVBoxLayout(github_group)

        github_url = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        github_link = QLabel(f'GitHub: <a href="{github_url}">{GITHUB_OWNER}/{GITHUB_REPO}</a>')
        github_link.setOpenExternalLinks(True)
        github_link.setStyleSheet("font-size: 12px;")
        github_layout.addWidget(github_link)

        license_label = QLabel("라이선스: MIT")
        license_label.setStyleSheet("font-size: 12px; color: #555;")
        github_layout.addWidget(license_label)

        layout.addWidget(github_group)

        layout.addStretch()

        return tab

    def _on_theme_changed(self, index: int):
        """테마 선택 변경 시 미리보기만 적용 (저장은 save_settings에서 처리)"""
        theme_value = self.theme_combo.currentData()
        try:
            theme_type = ThemeType(theme_value)
            theme_mgr = ThemeManager.instance()
            theme_mgr.set_theme(theme_type, save=False)
        except ValueError:
            pass

    def save_settings(self):
        """설정 저장"""
        if self.radio_minimize.isChecked():
            action = 'minimize'
        elif self.radio_exit.isChecked():
            action = 'exit'
        else:
            action = 'ask'

        self.config_mgr.set_app_setting('close_action', action)

        language = self.language_combo.currentData() or current_language()
        set_language(language)
        self.config_mgr.set_app_setting('language', language)

        # 테마 설정 저장 (미리보기 중이던 값을 확정 저장)
        theme_value = self.theme_combo.currentData()
        try:
            ThemeManager.instance().set_theme(ThemeType(theme_value), save=True)
            self._theme_saved = True
        except ValueError:
            pass

        # GitHub 자동 보고 설정 저장
        auto_report = self.chk_auto_report.isChecked()
        self.config_mgr.set_app_setting('github_auto_report', auto_report)

        # 자동 업데이트 확인 설정 저장
        auto_update_check = self.chk_auto_update.isChecked()
        self.config_mgr.set_app_setting('auto_update_check', auto_update_check)

        # 자동 재연결 설정 저장
        auto_reconnect = self.chk_auto_reconnect.isChecked()
        self.config_mgr.set_app_setting('auto_reconnect', auto_reconnect)

        max_reconnect = self.spin_max_reconnect.value()
        self.config_mgr.set_app_setting('max_reconnect_attempts', max_reconnect)

        # 시작 프로그램 설정 저장
        if StartupRegistrar().is_supported:
            self._set_startup_registry(self.chk_startup.isChecked())

        self.accept()

    def _restore_original_theme_if_unsaved(self):
        """테마가 저장되지 않은 채 다이얼로그가 닫히면 미리보기 이전 테마로 복원"""
        if not getattr(self, "_theme_saved", False):
            ThemeManager.instance().set_theme(self._original_theme_type, save=False)

    def reject(self):
        """취소(또는 창 닫기) 시 미리보기 중이던 테마를 원래 상태로 복원"""
        self._restore_original_theme_if_unsaved()
        super().reject()

    def _is_startup_registered(self) -> bool:
        """레지스트리에 시작 프로그램 등록 여부 확인"""
        return StartupRegistrar().is_registered()

    def _set_startup_registry(self, enable: bool):
        """레지스트리에 시작 프로그램 등록/해제"""
        success, message = StartupRegistrar().set_registered(enable)
        if not success and message:
            QMessageBox.warning(
                self, "시작 프로그램 설정 오류",
                f"시작 프로그램 설정 중 오류가 발생했습니다:\n{message}"
            )

    def _check_for_updates(self):
        """업데이트 확인"""
        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText("확인 중...")
        self.update_status_label.setHtml(
            translate_text('<div style="color: #3498db; font-size: 12px;">업데이트를 확인하는 중입니다...</div>')
        )

        # 백그라운드 스레드에서 확인
        self._update_checker_thread = UpdateCheckerThread(config_manager=self.config_mgr)
        self._update_checker_thread.update_checked.connect(self._on_update_checked)
        self._update_checker_thread.start()

    def _on_update_checked(self, needs_update: bool, latest_version: str, download_url: str, error_msg: str):
        """업데이트 확인 결과 처리"""
        self.btn_check_update.setEnabled(True)
        self.btn_check_update.setText("업데이트 확인")

        if error_msg:
            self.update_status_label.setHtml(
                f'<div style="color: #e74c3c; font-size: 12px;">❌ {error_msg}</div>'
            )
            self.download_widget.hide()
            return

        if needs_update:
            self._latest_version = latest_version
            self.update_status_label.setHtml(
                translate_text(
                    f'<div style="color: #27ae60; font-size: 12px;">'
                    f'✅ 새로운 버전 {latest_version}이 사용 가능합니다!'
                    f'</div>'
                )
            )
            # 다운로드 버튼 표시
            self.download_widget.show()
            self.btn_download.setText(f"🔽 v{latest_version} 다운로드")
            self.btn_download.setEnabled(True)
            self.download_progress.hide()
            self.download_detail_label.hide()
            self.btn_cancel_download.hide()
        else:
            self.update_status_label.setHtml(
                translate_text(
                    f'<div style="color: #27ae60; font-size: 12px;">'
                    f'✅ 최신 버전({__version__})을 사용하고 있습니다.'
                    f'</div>'
                )
            )
            self.download_widget.hide()

    def _start_download(self):
        """업데이트 다운로드 시작"""
        from src.ui.workers import UpdateDownloadWorker

        self._download_generation += 1
        generation = self._download_generation
        self._downloaded_installer_path = None
        self._downloaded_installer_sha256 = None
        self._downloaded_installer_size = 0
        self._downloaded_installer_owner = None

        # UI 상태 변경
        self.btn_check_update.setEnabled(False)
        self.btn_download.setEnabled(False)
        self.btn_download.setText("다운로드 준비 중...")
        self.btn_cancel_download.show()
        self.download_progress.setValue(0)
        self.download_progress.show()
        self.download_detail_label.setText("설치 파일 정보를 가져오는 중...")
        self.download_detail_label.show()

        # Worker 시작
        worker = UpdateDownloadWorker(config_manager=self.config_mgr)
        self._download_worker = worker

        def is_current_generation():
            return (
                generation == self._download_generation
                and worker is self._download_worker
            )

        def verification_relay(sha256, file_size):
            if is_current_generation():
                self._on_download_verification_ready(sha256, file_size)

        def info_relay(version, file_size):
            if is_current_generation():
                self._on_download_info_fetched(version, file_size)

        def progress_relay(downloaded, total):
            if is_current_generation():
                self._on_download_progress(downloaded, total)

        def finished_relay(success, result):
            if not is_current_generation():
                if success and result:
                    worker.downloader.discard_downloaded_installer(result)
                self._download_signal_relays.pop(generation, None)
                return
            self._on_download_finished(success, result, worker.downloader)
            self._download_worker = None
            self._download_signal_relays.pop(generation, None)

        relays = (
            verification_relay,
            info_relay,
            progress_relay,
            finished_relay,
        )
        self._download_signal_relays[generation] = relays
        worker.verification_ready.connect(verification_relay)
        worker.info_fetched.connect(info_relay)
        worker.progress.connect(progress_relay)
        worker.finished.connect(finished_relay)
        worker.start()

    def _on_download_info_fetched(self, version: str, file_size: int):
        """설치 프로그램 정보 수신"""
        self.btn_download.setText("다운로드 중...")
        size_str = format_size(file_size)
        self.download_detail_label.setText(f"파일 크기: {size_str}")

    def _on_download_verification_ready(self, sha256: str, file_size: int):
        """Store immutable release metadata for launch-time verification."""
        self._downloaded_installer_sha256 = sha256
        self._downloaded_installer_size = file_size

    def _on_download_progress(self, downloaded: int, total: int):
        """다운로드 진행률 업데이트"""
        if total > 0:
            percent = int((downloaded / total) * 100)
            self.download_progress.setValue(percent)
            downloaded_str = format_size(downloaded)
            total_str = format_size(total)
            self.download_detail_label.setText(f"{downloaded_str} / {total_str}")

    def _on_download_finished(self, success: bool, result: str, owner=None):
        """다운로드 완료 처리"""
        self.btn_cancel_download.hide()

        if success:
            self._downloaded_installer_path = result
            self._downloaded_installer_owner = owner
            action_text = update_package_action_text()
            self.download_progress.setValue(100)
            self.btn_download.setText(action_text.button)
            self.btn_download.setEnabled(True)
            self.btn_download.setStyleSheet(ButtonStyles.INSTALL)
            # 버튼 클릭 이벤트 변경
            self.btn_download.clicked.disconnect()
            self.btn_download.clicked.connect(self._launch_installer)
            self.download_detail_label.setText(action_text.done_message)
        else:
            self._downloaded_installer_path = None
            self._downloaded_installer_sha256 = None
            self._downloaded_installer_size = 0
            self._downloaded_installer_owner = None
            self.download_progress.hide()
            self.btn_download.setText(f"🔽 v{self._latest_version} 다운로드")
            self.btn_download.setEnabled(True)
            self.btn_check_update.setEnabled(True)
            self.download_detail_label.setText(f"❌ {result}")
            self.download_detail_label.setStyleSheet("font-size: 11px; color: #e74c3c;")

    def _cancel_download(self):
        """다운로드 취소"""
        cancelled_generation = self._download_generation
        self._download_generation += 1
        worker = self._download_worker
        self._download_worker = None
        if worker:
            worker.cancel()
            worker.wait()
        self._download_signal_relays.pop(cancelled_generation, None)

        self._downloaded_installer_path = None
        self._downloaded_installer_sha256 = None
        self._downloaded_installer_size = 0
        self._downloaded_installer_owner = None

        # UI 상태 복원
        self.btn_cancel_download.hide()
        self.download_progress.hide()
        self.btn_download.setText(f"🔽 v{self._latest_version} 다운로드")
        self.btn_download.setEnabled(True)
        self.btn_check_update.setEnabled(True)
        self.download_detail_label.setText("다운로드가 취소되었습니다.")

    def _launch_installer(self):
        """설치 프로그램 실행 및 앱 종료"""
        if not self._downloaded_installer_path or not os.path.exists(self._downloaded_installer_path):
            QMessageBox.warning(
                self,
                "설치 오류",
                "설치 파일을 찾을 수 없습니다.\n다시 다운로드해 주세요."
            )
            return

        if not SettingsDialog._verify_installer_before_launch(self):
            return

        # 확인 메시지 구성 (활성 터널 경고 포함)
        main_window = self.parent()
        action_text = update_package_action_text()
        confirm_msg = f"{action_text.confirm_question}\n\n{action_text.confirm_body}"

        tunnel_names = _collect_active_tunnel_names(main_window)
        if tunnel_names:
            tunnel_list = "\n".join(f"  • {name}" for name in tunnel_names)
            confirm_msg = (
                f"{action_text.confirm_question}\n\n"
                f"⚠️ 현재 {len(tunnel_names)}개의 활성 터널이 연결 해제됩니다:\n"
                f"{tunnel_list}\n\n"
                f"{action_text.confirm_body}"
            )

        # 확인 다이얼로그
        reply = QMessageBox.question(
            self,
            action_text.confirm_title,
            confirm_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        if not SettingsDialog._verify_installer_before_launch(self):
            return

        try:
            if sys.platform == 'win32':
                subprocess.Popen(
                    [self._downloaded_installer_path],
                    close_fds=True,
                )
            elif update_package_launch_strategy() == "open":
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(self._downloaded_installer_path)):
                    raise RuntimeError("다운로드한 패키지를 열 수 없습니다.")
            else:
                subprocess.Popen(
                    [self._downloaded_installer_path],
                    start_new_session=True,
                    close_fds=True,
                )

            # closeEvent 우회하여 직접 종료 (CloseConfirmDialog 방지)
            if main_window and hasattr(main_window, 'close_app'):
                main_window.close_app()
            else:
                QApplication.instance().quit()  # fallback

        except Exception as e:
            QMessageBox.critical(
                self,
                "실행 오류",
                f"설치 프로그램 실행에 실패했습니다:\n{str(e)}"
            )

    def _verify_installer_before_launch(self) -> bool:
        try:
            if not self._downloaded_installer_sha256:
                raise IntegrityError(
                    "release asset SHA-256 digest is missing or invalid"
                )
            verify_file_integrity(
                self._downloaded_installer_path,
                self._downloaded_installer_sha256,
                self._downloaded_installer_size,
            )
            return True
        except (IntegrityError, OSError) as exc:
            SettingsDialog._discard_invalid_installer(self, str(exc))
            return False

    def _discard_invalid_installer(self, error_message: str) -> None:
        installer_path = self._downloaded_installer_path
        owner = self._downloaded_installer_owner
        discard = getattr(owner, "discard_downloaded_installer", None)
        if installer_path and callable(discard):
            try:
                discard(installer_path)
            except Exception:
                pass

        self._downloaded_installer_path = None
        self._downloaded_installer_sha256 = None
        self._downloaded_installer_size = 0
        self._downloaded_installer_owner = None
        self.download_progress.hide()
        self.btn_download.setText(f"🔽 v{self._latest_version} 다운로드")
        self.btn_download.setEnabled(True)
        self.btn_download.setStyleSheet(ButtonStyles.SUCCESS_MD)
        self.btn_check_update.setEnabled(True)
        self.download_detail_label.setText(
            translate_text("❌ 설치 파일 무결성 검증에 실패했습니다.")
        )
        self.download_detail_label.setStyleSheet(
            "font-size: 11px; color: #e74c3c;"
        )
        self.btn_download.clicked.disconnect()
        self.btn_download.clicked.connect(self._start_download)
        QMessageBox.critical(
            self,
            translate_text("설치 파일 무결성 오류"),
            translate_text(
                "다운로드한 설치 파일이 릴리스 정보와 일치하지 않아 실행을 차단했습니다.\n"
                f"다시 다운로드해 주세요.\n\n{error_message}"
            ),
        )

    def _adjust_update_label_height(self):
        """업데이트 상태 라벨 높이를 내용에 맞게 조정"""
        doc_height = self.update_status_label.document().size().height()
        # 여백 추가하여 적절한 높이 설정
        new_height = min(max(int(doc_height) + 10, 20), 100)
        self.update_status_label.setFixedHeight(new_height)

    def _check_github_app(self) -> bool:
        """GitHub App 설정 여부 확인"""
        try:
            from src.core.github_app_auth import is_github_app_configured
            return is_github_app_configured()
        except ImportError:
            return False

    def _load_github_settings(self):
        """GitHub 설정 로드"""
        auto_report = self.config_mgr.get_app_setting('github_auto_report', False)
        # GitHub App이 설정되지 않았으면 자동 보고 비활성화
        if not self._github_app_configured:
            auto_report = False
        self.chk_auto_report.setChecked(auto_report)

    def _test_github_connection(self):
        """GitHub API 연결 테스트"""
        try:
            from src.core.github_app_auth import get_github_app_auth

            github_app = get_github_app_auth()
            if not github_app:
                QMessageBox.warning(
                    self,
                    "연결 테스트",
                    "GitHub App 인스턴스를 생성할 수 없습니다.\n환경변수 설정을 확인하세요."
                )
                return

            # 라이브러리 확인
            available, msg = github_app.check_available()
            if not available:
                QMessageBox.warning(self, "연결 테스트", msg)
                return

            # 연결 테스트 실행
            success, message = github_app.test_connection()

            if success:
                QMessageBox.information(
                    self,
                    "연결 테스트 성공",
                    message
                )
            else:
                QMessageBox.warning(
                    self,
                    "연결 테스트 실패",
                    message
                )

        except ImportError as e:
            QMessageBox.critical(
                self,
                "오류",
                f"모듈을 불러올 수 없습니다: {str(e)}"
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "오류",
                f"연결 테스트 중 오류 발생:\n{str(e)}"
            )
