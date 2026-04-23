"""설정 관련 다이얼로그"""
import os
import subprocess
import sys
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QRadioButton, QCheckBox,
                             QButtonGroup, QGroupBox, QMessageBox, QTabWidget,
                             QWidget, QTextBrowser, QSizePolicy, QTextEdit,
                             QComboBox, QListWidget, QListWidgetItem, QFileDialog,
                             QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
                             QProgressBar, QApplication)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QCursor, QFont
from PyQt6.QtCore import QUrl
from src.version import __version__, __app_name__, GITHUB_OWNER, GITHUB_REPO
from src.core.update_downloader import format_size
from src.core.logger import get_log_file_path, get_log_dir, read_log_file, filter_log_by_level, clear_log_file
from src.ui.themes import ThemeType
from src.ui.theme_manager import ThemeManager


class CloseConfirmDialog(QDialog):
    """종료 시 선택 다이얼로그"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("프로그램 종료")
        self.setFixedSize(350, 180)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 안내 메시지
        label = QLabel("프로그램을 어떻게 처리하시겠습니까?")
        label.setStyleSheet("font-size: 13px; margin-bottom: 10px;")
        layout.addWidget(label)

        # 라디오 버튼 그룹
        self.btn_group = QButtonGroup(self)

        self.radio_minimize = QRadioButton("시스템 트레이로 최소화 (백그라운드 실행)")
        self.radio_minimize.setChecked(True)
        self.btn_group.addButton(self.radio_minimize)
        layout.addWidget(self.radio_minimize)

        self.radio_exit = QRadioButton("프로그램 완전 종료")
        self.btn_group.addButton(self.radio_exit)
        layout.addWidget(self.radio_exit)

        # 기억 체크박스
        self.chk_remember = QCheckBox("이 선택을 기억하고 다시 묻지 않기")
        self.chk_remember.setStyleSheet("margin-top: 10px;")
        layout.addWidget(self.chk_remember)

        # 버튼
        button_layout = QHBoxLayout()
        btn_ok = QPushButton("확인")
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_ok.clicked.connect(self.accept)

        btn_cancel = QPushButton("취소")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(btn_ok)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def get_result(self):
        """선택된 동작과 기억 여부 반환"""
        action = 'minimize' if self.radio_minimize.isChecked() else 'exit'
        remember = self.chk_remember.isChecked()
        return action, remember


class UpdateCheckerThread(QThread):
    """업데이트 확인 백그라운드 스레드"""
    update_checked = pyqtSignal(bool, str, str, str)  # needs_update, latest_version, download_url, error_msg

    def __init__(self, config_manager=None):
        super().__init__()
        self._config_manager = config_manager

    def run(self):
        try:
            from src.core.update_checker import UpdateChecker
            checker = UpdateChecker(config_manager=self._config_manager)
            needs_update, latest_version, download_url, error_msg = checker.check_update()
            self.update_checked.emit(needs_update, latest_version or "", download_url or "", error_msg or "")
        except Exception as e:
            self.update_checked.emit(False, "", "", f"업데이트 확인 실패: {str(e)}")


class SettingsDialog(QDialog):
    """앱 설정 다이얼로그"""
    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_mgr = config_manager
        self.setWindowTitle("설정")
        self.setMinimumSize(600, 420)
        self._update_checker_thread = None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 탭 위젯 생성
        tabs = QTabWidget()
        tabs.addTab(self._create_general_tab(), "일반")
        tabs.addTab(self._create_pool_tab(), "연결 풀")
        tabs.addTab(self._create_log_tab(), "로그")
        tabs.addTab(self._create_about_tab(), "정보")
        layout.addWidget(tabs)

        # 버튼
        button_layout = QHBoxLayout()
        btn_save = QPushButton("저장")
        btn_save.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_save.clicked.connect(self.save_settings)

        btn_cancel = QPushButton("취소")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(btn_save)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def _create_general_tab(self) -> QWidget:
        """일반 설정 탭 생성"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 종료 동작 설정 그룹
        group_box = QGroupBox("창 닫기(X) 버튼 동작")
        group_layout = QVBoxLayout(group_box)

        self.btn_group = QButtonGroup(self)

        self.radio_ask = QRadioButton("매번 묻기")
        self.btn_group.addButton(self.radio_ask)
        group_layout.addWidget(self.radio_ask)

        self.radio_minimize = QRadioButton("항상 시스템 트레이로 최소화")
        self.btn_group.addButton(self.radio_minimize)
        group_layout.addWidget(self.radio_minimize)

        self.radio_exit = QRadioButton("항상 프로그램 종료")
        self.btn_group.addButton(self.radio_exit)
        group_layout.addWidget(self.radio_exit)

        layout.addWidget(group_box)

        # 현재 설정 로드
        current_action = self.config_mgr.get_app_setting('close_action', 'ask')
        if current_action == 'minimize':
            self.radio_minimize.setChecked(True)
        elif current_action == 'exit':
            self.radio_exit.setChecked(True)
        else:  # 'ask' or default
            self.radio_ask.setChecked(True)

        # 테마 설정 그룹
        theme_group = QGroupBox("테마")
        theme_layout = QHBoxLayout(theme_group)

        theme_label = QLabel("화면 테마:")
        theme_label.setStyleSheet("font-size: 12px;")
        theme_layout.addWidget(theme_label)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("시스템 설정 따르기", ThemeType.SYSTEM.value)
        self.theme_combo.addItem("라이트 모드", ThemeType.LIGHT.value)
        self.theme_combo.addItem("다크 모드", ThemeType.DARK.value)
        self.theme_combo.setStyleSheet("font-size: 12px; padding: 4px; min-width: 150px;")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_layout.addWidget(self.theme_combo)

        theme_layout.addStretch()

        layout.addWidget(theme_group)

        # 현재 테마 설정 로드
        theme_mgr = ThemeManager.instance()
        current_theme = theme_mgr.current_theme_type.value
        index = self.theme_combo.findData(current_theme)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)

        # GitHub 이슈 자동 보고 설정 그룹
        github_group = QGroupBox("GitHub 이슈 자동 보고")
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
            btn_test.setStyleSheet("""
                QPushButton {
                    background-color: #95a5a6; color: white;
                    padding: 6px 12px; border-radius: 4px; border: none;
                    font-size: 11px;
                    min-height: 26px;
                }
                QPushButton:hover { background-color: #7f8c8d; }
            """)
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
        layout.addWidget(github_group)

        # GitHub 설정 로드
        self._load_github_settings()

        # 설정 백업/복원 그룹
        backup_group = QGroupBox("설정 백업/복원")
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
        btn_restore.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #219a52; }
        """)
        btn_restore.clicked.connect(self._restore_selected_backup)
        backup_btn_layout.addWidget(btn_restore)

        btn_export = QPushButton("내보내기")
        btn_export.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_export.clicked.connect(self._export_config)
        backup_btn_layout.addWidget(btn_export)

        btn_import = QPushButton("가져오기")
        btn_import.setStyleSheet("""
            QPushButton {
                background-color: #95a5a6; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #7f8c8d; }
        """)
        btn_import.clicked.connect(self._import_config)
        backup_btn_layout.addWidget(btn_import)

        backup_btn_layout.addStretch()
        backup_layout.addLayout(backup_btn_layout)

        layout.addWidget(backup_group)

        # 백업 목록 로드
        self._refresh_backup_list()

        # 자동 재연결 설정 그룹
        reconnect_group = QGroupBox("터널 자동 재연결")
        reconnect_layout = QVBoxLayout(reconnect_group)

        self.chk_auto_reconnect = QCheckBox("연결 끊김 시 자동 재연결")
        self.chk_auto_reconnect.setStyleSheet("font-size: 12px;")
        self.chk_auto_reconnect.setChecked(
            self.config_mgr.get_app_setting('auto_reconnect', True)
        )
        reconnect_layout.addWidget(self.chk_auto_reconnect)

        max_attempts_layout = QHBoxLayout()
        max_attempts_label = QLabel("최대 재연결 시도 횟수:")
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

        reconnect_desc = QLabel(
            "연결이 끊어지면 점진적 백오프(1초→60초)를 적용하여 자동으로 재연결을 시도합니다."
        )
        reconnect_desc.setStyleSheet("color: gray; font-size: 11px; margin-left: 20px;")
        reconnect_desc.setWordWrap(True)
        reconnect_layout.addWidget(reconnect_desc)

        layout.addWidget(reconnect_group)

        # Windows 시작 프로그램 설정 그룹
        startup_group = QGroupBox("Windows 시작 프로그램")
        startup_layout = QVBoxLayout(startup_group)

        self.chk_startup = QCheckBox("Windows 시작 시 자동 실행")
        self.chk_startup.setStyleSheet("font-size: 12px;")
        self.chk_startup.setChecked(self._is_startup_registered())
        startup_layout.addWidget(self.chk_startup)

        startup_desc = QLabel(
            "Windows 부팅 시 시스템 트레이에 최소화된 상태로 자동 시작됩니다."
        )
        startup_desc.setStyleSheet("color: gray; font-size: 11px; margin-left: 20px;")
        startup_desc.setWordWrap(True)
        startup_layout.addWidget(startup_desc)

        layout.addWidget(startup_group)

        # Windows가 아닌 경우 숨김
        if sys.platform != 'win32':
            startup_group.setVisible(False)

        layout.addStretch()

        return tab

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

    def _create_pool_tab(self) -> QWidget:
        """연결 풀 상태 탭 생성"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 설명
        desc_label = QLabel("DB 연결 풀 상태를 모니터링합니다. 연결 풀은 DB 연결을 재사용하여 성능을 향상시킵니다.")
        desc_label.setStyleSheet("color: #666; font-size: 11px; padding: 8px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # 풀 상태 테이블
        pool_group = QGroupBox("활성 연결 풀")
        pool_layout = QVBoxLayout(pool_group)

        self.pool_table = QTableWidget()
        self.pool_table.setColumnCount(5)
        self.pool_table.setHorizontalHeaderLabels(["풀 키", "생성됨", "사용 중", "대기 중", "최대"])
        self.pool_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.pool_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.pool_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.pool_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.pool_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.pool_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pool_table.setAlternatingRowColors(True)
        self.pool_table.setStyleSheet("""
            QTableWidget {
                font-size: 12px;
            }
            QTableWidget::item {
                padding: 4px;
            }
        """)
        pool_layout.addWidget(self.pool_table)

        # 컨트롤 버튼
        btn_layout = QHBoxLayout()

        btn_refresh_pool = QPushButton("🔄 새로고침")
        btn_refresh_pool.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_refresh_pool.clicked.connect(self._refresh_pool_status)
        btn_layout.addWidget(btn_refresh_pool)

        btn_close_all = QPushButton("🛑 모든 연결 종료")
        btn_close_all.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """)
        btn_close_all.clicked.connect(self._close_all_pools)
        btn_layout.addWidget(btn_close_all)

        btn_layout.addStretch()
        pool_layout.addLayout(btn_layout)

        layout.addWidget(pool_group)

        # 정보 레이블
        self.pool_info_label = QLabel("풀 상태를 새로고침하려면 '새로고침' 버튼을 클릭하세요.")
        self.pool_info_label.setStyleSheet("color: #666; font-size: 11px; padding: 8px;")
        layout.addWidget(self.pool_info_label)

        layout.addStretch()

        # 초기 로드
        self._refresh_pool_status()

        return tab

    def _refresh_pool_status(self):
        """연결 풀 상태 새로고침"""
        try:
            from src.core.connection_pool import get_pool_registry
            registry = get_pool_registry()
            stats_list = registry.get_all_stats()

            self.pool_table.setRowCount(len(stats_list))

            for row, stats in enumerate(stats_list):
                # 풀 키
                self.pool_table.setItem(row, 0, QTableWidgetItem(stats['pool_key']))
                # 생성됨
                self.pool_table.setItem(row, 1, QTableWidgetItem(str(stats['total_created'])))
                # 사용 중
                self.pool_table.setItem(row, 2, QTableWidgetItem(str(stats['in_use'])))
                # 대기 중 (available)
                self.pool_table.setItem(row, 3, QTableWidgetItem(str(stats['available'])))
                # 최대
                self.pool_table.setItem(row, 4, QTableWidgetItem(str(stats['max_connections'])))

            if stats_list:
                total_created = sum(s['total_created'] for s in stats_list)
                total_in_use = sum(s['in_use'] for s in stats_list)
                self.pool_info_label.setText(f"✅ {len(stats_list)}개 풀, 총 {total_created}개 연결 ({total_in_use}개 사용 중)")
            else:
                self.pool_info_label.setText("ℹ️ 활성 연결 풀이 없습니다.")

        except Exception as e:
            self.pool_info_label.setText(f"❌ 풀 상태 조회 실패: {str(e)}")

    def _close_all_pools(self):
        """모든 연결 풀 종료"""
        reply = QMessageBox.question(
            self, "확인",
            "모든 DB 연결 풀을 종료하시겠습니까?\n\n"
            "활성 연결이 있으면 작업이 중단될 수 있습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                from src.core.connection_pool import get_pool_registry
                registry = get_pool_registry()
                registry.close_all_pools()
                self._refresh_pool_status()
                QMessageBox.information(self, "완료", "모든 연결 풀이 종료되었습니다.")
            except Exception as e:
                QMessageBox.warning(self, "오류", f"풀 종료 실패: {str(e)}")

    def _create_log_tab(self) -> QWidget:
        """로그 뷰어 탭 생성"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 상단 컨트롤
        control_layout = QHBoxLayout()

        # 로그 레벨 필터
        filter_label = QLabel("로그 레벨:")
        filter_label.setStyleSheet("font-size: 12px;")
        control_layout.addWidget(filter_label)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["ALL", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level_combo.setCurrentText("ALL")
        self.log_level_combo.currentTextChanged.connect(self._on_log_filter_changed)
        self.log_level_combo.setStyleSheet("font-size: 12px; padding: 4px; min-width: 100px;")
        control_layout.addWidget(self.log_level_combo)

        control_layout.addStretch()

        # 새로고침 버튼
        btn_refresh_log = QPushButton("새로고침")
        btn_refresh_log.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_refresh_log.clicked.connect(self._refresh_log_viewer)
        control_layout.addWidget(btn_refresh_log)

        # 로그 폴더 열기 버튼
        btn_open_log_folder = QPushButton("로그 폴더 열기")
        btn_open_log_folder.setStyleSheet("""
            QPushButton {
                background-color: #95a5a6; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #7f8c8d; }
        """)
        btn_open_log_folder.clicked.connect(self._open_log_folder)
        control_layout.addWidget(btn_open_log_folder)

        # 로그 초기화 버튼
        btn_clear_log = QPushButton("로그 초기화")
        btn_clear_log.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """)
        btn_clear_log.clicked.connect(self._clear_log_file)
        control_layout.addWidget(btn_clear_log)

        layout.addLayout(control_layout)

        # 로그 파일 경로 표시
        log_path_label = QLabel(f"로그 파일: {get_log_file_path()}")
        log_path_label.setStyleSheet("font-size: 10px; color: #666; margin: 5px 0;")
        log_path_label.setWordWrap(True)
        layout.addWidget(log_path_label)

        # 로그 뷰어 (읽기 전용 텍스트 에디터)
        self.log_viewer = QTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setFont(QFont("Consolas", 9))
        self.log_viewer.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 8px;
            }
        """)
        self.log_viewer.setPlaceholderText("로그 파일이 없습니다.")
        layout.addWidget(self.log_viewer)

        # 최초 로그 로드
        self._refresh_log_viewer()

        return tab

    def _refresh_log_viewer(self):
        """로그 뷰어 새로고침"""
        content = read_log_file(max_lines=500)
        level = self.log_level_combo.currentText()
        filtered_content = filter_log_by_level(content, level)
        self.log_viewer.setPlainText(filtered_content)
        # 스크롤을 맨 아래로
        scrollbar = self.log_viewer.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_log_filter_changed(self, level: str):
        """로그 필터 변경 시"""
        self._refresh_log_viewer()

    def _open_log_folder(self):
        """로그 폴더를 탐색기에서 열기"""
        log_dir = get_log_dir()
        if os.path.exists(log_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir))
        else:
            QMessageBox.information(self, "알림", "로그 폴더가 아직 생성되지 않았습니다.")

    def _clear_log_file(self):
        """로그 파일 초기화"""
        reply = QMessageBox.question(
            self, "로그 초기화",
            "로그 파일을 초기화하시겠습니까?\n이 작업은 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            success, msg = clear_log_file()
            if success:
                self._refresh_log_viewer()
                QMessageBox.information(self, "알림", msg)
            else:
                QMessageBox.warning(self, "오류", msg)

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
        self.btn_check_update.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 8px 16px; border-radius: 4px; border: none;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #2980b9; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
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
        self.btn_download.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                padding: 8px 16px; border-radius: 4px; border: none;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #229954; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_download.clicked.connect(self._start_download)
        download_btn_layout.addWidget(self.btn_download)

        self.btn_cancel_download = QPushButton("취소")
        self.btn_cancel_download.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white;
                padding: 8px 12px; border-radius: 4px; border: none;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """)
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
        """테마 선택 변경 시 즉시 적용"""
        theme_value = self.theme_combo.currentData()
        try:
            theme_type = ThemeType(theme_value)
            theme_mgr = ThemeManager.instance()
            theme_mgr.set_theme(theme_type)
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

        # 테마 설정은 _on_theme_changed에서 이미 저장됨

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

        # Windows 시작 프로그램 설정 저장
        if sys.platform == 'win32':
            self._set_startup_registry(self.chk_startup.isChecked())

        self.accept()

    def _is_startup_registered(self) -> bool:
        """레지스트리에 시작 프로그램 등록 여부 확인"""
        if sys.platform != 'win32':
            return False
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ
            )
            try:
                winreg.QueryValueEx(key, "TunnelForge")
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def _set_startup_registry(self, enable: bool):
        """레지스트리에 시작 프로그램 등록/해제"""
        if sys.platform != 'win32':
            return
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            )
            try:
                if enable:
                    if getattr(sys, 'frozen', False):
                        # PyInstaller 빌드: exe 직접 실행
                        app_path = f'"{sys.executable}" --minimized'
                    else:
                        # 개발 환경: pythonw.exe로 콘솔 없이 실행
                        python_dir = os.path.dirname(sys.executable)
                        pythonw = os.path.join(python_dir, 'pythonw.exe')
                        if not os.path.exists(pythonw):
                            pythonw = sys.executable
                        main_py = os.path.abspath(
                            os.path.join(os.path.dirname(__file__), '..', '..', '..', 'main.py')
                        )
                        app_path = f'"{pythonw}" "{main_py}" --minimized'
                    winreg.SetValueEx(key, "TunnelForge", 0, winreg.REG_SZ, app_path)
                else:
                    try:
                        winreg.DeleteValue(key, "TunnelForge")
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
        except Exception as e:
            QMessageBox.warning(
                self, "시작 프로그램 설정 오류",
                f"레지스트리 설정 중 오류가 발생했습니다:\n{str(e)}"
            )

    def _check_for_updates(self):
        """업데이트 확인"""
        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText("확인 중...")
        self.update_status_label.setHtml(
            '<div style="color: #3498db; font-size: 12px;">업데이트를 확인하는 중입니다...</div>'
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
                f'<div style="color: #27ae60; font-size: 12px;">'
                f'✅ 새로운 버전 {latest_version}이 사용 가능합니다!'
                f'</div>'
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
                f'<div style="color: #27ae60; font-size: 12px;">'
                f'✅ 최신 버전({__version__})을 사용하고 있습니다.'
                f'</div>'
            )
            self.download_widget.hide()

    def _start_download(self):
        """업데이트 다운로드 시작"""
        from src.ui.workers import UpdateDownloadWorker

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
        self._download_worker = UpdateDownloadWorker(config_manager=self.config_mgr)
        self._download_worker.info_fetched.connect(self._on_download_info_fetched)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.start()

    def _on_download_info_fetched(self, version: str, file_size: int):
        """설치 프로그램 정보 수신"""
        self.btn_download.setText("다운로드 중...")
        size_str = format_size(file_size)
        self.download_detail_label.setText(f"파일 크기: {size_str}")

    def _on_download_progress(self, downloaded: int, total: int):
        """다운로드 진행률 업데이트"""
        if total > 0:
            percent = int((downloaded / total) * 100)
            self.download_progress.setValue(percent)
            downloaded_str = format_size(downloaded)
            total_str = format_size(total)
            self.download_detail_label.setText(f"{downloaded_str} / {total_str}")

    def _on_download_finished(self, success: bool, result: str):
        """다운로드 완료 처리"""
        self.btn_cancel_download.hide()

        if success:
            self._downloaded_installer_path = result
            self.download_progress.setValue(100)
            self.btn_download.setText("🚀 설치 시작")
            self.btn_download.setEnabled(True)
            self.btn_download.setStyleSheet("""
                QPushButton {
                    background-color: #9b59b6; color: white;
                    padding: 8px 16px; border-radius: 4px; border: none;
                    font-size: 12px; font-weight: bold;
                }
                QPushButton:hover { background-color: #8e44ad; }
            """)
            # 버튼 클릭 이벤트 변경
            self.btn_download.clicked.disconnect()
            self.btn_download.clicked.connect(self._launch_installer)
            self.download_detail_label.setText("✅ 다운로드 완료! '설치 시작' 버튼을 클릭하세요.")
        else:
            self.download_progress.hide()
            self.btn_download.setText(f"🔽 v{self._latest_version} 다운로드")
            self.btn_download.setEnabled(True)
            self.btn_check_update.setEnabled(True)
            self.download_detail_label.setText(f"❌ {result}")
            self.download_detail_label.setStyleSheet("font-size: 11px; color: #e74c3c;")

    def _cancel_download(self):
        """다운로드 취소"""
        if self._download_worker:
            self._download_worker.cancel()
            self._download_worker.wait()
            self._download_worker = None

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

        # 확인 메시지 구성 (활성 터널 경고 포함)
        main_window = self.parent()
        confirm_msg = (
            f"TunnelForge v{self._latest_version} 설치를 시작하시겠습니까?\n\n"
            "설치를 위해 현재 앱이 종료됩니다."
        )

        if main_window and hasattr(main_window, 'engine'):
            active_count = len(main_window.engine.active_tunnels)
            if active_count > 0:
                tunnel_names = []
                for tid in main_window.engine.active_tunnels:
                    config = main_window.engine.tunnel_configs.get(tid, {})
                    tunnel_names.append(config.get('name', tid))
                tunnel_list = "\n".join(f"  • {name}" for name in tunnel_names)
                confirm_msg = (
                    f"TunnelForge v{self._latest_version} 설치를 시작하시겠습니까?\n\n"
                    f"⚠️ 현재 {active_count}개의 활성 터널이 연결 해제됩니다:\n"
                    f"{tunnel_list}\n\n"
                    "설치를 위해 현재 앱이 종료됩니다."
                )

        # 확인 다이얼로그
        reply = QMessageBox.question(
            self,
            "설치 확인",
            confirm_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            if sys.platform == 'win32':
                # 배치 스크립트가 업데이트 전체 라이프사이클을 관리:
                #   ① 기존 프로세스 종료 대기
                #   ② 설치 프로그램 /SILENT 실행 (skipifsilent로 Inno Setup 자동 실행 방지)
                #   ③ 설치 프로세스 완료 대기
                #   ④ 앱 실행
                # 이렇게 하면 Inno Setup의 [Run] 자동 실행과의 경합(race condition)을 방지하고
                # PyInstaller _MEI 임시 디렉토리 충돌 문제를 해결
                import tempfile
                current_pid = os.getpid()
                app_exe_path = sys.executable
                bat_path = os.path.join(
                    tempfile.gettempdir(),
                    f"tunnelforge_update_{current_pid}.bat"
                )
                bat_content = (
                    "@echo off\r\n"
                    "setlocal\r\n"
                    f"set PID={current_pid}\r\n"
                    f'set INSTALLER="{self._downloaded_installer_path}"\r\n'
                    f'set APP_EXE="{app_exe_path}"\r\n'
                    "set MAX_WAIT=30\r\n"
                    "set COUNT=0\r\n"
                    "\r\n"
                    "rem === Phase 1: Wait for old process to exit ===\r\n"
                    ":WAIT_EXIT\r\n"
                    "tasklist /FI \"PID eq %PID%\" 2>NUL | find /I \"%PID%\" >NUL\r\n"
                    "if errorlevel 1 goto RUN_INSTALLER\r\n"
                    "set /A COUNT+=1\r\n"
                    "if %COUNT% GEQ %MAX_WAIT% goto RUN_INSTALLER\r\n"
                    "ping -n 2 127.0.0.1 >NUL\r\n"
                    "goto WAIT_EXIT\r\n"
                    "\r\n"
                    "rem === Phase 2: Run installer silently and wait ===\r\n"
                    ":RUN_INSTALLER\r\n"
                    "ping -n 2 127.0.0.1 >NUL\r\n"
                    "%INSTALLER% /SILENT /NORESTART /SUPPRESSMSGBOXES\r\n"
                    "\r\n"
                    "rem === Phase 3: Launch updated app via explorer ===\r\n"
                    "rem explorer.exe launches the app as if user double-clicked it,\r\n"
                    "rem ensuring correct working directory and environment for PyInstaller\r\n"
                    "ping -n 4 127.0.0.1 >NUL\r\n"
                    "explorer.exe %APP_EXE%\r\n"
                    "\r\n"
                    "rem === Cleanup ===\r\n"
                    f'del /f /q "{bat_path}"\r\n'
                )
                with open(bat_path, 'w', encoding='ascii') as f:
                    f.write(bat_content)

                # cmd.exe로 bat 실행:
                # - CREATE_NO_WINDOW: 콘솔 창 숨김 (UX 개선)
                # - DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: 부모(TunnelForge)
                #   종료 후에도 독립 실행 (main.py 복구 모드와 동일 패턴)
                subprocess.Popen(
                    ['cmd.exe', '/c', bat_path],
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        | subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                    ),
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    [self._downloaded_installer_path],
                    start_new_session=True
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
