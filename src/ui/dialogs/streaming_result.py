"""SQL editor: result grid that fills while rows arrive (TF-STATUS-131).

The worker thread streams row batches; each batch becomes one bounded GUI update (a single
`setRowCount` growth plus its cells) so the event loop stays responsive for 100k+ rows. Sorting,
cell editing and "save" actions stay off until the result is complete.
"""
import gc
import threading
import time
import logging
import weakref
from collections import deque
from typing import Any, List, Optional, Sequence

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem

logger = logging.getLogger(__name__)
_NULL_COLOR = QColor("#888888")


class GcPause:
    """Process-wide, reference-counted pause of Python's cyclic GC.

    With ~600k table items a generation-2 pass stalls the GUI for 100+ ms (measured), and table
    items are not garbage cycles. Every grid that fills holds one handle; the collector is
    re-enabled only when the last handle is released, and only if it was enabled before the first
    one (a GC that was already disabled stays disabled). `release()` is idempotent.
    """

    _lock = threading.Lock()
    _holds = 0
    _was_enabled = True

    def __init__(self):
        self._released = False

    @classmethod
    def acquire(cls) -> "GcPause":
        with cls._lock:
            if cls._holds == 0:
                cls._was_enabled = gc.isenabled()
                gc.disable()
            cls._holds += 1
        return cls()

    def release(self) -> None:
        cls = type(self)
        with cls._lock:
            if self._released:
                return
            self._released = True
            cls._holds -= 1
            if cls._holds == 0 and cls._was_enabled:
                gc.enable()
# One GUI slice: batches are appended until this much time has passed, then the event loop runs
# again (timers, repaints, input) before the next slice.
FLUSH_BUDGET_SECONDS = 0.025


def append_result_rows(table: QTableWidget, rows: Sequence[Sequence[Any]]) -> None:
    """Append one batch to the grid and to `table._export_rows` (the rows the user can save)."""
    if not rows:
        return
    start = table.rowCount()
    table.setUpdatesEnabled(False)
    try:
        table.setRowCount(start + len(rows))
        for offset, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QTableWidgetItem("NULL" if value is None else str(value))
                if value is None:
                    item.setForeground(_NULL_COLOR)
                table.setItem(start + offset, column, item)
    finally:
        table.setUpdatesEnabled(True)
    table._export_rows.extend(rows)


def fit_columns(table: QTableWidget, max_width: int) -> None:
    """Size columns to the rows received so far, capped like a finished result."""
    table.resizeColumnsToContents()
    header = table.horizontalHeader()
    for column in range(table.columnCount()):
        if header.sectionSize(column) > max_width:
            header.resizeSection(column, max_width)


class StreamingResultMixin:
    """Methods mixed into SQLEditorDialog."""

    _streamed_tables = None

    def _hold_gc(self, table) -> None:
        # The handle is released on completion/cancel/error/close; weakref.finalize is the safety
        # net for a grid that is destroyed (window closed) without being finished.
        table._gc_hold = GcPause.acquire()
        weakref.finalize(table, table._gc_hold.release)

    def _release_gc(self, table) -> None:
        hold = getattr(table, "_gc_hold", None)
        if hold is not None:
            hold.release()

    def _streams(self) -> dict:
        if self._streamed_tables is None:
            self._streamed_tables = {}
        return self._streamed_tables

    def _stream_query_text(self, idx: int) -> str:
        worker = getattr(self, "worker", None)
        try:
            return str(worker.queries[idx])
        except (AttributeError, IndexError, TypeError):
            return ""

    # ------------------------------------------------------------------ slots

    def _on_result_started(self, idx: int, columns: List[str]) -> None:
        table = self._add_result_table(list(columns), [], 0, self._stream_query_text(idx), finalize=False)
        table._streaming = True
        table._truncated = False
        table._pending = deque()
        table._flush_scheduled = False
        self._hold_gc(table)
        table._result_number = self._result_counter
        self._streams()[idx] = table
        self._set_stream_tab_text(table)

    def _on_result_rows(self, idx: int, rows: list) -> None:
        table = self._streams().get(idx)
        if table is None or not rows:
            return
        # Queue only; appending happens in time-boxed slices so a burst of batches cannot starve
        # the event loop.
        table._pending.append(rows)
        if not table._flush_scheduled:
            table._flush_scheduled = True
            QTimer.singleShot(0, lambda t=table: self._flush_stream(t))

    def _flush_stream(self, table, budget: float = FLUSH_BUDGET_SECONDS) -> None:
        """Append queued batches for up to `budget` seconds; reschedule if more remain."""
        table._flush_scheduled = False
        deadline = time.monotonic() + budget
        while table._pending and time.monotonic() < deadline:
            first_rows = table.rowCount() == 0
            append_result_rows(table, table._pending.popleft())
            if first_rows:
                from src.ui.dialogs.sql_editor_dialog import MAX_AUTO_COLUMN_WIDTH_PX
                fit_columns(table, MAX_AUTO_COLUMN_WIDTH_PX)
        self._set_stream_tab_text(table)
        if table._pending and not table._flush_scheduled:
            table._flush_scheduled = True
            QTimer.singleShot(0, lambda t=table: self._flush_stream(t))

    def _drain_stream(self, table) -> None:
        """Append everything still queued, letting the event loop breathe between slices."""
        while table._pending:
            self._flush_stream(table)
            table._flush_scheduled = False
            if table._pending:
                QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def _mark_stream_truncated(self, idx: int) -> None:
        table = self._streams().get(idx)
        if table is not None:
            table._truncated = True

    # ------------------------------------------------------------------ completion

    def _set_stream_tab_text(self, table, state: str = "") -> None:
        index = self.result_tabs.indexOf(table)
        if index < 0:
            return
        count = table.rowCount()
        if state:
            detail = f"{count}행, {state}"
        elif getattr(table, "_streaming", False):
            detail = f"{count:,}행 수신 중..."
        else:
            detail = f"{count}행" + (", 잘림" if getattr(table, "_truncated", False) else "")
        self.result_tabs.setTabText(index, f"결과 {getattr(table, '_result_number', '')} ({detail})")

    def _finalize_streamed_result(self, idx: int, error: str = "") -> Optional[int]:
        """Finish the grid of query `idx`. Returns its row count, or None if it was not streamed."""
        table = self._streams().pop(idx, None)
        if table is None:
            return None
        try:
            self._drain_stream(table)
            table._streaming = False
        finally:
            self._release_gc(table)
        count = table.rowCount()
        if error:
            state = "취소됨" if "취소" in error else "오류로 중단됨"
            self._set_stream_tab_text(table, state)
            return count
        self._set_stream_tab_text(table)
        from src.ui.dialogs.sql_editor_dialog import MAX_AUTO_COLUMN_WIDTH_PX
        fit_columns(table, MAX_AUTO_COLUMN_WIDTH_PX)
        # Editability needs the complete result (primary keys, pending-edit bookkeeping).
        self._setup_result_table_editability(
            table, table._source_query, table._export_columns, table._export_rows
        )
        return count

    def _finalize_all_streamed(self) -> None:
        """The worker ended: whatever is still open was interrupted."""
        for idx in list(self._streams()):
            try:
                self._finalize_streamed_result(idx, error="중단됨")
            except Exception:
                logger.exception("streamed result cleanup failed")
