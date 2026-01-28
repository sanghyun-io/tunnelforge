"""
메타데이터 비동기 로딩 워커

UI 블로킹 없이 DB 스키마/테이블 목록을 백그라운드에서 로딩
"""
from PyQt6.QtCore import QThread, pyqtSignal
from typing import List, Optional

from src.core.db_connector import MySQLConnector


class MetadataWorker(QThread):
    """스키마/테이블 메타데이터 비동기 로딩 워커

    UI 스레드 블로킹 없이 DB 메타데이터를 로딩하여
    즉각적인 다이얼로그 응답성 제공
    """

    # 시그널 정의
    schemas_loaded = pyqtSignal(list)  # 스키마 목록 로딩 완료
    tables_loaded = pyqtSignal(str, list)  # (스키마명, 테이블 목록) 로딩 완료
    columns_loaded = pyqtSignal(str, str, list)  # (스키마명, 테이블명, 컬럼 목록)
    error = pyqtSignal(str)  # 오류 발생
    started_loading = pyqtSignal()  # 로딩 시작
    finished_loading = pyqtSignal()  # 로딩 완료

    def __init__(
        self,
        connector: MySQLConnector,
        task: str,
        schema: str = None,
        table: str = None,
        use_cache: bool = True
    ):
        """
        Args:
            connector: MySQLConnector 인스턴스
            task: 수행할 작업 ('schemas', 'tables', 'columns')
            schema: 스키마명 (tables/columns 작업 시 필요)
            table: 테이블명 (columns 작업 시 필요)
            use_cache: 캐시 사용 여부 (기본 True)
        """
        super().__init__()
        self.connector = connector
        self.task = task
        self.schema = schema
        self.table = table
        self.use_cache = use_cache

    def run(self):
        """백그라운드에서 메타데이터 로딩 실행"""
        self.started_loading.emit()

        try:
            if self.task == 'schemas':
                schemas = self.connector.get_schemas(use_cache=self.use_cache)
                self.schemas_loaded.emit(schemas)

            elif self.task == 'tables':
                if not self.schema:
                    self.error.emit("스키마명이 필요합니다.")
                    return
                tables = self.connector.get_tables(
                    self.schema,
                    use_cache=self.use_cache
                )
                self.tables_loaded.emit(self.schema, tables)

            elif self.task == 'columns':
                if not self.schema or not self.table:
                    self.error.emit("스키마명과 테이블명이 필요합니다.")
                    return
                columns = self.connector.get_table_columns(
                    self.table,
                    schema=self.schema
                )
                self.columns_loaded.emit(self.schema, self.table, columns)

            else:
                self.error.emit(f"알 수 없는 작업: {self.task}")

        except Exception as e:
            self.error.emit(f"메타데이터 로딩 오류: {str(e)}")
        finally:
            self.finished_loading.emit()


class BatchMetadataWorker(QThread):
    """여러 스키마의 테이블을 일괄 로딩하는 워커

    Export/Import 다이얼로그에서 여러 스키마의 테이블 목록을
    한 번에 로딩할 때 사용
    """

    # 시그널 정의
    schema_tables_loaded = pyqtSignal(str, list)  # 각 스키마별 테이블 로딩 완료
    all_loaded = pyqtSignal(dict)  # 전체 로딩 완료 {schema: [tables]}
    progress = pyqtSignal(int, int, str)  # (현재, 전체, 현재 스키마명)
    error = pyqtSignal(str)

    def __init__(
        self,
        connector: MySQLConnector,
        schemas: List[str],
        use_cache: bool = True
    ):
        """
        Args:
            connector: MySQLConnector 인스턴스
            schemas: 로딩할 스키마 목록
            use_cache: 캐시 사용 여부
        """
        super().__init__()
        self.connector = connector
        self.schemas = schemas
        self.use_cache = use_cache
        self._stop_requested = False

    def stop(self):
        """로딩 중단 요청"""
        self._stop_requested = True

    def run(self):
        """백그라운드에서 일괄 테이블 로딩 실행"""
        result = {}
        total = len(self.schemas)

        try:
            for idx, schema in enumerate(self.schemas):
                if self._stop_requested:
                    break

                self.progress.emit(idx + 1, total, schema)

                tables = self.connector.get_tables(
                    schema,
                    use_cache=self.use_cache
                )
                result[schema] = tables
                self.schema_tables_loaded.emit(schema, tables)

            self.all_loaded.emit(result)

        except Exception as e:
            self.error.emit(f"일괄 메타데이터 로딩 오류: {str(e)}")


class ConnectionTestWorkerAsync(QThread):
    """비동기 연결 테스트 워커

    DB 연결 테스트를 백그라운드에서 수행
    """

    # 시그널 정의
    connection_result = pyqtSignal(bool, str)  # (성공여부, 메시지)

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str = None
    ):
        """
        Args:
            host: MySQL 호스트
            port: MySQL 포트
            user: MySQL 사용자
            password: MySQL 비밀번호
            database: 데이터베이스명 (선택)
        """
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database

    def run(self):
        """백그라운드에서 연결 테스트 실행"""
        connector = None
        try:
            connector = MySQLConnector(
                self.host,
                self.port,
                self.user,
                self.password,
                self.database,
                use_cache=False  # 테스트 시 캐시 사용 안함
            )
            success, msg = connector.connect()
            self.connection_result.emit(success, msg)
        except Exception as e:
            self.connection_result.emit(False, str(e))
        finally:
            if connector:
                connector.disconnect()
