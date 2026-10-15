import json
import logging
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter, describe_snapshot_method
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

    assert dialog._compression() == "zstd"
    assert [dialog.combo_compression.itemText(i) for i in range(dialog.combo_compression.count())] == [
        "압축 안 함", "압축(zstd)"]
    assert dialog.combo_compression.currentText() == "압축(zstd)"
    dialog.close()


def _dialog(monkeypatch):
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


def _buttons_by_name(message_box):
    return {button.objectName(): button for button in message_box.buttons()}


def _finished_worker(mode=None, method="", omitted=(), refusal=None):
    """A worker as RustDumpWorker leaves it: the core's result (export_run) or its refusal."""
    worker = MagicMock()
    worker.isRunning.return_value = False  # a truthy isRunning() makes close() wait on a modal box
    worker.export_run = {"mode": mode, "method": method, "omitted": list(omitted)}
    worker.export_refusal = refusal
    return worker


def test_unsynchronized_export_completion_warns_about_consistency(monkeypatch):
    dialog = _dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog.worker = _finished_worker("parallel_unsynchronized", "테이블마다 시점이 다를 수 있음 · 동시 작업 6개")
    try:
        dialog.on_finished(True, "Rust DB Core export 완료")

        shown = dialog._show_export_done.call_args.args[0]
        assert "테이블 간 시점 일관성이 보장되지 않습니다" in shown
        visible = [dialog.txt_log.item(i).text() for i in range(dialog.txt_log.count())]
        assert any("시점 일관성이 보장되지 않습니다" in line for line in visible)
        assert any("시점 일관성이 보장되지 않습니다" in line for line in dialog.log_entries)
        assert dialog._snapshot_mode == "parallel_unsynchronized", "the method comes from the core's result"
    finally:
        dialog.worker = None
        dialog.close()


def test_consistent_export_completion_has_no_consistency_warning(monkeypatch):
    dialog = _dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog.worker = _finished_worker("parallel_table_lock", "같은 시점으로 내보냄 · 동시 작업 6개 (잠금 권한이 없어 테이블만 잠깐 잠금)")
    try:
        dialog.on_finished(True, "Rust DB Core export 완료")

        assert "시점 일관성" not in dialog._show_export_done.call_args.args[0]
    finally:
        dialog.worker = None
        dialog.close()


def test_export_completion_names_the_snapshot_method(monkeypatch):
    dialog = _dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    method = "같은 시점으로 내보냄 · 동시 작업 1개 (잠금 권한이 없어 연결 하나로 읽음)"
    dialog.worker = _finished_worker("single_connection", method)
    try:
        # The exporter's message already ends with the method (RustDumpExporter._run_rust_dump).
        dialog.on_finished(True, f"Export 완료: 테이블 1개, 행 2개 · {method}")

        assert f"\n내보낸 방법: {method}" in dialog._show_export_done.call_args.args[0]
        visible = [dialog.txt_log.item(i).text() for i in range(dialog.txt_log.count())]
        assert visible == [f"✅ Export 완료: 테이블 1개, 행 2개 · {method}"], "the method is in the run log once"
        assert sum(method in entry for entry in dialog.log_entries) == 1, "and once in the saved log"
    finally:
        dialog.worker = None
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
    dialog = _dialog(monkeypatch)
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
    assert "'담을 수 없는 객체는 빼고 내보내기'를 켜 두면 다음부터 묻지 않습니다" in text
    assert all(f"• 테이블 t: 트리거(trg{i})는 덤프에 담을 수 없습니다" in text for i in range(10))
    assert "trigger:t:" not in text, "the core's codes stay behind '자세히'"
    assert "trg10" not in text and "trg11" not in text
    assert text.rstrip().endswith("외 2개")
    assert box["detailed"] == ""
    assert box["buttons"]["export_incomplete_proceed"] == "이 객체 없이 내보내기 (불완전)"
    assert box["buttons"]["export_refusal_details"] == "자세히"
    assert box["default"] == "export_incomplete_cancel"


def test_incomplete_export_prompt_short_list_and_proceed(monkeypatch):
    answer, box = _capture_incomplete_prompt(monkeypatch, ["routine:fn"], click_proceed=True)

    assert answer is True
    assert box["text"].rstrip().endswith("뺄 객체:\n• 저장 프로시저·함수 fn: 덤프에 담을 수 없습니다")


