"""터널 연결 설정 다이얼로그"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                             QDialogButtonBox, QFileDialog, QPushButton,
                             QHBoxLayout, QSpinBox, QLabel, QMessageBox, QApplication,
                             QRadioButton, QCheckBox, QButtonGroup, QGroupBox, QWidget,
                             QComboBox, QMenu)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
import uuid

from src.core.logger import get_logger
from src.ui.styles import ButtonStyles, LabelStyles
from src.ui.workers.test_worker import ConnectionTestWorker, TestType
from src.ui.dialogs.test_dialogs import TestProgressDialog

logger = get_logger(__name__)


class _RunningTestProgressDialog(TestProgressDialog):
    """테스트 실행 중에는 ESC/닫기로 dismiss될 수 없는 진행 다이얼로그.

    ConnectionTestWorker(QThread) 실행 중 reject()가 호출되면(ESC 키,
    Alt+F4 등 QDialog.closeEvent도 내부적으로 reject()를 호출한다)
    exec()가 즉시 반환되어 로컬 worker 참조가 사라지고, 실행 중인 QThread의
    마지막 파이썬 참조가 사라져 크래시로 이어진다(WP-3.9 Finding 1).
    allow_dismiss()가 호출되기 전까지는 reject 경로를 막는다.
    """

    def __init__(self, parent, title: str = "연결 테스트"):
        super().__init__(parent, title)
        self._dismissable = False

    def allow_dismiss(self):
        """워커의 내장 QThread.finished()가 발화한 뒤에만 호출해야 한다."""
        self._dismissable = True

    def reject(self):
        if not self._dismissable:
            return
        super().reject()


class _TempCredentials:
    """DB 테스트용 임시 자격 증명 제공자.

    ConfigManager와 동일한 get_tunnel_credentials(tunnel_id) 인터페이스를
    제공해 ConnectionTestWorker가 실제 ConfigManager와 구분 없이 사용할 수
    있게 한다. _test_db_only/_test_integrated에 각각 인라인으로 중복
    정의되어 있던 동일한 클래스를 하나로 통합했다(WP-3.9 Finding 2).
    """

    def __init__(self, user, password, encrypted_password, encryptor):
        self._user = user
        self._password = password
        self._encrypted = encrypted_password
        self._encryptor = encryptor

    def get_tunnel_credentials(self, tunnel_id):
        if self._password:
            return self._user, self._password
        elif self._encrypted and self._encryptor:
            return self._user, self._encryptor.decrypt(self._encrypted)
        return self._user, None


class TunnelConfigDialog(QDialog):
    def __init__(self, parent=None, tunnel_data=None, tunnel_engine=None):
        super().__init__(parent)
        self.setWindowTitle("터널 연결 설정")
        self.resize(500, 450)

        # 엔진 인스턴스 저장 (테스트 연결용)
        self.engine = tunnel_engine

        # 현재 실행 중인 ConnectionTestWorker(QThread) 참조.
        # 내장 QThread.finished()가 발화하기 전까지는 절대 None으로 해제하지
        # 않는다 (WP-3.9 Finding 1).
        self._test_worker = None

        # 수정 모드일 경우 기존 데이터, 아니면 빈 딕셔너리
        self.tunnel_data = tunnel_data or {}
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        self._build_basic_info_section(form_layout)
        self._build_connection_mode_section(form_layout)
        self._build_bastion_section(form_layout)
        self._build_target_db_section(form_layout)
        self._build_environment_section(form_layout)
        self._build_local_section(form_layout)
        self._build_auth_section(form_layout)

        layout.addLayout(form_layout)

        # 초기 모드에 따라 UI 상태 설정
        self.on_mode_changed()

        self._build_footer_section(layout)

    def _build_basic_info_section(self, form_layout: QFormLayout):
        self.input_name = QLineEdit(self.tunnel_data.get('name', ''))
        self.input_name.setPlaceholderText("예: Project A (Master)")
        form_layout.addRow("이름(별칭):", self.input_name)

    def _build_connection_mode_section(self, form_layout: QFormLayout):
        lbl_mode = QLabel("--- 연결 방식 ---")
        lbl_mode.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(lbl_mode)

        self.mode_group = QButtonGroup(self)
        mode_layout = QHBoxLayout()

        self.radio_ssh_tunnel = QRadioButton("SSH 터널 (Bastion 경유)")
        self.radio_direct = QRadioButton("직접 연결 (로컬/외부 DB)")

        self.mode_group.addButton(self.radio_ssh_tunnel)
        self.mode_group.addButton(self.radio_direct)

        # 기존 데이터에서 모드 확인
        current_mode = self.tunnel_data.get('connection_mode', 'ssh_tunnel')
        if current_mode == 'direct':
            self.radio_direct.setChecked(True)
        else:
            self.radio_ssh_tunnel.setChecked(True)

        mode_layout.addWidget(self.radio_ssh_tunnel)
        mode_layout.addWidget(self.radio_direct)
        form_layout.addRow(mode_layout)

        # 모드 변경 시 UI 업데이트
        self.radio_ssh_tunnel.toggled.connect(self.on_mode_changed)
        self.radio_direct.toggled.connect(self.on_mode_changed)

    def _build_bastion_section(self, form_layout: QFormLayout):
        self.lbl_bastion = QLabel("--- Bastion Host (중계 서버) ---")
        self.lbl_bastion.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(self.lbl_bastion)

        self.input_bastion_host = QLineEdit(self.tunnel_data.get('bastion_host', ''))
        self.input_bastion_host.setPlaceholderText("예: 1.2.3.4 또는 ec2-xxx...")

        self.input_bastion_port = QSpinBox()
        self.input_bastion_port.setRange(1, 65535)
        self.input_bastion_port.setValue(int(self.tunnel_data.get('bastion_port', 22)))

        self.input_bastion_user = QLineEdit(self.tunnel_data.get('bastion_user', 'ec2-user'))

        # 키 파일 선택
        self.input_bastion_key = QLineEdit(self.tunnel_data.get('bastion_key', ''))
        self.input_bastion_key.setPlaceholderText("C:/Users/.../key.pem")
        self.btn_key_file = QPushButton("파일 찾기")
        self.btn_key_file.clicked.connect(self.select_key_file)

        self.key_layout_widget = QWidget()
        key_layout = QHBoxLayout(self.key_layout_widget)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.input_bastion_key)
        key_layout.addWidget(self.btn_key_file)

        # Bastion 필드 라벨 저장 (토글용)
        self.lbl_bastion_host = QLabel("Host 주소:")
        self.lbl_bastion_port = QLabel("Port:")
        self.lbl_bastion_user = QLabel("SSH User:")
        self.lbl_bastion_key = QLabel("SSH Key:")

        form_layout.addRow(self.lbl_bastion_host, self.input_bastion_host)
        form_layout.addRow(self.lbl_bastion_port, self.input_bastion_port)
        form_layout.addRow(self.lbl_bastion_user, self.input_bastion_user)
        form_layout.addRow(self.lbl_bastion_key, self.key_layout_widget)

        self.bastion_templates = self._load_bastion_templates()
        self.btn_copy_bastion = QPushButton("다른 연결 복사")
        self.btn_copy_bastion.setToolTip("기존 연결의 Bastion Host, Port, SSH User, SSH Key를 복사합니다")
        self.btn_copy_bastion.clicked.connect(self._show_bastion_copy_menu)
        self.btn_copy_bastion.setEnabled(bool(self.bastion_templates))
        form_layout.addRow("", self.btn_copy_bastion)

    def _build_target_db_section(self, form_layout: QFormLayout):
        lbl_remote = QLabel("--- Target DB (목적지) ---")
        lbl_remote.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(lbl_remote)

        self.input_remote_host = QLineEdit(self.tunnel_data.get('remote_host', ''))
        self.input_remote_host.setPlaceholderText("예: my-rds.ap-northeast-2.rds.amazonaws.com")

        self.input_remote_port = QSpinBox()
        self.input_remote_port.setRange(1, 65535)
        self.input_remote_port.setValue(int(self.tunnel_data.get('remote_port', 3306)))

        form_layout.addRow("Endpoint:", self.input_remote_host)
        form_layout.addRow("DB Port:", self.input_remote_port)

        self.combo_db_engine = QComboBox()
        self.combo_db_engine.addItem("DB Engine 선택", None)
        self.combo_db_engine.addItem("MySQL", "mysql")
        self.combo_db_engine.addItem("PostgreSQL", "postgresql")
        engine_index = self.combo_db_engine.findData(self.tunnel_data.get('db_engine'))
        self.combo_db_engine.setCurrentIndex(engine_index if engine_index >= 0 else 0)
        form_layout.addRow("DB Engine:", self.combo_db_engine)

        # 기본 스키마 (선택사항)
        self.input_default_database = QLineEdit(self.tunnel_data.get('default_database', ''))
        self.input_default_database.setPlaceholderText("(PostgreSQL 선택사항) 예: postgres 또는 appdb")
        form_layout.addRow("기본 DB 이름:", self.input_default_database)

        self.input_default_schema = QLineEdit(self.tunnel_data.get('default_schema', ''))
        self.input_default_schema.setPlaceholderText("(선택사항) MySQL DB명 또는 PostgreSQL schema명")
        form_layout.addRow("기본 스키마:", self.input_default_schema)

    def _build_environment_section(self, form_layout: QFormLayout):
        lbl_env = QLabel("--- 환경 설정 ---")
        lbl_env.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(lbl_env)

        self.combo_environment = QComboBox()
        self.combo_environment.addItem("(미설정)", None)
        self.combo_environment.addItem("🔴 Production", "production")
        self.combo_environment.addItem("🟠 Staging", "staging")
        self.combo_environment.addItem("🟢 Development", "development")
        self.combo_environment.setToolTip(
            "Production: 위험 작업 시 스키마명 직접 입력 필요\n"
            "Staging: 위험 작업 시 확인 다이얼로그 표시\n"
            "Development: 확인 없이 바로 실행"
        )
        env_index = self.combo_environment.findData(self.tunnel_data.get('environment'))
        self.combo_environment.setCurrentIndex(env_index if env_index >= 0 else 0)
        form_layout.addRow("환경:", self.combo_environment)

    def _build_local_section(self, form_layout: QFormLayout):
        self.lbl_local = QLabel("--- Local (내 컴퓨터) ---")
        self.lbl_local.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(self.lbl_local)

        self.input_local_port = QSpinBox()
        self.input_local_port.setRange(1, 65535)
        self.input_local_port.setValue(int(self.tunnel_data.get('local_port', 3308)))
        self.lbl_local_port = QLabel("Local Bind Port:")
        form_layout.addRow(self.lbl_local_port, self.input_local_port)

        # 터널 테스트 버튼 - 중앙화된 스타일 사용
        self.btn_tunnel_test = QPushButton("🔌 터널 테스트")
        self.btn_tunnel_test.setStyleSheet(ButtonStyles.TEST)
        self.btn_tunnel_test.clicked.connect(self._test_tunnel_only)
        form_layout.addRow("", self.btn_tunnel_test)

    def _build_auth_section(self, form_layout: QFormLayout):
        lbl_mysql = QLabel("--- DB 인증 정보 (선택 사항) ---")
        lbl_mysql.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(lbl_mysql)

        self.chk_save_credentials = QCheckBox("DB 자격 증명 저장")
        self.chk_save_credentials.setToolTip("암호화하여 저장합니다")
        self.chk_save_credentials.toggled.connect(self._on_save_credentials_toggled)
        form_layout.addRow(self.chk_save_credentials)

        self.input_db_user = QLineEdit(self.tunnel_data.get('db_user', ''))
        self.input_db_user.setPlaceholderText("DB 사용자명")
        self.input_db_user.setEnabled(False)
        form_layout.addRow("DB User:", self.input_db_user)

        self.input_db_password = QLineEdit()
        self.input_db_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_db_password.setPlaceholderText("DB 비밀번호")
        self.input_db_password.setEnabled(False)
        form_layout.addRow("DB Password:", self.input_db_password)

        # DB 인증 테스트 버튼 - 중앙화된 스타일 사용
        self.btn_db_test = QPushButton("🔐 DB 인증 테스트")
        self.btn_db_test.setStyleSheet(ButtonStyles.TEST)
        self.btn_db_test.setEnabled(False)  # 체크박스 연동
        self.btn_db_test.clicked.connect(self._test_db_only)
        form_layout.addRow("", self.btn_db_test)

        # 기존 자격 증명 있으면 체크
        if self.tunnel_data.get('db_user'):
            self.chk_save_credentials.setChecked(True)
            if self.tunnel_data.get('db_password_encrypted'):
                self.input_db_password.setPlaceholderText("(저장됨 - 변경시 새로 입력)")

    def _build_footer_section(self, layout: QVBoxLayout):
        self.btn_integrated_test = QPushButton("🚀 통합 테스트")
        self.btn_integrated_test.setStyleSheet(ButtonStyles.WARNING)
        self.btn_integrated_test.clicked.connect(self._test_integrated)
        layout.addWidget(self.btn_integrated_test)

        layout.addSpacing(10)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def select_key_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "SSH Key 파일 선택", "", "Key Files (*.pem *.ppk);;All Files (*)")
        if filename:
            self.input_bastion_key.setText(filename)

    def on_mode_changed(self):
        """연결 모드 변경 시 UI 업데이트"""
        is_ssh_mode = self.radio_ssh_tunnel.isChecked()

        # Bastion 관련 필드 토글
        bastion_widgets = [
            self.lbl_bastion, self.lbl_bastion_host, self.input_bastion_host,
            self.lbl_bastion_port, self.input_bastion_port,
            self.lbl_bastion_user, self.input_bastion_user,
            self.lbl_bastion_key, self.key_layout_widget,
            self.btn_copy_bastion
        ]
        for widget in bastion_widgets:
            widget.setEnabled(is_ssh_mode)
        self.btn_copy_bastion.setEnabled(is_ssh_mode and bool(self.bastion_templates))

        # Local Port 토글
        local_widgets = [self.lbl_local, self.lbl_local_port, self.input_local_port]
        for widget in local_widgets:
            widget.setEnabled(is_ssh_mode)

    def _on_save_credentials_toggled(self, checked):
        """MySQL 자격 증명 저장 체크박스 토글"""
        self.input_db_user.setEnabled(checked)
        self.input_db_password.setEnabled(checked)
        self.btn_db_test.setEnabled(checked)
        if not checked:
            self.input_db_user.clear()
            self.input_db_password.clear()

    def _load_bastion_templates(self):
        current_id = self.tunnel_data.get('id')
        templates = []
        for tunnel in self._available_tunnels():
            if tunnel.get('id') == current_id:
                continue
            if tunnel.get('connection_mode', 'ssh_tunnel') == 'direct':
                continue
            if not tunnel.get('bastion_host'):
                continue
            templates.append(tunnel)
        return templates

    def _available_tunnels(self):
        parent = self.parent()
        if parent is not None and hasattr(parent, 'tunnels'):
            return parent.tunnels
        if parent is not None and hasattr(parent, 'config_mgr'):
            try:
                return parent.config_mgr.load_config().get('tunnels', [])
            except Exception:
                logger.exception("failed to load tunnel list for bastion templates")
                return []
        return []

    def _show_bastion_copy_menu(self):
        if not self.bastion_templates:
            QMessageBox.information(self, "다른 연결 복사", "복사할 수 있는 SSH 터널 연결이 없습니다.")
            return

        menu = QMenu(self)
        for tunnel in self.bastion_templates:
            action = QAction(tunnel.get('name', '이름 없음'), menu)
            action.triggered.connect(lambda checked=False, t=tunnel: self._copy_bastion_from_tunnel(t))
            menu.addAction(action)
        menu.exec(self.btn_copy_bastion.mapToGlobal(self.btn_copy_bastion.rect().bottomLeft()))

    def _copy_bastion_from_tunnel(self, tunnel):
        self.radio_ssh_tunnel.setChecked(True)
        self.input_bastion_host.setText(tunnel.get('bastion_host', ''))
        self.input_bastion_port.setValue(int(tunnel.get('bastion_port', 22)))
        self.input_bastion_user.setText(tunnel.get('bastion_user', ''))
        self.input_bastion_key.setText(tunnel.get('bastion_key', ''))
        self.on_mode_changed()

    def get_data(self):
        """입력된 폼 데이터를 딕셔너리로 반환"""
        environment = self.combo_environment.currentData()

        is_direct = self.radio_direct.isChecked()
        remote_host = self.input_remote_host.text().strip()
        if is_direct and not remote_host:
            remote_host = "127.0.0.1"

        data = {
            # ID가 없으면 새로 생성 (신규 추가), 있으면 기존 ID 유지 (수정)
            "id": self.tunnel_data.get('id', str(uuid.uuid4())),
            "name": self.input_name.text(),
            "connection_mode": "direct" if is_direct else "ssh_tunnel",
            "bastion_host": self.input_bastion_host.text(),
            "bastion_port": self.input_bastion_port.value(),
            "bastion_user": self.input_bastion_user.text(),
            "bastion_key": self.input_bastion_key.text(),
            "remote_host": remote_host,
            "remote_port": self.input_remote_port.value(),
            "local_port": self.input_local_port.value(),
            "db_engine": self.combo_db_engine.currentData(),
            "default_database": self.input_default_database.text().strip() or None,
            "default_schema": self.input_default_schema.text().strip() or None,
            "environment": environment
        }

        # MySQL 자격 증명 (체크된 경우에만)
        if self.chk_save_credentials.isChecked():
            data['db_user'] = self.input_db_user.text()
            # 평문 비밀번호는 임시 필드로 전달 (main_window에서 암호화)
            data['_db_password_plain'] = self.input_db_password.text()
            # 기존 암호화된 비밀번호 유지 (수정 시 새로 입력하지 않으면)
            if not self.input_db_password.text() and self.tunnel_data.get('db_password_encrypted'):
                data['db_password_encrypted'] = self.tunnel_data.get('db_password_encrypted')

        return data

    def accept(self):
        if not self.combo_db_engine.currentData():
            QMessageBox.warning(self, "필수 항목 누락", "DB Engine을 선택해주세요.\nMySQL 또는 PostgreSQL을 명시해야 합니다.")
            return
        super().accept()

    def _test_tunnel_only(self):
        """SSH 터널만 테스트 (Local 포트까지 확인)"""
        if not self.engine:
            QMessageBox.critical(self, "오류", "터널 엔진이 초기화되지 않았습니다.")
            return

        temp_config = self.get_data()

        # 직접 연결 모드면 터널 테스트 불필요
        if temp_config.get('connection_mode') == 'direct':
            QMessageBox.information(self, "터널 테스트", "직접 연결 모드는 SSH 터널 테스트가 필요하지 않습니다.\nDB 인증 테스트 또는 통합 테스트를 실행해주세요.")
            return

        # SSH 터널 모드 필수 필드 검증
        bastion_host = temp_config.get('bastion_host', '').strip()
        bastion_user = temp_config.get('bastion_user', '').strip()
        remote_host = temp_config.get('remote_host', '').strip()

        missing_fields = []
        if not bastion_host:
            missing_fields.append("SSH 호스트")
        if not bastion_user:
            missing_fields.append("SSH 사용자")
        if not remote_host:
            missing_fields.append("Target DB (Endpoint)")

        if missing_fields:
            QMessageBox.warning(
                self,
                "필수 필드 누락",
                "다음 필드를 입력해주세요:\n\n• " + "\n• ".join(missing_fields)
            )
            return

        self._run_test(
            TestType.TUNNEL_ONLY, temp_config, None,
            f"터널 테스트 - {temp_config.get('name', 'Unknown')}"
        )

    def _test_db_only(self):
        """DB 인증만 테스트 (터널 경유)"""
        if not self.engine:
            QMessageBox.critical(self, "오류", "터널 엔진이 초기화되지 않았습니다.")
            return

        temp_config = self.get_data()
        if not temp_config.get('db_engine'):
            QMessageBox.warning(self, "필수 항목 누락", "DB Engine을 먼저 선택해주세요.")
            return

        # DB 자격 증명 확인
        db_user = self.input_db_user.text()
        db_password = self.input_db_password.text()

        if not db_user:
            QMessageBox.warning(self, "경고", "DB 사용자명을 입력해주세요.")
            return

        # 비밀번호가 없고 기존 암호화된 비밀번호도 없는 경우
        if not db_password and not self.tunnel_data.get('db_password_encrypted'):
            QMessageBox.warning(self, "경고", "DB 비밀번호를 입력해주세요.")
            return

        temp_config_mgr = _TempCredentials(
            db_user, db_password,
            self.tunnel_data.get('db_password_encrypted'),
            self._get_parent_encryptor()
        )

        self._run_test(
            TestType.DB_ONLY, temp_config, temp_config_mgr,
            f"DB 인증 테스트 - {temp_config.get('name', 'Unknown')}"
        )

    def _test_integrated(self):
        """통합 테스트 (터널 + DB)"""
        if not self.engine:
            QMessageBox.critical(self, "오류", "터널 엔진이 초기화되지 않았습니다.")
            return

        temp_config = self.get_data()
        if not temp_config.get('db_engine'):
            QMessageBox.warning(self, "필수 항목 누락", "DB Engine을 먼저 선택해주세요.")
            return

        # DB 자격 증명 확인 (선택 사항)
        db_user = self.input_db_user.text() if self.chk_save_credentials.isChecked() else None
        db_password = self.input_db_password.text() if self.chk_save_credentials.isChecked() else None

        temp_config_mgr = _TempCredentials(
            db_user, db_password,
            self.tunnel_data.get('db_password_encrypted') if db_user else None,
            self._get_parent_encryptor()
        )

        self._run_test(
            TestType.INTEGRATED, temp_config, temp_config_mgr,
            f"통합 테스트 - {temp_config.get('name', 'Unknown')}"
        )

    def _get_parent_encryptor(self):
        if hasattr(self.parent(), 'config_mgr'):
            return self.parent().config_mgr.encryptor
        return None

    def _run_test(self, test_type: TestType, temp_config: dict, config_mgr, title: str):
        dialog = self._start_connection_test(test_type, temp_config, config_mgr, title)
        dialog.exec()

    def _start_connection_test(self, test_type: TestType, temp_config: dict,
                                config_mgr, title: str) -> "_RunningTestProgressDialog":
        """ConnectionTestWorker를 생성해 진행 다이얼로그와 연결한 뒤 시작한다.

        worker는 self._test_worker에 보관해 로컬 변수 스코프가 끝나도 참조가
        사라지지 않게 하고, 내장 QThread.finished()가 발화하기 전까지는
        해제하지 않는다. 반환된 dialog는 호출자가 exec()해야 한다
        (WP-3.9 Finding 1).
        """
        dialog = _RunningTestProgressDialog(self, title)
        worker = ConnectionTestWorker(test_type, temp_config, self.engine, config_mgr)
        self._test_worker = worker

        worker.progress.connect(dialog.update_progress)
        worker.test_finished.connect(lambda s, m: self._on_test_result(dialog, s, m))
        # 내장 QThread.finished()(무인자) — 실제로 스레드가 정지한 뒤에만 발화.
        # 이 시점에야 dialog dismiss를 허용하고 worker 참조를 해제한다.
        worker.finished.connect(lambda: self._on_test_worker_thread_finished(dialog))

        worker.start()
        return dialog

    def _on_test_result(self, dialog, success: bool, message: str):
        dialog.show_result(success, message)

    def _on_test_worker_thread_finished(self, dialog):
        """내장 QThread.finished() 핸들러. worker가 실제로 정지한 뒤 호출된다."""
        dialog.allow_dismiss()
        self._test_worker = None
