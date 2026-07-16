"""Worker for the Rust cross-engine migration helper."""
import math
import re
import subprocess
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from src.core.cross_engine_migration import (
    HelperProtocolError,
    build_helper_request,
    db_core_executable,
    parse_helper_event,
)

_STDERR_TAIL_LINES = 200
_STDERR_LINE_CHARS = 4000
_PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 2.0
# Best-effort scrub for connection-secret shaped substrings (password/token/etc.)
# that the helper may echo into stderr on panic, since payloads carry DB credentials.
_SECRET_PATTERN = re.compile(
    r'(?i)(\b(?:password|passwd|pwd|secret|token|api_key|access_token|refresh_token)\b\s*"?\s*[:=]\s*"?)'
    r'([^",}\s]+)'
)


class ProcessCleanupError(RuntimeError):
    """Raised when bounded child cleanup cannot prove process reaping."""

    def __init__(self, stage: str, cause: BaseException):
        self.stage = stage
        self.cause = cause
        super().__init__(
            "tunnelforge-core process cleanup residual "
            f"at {stage}: {type(cause).__name__}: {cause}"
        )


def _redact_stderr_line(text: str) -> str:
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}[REDACTED]", text)


class CrossEngineMigrationWorker(QThread):
    """Run tunnelforge-core and translate JSONL events into Qt signals."""

    phase_changed = pyqtSignal(str, str)  # phase, message
    table_progress = pyqtSignal(str, str)  # table, status
    row_progress = pyqtSignal(str, int, object)  # table, rows, total
    checkpoint = pyqtSignal(object)
    issue = pyqtSignal(object)  # MigrationIssue
    log_message = pyqtSignal(str)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal(bool, object)  # success, result payload

    def __init__(
        self,
        command: str,
        payload: Dict[str, Any],
        helper_path: Optional[str] = None,
        request_id: Optional[str] = None,
        popen_factory: Optional[Callable[..., subprocess.Popen]] = None,
    ):
        super().__init__()
        self.command = command
        self.payload = payload
        self.helper_path = helper_path or db_core_executable()
        self.request_id = request_id
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._process_lock = threading.Lock()
        self._process_cleanup_error: Optional[ProcessCleanupError] = None
        self._last_checkpoint: Optional[Dict[str, Any]] = None
        self._stderr_tail: Deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._failure_emitted = False
        self._terminal_emitted = False

    def cancel(self):
        with self._process_lock:
            self._cancelled = True
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass

    def _start_stderr_drain(self) -> None:
        """Drain stderr on a background thread so a chatty helper never fills
        the OS pipe buffer and deadlocks the stdout read loop below."""
        process = self._process
        if process is None or process.stderr is None:
            return

        def _drain() -> None:
            try:
                while True:
                    line = process.stderr.readline()
                    if line == "":
                        return
                    text = _redact_stderr_line(line.rstrip())
                    if not text:
                        continue
                    with self._stderr_lock:
                        self._stderr_tail.append(text[-_STDERR_LINE_CHARS:])
            except (ValueError, OSError):
                return

        thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread = thread
        thread.start()

    def _join_stderr_drain(self, timeout: float = 2.0) -> None:
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=timeout)

    @staticmethod
    def _close_process_streams(process) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _terminate_and_reap_process(process, timeout_seconds: float) -> None:
        try:
            if process.poll() is not None:
                return
        except (OSError, ValueError):
            pass

        try:
            process.terminate()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        except (OSError, ValueError):
            pass

        try:
            process.kill()
        except (OSError, ValueError) as exc:
            raise ProcessCleanupError("kill", exc) from exc
        try:
            process.wait(timeout=timeout_seconds)
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            raise ProcessCleanupError("final_wait", exc) from exc

    def retry_process_cleanup(
        self,
        *,
        timeout_seconds: float = _PROCESS_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> bool:
        """Retry bounded cleanup for a retained cross-engine helper process."""
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        with self._process_lock:
            process = self._process
        if process is None:
            return False

        try:
            self._terminate_and_reap_process(process, timeout)
        except ProcessCleanupError as exc:
            self._process_cleanup_error = exc
            raise

        self._close_process_streams(process)
        self._join_stderr_drain(timeout)
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            error = ProcessCleanupError(
                "stderr_drain",
                TimeoutError("stderr drain did not settle after confirmed process reap"),
            )
            self._process_cleanup_error = error
            raise error
        with self._process_lock:
            if self._process is process:
                self._process = None
        self._process_cleanup_error = None
        return True

    def _stderr_tail_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def _emit_failed_once(self, message: str) -> None:
        if self._failure_emitted:
            return
        self._failure_emitted = True
        self.failed.emit(message)

    def _emit_finished_once(self, success: bool, payload: object) -> None:
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self.finished.emit(success, payload)

    def run(self):
        if self._terminal_emitted:
            return
        with self._process_lock:
            cancelled = self._cancelled
        if cancelled:
            self._emit_finished_once(False, {"cancelled": True})
            return
        final_payload = None
        success = False

        try:
            process = self._popen_factory(
                [self.helper_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self._process_lock:
                self._process = process
                cancelled = self._cancelled
            if cancelled:
                return
            self._start_stderr_drain()

            request = build_helper_request(self.command, self.payload, self.request_id)
            with self._process_lock:
                if self._cancelled:
                    return
                assert process.stdin is not None
                process.stdin.write(request)
                process.stdin.close()

            assert process.stdout is not None
            for line in process.stdout:
                if self._cancelled:
                    break
                event = parse_helper_event(line)
                if event.event == "result":
                    final_payload = event.payload
                    success = bool(event.success)
                elif event.event == "error":
                    final_payload = event.payload
                    success = False
                self._dispatch_event(event)

            return_code = process.wait()
            self._join_stderr_drain()
            if return_code != 0 and not self._cancelled:
                success = False
                stderr = self._stderr_tail_text()
                final_payload = {"error": stderr or f"tunnelforge-core exited with {return_code}"}
                self._emit_failed_once(final_payload["error"])

        except HelperProtocolError as exc:
            final_payload = {"error": str(exc)}
            if not self._cancelled:
                self._emit_failed_once(str(exc))
        except FileNotFoundError:
            final_payload = {"error": f"tunnelforge-core helper not found: {self.helper_path}"}
            if not self._cancelled:
                self._emit_failed_once(final_payload["error"])
        except Exception as exc:
            final_payload = {"error": str(exc)}
            if not self._cancelled:
                self._emit_failed_once(str(exc))
        finally:
            cleanup_error = None
            try:
                self.retry_process_cleanup()
            except ProcessCleanupError as exc:
                cleanup_error = exc
            if self._cancelled:
                success = False
                final_payload = {"cancelled": True}
                if self._last_checkpoint:
                    final_payload["state"] = self._last_checkpoint
            if cleanup_error is not None:
                success = False
                message = str(cleanup_error)
                final_payload = {
                    "error": message,
                    "cleanup_residual": True,
                }
                self._emit_failed_once(message)
            self._emit_finished_once(success, final_payload)

    def _dispatch_event(self, event) -> bool:
        if event.event == "result":
            self.result.emit(event.payload)
            return True
        if event.event == "error":
            self._emit_failed_once(event.message)
            return True
        if event.event == "phase":
            self.phase_changed.emit(event.phase or "", event.message)
            return False
        if event.event == "table_progress":
            self.table_progress.emit(event.table or "", event.status or "")
            self._emit_checkpoint_if_present(event)
            return False
        if event.event == "row_progress":
            self.row_progress.emit(event.table or "", int(event.rows or 0), event.total)
            self._emit_checkpoint_if_present(event)
            return False
        if event.event == "issue" and event.issue:
            self.issue.emit(event.issue)
            return False
        self.log_message.emit(event.raw_line)
        return False

    def _emit_checkpoint_if_present(self, event) -> None:
        if isinstance(event.payload.get("state"), dict):
            self._last_checkpoint = event.payload["state"]
            self.checkpoint.emit(event.payload["state"])
