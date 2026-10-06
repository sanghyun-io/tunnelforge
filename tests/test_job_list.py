"""Job list (TF-STATUS-132): recording hooks of the export / import / promotion / migration dialogs,
the job list dialog, re-open policy and the main-window wiring."""
import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core import job_history as jh
from src.core.job_history import JobHistory
from src.ui.dialogs import job_list_dialog, job_recording
from src.ui.dialogs.db_export_dialog import RustDumpExportDialog
from src.ui.dialogs.db_import_dialog import RustDumpImportDialog
from src.ui.dialogs.job_list_dialog import JobListDialog

_app = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def history(tmp_path, monkeypatch):
    store = JobHistory(tmp_path / "jobs.json")
    monkeypatch.setattr(jh, "make_history", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("src.ui.dialogs.db_export_dialog.check_rust_dump", lambda: (True, "ok"))
    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.check_rust_dump", lambda: (True, "ok"))
    for name in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Yes)


def only(history):
    records = history.list()
    assert len(records) == 1, records
    return records[0]


# ------------------------------------------------------------------ pure status mapping

def test_status_mappings():
    assert job_recording.import_status(True, False, 3, 0) == jh.STATUS_COMPLETED
    assert job_recording.import_status(False, False, 2, 1) == jh.STATUS_PARTIAL
    assert job_recording.import_status(False, False, 0, 2) == jh.STATUS_FAILED
    assert job_recording.import_status(False, True, 2, 1) == jh.STATUS_CANCELLED
    assert job_recording.promotion_status(True, False, {"status": "promoted"}) == jh.STATUS_COMPLETED
    assert job_recording.promotion_status(False, False, {"status": "cutover_unknown"}) == jh.STATUS_FAILED
    assert job_recording.promotion_status(True, False, {"status": "failed_original_unchanged"}) == jh.STATUS_FAILED
    assert job_recording.migration_status(True, {}) == jh.STATUS_COMPLETED
    assert job_recording.migration_status(False, {"cancelled": True}) == jh.STATUS_CANCELLED
    assert job_recording.migration_status(False, {"error": "x"}) == jh.STATUS_FAILED
    assert job_recording.migration_job_kind("inspect") is None and job_recording.migration_job_kind("plan") is None


# ------------------------------------------------------------------ export dialog

def idle_worker():
    """A MagicMock worker reports isRunning() truthy, which makes closeEvent wait on a modal box."""
    worker = MagicMock()
    worker.isRunning.return_value = False
    return worker


def make_export(monkeypatch=None):
    connector = MagicMock()
    connector.get_schemas.return_value = ["app", "other"]
    connector.get_tables.return_value = ["a", "b", "c"]
    dialog = RustDumpExportDialog(connector=connector, job_context={"profile_id": "prof-1", "profile_name": "Prod"})
    dialog._test_app = _app
    return dialog


def start_export(dialog, schema="app"):
    dialog._reset_export_state(schema)
    worker = idle_worker()
    dialog._start_export_worker(worker)
    return worker


def test_export_run_is_recorded_with_profile_target_and_actual_mode(history):
    dialog = make_export()
    try:
        dialog.combo_compression.setCurrentText("none")
        dialog.spin_threads.setValue(3)
        start_export(dialog)
        running = only(history)
        assert running.status == jh.STATUS_RUNNING and running.kind == jh.KIND_EXPORT_FULL
        assert (running.profile_id, running.profile_name, running.target) == ("prof-1", "Prod", "app")
        assert "일관 스냅샷(병렬)" in running.mode and "스레드 3" in running.mode and "압축 안 함" in running.mode
        assert "snapshot=" not in running.mode and running.rerun["snapshot_mode"] == "parallel_strict"
        dialog.export_total_tables = 3
        dialog.export_table_done = {"a": 10, "b": 5}
        dialog.input_output_dir.setText("C:/exports/app_1")
        dialog.on_finished(True, "done")
        done = only(history)
        assert done.status == jh.STATUS_COMPLETED and done.finished_at
        assert done.report_path.replace("\\", "/") == "C:/exports/app_1/_tunnelforge_dump.json"
        assert done.details == {"tables": 3, "rows": 15}
        assert done.rerun["schema"] == "app" and done.rerun["scope"] == "full" and done.rerun["compression"] == "none"
    finally:
        dialog.close()


