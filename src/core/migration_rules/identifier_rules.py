"""
식별자 규칙 믹스인 (S07-S09, S27, S30, S31)

SchemaRules에 합쳐지는 식별자 관련 검사 모음(달러 기호/트레일링 스페이스/
제어 문자/스키마 생략 dot/예약어 루틴명/연속 점). self._extract_source_line
같은 공통 기능은 SchemaRules가 상속하는 ProgressLoggingRuleBase에서 온다.
"""

from typing import List

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    DOLLAR_SIGN_PATTERN,
    TRAILING_SPACE_PATTERN,
    CONTROL_CHAR_PATTERN,
    EMPTY_DOT_TABLE_SYNTAX_PATTERN,
    ROUTINE_SYNTAX_KEYWORD_PATTERN,
    ALL_RESERVED_KEYWORDS,
    INVALID_57_NAME_MULTIPLE_DOTS_PATTERN,
)


class IdentifierRulesMixin:
    """식별자 관련 규칙 (S07-S09, S27, S30, S31)"""

    # ================================================================
    # S07: 달러 기호 식별자 검사 (덤프 파일)
    # ================================================================
    def check_dollar_sign_names(self, content: str, location: str) -> List[CompatibilityIssue]:
        """식별자에 $ 문자 사용 확인 (deprecated)"""
        issues = []

        for match in DOLLAR_SIGN_PATTERN.finditer(content):
            identifier = match.group(0)
            issues.append(CompatibilityIssue(
                issue_type=IssueType.DOLLAR_SIGN_NAME,
                severity="warning",
                location=location,
                description=f"식별자에 $ 문자 사용: {identifier}",
                suggestion="$ 문자는 향후 버전에서 제한될 수 있음"
            ))

        return issues

    # ================================================================
    # S08: 트레일링 스페이스 식별자 검사 (덤프 파일)
    # ================================================================
    def check_trailing_space_names(self, content: str, location: str) -> List[CompatibilityIssue]:
        """식별자 끝에 공백 문자 확인"""
        issues = []

        for match in TRAILING_SPACE_PATTERN.finditer(content):
            identifier = match.group(0)
            issues.append(CompatibilityIssue(
                issue_type=IssueType.TRAILING_SPACE_NAME,
                severity="error",
                location=location,
                description=f"식별자 끝에 공백: {identifier}",
                suggestion="식별자 끝의 공백 제거 필요"
            ))

        return issues

    # ================================================================
    # S09: 제어 문자 식별자 검사 (덤프 파일)
    # ================================================================
    def check_control_char_names(self, content: str, location: str) -> List[CompatibilityIssue]:
        """식별자에 제어 문자 포함 확인"""
        issues = []

        for match in CONTROL_CHAR_PATTERN.finditer(content):
            identifier = match.group(0)
            issues.append(CompatibilityIssue(
                issue_type=IssueType.CONTROL_CHAR_NAME,
                severity="error",
                location=location,
                description=f"식별자에 제어 문자 포함: {repr(identifier)}",
                suggestion="식별자에서 제어 문자 제거 필요"
            ))

        return issues

    # ================================================================
    # S27: 스키마 생략 dot 구문 검사 (덤프 파일)
    # ================================================================
    def check_empty_dot_table_syntax(self, content: str, location: str) -> List[CompatibilityIssue]:
        """스키마 생략 dot 구문 (.tableName) 사용 확인"""
        issues = []

        for match in EMPTY_DOT_TABLE_SYNTAX_PATTERN.finditer(content):
            line = self._extract_source_line(content, match)

            issues.append(CompatibilityIssue(
                issue_type=IssueType.EMPTY_DOT_TABLE_SYNTAX,
                severity="error",
                location=location,
                description=f"스키마 생략 dot 구문 사용: {match.group(0).strip()}",
                suggestion="스키마명을 명시적으로 지정 (예: schema_name.table_name)",
                code_snippet=line[:100]
            ))

        return issues

    # ================================================================
    # S30: 루틴 이름이 예약어와 충돌 검사 (덤프 파일)
    # ================================================================
    def check_routine_syntax_keyword(self, content: str, location: str) -> List[CompatibilityIssue]:
        """저장 프로시저/함수/트리거/이벤트 이름이 MySQL 예약어와 충돌하는지 확인"""
        issues = []

        for match in ROUTINE_SYNTAX_KEYWORD_PATTERN.finditer(content):
            routine_name = match.group(1).upper()
            if routine_name in ALL_RESERVED_KEYWORDS:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ROUTINE_SYNTAX_KEYWORD,
                    severity="error",
                    location=location,
                    description=f"루틴 이름 '{routine_name}'이 MySQL 예약어와 충돌",
                    suggestion=f"루틴 이름을 예약어가 아닌 이름으로 변경 필요 (예약어: {routine_name})",
                    code_snippet=match.group(0)[:80]
                ))

        return issues

    # ================================================================
    # S31: 식별자에 연속 점(..) 사용 검사 (덤프 파일)
    # ================================================================
    def check_invalid_57_name_multiple_dots(self, content: str, location: str) -> List[CompatibilityIssue]:
        """식별자에 연속 점(..) 사용 확인 (schema..table 또는 ..table 형태)"""
        issues = []

        for match in INVALID_57_NAME_MULTIPLE_DOTS_PATTERN.finditer(content):
            identifier = match.group(0)
            issues.append(CompatibilityIssue(
                issue_type=IssueType.INVALID_57_NAME_MULTIPLE_DOTS,
                severity="error",
                location=location,
                description=f"식별자에 연속 점(..) 사용: {identifier}",
                suggestion="연속 점 구문 제거 및 올바른 스키마.테이블 형식 사용 (예: schema.table)"
            ))

        return issues
