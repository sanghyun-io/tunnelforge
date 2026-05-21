import json
import os
import sys
from pathlib import Path
from typing import Any, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from src.core.cross_engine_migration import MigrationIssue
from src.ui.dialogs.cross_engine_migration_dialog import CrossEngineMigrationDialog


app = QApplication.instance() or QApplication(sys.argv)


class FakeTunnelEngine:
    def __init__(self):
        self.tunnel_configs = {
            "source": {
                "id": "source",
                "name": "Source DB",
                "connection_mode": "direct",
                "remote_host": "127.0.0.1",
                "remote_port": 3306,
                "local_port": 3306,
                "db_engine": "mysql",
                "default_schema": "source_db",
            },
            "target": {
                "id": "target",
                "name": "Target DB",
                "connection_mode": "direct",
                "remote_host": "127.0.0.1",
                "remote_port": 5432,
                "local_port": 5432,
                "db_engine": "postgresql",
                "default_schema": "target_db",
            },
        }
        self.started = set()

    def get_active_tunnels(self):
        return []

    def is_running(self, tunnel_id):
        return tunnel_id in self.started

    def start_tunnel(self, config):
        self.started.add(config["id"])
        return True, "연결 성공"

    def get_connection_info(self, tunnel_id):
        config = self.tunnel_configs[tunnel_id]
        return config["remote_host"], int(config["remote_port"])


class FakeConfigManager:
    def __init__(self, tunnel_engine):
        self.tunnel_engine = tunnel_engine

    def load_config(self):
        return {"tunnels": list(self.tunnel_engine.tunnel_configs.values())}

    def get_tunnel_credentials(self, tunnel_id):
        return f"{tunnel_id}_user", f"{tunnel_id}_password"


def make_dialog():
    tunnel_engine = FakeTunnelEngine()
    dialog = CrossEngineMigrationDialog(
        tunnel_engine=tunnel_engine,
        config_manager=FakeConfigManager(tunnel_engine),
    )
    dialog.source_form.combo_tunnel.setCurrentIndex(1)
    dialog.target_form.combo_tunnel.setCurrentIndex(1)
    return dialog


def assert_widget_reachable(widget, dialog):
    current = widget
    while current is not None:
        assert not current.isHidden(), current.objectName() or current.__class__.__name__
        if current is dialog:
            return
        current = current.parentWidget()
    raise AssertionError("widget is not parented under dialog")


def test_dialog_starts_as_guided_wizard_without_full_run_button():
    dialog = make_dialog()
    try:
        assert dialog.windowTitle() == "DB 전환 마법사"
        assert dialog.current_step_id == "connections"
        assert dialog.step_titles == [
            "1. 연결 선택",
            "2. Source 구조 분석",
            "3. 전환 가능 여부 점검",
            "4. 실행 계획 확인",
            "5. 승인 및 전환 실행",
            "6. 검증 및 결과 저장",
        ]
        assert not dialog.btn_full_run.isVisible()
        assert dialog.btn_previous.text() == "이전"
        assert dialog.btn_next.text() == "다음"
        assert dialog.lbl_direction_summary.text() == "MySQL source_db -> PostgreSQL target_db"
        assert dialog.btn_next.objectName() == "WizardNextButton"
        assert "QPushButton:disabled" in dialog.styleSheet()
        assert "background-color: #e4e7ec" in dialog.styleSheet()
    finally:
        dialog.close()


def test_inspect_step_explains_required_action_before_next():
    dialog = make_dialog()
    try:
        dialog._show_step("inspect")

        assert dialog.btn_auto_inspect.text() == "Source 구조 분석 시작"
        assert dialog.btn_auto_inspect.objectName() == "PrimaryActionButton"
        assert "Source 자동 검사" in dialog.lbl_inspect_step_help.text()
        assert "완료되면 다음 단계로 이동할 수 있습니다" in dialog.lbl_inspect_step_help.text()
        assert "Source 구조 분석이 완료되면" in dialog.lbl_next_hint.text()
        assert not dialog.btn_next.isEnabled()
    finally:
        dialog.close()


def test_inspect_step_hides_advanced_schema_actions_until_requested():
    dialog = make_dialog()
    try:
        dialog._show_step("inspect")
        dialog.show()
        app.processEvents()

        assert_widget_reachable(dialog.btn_auto_inspect, dialog)
        assert dialog.btn_auto_inspect.isVisible()
        assert not dialog.btn_inspect.isVisible()
        assert not dialog.btn_load_schema.isVisible()
        assert not dialog.txt_schema.isVisible()

        dialog.chk_show_schema_json.setChecked(True)
        app.processEvents()

        assert dialog.btn_inspect.isVisible()
        assert dialog.btn_load_schema.isVisible()
        assert dialog.txt_schema.isVisible()
    finally:
        dialog.close()


def test_direction_summary_updates_when_engine_or_tunnel_state_changes():
    dialog = make_dialog()
    try:
        source_pg_index = dialog.source_form.combo_engine.findData("postgresql")
        assert source_pg_index >= 0

        dialog.source_form.combo_engine.setCurrentIndex(source_pg_index)

        assert dialog.lbl_direction_summary.text() == "PostgreSQL source_db -> PostgreSQL target_db"

        dialog.source_form.combo_tunnel.setCurrentIndex(0)
        dialog.source_form.combo_tunnel.setCurrentIndex(1)

        assert dialog.lbl_direction_summary.text() == "MySQL source_db -> PostgreSQL target_db"
    finally:
        dialog.close()


