import json
import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

def _load(name):
    directory=os.getenv("TF_ROUND2_EVIDENCE")
    if not directory:
        pytest.skip("requires disposable round-2 live preview evidence")
    return json.loads((Path(directory)/name).read_text(encoding="utf-8"))


def _dialog():
    from src.ui.dialogs.backup_lifecycle_dialog import BackupLifecycleDialog
    app = QApplication.instance() or QApplication([])
    dialog = BackupLifecycleDialog({"engine": "mysql"}, ["C:/dump"])
    dialog._test_app = app
    dialog.refresh = MagicMock()
    return dialog

@pytest.mark.parametrize("source", ["live_failed_no_change", "rollback_unknown"])
def test_rvf15_non_rolled_back_success_shows_empty_warning(monkeypatch, source):
    if source == "live_failed_no_change":
        result = _load("d15_rollback_failed_no_change.json")
    else:
        result = {"status": "rollback_unknown", "message": "The recovery outcome is unknown. Reconcile the recorded identities before any retry; nothing was deleted."}
    warning, information = MagicMock(), MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    monkeypatch.setattr(QMessageBox, "information", information)
    dialog = _dialog()
    try:
        dialog._on_rolled_back(True, "", result)  # worker emits (True, "", result)
    finally:
        dialog.close()
    assert warning.call_args.args[1] == "복구 실패"
    assert result["message"] in warning.call_args.args[2]
    assert result.get("error","") in warning.call_args.args[2]
    assert warning.call_args.args[2]
    information.assert_not_called()


def test_rvf20_rolled_back_verdicts_have_no_label(monkeypatch):
    from src.ui.dialogs.backup_lifecycle_dialog import VERDICT_LABELS, status_label
    information = MagicMock()
    monkeypatch.setattr(QMessageBox, "information", information)
    dialog = _dialog()
    try:
        entry = {"restore_id": "r1", "journal_status": "promoted", "backup": {"namespace": "tf_backup_1", "verdict": "rolled_back", "tables": []}}
        dialog._on_list(True, "", {"backups": [entry, {**entry, "restore_id": "r2", "backup": {**entry["backup"], "verdict": "rollback_incomplete"}}]})
        cells = [dialog.table.item(0, 5).text(), dialog.table.item(1, 5).text()]
        dialog._on_reconciled(True, "", {"conclusion": "rolled_back", "message": "m"})
    finally:
        dialog.close()
    assert status_label(VERDICT_LABELS, "rolled_back") == "백업으로 복구됨"
    assert status_label(VERDICT_LABELS, "rollback_incomplete") == "테이블 복구됨 · 저장 프로그램 복원 필요"
    assert cells == ["백업으로 복구됨", "테이블 복구됨 · 저장 프로그램 복원 필요"]
    assert information.call_args.args[2].startswith("판정: 백업으로 복구됨")
def test_rvf23_safe_preview_suggests_turning_off_verify_for_a_refused_direct_import():
    from src.ui.dialogs.import_plan_dialog import describe_plan
    safe = describe_plan(_load("g23_safe_preview.json"), verify=True)["blockers"]
    direct = describe_plan(_load("g23_replace_preview.json"), verify=False)["blockers"]
    assert "'먼저 복사본에서 검증하고 교체'를 끄면 진행할 수 있습니다." not in safe
    assert any("트리거 없이" in line for line in safe)
    assert any("같은 이름의 트리거" in line for line in direct)
def test_rvf25_one_missing_grant_is_shown_twice():
    from src.ui.dialogs.import_plan_dialog import describe_plan
    trigger = describe_plan(_load("i25_no_trigger_preview.json"), verify=True)["blockers"]
    routine = describe_plan(_load("i25_no_create_routine_preview.json"), verify=True)["blockers"]
    sentence = "저장 프로그램을 만들거나 되돌릴 권한이 없습니다."
    assert any("TRIGGER 권한이 없습니다" in line for line in trigger) and not any(sentence in line for line in trigger)
    assert sum(sentence in line for line in routine) == 1
    assert not any("tf_restore_*" in line for line in routine)
def test_rvf28_no_trigger_grant_sentences_for_a_target_without_triggers():
    from src.ui.dialogs.import_plan_dialog import describe_plan
    lines = describe_plan(_load("i28_no_trigger_grant_preview.json"), verify=True)["blockers"]
    assert not any("저장 프로그램을 만들거나 되돌릴 권한이 없습니다" in line for line in lines)
    assert not any("교체할 테이블에 이미 트리거가 있어" in line for line in lines)
