"""Small runtime i18n layer for user-facing app chrome."""
import locale
import os
import re
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
_qt_i18n_installed = False

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
        "settings.connection_pool": "연결 풀",
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
        "Strict restorable dump": "엄격 복원 가능 Dump",
        "Dump compatibility is not checked": "Dump 호환성을 아직 확인하지 않았습니다",
        "복원 불가 Dump": "복원 불가 Dump",
        "제한적 복원 Dump": "제한적 복원 Dump",
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
        "settings.connection_pool": "Connection Pool",
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
        "Strict restorable dump": "Strict restorable dump",
        "Dump compatibility is not checked": "Dump compatibility is not checked",
        "복원 불가 Dump": "Not restorable dump",
        "제한적 복원 Dump": "Limited restorable dump",
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


_EN_TEXT_TRANSLATIONS = {
    "(백업 없음)": "(No backups)",
    "(미설정)": "(Not set)",
    "(복사)": "(Copy)",
    "가져오기": "Import",
    "가져오기 확인": "Import Confirmation",
    "가져오기 실패": "Import Failed",
    "가져오기 완료": "Import Complete",
    "경고": "Warning",
    "고급 설정 열기": "Open Advanced Settings",
    "고아 레코드 검사": "Check Orphan Records",
    "그룹 삭제": "Delete Group",
    "그룹 삭제 실패": "Failed to Delete Group",
    "그룹 생성 실패": "Failed to Create Group",
    "그룹 수정 실패": "Failed to Edit Group",
    "그룹 없음": "Ungrouped",
    "기본 검증: strict row/key/value 비교": "Default verification: strict row/key/value comparison",
    "다른 연결 복사": "Copy from Another Connection",
    "다음": "Next",
    "닫기": "Close",
    "대기 중": "Waiting",
    "데이터 없음": "No Data",
    "로그": "Logs",
    "로그 레벨:": "Log level:",
    "로그 새로고침": "Refresh Logs",
    "로그 없음": "No Logs",
    "로그 초기화": "Clear Logs",
    "로그 폴더 열기": "Open Log Folder",
    "마이그레이션 분석기": "Migration Analyzer",
    "모든 연결 종료": "Close All Connections",
    "분석 필요": "Analysis Required",
    "보관 기간 (일):": "Retention period (days):",
    "결과 보관 기간 (일):": "Result retention period (days):",
    "분:": "Minute:",
    "일:": "Day:",
    "예시:\n  0 3 * * *   = 매일 03:00\n  0 0 * * 0   = 매주 일요일 00:00\n  0 12 1 * *  = 매월 1일 12:00\n  30 6 * * 1-5 = 평일 06:30\n  0 * * * *   = 매시간 정각": "Examples:\n  0 3 * * *   = Daily 03:00\n  0 0 * * 0   = Weekly Sunday 00:00\n  0 12 1 * *  = Monthly day 1 12:00\n  30 6 * * 1-5 = Weekdays 06:30\n  0 * * * *   = Hourly on the hour",
    "비교 시작": "Start Comparison",
    "복구 및 업데이트": "Recovery and Update",
    "복사 완료": "Copy Complete",
    "복원": "Restore",
    "복원 실패": "Restore Failed",
    "복원 완료": "Restore Complete",
    "복원 확인": "Restore Confirmation",
    "삭제 불가": "Cannot Delete",
    "삭제 확인": "Delete Confirmation",
    "새 그룹": "New Group",
    "새로고침": "Refresh",
    "선택 필요": "Selection Required",
    "선택된 파일 없음": "No file selected",
    "설정 가져오기": "Import Settings",
    "설정 내보내기": "Export Settings",
    "설정 파일을 다시 불러왔습니다.": "Settings file has been reloaded.",
    "설정이 저장되었습니다.": "Settings saved.",
    "소스 오류": "Source Error",
    "스케줄 작업 관리": "Schedule Manager",
    "스케줄 작업 수정": "Edit Scheduled Job",
    "스케줄 작업 추가": "Add Scheduled Job",
    "스키마 비교": "Schema Diff",
    "실행": "Run",
    "실행 결과": "Run Result",
    "실행 전": "Before Run",
    "실행 실패": "Run Failed",
    "실행할 SQL이 없습니다.": "There is no SQL to run.",
    "이 Dump는 권장 Import 경로로 진행할 수 없습니다.\n\n{}": "This dump cannot proceed through the recommended import path.\n\n{}",
    "이 Dump는 Import할 수 없습니다.\n\n{}": "This dump cannot be imported.\n\n{}",
    "제한적 복원 Import 확인": "Limited Restore Import Confirmation",
    "Import는 가능하지만, 운영 DB의 완전히 같은 한 시점 백업이라고는 증명되지 않았습니다.": (
        "Import is possible, but this is not proven to be a fully same-point-in-time backup of the production DB."
    ),
    "제한적 복원 Dump - Import 가능, 주의 필요": "Limited restore dump - import possible, caution required",
    "Staging 검증은 진행할 수 있고, 운영 복구용 완전 일관 Dump가 필요하면 권한을 보완한 뒤 다시 Export하세요.": (
        "You can continue with Staging validation. If you need a fully consistent dump for production recovery, add the required privileges and export again."
    ),
    "상세 원인:": "Details:",
    "운영 복구용 완전 일관 Export에 필요한 BACKUP_ADMIN 권한이 없어 제한적 복원 Dump로 저장되었습니다.": (
        "The BACKUP_ADMIN privilege required for a fully consistent production-recovery export is missing, so this was saved as a limited restore dump."
    ),
    "Export 중 원본 DB가 변경되었다면 일부 데이터가 서로 다른 시점일 수 있습니다.": (
        "If the source DB changed during Export, some data may come from different moments."
    ),
    "병렬 Export 작업들이 모두 같은 순간을 기준으로 읽었다고 증명되지 않았습니다.": (
        "It is not proven that all parallel Export tasks read from the same moment."
    ),
    "완전히 같은 시점이 보장되는 병렬 Export 조건을 만들 수 없어 제한적 복원 Dump로 저장되었습니다.": (
        "The conditions for a parallel Export guaranteed to read from one same moment could not be created, so this was saved as a limited restore dump."
    ),
    "일부 테이블은 DB 트랜잭션 보호를 받지 않아 완전 일관 Dump로 볼 수 없습니다.": (
        "Some tables are not protected by DB transactions, so this cannot be treated as a fully consistent dump."
    ),
    "일관성 모드:": "Consistency mode:",
    "자동 (권장): 엄격 시도, 권한 부족 시 제한적 Export": "Auto (recommended): try strict, fall back to limited if privileges are missing",
    "엄격: 같은 시점이 증명되지 않으면 중단": "Strict: stop unless the same point in time is proven",
    "제한적: 빠른 병렬 Export, 같은 시점 보장 없음": "Limited: fast parallel Export, same point in time is not guaranteed",
    "자동은 mysqlsh처럼 모든 테이블을 같은 순간 기준으로 읽는 방식을 먼저 시도합니다. 권한이 부족하면 Export를 실패시키지 않고 제한적 복원 Dump로 저장합니다.": (
        "Auto first tries a mysqlsh-like method that reads every table from the same moment. If privileges are missing, it saves a limited restore dump instead of failing Export."
    ),
    "이 Dump는 제한적 복원 Dump입니다.\n\nImport는 할 수 있지만, Export 중 원본 DB가 변경되었다면 일부 테이블이나 같은 테이블의 일부 조각이 서로 다른 시점일 수 있습니다.\n\n운영 복구용으로 '모든 데이터를 같은 순간 기준으로 읽었다'는 보장이 필요하면 필요 권한을 보완한 뒤 다시 Export하세요.\n\n이 한계를 이해하고 제한적 복원으로 Import를 진행할까요?": (
        "This is a limited restore dump.\n\n"
        "It can be imported, but if the source DB changed during Export, some tables or parts of the same table may come from different moments.\n\n"
        "If production recovery requires proof that all data was read from the same moment, add the required privileges and export again.\n\n"
        "Do you understand this limit and want to continue with limited restore import?"
    ),
    "알 수 없음": "Unknown",
    "알림": "Notice",
    "업데이트": "Update",
    "업데이트 확인": "Check for Updates",
    "업데이트를 확인하는 중입니다...": "Checking for updates...",
    "최신 버전": "latest version",
    "업데이트 사용 가능": "Update Available",
    "연결 복사 - 새 연결 만들기": "Copy Connection - Create New Connection",
    "연결 오류": "Connection Error",
    "연결 정보": "Connection Info",
    "연결 테스트": "Test Connection",
    "연결 풀": "Connection Pool",
    "연결 풀 상태를 모니터링합니다. 연결 풀은 DB 연결을 재사용하여 성능을 향상시킵니다.": "Monitor DB connection pool status. Connection pools improve performance by reusing DB connections.",
    "오류": "Error",
    "완료": "Complete",
    "원인": "Cause",
    "유효한 SQL 쿼리가 없습니다.": "There is no valid SQL query.",
    "이전": "Previous",
    "이름:": "Name:",
    "입력 오류": "Input Error",
    "입력 필요": "Input Required",
    "자동 재연결 설정": "Auto-Reconnect Settings",
    "저장": "Save",
    "저장 실패": "Save Failed",
    "저장 완료": "Save Complete",
    "저장 오류": "Save Error",
    "재시도할 테이블을 선택하세요.": "Select tables to retry.",
    "전체 선택": "Select All",
    "전체 해제": "Deselect All",
    "정보": "About",
    "준비 중...": "Preparing...",
    "즉시 실행": "Run Now",
    "취소": "Cancel",
    "최신 버전 확인 중...": "Checking latest version...",
    "쿼리 히스토리": "Query History",
    "테스트 준비 중...": "Preparing test...",
    "터널 상태": "Tunnel Status",
    "터널 연결": "Tunnel Connection",
    "터널 연결 설정": "Tunnel Connection Settings",
    "터널 테스트": "Tunnel Test",
    "파일 없음": "File Not Found",
    "필수 항목 누락": "Required Field Missing",
    "현재 상태": "Current Status",
    "ℹ️ <b>FK 안전 변경 방식</b>으로 모든 테이블이 일괄 처리됩니다.<br>체크 해제 시 해당 테이블을 건너뜁니다.<br>FK 관계로 인해 연쇄적으로 건너뛰어야 하는 테이블이 있을 수 있습니다.": "ℹ️ All tables are processed in batch using the <b>FK-safe change method</b>.<br>Uncheck a table to skip it.<br>Some tables may need to be skipped together because of FK relationships.",
}

