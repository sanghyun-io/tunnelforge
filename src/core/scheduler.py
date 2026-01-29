"""
스케줄 백업 관리
- Cron 스타일 스케줄 설정
- 자동 DB Export 실행
- 백업 보관 정책 (개수, 기간)
"""
import os
import re
import json
import time
import shutil
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path

from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScheduleConfig:
    """스케줄 백업 설정"""
    id: str
    name: str
    tunnel_id: str              # 사용할 터널 ID
    schema: str                 # Export 대상 스키마
    tables: List[str] = field(default_factory=list)  # 빈 리스트 = 전체
    output_dir: str = ""        # 출력 디렉토리
    cron_expression: str = "0 3 * * *"  # 기본: 매일 03:00
    enabled: bool = True
    retention_count: int = 5    # 보관할 백업 수
    retention_days: int = 30    # 보관 기간 (일)
    last_run: Optional[str] = None  # ISO format
    next_run: Optional[str] = None  # ISO format

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScheduleConfig':
        """딕셔너리에서 생성"""
        return cls(**data)


class CronParser:
    """간단한 Cron 표현식 파서

    지원 형식: "분 시 일 월 요일"
    예:
        "0 3 * * *"   = 매일 03:00
        "0 0 * * 0"   = 매주 일요일 00:00
        "0 12 1 * *"  = 매월 1일 12:00
        "30 6 * * 1-5" = 평일 06:30
    """

    @staticmethod
    def parse_field(field: str, min_val: int, max_val: int, current: int) -> List[int]:
        """크론 필드를 값 목록으로 파싱"""
        if field == '*':
            return list(range(min_val, max_val + 1))

        values = []
        for part in field.split(','):
            # 범위 (예: 1-5)
            if '-' in part:
                start, end = part.split('-')
                values.extend(range(int(start), int(end) + 1))
            # 간격 (예: */5)
            elif part.startswith('*/'):
                step = int(part[2:])
                values.extend(range(min_val, max_val + 1, step))
            else:
                values.append(int(part))

        return sorted(set(v for v in values if min_val <= v <= max_val))

    @staticmethod
    def get_next_run(expression: str, after: datetime = None) -> Optional[datetime]:
        """다음 실행 시간 계산

        Args:
            expression: Cron 표현식 "분 시 일 월 요일"
            after: 이 시간 이후의 다음 실행 시간 (기본: 현재)

        Returns:
            다음 실행 datetime 또는 None (파싱 실패 시)
        """
        if after is None:
            after = datetime.now()

        try:
            parts = expression.strip().split()
            if len(parts) != 5:
                logger.warning(f"잘못된 cron 표현식: {expression}")
                return None

            minute_field, hour_field, day_field, month_field, dow_field = parts

            # 최대 1년간 검색
            check_time = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
            end_time = after + timedelta(days=366)

            while check_time < end_time:
                minutes = CronParser.parse_field(minute_field, 0, 59, check_time.minute)
                hours = CronParser.parse_field(hour_field, 0, 23, check_time.hour)
                days = CronParser.parse_field(day_field, 1, 31, check_time.day)
                months = CronParser.parse_field(month_field, 1, 12, check_time.month)
                dows = CronParser.parse_field(dow_field, 0, 6, check_time.weekday())
                # cron에서 0=일요일, Python에서 0=월요일 변환
                # Python weekday(): 월=0, 화=1, ..., 일=6
                # Cron: 일=0, 월=1, ..., 토=6
                python_dow = (check_time.weekday() + 1) % 7

                if (check_time.month in months and
                    check_time.day in days and
                    check_time.hour in hours and
                    check_time.minute in minutes and
                    python_dow in dows):
                    return check_time

                check_time += timedelta(minutes=1)

            return None

        except Exception as e:
            logger.error(f"Cron 파싱 오류: {e}")
            return None

    @staticmethod
    def describe(expression: str) -> str:
        """Cron 표현식을 사람이 읽기 쉬운 형태로 변환"""
        try:
            parts = expression.strip().split()
            if len(parts) != 5:
                return expression

            minute, hour, day, month, dow = parts

            # 매일
            if day == '*' and month == '*' and dow == '*':
                if minute == '0' and hour != '*':
                    return f"매일 {hour}:00"
                elif minute != '*' and hour != '*':
                    return f"매일 {hour}:{minute.zfill(2)}"

            # 매주
            dow_names = ['일', '월', '화', '수', '목', '금', '토']
            if day == '*' and month == '*' and dow != '*':
                if dow.isdigit():
                    day_name = dow_names[int(dow)]
                    return f"매주 {day_name}요일 {hour}:{minute.zfill(2)}"
                elif dow == '1-5':
                    return f"평일 {hour}:{minute.zfill(2)}"

            # 매월
            if day != '*' and month == '*' and dow == '*':
                return f"매월 {day}일 {hour}:{minute.zfill(2)}"

            return expression

        except Exception:
            return expression


