"""
스케줄 실행 로그 기록/조회 (백업 + SQL 쿼리 작업 공용)
"""
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.core.logger import get_logger
from src.core.platform_paths import log_dir as platform_log_dir
from src.core.schedule_config import ScheduleConfig

logger = get_logger(__name__)


class ExecutionLogWriter:
    """스케줄 실행 결과를 파일로 기록하고 조회한다

    ⛔ 온디스크 디렉토리명 'backup_logs'와 파일 접두사 'backup_'는 절대 변경하지 않는다
    (기존 사용자 로그, get_backup_logs 왕복 호환을 보존하기 위함).
    """

    def log_execution(self, schedule: ScheduleConfig, success: bool, message: str):
        """실행 결과 로그 저장"""
        try:
            # 로그 디렉토리
            log_dir = str(platform_log_dir() / 'backup_logs')

            os.makedirs(log_dir, exist_ok=True)

            # 오늘 날짜 로그 파일
            log_file = os.path.join(log_dir, f"backup_{datetime.now().strftime('%Y%m%d')}.log")

            with open(log_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                status = "성공" if success else "실패"
                f.write(f"[{timestamp}] [{status}] {schedule.name}: {message}\n")

        except Exception as e:
            logger.error(f"백업 로그 저장 실패: {e}")

    def get_logs(self, days: int = 7) -> List[Dict[str, Any]]:
        """최근 실행 로그 조회

        Args:
            days: 조회할 일수

        Returns:
            로그 항목 목록
        """
        logs = []

        try:
            log_dir = str(platform_log_dir() / 'backup_logs')

            if not os.path.exists(log_dir):
                return logs

            # 최근 N일간의 로그 파일
            for i in range(days):
                date = datetime.now() - timedelta(days=i)
                log_file = os.path.join(log_dir, f"backup_{date.strftime('%Y%m%d')}.log")

                if os.path.exists(log_file):
                    with open(log_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            # 파싱: [timestamp] [status] name: message
                            match = re.match(
                                r'\[(.+?)\] \[(.+?)\] (.+?): (.+)',
                                line.strip()
                            )
                            if match:
                                logs.append({
                                    'timestamp': match.group(1),
                                    'status': match.group(2),
                                    'name': match.group(3),
                                    'message': match.group(4)
                                })
        except Exception as e:
            logger.error(f"백업 로그 조회 오류: {e}")

        return logs
