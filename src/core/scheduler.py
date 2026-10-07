"""
스케줄 백업 관리
- Cron 스타일 스케줄 설정
- 자동 DB Export 실행
- 백업 보관 정책 (개수, 기간)

BackupScheduler는 스케줄링 엔진(등록/실행 큐/직렬화 실행 루프)만 담당하며,
실제 작업 실행은 아래 협력 모듈에 위임한다:
- schedule_config: ScheduleConfig 등 데이터 모델
- cron_parser: CronParser
- execution_log_writer: ExecutionLogWriter (실행 로그 기록/조회)
- backup_task_executor: BackupTaskExecutor (RustDumpExporter 백업 실행)
"""
import copy
import queue
import threading
import time
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable, Tuple

from src.core.logger import get_logger
from src.core.constants import DEFAULT_LOCAL_HOST
from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.schedule_config import ScheduleTaskType, ScheduleConfig, _ExecutionJob, _ResolvedConnection
from src.core.cron_parser import CronParser
from src.core.execution_log_writer import ExecutionLogWriter
from src.core.backup_task_executor import BackupTaskExecutor
from src.core.job_history import KIND_SCHEDULED_BACKUP, STATUS_SKIPPED, job_begin, job_finish
from src.core.schedule_time import classify_due, validate_expression
from src.core import scheduled_backup_store as backup_store
from src.core.restore_rehearsal import RestoreRehearsal, rehearsal_target_error

# 하위 호환 재노출 (consumer: src/ui/dialogs/schedule_dialog.py, src/ui/main_window.py)
__all__ = [
    "ScheduleTaskType",
    "ScheduleConfig",
    "CronParser",
    "BackupScheduler",
]

logger = get_logger(__name__)

DEFAULT_MIN_INTERVAL_MINUTES = 15
SQL_TASK_UNSUPPORTED_MESSAGE = (
    "예약 SQL 실행은 지원되지 않습니다 (무인 상태의 쓰기 위험과 운영 읽기 전용 정책 때문). "
    "예약은 백업만 사용할 수 있습니다."
)


