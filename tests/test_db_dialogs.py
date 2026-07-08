import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.ui.dialogs.db_dialogs import (
    OrphanAnalysisWorker,
    OrphanRecordDialog,
    OrphanReportWorker,
    RustDumpExportDialog,
    RustDumpImportDialog,
    RustDumpWizard,
    cap_incomplete_export_percent,
    displayed_import_percent,
    format_import_visible_telemetry,
    export_overall_percent,
    import_overall_percent,
    format_import_row_labels,
    format_export_visible_telemetry,
    format_export_row_labels,
    format_export_table_status,
    next_export_percent,
)
from src.exporters.rust_dump_exporter import OrphanRecordInfo, RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker


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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog(config_manager=config_manager)

    assert dialog.input_dir.text() == ""
    assert dialog._get_input_browse_start_dir() == str(tmp_path)
    dialog.close()


def test_import_dialog_does_not_claim_all_objects_are_recreated(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog()
    labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))

    assert "모든 객체" not in labels
    assert "테이블" in labels
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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


def test_preselected_export_tunnel_passes_mysql_default_database(monkeypatch):
    captured = {}

    class FakeMySQLConnector:
        def __init__(self, host, port, user, password, database=None):
            captured["host"] = host
            captured["port"] = port
            captured["user"] = user
            captured["password"] = password
            captured["database"] = database

        def connect(self):
            return True, "ok"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.MySQLConnector", FakeMySQLConnector)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("root", "tunnelpass")
    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 3309)

    wizard = RustDumpWizard(
        tunnel_engine=tunnel_engine,
        config_manager=config_manager,
        preselected_tunnel={
            "id": "mysql-tunnel",
            "name": "MySQL 터널",
            "db_engine": "mysql",
            "default_database": "tf_source84",
        },
    )

    connector, connection_info = wizard._connect_preselected_tunnel()

    assert connector is not None
    assert connection_info == "MySQL 터널_root"
    assert captured == {
        "host": "127.0.0.1",
        "port": 3309,
        "user": "root",
        "password": "tunnelpass",
        "database": "tf_source84",
    }


def test_preselected_export_tunnel_uses_postgres_connector_for_postgresql(monkeypatch):
    captured = {}

    class FailingMySQLConnector:
        def __init__(self, *args, **kwargs):
            raise AssertionError("PostgreSQL tunnel must not create MySQLConnector")

    class FakePostgresConnector:
        engine = "postgresql"

        def __init__(self, host, port, user, password, database=None):
            captured["host"] = host
            captured["port"] = port
            captured["user"] = user
            captured["password"] = password
            captured["database"] = database

        def connect(self):
            return True, "ok"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.MySQLConnector", FailingMySQLConnector)
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.PostgresConnector", FakePostgresConnector)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 55432)

    wizard = RustDumpWizard(
        tunnel_engine=tunnel_engine,
        config_manager=config_manager,
        preselected_tunnel={
            "id": "pg-tunnel",
            "name": "PostgreSQL 터널",
            "db_engine": "postgresql",
            "default_database": "postgres",
            "default_schema": "public",
        },
    )

    connector, connection_info = wizard._connect_preselected_tunnel()

    assert connector is not None
    assert connector.engine == "postgresql"
    assert connection_info == "PostgreSQL 터널_postgres"
    assert captured == {
        "host": "127.0.0.1",
        "port": 55432,
        "user": "postgres",
        "password": "tunnelpass",
        "database": "postgres",
    }


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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

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


# ---------------------------------------------------------------------------
# WP-3.1: RustDumpWorker.cancel()
# ---------------------------------------------------------------------------


def _make_worker_with_fake_runner(owns_facade: bool):
    config = RustDumpConfig(host="127.0.0.1", port=3306, user="root", password="pw", engine="mysql")
    worker = RustDumpWorker("export_schema", config)

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    class FakeClient:
        def __init__(self, process):
            self._process = process

    class FakeFacade:
        def __init__(self, client):
            self.client = client

    class FakeRunner:
        def __init__(self, facade, owns):
            self.facade = facade
            self._owns_facade = owns

    process = FakeProcess()
    runner = FakeRunner(FakeFacade(FakeClient(process)), owns_facade)
    worker._active_runner = runner
    return worker, process


