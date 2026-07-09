"""
마이그레이션 자동 수정 위저드 Worker

백그라운드에서 수정 작업을 실행하는 QThread 워커.
"""

from PyQt6.QtCore import QThread, pyqtSignal
from typing import List, Optional, Set
from dataclasses import dataclass

from src.core.db_connector import MySQLConnector
from src.core.migration_fix_wizard import (
    FixWizardStep, BatchFixExecutor, BatchExecutionResult, ExecutionSummary,
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

    def summary(self) -> ExecutionSummary:
        """UI가 공통으로 소비하는 실행 결과 요약"""
        return ExecutionSummary(
            total=(
                self.charset_tables_count
                + (self.other_result.total_steps if self.other_result else 0)
            ),
            success=self.total_success_count,
            fail=self.total_fail_count,
            skip=self.other_result.skip_count if self.other_result else 0,
            affected_rows=self.total_affected_rows,
        )

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
        # dry_run은 Rust Core mutation ownership을 강제하는 의도적 방지 가드다.
        if not dry_run:
            raise RuntimeError(
                "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )
        self.connector = connector
        self.schema = schema
        self.steps = steps
        self.charset_tables_to_fix = charset_tables_to_fix or set()
        self._cancel_requested = False

    def request_cancel(self):
        """협조적 취소 요청

        QThread.terminate()는 facade가 잡고 있는 락을 해제하지 못한 채
        스레드를 강제 종료시켜 데드락을 유발할 수 있다. 대신 안전한 체크포인트
        (각 단계 시작 전)에서 스스로 중단하도록 플래그만 세운다.
        """
        self._cancel_requested = True

    def run(self):
        try:
            if self._cancel_requested:
                self.finished.emit(False, "사용자 요청으로 취소되었습니다.", CombinedExecutionResult())
                return

            combined_result = CombinedExecutionResult()
            mode = "[DRY-RUN]"

            # === 1. 문자셋 변경 ===
            if self.charset_tables_to_fix:
                self.progress.emit(f"🔤 {mode} 문자셋 변경 시작...")
                self.progress.emit(f"   대상 테이블: {len(self.charset_tables_to_fix)}개")

                changer = FKSafeCharsetChanger(self.connector, self.schema)

                success, message, result_dict = changer.execute_safe_charset_change(
                    tables=self.charset_tables_to_fix,
                    charset="utf8mb4",
                    collation="utf8mb4_unicode_ci",
                    dry_run=True,
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

            # 단계 사이 취소 체크포인트 (facade 락을 오래 잡지 않는 안전 지점)
            if self._cancel_requested:
                self.progress.emit("🛑 취소 요청으로 나머지 작업을 건너뜁니다.")
                self.finished.emit(False, "사용자 요청으로 취소되었습니다.", combined_result)
                return

            # === 2. 기타 이슈 처리 ===
            if self.steps:
                self.progress.emit(f"📋 {mode} 기타 이슈 수정 시작...")

                executor = BatchFixExecutor(self.connector, self.schema)
                executor.set_progress_callback(lambda msg: self.progress.emit(msg))

                other_result = executor.execute_batch(self.steps, dry_run=True)
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
