import json
from unittest.mock import MagicMock
import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from src.core.db_core_service import DbCoreFacade
from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker
from src.ui.dialogs.db_import_dialog import RustDumpImportDialog


@pytest.fixture
def promotion_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    dialog = RustDumpImportDialog()
    dialog._test_app = app  # keep the QApplication alive
    dialog.restore_config = RustDumpConfig("localhost", 3306, "operator", "secret")
    dialog.import_audit.update(restore_id="id1", report_path="report.json", verified=True,
        original_unchanged=True, original_target={"engine": "mysql", "host": "localhost", "port": 3306, "database": "original"},
        candidate_target={"engine": "mysql", "host": "localhost", "port": 3306, "database": "tf_restore_1"})
    dialog._start_promotion = MagicMock()
    yield dialog
    dialog.close()


def _stop_window(dialog):
    boxes = [box for box in dialog.findChildren(QMessageBox) if box.isVisible()]
    assert len(boxes) == 1
    return boxes[0]


def test_promotion_facade_uses_explicit_command_and_confirmation_payload():
    client = MagicMock()
    facade = DbCoreFacade(client)
    payload = {"action": "confirm", "restore_id": "r", "plan_digest": "digest", "overwrite_confirmed": True}
    facade.promote_dump(payload)
    client.request.assert_called_once_with("dump.promote", payload, on_event=None)


def test_promotion_worker_runs_dedicated_facade_and_returns_plan(monkeypatch):
    runner = MagicMock()
    runner.facade.promote_dump.return_value = {"success": True, "can_promote": False, "blockers": ["dependency"]}
    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", lambda config: runner)
    worker = RustDumpWorker("promote", RustDumpConfig("localhost", 3306, "user", "secret"), payload={"action": "plan"})
    results = []
    worker.promotion_finished.connect(lambda *result: results.append(result))
    worker.run()
    assert results[0][0] is True
    assert results[0][2]["can_promote"] is False
    runner.facade.client.shutdown.assert_called_once()


def test_failed_cutover_does_not_claim_original_unchanged(promotion_dialog, monkeypatch):
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.warning", lambda *args: None)
    promotion_dialog._promotion_action = "confirm"
    promotion_dialog._on_promotion_finished(False, "connection lost", {})
    assert promotion_dialog.import_audit["original_unchanged"] is None
    assert promotion_dialog.import_audit["restore_status"] == "promotion_outcome_unknown"
    assert promotion_dialog.import_success is False
    assert "미확인" in promotion_dialog.label_status.text()


def test_confirmed_unchanged_refusal_preserves_failure_details(promotion_dialog, monkeypatch):
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.warning", lambda *args: None)
    promotion_dialog._promotion_action = "confirm"
    promotion_dialog._on_promotion_finished(False, "stale plan", {
        "status": "failed_original_unchanged", "original_unchanged": True,
        "backup_namespace": "tf_backup_r", "phase": "validation", "message": "stale plan",
    })
    assert promotion_dialog.import_audit["original_unchanged"] is True
    assert promotion_dialog.import_audit["promotion_result"]["phase"] == "validation"
    assert "차단" in promotion_dialog.label_status.text()


def test_verify_in_copy_stop_shows_the_coded_reason(promotion_dialog, monkeypatch):
    from src.exporters.rust_dump_exporter import describe_copy_blockers, describe_core_code

    shown = []
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.warning", lambda parent, title, text: shown.append((title, text)))
    promotion_dialog._report_error_anonymously = MagicMock()
    promotion_dialog.last_import_mode = "safe"
    reason = "\n".join([describe_core_code("copy_refused"),
                        *describe_copy_blockers([{"code": "linked_target_only", "table": "notes", "detail": "notes.fk references users"}])])
    promotion_dialog.on_finished(False, reason)
    title, text = shown[-1]
    assert title == "Import 실패" and text.startswith("Import 실패 · 원본은 그대로입니다")
    assert "복사본을 만들기 전에 멈췄습니다" in text
    assert "덤프에 없는 테이블이나 뷰가" in text and text.endswith(" (notes)")
    # The core's own text, from its safe_restore_failed event, opens from '자세히'.
    core_text = "copy_refused: verify-in-copy stopped before making a copy: notes.fk references users"
    promotion_dialog.on_raw_output(json.dumps({"event": "safe_restore_failed", "status": "failed_original_untouched",
                                               "error": core_text}))
    promotion_dialog._show_failure = MagicMock()
    promotion_dialog.on_finished(False, reason)
    assert promotion_dialog._show_failure.call_args.args[2] == core_text


