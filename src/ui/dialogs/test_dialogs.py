"""
테스트 및 SQL 실행 관련 다이얼로그
"""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QTextEdit, QFileDialog, QComboBox,
                             QGroupBox, QProgressBar, QMessageBox)
from PyQt6.QtCore import Qt

from src.core.db_core_service import create_rust_db_connector, normalize_db_engine


class SQLExecutionDialog(QDialog):
    """SQL 파일 실행 다이얼로그"""

    def __init__(self, parent, tunnel_config: dict, config_manager, tunnel_engine):
        super().__init__(parent)
        self.config = tunnel_config
        self.config_mgr = config_manager
        self.engine = tunnel_engine
        self.worker = None
        self.temp_server = None
        self.sql_file = None

        self.setWindowTitle(f"SQL 파일 실행 - {self.config.get('name', 'Unknown')}")
        self.setMinimumSize(600, 500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- 연결 정보 ---
        conn_group = QGroupBox("연결 정보")
        conn_layout = QVBoxLayout(conn_group)

        tid = self.config.get('id')
        db_user, _ = self.config_mgr.get_tunnel_credentials(tid)
        is_direct = self.config.get('connection_mode') == 'direct'

        if is_direct:
            host_info = f"{self.config['remote_host']}:{self.config['remote_port']}"
            mode_info = "직접 연결"
        else:
            host_info = f"localhost:{self.config.get('local_port', '?')}"
            mode_info = "SSH 터널"

        conn_layout.addWidget(QLabel(f"모드: {mode_info}"))
        conn_layout.addWidget(QLabel(f"호스트: {host_info}"))
        conn_layout.addWidget(QLabel(f"사용자: {db_user if db_user else '(미설정)'}"))

        if not db_user:
            warning = QLabel("⚠️ DB 자격 증명이 설정되지 않았습니다. 터널 설정에서 저장해주세요.")
            warning.setStyleSheet("color: #e74c3c; font-weight: bold;")
            conn_layout.addWidget(warning)

        layout.addWidget(conn_group)

        # --- SQL 파일 선택 ---
        file_group = QGroupBox("SQL 파일")
        file_layout = QHBoxLayout(file_group)

        self.file_label = QLabel("선택된 파일 없음")
        self.file_label.setStyleSheet("color: #7f8c8d;")

        btn_browse = QPushButton("📂 파일 선택...")
        btn_browse.clicked.connect(self.browse_file)

        file_layout.addWidget(self.file_label, 1)
        file_layout.addWidget(btn_browse)
        layout.addWidget(file_group)

        # --- 데이터베이스 선택 ---
        db_group = QGroupBox("데이터베이스 (선택사항)")
        db_layout = QHBoxLayout(db_group)

        self.db_combo = QComboBox()
        self.db_combo.setEditable(True)
        self.db_combo.setPlaceholderText("데이터베이스를 선택하거나 입력하세요 (생략 가능)")
        self.db_combo.setMinimumWidth(300)

        btn_refresh_db = QPushButton("🔄")
        btn_refresh_db.setToolTip("데이터베이스 목록 새로고침")
        btn_refresh_db.clicked.connect(self.refresh_databases)

        db_layout.addWidget(self.db_combo, 1)
        db_layout.addWidget(btn_refresh_db)
        layout.addWidget(db_group)

        # --- 진행 표시 ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminate
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # --- 출력 로그 ---
        output_group = QGroupBox("실행 결과")
        output_layout = QVBoxLayout(output_group)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setStyleSheet("""
            QTextEdit {
                background-color: #2c3e50;
                color: #ecf0f1;
                font-family: Consolas, 'Courier New', monospace;
                font-size: 12px;
                border: 1px solid #34495e;
            }
        """)
        output_layout.addWidget(self.output_text)
        layout.addWidget(output_group, 1)

        # --- 버튼 ---
        btn_layout = QHBoxLayout()

        self.btn_execute = QPushButton("▶️ 실행")
        self.btn_execute.setEnabled(False)
        self.btn_execute.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white; font-weight: bold;
                padding: 8px 20px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #229954; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_execute.clicked.connect(self.execute_sql)

        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.close)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_execute)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

        # 자격 증명이 있으면 실행 버튼 활성화 준비
        if db_user:
            self.refresh_databases()

    def browse_file(self):
        """SQL 파일 선택"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "SQL 파일 선택", "",
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if file_path:
            self.sql_file = file_path
            self.file_label.setText(file_path)
            self.file_label.setStyleSheet("color: #2c3e50;")
            self._update_execute_button()

    def _update_execute_button(self):
        """실행 버튼 활성화 상태 업데이트"""
        tid = self.config.get('id')
        db_user, _ = self.config_mgr.get_tunnel_credentials(tid)
        self.btn_execute.setEnabled(bool(self.sql_file and db_user))

    def refresh_databases(self):
        """데이터베이스 목록 새로고침"""
        tid = self.config.get('id')
        db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)

        if not db_user:
            return

        is_direct = self.config.get('connection_mode') == 'direct'
        temp_server = None

        try:
            self.output_text.append("📋 데이터베이스 목록 조회 중...")

            # 연결 정보 결정
            if is_direct:
                host = self.config['remote_host']
                port = int(self.config['remote_port'])
            elif self.engine.is_running(tid):
                host, port = self.engine.get_connection_info(tid)
            else:
                # 임시 터널 생성
                success, temp_server, error = self.engine.create_temp_tunnel(self.config)
                if not success:
                    self.output_text.append(f"❌ 터널 생성 실패: {error}")
                    return
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(temp_server)

            db_engine = normalize_db_engine(self.config.get('db_engine'), self.config.get('remote_port'))
            connector = create_rust_db_connector(
                db_engine,
                host,
                port,
                db_user,
                db_password,
                self.config.get('default_database') or (
                    self.config.get('default_schema') if db_engine == 'mysql' else None
                ),
            )
            success, msg = connector.connect()

            if success:
                schemas = connector.get_schemas()
                connector.disconnect()

                self.db_combo.clear()
                self.db_combo.addItem("")  # 빈 항목 (선택 안함)
                self.db_combo.addItems(schemas)
                self.output_text.append(f"✅ {len(schemas)}개 데이터베이스 발견")
            else:
                self.output_text.append(f"❌ DB 연결 실패: {msg}")

        except Exception as e:
            self.output_text.append(f"❌ 오류: {str(e)}")
        finally:
            if temp_server:
                self.engine.close_temp_tunnel(temp_server)

    def execute_sql(self):
        """SQL 파일 실행"""
        from src.ui.workers.test_worker import SQLExecutionWorker

        if not self.sql_file:
            QMessageBox.warning(self, "경고", "SQL 파일을 선택해주세요.")
            return

        tid = self.config.get('id')
        db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)

        if not db_user:
            QMessageBox.warning(self, "경고", "DB 자격 증명이 설정되지 않았습니다.")
            return

        is_direct = self.config.get('connection_mode') == 'direct'

        try:
            # 연결 정보 결정
            if is_direct:
                host = self.config['remote_host']
                port = int(self.config['remote_port'])
            elif self.engine.is_running(tid):
                host, port = self.engine.get_connection_info(tid)
            else:
                # 임시 터널 생성
                self.output_text.append("🔗 임시 터널 생성 중...")
                success, self.temp_server, error = self.engine.create_temp_tunnel(self.config)
                if not success:
                    self.output_text.append(f"❌ 터널 생성 실패: {error}")
                    return
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(self.temp_server)
                self.output_text.append(f"✅ 임시 터널 생성됨: localhost:{port}")

            # 데이터베이스 선택
            database = self.db_combo.currentText().strip() or None

            # UI 비활성화
            self.btn_execute.setEnabled(False)
            self.progress_bar.show()
            self.output_text.append(f"\n{'='*50}")
            self.output_text.append(f"🚀 SQL 실행 시작: {self.sql_file}")
            if database:
                self.output_text.append(f"📂 데이터베이스: {database}")
            self.output_text.append(f"{'='*50}\n")

            # Worker 실행
            self.worker = SQLExecutionWorker(
                self.sql_file, host, port, db_user, db_password, database
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.output.connect(self._on_output)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()

        except Exception as e:
            self.output_text.append(f"❌ 오류: {str(e)}")
            self._cleanup()

    def _on_progress(self, msg: str):
        """진행 메시지"""
        self.output_text.append(msg)

    def _on_output(self, text: str):
        """SQL 실행 출력"""
        self.output_text.append(text)

    def _on_finished(self, success: bool, msg: str):
        """실행 완료"""
        self.output_text.append(f"\n{msg}")
        self._cleanup()

    def _cleanup(self):
        """정리"""
        self.progress_bar.hide()
        self._update_execute_button()

        # 임시 터널 정리
        if self.temp_server:
            self.output_text.append("🛑 임시 터널 종료...")
            self.engine.close_temp_tunnel(self.temp_server)
            self.temp_server = None

    def closeEvent(self, event):
        """다이얼로그 닫기"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "확인",
                "SQL이 실행 중입니다. 정말 닫으시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        self._cleanup()
        event.accept()


class TestProgressDialog(QDialog):
    """연결 테스트 진행 다이얼로그"""

    def __init__(self, parent, title: str = "연결 테스트"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(480, 320)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)

        # 상태 메시지
        self.status_label = QLabel("테스트 준비 중...")
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self.status_label)

        # 진행 표시
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Indeterminate
        layout.addWidget(self.progress)

        # 상세 로그
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(180)
        layout.addWidget(self.log_text, 1)

        # 결과 버튼 (초기 숨김)
        self.btn_close = QPushButton("닫기")
        self.btn_close.hide()
        self.btn_close.clicked.connect(self.accept)
        layout.addWidget(self.btn_close)

    def update_progress(self, msg: str):
        """진행 상태 업데이트"""
        self.status_label.setText(msg)
        self.log_text.append(msg)

    def show_result(self, success: bool, msg: str):
        """결과 표시"""
        self.progress.hide()
        self.status_label.setText("✅ 테스트 완료!" if success else "❌ 테스트 실패")
        self.status_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {'#27ae60' if success else '#e74c3c'};"
        )
        self.log_text.append(f"\n{'='*40}")
        self.log_text.append(msg)
        self.btn_close.show()
