"""Schedule dialogs expose scheduled backups only (no scheduled SQL), with catch-up and interval rules."""
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.scheduler import BackupScheduler, ScheduleConfig
from src.ui.dialogs.schedule_dialog import ScheduleEditDialog, ScheduleListDialog

_app = QApplication.instance() or QApplication([])


@pytest.fixture
def warnings(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a: shown.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text, *a: shown.append(text))
    return shown


def filled_dialog(**kwargs):
    dialog = ScheduleEditDialog(None, [("t1", "Prod")], **kwargs)
    dialog.name_edit.setText("nightly")
    dialog.schema_edit.setText("app")
    dialog.output_edit.setText("C:/backups")
    dialog.cron_edit.setText("0 3 * * *")
    dialog.schedule_tabs.setCurrentIndex(1)
    return dialog


def test_task_type_choice_is_hidden_and_the_unattended_limits_are_explained():
    dialog = ScheduleEditDialog(None, [("t1", "Prod")])
    try:
        assert dialog.task_type_box.isHidden()
        assert "호스트 키" in dialog.unattended_note.text() and "개인키" in dialog.unattended_note.text()
        assert dialog.catch_up_check.isChecked()
    finally:
        dialog.close()


def test_saving_always_creates_a_backup_schedule_with_the_catch_up_choice():
    dialog = filled_dialog()
    try:
        dialog.sql_radio.setChecked(True)  # even a forced radio state cannot create a SQL schedule
        dialog.catch_up_check.setChecked(False)
        dialog._save()
        config = dialog.result_config
        assert config is not None
        assert config.task_type == "backup" and config.catch_up_missed is False and config.output_dir == "C:/backups"
        assert config.sql_query == ""
    finally:
        dialog.close()


def test_too_frequent_schedules_are_refused_with_the_interval_message(warnings):
    dialog = filled_dialog(min_interval_minutes=15)
    try:
        dialog.cron_edit.setText("*/5 * * * *")
        dialog._save()
        assert dialog.result_config is None and "너무 짧습니다" in warnings[-1] and "15분" in warnings[-1]
        dialog.cron_edit.setText("garbage")
        dialog._save()
        assert dialog.result_config is None and "잘못된 cron" in warnings[-1]
    finally:
        dialog.close()


def test_a_legacy_sql_schedule_can_not_be_saved_again(warnings):
    legacy = ScheduleConfig(id="q1", name="old sql", tunnel_id="t1", schema="app", task_type="sql_query",
                            sql_query="DELETE FROM t", output_dir="", cron_expression="0 3 * * *")
    dialog = ScheduleEditDialog(None, [("t1", "Prod")], schedule=legacy)
    try:
        assert not dialog.unsupported_label.isHidden() and not dialog.save_btn.isEnabled()
        dialog._save()
        assert dialog.result_config is None and "지원되지 않습니다" in warnings[-1]
    finally:
        dialog.close()


def test_loading_a_backup_schedule_restores_the_catch_up_choice():
    schedule = ScheduleConfig(id="b1", name="n", tunnel_id="t1", schema="app", output_dir="C:/b", catch_up_missed=False)
    dialog = ScheduleEditDialog(None, [("t1", "Prod")], schedule=schedule)
    try:
        assert dialog.catch_up_check.isChecked() is False and dialog.save_btn.isEnabled()
    finally:
        dialog.close()


def test_list_dialog_marks_legacy_sql_rows_as_unsupported_and_passes_the_interval(monkeypatch):
    config_manager = MagicMock()
    legacy = ScheduleConfig(id="q1", name="old sql", tunnel_id="t1", schema="app", task_type="sql_query").to_dict()
    config_manager.get_app_setting.side_effect = lambda key, default=None: (
        [legacy] if key == "schedules" else (30 if key == "scheduled_backup_min_interval_minutes" else default))
    scheduler = BackupScheduler(config_manager, MagicMock())
    dialog = ScheduleListDialog(None, scheduler, [("t1", "Prod")])
    try:
        assert dialog.table.item(0, 5).text() == "지원 중단 (실행 안 됨)"
        captured = {}

        class Capture(ScheduleEditDialog):
            def __init__(self, parent=None, tunnel_list=None, schedule=None, min_interval_minutes=15, **kwargs):
                captured["min"] = min_interval_minutes
                super().__init__(parent, tunnel_list, schedule, min_interval_minutes, **kwargs)

            def exec(self):
                return 0

        monkeypatch.setattr("src.ui.dialogs.schedule_dialog.ScheduleEditDialog", Capture)
        dialog._add_schedule()
        assert captured["min"] == 30
    finally:
        dialog.close()