@pytest.mark.parametrize("cancel, incomplete, success, expected", [
    (False, False, False, jh.STATUS_FAILED),
    (True, False, False, jh.STATUS_CANCELLED),
    (False, True, True, jh.STATUS_PARTIAL),
])
def test_export_failure_cancel_and_incomplete_choice_statuses(history, cancel, incomplete, success, expected):
    dialog = make_export()
    try:
        start_export(dialog)
        dialog._cancel_requested = cancel
        dialog._allow_incomplete = incomplete
        dialog.on_finished(success, "mysql://admin:hunter2@db/app: boom password=hunter2")
        record = only(history)
        assert record.status == expected
        if not success:
            assert "hunter2" not in record.error_summary
        if incomplete:
            assert "불완전 Export" in record.mode or record.rerun is not None
    finally:
        dialog.close()


def test_export_retries_stay_one_job_and_a_new_run_starts_a_new_one(history):
    dialog = make_export()
    try:
        start_export(dialog)
        dialog._start_export_worker(idle_worker())  # privilege / incomplete retry reuses the worker slot
        assert len(history.list()) == 1
        dialog.on_finished(False, "failed")
        start_export(dialog)
        assert len(history.list()) == 2
        dialog.on_finished(True, "ok")
    finally:
        dialog.close()


def test_partial_export_scope_records_the_selected_tables_for_rerun(history):
    dialog = make_export()
    try:
        dialog.combo_schema.setCurrentIndex(0)
        dialog.radio_partial.setChecked(True)
        dialog.list_tables.item(1).setCheckState(Qt.CheckState.Unchecked)
        start_export(dialog)
        record = only(history)
        assert record.kind == jh.KIND_EXPORT_TABLES and record.rerun["tables"] == ["a", "c"]
        assert "테이블 2개" in record.mode
        dialog.on_finished(True, "ok")
    finally:
        dialog.close()


def test_export_dialog_prefills_from_a_rerun_record_without_starting(history):
    dialog = make_export()
    try:
        dialog.apply_job_rerun({"schema": "other", "scope": "tables", "tables": ["b"], "compression": "none",
                                "threads": 2, "include_fk_parents": False})
        assert dialog.combo_schema.currentText() == "other" and dialog.radio_partial.isChecked()
        assert dialog.get_selected_tables() == ["b"]
        assert dialog.combo_compression.currentText() == "none" and dialog.spin_threads.value() == 2
        assert dialog.chk_include_fk.isChecked() is False
        assert history.list() == [], "prefilling must not start or record anything"
        dialog.apply_job_rerun({"schema": "missing", "scope": "full"})
        assert dialog.radio_full.isChecked() and dialog.combo_schema.currentText() == "other"
        dialog.apply_job_rerun(None)
    finally:
        dialog.close()


def test_recording_failure_never_blocks_the_export(monkeypatch):
    monkeypatch.setattr(jh, "make_history", lambda: (_ for _ in ()).throw(OSError("disk")))
    dialog = make_export()
    try:
        start_export(dialog)
        assert dialog._job_id is None
        dialog.on_finished(True, "ok")  # must not raise
        assert dialog.export_success is True
    finally:
        dialog.close()


# ------------------------------------------------------------------ import / promotion

def make_import():
    dialog = RustDumpImportDialog(tunnel_config={"id": "prof-1", "name": "Prod"})
    dialog._test_app = _app
    return dialog


