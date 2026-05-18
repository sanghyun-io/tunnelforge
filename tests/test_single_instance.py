import uuid

from PyQt6.QtCore import QCoreApplication

from src.core.single_instance import SingleInstanceGuard


APP = QCoreApplication.instance() or QCoreApplication([])


def _process_events_until(predicate, max_attempts=20):
    for _ in range(max_attempts):
        APP.processEvents()
        if predicate():
            return True
    return False


def test_single_instance_guard_primary_for_unused_name(tmp_path):
    server_name = f"tunnelforge-test-{uuid.uuid4()}"
    guard = SingleInstanceGuard(server_name, str(tmp_path / "single.lock"))

    try:
        assert guard.is_primary is True
        assert guard.is_secondary is False
    finally:
        guard.close()


def test_single_instance_guard_secondary_notifies_primary(tmp_path):
    server_name = f"tunnelforge-test-{uuid.uuid4()}"
    lock_file = str(tmp_path / "single.lock")
    primary = SingleInstanceGuard(server_name, lock_file)
    activations = []
    primary.activation_requested.connect(lambda: activations.append(True))

    try:
        secondary = SingleInstanceGuard(server_name, lock_file)

        assert primary.is_primary is True
        assert secondary.is_secondary is True
        assert SingleInstanceGuard.notify_existing_instance(server_name) is True
        assert _process_events_until(lambda: bool(activations)) is True
    finally:
        primary.close()