def describe_unattended_tunnel_failure(message: str) -> str:
    """무인 터널 시작 실패를 사용자에게 보이는 한 줄 사유로 바꾼다 (원인 코드 우선, 상세는 로그에만)."""
    text = message or ""
    if "ssh_host_key_unknown" in text:
        return ("SSH 호스트 키가 아직 신뢰되지 않았습니다. 예약 실행은 호스트 키를 자동으로 수락하지 않습니다. "
                "앱에서 이 연결을 한 번 직접 열어 지문을 확인하고 신뢰한 뒤 다시 시도하세요.")
    if "ssh_host_key_changed" in text:
        return ("SSH 서버의 호스트 키가 저장된 값과 다릅니다. 중간자 공격일 수 있어 예약 실행을 차단했습니다. "
                "서버 교체가 확실할 때만 터널 설정에서 '호스트 키 갱신'을 사용하세요.")
    if "Passphrase" in text or "개인키 비밀번호" in text:
        return ("SSH 개인키가 비밀번호로 보호되어 있어 예약 실행에서 사용할 수 없습니다 "
                "(키 비밀번호는 저장하지 않으며 무인 실행에서는 묻지 않습니다). 비밀번호 없는 전용 키를 사용하세요.")
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "알 수 없는 오류")
    return f"터널 연결 실패: {first_line[:200]}"


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
        self._rehearsal = RestoreRehearsal(
            resolve_connection=self._resolve_connection,
            find_tunnel_config=self._find_tunnel_config,
            connector_factory=self._make_connector,
        )
        self._backup_executor = BackupTaskExecutor(
            resolve_connection=self._resolve_connection,
            log_writer=self._log_writer,
            rehearsal=self._rehearsal,
        )

        # 스케줄 로드
        self._load_schedules()

    def _make_connector(self, *args, **kwargs):
        """RestoreRehearsal이 주입받는 connector factory

        모듈 전역 이름(create_rust_db_connector)을 호출 시점에 조회하므로
        monkeypatch.setattr("src.core.scheduler.create_rust_db_connector", ...)가 그대로 반영된다.
        협력 모듈이 create_rust_db_connector를 직접 import하면 이 monkeypatch가 무효화된다.
        """
        return create_rust_db_connector(*args, **kwargs)

    def _load_schedules(self):
        """설정에서 스케줄 로드"""
        schedules_data = self.config_manager.get_app_setting('schedules', [])
        self._schedules = []
        for data in schedules_data:
            try:
                schedule = ScheduleConfig.from_dict(data)
                # 저장된 next_run 은 그대로 둔다: 앱이 꺼져 있는 동안 지나간 실행을 시작 직후 감지해
                # (최대 1회) 따라잡거나 건너뛰기 위해서다. 유효한 next_run 이 없을 때만 새로 계산한다.
                if schedule.enabled and not self._valid_iso(schedule.next_run):
                    schedule.next_run = self._compute_next_run(schedule.cron_expression)
                self._schedules.append(schedule)
                if not schedule.is_sql_query_task() and schedule.output_dir:
                    backup_store.sweep_interrupted(schedule.output_dir, schedule.id, schedule_active=False)
            except Exception as e:
                logger.error(f"스케줄 로드 실패: {e}")

    @staticmethod
    def _valid_iso(value) -> bool:
        try:
            datetime.fromisoformat(value)
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _compute_next_run(expression: str, after: Optional[datetime] = None) -> Optional[str]:
        next_run = CronParser.get_next_run(expression, after)
        return next_run.isoformat() if next_run else None

    def min_interval_minutes(self) -> int:
        try:
            value = int(self.config_manager.get_app_setting('scheduled_backup_min_interval_minutes',
                                                            DEFAULT_MIN_INTERVAL_MINUTES))
        except (TypeError, ValueError):
            return DEFAULT_MIN_INTERVAL_MINUTES
        return max(1, value)

    def validate_schedule(self, config: ScheduleConfig) -> Optional[str]:
        """저장 전 검증. 사용자에게 보여줄 오류 문구 또는 None."""
        if config.is_sql_query_task():
            return SQL_TASK_UNSUPPORTED_MESSAGE
        if not config.output_dir:
            return "백업 출력 폴더를 지정하세요."
        error = rehearsal_target_error(self._find_tunnel_config(config.rehearsal_tunnel_id), config)
        if error:
            return error
        return validate_expression(config.cron_expression, self.min_interval_minutes())

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

    def has_active_jobs(self) -> bool:
        """대기 중이거나 실행 중인 예약 작업이 있는지"""
        with self._lock:
            return bool(self._active_schedule_ids)

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

            error = self.validate_schedule(config)
            if error:
                raise ValueError(error)

            # next_run 계산
            if config.enabled:
                config.next_run = self._compute_next_run(config.cron_expression) or config.next_run

            self._schedules.append(config)
            self._save_schedules()
            logger.info(f"스케줄 추가: {config.name}")

    def update_schedule(self, config: ScheduleConfig):
        """스케줄 업데이트"""
        with self._lock:
            for i, s in enumerate(self._schedules):
                if s.id == config.id:
                    error = self.validate_schedule(config)
                    if error:
                        raise ValueError(error)
                    # next_run 재계산
                    if config.enabled:
                        config.next_run = self._compute_next_run(config.cron_expression) or config.next_run

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
                # 다시 켜는 순간부터 계산한다: 꺼져 있던 동안의 실행은 '놓친 실행'이 아니다.
                schedule.next_run = self._compute_next_run(schedule.cron_expression) or schedule.next_run
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
                already_running = copy.deepcopy(schedule)
                job = None
            else:
                self._active_schedule_ids.add(schedule.id)
                job = _ExecutionJob(copy.deepcopy(schedule), update_next_run=False, trigger='manual')
        if job is None:
            self._record_skip(already_running, "이미 실행 중이라 '지금 실행' 요청을 건너뜀")
            return False, "이미 실행 중인 스케줄입니다."

        self._ensure_execution_thread()
        self._execution_queue.put(job)
        return True, "실행 요청이 등록되었습니다. 완료되면 실행 로그와 알림으로 표시됩니다."

    def _execute_task(self, schedule: ScheduleConfig, trigger: str = 'scheduled') -> tuple:
        """작업 유형별 분기 실행. 예약은 백업만 실행한다 (SQL 작업은 거부).

        Returns:
            (success, message)
        """
        if schedule.is_sql_query_task():
            self._log_writer.log_execution(schedule, False, SQL_TASK_UNSUPPORTED_MESSAGE)
            return False, SQL_TASK_UNSUPPORTED_MESSAGE
        return self._execute_backup(schedule, trigger)

    def _record_skip(self, schedule: ScheduleConfig, reason: str) -> None:
        """건너뛴 예약 실행을 작업 목록과 실행 로그에 남긴다."""
        try:
            job_id = job_begin(KIND_SCHEDULED_BACKUP, profile_id=schedule.tunnel_id, profile_name=schedule.name,
                               target=schedule.schema, mode="예약 실행")
            job_finish(job_id, STATUS_SKIPPED, error=reason)
            self._log_writer.log_execution(schedule, False, f"건너뜀: {reason}")
        except Exception:
            logger.warning("건너뛴 실행 기록 실패", exc_info=True)

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
            success, message = self._execute_task(job.schedule, job.trigger)
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
                    live.next_run = self._compute_next_run(live.cron_expression)
                self._save_schedules()
            self._active_schedule_ids.discard(job.schedule.id)

    def _snapshot_due_jobs(self, now: datetime) -> List["_ExecutionJob"]:
        """실행 대상 스케줄을 락 안에서 스냅샷만 뜨고, 실제 실행은 락 밖에서 진행하기 위한 준비"""
        jobs = []
        skipped: List[Tuple[ScheduleConfig, str]] = []
        now_epoch = now.timestamp()
        with self._lock:
            changed = False
            for schedule in self._schedules:
                if not schedule.enabled or not schedule.next_run or schedule.is_sql_query_task():
                    continue
                try:
                    due = classify_due(datetime.fromisoformat(schedule.next_run).timestamp(), now_epoch)
                    if due == 'not_due':
                        continue
                    if schedule.id in self._active_schedule_ids:
                        # 같은 일정의 중복 실행 방지: 실행 중이면 이번 시각은 건너뛰고 다음 시각으로 넘긴다.
                        skipped.append((copy.deepcopy(schedule), "이전 실행이 아직 진행 중이라 이번 예약을 건너뜀"))
                    elif due == 'missed' and not schedule.catch_up_missed:
                        skipped.append((copy.deepcopy(schedule), "절전/앱 미실행으로 놓친 실행 (따라잡기 설정 꺼짐)"))
                    else:
                        self._active_schedule_ids.add(schedule.id)
                        trigger = 'catch_up' if due == 'missed' else 'scheduled'
                        jobs.append(_ExecutionJob(copy.deepcopy(schedule), update_next_run=True, trigger=trigger))
                        continue
                    schedule.next_run = self._compute_next_run(schedule.cron_expression, now)
                    changed = True
                except Exception as e:
                    logger.error(f"스케줄 체크 오류 ({schedule.name}): {e}")
            if changed:
                self._save_schedules()
        for schedule, reason in skipped:
            self._record_skip(schedule, reason)
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

    def _find_tunnel_config(self, tunnel_id: str) -> Optional[dict]:
        if not tunnel_id:
            return None
        config = getattr(self.tunnel_engine, 'tunnel_configs', {}).get(tunnel_id)
        if not config:
            stored_tunnels = self.config_manager.load_config().get('tunnels', [])
            config = next((t for t in stored_tunnels if t.get('id') == tunnel_id), None)
        return config

    def _resolve_connection(self, schedule: ScheduleConfig) -> Tuple[Optional["_ResolvedConnection"], str]:
        """백업/SQL 실행이 공유하는 터널 연결 정보 + 복호화된 자격 증명 해석

        Returns:
            (resolved, error_message) - 실패 시 resolved는 None이고 error_message에 사유가 담긴다.
        """
        config = self._find_tunnel_config(schedule.tunnel_id)
        if not config:
            return None, "터널 설정을 찾을 수 없습니다."

        # 터널 연결 확인. 사용자가 이미 연 터널은 그대로 쓰고, 없으면 무인 모드로 연다
        # (처음 보는 SSH 호스트 키/비밀번호가 필요한 개인키는 묻거나 자동 수락하지 않고 실패한다).
        started_here = False
        if not self.tunnel_engine.is_running(schedule.tunnel_id):
            start = getattr(self.tunnel_engine, 'start_tunnel_unattended', None) or self.tunnel_engine.start_tunnel
            # 터널 시작 시도 (설정 딕셔너리 전체를 전달 - 터널 ID 문자열이 아님)
            success, msg = start(config)
            if not success:
                return None, describe_unattended_tunnel_failure(msg)
            started_here = True

        # 연결 정보 가져오기 (host, port) 튜플만 반환됨
        host, port = self.tunnel_engine.get_connection_info(schedule.tunnel_id)
        if host is None or port is None:
            if started_here:
                self._stop_tunnel_quietly(schedule.tunnel_id)
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

        tunnel_id = schedule.tunnel_id
        resolved = _ResolvedConnection(
            host=host or DEFAULT_LOCAL_HOST,
            port=int(port),
            user=user,
            password=password,
            engine=engine,
            release=(lambda: self._stop_tunnel_quietly(tunnel_id)) if started_here else None,
        )
        return resolved, ""

    def list_databases(self, tunnel_id: str) -> Tuple[List[str], str]:
        """PostgreSQL 터널의 접속 가능한 데이터베이스 목록 (UI 선택용). 반환: (목록, 오류 메시지)"""
        resolved, error = self._resolve_connection(ScheduleConfig(id="", name="", tunnel_id=tunnel_id, schema=""))
        if resolved is None:
            return [], error
        connector = None
        try:
            if resolved.engine != "postgresql":
                return [], "PostgreSQL 터널이 아닙니다."
            connector = self._make_connector(resolved.engine, resolved.host, resolved.port,
                                             resolved.user, resolved.password)
            ok, message = connector.connect()
            if not ok:
                return [], message
            return connector.list_databases(), ""
        except Exception as exc:
            return [], str(exc)
        finally:
            if connector is not None:
                connector.disconnect()
            if resolved.release:
                resolved.release()

    def _stop_tunnel_quietly(self, tunnel_id: str) -> None:
        """예약 실행이 직접 연 터널만 닫는다 (사용자가 열어 둔 터널은 건드리지 않는다)."""
        try:
            self.tunnel_engine.stop_tunnel(tunnel_id)
        except Exception:
            logger.warning("예약 실행 터널 정리 실패", exc_info=True)

    # =========================================================================
    # 작업 실행 - BackupTaskExecutor로 위임
    # (아래 얇은 위임 메서드는 tests/test_scheduler.py가 인스턴스에서 직접 호출하는
    #  private 표면이므로 이름/시그니처를 그대로 유지한다)
    # =========================================================================

    def _execute_backup(self, schedule: ScheduleConfig, trigger: str = 'scheduled') -> tuple:
        """백업 실행 (BackupTaskExecutor에 위임)

        Returns:
            (success, message)
        """
        return self._backup_executor.execute(schedule, trigger)

    def get_backup_logs(self, days: int = 7) -> List[Dict[str, Any]]:
        """최근 백업 로그 조회 (ExecutionLogWriter에 위임)

        Args:
            days: 조회할 일수

        Returns:
            로그 항목 목록
        """
        return self._log_writer.get_logs(days)
