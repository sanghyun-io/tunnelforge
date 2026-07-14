"""Best-effort background delivery for anonymous error reports."""

from datetime import datetime, timezone
import weakref
from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal

from src.core.error_report_builder import build_error_report
from src.core.error_report_consent import ConsentPolicy
from src.core.error_report_transport import (
    ERROR_REPORT_RELAY_URL,
    ErrorReportTransport,
    _is_canonical_issue_url,
    is_valid_relay_url,
)
from src.core.logger import get_logger


logger = get_logger("error_reporting_worker")
_ACTIVE_ERROR_REPORT_WORKERS = set()
_SAFE_OPERATION_MESSAGES = {
    ("export", "dump.run"): "Rust DB Core export operation failed.",
    ("import", "dump.import"): "Rust DB Core import operation failed.",
}


class ErrorReportingWorker(QThread):
    report_finished = pyqtSignal(bool, str, str)

    def __init__(
        self,
        config_manager,
        *,
        operation_kind: str,
        db_engine: str,
        phase: str,
        exception: Optional[BaseException] = None,
        db_server_version=None,
        relay_url: Optional[str] = None,
    ):
        super().__init__()
        self.config_manager = config_manager
        self.operation_kind = operation_kind
        self.db_engine = db_engine
        self.phase = phase
        self.exception = exception
        self.db_server_version = db_server_version
        self.relay_url = (
            ERROR_REPORT_RELAY_URL if relay_url is None else relay_url
        )
        self._consent_token = ConsentPolicy(
            self.config_manager
        ).capture_submission_token()

    def run(self):
        try:
            policy = ConsentPolicy(self.config_manager)
            if self._consent_token is None:
                self.report_finished.emit(
                    False, "Anonymous error reporting is disabled.", ""
                )
                return
            if not policy.is_submission_token_current(self._consent_token):
                self._emit_consent_changed()
                return
            if not is_valid_relay_url(self.relay_url):
                self.report_finished.emit(
                    False, "Anonymous error reporting is unavailable.", ""
                )
                return
            safe_error_message = _SAFE_OPERATION_MESSAGES.get(
                (self.operation_kind, self.phase)
            )
            if safe_error_message is None:
                raise ValueError("Unsupported anonymous reporting operation")
            payload = build_error_report(
                self.config_manager,
                operation_kind=self.operation_kind,
                db_engine=self.db_engine,
                phase=self.phase,
                error_message=safe_error_message,
                exception=self.exception,
                db_server_version=self.db_server_version,
            )
            if not policy.is_submission_token_current(self._consent_token):
                self._emit_consent_changed()
                return
            transport = ErrorReportTransport(
                self.relay_url,
                submission_authorizer=lambda: policy.authorize_submission(
                    self._consent_token
                ),
            )
            result = transport.submit(payload)
            if result.message == "Relay request cancelled.":
                self._emit_consent_changed()
                return
            _record_last_report_attempt(
                self.config_manager, result.success is True, result.issue_url
            )
            self.report_finished.emit(
                result.success, result.message, result.issue_url
            )
        except Exception:
            logger.warning("Anonymous error report worker failed")
            self.report_finished.emit(
                False, "Anonymous error report could not be sent.", ""
            )

    def _emit_consent_changed(self):
        self.report_finished.emit(
            False, "Anonymous error reporting consent changed.", ""
        )


def _record_last_report_attempt(config_manager, submitted, issue_url):
    """Persist a compact local summary after transport submission returns."""
    try:
        safe_issue_url = (
            issue_url if _is_canonical_issue_url(issue_url) else ""
        )
        attempted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def record(settings):
            settings["error_reporting_last_attempt_status"] = (
                "submitted" if submitted else "not_sent"
            )
            settings["error_reporting_last_attempt_at"] = attempted_at
            settings["error_reporting_last_attempt_issue_url"] = safe_issue_url
            return True, None

        config_manager.mutate_app_settings(record)
    except Exception:
        logger.warning("Anonymous error report attempt status could not be saved")


