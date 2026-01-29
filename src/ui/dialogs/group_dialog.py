"""
그룹 관리 다이얼로그

터널 그룹 생성/수정
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QColorDialog, QGroupBox, QFormLayout
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from src.ui.styles import ButtonStyles


class GroupDialog(QDialog):
    """그룹 생성/수정 다이얼로그"""

    def __init__(self, parent=None, group_data: dict = None):
        """
        Args:
            parent: 부모 위젯
            group_data: 수정 시 기존 그룹 데이터 {"id", "name", "color"}
        """
        super().__init__(parent)
        self._group_data = group_data
        self._is_edit_mode = group_data is not None
        self._selected_color = group_data.get('color', '#3498db') if group_data else '#3498db'

        self.setWindowTitle("그룹 수정" if self._is_edit_mode else "새 그룹")
        self.setFixedSize(400, 200)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 그룹 정보 입력
        group_box = QGroupBox("그룹 정보")
        form_layout = QFormLayout(group_box)

        # 이름 입력
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("그룹 이름을 입력하세요")
        if self._is_edit_mode:
            self.edit_name.setText(self._group_data.get('name', ''))
        form_layout.addRow("이름:", self.edit_name)

        # 색상 선택
        color_layout = QHBoxLayout()

        self.btn_color = QPushButton()
        self.btn_color.setFixedSize(60, 30)
        self._update_color_button()
        self.btn_color.clicked.connect(self._select_color)
        color_layout.addWidget(self.btn_color)

        self.label_color = QLabel(self._selected_color)
        self.label_color.setStyleSheet("color: #666; font-size: 11px;")
        color_layout.addWidget(self.label_color)
        color_layout.addStretch()

        form_layout.addRow("색상:", color_layout)

        layout.addWidget(group_box)

        # 버튼
        button_layout = QHBoxLayout()

        btn_save = QPushButton("저장" if self._is_edit_mode else "생성")
        btn_save.setStyleSheet(ButtonStyles.PRIMARY)
        btn_save.clicked.connect(self._on_save)

        btn_cancel = QPushButton("취소")
        btn_cancel.setStyleSheet(ButtonStyles.SECONDARY)
        btn_cancel.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(btn_save)
        button_layout.addWidget(btn_cancel)

        layout.addLayout(button_layout)

    def _update_color_button(self):
        """색상 버튼 업데이트"""
        self.btn_color.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._selected_color};
                border: 2px solid #ccc;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                border-color: #999;
            }}
        """)

    def _select_color(self):
        """색상 선택 다이얼로그"""
        color = QColorDialog.getColor(
            QColor(self._selected_color),
            self,
            "그룹 색상 선택"
        )
        if color.isValid():
            self._selected_color = color.name()
            self._update_color_button()
            self.label_color.setText(self._selected_color)

    def _on_save(self):
        """저장 버튼 클릭"""
        name = self.edit_name.text().strip()

        if not name:
            self.edit_name.setFocus()
            self.edit_name.setStyleSheet("border: 1px solid #e74c3c;")
            return

        self.accept()

    def get_result(self) -> dict:
        """결과 데이터 반환

        Returns:
            {"name": str, "color": str, "id": str (수정 모드인 경우)}
        """
        result = {
            "name": self.edit_name.text().strip(),
            "color": self._selected_color
        }
        if self._is_edit_mode and self._group_data:
            result["id"] = self._group_data.get('id')
        return result


# 편의 함수
def create_group_dialog(parent=None) -> tuple:
    """그룹 생성 다이얼로그

    Returns:
        (accepted, {"name", "color"}) 튜플
    """
    dialog = GroupDialog(parent)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return True, dialog.get_result()
    return False, None


def edit_group_dialog(parent=None, group_data: dict = None) -> tuple:
    """그룹 수정 다이얼로그

    Args:
        group_data: 기존 그룹 데이터

    Returns:
        (accepted, {"name", "color", "id"}) 튜플
    """
    dialog = GroupDialog(parent, group_data)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return True, dialog.get_result()
    return False, None
