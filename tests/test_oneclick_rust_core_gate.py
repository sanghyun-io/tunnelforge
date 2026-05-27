from types import SimpleNamespace

import pytest

from src.ui.dialogs.oneclick_migration_dialog import OneClickMigrationWorker


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
        endpoint=SimpleNamespace(engine="mysql"),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )

    worker._ensure_rust_core_connector()


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
