"""
SQL 에디터 자동완성 팝업 위젯
"""
from PyQt6.QtWidgets import QListWidget, QListWidgetItem
from PyQt6.QtCore import Qt, pyqtSignal
from typing import List, Dict


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
