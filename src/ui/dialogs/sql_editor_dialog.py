"""
SQL 에디터 다이얼로그
- SQL 쿼리 작성 및 실행
- 구문 하이라이팅
- 결과 테이블 표시
"""
import time
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QGroupBox, QSplitter, QPlainTextEdit, QTextEdit, QWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
    QStatusBar, QApplication, QAbstractItemView, QListWidget, QListWidgetItem,
    QDialogButtonBox
)
from PyQt6.QtCore import Qt, QRect, QSize, pyqtSignal, QThread
from PyQt6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont, QPainter,
    QTextCursor, QKeySequence, QShortcut
)
import re


# =====================================================================
# SQL 구문 하이라이터
# =====================================================================
class SQLHighlighter(QSyntaxHighlighter):
    """SQL 구문 하이라이팅"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_rules()

    def _init_rules(self):
        """하이라이팅 규칙 초기화"""
        self.highlighting_rules = []

        # 키워드 포맷
        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor("#569CD6"))  # 파란색
        keyword_format.setFontWeight(QFont.Weight.Bold)

        keywords = [
            "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "LIKE",
            "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
            "CREATE", "ALTER", "DROP", "TABLE", "INDEX", "VIEW", "DATABASE",
            "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "CROSS", "ON",
            "GROUP", "BY", "ORDER", "ASC", "DESC", "HAVING", "LIMIT", "OFFSET",
            "UNION", "ALL", "DISTINCT", "AS", "CASE", "WHEN", "THEN", "ELSE", "END",
            "NULL", "IS", "BETWEEN", "EXISTS", "PRIMARY", "KEY", "FOREIGN",
            "REFERENCES", "CONSTRAINT", "DEFAULT", "AUTO_INCREMENT",
            "TRUNCATE", "BEGIN", "COMMIT", "ROLLBACK", "TRANSACTION",
            "IF", "ELSE", "WHILE", "DECLARE", "CURSOR", "FETCH", "PROCEDURE", "FUNCTION",
            "RETURNS", "RETURN", "CALL", "TRIGGER", "BEFORE", "AFTER", "FOR", "EACH", "ROW",
            "TRUE", "FALSE", "USE", "SHOW", "DESCRIBE", "EXPLAIN", "GRANT", "REVOKE"
        ]

        for word in keywords:
            pattern = rf"\b{word}\b"
            self.highlighting_rules.append((re.compile(pattern, re.IGNORECASE), keyword_format))

        # 함수 포맷
        function_format = QTextCharFormat()
        function_format.setForeground(QColor("#DCDCAA"))  # 노란색

        functions = [
            "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "IFNULL", "NULLIF",
            "CONCAT", "SUBSTRING", "LENGTH", "TRIM", "UPPER", "LOWER", "REPLACE",
            "NOW", "DATE", "TIME", "DATETIME", "TIMESTAMP", "YEAR", "MONTH", "DAY",
            "HOUR", "MINUTE", "SECOND", "DATEDIFF", "DATE_ADD", "DATE_SUB",
            "CAST", "CONVERT", "ROUND", "FLOOR", "CEIL", "ABS", "MOD", "POWER",
            "GROUP_CONCAT", "JSON_EXTRACT", "JSON_ARRAY", "JSON_OBJECT"
        ]

        for word in functions:
            pattern = rf"\b{word}\s*\("
            self.highlighting_rules.append((re.compile(pattern, re.IGNORECASE), function_format))

        # 숫자 포맷
        number_format = QTextCharFormat()
        number_format.setForeground(QColor("#B5CEA8"))  # 연두색
        self.highlighting_rules.append((re.compile(r"\b\d+\.?\d*\b"), number_format))

        # 문자열 포맷 (작은따옴표)
        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))  # 주황색
        self.highlighting_rules.append((re.compile(r"'[^']*'"), string_format))
        self.highlighting_rules.append((re.compile(r'"[^"]*"'), string_format))

        # 주석 포맷
        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#6A9955"))  # 녹색
        comment_format.setFontItalic(True)
        self.highlighting_rules.append((re.compile(r"--[^\n]*"), comment_format))
        self.highlighting_rules.append((re.compile(r"#[^\n]*"), comment_format))

        # 멀티라인 주석 저장
        self.multiline_comment_format = comment_format

    def highlightBlock(self, text):
        """블록 하이라이팅"""
        # 일반 규칙 적용
        for pattern, format_ in self.highlighting_rules:
            for match in pattern.finditer(text):
                start = match.start()
                length = match.end() - start
                self.setFormat(start, length, format_)

        # 멀티라인 주석 처리
        self.setCurrentBlockState(0)

        start_index = 0
        if self.previousBlockState() != 1:
            start_match = re.search(r"/\*", text)
            start_index = start_match.start() if start_match else -1

        while start_index >= 0:
            end_match = re.search(r"\*/", text[start_index + 2:])
            if end_match:
                end_index = start_index + 2 + end_match.end()
                comment_length = end_index - start_index
            else:
                self.setCurrentBlockState(1)
                comment_length = len(text) - start_index

            self.setFormat(start_index, comment_length, self.multiline_comment_format)

            start_match = re.search(r"/\*", text[start_index + comment_length:])
            start_index = (start_index + comment_length + start_match.start()) if start_match else -1


# =====================================================================
# 줄 번호 위젯
# =====================================================================
class LineNumberArea(QWidget):
    """줄 번호 표시 영역"""

    def __init__(self, editor):
        super().__init__(editor)
        self.code_editor = editor

    def sizeHint(self):
        return QSize(self.code_editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.code_editor.line_number_area_paint_event(event)


# =====================================================================
# 코드 에디터 (줄 번호 + 하이라이팅)
# =====================================================================
class CodeEditor(QPlainTextEdit):
    """줄 번호가 있는 코드 에디터"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # 에디터 스타일
        self.setStyleSheet("""
            QPlainTextEdit {
                background-color: #1E1E1E;
                color: #D4D4D4;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: 1px solid #3C3C3C;
                selection-background-color: #264F78;
            }
        """)

        # 줄 번호 영역
        self.line_number_area = LineNumberArea(self)

        # 신호 연결
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)

        self.update_line_number_area_width(0)
        self.highlight_current_line()

        # SQL 하이라이터
        self.highlighter = SQLHighlighter(self.document())

        # 탭 크기 설정
        self.setTabStopDistance(40)

    def line_number_area_width(self):
        """줄 번호 영역 너비 계산"""
        digits = 1
        max_num = max(1, self.blockCount())
        while max_num >= 10:
            max_num //= 10
            digits += 1
        space = 10 + self.fontMetrics().horizontalAdvance('9') * digits
        return space

    def update_line_number_area_width(self, _):
        """줄 번호 영역 너비 업데이트"""
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        """줄 번호 영역 업데이트"""
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())

        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        """리사이즈 이벤트"""
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        """줄 번호 그리기"""
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#252526"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(QColor("#858585"))
                painter.drawText(0, top, self.line_number_area.width() - 5,
                                 self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, number)

            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def highlight_current_line(self):
        """현재 줄 하이라이트"""
        extra_selections = []

        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            line_color = QColor("#2D2D2D")
            selection.format.setBackground(line_color)
            selection.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)

        self.setExtraSelections(extra_selections)


