"""Result grid that fills while rows arrive + worker streaming signals (TF-STATUS-131)."""
import gc
import os
import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QTableWidget

from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog
from src.ui.dialogs.sql_editor_workers import SQLQueryWorker, SQLTransactionExecutionWorker
from src.ui.dialogs.streaming_result import append_result_rows

_app = QApplication.instance() or QApplication([])
WARMUP_ROWS = 5_000


def _dialog(monkeypatch):
    monkeypatch.setattr(SQLEditorDialog, "refresh_databases", lambda self: None)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("u", "p")
    dialog = SQLEditorDialog(
        None,
        {"id": "t", "name": "n", "connection_mode": "direct", "environment": "development",
         "remote_host": "127.0.0.1", "remote_port": 3306},
        config_manager, MagicMock(),
    )
    dialog.worker = MagicMock()
    dialog.worker.isRunning.return_value = False  # a truthy MagicMock would make closeEvent open a modal
    dialog.worker.queries = ["SELECT id, v FROM t"]
    dialog.history_manager = MagicMock()
    return dialog


def _close(dialog):
    for index in range(dialog.editor_tabs.count()):
        tab = dialog.editor_tabs.widget(index)
        if tab:
            tab.is_modified = False
    dialog.close()


def test_append_result_rows_extends_grid_and_export_rows():
    table = QTableWidget()
    table.setColumnCount(2)
    table._export_rows = []
    append_result_rows(table, [["1", None], ["2", "x"]])
    append_result_rows(table, [["3", ""]])
    assert table.rowCount() == 3 and len(table._export_rows) == 3
    assert table.item(0, 1).text() == "NULL" and table.item(2, 1).text() == ""
    assert table.item(1, 1).text() == "x"


