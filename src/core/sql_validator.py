"""
SQL 구문 Validator
- 테이블/컬럼 존재 여부 검증
- DB 버전별 문법 호환성 체크
- 정규식 기반 파싱 (의존성 없음)
"""
import re
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Tuple
from difflib import get_close_matches


# 별칭/CTE 파싱에서 공통으로 사용하는 예약어 스킵 목록
ALIAS_STOP_WORDS = {
    'WHERE', 'ON', 'AND', 'OR', 'SET', 'VALUES',
    'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'FULL',
    'JOIN', 'ORDER', 'GROUP', 'HAVING', 'LIMIT', 'OFFSET',
    'UNION', 'ALL', 'SELECT', 'FROM', 'BY', 'AS',
}


def _schema_key(schema: Optional[str]) -> Optional[str]:
    """스키마명을 캐시 키로 정규화 (None/빈 문자열/공백만 있으면 None)"""
    if schema is None:
        return None
    stripped = schema.strip()
    return stripped or None


def _normalize_identifier(identifier: str) -> str:
    """식별자를 감싸는 백틱 한 겹 제거 (없으면 그대로 반환)"""
    if len(identifier) >= 2 and identifier[0] == '`' and identifier[-1] == '`':
        return identifier[1:-1]
    return identifier


def _read_identifier(sql: str, pos: int) -> Tuple[Optional[str], int]:
    """pos 위치(앞쪽 공백 허용)부터 식별자 하나를 읽는다.

    백틱 식별자(`name`)와 일반 \\w+ 식별자를 모두 지원한다.

    Returns:
        (식별자, 다음 위치) 또는 식별자를 읽지 못하면 (None, pos)
    """
    length = len(sql)
    i = pos
    while i < length and sql[i].isspace():
        i += 1

    if i >= length:
        return None, pos

    if sql[i] == '`':
        end = sql.find('`', i + 1)
        if end == -1:
            return None, pos
        return sql[i + 1:end], end + 1

    match = re.match(r'\w+', sql[i:])
    if not match:
        return None, pos
    return match.group(0), i + match.end()


def _skip_balanced_parentheses(sql: str, open_pos: int) -> int:
    """open_pos가 가리키는 '('과 짝이 맞는 ')' 바로 다음 위치를 반환한다.

    문자열 리터럴(작은따옴표/큰따옴표) 내부의 괄호는 무시하며,
    이스케이프 처리는 `_find_string_regions`와 동일하게 따옴표 2개 연속을 이스케이프로 본다.
    짝이 맞지 않으면 len(sql)을 반환한다.
    """
    length = len(sql)
    if open_pos >= length or sql[open_pos] != '(':
        return open_pos

    depth = 0
    in_string = False
    string_char = None
    i = open_pos

    while i < length:
        char = sql[i]

        if in_string:
            if char == string_char:
                if i + 1 < length and sql[i + 1] == string_char:
                    i += 1  # 이스케이프된 따옴표 스킵
                else:
                    in_string = False
                    string_char = None
        else:
            if char in ("'", '"'):
                in_string = True
                string_char = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return i + 1

        i += 1

    return length


def extract_cte_names(sql: str) -> Set[str]:
    """WITH 절에 정의된 CTE 이름 목록 추출 (테이블 존재 검증 제외용)

    지원 형태:
        WITH cte AS (...) SELECT * FROM cte
        WITH a AS (...), b AS (...) SELECT * FROM b
        WITH RECURSIVE cte AS (...) SELECT * FROM cte
        WITH `cte` AS (...) SELECT * FROM `cte`
    """
    names: Set[str] = set()

    with_match = re.search(r'\bWITH\b', sql, re.IGNORECASE)
    if not with_match:
        return names

    length = len(sql)
    pos = with_match.end()

    recursive_match = re.match(r'\s+RECURSIVE\b', sql[pos:], re.IGNORECASE)
    if recursive_match:
        pos += recursive_match.end()

    while True:
        name, pos = _read_identifier(sql, pos)
        if not name:
            break
        names.add(_normalize_identifier(name).lower())

        while pos < length and sql[pos].isspace():
            pos += 1

        # 선택적 컬럼 목록: cte(col1, col2)
        if pos < length and sql[pos] == '(':
            pos = _skip_balanced_parentheses(sql, pos)
            while pos < length and sql[pos].isspace():
                pos += 1

        as_match = re.match(r'AS\b', sql[pos:], re.IGNORECASE)
        if not as_match:
            break
        pos += as_match.end()

        while pos < length and sql[pos].isspace():
            pos += 1

        if pos >= length or sql[pos] != '(':
            break
        pos = _skip_balanced_parentheses(sql, pos)

        while pos < length and sql[pos].isspace():
            pos += 1

        if pos < length and sql[pos] == ',':
            pos += 1
            continue
        break

    return names


