"""High-level facade over the Rust TunnelForge DB core service."""
import atexit
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.connection_trust import extract_error_code, friendly_error_message, lookup_endpoint_tls
from src.core.db_core_client import DbCoreServiceClient, DbCoreServiceError


def _query_control(job_id, timeout_ms, max_rows, max_bytes) -> Dict[str, Any]:
    control: Dict[str, Any] = {}
    for key, value in (("job_id", job_id), ("timeout_ms", timeout_ms),
                       ("max_rows", max_rows), ("max_bytes", max_bytes)):
        if value:
            control[key] = value
    return control


def _raise_if_query_failed(result: Dict[str, Any]) -> None:
    """Cancel/timeout arrive as a failed result (with partial counts); surface them as errors."""
    if result.get("success") is False and result.get("error_code"):
        raise DbCoreServiceError(
            str(result.get("message") or result["error_code"]),
            error_code=result["error_code"],
            payload=result,
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
    # TF-STATUS-110: "disable" | "verify_ca" | "verify_full" (Rust `TlsMode`).
    # Empty = take the policy registered for host:port by the tunnel engine (else "disable").
    tls_mode: str = ""
    tls_ca_file: str = ""
    tls_server_name: str = ""
    # TF-STATUS-128: server-enforced read-only session (production profiles); sent only when true.
    read_only: bool = False

    def __post_init__(self):
        if not self.tls_mode:
            policy = lookup_endpoint_tls(self.host, self.port)
            object.__setattr__(self, "tls_mode", policy.mode)
            object.__setattr__(self, "tls_ca_file", self.tls_ca_file or policy.ca_file)
            object.__setattr__(self, "tls_server_name", self.tls_server_name or policy.server_name)

    def to_payload(self) -> Dict[str, Any]:
        payload = {
            "engine": self.engine,
            "host": self.host,
            "port": int(self.port),
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "schema": self.schema,
        }
        if self.tls_mode != "disable":
            tls: Dict[str, Any] = {"mode": self.tls_mode}
            if self.tls_ca_file:
                tls["ca_file"] = self.tls_ca_file
            if self.tls_server_name:
                tls["server_name"] = self.tls_server_name
            payload["tls"] = tls
        if self.read_only:
            payload["read_only"] = True
        return payload


def _with_friendly_hint(message: str, code: Optional[str]) -> str:
    """안정 오류 코드가 있으면 사용자가 바로 조치할 수 있는 설명을 앞에 붙인다."""
    hint = friendly_error_message(code or extract_error_code(message))
    return f"{hint}\n\n{message}" if hint else message


class DbCoreFacade:
    """High-level DB operations exposed to UI/workers."""

    def __init__(self, client: Optional[DbCoreServiceClient] = None):
        self.client = client or DbCoreServiceClient()

    def hello(self) -> Dict[str, Any]:
        return self.client.request("service.hello")

    def test_connection(self, endpoint: DbEndpoint) -> Tuple[bool, str]:
        result = self.client.request("connection.test", {"connection": endpoint.to_payload()})
        message = str(result.get("message", ""))
        if not result.get("success"):
            message = _with_friendly_hint(message, result.get("error_code"))
        return bool(result.get("success")), message

    def open_connection(self, endpoint: DbEndpoint) -> str:
        result = self.client.request("connection.open", {"connection": endpoint.to_payload()})
        if not result.get("success"):
            message = str(result.get("message", "connection failed"))
            code = result.get("error_code") or extract_error_code(message)
            raise DbCoreServiceError(_with_friendly_hint(message, code), error_code=code, payload=result)
        return str(result.get("connection_id", ""))

    def close_connection(self, connection_id: str) -> bool:
        result = self.client.request("connection.close", {"connection_id": connection_id})
        return bool(result.get("success"))

    def inspect_schema(self, endpoint: DbEndpoint) -> Dict[str, Any]:
        result = self.client.request("schema.inspect", {"source": endpoint.to_payload()})
        return result.get("schema") if isinstance(result.get("schema"), dict) else {"tables": []}

    def compare_schemas(
        self,
        source: DbEndpoint,
        target: DbEndpoint,
        level: str = "standard",
        exact_row_counts: bool = False,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """두 MySQL 스키마의 테이블별 차이, 심각도 요약, 동기화 SQL (Rust schema.compare)."""
        payload = {
            "source": source.to_payload(),
            "target": target.to_payload(),
            "level": level,
            "exact_row_counts": exact_row_counts,
        }
        return self.client.request("schema.compare", payload, on_event=on_event)

    def analyze_upgrade(
        self,
        endpoint: DbEndpoint,
        options: Optional[Dict[str, bool]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """MySQL 8.4 업그레이드 호환성 분석 (Rust upgrade.analyze, 읽기 전용)."""
        payload = {"connection": endpoint.to_payload(), "options": dict(options or {})}
        return self.client.request("upgrade.analyze", payload, on_event=on_event)

    def plan_upgrade_fixes(
        self,
        endpoint: DbEndpoint,
        issues: List[Dict[str, Any]],
        charset_tables: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """이슈별 수정 옵션 + 문자셋 수정 대상 테이블 계획 (Rust upgrade.fix_plan, 읽기 전용)."""
        payload = {"connection": endpoint.to_payload(), "issues": list(issues), "charset_tables": sorted(charset_tables)}
        return self.client.request("upgrade.fix_plan", payload)

    def charset_fix_sql(
        self, endpoint: DbEndpoint, tables: Sequence[str], charset: str = "", collation: str = ""
    ) -> Dict[str, Any]:
        """FK 안전 문자셋 변환 SQL (Rust upgrade.charset_sql): FK DROP → CONVERT(부모 먼저) → FK ADD."""
        payload = {"connection": endpoint.to_payload(), "tables": sorted(tables), "charset": charset, "collation": collation}
        return self.client.request("upgrade.charset_sql", payload)

    def catalog(self, connection_id: str, kind: str, **args: str) -> List[str]:
        """열린 세션의 메타데이터 조회 (Rust catalog.query). kind 와 인자는 catalog.rs 참고."""
        result = self.client.request("catalog.query", {"connection_id": connection_id, "kind": kind, **args})
        values = result.get("values")
        return [str(value) for value in values] if isinstance(values, list) else []

    def list_tables(self, endpoint: DbEndpoint) -> List[str]:
        result = self.client.request("schema.list", {"connection": endpoint.to_payload()})
        tables = result.get("tables")
        return [str(table) for table in tables] if isinstance(tables, list) else []

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
        result = self.client.request(
            "query.execute",
            {"connection": endpoint.to_payload(), "sql": sql, "params": list(params or [])},
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
        job_id: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        max_rows: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = {"connection_id": connection_id, "sql": sql, "params": list(params or [])}
        payload.update(_query_control(job_id, timeout_ms, max_rows, max_bytes))
        result = self.client.request("query.execute", payload)
        _raise_if_query_failed(result)
        rows = result.get("rows")
        columns = result.get("columns")
        return {
            "rows": [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [],
            "columns": [str(column) for column in columns] if isinstance(columns, list) else [],
            "rows_affected": int(result.get("rows_affected") or 0),
            "truncated": bool(result.get("truncated")),
            "truncated_by": result.get("truncated_by"),
            "in_transaction": result.get("in_transaction"),
        }

    def export_query_to_file(
        self,
        connection_id: str,
        sql: str,
        output: Dict[str, Any],
        params: Optional[Sequence[Any]] = None,
        job_id: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Re-run `sql` and let the core stream the whole result into `output["path"]`.

        `output` keys: path, format (csv|jsonl), bom, formula_guard, binary (hex|base64),
        keep_partial, overwrite. The core writes `<path>.partial` and renames it only after a
        complete run; cancel/timeout/errors raise `DbCoreServiceError` (its payload reports
        `partial_path` when a partial file was kept) and never leave a final file behind.
        """
        payload: Dict[str, Any] = {
            "connection_id": connection_id,
            "sql": sql,
            "params": list(params or []),
            "output": dict(output),
        }
        payload.update(_query_control(job_id, timeout_ms, None, None))

        def handle_event(event: Dict[str, Any]) -> None:
            if on_progress and event.get("event") == "progress":
                on_progress(int(event.get("rows_written") or 0), int(event.get("bytes_written") or 0))

        result = self.client.request("query.execute", payload, on_event=handle_event)
        _raise_if_query_failed(result)
        return result

    def cancel_query(self, job_id: str) -> Dict[str, Any]:
        """Ask the core to cancel a running query on the server (KILL QUERY / pg cancel)."""
        return self.client.request("query.cancel", {"job_id": job_id})

    def execute_on_connection_streaming(
        self,
        connection_id: str,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        row_batch_size: int = 500,
        on_batch: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        job_id: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        max_rows: Optional[int] = None,
        max_bytes: Optional[int] = None,
        on_columns: Optional[Callable[[List[str]], None]] = None,
    ) -> Dict[str, Any]:
        def handle_event(payload: Dict[str, Any]) -> None:
            event = payload.get("event")
            if event == "columns" and on_columns:
                on_columns([str(column) for column in payload.get("columns") or []])
                return
            if event != "row_batch" or not on_batch:
                return
            rows = payload.get("rows")
            if isinstance(rows, list):
                on_batch([row for row in rows if isinstance(row, dict)])

        payload = {
            "connection_id": connection_id,
            "sql": sql,
            "params": list(params or []),
            "stream_rows": True,
            "row_batch_size": int(row_batch_size),
        }
        payload.update(_query_control(job_id, timeout_ms, max_rows, max_bytes))
        result = self.client.request("query.execute", payload, on_event=handle_event)
        _raise_if_query_failed(result)
        return result

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

    def run_oneclick(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request("oneclick.run", payload, on_event=on_event)

    def promote_dump(self, payload: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        return self.client.request("dump.promote", payload, on_event=on_event)

    def restore_backups(self, payload: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """TF-STATUS-119: list / reconcile / cleanup_plan / cleanup_apply for promotion backups."""
        return self.client.request("restore.backups", payload, on_event=on_event)

    def derive_oneclick_charset_contracts(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request("oneclick.derive_charset_contracts", payload, on_event=on_event)

    def apply_oneclick_fixes(
        self,
        payload: Dict[str, Any],
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        return self.client.request("oneclick.apply_fixes", payload, on_event=on_event)


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
