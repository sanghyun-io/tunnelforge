import asyncio
import concurrent.futures
import io
import inspect
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import src.core.db_core_facade as db_core_facade
import src.core.db_core_client as db_core_client
from src.core.db_core_service import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DbCoreCallbackError,
    DbCoreGenerationState,
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreRequestResult,
    DbCoreServiceClient,
    DbCoreServiceError,
    MAX_JSONL_FRAME_BYTES,
    REQUIRED_PROCESS_CAPABILITIES,
)


class _Process:
    def __init__(self, lines):
        self._lines = list(lines)
        self.stdout = _AsyncQueueReader()
        self.stderr = _AsyncTextReader("")
        self.returncode = None
        self.stdin = _AsyncTextWriter(self)

    def respond(self, request):
        if request["request_id"].startswith("py-hello-"):
            if hasattr(self.stdout, "feed_line"):
                self.stdout.feed_line(_task3_hello(request["request_id"]))
            return
        while self._lines:
            payload = json.loads(self._lines.pop(0))
            payload.setdefault("request_id", request["request_id"])
            if payload.get("event") in ("result", "error"):
                payload.setdefault("command", request["command"])
            self.stdout.feed_line(payload)
            if payload.get("event") in ("result", "error"):
                break

    def terminate(self):
        self.returncode = 0
        if hasattr(self.stdout, "feed_eof"):
            self.stdout.feed_eof()

    def kill(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class _AsyncTextWriter:
    def __init__(self, process=None):
        self._buffer = io.StringIO()
        self._process = process
        self.pending = None

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        written = self._buffer.write(data)
        if self._process is not None:
            self.pending = json.loads(data)
        return written

    async def drain(self):
        if self._process is not None and self.pending is not None:
            self._process.respond(self.pending)
        return None

    def getvalue(self):
        return self._buffer.getvalue()


class _AsyncTextReader:
    def __init__(self, text):
        self._buffer = io.StringIO(text)

    async def readline(self):
        return self._buffer.readline()


def _client_for_lines(lines):
    process = _Process(lines)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )
    return client, process


def test_typed_outcome_contract_has_exact_public_values():
    assert [kind.value for kind in DbCoreRequestKind] == ["read_only", "mutation"]
    assert [outcome.value for outcome in DbCoreOutcome] == [
        "definite",
        "not_started",
        "failed",
        "outcome_indeterminate",
    ]
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS == 3600.0
    assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS == 5.0
    assert MAX_JSONL_FRAME_BYTES == 1_048_576
    assert REQUIRED_PROCESS_CAPABILITIES == frozenset({
        "request.deadline",
        "request.strict_id",
        "process.generation",
        "mutation.outcome_indeterminate",
    })


def test_request_result_and_payload_wrappers_preserve_legacy_payloads():
    client, _ = _client_for_lines([
        '{"event":"result","command":"service.hello","success":true,"value":1}',
        '{"event":"result","command":"service.hello","success":true,"value":2}',
        '{"event":"result","command":"service.hello","success":true,"value":3}',
    ])

    typed = client.request_result(
        "service.hello",
        request_kind=DbCoreRequestKind.READ_ONLY,
    )
    wrapped = client.request_payload(
        "service.hello",
        request_kind=DbCoreRequestKind.READ_ONLY,
    )
    legacy = client.request("service.hello")

    assert isinstance(typed, DbCoreRequestResult)
    assert typed.request_kind is DbCoreRequestKind.READ_ONLY
    assert typed.outcome is DbCoreOutcome.DEFINITE
    assert typed.payload["value"] == 1
    assert typed.process_generation == 1
    assert wrapped["value"] == 2
    assert legacy["value"] == 3


def test_success_false_business_result_is_still_definite():
    client, _ = _client_for_lines([
        '{"event":"result","command":"connection.test","success":false,"message":"refused"}',
    ])

    result = client.request_result(
        "connection.test",
        request_kind=DbCoreRequestKind.READ_ONLY,
    )

    assert result.outcome is DbCoreOutcome.DEFINITE
    assert result.payload["success"] is False
    assert result.message == "refused"


def test_rust_error_preserves_structured_code_separately():
    client, _ = _client_for_lines([
        '{"event":"error","message":"access denied","code":"28000"}',
    ])

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "connection.open",
            request_kind=DbCoreRequestKind.MUTATION,
        )

    error = raised.value
    assert error.code == "db_core_business_failure"
    assert error.rust_code == "28000"
    assert error.outcome is DbCoreOutcome.FAILED
    assert error.request_kind is DbCoreRequestKind.MUTATION
    assert error.payload["message"] == "access denied"


def _assert_malformed_rust_code_is_protocol_mismatch(error_line):
    client, _ = _client_for_lines([error_line])

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "connection.open",
            request_kind=DbCoreRequestKind.MUTATION,
        )

    error = raised.value
    assert error.code == "db_core_protocol_mismatch"
    assert error.rust_code is None
    assert error.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert "db_core_timeout" not in error.code


def test_rust_error_rejects_missing_code():
    _assert_malformed_rust_code_is_protocol_mismatch(
        '{"event":"error","message":"db_core_timeout from text"}'
    )


def test_rust_error_rejects_empty_code():
    _assert_malformed_rust_code_is_protocol_mismatch(
        '{"event":"error","message":"db_core_timeout from text","code":""}'
    )


def test_rust_error_rejects_non_string_code():
    _assert_malformed_rust_code_is_protocol_mismatch(
        '{"event":"error","message":"db_core_timeout from text","code":123}'
    )


def test_callback_runs_on_caller_while_process_work_stays_on_owner_thread():
    caller_thread_id = threading.get_ident()
    factory_thread_ids = []
    callback_thread_ids = []
    process = _Process([
        '{"event":"phase","phase":"inspect","message":"started"}',
        '{"event":"result","command":"service.hello","success":true}',
    ])

    def process_factory(*args, **kwargs):
        factory_thread_ids.append(threading.get_ident())
        return process

    client = DbCoreServiceClient(executable="fake-core", process_factory=process_factory)
    result = client.request_result(
        "service.hello",
        request_kind=DbCoreRequestKind.READ_ONLY,
        on_event=lambda event: callback_thread_ids.append(threading.get_ident()),
    )

    assert result.outcome is DbCoreOutcome.DEFINITE
    assert factory_thread_ids == [client.owner_thread.ident]
    assert factory_thread_ids[0] != caller_thread_id
    assert callback_thread_ids == [caller_thread_id, caller_thread_id]


def test_progress_callback_ack_precedes_unblocked_terminal_read():
    callback_finished = threading.Event()
    terminal_read_before_callback_finished = []

    class _RaceStdout:
        def __init__(self, process):
            self._process = process
            self._reads = 0

        async def readline(self):
            self._reads += 1
            if self._reads == 1:
                request_id = self._process.stdin.pending["request_id"]
                return json.dumps(_task3_hello(request_id)) + "\n"
            request_id = self._process.stdin.pending["request_id"]
            if self._reads == 2:
                return json.dumps({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "inspect",
                    "message": "started",
                }) + "\n"
            terminal_read_before_callback_finished.append(not callback_finished.is_set())
            return json.dumps({
                "event": "result",
                "request_id": request_id,
                "command": "service.hello",
                "success": True,
            }) + "\n"

    process = _Process([])
    process.stdout = _RaceStdout(process)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )

    def on_event(event):
        if event.get("event") == "phase":
            time.sleep(0.05)
            callback_finished.set()

    result = client.request_result(
        "service.hello",
        request_kind=DbCoreRequestKind.READ_ONLY,
        on_event=on_event,
    )

    assert result.outcome is DbCoreOutcome.DEFINITE
    assert terminal_read_before_callback_finished == [False]


def test_progress_callback_error_prevents_terminal_read():
    terminal_reads = []

    class _RaceStdout:
        def __init__(self, process):
            self._process = process
            self._reads = 0

        async def readline(self):
            self._reads += 1
            if self._reads == 1:
                request_id = self._process.stdin.pending["request_id"]
                return json.dumps(_task3_hello(request_id)) + "\n"
            request_id = self._process.stdin.pending["request_id"]
            if self._reads == 2:
                return json.dumps({
                    "event": "phase",
                    "request_id": request_id,
                    "phase": "inspect",
                    "message": "started",
                }) + "\n"
            terminal_reads.append(True)
            return json.dumps({
                "event": "result",
                "request_id": request_id,
                "command": "service.hello",
                "success": True,
            }) + "\n"

    process = _Process([])
    process.stdout = _RaceStdout(process)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )

    with pytest.raises(DbCoreCallbackError) as raised:
        client.request_result(
            "service.hello",
            request_kind=DbCoreRequestKind.READ_ONLY,
            on_event=lambda event: (_ for _ in ()).throw(LookupError("callback failed")),
        )

    assert isinstance(raised.value.cause, LookupError)
    assert raised.value.outcome is DbCoreOutcome.FAILED
    assert terminal_reads == []


def test_callback_error_exposes_typed_context():
    client, _ = _client_for_lines([
        '{"event":"result","command":"service.hello","success":true}',
    ])

    def fail_callback(event):
        raise LookupError("callback failed")

    with pytest.raises(DbCoreCallbackError) as raised:
        client.request_result(
            "service.hello",
            request_kind=DbCoreRequestKind.READ_ONLY,
            on_event=fail_callback,
        )

    assert isinstance(raised.value.cause, LookupError)
    assert raised.value.request_kind is DbCoreRequestKind.READ_ONLY
    assert raised.value.outcome is DbCoreOutcome.DEFINITE
    assert raised.value.request_result is not None


def test_owner_loop_matches_platform_and_is_named_non_daemon():
    client = DbCoreServiceClient(executable="fake-core", process_factory=lambda *args, **kwargs: None)

    assert client.owner_thread.name.startswith("TunnelForgeDbCoreOwner-")
    assert client.owner_thread.daemon is False
    assert client.owner_thread.is_alive()
    if sys.platform == "win32":
        assert "Proactor" in type(client.owner_loop).__name__
    else:
        assert isinstance(client.owner_loop, asyncio.SelectorEventLoop)


def test_owner_can_bootstrap_python39_selector_loop_on_owner_thread():
    factory_thread_ids = []

    def selector_loop_factory():
        factory_thread_ids.append(threading.get_ident())
        return asyncio.SelectorEventLoop()

    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
        loop_factory=selector_loop_factory,
    )

    assert isinstance(client.owner_loop, asyncio.SelectorEventLoop)
    assert factory_thread_ids == [client.owner_thread.ident]


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_request_deadline_rejects_nonpositive_or_nonfinite_timeout_before_scheduling(timeout):
    process_factory_calls = []
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process_factory_calls.append(True),
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        client.request_result(
            "service.hello",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=timeout,
        )

    assert process_factory_calls == []