def test_import_run_is_recorded_as_partial_with_table_counts(history):
    dialog = make_import()
    try:
        dialog._job_id = job_recording.begin_import_job(dialog, "C:/dumps/app", "app_restore", "safe", 4)
        running = only(history)
        assert running.kind == jh.KIND_IMPORT and running.status == jh.STATUS_RUNNING
        assert running.mode == "safe · 스레드 4" and running.target == "app_restore" and running.profile_name == "Prod"
        dialog.import_results = {"a": {"status": "done"}, "b": {"status": "error"}, "c": {"status": "blocked"}}
        dialog.last_import_mode = "replace"
        dialog.on_finished(False, "table b failed: duplicate key")
        done = only(history)
        assert done.status == jh.STATUS_PARTIAL and "duplicate key" in done.error_summary
        assert done.details["tables_done"] == 1 and done.details["tables_failed"] == 1 and done.details["tables_not_run"] == 1
        assert done.report_path.replace("\\", "/") == "C:/dumps/app/_tunnelforge_import_report.json"
    finally:
        dialog.close()


def test_import_completed_and_cancelled(history):
    for cancelled, expected in ((False, jh.STATUS_COMPLETED), (True, jh.STATUS_CANCELLED)):
        dialog = make_import()
        try:
            dialog._job_id = job_recording.begin_import_job(dialog, "C:/d", "app", "replace", 1)
            dialog.import_results = {"a": {"status": "done"}}
            dialog.last_import_mode = "replace"
            dialog._cancel_requested = cancelled
            dialog.on_finished(not cancelled, "x")
            assert history.list()[0].status == expected
        finally:
            dialog.close()


def test_promotion_is_a_separate_job_and_unknown_outcome_is_not_called_success(history):
    dialog = make_import()
    try:
        dialog._promotion_action = "confirm"
        dialog._promotion_job_id = job_recording.begin_promotion_job(
            dialog, {"action": "confirm", "target": {"engine": "mysql", "schema": "app", "password": "p", "user": "u"}})
        assert only(history).kind == jh.KIND_PROMOTE and only(history).target == "app"
        dialog._on_promotion_finished(False, "connection lost", {"status": "cutover_unknown", "report_path": "C:/r/_promotion/report.json"})
        record = only(history)
        assert record.status == jh.STATUS_FAILED
        assert "전환 결과를 알 수 없습니다" in record.error_summary and record.details["outcome"] == "cutover_unknown"
        assert record.report_path.replace("\\", "/") == "C:/r/_promotion/report.json"
        assert "password" not in history.path.read_text(encoding="utf-8").lower()
    finally:
        dialog.close()


def test_promotion_success_is_completed_and_plan_is_not_recorded(history):
    dialog = make_import()
    try:
        dialog._promotion_action = "plan"
        dialog._review_promotion_plan = MagicMock()  # opens a modal review dialog
        dialog._on_promotion_finished(True, "", {"status": "planned"})
        assert history.list() == []
        dialog._promotion_action = "confirm"
        dialog._promotion_job_id = job_recording.begin_promotion_job(dialog, {"target": {"database": "app"}})
        dialog._on_promotion_finished(True, "", {"status": "promoted", "backup_namespace": "tf_backup_1"})
        record = only(history)
        assert record.status == jh.STATUS_COMPLETED and record.details["backup_namespace"] == "tf_backup_1"
    finally:
        dialog.close()


# ------------------------------------------------------------------ migration

def test_migration_commands_record_names_only_never_credentials(history):
    payload = {"source_engine": "mysql", "target_engine": "postgresql",
               "source": {"engine": "mysql", "host": "10.0.0.1", "user": "root", "password": "hunter2", "database": "src_db"},
               "target": {"engine": "postgresql", "host": "10.0.0.2", "user": "pg", "password": "hunter3", "database": "tgt", "schema": "public"},
               "execution_options": {"mode": "create_only"}}
    assert job_recording.begin_migration_job("inspect", payload) is None
    assert job_recording.begin_migration_job("plan", payload) is None
    job_id = job_recording.begin_migration_job("migrate", payload)
    record = only(history)
    assert record.kind == jh.KIND_MIGRATION_RUN and record.target == "src_db (mysql) -> public (postgresql)"
    assert record.mode == "migrate · create_only"
    job_recording.finish_migration_job(job_id, False, {"error": "target refused: password=hunter3"})
    done = only(history)
    assert done.status == jh.STATUS_FAILED and "hunter3" not in done.error_summary
    raw = history.path.read_text(encoding="utf-8")
    for secret in ("hunter2", "hunter3", "10.0.0.1", "10.0.0.2", "root"):
        assert secret not in raw


