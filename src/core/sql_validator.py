"""
SQL 구문 Validator
- 테이블/컬럼 존재 여부 검증
- DB 버전별 문법 호환성 체크
- 정규식 기반 파싱 (의존성 없음)
"""
import re
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Set, Optional, Tuple

from src.core import constants
from src.core.sql_identifier_utils import (
    ALIAS_STOP_WORDS, _normalize_identifier, _read_identifier, _skip_balanced_parentheses,
    extract_cte_names, extract_derived_table_aliases, extract_table_aliases,
)
from src.core.sql_metadata import (
    _schema_key, FUZZY_MATCH_CUTOFF, SchemaMetadata, SchemaMetadataProvider,
)
from src.core.sql_autocompleter import SQLAutoCompleter

# 하위호환 재수출 — sql_validator.py 분할(sql_identifier_utils/sql_metadata/sql_autocompleter) 이후에도
# 기존 소비자(src/core/__init__.py, src/ui/dialogs/sql_editor_dialog.py 등)가 old import path로
# 계속 동작하도록 유지한다. 재수출 목록 변경 시 tests/test_sql_validator.py의 identity 가드 테스트 확인.
__all__ = [
    'ALIAS_STOP_WORDS', '_normalize_identifier', '_read_identifier', '_skip_balanced_parentheses',
    'extract_cte_names', 'extract_derived_table_aliases', 'extract_table_aliases',
    '_schema_key', 'FUZZY_MATCH_CUTOFF', 'SchemaMetadata', 'SchemaMetadataProvider',
    'SQLAutoCompleter',
    'IssueSeverity', 'ValidationIssue', 'SQLValidator',
]


class IssueSeverity(Enum):
    """검증 이슈 심각도"""
    ERROR = "error"      # 빨간 밑줄
    WARNING = "warning"  # 노란 밑줄
    INFO = "info"        # 파란 밑줄


@dataclass
class ValidationIssue:
    """검증 결과 이슈"""
    line: int              # 줄 번호 (0-based)
    column: int            # 컬럼 위치 (0-based)
    end_column: int        # 끝 위치
    message: str           # 에러 메시지
    severity: IssueSeverity
    suggestions: List[str] = field(default_factory=list)  # 제안 목록

    @property
    def length(self) -> int:
        """이슈 범위 길이"""
        return self.end_column - self.column


