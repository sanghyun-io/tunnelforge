"""실행 계획 보기 대화상자 (TF-STATUS-133).

계획 트리(노드, 비용/행 추정, ANALYZE 시 실제 행/시간)와 원문 탭을 보여준다. 순차 스캔, filesort,
임시 테이블 등은 강조하지만 인덱스 추가 같은 권고는 하지 않는다(사실 표시만).
ANALYZE 는 쿼리를 실제로 실행하므로 명시적으로 선택하고 확인해야 한다.
"""
import uuid
from typing import Callable, Optional

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from src.core.db_core_client import DbCoreServiceError
from src.core.explain_plan import ANALYZE_WARNING, ExplainResult, PlanNode, explain_on_connection
from src.core.logger import get_logger

logger = get_logger(__name__)

COLUMNS = ["노드", "비용", "예상 행", "실제 행", "실제 시간(ms)", "루프", "비고"]
HIGHLIGHT = QColor("#fdecea")

# 실행 중인 워커는 대화상자가 닫혀도 끝날 때까지 참조를 유지한다 (QThread 가 실행 중 파괴되지 않도록).
_running_workers = set()


def has_active_explain_workers() -> bool:
    return bool(_running_workers)


def _fmt(value, digits=2) -> str:
    if value is None:
        return ""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.{digits}f}"


class ExplainWorker(QThread):
    done = pyqtSignal(object, str, str)  # (ExplainResult | None, error message, error_code)

    def __init__(self, explain: Callable, facade, connection_id: str, sql: str, analyze: bool,
                 job_id: str, timeout_ms: Optional[int]):
        super().__init__(None)
        self._args = (facade, connection_id, sql)
        self._explain, self._analyze, self._job_id, self._timeout_ms = explain, analyze, job_id, timeout_ms
        self.finished.connect(self._release, Qt.ConnectionType.QueuedConnection)

    def start(self, *args):
        _running_workers.add(self)
        try:
            super().start(*args)
        except Exception:
            _running_workers.discard(self)
            raise

    def run(self):
        try:
            result = self._explain(*self._args, analyze=self._analyze, job_id=self._job_id,
                                   timeout_ms=self._timeout_ms)
            self.done.emit(result, "", "")
        except DbCoreServiceError as exc:
            self.done.emit(None, str(exc), str(getattr(exc, "error_code", "") or ""))
        except Exception as exc:  # 파싱 오류 등
            logger.exception("explain failed")
            self.done.emit(None, str(exc), "")

    def _release(self):
        _running_workers.discard(self)
        self.deleteLater()