def test_migration_dialog_calls_the_recording_hooks():
    source = (ROOT / "src/ui/dialogs/cross_engine_migration_dialog.py").read_text(encoding="utf-8")
    assert "self._job_id = begin_migration_job(command, payload)" in source
    assert "finish_migration_job(self._job_id, success, payload)" in source


# ------------------------------------------------------------------ job list dialog

def seed(history):
    now = datetime.now(timezone.utc)
    ids = {}
    ids["old_fail"] = history.begin(jh.KIND_IMPORT, profile_name="Prod", target="orders", mode="replace", now=now - timedelta(hours=3))
    history.finish(ids["old_fail"], jh.STATUS_FAILED, error="duplicate key on orders", report_path="C:/missing/report.json",
                   now=now - timedelta(hours=3) + timedelta(seconds=30))
    ids["export"] = history.begin(jh.KIND_EXPORT_FULL, profile_name="Dev", target="app", mode="full",
                                  rerun={"schema": "app", "scope": "full"}, now=now - timedelta(hours=2))
    history.finish(ids["export"], jh.STATUS_COMPLETED, report_path=__file__, now=now - timedelta(hours=2) + timedelta(minutes=5))
    ids["running"] = history.begin(jh.KIND_MIGRATION_RUN, target="a -> b", now=now - timedelta(minutes=5))
    return ids


def rows(dialog):
    return [[dialog.table.item(r, c).text() for c in range(dialog.table.columnCount())] for r in range(dialog.table.rowCount())]


def select(dialog, row):
    dialog.table.selectRow(row)
    return dialog.selected_record()


def test_dialog_lists_newest_first_with_kind_status_and_duration_labels(history):
    seed(history)
    dialog = JobListDialog(history)
    try:
        data = rows(dialog)
        assert [r[1] for r in data] == ["이관 실행", "Export (전체)", "Import"]
        assert [r[5] for r in data] == ["실행 중", "완료", "실패"]
        assert data[1][6] == "5분 0초" and data[2][6] == "30초" and data[0][6] == "-"
    finally:
        dialog.close()


def test_dialog_filters_by_status_kind_and_text(history):
    seed(history)
    dialog = JobListDialog(history)
    try:
        dialog.status_filter.setCurrentIndex(dialog.status_filter.findData(jh.STATUS_FAILED))
        assert [r[2] for r in rows(dialog)] == ["orders"]
        dialog.status_filter.setCurrentIndex(0)
        dialog.kind_filter.setCurrentIndex(dialog.kind_filter.findData(jh.KIND_EXPORT_FULL))
        assert [r[2] for r in rows(dialog)] == ["app"]
        dialog.kind_filter.setCurrentIndex(0)
        dialog.search.setText("DUPLICATE")
        assert [r[2] for r in rows(dialog)] == ["orders"]
        dialog.search.setText("nothing matches")
        assert rows(dialog) == []
    finally:
        dialog.close()


def test_dialog_sorts_by_column_and_keeps_the_selection_mapped_to_the_right_record(history):
    ids = seed(history)
    dialog = JobListDialog(history)
    try:
        dialog.table.sortByColumn(6, Qt.SortOrder.AscendingOrder)  # duration: running(-1) first
        assert rows(dialog)[0][1] == "이관 실행"
        dialog.table.sortByColumn(2, Qt.SortOrder.AscendingOrder)  # target text
        assert [r[2] for r in rows(dialog)] == ["a -> b", "app", "orders"]
        record = select(dialog, 2)
        assert record.id == ids["old_fail"]
    finally:
        dialog.close()


