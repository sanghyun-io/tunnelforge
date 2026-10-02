from unittest.mock import MagicMock
import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.db_core_service import DbCoreFacade
from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker
from src.ui.dialogs.db_import_dialog import RustDumpImportDialog, _promotion_review_text


@pytest.fixture
def promotion_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    dialog = RustDumpImportDialog()
    dialog.restore_config = RustDumpConfig("localhost", 3306, "operator", "secret")
    dialog.import_audit.update(restore_id="id1", report_path="report.json", verified=True,
        original_unchanged=True, original_target={"engine": "mysql", "host": "localhost", "port": 3306, "database": "original"})
    dialog._start_promotion = MagicMock()
    dialog._confirm_promotion_guard = MagicMock(return_value=True)
    yield dialog
    dialog.close()


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


def test_promotion_requires_explicit_yes_and_preserves_plan_digest(promotion_dialog, monkeypatch):
    question = MagicMock(return_value=QMessageBox.StandardButton.No)
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.question", question)
    plan = {"can_promote": True, "plan_digest": "reviewed-digest"}
    promotion_dialog._confirm_promotion(plan)
    assert question.call_args.args[-1] == QMessageBox.StandardButton.No
    promotion_dialog._start_promotion.assert_not_called()
    question.return_value = QMessageBox.StandardButton.Yes
    promotion_dialog._confirm_promotion(plan)
    payload = promotion_dialog._start_promotion.call_args.args[0]
    assert payload["action"] == "confirm"
    assert payload["plan_digest"] == "reviewed-digest" and payload["overwrite_confirmed"] is True
    assert payload["target"]["database"] == "original"
    promotion_dialog._confirm_promotion_guard.assert_called_once_with(plan)


def test_blocked_promotion_never_asks_to_overwrite(promotion_dialog, monkeypatch):
    question = MagicMock()
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.question", question)
    promotion_dialog._confirm_promotion({"can_promote": False, "plan_digest": "d", "blockers": ["incoming FK"]})
    question.assert_not_called()
    promotion_dialog._start_promotion.assert_not_called()


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


def test_import_worker_and_convenience_api_default_to_safe(monkeypatch):
    from src.exporters import rust_dump_exporter
    runner = MagicMock()
    runner.import_dump.return_value = (True, "ready", {})
    monkeypatch.setattr(rust_dump_exporter, "RustDumpImporter", lambda config: runner)
    rust_dump_exporter.import_dump("localhost", 3306, "u", "p", "dump")
    assert runner.import_dump.call_args.kwargs["import_mode"] == "safe"
    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", lambda config: runner)
    RustDumpWorker("import", RustDumpConfig("localhost", 3306, "u", "p"), input_dir="dump").run()
    assert runner.import_dump.call_args.args[3] == "safe"


def test_review_summary_labels_unknown_data_and_counts_without_forged_lines():
    text = _promotion_review_text({
        "original_target": {"database": "original"}, "candidate_target": {"database": "candidate"},
        "backup_namespace": "backup", "summary": {"data_not_compared": ["sample"],
            "row_counts": {"sample": {"original": None, "candidate": 3}}, "schema_changed_tables": ["sample"]},
    }, {"name": "profile\nforged"})
    assert "sample: 미확인 / 3" in text
    assert "행 수만으로 데이터 일치를 보장하지 않습니다." in text
    assert "\nforged" not in text


