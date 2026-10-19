import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.exporters.rust_dump_exporter import RustDumpConfig
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
from src.core.production_guard import Environment
from PyQt6.QtWidgets import QDialog


def _write_dump(path, **manifest):
    """A dump folder the dialog accepts; by default a current MySQL dump of the database ``app``."""
    path.mkdir(parents=True, exist_ok=True)
    data = {"format": "tunnelforge-dump", "format_version": 5, "source_engine": "mysql", "database": "app",
            "source_timezone": "UTC", "tables": []}
    data.update(manifest)
    (path / "_tunnelforge_dump.json").write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *_args):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


_NOTHING_OVERWRITTEN = {"dry_run": True, "replace": [], "create": ["users"], "untouched": [], "blockers": [],
                        "warnings": []}


def _fake_plan_dialog(monkeypatch, accept=None):
    """Replace the plan dialog; ``accept=None`` fails the test if it is shown at all (exec would block)."""
    shown = []

    class FakePlanDialog:
        def __init__(self, parent, preview, plan, environment, target_text, name, toggles=""):
            shown.append({"preview": preview, "plan": plan, "environment": environment, "target": target_text,
                          "name": name, "toggles": toggles})

        def exec(self):
            assert accept is not None, "unexpected plan dialog"
            return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.ImportPlanDialog", FakePlanDialog)
    return shown


def _fake_workers(monkeypatch, preview=None, preview_ok=True, preview_message="", hold_preview=False):
    """Replace RustDumpWorker: a preview answers at once (``hold_preview``: it keeps running until the
    test emits its answer); any other task only records itself."""
    created = []

    class FakeWorker:
        def __init__(self, task_type, config, **kwargs):
            self.task_type, self.config, self.kwargs, self.started = task_type, config, kwargs, False
            self.running, self.cancelled = False, False
            for name in ("progress", "table_progress", "detail_progress", "table_status", "raw_output", "import_finished",
                         "finished", "metadata_analyzed", "table_chunk_progress", "preview_finished", "promotion_finished"):
                setattr(self, name, _Signal())
            created.append(self)

        def start(self):
            self.started = True
            if self.task_type == "import_preview":
                if hold_preview:
                    self.running = True
                    return
                self.preview_finished.emit(preview_ok, preview_message,
                                           dict(_NOTHING_OVERWRITTEN) if preview is None else preview)

        def isRunning(self):
            return self.running

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)
    _fake_plan_dialog(monkeypatch)
    return created


def _imports(created):
    return [worker for worker in created if worker.task_type == "import"]