def test_incomplete_export_completion_says_structure_and_data_were_exported(monkeypatch):
    dialog = _dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog.worker = _finished_worker("parallel_strict", omitted=["Object not exported: trigger:t:trg"])
    try:
        dialog.on_finished(True, "ok")

        shown = dialog._show_export_done.call_args.args[0]
        assert "일부 객체를 빼고 내보냈습니다(불완전 Export)" in shown
        assert "구조와 데이터는 모두 담았습니다" in shown
        assert "실행 로그의 'Export 경고' 줄" in shown and "manifest" not in shown
    finally:
        dialog.worker = None
        dialog.close()


def test_allow_incomplete_toggle_with_nothing_left_out_is_a_complete_export(monkeypatch):
    dialog = _dialog(monkeypatch)
    dialog._show_export_done = MagicMock()
    dialog.chk_allow_incomplete.setChecked(True)
    dialog._reset_export_state("app")
    dialog.worker = _finished_worker("parallel_strict")
    try:
        assert dialog._allow_incomplete is True
        dialog.on_finished(True, "ok")

        assert "불완전" not in dialog._show_export_done.call_args.args[0]
    finally:
        dialog.worker = None
        dialog.close()


def test_new_export_strings_have_english_translations(monkeypatch):
    from src.core import i18n
    from src.exporters.rust_dump_exporter import describe_export_refusal
    from src.ui.dialogs import db_export_dialog as module
    from src.ui.dialogs.job_recording import _SNAPSHOT_LABELS

    def has_hangul(value):
        return any("\uac00" <= char <= "\ud7a3" for char in value)

    dialog = _dialog(monkeypatch)
    try:
        korean = [
            module._UNSYNCHRONIZED_EXPORT_WARNING,
            module._INCOMPLETE_EXPORT_WARNING,
            module._PARTIAL_EXPORT_NOTE,
            module._PARTIAL_EXPORT_NOTE_POSTGRESQL,
            module._FK_AUTO_TOOLTIP,
            "DB를 선택하세요.",
            "테이블을 하나 이상 고르세요.",
            "연결된 부모 테이블을 확인하지 못했습니다. Export를 시작할 때 다시 확인해 함께 담습니다.",
            "이 객체 없이 내보내기 (불완전)",
            "사용자가 '이 객체 없이 내보내기 (불완전)'를 선택했습니다.",
            "일부 객체를 제외한 Export (불완전) 진행 중...",
            *_SNAPSHOT_LABELS.values(),
            " · 불완전 Export(일부 객체 제외)",
            "전체 · 내보내는 방법 자동 선택(시점 일관성 포기 허용) · 동시 작업 8개 · 압축 안 함",
            "스키마:",
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
        translated.update({
            "summary": dialog._advanced_summary(),
            "refusal": "\n".join(describe_export_refusal([
                "trigger:t:trg", "generated_column:t:g", "event:e", "unsupported_index:t:i", "cross_schema_fk:t:f",
                "non_transactional_table:t:MEMORY"])),
        })
        english = []
        monkeypatch.setattr(QMessageBox, "exec", lambda box: english.append(_box_snapshot(box)))
        dialog._prompt_incomplete_export([f"trigger:t:trg{i}" for i in range(11)])
        english.append(_box_snapshot(dialog._show_lossy_refusal(["generated_column:t:g", "trigger:t:trg"])))
        dialog.chk_include_fk.setChecked(True)
        dialog.chk_unsynchronized.setChecked(True)
        dialog.chk_allow_incomplete.setChecked(True)
        dialog.chk_manual_folder.setChecked(True)
        dialog.combo_compression.setCurrentIndex(0)
        translated["summary on"] = dialog._advanced_summary()
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
        dialog.close()

    assert [f"{source!r} -> {result!r}" for source, result in translated.items() if has_hangul(result)] == []
    for box in english:
        assert not any(has_hangul(text) for text in box["buttons"].values()), box["buttons"]
        assert not has_hangul(box["text"]), box["text"]
    assert "Export without these objects (incomplete)" in english[0]["text"]
    assert english[0]["text"].endswith("and 1 more")


def test_rust_dump_export_dialog_rejects_parent_manual_folder(tmp_path):
    app = QApplication.instance() or QApplication([])
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        str(tmp_path) if key == "rust_dump_export_base_dir" else default
    )
    dialog = RustDumpExportDialog(config_manager=config_manager)

    dialog.chk_manual_folder.setChecked(True)
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
    generic = []
    monkeypatch.setattr(
        "src.ui.dialogs.db_export_dialog.QMessageBox.warning",
        lambda *args: generic.append(args),
    )
    dialog = RustDumpExportDialog()
    dialog._test_app = app  # keep QApplication alive for the dialog's lifetime
    dialog._generic_failures = generic
    failed_worker = MagicMock()
    failed_worker.isRunning.return_value = False
    failed_worker.export_refusal = refusal
    retry_worker = MagicMock()
    retry_worker.isRunning.return_value = False
    dialog.worker = failed_worker
    dialog.export_schema = "app"
    dialog.input_output_dir.setText("C:/tmp/tunnelforge-export")
    dialog._prompt_incomplete_export = MagicMock(return_value=prompt_answer)
    dialog._show_lossy_refusal = MagicMock()
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
        dialog._build_worker.assert_called_once_with("app", "C:/tmp/tunnelforge-export")
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
        assert dialog._generic_failures == [], "the user already answered the question"
        dialog._report_error_anonymously.assert_not_called()
    finally:
        dialog.worker = None
        dialog.close()


