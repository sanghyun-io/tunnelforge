"""
스키마/객체 규칙 모듈

MySQL 8.0 → 8.4 업그레이드 시 스키마 및 객체 관련 호환성 검사 규칙.
구현 규칙 21개 (S01-S09, S16-S18, S23-S31):
- S01: YEAR(2) 데이터 타입
- S02: latin1 charset 권장
- S03-S04: 인덱스 크기 초과
- S05: GROUP BY ASC/DESC 구문
- S06: SQL_CALC_FOUND_ROWS
- S07: 달러 기호 식별자
- S08: 트레일링 스페이스 식별자
- S09: 제어 문자 식별자
- S16: 생성 컬럼 함수
- S17: old geometry 타입
- S18: BLOB/TEXT DEFAULT
- S23: MySQL 스키마 충돌
- S24-S25: Definer 검사
- S26: 파티션 키에 prefix 인덱스 사용 (이슈 #63)
- S27: 스키마 생략 dot 구문 (이슈 #63)
- S28: InnoDB ROW_FORMAT REDUNDANT/COMPACT (이슈 #63)
- S29: deprecated 날짜 구분자 (이슈 #63)
- S30: 루틴 이름이 예약어와 충돌 (이슈 #63)
- S31: 식별자에 연속 점(..) 사용 (이슈 #63)

SchemaRules는 규칙군별 믹스인(identifier/index_charset/definer/syntax)과
ProgressLoggingRuleBase를 상속하는 얇은 조합 클래스이며, S01/S26 및 통합
검사(check_all_*)만 직접 보유한다.
"""

from typing import List

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    YEAR2_PATTERN,
    PARTITION_PREFIX_KEY_PATTERN,
)
from ._base import ProgressLoggingRuleBase
from .identifier_rules import IdentifierRulesMixin
from .index_charset_rules import IndexCharsetRulesMixin
from .definer_rules import DefinerRulesMixin
from .syntax_rules import SyntaxRulesMixin


class SchemaRules(
    IdentifierRulesMixin,
    IndexCharsetRulesMixin,
    DefinerRulesMixin,
    SyntaxRulesMixin,
    ProgressLoggingRuleBase,
):
    """스키마/객체 규칙 모음 (규칙군 믹스인 + 진행 로깅 베이스 조합)"""

    # ================================================================
    # S01: YEAR(2) 데이터 타입 검사 (라이브 DB)
    # ================================================================
    def check_year2_type(self, schema: str) -> List[CompatibilityIssue]:
        """YEAR(2) 타입 사용 확인"""
        if not self.connector:
            return []

        self._log("🔍 YEAR(2) 타입 검사 중...")
        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'year'
            AND COLUMN_TYPE LIKE 'year(2)%%'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.YEAR2_TYPE,
                severity="error",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description="YEAR(2) 타입 사용 (8.4에서 제거됨)",
                suggestion="ALTER TABLE ... MODIFY COLUMN ... YEAR(4)",
                table_name=col['TABLE_NAME'],
                column_name=col['COLUMN_NAME']
            ))

        self._log_summary(issues, "YEAR(2) 타입", "YEAR(2) 타입 없음")

        return issues

    # ================================================================
    # S01: YEAR(2) 검사 (덤프 파일)
    # ================================================================
    def check_year2_in_sql(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일에서 YEAR(2) 타입 확인"""
        issues = []

        for match in YEAR2_PATTERN.finditer(content):
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.YEAR2_TYPE,
                severity="error",
                location=location,
                description="YEAR(2) 타입 사용 (8.4에서 제거됨)",
                suggestion="YEAR(4)로 변경 필요",
                code_snippet=line[:80]
            ))

        return issues

    # ================================================================
    # S26: 파티션 키에 prefix 인덱스 사용 검사 (덤프 파일)
    # ================================================================
    def check_partition_prefix_key(self, content: str, location: str) -> List[CompatibilityIssue]:
        """파티션 키에 prefix 인덱스 사용 확인 (8.0.21 deprecated, 8.4 제거됨)"""
        issues = []

        for match in PARTITION_PREFIX_KEY_PATTERN.finditer(content):
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.PARTITION_PREFIX_KEY,
                severity="error",
                location=location,
                description="파티션 KEY에 prefix 인덱스 사용 (8.4에서 제거됨)",
                suggestion="파티션 키 컬럼에서 길이 지정(prefix) 제거 필요",
                code_snippet=line[:100]
            ))

        return issues

    # ================================================================
    # 통합 검사 메서드
    # ================================================================
    def check_all_live_db(self, schema: str) -> List[CompatibilityIssue]:
        """라이브 DB의 모든 스키마 검사 실행"""
        if not self.connector:
            return []

        issues = []
        issues.extend(self.check_year2_type(schema))
        issues.extend(self.check_latin1_charset(schema))
        issues.extend(self.check_index_too_large(schema))
        issues.extend(self.check_old_geometry_types(schema))
        issues.extend(self.check_mysql_schema_conflict(schema))
        issues.extend(self.check_routine_definer_missing(schema))
        issues.extend(self.check_view_definer_missing(schema))
        return issues

    def check_all_sql_content(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일 내용의 모든 스키마 검사 실행"""
        issues = []
        issues.extend(self.check_year2_in_sql(content, location))
        issues.extend(self.check_groupby_asc_desc(content, location))
        issues.extend(self.check_sql_calc_found_rows(content, location))
        issues.extend(self.check_dollar_sign_names(content, location))
        issues.extend(self.check_trailing_space_names(content, location))
        issues.extend(self.check_control_char_names(content, location))
        issues.extend(self.check_generated_column_functions(content, location))
        issues.extend(self.check_blob_text_default(content, location))
        # 신규 규칙 (이슈 #63)
        issues.extend(self.check_partition_prefix_key(content, location))
        issues.extend(self.check_empty_dot_table_syntax(content, location))
        issues.extend(self.check_innodb_row_format(content, location))
        issues.extend(self.check_deprecated_temporal_delimiter(content, location))
        issues.extend(self.check_routine_syntax_keyword(content, location))
        issues.extend(self.check_invalid_57_name_multiple_dots(content, location))
        return issues
