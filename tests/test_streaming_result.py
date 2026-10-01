"""Result grid that fills while rows arrive + worker streaming signals (TF-STATUS-131)."""
import gc
import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QTableWidget

from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog
from src.ui.dialogs.sql_editor_workers import SQLQueryWorker, SQLTransactionExecutionWorker
from src.ui.dialogs.streaming_result import append_result_rows

_app = QApplication.instance() or QApplication([])


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

        def tick():
            now = time.monotonic()
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
        table = dialog.result_tabs.widget(0)
        while table.rowCount() < 100_000 and time.monotonic() < deadline:
            QCoreApplication.processEvents()
        producer.wait()
        timer.stop()
        assert table.rowCount() == 100_000
        assert gc.isenabled() is False, "GC is paused while the grid fills"
        dialog._finalize_streamed_result(0)
        assert gc.isenabled() is True, "GC is resumed when the result is complete"
        worst = max(gaps)
        top = sorted(gaps, reverse=True)[:5]
        print(f"100k rows: {len(gaps)} timer ticks, worst event-loop gap {worst * 1000:.0f} ms, top5 {[round(g * 1000) for g in top]}")
        assert worst < 0.1, f"GUI event loop stalled {worst * 1000:.0f} ms while rows arrived"
    finally:
        _close(dialog)


# ------------------------------------------------------------------ worker-side backpressure


def test_stream_flow_blocks_over_the_limit_until_the_gui_consumes():
    import threading

    from src.ui.dialogs.sql_editor_workers import StreamFlow

    flow = StreamFlow(limit_rows=1000)
    flow.produced(1000)  # at the limit: no wait
    released = threading.Event()

    def producer():
        flow.produced(500)  # 1500 > 1000: waits
        released.set()

    thread = threading.Thread(target=producer)
    thread.start()
    assert not released.wait(0.3), "producer must wait while the GUI is behind"
    flow.consumed(600)
    assert released.wait(2), "producer continues once the GUI caught up"
    thread.join()
    flow.consumed(10**6)
    assert flow.pending == 0, "pending never goes negative"


def test_stream_flow_wait_ends_on_stop_or_stall():
    from src.ui.dialogs.sql_editor_workers import StreamFlow

    flow = StreamFlow(limit_rows=10)
    stopped = {"value": False}
    started = time.monotonic()
    flow.produced(100, should_stop=lambda: True)  # cancel: returns at once
    assert time.monotonic() - started < 0.5
    stalled = StreamFlow(limit_rows=10, stall_seconds=0.2)
    started = time.monotonic()
    stalled.produced(100)  # nobody consumes: gives up after the stall window
    assert 0.15 < time.monotonic() - started < 2
    assert stopped["value"] is False


def test_throttled_stream_keeps_every_row_in_order_and_bounds_the_backlog():
    import threading

    from src.ui.dialogs.sql_editor_workers import StreamFlow, run_streaming_query

    batches = [[{"n": b * 500 + i} for i in range(500)] for b in range(40)]
    connection = _fake_streaming_connection(["n"], batches)
    flow = StreamFlow(limit_rows=1500)
    received, peak = [], [0]
    lock = threading.Lock()

    def on_rows(rows):
        with lock:
            received.append(rows)
            peak[0] = max(peak[0], flow.pending + len(rows))

    def consumer():  # a slow GUI
        done = 0
        while done < 20_000:
            time.sleep(0.002)
            with lock:
                ready = sum(len(r) for r in received)
            if ready > done:
                flow.consumed(ready - done)
                done = ready

    thread = threading.Thread(target=consumer)
    thread.start()
    columns, count, _ = run_streaming_query(
        connection, "SELECT n", {}, on_started=lambda c: None, on_rows=on_rows, flow=flow
    )
    thread.join(10)
    assert count == 20_000
    flat = [row[0] for batch in received for row in batch]
    assert flat == list(range(20_000)), "no row lost or reordered"
    assert peak[0] <= 1500 + 500, f"backlog grew to {peak[0]} rows"


