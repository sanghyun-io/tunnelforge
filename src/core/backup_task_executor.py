"""
스케줄 백업 작업 실행기
- RustDumpExporter를 통한 DB Export 실행
- 오래된 백업 정리 (보관 정책 적용)
"""
import os
import shutil
from datetime import datetime
from typing import Callable, Tuple

from src.core.logger import get_logger
from src.core.retention_policy import select_paths_for_retention
from src.core.schedule_config import ScheduleConfig

logger = get_logger(__name__)


class BackupTaskExecutor:
    """스케줄 백업 실행 + 보관 정책 적용"""

    def __init__(self, resolve_connection: Callable, log_writer):
        """
        Args:
            resolve_connection: schedule -> (resolved, error_message) 콜백
            log_writer: ExecutionLogWriter 인스턴스 (log_execution 메서드 제공)
        """
        self.resolve_connection = resolve_connection
        self.log_writer = log_writer

    def execute(self, schedule: ScheduleConfig) -> Tuple[bool, str]:
        """백업 실행

        Returns:
            (success, message)
        """
        # RustDumpExporter/RustDumpConfig는 호출 시점에 조회되어야 테스트의
        # monkeypatch("src.exporters.rust_dump_exporter.RustDumpExporter")가 반영된다.
        from src.exporters.rust_dump_exporter import RustDumpExporter, RustDumpConfig

        logger.info(f"백업 시작: {schedule.name}")

        try:
            resolved, error_msg = self.resolve_connection(schedule)
            if error_msg:
                logger.error(error_msg)
                self.log_writer.log_execution(schedule, False, error_msg)
                return False, error_msg

            # 출력 디렉토리 생성
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_subdir = os.path.join(schedule.output_dir, f"{schedule.name}_{timestamp}")
            os.makedirs(output_subdir, exist_ok=True)

            # RustDump Export 실행
            config = RustDumpConfig(
                host=resolved.host,
                port=resolved.port,
                user=resolved.user,
                password=resolved.password,
                schema=schedule.schema,
                engine=resolved.engine,
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
                # 백업 정리
                self._cleanup_old_backups(schedule)

                # last_run 업데이트
                schedule.last_run = datetime.now().isoformat()

                message = f"백업 완료: {output_subdir}"
                logger.info(message)
                self.log_writer.log_execution(schedule, True, message)
                return True, message
            else:
                error_msg = f"Export 실패: {result}"
                logger.error(error_msg)
                self.log_writer.log_execution(schedule, False, error_msg)
                return False, error_msg

        except Exception as e:
            error_msg = f"백업 오류: {str(e)}"
            logger.exception(error_msg)
            self.log_writer.log_execution(schedule, False, error_msg)
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

            to_delete = select_paths_for_retention(
                backup_dirs, schedule.retention_days, schedule.retention_count
            )

            # 삭제 실행
            for path in to_delete:
                try:
                    shutil.rmtree(path)
                    logger.info(f"오래된 백업 삭제: {path}")
                except Exception as e:
                    logger.error(f"백업 삭제 실패: {path} - {e}")

        except Exception as e:
            logger.error(f"백업 정리 오류: {e}")