def extract_derived_table_aliases(sql: str) -> Set[str]:
    """FROM (...) AS alias / JOIN (...) AS alias 형태의 파생 테이블 별칭 추출

    파생 테이블 별칭은 실제 테이블이 아니므로, 테이블 존재 검증에서 제외하기 위한
    스킵 목록으로만 사용한다 (메타데이터 조회용이 아님).
    """
    aliases: Set[str] = set()
    length = len(sql)

    for match in re.finditer(r'\b(?:FROM|JOIN)\s*\(', sql, re.IGNORECASE):
        open_pos = match.end() - 1
        pos = _skip_balanced_parentheses(sql, open_pos)

        while pos < length and sql[pos].isspace():
            pos += 1

        as_match = re.match(r'AS\b', sql[pos:], re.IGNORECASE)
        if as_match:
            pos += as_match.end()

        alias, _ = _read_identifier(sql, pos)
        if not alias:
            continue

        normalized = _normalize_identifier(alias)
        if normalized.upper() not in ALIAS_STOP_WORDS:
            aliases.add(normalized.lower())

    return aliases


def extract_table_aliases(sql: str, metadata: 'SchemaMetadata') -> Dict[str, str]:
    """FROM/JOIN 절의 실제 테이블 참조에서 별칭(별칭 → 테이블명) 추출

    CTE 이름과 파생 테이블(서브쿼리)은 실제 테이블이 아니므로 별칭 매핑에서 제외한다.
    Validator와 AutoCompleter가 공용으로 사용하는 단일 파서다.
    """
    aliases: Dict[str, str] = {}
    cte_names = extract_cte_names(sql)

    # FROM/JOIN 뒤가 '('인 경우(파생 테이블)는 이 패턴에서 제외
    pattern = r'\b(?:FROM|JOIN)\s+(?!\()(?:`?(\w+)`?\.)?`?(\w+)`?(?:\s+(?:AS\s+)?`?(\w+)`?)?'

    for match in re.finditer(pattern, sql, re.IGNORECASE):
        table = match.group(2)
        alias = match.group(3)

        # CTE 이름이면서 실제 메타데이터 테이블이 아니면 별칭 소스로 사용하지 않음
        if table.lower() in cte_names and not metadata.has_table(table):
            continue

        if alias and alias.upper() in ALIAS_STOP_WORDS:
            alias = None

        if alias:
            aliases[alias.lower()] = table

    # 테이블 자체도 추가 (self-reference)
    for real_table in metadata.tables:
        aliases[real_table.lower()] = real_table

    return aliases


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


@dataclass
class SchemaMetadata:
    """스키마 메타데이터"""
    tables: Set[str] = field(default_factory=set)
    columns: Dict[str, Set[str]] = field(default_factory=dict)  # table -> columns
    db_version: Tuple[int, int, int] = (0, 0, 0)

    def has_table(self, table: str) -> bool:
        """테이블 존재 여부 (대소문자 무시)"""
        return table.lower() in {t.lower() for t in self.tables}

    def get_table_name(self, table: str) -> Optional[str]:
        """실제 테이블명 반환 (대소문자 매칭)"""
        table_lower = table.lower()
        for t in self.tables:
            if t.lower() == table_lower:
                return t
        return None

    def has_column(self, table: str, column: str) -> bool:
        """컬럼 존재 여부 (대소문자 무시)"""
        real_table = self.get_table_name(table)
        if not real_table or real_table not in self.columns:
            return False
        return column.lower() in {c.lower() for c in self.columns[real_table]}

    def get_column_name(self, table: str, column: str) -> Optional[str]:
        """실제 컬럼명 반환"""
        real_table = self.get_table_name(table)
        if not real_table or real_table not in self.columns:
            return None
        col_lower = column.lower()
        for c in self.columns[real_table]:
            if c.lower() == col_lower:
                return c
        return None

    def get_similar_tables(self, table: str, n: int = 3) -> List[str]:
        """유사한 테이블명 제안"""
        return get_close_matches(table.lower(), [t.lower() for t in self.tables], n=n, cutoff=0.5)

    def get_similar_columns(self, table: str, column: str, n: int = 3) -> List[str]:
        """유사한 컬럼명 제안"""
        real_table = self.get_table_name(table)
        if not real_table or real_table not in self.columns:
            return []
        return get_close_matches(column.lower(), [c.lower() for c in self.columns[real_table]], n=n, cutoff=0.5)