def test_rvf30_english_sentences_name_a_toggle_that_does_not_exist():
    from src.core import i18n
    from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES
    i18n.set_language("en")
    try:
        toggle = i18n.translate_text("먼저 복사본에서 검증하고 교체")
        texts = [i18n.translate_text(CORE_CODE_SENTENCES[code]) for code in ("copy_routine_binlog_unsafe", "view_calls_dump_routine")]
        hint = i18n.translate_text("'먼저 복사본에서 검증하고 교체'를 끄면 진행할 수 있습니다.")
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
    assert toggle == "Verify in a copy first, then swap"
    assert all(toggle in text and "then switch" not in text for text in texts)
    assert "then swap" in hint
def test_rvf31_rollback_incomplete_without_names(monkeypatch):
    warning = MagicMock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    dialog = _dialog()
    try:
        dialog._on_rolled_back(True, "", {"status": "rollback_incomplete", "missing_programs": [], "pending_program": None,
                                          "program_verification_error": "Lost connection to MySQL server during query"})
    finally:
        dialog.close()
    text = warning.call_args.args[2]
    assert "돌아오지 않았습니다: ." not in text
    assert "저장 프로그램 상태를 확인하지 못했습니다" in text
    assert "Lost connection" in text
def test_rvf32_review_sentence_is_glued_to_the_last_program_line(tmp_path):
    from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter
    (tmp_path / "_tunnelforge_dump.json").write_text(json.dumps({"format": "tunnelforge-dump", "format_version": 5, "source_engine": "mysql", "database": "app", "tables": [{"name": "samples", "path": "samples", "rows": 0, "chunks": 0}]}), encoding="utf-8")
    class Facade:
        def import_dump(self, payload, on_event=None):
            return {"success": True, "status": "ready_for_review", "verified": True, "original_unchanged": True, "cutover_pending": True,
                    "candidate_target": {"engine": "mysql", "database": "tf_restore_x"},
                    "programs": {"create": ["routine:greet", "event:tick"]},
                    "blockers": ["Original database objects outside the declared dump model require a preservation review before switching."]}
    ok, message, _ = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "pw"), facade=Facade()).import_dump(str(tmp_path), import_mode="safe")
    assert ok, message
    assert "교체 전에 따로 확인할 것이 있습니다." in message.splitlines()
    assert "만들 루틴·이벤트: 루틴 greet, 이벤트 tick" in message.splitlines()

@pytest.mark.parametrize("language,labels",[("ko",("원래 트리거","검증용 복사본")),("en",("original triggers","the verification copy"))])
def test_internal_copy_placeholders_are_translated(language,labels):
    from src.core import i18n
    from src.exporters.rust_dump_exporter import describe_copy_blockers
    i18n.set_language(language)
    try:
        line=describe_copy_blockers([{"code":"program_privilege_missing","table":"original triggers"},{"code":"program_privilege_missing","table":"tf_restore_*"},{"code":"program_privilege_missing","table":"tf_restore_*"}])[0]
        assert all(label in line for label in labels)
        assert "tf_restore_*" not in line
        assert line.count(labels[1])==1
    finally:
        i18n.set_language("ko")

@pytest.mark.parametrize("code",["copy_routine_binlog_unsafe","target_routine_binlog_unsafe","view_calls_dump_routine"])
@pytest.mark.parametrize("language",["ko","en"])
def test_direct_blockers_suppress_even_embedded_verify_off_advice(code,language):
    from src.core import i18n
    from src.ui.dialogs.import_plan_dialog import describe_plan
    i18n.set_language(language)
    try:
        lines=describe_plan({"blockers":[],"direct_blockers":[{"code":"trigger_name_conflict","table":"p.guard"}],"copy_blockers":[{"code":code,"table":"v"}]},True)["blockers"]
        assert not any("끄" in line or "Turning off" in line or "Turn off" in line for line in lines)
    finally:i18n.set_language("ko")

def test_backup_list_shows_per_entry_inspection_error():
    dialog=_dialog()
    try:
        dialog._on_list(True,"",{"backups":[{"restore_id":"bad","inspection_error":"journal endpoint mismatch","backup":{"verdict":"undeterminable"}},{"restore_id":"good","backup":{"verdict":"promoted"}}]})
        assert dialog.table.rowCount()==2
        assert "journal endpoint mismatch" in dialog.notes.toPlainText()
    finally:dialog.close()
