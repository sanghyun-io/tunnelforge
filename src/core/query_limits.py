"""Result-size and time limits applied to SQL editor executions (TF-STATUS-112)."""
import uuid
from typing import Dict, Optional

DEFAULT_MAX_ROWS = 100_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024

ERROR_QUERY_CANCELLED = "query_cancelled"
ERROR_QUERY_TIMEOUT = "query_timeout"
CANCEL_ERROR_CODES = (ERROR_QUERY_CANCELLED, ERROR_QUERY_TIMEOUT)


def build_query_limits(timeout_seconds: int = 0, max_rows: int = DEFAULT_MAX_ROWS,
                       max_bytes: int = DEFAULT_MAX_BYTES) -> Dict[str, int]:
    """Keyword arguments for `execute_on_connection_*`; a zero timeout means no time limit."""
    limits = {"max_rows": int(max_rows), "max_bytes": int(max_bytes)}
    if timeout_seconds and int(timeout_seconds) > 0:
        limits["timeout_ms"] = int(timeout_seconds) * 1000
    return limits


def truncation_notice(truncated_by: Optional[str], limits: Dict[str, int]) -> str:
    if truncated_by == "bytes":
        mib = limits.get("max_bytes", DEFAULT_MAX_BYTES) // (1024 * 1024)
        return f"결과가 잘렸습니다: 크기 상한 {mib} MiB 초과"
    return f"결과가 잘렸습니다: 행 상한 {limits.get('max_rows', DEFAULT_MAX_ROWS):,}행 초과"


def new_job_id(prefix: str = "sql") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