class SchemaMetadataProvider:
    """스키마 메타데이터 제공자 (스키마별 인메모리 캐시)

    Python 쪽에서는 동기 DB 조회를 하지 않는다. 메타데이터는 반드시
    `set_metadata()` (또는 호환용 `_metadata` 직접 대입)로 채워져야 하며,
    캐시 미스 시에는 커넥터를 호출하지 않고 빈 SchemaMetadata를 반환한다.
    이는 ValidationWorker가 MetadataLoadWorker와 같은 커넥터를 두고
    경쟁(race)하는 것을 방지하기 위함이다.
    """

    def __init__(self):
        self._metadata_by_schema: Dict[Optional[str], SchemaMetadata] = {}
        self._active_schema_key: Optional[str] = None
        self._connector = None
        self._lock = threading.RLock()

    @property
    def _metadata(self) -> Optional[SchemaMetadata]:
        """호환용 속성 (UI 등 외부에서 `_metadata`를 직접 읽는 경우 대응)"""
        with self._lock:
            if self._active_schema_key in self._metadata_by_schema:
                return self._metadata_by_schema[self._active_schema_key]
            return self._metadata_by_schema.get(None)

    @_metadata.setter
    def _metadata(self, value: Optional[SchemaMetadata]):
        """호환용 속성 (UI 등 외부에서 `_metadata`를 직접 대입하는 경우 대응)

        `set_connector(connector)` 직후 활성 스키마(`connector.database`)에
        매핑해 저장한다. 신규 코드는 `set_metadata(schema, metadata)`를 사용할 것.
        """
        with self._lock:
            if value is None:
                if self._active_schema_key is not None:
                    self._metadata_by_schema.pop(self._active_schema_key, None)
                else:
                    self._metadata_by_schema.clear()
                return
            self._metadata_by_schema[self._active_schema_key] = value

    def set_connector(self, connector):
        """DB 커넥터 설정

        커넥터가 바뀌면 이전 캐시가 다른 연결의 것일 수 있으므로 무효화한다.
        여기서는 DB에 동기 조회를 하지 않는다.
        """
        with self._lock:
            self._connector = connector
            self._active_schema_key = _schema_key(getattr(connector, "database", None))
            self._metadata_by_schema.clear()

    def set_metadata(self, schema: str, metadata: SchemaMetadata):
        """스키마에 대한 메타데이터를 캐시에 저장 (백그라운드 로드 완료 후 호출)"""
        if metadata is None:
            raise ValueError("metadata는 None일 수 없습니다")

        key = _schema_key(schema)
        with self._lock:
            self._metadata_by_schema[key] = metadata
            self._active_schema_key = key

    def get_metadata(self, schema: str = None) -> SchemaMetadata:
        """메타데이터 조회 (캐시 히트만 반환, 캐시 미스 시 커넥터 조회하지 않음)"""
        key = _schema_key(schema)
        with self._lock:
            if key in self._metadata_by_schema:
                return self._metadata_by_schema[key]
            if key is None and None in self._metadata_by_schema:
                return self._metadata_by_schema[None]
            return SchemaMetadata()

    def invalidate(self, schema: str = None):
        """캐시 무효화

        Args:
            schema: 지정하면 해당 스키마만 무효화, None이면 전체 무효화
        """
        with self._lock:
            if schema is None:
                self._metadata_by_schema.clear()
            else:
                self._metadata_by_schema.pop(_schema_key(schema), None)


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

    # 시스템 스키마 (검증 제외) - 대문자로 저장, 비교 시 upper()
    # https://dev.mysql.com/doc/refman/8.0/en/system-schema.html
    SYSTEM_SCHEMAS = {
        'INFORMATION_SCHEMA',  # 메타데이터
        'MYSQL',               # 시스템 테이블 (권한, 설정 등)
        'PERFORMANCE_SCHEMA',  # 성능 모니터링
        'SYS',                 # Performance Schema 해석용 (5.7+)
        'NDBINFO',             # NDB Cluster 정보 (NDB Cluster 전용)
    }

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

    def _validate_version_compatibility(self, sql: str, metadata: SchemaMetadata,
                                        line_offsets: List[int]) -> List[ValidationIssue]:
        """DB 버전 호환성 검증"""
        issues = []
        major, minor, _ = metadata.db_version

        # MySQL 5.x에서 8.0+ 기능 사용 체크
        if major > 0 and major < 8:
            string_regions = self._find_string_regions(sql)

            # 키워드 체크
            for keyword in self.MYSQL8_KEYWORDS:
                pattern = rf'\b{keyword}\b'
                for match in re.finditer(pattern, sql, re.IGNORECASE):
                    if self._is_in_string(match.start(), string_regions):
                        continue

                    line, col = self._offset_to_line_col(match.start(), line_offsets)
                    issues.append(ValidationIssue(
                        line=line,
                        column=col,
                        end_column=col + len(keyword),
                        message=f"'{keyword}'은(는) MySQL 8.0 이상에서만 지원됩니다 (현재: {major}.{minor})",
                        severity=IssueSeverity.WARNING,
                        suggestions=[]
                    ))

            # 함수 체크
            for func in self.MYSQL8_FUNCTIONS:
                pattern = rf'\b{func}\s*\('
                for match in re.finditer(pattern, sql, re.IGNORECASE):
                    if self._is_in_string(match.start(), string_regions):
                        continue

                    line, col = self._offset_to_line_col(match.start(), line_offsets)
                    issues.append(ValidationIssue(
                        line=line,
                        column=col,
                        end_column=col + len(func),
                        message=f"함수 '{func}'은(는) MySQL 8.0 이상에서만 지원됩니다 (현재: {major}.{minor})",
                        severity=IssueSeverity.WARNING,
                        suggestions=[]
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


class SQLAutoCompleter:
    """SQL 자동완성 제공자"""

    SQL_KEYWORDS = [
        'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'LIKE', 'BETWEEN',
        'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
        'CREATE', 'ALTER', 'DROP', 'TABLE', 'INDEX', 'VIEW', 'DATABASE',
        'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER', 'FULL', 'CROSS', 'ON',
        'GROUP', 'BY', 'ORDER', 'ASC', 'DESC', 'HAVING', 'LIMIT', 'OFFSET',
        'UNION', 'ALL', 'DISTINCT', 'AS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
        'NULL', 'IS', 'EXISTS', 'PRIMARY', 'KEY', 'FOREIGN', 'REFERENCES',
        'CONSTRAINT', 'DEFAULT', 'AUTO_INCREMENT', 'TRUNCATE',
        'BEGIN', 'COMMIT', 'ROLLBACK', 'TRANSACTION',
    ]

    SQL_FUNCTIONS = [
        'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COALESCE', 'IFNULL', 'NULLIF',
        'CONCAT', 'SUBSTRING', 'LENGTH', 'TRIM', 'UPPER', 'LOWER', 'REPLACE',
        'NOW', 'DATE', 'TIME', 'DATETIME', 'TIMESTAMP', 'YEAR', 'MONTH', 'DAY',
        'HOUR', 'MINUTE', 'SECOND', 'DATEDIFF', 'DATE_ADD', 'DATE_SUB',
        'CAST', 'CONVERT', 'ROUND', 'FLOOR', 'CEIL', 'ABS', 'MOD', 'POWER',
        'GROUP_CONCAT', 'JSON_EXTRACT', 'JSON_ARRAY', 'JSON_OBJECT',
    ]

    def __init__(self, metadata_provider: SchemaMetadataProvider = None):
        self.metadata_provider = metadata_provider or SchemaMetadataProvider()

    def get_completions(self, sql: str, cursor_pos: int, schema: str = None) -> List[Dict]:
        """커서 위치에서 자동완성 목록 반환

        Args:
            sql: SQL 쿼리 문자열
            cursor_pos: 커서 위치
            schema: 대상 스키마

        Returns:
            자동완성 항목 목록 [{label, type, detail}, ...]
        """
        completions = []
        metadata = self.metadata_provider.get_metadata(schema)

        # 커서 앞 텍스트 분석
        text_before = sql[:cursor_pos]
        context = self._analyze_context(text_before)
        prefix = self._get_current_word(text_before)

        if context['type'] == 'table':
            # FROM/JOIN 뒤 → 테이블 목록
            for table in sorted(metadata.tables):
                if self._matches_prefix(table, prefix):
                    completions.append({
                        'label': table,
                        'type': 'table',
                        'detail': '테이블'
                    })

        elif context['type'] == 'column':
            # SELECT/WHERE 뒤 또는 table. 뒤 → 컬럼 목록
            target_table = context.get('table')

            if target_table:
                # 특정 테이블의 컬럼 (별칭 → 실제 테이블명 변환은 조회 전에 수행)
                aliases = extract_table_aliases(sql, metadata)
                resolved_table = aliases.get(target_table.lower(), target_table)
                real_table = metadata.get_table_name(resolved_table)
                if real_table and real_table in metadata.columns:
                    for col in sorted(metadata.columns[real_table]):
                        if self._matches_prefix(col, prefix):
                            completions.append({
                                'label': col,
                                'type': 'column',
                                'detail': f'{real_table} 컬럼'
                            })
            else:
                # FROM 절의 모든 테이블 컬럼
                from_tables = self._extract_from_tables(sql, metadata)
                for table in from_tables:
                    if table in metadata.columns:
                        for col in sorted(metadata.columns[table]):
                            if self._matches_prefix(col, prefix):
                                completions.append({
                                    'label': col,
                                    'type': 'column',
                                    'detail': f'{table}'
                                })

        # table. 뒤가 아닌 경우에만 키워드/함수 추가
        # (table. 뒤에서는 해당 테이블 컬럼만 제안)
        if not context.get('table'):
            # 키워드 추가 (keyword 또는 column 컨텍스트)
            if context['type'] in ('keyword', 'column'):
                for kw in self.SQL_KEYWORDS:
                    if self._matches_prefix(kw, prefix):
                        completions.append({
                            'label': kw,
                            'type': 'keyword',
                            'detail': 'SQL 키워드'
                        })

            # 함수 추가 (keyword 또는 column 컨텍스트)
            if context['type'] in ('keyword', 'column'):
                for func in self.SQL_FUNCTIONS:
                    if self._matches_prefix(func, prefix):
                        completions.append({
                            'label': f'{func}()',
                            'type': 'function',
                            'detail': 'SQL 함수'
                        })

        return completions

    def _analyze_context(self, text_before: str) -> Dict:
        """커서 앞 컨텍스트 분석"""
        text_upper = text_before.upper()

        # FROM schema. / JOIN schema. 뒤에서는 schema-qualified table 이름을 제안
        if re.search(r'\b(FROM|JOIN)\s+`?\w+`?\.\w*$', text_upper):
            return {'type': 'table'}

        if re.search(r'\b(LEFT|RIGHT|INNER|OUTER|CROSS)\s+JOIN\s+`?\w+`?\.\w*$', text_upper):
            return {'type': 'table'}

        # table. 뒤인지 확인 (table.col 입력 중)
        dot_match = re.search(r'`?(\w+)`?\.\w*$', text_before)
        if dot_match:
            return {'type': 'column', 'table': dot_match.group(1)}

        # FROM/JOIN 뒤인지 확인 (FROM table 또는 FROM 직후)
        # \w*$로 현재 입력 중인 단어까지 포함
        if re.search(r'\b(FROM|JOIN)\s+\w*$', text_upper):
            return {'type': 'table'}

        # LEFT/RIGHT/INNER/OUTER/CROSS JOIN 뒤인지 확인
        if re.search(r'\b(LEFT|RIGHT|INNER|OUTER|CROSS)\s+JOIN\s+\w*$', text_upper):
            return {'type': 'table'}

        # SELECT/WHERE/ORDER BY 등 뒤인지 확인
        if re.search(r'\b(SELECT|WHERE|AND|OR|ORDER\s+BY|GROUP\s+BY|HAVING|SET)\s+\w*$', text_upper):
            return {'type': 'column'}

        # 기본값: 키워드
        return {'type': 'keyword'}

    def _get_current_word(self, text: str) -> str:
        """현재 입력 중인 단어 추출"""
        match = re.search(r'(\w*)$', text)
        return match.group(1) if match else ''

    def _matches_prefix(self, item: str, prefix: str) -> bool:
        """접두사 매칭 (대소문자 무시)"""
        if not prefix:
            return True
        return item.lower().startswith(prefix.lower())

    def _extract_from_tables(self, sql: str, metadata: SchemaMetadata) -> List[str]:
        """FROM 절에서 테이블 추출 (CTE 이름 / 파생 테이블 별칭은 제외)"""
        tables = []
        virtual_tables = extract_cte_names(sql) | extract_derived_table_aliases(sql)
        pattern = r'\b(?:FROM|JOIN)\s+(?:`?(\w+)`?\.)?`?(\w+)`?'

        for match in re.finditer(pattern, sql, re.IGNORECASE):
            table = match.group(2)
            if table.lower() in virtual_tables:
                continue
            real_table = metadata.get_table_name(table)
            if real_table and real_table not in tables:
                tables.append(real_table)

        return tables
