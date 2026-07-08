"""
마이그레이션 자동 수정 위저드 - 데이터 모델 (Enum/dataclass) + 공유 순수 헬퍼
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Tuple, Callable, Any

from src.core.migration_constants import IssueType


class FixStrategy(Enum):
    """수정 전략"""
    # 날짜 관련
    DATE_TO_NULL = "date_to_null"                    # NULL로 변경
    DATE_TO_MIN = "date_to_min"                      # 최소값 (1970-01-01)으로 변경
    DATE_TO_CUSTOM = "date_to_custom"                # 사용자 지정 날짜

    # Collation 관련
    COLLATION_SINGLE = "collation_single"            # 단일 테이블만 변경
    COLLATION_FK_CASCADE = "collation_fk_cascade"    # FK 연관 테이블 일괄 변경
    COLLATION_FK_SAFE = "collation_fk_safe"          # FK 안전 변경 (DROP → 변경 → 재생성)

    # 기타
    SKIP = "skip"                                     # 건너뛰기
    MANUAL = "manual"                                 # 수동 처리


@dataclass
class FKDefinition:
    """FK 정의 (DROP/ADD용)

    복합 FK를 지원하기 위해 columns와 ref_columns를 리스트로 관리합니다.
    """
    constraint_name: str
    table_name: str
    columns: List[str]          # 복합 FK 지원
    ref_table: str
    ref_columns: List[str]
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"

    def get_drop_sql(self, schema: str) -> str:
        """FK DROP SQL 생성"""
        return f"ALTER TABLE `{schema}`.`{self.table_name}` DROP FOREIGN KEY `{self.constraint_name}`;"

    def get_add_sql(self, schema: str) -> str:
        """FK ADD SQL 생성"""
        cols = ", ".join(f"`{c}`" for c in self.columns)
        ref_cols = ", ".join(f"`{c}`" for c in self.ref_columns)
        return (
            f"ALTER TABLE `{schema}`.`{self.table_name}` ADD CONSTRAINT `{self.constraint_name}` "
            f"FOREIGN KEY ({cols}) REFERENCES `{self.ref_table}` ({ref_cols}) "
            f"ON DELETE {self.on_delete} ON UPDATE {self.on_update};"
        )


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
    related_tables: List[str] = field(default_factory=list)  # 관련 테이블 (collation용)
    modify_clause: Optional[str] = None              # column-level MODIFY COLUMN 절 (병합 최적화용)


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

    # FK 연관 테이블 일괄 변경으로 인한 자동 포함 정보
    # (옵션 선택 단계만 생략, 실제 SQL에는 포함됨)
    included_by: Optional[str] = None                # 포함시킨 원본 테이블명 (예: "companies")
    included_reason: str = ""                        # 포함 사유 설명


@dataclass
class FixExecutionResult:
    """실행 결과"""
    success: bool
    message: str
    sql_executed: str
    affected_rows: int = 0
    error: Optional[str] = None
    location: str = ""        # step.location을 함께 저장 (FK 정렬 후 매핑 오류 방지)
    description: str = ""     # 스킵/수동처리 사유 (step.description 또는 선택된 옵션 description)


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


def _format_default_sql_clause(col_info: Dict[str, Any]) -> str:
    """COLUMN_DEFAULT 값 → DEFAULT 절 문자열 생성 (공유 헬퍼)

    INFORMATION_SCHEMA.COLUMNS의 COLUMN_DEFAULT는 문자열/None으로 저장됨.
    타입에 따라 따옴표 여부를 결정하고, MySQL 함수/표현식은 따옴표 없이 출력.
    문자열 기본값은 내부 작은따옴표를 이스케이프하여 잘못된 DDL 생성을 방지한다.
    SmartFixGenerator(생성 SQL)와 RollbackSQLGenerator(롤백 SQL)가 이 헬퍼를 공유한다.
    """
    default_val = col_info.get('COLUMN_DEFAULT')
    col_type = (col_info.get('COLUMN_TYPE') or '').upper()
    nullable = col_info.get('IS_NULLABLE') == 'YES'

    if default_val is None:
        return 'DEFAULT NULL' if nullable else ''

    # MySQL 함수/표현식 → 따옴표 없이
    unquoted_keywords = {
        'CURRENT_TIMESTAMP', 'CURRENT_DATE', 'CURRENT_TIME',
        'NOW', 'NOW()', 'UUID', 'UUID()', 'LOCALTIME', 'LOCALTIMESTAMP',
    }
    stripped = default_val.upper().rstrip('()')
    if stripped in unquoted_keywords:
        return f'DEFAULT {default_val}'

    # 숫자형 → 따옴표 없이
    numeric_prefixes = (
        'INT', 'TINYINT', 'SMALLINT', 'MEDIUMINT', 'BIGINT',
        'DECIMAL', 'FLOAT', 'DOUBLE', 'NUMERIC', 'BIT', 'YEAR', 'BOOL',
    )
    if any(col_type.startswith(t) for t in numeric_prefixes):
        return f'DEFAULT {default_val}'

    # 문자열/기타 → 작은따옴표로 감싸기 (내부 ' 이스케이프)
    escaped = default_val.replace("'", "''")
    return f"DEFAULT '{escaped}'"


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