class BackupScheduler:
    """스케줄 백업 관리자"""

    def __init__(self, config_manager, tunnel_engine):
        """
        Args:
            config_manager: ConfigManager 인스턴스
            tunnel_engine: TunnelEngine 인스턴스
        """
        self.config_manager = config_manager
        self.tunnel_engine = tunnel_engine
        self._schedules: List[ScheduleConfig] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._callbacks: List[Callable[[str, bool, str], None]] = []
        self._lock = threading.Lock()

        # 스케줄 로드
        self._load_schedules()

    def _load_schedules(self):
        """설정에서 스케줄 로드"""
        schedules_data = self.config_manager.get_app_setting('schedules', [])
        self._schedules = []
        for data in schedules_data:
            try:
                schedule = ScheduleConfig.from_dict(data)
                # next_run 갱신
                if schedule.enabled:
                    next_run = CronParser.get_next_run(schedule.cron_expression)
                    if next_run:
                        schedule.next_run = next_run.isoformat()
                self._schedules.append(schedule)
            except Exception as e:
                logger.error(f"스케줄 로드 실패: {e}")

    def _save_schedules(self):
        """스케줄을 설정에 저장"""
        schedules_data = [s.to_dict() for s in self._schedules]
        self.config_manager.set_app_setting('schedules', schedules_data)

    def add_callback(self, callback: Callable[[str, bool, str], None]):
        """백업 완료 콜백 등록

        Args:
            callback: callback(schedule_name, success, message)
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable):
        """콜백 제거"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _notify_callbacks(self, schedule_name: str, success: bool, message: str):
        """콜백 호출"""
        for callback in self._callbacks:
            try:
                callback(schedule_name, success, message)
            except Exception as e:
                logger.error(f"콜백 실행 오류: {e}")

    def start(self):
        """스케줄러 시작 (백그라운드 스레드)"""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("백업 스케줄러 시작")

    def stop(self):
        """스케줄러 중지"""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("백업 스케줄러 중지")

    def is_running(self) -> bool:
        """스케줄러 실행 중 여부"""
        return self._running

    def get_schedules(self) -> List[ScheduleConfig]:
        """모든 스케줄 반환"""
        return list(self._schedules)

    def get_schedule(self, schedule_id: str) -> Optional[ScheduleConfig]:
        """ID로 스케줄 조회"""
        for schedule in self._schedules:
            if schedule.id == schedule_id:
                return schedule
        return None

    def add_schedule(self, config: ScheduleConfig):
        """스케줄 추가"""
        with self._lock:
            # 중복 ID 체크
            for s in self._schedules:
                if s.id == config.id:
                    raise ValueError(f"중복된 스케줄 ID: {config.id}")

            # next_run 계산
            if config.enabled:
                next_run = CronParser.get_next_run(config.cron_expression)
                if next_run:
                    config.next_run = next_run.isoformat()

            self._schedules.append(config)
            self._save_schedules()
            logger.info(f"스케줄 추가: {config.name}")

    def update_schedule(self, config: ScheduleConfig):
        """스케줄 업데이트"""
        with self._lock:
            for i, s in enumerate(self._schedules):
                if s.id == config.id:
                    # next_run 재계산
                    if config.enabled:
                        next_run = CronParser.get_next_run(config.cron_expression)
                        if next_run:
                            config.next_run = next_run.isoformat()

                    self._schedules[i] = config
                    self._save_schedules()
                    logger.info(f"스케줄 업데이트: {config.name}")
                    return

            raise ValueError(f"스케줄을 찾을 수 없음: {config.id}")

    def remove_schedule(self, schedule_id: str):
        """스케줄 삭제"""
        with self._lock:
            for i, s in enumerate(self._schedules):
                if s.id == schedule_id:
                    removed = self._schedules.pop(i)
                    self._save_schedules()
                    logger.info(f"스케줄 삭제: {removed.name}")
                    return

            raise ValueError(f"스케줄을 찾을 수 없음: {schedule_id}")

    def set_enabled(self, schedule_id: str, enabled: bool):
        """스케줄 활성화/비활성화"""
        schedule = self.get_schedule(schedule_id)
        if schedule:
            schedule.enabled = enabled
            if enabled:
                next_run = CronParser.get_next_run(schedule.cron_expression)
                if next_run:
                    schedule.next_run = next_run.isoformat()
            self._save_schedules()
            logger.info(f"스케줄 {'활성화' if enabled else '비활성화'}: {schedule.name}")

    def run_now(self, schedule_id: str) -> tuple:
        """즉시 실행

        Returns:
            (success, message)
        """
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False, "스케줄을 찾을 수 없습니다."

        return self._execute_backup(schedule)

    def _run_loop(self):
        """메인 루프 (60초 간격 체크)"""
        while self._running and not self._stop_event.is_set():
            now = datetime.now()

            with self._lock:
                for schedule in self._schedules:
                    if not schedule.enabled:
                        continue

                    if not schedule.next_run:
                        continue

                    try:
                        next_run = datetime.fromisoformat(schedule.next_run)
                        if next_run <= now:
                            # 백업 실행
                            success, message = self._execute_backup(schedule)
                            self._notify_callbacks(schedule.name, success, message)

                            # next_run 갱신
                            new_next = CronParser.get_next_run(schedule.cron_expression)
                            if new_next:
                                schedule.next_run = new_next.isoformat()

                            self._save_schedules()
                    except Exception as e:
                        logger.error(f"스케줄 체크 오류 ({schedule.name}): {e}")

            # 60초 대기 (중단 가능)
            self._stop_event.wait(60)

    def _execute_backup(self, schedule: ScheduleConfig) -> tuple:
        """백업 실행

        Returns:
            (success, message)
        """
        from src.exporters.mysqlsh_exporter import MySQLShellExporter, MySQLShellConfig

        logger.info(f"백업 시작: {schedule.name}")

        try:
            # 터널 연결 확인
            if not self.tunnel_engine.is_running(schedule.tunnel_id):
                # 터널 시작 시도
                success, msg = self.tunnel_engine.start_tunnel(schedule.tunnel_id)
                if not success:
                    error_msg = f"터널 연결 실패: {msg}"
                    logger.error(error_msg)
                    self._log_backup(schedule, False, error_msg)
                    return False, error_msg

            # 연결 정보 가져오기
            conn_info = self.tunnel_engine.get_connection_info(schedule.tunnel_id)
            if not conn_info:
                error_msg = "연결 정보를 가져올 수 없습니다."
                logger.error(error_msg)
                self._log_backup(schedule, False, error_msg)
                return False, error_msg

            # 출력 디렉토리 생성
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_subdir = os.path.join(schedule.output_dir, f"{schedule.name}_{timestamp}")
            os.makedirs(output_subdir, exist_ok=True)

            # MySQLShell Export 실행
            config = MySQLShellConfig(
                host=conn_info.get('host', '127.0.0.1'),
                port=conn_info.get('local_port', 3306),
                user=conn_info.get('db_user', 'root'),
                password=conn_info.get('db_password', ''),
                schema=schedule.schema
            )

            exporter = MySQLShellExporter(config)

            # 테이블 지정 여부
            if schedule.tables:
                tables_param = schedule.tables
            else:
                tables_param = None  # 전체

            success, result = exporter.export_tables(
                output_dir=output_subdir,
                tables=tables_param,
                parallel=4
            )

            if success:
                # 백업 정리
                self._cleanup_old_backups(schedule)

                # last_run 업데이트
                schedule.last_run = datetime.now().isoformat()

                message = f"백업 완료: {output_subdir}"
                logger.info(message)
                self._log_backup(schedule, True, message)
                return True, message
            else:
                error_msg = f"Export 실패: {result}"
                logger.error(error_msg)
                self._log_backup(schedule, False, error_msg)
                return False, error_msg

        except Exception as e:
            error_msg = f"백업 오류: {str(e)}"
            logger.exception(error_msg)
            self._log_backup(schedule, False, error_msg)
            return False, error_msg

    def _cleanup_old_backups(self, schedule: ScheduleConfig):
        """오래된 백업 정리"""
        try:
            if not os.path.exists(schedule.output_dir):
                return

            # 백업 디렉토리 목록 (schedule.name_으로 시작하는 것만)
            prefix = f"{schedule.name}_"
            backup_dirs = []

            for name in os.listdir(schedule.output_dir):
                full_path = os.path.join(schedule.output_dir, name)
                if os.path.isdir(full_path) and name.startswith(prefix):
                    try:
                        # 타임스탬프 추출
                        timestamp_str = name[len(prefix):]
                        timestamp = datetime.strptime(timestamp_str, '%Y%m%d_%H%M%S')
                        backup_dirs.append((full_path, timestamp))
                    except ValueError:
                        continue

            if not backup_dirs:
                return

            # 시간순 정렬 (오래된 것부터)
            backup_dirs.sort(key=lambda x: x[1])

            now = datetime.now()
            to_delete = []

            # retention_days 체크
            cutoff = now - timedelta(days=schedule.retention_days)
            for path, timestamp in backup_dirs:
                if timestamp < cutoff:
                    to_delete.append(path)

            # retention_count 체크 (남은 것 중에서)
            remaining = [b for b in backup_dirs if b[0] not in to_delete]
            if len(remaining) > schedule.retention_count:
                excess = len(remaining) - schedule.retention_count
                for path, _ in remaining[:excess]:
                    to_delete.append(path)

            # 삭제 실행
            for path in to_delete:
                try:
                    shutil.rmtree(path)
                    logger.info(f"오래된 백업 삭제: {path}")
                except Exception as e:
                    logger.error(f"백업 삭제 실패: {path} - {e}")

        except Exception as e:
            logger.error(f"백업 정리 오류: {e}")

    def _log_backup(self, schedule: ScheduleConfig, success: bool, message: str):
        """백업 로그 저장"""
        try:
            # 로그 디렉토리
            if os.name == 'nt':
                log_dir = os.path.join(
                    os.environ.get('LOCALAPPDATA', ''),
                    'TunnelForge', 'backup_logs'
                )
            else:
                log_dir = os.path.expanduser('~/.tunnelforge/backup_logs')

            os.makedirs(log_dir, exist_ok=True)

            # 오늘 날짜 로그 파일
            log_file = os.path.join(log_dir, f"backup_{datetime.now().strftime('%Y%m%d')}.log")

            with open(log_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                status = "성공" if success else "실패"
                f.write(f"[{timestamp}] [{status}] {schedule.name}: {message}\n")

        except Exception as e:
            logger.error(f"백업 로그 저장 실패: {e}")

    def get_backup_logs(self, days: int = 7) -> List[Dict[str, Any]]:
        """최근 백업 로그 조회

        Args:
            days: 조회할 일수

        Returns:
            로그 항목 목록
        """
        logs = []

        try:
            if os.name == 'nt':
                log_dir = os.path.join(
                    os.environ.get('LOCALAPPDATA', ''),
                    'TunnelForge', 'backup_logs'
                )
            else:
                log_dir = os.path.expanduser('~/.tunnelforge/backup_logs')

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