def test_dialog_initial_button_states_and_running_toggle():
    dialog = make_dialog()
    try:
        assert not dialog.btn_save_report.isEnabled()
        assert not dialog.btn_cancel.isEnabled()
        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_full_run.isVisible()
        assert not dialog.source_form.combo_engine.isEnabled()
        assert not dialog.target_form.combo_engine.isEnabled()
        assert dialog._payload()["guide_options"]["row_limit"] == 20
        assert dialog._payload()["source"]["schema"] == "source_db"
        assert dialog._payload()["target"]["database"] == "postgres"
        assert dialog._payload()["target"]["schema"] == "target_db"
        assert dialog.target_form.combo_tunnel.count() == 2
        assert "PostgreSQL" in dialog.target_form.combo_tunnel.itemText(1)

        dialog._set_running(True)

        assert not dialog.btn_inspect.isEnabled()
        assert not dialog.btn_preflight.isEnabled()
        assert not dialog.btn_plan.isEnabled()
        assert not dialog.btn_migrate.isEnabled()
        assert dialog.btn_cancel.isEnabled()
        assert not dialog.btn_next.isEnabled()

        dialog._set_running(False)

        assert dialog.btn_inspect.isEnabled()
        assert dialog.btn_preflight.isEnabled()
        assert dialog.btn_plan.isEnabled()
        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_cancel.isEnabled()
        assert dialog.btn_next.isEnabled()
    finally:
        dialog.close()


def test_wizard_navigation_preserves_payload_and_step_controls():
    dialog = make_dialog()
    try:
        assert dialog.current_step_id == "connections"
        assert dialog.btn_previous.isEnabled() is False
        assert dialog.btn_next.isEnabled() is True

        dialog._go_next_step()

        assert dialog.current_step_id == "inspect"
        assert dialog.btn_previous.isEnabled() is True
        assert not dialog.btn_next.isEnabled()
        assert dialog._payload()["guide_options"]["row_limit"] == 20

        dialog._go_previous_step()

        assert dialog.current_step_id == "connections"
        assert dialog.btn_previous.isEnabled() is False
    finally:
        dialog.close()


def test_wizard_next_requires_current_step_completion():
    dialog = make_dialog()
    schema = {"tables": [{"name": "users", "columns": [{"name": "id", "type": "int"}]}]}
    try:
        dialog._show_step("inspect")
        assert not dialog.btn_next.isEnabled()

        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": schema,
        })
        assert dialog.btn_next.isEnabled()

        dialog._show_step("safety")
        assert not dialog.btn_next.isEnabled()

        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": True,
            "issues": [],
        })
        assert dialog.btn_next.isEnabled()

        dialog._show_step("plan")
        assert not dialog.btn_next.isEnabled()

        dialog._on_result({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {"tables": [{"name": "users", "estimated_rows": 1}]},
            "issues": [],
        })
        assert dialog.btn_next.isEnabled()

        dialog._show_step("execute")
        dialog.input_approval_schema.setText("target_db")
        assert dialog.btn_migrate.isEnabled()
        assert dialog.btn_next.isEnabled()
        assert dialog.btn_next.text() == "DB 변경 실행"

        dialog._on_result({
            "event": "result",
            "command": "migrate",
            "success": True,
        })
        assert dialog.btn_next.isEnabled()
        assert dialog.btn_next.text() == "검증 단계로 이동"

        dialog._show_step("verify")
        assert not dialog.btn_next.isEnabled()

        dialog._on_result({
            "event": "result",
            "command": "verify",
            "success": True,
            "mismatches": [],
        })
        assert dialog.btn_next.isEnabled()
    finally:
        dialog.close()


def test_safety_step_shows_activity_bar_and_recent_log_while_preflight_runs():
    dialog = make_dialog()
    try:
        dialog._show_step("safety")
        dialog.show()
        app.processEvents()

        assert not dialog.safety_activity_bar.isVisible()

        dialog._current_command = "preflight"
        dialog._set_running(True)
        app.processEvents()

        assert dialog.safety_activity_bar.isVisible()
        assert dialog.safety_activity_bar.minimum() == 0
        assert dialog.safety_activity_bar.maximum() == 0
        assert "전환 가능 여부 점검 중" in dialog.lbl_safety_activity.text()
        assert "전환 가능 여부 점검을 시작했습니다" in dialog.txt_safety_log.toPlainText()
        assert not dialog.btn_run_safety.isEnabled()
    finally:
        dialog.close()


def test_safety_activity_updates_from_preflight_phase_and_stops_on_finish():
    dialog = make_dialog()
    try:
        dialog._show_step("safety")
        dialog.show()
        app.processEvents()

        dialog._current_command = "preflight"
        dialog._set_running(True)
        dialog._on_phase_changed("preflight", "checking target state")
        before_tick = dialog.lbl_safety_activity.text()

        assert "Target 상태 확인 중" in before_tick
        assert "Target 상태 확인 중" in dialog.txt_safety_log.toPlainText()

        dialog._tick_safety_activity()

        assert dialog.lbl_safety_activity.text() != before_tick

        dialog._on_finished(True, {"command": "preflight", "success": True})
        app.processEvents()

        assert not dialog.safety_activity_bar.isVisible()
        assert "점검 완료" in dialog.lbl_safety_activity.text()
    finally:
        dialog.close()


def test_safety_issue_is_visible_in_recent_log():
    dialog = make_dialog()
    issue = MigrationIssue(
        severity="warning",
        location="target",
        message="existing table check skipped",
    )
    try:
        dialog._show_step("safety")
        dialog._current_command = "preflight"
        dialog._set_running(True)

        dialog._on_issue(issue)

        assert "[warning] target: existing table check skipped" in dialog.txt_safety_log.toPlainText()
        assert "[warning] target: existing table check skipped" in dialog.txt_log.toPlainText()
    finally:
        dialog.close()


def test_wizard_next_does_not_advance_when_step_is_incomplete():
    dialog = make_dialog()
    try:
        dialog._show_step("inspect")

        dialog._go_next_step()

        assert dialog.current_step_id == "inspect"
    finally:
        dialog.close()


