"""작업 목록 대화상자 (TF-STATUS-132): Export / Import / 안전 전환 / 크로스엔진 이관 실행 기록.

정렬(열 머리글 클릭)과 필터(상태/종류/검색어), 보고서 열기, 실패 원인 보기, 기록 삭제를 제공한다.
"다시 열기"는 원래 대화상자를 여는 것까지만 한다: Export 는 이전 설정을 채워 열고, 실제 실행 때
Core 의 사전 검증을 다시 거친다. Import/안전 전환/이관 실행은 설정을 채우지 않고 원래 대화상자만 연다
(부분 재시도 금지 정책).
"""
import os
from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QBrush, QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from src.core import job_history as jh
from src.core.job_history import JobHistory, JobRecord

COLUMNS = ["시작", "종류", "대상", "프로필", "실행 모드", "상태", "소요", "오류 요약"]

KIND_LABELS = {
    jh.KIND_EXPORT_FULL: "Export (전체)",
    jh.KIND_EXPORT_TABLES: "Export (선택 테이블)",
    jh.KIND_IMPORT: "Import",
    jh.KIND_PROMOTE: "안전 전환",
    jh.KIND_MIGRATION_PREFLIGHT: "이관 사전 점검",
    jh.KIND_MIGRATION_RUN: "이관 실행",
    jh.KIND_MIGRATION_RESUME: "이관 재개",
    jh.KIND_SCHEDULED_BACKUP: "예약 백업",
    jh.KIND_RESTORE_REHEARSAL: "복원 리허설",
}
STATUS_LABELS = {
    jh.STATUS_RUNNING: "실행 중",
    jh.STATUS_COMPLETED: "완료",
    jh.STATUS_PARTIAL: "부분 완료",
    jh.STATUS_FAILED: "실패",
    jh.STATUS_CANCELLED: "취소",
    jh.STATUS_INTERRUPTED: "중단됨 (앱 종료)",
    jh.STATUS_SKIPPED: "건너뜀",
}
# 상태 열 글자색. 색만으로 구분하지 않도록 상태 텍스트는 항상 함께 표시한다.
STATUS_COLORS = {
    jh.STATUS_FAILED: "#c0392b",
    jh.STATUS_PARTIAL: "#b9770e",
    jh.STATUS_INTERRUPTED: "#8e44ad",
}
# 새 사전 검증을 다시 거치는 종류만 "설정을 채워" 다시 연다.
REOPEN_WITH_SETTINGS = (jh.KIND_EXPORT_FULL, jh.KIND_EXPORT_TABLES)


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if hours else (f"{minutes}분 {secs}초" if minutes else f"{secs}초")


def format_local(record: JobRecord) -> str:
    started = record.started()
    return started.astimezone().strftime("%Y-%m-%d %H:%M:%S") if started else "-"


class _SortItem(QTableWidgetItem):
    """표시 문자열과 별개의 정렬 키를 가진 항목."""

    def __init__(self, text: str, key):
        super().__init__(text)
        self._key = key

    def __lt__(self, other):
        if isinstance(other, _SortItem):
            return self._key < other._key
        return super().__lt__(other)


