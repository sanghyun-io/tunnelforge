import json
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.ui.dialogs.db_dialogs import (
    RustDumpExportDialog,
    RustDumpImportDialog,
    RustDumpWizard,
    build_safe_recreate_capacity_notice,
    cap_incomplete_export_percent,
    displayed_import_percent,
    folder_size_bytes,
    is_import_ddl_detail_message,
    import_phase_label,
    import_stage_percent,
    plain_import_progress_message,
    format_import_visible_telemetry,
    format_storage_size,
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


def test_format_import_row_labels_prefers_overall_rows_when_provided():
    labels = format_import_row_labels({
        "table": "df_subs",
        "rows_done": 50_000,
        "rows_total": 100_000,
        "overall_rows_done": 750_000,
        "overall_rows_total": 3_000_000,
        "chunk_rows": 50_000,
    })

    assert labels[0] == "📦 전체 rows: 750,000 / 3,000,000 rows"


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


def test_import_stage_percent_keeps_data_load_below_final_completion():
    assert import_stage_percent(100, "dump_import") == 85
    assert import_stage_percent(100, "dump_import_post_load") == 88
    assert import_stage_percent(100, "dump_import_objects") == 96


def test_import_phase_label_uses_plain_stage_names():
    assert import_phase_label("dump_import_post_load") == "인덱스/FK 생성"
    assert import_phase_label("dump_import_objects") == "View/이벤트/루틴/트리거 복원"


def test_plain_import_progress_message_translates_post_load():
    assert plain_import_progress_message("creating indexes and foreign keys") == "🔄 인덱스/FK 생성 중..."


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


def test_format_import_visible_telemetry_hides_local_infile_phase_noise():
    line = format_import_visible_telemetry({
        "event": "phase",
        "message": "MySQL local_infile is disabled; using safe Rust INSERT fallback",
        "strategy": "insert_fallback",
    })

    assert line is None


def test_format_import_visible_telemetry_translates_phase_message():
    line = format_import_visible_telemetry({
        "event": "phase",
        "phase": "dump_import_post_load",
        "message": "creating indexes and foreign keys",
    })

    assert line == "인덱스/FK 생성: 🔄 인덱스/FK 생성 중..."


def test_format_import_visible_telemetry_shows_post_load_ddl_detail():
    line = format_import_visible_telemetry({
        "event": "ddl_progress",
        "phase": "dump_import_post_load",
        "kind": "foreign_key",
        "table": "orders",
        "name": "fk_orders_users",
        "current": 2,
        "total": 8,
        "status": "running",
    })

    assert line == "FK 생성 중: orders.fk_orders_users (2/8)"


def test_import_ddl_detail_message_is_identified_for_deduplication():
    assert is_import_ddl_detail_message("인덱스 생성 중: df_subs.idx_name (195/658)")
    assert is_import_ddl_detail_message("FK 생성 완료: orders.fk_orders_users (8/8)")
    assert not is_import_ddl_detail_message("인덱스/FK 생성: 🔄 인덱스/FK 생성 중...")


def test_safe_recreate_capacity_notice_reports_dump_size(tmp_path, monkeypatch):
    dump_dir = tmp_path / "dump"
    table_dir = dump_dir / "0001_users"
    table_dir.mkdir(parents=True)
    (table_dir / "chunk_000001.tsv").write_bytes(b"x" * 2048)
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.shutil.disk_usage",
        lambda path: (100_000, 10_000, 90_000),
    )

    assert folder_size_bytes(str(dump_dir)) == 2048
    assert format_storage_size(2048) == "2.0 KB"

    notice = build_safe_recreate_capacity_notice(str(dump_dir))

    assert "안전 재생성 Import는 임시 스키마에 먼저 복원" in notice
    assert "덤프 폴더 크기: 2.0 KB" in notice
    assert "권장 여유 공간(대략): 6.0 KB 이상" in notice
    assert "현재 덤프 드라이브 여유 공간: 87.9 KB" in notice


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
    assert "[rust_dump]" in dialog.log_entries[1]