_EN_PHRASE_TRANSLATIONS = {
    "그룹 없음": "Ungrouped",
    "연결 테스트 중": "Testing connection",
    "연결 테스트 중단": "Connection test stopped",
    "연결 테스트 오류": "Connection Test Error",
    "연결 시도 중": "Connecting",
    "연결 종료": "Disconnected",
    "자동 연결 완료": "Auto-Connect Complete",
    "자동 연결 성공": "Auto-connect succeeded",
    "자동 연결 스킵": "Auto-connect skipped",
    "터널 설정을 찾을 수 없음": "Tunnel setting not found",
    "터널이 이미 연결되어 있습니다.": "The tunnel is already connected.",
    "터널 연결 테스트 성공": "Tunnel connection test succeeded",
    "연결 테스트 중 오류 발생": "An error occurred during connection test",
    "연결이 생성되었습니다.": "Connection created.",
    "연결되었습니다.": "connected.",
    "백업이 완료되었습니다.": "Backup completed.",
    "새로운 버전": "New version",
    "이 사용 가능합니다.": "is available.",
    "설정에서 다운로드할 수 있습니다.": "You can download it in Settings.",
    "DB Engine이 설정되어 있지 않습니다.": "DB Engine is not configured.",
    "연결 설정에서 MySQL 또는 PostgreSQL을 먼저 선택해주세요.": "Select MySQL or PostgreSQL in connection settings first.",
    "그룹을 삭제하시겠습니까?": "Do you want to delete this group?",
    "그룹에 속한 터널은 '그룹 없음'으로 이동됩니다.": "Tunnels in this group will move to 'Ungrouped'.",
    "앱 시작 시 자동으로 업데이트 확인": "Automatically check for updates on app startup",
    "자동으로 GitHub 이슈 생성": "automatically create a GitHub issue",
    "GitHub 이슈 자동 보고": "Automatic GitHub Issue Reporting",
    "GitHub App이 설정되어 있습니다.": "GitHub App is configured.",
    "GitHub App이 설정되지 않았습니다.": "GitHub App is not configured.",
    "오류 발생 시 자동으로 이슈를 생성하거나, 유사한 이슈가 있으면 코멘트를 추가합니다.": "When errors occur, TunnelForge creates an issue or comments on a similar existing issue.",
    "환경변수 또는 내장 설정이 필요합니다.": "Environment variables or embedded settings are required.",
    "백업 목록": "Backup List",
    "최근 5개": "latest 5",
    "설정 백업/복원": "Settings Backup/Restore",
    "설정을 복원하시겠습니까?": "Do you want to restore settings?",
    "현재 설정은 자동으로 백업됩니다.": "Current settings will be backed up automatically.",
    "앱을 재시작하면 변경사항이 적용됩니다.": "Restart the app to apply changes.",
    "설정이 복원되었습니다": "Settings restored",
    "설정이 내보내기되었습니다": "Settings exported",
    "설정 파일이 없습니다.": "Settings file does not exist.",
    "내보내기 경로가 비어 있습니다.": "Export path is empty.",
    "현재 설정 파일과 동일한 경로로는 내보낼 수 없습니다.": "Cannot export to the current settings file path.",
    "스키마를 선택하세요.": "Select a schema.",
    "출력 폴더를 선택하세요.": "Select an output folder.",
    "최소 하나의 테이블을 선택하세요.": "Select at least one table.",
    "최소 하나의 옵션은 선택되어야 합니다.": "At least one option must be selected.",
    "사용자명을 입력하세요.": "Enter a username.",
    "DB 사용자명을 입력해주세요.": "Enter a DB username.",
    "DB 비밀번호를 입력해주세요.": "Enter a DB password.",
    "DB Engine을 먼저 선택해주세요.": "Select DB Engine first.",
    "DB Engine을 선택해주세요.": "Select DB Engine.",
    "터널 엔진이 초기화되지 않았습니다.": "Tunnel engine is not initialized.",
    "터널 설정에서 DB 사용자/비밀번호를 저장해주세요.": "Save DB username/password in tunnel settings.",
    "DB 자격 증명이 저장되어 있지 않습니다.": "DB credentials are not saved.",
    "실행 중인 터널은 수정할 수 없습니다.": "Running tunnels cannot be edited.",
    "먼저 연결을 중지해주세요.": "Stop the connection first.",
    "실행 중인 터널은 삭제할 수 없습니다.": "Running tunnels cannot be deleted.",
    "연결 설정을 삭제하시겠습니까?": "Do you want to delete this connection setting?",
    "터널이 연결되어 있지 않습니다.": "The tunnel is not connected.",
    "터널을 시작하시겠습니까?": "Do you want to start the tunnel?",
    "터널 시작 실패": "Failed to start tunnel",
    "터널 연결에 실패했습니다.": "Failed to connect tunnel.",
    "직접 연결 모드": "Direct connection mode",
    "SSH 터널": "SSH tunnel",
    "스키마 목록 로드 실패": "Failed to load schema list",
    "분석 중 오류 발생": "An error occurred during analysis",
    "파일 저장 실패": "Failed to save file",
    "파일 불러오기 실패": "Failed to load file",
    "찾을 수 없음": "not found",
    "클립보드에 복사되었습니다.": "Copied to clipboard.",
    "로그 폴더가 아직 생성되지 않았습니다.": "Log folder has not been created yet.",
    "모든 연결 풀이 종료되었습니다.": "All connection pools have been closed.",
    "풀 상태를 새로고침하려면": "To refresh pool status",
    "새 버전 다운로드": "Download New Version",
    "다운로드 완료": "Download complete",
    "다운로드한 패키지를 여시겠습니까?": "Do you want to open the downloaded TunnelForge package?",
    "다운로드한 패키지를 열면 현재 앱이 종료됩니다.": "Opening the downloaded package will close the current app.",
    "설치를 위해 현재 앱이 종료됩니다.": "The current app will close for installation.",
    "시작 프로그램 설정 중 오류가 발생했습니다": "An error occurred while configuring startup",
    "프로그램 종료": "Exit Program",
    "프로그램을 어떻게 처리하시겠습니까?": "What do you want to do with the program?",
    "시스템 트레이로 최소화": "Minimize to system tray",
    "백그라운드 실행": "run in background",
    "프로그램 완전 종료": "Exit program completely",
    "이 선택을 기억하고 다시 묻지 않기": "Remember this choice and do not ask again",
    "생성": "Create",
    "그룹 수정": "Edit Group",
    "이름을 입력하세요.": "Enter a name.",
    "스케줄을 설정하세요.": "Set a schedule.",
    "잘못된 Cron 표현식입니다.": "Invalid Cron expression.",
    "터널을 선택하세요.": "Select a tunnel.",
    "SQL 쿼리를 입력하세요.": "Enter an SQL query.",
    "결과 저장 경로를 선택하세요.": "Select a result save path.",
    "검색": "Search",
    "초기화": "Reset",
    "히스토리 로딩 중": "Loading history",
    "더 보기": "Load More",
    "즐겨찾기": "Favorite",
    "성공만": "Successful only",
    "실패만": "Failed only",
    "날짜 적용": "Use date filter",
    "자동 커밋": "Auto Commit",
    "트랜잭션": "Transaction",
    "커밋": "Commit",
    "롤백": "Rollback",
    "실행 로그": "Execution Log",
    "상세": "Details",
    "복사": "Copy",
    "삭제": "Delete",
    "전체 삭제": "Delete All",
    "읽기 전용": "Read Only",
    "헤더 포함 복사": "Copy with Headers",
    "변경사항 취소": "Discard Changes",
    "스키마 비교 실패": "Schema comparison failed",
    "동기화 스크립트": "Sync Script",
    "클립보드에 복사": "Copy to Clipboard",
    "파일로 저장": "Save to File",
    "위험 작업": "Dangerous Operation",
    "이 작업은 되돌릴 수 없습니다.": "This operation cannot be undone.",
    "계속하려면": "To continue",
    "스키마명을 정확히 입력": "enter the schema name exactly",
    "스키마명 입력": "Enter schema name",
    "환경": "Environment",
    "작업을 실행하려 합니다.": "operation is about to run.",
    "작업을 실행하시겠습니까?": "Do you want to run this operation?",
    "복사하여 새로 만들기": "Duplicate as New",
    "고아 레코드": "Orphan Records",
    "SQL 에디터": "SQL Editor",
    "다이얼로그": "Dialog",
    "로그인": "Login",
    "프로그레스": "Progress",
    "DB 전환 마법사": "DB Transition Wizard",
    "스키마 검사 결과": "Schema Inspection Result",
    "Source 자동 검사를 실행해 Rust Core가 구조를 분석해야 합니다. 완료되면 다음 단계로 이동할 수 있습니다.": "Run automatic Source inspection so Rust Core can analyze the structure. When it completes, you can move to the next step.",
    "Source DB를 검사하면 Rust Core가 정규화한 스키마가 자동으로 채워집니다.": "Inspect the Source DB to automatically fill the schema normalized by Rust Core.",
    "아직 Source 구조를 분석하지 않았습니다.": "Source structure has not been analyzed yet.",
    "고급 설정: schema JSON 및 수동 검사 도구 보기": "Advanced settings: show schema JSON and manual inspection tools",
    "JSON 불러오기": "Load JSON",
    "Source 구조 분석 시작": "Start Source Structure Analysis",
    "선택한 Source DB를 Rust Core로 분석합니다.": "Analyze the selected Source DB with Rust Core.",
    "승인 및 전환 실행": "Approve and Run Transition",
    "대상 schema 이름을 정확히 입력해야 DB 변경 실행이 활성화됩니다.": "Enter the target schema name exactly to enable DB changes.",
    "Target schema 이름 입력": "Enter target schema name",
    "전환 가능 여부 점검": "Transition Preflight Check",
    "아직 전환 가능 여부를 점검하지 않았습니다.": "Transition preflight has not run yet.",
    "Target 상태를 아직 확인하지 않았습니다.": "Target status has not been checked yet.",
    "전환 가능 여부 점검의 최근 진행 상황이 표시됩니다.": "Recent transition preflight progress is shown here.",
    "DB 변경 실행 직전에 기존 Target 테이블 정리": "Clean existing Target tables right before DB changes",
    "DB 변경 실행 직전에 기존 Target 테이블을 정리하도록 계획할 수 있습니다. 실행 버튼을 누르기 전까지 DB는 변경되지 않습니다.": "You can plan cleanup of existing Target tables immediately before DB changes. The database is not changed until you press Run.",
    "대상 DB에 스키마 생성과 데이터 적재를 실행합니다.": "Create schema and load data into the target DB.",
    "저장된 상태부터 대상 DB 변경 작업을 재개합니다.": "Resume target DB changes from saved state.",
    "실패한 전환에서 생성된 Target 테이블을 정리합니다.": "Clean Target tables created by a failed transition.",
    "작업 순서": "Workflow",
    "DB 변경 실행은 사전 점검 또는 계획 생성 성공 후 활성화됩니다.": "DB changes are enabled after preflight or plan creation succeeds.",
    "스키마 검사": "Schema Inspection",
    "사전 점검": "Preflight",
    "양방향 점검": "Bidirectional Check",
    "상세 가이드": "Detailed Guide",
    "계획 생성": "Create Plan",
    "DB 변경 실행": "Run DB Changes",
    "중단 지점부터 재개": "Resume from Checkpoint",
    "실패한 전환 정리": "Clean Failed Transition",
    "검증": "Verify",
    "실행 계획 확인": "Review Execution Plan",
    "아직 실행 계획을 생성하지 않았습니다.": "No execution plan has been created yet.",
    "실행 계획 생성에 실패했습니다. 다시 계획 생성을 실행해 주세요.": "Failed to create execution plan. Run plan creation again.",
    "검증 및 결과 저장": "Verify and Save Result",
    "검증 대기 중": "Waiting for verification",
    "검증 완료": "Verification Complete",
    "검증 완료: 불일치 확인 필요": "Verification Complete: Mismatches Need Review",
    "검증 실패": "Verification Failed",
    "불일치 확인 필요": "Mismatches Need Review",
    "검증 rows": "Verification rows",
    "검증 진행 상황이 표시됩니다.": "Verification progress is shown here.",
    "검증 실행 후 mismatch 예시와 요약이 표시됩니다.": "Mismatch examples and summary are shown after verification.",
    "승인 필요": "Approval Required",
    "Target schema 이름을 정확히 입력해야 DB 변경을 실행할 수 있습니다.": "Enter the Target schema name exactly to run DB changes.",
    "Target schema 이름을 정확히 입력해야 실패한 전환 정리를 실행할 수 있습니다.": "Enter the Target schema name exactly to clean a failed transition.",
    "다음 DB 변경 실행 전에 Target 정리를 수행합니다. Target schema 이름을 확인한 뒤 DB 변경 실행을 다시 눌러 주세요.": "Target cleanup will run before the next DB change. Confirm the Target schema name, then press Run DB Changes again.",
    "DB 전환 결과 저장": "Save DB Transition Result",
    "DB 변경 실패": "DB Change Failed",
    "사용할 수 있습니다.": "can be used.",
    "터널 연결 정보에서 자동 인식됩니다.": "Detected automatically from tunnel connection info.",
    "MySQL은 database, PostgreSQL은 schema": "MySQL uses database, PostgreSQL uses schema.",
    "기존 연결": "Existing connection",
    "터널 목록에서 선택": "Select from tunnel list",
    "Target에 기존 테이블 또는 데이터가 있습니다. 기본 설정에서는 빈 Target만 전환을 실행할 수 있습니다.": "Target has existing tables or data. By default, transitions can run only against an empty Target.",
    "Target 정리를 실행 직전에 수행하도록 계획했습니다. 실행 버튼을 누르기 전까지 DB는 변경되지 않습니다.": "Target cleanup is planned immediately before execution. The database is not changed until you press Run.",
    "Rust Core 검사 완료": "Rust Core inspection complete",
    "검증 통과: Source와 Target 데이터가 일치합니다.": "Validation passed: Source and Target data match.",
    "검증 실패: Rust Core가 비교 차이 상세를 반환하지 않았습니다.": "Verification failed: Rust Core did not return comparison details.",
    "검증 실패: 새 검증 결과를 받지 못했습니다.": "Verification failed: no new verification result was received.",
    "입력 정보가 변경되어 새 검증이 필요합니다.": "Input information changed; run verification again.",
    "Row count 차이": "Row count difference",
    "차이 유형": "Difference type",
    "변경 내용": "Changes",
    "다음 행동": "Next action",
    "PostgreSQL 오류 코드": "PostgreSQL error code",
    "데이터 적재 후 생성": "created after data load",
    "데이터 적재": "data load",
    "실패한 전환 정리가 완료되었습니다. 전환 가능 여부 점검을 다시 실행하세요.": "Failed transition cleanup is complete. Run transition preflight again.",
    "Target 정리 후 전환 가능 여부 점검부터 다시 실행하세요.": "After Target cleanup, rerun transition preflight.",
    "Target 정리 완료: 전환 가능 여부 점검을 다시 실행하세요.": "Target cleanup complete: run transition preflight again.",
    "Target 정리가 완료되었습니다. 다시 점검해 빈 Target 상태를 확인하세요.": "Target cleanup is complete. Run checks again to confirm the Target is empty.",
    "재개 상태 없음": "No Resume State",
    "저장된 재개 상태가 없습니다.": "There is no saved resume state.",
    "DB 변경 준비 중": "Preparing DB changes",
    "정리 준비 중": "Preparing cleanup",
    "검증 준비 중": "Preparing verification",
    "DB 변경 완료": "DB Change Complete",
    "DB 변경이 완료되었습니다. 다음 단계에서 검증을 실행하세요.": "DB changes are complete. Run verification in the next step.",
    "DB 전환 작업이 진행 중입니다. 종료하시겠습니까?": "A DB transition is in progress. Do you want to close?",
    "사용 가능한 터널 항목 없음": "No available tunnel entries",
    "불러오기 실패": "Load Failed",
    "DB 변경 잠김": "DB Changes Locked",
    "사전 점검 또는 계획 생성이 성공한 뒤에 DB 변경 실행을 사용할 수 있습니다.": "Run DB Changes is available after preflight or plan creation succeeds.",
    "DB 변경 실패: Rust Core가 상세 실패 원인을 반환하지 않았습니다.": "DB change failed: Rust Core did not return a detailed failure reason.",
    "점검이 통과되었습니다. Target schema 이름 입력 후 DB 변경 실행을 사용할 수 있습니다.": "Checks passed. Enter the Target schema name to use Run DB Changes.",
    "작업 진행 중": "Operation in Progress",
    "이미 실행 중인 작업이 있습니다.": "An operation is already running.",
    "연결 방식": "Connection Mode",
    "활성 터널 사용": "Use Active Tunnel",
    "직접 입력": "Manual Input",
    "DB 사용자명": "DB username",
    "DB 이름": "DB name",
    "설정 접기": "Collapse Settings",
    "설정 펼치기": "Expand Settings",
    "Export 유형": "Export Type",
    "FK 의존성 테이블 자동 포함": "Automatically include FK dependency tables",
    "병렬 스레드": "Parallel threads",
    "압축 방식": "Compression",
    "출력 폴더 설정": "Output Folder Settings",
    "자동 지정": "Automatic",
    "수동 지정": "Manual",
    "폴더명 입력": "Enter folder name",
    "진행 상황": "Progress",
    "처리 rows": "Processed rows",
    "예상 전체": "Estimated total",
    "기본 위치": "Default location",
    "폴더 이름": "Folder name",
    "최종 경로": "Final path",
    "설치 가이드 보기": "View Install Guide",
    "Rust DB Core 설치 가이드": "Rust DB Core Install Guide",
    "Rust DB Core Export (병렬 처리)": "Rust DB Core Export (Parallel)",
    "Rust DB Core Import (병렬 처리)": "Rust DB Core Import (Parallel)",
    "Rust DB Core dump 압축 방식입니다. zstd는 디스크 사용량을 줄이고 import 시 스트리밍 해제됩니다.": "Rust DB Core dump compression method. zstd reduces disk usage and is streamed during import.",
    "Export 준비 중": "Preparing export",
    "Export 완료 후 로그를 파일로 저장할 수 있습니다.": "You can save the log to a file after export completes.",
    "Export 기본 폴더 선택": "Select Export Base Folder",
    "Export 계획 수립 완료": "Export plan created",
    "Export가 완료되었습니다.": "Export completed.",
    "Export 실패": "Export failed",
    "텍스트 파일": "Text files",
    "모든 파일": "All files",
    "대상 스키마": "Target Schema",
    "원본 스키마명 사용": "Use source schema name",
    "타임존 설정": "Timezone Settings",
    "자동 감지 및 보정": "Auto-detect and adjust",
    "강제 KST": "Force KST",
    "강제 UTC": "Force UTC",
    "설정 안 함": "Do not set",
    "서버 기본값": "server default",
    "증분 Import": "Incremental Import",
    "전체 교체 Import": "Full Replacement Import",
    "안전 재생성 Import": "Safe Recreate Import",
    "기존 데이터 유지, 새로운 것만 추가": "Keep existing data and add only new items",
    "중복 객체가 있으면 오류 발생": "Duplicate objects cause errors",
    "모든 객체": "All objects",
    "재생성": "recreate",
    "테이블 구조와 데이터를 Rust DB Core로 복원\n   ⚠️ 뷰/프로시저/트리거/이벤트는 자동 복원 대상 아님": "Restore table structure and data with Rust DB Core\n   ⚠️ Views/procedures/triggers/events are not automatically restored",
    "임시 스키마에 먼저 복원 후 성공 시 전환": "Restore to a temporary schema first, then switch after success",
    "전환 시 대상 DB 교체, 추가 여유 공간 필요": "Target DB is replaced during switch; extra free space is required",
    "대상 MySQL 저장소가 다른 드라이브나 서버에 있으면 실제 필요 공간은 달라질 수 있습니다.": "If the target MySQL storage is on another drive or server, the actual required space may differ.",
    "덤프 폴더 크기: {}": "Dump folder size: {}",
    "권장 여유 공간(대략): {} 이상": "Recommended free space (approx.): at least {}",
    "현재 덤프 드라이브 여유 공간: {}": "Free space on the current dump drive: {}",
    "주의: 현재 확인 가능한 여유 공간이 권장치보다 작습니다.": "Warning: the currently measurable free space is below the recommendation.",
    "현재 드라이브 여유 공간은 확인할 수 없습니다.": "Current drive free space could not be checked.",
    "{}\n\n계속 진행할까요?": "{}\n\nContinue?",
    "호환성 검사 중": "Checking compatibility",
    "MySQL 8.4 호환성 이슈 상세": "MySQL 8.4 Compatibility Issue Details",
    "MySQL 8.4 호환성 검사": "MySQL 8.4 Compatibility Check",
    "Dump 폴더를 선택하면 자동 검사됩니다.": "Select a Dump folder to run automatic checks.",
    "상세 보기": "View Details",
    "권장": "Recommended",
    "병합": "Merge",
    "뷰": "views",
    "프로시저": "procedures",
    "이벤트": "events",
    "Export → Import 시 권장": "Recommended for Export to Import",
    "데이터": "Data",
    "대기 중": "Waiting",
    "Import 완료 후 로그를 파일로 저장할 수 있습니다.": "You can save the log to a file after import completes.",
    "Import 준비 중": "Preparing import",
    "Import가 완료되었습니다.": "Import completed.",
    "Import 중 오류가 발생했습니다.": "An error occurred during import.",
    "실패한 테이블을 선택하여 재시도할 수 있습니다.": "Select failed tables to retry.",
    "심각도": "Severity",
    "유형": "Type",
    "위치": "Location",
    "설명": "Description",
    "권장 조치": "Recommended Action",
    "재시도 확인": "Retry Confirmation",
    "선택한 테이블을 재시도하시겠습니까?": "Do you want to retry the selected tables?",
    "선택한 테이블 재시도": "Retry Selected Tables",
    "실패한 테이블 모두 선택": "Select All Failed Tables",
    "저장할 로그가 없습니다.": "There is no log to save.",
    "로그가 저장되었습니다.": "Log saved.",
    "Dump 폴더를 선택하세요.": "Select a Dump folder.",
    "폴더가 존재하지 않습니다.": "The folder does not exist.",
    "DB 연결에 실패했습니다:": "Failed to connect DB:",
    "터널이 활성화되어 있지 않습니다.": "The tunnel is not active.",
    "서버가 지역명 타임존을 지원합니다.": "The server supports named time zones.",
    "서버가 지역명 타임존을 지원하지 않습니다.": "The server does not support named time zones.",
    "'Asia/Seoul' 에러 방지를 위해 타임존을 '+09:00'으로 자동 보정합니다.": "Timezone is adjusted to '+09:00' to avoid 'Asia/Seoul' errors.",
    "타임존을 강제로 '+09:00' (KST)로 설정합니다.": "Force timezone to '+09:00' (KST).",
    "타임존을 강제로 '+00:00' (UTC)로 설정합니다.": "Force timezone to '+00:00' (UTC).",
    "타임존 지원 여부 확인 중": "Checking timezone support",
    "호환성 검사 통과": "Compatibility check passed",
    "호환성 이슈": "Compatibility issues",
    "호환성 경고": "Compatibility warnings",
    "검사 실패": "Check failed",
    "쿼리 복사": "Copy Query",
    "전체 쿼리 내보내기": "Export All Queries",
    "보고서 저장": "Save Report",
    "분석 중": "Analyzing",
    "발견된 고아 관계": "Detected Orphan Relationships",
    "상세 정보 / SQL 쿼리": "Details / SQL Query",
    "스키마를 선택해주세요.": "Select a schema.",
    "고아 레코드가 발견되지 않았습니다.": "No orphan records were found.",
    "모든 FK 관계가 정상입니다.": "All FK relationships are valid.",
    "쿼리가 저장되었습니다.": "Query saved.",
    "보고서가 저장되었습니다.": "Report saved.",
    "로그 저장 중 오류가 발생했습니다.": "An error occurred while saving the log.",
    "쿼리 저장 중 오류가 발생했습니다.": "An error occurred while saving the query.",
    "검증 통과": "Validation passed",
    "소스 터널": "Source tunnel",
    "타겟 터널": "Target tunnel",
    "빠른 비교": "fast comparison",
    "표준": "standard",
    "엄격": "strict",
    "비교 수준": "Comparison level",
    "테이블 목록": "Table list",
    "테이블/항목": "Table/Item",
    "행 수": "Rows",
    "동일": "Unchanged",
    "주의": "Warning",
    "이 스크립트를 실행하기 전에 반드시 타겟 데이터베이스를 백업하세요!": "Back up the target database before running this script.",
    "이 스크립트는 테이블 구조(DDL)만 동기화합니다.": "This script synchronizes table structure (DDL) only.",
    "데이터는 복사되지 않습니다. 데이터 이전은 Export/Import 기능을 사용하세요.": "Data is not copied. Use Export/Import to move data.",
    "스크립트가 클립보드에 복사되었습니다.": "Script copied to clipboard.",
    "스크립트 저장": "Save Script",
    "스크립트가 저장되었습니다:": "Script saved:",
    "모든 연결 정보를 선택하세요.": "Select all connection information.",
    "유효한 스키마를 선택하세요.": "Select a valid schema.",
    "타겟 오류": "Target Error",
    "Critical 이슈 감지": "Critical Issue Detected",
    "Critical 이슈": "Critical issues",
    "건이 발견되었습니다.": "items were found.",
    "Import 실패 위험이 있는 변경 사항이 포함되어 있습니다.": "Changes include Import failure risk.",
    "그래도 동기화 스크립트를 생성하시겠습니까?": "Do you still want to create the sync script?",
    "마이그레이션 자동 수정 위저드": "Migration Auto-Fix Wizard",
    "수정할 이슈 선택": "Select Issues to Fix",
    "자동 수정을 적용할 호환성 이슈를 선택하세요.": "Select compatibility issues for automatic fixes.",
    "필터": "Filter",
    "문자셋 변경 대상 테이블": "Tables for Charset Change",
    "FK 안전 변경 방식으로 일괄 처리됩니다. (FK DROP → charset 변경 → FK 재생성)": "Processed in batch using the FK-safe change method (FK DROP -> charset change -> FK recreate).",
    "대상 테이블": "Target Tables",
    "전체 일괄 옵션 적용": "Apply Batch Options to All",
    "이슈 유형별로 기본 옵션을 선택하세요.": "Select default options by issue type.",
    "선택한 옵션이 해당 유형의 모든 이슈에 적용됩니다.": "The selected option applies to all issues of that type.",
    "적용": "Apply",
    "자동 포함된 테이블 목록": "Automatically Included Tables",
    "다음": "Next",
    "이전": "Back",
    "완료": "Finish",
    "테이블명": "Table Name",
    "포함 원인": "Included By",
    "각 이슈에 대한 수정 방법을 선택하세요.": "Select a fix method for each issue.",
    "문자셋 이슈는 이전 단계에서 처리됨": "charset issues are handled in the previous step",
    "전체 일괄 적용": "Apply to All",
    "모든 이슈에 동일한 옵션을 일괄 적용합니다": "Apply the same option to all issues",
    "자동 포함된 테이블": "Automatically Included Tables",
    "Target 상태 확인 완료: 기존 테이블 또는 데이터 차단 이슈가 없습니다.": "Target status check complete: no existing tables or data blockers were found.",
    "고급 설정 닫기": "Close Advanced Settings",
    "계산 중": "Calculating",
    "활성 터널 없음": "No active tunnel",
    "터널명": "tunnel name",
    "쿼리가 클립보드에 복사되었습니다.": "Query copied to clipboard.",
    "쿼리 저장": "Save Query",
    "대상 스키마를 선택하세요.": "Select a target schema.",
    "소스": "Source",
    "타겟": "Target",
    "마이그레이션 분석기": "Migration Analyzer",
    "한 번의 클릭으로 MySQL 8.0 → 8.4 마이그레이션을 자동 수행합니다.": "Run the MySQL 8.0 to 8.4 migration automatically with one click.",
    "사전 검사 → 분석 → 자동 수정 → 검증까지 전 과정을 자동화합니다.": "Automates preflight, analysis, auto-fix, and verification.",
    "결과 불러오기": "Load Results",
    "개요": "Overview",
    "호환성": "Compatibility",
    "FK 관계": "FK Relationships",
    "분석을 시작하세요.": "Start analysis.",
    "항목": "Item",
    "값": "Value",
    "자식 테이블": "Child Table",
    "자식 컬럼": "Child Column",
    "부모 테이블": "Parent Table",
    "부모 컬럼": "Parent Column",
    "고아 수": "Orphan Count",
    "샘플 값": "Sample Values",
    "정리 작업": "Cleanup Action",
    "NULL로 설정": "Set to NULL",
    "정리할 레코드를 선택하면 SQL이 표시됩니다...": "Select records to clean and SQL will be shown...",
    "미리보기": "Preview",
    "조회쿼리 복사": "Copy Select Query",
    "조회쿼리 저장": "Save Select Query",
    "선택된 고아 레코드의 조회 쿼리를 클립보드에 복사": "Copy the selected orphan-record query to the clipboard",
    "모든 고아 레코드 조회 쿼리를 .sql 파일로 저장": "Save all orphan-record queries to a .sql file",
    "자동 수정 위저드": "Auto-Fix Wizard",
    "자동 수정 가능한 이슈를 대화형 위저드로 수정합니다.": "Fix auto-fixable issues with an interactive wizard.",
    "수동 처리 가이드": "Manual Handling Guide",
    "자동 수정이 불가능한 이슈에 대한 수동 처리 가이드를 제공합니다.": "Provides a manual handling guide for issues that cannot be auto-fixed.",
    "테이블 (부모 → 자식)": "Tables (Parent -> Child)",
    "고아 레코드 조회 쿼리 저장": "Save Orphan Record Query",
    "작업 완료": "Operation Complete",
    "분석 결과 불러오기": "Load Analysis Result",
    "이슈 목록": "Issue List",
    "가이드": "Guide",
    "조치": "Action",
    "FK 관계가 없습니다.": "There are no FK relationships.",
    "복사할 고아 레코드를 선택하세요.": "Select orphan records to copy.",
    "내보낼 고아 레코드가 없습니다.": "There are no orphan records to export.",
    "정리할 고아 레코드를 선택하세요.": "Select orphan records to clean.",
    "먼저 스키마 분석을 실행하세요.": "Run schema analysis first.",
    "자동 수정 가능한 이슈가 없습니다.": "There are no auto-fixable issues.",
    "수동 처리가 필요한 이슈가 없습니다.": "There are no issues requiring manual handling.",
    "저장할 분석 결과가 없습니다.": "There are no analysis results to save.",
    "불러오기 완료": "Load Complete",
    "재분석": "Reanalyze",
    "수정이 완료되었습니다. 변경사항을 확인하기 위해 재분석하시겠습니까?": "Fixes are complete. Do you want to reanalyze to verify changes?",
    "불러오기 오류": "Load Error",
    "8.4 검사": "8.4 Checks",
    "문자셋 이슈": "Charset Issues",
    "예약어 충돌": "Reserved Word Conflicts",
    "저장 프로시저/함수": "Stored Procedures/Functions",
    "인증 플러그인": "Authentication Plugins",
    "FK 이름 길이": "FK Name Length",
    "FK 관계에서 부모가 없는 자식 레코드 탐지": "Detect child records without parents in FK relationships",
    "utf8mb3 사용 테이블/컬럼 확인": "Check tables/columns using utf8mb3",
    "MySQL 8.4 새 예약어와 충돌하는 이름 확인": "Check names that conflict with new MySQL 8.4 reserved words",
    "deprecated 함수 사용 여부 확인": "Check deprecated function usage",
    "deprecated SQL 모드 사용 여부 확인": "Check deprecated SQL mode usage",
    "ZEROFILL 속성 사용 컬럼 확인": "Check columns using the ZEROFILL attribute",
    "FLOAT(M,D), DOUBLE(M,D) deprecated 구문 확인": "Check deprecated FLOAT(M,D), DOUBLE(M,D) syntax",
    "FK 이름 64자 초과 확인": "Check FK names longer than 64 characters",
    "SQL 미리보기": "SQL Preview",
    "생성된 수정 SQL을 확인하고 Dry-run을 실행하세요.": "Review the generated fix SQL and run a dry run.",
    "수정 작업을 실행합니다. 문제 발생 시 Rollback SQL을 제공합니다.": "Run fix operations. Rollback SQL is provided if a problem occurs.",
    "실행 버튼을 클릭하면 데이터베이스가 수정됩니다. 이 작업은 되돌릴 수 없으니 신중하게 진행하세요.": "Clicking Run modifies the database. This operation cannot be undone, so proceed carefully.",
    "총 작업": "Total Operations",
    "영향 행": "Affected Rows",
    "다른 위치에 저장": "Save Elsewhere",
    "선택한 수정 작업을 실행합니다.": "Run the selected fix operations.",
    "계속하시겠습니까?": "Do you want to continue?",
    "원본 이슈": "Original Issue",
    "FK 연관": "FK Related",
    "연쇄 건너뛰기 확인": "Confirm Cascading Skip",
    "자동 수정 가능": "Auto-fixable",
    "그룹 이름을 입력하세요": "Enter a group name",
    "색상": "Color",
    "내용 없음": "No Content",
    "Rollback SQL 파일을 찾을 수 없습니다.": "Rollback SQL file was not found.",
    "복사할 Rollback SQL이 없습니다.": "There is no Rollback SQL to copy.",
    "저장할 Rollback SQL이 없습니다.": "There is no Rollback SQL to save.",
    "Rollback SQL이 저장되었습니다:": "Rollback SQL saved:",
    "Rollback SQL 파일 저장에 실패했습니다:": "Failed to save Rollback SQL file:",
    "SQL 복사": "Copy SQL",
    "시간 내에 종료되지 않음, 강제 종료": "did not exit in time; forcing shutdown",
    "FK 안전 변경 방식": "FK-safe change method",
    "문자셋 변경": "Charset change",
    "FK 안전 변경": "FK-safe change",
    "기타 이슈": "Other issues",
    "마이그레이션 자동 수정 SQL": "Migration auto-fix SQL",
    "실행할 SQL이 없습니다": "There is no SQL to run",
    "전략": "Strategy",
    "총 영향": "Total impact",
    "예상 영향 행": "Estimated affected rows",
    "Rollback SQL이 생성되었습니다. 복원에 사용하세요.": "Rollback SQL has been created. Use it to restore.",
    "Rollback SQL 저장됨": "Rollback SQL saved",
    "롤백 SQL이 생성되었습니다. 복원에 사용하세요.": "Rollback SQL has been created. Use it to restore.",
    "저장에 실패했습니다": "failed to save",
    "내용을 복사하여 수동으로 저장하세요.": "Copy the content and save it manually.",
    "수정을 적용하세요.": "Apply the fix.",
    "모든 테이블이 일괄 처리됩니다.": "All tables are processed in batch.",
    "체크 해제 시 해당 테이블을 건너뜁니다.": "Uncheck to skip that table.",
    "FK 관계로 인해 연쇄적으로 건너뛰어야 하는 테이블이 있을 수 있습니다.": "Some tables may need to be skipped together because of FK relationships.",
    "선택됨": "Selected",
    "건너뛰기": "Skipped",
    "총 FK": "Total FK",
    "전체": "All",
    "FK 일괄 변경에 자동 포함된 테이블 목록": "Tables automatically included in the FK batch change",
    "FK 일괄 변경에 포함": "included in FK batch change",
    "다음 테이블은 FK 연관테이블 일괄 변경에 자동 포함되었습니다.": "The following tables were automatically included in the FK related-table batch change.",
    "옵션 선택 단계만 건너뛰고, 실제 SQL 실행에는 모두 포함됩니다": "Only the option selection step is skipped; all are included when SQL is executed.",
    "FK 연관 테이블": "FK Related Tables",
    "다음 테이블도 함께 건너뛰어야 합니다": "the following tables must also be skipped",
    "진행하시겠습니까?": "Do you want to proceed?",
    "자동 포함": "auto-included",
    "모두 일괄 처리": "all batch-processed",
    "저장되었습니다": "saved",
    "파일을 찾을 수 없습니다.": "file was not found.",
    "찾아보기...": "Browse...",
    "작업 유형": "Task Type",
    "SQL 쿼리 실행": "Run SQL Query",
    "기본 정보": "Basic Info",
    "작업 이름": "Job name",
    "대상 데이터베이스": "Target database",
    "비워두면 전체": "leave empty for all",
    "출력 경로": "Output path",
    "최대 백업 수": "Max backups",
    "보관 기간": "Retention period",
    "SQL 쿼리": "SQL Query",
    "실행할 SQL을 입력하세요.": "Enter SQL to run.",
    "여러 쿼리는 세미콜론(;)으로 구분합니다.": "Separate multiple queries with semicolons (;).",
    "예시": "Examples",
    "저장 안 함": "Do not save",
    "DML용": "for DML",
    "결과 형식": "Result format",
    "파일명 패턴": "Filename pattern",
    "타임아웃": "Timeout",
    "결과 보관 수": "Result retention count",
    "결과 보관 기간": "Result retention period",
    "위험한 쿼리가 포함되어 있습니다.": "This SQL contains dangerous queries.",
    "실행 로그가 없습니다.": "There are no execution logs.",
    "을 사용하고 있습니다.": "is installed.",
    "정말 저장하시겠습니까?": "Do you really want to save?",
    "매일": "Daily",
    "매주": "Weekly",
    "매월": "Monthly",
    "매시간": "Hourly",
    "일요일": "Sunday",
    "월요일": "Monday",
    "화요일": "Tuesday",
    "수요일": "Wednesday",
    "목요일": "Thursday",
    "금요일": "Friday",
    "토요일": "Saturday",
    "간편 설정": "Simple Settings",
    "Cron 표현식": "Cron expression",
    "분 시 일 월 요일": "minute hour day month weekday",
    "평일": "weekdays",
    "정각": "on the hour",
    "스케줄 활성화": "Enable schedule",
    "스케줄 목록": "Schedule List",
    "요일": "Day of week",
    "잘못된 표현식": "Invalid expression",
    "위험한 쿼리 감지": "Dangerous Query Detected",
    "스키마를 입력하세요.": "Enter a schema.",
    "출력 경로를 선택하세요.": "Select an output path.",
    "서버가 지역명 타임존을 지원하지 않으면 자동으로 +09:00(KST)로 보정합니다.": "If the server does not support named time zones, it is automatically adjusted to +09:00 (KST).",
    "FK 안전 변경 방식으로 모든 테이블이 일괄 처리됩니다.": "All tables are processed in batch using the FK-safe change method.",
    "체크 해제 시 해당 테이블을 건너뜁니다.": "Uncheck to skip that table.",
    "사전 검사": "Pre-flight Check",
    "마이그레이션 전 필수 요건을 검사합니다.": "Checks required conditions before migration.",
    "검사 항목": "Check Items",
    "실행 중": "Running",
    "요약": "Summary",
    "HTML 리포트 다운로드": "Download HTML Report",
    "JSON 리포트 다운로드": "Download JSON Report",
    "HTML 리포트 저장": "Save HTML Report",
    "JSON 리포트 저장": "Save JSON Report",
    "아래 내용을 확인 후 실행을 시작하세요.": "Review the following, then start execution.",
    "자동 실행 대상": "Auto-run Items",
    "조치 불필요": "No Action Needed",
    "자동 처리": "handled automatically",
    "마이그레이션 후 수동 처리 필요": "Manual handling required after migration",
    "아래 항목은 DB가 아닌 애플리케이션 또는 설정 변경이 필요합니다.": "The items below require application or setting changes, not DB changes.",
    "실제 실행하지 않음": "do not execute",
    "체크하면 실제 SQL을 실행하지 않고 시뮬레이션만 합니다.": "When checked, SQL is simulated without actually running.",
    "체크하면 백업 완료로 간주합니다.": "When checked, backup is considered complete.",
    "Pre-flight 검사 통과": "Pre-flight check passed",
    "Pre-flight 검사 실패": "Pre-flight check failed",
    "마이그레이션 완료": "Migration Complete",
    "일부 이슈 남음": "some issues remain",
    "실행할 항목 없음": "No items to run",
    "마이그레이션을 취소하시겠습니까?": "Do you want to cancel migration?",
    "작업 중": "Operation in progress",
    "마이그레이션이 실행 중입니다. 종료하시겠습니까?": "Migration is running. Do you want to close?",
    "변수": "Variables",
    "이름": "Name",
    "마지막 실행": "Last Run",
    "활성화": "Enabled",
    "Export/Import 오류 시 자동으로 GitHub 이슈 생성": "Create GitHub issues automatically on Export/Import errors",
    "백업 목록": "Backup List",
    "최근": "recent",
    "선택한 백업으로 설정을 복원하시겠습니까?": "Do you want to restore settings from the selected backup?",
    "선택한 파일에서 설정을 가져오시겠습니까?": "Do you want to import settings from the selected file?",
    "DB 연결 풀 상태를 모니터링합니다. 연결 풀은 DB 연결을 재사용하여 성능을 향상시킵니다.": "Monitor DB connection pool status. Connection pools improve performance by reusing DB connections.",
    "활성 연결 풀": "Active Connection Pools",
    "풀 키": "Pool Key",
    "생성됨": "Created",
    "사용 중": "In Use",
    "최대": "Max",
    "새로고침": "Refresh",
    "모든 연결 종료": "Close All Connections",
    "풀 상태를 새로고침하려면 '새로고침' 버튼을 클릭하세요.": "Click Refresh to refresh pool status.",
    "모든 DB 연결 풀을 종료하시겠습니까?": "Do you want to close all DB connection pools?",
    "활성 연결이 있으면 작업이 중단될 수 있습니다.": "Active connections may be interrupted.",
    "로그 파일이 없습니다.": "There is no log file.",
    "로그가 저장되었습니다": "Log saved",
    "로그 파일을 초기화하시겠습니까?": "Do you want to clear the log file?",
    "애플리케이션 정보": "Application Info",
    "확인 중": "Checking",
    "다운로드 준비 중": "Preparing download",
    "설치 파일 정보를 가져오는 중": "Fetching installer information",
    "다운로드 중": "Downloading",
    "다운로드가 취소되었습니다.": "Download canceled.",
    "참조": "reference",
    "복원할 백업을 선택하세요.": "Select a backup to restore.",
    "시작 프로그램 설정 오류": "Startup Setting Error",
    "설치 파일을 찾을 수 없습니다.": "Installer file was not found.",
    "다시 다운로드해 주세요.": "Download it again.",
    "활성 연결 풀이 없습니다.": "There are no active connection pools.",
    "파일 크기": "File size",
    "풀 상태 조회 실패": "Failed to query pool status",
    "풀 종료 실패": "Failed to close pool",
    "설치 프로그램 실행에 실패했습니다": "Failed to run installer",
    "모듈을 불러올 수 없습니다": "Could not load module",
    "GitHub App 인스턴스를 생성할 수 없습니다.": "Cannot create GitHub App instance.",
    "환경변수 설정을 확인하세요.": "Check environment variable settings.",
    "연결 테스트 성공": "Connection Test Succeeded",
    "연결 테스트 실패": "Connection Test Failed",
    "쿼리 내용으로 검색": "Search query content",
    "즐겨찾기만": "Favorites only",
    "쿼리 미리보기": "Query Preview",
    "즐겨찾기 토글": "Toggle Favorite",
    "에디터에 붙여넣기": "Paste into Editor",
    "자동완성": "autocomplete",
    "데이터베이스 목록 새로고침": "Refresh database list",
    "현재 쿼리 실행": "Run current query",
    "커서 위치의 쿼리만 실행": "Run only the query at the cursor",
    "전체 쿼리 실행": "Run all queries",
    "에디터의 모든 쿼리 실행": "Run all queries in the editor",
    "히스토리": "History",
    "쿼리 히스토리 보기": "View query history",
    "체크 해제 시": "When unchecked",
    "체크 시": "When checked",
    "수정 쿼리 실행 전 확인 필요": "confirmation is required before running modifying queries",
    "모든 쿼리 즉시 실행": "run all queries immediately",
    "기존 방식": "legacy behavior",
    "제한 없음": "No limit",
    "행 제한": "row limit",
    "LIMIT 절이 없는 경우에만 적용": "applies only when there is no LIMIT clause",
    "새 탭": "New Tab",
    "실행 로그 펼치기": "Expand Execution Log",
    "실행 로그 접기": "Collapse Execution Log",
    "실행 로그 요약 보기": "Show execution log summary",
    "준비됨": "Ready",
    "키워드": "Keyword",
    "기간": "Period",
    "실행할 쿼리가 없습니다.": "There is no query to run.",
    "쿼리가 이미 실행 중입니다.": "A query is already running.",
    "DB 자격 증명이 설정되지 않았습니다.": "DB credentials are not configured.",
    "쿼리 실행 중": "Running query",
    "쿼리 완료": "Query complete",
    "실행 완료": "Execution complete",
    "미커밋 변경": "uncommitted changes",
    "미커밋": "uncommitted",
    "메타데이터 로드 중": "Loading metadata",
    "메타데이터 로드 실패": "Failed to load metadata",
    "데이터베이스 목록 조회 중": "Loading database list",
    "데이터베이스 발견": "databases found",
    "기본 스키마": "Default schema",
    "찾을 수 없습니다.": "could not be found.",
    "임시 터널 생성 중": "Creating temporary tunnel",
    "임시 터널 생성됨": "Temporary tunnel created",
    "임시 터널": "Temporary tunnel",
    "임시 터널 종료": "Closing temporary tunnel",
    "파일 열림": "File opened",
    "파일 저장됨": "File saved",
    "파일을 열 수 없습니다": "Could not open file",
    "파일을 저장할 수 없습니다": "Could not save file",
    "쿼리 실행": "Run queries",
    "쿼리": "Query",
    "셀 편집": "Cell edits",
    "버튼": "button",
    "클릭하여": "click to",
    "항목": "items",
    "자동 커밋": "auto commit",
    "커밋 완료": "Commit complete",
    "커밋에 실패했습니다": "Commit failed",
    "롤백 완료": "Rollback complete",
    "정말 롤백하시겠습니까?": "Do you really want to roll back?",
    "모두 취소됩니다.": "will all be canceled.",
    "미커밋 변경사항": "Uncommitted changes",
    "자동 롤백됨": "automatically rolled back",
    "저장되지 않은 SQL 편집 내용": "Unsaved SQL edits",
    "다음 내용이 손실됩니다": "The following content will be lost",
    "셀 편집 적용 실패": "Failed to apply cell edits",
    "전체 롤백되었습니다": "rolled back completely",
    "영향받은 행 수": "Affected rows",
    "로드됨": "loaded",
    "적용됨": "applied",
    "취소됨": "canceled",
    "롤백됨": "rolled back",
    "탭": "tabs",
    "제안": "Suggestion",
    "분석 결과가 저장되었습니다.": "Analysis results saved.",
    "분석 결과를 불러왔습니다.": "Analysis results loaded.",
    "분석일시": "Analysis time",
    "표시 중 오류 발생": "Error while displaying",
    "파일 저장 중 오류가 발생했습니다.": "An error occurred while saving the file.",
    "자동 수정 위저드를 불러올 수 없습니다": "Could not load the auto-fix wizard",
    "수동 처리 가이드 표시 중 오류": "Error while displaying manual handling guide",
    "순환 참조": "circular reference",
    "리포트가 저장되었습니다": "Report saved",
    "남은 이슈": "Remaining issues",
    "새 이슈": "New issues",
    "풀": "pools",
    "사용 중": "in use",
    "인증 테스트 건너뜀": "auth test skipped",
    "자격 증명 없음": "no credentials",
    "도달 실패": "reach failed",
    "DB 테스트 실패": "DB test failed",
    "터널 생성 오류": "tunnel creation error",
    "이미 실행 중이지만 창 활성화 요청을 보내지 못했습니다.": "is already running, but the window activation request failed.",
    "쿼리가 실행 중입니다. 정말 닫으시겠습니까?": "A query is running. Do you really want to close?",
    "데이터베이스": "Database",
    "선택사항": "optional",
    "데이터베이스를 선택하거나 입력하세요": "Select or enter a database",
    "생략 가능": "optional",
    "DB 자격 증명이 설정되지 않았습니다. 터널 설정에서 저장해주세요.": "DB credentials are not configured. Save them in tunnel settings.",
    "SQL 파일을 선택해주세요.": "Select an SQL file.",
    "SQL이 실행 중입니다. 정말 닫으시겠습니까?": "SQL is running. Do you really want to close?",
    "예": "Example",
    "이름(별칭)": "Name (alias)",
    "경유": "via",
    "직접 연결": "Direct connection",
    "로컬/외부 DB": "local/external DB",
    "중계 서버": "jump server",
    "파일 찾기": "Find File",
    "주소": "address",
    "목적지": "destination",
    "기본 DB 이름": "Default DB name",
    "기본 스키마": "Default schema",
    "위험 작업 시": "for dangerous operations",
    "직접 입력 필요": "manual input required",
    "다이얼로그 표시": "show dialog",
    "확인 없이 바로 실행": "run immediately without confirmation",
    "내 컴퓨터": "my computer",
    "터널 테스트": "Tunnel Test",
    "DB 인증 정보": "DB Auth Info",
    "선택 사항": "optional",
    "DB 자격 증명 저장": "Save DB Credentials",
    "암호화하여 저장합니다": "Saved encrypted.",
    "DB 인증 테스트": "DB Auth Test",
    "통합 테스트": "Integrated Test",
    "복사할 수 있는 SSH 터널 연결이 없습니다.": "There are no SSH tunnel connections to copy.",
    "필수 필드 누락": "Required Fields Missing",
    "직접 연결 모드는 SSH 터널 테스트가 필요하지 않습니다.": "Direct connection mode does not require SSH tunnel testing.",
    "DB 인증 테스트 또는 통합 테스트를 실행해주세요.": "Run DB Auth Test or Integrated Test.",
    "저장됨 - 변경시 새로 입력": "saved - enter again to change",
    "재연결 횟수": "Reconnect Count",
    "마지막 확인": "Last Check",
    "최근 이벤트": "Recent Events",
    "자동 재연결 활성화": "Enable Auto-Reconnect",
    "연결됨": "Connected",
    "연결 안됨": "Disconnected",
    "최대 재연결 시도 횟수": "Max reconnect attempts",
    "계속하려면 <b>스키마명을 정확히 입력</b>하세요:": "To continue, <b>enter the schema name exactly</b>:",
    "연결 정보 (터널명 또는 host_port)": "Connection info (tunnel name or host_port)",
    "SQL 파일": "SQL files",
    "JSON 파일": "JSON files",
    "Markdown 파일": "Markdown files",
    "로그 파일 저장": "Save Log File",
    "파일 열기": "Open File",
    "파일 선택": "Select File",
    "파일 선택...": "Select File...",
    "SQL 파일 열기": "Open SQL File",
    "SQL 파일 저장": "Save SQL File",
    "SQL 파일 선택": "Select SQL File",
    "SSH Key 파일 선택": "Select SSH Key File",
    "백업 파일 저장 위치": "Backup file save location",
    "결과 파일 저장 위치": "Result file save location",
    "고급 설정": "Advanced Settings",
    "조치 불필요 (MySQL 8.4에서 자동 처리)": "No action needed (handled automatically by MySQL 8.4)",
    "체크 해제 시: INSERT/UPDATE/DELETE 등 수정 쿼리 실행 전 확인 필요": "When unchecked: confirmation is required before running modifying queries such as INSERT/UPDATE/DELETE.",
    "체크 시: 모든 쿼리 즉시 실행 (기존 방식)": "When checked: run all queries immediately (legacy behavior).",
    "SELECT 쿼리에 자동으로 적용되는 행 제한": "Row limit automatically applied to SELECT queries",
    "기존 연결의 Bastion Host, Port, SSH User, SSH Key를 복사합니다": "Copy Bastion Host, Port, SSH User, and SSH Key from an existing connection.",
    "또는": "or",
    "DB명": "DB name",
    "schema명": "schema name",
    "스키마명": "schema name",
    "Production: 위험 작업 시 스키마명 직접 입력 필요\nStaging: 위험 작업 시 확인 다이얼로그 표시\nDevelopment: 확인 없이 바로 실행": "Production: schema name must be entered for dangerous operations\nStaging: confirmation dialog is shown for dangerous operations\nDevelopment: runs immediately without confirmation",
    "DB Engine을 선택해주세요.\nMySQL 또는 PostgreSQL을 명시해야 합니다.": "Select DB Engine.\nMySQL or PostgreSQL must be specified.",
}

