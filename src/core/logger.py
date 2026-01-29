"""
TunnelForge 통합 로깅 시스템

모든 모듈에서 일관된 로깅을 제공합니다.
- 파일 로깅: %APPDATA%/Local/TunnelForge/logs/tunnelforge.log
- 콘솔 로깅: 개발 환경에서 디버깅용
- 로그 로테이션: 5MB, 최대 3개 백업
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# 로그 디렉토리 경로
if os.name == 'nt':
    LOG_DIR = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'TunnelForge', 'logs')
else:
    LOG_DIR = os.path.join(os.path.expanduser('~'), '.config', 'tunnelforge', 'logs')

# 로그 파일 경로
LOG_FILE = os.path.join(LOG_DIR, 'tunnelforge.log')

# 로그 포맷
LOG_FORMAT = '[%(asctime)s] %(levelname)s [%(name)s] %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# 루트 로거 설정 여부
_root_configured = False


def get_logger(name: str) -> logging.Logger:
    """모듈별 로거 반환

    Args:
        name: 모듈 이름 (예: 'tunnel_engine', 'config_manager')

    Returns:
        설정된 Logger 인스턴스

    사용법:
        from src.core.logger import get_logger
        logger = get_logger('my_module')
        logger.info("메시지")
        logger.error("에러 메시지")
    """
    logger = logging.getLogger(f'tunnelforge.{name}')

    # 루트 로거가 아직 설정되지 않았으면 설정
    global _root_configured
    if not _root_configured:
        _setup_root_logger()
        _root_configured = True

    return logger


def _setup_root_logger():
    """루트 로거 설정 (앱 시작 시 한 번만 호출)"""
    # 로그 디렉토리 생성
    os.makedirs(LOG_DIR, exist_ok=True)

    # 루트 로거 가져오기
    root_logger = logging.getLogger('tunnelforge')
    root_logger.setLevel(logging.DEBUG)

    # 이미 핸들러가 있으면 스킵 (중복 방지)
    if root_logger.handlers:
        return

    # 파일 핸들러 (RotatingFileHandler)
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root_logger.addHandler(file_handler)
    except Exception as e:
        # 파일 로깅 실패 시 콘솔에만 출력
        print(f"[Logger] 파일 로깅 초기화 실패: {e}")

    # 콘솔 핸들러 (개발 환경 또는 콘솔 모드에서만)
    # PyInstaller --noconsole 빌드에서는 stdout이 None일 수 있음
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)  # 콘솔은 INFO 이상만
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root_logger.addHandler(console_handler)


def get_log_file_path() -> str:
    """로그 파일 경로 반환"""
    return LOG_FILE


def get_log_dir() -> str:
    """로그 디렉토리 경로 반환"""
    return LOG_DIR


def read_log_file(max_lines: int = 500) -> str:
    """로그 파일 내용 읽기 (UI 표시용)

    Args:
        max_lines: 읽을 최대 줄 수 (기본 500줄)

    Returns:
        로그 파일 내용 문자열
    """
    if not os.path.exists(LOG_FILE):
        return "(로그 파일이 없습니다)"

    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # 최신 로그가 위에 오도록 역순 정렬
            recent_lines = lines[-max_lines:] if len(lines) > max_lines else lines
            return ''.join(recent_lines)
    except Exception as e:
        return f"(로그 파일 읽기 오류: {e})"


def filter_log_by_level(content: str, level: str) -> str:
    """로그 레벨로 필터링

    Args:
        content: 전체 로그 내용
        level: 필터링할 레벨 ('ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR')

    Returns:
        필터링된 로그 내용
    """
    if level == 'ALL':
        return content

    filtered_lines = []
    level_upper = level.upper()

    for line in content.splitlines():
        # 로그 레벨 확인 (포맷: [timestamp] LEVEL [module] message)
        if f'] {level_upper} [' in line:
            filtered_lines.append(line)
        elif level_upper == 'ERROR' and '] ERROR [' in line:
            filtered_lines.append(line)
        elif level_upper == 'WARNING' and ('] WARNING [' in line or '] ERROR [' in line):
            filtered_lines.append(line)
        elif level_upper == 'INFO' and ('] INFO [' in line or '] WARNING [' in line or '] ERROR [' in line):
            filtered_lines.append(line)

    return '\n'.join(filtered_lines) if filtered_lines else "(해당 레벨의 로그가 없습니다)"


def clear_log_file() -> tuple:
    """로그 파일 비우기

    Returns:
        (success: bool, message: str)
    """
    try:
        if os.path.exists(LOG_FILE):
            open(LOG_FILE, 'w', encoding='utf-8').close()
        return True, "로그가 초기화되었습니다."
    except Exception as e:
        return False, f"로그 초기화 실패: {e}"
