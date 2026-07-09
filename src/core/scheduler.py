"""
스케줄 백업 관리
- Cron 스타일 스케줄 설정
- 자동 DB Export 실행
- 백업 보관 정책 (개수, 기간)
- SQL 쿼리 실행 (SELECT → CSV/JSON, DML → commit)

BackupScheduler는 스케줄링 엔진(등록/실행 큐/직렬화 실행 루프)만 담당하며,
실제 작업 실행은 아래 협력 모듈에 위임한다:
- schedule_config: ScheduleConfig 등 데이터 모델
- cron_parser: CronParser
- execution_log_writer: ExecutionLogWriter (실행 로그 기록/조회)
- backup_task_executor: BackupTaskExecutor (RustDumpExporter 백업 실행)
- sql_query_task_executor: SqlQueryTaskExecutor (SQL 쿼리 실행)
- retention_policy: 보관 정책 선정 로직 (위 두 executor가 공용)
"""
import copy
import queue
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable, Tuple

from src.core.logger import get_logger
from src.core.constants import DEFAULT_LOCAL_HOST
from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.schedule_config import ScheduleTaskType, ScheduleConfig, _ExecutionJob, _ResolvedConnection
from src.core.cron_parser import CronParser
from src.core.execution_log_writer import ExecutionLogWriter
from src.core.backup_task_executor import BackupTaskExecutor
from src.core.sql_query_task_executor import SqlQueryTaskExecutor

# 하위 호환 재노출 (consumer: src/ui/dialogs/schedule_dialog.py, src/ui/main_window.py)
__all__ = [
    "ScheduleTaskType",
    "ScheduleConfig",
    "CronParser",
    "BackupScheduler",
]

