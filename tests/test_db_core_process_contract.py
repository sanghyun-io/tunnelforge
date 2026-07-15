import asyncio
import concurrent.futures
import io
import inspect
import json
import math
import sys
import threading
import time

import pytest

import src.core.db_core_facade as db_core_facade
from src.core.db_core_service import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DbCoreCallbackError,
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
        self.stdin = _AsyncTextWriter()
        self.stdout = _AsyncTextReader("\n".join(lines) + "\n")
        self.stderr = _AsyncTextReader("")
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class _AsyncTextWriter:
    def __init__(self):
        self._buffer = io.StringIO()

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return self._buffer.write(data)

    async def drain(self):
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
        def __init__(self):
            self._reads = 0

        async def readline(self):
            self._reads += 1
            if self._reads == 1:
                return '{"event":"phase","phase":"inspect","message":"started"}\n'
            terminal_read_before_callback_finished.append(not callback_finished.is_set())
            return '{"event":"result","command":"service.hello","success":true}\n'

    process = _Process([])
    process.stdout = _RaceStdout()
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
        def __init__(self):
            self._reads = 0

        async def readline(self):
            self._reads += 1
            if self._reads == 1:
                return '{"event":"phase","phase":"inspect","message":"started"}\n'
            terminal_reads.append(True)
            return '{"event":"result","command":"service.hello","success":true}\n'

    process = _Process([])
    process.stdout = _RaceStdout()
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
        async def readline(self):
            read_entered.set()
            while not release_read.is_set():
                await asyncio.sleep(0.005)
            return ""

    class _CancelableProcess(_Process):
        def __init__(self):
            super().__init__([])
            self.stdout = _BlockingStdout()

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
        async def readline(self):
            read_entered.set()
            while not release_read.is_set():
                await asyncio.sleep(0.005)
            return ""

    class _CancelableProcess(_Process):
        def __init__(self):
            super().__init__([])
            self.stdout = _BlockingStdout()

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
