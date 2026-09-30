"""JSONL client for the long-lived Rust TunnelForge DB core process.

One stdout reader thread routes events by `request_id`, so several requests (for example a
running query and its `query.cancel`) can be in flight at once. The lock covers writes only."""
import json
import re
import queue
import subprocess
import threading
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from src.core.cross_engine_migration import db_core_executable, parse_helper_event
from src.core.logger import get_logger
from src.core.platform_integration import no_window_creation_flags

logger = get_logger("db_core_service")


class DbCoreServiceError(RuntimeError):
    """Raised when the Rust DB core service cannot complete a request.

    `error_code` is the stable machine-readable code from the core (for example
    `query_cancelled`); UI logic must branch on it, never on the message text.
    """

    def __init__(self, message: str = "", *, error_code: Optional[str] = None,
                 payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.error_code = error_code
        self.payload = payload or {}


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


class _Pending:
    """One in-flight request: events are queued by the reader and consumed by the caller thread."""

    __slots__ = ("events", "process")

    def __init__(self, process: Any):
        self.events: "queue.Queue[Any]" = queue.Queue()
        self.process = process


_CLOSED = object()
_UNROUTED_LIMIT = 256


class DbCoreServiceClient:
    """JSONL client for the long-lived Rust DB core process (multiplexed by `request_id`)."""

    def __init__(
        self,
        executable: Optional[str] = None,
        popen_factory: Optional[Callable[..., subprocess.Popen]] = None,
    ):
        self.executable = executable or db_core_executable()
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: Optional[subprocess.Popen] = None
        # `_lock` serializes process start/shutdown and stdin writes; it is never held while
        # waiting for a result (except by shutdown), so a cancel can overtake a running query.
        self._lock = threading.Lock()
        self._stderr_tail: Deque[str] = deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        # Events whose request is not registered yet (or unknown); claimed by the next request.
        self._unrouted: Deque[Tuple[Optional[str], Any]] = deque(maxlen=_UNROUTED_LIMIT)
        self._closed_processes: set = set()
        self._reader_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """Start the core process. Caller must already hold `_lock`."""
        if self._process and self._process.poll() is None:
            return
        try:
            process = self._popen_factory(
                [self.executable],
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
                "설치본이면 배포 패키지에 tunnelforge-core 실행 파일이 포함되어 있는지 확인하세요."
            ) from exc
        self._process = process
        with self._stderr_lock:
            self._stderr_tail.clear()
        self._start_stderr_drain_locked(process)
        self._start_reader_locked(process)

    def _start_stderr_drain_locked(self, process: subprocess.Popen) -> None:
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

    def _start_reader_locked(self, process: subprocess.Popen) -> None:
        """Spawn the single stdout reader that routes events to their requests."""
        stdout = process.stdout
        if stdout is None:
            return

        def _read() -> None:
            try:
                while True:
                    line = stdout.readline()
                    if line == "":
                        break
                    self._route_line(line)
            except (ValueError, OSError):
                pass
            self._process_closed(process)

        thread = threading.Thread(target=_read, daemon=True, name="db-core-reader")
        self._reader_thread = thread
        thread.start()

    def _route_line(self, line: str) -> None:
        try:
            event = parse_helper_event(line)
        except Exception:
            logger.warning("DB core emitted an unparsable line: %s", line[:200])
            return
        with self._pending_lock:
            pending = self._pending.get(event.request_id) if event.request_id else None
            if pending is None and event.request_id is None and len(self._pending) == 1:
                pending = next(iter(self._pending.values()))
            if pending is None:
                self._unrouted.append((event.request_id, event))
                return
        pending.events.put(event)

    def _process_closed(self, process: Any) -> None:
        """stdout reached EOF: wake every request that was waiting on this process."""
        thread = self._stderr_thread
        if thread is not None:
            thread.join(timeout=1.0)  # let the last stderr lines land in the tail
        with self._pending_lock:
            self._closed_processes.add(id(process))
            waiting = [p for p in self._pending.values() if p.process is process]
        for pending in waiting:
            pending.events.put(_CLOSED)

    def _stderr_tail_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def _register_locked(self, request_id: str) -> _Pending:
        """Register a request before writing it. Caller must hold `_lock` (process is running)."""
        process = self._process
        assert process is not None
        pending = _Pending(process)
        with self._pending_lock:
            self._pending[request_id] = pending
            keep: Deque[Tuple[Optional[str], Any]] = deque(maxlen=_UNROUTED_LIMIT)
            claimed_final = False  # id-less events belong to one request: stop after its result
            for key, event in self._unrouted:
                anonymous = key is None and len(self._pending) == 1 and not claimed_final
                if key == request_id or anonymous:
                    pending.events.put(event)
                    if key is None and event.event in ("result", "error"):
                        claimed_final = True
                else:
                    keep.append((key, event))
            self._unrouted = keep
            if id(process) in self._closed_processes:
                pending.events.put(_CLOSED)
        return pending

    def _forget(self, request_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def _write_locked(self, request_id: str, command: str, payload: Optional[Dict[str, Any]]) -> _Pending:
        body = {
            "command": command,
            "request_id": request_id,
            "payload": payload or {},
        }
        process = self._process
        assert process is not None
        stdin = process.stdin
        if stdin is None or process.stdout is None:
            raise DbCoreServiceError("DB core service pipes are not available")
        pending = self._register_locked(request_id)
        try:
            stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
            stdin.flush()
        except Exception:
            self._forget(request_id)
            raise
        return pending

    def _await(
        self,
        request_id: str,
        pending: _Pending,
        on_event: Optional[Callable[[Dict[str, Any]], None]],
    ) -> Dict[str, Any]:
        """Consume this request's events on the caller thread until its result or error."""
        try:
            while True:
                item = pending.events.get()
                if item is _CLOSED:
                    raise DbCoreServiceError(self._stderr_tail_text() or "DB core service stopped before a result")
                event = item
                if on_event:
                    on_event(event.payload)
                if event.event == "result":
                    return event.payload
                if event.event == "error":
                    raise DbCoreServiceError(
                        _format_error_event(event.payload),
                        error_code=event.payload.get("error_code"),
                        payload=event.payload,
                    )
        finally:
            self._forget(request_id)

    def request(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        request_id = request_id or f"py-{uuid.uuid4().hex}"
        with self._lock:
            self._start_locked()
            pending = self._write_locked(request_id, command, payload)
        return self._await(request_id, pending, on_event)

    def shutdown(self) -> None:
        with self._lock:
            process = self._process
            if not process:
                return
            try:
                if process.poll() is None:
                    request_id = f"py-{uuid.uuid4().hex}"
                    pending = self._write_locked(request_id, "service.shutdown", None)
                    self._await(request_id, pending, None)
            except Exception:
                process.terminate()
            finally:
                self._process = None

    def __enter__(self) -> "DbCoreServiceClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.shutdown()
        return False