def test_a_plan_blocked_by_the_target_lists_the_reasons_in_korean(promotion_dialog):
    promotion_dialog._remember_promotion_plan({
        "can_promote": False, "status": "blocked", "blockers": ["notes.fk_notes references parents; ..."],
        "copy_blockers": [{"code": "linked_target_only", "table": "notes", "detail": "notes.fk_notes references parents"}]})
    blockers = promotion_dialog.import_audit["promotion_plan"]["blockers"]
    assert blockers == ["- 덤프에 없는 테이블이나 뷰가 외래 키나 뷰로 덤프 테이블과 이어져 있어, 그대로 둔 채 교체할 수 없습니다. (notes)"]


def test_cancelled_pending_promotion_cannot_start_later(promotion_dialog, monkeypatch):
    callbacks = []
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QTimer.singleShot", lambda delay, callback: callbacks.append(callback))
    promotion_dialog.worker = MagicMock()
    promotion_dialog.worker.isRunning.return_value = True
    RustDumpImportDialog._start_promotion(promotion_dialog, {"action": "confirm"})
    assert len(callbacks) == 1
    promotion_dialog._cancel_requested = True
    callbacks[0]()
    promotion_dialog._start_promotion.assert_not_called()
    promotion_dialog.worker.isRunning.return_value = False


def test_import_mode_is_required_by_the_worker_and_the_api(monkeypatch):
    """No caller falls into replace or safe by accident: the mode has no default anywhere."""
    from src.exporters import rust_dump_exporter
    with pytest.raises(TypeError):
        rust_dump_exporter.RustDumpImporter.import_dump(MagicMock(), "dump")
    runner = MagicMock()
    monkeypatch.setattr(rust_dump_exporter, "RustDumpImporter", lambda config: runner)
    with pytest.raises(TypeError):
        rust_dump_exporter.import_dump("localhost", 3306, "u", "p", "dump")
    rust_dump_exporter.import_dump("localhost", 3306, "u", "p", "dump", import_mode="replace")
    assert runner.import_dump.call_args.kwargs["import_mode"] == "replace"

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", lambda config: runner)
    runner.import_dump.reset_mock()
    finished = []
    worker = RustDumpWorker("import", RustDumpConfig("localhost", 3306, "u", "p"), input_dir="dump")
    worker.finished.connect(lambda *result: finished.append(result))
    worker.run()
    runner.import_dump.assert_not_called()
    assert finished and finished[0][0] is False
    runner.import_dump.return_value = (True, "ready", {})
    RustDumpWorker("import", RustDumpConfig("localhost", 3306, "u", "p"), input_dir="dump", import_mode="safe").run()
    assert runner.import_dump.call_args.kwargs["import_mode"] == "safe"


def test_verify_in_copy_runs_safe_restore_after_one_confirmation(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])  # noqa: F841 - keep the app alive
    from tests.test_db_import_dialog import _NOTHING_OVERWRITTEN, _fake_plan_dialog, _fake_workers, _imports, _write_dump
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    created = _fake_workers(monkeypatch, preview={**_NOTHING_OVERWRITTEN, "replace": ["users"], "create": []})
    shown = _fake_plan_dialog(monkeypatch, accept=True)
    dialog = RustDumpImportDialog(tunnel_config={"environment": "staging"})
    try:
        dialog.input_dir.setText(_write_dump(tmp_path / "dump"))
        dialog.chk_verify_copy.setChecked(True)
        dialog.do_import()
        assert len(shown) == 1 and shown[0]["plan"]["verify"] is True
        assert "먼저 복사본에서 검증" in shown[0]["toggles"]
        assert [worker.kwargs["import_mode"] for worker in _imports(created)] == ["safe"]
    finally:
        dialog._job_id = None
        dialog.close()


