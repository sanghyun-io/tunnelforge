import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.exporters.rust_dump_exporter import OrphanRecordInfo, RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker

from src.ui.dialogs.db_dialogs import (
    RustDumpImportDialog,
    displayed_import_percent,
    format_import_row_labels,
    format_import_visible_telemetry,
    import_overall_percent,
    _sanitized_rust_event,
    _sanitize_plain_rust_line,
)

def test_format_import_row_labels_separates_rows_chunks_and_strategy():
    labels = format_import_row_labels({
        "table": "df_subs",
        "rows_done": 100_000,
        "rows_total": 387_398,
        "chunk_rows": 50_000,
        "chunks_done": 2,
        "chunks_total": 8,
        "rows_sec": 40_000,
        "strategy": "parallel_load_data_local_infile",
    })

    assert labels == (
        "📦 처리 rows: 100,000 / 387,398 rows",
        "⚡ 속도: 40,000 rows/s",
        "🔄 현재: df_subs 2/8 chunks, +50,000 rows, 병렬 LOAD DATA LOCAL",
    )

def test_format_import_row_labels_reports_cumulative_average_current_and_eta():
    labels = format_import_row_labels({
        "table": "df_subs",
        "rows_done": 100_000,
        "rows_total": 387_398,
        "overall_rows_done": 300_000,
        "overall_rows_total": 1_000_000,
        "chunk_rows": 50_000,
        "chunks_done": 2,
        "chunks_total": 8,
        "rows_sec": 40_000,
        "avg_rows_sec": 10_000,
        "eta_seconds": 70,
        "strategy": "parallel_load_data_local_infile",
    })

    assert labels == (
        "📦 처리 rows: 300,000 / 1,000,000 rows",
        "⚡ 평균: 10,000 rows/s · 현재: 40,000 rows/s",
        "🔄 현재: df_subs 2/8 chunks, +50,000 rows, 병렬 LOAD DATA LOCAL · ETA 1m 10s",
    )

def test_format_import_row_labels_stops_row_eta_during_post_load_phase():
    labels = format_import_row_labels({
        "overall_rows_done": 1_000,
        "overall_rows_total": 1_000,
        "avg_rows_sec": 100,
        "current_phase": "post_load_ddl",
    })

    assert labels == (
        "📦 처리 rows: 1,000 / 1,000 rows",
        "⚡ 평균: 100 rows/s · 현재: -",
        "🔄 현재 단계: 인덱스/FK 생성 중 · 데이터 Import 완료, 후처리 진행 중",
    )

def test_format_import_row_labels_explains_safe_insert_fallback():
    labels = format_import_row_labels({
        "table": "df_subs",
        "rows_done": 50_000,
        "rows_total": 100_000,
        "chunk_rows": 50_000,
        "chunks_done": 1,
        "chunks_total": 2,
        "strategy": "insert_fallback",
    })

    assert labels[2] == "🔄 현재: df_subs 1/2 chunks, +50,000 rows, 안전 INSERT fallback"

def test_import_overall_percent_uses_all_table_row_totals():
    assert import_overall_percent(
        {"users": 500, "orders": 250},
        {"users": 1_000, "orders": 2_000},
    ) == 25

def test_displayed_import_percent_does_not_promote_table_percent_to_overall():
    assert displayed_import_percent(
        {"small_table": 390},
        {"small_table": 390, "large_table": 8_905_087},
        event_percent=100,
    ) == 1

def test_displayed_import_percent_uses_event_percent_when_overall_total_unknown():
    assert displayed_import_percent({}, {}, event_percent=42) == 42

def test_format_import_visible_telemetry_summarizes_row_progress():
    line = format_import_visible_telemetry({
        "event": "row_progress",
        "table": "ai_phase1_cache",
        "rows": 390,
        "total": 390,
        "chunk_rows": 390,
        "load_ms": 167,
        "strategy": "load_data_local_infile",
    })

    assert line == (
        "ai_phase1_cache: +390 rows, 390/390 rows (100%), "
        "2,335 rows/s, load_data_local_infile"
    )

