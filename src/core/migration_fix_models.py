"""
마이그레이션 자동 수정 위저드 - 데이터 모델

수정 옵션/SQL 생성과 문자셋 수정 계획은 Rust core(upgrade.fix_plan / upgrade.charset_sql)가 만든다.
이 모듈은 UI 가 다루는 모델만 정의한다.
"""
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional

from src.core.migration_constants import IssueType


# 마이그레이션 자동 수정의 목표 charset/collation (wizard-domain 공유 상수)
DEFAULT_TARGET_CHARSET = "utf8mb4"
DEFAULT_TARGET_COLLATION = "utf8mb4_unicode_ci"


class FixStrategy(Enum):
    """수정 전략"""
    # 날짜 관련
    DATE_TO_NULL = "date_to_null"                    # NULL로 변경
    DATE_TO_MIN = "date_to_min"                      # 최소값 (1970-01-01)으로 변경
    DATE_TO_CUSTOM = "date_to_custom"                # 사용자 지정 날짜

    # 기타
    SKIP = "skip"                                     # 건너뛰기
    MANUAL = "manual"                                 # 수동 처리


@dataclass
class FixOption:
    """수정 옵션"""
    strategy: FixStrategy
    label: str
    description: str
    sql_template: Optional[str] = None
    requires_input: bool = False                     # 사용자 입력 필요 여부
    input_label: Optional[str] = None                # 입력 필드 라벨
    input_default: Optional[str] = None              # 기본값
    is_recommended: bool = False                     # 권장 옵션 여부
    estimated_rows: Optional[int] = None             # dry-run 예상 영향 행 수 (계획 시 Rust 가 계산)


@dataclass
class FixWizardStep:
    """위저드 단계"""
    issue_index: int                                 # 원본 이슈 인덱스
    issue_type: IssueType
    location: str
    description: str
    options: List[FixOption]
    selected_option: Optional[FixOption] = None
    user_input: Optional[str] = None                 # 사용자 입력값

    def rendered_sql(self) -> str:
        """선택된 옵션과 사용자 입력으로 렌더링된 SQL 반환"""
        if not self.selected_option:
            return ""

        sql = self.selected_option.sql_template or ""
        if self.selected_option.requires_input and self.user_input:
            sql = sql.replace("{custom_date}", self.user_input)
            sql = sql.replace("{precision}", self.user_input)

        return sql


@dataclass
class FixExecutionResult:
    """실행 결과"""
    success: bool
    message: str
    sql_executed: str
    affected_rows: int = 0
    error: Optional[str] = None
    location: str = ""        # step.location을 함께 저장 (정렬 후 매핑 오류 방지)
    description: str = ""     # 스킵/수동처리 사유 (step.description 또는 선택된 옵션 description)


@dataclass
class ExecutionSummary:
    """실행 결과 공통 요약"""
    total: int
    success: int
    fail: int
    skip: int
    affected_rows: int


@dataclass
class BatchExecutionResult:
    """배치 실행 결과"""
    total_steps: int
    success_count: int
    fail_count: int
    skip_count: int
    results: List[FixExecutionResult]
    total_affected_rows: int = 0
    rollback_sql: str = ""  # Rollback SQL

    def summary(self) -> ExecutionSummary:
        """UI가 공통으로 소비하는 실행 결과 요약"""
        return ExecutionSummary(
            total=self.total_steps,
            success=self.success_count,
            fail=self.fail_count,
            skip=self.skip_count,
            affected_rows=self.total_affected_rows,
        )


@dataclass
class CharsetTableInfo:
    """문자셋 수정 대상 테이블 정보

    UI에서 테이블 목록을 표시하고 건너뛰기 선택을 처리하기 위한 정보 클래스.
    """
    table_name: str
    current_charset: str
    current_collation: str
    fk_parents: List[str]       # 이 테이블이 참조하는 부모 테이블
    fk_children: List[str]      # 이 테이블을 참조하는 자식 테이블
    is_original_issue: bool     # 원본 분석 이슈에 있는 테이블인지
    skip: bool = False          # 건너뛰기 여부


@dataclass
class ForeignKeyRef:
    """문자셋 계획 대상에 걸린 FK (선택한 테이블의 FK 수 표시용)"""
    constraint_name: str
    table_name: str
    ref_table: str