def test_verify_in_copy_swaps_a_clean_plan_without_asking_again(promotion_dialog, monkeypatch):
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    promotion_dialog.import_audit["restore_status"] = "ready_for_switch"
    promotion_dialog._promotion_action = "plan"
    plan = {"success": True, "can_promote": True, "plan_digest": "verified-digest", "blockers": []}
    promotion_dialog._on_promotion_finished(True, "planned", plan)
    payload = promotion_dialog._start_promotion.call_args.args[0]
    assert payload["action"] == "confirm"
    assert payload["plan_digest"] == "verified-digest" and payload["overwrite_confirmed"] is True
    assert payload["target"]["database"] == "original"
    assert promotion_dialog.import_audit["operator_choice"] == "overwrite_confirmed_before_import"


@pytest.mark.parametrize("restore_status, plan, reason", [
    ("ready_for_review", {"can_promote": True, "plan_digest": "d", "blockers": []}, "교체를 막는 다른 조건이 있습니다"),
    ("ready_for_switch", {"can_promote": False, "plan_digest": "d", "blockers": ["incoming FK"]}, "교체를 막는 다른 조건이 있습니다"),
    ("ready_for_switch", {"can_promote": True, "plan_digest": "d", "blockers": ["view dependency"]}, "교체를 막는 다른 조건이 있습니다"),
    ("ready_for_switch", {"can_promote": True, "plan_digest": "", "blockers": []}, "교체를 막는 다른 조건이 있습니다"),
    ("ready_for_switch", {"can_promote": False, "plan_digest": "d", "blockers": ["x"],
                          "copy_blockers": [{"code": "target_triggers", "table": "users", "detail": "trg"}]},
     "대상 테이블에 트리거가 있어 교체할 수 없습니다. (users)"),
])
def test_verify_in_copy_not_clean_shows_stop_and_close_only(promotion_dialog, monkeypatch, restore_status, plan, reason):
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    promotion_dialog.import_audit["restore_status"] = restore_status
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._on_promotion_finished(True, "planned", {"success": True, **plan})
    promotion_dialog._start_promotion.assert_not_called()
    box = _stop_window(promotion_dialog)
    assert [button.text() for button in box.buttons()] == ["닫기"]
    assert box.text().startswith("복사본 tf_restore_1에 넣고 검증했지만 다음 이유로 교체하지 않았습니다.")
    assert reason in box.text() and "원본은 그대로입니다" in box.text()
    assert "incoming FK" not in box.text() and "view dependency" not in box.text()  # raw core text stays in the log


def test_failed_swap_plan_stops_and_explains_without_swapping(promotion_dialog, monkeypatch):
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    promotion_dialog.import_audit["restore_status"] = "ready_for_switch"
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._on_promotion_finished(False, "core unavailable", {})
    promotion_dialog._start_promotion.assert_not_called()
    assert "교체를 막는 다른 조건이 있습니다" in _stop_window(promotion_dialog).text()
    assert any("core unavailable" in entry for entry in promotion_dialog.log_entries)


def test_cancelled_swap_plan_shows_no_window(promotion_dialog, monkeypatch):
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, MagicMock(side_effect=AssertionError(f"QMessageBox.{name}")))
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._cancel_requested = True
    promotion_dialog._on_promotion_finished(False, "작업이 취소되었습니다.", {})
    assert [box for box in promotion_dialog.findChildren(QMessageBox) if box.isVisible()] == []
    assert promotion_dialog.label_status.text() == "⏹ 교체 확인을 중지했습니다 · 원본은 그대로입니다"


def test_advanced_toggles_are_locked_while_running(promotion_dialog):
    toggles = (promotion_dialog.chk_other_name, promotion_dialog.chk_verify_copy, promotion_dialog.spin_threads,
               promotion_dialog.input_dir, promotion_dialog.btn_browse)
    promotion_dialog.set_ui_enabled(False)
    assert all(not widget.isEnabled() for widget in (*toggles, promotion_dialog.chk_timezone, promotion_dialog.combo_timezone))
    promotion_dialog.set_ui_enabled(True)
    assert all(widget.isEnabled() for widget in toggles)
    assert not promotion_dialog.chk_timezone.isEnabled()  # no dump chosen: nothing says the dump lacks a timezone


def test_the_plan_dialog_is_a_real_qdialog_subclass():
    from src.core.production_guard import SchemaConfirmDialog
    from src.ui.dialogs.import_plan_dialog import ImportPlanDialog
    assert issubclass(ImportPlanDialog, SchemaConfirmDialog) and issubclass(ImportPlanDialog, QDialog)
