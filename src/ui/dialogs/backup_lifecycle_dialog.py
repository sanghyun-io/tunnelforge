"""안전 전환이 남긴 백업/후보 네임스페이스 조회·대조·정리 대화상자 (TF-STATUS-119)"""
from typing import Callable, List, Optional

from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from src.ui.styles import ButtonStyles
from src.ui.workers.backup_lifecycle_worker import BackupLifecycleWorker

COLUMNS = ["복원 ID", "전환 기록", "백업 네임스페이스", "소유 확인", "테이블(행 추정)", "실제 전환 상태", "후보 네임스페이스"]

# Core 상태 코드 → 화면 라벨. 모르는 코드는 그대로 보여 준다 (원본 코드는 툴팁).
JOURNAL_LABELS = {
    "not_attempted": "전환 시도 안 함",
    "planned": "전환 계획만 있음",
    "pending": "결과 미기록",
    "preparing": "전환 준비 중",
    "promoted": "전환 완료",
    "cutover_unknown": "전환 결과 불명",
    "failed_no_change": "실패 (변경 없음)",
    "failed_original_unchanged": "실패 (원본 그대로)",
    "rolled_back": "백업으로 복구됨",
    "rollback_unknown": "복구 결과 불명",
}
OWNERSHIP_LABELS = {
    "proven": "TunnelForge 소유 확인",
    "unproven": "소유 미확인",
    "missing": "없음",
    "journal_named": "저널에 기록됨",
    "destination_not_a_candidate": "복원 대상 (정리 안 함)",
}
VERDICT_LABELS = {
    "promoted": "전환됨",
    "not_promoted": "전환 안 됨",
    "undeterminable": "판정 불가",
    "none": "해당 없음",
}


