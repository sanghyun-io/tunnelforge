"""
마이그레이션 자동 수정 위저드 Core 로직

MySQL 8.0 → 8.4 마이그레이션 시 검출된 호환성 이슈를 자동 수정하는 핵심 로직.
- SmartFixGenerator: 컨텍스트 인식 Fix 옵션 생성
- CollationFKGraphBuilder: FK 관계 그래프 분석 (collation 일괄 변경용)
- BatchFixExecutor: 트랜잭션 기반 일괄 실행
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Tuple, Callable, Any
from collections import deque

from src.core.db_connector import MySQLConnector
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


class SmartFixGenerator:
    """컨텍스트 인식 Fix 옵션 생성기

    호환성 이슈에 대해 적절한 수정 옵션을 생성합니다.
    - 날짜 이슈: nullable 여부 확인 후 옵션 제시
    - Collation 이슈: FK 연관 테이블 포함 옵션 제시
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        self._column_nullable_cache: Dict[str, bool] = {}
        self._fk_graph_builder: Optional['CollationFKGraphBuilder'] = None

    def get_fk_graph_builder(self) -> 'CollationFKGraphBuilder':
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = CollationFKGraphBuilder(self.connector, self.schema)
            self._fk_graph_builder.build_graph()
        return self._fk_graph_builder

    def get_fix_options(self, issue: Any) -> List[FixOption]:
        """이슈에 대한 수정 옵션 생성

        Args:
            issue: CompatibilityIssue 객체

        Returns:
            사용 가능한 FixOption 목록
        """
        handlers = {
            IssueType.INVALID_DATE: self._get_invalid_date_options,
            IssueType.CHARSET_ISSUE: self._get_charset_options,
            IssueType.ZEROFILL_USAGE: self._get_zerofill_options,
            IssueType.FLOAT_PRECISION: self._get_float_precision_options,
            IssueType.INT_DISPLAY_WIDTH: self._get_int_display_width_options,
            IssueType.ENUM_EMPTY_VALUE: self._get_enum_empty_options,
            IssueType.DEPRECATED_ENGINE: self._get_deprecated_engine_options,
        }

        handler = handlers.get(issue.issue_type)
        if handler:
            options = handler(issue)
        else:
            # 기본 옵션 (수동 처리 또는 건너뛰기)
            options = self._get_default_options(issue)

        # 항상 "건너뛰기" 옵션 추가
        options.append(FixOption(
            strategy=FixStrategy.SKIP,
            label="건너뛰기",
            description="이 이슈는 수정하지 않고 넘어갑니다."
        ))

        return options

    def _is_column_nullable(self, table: str, column: str) -> bool:
        """컬럼의 nullable 여부 확인"""
        cache_key = f"{self.schema}.{table}.{column}"
        if cache_key in self._column_nullable_cache:
            return self._column_nullable_cache[cache_key]

        query = """
        SELECT IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table, column))

        is_nullable = result[0]['IS_NULLABLE'] == 'YES' if result else False
        self._column_nullable_cache[cache_key] = is_nullable
        return is_nullable

    def _get_column_definition(
        self,
        schema: str,
        table: str,
        column: str,
        charset: Optional[str] = None,
        collation: Optional[str] = None
    ) -> Optional[str]:
        """컬럼의 전체 정의 조회 (MODIFY COLUMN용)

        Args:
            charset:   삽입할 CHARACTER SET 값 (예: 'utf8mb4'). None이면 생략.
            collation: 삽입할 COLLATE 값 (예: 'utf8mb4_unicode_ci'). None이면 생략.

        Returns:
            컬럼 정의 문자열. charset 지정 시 올바른 MySQL 순서로 조립:
            "COLUMN_TYPE [CHARACTER SET ...] [COLLATE ...] [NOT NULL] [DEFAULT ...] [EXTRA]"
            조회 실패 시 None

        Note:
            MySQL에서 CHARACTER SET / COLLATE 절은 데이터 타입의 일부이므로
            반드시 NOT NULL / DEFAULT 앞에 위치해야 합니다.
            (NOT NULL 뒤에 CHARACTER SET을 두면 1064 문법 오류 발생)
        """
        query = """
        SELECT
            COLUMN_TYPE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """
        result = self.connector.execute(query, (schema, table, column))

        if not result:
            return None

        col = result[0]
        parts = [col['COLUMN_TYPE']]  # VARCHAR(255), TEXT, etc.

        # CHARACTER SET / COLLATE는 NOT NULL 앞에 삽입 (MySQL 문법 요구사항)
        if charset:
            parts.append(f"CHARACTER SET {charset}")
        if collation:
            parts.append(f"COLLATE {collation}")

        # NOT NULL / NULL
        if col['IS_NULLABLE'] == 'NO':
            parts.append('NOT NULL')

        # DEFAULT (공유 헬퍼로 escape 처리 — quoted/expression default 모두 안전하게 생성)
        if col['COLUMN_DEFAULT'] is not None:
            default_clause = _format_default_sql_clause(col)
            if default_clause:
                parts.append(default_clause)

        # EXTRA (AUTO_INCREMENT, ON UPDATE CURRENT_TIMESTAMP 등)
        if col['EXTRA']:
            parts.append(col['EXTRA'])

        return ' '.join(parts)

    def _get_invalid_date_options(self, issue: Any) -> List[FixOption]:
        """0000-00-00 날짜 수정 옵션"""
        options = []
        table = issue.table_name
        column = issue.column_name

        if not table or not column:
            return self._get_default_options(issue)

        # nullable 여부 확인
        is_nullable = self._is_column_nullable(table, column)

        # 1. NULL로 변경 (nullable 컬럼만)
        if is_nullable:
            options.append(FixOption(
                strategy=FixStrategy.DATE_TO_NULL,
                label="NULL로 변경 (권장)",
                description=f"0000-00-00 값을 NULL로 변경합니다.",
                sql_template=f"""UPDATE `{self.schema}`.`{table}`
SET `{column}` = NULL
WHERE `{column}` = '0000-00-00'
   OR `{column}` = '0000-00-00 00:00:00'
   OR (MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0);""",
                is_recommended=True
            ))

        # 2. 최소값으로 변경
        options.append(FixOption(
            strategy=FixStrategy.DATE_TO_MIN,
            label="1970-01-01로 변경",
            description="0000-00-00 값을 Unix 시작일(1970-01-01)로 변경합니다.",
            sql_template=f"""UPDATE `{self.schema}`.`{table}`
SET `{column}` = '1970-01-01'
WHERE `{column}` = '0000-00-00'
   OR `{column}` = '0000-00-00 00:00:00'
   OR (MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0);""",
            is_recommended=not is_nullable  # nullable 아니면 이게 권장
        ))

        # 3. 사용자 지정 날짜
        options.append(FixOption(
            strategy=FixStrategy.DATE_TO_CUSTOM,
            label="사용자 지정 날짜",
            description="원하는 날짜로 직접 지정합니다.",
            sql_template=f"""UPDATE `{self.schema}`.`{table}`
