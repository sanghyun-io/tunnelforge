"""Sequential JSONL client for the long-lived Rust TunnelForge DB core process."""
import asyncio
import concurrent.futures
import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from src.core.cross_engine_migration import (
    HelperProtocolError,
    db_core_executable,
    parse_helper_event,
)
from src.core.logger import get_logger
from src.core.platform_integration import no_window_creation_flags

logger = get_logger("db_core_service")


class DbCoreRequestKind(str, Enum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"


class DbCoreOutcome(str, Enum):
    DEFINITE = "definite"
    NOT_STARTED = "not_started"
    FAILED = "failed"
    OUTCOME_INDETERMINATE = "outcome_indeterminate"


class DbCoreGenerationState(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    POISONED = "poisoned"
    REAPING = "reaping"
    CLOSED = "closed"


MAX_JSONL_FRAME_BYTES = 1_048_576
DB_CORE_STDIN_HIGH_WATER_BYTES = 65_536
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3600.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
REQUIRED_PROCESS_CAPABILITIES = frozenset({
    "request.deadline",
    "request.strict_id",
    "process.generation",
    "mutation.outcome_indeterminate",
})


@dataclass(frozen=True)
class DbCoreRequestResult:
    request_kind: DbCoreRequestKind
    outcome: DbCoreOutcome
    request_id: str
    process_generation: int
    message: str
    rust_code: Optional[str]
    payload: Mapping[str, Any]


class DbCoreServiceError(RuntimeError):
    """Raised when the Rust DB core service cannot complete a request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "db_core_business_failure",
        request_kind: DbCoreRequestKind = DbCoreRequestKind.MUTATION,
        outcome: DbCoreOutcome = DbCoreOutcome.FAILED,
        request_id: str = "",
        process_generation: int = 0,
        rust_code: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_kind = request_kind
        self.outcome = outcome
        self.request_id = request_id
        self.process_generation = process_generation
        self.rust_code = rust_code
        self.payload = payload or {}


class DbCoreCallbackError(RuntimeError):
    """Raised when a caller callback fails while consuming DB Core events."""

    def __init__(
        self,
        cause: BaseException,
        *,
        request_kind: DbCoreRequestKind,
        outcome: DbCoreOutcome,
        request_result: Optional[DbCoreRequestResult] = None,
    ):
        super().__init__(str(cause))
        self.request_result = request_result
        self.request_kind = request_kind
        self.outcome = outcome
        self.cause = cause


def _format_error_event(payload: Dict[str, Any]) -> str:
    message = str(payload.get("message") or payload.get("error") or "DB core service error")
    details: List[str] = []
    for key, label in (
        ("code", "code"),
        ("detail", "detail"),
        ("hint", "hint"),
        ("context", "context"),
        ("table", "table"),
        ("column", "column"),
        ("constraint", "constraint"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            details.append(f"{label}={value}")
    if not details:
        return message
    return f"{message} ({'; '.join(details)})"


SUPPORTED_DB_ENGINES = {"mysql", "postgresql"}


def parse_db_version_tuple(version: Any) -> Tuple[int, int, int]:
    """Return a connector-compatible (major, minor, patch) tuple."""
    if isinstance(version, tuple):
        parts = list(version)
    elif isinstance(version, list):
        parts = version
    else:
        text = str(version or "")
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
        if not match:
            return (0, 0, 0)
        parts = [match.group(1), match.group(2) or 0, match.group(3) or 0]

    parsed = []
    for index in range(3):
        try:
            parsed.append(int(parts[index]))
        except (IndexError, TypeError, ValueError):
            parsed.append(0)
    return tuple(parsed)


def normalize_db_engine(engine: Optional[str], port: Optional[int] = None) -> str:
    """Return the Rust core engine id used by DB-facing product paths."""
    value = str(engine or "").strip().lower()
    if value in ("postgres", "postgresql", "pg"):
        return "postgresql"
    if value in ("mysql", "mariadb"):
        return "mysql"
    if int(port or 0) == 5432:
        return "postgresql"
    return "mysql"


def default_database_for_engine(engine: str, database: Optional[str] = None) -> str:
    if database:
        return database
    return "postgres" if normalize_db_engine(engine) == "postgresql" else ""


class DbCoreServiceClient:
    """Sequential JSONL client for the long-lived Rust DB core process."""

    def __init__(
        self,
        executable: Optional[str] = None,
        popen_factory: Optional[Callable[..., subprocess.Popen]] = None,
        *,
        process_argv: Optional[List[str]] = None,
        process_factory: Optional[Callable[..., subprocess.Popen]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        loop_factory: Optional[Callable[[], asyncio.AbstractEventLoop]] = None,
        phase_observer: Optional[Callable[..., None]] = None,
    ):
        if popen_factory is not None and process_factory is not None:
            raise ValueError("popen_factory and process_factory are mutually exclusive")
        self.executable = executable or db_core_executable()
        self._process_argv = list(process_argv or [self.executable])
        self._process_factory = process_factory or popen_factory or subprocess.Popen
        self._monotonic = monotonic
        self._loop_factory = loop_factory or self._default_loop_factory
        self._phase_observer = phase_observer
        self._process: Optional[subprocess.Popen] = None
        self._process_generation = 0
        self._stderr_tail: Deque[str] = deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._owner_ready = threading.Event()
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        self._owner_error: Optional[BaseException] = None
        self._owner_thread = threading.Thread(
            target=self._run_owner_loop,
            name=f"TunnelForgeDbCoreOwner-{id(self):x}",
            daemon=False,
        )
        self._owner_thread.start()
        if not self._owner_ready.wait(timeout=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS):
            raise DbCoreServiceError(
                "DB Core owner loop did not start within the bounded timeout",
                code="db_core_residual_process",
                outcome=DbCoreOutcome.NOT_STARTED,
            )
        if self._owner_loop is None or self._owner_error is not None:
            raise DbCoreServiceError(
                f"DB Core owner loop failed to start: {self._owner_error}",
                code="db_core_start_failed",
                outcome=DbCoreOutcome.NOT_STARTED,
            )

    @staticmethod
    def _default_loop_factory() -> asyncio.AbstractEventLoop:
        if sys.platform == "win32":
            return asyncio.ProactorEventLoop()
        return asyncio.SelectorEventLoop()

    @property
    def owner_thread(self) -> threading.Thread:
        return self._owner_thread

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._owner_loop
        if loop is None:
            raise RuntimeError("DB Core owner loop is unavailable")
        return loop

    def _run_owner_loop(self) -> None:
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            loop = self._loop_factory()
            self._owner_loop = loop
            asyncio.set_event_loop(loop)
            self._owner_ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._owner_error = exc
            self._owner_ready.set()
        finally:
            if loop is not None:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            asyncio.set_event_loop(None)

    @staticmethod
    def _validated_timeout(timeout_seconds: Optional[float], default: float) -> float:
        timeout = default if timeout_seconds is None else float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        return timeout

    def _owner_unavailable_error(
        self,
        request_kind: DbCoreRequestKind,
        request_id: str,
    ) -> DbCoreServiceError:
        return DbCoreServiceError(
            "DB Core owner is already shut down",
            code="db_core_start_failed",
            request_kind=request_kind,
            outcome=DbCoreOutcome.NOT_STARTED,
            request_id=request_id,
            process_generation=self._process_generation,
        )

    def _submit_owner(self, coroutine, request_kind: DbCoreRequestKind, request_id: str):
        if self._shutdown_complete or not self._owner_thread.is_alive():
            coroutine.close()
            raise self._owner_unavailable_error(request_kind, request_id)
        loop = self._owner_loop
        if loop is None or loop.is_closed():
            coroutine.close()
            raise self._owner_unavailable_error(request_kind, request_id)
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError as exc:
            coroutine.close()
            raise self._owner_unavailable_error(request_kind, request_id) from exc

    def _remaining(self, deadline_at: float) -> float:
        return max(0.0, deadline_at - self._monotonic())

    def start(self) -> None:
        request_id = f"py-{uuid.uuid4().hex}"
        deadline_at = self._monotonic() + DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        future = self._submit_owner(
            self._start_on_owner(DbCoreRequestKind.MUTATION, request_id),
            DbCoreRequestKind.MUTATION,
            request_id,
        )
        try:
            future.result(timeout=self._remaining(deadline_at))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise DbCoreServiceError(
                "DB Core process start exceeded its deadline",
                code="db_core_timeout",
                request_kind=DbCoreRequestKind.MUTATION,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc

    async def _start_on_owner(
        self,
        request_kind: DbCoreRequestKind,
        request_id: str,
    ) -> None:
        self._start_process_on_owner(request_kind, request_id)

    def _start_process_on_owner(
        self,
        request_kind: DbCoreRequestKind,
        request_id: str,
    ) -> None:
        """Start the core process on the dedicated owner thread."""
        if self._process and self._process.poll() is None:
            return
        try:
            process = self._process_factory(
                self._process_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=no_window_creation_flags(),
            )
        except FileNotFoundError as exc:
            raise DbCoreServiceError(
                "Rust DB Core 실행 파일을 찾을 수 없습니다: "
                f"{self.executable}\n"
                "소스 실행이면 `cargo build --manifest-path migration_core\\Cargo.toml --release`를 먼저 실행하고, "
                "설치본이면 배포 패키지에 tunnelforge-core 실행 파일이 포함되어 있는지 확인하세요.",
                code="db_core_start_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        except DbCoreServiceError:
            raise
        except Exception as exc:
            raise DbCoreServiceError(
                f"DB Core process failed to start: {type(exc).__name__}: {exc}",
                code="db_core_start_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc
        self._process = process
        self._process_generation += 1
        with self._stderr_lock:
            self._stderr_tail.clear()
        self._start_stderr_drain_on_owner(process)

    def _start_stderr_drain_on_owner(self, process: subprocess.Popen) -> None:
        """Spawn a background thread draining stderr so it never fills the OS pipe buffer."""
        if process.stderr is None:
            return

        def _drain() -> None:
            try:
                while True:
                    line = process.stderr.readline()
                    if line == "":
                        return
                    text = line.rstrip()
                    if not text:
                        continue
                    with self._stderr_lock:
                        self._stderr_tail.append(text[-4000:])
            except (ValueError, OSError):
                return

        thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread = thread
        thread.start()

    def _stderr_tail_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def _transport_outcome(self, request_kind: DbCoreRequestKind) -> DbCoreOutcome:
        if request_kind is DbCoreRequestKind.MUTATION:
            return DbCoreOutcome.OUTCOME_INDETERMINATE
        return DbCoreOutcome.FAILED

    def _send_on_owner(
        self,
        command: str,
        payload: Optional[Dict[str, Any]],
        request_id: str,
        request_kind: DbCoreRequestKind,
        event_queue: "queue.Queue[Tuple[Dict[str, Any], bool]]",
        required_generation: Optional[int],
    ) -> DbCoreRequestResult:
        body = {
            "command": command,
            "request_id": request_id,
            "payload": payload or {},
        }
        self._start_process_on_owner(request_kind, request_id)
        if required_generation is not None and required_generation != self._process_generation:
            raise DbCoreServiceError(
                "DB Core connection belongs to a stale process generation",
                code="db_core_stale_connection",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )
        process = self._process
        assert process is not None
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            raise DbCoreServiceError(
                "DB core service pipes are not available",
                code="db_core_write_failed",
                request_kind=request_kind,
                outcome=DbCoreOutcome.NOT_STARTED,
                request_id=request_id,
                process_generation=self._process_generation,
            )

        try:
            stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
            stdin.flush()
        except Exception as exc:
            raise DbCoreServiceError(
                f"DB Core request write failed: {type(exc).__name__}: {exc}",
                code="db_core_write_failed",
                request_kind=request_kind,
                outcome=self._transport_outcome(request_kind),
                request_id=request_id,
                process_generation=self._process_generation,
            ) from exc

        while True:
            line = stdout.readline()
            if line == "":
                raise DbCoreServiceError(
                    self._stderr_tail_text() or "DB core service stopped before a result",
                    code="db_core_process_died",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                )

            try:
                event = parse_helper_event(line)
            except HelperProtocolError as exc:
                raise DbCoreServiceError(
                    f"DB Core emitted a malformed protocol event: {exc}",
                    code="db_core_protocol_mismatch",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                ) from exc
            if event.request_id not in (None, request_id):
                raise DbCoreServiceError(
                    "DB Core response request_id did not match the active request",
                    code="db_core_request_id_mismatch",
                    request_kind=request_kind,
                    outcome=self._transport_outcome(request_kind),
                    request_id=request_id,
                    process_generation=self._process_generation,
                    payload=event.payload,
                )
            is_terminal = event.event in ("result", "error")
            event_queue.put((event.payload, is_terminal))
            if event.event == "result":
                return DbCoreRequestResult(
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.DEFINITE,
                    request_id=request_id,
                    process_generation=self._process_generation,
                    message=str(event.payload.get("message") or ""),
                    rust_code=None,
                    payload=event.payload,
                )
            if event.event == "error":
                rust_code = event.payload.get("code")
                if not isinstance(rust_code, str) or not rust_code.strip():
                    raise DbCoreServiceError(
                        "DB Core error event is missing a non-empty string code",
                        code="db_core_protocol_mismatch",
                        request_kind=request_kind,
                        outcome=self._transport_outcome(request_kind),
                        request_id=request_id,
                        process_generation=self._process_generation,
                        payload=event.payload,
                    )
                raise DbCoreServiceError(
                    _format_error_event(event.payload),
                    code="db_core_business_failure",
                    request_kind=request_kind,
                    outcome=DbCoreOutcome.FAILED,
                    request_id=request_id,
                    process_generation=self._process_generation,
                    rust_code=rust_code,
                    payload=event.payload,
                )

    async def _request_on_owner(
        self,
        command: str,
        payload: Optional[Dict[str, Any]],
        request_id: str,
        request_kind: DbCoreRequestKind,
        event_queue: "queue.Queue[Tuple[Dict[str, Any], bool]]",
        required_generation: Optional[int],
    ) -> DbCoreRequestResult:
        return self._send_on_owner(
            command,
            payload,
            request_id,
            request_kind,
            event_queue,
            required_generation,
        )

    @staticmethod
    def _infer_request_kind(command: str) -> DbCoreRequestKind:
        read_only_commands = {
            "service.hello",
            "connection.test",
            "schema.list",
            "schema.inspect",
            "schema.diff",
            "migration.plan",
            "migration.verify",
            "oneclick.derive_charset_contracts",
        }
        if command in read_only_commands:
            return DbCoreRequestKind.READ_ONLY
        return DbCoreRequestKind.MUTATION

    def _invoke_callback(
        self,
        payload: Dict[str, Any],
        on_event: Callable[[Dict[str, Any]], None],
        request_kind: DbCoreRequestKind,
        request_result: Optional[DbCoreRequestResult],
        request_error: Optional[DbCoreServiceError],
    ) -> None:
        try:
            on_event(payload)
        except BaseException as exc:
            if request_result is not None:
                outcome = request_result.outcome
            elif request_error is not None:
                outcome = request_error.outcome
            else:
                outcome = self._transport_outcome(request_kind)
            raise DbCoreCallbackError(
                exc,
                request_kind=request_kind,
                outcome=outcome,
                request_result=request_result,
            ) from exc

    def request_result(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        request_kind: DbCoreRequestKind,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout_seconds: Optional[float] = None,
        required_generation: Optional[int] = None,
    ) -> DbCoreRequestResult:
        if not isinstance(request_kind, DbCoreRequestKind):
            request_kind = DbCoreRequestKind(request_kind)
        timeout = self._validated_timeout(timeout_seconds, DEFAULT_REQUEST_TIMEOUT_SECONDS)
        deadline_at = self._monotonic() + timeout
        request_id = request_id or f"py-{uuid.uuid4().hex}"
        events: "queue.Queue[Tuple[Dict[str, Any], bool]]" = queue.Queue()
        future = self._submit_owner(
            self._request_on_owner(
                command,
                payload,
                request_id,
                request_kind,
                events,
                required_generation,
            ),
            request_kind,
            request_id,
        )
        result: Optional[DbCoreRequestResult] = None
        request_error: Optional[DbCoreServiceError] = None
        deferred_events: List[Dict[str, Any]] = []
        timed_out = False
        if on_event is not None:
            while not future.done():
                remaining = self._remaining(deadline_at)
                if remaining <= 0.0:
                    timed_out = True
                    break
                try:
                    event_payload, is_terminal = events.get(
                        timeout=min(0.05, remaining),
                    )
                except queue.Empty:
                    continue
                if is_terminal:
                    deferred_events.append(event_payload)
                else:
                    try:
                        self._invoke_callback(
                            event_payload,
                            on_event,
                            request_kind,
                            None,
                            None,
                        )
                    except DbCoreCallbackError:
                        future.cancel()
                        raise

        try:
            if timed_out:
                raise concurrent.futures.TimeoutError()
            result = future.result(timeout=self._remaining(deadline_at))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            request_error = DbCoreServiceError(
                "DB Core request exceeded its absolute deadline",
                code="db_core_timeout",
                request_kind=request_kind,
                outcome=self._transport_outcome(request_kind),
                request_id=request_id,
                process_generation=self._process_generation,
            )
            request_error.__cause__ = exc
        except DbCoreServiceError as exc:
            request_error = exc

        if on_event is not None:
            while True:
                try:
                    event_payload, _ = events.get_nowait()
                except queue.Empty:
                    break
                deferred_events.append(event_payload)
            for event_payload in deferred_events:
                self._invoke_callback(
                    event_payload,
                    on_event,
                    request_kind,
                    result,
                    request_error,
                )
        if request_error is not None:
            raise request_error
        assert result is not None
        return result

    def request_payload(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        request_kind: Optional[DbCoreRequestKind] = None,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout_seconds: Optional[float] = None,
        required_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = self.request_result(
            command,
            payload,
            request_kind=request_kind or self._infer_request_kind(command),
            request_id=request_id,
            on_event=on_event,
            timeout_seconds=timeout_seconds,
            required_generation=required_generation,
        )
        return dict(result.payload)

    def request(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        *,
        request_kind: Optional[DbCoreRequestKind] = None,
        timeout_seconds: Optional[float] = None,
        required_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.request_payload(
            command,
            payload,
            request_kind=request_kind,
            request_id=request_id,
            on_event=on_event,
            timeout_seconds=timeout_seconds,
            required_generation=required_generation,
        )

    async def _shutdown_on_owner(self, deadline_at: float) -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks(self.owner_loop)
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            _, still_pending = await asyncio.wait(
                pending,
                timeout=self._remaining(deadline_at),
            )
            if still_pending:
                raise DbCoreServiceError(
                    "DB Core owner tasks did not drain before shutdown deadline",
                    code="db_core_residual_process",
                    outcome=DbCoreOutcome.FAILED,
                    process_generation=self._process_generation,
                    payload={"pending_tasks": len(still_pending)},
                )

        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except Exception as exc:
                raise DbCoreServiceError(
                    f"DB Core process termination failed: {type(exc).__name__}: {exc}",
                    code="db_core_residual_process",
                    outcome=DbCoreOutcome.FAILED,
                    process_generation=self._process_generation,
                ) from exc
            wait = getattr(process, "wait", None)
            if callable(wait):
                try:
                    wait(timeout=self._remaining(deadline_at))
                except subprocess.TimeoutExpired as exc:
                    kill = getattr(process, "kill", None)
                    if not callable(kill):
                        raise DbCoreServiceError(
                            "DB Core process did not exit before shutdown deadline",
                            code="db_core_residual_process",
                            outcome=DbCoreOutcome.FAILED,
                            process_generation=self._process_generation,
                        ) from exc
                    kill()
                    try:
                        wait(timeout=self._remaining(deadline_at))
                    except subprocess.TimeoutExpired as final_exc:
                        raise DbCoreServiceError(
                            "DB Core process remained alive after bounded kill",
                            code="db_core_residual_process",
                            outcome=DbCoreOutcome.FAILED,
                            process_generation=self._process_generation,
                        ) from final_exc
        self._process = None

    def shutdown(
        self,
        *,
        timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        timeout = self._validated_timeout(timeout_seconds, DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
        deadline_at = self._monotonic() + timeout
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            try:
                future = self._submit_owner(
                    self._shutdown_on_owner(deadline_at),
                    DbCoreRequestKind.MUTATION,
                    "shutdown",
                )
                future.result(timeout=self._remaining(deadline_at))
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise DbCoreServiceError(
                    "DB Core owner shutdown exceeded its deadline",
                    code="db_core_residual_process",
                    outcome=DbCoreOutcome.FAILED,
                    process_generation=self._process_generation,
                ) from exc

            loop = self._owner_loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
            self._owner_thread.join(timeout=self._remaining(deadline_at))
            if self._owner_thread.is_alive():
                raise DbCoreServiceError(
                    "DB Core owner thread remained alive after bounded join",
                    code="db_core_residual_process",
                    outcome=DbCoreOutcome.FAILED,
                    process_generation=self._process_generation,
                    payload={"thread_name": self._owner_thread.name},
                )
            self._shutdown_complete = True

    def __enter__(self) -> "DbCoreServiceClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.shutdown()
        return False