def test_payload_uses_strict_verification_by_default():
    dialog = make_dialog()
    try:
        payload = dialog._payload()
        assert payload["verify_options"]["mode"] == "strict"
        assert payload["verify_options"]["mismatch_limit"] == 20
    finally:
        dialog.close()


def test_verify_result_shows_mismatch_examples_before_summary():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "verify",
            "success": False,
            "mismatches": [
                {
                    "table": "users",
                    "key": "id=7",
                    "column": "email",
                    "source_value": "a@example.com",
                    "target_value": "b@example.com",
                    "difference": "value_mismatch",
                }
            ],
            "row_count_differences": [{"table": "orders", "source_rows": 10, "target_rows": 9}],
        })

        text = dialog.txt_verify_result.toPlainText()
        mismatch_index = text.index("테이블: users")
        assert "Key: id=7" in text
        assert "Column: email" in text
        assert "Source: a@example.com" in text
        assert "Target: b@example.com" in text
        summary_index = text.index("orders: Source 10 rows / Target 9 rows")
        assert mismatch_index < summary_index
    finally:
        dialog.close()


def test_verify_failure_with_malformed_differences_shows_unknown_format_message():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "verify",
            "success": False,
            "mismatches": ["bad-entry", None],
            "row_count_differences": ["bad-entry", None],
        })

        text = dialog.txt_verify_result.toPlainText()
        assert "검증 통과" not in text
        assert "검증 실패: Rust Core가 비교 차이 상세를 반환하지 않았습니다." in text
    finally:
        dialog.close()


def test_verify_finished_failure_marks_stale_result_and_disables_save_report():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "verify",
            "success": True,
            "mismatches": [],
            "row_count_differences": [],
        })
        assert dialog.btn_save_report.isEnabled()

        dialog._current_command = "verify"
        dialog._verify_result_received = False
        dialog._on_finished(False, {})

        text = dialog.txt_verify_result.toPlainText()
        assert "검증 실패: 새 검증 결과를 받지 못했습니다." in text
        assert dialog.last_result is None
        assert not dialog.btn_save_report.isEnabled()
    finally:
        dialog.close()


def test_verify_finished_failure_preserves_failed_result_with_mismatches():
    dialog = make_dialog()
    payload = {
        "event": "result",
        "command": "verify",
        "success": False,
        "mismatches": [
            {
                "table": "users",
                "key": "id=7",
                "column": "email",
                "source_value": "a@example.com",
                "target_value": "b@example.com",
                "difference": "value_mismatch",
            }
        ],
        "row_count_differences": [],
    }
    try:
        dialog._on_result(payload)

        dialog._current_command = "verify"
        dialog._on_finished(False, payload)

        text = dialog.txt_verify_result.toPlainText()
        assert "테이블: users" in text
        assert "Key: id=7" in text
        assert "새 검증 결과를 받지 못했습니다" not in text
        assert dialog.btn_save_report.isEnabled()
        assert dialog.last_result is payload
    finally:
        dialog.close()


def test_verify_step_has_single_visible_verify_trigger_and_save_report_reachable():
    dialog = make_dialog()
    try:
        dialog._show_step("verify")
        dialog.show()
        app.processEvents()

        visible_verify_buttons = [
            button
            for button in dialog.step_pages["verify"].findChildren(QPushButton)
            if button.text() == "검증" and button.isVisible()
        ]

        assert visible_verify_buttons == [dialog.btn_run_verify]
        assert dialog.btn_run_verify is dialog.btn_verify
        assert dialog.btn_save_report.isVisible()
        assert_widget_reachable(dialog.btn_save_report, dialog)
    finally:
        dialog.close()


def test_step_pages_keep_current_step_actions_reachable():
    dialog = make_dialog()
    step_actions = {
        "inspect": [dialog.btn_auto_inspect],
        "safety": [dialog.btn_run_safety],
        "plan": [dialog.btn_guide, dialog.btn_plan],
        "execute": [dialog.btn_resume],
        "verify": [dialog.btn_verify, dialog.btn_save_report],
    }
    try:
        dialog.show()
        app.processEvents()
        assert set(dialog.step_page_layouts) == set(dialog.step_ids)
        assert all(isinstance(layout, QVBoxLayout) for layout in dialog.step_page_layouts.values())

        for step_id, buttons in step_actions.items():
            dialog._show_step(step_id)
            app.processEvents()
            assert isinstance(dialog.step_pages[step_id], QWidget)
            for button in buttons:
                assert_widget_reachable(button, dialog)
            if step_id == "inspect":
                dialog.chk_show_schema_json.setChecked(True)
                app.processEvents()
                assert_widget_reachable(dialog.btn_load_schema, dialog)
                assert_widget_reachable(dialog.btn_inspect, dialog)
    finally:
        dialog.close()


def test_plan_step_has_single_visible_plan_trigger():
    dialog = make_dialog()
    try:
        dialog._show_step("plan")
        dialog.show()
        app.processEvents()

        visible_plan_buttons = [
            button
            for button in dialog.step_pages["plan"].findChildren(QPushButton)
            if button.text() == "계획 생성" and button.isVisible()
        ]

        assert visible_plan_buttons == [dialog.btn_run_plan]
        assert dialog.btn_plan is dialog.btn_run_plan
    finally:
        dialog.close()


def test_inspect_result_enables_report_and_updates_schema():
    dialog = make_dialog()
    schema = {
        "tables": [
            {
                "name": "users",
                "columns": [
                    {
                        "name": "id",
                        "type": "int",
                        "nullable": False,
                        "primary_key": True,
                    }
                ],
            }
        ]
    }
    try:
        dialog._show_step("inspect")
        dialog.show()
        app.processEvents()

        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": schema,
            "unsupported_objects": ["view:active_users"],
        })

        assert dialog.btn_save_report.isEnabled()
        assert json.loads(dialog.txt_schema.toPlainText()) == schema
        assert "테이블 1개" in dialog.lbl_source_summary.text()
        assert not dialog.btn_auto_inspect.isVisible()
        assert dialog._payload()["unsupported_objects"] == ["view:active_users"]
        assert "스키마 검사 결과를 입력에 반영했습니다." in dialog.txt_log.toPlainText()
    finally:
        dialog.close()


