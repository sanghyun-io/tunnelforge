"""
One-Click 마이그레이션 전용 로거

실행마다 독립적인 로그 파일을 생성합니다.
- 경로: %LOCALAPPDATA%/TunnelForge/logs/migration_{schema}_{YYYYMMDD_HHMMSS}_{runid}.log
- 로테이션 없음 (per-run 파일은 불변)
- Python logging은 thread-safe이므로 QThread에서 안전하게 사용 가능
"""
import logging
import os
import uuid
from datetime import datetime
from typing import Optional


def _get_migration_log_dir() -> str:
    """마이그레이션 로그 디렉토리 경로"""
    if os.name == 'nt':
        return os.path.join(os.environ.get('LOCALAPPDATA', ''), 'TunnelForge', 'logs')
    else:
        return os.path.join(os.path.expanduser('~'), '.config', 'tunnelforge', 'logs')


def create_oneclick_logger(schema: str) -> tuple:
    """One-Click 마이그레이션 전용 per-run 로거 생성

    Args:
        schema: 마이그레이션 대상 스키마명

    Returns:
        (logger, log_path): 로거 인스턴스와 로그 파일 경로
    """
    log_dir = _get_migration_log_dir()
    os.makedirs(log_dir, exist_ok=True)

    run_id = uuid.uuid4().hex[:8]
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = f"migration_{schema}_{timestamp}_{run_id}.log"
    log_path = os.path.join(log_dir, log_filename)

    # 고유한 로거 이름 (실행마다 독립, 전역 로거와 격리)
    logger_name = f"tunnelforge.oneclick.{timestamp}_{run_id}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # 전역 로거로 전파하지 않음

    # 기존 핸들러 제거 (재사용 방지)
    logger.handlers.clear()

    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-5s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)

    return logger, log_path


def close_oneclick_logger(logger: logging.Logger) -> None:
    """로거 종료 (핸들러 닫기)

    Worker finally 블록에서 반드시 호출해야 합니다.
    """
    for handler in logger.handlers[:]:
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)