def test_format_import_visible_telemetry_labels_overall_and_current_speed():
    line = format_import_visible_telemetry({
        "event": "row_progress",
        "table": "df_subs",
        "rows": 100_000,
        "total": 387_398,
        "overall_rows_done": 300_000,
        "overall_rows_total": 1_000_000,
        "chunk_rows": 50_000,
        "chunks_done": 2,
        "chunks_total": 8,
        "load_ms": 1_250,
        "strategy": "parallel_load_data_local_infile",
    })

    assert line == (
        "df_subs: 2/8 chunks, table 100,000/387,398 rows (25%), "
        "전체 300,000/1,000,000 rows (30%), 현재 40,000 rows/s, "
        "parallel_load_data_local_infile"
    )

def test_format_import_visible_telemetry_hides_local_infile_phase_noise():
    line = format_import_visible_telemetry({
        "event": "phase",
        "message": "MySQL local_infile is disabled; using safe Rust INSERT fallback",
        "strategy": "insert_fallback",
    })

    assert line is None

def test_import_raw_output_shows_visible_telemetry_summary():
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
    dialog._add_log = lambda message: dialog.log_entries.append(f"[00:00:00] {message}")

    RustDumpImportDialog.on_raw_output(dialog, json.dumps({
        "event": "row_progress",
        "table": "ai_phase1_cache",
        "rows": 390,
        "total": 390,
        "chunk_rows": 390,
        "load_ms": 167,
        "strategy": "load_data_local_infile",
    }))

    assert dialog.txt_log.items == [
        "ai_phase1_cache: +390 rows, 390/390 rows (100%), "
        "2,335 rows/s, load_data_local_infile"
    ]
    assert dialog.log_entries[0].endswith("load_data_local_infile")
    # 원시 JSONL 라인은 더 이상 persisted 로그에 남기지 않는다 (자격 증명 유출 방지).
    assert len(dialog.log_entries) == 1
    assert not any("[rust_dump]" in entry for entry in dialog.log_entries)

def test_rust_dump_import_dialog_defaults_to_last_export_dump_dir(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    dump_dir = tmp_path / "dataflare_20260528_090000"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps({"format": "tunnelforge-dump", "tables": []}),
        encoding="utf-8",
    )
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(dump_dir) if key == "rust_dump_export_dir" else default
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog(config_manager=config_manager)

    assert dialog.input_dir.text() == str(dump_dir)
    dialog.close()

def test_rust_dump_import_browse_starts_from_export_base_when_no_dump(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(tmp_path) if key == "rust_dump_export_base_dir" else default
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog(config_manager=config_manager)

    assert dialog.input_dir.text() == ""
    assert dialog._get_input_browse_start_dir() == str(tmp_path)
    dialog.close()

def test_import_dialog_does_not_claim_all_objects_are_recreated(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog()
    labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))

    assert "모든 객체" not in labels
    assert "테이블" in labels
    dialog.close()

def test_import_dialog_uses_direct_connector_host_for_rust_dump(monkeypatch, tmp_path):
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
        import_finished = FakeSignal()
        finished = FakeSignal()
        metadata_analyzed = FakeSignal()
        table_chunk_progress = FakeSignal()

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
        user = "importer"
        password = "secret"
        engine = "postgresql"

        def get_schemas(self):
            return ["app"]

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()

    dialog = RustDumpImportDialog(connector=FakeConnector())
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_none.setChecked(True)

    dialog.do_import()

    assert captured["task_type"] == "import"
    assert captured["config"].host == "db.example.com"
    assert captured["config"].port == 5432
    assert captured["config"].user == "importer"
    assert captured["config"].password == "secret"
    assert captured["config"].engine == "postgresql"
    assert captured["started"] is True
    dialog.deleteLater()