def test_lossy_refusal_offers_no_proceed(monkeypatch):
    dialog, _ = _refusal_dialog(
        monkeypatch, {"objects": ["generated_column:t:g", "trigger:t:trg"], "bypassable": False}, True
    )
    try:
        dialog.on_finished(False, "Rust DB Core export 오류: dump.run refused: ... generated_column:t:g")

        dialog._prompt_incomplete_export.assert_not_called()
        dialog._build_worker.assert_not_called()
        dialog._show_lossy_refusal.assert_called_once_with(["generated_column:t:g", "trigger:t:trg"])
        assert dialog._generic_failures == [], "no generic 'Export 실패' box"
        dialog._report_error_anonymously.assert_not_called()
        visible = [dialog.txt_log.item(i).text() for i in range(dialog.txt_log.count())]
        assert "❌ 담을 수 없는 객체가 있어 Export를 시작하지 않았습니다." in visible
        assert not any("generated_column" in line for line in visible)
        # The saved log keeps sentences, not the core's English text.
        result = [entry for entry in dialog.log_entries if "결과 메시지:" in entry]
        assert len(result) == 1 and result[0].endswith(
            "결과 메시지: 담을 수 없는 객체가 있어 Export를 시작하지 않았습니다. "
            "테이블 t: 계산 열 g의 정의를 덤프에 담을 수 없습니다; 테이블 t: 트리거(trg)는 덤프에 담을 수 없습니다")
        assert not any("dump.run refused" in entry for entry in dialog.log_entries)
        assert dialog.label_data.text() == "📦 데이터: -" and dialog.label_status.text() == "⏹ Export를 시작하지 않았습니다"
    finally:
        dialog.worker = None
        dialog.close()


