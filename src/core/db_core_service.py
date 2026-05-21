"""Client facade for the Rust TunnelForge DB core service."""
import json
import os
import re
import subprocess
import threading
import uuid
import atexit
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.cross_engine_migration import db_core_executable, parse_helper_event


class DbCoreServiceError(RuntimeError):
    """Raised when the Rust DB core service cannot complete a request."""


def _format_error_event(payload: Dict[str, Any]) -> str:
    message = str(payload.get("message") or payload.get("error") or "DB core service error")
    details: List[str] = []
    for key, label in (
        ("code", "code"),
        ("detail", "detail"),
        ("hint", "hint"),
        ("context", "context"),
        ("table", "table"),
        ("column", "column"),
        ("constraint", "constraint"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            details.append(f"{label}={value}")
    if not details:
        return message
    return f"{message} ({'; '.join(details)})"


SUPPORTED_DB_ENGINES = {"mysql", "postgresql"}


def parse_db_version_tuple(version: Any) -> Tuple[int, int, int]:
    """Return a connector-compatible (major, minor, patch) tuple."""
    if isinstance(version, tuple):
        parts = list(version)
    elif isinstance(version, list):
        parts = version
    else:
        text = str(version or "")
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
        if not match:
            return (0, 0, 0)
        parts = [match.group(1), match.group(2) or 0, match.group(3) or 0]

    parsed = []
    for index in range(3):
        try:
            parsed.append(int(parts[index]))
        except (IndexError, TypeError, ValueError):
            parsed.append(0)
    return tuple(parsed)


def normalize_db_engine(engine: Optional[str], port: Optional[int] = None) -> str:
    """Return the Rust core engine id used by DB-facing product paths."""
    value = str(engine or "").strip().lower()
    if value in ("postgres", "postgresql", "pg"):
        return "postgresql"
    if value in ("mysql", "mariadb"):
        return "mysql"
    if int(port or 0) == 5432:
        return "postgresql"
    return "mysql"


def default_database_for_engine(engine: str, database: Optional[str] = None) -> str:
    if database:
        return database
    return "postgres" if normalize_db_engine(engine) == "postgresql" else ""


@dataclass(frozen=True)
class DbEndpoint:
    engine: str
    host: str
    port: int
    user: str
    password: str
    database: str
    schema: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "host": self.host,
            "port": int(self.port),
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "schema": self.schema,
        }


class DbCoreServiceClient:
    """Sequential JSONL client for the long-lived Rust DB core process."""

    def __init__(
        self,
        executable: Optional[str] = None,
        popen_factory: Optional[Callable[..., subprocess.Popen]] = None,
    ):
        self.executable = executable or db_core_executable()
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._process and self._process.poll() is None:
            return
        try:
            self._process = self._popen_factory(
                [self.executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except FileNotFoundError as exc:
            raise DbCoreServiceError(
                "Rust DB Core 실행 파일을 찾을 수 없습니다: "
                f"{self.executable}\n"
                "소스 실행이면 `cargo build --manifest-path migration_core\\Cargo.toml --release`를 먼저 실행하고, "
                "설치본이면 배포 패키지에 tunnelforge-core.exe가 포함되어 있는지 확인하세요."
            ) from exc

    def request(
        self,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        request_id = request_id or f"py-{uuid.uuid4().hex}"
        body = {
            "command": command,
            "request_id": request_id,
            "payload": payload or {},
        }

        with self._lock:
            self.start()
            assert self._process is not None
            if self._process.stdin is None or self._process.stdout is None:
                raise DbCoreServiceError("DB core service pipes are not available")

            self._process.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
            self._process.stdin.flush()

            while True:
                line = self._process.stdout.readline()
                if line == "":
                    stderr = self._process.stderr.read().strip() if self._process.stderr else ""
                    raise DbCoreServiceError(stderr or "DB core service stopped before a result")

                event = parse_helper_event(line)
                if event.request_id not in (None, request_id):
                    continue
                if on_event:
                    on_event(event.payload)
                if event.event == "result":
                    return event.payload
                if event.event == "error":
                    raise DbCoreServiceError(_format_error_event(event.payload))

    def shutdown(self) -> None:
        process = self._process
        if not process:
            return
        try:
            if process.poll() is None:
                self.request("service.shutdown")
        except Exception:
            process.terminate()
        finally:
            self._process = None

    def __enter__(self) -> "DbCoreServiceClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.shutdown()
        return False


class DbCoreFacade:
    """High-level DB operations exposed to UI/workers."""

    def __init__(self, client: Optional[DbCoreServiceClient] = None):
        self.client = client or DbCoreServiceClient()

    def hello(self) -> Dict[str, Any]:
        return self.client.request("service.hello")

    def test_connection(self, endpoint: DbEndpoint) -> Tuple[bool, str]:
        result = self.client.request("connection.test", {"connection": endpoint.to_payload()})
        return bool(result.get("success")), str(result.get("message", ""))

    def open_connection(self, endpoint: DbEndpoint) -> str:
        result = self.client.request("connection.open", {"connection": endpoint.to_payload()})
        if not result.get("success"):
            raise DbCoreServiceError(str(result.get("message", "connection failed")))
        return str(result.get("connection_id", ""))

    def close_connection(self, connection_id: str) -> bool:
        result = self.client.request("connection.close", {"connection_id": connection_id})
        return bool(result.get("success"))

    def inspect_schema(self, endpoint: DbEndpoint) -> Dict[str, Any]:
        result = self.client.request("schema.inspect", {"source": endpoint.to_payload()})
        return result.get("schema") if isinstance(result.get("schema"), dict) else {"tables": []}

    def list_tables(self, endpoint: DbEndpoint) -> List[str]:
        result = self.client.request("schema.list", {"connection": endpoint.to_payload()})
        tables = result.get("tables")
        return [str(table) for table in tables] if isinstance(tables, list) else []

    def schema_diff(self, source_schema: Dict[str, Any], target_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
        result = self.client.request(
            "schema.diff",
            {"source_schema": source_schema, "target_schema": target_schema},
        )
        differences = result.get("differences")
        return [item for item in differences if isinstance(item, dict)] if isinstance(differences, list) else []

    def execute_query(
        self,
        endpoint: DbEndpoint,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        result = self.client.request(
            "query.execute",
            {"connection": endpoint.to_payload(), "sql": sql, "params": list(params or [])},
        )
        rows = result.get("rows")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def execute_on_connection(
        self,
        connection_id: str,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        result = self.client.request(
            "query.execute",
            {"connection_id": connection_id, "sql": sql, "params": list(params or [])},
        )
        rows = result.get("rows")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def execute_on_connection_streaming(
        self,
        connection_id: str,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        row_batch_size: int = 500,
        on_batch: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, Any]:
        def handle_event(payload: Dict[str, Any]) -> None:
            if payload.get("event") != "row_batch" or not on_batch:
                return
            rows = payload.get("rows")
            if isinstance(rows, list):
                on_batch([row for row in rows if isinstance(row, dict)])

        return self.client.request(
            "query.execute",
            {
                "connection_id": connection_id,
                "sql": sql,
                "params": list(params or []),
                "stream_rows": True,
                "row_batch_size": int(row_batch_size),
            },
            on_event=handle_event,
        )

    def run_migration(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request("migration.run", payload, on_event=on_event)

    def verify_migration(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.request("migration.verify", payload)

    def run_dump(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request("dump.run", payload, on_event=on_event)

    def import_dump(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request("dump.import", payload, on_event=on_event)


_shared_facade_lock = threading.Lock()
_shared_facade: Optional[DbCoreFacade] = None


def get_shared_db_core_facade() -> DbCoreFacade:
    """Return the app-wide Rust DB core facade."""
    global _shared_facade
    with _shared_facade_lock:
        if _shared_facade is None:
            _shared_facade = DbCoreFacade()
        return _shared_facade


def shutdown_shared_db_core_facade() -> None:
    """Shutdown the app-wide Rust DB core process if it was started."""
    global _shared_facade
    with _shared_facade_lock:
        facade = _shared_facade
        _shared_facade = None
    if facade is not None:
        facade.client.shutdown()


atexit.register(shutdown_shared_db_core_facade)


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
    ):
        self.endpoint = DbEndpoint(
            engine=engine,
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database or ("postgres" if engine == "postgresql" else ""),
            schema=schema,
        )
        self.facade = facade or get_shared_db_core_facade()
        self.connection_id: Optional[str] = None
        self.connection: Optional[RustDbConnection] = None

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
        except Exception:
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
                excluded = {"information_schema", "performance_schema", "mysql", "sys"}
                return [
                    str(row.get("Database"))
                    for row in cursor.fetchall()
                    if str(row.get("Database")) not in excluded
                ]
        except Exception:
            return []

    def get_tables(self, schema: Optional[str] = None, use_cache: bool = True) -> List[str]:
        endpoint = self.endpoint
        if schema:
            endpoint = DbEndpoint(
                engine=self.endpoint.engine,
                host=self.endpoint.host,
                port=self.endpoint.port,
                user=self.endpoint.user,
                password=self.endpoint.password,
                database=self.endpoint.database if self.endpoint.engine == "postgresql" else schema,
                schema=schema if self.endpoint.engine == "postgresql" else "",
            )
        return self.facade.list_tables(endpoint)

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
        except Exception:
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
        except Exception:
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
    return RustDbConnector(
        resolved_engine,
        host,
        int(port),
        user,
        password,
        default_database_for_engine(resolved_engine, database),
        schema,
        facade=facade,
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

    def cursor(self) -> "RustDbCursor":
        return RustDbCursor(self)

    def ping(self, reconnect: bool = False) -> None:
        if not self.open:
            raise DbCoreServiceError("connection is closed")

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
        self.endpoint = DbEndpoint(
            engine=self.endpoint.engine,
            host=self.endpoint.host,
            port=self.endpoint.port,
            user=self.endpoint.user,
            password=self.endpoint.password,
            database=database,
            schema=self.endpoint.schema,
        )
        if self.endpoint.engine == "mysql":
            self.facade.execute_on_connection(self.connection_id, f"USE {quote_mysql_ident(database)}")


class RustDbCursor:
    """Small cursor shim for legacy PyQt code using connection.cursor()."""

    def __init__(self, connection: RustDbConnection):
        self.connection = connection
        self._rows: List[Dict[str, Any]] = []
        self.rowcount = 0
        self.description = None

    def __enter__(self) -> "RustDbCursor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> int:
        self._rows = self.connection.facade.execute_on_connection(
            self.connection.connection_id,
            query,
            params=params,
        )
        if self._rows:
            self.description = [(column,) for column in self._rows[0].keys()]
        elif query_returns_rows(query):
            self.description = []
        else:
            self.description = None
        self.rowcount = len(self._rows)
        return self.rowcount

    def executemany(self, query: str, data: Sequence[Sequence[Any]]) -> int:
        total = 0
        for params in data:
            total += self.execute(query, params)
        self.rowcount = total
        return total

    def fetchall(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._rows[0] if self._rows else None


def bind_sql_params(query: str, params: Optional[Sequence[Any]]) -> str:
    if not params:
        return query
    rendered = query
    if isinstance(params, dict):
        for key, value in params.items():
            rendered = rendered.replace(f"%({key})s", sql_literal(value))
        return rendered
    for value in params:
        rendered = rendered.replace("%s", sql_literal(value), 1)
    return rendered


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def quote_mysql_ident(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def query_returns_rows(sql: str) -> bool:
    lower = sql.lstrip().lower()
    return lower.startswith(("select", "with", "show", "desc", "describe", "explain"))
