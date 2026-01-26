"""
DB 연결 및 Export 관련 다이얼로그
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem, QGroupBox,
    QFileDialog, QMessageBox, QProgressBar, QApplication,
    QRadioButton, QButtonGroup, QWidget, QAbstractItemView
)
from PyQt6.QtCore import Qt
from typing import List, Optional

from src.core.db_connector import MySQLConnector
from src.exporters.mysqlsh_exporter import (
    MySQLShellChecker, MySQLShellConfig, check_mysqlsh
)
from src.ui.workers.mysql_worker import MySQLShellWorker


class DBConnectionDialog(QDialog):
    """DB 연결 다이얼로그"""

    def __init__(self, parent=None, tunnel_engine=None, config_manager=None):
        super().__init__(parent)
        self.setWindowTitle("DB 연결")
        self.resize(450, 350)

        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.connector: Optional[MySQLConnector] = None

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

        self.input_host = QLineEdit("127.0.0.1")
        self.input_port = QSpinBox()
        self.input_port.setRange(1, 65535)
        self.input_port.setValue(3306)

        self.input_user = QLineEdit()
        self.input_user.setPlaceholderText("MySQL 사용자명")

        self.input_password = QLineEdit()
        self.input_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_password.setPlaceholderText("비밀번호")

        form_layout.addRow("Host:", self.input_host)
        form_layout.addRow("Port:", self.input_port)
        form_layout.addRow("User:", self.input_user)
        form_layout.addRow("Password:", self.input_password)

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
                display = f"{t['name']} ({t['host']}:{t['port']})"
                self.combo_tunnel.addItem(display, t)
            self.radio_tunnel.setEnabled(True)
            # 터널 선택 변경 시 자격 증명 자동 채우기
            self.combo_tunnel.currentIndexChanged.connect(self._on_tunnel_selected)

    def _on_tunnel_selected(self):
        """터널 선택 시 저장된 자격 증명 자동 채우기"""
        if not self.radio_tunnel.isChecked():
            return

        current_data = self.combo_tunnel.currentData()
        if current_data and 'tunnel_id' in current_data:
            self._fill_saved_credentials(current_data['tunnel_id'])

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
            tunnel_data = self.combo_tunnel.currentData()
            self.input_host.setText(tunnel_data['host'])
            self.input_port.setValue(tunnel_data['port'])
            # 저장된 자격 증명 자동 채우기
            if 'tunnel_id' in tunnel_data:
                self._fill_saved_credentials(tunnel_data['tunnel_id'])

    def test_connection(self):
        """연결 테스트"""
        host = self.input_host.text()
        port = self.input_port.value()
        user = self.input_user.text()
        password = self.input_password.text()

        if not user:
            QMessageBox.warning(self, "입력 오류", "사용자명을 입력하세요.")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            connector = MySQLConnector(host, port, user, password)
            success, msg = connector.connect()
            connector.disconnect()

            QApplication.restoreOverrideCursor()

            if success:
                QMessageBox.information(self, "연결 성공", f"✅ {msg}")
            else:
                QMessageBox.warning(self, "연결 실패", f"❌ {msg}")
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "오류", str(e))

    def do_connect(self):
        """연결 수행"""
        host = self.input_host.text()
        port = self.input_port.value()
        user = self.input_user.text()
        password = self.input_password.text()

        if not user:
            QMessageBox.warning(self, "입력 오류", "사용자명을 입력하세요.")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            self.connector = MySQLConnector(host, port, user, password)
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


# ============================================================
# MySQL Shell 기반 Export/Import 다이얼로그
# ============================================================

class MySQLShellExportDialog(QDialog):
    """MySQL Shell Export 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None,
                 config_manager=None, connection_info: str = ""):
        super().__init__(parent)
        self.setWindowTitle("MySQL Shell Export (병렬 처리)")
        self.resize(600, 650)

        self.connector = connector
        self.config_manager = config_manager
        self.connection_info = connection_info  # 터널명 또는 host_port
        self.worker: Optional[MySQLShellWorker] = None

        # mysqlsh 설치 확인
        self.mysqlsh_installed, self.mysqlsh_msg = check_mysqlsh()

        self.init_ui()
        self.load_schemas()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- mysqlsh 상태 표시 ---
        status_group = QGroupBox("MySQL Shell 상태")
        status_layout = QVBoxLayout(status_group)

        if self.mysqlsh_installed:
            status_label = QLabel(f"✅ {self.mysqlsh_msg}")
            status_label.setStyleSheet("color: green;")
        else:
            status_label = QLabel(f"❌ {self.mysqlsh_msg}")
            status_label.setStyleSheet("color: red;")

            btn_guide = QPushButton("설치 가이드 보기")
            btn_guide.setStyleSheet("""
                QPushButton {
                    background-color: #3498db; color: white;
                    padding: 4px 12px; border-radius: 4px; border: none;
                }
                QPushButton:hover { background-color: #2980b9; }
            """)
            btn_guide.clicked.connect(self.show_install_guide)
            status_layout.addWidget(btn_guide)

        status_layout.addWidget(status_label)
        layout.addWidget(status_group)

        # --- Export 유형 선택 ---
        type_group = QGroupBox("Export 유형")
        type_layout = QVBoxLayout(type_group)

        self.btn_type_group = QButtonGroup(self)

        self.radio_full = QRadioButton("전체 스키마 Export")
        self.radio_full.setChecked(True)
        self.radio_partial = QRadioButton("선택 테이블 Export")

        self.btn_type_group.addButton(self.radio_full)
        self.btn_type_group.addButton(self.radio_partial)

        self.radio_full.toggled.connect(self.on_type_changed)

        type_layout.addWidget(self.radio_full)
        type_layout.addWidget(self.radio_partial)
        layout.addWidget(type_group)

        # --- 스키마 선택 ---
        schema_layout = QHBoxLayout()
        schema_layout.addWidget(QLabel("Schema:"))
        self.combo_schema = QComboBox()
        self.combo_schema.setMinimumWidth(300)
        self.combo_schema.currentTextChanged.connect(self.on_schema_changed)
        schema_layout.addWidget(self.combo_schema)
        schema_layout.addStretch()
        layout.addLayout(schema_layout)

        # --- 테이블 선택 (일부 테이블 Export 시) ---
        self.table_group = QGroupBox("테이블 선택")
        table_layout = QVBoxLayout(self.table_group)

        btn_layout = QHBoxLayout()
        btn_select_all = QPushButton("전체 선택")
        btn_select_all.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_select_all.clicked.connect(self.select_all_tables)
        btn_deselect_all = QPushButton("전체 해제")
        btn_deselect_all.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_deselect_all.clicked.connect(self.deselect_all_tables)
        btn_layout.addWidget(btn_select_all)
        btn_layout.addWidget(btn_deselect_all)
        btn_layout.addStretch()
        table_layout.addLayout(btn_layout)

        self.list_tables = QListWidget()
        self.list_tables.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_tables.setMaximumHeight(150)
        table_layout.addWidget(self.list_tables)

        self.chk_include_fk = QCheckBox("FK 의존성 테이블 자동 포함")
        self.chk_include_fk.setChecked(True)
        table_layout.addWidget(self.chk_include_fk)

        self.table_group.setVisible(False)
        layout.addWidget(self.table_group)

        # --- Export 옵션 ---
        option_group = QGroupBox("Export 옵션")
        option_layout = QFormLayout(option_group)

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(4)
        option_layout.addRow("병렬 스레드:", self.spin_threads)

        self.combo_compression = QComboBox()
        self.combo_compression.addItems(["zstd", "gzip", "none"])
        option_layout.addRow("압축 방식:", self.combo_compression)

        layout.addWidget(option_group)

        # --- 출력 폴더 설정 ---
        folder_group = QGroupBox("출력 폴더 설정")
        folder_main_layout = QVBoxLayout(folder_group)

        # 기본 위치
        base_layout = QHBoxLayout()
        base_layout.addWidget(QLabel("기본 위치:"))
        self.input_base_dir = QLineEdit()
        self.input_base_dir.setReadOnly(True)
        self.input_base_dir.setText(self._get_base_output_dir())
        btn_browse_base = QPushButton("선택")
        btn_browse_base.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_browse_base.clicked.connect(self.browse_base_dir)
        base_layout.addWidget(self.input_base_dir)
        base_layout.addWidget(btn_browse_base)
        folder_main_layout.addLayout(base_layout)

        # 폴더 이름 옵션
        naming_layout = QHBoxLayout()
        naming_layout.addWidget(QLabel("폴더 이름:"))

        # 자동 지정 라디오
        self.radio_auto_naming = QRadioButton("자동 지정")
        self.radio_auto_naming.setChecked(True)
        self.radio_auto_naming.toggled.connect(self._on_naming_mode_changed)
        naming_layout.addWidget(self.radio_auto_naming)

        # 자동 지정 옵션 체크박스들
        self.chk_name = QCheckBox("name")
        self.chk_name.setChecked(True)
        self.chk_name.setToolTip("연결 정보 (터널명 또는 host_port)")
        self.chk_name.toggled.connect(self._on_naming_option_changed)

        self.chk_schema = QCheckBox("schema")
        self.chk_schema.setChecked(True)
        self.chk_schema.toggled.connect(self._on_naming_option_changed)

        self.chk_timestamp = QCheckBox("timestamp")
        self.chk_timestamp.setChecked(True)
        self.chk_timestamp.toggled.connect(self._on_naming_option_changed)

        naming_layout.addWidget(self.chk_name)
        naming_layout.addWidget(self.chk_schema)
        naming_layout.addWidget(self.chk_timestamp)

        # 수동 지정 라디오
        self.radio_manual_naming = QRadioButton("수동 지정:")
        self.radio_manual_naming.toggled.connect(self._on_naming_mode_changed)
        naming_layout.addWidget(self.radio_manual_naming)

        self.input_manual_folder = QLineEdit()
        self.input_manual_folder.setPlaceholderText("폴더명 입력...")
        self.input_manual_folder.setEnabled(False)
        self.input_manual_folder.setMaximumWidth(150)
        self.input_manual_folder.textChanged.connect(self._update_output_dir_preview)
        naming_layout.addWidget(self.input_manual_folder)

        naming_layout.addStretch()
        folder_main_layout.addLayout(naming_layout)

        # 최종 경로 미리보기
        preview_layout = QHBoxLayout()
        preview_layout.addWidget(QLabel("최종 경로:"))
        self.input_output_dir = QLineEdit()
        self.input_output_dir.setReadOnly(True)
        self.input_output_dir.setStyleSheet("background-color: #f0f0f0;")
        preview_layout.addWidget(self.input_output_dir)
        folder_main_layout.addLayout(preview_layout)

        layout.addWidget(folder_group)

        # 초기 출력 경로 설정
        self._load_naming_settings()
        self._update_output_dir_preview()

        # --- 진행 상황 ---
        # 프로그레스 바
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m 테이블 (%p%)")
        layout.addWidget(self.progress_bar)

        # 상태 레이블
        self.label_status = QLabel()
        self.label_status.setVisible(False)
        self.label_status.setStyleSheet("color: #2980b9; font-weight: bold;")
        layout.addWidget(self.label_status)

        # 로그
        self.txt_log = QListWidget()
        self.txt_log.setMaximumHeight(120)
        self.txt_log.setVisible(False)
        layout.addWidget(self.txt_log)

        # --- 버튼 ---
        button_layout = QHBoxLayout()

        self.btn_export = QPushButton("🚀 Export 시작")
        self.btn_export.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #229954; }
        """)
        self.btn_export.clicked.connect(self.do_export)
        self.btn_export.setEnabled(self.mysqlsh_installed)

        btn_cancel = QPushButton("닫기")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.close)

        button_layout.addStretch()
        button_layout.addWidget(self.btn_export)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def _get_base_output_dir(self) -> str:
        """기본 출력 디렉토리 (부모 폴더)"""
        if self.config_manager:
            saved = self.config_manager.get_app_setting('mysqlsh_export_base_dir')
            if saved:
                return saved
        import os
        return os.path.join(os.path.expanduser("~"), "Desktop")

    def _generate_output_dir(self, schema: str = "") -> str:
        """
        동적 출력 폴더명 생성
        설정에 따라 name, schema, timestamp 조합
        """
        import os
        from datetime import datetime

        base_dir = self._get_base_output_dir()

        # 수동 모드일 경우
        if hasattr(self, 'radio_manual_naming') and self.radio_manual_naming.isChecked():
            manual_name = self.input_manual_folder.text().strip()
            if manual_name:
                # 파일명에 사용할 수 없는 문자 제거
                safe_name = manual_name.replace(':', '_').replace('/', '_').replace('\\', '_')
                safe_name = safe_name.replace('*', '_').replace('?', '_').replace('"', '_')
                safe_name = safe_name.replace('<', '_').replace('>', '_').replace('|', '_')
                return os.path.join(base_dir, safe_name)
            return os.path.join(base_dir, f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

        # 자동 모드
        parts = []

        # name (연결 정보)
        if hasattr(self, 'chk_name') and self.chk_name.isChecked() and self.connection_info:
            safe_conn = self.connection_info.replace(':', '_').replace('/', '_').replace('\\', '_')
            parts.append(safe_conn)

        # schema
        if hasattr(self, 'chk_schema') and self.chk_schema.isChecked() and schema:
            parts.append(schema)

        # timestamp
        if hasattr(self, 'chk_timestamp') and self.chk_timestamp.isChecked():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            parts.append(timestamp)

        # 모두 비활성화된 경우 기본값
        if not parts:
            parts.append(f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

        folder_name = "_".join(parts)
        return os.path.join(base_dir, folder_name)

    def _get_default_output_dir(self) -> str:
        """기본 출력 디렉토리 (초기값)"""
        return self._generate_output_dir("")

    def _load_naming_settings(self):
        """폴더 네이밍 설정 로드"""
        if not self.config_manager:
            return

        mode = self.config_manager.get_app_setting('mysqlsh_export_folder_mode', 'auto')
        use_name = self.config_manager.get_app_setting('mysqlsh_export_folder_use_name', True)
        use_schema = self.config_manager.get_app_setting('mysqlsh_export_folder_use_schema', True)
        use_timestamp = self.config_manager.get_app_setting('mysqlsh_export_folder_use_timestamp', True)
        manual_name = self.config_manager.get_app_setting('mysqlsh_export_folder_manual_name', '')

        if mode == 'manual':
            self.radio_manual_naming.setChecked(True)
            self.input_manual_folder.setText(manual_name)
        else:
            self.radio_auto_naming.setChecked(True)

        self.chk_name.setChecked(use_name)
        self.chk_schema.setChecked(use_schema)
        self.chk_timestamp.setChecked(use_timestamp)

    def _save_naming_settings(self):
        """폴더 네이밍 설정 저장"""
        if not self.config_manager:
            return

        mode = 'manual' if self.radio_manual_naming.isChecked() else 'auto'
        self.config_manager.set_app_setting('mysqlsh_export_folder_mode', mode)
        self.config_manager.set_app_setting('mysqlsh_export_folder_use_name', self.chk_name.isChecked())
        self.config_manager.set_app_setting('mysqlsh_export_folder_use_schema', self.chk_schema.isChecked())
        self.config_manager.set_app_setting('mysqlsh_export_folder_use_timestamp', self.chk_timestamp.isChecked())
        self.config_manager.set_app_setting('mysqlsh_export_folder_manual_name', self.input_manual_folder.text())

    def _on_naming_mode_changed(self):
        """폴더 네이밍 모드 변경 시"""
        is_auto = self.radio_auto_naming.isChecked()

        # 자동 옵션 활성화/비활성화
        self.chk_name.setEnabled(is_auto)
        self.chk_schema.setEnabled(is_auto)
        self.chk_timestamp.setEnabled(is_auto)

        # 수동 입력 활성화/비활성화
        self.input_manual_folder.setEnabled(not is_auto)

        self._save_naming_settings()
        self._update_output_dir_preview()

    def _on_naming_option_changed(self):
        """자동 네이밍 옵션 변경 시"""
        # 최소 하나는 선택되어야 함
        if not self.chk_name.isChecked() and not self.chk_schema.isChecked() and not self.chk_timestamp.isChecked():
            sender = self.sender()
            if sender:
                sender.setChecked(True)
                QMessageBox.warning(self, "경고", "최소 하나의 옵션은 선택되어야 합니다.")
            return

        self._save_naming_settings()
        self._update_output_dir_preview()

    def _update_output_dir_preview(self):
        """출력 경로 미리보기 업데이트"""
        schema = self.combo_schema.currentText() if hasattr(self, 'combo_schema') else ""
        self.input_output_dir.setText(self._generate_output_dir(schema))

    def browse_base_dir(self):
        """기본 위치 선택"""
        import os
        current_path = self.input_base_dir.text()
        default_path = current_path if os.path.exists(current_path) else os.path.expanduser("~")

        folder = QFileDialog.getExistingDirectory(
            self, "Export 기본 폴더 선택", default_path
        )
        if folder:
            self.input_base_dir.setText(folder)
            if self.config_manager:
                self.config_manager.set_app_setting('mysqlsh_export_base_dir', folder)
            self._update_output_dir_preview()

    def show_install_guide(self):
        guide = MySQLShellChecker.get_install_guide()
        QMessageBox.information(self, "MySQL Shell 설치 가이드", guide)

    def on_type_changed(self):
        is_partial = self.radio_partial.isChecked()
        self.table_group.setVisible(is_partial)

    def load_schemas(self):
        self.combo_schema.clear()
        if not self.connector:
            return
        schemas = self.connector.get_schemas()
        for schema in schemas:
            self.combo_schema.addItem(schema)

    def on_schema_changed(self, schema: str):
        self.list_tables.clear()
        if not schema or not self.connector:
            return
        tables = self.connector.get_tables(schema)
        for table in tables:
            item = QListWidgetItem(table)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_tables.addItem(item)

        # 출력 폴더명 업데이트 (스키마 반영)
        self._update_output_dir_preview()

    def select_all_tables(self):
        for i in range(self.list_tables.count()):
            self.list_tables.item(i).setCheckState(Qt.CheckState.Checked)

    def deselect_all_tables(self):
        for i in range(self.list_tables.count()):
            self.list_tables.item(i).setCheckState(Qt.CheckState.Unchecked)

    def get_selected_tables(self) -> List[str]:
        tables = []
        for i in range(self.list_tables.count()):
            item = self.list_tables.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                tables.append(item.text())
        return tables

    def set_ui_enabled(self, enabled: bool):
        """Export 진행 중 UI 요소 활성화/비활성화"""
        self.radio_full.setEnabled(enabled)
        self.radio_partial.setEnabled(enabled)
        self.combo_schema.setEnabled(enabled)
        self.table_group.setEnabled(enabled)
        self.spin_threads.setEnabled(enabled)
        self.combo_compression.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)

        # 폴더 설정 UI
        self.input_base_dir.setEnabled(enabled)
        self.radio_auto_naming.setEnabled(enabled)
        self.radio_manual_naming.setEnabled(enabled)
        if enabled:
            # 모드에 따라 옵션 활성화
            is_auto = self.radio_auto_naming.isChecked()
            self.chk_name.setEnabled(is_auto)
            self.chk_schema.setEnabled(is_auto)
            self.chk_timestamp.setEnabled(is_auto)
            self.input_manual_folder.setEnabled(not is_auto)
        else:
            self.chk_name.setEnabled(False)
            self.chk_schema.setEnabled(False)
            self.chk_timestamp.setEnabled(False)
            self.input_manual_folder.setEnabled(False)

    def do_export(self):
        schema = self.combo_schema.currentText()
        output_dir = self.input_output_dir.text()

        if not schema:
            QMessageBox.warning(self, "오류", "스키마를 선택하세요.")
            return

        if not output_dir:
            QMessageBox.warning(self, "오류", "출력 폴더를 선택하세요.")
            return

        # 일부 테이블 Export 시 테이블 확인
        if self.radio_partial.isChecked():
            tables = self.get_selected_tables()
            if not tables:
                QMessageBox.warning(self, "오류", "최소 하나의 테이블을 선택하세요.")
                return

        # 설정 저장
        if self.config_manager:
            self.config_manager.set_app_setting('mysqlsh_export_dir', output_dir)

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)
        self.txt_log.clear()
        self.txt_log.setVisible(True)

        # 프로그레스 바 초기화 및 표시
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(0)  # 초기에는 테이블 수 미정 (indeterminate)
        self.label_status.setVisible(True)
        self.label_status.setText("Export 준비 중...")

        # MySQL Shell 설정
        config = MySQLShellConfig(
            host="127.0.0.1",  # 터널 통해 로컬 접속
            port=self.connector.port if hasattr(self.connector, 'port') else 3306,
            user=self.connector.user if hasattr(self.connector, 'user') else "root",
            password=self.connector.password if hasattr(self.connector, 'password') else ""
        )

        # 작업 스레드 시작
        if self.radio_full.isChecked():
            self.worker = MySQLShellWorker(
                "export_schema", config,
                schema=schema,
                output_dir=output_dir,
                threads=self.spin_threads.value(),
                compression=self.combo_compression.currentText()
            )
        else:
            self.worker = MySQLShellWorker(
                "export_tables", config,
                schema=schema,
                tables=self.get_selected_tables(),
                output_dir=output_dir,
                threads=self.spin_threads.value(),
                compression=self.combo_compression.currentText(),
                include_fk_parents=self.chk_include_fk.isChecked()
            )

        self.worker.progress.connect(self.on_progress)
        self.worker.table_progress.connect(self.on_table_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_progress(self, msg: str):
        self.txt_log.addItem(msg)
        self.txt_log.scrollToBottom()

    def on_table_progress(self, current: int, total: int, table_name: str):
        """테이블별 진행률 업데이트"""
        # 프로그레스 바 최대값 설정 (처음 호출 시)
        if self.progress_bar.maximum() != total:
            self.progress_bar.setMaximum(total)

        self.progress_bar.setValue(current)
        self.label_status.setText(f"✅ {table_name} ({current}/{total})")

    def on_finished(self, success: bool, message: str):
        # UI 상태 복구
        self.set_ui_enabled(True)
        self.progress_bar.setVisible(False)
        self.label_status.setVisible(False)

        if success:
            self.txt_log.addItem(f"✅ 완료: {message}")
            QMessageBox.information(
                self, "Export 완료",
                f"✅ Export가 완료되었습니다.\n\n폴더: {self.input_output_dir.text()}"
            )
        else:
            self.txt_log.addItem(f"❌ 실패: {message}")
            QMessageBox.warning(self, "Export 실패", f"❌ {message}")

    def closeEvent(self, event):
        if self.connector:
            self.connector.disconnect()
        event.accept()


class MySQLShellImportDialog(QDialog):
    """MySQL Shell Import 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None):
        super().__init__(parent)
        self.setWindowTitle("MySQL Shell Import (병렬 처리)")
        self.resize(600, 700)

        self.connector = connector
        self.config_manager = config_manager
        self.worker: Optional[MySQLShellWorker] = None

        self.mysqlsh_installed, self.mysqlsh_msg = check_mysqlsh()

        # Import 결과 저장 (재시도용)
        self.import_results: dict = {}
        self.table_items: dict = {}  # 테이블명 -> QListWidgetItem 매핑
        self.last_input_dir: str = ""  # 마지막 사용한 input_dir
        self.last_target_schema: str = ""  # 마지막 사용한 target_schema

        self.init_ui()
        self.load_schemas()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- mysqlsh 상태 ---
        status_group = QGroupBox("MySQL Shell 상태")
        status_layout = QVBoxLayout(status_group)

        if self.mysqlsh_installed:
            status_label = QLabel(f"✅ {self.mysqlsh_msg}")
            status_label.setStyleSheet("color: green;")
        else:
            status_label = QLabel(f"❌ {self.mysqlsh_msg}")
            status_label.setStyleSheet("color: red;")

        status_layout.addWidget(status_label)
        layout.addWidget(status_group)

        # --- 입력 폴더 선택 ---
        input_group = QGroupBox("Dump 폴더")
        input_layout = QHBoxLayout(input_group)

        self.input_dir = QLineEdit()
        self.input_dir.setPlaceholderText("mysqlsh dump 폴더 선택...")

        btn_browse = QPushButton("선택")
        btn_browse.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_browse.clicked.connect(self.browse_input_dir)

        input_layout.addWidget(self.input_dir)
        input_layout.addWidget(btn_browse)
        layout.addWidget(input_group)

        # --- 대상 스키마 ---
        schema_group = QGroupBox("대상 스키마")
        schema_layout = QVBoxLayout(schema_group)

        self.chk_use_original = QCheckBox("원본 스키마명 사용")
        self.chk_use_original.setChecked(True)
        self.chk_use_original.toggled.connect(self.on_schema_option_changed)
        schema_layout.addWidget(self.chk_use_original)

        target_layout = QHBoxLayout()
        target_layout.addWidget(QLabel("대상 스키마:"))
        self.combo_target_schema = QComboBox()
        self.combo_target_schema.setMinimumWidth(200)
        self.combo_target_schema.setEnabled(False)
        target_layout.addWidget(self.combo_target_schema)
        target_layout.addStretch()
        schema_layout.addLayout(target_layout)

        layout.addWidget(schema_group)

        # --- Import 옵션 ---
        option_group = QGroupBox("Import 옵션")
        option_layout = QFormLayout(option_group)

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(4)
        option_layout.addRow("병렬 스레드:", self.spin_threads)

        layout.addWidget(option_group)

        # --- 타임존 설정 ---
        tz_group = QGroupBox("타임존 설정")
        tz_layout = QVBoxLayout(tz_group)

        self.btn_tz_group = QButtonGroup(self)
        
        # 1. 자동 감지 (권장)
        self.radio_tz_auto = QRadioButton("자동 감지 및 보정 (권장)")
        self.radio_tz_auto.setChecked(True)
        self.radio_tz_auto.setToolTip("서버가 지역명 타임존을 지원하지 않으면 자동으로 +09:00(KST)로 보정합니다.")
        
        # 2. 강제 KST
        self.radio_tz_kst = QRadioButton("강제 KST (+09:00)")
        
        # 3. 강제 UTC
        self.radio_tz_utc = QRadioButton("강제 UTC (+00:00)")
        
        # 4. 설정 안 함
        self.radio_tz_none = QRadioButton("설정 안 함 (서버 기본값)")

        self.btn_tz_group.addButton(self.radio_tz_auto)
        self.btn_tz_group.addButton(self.radio_tz_kst)
        self.btn_tz_group.addButton(self.radio_tz_utc)
        self.btn_tz_group.addButton(self.radio_tz_none)

        tz_layout.addWidget(self.radio_tz_auto)
        tz_layout.addWidget(self.radio_tz_kst)
        tz_layout.addWidget(self.radio_tz_utc)
        tz_layout.addWidget(self.radio_tz_none)
        
        layout.addWidget(tz_group)

        # --- Import 모드 선택 ---
        mode_group = QGroupBox("Import 모드 선택")
        mode_layout = QVBoxLayout(mode_group)

        self.btn_import_mode = QButtonGroup(self)

        # 1. 증분 Import (병합)
        mode_merge_layout = QVBoxLayout()
        self.radio_merge = QRadioButton("증분 Import (병합)")
        mode_merge_desc = QLabel("   기존 데이터 유지, 새로운 것만 추가\n   ⚠️ 중복 객체가 있으면 오류 발생")
        mode_merge_desc.setStyleSheet("color: #7f8c8d; font-size: 10pt; margin-left: 20px;")
        mode_merge_layout.addWidget(self.radio_merge)
        mode_merge_layout.addWidget(mode_merge_desc)
        mode_layout.addLayout(mode_merge_layout)

        # 2. 전체 교체 Import (권장)
        mode_replace_layout = QVBoxLayout()
        self.radio_replace = QRadioButton("전체 교체 Import (권장) ⭐")
        self.radio_replace.setChecked(True)  # 기본값
        mode_replace_desc = QLabel("   모든 객체(테이블/뷰/프로시저/이벤트) 재생성\n   ✅ Export → Import 시 권장")
        mode_replace_desc.setStyleSheet("color: #27ae60; font-size: 10pt; font-weight: bold; margin-left: 20px;")
        mode_replace_layout.addWidget(self.radio_replace)
        mode_replace_layout.addWidget(mode_replace_desc)
        mode_layout.addLayout(mode_replace_layout)

        # 3. 완전 재생성 Import
        mode_recreate_layout = QVBoxLayout()
        self.radio_recreate = QRadioButton("완전 재생성 Import")
        mode_recreate_desc = QLabel("   데이터베이스 삭제 후 처음부터 재생성\n   ⚠️ 모든 데이터 손실")
        mode_recreate_desc.setStyleSheet("color: #e74c3c; font-size: 10pt; margin-left: 20px;")
        mode_recreate_layout.addWidget(self.radio_recreate)
        mode_recreate_layout.addWidget(mode_recreate_desc)
        mode_layout.addLayout(mode_recreate_layout)

        self.btn_import_mode.addButton(self.radio_merge)
        self.btn_import_mode.addButton(self.radio_replace)
        self.btn_import_mode.addButton(self.radio_recreate)

        layout.addWidget(mode_group)

        # --- 진행 상황 섹션 (확장된 UI) ---
        self.progress_group = QGroupBox("진행 상황")
        self.progress_group.setVisible(False)
        progress_layout = QVBoxLayout(self.progress_group)

        # 상세 진행률 표시 영역
        detail_layout = QHBoxLayout()

        # 왼쪽: 진행률 정보
        left_detail = QVBoxLayout()
        self.label_percent = QLabel("📊 진행률: 0%")
        self.label_percent.setStyleSheet("font-weight: bold; font-size: 12pt;")
        self.label_data = QLabel("📦 데이터: 0 MB / 0 MB")
        self.label_speed = QLabel("⚡ 속도: 0 rows/s")
        self.label_tables = QLabel("📋 테이블: 0 / 0 완료")
        left_detail.addWidget(self.label_percent)
        left_detail.addWidget(self.label_data)
        left_detail.addWidget(self.label_speed)
        left_detail.addWidget(self.label_tables)

        detail_layout.addLayout(left_detail)
        detail_layout.addStretch()
        progress_layout.addLayout(detail_layout)

        # 프로그레스 바 (퍼센트 기준)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                text-align: center;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #27ae60;
                border-radius: 3px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)

        # 상태 라벨
        self.label_status = QLabel("준비 중...")
        self.label_status.setStyleSheet("color: #27ae60; font-weight: bold;")
        progress_layout.addWidget(self.label_status)

        layout.addWidget(self.progress_group)

        # --- 테이블 상태 목록 (GitHub Actions 스타일) ---
        self.table_status_group = QGroupBox("테이블 Import 상태")
        self.table_status_group.setVisible(False)
        table_status_layout = QVBoxLayout(self.table_status_group)

        self.table_list = QListWidget()
        self.table_list.setMinimumHeight(150)
        self.table_list.setMaximumHeight(200)
        self.table_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.table_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                background-color: #fafafa;
            }
            QListWidget::item {
                padding: 4px 8px;
                border-bottom: 1px solid #ecf0f1;
            }
            QListWidget::item:selected {
                background-color: #e8f4f8;
            }
        """)
        table_status_layout.addWidget(self.table_list)

        # 재시도 버튼 (실패 시에만 표시)
        retry_layout = QHBoxLayout()
        self.btn_retry = QPushButton("🔄 선택한 테이블 재시도")
        self.btn_retry.setVisible(False)
        self.btn_retry.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        self.btn_retry.clicked.connect(self.do_retry)

        self.btn_select_failed = QPushButton("실패한 테이블 모두 선택")
        self.btn_select_failed.setVisible(False)
        self.btn_select_failed.setStyleSheet("""
            QPushButton {
                background-color: #95a5a6; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #7f8c8d; }
        """)
        self.btn_select_failed.clicked.connect(self.select_failed_tables)

        retry_layout.addWidget(self.btn_select_failed)
        retry_layout.addWidget(self.btn_retry)
        retry_layout.addStretch()
        table_status_layout.addLayout(retry_layout)

        layout.addWidget(self.table_status_group)

        # --- 실행 로그 ---
        self.log_group = QGroupBox("실행 로그")
        self.log_group.setVisible(False)
        log_layout = QVBoxLayout(self.log_group)

        self.txt_log = QListWidget()
        self.txt_log.setMinimumHeight(80)
        self.txt_log.setMaximumHeight(120)
        self.txt_log.setStyleSheet("""
            QListWidget {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 9pt;
                background-color: #2c3e50;
                color: #ecf0f1;
                border: 1px solid #34495e;
                border-radius: 4px;
            }
            QListWidget::item {
                padding: 2px 4px;
            }
        """)
        log_layout.addWidget(self.txt_log)

        layout.addWidget(self.log_group)

        # --- 버튼 ---
        button_layout = QHBoxLayout()

        self.btn_import = QPushButton("📥 Import 시작")
        self.btn_import.setStyleSheet("""
            QPushButton {
                background-color: #e67e22; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #d35400; }
        """)
        self.btn_import.clicked.connect(self.do_import)
        self.btn_import.setEnabled(self.mysqlsh_installed)

        btn_cancel = QPushButton("닫기")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.close)

        button_layout.addStretch()
        button_layout.addWidget(self.btn_import)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def load_schemas(self):
        self.combo_target_schema.clear()
        if not self.connector:
            return
        schemas = self.connector.get_schemas()
        for schema in schemas:
            self.combo_target_schema.addItem(schema)

    def on_schema_option_changed(self):
        use_original = self.chk_use_original.isChecked()
        self.combo_target_schema.setEnabled(not use_original)

    def browse_input_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Dump 폴더 선택", self.input_dir.text()
        )
        if folder:
            self.input_dir.setText(folder)

    def set_ui_enabled(self, enabled: bool):
        """Import 진행 중 UI 요소 활성화/비활성화"""
        self.input_dir.setEnabled(enabled)
        self.chk_use_original.setEnabled(enabled)
        self.combo_target_schema.setEnabled(enabled and not self.chk_use_original.isChecked())
        self.spin_threads.setEnabled(enabled)
        self.radio_merge.setEnabled(enabled)
        self.radio_replace.setEnabled(enabled)
        self.radio_recreate.setEnabled(enabled)
        self.radio_recreate.setEnabled(enabled)
        self.radio_tz_auto.setEnabled(enabled)
        self.radio_tz_kst.setEnabled(enabled)
        self.radio_tz_utc.setEnabled(enabled)
        self.radio_tz_none.setEnabled(enabled)
        self.btn_import.setEnabled(enabled)

    def check_timezone_support(self) -> bool:
        """
        서버가 'Asia/Seoul' 같은 지역명 타임존을 지원하는지 확인
        """
        if not self.connector:
            return False
            
        try:
            # mysql.time_zone_name 테이블에서 Asia/Seoul 조회
            # 단순히 테이블 존재 여부만 보지 않고 실제 데이터가 있는지 확인
            query = "SELECT 1 FROM mysql.time_zone_name WHERE Name = 'Asia/Seoul' LIMIT 1"
            rows = self.connector.execute(query)
            return len(rows) > 0
        except Exception:
            return False

    def do_import(self, retry_tables: list = None):
        """Import 실행 (retry_tables가 주어지면 해당 테이블만 재시도)"""
        input_dir = self.input_dir.text()

        if not input_dir:
            QMessageBox.warning(self, "오류", "Dump 폴더를 선택하세요.")
            return

        import os
        if not os.path.exists(input_dir):
            QMessageBox.warning(self, "오류", "폴더가 존재하지 않습니다.")
            return

        target_schema = None
        if not self.chk_use_original.isChecked():
            target_schema = self.combo_target_schema.currentText()
            if not target_schema:
                QMessageBox.warning(self, "오류", "대상 스키마를 선택하세요.")
                return

        # 저장 (재시도용)
        self.last_input_dir = input_dir
        self.last_target_schema = target_schema

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)
        self.btn_retry.setVisible(False)
        self.btn_select_failed.setVisible(False)

        # 로그 및 진행 상황 UI 표시
        self.progress_group.setVisible(True)
        self.table_status_group.setVisible(True)
        self.log_group.setVisible(True)

        # 재시도가 아닌 경우 초기화
        if not retry_tables:
            self.txt_log.clear()
            self.table_list.clear()
            self.table_items.clear()
            self.import_results.clear()

        # 프로그레스 바 초기화
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.label_percent.setText("📊 진행률: 0%")
        self.label_data.setText("📦 데이터: 0 MB / 0 MB")
        self.label_speed.setText("⚡ 속도: 0 rows/s")
        self.label_tables.setText("📋 테이블: 0 / 0 완료")
        self.label_status.setText("Import 준비 중...")

        # MySQL Shell 설정
        config = MySQLShellConfig(
            host="127.0.0.1",
            port=self.connector.port if hasattr(self.connector, 'port') else 3306,
            user=self.connector.user if hasattr(self.connector, 'user') else "root",
            password=self.connector.password if hasattr(self.connector, 'password') else ""
        )

        # 타임존 설정 결정
        timezone_sql = None

        if self.radio_tz_auto.isChecked():
            self.txt_log.addItem("🔍 타임존 지원 여부 확인 중...")
            QApplication.processEvents()

            supports_named_tz = self.check_timezone_support()

            if supports_named_tz:
                self.txt_log.addItem("✅ 서버가 지역명 타임존을 지원합니다.")
            else:
                timezone_sql = "SET SESSION time_zone = '+09:00'"
                self.txt_log.addItem("⚠️ 서버가 지역명 타임존을 지원하지 않습니다.")
                self.txt_log.addItem("ℹ️ 'Asia/Seoul' 에러 방지를 위해 타임존을 '+09:00'으로 자동 보정합니다.")

        elif self.radio_tz_kst.isChecked():
            timezone_sql = "SET SESSION time_zone = '+09:00'"
            self.txt_log.addItem("ℹ️ 타임존을 강제로 '+09:00' (KST)로 설정합니다.")

        elif self.radio_tz_utc.isChecked():
            timezone_sql = "SET SESSION time_zone = '+00:00'"
            self.txt_log.addItem("ℹ️ 타임존을 강제로 '+00:00' (UTC)로 설정합니다.")

        # Import 모드 결정
        import_mode = "merge"  # 기본값
        if self.radio_replace.isChecked():
            import_mode = "replace"
        elif self.radio_recreate.isChecked():
            import_mode = "recreate"

        # 재시도 시 모드 표시
        if retry_tables:
            self.txt_log.addItem(f"🔄 재시도 모드: {len(retry_tables)}개 테이블")
            import_mode = "merge"  # 재시도 시에는 병합 모드 사용

        # 작업 스레드 시작
        self.worker = MySQLShellWorker(
            "import", config,
            input_dir=input_dir,
            target_schema=target_schema,
            threads=self.spin_threads.value(),
            import_mode=import_mode,
            timezone_sql=timezone_sql,
            retry_tables=retry_tables
        )

        # 시그널 연결
        self.worker.progress.connect(self.on_progress)
        self.worker.table_progress.connect(self.on_table_progress)
        self.worker.detail_progress.connect(self.on_detail_progress)
        self.worker.table_status.connect(self.on_table_status)
        self.worker.raw_output.connect(self.on_raw_output)
        self.worker.import_finished.connect(self.on_import_finished)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_progress(self, msg: str):
        """일반 진행 메시지 처리"""
        self.txt_log.addItem(msg)
        self.txt_log.scrollToBottom()
        self.label_status.setText(msg)

    def on_table_progress(self, current: int, total: int, table_name: str):
        """테이블별 진행률 업데이트"""
        self.label_tables.setText(f"📋 테이블: {current} / {total} 완료")

    def on_detail_progress(self, info: dict):
        """상세 진행 정보 업데이트"""
        percent = info.get('percent', 0)
        mb_done = info.get('mb_done', 0)
        mb_total = info.get('mb_total', 0)
        rows_sec = info.get('rows_sec', 0)
        speed = info.get('speed', '0 B/s')

        self.progress_bar.setValue(percent)
        self.label_percent.setText(f"📊 진행률: {percent}%")
        self.label_data.setText(f"📦 데이터: {mb_done:.2f} MB / {mb_total:.2f} MB")
        self.label_speed.setText(f"⚡ 속도: {rows_sec:,} rows/s | {speed}")

    def on_table_status(self, table_name: str, status: str, message: str):
        """테이블 상태 업데이트 (GitHub Actions 스타일)"""
        # 상태별 아이콘 및 스타일
        status_icons = {
            'pending': '⏳',
            'loading': '🔄',
            'done': '✅',
            'error': '❌'
        }
        status_colors = {
            'pending': '#95a5a6',
            'loading': '#3498db',
            'done': '#27ae60',
            'error': '#e74c3c'
        }

        icon = status_icons.get(status, '❓')
        color = status_colors.get(status, '#7f8c8d')

        # 기존 아이템이 있으면 업데이트, 없으면 새로 생성
        if table_name in self.table_items:
            item = self.table_items[table_name]
            display_text = f"{icon} {table_name}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item.setText(display_text)
            item.setForeground(Qt.GlobalColor.black)
        else:
            display_text = f"{icon} {table_name}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item = QListWidgetItem(display_text)
            self.table_list.addItem(item)
            self.table_items[table_name] = item

        # 결과 저장
        self.import_results[table_name] = {'status': status, 'message': message}

    def on_raw_output(self, line: str):
        """mysqlsh 실시간 출력 처리 (로그에 추가)"""
        # 너무 많은 로그 방지 (최대 500줄)
        if self.txt_log.count() > 500:
            self.txt_log.takeItem(0)
        self.txt_log.addItem(line)
        self.txt_log.scrollToBottom()

    def on_import_finished(self, success: bool, message: str, results: dict):
        """Import 완료 처리 (결과 저장 및 재시도 버튼 표시)"""
        self.import_results = results

        # 실패한 테이블이 있는지 확인
        failed_tables = [t for t, r in results.items() if r.get('status') == 'error']

        if failed_tables:
            self.btn_retry.setVisible(True)
            self.btn_select_failed.setVisible(True)
            self.txt_log.addItem(f"⚠️ {len(failed_tables)}개 테이블 Import 실패")

    def on_finished(self, success: bool, message: str):
        """작업 완료 처리"""
        # UI 상태 복구
        self.set_ui_enabled(True)

        # 결과 요약
        done_count = sum(1 for r in self.import_results.values() if r.get('status') == 'done')
        error_count = sum(1 for r in self.import_results.values() if r.get('status') == 'error')
        total_count = len(self.import_results)

        if success:
            self.label_status.setText(f"✅ Import 완료: {done_count}/{total_count} 테이블 성공")
            self.progress_bar.setValue(100)
            self.txt_log.addItem(f"✅ 완료: {message}")
            QMessageBox.information(self, "Import 완료", f"✅ Import가 완료되었습니다.\n\n성공: {done_count}개 테이블")
        else:
            self.label_status.setText(f"❌ Import 실패: {error_count}/{total_count} 테이블 오류")
            self.txt_log.addItem(f"❌ 실패: {message}")

            if error_count > 0:
                QMessageBox.warning(
                    self, "Import 실패",
                    f"❌ Import 중 오류가 발생했습니다.\n\n"
                    f"성공: {done_count}개 테이블\n"
                    f"실패: {error_count}개 테이블\n\n"
                    f"실패한 테이블을 선택하여 재시도할 수 있습니다."
                )
            else:
                QMessageBox.warning(self, "Import 실패", f"❌ {message}")

    def select_failed_tables(self):
        """실패한 테이블 모두 선택"""
        for table_name, result in self.import_results.items():
            if result.get('status') == 'error':
                if table_name in self.table_items:
                    self.table_items[table_name].setSelected(True)

    def do_retry(self):
        """선택한 테이블 재시도"""
        # 선택된 테이블 목록 가져오기
        selected_tables = []
        for table_name, item in self.table_items.items():
            if item.isSelected():
                selected_tables.append(table_name)

        if not selected_tables:
            QMessageBox.warning(self, "선택 필요", "재시도할 테이블을 선택하세요.")
            return

        # 확인 대화상자
        reply = QMessageBox.question(
            self, "재시도 확인",
            f"선택한 {len(selected_tables)}개 테이블을 재시도하시겠습니까?\n\n"
            f"테이블: {', '.join(selected_tables[:5])}{'...' if len(selected_tables) > 5 else ''}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # 선택된 테이블 상태를 pending으로 초기화
            for table in selected_tables:
                self.on_table_status(table, 'pending', '')

            # 재시도 실행
            self.do_import(retry_tables=selected_tables)

    def closeEvent(self, event):
        if self.connector:
            self.connector.disconnect()
        event.accept()


class MySQLShellWizard:
    """MySQL Shell Export/Import 마법사"""

    def __init__(self, parent=None, tunnel_engine=None, config_manager=None):
        self.parent = parent
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager

    def start_export(self) -> bool:
        """Export 마법사 시작"""
        # 1단계: DB 연결
        conn_dialog = DBConnectionDialog(
            self.parent,
            tunnel_engine=self.tunnel_engine,
            config_manager=self.config_manager
        )

        if conn_dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        connector = conn_dialog.get_connector()
        if not connector:
            return False

        # 연결 식별자 가져오기
        connection_info = conn_dialog.get_connection_identifier()

        # 2단계: Export
        export_dialog = MySQLShellExportDialog(
            self.parent,
            connector=connector,
            config_manager=self.config_manager,
            connection_info=connection_info
        )
        export_dialog.exec()

        return True

    def start_import(self) -> bool:
        """Import 마법사 시작"""
        # 1단계: DB 연결
        conn_dialog = DBConnectionDialog(
            self.parent,
            tunnel_engine=self.tunnel_engine,
            config_manager=self.config_manager
        )

        if conn_dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        connector = conn_dialog.get_connector()
        if not connector:
            return False

        # 2단계: Import
        import_dialog = MySQLShellImportDialog(
            self.parent,
            connector=connector,
            config_manager=self.config_manager
        )
        import_dialog.exec()

        return True