def test_inspect_result_shows_readable_source_summary_and_hides_json_by_default():
    dialog = make_dialog()
    schema = {
        "tables": [
            {
                "name": "users",
                "columns": [
                    {"name": "id", "type": "int", "nullable": False, "primary_key": True},
                    {"name": "email", "type": "varchar(255)", "nullable": False},
                ],
                "indexes": [{"name": "idx_users_email"}],
                "foreign_keys": [],
            },
            {
                "name": "orders",
                "columns": [{"name": "user_id", "type": "int", "foreign_key": True}],
                "indexes": [],
                "foreign_keys": [{"name": "fk_orders_users"}],
            },
        ]
    }
    try:
        dialog._show_step("inspect")
        dialog.show()
        app.processEvents()
        assert not dialog.txt_schema.isVisible()

        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": schema,
            "unsupported_objects": ["view:active_users"],
        })

        summary = dialog.lbl_source_summary.text()
        assert "테이블 2개" in summary
        assert "컬럼 3개" in summary
        assert "인덱스 1개" in summary
        assert "FK 1개" in summary
        assert "지원 제외 1개" in summary
        assert json.loads(dialog.txt_schema.toPlainText()) == schema

        dialog.chk_show_schema_json.setChecked(True)
        app.processEvents()

        assert dialog.txt_schema.isVisible()
    finally:
        dialog.close()


def test_source_summary_counts_only_valid_tables_and_handles_partial_schema():
    dialog = make_dialog()
    schema = {
        "tables": [
            {"name": "users", "columns": [{"name": "id"}], "indexes": "bad", "foreign_keys": []},
            "not-a-table",
            {"name": "orders", "columns": None, "indexes": [{"name": "idx_orders"}]},
            None,
            {"name": "logs", "foreign_keys": [{"name": "fk_logs_users"}]},
        ]
    }
    try:
        summary = dialog._schema_summary_text(schema, ["view:active_users"])

        assert "테이블 3개" in summary
        assert "컬럼 1개" in summary
        assert "인덱스 1개" in summary
        assert "FK 1개" in summary
        assert "지원 제외 1개" in summary
    finally:
        dialog.close()


def test_inspect_result_clears_stale_unsupported_objects_when_next_inspect_omits_them():
    dialog = make_dialog()
    first_schema = {"tables": [{"name": "users", "columns": [{"name": "id"}]}]}
    second_schema = {"tables": [{"name": "orders", "columns": [{"name": "id"}]}]}
    try:
        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": first_schema,
            "unsupported_objects": ["view:active_users"],
        })
        assert dialog._payload()["unsupported_objects"] == ["view:active_users"]

        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": second_schema,
        })

        assert "지원 제외 0개" in dialog.lbl_source_summary.text()
        assert "unsupported_objects" not in dialog._payload()
    finally:
        dialog.close()


def test_empty_schema_runs_inspect_before_requested_plan(monkeypatch):
    dialog = make_dialog()
    started = []
    schema = {
        "tables": [{"name": "users", "columns": [{"name": "id", "type": "int"}]}]
    }

    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.QTimer.singleShot",
        lambda _msec, callback: callback(),
    )
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda command, payload, workflow=False: started.append((command, payload, workflow)),
    )

    try:
        dialog._start_command("plan")
        assert started[0][0] == "inspect"
        assert dialog._pending_after_inspect == "plan"

        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": schema,
        })

        assert started[1][0] == "plan"
        assert started[1][1]["schema"] == schema
        assert "Rust Core 검사 완료" in dialog.lbl_schema_status.text()
    finally:
        dialog.close()


def test_migrate_result_saves_resume_state(monkeypatch, tmp_path):
    saved = {}

    def fake_save_resume_state(key, state):
        saved["key"] = key
        saved["state"] = state
        return tmp_path / "resume.json"

    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.save_resume_state",
        fake_save_resume_state,
    )

    dialog = make_dialog()
    state = {"tables": [{"table": "users", "completed": False, "rows_copied": 5000}]}
    try:
        dialog._on_result({
            "event": "result",
            "command": "migrate",
            "success": False,
            "state": state,
        })

        assert saved["state"] == state
        assert saved["key"]
        assert "재개 상태 저장" in dialog.txt_log.toPlainText()
    finally:
        dialog.close()


def test_migrate_failure_shows_human_readable_issue_and_cleanup_action():
    dialog = make_dialog()
    try:
        dialog._show_step("execute")
        dialog.show()
        app.processEvents()
        dialog._on_result({
            "event": "result",
            "command": "migrate",
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "location": "localized_strings",
                    "message": "postgresql insert error: duplicate key",
                    "suggestion": "Clean the failed target tables and retry.",
                    "blocking": True,
                }
            ],
            "state": {"tables": [{"table": "localized_strings", "completed": False}]},
        })

        assert "DB 변경 실패" in dialog.lbl_execution_phase.text()
        assert "localized_strings" in dialog.lbl_current_table.text()
        assert "duplicate key" in dialog.txt_log.toPlainText()
        assert dialog.btn_cleanup_failed.isVisible()
    finally:
        dialog.close()


