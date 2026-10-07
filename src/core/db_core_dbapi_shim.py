"""DB-API-like shim adapters backed by the Rust TunnelForge DB core service."""
import uuid
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.db_core_client import (
    DbCoreServiceError,
    default_database_for_engine,
    normalize_db_engine,
    parse_db_version_tuple,
)
from src.core.db_core_facade import DbCoreFacade, DbEndpoint, get_shared_db_core_facade
from src.core.logger import get_logger
from src.core.sql_query_classifier import statement_returns_rows

logger = get_logger("db_core_service")


class RustDbConnector:
    """Connector-shaped adapter used by PyQt workers during DB auth checks."""

    def __init__(
        self,
        engine: str,
        host: str,
        port: int,
        user: str,
        password: str,
        database: Optional[str] = None,
        schema: str = "",
        facade: Optional[DbCoreFacade] = None,
        *,
        endpoint: Optional[DbEndpoint] = None,
    ):
        self.endpoint = endpoint if endpoint is not None else DbEndpoint(
            engine=engine,
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database or ("postgres" if engine == "postgresql" else ""),
            schema=schema,
        )
        self.facade = facade if facade is not None else get_shared_db_core_facade()
        self.connection_id: Optional[str] = None
        self.connection: Optional["RustDbConnection"] = None

    # 연결 정보 (덤프 설정, 화면 표시용)
    host = property(lambda self: self.endpoint.host)
    port = property(lambda self: self.endpoint.port)
    user = property(lambda self: self.endpoint.user)
    password = property(lambda self: self.endpoint.password)
    database = property(lambda self: self.endpoint.database)
    engine = property(lambda self: self.endpoint.engine)

    def _log_metadata_error(self, operation: str, exc: Exception) -> None:
        logger.exception("%s 메타데이터 조회 실패: %s", operation, exc)

    def connect(self) -> Tuple[bool, str]:
        try:
            self.connection_id = self.facade.open_connection(self.endpoint)
            self.connection = RustDbConnection(self.endpoint, self.facade, self.connection_id)
            return True, "연결 성공"
        except Exception as exc:
            return False, str(exc)

    def disconnect(self) -> None:
        if self.connection_id:
            self.facade.close_connection(self.connection_id)
        self.connection_id = None
        self.connection = None

    def _catalog(self, operation: str, kind: str, default: Any, **args: str) -> Any:
        """메타데이터 조회는 Rust catalog.query 가 한다. 서비스 오류는 올리고, 그 밖의 실패는 default."""
        if not self.connection:
            success, _ = self.connect()
            if not success:
                return default
        try:
            # 세션 ID 는 연결 객체에서 읽는다 (connection 만 넘겨받은 커넥터도 있다).
            return self.facade.catalog(self.connection.connection_id, kind, **args)
        except DbCoreServiceError as exc:
            self._log_metadata_error(operation, exc)
            raise
        except Exception as exc:
            self._log_metadata_error(operation, exc)
            return default

    def schema_exists(self, schema_name: Optional[str]) -> bool:
        if not schema_name:
            return True
        return bool(self._catalog("schema_exists", "schema_exists", [], name=schema_name))

    def get_schemas(self, use_cache: bool = True) -> List[str]:
        return self._catalog("get_schemas", "schemas", [])

    def get_tables(self, schema: Optional[str] = None, use_cache: bool = True) -> List[str]:
        endpoint = self.endpoint
        if schema:
            new_database = self.endpoint.database if self.endpoint.engine == "postgresql" else schema
            new_schema = schema if self.endpoint.engine == "postgresql" else ""
            endpoint = replace(self.endpoint, database=new_database, schema=new_schema)
        try:
            return self.facade.list_tables(endpoint)
        except DbCoreServiceError as exc:
            self._log_metadata_error("get_tables", exc)
            raise
        except Exception as exc:
            self._log_metadata_error("get_tables", exc)
            return []

    def get_db_version(self) -> Tuple[int, int, int]:
        return parse_db_version_tuple(self.get_db_version_string())

    def get_db_version_string(self) -> str:
        values = self._catalog("get_db_version_string", "version", [])
        return values[0] if values else ""

    def get_column_names(self, table: str, schema: Optional[str] = None) -> List[str]:
        default_schema = (self.endpoint.schema or "public") if self.endpoint.engine == "postgresql" else self.endpoint.database
        return self._catalog("get_column_names", "columns", [], schema=schema or default_schema, table=table)

    def list_databases(self) -> List[str]:
        """PostgreSQL 접속 가능한 데이터베이스 목록"""
        return self._catalog("list_databases", "databases", [])

    def has_named_timezone(self, name: str) -> bool:
        """MySQL 이 'Asia/Seoul' 같은 지역명 타임존을 아는지 (mysql.time_zone_name 에 데이터가 있는지)"""
        return bool(self._catalog("has_named_timezone", "named_timezone", [], name=name))

def create_rust_db_connector(
    engine: Optional[str],
    host: str,
    port: int,
    user: str,
    password: str,
    database: Optional[str] = None,
    schema: str = "",
    facade: Optional[DbCoreFacade] = None,
    read_only: bool = False,
) -> RustDbConnector:
    """Create an engine-aware Rust connector for UI/orchestration code."""
    resolved_engine = normalize_db_engine(engine, port)
    endpoint = DbEndpoint(
        engine=resolved_engine,
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=default_database_for_engine(resolved_engine, database),
        schema=schema,
        read_only=read_only,
    )
    return RustDbConnector(
        resolved_engine,
        host,
        int(port),
        user,
        password,
        facade=facade,
        endpoint=endpoint,
    )