logger = get_logger(__name__)


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

        # 실행 큐 상태 (run_now/스케줄 due 작업이 공유하는 직렬화된 실행 경로)
        self._execution_queue: "queue.Queue[_ExecutionJob]" = queue.Queue()
        self._execution_thread: Optional[threading.Thread] = None
        self._execution_stop_event = threading.Event()
        self._active_schedule_ids: set = set()

        # 작업 실행 협력자 조립 (DI - 아래 모듈들은 scheduler.py를 import하지 않는 leaf 모듈)
        self._log_writer = ExecutionLogWriter()
        self._backup_executor = BackupTaskExecutor(
            resolve_connection=self._resolve_connection,
            log_writer=self._log_writer,
        )
        self._sql_executor = SqlQueryTaskExecutor(
            resolve_connection=self._resolve_connection,
            connector_factory=self._make_connector,
            log_writer=self._log_writer,
        )

        # 스케줄 로드
        self._load_schedules()

    def _make_connector(self, *args, **kwargs):
        """SqlQueryTaskExecutor가 주입받는 connector factory

        모듈 전역 이름(create_rust_db_connector)을 호출 시점에 조회하므로
        monkeypatch.setattr("src.core.scheduler.create_rust_db_connector", ...)가 그대로 반영된다.
        SqlQueryTaskExecutor가 create_rust_db_connector를 직접 import하면 이 monkeypatch가 무효화된다.
        """
        return create_rust_db_connector(*args, **kwargs)

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

        # 실행 워커 스레드도 협조적으로 중지 (강제 종료 없음)
        self._execution_stop_event.set()
        if self._execution_thread and self._execution_thread.is_alive():
            self._execution_thread.join(timeout=5)
        self._execution_thread = None

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

    def _find_schedule_locked(self, schedule_id: str) -> Optional[ScheduleConfig]:
        """ID로 스케줄 조회 (호출자가 이미 _lock을 보유한 상태에서 사용)"""
        for schedule in self._schedules:
            if schedule.id == schedule_id:
                return schedule
        return None

    def run_now(self, schedule_id: str) -> tuple:
        """즉시 실행 요청을 실행 큐에 등록 (비동기)

        run_now와 스케줄 due 실행은 동일한 직렬화된 백그라운드 실행 경로를 공유한다.
        완료 여부는 기존 콜백(add_callback)으로 통지된다.

        Returns:
            (success, message) - message는 "등록됨"을 의미하며 "완료"를 의미하지 않는다.
        """
        with self._lock:
            schedule = self._find_schedule_locked(schedule_id)
            if not schedule:
                return False, "스케줄을 찾을 수 없습니다."
            if schedule.id in self._active_schedule_ids:
                return False, "이미 실행 중인 스케줄입니다."
            self._active_schedule_ids.add(schedule.id)
            job = _ExecutionJob(copy.deepcopy(schedule), update_next_run=False)

        self._ensure_execution_thread()
        self._execution_queue.put(job)
        return True, "실행 요청이 등록되었습니다. 완료되면 실행 로그와 알림으로 표시됩니다."

    def _execute_task(self, schedule: ScheduleConfig) -> tuple:
        """작업 유형별 분기 실행

        Returns:
            (success, message)
        """
        if schedule.is_sql_query_task():
            return self._execute_sql_query(schedule)
        return self._execute_backup(schedule)

    def _ensure_execution_thread(self):
        """실행 워커 스레드가 살아있지 않으면 새로 시작"""
        if self._execution_thread and self._execution_thread.is_alive():
            return
        self._execution_stop_event.clear()
        self._execution_thread = threading.Thread(
            target=self._execution_worker_loop,
            daemon=True,
            name="TunnelForgeSchedulerExecution",
        )
        self._execution_thread.start()

    def _execution_worker_loop(self):
        """실행 큐에서 작업을 꺼내 순차 실행 (run_now/due 스케줄 공용)"""
        while not self._execution_stop_event.is_set():
            try:
                job = self._execution_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._run_execution_job(job)
            finally:
                self._execution_queue.task_done()

    def _run_execution_job(self, job: "_ExecutionJob"):
        """실행 큐에서 꺼낸 작업 하나를 실행하고 결과를 반영"""
        success = False
        message = ""
        try:
            success, message = self._execute_task(job.schedule)
        except Exception as e:
            message = f"스케줄 실행 오류: {e}"
            logger.exception(message)

        self._notify_callbacks(job.schedule.name, success, message)

        with self._lock:
            live = self._find_schedule_locked(job.schedule.id)
            if live:
                if job.schedule.last_run:
                    live.last_run = job.schedule.last_run
                if job.update_next_run and live.enabled:
                    next_run = CronParser.get_next_run(live.cron_expression)
                    live.next_run = next_run.isoformat() if next_run else None
                self._save_schedules()
            self._active_schedule_ids.discard(job.schedule.id)

    def _snapshot_due_jobs(self, now: datetime) -> List["_ExecutionJob"]:
        """실행 대상 스케줄을 락 안에서 스냅샷만 뜨고, 실제 실행은 락 밖에서 진행하기 위한 준비"""
        jobs = []
        with self._lock:
            for schedule in self._schedules:
                if not schedule.enabled or not schedule.next_run:
                    continue
                if schedule.id in self._active_schedule_ids:
                    continue
                try:
                    if datetime.fromisoformat(schedule.next_run) <= now:
                        self._active_schedule_ids.add(schedule.id)
                        jobs.append(_ExecutionJob(copy.deepcopy(schedule), update_next_run=True))
                except Exception as e:
                    logger.error(f"스케줄 체크 오류 ({schedule.name}): {e}")
        return jobs

    def _run_loop(self):
        """메인 루프 (60초 간격 체크)

        due 스케줄은 락 안에서 스냅샷만 뜨고, 실제 실행은 실행 큐를 통해 락 밖에서 처리한다
        (UI/다른 스레드가 _lock을 기다리며 블로킹되는 것을 방지).
        """
        while self._running and not self._stop_event.is_set():
            now = datetime.now()

            jobs = self._snapshot_due_jobs(now)
            for job in jobs:
                self._ensure_execution_thread()
                self._execution_queue.put(job)

            # 60초 대기 (중단 가능)
            self._stop_event.wait(60)

    def _resolve_connection(self, schedule: ScheduleConfig) -> Tuple[Optional["_ResolvedConnection"], str]:
        """백업/SQL 실행이 공유하는 터널 연결 정보 + 복호화된 자격 증명 해석

        Returns:
            (resolved, error_message) - 실패 시 resolved는 None이고 error_message에 사유가 담긴다.
        """
        config = getattr(self.tunnel_engine, 'tunnel_configs', {}).get(schedule.tunnel_id)
        if not config:
            stored_tunnels = self.config_manager.load_config().get('tunnels', [])
            config = next((t for t in stored_tunnels if t.get('id') == schedule.tunnel_id), None)
        if not config:
            return None, "터널 설정을 찾을 수 없습니다."

        # 터널 연결 확인
        if not self.tunnel_engine.is_running(schedule.tunnel_id):
            # 터널 시작 시도 (설정 딕셔너리 전체를 전달 - 터널 ID 문자열이 아님)
            success, msg = self.tunnel_engine.start_tunnel(config)
            if not success:
                return None, f"터널 연결 실패: {msg}"

        # 연결 정보 가져오기 (host, port) 튜플만 반환됨
        host, port = self.tunnel_engine.get_connection_info(schedule.tunnel_id)
        if host is None or port is None:
            return None, "연결 정보를 가져올 수 없습니다."

        # 저장된 자격 증명 복호화
        credential_result = None
        get_credentials = getattr(self.config_manager, 'get_tunnel_credentials', None)
        if callable(get_credentials):
            credential_result = get_credentials(schedule.tunnel_id)

        credential_user = ''
        credential_password = ''
        if isinstance(credential_result, (tuple, list)) and len(credential_result) >= 2:
            credential_user = credential_result[0] or ''
            credential_password = credential_result[1] or ''

        user = credential_user or config.get('db_user') or config.get('db_username') or 'root'
        password = credential_password or config.get('db_password') or ''
        engine = normalize_db_engine(config.get('db_engine'), config.get('remote_port') or port)

        resolved = _ResolvedConnection(
            host=host or DEFAULT_LOCAL_HOST,
            port=int(port),
            user=user,
            password=password,
            engine=engine,
        )
        return resolved, ""

    # =========================================================================
    # 작업 실행 - BackupTaskExecutor / SqlQueryTaskExecutor로 위임
    # (아래 얇은 위임 메서드는 tests/test_scheduler.py가 인스턴스에서 직접 호출하는
    #  private 표면이므로 이름/시그니처를 그대로 유지한다)
    # =========================================================================

    def _execute_backup(self, schedule: ScheduleConfig) -> tuple:
        """백업 실행 (BackupTaskExecutor에 위임)

        Returns:
            (success, message)
        """
        return self._backup_executor.execute(schedule)

    def get_backup_logs(self, days: int = 7) -> List[Dict[str, Any]]:
        """최근 백업 로그 조회 (ExecutionLogWriter에 위임)

        Args:
            days: 조회할 일수

        Returns:
            로그 항목 목록
        """
        return self._log_writer.get_logs(days)

    def _execute_sql_query(self, schedule: ScheduleConfig) -> Tuple[bool, str]:
        """SQL 쿼리 실행 (SqlQueryTaskExecutor에 위임)

        Returns:
            (success, message)
        """
        return self._sql_executor.execute(schedule)

    def _parse_sql_queries(self, sql_text: str) -> List[str]:
        """SQL 텍스트를 개별 쿼리로 파싱 (SqlQueryTaskExecutor에 위임)."""
        return self._sql_executor.parse_queries(sql_text)

    def _execute_single_query(
        self,
        connector,
        schedule: ScheduleConfig,
        query: str,
        timestamp: str,
        query_index: int
    ) -> Dict[str, Any]:
        """단일 쿼리 실행 (SqlQueryTaskExecutor에 위임)

        Returns:
            {
                'success': bool,
                'error': str (실패 시),
                'file_path': str (결과셋 저장 시),
                'row_count': int (결과셋인 경우),
                'affected_rows': int (DML 실행 시)
            }
        """
        return self._sql_executor.execute_single(connector, schedule, query, timestamp, query_index)