_EN_REGEX_TRANSLATIONS = (
    (r"'(?P<name>[^']+)' 그룹을 삭제하시겠습니까\?\n\n그룹에 속한 터널은 '그룹 없음'으로 이동됩니다\.", r"Do you want to delete group '\g<name>'?\n\nTunnels in this group will move to 'Ungrouped'."),
    (r"✅ '(?P<name>[^']+)' 터널이 이미 연결되어 있습니다\.", r"✅ Tunnel '\g<name>' is already connected."),
    (r"✅ '(?P<name>[^']+)' 터널 연결 테스트 성공!\n\n(?P<rest>.*)", r"✅ Tunnel connection test succeeded for '\g<name>'.\n\n\g<rest>"),
    (r"❌ '(?P<name>[^']+)' DB Engine이 설정되어 있지 않습니다\.\n\n연결 설정에서 MySQL 또는 PostgreSQL을 먼저 선택해주세요\.", r"❌ DB Engine is not configured for '\g<name>'.\n\nSelect MySQL or PostgreSQL in connection settings first."),
    (r"❌ '(?P<name>[^']+)' 연결 테스트 중 오류 발생\n\n(?P<rest>.*)", r"❌ An error occurred during connection test for '\g<name>'.\n\n\g<rest>"),
    (r"연결 테스트 중: (?P<name>.*)\.\.\.", r"Testing connection: \g<name>..."),
    (r"연결 테스트 중단: (?P<name>.*)", r"Connection test stopped: \g<name>"),
    (r"연결 시도 중: (?P<name>.*)\.\.\.", r"Connecting: \g<name>..."),
    (r"연결 종료: (?P<name>.*)", r"Disconnected: \g<name>"),
    (r"✅ '(?P<name>[^']+)' 연결이 생성되었습니다\.", r"✅ Connection '\g<name>' created."),
    (r"(?P<name>.+) 연결되었습니다\.", r"\g<name> connected."),
    (r"(?P<name>.+) 백업이 완료되었습니다\.", r"Backup completed for \g<name>."),
    (r"새로운 버전 (?P<version>.+)이 사용 가능합니다\.\n설정에서 다운로드할 수 있습니다\.", r"New version \g<version> is available.\nYou can download it in Settings."),
    (r"자동 연결 완료", r"Auto-Connect Complete"),
    (r"선택한 (?P<count>\{[^}]*\}|[0-9,]+)개 테이블을 재시도하시겠습니까\?\n\n테이블: (?P<tables>.*)", r"Do you want to retry \g<count> selected tables?\n\nTables: \g<tables>"),
    (r"⚠️ (?P<relations>\{[^}]*\}|[0-9,]+)개 관계에서 총 (?P<records>\{[^}]*\}|[0-9,]+)개 고아 레코드 발견", r"⚠️ Found \g<records> orphan records across \g<relations> relationships"),
    (r"⚠️ 호환성 이슈: (?P<errors>\{[^}]*\}|[0-9,]+)개 오류, (?P<warnings>\{[^}]*\}|[0-9,]+)개 경고", r"⚠️ Compatibility issues: \g<errors> errors, \g<warnings> warnings"),
    (r"⚠️ 호환성 경고: (?P<warnings>\{[^}]*\}|[0-9,]+)개 \(Import 가능\)", r"⚠️ Compatibility warnings: \g<warnings> (Import allowed)"),
    (r"총 (?P<count>\{[^}]*\}|[0-9,]+)개 테이블: 🟢 추가 (?P<added>\{[^}]*\}|[0-9,]+), 🟡 수정 (?P<changed>\{[^}]*\}|[0-9,]+), 🔴 삭제 (?P<deleted>\{[^}]*\}|[0-9,]+), ⚪ 동일 (?P<same>\{[^}]*\}|[0-9,]+)", r"Total \g<count> tables: 🟢 added \g<added>, 🟡 changed \g<changed>, 🔴 deleted \g<deleted>, ⚪ unchanged \g<same>"),
    (r"현재 (?P<count>\{[^}]*\}|[0-9,]+)개의 작업이 진행 중입니다\.\n창을 닫으면 작업이 중단됩니다\. 닫으시겠습니까\?", r"\g<count> operations are in progress.\nClosing the window will stop them. Do you want to close?"),
    (r"스케줄 '(?P<name>[^']+)'을\(를\) 삭제하시겠습니까\?", r"Do you want to delete schedule '\g<name>'?"),
    (r"'(?P<name>[^']+)' (?P<task>.+)을\(를\) 지금 실행하시겠습니까\?", r"Do you want to run '\g<name>' \g<task> now?"),
    (r"🔴 Critical 이슈 (?P<count>\{[^}]*\}|[0-9,]+)건이 발견되었습니다\.\nImport 실패 위험이 있는 변경 사항이 포함되어 있습니다\.\n\n그래도 동기화 스크립트를 생성하시겠습니까\?", r"🔴 \g<count> critical issues were found.\nChanges include Import failure risk.\n\nDo you still want to create the sync script?"),
    (r"다음 (?P<count>\{[^}]*\}|[0-9,]+)개 테이블은 FK 연관테이블 일괄 변경에 자동 포함되었습니다\.\n\(옵션 선택 단계만 건너뛰고, 실제 SQL 실행에는 모두 포함됩니다\)", r"The following \g<count> tables were automatically included in the FK related-table batch change.\n(Only the option selection step is skipped; all are included when SQL is executed.)"),
    (r"'(?P<table>[^']+)' 테이블을 건너뛰면\nFK 관계로 인해 다음 테이블도 함께 건너뛰어야 합니다:\n\n(?P<rest>.*)\n\n진행하시겠습니까\?", r"If you skip table '\g<table>', the following tables must also be skipped because of FK relationships:\n\n\g<rest>\n\nDo you want to proceed?"),
    (r"이슈 (?P<current>\{[^}]*\}|[0-9,]+) / (?P<total>\{[^}]*\}|[0-9,]+) \(전체 (?P<all>\{[^}]*\}|[0-9,]+)개 중 (?P<included>\{[^}]*\}|[0-9,]+)개 자동 포함\)", r"Issue \g<current> / \g<total> (\g<included> auto-included out of \g<all> total)"),
    (r"💡 <b>Rollback SQL이 자동 저장되었습니다\.</b><br><br>문제 발생 시 아래 파일을 실행하여 변경사항을 되돌릴 수 있습니다:<br><code>(?P<path>.*)</code><br><br>⚠️ DDL\(ALTER TABLE\)은 트랜잭션 롤백이 불가능하므로, 문제 발생 시 이 SQL을 수동으로 실행하세요\.", r"💡 <b>Rollback SQL was saved automatically.</b><br><br>If a problem occurs, run the file below to revert the changes:<br><code>\g<path></code><br><br>⚠️ DDL (ALTER TABLE) cannot be rolled back in a transaction, so run this SQL manually if a problem occurs."),
    (r"'(?P<name>[^']+)'의 수정 옵션을 선택하세요\.", r"Select a fix option for '\g<name>'."),
    (r"'(?P<name>[^']+)'의 추가 입력값을 입력하세요\.", r"Enter the additional input value for '\g<name>'."),
    (r"정리 작업이 완료되었습니다\.\n\n성공: (?P<success>\{[^}]*\}|[0-9,]+)개\n실패: (?P<failed>\{[^}]*\}|[0-9,]+)개\n영향받은 행: (?P<affected>\{[^}]*\}|[0-9,]+)개", r"Cleanup completed.\n\nSuccess: \g<success>\nFailed: \g<failed>\nAffected rows: \g<affected>"),
    (r"다음 (?P<count>\{[^}]*\}|[0-9,]+)개 이슈는 자동 수정이 불가능합니다\.\n아래 가이드를 참고하여 수동으로 처리하세요\.", r"The following \g<count> issues cannot be auto-fixed.\nUse the guide below to handle them manually."),
    (r"선택된 (?P<count>\{[^}]*\}|[0-9,]+)개 항목에 대해 정리 작업을 실행합니다\.\n\n이 작업은 되돌릴 수 없습니다\. 계속하시겠습니까\?", r"Cleanup will run for \g<count> selected items.\n\nThis operation cannot be undone. Do you want to continue?"),
    (r"이 SQL에 위험한 쿼리가 포함되어 있습니다\.\n\n(?P<sql>.*)\n\n정말 저장하시겠습니까\?", r"This SQL contains dangerous queries.\n\n\g<sql>\n\nDo you really want to save?"),
    (r"'(?P<name>[^']+)'의 변경사항을 저장하시겠습니까\?", r"Do you want to save changes to '\g<name>'?"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)초", r"\g<count>s"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 터널 연결됨", r"\g<count> tunnels connected"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 스킵", r"\g<count> skipped"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 테이블", r"\g<count> tables"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 이슈", r"\g<count> issues"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 관계", r"\g<count> relationships"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 고아 레코드", r"\g<count> orphan records"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 오류", r"\g<count> errors"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 경고", r"\g<count> warnings"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 표시", r"\g<count> shown"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개 남음", r"\g<count> remaining"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)행 반환", r"\g<count> rows returned"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)행 영향받음", r"\g<count> rows affected"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)행 영향", r"\g<count> rows affected"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)행", r"\g<count> rows"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)개", r"\g<count>"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)건", r"\g<count> items"),
    (r"(?P<count>\{[^}]*\}|[0-9,]+)회", r"\g<count> times"),
)

