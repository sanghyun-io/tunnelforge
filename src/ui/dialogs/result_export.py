"""SQL editor: save query results to CSV / JSON Lines files.

Two distinct actions (see `docs/query_results_export.md`):
- displayed rows: what the grid already holds (bounded by the editor's result limit)
- full result: the query is re-run on its own connection and the Rust core streams every row
  straight to the file (no row limit, constant memory, cancellable)
"""
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel,
    QMessageBox, QVBoxLayout,
)

from src.core.query_limits import CANCEL_ERROR_CODES, ERROR_QUERY_TIMEOUT, new_job_id
from src.core.result_file_writer import FORMAT_CSV, FORMAT_JSONL, write_result_file
from src.core.sql_query_classifier import _leading_tokens

logger = logging.getLogger(__name__)

_FILTER_CSV = "CSV (*.csv)"
_FILTER_JSONL = "JSON Lines (*.jsonl)"


_DATA_CHANGING_WORD = re.compile(r"\b(insert|update|delete|merge)\b", re.IGNORECASE)


def is_export_safe_query(sql: str) -> bool:
    """UI hint only: True for plain read queries (SELECT/TABLE/VALUES/SHOW/DESCRIBE, WITH without
    data-changing CTEs, EXPLAIN without ANALYZE). The safety guarantee is the Rust core, which
    runs every file export in a server-enforced read-only transaction and rolls it back."""
    tokens = _leading_tokens(sql or "", max_tokens=2)
    if not tokens:
        return False
    keyword = tokens[0]
    if keyword in ("select", "table", "values", "show", "describe", "desc"):
        return True
    if keyword == "with":
        return not _DATA_CHANGING_WORD.search(sql)
    if keyword == "explain":
        return not re.search(r"\banalyze\b", sql[:200], re.IGNORECASE)
    return False