class ExplainPlanDialog(QDialog):
    def __init__(self, parent, facade, connection_id: str, sql: str, timeout_ms: Optional[int] = None,
                 explain: Callable = explain_on_connection):
        super().__init__(parent)
        self.setWindowTitle("실행 계획")
        self.resize(900, 620)
        self._facade, self._connection_id, self._sql = facade, connection_id, sql
        self._timeout_ms, self._explain = timeout_ms, explain
        self._worker: Optional[ExplainWorker] = None
        self._job_id = ""
        self.result: Optional[ExplainResult] = None

        layout = QVBoxLayout(self)
        self.txt_sql = QPlainTextEdit(sql)
        self.txt_sql.setReadOnly(True)
        self.txt_sql.setMaximumHeight(90)
        layout.addWidget(self.txt_sql)

        controls = QHBoxLayout()
        self.chk_analyze = QCheckBox("ANALYZE (쿼리를 실제로 실행하여 실제 행/시간 측정)")
        self.chk_analyze.toggled.connect(self._update_warning)
        controls.addWidget(self.chk_analyze)
        controls.addStretch()
        self.btn_run = QPushButton("계획 보기")
        self.btn_run.clicked.connect(self.run_explain)
        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.clicked.connect(self.cancel_running)
        self.btn_cancel.setEnabled(False)
        self.btn_close = QPushButton("닫기")
        self.btn_close.clicked.connect(self.reject)
        for button in (self.btn_run, self.btn_cancel, self.btn_close):
            controls.addWidget(button)
        layout.addLayout(controls)

        self.lbl_warning = QLabel(ANALYZE_WARNING)
        self.lbl_warning.setWordWrap(True)
        self.lbl_warning.setStyleSheet("color: #c0392b; font-weight: bold;")
        self.lbl_warning.setVisible(False)
        layout.addWidget(self.lbl_warning)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        self.tabs = QTabWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setColumnWidth(0, 360)
        self.tabs.addTab(self.tree, "계획 트리")
        self.txt_raw = QPlainTextEdit()
        self.txt_raw.setReadOnly(True)
        self.txt_raw.setFont(QFont("Consolas"))
        self.tabs.addTab(self.txt_raw, "원문")
        layout.addWidget(self.tabs, 1)

    # --- 실행 ---
    def _update_warning(self, checked: bool):
        self.lbl_warning.setVisible(checked)

    def is_running(self) -> bool:
        return self._worker is not None

    def run_explain(self):
        if self._worker is not None:
            return
        analyze = self.chk_analyze.isChecked()
        if analyze:
            reply = QMessageBox.question(
                self, "ANALYZE 실행 확인",
                "ANALYZE 는 쿼리를 실제로 실행합니다.\n실행 시간과 서버 부하가 발생할 수 있습니다. 계속할까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._job_id = f"explain-{uuid.uuid4().hex[:12]}"
        self._set_running(True)
        self.lbl_status.setText("실행 계획을 조회하는 중…")
        worker = ExplainWorker(self._explain, self._facade, self._connection_id, self._sql, analyze,
                               self._job_id, self._timeout_ms)
        self._worker = worker
        worker.done.connect(lambda result, error, code, w=worker: self._deliver(w, result, error, code))
        worker.start()

    def cancel_running(self):
        """서버에 취소를 요청하고, 늦게 도착하는 결과는 버린다."""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        try:
            self._facade.cancel_query(self._job_id)
        except Exception:
            logger.warning("explain cancel request failed", exc_info=True)
        self._set_running(False)
        self.lbl_status.setText("취소했습니다.")

    def _set_running(self, running: bool):
        self.btn_run.setEnabled(not running)
        self.chk_analyze.setEnabled(not running)
        self.btn_cancel.setEnabled(running)

    def _deliver(self, worker, result, error: str, code: str):
        try:
            self._on_done(worker, result, error, code)
        except RuntimeError:
            pass  # 대화상자(C++ 객체)가 이미 파괴된 뒤 도착한 결과

    def _on_done(self, worker, result, error: str, code: str):
        if worker is not self._worker:
            return  # 취소되었거나 대화상자가 닫힌 뒤 도착한 결과
        self._worker = None
        self._set_running(False)
        if result is None:
            self.lbl_status.setText(error)
            QMessageBox.warning(self, "실행 계획", error)
            return
        self.show_result(result)

    def reject(self):
        self.cancel_running()
        super().reject()

    # --- 표시 ---
    def show_result(self, result: ExplainResult):
        self.result = result
        self.tree.clear()
        if result.root is not None:
            self._add_node(self.tree.invisibleRootItem(), result.root)
            self.tree.expandAll()
        self.txt_raw.setPlainText(result.raw)
        parts = [f"{result.engine} / {'ANALYZE(실제 실행)' if result.analyze else 'EXPLAIN(실행 안 함)'}"]
        parts += [f"{key}: {value} ms" for key, value in result.summary.items()]
        parts += result.warnings
        self.lbl_status.setText("  |  ".join(parts))

    def _add_node(self, parent: QTreeWidgetItem, node: PlanNode):
        remark = ", ".join(node.flags + ([node.detail] if node.detail else []))
        item = QTreeWidgetItem(parent, [
            node.title, _fmt(node.cost), _fmt(node.rows_estimate), _fmt(node.rows_actual),
            _fmt(node.time_ms, 3), "" if node.loops is None else str(node.loops), remark,
        ])
        if node.facts:
            item.setToolTip(0, "\n".join(f"{key}: {value}" for key, value in node.facts.items()))
        if node.flags:
            for column in range(len(COLUMNS)):
                item.setBackground(column, QBrush(HIGHLIGHT))
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
        for child in node.children:
            self._add_node(item, child)


def show_explain_plan(parent, facade, connection_id: str, sql: str, timeout_ms: Optional[int] = None):
    """SQL 에디터 등에서 한 줄로 여는 진입점. 대화상자를 반환한다(호출자가 show/exec)."""
    return ExplainPlanDialog(parent, facade, connection_id, sql, timeout_ms)
