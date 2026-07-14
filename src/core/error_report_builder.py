"""Construction of strict anonymous error report payloads."""

import hashlib
import json
import re
import threading
import unicodedata
import uuid

from src.core.error_report_environment import collect_environment
from src.core.error_report_sanitizer import sanitize_error_text
from src.core.error_report_schema import REPORT_SCHEMA_VERSION, validate_report_payload


INSTALLATION_ID_SETTING = "error_reporting_installation_id"
_EXCEPTION_CLASS_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_FRAME_MODULE_PATTERN = re.compile(r"^src(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_FRAME_FUNCTION_PATTERN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|<module>)$")
_EMPTY_UUID_V4 = "00000000-0000-4000-8000-000000000000"
_INSTALLATION_ID_LOCK = threading.Lock()
_BASE_EXCEPTION_DICT = BaseException.__dict__["__dict__"]
_BASE_EXCEPTION_TRACEBACK = BaseException.__dict__["__traceback__"]


def _normalized_text(value):
    try:
        return unicodedata.normalize("NFKC", str(value or "")).strip()
    except BaseException:
        return ""


def _canonical_uuid_v4(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    if parsed.version != 4 or str(parsed) != value or value == _EMPTY_UUID_V4:
        return None
    return value


def _installation_id(config_manager):
    with _INSTALLATION_ID_LOCK:
        stored = config_manager.get_app_setting(INSTALLATION_ID_SETTING, None)
        installation_id = _canonical_uuid_v4(stored)
        if installation_id is not None:
            return installation_id
        installation_id = str(uuid.uuid4())
        config_manager.set_app_setting(INSTALLATION_ID_SETTING, installation_id)
        return installation_id


def _normalize_db_server_version(value):
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        parts = value[:2]
        normalized_parts = []
        for part in parts:
            if type(part) is int and part >= 0:
                normalized_parts.append(part)
            elif isinstance(part, str) and re.fullmatch(r"[0-9]+", part):
                normalized_parts.append(int(part))
            else:
                return None
        if len(normalized_parts) != 2:
            return None
        normalized = f"{normalized_parts[0]}.{normalized_parts[1]}"
        return normalized if len(normalized) <= 32 else None

    text = _normalized_text(value)
    match = re.search(r"(?<![0-9])([0-9]+)\.([0-9]+)(?![0-9])", text)
    if match is None:
        return None
    normalized = f"{int(match.group(1))}.{int(match.group(2))}"
    return normalized if len(normalized) <= 32 else None


def _exception_class(exception):
    if exception is None:
        return "RuntimeError"
    if not isinstance(exception, BaseException):
        return "Exception"
    exception_type = type(exception)
    try:
        name = _normalized_text(type.__getattribute__(exception_type, "__name__"))
        module = _normalized_text(
            type.__getattribute__(exception_type, "__module__")
        )
    except BaseException:
        return "Exception"
    if module == "builtins":
        qualified = name
    elif module.startswith("src."):
        qualified = f"{module}.{name}"
    else:
        return "Exception"
    if len(qualified) <= 128 and _EXCEPTION_CLASS_PATTERN.fullmatch(qualified):
        return qualified
    if len(name) <= 128 and _EXCEPTION_CLASS_PATTERN.fullmatch(name):
        return name
    return "Exception"


def _error_code(exception):
    if exception is None or not isinstance(exception, BaseException):
        return None
    exception_type = type(exception)
    try:
        module = _normalized_text(
            type.__getattribute__(exception_type, "__module__")
        )
    except BaseException:
        return None
    if not module.startswith("src."):
        return None
    try:
        attributes = _BASE_EXCEPTION_DICT.__get__(exception, exception_type)
    except BaseException:
        return None
    if type(attributes) is not dict:
        return None
    value = dict.get(attributes, "error_code")
    if value is None:
        value = dict.get(attributes, "code")
    if type(value) not in (str, int):
        return None
    normalized = _normalized_text(value).upper()
    if len(normalized) > 64 or _ERROR_CODE_PATTERN.fullmatch(normalized) is None:
        return None
    return normalized


def _application_frames(exception):
    if exception is None or not isinstance(exception, BaseException):
        return []
    try:
        traceback = _BASE_EXCEPTION_TRACEBACK.__get__(exception, type(exception))
    except BaseException:
        return []
    frames = []
    try:
        while traceback is not None:
            frame = traceback.tb_frame
            module = _normalized_text(dict.get(frame.f_globals, "__name__", ""))
            function = _normalized_text(frame.f_code.co_name)
            line = traceback.tb_lineno
            if (
                len(module) <= 160
                and _FRAME_MODULE_PATTERN.fullmatch(module)
                and len(function) <= 128
                and _FRAME_FUNCTION_PATTERN.fullmatch(function)
                and type(line) is int
                and 1 <= line <= 10_000_000
            ):
                frames.append(
                    {"module": module, "function": function, "line": line}
                )
            traceback = traceback.tb_next
    except BaseException:
        return []
    return frames[-20:]


def _fingerprint(operation_kind, db_engine, exception_class, error_code, frames):
    canonical_input = {
        "app_frame_signature": [
            f"{frame['module']}:{frame['function']}:{frame['line']}"
            for frame in frames
        ],
        "db_engine": db_engine,
        "error_code": error_code or "",
        "exception_class": exception_class,
        "operation_kind": operation_kind,
    }
    canonical_json = json.dumps(
        canonical_input,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_error_report(
    config_manager,
    *,
    operation_kind,
    db_engine,
    phase,
    error_message,
    exception=None,
    db_server_version=None,
) -> dict:
    """Build and validate one schema-v1 privacy-allowlisted report."""

    environment = collect_environment()
    kind = _normalized_text(operation_kind).lower()
    engine = _normalized_text(db_engine).lower()
    normalized_phase = _normalized_text(phase).lower()
    exception_class = _exception_class(exception)
    error_code = _error_code(exception)
    frames = _application_frames(exception)

    operation = {"kind": kind, "db_engine": engine, "phase": normalized_phase}
    normalized_db_version = _normalize_db_server_version(db_server_version)
    if normalized_db_version is not None:
        operation["db_server_version"] = normalized_db_version

    error = {
        "exception_class": exception_class,
        "sanitized_message": sanitize_error_text(error_message),
        "app_frames": frames,
    }
    if error_code is not None:
        error["error_code"] = error_code

    payload = {
        "report": {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "anonymous_installation_id": _installation_id(config_manager),
            "error_fingerprint": _fingerprint(
                kind, engine, exception_class, error_code, frames
            ),
        },
        "app": {
            key: environment["app"][key]
            for key in ("version", "package_kind", "ui_language")
        },
        "system": {
            key: environment["system"][key]
            for key in (
                "os_family",
                "os_version",
                "architecture",
                "locale",
                "utc_offset_minutes",
            )
        },
        "runtime": {
            key: environment["runtime"][key]
            for key in ("python_version", "qt_version", "rust_core_version")
        },
        "operation": operation,
        "error": error,
    }
    return validate_report_payload(payload)