def test_dialog_failure_reason_report_and_buttons_follow_the_selection(history, monkeypatch):
    ids = seed(history)
    opened = []
    shown = []
    dialog = JobListDialog(history)
    monkeypatch.setattr(dialog, "_open_path", lambda path: opened.append(path))
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text, *a: shown.append((title, text)))
    try:
        record = select(dialog, 2)
        assert record.id == ids["old_fail"]
        assert dialog.btn_error.isEnabled() and "duplicate key on orders" in dialog.detail.toPlainText()
        dialog.show_error()
        assert shown[-1] == ("실패 원인", "duplicate key on orders")
        assert dialog.open_report() is False, "missing report file and missing folder"
        assert shown[-1][0] == "보고서 열기" and opened == []
        select(dialog, 1)
        assert dialog.open_report() is True and opened == [__file__]
        select(dialog, 0)
        assert dialog.btn_delete.isEnabled() is False and dialog.btn_report.isEnabled() is False
    finally:
        dialog.close()


def test_report_folder_is_opened_when_the_file_was_moved(history, tmp_path, monkeypatch):
    job = history.begin(jh.KIND_EXPORT_FULL, target="app")
    history.finish(job, jh.STATUS_COMPLETED, report_path=str(tmp_path / "gone" / "_tunnelforge_dump.json"))
    (tmp_path / "gone").mkdir()
    opened = []
    dialog = JobListDialog(history)
    monkeypatch.setattr(dialog, "_open_path", lambda path: opened.append(path))
    try:
        select(dialog, 0)
        assert dialog.open_report() is True and opened == [str(tmp_path / "gone")]
    finally:
        dialog.close()


def test_dialog_delete_requires_confirmation_and_never_removes_running_jobs(history, monkeypatch):
    ids = seed(history)
    dialog = JobListDialog(history)
    try:
        select(dialog, 2)
        monkeypatch.setattr(dialog, "_confirm", lambda *a: False)
        assert dialog.delete_selected() is False and len(history.list()) == 3
        monkeypatch.setattr(dialog, "_confirm", lambda *a: True)
        assert dialog.delete_selected() is True
        assert {r.id for r in history.list()} == {ids["export"], ids["running"]}
        assert dialog.clear_all() is True
        assert [r.id for r in history.list()] == [ids["running"]], "running jobs survive 'delete all'"
        select(dialog, 0)
        assert dialog.delete_selected() is False
    finally:
        dialog.close()


def test_reopen_labels_and_callback_follow_the_policy(history):
    ids = seed(history)
    reopened = []
    dialog = JobListDialog(history, reopen=reopened.append)
    try:
        select(dialog, 1)  # export: settings are pre-filled, validated again on run
        assert "같은 설정으로" in dialog.btn_reopen.text()
        dialog.reopen_selected()
        select(dialog, 2)  # import: only the original dialog, no settings
        assert dialog.btn_reopen.text() == "원래 대화상자 열기"
        dialog.reopen_selected()
        assert [r.id for r in reopened] == [ids["export"], ids["old_fail"]]
        assert JobListDialog(history).btn_reopen.isHidden()
    finally:
        dialog.close()


def test_only_export_kinds_reopen_with_settings():
    assert job_list_dialog.REOPEN_WITH_SETTINGS == (jh.KIND_EXPORT_FULL, jh.KIND_EXPORT_TABLES)


# ------------------------------------------------------------------ main window / wizard wiring

def make_window(tunnels):
    from src.ui.main_window import TunnelManagerUI

    launcher = MagicMock()
    window = SimpleNamespace(
        _wizard_launcher=launcher,
        config_mgr=MagicMock(load_config=lambda: {"tunnels": tunnels}),
    )
    return window, launcher, TunnelManagerUI.reopen_job_dialog


