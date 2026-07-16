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
    def __init__(
        self,
        *,
        terminate_wait_times_out=False,
        wait_effects=None,
        kill_error=None,
    ):
        self.stdin = RecordingStream()
        self.stdout = RecordingStream()
        self.stderr = RecordingStream()
        self.terminate_wait_times_out = terminate_wait_times_out
        self.wait_effects = list(wait_effects or [])
        self.kill_error = kill_error
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
        if self.kill_error is not None:
            raise self.kill_error

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.wait_effects:
            effect = self.wait_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            self.reaped = True
            self.return_code = int(effect)
            return self.return_code
        if (
            timeout is not None
            and self.terminate_wait_times_out
            and self.kill_calls == 0
        ):
            raise subprocess.TimeoutExpired("fake-helper", timeout)
        self.reaped = True
        self.return_code = -9 if self.kill_calls else -15
        return self.return_code


class BlockingWriteStream(RecordingStream):
    def __init__(self):
        super().__init__()
        self.write_started = threading.Event()
        self.release_write = threading.Event()
        self.close_called = threading.Event()
        self.allow_close = threading.Event()
        self.write_completed = threading.Event()

    def write(self, text):
        self.write_started.set()
        if not self.release_write.wait(timeout=2.0):
            raise AssertionError("cancel did not interrupt the blocked stdin write")
        written = super().write(text)
        self.write_completed.set()
        return written

    def close(self):
        self.close_called.set()
        if not self.allow_close.wait(timeout=2.0):
            raise AssertionError("cancel synchronously closed a blocked stdin stream")
        super().close()


class FailOnceCloseStream(RecordingStream):
    def __init__(self, initial_value=""):
        super().__init__(initial_value)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_calls == 1:
            raise OSError("stream close interrupted")
        super().close()


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


def test_worker_cancel_returns_without_closing_a_blocked_stdin_writer():
    process = LifecycleProcess()
    process.stdin = BlockingWriteStream()
    process.stdout = RecordingStream(
        '{"event":"result","command":"migrate","success":true}\n'
    )
    worker = CrossEngineMigrationWorker(
        "migrate",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    cancel_returned = threading.Event()
    results = []
    finished = []
    worker.result.connect(
        results.append,
        type=Qt.ConnectionType.DirectConnection,
    )
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload)),
        type=Qt.ConnectionType.DirectConnection,
    )

    run_thread = threading.Thread(target=worker.run, name="worker-run")
    cancel_thread = threading.Thread(
        target=lambda: (worker.cancel(), cancel_returned.set()),
        name="worker-cancel",
    )
    run_thread.start()
    assert process.stdin.write_started.wait(timeout=2.0)
    cancel_thread.start()
    cancel_returned_without_external_release = cancel_returned.wait(timeout=0.5)
    assert cancel_returned_without_external_release is True
    assert not process.stdin.close_called.is_set()
    assert process.terminate_calls >= 1

    process.stdin.release_write.set()
    process.stdin.allow_close.set()
    run_thread.join(timeout=2.0)
    cancel_thread.join(timeout=2.0)

    assert process.stdin.close_called.is_set()
    assert process.stdin.write_completed.is_set()
    assert process.terminate_calls >= 1
    assert results == []
    assert not run_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert finished == [(False, {"cancelled": True})]


@pytest.mark.parametrize("poll_error", [OSError("poll failed"), ValueError("closed handle")])
def test_worker_cancel_skips_stream_close_and_terminates_without_polling(poll_error):
    process = LifecycleProcess()

    def fail_poll():
        raise poll_error

    process.poll = fail_poll
    worker = CrossEngineMigrationWorker("migrate", {}, helper_path="fake-helper")
    worker._process = process

    worker.cancel()

    assert not process.stdin.closed
    assert process.terminate_calls == 1


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


