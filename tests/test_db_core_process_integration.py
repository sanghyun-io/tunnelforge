import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from src.core.db_core_client import (
    DB_CORE_STDIN_HIGH_WATER_BYTES,
    MAX_JSONL_FRAME_BYTES,
    DbCoreGenerationState,
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreServiceClient,
    DbCoreServiceError,
)


PROJECT_ROOT = Path(__file__).parents[1]
HELPER_PATH = PROJECT_ROOT / "tests" / "helpers" / "db_core_process_helper.py"
HELPER_PYTHON = getattr(sys, "_base_executable", sys.executable)


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


def _state_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _wait_for_state(path, prefix):
    matched = _wait_until(
        lambda: next(
            (line for line in _state_lines(path) if line.startswith(prefix)),
            None,
        )
    )
    assert matched is not None, "missing helper state {!r}: {}".format(
        prefix,
        _state_lines(path),
    )
    return matched


def _pid_is_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        pytest.fail(
            "OpenProcess failed for PID {} with Win32 error {}".format(
                pid,
                ctypes.get_last_error(),
            )
        )
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            pytest.fail(
                "GetExitCodeProcess failed for PID {} with Win32 error {}".format(
                    pid,
                    ctypes.get_last_error(),
                )
            )
        return exit_code.value == still_active
    finally:
        if not kernel32.CloseHandle(handle):
            pytest.fail(
                "CloseHandle failed for PID {} with Win32 error {}".format(
                    pid,
                    ctypes.get_last_error(),
                )
            )


def _assert_pid_terminal(pid):
    assert _wait_until(lambda: not _pid_is_alive(pid)), "PID {} is still alive".format(pid)


def _helper_client(tmp_path, mode, *, transitions=None, marker_path=None):
    state_path = tmp_path / "{}-state.log".format(mode)
    argv = [
        HELPER_PYTHON,
        str(HELPER_PATH),
        "--mode",
        mode,
        "--state-file",
        str(state_path),
    ]
    if marker_path is not None:
        argv.extend(["--marker-file", str(marker_path)])
    client = DbCoreServiceClient(
        executable=HELPER_PYTHON,
        process_argv=argv,
        phase_observer=(
            None
            if transitions is None
            else lambda state, generation: transitions.append((state, generation))
        ),
    )
    return client, state_path


def _start_request(client, command, payload, request_id, request_kind, timeout_seconds=5.0):
    errors = []

    def run():
        try:
            client.request_result(
                command,
                payload,
                request_id=request_id,
                request_kind=request_kind,
                timeout_seconds=timeout_seconds,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _assert_signal_or_forced_exit(process, state_path=None):
    assert process.pid is not None and process.pid > 0
    assert process.returncode is not None
    if os.name == "nt":
        assert process.returncode != 259
        return
    state = [] if state_path is None else _state_lines(state_path)
    signal_recorded = any(line.startswith("SIGNAL {} ".format(process.pid)) for line in state)
    assert signal_recorded or process.returncode in (
        -int(signal.SIGTERM),
        -int(signal.SIGKILL),
    )


def _shutdown_and_assert_zero_residual(
    client,
    pids,
    *,
    processes=(),
    request_threads=(),
):
    owner = client.owner_thread
    tracked_processes = [process for process in processes if process is not None]
    current_process = client._process
    if current_process is not None and all(
        current_process is not process for process in tracked_processes
    ):
        tracked_processes.append(current_process)
    tracked_pids = list(dict.fromkeys([
        *pids,
        *(
            process.pid
            for process in tracked_processes
            if getattr(process, "pid", None) is not None
        ),
    ]))
    failures = []

    try:
        client.shutdown(timeout_seconds=2.0)
    except BaseException as exc:
        failures.append(exc)

    if owner.is_alive() or client._process is not None:
        for process in tracked_processes:
            if getattr(process, "returncode", None) is not None:
                continue
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except BaseException as exc:
                failures.append(exc)
        if owner.is_alive():
            try:
                client.shutdown(timeout_seconds=2.0)
            except BaseException as exc:
                failures.append(exc)

    if owner.is_alive():
        try:
            client.owner_loop.call_soon_threadsafe(client.owner_loop.stop)
        except BaseException as exc:
            failures.append(exc)
        owner.join(timeout=2.0)

    for thread in request_threads:
        thread.join(timeout=2.0)

    try:
        assert client.shutdown_complete is True
        assert client._process is None
        assert client._spawn_task is None
        assert client._stderr_task is None
        assert client._request_tasks == set()
        assert not owner.is_alive()
        assert client.owner_loop.is_closed()
        assert owner not in threading.enumerate()
        assert all(not thread.is_alive() for thread in request_threads)
    except BaseException as exc:
        failures.append(exc)
    for pid in tracked_pids:
        try:
            _assert_pid_terminal(pid)
        except BaseException as exc:
            failures.append(exc)

    if failures:
        raise failures[0]


def _finalize_real_child_runner(
    client,
    pids,
    *,
    processes=(),
    request_threads=(),
):
    preserve_primary_failure = sys.exc_info()[0] is not None
    try:
        _shutdown_and_assert_zero_residual(
            client,
            pids,
            processes=processes,
            request_threads=request_threads,
        )
    except BaseException:
        if not preserve_primary_failure:
            raise


def _assert_cancel_transitions(transitions, generation):
    assert transitions == [
        (DbCoreGenerationState.POISONED, generation),
        (DbCoreGenerationState.REAPING, generation),
        (DbCoreGenerationState.CLOSED, generation),
    ]


def test_real_child_stall_cancel_signals_reaps_and_leaves_zero_residual(tmp_path):
    transitions = []
    client, state_path = _helper_client(tmp_path, "stall", transitions=transitions)
    processes = []
    pids = []
    request_threads = []
    try:
        client.start()
        process = client._process
        assert process is not None
        processes.append(process)
        pid = process.pid
        pids.append(pid)
        transitions.clear()
        request_thread, errors = _start_request(
            client,
            "schema.list",
            {},
            "real-stall",
            DbCoreRequestKind.READ_ONLY,
        )
        request_threads.append(request_thread)
        _wait_for_state(state_path, "REQUEST {} real-stall ".format(pid))

        assert client.cancel_active_request(timeout_seconds=1.0) is True
        request_thread.join(timeout=2.0)

        assert not request_thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], DbCoreServiceError)
        assert errors[0].outcome is DbCoreOutcome.FAILED
        _assert_cancel_transitions(transitions, 1)
    finally:
        _finalize_real_child_runner(
            client,
            pids,
            processes=processes,
            request_threads=request_threads,
        )

    _assert_signal_or_forced_exit(process, state_path)


