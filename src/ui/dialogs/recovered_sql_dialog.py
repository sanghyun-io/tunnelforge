"""Read-only list of SQL workspaces whose profile no longer exists ("Recovered SQL", P1-4).

View, copy, save to a file and delete only - there is deliberately no editing. Entries older than
90 days are marked expired; nothing is deleted without the user's confirmation.
"""
from typing import Iterable, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from src.core.workspace_store import (
    STATUS_OK, WorkspaceStore, WorkspaceSummary, WorkspaceStoreError,
)


def find_orphans(store: WorkspaceStore, known_profile_ids: Iterable[str]) -> List[WorkspaceSummary]:
    return [item for item in store.list_workspaces(known_profile_ids) if item.orphan]


class RecoveredSqlDialog(QDialog):
    def __init__(self, store: WorkspaceStore, known_profile_ids: Iterable[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("복구된 SQL")
        self.resize(1000, 540)
        self.store = store
        self.known_profile_ids = list(known_profile_ids)
        self._items: List[WorkspaceSummary] = []
        self._tabs = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "삭제된 연결 프로필에서 남은 SQL 작업 공간입니다. 읽기 전용이며 90일이 지난 항목은 '만료'로 표시됩니다."))
        # 목록 폭을 넉넉히 잡고(프로필 id/시각이 잘리지 않게) 사용자가 분할선을 끌어 조절할 수 있게 한다.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        left_panel = QWidget()
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(0, 0, 0, 0)
        self.workspace_list = QListWidget()
        self.workspace_list.setMinimumWidth(300)
        self.workspace_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.workspace_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # 넘치면 가운데를 줄이고 툴팁으로 확인
        self.workspace_list.currentRowChanged.connect(self._on_workspace_selected)
        left.addWidget(self.workspace_list)
        self.tab_list = QListWidget()
        self.tab_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tab_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tab_list.currentRowChanged.connect(self._on_tab_selected)
        left.addWidget(self.tab_list)
        self.splitter.addWidget(left_panel)
        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.splitter.addWidget(self.viewer)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 580])
        layout.addWidget(self.splitter, 1)

        buttons = QHBoxLayout()
        self.btn_copy = QPushButton("클립보드로 복사")
        self.btn_save = QPushButton("파일로 저장")
        self.btn_delete = QPushButton("작업 공간 삭제")
        for button in (self.btn_copy, self.btn_save, self.btn_delete):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.btn_copy.clicked.connect(self.copy_current)
        self.btn_save.clicked.connect(self.save_current)
        self.btn_delete.clicked.connect(self.delete_current)
        self.reload()

    # -- data --------------------------------------------------------------
    def reload(self) -> None:
        self._items = find_orphans(self.store, self.known_profile_ids)
        self.workspace_list.clear()
        for item in self._items:
            when = item.saved_at.strftime("%Y-%m-%d %H:%M") if item.saved_at else "?"
            mark = " [만료]" if item.expired else ""
            self.workspace_list.addItem(f"{item.profile_id[:8]}…  {when}  탭 {item.tab_count}개{mark}")
            # 잘릴 수 있는 정보는 툴팁에 전체를 보여 준다
            self.workspace_list.item(self.workspace_list.count() - 1).setToolTip(
                f"프로필 ID: {item.profile_id}\n저장 시각: {when}\n탭: {item.tab_count}개"
                + ("\n90일이 지나 만료됨" if item.expired else ""))
        self.tab_list.clear()
        self.viewer.clear()
        self._tabs = []
        if self._items:
            self.workspace_list.setCurrentRow(0)

    def _on_workspace_selected(self, row: int) -> None:
        self.tab_list.clear()
        self.viewer.clear()
        self._tabs = []
        if not 0 <= row < len(self._items):
            return
        loaded = self.store.load(self._items[row].profile_id)
        if loaded.status != STATUS_OK or loaded.state is None:
            self.viewer.setPlainText(f"읽을 수 없는 작업 공간입니다: {loaded.message or loaded.status}")
            return
        self._tabs = loaded.state.tabs
        for tab in self._tabs:
            name = tab.file_path or f"Query {tab.title_index}"
            suffix = "" if tab.text else " (디스크 파일 / 초안 없음)"
            self.tab_list.addItem(name + suffix)
            self.tab_list.item(self.tab_list.count() - 1).setToolTip(name + suffix)
        if self._tabs:
            self.tab_list.setCurrentRow(0)

    def _on_tab_selected(self, row: int) -> None:
        if 0 <= row < len(self._tabs):
            tab = self._tabs[row]
            self.viewer.setPlainText(tab.text if tab.text is not None else (
                f"이 탭은 파일 {tab.file_path} 의 저장된 내용을 사용하며 초안이 따로 없습니다." if tab.file_path
                else "저장된 초안이 없습니다."))

    def _current_text(self) -> Optional[str]:
        row = self.tab_list.currentRow()
        if 0 <= row < len(self._tabs) and self._tabs[row].text:
            return self._tabs[row].text
        return None

    # -- actions -----------------------------------------------------------
    def copy_current(self) -> None:
        text = self._current_text()
        if text is not None:
            QApplication.clipboard().setText(text)

    def _choose_save_path(self) -> str:
        path, _ = QFileDialog.getSaveFileName(self, "SQL 파일 저장", "", "SQL 파일 (*.sql);;모든 파일 (*.*)")
        return path

    def save_current(self) -> bool:
        text = self._current_text()
        if text is None:
            return False
        path = self._choose_save_path()
        if not path:
            return False
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))
            return False
        return True

    def _confirm_delete(self) -> bool:
        reply = QMessageBox.question(
            self, "작업 공간 삭제", "이 작업 공간의 복구 데이터를 삭제합니다. 되돌릴 수 없습니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def delete_current(self) -> bool:
        row = self.workspace_list.currentRow()
        if not 0 <= row < len(self._items) or not self._confirm_delete():
            return False
        try:
            self.store.delete(self._items[row].profile_id)
        except WorkspaceStoreError as exc:
            QMessageBox.warning(self, "삭제 실패", str(exc))
            return False
        self.reload()
        return True