def test_trigger_skip_toggle_is_shared_with_preview_and_never_persisted(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    preview = {**_NOTHING_OVERWRITTEN, "programs_skipped_toggle": ["trigger:sample:guard"],
               "warnings": [{"code": "programs_skipped_toggle", "detail": "trigger:sample:guard"}]}
    created = _fake_workers(monkeypatch, preview=preview)
    shown = _fake_plan_dialog(monkeypatch, accept=True)
    values = {}
    config = MagicMock()
    config.get_app_setting.side_effect = lambda key, default=None: values.get(key, default)
    config.set_app_setting.side_effect = lambda key, value: values.__setitem__(key, value)
    config.set_app_settings.side_effect = values.update
    dialog = RustDumpImportDialog(config_manager=config, tunnel_config={"environment": "development"})
    try:
        assert not dialog.chk_skip_triggers.isChecked()
        dialog.input_dir.setText(_write_dump(tmp_path / "dump", stored_programs=[{"kind":"TRIGGER","name":"guard","table":"sample"}]))
        dialog.chk_skip_triggers.setChecked(True)
        assert "트리거 없이" in dialog._advanced_summary()
        dialog.do_import()
        assert shown[0]["plan"]["must_see"] is True
        assert "sample.guard" in "\n".join(shown[0]["plan"]["warnings"])
        assert all(worker.kwargs["skip_triggers"] is True for worker in created)
    finally:
        dialog.close()
    fresh = RustDumpImportDialog(config_manager=config)
    try:
        assert not fresh.chk_skip_triggers.isChecked()
    finally:
        fresh.close()


def test_failure_report_names_tables_whose_triggers_were_lost(monkeypatch):
    from src.ui.dialogs.db_import_dialog import _replace_failure_details
    assert "트리거를 다시 만들지 못한 테이블: second" in _replace_failure_details({"tables_lost_triggers": ["second"]})


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_trigger_skip_toggle_applies_only_to_mysql_replace(monkeypatch, engine):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    connector = MagicMock(host="localhost", port=3306, user="root", password="pw", engine=engine, database="app")
    connector.get_schemas.return_value = []
    dialog = RustDumpImportDialog(connector=connector)
    try:
        assert "기존 트리거도 삭제" in dialog.chk_skip_triggers.toolTip()
        assert dialog.chk_skip_triggers.isEnabled() is (engine == "mysql")
        if engine == "mysql":
            dialog.chk_skip_triggers.setChecked(True)
        dialog.chk_verify_copy.setChecked(True)
        assert not dialog.chk_skip_triggers.isEnabled() and not dialog.chk_skip_triggers.isChecked()
        dialog.chk_verify_copy.setChecked(False)
        assert dialog.chk_skip_triggers.isEnabled() is (engine == "mysql")
        assert not dialog.chk_skip_triggers.isChecked()
    finally:
        dialog.close()


def test_failed_import_report_shows_program_warnings():
    from src.ui.dialogs.db_import_dialog import _capture_import_audit, _replace_failure_details
    audit = {}
    _capture_import_audit(audit, {"event":"import_report", "program_warnings":[{"code":"program_database_collation_changed", "name":"guard", "source":"utf8mb4_bin", "target":"utf8mb4_0900_ai_ci"}]})
    text = _replace_failure_details(audit)
    assert "guard" in text and "정렬 규칙" in text and "utf8mb4_bin" in text


@pytest.mark.parametrize("tunnel_config", [None, {"environment": "staging"}])
def test_staging_shows_only_plan_dialog(monkeypatch, tmp_path, tunnel_config):
    """Staging and unclassified profiles get the one plan dialog, and no other prompt, before a run."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    monkeypatch.setattr("src.core.production_guard.ProductionGuard.confirm_dangerous_operation",
                        MagicMock(side_effect=AssertionError("the old guard prompt")))
    created = _fake_workers(monkeypatch, preview={**_NOTHING_OVERWRITTEN, "replace": ["users"], "create": []})
    shown = _fake_plan_dialog(monkeypatch, accept=False)
    dialog = RustDumpImportDialog(tunnel_config=tunnel_config)
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
    finally:
        dialog.close()

    assert len(shown) == 1
    assert shown[0]["environment"] == (Environment.UNKNOWN if tunnel_config is None else Environment.STAGING)
    assert shown[0]["name"] == "app" and shown[0]["target"].endswith("/ app")
    assert [worker.task_type for worker in created] == ["import_preview"]

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

class _PgConnector:
    host = "pg.example.com"
    port = 5432
    user = "importer"
    password = "secret"
    engine = "postgresql"
    database = "appdb"

    def get_schemas(self):
        return ["app"]


def test_import_dialog_uses_direct_connector_host_for_rust_dump(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    created = _fake_workers(monkeypatch)
    dialog = RustDumpImportDialog(connector=_PgConnector(), tunnel_config={"environment": "development"})
    dialog.input_dir.setText(_write_dump(tmp_path / "dump", source_engine="postgresql", source_schema="app"))

    dialog.do_import()

    preview, imported = created
    assert preview.task_type == "import_preview" and imported.task_type == "import"
    assert imported.kwargs["import_mode"] == "replace"
    assert imported.config.host == "pg.example.com"
    assert imported.config.port == 5432
    assert imported.config.user == "importer"
    assert imported.config.password == "secret"
    assert imported.config.engine == "postgresql"
    assert imported.started is True
    assert dialog.lbl_target_endpoint.text() == "pg.example.com:5432 / appdb /"
    dialog.deleteLater()


def test_postgresql_import_without_the_timezone_toggle_keeps_the_dump_timezone(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    created = _fake_workers(monkeypatch)
    dialog = RustDumpImportDialog(connector=_PgConnector(), tunnel_config={"environment": "development"})
    dialog.input_dir.setText(_write_dump(tmp_path / "dump", source_engine="postgresql", source_schema="app"))

    dialog.do_import()

    for worker in created:
        assert worker.kwargs["timezone_sql"] is None and worker.kwargs["use_source_timezone"] is True
    assert _imports(created)[0].started is True
    dialog.deleteLater()


def test_postgresql_import_legacy_dump_kst_uses_postgresql_timezone_sql(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    created = _fake_workers(monkeypatch)
    dialog = RustDumpImportDialog(connector=_PgConnector(), tunnel_config={"environment": "development"})
    # A legacy dump records no source timezone, so the toggle is offered.
    dialog.input_dir.setText(_write_dump(tmp_path / "dump", source_engine="postgresql", source_schema="app",
                                         source_timezone=None))
    dialog._update_target_options()
    dialog.chk_timezone.setChecked(True)
    dialog.combo_timezone.setCurrentIndex(dialog.combo_timezone.findData("kst"))

    dialog.do_import()

    imported = _imports(created)[0]
    assert imported.kwargs["timezone_sql"] == "SET TIME ZONE '+09:00'"
    assert imported.kwargs["use_source_timezone"] is True
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
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    _fake_workers(monkeypatch)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    dialog.input_dir.setText(_write_dump(tmp_path / "dump"))

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

        def setText(self, text):
            self.text = text

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
    dialog.btn_stop = FakeButton()
    dialog.label_status = FakeLabel()
    dialog.log_entries = []
    dialog._cancel_requested = False
    dialog._close_after_cancel = False
    dialog._add_log = lambda message: dialog.log_entries.append(message)
    dialog.request_stop = lambda close_after=False: RustDumpImportDialog.request_stop(dialog, close_after)
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
    dialog.chk_other_name.setChecked(True)

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


@pytest.mark.parametrize("db_engine", ["mysql", "postgresql"])
def test_import_save_log_summary_records_executed_replace_mode(tmp_path, monkeypatch, db_engine):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive

    class FakeConnector:
        host = "127.0.0.1"
        port = 5432 if db_engine == "postgresql" else 3306
        user = "audit_sensitive_login"
        password = "pw"
        engine = db_engine
        database = "selected_database"

        def get_schemas(self):
            return ["grireport"]

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    output_path = tmp_path / "import-log.txt"
    manifest_path = dump_dir / "_tunnelforge_dump.json"
    manifest_path.write_text(json.dumps({"source_engine": db_engine, "database": "original/db", "source_schema": "original/db", "tables": []}), encoding="utf-8")
    dialog_paths = []
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    _fake_workers(monkeypatch)
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.QFileDialog.getSaveFileName",
        lambda *_args: (dialog_paths.append(_args[2]) or str(output_path), ""),
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.QMessageBox.information",
        lambda *_args: None,
    )

    dialog = RustDumpImportDialog(
        connector=FakeConnector(),
        tunnel_config={"name": "staging-profile", "environment": "development", "connection_mode": "ssh", "remote_host": "staging-db.internal", "remote_port": 5432, "bastion_host": "staging-jump.internal", "bastion_user": "private-ssh-user", "bastion_key": "private-key-file", "db_password": "private-db-password"},
    )
    dialog.input_dir.setText(str(dump_dir))

    dialog.do_import()
    dialog.chk_verify_copy.setChecked(True)  # a toggle changed after the run does not rewrite its log
    dialog.connector.host = "changed-after-job"
    dialog.tunnel_config.update(name="production-profile", environment="production", remote_host="production-db.internal", bastion_host="production-jump.internal")
    manifest_path.write_text('{"source_engine":"mysql","database":"changed_after_job","tables":[]}', encoding="utf-8")
    dialog.on_raw_output(json.dumps({"event": "target_change", "phase": "dump_import_prepare", "action": "drop_table", "status": "completed", "table": "confirmed_drop"}))
    dialog.on_raw_output(json.dumps({"event": "target_change", "phase": "dump_import_prepare", "action": "drop_table", "status": "failed", "table": "not_confirmed"}))
    dialog.on_raw_output(json.dumps({"event": "import_report", "status": "failed", "report_path": "report.json"}))
    dialog.import_results["unattempted"] = {"status": "blocked", "message": "not started"}
    for index in range(600):
        dialog._add_log(f"progress {index}")
    dialog.import_success = False
    dialog.save_log()

    saved_summary = output_path.read_text(encoding="utf-8").split("상세 로그", 1)[0]
    assert "실행 방식: 덤프 테이블 교체" in saved_summary
    assert "실행 방식: 먼저 복사본에서 검증하고 교체" not in saved_summary
    assert f"127.0.0.1:{5432 if db_engine == 'postgresql' else 3306}" in saved_summary
    assert Path(dialog_paths[0]).name.startswith("import_log_original_db_failed_")
    assert dialog.worker.kwargs["target_schema"] == "original/db"
    if db_engine == "postgresql":
        assert "Target database: selected_database" in saved_summary
        assert "Target schema: original/db" in saved_summary
    assert "original/db" in saved_summary
    assert "changed-after-job" not in saved_summary
    assert "staging-profile" in saved_summary
    assert "Environment: development" in saved_summary
    assert "staging-db.internal:5432" in saved_summary
    assert "staging-jump.internal" in saved_summary
    assert "production-profile" not in saved_summary
    assert "production-db.internal" not in saved_summary
    assert "production-jump.internal" not in saved_summary
    assert "private-ssh-user" not in saved_summary
    assert "private-key-file" not in saved_summary
    assert "private-db-password" not in saved_summary
    assert "changed_after_job" not in saved_summary
    assert "confirmed_drop" in saved_summary
    assert "not_confirmed" not in saved_summary
    assert "dump_import_prepare" in saved_summary
    assert "report.json" in saved_summary
    assert "audit_sensitive_login" not in saved_summary
    assert "pw" not in saved_summary
    dialog.deleteLater()


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

def test_resolve_timezone_sql_preserves_engine_specific_modes():
    assert resolve_timezone_sql("postgresql", "auto") is None
    assert resolve_timezone_sql("postgresql", "kst") == "SET TIME ZONE '+09:00'"
    assert resolve_timezone_sql("postgresql", "utc") == "SET TIME ZONE '+00:00'"
    assert resolve_timezone_sql("mysql", "kst") == "SET SESSION time_zone = '+09:00'"
    assert resolve_timezone_sql("mysql", "utc") == "SET SESSION time_zone = '+00:00'"
    assert resolve_timezone_sql("mysql", "none") is None


def test_import_default_is_replace_and_has_no_mode_radios(monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QRadioButton
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    created = _fake_workers(monkeypatch)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    try:
        assert dialog.findChildren(QRadioButton) == []
        assert not hasattr(dialog, "btn_import_mode") and not hasattr(dialog, "radio_tz_auto")
        assert dialog.advanced_panel.isHidden() and dialog.btn_advanced.text() == "고급 설정 열기"
        assert not dialog.chk_verify_copy.isChecked() and not dialog.chk_other_name.isChecked()
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        assert [worker.kwargs["import_mode"] for worker in _imports(created)] == ["replace"]
        assert "import_mode" not in created[0].kwargs  # the preview takes the payload without its mode
    finally:
        dialog.close()


@pytest.mark.parametrize(("setup", "expected"), [
    (lambda d: None, {"import_mode": "replace", "target_schema": "app", "threads": 8,
                      "timezone_sql": None, "use_source_timezone": True}),
    (lambda d: (d.chk_other_name.setChecked(True), d.combo_target_schema.setCurrentText(" other ")),
     {"target_schema": "other"}),
    (lambda d: d.chk_verify_copy.setChecked(True), {"import_mode": "safe"}),
    (lambda d: d.spin_threads.setValue(3), {"threads": 3}),
    (lambda d: (d.chk_timezone.setChecked(True), d.combo_timezone.setCurrentIndex(d.combo_timezone.findData("kst"))),
     {"timezone_sql": "SET SESSION time_zone = '+09:00'", "use_source_timezone": True}),
    (lambda d: (d.chk_timezone.setChecked(True), d.combo_timezone.setCurrentIndex(d.combo_timezone.findData("utc"))),
     {"timezone_sql": "SET SESSION time_zone = '+00:00'"}),
    # 서버 기본값 turns the dump's own timezone off.
    (lambda d: (d.chk_timezone.setChecked(True), d.combo_timezone.setCurrentIndex(d.combo_timezone.findData("none"))),
     {"timezone_sql": None, "use_source_timezone": False}),
    (lambda d: (d.chk_verify_copy.setChecked(True), d.chk_other_name.setChecked(True),
                d.combo_target_schema.setCurrentText("other")),
     {"import_mode": "safe", "target_schema": "other"}),
])
def test_import_toggle_payloads(monkeypatch, tmp_path, setup, expected):
    """Each toggle changes only its own field, and the preview gets the same payload without the mode."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    created = _fake_workers(monkeypatch)
    _fake_plan_dialog(monkeypatch, accept=True)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump", source_timezone=None))  # legacy: timezone offered
        dialog._update_target_options()
        setup(dialog)
        dialog.do_import()
        preview, imported = created
        default = {"input_dir": str(tmp_path / "dump"), "import_mode": "replace", "target_schema": "app", "threads": 8,
                   "timezone_sql": None, "use_source_timezone": True, "skip_triggers": False}
        assert imported.kwargs == {**default, **expected}
        assert preview.kwargs == {key: value for key, value in imported.kwargs.items() if key != "import_mode"}
    finally:
        dialog.close()


def test_plan_dialog_only_when_an_existing_table_or_view_is_overwritten(monkeypatch, tmp_path):
    """F12: the list appears only when the run overwrites something (or must show a blocker or a data
    warning); otherwise the run starts at once, on production too. A blocker never starts a run."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    for environment, preview, plan_shown, imports in (
        ("development", _NOTHING_OVERWRITTEN, False, 1),
        ("staging", _NOTHING_OVERWRITTEN, False, 1),
        ("production", _NOTHING_OVERWRITTEN, False, 1),
        ("development", {**_NOTHING_OVERWRITTEN, "replace": ["users"]}, True, 1),
        ("production", {**_NOTHING_OVERWRITTEN, "replace": ["users"]}, True, 1),
        # Replacing an existing view overwrites its definition.
        ("development", {**_NOTHING_OVERWRITTEN, "views": {"replace": ["v_users"], "create": []}}, True, 1),
        ("development", {**_NOTHING_OVERWRITTEN, "views": {"replace": [], "create": ["v_new"]}}, False, 1),
        # A warning that the data may be off is shown even when nothing is overwritten.
        ("development", {**_NOTHING_OVERWRITTEN, "snapshot_policy": "mysql_parallel_unsynchronized_snapshots",
                         "warnings": [{"code": "unsynchronized_snapshot", "detail": "x"}]}, True, 1),
        ("development", {**_NOTHING_OVERWRITTEN, "warnings": [{"code": "views_outside_snapshot", "detail": "x"}]}, False, 1),
        # The fake dialog accepts, as a dialog whose start button was enabled by mistake would: nothing starts.
        ("development", {**_NOTHING_OVERWRITTEN, "blockers": [{"code": "no_tables", "table": None, "detail": "x"}]}, True, 0),
        ("development", {**_NOTHING_OVERWRITTEN, "blockers": ["a blocker the app cannot read"]}, True, 0),
    ):
        created = _fake_workers(monkeypatch, preview=preview)
        shown = _fake_plan_dialog(monkeypatch, accept=True)
        dialog = RustDumpImportDialog(tunnel_config={"environment": environment})
        try:
            dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
            dialog.do_import()
            assert bool(shown) is plan_shown, (environment, preview)
            assert len(_imports(created)) == imports, (environment, preview)
        finally:
            dialog.close()


@pytest.mark.parametrize("preview", [
    {**_NOTHING_OVERWRITTEN, "dry_run": None},  # not a preview result at all
    {key: value for key, value in _NOTHING_OVERWRITTEN.items() if key != "replace"},
    {**_NOTHING_OVERWRITTEN, "replace": "users"},
    {**_NOTHING_OVERWRITTEN, "blockers": None},
    ["not", "a", "dict"],
])
def test_unreadable_preview_starts_nothing(monkeypatch, tmp_path, preview):
    """The one confirmation fails closed: a preview it cannot read is a failed preview, never 'nothing to show'."""
    dialog, shown = _failure_test_dialog(monkeypatch)
    created = _fake_workers(monkeypatch, preview=preview)
    dialog.tunnel_config = {"environment": "development"}
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        assert _imports(created) == []
        box = _open_failure_window(dialog)
        assert box.windowTitle() == "Import를 시작하지 않았습니다"
        assert box.text() == "실행 전 확인 중에 오류가 나서 Import를 시작하지 않았습니다. '자세히'에서 원문을 확인하세요."
        _click_details(box)
        assert shown and shown[0].startswith("Import 미리보기 오류: unexpected preview result")
    finally:
        dialog.close()


def test_rerun_while_the_preview_runs_starts_no_second_run(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    created = _fake_workers(monkeypatch, hold_preview=True)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.on_import_finished(False, "x", {"a": {"status": "error"}})  # an earlier failure shows the rerun button
        assert not dialog.btn_rerun.isHidden()
        dialog.btn_rerun.click()
        assert not dialog.btn_rerun.isEnabled()
        dialog.btn_rerun.click()
        dialog.do_import()  # even a direct call waits for the running preview
        assert [worker.task_type for worker in created] == ["import_preview"]
        preview = created[0]
        # A late answer of an earlier preview is dropped.
        monkeypatch.setattr(dialog, "sender", lambda: object())
        preview.preview_finished.emit(True, "", dict(_NOTHING_OVERWRITTEN))
        assert _imports(created) == [] and dialog._previewing
        monkeypatch.setattr(dialog, "sender", lambda: preview)
        preview.running = False
        preview.preview_finished.emit(True, "", dict(_NOTHING_OVERWRITTEN))
        assert len(_imports(created)) == 1
    finally:
        dialog._job_id = None
        dialog.close()


def test_closing_during_the_preview_stops_it_without_asking(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    monkeypatch.setattr(QMessageBox, "question", MagicMock(side_effect=AssertionError("the read-only check asks nothing")))
    scheduled = []
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QTimer.singleShot", lambda delay, callback: scheduled.append(callback))
    created = _fake_workers(monkeypatch, hold_preview=True)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        preview = created[0]
        dialog.close()
        assert preview.cancelled and dialog._cancel_requested and dialog._close_after_cancel
        preview.running = False
        preview.preview_finished.emit(False, "작업이 취소되었습니다.", {})
        assert _imports(created) == [] and scheduled == [dialog.close]
    finally:
        dialog.close()


def test_a_preview_answer_after_the_window_closed_starts_nothing(monkeypatch, tmp_path):
    """The thread ended (success already sent) just before the window closed: no plan dialog, no replace."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QTimer.singleShot", lambda delay, callback: None)
    created = _fake_workers(monkeypatch, hold_preview=True)
    _fake_plan_dialog(monkeypatch)  # fails the test if shown
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        preview = created[0]
        preview.running = False
        dialog.close()  # the thread already ended: the window closes at once
        assert dialog._close_after_cancel
        preview.preview_finished.emit(True, "", {**_NOTHING_OVERWRITTEN, "replace": ["users"]})
        assert _imports(created) == []
    finally:
        dialog.close()