def test_request_deadline_is_created_on_caller_before_owner_work():
    caller_thread_id = threading.get_ident()
    monotonic_threads = []
    factory_threads = []
    process = _Process([
        '{"event":"result","command":"service.hello","success":true}',
    ])

    def monotonic():
        monotonic_threads.append(threading.get_ident())
        return time.monotonic()

    def process_factory(*args, **kwargs):
        factory_threads.append(threading.get_ident())
        return process

    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=process_factory,
        monotonic=monotonic,
    )
    client.request_result(
        "service.hello",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=1.0,
    )

    assert monotonic_threads[0] == caller_thread_id
    assert factory_threads == [client.owner_thread.ident]


def test_shutdown_stops_loop_and_joins_non_daemon_owner_boundedly():
    client = DbCoreServiceClient(executable="fake-core", process_factory=lambda *args, **kwargs: None)
    owner = client.owner_thread

    started = time.monotonic()
    client.shutdown(timeout_seconds=0.5)
    elapsed = time.monotonic() - started
    client.shutdown(timeout_seconds=0.5)

    assert elapsed < 0.5
    assert owner.daemon is False
    assert not owner.is_alive()
    assert client.owner_loop.is_closed()


def test_shutdown_start_atomically_rejects_new_request_without_default_deadline_wait(monkeypatch):
    shutdown_entered = threading.Event()
    release_shutdown = threading.Event()
    shutdown_errors = []
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
    )

    async def blocked_shutdown(deadline_at):
        shutdown_entered.set()
        while not release_shutdown.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(client, "_shutdown_on_owner", blocked_shutdown)

    def run_shutdown():
        try:
            client.shutdown(timeout_seconds=1.0)
        except Exception as exc:
            shutdown_errors.append(exc)

    shutdown_thread = threading.Thread(target=run_shutdown)
    shutdown_thread.start()
    assert shutdown_entered.wait(timeout=0.5)

    started = time.monotonic()
    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.request("service.hello", timeout_seconds=3600.0)
        elapsed = time.monotonic() - started
    finally:
        release_shutdown.set()
        shutdown_thread.join(timeout=1.0)

    assert elapsed < 0.2
    assert raised.value.code == "db_core_cleanup_failed"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert shutdown_errors == []
    assert not shutdown_thread.is_alive()


def test_shutdown_cancels_inflight_request_with_typed_bounded_error():
    read_entered = threading.Event()
    release_read = threading.Event()
    request_errors = []
    shutdown_errors = []

    class _BlockingStdout:
        def __init__(self, process):
            self._process = process
            self._reads = 0

        async def readline(self):
            self._reads += 1
            if self._reads == 1:
                request_id = self._process.stdin.pending["request_id"]
                return json.dumps(_task3_hello(request_id)) + "\n"
            read_entered.set()
            while not release_read.is_set():
                await asyncio.sleep(0.005)
            return ""

    class _CancelableProcess(_Process):
        def __init__(self):
            super().__init__([])
            self.stdout = _BlockingStdout(self)

        def terminate(self):
            super().terminate()
            release_read.set()

    process = _CancelableProcess()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )

    def run_request():
        try:
            client.request("service.hello", timeout_seconds=3600.0)
        except BaseException as exc:
            request_errors.append(exc)

    request_thread = threading.Thread(target=run_request)
    request_thread.start()
    assert read_entered.wait(timeout=0.5)

    def run_shutdown():
        try:
            client.shutdown(timeout_seconds=0.5)
        except BaseException as exc:
            shutdown_errors.append(exc)

    shutdown_thread = threading.Thread(target=run_shutdown)
    started = time.monotonic()
    shutdown_thread.start()
    shutdown_thread.join(timeout=1.0)
    if shutdown_thread.is_alive() or request_thread.is_alive():
        release_read.set()
    shutdown_thread.join(timeout=1.0)
    request_thread.join(timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert shutdown_errors == []
    assert len(request_errors) == 1
    assert isinstance(request_errors[0], DbCoreServiceError)
    assert not isinstance(request_errors[0], concurrent.futures.CancelledError)
    assert not request_thread.is_alive()
    assert not shutdown_thread.is_alive()


def test_start_future_cancelled_by_shutdown_is_typed(monkeypatch):
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
    )
    original_submit = client._submit_admitted

    def cancelled_submit(coroutine, request_kind, request_id, deadline_at):
        coroutine.close()
        future = concurrent.futures.Future()
        future.cancel()
        return future

    monkeypatch.setattr(client, "_submit_admitted", cancelled_submit)

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.start()
    finally:
        monkeypatch.setattr(client, "_submit_admitted", original_submit)
        client.shutdown()

    assert raised.value.code == "db_core_cleanup_failed"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED


def test_cancel_active_future_cancelled_by_shutdown_is_typed(monkeypatch):
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
    )
    original_submit = client._submit_owner

    def cancelled_submit(coroutine, request_kind, request_id):
        coroutine.close()
        future = concurrent.futures.Future()
        future.cancel()
        return future

    monkeypatch.setattr(client, "_submit_owner", cancelled_submit)

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.cancel_active_request(timeout_seconds=0.5)
    finally:
        monkeypatch.setattr(client, "_submit_owner", original_submit)
        client.shutdown()

    assert raised.value.code == "db_core_residual_process"
    assert raised.value.outcome is DbCoreOutcome.FAILED


def test_public_cancel_active_request_terminates_only_on_owner_thread():
    read_entered = threading.Event()
    release_read = threading.Event()
    request_errors = []
    terminate_thread_ids = []

    class _BlockingStdout:
        def __init__(self, process):
            self._process = process
            self._reads = 0

        async def readline(self):
            self._reads += 1
            if self._reads == 1:
                request_id = self._process.stdin.pending["request_id"]
                return json.dumps(_task3_hello(request_id)) + "\n"
            read_entered.set()
            while not release_read.is_set():
                await asyncio.sleep(0.005)
            return ""

    class _CancelableProcess(_Process):
        def __init__(self):
            super().__init__([])
            self.stdout = _BlockingStdout(self)

        def terminate(self):
            terminate_thread_ids.append(threading.get_ident())
            super().terminate()
            release_read.set()

    process = _CancelableProcess()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )

    def run_request():
        try:
            client.request("service.hello", timeout_seconds=5.0)
        except BaseException as exc:
            request_errors.append(exc)

    request_thread = threading.Thread(target=run_request)
    request_thread.start()
    assert read_entered.wait(timeout=0.5)

    try:
        assert client.cancel_active_request(timeout_seconds=0.5) is True
    finally:
        release_read.set()
        request_thread.join(timeout=1.0)

    assert terminate_thread_ids == [client.owner_thread.ident]
    assert len(request_errors) == 1
    assert isinstance(request_errors[0], DbCoreServiceError)
    assert not request_thread.is_alive()


def test_client_fixture_teardown_joins_every_owner(tracked_db_core_clients):
    first = DbCoreServiceClient(executable="fake-core", process_factory=lambda *args, **kwargs: None)
    second = DbCoreServiceClient(executable="fake-core", process_factory=lambda *args, **kwargs: None)
    assert first.owner_thread.is_alive()
    assert second.owner_thread.is_alive()

    tracked_db_core_clients.shutdown_all()

    assert tracked_db_core_clients.live_owner_threads() == []


def test_shared_shutdown_is_bounded_and_idempotent(monkeypatch):
    calls = []

    class _Client:
        def shutdown(self, *, timeout_seconds):
            calls.append(timeout_seconds)

    facade = type("_Facade", (), {"client": _Client()})()
    monkeypatch.setattr(db_core_facade, "_shared_facade", facade)

    db_core_facade.shutdown_shared_db_core_facade(timeout_seconds=0.25)
    db_core_facade.shutdown_shared_db_core_facade(timeout_seconds=0.25)

    assert calls == [0.25]
    assert db_core_facade._shared_facade is None


def test_shared_shutdown_retains_facade_when_owner_reports_residual(monkeypatch):
    error = DbCoreServiceError(
        "owner still alive",
        code="db_core_residual_process",
        request_kind=DbCoreRequestKind.MUTATION,
        outcome=DbCoreOutcome.FAILED,
    )

    class _Client:
        def shutdown(self, *, timeout_seconds):
            raise error

    facade = type("_Facade", (), {"client": _Client()})()
    monkeypatch.setattr(db_core_facade, "_shared_facade", facade)

    with pytest.raises(DbCoreServiceError) as raised:
        db_core_facade.shutdown_shared_db_core_facade(timeout_seconds=0.25)

    assert raised.value is error
    assert db_core_facade._shared_facade is facade
    db_core_facade._shared_facade = None


class _AsyncQueueReader:
    def __init__(self):
        self._queue = asyncio.Queue()

    async def readline(self):
        return await self._queue.get()

    def feed_line(self, payload):
        self._queue.put_nowait((json.dumps(payload) + "\n").encode("utf-8"))

    def feed_eof(self):
        self._queue.put_nowait(b"")


class _EmptyAsyncReader:
    async def readline(self):
        return b""


class _ControlledAsyncWriter:
    def __init__(self, process, release_first):
        self._process = process
        self._release_first = release_first
        self._pending = None

    def write(self, data):
        self._process.handle_thread_ids.append(threading.get_ident())
        self._pending = json.loads(data.decode("utf-8"))

    async def drain(self):
        request = self._pending
        request_id = request["request_id"]
        if request_id.startswith("py-hello-"):
            self._process.stdout.feed_line(_task3_hello(request_id))
            return
        self._process.write_order.append(request_id)
        if len(self._process.write_order) == 1:
            self._process.first_write.set()
            while not self._release_first.is_set():
                await asyncio.sleep(0.005)
        self._process.stdout.feed_line({
            "event": "result",
            "command": request["command"],
            "request_id": request_id,
            "success": True,
            "value": request_id,
        })


class _ControlledAsyncProcess:
    def __init__(self, release_first):
        self.returncode = None
        self.stdout = _AsyncQueueReader()
        self.stderr = _EmptyAsyncReader()
        self.first_write = threading.Event()
        self.write_order = []
        self.handle_thread_ids = []
        self.terminate_calls = 0
        self.stdin = _ControlledAsyncWriter(self, release_first)

    def terminate(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.terminate_calls += 1
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.005)
        return self.returncode


