"""안전 전환이 남긴 백업/후보 네임스페이스 조회·대조·정리 대화상자 (TF-STATUS-119)"""
from typing import Callable, List, Optional

from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from src.ui.workers.backup_lifecycle_worker import BackupLifecycleWorker

COLUMNS = ["복원 ID", "저널 상태", "백업 네임스페이스", "소유 증명", "테이블(행 추정)", "전환 판정", "후보 네임스페이스"]


def _rows_text(tables: list) -> str:
    total = sum(int(t.get("rows") or 0) for t in tables if isinstance(t, dict))
    return f"{len(tables)}개 ({total:,})"


class BackupLifecycleDialog(QDialog):
    """백업 목록을 보여주고, 결과 대조와 명시적 정리를 실행한다.

    삭제는 Core가 소유권·외부 참조·변경 여부를 다시 검증한 계획에 대해서만,
    미리보기를 확인한 뒤 명시적으로 승인했을 때 수행된다.
    """

    def __init__(self, endpoint: dict, input_dirs: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("복원 백업 관리")
        self.resize(980, 520)
        self.endpoint = dict(endpoint)
        self.input_dirs = list(input_dirs)
        self.entries: list = []
        self._worker: Optional[BackupLifecycleWorker] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "안전 전환이 남긴 백업과 후보 네임스페이스입니다. 저널로 소유가 증명된 항목만 정리할 수 있으며 "
            "원본과 미확인 객체는 삭제되지 않습니다."))
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(120)
        layout.addWidget(self.notes)

        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton("새로고침")
        self.btn_reconcile = QPushButton("전환 결과 대조")
        self.btn_cleanup_backup = QPushButton("백업 정리 미리보기")
        self.btn_cleanup_candidate = QPushButton("후보 정리 미리보기")
        self.btn_cleanup_clone = QPushButton("임시 clone 정리 미리보기")
        self.btn_cleanup_displaced = QPushButton("복구 백업 정리 미리보기")
        self.btn_rollback = QPushButton("백업으로 복구 미리보기")
        for button in (self.btn_refresh, self.btn_reconcile, self.btn_rollback, self.btn_cleanup_backup,
                       self.btn_cleanup_candidate, self.btn_cleanup_clone, self.btn_cleanup_displaced):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_reconcile.clicked.connect(self.reconcile)
        self.btn_cleanup_backup.clicked.connect(lambda: self.cleanup("backup"))
        self.btn_cleanup_candidate.clicked.connect(lambda: self.cleanup("candidate"))
        self.btn_cleanup_clone.clicked.connect(lambda: self.cleanup("clone"))
        self.btn_cleanup_displaced.clicked.connect(lambda: self.cleanup("displaced"))
        self.btn_rollback.clicked.connect(self.rollback)

    # -- 요청 -----------------------------------------------------------
    def _payload(self, action: str, **extra) -> dict:
        return {"action": action, "endpoint": self.endpoint, "input_dirs": self.input_dirs, **extra}

    def _request(self, payload: dict, on_done: Callable[[bool, str, dict], None]) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        worker = BackupLifecycleWorker(payload)
        worker.finished_with_result.connect(on_done)
        self._worker = worker
        worker.start()

    # -- 목록 -----------------------------------------------------------
    def refresh(self) -> None:
        self._request(self._payload("list"), self._on_list)

    def _on_list(self, success: bool, message: str, result: dict) -> None:
        if not success:
            QMessageBox.warning(self, "백업 목록 조회 실패", message)
            return
        self.entries = list(result.get("backups") or [])
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            backup = entry.get("backup") or {}
            candidate = entry.get("candidate") or {}
            cells = [
                entry.get("restore_id", ""), entry.get("journal_status", ""),
                (backup.get("namespace") or "-") + ("" if backup.get("exists", True) else " (없음)"),
                backup.get("ownership", "-"), _rows_text(backup.get("tables") or []) if backup else "-",
                backup.get("verdict", "-"),
                (candidate.get("namespace") or "-") + ("" if candidate.get("exists", True) else " (없음)"),
            ]
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(str(text)))
        lines = []
        for entry in self.entries:
            note = (entry.get("backup") or {}).get("saved_view_alias_note")
            if note:
                lines.append(f"[{entry.get('restore_id')}] {note}")
        for item in result.get("unproven_namespaces") or []:
            lines.append(f"소유 미증명: {item.get('namespace')} - 삭제 대상이 아닙니다.")
        self.notes.setPlainText("\n".join(dict.fromkeys(lines)))

    def _selected_restore_id(self) -> Optional[str]:
        model = self.table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            QMessageBox.information(self, "선택 필요", "먼저 목록에서 항목을 선택하세요.")
            return None
        return self.entries[rows[0].row()].get("restore_id")

    # -- 대조 -----------------------------------------------------------
    def reconcile(self) -> None:
        restore_id = self._selected_restore_id()
        if restore_id:
            self._request(self._payload("reconcile", restore_id=restore_id), self._on_reconciled)

    def _on_reconciled(self, success: bool, message: str, result: dict) -> None:
        if not success:
            QMessageBox.warning(self, "대조 실패", message)
            return
        QMessageBox.information(
            self, "전환 결과 대조",
            f"판정: {result.get('conclusion')}\n{result.get('message', '')}\n\n"
            "이 대조는 조회 전용이며 저널이나 DB 객체를 바꾸지 않습니다.")

    # -- 복구 -----------------------------------------------------------
    def rollback(self) -> None:
        restore_id = self._selected_restore_id()
        if restore_id:
            self._request(self._payload("rollback_plan", restore_id=restore_id),
                          lambda ok, msg, res: self._on_rollback_plan(restore_id, ok, msg, res))

    def _confirm_rollback(self, plan: dict) -> bool:
        displaced = "\n".join(f"- {t.get('name')}: {int(t.get('rows') or 0):,} rows" for t in plan.get("displace") or [])
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("복구 확인")
        box.setText(
            "보존된 원본 테이블을 원래 이름으로 되돌립니다.\n"
            f"현재 활성 테이블은 새 백업 {plan.get('displaced_backup')} 으로 이동해 보존되며 삭제되지 않습니다.\n\n"
            f"{displaced}")
        yes = box.addButton("복구", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def _on_rollback_plan(self, restore_id: str, success: bool, message: str, plan: dict) -> None:
        if not success:
            QMessageBox.warning(self, "복구 미리보기 실패", message)
            return
        if not plan.get("can_rollback"):
            QMessageBox.warning(
                self, "복구 차단",
                "복구할 수 없습니다:\n" + "\n".join(f"- {b}" for b in plan.get("blockers") or []))
            return
        if not self._confirm_rollback(plan):
            return
        self._request(
            self._payload("rollback_apply", restore_id=restore_id,
                          plan_digest=plan.get("plan_digest"), confirmed=True),
            self._on_rolled_back)

    def _on_rolled_back(self, success: bool, message: str, result: dict) -> None:
        if not success:
            QMessageBox.warning(self, "복구 실패", message)
        else:
            QMessageBox.information(self, "복구 결과", f"{result.get('status')}\n{result.get('message', '')}")
        self.refresh()

    # -- 정리 -----------------------------------------------------------
    def cleanup(self, target: str) -> None:
        restore_id = self._selected_restore_id()
        if restore_id:
            self._request(self._payload("cleanup_plan", restore_id=restore_id, target=target),
                          lambda ok, msg, res: self._on_plan(target, restore_id, ok, msg, res))

    def _confirm_cleanup(self, plan: dict) -> bool:
        tables = plan.get("tables") or []
        detail = "\n".join(f"- {t.get('name')}: {int(t.get('rows') or 0):,} rows" for t in tables)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("정리 확인")
        box.setText(
            f"다음 네임스페이스를 삭제합니다 (되돌릴 수 없음):\n{', '.join(plan.get('will_delete') or [])}\n\n{detail}\n\n"
            "원본과 다른 객체는 삭제되지 않습니다.")
        yes = box.addButton("삭제", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def _on_plan(self, target: str, restore_id: str, success: bool, message: str, plan: dict) -> None:
        if not success:
            QMessageBox.warning(self, "정리 미리보기 실패", message)
            return
        if not plan.get("can_cleanup"):
            QMessageBox.warning(
                self, "정리 차단",
                "정리할 수 없습니다:\n" + "\n".join(f"- {b}" for b in plan.get("blockers") or []))
            return
        if not self._confirm_cleanup(plan):
            return
        self._request(
            self._payload("cleanup_apply", restore_id=restore_id, target=target,
                          plan_digest=plan.get("plan_digest"), confirmed=True),
            self._on_applied)

    def _on_applied(self, success: bool, message: str, result: dict) -> None:
        if not success:
            QMessageBox.warning(self, "정리 실패", message)
        else:
            QMessageBox.information(self, "정리 완료", result.get("message", ""))
        self.refresh()