def test_rows_appear_per_batch_and_editing_waits_for_completion(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        setups = []
        monkeypatch.setattr(dialog, "_setup_result_table_editability",
                            lambda table, query, columns, rows: setups.append((query, list(columns), len(rows))))
        dialog._on_result_started(0, ["id", "v"])
        table = dialog.result_tabs.widget(0)
        assert dialog.result_tabs.count() == 1 and table.rowCount() == 0
        assert "수신 중" in dialog.result_tabs.tabText(0)

        dialog._on_result_rows(0, [["1", "a"], ["2", "b"]])
        dialog._drain_stream(table)
        assert table.rowCount() == 2 and setups == [], "no editing while the result is incomplete"
        dialog._on_result_rows(0, [["3", None]])
        dialog._drain_stream(table)
        assert table.rowCount() == 3 and "3행 수신 중" in dialog.result_tabs.tabText(0)

        dialog._on_query_result(0, True, ["id", "v"], [], "", 3, 0.1)
        assert dialog.result_tabs.count() == 1, "the streamed grid is reused, not duplicated"
        assert dialog.result_tabs.tabText(0) == "결과 1 (3행)"
        assert setups == [("SELECT id, v FROM t", ["id", "v"], 3)]
        assert dialog.history_manager.add_query.call_args.args[2] == 3
        assert "3행 반환" in dialog.message_text.toPlainText()
    finally:
        _close(dialog)


def test_cancelled_stream_keeps_received_rows_and_says_so(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        setups = []
        monkeypatch.setattr(dialog, "_setup_result_table_editability", lambda *a: setups.append(a))
        dialog._on_result_started(0, ["id"])
        dialog._on_result_rows(0, [["1"], ["2"]])
        dialog._on_query_result(0, True, [], [], "쿼리가 사용자에 의해 취소되었습니다", 0, 0.2)
        table = dialog.result_tabs.widget(0)
        assert table.rowCount() == 2
        assert "취소됨" in dialog.result_tabs.tabText(0)
        assert setups == [], "an interrupted result is not editable"
        assert "2행을 받았습니다" in dialog.message_text.toPlainText()
    finally:
        _close(dialog)


def test_truncated_stream_is_marked_on_the_tab(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        monkeypatch.setattr(dialog, "_setup_result_table_editability", lambda *a: None)
        dialog._on_result_started(0, ["id"])
        dialog._on_result_rows(0, [["1"]])
        dialog._on_result_truncated(0, "결과가 잘렸습니다")
        dialog._on_query_result(0, True, ["id"], [], "", 1, 0.1)
        assert "잘림" in dialog.result_tabs.tabText(0)
    finally:
        _close(dialog)


def test_worker_that_never_streams_still_creates_the_result_tab(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        monkeypatch.setattr(dialog, "_setup_result_table_editability", lambda *a: None)
        dialog._on_query_result(0, True, ["id"], [["1"], ["2"]], "", 2, 0.1)
        assert dialog.result_tabs.count() == 1 and dialog.result_tabs.widget(0).rowCount() == 2
    finally:
        _close(dialog)


def test_interrupted_worker_closes_open_grids(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        dialog._on_result_started(0, ["id"])
        dialog._on_result_rows(0, [["1"]])
        dialog._exec_start_time = None
        dialog._on_finished(False, "중단")
        assert "중단됨" in dialog.result_tabs.tabText(0)
        assert dialog.result_tabs.widget(0)._streaming is False
    finally:
        dialog._finalize_all_streamed()
        gc.enable()
        _close(dialog)


# ------------------------------------------------------------------ workers


def _fake_streaming_connection(columns, batches, with_columns_event=True):
    connection = MagicMock()
    connection.connection_id = "c1"

    def stream(connection_id, query, row_batch_size=500, on_batch=None, on_columns=None, **kwargs):
        if with_columns_event and on_columns:
            on_columns(columns)
        for batch in batches:
            on_batch(batch)
        return {"success": True, "columns": columns, "truncated": False}

    connection.facade.execute_on_connection_streaming.side_effect = stream
    return connection


def test_autocommit_worker_emits_started_rows_and_a_rowless_final_result(monkeypatch):
    from src.ui.dialogs import sql_editor_workers as module

    connection = _fake_streaming_connection(["id", "v"], [[{"id": 1, "v": "a"}, {"id": 2, "v": None}], [{"id": 3, "v": "c"}]])
    connector = MagicMock()
    connector.connect.return_value = (True, "")
    connector.connection = connection
    monkeypatch.setattr(module, "create_sql_editor_connector", lambda *a, **k: connector)
    worker = SQLQueryWorker("h", 1, "u", "p", "d", ["SELECT id, v FROM t"])
    started, batches, finals, progress = [], [], [], []
    worker.result_started.connect(lambda idx, cols: started.append((idx, cols)))
    worker.result_rows.connect(lambda idx, rows: batches.append(rows))
    worker.query_result.connect(lambda *args: finals.append(args))
    worker.rows_progress.connect(lambda idx, n: progress.append(n))
    worker.run()
    assert started == [(0, ["id", "v"])]
    assert batches == [[[1, "a"], [2, None]], [[3, "c"]]]
    assert progress == [2, 3]
    idx, returns_rows, columns, rows, error, affected, _ = finals[0]
    assert (returns_rows, columns, rows, error, affected) == (True, ["id", "v"], [], "", 3)


def test_worker_derives_columns_from_the_first_batch_when_no_columns_event():
    connection = _fake_streaming_connection(["id"], [[{"id": 1}]], with_columns_event=False)
    worker = SQLTransactionExecutionWorker(connection, ["SELECT id FROM t"], "mysql")
    started, batches, finals = [], [], []
    worker.result_started.connect(lambda idx, cols: started.append(cols))
    worker.result_rows.connect(lambda idx, rows: batches.append(rows))
    worker.query_result.connect(lambda *args: finals.append(args))
    worker.run()
    assert started == [["id"]] and batches == [[[1]]]
    assert finals[0][2] is True and finals[0][4] == [] and finals[0][6] == 1


# ------------------------------------------------------------------ responsiveness


class _Producer(QThread):
    batch = pyqtSignal(int, list)

    def __init__(self, batches, rows_per_batch, columns, pause_ms=0):
        super().__init__()
        self._pause_ms = pause_ms
        self._row = [str(i) * 3 for i in range(columns)]
        self._batches = batches
        self._rows = rows_per_batch

    def run(self):
        for _ in range(self._batches):
            self.batch.emit(0, [list(self._row) for _ in range(self._rows)])
            if self._pause_ms:
                self.msleep(self._pause_ms)


def test_event_loop_stays_responsive_while_100k_rows_arrive(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        monkeypatch.setattr(dialog, "_setup_result_table_editability", lambda *a: None)
        dialog._on_result_started(0, [f"c{i}" for i in range(6)])
        gaps = []
        last = [time.monotonic()]
        table = dialog.result_tabs.widget(0)

        def tick():
            now = time.monotonic()
            # Warm-up: the first batches pay one-time costs (allocator, style, first paint).
            if table.rowCount() >= WARMUP_ROWS:
                gaps.append(now - last[0])
            last[0] = now

        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(tick)
        timer.start()
        # 100,000 rows at ~100k rows/s (one 500-row batch per 5 ms), faster than the core delivers
        producer = _Producer(batches=200, rows_per_batch=500, columns=6, pause_ms=5)
        producer.batch.connect(dialog._on_result_rows)
        producer.start()
        deadline = time.monotonic() + 120
        while table.rowCount() < 100_000 and time.monotonic() < deadline:
            QCoreApplication.processEvents()
        producer.wait()
        timer.stop()
        assert table.rowCount() == 100_000
        assert gc.isenabled() is False, "GC is paused while the grid fills"
        dialog._finalize_streamed_result(0)
        assert gc.isenabled() is True, "GC is resumed when the result is complete"
        ordered = sorted(gaps)
        p95 = ordered[int(len(ordered) * 0.95)]
        worst = ordered[-1]
        print(f"100k rows: {len(gaps)} timer ticks, p95 {p95 * 1000:.0f} ms, worst {worst * 1000:.0f} ms, "
              f"top5 {[round(g * 1000) for g in ordered[-5:]]}")
        # Shared CI runners add scheduler/timer jitter (a docs-only PR measured 109 ms), so the single
        # worst gap gets a looser bound there; p95 keeps catching a loop that really starves.
        max_allowed = 0.3 if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS") else 0.1
        assert p95 < 0.1, f"GUI event loop p95 gap {p95 * 1000:.0f} ms while rows arrived"
        assert worst < max_allowed, f"GUI event loop stalled {worst * 1000:.0f} ms while rows arrived"
    finally:
        _close(dialog)
