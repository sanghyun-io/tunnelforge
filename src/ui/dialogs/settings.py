"""설정 관련 다이얼로그"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QRadioButton, QCheckBox,
                             QButtonGroup, QGroupBox, QLineEdit, QMessageBox)
from PyQt6.QtCore import Qt


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
        self._bot_mode = False  # init_ui에서 설정됨
        self.setWindowTitle("설정")
        self.init_ui()
        # 봇 모드에 따라 다이얼로그 크기 조정
        if self._bot_mode:
            self.setFixedSize(500, 280)
        else:
            self.setFixedSize(500, 420)

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

        # 봇 모드 확인
        self._bot_mode = self._check_bot_mode()

        # 자동 보고 활성화 체크박스
        self.chk_auto_report = QCheckBox("Export/Import 오류 시 자동으로 GitHub 이슈 생성")
        self.chk_auto_report.stateChanged.connect(self._on_auto_report_changed)
        github_layout.addWidget(self.chk_auto_report)

        # 설명 라벨
        if self._bot_mode:
            desc_label = QLabel(
                "오류 발생 시 자동으로 이슈를 생성하거나,\n"
                "유사한 이슈가 있으면 코멘트를 추가합니다.\n"
                "✅ 봇 인증이 설정되어 있습니다."
            )
            desc_label.setStyleSheet("color: #27ae60; font-size: 11px; margin-left: 20px;")
        else:
            desc_label = QLabel(
                "오류 발생 시 자동으로 이슈를 생성하거나,\n"
                "유사한 이슈가 있으면 코멘트를 추가합니다."
            )
            desc_label.setStyleSheet("color: #666; font-size: 11px; margin-left: 20px;")
        github_layout.addWidget(desc_label)

        # 사용자 토큰 입력 위젯들 (봇 모드가 아닐 때만 표시)
        self.manual_credentials_widget = QWidget()
        manual_layout = QVBoxLayout(self.manual_credentials_widget)
        manual_layout.setContentsMargins(0, 0, 0, 0)

        # GitHub 토큰 입력
        token_layout = QHBoxLayout()
        token_label = QLabel("Personal Access Token:")
        token_label.setFixedWidth(140)
        self.input_github_token = QLineEdit()
        self.input_github_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_github_token.setPlaceholderText("ghp_xxxxxxxxxxxx")
        token_layout.addWidget(token_label)
        token_layout.addWidget(self.input_github_token)
        manual_layout.addLayout(token_layout)

        # 리포지토리 입력
        repo_layout = QHBoxLayout()
        repo_label = QLabel("Repository:")
        repo_label.setFixedWidth(140)
        self.input_github_repo = QLineEdit()
        self.input_github_repo.setPlaceholderText("owner/repo (예: sanghyun-io/db-connector)")
        repo_layout.addWidget(repo_label)
        repo_layout.addWidget(self.input_github_repo)
        manual_layout.addLayout(repo_layout)

        # 토큰 안내
        token_help = QLabel(
            "토큰 발급: GitHub → Settings → Developer settings → Personal access tokens\n"
            "필요 권한: repo (이슈 생성/코멘트 추가)"
        )
        token_help.setStyleSheet("color: #888; font-size: 10px; margin-top: 5px;")
        manual_layout.addWidget(token_help)

        # 연결 테스트 버튼
        test_layout = QHBoxLayout()
        test_layout.addStretch()
        self.btn_test_github = QPushButton("연결 테스트")
        self.btn_test_github.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                padding: 4px 12px; border-radius: 3px; border: none;
            }
            QPushButton:hover { background-color: #229954; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_test_github.clicked.connect(self._test_github_connection)
        test_layout.addWidget(self.btn_test_github)
        manual_layout.addLayout(test_layout)

        github_layout.addWidget(self.manual_credentials_widget)

        # 봇 모드면 수동 입력 숨김
        if self._bot_mode:
            self.manual_credentials_widget.setVisible(False)

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

        # GitHub 설정 저장
        auto_report = self.chk_auto_report.isChecked()

        # 봇 모드가 아닐 때만 수동 입력 값 체크
        if not self._bot_mode:
            github_token = self.input_github_token.text().strip()
            github_repo = self.input_github_repo.text().strip()

            # 자동 보고 활성화 시 필수값 체크
            if auto_report and (not github_token or not github_repo):
                QMessageBox.warning(
                    self, "설정 오류",
                    "GitHub 자동 이슈 보고를 활성화하려면\n토큰과 리포지토리를 모두 입력해야 합니다."
                )
                return

            self.config_mgr.set_app_setting('github_token', github_token)
            self.config_mgr.set_app_setting('github_repo', github_repo)

        self.config_mgr.set_app_setting('github_auto_report', auto_report)
        self.accept()

    def _check_bot_mode(self) -> bool:
        """봇 모드 사용 가능 여부 확인"""
        try:
            from src.core.bot_credentials import is_bot_configured
            return is_bot_configured()
        except ImportError:
            return False

    def _load_github_settings(self):
        """GitHub 설정 로드"""
        auto_report = self.config_mgr.get_app_setting('github_auto_report', False)

        self.chk_auto_report.setChecked(auto_report)

        # 봇 모드가 아닐 때만 수동 입력 값 로드
        if not self._bot_mode:
            github_token = self.config_mgr.get_app_setting('github_token', '')
            github_repo = self.config_mgr.get_app_setting('github_repo', '')
            self.input_github_token.setText(github_token)
            self.input_github_repo.setText(github_repo)
            # 비활성화 상태면 입력 필드 비활성화
            self._on_auto_report_changed(Qt.CheckState.Checked.value if auto_report else Qt.CheckState.Unchecked.value)

    def _on_auto_report_changed(self, state):
        """자동 보고 체크박스 상태 변경 시"""
        # 봇 모드면 수동 입력 필드 상태 변경 불필요
        if self._bot_mode:
            return

        enabled = state == Qt.CheckState.Checked.value
        self.input_github_token.setEnabled(enabled)
        self.input_github_repo.setEnabled(enabled)
        self.btn_test_github.setEnabled(enabled)

    def _test_github_connection(self):
        """GitHub 연결 테스트"""
        token = self.input_github_token.text().strip()
        repo = self.input_github_repo.text().strip()

        if not token or not repo:
            QMessageBox.warning(self, "입력 오류", "토큰과 리포지토리를 모두 입력하세요.")
            return

        try:
            from src.core.github_issue_reporter import GitHubIssueReporter

            available, msg = GitHubIssueReporter.check_available()
            if not available:
                QMessageBox.warning(self, "라이브러리 오류", msg)
                return

            reporter = GitHubIssueReporter(token, repo)

            # 리포지토리 정보 조회로 연결 테스트
            import requests
            url = f"{reporter.GITHUB_API_BASE}/repos/{repo}"
            response = requests.get(url, headers=reporter._headers, timeout=10)

            if response.status_code == 200:
                repo_info = response.json()
                repo_name = repo_info.get('full_name', repo)
                QMessageBox.information(
                    self, "연결 성공",
                    f"✅ GitHub 연결 성공!\n\n리포지토리: {repo_name}"
                )
            elif response.status_code == 401:
                QMessageBox.warning(self, "인증 실패", "❌ 토큰이 유효하지 않습니다.")
            elif response.status_code == 404:
                QMessageBox.warning(self, "리포지토리 없음", "❌ 리포지토리를 찾을 수 없습니다.")
            else:
                QMessageBox.warning(
                    self, "연결 실패",
                    f"❌ 연결 실패: HTTP {response.status_code}"
                )

        except ImportError:
            QMessageBox.warning(
                self, "라이브러리 오류",
                "requests 라이브러리가 설치되지 않았습니다.\npip install requests"
            )
        except Exception as e:
            QMessageBox.warning(self, "연결 실패", f"❌ 오류: {str(e)}")