def test_migrate_failure_summarizes_database_error_details():
    dialog = make_dialog()
    try:
        dialog._show_step("execute")
        dialog.show()
        app.processEvents()
        dialog._on_result({
            "event": "result",
            "command": "migrate",
            "success": False,
            "message": "postgresql copy finish error: db error",
            "table": "log_entry",
            "code": "22021",
            "detail": "invalid byte sequence for encoding UTF8: 0x00",
            "context": "COPY log_entry, line 34055",
            "state": {"tables": [{"table": "log_entry", "completed": False}]},
        })

        summary = dialog.lbl_migration_result.text()
        assert "실패 위치: log_entry" in summary
        assert "원인: postgresql copy finish error: db error" in summary
        assert "PostgreSQL 오류 코드: 22021" in summary
        assert "상세: invalid byte sequence for encoding UTF8: 0x00" in summary
        assert "위치: COPY log_entry, line 34055" in summary
    finally:
        dialog.close()


def test_command_start_resets_stale_execution_state():
    dialog = make_dialog()
    try:
        dialog.lbl_execution_phase.setText("이전 실패")
        dialog.lbl_current_table.setText("현재 테이블: old_table (completed)")
        dialog.lbl_current_rows.setText("현재 rows: 30,000 / 54,429 rows")
        dialog.txt_log.setPlainText("old json payload")
        dialog.btn_cleanup_failed.show()

        dialog._reset_command_ui("migrate")

        assert "DB 변경 준비 중" in dialog.lbl_execution_phase.text()
        assert dialog.lbl_current_table.text() == "현재 테이블: -"
        assert dialog.lbl_current_rows.text() == "현재 rows: -"
        assert dialog.txt_log.toPlainText() == ""
        assert not dialog.btn_cleanup_failed.isVisible()
    finally:
        dialog.close()


def test_finished_waits_for_worker_to_stop_before_clearing_reference():
    class FinishingWorker:
        def __init__(self):
            self.wait_timeout = None
            self.running = True

        def isRunning(self):
            return self.running

        def wait(self, timeout):
            self.wait_timeout = timeout
            self.running = False
            return True

    dialog = make_dialog()
    worker = FinishingWorker()
    try:
        dialog.worker = cast(Any, worker)
        dialog._current_command = "migrate"
        dialog._on_finished(False, {"command": "migrate", "success": False})

        assert worker.wait_timeout == 5000
        assert dialog.worker is None
        assert "현재 작업이 실행 중입니다" not in dialog.lbl_next_hint.text()
    finally:
        dialog.worker = None
        dialog.close()


def test_close_button_is_disabled_while_worker_runs():
    dialog = make_dialog()
    try:
        dialog._current_command = "verify"
        dialog._set_running(True)

        assert not dialog.btn_close.isEnabled()
    finally:
        dialog.close()


def test_resume_migration_loads_state_and_starts_migrate(monkeypatch):
    state = {"tables": [{"table": "users", "completed": False, "rows_copied": 5000}]}
    started = {}

    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.load_resume_state",
        lambda key: state,
    )

    dialog = make_dialog()
    dialog.txt_schema.setPlainText(json.dumps({
        "tables": [{"name": "users", "columns": [{"name": "id", "type": "int"}]}]
    }))
    monkeypatch.setattr(dialog, "_confirm_migration_execution", lambda: True)

    def fake_start(command, payload, workflow=False):
        started["command"] = command
        started["payload"] = payload
        started["workflow"] = workflow

    monkeypatch.setattr(dialog, "_start_command_with_payload", fake_start)

    try:
        dialog._resume_migration()

        assert started["command"] == "migrate"
        assert started["payload"]["state"] == state
        assert started["workflow"] is False
    finally:
        dialog.close()


def test_resume_migration_requires_matching_target_schema_approval(monkeypatch):
    state = {"tables": [{"table": "users", "completed": False, "rows_copied": 5000}]}
    started = []

    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.load_resume_state",
        lambda key: state,
    )
    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.QMessageBox.warning",
        lambda *args, **kwargs: None,
    )

    dialog = make_dialog()
    dialog.txt_schema.setPlainText(json.dumps({
        "tables": [{"name": "users", "columns": [{"name": "id", "type": "int"}]}]
    }))
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    try:
        dialog._resume_migration()

        assert started == []
    finally:
        dialog.close()


def test_execute_requires_exact_target_schema_text_before_migrate(monkeypatch):
    dialog = make_dialog()
    started = []
    dialog._set_execution_unlocked(True)
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    try:
        dialog._show_step("execute")

        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_next.isEnabled()

        dialog.input_approval_schema.setText("wrong")

        assert not dialog.btn_migrate.isEnabled()

        dialog.input_approval_schema.setText("target_db")

        assert dialog.btn_migrate.isEnabled()
        dialog._start_command("migrate")

        assert started
        assert started[0][0][0] == "migrate"
    finally:
        dialog.close()


def test_execute_approval_uses_public_when_postgresql_target_schema_blank():
    dialog = make_dialog()
    try:
        dialog.target_form.input_schema.setText("")
        dialog.target_form.input_database.setText("postgres")
        dialog._set_execution_unlocked(True)
        dialog._show_step("execute")

        dialog.input_approval_schema.setText("postgres")

        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_next.isEnabled()

        dialog.input_approval_schema.setText("public")

        assert dialog.btn_migrate.isEnabled()
        assert dialog.btn_next.isEnabled()
        assert dialog.btn_next.text() == "DB 변경 실행"
    finally:
        dialog.close()


def test_execute_approval_invalidates_when_target_schema_changes():
    dialog = make_dialog()
    try:
        dialog._show_step("execute")
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": True,
            "issues": [],
        })
        dialog.input_approval_schema.setText("target_db")

        assert dialog.btn_migrate.isEnabled()
        assert dialog.btn_next.isEnabled()
        assert dialog.btn_next.text() == "DB 변경 실행"

        dialog.target_form.input_schema.setText("changed_target")

        assert not dialog._approval_matches_target_schema()
        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_next.isEnabled()
    finally:
        dialog.close()