def test_postgresql_import_auto_timezone_skips_mysql_detection(monkeypatch, tmp_path):
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
        import_finished = FakeSignal()
        finished = FakeSignal()
        metadata_analyzed = FakeSignal()
        table_chunk_progress = FakeSignal()

        def __init__(self, task_type, config, **kwargs):
            captured["task_type"] = task_type
            captured["config"] = config
            captured["kwargs"] = kwargs

        def start(self):
            captured["started"] = True

        def isRunning(self):
            return False

    class FakeConnector:
        host = "pg.example.com"
        port = 5432
        user = "importer"
        password = "secret"
        engine = "postgresql"

        def get_schemas(self):
            return ["app"]

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()

    dialog = RustDumpImportDialog(connector=FakeConnector())
    dialog.input_dir.setText(str(dump_dir))
    dialog.check_timezone_support = MagicMock(
        side_effect=AssertionError("PostgreSQL import must not query MySQL timezone tables")
    )

    dialog.do_import()

    assert captured["task_type"] == "import"
    assert captured["config"].engine == "postgresql"
    assert captured["kwargs"]["timezone_sql"] is None
    dialog.check_timezone_support.assert_not_called()
    assert captured["started"] is True
    dialog.deleteLater()

def test_postgresql_import_forced_kst_uses_postgresql_timezone_sql(monkeypatch, tmp_path):
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
        import_finished = FakeSignal()
        finished = FakeSignal()
        metadata_analyzed = FakeSignal()
        table_chunk_progress = FakeSignal()

        def __init__(self, task_type, config, **kwargs):
            captured["task_type"] = task_type
            captured["config"] = config
            captured["kwargs"] = kwargs

        def start(self):
            captured["started"] = True

        def isRunning(self):
            return False

    class FakeConnector:
        host = "pg.example.com"
        port = 5432
        user = "importer"
        password = "secret"
        engine = "postgresql"

        def get_schemas(self):
            return ["app"]

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()

    dialog = RustDumpImportDialog(connector=FakeConnector())
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_kst.setChecked(True)

    dialog.do_import()

    assert captured["task_type"] == "import"
    assert captured["config"].engine == "postgresql"
    assert captured["kwargs"]["timezone_sql"] == "SET TIME ZONE '+09:00'"
    assert captured["started"] is True
    dialog.deleteLater()

def test_sanitized_rust_event_redacts_password_and_credentials_recursively():
    from src.ui.dialogs.db_dialogs import _sanitized_rust_event

    sanitized = _sanitized_rust_event({
        "event": "error",
        "password": "s3cr3t",
        "nested": {"credentials": {"user": "root", "password": "s3cr3t"}},
    })

    flattened = json.dumps(sanitized)
    assert "s3cr3t" not in flattened

def test_sanitize_plain_rust_line_masks_password_assignment():
    from src.ui.dialogs.db_dialogs import _sanitize_plain_rust_line

    sanitized = _sanitize_plain_rust_line("connecting with password=s3cr3t to host")
    assert "s3cr3t" not in sanitized

def test_import_raw_output_redacts_password_and_credentials_from_saved_log():
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
    dialog._add_log = lambda message: dialog.log_entries.append(f"[00:00:00] {message}")

    # message가 비어 있으면 format_import_visible_telemetry의 "error" 분기가
    # 이벤트 dict 전체를 문자열로 echo한다. 정제되지 않으면 여기서 그대로 유출된다.
    RustDumpImportDialog.on_raw_output(dialog, json.dumps({
        "event": "error",
        "password": "s3cr3t-pw",
        "credentials": {"user": "root", "password": "s3cr3t-pw"},
    }))

    combined = "\n".join(dialog.log_entries)
    assert "s3cr3t-pw" not in combined