def test_import_preview_failure_never_starts_import(monkeypatch, tmp_path):
    dialog, shown = _failure_test_dialog(monkeypatch)
    raw = "Rust DB Core import 미리보기 오류: dump_invalid: chunk checksum mismatch"
    created = _fake_workers(monkeypatch, preview={}, preview_ok=False, preview_message=raw)
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        assert [worker.task_type for worker in created] == ["import_preview"]
        box = _open_failure_window(dialog)
        assert box.windowTitle() == "Import를 시작하지 않았습니다"
        assert box.text() == "덤프 파일 내용에 문제가 있어 넣을 수 없습니다." and "checksum" not in box.text()
        _click_details(box)
        assert shown == [raw]
        assert dialog.btn_import.isEnabled() and dialog.btn_import.text() == "📥 Import 시작"
    finally:
        dialog.close()


def test_preview_refused_without_core_capability_starts_no_worker(monkeypatch, tmp_path):
    """A core built before the preview gets nothing: neither the preview nor an import."""
    from src.exporters import rust_dump_exporter

    class OldCore:
        client = MagicMock()

        def hello(self):
            return {"capabilities": ["dump.run", "dump.import"]}

        def preview_import(self, payload):
            raise AssertionError("an old core must not get the preview")

        def import_dump(self, payload, on_event=None):
            raise AssertionError("no import after a refused preview")

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter",
                        lambda config: rust_dump_exporter.RustDumpImporter(config, facade=OldCore()))
    monkeypatch.setattr(RustDumpWorker, "start", lambda self: self.run())  # run in this thread
    dialog, shown = _failure_test_dialog(monkeypatch)
    dialog.tunnel_config = {"environment": "development"}
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        assert shown == ["Import 엔진이 이 버전보다 오래되어 실행 전 확인을 할 수 없습니다. TunnelForge를 다시 설치하세요."]
        assert dialog.last_import_mode == "" and dialog.btn_import.isEnabled()
    finally:
        dialog.close()