def test_import_raw_output_does_not_save_duplicate_ddl_summary():
    app = QApplication.instance() or QApplication([])

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
    dialog.label_status = QLabel()
    dialog.label_fk_status = QLabel()
    dialog._add_log = lambda message: dialog.log_entries.append(f"[00:00:00] {message}")

    RustDumpImportDialog.on_raw_output(dialog, json.dumps({
        "event": "ddl_progress",
        "phase": "dump_import_post_load",
        "kind": "foreign_key",
        "table": "orders",
        "name": "fk_orders_users",
        "current": 2,
        "total": 8,
        "status": "running",
    }))

    assert dialog.txt_log.items == ["FK 생성 중: orders.fk_orders_users (2/8)"]
    assert len(dialog.log_entries) == 1
    assert "[rust_dump]" in dialog.log_entries[0]
    assert "ddl_progress" in dialog.log_entries[0]


def test_import_dialog_phase_progress_includes_post_load_stage(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)
    dialog.import_data_percent = 100
    dialog.import_display_percent = 85
    dialog.progress_bar.setValue(85)

    dialog.on_raw_output(json.dumps({
        "event": "phase",
        "phase": "dump_import_post_load",
        "message": "creating indexes and foreign keys",
    }))

    assert dialog.progress_bar.value() == 88
    assert "인덱스/FK 생성" in dialog.label_percent.text()
    assert "FK/인덱스: 생성 중" in dialog.label_fk_status.text()
    dialog.close()


