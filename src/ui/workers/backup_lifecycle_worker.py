"""백업 생명주기(restore.backups) Rust DB Core 요청 스레드"""
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_core_facade import DbCoreFacade


class BackupLifecycleWorker(QThread):
    """restore.backups 한 건을 실행한다 (list / reconcile / cleanup_plan / cleanup_apply)."""
    finished_with_result = pyqtSignal(bool, str, dict)  # success, message, result

    def __init__(self, payload: dict):
        super().__init__()
        self.payload = payload

    def run(self):
        facade = DbCoreFacade()
        try:
            result = facade.restore_backups(self.payload)
            self.finished_with_result.emit(True, "", result)
        except Exception as exc:  # DbCoreServiceError 포함: 메시지는 Core가 이미 비밀정보를 제거했다
            self.finished_with_result.emit(False, str(exc), {})
        finally:
            try:
                facade.client.shutdown()
            except Exception:
                pass
