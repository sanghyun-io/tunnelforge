"""Strict validation for anonymous error report schema version 1."""

import re
import uuid


REPORT_SCHEMA_VERSION = 1

PACKAGE_KINDS = frozenset({"source", "frozen"})
OS_FAMILIES = frozenset({"windows", "macos", "linux"})
ARCHITECTURES = frozenset({"x86_64", "arm64"})
OPERATION_KINDS = frozenset({"export", "import"})
DB_ENGINES = frozenset({"mysql", "postgresql"})
OPERATION_PHASES = frozenset({"dump.run", "dump.import"})

MAX_APP_VERSION_LENGTH = 32
MAX_UI_LANGUAGE_LENGTH = 16
MAX_OS_VERSION_LENGTH = 64
MAX_LOCALE_LENGTH = 16
MAX_RUNTIME_VERSION_LENGTH = 32
MAX_EXCEPTION_CLASS_LENGTH = 128
MAX_ERROR_CODE_LENGTH = 64
MAX_SANITIZED_MESSAGE_LENGTH = 2000
MAX_APP_FRAMES = 20
MAX_FRAME_MODULE_LENGTH = 160
MAX_FRAME_FUNCTION_LENGTH = 128
MAX_SOURCE_LINE = 10_000_000

TOP_LEVEL_FIELDS = frozenset(
    {"report", "app", "system", "runtime", "operation", "error"}
)
REPORT_FIELDS = frozenset(
    {"report_schema_version", "anonymous_installation_id", "error_fingerprint"}
)
APP_FIELDS = frozenset({"version", "package_kind", "ui_language"})
SYSTEM_FIELDS = frozenset(
    {"os_family", "os_version", "architecture", "locale", "utc_offset_minutes"}
)
RUNTIME_FIELDS = frozenset(
    {"python_version", "qt_version", "rust_core_version"}
)
OPERATION_FIELDS = frozenset({"kind", "db_engine", "db_server_version", "phase"})
ERROR_FIELDS = frozenset(
    {"exception_class", "error_code", "sanitized_message", "app_frames"}
)
FRAME_FIELDS = frozenset({"module", "function", "line"})

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOTTED_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_MAJOR_MINOR_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")
_LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z]{2})?$")
_SAFE_OS_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EXCEPTION_CLASS_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_FRAME_MODULE_PATTERN = re.compile(
    r"^src(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_FRAME_FUNCTION_PATTERN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|<module>)$")
_EMPTY_UUID_V4 = uuid.UUID("00000000-0000-4000-8000-000000000000")


class ReportValidationError(ValueError):
    """A stable validation failure suitable for cross-language fixture parity."""

    def __init__(self, message, rejection_class="value", path=None):
        super().__init__(message)
        self.rejection_class = rejection_class
        self.path = path


def _fail(path, rejection_class, detail):
    raise ReportValidationError(
        f"{path}: {detail}", rejection_class=rejection_class, path=path
    )


def _require_object(value, path):
    if not isinstance(value, dict):
        _fail(path, "type", "expected object")
    return value


def _require_exact_keys(value, allowed, required, path):
    unknown = set(value) - allowed
    if unknown:
        first_unknown = sorted(unknown, key=lambda item: str(item))[0]
        _fail(path, "unknown_field", f"unknown field: {first_unknown}")
    missing = required - set(value)
    if missing:
        _fail(path, "required_field", f"required field: {sorted(missing)[0]}")


def _require_string(value, path, *, minimum=1, maximum, pattern=None):
    if not isinstance(value, str):
        _fail(path, "type", "expected string")
    if len(value) < minimum:
        _fail(path, "bounds", "too short")
    if len(value) > maximum:
        _fail(path, "bounds", "too long")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(path, "format", "invalid format")
    return value


def _require_enum(value, allowed, path):
    if not isinstance(value, str):
        _fail(path, "type", "expected string")
    if value not in allowed:
        _fail(path, "enum", "invalid value")
    return value


