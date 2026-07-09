"""Structured translation catalog and active-language state.

Owns the ``tr()`` key/value catalog, the process-wide current-language flag,
and language selection (CLI args, installer hint, saved setting, OS locale).
Other i18n submodules must read the active language only through
``current_language()`` / ``set_language()`` so a language switch is reflected
immediately; importing the ``_current_language`` global directly would copy a
stale snapshot at import time.
"""
import locale
import os
from pathlib import Path
from typing import Iterable, Optional

from src.core.platform_paths import app_support_dir


DEFAULT_LANGUAGE = "ko"
SUPPORTED_LANGUAGES = {
    "ko": "한국어",
    "en": "English",
}
INSTALLER_LANGUAGE_HINT_FILE = "installer-language.txt"

_current_language = DEFAULT_LANGUAGE

_TRANSLATIONS = {
    "ko": {
        "app.ready": "준비됨",
        "common.cancel": "취소",
        "common.close": "닫기",
        "common.delete": "삭제",
        "common.edit": "수정",
        "common.ok": "확인",
        "common.save": "저장",
        "common.start": "시작",
        "common.stop": "중지",
        "common.unknown": "알 수 없음",
        "main.add_group": "그룹 추가",
        "main.add_tunnel": "연결 추가",
        "main.db_transition": "DB 전환",
        "main.manage": "관리",
        "main.migration": "마이그레이션",
        "main.name": "이름",
        "main.open": "열기",
        "main.power": "전원",
        "main.quit": "종료",
        "main.schedule": "스케줄",
        "main.schedule_backup": "스케줄 백업",
        "main.schedule_manage": "스케줄 관리...",
        "main.run_now": "즉시 실행",
        "main.schema_diff": "스키마 비교",
        "main.settings": "설정",
        "main.status": "상태",
        "main.target_host": "타겟 호스트",
        "main.default_schema": "기본 스키마",
        "main.title": "터널링 연결 목록",
        "main.local_port": "로컬 포트",
        "tree.connect_all": "모두 연결",
        "tree.db_connect": "DB 연결",
        "tree.delete_group": "그룹 삭제",
        "tree.disconnect_all": "모두 해제",
        "tree.duplicate": "복사하여 새로 만들기",
        "tree.edit_group": "그룹 수정",
        "tree.sql_editor": "SQL 에디터",
        "tree.test_connection": "연결 테스트",
        "tree.ungrouped": "그룹 없음",
        "settings.about": "정보",
        "settings.always_exit": "항상 프로그램 종료",
        "settings.always_minimize": "항상 시스템 트레이로 최소화",
        "settings.ask_every_time": "매번 묻기",
        "settings.auto_reconnect": "연결 끊김 시 자동 재연결",
        "settings.backup_restore": "설정 백업/복원",
        "settings.close_behavior": "창 닫기(X) 버튼 동작",
        "settings.dark_mode": "다크 모드",
        "settings.general": "일반",
        "settings.github_auto_report": "GitHub 이슈 자동 보고",
        "settings.language": "언어",
        "settings.light_mode": "라이트 모드",
        "settings.logs": "로그",
        "settings.max_reconnect_attempts": "최대 재연결 시도 횟수:",
        "settings.reconnect": "터널 자동 재연결",
        "settings.reconnect_description": "연결이 끊어지면 점진적 백오프(1초→60초)를 적용하여 자동으로 재연결을 시도합니다.",
        "settings.restart_note": "일부 화면의 언어는 새 창을 열거나 앱을 재시작하면 완전히 반영됩니다.",
        "settings.startup": "시작 프로그램",
        "settings.startup_auto": "시스템 시작 시 자동 실행",
        "settings.startup_description": "로그인 시 시스템 트레이에 최소화된 상태로 자동 시작됩니다.",
        "settings.system_theme": "시스템 설정 따르기",
        "settings.theme": "테마",
        "settings.theme_label": "화면 테마:",
        "settings.title": "설정",
    },
    "en": {
        "app.ready": "Ready",
        "common.cancel": "Cancel",
        "common.close": "Close",
        "common.delete": "Delete",
        "common.edit": "Edit",
        "common.ok": "OK",
        "common.save": "Save",
        "common.start": "Start",
        "common.stop": "Stop",
        "common.unknown": "Unknown",
        "main.add_group": "Add Group",
        "main.add_tunnel": "Add Connection",
        "main.db_transition": "DB Transition",
        "main.manage": "Manage",
        "main.migration": "Migration",
        "main.name": "Name",
        "main.open": "Open",
        "main.power": "Power",
        "main.quit": "Quit",
        "main.schedule": "Schedule",
        "main.schedule_backup": "Scheduled Backups",
        "main.schedule_manage": "Manage Schedules...",
        "main.run_now": "Run Now",
        "main.schema_diff": "Schema Diff",
        "main.settings": "Settings",
        "main.status": "Status",
        "main.target_host": "Target Host",
        "main.default_schema": "Default Schema",
        "main.title": "Tunnel Connections",
        "main.local_port": "Local Port",
        "tree.connect_all": "Connect All",
        "tree.db_connect": "DB Connect",
        "tree.delete_group": "Delete Group",
        "tree.disconnect_all": "Disconnect All",
        "tree.duplicate": "Duplicate as New",
        "tree.edit_group": "Edit Group",
        "tree.sql_editor": "SQL Editor",
        "tree.test_connection": "Test Connection",
        "tree.ungrouped": "Ungrouped",
        "settings.about": "About",
        "settings.always_exit": "Always exit the app",
        "settings.always_minimize": "Always minimize to system tray",
        "settings.ask_every_time": "Ask every time",
        "settings.auto_reconnect": "Automatically reconnect when disconnected",
        "settings.backup_restore": "Settings Backup/Restore",
        "settings.close_behavior": "Close (X) Button Behavior",
        "settings.dark_mode": "Dark Mode",
        "settings.general": "General",
        "settings.github_auto_report": "Automatic GitHub Issue Reporting",
        "settings.language": "Language",
        "settings.light_mode": "Light Mode",
        "settings.logs": "Logs",
        "settings.max_reconnect_attempts": "Max reconnect attempts:",
        "settings.reconnect": "Tunnel Auto-Reconnect",
        "settings.reconnect_description": "When a connection drops, TunnelForge retries with incremental backoff from 1 to 60 seconds.",
        "settings.restart_note": "Some screens fully apply the language after opening a new window or restarting the app.",
        "settings.startup": "Startup",
        "settings.startup_auto": "Launch automatically at system startup",
        "settings.startup_description": "Start minimized to the system tray when you log in.",
        "settings.system_theme": "Follow system setting",
        "settings.theme": "Theme",
        "settings.theme_label": "Display theme:",
        "settings.title": "Settings",
    },
}


