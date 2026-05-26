"""Platform-specific filesystem locations for TunnelForge."""
import os
import platform
from pathlib import Path
from typing import Mapping, Optional


APP_NAME = "TunnelForge"
APP_ID = "tunnelforge"


def _platform_name(platform_name: Optional[str] = None) -> str:
    return platform_name or platform.system()


def _home_path(home: Optional[Path] = None) -> Path:
    return Path(home) if home is not None else Path.home()


def _environ(environ: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def app_support_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the directory for persistent app configuration/state."""
    system = _platform_name(platform_name)
    home_path = _home_path(home)
    env = _environ(environ)

    if system == "Windows":
        base = Path(env.get("LOCALAPPDATA") or home_path / "AppData" / "Local")
        return base / APP_NAME
    if system == "Darwin":
        return home_path / "Library" / "Application Support" / APP_NAME

    base = Path(env.get("XDG_CONFIG_HOME") or home_path / ".config")
    return base / APP_ID


def data_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the directory for larger runtime data and resumable state."""
    system = _platform_name(platform_name)
    home_path = _home_path(home)
    env = _environ(environ)

    if system == "Windows":
        base = Path(env.get("LOCALAPPDATA") or home_path / "AppData" / "Local")
        return base / APP_NAME
    if system == "Darwin":
        return home_path / "Library" / "Application Support" / APP_NAME

    base = Path(env.get("XDG_DATA_HOME") or home_path / ".local" / "share")
    return base / APP_ID


def log_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the directory for user-visible log files."""
    system = _platform_name(platform_name)
    home_path = _home_path(home)
    env = _environ(environ)

    if system == "Windows":
        base = Path(env.get("LOCALAPPDATA") or home_path / "AppData" / "Local")
        return base / APP_NAME / "logs"
    if system == "Darwin":
        return home_path / "Library" / "Logs" / APP_NAME

    base = Path(env.get("XDG_STATE_HOME") or home_path / ".local" / "state")
    return base / APP_ID / "logs"


def config_file(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return app_support_dir(platform_name, home, environ) / "config.json"


def encryption_key_file(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return app_support_dir(platform_name, home, environ) / ".encryption_key"


def backups_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return app_support_dir(platform_name, home, environ) / "backups"


def sql_history_file(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return app_support_dir(platform_name, home, environ) / "sql_history.json"


def analysis_dir() -> Path:
    return app_support_dir() / "analysis"


def rollback_dir() -> Path:
    return app_support_dir() / "rollback"

