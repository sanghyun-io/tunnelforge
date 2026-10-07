"""
스키마 비교/로드 백그라운드 워커
"""
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_core_facade import DbEndpoint, get_shared_db_core_facade
from src.core.db_core_dbapi_shim import create_rust_db_connector
from src.core.schema_diff import CompareLevel, parse_compare_result
from src.core.logger import get_logger

logger = get_logger(__name__)


class SchemaCompareThread(QThread):
    """스키마 비교 백그라운드 스레드 (비교 자체는 Rust core schema.compare 가 수행)"""

    progress = pyqtSignal(str)
    # NOTE: QThread에는 인자 없는 기본 finished 시그널이 있으므로,
    # 이름을 겹치지 않게 compare_finished로 분리한다.
    compare_finished = pyqtSignal(object)  # CompareResult
    error = pyqtSignal(str)

    def __init__(self, source: DbEndpoint, target: DbEndpoint,
                 compare_level: CompareLevel = CompareLevel.STANDARD,
                 exact_row_counts: bool = False, facade=None):
        super().__init__()
        self.source = source
        self.target = target
        self.compare_level = compare_level
        self.exact_row_counts = exact_row_counts
        self._facade = facade

    def _on_event(self, event: dict):
        if event.get("event") == "progress" and event.get("message"):
            self.progress.emit(str(event["message"]))

    def run(self):
        try:
            facade = self._facade or get_shared_db_core_facade()
            result = facade.compare_schemas(
                self.source,
                self.target,
                level=self.compare_level.value,
                exact_row_counts=self.exact_row_counts,
                on_event=self._on_event,
            )
            self.compare_finished.emit(parse_compare_result(result))
        except Exception as e:
            self.error.emit(str(e))


class SchemaLoadThread(QThread):
    """스키마 목록 조회 백그라운드 스레드

    DB 연결/조회/해제가 UI 스레드를 블로킹하지 않도록 별도 스레드에서 수행한다.
    """

    loaded = pyqtSignal(str, list)       # side, schema_names
    load_failed = pyqtSignal(str, str)   # side, display_message

    def __init__(self, side: str, host: str, port: int,
                 user: str, password: str):
        super().__init__()
        self.side = side
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def run(self):
        connector = None
        try:
            connector = create_rust_db_connector(
                "mysql", self.host, self.port, self.user, self.password
            )

            success, _ = connector.connect()
            if not success:
                self.load_failed.emit(self.side, "(연결 실패)")
                return

            schemas = connector.get_schemas(use_cache=False)
            self.loaded.emit(self.side, list(schemas))

        except Exception as e:
            logger.error(f"스키마 로드 실패: {e}")
            self.load_failed.emit(self.side, "(오류)")
        finally:
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass
