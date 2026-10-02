"""Allowlisted failure codes for anonymous error reports.

Rust DB Core failures carry a stable code as the ``error_code`` field, as the
leading ``code:`` token of the message, or as a trailing ``(error_code=...)``;
the rest of the message holds table names, SQL and paths and is never
reported. Only codes from the fixed list below and server error numbers in the
exact driver renderings leave the machine.
"""

import re
from typing import Optional

# Stable codes emitted by migration_core (classified_import_error and friends).
RUST_CORE_CODES = frozenset({
    "connection_busy",
    "ddl_preflight_failed",
    "ddl_probe_cleanup_failed",
    "explain_refused",
    "export_invalid",
    "export_requires_read_only",
    "export_session_in_transaction",
    "import_plan_invalid",
    "incompatible_surviving_fk",
    "load_failed",
    "multiple_result_sets_unsupported",
    "mysql_parallel_snapshot_privilege_required",
    "post_load_validation_failed",
    "query_cancelled",
    "query_timeout",
    "read_only_session",
    "safe_promotion",
    "safe_restore_content_invalid",
    "safe_restore_content_mismatch",
    "safe_restore_content_read_failed",
    "target_dependency_preflight_failed",
    "target_not_empty",
    "tls_unavailable",
    "tls_verification_failed",
    "unsupported_objects",
    "view_target_invalid",
})

_LEADING_CODE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]{2,63})\s*:")
_TRAILING_CODE = re.compile(r"\(error_code=([a-z_]+)\)")
# Anchored to the exact renderings Rust Core emits (mysql crate
# `MySqlError { ERROR n (state): ... }`, ddl.rs format_postgres_error
# `...: db error; code=STATE; ...`), so `code=` or `ERROR n` text inside user
# paths or identifiers is never taken for a server code.
_MYSQL_ERROR = re.compile(r"MySqlError \{ ERROR (\d{4,5}) \([0-9A-Z]{5}\): ")
_POSTGRES_SQLSTATE = re.compile(r"\bdb error; code=([0-9A-Z]{5})(?:;|$)")
_CLASSIFIED = re.compile(
    r"^(?:(?P<code>[A-Z_]+)(?::(?P<db1>MYSQL-\d{4,5}|PG-[0-9A-Z]{5}))?|(?P<db2>MYSQL-\d{4,5}|PG-[0-9A-Z]{5}))$"
)


def classify_error_code(error_code, message) -> Optional[str]:
    """Return ``CODE``, ``CODE:MYSQL-1822``, ``CODE:PG-23503`` or a bare server code."""
    text = str(message or "")
    leading = _LEADING_CODE.match(text)
    trailing = _TRAILING_CODE.search(text)
    code = None
    for candidate in (
        error_code,
        leading.group(1) if leading else None,
        trailing.group(1) if trailing else None,
    ):
        if isinstance(candidate, str) and candidate.lower() in RUST_CORE_CODES:
            code = candidate.upper()
            break
    server = None
    if match := _MYSQL_ERROR.search(text):
        server = f"MYSQL-{match.group(1)}"
    elif match := _POSTGRES_SQLSTATE.search(text):
        server = f"PG-{match.group(1)}"
    if code and server:
        return f"{code}:{server}"
    return code or server


def is_classified_error_code(value) -> bool:
    """True only for values ``classify_error_code`` can produce."""
    if not isinstance(value, str) or len(value) > 64:
        return False
    match = _CLASSIFIED.fullmatch(value)
    if match is None:
        return False
    code = match.group("code")
    return code is None or code.lower() in RUST_CORE_CODES
