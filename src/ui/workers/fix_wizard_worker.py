"""
마이그레이션 자동 수정 위저드 Worker

백그라운드에서 수정 작업을 실행하는 QThread 워커.
"""

from PyQt6.QtCore import QThread, pyqtSignal
from typing import List, Optional, Set
from dataclasses import dataclass

from src.core.db_connector import MySQLConnector
from src.core.migration_fix_wizard import (
    FixWizardStep, BatchFixExecutor, BatchExecutionResult,
    FKSafeCharsetChanger
)


@dataclass
class CombinedExecutionResult:
    """문자셋 + 기타 이슈 통합 실행 결과"""
    charset_success: bool = True
    charset_message: str = ""
    charset_tables_count: int = 0
    charset_fk_count: int = 0
    charset_rollback_sql: str = ""  # 문자셋 변경 실패 시 롤백 SQL

    other_result: Optional[BatchExecutionResult] = None

    @property
    def total_success_count(self) -> int:
        count = 1 if self.charset_success and self.charset_tables_count > 0 else 0
        if self.other_result:
            count += self.other_result.success_count
        return count

    @property
    def total_fail_count(self) -> int:
        count = 0 if self.charset_success else 1
        if self.other_result:
            count += self.other_result.fail_count
        return count

    @property
    def total_affected_rows(self) -> int:
        rows = self.charset_tables_count  # 테이블 수를 영향 단위로 계산
        if self.other_result:
            rows += self.other_result.total_affected_rows
        return rows

    @property
    def rollback_sql(self) -> str:
        """통합 롤백 SQL 반환"""
        parts = []
        # 문자셋 롤백 SQL
        if self.charset_rollback_sql:
            parts.append(self.charset_rollback_sql)
        # 기타 이슈 롤백 SQL
        if self.other_result and self.other_result.rollback_sql:
            parts.append(self.other_result.rollback_sql)
        return "\n\n".join(parts)


class FixWizardWorker(QThread):
    """수정 위저드 워커 스레드

    두 가지 유형의 수정 작업을 처리:
    1. 문자셋 변경 (charset_tables_to_fix) - FK 안전 변경
    2. 기타 이슈 (steps) - BatchFixExecutor로 처리
    """

    progress = pyqtSignal(str)  # 진행 메시지
    finished = pyqtSignal(bool, str, object)  # success, message, CombinedExecutionResult

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        steps: List[FixWizardStep],
        dry_run: bool = True,
        charset_tables_to_fix: Optional[Set[str]] = None
    ):
        super().__init__()
        self.connector = connector
        self.schema = schema
        self.steps = steps
        self.dry_run = dry_run
        self.charset_tables_to_fix = charset_tables_to_fix or set()

    def run(self):
        try:
            combined_result = CombinedExecutionResult()
            mode = "[DRY-RUN]" if self.dry_run else "[실행]"

            # === 1. 문자셋 변경 ===
            if self.charset_tables_to_fix:
                self.progress.emit(f"🔤 {mode} 문자셋 변경 시작...")
                self.progress.emit(f"   대상 테이블: {len(self.charset_tables_to_fix)}개")

                changer = FKSafeCharsetChanger(self.connector, self.schema)

                success, message, result_dict = changer.execute_safe_charset_change(
                    tables=self.charset_tables_to_fix,
                    charset="utf8mb4",
                    collation="utf8mb4_unicode_ci",
                    dry_run=self.dry_run,
                    progress_callback=lambda msg: self.progress.emit(f"   {msg}")
                )

                combined_result.charset_success = success
                combined_result.charset_message = message
                combined_result.charset_tables_count = len(self.charset_tables_to_fix)
                combined_result.charset_fk_count = result_dict.get('fk_count', 0)

                # 에러 발생 시 롤백 SQL 저장
                if not success:
                    recovery_sql = result_dict.get('recovery_sql', [])
                    if recovery_sql:
                        combined_result.charset_rollback_sql = "\n".join(recovery_sql)
                        self.progress.emit(f"   📋 롤백 SQL 생성됨 ({len(recovery_sql)}줄)")

                if success:
                    self.progress.emit(f"   ✅ 문자셋 변경 완료")
                else:
                    self.progress.emit(f"   ❌ 문자셋 변경 실패: {message}")

                self.progress.emit("")

            # === 2. 기타 이슈 처리 ===
            if self.steps:
                self.progress.emit(f"📋 {mode} 기타 이슈 수정 시작...")

                executor = BatchFixExecutor(self.connector, self.schema)
                executor.set_progress_callback(lambda msg: self.progress.emit(msg))

                other_result = executor.execute_batch(self.steps, dry_run=self.dry_run)
                combined_result.other_result = other_result

            # === 결과 요약 ===
            total_success = combined_result.total_success_count
            total_fail = combined_result.total_fail_count

            message = f"{mode} 완료: 성공 {total_success}, 실패 {total_fail}"

            overall_success = combined_result.charset_success and (
                combined_result.other_result is None or
                combined_result.other_result.fail_count == 0
            )

            self.finished.emit(overall_success, message, combined_result)

        except Exception as e:
            self.finished.emit(False, f"오류: {str(e)}", None)
