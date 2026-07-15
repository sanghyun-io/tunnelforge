import ctypes
import asyncio
import gc
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import warnings

import pytest

from src.core.db_core_client import (
    DB_CORE_STDIN_HIGH_WATER_BYTES,
    MAX_ASSEMBLED_EVENT_BYTES,
    MAX_ASSEMBLED_EVENT_CHUNKS,
    MAX_ASSEMBLED_EVENT_DEPTH,
    MAX_ASSEMBLED_EVENT_NODES,
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


def _is_asyncio_process(process):
    return isinstance(process, asyncio.subprocess.Process)


def _asyncio_process_transport_is_settled(process):
    if process.returncode is None:
        return False
    transport = getattr(process, "_transport", None)
    if transport is None or not getattr(transport, "_finished", False):
        return False
    if not transport.is_closing():
        return False
    if process.stdout is not None and not process.stdout.at_eof():
        return False
    if process.stderr is not None and not process.stderr.at_eof():
        return False
    if process.stdin is not None and not process.stdin.is_closing():
        return False
    return True


async def _settle_tracked_asyncio_process_on_owner(client, process):
    stdin = process.stdin
    stdin_close_requested = False
    if stdin is not None:
        stdin.close()
        stdin_close_requested = True
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    wait_task = asyncio.create_task(process.wait())
    await asyncio.shield(wait_task)
    stdout = process.stdout
    if stdout is not None:
        await stdout.read()
    current_process = getattr(client, "_process", None)
    stderr_task = (
        client._stderr_task
        if current_process is None or current_process is process
        else None
    )
    if stderr_task is not None:
        await asyncio.shield(stderr_task)
    elif process.stderr is not None:
        await process.stderr.read()
    if stdin is not None:
        wait_closed = getattr(stdin, "wait_closed", None)
        if callable(wait_closed):
            try:
                await wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                if not stdin_close_requested:
                    raise
    if client._stderr_task is stderr_task:
        client._stderr_task = None


def _terminate_pid_without_transport(process):
    pid = process.pid
    if not _pid_is_alive(pid):
        if isinstance(process, subprocess.Popen) and process.returncode is None:
            process.wait(timeout=2.0)
        return
    os.kill(pid, signal.SIGTERM)
    if isinstance(process, subprocess.Popen):
        process.wait(timeout=2.0)
    else:
        _assert_pid_terminal(pid)


def _shutdown_and_assert_zero_residual(
    client,
    pids,
    *,
    processes=(),
    request_threads=(),
    simulated_owner_stopped_transports=(),
):
    owner = client.owner_thread
    tracked_processes = [process for process in processes if process is not None]
    current_process_before_shutdown = client._process
    if current_process_before_shutdown is not None and all(
        current_process_before_shutdown is not process for process in tracked_processes
    ):
        tracked_processes.append(current_process_before_shutdown)
    failures = []
    asyncio_processes = [
        process
        for process in tracked_processes
        if _is_asyncio_process(process)
        and process is not current_process_before_shutdown
        and not _asyncio_process_transport_is_settled(process)
    ]
    if not owner.is_alive() or client.owner_loop.is_closed():
        asyncio_processes.extend(
            process
            for process in simulated_owner_stopped_transports
            if all(process is not tracked for tracked in asyncio_processes)
        )
    owner_stopped_asyncio_processes = set()

    if asyncio_processes:
        if owner.is_alive() and not client.owner_loop.is_closed():
            for process in asyncio_processes:
                try:
                    asyncio.run_coroutine_threadsafe(
                        _settle_tracked_asyncio_process_on_owner(client, process),
                        client.owner_loop,
                    ).result(timeout=2.0)
                    if not _asyncio_process_transport_is_settled(process):
                        raise AssertionError(
                            "tracked orphan asyncio process transport did not settle"
                        )
                except BaseException as exc:
                    failures.append(exc)
        else:
            for process in asyncio_processes:
                pid = getattr(process, "pid", None)
                try:
                    if pid is not None:
                        _terminate_pid_without_transport(process)
                except BaseException as exc:
                    failures.append(exc)
                failures.append(DbCoreServiceError(
                    "DB Core asyncio transport could not be settled after owner stop",
                    code="db_core_residual_process",
                    outcome=DbCoreOutcome.FAILED,
                    payload={
                        "stage": "transport_unsettled_owner_stopped",
                        "pid": pid,
                    },
                ))
                owner_stopped_asyncio_processes.add(process)

    try:
        client.shutdown(timeout_seconds=2.0)
    except BaseException as exc:
        failures.append(exc)

    current_process = client._process
    if current_process is not None and all(
        current_process is not process for process in tracked_processes
    ):
        tracked_processes.append(current_process)
    for process in tracked_processes:
        if (
            _is_asyncio_process(process)
            and process not in owner_stopped_asyncio_processes
            and not _asyncio_process_transport_is_settled(process)
        ):
            failures.append(AssertionError(
                "tracked asyncio process transport remained unsettled after shutdown"
            ))
    tracked_pids = list(dict.fromkeys([
        *pids,
        *(
            process.pid
            for process in tracked_processes
            if getattr(process, "pid", None) is not None
        ),
    ]))

    for process in tracked_processes:
        if process in owner_stopped_asyncio_processes:
            continue
        if getattr(process, "returncode", None) is not None:
            continue
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except BaseException as exc:
            failures.append(exc)
        try:
            if isinstance(process, subprocess.Popen):
                process.wait(timeout=2.0)
            else:
                pid = getattr(process, "pid", None)
                if pid is not None:
                    _assert_pid_terminal(pid)
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


def test_finalizer_reaps_tracked_child_with_cleared_client_and_stopped_owner():
    client = DbCoreServiceClient(
        executable="unused-core",
        process_factory=lambda *args, **kwargs: None,
    )
    client.shutdown(timeout_seconds=1.0)
    assert client._process is None
    assert not client.owner_thread.is_alive()
    child = subprocess.Popen(
        [HELPER_PYTHON, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert child.poll() is None
    cleanup_error = None
    alive_after_finalizer = True
    try:
        try:
            _shutdown_and_assert_zero_residual(
                client,
                [child.pid],
                processes=[child],
            )
        except BaseException as exc:
            cleanup_error = exc
        alive_after_finalizer = _pid_is_alive(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5.0)

    assert cleanup_error is None
    assert not alive_after_finalizer


def test_finalizer_settles_tracked_asyncio_process_on_owner_loop(tmp_path):
    client, _state_path = _helper_client(tmp_path, "stall")
    processes = []
    pids = []
    try:
        client.start()
        process = client._process
        assert isinstance(process, asyncio.subprocess.Process)
        processes.append(process)
        pids.append(process.pid)
        client._process = None

        _shutdown_and_assert_zero_residual(
            client,
            pids,
            processes=processes,
        )

        assert process.returncode is not None
    finally:
        if client.owner_thread.is_alive():
            _finalize_real_child_runner(client, pids, processes=processes)


def test_finalizer_leaves_current_asyncio_process_to_client_shutdown(
    tmp_path,
    monkeypatch,
):
    client, _state_path = _helper_client(tmp_path, "stall")
    independently_settled = []
    original_settle = _settle_tracked_asyncio_process_on_owner

    async def record_independent_settlement(owner_client, process):
        independently_settled.append(process)
        await original_settle(owner_client, process)

    monkeypatch.setattr(
        sys.modules[__name__],
        "_settle_tracked_asyncio_process_on_owner",
        record_independent_settlement,
    )
    client.start()
    process = client._process
    assert isinstance(process, asyncio.subprocess.Process)

    _shutdown_and_assert_zero_residual(
        client,
        [process.pid],
        processes=[process],
    )

    assert independently_settled == []
    assert process.returncode is not None


@pytest.mark.parametrize("terminal_error", [BrokenPipeError, ConnectionResetError])
def test_finalizer_accepts_terminal_stdin_close_after_close_requested(terminal_error):
    class _TerminalStdin:
        def __init__(self):
            self.close_requested = False

        def close(self):
            self.close_requested = True

        async def wait_closed(self):
            assert self.close_requested
            raise terminal_error("terminal pipe closure")

    class _TerminalProcess:
        def __init__(self):
            self.stdin = _TerminalStdin()
            self.stdout = None
            self.stderr = None
            self.returncode = 0

        async def wait(self):
            return self.returncode

    client = SimpleNamespace(_stderr_task=None)
    process = _TerminalProcess()

    asyncio.run(_settle_tracked_asyncio_process_on_owner(client, process))

    assert process.stdin.close_requested is True


@pytest.mark.parametrize("unrelated_error", [OSError, ResourceWarning])
def test_finalizer_does_not_suppress_unrelated_stdin_close_errors(unrelated_error):
    class _FailingStdin:
        def __init__(self):
            self.close_requested = False

        def close(self):
            self.close_requested = True

        async def wait_closed(self):
            raise unrelated_error("unrelated close failure")

    process = SimpleNamespace(
        stdin=_FailingStdin(),
        stdout=None,
        stderr=None,
        returncode=0,
        wait=lambda: asyncio.sleep(0, result=0),
    )
    client = SimpleNamespace(_stderr_task=None)

    with pytest.raises(unrelated_error, match="unrelated close failure"):
        asyncio.run(_settle_tracked_asyncio_process_on_owner(client, process))

    assert process.stdin.close_requested is True


def test_finalizer_stopped_owner_reports_transport_unsettled():
    client = DbCoreServiceClient(
        executable="unused-core",
        process_factory=lambda *args, **kwargs: None,
    )
    client.shutdown(timeout_seconds=1.0)
    process = subprocess.Popen(
        [HELPER_PYTHON, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert not isinstance(process, asyncio.subprocess.Process)
        assert process.poll() is None
        assert not client.owner_thread.is_alive()
        assert client.owner_loop.is_closed()

        with pytest.raises(DbCoreServiceError) as raised:
            _shutdown_and_assert_zero_residual(
                client,
                [process.pid],
                processes=[process],
                simulated_owner_stopped_transports=[process],
            )

        assert raised.value.code == "db_core_residual_process"
        assert raised.value.payload["stage"] == "transport_unsettled_owner_stopped"
        assert raised.value.payload["pid"] == process.pid
        assert process.returncode is not None
        _assert_pid_terminal(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Proactor subprocess pipes")
def test_proactor_terminal_child_pipe_settlement_has_no_transport_residual(
    tmp_path,
):
    child_path = tmp_path / "terminal_pipe_child.py"
    hello_contract = {
        "event": "result",
        "command": "service.hello",
        "success": True,
        "service": "tunnelforge-core",
        "protocol_version": 1,
        "process_version": 1,
        "process_capabilities": [
            "mutation.outcome_indeterminate",
            "process.generation",
            "request.deadline",
            "request.strict_id",
        ],
        "max_jsonl_frame_bytes": MAX_JSONL_FRAME_BYTES,
        "max_assembled_event_bytes": MAX_ASSEMBLED_EVENT_BYTES,
        "max_assembled_event_chunks": MAX_ASSEMBLED_EVENT_CHUNKS,
        "max_assembled_event_nodes": MAX_ASSEMBLED_EVENT_NODES,
        "max_assembled_event_depth": MAX_ASSEMBLED_EVENT_DEPTH,
    }
    child_path.write_text(
        "import json\n"
        "import sys\n"
        "request = json.loads(sys.stdin.buffer.readline())\n"
        "response = {!r}\n".format(hello_contract)
        + "response['request_id'] = request['request_id']\n"
        "sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdout.write('pending-stdout-before-eof\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('pending-stderr-before-eof\\n')\n"
        "sys.stderr.flush()\n",
        encoding="utf-8",
    )
    stderr_gate = threading.Event()
    stderr_reader_entered = threading.Event()
    stderr_reader_cancelled = threading.Event()
    stdin_wait_closed_calls = []
    stdout_read_calls = []
    unraisable = []
    cleanup_errors = []
    client = DbCoreServiceClient(
        executable=HELPER_PYTHON,
        process_argv=[HELPER_PYTHON, str(child_path)],
    )
    original_stderr_reader = client._drain_stderr_on_owner

    async def gated_stderr_reader(process):
        stderr_reader_entered.set()
        try:
            while not stderr_gate.is_set():
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            stderr_reader_cancelled.set()
            raise
        await original_stderr_reader(process)

    client._drain_stderr_on_owner = gated_stderr_reader
    process = None
    existing_wait_task = None
    stderr_task = None
    previous_unraisable_hook = sys.unraisablehook

    def capture_unraisable(unraisable_hook_args):
        unraisable.append(unraisable_hook_args)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            sys.unraisablehook = capture_unraisable
            client.start()
            process = client._process
            assert isinstance(process, asyncio.subprocess.Process)
            assert stderr_reader_entered.wait(timeout=0.5)
            assert _wait_until(lambda: process.returncode is not None)
            assert process.stdout is not None
            assert not process.stdout.at_eof()
            stderr_task = client._stderr_task
            assert stderr_task is not None

            async def install_settlement_tracking():
                nonlocal existing_wait_task
                existing_wait_task = asyncio.create_task(
                    process.wait(),
                    name="preexisting-proactor-process-wait",
                )
                client._process_wait_task = existing_wait_task
                stdin = process.stdin
                assert stdin is not None
                original_wait_closed = stdin.wait_closed
                original_stdout_read = process.stdout.read

                async def tracked_wait_closed():
                    stdin_wait_closed_calls.append(threading.get_ident())
                    return await original_wait_closed()

                async def tracked_stdout_read(*args, **kwargs):
                    stdout_read_calls.append(threading.get_ident())
                    return await original_stdout_read(*args, **kwargs)

                stdin.wait_closed = tracked_wait_closed
                process.stdout.read = tracked_stdout_read
                await asyncio.sleep(0)

            client._submit_owner(
                install_settlement_tracking(),
                DbCoreRequestKind.MUTATION,
                "install-proactor-pipe-tracking",
            ).result(timeout=0.5)

            def shutdown_client():
                try:
                    client.shutdown(timeout_seconds=1.0)
                except BaseException as exc:
                    cleanup_errors.append(exc)

            cleanup_thread = threading.Thread(target=shutdown_client)
            cleanup_thread.start()
            time.sleep(0.05)
            try:
                assert cleanup_thread.is_alive()
                assert not stderr_reader_cancelled.is_set()
            finally:
                stderr_gate.set()
                cleanup_thread.join(timeout=2.0)

            assert not cleanup_thread.is_alive()
            assert cleanup_errors == []
            assert stdin_wait_closed_calls == [client.owner_thread.ident]
            assert stdout_read_calls
            assert set(stdout_read_calls) == {client.owner_thread.ident}
            assert process.stdout.at_eof()
            assert existing_wait_task is not None
            assert existing_wait_task.done()
            assert not existing_wait_task.cancelled()
            assert stderr_task.done()
            assert not stderr_task.cancelled()
            assert client._process_wait_task is None
            assert client._stderr_task is None
            assert client._process is None
            assert client._request_tasks == set()
            assert client.generation_state is DbCoreGenerationState.CLOSED
            assert not client.owner_thread.is_alive()
            assert client.owner_thread not in threading.enumerate()
            _assert_pid_terminal(process.pid)
            gc.collect()
            assert unraisable == []
    finally:
        sys.unraisablehook = previous_unraisable_hook
        stderr_gate.set()
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if client.owner_thread.is_alive():
            _finalize_real_child_runner(
                client,
                [] if process is None else [process.pid],
                processes=[] if process is None else [process],
            )


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
