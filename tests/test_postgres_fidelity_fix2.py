"""Expected RLS refusals use localized guidance and retain technical details."""
from unittest.mock import MagicMock

import pytest

from src.core import i18n
from src.core.backup_task_executor import BackupTaskExecutor
from src.core.db_core_client import _format_error_event
from src.core.schedule_config import ScheduleConfig
from src.exporters import rust_dump_exporter as rde
from tests.test_db_export_dialog import _dialog, _finished_worker
from tests.test_cross_engine_migration_dialog import make_dialog


TABLE = "orders archive:2024"
DETAIL = f'row_security_blocks_read: {TABLE}: postgresql source probe error: db error; code=42501; message="{TABLE}" 테이블의 로우 단위 보안 정책에 의해 쿼리가 영향을 받음'
EVENT = {"event": "error", "error_code": "row_security_blocks_read", "message": DETAIL}


@pytest.fixture(params=["ko", "en"])
def sentence(request):
    i18n.install_qt_i18n()
    i18n.set_language(request.param)
    yield rde.describe_core_code("row_security_blocks_read", TABLE)
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


class Facade:
    def __init__(self):
        self.client = MagicMock()

    def hello(self):
        return {"service": "tunnelforge-core", "protocol_version": "1", "capabilities": ["dump.run", rde.MYSQL_SNAPSHOT_AUTO_CAPABILITY]}

    def run_dump(self, payload, on_event=None):
        if on_event:
            on_event(EVENT)
        raise rde.DbCoreServiceError(_format_error_event(EVENT), error_code=EVENT["error_code"], payload=EVENT)

    def inspect_schema(self, endpoint):
        return {"tables": [{"name": "secured", "foreign_keys": []}]}


def test_export_refusal_is_localized_with_details_and_no_anonymous_report(sentence, monkeypatch, tmp_path):
    exporter = rde.RustDumpExporter(rde.RustDumpConfig("localhost", 5432, "reader", "dummy", engine="postgresql"), facade=Facade())
    ok, message = exporter.export_full_schema("app", str(tmp_path / "out"))
    assert not ok
    dialog = _dialog(monkeypatch)
    dialog.worker = _finished_worker()
    boxes = []
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.QMessageBox.exec", lambda box: boxes.append((box.text(), box.detailedText())))
    try:
        dialog.on_finished(False, message)
        assert boxes == [(sentence, message)]
        visible = "\n".join(dialog.txt_log.item(i).text() for i in range(dialog.txt_log.count()))
        assert sentence in visible and "postgresql source probe error" not in visible
        dialog._report_error_anonymously.assert_not_called()
    finally:
        dialog.worker = None
        dialog.close()


@pytest.mark.parametrize("tables", [[], ["secured"]])
def test_scheduled_backup_localizes_full_and_table_failures_and_keeps_detail(sentence, monkeypatch, tmp_path, tables):
    monkeypatch.setattr(rde, "DbCoreFacade", Facade)
    finished = MagicMock()
    monkeypatch.setattr("src.core.backup_task_executor.job_finish", finished)
    log_writer = MagicMock()
    executor = BackupTaskExecutor(lambda _: (MagicMock(host="localhost", port=5432, user="reader", password="dummy", engine="postgresql", release=None), ""), log_writer)
    schedule = ScheduleConfig(id="rls", name="nightly", tunnel_id="t", schema="app", database="db", tables=tables, output_dir=str(tmp_path))
    ok, message = executor.execute(schedule)
    assert not ok and message == sentence
    assert sentence in log_writer.log_execution.call_args.args[2]
    assert DETAIL in log_writer.log_execution.call_args.args[2]
    assert finished.call_args.kwargs["error"] == sentence
    assert DETAIL in finished.call_args.kwargs["details"]["core_error"]


def test_temporal_preflight_uses_rls_guidance_and_keeps_tooltip_detail(sentence):
    raw = "could not check the dates and times in secured: " + DETAIL
    dialog = make_dialog()
    try:
        dialog._populate_safety_issue_table([{"blocking": True, "location": TABLE, "message": raw, "suggestion": "Check SELECT permission"}])
        assert dialog.tbl_safety_issues.item(0, 2).text() == sentence
        assert dialog.tbl_safety_issues.item(0, 2).toolTip() == raw
        assert dialog.tbl_safety_issues.item(0, 3).text() == "-"
    finally:
        dialog.close()


def test_verify_source_error_keeps_guidance_and_details(sentence):
    dialog = make_dialog()
    try:
        dialog._update_verification_result({"success": False, "mismatches": [{"kind": "error", "side": "source", "table": TABLE, "message": DETAIL}]})
        text = dialog.txt_verify_result.toPlainText()
        assert sentence in text and DETAIL in text
    finally:
        dialog.close()
