"""
마이그레이션 자동 수정 위저드 Worker

백그라운드에서 수정 작업을 실행하는 QThread 워커.
"""

from PyQt6.QtCore import QThread, pyqtSignal
from typing import List, Optional

from src.core.db_connector import MySQLConnector
from src.core.migration_fix_wizard import (
    FixWizardStep, BatchFixExecutor, BatchExecutionResult
)


class FixWizardWorker(QThread):
    """수정 위저드 워커 스레드"""

    progress = pyqtSignal(str)  # 진행 메시지
    finished = pyqtSignal(bool, str, object)  # success, message, BatchExecutionResult

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        steps: List[FixWizardStep],
        dry_run: bool = True
    ):
        super().__init__()
        self.connector = connector
        self.schema = schema
        self.steps = steps
        self.dry_run = dry_run

    def run(self):
        try:
            executor = BatchFixExecutor(self.connector, self.schema)
            executor.set_progress_callback(lambda msg: self.progress.emit(msg))

            result = executor.execute_batch(self.steps, dry_run=self.dry_run)

            mode = "Dry-run" if self.dry_run else "실행"
            message = f"{mode} 완료: 성공 {result.success_count}, 실패 {result.fail_count}"

            self.finished.emit(True, message, result)

        except Exception as e:
            self.finished.emit(False, f"오류: {str(e)}", None)