# =====================================================================
# SQL 쿼리 실행 워커
# =====================================================================
class SQLQueryWorker(QThread):
    """SQL 쿼리 실행 워커"""
    progress = pyqtSignal(str)
    query_result = pyqtSignal(int, list, list, str, int, float)  # idx, columns, rows, error, affected, time
    finished = pyqtSignal(bool, str)

    def __init__(self, host, port, user, password, database, queries):
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.queries = queries  # List of query strings

    def run(self):
        from src.core.db_connector import MySQLConnector
        import pymysql

        try:
            connector = MySQLConnector(self.host, self.port, self.user, self.password, self.database)
            success, msg = connector.connect()

            if not success:
                self.finished.emit(False, f"연결 실패: {msg}")
                return

            self.progress.emit(f"✅ 연결 성공: {self.host}:{self.port}")

            total_queries = len(self.queries)
            success_count = 0
            error_count = 0

            for idx, query in enumerate(self.queries):
                query = query.strip()
                if not query:
                    continue

                self.progress.emit(f"📄 쿼리 {idx + 1}/{total_queries} 실행 중...")

                start_time = time.time()
                try:
                    # 직접 커서 사용하여 실행
                    with connector.connection.cursor() as cursor:
                        cursor.execute(query)

                        # SELECT 쿼리인지 확인
                        if cursor.description:
                            # SELECT 결과
                            columns = [desc[0] for desc in cursor.description]
                            rows = cursor.fetchall()
                            # Dict to list 변환
                            row_list = []
                            for row in rows:
                                if isinstance(row, dict):
                                    row_list.append([row.get(col) for col in columns])
                                else:
                                    row_list.append(list(row))

                            execution_time = time.time() - start_time
                            self.query_result.emit(idx, columns, row_list, "", len(row_list), execution_time)
                            success_count += 1
                        else:
                            # INSERT, UPDATE, DELETE 등
                            affected = cursor.rowcount
                            connector.connection.commit()
                            execution_time = time.time() - start_time
                            self.query_result.emit(idx, [], [], "", affected, execution_time)
                            success_count += 1

                except pymysql.Error as e:
                    execution_time = time.time() - start_time
                    error_msg = f"MySQL 오류 ({e.args[0]}): {e.args[1] if len(e.args) > 1 else str(e)}"
                    self.query_result.emit(idx, [], [], error_msg, 0, execution_time)
                    error_count += 1

                except Exception as e:
                    execution_time = time.time() - start_time
                    self.query_result.emit(idx, [], [], str(e), 0, execution_time)
                    error_count += 1

            connector.disconnect()

            if error_count == 0:
                self.finished.emit(True, f"✅ {success_count}개 쿼리 실행 완료")
            else:
                self.finished.emit(False, f"⚠️ {success_count}개 성공, {error_count}개 실패")

        except Exception as e:
            self.finished.emit(False, f"❌ 오류: {str(e)}")


