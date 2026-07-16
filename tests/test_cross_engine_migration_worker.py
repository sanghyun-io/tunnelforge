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
        self.reaped = False

    def wait(self, timeout=None):
        self.reaped = True
        return self.return_code

    def poll(self):
        return self.return_code if self.terminated or self.reaped else None

    def terminate(self):
        self.terminated = True


class RecordingStream(io.StringIO):
    def __init__(self, initial_value=""):
        super().__init__(initial_value)
        self.writes = []

    def write(self, text):
        self.writes.append(text)
        return super().write(text)


class LifecycleProcess:
    def __init__(self, *, terminate_wait_times_out=False):
        self.stdin = RecordingStream()
        self.stdout = RecordingStream()
        self.stderr = RecordingStream()
        self.terminate_wait_times_out = terminate_wait_times_out
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts = []
        self.return_code = None
        self.reaped = False

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if (
            timeout is not None
            and self.terminate_wait_times_out
            and self.kill_calls == 0
        ):
            raise subprocess.TimeoutExpired("fake-helper", timeout)
        self.reaped = True
        self.return_code = -9 if self.kill_calls else -15
        return self.return_code


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


def test_worker_cancel_during_popen_factory_reaps_without_building_or_writing_request(
    monkeypatch,
):
    import src.ui.workers.cross_engine_migration_worker as worker_module

    process = LifecycleProcess()
    request_builds = []
    worker = None

    def build_request(*args, **kwargs):
        request_builds.append((args, kwargs))
        return "request-must-not-be-written\n"

    def factory(*args, **kwargs):
        assert worker._process is None
        worker.cancel()
        return process

    monkeypatch.setattr(worker_module, "build_helper_request", build_request)
    worker = CrossEngineMigrationWorker(
        "migrate",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=factory,
    )
    failures = []
    finished = []
    worker.failed.connect(failures.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()
    worker.run()

    assert request_builds == []
    assert process.stdin.writes == []
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_timeouts and process.wait_timeouts[0] is not None
    assert process.reaped is True
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert failures == []
    assert finished == [(False, {"cancelled": True})]


def test_worker_exception_after_child_creation_kills_reaps_joins_and_closes():
    import src.ui.workers.cross_engine_migration_worker as worker_module

    process = LifecycleProcess(terminate_wait_times_out=True)
    process.stdout = RecordingStream("not valid jsonl\n")
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    drain_join_stream_states = []
    original_join = worker._join_stderr_drain

    def record_drain_join(timeout=2.0):
        drain_join_stream_states.append(process.stderr.closed)
        original_join(timeout)

    worker._join_stderr_drain = record_drain_join
    failures = []
    finished = []
    worker.failed.connect(failures.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()
    worker.run()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts == [
        worker_module._PROCESS_SHUTDOWN_TIMEOUT_SECONDS,
        worker_module._PROCESS_SHUTDOWN_TIMEOUT_SECONDS,
    ]
    assert process.reaped is True
    assert drain_join_stream_states == [True]
    assert worker._stderr_thread is not None
    assert not worker._stderr_thread.is_alive()
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert len(failures) == 1
    assert "Invalid helper JSON" in failures[0]
    assert finished == [(False, {"error": failures[0]})]


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