def test_real_child_no_read_proves_pending_drain_kill_and_zero_residual(tmp_path):
    transitions = []
    client, state_path = _helper_client(tmp_path, "no-read", transitions=transitions)
    processes = []
    pids = []
    request_threads = []
    try:
        client.start()
        process = client._process
        assert process is not None
        processes.append(process)
        pid = process.pid
        pids.append(pid)
        _wait_for_state(state_path, "NO_READ_READY {}".format(pid))
        transitions.clear()
        payload = {"blob": "x" * (MAX_JSONL_FRAME_BYTES - 4096)}
        request_thread, errors = _start_request(
            client,
            "dump.import",
            payload,
            "real-no-read",
            DbCoreRequestKind.MUTATION,
        )
        request_threads.append(request_thread)

        def write_buffer_is_pending():
            writer = process.stdin
            transport = getattr(writer, "transport", None)
            if transport is None:
                return False
            return transport.get_write_buffer_size() > DB_CORE_STDIN_HIGH_WATER_BYTES

        assert _wait_until(write_buffer_is_pending), "stdin drain never became pending"
        assert request_thread.is_alive()
        assert client.cancel_active_request(timeout_seconds=1.0) is True
        request_thread.join(timeout=2.0)

        assert not request_thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], DbCoreServiceError)
        assert errors[0].outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
        _assert_cancel_transitions(transitions, 1)
    finally:
        _finalize_real_child_runner(
            client,
            pids,
            processes=processes,
            request_threads=request_threads,
        )

    _assert_signal_or_forced_exit(process, state_path)


@pytest.mark.parametrize("mode", ["near-limit", "oversized-scalar"])
def test_real_child_exact_public_scalar_reconstruction_and_signal_reap(tmp_path, mode):
    client, state_path = _helper_client(tmp_path, mode)
    processes = []
    pids = []
    try:
        client.start()
        process = client._process
        assert process is not None
        processes.append(process)
        pid = process.pid
        pids.append(pid)
        request_id = "real-{}".format(mode)
        result = client.request_result(
            "schema.list",
            request_id=request_id,
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=8.0,
        )

        expected = (
            "x" * (MAX_JSONL_FRAME_BYTES + 257)
            if mode == "near-limit"
            else "\U0001f642" * 300_000
        )
        assert result.payload["value"] == expected
        assert process.returncode is None
        chunk_sizes = [
            int(line.rsplit(" ", maxsplit=1)[1])
            for line in _state_lines(state_path)
            if line.startswith("CHUNK_FRAME {} ".format(pid))
        ]
        assert len(chunk_sizes) >= 2
        assert max(chunk_sizes) <= MAX_JSONL_FRAME_BYTES
        if mode == "near-limit":
            assert max(chunk_sizes) >= MAX_JSONL_FRAME_BYTES - 2_048
    finally:
        _finalize_real_child_runner(client, pids, processes=processes)

    _assert_signal_or_forced_exit(process, state_path)


