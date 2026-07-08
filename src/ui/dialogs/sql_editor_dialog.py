"""
SQL 에디터 다이얼로그
- SQL 쿼리 작성 및 실행
- 구문 하이라이팅
- 실시간 테이블/컬럼 검증 (인라인 표시)
- 자동완성 (Ctrl+Space)
- 결과 테이블 표시
- 멀티 탭 에디터 지원
"""
import os
import time
import logging
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QGroupBox, QSplitter, QPlainTextEdit, QTextEdit, QWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
    QStatusBar, QApplication, QAbstractItemView, QListWidget, QListWidgetItem, QProgressBar,
    QDialogButtonBox, QMenu, QCheckBox, QFrame, QToolTip, QLineEdit,
    QTreeWidget, QTreeWidgetItem
)
from PyQt6.QtCore import Qt, QRect, QSize, pyqtSignal, QThread, QTimer, QPoint
from PyQt6.QtGui import (
    QTextCharFormat, QColor, QPainter,
    QTextCursor, QKeySequence, QShortcut, QPen, QTextFormat
)
import re
from typing import List, Dict, Optional, Tuple

from src.core.db_core_service import normalize_db_engine
from src.core.sql_query_classifier import (
    classify_sql_statement,
    is_mysql_implicit_commit_ddl,
)
from src.core.sql_statement_parser import (
    find_sql_statement_at_position,
    parse_sql_statements,
)
from src.ui.dialogs.sql_editor_highlighters import SQLHighlighter, SQLValidatorHighlighter
from src.ui.dialogs.sql_editor_autocomplete import AutoCompletePopup
from src.ui.dialogs.sql_editor_code_editor import (
    LineNumberArea,
    CodeEditor,
    ValidatingCodeEditor,
    SQLEditorTab,
    LARGE_SQL_RENDER_LIMIT_BYTES,
)
from src.ui.dialogs.sql_editor_history_dialog import HistoryDialog
from src.ui.dialogs.sql_editor_workers import (
    SQLQueryWorker,
    SQLTransactionExecutionWorker,
    create_sql_editor_connector,
)

logger = logging.getLogger(__name__)


def format_metadata_db_version(db_version) -> str:
    if isinstance(db_version, (tuple, list)):
        major = db_version[0] if len(db_version) > 0 else 0
        minor = db_version[1] if len(db_version) > 1 else 0
        return f"{major}.{minor}"

    text = str(db_version or "").strip()
    if not text:
        return "unknown"
    match = re.search(r"(\d+)(?:\.(\d+))?", text)
    if match:
        return f"{match.group(1)}.{match.group(2) or 0}"
    return text


# =====================================================================
# SQL 구문 하이라이터
# =====================================================================


# =====================================================================
# SQL 검증 하이라이터 (밑줄 표시)
# =====================================================================


# =====================================================================
# 자동완성 팝업
# =====================================================================


# =====================================================================
# 줄 번호 위젯
# =====================================================================


# =====================================================================
# 코드 에디터 (줄 번호 + 하이라이팅)
# =====================================================================


# =====================================================================
# 검증 기능이 있는 코드 에디터
# =====================================================================


# =====================================================================
# SQL 쿼리 실행 워커 (자동 커밋 모드)
# =====================================================================


# =====================================================================
# SQL 트랜잭션 실행 워커 (지속 연결 모드 - 커밋/롤백은 다이얼로그가 소유)
# =====================================================================


# =====================================================================
# 히스토리 다이얼로그
# =====================================================================


# =====================================================================
# SQL 에디터 탭 (개별 탭 위젯)
# =====================================================================


