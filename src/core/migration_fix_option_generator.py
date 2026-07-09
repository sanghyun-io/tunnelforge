"""
마이그레이션 자동 수정 위저드 - Fix 옵션 생성기

호환성 이슈에 대해 컨텍스트 인식 수정 옵션을 생성하고, 이슈 목록에서
위저드 단계(FixWizardStep)를 만든다.
"""
from typing import List, Dict, Optional, Any

from src.core.db_connector import MySQLConnector
from src.core.migration_constants import IssueType
from src.core.migration_fix_models import (
    FixStrategy,
    FixOption,
    FixWizardStep,
    _format_default_sql_clause,
    DEFAULT_TARGET_CHARSET,
    DEFAULT_TARGET_COLLATION,
)
from src.core.migration_fk_graph import CollationFKGraphBuilder, build_fk_graph
from src.core.migration_fk_safe_charset import FKSafeCharsetChanger


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
            self._fk_graph_builder = build_fk_graph(self.connector, self.schema)
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

    def _invalid_date_where_clause(self, column: str) -> str:
        """0000-00-00 날짜 UPDATE의 WHERE 절 (3개 옵션 공유)"""
        return (
            f"WHERE `{column}` = '0000-00-00'\n"
            f"   OR `{column}` = '0000-00-00 00:00:00'\n"
            f"   OR (MONTH(`{column}`) = 0 OR DAY(`{column}`) = 0);"
        )

    def _get_invalid_date_options(self, issue: Any) -> List[FixOption]:
        """0000-00-00 날짜 수정 옵션"""
        options = []
        table = issue.table_name
        column = issue.column_name

        if not table or not column:
            return self._get_default_options(issue)

        # nullable 여부 확인
        is_nullable = self._is_column_nullable(table, column)

        where_clause = self._invalid_date_where_clause(column)

        # 1. NULL로 변경 (nullable 컬럼만)
        if is_nullable:
            options.append(FixOption(
                strategy=FixStrategy.DATE_TO_NULL,
                label="NULL로 변경 (권장)",
                description=f"0000-00-00 값을 NULL로 변경합니다.",
                sql_template=(
                    f"UPDATE `{self.schema}`.`{table}`\n"
                    f"SET `{column}` = NULL\n"
                    f"{where_clause}"
                ),
                is_recommended=True
            ))

        # 2. 최소값으로 변경
        options.append(FixOption(
            strategy=FixStrategy.DATE_TO_MIN,
            label="1970-01-01로 변경",
            description="0000-00-00 값을 Unix 시작일(1970-01-01)로 변경합니다.",
            sql_template=(
                f"UPDATE `{self.schema}`.`{table}`\n"
                f"SET `{column}` = '1970-01-01'\n"
                f"{where_clause}"
            ),
            is_recommended=not is_nullable  # nullable 아니면 이게 권장
        ))

        # 3. 사용자 지정 날짜
        options.append(FixOption(
            strategy=FixStrategy.DATE_TO_CUSTOM,
            label="사용자 지정 날짜",
            description="원하는 날짜로 직접 지정합니다.",
            sql_template=(
                f"UPDATE `{self.schema}`.`{table}`\n"
                f"SET `{column}` = '{{custom_date}}'\n"
                f"{where_clause}"
            ),
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
                charset=DEFAULT_TARGET_CHARSET,
                collation=DEFAULT_TARGET_COLLATION
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
                sql_template=(
                    f"ALTER TABLE `{schema}`.`{table}` CONVERT TO CHARACTER SET "
                    f"{DEFAULT_TARGET_CHARSET} COLLATE {DEFAULT_TARGET_COLLATION};"
                )
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
                        f"CONVERT TO CHARACTER SET {DEFAULT_TARGET_CHARSET} "
                        f"COLLATE {DEFAULT_TARGET_COLLATION};"
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
                    charset=DEFAULT_TARGET_CHARSET,
                    collation=DEFAULT_TARGET_COLLATION
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