def test_import_guard_reads_rust_manifest_namespace(tmp_path):
    import json
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({
        "database": "analytics", "source_schema": "reporting", "source_engine": "postgresql",
    }), encoding="utf-8")
    dialog = type("Dialog", (), {"connector": None})()
    assert RustDumpImportDialog._get_dump_schema_name(dialog, str(tmp_path)) == "reporting"


def test_import_default_description_discloses_per_table_replace_and_no_rollback(monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    dialog = RustDumpImportDialog()
    descriptions = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert "하나씩 지우고 다시 만들어" in descriptions
    assert "덤프에 없는 테이블은 그대로" in descriptions
    assert "이미 덮어쓴 테이블은 되돌리지 않고, 작업 중이던 테이블은 비어 있을 수 있습니다" in descriptions
    # Verify-in-copy leaves the original alone: the line under the target says so instead.
    dialog.chk_verify_copy.setChecked(True)
    verify = dialog.lbl_mode_description.text()
    assert verify.startswith("덤프를 먼저 복사본 DB에 넣어 검증하고") and "원본은 그대로입니다" in verify
    assert "지우고" not in verify
    for visible in (descriptions, verify, dialog.chk_verify_copy.text(), dialog.chk_other_name.text()):
        for jargon in ("preflight", "manifest", "namespace", "replace 모드", "safe restore", "안전 복원"):
            assert jargon not in visible
    dialog.chk_verify_copy.setChecked(False)
    assert "하나씩 지우고" in dialog.lbl_mode_description.text()
    dialog.close()


def test_replace_failure_details_name_lost_and_untouched_tables():
    from src.ui.dialogs.db_import_dialog import _capture_import_audit, _replace_failure_details
    audit = {}
    _capture_import_audit(audit, {
        "event": "import_report", "status": "failed",
        "dropped_not_restored_tables": ["orders"],
        "unattempted_tables": ["items", "logs"],
        "dropped_foreign_keys": [
            {"table": "items", "constraint": "fk_items_orders", "referenced_table": "orders"},
            {"table": "orders", "constraint": "fk_orders_users", "referenced_table": "users"},
        ],
    })
    details = _replace_failure_details(audit)
    assert "삭제 후 복원되지 않은 테이블: orders" in details
    assert "원본 데이터를 유지한 테이블(미실행): items, logs" in details
    assert "items.fk_items_orders" in details
    assert "orders.fk_orders_users" in details  # orders was never dropped: its original rows remain
    _capture_import_audit(audit, {"event": "import_report", "dropped_tables": ["orders"]})
    assert "orders.fk_orders_users" not in _replace_failure_details(audit)
    # A child FK the core linked again is no longer listed as removed.
    _capture_import_audit(audit, {"event": "import_report", "restored_foreign_keys": ["items.fk_items_orders"]})
    details = _replace_failure_details(audit)
    assert "원본 테이블에서 제거된 외래 키" not in details
    assert "다시 연결한 외래 키: items.fk_items_orders" in details
    assert "새로 만든 대상" not in details
    assert _replace_failure_details({}) == ""
    # A database the import created is named, so the user knows it was left in place.
    created = {}
    _capture_import_audit(created, {"event": "import_report", "namespace_created": True,
                                    "target": {"engine": "mysql", "schema": "new_db"}})
    assert _replace_failure_details(created) == "이번 Import에서 새로 만든 대상(지우지 않고 남겨 둠): new_db"
    # Tables of a target this import created never held original data.
    _capture_import_audit(created, {"event": "import_report", "unattempted_tables": ["items"]})
    assert "아직 만들지 않은 테이블: items" in _replace_failure_details(created)
    assert "원본 데이터를 유지" not in _replace_failure_details(created)
    # A CREATE of unknown outcome is named with its PostgreSQL database.
    unknown = {}
    _capture_import_audit(unknown, {"event": "import_report", "namespace_created": False,
                                    "namespace_creation_outcome_unknown": True, "unattempted_tables": ["items"],
                                    "target": {"engine": "postgresql", "database": "app", "schema": "new_schema"}})
    assert _replace_failure_details(unknown) == ("아직 만들지 않은 테이블: items\n"
                                                 "새 대상을 만들었는지 확인하지 못함: app.new_schema")


def test_forced_name_field_is_on_main_line(monkeypatch, tmp_path):
    """A dump that names no DB of this engine needs a typed name, on the target line, not in the closed panel."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    created = _fake_workers(monkeypatch)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "pg", source_engine="postgresql", source_schema="app"))
        dialog._update_target_options()
        assert dialog.chk_other_name.isChecked() and not dialog.chk_other_name.isEnabled()
        assert dialog.advanced_panel.isHidden()
        assert not dialog.combo_target_schema.isHidden() and dialog.combo_target_schema.isEditable()
        assert not dialog.advanced_panel.isAncestorOf(dialog.combo_target_schema)
        assert dialog.lbl_target_name.isHidden()
        # The main line says why the name field appeared; the field starts empty.
        assert not dialog.lbl_name_forced.isHidden()
        assert dialog.lbl_name_forced.text() == "이 덤프에는 여기서 쓸 DB 이름이 없어 직접 정해야 합니다."
        assert dialog.combo_target_schema.currentText() == ""
        assert dialog.lbl_advanced_summary.text() == "적용 중인 고급 설정: 다른 DB 이름(입력 필요)"
        # Another engine's dump cannot be verified in a copy either, and the panel says why.
        assert not dialog.chk_verify_copy.isEnabled() and not dialog.lbl_verify_note.isHidden()
        dialog.do_import()
        assert warnings == ["넣을 DB 이름을 정할 수 없습니다. '넣을 곳'에 DB 이름을 입력하세요."] and created == []
        dialog.combo_target_schema.setCurrentText("typed")
        dialog.do_import()
        assert _imports(created)[0].kwargs["target_schema"] == "typed"

        # A dump of this engine names its DB again: the name is no longer forced.
        dialog.input_dir.setText(_write_dump(tmp_path / "mysql"))
        dialog._update_target_options()
        assert not dialog.chk_other_name.isChecked() and dialog.chk_other_name.isEnabled()
        assert dialog.combo_target_schema.isHidden() and dialog.lbl_target_name.text() == "app"
        assert dialog.chk_verify_copy.isEnabled() and dialog.lbl_verify_note.isHidden()
        assert dialog.lbl_name_forced.isHidden()
    finally:
        dialog.close()


def test_other_name_field_starts_empty_even_with_existing_databases(monkeypatch, tmp_path):
    """The name list holds the server's DBs, but none is picked for the user: an unchanged field starts nothing."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    created = _fake_workers(monkeypatch)
    connector = MagicMock(host="127.0.0.1", port=3306, user="root", password="pw", engine="mysql")
    connector.get_schemas.return_value = ["first_db", "second_db"]
    dialog = RustDumpImportDialog(connector=connector, tunnel_config={"environment": "development"})
    try:
        assert dialog.combo_target_schema.count() == 2 and dialog.combo_target_schema.currentText() == ""
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog._update_target_options()
        dialog.combo_target_schema.setCurrentIndex(1)
        dialog.chk_other_name.setChecked(True)  # turning it on clears anything left in the field
        assert dialog.combo_target_schema.currentText() == ""
        dialog.do_import()
        assert warnings == ["넣을 DB 이름을 입력하세요."] and created == []
    finally:
        dialog.close()


def test_timezone_toggle_disabled_for_dump_with_source_timezone(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    dialog = RustDumpImportDialog()
    try:
        assert not dialog.chk_timezone.isEnabled() and dialog.lbl_timezone_note.isHidden()  # no dump yet
        dialog.input_dir.setText(_write_dump(tmp_path / "legacy", source_timezone=None))
        dialog._update_target_options()
        assert dialog.chk_timezone.isEnabled() and dialog.lbl_timezone_note.isHidden()
        dialog.chk_timezone.setChecked(True)
        assert dialog.combo_timezone.isEnabled()
        dialog.input_dir.setText(_write_dump(tmp_path / "current"))
        dialog._update_target_options()
        assert not dialog.chk_timezone.isEnabled() and not dialog.chk_timezone.isChecked()
        assert not dialog.combo_timezone.isEnabled()
        assert dialog.lbl_timezone_note.text() == "이 덤프에는 시간대가 기록되어 있어 그대로 씁니다."
        assert not dialog.lbl_timezone_note.isHidden()
    finally:
        dialog.close()


def test_advanced_summary_lists_active_toggles(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    dialog = RustDumpImportDialog()
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump", source_timezone=None))
        dialog._update_target_options()
        assert dialog.lbl_advanced_summary.text() == ""
        dialog.chk_other_name.setChecked(True)
        dialog.combo_target_schema.setCurrentText("abc")
        dialog.chk_verify_copy.setChecked(True)
        dialog.chk_timezone.setChecked(True)
        dialog.spin_threads.setValue(4)
        assert dialog.lbl_advanced_summary.text() == (
            "적용 중인 고급 설정: 다른 DB 이름(abc), 먼저 복사본에서 검증, 시간대 한국 시간(+09:00), 동시 작업 4개")
        dialog._toggle_advanced_options()
        assert not dialog.advanced_panel.isHidden() and dialog.btn_advanced.text() == "고급 설정 닫기"
    finally:
        dialog.close()


def _settings(values):
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: values.get(key, default)
    config_manager.set_app_settings.side_effect = values.update
    return config_manager


def test_only_threads_and_verify_in_copy_are_remembered(monkeypatch, tmp_path):
    """F5: the toggles are remembered; the other DB name and the timezone never are."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    _fake_workers(monkeypatch)
    saved = {}
    dialog = RustDumpImportDialog(config_manager=_settings(saved), tunnel_config={"environment": "development"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump", source_timezone=None))
        dialog._update_target_options()
        dialog.chk_other_name.setChecked(True)
        dialog.combo_target_schema.setCurrentText("other")
        dialog.chk_verify_copy.setChecked(True)
        dialog.chk_timezone.setChecked(True)
        dialog.spin_threads.setValue(3)
        dialog.do_import()
    finally:
        dialog.close()
    assert saved == {"rust_dump_import_dir": str(tmp_path / "dump"), "rust_dump_last_dump_dir": str(tmp_path / "dump"),
                     "rust_dump_import_threads": 3, "rust_dump_import_verify_in_copy": True}

    again = RustDumpImportDialog(config_manager=_settings(saved))
    try:
        assert again.spin_threads.value() == 3 and again.chk_verify_copy.isChecked()
        assert not again.chk_other_name.isChecked() and not again.chk_timezone.isChecked()
        assert again.combo_target_schema.currentText() != "other"
    finally:
        again.close()


@pytest.mark.parametrize("threads, verify", [("8", "true"), (None, 1), (99, None), (True, [True]), (0, "yes")])
def test_old_or_edited_settings_do_not_break_the_dialog(monkeypatch, threads, verify):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    dialog = RustDumpImportDialog(config_manager=_settings({
        "rust_dump_import_threads": threads, "rust_dump_import_verify_in_copy": verify,
        # Keys earlier versions might have left behind are ignored.
        "rust_dump_import_mode": "merge", "rust_dump_import_timezone": "kst"}))
    try:
        assert dialog.spin_threads.value() == 8 and not dialog.chk_verify_copy.isChecked()
        assert not dialog.chk_timezone.isChecked()
    finally:
        dialog.close()


@pytest.mark.parametrize("restore_status", ["ready_for_switch", "ready_for_review"])
def test_verified_copy_swaps_or_stops_without_any_handoff_button(monkeypatch, restore_status):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    monkeypatch.setattr(QMessageBox, "information", MagicMock(side_effect=AssertionError("success opens no window")))
    scheduled = []
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QTimer.singleShot", lambda delay, callback: scheduled.append(callback))
    dialog = RustDumpImportDialog()
    try:
        for removed in ("btn_copy_restore_target", "btn_review_restore"):
            assert not hasattr(dialog, removed)
        assert dialog.btn_manage_backups.isHidden()  # only after a run that made a copy
        dialog.last_import_mode = "safe"
        dialog.on_raw_output(json.dumps({
            "event": "safe_restore_ready", "status": restore_status, "verified": True,
            "original_unchanged": True, "cutover_pending": True, "candidate_created": True, "namespace_existed": True,
            "original_target": {"engine": "mysql", "database": "original"},
            "candidate_target": {"engine": "mysql", "host": "127.0.0.1", "port": 3333, "database": "candidate",
                                 "password": "private", "user": "private-user"},
        }))
        dialog.on_finished(True, "candidate ready")
        assert not dialog.btn_manage_backups.isHidden()  # the copy can be cleaned up from this window too
        if restore_status == "ready_for_switch":
            assert scheduled == [dialog._plan_promotion]  # the swap follows without asking again
        else:
            assert scheduled == []
            box = _open_failure_window(dialog)
            assert sorted(button.text() for button in box.buttons()) == ["닫기", "복원 백업 관리"]
            assert box.text().startswith("복사본 candidate에 넣고 검증했지만 다음 이유로 교체하지 않았습니다.")
            assert "'복원 백업 관리'에서 정리" in box.text() and "private" not in box.text()
            assert dialog.label_status.text() == "교체하지 않았습니다 · 원본은 그대로입니다"
    finally:
        dialog.close()


def test_backup_management_opens_with_this_sessions_credentials(monkeypatch, tmp_path):
    """A main-menu Import has no saved profile: the stop window and the dialog's button still open it."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    opened = []

    class FakeBackups:
        def __init__(self, target, dirs, parent):
            opened.append((target, dirs))

        def refresh(self):
            pass

        def exec(self):
            return 0

    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.BackupLifecycleDialog", FakeBackups)
    dialog = RustDumpImportDialog()
    try:
        dialog.restore_config = RustDumpConfig("127.0.0.1", 3306, "session-user", "session-pw")
        dialog.import_audit = {"report_path": str(tmp_path / "_tunnelforge_import_report_1.json"),
                               "candidate_created": True, "namespace_existed": True,
                               "original_target": {"engine": "mysql", "host": "127.0.0.1", "port": 3306, "database": "app"},
                               "candidate_target": {"engine": "mysql", "database": "tf_restore_1"}}
        dialog._stop_and_explain(["- x"])
        box = _open_failure_window(dialog)
        next(button for button in box.buttons() if button.text() == "복원 백업 관리").click()
        target, dirs = opened[0]
        assert target["user"] == "session-user" and target["password"] == "session-pw" and target["database"] == "app"
        assert dirs == [str(tmp_path)]
        dialog.btn_manage_backups.click()
        assert len(opened) == 2
    finally:
        dialog.close()


def test_plan_decline_starts_no_import_worker(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    created = _fake_workers(monkeypatch, preview={**_NOTHING_OVERWRITTEN, "replace": ["users"]})
    shown = _fake_plan_dialog(monkeypatch, accept=False)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "development"})
    dialog._begin_error_report_operation = MagicMock()
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        assert len(shown) == 1 and _imports(created) == []
        dialog._begin_error_report_operation.assert_not_called()
        assert dialog.btn_import.isEnabled() and dialog.input_dir.isEnabled() and dialog.btn_stop.isHidden()
        assert dialog.last_import_mode == ""
    finally:
        dialog.close()


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_import_success_has_no_modal(monkeypatch, engine):
    """Success puts one sentence on the status line and in the log, and opens no window."""
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.finish_promotion_job", lambda *a: None)
    dialog = RustDumpImportDialog()
    try:
        dialog.last_import_context = {"engine": engine}
        dialog.last_import_mode = "replace"
        dialog.import_results = {"users": {"status": "done"}, "orders": {"status": "done"}}
        dialog.on_finished(True, "Rust DB Core import 완료")
        # Every dump table counts, overwritten or new: "넣었습니다", never "교체했습니다".
        assert dialog.label_status.text() == "✅ 덤프 테이블 2개를 넣었습니다"
        assert dialog.txt_log.item(dialog.txt_log.count() - 1).text() == "✅ 덤프 테이블 2개를 넣었습니다"

        name = "app" if engine == "mysql" else "appdb.app"
        kind = "DB" if engine == "mysql" else "스키마"
        dialog.import_audit = {"created_namespace": name}
        dialog.on_finished(True, "Rust DB Core import 완료")
        assert dialog.label_status.text() == f"✅ 새 {kind}를 만들어 넣었습니다: {name}"

        dialog.last_import_mode = "safe"
        dialog.import_audit = {"restore_status": "completed_new_target", "namespace_existed": False,
                               "namespace_created": True, "verified": True, "original_unchanged": True,
                               "cutover_pending": False,
                               "candidate_target": {"engine": engine, "database": "appdb" if engine == "postgresql" else "app",
                                                    "schema": "app"}}
        dialog.on_finished(True, "verified")
        assert dialog.label_status.text() == f"✅ 새 {kind}를 만들어 넣었습니다: {name}"

        dialog._promotion_action = "confirm"
        dialog.import_audit["original_target"] = {"engine": engine, "database": "app"}
        dialog._on_promotion_finished(True, "", {"status": "promoted", "backup_namespace": "tf_backup_1"})
        assert dialog.label_status.text() == f"✅ 교체했습니다. 이전 테이블은 백업 {kind} tf_backup_1에 있습니다"
    finally:
        dialog.close()


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


def test_failed_import_offers_a_full_rerun_instead_of_a_dead_end_table_selection(monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    dialog = RustDumpImportDialog()
    try:
        from PyQt6.QtWidgets import QAbstractItemView
        assert not hasattr(dialog, "btn_select_failed") and not hasattr(dialog, "btn_retry")
        assert dialog.table_list.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
        assert dialog.btn_rerun.isHidden()
        dialog.on_import_finished(False, "x", {"a": {"status": "done"}, "b": {"status": "error"}})
        assert not dialog.btn_rerun.isHidden() and not dialog.label_rerun_hint.isHidden()
        dialog.do_import = MagicMock()
        dialog.btn_rerun.click()
        dialog.do_import.assert_called_once_with()  # full rerun with the same settings, never a table subset
    finally:
        dialog.close()


def test_import_and_export_titles_do_not_expose_the_engine_name(monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    dialog = RustDumpImportDialog()
    try:
        assert dialog.windowTitle() == "데이터 Import"
    finally:
        dialog.close()


def test_import_stop_button_cancels_without_closing_and_shows_final_status_and_report(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog = RustDumpImportDialog()
    try:
        assert dialog.btn_stop.isHidden(), "stop is offered only while running"
        dialog.set_ui_enabled(False)
        assert not dialog.btn_stop.isHidden() and dialog.btn_stop.isEnabled()
        dialog.worker = MagicMock()
        dialog.worker.isRunning.return_value = True

        dialog.btn_stop.click()

        dialog.worker.cancel.assert_called_once_with()
        assert dialog._cancel_requested is True and dialog._close_after_cancel is False
        assert dialog.btn_stop.text() == "중지 중…" and not dialog.btn_stop.isEnabled()
        assert dialog.label_status.text() == "⏹ 중지 중…"

        from datetime import datetime
        dialog.import_start_time = datetime.now()
        report = tmp_path / "_tunnelforge_import_report_1_2_0.json"
        report.write_text("{}", encoding="utf-8")
        dialog.on_raw_output(json.dumps({"event": "import_report", "status": "running", "report_path": str(report)}))
        dialog.last_input_dir = str(tmp_path)
        dialog.last_import_mode = "replace"
        dialog.import_results = {"a": {"status": "done"}, "b": {"status": "blocked"}}
        dialog.worker.isRunning.return_value = False
        closed = []
        monkeypatch.setattr(dialog, "close", lambda: closed.append(True))
        dialog.on_finished(False, "cancelled")
        app.processEvents()

        assert closed == [], "stopping keeps the dialog open"
        assert dialog.label_status.text().startswith("⏹ Import 중지됨")
        assert dialog.btn_stop.isHidden()
        assert not dialog.label_report_link.isHidden()
        assert report.name in dialog.label_report_link.text()
    finally:
        dialog.worker = None
        type(dialog).close(dialog)


def test_import_close_while_running_still_stops_and_closes_after_finish(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)
    dialog = RustDumpImportDialog()
    try:
        dialog.worker = MagicMock()
        dialog.worker.isRunning.return_value = True
        dialog.request_stop(close_after=False)
        dialog.close()  # stop already requested: closing only schedules close-after-finish, no second prompt
        assert asked == [1] and dialog._close_after_cancel is True
        dialog.worker.cancel.assert_called_once_with()
    finally:
        dialog.worker = None
        type(dialog).close(dialog)


def _stop_test_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    for name in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: None)
    dialog = RustDumpImportDialog()
    dialog._test_app = app  # keep the QApplication alive while the dialog exists
    return dialog


@pytest.mark.parametrize("mode, action, expected", [
    ("replace", "", "이미 덮어쓴 테이블은 되돌리지 않고, 작업 중이던 테이블은 비어 있을 수 있습니다"),
    ("safe", "", "복사본에 넣는 중입니다. 지금 중지해도 원본은 그대로입니다."),
    ("safe", "confirm", "교체하는 중입니다. 지금 중지하면 교체가 끝났는지 알 수 없게 될 수 있습니다."),
    ("safe", "plan", None),  # the check before the swap only reads: it stops without asking
])
def test_stop_question_says_what_stopping_leaves_behind(monkeypatch, mode, action, expected):
    asked = []
    dialog = _stop_test_dialog(monkeypatch)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda parent, title, text, *a, **k: asked.append(text) or QMessageBox.StandardButton.Yes)
    try:
        dialog.worker = MagicMock()
        dialog.worker.isRunning.return_value = True
        dialog.last_import_mode, dialog._promotion_action = mode, action
        assert dialog.request_stop() is True
        dialog.worker.cancel.assert_called_once_with()
        if expected is None:
            assert asked == []
        else:
            assert len(asked) == 1 and expected in asked[0] and asked[0].endswith("중지하시겠습니까?")
            assert "Rust DB Core" not in asked[0] and "취소" not in asked[0]
    finally:
        dialog.worker = None
        type(dialog).close(dialog)


def test_stopped_safe_import_shows_stopped_status_not_safe_restore_failure(monkeypatch):
    from datetime import datetime as _dt
    dialog = _stop_test_dialog(monkeypatch)
    try:
        dialog.import_start_time = _dt.now()
        dialog.last_import_mode = "safe"  # overwrite also runs as safe
        dialog.import_audit = {"original_unchanged": True}
        dialog.import_results = {"a": {"status": "done"}, "b": {"status": "blocked"}}
        dialog._cancel_requested = True
        dialog.on_finished(False, "cancelled")
        assert dialog.label_status.text().startswith("⏹ Import 중지됨")
    finally:
        dialog.worker = None
        type(dialog).close(dialog)


def test_report_link_shows_only_the_report_the_core_named_for_this_run(monkeypatch, tmp_path):
    from datetime import datetime as _dt
    dialog = _stop_test_dialog(monkeypatch)
    try:
        # Earlier attempts' reports in the same dump folder, even freshly written, belong to other runs.
        (tmp_path / "_tunnelforge_import_report.json").write_text("{}", encoding="utf-8")
        (tmp_path / "_tunnelforge_import_report_1_1_0.json").write_text("{}", encoding="utf-8")
        dialog.last_input_dir = str(tmp_path)
        dialog.import_audit = {}
        dialog.import_start_time = _dt.now()
        dialog._show_report_link()
        assert dialog.label_report_link.isHidden(), "a report the core did not name is not this run's report"
        own = tmp_path / "_tunnelforge_import_report_2_1_0.json"
        own.write_text("{}", encoding="utf-8")
        dialog.import_audit = {"report_path": str(own)}
        dialog._show_report_link()
        assert not dialog.label_report_link.isHidden()
        assert own.name in dialog.label_report_link.text()
    finally:
        type(dialog).close(dialog)


def test_promotion_plan_step_has_no_stop_button_and_cutover_stop_shows_stopped_status(monkeypatch, tmp_path):
    from datetime import datetime as _dt
    dialog = _stop_test_dialog(monkeypatch)

    class FakeWorker:
        def __init__(self, *a, **k):
            self.raw_output = MagicMock()
            self.promotion_finished = MagicMock()

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", FakeWorker)
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.begin_promotion_job", lambda *a: None)
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.finish_promotion_job", lambda *a: None)
    try:
        dialog._start_promotion({"action": "plan"})
        assert dialog.btn_stop.isHidden(), "the read-only plan step changes nothing, so there is nothing to stop"
        dialog._start_promotion({"action": "confirm"})
        assert not dialog.btn_stop.isHidden()

        dialog.import_start_time = _dt.now()
        report = tmp_path / "_tunnelforge_import_report.json"
        report.write_text("{}", encoding="utf-8")
        dialog.import_audit["report_path"] = str(report)
        dialog._cancel_requested = True
        dialog._on_promotion_finished(False, "cancelled", {"original_unchanged": False})
        assert dialog.label_status.text().startswith("⏹ 교체 중지됨 · 원본·백업·복사본 상태")
        assert not dialog.label_report_link.isHidden()
        dialog._on_promotion_finished(False, "cancelled", {"original_unchanged": True})
        assert dialog.label_status.text() == "⏹ 교체 중지됨 · 원본은 그대로입니다"
    finally:
        dialog.worker = None
        type(dialog).close(dialog)


def _failure_test_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    shown = []
    for name in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda parent, title, text, *a, **k: shown.append(text))
    dialog = RustDumpImportDialog()
    dialog._test_app = app  # keep the QApplication alive
    return dialog, shown


def _open_failure_window(dialog):
    boxes = [box for box in dialog.findChildren(QMessageBox) if box.isVisible()]
    assert len(boxes) == 1
    return boxes[0]


def _click_details(box):
    next(button for button in box.buttons() if button.text() == "자세히").click()


_LOAD_FAILED = "이 테이블을 지우고 다시 만들거나 데이터를 넣는 중에 멈췄습니다."


def _safe_dump_dir(tmp_path):
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({
        "format": "tunnelforge-dump", "format_version": 5, "source_engine": "mysql", "database": "app",
        "tables": [{"name": "users", "path": "0001_users", "rows": 0, "chunks": 0}],
    }), encoding="utf-8")
    return str(tmp_path)


def test_import_failure_window_shows_first_cause(monkeypatch):
    dialog, shown = _failure_test_dialog(monkeypatch)
    raw = ("Rust DB Core import 오류: load_failed: orders: create_table failed: "
           "MySqlError { ERROR 1118 (42000): Row size too large }")
    try:
        dialog.last_import_mode = "replace"
        dialog.import_results = {"customers": {"status": "done"}, "orders": {"status": "error", "message": raw},
                                 "items": {"status": "blocked"}}
        dialog.on_finished(False, raw)

        box = _open_failure_window(dialog)
        assert f"orders: {_LOAD_FAILED}" in box.text()
        assert "실패: 1개 테이블" in box.text()
        assert "MySqlError" not in box.text() and "create_table" not in box.text()
        assert sorted(button.text() for button in box.buttons()) == ["닫기", "자세히"]
        assert shown == []
        _click_details(box)
        assert shown == [raw]
        assert box.isVisible()  # the details open beside the failure window, which stays
    finally:
        dialog.close()


def test_import_failure_before_any_table_words_every_code(monkeypatch):
    dialog, shown = _failure_test_dialog(monkeypatch)
    raw = ("Rust DB Core import 오류: ddl_preflight_failed: orders: server rejected create_table before target "
           "replacement: MySqlError { ERROR 1118 (42000): Row size too large }; additionally: ddl_probe_cleanup_failed: "
           "owned probes tf_p_0 could not be removed; original target tables were not changed: MySqlError { x }")
    cause = ("orders: 대상에 테이블을 만들 수 없어 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다. "
             "확인용으로 잠시 만든 테이블을 지우지 못했습니다. 원래 테이블은 바뀌지 않았습니다.")
    try:
        dialog.last_import_mode = "replace"
        dialog.import_results = {"orders": {"status": "pending"}}
        dialog.on_finished(False, raw)

        box = _open_failure_window(dialog)
        assert f"❌ {cause}" in box.text() and "MySqlError" not in box.text()
        assert f"❌ 실패: {cause}" in [dialog.txt_log.item(index).text() for index in range(dialog.txt_log.count())]
        _click_details(box)
        assert shown == [raw]
    finally:
        dialog.close()


def test_an_uncoded_core_failure_keeps_its_raw_text_behind_details(monkeypatch, tmp_path):
    dialog, shown = _failure_test_dialog(monkeypatch)
    raw = "Rust DB Core import 오류: unexpected end of stream"
    try:
        dialog.last_import_mode = "replace"
        dialog.import_results = {"orders": {"status": "pending"}}
        dialog.on_finished(False, raw)
        box = _open_failure_window(dialog)
        assert box.text() == "❌ Import 중에 오류가 나서 멈췄습니다. '자세히'에서 원문을 확인하세요."
        _click_details(box)
        assert shown == [raw]
        box.close()

        created = _fake_workers(monkeypatch, preview_ok=False, preview_message="Rust DB Core import 미리보기 오류: boom")
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.do_import()
        assert _imports(created) == []
        box = _open_failure_window(dialog)
        assert box.text() == "실행 전 확인 중에 오류가 나서 Import를 시작하지 않았습니다. '자세히'에서 원문을 확인하세요."
    finally:
        dialog.close()


def test_safe_restore_failure_passes_core_reason_through(monkeypatch, tmp_path):
    from src.exporters.rust_dump_exporter import RustDumpImporter

    reason = "load_failed: users: create_table failed: MySqlError { ERROR 1822 (HY000): Failed to add the foreign key }"
    blocker = "The original namespace contains tables absent from this dump; preserve them before switching."

    class FakeFacade:
        def import_dump(self, payload, on_event=None):
            return {"success": False, "mode": "safe", "status": "failed_original_untouched", "original_unchanged": True,
                    "message": f"Safe restore failed; original database was not changed. {reason}",
                    "error": reason, "blockers": [blocker]}

    importer = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "pw"), facade=FakeFacade())
    success, message, _ = importer.import_dump(_safe_dump_dir(tmp_path), import_mode="safe")
    assert success is False
    assert message == f"{reason}\n{blocker}"
    assert importer.last_error_code == "LOAD_FAILED:MYSQL-1822"

    dialog, shown = _failure_test_dialog(monkeypatch)
    try:
        dialog.last_import_mode = "safe"
        dialog.import_audit = {"original_unchanged": True}
        dialog.on_finished(False, message)

        box = _open_failure_window(dialog)
        assert box.windowTitle() == "Import 실패" and "원본은 그대로입니다" in box.text()
        assert f"users: {_LOAD_FAILED}" in box.text()
        assert "MySqlError" not in box.text() and "absent from this dump" not in box.text()
        assert "남겨 둠" not in box.text()  # no copy was made
        _click_details(box)
        assert shown == [f"{reason}\n{blocker}"]
    finally:
        dialog.close()