def test_overwrite_mode_runs_safe_restore_after_one_confirmation(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    question = MagicMock(return_value=QMessageBox.StandardButton.No)
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.question", question)
    dialog = RustDumpImportDialog()
    dialog.input_dir.setText(str(tmp_path))
    dialog.radio_overwrite.setChecked(True)
    dialog._confirm_production_guard = MagicMock(return_value=True)
    dialog._begin_error_report_operation = MagicMock()
    assert dialog._get_selected_import_mode() == "overwrite"
    dialog.do_import()
    assert question.call_args.args[1] == "덮어쓰기 확인"
    assert question.call_args.args[-1] == QMessageBox.StandardButton.No
    dialog._begin_error_report_operation.assert_not_called()
    assert dialog.worker is None
    dialog.close()


def test_overwrite_mode_swaps_a_clean_plan_without_the_review_dialog(promotion_dialog):
    promotion_dialog._auto_overwrite = True
    promotion_dialog.import_audit["restore_status"] = "ready_for_switch"
    promotion_dialog._review_promotion_plan = MagicMock()
    promotion_dialog._promotion_action = "plan"
    plan = {"success": True, "can_promote": True, "plan_digest": "verified-digest", "blockers": []}
    promotion_dialog._on_promotion_finished(True, "planned", plan)
    promotion_dialog._review_promotion_plan.assert_not_called()
    payload = promotion_dialog._start_promotion.call_args.args[0]
    assert payload["action"] == "confirm"
    assert payload["plan_digest"] == "verified-digest" and payload["overwrite_confirmed"] is True
    assert payload["target"]["database"] == "original"
    assert promotion_dialog._auto_overwrite is False  # one shot


@pytest.mark.parametrize("restore_status, plan", [
    ("ready_for_review", {"can_promote": True, "plan_digest": "d", "blockers": []}),
    ("ready_for_switch", {"can_promote": False, "plan_digest": "d", "blockers": ["incoming FK"]}),
    ("ready_for_switch", {"can_promote": True, "plan_digest": "d", "blockers": ["view dependency"]}),
    ("ready_for_switch", {"can_promote": True, "plan_digest": "", "blockers": []}),
])
def test_overwrite_mode_falls_back_to_review_when_the_swap_needs_a_decision(promotion_dialog, restore_status, plan):
    promotion_dialog._auto_overwrite = True
    promotion_dialog.import_audit["restore_status"] = restore_status
    promotion_dialog._review_promotion_plan = MagicMock()
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._on_promotion_finished(True, "planned", {"success": True, **plan})
    promotion_dialog._review_promotion_plan.assert_called_once()
    promotion_dialog._start_promotion.assert_not_called()


def test_safe_mode_still_reviews_every_plan(promotion_dialog):
    promotion_dialog.import_audit["restore_status"] = "ready_for_switch"
    promotion_dialog._review_promotion_plan = MagicMock()
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._on_promotion_finished(True, "planned", {"success": True, "can_promote": True, "plan_digest": "d"})
    promotion_dialog._review_promotion_plan.assert_called_once()
    promotion_dialog._start_promotion.assert_not_called()


def test_overwrite_flag_is_cleared_when_the_auto_plan_fails(promotion_dialog, monkeypatch):
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.QMessageBox.warning", MagicMock())
    promotion_dialog._auto_overwrite = True
    promotion_dialog.import_audit["restore_status"] = "ready_for_switch"
    promotion_dialog._review_promotion_plan = MagicMock()
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._on_promotion_finished(False, "core unavailable", {})
    assert promotion_dialog._auto_overwrite is False
    # A later manual review click must show the review dialog, not swap.
    promotion_dialog._on_promotion_finished(True, "planned", {"success": True, "can_promote": True, "plan_digest": "d"})
    promotion_dialog._review_promotion_plan.assert_called_once()
    promotion_dialog._start_promotion.assert_not_called()


def test_auto_overwrite_records_the_operator_authorization(promotion_dialog):
    promotion_dialog._auto_overwrite = True
    promotion_dialog.import_audit["restore_status"] = "ready_for_switch"
    promotion_dialog._promotion_action = "plan"
    promotion_dialog._on_promotion_finished(True, "planned", {"success": True, "can_promote": True, "plan_digest": "d"})
    assert promotion_dialog.import_audit["operator_choice"] == "overwrite_confirmed_before_import"


def test_overwrite_radio_is_locked_with_the_other_modes(promotion_dialog):
    promotion_dialog.set_ui_enabled(False)
    assert all(not button.isEnabled() for button in promotion_dialog.btn_import_mode.buttons())
    promotion_dialog.set_ui_enabled(True)
    assert promotion_dialog.radio_overwrite.isEnabled()