def test_schema_change_invalidates_stale_plan_and_verify_reports():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {
                "tables": [{"name": "users", "estimated_rows": 1000}],
                "type_mappings": [{"source_type": "int", "target_type": "bigint"}],
            },
            "issues": [],
        })
        assert dialog._execution_unlocked
        assert "int -> bigint" in dialog.lbl_plan_summary.text()
        assert dialog.last_result is not None
        assert dialog.last_result["command"] == "plan"
        assert dialog.btn_save_report.isEnabled()

        dialog._on_result({
            "event": "result",
            "command": "verify",
            "success": False,
            "mismatches": [
                {
                    "table": "users",
                    "key": "id=7",
                    "column": "email",
                    "source_value": "a@example.com",
                    "target_value": "b@example.com",
                    "difference": "value_mismatch",
                }
            ],
            "row_count_differences": [],
        })
        assert "테이블: users" in dialog.txt_verify_result.toPlainText()
        assert dialog.last_result is not None
        assert dialog.last_result["command"] == "verify"
        assert dialog.btn_save_report.isEnabled()

        dialog.target_form.input_schema.setText("changed_target")

        assert not dialog._execution_unlocked
        assert "int -> bigint" not in dialog.lbl_plan_summary.text()
        assert "아직 실행 계획을 생성하지 않았습니다." in dialog.lbl_plan_summary.text()
        verify_text = dialog.txt_verify_result.toPlainText()
        assert "테이블: users" not in verify_text
        assert "새 검증이 필요합니다" in verify_text
        assert dialog.last_result is None
        assert not dialog.btn_save_report.isEnabled()
    finally:
        dialog.close()


def test_execution_progress_prioritizes_current_table_and_chunk():
    dialog = make_dialog()
    try:
        dialog._on_phase_changed("copy", "copying data")
        dialog._on_table_progress("users", "running")
        dialog._on_row_progress("users", 5000, 20000)

        assert "users" in dialog.lbl_current_table.text()
        assert "5,000 / 20,000 rows" in dialog.lbl_current_rows.text()
        assert "copying data" in dialog.lbl_execution_phase.text()
    finally:
        dialog.close()


def test_execution_row_progress_shows_unknown_total():
    dialog = make_dialog()
    try:
        dialog._on_row_progress("users", 5000, None)

        assert "5,000 / ? rows" in dialog.lbl_current_rows.text()
    finally:
        dialog.close()


def test_verify_progress_updates_status_panel():
    dialog = make_dialog()
    try:
        dialog._show_step("verify")
        dialog.show()
        app.processEvents()
        dialog._current_command = "verify"

        dialog._reset_command_ui("verify")
        dialog._on_phase_changed("verify", "checking table counts")
        dialog._on_table_progress("users", "verifying")
        dialog._on_row_progress("users", 5000, 20000)

        assert dialog.verify_activity_bar.isVisible()
        assert "checking table counts" in dialog.lbl_verify_status.text()
        assert "users" in dialog.lbl_verify_table.text()
        assert "5,000 / 20,000 rows" in dialog.lbl_verify_rows.text()
        assert "[rows:users] 5000/20000" in dialog.txt_verify_log.toPlainText()
    finally:
        dialog.close()


def test_db_change_unlocks_after_preflight_success_and_locks_on_input_change():
    dialog = make_dialog()
    try:
        assert not dialog.btn_migrate.isEnabled()

        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": True,
            "issues": [],
        })

        assert not dialog.btn_migrate.isEnabled()
        assert "Target schema 이름 입력 후" in dialog.lbl_execution_lock.text()

        dialog.input_approval_schema.setText("target_db")

        assert dialog.btn_migrate.isEnabled()

        dialog.source_form.input_database.setText("changed_schema")

        assert not dialog.btn_migrate.isEnabled()
        assert "사전 점검 또는 계획 생성 성공 후" in dialog.lbl_execution_lock.text()
    finally:
        dialog.close()


def test_plan_failure_keeps_db_change_locked():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "plan",
            "success": False,
            "issues": [{"blocking": True}],
        })

        assert not dialog.btn_migrate.isEnabled()
        assert "차단 이슈가 있어" in dialog.txt_log.toPlainText()
    finally:
        dialog.close()


def test_plan_result_renders_meaningful_conversion_changes():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {
                "tables": [
                    {"name": "users", "estimated_rows": 1000},
                    {"name": "orders", "estimated_rows": 2500},
                ],
                "type_mappings": [
                    {
                        "table": "users",
                        "column": "id",
                        "source_type": "int unsigned",
                        "target_type": "bigint",
                        "note": "unsigned widening",
                    },
                    {
                        "table": "users",
                        "column": "payload",
                        "source_type": "json",
                        "target_type": "jsonb",
                        "note": "json normalization",
                    },
                ],
                "ddl_order": ["create tables", "load data", "create foreign keys"],
            },
            "issues": [{"blocking": False, "message": "index prefix length converted"}],
        })

        text = dialog.lbl_plan_summary.text()
        assert "전환 대상 테이블 2개" in text
        assert "예상 rows 3,500" in text
        assert "int unsigned -> bigint" in text
        assert "json -> jsonb" in text
        assert "FK/index는 데이터 적재 후 생성" in text
    finally:
        dialog.close()


def test_plan_summary_ignores_malformed_tables_and_rows():
    dialog = make_dialog()
    try:
        text = dialog._plan_summary_text({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {
                "tables": [
                    {"name": "users", "estimated_rows": 1000},
                    "orders",
                    {"name": "logs", "rows": "not-a-number"},
                    {"name": "events", "estimated_rows": True, "rows": 250},
                    {"name": "audit", "rows": 500},
                    None,
                ],
                "type_mappings": [
                    {"source_type": "varchar", "target_type": "text"},
                    "bad-mapping",
                    {"source_type": "json", "target_type": ""},
                ],
            },
        })

        assert "전환 대상 테이블 4개" in text
        assert "예상 rows 1,750" in text
        assert "varchar -> text" in text
    finally:
        dialog.close()