class SQLValidator:
    """SQL 구문 검증기"""

    # MySQL 8.0+ 전용 키워드
    MYSQL8_KEYWORDS = {
        'LATERAL', 'CUME_DIST', 'DENSE_RANK', 'FIRST_VALUE', 'GROUPS',
        'JSON_TABLE', 'LAG', 'LAST_VALUE', 'LEAD', 'NTH_VALUE', 'NTILE',
        'OF', 'OVER', 'PERCENT_RANK', 'RANK', 'ROW_NUMBER', 'WINDOW'
    }

    # MySQL 8.0+ 전용 함수
    MYSQL8_FUNCTIONS = {
        'JSON_TABLE', 'JSON_OVERLAPS', 'JSON_SCHEMA_VALID', 'JSON_SCHEMA_VALIDATION_REPORT',
        'MEMBER OF', 'REGEXP_LIKE', 'REGEXP_INSTR', 'REGEXP_REPLACE', 'REGEXP_SUBSTR',
        'BIN_TO_UUID', 'UUID_TO_BIN', 'IS_UUID'
    }

    # 시스템 스키마 (검증 제외) - constants.SYSTEM_SCHEMAS(소문자)의 대문자 파생 (단일 소스화)
    # https://dev.mysql.com/doc/refman/8.0/en/system-schema.html
    SYSTEM_SCHEMAS = frozenset(s.upper() for s in constants.SYSTEM_SCHEMAS)

    # 테이블명 추출 패턴 (순서 중요: 더 구체적인 패턴 먼저)
    TABLE_PATTERNS = [
        # DELETE FROM table (FROM만 매칭되지 않도록 DELETE FROM을 먼저)
        (r'\bDELETE\s+FROM\s+(?:`?(\w+)`?\.)?`?(\w+)`?', 'DELETE'),
        # INSERT INTO table
        (r'\bINSERT\s+INTO\s+(?:`?(\w+)`?\.)?`?(\w+)`?', 'INSERT'),
        # TRUNCATE TABLE table
        (r'\bTRUNCATE\s+(?:TABLE\s+)?(?:`?(\w+)`?\.)?`?(\w+)`?', 'TRUNCATE'),
        # UPDATE table
        (r'\bUPDATE\s+(?:`?(\w+)`?\.)?`?(\w+)`?', 'UPDATE'),
        # JOIN table (LEFT/RIGHT/INNER/OUTER/CROSS JOIN)
        (r'\b(?:LEFT\s+|RIGHT\s+|INNER\s+|OUTER\s+|CROSS\s+)?JOIN\s+(?:`?(\w+)`?\.)?`?(\w+)`?', 'JOIN'),
        # FROM table (단독 FROM - DELETE FROM 이후의 위치는 제외)
        (r'(?<!\bDELETE\s)\bFROM\s+(?:`?(\w+)`?\.)?`?(\w+)`?', 'FROM'),
    ]

    def __init__(self, metadata_provider: SchemaMetadataProvider = None):
        self.metadata_provider = metadata_provider or SchemaMetadataProvider()

    def validate(self, sql: str, schema: str = None) -> List[ValidationIssue]:
        """SQL 검증 실행

        Args:
            sql: SQL 쿼리 문자열
            schema: 대상 스키마 (None이면 현재 DB)

        Returns:
            검증 이슈 목록
        """
        issues: List[ValidationIssue] = []
        metadata = self.metadata_provider.get_metadata(schema)

        if not metadata.tables:
            # 메타데이터 없으면 검증 스킵
            return issues

        # 줄 단위로 분리 (위치 계산용)
        lines = sql.split('\n')
        line_offsets = self._calculate_line_offsets(lines)

        # 1. 테이블명 검증
        table_issues = self._validate_tables(sql, metadata, line_offsets)
        issues.extend(table_issues)

        # 2. 컬럼명 검증
        column_issues = self._validate_columns(sql, metadata, line_offsets)
        issues.extend(column_issues)

        # 3. DB 버전 호환성 검증
        version_issues = self._validate_version_compatibility(sql, metadata, line_offsets)
        issues.extend(version_issues)

        return issues

    def _calculate_line_offsets(self, lines: List[str]) -> List[int]:
        """각 줄의 시작 오프셋 계산"""
        offsets = [0]
        for line in lines[:-1]:
            offsets.append(offsets[-1] + len(line) + 1)  # +1 for newline
        return offsets

    def _offset_to_line_col(self, offset: int, line_offsets: List[int]) -> Tuple[int, int]:
        """오프셋을 줄/컬럼으로 변환"""
        for i in range(len(line_offsets) - 1, -1, -1):
            if offset >= line_offsets[i]:
                return i, offset - line_offsets[i]
        return 0, offset

    def _validate_tables(self, sql: str, metadata: SchemaMetadata,
                         line_offsets: List[int]) -> List[ValidationIssue]:
        """테이블명 검증"""
        issues = []

        # 문자열 리터럴 및 주석 영역 찾기 (검증에서 제외)
        string_regions = self._find_string_regions(sql)
        comment_regions = self._find_comment_regions(sql)
        excluded_regions = string_regions + comment_regions

        # CTE 이름 / 파생 테이블(서브쿼리) 별칭은 실제 테이블이 아니므로 존재 검증에서 제외
        virtual_tables = extract_cte_names(sql) | extract_derived_table_aliases(sql)

        # 이미 검증한 위치 추적 (중복 방지)
        validated_positions = set()

        for pattern, pattern_type in self.TABLE_PATTERNS:
            for match in re.finditer(pattern, sql, re.IGNORECASE):
                # 원본 SQL에서 실제 테이블명 추출 (대소문자 보존)
                full_match_start = match.start()
                full_match_text = sql[match.start():match.end()]

                # 테이블명 위치 찾기
                table_match = re.search(r'(?:`?(\w+)`?\.)?`?(\w+)`?\s*$', full_match_text)
                if not table_match:
                    continue

                schema_name = table_match.group(1)  # 스키마명 (없으면 None)
                table_name = table_match.group(2)

                # 시스템 스키마면 검증 건너뛰기 (INFORMATION_SCHEMA, mysql 등)
                if schema_name and schema_name.upper() in self.SYSTEM_SCHEMAS:
                    continue

                table_start = full_match_start + table_match.start(2)

                # 이미 검증한 위치인지 확인 (중복 방지)
                if table_start in validated_positions:
                    continue
                validated_positions.add(table_start)

                # 문자열/주석 내부인지 확인
                if self._is_in_regions(table_start, excluded_regions):
                    continue

                # CTE/파생 테이블 별칭이면 존재하지 않는 테이블로 오탐하지 않도록 스킵
                if table_name.lower() in virtual_tables:
                    continue

                # 테이블 존재 여부 확인
                if not metadata.has_table(table_name):
                    line, col = self._offset_to_line_col(table_start, line_offsets)
                    suggestions = metadata.get_similar_tables(table_name)

                    issues.append(ValidationIssue(
                        line=line,
                        column=col,
                        end_column=col + len(table_name),
                        message=f"테이블 '{table_name}' 이(가) 존재하지 않습니다",
                        severity=IssueSeverity.ERROR,
                        suggestions=suggestions
                    ))

        return issues

    def _validate_columns(self, sql: str, metadata: SchemaMetadata,
                          line_offsets: List[int]) -> List[ValidationIssue]:
        """컬럼명 검증"""
        issues = []

        # 문자열 리터럴 영역
        string_regions = self._find_string_regions(sql)

        # FROM 절에서 테이블/별칭 매핑 추출 (Validator/AutoCompleter 공용 파서)
        table_aliases = extract_table_aliases(sql, metadata)

        # table.column 패턴 검증
        column_pattern = r'`?(\w+)`?\s*\.\s*`?(\w+)`?'

        for match in re.finditer(column_pattern, sql, re.IGNORECASE):
            prefix = match.group(1)  # 테이블명 또는 별칭
            column = match.group(2)

            # 문자열 내부 체크
            if self._is_in_string(match.start(), string_regions):
                continue

            # 별칭 → 실제 테이블명 변환
            table_name = table_aliases.get(prefix.lower(), prefix)

            # 테이블이 존재하는지 먼저 확인
            if not metadata.has_table(table_name):
                continue  # 테이블 검증은 별도로 처리됨

            # 컬럼 존재 여부 확인
            if not metadata.has_column(table_name, column):
                col_start = match.start(2)
                line, col_pos = self._offset_to_line_col(col_start, line_offsets)
                suggestions = metadata.get_similar_columns(table_name, column)

                issues.append(ValidationIssue(
                    line=line,
                    column=col_pos,
                    end_column=col_pos + len(column),
                    message=f"컬럼 '{column}'이(가) 테이블 '{table_name}'에 존재하지 않습니다",
                    severity=IssueSeverity.WARNING,
                    suggestions=suggestions
                ))

        return issues

    def _flag_unsupported_items(self, sql: str, items: Set[str], pattern_template: str,
                                message_template: str, string_regions: List[Tuple[int, int]],
                                line_offsets: List[int], major: int, minor: int) -> List[ValidationIssue]:
        """MYSQL8_KEYWORDS/MYSQL8_FUNCTIONS 스캔 공통 로직

        Args:
            items: 스캔할 키워드 또는 함수명 집합
            pattern_template: '{item}' 플레이스홀더를 포함하는 정규식 템플릿
            message_template: '{item}', '{major}', '{minor}' 플레이스홀더를 포함하는 메시지 템플릿
        """
        issues = []
        for item in items:
            pattern = pattern_template.format(item=item)
            for match in re.finditer(pattern, sql, re.IGNORECASE):
                if self._is_in_string(match.start(), string_regions):
                    continue

                line, col = self._offset_to_line_col(match.start(), line_offsets)
                issues.append(ValidationIssue(
                    line=line,
                    column=col,
                    end_column=col + len(item),
                    message=message_template.format(item=item, major=major, minor=minor),
                    severity=IssueSeverity.WARNING,
                    suggestions=[]
                ))
        return issues

    def _validate_version_compatibility(self, sql: str, metadata: SchemaMetadata,
                                        line_offsets: List[int]) -> List[ValidationIssue]:
        """DB 버전 호환성 검증"""
        issues = []
        major, minor, _ = metadata.db_version

        # MySQL 5.x에서 8.0+ 기능 사용 체크
        if major > 0 and major < 8:
            string_regions = self._find_string_regions(sql)

            # 키워드 체크
            issues.extend(self._flag_unsupported_items(
                sql, self.MYSQL8_KEYWORDS, r'\b{item}\b',
                "'{item}'은(는) MySQL 8.0 이상에서만 지원됩니다 (현재: {major}.{minor})",
                string_regions, line_offsets, major, minor
            ))

            # 함수 체크
            issues.extend(self._flag_unsupported_items(
                sql, self.MYSQL8_FUNCTIONS, r'\b{item}\s*\(',
                "함수 '{item}'은(는) MySQL 8.0 이상에서만 지원됩니다 (현재: {major}.{minor})",
                string_regions, line_offsets, major, minor
            ))

        return issues

    def _find_string_regions(self, sql: str) -> List[Tuple[int, int]]:
        """문자열 리터럴 영역 찾기 (시작, 끝)"""
        regions = []
        in_string = False
        string_char = None
        start = 0

        i = 0
        while i < len(sql):
            char = sql[i]

            if not in_string:
                if char in ("'", '"'):
                    in_string = True
                    string_char = char
                    start = i
            else:
                if char == string_char:
                    # 이스케이프 체크 (\' 또는 '')
                    if i + 1 < len(sql) and sql[i + 1] == string_char:
                        i += 1  # 이스케이프된 따옴표 스킵
                    else:
                        regions.append((start, i + 1))
                        in_string = False
                        string_char = None

            i += 1

        return regions

    def _is_in_string(self, pos: int, string_regions: List[Tuple[int, int]]) -> bool:
        """위치가 문자열 내부인지 확인"""
        for start, end in string_regions:
            if start <= pos < end:
                return True
        return False

    def _find_comment_regions(self, sql: str) -> List[Tuple[int, int]]:
        """주석 영역 찾기 (시작, 끝)"""
        regions = []

        # 단일 행 주석: -- 또는 #
        for match in re.finditer(r'(--|#)[^\n]*', sql):
            regions.append((match.start(), match.end()))

        # 멀티라인 주석: /* */
        for match in re.finditer(r'/\*.*?\*/', sql, re.DOTALL):
            regions.append((match.start(), match.end()))

        return regions

    def _is_in_regions(self, pos: int, regions: List[Tuple[int, int]]) -> bool:
        """위치가 특정 영역들 내부인지 확인"""
        for start, end in regions:
            if start <= pos < end:
                return True
        return False