def test_owner_serializes_concurrent_request_ids_and_stream_order():
    release_first = threading.Event()
    process = _ControlledAsyncProcess(release_first)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )
    results = {}
    errors = []

    def request(request_id):
        try:
            results[request_id] = client.request(
                "service.hello",
                request_id=request_id,
                timeout_seconds=1.0,
            )["value"]
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=request, args=("req-1",))
    second = threading.Thread(target=request, args=("req-2",))
    first.start()
    first_started = process.first_write.wait(timeout=0.5)
    if first_started:
        second.start()
        time.sleep(0.05)
        serialized_before_release = process.write_order == ["req-1"]
    else:
        serialized_before_release = False
    release_first.set()
    first.join(timeout=1.0)
    if second.ident is not None:
        second.join(timeout=1.0)

    assert first_started is True
    assert serialized_before_release is True
    assert errors == []
    assert results == {"req-1": "req-1", "req-2": "req-2"}
    assert process.write_order == ["req-1", "req-2"]
    assert set(process.handle_thread_ids) == {client.owner_thread.ident}


def test_queued_request_timeout_does_not_cancel_or_terminate_active_request():
    release_first = threading.Event()
    process = _ControlledAsyncProcess(release_first)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )
    first_results = []

    first = threading.Thread(
        target=lambda: first_results.append(client.request(
            "service.hello",
            request_id="active",
            timeout_seconds=1.0,
        )),
    )
    first.start()
    first_started = process.first_write.wait(timeout=0.5)
    if first_started:
        with pytest.raises(DbCoreServiceError) as raised:
            client.request(
                "service.hello",
                request_id="queued",
                timeout_seconds=0.05,
            )
    release_first.set()
    first.join(timeout=1.0)

    assert first_started is True
    assert raised.value.code == "db_core_timeout"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert process.terminate_calls == 0
    assert process.write_order == ["active"]

    assert len(first_results) == 1
    assert first_results[0]["value"] == "active"


def test_client_shutdown_lock_wait_consumes_same_absolute_deadline():
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
    )
    client._shutdown_lock.acquire()
    errors = []

    def shutdown():
        try:
            client.shutdown(timeout_seconds=0.05)
        except BaseException as exc:
            errors.append(exc)

    started = time.monotonic()
    thread = threading.Thread(target=shutdown)
    try:
        thread.start()
        thread.join(timeout=0.2)
        bounded = not thread.is_alive()
    finally:
        client._shutdown_lock.release()
        thread.join(timeout=0.5)
    elapsed = time.monotonic() - started

    assert bounded is True
    assert elapsed < 0.2
    assert len(errors) == 1
    assert isinstance(errors[0], DbCoreServiceError)
    assert errors[0].code == "db_core_residual_process"
    client.shutdown(timeout_seconds=0.5)


def test_shared_shutdown_lock_wait_consumes_same_absolute_deadline(monkeypatch):
    class FakeClient:
        def shutdown(self, *, timeout_seconds):
            raise AssertionError("client shutdown must not start after lock deadline")

    monkeypatch.setattr(db_core_facade, "_shared_facade", type("Facade", (), {"client": FakeClient()})())
    db_core_facade._shared_facade_lock.acquire()
    errors = []

    def shutdown():
        try:
            db_core_facade.shutdown_shared_db_core_facade(timeout_seconds=0.05)
        except BaseException as exc:
            errors.append(exc)

    started = time.monotonic()
    thread = threading.Thread(target=shutdown)
    try:
        thread.start()
        thread.join(timeout=0.2)
        bounded = not thread.is_alive()
    finally:
        db_core_facade._shared_facade_lock.release()
        thread.join(timeout=0.5)
        db_core_facade._shared_facade = None
    elapsed = time.monotonic() - started

    assert bounded is True
    assert elapsed < 0.2
    assert len(errors) == 1
    assert isinstance(errors[0], DbCoreServiceError)
    assert errors[0].code == "db_core_residual_process"


def test_bootstrap_timeout_stops_and_joins_started_owner_without_leak():
    release_factory = threading.Event()
    timer = threading.Timer(0.03, release_factory.set)
    before = set(threading.enumerate())

    def delayed_loop_factory():
        release_factory.wait(timeout=0.2)
        return asyncio.SelectorEventLoop()

    timer.start()
    try:
        with pytest.raises(DbCoreServiceError):
            DbCoreServiceClient(
                executable="fake-core",
                process_factory=lambda *args, **kwargs: None,
                loop_factory=delayed_loop_factory,
                bootstrap_timeout_seconds=0.05,
            )
    finally:
        release_factory.set()
        timer.join(timeout=0.2)

    leaked = [
        thread for thread in threading.enumerate()
        if thread not in before and thread.name.startswith("TunnelForgeDbCoreOwner-")
    ]
    assert leaked == []


def test_bootstrap_loop_factory_failure_joins_started_owner_without_leak():
    before = set(threading.enumerate())

    def failing_loop_factory():
        raise RuntimeError("loop factory failed")

    with pytest.raises(DbCoreServiceError) as raised:
        DbCoreServiceClient(
            executable="fake-core",
            process_factory=lambda *args, **kwargs: None,
            loop_factory=failing_loop_factory,
            bootstrap_timeout_seconds=0.1,
        )

    leaked = [
        thread for thread in threading.enumerate()
        if thread not in before and thread.name.startswith("TunnelForgeDbCoreOwner-")
    ]
    assert raised.value.code == "db_core_start_failed"
    assert leaked == []


def test_shared_shutdown_retries_retained_facades_even_when_shared_is_residual(monkeypatch):
    shared_residual = DbCoreServiceError(
        "shared owner still alive",
        code="db_core_residual_process",
        outcome=DbCoreOutcome.FAILED,
    )

    class Client:
        def __init__(self, error=None):
            self.error = error
            self.calls = 0

        def shutdown(self, *, timeout_seconds):
            self.calls += 1
            if self.error is not None:
                raise self.error

    shared = type("Facade", (), {"client": Client(shared_residual)})()
    retained = type("Facade", (), {"client": Client()})()
    db_core_facade.retain_db_core_facade_for_retry(retained)
    monkeypatch.setattr(db_core_facade, "_shared_facade", shared)

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            db_core_facade.shutdown_shared_db_core_facade(timeout_seconds=0.5)
    finally:
        db_core_facade._shared_facade = None

    assert raised.value is shared_residual
    assert retained.client.calls == 1
    assert db_core_facade.is_db_core_facade_retained(retained) is False


def test_transport_uses_only_owner_asyncio_process_handles():
    source = inspect.getsource(DbCoreServiceClient)

    assert "asyncio.create_subprocess_exec" in source
    assert "run_in_executor" not in source
    assert "subprocess.Popen" not in source
    assert "_stderr_thread" not in source


def _task3_hello(request_id, **overrides):
    event = {
        "event": "result",
        "request_id": request_id,
        "command": "service.hello",
        "success": True,
        "service": "tunnelforge-core",
        "protocol_version": 1,
        "process_version": 1,
        "process_capabilities": sorted(REQUIRED_PROCESS_CAPABILITIES),
        "max_jsonl_frame_bytes": MAX_JSONL_FRAME_BYTES,
        "max_assembled_event_bytes": db_core_client.MAX_ASSEMBLED_EVENT_BYTES,
        "max_assembled_event_chunks": db_core_client.MAX_ASSEMBLED_EVENT_CHUNKS,
        "max_assembled_event_nodes": db_core_client.MAX_ASSEMBLED_EVENT_NODES,
        "max_assembled_event_depth": db_core_client.MAX_ASSEMBLED_EVENT_DEPTH,
        "capabilities": ["service.shutdown"],
    }
    event.update(overrides)
    return event