def test_cancel_stops_forwarding_without_blocking():
    from src.ui.dialogs.sql_editor_workers import StreamFlow, run_streaming_query

    batches = [[{"n": i}] for i in range(50)]
    connection = _fake_streaming_connection(["n"], batches)
    seen = []
    flow = StreamFlow(limit_rows=1000)
    started = time.monotonic()
    run_streaming_query(
        connection, "SELECT n", {}, on_started=lambda c: None, on_rows=seen.append, flow=flow,
        should_stop=lambda: len(seen) >= 3,
    )
    assert len(seen) == 3 and time.monotonic() - started < 2, "forwarding ends the moment cancel is requested"


def _burst_run(monkeypatch, flow_limit):
    """A core that delivers 100k rows as fast as it can into a real worker + dialog."""
    from src.ui.dialogs import sql_editor_workers as module
    from src.ui.dialogs.sql_editor_workers import StreamFlow

    dialog = _dialog(monkeypatch)
    monkeypatch.setattr(dialog, "_setup_result_table_editability", lambda *a: None)
    batches = [
        [{f"c{c}": (b * 500 + i if c == 0 else f"v{c}") for c in range(6)} for i in range(500)]
        for b in range(200)
    ]
    connection = _fake_streaming_connection([f"c{c}" for c in range(6)], batches)
    connector = MagicMock()
    connector.connect.return_value = (True, "")
    connector.connection = connection
    monkeypatch.setattr(module, "create_sql_editor_connector", lambda *a, **k: connector)
    worker = SQLQueryWorker("h", 1, "u", "p", "d", ["SELECT * FROM t"])
    worker.stream_flow = StreamFlow(limit_rows=flow_limit)
    dialog.worker = worker
    worker.result_started.connect(dialog._on_result_started)
    worker.result_rows.connect(dialog._on_result_rows)
    worker.query_result.connect(dialog._on_query_result)
    worker.rows_progress.connect(dialog._on_rows_progress)
    done = []
    worker.finished.connect(lambda ok, msg: done.append(ok))
    dialog._exec_start_time = None

    gaps, completion_gaps, last = [], [], [time.monotonic()]

    marks = []

    def tick():
        now = time.monotonic()
        rows = dialog.result_tabs.widget(0).rowCount() if dialog.result_tabs.count() else 0
        # Gaps while rows are still arriving; the last tick belongs to completion (GC resume).
        (gaps if rows < 100_000 else completion_gaps).append(now - last[0])
        if now - last[0] > 0.06:
            marks.append((round((now - last[0]) * 1000), rows))
        last[0] = now

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    timer.start()
    worker.start()
    deadline = time.monotonic() + 120
    while not done and time.monotonic() < deadline:
        QCoreApplication.processEvents()
    worker.wait()
    QCoreApplication.processEvents()
    timer.stop()
    print('slow ticks (ms, rows):', marks)
    return dialog, gaps, completion_gaps


def test_burst_of_200_batches_keeps_the_gui_responsive_and_loses_nothing(monkeypatch):
    gc.enable()
    dialog, gaps, completion_gaps = _burst_run(monkeypatch, flow_limit=5000)
    try:
        table = dialog.result_tabs.widget(0)
        assert table.rowCount() == 100_000
        assert [table.item(r, 0).text() for r in (0, 1, 49_999, 99_999)] == ["0", "1", "49999", "99999"]
        assert [row[0] for row in table._export_rows] == list(range(100_000)), "order preserved"
        assert gc.isenabled() is True
        worst = max(gaps)
        done = max(completion_gaps, default=0)
        print(f"burst: worst gap while receiving {worst * 1000:.0f} ms over {len(gaps)} ticks, "
              f"completion {done * 1000:.0f} ms")
        assert worst < 0.1, f"event loop stalled {worst * 1000:.0f} ms during a burst"
        assert done < 0.3, f"completing the grid stalled {done * 1000:.0f} ms"
    finally:
        gc.enable()
        _close(dialog)
