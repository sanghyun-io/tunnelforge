"""Allowlist-only local environment collection for error reports."""

import locale
import platform
import re
import sys
import unicodedata
from datetime import datetime

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR

from src.core.i18n import current_language
from src.version import __version__


_DOTTED_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_LOCALE_PATTERN = re.compile(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}))?$")
_LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")


def _dotted_version(value, fallback="0.0"):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if _DOTTED_VERSION_PATTERN.fullmatch(text):
        return text
    return fallback


def _os_family():
    family = platform.system().strip().lower()
    mapping = {"windows": "windows", "darwin": "macos", "linux": "linux"}
    if family not in mapping:
        raise ValueError("unsupported operating system for error reporting")
    return mapping[family]


def _architecture():
    architecture = platform.machine().strip().lower()
    if architecture in {"amd64", "x86_64"}:
        return "x86_64"
    if architecture in {"arm64", "aarch64"}:
        return "arm64"
    raise ValueError("unsupported architecture for error reporting")


def _os_version():
    value = unicodedata.normalize("NFKC", str(platform.release() or "")).strip()
    match = re.search(r"[0-9]+(?:\.[0-9]+){0,3}", value)
    if match is None or len(match.group(0)) > 64:
        return "0.0"
    return match.group(0)


def _locale_name():
    try:
        raw_locale = locale.getlocale()[0]
    except (TypeError, ValueError, locale.Error):
        raw_locale = None
    value = unicodedata.normalize("NFKC", str(raw_locale or "")).strip()
    match = _LOCALE_PATTERN.fullmatch(value)
    if not match:
        return "und"
    language, territory = match.groups()
    return language.lower() + (f"_{territory.upper()}" if territory else "")


def _ui_language():
    value = unicodedata.normalize("NFKC", str(current_language() or "")).strip()
    return value if _LANGUAGE_PATTERN.fullmatch(value) else "und"


def _utc_offset_minutes():
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    minutes = int(offset.total_seconds() // 60)
    return max(-840, min(840, minutes))


def collect_environment() -> dict:
    """Collect only schema-approved local application and runtime fields."""

    python_version = ".".join(
        str(part)
        for part in (
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        )
    )
    app_version = _dotted_version(__version__)
    qt_version = _dotted_version(QT_VERSION_STR)
    # Reading this constant proves the binding version is local and numeric without
    # probing the package manager; the report contract intentionally exposes Qt only.
    _dotted_version(PYQT_VERSION_STR)

    return {
        "app": {
            "version": app_version,
            "package_kind": "frozen" if getattr(sys, "frozen", False) else "source",
            "ui_language": _ui_language(),
        },
        "system": {
            "os_family": _os_family(),
            "os_version": _os_version(),
            "architecture": _architecture(),
            "locale": _locale_name(),
            "utc_offset_minutes": _utc_offset_minutes(),
        },
        "runtime": {
            "python_version": _dotted_version(python_version),
            "qt_version": qt_version,
            "rust_core_version": app_version,
        },
    }