class FakeClock:
    def __init__(self, now=100.0):
        self.now = float(now)
        self.calls = []

    def monotonic(self):
        self.calls.append((threading.get_ident(), self.now))
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _Task3QueueReader:
    def __init__(self, process, name):
        self.process = process
        self.name = name
        self.queue = asyncio.Queue()
        self.error = None

    async def readline(self):
        self.process.handle_thread_ids.append(threading.get_ident())
        if self.name == "stdout":
            self.process.read_entered.set()
        else:
            self.process.stderr_entered.set()
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        return await self.queue.get()

    def feed_event(self, event):
        self.queue.put_nowait(
            (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        )

    def feed_raw(self, data):
        self.queue.put_nowait(data)

    def feed_eof(self):
        self.queue.put_nowait(b"")

    def fail(self, error):
        self.error = error


class _Task3Writer:
    def __init__(self, process):
        self.process = process
        self.pending = None

    def write(self, data):
        self.process.handle_thread_ids.append(threading.get_ident())
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.process.frame_byte_lengths.append(len(data))
        self.process.raw_writes.append(data)
        self.pending = json.loads(data.decode("utf-8"))
        if self.pending["command"] != "service.hello" and self.process.fail_write:
            raise OSError("simulated transport write failure")
        self.process.writes.append(self.pending)
        if self.pending["command"] != "service.hello":
            self.process.before_write.set()

    async def drain(self):
        self.process.handle_thread_ids.append(threading.get_ident())
        request = self.pending
        assert request is not None
        self.process.drain_entered.set()
        if request["command"] != "service.hello" and self.process.stall_drain:
            await self.process.release_drain.wait()
        self.process.respond(request)


class _Task3Process:
    def __init__(
        self,
        *,
        hello_overrides=None,
        hello_remove=(),
        responder=None,
        stall_hello=False,
        stall_drain=False,
        stall_wait=False,
        fail_write=False,
        fail_terminate=False,
    ):
        self.returncode = None
        self.hello_overrides = dict(hello_overrides or {})
        self.hello_remove = tuple(hello_remove)
        self.responder = responder
        self.stall_hello = stall_hello
        self.stall_drain = stall_drain
        self.stall_wait = stall_wait
        self.fail_write = fail_write
        self.fail_terminate = fail_terminate
        self.release_drain = asyncio.Event()
        self.stdout = _Task3QueueReader(self, "stdout")
        self.stderr = _Task3QueueReader(self, "stderr")
        self.stdin = _Task3Writer(self)
        self.before_write = threading.Event()
        self.drain_entered = threading.Event()
        self.read_entered = threading.Event()
        self.stderr_entered = threading.Event()
        self.wait_entered = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.writes = []
        self.raw_writes = []
        self.frame_byte_lengths = []
        self.handle_thread_ids = []

    def respond(self, request):
        if request["command"] == "service.hello":
            if not self.stall_hello:
                event = _task3_hello(request["request_id"], **self.hello_overrides)
                for field in self.hello_remove:
                    event.pop(field, None)
                self.stdout.feed_event(event)
            return
        if self.responder is not None:
            self.responder(self, request)

    def terminate(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.terminate_calls += 1
        if self.fail_terminate:
            raise OSError("simulated terminate failure")
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def kill(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.wait_calls += 1
        self.wait_entered.set()
        if self.stall_wait and self.returncode == 0:
            await asyncio.Event().wait()
        while self.returncode is None:
            await asyncio.sleep(0.001)
        return self.returncode


class _Task3Factory:
    def __init__(self, processes):
        self.processes = list(processes)
        self.calls = []
        self.thread_ids = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.thread_ids.append(threading.get_ident())
        return self.processes.pop(0)


class _Task3StalledSpawnFactory:
    def __init__(self):
        self.entered = threading.Event()
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.entered.set()
        await asyncio.Event().wait()


def _task3_result(process, request, **values):
    process.stdout.feed_event({
        "event": "result",
        "request_id": request["request_id"],
        "command": request["command"],
        "success": True,
        **values,
    })


def _task3_chunk_frames(event, *, text_chars=80_000, item_count=2_000):
    request_id = event["request_id"]
    command = event.get("command")
    logical_event = event["event"]
    frames = []
    next_node_id = 0

    def add_frame(node_id, parent_id, slot_index, sequence, final, value_kind, **payload):
        frame = {
            "event": "payload_chunk",
            "request_id": request_id,
            "command": command,
            "logical_event": logical_event,
            "node_id": node_id,
            "parent_node_id": parent_id,
            "slot_index": slot_index,
            "sequence": sequence,
            "final": final,
            "value_kind": value_kind,
            **payload,
        }
        encoded = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        assert len(encoded) <= MAX_JSONL_FRAME_BYTES
        frames.append(frame)

    def visit(value, parent_id=None, slot_index=None):
        nonlocal next_node_id
        node_id = next_node_id
        next_node_id += 1
        if isinstance(value, dict):
            items = []
            for index, (key, child) in enumerate(value.items()):
                key_id = visit(key, node_id, index)
                value_id = visit(child, node_id, index)
                items.append({"key_node_id": key_id, "value_node_id": value_id})
            chunks = [items[index:index + item_count] for index in range(0, len(items), item_count)] or [[]]
            for sequence, chunk in enumerate(chunks):
                add_frame(
                    node_id,
                    parent_id,
                    slot_index,
                    sequence,
                    sequence == len(chunks) - 1,
                    "object",
                    items=chunk,
                )
        elif isinstance(value, list):
            child_ids = [visit(child, node_id, index) for index, child in enumerate(value)]
            chunks = [child_ids[index:index + item_count] for index in range(0, len(child_ids), item_count)] or [[]]
            for sequence, chunk in enumerate(chunks):
                add_frame(
                    node_id,
                    parent_id,
                    slot_index,
                    sequence,
                    sequence == len(chunks) - 1,
                    "list",
                    items=chunk,
                )
        elif isinstance(value, str):
            chunks = [value[index:index + text_chars] for index in range(0, len(value), text_chars)] or [""]
            for sequence, chunk in enumerate(chunks):
                add_frame(
                    node_id,
                    parent_id,
                    slot_index,
                    sequence,
                    sequence == len(chunks) - 1,
                    "utf8_string",
                    text=chunk,
                )
        else:
            add_frame(
                node_id,
                parent_id,
                slot_index,
                0,
                True,
                "atomic",
                items=[value],
            )
        return node_id

    assert visit(event) == 0
    return frames


def test_spawn_hello_exact_capabilities_and_generation_state_correlation():
    transitions = []
    process = _Task3Process(responder=lambda proc, req: _task3_result(proc, req, value=7))
    factory = _Task3Factory([process])
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=factory,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )

    result = client.request_result(
        "schema.list",
        request_id="request-1",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=1.0,
    )

    assert result.payload["value"] == 7
    assert result.process_generation == 1
    assert client.process_generation == 1
    assert client.generation_state is DbCoreGenerationState.ACTIVE
    assert [request["command"] for request in process.writes] == ["service.hello", "schema.list"]
    assert process.writes[1]["request_id"] == "request-1"
    assert factory.calls[0][1]["limit"] == MAX_JSONL_FRAME_BYTES
    assert transitions == [
        (DbCoreGenerationState.CREATING, 1),
        (DbCoreGenerationState.ACTIVE, 1),
    ]
    assert set(factory.thread_ids + process.handle_thread_ids) == {client.owner_thread.ident}


@pytest.mark.parametrize(
    "hello_overrides, expected_code",
    [
        ({"protocol_version": 2}, "db_core_capability_missing"),
        ({"process_version": 2}, "db_core_capability_missing"),
        ({"max_jsonl_frame_bytes": MAX_JSONL_FRAME_BYTES - 1}, "db_core_capability_missing"),
        ({"max_assembled_event_bytes": db_core_client.MAX_ASSEMBLED_EVENT_BYTES - 1}, "db_core_capability_missing"),
        ({"max_assembled_event_chunks": db_core_client.MAX_ASSEMBLED_EVENT_CHUNKS - 1}, "db_core_capability_missing"),
        ({"max_assembled_event_nodes": db_core_client.MAX_ASSEMBLED_EVENT_NODES - 1}, "db_core_capability_missing"),
        ({"max_assembled_event_depth": db_core_client.MAX_ASSEMBLED_EVENT_DEPTH - 1}, "db_core_capability_missing"),
        ({"process_capabilities": ["request.deadline"]}, "db_core_capability_missing"),
        ({"process_capabilities": sorted(REQUIRED_PROCESS_CAPABILITIES) + ["extra"]}, "db_core_capability_missing"),
        ({"process_capabilities": [*sorted(REQUIRED_PROCESS_CAPABILITIES)[:-1], 7]}, "db_core_capability_missing"),
    ],
)
def test_hello_capability_negotiation_is_exact_and_reaps(hello_overrides, expected_code):
    transitions = []
    process = _Task3Process(hello_overrides=hello_overrides)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id="capability-request",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert raised.value.code == expected_code
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert process.terminate_calls == 1
    assert process.wait_calls >= 1
    assert transitions == [
        (DbCoreGenerationState.CREATING, 1),
        (DbCoreGenerationState.POISONED, 1),
        (DbCoreGenerationState.REAPING, 1),
        (DbCoreGenerationState.CLOSED, 1),
    ]


@pytest.mark.parametrize(
    "field",
    [
        "max_assembled_event_bytes",
        "max_assembled_event_chunks",
        "max_assembled_event_nodes",
        "max_assembled_event_depth",
    ],
)
def test_hello_missing_aggregate_limit_reaps(field):
    process = _Task3Process(hello_remove=(field,))
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
    )

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id=f"missing-{field}",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_capability_missing"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert process.terminate_calls == 1


@pytest.mark.parametrize("response_id", [None, "wrong-request"])
def test_strict_response_id_mismatch_poison_reaps(response_id):
    def respond(process, request):
        process.stdout.feed_event({
            "event": "result",
            "request_id": response_id,
            "command": request["command"],
            "success": True,
        })

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id="strict-request",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_request_id_mismatch"
    assert raised.value.outcome is DbCoreOutcome.FAILED
    assert process.terminate_calls == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


@pytest.mark.parametrize("response_command", [None, "schema.inspect"])
def test_terminal_error_command_mismatch_poison_reaps(response_command):
    def respond(process, request):
        event = {
            "event": "error",
            "request_id": request["request_id"],
            "code": "planned_failure",
            "message": "planned failure",
        }
        if response_command is not None:
            event["command"] = response_command
        process.stdout.feed_event(event)

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id="strict-error-command",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_protocol_mismatch"
    assert raised.value.outcome is DbCoreOutcome.FAILED
    assert process.terminate_calls == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


def _bounded_chunk(**overrides):
    frame = {
        "event": "payload_chunk",
        "request_id": "bounded-assembly",
        "command": "schema.list",
        "logical_event": "result",
        "node_id": 1,
        "parent_node_id": 0,
        "slot_index": 0,
        "sequence": 0,
        "final": False,
        "value_kind": "utf8_string",
        "text": "x",
    }
    frame.update(overrides)
    return frame


def test_payload_assembler_aggregate_byte_limit_accepts_exact_boundary_and_rejects_over():
    exact = db_core_client._PayloadAssembler(
        "bounded-assembly",
        max_aggregate_bytes=10,
    )
    assert exact.consume(_bounded_chunk(), frame_bytes=10) is None

    over = db_core_client._PayloadAssembler(
        "bounded-assembly",
        max_aggregate_bytes=10,
    )
    with pytest.raises(db_core_client.HelperProtocolError, match="aggregate byte limit"):
        over.consume(_bounded_chunk(), frame_bytes=11)


def test_payload_assembler_chunk_and_node_limits_accept_exact_boundary_and_reject_over():
    exact_chunks = db_core_client._PayloadAssembler(
        "bounded-assembly",
        max_chunks=2,
    )
    assert exact_chunks.consume(_bounded_chunk(), frame_bytes=1) is None
    assert exact_chunks.consume(
        _bounded_chunk(sequence=1, text="y"),
        frame_bytes=1,
    ) is None
    with pytest.raises(db_core_client.HelperProtocolError, match="chunk count limit"):
        exact_chunks.consume(
            _bounded_chunk(sequence=2, text="z"),
            frame_bytes=1,
        )

    exact_nodes = db_core_client._PayloadAssembler(
        "bounded-assembly",
        max_nodes=2,
    )
    assert exact_nodes.consume(_bounded_chunk(node_id=1), frame_bytes=1) is None
    assert exact_nodes.consume(
        _bounded_chunk(node_id=2, slot_index=1),
        frame_bytes=1,
    ) is None
    with pytest.raises(db_core_client.HelperProtocolError, match="node count limit"):
        exact_nodes.consume(
            _bounded_chunk(node_id=3, slot_index=2),
            frame_bytes=1,
        )


def test_payload_assembler_depth_limit_accepts_exact_boundary_and_rejects_over():
    logical = {
        "event": "result",
        "request_id": "bounded-assembly",
        "command": "schema.list",
        "success": True,
        "value": {"outer": [{"inner": "done"}]},
    }

    def value_depth(value):
        if isinstance(value, dict):
            children = [item for pair in value.items() for item in pair]
            return 1 + max(value_depth(child) for child in children)
        if isinstance(value, list):
            return 1 + max(value_depth(child) for child in value)
        return 1

    frames = _task3_chunk_frames(logical)
    depth = value_depth(logical)
    exact = db_core_client._PayloadAssembler("bounded-assembly", max_depth=depth)
    assembled = None
    for frame in frames:
        assembled = exact.consume(frame, frame_bytes=1) or assembled
    assert assembled == logical

    over = db_core_client._PayloadAssembler("bounded-assembly", max_depth=depth - 1)
    with pytest.raises(db_core_client.HelperProtocolError, match="depth limit"):
        for frame in frames:
            over.consume(frame, frame_bytes=1)


def test_outbound_frame_over_limit_is_not_written_and_generation_stays_active():
    process = _Task3Process(responder=lambda proc, req: _task3_result(proc, req))
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.request_result(
        "schema.list",
        request_id="activate-before-oversized-write",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=1.0,
    )
    writes_before_oversized_request = list(process.writes)

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "query.execute",
            {"sql": "x" * MAX_JSONL_FRAME_BYTES},
            request_id="oversized-write",
            request_kind=DbCoreRequestKind.MUTATION,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_write_failed"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert process.writes == writes_before_oversized_request
    assert process.terminate_calls == 0
    assert client.generation_state is DbCoreGenerationState.ACTIVE


def test_malicious_raw_over_limit_frame_poison_reaps():
    def respond(process, request):
        process.stdout.feed_raw(b"{" + (b"x" * MAX_JSONL_FRAME_BYTES) + b"}\n")

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id="oversized-read",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_protocol_mismatch"
    assert raised.value.outcome is DbCoreOutcome.FAILED
    assert process.terminate_calls == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_stream_reader_limit_error_poison_reaps():
    def respond(process, _request):
        process.stdout.fail(ValueError("Separator is found, but chunk is longer than limit"))

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id="stream-limit-read",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_protocol_mismatch"
    assert raised.value.outcome is DbCoreOutcome.FAILED
    assert process.terminate_calls == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_oversized_intermediate_utf8_scalar_reassembles_exactly():
    value = "🙂한" * 180_000
    logical = {
        "event": "result",
        "request_id": "utf8-scalar",
        "command": "query.execute",
        "success": True,
        "value": value,
    }

    def respond(process, request):
        for frame in _task3_chunk_frames(logical):
            process.stdout.feed_event(frame)

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    callbacks = []

    result = client.request_result(
        "query.execute",
        request_id="utf8-scalar",
        request_kind=DbCoreRequestKind.READ_ONLY,
        on_event=callbacks.append,
        timeout_seconds=2.0,
    )

    assert result.payload == logical
    assert result.payload["value"] == value
    assert callbacks == [logical]


def test_nested_large_key_and_multibyte_value_reassemble_exactly():
    large_key = "키" * 400_000
    multibyte_value = "값🙂" * 210_000
    nested = {"outer": [{large_key: {"inner": multibyte_value}}]}
    logical = {
        "event": "result",
        "request_id": "nested-utf8",
        "command": "schema.inspect",
        "success": True,
        "schema": nested,
    }

    def respond(process, request):
        for frame in _task3_chunk_frames(logical):
            process.stdout.feed_event(frame)

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    result = client.request_result(
        "schema.inspect",
        request_id="nested-utf8",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=3.0,
    )

    assert result.payload == logical
    assert result.payload["schema"] == nested


@pytest.mark.parametrize(
    "command,event_name,field",
    [
        ("query.execute", "result", "rows"),
        ("dump.run", "row_progress", "detail"),
        ("schema.inspect", "result", "schema"),
        ("migration.plan", "result", "plan"),
    ],
)
def test_near_limit_query_stream_schema_plan_frame_compatibility(command, event_name, field):
    request_id = "near-limit"
    event = {
        "event": event_name,
        "request_id": request_id,
        "command": command,
        field: "x" * (MAX_JSONL_FRAME_BYTES - 2_000),
    }
    if event_name == "result":
        event["success"] = True

    def respond(process, request):
        process.stdout.feed_event(event)
        if event_name != "result":
            _task3_result(process, request, value="done")

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    callbacks = []

    result = client.request_result(
        command,
        request_id=request_id,
        request_kind=DbCoreRequestKind.READ_ONLY,
        on_event=callbacks.append,
        timeout_seconds=2.0,
    )

    assert callbacks[0] == event
    assert result.outcome is DbCoreOutcome.DEFINITE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda frames: frames[:1] + [frames[0]] + frames[1:],
        lambda frames: [dict(frames[0], sequence=1)] + frames[1:],
        lambda frames: [dict(frames[0], value_kind="list")] + frames[1:],
    ],
)
def test_malformed_node_chunk_poison_reaps(mutate):
    logical = {
        "event": "result",
        "request_id": "malformed-chunk",
        "command": "schema.inspect",
        "success": True,
        "value": "🙂" * 300_000,
    }

    def respond(process, request):
        for frame in mutate(_task3_chunk_frames(logical)):
            process.stdout.feed_event(frame)

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.inspect",
            request_id="malformed-chunk",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=2.0,
        )

    assert raised.value.code == "db_core_protocol_mismatch"
    assert process.terminate_calls == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_mutation_terminal_scalar_encode_failure_after_side_effect_is_indeterminate():
    def fail_terminal_encode(process, request):
        process.stdout.feed_eof()

    process = _Task3Process(responder=fail_terminal_encode)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "dump.import",
            request_id="terminal-scalar-encode-failure",
            request_kind=DbCoreRequestKind.MUTATION,
            timeout_seconds=1.0,
        )

    assert raised.value.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert [request["command"] for request in process.writes].count("dump.import") == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_emit_failure_produces_no_followup_frames():
    def fail_after_progress(process, request):
        process.stdout.feed_event({
            "event": "phase",
            "request_id": request["request_id"],
            "phase": "mutation",
            "message": "side effect started",
        })
        process.stdout.feed_eof()

    process = _Task3Process(responder=fail_after_progress)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    callbacks = []

    with pytest.raises(DbCoreServiceError):
        client.request_result(
            "dump.import",
            request_id="no-followup",
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=callbacks.append,
            timeout_seconds=1.0,
        )

    assert [event["event"] for event in callbacks] == ["phase"]
    assert [request["command"] for request in process.writes].count("dump.import") == 1