class JobListDialog(QDialog):
    def __init__(self, history: JobHistory, reopen: Optional[Callable[[JobRecord], None]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("작업 목록")
        self.resize(1100, 560)
        self.history = history
        self._reopen = reopen
        self._records: List[JobRecord] = []
        self._shown: List[JobRecord] = []

        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("상태:"))
        self.status_filter = QComboBox()
        self.status_filter.addItem("전체", "")
        for status in jh.STATUSES:
            self.status_filter.addItem(STATUS_LABELS[status], status)
        filters.addWidget(self.status_filter)
        filters.addWidget(QLabel("종류:"))
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("전체", "")
        for kind in jh.KINDS:
            self.kind_filter.addItem(KIND_LABELS[kind], kind)
        filters.addWidget(self.kind_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("대상 / 프로필 / 오류 검색")
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)  # 대상: 긴 경로는 사용자가 조절
        self.table.setColumnWidth(2, 240)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)  # 오류 요약이 남은 폭을 채운다
        header.setStretchLastSection(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 3)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        layout.addWidget(self.detail, 1)

        buttons = QHBoxLayout()
        self.btn_report = QPushButton("보고서 열기")
        self.btn_error = QPushButton("실패 원인 보기")
        self.btn_reopen = QPushButton("다시 열기")
        self.btn_delete = QPushButton("선택 기록 삭제")
        self.btn_clear = QPushButton("모든 기록 삭제")
        self.btn_refresh = QPushButton("새로고침")
        for button in (self.btn_report, self.btn_error, self.btn_reopen, self.btn_delete, self.btn_clear, self.btn_refresh):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.btn_reopen.setVisible(reopen is not None)

        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.kind_filter.currentIndexChanged.connect(self._apply_filters)
        self.search.textChanged.connect(self._apply_filters)
        self.btn_report.clicked.connect(self.open_report)
        self.btn_error.clicked.connect(self.show_error)
        self.btn_reopen.clicked.connect(self.reopen_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_clear.clicked.connect(self.clear_all)
        self.btn_refresh.clicked.connect(self.reload)
        self.reload()

    # -- data ---------------------------------------------------------------
    def reload(self) -> None:
        self._records = self.history.list()
        self._apply_filters()

    def _matches(self, record: JobRecord) -> bool:
        status, kind = self.status_filter.currentData(), self.kind_filter.currentData()
        if status and record.status != status:
            return False
        if kind and record.kind != kind:
            return False
        needle = self.search.text().strip().lower()
        if needle:
            haystack = " ".join((record.target, record.profile_name, record.error_summary, record.mode)).lower()
            return needle in haystack
        return True

    def _apply_filters(self) -> None:
        self._shown = [r for r in self._records if self._matches(r)]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._shown))
        for row, record in enumerate(self._shown):
            started = record.started()
            duration = record.duration_seconds()
            cells = [
                _SortItem(format_local(record), started.timestamp() if started else 0),
                QTableWidgetItem(KIND_LABELS.get(record.kind, record.kind)),
                QTableWidgetItem(record.target),
                QTableWidgetItem(record.profile_name),
                QTableWidgetItem(record.mode),
                QTableWidgetItem(STATUS_LABELS.get(record.status, record.status)),
                _SortItem(format_duration(duration), duration if duration is not None else -1),
                QTableWidgetItem(record.error_summary),
            ]
            cells[0].setData(Qt.ItemDataRole.UserRole, record.id)
            color = STATUS_COLORS.get(record.status)
            if color:
                cells[5].setForeground(QBrush(QColor(color)))
            # 잘린 열은 툴팁에서 전체를 읽는다
            for column in (2, 3):
                cells[column].setToolTip(cells[column].text())
            if record.error_summary:
                cells[7].setToolTip(record.error_summary)
            for column, item in enumerate(cells):
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        for column in (0, 1, 3, 4, 5, 6):
            self.table.resizeColumnToContents(column)
        self.detail.clear()
        self._update_buttons()

    def selected_record(self) -> Optional[JobRecord]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        job_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((r for r in self._records if r.id == job_id), None)

    def _on_selection_changed(self) -> None:
        record = self.selected_record()
        if record is None:
            self.detail.clear()
        else:
            self.detail.setPlainText(self.describe(record))
        self._update_buttons()

    @staticmethod
    def describe(record: JobRecord) -> str:
        lines = [
            f"종류: {KIND_LABELS.get(record.kind, record.kind)}",
            f"상태: {STATUS_LABELS.get(record.status, record.status)}",
            f"대상: {record.target}",
            f"프로필: {record.profile_name or '-'}",
            f"실행 모드: {record.mode or '-'}",
            f"시작: {format_local(record)}",
            f"소요: {format_duration(record.duration_seconds())}",
        ]
        if record.error_summary:
            lines.append(f"오류: {record.error_summary}")
        if record.report_path:
            lines.append(f"보고서: {record.report_path}")
        if record.log_path:
            lines.append(f"로그: {record.log_path}")
        for key, value in record.details.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines)

    def _update_buttons(self) -> None:
        record = self.selected_record()
        self.btn_report.setEnabled(bool(record and (record.report_path or record.log_path)))
        self.btn_error.setEnabled(bool(record and record.error_summary))
        self.btn_delete.setEnabled(bool(record and record.status != jh.STATUS_RUNNING))
        self.btn_reopen.setEnabled(record is not None)
        if record is not None:
            self.btn_reopen.setText("같은 설정으로 다시 열기 (실행 때 새로 검증)" if record.kind in REOPEN_WITH_SETTINGS
                                    else "원래 대화상자 열기")

    # -- actions ------------------------------------------------------------
    def _open_path(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_report(self) -> bool:
        record = self.selected_record()
        if record is None:
            return False
        for path in (record.report_path, record.log_path):
            if path and os.path.exists(path):
                self._open_path(path)
                return True
            parent = os.path.dirname(path) if path else ""
            if parent and os.path.isdir(parent):
                self._open_path(parent)  # 파일이 없으면 보고서가 있어야 할 폴더를 연다
                return True
        QMessageBox.information(self, "보고서 열기", "보고서 파일을 찾을 수 없습니다. 이동되었거나 삭제되었을 수 있습니다.")
        return False

    def show_error(self) -> None:
        record = self.selected_record()
        if record is not None and record.error_summary:
            QMessageBox.information(self, "실패 원인", record.error_summary)

    def reopen_selected(self) -> None:
        record = self.selected_record()
        if record is not None and self._reopen is not None:
            self._reopen(record)

    def _confirm(self, title: str, text: str) -> bool:
        reply = QMessageBox.question(self, title, text,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def delete_selected(self) -> bool:
        record = self.selected_record()
        if record is None or record.status == jh.STATUS_RUNNING:
            return False
        if not self._confirm("기록 삭제", "선택한 작업 기록을 삭제합니다. 보고서 파일과 Export/Import 결과물은 삭제되지 않습니다. 계속할까요?"):
            return False
        self.history.delete([record.id])
        self.reload()
        return True

    def clear_all(self) -> bool:
        if not self._confirm("모든 기록 삭제", "실행 중이 아닌 모든 작업 기록을 삭제합니다. 보고서 파일과 결과물은 삭제되지 않습니다. 계속할까요?"):
            return False
        self.history.clear()
        self.reload()
        return True
