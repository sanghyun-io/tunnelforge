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
import threading
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QGroupBox, QSplitter, QPlainTextEdit, QTextEdit, QWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
    QStatusBar, QApplication, QAbstractItemView, QListWidget, QListWidgetItem, QProgressBar,
    QDialogButtonBox, QMenu, QCheckBox, QFrame, QToolTip, QLineEdit
)
from PyQt6.QtCore import Qt, QRect, QSize, pyqtSignal, QThread, QTimer, QPoint
from PyQt6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont, QPainter,
    QTextCursor, QKeySequence, QShortcut, QPen, QTextFormat
)
import re
from typing import List, Dict, Optional


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
# SQL 검증 하이라이터 (밑줄 표시)
# =====================================================================
class SQLValidatorHighlighter(SQLHighlighter):
    """SQL 구문 하이라이터 + 검증 이슈 밑줄 표시"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._issues = []  # ValidationIssue 목록
        self._issue_formats = {}  # line -> [(col, end_col, format), ...]

        # 에러 포맷 (빨간 물결 밑줄)
        self.error_format = QTextCharFormat()
        self.error_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        self.error_format.setUnderlineColor(QColor("#E74C3C"))  # 빨간색

        # 경고 포맷 (노란 물결 밑줄)
        self.warning_format = QTextCharFormat()
        self.warning_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        self.warning_format.setUnderlineColor(QColor("#F39C12"))  # 노란색

        # 정보 포맷 (파란 밑줄)
        self.info_format = QTextCharFormat()
        self.info_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
        self.info_format.setUnderlineColor(QColor("#3498DB"))  # 파란색

    def set_issues(self, issues: list):
        """검증 이슈 설정 및 재하이라이팅"""
        self._issues = issues
        self._build_issue_map()
        self.rehighlight()

    def _build_issue_map(self):
        """줄별 이슈 맵 생성"""
        self._issue_formats = {}

        for issue in self._issues:
            line = issue.line
            if line not in self._issue_formats:
                self._issue_formats[line] = []

            # 심각도에 따른 포맷 선택
            from src.core.sql_validator import IssueSeverity
            if issue.severity == IssueSeverity.ERROR:
                fmt = self.error_format
            elif issue.severity == IssueSeverity.WARNING:
                fmt = self.warning_format
            else:
                fmt = self.info_format

            self._issue_formats[line].append((issue.column, issue.end_column, fmt))

    def highlightBlock(self, text):
        """블록 하이라이팅 (기본 + 검증 이슈)"""
        # 기본 SQL 하이라이팅
        super().highlightBlock(text)

        # 검증 이슈 밑줄 추가
        block_number = self.currentBlock().blockNumber()
        if block_number in self._issue_formats:
            for col, end_col, fmt in self._issue_formats[block_number]:
                # 범위 검증
                start = max(0, col)
                length = min(end_col, len(text)) - start
                if length > 0:
                    self.setFormat(start, length, fmt)

    def get_issues(self) -> list:
        """현재 이슈 목록 반환"""
        return self._issues


# =====================================================================
# 자동완성 팝업
# =====================================================================
class AutoCompletePopup(QListWidget):
    """SQL 자동완성 팝업 위젯"""

    item_selected = pyqtSignal(str)  # 선택된 텍스트

    # 항목당 높이 (padding 포함)
    ITEM_HEIGHT = 24
    MAX_HEIGHT = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        # Tool 윈도우로 설정하여 포커스를 부모에게 유지
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)  # 포커스 가져가지 않음
        self.setMouseTracking(True)
        self.setMinimumWidth(200)

        self.setStyleSheet("""
            QListWidget {
                background-color: #2D2D2D;
                color: #D4D4D4;
                border: 1px solid #454545;
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
            QListWidget::item {
                padding: 4px 8px;
            }
            QListWidget::item:selected {
                background-color: #094771;
                color: white;
            }
            QListWidget::item:hover {
                background-color: #383838;
            }
        """)

        self.itemClicked.connect(self._on_item_clicked)
        self.itemDoubleClicked.connect(self._on_item_clicked)

    def set_completions(self, completions: List[Dict]):
        """자동완성 목록 설정

        Args:
            completions: [{label, type, detail}, ...]
        """
        self.clear()

        for item_data in completions:
            label = item_data.get('label', '')
            item_type = item_data.get('type', '')
            detail = item_data.get('detail', '')

            # 아이콘 접두사
            icons = {
                'table': '📋',
                'column': '📊',
                'keyword': '🔤',
                'function': '⚡',
            }
            icon = icons.get(item_type, '•')

            display_text = f"{icon} {label}"
            if detail:
                display_text += f"  ({detail})"

            list_item = QListWidgetItem(display_text)
            list_item.setData(Qt.ItemDataRole.UserRole, label)
            self.addItem(list_item)

        if self.count() > 0:
            self.setCurrentRow(0)
            self._adjust_height(self.count())

    def filter_items(self, prefix: str):
        """입력에 따라 항목 필터링"""
        prefix_lower = prefix.lower()
        visible_count = 0

        for i in range(self.count()):
            item = self.item(i)
            label = item.data(Qt.ItemDataRole.UserRole) or ""
            matches = label.lower().startswith(prefix_lower)
            item.setHidden(not matches)
            if matches:
                visible_count += 1

        # 첫 번째 보이는 항목 선택
        for i in range(self.count()):
            if not self.item(i).isHidden():
                self.setCurrentRow(i)
                break

        # 높이 조절
        self._adjust_height(visible_count)

        return visible_count > 0

    def _adjust_height(self, item_count: int):
        """항목 개수에 따라 높이 조절"""
        if item_count <= 0:
            return

        # 항목 개수 * 항목 높이 + 테두리 여백
        calculated_height = item_count * self.ITEM_HEIGHT + 4
        # 최대 높이 제한
        new_height = min(calculated_height, self.MAX_HEIGHT)
        self.setFixedHeight(new_height)

    def _on_item_clicked(self, item):
        """항목 클릭"""
        label = item.data(Qt.ItemDataRole.UserRole)
        if label:
            self.item_selected.emit(label)
            self.hide()

    def select_current(self):
        """현재 선택 항목 확정"""
        item = self.currentItem()
        if item and not item.isHidden():
            label = item.data(Qt.ItemDataRole.UserRole)
            if label:
                self.item_selected.emit(label)
                self.hide()
                return True
        return False

    def move_selection(self, direction: int):
        """선택 이동 (위/아래)"""
        current = self.currentRow()
        new_row = current + direction

        # 보이는 항목만 선택
        while 0 <= new_row < self.count():
            if not self.item(new_row).isHidden():
                self.setCurrentRow(new_row)
                return
            new_row += direction

    def keyPressEvent(self, event):
        """키 이벤트 (팝업 내부) - 부모 에디터로 전달"""
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            self.select_current()
        elif event.key() == Qt.Key.Key_Up:
            self.move_selection(-1)
        elif event.key() == Qt.Key.Key_Down:
            self.move_selection(1)
        else:
            # 다른 키 입력은 부모 에디터로 전달
            if self.parent():
                self.parent().keyPressEvent(event)


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
# 검증 기능이 있는 코드 에디터
# =====================================================================
class ValidatingCodeEditor(CodeEditor):
    """검증 + 자동완성 기능이 있는 코드 에디터"""

    validation_requested = pyqtSignal(str)  # SQL 텍스트
    autocomplete_requested = pyqtSignal(str, int)  # SQL, cursor_pos

    def __init__(self, parent=None):
        super().__init__(parent)

        # 검증 하이라이터로 교체
        self.highlighter = SQLValidatorHighlighter(self.document())

        # 검증 이슈 목록 (툴팁용)
        self._issues: List = []

        # Debounce 타이머 (300ms)
        self._validation_timer = QTimer(self)
        self._validation_timer.setSingleShot(True)
        self._validation_timer.setInterval(300)
        self._validation_timer.timeout.connect(self._trigger_validation)

        # 자동완성 팝업
        self._autocomplete_popup = AutoCompletePopup(self)
        self._autocomplete_popup.item_selected.connect(self._on_autocomplete_selected)
        self._autocomplete_prefix = ""  # 현재 입력 중인 접두사

        # 마우스 이동 추적 (호버 툴팁)
        self.setMouseTracking(True)

        # 텍스트 변경 시 검증 예약
        self.textChanged.connect(self._schedule_validation)

    def _schedule_validation(self):
        """검증 예약 (debounce)"""
        self._validation_timer.stop()
        self._validation_timer.start()

    def _trigger_validation(self):
        """검증 실행 시그널 발생"""
        self.validation_requested.emit(self.toPlainText())

    def set_validation_issues(self, issues: list):
        """검증 이슈 설정"""
        self._issues = issues
        self.highlighter.set_issues(issues)

    def get_issue_at_position(self, pos: int) -> Optional[object]:
        """특정 위치의 이슈 반환"""
        # 위치 → 줄/컬럼 변환
        block = self.document().findBlock(pos)
        line = block.blockNumber()
        col = pos - block.position()

        for issue in self._issues:
            if issue.line == line and issue.column <= col < issue.end_column:
                return issue

        return None

    def mouseMoveEvent(self, event):
        """마우스 이동 시 툴팁 표시"""
        super().mouseMoveEvent(event)

        # 커서 위치에서 문자 위치 계산
        cursor = self.cursorForPosition(event.pos())
        pos = cursor.position()

        issue = self.get_issue_at_position(pos)
        if issue:
            # 툴팁 내용 생성
            tooltip_lines = [issue.message]
            if issue.suggestions:
                tooltip_lines.append(f"💡 제안: {', '.join(issue.suggestions)}")

            QToolTip.showText(event.globalPosition().toPoint(), '\n'.join(tooltip_lines), self)
        else:
            QToolTip.hideText()

    def keyPressEvent(self, event):
        """키 입력 처리 (자동완성 포함)"""
        # 자동완성 팝업이 열려있을 때
        if self._autocomplete_popup.isVisible():
            if event.key() == Qt.Key.Key_Escape:
                self._autocomplete_popup.hide()
                return
            elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                if self._autocomplete_popup.select_current():
                    return
                # 선택 실패 시 (빈 목록 등) 팝업 닫고 기본 동작
                self._autocomplete_popup.hide()
            elif event.key() == Qt.Key.Key_Up:
                self._autocomplete_popup.move_selection(-1)
                return
            elif event.key() == Qt.Key.Key_Down:
                self._autocomplete_popup.move_selection(1)
                return
            # 그 외 키 입력은 에디터에 전달 후 필터링 업데이트

        # Ctrl+Space: 자동완성 표시
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Space:
            self._show_autocomplete()
            return

        # '.' 입력 후 자동완성 (table. 패턴)
        if event.text() == '.':
            super().keyPressEvent(event)
            # 잠시 후 자동완성 표시
            QTimer.singleShot(50, self._show_autocomplete)
            return

        # 일반 키 입력 처리
        super().keyPressEvent(event)

        # 팝업이 열려있으면 필터링 업데이트
        if self._autocomplete_popup.isVisible():
            # 공백이나 특수문자 입력 시 팝업 닫기
            if event.text() and not event.text().isalnum() and event.text() != '_':
                self._autocomplete_popup.hide()
            else:
                self._update_autocomplete_filter()

    def _show_autocomplete(self):
        """자동완성 팝업 표시 요청"""
        cursor = self.textCursor()
        pos = cursor.position()
        self._autocomplete_prefix = self._get_current_word()
        self.autocomplete_requested.emit(self.toPlainText(), pos)

    def show_autocomplete_popup(self, completions: List[Dict]):
        """자동완성 팝업 표시"""
        if not completions:
            self._autocomplete_popup.hide()
            return

        self._autocomplete_popup.set_completions(completions)

        # 필터링
        if self._autocomplete_prefix:
            if not self._autocomplete_popup.filter_items(self._autocomplete_prefix):
                self._autocomplete_popup.hide()
                return

        # 팝업 위치 계산 (커서 아래)
        cursor_rect = self.cursorRect()
        global_pos = self.mapToGlobal(cursor_rect.bottomLeft())
        self._autocomplete_popup.move(global_pos)
        self._autocomplete_popup.show()

        # 에디터에 포커스 유지 (팝업이 포커스 가져가지 않도록)
        self.setFocus()
        self.activateWindow()

    def _update_autocomplete_filter(self):
        """자동완성 필터 업데이트"""
        self._autocomplete_prefix = self._get_current_word()
        if not self._autocomplete_popup.filter_items(self._autocomplete_prefix):
            self._autocomplete_popup.hide()

    def _get_current_word(self) -> str:
        """커서 위치의 현재 단어 추출"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfWord, QTextCursor.MoveMode.KeepAnchor)
        return cursor.selectedText()

    def _on_autocomplete_selected(self, text: str):
        """자동완성 항목 선택됨"""
        cursor = self.textCursor()

        # 현재 단어 선택 후 교체
        cursor.movePosition(QTextCursor.MoveOperation.StartOfWord, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)

        self.setTextCursor(cursor)

        # 에디터로 포커스 복원
        self.setFocus()
        self.activateWindow()

    def focusOutEvent(self, event):
        """포커스 잃을 때 자동완성 숨김"""
        # 자동완성 팝업으로 포커스 이동 시 숨기지 않음
        if self._autocomplete_popup.isVisible():
            # 약간의 딜레이 후 숨김 (팝업 클릭 허용)
            QTimer.singleShot(100, self._maybe_hide_autocomplete)
        super().focusOutEvent(event)

    def _maybe_hide_autocomplete(self):
        """자동완성 팝업 숨김 (조건부)"""
        if not self.hasFocus() and not self._autocomplete_popup.underMouse():
            self._autocomplete_popup.hide()


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

        connector = None
        try:
            connector = MySQLConnector(self.host, self.port, self.user, self.password, self.database)
            success, msg = connector.connect()

            if not success:
                self.finished.emit(False, f"연결 실패: {msg}")
                return

            self.progress.emit(f"✅ 연결 성공: {self.host}:{self.port}")
            connector.connection.autocommit(True)

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

            if error_count == 0:
                self.finished.emit(True, f"✅ {success_count}개 쿼리 실행 완료")
            else:
                self.finished.emit(False, f"⚠️ {success_count}개 성공, {error_count}개 실패")

        except Exception as e:
            self.finished.emit(False, f"❌ 오류: {str(e)}")

        finally:
            # 연결 정리
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass


# =====================================================================
# 트랜잭션 대기 워커 (수정 쿼리용 - 커밋 전 확인)
# =====================================================================
class SQLTransactionWorker(QThread):
    """수정 쿼리 실행 워커 (트랜잭션 분리 - 커밋 전 확인 대기)"""
    progress = pyqtSignal(str)
    preview_result = pyqtSignal(int, str, int, float, str)  # idx, query_type, affected, time, preview_info
    select_result = pyqtSignal(int, list, list, str, int, float)  # idx, columns, rows, error, affected, time
    error_result = pyqtSignal(int, str, float)  # idx, error, time
    ready_for_confirm = pyqtSignal()  # 커밋 대기 상태
    finished = pyqtSignal(bool, str)

    def __init__(self, host, port, user, password, database, queries):
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.queries = queries
        self.connector = None
        self.pending_commit = False
        self.should_commit = None  # None: 대기 중, True: 커밋, False: 롤백

    def run(self):
        from src.core.db_connector import MySQLConnector
        import pymysql

        try:
            # autocommit=False로 연결
            self.connector = MySQLConnector(self.host, self.port, self.user, self.password, self.database)
            success, msg = self.connector.connect()

            if not success:
                self.finished.emit(False, f"연결 실패: {msg}")
                return

            # autocommit 비활성화
            self.connector.connection.autocommit(False)
            self.progress.emit(f"✅ 연결 성공 (트랜잭션 모드): {self.host}:{self.port}")

            total_queries = len(self.queries)
            has_modification = False
            modification_queries = []

            for idx, query in enumerate(self.queries):
                query = query.strip()
                if not query:
                    continue

                query_type = self._get_query_type(query)
                self.progress.emit(f"📄 쿼리 {idx + 1}/{total_queries} 실행 중... ({query_type})")

                start_time = time.time()
                try:
                    with self.connector.connection.cursor() as cursor:
                        cursor.execute(query)

                        if cursor.description:
                            # SELECT 결과
                            columns = [desc[0] for desc in cursor.description]
                            rows = cursor.fetchall()
                            row_list = []
                            for row in rows:
                                if isinstance(row, dict):
                                    row_list.append([row.get(col) for col in columns])
                                else:
                                    row_list.append(list(row))

                            execution_time = time.time() - start_time
                            self.select_result.emit(idx, columns, row_list, "", len(row_list), execution_time)
                        else:
                            # 수정 쿼리 (INSERT, UPDATE, DELETE 등)
                            affected = cursor.rowcount
                            execution_time = time.time() - start_time
                            has_modification = True
                            modification_queries.append((idx, query_type, affected, execution_time, query[:100]))

                            # 미리보기 정보 전송
                            preview_info = query[:100] + ("..." if len(query) > 100 else "")
                            self.preview_result.emit(idx, query_type, affected, execution_time, preview_info)

                except pymysql.Error as e:
                    execution_time = time.time() - start_time
                    error_msg = f"MySQL 오류 ({e.args[0]}): {e.args[1] if len(e.args) > 1 else str(e)}"
                    self.error_result.emit(idx, error_msg, execution_time)
                    # 오류 발생 시 롤백
                    self.connector.connection.rollback()
                    self.connector.disconnect()
                    self.finished.emit(False, "❌ 오류 발생, 트랜잭션 롤백됨")
                    return

                except Exception as e:
                    execution_time = time.time() - start_time
                    self.error_result.emit(idx, str(e), execution_time)
                    self.connector.connection.rollback()
                    self.connector.disconnect()
                    self.finished.emit(False, "❌ 오류 발생, 트랜잭션 롤백됨")
                    return

            # 수정 쿼리가 있으면 커밋 확인 대기
            if has_modification:
                self.pending_commit = True
                self.progress.emit("⏳ 커밋 확인 대기 중...")
                self.ready_for_confirm.emit()

                # 커밋/롤백 결정 대기
                while self.should_commit is None:
                    self.msleep(100)

                if self.should_commit:
                    self.connector.connection.commit()
                    self.progress.emit("✅ 트랜잭션 커밋 완료")
                    self.finished.emit(True, f"✅ {len(modification_queries)}개 수정 쿼리 커밋됨")
                else:
                    self.connector.connection.rollback()
                    self.progress.emit("⚠️ 트랜잭션 롤백됨")
                    self.finished.emit(False, "⚠️ 사용자에 의해 롤백됨")
            else:
                self.finished.emit(True, "✅ SELECT 쿼리 실행 완료")

            self.connector.disconnect()

        except Exception as e:
            if self.connector:
                try:
                    self.connector.connection.rollback()
                    self.connector.disconnect()
                except Exception:
                    pass
            self.finished.emit(False, f"❌ 오류: {str(e)}")

    def _get_query_type(self, query):
        """쿼리 타입 반환"""
        query_upper = query.upper().strip()
        if query_upper.startswith('SELECT'):
            return 'SELECT'
        elif query_upper.startswith('INSERT'):
            return 'INSERT'
        elif query_upper.startswith('UPDATE'):
            return 'UPDATE'
        elif query_upper.startswith('DELETE'):
            return 'DELETE'
        elif query_upper.startswith('TRUNCATE'):
            return 'TRUNCATE'
        elif query_upper.startswith('DROP'):
            return 'DROP'
        elif query_upper.startswith('ALTER'):
            return 'ALTER'
        elif query_upper.startswith('CREATE'):
            return 'CREATE'
        else:
            return 'OTHER'

    def do_commit(self):
        """커밋 실행"""
        self.should_commit = True

    def do_rollback(self):
        """롤백 실행"""
        self.should_commit = False


# =====================================================================
# 히스토리 다이얼로그
# =====================================================================
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


# =====================================================================
# SQL 에디터 탭 (개별 탭 위젯)
# =====================================================================
class SQLEditorTab(QWidget):
    """단일 SQL 에디터 탭"""

    modified_changed = pyqtSignal(bool)  # 수정 상태 변경
    title_changed = pyqtSignal(str)  # 탭 제목 변경 요청

    def __init__(self, parent=None, tab_index: int = 1):
        super().__init__(parent)
        self.file_path = None
        self.is_modified = False
        self._tab_index = tab_index

        self._init_ui()

    def _init_ui(self):
        """UI 초기화"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 에디터
        self.editor = ValidatingCodeEditor()
        self.editor.setPlaceholderText("SELECT * FROM table_name;\n-- Ctrl+Space: 자동완성")
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor)

        # 검증 상태 라벨
        self.validation_label = QLabel("")
        self.validation_label.setStyleSheet("color: #666; font-size: 11px; padding: 2px 4px;")
        layout.addWidget(self.validation_label)

    def _on_text_changed(self):
        """텍스트 변경 시"""
        if not self.is_modified:
            self.is_modified = True
            self.modified_changed.emit(True)
            self.title_changed.emit(self.get_title())

    def get_title(self) -> str:
        """탭 제목 반환"""
        if self.file_path:
            name = os.path.basename(self.file_path)
        else:
            name = f"Query {self._tab_index}"
        return f"{name} *" if self.is_modified else name

    def set_tab_index(self, index: int):
        """탭 인덱스 설정"""
        self._tab_index = index
        self.title_changed.emit(self.get_title())

    def set_content(self, text: str):
        """내용 설정 (수정 플래그 초기화)"""
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self.is_modified = False
        self.title_changed.emit(self.get_title())

    def get_content(self) -> str:
        """내용 반환"""
        return self.editor.toPlainText()

    def load_file(self, file_path: str) -> bool:
        """파일 불러오기"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.editor.blockSignals(True)
            self.editor.setPlainText(content)
            self.editor.blockSignals(False)
            self.file_path = file_path
            self.is_modified = False
            self.title_changed.emit(self.get_title())
            return True
        except Exception:
            return False

    def save_file(self, file_path: str = None) -> tuple:
        """파일 저장

        Returns:
            (success, file_path, error_message)
        """
        target_path = file_path or self.file_path

        if not target_path:
            return False, None, "파일 경로가 지정되지 않았습니다."

        try:
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(self.editor.toPlainText())
            self.file_path = target_path
            self.is_modified = False
            self.modified_changed.emit(False)
            self.title_changed.emit(self.get_title())
            return True, target_path, None
        except Exception as e:
            return False, None, str(e)

    def mark_saved(self):
        """저장 완료 표시"""
        self.is_modified = False
        self.modified_changed.emit(False)
        self.title_changed.emit(self.get_title())


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
        self._tab_counter = 0  # 탭 번호 카운터

        # 지속 연결 (트랜잭션 세션)
        self.db_connection = None
        self.pending_queries = []  # 미커밋 쿼리 목록: [(query, type, affected, timestamp, history_id), ...]

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

        # --- 메인 스플리터 (에디터 + 결과) ---
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
        # 메시지 탭은 닫기 버튼 숨김
        self.result_tabs.tabBar().setTabButton(0, self.result_tabs.tabBar().ButtonPosition.RightSide, None)

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
        layout.addWidget(splitter)

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
        """단축키 설정"""
        # F5: 전체 실행
        shortcut_f5 = QShortcut(QKeySequence("F5"), self)
        shortcut_f5.activated.connect(self.execute_all_queries)

        # Ctrl+Enter: 현재 쿼리 실행
        shortcut_ctrl_enter = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut_ctrl_enter.activated.connect(self.execute_current_query)

        # Ctrl+Shift+Enter: 전체 실행
        shortcut_ctrl_shift_enter = QShortcut(QKeySequence("Ctrl+Shift+Return"), self)
        shortcut_ctrl_shift_enter.activated.connect(self.execute_all_queries)

        # Ctrl+O: 열기
        shortcut_open = QShortcut(QKeySequence("Ctrl+O"), self)
        shortcut_open.activated.connect(self.open_file)

        # Ctrl+S: 저장
        shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        shortcut_save.activated.connect(self.save_file)

        # Ctrl+Shift+S: 다른 이름으로 저장
        shortcut_save_as = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        shortcut_save_as.activated.connect(self.save_file_as)

        # Ctrl+N: 새 탭
        shortcut_new_tab = QShortcut(QKeySequence("Ctrl+N"), self)
        shortcut_new_tab.activated.connect(self._add_new_tab)

        # Ctrl+W: 현재 탭 닫기
        shortcut_close_tab = QShortcut(QKeySequence("Ctrl+W"), self)
        shortcut_close_tab.activated.connect(self._close_current_tab)

        # Ctrl+Tab: 다음 탭
        shortcut_next_tab = QShortcut(QKeySequence("Ctrl+Tab"), self)
        shortcut_next_tab.activated.connect(self._next_tab)

        # Ctrl+Shift+Tab: 이전 탭
        shortcut_prev_tab = QShortcut(QKeySequence("Ctrl+Shift+Tab"), self)
        shortcut_prev_tab.activated.connect(self._prev_tab)

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
        """지속 연결 확보 (없으면 생성)"""
        if self.db_connection and self.db_connection.open:
            return True, None

        tid = self.config.get('id')
        db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)

        if not db_user:
            return False, "DB 자격 증명이 설정되지 않았습니다."

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
                    return False, f"터널 생성 실패: {error}"
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(self.temp_server)
                self.message_text.append(f"✅ 임시 터널: localhost:{port}")

            database = self.db_combo.currentText().strip() or None

            # PyMySQL 직접 연결 (autocommit=False)
            import pymysql
            self.db_connection = pymysql.connect(
                host=host,
                port=port,
                user=db_user,
                password=db_password,
                database=database,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False  # 수동 트랜잭션 관리
            )
            # READ COMMITTED: 각 SELECT가 최신 커밋 데이터를 조회 (외부 변경 즉시 반영)
            self.db_connection.cursor().execute(
                "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
            )
            self.message_text.append(f"✅ DB 연결 성공 (트랜잭션 모드): {host}:{port}")
            self._update_tx_status()
            return True, None

        except Exception as e:
            return False, str(e)

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
        """커서 위치의 쿼리 반환 (세미콜론 기준)"""
        full_text = self.editor.toPlainText()
        cursor_pos = self.editor.textCursor().position()

        if not full_text.strip():
            return ""

        # 세미콜론으로 쿼리 경계 찾기 (문자열 내 세미콜론 무시)
        query_ranges = []
        current_start = 0
        in_string = False
        string_char = None

        for i, char in enumerate(full_text):
            if char in ("'", '"') and not in_string:
                in_string = True
                string_char = char
            elif char == string_char and in_string:
                in_string = False
                string_char = None
            elif char == ';' and not in_string:
                query_ranges.append((current_start, i + 1))
                current_start = i + 1

        # 마지막 쿼리 (세미콜론 없이 끝난 경우)
        if current_start < len(full_text):
            query_ranges.append((current_start, len(full_text)))

        # 커서 위치가 포함된 쿼리 찾기
        for start, end in query_ranges:
            if start <= cursor_pos <= end:
                query = full_text[start:end].strip()
                # 끝의 세미콜론 제거 (나중에 다시 붙음)
                if query.endswith(';'):
                    query = query[:-1].strip()
                return query

        # 못 찾으면 마지막 쿼리
        if query_ranges:
            start, end = query_ranges[-1]
            query = full_text[start:end].strip()
            if query.endswith(';'):
                query = query[:-1].strip()
            return query

        return full_text.strip()

    def _execute_sql(self, sql_text, single_query=False):
        """SQL 실행 (내부 메서드)"""
        if self.worker and self.worker.isRunning():
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

        # 단일 쿼리 실행 시 결과 탭 유지, 전체 실행 시 초기화
        if not single_query:
            while self.result_tabs.count() > 1:
                self.result_tabs.removeTab(1)

        self._set_executing_state(True)

        query_label = "현재 쿼리" if single_query else f"{len(queries)}개 쿼리"
        self.message_text.append(f"\n{'─'*40}")
        self.message_text.append(f"🚀 {query_label} 실행")
        self.message_text.append(f"{'─'*40}")

        # 쿼리 실행
        import pymysql
        from datetime import datetime

        total = len(queries)
        if total > 1:
            self.progress_bar.setMaximum(total)

        for idx, query in enumerate(queries):
            query = query.strip()
            if not query:
                continue

            # 실행할 쿼리 미리보기 (짧게)
            preview = query[:60] + "..." if len(query) > 60 else query
            preview = preview.replace('\n', ' ')

            # 진행률 업데이트
            if total > 1:
                self.progress_bar.setValue(idx)
                self._exec_query_progress = f"{idx + 1}/{total}"

            start_time = time.time()
            query_type = self._get_query_type(query)

            try:
                with self.db_connection.cursor() as db_cursor:
                    self._execute_query_in_thread(db_cursor, query)

                    if db_cursor.description:
                        # SELECT 결과
                        columns = [desc[0] for desc in db_cursor.description]
                        rows = db_cursor.fetchall()
                        row_list = [[row.get(col) for col in columns] for row in rows]
                        exec_time = time.time() - start_time

                        tab_idx = self.result_tabs.count()
                        self._add_result_table(tab_idx, columns, row_list, exec_time, query)
                        self.message_text.append(f"✅ {len(rows)}행 반환 ({exec_time:.3f}초)")
                        self.message_text.append(f"   └ {preview}")

                        # 히스토리 저장 (SELECT - 즉시 완료)
                        self.history_manager.add_query(query, True, len(rows), exec_time)
                    else:
                        # 수정 쿼리
                        affected = db_cursor.rowcount
                        exec_time = time.time() - start_time

                        # 히스토리 저장 (pending 상태)
                        history_id = self.history_manager.add_query(
                            query, True, affected, exec_time, status='pending'
                        )

                        # 미커밋 목록에 추가
                        timestamp = datetime.now().strftime("%H:%M:%S")
                        self.pending_queries.append({
                            'query': query,
                            'type': query_type,
                            'affected': affected,
                            'timestamp': timestamp,
                            'history_id': history_id
                        })

                        self.message_text.append(f"📝 [{query_type}] {affected}행 영향 ({exec_time:.3f}초) - 미커밋")
                        self.message_text.append(f"   └ {preview}")

            except pymysql.Error as e:
                exec_time = time.time() - start_time
                error_msg = f"MySQL 오류 ({e.args[0]}): {e.args[1] if len(e.args) > 1 else str(e)}"
                self.message_text.append(f"❌ {error_msg}")
                self.message_text.append(f"   └ {preview}")

                # 히스토리 저장 (실패)
                self.history_manager.add_query(query, False, 0, exec_time, error=error_msg)

            except Exception as e:
                exec_time = time.time() - start_time
                self.message_text.append(f"❌ {str(e)}")
                self.history_manager.add_query(query, False, 0, exec_time, error=str(e))

            # 쿼리 완료 후 진행률 갱신
            if total > 1:
                self.progress_bar.setValue(idx + 1)
                self._exec_query_progress = f"{idx + 1}/{total}"

        # 상태 업데이트
        total_elapsed = time.time() - self._exec_start_time if self._exec_start_time else 0
        self._set_executing_state(False)
        self._update_tx_status()
        pending_count = len(self.pending_queries)
        self.status_bar.showMessage(f"✅ 실행 완료 ({total_elapsed:.1f}초, 미커밋 변경: {pending_count}건)")

    def _execute_with_autocommit(self, queries, sql_text):
        """자동 커밋 모드로 실행 (기존 워커 사용)"""
        tid = self.config.get('id')
        db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)

        if not db_user:
            QMessageBox.warning(self, "경고", "DB 자격 증명이 설정되지 않았습니다.")
            return

        is_direct = self.config.get('connection_mode') == 'direct'

        try:
            if is_direct:
                host = self.config['remote_host']
                port = int(self.config['remote_port'])
            elif self.engine.is_running(tid):
                host, port = self.engine.get_connection_info(tid)
            else:
                self.message_text.append("🔗 임시 터널 생성 중...")
                QApplication.processEvents()
                success, self.temp_server, error = self.engine.create_temp_tunnel(self.config)
                if not success:
                    self.message_text.append(f"❌ 터널 생성 실패: {error}")
                    return
                host = '127.0.0.1'
                port = self.engine.get_temp_tunnel_port(self.temp_server)

            database = self.db_combo.currentText().strip() or None

            while self.result_tabs.count() > 1:
                self.result_tabs.removeTab(1)

            self._set_executing_state(True)
            self.progress_bar.setMaximum(len(queries))
            self.message_text.append(f"\n{'='*50}")
            self.message_text.append(f"🚀 {len(queries)}개 쿼리 실행 (자동 커밋)")
            self.message_text.append(f"{'='*50}\n")

            self.worker = SQLQueryWorker(host, port, db_user, db_password, database, queries)
            self.worker.progress.connect(self._on_progress)
            self.worker.query_result.connect(self._on_query_result)
            self.worker.finished.connect(self._on_finished)
            self.worker.start()

            self.history_manager.add_query(sql_text, True, 0, 0)

        except Exception as e:
            self.message_text.append(f"❌ 오류: {str(e)}")
            self._cleanup()

    def _get_query_type(self, query):
        """쿼리 타입 반환"""
        query_upper = query.upper().strip()
        for kw in ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'DROP', 'ALTER', 'CREATE']:
            if query_upper.startswith(kw):
                return kw
        return 'OTHER'

    def _add_result_table(self, idx, columns, rows, exec_time, query=''):
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

        tab_name = f"결과 {idx + 1} ({len(rows)}행)"
        self.result_tabs.addTab(table, tab_name)
        self.result_tabs.setCurrentWidget(table)

        # 편집 가능성 분석 + 설정
        self._setup_result_table_editability(table, query, columns, rows)

    def _update_tx_status(self):
        """트랜잭션 상태 UI 업데이트"""
        pending_count = len(self.pending_queries)

        if pending_count > 0:
            self.tx_status_frame.setStyleSheet("""
                QFrame {
                    background-color: #FFF3CD;
                    border: 2px solid #FFC107;
                    border-radius: 4px;
                }
            """)
            self.tx_status_icon.setText("⚠️")
            self.tx_info_label.setText(f"미커밋 변경: {pending_count}건")
            self.tx_info_label.setStyleSheet("color: #856404; font-weight: bold; background: transparent; border: none;")
            self.btn_commit.setEnabled(True)
            self.btn_rollback.setEnabled(True)
            self.btn_toggle_pending.setVisible(True)

            # 미커밋 쿼리 목록 업데이트
            self.pending_list_widget.clear()
            for pq in self.pending_queries:
                preview = pq['query'][:50] + "..." if len(pq['query']) > 50 else pq['query']
                preview = preview.replace('\n', ' ')
                item_text = f"[{pq['timestamp']}] {pq['type']} ({pq['affected']}행) - {preview}"
                self.pending_list_widget.addItem(item_text)
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
            self.btn_commit.setEnabled(False)
            self.btn_rollback.setEnabled(False)
            self.btn_toggle_pending.setVisible(False)
            self.pending_list_widget.setVisible(False)
            self.pending_list_widget.clear()

    def _toggle_pending_list(self):
        """미커밋 쿼리 목록 펼치기/접기"""
        is_visible = self.pending_list_widget.isVisible()
        self.pending_list_widget.setVisible(not is_visible)
        self.btn_toggle_pending.setText("▲ 접기" if not is_visible else "▼ 상세")

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

            # 컬럼 너비 설정
            header = table.horizontalHeader()
            header.setSectionsMovable(True)  # 컬럼 드래그 이동 활성화
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # 수동 조절 가능
            header.setStretchLastSection(False)
            table.resizeColumnsToContents()  # 초기 너비는 내용에 맞게
            # 초기 렌더링 시 400px 초과 컬럼 제한 (이후 자유 조정 가능)
            for col in range(len(columns)):
                if header.sectionSize(col) > 400:
                    header.resizeSection(col, 400)

            table.setAlternatingRowColors(True)
            # 셀 단위 드래그 선택 (기존: SelectRows)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
            table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

            # 행 높이 확보 (편집 시 텍스트 잘림 방지)
            table.verticalHeader().setDefaultSectionSize(28)

            # 컨텍스트 메뉴 설정 (우클릭 복사)
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(
                lambda pos, t=table, c=columns: self._show_table_context_menu(pos, t, c)
            )

            # Ctrl+C: 우클릭 복사와 동일하게 선택한 모든 셀을 탭 구분으로 복사
            copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
            copy_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            copy_shortcut.activated.connect(
                lambda t=table, c=columns: self._copy_table_data(t, c, False)
            )

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

            # 편집 가능성 분석 + 설정 (워커에 실행된 원본 쿼리 사용)
            worker_query = ''
            if self.worker is not None and hasattr(self.worker, 'queries'):
                try:
                    worker_query = self.worker.queries[idx]
                except (IndexError, TypeError):
                    worker_query = ''
            self._setup_result_table_editability(table, worker_query, columns, rows)

            self.message_text.append(f"✅ 쿼리 {idx + 1}: {len(rows)}행 반환 ({exec_time:.3f}초)")
        else:
            # INSERT/UPDATE/DELETE
            self.message_text.append(f"✅ 쿼리 {idx + 1}: {affected}행 영향받음 ({exec_time:.3f}초)")

        self.progress_bar.setValue(idx + 1)
        self.status_bar.showMessage(f"쿼리 {idx + 1} 완료 ({exec_time:.3f}초)")

    def _on_finished(self, success, msg):
        """실행 완료"""
        total_elapsed = time.time() - self._exec_start_time if self._exec_start_time else 0
        self.message_text.append(f"\n{msg}")
        self._cleanup()
        self.status_bar.showMessage(f"✅ {msg} ({total_elapsed:.1f}초)")

    def _is_modification_query(self, query):
        """수정 쿼리인지 확인 (SELECT가 아닌 쿼리)"""
        query_upper = query.upper().strip()
        # 주석 제거
        while query_upper.startswith('--') or query_upper.startswith('#'):
            newline_idx = query_upper.find('\n')
            if newline_idx == -1:
                return False
            query_upper = query_upper[newline_idx + 1:].strip()

        modification_keywords = ['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'DROP', 'ALTER', 'CREATE', 'REPLACE']
        return any(query_upper.startswith(kw) for kw in modification_keywords)

    def _do_commit(self):
        """트랜잭션 커밋"""
        if not self.db_connection or not self.pending_queries:
            return

        pending_count = len(self.pending_queries)

        try:
            self.db_connection.commit()

            # 히스토리 상태 업데이트
            history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
            if history_ids:
                self.history_manager.update_status_batch(history_ids, 'committed')

            self.message_text.append(f"\n✅ 커밋 완료! ({pending_count}건 변경사항 적용됨)")
            self.status_bar.showMessage("커밋 완료")
            self.pending_queries.clear()
            self._update_tx_status()
        except Exception as e:
            self.message_text.append(f"❌ 커밋 실패: {str(e)}")
            QMessageBox.critical(self, "커밋 오류", f"커밋에 실패했습니다:\n{str(e)}")

    def _do_rollback(self):
        """트랜잭션 롤백"""
        if not self.db_connection or not self.pending_queries:
            return

        pending_count = len(self.pending_queries)

        reply = QMessageBox.question(
            self, "롤백 확인",
            f"정말 롤백하시겠습니까?\n{pending_count}건의 변경사항이 취소됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.db_connection.rollback()

            # 히스토리 상태 업데이트
            history_ids = [pq['history_id'] for pq in self.pending_queries if pq.get('history_id')]
            if history_ids:
                self.history_manager.update_status_batch(history_ids, 'rolled_back')

            self.message_text.append(f"\n↩️ 롤백 완료! ({pending_count}건 변경사항 취소됨)")
            self.status_bar.showMessage("롤백 완료")
            self.pending_queries.clear()
            self._update_tx_status()
        except Exception as e:
            self.message_text.append(f"❌ 롤백 실패: {str(e)}")

    def _close_db_connection(self):
        """DB 연결 종료 (미커밋 시 롤백)"""
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
                self.db_connection.close()
            except Exception:
                pass
            self.db_connection = None
            self.pending_queries.clear()

    def _set_executing_state(self, is_executing: bool):
        """쿼리 실행 상태 UI 전환"""
        self.btn_execute_current.setEnabled(not is_executing)
        self.btn_execute_all.setEnabled(not is_executing)
        self.progress_bar.setVisible(is_executing)

        if is_executing:
            self.progress_bar.setValue(0)
            self.progress_bar.setMaximum(0)  # indeterminate 모드 (단일 쿼리)
            self._exec_start_time = time.time()
            self._exec_query_progress = None  # 다중 쿼리 진행률 (예: "2/5")
            self._exec_timer = QTimer()
            self._exec_timer.timeout.connect(self._update_elapsed_time)
            self._exec_timer.start(100)
            self.status_bar.showMessage("⏳ 쿼리 실행 중...")
        else:
            self.progress_bar.setMaximum(100)  # determinate 복귀
            self._exec_query_progress = None
            if hasattr(self, '_exec_timer') and self._exec_timer:
                self._exec_timer.stop()
                self._exec_timer = None
            self._exec_start_time = None

    def _execute_query_in_thread(self, db_cursor, query):
        """쿼리를 백그라운드 스레드에서 실행 (메인 스레드 UI 블록 방지)"""
        result = {'done': False, 'error': None}

        def run():
            try:
                db_cursor.execute(query)
            except Exception as e:
                result['error'] = e
            finally:
                result['done'] = True

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while not result['done']:
            QApplication.processEvents()
            thread.join(timeout=0.05)

        if result['error']:
            raise result['error']

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
        """정리"""
        self._set_executing_state(False)

        if self.temp_server:
            self.message_text.append("🛑 임시 터널 종료...")
            self.engine.close_temp_tunnel(self.temp_server)
            self.temp_server = None

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
                apply_action = menu.addAction(f"💾 변경사항 적용 ({pending}건)")
                apply_action.triggered.connect(lambda: self._apply_pending_edits(table))
                discard_action = menu.addAction(f"↩️ 변경사항 취소 ({pending}건)")
                discard_action.triggered.connect(lambda: self._discard_pending_edits(table))
            else:
                info = menu.addAction(
                    f"✏️ 편집 가능 — `{ctx['table']}` (셀 더블클릭)"
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
        """INFORMATION_SCHEMA에서 테이블의 PK 컬럼명 조회"""
        if not self.db_connection or not self.db_connection.open:
            return []
        try:
            with self.db_connection.cursor() as cursor:
                if schema:
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

    def _apply_pending_edits(self, table):
        """변경사항을 UPDATE 쿼리로 DB에 반영 (트랜잭션)"""
        ctx = getattr(table, '_edit_context', None)
        if ctx is None or not ctx['pending_edits']:
            return

        if not self.db_connection or not self.db_connection.open:
            QMessageBox.warning(self, '경고', 'DB 연결이 끊어졌습니다.')
            return

        if self.pending_queries:
            QMessageBox.warning(
                self, '경고',
                '미커밋 쿼리가 있습니다. 먼저 커밋 또는 롤백 후 다시 시도하세요.'
            )
            return

        # 행별로 묶기
        by_row = {}
        for (row, col), value in ctx['pending_edits'].items():
            by_row.setdefault(row, {})[col] = value

        schema, tbl = ctx['schema'], ctx['table']
        qualified = f"`{schema}`.`{tbl}`" if schema else f"`{tbl}`"
        columns = ctx['columns']
        pk_cols = ctx['pk_columns']
        pk_indices = ctx['pk_indices']

        # 미리보기 생성
        preview = []
        for row_idx in sorted(by_row):
            col_values = by_row[row_idx]
            set_parts = []
            for c, v in col_values.items():
                set_parts.append(
                    f"`{columns[c]}`=" + ('NULL' if v is None else repr(v))
                )
            where_parts = []
            for i, pk_idx in enumerate(pk_indices):
                itm = table.item(row_idx, pk_idx)
                raw = itm.data(Qt.ItemDataRole.UserRole) if itm else None
                where_parts.append(
                    f"`{pk_cols[i]}`=" + ('NULL' if raw is None else repr(raw))
                )
            preview.append(
                f"UPDATE {qualified} SET {', '.join(set_parts)} "
                f"WHERE {' AND '.join(where_parts)};"
            )
        preview_text = '\n'.join(preview[:20])
        if len(preview) > 20:
            preview_text += f"\n... (총 {len(preview)}건)"

        reply = QMessageBox.question(
            self, '변경사항 적용',
            f"{len(preview)}개 행을 UPDATE 합니다.\n\n{preview_text}\n\n실행할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 실행 (트랜잭션)
        prev_autocommit = True
        try:
            prev_autocommit = self.db_connection.get_autocommit()
        except Exception:
            prev_autocommit = True

        failed = []
        success_count = 0
        try:
            self.db_connection.autocommit(False)
            with self.db_connection.cursor() as cursor:
                for row_idx in sorted(by_row):
                    col_values = by_row[row_idx]
                    set_parts = []
                    params = []
                    for c, v in col_values.items():
                        set_parts.append(f"`{columns[c]}`=%s")
                        params.append(v)
                    where_parts = []
                    for i, pk_idx in enumerate(pk_indices):
                        itm = table.item(row_idx, pk_idx)
                        raw = itm.data(Qt.ItemDataRole.UserRole) if itm else None
                        if raw is None:
                            where_parts.append(f"`{pk_cols[i]}` IS NULL")
                        else:
                            where_parts.append(f"`{pk_cols[i]}`=%s")
                            params.append(raw)
                    sql = (
                        f"UPDATE {qualified} SET {', '.join(set_parts)} "
                        f"WHERE {' AND '.join(where_parts)}"
                    )
                    try:
                        affected = cursor.execute(sql, params)
                        if affected == 1:
                            success_count += 1
                        else:
                            failed.append((row_idx, f'영향받은 행 수: {affected}'))
                    except Exception as e:
                        failed.append((row_idx, str(e)))

            if failed:
                self.db_connection.rollback()
                msg = '\n'.join(f'행 {r + 1}: {err}' for r, err in failed[:10])
                QMessageBox.critical(
                    self, '실패',
                    f'변경사항 적용 실패 (전체 롤백됨):\n\n{msg}'
                )
                return

            self.db_connection.commit()

        except Exception as e:
            try:
                self.db_connection.rollback()
            except Exception:
                pass
            QMessageBox.critical(self, '오류', f'UPDATE 실행 중 오류:\n{e}')
            return
        finally:
            try:
                self.db_connection.autocommit(prev_autocommit)
            except Exception:
                pass

        # 성공 처리: 원본값 갱신 + 시각 초기화
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
        self.message_text.append(f"✅ {success_count}개 행 UPDATE 적용 완료")

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

    def close_result_tab(self, index):
        """결과 탭 닫기"""
        if index > 0:  # 메시지 탭은 닫지 않음
            self.result_tabs.removeTab(index)

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
        """스키마 변경 시 메타데이터 리로드"""
        if not schema or not schema.strip():
            return

        self.metadata_provider.invalidate()
        self._load_metadata(schema)

    def _load_metadata(self, schema: str = None):
        """메타데이터 백그라운드 로드"""
        from src.core.db_connector import MySQLConnector
        from src.ui.workers.validation_worker import MetadataLoadWorker

        # 기존 워커 취소
        if self.metadata_worker and self.metadata_worker.isRunning():
            self.metadata_worker.cancel()
            self.metadata_worker.wait()

        # 연결 확보
        tid = self.config.get('id')
        db_user, db_password = self.config_mgr.get_tunnel_credentials(tid)

        if not db_user:
            return

        is_direct = self.config.get('connection_mode') == 'direct'

        try:
            if is_direct:
                host = self.config['remote_host']
                port = int(self.config['remote_port'])
            elif self.engine.is_running(tid):
                host, port = self.engine.get_connection_info(tid)
            else:
                # 터널 미실행 시 스킵
                return

            target_schema = schema or self.db_combo.currentText().strip()
            if not target_schema:
                return

            # 이전 연결 정리
            if self._metadata_connector:
                try:
                    self._metadata_connector.disconnect()
                except Exception:
                    pass
                self._metadata_connector = None

            connector = MySQLConnector(host, port, db_user, db_password, target_schema)
            success, _ = connector.connect()
            if not success:
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

        table_count = len(metadata.tables)
        version = f"{metadata.db_version[0]}.{metadata.db_version[1]}"
        self.validation_label.setText(f"✅ {table_count}개 테이블 로드됨 (MySQL {version})")

        # 현재 SQL 재검증
        self._on_validation_requested(self.editor.toPlainText())

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

        # 기존 워커 취소
        if self.validation_worker and self.validation_worker.isRunning():
            self.validation_worker.cancel()

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

        # 기존 워커 취소
        if self.autocomplete_worker and self.autocomplete_worker.isRunning():
            self.autocomplete_worker.cancel()

        self.autocomplete_worker = AutoCompleteWorker(
            self.sql_completer, sql, cursor_pos, schema
        )
        self.autocomplete_worker.completions_ready.connect(self._on_autocomplete_ready)
        self.autocomplete_worker.start()

    def _on_autocomplete_ready(self, completions: list):
        """자동완성 목록 준비됨"""
        self.editor.show_autocomplete_popup(completions)

    def closeEvent(self, event):
        """다이얼로그 닫기"""
        # 실행 중 확인
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "확인", "쿼리가 실행 중입니다. 정말 닫으시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        # 미커밋 + 모든 탭 수정 상태 확인
        warnings = []
        if self.pending_queries:
            warnings.append(f"미커밋 변경사항 {len(self.pending_queries)}건 (롤백됨)")

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

        # 검증 워커 정리
        if self.validation_worker and self.validation_worker.isRunning():
            self.validation_worker.cancel()
            self.validation_worker.wait()
        if self.metadata_worker and self.metadata_worker.isRunning():
            self.metadata_worker.cancel()
            self.metadata_worker.wait()
        if self.autocomplete_worker and self.autocomplete_worker.isRunning():
            self.autocomplete_worker.cancel()
            self.autocomplete_worker.wait()

        # 메타데이터 연결 정리
        if self._metadata_connector:
            try:
                self._metadata_connector.disconnect()
            except Exception:
                pass
            self._metadata_connector = None

        event.accept()
