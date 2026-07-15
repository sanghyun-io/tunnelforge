import asyncio
import io
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
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.stderr = io.StringIO()
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


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


def test_progress_callback_runs_before_terminal_result_is_released():
    callback_seen = threading.Event()
    release_terminal = threading.Event()
    callback_was_live = []

    class _BlockingStdout:
        def __init__(self):
            self._reads = 0

        def readline(self):
            self._reads += 1
            if self._reads == 1:
                return '{"event":"phase","phase":"inspect","message":"started"}\n'
            release_terminal.wait(timeout=2.0)
            return '{"event":"result","command":"service.hello","success":true}\n'

    process = _Process([])
    process.stdout = _BlockingStdout()
    client = DbCoreServiceClient(
        executable="fake-core",
        process_factory=lambda *args, **kwargs: process,
    )

    def release_after_callback():
        callback_was_live.append(callback_seen.wait(timeout=0.5))
        release_terminal.set()

    releaser = threading.Thread(target=release_after_callback)
    releaser.start()
    result = client.request_result(
        "service.hello",
        request_kind=DbCoreRequestKind.READ_ONLY,
        on_event=lambda event: callback_seen.set(),
    )
    releaser.join(timeout=1.0)

    assert result.outcome is DbCoreOutcome.DEFINITE
    assert callback_was_live == [True]


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
