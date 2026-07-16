"""DB-API-like shim adapters backed by the Rust TunnelForge DB core service."""
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.constants import SYSTEM_SCHEMAS
from src.core.db_core_client import (
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreServiceError,
    default_database_for_engine,
    normalize_db_engine,
    parse_db_version_tuple,
)
from src.core.db_core_facade import (
    DbCoreConnectionHandle,
    DbCoreFacade,
    DbEndpoint,
    get_shared_db_core_facade,
)
from src.core.logger import get_logger
from src.core.sql_query_classifier import statement_returns_rows

logger = get_logger("db_core_service")


def _require_connection_handle(
    connection_handle: DbCoreConnectionHandle,
) -> DbCoreConnectionHandle:
    if (
        type(connection_handle) is DbCoreConnectionHandle
        and connection_handle.is_well_formed()
    ):
        return connection_handle
    raise DbCoreServiceError(
        "DB Core connection handle is missing its process generation",
        code="db_core_stale_connection",
        request_kind=DbCoreRequestKind.MUTATION,
        outcome=DbCoreOutcome.NOT_STARTED,
        request_id="",
        process_generation=0,
        rust_code=None,
        payload={"wire_writes": 0},
    )


def _closed_connection_error(
    connection_handle: DbCoreConnectionHandle,
) -> DbCoreServiceError:
    return DbCoreServiceError(
        "connection is closed",
        code="db_core_business_failure",
        request_kind=DbCoreRequestKind.MUTATION,
        outcome=DbCoreOutcome.NOT_STARTED,
        request_id="",
        process_generation=connection_handle.process_generation,
        rust_code=None,
        payload={"connection_id": connection_handle.connection_id, "wire_writes": 0},
    )


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
        self.connection_id: Optional[DbCoreConnectionHandle] = None
        self.connection: Optional["RustDbConnection"] = None

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

    def schema_exists(self, schema_name: Optional[str]) -> bool:
        if not schema_name:
            return True
        if not self.connection:
            success, _ = self.connect()
            if not success:
                return False
        try:
            with self.connection.cursor() as cursor:
                if self.endpoint.engine == "postgresql":
                    cursor.execute(
                        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                        (schema_name,),
                    )
                else:
                    cursor.execute("SHOW DATABASES LIKE %s", (schema_name,))
                return cursor.fetchone() is not None
        except DbCoreServiceError as exc:
            self._log_metadata_error("schema_exists", exc)
            raise
        except Exception as exc:
            self._log_metadata_error("schema_exists", exc)
            return False

    def get_schemas(self, use_cache: bool = True) -> List[str]:
        if not self.connection:
            success, _ = self.connect()
            if not success:
                return []
        try:
            with self.connection.cursor() as cursor:
                if self.endpoint.engine == "postgresql":
                    cursor.execute(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name <> 'information_schema' "
                        "AND schema_name NOT LIKE 'pg_%' "
                        "ORDER BY schema_name"
                    )
                    return [str(row.get("schema_name")) for row in cursor.fetchall()]
                cursor.execute("SHOW DATABASES")
                return [
                    str(row.get("Database"))
                    for row in cursor.fetchall()
                    if str(row.get("Database")) not in SYSTEM_SCHEMAS
                ]
        except DbCoreServiceError as exc:
            self._log_metadata_error("get_schemas", exc)
            raise
        except Exception as exc:
            self._log_metadata_error("get_schemas", exc)
            return []

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
        if not self.connection:
            success, _ = self.connect()
            if not success:
                return ""
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version" if self.endpoint.engine == "mysql" else "SELECT version() AS version")
                row = cursor.fetchone() or {}
                return str(row.get("version", ""))
        except DbCoreServiceError as exc:
            self._log_metadata_error("get_db_version_string", exc)
            raise
        except Exception as exc:
            self._log_metadata_error("get_db_version_string", exc)
            return ""

    def get_column_names(self, table: str, schema: Optional[str] = None) -> List[str]:
        if not self.connection:
            success, _ = self.connect()
            if not success:
                return []
        try:
            with self.connection.cursor() as cursor:
                if self.endpoint.engine == "postgresql":
                    cursor.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s "
                        "ORDER BY ordinal_position",
                        (schema or self.endpoint.schema or "public", table),
                    )
                else:
                    cursor.execute(
                        "SELECT COLUMN_NAME AS column_name FROM information_schema.columns "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION",
                        (schema or self.endpoint.database, table),
                    )
                return [str(row.get("column_name")) for row in cursor.fetchall()]
        except DbCoreServiceError as exc:
            self._log_metadata_error("get_column_names", exc)
            raise
        except Exception as exc:
            self._log_metadata_error("get_column_names", exc)
            return []


