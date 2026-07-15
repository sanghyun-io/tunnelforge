"""Client facade for the Rust TunnelForge DB core service.

This module is a pure re-export shim kept for backward-compatible imports.
The actual implementation lives in:
- `src.core.db_core_client` (JSONL client + engine/version helpers)
- `src.core.db_core_facade` (DbEndpoint + DbCoreFacade + shared facade lifecycle)
- `src.core.db_core_dbapi_shim` (RustDbConnector/RustDbConnection/RustDbCursor)
"""
from src.core.db_core_client import (
    DB_CORE_STDIN_HIGH_WATER_BYTES,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    MAX_JSONL_FRAME_BYTES,
    REQUIRED_PROCESS_CAPABILITIES,
    DbCoreCallbackError,
    DbCoreGenerationState,
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreRequestResult,
    DbCoreServiceClient,
    DbCoreServiceError,
    SUPPORTED_DB_ENGINES,
    _format_error_event,
    default_database_for_engine,
    has_bootstrap_residual_db_core_clients,
    normalize_db_engine,
    parse_db_version_tuple,
    retry_bootstrap_residual_db_core_clients,
)
from src.core.db_core_dbapi_shim import (
    RustDbConnection,
    RustDbConnector,
    RustDbCursor,
    create_rust_db_connector,
    quote_mysql_ident,
)
from src.core.db_core_facade import (
    DbCoreFacade,
    DbEndpoint,
    get_shared_db_core_facade,
    is_db_core_facade_retained,
    release_db_core_facade_retry,
    retain_db_core_facade_for_retry,
    retry_retained_db_core_facades,
    shutdown_shared_db_core_facade,
)

__all__ = [
    "DbCoreServiceError",
    "DbCoreCallbackError",
    "DbCoreRequestKind",
    "DbCoreOutcome",
    "DbCoreGenerationState",
    "DbCoreRequestResult",
    "MAX_JSONL_FRAME_BYTES",
    "DB_CORE_STDIN_HIGH_WATER_BYTES",
    "REQUIRED_PROCESS_CAPABILITIES",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "_format_error_event",
    "SUPPORTED_DB_ENGINES",
    "parse_db_version_tuple",
    "normalize_db_engine",
    "default_database_for_engine",
    "has_bootstrap_residual_db_core_clients",
    "retry_bootstrap_residual_db_core_clients",
    "DbEndpoint",
    "DbCoreServiceClient",
    "DbCoreFacade",
    "get_shared_db_core_facade",
    "retain_db_core_facade_for_retry",
    "release_db_core_facade_retry",
    "is_db_core_facade_retained",
    "retry_retained_db_core_facades",
    "shutdown_shared_db_core_facade",
    "RustDbConnector",
    "create_rust_db_connector",
    "RustDbConnection",
    "RustDbCursor",
    "quote_mysql_ident",
]