def test_the_schedule_feature_is_exposed_for_backups_only():
    import src.ui.main_window as main_window

    assert main_window.SCHEDULE_FEATURE_ENABLED is True


def test_postgresql_tunnels_pick_a_database_and_legacy_schedules_show_postgres(warnings):
    engines = {"t1": "postgresql", "t2": "mysql"}
    lister = MagicMock(return_value=(["app_db", "postgres"], ""))
    legacy = ScheduleConfig(id="b1", name="n", tunnel_id="t1", schema="app", output_dir="C:/b")
    dialog = ScheduleEditDialog(None, [("t1", "PG"), ("t2", "My")], schedule=legacy,
                                tunnel_engines=engines, database_lister=lister)
    try:
        assert not dialog.database_row.isHidden() and dialog.database_combo.currentText() == "postgres"
        dialog._load_databases()
        assert [dialog.database_combo.itemText(i) for i in range(dialog.database_combo.count())] == ["app_db", "postgres"]
        dialog.database_combo.setCurrentText("app_db")
        dialog.cron_edit.setText("0 3 * * *")
        dialog.schedule_tabs.setCurrentIndex(1)
        dialog._save()
        assert dialog.result_config.database == "app_db"
        dialog.tunnel_combo.setCurrentIndex(1)  # MySQL tunnel: the field is hidden and not saved
        assert dialog.database_row.isHidden()
        dialog._save()
        assert dialog.result_config.database == ""
    finally:
        dialog.close()


def test_rehearsal_targets_are_limited_to_dev_staging_profiles_and_saved_with_the_schedule(warnings):
    tunnels = [("prod", "Prod"), ("dev", "Dev"), ("stg", "Stage"), ("none", "Unset")]
    envs = {"prod": "production", "dev": "development", "stg": "staging", "none": None}
    dialog = filled_dialog_with(tunnels, envs)
    try:
        names = [dialog.rehearsal_tunnel_combo.itemText(i) for i in range(dialog.rehearsal_tunnel_combo.count())]
        assert names == ["Dev", "Stage"], "production and unset-environment profiles are not selectable"
        dialog.rehearsal_check.setChecked(True)
        dialog._save()
        assert dialog.result_config is None and "스키마" in warnings[-1]
        dialog.rehearsal_schema_edit.setText("rehearsal")
        dialog._save()
        config = dialog.result_config
        assert (config.rehearsal_tunnel_id, config.rehearsal_schema, config.rehearsal_database) == ("dev", "rehearsal", "")
        dialog.rehearsal_check.setChecked(False)
        dialog._save()
        assert dialog.result_config.rehearsal_tunnel_id == ""
    finally:
        dialog.close()


def filled_dialog_with(tunnels, envs):
    dialog = ScheduleEditDialog(None, tunnels, tunnel_environments=envs)
    dialog.name_edit.setText("nightly")
    dialog.schema_edit.setText("app")
    dialog.output_edit.setText("C:/backups")
    dialog.cron_edit.setText("0 3 * * *")
    dialog.schedule_tabs.setCurrentIndex(1)
    return dialog


def test_the_rehearsal_option_is_disabled_when_no_profile_qualifies():
    dialog = ScheduleEditDialog(None, [("prod", "Prod")], tunnel_environments={"prod": "production"})
    try:
        assert not dialog.rehearsal_check.isEnabled()
    finally:
        dialog.close()