SET `{column}` = '{{custom_date}}'
WHERE `{column}` = '0000-00-00'
   OR `{column}` = '0000-00-00 00:00:00'
   OR (MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0);""",
            requires_input=True,
            input_label="변경할 날짜 (YYYY-MM-DD)",
            input_default="2000-01-01"
        ))

        return options

    def _get_charset_options(self, issue: Any) -> List[FixOption]:
        """Collation/Charset 수정 옵션"""
        options = []
        location_parts = issue.location.split('.')

        if len(location_parts) < 2:
            return self._get_default_options(issue)

        schema = location_parts[0]
        table = location_parts[1]
        column = location_parts[2] if len(location_parts) > 2 else None

        if column:
            # 컬럼 레벨 charset 변경 - CHARACTER SET을 NOT NULL 앞에 삽입하여 조회
            col_def = self._get_column_definition(
                schema, table, column,
                charset='utf8mb4',
                collation='utf8mb4_unicode_ci'
            )

            if col_def:
                # 컬럼 정의를 성공적으로 조회한 경우
                # col_def에 이미 CHARACTER SET / COLLATE가 올바른 위치(NOT NULL 앞)에 포함됨
                modify_clause = f"`{column}` {col_def}"
                options.append(FixOption(
                    strategy=FixStrategy.COLLATION_SINGLE,
                    label="이 컬럼만 변경",
                    description=f"{table}.{column} 컬럼의 charset을 utf8mb4로 변경합니다.",
                    sql_template=f"ALTER TABLE `{schema}`.`{table}` MODIFY COLUMN `{column}` {col_def};",
                    modify_clause=modify_clause,  # 병합 최적화: regex 파싱 불필요
                ))
            else:
                # 컬럼 정의 조회 실패 - 수동 처리로 안내
                options.append(FixOption(
                    strategy=FixStrategy.MANUAL,
                    label="수동 처리 필요",
                    description=f"{table}.{column} 컬럼 정보를 조회할 수 없습니다. 수동으로 확인 후 변경하세요.",
                    sql_template=f"-- {table}.{column} 컬럼 타입 확인 후 수동 변경 필요\n"
                                 f"-- SHOW CREATE TABLE `{schema}`.`{table}`;",
                ))
        else:
            # 테이블 레벨 charset 변경

            # 1. 단일 테이블만 변경
            options.append(FixOption(
                strategy=FixStrategy.COLLATION_SINGLE,
                label="이 테이블만 변경",
                description=f"{table} 테이블만 utf8mb4로 변경합니다.",
                sql_template=f"ALTER TABLE `{schema}`.`{table}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            ))

            # 2. FK 연관 테이블 일괄 변경
            fk_builder = self.get_fk_graph_builder()
            related_tables = fk_builder.get_related_tables(table)

            if related_tables:
                # 위상 정렬 순서로 SQL 생성
                ordered_tables = fk_builder.get_topological_order(related_tables | {table})

                sql_lines = ["SET FOREIGN_KEY_CHECKS = 0;"]
                for t in ordered_tables:
                    sql_lines.append(
                        f"ALTER TABLE `{schema}`.`{t}` "
                        f"CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
                    )
                sql_lines.append("SET FOREIGN_KEY_CHECKS = 1;")

                options.append(FixOption(
                    strategy=FixStrategy.COLLATION_FK_CASCADE,
                    label=f"FK 연관 테이블 일괄 변경 ({len(ordered_tables)}개)",
                    description=(
                        f"FK로 연결된 테이블을 모두 utf8mb4로 변경합니다.\n"
                        f"대상 테이블: {', '.join(ordered_tables)}"
                    ),
                    sql_template="\n".join(sql_lines),
                    related_tables=list(ordered_tables),
                    is_recommended=False  # FK 안전 변경이 권장
                ))

                # 3. FK 안전 변경 (권장 - Error 3780 방지)
                # FK를 임시 DROP → charset 변경 → FK 재생성
                fk_safe_changer = FKSafeCharsetChanger(self.connector, schema)
                safe_sql_parts = fk_safe_changer.generate_safe_charset_sql(
                    related_tables | {table},
                    charset="utf8mb4",
                    collation="utf8mb4_unicode_ci"
                )

                options.append(FixOption(
                    strategy=FixStrategy.COLLATION_FK_SAFE,
                    label=f"FK 안전 변경 ({len(ordered_tables)}개 테이블, {safe_sql_parts['fk_count']}개 FK)",
                    description=(
                        f"⚠️ Error 3780 방지: FK를 임시 DROP 후 charset 변경, FK 재생성합니다.\n"
                        f"대상 테이블: {', '.join(ordered_tables)}\n"
                        f"영향받는 FK: {safe_sql_parts['fk_count']}개"
                    ),
                    sql_template="\n".join(safe_sql_parts['full_sql']),
                    related_tables=list(ordered_tables),
                    is_recommended=True
                ))

        return options

    def _get_zerofill_options(self, issue: Any) -> List[FixOption]:
        """ZEROFILL 속성 제거 옵션"""
        return [
            FixOption(
                strategy=FixStrategy.MANUAL,
                label="수동 처리",
                description=(
                    "ZEROFILL은 deprecated됩니다. "
                    "애플리케이션에서 LPAD() 함수로 포맷팅 처리를 권장합니다.\n"
                    "예: SELECT LPAD(column, 5, '0') FROM table;"
                ),
                sql_template="-- ZEROFILL 제거 후 LPAD() 함수로 애플리케이션에서 포맷팅 처리"
            )
        ]

    def _get_float_precision_options(self, issue: Any) -> List[FixOption]:
        """FLOAT(M,D) 구문 수정 옵션"""
        table = issue.table_name
        column = issue.column_name

        if not table or not column:
            return self._get_default_options(issue)

        return [
            FixOption(
                strategy=FixStrategy.MANUAL,
                label="FLOAT로 변경",
                description="정밀도 구문을 제거하고 FLOAT 타입으로 변경합니다.",
                sql_template=f"ALTER TABLE `{self.schema}`.`{table}` MODIFY COLUMN `{column}` FLOAT;",
                is_recommended=True
            ),
            FixOption(
                strategy=FixStrategy.MANUAL,
                label="DECIMAL로 변경",
                description="정확한 소수점 연산이 필요하면 DECIMAL을 사용합니다.",
                sql_template=f"ALTER TABLE `{self.schema}`.`{table}` MODIFY COLUMN `{column}` DECIMAL({{precision}});",
                requires_input=True,
                input_label="DECIMAL 정밀도 (M,D)",
                input_default="10,2"
            )
        ]

    def _get_int_display_width_options(self, issue: Any) -> List[FixOption]:
        """INT 표시 너비 수정 옵션"""
        return [
            FixOption(
                strategy=FixStrategy.SKIP,
                label="무시 (권장)",
                description=(
                    "INT 표시 너비는 MySQL 8.4에서 자동으로 무시됩니다.\n"
                    "별도 수정 없이 사용해도 영향이 없습니다."
                ),
                is_recommended=True
            )
        ]

    def _get_enum_empty_options(self, issue: Any) -> List[FixOption]:
        """ENUM 빈 문자열 수정 옵션"""
        return [
            FixOption(
                strategy=FixStrategy.MANUAL,
                label="수동 처리",
                description=(
                    "ENUM 정의에서 빈 문자열('')을 제거해야 합니다.\n"
                    "먼저 데이터를 정리한 후 ENUM 정의를 변경하세요."
                ),
                sql_template="-- ENUM 정의에서 빈 문자열('') 제거 및 데이터 정제 필요"
            )
        ]

    def _get_deprecated_engine_options(self, issue: Any) -> List[FixOption]:
        """deprecated 스토리지 엔진 수정 옵션"""
        table = issue.table_name
        if not table:
            parts = issue.location.split('.')
            table = parts[1] if len(parts) > 1 else None

        if not table:
            return self._get_default_options(issue)

        return [
            FixOption(
                strategy=FixStrategy.MANUAL,
                label="InnoDB로 변경",
                description="테이블 엔진을 InnoDB로 변경합니다.",
                sql_template=f"ALTER TABLE `{self.schema}`.`{table}` ENGINE=InnoDB;",
                is_recommended=True
            )
        ]

    def _get_default_options(self, issue: Any) -> List[FixOption]:
        """기본 옵션 (수동 처리)"""
        return [
            FixOption(
                strategy=FixStrategy.MANUAL,
                label="수동 처리",
                description="이 이슈는 자동 수정이 지원되지 않습니다. 수동으로 처리하세요.",
                sql_template=f"-- 수동 처리 필요: {issue.description}"
            )
        ]

    def generate_sql(self, step: FixWizardStep) -> str:
        """선택된 옵션으로 SQL 생성"""
        if not step.selected_option:
            return ""

        sql = step.selected_option.sql_template or ""

        # 사용자 입력값 대체
        if step.selected_option.requires_input and step.user_input:
            sql = sql.replace("{custom_date}", step.user_input)
            sql = sql.replace("{precision}", step.user_input)

        return sql


class CollationFKGraphBuilder:
    """FK 관계 그래프 분석기

    Collation 변경 시 FK로 연결된 테이블을 함께 변경해야 합니다.
    이 클래스는 FK 관계를 분석하여:
    1. 연관된 테이블 목록 탐색 (BFS)
    2. 변경 순서 결정 (위상 정렬)
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        # 양방향 그래프: table -> set of related tables
        self.graph: Dict[str, Set[str]] = {}
        # 방향 그래프: child -> parent (위상 정렬용)
        self.parent_graph: Dict[str, Set[str]] = {}

    def build_graph(self):
        """FK 관계 그래프 구성

        Note: VIEW는 FK 관계 대상에서 제외 (BASE TABLE만 포함)
        """
        query = """
        SELECT
            kcu.TABLE_NAME as CHILD_TABLE,
            kcu.REFERENCED_TABLE_NAME as PARENT_TABLE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.TABLES t_child
            ON kcu.TABLE_NAME = t_child.TABLE_NAME
            AND kcu.TABLE_SCHEMA = t_child.TABLE_SCHEMA
        JOIN INFORMATION_SCHEMA.TABLES t_parent
            ON kcu.REFERENCED_TABLE_NAME = t_parent.TABLE_NAME
            AND kcu.TABLE_SCHEMA = t_parent.TABLE_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s
            AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            AND t_child.TABLE_TYPE = 'BASE TABLE'
            AND t_parent.TABLE_TYPE = 'BASE TABLE'
        """
        rows = self.connector.execute(query, (self.schema,))

        for row in rows:
            child = row['CHILD_TABLE']
            parent = row['PARENT_TABLE']

            # 양방향 그래프
            if child not in self.graph:
                self.graph[child] = set()
            if parent not in self.graph:
                self.graph[parent] = set()

            self.graph[child].add(parent)
            self.graph[parent].add(child)

            # 방향 그래프 (자식 → 부모)
            if child not in self.parent_graph:
                self.parent_graph[child] = set()
            self.parent_graph[child].add(parent)

    def get_related_tables(self, start_table: str) -> Set[str]:
        """BFS로 연관 테이블 탐색

        Args:
            start_table: 시작 테이블

        Returns:
            연관된 모든 테이블 집합 (시작 테이블 제외)
        """
        if start_table not in self.graph:
            return set()

        visited = {start_table}
        queue = deque([start_table])
        related = set()

        while queue:
            current = queue.popleft()
            for neighbor in self.graph.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    related.add(neighbor)
                    queue.append(neighbor)

        return related

    def get_topological_order(self, tables: Set[str]) -> List[str]:
        """위상 정렬 (Kahn's algorithm)

        FK 관계에서 부모 테이블을 먼저 변경해야 합니다.

        Args:
            tables: 정렬할 테이블 집합

        Returns:
            위상 정렬된 테이블 목록 (부모 먼저)
        """
        # 부분 그래프의 진입 차수 계산
        in_degree: Dict[str, int] = {t: 0 for t in tables}

        for child in tables:
            parents = self.parent_graph.get(child, set())
            for parent in parents:
                if parent in tables:
                    in_degree[child] += 1

        # 진입 차수가 0인 노드(루트 테이블)부터 시작
        queue = deque([t for t in tables if in_degree[t] == 0])
        result = []

        while queue:
            current = queue.popleft()
            result.append(current)

            # 현재 노드를 부모로 가진 자식들의 진입 차수 감소
            for child in tables:
                if current in self.parent_graph.get(child, set()):
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

        # 순환 참조가 있으면 남은 테이블 추가
        remaining = [t for t in tables if t not in result]
        result.extend(remaining)

        return result

    def get_children(self, table: str) -> Set[str]:
        """table을 참조하는 자식 테이블 목록

        Args:
            table: 부모 테이블명

        Returns:
            자식 테이블 집합 (이 테이블을 FK로 참조하는 테이블들)
        """
        children = set()
        for child, parents in self.parent_graph.items():
            if table in parents:
                children.add(child)
        return children

    def get_parents(self, table: str) -> Set[str]:
        """table이 참조하는 부모 테이블 목록

        Args:
            table: 자식 테이블명

        Returns:
            부모 테이블 집합 (이 테이블이 FK로 참조하는 테이블들)
        """
        return self.parent_graph.get(table, set()).copy()

    def get_cascade_skip_tables(self, table_to_skip: str, target_tables: Set[str]) -> Set[str]:
        """특정 테이블 건너뛰기 시 연쇄적으로 건너뛰어야 하는 테이블 목록

        규칙:
        1. table_to_skip을 참조하는 자식 테이블 → 반드시 건너뛰기
           (부모 charset이 변경되지 않으면 자식도 변경 불가)
        2. table_to_skip이 참조하는 부모 (target_tables에 있으면) → 건너뛰기
           (자식이 변경되지 않으면 부모만 변경해도 FK 불일치 발생)
        3. 위 테이블들에 대해 재귀적으로 BFS 수행

        Args:
            table_to_skip: 건너뛰기할 테이블
            target_tables: 변경 대상 테이블 집합

        Returns:
            연쇄적으로 건너뛰어야 하는 테이블 집합 (table_to_skip 제외)
        """
        cascade_skip = set()
        visited = {table_to_skip}
        queue = deque([table_to_skip])

        while queue:
            current = queue.popleft()

            # 1. 자식 테이블 (current를 참조하는 테이블)
            children = self.get_children(current)
            for child in children:
                if child in target_tables and child not in visited:
                    visited.add(child)
                    cascade_skip.add(child)
                    queue.append(child)

            # 2. 부모 테이블 (current가 참조하는 테이블)
            # 자식이 건너뛰면 부모도 건너뛰어야 함 (FK 일관성)
            parents = self.get_parents(current)
            for parent in parents:
                if parent in target_tables and parent not in visited:
                    visited.add(parent)
                    cascade_skip.add(parent)
                    queue.append(parent)

        return cascade_skip


