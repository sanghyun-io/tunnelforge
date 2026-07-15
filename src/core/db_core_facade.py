"""High-level facade over the Rust TunnelForge DB core service."""
import atexit
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.db_core_client import (
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreServiceClient,
    DbCoreServiceError,
    has_bootstrap_residual_db_core_clients,
    retry_bootstrap_residual_db_core_clients,
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


@dataclass(frozen=True)
class DbCoreConnectionHandle:
    connection_id: str
    process_generation: int


def _client_process_generation(client: Any) -> int:
    try:
        return int(getattr(client, "process_generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _residual_process_error(
    message: str,
    *,
    process_generation: int = 0,
    payload: Optional[Dict[str, Any]] = None,
) -> DbCoreServiceError:
    return DbCoreServiceError(
        message,
        code="db_core_residual_process",
        request_kind=DbCoreRequestKind.MUTATION,
        outcome=DbCoreOutcome.FAILED,
        request_id="",
        process_generation=process_generation,
        rust_code=None,
        payload=payload or {},
    )


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

    def open_connection(self, endpoint: DbEndpoint) -> DbCoreConnectionHandle:
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
                rust_code=request_result.rust_code,
                payload=request_result.payload,
            )
        return DbCoreConnectionHandle(
            connection_id=str(request_result.payload.get("connection_id", "")),
            process_generation=request_result.process_generation,
        )

    def _require_connection_handle(
        self,
        connection_handle: DbCoreConnectionHandle,
    ) -> DbCoreConnectionHandle:
        if isinstance(connection_handle, DbCoreConnectionHandle):
            return connection_handle
        raise DbCoreServiceError(
            "DB Core connection handle is missing its process generation",
            code="db_core_stale_connection",
            request_kind=DbCoreRequestKind.MUTATION,
            outcome=DbCoreOutcome.NOT_STARTED,
            request_id="",
            process_generation=_client_process_generation(self.client),
            rust_code=None,
            payload={"wire_writes": 0},
        )

    def close_connection(self, connection_handle: DbCoreConnectionHandle) -> bool:
        handle = self._require_connection_handle(connection_handle)
        result = self.client.request_payload(
            "connection.close",
            {"connection_id": handle.connection_id},
            request_kind=DbCoreRequestKind.MUTATION,
            required_generation=handle.process_generation,
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
        connection_handle: DbCoreConnectionHandle,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        result = self.execute_on_connection_result(connection_handle, sql, params=params)
        return result["rows"]

    def execute_on_connection_result(
        self,
        connection_handle: DbCoreConnectionHandle,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        handle = self._require_connection_handle(connection_handle)
        result = self.client.request_payload(
            "query.execute",
            {
                "connection_id": handle.connection_id,
                "sql": sql,
                "params": list(params or []),
            },
            request_kind=DbCoreRequestKind.MUTATION,
            required_generation=handle.process_generation,
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
        connection_handle: DbCoreConnectionHandle,
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

        handle = self._require_connection_handle(connection_handle)
        return self.client.request_payload(
            "query.execute",
            {
                "connection_id": handle.connection_id,
                "sql": sql,
                "params": list(params or []),
                "stream_rows": True,
                "row_batch_size": int(row_batch_size),
            },
            request_kind=DbCoreRequestKind.MUTATION,
            on_event=handle_event,
            required_generation=handle.process_generation,
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
_retained_facade_lock = threading.Lock()
_retained_facades: List[DbCoreFacade] = []


def retain_db_core_facade_for_retry(facade: DbCoreFacade) -> None:
    """Keep strong ownership of a facade whose bounded shutdown was residual."""
    with _retained_facade_lock:
        if not any(retained is facade for retained in _retained_facades):
            _retained_facades.append(facade)
    setattr(facade, "_db_core_shutdown_retry_pending", True)


def release_db_core_facade_retry(facade: DbCoreFacade) -> None:
    with _retained_facade_lock:
        _retained_facades[:] = [
            retained for retained in _retained_facades if retained is not facade
        ]
    setattr(facade, "_db_core_shutdown_retry_pending", False)


def is_db_core_facade_retained(facade: DbCoreFacade) -> bool:
    with _retained_facade_lock:
        return any(retained is facade for retained in _retained_facades)


def retry_retained_db_core_facades(
    *,
    timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    """Retry retained dedicated facade shutdowns within one absolute deadline."""
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    deadline_at = time.monotonic() + timeout
    remaining = max(0.0, deadline_at - time.monotonic())
    if remaining <= 0.0 or not _retained_facade_lock.acquire(timeout=remaining):
        raise _residual_process_error(
            "DB Core retained facade registry lock exceeded its deadline",
            payload={"stage": "retained_registry_lock"},
        )
    try:
        retained = list(_retained_facades)
    finally:
        _retained_facade_lock.release()

    errors = []
    for facade in retained:
        remaining = max(0.0, deadline_at - time.monotonic())
        if remaining <= 0.0:
            errors.append(
                _residual_process_error(
                    "DB Core retained facade retry exceeded its deadline",
                    process_generation=_client_process_generation(facade.client),
                    payload={"stage": "retained_facade_retry"},
                )
            )
            break
        try:
            facade.client.shutdown(timeout_seconds=remaining)
        except BaseException as exc:
            errors.append(exc)
        else:
            release_db_core_facade_retry(facade)
    remaining = max(0.0, deadline_at - time.monotonic())
    if remaining > 0.0:
        try:
            retry_bootstrap_residual_db_core_clients(timeout_seconds=remaining)
        except BaseException as exc:
            errors.append(exc)
    elif has_bootstrap_residual_db_core_clients():
        errors.append(
            _residual_process_error(
                "DB Core bootstrap owner retry exceeded its deadline",
                payload={"stage": "bootstrap_owner_retry"},
            )
        )
    if errors:
        error = errors[0]
        if isinstance(error, DbCoreServiceError):
            raise error
        raise _residual_process_error(
            f"DB Core retained facade retry failed: {type(error).__name__}: {error}",
            payload={"stage": "retained_facade_retry"},
        ) from error


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
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    deadline_at = time.monotonic() + timeout
    remaining = max(0.0, deadline_at - time.monotonic())
    if remaining <= 0.0 or not _shared_facade_lock.acquire(timeout=remaining):
        raise _residual_process_error(
            "DB Core shared facade shutdown lock exceeded its deadline",
            payload={"stage": "shared_facade_lock"},
        )
    shared_error: Optional[BaseException] = None
    try:
        facade = _shared_facade
        if facade is not None:
            remaining = max(0.0, deadline_at - time.monotonic())
            if remaining <= 0.0:
                raise _residual_process_error(
                    "DB Core shared facade shutdown exceeded its deadline",
                    process_generation=_client_process_generation(facade.client),
                    payload={"stage": "shared_facade_shutdown"},
                )
            try:
                facade.client.shutdown(timeout_seconds=remaining)
            except BaseException as exc:
                shared_error = exc
            else:
                if _shared_facade is facade:
                    _shared_facade = None
    finally:
        _shared_facade_lock.release()

    retry_error: Optional[BaseException] = None
    remaining = max(0.0, deadline_at - time.monotonic())
    if remaining > 0.0:
        try:
            retry_retained_db_core_facades(timeout_seconds=remaining)
        except BaseException as exc:
            retry_error = exc
    else:
        with _retained_facade_lock:
            retained_pending = bool(_retained_facades)
        if retained_pending or has_bootstrap_residual_db_core_clients():
            retry_error = _residual_process_error(
                "DB Core retained ownership remained after the shutdown deadline",
                payload={"stage": "retained_ownership"},
            )
    error = shared_error or retry_error
    if error is not None:
        if isinstance(error, DbCoreServiceError):
            raise error
        raise _residual_process_error(
            f"DB Core shared shutdown failed: {type(error).__name__}: {error}",
            payload={"stage": "shared_facade_shutdown"},
        ) from error


atexit.register(shutdown_shared_db_core_facade)
