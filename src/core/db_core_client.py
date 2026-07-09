"""Sequential JSONL client for the long-lived Rust TunnelForge DB core process."""
import json
import re
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
    """Raised when the Rust DB core service cannot complete a request."""


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
    ):
        self.executable = executable or db_core_executable()
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stderr_tail: Deque[str] = deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None

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

    def _stderr_tail_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def _send_locked(
        self,
        command: str,
        payload: Optional[Dict[str, Any]],
        request_id: str,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Send one JSONL request and read its result. Caller must already hold `_lock`."""
        body = {
            "command": command,
            "request_id": request_id,
            "payload": payload or {},
        }
        process = self._process
        assert process is not None
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            raise DbCoreServiceError("DB core service pipes are not available")

        stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
        stdin.flush()

        while True:
            line = stdout.readline()
            if line == "":
                raise DbCoreServiceError(self._stderr_tail_text() or "DB core service stopped before a result")

            event = parse_helper_event(line)
            if event.request_id not in (None, request_id):
                continue
            if on_event:
                on_event(event.payload)
            if event.event == "result":
                return event.payload
            if event.event == "error":
                raise DbCoreServiceError(_format_error_event(event.payload))

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
            return self._send_locked(command, payload, request_id, on_event)

    def shutdown(self) -> None:
        with self._lock:
            process = self._process
            if not process:
                return
            try:
                if process.poll() is None:
                    self._send_locked("service.shutdown", None, f"py-{uuid.uuid4().hex}")
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