def test_import_log_entries_are_capped(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()

    for i in range(600):
        dialog._add_log(f"entry-{i}")

    assert len(dialog.log_entries) == 500
    assert dialog.log_entries[-1].endswith("entry-599")
    dialog.close()

def test_import_button_click_does_not_pass_checked_bool_as_retry_tables(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()
    dialog.do_import = MagicMock()

    dialog.btn_import.click()

    dialog.do_import.assert_called_once_with()
    dialog.close()

def test_import_default_dump_dir_runs_upgrade_check(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    dump_dir = tmp_path / "dataflare_20260528_090000"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps({"format": "tunnelforge-dump", "tables": []}),
        encoding="utf-8",
    )
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(dump_dir) if key == "rust_dump_export_dir" else default
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    calls = []
    monkeypatch.setattr(
        RustDumpImportDialog, "_run_upgrade_check",
        lambda self, path: calls.append(path),
    )

    dialog = RustDumpImportDialog(config_manager=config_manager)

    assert calls == [str(dump_dir)]
    dialog.close()

def test_import_input_editing_finished_runs_upgrade_check_for_valid_dir(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps({"format": "tunnelforge-dump", "tables": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()

    calls = []
    dialog._run_upgrade_check = lambda path: calls.append(path)

    dialog.input_dir.setText(str(dump_dir))
    dialog.input_dir.editingFinished.emit()

    assert calls == [str(dump_dir)]
    dialog.close()

def test_import_fresh_run_clears_stale_progress_state(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeWorker:
        progress = FakeSignal()
        table_progress = FakeSignal()
        detail_progress = FakeSignal()
        table_status = FakeSignal()
        raw_output = FakeSignal()
        import_finished = FakeSignal()
        finished = FakeSignal()
        metadata_analyzed = FakeSignal()
        table_chunk_progress = FakeSignal()

        def __init__(self, task_type, config, **kwargs):
            pass

        def start(self):
            pass

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

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()

    dialog = RustDumpImportDialog(connector=FakeConnector())
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_none.setChecked(True)

    dialog.import_table_rows_done["users"] = 10
    dialog.import_table_rows_total["users"] = 100
    dialog.table_chunk_progress["users"] = (1, 4)
    dialog.dump_metadata = {"schema": "old"}

    dialog.do_import()

    assert dialog.import_table_rows_done == {}
    assert dialog.import_table_rows_total == {}
    assert dialog.table_chunk_progress == {}
    assert dialog.dump_metadata is None
    dialog.deleteLater()

def test_import_save_log_uses_running_status_when_success_unknown(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()
    dialog.log_entries = ["[00:00:00] started"]
    dialog.import_success = None

    captured = {}

    def fake_get_save_file_name(parent, title, default_path, filter_str):
        captured["default_path"] = default_path
        return "", ""

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.QFileDialog.getSaveFileName",
        fake_get_save_file_name,
    )

    dialog.save_log()

    assert "_running_" in captured["default_path"]
    dialog.close()

def test_import_close_running_requests_cancel_and_keeps_dialog_until_finished(monkeypatch):
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

    RustDumpImportDialog.closeEvent(dialog, event)

    assert event.ignored
    assert not event.accepted
    assert not dialog.connector.disconnected
    assert dialog.worker.cancel_called
    assert dialog._cancel_requested
    assert dialog._close_after_cancel

def test_import_github_workers_are_retained_until_finished(monkeypatch):
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
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog(config_manager=MagicMock())
    dialog.import_results = {}

    dialog._report_error_to_github("import", "error 1")
    dialog._report_error_to_github("import", "error 2")

    assert len(dialog._github_workers) == 2
    worker1, worker2 = dialog._github_workers

    worker1.finished.emit(True, "ok")

    assert dialog._github_workers == [worker2]
    dialog.close()

def test_import_mode_text_uses_single_korean_label_source(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()

    dialog.radio_merge.setChecked(True)
    assert dialog._get_import_mode_text() == "증분 Import (병합)"

    dialog.radio_replace.setChecked(True)
    assert dialog._get_import_mode_text() == "전체 교체 Import"

    dialog.radio_recreate.setChecked(True)
    assert dialog._get_import_mode_text() == "완전 재생성 Import"

    dialog.close()