def test_next_request_after_emit_failure_uses_fresh_generation():
    def fail_after_side_effect(process, request):
        process.stdout.feed_eof()

    first = _Task3Process(responder=fail_after_side_effect)
    second = _Task3Process(responder=lambda proc, req: _task3_result(proc, req, value="fresh"))
    factory = _Task3Factory([first, second])
    client = DbCoreServiceClient(executable="fake-core", process_factory=factory)

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "dump.import",
            request_id="mutation-emit-failure",
            request_kind=DbCoreRequestKind.MUTATION,
            timeout_seconds=1.0,
        )

    recovered = client.request_result(
        "schema.list",
        request_id="fresh-read",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=1.0,
    )

    assert raised.value.code == "db_core_process_died"
    assert raised.value.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert [request["command"] for request in first.writes].count("dump.import") == 1
    assert first.terminate_calls <= 1
    assert first.wait_calls >= 1
    assert recovered.process_generation == 2
    assert recovered.payload["value"] == "fresh"
    assert client.generation_state is DbCoreGenerationState.ACTIVE


def test_no_callback_never_enqueues_long_progress_stream(monkeypatch):
    original_queue = db_core_client.queue.Queue
    instances = []

    class RecordingQueue(original_queue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.put_count = 0
            self.peak_size = 0
            instances.append(self)

        def put(self, item, *args, **kwargs):
            self.put_count += 1
            result = super().put(item, *args, **kwargs)
            self.peak_size = max(self.peak_size, self.qsize())
            return result

    monkeypatch.setattr(db_core_client.queue, "Queue", RecordingQueue)

    def respond(process, request):
        for index in range(5_000):
            process.stdout.feed_event({
                "event": "row_progress",
                "request_id": request["request_id"],
                "command": request["command"],
                "rows": index,
            })
        _task3_result(process, request)

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    result = client.request_result(
        "schema.list",
        request_id="no-callback-progress",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=3.0,
    )

    assert result.outcome is DbCoreOutcome.DEFINITE
    assert len(instances) == 1
    assert instances[0].put_count == 0
    assert instances[0].qsize() == 0


def test_callback_progress_queue_is_single_slot_and_acknowledged(monkeypatch):
    original_queue = db_core_client.queue.Queue
    instances = []

    class RecordingQueue(original_queue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.peak_size = 0
            instances.append(self)

        def put(self, item, *args, **kwargs):
            result = super().put(item, *args, **kwargs)
            self.peak_size = max(self.peak_size, self.qsize())
            return result

    monkeypatch.setattr(db_core_client.queue, "Queue", RecordingQueue)

    def respond(process, request):
        for index in range(100):
            process.stdout.feed_event({
                "event": "row_progress",
                "request_id": request["request_id"],
                "command": request["command"],
                "rows": index,
            })
        _task3_result(process, request)

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    callbacks = []

    result = client.request_result(
        "schema.list",
        request_id="callback-progress",
        request_kind=DbCoreRequestKind.READ_ONLY,
        on_event=callbacks.append,
        timeout_seconds=3.0,
    )

    assert result.outcome is DbCoreOutcome.DEFINITE
    assert len(callbacks) == 101
    assert len(instances) == 1
    assert instances[0].maxsize == 1
    assert instances[0].peak_size <= 1


def test_stderr_cancellation_resistance_is_deadline_bounded_and_not_closed():
    process = _Task3Process(responder=lambda proc, req: _task3_result(proc, req))
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.request_result(
        "schema.list",
        request_id="activate-stderr-cancel",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=1.0,
    )

    async def exercise():
        old_task = client._stderr_task
        assert old_task is not None
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
        release = asyncio.Event()

        async def cancellation_resistant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        resistant = asyncio.create_task(cancellation_resistant())
        await asyncio.sleep(0)
        client._stderr_task = resistant
        started = time.monotonic()
        try:
            await client._terminate_process_on_owner(time.monotonic() + 0.05)
        except DbCoreServiceError as error:
            caught = error
            elapsed = time.monotonic() - started
            state = client.generation_state
        else:
            pytest.fail("cancellation-resistant stderr task must report a residual")
        finally:
            release.set()
            await resistant
            client._stderr_task = None
        return caught, elapsed, state

    future = client._submit_owner(
        exercise(),
        DbCoreRequestKind.READ_ONLY,
        "stderr-cancel",
    )
    error, elapsed, state = future.result(timeout=1.0)

    assert error.code == "db_core_residual_process"
    assert elapsed < 0.2
    assert state is DbCoreGenerationState.REAPING
    client.shutdown(timeout_seconds=1.0)


def test_external_reap_cancellation_retains_process_and_stderr_for_retry():
    process = _Task3Process(responder=lambda proc, req: _task3_result(proc, req))
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.request_result(
        "schema.list",
        request_id="activate-external-reap-cancel",
        request_kind=DbCoreRequestKind.READ_ONLY,
        timeout_seconds=1.0,
    )

    async def exercise():
        old_task = client._stderr_task
        assert old_task is not None
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
        release = asyncio.Event()

        async def cancellation_resistant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        resistant = asyncio.create_task(cancellation_resistant())
        await asyncio.sleep(0)
        client._stderr_task = resistant
        reap = asyncio.create_task(
            client._terminate_process_on_owner(time.monotonic() + 1.0)
        )
        await asyncio.sleep(0.02)
        reap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reap
        retained = (
            client.generation_state,
            client._process is process,
            client._stderr_task is resistant,
        )
        release.set()
        await resistant
        return retained

    retained = client._submit_owner(
        exercise(), DbCoreRequestKind.READ_ONLY, "external-reap-cancel"
    ).result(timeout=1.0)

    assert retained == (DbCoreGenerationState.REAPING, True, True)
    client.shutdown(timeout_seconds=1.0)
    assert client.generation_state is DbCoreGenerationState.CLOSED


@pytest.mark.parametrize("cleanup_exception", [RuntimeError("cleanup runtime"), asyncio.CancelledError()])
def test_cleanup_base_exception_preserves_mutation_indeterminate(cleanup_exception):
    def respond(process, request):
        process.stdout.feed_event({
            "event": "result",
            "request_id": request["request_id"],
            "command": "wrong.command",
            "success": True,
        })

    process = _Task3Process(responder=respond)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    original_reap = client._poison_and_reap_on_owner

    async def failing_reap(_deadline_at):
        raise cleanup_exception

    client._poison_and_reap_on_owner = failing_reap
    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.request_result(
                "dump.import",
                request_id="base-exception-cleanup",
                request_kind=DbCoreRequestKind.MUTATION,
                timeout_seconds=1.0,
            )
    finally:
        client._poison_and_reap_on_owner = original_reap
        client.shutdown(timeout_seconds=1.0)

    assert raised.value.code == "db_core_protocol_mismatch"
    assert raised.value.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert raised.value.cleanup_error.code == "db_core_cleanup_failed"
    assert raised.value.payload["cleanup_error"]["code"] == "db_core_cleanup_failed"
    assert [request["command"] for request in process.writes].count("dump.import") == 1


def test_reap_failure_preserves_post_write_mutation_uncertainty_without_retry():
    def respond(process, request):
        process.stdout.feed_event({
            "event": "result",
            "request_id": request["request_id"],
            "command": "wrong.command",
            "success": True,
        })

    process = _Task4Process(
        responder=respond,
        fail_terminate=True,
        fail_kill=True,
    )
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.request_result(
                "dump.import",
                request_id="indeterminate-reap-failure",
                request_kind=DbCoreRequestKind.MUTATION,
                timeout_seconds=1.0,
            )
    finally:
        process.fail_terminate_task4 = False
        process.fail_kill = False
        client.shutdown(timeout_seconds=1.0)

    error = raised.value
    assert error.code == "db_core_protocol_mismatch"
    assert error.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert error.cleanup_error.code == "db_core_residual_process"
    assert error.payload["cleanup_error"]["code"] == "db_core_residual_process"
    cleanup_payload = error.payload["cleanup_error"]["payload"]
    assert cleanup_payload["stage"] == "kill"
    assert cleanup_payload["pid"] == 4242
    assert cleanup_payload["process_generation"] == 1
    assert cleanup_payload["generation_state"] == "reaping"
    assert cleanup_payload["pending_tasks"] == []
    assert [request["command"] for request in process.writes].count("dump.import") == 1


def test_real_rust_cli_chunks_reassemble_in_python():
    repository = Path(__file__).parents[1]
    manifest = repository / "migration_core" / "Cargo.toml"
    subprocess.run(
        ["cargo", "build", "--manifest-path", str(manifest), "--bin", "tunnelforge-core"],
        cwd=repository,
        capture_output=True,
        check=True,
        timeout=120,
    )
    metadata = subprocess.run(
        ["cargo", "metadata", "--manifest-path", str(manifest), "--format-version", "1", "--no-deps"],
        cwd=repository,
        capture_output=True,
        check=True,
        timeout=30,
    )
    binary_name = "tunnelforge-core.exe" if sys.platform == "win32" else "tunnelforge-core"
    binary = Path(json.loads(metadata.stdout)["target_directory"]) / "debug" / binary_name
    assert binary.is_file()
    request_id = "rust-python-chunk-compat"
    column_name = "열" * 220_000
    request = {
        "command": "plan",
        "request_id": request_id,
        "payload": {
            "source_engine": "mysql",
            "target_engine": "postgresql",
            "schema": {
                "tables": [{
                    "name": "large_names",
                    "columns": [{
                        "name": column_name,
                        "type": "int(11) auto_increment",
                        "nullable": False,
                        "primary_key": True,
                    }],
                }],
            },
        },
    }
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    assert len(encoded) < MAX_JSONL_FRAME_BYTES

    completed = subprocess.run(
        [str(binary)],
        input=encoded,
        capture_output=True,
        check=True,
        timeout=30,
    )
    frames = completed.stdout.splitlines(keepends=True)
    assert len(frames) > 1
    assembler = db_core_client._PayloadAssembler(request_id)
    logical = None
    for frame in frames:
        payload = json.loads(frame.decode("utf-8"))
        logical = assembler.consume(payload, frame_bytes=len(frame)) or logical

    assert logical is not None
    assert logical["event"] == "result"
    assert logical["request_id"] == request_id
    assert logical["command"] == "plan"
    assert logical["success"] is True
    expected_ddl = (
        'CREATE TABLE "large_names" (\n'
        f'  "{column_name}" INTEGER GENERATED BY DEFAULT AS IDENTITY NOT NULL,\n'
        f'  PRIMARY KEY ("{column_name}")\n'
        ');'
    )
    assert logical["plan"]["ddl"] == [expected_ddl]


def test_stale_required_generation_is_rejected_before_fresh_process_spawn():
    def mismatch_response(process, request):
        process.stdout.feed_event({
            "event": "result",
            "request_id": "wrong-request",
            "command": request["command"],
            "success": True,
        })

    first = _Task3Process(responder=mismatch_response)
    second = _Task3Process(responder=lambda proc, req: _task3_result(proc, req))
    factory = _Task3Factory([first, second])
    client = DbCoreServiceClient(executable="fake-core", process_factory=factory)

    with pytest.raises(DbCoreServiceError):
        client.request_result(
            "schema.list",
            request_id="poison-generation-one",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=1.0,
        )

    assert client.process_generation == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "query.execute",
            {"sql": "select 1"},
            request_id="stale-generation-one",
            request_kind=DbCoreRequestKind.READ_ONLY,
            required_generation=1,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_stale_connection"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert len(factory.calls) == 1
    assert second.writes == []
    assert client.process_generation == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_spawn_stall_consumes_same_absolute_deadline_and_closes_generation():
    factory = _Task3StalledSpawnFactory()
    client = DbCoreServiceClient(executable="fake-core", process_factory=factory)

    started = time.monotonic()
    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id="stall-spawn",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=0.15,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert raised.value.code == "db_core_timeout"
    assert raised.value.outcome is DbCoreOutcome.NOT_STARTED
    assert factory.entered.is_set()
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_cleanup_cancelled_injected_spawn_settles_at_exhausted_deadline():
    clock = FakeClock()
    factory = _Task3StalledSpawnFactory()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=factory,
        monotonic=clock.monotonic,
    )

    async def exercise():
        client._process_generation = 1
        client._transition_generation(DbCoreGenerationState.CREATING)
        client._spawn_task = asyncio.create_task(factory())
        client._spawn_is_native = False
        await asyncio.sleep(0)
        await client._terminate_process_on_owner(clock.monotonic())

    future = client._submit_owner(
        exercise(),
        DbCoreRequestKind.MUTATION,
        "cancel-injected-spawn-at-deadline",
    )
    try:
        future.result(timeout=1.0)
    finally:
        async def clear_failed_probe():
            spawn_task = client._spawn_task
            if spawn_task is not None:
                if not spawn_task.done():
                    spawn_task.cancel()
                await asyncio.gather(spawn_task, return_exceptions=True)
            client._spawn_task = None
            client._spawn_is_native = False
            client._spawn_residual = None
            if client.generation_state is DbCoreGenerationState.CREATING:
                client._transition_generation(DbCoreGenerationState.POISONED)
            if client.generation_state is DbCoreGenerationState.POISONED:
                client._transition_generation(DbCoreGenerationState.REAPING)
            if client.generation_state is DbCoreGenerationState.REAPING:
                client._transition_generation(DbCoreGenerationState.CLOSED)

        client._submit_owner(
            clear_failed_probe(),
            DbCoreRequestKind.MUTATION,
            "clear-cancel-injected-spawn-probe",
        ).result(timeout=1.0)
        client.shutdown(timeout_seconds=1.0)

    assert factory.entered.is_set()
    assert client._spawn_task is None
    assert client._spawn_residual is None
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_cleanup_cancelled_stderr_settles_at_exhausted_deadline():
    clock = FakeClock()
    process = _Task3Process(responder=lambda proc, req: _task3_result(proc, req))
    process.wait = None
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
    )
    client.start()

    async def exercise():
        old_task = client._stderr_task
        assert old_task is not None
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)

        async def pending_stderr():
            await asyncio.Event().wait()

        stderr_task = asyncio.create_task(pending_stderr())
        await asyncio.sleep(0)
        client._stderr_task = stderr_task
        await client._terminate_process_on_owner(clock.monotonic())

    future = client._submit_owner(
        exercise(),
        DbCoreRequestKind.MUTATION,
        "cancel-stderr-at-deadline",
    )
    try:
        future.result(timeout=1.0)
    finally:
        async def clear_failed_probe():
            stderr_task = client._stderr_task
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            client._stderr_task = None
            client._process = None
            if client.generation_state is DbCoreGenerationState.REAPING:
                client._transition_generation(DbCoreGenerationState.CLOSED)

        client._submit_owner(
            clear_failed_probe(),
            DbCoreRequestKind.MUTATION,
            "clear-cancel-stderr-probe",
        ).result(timeout=1.0)
        client.shutdown(timeout_seconds=1.0)

    assert client._stderr_task is None
    assert client._process is None
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_shutdown_owner_cleanup_deadline_reserves_stop_and_join_handoff(monkeypatch):
    clock = FakeClock()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
        monotonic=clock.monotonic,
    )
    observed_deadlines = []
    original_shutdown = client._shutdown_on_owner

    async def record_and_fail(deadline_at):
        observed_deadlines.append(deadline_at)
        raise client._residual_process_error(
            "final_wait",
            "simulated final wait refusal",
        )

    monkeypatch.setattr(client, "_shutdown_on_owner", record_and_fail)
    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.shutdown(timeout_seconds=1.0)

        assert raised.value.payload["stage"] == "final_wait"
        assert observed_deadlines == [pytest.approx(100.8)]
    finally:
        monkeypatch.setattr(client, "_shutdown_on_owner", original_shutdown)
        client.shutdown(timeout_seconds=1.0)


