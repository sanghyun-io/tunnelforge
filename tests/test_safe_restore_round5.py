"""Round-5 status-first job history and PostgreSQL unknown-outcome diagnostics."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.core import i18n
from src.exporters.rust_dump_exporter import (
    CORE_CODE_SENTENCES, RustDumpConfig, describe_incomplete_promotion, without_import_toggle_hints,
)
from src.ui.dialogs import db_import_dialog as dialog_module, job_recording
from tests.test_dump_promotion_ui import _stop_window, promotion_dialog  # noqa: F401
from tests.test_safe_restore_round4 import _stub


def _load(name):
    return json.loads((Path(__file__).parent / "fixtures" / "safe_restore_round5" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("status", ["promoted_incomplete", "failed_original_triggers_missing"])
@pytest.mark.parametrize("code", ["program_create_failed", "trigger_binlog_privilege"])
def test_partial_promotion_job_starts_with_the_dialog_status_and_omits_import_toggles(
        monkeypatch, language, status, code):
    done = _load("trigger_create_failure.json")
    done["status"] = status
    if code == "trigger_binlog_privilege":
        done["error"] = "trigger_binlog_privilege: binary logging is enabled (ERROR 1419)"
    recorded = {}
    monkeypatch.setattr(job_recording, "job_finish", lambda job_id, state, **kw: recorded.update(kw))
    i18n.set_language(language)
    try:
        stub, _ = _stub()
        dialog_module.RustDumpImportDialog._on_promotion_finished(stub, False, done["message"], done)
        sentence = describe_incomplete_promotion(status)
        assert stub._show_failure.call_args.args[1].splitlines()[0] == sentence
        job_recording.finish_promotion_job(SimpleNamespace(_promotion_job_id="j1", _cancel_requested=False),
                                           False, done["message"], done)
        error = recorded["error"]
        assert error.splitlines()[0] == sentence
        assert i18n.translate_text(CORE_CODE_SENTENCES[code]).split(".")[0] in error
        for name in ("먼저 복사본에서 검증하고 교체", "트리거 없이 테이블만 가져오기",
                     "Verify in a copy first, then swap", "Import tables without triggers"):
            assert name not in error
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("raw_error", ["db error", "error communicating with database: connection reset (os error 10054)"])
def test_pg_unknown_outcome_keeps_driver_text_in_details_and_job_metadata(
        promotion_dialog, monkeypatch, raw_error):  # noqa: F811
    done = _load("pg_terminated_cutover.json")
    done["error"] = raw_error
    promotion_dialog.import_audit["original_target"] = {"engine": "postgresql", "database": "tf_test", "schema": "original"}
    promotion_dialog._promotion_action = "confirm"
    promotion_dialog._on_promotion_finished(False, done["message"], done)
    box = _stop_window(promotion_dialog)
    assert "Retain all recorded namespaces" in box.text()
    assert raw_error not in box.text()
    information = MagicMock()
    monkeypatch.setattr(dialog_module.QMessageBox, "information", information)
    next(button for button in box.buttons() if button.text() == i18n.translate_text("자세히")).click()
    assert raw_error in information.call_args.args[2]
    recorded = {}
    monkeypatch.setattr(job_recording, "job_finish", lambda job_id, state, **kw: recorded.update(kw))
    job_recording.finish_promotion_job(SimpleNamespace(_promotion_job_id="j1", _cancel_requested=False),
                                       False, done["message"], done)
    assert done["message"] in recorded["error"]
    assert recorded["details"]["core_error"] == raw_error


def test_promotion_worker_preserves_pg_guidance_and_structured_driver_error(monkeypatch):
    from src.ui.workers.rust_dump_worker import RustDumpWorker
    done = _load("pg_terminated_cutover.json")
    runner = MagicMock()
    runner.facade.promote_dump.return_value = done
    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", lambda config: runner)
    worker = RustDumpWorker("promote", RustDumpConfig("localhost", 5432, "test", "dummy", engine="postgresql"),
                            payload={"action": "confirm"})
    results = []
    worker.promotion_finished.connect(lambda *result: results.append(result))
    worker.run()
    assert results == [(False, done["message"], done)]
    assert results[0][2]["error"] == "db error"
    runner.facade.client.shutdown.assert_called_once()


@pytest.mark.parametrize("language", ["ko", "en"])
def test_import_toggle_hints_can_be_removed_from_translated_specific_sentences(language):
    i18n.set_language(language)
    try:
        text = i18n.translate_text(CORE_CODE_SENTENCES["trigger_binlog_privilege"])
        cleaned = without_import_toggle_hints(text)
        assert "log_bin_trust_function_creators=1" in cleaned
        assert "트리거 없이 테이블만 가져오기" not in cleaned
        assert "Import tables without triggers" not in cleaned
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
