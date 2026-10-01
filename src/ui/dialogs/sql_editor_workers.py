"""
SQL 에디터 쿼리 실행 백그라운드 워커 (자동커밋 모드 / 명시적 트랜잭션 모드)
"""
from dataclasses import dataclass
import logging
import threading
import time
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.query_limits import (
    CANCEL_ERROR_CODES,
    build_query_limits,
    new_job_id,
    truncation_notice,
)
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
    read_only: bool = False  # TF-STATUS-128: production windows open read-only sessions


def truncate_sql_preview(text, length=60) -> str:
    text = text or ""
    return text[:length] + ("..." if len(text) > length else "")


def create_sql_editor_connector(engine, host, port, user, password, database=None, schema=None,
                                read_only=False):
    db_engine = normalize_db_engine(engine, port)
    return create_rust_db_connector(
        db_engine,
        host,
        port,
        user,
        password,
        database,
        schema=(schema or "") if db_engine == "postgresql" else "",
        read_only=read_only,
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
        read_only=params.read_only,
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


class StreamFlow:
    """Backpressure between a streaming worker and the GUI that renders its rows.

    The worker calls `produced(n)` after emitting a batch and blocks while more than `limit_rows`
    emitted rows are still waiting to be appended; the GUI calls `consumed(n)` after it appended
    them. This bounds the queued signals (each one converts its rows into Qt variants on the GUI
    thread), so a fast core cannot stall the event loop. Rows are never dropped or reordered.
    The wait ends early when `should_stop()` turns true (cancel / window close) or, as a last
    resort, when the GUI made no progress for `stall_seconds`.
    """

    def __init__(self, limit_rows: int = 5000, stall_seconds: float = 30.0):
        self.limit_rows = limit_rows
        self.stall_seconds = stall_seconds
        self._cond = threading.Condition()
        self._pending = 0
        self._last_progress = time.monotonic()

    @property
    def pending(self) -> int:
        with self._cond:
            return self._pending

    def produced(self, count: int, should_stop=None) -> None:
        with self._cond:
            if self._pending == 0:
                self._last_progress = time.monotonic()
            self._pending += count
            while self._pending > self.limit_rows:
                if should_stop is not None and should_stop():
                    return
                if time.monotonic() - self._last_progress > self.stall_seconds:
                    return
                self._cond.wait(0.05)

    def consumed(self, count: int) -> None:
        with self._cond:
            self._pending = max(0, self._pending - count)
            self._last_progress = time.monotonic()
            self._cond.notify_all()


def run_streaming_query(connection, query, limits, on_started, on_rows, on_progress=None,
                        flow=None, should_stop=None):
    """Stream one row-returning statement on `connection` without collecting it.

    `on_started(columns)` fires once, before the first row; `on_rows(rows)` gets each batch as
    lists in column order (converted on the worker thread). With `flow`, the stream waits for the
    GUI to keep up; once `should_stop()` is true later batches are no longer forwarded.
    Returns (columns, row_count, result).
    """
    state = {"columns": None, "count": 0}

    def start(columns):
        if state["columns"] is None:
            state["columns"] = list(columns)
            on_started(state["columns"])

    def on_batch(batch):
        if should_stop is not None and should_stop():
            return  # cancelled: the core is being stopped, forward nothing more
        if state["columns"] is None:
            start(list(batch[0].keys()) if batch else [])
        columns = state["columns"]
        rows = [[row.get(column) for column in columns] for row in batch]
        state["count"] += len(rows)
        on_rows(rows)
        if on_progress:
            on_progress(state["count"])
        if flow is not None:
            flow.produced(len(rows), should_stop)

    job_id = new_job_id()
    connection.current_job_id = job_id
    try:
        result = connection.facade.execute_on_connection_streaming(
            connection.connection_id,
            query,
            row_batch_size=500,
            on_batch=on_batch,
            on_columns=start,
            job_id=job_id,
            **limits,
        )
    finally:
        connection.current_job_id = None
    columns = result.get("columns") or state["columns"] or []
    return columns, state["count"], result


def _cancel_running_query(connection) -> None:
    """연결에서 실행 중인 쿼리를 서버 측에서 취소 (실패해도 UI 흐름은 유지)."""
    cancel = getattr(connection, "cancel_running_query", None)
    if cancel is None:
        return
    try:
        cancel()
    except Exception:
        logger.warning("query cancel request to server failed", exc_info=True)


def _cancelled_transaction_message(engine, error) -> str:
    in_tx = (getattr(error, "payload", None) or {}).get("in_transaction")
    if engine == "postgresql":
        state = "트랜잭션이 중단(aborted) 상태입니다. 롤백을 실행하세요" if in_tx else "트랜잭션 상태를 확인하세요"
    elif in_tx is False:
        state = "열린 트랜잭션이 없습니다"
    else:
        state = "MySQL 트랜잭션은 유지됩니다. 커밋 또는 롤백을 선택하세요"
    return f"⚠️ 쿼리가 취소되었습니다 - {state}"


class SQLQueryWorker(QThread):
    """SQL 쿼리 실행 워커 (자동 커밋)"""
    progress = pyqtSignal(str)
    query_result = pyqtSignal(int, bool, list, list, str, int, float)  # idx, returns_rows, columns, rows, error, affected, time
    rows_progress = pyqtSignal(int, int)  # idx, 지금까지 받은 행 수 (스트리밍 진행)
    result_truncated = pyqtSignal(int, str)  # idx, 안내 메시지 (상한 도달로 결과가 잘림)
    result_started = pyqtSignal(int, list)  # idx, columns - 첫 행이 오기 전 (증분 표시 시작)
    result_rows = pyqtSignal(int, list)  # idx, 행 배치(컬럼 순서 리스트의 리스트)
    finished = pyqtSignal(bool, str)

    def __init__(self, host, port, user, password, database, queries, engine="mysql", schema=None,
                 limits=None, read_only=False):
        super().__init__()
        self.limits = dict(limits) if limits is not None else build_query_limits()
        self.stream_flow = StreamFlow()  # GUI backpressure for streamed rows
        self._connector = None
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
            read_only=read_only,
        )
        self.queries = queries  # List of query strings

    def cancel_query(self):
        """UI 스레드에서 호출: 실행 중인 쿼리를 서버에서 실제로 취소하고 남은 쿼리 실행을 중단한다."""
        self.requestInterruption()
        _cancel_running_query(getattr(self._connector, "connection", None))

    def run(self):
        connector = None
        try:
            connector = connector_from_params(self.params)
            self._connector = connector
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
                        columns, row_count, result = run_streaming_query(
                            connector.connection,
                            query,
                            self.limits,
                            flow=self.stream_flow,
                            should_stop=self.isInterruptionRequested,
                            on_started=lambda cols, idx=idx: self.result_started.emit(idx, cols),
                            on_rows=lambda rows, idx=idx: self.result_rows.emit(idx, rows),
                            on_progress=lambda count, idx=idx: self.rows_progress.emit(idx, count),
                        )
                        if result.get("truncated"):
                            self.result_truncated.emit(idx, truncation_notice(result.get("truncated_by"), self.limits))
                        execution_time = time.time() - start_time
                        # 행은 result_rows로 이미 전달됨 — 최종 신호에는 개수만 의미가 있다.
                        self.query_result.emit(idx, True, columns, [], "", row_count, execution_time)
                        success_count += 1
                        continue

                    # 직접 커서 사용하여 실행
                    connector.connection.query_limits = dict(self.limits)
                    with connector.connection.cursor() as cursor:
                        cursor.execute(query)
                        if cursor.truncated:
                            self.result_truncated.emit(idx, truncation_notice(cursor.truncated_by, self.limits))

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
                    if getattr(e, "error_code", None) in CANCEL_ERROR_CODES or self.isInterruptionRequested():
                        self.finished.emit(False, "⚠️ 실행이 취소되었습니다")
                        return

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
    result_truncated = pyqtSignal(int, str)  # idx, 안내 메시지 (상한 도달로 결과가 잘림)
    rows_progress = pyqtSignal(int, int)  # idx, 지금까지 받은 행 수
    result_started = pyqtSignal(int, list)  # idx, columns
    result_rows = pyqtSignal(int, list)  # idx, 행 배치
    finished = pyqtSignal(bool, str)

    def __init__(self, connection, queries, engine, limits=None):
        super().__init__()
        self.connection = connection
        self.queries = queries
        self.engine = engine
        self.limits = dict(limits) if limits is not None else build_query_limits()
        self.stream_flow = StreamFlow()  # GUI backpressure for streamed rows

    def cancel_query(self):
        """UI 스레드에서 호출: 실행 중인 쿼리를 서버에서 취소한다. 트랜잭션은 자동 커밋/롤백하지 않는다."""
        self.requestInterruption()
        _cancel_running_query(self.connection)

    def run(self):
        self.connection.query_limits = dict(self.limits)
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
                if statement_returns_rows(query):
                    columns, row_count, result = run_streaming_query(
                        self.connection,
                        query,
                        self.limits,
                        flow=self.stream_flow,
                        should_stop=self.isInterruptionRequested,
                        on_started=lambda cols, idx=idx: self.result_started.emit(idx, cols),
                        on_rows=lambda rows, idx=idx: self.result_rows.emit(idx, rows),
                        on_progress=lambda count, idx=idx: self.rows_progress.emit(idx, count),
                    )
                    if result.get("truncated"):
                        self.result_truncated.emit(idx, truncation_notice(result.get("truncated_by"), self.limits))
                    execution_time = time.time() - start_time
                    self.query_result.emit(idx, query, True, columns, [], "", row_count, execution_time)
                    continue
                with self.connection.cursor() as cursor:
                    cursor.execute(query)
                    if cursor.truncated:
                        self.result_truncated.emit(idx, truncation_notice(cursor.truncated_by, self.limits))

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
                if getattr(e, "error_code", None) in CANCEL_ERROR_CODES:
                    # 취소/제한시간: 트랜잭션을 몰래 커밋/롤백하지 않고 상태만 알린다.
                    self.query_result.emit(idx, query, False, [], [], str(e), 0, execution_time)
                    self.finished.emit(False, _cancelled_transaction_message(self.engine, e))
                    return
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
