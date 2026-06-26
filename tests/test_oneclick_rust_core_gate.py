from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from src.ui.dialogs.oneclick_migration_dialog import (
    OneClickMigrationDialog,
    OneClickMigrationWorker,
)
from src.ui.dialogs import migration_dialogs


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


def test_oneclick_worker_rejects_real_execution_until_readiness_gate_opens():
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

    with pytest.raises(RuntimeError, match="GitHub #138"):
        worker._core_payload(connection)


def test_oneclick_dialog_locks_dry_run_until_readiness_gate_opens():
    app = QApplication.instance() or QApplication([])

    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    assert dialog.chk_dry_run.isChecked()
    assert not dialog.chk_dry_run.isEnabled()
    assert "GitHub #138" in dialog.chk_dry_run.toolTip()
    dialog.close()


def test_migration_analyzer_exposes_oneclick_as_dry_run_preview_only():
    app = QApplication.instance() or QApplication([])

    dialog = migration_dialogs.MigrationAnalyzerDialog(
        None,
        connector=FakeSchemaConnector(),
    )

    assert migration_dialogs.ONE_CLICK_MIGRATION_FEATURE_ENABLED is True
    assert not dialog.btn_oneclick.isHidden()
    assert "Dry-run Preview" in dialog.btn_oneclick.text()
    tooltip = dialog.btn_oneclick.toolTip()
    assert "dry-run" in tooltip.lower()
    assert "실제 변경" in tooltip
    assert "자동 수행" not in tooltip
    assert "자동 수정 → 검증" not in tooltip
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
        "summary": {"total_issues": 2, "auto_fixable": 0, "manual_review": 2},
    })
    worker._handle_core_event({
        "event": "execution_plan",
        "steps": [{"location": "backup"}],
        "summary": {"total_issues": 1},
    })

    assert phases == [("analysis", "분석")]
    assert progress == [(40, "Analysis completed")]
    assert analysis == [(2, 0, 2)]
    assert plans[0][0] == [{"location": "backup"}]
    assert "analysis started" in logs
