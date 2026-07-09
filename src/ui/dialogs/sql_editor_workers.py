"""
SQL 에디터 쿼리 실행 백그라운드 워커 (자동커밋 모드 / 명시적 트랜잭션 모드)
"""
from dataclasses import dataclass
import logging
import time
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.sql_query_classifier import classify_sql_statement, statement_returns_rows

logger = logging.getLogger(__name__)

WORKER_PROGRESS_PREVIEW_LEN = 100


@dataclass
class ConnectionParams:
    engine: str
    host: str
    port: int
    user: str
    password: str
    database: str = None
    schema: str = None


def truncate_sql_preview(text, length=60) -> str:
    text = text or ""
    return text[:length] + ("..." if len(text) > length else "")


def create_sql_editor_connector(engine, host, port, user, password, database=None, schema=None):
    db_engine = normalize_db_engine(engine, port)
    return create_rust_db_connector(
        db_engine,
        host,
        port,
        user,
        password,
        database,
        schema=(schema or "") if db_engine == "postgresql" else "",
    )


def connector_from_params(params: ConnectionParams):
    return create_sql_editor_connector(
        params.engine,
        params.host,
        params.port,
        params.user,
        params.password,
        params.database,
        params.schema,
    )


def _rows_from_cursor(cursor) -> tuple[list, list]:
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    row_list = []
    for row in rows:
        if isinstance(row, dict):
            row_list.append([row.get(col) for col in columns])
        else:
            row_list.append(list(row))
    return columns, row_list


class SQLQueryWorker(QThread):
    """SQL 쿼리 실행 워커 (자동 커밋)"""
    progress = pyqtSignal(str)
    query_result = pyqtSignal(int, bool, list, list, str, int, float)  # idx, returns_rows, columns, rows, error, affected, time
    finished = pyqtSignal(bool, str)

    def __init__(self, host, port, user, password, database, queries, engine="mysql", schema=None):
        super().__init__()
        self.engine = normalize_db_engine(engine, port)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.schema = schema
        self.params = ConnectionParams(
            self.engine,
            self.host,
            self.port,
            self.user,
            self.password,
            self.database,
            self.schema,
        )
        self.queries = queries  # List of query strings

    def run(self):
        connector = None
        try:
            connector = connector_from_params(self.params)
            success, msg = connector.connect()

            if not success:
                self.finished.emit(False, f"연결 실패: {msg}")
                return

            self.progress.emit(f"✅ 연결 성공: {self.host}:{self.port}")
            connector.connection.autocommit(True)

            total_queries = len(self.queries)
            success_count = 0
            error_count = 0

            for idx, query in enumerate(self.queries):
                if self.isInterruptionRequested():
                    self.finished.emit(False, "⚠️ 실행이 취소되었습니다")
                    return

                query = query.strip()
                if not query:
                    continue

                self.progress.emit(f"📄 쿼리 {idx + 1}/{total_queries} 실행 중...")

                start_time = time.time()
                try:
                    if statement_returns_rows(query):
                        rows = []

                        def collect_batch(batch):
                            rows.extend(batch)

                        result = connector.connection.facade.execute_on_connection_streaming(
                            connector.connection.connection_id,
                            query,
                            row_batch_size=500,
                            on_batch=collect_batch,
                        )
                        columns = result.get("columns") or []
                        row_list = [[row.get(col) for col in columns] for row in rows]
                        execution_time = time.time() - start_time
                        self.query_result.emit(idx, True, columns, row_list, "", len(row_list), execution_time)
                        success_count += 1
                        continue

                    # 직접 커서 사용하여 실행
                    with connector.connection.cursor() as cursor:
                        cursor.execute(query)

                        # 행을 반환하는 statement인지 확인 (None만 비행-statement)
                        if cursor.description is not None:
                            # SELECT 결과 (0행이어도 columns == [] 로 반환됨)
                            columns, row_list = _rows_from_cursor(cursor)

                            execution_time = time.time() - start_time
                            self.query_result.emit(idx, True, columns, row_list, "", len(row_list), execution_time)
                            success_count += 1
                        else:
                            # INSERT, UPDATE, DELETE 등
                            affected = cursor.rowcount
                            connector.connection.commit()
                            execution_time = time.time() - start_time
                            self.query_result.emit(idx, False, [], [], "", affected, execution_time)
                            success_count += 1

                except Exception as e:
                    execution_time = time.time() - start_time
                    self.query_result.emit(
                        idx, statement_returns_rows(query), [], [], str(e), 0, execution_time
                    )
                    error_count += 1

            if error_count == 0:
                self.finished.emit(True, f"✅ {success_count}개 쿼리 실행 완료")
            else:
                self.finished.emit(False, f"⚠️ {success_count}개 성공, {error_count}개 실패")

        except Exception as e:
            self.finished.emit(False, f"❌ 오류: {str(e)}")

        finally:
            # 연결 정리
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    logger.debug("자동 커밋 워커 연결 정리 실패", exc_info=True)


class SQLTransactionExecutionWorker(QThread):
    """지속 트랜잭션 연결에서 쿼리를 순차 실행하는 워커.

    커밋/롤백은 이 워커가 아니라 SQLEditorDialog가 소유한 연결에서 처리한다.
    PostgreSQL은 에러 발생 시 트랜잭션 전체가 aborted 상태가 되므로 즉시 롤백하고 중단한다.
    """
    progress = pyqtSignal(int, int, str, str)  # idx, total, query_type, preview
    query_result = pyqtSignal(int, str, bool, list, list, str, int, float)  # idx, query, returns_rows, columns, rows, error, affected, time
    postgres_rolled_back = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, connection, queries, engine):
        super().__init__()
        self.connection = connection
        self.queries = queries
        self.engine = engine

    def run(self):
        total = len(self.queries)
        for idx, raw_query in enumerate(self.queries):
            if self.isInterruptionRequested():
                self.finished.emit(False, "⚠️ 실행이 취소되었습니다")
                return

            query = raw_query.strip()
            if not query:
                continue

            classification = classify_sql_statement(query)
            query_type = (classification.leading_keyword or "other").upper()
            preview = truncate_sql_preview(query, WORKER_PROGRESS_PREVIEW_LEN)
            self.progress.emit(idx, total, query_type, preview)

            start_time = time.time()
            try:
                with self.connection.cursor() as cursor:
                    cursor.execute(query)

                    if cursor.description is not None:
                        columns, row_list = _rows_from_cursor(cursor)
                        execution_time = time.time() - start_time
                        self.query_result.emit(idx, query, True, columns, row_list, "", len(row_list), execution_time)
                    else:
                        affected = cursor.rowcount
                        execution_time = time.time() - start_time
                        self.query_result.emit(idx, query, False, [], [], "", affected, execution_time)

            except Exception as e:
                execution_time = time.time() - start_time
                if self.engine == "postgresql":
                    try:
                        self.connection.rollback()
                    except Exception:
                        logger.debug("PostgreSQL 오류 후 롤백 실패", exc_info=True)
                    self.postgres_rolled_back.emit(str(e))
                    self.query_result.emit(idx, query, False, [], [], str(e), 0, execution_time)
                    self.finished.emit(False, "❌ PostgreSQL 오류로 트랜잭션이 롤백되었습니다")
                    return
                # MySQL 등: 이전 쿼리는 이미 반영되었으므로 실패만 기록하고 계속 진행
                self.query_result.emit(idx, query, False, [], [], str(e), 0, execution_time)

        self.finished.emit(True, "✅ 실행 완료")