def test_reopen_routes_export_with_settings_and_everything_else_without():
    tunnels = [{"id": "prof-1", "name": "Prod"}]
    window, launcher, reopen = make_window(tunnels)
    export = jh.JobRecord(id="1", kind=jh.KIND_EXPORT_FULL, profile_id="prof-1", rerun={"schema": "app"})
    reopen(window, export)
    launcher.open_rust_dump_export.assert_called_once_with(tunnels[0], {"schema": "app"})
    for kind in (jh.KIND_IMPORT, jh.KIND_PROMOTE):
        launcher.reset_mock()
        reopen(window, jh.JobRecord(id="2", kind=kind, profile_id="prof-1", rerun={"schema": "x"}))
        launcher.open_rust_dump_import.assert_called_once_with(tunnels[0])
        launcher.open_rust_dump_export.assert_not_called()
    for kind in (jh.KIND_MIGRATION_PREFLIGHT, jh.KIND_MIGRATION_RUN, jh.KIND_MIGRATION_RESUME):
        launcher.reset_mock()
        reopen(window, jh.JobRecord(id="3", kind=kind))
        launcher.open_cross_engine_migration.assert_called_once_with()


def test_reopen_for_a_deleted_profile_falls_back_to_the_generic_wizard():
    window, launcher, reopen = make_window([])
    reopen(window, jh.JobRecord(id="1", kind=jh.KIND_EXPORT_TABLES, profile_id="gone", rerun={"scope": "tables"}))
    launcher.open_rust_dump_export.assert_called_once_with(None, {"scope": "tables"})


def test_wizard_applies_the_rerun_to_the_export_dialog(monkeypatch):
    from src.ui.dialogs import db_dialogs

    dialog = MagicMock()
    created = {}

    def fake_dialog(parent, **kwargs):
        created.update(kwargs)
        return dialog

    monkeypatch.setattr(db_dialogs, "RustDumpExportDialog", fake_dialog)
    wizard = db_dialogs.RustDumpWizard(preselected_tunnel={"id": "prof-1", "name": "Prod"})
    monkeypatch.setattr(wizard, "_resolve_connector", lambda need_connection_info=False: (MagicMock(), "Prod_user"))
    assert wizard.start_export(rerun={"schema": "app"}) is True
    dialog.apply_job_rerun.assert_called_once_with({"schema": "app"})
    assert created["job_context"] == {"profile_id": "prof-1", "profile_name": "Prod"}
    dialog.reset_mock()
    wizard.start_export()
    dialog.apply_job_rerun.assert_not_called()


def test_launcher_passes_tunnel_and_rerun_to_the_wizard(monkeypatch):
    from src.ui.controllers import wizard_launcher

    wizard = MagicMock()
    seen = {}
    monkeypatch.setattr(wizard_launcher, "RustDumpWizard", lambda **kwargs: seen.update(kwargs) or wizard)
    launcher = wizard_launcher.WizardLauncher(SimpleNamespace(engine=None, config_mgr=None))
    launcher.open_rust_dump_export({"id": "t"}, {"schema": "app"})
    assert seen["preselected_tunnel"] == {"id": "t"}
    wizard.start_export.assert_called_once_with(rerun={"schema": "app"})
    launcher.open_rust_dump_import({"id": "t"})
    wizard.start_import.assert_called_once_with()
    launcher.open_rust_dump_export()
    wizard.start_export.assert_called_with()