def test_write_failure_poison_reaps_without_retry():
    process = _Task3Process(fail_write=True)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "dump.import",
            request_id="failed-write",
            request_kind=DbCoreRequestKind.MUTATION,
            timeout_seconds=1.0,
        )

    assert raised.value.code == "db_core_write_failed"
    assert raised.value.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
    assert len([frame for frame in process.raw_writes if b'"command":"dump.import"' in frame]) == 1
    assert process.terminate_calls == 1
    assert client.generation_state is DbCoreGenerationState.CLOSED


@pytest.mark.parametrize("stall", ["hello", "drain", "read", "cleanup"])
def test_one_absolute_deadline_bounds_hello_write_drain_read_cleanup(stall):
    process = _Task3Process(
        stall_hello=stall == "hello",
        stall_drain=stall == "drain",
        stall_wait=stall == "cleanup",
        responder=(
            None
            if stall in ("read", "cleanup")
            else lambda proc, req: _task3_result(proc, req)
        ),
    )
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))

    started = time.monotonic()
    with pytest.raises(DbCoreServiceError) as raised:
        client.request_result(
            "schema.list",
            request_id=f"stall-{stall}",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=0.15,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert raised.value.code == "db_core_timeout"
    assert raised.value.outcome in (DbCoreOutcome.NOT_STARTED, DbCoreOutcome.FAILED)
    assert process.terminate_calls == 1
    assert process.wait_calls >= 1
    assert process.kill_calls == (1 if stall == "cleanup" else 0)
    assert client.generation_state is DbCoreGenerationState.CLOSED


class _Task4Writer(_Task3Writer):
    def __init__(self, process, *, fail_close=False):
        super().__init__(process)
        self.fail_close = fail_close
        self.close_calls = 0
        self.wait_closed_calls = 0

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise OSError("simulated stdin close failure")

    async def wait_closed(self):
        self.wait_closed_calls += 1


class _Task4Process(_Task3Process):
    def __init__(
        self,
        *,
        pid=4242,
        fail_close=False,
        fail_terminate=False,
        stall_terminate_wait=False,
        fail_kill=False,
        stall_final_wait=False,
        **kwargs,
    ):
        super().__init__(fail_terminate=False, **kwargs)
        self.pid = pid
        self.stdin = _Task4Writer(self, fail_close=fail_close)
        self.fail_terminate_task4 = fail_terminate
        self.stall_terminate_wait = stall_terminate_wait
        self.fail_kill = fail_kill
        self.stall_final_wait = stall_final_wait

    def terminate(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.terminate_calls += 1
        if self.fail_terminate_task4:
            raise OSError("simulated terminate failure")
        if not self.stall_terminate_wait:
            self.returncode = 15
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    def kill(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.kill_calls += 1
        if self.fail_kill:
            raise OSError("simulated kill failure")
        if not self.stall_final_wait:
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self):
        self.handle_thread_ids.append(threading.get_ident())
        self.wait_calls += 1
        self.wait_entered.set()
        while self.returncode is None:
            await asyncio.sleep(0.001)
        return self.returncode


class _Task4CancelledSpawnReturnsChild:
    def __init__(self, process):
        self.process = process
        self.entered = threading.Event()
        self.cancelled = threading.Event()

    async def __call__(self, *args, **kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            return self.process


def _task4_start_request(client, *, request_id, timeout_seconds=2.0):
    errors = []

    def run():
        try:
            client.request_result(
                "dump.import",
                request_id=request_id,
                request_kind=DbCoreRequestKind.MUTATION,
                timeout_seconds=timeout_seconds,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _task4_record_deadlines_and_threads(client, monkeypatch):
    active_deadlines = []
    cleanup_deadlines = []
    cleanup_threads = []
    original_request = client._request_on_owner
    original_terminate = client._terminate_process_on_owner

    async def recording_request(*args, **kwargs):
        active_deadlines.append(args[7])
        return await original_request(*args, **kwargs)

    async def recording_terminate(deadline_at):
        cleanup_deadlines.append(deadline_at)
        cleanup_threads.append(threading.get_ident())
        return await original_terminate(deadline_at)

    monkeypatch.setattr(client, "_request_on_owner", recording_request)
    monkeypatch.setattr(client, "_terminate_process_on_owner", recording_terminate)
    return active_deadlines, cleanup_deadlines, cleanup_threads


def _task4_assert_cancelled_generation(
    client,
    process,
    request_thread,
    request_errors,
    transitions,
    frames_at_linearization,
    active_deadlines,
    cleanup_deadlines,
    cleanup_threads,
):
    request_thread.join(timeout=1.0)
    assert not request_thread.is_alive()
    assert len(request_errors) == 1
    assert isinstance(request_errors[0], DbCoreServiceError)
    assert len(process.raw_writes) == frames_at_linearization
    assert [state for state, _ in transitions] == [
        DbCoreGenerationState.POISONED,
        DbCoreGenerationState.REAPING,
        DbCoreGenerationState.CLOSED,
    ]
    assert cleanup_threads and set(cleanup_threads) == {client.owner_thread.ident}
    assert active_deadlines
    assert cleanup_deadlines
    assert max(cleanup_deadlines) <= active_deadlines[0]
    assert client._process is None
    assert client._stderr_task is None
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_cancel_during_start(monkeypatch):
    clock = FakeClock()
    transitions = []
    process = _Task4Process()
    factory = _Task4CancelledSpawnReturnsChild(process)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=factory,
        monotonic=clock.monotonic,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )
    active, cleanup, threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(client, request_id="cancel-start")
    assert factory.entered.wait(timeout=0.5)
    transitions.clear()
    frames = len(process.raw_writes)

    assert client.cancel_active_request(timeout_seconds=5.0) is True

    assert factory.cancelled.is_set()
    _task4_assert_cancelled_generation(
        client, process, request_thread, errors, transitions, frames, active, cleanup, threads
    )


def test_cancel_native_spawn_without_process_identity_reports_residual(monkeypatch):
    entered = threading.Event()

    async def stalled_native_spawn(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        db_core_client.asyncio,
        "create_subprocess_exec",
        stalled_native_spawn,
    )
    client = DbCoreServiceClient(executable="fake-native-core")
    request_thread, errors = _task4_start_request(
        client,
        request_id="cancel-native-spawn",
    )
    assert entered.wait(timeout=0.5)

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.cancel_active_request(timeout_seconds=0.5)

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload["stage"] == "spawn_identity"
        assert raised.value.payload["pid"] is None
        assert raised.value.payload["process_generation"] == 1
        assert raised.value.payload["pending_tasks"]
    finally:
        request_thread.join(timeout=1.0)

        async def clear_fault_injection():
            client._spawn_residual = None
            if client.generation_state is DbCoreGenerationState.REAPING:
                client._transition_generation(DbCoreGenerationState.CLOSED)

        client._submit_owner(
            clear_fault_injection(),
            DbCoreRequestKind.MUTATION,
            "clear-native-spawn-fault",
        ).result(timeout=1.0)
        client.shutdown(timeout_seconds=1.0)

    assert not request_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DbCoreServiceError)


def test_cancel_during_hello(monkeypatch):
    clock = FakeClock()
    transitions = []
    process = _Task4Process(stall_hello=True)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )
    active, cleanup, threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(client, request_id="cancel-hello")
    assert process.read_entered.wait(timeout=0.5)
    transitions.clear()
    frames = len(process.raw_writes)

    assert client.cancel_active_request(timeout_seconds=5.0) is True

    _task4_assert_cancelled_generation(
        client, process, request_thread, errors, transitions, frames, active, cleanup, threads
    )


def test_cancel_while_request_waits_to_write(monkeypatch):
    clock = FakeClock()
    transitions = []
    process = _Task4Process()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )
    client.start()
    transitions.clear()
    write_waiting = threading.Event()
    original_write = client._write_request_frame_on_owner

    async def wait_before_application_write(process_arg, body, cutoff_at, **kwargs):
        if body["command"] != "service.hello":
            write_waiting.set()
            await asyncio.Event().wait()
        return await original_write(process_arg, body, cutoff_at, **kwargs)

    monkeypatch.setattr(client, "_write_request_frame_on_owner", wait_before_application_write)
    active, cleanup, threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(client, request_id="cancel-before-write")
    assert write_waiting.wait(timeout=0.5)
    frames = len(process.raw_writes)

    assert client.cancel_active_request(timeout_seconds=5.0) is True

    _task4_assert_cancelled_generation(
        client, process, request_thread, errors, transitions, frames, active, cleanup, threads
    )


