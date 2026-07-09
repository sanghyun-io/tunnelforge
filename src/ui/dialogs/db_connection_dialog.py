"""
DB 연결 다이얼로그
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QSpinBox, QPushButton, QComboBox,
    QGroupBox, QMessageBox, QApplication,
    QRadioButton, QButtonGroup
)
from PyQt6.QtCore import Qt
from typing import Optional

from src.core.db_connector import MySQLConnector
from src.core.postgres_connector import PostgresConnector
from src.core.constants import DEFAULT_MYSQL_PORT, DEFAULT_LOCAL_HOST


class DBConnectionDialog(QDialog):
    """DB 연결 다이얼로그"""

    def __init__(self, parent=None, tunnel_engine=None, config_manager=None):
        super().__init__(parent)
        self.setWindowTitle("DB 연결")
        self.resize(450, 350)

        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.connector = None

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- 연결 방식 선택 ---
        mode_group = QGroupBox("연결 방식")
        mode_layout = QVBoxLayout(mode_group)

        self.btn_group = QButtonGroup(self)

        # 활성 터널 사용
        tunnel_layout = QHBoxLayout()
        self.radio_tunnel = QRadioButton("활성 터널 사용")
        self.combo_tunnel = QComboBox()
        self.combo_tunnel.setMinimumWidth(200)
        tunnel_layout.addWidget(self.radio_tunnel)
        tunnel_layout.addWidget(self.combo_tunnel)
        tunnel_layout.addStretch()
        mode_layout.addLayout(tunnel_layout)

        # 직접 입력
        self.radio_direct = QRadioButton("직접 입력")
        mode_layout.addWidget(self.radio_direct)

        self.btn_group.addButton(self.radio_tunnel)
        self.btn_group.addButton(self.radio_direct)
        self.radio_direct.setChecked(True)

        # 모드 변경 시 UI 업데이트
        self.radio_tunnel.toggled.connect(self.on_mode_changed)

        layout.addWidget(mode_group)

        # --- 연결 정보 입력 ---
        conn_group = QGroupBox("연결 정보")
        form_layout = QFormLayout(conn_group)

        self.input_host = QLineEdit(DEFAULT_LOCAL_HOST)
        self.input_port = QSpinBox()
        self.input_port.setRange(1, 65535)
        self.input_port.setValue(DEFAULT_MYSQL_PORT)

        self.combo_engine = QComboBox()
        self.combo_engine.addItem("MySQL", "mysql")
        self.combo_engine.addItem("PostgreSQL", "postgresql")

        self.input_user = QLineEdit()
        self.input_user.setPlaceholderText("DB 사용자명")

        self.input_password = QLineEdit()
        self.input_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_password.setPlaceholderText("비밀번호")

        self.input_database = QLineEdit()
        self.input_database.setPlaceholderText("(선택) DB 이름")

        form_layout.addRow("Host:", self.input_host)
        form_layout.addRow("Port:", self.input_port)
        form_layout.addRow("DB Engine:", self.combo_engine)
        form_layout.addRow("User:", self.input_user)
        form_layout.addRow("Password:", self.input_password)
        form_layout.addRow("Database:", self.input_database)

        layout.addWidget(conn_group)

        # --- 버튼 ---
        button_layout = QHBoxLayout()

        btn_test = QPushButton("연결 테스트")
        btn_test.setStyleSheet("""
            QPushButton {
                background-color: #f1c40f; color: #333; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #d4ac0d; }
        """)
        btn_test.clicked.connect(self.test_connection)

        btn_connect = QPushButton("연결")
        btn_connect.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_connect.clicked.connect(self.do_connect)

        btn_cancel = QPushButton("취소")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.reject)

        button_layout.addWidget(btn_test)
        button_layout.addStretch()
        button_layout.addWidget(btn_connect)
        button_layout.addWidget(btn_cancel)

        layout.addLayout(button_layout)

        # 활성 터널 목록 로드
        self.load_active_tunnels()

    def load_active_tunnels(self):
        """활성화된 터널 목록 로드"""
        self.combo_tunnel.clear()

        if not self.tunnel_engine:
            self.radio_tunnel.setEnabled(False)
            return

        tunnels = self.tunnel_engine.get_active_tunnels()
        if not tunnels:
            self.combo_tunnel.addItem("활성 터널 없음")
            self.radio_tunnel.setEnabled(False)
        else:
            for t in tunnels:
                engine = self._engine_from_tunnel(t).upper() if self._engine_from_tunnel(t) else "DB"
                display = f"{t['name']} ({engine}, {t['host']}:{t['port']})"
                self.combo_tunnel.addItem(display, t)
            self.radio_tunnel.setEnabled(True)
            # 터널 선택 변경 시 Host/Port 및 자격 증명 자동 채우기
            self.combo_tunnel.currentIndexChanged.connect(self._on_tunnel_selected)

    def _on_tunnel_selected(self):
        """터널 선택 시 Host/Port 및 저장된 자격 증명 자동 채우기"""
        if not self.radio_tunnel.isChecked():
            return

        current_data = self.combo_tunnel.currentData()
        if current_data:
            self._apply_tunnel_data(current_data)

    def _apply_tunnel_data(self, tunnel_data: dict) -> None:
        """터널 데이터로 Host/Port, 자격 증명, 엔진, 기본 DB를 채운다."""
        if 'host' in tunnel_data and 'port' in tunnel_data:
            self.input_host.setText(tunnel_data['host'])
            self.input_port.setValue(tunnel_data['port'])
        if 'tunnel_id' in tunnel_data:
            self._fill_saved_credentials(tunnel_data['tunnel_id'])
            config = getattr(self.tunnel_engine, "tunnel_configs", {}).get(tunnel_data['tunnel_id'], {})
            self._apply_engine_from_config(config)
            database = config.get('default_database') or config.get('default_schema')
            if database:
                self.input_database.setText(database or "")

    def _fill_saved_credentials(self, tunnel_id: str):
        """저장된 자격 증명 자동 채우기"""
        if not self.config_manager:
            return

        db_user, db_password = self.config_manager.get_tunnel_credentials(tunnel_id)
        if db_user:
            self.input_user.setText(db_user)
        if db_password:
            self.input_password.setText(db_password)

    def on_mode_changed(self):
        """연결 모드 변경 시"""
        use_tunnel = self.radio_tunnel.isChecked()

        self.combo_tunnel.setEnabled(use_tunnel)
        self.input_host.setEnabled(not use_tunnel)
        self.input_port.setEnabled(not use_tunnel)

        if use_tunnel and self.combo_tunnel.currentData():
            self._apply_tunnel_data(self.combo_tunnel.currentData())

    def _read_connection_fields(self) -> tuple:
        """입력 필드에서 연결 정보를 읽는다."""
        host = self.input_host.text()
        port = self.input_port.value()
        user = self.input_user.text()
        password = self.input_password.text()
        database = self.input_database.text().strip() or None
        return host, port, user, password, database

    def _build_connector_or_raise(self, host, port, user, password, database):
        """선택된 엔진에 맞는 커넥터를 생성한다."""
        engine = self._current_engine(host, port)
        connector = self._create_connector(engine, host, port, user, password, database)
        return engine, connector

    def test_connection(self):
        """연결 테스트"""
        host, port, user, password, database = self._read_connection_fields()

        if not user:
            QMessageBox.warning(self, "입력 오류", "사용자명을 입력하세요.")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            engine, connector = self._build_connector_or_raise(host, port, user, password, database)
            success, msg = connector.connect()
            connector.disconnect()

            QApplication.restoreOverrideCursor()

            if success:
                QMessageBox.information(self, "연결 성공", f"✅ {self._engine_label(engine)} {msg}")
            else:
                QMessageBox.warning(self, "연결 실패", f"❌ {msg}")
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "오류", str(e))

    def do_connect(self):
        """연결 수행"""
        host, port, user, password, database = self._read_connection_fields()

        if not user:
            QMessageBox.warning(self, "입력 오류", "사용자명을 입력하세요.")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            _, self.connector = self._build_connector_or_raise(host, port, user, password, database)
            success, msg = self.connector.connect()

            QApplication.restoreOverrideCursor()

            if success:
                self.accept()
            else:
                self.connector = None
                QMessageBox.warning(self, "연결 실패", msg)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.connector = None
            QMessageBox.critical(self, "오류", str(e))

    def get_connector(self) -> Optional[MySQLConnector]:
        """연결된 커넥터 반환"""
        return self.connector

    def _engine_from_tunnel(self, tunnel_data: dict) -> Optional[str]:
        tunnel_id = tunnel_data.get('tunnel_id')
        config = getattr(self.tunnel_engine, "tunnel_configs", {}).get(tunnel_id, {})
        engine = config.get('db_engine')
        return engine if engine in ('mysql', 'postgresql') else None

    def _apply_engine_from_config(self, config: dict):
        engine = config.get('db_engine')
        index = self.combo_engine.findData(engine)
        if index >= 0:
            self.combo_engine.setCurrentIndex(index)

    def _current_engine(self, host: str, port: int) -> str:
        if self.radio_tunnel.isChecked() and self.combo_tunnel.currentData():
            tunnel_data = self.combo_tunnel.currentData()
            engine = self._engine_from_tunnel(tunnel_data)
            if engine:
                return engine
            raise ValueError("선택한 터널에 DB Engine이 설정되어 있지 않습니다.\n터널 연결 설정에서 MySQL 또는 PostgreSQL을 먼저 선택해주세요.")
        engine = self.combo_engine.currentData()
        if engine in ("mysql", "postgresql"):
            return engine
        raise ValueError("DB Engine을 선택해주세요.")

    def _create_connector(self, engine: str, host: str, port: int, user: str, password: str, database: str = None):
        if engine == "postgresql":
            return PostgresConnector(host, port, user, password, database)
        return MySQLConnector(host, port, user, password, database)

    def _engine_label(self, engine: str) -> str:
        return "PostgreSQL" if engine == "postgresql" else "MySQL"

    def get_connection_identifier(self) -> str:
        """
        연결 식별자 반환
        - 터널 사용 시: 터널 이름
        - 직접 입력 시: host_port 형식
        """
        if self.radio_tunnel.isChecked() and self.combo_tunnel.currentData():
            tunnel_data = self.combo_tunnel.currentData()
            return tunnel_data.get('name', 'unknown')
        else:
            host = self.input_host.text().replace('.', '-')
            port = self.input_port.value()
            return f"{host}_{port}"