def test_lossy_refusal_window_names_the_table_and_has_only_close(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        box = dialog._show_lossy_refusal(["generated_column:orders:total"])
        snapshot = _box_snapshot(box)
        box.close()
    finally:
        dialog.close()

    assert "• 테이블 orders: 계산 열 total의 정의를 덤프에 담을 수 없습니다" in snapshot["text"]
    assert snapshot["text"].rstrip().endswith(
        "목록에서 이 테이블의 체크를 풀면 나머지 테이블은 내보낼 수 있습니다. "
        "일부 테이블만 내보낼 때는 테이블만 담습니다. 뷰·저장 프로시저·함수·이벤트는 빠집니다."), "a full export says what unchecking costs"
    assert "그 밖에" not in snapshot["text"]
    assert "generated_column" not in snapshot["text"]
    assert set(snapshot["buttons"]) == {"export_refusal_close", "export_refusal_details"}


def test_lossy_refusal_lists_the_omittable_objects_apart(monkeypatch):
    # The core puts the omittable objects after the lossy ones (dump_scope_refusal) when any is lossy.
    dialog = _dialog(monkeypatch)
    dialog.export_tables = ["orders", "logs"]  # already a table export: no word about views
    try:
        box = dialog._show_lossy_refusal(["generated_column:orders:total", "trigger:orders:trg", "deprecated_engine:logs:MyISAM"])
        text = box.text()
        box.close()
    finally:
        dialog.close()

    lossy, _, rest = text.partition("그 밖에 빼고 내보낼 수 있는 객체:")
    assert lossy.startswith("덤프에 그대로 담을 수 없는 테이블 정의가 있어 Export를 시작하지 않았습니다.")
    assert "계산 열 total" in lossy and "trg" not in lossy and "MyISAM" not in lossy
    assert "• 테이블 orders: 트리거(trg)는 덤프에 담을 수 없습니다" in rest
    assert "• 테이블 logs: MyISAM 엔진이라 다른 테이블과 같은 시점으로 읽을 수 없습니다. 빼고 내보내면 이 테이블이 통째로 빠집니다" in rest
    assert rest.rstrip().endswith("목록에서 이 테이블의 체크를 풀면 나머지 테이블은 내보낼 수 있습니다.")


def test_allow_incomplete_toggle_skips_prompt(monkeypatch):
    dialog = _dialog(monkeypatch)
    try:
        dialog.chk_allow_incomplete.setChecked(True)
        dialog._reset_export_state("app")
        assert dialog._build_worker("app", "C:/tmp/x").kwargs["allow_incomplete"] is True
        # The core waives the omittable objects itself, so only a lossy refusal can still come back.
        dialog._prompt_incomplete_export = MagicMock()
        dialog._show_lossy_refusal = MagicMock()
        dialog.worker = _finished_worker(refusal={"objects": ["generated_column:t:g"], "bypassable": False})
        dialog.on_finished(False, "refused")
        dialog._prompt_incomplete_export.assert_not_called()
        dialog._show_lossy_refusal.assert_called_once()
    finally:
        dialog.worker = None
        dialog.close()


def test_a_missing_privilege_never_opens_a_privilege_dialog(monkeypatch):
    dialog = _dialog(monkeypatch)
    dialog._build_worker = MagicMock()
    try:
        assert not any(hasattr(dialog, name) for name in (
            "_create_mysql_privilege_dialog", "_prompt_mysql_privilege_action", "_retry_with_snapshot_mode"))
        dialog.worker = _finished_worker()
        dialog.worker.export_refusal = None
        dialog.on_finished(False, "Rust DB Core export 오류: MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:LOCK_TABLES:ERROR 1044")

        dialog._build_worker.assert_not_called()
        dialog._report_error_anonymously.assert_called_once()
    finally:
        dialog.worker = None
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

# ------------------------------------------------------------------ one table list, Advanced toggles

class _FkFacade:
    """schema.inspect of a schema where items -> orders -> customers; logs stands alone."""

    def __init__(self):
        self.calls = 0

    def inspect_schema(self, endpoint):
        self.calls += 1
        return {"tables": [
            {"name": "customers", "foreign_keys": []},
            {"name": "orders", "foreign_keys": [{"name": "fk_orders_customers", "referenced_table": "customers"}]},
            {"name": "items", "foreign_keys": [{"name": "fk_items_orders", "referenced_table": "orders"}]},
            {"name": "logs", "foreign_keys": []},
        ]}


class _CapturingWorker:
    """Stands in for RustDumpWorker: records what the dialog would run."""

    created = []

    def __init__(self, task_type, config, **kwargs):
        self.task_type, self.config, self.kwargs = task_type, config, kwargs
        self.export_refusal = self.export_run = None
        _CapturingWorker.created.append(self)
        for name in ("progress", "table_progress", "detail_progress", "table_status", "raw_output", "finished"):
            setattr(self, name, MagicMock())

    def start(self):
        pass

    def isRunning(self):
        return False


class _FkLookup(QObject):
    """Stands in for FkParentsWorker: reads through the test connector's facade at once, or, while
    ``held``, only when ``answer()`` is called (what the user sees while a large schema is read)."""

    loaded = pyqtSignal(str, object)
    finished = pyqtSignal()
    facade = None
    held = False
    waiting = []

    def __init__(self, config, schema):
        super().__init__()
        self.config, self.schema = config, schema

    def start(self):
        if _FkLookup.held:
            _FkLookup.waiting.append(self)
        else:
            self.answer()

    def answer(self):
        try:
            deps = RustDumpExporter(self.config, facade=_FkLookup.facade).fetch_fk_parents(self.schema)
        except Exception:
            deps = None
        self.loaded.emit(self.schema, deps)
        self.finished.emit()

    def wait(self):
        return True


def _toggles_dialog(monkeypatch, tmp_path, settings=None, engine="mysql"):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.check_rust_dump", lambda: (True, "ok"))
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.RustDumpWorker", _CapturingWorker)
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.FkParentsWorker", _FkLookup)
    _CapturingWorker.created = []
    _FkLookup.held, _FkLookup.waiting = False, []
    store = {"rust_dump_export_base_dir": str(tmp_path), **(settings or {})}
    config_manager = MagicMock()
    config_manager.get_app_setting.side_effect = lambda key, default=None: store.get(key, default)
    config_manager.set_app_settings.side_effect = store.update
    connector = MagicMock(host="127.0.0.1", port=3306, user="u", password="p", engine=engine, database="")
    connector.get_schemas.return_value = ["app"]
    connector.get_tables.return_value = ["customers", "items", "logs", "orders"]
    connector.facade = _FkFacade()
    _FkLookup.facade = connector.facade
    dialog = RustDumpExportDialog(connector=connector, config_manager=config_manager, connection_info="conn")
    dialog._test_store = store
    return dialog


def _items(dialog):
    return {item.text(): item for item in dialog._table_items()}


def _uncheck(dialog, *names):
    for name in names:
        _items(dialog)[name].setCheckState(Qt.CheckState.Unchecked)


def _enabled(item):
    return bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)


