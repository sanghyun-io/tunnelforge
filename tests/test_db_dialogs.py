import json
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication

from src.ui.dialogs.db_dialogs import (
    RustDumpExportDialog,
    RustDumpWizard,
    cap_incomplete_export_percent,
    displayed_import_percent,
    export_overall_percent,
    import_overall_percent,
    format_import_row_labels,
    format_export_visible_telemetry,
    format_export_row_labels,
    format_export_table_status,
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
