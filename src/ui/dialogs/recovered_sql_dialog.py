"""Read-only list of SQL workspaces whose profile no longer exists ("Recovered SQL", P1-4).

View, copy, save to a file and delete only - there is deliberately no editing. Entries older than
90 days are marked expired; nothing is deleted without the user's confirmation.
"""
from typing import Iterable, List, Optional

from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout,
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
        self.resize(900, 520)
        self.store = store
        self.known_profile_ids = list(known_profile_ids)
        self._items: List[WorkspaceSummary] = []
        self._tabs = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "삭제된 연결 프로필에서 남은 SQL 작업 공간입니다. 읽기 전용이며 90일이 지난 항목은 '만료'로 표시됩니다."))
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.workspace_list = QListWidget()
        self.workspace_list.currentRowChanged.connect(self._on_workspace_selected)
        left.addWidget(self.workspace_list)
        self.tab_list = QListWidget()
        self.tab_list.currentRowChanged.connect(self._on_tab_selected)
        left.addWidget(self.tab_list)
        body.addLayout(left, 1)
        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        body.addWidget(self.viewer, 2)
        layout.addLayout(body)

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