def test_real_child_malicious_raw_frame_poison_reaps_and_signals(tmp_path):
    transitions = []
    client, state_path = _helper_client(
        tmp_path,
        "malicious-frame",
        transitions=transitions,
    )
    processes = []
    pids = []
    try:
        client.start()
        process = client._process
        assert process is not None
        processes.append(process)
        pid = process.pid
        pids.append(pid)
        transitions.clear()

        with pytest.raises(DbCoreServiceError) as raised:
            client.request_result(
                "schema.list",
                request_id="real-malicious-frame",
                request_kind=DbCoreRequestKind.READ_ONLY,
                timeout_seconds=5.0,
            )

        assert raised.value.code == "db_core_protocol_mismatch"
        assert raised.value.outcome is DbCoreOutcome.FAILED
        _assert_cancel_transitions(transitions, 1)
    finally:
        _finalize_real_child_runner(client, pids, processes=processes)

    _assert_signal_or_forced_exit(process, state_path)


def test_real_child_post_side_effect_terminal_encode_failure_is_indeterminate_no_retry(
    tmp_path,
):
    transitions = []
    marker_path = tmp_path / "terminal-failure.marker"
    client, state_path = _helper_client(
        tmp_path,
        "post-side-effect-encode-failure-once",
        transitions=transitions,
        marker_path=marker_path,
    )
    callbacks = []
    processes = []
    pids = []
    try:
        client.start()
        first_process = client._process
        assert first_process is not None
        processes.append(first_process)
        first_pid = first_process.pid
        pids.append(first_pid)

        with pytest.raises(DbCoreServiceError) as raised:
            client.request_result(
                "dump.import",
                request_id="side-effect-once",
                request_kind=DbCoreRequestKind.MUTATION,
                on_event=callbacks.append,
                timeout_seconds=5.0,
            )

        observed_first_pid = int(_wait_for_state(state_path, "REQUEST ").split()[1])
        assert observed_first_pid == first_pid
        assert raised.value.code == "db_core_process_died"
        assert raised.value.outcome is DbCoreOutcome.OUTCOME_INDETERMINATE
        assert [event["event"] for event in callbacks] == ["phase"]
        assert _wait_for_state(
            state_path,
            "ENCODE_FAILURE {} side-effect-once".format(first_pid),
        )
        assert not any(
            line.startswith("FRAME_AFTER_ENCODE_FAILURE {} ".format(first_pid))
            for line in _state_lines(state_path)
        )
        assert [
            line
            for line in _state_lines(state_path)
            if " side-effect-once dump.import" in line
        ] == ["REQUEST {} side-effect-once dump.import".format(first_pid)]
        assert transitions[:5] == [
            (DbCoreGenerationState.CREATING, 1),
            (DbCoreGenerationState.ACTIVE, 1),
            (DbCoreGenerationState.POISONED, 1),
            (DbCoreGenerationState.REAPING, 1),
            (DbCoreGenerationState.CLOSED, 1),
        ]
        _assert_pid_terminal(first_pid)

        recovered = client.request_result(
            "schema.list",
            request_id="fresh-after-terminal-failure",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=5.0,
        )
        second_process = client._process
        assert second_process is not None
        processes.append(second_process)
        second_pid = second_process.pid
        pids.append(second_pid)
        _wait_for_state(
            state_path,
            "REQUEST {} fresh-after-terminal-failure ".format(second_pid),
        )

        assert recovered.process_generation == 2
        assert recovered.payload["value"] == "fresh-generation"
        assert second_pid != first_pid
        assert len(
            [line for line in _state_lines(state_path) if line.startswith("REQUEST ")]
        ) == 2
        assert second_process.returncode is None
    finally:
        _finalize_real_child_runner(client, pids, processes=processes)

    _assert_signal_or_forced_exit(second_process, state_path)


@pytest.fixture
def release_core_binary():
    manifest = PROJECT_ROOT / "migration_core" / "Cargo.toml"
    metadata = subprocess.run(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(manifest),
            "--format-version",
            "1",
            "--no-deps",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
        timeout=30,
    )
    binary_name = "tunnelforge-core.exe" if os.name == "nt" else "tunnelforge-core"
    binary = Path(json.loads(metadata.stdout)["target_directory"]) / "release" / binary_name
    assert binary.is_file(), "release Rust core is missing: {}".format(binary)
    return binary


def test_release_rust_core_reconstructs_exact_oversized_utf8_plan_and_reaps(
    release_core_binary,
):
    column_name = "\U0001f642" * 180_000
    client = DbCoreServiceClient(
        executable=str(release_core_binary),
        process_argv=[str(release_core_binary)],
    )
    processes = []
    pids = []
    try:
        client.start()
        process = client._process
        assert process is not None
        processes.append(process)
        pid = process.pid
        pids.append(pid)
        result = client.request_result(
            "plan",
            {
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
            request_id="release-rust-utf8-plan",
            request_kind=DbCoreRequestKind.READ_ONLY,
            timeout_seconds=20.0,
        )
        expected_ddl = (
            'CREATE TABLE "large_names" (\n'
            '  "{}" INTEGER GENERATED BY DEFAULT AS IDENTITY NOT NULL,\n'
            '  PRIMARY KEY ("{}")\n'
            ');'
        ).format(column_name, column_name)

        assert result.payload["plan"]["ddl"] == [expected_ddl]
        assert result.process_generation == 1
    finally:
        _finalize_real_child_runner(client, pids, processes=processes)

    _assert_signal_or_forced_exit(process)
