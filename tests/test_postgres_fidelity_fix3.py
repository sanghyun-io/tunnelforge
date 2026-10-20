"""Actual error-only migration events and persisted RLS diagnostics."""
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core import i18n, job_history as jh
from src.core.backup_task_executor import BackupTaskExecutor
from src.core.execution_log_writer import ExecutionLogWriter
from src.core.schedule_config import ScheduleConfig
from src.exporters import rust_dump_exporter as rde
from src.ui.dialogs import cross_engine_migration_dialog as dialogs
from src.ui.workers.cross_engine_migration_worker import CrossEngineMigrationWorker
from tests.test_cross_engine_migration_dialog import make_dialog
from tests.test_postgres_fidelity_fix2 import Facade

EVENTS = json.loads((Path(__file__).parent / "fixtures/postgres_rls_migrate_events.json").read_text(encoding="utf-8"))
RAW = EVENTS[-1]["message"]


@pytest.fixture(params=["ko", "en"])
def language(request):
    i18n.install_qt_i18n()
    i18n.set_language(request.param)
    yield request.param
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


class Process:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = iter(json.dumps(event) + "\n" for event in EVENTS)
        self.stderr = io.StringIO()

    def wait(self):
        return 0

    def poll(self):
        return 0


class Worker(CrossEngineMigrationWorker):
    def __init__(self, command, payload, *args, **kwargs):
        super().__init__(command, payload, helper_path="tunnelforge-core", popen_factory=lambda *a, **k: Process())

    def start(self):
        self.run()


def test_error_only_migrate_sequence_updates_result_and_persisted_job(language, monkeypatch, tmp_path):
    assert [event["event"] for event in EVENTS] == ["phase", "error"]
    history = jh.JobHistory(tmp_path / "jobs.json")
    monkeypatch.setattr(jh, "make_history", lambda: history)
    monkeypatch.setattr(dialogs, "CrossEngineMigrationWorker", Worker)
    dialog = make_dialog()
    try:
        dialog._start_command_with_payload("migrate", dialog._payload())
        sentence = rde.describe_core_code("row_security_blocks_read", "orders")
        summary = dialog.lbl_migration_result.text()
        assert sentence in summary and RAW in summary
        assert not dialog.lbl_migration_result.isHidden()
        assert dialog.btn_cleanup_failed.isHidden()
        assert "상세 실패 원인을 반환하지" not in summary and "did not return a detailed failure" not in summary
        assert "Target 정리 후" not in summary
        [record] = jh.JobHistory(tmp_path / "jobs.json").list()
        assert record.status == jh.STATUS_FAILED
        assert record.error_summary == sentence
        assert record.details["core_error"] == RAW
    finally:
        dialog.worker = None
        dialog.close()


@pytest.mark.parametrize("overlong", [False, True])
def test_backup_real_job_history_preserves_hint_and_400_character_limit(language, monkeypatch, tmp_path, overlong):
    raw = RAW + "; hint=To disable the policy for the table's owner, use ALTER TABLE NO FORCE ROW LEVEL SECURITY."
    if overlong:
        raw += " extra diagnostic" * 30
    event = {"event": "error", "error_code": "row_security_blocks_read", "message": raw}

    class BackupFacade(Facade):
        def run_dump(self, payload, on_event=None):
            from src.core.db_core_client import _format_error_event
            if on_event:
                on_event(event)
            raise rde.DbCoreServiceError(_format_error_event(event), error_code=event["error_code"], payload=event)

    monkeypatch.setattr(rde, "DbCoreFacade", BackupFacade)
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(jh, "make_history", lambda: jh.JobHistory(path))
    monkeypatch.setattr("src.core.execution_log_writer.platform_log_dir", lambda: tmp_path / "logs")
    log = MagicMock(wraps=ExecutionLogWriter())
    executor = BackupTaskExecutor(lambda _: (MagicMock(host="localhost", port=5432, user="reader", password="dummy", engine="postgresql", release=None), ""), log)
    schedule = ScheduleConfig(id="rls", name="nightly", tunnel_id="t", schema="app", database="db", output_dir=str(tmp_path / "out"))
    ok, sentence = executor.execute(schedule)
    assert not ok and "BYPASSRLS" in sentence
    [record] = jh.JobHistory(path).list()
    full = "Export 실패: Rust DB Core export 오류: " + raw
    assert record.error_summary == sentence
    assert record.details["core_error"] == jh.scrub_text(full, jh.MAX_ERROR_CHARS)
    assert "NO FORCE ROW LEVEL SECURITY" in record.details["core_error"]
    assert len(record.details["core_error"]) == jh.MAX_ERROR_CHARS if overlong else len(record.details["core_error"]) < jh.MAX_ERROR_CHARS
    assert sentence in log.log_execution.call_args.args[2] and full in log.log_execution.call_args.args[2]
    [entry] = log.get_logs()
    assert sentence in entry["message"] and full in entry["message"]
