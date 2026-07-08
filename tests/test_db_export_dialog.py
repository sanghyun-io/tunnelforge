import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.exporters.rust_dump_exporter import OrphanRecordInfo, RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker

from src.ui.dialogs.db_export_dialog import (
    RustDumpExportDialog,
    cap_incomplete_export_percent,
    export_overall_percent,
    format_export_row_labels,
    format_export_table_status,
    format_export_visible_telemetry,
    next_export_percent,
)

def test_cap_incomplete_export_percent_prevents_early_100():
    assert cap_incomplete_export_percent(100, completed_tables=35, total_tables=208) == 17

def test_cap_incomplete_export_percent_prevents_early_99():
    assert cap_incomplete_export_percent(99, completed_tables=40, total_tables=208) == 19

def test_cap_incomplete_export_percent_allows_final_100():
    assert cap_incomplete_export_percent(100, completed_tables=208, total_tables=208) == 100

def test_next_export_percent_reduces_stale_99_when_table_count_is_incomplete():
    assert next_export_percent(
        last_percent=99,
        computed_percent=99,
        completed_tables=40,
        total_tables=208,
    ) == 19

def test_export_overall_percent_uses_rows_when_total_estimate_exists():
    assert export_overall_percent(
        last_percent=20,
        overall_done=5_000_000,
        total_rows=8_870_000,
        fallback_percent=0,
        completed_tables=10,
        total_tables=208,
    ) == 56

def test_export_overall_percent_caps_when_total_estimate_is_missing():
    assert export_overall_percent(
        last_percent=99,
        overall_done=0,
        total_rows=0,
        fallback_percent=99,
        completed_tables=40,
        total_tables=208,
    ) == 19

def test_format_export_row_labels_separates_done_and_estimate():
    assert format_export_row_labels(3_250_000, 8_900_000) == (
        "📦 처리 rows: 3,250,000 rows",
        "📐 예상 전체: 약 8,900,000 rows",
    )

def test_format_export_row_labels_handles_unknown_estimate():
    assert format_export_row_labels(42, 0) == (
        "📦 처리 rows: 42 rows",
        "📐 예상 전체: 계산 중...",
    )

def test_format_export_table_status_includes_current_table_rows():
    assert (
        format_export_table_status("qe_view_factors_result", 450_000, 1_946_153)
        == "🔄 현재: qe_view_factors_result 450,000 / 1,946,153 rows (23%)"
    )

def test_format_export_visible_telemetry_summarizes_chunk_progress():
    line = format_export_visible_telemetry({
        "event": "row_progress",
        "table": "qe_view_factors_result",
        "rows": 1_000_556,
        "total": 1_946_153,
        "chunk_index": 22,
        "chunks_done": 22,
        "chunks_total": 39,
        "chunk_rows": 55_643,
        "strategy": "pk_range_parallel",
        "stream_ms": 4_731,
    })

    assert line == (
        "qe_view_factors_result: 22/39 chunks, "
        "1,000,556 / 1,946,153 rows (51%), "
        "55,643 rows in 4.7s, pk_range_parallel"
    )

def test_format_export_visible_telemetry_summarizes_schedule():
    line = format_export_visible_telemetry({
        "event": "dump_schedule",
        "scheduler": "global_chunk",
        "threads": 8,
        "table_workers": 2,
        "range_workers_per_table": 4,
        "compression": "zstd",
        "data_format": "tsv",
    })

    assert line == (
        "스케줄: tsv/zstd, scheduler=global_chunk, "
        "threads=8, table_workers=2, range_workers/table=4"
    )

def test_rust_dump_export_dialog_defaults_to_zstd():
    app = QApplication.instance() or QApplication([])
    dialog = RustDumpExportDialog()

    assert dialog.combo_compression.currentText() == "zstd"
    dialog.close()

def test_rust_dump_export_dialog_rejects_parent_manual_folder(tmp_path):
    app = QApplication.instance() or QApplication([])
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(tmp_path) if key == "rust_dump_export_base_dir" else default
    )
    dialog = RustDumpExportDialog(config_manager=config_manager)

    dialog.radio_manual_naming.setChecked(True)
    dialog.input_manual_folder.setText("..")

    generated = Path(dialog._generate_output_dir("dataflare")).resolve()
    assert generated.is_relative_to(tmp_path.resolve())
    assert generated != tmp_path.parent.resolve()
    dialog.close()

def test_export_raw_output_shows_visible_telemetry_summary():
    class FakeLogList:
        def __init__(self):
            self.items = []

        def count(self):
            return len(self.items)

        def takeItem(self, index):
            self.items.pop(index)

        def addItem(self, item):
            self.items.append(item)

        def scrollToBottom(self):
            pass

    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.log_entries = []
    dialog.export_telemetry_events = []
    dialog._add_log = lambda message: dialog.log_entries.append(f"[00:00:00] {message}")

    RustDumpExportDialog.on_raw_output(dialog, json.dumps({
        "event": "row_progress",
        "table": "qe_view_factors_result",
        "rows": 1_000_556,
        "total": 1_946_153,
        "chunks_done": 22,
        "chunks_total": 39,
        "chunk_rows": 55_643,
        "strategy": "pk_range_parallel",
        "stream_ms": 4_731,
    }))

    assert dialog.export_telemetry_events
    assert dialog.txt_log.items == [
        "qe_view_factors_result: 22/39 chunks, "
        "1,000,556 / 1,946,153 rows (51%), "
        "55,643 rows in 4.7s, pk_range_parallel"
    ]
    assert dialog.log_entries[0].endswith("pk_range_parallel")