def test_plan_summary_handles_missing_or_partial_plan_payload():
    dialog = make_dialog()
    try:
        text = dialog._plan_summary_text({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {"tables": None, "type_mappings": None, "ddl_order": "create foreign keys"},
        })

        assert "전환 대상 테이블 0개" in text
        assert "예상 rows 0" in text

        dialog._on_result({"event": "result", "command": "plan", "success": True})

        assert "전환 대상 테이블 0개" in dialog.lbl_plan_summary.text()
    finally:
        dialog.close()


def test_plan_failure_finished_clears_stale_success_summary():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {
                "tables": [{"name": "users", "estimated_rows": 1000}],
                "type_mappings": [{"source_type": "int", "target_type": "bigint"}],
            },
        })
        assert "int -> bigint" in dialog.lbl_plan_summary.text()

        dialog._current_command = "plan"
        dialog._on_finished(False, {})

        text = dialog.lbl_plan_summary.text()
        assert "실행 계획 생성에 실패했습니다" in text
        assert "int -> bigint" not in text
    finally:
        dialog.close()


def test_tunnel_selection_fills_endpoint_fields_from_configured_list():
    class FakeTunnelEngine:
        tunnel_configs = {
            "pg": {
                "remote_port": 5432,
                "default_schema": "analytics",
            }
        }

        def get_active_tunnels(self):
            return []

        def is_running(self, tunnel_id):
            return False

    class FakeConfigManager:
        def load_config(self):
            return {"tunnels": [{
                "id": "pg",
                "name": "PG 분석",
                "connection_mode": "direct",
                "remote_host": "127.0.0.1",
                "remote_port": 5432,
                "db_engine": "postgresql",
                "default_schema": "analytics",
            }]}

        def get_tunnel_credentials(self, tunnel_id):
            assert tunnel_id == "pg"
            return "pg_user", "pg_password"

    dialog = CrossEngineMigrationDialog(
        tunnel_engine=FakeTunnelEngine(),
        config_manager=FakeConfigManager(),
    )
    try:
        dialog.source_form.combo_tunnel.setCurrentIndex(1)

        assert dialog.source_form.engine().value == "postgresql"
        assert dialog.source_form.input_host.text() == "127.0.0.1"
        assert dialog.source_form.input_port.value() == 5432
        assert dialog.source_form.input_user.text() == "pg_user"
        assert dialog.source_form.input_password.text() == "pg_password"
        assert dialog.source_form.input_database.text() == "postgres"
        assert dialog.source_form.input_schema.text() == "analytics"
    finally:
        dialog.close()


def test_readiness_result_shows_only_selected_direction_summary():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "readiness",
            "success": False,
            "directions": [
                {
                    "direction": "mysql_to_postgresql",
                    "success": True,
                    "table_count": 3,
                    "issues": [{"blocking": False, "message": "index prefix requires review"}],
                },
                {
                    "direction": "postgresql_to_mysql",
                    "success": False,
                    "table_count": 2,
                    "issues": [{"blocking": True, "message": "reverse issue"}],
                },
            ],
        })

        text = dialog.lbl_safety_summary.text()
        log = dialog.txt_log.toPlainText()
        assert "MySQL -> PostgreSQL 가능" in text
        assert "warnings=1" in text
        assert "postgresql_to_mysql" not in text
        assert "reverse issue" not in log
    finally:
        dialog.close()


def test_preflight_blocks_execution_when_target_is_not_empty():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "location": "target.public",
                    "message": "target schema is not empty",
                    "blocking": True,
                }
            ],
        })

        assert not dialog.btn_migrate.isEnabled()
        assert "점검 실패" in dialog.lbl_safety_summary.text()
        assert "차단 이슈 1개" in dialog.lbl_safety_summary.text()
        assert "아직 전환 가능 여부를 점검하지 않았습니다" not in dialog.lbl_safety_summary.text()
        assert "Target에 기존 테이블 또는 데이터가 있습니다" in dialog.lbl_target_safety.text()
        assert dialog.btn_target_advanced.isVisible()
    finally:
        dialog.close()


def test_preflight_success_with_nonblocking_target_warning_unlocks_execution():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": True,
            "issues": [
                {
                    "severity": "warning",
                    "location": "target.public",
                    "message": "target has existing advisory metadata",
                    "blocking": False,
                }
            ],
        })

        assert not dialog.btn_migrate.isEnabled()
        dialog.input_approval_schema.setText("target_db")

        assert dialog.btn_migrate.isEnabled()
        assert "점검 통과" in dialog.lbl_safety_summary.text()
        assert "경고 1개" in dialog.lbl_safety_summary.text()
        assert "기존 테이블 또는 데이터 차단 이슈가 없습니다" in dialog.lbl_target_safety.text()
        assert not dialog.btn_target_advanced.isVisible()
    finally:
        dialog.close()


def test_target_advanced_button_expands_inline_without_leaving_safety_step():
    dialog = make_dialog()
    try:
        dialog.show()
        app.processEvents()
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "location": "target.public",
                    "message": "target schema is not empty",
                    "blocking": True,
                }
            ],
        })

        dialog.btn_target_advanced.click()

        assert dialog.current_step_id == "safety"
        assert dialog.target_advanced_panel.isVisible()
        assert dialog.btn_target_advanced.text() == "고급 설정 닫기"
        assert "DB 변경 실행 직전에" in dialog.lbl_target_advanced_help.text()
        assert dialog.chk_cleanup_before_migrate.isVisible()

        dialog.btn_target_advanced.click()

        assert not dialog.target_advanced_panel.isVisible()
        assert dialog.btn_target_advanced.text() == "고급 설정 열기"
    finally:
        dialog.close()


