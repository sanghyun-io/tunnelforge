"""
스케줄 백업 관리
- Cron 스타일 스케줄 설정
- 자동 DB Export 실행
- 백업 보관 정책 (개수, 기간)
- SQL 쿼리 실행 (SELECT → CSV/JSON, DML → commit)
"""
import csv
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Dict, Any, Optional, Callable, Tuple

from src.core.logger import get_logger

logger = get_logger(__name__)


class ScheduleTaskType(str, Enum):
    """스케줄 작업 유형"""
    BACKUP = "backup"
    SQL_QUERY = "sql_query"


@dataclass
class ScheduleConfig:
    """스케줄 백업/SQL 실행 설정"""
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

    # === SQL 쿼리 실행 전용 필드 ===
    task_type: str = "backup"           # 작업 유형: backup, sql_query
    sql_query: str = ""                 # 실행할 SQL (;로 멀티 쿼리 구분)
    result_format: str = "csv"          # 결과 저장 형식: csv, json, none
    result_output_dir: str = ""         # 결과 저장 경로 (없으면 output_dir 사용)
    result_filename_pattern: str = "{name}_{timestamp}"  # 파일명 패턴
    query_timeout: int = 300            # 타임아웃 (초)
    result_retention_count: int = 10    # 결과 파일 보관 개수
    result_retention_days: int = 30     # 결과 파일 보관 기간

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScheduleConfig':
        """딕셔너리에서 생성 (하위 호환성 지원)"""
        # 기존 설정에 새 필드가 없으면 기본값 적용
        defaults = {
            'task_type': 'backup',
            'sql_query': '',
            'result_format': 'csv',
            'result_output_dir': '',
            'result_filename_pattern': '{name}_{timestamp}',
            'query_timeout': 300,
            'result_retention_count': 10,
            'result_retention_days': 30,
        }
        for key, default_value in defaults.items():
            if key not in data:
                data[key] = default_value
        return cls(**data)

    def is_sql_query_task(self) -> bool:
        """SQL 쿼리 작업 여부"""
        return self.task_type == ScheduleTaskType.SQL_QUERY.value

    def get_result_output_path(self) -> str:
        """결과 저장 경로 반환 (result_output_dir 우선, 없으면 output_dir)"""
        return self.result_output_dir or self.output_dir


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

        return self._execute_task(schedule)

    def _execute_task(self, schedule: ScheduleConfig) -> tuple:
        """작업 유형별 분기 실행

        Returns:
            (success, message)
        """
        if schedule.is_sql_query_task():
            return self._execute_sql_query(schedule)
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
                            # 작업 실행 (백업 또는 SQL 쿼리)
                            success, message = self._execute_task(schedule)
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

    # =========================================================================
    # SQL 쿼리 실행 기능
    # =========================================================================

    def _execute_sql_query(self, schedule: ScheduleConfig) -> Tuple[bool, str]:
        """SQL 쿼리 실행

        Returns:
            (success, message)
        """
        from src.core.db_connector import MySQLConnector

        logger.info(f"SQL 쿼리 실행 시작: {schedule.name}")

        try:
            # 터널 연결 확인
            if not self.tunnel_engine.is_running(schedule.tunnel_id):
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

            # DB 연결
            connector = MySQLConnector(
                host=conn_info.get('host', '127.0.0.1'),
                port=conn_info.get('local_port', 3306),
                user=conn_info.get('db_user', 'root'),
                password=conn_info.get('db_password', ''),
                database=schedule.schema if schedule.schema else None
            )

            success, msg = connector.connect()
            if not success:
                error_msg = f"DB 연결 실패: {msg}"
                logger.error(error_msg)
                self._log_backup(schedule, False, error_msg)
                return False, error_msg

            try:
                # 멀티 쿼리 파싱 (세미콜론으로 구분)
                queries = self._parse_sql_queries(schedule.sql_query)
                if not queries:
                    error_msg = "실행할 SQL 쿼리가 없습니다."
                    logger.error(error_msg)
                    self._log_backup(schedule, False, error_msg)
                    return False, error_msg

                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                results_saved = []
                affected_rows_total = 0

                for idx, query in enumerate(queries):
                    query_result = self._execute_single_query(
                        connector, schedule, query, timestamp, idx
                    )
                    if not query_result['success']:
                        error_msg = f"쿼리 실행 실패 (#{idx + 1}): {query_result['error']}"
                        logger.error(error_msg)
                        self._log_backup(schedule, False, error_msg)
                        return False, error_msg

                    if query_result.get('file_path'):
                        results_saved.append(query_result['file_path'])
                    if query_result.get('affected_rows'):
                        affected_rows_total += query_result['affected_rows']

                # 결과 파일 정리 (보관 정책)
                self._cleanup_old_results(schedule)

                # last_run 업데이트
                schedule.last_run = datetime.now().isoformat()

                # 결과 메시지 생성
                if results_saved:
                    message = f"SQL 실행 완료: {len(queries)}개 쿼리, 결과 파일 {len(results_saved)}개 저장"
                else:
                    message = f"SQL 실행 완료: {len(queries)}개 쿼리, {affected_rows_total}행 영향"

                logger.info(message)
                self._log_backup(schedule, True, message)
                return True, message

            finally:
                connector.disconnect()

        except Exception as e:
            error_msg = f"SQL 쿼리 실행 오류: {str(e)}"
            logger.exception(error_msg)
            self._log_backup(schedule, False, error_msg)
            return False, error_msg

    def _parse_sql_queries(self, sql_text: str) -> List[str]:
        """SQL 텍스트를 개별 쿼리로 파싱

        세미콜론으로 구분, 문자열 내부 세미콜론은 무시
        """
        if not sql_text or not sql_text.strip():
            return []

        queries = []
        current_query = []
        in_string = False
        string_char = None

        for char in sql_text:
            if char in ('"', "'") and not in_string:
                in_string = True
                string_char = char
            elif char == string_char and in_string:
                in_string = False
                string_char = None

            if char == ';' and not in_string:
                query = ''.join(current_query).strip()
                if query:
                    queries.append(query)
                current_query = []
            else:
                current_query.append(char)

        # 마지막 쿼리 (세미콜론 없이 끝난 경우)
        last_query = ''.join(current_query).strip()
        if last_query:
            queries.append(last_query)

        return queries

    def _execute_single_query(
        self,
        connector,
        schedule: ScheduleConfig,
        query: str,
        timestamp: str,
        query_index: int
    ) -> Dict[str, Any]:
        """단일 쿼리 실행

        Returns:
            {
                'success': bool,
                'error': str (실패 시),
                'file_path': str (SELECT 결과 저장 시),
                'affected_rows': int (DML 실행 시)
            }
        """
        try:
            query_upper = query.upper().strip()
            is_select = query_upper.startswith('SELECT')

            with connector.connection.cursor() as cursor:
                # 타임아웃 설정 (MySQL 8.0+)
                if schedule.query_timeout > 0:
                    try:
                        cursor.execute(
                            f"SET SESSION MAX_EXECUTION_TIME = {schedule.query_timeout * 1000}"
                        )
                    except Exception:
                        # MAX_EXECUTION_TIME 미지원 시 무시
                        pass

                # 쿼리 실행
                cursor.execute(query)

                if is_select:
                    # SELECT 결과 저장
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    rows = cursor.fetchall()

                    if schedule.result_format != 'none' and rows:
                        file_path = self._save_query_result(
                            schedule, columns, rows, timestamp, query_index
                        )
                        return {
                            'success': True,
                            'file_path': file_path,
                            'row_count': len(rows)
                        }
                    return {'success': True, 'row_count': len(rows) if rows else 0}
                else:
                    # DML (INSERT, UPDATE, DELETE)
                    connector.connection.commit()
                    return {
                        'success': True,
                        'affected_rows': cursor.rowcount
                    }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def _save_query_result(
        self,
        schedule: ScheduleConfig,
        columns: List[str],
        rows: List[Dict[str, Any]],
        timestamp: str,
        query_index: int
    ) -> str:
        """쿼리 결과를 파일로 저장

        Returns:
            저장된 파일 경로
        """
        # 출력 디렉토리
        output_dir = schedule.get_result_output_path()
        os.makedirs(output_dir, exist_ok=True)

        # 파일명 생성
        filename_base = schedule.result_filename_pattern.format(
            name=schedule.name,
            timestamp=timestamp,
            date=timestamp[:8]  # YYYYMMDD
        )

        # 멀티 쿼리인 경우 suffix 추가
        if query_index > 0:
            filename_base = f"{filename_base}_{query_index + 1:02d}"

        # 확장자
        ext = 'csv' if schedule.result_format == 'csv' else 'json'
        filename = f"{filename_base}.{ext}"
        file_path = os.path.join(output_dir, filename)

        # 저장
        if schedule.result_format == 'csv':
            self._save_as_csv(file_path, columns, rows)
        else:
            self._save_as_json(file_path, columns, rows)

        logger.info(f"쿼리 결과 저장: {file_path} ({len(rows)}행)")
        return file_path

    def _save_as_csv(
        self,
        file_path: str,
        columns: List[str],
        rows: List[Dict[str, Any]]
    ):
        """CSV 형식으로 저장"""
        with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def _save_as_json(
        self,
        file_path: str,
        columns: List[str],
        rows: List[Dict[str, Any]]
    ):
        """JSON 형식으로 저장"""
        # datetime 객체 직렬화 처리
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            if hasattr(obj, '__str__'):
                return str(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        data = {
            'columns': columns,
            'row_count': len(rows),
            'generated_at': datetime.now().isoformat(),
            'data': rows
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=json_serializer)

    def _cleanup_old_results(self, schedule: ScheduleConfig):
        """오래된 결과 파일 정리 (보관 정책 적용)"""
        try:
            output_dir = schedule.get_result_output_path()
            if not os.path.exists(output_dir):
                return

            # 파일명 패턴에서 prefix 추출
            prefix = schedule.name

            # 결과 파일 목록
            result_files = []
            for name in os.listdir(output_dir):
                if not name.startswith(prefix):
                    continue

                full_path = os.path.join(output_dir, name)
                if not os.path.isfile(full_path):
                    continue

                # csv 또는 json 파일만
                if not (name.endswith('.csv') or name.endswith('.json')):
                    continue

                # 파일 수정 시간
                mtime = datetime.fromtimestamp(os.path.getmtime(full_path))
                result_files.append((full_path, mtime))

            if not result_files:
                return

            # 시간순 정렬 (오래된 것부터)
            result_files.sort(key=lambda x: x[1])

            now = datetime.now()
            to_delete = []

            # retention_days 체크
            cutoff = now - timedelta(days=schedule.result_retention_days)
            for path, mtime in result_files:
                if mtime < cutoff:
                    to_delete.append(path)

            # retention_count 체크
            remaining = [f for f in result_files if f[0] not in to_delete]
            if len(remaining) > schedule.result_retention_count:
                excess = len(remaining) - schedule.result_retention_count
                for path, _ in remaining[:excess]:
                    to_delete.append(path)

            # 삭제 실행
            for path in to_delete:
                try:
                    os.remove(path)
                    logger.info(f"오래된 결과 파일 삭제: {path}")
                except Exception as e:
                    logger.error(f"결과 파일 삭제 실패: {path} - {e}")

        except Exception as e:
            logger.error(f"결과 파일 정리 오류: {e}")