def test_cancel_during_stdin_drain(monkeypatch):
    clock = FakeClock()
    transitions = []
    process = _Task4Process(stall_drain=True)
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )
    client.start()
    transitions.clear()
    process.before_write.clear()
    active, cleanup, threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(client, request_id="cancel-drain")
    assert process.before_write.wait(timeout=0.5)
    frames = len(process.raw_writes)

    assert client.cancel_active_request(timeout_seconds=5.0) is True

    _task4_assert_cancelled_generation(
        client, process, request_thread, errors, transitions, frames, active, cleanup, threads
    )


def test_cancel_during_stdout_read(monkeypatch):
    clock = FakeClock()
    transitions = []
    process = _Task4Process()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )
    client.start()
    transitions.clear()
    process.before_write.clear()
    process.read_entered.clear()
    active, cleanup, threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(client, request_id="cancel-read")
    assert process.before_write.wait(timeout=0.5)
    assert process.read_entered.wait(timeout=0.5)
    frames = len(process.raw_writes)

    assert client.cancel_active_request(timeout_seconds=5.0) is True

    _task4_assert_cancelled_generation(
        client, process, request_thread, errors, transitions, frames, active, cleanup, threads
    )


def test_cancel_during_shutdown(monkeypatch):
    clock = FakeClock()
    transitions = []
    process = _Task4Process()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
        phase_observer=lambda state, generation: transitions.append((state, generation)),
    )
    client.start()
    transitions.clear()
    process.before_write.clear()
    process.read_entered.clear()
    active, cleanup, threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(client, request_id="cancel-shutdown")
    assert process.before_write.wait(timeout=0.5)
    assert process.read_entered.wait(timeout=0.5)
    frames = len(process.raw_writes)

    client.shutdown(timeout_seconds=5.0)

    _task4_assert_cancelled_generation(
        client, process, request_thread, errors, transitions, frames, active, cleanup, threads
    )
    assert not client.owner_thread.is_alive()
    assert client.owner_loop.is_closed()