def test_import_dialog_failure_preserves_stage_progress(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.QMessageBox.warning", lambda *args, **kwargs: None)
    dialog = RustDumpImportDialog(connector=None)
    dialog.import_results = {"users": {"status": "done", "message": ""}}
    dialog.import_data_percent = 100
    dialog.import_display_percent = 88
    dialog.import_current_phase_label = "인덱스/FK 생성"
    dialog.progress_bar.setValue(88)

    dialog.on_finished(False, "creating indexes and foreign keys failed")

    assert dialog.progress_bar.value() == 88
    assert "실패 지점: 인덱스/FK 생성" in dialog.label_percent.text()
    assert "Import 실패: 인덱스/FK 생성 단계" in dialog.label_status.text()
    dialog.close()


def test_rust_dump_export_dialog_defaults_to_zstd():
    app = QApplication.instance() or QApplication([])
    dialog = RustDumpExportDialog()

    assert dialog.combo_compression.currentText() == "zstd"
    dialog.close()


def test_rust_dump_export_dialog_defaults_to_best_effort_consistency():
    app = QApplication.instance() or QApplication([])
    dialog = RustDumpExportDialog()

    assert dialog.combo_consistency_mode.currentData() == "best_effort"
    assert "자동" in dialog.combo_consistency_mode.currentText()
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
    combined = "\n".join(label.text() for label in dialog.findChildren(QLabel))

    assert "모든 객체" not in combined
    assert "테이블" in combined
    dialog.close()


def test_import_dialog_enables_limited_import_with_warning_state(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)
    dialog._set_dump_compatibility(
        {
            "restorability": "limited_restorable",
            "warnings": ["snapshot consistency is not proven"],
            "blockers": [],
        }
    )

    assert dialog.btn_import.isEnabled()
    assert "제한적 복원" in dialog.lbl_dump_compatibility.text()
    dialog.close()


def test_import_dialog_limited_compatibility_uses_plain_language_for_backup_admin(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)
    dialog._set_dump_compatibility(
        {
            "restorability": "limited_restorable",
            "warnings": [
                "snapshot consistency is not proven",
                "strict parallel snapshot unavailable; exported as limited restore dump: "
                "mysql strict parallel snapshot requires LOCK INSTANCE FOR BACKUP privilege: "
                "MySqlError { ERROR 1227 (42000): Access denied; you need (at least one of) "
                "the BACKUP_ADMIN privilege(s) for this operation }",
            ],
            "blockers": [],
        }
    )

    text = dialog.lbl_dump_compatibility.text()
    assert "Import는 가능" in text
    assert "같은 한 시점" in text
    assert "서로 다른 시점" in text
    assert "BACKUP_ADMIN 권한" in text
    assert "snapshot" not in text
    assert "strict" not in text
    assert "MySqlError" not in text
    assert "ERROR 1227" not in text
    dialog.close()


def test_import_dialog_initial_import_button_disabled_until_compatibility_checked(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)

    assert not dialog.btn_import.isEnabled()
    dialog.close()


def test_import_dialog_enables_recommended_import_for_strict_dump(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)
    dialog._set_dump_compatibility(
        {
            "restorability": "strict_restorable",
            "warnings": [],
            "blockers": [],
        }
    )

    assert dialog.btn_import.isEnabled()
    assert "엄격 복원 가능 Dump" in dialog.lbl_dump_compatibility.text()
    dialog.close()


def test_import_dialog_compatibility_text_includes_blockers_and_warnings(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)
    dialog._set_dump_compatibility(
        {
            "restorability": "not_restorable",
            "warnings": ["w1", "shared"],
            "blockers": ["b1", "shared"],
        }
    )

    text = dialog.lbl_dump_compatibility.text()
    assert "복원 불가 Dump" in text
    assert "b1" in text
    assert "w1" in text
    assert text.index("b1") < text.index("w1")
    assert text.count("shared") == 1
    dialog.close()


def test_import_dialog_limited_dump_starts_after_confirmation_with_non_strict_manifest(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps(
            {
                "format": "tunnelforge-dump",
                "format_version": 3,
                "database": "app",
                "restorability": "limited_restorable",
                "manifest_warnings": ["snapshot consistency is not proven"],
                "blockers": [],
                "tables": [],
            }
        ),
        encoding="utf-8",
    )

    question_calls = []
    worker_kwargs = {}
    worker_started = {"value": False}

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            worker_kwargs.update(kwargs)
            self.progress = FakeSignal()
            self.table_progress = FakeSignal()
            self.detail_progress = FakeSignal()
            self.table_status = FakeSignal()
            self.raw_output = FakeSignal()
            self.import_finished = FakeSignal()
            self.finished = FakeSignal()
            self.metadata_analyzed = FakeSignal()
            self.table_chunk_progress = FakeSignal()

        def start(self):
            worker_started["value"] = True

    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.QMessageBox.question",
        lambda *args, **kwargs: question_calls.append((args, kwargs)) or QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.RustDumpWorker", FakeWorker)

    dialog = RustDumpImportDialog(connector=None)
    dialog.input_dir.setText(str(dump_dir))

    dialog.do_import()

    assert question_calls
    assert "제한적 복원" in question_calls[0][0][2]
    assert worker_started["value"] is True
    assert worker_kwargs["import_mode"] == "replace"
    assert worker_kwargs["progress_policy"] == "reset"
    assert worker_kwargs["strict_manifest"] is False
    dialog.close()


def test_import_dialog_manual_strict_dump_path_enables_import(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))

    class FakeAnalysisResult:
        compatibility_issues = []

    class FakeDumpFileAnalyzer:
        def analyze_dump_folder(self, dump_path):
            return FakeAnalysisResult()

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.DumpFileAnalyzer", FakeDumpFileAnalyzer)

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps(
            {
                "format": "tunnelforge-dump",
                "format_version": 3,
                "database": "app",
                "restorability": "strict_restorable",
                "manifest_warnings": [],
                "blockers": [],
                "tables": [],
            }
        ),
        encoding="utf-8",
    )

    dialog = RustDumpImportDialog(connector=None)
    dialog.input_dir.setText(str(dump_dir))
    dialog._on_input_dir_editing_finished()

    assert dialog.btn_import.isEnabled()
    assert "엄격 복원 가능 Dump" in dialog.lbl_dump_compatibility.text()
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