def create_rust_db_connector(
    engine: Optional[str],
    host: str,
    port: int,
    user: str,
    password: str,
    database: Optional[str] = None,
    schema: str = "",
    facade: Optional[DbCoreFacade] = None,
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

    def __init__(
        self,
        endpoint: DbEndpoint,
        facade: DbCoreFacade,
        connection_handle: DbCoreConnectionHandle,
    ):
        self.endpoint = endpoint
        self.facade = facade
        self.connection_handle = _require_connection_handle(connection_handle)
        self.connection_id = self.connection_handle
        self.open = True
        self._autocommit = True
        self._in_transaction = False

    def cursor(self) -> "RustDbCursor":
        return RustDbCursor(self)

    def ping(self, reconnect: bool = False) -> None:
        # `reconnect` is accepted for DB-API shim compatibility only; the Rust core owns
        # connection lifecycle and Python does not implement reconnect.
        if not self.open:
            raise _closed_connection_error(self.connection_handle)
        try:
            self.facade.execute_on_connection(self.connection_handle, "SELECT 1")
        except Exception:
            self.open = False
            raise

    def close(self) -> None:
        if self.open:
            self.facade.close_connection(self.connection_handle)
            self.open = False

    def commit(self) -> None:
        if self.open:
            self.facade.execute_on_connection(self.connection_handle, "COMMIT")
            self._in_transaction = False
            if not self._autocommit:
                self._begin_transaction()

    def rollback(self) -> None:
        if self.open:
            self.facade.execute_on_connection(self.connection_handle, "ROLLBACK")
            self._in_transaction = False
            if not self._autocommit:
                self._begin_transaction()

    def autocommit(self, enabled: bool) -> None:
        self._autocommit = bool(enabled)
        if not self.open:
            return
        if self.endpoint.engine == "mysql":
            self.facade.execute_on_connection(
                self.connection_handle,
                "SET autocommit = 1" if enabled else "SET autocommit = 0",
            )
            self._in_transaction = not enabled
        elif enabled:
            if self._in_transaction:
                self.facade.execute_on_connection(self.connection_handle, "COMMIT")
            self._in_transaction = False
        else:
            self._begin_transaction()

    def _begin_transaction(self) -> None:
        if self.open and not self._in_transaction:
            self.facade.execute_on_connection(self.connection_handle, "BEGIN")
            self._in_transaction = True

    def select_db(self, database: str) -> None:
        self.endpoint = replace(self.endpoint, database=database)
        if self.endpoint.engine == "mysql":
            self.facade.execute_on_connection(
                self.connection_handle,
                f"USE {quote_mysql_ident(database)}",
            )


class RustDbCursor:
    """Small cursor shim for legacy PyQt code using connection.cursor()."""

    def __init__(self, connection: RustDbConnection):
        self.connection = connection
        self.connection_handle = connection.connection_handle
        self._rows: List[Dict[str, Any]] = []
        self.rowcount = 0
        self.description = None

    def __enter__(self) -> "RustDbCursor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> int:
        result = self.connection.facade.execute_on_connection_result(
            self.connection_handle,
            query,
            params=params,
        )
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
        return list(self._rows)

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._rows[0] if self._rows else None


def quote_mysql_ident(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"
