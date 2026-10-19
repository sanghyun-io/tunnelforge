"""Round-3 recovery messages and safe-import plan regressions."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QDialog, QMessageBox

from src.core import i18n
from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES
from tests.test_safe_restore_round2 import _dialog, _load


def test_direct_only_trigger_conflict_allows_safe_import_and_confirmation(monkeypatch):
    from src.core.production_guard import Environment
    from src.ui.dialogs import db_import_dialog as mod
    from src.ui.dialogs.import_plan_dialog import ImportPlanDialog, describe_plan

    preview = _load("rvg8_safe_preview_root.json")
    assert preview["copy_blockers"] == []
    assert preview["direct_blockers"][0]["code"] == "trigger_name_conflict"
    plan = describe_plan(preview, True)
    assert plan["blockers"] == []
    owner = _dialog()
    dialog = ImportPlanDialog(owner, preview, plan, Environment.DEVELOPMENT, "test", "app")
    try:
        assert dialog.btn_execute.isEnabled()
        monkeypatch.setattr(mod, "ImportPlanDialog", MagicMock(return_value=dialog))
        monkeypatch.setattr(dialog, "exec", lambda: QDialog.DialogCode.Accepted)
        stub = SimpleNamespace(tunnel_config={}, _plan_notes=lambda *a: [],
                               _target_endpoint_text=lambda: "test", _advanced_summary=lambda: "")
        assert mod.RustDumpImportDialog._confirm_plan(
            stub, {"verify": True, "timezone_label": "", "namespace": "app"}, preview)
    finally:
        dialog.close()
        owner.close()


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("code", ["rollback_routine_binlog_unsafe", "rollback_trigger_privilege_missing",
                                  "rollback_trigger_binlog_privilege"])
def test_rollback_plan_translates_known_codes_and_preserves_other_blockers(monkeypatch, language, code):
    warning = MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    dialog = _dialog()
    i18n.set_language(language)
    try:
        dialog._on_rollback_plan("r1", True, "", {"can_rollback": False, "blockers": [
            f"{code}: f: server detail", "foreign-key graph changed"]})
        text = warning.call_args.args[2]
        assert i18n.translate_text(CORE_CODE_SENTENCES[code]) + " (f)" in text
        assert code not in text and "server detail" not in text
        assert "foreign-key graph changed" in text
    finally:
        dialog.close()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("status", [None, "failed_triggers_recreated"])
def test_rollback_warning_omits_absent_status_and_preserves_real_line_breaks(monkeypatch, status):
    warning = MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    dialog = _dialog()
    result = {"status": status, "error": "rename rejected"} if status else {}
    try:
        dialog._on_rolled_back(False, "first line\nsecond line", result)
        body = warning.call_args.args[2]
        assert not body.startswith("-")
        assert "\\n" not in body
        assert "first line\nsecond line" in body
        if status:
            assert body.splitlines()[0] == "실패 (변경 없음, 트리거 다시 만듦)"
            assert body.splitlines()[-1] == "rename rejected"
    finally:
        dialog.close()


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("fixed_status_message", [False, True])
def test_hidden_trigger_failure_reaches_stop_dialog_and_job_record(monkeypatch, language, fixed_status_message):
    from src.ui.dialogs import db_import_dialog as mod, job_recording

    error = "safe_promotion: target_trigger_hidden: an account with TRIGGER privilege is required (ERROR 1435)"
    message = "Promotion did not change original tables." if fixed_status_message else error
    done = {"status": "failed_original_unchanged", "success": False, "original_unchanged": True,
            "message": message, "error": error, "error_code": "target_trigger_hidden"}
    reasons = []
    stub = SimpleNamespace(_promotion_action="confirm", set_ui_enabled=lambda *a: None,
                           btn_save_log=MagicMock(), _show_report_link=lambda: None, import_audit={},
                           _log_visible=MagicMock(), _cancel_requested=False, _stop_and_explain=reasons.extend,
                           label_status=MagicMock(), _close_after_cancel=False, _promotion_job_id=None,
                           _show_failure=MagicMock())
    recorded = {}
    monkeypatch.setattr(job_recording, "job_finish", lambda job_id, status, **kw: recorded.update(kw))
    i18n.set_language(language)
    try:
        mod.RustDumpImportDialog._on_promotion_finished(stub, False, message, done)
        expected = i18n.translate_text(CORE_CODE_SENTENCES["target_trigger_hidden"])
        assert reasons == ["- " + expected]
        job_recording.finish_promotion_job(SimpleNamespace(_promotion_job_id="j1", _cancel_requested=False),
                                           False, message, done)
        assert recorded["error"] == expected
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_failed_entry_displays_its_known_backup_namespace():
    dialog = _dialog()
    try:
        dialog._on_list(True, "", {"backups": [{"restore_id": "bad", "inspection_error": "endpoint mismatch",
            "backup": {"namespace": "tf_backup_bad", "verdict": "undeterminable"}}],
            "unproven_namespaces": []})
        assert dialog.table.item(0, 2).text() == "tf_backup_bad"
        assert "[bad] endpoint mismatch" in dialog.notes.toPlainText()
        assert "소유 미증명" not in dialog.notes.toPlainText()
    finally:
        dialog.close()


def test_new_target_success_first_line_has_english_translation(tmp_path):
    from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({"format": "tunnelforge-dump",
        "format_version": 5, "source_engine": "mysql", "database": "app", "tables": []}), encoding="utf-8")
    facade = MagicMock()
    facade.import_dump.return_value = {"status": "completed_new_target", "namespace_existed": False,
        "namespace_created": True, "verified": True, "original_unchanged": True, "cutover_pending": False,
        "candidate_target": {"database": "app"}}
    i18n.set_language("en")
    try:
        ok, message, _ = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "pw"), facade=facade).import_dump(
            str(tmp_path), import_mode="safe")
        assert ok, message
        assert message == "Created new target app, imported and verified it."
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_committed_preview_fixtures_are_small_and_have_no_connection_fields():
    directory = Path(__file__).parent / "fixtures" / "safe_restore_round2"
    for path in directory.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert len(text.encode()) < 5000
        assert not any(key in text.lower() for key in ('"password"', '"host"', '"user"', 'mysql://', 'postgresql://'))
