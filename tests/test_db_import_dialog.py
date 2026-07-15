import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
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
from src.ui.dialogs.db_import_dialog import resolve_timezone_sql


def test_import_without_tunnel_config_uses_unknown_environment_guard(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    calls = []

    def fake_confirm(self, tunnel_config, operation, schema_name, details):
        calls.append((tunnel_config, operation, schema_name, details))
        return False

    monkeypatch.setattr(
        "src.core.production_guard.ProductionGuard.confirm_dangerous_operation",
        fake_confirm,
    )

    dialog = RustDumpImportDialog()
    try:
        assert dialog._confirm_production_guard("C:/dumps/app", "app") is False
    finally:
        dialog.close()

    assert len(calls) == 1
    tunnel_config, operation, schema_name, details = calls[0]
    assert tunnel_config == {}
    assert operation == "데이터 Import"
    assert schema_name == "app"
    assert "C:/dumps/app" in details
    assert "Import 모드" in details

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

    dialog = RustDumpImportDialog(
        connector=FakeConnector(),
        tunnel_config={"environment": "development"},
    )
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_auto.setChecked(True)

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

    executed_queries = []

    class FakeConnector:
        host = "pg.example.com"
        port = 5432
        user = "importer"
        password = "secret"
        engine = "postgresql"

        def get_schemas(self):
            return ["app"]

        def execute(self, query):
            executed_queries.append(query)
            return []

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()

    dialog = RustDumpImportDialog(
        connector=FakeConnector(),
        tunnel_config={"environment": "development"},
    )
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_auto.setChecked(True)

    dialog.do_import()

    assert captured["task_type"] == "import"
    assert captured["config"].engine == "postgresql"
    assert captured["kwargs"]["timezone_sql"] is None
    assert not any("mysql.time_zone_name" in query for query in executed_queries)
    assert captured["started"] is True
    dialog.deleteLater()


def test_mysql_import_auto_preserves_server_session_timezone(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def connect(self, _callback):
            pass

    captured = {}
    executed_queries = []

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
        host = "mysql.example.com"
        port = 3306
        user = "importer"
        password = "secret"
        engine = "mysql"

        def get_schemas(self):
            return ["app"]

        def execute(self, query):
            executed_queries.append(query)
            return []

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()

    dialog = RustDumpImportDialog(
        connector=FakeConnector(),
        tunnel_config={"environment": "development"},
    )
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_auto.setChecked(True)

    dialog.do_import()

    assert captured["task_type"] == "import"
    assert captured["config"].engine == "mysql"
    assert captured["kwargs"]["timezone_sql"] is None
    assert not any("mysql.time_zone_name" in query for query in executed_queries)
    assert captured["started"] is True
    dialog.deleteLater()


def test_auto_timezone_copy_describes_preservation(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog()

    assert dialog.radio_tz_auto.text() == "자동 (서버/세션 기본값 유지, 권장)"
    assert dialog.radio_tz_auto.toolTip() == "서버/세션 타임존을 변경하지 않고 기본값을 유지합니다."
    assert "자동 보정" not in dialog.radio_tz_auto.toolTip()
    dialog.close()


def test_timezone_group_has_no_duplicate_none_choice(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog()

    assert not hasattr(dialog, "radio_tz_none")
    dialog.close()

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

    dialog = RustDumpImportDialog(
        connector=FakeConnector(),
        tunnel_config={"environment": "development"},
    )
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

@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("table orders failed password=s3cr3t", "s3cr3t"),
        ("table orders failed token=short-token", "short-token"),
        (
            "postgresql://alice:uri-secret@db.internal/customer failed",
            "uri-secret",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY----- table orders failed",
            "private-material",
        ),
    ],
)
def test_sanitize_plain_rust_line_masks_local_diagnostic_secrets(line, secret):
    from src.ui.dialogs.db_dialogs import _sanitize_plain_rust_line

    sanitized = _sanitize_plain_rust_line(line)

    assert secret not in sanitized
    assert "REDACTED" in sanitized


def test_sanitize_plain_rust_line_preserves_diagnostics_but_escapes_log_controls():
    sanitized = _sanitize_plain_rust_line(
        "normal diagnostic\n[FORGED] second entry\x1b[31m\u202ereversed"
    )

    assert sanitized == r"normal diagnostic\n[FORGED] second entry\x1b[31m\u202ereversed"


def test_import_raw_output_falls_back_when_recursive_sanitization_fails(monkeypatch):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

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
    dialog._add_log = dialog.log_entries.append
    raw_line = (
        '{"event":"error","message":"table orders failed '
        'password=fallback-secret\\n[FORGED]"}'
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog._sanitized_rust_event",
        lambda _event: (_ for _ in ()).throw(RecursionError()),
    )

    RustDumpImportDialog.on_raw_output(dialog, raw_line)

    expected = sanitize_local_diagnostic(raw_line)
    assert dialog.txt_log.items == [expected]
    assert dialog.log_entries == [expected]
    assert "fallback-secret" not in "\n".join(dialog.log_entries)


def test_import_unknown_json_event_is_structurally_sanitized_for_display_and_log():
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
    dialog._add_log = dialog.log_entries.append
    raw_line = json.dumps({
        "password": "hunter2",
        "nested": {
            r"AW\x53_SECRET_ACCESS_KEY": "escaped-aws-secret",
            "request_id": "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq",
        },
    })

    RustDumpImportDialog.on_raw_output(dialog, raw_line)

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert dialog.txt_log.items
    assert "hunter2" not in rendered
    assert "escaped-aws-secret" not in rendered
    assert "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq" in rendered
    assert "REDACTED" in rendered


@pytest.mark.parametrize(
    ("value", "expected", "forbidden"),
    [
        (
            [
                "table customer_orders failed",
                {
                    "AWS_SESSION_TOKEN": (
                        "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD"
                    )
                },
            ],
            "customer_orders",
            "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD",
        ),
        (
            "request failed with ASIAABCDEFGHIJKLMNOP",
            "REDACTED",
            "ASIAABCDEFGHIJKLMNOP",
        ),
    ],
)
def test_import_valid_json_arrays_and_scalars_preserve_sanitized_diagnostics(
    value, expected, forbidden
):
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
    dialog._add_log = dialog.log_entries.append

    RustDumpImportDialog.on_raw_output(dialog, json.dumps(value))

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert dialog.txt_log.items
    assert expected in rendered
    assert forbidden not in rendered
    assert "REDACTED" in rendered


def test_import_scalar_json_preserves_unprefixed_opaque_request_id():
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

    request_id = "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq"
    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.log_entries = []
    dialog._add_log = dialog.log_entries.append

    RustDumpImportDialog.on_raw_output(
        dialog, json.dumps(f"request id {request_id} failed")
    )

    assert request_id in "\n".join(dialog.txt_log.items + dialog.log_entries)


def test_import_recursive_malformed_telemetry_falls_back_without_raising(monkeypatch):
    import src.ui.dialogs.db_import_dialog as import_module
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

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

    recursive = []
    recursive.append(recursive)
    malformed_event = {
        "event": "row_progress",
        "table": "customer_orders",
        "rows": {"not": "numeric"},
        "nested": recursive,
    }
    raw_line = '{"event":"row_progress","token":"short-token"}'
    monkeypatch.setattr(import_module.json, "loads", lambda _line: malformed_event)
    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.log_entries = []
    dialog._add_log = dialog.log_entries.append

    import_module.RustDumpImportDialog.on_raw_output(dialog, raw_line)

    assert len(dialog.txt_log.items) == 1
    assert dialog.log_entries == dialog.txt_log.items
    assert "customer_orders" in dialog.txt_log.items[0]
    assert "short-token" not in dialog.txt_log.items[0]


def test_import_malformed_parsed_telemetry_structurally_redacts_nested_credentials():
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
    dialog._add_log = dialog.log_entries.append
    raw_line = json.dumps({
        "event": "row_progress",
        "table": "customer_orders",
        "rows": {"not": "numeric"},
        "password": {"primary": "hunter2", "backup": "backup-secret"},
    })

    RustDumpImportDialog.on_raw_output(dialog, raw_line)

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert "customer_orders" in rendered
    assert "hunter2" not in rendered
    assert "backup-secret" not in rendered


@pytest.mark.parametrize(
    "event",
    [
        {
            "event": "table_progress",
            "table": "customer_orders",
            "status": "completed",
            "current": -1,
            "total": -1,
        },
        {
            "event": "row_progress",
            "table": "customer_orders",
            "rows": -1,
            "total": -1,
            "chunk_rows": -1,
        },
    ],
)
def test_import_negative_numeric_telemetry_uses_plain_fallback(event):
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
    dialog._add_log = dialog.log_entries.append

    RustDumpImportDialog.on_raw_output(dialog, json.dumps(event))

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert "customer_orders" in rendered
    assert "(-1/-1)" not in rendered
    assert "-1/-1 rows" not in rendered


@pytest.mark.parametrize(
    "event",
    [
        {
            "event": "table_progress",
            "table": "customer_orders",
            "status": "completed",
            "current": 2,
            "total": 1,
        },
        {
            "event": "row_progress",
            "table": "customer_orders",
            "rows": 100,
            "total": 1,
            "chunks_done": 5,
            "chunks_total": 1,
        },
    ],
)
def test_import_impossible_progress_relationships_use_plain_fallback(event):
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
    dialog._add_log = dialog.log_entries.append

    RustDumpImportDialog.on_raw_output(dialog, json.dumps(event))

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert "customer_orders" in rendered
    assert "(2/1)" not in rendered
    assert "5/1 chunks" not in rendered
    assert "100/1 rows" not in rendered


@pytest.mark.parametrize(
    "event",
    [
        {"event": "row_progress", "rows": 1, "total": 2},
        {
            "event": "table_progress",
            "table": "customer_orders",
            "status": "unexpected",
            "current": 1,
            "total": 2,
        },
    ],
)
def test_import_invalid_required_telemetry_text_uses_plain_fallback(event):
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
    dialog._add_log = dialog.log_entries.append

    RustDumpImportDialog.on_raw_output(dialog, json.dumps(event))

    assert dialog.txt_log.items
    assert dialog.log_entries == dialog.txt_log.items
    assert '"event"' in dialog.txt_log.items[0]


def test_import_progress_escapes_controls_before_display_and_log():
    class FakeValue:
        def __init__(self):
            self.value = None

        def setText(self, value):
            self.value = value

    class FakeLogList:
        def __init__(self):
            self.items = []

        def addItem(self, item):
            self.items.append(item)

        def scrollToBottom(self):
            pass

    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.label_status = FakeValue()
    dialog.label_fk_status = FakeValue()
    dialog.log_entries = []
    dialog._add_log = dialog.log_entries.append

    RustDumpImportDialog.on_progress(dialog, "phase\n[FORGED]\u202ereversed")

    expected = r"phase\n[FORGED]\u202ereversed"
    assert dialog.txt_log.items == [expected]
    assert dialog.label_status.value == expected
    assert dialog.log_entries == [expected]

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


def test_import_raw_output_sanitizes_all_local_secret_classes_before_logging(caplog):
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
    dialog._add_log = dialog.log_entries.append
    private_key = (
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
        "-----END PRIVATE KEY-----"
    )
    raw_line = json.dumps({
        "event": "error",
        "message": (
            "table customer_orders at C:\\dumps\\customer failed "
            "password=pw-secret; token=token-secret; "
            "postgresql://alice:uri-secret@db.internal/customer; "
            f"{private_key}\n[FORGED]\u202ereversed"
        ),
    })
    caplog.set_level(logging.DEBUG, logger="tunnelforge.db_dialogs")

    RustDumpImportDialog.on_raw_output(dialog, raw_line)

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert "customer_orders" in rendered
    assert r"C:\dumps\customer" in rendered
    assert r"\n[FORGED]\u202ereversed" in rendered
    for secret in ("pw-secret", "token-secret", "uri-secret", "private-material"):
        assert secret not in rendered
    assert raw_line not in caplog.text

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

    dialog = RustDumpImportDialog(
        connector=FakeConnector(),
        tunnel_config={"environment": "development"},
    )
    dialog.input_dir.setText(str(dump_dir))
    dialog.radio_tz_auto.setChecked(True)

    dialog.import_table_rows_done["users"] = 10
    dialog.import_table_rows_total["users"] = 100
    dialog.table_chunk_progress["users"] = (1, 4)
    dialog.dump_metadata = {"schema": "old"}

    dialog.do_import()

    assert dialog._error_report_operation_generation == 1
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

def test_import_error_report_workers_are_retained_without_table_context(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class FakeReportSignal:
        def __init__(self):
            self._slot = None

        def connect(self, slot, *_args):
            self._slot = slot

        def emit(self, *args):
            self._slot(*args)

    created = []

    class FakeReportWorker:
        def __init__(self, config_manager, **kwargs):
            self.report_finished = FakeReportSignal()
            self.finished = FakeReportSignal()
            self.kwargs = kwargs
            self.running = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            pass

    monkeypatch.setattr(
        "src.ui.workers.error_reporting_worker.ErrorReportingWorker",
        FakeReportWorker,
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    class FakeConnector:
        engine = "postgresql"

        def get_schemas(self):
            return []

        def disconnect(self):
            pass

    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = (
        lambda key, default=None: (
            "550e8400-e29b-41d4-a716-446655440000"
            if key == "error_reporting_installation_id"
            else default
        )
    )
    dialog = RustDumpImportDialog(
        connector=FakeConnector(), config_manager=config_manager
    )
    dialog.import_results = {
        "failed_table_secret": {
            "status": "error",
            "message": "per-table-message-secret",
        }
    }
    dialog.combo_target_schema.addItem("target_schema_secret")
    dialog.chk_use_original.setChecked(False)

    engine_error = (
        'dump.import failed for schema "target_schema_secret", '
        'table `failed_table_secret`: per-table-message-secret'
    )
    dialog._report_error_anonymously()
    dialog._report_error_anonymously()

    assert len(dialog._error_report_workers) == 2
    worker1, worker2 = dialog._error_report_workers
    assert worker1.kwargs == {
        "operation_kind": "import",
        "db_engine": "postgresql",
        "phase": "dump.import",
    }
    from src.core.error_report_builder import build_error_report

    payload = build_error_report(
        config_manager,
        **worker1.kwargs,
        error_message="Rust DB Core import operation failed.",
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert "failed_table_secret" not in serialized
    assert "per-table-message-secret" not in serialized
    assert "target_schema_secret" not in serialized

    worker1.report_finished.emit(True, "remote-secret", "https://github.com/issues/1")

    assert dialog._error_report_workers == [worker1, worker2]

    worker1.running = False
    worker1.finished.emit()

    assert dialog._error_report_workers == [worker2]
    worker2.running = False
    worker2.finished.emit()

    assert dialog._error_report_workers == []
    from src.ui.workers import error_reporting_worker as worker_module

    assert worker1 not in worker_module._ACTIVE_ERROR_REPORT_WORKERS
    assert worker2 not in worker_module._ACTIVE_ERROR_REPORT_WORKERS
    dialog.close()


def test_import_retry_confirmation_sanitizes_rust_table_names(monkeypatch):
    class SelectedItem:
        @staticmethod
        def isSelected():
            return True

    captured = {}
    malicious_table = "customer_orders\n[FORGED]\u202ereversed"
    dialog = type("DummyDialog", (), {})()
    dialog.table_items = {malicious_table: SelectedItem()}

    def capture_question(_parent, _title, message, _buttons):
        captured["message"] = message
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.QMessageBox.question",
        capture_question,
    )

    RustDumpImportDialog.do_retry(dialog)

    message = captured["message"]
    assert "\n[FORGED]" not in message
    assert "\u202e" not in message
    assert r"\n[FORGED]\u202e" in message
    assert "customer_orders" in message


def test_import_save_log_escapes_result_controls(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    output_path = tmp_path / "import-log.txt"
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.QFileDialog.getSaveFileName",
        lambda *_args: (str(output_path), ""),
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.QMessageBox.information",
        lambda *_args: None,
    )
    dialog = RustDumpImportDialog()
    dialog.log_entries = ["[00:00:00] started"]
    dialog.last_input_dir = "C:\\dump\\customer\n[FORGED]\u202ereversed"
    dialog.last_target_schema = "schema\n[FORGED]\u202ereversed"
    dialog.import_results = {
        "table\n[FORGED]\u202ereversed": {
            "status": "error",
            "message": (
                "failure Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq "
                "; mysql://alice:dsn@secret@db.internal/customer;\n"
                "[FORGED]\u202ereversed"
            ),
        }
    }

    dialog.save_log()

    saved = output_path.read_text(encoding="utf-8")
    assert r"C:\dump\customer\n[FORGED]\u202ereversed" in saved
    assert r"schema\n[FORGED]\u202ereversed" in saved
    assert r"table\n[FORGED]\u202ereversed" in saved
    assert r"failure" in saved
    assert r"\n[FORGED]\u202ereversed" in saved
    assert "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq" in saved
    assert "alice" not in saved
    assert "dsn@secret" not in saved
    assert "db.internal/customer" in saved
    dialog.close()


def test_import_cancellation_does_not_start_error_reporting(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()
    dialog._cancel_requested = True
    dialog._report_error_anonymously = MagicMock()

    dialog.on_finished(False, "cancelled by user")

    dialog._report_error_anonymously.assert_not_called()
    assert dialog.import_success is False
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


def test_resolve_timezone_sql_preserves_engine_specific_modes():
    assert resolve_timezone_sql("postgresql", "auto") is None
    assert resolve_timezone_sql("postgresql", "kst") == "SET TIME ZONE '+09:00'"
    assert resolve_timezone_sql("postgresql", "utc") == "SET TIME ZONE '+00:00'"
    assert resolve_timezone_sql("postgresql", "none") is None
    assert resolve_timezone_sql("mysql", "kst") == "SET SESSION time_zone = '+09:00'"
    assert resolve_timezone_sql("mysql", "utc") == "SET SESSION time_zone = '+00:00'"
    assert resolve_timezone_sql("mysql", "none") is None


def test_import_table_result_helpers_ignore_fk_restore_and_non_dict_entries():
    dialog = type("DummyDialog", (), {})()
    dialog.import_results = {
        "users": {"status": "done"},
        "orders": {"status": "error"},
        "fk_restore": {"status": "error"},
        "summary": "ignored",
    }

    table_results = RustDumpImportDialog._table_results(dialog)

    assert table_results == {
        "users": {"status": "done"},
        "orders": {"status": "error"},
    }
    assert RustDumpImportDialog._count_by_status(dialog, table_results, "done") == 1
    assert RustDumpImportDialog._count_by_status(dialog, table_results, "error") == 1
