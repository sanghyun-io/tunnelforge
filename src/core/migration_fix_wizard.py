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
    related_tables: List[str] = field(default_factory=list)  # 관련 테이블 (collation용)


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


@dataclass
class BatchExecutionResult:
    """배치 실행 결과"""
    total_steps: int
    success_count: int
    fail_count: int
    skip_count: int
    results: List[FixExecutionResult]
    total_affected_rows: int = 0


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
            # 컬럼 레벨 charset 변경
            options.append(FixOption(
                strategy=FixStrategy.COLLATION_SINGLE,
                label="이 컬럼만 변경",
                description=f"{table}.{column} 컬럼의 charset을 utf8mb4로 변경합니다.",
                sql_template=f"""ALTER TABLE `{schema}`.`{table}`
MODIFY COLUMN `{column}` ... CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- 주의: 컬럼 타입(VARCHAR 등)을 확인 후 수동 조정이 필요합니다.""",
            ))
        else:
            # 테이블 레벨 charset 변경

            # 1. 단일 테이블만 변경
            options.append(FixOption(
                strategy=FixStrategy.COLLATION_SINGLE,
                label="이 테이블만 변경",
                description=f"{table} 테이블만 utf8mb4로 변경합니다.",
                sql_template=f"""ALTER TABLE `{schema}`.`{table}`
CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"""
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
                sql_template=f"ALTER TABLE `{self.schema}`.`{table}` MODIFY COLUMN `{column}` DECIMAL(10,2);",
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
        """FK 관계 그래프 구성"""
        query = """
        SELECT
            kcu.TABLE_NAME as CHILD_TABLE,
            kcu.REFERENCED_TABLE_NAME as PARENT_TABLE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        WHERE kcu.TABLE_SCHEMA = %s
            AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
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
        """문자셋 이슈가 포함되어 있는지 확인"""
        return any(
            step.issue_type == IssueType.CHARSET_ISSUE
            and step.selected_option
            and step.selected_option.strategy != FixStrategy.SKIP
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

            # 테이블명 추출
            table_to_step: Dict[str, FixWizardStep] = {}
            for step in charset_steps:
                table_name = step.location.split('.')[-1]
                table_to_step[table_name] = step

            # 위상 정렬
            all_tables = set(table_to_step.keys())
            sorted_tables = fk_builder.get_topological_order(all_tables)

            # 정렬된 순서로 steps 재배치
            sorted_charset_steps = []
            for table in sorted_tables:
                if table in table_to_step:
                    sorted_charset_steps.append(table_to_step[table])

            # 정렬되지 않은 테이블 추가 (FK 관계 없는 테이블)
            sorted_set = set(sorted_tables)
            for step in charset_steps:
                table_name = step.location.split('.')[-1]
                if table_name not in sorted_set:
                    sorted_charset_steps.append(step)

            self._log(f"  📊 FK 관계에 따라 {len(sorted_charset_steps)}개 테이블 정렬 완료")

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

        개선사항:
        - 문자셋 이슈 포함 시 FOREIGN_KEY_CHECKS=0 적용
        - FK 관계에 따른 실행 순서 최적화
        """
        results: List[FixExecutionResult] = []
        success_count = 0
        fail_count = 0
        skip_count = 0
        total_affected = 0

        mode = "[DRY-RUN]" if dry_run else "[실행]"
        self._log(f"🔧 {mode} 배치 수정 시작 ({len(steps)}개)")

        # 문자셋 이슈 확인 및 FK_CHECKS 비활성화
        has_charset = self._has_charset_issues(steps)
        if has_charset and not dry_run:
            self._log("  🔓 FOREIGN_KEY_CHECKS 비활성화 (문자셋 변경용)")
            try:
                with self.connector.connection.cursor() as cursor:
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                self.connector.connection.commit()
            except Exception as e:
                self._log(f"  ⚠️ FK_CHECKS 비활성화 실패: {e}")

        # FK 관계에 따른 실행 순서 정렬
        if has_charset:
            steps = self._sort_steps_by_fk_order(steps)

        for i, step in enumerate(steps, 1):
            # 건너뛰기 옵션 확인
            if step.selected_option and step.selected_option.strategy == FixStrategy.SKIP:
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 건너뛰기")
                results.append(FixExecutionResult(
                    success=True,
                    message="건너뛰기",
                    sql_executed="",
                    affected_rows=0
                ))
                skip_count += 1
                continue

            # SQL 생성
            sql = step.selected_option.sql_template if step.selected_option else ""
            if not sql or sql.startswith("--"):
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 수동 처리 필요")
                results.append(FixExecutionResult(
                    success=True,
                    message="수동 처리 필요",
                    sql_executed=sql,
                    affected_rows=0
                ))
                skip_count += 1
                continue

            # 사용자 입력 대체
            if step.selected_option and step.selected_option.requires_input and step.user_input:
                sql = sql.replace("{custom_date}", step.user_input)
                sql = sql.replace("{precision}", step.user_input)

            self._log(f"  [{i}/{len(steps)}] {mode} {step.location}...")

            if dry_run:
                # Dry-run: COUNT 쿼리로 영향 행 추정
                result = self._estimate_affected_rows(sql, step)
            else:
                # 실제 실행
                result = self._execute_single(sql)

            results.append(result)

            if result.success:
                if result.affected_rows > 0:
                    success_count += 1
                    total_affected += result.affected_rows
                    self._log(f"    ✅ {result.message} ({result.affected_rows}행)")
                else:
                    self._log(f"    ✅ {result.message}")
            else:
                fail_count += 1
                self._log(f"    ❌ {result.message}")

        # FOREIGN_KEY_CHECKS 복원
        if has_charset and not dry_run:
            self._log("  🔒 FOREIGN_KEY_CHECKS 복원")
            try:
                with self.connector.connection.cursor() as cursor:
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
                self.connector.connection.commit()
            except Exception as e:
                self._log(f"  ⚠️ FK_CHECKS 복원 실패: {e}")

        return BatchExecutionResult(
            total_steps=len(steps),
            success_count=success_count,
            fail_count=fail_count,
            skip_count=skip_count,
            results=results,
            total_affected_rows=total_affected
        )

    def _execute_single(self, sql: str) -> FixExecutionResult:
        """단일 SQL 실행"""
        try:
            # 여러 문장이 있을 수 있음 (FK_CHECKS 설정 등)
            statements = [s.strip() for s in sql.split(';') if s.strip()]

            total_affected = 0
            with self.connector.connection.cursor() as cursor:
                for stmt in statements:
                    if not stmt or stmt.startswith('--'):
                        continue
                    cursor.execute(stmt)
                    total_affected += cursor.rowcount if cursor.rowcount > 0 else 0

                self.connector.connection.commit()

            return FixExecutionResult(
                success=True,
                message="실행 완료",
                sql_executed=sql,
                affected_rows=total_affected
            )

        except Exception as e:
            self.connector.connection.rollback()
            return FixExecutionResult(
                success=False,
                message=f"실행 오류: {str(e)}",
                sql_executed=sql,
                error=str(e)
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

                result = self.connector.execute(count_sql)
                affected = result[0]['cnt'] if result else 0

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
