"""High-level facade over the Rust TunnelForge DB core service."""
import atexit
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.db_core_client import (
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreServiceClient,
    DbCoreServiceError,
)


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


class DbCoreFacade:
    """High-level DB operations exposed to UI/workers."""

    def __init__(self, client: Optional[DbCoreServiceClient] = None):
        self.client = client or DbCoreServiceClient()

    def hello(self) -> Dict[str, Any]:
        return self.client.request_payload(
            "service.hello",
            request_kind=DbCoreRequestKind.READ_ONLY,
        )

    def test_connection(self, endpoint: DbEndpoint) -> Tuple[bool, str]:
        result = self.client.request_payload(
            "connection.test",
            {"connection": endpoint.to_payload()},
            request_kind=DbCoreRequestKind.READ_ONLY,
        )
        return bool(result.get("success")), str(result.get("message", ""))

    def open_connection(self, endpoint: DbEndpoint) -> str:
        request_result = self.client.request_result(
            "connection.open",
            {"connection": endpoint.to_payload()},
            request_kind=DbCoreRequestKind.MUTATION,
        )
        if not request_result.payload.get("success"):
            raise DbCoreServiceError(
                str(request_result.payload.get("message", "connection failed")),
                code="db_core_business_failure",
                request_kind=DbCoreRequestKind.MUTATION,
                outcome=DbCoreOutcome.DEFINITE,
                request_id=request_result.request_id,
                process_generation=request_result.process_generation,
                payload=request_result.payload,
            )
        return str(request_result.payload.get("connection_id", ""))

    def close_connection(self, connection_id: str) -> bool:
        result = self.client.request_payload(
            "connection.close",
            {"connection_id": connection_id},
            request_kind=DbCoreRequestKind.MUTATION,
        )
        return bool(result.get("success"))

    def inspect_schema(self, endpoint: DbEndpoint) -> Dict[str, Any]:
        result = self.client.request_payload(
            "schema.inspect",
            {"source": endpoint.to_payload()},
            request_kind=DbCoreRequestKind.READ_ONLY,
        )
        return result.get("schema") if isinstance(result.get("schema"), dict) else {"tables": []}

    def list_tables(self, endpoint: DbEndpoint) -> List[str]:
        result = self.client.request_payload(
            "schema.list",
            {"connection": endpoint.to_payload()},
            request_kind=DbCoreRequestKind.READ_ONLY,
        )
        tables = result.get("tables")
        return [str(table) for table in tables] if isinstance(tables, list) else []

    def schema_diff(self, source_schema: Dict[str, Any], target_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
        result = self.client.request_payload(
            "schema.diff",
            {"source_schema": source_schema, "target_schema": target_schema},
            request_kind=DbCoreRequestKind.READ_ONLY,
        )
        differences = result.get("differences")
        return [item for item in differences if isinstance(item, dict)] if isinstance(differences, list) else []

    def execute_query(
        self,
        endpoint: DbEndpoint,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        result = self.execute_query_result(endpoint, sql, params=params)
        return result["rows"]

    def execute_query_result(
        self,
        endpoint: DbEndpoint,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        result = self.client.request_payload(
            "query.execute",
            {"connection": endpoint.to_payload(), "sql": sql, "params": list(params or [])},
            request_kind=DbCoreRequestKind.MUTATION,
        )
        rows = result.get("rows")
        columns = result.get("columns")
        return {
            "rows": [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [],
            "columns": [str(column) for column in columns] if isinstance(columns, list) else [],
            "rows_affected": int(result.get("rows_affected") or 0),
        }

    def execute_on_connection(
        self,
        connection_id: str,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        result = self.execute_on_connection_result(connection_id, sql, params=params)
        return result["rows"]

    def execute_on_connection_result(
        self,
        connection_id: str,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        result = self.client.request_payload(
            "query.execute",
            {"connection_id": connection_id, "sql": sql, "params": list(params or [])},
            request_kind=DbCoreRequestKind.MUTATION,
        )
        rows = result.get("rows")
        columns = result.get("columns")
        return {
            "rows": [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [],
            "columns": [str(column) for column in columns] if isinstance(columns, list) else [],
            "rows_affected": int(result.get("rows_affected") or 0),
        }

    def execute_on_connection_streaming(
        self,
        connection_id: str,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        row_batch_size: int = 500,
        on_batch: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ) -> Dict[str, Any]:
        def handle_event(payload: Dict[str, Any]) -> None:
            # Ignores the leading "columns" progress event; only row_batch is consumed here.
            if payload.get("event") != "row_batch" or not on_batch:
                return
            rows = payload.get("rows")
            if isinstance(rows, list):
                on_batch([row for row in rows if isinstance(row, dict)])

        return self.client.request_payload(
            "query.execute",
            {
                "connection_id": connection_id,
                "sql": sql,
                "params": list(params or []),
                "stream_rows": True,
                "row_batch_size": int(row_batch_size),
            },
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=handle_event,
        )

    def run_migration(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request_payload(
            "migration.run",
            payload,
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=on_event,
        )

    def verify_migration(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.request_payload(
            "migration.verify",
            payload,
            request_kind=DbCoreRequestKind.READ_ONLY,
        )

    def run_dump(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request_payload(
            "dump.run",
            payload,
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=on_event,
        )

    def import_dump(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request_payload(
            "dump.import",
            payload,
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=on_event,
        )

    def run_oneclick(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request_payload(
            "oneclick.run",
            payload,
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=on_event,
        )

    def derive_oneclick_charset_contracts(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request_payload(
            "oneclick.derive_charset_contracts",
            payload,
            request_kind=DbCoreRequestKind.READ_ONLY,
            on_event=on_event,
        )

    def apply_oneclick_fixes(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request_payload(
            "oneclick.apply_fixes",
            payload,
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=on_event,
        )


_shared_facade_lock = threading.Lock()
_shared_facade: Optional[DbCoreFacade] = None


def get_shared_db_core_facade() -> DbCoreFacade:
    """Return the app-wide Rust DB core facade."""
    global _shared_facade
    with _shared_facade_lock:
        if _shared_facade is None:
            _shared_facade = DbCoreFacade()
        return _shared_facade


def shutdown_shared_db_core_facade(
    *,
    timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Shutdown the app-wide Rust DB core process if it was started."""
    global _shared_facade
    with _shared_facade_lock:
        facade = _shared_facade
        if facade is None:
            return
        facade.client.shutdown(timeout_seconds=timeout_seconds)
        if _shared_facade is facade:
            _shared_facade = None


atexit.register(shutdown_shared_db_core_facade)