# =====================================================================
# SQL 에디터 다이얼로그
# =====================================================================
class SQLEditorDialog(QDialog):
    """SQL 에디터 다이얼로그"""

    def __init__(self, parent, tunnel_config: dict, config_manager, tunnel_engine):
        super().__init__(parent)
        self.config = tunnel_config
        self.config_mgr = config_manager
        self.engine = tunnel_engine
        self.worker = None
        self._tab_counter = 0  # 탭 번호 카운터
        self._result_counter = 0  # 결과 탭 번호 카운터
        self._message_collapsed = True

        # 지속 연결 (트랜잭션 세션)
        self.db_connection = None
        self._db_connector = None
        self.pending_queries = []  # 미커밋 쿼리 목록: [(query, type, affected, timestamp, history_id), ...]

        # 임시 터널 소유권 분리 — 지속 트랜잭션 연결 vs 자동 커밋 1회성 실행
        self._persistent_temp_server = None
        self._autocommit_temp_server = None
        self._connected_target = None  # (database, schema) — db_connection이 실제로 물려있는 대상
        self._query_executing = False
        self._schema_change_guard = False
        self._pg_rolled_back_due_to_error = False
        self._retired_workers = []  # 취소되었지만 finished 시그널까지 유지해야 하는 워커들

        # 히스토리 매니저
        from src.core.sql_history import SQLHistory
        self.history_manager = SQLHistory()

        # SQL 검증 관련
        from src.core.sql_validator import SQLValidator, SQLAutoCompleter, SchemaMetadataProvider
        from src.ui.workers.validation_worker import ValidationWorker, MetadataLoadWorker, AutoCompleteWorker

        self.metadata_provider = SchemaMetadataProvider()
        self.sql_validator = SQLValidator(self.metadata_provider)
        self.sql_completer = SQLAutoCompleter(self.metadata_provider)
        self.validation_worker = None
        self.metadata_worker = None
        self.autocomplete_worker = None
        self._metadata_connector = None  # 메타데이터 로드용 연결

        self.setWindowTitle(f"SQL 에디터 - {self.config.get('name', 'Unknown')}")
        self.setMinimumSize(1000, 700)
        self.init_ui()
        self.setup_shortcuts()
        self.refresh_databases()

    def _db_engine(self) -> str:
        """Return the configured DB engine for Rust Core calls."""
        return normalize_db_engine(self.config.get('db_engine'), self.config.get('remote_port'))

    def _db_credentials(self) -> Tuple[str, str]:
        tid = self.config.get('id')
        return self.config_mgr.get_tunnel_credentials(tid)

    def _resolve_db_target(
        self,
        allow_temp_tunnel: bool,
        keep_temp_tunnel: bool = False,
        log_temp_tunnel: bool = False,
    ) -> Tuple[Optional[str], Optional[int], object, Optional[str]]:
        tid = self.config.get('id')
        if self.config.get('connection_mode') == 'direct':
            return self.config['remote_host'], int(self.config['remote_port']), None, None
        if self.engine.is_running(tid):
            host, port = self.engine.get_connection_info(tid)
            return host, int(port), None, None
        if not allow_temp_tunnel:
            return None, None, None, None

        if log_temp_tunnel:
            self.message_text.append("🔗 임시 터널 생성 중...")
            QApplication.processEvents()
        success, temp_server, error = self.engine.create_temp_tunnel(self.config)
        if not success:
            return None, None, None, f"터널 생성 실패: {error}"
        host = '127.0.0.1'
        port = int(self.engine.get_temp_tunnel_port(temp_server))
        if log_temp_tunnel:
            self.message_text.append(f"✅ 임시 터널: localhost:{port}")
        return host, port, temp_server, None

    def _create_db_connector(self, host, port, user, password, database=None, schema=None):
        return create_sql_editor_connector(
            self._db_engine(),
            host,
            port,
            user,
            password,
            database,
            schema,
        )

    def _database_and_schema_for_selection(self, selected: Optional[str] = None) -> Tuple[Optional[str], str]:
        db_engine = self._db_engine()
        selected_name = (selected or "").strip()
        if db_engine == "postgresql":
            return (
                self.config.get("default_database") or "postgres",
                selected_name or self.config.get("default_schema") or "public",
            )
        return (
            selected_name or self.config.get("default_database") or self.config.get("default_schema"),
            "",
        )

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- 연결 정보 바 ---
        conn_bar = QHBoxLayout()

        tid = self.config.get('id')
        db_user, _ = self.config_mgr.get_tunnel_credentials(tid)
        is_direct = self.config.get('connection_mode') == 'direct'

        if is_direct:
            host_info = f"{self.config['remote_host']}:{self.config['remote_port']}"
            mode_label = "직접 연결"
        else:
            host_info = f"localhost:{self.config.get('local_port', '?')}"
            mode_label = "SSH 터널"

        conn_bar.addWidget(QLabel(f"🔗 {mode_label}: {host_info}"))
        conn_bar.addWidget(QLabel(f"👤 {db_user or '(미설정)'}"))
        selector_text = "📂 Schema:" if self._db_engine() == "postgresql" else "📂 DB:"
        self.db_selector_label = QLabel(selector_text)
        conn_bar.addWidget(self.db_selector_label)

        self.db_combo = QComboBox()
        self.db_combo.setMinimumWidth(200)
        self.db_combo.currentTextChanged.connect(self._on_schema_changed)
        conn_bar.addWidget(self.db_combo)

        btn_refresh_db = QPushButton("🔄")
        btn_refresh_db.setToolTip("데이터베이스 목록 새로고침")
        btn_refresh_db.setMaximumWidth(40)
        btn_refresh_db.clicked.connect(self.refresh_databases)
        conn_bar.addWidget(btn_refresh_db)

        conn_bar.addStretch()
        layout.addLayout(conn_bar)

        # --- 툴바 ---
        toolbar = QHBoxLayout()

        # 현재 쿼리 실행 (커서 위치)
        self.btn_execute_current = QPushButton("▷ 실행")
        self.btn_execute_current.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #229954; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_execute_current.setToolTip("현재 쿼리 실행 (Ctrl+Enter)\n커서 위치의 쿼리만 실행")
        self.btn_execute_current.clicked.connect(self.execute_current_query)
        toolbar.addWidget(self.btn_execute_current)

        # 전체 실행
        self.btn_execute_all = QPushButton("▶ 전체")
        self.btn_execute_all.setStyleSheet("""
            QPushButton {
                background-color: #17a2b8; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #138496; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_execute_all.setToolTip("전체 쿼리 실행 (F5)\n에디터의 모든 쿼리 실행")
        self.btn_execute_all.clicked.connect(self.execute_all_queries)
        toolbar.addWidget(self.btn_execute_all)

        btn_open = QPushButton("📂 열기")
        btn_open.setToolTip("SQL 파일 열기 (Ctrl+O)")
        btn_open.clicked.connect(self.open_file)
        toolbar.addWidget(btn_open)

        btn_save = QPushButton("💾 저장")
        btn_save.setToolTip("SQL 파일 저장 (Ctrl+S)")
        btn_save.clicked.connect(self.save_file)
        toolbar.addWidget(btn_save)

        btn_history = QPushButton("📜 히스토리")
        btn_history.setToolTip("쿼리 히스토리 보기")
        btn_history.clicked.connect(self.show_history)
        toolbar.addWidget(btn_history)

        toolbar.addStretch()

        # 자동 커밋 체크박스
        self.auto_commit_check = QCheckBox("자동 커밋")
        self.auto_commit_check.setToolTip(
            "체크 해제 시: INSERT/UPDATE/DELETE 등 수정 쿼리 실행 전 확인 필요\n"
            "체크 시: 모든 쿼리 즉시 실행 (기존 방식)"
        )
        self.auto_commit_check.setChecked(False)  # 기본값: 확인 필요
        toolbar.addWidget(self.auto_commit_check)

        # 구분선
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        toolbar.addWidget(separator)

        # LIMIT 설정
        toolbar.addWidget(QLabel("LIMIT:"))
        self.limit_combo = QComboBox()
        self.limit_combo.setEditable(True)
        self.limit_combo.addItems(["100", "500", "1000", "5000", "10000", "제한 없음"])
        self.limit_combo.setCurrentText("1000")
        self.limit_combo.setToolTip("SELECT 쿼리에 자동으로 적용되는 행 제한\n(LIMIT 절이 없는 경우에만 적용)")
        self.limit_combo.setMinimumWidth(100)
        toolbar.addWidget(self.limit_combo)

        layout.addLayout(toolbar)

        # --- 메인 스플리터 (스키마 트리 + 에디터/결과) ---
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        schema_group = QGroupBox("스키마 / 테이블")
        schema_layout = QVBoxLayout(schema_group)
        schema_layout.setContentsMargins(4, 8, 4, 4)

        self.schema_tree = QTreeWidget()
        self.schema_tree.setHeaderLabels(["이름"])
        self.schema_tree.setMinimumWidth(180)
        self.schema_tree.setStyleSheet("""
            QTreeWidget {
                border: 1px solid #d8e0e8;
                border-radius: 4px;
                background-color: #fafafa;
            }
            QTreeWidget::item {
                padding: 3px 4px;
            }
        """)
        self.schema_tree.itemClicked.connect(self._on_schema_tree_item_clicked)
        schema_layout.addWidget(self.schema_tree)
        main_splitter.addWidget(schema_group)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # 에디터 영역 (멀티 탭)
        editor_group = QGroupBox("SQL 쿼리")
        editor_layout = QVBoxLayout(editor_group)
        editor_layout.setContentsMargins(4, 8, 4, 4)

        # 에디터 탭 위젯
        self.editor_tabs = QTabWidget()
        self.editor_tabs.setTabsClosable(True)
        self.editor_tabs.setMovable(True)
        self.editor_tabs.setDocumentMode(True)
        self.editor_tabs.tabCloseRequested.connect(self._close_editor_tab)
        self.editor_tabs.currentChanged.connect(self._on_editor_tab_changed)

        # 새 탭 버튼 (+)
        self.new_tab_button = QPushButton("+")
        self.new_tab_button.setFixedSize(24, 24)
        self.new_tab_button.setToolTip("새 탭 (Ctrl+N)")
        self.new_tab_button.setStyleSheet("""
            QPushButton {
                border: none;
                background: transparent;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background: #e0e0e0;
                border-radius: 4px;
            }
        """)
        self.new_tab_button.clicked.connect(self._add_new_tab)
        self.editor_tabs.setCornerWidget(self.new_tab_button, Qt.Corner.TopRightCorner)

        # 첫 번째 탭 추가
        self._add_new_tab()

        editor_layout.addWidget(self.editor_tabs)

        splitter.addWidget(editor_group)

        # 결과 영역
        result_group = QGroupBox("결과")
        result_layout = QVBoxLayout(result_group)
        result_layout.setContentsMargins(4, 8, 4, 4)

        # 메시지 영역 (접힘 상태에서는 한 줄 요약만 노출)
        self.btn_toggle_message = QPushButton("실행 로그 펼치기")
        self.btn_toggle_message.setToolTip("실행 로그 상세 보기")
        self.btn_toggle_message.setStyleSheet("""
            QPushButton {
                text-align: left;
                background-color: #34495e;
                color: #ecf0f1;
                border: none;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3f5870;
            }
        """)
        self.btn_toggle_message.clicked.connect(self._toggle_message_panel)
        result_layout.addWidget(self.btn_toggle_message)

        self.message_summary = QLabel("실행 대기 중")
        self.message_summary.setStyleSheet("""
            QLabel {
                background-color: #f4f7fa;
                color: #2c3e50;
                border: 1px solid #d8e0e8;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 12px;
            }
        """)
        result_layout.addWidget(self.message_summary)

        self.message_text = QTextEdit()
        self.message_text.setReadOnly(True)
        self.message_text.setStyleSheet("""
            QTextEdit {
                background-color: #2c3e50;
                color: #ecf0f1;
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
        """)
        result_layout.addWidget(self.message_text)
        self._set_message_panel_collapsed(True)

        self.result_tabs = QTabWidget()
        self.result_tabs.setTabsClosable(True)
        self.result_tabs.setMovable(True)
        self.result_tabs.tabCloseRequested.connect(self.close_result_tab)

        result_tab_bar = self.result_tabs.tabBar()
        result_tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        result_tab_bar.customContextMenuRequested.connect(self._show_result_tab_context_menu)

        result_layout.addWidget(self.result_tabs)

        # 트랜잭션 상태 패널
        self.tx_panel = QWidget()
        tx_panel_layout = QVBoxLayout(self.tx_panel)
        tx_panel_layout.setContentsMargins(0, 0, 0, 0)
        tx_panel_layout.setSpacing(4)

        # 상태 바 (헤더)
        self.tx_status_frame = QFrame()
        self.tx_status_frame.setStyleSheet("""
            QFrame {
                background-color: #E8F4FD;
                border: 1px solid #B8DAFF;
                border-radius: 4px;
            }
        """)
        tx_header_layout = QHBoxLayout(self.tx_status_frame)
        tx_header_layout.setContentsMargins(12, 6, 12, 6)

        self.tx_status_icon = QLabel("💾")
        self.tx_status_icon.setStyleSheet("font-size: 14px; background: transparent; border: none;")
        tx_header_layout.addWidget(self.tx_status_icon)

        self.tx_info_label = QLabel("트랜잭션: 대기 중")
        self.tx_info_label.setStyleSheet("color: #004085; background: transparent; border: none;")
        tx_header_layout.addWidget(self.tx_info_label)

        # 펼치기/접기 버튼
        self.btn_toggle_pending = QPushButton("▼ 상세")
        self.btn_toggle_pending.setStyleSheet("""
            QPushButton {
                background: transparent; border: none; color: #666;
                padding: 4px 8px; font-size: 11px;
            }
            QPushButton:hover { color: #333; }
        """)
        self.btn_toggle_pending.clicked.connect(self._toggle_pending_list)
        self.btn_toggle_pending.setVisible(False)
        tx_header_layout.addWidget(self.btn_toggle_pending)

        tx_header_layout.addStretch()

        self.btn_commit = QPushButton("✅ 커밋")
        self.btn_commit.setStyleSheet("""
            QPushButton {
                background-color: #28A745; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #94D3A2; }
        """)
        self.btn_commit.clicked.connect(self._do_commit)
        self.btn_commit.setEnabled(False)
        tx_header_layout.addWidget(self.btn_commit)

        self.btn_rollback = QPushButton("↩️ 롤백")
        self.btn_rollback.setStyleSheet("""
            QPushButton {
                background-color: #6C757D; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #5A6268; }
            QPushButton:disabled { background-color: #ADB5BD; }
        """)
        self.btn_rollback.clicked.connect(self._do_rollback)
        self.btn_rollback.setEnabled(False)
        tx_header_layout.addWidget(self.btn_rollback)

        tx_panel_layout.addWidget(self.tx_status_frame)

        # 미커밋 쿼리 목록 (접혀있는 상태로 시작)
        self.pending_list_widget = QListWidget()
        self.pending_list_widget.setStyleSheet("""
            QListWidget {
                background-color: #FFFBEA;
                border: 1px solid #FFC107;
                border-radius: 4px;
                font-family: 'Consolas', monospace;
                font-size: 11px;
            }
            QListWidget::item {
                padding: 4px 8px;
                border-bottom: 1px solid #FFE082;
            }
            QListWidget::item:selected {
                background-color: #FFE082;
                color: #333;
            }
        """)
        self.pending_list_widget.setMaximumHeight(120)
        self.pending_list_widget.setVisible(False)
        tx_panel_layout.addWidget(self.pending_list_widget)

        result_layout.addWidget(self.tx_panel)

        splitter.addWidget(result_group)

        splitter.setSizes([350, 300])
        main_splitter.addWidget(splitter)
        main_splitter.setSizes([220, 780])
        layout.addWidget(main_splitter)

        # --- 프로그레스 바 ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(3)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: none; background-color: #ecf0f1; }
            QProgressBar::chunk { background-color: #3498db; }
        """)
        layout.addWidget(self.progress_bar)

        # --- 상태바 ---
        self.status_bar = QStatusBar()
        self.status_bar.showMessage("준비됨")
        layout.addWidget(self.status_bar)

    def setup_shortcuts(self):
        """단축키 설정 (쿼리 실행 중에는 실행 단축키를 비활성화하기 위해 self에 보관)"""
        # F5: 전체 실행
        self.shortcut_f5 = QShortcut(QKeySequence("F5"), self)
        self.shortcut_f5.activated.connect(self.execute_all_queries)

        # Ctrl+Enter: 현재 쿼리 실행
        self.shortcut_ctrl_enter = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.shortcut_ctrl_enter.activated.connect(self.execute_current_query)

        # Ctrl+Shift+Enter: 전체 실행
        self.shortcut_ctrl_shift_enter = QShortcut(QKeySequence("Ctrl+Shift+Return"), self)
        self.shortcut_ctrl_shift_enter.activated.connect(self.execute_all_queries)

        # Ctrl+O: 열기
        self.shortcut_open = QShortcut(QKeySequence("Ctrl+O"), self)
        self.shortcut_open.activated.connect(self.open_file)

        # Ctrl+S: 저장
        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save_file)

        # Ctrl+Shift+S: 다른 이름으로 저장
        self.shortcut_save_as = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        self.shortcut_save_as.activated.connect(self.save_file_as)

        # Ctrl+N: 새 탭
        self.shortcut_new_tab = QShortcut(QKeySequence("Ctrl+N"), self)
        self.shortcut_new_tab.activated.connect(self._add_new_tab)

        # Ctrl+W: 현재 탭 닫기
        self.shortcut_close_tab = QShortcut(QKeySequence("Ctrl+W"), self)
        self.shortcut_close_tab.activated.connect(self._close_current_tab)

        # Ctrl+Tab: 다음 탭
        self.shortcut_next_tab = QShortcut(QKeySequence("Ctrl+Tab"), self)
        self.shortcut_next_tab.activated.connect(self._next_tab)

        # Ctrl+Shift+Tab: 이전 탭
        self.shortcut_prev_tab = QShortcut(QKeySequence("Ctrl+Shift+Tab"), self)
        self.shortcut_prev_tab.activated.connect(self._prev_tab)

    # =====================================================================
    # 에디터 탭 관리
    # =====================================================================
    @property
    def editor(self):
        """현재 탭의 에디터 반환 (하위 호환성)"""
        tab = self._current_tab()
        return tab.editor if tab else None

    @property
    def validation_label(self):
        """현재 탭의 검증 라벨 반환"""
        tab = self._current_tab()
        return tab.validation_label if tab else None

    @property
    def current_file(self):
        """현재 탭의 파일 경로"""
        tab = self._current_tab()
        return tab.file_path if tab else None

    @current_file.setter
    def current_file(self, value):
        """현재 탭의 파일 경로 설정"""
        tab = self._current_tab()
        if tab:
            tab.file_path = value

    @property
    def is_modified(self):
        """현재 탭의 수정 상태"""
        tab = self._current_tab()
        return tab.is_modified if tab else False

    @is_modified.setter
    def is_modified(self, value):
        """현재 탭의 수정 상태 설정"""
        tab = self._current_tab()
        if tab:
            tab.is_modified = value
            if not value:
                tab.title_changed.emit(tab.get_title())

    def _current_tab(self) -> Optional[SQLEditorTab]:
        """현재 에디터 탭 반환"""
        return self.editor_tabs.currentWidget()

    def _add_new_tab(self, file_path: str = None) -> SQLEditorTab:
        """새 에디터 탭 추가"""
        self._tab_counter += 1
        tab = SQLEditorTab(self, self._tab_counter)

        # 시그널 연결
        tab.title_changed.connect(lambda title, t=tab: self._update_tab_title(t, title))
        tab.editor.validation_requested.connect(self._on_validation_requested)
        tab.editor.autocomplete_requested.connect(self._on_autocomplete_requested)

        # 파일 로드
        if file_path:
            if tab.load_file(file_path):
                self.message_text.append(f"📂 파일 열림: {file_path}")
            else:
                self.message_text.append(f"❌ 파일을 열 수 없습니다: {file_path}")

        # 탭 추가
        tab_title = tab.get_title()
        index = self.editor_tabs.addTab(tab, tab_title)
        self.editor_tabs.setCurrentIndex(index)

        return tab

    def _close_editor_tab(self, index: int):
        """에디터 탭 닫기 요청"""
        if self.editor_tabs.count() <= 1:
            # 마지막 탭이면 새 빈 탭 추가 후 닫기
            self._add_new_tab()

        tab = self.editor_tabs.widget(index)
        if tab and tab.is_modified:
            reply = QMessageBox.question(
                self, "저장 확인",
                f"'{tab.get_title().rstrip(' *')}'의 변경사항을 저장하시겠습니까?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self._save_tab(tab):
                    return  # 저장 실패/취소
            elif reply == QMessageBox.StandardButton.Cancel:
                return

        self.editor_tabs.removeTab(index)

    def _close_current_tab(self):
        """현재 탭 닫기"""
        index = self.editor_tabs.currentIndex()
        if index >= 0:
            self._close_editor_tab(index)

    def _next_tab(self):
        """다음 탭으로 이동"""
        current = self.editor_tabs.currentIndex()
        count = self.editor_tabs.count()
        if count > 1:
            self.editor_tabs.setCurrentIndex((current + 1) % count)

    def _prev_tab(self):
        """이전 탭으로 이동"""
        current = self.editor_tabs.currentIndex()
        count = self.editor_tabs.count()
        if count > 1:
            self.editor_tabs.setCurrentIndex((current - 1) % count)

    def _update_tab_title(self, tab: SQLEditorTab, title: str):
        """탭 제목 업데이트"""
        index = self.editor_tabs.indexOf(tab)
        if index >= 0:
            self.editor_tabs.setTabText(index, title)

    def _on_editor_tab_changed(self, index: int):
        """에디터 탭 변경 시"""
        tab = self.editor_tabs.widget(index)
        if tab:
            # 현재 탭 파일 정보 윈도우 제목에 반영
            file_info = tab.file_path or ""
            if file_info:
                self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {file_info}")
            else:
                self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')}")

            # 현재 탭의 내용으로 재검증
            self._on_validation_requested(tab.editor.toPlainText())

    def _save_tab(self, tab: SQLEditorTab) -> bool:
        """특정 탭 저장"""
        if tab.file_path:
            success, path, error = tab.save_file()
            if success:
                self.message_text.append(f"💾 파일 저장됨: {path}")
                return True
            else:
                QMessageBox.critical(self, "오류", f"파일을 저장할 수 없습니다:\n{error}")
                return False
        else:
            # 새 파일명 요청
            file_path, _ = QFileDialog.getSaveFileName(
                self, "SQL 파일 저장", "",
                "SQL 파일 (*.sql);;모든 파일 (*.*)"
            )
            if file_path:
                success, path, error = tab.save_file(file_path)
                if success:
                    self.message_text.append(f"💾 파일 저장됨: {path}")
                    return True
                else:
                    QMessageBox.critical(self, "오류", f"파일을 저장할 수 없습니다:\n{error}")
                    return False
            return False

    def refresh_databases(self):
        """데이터베이스 목록 새로고침"""
        db_user, db_password = self._db_credentials()

        if not db_user:
            self.message_text.append("⚠️ DB 자격 증명이 설정되지 않았습니다.")
            return

        temp_server = None

        try:
            self.message_text.append("📋 데이터베이스 목록 조회 중...")
            QApplication.processEvents()

            host, port, temp_server, error = self._resolve_db_target(allow_temp_tunnel=True)
            if error:
                self.message_text.append(f"❌ {error}")
                return
            db_engine = self._db_engine()
            connect_database, connect_schema = self._database_and_schema_for_selection(
                self.config.get('default_schema') if db_engine == 'postgresql' else None
            )
            connector = self._create_db_connector(
                host,
                port,
                db_user,
                db_password,
                connect_database,
                connect_schema,
            )
            try:
                success, msg = connector.connect()

                if success:
                    schemas = connector.get_schemas()

                    self.db_combo.clear()
                    self.db_combo.addItems(schemas)
                    self.message_text.append(f"✅ {len(schemas)}개 데이터베이스 발견")

                    # 기본 스키마 자동 선택
                    default_schema = self.config.get('default_schema')
                    if default_schema:
                        index = self.db_combo.findText(default_schema)
                        if index >= 0:
                            self.db_combo.setCurrentIndex(index)
                            self.message_text.append(f"📌 기본 스키마 선택됨: {default_schema}")
                        else:
                            self.message_text.append(f"⚠️ 기본 스키마 '{default_schema}'를 찾을 수 없습니다.")
                    # 메타데이터 로드 (선택된 스키마 기준)
                    selected = self.db_combo.currentText()
                    if selected:
                        self._load_metadata(selected)
                else:
                    self.message_text.append(f"❌ DB 연결 실패: {msg}")
            finally:
                # 항상 연결 정리
                try:
                    connector.disconnect()
                except Exception:
                    pass

        except Exception as e:
            self.message_text.append(f"❌ 오류: {str(e)}")
        finally:
            if temp_server:
                self.engine.close_temp_tunnel(temp_server)

    def _ensure_connection(self):
        """지속 연결 확보 (없으면 생성).

        db_combo에서 선택된 DB/스키마가 현재 지속 연결의 대상과 다르면,
        미커밋 변경이 있는 동안은 재연결을 거부하고 없으면 재연결한다.
        """
        selected = self.db_combo.currentText().strip()
        target = self._database_and_schema_for_selection(selected)

        if self.db_connection and self.db_connection.open:
            if self._connected_target == target:
                return True, None
            if self.pending_queries or self._collect_all_pending_edits():
                return False, (
                    "선택된 DB/스키마가 현재 트랜잭션 연결과 다릅니다. "
                    "미커밋 변경을 커밋하거나 롤백한 뒤 다시 실행하세요."
                )
            self._close_db_connection()

        db_user, db_password = self._db_credentials()

        if not db_user:
            return False, "DB 자격 증명이 설정되지 않았습니다."

        temp_server = None
        try:
            host, port, temp_server, error = self._resolve_db_target(
                allow_temp_tunnel=True,
                keep_temp_tunnel=True,
                log_temp_tunnel=True,
            )
            if error:
                return False, error

            database, schema = target

            db_engine = self._db_engine()
            connector = self._create_db_connector(
                host,
                port,
                db_user,
                db_password,
                database,
                schema,
            )
            success, msg = connector.connect()
            if not success:
                return False, msg
            self.db_connection = connector.connection
            self._db_connector = connector
            self._persistent_temp_server = temp_server
            temp_server = None  # 소유권이 self._persistent_temp_server로 이전됨
            self.db_connection.autocommit(False)
            # READ COMMITTED: 각 SELECT가 최신 커밋 데이터를 조회 (외부 변경 즉시 반영)
            if db_engine == 'postgresql':
                self.db_connection.cursor().execute(
                    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
                )
            else:
                self.db_connection.cursor().execute(
                    "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
                )
            self._connected_target = target
            self.message_text.append(f"✅ DB 연결 성공 (트랜잭션 모드): {host}:{port}")
            self._update_tx_status()
            return True, None

        except Exception as e:
            return False, str(e)
        finally:
            # 연결 성공 시 temp_server는 이미 None으로 비워짐 — 실패 시에만 여기서 정리
            if temp_server:
                self.engine.close_temp_tunnel(temp_server)

    def execute_current_query(self):
        """커서 위치의 현재 쿼리만 실행 (Ctrl+Enter)"""
        # 선택된 텍스트가 있으면 그것을 실행
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            sql_text = cursor.selectedText().replace('\u2029', '\n')
        else:
            # 커서 위치에서 현재 쿼리 찾기
            sql_text = self._get_query_at_cursor()

        if not sql_text or not sql_text.strip():
            self.status_bar.showMessage("실행할 쿼리가 없습니다.")
            return

        self._execute_sql(sql_text, single_query=True)

    def execute_all_queries(self):
        """전체 쿼리 실행 (F5)"""
        sql_text = self.editor.toPlainText()
        if not sql_text.strip():
            QMessageBox.warning(self, "경고", "실행할 SQL이 없습니다.")
            return

        self._execute_sql(sql_text, single_query=False)

    def _get_query_at_cursor(self):
        """커서 위치의 쿼리 반환"""
        full_text = self.editor.toPlainText()
        cursor_pos = self.editor.textCursor().position()

        if not full_text.strip():
            return ""

        return find_sql_statement_at_position(full_text, cursor_pos)

    def _execute_sql(self, sql_text, single_query=False):
        """SQL 실행 (내부 메서드) — 트랜잭션 모드는 QThread 워커로 순차 실행한다."""
        if self._query_executing or (self.worker and self.worker.isRunning()):
            QMessageBox.warning(self, "경고", "쿼리가 이미 실행 중입니다.")
            return

        if not sql_text.strip():
            QMessageBox.warning(self, "경고", "실행할 SQL이 없습니다.")
            return

        # 쿼리 분리
        queries = self._split_queries(sql_text)
        if not queries:
            QMessageBox.warning(self, "경고", "유효한 SQL 쿼리가 없습니다.")
            return

        # Production 환경에서 위험 쿼리 확인
        from src.core.production_guard import ProductionGuard
        guard = ProductionGuard(self)

        is_dangerous, keyword = guard.is_dangerous_query(sql_text)
        if is_dangerous:
            schema_name = self.db_combo.currentText() or "(미선택)"
            preview = sql_text[:200] + "..." if len(sql_text) > 200 else sql_text
            # HTML 특수문자 이스케이프
            preview = preview.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            if not guard.confirm_dangerous_operation(
                self.config,
                f"{keyword} 쿼리 실행",
                schema_name,
                f"<pre style='background: #f5f5f5; padding: 8px; border-radius: 4px; white-space: pre-wrap;'>{preview}</pre>"
            ):
                return  # 사용자가 취소

        # LIMIT 자동 적용
        limit_value = self._get_limit_value()
        if limit_value:
            queries = [self._apply_limit(q, limit_value) for q in queries]

        # 자동 커밋 모드면 기존 워커 사용
        if self.auto_commit_check.isChecked():
            self._execute_with_autocommit(queries, sql_text)
            return

        # 지속 연결 확보
        success, error = self._ensure_connection()
        if not success:
            QMessageBox.warning(self, "경고", error)
            return

        # MySQL DDL은 암묵적 COMMIT을 일으켜 이전 미커밋 변경을 되돌릴 수 없게 만든다 — 사전 확인
        if (
            self._db_engine() == 'mysql'
            and self.pending_queries
            and any(is_mysql_implicit_commit_ddl(q) for q in queries)
        ):
            reply = QMessageBox.question(
                self, "DDL 실행 확인",
                "MySQL DDL은 암묵적 COMMIT을 발생시켜 이전 미커밋 변경을 롤백할 수 없게 됩니다. 계속 실행하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 단일 쿼리 실행 시 결과 탭 유지, 전체 실행 시 초기화 (미저장 셀 편집이 있으면 확인)
        if not single_query:
            if not self._clear_result_tabs():
                return

        self._set_executing_state(True)

        query_label = "현재 쿼리" if single_query else f"{len(queries)}개 쿼리"
        self.message_text.append(f"\n{'─'*40}")
        self.message_text.append(f"🚀 {query_label} 실행")
        self.message_text.append(f"{'─'*40}")

        if len(queries) > 1:
            self.progress_bar.setMaximum(len(queries))

        self.worker = SQLTransactionExecutionWorker(self.db_connection, queries, self._db_engine())
        self.worker.progress.connect(self._on_transaction_progress)
        self.worker.query_result.connect(self._on_transaction_query_result)
        self.worker.postgres_rolled_back.connect(self._on_postgres_transaction_rolled_back)
        self.worker.finished.connect(self._on_transaction_finished)
        self.worker.start()

    def _on_transaction_progress(self, idx, total, query_type, preview):
        """트랜잭션 워커가 쿼리 실행을 시작하기 전 진행 상황을 알림"""
        if total > 1:
            self.progress_bar.setValue(idx)
            self._exec_query_progress = f"{idx + 1}/{total}"
        self.message_text.append(f"📄 쿼리 {idx + 1}/{total} 실행 중... ({query_type})")

    def _on_transaction_query_result(self, idx, query, returns_rows, columns, rows, error, affected, exec_time):
        """트랜잭션 워커에서 쿼리 1건 실행 결과 수신"""
        preview = query[:60] + "..." if len(query) > 60 else query
        preview = preview.replace('\n', ' ')

        if error:
            self.message_text.append(f"❌ {error}")
            self.message_text.append(f"   └ {preview}")
            self.history_manager.add_query(query, False, 0, exec_time, status='error', error=error)
        elif returns_rows:
            # columns == [] 인 0행 결과도 결과 탭으로 표시 (SELECT 실행 자체는 성공)
            self._add_result_table(columns, rows, exec_time, query)
            self.message_text.append(f"✅ {len(rows)}행 반환 ({exec_time:.3f}초)")
            self.message_text.append(f"   └ {preview}")
            self.history_manager.add_query(query, True, len(rows), exec_time)
        else:
            query_type = (classify_sql_statement(query).leading_keyword or "other").upper()
            if self._db_engine() == 'mysql' and is_mysql_implicit_commit_ddl(query):
                # MySQL 암묵적 COMMIT DDL: 이전 미커밋 변경은 이미 서버에 커밋되어 되돌릴 수 없다
                if self.pending_queries:
                    history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
                    if history_ids:
                        self.history_manager.update_status_batch(history_ids, 'auto_committed_by_ddl')
                    self.pending_queries.clear()
                    self.message_text.append("⚠️ DDL로 인해 이전 미커밋 변경이 자동 커밋되었습니다. 롤백할 수 없습니다.")
                self.history_manager.add_query(query, True, affected, exec_time, status='committed')
                self.message_text.append(f"✅ [DDL] {affected}행 영향 ({exec_time:.3f}초) - 자동 커밋됨")
                self.message_text.append(f"   └ {preview}")
            else:
                history_id = self.history_manager.add_query(
                    query, True, affected, exec_time, status='pending'
                )
                from datetime import datetime
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.pending_queries.append({
                    'query': query,
                    'type': query_type,
                    'affected': affected,
                    'timestamp': timestamp,
                    'history_id': history_id,
                })
                self.message_text.append(f"📝 [{query_type}] {affected}행 영향 ({exec_time:.3f}초) - 미커밋")
                self.message_text.append(f"   └ {preview}")

        total = len(self.worker.queries) if self.worker is not None else 0
        if total > 1:
            self.progress_bar.setValue(idx + 1)
            self._exec_query_progress = f"{idx + 1}/{total}"

        self._update_tx_status()

    def _on_postgres_transaction_rolled_back(self, error):
        """PostgreSQL은 에러 발생 시 트랜잭션 전체가 aborted 상태가 되어 즉시 롤백된다"""
        history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
        if history_ids:
            self.history_manager.update_status_batch(history_ids, 'rolled_back_due_to_error')
        self.pending_queries.clear()
        self._pg_rolled_back_due_to_error = True
        self.message_text.append(
            "⚠️ PostgreSQL 오류로 트랜잭션이 즉시 롤백되었습니다. "
            "이전 미커밋 변경은 적용되지 않았습니다. 쿼리를 수정한 뒤 다시 실행하세요."
        )
        self._update_tx_status()

    def _on_transaction_finished(self, success, msg):
        """트랜잭션 워커 실행 종료 — 지속 연결은 유지, 커밋/롤백은 사용자가 결정"""
        total_elapsed = time.time() - self._exec_start_time if self._exec_start_time else 0
        self.message_text.append(f"\n{msg}")
        self._set_message_summary(f"{msg} · {total_elapsed:.1f}초")
        self._set_executing_state(False)
        pending_count = len(self.pending_queries)
        self.status_bar.showMessage(f"{msg} ({total_elapsed:.1f}초, 미커밋 변경: {pending_count}건)")
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None

    def _execute_with_autocommit(self, queries, sql_text):
        """자동 커밋 모드로 실행 (기존 워커 사용)"""
        db_user, db_password = self._db_credentials()

        if not db_user:
            QMessageBox.warning(self, "경고", "DB 자격 증명이 설정되지 않았습니다.")
            return

        try:
            host, port, temp_server, error = self._resolve_db_target(
                allow_temp_tunnel=True,
                keep_temp_tunnel=True,
                log_temp_tunnel=True,
            )
            if error:
                self.message_text.append(f"❌ {error}")
                return
            self._autocommit_temp_server = temp_server

            selected = self.db_combo.currentText().strip()
            database, schema = self._database_and_schema_for_selection(selected)

            if not self._clear_result_tabs():
                if self._autocommit_temp_server:
                    self.engine.close_temp_tunnel(self._autocommit_temp_server)
                    self._autocommit_temp_server = None
                return

            self._set_executing_state(True)
            self.progress_bar.setMaximum(len(queries))
            self.message_text.append(f"\n{'='*50}")
            self.message_text.append(f"🚀 {len(queries)}개 쿼리 실행 (자동 커밋)")
            self.message_text.append(f"{'='*50}\n")

            self.worker = SQLQueryWorker(
                host,
                port,
                db_user,
                db_password,
                database,
                queries,
                engine=self._db_engine(),
                schema=schema,
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.query_result.connect(self._on_query_result)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()

        except Exception as e:
            self.message_text.append(f"❌ 오류: {str(e)}")
            self._cleanup()

    def _add_result_table(self, columns, rows, exec_time, query=''):
        """결과 테이블 탭 추가"""
        table = QTableWidget()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value) if value is not None else "NULL")
                if value is None:
                    item.setForeground(QColor("#888888"))
                table.setItem(r, c, item)

        header = table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        table.resizeColumnsToContents()
        # 초기 렌더링 시 400px 초과 컬럼 제한 (이후 자유 조정 가능)
        for col in range(len(columns)):
            if header.sectionSize(col) > 400:
                header.resizeSection(col, 400)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        # 행 높이 확보 (편집 시 텍스트 잘림 방지)
        table.verticalHeader().setDefaultSectionSize(28)

        # 셀 편집기(QLineEdit) 스타일 — 셀 경계 내에 정확히 맞도록
        table.setStyleSheet("""
            QTableWidget {
                gridline-color: #ddd;
                font-size: 12px;
            }
            QTableWidget::item:selected {
                background-color: #3498db;
                color: white;
            }
            QTableWidget QLineEdit {
                padding: 1px 4px;
                margin: 0px;
                border: 2px solid #2196F3;
                background: white;
                color: #000;
                font-size: 12px;
            }
        """)

        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table, c=columns: self._show_table_context_menu(pos, t, c)
        )

        # Ctrl+C: 선택한 모든 셀을 탭 구분으로 복사
        copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
        copy_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        copy_shortcut.activated.connect(
            lambda t=table, c=columns: self._copy_table_data(t, c, False)
        )

        self._result_counter += 1
        tab_name = f"결과 {self._result_counter} ({len(rows)}행)"
        self.result_tabs.addTab(table, tab_name)
        self.result_tabs.setCurrentWidget(table)

        # 편집 가능성 분석 + 설정
        self._setup_result_table_editability(table, query, columns, rows)

    def _pending_edit_count_for_result_tab(self, index: int) -> int:
        """특정 결과 탭의 미저장 셀 편집 건수"""
        widget = self.result_tabs.widget(index)
        if not isinstance(widget, QTableWidget):
            return 0
        ctx = getattr(widget, '_edit_context', None)
        if not ctx:
            return 0
        return len(ctx['pending_edits'])

    def _confirm_discard_pending_edits(self, count: int, action_label: str) -> bool:
        """미저장 셀 편집이 있으면 확인 다이얼로그를 띄우고, 계속 여부를 반환"""
        if count <= 0:
            return True
        reply = QMessageBox.question(
            self, "확인",
            f"저장되지 않은 셀 편집 {count}건이 있습니다. {action_label} 손실됩니다. 계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _discard_all_pending_edits(self):
        """모든 결과 탭의 미저장 셀 편집을 원본값으로 되돌림"""
        for table, _ctx in self._collect_all_pending_edits():
            self._discard_pending_edits(table)

    def _clear_result_tabs(self) -> bool:
        """모든 결과 탭 삭제 (미저장 셀 편집이 있으면 확인). 실행 여부를 반환."""
        total_pending = sum(
            self._pending_edit_count_for_result_tab(i)
            for i in range(self.result_tabs.count())
        )
        if not self._confirm_discard_pending_edits(total_pending, "결과 탭을 삭제하면"):
            return False
        while self.result_tabs.count() > 0:
            self.result_tabs.removeTab(0)
        self._result_counter = 0
        return True

    def _set_message_panel_collapsed(self, collapsed: bool):
        """메시지 영역 접힘/펼침 상태 적용"""
        self._message_collapsed = collapsed
        if collapsed:
            self.btn_toggle_message.setText("실행 로그 펼치기")
            self.btn_toggle_message.setToolTip("실행 로그 상세 보기")
            self.message_summary.show()
            self.message_text.hide()
        else:
            self.btn_toggle_message.setText("실행 로그 접기")
            self.btn_toggle_message.setToolTip("실행 로그 요약 보기")
            self.message_summary.show()
            self.message_text.show()
            self.message_text.setMinimumHeight(120)
            self.message_text.setMaximumHeight(220)

    def _toggle_message_panel(self):
        """메시지 영역 펼치기/접기"""
        self._set_message_panel_collapsed(not self._message_collapsed)

    def _set_message_summary(self, text: str):
        """접힌 실행 로그에 표시할 한 줄 상태 요약."""
        self.message_summary.setText(text or "실행 대기 중")

    def _show_result_tab_context_menu(self, position):
        """결과 탭 컨텍스트 메뉴"""
        tab_bar = self.result_tabs.tabBar()
        tab_index = tab_bar.tabAt(position)

        menu = QMenu(self)
        delete_action = menu.addAction("삭제")
        delete_action.setEnabled(tab_index >= 0)
        delete_action.triggered.connect(lambda: self.close_result_tab(tab_index))

        clear_action = menu.addAction("전체 삭제")
        clear_action.setEnabled(self.result_tabs.count() > 0)
        clear_action.triggered.connect(self._clear_result_tabs)

        menu.exec(tab_bar.mapToGlobal(position))

    def _update_tx_status(self):
        """트랜잭션 상태 UI 업데이트.

        쿼리 실행 중이거나 PostgreSQL 오류로 롤백된 직후에는 커밋/롤백 버튼을
        pending 건수와 무관하게 별도로 가드한다.
        """
        pending_count = len(self.pending_queries)
        try:
            cell_edit_count = sum(
                len(ctx['pending_edits'])
                for _, ctx in self._collect_all_pending_edits()
            )
        except Exception:
            cell_edit_count = 0

        has_pending = pending_count > 0 or cell_edit_count > 0

        if has_pending:
            self.tx_status_frame.setStyleSheet("""
                QFrame {
                    background-color: #FFF3CD;
                    border: 2px solid #FFC107;
                    border-radius: 4px;
                }
            """)
            self.tx_status_icon.setText("⚠️")

            # 라벨 분기: 쿼리 + 셀 편집 / 쿼리만 / 편집만
            if pending_count > 0 and cell_edit_count > 0:
                label_text = f"미커밋 변경: 쿼리 {pending_count}건, 셀 편집 {cell_edit_count}건"
            elif pending_count > 0:
                label_text = f"미커밋 변경: {pending_count}건"
            else:
                label_text = f"미커밋 변경: 셀 편집 {cell_edit_count}건"

            self.tx_info_label.setText(label_text)
            self.tx_info_label.setStyleSheet("color: #856404; font-weight: bold; background: transparent; border: none;")
            self.btn_toggle_pending.setVisible(pending_count > 0)

            # 미커밋 쿼리 목록 업데이트 (DML만 — 셀 편집은 노랑 배경/*N으로 별도 표시)
            self.pending_list_widget.clear()
            for pq in self.pending_queries:
                preview = pq['query'][:50] + "..." if len(pq['query']) > 50 else pq['query']
                preview = preview.replace('\n', ' ')
                item_text = f"[{pq['timestamp']}] {pq['type']} ({pq['affected']}행) - {preview}"
                self.pending_list_widget.addItem(item_text)
            if pending_count == 0:
                self.pending_list_widget.setVisible(False)
        else:
            self.tx_status_frame.setStyleSheet("""
                QFrame {
                    background-color: #E8F4FD;
                    border: 1px solid #B8DAFF;
                    border-radius: 4px;
                }
            """)
            self.tx_status_icon.setText("💾")
            self.tx_info_label.setText("트랜잭션: 대기 중")
            self.tx_info_label.setStyleSheet("color: #004085; background: transparent; border: none;")
            self.btn_toggle_pending.setVisible(False)
            self.pending_list_widget.setVisible(False)
            self.pending_list_widget.clear()

        if self._pg_rolled_back_due_to_error:
            self.tx_info_label.setText("PostgreSQL 오류로 트랜잭션 롤백됨 - 쿼리를 다시 실행하세요")
            self.tx_info_label.setStyleSheet("color: #856404; font-weight: bold; background: transparent; border: none;")
            commit_enabled = False
            rollback_enabled = cell_edit_count > 0
        else:
            commit_enabled = has_pending
            rollback_enabled = has_pending

        if self._query_executing:
            commit_enabled = False
            rollback_enabled = False

        self.btn_commit.setEnabled(commit_enabled)
        self.btn_rollback.setEnabled(rollback_enabled)

    def _toggle_pending_list(self):
        """미커밋 쿼리 목록 펼치기/접기"""
        is_visible = self.pending_list_widget.isVisible()
        self.pending_list_widget.setVisible(not is_visible)
        self.btn_toggle_pending.setText("▲ 접기" if not is_visible else "▼ 상세")

    def _split_queries(self, sql_text):
        """SQL 텍스트를 개별 쿼리로 분리."""
        return parse_sql_statements(sql_text)

    def _get_limit_value(self):
        """LIMIT 설정값 반환 (None이면 제한 없음)"""
        limit_text = self.limit_combo.currentText().strip()
        if limit_text == "제한 없음" or not limit_text:
            return None
        try:
            return int(limit_text)
        except ValueError:
            return None

    def _apply_limit(self, query, limit_value):
        """SELECT/WITH 쿼리에 LIMIT 자동 적용 (이미 LIMIT이 있거나 SHOW/DESCRIBE/EXPLAIN/CALL이면 적용 안함)"""
        classification = classify_sql_statement(query)
        if classification.leading_keyword not in {"select", "with"} or not classification.returns_rows:
            return query

        # 문자열 리터럴/주석을 동일 길이 공백으로 치환 — LIMIT 오탐지(문자열·주석 내부) 방지
        cleaned = re.sub(r"'(?:[^'\\]|\\.)*'", lambda m: ' ' * len(m.group(0)), query, flags=re.DOTALL)
        cleaned = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: ' ' * len(m.group(0)), cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'--[^\n]*', lambda m: ' ' * len(m.group(0)), cleaned)
        cleaned = re.sub(r'#[^\n]*', lambda m: ' ' * len(m.group(0)), cleaned)
        cleaned = re.sub(r'/\*.*?\*/', lambda m: ' ' * len(m.group(0)), cleaned, flags=re.DOTALL)

        # 이미 LIMIT이 있으면 그대로 반환
        if re.search(r'\bLIMIT\b', cleaned, re.IGNORECASE):
            return query

        # 줄바꿈 후 LIMIT 추가 — 같은 줄에 붙이면 trailing `-- comment`에 삼켜질 수 있음
        return f"{query.rstrip()}\nLIMIT {limit_value}"

    def _on_progress(self, msg):
        """진행 메시지"""
        self.message_text.append(msg)
        self._set_message_summary(msg)
        self.status_bar.showMessage(msg)

    def _on_query_result(self, idx, returns_rows, columns, rows, error, affected, exec_time):
        """쿼리 결과 수신 (자동 커밋 모드).

        returns_rows로 분기하며(columns가 빈 리스트여도 0행 SELECT는 결과 탭으로 표시),
        히스토리는 배치 시작 시점이 아니라 쿼리별로 여기서 기록한다.
        """
        worker_query = ''
        if self.worker is not None and hasattr(self.worker, 'queries'):
            try:
                worker_query = self.worker.queries[idx]
            except (IndexError, TypeError):
                worker_query = ''

        if error:
            self.message_text.append(f"❌ 쿼리 {idx + 1}: {error}")
            self._set_message_summary(f"쿼리 {idx + 1} 실패 · {error}")
            self.history_manager.add_query(worker_query, False, 0, exec_time, status='error', error=error)
        elif returns_rows:
            # 편집 가능성 분석 + 설정 (워커에 실행된 원본 쿼리 사용)
            self._add_result_table(columns, rows, exec_time, worker_query)

            self.message_text.append(f"✅ 쿼리 {idx + 1}: {len(rows)}행 반환 ({exec_time:.3f}초)")
            self._set_message_summary(f"쿼리 {idx + 1} 완료 · {len(rows)}행 반환 · {exec_time:.3f}초")
            self._set_message_panel_collapsed(True)
            self.history_manager.add_query(worker_query, True, len(rows), exec_time)
        else:
            # INSERT/UPDATE/DELETE
            self.message_text.append(f"✅ 쿼리 {idx + 1}: {affected}행 영향받음 ({exec_time:.3f}초)")
            self._set_message_summary(f"쿼리 {idx + 1} 완료 · {affected}행 영향 · {exec_time:.3f}초")
            self.history_manager.add_query(worker_query, True, affected, exec_time)

        self.progress_bar.setValue(idx + 1)
        self.status_bar.showMessage(f"쿼리 {idx + 1} 완료 ({exec_time:.3f}초)")

    def _on_finished(self, success, msg):
        """실행 완료"""
        total_elapsed = time.time() - self._exec_start_time if self._exec_start_time else 0
        self.message_text.append(f"\n{msg}")
        self._set_message_summary(f"{msg} · {total_elapsed:.1f}초")
        self._cleanup()
        self.status_bar.showMessage(f"✅ {msg} ({total_elapsed:.1f}초)")

    def _do_commit(self):
        """트랜잭션 커밋 — DML pending_queries + 모든 탭의 셀 편집을 동일 트랜잭션에서 처리"""
        if self._query_executing:
            QMessageBox.warning(self, "경고", "쿼리 실행 중에는 커밋할 수 없습니다.")
            return
        if self._pg_rolled_back_due_to_error:
            QMessageBox.warning(self, "경고", "PostgreSQL 오류로 트랜잭션이 롤백되었습니다. 쿼리를 다시 실행하세요.")
            return
        if not self.db_connection or not self.db_connection.open:
            return

        pending_count = len(self.pending_queries)
        table_edits = self._collect_all_pending_edits()
        cell_edit_count = sum(len(ctx['pending_edits']) for _, ctx in table_edits)

        if pending_count == 0 and cell_edit_count == 0:
            return

        # Production 환경 가드 (셀 편집에 한함 — pending_queries는 실행 시점에 이미 통과)
        if table_edits:
            from src.core.production_guard import ProductionGuard
            guard = ProductionGuard(self)

            # 관여 스키마별로 그룹화
            schemas_with_tables = {}
            for tbl_widget, ctx in table_edits:
                schema_key = ctx['schema'] or (self.db_combo.currentText() or '')
                schemas_with_tables.setdefault(schema_key, []).append(
                    (ctx['table'], len(ctx['pending_edits']))
                )

            for schema, tables_info in schemas_with_tables.items():
                details = '<br>'.join(
                    f'• <code>{t}</code>: {n}개 셀 변경'
                    for t, n in tables_info
                )
                if not guard.confirm_dangerous_operation(
                    self.config,
                    "셀 편집 커밋 (UPDATE)",
                    schema or '(기본 스키마)',
                    f"대상 테이블:<br>{details}"
                ):
                    return  # 사용자 취소 — 커밋 중단

        try:
            # 셀 편집이 있으면 같은 트랜잭션에서 UPDATE 실행
            if table_edits:
                with self.db_connection.cursor() as cursor:
                    failed = self._execute_cell_edits_in_txn(cursor, table_edits)
                if failed:
                    self.db_connection.rollback()
                    msg = '\n'.join(
                        f'행 {r + 1}: {err}' for _, r, err in failed[:10]
                    )
                    extra = f"\n... (총 {len(failed)}건 실패)" if len(failed) > 10 else ""
                    QMessageBox.critical(
                        self, "커밋 실패",
                        f"셀 편집 적용 실패 — 전체 롤백되었습니다"
                        f"{'  (미커밋 쿼리 ' + str(pending_count) + '건 포함)' if pending_count else ''}:\n\n{msg}{extra}"
                    )
                    # 롤백 후 상태 정리
                    history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
                    if history_ids:
                        self.history_manager.update_status_batch(history_ids, 'rolled_back')
                    self.pending_queries.clear()
                    self._update_tx_status()
                    return

            # 최종 커밋 (DML + 셀 편집 UPDATE 모두 포함)
            self.db_connection.commit()

            # 히스토리 상태 업데이트
            history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
            if history_ids:
                self.history_manager.update_status_batch(history_ids, 'committed')

            # 셀 편집 UI 후처리 (UserRole 갱신 + 시각 초기화)
            self._finalize_cell_edits(table_edits)

            # 완료 메시지
            parts = []
            if pending_count > 0:
                parts.append(f"쿼리 {pending_count}건")
            if cell_edit_count > 0:
                parts.append(f"셀 편집 {cell_edit_count}건")
            self.message_text.append(f"\n✅ 커밋 완료! ({', '.join(parts)} 적용됨)")
            self.status_bar.showMessage("커밋 완료")

            self.pending_queries.clear()
            self._update_tx_status()
        except Exception as e:
            try:
                self.db_connection.rollback()
            except Exception:
                pass
            history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
            if history_ids:
                self.history_manager.update_status_batch(history_ids, 'rolled_back')
            self.pending_queries.clear()
            self._update_tx_status()
            self.message_text.append(f"❌ 커밋 실패: {str(e)}")
            QMessageBox.critical(self, "커밋 오류", f"커밋에 실패했습니다:\n{str(e)}")

    def _do_rollback(self):
        """트랜잭션 롤백 — DML + 셀 편집 모두 원복"""
        if self._query_executing:
            QMessageBox.warning(self, "경고", "쿼리 실행 중에는 롤백할 수 없습니다.")
            return
        if not self.db_connection or not self.db_connection.open:
            return

        pending_count = len(self.pending_queries)
        table_edits = self._collect_all_pending_edits()
        cell_edit_count = sum(len(ctx['pending_edits']) for _, ctx in table_edits)

        if pending_count == 0 and cell_edit_count == 0:
            return

        parts = []
        if pending_count > 0:
            parts.append(f"쿼리 {pending_count}건")
        if cell_edit_count > 0:
            parts.append(f"셀 편집 {cell_edit_count}건")
        summary = ', '.join(parts)

        reply = QMessageBox.question(
            self, "롤백 확인",
            f"정말 롤백하시겠습니까?\n{summary} 모두 취소됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.db_connection.rollback()
            self._pg_rolled_back_due_to_error = False

            # 히스토리 상태 업데이트
            history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
            if history_ids:
                self.history_manager.update_status_batch(history_ids, 'rolled_back')

            # 각 테이블 셀 편집 시각/값 복원 (DB 롤백 여부와 무관하게 UI 복원)
            for table, _ in table_edits:
                self._discard_pending_edits(table)

            self.message_text.append(f"\n↩️ 롤백 완료! ({summary} 취소됨)")
            self.status_bar.showMessage("롤백 완료")
            self.pending_queries.clear()
            self._update_tx_status()
        except Exception as e:
            self.message_text.append(f"❌ 롤백 실패: {str(e)}")

    def _close_db_connection(self):
        """DB 연결 종료 (미커밋 시 롤백, 지속 트랜잭션 터널도 함께 정리)"""
        if self.db_connection:
            try:
                if self.pending_queries:
                    self.db_connection.rollback()
                    pending_count = len(self.pending_queries)

                    # 히스토리 상태 업데이트
                    history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
                    if history_ids:
                        self.history_manager.update_status_batch(history_ids, 'rolled_back')

                    self.message_text.append(f"↩️ 연결 종료 - {pending_count}건 자동 롤백됨")
                if self._db_connector:
                    self._db_connector.disconnect()
                else:
                    self.db_connection.close()
            except Exception:
                logger.debug("DB 연결 종료 중 정리 실패", exc_info=True)
            self.db_connection = None
            self._db_connector = None
            self.pending_queries.clear()

            if self._persistent_temp_server:
                self.engine.close_temp_tunnel(self._persistent_temp_server)
                self._persistent_temp_server = None

        self._connected_target = None
        self._pg_rolled_back_due_to_error = False

    def _set_executing_state(self, is_executing: bool):
        """쿼리 실행 상태 UI 전환.

        실행 중에는 실행 버튼/커밋/롤백/DB 콤보/자동 커밋 체크박스와 실행 단축키를 비활성화한다.
        """
        self.btn_execute_current.setEnabled(not is_executing)
        self.btn_execute_all.setEnabled(not is_executing)
        self.db_combo.setEnabled(not is_executing)
        self.auto_commit_check.setEnabled(not is_executing)
        self.progress_bar.setVisible(is_executing)

        for shortcut in (self.shortcut_f5, self.shortcut_ctrl_enter, self.shortcut_ctrl_shift_enter):
            shortcut.setEnabled(not is_executing)

        if is_executing:
            self._query_executing = True
            self.btn_commit.setEnabled(False)
            self.btn_rollback.setEnabled(False)
            self.progress_bar.setValue(0)
            self.progress_bar.setMaximum(0)  # indeterminate 모드 (단일 쿼리)
            self._exec_start_time = time.time()
            self._exec_query_progress = None  # 다중 쿼리 진행률 (예: "2/5")
            self._exec_timer = QTimer()
            self._exec_timer.timeout.connect(self._update_elapsed_time)
            self._exec_timer.start(100)
            self.status_bar.showMessage("⏳ 쿼리 실행 중...")
        else:
            self._query_executing = False
            self.progress_bar.setMaximum(100)  # determinate 복귀
            self._exec_query_progress = None
            if hasattr(self, '_exec_timer') and self._exec_timer:
                self._exec_timer.stop()
                self._exec_timer = None
            self._exec_start_time = None
            self._update_tx_status()

    def _update_elapsed_time(self):
        """경과 시간 실시간 업데이트"""
        if self._exec_start_time:
            elapsed = time.time() - self._exec_start_time
            progress = getattr(self, '_exec_query_progress', None)
            if progress:
                self.status_bar.showMessage(f"⏳ 쿼리 실행 중... ({progress}, {elapsed:.1f}초)")
            else:
                self.status_bar.showMessage(f"⏳ 쿼리 실행 중... ({elapsed:.1f}초)")

    def _cleanup(self):
        """정리 (자동 커밋 모드의 1회성 임시 터널만 종료 — 지속 트랜잭션 터널은 유지)"""
        self._set_executing_state(False)

        if self._autocommit_temp_server:
            self.message_text.append("🛑 임시 터널 종료...")
            self.engine.close_temp_tunnel(self._autocommit_temp_server)
            self._autocommit_temp_server = None

    def _show_table_context_menu(self, position, table, columns):
        """결과 테이블 컨텍스트 메뉴"""
        menu = QMenu(self)

        copy_action = menu.addAction("📋 복사")
        copy_action.triggered.connect(lambda: self._copy_table_data(table, columns, False))

        copy_header_action = menu.addAction("📋 헤더 포함 복사")
        copy_header_action.triggered.connect(lambda: self._copy_table_data(table, columns, True))

        # 편집 기능 메뉴
        ctx = getattr(table, '_edit_context', None)
        if ctx is not None:
            pending = len(ctx['pending_edits'])
            menu.addSeparator()
            if pending > 0:
                discard_action = menu.addAction(f"↩️ 변경사항 취소 ({pending}건)")
                discard_action.triggered.connect(lambda: self._discard_pending_edits(table))
                info = menu.addAction("💡 커밋은 하단 '✅ 커밋' 버튼")
                info.setEnabled(False)
            else:
                info = menu.addAction(
                    f"✏️ 편집 가능 — `{ctx['table']}` (더블클릭 · 커밋은 하단)"
                )
                info.setEnabled(False)
        else:
            menu.addSeparator()
            info = menu.addAction("🔒 읽기 전용 (단일 테이블 SELECT + PK 필요)")
            info.setEnabled(False)

        menu.exec(table.mapToGlobal(position))

    def _copy_table_data(self, table, columns, include_header):
        """테이블 데이터를 탭 구분 형식으로 클립보드에 복사 (Excel 호환)

        컬럼 이동 시 시각적 순서(visual order)를 따름
        """
        selected_ranges = table.selectedRanges()
        if not selected_ranges:
            return

        lines = []
        header = table.horizontalHeader()

        # 선택된 행/열 수집 (logical index 기준)
        all_rows = set()
        all_logical_cols = set()

        for range_ in selected_ranges:
            for row in range(range_.topRow(), range_.bottomRow() + 1):
                all_rows.add(row)
            for col in range(range_.leftColumn(), range_.rightColumn() + 1):
                all_logical_cols.add(col)

        sorted_rows = sorted(all_rows)

        # visual index로 정렬 (컬럼 이동 순서 반영)
        sorted_visual_cols = sorted(
            [header.visualIndex(col) for col in all_logical_cols]
        )

        # 헤더 포함 옵션 (시각적 순서로)
        if include_header:
            header_values = []
            for visual_col in sorted_visual_cols:
                logical_col = header.logicalIndex(visual_col)
                header_values.append(columns[logical_col])
            lines.append('\t'.join(header_values))

        # 데이터 행 (시각적 순서로)
        for row in sorted_rows:
            row_data = []
            for visual_col in sorted_visual_cols:
                logical_col = header.logicalIndex(visual_col)
                item = table.item(row, logical_col)
                value = item.text() if item else ''
                # 탭과 줄바꿈은 공백으로 치환 (셀 구분 보호)
                value = value.replace('\t', ' ').replace('\n', ' ')
                row_data.append(value)
            lines.append('\t'.join(row_data))

        QApplication.clipboard().setText('\n'.join(lines))

    # =====================================================================
    # 결과 테이블 편집 (MVP: 단일 테이블 SELECT + PK 있을 때만 허용)
    # =====================================================================
    def _analyze_query_editability(self, query):
        """SELECT 쿼리에서 편집 가능한 단일 테이블 정보 추출.

        반환: {'schema': str|None, 'table': str} 또는 None
        """
        if not query:
            return None

        # 주석 제거
        q = re.sub(r'/\*.*?\*/', ' ', query, flags=re.DOTALL)
        q = re.sub(r'--[^\n]*', ' ', q)
        q_norm = q.strip().rstrip(';').strip()
        if not q_norm:
            return None

        q_upper = q_norm.upper()
        if not q_upper.startswith('SELECT'):
            return None

        # 복잡 구조 거부 (JOIN / UNION / GROUP BY / HAVING / DISTINCT / 집계)
        forbidden_patterns = [
            r'\bJOIN\b', r'\bUNION\b', r'\bGROUP\s+BY\b',
            r'\bHAVING\b', r'\bDISTINCT\b',
        ]
        for pat in forbidden_patterns:
            if re.search(pat, q_upper):
                return None
        if re.search(r'\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(', q_upper):
            return None

        # FROM 절 테이블 이름 추출: `schema`.`table` 또는 schema.table 또는 table
        m = re.search(
            r'\bFROM\s+(`[^`]+`|"[^"]+"|[\w$]+)(\s*\.\s*(`[^`]+`|"[^"]+"|[\w$]+))?',
            q_norm, re.IGNORECASE
        )
        if not m:
            return None

        # FROM 뒤에 서브쿼리 괄호가 붙는 경우 거부
        after_from = q_norm[m.start():]
        from_kw_end = re.search(r'\bFROM\s+', after_from, re.IGNORECASE).end()
        if after_from[from_kw_end:].lstrip().startswith('('):
            return None

        part1 = m.group(1).strip().strip('`"')
        part2 = m.group(3).strip().strip('`"') if m.group(3) else None
        schema, table = (part1, part2) if part2 else (None, part1)

        # 여러 테이블(콤마 결합) 거부 - FROM 뒤 WHERE 이전 구간에 쉼표 있으면 탈락
        rest = q_norm[m.end():]
        stop = re.search(r'\b(WHERE|ORDER|LIMIT|GROUP|HAVING|FOR)\b', rest, re.IGNORECASE)
        rest_check = rest[:stop.start()] if stop else rest
        if ',' in rest_check:
            return None

        return {'schema': schema, 'table': table}

    def _fetch_primary_keys(self, schema, table):
        """PK 컬럼명 조회 (엔진별 분기).

        PostgreSQL의 information_schema.columns에는 MySQL의 COLUMN_KEY가 없으므로
        table_constraints/key_column_usage 조인으로 PK를 조회해야 한다.
        """
        if not self.db_connection or not self.db_connection.open:
            return []
        db_engine = self._db_engine()
        try:
            with self.db_connection.cursor() as cursor:
                if db_engine == 'postgresql':
                    pg_schema = schema or self.db_combo.currentText().strip() or "public"
                    cursor.execute(
                        "SELECT kcu.column_name "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "  ON tc.constraint_name = kcu.constraint_name "
                        " AND tc.table_schema = kcu.table_schema "
                        " AND tc.table_name = kcu.table_name "
                        "WHERE tc.constraint_type = 'PRIMARY KEY' "
                        "  AND tc.table_schema = %s "
                        "  AND tc.table_name = %s "
                        "ORDER BY kcu.ordinal_position",
                        (pg_schema, table)
                    )
                elif schema:
                    cursor.execute(
                        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_KEY='PRI' "
                        "ORDER BY ORDINAL_POSITION",
                        (schema, table)
                    )
                else:
                    cursor.execute(
                        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_KEY='PRI' "
                        "ORDER BY ORDINAL_POSITION",
                        (table,)
                    )
                rows = cursor.fetchall()
        except Exception:
            logger.debug("PK 컬럼 조회 실패: schema=%s table=%s", schema, table, exc_info=True)
            return []

        pks = []
        for row in rows:
            if isinstance(row, dict):
                pks.append(row.get('COLUMN_NAME') or row.get('column_name'))
            else:
                pks.append(row[0])
        return [p for p in pks if p]

    def _setup_result_table_editability(self, table, query, columns, rows):
        """결과 테이블에 편집 기능 설정.

        편집 가능 조건: 단일 테이블 SELECT + PK 존재 + 모든 PK 컬럼이 결과에 포함.
        조건 미충족 시 전체 읽기 전용.
        """
        edit_ctx = None
        analysis = self._analyze_query_editability(query)
        if analysis and self.db_connection and self.db_connection.open:
            pk_cols = self._fetch_primary_keys(analysis['schema'], analysis['table'])
            if pk_cols:
                col_lower = [c.lower() for c in columns]
                pk_indices = []
                all_present = True
                for pk in pk_cols:
                    if pk.lower() in col_lower:
                        pk_indices.append(col_lower.index(pk.lower()))
                    else:
                        all_present = False
                        break
                if all_present:
                    edit_ctx = {
                        'schema': analysis['schema'],
                        'table': analysis['table'],
                        'pk_columns': pk_cols,
                        'pk_indices': pk_indices,
                        'columns': list(columns),
                        'pending_edits': {},
                    }

        if edit_ctx is not None:
            # 원본값을 UserRole에 저장 + PK 셀은 편집 불가 플래그
            table.blockSignals(True)
            try:
                for r in range(len(rows)):
                    for c in range(len(columns)):
                        item = table.item(r, c)
                        if item is None:
                            continue
                        item.setData(Qt.ItemDataRole.UserRole, rows[r][c])
                        if c in edit_ctx['pk_indices']:
                            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            finally:
                table.blockSignals(False)

            table._edit_context = edit_ctx
            table.setEditTriggers(
                QAbstractItemView.EditTrigger.DoubleClicked
                | QAbstractItemView.EditTrigger.EditKeyPressed
                | QAbstractItemView.EditTrigger.AnyKeyPressed
            )
            table.itemChanged.connect(
                lambda item, t=table: self._on_result_cell_changed(t, item)
            )
        else:
            table._edit_context = None
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def _on_result_cell_changed(self, table, item):
        """셀 편집 시 변경사항 트래킹 + 시각 표시"""
        ctx = getattr(table, '_edit_context', None)
        if ctx is None:
            return
        row, col = item.row(), item.column()
        if col in ctx['pk_indices']:
            return

        original = item.data(Qt.ItemDataRole.UserRole)
        new_text = item.text()
        if new_text.upper() == 'NULL':
            new_value, is_null = None, True
        else:
            new_value, is_null = new_text, False

        orig_is_null = original is None
        if is_null and orig_is_null:
            changed = False
        elif is_null != orig_is_null:
            changed = True
        else:
            changed = str(original) != str(new_value)

        key = (row, col)
        if changed:
            ctx['pending_edits'][key] = new_value
            item.setBackground(QColor('#FFF59D'))
            if is_null:
                item.setForeground(QColor('#888888'))
            else:
                item.setForeground(QColor('#000000'))
        else:
            ctx['pending_edits'].pop(key, None)
            item.setBackground(QColor(0, 0, 0, 0))
            if orig_is_null:
                item.setForeground(QColor('#888888'))
            else:
                item.setForeground(QColor('#000000'))

        self._update_edit_tab_title(table)
        self._update_tx_status()

    def _update_edit_tab_title(self, table):
        """결과 탭 제목에 변경사항 개수 표시"""
        ctx = getattr(table, '_edit_context', None)
        if ctx is None:
            return
        idx = self.result_tabs.indexOf(table)
        if idx < 0:
            return
        current = self.result_tabs.tabText(idx)
        base = re.sub(r'\s*\*\d+$', '', current)
        n = len(ctx['pending_edits'])
        self.result_tabs.setTabText(idx, f"{base} *{n}" if n > 0 else base)

    def _collect_all_pending_edits(self):
        """편집 변경사항이 있는 모든 결과 탭 수집.

        반환: [(table, edit_ctx), ...] — pending_edits가 비어있지 않은 것만.
        """
        results = []
        for idx in range(self.result_tabs.count()):
            widget = self.result_tabs.widget(idx)
            if not isinstance(widget, QTableWidget):
                continue
            ctx = getattr(widget, '_edit_context', None)
            if ctx and ctx['pending_edits']:
                results.append((widget, ctx))
        return results

    def _execute_cell_edits_in_txn(self, cursor, table_edits):
        """셀 편집 목록을 주어진 cursor로 UPDATE 실행.

        동일 트랜잭션 내에서 실행되므로 autocommit/commit 관리는 호출자 책임.
        반환: failed 리스트 [(table, row_idx, error_msg), ...]. 비어있으면 전체 성공.
        """
        failed = []
        for table, ctx in table_edits:
            schema, tbl = ctx['schema'], ctx['table']
            qualified = (
                f"{self._quote_editor_identifier(schema)}.{self._quote_editor_identifier(tbl)}"
                if schema else self._quote_editor_identifier(tbl)
            )
            columns = ctx['columns']
            pk_cols = ctx['pk_columns']
            pk_indices = ctx['pk_indices']

            # 행별로 묶기
            by_row = {}
            for (row, col), value in ctx['pending_edits'].items():
                by_row.setdefault(row, {})[col] = value

            for row_idx in sorted(by_row):
                col_values = by_row[row_idx]
                set_parts = []
                params = []
                for c, v in col_values.items():
                    set_parts.append(f"{self._quote_editor_identifier(columns[c])}=%s")
                    params.append(v)
                where_parts = []
                for i, pk_idx in enumerate(pk_indices):
                    itm = table.item(row_idx, pk_idx)
                    raw = itm.data(Qt.ItemDataRole.UserRole) if itm else None
                    quoted_pk = self._quote_editor_identifier(pk_cols[i])
                    if raw is None:
                        where_parts.append(f"{quoted_pk} IS NULL")
                    else:
                        where_parts.append(f"{quoted_pk}=%s")
                        params.append(raw)
                sql = (
                    f"UPDATE {qualified} SET {', '.join(set_parts)} "
                    f"WHERE {' AND '.join(where_parts)}"
                )
                try:
                    affected = cursor.execute(sql, params)
                    if affected != 1:
                        failed.append((table, row_idx, f'영향받은 행 수: {affected}'))
                except Exception as e:
                    failed.append((table, row_idx, str(e)))
        return failed

    def _finalize_cell_edits(self, table_edits):
        """커밋 성공 후 각 테이블의 UserRole 갱신 + 시각 초기화."""
        for table, ctx in table_edits:
            table.blockSignals(True)
            try:
                for (row, col), new_value in ctx['pending_edits'].items():
                    item = table.item(row, col)
                    if item is None:
                        continue
                    item.setData(Qt.ItemDataRole.UserRole, new_value)
                    if new_value is None:
                        item.setText('NULL')
                        item.setForeground(QColor('#888888'))
                    else:
                        item.setText(str(new_value))
                        item.setForeground(QColor('#000000'))
                    item.setBackground(QColor(0, 0, 0, 0))
            finally:
                table.blockSignals(False)
            ctx['pending_edits'].clear()
            self._update_edit_tab_title(table)

    def _discard_pending_edits(self, table):
        """변경사항 취소 — 원본값으로 되돌림"""
        ctx = getattr(table, '_edit_context', None)
        if ctx is None or not ctx['pending_edits']:
            return
        table.blockSignals(True)
        try:
            for (row, col) in list(ctx['pending_edits'].keys()):
                item = table.item(row, col)
                if item is None:
                    continue
                orig = item.data(Qt.ItemDataRole.UserRole)
                if orig is None:
                    item.setText('NULL')
                    item.setForeground(QColor('#888888'))
                else:
                    item.setText(str(orig))
                    item.setForeground(QColor('#000000'))
                item.setBackground(QColor(0, 0, 0, 0))
        finally:
            table.blockSignals(False)
        ctx['pending_edits'].clear()
        self._update_edit_tab_title(table)
        self._update_tx_status()

    def close_result_tab(self, index):
        """결과 탭 닫기 (미저장 셀 편집이 있으면 확인)"""
        if not (0 <= index < self.result_tabs.count()):
            return
        count = self._pending_edit_count_for_result_tab(index)
        if not self._confirm_discard_pending_edits(count, "이 탭을 닫으면"):
            return
        self.result_tabs.removeTab(index)
        if self.result_tabs.count() == 0:
            self._result_counter = 0

    def open_file(self):
        """SQL 파일 열기 (새 탭에서)"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "SQL 파일 열기", "",
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if file_path:
            # 현재 탭이 빈 새 탭이면 거기에 로드, 아니면 새 탭 생성
            current_tab = self._current_tab()
            if current_tab and not current_tab.is_modified and not current_tab.file_path and not current_tab.editor.toPlainText().strip():
                # 현재 탭에 로드
                if current_tab.load_file(file_path):
                    self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {file_path}")
                    self.message_text.append(f"📂 파일 열림: {file_path}")
                else:
                    QMessageBox.critical(self, "오류", f"파일을 열 수 없습니다:\n{file_path}")
            else:
                # 새 탭에 로드
                self._add_new_tab(file_path)
                self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {file_path}")

    def save_file(self):
        """현재 탭 저장"""
        tab = self._current_tab()
        if not tab:
            return

        self._save_tab(tab)
        # 윈도우 제목 업데이트
        if tab.file_path:
            self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {tab.file_path}")

    def save_file_as(self):
        """다른 이름으로 저장"""
        tab = self._current_tab()
        if not tab:
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "SQL 파일 저장", "",
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if file_path:
            success, path, error = tab.save_file(file_path)
            if success:
                self.message_text.append(f"💾 파일 저장됨: {path}")
                self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {path}")
            else:
                QMessageBox.critical(self, "오류", f"파일을 저장할 수 없습니다:\n{error}")

    def show_history(self):
        """히스토리 다이얼로그 표시"""
        dialog = HistoryDialog(self, self.history_manager)
        dialog.query_selected.connect(self._on_history_selected)
        dialog.exec()

    def _on_history_selected(self, query):
        """히스토리에서 쿼리 선택됨"""
        self.editor.setPlainText(query)

    # =====================================================================
    # SQL 검증 및 자동완성
    # =====================================================================
    def _on_schema_changed(self, schema: str):
        """스키마 변경 시 메타데이터 리로드.

        지속 트랜잭션 연결의 대상과 다른 DB/스키마로 바뀌면, 미커밋 변경이 있는 동안은
        확인 후에만 진행하고 취소 시 콤보를 원래 선택으로 되돌린다.
        """
        if self._schema_change_guard:
            return
        if not schema or not schema.strip():
            return

        target = self._database_and_schema_for_selection(schema)

        if target != self._connected_target and self.db_connection and self.db_connection.open:
            has_pending = bool(self.pending_queries) or bool(self._collect_all_pending_edits())
            if has_pending:
                reply = QMessageBox.question(
                    self, "DB/스키마 변경",
                    "DB/스키마를 변경하면 현재 미커밋 변경사항과 셀 편집이 롤백/삭제됩니다. 계속하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    self._restore_db_combo_to_connected_target()
                    return
                self._close_db_connection()
                self._discard_all_pending_edits()
            else:
                self._close_db_connection()

        self.metadata_provider.invalidate()
        self._load_metadata(schema)

    def _restore_db_combo_to_connected_target(self):
        """스키마 변경을 취소했을 때 db_combo를 현재 연결된 대상으로 되돌림"""
        if self._connected_target is None:
            return
        database, schema = self._connected_target
        previous_text = schema if self._db_engine() == "postgresql" else database
        if not previous_text:
            return
        self._schema_change_guard = True
        try:
            index = self.db_combo.findText(previous_text)
            if index >= 0:
                self.db_combo.setCurrentIndex(index)
            else:
                self.db_combo.setCurrentText(previous_text)
        finally:
            self._schema_change_guard = False

    def _retire_worker(self, worker, cancel: bool = True, cleanup=None):
        """실행 중인 워커를 finished 시그널이 올 때까지 참조를 유지한 채 취소/교체한다.

        QThread를 곧바로 교체하면 아직 실행 중인 스레드 객체의 참조가 끊겨 GC 크래시로
        이어질 수 있으므로, finished가 발생할 때까지 self._retired_workers에 보관한다.
        """
        if worker is None:
            return
        if cancel and hasattr(worker, 'cancel') and worker.isRunning():
            worker.cancel()

        self._retired_workers.append(worker)

        def _on_finished(w=worker):
            if w in self._retired_workers:
                self._retired_workers.remove(w)
            if cleanup:
                cleanup()
            w.deleteLater()

        worker.finished.connect(_on_finished)

    def _load_metadata(self, schema: str = None):
        """메타데이터 백그라운드 로드"""
        from src.ui.workers.validation_worker import MetadataLoadWorker

        # 기존 워커 취소 — 연결은 그 워커가 실제로 끝날 때까지 유지 후 정리
        if self.metadata_worker and self.metadata_worker.isRunning():
            old_connector = self._metadata_connector

            def _cleanup_old_connector(connector=old_connector):
                if connector:
                    try:
                        connector.disconnect()
                    except Exception:
                        logger.debug("메타데이터 연결 정리 실패", exc_info=True)

            self._retire_worker(self.metadata_worker, cleanup=_cleanup_old_connector)
            self._metadata_connector = None
        self.metadata_worker = None

        # 연결 확보
        db_user, db_password = self._db_credentials()

        if not db_user:
            return

        try:
            host, port, _temp_server, error = self._resolve_db_target(allow_temp_tunnel=False)
            if error or not host or not port:
                return

            target_schema = schema or self.db_combo.currentText().strip()
            if not target_schema:
                return

            database, connector_schema = self._database_and_schema_for_selection(target_schema)
            connector = self._create_db_connector(
                host,
                port,
                db_user,
                db_password,
                database,
                connector_schema,
            )
            success, msg = connector.connect()
            if not success:
                error_text = f"❌ 메타데이터 DB 연결 실패: {msg}"
                self.validation_label.setText(error_text)
                self.message_text.append(error_text)
                return

            # 연결 저장 (워커 완료 후 정리용)
            self._metadata_connector = connector

            # 메타데이터 로드 워커 시작
            self.metadata_provider.set_connector(connector)
            self.metadata_worker = MetadataLoadWorker(connector, target_schema)
            self.metadata_worker.progress.connect(self._on_metadata_progress)
            self.metadata_worker.load_completed.connect(self._on_metadata_loaded)
            self.metadata_worker.error_occurred.connect(self._on_metadata_error)
            self.metadata_worker.start()

            self.validation_label.setText("🔄 메타데이터 로드 중...")

        except Exception as e:
            self.validation_label.setText(f"❌ 메타데이터 로드 실패: {str(e)}")

    def _on_metadata_progress(self, msg: str):
        """메타데이터 로드 진행"""
        self.validation_label.setText(f"🔄 {msg}")

    def _on_metadata_loaded(self, metadata):
        """메타데이터 로드 완료"""
        # 연결 정리 (메타데이터는 이미 메모리에 로드됨)
        if self._metadata_connector:
            try:
                self._metadata_connector.disconnect()
            except Exception:
                pass
            self._metadata_connector = None

        # 캐시된 메타데이터 업데이트
        self.metadata_provider._metadata = metadata
        self._populate_schema_tree(metadata)

        table_count = len(metadata.tables)
        version = format_metadata_db_version(metadata.db_version)
        self.validation_label.setText(f"✅ {table_count}개 테이블 로드됨 (MySQL {version})")

        # 현재 SQL 재검증
        self._on_validation_requested(self.editor.toPlainText())

    def _populate_schema_tree(self, metadata):
        """Populate the side schema/table tree from loaded metadata."""
        self.schema_tree.clear()
        schema_name = self.db_combo.currentText().strip() or self.config.get("default_database") or "Schema"
        schema_names = [
            self.db_combo.itemText(index).strip()
            for index in range(self.db_combo.count())
            if self.db_combo.itemText(index).strip()
        ] or [schema_name]

        selected_root = None
        for name in schema_names:
            root = QTreeWidgetItem([name])
            root.setData(0, Qt.ItemDataRole.UserRole, {"kind": "schema", "name": name})
            self.schema_tree.addTopLevelItem(root)
            if name != schema_name:
                continue
            selected_root = root
            for table in sorted(metadata.tables):
                table_item = QTreeWidgetItem([table])
                table_item.setData(0, Qt.ItemDataRole.UserRole, {"kind": "table", "name": table})
                root.addChild(table_item)

                for column in sorted(metadata.columns.get(table, [])):
                    column_item = QTreeWidgetItem([column])
                    column_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        {"kind": "column", "name": column, "table": table},
                    )
                    table_item.addChild(column_item)

        if selected_root:
            selected_root.setExpanded(True)

    def _on_schema_tree_item_clicked(self, item, _column):
        payload = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(payload, dict) or payload.get("kind") != "table":
            return
        if not self.editor:
            return
        self.editor.insertPlainText(f"{self._quote_editor_identifier(payload.get('name') or '')} ")

    def _quote_editor_identifier(self, name: str) -> str:
        if self._db_engine() == "postgresql":
            return f'"{name.replace(chr(34), chr(34) + chr(34))}"'
        return f"`{name.replace('`', '``')}`"

    def _on_metadata_error(self, error: str):
        """메타데이터 로드 오류"""
        # 연결 정리
        if self._metadata_connector:
            try:
                self._metadata_connector.disconnect()
            except Exception:
                pass
            self._metadata_connector = None

        self.validation_label.setText(f"⚠️ {error}")

    def _on_validation_requested(self, sql: str):
        """검증 요청 (debounce 후 호출)"""
        from src.ui.workers.validation_worker import ValidationWorker

        if not sql.strip():
            self.editor.set_validation_issues([])
            self.validation_label.setText("")
            return

        # 메타데이터가 없으면 스킵
        if not self.metadata_provider._metadata or not self.metadata_provider._metadata.tables:
            return

        # 기존 워커 취소 (finished까지 참조 유지)
        if self.validation_worker and self.validation_worker.isRunning():
            self._retire_worker(self.validation_worker)

        schema = self.db_combo.currentText().strip()
        self.validation_worker = ValidationWorker(self.sql_validator, sql, schema)
        self.validation_worker.validation_completed.connect(self._on_validation_completed)
        self.validation_worker.start()

    def _on_validation_completed(self, issues: list):
        """검증 완료"""
        from src.core.sql_validator import IssueSeverity

        self.editor.set_validation_issues(issues)

        # 상태 요약
        errors = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
        warnings = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)

        if errors == 0 and warnings == 0:
            self.validation_label.setText("✅ 검증 통과")
            self.validation_label.setStyleSheet("color: #27ae60; font-size: 11px;")
        else:
            parts = []
            if errors > 0:
                parts.append(f"❌ {errors}개 오류")
            if warnings > 0:
                parts.append(f"⚠️ {warnings}개 경고")
            self.validation_label.setText(" / ".join(parts))
            self.validation_label.setStyleSheet(
                f"color: {'#e74c3c' if errors > 0 else '#f39c12'}; font-size: 11px; font-weight: bold;"
            )

    def _on_autocomplete_requested(self, sql: str, cursor_pos: int):
        """자동완성 요청"""
        from src.ui.workers.validation_worker import AutoCompleteWorker

        # 메타데이터가 없으면 키워드만 제공
        schema = self.db_combo.currentText().strip()

        # 기존 워커 취소 (finished까지 참조 유지)
        if self.autocomplete_worker and self.autocomplete_worker.isRunning():
            self._retire_worker(self.autocomplete_worker)

        self.autocomplete_worker = AutoCompleteWorker(
            self.sql_completer, sql, cursor_pos, schema
        )
        self.autocomplete_worker.completions_ready.connect(self._on_autocomplete_ready)
        self.autocomplete_worker.start()

    def _on_autocomplete_ready(self, completions: list):
        """자동완성 목록 준비됨"""
        self.editor.show_autocomplete_popup(completions)

    def closeEvent(self, event):
        """다이얼로그 닫기.

        실행 중인 쿼리는 강제로 끊지 않는다 — 완료를 기다리거나(협조적 인터럽션 요청)
        닫기 자체를 취소한다. DB 작업 도중 다이얼로그를 파괴하면 워커 스레드가 이미
        사라진 connection을 참조해 크래시할 수 있기 때문이다.
        """
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "확인",
                "쿼리가 실행 중입니다. 현재 DB 작업은 즉시 중단되지 않을 수 있습니다. "
                "종료 전에 완료를 기다리시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.requestInterruption()
            self.worker.wait()

        # 미커밋 쿼리 + 미저장 셀 편집 + 수정된 탭 확인
        warnings = []
        if self.pending_queries:
            warnings.append(f"미커밋 변경사항 {len(self.pending_queries)}건 (롤백됨)")

        pending_edit_count = sum(
            self._pending_edit_count_for_result_tab(i)
            for i in range(self.result_tabs.count())
        )
        if pending_edit_count > 0:
            warnings.append(f"저장되지 않은 셀 편집 {pending_edit_count}건")

        # 수정된 탭 목록 확인
        modified_tabs = []
        for i in range(self.editor_tabs.count()):
            tab = self.editor_tabs.widget(i)
            if tab and tab.is_modified:
                modified_tabs.append(tab.get_title().rstrip(' *'))

        if modified_tabs:
            warnings.append(f"저장되지 않은 SQL 편집 내용 ({len(modified_tabs)}개 탭)")

        if warnings:
            msg = "\n".join(f"• {w}" for w in warnings)
            reply = QMessageBox.question(
                self, "닫기 확인",
                f"다음 내용이 손실됩니다:\n\n{msg}\n\n계속하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        # 정리 및 종료
        self._close_db_connection()
        self._cleanup()

        # 검증/자동완성/메타데이터 워커 정리 — 닫는 시점에는 완전히 멈출 때까지 대기해도 무방
        for worker_attr in ('validation_worker', 'autocomplete_worker', 'metadata_worker'):
            worker = getattr(self, worker_attr)
            if worker and worker.isRunning():
                worker.cancel()
                worker.wait()

        # 메타데이터 연결 정리 (워커가 완전히 멈춘 뒤에만)
        if self._metadata_connector:
            try:
                self._metadata_connector.disconnect()
            except Exception:
                logger.debug("메타데이터 연결 정리 실패 (닫기)", exc_info=True)
            self._metadata_connector = None

        event.accept()
