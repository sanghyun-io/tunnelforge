"""Query result export: file writer, facade contract and SQL editor wiring (P1-2)."""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.db_core_client import DbCoreServiceError
from src.core.db_core_facade import DbCoreFacade
from src.core.result_file_writer import csv_field, write_result_file
from src.ui.dialogs.result_export import (
    ResultExportMixin,
    SQLResultExportWorker,
    describe_export_failure,
)

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "csv_field_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: repr(v["value"])[:30])
def test_csv_field_matches_shared_vectors(vector):
    assert csv_field(vector["value"], vector["guard"]) == vector["expected"]


def test_csv_file_distinguishes_null_and_empty_and_writes_bom_and_crlf(tmp_path):
    path = str(tmp_path / "out.csv")
    rows = [{"id": "1", "s": ""}, {"id": "2", "s": None}, {"id": "3", "s": "a,b"}]
    assert write_result_file(path, ["id", "s"], rows, "csv", bom=True) == 3
    data = Path(path).read_bytes()
    assert data == b'\xef\xbb\xbfid,s\r\n1,""\r\n2,\r\n3,"a,b"\r\n'
    assert not os.path.exists(path + ".partial")


def test_jsonl_keeps_column_order_null_and_unicode(tmp_path):
    path = str(tmp_path / "out.jsonl")
    write_result_file(path, ["z", "a"], [["한글", None], ["", "x"]], "jsonl")
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    assert lines == ['{"z": "한글", "a": null}', '{"z": "", "a": "x"}']


def test_failed_write_leaves_neither_final_nor_partial_file(tmp_path):
    path = str(tmp_path / "out.csv")

    def rows():
        yield ["1"]
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        write_result_file(path, ["id"], rows(), "csv")
    assert not os.path.exists(path) and not os.path.exists(path + ".partial")


def test_facade_export_sends_output_options_and_reports_progress():
    client = MagicMock()

    def request(command, payload, on_event=None):
        on_event({"event": "progress", "rows_written": 500, "bytes_written": 2048})
        return {"success": True, "output_path": payload["output"]["path"], "rows_written": 900, "bytes_written": 4096}

    client.request.side_effect = request
    progress = []
    result = DbCoreFacade(client).export_query_to_file(
        "c1", "SELECT 1", {"path": "x.csv", "format": "csv"}, job_id="j1", timeout_ms=5000,
        on_progress=lambda rows, size: progress.append((rows, size)),
    )
    payload = client.request.call_args.args[1]
    assert payload["output"] == {"path": "x.csv", "format": "csv"}
    assert payload["job_id"] == "j1" and payload["timeout_ms"] == 5000 and "max_rows" not in payload
    assert progress == [(500, 2048)] and result["rows_written"] == 900


def test_facade_export_cancel_raises_with_partial_info():
    client = MagicMock()
    client.request.return_value = {
        "success": False, "error_code": "query_cancelled", "message": "cancelled",
        "output_path": None, "partial_path": "x.csv.partial", "rows_written": 42,
    }
    with pytest.raises(DbCoreServiceError) as info:
        DbCoreFacade(client).export_query_to_file("c1", "SELECT 1", {"path": "x.csv"})
    assert info.value.error_code == "query_cancelled"
    assert info.value.payload["partial_path"] == "x.csv.partial"


def test_failure_message_never_reads_like_a_finished_file():
    cancelled = DbCoreServiceError("c", error_code="query_cancelled", payload={"rows_written": 5})
    assert "취소" in describe_export_failure(cancelled, {}) and "삭제" in describe_export_failure(cancelled, {})
    kept = DbCoreServiceError("t", error_code="query_timeout", payload={"rows_written": 1234, "partial_path": "a.csv.partial"})
    text = describe_export_failure(kept, {})
    assert "제한시간" in text and "사용하지 마세요" in text and "a.csv.partial" in text
    assert "저장 실패" in describe_export_failure(RuntimeError("disk full"), {})


def _worker_with(facade_result=None, error=None):
    connection = MagicMock()
    connection.connection_id = "c1"
    if error is not None:
        connection.facade.export_query_to_file.side_effect = error
    else:
        connection.facade.export_query_to_file.return_value = facade_result
    connector = MagicMock()
    connector.connect.return_value = (True, "")
    connector.connection = connection
    return connector, connection


def test_export_worker_reports_success_and_disconnects(monkeypatch):
    connector, connection = _worker_with({"output_path": "out.csv", "rows_written": 3_000_000, "bytes_written": 42 << 20})
    monkeypatch.setattr("src.ui.dialogs.sql_editor_workers.connector_from_params", lambda params: connector)
    worker = SQLResultExportWorker(MagicMock(), "SELECT 1", {"path": "out.csv"}, timeout_ms=1000)
    finished = []
    worker.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    worker.run()
    assert finished[0][0] is True and "3,000,000" in finished[0][1] and "out.csv" in finished[0][1]
    kwargs = connection.facade.export_query_to_file.call_args.kwargs
    assert kwargs["timeout_ms"] == 1000 and kwargs["job_id"].startswith("export-")
    connector.disconnect.assert_called_once()
    assert connection.current_job_id is None


def test_export_worker_reports_cancel_as_failure_and_cancel_query_reaches_server(monkeypatch):
    error = DbCoreServiceError("c", error_code="query_cancelled", payload={"rows_written": 7})
    connector, connection = _worker_with(error=error)
    monkeypatch.setattr("src.ui.dialogs.sql_editor_workers.connector_from_params", lambda params: connector)
    worker = SQLResultExportWorker(MagicMock(), "SELECT 1", {"path": "out.csv"})
    finished = []
    worker.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    worker.run()
    assert finished[0][0] is False and "취소" in finished[0][1]
    worker.cancel_query()
    connection.cancel_running_query.assert_called_once_with()


class _Stub(ResultExportMixin):
    def __init__(self, picked):
        self.message_text = MagicMock()
        self.status_bar = MagicMock()
        self._picked = picked

    def _prompt_result_export(self, full_result):
        return self._picked


def test_save_displayed_result_writes_exact_rows_from_the_grid(tmp_path):
    path = str(tmp_path / "shown.csv")
    stub = _Stub((path, "csv", {"bom": False, "formula_guard": True, "binary": "hex"}))
    table = MagicMock()
    table._export_columns = ["id", "name"]
    table._export_rows = [["1", None], ["2", "=cmd"], ["3", ""]]
    stub._save_displayed_result(table)
    assert Path(path).read_bytes() == b"id,name\r\n1,\r\n2,'=cmd\r\n3,\"\"\r\n"
    assert "3" in stub.message_text.append.call_args.args[0]


def test_full_export_requires_a_known_source_query():
    stub = _Stub(None)
    stub._query_executing = False
    table = MagicMock()
    table._source_query = ""
    from unittest.mock import patch

    with patch("src.ui.dialogs.result_export.QMessageBox.warning") as warning:
        stub._export_result_full(table)
    warning.assert_called_once()