def test_table_list_is_checkbox_only_always_shown_and_starts_fully_checked(monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QAbstractItemView

    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        assert dialog.list_tables.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
        assert not dialog.table_group.isHidden()
        assert dialog.get_selected_tables() == ["customers", "items", "logs", "orders"]
        assert dialog._is_full_export() and dialog.lbl_partial_note.isHidden()
        assert not any(hasattr(dialog, name) for name in ("radio_full", "radio_partial", "btn_type_group",
                                                          "radio_auto_naming", "radio_manual_naming", "chk_timestamp"))
        assert dialog.connector.facade.calls == 0, "a full export never reads the FKs"
    finally:
        dialog.close()


def test_all_checked_sends_full_scope(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        assert worker.task_type == "export_schema" and "tables" not in worker.kwargs
    finally:
        dialog.close()


def test_export_sends_auto_snapshot_mode(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.spin_threads.setValue(5)
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        assert (worker.kwargs["mysql_snapshot_mode"], worker.kwargs["mysql_snapshot_fallback"]) == ("auto", "single_connection")
        assert worker.kwargs["threads"] == 5, "the core lowers the worker count itself"
        assert worker.kwargs["allow_incomplete"] is False
    finally:
        dialog.close()


def test_unsynchronized_toggle_sets_fallback(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        assert dialog.chk_unsynchronized.isChecked() is False
        dialog.chk_unsynchronized.setChecked(True)
        dialog.do_export()
        assert _CapturingWorker.created[0].kwargs["mysql_snapshot_fallback"] == "parallel_unsynchronized"
    finally:
        dialog.close()
    postgres = _toggles_dialog(monkeypatch, tmp_path, settings={"rust_dump_export_unsynchronized": True}, engine="postgresql")
    try:
        assert postgres.chk_unsynchronized.isHidden(), "PostgreSQL has no such choice"
        assert postgres._snapshot_fallback() == "single_connection"
        assert "시점 일관성" not in postgres._advanced_summary()
    finally:
        postgres.close()


def test_export_sends_exact_checked_list(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        _uncheck(dialog, "items", "logs")
        assert not dialog.lbl_partial_note.isHidden()
        assert dialog.lbl_partial_note.text() == "일부 테이블만 내보낼 때는 테이블만 담습니다. 뷰·저장 프로시저·함수·이벤트는 빠집니다."
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        assert worker.task_type == "export_tables"
        assert worker.kwargs["tables"] == ["customers", "orders"]
        assert worker.kwargs["include_fk_parents"] is False
        assert worker.kwargs["auto_added_tables"] == []
        assert worker.kwargs["fk_parents_outside_dump"] == []
    finally:
        dialog.close()


def test_fk_parent_toggle_defaults_off(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        assert dialog.chk_include_fk.isChecked() is False
        assert dialog.advanced_panel.isHidden() and dialog.btn_advanced.text() == "고급 설정 열기"
        assert dialog.lbl_advanced_summary.text() == ""
    finally:
        dialog.close()


def test_fk_toggle_off_shows_missing_parent_notice_and_checks_nothing(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        _uncheck(dialog, "customers", "logs")
        assert dialog.get_selected_tables() == ["items", "orders"], "nothing is checked for the user"
        assert dialog.lbl_fk_note.text() == (
            "고른 테이블 1개의 연결된 부모 테이블이 체크되어 있지 않습니다 (orders → customers). 고른 테이블만 내보냅니다. "
            "함께 담으려면 고급 설정에서 '연결된 부모 테이블 함께 담기'를 켜세요.")
        _uncheck(dialog, "orders")
        assert "(items → orders)" in dialog.lbl_fk_note.text()
        dialog._fk_outside = [{"child": f"c{i}", "constraint": "fk", "parent": "p"} for i in range(5)]
        dialog._update_table_notes()
        assert "(c0 → p, c1 → p, c2 → p 외 2개)" in dialog.lbl_fk_note.text()
        dialog._refresh_table_state()
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        assert worker.kwargs["tables"] == ["items"]
        assert worker.kwargs["fk_parents_outside_dump"] == [
            {"child": "items", "constraint": "fk_items_orders", "parent": "orders"}]
        assert dialog.connector.facade.calls == 1, "the FKs are read once per schema"
    finally:
        dialog.close()


def test_fk_toggle_on_disables_auto_added_parents(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.chk_include_fk.setChecked(True)
        _uncheck(dialog, "customers", "orders", "logs")
        items = _items(dialog)
        # items needs orders, which needs customers: both are checked again and locked while it needs them.
        assert dialog.get_selected_tables() == ["customers", "items", "orders"]
        assert not _enabled(items["orders"]) and not _enabled(items["customers"]) and _enabled(items["items"])
        assert items["orders"].toolTip() == (
            "체크한 테이블이 이 테이블을 참조해 자동으로 담았습니다. 참조하는 테이블의 체크를 풀면 다시 고를 수 있습니다.")
        assert items["orders"].text() == "orders", "the table name stays the list text"
        assert dialog.lbl_fk_note.text() == "연결된 부모 테이블 자동 추가 2개: customers, orders"
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        assert worker.kwargs["tables"] == ["customers", "items", "orders"]
        assert (worker.kwargs["include_fk_parents"], worker.kwargs["auto_added_tables"]) == (False, ["customers", "orders"])
        assert worker.kwargs["fk_parents_outside_dump"] == []
        # Unchecking the child releases its parents back to what the user had chosen (unchecked).
        dialog.set_ui_enabled(True)
        _uncheck(dialog, "items")
        assert dialog.get_selected_tables() == [] and _enabled(items["orders"]) and _enabled(items["customers"])
        assert items["orders"].toolTip() == ""
    finally:
        dialog.close()


def test_first_uncheck_does_not_wait_for_the_fk_lookup(monkeypatch, tmp_path):
    # On a large schema the lookup takes long; it ran on the UI thread and froze the first uncheck.
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    _FkLookup.held = True
    try:
        _uncheck(dialog, "customers", "logs")
        assert dialog.get_selected_tables() == ["items", "orders"], "the uncheck takes effect at once"
        assert dialog.lbl_fk_note.text() == "연결된 부모 테이블을 확인하는 중입니다."
        assert len(_FkLookup.waiting) == 1, "one lookup per schema however many changes come in"
        _FkLookup.waiting[0].answer()
        assert "(orders → customers)" in dialog.lbl_fk_note.text()
        assert dialog.connector.facade.calls == 1
    finally:
        dialog.close()


def test_fk_lookup_failure_says_so_and_exports_the_checked_tables(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    dialog.connector.facade.inspect_schema = MagicMock(side_effect=RuntimeError("core down"))
    try:
        _uncheck(dialog, "customers")
        assert dialog.lbl_fk_note.text() == "연결된 부모 테이블을 확인하지 못했습니다. 고른 테이블만 내보냅니다."
        dialog.do_export()
        assert _CapturingWorker.created[0].kwargs["tables"] == ["items", "logs", "orders"]
        assert _CapturingWorker.created[0].kwargs["include_fk_parents"] is False, "the toggle is off"
        assert _CapturingWorker.created[0].kwargs["fk_parents_outside_dump"] is None
    finally:
        dialog.close()


def test_fk_lookup_failure_with_the_toggle_on_reads_them_again_in_the_worker(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    dialog.connector.facade.inspect_schema = MagicMock(side_effect=RuntimeError("core down"))
    try:
        dialog.chk_include_fk.setChecked(True)
        _uncheck(dialog, "customers")
        assert dialog.lbl_fk_note.text() == (
            "연결된 부모 테이블을 확인하지 못했습니다. Export를 시작할 때 다시 확인해 함께 담습니다.")
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        # The worker's own exporter reads the FKs and adds the parents, or fails the run if it cannot.
        assert (worker.kwargs["tables"], worker.kwargs["include_fk_parents"]) == (["items", "logs", "orders"], True)
    finally:
        dialog.close()


def test_fk_lookup_is_tried_again_after_a_failure(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    working = dialog.connector.facade.inspect_schema
    dialog.connector.facade.inspect_schema = MagicMock(side_effect=RuntimeError("core down"))
    try:
        dialog.chk_include_fk.setChecked(True)
        _uncheck(dialog, "customers")
        assert dialog.get_selected_tables() == ["items", "logs", "orders"]
        dialog.connector.facade.inspect_schema = working  # the core is back
        _uncheck(dialog, "logs")
        assert dialog.get_selected_tables() == ["customers", "items", "orders"], "the parents are read on the next change"
        assert dialog.lbl_fk_note.text() == "연결된 부모 테이블 자동 추가 1개: customers"
    finally:
        dialog.close()


def test_unchecking_a_needed_parent_keeps_a_table_export_and_says_so(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.chk_include_fk.setChecked(True)
        _uncheck(dialog, "customers")  # orders still needs it: checked again and locked
        assert dialog.get_selected_tables() == ["customers", "items", "logs", "orders"]
        assert not _enabled(_items(dialog)["customers"])
        assert not dialog._is_full_export(), "the user unchecked a table: no views, routines or events"
        assert not dialog.lbl_partial_note.isHidden()
        assert dialog.lbl_fk_note.text() == "연결된 부모 테이블 자동 추가 1개: customers"
        dialog.do_export()
        (worker,) = _CapturingWorker.created
        assert worker.task_type == "export_tables"
        assert (worker.kwargs["tables"], worker.kwargs["auto_added_tables"]) == (["customers", "items", "logs", "orders"],
                                                                                 ["customers"])
    finally:
        dialog.close()


def test_select_all_makes_an_added_parent_the_users_own_pick(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.chk_include_fk.setChecked(True)
        dialog.deselect_all_tables()
        _items(dialog)["orders"].setCheckState(Qt.CheckState.Checked)
        assert dialog._fk_auto_added == {"customers"}
        dialog.select_all_tables()
        assert dialog._fk_auto_added == set() and dialog._is_full_export()
        assert _enabled(_items(dialog)["customers"]) and _items(dialog)["customers"].toolTip() == ""
        _uncheck(dialog, "items", "orders")
        assert dialog.get_selected_tables() == ["customers", "logs"], "'전체 선택' picked customers too"
        dialog.chk_include_fk.setChecked(False)
        assert dialog.get_selected_tables() == ["customers", "logs"]
    finally:
        dialog.close()


def test_rerun_ignores_old_include_fk_parents(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.apply_job_rerun({"schema": "app", "scope": "tables", "tables": ["orders"], "include_fk_parents": True,
                                "snapshot_mode": "parallel_unsynchronized", "compression": "none", "threads": 3})
        assert dialog.chk_include_fk.isChecked() is False and dialog.chk_unsynchronized.isChecked() is False
        assert dialog.get_selected_tables() == ["orders"], "the old record never adds parents"
        assert (dialog._compression(), dialog.spin_threads.value()) == ("none", 3)
        dialog.apply_job_rerun({"schema": "app", "scope": "full"})
        assert dialog._is_full_export(), "a full rerun checks everything"
    finally:
        dialog.close()


def test_rerun_with_the_toggle_on_adds_parents_of_the_users_own_picks(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path, settings={"rust_dump_export_include_fk_parents": True})
    try:
        dialog.apply_job_rerun({"schema": "app", "scope": "tables", "tables": ["orders"]})
        assert dialog.get_selected_tables() == ["customers", "orders"] and dialog._picked_tables() == ["orders"]
    finally:
        dialog.close()


def test_toggle_states_are_remembered_but_not_the_table_selection(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.chk_include_fk.setChecked(True)
        dialog.chk_unsynchronized.setChecked(True)
        dialog.chk_allow_incomplete.setChecked(True)
        dialog.spin_threads.setValue(4)
        dialog.combo_compression.setCurrentIndex(dialog.combo_compression.findData("none"))
        _uncheck(dialog, "logs")
        dialog.do_export()
        store = dialog._test_store
    finally:
        dialog.close()
    assert {key: store[key] for key in store if key.startswith("rust_dump_export_") and key != "rust_dump_export_base_dir"
            and key != "rust_dump_export_dir"} == {
        "rust_dump_export_threads": 4, "rust_dump_export_compression": "none",
        "rust_dump_export_include_fk_parents": True, "rust_dump_export_folder_mode": "auto",
        "rust_dump_export_folder_manual_name": "",
    }, "giving up consistency or leaving objects out is chosen again for every run"
    again = _toggles_dialog(monkeypatch, tmp_path, settings={
        **store, "rust_dump_export_unsynchronized": True, "rust_dump_export_allow_incomplete": True})
    try:
        assert (again.chk_include_fk.isChecked(), again.chk_unsynchronized.isChecked(),
                again.chk_allow_incomplete.isChecked()) == (True, False, False), "older saved values are not read"
        assert (again.spin_threads.value(), again._compression()) == (4, "none")
        assert again._is_full_export(), "the table list starts fully checked again"
    finally:
        again.close()


def test_saved_values_of_the_wrong_type_are_ignored(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path, settings={
        "rust_dump_export_threads": "8", "rust_dump_export_include_fk_parents": "yes",
        "rust_dump_export_compression": "brotli", "rust_dump_export_folder_manual_name": 3})
    try:
        assert (dialog.spin_threads.value(), dialog.chk_include_fk.isChecked(), dialog._compression()) == (8, False, "zstd")
        assert dialog.input_manual_folder.text() == ""
    finally:
        dialog.close()


def test_manual_folder_name_setting_migrates(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path, settings={
        "rust_dump_export_folder_mode": "manual", "rust_dump_export_folder_manual_name": "nightly",
        "rust_dump_export_folder_use_timestamp": False, "rust_dump_export_folder_use_name": False})
    try:
        assert dialog.chk_manual_folder.isChecked() and dialog.input_manual_folder.text() == "nightly"
        assert dialog.input_manual_folder.isEnabled()
        assert Path(dialog.input_output_dir.text()).name == "nightly"
        assert "폴더 이름(nightly)" in dialog.lbl_advanced_summary.text()
    finally:
        dialog.close()
    automatic = _toggles_dialog(monkeypatch, tmp_path, settings={"rust_dump_export_folder_use_timestamp": False})
    try:
        # The old use_* switches are gone: the name is always connection + DB + time.
        assert not automatic.chk_manual_folder.isChecked() and not automatic.input_manual_folder.isEnabled()
        assert re.fullmatch(r"conn_app_\d{8}_\d{6}", Path(automatic.input_output_dir.text()).name)
    finally:
        automatic.close()


def test_advanced_summary_lists_active_toggles(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.chk_include_fk.setChecked(True)
        dialog.chk_unsynchronized.setChecked(True)
        dialog.chk_allow_incomplete.setChecked(True)
        dialog.spin_threads.setValue(2)
        dialog.combo_compression.setCurrentIndex(dialog.combo_compression.findData("none"))
        dialog.chk_manual_folder.setChecked(True)
        assert dialog.lbl_advanced_summary.text() == (
            "적용 중인 고급 설정: 연결된 부모 테이블 함께 담기, 시점 일관성 포기 허용, 담을 수 없는 객체 빼기, "
            "동시 작업 2개, 압축 안 함, 폴더 이름(비우면 export_시각)")
        dialog._toggle_advanced_options()
        assert not dialog.advanced_panel.isHidden() and dialog.btn_advanced.text() == "고급 설정 닫기"
    finally:
        dialog.close()


def test_advanced_toggles_are_locked_while_running(monkeypatch, tmp_path):
    dialog = _toggles_dialog(monkeypatch, tmp_path)
    try:
        dialog.do_export()
        assert not dialog.advanced_panel.isEnabled() and not dialog.table_group.isEnabled()
        assert not dialog.btn_export.isEnabled()
    finally:
        dialog.close()
