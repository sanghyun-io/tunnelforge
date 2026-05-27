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
