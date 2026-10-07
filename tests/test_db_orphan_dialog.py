import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.core.migration_analysis_models import OrphanRecord
from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker

from src.ui.dialogs.db_dialogs import (
    OrphanAnalysisWorker,
    OrphanRecordDialog,
    OrphanReportWorker,
)

def test_orphan_analysis_worker_emits_results_without_gui_thread_process_events(monkeypatch):
    app = QApplication.instance() or QApplication([])

    fake_results = [object()]
    calls = {}

    class FakeAnalyzer:
        def __init__(self, connector):
            self.connector = connector

        def set_progress_callback(self, callback):
            self.callback = callback

        def analyze_schema(self, schema, **checks):
            calls["schema"], calls["checks"] = schema, checks
            self.callback("검사 중...")
            return type("Result", (), {"orphan_records": fake_results})()

    monkeypatch.setattr(
        "src.ui.dialogs.db_orphan_dialog.MigrationAnalyzer", FakeAnalyzer
    )

    worker = OrphanAnalysisWorker(connector=MagicMock(), schema="app")

    received = {}
    worker.analysis_finished.connect(lambda results: received.setdefault("results", results))
    worker.progress.connect(lambda msg: received.setdefault("progress", []).append(msg))

    # QThread.start()를 통한 실제 OS 스레드 생성 없이 run()을 직접 호출한다.
    worker.run()

    assert received["results"] == fake_results
    assert received["progress"] == ["검사 중..."]
    # Rust upgrade.analyze 에서 고아 검사만 켠다
    assert calls["schema"] == "app"
    assert [name for name, enabled in calls["checks"].items() if enabled] == ["check_orphans"]

def test_orphan_dialog_start_analysis_starts_worker_and_disables_reentrant_actions(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeWorker:
        progress = FakeSignal()
        analysis_finished = FakeSignal()
        failed = FakeSignal()
        finished = FakeSignal()

        def __init__(self, connector, schema):
            self.connector = connector
            self.schema = schema
            self.started = False

        def isRunning(self):
            return False

        def start(self):
            self.started = True

    monkeypatch.setattr(
        "src.ui.dialogs.db_orphan_dialog.OrphanAnalysisWorker", FakeWorker
    )

    connector = MagicMock()
    connector.get_schemas.return_value = ["app"]

    dialog = OrphanRecordDialog(connector=connector)
    dialog.schema_combo.setCurrentText("app")

    dialog.start_analysis()

    assert isinstance(dialog.worker, FakeWorker)
    assert dialog.worker.started
    assert not dialog.analyze_btn.isEnabled()
    dialog.close()

def test_orphan_export_report_uses_current_results_not_resolver_rerun(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def connect(self, _callback):
            pass

    captured = {}

    class FakeWorker:
        report_finished = FakeSignal()
        finished = FakeSignal()

        def __init__(self, schema, output_path, orphan_results):
            captured["schema"] = schema
            captured["output_path"] = output_path
            captured["orphan_results"] = orphan_results

        def isRunning(self):
            return False

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(
        "src.ui.dialogs.db_orphan_dialog.OrphanReportWorker", FakeWorker
    )

    connector = MagicMock()
    connector.get_schemas.return_value = ["app"]

    dialog = OrphanRecordDialog(connector=connector)
    dialog.schema_combo.setCurrentText("app")

    seeded_results = [
        OrphanRecord(
            child_table="orders", child_column="user_id", parent_table="users",
            parent_column="id", orphan_count=3, sample_values=[1], cleanup_sql={"select": "SELECT 1"},
        )
    ]
    dialog.orphan_results = seeded_results

    output_path = str(tmp_path / "report.md")
    monkeypatch.setattr(
        "src.ui.dialogs.db_orphan_dialog.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (output_path, ""),
    )

    dialog.export_report()

    assert captured["orphan_results"] == seeded_results
    assert captured["started"] is True
    dialog.close()

def test_orphan_dialog_close_blocks_while_worker_running(monkeypatch):
    class FakeWorker:
        def isRunning(self):
            return True

    class FakeEvent:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    warnings = []
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args),
    )

    dialog = type("DummyDialog", (), {})()
    dialog.worker = FakeWorker()
    event = FakeEvent()

    OrphanRecordDialog.closeEvent(dialog, event)

    assert event.ignored
    assert not event.accepted
    assert warnings
