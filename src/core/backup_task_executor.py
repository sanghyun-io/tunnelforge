"""
스케줄 백업 작업 실행기
- RustDumpExporter를 통한 DB Export 실행 (무인 실행: 사용자 확인/입력 없음)
- 소유 마커 기록 + 보존 정책 적용 (이 기능이 만든 백업 폴더만 정리)
- 작업 목록(job_history) 기록
"""
import os
import re
import shutil
from datetime import datetime
from typing import Callable, Tuple

from src.core.job_history import (
    KIND_SCHEDULED_BACKUP, STATUS_COMPLETED, STATUS_FAILED, job_begin, job_finish,
)
from src.core.logger import get_logger
from src.core.schedule_config import ScheduleConfig
from src.core import scheduled_backup_store as backup_store

logger = get_logger(__name__)

TRIGGER_LABELS = {
    'scheduled': '예약 실행',
    'catch_up': '놓친 실행 따라잡기',
    'manual': '지금 실행',
}
_UNSAFE_NAME_CHARS = re.compile(r'[^\w.\- ]+', re.UNICODE)


def safe_folder_name(name: str) -> str:
    """스케줄 이름을 폴더 이름 한 조각으로 정리한다 (경로 구분자/제어 문자 제거)."""
    cleaned = _UNSAFE_NAME_CHARS.sub('_', name or '').strip(' ._')
    return cleaned or 'backup'


class BackupTaskExecutor:
    """스케줄 백업 실행 + 보존 정책 적용"""

    def __init__(self, resolve_connection: Callable, log_writer):
        """
        Args:
            resolve_connection: schedule -> (resolved, error_message) 콜백
            log_writer: ExecutionLogWriter 인스턴스 (log_execution 메서드 제공)
        """
        self.resolve_connection = resolve_connection
        self.log_writer = log_writer

    @staticmethod
    def _target_label(schedule: ScheduleConfig) -> str:
        """작업 기록의 대상 표시: PostgreSQL은 database.schema (database 미지정 일정은 postgres)."""
        if schedule.database:
            return f"{schedule.database}.{schedule.schema}"
        return schedule.schema

    def _unique_output_subdir(self, schedule: ScheduleConfig) -> str:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = os.path.join(schedule.output_dir, f"{safe_folder_name(schedule.name)}_{timestamp}")
        candidate, counter = base, 1
        while os.path.exists(candidate):
            counter += 1
            candidate = f"{base}_{counter}"
        os.makedirs(candidate)
        return candidate

    def execute(self, schedule: ScheduleConfig, trigger: str = 'scheduled') -> Tuple[bool, str]:
        """백업 실행

        Returns:
            (success, message)
        """
        # RustDumpExporter/RustDumpConfig는 호출 시점에 조회되어야 테스트의
        # monkeypatch("src.exporters.rust_dump_exporter.RustDumpExporter")가 반영된다.
        from src.exporters.rust_dump_exporter import RustDumpExporter, RustDumpConfig

        logger.info(f"백업 시작: {schedule.name} ({trigger})")
        mode = (f"{TRIGGER_LABELS.get(trigger, trigger)} · "
                f"{'선택 테이블 ' + str(len(schedule.tables)) + '개' if schedule.tables else '전체 스키마'} · 스레드 4")
        job_id = job_begin(KIND_SCHEDULED_BACKUP, profile_id=schedule.tunnel_id, profile_name=schedule.name,
                           target=self._target_label(schedule), mode=mode)
        resolved = None
        output_subdir = ""
        try:
            resolved, error_msg = self.resolve_connection(schedule)
            if error_msg:
                return self._fail(schedule, job_id, error_msg, output_subdir)

            if not schedule.output_dir:
                return self._fail(schedule, job_id, "백업 출력 폴더가 설정되지 않았습니다.", output_subdir)

            # 출력 폴더 생성. 소유 마커는 Export가 끝난 뒤에 쓴다: Rust dump는 비어 있지 않은 폴더를 거부하므로
            # 진행 중에는 마커를 둘 수 없고, 마커가 없는 폴더는 보존 정책이 건드리지 않으므로 진행 중 백업도 보호된다.
            os.makedirs(schedule.output_dir, exist_ok=True)
            output_subdir = self._unique_output_subdir(schedule)

            # RustDump Export 실행
            config = RustDumpConfig(
                host=resolved.host,
                port=resolved.port,
                user=resolved.user,
                password=resolved.password,
                schema=schedule.schema,
                engine=resolved.engine,
                database=schedule.database if resolved.engine == "postgresql" else "",
            )

            exporter = RustDumpExporter(config)

            if schedule.tables:
                success, result, _ = exporter.export_tables(
                    schema=schedule.schema,
                    tables=schedule.tables,
                    output_dir=output_subdir,
                    threads=4
                )
            else:
                success, result = exporter.export_full_schema(
                    schema=schedule.schema,
                    output_dir=output_subdir,
                    threads=4
                )

            if success:
                backup_store.write_marker(output_subdir, schedule.id, schedule.name, backup_store.STATE_COMPLETED,
                                          finished_at=datetime.now(), message='ok')
                removed = self._apply_retention(schedule)

                schedule.last_run = datetime.now().isoformat()

                message = f"백업 완료: {output_subdir}"
                if removed:
                    message += f" (보존 정책으로 오래된 백업 {len(removed)}개 정리)"
                logger.info(message)
                self.log_writer.log_execution(schedule, True, message)
                job_finish(job_id, STATUS_COMPLETED,
                           report_path=os.path.join(output_subdir, '_tunnelforge_dump.json'),
                           details={'retention_removed': len(removed)})
                return True, message
            return self._fail(schedule, job_id, f"Export 실패: {result}", output_subdir)

        except Exception as e:
            logger.exception("백업 오류")
            return self._fail(schedule, job_id, f"백업 오류: {str(e)}", output_subdir)
        finally:
            release = getattr(resolved, 'release', None)
            if callable(release):
                try:
                    release()
                except Exception:
                    logger.warning("백업 연결 정리 실패", exc_info=True)

    def _fail(self, schedule: ScheduleConfig, job_id, error_msg: str, output_subdir: str) -> Tuple[bool, str]:
        logger.error(error_msg)
        if output_subdir and os.path.isdir(output_subdir):
            try:
                backup_store.write_marker(output_subdir, schedule.id, schedule.name, backup_store.STATE_FAILED,
                                          finished_at=datetime.now(), message=error_msg)
                # 아무것도 쓰이지 않은 폴더(사전 거부 등)는 남기지 않는다. 진단할 내용이 없다.
                if os.listdir(output_subdir) == [backup_store.MARKER_NAME]:
                    shutil.rmtree(output_subdir, ignore_errors=True)
            except OSError:
                logger.warning("실패한 백업 마커 갱신 실패", exc_info=True)
        self.log_writer.log_execution(schedule, False, error_msg)
        job_finish(job_id, STATUS_FAILED, error=error_msg)
        return False, error_msg

    def _apply_retention(self, schedule: ScheduleConfig):
        """보존 정책 적용: 소유 마커가 증명된 이 스케줄의 백업 폴더만 대상으로 한다."""
        try:
            return backup_store.apply_retention(
                schedule.output_dir, schedule.id, schedule.retention_count, schedule.retention_days
            )
        except Exception as e:
            logger.error(f"백업 정리 오류: {e}")
            return []