def _canonical_integer(value, path):
    if isinstance(value, bool):
        _fail(path, "type", "expected integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    _fail(path, "type", "expected integer")


def _require_integer(value, path, *, minimum, maximum):
    integer = _canonical_integer(value, path)
    if integer < minimum or integer > maximum:
        _fail(path, "bounds", "out of range")
    return integer


def _validate_report(value):
    path = "report"
    value = _require_object(value, path)
    _require_exact_keys(value, REPORT_FIELDS, REPORT_FIELDS, path)

    version = _canonical_integer(
        value["report_schema_version"], f"{path}.report_schema_version"
    )
    if version != REPORT_SCHEMA_VERSION:
        _fail(f"{path}.report_schema_version", "version", "unsupported version")

    installation_id = _require_string(
        value["anonymous_installation_id"],
        f"{path}.anonymous_installation_id",
        maximum=36,
    )
    try:
        parsed_id = uuid.UUID(installation_id)
    except (ValueError, AttributeError):
        _fail(f"{path}.anonymous_installation_id", "format", "invalid UUIDv4")
    if (
        parsed_id.version != 4
        or str(parsed_id) != installation_id
        or parsed_id == _EMPTY_UUID_V4
    ):
        _fail(f"{path}.anonymous_installation_id", "format", "invalid UUIDv4")

    fingerprint = _require_string(
        value["error_fingerprint"],
        f"{path}.error_fingerprint",
        minimum=64,
        maximum=64,
        pattern=_SHA256_PATTERN,
    )
    return {
        "report_schema_version": version,
        "anonymous_installation_id": installation_id,
        "error_fingerprint": fingerprint,
    }


def _validate_app(value):
    path = "app"
    value = _require_object(value, path)
    _require_exact_keys(value, APP_FIELDS, APP_FIELDS, path)
    return {
        "version": _require_string(
            value["version"],
            f"{path}.version",
            maximum=MAX_APP_VERSION_LENGTH,
            pattern=_DOTTED_VERSION_PATTERN,
        ),
        "package_kind": _require_enum(
            value["package_kind"], PACKAGE_KINDS, f"{path}.package_kind"
        ),
        "ui_language": _require_string(
            value["ui_language"],
            f"{path}.ui_language",
            maximum=MAX_UI_LANGUAGE_LENGTH,
            pattern=_LANGUAGE_PATTERN,
        ),
    }


def _validate_system(value):
    path = "system"
    value = _require_object(value, path)
    _require_exact_keys(value, SYSTEM_FIELDS, SYSTEM_FIELDS, path)
    return {
        "os_family": _require_enum(
            value["os_family"], OS_FAMILIES, f"{path}.os_family"
        ),
        "os_version": _require_string(
            value["os_version"],
            f"{path}.os_version",
            maximum=MAX_OS_VERSION_LENGTH,
            pattern=_SAFE_OS_VERSION_PATTERN,
        ),
        "architecture": _require_enum(
            value["architecture"], ARCHITECTURES, f"{path}.architecture"
        ),
        "locale": _require_string(
            value["locale"],
            f"{path}.locale",
            maximum=MAX_LOCALE_LENGTH,
            pattern=_LOCALE_PATTERN,
        ),
        "utc_offset_minutes": _require_integer(
            value["utc_offset_minutes"],
            f"{path}.utc_offset_minutes",
            minimum=-840,
            maximum=840,
        ),
    }


def _validate_runtime(value):
    path = "runtime"
    value = _require_object(value, path)
    _require_exact_keys(value, RUNTIME_FIELDS, RUNTIME_FIELDS, path)
    return {
        field: _require_string(
            value[field],
            f"{path}.{field}",
            maximum=MAX_RUNTIME_VERSION_LENGTH,
            pattern=_DOTTED_VERSION_PATTERN,
        )
        for field in ("python_version", "qt_version", "rust_core_version")
    }


def _validate_operation(value):
    path = "operation"
    required = OPERATION_FIELDS - {"db_server_version"}
    value = _require_object(value, path)
    _require_exact_keys(value, OPERATION_FIELDS, required, path)
    validated = {
        "kind": _require_enum(
            value["kind"], OPERATION_KINDS, f"{path}.kind"
        ),
        "db_engine": _require_enum(
            value["db_engine"], DB_ENGINES, f"{path}.db_engine"
        ),
        "phase": _require_enum(
            value["phase"], OPERATION_PHASES, f"{path}.phase"
        ),
    }
    if "db_server_version" in value:
        validated["db_server_version"] = _require_string(
            value["db_server_version"],
            f"{path}.db_server_version",
            maximum=MAX_RUNTIME_VERSION_LENGTH,
            pattern=_MAJOR_MINOR_PATTERN,
        )
    return validated


def _validate_frame(value, index):
    path = f"error.app_frames[{index}]"
    value = _require_object(value, path)
    _require_exact_keys(value, FRAME_FIELDS, FRAME_FIELDS, path)
    return {
        "module": _require_string(
            value["module"],
            f"{path}.module",
            maximum=MAX_FRAME_MODULE_LENGTH,
            pattern=_FRAME_MODULE_PATTERN,
        ),
        "function": _require_string(
            value["function"],
            f"{path}.function",
            maximum=MAX_FRAME_FUNCTION_LENGTH,
            pattern=_FRAME_FUNCTION_PATTERN,
        ),
        "line": _require_integer(
            value["line"], f"{path}.line", minimum=1, maximum=MAX_SOURCE_LINE
        ),
    }


def _validate_error(value):
    path = "error"
    required = ERROR_FIELDS - {"error_code"}
    value = _require_object(value, path)
    _require_exact_keys(value, ERROR_FIELDS, required, path)

    frames = value["app_frames"]
    if not isinstance(frames, list):
        _fail(f"{path}.app_frames", "type", "expected array")
    if len(frames) > MAX_APP_FRAMES:
        _fail(f"{path}.app_frames", "bounds", "too many items")

    validated = {
        "exception_class": _require_string(
            value["exception_class"],
            f"{path}.exception_class",
            maximum=MAX_EXCEPTION_CLASS_LENGTH,
            pattern=_EXCEPTION_CLASS_PATTERN,
        ),
        "sanitized_message": _require_string(
            value["sanitized_message"],
            f"{path}.sanitized_message",
            minimum=0,
            maximum=MAX_SANITIZED_MESSAGE_LENGTH,
        ),
        "app_frames": [_validate_frame(frame, index) for index, frame in enumerate(frames)],
    }
    if "error_code" in value:
        validated["error_code"] = _require_string(
            value["error_code"],
            f"{path}.error_code",
            maximum=MAX_ERROR_CODE_LENGTH,
            pattern=_ERROR_CODE_PATTERN,
        )
    return validated


def validate_report_payload(payload: object) -> dict:
    """Validate and return a newly constructed schema-v1 report dictionary."""

    payload = _require_object(payload, "root")
    _require_exact_keys(payload, TOP_LEVEL_FIELDS, TOP_LEVEL_FIELDS, "root")
    return {
        "report": _validate_report(payload["report"]),
        "app": _validate_app(payload["app"]),
        "system": _validate_system(payload["system"]),
        "runtime": _validate_runtime(payload["runtime"]),
        "operation": _validate_operation(payload["operation"]),
        "error": _validate_error(payload["error"]),
    }