@pytest.mark.parametrize(("namespace_existed", "report_write_failed", "copy_left"), [
    (True, False, "이번 Import에서 만든 복사본(지우지 않고 남겨 둠): tf_restore_1"),
    # Without a target, the copy is the requested database itself.
    (False, True, "이번 Import에서 새로 만든 대상(일부만 들어갔을 수 있음, 지우지 않고 남겨 둠): app"),
])
def test_safe_restore_failure_names_the_copy_it_left(monkeypatch, tmp_path, namespace_existed, report_write_failed,
                                                    copy_left):
    from src.exporters.rust_dump_exporter import RustDumpImporter

    reason = ("load_failed: users: load_rows failed: mysql insert error: "
              "MySqlError { ERROR 1406 (22001): Data too long for column 'name' at row 1 }")
    # A failed final report write keeps the first error and says so only in the message.
    message = (f"{reason}; final safe restore report could not be persisted: disk full. Original namespace was not changed."
               if report_write_failed else f"Safe restore failed; original database was not changed. {reason}")
    candidate = "tf_restore_1" if namespace_existed else "app"

    class FakeFacade:
        def import_dump(self, payload, on_event=None):
            return {"success": False, "mode": "safe", "status": "failed_original_untouched", "original_unchanged": True,
                    "candidate_created": True, "namespace_created": True, "namespace_existed": namespace_existed,
                    "candidate_target": {"engine": "mysql", "host": "localhost", "port": 3306, "database": candidate},
                    "report_write_failed": report_write_failed, "message": message, "error": reason}

    lines = []
    importer = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "pw"), facade=FakeFacade())
    success, failure, _ = importer.import_dump(_safe_dump_dir(tmp_path), import_mode="safe",
                                               raw_output_callback=lines.append)
    assert success is False and failure == (message if report_write_failed else reason)

    dialog, shown = _failure_test_dialog(monkeypatch)
    try:
        dialog.last_import_mode = "safe"
        for line in lines:
            dialog.on_raw_output(line)
        dialog.on_finished(False, failure)

        text = _open_failure_window(dialog).text()
        assert f"users: {_LOAD_FAILED}" in text and copy_left in text
        assert ("Import 보고서를 저장하지 못했습니다." in text) is report_write_failed
        assert "MySqlError" not in text
    finally:
        dialog.close()


