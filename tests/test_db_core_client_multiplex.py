"""Multiplexed DbCoreServiceClient: one reader thread routes events by request_id (TF-STATUS-112)."""
import io
import json
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

from src.core.db_core_client import DbCoreServiceClient, DbCoreServiceError
from src.core.db_core_dbapi_shim import RustDbConnection
from src.core.db_core_facade import DbCoreFacade, DbEndpoint


class _Stdout:
    def __init__(self):
        self.lines: "queue.Queue[str]" = queue.Queue()

    def readline(self):
        return self.lines.get()

    def feed(self, **event):
        self.lines.put(json.dumps(event) + "\n")

    def eof(self):
        self.lines.put("")


class _Stdin:
    def __init__(self):
        self.requests: "queue.Queue[dict]" = queue.Queue()

    def write(self, text):
        for line in text.splitlines():
            self.requests.put(json.loads(line))

    def flush(self):
        pass


class _Process:
    def __init__(self, stderr_text=""):
        self.stdin = _Stdin()
        self.stdout = _Stdout()
        self.stderr = io.StringIO(stderr_text)
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True
        self.stdout.eof()


@pytest.fixture
def core():
    process = _Process()
    client = DbCoreServiceClient(executable="fake-core", popen_factory=lambda *a, **k: process)
    yield client, process
    process.stdout.eof()
    reader = client._reader_thread
    if reader is not None:
        reader.join(timeout=2)
        assert not reader.is_alive(), "reader thread must not outlive the test"


def _in_thread(fn, *args, **kwargs):
    box = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the asserting test
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    box["thread"] = thread
    return box


def test_concurrent_requests_receive_only_their_own_events(core):
    client, process = core
    seen_a, seen_b = [], []
    a = _in_thread(client.request, "query.execute", {"n": 1}, request_id="A", on_event=seen_a.append)
    b = _in_thread(client.request, "query.execute", {"n": 2}, request_id="B", on_event=seen_b.append)
    process.stdin.requests.get(timeout=2)
    process.stdin.requests.get(timeout=2)

    process.stdout.feed(event="row_batch", request_id="B", rows=[{"x": 2}])
    process.stdout.feed(event="row_batch", request_id="A", rows=[{"x": 1}])
    process.stdout.feed(event="result", request_id="B", success=True, tag="b")
    process.stdout.feed(event="result", request_id="A", success=True, tag="a")

    a["thread"].join(2)
    b["thread"].join(2)
    assert a["value"]["tag"] == "a" and b["value"]["tag"] == "b"
    assert [e["rows"] for e in seen_a if e["event"] == "row_batch"] == [[{"x": 1}]]
    assert [e["rows"] for e in seen_b if e["event"] == "row_batch"] == [[{"x": 2}]]


def test_cancel_request_completes_while_query_is_still_running(core):
    client, process = core
    query = _in_thread(client.request, "query.execute", {"job_id": "j1"}, request_id="Q")
    assert process.stdin.requests.get(timeout=2)["request_id"] == "Q"

    cancel = _in_thread(client.request, "query.cancel", {"job_id": "j1"}, request_id="C")
    assert process.stdin.requests.get(timeout=2)["request_id"] == "C"
    process.stdout.feed(event="result", request_id="C", success=True, cancelled=True)
    cancel["thread"].join(2)

    assert cancel["value"]["cancelled"] is True
    assert query["thread"].is_alive(), "the query is still waiting for its own result"
    process.stdout.feed(event="result", request_id="Q", success=False, error_code="query_cancelled")
    query["thread"].join(2)
    assert query["value"]["error_code"] == "query_cancelled"


def test_error_event_carries_error_code(core):
    client, process = core
    box = _in_thread(client.request, "query.execute", {}, request_id="E")
    process.stdin.requests.get(timeout=2)
    process.stdout.feed(event="error", request_id="E", message="busy", error_code="connection_busy")
    box["thread"].join(2)
    assert isinstance(box["error"], DbCoreServiceError)
    assert box["error"].error_code == "connection_busy"


def test_process_death_wakes_every_waiting_request_with_stderr_tail():
    process = _Process(stderr_text="fatal: boom\n")
    client = DbCoreServiceClient(executable="fake-core", popen_factory=lambda *a, **k: process)
    a = _in_thread(client.request, "query.execute", {}, request_id="A")
    b = _in_thread(client.request, "query.execute", {}, request_id="B")
    process.stdin.requests.get(timeout=2)
    process.stdin.requests.get(timeout=2)

    process.stdout.eof()
    a["thread"].join(3)
    b["thread"].join(3)

    for box in (a, b):
        assert isinstance(box["error"], DbCoreServiceError)
        assert "fatal: boom" in str(box["error"])
    client._reader_thread.join(2)
    assert not client._reader_thread.is_alive()