def test_worker_valid_result_followed_by_malformed_frame_finishes_false():
    process = LifecycleProcess(wait_effects=[0])
    process.stdout = RecordingStream(
        '{"event":"result","command":"migrate","success":true}\n'
        "not valid jsonl\n"
    )
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    failures = []
    results = []
    finished = []
    worker.failed.connect(failures.append)
    worker.result.connect(results.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert len(failures) == 1
    assert "Invalid helper JSON" in failures[0]
    assert results == []
    assert finished == [(False, {"error": failures[0]})]


def _assert_worker_rejects_multiple_terminal_frames(stdout_lines):
    process = FakeProcess(stdout_lines)
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    failures = []
    results = []
    finished = []
    worker.failed.connect(failures.append)
    worker.result.connect(results.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert len(failures) == 1
    assert "multiple terminal" in failures[0].lower()
    assert results == []
    assert finished == [(False, {"error": failures[0]})]


def test_worker_rejects_error_followed_by_result_terminal_frame():
    _assert_worker_rejects_multiple_terminal_frames([
        '{"event":"error","message":"first failure"}',
        '{"event":"result","command":"migrate","success":true}',
    ])


def test_worker_rejects_result_followed_by_result_terminal_frame():
    _assert_worker_rejects_multiple_terminal_frames([
        '{"event":"result","command":"migrate","success":true}',
        '{"event":"result","command":"migrate","success":false}',
    ])


def test_worker_rejects_result_followed_by_error_terminal_frame():
    _assert_worker_rejects_multiple_terminal_frames([
        '{"event":"result","command":"migrate","success":true}',
        '{"event":"error","message":"late failure"}',
    ])


def test_worker_rejects_zero_exit_without_terminal_frame():
    process = FakeProcess([
        '{"event":"phase","phase":"migrate","message":"started"}',
    ])
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    failures = []
    results = []
    finished = []
    worker.failed.connect(failures.append)
    worker.result.connect(results.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert len(failures) == 1
    assert "exactly one terminal" in failures[0].lower()
    assert results == []
    assert finished == [(False, {"error": failures[0]})]


def test_worker_cancel_terminate_failure_retains_the_published_process_handle():
    process = LifecycleProcess()

    def fail_terminate():
        process.terminate_calls += 1
        raise OSError("terminate denied")

    process.terminate = fail_terminate
    worker = CrossEngineMigrationWorker("migrate", {}, helper_path="fake-helper")
    worker._process = process

    assert worker.cancel() is False
    assert process.terminate_calls == 1
    assert worker.has_unsettled_process() is True


def test_worker_cleanup_continues_from_first_wait_oserror_to_kill_and_reap():
    process = LifecycleProcess(wait_effects=[OSError("wait interrupted"), -9])
    process.stdout = RecordingStream("not valid jsonl\n")
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    finished = []
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert process.kill_calls == 1
    assert process.reaped is True
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert finished and finished[0][0] is False


def test_worker_cleanup_second_wait_timeout_is_residual_until_retry():
    process = LifecycleProcess(
        wait_effects=[
            subprocess.TimeoutExpired("fake-helper", 2.0),
            subprocess.TimeoutExpired("fake-helper", 2.0),
        ]
    )
    process.stdout = RecordingStream(
        '{"event":"result","command":"migrate","success":true}\n'
        "not valid jsonl\n"
    )
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    join_calls = []
    worker._join_stderr_drain = lambda timeout=2.0: join_calls.append(timeout)
    finished = []
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert process.reaped is False
    assert process.stdin.closed
    assert not process.stdout.closed
    assert not process.stderr.closed
    assert join_calls == []
    assert worker._process is process
    assert finished[0][0] is False
    assert finished[0][1]["cleanup_residual"] is True

    process.wait_effects = [-9]
    assert worker.retry_process_cleanup(timeout_seconds=0.05) is True
    assert process.reaped is True
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert join_calls == [0.05]
    assert len(finished) == 1


def test_worker_cleanup_kill_failure_is_residual_until_retry():
    process = LifecycleProcess(
        wait_effects=[subprocess.TimeoutExpired("fake-helper", 2.0)],
        kill_error=OSError("kill denied"),
    )
    process.stdout = RecordingStream("not valid jsonl\n")
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    finished = []
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert process.reaped is False
    assert process.stdin.closed
    assert not process.stdout.closed
    assert not process.stderr.closed
    assert finished[0][0] is False
    assert "kill" in finished[0][1]["error"]
    assert finished[0][1]["cleanup_residual"] is True

    process.kill_error = None
    process.wait_effects = [
        subprocess.TimeoutExpired("fake-helper", 0.05),
        -9,
    ]
    assert worker.retry_process_cleanup(timeout_seconds=0.05) is True
    assert process.reaped is True
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed
    assert len(finished) == 1


def test_worker_stream_close_failure_is_residual_until_retry():
    process = LifecycleProcess(wait_effects=[0])
    process.stdout = FailOnceCloseStream(
        '{"event":"result","command":"migrate","success":true}\n'
    )
    worker = CrossEngineMigrationWorker(
        "migrate",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )
    failures = []
    results = []
    finished = []
    worker.failed.connect(failures.append)
    worker.result.connect(results.append)
    worker.finished.connect(
        lambda success, payload: finished.append((success, payload))
    )

    worker.run()

    assert worker._process is process
    assert not process.stdout.closed
    assert process.stdout.close_calls == 1
    assert results == []
    assert len(failures) == 1
    assert "stream_close" in failures[0]
    assert finished[0][0] is False
    assert finished[0][1]["cleanup_residual"] is True

    assert worker.retry_process_cleanup(timeout_seconds=0.05) is True
    assert process.stdout.closed
    assert process.stdout.close_calls == 2
    assert worker._process is None
    assert len(finished) == 1


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
