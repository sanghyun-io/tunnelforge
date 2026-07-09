"""
구문/함수 규칙 믹스인 (S05, S06, S16-S18, S28, S29)

SchemaRules에 합쳐지는 deprecated 구문/함수 관련 검사 모음(GROUP BY ASC/DESC,
SQL_CALC_FOUND_ROWS, 생성 컬럼 함수, old geometry, BLOB/TEXT DEFAULT, InnoDB
ROW_FORMAT, deprecated 날짜 구분자). self._extract_source_line / self._log_summary
등 공통 기능은 SchemaRules가 상속하는 ProgressLoggingRuleBase에서 온다.
"""

import re
from typing import List

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    DEPRECATED_SYNTAX_PATTERNS,
    BLOB_TEXT_DEFAULT_PATTERN,
    GENERATED_COLUMN_PATTERN,
    ALL_REMOVED_FUNCTIONS,
    CHANGED_FUNCTIONS_IN_GENERATED_COLUMNS,
    INNODB_ROW_FORMAT_PATTERN,
    DEPRECATED_TEMPORAL_DELIMITER_PATTERN,
)


class SyntaxRulesMixin:
    """구문/함수 관련 규칙 (S05, S06, S16-S18, S28, S29)"""

    # ================================================================
    # S05: GROUP BY ASC/DESC 구문 검사 (덤프 파일)
    # ================================================================
    def check_groupby_asc_desc(self, content: str, location: str) -> List[CompatibilityIssue]:
        """GROUP BY ASC/DESC 구문 사용 확인 (8.4에서 제거됨)"""
        issues = []

        for match in DEPRECATED_SYNTAX_PATTERNS['GROUP_BY_ASC_DESC'].finditer(content):
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.GROUPBY_ASC_DESC,
                severity="error",
                location=location,
                description="GROUP BY ASC/DESC 구문 사용 (8.4에서 제거됨)",
                suggestion="ORDER BY 절로 정렬을 분리하세요",
                code_snippet=line[:100]
            ))

        return issues

    # ================================================================
    # S06: SQL_CALC_FOUND_ROWS 검사 (덤프 파일)
    # ================================================================
    def check_sql_calc_found_rows(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL_CALC_FOUND_ROWS 사용 확인 (deprecated)"""
        issues = []

        for match in DEPRECATED_SYNTAX_PATTERNS['SQL_CALC_FOUND_ROWS'].finditer(content):
            issues.append(CompatibilityIssue(
                issue_type=IssueType.SQL_CALC_FOUND_ROWS_USAGE,
                severity="warning",
                location=location,
                description="SQL_CALC_FOUND_ROWS 사용 (deprecated)",
                suggestion="SELECT COUNT(*) 또는 ROW_COUNT() 사용 권장"
            ))

        # FOUND_ROWS() 함수도 확인
        for match in DEPRECATED_SYNTAX_PATTERNS['FOUND_ROWS_FUNC'].finditer(content):
            issues.append(CompatibilityIssue(
                issue_type=IssueType.SQL_CALC_FOUND_ROWS_USAGE,
                severity="warning",
                location=location,
                description="FOUND_ROWS() 함수 사용 (deprecated)",
                suggestion="ROW_COUNT() 또는 별도 COUNT 쿼리 사용 권장"
            ))

        return issues

    # ================================================================
    # S16: 생성 컬럼 함수 검사 (덤프 파일)
    # ================================================================
    def _matches_sql_function_call(self, expression: str, func: str) -> bool:
        """expression에서 func가 실제 함수 호출(또는 CASE 키워드)로 등장하는지 확인

        기존의 단순 부분 문자열 검사(`func in expression`)는
        'shift_rate' 안의 'IF'나 'IFNULL(...)' 안의 'IF'처럼 함수명이
        다른 식별자/함수명의 일부일 때 오탐한다. 식별자 경계(직전 문자가
        영숫자/밑줄/$가 아님)와 함수 호출을 나타내는 여는 괄호를 함께
        요구해 실제 호출만 매치한다. CASE는 함수가 아닌 키워드이므로
        괄호 대신 단어 경계만 요구한다.
        """
        if func.upper() == "CASE":
            pattern = r"(?<![A-Z0-9_$])CASE\b"
        else:
            pattern = rf"(?<![A-Z0-9_$]){re.escape(func)}\s*\("
        return re.search(pattern, expression, re.IGNORECASE) is not None

    def check_generated_column_functions(self, content: str, location: str) -> List[CompatibilityIssue]:
        """생성 컬럼에서 동작 변경 함수 사용 확인

        MySQL 8.4에서 generated column 내 동작이 변경된 함수를 검사합니다.
        (IF, IFNULL, CASE, COALESCE 등 — mysql-upgrade-checker 참조)
        """
        issues = []

        for match in GENERATED_COLUMN_PATTERN.finditer(content):
            expression = match.group(1)
            # 8.4에서 동작이 변경된 함수 검사 (호출/키워드 경계 기반 매칭)
            for func in tuple(dict.fromkeys(CHANGED_FUNCTIONS_IN_GENERATED_COLUMNS)):
                if self._matches_sql_function_call(expression, func):
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.GENERATED_COLUMN_ISSUE,
                        severity="warning",
                        location=location,
                        description=f"생성 컬럼에 8.4 동작 변경 함수 사용: {func}",
                        suggestion=f"'{func}' 함수의 8.4 동작 변경 확인 필요 (타입 추론 규칙 변경)",
                        code_snippet=match.group(0)[:80]
                    ))
            # 제거된 함수 검사 (PASSWORD, ENCRYPT 등)
            # ALL_REMOVED_FUNCTIONS는 정의 시점에 이미 dict.fromkeys로
            # 중복 제거되어 있지만, 이 메서드에서도 방어적으로 한 번 더
            # dedupe한다 (migration_constants.py는 이 WP의 수정 대상이 아님)
            for func in tuple(dict.fromkeys(ALL_REMOVED_FUNCTIONS)):
                if self._matches_sql_function_call(expression, func):
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.GENERATED_COLUMN_ISSUE,
                        severity="error",
                        location=location,
                        description=f"생성 컬럼에 제거된 함수 사용: {func}",
                        suggestion=f"'{func}' 함수를 대체 함수로 변경 필요",
                        code_snippet=match.group(0)[:80]
                    ))

        return issues

    # ================================================================
    # S17: old geometry 타입 검사 (라이브 DB)
    # ================================================================
    def check_old_geometry_types(self, schema: str) -> List[CompatibilityIssue]:
        """구 geometry 타입 사용 확인"""
        if not self.connector:
            return []

        self._log("🔍 Geometry 타입 검사 중...")
        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('geometry', 'point', 'linestring', 'polygon',
                             'multipoint', 'multilinestring', 'multipolygon',
                             'geometrycollection')
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.OLD_GEOMETRY_TYPE,
                severity="info",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"Geometry 타입 사용: {col['DATA_TYPE']}",
                suggestion="8.4에서 WKB 형식 변경 확인 필요",
                table_name=col['TABLE_NAME'],
                column_name=col['COLUMN_NAME']
            ))

        self._log_summary(issues, "Geometry 타입", "Geometry 타입 없음", emoji="ℹ️")

        return issues

    # ================================================================
    # S18: BLOB/TEXT DEFAULT 검사 (덤프 파일)
    # ================================================================
    def check_blob_text_default(self, content: str, location: str) -> List[CompatibilityIssue]:
        """BLOB/TEXT 컬럼의 DEFAULT 값 확인"""
        issues = []

        for match in BLOB_TEXT_DEFAULT_PATTERN.finditer(content):
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.BLOB_TEXT_DEFAULT,
                severity="error",
                location=location,
                description="BLOB/TEXT 컬럼에 DEFAULT 값 설정",
                suggestion="BLOB/TEXT 컬럼은 DEFAULT를 지원하지 않음",
                code_snippet=line[:80]
            ))

        return issues

    # ================================================================
    # S28: InnoDB ROW_FORMAT REDUNDANT/COMPACT 검사 (덤프 파일)
    # ================================================================
    def check_innodb_row_format(self, content: str, location: str) -> List[CompatibilityIssue]:
        """REDUNDANT/COMPACT ROW_FORMAT 사용 확인 (DYNAMIC 권장)"""
        issues = []

        for match in INNODB_ROW_FORMAT_PATTERN.finditer(content):
            row_format = match.group(1).upper()
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.INNODB_ROW_FORMAT,
                severity="warning",
                location=location,
                description=f"InnoDB ROW_FORMAT={row_format} 사용 (DYNAMIC 권장)",
                suggestion="ROW_FORMAT=DYNAMIC으로 변경 권장 (더 나은 성능과 호환성)",
                code_snippet=line[:100]
            ))

        return issues

    # ================================================================
    # S29: deprecated 날짜 구분자 검사 (덤프 파일)
    # ================================================================
    def check_deprecated_temporal_delimiter(self, content: str, location: str) -> List[CompatibilityIssue]:
        """deprecated 날짜 구분자 사용 확인 (@ ! # 등 비표준 구분자)"""
        issues = []

        for match in DEPRECATED_TEMPORAL_DELIMITER_PATTERN.finditer(content):
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.DEPRECATED_TEMPORAL_DELIMITER,
                severity="error",
                location=location,
                description=f"deprecated 날짜 구분자 사용: {match.group(0)}",
                suggestion="날짜 구분자로 '-' 또는 '/' 사용 권장 (예: '2024-01-01')",
                code_snippet=line[:100]
            ))

        return issues