@pytest.mark.parametrize(
    "active_timeout, cancel_timeout, expected_deadline",
    [
        (1.0, 30.0, 101.0),
        (30.0, 1.0, 100.8),
        (30.0, 30.0, 100.0 + DEFAULT_SHUTDOWN_TIMEOUT_SECONDS),
    ],
)
def test_cancel_cleanup_deadline_is_earliest_active_cancel_or_cleanup_cap(
    monkeypatch,
    active_timeout,
    cancel_timeout,
    expected_deadline,
):
    clock = FakeClock()
    process = _Task4Process()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
    )
    client.start()
    process.before_write.clear()
    process.read_entered.clear()
    active, cleanup, _threads = _task4_record_deadlines_and_threads(client, monkeypatch)
    request_thread, errors = _task4_start_request(
        client,
        request_id="cancel-deadline-{}-{}".format(active_timeout, cancel_timeout),
        timeout_seconds=active_timeout,
    )
    assert process.before_write.wait(timeout=0.5)
    assert process.read_entered.wait(timeout=0.5)

    assert client.cancel_active_request(timeout_seconds=cancel_timeout) is True
    request_thread.join(timeout=1.0)

    assert not request_thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], DbCoreServiceError)
    assert active == [pytest.approx(100.0 + active_timeout)]
    assert cleanup
    assert cleanup == [pytest.approx(expected_deadline)]
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_cancel_owner_cleanup_reserves_caller_handoff_and_preserves_diagnostics(
    monkeypatch,
):
    process = _Task4Process(
        pid=9877,
        stall_terminate_wait=True,
        stall_final_wait=True,
    )
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
    )
    client.start()
    process.before_write.clear()
    process.read_entered.clear()
    request_thread, errors = _task4_start_request(
        client,
        request_id="cancel-reserved-handoff",
        timeout_seconds=2.0,
    )
    assert process.before_write.wait(timeout=0.5)
    assert process.read_entered.wait(timeout=0.5)
    owner_deadlines = []
    original_terminate = client._terminate_process_on_owner

    async def delay_residual_handoff(deadline_at):
        owner_deadlines.append(deadline_at)
        try:
            return await original_terminate(deadline_at)
        except DbCoreServiceError:
            await asyncio.sleep(0.03)
            raise

    monkeypatch.setattr(
        client,
        "_terminate_process_on_owner",
        delay_residual_handoff,
    )
    started = client._monotonic()
    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.cancel_active_request(timeout_seconds=0.6)

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload.get("stage") == "final_wait"
        assert raised.value.payload.get("pid") == 9877
        assert raised.value.payload.get("generation_state") == "reaping"
        assert raised.value.payload.get("pending_tasks") == []
        assert owner_deadlines == [pytest.approx(started + 0.48, abs=0.03)]
        assert client.generation_state is DbCoreGenerationState.REAPING
    finally:
        monkeypatch.setattr(
            client,
            "_terminate_process_on_owner",
            original_terminate,
        )
        process.stall_final_wait = False
        process.returncode = -9
        process.stdout.feed_eof()
        process.stderr.feed_eof()
        request_thread.join(timeout=1.0)
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)

    assert not request_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DbCoreServiceError)


def test_cancel_active_at_exhausted_deadline_settles_task_and_reaps_process():
    clock = FakeClock()
    process = _Task4Process()
    process.wait = None
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
    )
    client.start()
    task_settled = threading.Event()

    async def exercise():
        async def active_request():
            try:
                await asyncio.Event().wait()
            finally:
                task_settled.set()

        task = asyncio.create_task(active_request())
        await asyncio.sleep(0)
        client._active_request_task = task
        client._active_request_deadline_at = clock.monotonic()
        try:
            return await client._cancel_active_on_owner(clock.monotonic() + 1.0)
        finally:
            client._active_request_task = None
            client._active_request_deadline_at = None
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    future = client._submit_owner(
        exercise(),
        DbCoreRequestKind.MUTATION,
        "cancel-active-at-exhausted-deadline",
    )
    try:
        assert future.result(timeout=1.0) is True
    finally:
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)

    assert task_settled.is_set()
    assert process.terminate_calls == 1
    assert client._process is None
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_native_like_process_wait_settles_once_at_exhausted_deadline():
    clock = FakeClock()
    process = _Task4Process()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
    )
    client.start()

    future = client._submit_owner(
        client._terminate_process_on_owner(clock.monotonic()),
        DbCoreRequestKind.MUTATION,
        "native-wait-at-exhausted-deadline",
    )
    try:
        future.result(timeout=1.0)
    finally:
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)

    assert process.wait_entered.is_set()
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0
    assert client._process is None
    assert client.generation_state is DbCoreGenerationState.CLOSED


def test_cancel_active_task_residual_still_reaps_process():
    clock = FakeClock()
    process = _Task4Process()
    process.wait = None
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
        monotonic=clock.monotonic,
    )
    client.start()

    async def exercise():
        release = asyncio.Event()

        async def resistant_request():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(resistant_request())
        await asyncio.sleep(0)
        client._active_request_task = task
        client._active_request_deadline_at = clock.monotonic()
        try:
            with pytest.raises(DbCoreServiceError) as raised:
                await client._cancel_active_on_owner(clock.monotonic() + 1.0)
            assert process.terminate_calls == 1
            assert client._process is None
            assert client.generation_state is DbCoreGenerationState.CLOSED
            return raised.value
        finally:
            release.set()
            client._active_request_task = None
            client._active_request_deadline_at = None
            await asyncio.gather(task, return_exceptions=True)

    future = client._submit_owner(
        exercise(),
        DbCoreRequestKind.MUTATION,
        "cancel-resistant-active-at-exhausted-deadline",
    )
    try:
        error = future.result(timeout=1.0)
    finally:
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)

    assert error.code == "db_core_residual_process"
    assert error.payload["stage"] == "request_cancel"
    assert error.payload["pending_tasks"]


def test_cleanup_close_stdin_failure_still_reaps_child():
    process = _Task4Process(fail_close=True)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.start()

    client.shutdown(timeout_seconds=0.5)

    assert process.stdin.close_calls == 1
    assert process.terminate_calls == 1
    assert process.wait_calls >= 1
    assert client._process is None
    assert client._stderr_task is None
    assert not client.owner_thread.is_alive()


def test_cleanup_accepts_terminal_injected_process_without_wait_handle():
    process = _Task4Process()
    process.wait = None
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=_Task3Factory([process]),
    )
    client.start()

    client.shutdown(timeout_seconds=0.5)

    assert process.terminate_calls == 1
    assert process.returncode is not None
    assert client._process is None
    assert client.generation_state is DbCoreGenerationState.CLOSED
    assert not client.owner_thread.is_alive()


def test_cleanup_terminate_exception_escalates_to_kill_and_reaps():
    process = _Task4Process(fail_terminate=True)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.start()

    try:
        client.shutdown(timeout_seconds=0.5)
    finally:
        if client.owner_thread.is_alive():
            process.fail_terminate_task4 = False
            client.shutdown(timeout_seconds=1.0)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls >= 1
    assert client._process is None
    assert not client.owner_thread.is_alive()


def test_cleanup_terminate_wait_timeout_escalates_to_kill():
    process = _Task4Process(stall_terminate_wait=True)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.start()

    client.shutdown(timeout_seconds=0.2)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls >= 2
    assert client._process is None
    assert not client.owner_thread.is_alive()


@pytest.mark.parametrize(
    "process_kwargs, expected_stage",
    [
        ({"stall_terminate_wait": True, "fail_kill": True}, "kill"),
        ({"stall_terminate_wait": True, "stall_final_wait": True}, "final_wait"),
    ],
)
def test_cleanup_native_refusal_has_explicit_residual_diagnostics(
    process_kwargs,
    expected_stage,
):
    process = _Task4Process(pid=9876, **process_kwargs)
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.start()

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.shutdown(timeout_seconds=0.15)

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload["stage"] == expected_stage
        assert raised.value.payload["pid"] == 9876
        assert raised.value.payload["process_generation"] == 1
        assert raised.value.payload["generation_state"] == "reaping"
        assert client._process is process
        assert client.generation_state is DbCoreGenerationState.REAPING
    finally:
        process.fail_kill = False
        process.stall_final_wait = False
        process.returncode = -9
        process.stdout.feed_eof()
        process.stderr.feed_eof()
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)


def test_cleanup_stderr_drain_cancellation_is_awaited_without_residual_task():
    process = _Task4Process()
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.start()
    stderr_task = client._stderr_task
    assert stderr_task is not None

    client.shutdown(timeout_seconds=0.5)

    assert stderr_task.done()
    assert client._stderr_task is None
    assert not client.owner_thread.is_alive()


def test_shutdown_owner_task_refusal_reports_task_diagnostics():
    process = _Task4Process()
    client = DbCoreServiceClient(executable="fake-core", process_factory=_Task3Factory([process]))
    client.start()
    release = threading.Event()

    async def resistant_task():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(0.005)

    task_future = client._submit_owner(
        resistant_task(),
        DbCoreRequestKind.READ_ONLY,
        "resistant-owner-task",
    )
    time.sleep(0.02)

    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.shutdown(timeout_seconds=0.1)

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload["stage"] == "task_drain"
        assert raised.value.payload["pending_tasks"]
        assert "resistant_task" in " ".join(raised.value.payload["pending_tasks"])
        assert client.owner_thread.is_alive()
    finally:
        release.set()
        try:
            task_future.result(timeout=0.5)
        except (concurrent.futures.CancelledError, DbCoreServiceError):
            pass
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)


def test_shutdown_loop_stop_failure_reports_residual_stage(monkeypatch):
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
    )
    loop = client.owner_loop
    original = loop.call_soon_threadsafe

    def fail_stop(callback, *args, **kwargs):
        if callback == loop.stop:
            raise RuntimeError("simulated loop stop failure")
        return original(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", fail_stop)
    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.shutdown(timeout_seconds=0.5)

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload["stage"] == "loop_stop"
        assert raised.value.payload["thread_name"] == client.owner_thread.name
        assert client.owner_thread.is_alive()
    finally:
        monkeypatch.setattr(loop, "call_soon_threadsafe", original)
        if client.owner_thread.is_alive():
            client.shutdown(timeout_seconds=1.0)


def test_shutdown_owner_join_timeout_reports_residual_stage(monkeypatch):
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: None,
    )
    owner = client.owner_thread
    original_join = owner.join

    monkeypatch.setattr(owner, "join", lambda timeout=None: None)
    try:
        with pytest.raises(DbCoreServiceError) as raised:
            client.shutdown(timeout_seconds=0.5)

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload["stage"] == "owner_join"
        assert raised.value.payload["thread_name"] == owner.name
        assert raised.value.payload["thread_ident"] == owner.ident
    finally:
        monkeypatch.setattr(owner, "join", original_join)
        original_join(timeout=1.0)
        if owner.is_alive():
            client.shutdown(timeout_seconds=1.0)
