"""Worker for the Rust cross-engine migration helper."""
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
# Best-effort scrub for connection-secret shaped substrings (password/token/etc.)
# that the helper may echo into stderr on panic, since payloads carry DB credentials.
_SECRET_PATTERN = re.compile(
    r'(?i)(\b(?:password|passwd|pwd|secret|token|api_key|access_token|refresh_token)\b\s*"?\s*[:=]\s*"?)'
    r'([^",}\s]+)'
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
        self._last_checkpoint: Optional[Dict[str, Any]] = None
        self._stderr_tail: Deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

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

    def _stderr_tail_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def run(self):
        final_payload = None
        success = False

        try:
            self._process = self._popen_factory(
                [self.helper_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._start_stderr_drain()

            request = build_helper_request(self.command, self.payload, self.request_id)
            assert self._process.stdin is not None
            self._process.stdin.write(request)
            self._process.stdin.close()

            assert self._process.stdout is not None
            for line in self._process.stdout:
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

            return_code = self._process.wait()
            self._join_stderr_drain()
            if return_code != 0 and not self._cancelled:
                success = False
                stderr = self._stderr_tail_text()
                final_payload = {"error": stderr or f"tunnelforge-core exited with {return_code}"}
                self.failed.emit(final_payload["error"])

        except HelperProtocolError as exc:
            final_payload = {"error": str(exc)}
            self.failed.emit(str(exc))
        except FileNotFoundError:
            final_payload = {"error": f"tunnelforge-core helper not found: {self.helper_path}"}
            self.failed.emit(final_payload["error"])
        except Exception as exc:
            final_payload = {"error": str(exc)}
            self.failed.emit(str(exc))
        finally:
            self._join_stderr_drain()
            if self._cancelled:
                success = False
                final_payload = {"cancelled": True}
                if self._last_checkpoint:
                    final_payload["state"] = self._last_checkpoint
            self.finished.emit(success, final_payload)

    def _dispatch_event(self, event) -> bool:
        if event.event == "result":
            self.result.emit(event.payload)
            return True
        if event.event == "error":
            self.failed.emit(event.message)
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
