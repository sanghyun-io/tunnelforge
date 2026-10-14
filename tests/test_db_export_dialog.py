import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.exporters.rust_dump_exporter import RustDumpConfig, describe_snapshot_method
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

# 모듈 동안 QApplication 을 유지한다. 테스트 지역 변수로만 잡으면 GC 순서에 따라
# 이후 테스트가 QApplication 없이 위젯을 만들다 프로세스째 중단된다.
_app = QApplication.instance() or QApplication([])

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


def _privilege_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.QMessageBox.warning",
        lambda *_args: None,
    )
    dialog = RustDumpExportDialog()
    dialog._test_app = app  # keep QApplication alive for the dialog's lifetime
    dialog.export_schema = "app"
    dialog.input_output_dir.setText("C:/tmp/tunnelforge-export")
    dialog.spin_threads.setValue(6)
    dialog._start_export_worker = MagicMock()
    dialog._report_error_anonymously = MagicMock()
    return dialog


def _privilege_marker(privilege):
    return (
        "Rust DB Core export 오류: "
        f"MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:{privilege}:ERROR 1227"
    )


def _buttons_by_name(message_box):
    return {button.objectName(): button for button in message_box.buttons()}


def test_first_mysql_privilege_dialog_recommends_table_lock(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    try:
        message_box = dialog._create_mysql_privilege_dialog()
        buttons = _buttons_by_name(message_box)

        assert message_box.windowTitle() == "병렬 Export 권한 필요"
        assert {name: button.text() for name, button in buttons.items()} == {
            "mysql_parallel_table_lock_continue": "병렬로 계속 (테이블 락, 권장)",
            "mysql_parallel_unsynchronized_continue": "병렬로 계속 (시점 일관성 보장 안 함)",
            "mysql_single_connection_continue": "단일 연결로 계속",
            "mysql_privilege_cancel": "취소",
        }
        assert message_box.defaultButton().objectName() == "mysql_parallel_table_lock_continue"
        assert message_box.escapeButton().objectName() == "mysql_privilege_cancel"
        for expected in ("RELOAD", "BACKUP_ADMIN", "최대 2초", "쓰기는 대기"):
            assert expected in message_box.text()
    finally:
        dialog.close()


def test_lock_tables_denied_dialog_drops_the_table_lock_option(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    try:
        message_box = dialog._create_mysql_privilege_dialog(table_lock_denied=True)
        buttons = _buttons_by_name(message_box)

        assert set(buttons) == {
            "mysql_single_connection_continue",
            "mysql_parallel_unsynchronized_continue",
            "mysql_privilege_cancel",
        }
        assert message_box.defaultButton().objectName() == "mysql_single_connection_continue"
        assert message_box.escapeButton().objectName() == "mysql_privilege_cancel"
        assert "LOCK TABLES" in message_box.text()
    finally:
        dialog.close()


@pytest.mark.parametrize("object_name, expected", [
    ("mysql_parallel_table_lock_continue", "parallel_table_lock"),
    ("mysql_parallel_unsynchronized_continue", "parallel_unsynchronized"),
    ("mysql_single_connection_continue", "single_connection"),
    ("mysql_privilege_cancel", "cancel"),
])
def test_mysql_privilege_prompt_returns_the_mode_of_the_clicked_button(
    monkeypatch, object_name, expected
):
    dialog = _privilege_dialog(monkeypatch)
    # exec() 대신 버튼을 눌러 모달 대기 없이 선택을 재현한다.
    monkeypatch.setattr(
        QMessageBox, "exec", lambda box: _buttons_by_name(box)[object_name].click()
    )
    try:
        assert dialog._prompt_mysql_privilege_action() == expected
    finally:
        dialog.close()


@pytest.mark.parametrize("denied", ["BACKUP_ADMIN", "FLUSH_TABLES_OR_RELOAD", ""])
@pytest.mark.parametrize("mode, threads", [
    ("parallel_table_lock", 6),
    ("parallel_unsynchronized", 6),
    ("single_connection", 1),
])
def test_strict_privilege_failure_retries_with_the_chosen_mode(monkeypatch, denied, mode, threads):
    dialog = _privilege_dialog(monkeypatch)
    dialog._prompt_mysql_privilege_action = MagicMock(return_value=mode)
    try:
        dialog.on_finished(False, _privilege_marker(denied))

        dialog._prompt_mysql_privilege_action.assert_called_once_with(False)
        worker = dialog._start_export_worker.call_args.args[0]
        assert worker.kwargs["mysql_snapshot_mode"] == mode
        assert worker.kwargs["threads"] == threads
        assert worker.kwargs["output_dir"] == "C:/tmp/tunnelforge-export"
        assert dialog._snapshot_mode == mode
        dialog._report_error_anonymously.assert_not_called()
    finally:
        dialog.close()


def test_lock_tables_denial_asks_once_more_and_never_a_third_time(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    dialog._prompt_mysql_privilege_action = MagicMock(
        side_effect=["parallel_table_lock", "parallel_unsynchronized"]
    )
    try:
        dialog.on_finished(False, _privilege_marker("BACKUP_ADMIN"))
        assert dialog._snapshot_mode == "parallel_table_lock"
        dialog.on_finished(False, _privilege_marker("LOCK_TABLES"))
        assert dialog._snapshot_mode == "parallel_unsynchronized"
        dialog.on_finished(False, _privilege_marker("LOCK_TABLES"))

        assert [c.args for c in dialog._prompt_mysql_privilege_action.call_args_list] == [
            (False,), (True,)
        ]
        assert dialog._start_export_worker.call_count == 2
        dialog._report_error_anonymously.assert_called_once()
    finally:
        dialog.close()


@pytest.mark.parametrize("mode, denied", [
    ("parallel_unsynchronized", "LOCK_TABLES"),
    ("single_connection", "BACKUP_ADMIN"),
    ("parallel_table_lock", "BACKUP_ADMIN"),
])
def test_modes_after_the_prompts_never_prompt_again(monkeypatch, mode, denied):
    dialog = _privilege_dialog(monkeypatch)
    dialog._snapshot_mode = mode
    dialog._prompt_mysql_privilege_action = MagicMock()
    try:
        dialog.on_finished(False, _privilege_marker(denied))

        dialog._prompt_mysql_privilege_action.assert_not_called()
        dialog._start_export_worker.assert_not_called()
        dialog._report_error_anonymously.assert_called_once()
    finally:
        dialog.close()


def test_privilege_cancel_stops_without_error_report(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    dialog._prompt_mysql_privilege_action = MagicMock(return_value="cancel")
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.QMessageBox.warning",
        lambda *_args: pytest.fail("cancel must not show a generic failure"),
    )
    try:
        dialog.on_finished(False, _privilege_marker("BACKUP_ADMIN"))

        dialog._start_export_worker.assert_not_called()
        dialog._report_error_anonymously.assert_not_called()
        assert dialog.export_success is False
    finally:
        dialog.close()


def test_new_export_and_rerun_start_from_the_strict_snapshot(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    worker = MagicMock()
    worker.isRunning.return_value = False  # a truthy isRunning() makes close() wait on a modal box
    dialog._build_worker = MagicMock(return_value=worker)
    try:
        dialog.combo_schema.addItem("app")
        dialog._snapshot_mode = "parallel_unsynchronized"
        dialog.apply_job_rerun({"schema": "app", "scope": "full", "snapshot_mode": "parallel_unsynchronized"})
        dialog.do_export()

        assert dialog._snapshot_mode == "parallel_strict"
        assert "mysql_snapshot_mode" not in dialog._build_worker.call_args.kwargs
    finally:
        dialog.close()


def test_unsynchronized_export_completion_warns_about_consistency(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog._snapshot_mode = "parallel_unsynchronized"
    try:
        dialog.on_finished(True, "Rust DB Core export 완료")

        shown = dialog._show_export_done.call_args.args[0]
        assert "테이블 간 시점 일관성이 보장되지 않습니다" in shown
        visible = [dialog.txt_log.item(i).text() for i in range(dialog.txt_log.count())]
        assert any("시점 일관성이 보장되지 않습니다" in line for line in visible)
        assert any("시점 일관성이 보장되지 않습니다" in line for line in dialog.log_entries)
    finally:
        dialog.close()


def test_consistent_export_completion_has_no_consistency_warning(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog._snapshot_mode = "parallel_table_lock"
    try:
        dialog.on_finished(True, "Rust DB Core export 완료")

        assert "시점 일관성" not in dialog._show_export_done.call_args.args[0]
    finally:
        dialog.close()


def _box_snapshot(box):
    # 대화상자를 닫으면 자식 QMessageBox 도 삭제되므로 필요한 값만 미리 꺼내 둔다.
    return {
        "text": box.text(),
        "detailed": box.detailedText(),
        "buttons": {button.objectName(): button.text() for button in box.buttons()},
        "default": box.defaultButton().objectName() if box.defaultButton() else "",
    }


def _capture_incomplete_prompt(monkeypatch, objects, click_proceed=False):
    dialog = _privilege_dialog(monkeypatch)
    captured = []

    def fake_exec(box):
        captured.append(_box_snapshot(box))
        if click_proceed:
            _buttons_by_name(box)["export_incomplete_proceed"].click()

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        answer = dialog._prompt_incomplete_export(objects)
    finally:
        dialog.close()
    return answer, captured[0]


def test_incomplete_export_prompt_lists_objects_inline_up_to_ten(monkeypatch):
    objects = [f"trigger:t:trg{i}" for i in range(12)]
    answer, box = _capture_incomplete_prompt(monkeypatch, objects)

    assert answer is False
    text = box["text"]
    assert "이 객체 없이 내보내기 (불완전)" in text
    assert "구조와 데이터는 모두 내보냅니다" in text
    assert "테이블 데이터만" not in text
    assert all(f"• {name}" in text for name in objects[:10])
    assert "trg10" not in text and "trg11" not in text
    assert text.rstrip().endswith("외 2개")
    assert box["detailed"] == ""
    assert box["buttons"]["export_incomplete_proceed"] == "이 객체 없이 내보내기 (불완전)"
    assert box["default"] == "export_incomplete_cancel"


def test_incomplete_export_prompt_short_list_and_proceed(monkeypatch):
    answer, box = _capture_incomplete_prompt(monkeypatch, ["routine:fn"], click_proceed=True)

    assert answer is True
    assert box["text"].rstrip().endswith("제외할 객체:\n• routine:fn")


def test_incomplete_export_completion_says_structure_and_data_were_exported(monkeypatch):
    dialog = _privilege_dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog._allow_incomplete = True
    try:
        dialog.on_finished(True, "ok")

        shown = dialog._show_export_done.call_args.args[0]
        assert "일부 객체를 제외한 불완전 Export" in shown
        assert "구조와 데이터는 모두 내보냈으며" in shown
    finally:
        dialog.close()


def test_new_export_strings_have_english_translations(monkeypatch):
    from src.core import i18n
    from src.ui.dialogs import db_export_dialog as module
    from src.ui.dialogs.job_recording import _SNAPSHOT_LABELS

    def has_hangul(value):
        return any("\uac00" <= char <= "\ud7a3" for char in value)

    dialog = _privilege_dialog(monkeypatch)
    try:
        boxes = [
            _box_snapshot(dialog._create_mysql_privilege_dialog()),
            _box_snapshot(dialog._create_mysql_privilege_dialog(table_lock_denied=True)),
        ]
        korean = [box["text"] for box in boxes] + ["병렬 Export 권한 필요", "일관 병렬 Export 불가"]
        korean += [label for label, _mode in module._MYSQL_PRIVILEGE_CHOICES.values()]
        korean += [text for pair in module._MYSQL_RETRY_MESSAGES.values() for text in pair]
        korean += [
            f"✅ Export가 완료되었습니다.\n\n폴더: C:/x\n\n⚠️ {module._UNSYNCHRONIZED_EXPORT_WARNING}"
            f"\n\n⚠️ {module._INCOMPLETE_EXPORT_WARNING}",
            "이 객체 없이 내보내기 (불완전)",
            "사용자가 '이 객체 없이 내보내기 (불완전)'를 선택했습니다.",
            "일부 객체를 제외한 Export (불완전) 진행 중...",
            _SNAPSHOT_LABELS["parallel_table_lock"],
            _SNAPSHOT_LABELS["parallel_unsynchronized"],
            " · 불완전 Export(일부 객체 제외)",
        ]
        korean += [
            describe_snapshot_method({"mysql_snapshot_mode": mode, "worker_count": 3, "snapshot_fallbacks": [{"reason": reason}]}, 8)
            for mode in ("parallel_strict", "parallel_table_lock", "single_connection", "parallel_unsynchronized")
            for reason in ("privilege", "unsupported", "connection_limit", "lock_timeout")
        ]
        # The core's log lines when the automatic choice moves on (dump.rs mysql_snapshot_fallback_message).
        korean += [
            f"{reason} 다음 방법으로 내보냅니다."
            for reason in ("잠금 권한이 없어", "서버가 백업 잠금을 지원하지 않아", "동시 연결 수 제한 때문에", "잠금을 제때 얻지 못해")
        ]
        i18n.set_language("en")
        translated = {text: i18n.translate_text(text) for text in korean}
        english = []
        monkeypatch.setattr(QMessageBox, "exec", lambda box: english.append(_box_snapshot(box)))
        dialog._prompt_incomplete_export([f"trigger:t:trg{i}" for i in range(11)])
        english += [
            _box_snapshot(dialog._create_mysql_privilege_dialog()),
            _box_snapshot(dialog._create_mysql_privilege_dialog(table_lock_denied=True)),
        ]
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
        dialog.close()

    assert [f"{source!r} -> {result!r}" for source, result in translated.items() if has_hangul(result)] == []
    for box in english:
        assert not any(has_hangul(text) for text in box["buttons"].values()), box["buttons"]
    assert "Export without these objects (incomplete)" in english[0]["text"]
    assert english[0]["text"].endswith("and 1 more")


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


def test_export_retains_only_the_latest_configured_log_and_telemetry_entries():
    from src.core.constants import MAX_LOG_ENTRIES

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

    class FakeSaveButton:
        def setEnabled(self, _enabled):
            pass

    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.log_entries = []
    dialog.export_telemetry_events = []
    dialog.btn_save_log = FakeSaveButton()
    dialog._add_log = lambda message: RustDumpExportDialog._add_log(dialog, message)

    for index in range(MAX_LOG_ENTRIES + 25):
        RustDumpExportDialog.on_raw_output(dialog, json.dumps({
            "event": "row_progress",
            "table": f"event-{index}",
            "rows": index,
            "total": MAX_LOG_ENTRIES + 25,
        }))

    assert len(dialog.log_entries) == MAX_LOG_ENTRIES
    assert len(dialog.export_telemetry_events) == MAX_LOG_ENTRIES
    assert "event-0" not in "\n".join(dialog.log_entries)
    assert "event-524" in "\n".join(dialog.log_entries)
    assert dialog.export_telemetry_events[0]["table"] == "event-25"
    assert dialog.export_telemetry_events[-1]["table"] == "event-524"


def test_export_malformed_raw_output_escapes_controls_and_never_hits_file_logger(
    caplog,
):
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
    dialog.export_telemetry_events = []
    dialog._add_log = lambda message: dialog.log_entries.append(message)
    raw_line = (
        "table customer_orders failed password=hunter2\n"
        "[FORGED] second entry\x1b[31m\u202ereversed"
    )
    caplog.set_level(logging.DEBUG, logger="tunnelforge.db_dialogs")

    RustDumpExportDialog.on_raw_output(dialog, raw_line)

    assert dialog.txt_log.items == [sanitize_local_diagnostic(raw_line)]
    assert "customer_orders" in dialog.txt_log.items[0]
    assert "hunter2" not in dialog.txt_log.items[0]
    assert dialog.log_entries == []
    assert raw_line not in caplog.text


def test_export_rejects_malformed_recursive_telemetry_without_poisoning_summary(
    monkeypatch,
):
    import src.ui.dialogs.db_export_dialog as export_module
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

    scheduled_tables = []
    scheduled_tables.append(scheduled_tables)
    malformed_event = {
        "event": "dump_schedule",
        "threads": {"not": "numeric"},
        "scheduled_tables": scheduled_tables,
    }
    raw_line = (
        '{"event":"dump_schedule","password":"hunter2","padding":"'
        + ("context " * 4_000)
        + '"}'
    )
    monkeypatch.setattr(export_module.json, "loads", lambda _line: malformed_event)
    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.log_entries = []
    dialog.export_telemetry_events = []
    dialog._add_log = dialog.log_entries.append

    export_module.RustDumpExportDialog.on_raw_output(dialog, raw_line)

    assert dialog.export_telemetry_events == []
    assert len(dialog.txt_log.items) == 1
    assert len(dialog.txt_log.items[0]) <= 20_000
    assert "not" in dialog.txt_log.items[0]
    assert "hunter2" not in dialog.txt_log.items[0]
    assert dialog.log_entries == []


def test_export_malformed_parsed_telemetry_structurally_redacts_nested_credentials():
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
    dialog._add_log = dialog.log_entries.append
    raw_line = json.dumps({
        "event": "row_progress",
        "table": "customer_orders",
        "rows": {"not": "numeric"},
        "outer": [{r"AWS\nACCESS_KEY_ID": "nested-aws-secret"}],
        "request_id": "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq",
        "padding": "context " * 4_000,
    })

    RustDumpExportDialog.on_raw_output(dialog, raw_line)

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert dialog.export_telemetry_events == []
    assert "customer_orders" in rendered
    assert "nested-aws-secret" not in rendered
    assert "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq" in rendered
    assert len(dialog.txt_log.items[0]) == 20_000


@pytest.mark.parametrize(
    ("value", "expected", "forbidden"),
    [
        (
            [{"AWS_SECURITY_TOKEN": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD"}],
            "REDACTED",
            "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD",
        ),
        (
            "request failed with AKIA1234567890ABCDEF",
            "REDACTED",
            "AKIA1234567890ABCDEF",
        ),
        (
            "request id Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq failed",
            "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq",
            None,
        ),
    ],
)
def test_export_json_fallback_redacts_list_and_scalar_credentials(
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
    dialog.export_telemetry_events = []
    dialog._add_log = dialog.log_entries.append
    raw_line = json.dumps(value)

    RustDumpExportDialog.on_raw_output(dialog, raw_line)

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert expected in rendered
    if forbidden is not None:
        assert forbidden not in rendered


def test_export_impossible_progress_relationship_uses_plain_fallback():
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
    dialog._add_log = dialog.log_entries.append
    event = {
        "event": "row_progress",
        "table": "customer_orders",
        "rows": 100,
        "total": 1,
        "chunks_done": 5,
        "chunks_total": 1,
    }

    RustDumpExportDialog.on_raw_output(dialog, json.dumps(event))

    rendered = "\n".join(dialog.txt_log.items + dialog.log_entries)
    assert dialog.export_telemetry_events == []
    assert "100 / 1 rows" not in rendered
    assert "5/1 chunks" not in rendered


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
        {
            "event": "row_progress",
            "table": "customer_orders",
            "rows": 1,
            "total": 2,
            "chunk_index": 5,
            "chunks_total": 1,
        },
    ],
)
def test_export_invalid_required_telemetry_uses_plain_fallback(event):
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
    dialog._add_log = dialog.log_entries.append

    RustDumpExportDialog.on_raw_output(dialog, json.dumps(event))

    assert dialog.export_telemetry_events == []
    assert dialog.txt_log.items
    assert '"event"' in dialog.txt_log.items[0]


def test_export_progress_escapes_controls_before_display_and_log():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    class FakeLogList:
        def __init__(self):
            self.items = []

        def addItem(self, item):
            self.items.append(item)

        def scrollToBottom(self):
            pass

    dialog = type("DummyDialog", (), {})()
    dialog.txt_log = FakeLogList()
    dialog.log_entries = []
    dialog._add_log = dialog.log_entries.append

    raw_message = (
        "table customer_orders failed token=short-secret\n"
        "[FORGED]\u202ereversed"
    )
    RustDumpExportDialog.on_progress(dialog, raw_message)

    expected = sanitize_local_diagnostic(raw_message)
    assert dialog.txt_log.items == [expected]
    assert dialog.log_entries == [expected]
    assert "customer_orders" in expected
    assert "short-secret" not in expected


def test_export_saved_log_redacts_credentials_and_preserves_opaque_ids(
    tmp_path, monkeypatch
):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    output_path = tmp_path / "export-log.txt"
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.QFileDialog.getSaveFileName",
        lambda *_args: (str(output_path), ""),
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.QMessageBox.information",
        lambda *_args: None,
    )
    dialog = RustDumpExportDialog()

    try:
        dialog.on_raw_output(json.dumps({
            "event": "phase",
            "message": (
                r"AW\x53_SECRET_ACCESS_KEY=structured-aws-secret "
                "for table customer_orders"
            ),
        }))
        dialog.on_progress(
            "request Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq failed at "
            "postgresql://alice:p@ss@db.internal/customer_orders"
        )

        dialog.save_log()

        visible = "\n".join(
            dialog.txt_log.item(index).text()
            for index in range(dialog.txt_log.count())
        )
        persisted = "\n".join(dialog.log_entries)
        saved = output_path.read_text(encoding="utf-8")
        combined = "\n".join((visible, persisted, saved))
        for secret in (
            "structured-aws-secret",
            "alice",
            "p@ss",
        ):
            assert secret not in combined
        assert "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq" in combined
        assert "customer_orders" in combined
        assert "db.internal/customer_orders" in combined
    finally:
        dialog.close()


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

    assert dialog._error_report_operation_generation == 1
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

def test_export_error_report_workers_are_retained_and_use_allowlisted_context(monkeypatch):
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
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    class FakeConnector:
        engine = "mysql"

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
    dialog = RustDumpExportDialog(
        connector=FakeConnector(), config_manager=config_manager
    )
    dialog.export_schema = "customer_schema_secret"
    dialog.export_tables = ["customer_table_secret"]

    engine_error = (
        'dump.run failed for schema "customer_schema_secret", '
        'table `customer_table_secret`: access denied'
    )
    dialog._report_error_anonymously()
    dialog._report_error_anonymously()

    assert len(dialog._error_report_workers) == 2
    worker1, worker2 = dialog._error_report_workers
    assert worker1.kwargs == {
        "operation_kind": "export",
        "db_engine": "mysql",
        "phase": "dump.run",
    }
    from src.core.error_report_builder import build_error_report

    payload = build_error_report(
        config_manager,
        **worker1.kwargs,
        error_message="Rust DB Core export operation failed.",
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert "customer_schema_secret" not in serialized
    assert "customer_table_secret" not in serialized
    assert "access denied" not in serialized

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


def test_export_cancellation_does_not_start_error_reporting(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpExportDialog()
    dialog._cancel_requested = True
    dialog._report_error_anonymously = MagicMock()

    dialog.on_finished(False, "cancelled by user")

    dialog._report_error_anonymously.assert_not_called()
    assert dialog.export_success is False
    dialog.close()


def _refusal_dialog(monkeypatch, refusal, prompt_answer):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.QMessageBox.warning",
        lambda *_args: None,
    )
    dialog = RustDumpExportDialog()
    dialog._test_app = app  # keep QApplication alive for the dialog's lifetime
    failed_worker = MagicMock()
    failed_worker.isRunning.return_value = False
    failed_worker.export_refusal = refusal
    retry_worker = MagicMock()
    retry_worker.isRunning.return_value = False
    dialog.worker = failed_worker
    dialog.export_schema = "app"
    dialog.input_output_dir.setText("C:/tmp/tunnelforge-export")
    dialog._prompt_incomplete_export = MagicMock(return_value=prompt_answer)
    dialog._build_worker = MagicMock(return_value=retry_worker)
    dialog._start_export_worker = MagicMock()
    dialog._report_error_anonymously = MagicMock()
    return dialog, retry_worker


def test_bypassable_refusal_retries_only_after_explicit_incomplete_choice(monkeypatch):
    dialog, retry_worker = _refusal_dialog(
        monkeypatch, {"objects": ["trigger:t:trg"], "bypassable": True}, True
    )
    try:
        dialog.on_finished(False, "Rust DB Core export 오류: refused")

        dialog._prompt_incomplete_export.assert_called_once_with(["trigger:t:trg"])
        assert dialog._allow_incomplete is True
        dialog._build_worker.assert_called_once_with(
            "app", "C:/tmp/tunnelforge-export", mysql_snapshot_mode="parallel_strict"
        )
        dialog._start_export_worker.assert_called_once_with(retry_worker)
    finally:
        dialog.worker = None
        dialog.close()


def test_declining_incomplete_choice_does_not_retry(monkeypatch):
    dialog, _ = _refusal_dialog(
        monkeypatch, {"objects": ["routine:fn"], "bypassable": True}, False
    )
    try:
        dialog.on_finished(False, "Rust DB Core export 오류: refused")

        assert dialog._allow_incomplete is False
        dialog._build_worker.assert_not_called()
        dialog._start_export_worker.assert_not_called()
    finally:
        dialog.worker = None
        dialog.close()


def test_lossy_definition_refusal_is_never_offered_as_incomplete_export(monkeypatch):
    dialog, _ = _refusal_dialog(
        monkeypatch, {"objects": ["generated_column:t:g"], "bypassable": False}, True
    )
    try:
        dialog.on_finished(False, "Rust DB Core export 오류: refused")

        dialog._prompt_incomplete_export.assert_not_called()
        dialog._build_worker.assert_not_called()
    finally:
        dialog.worker = None
        dialog.close()


def test_incomplete_choice_survives_snapshot_fallback_worker_rebuild(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpExportDialog()
    dialog.radio_full.setChecked(True)
    dialog._allow_incomplete = True
    try:
        worker = dialog._build_worker("app", "C:/tmp/x", mysql_snapshot_mode="single_connection")
        assert worker.kwargs["allow_incomplete"] is True
        assert worker.kwargs["mysql_snapshot_mode"] == "single_connection"
    finally:
        dialog.close()


def test_export_title_does_not_expose_the_engine_name(monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.check_rust_dump", lambda: (True, "Rust DB Core OK"))
    dialog = RustDumpExportDialog()
    try:
        assert dialog.windowTitle() == "데이터 Export"
    finally:
        dialog.close()


def _export_with_tables(tables):
    QApplication.instance() or QApplication([])
    connector = MagicMock()
    connector.get_schemas.return_value = ["app"]
    connector.get_tables.return_value = tables
    return RustDumpExportDialog(connector=connector)


def test_export_table_filter_and_selection_counter():
    dialog = _export_with_tables(["orders", "order_items", "users"])
    try:
        assert dialog.label_table_count.text() == "3/3 선택"
        dialog.input_table_filter.setText("ORDER")
        visible = [dialog.list_tables.item(i).text() for i in range(dialog.list_tables.count())
                   if not dialog.list_tables.item(i).isHidden()]
        assert visible == ["orders", "order_items"]
        dialog.deselect_all_tables()  # applies to the filtered rows only
        assert dialog.get_selected_tables() == ["users"]
        assert dialog.label_table_count.text() == "1/3 선택"
        dialog.input_table_filter.clear()
        assert all(not dialog.list_tables.item(i).isHidden() for i in range(dialog.list_tables.count()))
        dialog.select_all_tables()
        assert dialog.label_table_count.text() == "3/3 선택"
    finally:
        dialog.close()


def test_export_done_offers_open_folder(monkeypatch, tmp_path):
    from src.ui.dialogs import db_export_dialog

    dialog = _export_with_tables(["a"])
    opened = []
    monkeypatch.setattr(db_export_dialog.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)

    try:
        dialog.input_output_dir.setText(str(tmp_path))
        box = dialog._show_export_done("done")  # non-blocking: the caller (job recording etc.) continues
        buttons = {b.text(): b for b in box.buttons()}
        assert "📂 폴더 열기" in buttons
        buttons["📂 폴더 열기"].click()
        assert [os.path.normcase(os.path.normpath(p)) for p in opened] == [os.path.normcase(str(tmp_path))]
        dialog.input_output_dir.setText(str(tmp_path / "missing"))
        assert dialog.open_output_folder() is False
    finally:
        dialog.close()


def test_export_done_open_folder_button_is_translated(tmp_path):
    from src.core import i18n

    dialog = _export_with_tables(["a"])
    i18n.set_language("en")
    try:
        dialog.input_output_dir.setText(str(tmp_path))
        box = dialog._show_export_done("done")
        assert "📂 Open folder" in [b.text() for b in box.buttons()]
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
        dialog.close()