def test_rust_dump_worker_cancel_terminates_owned_dedicated_process():
    worker, process = _make_worker_with_fake_runner(owns_facade=True)

    result = worker.cancel()

    assert result is True
    assert worker._cancel_requested is True
    assert process.terminated is True


def test_rust_dump_worker_cancel_does_not_touch_shared_facade_process():
    worker, process = _make_worker_with_fake_runner(owns_facade=False)

    result = worker.cancel()

    assert result is True
    assert worker._cancel_requested is True
    assert process.terminated is False


# ---------------------------------------------------------------------------
# WP-3.1: sanitization helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# WP-3.1: Export dialog — output dir regeneration, GitHub workers, cancel-close
# ---------------------------------------------------------------------------


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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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


# ---------------------------------------------------------------------------
# WP-3.1: Import dialog — raw output sanitization, log cap, button click,
# upgrade check, fresh-run reset, save_log status, cancel-close, GitHub workers
# ---------------------------------------------------------------------------


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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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
        "src.ui.dialogs.db_dialogs.QFileDialog.getSaveFileName",
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
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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


# ---------------------------------------------------------------------------
# WP-3.1: Import mode label — single Korean-label source
# ---------------------------------------------------------------------------


def test_import_mode_text_uses_single_korean_label_source(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.check_rust_dump",
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


# ---------------------------------------------------------------------------
# WP-3.1: Orphan analysis/report threading and connector cleanup
# ---------------------------------------------------------------------------


def test_orphan_analysis_worker_emits_results_without_gui_thread_process_events(monkeypatch):
    app = QApplication.instance() or QApplication([])

    fake_results = [object()]

    class FakeResolver:
        def __init__(self, connector):
            self.connector = connector

        def find_orphan_records(self, schema, progress_callback=None):
            if progress_callback:
                progress_callback("검사 중...")
            return fake_results

    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.ForeignKeyResolver", FakeResolver
    )

    worker = OrphanAnalysisWorker(connector=MagicMock(), schema="app")

    received = {}
    worker.analysis_finished.connect(lambda results: received.setdefault("results", results))
    worker.progress.connect(lambda msg: received.setdefault("progress", []).append(msg))

    # QThread.start()를 통한 실제 OS 스레드 생성 없이 run()을 직접 호출한다.
    worker.run()

    assert received["results"] == fake_results
    assert received["progress"] == ["검사 중..."]


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
        "src.ui.dialogs.db_dialogs.OrphanAnalysisWorker", FakeWorker
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
        "src.ui.dialogs.db_dialogs.OrphanReportWorker", FakeWorker
    )

    connector = MagicMock()
    connector.get_schemas.return_value = ["app"]

    dialog = OrphanRecordDialog(connector=connector)
    dialog.schema_combo.setCurrentText("app")

    seeded_results = [
        OrphanRecordInfo(
            table="orders", column="user_id", referenced_table="users",
            referenced_column="id", orphan_count=3, sample_values=["1"], query="SELECT 1",
        )
    ]
    dialog.orphan_results = seeded_results

    output_path = str(tmp_path / "report.md")
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.QFileDialog.getSaveFileName",
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


def test_start_orphan_check_disconnects_connector_after_dialog_exec(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class FakeConnector:
        def __init__(self):
            self.disconnect_calls = 0

        def get_schemas(self):
            return []

        def disconnect(self):
            self.disconnect_calls += 1

    connector = FakeConnector()

    wizard = RustDumpWizard(preselected_tunnel={"id": "t1"})
    monkeypatch.setattr(
        wizard, "_connect_preselected_tunnel", lambda: (connector, "info")
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.OrphanRecordDialog.exec", lambda self: None
    )

    result = wizard.start_orphan_check()

    assert result is True
    assert connector.disconnect_calls == 1