class RustDbConnection:
    """Minimal DB-API-like connection backed by a Rust service connection."""

    def __init__(self, endpoint: DbEndpoint, facade: DbCoreFacade, connection_id: str):
        self.endpoint = endpoint
        self.facade = facade
        self.connection_id = connection_id
        self.open = True
        self._autocommit = True
        self._in_transaction = False
        # Opt-in limits for cursor.execute (max_rows / max_bytes / timeout_ms); empty = unlimited.
        self.query_limits: Dict[str, int] = {}
        self.current_job_id: Optional[str] = None

    def cancel_running_query(self) -> bool:
        """Cancel the query currently running on this connection (safe from another thread)."""
        job_id = self.current_job_id
        if not job_id:
            return False
        return bool(self.facade.cancel_query(job_id).get("cancelled"))

    def cursor(self) -> "RustDbCursor":
        return RustDbCursor(self)

    def ping(self, reconnect: bool = False) -> None:
        # `reconnect` is accepted for DB-API shim compatibility only; the Rust core owns
        # connection lifecycle and Python does not implement reconnect.
        if not self.open:
            raise DbCoreServiceError("connection is closed")
        try:
            self.facade.execute_on_connection(self.connection_id, "SELECT 1")
        except Exception:
            self.open = False
            raise

    def close(self) -> None:
        if self.open:
            self.facade.close_connection(self.connection_id)
            self.open = False

    def commit(self) -> None:
        if self.open:
            self.facade.execute_on_connection(self.connection_id, "COMMIT")
            self._in_transaction = False
            if not self._autocommit:
                self._begin_transaction()

    def rollback(self) -> None:
        if self.open:
            self.facade.execute_on_connection(self.connection_id, "ROLLBACK")
            self._in_transaction = False
            if not self._autocommit:
                self._begin_transaction()

    def autocommit(self, enabled: bool) -> None:
        self._autocommit = bool(enabled)
        if not self.open:
            return
        if self.endpoint.engine == "mysql":
            self.facade.execute_on_connection(
                self.connection_id,
                "SET autocommit = 1" if enabled else "SET autocommit = 0",
            )
            self._in_transaction = not enabled
        elif enabled:
            if self._in_transaction:
                self.facade.execute_on_connection(self.connection_id, "COMMIT")
            self._in_transaction = False
        else:
            self._begin_transaction()

    def _begin_transaction(self) -> None:
        if self.open and not self._in_transaction:
            self.facade.execute_on_connection(self.connection_id, "BEGIN")
            self._in_transaction = True

    def select_db(self, database: str) -> None:
        if not self.open:
            raise DbCoreServiceError("connection is closed")
        if self.endpoint.engine == "mysql":
            self.facade.execute_on_connection(self.connection_id, f"USE {quote_mysql_ident(database)}")
        elif database != self.endpoint.database:
            raise DbCoreServiceError("PostgreSQL database selection requires reconnect")
        self.endpoint = replace(self.endpoint, database=database)


class RustDbCursor:
    """Small cursor shim for legacy PyQt code using connection.cursor()."""

    def __init__(self, connection: RustDbConnection):
        self.connection = connection
        self._rows: List[Dict[str, Any]] = []
        self._position = 0
        self.rowcount = 0
        self.description = None
        self.truncated = False
        self.truncated_by: Optional[str] = None

    def __enter__(self) -> "RustDbCursor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> int:
        self._rows = []
        self._position = 0
        self.description = None
        self.rowcount = -1
        if not self.connection.open:
            raise DbCoreServiceError("connection is closed")
        self.truncated = False
        self.truncated_by = None
        control = {}
        if self.connection.query_limits:  # controlled execution (limits + cancellable job id)
            control = {"job_id": f"cur-{uuid.uuid4().hex}", **self.connection.query_limits}
            self.connection.current_job_id = control["job_id"]
        try:
            result = self.connection.facade.execute_on_connection_result(
                self.connection.connection_id,
                query,
                params=params,
                **control,
            )
        finally:
            self.connection.current_job_id = None
        self.truncated = bool(result.get("truncated"))
        self.truncated_by = result.get("truncated_by")
        self._rows = result.get("rows", [])
        columns = result.get("columns") or None
        rows_affected = int(result.get("rows_affected") or 0)

        if not columns and self._rows:
            columns = list(self._rows[0].keys())

        returns_rows = bool(columns) or statement_returns_rows(query)
        if returns_rows:
            self.description = [(column,) for column in columns] if columns else []
            self.rowcount = len(self._rows)
        else:
            self.description = None
            self.rowcount = rows_affected
        return self.rowcount

    def executemany(self, query: str, data: Sequence[Sequence[Any]]) -> int:
        raise RuntimeError(
            "RustDbCursor.executemany is disabled. "
            "Batch DB operations must be modeled as explicit Rust Core commands."
        )

    def fetchall(self) -> List[Dict[str, Any]]:
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def fetchone(self) -> Optional[Dict[str, Any]]:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row


def quote_mysql_ident(identifier: str) -> str:
    """MySQL 식별자를 백틱으로 감싸고, 이름 안의 백틱은 두 번 써서 escape한다."""
    return "`" + str(identifier).replace("`", "``") + "`"
