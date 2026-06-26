from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from src.ui.dialogs.oneclick_migration_dialog import (
    OneClickMigrationDialog,
    OneClickMigrationWorker,
)
from src.ui.dialogs import migration_dialogs, oneclick_migration_dialog


class FakeEndpoint:
    engine = "mysql"

    def to_payload(self):
        return {
            "engine": "mysql",
            "host": "localhost",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "app",
            "schema": "",
        }


class FakeSchemaConnector:
    def get_schemas(self):
        return ["app"]


class FakeDerivationFacade:
    def __init__(self, result):
        self.result = result
        self.payload = None

    def derive_oneclick_charset_contracts(self, payload):
        self.payload = payload
        return self.result


def test_oneclick_worker_rejects_non_rust_core_connector():
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=object()),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )

    with pytest.raises(RuntimeError, match="Rust DB Core connector"):
        worker._ensure_rust_core_connector()


def test_oneclick_worker_accepts_rust_core_connector_shape():
    connection = SimpleNamespace(
        facade=object(),
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )

    worker._ensure_rust_core_connector()


def test_oneclick_worker_rejects_real_execution_without_backup_confirmation():
    connection = SimpleNamespace(
        facade=object(),
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=False,
        backup_confirmed=True,
    )

    worker.backup_confirmed = False
    with pytest.raises(RuntimeError, match="backup"):
        worker._core_payload(connection)


def test_oneclick_worker_allows_limited_real_execution_with_backup_confirmation():
    connection = SimpleNamespace(
        facade=object(),
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=False,
        backup_confirmed=True,
    )

    payload = worker._core_payload(connection)

    assert payload["dry_run"] is False
    assert payload["backup_confirmed"] is True


def test_oneclick_worker_includes_derived_charset_contracts_when_gate_passes():
    facade = FakeDerivationFacade({
        "success": True,
        "issues": [{
            "issue_type": "charset_issue",
            "severity": "warning",
            "location": "tf_oneclick_app.tf_oneclick_parent",
            "table_name": "tf_oneclick_parent",
            "message": "Table uses a legacy charset.",
            "suggestion": "Convert table charset/collation after FK-safe review.",
            "blocking": False,
        }],
        "contracts": [{
            "issue_index": 0,
            "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
            "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci",
            "rollback_sql": [
                "ALTER TABLE `tf_oneclick_app`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                "ALTER TABLE `tf_oneclick_app`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
            ],
        }],
    })
    connection = SimpleNamespace(
        facade=facade,
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="tf_oneclick_app",
        dry_run=False,
        backup_confirmed=True,
    )

    payload = worker._core_payload(connection)

    assert facade.payload["schema"] == "tf_oneclick_app"
    assert facade.payload["connection"] == FakeEndpoint().to_payload()
    assert payload["issues"][0]["issue_type"] == "charset_issue"
    assert payload["charset_contracts"][0]["issue_index"] == 0
    assert payload["charset_contracts"][0]["target_collation"] == "utf8mb4_0900_ai_ci"


def test_oneclick_worker_omits_charset_contracts_when_derivation_gate_fails():
    facade = FakeDerivationFacade({"success": True, "issues": [], "contracts": []})
    connection = SimpleNamespace(
        facade=facade,
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=False,
        backup_confirmed=True,
    )

    payload = worker._core_payload(connection)

    assert "issues" not in payload
    assert "charset_contracts" not in payload


def test_oneclick_dialog_keeps_dry_run_default_but_allows_limited_real_execution():
    app = QApplication.instance() or QApplication([])

    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    assert dialog.chk_dry_run.isChecked()
    assert dialog.chk_dry_run.isEnabled()
    assert "InnoDB" in dialog.chk_dry_run.toolTip()
    dialog.close()