_EN_WORD_TRANSLATIONS = {
    "성공": "Success",
    "실패": "Failed",
    "오류": "Error",
    "경고": "Warning",
    "알림": "Notice",
    "확인": "Confirm",
    "취소": "Cancel",
    "저장": "Save",
    "삭제": "Delete",
    "수정": "Edit",
    "추가": "Add",
    "선택": "Select",
    "해제": "Deselect",
    "전체": "All",
    "닫기": "Close",
    "열기": "Open",
    "실행": "Run",
    "시작": "Start",
    "중지": "Stop",
    "완료": "Complete",
    "원인": "Cause",
    "대기": "Waiting",
    "설정": "Settings",
    "정보": "Info",
    "상태": "Status",
    "로그": "Log",
    "스키마": "Schema",
    "테이블": "Table",
    "컬럼": "Column",
    "인덱스": "Index",
    "변경": "Change",
    "차이": "Difference",
    "유형": "Type",
    "총": "Total",
    "대상": "Target",
    "관계": "Relationship",
    "레코드": "Record",
    "보고서": "Report",
    "리포트": "Report",
    "조회": "Select",
    "크기": "Size",
    "버전": "Version",
    "파일": "File",
    "폴더": "Folder",
    "경로": "Path",
    "사용자": "User",
    "호스트": "Host",
    "연결": "Connection",
    "터널": "Tunnel",
    "분석": "Analysis",
    "비교": "Comparison",
    "마이그레이션": "Migration",
    "업데이트": "Update",
    "다운로드": "Download",
    "설치": "Install",
    "복원": "Restore",
    "내보내기": "Export",
    "가져오기": "Import",
    "검색": "Search",
    "결과": "Result",
    "진행률": "Progress",
    "속도": "Speed",
    "현재": "Current",
    "평균": "Average",
    "시간": "Time",
    "메시지": "Message",
    "이벤트": "Event",
    "이슈": "Issue",
    "옵션": "Option",
    "입력": "Input",
    "모드": "Mode",
    "엔진": "Engine",
    "인증": "Auth",
    "비밀번호": "Password",
    "키": "Key",
    "백업": "Backup",
    "프로젝트": "Project",
    "라이선스": "License",
    "그룹": "Group",
    "없음": "None",
    "만들기": "Create",
    "에디터": "Editor",
    "불가": "Not allowed",
    "스케줄": "Schedule",
    "관리": "Manage",
    "메뉴": "Menu",
    "스킵": "Skipped",
    "시도": "Attempt",
    "중단": "Stopped",
    "등록": "Register",
    "제거": "Remove",
    "이동": "Move",
    "재시도": "Retry",
    "정리": "Cleanup",
    "불러오기": "Load",
    "표시": "Shown",
    "반환": "Returned",
    "영향": "Affected",
    "적용": "Apply",
    "생성": "Create",
    "복사": "Copy",
    "부모": "Parent",
    "자식": "Child",
    "최근": "Recent",
    "불필요": "not required",
    "위험한": "Dangerous",
    "포트": "port",
}


