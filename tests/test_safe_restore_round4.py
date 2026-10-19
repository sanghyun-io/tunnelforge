"""Recorded rollback verdicts and actionable promotion/rehearsal messages."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QMessageBox

from src.core import i18n
from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES, describe_specific_promotion_failure
from src.ui.dialogs.backup_lifecycle_dialog import BACKUP_NOTE_LABELS
from tests.test_safe_restore_round2 import _dialog


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("note", list(BACKUP_NOTE_LABELS))
def test_core_backup_notes_are_visible_in_list_and_reconcile(monkeypatch, language, note):
    info = MagicMock()
    monkeypatch.setattr(QMessageBox, "information", info)
    dialog = _dialog()
    i18n.set_language(language)
    try:
        expected = i18n.translate_text(BACKUP_NOTE_LABELS[note])
        assert expected == note if language == "en" else expected == BACKUP_NOTE_LABELS[note]
        dialog._on_reconciled(True, "", {"conclusion": "not_promoted", "message": "Core result",
                                         "evidence": {"note": note}})
        assert expected in info.call_args.args[2]
        dialog._on_list(True, "", {"backups": [{"restore_id": "r1", "backup": {
            "verdict": "rollback_incomplete", "note": note}}]})
        assert f"[r1] {expected}" in dialog.notes.toPlainText()
    finally:
        dialog.close()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_incomplete_row_retains_program_error_candidate_and_pending_program():
    dialog = _dialog()
    try:
        dialog._on_list(True, "", {"backups": [{"restore_id": "r1", "candidate": {"namespace": "tf_restore_r1"},
            "backup": {"verdict": "rollback_incomplete", "missing_programs": ["routine:f"],
                       "pending_program": {"kind": "FUNCTION", "name": "f"},
                       "program_verification_error": "stored program definitions cannot be saved"}}]})
        assert dialog.table.item(0, 5).text() == "테이블 복구됨 · 저장 프로그램 복원 필요"
        assert dialog.table.item(0, 6).text() == "tf_restore_r1"
        assert "[r1] stored program definitions cannot be saved" in dialog.notes.toPlainText()
        assert dialog.entries[0]["backup"]["pending_program"]["name"] == "f"
    finally:
        dialog.close()


def _stub():
    reasons = []
    return SimpleNamespace(_promotion_action="confirm", set_ui_enabled=lambda *a: None,
        btn_save_log=MagicMock(), _show_report_link=lambda: None, import_audit={},
        _log_visible=MagicMock(), _cancel_requested=False, _stop_and_explain=reasons.extend,
        label_status=MagicMock(), _close_after_cancel=False, _promotion_job_id=None,
        _show_failure=MagicMock(), progress_bar=MagicMock()), reasons


@pytest.mark.parametrize("case", ["rvh4a", "rvh4b", "rvh4c", "promoted_incomplete"])
@pytest.mark.parametrize("language", ["ko", "en"])
def test_promotion_failure_retains_status_guidance_in_window_and_job(monkeypatch, case, language):
    from src.ui.dialogs import db_import_dialog as mod, job_recording
    if case == "promoted_incomplete":
        done = {"status": case, "original_unchanged": False,
            "message": "Tables were promoted, but stored programs are missing. Check the report before recovery.",
            "error": "safe_promotion: atomic rename: IoError { server disconnected }"}
    else:
        path = Path(__file__).parent / "fixtures" / "safe_restore_round4" / f"{case}.json"
        done = json.loads(path.read_text(encoding="utf-8"))
    stub, reasons = _stub()
    recorded = {}
    monkeypatch.setattr(job_recording, "job_finish", lambda job_id, status, **kw: recorded.update(kw))
    i18n.set_language(language)
    try:
        mod.RustDumpImportDialog._on_promotion_finished(stub, False, done["message"], done)
        job_recording.finish_promotion_job(SimpleNamespace(_promotion_job_id="j1", _cancel_requested=False),
                                           False, done["message"], done)
        error = recorded["error"]
        generic = i18n.translate_text(CORE_CODE_SENTENCES["safe_promotion"])
        assert generic not in error
        if done["status"] in ("promoted_incomplete","failed_original_triggers_missing"):
            from src.exporters.rust_dump_exporter import describe_incomplete_promotion
            assert error.startswith(describe_incomplete_promotion(done["status"]))
        else:
            assert done["message"] in error
        if case == "rvh4a":
            body = stub._show_failure.call_args.args[1]
            assert "Retain all recorded namespaces" in body
            assert "Retain all recorded namespaces" in error
            assert error.startswith("전환 결과를 알 수 없습니다.")
            assert generic not in body
        elif case == "rvh4c":
            assert reasons == ["- " + i18n.translate_text(mod._PLAN_BLOCKED_REASON)]
            assert "실행 로그" in reasons[0] if language == "ko" else "log" in reasons[0].lower()
        else:
            sentence = ("테이블은 교체했지만 저장 프로그램이 빠져 있습니다. 백업을 보존하고 복구하세요."
                        if case == "promoted_incomplete" else
                        "테이블은 교체하지 않았지만 원래 트리거를 되살리지 못했습니다. 보고서에 저장된 정의로 복구하세요.")
            assert i18n.translate_text(sentence) in stub._show_failure.call_args.args[1]
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("wrapper", ["", "safe_promotion: ", "Rust DB Core import 오류: "])
def test_only_specific_error_codes_override_status_guidance(wrapper):
    assert describe_specific_promotion_failure({"error": wrapper + "safe_promotion: atomic rename failed"}) is None
    sentence = describe_specific_promotion_failure({"error": "safe_promotion: target_trigger_hidden: TRIGGER required"})
    assert sentence == CORE_CODE_SENTENCES["target_trigger_hidden"]
    assert describe_specific_promotion_failure({"error_code": "target_trigger_hidden", "error": "safe_promotion: fail"}) == sentence


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("name", ["a:b", "a: b"])
def test_rollback_blocker_keeps_the_full_colon_name(monkeypatch, language, name):
    warning = MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    dialog = _dialog()
    i18n.set_language(language)
    try:
        dialog._on_rollback_plan("r1", True, "", {"can_rollback": False, "blockers": [
            f"rollback_routine_binlog_unsafe: {name}: the previous target function cannot be recreated"]})
        assert f"({name})" in warning.call_args.args[2]
    finally:
        dialog.close()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_fixed_core_rollback_messages_have_no_name_separator():
    source = (Path(__file__).parents[1] / "migration_core" / "src" / "safe_restore.rs").read_text(encoding="utf-8")
    start = source.index("pub(crate) fn target_function_refusals")
    end = source.index("/// What verify-in-copy refuses", start)
    messages = re.findall(r'"([^"\n]+)"\.into\(\)\)\)', source[start:end])
    assert len(messages) == 3, messages
    assert all(": " not in message for message in messages)


def test_renamed_lifecycle_tests_have_no_workflow_name_filters():
    root = Path(__file__).parents[1]
    workflows = "\n".join(p.read_text(encoding="utf-8") for p in (root / ".github" / "workflows").glob("*.yml"))
    for name in ("rv204_unrelated_routine_created_after_promotion_blocks_backup_cleanup",
                 "rv204_dumped_not_preserve_event_firing_after_promotion_flips_verdict"):
        assert name not in workflows
        assert name not in (root / "migration_core" / "tests" / "live_backup_lifecycle.rs").read_text(encoding="utf-8")
