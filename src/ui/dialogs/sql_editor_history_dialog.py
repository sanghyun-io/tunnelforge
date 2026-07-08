"""
SQL 쿼리 히스토리 브라우저 다이얼로그
"""
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QTextEdit, QVBoxLayout
)
from PyQt6.QtCore import Qt, pyqtSignal


class HistoryDialog(QDialog):
    """쿼리 히스토리 다이얼로그 (영구 보관, 고급 검색, 즐겨찾기)"""
    query_selected = pyqtSignal(str)

    ITEMS_PER_PAGE = 50  # 한 번에 로드할 항목 수

    def __init__(self, parent, history_manager):
        super().__init__(parent)
        self.history_manager = history_manager
        self.current_offset = 0
        self.total_count = 0
        self._history_items = []  # 현재 표시된 항목 데이터
        self._is_searching = False  # 검색 모드 여부
        self.setWindowTitle("쿼리 히스토리")
        self.setMinimumSize(800, 600)
        self.init_ui()
        self.load_history()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # === 검색 필터 패널 ===
        filter_group = QGroupBox("검색 필터")
        filter_layout = QVBoxLayout(filter_group)

        # 첫 번째 줄: 키워드, 날짜
        row1 = QHBoxLayout()

        row1.addWidget(QLabel("키워드:"))
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("쿼리 내용으로 검색...")
        self.keyword_edit.setMinimumWidth(200)
        self.keyword_edit.returnPressed.connect(self._do_search)
        row1.addWidget(self.keyword_edit)

        row1.addWidget(QLabel("기간:"))

        from PyQt6.QtWidgets import QDateEdit
        from PyQt6.QtCore import QDate
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(QDate.currentDate().addMonths(-1))
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        row1.addWidget(self.date_from)

        row1.addWidget(QLabel("~"))

        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDate(QDate.currentDate())
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        row1.addWidget(self.date_to)

        self.date_filter_check = QCheckBox("날짜 적용")
        self.date_filter_check.setChecked(False)
        row1.addWidget(self.date_filter_check)

        row1.addStretch()
        filter_layout.addLayout(row1)

        # 두 번째 줄: 체크박스들, 버튼
        row2 = QHBoxLayout()

        self.success_check = QCheckBox("성공만")
        row2.addWidget(self.success_check)

        self.failure_check = QCheckBox("실패만")
        row2.addWidget(self.failure_check)

        self.favorites_check = QCheckBox("즐겨찾기만")
        row2.addWidget(self.favorites_check)

        row2.addStretch()

        btn_search = QPushButton("🔍 검색")
        btn_search.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 6px 16px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_search.clicked.connect(self._do_search)
        row2.addWidget(btn_search)

        btn_reset = QPushButton("초기화")
        btn_reset.setStyleSheet("""
            QPushButton {
                background-color: #95a5a6; color: white;
                padding: 6px 12px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #7f8c8d; }
        """)
        btn_reset.clicked.connect(self._reset_search)
        row2.addWidget(btn_reset)

        filter_layout.addLayout(row2)
        layout.addWidget(filter_group)

        # === 정보 바 ===
        info_layout = QHBoxLayout()
        self.info_label = QLabel("히스토리 로딩 중...")
        self.info_label.setStyleSheet("color: #666;")
        info_layout.addWidget(self.info_label)

        self.fav_count_label = QLabel("⭐ 0")
        self.fav_count_label.setStyleSheet("color: #f39c12; font-weight: bold;")
        info_layout.addWidget(self.fav_count_label)

        info_layout.addStretch()
        layout.addLayout(info_layout)

        # === 히스토리 리스트 ===
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
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.list_widget)

        # === 미리보기 ===
        preview_group = QGroupBox("쿼리 미리보기")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(120)
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

        # === 페이지네이션 ===
        page_layout = QHBoxLayout()

        self.btn_load_more = QPushButton("📜 더 보기")
        self.btn_load_more.setStyleSheet("""
            QPushButton {
                background-color: #6c757d; color: white;
                padding: 6px 16px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #5a6268; }
            QPushButton:disabled { background-color: #adb5bd; }
        """)
        self.btn_load_more.clicked.connect(self.load_more)
        self.btn_load_more.setVisible(False)

        page_layout.addStretch()
        page_layout.addWidget(self.btn_load_more)
        page_layout.addStretch()
        layout.addLayout(page_layout)

        # === 하단 버튼 ===
        btn_layout = QHBoxLayout()

        btn_fav_toggle = QPushButton("⭐ 즐겨찾기 토글")
        btn_fav_toggle.setStyleSheet("""
            QPushButton {
                background-color: #f39c12; color: white;
                padding: 8px 16px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #d68910; }
        """)
        btn_fav_toggle.clicked.connect(self._toggle_favorite)
        btn_layout.addWidget(btn_fav_toggle)

        btn_layout.addStretch()

        btn_use = QPushButton("📋 에디터에 붙여넣기")
        btn_use.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 8px 16px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_use.clicked.connect(self.select_current)
        btn_layout.addWidget(btn_use)

        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(btn_close)

        layout.addLayout(btn_layout)

    def load_history(self):
        """히스토리 초기 로드"""
        self.list_widget.clear()
        self._history_items.clear()
        self.current_offset = 0
        self._is_searching = False
        self._load_chunk()
        self._update_fav_count()

    def _load_chunk(self):
        """히스토리 청크 로드"""
        if self._is_searching:
            self._load_search_chunk()
        else:
            history, self.total_count = self.history_manager.get_history(
                limit=self.ITEMS_PER_PAGE,
                offset=self.current_offset
            )

            for item in history:
                self._add_history_item(item)
                self._history_items.append(item)

            self.current_offset += len(history)

        # 정보 라벨 업데이트
        loaded = self.list_widget.count()
        self.info_label.setText(f"📊 {loaded:,} / {self.total_count:,}개 표시")

        # 더 보기 버튼 표시/숨김
        has_more = self.current_offset < self.total_count
        self.btn_load_more.setVisible(has_more)
        if has_more:
            remaining = self.total_count - self.current_offset
            self.btn_load_more.setText(f"📜 더 보기 ({remaining:,}개 남음)")

    def _load_search_chunk(self):
        """검색 결과 청크 로드"""
        from datetime import datetime

        keyword = self.keyword_edit.text().strip() or None

        date_from = None
        date_to = None
        if self.date_filter_check.isChecked():
            date_from = datetime(
                self.date_from.date().year(),
                self.date_from.date().month(),
                self.date_from.date().day()
            )
            date_to = datetime(
                self.date_to.date().year(),
                self.date_to.date().month(),
                self.date_to.date().day()
            )

        success_only = None
        if self.success_check.isChecked():
            success_only = True
        elif self.failure_check.isChecked():
            success_only = False

        favorites_only = self.favorites_check.isChecked()

        results, self.total_count = self.history_manager.search_advanced(
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            success_only=success_only,
            favorites_only=favorites_only,
            limit=self.ITEMS_PER_PAGE,
            offset=self.current_offset
        )

        for item in results:
            self._add_history_item(item)
            self._history_items.append(item)

        self.current_offset += len(results)

    def _add_history_item(self, item):
        """히스토리 항목 추가"""
        # 즐겨찾기 아이콘
        fav_icon = "⭐" if item.get('is_favorite', False) else "☆"

        # 타임스탬프
        timestamp = item.get('timestamp', '')[:16]  # YYYY-MM-DD HH:MM

        # 상태 아이콘
        status = item.get('status', 'completed')
        if status == 'pending':
            status_icon = "⏳"
        elif status == 'committed':
            status_icon = "✅"
        elif status == 'rolled_back':
            status_icon = "↩️"
        elif not item.get('success', False):
            status_icon = "❌"
        else:
            status_icon = "✅"

        # 쿼리 미리보기
        query_preview = item.get('query', '')[:50].replace('\n', ' ')
        if len(item.get('query', '')) > 50:
            query_preview += "..."

        # 영향받은 행 수
        result_count = item.get('result_count', 0)
        count_str = f"({result_count}행)" if result_count > 0 else ""

        display = f"{fav_icon} {timestamp}  {status_icon} {count_str:>8}  {query_preview}"

        list_item = QListWidgetItem(display)
        list_item.setData(Qt.ItemDataRole.UserRole, item.get('query', ''))
        # 항목 ID 저장 (즐겨찾기 토글용)
        list_item.setData(Qt.ItemDataRole.UserRole + 1, item.get('id') or item.get('timestamp'))
        self.list_widget.addItem(list_item)

    def _do_search(self):
        """검색 실행"""
        self.list_widget.clear()
        self._history_items.clear()
        self.current_offset = 0
        self._is_searching = True
        self._load_chunk()

    def _reset_search(self):
        """검색 필터 초기화"""
        self.keyword_edit.clear()
        self.date_filter_check.setChecked(False)
        self.success_check.setChecked(False)
        self.failure_check.setChecked(False)
        self.favorites_check.setChecked(False)
        self.load_history()

    def _toggle_favorite(self):
        """현재 선택 항목 즐겨찾기 토글"""
        row = self.list_widget.currentRow()
        if row < 0:
            return

        item = self.list_widget.item(row)
        history_id = item.data(Qt.ItemDataRole.UserRole + 1)
        if history_id:
            new_state = self.history_manager.toggle_favorite(history_id)

            # 리스트 항목 텍스트 업데이트
            text = item.text()
            if new_state:
                text = "⭐" + text[1:]
            else:
                text = "☆" + text[1:]
            item.setText(text)

            # 즐겨찾기 수 업데이트
            self._update_fav_count()

    def _update_fav_count(self):
        """즐겨찾기 카운트 업데이트"""
        fav_count = self.history_manager.get_favorite_count()
        self.fav_count_label.setText(f"⭐ {fav_count}")

    def _show_context_menu(self, pos):
        """컨텍스트 메뉴 표시"""
        item = self.list_widget.itemAt(pos)
        if not item:
            return

        menu = QMenu(self)

        fav_action = menu.addAction("⭐ 즐겨찾기 토글")
        fav_action.triggered.connect(self._toggle_favorite)

        copy_action = menu.addAction("📋 쿼리 복사")
        copy_action.triggered.connect(lambda: self._copy_query(item))

        use_action = menu.addAction("📝 에디터에 붙여넣기")
        use_action.triggered.connect(self.select_current)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def _copy_query(self, item):
        """쿼리를 클립보드에 복사"""
        query = item.data(Qt.ItemDataRole.UserRole)
        if query:
            QApplication.clipboard().setText(query)

    def load_more(self):
        """더 많은 히스토리 로드"""
        self._load_chunk()

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
