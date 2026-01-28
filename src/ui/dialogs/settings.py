"""설정 관련 다이얼로그"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QRadioButton, QCheckBox,
                             QButtonGroup, QGroupBox, QMessageBox, QTabWidget,
                             QWidget, QTextBrowser, QSizePolicy)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QCursor
from PyQt6.QtCore import QUrl
from src.version import __version__, __app_name__, GITHUB_OWNER, GITHUB_REPO


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

    def run(self):
        try:
            from src.core.update_checker import UpdateChecker
            checker = UpdateChecker()
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

        layout.addStretch()

        return tab

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

    def save_settings(self):
        """설정 저장"""
        if self.radio_minimize.isChecked():
            action = 'minimize'
        elif self.radio_exit.isChecked():
            action = 'exit'
        else:
            action = 'ask'

        self.config_mgr.set_app_setting('close_action', action)

        # GitHub 자동 보고 설정 저장
        auto_report = self.chk_auto_report.isChecked()
        self.config_mgr.set_app_setting('github_auto_report', auto_report)

        # 자동 업데이트 확인 설정 저장
        auto_update_check = self.chk_auto_update.isChecked()
        self.config_mgr.set_app_setting('auto_update_check', auto_update_check)

        self.accept()

    def _check_for_updates(self):
        """업데이트 확인"""
        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText("확인 중...")
        self.update_status_label.setHtml(
            '<div style="color: #3498db; font-size: 12px;">업데이트를 확인하는 중입니다...</div>'
        )

        # 백그라운드 스레드에서 확인
        self._update_checker_thread = UpdateCheckerThread()
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
            return

        if needs_update:
            self.update_status_label.setHtml(
                f'<div style="color: #27ae60; font-size: 12px;">'
                f'✅ 새로운 버전 {latest_version}이 사용 가능합니다!<br>'
                f'<a href="{download_url}" style="color: #3498db;">다운로드 페이지로 이동</a>'
                f'</div>'
            )
        else:
            self.update_status_label.setHtml(
                f'<div style="color: #27ae60; font-size: 12px;">'
                f'✅ 최신 버전({__version__})을 사용하고 있습니다.'
                f'</div>'
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
