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


def _platform_base_dir(
    system: str,
    home_path: Path,
    env: Mapping[str, str],
    xdg_env_var: str,
    xdg_default_subdir: str,
) -> Path:
    """Windows(LOCALAPPDATA fallback)와 Linux(XDG env fallback)의 공통 base 디렉토리.

    macOS(Darwin)는 함수마다 서브패스가 다르므로(log_dir은 Library/Logs, 나머지는
    Library/Application Support) 이 helper로 뭉개지 않고 각 호출부가 직접 처리한다.
    """
    if system == "Windows":
        return Path(env.get("LOCALAPPDATA") or home_path / "AppData" / "Local")

    return Path(env.get(xdg_env_var) or home_path / xdg_default_subdir)


def app_support_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the directory for persistent app configuration/state."""
    system = _platform_name(platform_name)
    home_path = _home_path(home)
    env = _environ(environ)

    if system == "Darwin":
        return home_path / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        return _platform_base_dir(system, home_path, env, "XDG_CONFIG_HOME", ".config") / APP_NAME

    return _platform_base_dir(system, home_path, env, "XDG_CONFIG_HOME", ".config") / APP_ID


def data_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the directory for larger runtime data and resumable state."""
    system = _platform_name(platform_name)
    home_path = _home_path(home)
    env = _environ(environ)

    if system == "Darwin":
        return home_path / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        return _platform_base_dir(system, home_path, env, "XDG_DATA_HOME", ".local/share") / APP_NAME

    return _platform_base_dir(system, home_path, env, "XDG_DATA_HOME", ".local/share") / APP_ID


def log_dir(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the directory for user-visible log files."""
    system = _platform_name(platform_name)
    home_path = _home_path(home)
    env = _environ(environ)

    if system == "Darwin":
        return home_path / "Library" / "Logs" / APP_NAME
    if system == "Windows":
        return _platform_base_dir(system, home_path, env, "XDG_STATE_HOME", ".local/state") / APP_NAME / "logs"

    return _platform_base_dir(system, home_path, env, "XDG_STATE_HOME", ".local/state") / APP_ID / "logs"


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


def ssh_host_trust_file(
    platform_name: Optional[str] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return app_support_dir(platform_name, home, environ) / "ssh_host_trust.json"


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