def normalize_language(value: Optional[str]) -> str:
    """Return a supported language code, falling back to Korean."""
    text = (value or "").strip().lower().replace("_", "-")
    if text.startswith("ko"):
        return "ko"
    if text.startswith("en"):
        return "en"
    return DEFAULT_LANGUAGE


def detect_system_language() -> str:
    language, _ = locale.getlocale()
    return normalize_language(language)


def installer_language_hint_path() -> Path:
    return app_support_dir() / INSTALLER_LANGUAGE_HINT_FILE


def read_installer_language_hint() -> Optional[str]:
    path = installer_language_hint_path()
    try:
        if not path.exists():
            return None
        return normalize_language(path.read_text(encoding="utf-8").strip())
    except OSError:
        return None


def consume_installer_language_hint() -> Optional[str]:
    language = read_installer_language_hint()
    if language:
        try:
            installer_language_hint_path().unlink()
        except OSError:
            pass
    return language


def language_from_args(args: Optional[Iterable[str]]) -> Optional[str]:
    if not args:
        return None
    prefixes = ("--language=", "--lang=")
    for arg in args:
        for prefix in prefixes:
            if arg.startswith(prefix):
                return normalize_language(arg[len(prefix):])
    env_value = os.environ.get("TUNNELFORGE_LANGUAGE")
    return normalize_language(env_value) if env_value else None


def current_language() -> str:
    return _current_language


def set_language(language: Optional[str]) -> str:
    global _current_language
    _current_language = normalize_language(language)
    return _current_language


def configure_language(config_manager=None, args: Optional[Iterable[str]] = None) -> str:
    """Load the active language from args, installer hint, settings, then OS locale."""
    explicit = language_from_args(args)
    if explicit:
        language = explicit
    else:
        language = None
        if config_manager is not None:
            get_setting = getattr(config_manager, "get_app_setting", None)
            saved = get_setting("language", None) if get_setting else None
            if saved:
                language = normalize_language(saved)
            else:
                language = consume_installer_language_hint()
        if not language:
            language = detect_system_language()

    language = set_language(language)
    if config_manager is not None:
        get_setting = getattr(config_manager, "get_app_setting", None)
        set_setting = getattr(config_manager, "set_app_setting", None)
        if get_setting and set_setting and normalize_language(get_setting("language", None)) != language:
            set_setting("language", language)
    return language


def tr(key: str) -> str:
    return _TRANSLATIONS.get(_current_language, {}).get(
        key,
        _TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key),
    )


def language_label(language: str) -> str:
    return SUPPORTED_LANGUAGES[normalize_language(language)]