class ResultExportOptionsDialog(QDialog):
    """Format-specific options; defaults favour safe, Excel-friendly output."""

    def __init__(self, parent, file_format: str, full_result: bool):
        super().__init__(parent)
        self.setWindowTitle("결과 저장 옵션")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.chk_bom = QCheckBox("Excel용 UTF-8 BOM 추가")
        self.chk_bom.setToolTip("Excel에서 한글이 깨지지 않게 합니다. 다른 도구에서 첫 글자가 이상하면 끄세요")
        self.chk_bom.setChecked(True)
        self.chk_guard = QCheckBox("수식 주입 방지 (=, +, -, @로 시작하는 셀 앞에 ' 추가)")
        self.chk_guard.setToolTip("스프레드시트가 셀 내용을 수식으로 실행하는 것을 막습니다. 숫자는 바뀌지 않습니다")
        self.chk_guard.setChecked(True)
        self.combo_binary = QComboBox()
        self.combo_binary.addItem("hex", "hex")
        self.combo_binary.addItem("base64", "base64")
        form.addRow(self.chk_bom)
        form.addRow(self.chk_guard)
        if file_format == FORMAT_JSONL:
            self.chk_bom.setChecked(False)
            self.chk_bom.setEnabled(False)
            self.chk_guard.setChecked(False)
            self.chk_guard.setEnabled(False)
        if full_result:
            form.addRow("바이너리 컬럼 인코딩", self.combo_binary)
        layout.addLayout(form)
        layout.addWidget(QLabel("NULL은 빈 값, 빈 문자열은 \"\"로 저장됩니다"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> Dict[str, Any]:
        return {
            "bom": self.chk_bom.isChecked(),
            "formula_guard": self.chk_guard.isChecked(),
            "binary": self.combo_binary.currentData(),
        }


class SQLResultExportWorker(QThread):
    """Re-runs one query and streams its full result into a file inside the Rust core."""

    progress = pyqtSignal(int, int)  # rows written, bytes written
    finished = pyqtSignal(bool, str)

    def __init__(self, params, sql: str, output: Dict[str, Any], timeout_ms: Optional[int] = None):
        super().__init__()
        self.params = params  # sql_editor_workers.ConnectionParams
        self.sql = sql
        self.output = output
        self.timeout_ms = timeout_ms
        self._connector = None

    def cancel_query(self):
        """UI thread: cancel on the server; the core removes (or keeps, if asked) the partial file."""
        self.requestInterruption()
        connection = getattr(self._connector, "connection", None)
        cancel = getattr(connection, "cancel_running_query", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                logger.warning("export cancel request failed", exc_info=True)

    def run(self):
        from src.ui.dialogs.sql_editor_workers import connector_from_params

        connector = None
        started = time.time()
        try:
            connector = connector_from_params(self.params)
            success, message = connector.connect()
            if not success:
                self.finished.emit(False, f"연결 실패: {message}")
                return
            self._connector = connector
            connection = connector.connection
            job_id = new_job_id("export")
            connection.current_job_id = job_id
            try:
                result = connection.facade.export_query_to_file(
                    connection.connection_id,
                    self.sql,
                    self.output,
                    job_id=job_id,
                    timeout_ms=self.timeout_ms,
                    on_progress=lambda rows, size: self.progress.emit(rows, size),
                )
            finally:
                connection.current_job_id = None
            rows = int(result.get("rows_written") or 0)
            size = int(result.get("bytes_written") or 0)
            self.finished.emit(
                True,
                f"✅ 전체 결과 저장 완료: {rows:,}행, {size / (1024 * 1024):.1f} MiB, "
                f"{time.time() - started:.1f}초\n   {result.get('output_path')}",
            )
        except Exception as exc:
            self.finished.emit(False, describe_export_failure(exc, self.output))
        finally:
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    logger.debug("export connection cleanup failed", exc_info=True)


def describe_export_failure(exc: Exception, output: Dict[str, Any]) -> str:
    """A failed export must never read like a finished one."""
    payload = getattr(exc, "payload", None) or {}
    code = getattr(exc, "error_code", None)
    if code in CANCEL_ERROR_CODES:
        head = "⏱ 제한시간 초과로 중단" if code == ERROR_QUERY_TIMEOUT else "⏹ 사용자가 취소"
    elif code == "export_requires_read_only":
        head = "🔒 이 쿼리는 데이터를 변경하므로 저장하지 않았습니다 (읽기 전용 트랜잭션에서 거부됨, 변경 없음)"
    elif code == "export_session_in_transaction":
        head = f"❌ 저장 실패: 열린 트랜잭션이 있는 연결에서는 실행할 수 없습니다 ({exc})"
    else:
        head = f"❌ 저장 실패: {exc}"
    rows = int(payload.get("rows_written") or 0)
    partial = payload.get("partial_path")
    if partial:
        tail = f"불완전한 파일({rows:,}행)이 남아 있습니다 - 사용하지 마세요: {partial}"
    else:
        tail = "저장 파일이 만들어지지 않았습니다 (부분 파일은 삭제됨)"
    return f"{head}\n   {tail}"


class ResultExportMixin:
    """Methods mixed into SQLEditorDialog."""

    def _prompt_result_export(self, full_result: bool) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        path, selected = QFileDialog.getSaveFileName(
            self, "결과 파일 저장", "result.csv", f"{_FILTER_CSV};;{_FILTER_JSONL}"
        )
        if not path:
            return None
        lowered = path.lower()
        if lowered.endswith((".jsonl", ".json")) or (selected == _FILTER_JSONL and "." not in os.path.basename(path)):
            file_format = FORMAT_JSONL
            if "." not in os.path.basename(path):
                path += ".jsonl"
        else:
            file_format = FORMAT_CSV
            if "." not in os.path.basename(path):
                path += ".csv"
        dialog = ResultExportOptionsDialog(self, file_format, full_result)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return path, file_format, dialog.options()

    def _save_displayed_result(self, table) -> None:
        """Save the rows the grid already holds (bounded by the editor's result limit)."""
        if getattr(table, "_streaming", False) is True:
            QMessageBox.warning(self, "경고", "결과를 받는 중에는 저장할 수 없습니다.")
            return
        columns = getattr(table, "_export_columns", None)
        rows = getattr(table, "_export_rows", None)
        if columns is None or rows is None:
            QMessageBox.warning(self, "경고", "저장할 결과가 없습니다.")
            return
        picked = self._prompt_result_export(full_result=False)
        if picked is None:
            return
        path, file_format, options = picked
        try:
            count = write_result_file(
                path, columns, rows, file_format,
                bom=options["bom"], formula_guard=options["formula_guard"],
            )
        except OSError as exc:
            QMessageBox.warning(self, "경고", f"파일 저장 실패: {exc}")
            return
        self.message_text.append(f"💾 표시된 결과 {count:,}행 저장: {path}")
        self.status_bar.showMessage(f"💾 표시된 결과 {count:,}행을 저장했습니다")

    def _export_result_full(self, table) -> None:
        """Re-run the result's query and stream every row to a file through the Rust core."""
        from src.ui.dialogs.sql_editor_workers import ConnectionParams

        if getattr(self, "_query_executing", False):
            QMessageBox.warning(self, "경고", "쿼리 실행 중에는 저장할 수 없습니다.")
            return
        sql = (getattr(table, "_source_query", "") or "").strip()
        if not sql:
            QMessageBox.warning(self, "경고", "이 결과의 원본 쿼리를 알 수 없습니다.")
            return
        if not is_export_safe_query(sql):
            QMessageBox.warning(
                self, "경고",
                "데이터를 변경할 수 있는 쿼리는 다시 실행해 파일로 저장할 수 없습니다. "
                "'표시된 결과 저장'을 사용하세요.",
            )
            return
        db_user, db_password = self._db_credentials()
        if not db_user:
            QMessageBox.warning(self, "경고", "DB 자격 증명이 설정되지 않았습니다.")
            return
        picked = self._prompt_result_export(full_result=True)
        if picked is None:
            return
        path, file_format, options = picked

        host, port, temp_server, error = self._resolve_db_target(
            allow_temp_tunnel=True, keep_temp_tunnel=True, log_temp_tunnel=True,
        )
        if error:
            self.message_text.append(f"❌ {error}")
            return
        self._autocommit_temp_server = temp_server
        selected = self.db_combo.currentText().strip()
        database, schema = self._database_and_schema_for_selection(selected)
        engine = self._db_engine()
        params = ConnectionParams(engine, host, port, db_user, db_password, database, schema,
                                  read_only=self._session_read_only())
        output = {
            "path": path,
            "format": file_format,
            "bom": options["bom"],
            "formula_guard": options["formula_guard"],
            "binary": options["binary"],
            "overwrite": True,  # the save dialog already asked for confirmation
            "keep_partial": False,
        }
        timeout_ms = self._query_limits().get("timeout_ms")

        self._set_executing_state(True)
        self.message_text.append(f"\n{'─' * 40}")
        self.message_text.append(f"💾 전체 결과를 파일로 저장 (읽기 전용 트랜잭션으로 쿼리 재실행): {path}")
        self.worker = SQLResultExportWorker(params, sql, output, timeout_ms=timeout_ms)
        self.worker.progress.connect(self._on_export_progress)
        self.worker.finished.connect(self._on_export_finished)
        self.worker.start()

    def _on_export_progress(self, rows: int, size: int) -> None:
        self.status_bar.showMessage(f"💾 파일로 저장 중... {rows:,}행, {size / (1024 * 1024):.1f} MiB")

    def _on_export_finished(self, success: bool, message: str) -> None:
        self.message_text.append(message)
        self._set_message_summary(message.splitlines()[0])
        self._cleanup()
        self.status_bar.showMessage(message.splitlines()[0])
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None