def status_label(labels: dict, code) -> str:
    code = str(code or "-")
    return labels.get(code, code)


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
        self._busy = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "안전 전환이 남긴 백업과 후보 네임스페이스입니다. 저널로 소유가 증명된 항목만 정리할 수 있으며 "
            "원본과 미확인 객체는 삭제되지 않습니다."))
        self.label_state = QLabel("")
        layout.addWidget(self.label_state)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(120)
        layout.addWidget(self.notes)

        # 조회 전용 동작과 DB를 바꾸는 동작(복구/정리)을 그룹과 색으로 구분한다.
        safe_group = QGroupBox("조회 (DB를 바꾸지 않음)")
        safe_row = QHBoxLayout(safe_group)
        self.btn_refresh = QPushButton("새로고침")
        self.btn_reconcile = QPushButton("전환 결과 대조")
        safe_row.addWidget(self.btn_refresh)
        safe_row.addWidget(self.btn_reconcile)
        danger_group = QGroupBox("복구 / 정리 (DB 변경 · 미리보기 확인 후 실행)")
        danger_row = QHBoxLayout(danger_group)
        self.btn_rollback = QPushButton("백업으로 복구 미리보기")
        self.btn_cleanup_backup = QPushButton("백업 정리 미리보기")
        self.btn_cleanup_candidate = QPushButton("후보 정리 미리보기")
        self.btn_cleanup_clone = QPushButton("임시 clone 정리 미리보기")
        self.btn_cleanup_displaced = QPushButton("복구 백업 정리 미리보기")
        self._danger_buttons = (self.btn_rollback, self.btn_cleanup_backup, self.btn_cleanup_candidate,
                                self.btn_cleanup_clone, self.btn_cleanup_displaced)
        for button in self._danger_buttons:
            button.setStyleSheet(ButtonStyles.DELETE)
            danger_row.addWidget(button)
        danger_row.addStretch()
        self.btn_close = QPushButton("닫기")
        self.btn_close.clicked.connect(self.close)
        top_row = QHBoxLayout()
        top_row.addWidget(safe_group)
        top_row.addStretch()
        top_row.addWidget(self.btn_close)
        layout.addLayout(top_row)
        layout.addWidget(danger_group)
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_reconcile.clicked.connect(self.reconcile)
        self.btn_cleanup_backup.clicked.connect(lambda: self.cleanup("backup"))
        self.btn_cleanup_candidate.clicked.connect(lambda: self.cleanup("candidate"))
        self.btn_cleanup_clone.clicked.connect(lambda: self.cleanup("clone"))
        self.btn_cleanup_displaced.clicked.connect(lambda: self.cleanup("displaced"))
        self.btn_rollback.clicked.connect(self.rollback)
        self._update_buttons()

    def _update_buttons(self) -> None:
        """요청 중에는 모두 끄고, 항목 대상 동작은 선택이 있을 때만 켠다."""
        model = self.table.selectionModel()
        selected = bool(model and model.selectedRows())
        self.btn_refresh.setEnabled(not self._busy)
        for button in (self.btn_reconcile, *self._danger_buttons):
            button.setEnabled(not self._busy and selected)

    # -- 요청 -----------------------------------------------------------
    def _payload(self, action: str, **extra) -> dict:
        return {"action": action, "endpoint": self.endpoint, "input_dirs": self.input_dirs, **extra}

    def _request(self, payload: dict, on_done: Callable[[bool, str, dict], None]) -> None:
        if self._busy:
            return
        worker = BackupLifecycleWorker(payload)
        result = []
        worker.finished_with_result.connect(lambda *values: result.extend(values))
        # 스레드가 완전히 끝난 뒤(QThread.finished) 결과를 넘겨야 이어지는 요청(미리보기 → 실행)이 무시되지 않는다.
        worker.finished.connect(lambda: self._on_worker_done(worker, result, on_done))
        self._worker = worker
        self._busy = True
        self.label_state.setText("조회 중…" if payload.get("action") == "list" else "처리 중…")
        self._update_buttons()
        worker.start()

    def _on_worker_done(self, worker, result: list, on_done: Callable[[bool, str, dict], None]) -> None:
        worker.wait()
        self._busy = False
        self.label_state.setText("")
        self._update_buttons()
        on_done(*(result or [False, "응답 없이 종료되었습니다.", {}]))

    # -- 목록 -----------------------------------------------------------
    def refresh(self) -> None:
        self._request(self._payload("list"), self._on_list)

    def _on_list(self, success: bool, message: str, result: dict) -> None:
        if not success:
            self.label_state.setText("목록을 불러오지 못했습니다.")
            QMessageBox.warning(self, "백업 목록 조회 실패", message)
            return
        self.entries = list(result.get("backups") or [])
        self.label_state.setText(f"보존된 백업 {len(self.entries)}건" if self.entries else "보존된 백업이 없습니다.")
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            backup = entry.get("backup") or {}
            candidate = entry.get("candidate") or {}
            cells = [
                (entry.get("restore_id", ""), None),
                (status_label(JOURNAL_LABELS, entry.get("journal_status")), entry.get("journal_status")),
                ((backup.get("namespace") or "-") + ("" if backup.get("exists", True) else " (없음)"), None),
                (status_label(OWNERSHIP_LABELS, backup.get("ownership")), backup.get("ownership")),
                (_rows_text(backup.get("tables") or []) if backup else "-", None),
                (status_label(VERDICT_LABELS, backup.get("verdict")), backup.get("verdict")),
                ((candidate.get("namespace") or "-") + ("" if candidate.get("exists", True) else " (없음)"), None),
            ]
            for column, (text, code) in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if code:
                    item.setToolTip(str(code))
                self.table.setItem(row, column, item)
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
            f"판정: {status_label(VERDICT_LABELS, result.get('conclusion'))}\n{result.get('message', '')}\n\n"
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
            QMessageBox.information(self, "복구 결과", f"{status_label(JOURNAL_LABELS, result.get('status'))}\n{result.get('message', '')}")
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