class ErrorReportingMixin:
    """Isolate operation logs and retain workers through QThread cleanup."""

    def _begin_error_report_operation(self):
        """Advance the generation that may write report completion logs."""
        generation = getattr(self, "_error_report_operation_generation", 0)
        if type(generation) is not int or generation < 0:
            generation = 0
        generation += 1
        self._error_report_operation_generation = generation
        return generation

    def _start_error_report_worker(
        self,
        *,
        operation_kind: str,
        db_engine: str,
        phase: str,
        exception: Optional[BaseException] = None,
        db_server_version=None,
    ):
        worker_args = {
            "operation_kind": operation_kind,
            "db_engine": db_engine,
            "phase": phase,
        }
        if exception is not None:
            worker_args["exception"] = exception
        if db_server_version is not None:
            worker_args["db_server_version"] = db_server_version
        worker = None
        lifecycle_connected = False
        retained_workers = None
        cleanup_worker_once = None
        operation_generation = getattr(
            self, "_error_report_operation_generation", 0
        )
        try:
            worker = ErrorReportingWorker(self.config_manager, **worker_args)
            retained_workers = self._error_report_workers
            receiver_ref = weakref.ref(self)
            cleanup_complete = False

            def cleanup_worker_once():
                nonlocal cleanup_complete
                if cleanup_complete or _worker_is_running(worker):
                    return
                cleanup_complete = True
                _cleanup_error_report_worker(
                    _live_receiver(receiver_ref),
                    worker,
                    retained_workers=retained_workers,
                )

            retained_workers.append(worker)
            _ACTIVE_ERROR_REPORT_WORKERS.add(worker)

            def deliver_result(success, message, issue_url, worker=worker):
                receiver = _live_receiver(receiver_ref)
                if receiver is None:
                    return
                if getattr(
                    receiver, "_error_report_operation_generation", 0
                ) != operation_generation:
                    return
                try:
                    receiver._on_error_report_finished(
                        success, message, issue_url, worker
                    )
                except Exception:
                    logger.warning(
                        "Anonymous error report completion callback failed"
                    )

            def lifecycle_finished(worker=worker):
                if _worker_is_running(worker):
                    QTimer.singleShot(0, lifecycle_finished)
                    return
                cleanup_worker_once()

            worker.report_finished.connect(deliver_result)
            worker.finished.connect(
                lifecycle_finished,
                Qt.ConnectionType.QueuedConnection,
            )
            lifecycle_connected = True
            worker.start()
        except Exception:
            if worker is not None and not (
                lifecycle_connected and _worker_is_running(worker)
            ):
                if cleanup_worker_once is None:
                    _cleanup_error_report_worker(
                        _safe_receiver(self),
                        worker,
                        retained_workers=retained_workers,
                    )
                else:
                    cleanup_worker_once()
            logger.warning("Anonymous error report worker could not be started")
            receiver = _safe_receiver(self)
            if receiver is not None:
                try:
                    receiver._add_log(
                        "Anonymous error reporting could not be started."
                    )
                except Exception:
                    logger.warning(
                        "Anonymous error report local logging failed"
                    )

    def _on_error_report_finished(
        self, success: bool, _message: str, _issue_url: str, worker=None
    ):
        if success:
            self._add_log("Anonymous error report submitted.")
        else:
            self._add_log("Anonymous error report was not sent.")


def _safe_receiver(receiver):
    try:
        if sip.isdeleted(receiver):
            return None
    except (RuntimeError, TypeError):
        pass
    return receiver


def _live_receiver(receiver_ref):
    receiver = receiver_ref()
    return None if receiver is None else _safe_receiver(receiver)


def _worker_is_running(worker):
    try:
        return worker.isRunning() is True
    except Exception:
        return False


def _cleanup_error_report_worker(receiver, worker, retained_workers=None):
    if _worker_is_running(worker):
        return
    workers = retained_workers
    if workers is None and receiver is not None:
        try:
            workers = receiver._error_report_workers
        except Exception:
            logger.warning(
                "Anonymous error report local retention lookup failed"
            )
    if workers is not None:
        try:
            while worker in workers:
                workers.remove(worker)
        except Exception:
            logger.warning(
                "Anonymous error report local retention cleanup failed"
            )
    try:
        _ACTIVE_ERROR_REPORT_WORKERS.discard(worker)
    except Exception:
        logger.warning("Anonymous error report global retention cleanup failed")
    try:
        worker.deleteLater()
    except Exception:
        logger.warning("Anonymous error report worker cleanup failed")