# =====================================================================
# 히스토리 다이얼로그
# =====================================================================
class HistoryDialog(QDialog):
    """쿼리 히스토리 다이얼로그"""
    query_selected = pyqtSignal(str)

    def __init__(self, parent, history_manager):
        super().__init__(parent)
        self.history_manager = history_manager
        self.setWindowTitle("쿼리 히스토리")
        self.setMinimumSize(700, 500)
        self.init_ui()
        self.load_history()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 히스토리 리스트
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget {
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #ddd;
            }
            QListWidget::item:selected {
                background-color: #3498db;
                color: white;
            }
        """)
        self.list_widget.itemDoubleClicked.connect(self.select_query)
        layout.addWidget(self.list_widget)

        # 미리보기
        preview_group = QGroupBox("쿼리 미리보기")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(150)
        self.preview_text.setStyleSheet("""
            QTextEdit {
                background-color: #2c3e50;
                color: #ecf0f1;
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
        """)
        preview_layout.addWidget(self.preview_text)
        layout.addWidget(preview_group)

        # 선택 시 미리보기 업데이트
        self.list_widget.currentRowChanged.connect(self.update_preview)

        # 버튼
        btn_layout = QHBoxLayout()

        btn_clear = QPushButton("🗑️ 히스토리 삭제")
        btn_clear.clicked.connect(self.clear_history)

        btn_use = QPushButton("📋 에디터에 붙여넣기")
        btn_use.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 8px 16px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_use.clicked.connect(self.select_current)

        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.reject)

        btn_layout.addWidget(btn_clear)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_use)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def load_history(self):
        """히스토리 로드"""
        self.list_widget.clear()
        history = self.history_manager.get_history(limit=100)

        for item in history:
            # 표시 텍스트
            timestamp = item.get('timestamp', '')[:19]  # YYYY-MM-DD HH:MM:SS
            success = "✅" if item.get('success', False) else "❌"
            query_preview = item.get('query', '')[:80].replace('\n', ' ')
            if len(item.get('query', '')) > 80:
                query_preview += "..."

            display = f"{timestamp}  {success}  {query_preview}"

            list_item = QListWidgetItem(display)
            list_item.setData(Qt.ItemDataRole.UserRole, item.get('query', ''))
            self.list_widget.addItem(list_item)

    def update_preview(self, row):
        """미리보기 업데이트"""
        if row >= 0:
            item = self.list_widget.item(row)
            query = item.data(Qt.ItemDataRole.UserRole)
            self.preview_text.setPlainText(query)
        else:
            self.preview_text.clear()

    def select_query(self, item):
        """쿼리 선택 (더블클릭)"""
        query = item.data(Qt.ItemDataRole.UserRole)
        self.query_selected.emit(query)
        self.accept()

    def select_current(self):
        """현재 선택된 쿼리 사용"""
        item = self.list_widget.currentItem()
        if item:
            query = item.data(Qt.ItemDataRole.UserRole)
            self.query_selected.emit(query)
            self.accept()

    def clear_history(self):
        """히스토리 삭제"""
        reply = QMessageBox.question(
            self, "확인", "모든 히스토리를 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.history_manager.clear_history()
            self.load_history()


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
        self.temp_server = None
        self.current_file = None
        self.is_modified = False

        # 히스토리 매니저
        from src.core.sql_history import SQLHistory
        self.history_manager = SQLHistory()

        self.setWindowTitle(f"SQL 에디터 - {self.config.get('name', 'Unknown')}")
        self.setMinimumSize(1000, 700)
        self.init_ui()
        self.setup_shortcuts()
        self.refresh_databases()

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
        conn_bar.addWidget(QLabel("📂 DB:"))

        self.db_combo = QComboBox()
        self.db_combo.setMinimumWidth(200)
        self.db_combo.setEditable(True)
        self.db_combo.setPlaceholderText("데이터베이스 선택...")
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

        self.btn_execute = QPushButton("▶ 실행")
        self.btn_execute.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #229954; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_execute.setToolTip("쿼리 실행 (F5 또는 Ctrl+Enter)")
        self.btn_execute.clicked.connect(self.execute_query)
        toolbar.addWidget(self.btn_execute)

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

        # --- 메인 스플리터 (에디터 + 결과) ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        # 에디터 영역
        editor_group = QGroupBox("SQL 쿼리")
        editor_layout = QVBoxLayout(editor_group)
        editor_layout.setContentsMargins(4, 8, 4, 4)

        self.editor = CodeEditor()
        self.editor.setPlaceholderText("SELECT * FROM table_name;")
        self.editor.textChanged.connect(self._on_text_changed)
        editor_layout.addWidget(self.editor)

        splitter.addWidget(editor_group)

        # 결과 영역
        result_group = QGroupBox("결과")
        result_layout = QVBoxLayout(result_group)
        result_layout.setContentsMargins(4, 8, 4, 4)

        self.result_tabs = QTabWidget()
        self.result_tabs.setTabsClosable(True)
        self.result_tabs.tabCloseRequested.connect(self.close_result_tab)

        # 메시지 탭 (항상 표시)
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
        self.result_tabs.addTab(self.message_text, "📋 메시지")

        result_layout.addWidget(self.result_tabs)
        splitter.addWidget(result_group)

        splitter.setSizes([350, 300])
        layout.addWidget(splitter)

        # --- 상태바 ---
        self.status_bar = QStatusBar()
        self.status_bar.showMessage("준비됨")
        layout.addWidget(self.status_bar)

    def setup_shortcuts(self):
        """단축키 설정"""
        # F5: 실행
        shortcut_f5 = QShortcut(QKeySequence("F5"), self)
        shortcut_f5.activated.connect(self.execute_query)

        # Ctrl+Enter: 실행
        shortcut_ctrl_enter = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut_ctrl_enter.activated.connect(self.execute_query)

        # Ctrl+O: 열기
        shortcut_open = QShortcut(QKeySequence("Ctrl+O"), self)
        shortcut_open.activated.connect(self.open_file)

        # Ctrl+S: 저장
        shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        shortcut_save.activated.connect(self.save_file)

    def _on_text_changed(self):
        """텍스트 변경 시"""
        self.is_modified = True

    def refresh_databases(self):
        """데이터베이스 목록 새로고침"""
        from src.core.db_connector import MySQLConnector

        tid = self.config.get('id')
        db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)

        if not db_user:
            self.message_text.append("⚠️ DB 자격 증명이 설정되지 않았습니다.")
            return

        is_direct = self.config.get('connection_mode') == 'direct'
        temp_server = None

        try:
            self.message_text.append("📋 데이터베이스 목록 조회 중...")
            QApplication.processEvents()

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
                    self.message_text.append(f"❌ 터널 생성 실패: {error}")
                    return
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(temp_server)

            connector = MySQLConnector(host, port, db_user, db_password)
            success, msg = connector.connect()

            if success:
                schemas = connector.get_schemas()
                connector.disconnect()

                self.db_combo.clear()
                self.db_combo.addItem("")  # 빈 항목
                self.db_combo.addItems(schemas)
                self.message_text.append(f"✅ {len(schemas)}개 데이터베이스 발견")
            else:
                self.message_text.append(f"❌ DB 연결 실패: {msg}")

        except Exception as e:
            self.message_text.append(f"❌ 오류: {str(e)}")
        finally:
            if temp_server:
                self.engine.close_temp_tunnel(temp_server)

    def execute_query(self):
        """쿼리 실행"""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "경고", "쿼리가 이미 실행 중입니다.")
            return

        # 선택된 텍스트 또는 전체 텍스트
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            sql_text = cursor.selectedText().replace('\u2029', '\n')  # Qt paragraph separator
        else:
            sql_text = self.editor.toPlainText()

        if not sql_text.strip():
            QMessageBox.warning(self, "경고", "실행할 SQL이 없습니다.")
            return

        # 쿼리 분리
        queries = self._split_queries(sql_text)
        if not queries:
            QMessageBox.warning(self, "경고", "유효한 SQL 쿼리가 없습니다.")
            return

        # LIMIT 자동 적용
        limit_value = self._get_limit_value()
        if limit_value:
            queries = [self._apply_limit(q, limit_value) for q in queries]

        # 연결 정보 획득
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
                self.message_text.append("🔗 임시 터널 생성 중...")
                QApplication.processEvents()
                success, self.temp_server, error = self.engine.create_temp_tunnel(self.config)
                if not success:
                    self.message_text.append(f"❌ 터널 생성 실패: {error}")
                    return
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(self.temp_server)
                self.message_text.append(f"✅ 임시 터널: localhost:{port}")

            database = self.db_combo.currentText().strip() or None

            # 기존 결과 탭 제거 (메시지 탭 제외)
            while self.result_tabs.count() > 1:
                self.result_tabs.removeTab(1)

            # UI 상태
            self.btn_execute.setEnabled(False)
            self.message_text.append(f"\n{'='*50}")
            self.message_text.append(f"🚀 {len(queries)}개 쿼리 실행 시작...")
            self.message_text.append(f"{'='*50}\n")

            # Worker 시작
            self.worker = SQLQueryWorker(host, port, db_user, db_password, database, queries)
            self.worker.progress.connect(self._on_progress)
            self.worker.query_result.connect(self._on_query_result)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()

            # 히스토리에 저장
            self.history_manager.add_query(sql_text, True, 0, 0)

        except Exception as e:
            self.message_text.append(f"❌ 오류: {str(e)}")
            self._cleanup()

    def _split_queries(self, sql_text):
        """SQL 텍스트를 개별 쿼리로 분리 (문자열 내 세미콜론 무시)"""
        queries = []
        current_query = []
        in_string = False
        string_char = None

        for char in sql_text:
            if char in ("'", '"') and not in_string:
                in_string = True
                string_char = char
            elif char == string_char and in_string:
                in_string = False
                string_char = None

            if char == ';' and not in_string:
                query = ''.join(current_query).strip()
                if query:
                    queries.append(query)
                current_query = []
            else:
                current_query.append(char)

        # 마지막 쿼리 (세미콜론 없이 끝난 경우)
        query = ''.join(current_query).strip()
        if query:
            queries.append(query)

        return queries

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
        """SELECT 쿼리에 LIMIT 자동 적용 (이미 LIMIT이 있으면 적용 안함)"""
        query_upper = query.upper().strip()

        # SELECT 쿼리가 아니면 그대로 반환
        if not query_upper.startswith('SELECT'):
            return query

        # 이미 LIMIT이 있으면 그대로 반환
        # LIMIT 키워드 검색 (문자열 내부 제외)
        in_string = False
        string_char = None
        check_text = []

        for char in query:
            if char in ("'", '"') and not in_string:
                in_string = True
                string_char = char
                check_text.append(' ')
            elif char == string_char and in_string:
                in_string = False
                string_char = None
                check_text.append(' ')
            elif in_string:
                check_text.append(' ')
            else:
                check_text.append(char)

        clean_query = ''.join(check_text).upper()
        if ' LIMIT ' in clean_query or clean_query.endswith(' LIMIT'):
            return query

        # LIMIT 추가
        return f"{query} LIMIT {limit_value}"

    def _on_progress(self, msg):
        """진행 메시지"""
        self.message_text.append(msg)
        self.status_bar.showMessage(msg)

    def _on_query_result(self, idx, columns, rows, error, affected, exec_time):
        """쿼리 결과 수신"""
        if error:
            self.message_text.append(f"❌ 쿼리 {idx + 1}: {error}")
        elif columns:
            # SELECT 결과 - 테이블 탭 추가
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

            # 컬럼 너비 자동 조절
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

            # 셀 복사 허용
            table.setStyleSheet("""
                QTableWidget {
                    gridline-color: #ddd;
                    font-size: 12px;
                }
                QTableWidget::item:selected {
                    background-color: #3498db;
                    color: white;
                }
            """)

            tab_name = f"결과 {idx + 1} ({len(rows)}행)"
            self.result_tabs.addTab(table, tab_name)
            self.result_tabs.setCurrentWidget(table)

            self.message_text.append(f"✅ 쿼리 {idx + 1}: {len(rows)}행 반환 ({exec_time:.3f}초)")
        else:
            # INSERT/UPDATE/DELETE
            self.message_text.append(f"✅ 쿼리 {idx + 1}: {affected}행 영향받음 ({exec_time:.3f}초)")

        self.status_bar.showMessage(f"쿼리 {idx + 1} 완료 ({exec_time:.3f}초)")

    def _on_finished(self, success, msg):
        """실행 완료"""
        self.message_text.append(f"\n{msg}")
        self.status_bar.showMessage(msg)
        self._cleanup()

    def _cleanup(self):
        """정리"""
        self.btn_execute.setEnabled(True)

        if self.temp_server:
            self.message_text.append("🛑 임시 터널 종료...")
            self.engine.close_temp_tunnel(self.temp_server)
            self.temp_server = None

    def close_result_tab(self, index):
        """결과 탭 닫기"""
        if index > 0:  # 메시지 탭은 닫지 않음
            self.result_tabs.removeTab(index)

    def open_file(self):
        """SQL 파일 열기"""
        if self.is_modified:
            reply = QMessageBox.question(
                self, "확인", "저장되지 않은 변경사항이 있습니다. 계속하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        file_path, _ = QFileDialog.getOpenFileName(
            self, "SQL 파일 열기", "",
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.editor.setPlainText(content)
                self.current_file = file_path
                self.is_modified = False
                self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {file_path}")
                self.message_text.append(f"📂 파일 열림: {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "오류", f"파일을 열 수 없습니다:\n{str(e)}")

    def save_file(self):
        """SQL 파일 저장"""
        if self.current_file:
            file_path = self.current_file
        else:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "SQL 파일 저장", "",
                "SQL 파일 (*.sql);;모든 파일 (*.*)"
            )

        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.editor.toPlainText())
                self.current_file = file_path
                self.is_modified = False
                self.setWindowTitle(f"SQL 에디터 - {self.config.get('name')} - {file_path}")
                self.message_text.append(f"💾 파일 저장됨: {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "오류", f"파일을 저장할 수 없습니다:\n{str(e)}")

    def show_history(self):
        """히스토리 다이얼로그 표시"""
        dialog = HistoryDialog(self, self.history_manager)
        dialog.query_selected.connect(self._on_history_selected)
        dialog.exec()

    def _on_history_selected(self, query):
        """히스토리에서 쿼리 선택됨"""
        self.editor.setPlainText(query)

    def closeEvent(self, event):
        """다이얼로그 닫기"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "확인", "쿼리가 실행 중입니다. 정말 닫으시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        if self.is_modified:
            reply = QMessageBox.question(
                self, "확인", "저장되지 않은 변경사항이 있습니다. 정말 닫으시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        self._cleanup()
        event.accept()
