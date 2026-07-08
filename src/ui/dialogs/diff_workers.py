"""
스키마 비교/로드 백그라운드 워커
"""
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.schema_diff import (
    SchemaComparator, SchemaExtractor, SeverityClassifier,
    SeveritySummary, VersionContext, CompareLevel
)
from src.core.db_connector import MySQLConnector
from src.core.logger import get_logger

logger = get_logger(__name__)


class SchemaCompareThread(QThread):
    """스키마 비교 백그라운드 스레드"""

    progress = pyqtSignal(str)
    # NOTE: QThread에는 인자 없는 기본 finished 시그널이 있으므로,
    # 이름을 겹치지 않게 compare_finished로 분리한다.
    compare_finished = pyqtSignal(list, object, object)  # diffs, SeveritySummary, VersionContext
    error = pyqtSignal(str)

    def __init__(self, source_connector, target_connector,
                 source_schema: str, target_schema: str,
                 compare_level: CompareLevel = CompareLevel.STANDARD):
        super().__init__()
        self.source_connector = source_connector
        self.target_connector = target_connector
        self.source_schema = source_schema
        self.target_schema = target_schema
        self.compare_level = compare_level

    def run(self):
        try:
            # MySQL 버전 감지
            self.progress.emit("MySQL 버전 확인 중...")
            version_ctx = VersionContext(
                source_version=self.source_connector.get_db_version(),
                target_version=self.target_connector.get_db_version(),
                source_version_str=self.source_connector.get_db_version_string(),
                target_version_str=self.target_connector.get_db_version_string(),
            )

            self.progress.emit("소스 스키마 추출 중...")
            source_extractor = SchemaExtractor(self.source_connector)
            source_tables = source_extractor.extract_all_tables(self.source_schema)

            self.progress.emit("타겟 스키마 추출 중...")
            target_extractor = SchemaExtractor(self.target_connector)
            target_tables = target_extractor.extract_all_tables(self.target_schema)

            self.progress.emit("스키마 비교 중...")
            comparator = SchemaComparator()
            diffs = comparator.compare_schemas(
                source_tables, target_tables, self.compare_level
            )

            # 심각도 분류
            self.progress.emit("심각도 분류 중...")
            classifier = SeverityClassifier(version_ctx)
            diffs, summary = classifier.classify(diffs)

            self.compare_finished.emit(diffs, summary, version_ctx)

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
            connector = MySQLConnector(
                host=self.host, port=self.port,
                user=self.user, password=self.password
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