class FKSafeCharsetChanger:
    """FK 안전 Charset 변경기

    Error 3780 방지를 위해 FK를 임시 DROP 후 charset 변경, 다시 FK 재생성합니다.

    문제: SET FOREIGN_KEY_CHECKS = 0은 데이터 삽입 시 FK 검증만 비활성화.
    기존 FK 제약조건의 컬럼 타입 호환성 검사는 여전히 동작함.

    해결:
    1. FK 임시 DROP (영향받는 모든 FK)
    2. CONVERT TO CHARACTER SET (위상 정렬: 부모 먼저)
    3. FK 재생성 (원래 정의대로)
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

    def _get_fk_graph_builder(self) -> CollationFKGraphBuilder:
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = CollationFKGraphBuilder(self.connector, self.schema)
            self._fk_graph_builder.build_graph()
        return self._fk_graph_builder

    def get_related_fks(self, tables: Set[str]) -> List[FKDefinition]:
        """대상 테이블과 연관된 모든 FK 정의 조회

        Args:
            tables: 대상 테이블 집합

        Returns:
            FKDefinition 목록 (복합 FK는 ORDINAL_POSITION으로 그룹화)

        Note: VIEW는 FK 관계 대상에서 제외 (BASE TABLE만 포함)
        """
        if not tables:
            return []

        # 테이블 목록을 IN 절에서 사용
        placeholders = ", ".join(["%s"] * len(tables))

        query = f"""
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.TABLE_NAME,
            kcu.COLUMN_NAME,
            kcu.REFERENCED_TABLE_NAME,
            kcu.REFERENCED_COLUMN_NAME,
            kcu.ORDINAL_POSITION,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        JOIN INFORMATION_SCHEMA.TABLES t_child
            ON kcu.TABLE_NAME = t_child.TABLE_NAME
            AND kcu.TABLE_SCHEMA = t_child.TABLE_SCHEMA
        JOIN INFORMATION_SCHEMA.TABLES t_parent
            ON kcu.REFERENCED_TABLE_NAME = t_parent.TABLE_NAME
            AND kcu.TABLE_SCHEMA = t_parent.TABLE_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s
            AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            AND t_child.TABLE_TYPE = 'BASE TABLE'
            AND t_parent.TABLE_TYPE = 'BASE TABLE'
            AND (kcu.TABLE_NAME IN ({placeholders}) OR kcu.REFERENCED_TABLE_NAME IN ({placeholders}))
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """

        params = [self.schema] + list(tables) + list(tables)
        rows = self.connector.execute(query, tuple(params))

        # 복합 FK 그룹화
        fk_map: Dict[str, FKDefinition] = {}

        for row in rows:
            constraint_name = row['CONSTRAINT_NAME']

            if constraint_name not in fk_map:
                fk_map[constraint_name] = FKDefinition(
                    constraint_name=constraint_name,
                    table_name=row['TABLE_NAME'],
                    columns=[],
                    ref_table=row['REFERENCED_TABLE_NAME'],
                    ref_columns=[],
                    on_delete=row['DELETE_RULE'] or 'RESTRICT',
                    on_update=row['UPDATE_RULE'] or 'RESTRICT'
                )

            fk_map[constraint_name].columns.append(row['COLUMN_NAME'])
            fk_map[constraint_name].ref_columns.append(row['REFERENCED_COLUMN_NAME'])

        return list(fk_map.values())

    def generate_safe_charset_sql(
        self,
        tables: Set[str],
        charset: str = "utf8mb4",
        collation: str = "utf8mb4_unicode_ci"
    ) -> Dict[str, List[str]]:
        """FK 안전 Charset 변경 SQL 생성

        Args:
            tables: 변경할 테이블 집합
            charset: 목표 charset
            collation: 목표 collation

        Returns:
            Dict with keys: 'drop_fks', 'alter_tables', 'add_fks', 'full_sql'
        """
        # 1. 연관 FK 조회
        fks = self.get_related_fks(tables)

        # 2. 위상 정렬 (부모 먼저)
        fk_builder = self._get_fk_graph_builder()
        ordered_tables = fk_builder.get_topological_order(tables)

        # 3. SQL 생성
        drop_fks = []
        add_fks = []

        for fk in fks:
            drop_fks.append(fk.get_drop_sql(self.schema))
            add_fks.append(fk.get_add_sql(self.schema))

        alter_tables = []
        for table in ordered_tables:
            alter_tables.append(
                f"ALTER TABLE `{self.schema}`.`{table}` "
                f"CONVERT TO CHARACTER SET {charset} COLLATE {collation};"
            )

        # 4. 전체 SQL 조합
        full_sql = []
        full_sql.append("-- ===== Phase 1: FK 임시 DROP =====")
        if drop_fks:
            full_sql.extend(drop_fks)
        else:
            full_sql.append("-- (연관 FK 없음)")

        full_sql.append("")
        full_sql.append("-- ===== Phase 2: Charset 변경 (부모 먼저) =====")
        full_sql.extend(alter_tables)

        full_sql.append("")
        full_sql.append("-- ===== Phase 3: FK 재생성 =====")
        if add_fks:
            full_sql.extend(add_fks)
        else:
            full_sql.append("-- (재생성할 FK 없음)")

        return {
            'drop_fks': drop_fks,
            'alter_tables': alter_tables,
            'add_fks': add_fks,
            'full_sql': full_sql,
            'fk_count': len(fks),
            'table_count': len(ordered_tables)
        }

    def execute_safe_charset_change(
        self,
        tables: Set[str],
        charset: str = "utf8mb4",
        collation: str = "utf8mb4_unicode_ci",
        dry_run: bool = True,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str, Dict]:
        """FK 안전 Charset 변경 실행

        Args:
            tables: 변경할 테이블 집합
            charset: 목표 charset
            collation: 목표 collation
            dry_run: True면 SQL만 생성
            progress_callback: 진행 로그 콜백

        Returns:
            (success, message, result_dict)

        dry-run-only 계약: dry_run=False는 즉시 거부되므로 이 메서드는 항상
        SQL 미리보기만 생성한다. 실제 DDL 실행 및 그에 딸린 recovery-SQL
        스택 관리는 Rust Core가 담당하며, 이 클래스에는 존재하지 않는다.
        """
        if not dry_run:
            raise RuntimeError(
                "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        def log(msg: str):
            if progress_callback:
                progress_callback(msg)

        sql_parts = self.generate_safe_charset_sql(tables, charset, collation)

        log(f"📋 [DRY-RUN] FK 안전 Charset 변경 SQL 생성 완료")
        log(f"   - 영향받는 FK: {sql_parts['fk_count']}개")
        log(f"   - 변경할 테이블: {sql_parts['table_count']}개")
        return True, "DRY-RUN 완료", sql_parts


class BatchFixExecutor:
    """배치 수정 실행기

    트랜잭션 기반으로 수정 SQL을 일괄 실행합니다.
    Dry-run 모드 지원.

    개선사항:
    - 문자셋 변경 시 FOREIGN_KEY_CHECKS=0으로 전체 감싸기
    - FK 관계에 따른 실행 순서 최적화 (위상 정렬)
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        self._progress_callback: Optional[Callable[[str], None]] = None
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 로그"""
        if self._progress_callback:
            self._progress_callback(message)

    def _get_fk_graph_builder(self) -> CollationFKGraphBuilder:
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = CollationFKGraphBuilder(self.connector, self.schema)
            self._fk_graph_builder.build_graph()
        return self._fk_graph_builder

    def _has_charset_issues(self, steps: List[FixWizardStep]) -> bool:
        """문자셋 이슈가 포함되어 있는지 확인 (FK_CHECKS 비활성화 필요 여부)

        참고: COLLATION_FK_SAFE 전략은 자체적으로 FK를 관리하므로 제외
        """
        return any(
            step.issue_type == IssueType.CHARSET_ISSUE
            and step.selected_option
            and step.selected_option.strategy not in (
                FixStrategy.SKIP,
                FixStrategy.COLLATION_FK_SAFE  # FK 안전 변경은 자체 FK 관리
            )
            for step in steps
        )

    def _sort_steps_by_fk_order(self, steps: List[FixWizardStep]) -> List[FixWizardStep]:
        """FK 관계에 따라 실행 순서 정렬 (부모 테이블 먼저)

        위상 정렬을 사용하여 FK 참조 순서에 맞게 정렬합니다.
        부모 테이블이 먼저 변경되어야 자식 테이블 변경 시 FK 충돌이 줄어듭니다.
        """
        # 문자셋 이슈만 정렬 대상
        charset_steps = [s for s in steps if s.issue_type == IssueType.CHARSET_ISSUE]
        other_steps = [s for s in steps if s.issue_type != IssueType.CHARSET_ISSUE]

        if not charset_steps:
            return steps

        try:
            fk_builder = self._get_fk_graph_builder()

            # 테이블명 추출 (location 형식: "schema.table" 또는 "schema.table.column")
            # 컬럼 레벨 스텝(schema.table.column)의 경우 split('.')[-1]이 column명이므로
            # parts[1]을 사용해야 올바른 table명을 얻을 수 있음
            table_to_steps: Dict[str, List[FixWizardStep]] = {}
            for step in charset_steps:
                parts = step.location.split('.')
                table_name = parts[1] if len(parts) >= 2 else parts[0]
                if table_name not in table_to_steps:
                    table_to_steps[table_name] = []
                table_to_steps[table_name].append(step)

            # 위상 정렬
            all_tables = set(table_to_steps.keys())
            sorted_tables = fk_builder.get_topological_order(all_tables)

            # 정렬된 순서로 steps 재배치 (같은 테이블의 여러 스텝 모두 포함)
            sorted_charset_steps = []
            for table in sorted_tables:
                if table in table_to_steps:
                    sorted_charset_steps.extend(table_to_steps[table])

            # 정렬되지 않은 테이블 추가 (FK 관계 없는 테이블)
            sorted_set = set(sorted_tables)
            for step in charset_steps:
                parts = step.location.split('.')
                table_name = parts[1] if len(parts) >= 2 else parts[0]
                if table_name not in sorted_set:
                    sorted_charset_steps.append(step)

            self._log(f"  📊 FK 관계에 따라 {len(sorted_charset_steps)}개 스텝 정렬 완료")

            return sorted_charset_steps + other_steps

        except Exception as e:
            self._log(f"  ⚠️ FK 정렬 실패, 원본 순서 유지: {e}")
            return steps

    def execute_batch(
        self,
        steps: List[FixWizardStep],
        dry_run: bool = True
    ) -> BatchExecutionResult:
        """배치 실행

        Args:
            steps: 실행할 위저드 단계 목록
            dry_run: True면 실제 실행하지 않고 영향 행 추정

        Returns:
            BatchExecutionResult

        dry-run-only 계약: dry_run=False는 즉시 거부되므로 이 메서드는 실제
        DDL/DML을 실행하지 않는다. 세션 상태(sql_mode 등) 변경·복원, 실행 전
        상태 캡처, rollback SQL 자동 생성은 실제 mutation이 있을 때만 의미가
        있던 기능이며 Rust Core가 담당하므로 이 클래스에는 존재하지 않는다.

        개선사항:
        - 문자셋 이슈 포함 시 FK 관계에 따른 실행 순서 최적화
        """
        if not dry_run:
            raise RuntimeError(
                "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        results: List[FixExecutionResult] = []
        success_count = 0
        fail_count = 0
        skip_count = 0
        total_affected = 0
        rollback_sql = ""

        mode = "[DRY-RUN]" if dry_run else "[실행]"
        self._log(f"🔧 {mode} 배치 수정 시작 ({len(steps)}개)")

        # FK 관계에 따른 실행 순서 정렬
        has_charset = self._has_charset_issues(steps)
        if has_charset:
            steps = self._sort_steps_by_fk_order(steps)

        # === COLLATION_FK_SAFE 배치 최적화 ===
        # 개별 스텝마다 FK DROP→ALTER→ADD를 반복하면 O(N²) DDL 발생.
        # FK 클러스터별(related_tables 집합이 동일한 스텝끼리)로 그룹핑하여
        # 클러스터당 generate_safe_charset_sql을 1회만 호출한다.
        #
        # 처리 여부는 step identity(id())로 추적한다. location 문자열로
        # 추적하면 같은 location에 다른 issue_type/strategy를 가진 별개
        # step이 우연히 존재할 때 그 step의 선택된 fix가 조용히 누락되는
        # 버그가 있었다 (아래 per-step 루프의 skip 조건 참조).
        fk_safe_processed: Set[int] = set()
        fk_safe_steps = [
            s for s in steps
            if s.selected_option and s.selected_option.strategy == FixStrategy.COLLATION_FK_SAFE
        ]
        if fk_safe_steps:
            from collections import defaultdict as _defaultdict
            # 스키마별 → 클러스터별 2단계 그룹핑
            schema_cluster: Dict[str, Dict[frozenset, List[FixWizardStep]]] = _defaultdict(
                lambda: _defaultdict(list)
            )
            for s in fk_safe_steps:
                _schema = s.location.split('.')[0] if '.' in s.location else self.schema
                _cluster_key = frozenset(s.selected_option.related_tables or [])
                schema_cluster[_schema][_cluster_key].append(s)

            total_clusters = sum(len(v) for v in schema_cluster.values())
            self._log(
                f"  🔐 FK 안전 변경 배치 처리"
                f" ({len(fk_safe_steps)}개 스텝 → {total_clusters}개 클러스터)..."
            )

            for _schema, cluster_map in schema_cluster.items():
                for cluster_tables_frozen, cluster_steps in cluster_map.items():
                    cluster_tables = set(cluster_tables_frozen)
                    self._log(
                        f"    📦 클러스터 [{_schema}]: {len(cluster_tables)}개 테이블,"
                        f" {len(cluster_steps)}개 스텝"
                    )
                    fk_changer = FKSafeCharsetChanger(self.connector, _schema)

                    sql_parts = fk_changer.generate_safe_charset_sql(
                        cluster_tables, "utf8mb4", "utf8mb4_unicode_ci"
                    )
                    fk_msg = (
                        f"DRY-RUN: {sql_parts['fk_count']}개 FK,"
                        f" {sql_parts['table_count']}개 테이블 변경 예정"
                    )

                    for s in cluster_steps:
                        fk_safe_processed.add(id(s))
                        results.append(FixExecutionResult(
                            success=True,
                            message=f"{fk_msg} (배치)",
                            sql_executed=s.selected_option.sql_template or "",
                            affected_rows=1,
                            location=s.location,
                            description=s.description
                        ))
                        success_count += 1
                        total_affected += 1

                    self._log(f"    ✅ 클러스터 완료 ({len(cluster_tables)}개 테이블)")

        # === COLLATION_SINGLE 컬럼별 → 테이블별 병합 ===
        merged_steps: Set[int] = set()

        single_col_steps = [
            s for s in steps
            if (s.selected_option
                and s.selected_option.strategy == FixStrategy.COLLATION_SINGLE
                and s.selected_option.modify_clause  # 구조화 필드 존재
                and len(s.location.split('.')) > 2)  # column-level
        ]

        if single_col_steps:
            from collections import defaultdict as _defaultdict2
            table_groups: Dict[tuple, List[FixWizardStep]] = _defaultdict2(list)
            for s in single_col_steps:
                parts = s.location.split('.')
                table_groups[(parts[0], parts[1])].append(s)

            for (schema_name, table_name), group_steps in table_groups.items():
                if len(group_steps) < 2:
                    continue

                # modify_clause 필드에서 직접 병합 (regex 파싱 불필요)
                clauses = [
                    f"MODIFY COLUMN {s.selected_option.modify_clause}"
                    for s in group_steps
                    if s.selected_option and s.selected_option.modify_clause
                ]
                if len(clauses) < 2:
                    continue

                merged_sql = (
                    f"ALTER TABLE `{schema_name}`.`{table_name}`\n  "
                    + ",\n  ".join(clauses) + ";"
                )

                self._log(
                    f"  📦 COLLATION_SINGLE 병합: `{table_name}` "
                    f"({len(clauses)}개 컬럼 → 1개 DDL)"
                )

                merge_result = self._estimate_affected_rows(merged_sql, group_steps[0])

                # 그룹 내 모든 스텝 결과 기록 (2-phase bookkeeping: results 확정 후 merged_steps 갱신)
                pending: Set[int] = set()
                for idx, s in enumerate(group_steps):
                    results.append(FixExecutionResult(
                        success=merge_result.success,
                        message=merge_result.message + f" (병합: {len(clauses)}컬럼)",
                        sql_executed=(
                            merged_sql if idx == 0
                            else f"-- 병합됨 ({table_name})"
                        ),
                        affected_rows=(
                            merge_result.affected_rows if idx == 0 else 0
                        ),
                        location=s.location,
                        description=s.description
                    ))
                    pending.add(id(s))
                    if merge_result.success:
                        success_count += 1
                        if idx == 0:
                            total_affected += merge_result.affected_rows
                    else:
                        fail_count += 1
                merged_steps.update(pending)

                if merge_result.success:
                    self._log(f"    ✅ {table_name} 병합 완료 ({len(clauses)}컬럼)")

        for i, step in enumerate(steps, 1):
            # 배치로 이미 처리된 FK 안전 변경 스텝 건너뛰기 (step identity 기준)
            if id(step) in fk_safe_processed:
                continue
            # COLLATION_SINGLE 병합 처리된 스텝 건너뛰기 (step identity 기준)
            if id(step) in merged_steps:
                continue

            # 건너뛰기 옵션 확인
            if step.selected_option and step.selected_option.strategy == FixStrategy.SKIP:
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 건너뛰기")
                results.append(FixExecutionResult(
                    success=True,
                    message="건너뛰기",
                    sql_executed="",
                    affected_rows=0,
                    location=step.location,
                    description=step.description
                ))
                skip_count += 1
                continue

            # SQL 생성
            sql = step.selected_option.sql_template if step.selected_option else ""
            if not sql or sql.startswith("--"):
                # 수동 처리 사유: 선택된 옵션의 description 또는 step.description 사용
                skip_desc = ""
                if step.selected_option:
                    skip_desc = step.selected_option.description
                if not skip_desc:
                    skip_desc = step.description
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 수동 처리 필요: {skip_desc}")
                results.append(FixExecutionResult(
                    success=True,
                    message="수동 처리 필요",
                    sql_executed=sql,
                    affected_rows=0,
                    location=step.location,
                    description=skip_desc
                ))
                skip_count += 1
                continue

            # 사용자 입력 대체
            if step.selected_option and step.selected_option.requires_input and step.user_input:
                sql = sql.replace("{custom_date}", step.user_input)
                sql = sql.replace("{precision}", step.user_input)

            self._log(f"  [{i}/{len(steps)}] {mode} {step.location}...")

            # Dry-run: COUNT 쿼리로 영향 행 추정
            result = self._estimate_affected_rows(sql, step)

            # FK 정렬 후 step↔result 매핑 오류 방지: location을 result에 직접 저장
            result.location = step.location
            results.append(result)

            if result.success:
                success_count += 1
                total_affected += result.affected_rows
                if result.affected_rows > 0:
                    self._log(f"    ✅ {result.message} ({result.affected_rows}행)")
                else:
                    self._log(f"    ✅ {result.message}")
            else:
                fail_count += 1
                self._log(f"    ❌ {result.message}")

        return BatchExecutionResult(
            total_steps=len(steps),
            success_count=success_count,
            fail_count=fail_count,
            skip_count=skip_count,
            results=results,
            total_affected_rows=total_affected,
            rollback_sql=rollback_sql
        )

    def _execute_single(self, sql: str) -> FixExecutionResult:
        """단일 SQL 실행"""
        raise RuntimeError(
            "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
            "DB mutations must be owned by Rust Core."
        )

    def _estimate_affected_rows(self, sql: str, step: FixWizardStep) -> FixExecutionResult:
        """영향 행 추정 (Dry-run용)

        UPDATE/DELETE 문을 COUNT 쿼리로 변환
        """
        try:
            sql_upper = sql.upper()

            # UPDATE 문 처리
            if 'UPDATE' in sql_upper and 'WHERE' in sql_upper:
                # UPDATE table SET ... WHERE condition → SELECT COUNT(*) FROM table WHERE condition
                # 간단한 파싱
                where_idx = sql.upper().find('WHERE')
                from_idx = sql.upper().find('UPDATE') + 6
                set_idx = sql.upper().find('SET')

                table_part = sql[from_idx:set_idx].strip()
                where_clause = sql[where_idx:]

                count_sql = f"SELECT COUNT(*) as cnt FROM {table_part} {where_clause}"
                # 세미콜론 제거
                count_sql = count_sql.rstrip(';')

                # 0000-00-00 날짜값이 있을 경우 strict mode에서 COUNT 쿼리가 실패하므로
                # 임시로 sql_mode를 완화한 뒤 실행하고 복원한다
                _saved_mode: Optional[str] = None
                try:
                    _saved_mode = self.connector.get_session_sql_mode()
                    self.connector.set_session_sql_mode('')
                except Exception:
                    pass  # 모드 조회/설정 실패 시 현재 모드로 시도

                try:
                    result = self.connector.execute(count_sql)
                    affected = result[0]['cnt'] if result else 0
                    count_ok = True
                except Exception:
                    affected = 0
                    count_ok = False
                finally:
                    if _saved_mode is not None:
                        try:
                            self.connector.set_session_sql_mode(_saved_mode)
                        except Exception:
                            pass

                if not count_ok:
                    return FixExecutionResult(
                        success=True,
                        message="[DRY-RUN] 예상 영향 행: ≥1 (0000-00-00 등 비표준 값 포함으로 정확한 수 불명)",
                        sql_executed=sql,
                        affected_rows=1
                    )

                return FixExecutionResult(
                    success=True,
                    message=f"[DRY-RUN] 예상 영향 행: {affected:,}",
                    sql_executed=sql,
                    affected_rows=affected
                )

            # ALTER TABLE 등 DDL은 영향 행 추정 불가
            elif 'ALTER' in sql_upper:
                return FixExecutionResult(
                    success=True,
                    message="[DRY-RUN] DDL 문 - 영향 행 추정 불가",
                    sql_executed=sql,
                    affected_rows=0
                )

            else:
                return FixExecutionResult(
                    success=True,
                    message="[DRY-RUN] 분석 완료",
                    sql_executed=sql,
                    affected_rows=0
                )

        except Exception as e:
            return FixExecutionResult(
                success=False,
                message=f"[DRY-RUN] 분석 오류: {str(e)}",
                sql_executed=sql,
                error=str(e)
            )


class RollbackSQLGenerator:
    """Rollback SQL 생성기

    DDL(ALTER TABLE)은 auto-commit되므로 트랜잭션 롤백이 불가능합니다.
    대신 변경 전 상태를 기록하고, 원래 상태로 되돌리는 SQL을 생성합니다.
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        # 변경 전 상태 캐시
        self._table_charset_cache: Dict[str, Dict[str, str]] = {}
        self._column_info_cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _format_default_clause(col_info: Dict[str, Any]) -> str:
        """COLUMN_DEFAULT 값 → DEFAULT 절 문자열 생성

        SmartFixGenerator와 공유하는 모듈 레벨 헬퍼(_format_default_sql_clause)에 위임한다.
        """
        return _format_default_sql_clause(col_info)

    @staticmethod
    def _format_extra_clause(col_info: Dict[str, Any]) -> str:
        """EXTRA 필드 → SQL 절 생성 (AUTO_INCREMENT, ON UPDATE 등)

        'DEFAULT_GENERATED' 등 내부 마킹은 생략하고 유의미한 속성만 출력.
        """
        extra = (col_info.get('EXTRA') or '').lower()
        if not extra:
            return ''
        parts = []
        if 'auto_increment' in extra:
            parts.append('AUTO_INCREMENT')
        if 'on update current_timestamp' in extra:
            parts.append('ON UPDATE CURRENT_TIMESTAMP')
        return ' '.join(parts)

    def capture_table_charset(self, table: str) -> Dict[str, str]:
        """테이블의 현재 charset/collation 캡처"""
        cache_key = f"{self.schema}.{table}"
        if cache_key in self._table_charset_cache:
            return self._table_charset_cache[cache_key]

        query = """
        SELECT
            TABLE_NAME,
            TABLE_COLLATION,
            CCSA.CHARACTER_SET_NAME as TABLE_CHARSET
        FROM INFORMATION_SCHEMA.TABLES T
        LEFT JOIN INFORMATION_SCHEMA.COLLATION_CHARACTER_SET_APPLICABILITY CCSA
            ON T.TABLE_COLLATION = CCSA.COLLATION_NAME
        WHERE T.TABLE_SCHEMA = %s AND T.TABLE_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table))

        if result:
            info = {
                'charset': result[0]['TABLE_CHARSET'] or 'utf8mb3',
                'collation': result[0]['TABLE_COLLATION'] or 'utf8mb3_general_ci'
            }
        else:
            info = {'charset': 'utf8mb3', 'collation': 'utf8mb3_general_ci'}

        self._table_charset_cache[cache_key] = info
        return info

    def _get_fk_sql_for_tables(self, schema: str, tables: List[str]) -> Tuple[List[str], List[str]]:
        """대상 테이블의 FK DROP/ADD SQL 조회

        Returns:
            (drop_sqls, add_sqls) 튜플
        """
        if not tables or not self.connector:
            return [], []

        placeholders = ", ".join(["%s"] * len(tables))
        query = f"""
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.TABLE_NAME,
            kcu.COLUMN_NAME,
            kcu.REFERENCED_TABLE_NAME,
            kcu.REFERENCED_COLUMN_NAME,
            kcu.ORDINAL_POSITION,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s
            AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            AND (kcu.TABLE_NAME IN ({placeholders}) OR kcu.REFERENCED_TABLE_NAME IN ({placeholders}))
        ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """
        try:
            params = (schema,) + tuple(tables) + tuple(tables)
            rows = self.connector.execute(query, params)
        except Exception:
            return [], []

        # 복합 FK 그룹화
        fk_map: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = f"{row['TABLE_NAME']}.{row['CONSTRAINT_NAME']}"
            if key not in fk_map:
                fk_map[key] = {
                    'constraint': row['CONSTRAINT_NAME'],
                    'table': row['TABLE_NAME'],
                    'columns': [],
                    'ref_table': row['REFERENCED_TABLE_NAME'],
                    'ref_columns': [],
                    'on_delete': row.get('DELETE_RULE', 'RESTRICT'),
                    'on_update': row.get('UPDATE_RULE', 'RESTRICT'),
                }
            fk_map[key]['columns'].append(row['COLUMN_NAME'])
            fk_map[key]['ref_columns'].append(row['REFERENCED_COLUMN_NAME'])

        drop_sqls = []
        add_sqls = []
        for fk in fk_map.values():
            drop_sqls.append(
                f"ALTER TABLE `{schema}`.`{fk['table']}` DROP FOREIGN KEY `{fk['constraint']}`;"
            )
            cols = ", ".join(f"`{c}`" for c in fk['columns'])
            ref_cols = ", ".join(f"`{c}`" for c in fk['ref_columns'])
            add_sqls.append(
                f"ALTER TABLE `{schema}`.`{fk['table']}` ADD CONSTRAINT `{fk['constraint']}` "
                f"FOREIGN KEY ({cols}) REFERENCES `{fk['ref_table']}` ({ref_cols}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']};"
            )

        return drop_sqls, add_sqls

    def capture_column_info(self, table: str, column: str) -> Dict[str, Any]:
        """컬럼의 현재 정보 캡처 (charset 포함)"""
        cache_key = f"{self.schema}.{table}.{column}"
        if cache_key in self._column_info_cache:
            return self._column_info_cache[cache_key]

        query = """
        SELECT
            COLUMN_NAME,
            COLUMN_TYPE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            CHARACTER_SET_NAME,
            COLLATION_NAME,
            EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table, column))

        if result:
            info = dict(result[0])
        else:
            info = {}

        self._column_info_cache[cache_key] = info
        return info

    def capture_tables_state(self, tables: Set[str]) -> Dict[str, Dict[str, str]]:
        """여러 테이블의 상태 일괄 캡처"""
        states = {}
        for table in tables:
            states[table] = self.capture_table_charset(table)
        return states

    def generate_rollback_sql(
        self,
        step: 'FixWizardStep',
        original_state: Optional[Dict[str, Any]] = None,
        all_pre_states: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> str:
        """단일 step에 대한 Rollback SQL 생성

        Args:
            step: 실행된 FixWizardStep
            original_state: 변경 전 상태 (없으면 캐시에서 조회)
            all_pre_states: 전체 pre-state 맵 (FK 일괄 변경 시 연관 테이블 상태 조회용)

        Returns:
            Rollback SQL 문자열
        """
        if not step.selected_option:
            return ""

        strategy = step.selected_option.strategy

        # 건너뛰기/수동 처리는 롤백 불필요
        if strategy in (FixStrategy.SKIP, FixStrategy.MANUAL):
            return ""

        location_parts = step.location.split('.')
        if len(location_parts) < 2:
            return ""

        schema = location_parts[0]
        table = location_parts[1]
        column = location_parts[2] if len(location_parts) > 2 else None

        lines = []

        # === 날짜 수정 롤백 ===
        if strategy in (FixStrategy.DATE_TO_NULL, FixStrategy.DATE_TO_MIN, FixStrategy.DATE_TO_CUSTOM):
            lines.append(f"-- ⚠️ 날짜 값 롤백 불가")
            lines.append(f"-- 원본 값이 0000-00-00이었으므로 복원할 값을 알 수 없습니다.")
            lines.append(f"-- 테이블: {table}, 컬럼: {column}")
            lines.append(f"-- 백업 데이터에서 복원하거나 수동으로 처리하세요.")
            return "\n".join(lines)

        # === Collation 롤백 ===
        if strategy == FixStrategy.COLLATION_SINGLE:
            if column:
                # 컬럼 레벨 롤백
                col_info = original_state or self.capture_column_info(table, column)
                if col_info:
                    orig_charset = col_info.get('CHARACTER_SET_NAME', 'utf8mb3')
                    orig_collation = col_info.get('COLLATION_NAME', 'utf8mb3_general_ci')
                    col_type = col_info.get('COLUMN_TYPE', 'VARCHAR(255)')
                    nullable = 'NULL' if col_info.get('IS_NULLABLE') == 'YES' else 'NOT NULL'
                    default_clause = self._format_default_clause(col_info)
                    extra_clause = self._format_extra_clause(col_info)

                    # 컬럼 정의: type nullable [default] [extra] charset collation
                    col_def_parts = [col_type, nullable]
                    if default_clause:
                        col_def_parts.append(default_clause)
                    if extra_clause:
                        col_def_parts.append(extra_clause)
                    col_def_parts.append(
                        f"CHARACTER SET {orig_charset} COLLATE {orig_collation}"
                    )

                    lines.append(f"-- Rollback: {table}.{column} 컬럼 charset 복원")
                    lines.append(f"-- 원본: {orig_charset} / {orig_collation}")
                    lines.append(
                        f"ALTER TABLE `{schema}`.`{table}` "
                        f"MODIFY COLUMN `{column}` {' '.join(col_def_parts)};"
                    )
            else:
                # 테이블 레벨 롤백
                tbl_info = original_state or self.capture_table_charset(table)
                orig_charset = tbl_info.get('charset', 'utf8mb3')
                orig_collation = tbl_info.get('collation', 'utf8mb3_general_ci')

                lines.append(f"-- Rollback: {table} 테이블 charset 복원")
                lines.append(f"-- 원본: {orig_charset} / {orig_collation}")
                lines.append(
                    f"ALTER TABLE `{schema}`.`{table}` "
                    f"CONVERT TO CHARACTER SET {orig_charset} COLLATE {orig_collation};"
                )

        elif strategy in (FixStrategy.COLLATION_FK_CASCADE, FixStrategy.COLLATION_FK_SAFE):
            # FK 일괄 변경 롤백 - 모든 연관 테이블 복원
            related_tables = step.selected_option.related_tables or [table]

            lines.append(f"-- Rollback: FK 연관 테이블 일괄 charset 복원")
            lines.append(f"-- 대상 테이블: {', '.join(related_tables)}")
            lines.append("")

            # FK 안전 변경과 동일하게 FK DROP → 변경 → FK 재생성 구조
            # FK SQL 조회 (concrete SQL 생성)
            drop_sqls, add_sqls = [], []
            if strategy == FixStrategy.COLLATION_FK_SAFE:
                drop_sqls, add_sqls = self._get_fk_sql_for_tables(schema, related_tables)

                lines.append("-- Phase 1: FK 임시 DROP")
                if drop_sqls:
                    for sql in drop_sqls:
                        lines.append(sql)
                else:
                    lines.append("-- (FK 정의 조회 실패 - 원본 실행 로그 참조)")
                lines.append("")

            lines.append("-- Phase 2: Charset 복원")
            for tbl in related_tables:
                # pre-state 우선 사용 (변경 전 상태), 없으면 현재 상태 캡처 (fallback)
                # 테이블 레벨 키(schema.table) 먼저 조회, 없으면 컬럼 레벨 키도 탐색
                tbl_location = f"{schema}.{tbl}"
                tbl_info = None
                if all_pre_states:
                    if tbl_location in all_pre_states:
                        tbl_info = all_pre_states[tbl_location]
                    else:
                        # 컬럼 레벨 키 중 해당 테이블 소속 첫 번째 항목 사용
                        for key, val in all_pre_states.items():
                            if key.startswith(f"{tbl_location}."):
                                tbl_info = val
                                break
                if tbl_info is None:
                    if original_state and tbl == table:
                        tbl_info = original_state
                    else:
                        tbl_info = self.capture_table_charset(tbl)
                orig_charset = tbl_info.get('charset', 'utf8mb3')
                orig_collation = tbl_info.get('collation', 'utf8mb3_general_ci')

                lines.append(f"-- {tbl}: {orig_charset} / {orig_collation}")
                lines.append(
                    f"ALTER TABLE `{schema}`.`{tbl}` "
                    f"CONVERT TO CHARACTER SET {orig_charset} COLLATE {orig_collation};"
                )

            if strategy == FixStrategy.COLLATION_FK_SAFE:
                lines.append("")
                lines.append("-- Phase 3: FK 재생성")
                if add_sqls:
                    for sql in add_sqls:
                        lines.append(sql)
                else:
                    lines.append("-- (FK 정의 조회 실패 - 원본 실행 로그 참조)")

        return "\n".join(lines)

    def generate_batch_rollback(
        self,
        steps: List['FixWizardStep'],
        pre_states: Dict[str, Dict[str, Any]]
    ) -> str:
        """배치 실행에 대한 전체 Rollback SQL 생성

        Args:
            steps: 실행된 FixWizardStep 목록
            pre_states: 변경 전 상태 맵 (location -> state)

        Returns:
            전체 Rollback SQL 문자열
        """
        from datetime import datetime

        lines = []
        lines.append("-- " + "=" * 60)
        lines.append("-- 마이그레이션 자동 수정 ROLLBACK SQL")
        lines.append(f"-- 스키마: {self.schema}")
        lines.append(f"-- 생성일시: {datetime.now().isoformat()}")
        lines.append("-- " + "=" * 60)
        lines.append("")
        lines.append("-- ⚠️ 주의사항:")
        lines.append("-- 1. 이 파일은 변경 전 상태로 되돌리기 위한 SQL입니다.")
        lines.append("-- 2. DDL(ALTER TABLE)은 트랜잭션 롤백이 불가능하므로")
        lines.append("--    문제 발생 시 이 SQL을 수동으로 실행하세요.")
        lines.append("-- 3. 날짜 값 변경은 원본 값을 알 수 없어 자동 롤백이 불가능합니다.")
        lines.append("-- 4. 실행 전 반드시 내용을 확인하세요.")
        lines.append("")
        lines.append("")

        # 이미 처리한 테이블/컬럼 추적 (중복 방지)
        processed_tables: Set[str] = set()      # 테이블 레벨 중복 방지
        processed_locations: Set[str] = set()  # 컬럼 레벨 COLLATION_SINGLE 중복 방지
        rollback_count = 0

        for step in steps:
            if not step.selected_option:
                continue

            if step.selected_option.strategy == FixStrategy.SKIP:
                continue

            # 자동 포함된 테이블은 건너뛰기 (원본 step에서 처리)
            if step.included_by is not None:
                continue

            location = step.location
            location_parts = location.split('.')
            table = location_parts[1] if len(location_parts) > 1 else location
            column = location_parts[2] if len(location_parts) > 2 else None
            strategy = step.selected_option.strategy

            if strategy in (FixStrategy.COLLATION_FK_CASCADE, FixStrategy.COLLATION_FK_SAFE):
                # FK 일괄 변경: 연관 테이블 전체를 테이블 단위로 중복 방지
                tables_to_check = set(step.selected_option.related_tables or [table])
                if tables_to_check & processed_tables:
                    continue
                processed_tables.update(tables_to_check)
            elif strategy == FixStrategy.COLLATION_SINGLE and column:
                # 컬럼 레벨: 같은 테이블의 여러 컬럼이 각각 롤백되어야 하므로
                # 테이블 단위가 아닌 location 전체를 키로 사용
                if location in processed_locations:
                    continue
                processed_locations.add(location)
            else:
                # 테이블 레벨: 테이블 단위 중복 방지
                if table in processed_tables:
                    continue
                processed_tables.add(table)

            # 원본 상태 가져오기
            original_state = pre_states.get(location)

            rollback_sql = self.generate_rollback_sql(step, original_state, all_pre_states=pre_states)
            if rollback_sql:
                rollback_count += 1
                lines.append(f"-- [{rollback_count}] {location}")
                lines.append(f"-- 전략: {step.selected_option.label}")
                lines.append(rollback_sql)
                lines.append("")

        if rollback_count == 0:
            lines.append("-- (롤백 가능한 변경사항이 없습니다)")

        return "\n".join(lines)


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


class CharsetFixPlanBuilder:
    """문자셋 수정 계획 빌더

    문자셋 이슈에 대해:
    1. 원본 이슈 테이블 + FK 연관 테이블 전체 목록 생성
    2. 연쇄 건너뛰기 테이블 계산
    3. FK 안전 변경 SQL 생성 (무조건 FK DROP → 변경 → FK 재생성)
    """

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        original_issue_tables: Set[str]
    ):
        """
        Args:
            connector: DB 연결
            schema: 스키마명
            original_issue_tables: 원본 분석에서 검출된 이슈 테이블 집합
        """
        self.connector = connector
        self.schema = schema
        self.original_issue_tables = original_issue_tables

        # FK 그래프 빌더
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

        # 테이블 정보 캐시
        self._table_info_cache: Dict[str, CharsetTableInfo] = {}

    def _get_fk_graph_builder(self) -> CollationFKGraphBuilder:
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = CollationFKGraphBuilder(self.connector, self.schema)
            self._fk_graph_builder.build_graph()
        return self._fk_graph_builder

    def _get_table_charset(self, table: str) -> Tuple[str, str]:
        """테이블의 현재 charset/collation 조회"""
        query = """
        SELECT
            TABLE_COLLATION,
            CCSA.CHARACTER_SET_NAME as TABLE_CHARSET
        FROM INFORMATION_SCHEMA.TABLES T
        LEFT JOIN INFORMATION_SCHEMA.COLLATION_CHARACTER_SET_APPLICABILITY CCSA
            ON T.TABLE_COLLATION = CCSA.COLLATION_NAME
        WHERE T.TABLE_SCHEMA = %s AND T.TABLE_NAME = %s
        """
        result = self.connector.execute(query, (self.schema, table))

        if result:
            charset = result[0]['TABLE_CHARSET'] or 'utf8mb3'
            collation = result[0]['TABLE_COLLATION'] or 'utf8mb3_general_ci'
            return charset, collation
        return 'utf8mb3', 'utf8mb3_general_ci'

    def build_full_table_list(self) -> List[CharsetTableInfo]:
        """원본 이슈 테이블 + FK 연관 테이블 전체 목록 생성

        Returns:
            CharsetTableInfo 목록 (위상 정렬 순서)
        """
        fk_builder = self._get_fk_graph_builder()

        # 1. 원본 이슈 테이블의 모든 FK 연관 테이블 수집
        all_tables: Set[str] = set()
        for table in self.original_issue_tables:
            all_tables.add(table)
            related = fk_builder.get_related_tables(table)
            all_tables.update(related)

        # 2. 위상 정렬 (부모 먼저)
        ordered_tables = fk_builder.get_topological_order(all_tables)

        # 3. 각 테이블 정보 생성
        result: List[CharsetTableInfo] = []
        for table in ordered_tables:
            if table in self._table_info_cache:
                result.append(self._table_info_cache[table])
                continue

            charset, collation = self._get_table_charset(table)
            parents = list(fk_builder.get_parents(table))
            children = list(fk_builder.get_children(table))

            info = CharsetTableInfo(
                table_name=table,
                current_charset=charset,
                current_collation=collation,
                fk_parents=parents,
                fk_children=children,
                is_original_issue=(table in self.original_issue_tables),
                skip=False
            )
            self._table_info_cache[table] = info
            result.append(info)

        return result

    def get_cascade_skip_tables(self, table_to_skip: str) -> Set[str]:
        """연쇄 건너뛰기 테이블 계산

        특정 테이블 건너뛰기 시 FK 관계로 인해 함께 건너뛰어야 하는 테이블 목록.

        Args:
            table_to_skip: 건너뛰기할 테이블

        Returns:
            연쇄적으로 건너뛰어야 하는 테이블 집합 (table_to_skip 제외)
        """
        fk_builder = self._get_fk_graph_builder()

        # 전체 대상 테이블 목록
        target_tables = {info.table_name for info in self.build_full_table_list()}

        return fk_builder.get_cascade_skip_tables(table_to_skip, target_tables)

    def generate_fix_sql(
        self,
        tables_to_fix: Set[str],
        charset: str = "utf8mb4",
        collation: str = "utf8mb4_unicode_ci"
    ) -> Dict[str, Any]:
        """FK 안전 변경 SQL 생성

        무조건 FK DROP → charset 변경 → FK 재생성 방식 사용.

        Args:
            tables_to_fix: 변경할 테이블 집합
            charset: 목표 charset
            collation: 목표 collation

        Returns:
            Dict with keys: 'drop_fks', 'alter_tables', 'add_fks', 'full_sql', 'fk_count', 'table_count'
        """
        if not tables_to_fix:
            return {
                'drop_fks': [],
                'alter_tables': [],
                'add_fks': [],
                'full_sql': ["-- 변경할 테이블이 없습니다."],
                'fk_count': 0,
                'table_count': 0
            }

        # FKSafeCharsetChanger 사용
        changer = FKSafeCharsetChanger(self.connector, self.schema)
        return changer.generate_safe_charset_sql(tables_to_fix, charset, collation)


def create_wizard_steps(
    issues: List[Any],
    connector: MySQLConnector,
    schema: str
) -> List[FixWizardStep]:
    """이슈 목록에서 위저드 단계 생성

    Args:
        issues: CompatibilityIssue 목록
        connector: DB 연결
        schema: 스키마명

    Returns:
        FixWizardStep 목록
    """
    generator = SmartFixGenerator(connector, schema)
    steps = []

    for i, issue in enumerate(issues):
        options = generator.get_fix_options(issue)

        step = FixWizardStep(
            issue_index=i,
            issue_type=issue.issue_type,
            location=issue.location,
            description=issue.description,
            options=options
        )
        steps.append(step)

    return steps