def test_shutdown_still_waits_for_core_acknowledgement(core):
    client, process = core
    client.start()
    box = _in_thread(client.shutdown)
    request = process.stdin.requests.get(timeout=2)
    assert request["command"] == "service.shutdown"
    assert box["thread"].is_alive()
    process.stdout.feed(event="result", request_id=request["request_id"], success=True)
    box["thread"].join(2)
    assert "error" not in box


# ---------------------------------------------------------------- facade / cursor / worker


def test_facade_streaming_sends_limits_and_raises_on_cancel():
    client = MagicMock()
    client.request.return_value = {
        "success": False, "error_code": "query_cancelled", "message": "cancelled", "in_transaction": True,
    }
    facade = DbCoreFacade(client)
    with pytest.raises(DbCoreServiceError) as info:
        facade.execute_on_connection_streaming(
            "c1", "SELECT 1", job_id="j1", timeout_ms=5000, max_rows=10, max_bytes=99,
        )
    assert info.value.error_code == "query_cancelled"
    assert info.value.payload["in_transaction"] is True
    payload = client.request.call_args.args[1]
    assert payload["job_id"] == "j1" and payload["timeout_ms"] == 5000
    assert payload["max_rows"] == 10 and payload["max_bytes"] == 99 and payload["stream_rows"] is True


def test_facade_result_reports_truncation_and_cancel_query_uses_job_id():
    client = MagicMock()
    client.request.return_value = {
        "success": True, "rows": [{"a": 1}], "columns": ["a"], "truncated": True, "truncated_by": "rows",
    }
    facade = DbCoreFacade(client)
    result = facade.execute_on_connection_result("c1", "SELECT 1", max_rows=1)
    assert result["truncated"] is True and result["truncated_by"] == "rows"

    client.request.return_value = {"success": True, "cancelled": True}
    facade.cancel_query("j9")
    assert client.request.call_args.args[:2] == ("query.cancel", {"job_id": "j9"})


def test_legacy_result_calls_do_not_send_control_fields():
    client = MagicMock()
    client.request.return_value = {"success": True, "rows": [], "columns": []}
    DbCoreFacade(client).execute_on_connection_result("c1", "SELECT 1")
    payload = client.request.call_args.args[1]
    assert set(payload) == {"connection_id", "sql", "params"}


def test_connection_cancel_running_query_targets_current_job():
    facade = MagicMock()
    facade.cancel_query.return_value = {"cancelled": True}
    connection = RustDbConnection(DbEndpoint(engine="mysql", host="h", port=1, user="u", password="p", database="d"),
                                  facade, "c1")
    assert connection.cancel_running_query() is False  # nothing running
    connection.query_limits = {"max_rows": 5}
    seen = {}

    def execute(connection_id, sql, params=None, **control):
        seen.update(control)
        assert connection.current_job_id == control["job_id"]
        assert connection.cancel_running_query() is True
        return {"rows": [], "columns": [], "rows_affected": 0}

    facade.execute_on_connection_result.side_effect = execute
    connection.cursor().execute("UPDATE t SET a = 1")
    facade.cancel_query.assert_called_once_with(seen["job_id"])
    assert seen["max_rows"] == 5
    assert connection.current_job_id is None


def test_transaction_worker_reports_cancel_without_rollback():
    from src.ui.dialogs.sql_editor_workers import SQLTransactionExecutionWorker

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query):
            raise DbCoreServiceError("cancelled", error_code="query_cancelled", payload={"in_transaction": True})

    connection = MagicMock()
    connection.cursor.return_value = Cursor()
    # row-returning statements stream through the facade
    connection.facade.execute_on_connection_streaming.side_effect = DbCoreServiceError(
        "cancelled", error_code="query_cancelled", payload={"in_transaction": True}
    )
    worker = SQLTransactionExecutionWorker(connection, ["SELECT pg_sleep(60)"], "postgresql")
    finished = []
    results = []
    worker.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    worker.query_result.connect(lambda *args: results.append(args))
    worker.run()

    connection.rollback.assert_not_called()
    connection.commit.assert_not_called()
    assert finished and finished[0][0] is False and "롤백" in finished[0][1]
    assert results and results[0][5]  # error text reported


def test_cancel_query_requests_interruption_and_server_cancel():
    from src.ui.dialogs.sql_editor_workers import SQLTransactionExecutionWorker

    connection = MagicMock()
    worker = SQLTransactionExecutionWorker(connection, ["SELECT 1"], "mysql")
    worker.cancel_query()
    connection.cancel_running_query.assert_called_once_with()