def test_export_add_log_enables_save_log_before_finish(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpExportDialog()

    assert not dialog.btn_save_log.isEnabled()

    dialog._add_log("export started")

    assert dialog.btn_save_log.isEnabled()
    dialog.close()

def test_export_close_running_can_request_cancel_without_disconnect(monkeypatch):
    class FakeWorker:
        def __init__(self):
            self.cancel_called = False

        def isRunning(self):
            return True

        def cancel(self):
            self.cancel_called = True

    class FakeConnector:
        def __init__(self):
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True

    class FakeButton:
        def __init__(self):
            self.enabled = False

        def setEnabled(self, enabled):
            self.enabled = enabled

    class FakeLabel:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    class FakeEvent:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    dialog = type("DummyDialog", (), {})()
    dialog.worker = FakeWorker()
    dialog.connector = FakeConnector()
    dialog.btn_save_log = FakeButton()
    dialog.label_status = FakeLabel()
    dialog.log_entries = []
    dialog._cancel_requested = False
    dialog._close_after_cancel = False
    dialog._add_log = lambda message: dialog.log_entries.append(message)
    event = FakeEvent()

    RustDumpExportDialog.closeEvent(dialog, event)

    assert event.ignored
    assert not event.accepted
    assert not dialog.connector.disconnected
    assert dialog.worker.cancel_called
    assert dialog._cancel_requested
    assert dialog._close_after_cancel
    assert dialog.btn_save_log.enabled

def test_export_dialog_uses_direct_connector_host_for_rust_dump(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def connect(self, _callback):
            pass

    captured = {}

    class FakeWorker:
        progress = FakeSignal()
        table_progress = FakeSignal()
        detail_progress = FakeSignal()
        table_status = FakeSignal()
        raw_output = FakeSignal()
        finished = FakeSignal()

        def __init__(self, task_type, config, **kwargs):
            captured["task_type"] = task_type
            captured["config"] = config
            captured["kwargs"] = kwargs

        def start(self):
            captured["started"] = True

        def isRunning(self):
            return False

    class FakeConnector:
        host = "db.example.com"
        port = 5432
        user = "exporter"
        password = "secret"
        engine = "postgresql"

        def get_schemas(self):
            return ["app"]

        def get_tables(self, _schema):
            return ["users"]

    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(tmp_path) if key == "rust_dump_export_base_dir" else default
    )

    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.RustDumpWorker", FakeWorker)

    dialog = RustDumpExportDialog(
        connector=FakeConnector(),
        config_manager=config_manager,
        connection_info="db-example_3307",
    )

    dialog.do_export()

    assert captured["task_type"] == "export_schema"
    assert captured["config"].host == "db.example.com"
    assert captured["config"].port == 5432
    assert captured["config"].user == "exporter"
    assert captured["config"].password == "secret"
    assert captured["config"].engine == "postgresql"
    assert captured["started"] is True
    dialog.deleteLater()

def test_export_do_export_regenerates_existing_auto_output_dir(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def connect(self, _callback):
            pass

    captured = {}

    class FakeWorker:
        progress = FakeSignal()
        table_progress = FakeSignal()
        detail_progress = FakeSignal()
        table_status = FakeSignal()
        raw_output = FakeSignal()
        finished = FakeSignal()

        def __init__(self, task_type, config, **kwargs):
            captured["kwargs"] = kwargs

        def start(self):
            captured["started"] = True

        def isRunning(self):
            return False

    class FakeConnector:
        host = "127.0.0.1"
        port = 3306
        user = "root"
        password = "pw"
        engine = "mysql"

        def get_schemas(self):
            return ["app"]

        def get_tables(self, _schema):
            return ["users"]

    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(tmp_path) if key == "rust_dump_export_base_dir" else default
    )

    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.RustDumpWorker", FakeWorker)

    dialog = RustDumpExportDialog(
        connector=FakeConnector(),
        config_manager=config_manager,
        connection_info="conn",
    )
    dialog.combo_schema.setCurrentText("app")

    # 미리보기 시점의 output_dir 를 실제 디스크에 미리 만들어 충돌 상황을 재현한다.
    preview_path = dialog.input_output_dir.text()
    os.makedirs(preview_path, exist_ok=True)

    dialog.do_export()

    used_output_dir = captured["kwargs"]["output_dir"]
    assert used_output_dir != preview_path
    assert not os.path.exists(used_output_dir)
    assert dialog.input_output_dir.text() == used_output_dir
    dialog.deleteLater()

def test_export_github_workers_are_retained_until_finished(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class FakeGithubSignal:
        def __init__(self):
            self._slot = None

        def connect(self, slot):
            self._slot = slot

        def emit(self, *args):
            self._slot(*args)

    class FakeGithubWorker:
        def __init__(self, config_manager, error_type, error_message, context):
            self.finished = FakeGithubSignal()

        def start(self):
            pass

        def deleteLater(self):
            pass

    monkeypatch.setattr(
        "src.ui.workers.github_worker.GitHubReportWorker", FakeGithubWorker
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: default
    dialog = RustDumpExportDialog(config_manager=config_manager)
    dialog.export_schema = "app"
    dialog.export_tables = []

    dialog._report_error_to_github("export", "error 1")
    dialog._report_error_to_github("export", "error 2")

    assert len(dialog._github_workers) == 2
    worker1, worker2 = dialog._github_workers

    worker1.finished.emit(True, "ok")

    assert dialog._github_workers == [worker2]
    dialog.close()