def test_safety_advanced_cleanup_is_planned_not_executed(monkeypatch):
    dialog = make_dialog()
    started = []
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda command, payload, workflow=False: started.append((command, payload, workflow)),
    )
    try:
        dialog.show()
        app.processEvents()
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "location": "target.public",
                    "message": "target schema is not empty",
                    "blocking": True,
                }
            ],
        })
        dialog.btn_target_advanced.click()

        dialog.chk_cleanup_before_migrate.setChecked(True)
        payload = dialog._payload()

        assert payload["execution_options"]["cleanup_before_migrate"] is True
        assert started == []
        assert dialog.btn_next.isEnabled()
        assert "Target 정리를 실행 직전에 수행하도록 계획했습니다" in dialog.lbl_next_hint.text()
    finally:
        dialog.close()


def test_safety_cleanup_plan_does_not_unlock_unrelated_blocking_issue():
    dialog = make_dialog()
    try:
        dialog._show_step("safety")
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "location": "orders.amount",
                    "message": "unsupported precision conversion",
                    "blocking": True,
                }
            ],
        })

        dialog.chk_cleanup_before_migrate.setChecked(True)

        assert not dialog.btn_next.isEnabled()
    finally:
        dialog.close()


def test_migrate_payload_includes_cleanup_before_migrate_option(monkeypatch):
    dialog = make_dialog()
    started = []
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda command, payload, workflow=False: started.append((command, payload, workflow)),
    )
    try:
        dialog._set_execution_unlocked(True)
        dialog._show_step("execute")
        dialog.input_approval_schema.setText("target_db")
        dialog.chk_cleanup_before_migrate.setChecked(True)

        dialog._go_next_step()

        assert started[0][0] == "migrate"
        assert started[0][1]["execution_options"]["cleanup_before_migrate"] is True
    finally:
        dialog.close()


def test_execute_step_uses_bottom_next_button_as_db_change_cta(monkeypatch):
    dialog = make_dialog()
    started = []
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda command, payload, workflow=False: started.append((command, payload, workflow)),
    )
    try:
        dialog._set_execution_unlocked(True)
        dialog._show_step("execute")

        assert not dialog.btn_migrate.isVisible()
        assert dialog.btn_next.text() == "DB 변경 실행"
        assert not dialog.btn_next.isEnabled()

        dialog.input_approval_schema.setText("target_db")

        assert dialog.btn_next.isEnabled()
        dialog._go_next_step()

        assert started[0][0] == "migrate"
        assert dialog.current_step_id == "execute"
    finally:
        dialog.close()


def test_execute_step_next_moves_to_verify_after_migration_success():
    dialog = make_dialog()
    try:
        dialog._show_step("execute")
        dialog._on_result({"event": "result", "command": "migrate", "success": True})

        assert dialog.btn_next.text() == "검증 단계로 이동"
        assert dialog.btn_next.isEnabled()

        dialog._go_next_step()

        assert dialog.current_step_id == "verify"
    finally:
        dialog.close()


def test_failed_migration_cleanup_action_plans_cleanup_for_next_migrate(monkeypatch):
    dialog = make_dialog()
    started = []
    warnings = []
    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args),
    )
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda command, payload, workflow=False: started.append((command, payload, workflow)),
    )
    try:
        dialog._show_step("execute")
        dialog.show()
        app.processEvents()
        dialog.btn_cleanup_failed.show()

        dialog._cleanup_failed_migration()

        assert started == []
        assert warnings

        dialog.input_approval_schema.setText("target_db")
        dialog._cleanup_failed_migration()

        assert started == []
        assert dialog.chk_cleanup_before_migrate.isChecked()
        assert "DB 변경 실행 전에 Target 정리를 수행합니다" in dialog.lbl_migration_result.text()
    finally:
        dialog.close()


def test_safety_step_exposes_only_primary_preflight_action_by_default():
    dialog = make_dialog()
    try:
        dialog._show_step("safety")
        dialog.show()
        app.processEvents()

        visible_button_texts = [
            button.text()
            for button in dialog.step_pages["safety"].findChildren(QPushButton)
            if button.isVisible()
        ]

        assert all("양방향" not in text for text in visible_button_texts)
        assert dialog.btn_run_safety.isVisible()
        assert_widget_reachable(dialog.btn_run_safety, dialog)
        assert not dialog.btn_readiness.isVisible()
        assert dialog.btn_readiness.parentWidget() is None
        assert not dialog.btn_preflight.isVisible()
        assert dialog.btn_preflight.parentWidget() is None
    finally:
        dialog.close()


def test_guide_result_logs_summary():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "guide",
            "success": True,
            "directions": [{
                "direction": "mysql_to_postgresql",
                "success": True,
                "guide": {
                    "create_table_sql": ["CREATE TABLE users(id int);"],
                    "tables": [{"table": "users", "row_samples": [{"id": "1"}]}],
                },
            }],
        })

        log = dialog.txt_log.toPlainText()
        assert "[상세 가이드]" in log
        assert "mysql_to_postgresql" in log
        assert "table guide 1개" in log
    finally:
        dialog.close()


def test_save_report_writes_text_report(monkeypatch, tmp_path):
    report_path = tmp_path / "report.txt"
    monkeypatch.setattr(
        "src.ui.dialogs.cross_engine_migration_dialog.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(report_path), "Text Files (*.txt)"),
    )

    dialog = make_dialog()
    dialog.last_result = {
        "event": "result",
        "command": "verify",
        "success": True,
        "mismatches": [],
    }
    try:
        dialog._save_report()

        assert report_path.exists()
        assert "Command: verify" in Path(report_path).read_text(encoding="utf-8")
        assert "결과 저장 완료" in dialog.txt_log.toPlainText()
        assert "결과 저장 완료" in dialog.txt_verify_log.toPlainText()
    finally:
        dialog.close()
