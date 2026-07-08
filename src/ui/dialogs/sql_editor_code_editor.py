"""
SQL 에디터 편집 위젯 (줄번호 거터 + 검증/자동완성 연동 코드 에디터 + 탭 위젯)
"""
import os
from PyQt6.QtWidgets import QWidget, QPlainTextEdit, QTextEdit, QToolTip, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, QRect, QSize, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QPainter, QTextCharFormat, QTextCursor
from typing import List, Dict, Optional

from src.ui.dialogs.sql_editor_highlighters import SQLHighlighter, SQLValidatorHighlighter
from src.ui.dialogs.sql_editor_autocomplete import AutoCompletePopup

LARGE_SQL_RENDER_LIMIT_BYTES = 512 * 1024


class LineNumberArea(QWidget):
    """줄 번호 표시 영역"""

    def __init__(self, editor):
        super().__init__(editor)
        self.code_editor = editor

    def sizeHint(self):
        return QSize(self.code_editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.code_editor.line_number_area_paint_event(event)


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


class ValidatingCodeEditor(CodeEditor):
    """검증 + 자동완성 기능이 있는 코드 에디터"""

    validation_requested = pyqtSignal(str)  # SQL 텍스트
    autocomplete_requested = pyqtSignal(str, int)  # SQL, cursor_pos

    def __init__(self, parent=None):
        super().__init__(parent)

        # 검증 하이라이터로 교체
        self.highlighter = SQLValidatorHighlighter(self.document())
        self._large_document_mode = False

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
        if self._large_document_mode:
            return
        self._validation_timer.stop()
        self._validation_timer.start()

    def _trigger_validation(self):
        """검증 실행 시그널 발생"""
        if self._large_document_mode:
            return
        self.validation_requested.emit(self.toPlainText())

    def set_validation_issues(self, issues: list):
        """검증 이슈 설정"""
        self._issues = issues
        if not self._large_document_mode:
            self.highlighter.set_issues(issues)

    def set_large_document_mode(self, enabled: bool):
        """Disable expensive whole-document features for large SQL text."""
        enabled = bool(enabled)
        if self._large_document_mode == enabled:
            return

        self._large_document_mode = enabled
        self._validation_timer.stop()
        self._issues = []

        if enabled:
            if self.highlighter:
                self.highlighter.setDocument(None)
            self.setToolTip("대용량 SQL 파일: 구문 하이라이트와 실시간 검증이 비활성화되었습니다.")
        else:
            self.highlighter = SQLValidatorHighlighter(self.document())
            self.setToolTip("")

    def is_large_document_mode(self) -> bool:
        return self._large_document_mode

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
        self._set_large_document_mode_for_text(text)
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
            self._set_large_document_mode_for_text(content)
            self.editor.blockSignals(True)
            self.editor.setPlainText(content)
            self.editor.blockSignals(False)
            self.file_path = file_path
            self.is_modified = False
            self.title_changed.emit(self.get_title())
            return True
        except Exception:
            return False

    def _set_large_document_mode_for_text(self, text: str):
        byte_size = len(text.encode("utf-8", errors="ignore"))
        is_large = byte_size >= LARGE_SQL_RENDER_LIMIT_BYTES
        self.editor.set_large_document_mode(is_large)
        if is_large:
            self.validation_label.setText(
                "대용량 SQL: 구문 하이라이트와 실시간 검증을 비활성화했습니다."
            )
        else:
            self.validation_label.setText("")

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
