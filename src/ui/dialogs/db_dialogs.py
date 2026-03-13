"""
DB 연결 및 Export 관련 다이얼로그
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem, QGroupBox,
    QFileDialog, QMessageBox, QProgressBar, QApplication,
    QRadioButton, QButtonGroup, QWidget, QAbstractItemView,
    QSplitter, QScrollArea
)
from PyQt6.QtCore import Qt
from typing import List, Optional
from datetime import datetime
import os

from src.core.db_connector import MySQLConnector
from src.core.constants import DEFAULT_MYSQL_PORT, DEFAULT_LOCAL_HOST
from src.exporters.mysqlsh_exporter import (
    MySQLShellChecker, MySQLShellConfig, check_mysqlsh,
    ForeignKeyResolver, OrphanRecordInfo
)
from src.ui.workers.mysql_worker import MySQLShellWorker
from src.core.migration_analyzer import DumpFileAnalyzer, CompatibilityIssue


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

        self.input_host = QLineEdit(DEFAULT_LOCAL_HOST)
        self.input_port = QSpinBox()
        self.input_port.setRange(1, 65535)
        self.input_port.setValue(DEFAULT_MYSQL_PORT)

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
            # 터널 선택 변경 시 Host/Port 및 자격 증명 자동 채우기
            self.combo_tunnel.currentIndexChanged.connect(self._on_tunnel_selected)

    def _on_tunnel_selected(self):
        """터널 선택 시 Host/Port 및 저장된 자격 증명 자동 채우기"""
        if not self.radio_tunnel.isChecked():
            return

        current_data = self.combo_tunnel.currentData()
        if current_data:
            # Host와 Port 업데이트
            if 'host' in current_data and 'port' in current_data:
                self.input_host.setText(current_data['host'])
                self.input_port.setValue(current_data['port'])
            # 저장된 자격 증명 자동 채우기
            if 'tunnel_id' in current_data:
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

        # 로그 수집용 변수
        self.log_entries: List[str] = []
        self.export_start_time: Optional[datetime] = None
        self.export_end_time: Optional[datetime] = None
        self.export_success: Optional[bool] = None
        self.export_schema: str = ""
        self.export_tables: List[str] = []

        # mysqlsh 설치 확인
        self.mysqlsh_installed, self.mysqlsh_msg = check_mysqlsh()

        self.init_ui()
        self.load_schemas()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # QSplitter로 상하 분할 (설정 영역 / 진행 상황 영역)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(self.splitter)

        # ========== 상단: 설정 영역 (스크롤 가능) ==========
        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 0, 0, 0)

        # 접기/펼치기 버튼 추가
        collapse_layout = QHBoxLayout()
        self.btn_collapse = QPushButton("🔽 설정 접기")
        self.btn_collapse.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        self.btn_collapse.clicked.connect(self.toggle_config_section)
        self.btn_collapse.setVisible(False)  # 초기에는 숨김
        collapse_layout.addWidget(self.btn_collapse)
        collapse_layout.addStretch()
        config_layout.addLayout(collapse_layout)

        # 설정 내용을 담을 컨테이너
        self.config_container = QWidget()
        container_layout = QVBoxLayout(self.config_container)
        container_layout.setContentsMargins(0, 0, 0, 0)

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
        container_layout.addWidget(status_group)

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
        container_layout.addWidget(type_group)

        # --- 스키마 선택 ---
        schema_layout = QHBoxLayout()
        schema_layout.addWidget(QLabel("Schema:"))
        self.combo_schema = QComboBox()
        self.combo_schema.setMinimumWidth(300)
        self.combo_schema.currentTextChanged.connect(self.on_schema_changed)
        schema_layout.addWidget(self.combo_schema)
        schema_layout.addStretch()
        container_layout.addLayout(schema_layout)

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
        container_layout.addWidget(self.table_group)

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

        container_layout.addWidget(option_group)

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

        container_layout.addWidget(folder_group)

        # 초기 출력 경로 설정
        self._load_naming_settings()
        self._update_output_dir_preview()

        # 설정 컨테이너를 config_layout에 추가
        config_layout.addWidget(self.config_container)
        config_layout.addStretch()

        # 스크롤 영역으로 감싸기
        scroll_area = QScrollArea()
        scroll_area.setWidget(config_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.splitter.addWidget(scroll_area)

        # ========== 하단: 진행 상황 영역 ==========
        progress_widget = QWidget()
        progress_main_layout = QVBoxLayout(progress_widget)
        progress_main_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter.addWidget(progress_widget)

        # --- 진행 상황 (개선된 UI) ---
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
        self.label_speed = QLabel("⚡ 속도: 0 MB/s")
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

        progress_main_layout.addWidget(self.progress_group)

        # --- 테이블 상태 목록 (GitHub Actions 스타일) ---
        self.table_status_group = QGroupBox("테이블 Export 상태")
        self.table_status_group.setVisible(False)
        table_status_layout = QVBoxLayout(self.table_status_group)

        self.table_list = QListWidget()
        self.table_list.setMinimumHeight(150)
        self.table_list.setMaximumHeight(200)
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
        """)
        table_status_layout.addWidget(self.table_list)
        progress_main_layout.addWidget(self.table_status_group)

        # 테이블 아이템 매핑 (테이블명 -> QListWidgetItem)
        self.table_items = {}

        # --- 실행 로그 (터미널 스타일) ---
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
        progress_main_layout.addWidget(self.log_group)

        # Splitter 초기 비율 설정 (설정:진행 = 60:40)
        self.splitter.setStretchFactor(0, 60)
        self.splitter.setStretchFactor(1, 40)

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

        self.btn_save_log = QPushButton("📄 로그 저장")
        self.btn_save_log.setStyleSheet("""
            QPushButton {
                background-color: #9b59b6; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #8e44ad; }
            QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }
        """)
        self.btn_save_log.clicked.connect(self.save_log)
        self.btn_save_log.setEnabled(False)
        self.btn_save_log.setToolTip("Export 완료 후 로그를 파일로 저장할 수 있습니다.")

        btn_cancel = QPushButton("닫기")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.close)

        button_layout.addWidget(self.btn_save_log)
        button_layout.addStretch()
        button_layout.addWidget(self.btn_export)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def toggle_config_section(self):
        """설정 섹션 접기/펼치기"""
        is_visible = self.config_container.isVisible()

        if is_visible:
            # 접기
            self.config_container.setVisible(False)
            self.btn_collapse.setText("🔼 설정 펼치기")
        else:
            # 펼치기
            self.config_container.setVisible(True)
            self.btn_collapse.setText("🔽 설정 접기")

    def collapse_config_section(self):
        """설정 섹션을 접음 (Export 시작 시)"""
        self.config_container.setVisible(False)
        self.btn_collapse.setText("🔼 설정 펼치기")
        self.btn_collapse.setVisible(True)

        # Splitter 비율 조정 (설정:진행 = 10:90)
        total_height = self.splitter.height()
        self.splitter.setSizes([int(total_height * 0.1), int(total_height * 0.9)])

    def expand_config_section(self):
        """설정 섹션을 펼침 (Export 완료 시)"""
        self.config_container.setVisible(True)
        self.btn_collapse.setText("🔽 설정 접기")

        # Splitter 비율 복원 (설정:진행 = 60:40)
        total_height = self.splitter.height()
        self.splitter.setSizes([int(total_height * 0.6), int(total_height * 0.4)])

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

        # 로그 수집 초기화
        self.log_entries.clear()
        self.export_start_time = datetime.now()
        self.export_end_time = None
        self.export_success = None
        self.export_schema = schema
        self.export_tables = self.get_selected_tables() if self.radio_partial.isChecked() else []
        self.btn_save_log.setEnabled(False)

        # 로그 헤더 추가
        self._add_log(f"{'='*60}")
        self._add_log("MySQL Shell Export 시작")
        self._add_log(f"시작 시간: {self.export_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._add_log(f"스키마: {schema}")
        self._add_log(f"Export 유형: {'전체 스키마' if self.radio_full.isChecked() else '선택 테이블'}")
        if self.radio_partial.isChecked():
            self._add_log(f"선택 테이블: {', '.join(self.export_tables)}")
        self._add_log(f"출력 폴더: {output_dir}")
        self._add_log(f"병렬 스레드: {self.spin_threads.value()}")
        self._add_log(f"압축 방식: {self.combo_compression.currentText()}")
        self._add_log(f"{'='*60}")

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)

        # 설정 섹션 접기
        self.collapse_config_section()

        # 진행 상황 UI 표시
        self.progress_group.setVisible(True)
        self.table_status_group.setVisible(True)
        self.log_group.setVisible(True)
        self.txt_log.clear()
        self.table_list.clear()
        self.table_items.clear()

        # 프로그레스 바 초기화
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.label_percent.setText("📊 진행률: 0%")
        self.label_data.setText("📦 데이터: 0 MB / 0 MB")
        self.label_speed.setText("⚡ 속도: 0 MB/s")
        self.label_tables.setText("📋 테이블: 0 / 0 완료")
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

        # 시그널 연결
        self.worker.progress.connect(self.on_progress)
        self.worker.table_progress.connect(self.on_table_progress)
        self.worker.detail_progress.connect(self.on_detail_progress)
        self.worker.table_status.connect(self.on_table_status)
        self.worker.raw_output.connect(self.on_raw_output)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def _add_log(self, msg: str):
        """로그 항목 추가 (수집용)"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {msg}"
        self.log_entries.append(log_entry)

    def on_progress(self, msg: str):
        self.txt_log.addItem(msg)
        self.txt_log.scrollToBottom()
        self._add_log(msg)

    def on_table_progress(self, current: int, total: int, table_name: str):
        """테이블별 진행률 업데이트"""
        # 프로그레스 바 최대값 설정 (처음 호출 시)
        if self.progress_bar.maximum() != total:
            self.progress_bar.setMaximum(total)

        self.progress_bar.setValue(current)
        self.label_tables.setText(f"📋 테이블: {current} / {total} 완료")
        self.label_status.setText(f"✅ {table_name} ({current}/{total})")
        self._add_log(f"테이블 완료: {table_name} ({current}/{total})")

    def on_finished(self, success: bool, message: str):
        # 로그 기록
        self.export_end_time = datetime.now()
        self.export_success = success

        self._add_log(f"{'='*60}")
        self._add_log(f"Export {'성공' if success else '실패'}")
        self._add_log(f"종료 시간: {self.export_end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        if self.export_start_time:
            elapsed = self.export_end_time - self.export_start_time
            self._add_log(f"소요 시간: {elapsed}")
        self._add_log(f"결과 메시지: {message}")
        self._add_log(f"{'='*60}")

        # UI 상태 복구
        self.set_ui_enabled(True)
        self.btn_save_log.setEnabled(True)  # 로그 저장 버튼 활성화

        # 설정 섹션 펼치기
        self.expand_config_section()

        if success:
            self.txt_log.addItem(f"✅ 완료: {message}")
            # 최종 진행률 100% 표시
            self.progress_bar.setValue(100)
            self.progress_bar.setMaximum(100)  # 퍼센트 기준으로 재설정
            self.label_percent.setText("📊 진행률: 100%")
            self.label_data.setText("📦 데이터: Export 완료")
            self.label_speed.setText("⚡ 속도: -")
            self.label_status.setText("✅ Export 완료")
            # 테이블 완료 수 계산 (done 상태인 테이블 수)
            done_count = sum(1 for item in self.table_items.values()
                           if item.text().startswith("✅"))
            total_count = len(self.table_items)
            if total_count > 0:
                self.label_tables.setText(f"📋 테이블: {done_count} / {total_count} 완료")
            QMessageBox.information(
                self, "Export 완료",
                f"✅ Export가 완료되었습니다.\n\n폴더: {self.input_output_dir.text()}"
            )
        else:
            self.txt_log.addItem(f"❌ 실패: {message}")
            self.label_data.setText("📦 데이터: Export 실패")
            self.label_speed.setText("⚡ 속도: -")
            self.label_status.setText("❌ Export 실패")
            # 테이블 완료 수 계산
            done_count = sum(1 for item in self.table_items.values()
                           if item.text().startswith("✅"))
            total_count = len(self.table_items)
            if total_count > 0:
                self.label_tables.setText(f"📋 테이블: {done_count} / {total_count} 완료")
            QMessageBox.warning(self, "Export 실패", f"❌ {message}")

            # GitHub 이슈 자동 보고
            self._report_error_to_github("export", message)

    def on_detail_progress(self, info: dict):
        """상세 진행 정보 업데이트"""
        percent = info.get('percent', 0)
        mb_done = info.get('mb_done', 0)
        mb_total = info.get('mb_total', 0)
        speed = info.get('speed', '0 B/s')

        self.progress_bar.setValue(percent)
        self.label_percent.setText(f"📊 진행률: {percent}%")

        # Export는 데이터 크기를 표시하지 않음 (rows만 표시되므로)
        if mb_done == 0 and mb_total == 0:
            self.label_data.setText("📦 데이터: Export 진행 중...")
        else:
            self.label_data.setText(f"📦 데이터: {mb_done:.2f} MB / {mb_total:.2f} MB")

        self.label_speed.setText(f"⚡ 속도: {speed}")

    def on_table_status(self, table_name: str, status: str, message: str):
        """테이블 상태 업데이트"""
        # 상태별 아이콘 및 스타일
        status_icons = {
            'pending': '⏳',
            'loading': '🔄',
            'done': '✅',
            'error': '❌'
        }

        icon = status_icons.get(status, '❓')

        # 기존 아이템이 있으면 업데이트, 없으면 새로 생성
        if table_name in self.table_items:
            item = self.table_items[table_name]
            display_text = f"{icon} {table_name}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item.setText(display_text)
        else:
            # 새 아이템 생성
            display_text = f"{icon} {table_name}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item = QListWidgetItem(display_text)
            self.table_list.addItem(item)
            self.table_items[table_name] = item

        # 로그에 테이블 상태 변경 기록 (done/error만)
        if status in ('done', 'error'):
            status_text = '완료' if status == 'done' else f'오류: {message}'
            self._add_log(f"테이블 [{table_name}] {status_text}")

    def on_raw_output(self, line: str):
        """mysqlsh 실시간 출력 처리 (로그에 추가)"""
        # 너무 많은 로그 방지 (최대 500줄)
        if self.txt_log.count() > 500:
            self.txt_log.takeItem(0)
        self.txt_log.addItem(line)
        self.txt_log.scrollToBottom()

    def _report_error_to_github(self, error_type: str, error_message: str):
        """GitHub 이슈 자동 보고 (백그라운드)"""
        if not self.config_manager:
            return

        context = {
            'schema': self.export_schema,
            'tables': self.export_tables,
            'mode': '전체 스키마' if self.radio_full.isChecked() else '선택 테이블'
        }

        from src.ui.workers.github_worker import GitHubReportWorker
        self._github_worker = GitHubReportWorker(
            self.config_manager, error_type, error_message, context
        )
        self._github_worker.finished.connect(self._on_github_report_finished)
        self._github_worker.start()

    def _on_github_report_finished(self, success: bool, message: str):
        """GitHub 이슈 보고 완료 콜백"""
        if success:
            self._add_log(f"🐙 GitHub: {message}")
        else:
            self._add_log(f"⚠️ GitHub 이슈 보고 실패: {message}")

    def save_log(self):
        """로그를 파일로 저장"""
        if not self.log_entries:
            QMessageBox.warning(self, "로그 없음", "저장할 로그가 없습니다.")
            return

        # 기본 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        status = "success" if self.export_success else "failed"
        default_filename = f"export_log_{self.export_schema}_{status}_{timestamp}.txt"

        # 파일 저장 대화상자
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "로그 파일 저장",
            os.path.join(self._get_base_output_dir(), default_filename),
            "텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                # 헤더 정보
                f.write("=" * 70 + "\n")
                f.write("MySQL Shell Export Log\n")
                f.write("=" * 70 + "\n\n")

                f.write(f"스키마: {self.export_schema}\n")
                f.write(f"Export 유형: {'전체 스키마' if self.radio_full.isChecked() else '선택 테이블'}\n")
                if self.export_tables:
                    f.write(f"선택 테이블: {', '.join(self.export_tables)}\n")
                f.write(f"출력 폴더: {self.input_output_dir.text()}\n")
                f.write(f"연결 정보: {self.connection_info}\n")
                f.write(f"결과: {'성공 ✅' if self.export_success else '실패 ❌'}\n")

                if self.export_start_time:
                    f.write(f"시작 시간: {self.export_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.export_end_time:
                    f.write(f"종료 시간: {self.export_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.export_start_time and self.export_end_time:
                    elapsed = self.export_end_time - self.export_start_time
                    f.write(f"소요 시간: {elapsed}\n")

                f.write("\n" + "=" * 70 + "\n")
                f.write("상세 로그\n")
                f.write("=" * 70 + "\n\n")

                for entry in self.log_entries:
                    f.write(entry + "\n")

            QMessageBox.information(
                self, "저장 완료",
                f"✅ 로그가 저장되었습니다.\n\n{file_path}"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "저장 실패",
                f"❌ 로그 저장 중 오류가 발생했습니다.\n\n{str(e)}"
            )

    def closeEvent(self, event):
        if self.connector:
            self.connector.disconnect()
        event.accept()


class MySQLShellImportDialog(QDialog):
    """MySQL Shell Import 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None,
                 tunnel_config: dict = None):
        super().__init__(parent)
        self.setWindowTitle("MySQL Shell Import (병렬 처리)")
        self.resize(600, 700)

        self.connector = connector
        self.config_manager = config_manager
        self.tunnel_config = tunnel_config  # Production 환경 보호용
        self.worker: Optional[MySQLShellWorker] = None

        self.mysqlsh_installed, self.mysqlsh_msg = check_mysqlsh()

        # Import 결과 저장 (재시도용)
        self.import_results: dict = {}
        self.table_items: dict = {}  # 테이블명 -> QListWidgetItem 매핑
        self.last_input_dir: str = ""  # 마지막 사용한 input_dir
        self.last_target_schema: str = ""  # 마지막 사용한 target_schema

        # 로그 수집용 변수
        self.log_entries: List[str] = []
        self.import_start_time: Optional[datetime] = None
        self.import_end_time: Optional[datetime] = None
        self.import_success: Optional[bool] = None

        # 메타데이터 정보
        self.dump_metadata: Optional[dict] = None

        # 테이블별 chunk 진행률 추적
        self.table_chunk_progress: dict = {}  # {table_name: (completed, total)}

        self.init_ui()
        self.load_schemas()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # QSplitter로 상하 분할 (설정 영역 / 진행 상황 영역)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(self.splitter)

        # ========== 상단: 설정 영역 (스크롤 가능) ==========
        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 0, 0, 0)

        # 접기/펼치기 버튼 추가
        collapse_layout = QHBoxLayout()
        self.btn_collapse = QPushButton("🔽 설정 접기")
        self.btn_collapse.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 4px 12px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        self.btn_collapse.clicked.connect(self.toggle_config_section)
        self.btn_collapse.setVisible(False)  # 초기에는 숨김
        collapse_layout.addWidget(self.btn_collapse)
        collapse_layout.addStretch()
        config_layout.addLayout(collapse_layout)

        # 설정 내용을 담을 컨테이너
        self.config_container = QWidget()
        container_layout = QVBoxLayout(self.config_container)
        container_layout.setContentsMargins(0, 0, 0, 0)

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
        container_layout.addWidget(status_group)

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
        container_layout.addWidget(input_group)

        # --- MySQL 8.4 호환성 검사 상태 ---
        self.upgrade_check_group = QGroupBox("MySQL 8.4 호환성 검사")
        upgrade_check_layout = QVBoxLayout(self.upgrade_check_group)

        # 상태 표시 레이아웃
        status_line = QHBoxLayout()
        self.lbl_upgrade_status = QLabel("📋 Dump 폴더를 선택하면 자동 검사됩니다.")
        self.lbl_upgrade_status.setStyleSheet("color: #7f8c8d;")
        status_line.addWidget(self.lbl_upgrade_status)
        status_line.addStretch()

        # 상세 보기 버튼
        self.btn_view_issues = QPushButton("📊 상세 보기")
        self.btn_view_issues.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 4px 12px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        self.btn_view_issues.setVisible(False)
        self.btn_view_issues.clicked.connect(self._show_upgrade_issues_dialog)
        status_line.addWidget(self.btn_view_issues)

        upgrade_check_layout.addLayout(status_line)

        # 호환성 검사 결과 저장
        self._upgrade_issues: List[CompatibilityIssue] = []

        container_layout.addWidget(self.upgrade_check_group)

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

        container_layout.addWidget(schema_group)

        # --- Import 옵션 ---
        option_group = QGroupBox("Import 옵션")
        option_layout = QFormLayout(option_group)

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(4)
        option_layout.addRow("병렬 스레드:", self.spin_threads)

        container_layout.addWidget(option_group)

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

        container_layout.addWidget(tz_group)

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

        container_layout.addWidget(mode_group)

        # 설정 컨테이너를 config_layout에 추가
        config_layout.addWidget(self.config_container)
        config_layout.addStretch()

        # 스크롤 영역으로 감싸기
        scroll_area = QScrollArea()
        scroll_area.setWidget(config_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.splitter.addWidget(scroll_area)

        # ========== 하단: 진행 상황 영역 ==========
        progress_widget = QWidget()
        progress_main_layout = QVBoxLayout(progress_widget)
        progress_main_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter.addWidget(progress_widget)

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
        self.label_fk_status = QLabel("🔗 FK: 대기 중")
        self.label_fk_status.setStyleSheet("color: #7f8c8d;")
        left_detail.addWidget(self.label_percent)
        left_detail.addWidget(self.label_data)
        left_detail.addWidget(self.label_speed)
        left_detail.addWidget(self.label_tables)
        left_detail.addWidget(self.label_fk_status)

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

        progress_main_layout.addWidget(self.progress_group)

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

        progress_main_layout.addWidget(self.table_status_group)

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

        progress_main_layout.addWidget(self.log_group)

        # Splitter 초기 비율 설정 (설정:진행 = 60:40)
        self.splitter.setStretchFactor(0, 60)
        self.splitter.setStretchFactor(1, 40)

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

        self.btn_save_log = QPushButton("📄 로그 저장")
        self.btn_save_log.setStyleSheet("""
            QPushButton {
                background-color: #9b59b6; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #8e44ad; }
            QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }
        """)
        self.btn_save_log.clicked.connect(self.save_log)
        self.btn_save_log.setEnabled(False)
        self.btn_save_log.setToolTip("Import 완료 후 로그를 파일로 저장할 수 있습니다.")

        btn_cancel = QPushButton("닫기")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_cancel.clicked.connect(self.close)

        button_layout.addWidget(self.btn_save_log)
        button_layout.addStretch()
        button_layout.addWidget(self.btn_import)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

    def toggle_config_section(self):
        """설정 섹션 접기/펼치기"""
        is_visible = self.config_container.isVisible()

        if is_visible:
            # 접기
            self.config_container.setVisible(False)
            self.btn_collapse.setText("🔼 설정 펼치기")
        else:
            # 펼치기
            self.config_container.setVisible(True)
            self.btn_collapse.setText("🔽 설정 접기")

    def collapse_config_section(self):
        """설정 섹션을 접음 (Import 시작 시)"""
        self.config_container.setVisible(False)
        self.btn_collapse.setText("🔼 설정 펼치기")
        self.btn_collapse.setVisible(True)

        # Splitter 비율 조정 (설정:진행 = 10:90)
        total_height = self.splitter.height()
        self.splitter.setSizes([int(total_height * 0.1), int(total_height * 0.9)])

    def expand_config_section(self):
        """설정 섹션을 펼침 (Import 완료 시)"""
        self.config_container.setVisible(True)
        self.btn_collapse.setText("🔽 설정 접기")

        # Splitter 비율 복원 (설정:진행 = 60:40)
        total_height = self.splitter.height()
        self.splitter.setSizes([int(total_height * 0.6), int(total_height * 0.4)])

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
            # 폴더 선택 시 자동으로 MySQL 8.4 호환성 검사 실행
            self._run_upgrade_check(folder)

    def _run_upgrade_check(self, dump_path: str):
        """Import 전 MySQL 8.4 호환성 검사"""
        self.lbl_upgrade_status.setText("🔍 호환성 검사 중...")
        self.lbl_upgrade_status.setStyleSheet("color: #3498db;")
        self.btn_view_issues.setVisible(False)
        QApplication.processEvents()

        try:
            analyzer = DumpFileAnalyzer()
            result = analyzer.analyze_dump_folder(dump_path)

            self._upgrade_issues = result.compatibility_issues
            error_count = sum(1 for i in self._upgrade_issues if i.severity == "error")
            warning_count = sum(1 for i in self._upgrade_issues if i.severity == "warning")

            if error_count > 0:
                self.lbl_upgrade_status.setText(
                    f"⚠️ 호환성 이슈: {error_count}개 오류, {warning_count}개 경고"
                )
                self.lbl_upgrade_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
                self.btn_view_issues.setVisible(True)
            elif warning_count > 0:
                self.lbl_upgrade_status.setText(
                    f"⚠️ 호환성 경고: {warning_count}개 (Import 가능)"
                )
                self.lbl_upgrade_status.setStyleSheet("color: #f39c12;")
                self.btn_view_issues.setVisible(True)
            else:
                self.lbl_upgrade_status.setText("✅ 호환성 검사 통과")
                self.lbl_upgrade_status.setStyleSheet("color: #27ae60;")
                self.btn_view_issues.setVisible(False)

        except Exception as e:
            self.lbl_upgrade_status.setText(f"❌ 검사 실패: {str(e)}")
            self.lbl_upgrade_status.setStyleSheet("color: #e74c3c;")
            self._upgrade_issues = []

    def _show_upgrade_issues_dialog(self):
        """호환성 이슈 상세 다이얼로그 표시"""
        if not self._upgrade_issues:
            return

        from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView

        dialog = QDialog(self)
        dialog.setWindowTitle("MySQL 8.4 호환성 이슈 상세")
        dialog.resize(800, 500)

        layout = QVBoxLayout(dialog)

        # 요약
        error_count = sum(1 for i in self._upgrade_issues if i.severity == "error")
        warning_count = sum(1 for i in self._upgrade_issues if i.severity == "warning")
        info_count = sum(1 for i in self._upgrade_issues if i.severity == "info")

        summary_label = QLabel(
            f"<b>총 {len(self._upgrade_issues)}개 이슈</b>: "
            f"<span style='color:red'>❌ 오류 {error_count}</span>, "
            f"<span style='color:orange'>⚠️ 경고 {warning_count}</span>, "
            f"<span style='color:blue'>ℹ️ 정보 {info_count}</span>"
        )
        layout.addWidget(summary_label)

        # 테이블
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["심각도", "유형", "위치", "설명", "권장 조치"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setRowCount(len(self._upgrade_issues))

        severity_icons = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}

        for i, issue in enumerate(self._upgrade_issues):
            severity_text = f"{severity_icons.get(issue.severity, '')} {issue.severity.upper()}"
            table.setItem(i, 0, QTableWidgetItem(severity_text))
            table.setItem(i, 1, QTableWidgetItem(issue.issue_type.value))
            table.setItem(i, 2, QTableWidgetItem(issue.location))
            table.setItem(i, 3, QTableWidgetItem(issue.description))
            table.setItem(i, 4, QTableWidgetItem(issue.suggestion))

        layout.addWidget(table)

        # 닫기 버튼
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(dialog.close)
        layout.addWidget(btn_close)

        dialog.exec()

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

    def _add_log(self, msg: str):
        """로그 항목 추가 (수집용)"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {msg}"
        self.log_entries.append(log_entry)

    def _get_dump_schema_name(self, dump_dir: str) -> str:
        """덤프 디렉토리의 @.done.json에서 원본 스키마명 읽기"""
        try:
            import json
            done_json_path = os.path.join(dump_dir, '@.done.json')
            if not os.path.exists(done_json_path):
                return ""
            with open(done_json_path, 'r', encoding='utf-8') as f:
                done_data = json.load(f)
            table_data_bytes = done_data.get('tableDataBytes', {})
            for schema_name in table_data_bytes.keys():
                return schema_name
            return ""
        except Exception:
            return ""

    def _get_import_mode_text(self) -> str:
        """현재 선택된 Import 모드 텍스트 반환"""
        if self.radio_replace.isChecked():
            return "전체 교체 Import"
        elif self.radio_recreate.isChecked():
            return "완전 재생성 Import"
        return "증분 Import (병합)"

    def do_import(self, retry_tables: list = None):
        """Import 실행 (retry_tables가 주어지면 해당 테이블만 재시도)"""
        input_dir = self.input_dir.text()

        if not input_dir:
            QMessageBox.warning(self, "오류", "Dump 폴더를 선택하세요.")
            return

        if not os.path.exists(input_dir):
            QMessageBox.warning(self, "오류", "폴더가 존재하지 않습니다.")
            return

        target_schema = None
        if not self.chk_use_original.isChecked():
            target_schema = self.combo_target_schema.currentText()
            if not target_schema:
                QMessageBox.warning(self, "오류", "대상 스키마를 선택하세요.")
                return

        # Production 환경 확인
        if self.tunnel_config:
            from src.core.production_guard import ProductionGuard
            guard = ProductionGuard(self)

            if target_schema:
                schema_name = target_schema
            else:
                # 원본 스키마명 사용 - 덤프에서 실제 스키마명 읽기
                schema_name = self._get_dump_schema_name(input_dir) or "(원본 스키마)"
            details = (f"Dump 폴더: {input_dir}<br>"
                      f"Import 모드: {self._get_import_mode_text()}")

            if not guard.confirm_dangerous_operation(
                self.tunnel_config, "데이터 Import", schema_name, details
            ):
                return  # 사용자가 취소

        # 저장 (재시도용)
        self.last_input_dir = input_dir
        self.last_target_schema = target_schema

        # UI 상태 변경 - 모든 입력 비활성화
        self.set_ui_enabled(False)
        self.btn_retry.setVisible(False)
        self.btn_select_failed.setVisible(False)
        self.btn_save_log.setEnabled(False)

        # 설정 섹션 접기
        self.collapse_config_section()

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
            # 로그 수집 초기화
            self.log_entries.clear()
            self.import_start_time = datetime.now()
            self.import_end_time = None
            self.import_success = None

            # Import 모드 결정
            import_mode_str = "증분 Import (병합)"
            if self.radio_replace.isChecked():
                import_mode_str = "전체 교체 Import"
            elif self.radio_recreate.isChecked():
                import_mode_str = "완전 재생성 Import"

            # 로그 헤더 추가
            self._add_log(f"{'='*60}")
            self._add_log("MySQL Shell Import 시작")
            self._add_log(f"시작 시간: {self.import_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            self._add_log(f"Dump 폴더: {input_dir}")
            self._add_log(f"대상 스키마: {target_schema if target_schema else '원본 스키마명 사용'}")
            self._add_log(f"Import 모드: {import_mode_str}")
            self._add_log(f"병렬 스레드: {self.spin_threads.value()}")
            self._add_log(f"{'='*60}")

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
        self.worker.metadata_analyzed.connect(self.on_metadata_analyzed)
        self.worker.table_chunk_progress.connect(self.on_table_chunk_progress)
        self.worker.start()

    def on_progress(self, msg: str):
        """일반 진행 메시지 처리"""
        self.txt_log.addItem(msg)
        self.txt_log.scrollToBottom()
        self.label_status.setText(msg)
        self._add_log(msg)

        # FK 관련 메시지 감지 시 FK 상태 라벨 업데이트
        if "FK 제약조건" in msg or "FK 재연결" in msg:
            self.label_fk_status.setText(msg)
            if "백업 중" in msg or "재연결 중" in msg:
                self.label_fk_status.setStyleSheet("color: #3498db; font-weight: bold;")
            elif "완료" in msg:
                if "실패 0" in msg or ("성공" in msg and "실패" not in msg):
                    self.label_fk_status.setStyleSheet("color: #27ae60; font-weight: bold;")
                else:
                    self.label_fk_status.setStyleSheet("color: #e67e22; font-weight: bold;")

    def on_table_progress(self, current: int, total: int, table_name: str):
        """테이블별 진행률 업데이트"""
        self.label_tables.setText(f"📋 테이블: {current} / {total} 완료")
        self._add_log(f"테이블 완료: {table_name} ({current}/{total})")

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
        """테이블 상태 업데이트 (메타데이터 정보 포함)"""
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
        # color는 향후 테이블 행 스타일링에 사용 예정
        _color = status_colors.get(status, '#7f8c8d')  # noqa: F841

        # 메타데이터에서 테이블 정보 가져오기
        size_info = ""
        chunk_info = ""
        if self.dump_metadata and 'table_sizes' in self.dump_metadata:
            size_bytes = self.dump_metadata['table_sizes'].get(table_name, 0)
            chunk_count = self.dump_metadata['chunk_counts'].get(table_name, 1)

            if size_bytes > 0:
                size_mb = size_bytes / (1024 * 1024)
                if size_mb < 1024:
                    size_str = f"{size_mb:.1f} MB"
                else:
                    size_str = f"{size_mb / 1024:.2f} GB"
                size_info = f" ({size_str})"

                # chunk 진행률 표시 (loading 상태이고 chunk가 2개 이상인 경우)
                if status == 'loading' and chunk_count > 1 and table_name in self.table_chunk_progress:
                    completed, total = self.table_chunk_progress[table_name]
                    chunk_percent = (completed / total * 100) if total > 0 else 0
                    chunk_info = f" [{completed}/{total} chunks, {chunk_percent:.0f}%]"

        # 기존 아이템이 있으면 업데이트, 없으면 새로 생성
        if table_name in self.table_items:
            item = self.table_items[table_name]
            display_text = f"{icon} {table_name}{size_info}{chunk_info}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item.setText(display_text)
            item.setForeground(Qt.GlobalColor.black)
        else:
            display_text = f"{icon} {table_name}{size_info}{chunk_info}"
            if status == 'error' and message:
                display_text += f" - {message[:50]}..."
            item = QListWidgetItem(display_text)
            self.table_list.addItem(item)
            self.table_items[table_name] = item

        # 결과 저장
        self.import_results[table_name] = {'status': status, 'message': message}

        # 로그에 테이블 상태 변경 기록 (done/error만)
        if status in ('done', 'error'):
            status_text = '완료' if status == 'done' else f'오류: {message}'
            self._add_log(f"테이블 [{table_name}] {status_text}")

    def on_table_chunk_progress(self, table_name: str, completed_chunks: int, total_chunks: int):
        """
        테이블별 chunk 진행률 업데이트 (다중 파일 병렬 다운로드 스타일)

        Args:
            table_name: 테이블명
            completed_chunks: 완료된 chunk 수
            total_chunks: 전체 chunk 수
        """
        # 진행률 저장
        self.table_chunk_progress[table_name] = (completed_chunks, total_chunks)

        # 테이블 아이템이 존재하면 업데이트
        if table_name in self.table_items:
            item = self.table_items[table_name]

            # 현재 상태 확인
            current_status = self.import_results.get(table_name, {}).get('status', 'loading')
            status_icons = {
                'pending': '⏳',
                'loading': '🔄',
                'done': '✅',
                'error': '❌'
            }
            icon = status_icons.get(current_status, '❓')

            # 크기 정보
            size_info = ""
            if self.dump_metadata and 'table_sizes' in self.dump_metadata:
                size_bytes = self.dump_metadata['table_sizes'].get(table_name, 0)
                if size_bytes > 0:
                    size_mb = size_bytes / (1024 * 1024)
                    if size_mb < 1024:
                        size_str = f"{size_mb:.1f} MB"
                    else:
                        size_str = f"{size_mb / 1024:.2f} GB"
                    size_info = f" ({size_str})"

            # chunk 진행률 표시
            chunk_percent = (completed_chunks / total_chunks * 100) if total_chunks > 0 else 0
            if total_chunks > 1:
                # 다중 chunk 테이블: "🔄 df_subs (1.29 GB) [45/81 chunks, 55%]"
                chunk_info = f" [{completed_chunks}/{total_chunks} chunks, {chunk_percent:.0f}%]"
            else:
                # 단일 chunk 테이블: 진행률 표시 안 함
                chunk_info = ""

            display_text = f"{icon} {table_name}{size_info}{chunk_info}"

            # error 상태이면 메시지 추가
            if current_status == 'error':
                message = self.import_results.get(table_name, {}).get('message', '')
                if message:
                    display_text += f" - {message[:50]}..."

            item.setText(display_text)

    def on_raw_output(self, line: str):
        """mysqlsh 실시간 출력 처리 (로그에 추가)"""
        # 너무 많은 로그 방지 (최대 500줄)
        if self.txt_log.count() > 500:
            self.txt_log.takeItem(0)
        self.txt_log.addItem(line)
        self.txt_log.scrollToBottom()
        # raw output도 로그에 기록
        self._add_log(f"[mysqlsh] {line}")

    def on_metadata_analyzed(self, metadata: dict):
        """
        Dump 메타데이터 분석 결과 처리

        메타데이터 구조:
        {
            'chunk_counts': {'table_name': chunk_count, ...},
            'table_sizes': {'table_name': bytes, ...},
            'total_bytes': int,
            'schema': str
        }
        """
        self.dump_metadata = metadata

        # 대용량 테이블 정보를 테이블 상태 목록에 표시
        if metadata and 'table_sizes' in metadata:
            large_tables = [
                (name, size, metadata['chunk_counts'].get(name, 1))
                for name, size in metadata['table_sizes'].items()
                if size > 50_000_000  # 50MB 이상
            ]
            large_tables.sort(key=lambda x: -x[1])

            if large_tables:
                # 상위 대용량 테이블을 미리 표시 (pending 상태로)
                for table_name, size_bytes, chunk_count in large_tables[:10]:
                    size_mb = size_bytes / (1024 * 1024)
                    if size_mb < 1024:
                        size_str = f"{size_mb:.1f} MB"
                    else:
                        size_str = f"{size_mb / 1024:.2f} GB"

                    display = f"⏳ {table_name} ({size_str}, {chunk_count} chunks)"
                    item = QListWidgetItem(display)
                    item.setForeground(Qt.GlobalColor.gray)
                    self.table_list.addItem(item)
                    self.table_items[table_name] = item

    def on_import_finished(self, success: bool, message: str, results: dict):
        """Import 완료 처리 (결과 저장 및 재시도 버튼 표시)"""
        self.import_results = results

        # 실패한 테이블이 있는지 확인 (fk_restore는 제외)
        failed_tables = [
            t for t, r in results.items()
            if t != 'fk_restore' and isinstance(r, dict) and r.get('status') == 'error'
        ]

        if failed_tables:
            self.btn_retry.setVisible(True)
            self.btn_select_failed.setVisible(True)
            self.txt_log.addItem(f"⚠️ {len(failed_tables)}개 테이블 Import 실패")

    def on_finished(self, success: bool, message: str):
        """작업 완료 처리"""
        # 로그 기록
        self.import_end_time = datetime.now()
        self.import_success = success

        # 결과 요약 (fk_restore 제외)
        table_results = {k: v for k, v in self.import_results.items() if k != 'fk_restore' and isinstance(v, dict)}
        done_count = sum(1 for r in table_results.values() if r.get('status') == 'done')
        error_count = sum(1 for r in table_results.values() if r.get('status') == 'error')
        total_count = len(table_results)

        self._add_log(f"{'='*60}")
        self._add_log(f"Import {'성공' if success else '실패'}")
        self._add_log(f"종료 시간: {self.import_end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        if self.import_start_time:
            elapsed = self.import_end_time - self.import_start_time
            self._add_log(f"소요 시간: {elapsed}")
        self._add_log(f"성공: {done_count}개 테이블")
        self._add_log(f"실패: {error_count}개 테이블")

        # FK 복원 결과 표시
        fk_restore = self.import_results.get('fk_restore', {})
        if fk_restore:
            fk_success = fk_restore.get('success', 0)
            fk_fail = fk_restore.get('fail', 0)
            fk_msg = f"🔗 FK 재연결: 성공 {fk_success}, 실패 {fk_fail}"
            self.label_fk_status.setText(fk_msg)
            self._add_log(f"FK 재연결: 성공 {fk_success}, 실패 {fk_fail}")

            if fk_fail > 0:
                self.label_fk_status.setStyleSheet("color: #e74c3c; font-weight: bold;")
                self._add_log("⚠️ 일부 FK 연결에 실패했습니다:")
                for err in fk_restore.get('errors', []):
                    self._add_log(f"  - {err.get('constraint_name', 'unknown')}: {err.get('error', '')}")
            else:
                self.label_fk_status.setStyleSheet("color: #27ae60; font-weight: bold;")
        else:
            self.label_fk_status.setText("🔗 FK: -")
            self.label_fk_status.setStyleSheet("color: #7f8c8d;")

        self._add_log(f"결과 메시지: {message}")
        self._add_log(f"{'='*60}")

        # UI 상태 복구
        self.set_ui_enabled(True)
        self.btn_save_log.setEnabled(True)  # 로그 저장 버튼 활성화

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

            # GitHub 이슈 자동 보고
            self._report_error_to_github("import", message, error_count)

    def _report_error_to_github(self, error_type: str, error_message: str, error_count: int = 0):
        """GitHub 이슈 자동 보고 (백그라운드)"""
        if not self.config_manager:
            return

        # 실패한 테이블 목록
        failed_tables = [t for t, r in self.import_results.items() if r.get('status') == 'error']
        failed_messages = [r.get('message', '') for t, r in self.import_results.items() if r.get('status') == 'error']

        # 컨텍스트 정보 수집
        target_schema = self.combo_target_schema.currentText() if not self.chk_use_original.isChecked() else "(원본 스키마)"
        context = {
            'schema': target_schema,
            'failed_tables': failed_tables,
            'mode': self._get_import_mode_text()
        }

        # 오류 메시지 조합 (첫 3개 실패 메시지)
        combined_error = error_message
        if failed_messages:
            combined_error += "\n\n실패한 테이블 오류:\n" + "\n".join(failed_messages[:3])

        from src.ui.workers.github_worker import GitHubReportWorker
        self._github_worker = GitHubReportWorker(
            self.config_manager, error_type, combined_error, context
        )
        self._github_worker.finished.connect(self._on_github_report_finished)
        self._github_worker.start()

    def _on_github_report_finished(self, success: bool, message: str):
        """GitHub 이슈 보고 완료 콜백"""
        if success:
            self._add_log(f"🐙 GitHub: {message}")
        else:
            self._add_log(f"⚠️ GitHub 이슈 보고 실패: {message}")

    def _get_import_mode_text(self) -> str:
        """Import 모드 텍스트 반환"""
        if self.radio_merge.isChecked():
            return "merge (기존 데이터 유지)"
        elif self.radio_replace.isChecked():
            return "replace (기존 테이블 삭제)"
        else:
            return "recreate (스키마 재생성)"

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

    def save_log(self):
        """로그를 파일로 저장"""
        if not self.log_entries:
            QMessageBox.warning(self, "로그 없음", "저장할 로그가 없습니다.")
            return

        # 기본 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        status = "success" if self.import_success else "failed"

        # 스키마 이름 추출 (폴더명에서)
        schema_name = "unknown"
        if self.last_input_dir:
            schema_name = os.path.basename(self.last_input_dir).split('_')[0]
        if self.last_target_schema:
            schema_name = self.last_target_schema

        default_filename = f"import_log_{schema_name}_{status}_{timestamp}.txt"

        # 기본 저장 경로
        default_dir = os.path.dirname(self.last_input_dir) if self.last_input_dir else os.path.expanduser("~")

        # 파일 저장 대화상자
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "로그 파일 저장",
            os.path.join(default_dir, default_filename),
            "텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            # 결과 요약
            done_count = sum(1 for r in self.import_results.values() if r.get('status') == 'done')
            error_count = sum(1 for r in self.import_results.values() if r.get('status') == 'error')
            total_count = len(self.import_results)

            with open(file_path, 'w', encoding='utf-8') as f:
                # 헤더 정보
                f.write("=" * 70 + "\n")
                f.write("MySQL Shell Import Log\n")
                f.write("=" * 70 + "\n\n")

                f.write(f"Dump 폴더: {self.last_input_dir}\n")
                f.write(f"대상 스키마: {self.last_target_schema if self.last_target_schema else '원본 스키마명 사용'}\n")
                f.write(f"결과: {'성공 ✅' if self.import_success else '실패 ❌'}\n")
                f.write(f"테이블 통계: 성공 {done_count}개, 실패 {error_count}개, 총 {total_count}개\n")

                if self.import_start_time:
                    f.write(f"시작 시간: {self.import_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.import_end_time:
                    f.write(f"종료 시간: {self.import_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if self.import_start_time and self.import_end_time:
                    elapsed = self.import_end_time - self.import_start_time
                    f.write(f"소요 시간: {elapsed}\n")

                # 실패한 테이블 목록
                if error_count > 0:
                    f.write("\n" + "-" * 70 + "\n")
                    f.write("실패한 테이블 목록\n")
                    f.write("-" * 70 + "\n")
                    for table_name, result in self.import_results.items():
                        if result.get('status') == 'error':
                            f.write(f"  ❌ {table_name}: {result.get('message', 'Unknown error')}\n")

                f.write("\n" + "=" * 70 + "\n")
                f.write("상세 로그\n")
                f.write("=" * 70 + "\n\n")

                for entry in self.log_entries:
                    f.write(entry + "\n")

            QMessageBox.information(
                self, "저장 완료",
                f"✅ 로그가 저장되었습니다.\n\n{file_path}"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "저장 실패",
                f"❌ 로그 저장 중 오류가 발생했습니다.\n\n{str(e)}"
            )

    def closeEvent(self, event):
        if self.connector:
            self.connector.disconnect()
        event.accept()


class MySQLShellWizard:
    """MySQL Shell Export/Import 마법사"""

    def __init__(self, parent=None, tunnel_engine=None, config_manager=None, preselected_tunnel=None):
        self.parent = parent
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.preselected_tunnel = preselected_tunnel

    def _connect_preselected_tunnel(self) -> tuple:
        """미리 선택된 터널로 연결 - (connector, connection_info) 반환"""
        if not self.preselected_tunnel:
            return None, None

        tunnel = self.preselected_tunnel
        tid = tunnel.get('id')
        is_direct = tunnel.get('connection_mode') == 'direct'

        # 자격 증명 가져오기
        db_user, db_password = self.config_manager.get_tunnel_credentials(tid)
        if not db_user:
            QMessageBox.warning(
                self.parent, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다."
            )
            return None, None

        # 연결 정보 결정
        if is_direct:
            host = tunnel['remote_host']
            port = int(tunnel['remote_port'])
        elif self.tunnel_engine.is_running(tid):
            host, port = self.tunnel_engine.get_connection_info(tid)
        else:
            QMessageBox.warning(
                self.parent, "경고",
                "터널이 활성화되어 있지 않습니다."
            )
            return None, None

        # MySQLConnector 생성 및 연결
        connector = MySQLConnector(host, port, db_user, db_password)
        success, msg = connector.connect()

        if not success:
            QMessageBox.critical(
                self.parent, "연결 오류",
                f"DB 연결에 실패했습니다:\n{msg}"
            )
            return None, None

        # 연결 식별자 (Export 폴더명 등에 사용)
        connection_info = f"{tunnel.get('name', 'Unknown')}_{db_user}"

        return connector, connection_info

    def start_export(self) -> bool:
        """Export 마법사 시작"""
        connector = None
        connection_info = None

        # 미리 선택된 터널이 있으면 바로 연결
        if self.preselected_tunnel:
            connector, connection_info = self._connect_preselected_tunnel()
            if not connector:
                return False
        else:
            # 1단계: DB 연결 다이얼로그
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
        connector = None

        # 미리 선택된 터널이 있으면 바로 연결
        if self.preselected_tunnel:
            connector, _ = self._connect_preselected_tunnel()
            if not connector:
                return False
        else:
            # 1단계: DB 연결 다이얼로그
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
            config_manager=self.config_manager,
            tunnel_config=self.preselected_tunnel  # Production 환경 보호용
        )
        import_dialog.exec()

        return True

    def start_orphan_check(self) -> bool:
        """고아 레코드 검사 마법사 시작"""
        connector = None

        # 미리 선택된 터널이 있으면 바로 연결
        if self.preselected_tunnel:
            connector, _ = self._connect_preselected_tunnel()
            if not connector:
                return False
        else:
            # 1단계: DB 연결 다이얼로그
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

        # 2단계: 고아 레코드 검사
        orphan_dialog = OrphanRecordDialog(
            self.parent,
            connector=connector,
            config_manager=self.config_manager
        )
        orphan_dialog.exec()

        return True


class OrphanRecordDialog(QDialog):
    """고아 레코드 분석 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None):
        super().__init__(parent)
        self.connector = connector
        self.config_manager = config_manager
        self.resolver: Optional[ForeignKeyResolver] = None
        self.orphan_results: List[OrphanRecordInfo] = []

        self.setWindowTitle("🔍 고아 레코드 분석")
        self.setMinimumSize(900, 650)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # === 상단: 스키마 선택 ===
        schema_group = QGroupBox("스키마 선택")
        schema_layout = QHBoxLayout(schema_group)

        self.schema_combo = QComboBox()
        self.schema_combo.setMinimumWidth(200)
        schema_layout.addWidget(QLabel("스키마:"))
        schema_layout.addWidget(self.schema_combo)

        self.analyze_btn = QPushButton("🔍 분석 시작")
        self.analyze_btn.clicked.connect(self.start_analysis)
        schema_layout.addWidget(self.analyze_btn)

        schema_layout.addStretch()
        layout.addWidget(schema_group)

        # === 중앙: 결과 영역 ===
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 왼쪽: 고아 관계 목록
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_layout.addWidget(QLabel("발견된 고아 관계:"))
        self.result_list = QListWidget()
        self.result_list.currentRowChanged.connect(self.on_result_selected)
        left_layout.addWidget(self.result_list)

        splitter.addWidget(left_widget)

        # 오른쪽: 상세 정보
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel("상세 정보 / SQL 쿼리:"))

        from PyQt6.QtWidgets import QTextEdit
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        right_layout.addWidget(self.detail_text)

        # 쿼리 복사 버튼
        copy_btn_layout = QHBoxLayout()
        self.copy_query_btn = QPushButton("📋 쿼리 복사")
        self.copy_query_btn.clicked.connect(self.copy_current_query)
        self.copy_query_btn.setEnabled(False)
        copy_btn_layout.addWidget(self.copy_query_btn)
        copy_btn_layout.addStretch()
        right_layout.addLayout(copy_btn_layout)

        splitter.addWidget(right_widget)
        splitter.setSizes([350, 550])

        layout.addWidget(splitter, stretch=1)

        # === 하단: 진행상황 및 버튼 ===
        progress_layout = QHBoxLayout()
        self.progress_label = QLabel("")
        progress_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        layout.addLayout(progress_layout)

        # 버튼 영역
        btn_layout = QHBoxLayout()

        self.export_all_queries_btn = QPushButton("📄 전체 쿼리 내보내기")
        self.export_all_queries_btn.clicked.connect(self.export_all_queries)
        self.export_all_queries_btn.setEnabled(False)
        btn_layout.addWidget(self.export_all_queries_btn)

        self.export_report_btn = QPushButton("📊 보고서 저장")
        self.export_report_btn.clicked.connect(self.export_report)
        self.export_report_btn.setEnabled(False)
        btn_layout.addWidget(self.export_report_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

        # 스키마 목록 로드
        self.load_schemas()

    def load_schemas(self):
        """스키마 목록 로드"""
        if not self.connector:
            return

        try:
            schemas = self.connector.get_schemas()
            self.schema_combo.clear()
            self.schema_combo.addItems(schemas)
        except Exception as e:
            QMessageBox.warning(self, "경고", f"스키마 목록 로드 실패:\n{str(e)}")

    def start_analysis(self):
        """고아 레코드 분석 시작"""
        schema = self.schema_combo.currentText()
        if not schema:
            QMessageBox.warning(self, "경고", "스키마를 선택해주세요.")
            return

        self.result_list.clear()
        self.detail_text.clear()
        self.orphan_results.clear()
        self.copy_query_btn.setEnabled(False)
        self.export_all_queries_btn.setEnabled(False)
        self.export_report_btn.setEnabled(False)

        self.analyze_btn.setEnabled(False)
        self.progress_label.setText("분석 중...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate

        QApplication.processEvents()

        try:
            self.resolver = ForeignKeyResolver(self.connector)

            def progress_cb(msg):
                self.progress_label.setText(msg)
                QApplication.processEvents()

            self.orphan_results = self.resolver.find_orphan_records(
                schema,
                progress_callback=progress_cb
            )

            # 결과 표시
            self.display_results()

        except Exception as e:
            QMessageBox.critical(self, "오류", f"분석 중 오류 발생:\n{str(e)}")
        finally:
            self.analyze_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            self.progress_label.setText("")

    def display_results(self):
        """분석 결과 표시"""
        if not self.orphan_results:
            self.result_list.addItem("✅ 고아 레코드가 발견되지 않았습니다.")
            self.detail_text.setText("모든 FK 관계가 정상입니다.")
            return

        total_orphans = sum(o.orphan_count for o in self.orphan_results)
        self.progress_label.setText(f"⚠️ {len(self.orphan_results)}개 관계에서 총 {total_orphans:,}개 고아 레코드 발견")

        for o in self.orphan_results:
            item_text = f"⚠️ {o.table}.{o.column} → {o.referenced_table} ({o.orphan_count:,}건)"
            self.result_list.addItem(item_text)

        self.export_all_queries_btn.setEnabled(True)
        self.export_report_btn.setEnabled(True)

        # 첫 번째 항목 선택
        if self.result_list.count() > 0:
            self.result_list.setCurrentRow(0)

    def on_result_selected(self, row: int):
        """결과 목록 선택 시"""
        if row < 0 or row >= len(self.orphan_results):
            self.detail_text.clear()
            self.copy_query_btn.setEnabled(False)
            return

        o = self.orphan_results[row]

        detail = f"""═══════════════════════════════════════════════════════════════════
 고아 레코드 상세 정보
═══════════════════════════════════════════════════════════════════

📊 FK 관계:
   자식 테이블: {o.table}
   FK 컬럼: {o.column}
   부모 테이블: {o.referenced_table}
   참조 컬럼: {o.referenced_column}

⚠️ 고아 레코드 수: {o.orphan_count:,}건

📝 샘플 값 (최대 5개):
   {', '.join(o.sample_values) if o.sample_values else '(없음)'}

═══════════════════════════════════════════════════════════════════
 조회 쿼리 (아래 쿼리로 고아 레코드를 직접 조회할 수 있습니다)
═══════════════════════════════════════════════════════════════════

{o.query}
"""
        self.detail_text.setText(detail)
        self.copy_query_btn.setEnabled(True)

    def copy_current_query(self):
        """현재 선택된 쿼리 복사"""
        row = self.result_list.currentRow()
        if row < 0 or row >= len(self.orphan_results):
            return

        o = self.orphan_results[row]
        clipboard = QApplication.clipboard()
        clipboard.setText(o.query)

        self.progress_label.setText("✅ 쿼리가 클립보드에 복사되었습니다.")

    def export_all_queries(self):
        """전체 쿼리 내보내기"""
        if not self.resolver:
            return

        schema = self.schema_combo.currentText()
        if not schema:
            return

        # 파일 저장 다이얼로그
        default_name = f"orphan_queries_{schema}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "쿼리 저장",
            default_name,
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            all_queries = self.resolver.get_all_orphan_queries(schema)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(all_queries)

            QMessageBox.information(
                self, "저장 완료",
                f"✅ 쿼리가 저장되었습니다.\n\n{file_path}"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "저장 실패",
                f"❌ 쿼리 저장 중 오류가 발생했습니다.\n\n{str(e)}"
            )

    def export_report(self):
        """보고서 저장"""
        if not self.resolver:
            return

        schema = self.schema_combo.currentText()
        if not schema:
            return

        # 파일 저장 다이얼로그
        default_name = f"orphan_report_{schema}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "보고서 저장",
            default_name,
            "Markdown 파일 (*.md);;텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        def progress_cb(msg):
            self.progress_label.setText(msg)
            QApplication.processEvents()

        success, msg, count = self.resolver.export_orphan_report(
            schema,
            file_path,
            progress_callback=progress_cb
        )

        if success:
            QMessageBox.information(
                self, "저장 완료",
                f"✅ 보고서가 저장되었습니다.\n\n{file_path}\n\n발견된 고아 관계: {count}건"
            )
        else:
            QMessageBox.critical(self, "저장 실패", f"❌ {msg}")

    def closeEvent(self, event):
        """다이얼로그 닫기"""
        # connector는 외부에서 관리하므로 여기서 닫지 않음
        event.accept()