def test_oneclick_dialog_disabled_real_execution_tooltip_does_not_reference_closed_138(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(oneclick_migration_dialog, "ONECLICK_REAL_EXECUTION_ENABLED", False)

    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    tooltip = dialog.chk_dry_run.toolTip()
    assert dialog.chk_dry_run.isChecked()
    assert not dialog.chk_dry_run.isEnabled()
    assert "#138" not in tooltip
    assert "until" not in tooltip.lower()
    assert "disabled in this build" in tooltip
    dialog.close()


def test_migration_analyzer_exposes_oneclick_with_limited_real_execution_copy():
    app = QApplication.instance() or QApplication([])

    dialog = migration_dialogs.MigrationAnalyzerDialog(
        None,
        connector=FakeSchemaConnector(),
    )

    assert migration_dialogs.ONE_CLICK_MIGRATION_FEATURE_ENABLED is True
    assert not dialog.btn_oneclick.isHidden()
    assert "One-Click Migration" in dialog.btn_oneclick.text()
    tooltip = dialog.btn_oneclick.toolTip()
    assert "dry-run" in tooltip.lower()
    assert "InnoDB" in tooltip
    assert "백업" in tooltip
    dialog.close()


def test_oneclick_worker_translates_core_events_to_ui_signals():
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=SimpleNamespace()),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )
    phases = []
    progress = []
    analysis = []
    plans = []
    logs = []

    worker.phase_changed.connect(lambda phase, name: phases.append((phase, name)))
    worker.progress.connect(lambda percent, message: progress.append((percent, message)))
    worker.analysis_result.connect(lambda total, auto, manual: analysis.append((total, auto, manual)))
    worker.execution_plan_ready.connect(lambda steps, summary: plans.append((steps, summary)))
    worker.log_message.connect(lambda message, style: logs.append(message))

    worker._handle_core_event({"event": "phase", "phase": "analysis", "message": "analysis started"})
    worker._handle_core_event({"event": "progress", "percent": 40, "message": "Analysis completed"})
    worker._handle_core_event({
        "event": "analysis",
        "summary": {"total_issues": 2, "auto_fixable": 1, "manual_review": 1},
    })
    worker._handle_core_event({
        "event": "execution_plan",
        "steps": [{"location": "backup"}],
        "summary": {"total_issues": 1},
    })

    assert phases == [("analysis", "분석")]
    assert progress == [(40, "Analysis completed")]
    assert analysis == [(2, 1, 1)]
    assert plans[0][0] == [{"location": "backup"}]
    assert "analysis started" in logs


def test_oneclick_dialog_renders_charset_plan_counts_and_copy():
    app = QApplication.instance() or QApplication([])
    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )
    logs = []
    dialog._on_log = lambda message, style: logs.append(message)

    dialog._on_execution_plan_ready(
        [
            {
                "issue_type": "charset_issue",
                "location": "app.tf_oneclick_parent",
                "description": "Legacy charset",
                "selected_option": {
                    "strategy": "charset_collation_fk_safe",
                    "label": "Convert charset/collation with FK-safe order",
                    "description": "Convert to utf8mb4 / utf8mb4_0900_ai_ci.",
                    "sql_template": "ALTER TABLE `app`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                },
            },
            {
                "issue_type": "charset_issue",
                "location": "app.legacy_customer",
                "description": "Legacy charset without a complete contract",
                "selected_option": {
                    "strategy": "manual",
                    "label": "Manual review",
                    "description": "Review charset/collation manually.",
                    "sql_template": "",
                },
            },
            {
                "issue_type": "int_display_width",
                "location": "app.orders.id",
                "description": "Display width is ignored by MySQL 8.4",
                "selected_option": {
                    "strategy": "skip",
                    "label": "No DB action required",
                    "description": "MySQL 8.4 ignores integer display width.",
                    "sql_template": "",
                },
            },
        ],
        {"total_issues": 3, "auto_fixable": 1, "manual_review": 1, "skip_recommended": 1},
    )

    widget = dialog.execution_plan_widget
    assert "(1개)" in widget.auto_group.title()
    assert "Convert charset/collation with FK-safe order" in widget.auto_text.toPlainText()
    assert "(1개)" in widget.manual_group.title()
    assert "Review charset/collation manually." in widget.manual_text.toPlainText()
    assert "(1개)" in widget.skip_group.title()
    assert "MySQL 8.4 ignores integer display width." in widget.skip_label.text()
    assert logs == ["📋 실행 계획: 전체 3개, 자동 1개, 수동 1개, 조치 불필요 1개"]
    dialog.close()
