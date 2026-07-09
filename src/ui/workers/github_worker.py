"""GitHub 이슈 자동 보고 백그라운드 워커"""
from typing import Dict, Optional

from PyQt6.QtCore import QThread, pyqtSignal


class GitHubReportWorker(QThread):
    """GitHub 이슈 보고를 백그라운드에서 수행하는 워커"""
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(self, config_manager, error_type: str, error_message: str,
                 context: Optional[Dict] = None):
        super().__init__()
        self.config_manager = config_manager
        self.error_type = error_type
        self.error_message = error_message
        self.context = context

    def run(self):
        try:
            from src.core.github_issue_reporter import get_reporter_from_config

            reporter = get_reporter_from_config(self.config_manager)
            if not reporter:
                return  # 자동 보고 비활성화 또는 설정 미완료

            success, result_msg = reporter.report_error(
                self.error_type, self.error_message, self.context
            )
            self.finished.emit(success, result_msg)

        except Exception as e:
            self.finished.emit(False, str(e))


class GithubReportingMixin:
    """Mixin for dialogs that retain GitHub report workers until completion."""

    def _start_github_report_worker(self, error_type: str, message: str, context: Optional[Dict] = None):
        worker = GitHubReportWorker(self.config_manager, error_type, message, context)
        self._github_workers.append(worker)
        worker.finished.connect(
            lambda success, report_message, worker=worker: self._on_github_report_finished(
                success, report_message, worker
            )
        )
        worker.start()

    def _on_github_report_finished(self, success: bool, message: str, worker=None):
        """GitHub 이슈 보고 완료 콜백"""
        if success:
            self._add_log(f"🐙 GitHub: {message}")
        else:
            self._add_log(f"⚠️ GitHub 이슈 보고 실패: {message}")
        if worker is not None and worker in self._github_workers:
            self._github_workers.remove(worker)
        if worker is not None:
            worker.deleteLater()
