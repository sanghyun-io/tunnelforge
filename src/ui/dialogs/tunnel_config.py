"""터널 연결 설정 다이얼로그"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                             QDialogButtonBox, QFileDialog, QPushButton,
                             QHBoxLayout, QSpinBox, QLabel, QMessageBox, QApplication,
                             QRadioButton, QCheckBox, QButtonGroup, QGroupBox, QWidget)
from PyQt6.QtCore import Qt
import uuid

from src.ui.styles import ButtonStyles, LabelStyles
from src.ui.workers.test_worker import ConnectionTestWorker, TestType
from src.ui.dialogs.test_dialogs import TestProgressDialog


class TunnelConfigDialog(QDialog):
    def __init__(self, parent=None, tunnel_data=None, tunnel_engine=None):
        super().__init__(parent)
        self.setWindowTitle("터널 연결 설정")
        self.resize(500, 450)

        # 엔진 인스턴스 저장 (테스트 연결용)
        self.engine = tunnel_engine

        # 수정 모드일 경우 기존 데이터, 아니면 빈 딕셔너리
        self.tunnel_data = tunnel_data or {}
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        # --- 1. 기본 정보 ---
        self.input_name = QLineEdit(self.tunnel_data.get('name', ''))
        self.input_name.setPlaceholderText("예: Project A (Master)")
        form_layout.addRow("이름(별칭):", self.input_name)

        # --- 연결 방식 선택 ---
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

        # --- 2. Bastion 서버 정보 ---
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

        # --- 3. RDS/Remote 정보 ---
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

        # 기본 스키마 (선택사항)
        self.input_default_schema = QLineEdit(self.tunnel_data.get('default_schema', ''))
        self.input_default_schema.setPlaceholderText("(선택사항) 예: my_database")
        form_layout.addRow("기본 스키마:", self.input_default_schema)

        # --- 4. 로컬 설정 ---
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

        # --- 5. MySQL 인증 정보 (선택 사항) ---
        lbl_mysql = QLabel("--- MySQL 인증 정보 (선택 사항) ---")
        lbl_mysql.setStyleSheet(LabelStyles.SECTION_HEADER)
        form_layout.addRow(lbl_mysql)

        self.chk_save_credentials = QCheckBox("MySQL 자격 증명 저장")
        self.chk_save_credentials.setToolTip("암호화하여 저장합니다")
        self.chk_save_credentials.toggled.connect(self._on_save_credentials_toggled)
        form_layout.addRow(self.chk_save_credentials)

        self.input_db_user = QLineEdit(self.tunnel_data.get('db_user', ''))
        self.input_db_user.setPlaceholderText("MySQL 사용자명")
        self.input_db_user.setEnabled(False)
        form_layout.addRow("DB User:", self.input_db_user)

        self.input_db_password = QLineEdit()
        self.input_db_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_db_password.setPlaceholderText("MySQL 비밀번호")
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

        layout.addLayout(form_layout)

        # 초기 모드에 따라 UI 상태 설정
        self.on_mode_changed()

        # --- 하단 버튼 (통합 테스트 & 저장/취소) ---

        # 통합 테스트 버튼 - 중앙화된 스타일 사용
        self.btn_integrated_test = QPushButton("🚀 통합 테스트")
        self.btn_integrated_test.setStyleSheet(ButtonStyles.WARNING)
        self.btn_integrated_test.clicked.connect(self._test_integrated)
        layout.addWidget(self.btn_integrated_test)

        # 구분 공백
        layout.addSpacing(10)

        # 기본 버튼 (저장/취소)
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
            self.lbl_bastion_key, self.key_layout_widget
        ]
        for widget in bastion_widgets:
            widget.setEnabled(is_ssh_mode)

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

    def get_data(self):
        """입력된 폼 데이터를 딕셔너리로 반환"""
        data = {
            # ID가 없으면 새로 생성 (신규 추가), 있으면 기존 ID 유지 (수정)
            "id": self.tunnel_data.get('id', str(uuid.uuid4())),
            "name": self.input_name.text(),
            "connection_mode": "direct" if self.radio_direct.isChecked() else "ssh_tunnel",
            "bastion_host": self.input_bastion_host.text(),
            "bastion_port": self.input_bastion_port.value(),
            "bastion_user": self.input_bastion_user.text(),
            "bastion_key": self.input_bastion_key.text(),
            "remote_host": self.input_remote_host.text(),
            "remote_port": self.input_remote_port.value(),
            "local_port": self.input_local_port.value(),
            "default_schema": self.input_default_schema.text().strip() or None
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

    def _test_tunnel_only(self):
        """SSH 터널만 테스트 (Local 포트까지 확인)"""
        if not self.engine:
            QMessageBox.critical(self, "오류", "터널 엔진이 초기화되지 않았습니다.")
            return

        temp_config = self.get_data()

        # 직접 연결 모드면 터널 테스트 불필요
        if temp_config.get('connection_mode') == 'direct':
            QMessageBox.information(self, "알림", "직접 연결 모드에서는 터널 테스트가 필요하지 않습니다.")
            return

        # SSH 터널 모드 필수 필드 검증
        ssh_host = temp_config.get('ssh_host', '').strip()
        ssh_user = temp_config.get('ssh_user', '').strip()
        remote_host = temp_config.get('remote_host', '').strip()

        missing_fields = []
        if not ssh_host:
            missing_fields.append("SSH 호스트")
        if not ssh_user:
            missing_fields.append("SSH 사용자")
        if not remote_host:
            missing_fields.append("Target DB (Endpoint)")

        if missing_fields:
            QMessageBox.warning(
                self,
                "필수 필드 누락",
                f"다음 필드를 입력해주세요:\n\n• " + "\n• ".join(missing_fields)
            )
            return

        dialog = TestProgressDialog(self, f"터널 테스트 - {temp_config.get('name', 'Unknown')}")
        worker = ConnectionTestWorker(TestType.TUNNEL_ONLY, temp_config, self.engine, None)
        worker.progress.connect(dialog.update_progress)
        worker.finished.connect(lambda s, m: dialog.show_result(s, m))
        worker.start()
        dialog.exec()

    def _test_db_only(self):
        """DB 인증만 테스트 (터널 경유)"""
        if not self.engine:
            QMessageBox.critical(self, "오류", "터널 엔진이 초기화되지 않았습니다.")
            return

        temp_config = self.get_data()

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

        dialog = TestProgressDialog(self, f"DB 인증 테스트 - {temp_config.get('name', 'Unknown')}")

        # DB 테스트용 임시 ConfigManager 생성 (현재 입력값 사용)
        class TempConfigManager:
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

        # 부모 창(main_window)에서 encryptor 가져오기
        encryptor = None
        if hasattr(self.parent(), 'config_mgr'):
            encryptor = self.parent().config_mgr.encryptor

        temp_config_mgr = TempConfigManager(
            db_user, db_password,
            self.tunnel_data.get('db_password_encrypted'),
            encryptor
        )

        worker = ConnectionTestWorker(TestType.DB_ONLY, temp_config, self.engine, temp_config_mgr)
        worker.progress.connect(dialog.update_progress)
        worker.finished.connect(lambda s, m: dialog.show_result(s, m))
        worker.start()
        dialog.exec()

    def _test_integrated(self):
        """통합 테스트 (터널 + DB)"""
        if not self.engine:
            QMessageBox.critical(self, "오류", "터널 엔진이 초기화되지 않았습니다.")
            return

        temp_config = self.get_data()
        dialog = TestProgressDialog(self, f"통합 테스트 - {temp_config.get('name', 'Unknown')}")

        # DB 자격 증명 확인 (선택 사항)
        db_user = self.input_db_user.text() if self.chk_save_credentials.isChecked() else None
        db_password = self.input_db_password.text() if self.chk_save_credentials.isChecked() else None

        # 임시 ConfigManager
        class TempConfigManager:
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

        encryptor = None
        if hasattr(self.parent(), 'config_mgr'):
            encryptor = self.parent().config_mgr.encryptor

        temp_config_mgr = TempConfigManager(
            db_user, db_password,
            self.tunnel_data.get('db_password_encrypted') if db_user else None,
            encryptor
        )

        worker = ConnectionTestWorker(TestType.INTEGRATED, temp_config, self.engine, temp_config_mgr)
        worker.progress.connect(dialog.update_progress)
        worker.finished.connect(lambda s, m: dialog.show_result(s, m))
        worker.start()
        dialog.exec()