@pytest.mark.parametrize("programs", [[], [{"kind":"PROCEDURE","name":"p"}], [{"kind":"TRIGGER","table":"users","name":"guard"}]])
def test_skip_trigger_toggle_follows_dump_and_locks_during_preview(monkeypatch, tmp_path, programs):
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda:(True,"ok"))
    created=_fake_workers(monkeypatch,hold_preview=True)
    dialog=RustDumpImportDialog()
    try:
        dialog.input_dir.setText(_write_dump(tmp_path/"dump", stored_programs=programs))
        applies=any(p["kind"]=="TRIGGER" for p in programs)
        assert dialog.chk_skip_triggers.isEnabled() is applies
        if applies: dialog.chk_skip_triggers.setChecked(True)
        dialog.do_import()
        assert created and not dialog.chk_skip_triggers.isEnabled()
        dialog.set_ui_enabled(True)
        assert dialog.chk_skip_triggers.isEnabled() is applies
    finally:
        for worker in created: worker.running=False
        dialog.worker=None
        dialog.close()


def test_program_partial_has_warning_outcome_once_and_no_anonymous_report(monkeypatch):
    from src.core.job_history import STATUS_PARTIAL
    from src.ui.dialogs import job_recording
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda:(True,"ok"))
    finish=MagicMock()
    monkeypatch.setattr(job_recording,"job_finish",finish)
    dialog=RustDumpImportDialog()
    try:
        dialog._job_id="job-partial"
        dialog.last_import_mode="replace"
        dialog.import_results={"users":{"status":"done"}}
        dialog.import_audit={"status":"partial","data_imported":True,"program_warnings":[{"code":"routine_binlog_unsafe","name":"f"}]}
        dialog._show_failure=MagicMock()
        dialog._report_error_anonymously=MagicMock()
        message="테이블 데이터는 가져왔지만 일부 객체를 복원하지 못했습니다.\nf: binlog warning"
        dialog.on_finished(False,message)
        assert "일부 완료" in dialog.label_status.text()
        assert dialog._show_failure.call_args.args[1].count("binlog warning")==1
        dialog._report_error_anonymously.assert_not_called()
        assert finish.call_args.args[1]==STATUS_PARTIAL
        assert any("binlog warning" in dialog.txt_log.item(i).text() for i in range(dialog.txt_log.count()))
    finally: dialog.close()
