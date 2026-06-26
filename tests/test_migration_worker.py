import pytest

from src.core.migration_analyzer import ActionType, CleanupAction
from src.ui.workers.migration_worker import CleanupWorker
from tests.conftest import FakeMySQLConnector


def _cleanup_action() -> CleanupAction:
    return CleanupAction(
        action_type=ActionType.DELETE,
        table="orders",
        description="delete orphan orders",
        sql="DELETE FROM `app`.`orders` WHERE `user_id` IS NULL",
        affected_rows=3,
    )


def test_cleanup_worker_rejects_legacy_actual_cleanup_mode():
    with pytest.raises(RuntimeError, match="Rust Core"):
        CleanupWorker(
            connector=FakeMySQLConnector(),
            schema="app",
            actions=[_cleanup_action()],
            dry_run=False,
        )


def test_cleanup_worker_allows_dry_run_mode():
    worker = CleanupWorker(
        connector=FakeMySQLConnector(),
        schema="app",
        actions=[_cleanup_action()],
        dry_run=True,
    )

    assert worker.dry_run is True
