import io
import subprocess
import sys
import threading

import pytest
from PyQt6.QtCore import Qt

from src.core.cross_engine_migration import parse_helper_event
from src.ui.workers.cross_engine_migration_worker import CrossEngineMigrationWorker


class FakeProcess:
    def __init__(self, stdout_lines, return_code=0, stderr=""):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(stdout_lines) + "\n")
        self.stderr = io.StringIO(stderr)
        self.return_code = return_code
        self.terminated = False

    def wait(self):
        return self.return_code

    def poll(self):
        return None if not self.terminated else self.return_code

    def terminate(self):
        self.terminated = True


def test_worker_run_emits_result():
    process = FakeProcess([
        '{"event":"phase","phase":"preflight","message":"started"}',
        '{"event":"result","command":"preflight","success":true,"issues":[]}',
    ])

    worker = CrossEngineMigrationWorker(
        "preflight",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    phases = []
    results = []
    worker.phase_changed.connect(lambda phase, message: phases.append((phase, message)))
    worker.finished.connect(lambda success, payload: results.append((success, payload)))

    worker.run()

    assert phases == [("preflight", "started")]
    assert results[0][0] is True
    assert results[0][1]["command"] == "preflight"


def test_worker_cancel_before_run_skips_helper_setup_and_finishes_once():
    popen_calls = []

    def fail_if_started(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("cancel-before-run must not start tunnelforge-core")

    worker = CrossEngineMigrationWorker(
        "migrate",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=fail_if_started,
    )
    failures = []
    finished = []
    worker.failed.connect(failures.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.cancel()
    worker.run()
    worker.run()

    assert popen_calls == []
    assert failures == []
    assert finished == [(False, {"cancelled": True})]


def test_worker_run_emits_checkpoint_state():
    state = {"tables": [{"table": "users", "completed": False, "rows_copied": 2}]}
    process = FakeProcess([
        '{"event":"row_progress","table":"users","rows":2,"total":3,"state":{"tables":[{"table":"users","completed":false,"rows_copied":2}]}}',
        '{"event":"result","command":"migrate","success":false,"state":{"tables":[{"table":"users","completed":false,"rows_copied":2}]}}',
    ])

    worker = CrossEngineMigrationWorker(
        "migrate",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    checkpoints = []
    worker.checkpoint.connect(checkpoints.append)
    worker.run()

    assert checkpoints == [state]


def test_worker_dispatch_event_emits_checkpoint_state():
    state = {"tables": [{"table": "users", "completed": False, "rows_copied": 2}]}
    event = parse_helper_event(
        '{"event":"table_progress","table":"users","status":"copying",'
        '"state":{"tables":[{"table":"users","completed":false,"rows_copied":2}]}}'
    )
    worker = CrossEngineMigrationWorker("migrate", {}, helper_path="fake-helper")

    table_events = []
    checkpoints = []
    worker.table_progress.connect(lambda table, status: table_events.append((table, status)))
    worker.checkpoint.connect(checkpoints.append)

    assert worker._dispatch_event(event) is False
    assert table_events == [("users", "copying")]
    assert checkpoints == [state]
    assert worker._last_checkpoint == state


def test_worker_run_emits_failure_on_error():
    process = FakeProcess([
        '{"event":"error","message":"boom"}',
    ])

    worker = CrossEngineMigrationWorker(
        "plan",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    failures = []
    worker.failed.connect(failures.append)
    worker.run()

    assert failures == ["boom"]


def test_worker_run_reports_redacted_stderr_tail_on_failure():
    process = FakeProcess(
        ['{"event":"phase","phase":"preflight","message":"starting"}'],
        return_code=1,
        stderr='connection failed\npassword=supersecret\n"token": "abc123"\n',
    )

    worker = CrossEngineMigrationWorker(
        "preflight",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    failures = []
    worker.failed.connect(failures.append)
    worker.run()

    assert failures, "expected a failure message built from the drained stderr tail"
    message = failures[0]
    assert "connection failed" in message
    assert "supersecret" not in message
    assert "abc123" not in message
    assert "[REDACTED]" in message


def test_worker_run_does_not_deadlock_on_large_stderr():
    """Regression test for the pipe-buffer deadlock: a helper that writes more
    than the OS pipe buffer to stderr before emitting its stdout result must
    not hang the worker, because stderr is now drained concurrently."""
    script = (
        "import sys\n"
        "sys.stderr.write('x' * 300000)\n"
        "sys.stderr.write('\\npassword=supersecret\\n')\n"
        "sys.stderr.flush()\n"
        "print('{\"event\": \"result\", \"command\": \"preflight\", \"success\": true}')\n"
    )

    process_holder = {}

    def factory(*args, **kwargs):
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        process_holder["proc"] = proc
        return proc

    worker = CrossEngineMigrationWorker("preflight", {}, popen_factory=factory)

    results = []
    # run() executes on a plain background thread here (not via QThread.start()),
    # so the default AutoConnection would queue onto an event loop that never runs
    # in this test. Force DirectConnection so the slot runs synchronously instead.
    worker.finished.connect(
        lambda success, payload: results.append((success, payload)),
        type=Qt.ConnectionType.DirectConnection,
    )

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    thread.join(timeout=15)

    if thread.is_alive():
        proc = process_holder.get("proc")
        if proc is not None:
            proc.kill()
        pytest.fail("worker.run() did not complete within timeout - stderr likely deadlocked")

    assert results and results[0][0] is True
    proc = process_holder["proc"]
    assert proc.stdin is not None and proc.stdin.closed
    assert proc.stdout is not None and proc.stdout.closed
    assert proc.stderr is not None and proc.stderr.closed
