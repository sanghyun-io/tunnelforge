"""설정 관련 다이얼로그"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QRadioButton, QCheckBox,
                             QButtonGroup, QGroupBox, QMessageBox)


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


class SettingsDialog(QDialog):
    """앱 설정 다이얼로그"""
    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_mgr = config_manager
        self.setWindowTitle("설정")
        self.setFixedSize(500, 280)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

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
        github_layout.addWidget(self.chk_auto_report)

        # GitHub App 설정 상태에 따른 설명
        if self._github_app_configured:
            desc_label = QLabel(
                "오류 발생 시 자동으로 이슈를 생성하거나,\n"
                "유사한 이슈가 있으면 코멘트를 추가합니다.\n"
                "✅ GitHub App이 설정되어 있습니다."
            )
            desc_label.setStyleSheet("color: #27ae60; font-size: 11px; margin-left: 20px;")

            # 연결 테스트 버튼
            test_layout = QHBoxLayout()
            btn_test = QPushButton("연결 테스트")
            btn_test.setStyleSheet("""
                QPushButton {
                    background-color: #95a5a6; color: white;
                    padding: 4px 12px; border-radius: 4px; border: none;
                    font-size: 11px;
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
                "환경변수 또는 내장 설정이 필요합니다.\n"
                "(GITHUB_APP_SETUP.md 참조)"
            )
            desc_label.setStyleSheet("color: #e74c3c; font-size: 11px; margin-left: 20px;")
            self.chk_auto_report.setEnabled(False)

        github_layout.addWidget(desc_label)
        layout.addWidget(github_group)

        # GitHub 설정 로드
        self._load_github_settings()

        layout.addStretch()

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

        self.accept()

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