def test_main_window_has_the_job_list_button_and_startup_sweep():
    tree = ast.parse((ROOT / "src/ui/main_window.py").read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert {"btn_job_list", "open_job_list_dialog"} <= names
    source = (ROOT / "src/ui/main_window.py").read_text(encoding="utf-8")
    assert "sweep_interrupted()" in source


def test_job_history_is_isolated_from_the_real_application_directory(tmp_path):
    real = jh.job_history_file()
    jh.job_begin(jh.KIND_IMPORT, target="isolation-check")
    assert not real.exists() or "isolation-check" not in real.read_text(encoding="utf-8")


def test_overwrite_import_is_recorded_as_overwrite_with_the_dialog_label(history, monkeypatch, tmp_path):
    started = {}

    def fake_worker(task_type, config, **kwargs):
        started.update(kwargs)
        return idle_worker()

    monkeypatch.setattr("src.ui.dialogs.db_import_dialog.RustDumpWorker", fake_worker)
    monkeypatch.setattr(RustDumpImportDialog, "_confirm_production_guard", lambda *a: True)
    connector = MagicMock(host="127.0.0.1", port=3306, user="root", password="pw", engine="mysql")
    connector.get_schemas.return_value = ["app"]
    dialog = RustDumpImportDialog(connector=connector, tunnel_config={"id": "prof-1", "name": "Prod"})
    try:
        dialog.input_dir.setText(str(tmp_path))
        dialog.chk_use_original.setChecked(False)
        dialog.combo_target_schema.setEditable(True)
        dialog.combo_target_schema.setCurrentText("app")
        dialog.radio_overwrite.setChecked(True)
        dialog.do_import()
        assert started["import_mode"] == "safe"  # the core still runs a safe restore first
        assert only(history).mode.startswith(dialog._get_import_mode_text("overwrite"))
    finally:
        dialog._job_id = None
        dialog.close()


# ------------------------------------------------------------------ backup management entry point (sprint 2)

def test_backup_management_button_is_offered_for_import_records_with_a_report(history):
    seed(history)
    opened = []
    dialog = JobListDialog(history, manage_backups=opened.append)
    try:
        assert not dialog.btn_backups.isHidden()
        assert select(dialog, 1).kind == jh.KIND_EXPORT_FULL
        assert dialog.btn_backups.isEnabled() is False
        import_job = history.begin(jh.KIND_IMPORT, target="app", report_path="C:/dumps/app/_tunnelforge_import_report.json")
        dialog.reload()
        row = next(r for r in range(dialog.table.rowCount())
                   if dialog.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == import_job)
        select(dialog, row)
        assert dialog.btn_backups.isEnabled() is True
        dialog.manage_backups_selected()
        assert [r.id for r in opened] == [import_job]
    finally:
        dialog.close()
    plain = JobListDialog(history)
    try:
        assert plain.btn_backups.isHidden(), "no entry point without a handler"
    finally:
        plain.close()


def test_main_window_opens_backup_management_from_the_report_and_profile_credentials(tmp_path, monkeypatch):
    from src.ui.dialogs import backup_lifecycle_dialog
    from src.ui.main_window import TunnelManagerUI

    report = tmp_path / "_tunnelforge_import_report.json"
    report.write_text(json.dumps({"original_target": {"engine": "mysql", "host": "127.0.0.1", "port": 13306,
                                                      "database": "app", "password": "never-in-report"}}),
                      encoding="utf-8")
    captured = {}

    class FakeDialog:
        def __init__(self, endpoint, input_dirs, parent=None):
            captured.update(endpoint=endpoint, input_dirs=input_dirs)

        def refresh(self):
            captured["refreshed"] = True

        def exec(self):
            pass

    monkeypatch.setattr(backup_lifecycle_dialog, "BackupLifecycleDialog", FakeDialog)
    ensured = []
    window = SimpleNamespace(
        config_mgr=MagicMock(load_config=lambda: {"tunnels": [{"id": "prof-1", "name": "Prod"}]},
                             get_tunnel_credentials=lambda tid: ("u", "p")),
        _ensure_tunnel_running=lambda tunnel, prompt: ensured.append(tunnel["id"]) or True,
    )
    record = jh.JobRecord(id="1", kind=jh.KIND_IMPORT, profile_id="prof-1", report_path=str(report))
    TunnelManagerUI.open_job_backups(window, record)
    assert ensured == ["prof-1"] and captured["refreshed"] is True
    assert captured["input_dirs"] == [str(tmp_path)]
    endpoint = captured["endpoint"]
    assert (endpoint["host"], endpoint["port"], endpoint["database"]) == ("127.0.0.1", 13306, "app")
    assert (endpoint["user"], endpoint["password"]) == ("u", "p")

    captured.clear()
    TunnelManagerUI.open_job_backups(window, jh.JobRecord(id="2", kind=jh.KIND_IMPORT, profile_id="prof-1",
                                                          report_path=str(tmp_path / "missing.json")))
    assert captured == {}, "no safe-restore report -> no dialog"
