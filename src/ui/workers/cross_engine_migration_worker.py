"""Worker for the Rust cross-engine migration helper."""
import subprocess
from typing import Any, Callable, Dict, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from src.core.cross_engine_migration import (
    HelperProtocolError,
    build_helper_request,
    db_core_executable,
    parse_helper_event,
)


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

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

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
                    self.result.emit(event.payload)
                elif event.event == "error":
                    final_payload = event.payload
                    success = False
                    self.failed.emit(event.message)
                elif event.event == "phase":
                    self.phase_changed.emit(event.phase or "", event.message)
                elif event.event == "table_progress":
                    self.table_progress.emit(event.table or "", event.status or "")
                    if isinstance(event.payload.get("state"), dict):
                        self._last_checkpoint = event.payload["state"]
                        self.checkpoint.emit(event.payload["state"])
                elif event.event == "row_progress":
                    self.row_progress.emit(event.table or "", int(event.rows or 0), event.total)
                    if isinstance(event.payload.get("state"), dict):
                        self._last_checkpoint = event.payload["state"]
                        self.checkpoint.emit(event.payload["state"])
                elif event.event == "issue" and event.issue:
                    self.issue.emit(event.issue)
                else:
                    self.log_message.emit(line.rstrip())

            return_code = self._process.wait()
            if return_code != 0 and not self._cancelled:
                stderr = ""
                if self._process.stderr:
                    stderr = self._process.stderr.read().strip()
                success = False
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
            if self._cancelled:
                success = False
                final_payload = {"cancelled": True}
                if self._last_checkpoint:
                    final_payload["state"] = self._last_checkpoint
            self.finished.emit(success, final_payload)