def _has_hangul(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)


def translate_text(value):
    """Translate hardcoded UI text at runtime.

    This intentionally accepts any object because Qt setters often receive
    non-string overloads; those values pass through unchanged.
    """
    if not isinstance(value, str) or current_language() == DEFAULT_LANGUAGE or not _has_hangul(value):
        return value

    if value in _EN_TEXT_TRANSLATIONS:
        return _EN_TEXT_TRANSLATIONS[value]

    stripped = value.strip()
    if stripped in _EN_TEXT_TRANSLATIONS:
        return value.replace(stripped, _EN_TEXT_TRANSLATIONS[stripped], 1)

    translated = value
    for pattern, replacement in _EN_REGEX_TRANSLATIONS:
        translated = re.sub(pattern, replacement, translated, flags=re.DOTALL)

    for korean, english in sorted(_EN_PHRASE_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        translated = translated.replace(korean, english)

    for korean, english in sorted(_EN_WORD_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        translated = re.sub(
            rf"(?<![\uac00-\ud7a3]){re.escape(korean)}(?![\uac00-\ud7a3])",
            english,
            translated,
        )

    translated = re.sub(r"(?<=[A-Za-z0-9)}'\"`])(?:은|는|이|가|을|를|와|과|의|에|에서|부터|까지|도|로|으로)(?=[\s,.:;!?)]|$)", "", translated)
    translated = re.sub(r"(?<=[A-Za-z0-9)}'\"`])하시겠습니까", "?", translated)
    translated = re.sub(r"(?<=[A-Za-z0-9)}'\"`])하세요", ".", translated)

    return translated


def _translate_sequence(values):
    return [translate_text(value) for value in values]


def install_qt_i18n() -> bool:
    """Install broad PyQt text translation hooks for legacy hardcoded UI strings."""
    global _qt_i18n_installed
    if _qt_i18n_installed:
        return False

    try:
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import (
            QAbstractButton,
            QCheckBox,
            QComboBox,
            QDialog,
            QFileDialog,
            QFormLayout,
            QGroupBox,
            QLabel,
            QLineEdit,
            QMenu,
            QMessageBox,
            QPlainTextEdit,
            QProgressBar,
            QPushButton,
            QRadioButton,
            QStatusBar,
            QSystemTrayIcon,
            QTabWidget,
            QTableWidget,
            QTextEdit,
            QToolTip,
            QTreeWidget,
            QWidget,
            QWizard,
            QWizardPage,
        )
    except Exception:
        return False

    def translate_qt_arg(value):
        if isinstance(value, str):
            return translate_text(value)
        if isinstance(value, list):
            return [translate_qt_arg(item) for item in value]
        if isinstance(value, tuple):
            return tuple(translate_qt_arg(item) for item in value)
        return value

    def patch_init(cls, text_index=0):
        original = cls.__init__
        if getattr(original, "_tf_i18n_wrapped", False):
            return

        def wrapped(self, *args, **kwargs):
            args = list(args)
            if text_index is None:
                args = [translate_qt_arg(arg) for arg in args]
            elif len(args) > text_index:
                args[text_index] = translate_qt_arg(args[text_index])
            original(self, *args, **kwargs)

        wrapped._tf_i18n_wrapped = True
        cls.__init__ = wrapped

    def patch_method(cls, name, text_indexes):
        original = getattr(cls, name, None)
        if original is None or getattr(original, "_tf_i18n_wrapped", False):
            return

        def wrapped(self, *args, **kwargs):
            args = list(args)
            for index in text_indexes:
                if len(args) > index:
                    args[index] = translate_qt_arg(args[index])
            return original(self, *args, **kwargs)

        wrapped._tf_i18n_wrapped = True
        setattr(cls, name, wrapped)

    def patch_all_string_args_method(cls, name):
        original = getattr(cls, name, None)
        if original is None or getattr(original, "_tf_i18n_wrapped", False):
            return

        def wrapped(self, *args, **kwargs):
            args = [translate_qt_arg(arg) for arg in args]
            return original(self, *args, **kwargs)

        wrapped._tf_i18n_wrapped = True
        setattr(cls, name, wrapped)

    for cls in (QLabel, QAbstractButton, QPushButton, QCheckBox, QRadioButton, QGroupBox, QAction, QMenu):
        patch_init(cls, None)
        patch_method(cls, "setText", [0])

    patch_init(QMessageBox, None)
    patch_method(QWidget, "setWindowTitle", [0])
    patch_method(QWidget, "setToolTip", [0])
    patch_method(QWidget, "setStatusTip", [0])
    patch_method(QWidget, "setWhatsThis", [0])
    patch_method(QDialog, "setWindowTitle", [0])
    patch_method(QGroupBox, "setTitle", [0])
    patch_method(QMenu, "setTitle", [0])
    patch_method(QAction, "setToolTip", [0])
    patch_method(QAction, "setStatusTip", [0])
    patch_method(QStatusBar, "showMessage", [0])
    patch_method(QSystemTrayIcon, "showMessage", [0, 1])
    patch_method(QProgressBar, "setFormat", [0])
    patch_method(QWizard, "setButtonText", [1])
    patch_method(QWizardPage, "setTitle", [0])
    patch_method(QWizardPage, "setSubTitle", [0])

    for cls in (QLineEdit, QTextEdit, QPlainTextEdit):
        patch_method(cls, "setPlaceholderText", [0])
    patch_method(QTextEdit, "append", [0])
    patch_method(QTextEdit, "setHtml", [0])
    patch_method(QPlainTextEdit, "appendPlainText", [0])

    patch_method(QComboBox, "setPlaceholderText", [0])
    patch_method(QComboBox, "setItemText", [1])
    patch_all_string_args_method(QComboBox, "addItem")
    patch_all_string_args_method(QComboBox, "insertItem")
    original_add_items = QComboBox.addItems
    if not getattr(original_add_items, "_tf_i18n_wrapped", False):
        def add_items(self, texts):
            return original_add_items(self, _translate_sequence(list(texts)))

        add_items._tf_i18n_wrapped = True
        QComboBox.addItems = add_items

    patch_all_string_args_method(QTabWidget, "addTab")
    patch_method(QTabWidget, "setTabText", [1])
    patch_all_string_args_method(QFormLayout, "addRow")

    original_tree_headers = QTreeWidget.setHeaderLabels
    if not getattr(original_tree_headers, "_tf_i18n_wrapped", False):
        def tree_headers(self, labels):
            return original_tree_headers(self, _translate_sequence(list(labels)))

        tree_headers._tf_i18n_wrapped = True
        QTreeWidget.setHeaderLabels = tree_headers

    original_table_headers = QTableWidget.setHorizontalHeaderLabels
    if not getattr(original_table_headers, "_tf_i18n_wrapped", False):
        def table_headers(self, labels):
            return original_table_headers(self, _translate_sequence(list(labels)))

        table_headers._tf_i18n_wrapped = True
        QTableWidget.setHorizontalHeaderLabels = table_headers

    original_vertical_table_headers = QTableWidget.setVerticalHeaderLabels
    if not getattr(original_vertical_table_headers, "_tf_i18n_wrapped", False):
        def vertical_table_headers(self, labels):
            return original_vertical_table_headers(self, _translate_sequence(list(labels)))

        vertical_table_headers._tf_i18n_wrapped = True
        QTableWidget.setVerticalHeaderLabels = vertical_table_headers

    original_tooltip_show_text = QToolTip.showText
    if not getattr(original_tooltip_show_text, "_tf_i18n_wrapped", False):
        def tooltip_show_text(*args, **kwargs):
            args = list(args)
            if len(args) > 1 and isinstance(args[1], str):
                args[1] = translate_text(args[1])
            return original_tooltip_show_text(*args, **kwargs)

        tooltip_show_text._tf_i18n_wrapped = True
        QToolTip.showText = tooltip_show_text

    original_menu_add_action = QMenu.addAction
    if not getattr(original_menu_add_action, "_tf_i18n_wrapped", False):
        def menu_add_action(self, *args, **kwargs):
            args = list(args)
            if args and isinstance(args[0], str):
                args[0] = translate_text(args[0])
            elif len(args) > 1 and isinstance(args[1], str):
                args[1] = translate_text(args[1])
            return original_menu_add_action(self, *args, **kwargs)

        menu_add_action._tf_i18n_wrapped = True
        QMenu.addAction = menu_add_action

    original_menu_add_menu = QMenu.addMenu
    if not getattr(original_menu_add_menu, "_tf_i18n_wrapped", False):
        def menu_add_menu(self, *args, **kwargs):
            args = list(args)
            if args and isinstance(args[0], str):
                args[0] = translate_text(args[0])
            elif len(args) > 1 and isinstance(args[1], str):
                args[1] = translate_text(args[1])
            return original_menu_add_menu(self, *args, **kwargs)

        menu_add_menu._tf_i18n_wrapped = True
        QMenu.addMenu = menu_add_menu

    for name in ("information", "warning", "critical", "question"):
        original = getattr(QMessageBox, name)
        if getattr(original, "_tf_i18n_wrapped", False):
            continue

        def make_message_wrapper(fn):
            def wrapped(*args, **kwargs):
                args = list(args)
                for index in (1, 2):
                    if len(args) > index and isinstance(args[index], str):
                        args[index] = translate_text(args[index])
                if isinstance(kwargs.get("title"), str):
                    kwargs["title"] = translate_text(kwargs["title"])
                if isinstance(kwargs.get("text"), str):
                    kwargs["text"] = translate_text(kwargs["text"])
                return fn(*args, **kwargs)

            wrapped._tf_i18n_wrapped = True
            return wrapped

        setattr(QMessageBox, name, make_message_wrapper(original))

    patch_method(QMessageBox, "setText", [0])
    patch_method(QMessageBox, "setInformativeText", [0])
    patch_method(QMessageBox, "setDetailedText", [0])

    for name in ("getOpenFileName", "getSaveFileName", "getExistingDirectory"):
        original = getattr(QFileDialog, name)
        if getattr(original, "_tf_i18n_wrapped", False):
            continue

        def make_file_dialog_wrapper(fn):
            def wrapped(*args, **kwargs):
                args = list(args)
                for index in (1, 3):
                    if len(args) > index and isinstance(args[index], str):
                        args[index] = translate_text(args[index])
                if isinstance(kwargs.get("caption"), str):
                    kwargs["caption"] = translate_text(kwargs["caption"])
                if isinstance(kwargs.get("filter"), str):
                    kwargs["filter"] = translate_text(kwargs["filter"])
                return fn(*args, **kwargs)

            wrapped._tf_i18n_wrapped = True
            return wrapped

        setattr(QFileDialog, name, make_file_dialog_wrapper(original))

    _qt_i18n_installed = True
    return True
