"""
데이터 무결성 규칙 모듈

MySQL 8.0 → 8.4 업그레이드 시 데이터 무결성 관련 호환성 검사 규칙.
13개 규칙 구현:
- D01: ENUM 빈 값 정의
- D02: ENUM 빈 값 INSERT
- D03: ENUM 숫자 인덱스
- D04: ENUM 요소 길이 초과
- D05: SET 요소 길이 초과
- D06: 4바이트 UTF-8 문자 감지
- D07: NULL 바이트 감지
- D08: TIMESTAMP 범위 초과
- D09: latin1 비ASCII 데이터
- D10: ZEROFILL 데이터 의존성
- D11: 잘못된 DATETIME (기존 확장)
- D12: FK 비고유 참조 (2-Pass)
- D13: FK 참조 테이블 미존재 (2-Pass)
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Callable, TYPE_CHECKING

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    IDENTIFIER_LIMITS,
    ENUM_EMPTY_PATTERN,
    INVALID_DATE_PATTERN,
    INVALID_DATETIME_PATTERN,
    INVALID_DATE_VALUES_PATTERN,
    TIMESTAMP_PATTERN,
)
from ..migration_parsers import CreateTableParser

if TYPE_CHECKING:
    from ..db_connector import MySQLConnector


class DataIntegrityRules:
    """데이터 무결성 규칙 모음"""

    def __init__(self, connector: Optional['MySQLConnector'] = None):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    # ================================================================
    # D01: ENUM 빈 값 정의 검사 (라이브 DB)
    # ================================================================
    def check_enum_empty_value_definition(self, schema: str) -> List[CompatibilityIssue]:
        """ENUM 정의에 빈 문자열('') 포함 여부 확인"""
        if not self.connector:
            return []

        self._log("🔍 ENUM 빈 값 정의 검사 중...")
        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'enum'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            # COLUMN_TYPE의 ENUM 요소를 파싱하여 실제 빈 문자열 요소만 확인
            # (이스케이프된 작은따옴표('')를 단순 부분 문자열로 찾으면
            #  enum('don''t','other') 같은 정상 값을 오탐하게 된다)
            column_type = col.get('COLUMN_TYPE', '')
            elements = self._extract_enum_elements(column_type)
            if any(element == "" for element in elements):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ENUM_EMPTY_VALUE,
                    severity="error",
                    location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                    description=f"ENUM에 빈 문자열('') 정의됨: {column_type[:80]}...",
                    suggestion="빈 문자열 대신 유효한 값 사용 또는 NULL 허용으로 변경",
                    table_name=col['TABLE_NAME'],
                    column_name=col['COLUMN_NAME']
                ))

        if issues:
            self._log(f"  ⚠️ ENUM 빈 값 정의 {len(issues)}개 발견")
        else:
            self._log("  ✅ ENUM 빈 값 정의 없음")

        return issues

    # ================================================================
    # D01: ENUM 빈 값 정의 검사 (덤프 파일)
    # ================================================================
    def check_enum_empty_in_sql(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일에서 ENUM 빈 값 정의 확인"""
        issues = []

        for match in ENUM_EMPTY_PATTERN.finditer(content):
            # 라인 추출
            line_start = content.rfind('\n', 0, match.start()) + 1
            line_end = content.find('\n', match.end())
            line = content[line_start:line_end].strip()

            issues.append(CompatibilityIssue(
                issue_type=IssueType.ENUM_EMPTY_VALUE,
                severity="error",
                location=location,
                description=f"ENUM에 빈 문자열('') 정의: {line[:60]}...",
                suggestion="빈 문자열 대신 유효한 값 사용"
            ))

        return issues

    # ================================================================
    # D02: ENUM 빈 값 INSERT 검사
    # ================================================================
    def check_enum_empty_insert(self, content: str, location: str) -> List[CompatibilityIssue]:
        """INSERT 문에서 ENUM 컬럼에 빈 문자열 삽입 확인"""
        issues = []

        # INSERT ... VALUES ('', ...) 패턴
        # 단순화된 검사 - VALUES 절에서 빈 문자열 찾기
        insert_pattern = re.compile(
            r"INSERT\s+INTO\s+`?(\w+)`?.*?VALUES\s*\([^)]*''\s*[,)]",
            re.IGNORECASE | re.DOTALL
        )

        for match in insert_pattern.finditer(content):
            table_name = match.group(1)
            issues.append(CompatibilityIssue(
                issue_type=IssueType.ENUM_EMPTY_VALUE,
                severity="warning",
                location=location,
                description=f"INSERT에 빈 문자열 삽입 (ENUM 컬럼일 경우 문제): {table_name}",
                suggestion="ENUM 컬럼에 빈 문자열 삽입 시 오류 발생 가능, 유효한 값 사용"
            ))

        return issues

    # ================================================================
    # D03: ENUM 숫자 인덱스 사용 검사
    # ================================================================
    # ENUM 컬럼 정의 패턴: `col_name` enum('a','b','c')
    _ENUM_COL_PATTERN = re.compile(
        r'`(\w+)`\s+enum\s*\(([^)]+)\)',
        re.IGNORECASE
    )
    # INSERT 문 패턴: INSERT INTO `table` (cols) VALUES (vals)
    _INSERT_PATTERN = re.compile(
        r'INSERT\s+INTO\s+`?(\w+)`?\s*\(([^)]+)\)\s*VALUES\s*',
        re.IGNORECASE
    )
    # VALUES 행 패턴
    _VALUES_ROW_PATTERN = re.compile(r'\(([^)]+)\)')

    def _find_statement_end(self, content: str, start: int) -> int:
        """start 이후 문자열/식별자 밖의 첫 세미콜론 위치(문장 경계)를 찾는다

        작은따옴표, 큰따옴표, 백틱 안의 세미콜론은 무시하며, 백슬래시
        이스케이프와 이중 작은따옴표('')로 이스케이프된 따옴표도 처리한다.
        종료 세미콜론이 없으면 len(content)를 반환한다.
        """
        in_single = False
        in_double = False
        in_backtick = False
        i = start
        n = len(content)
        while i < n:
            ch = content[i]
            if in_single:
                if ch == '\\':
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and content[i + 1] == "'":
                        i += 2
                        continue
                    in_single = False
                i += 1
                continue
            if in_double:
                if ch == '\\':
                    i += 2
                    continue
                if ch == '"':
                    in_double = False
                i += 1
                continue
            if in_backtick:
                if ch == '`':
                    in_backtick = False
                i += 1
                continue
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == '`':
                in_backtick = True
            elif ch == ';':
                return i
            i += 1
        return n

    def _iter_create_table_statements(self, content: str):
        """content에서 CREATE TABLE 문 전체 텍스트를 하나씩 생성한다

        CreateTableParser.TABLE_NAME_PATTERN으로 문장 시작 위치를 찾고
        _find_statement_end로 종료 세미콜론까지 경계를 잡는다. 이렇게 얻은
        완전한 문장을 CreateTableParser.parse()에 넘기면, 파서의 괄호
        균형 기반 바디 추출(_extract_body)이 varchar(255) DEFAULT ... 같은
        중첩 괄호에서도 CREATE TABLE 바디를 올바르게 잘라낸다.
        """
        for table_match in CreateTableParser.TABLE_NAME_PATTERN.finditer(content):
            start = table_match.start()
            end = self._find_statement_end(content, start)
            yield content[start:end]

    def _iter_values_rows(self, values_segment: str):
        """VALUES (...) 세그먼트에서 최상위 행 바디만 추출한다

        따옴표 상태와 괄호 중첩(깊이)을 함께 추적하여, 문자열 리터럴 안의
        괄호나 함수 호출의 중첩 괄호가 행 경계로 오인되지 않도록 한다.
        """
        depth = 0
        in_quote = False
        buf: list = []
        i = 0
        n = len(values_segment)
        while i < n:
            ch = values_segment[i]

            if in_quote:
                if ch == '\\' and i + 1 < n:
                    if depth >= 1:
                        buf.append(values_segment[i:i + 2])
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and values_segment[i + 1] == "'":
                        if depth >= 1:
                            buf.append("''")
                        i += 2
                        continue
                    in_quote = False
                if depth >= 1:
                    buf.append(ch)
                i += 1
                continue

            if ch == "'":
                in_quote = True
                if depth >= 1:
                    buf.append(ch)
                i += 1
                continue

            if ch == '(':
                if depth == 0:
                    buf = []
                else:
                    buf.append(ch)
                depth += 1
                i += 1
                continue

            if ch == ')':
                depth -= 1
                if depth == 0:
                    yield ''.join(buf)
                elif depth > 0:
                    buf.append(ch)
                else:
                    depth = 0
                i += 1
                continue

            if depth >= 1:
                buf.append(ch)
            i += 1

    def _split_sql_values(self, row_body: str) -> List[str]:
        """행 바디를 최상위 콤마 기준으로 분리한다 (따옴표/괄호 안 콤마 제외)

        작게 유지되는 로컬 헬퍼로, 외부 파서를 별도로 손대지 않는다.
        """
        parts: List[str] = []
        current: list = []
        in_quote = False
        depth = 0
        i = 0
        n = len(row_body)
        while i < n:
            ch = row_body[i]
            if in_quote:
                current.append(ch)
                if ch == '\\' and i + 1 < n:
                    current.append(row_body[i + 1])
                    i += 2
                    continue
                if ch == "'":
                    if i + 1 < n and row_body[i + 1] == "'":
                        current.append("'")
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
                current.append(ch)
                i += 1
                continue
            if ch == '(':
                depth += 1
                current.append(ch)
                i += 1
                continue
            if ch == ')':
                depth = max(depth - 1, 0)
                current.append(ch)
                i += 1
                continue
            if ch == ',' and depth == 0:
                parts.append(''.join(current))
                current = []
                i += 1
                continue
            current.append(ch)
            i += 1
        parts.append(''.join(current))
        return parts

    def check_enum_numeric_index(self, content: str, location: str) -> List[CompatibilityIssue]:
        """INSERT 문에서 ENUM 컬럼에 숫자 인덱스 사용 확인

        CREATE TABLE의 ENUM 정의와 INSERT VALUES를 결합하여
        ENUM 컬럼에 숫자 값(인덱스)이 삽입되는 경우를 감지합니다.
        MySQL 8.4에서 ENUM 인덱스 동작 변경으로 인한 잠재적 문제를 경고합니다.
        """
        issues = []

        # Step 1: CreateTableParser로 ENUM 컬럼이 있는 테이블 수집
        # table_name -> set of enum column names
        enum_columns: dict = {}
        for statement in self._iter_create_table_statements(content):
            parsed = CreateTableParser().parse(statement)
            if not parsed:
                continue
            cols = {
                col.name.lower()
                for col in parsed.columns
                if col.data_type.upper() == "ENUM"
            }
            if cols:
                enum_columns[parsed.name.lower()] = cols

        if not enum_columns:
            return issues

        # Step 2: INSERT 문에서 ENUM 컬럼 위치의 값이 숫자인지 확인
        for insert_match in self._INSERT_PATTERN.finditer(content):
            table_name = insert_match.group(1).lower()
            if table_name not in enum_columns:
                continue

            cols = [c.strip().strip('`').lower() for c in insert_match.group(2).split(',')]
            enum_col_indices = [
                i for i, col in enumerate(cols)
                if col in enum_columns[table_name]
            ]
            if not enum_col_indices:
                continue

            # 현재 INSERT 문 범위로만 스캔 (다음 문장의 튜플을 읽지 않도록 경계 제한)
            statement_end = self._find_statement_end(content, insert_match.start())
            values_segment = content[insert_match.end():statement_end]

            found_in_current_insert = False
            for row_body in self._iter_values_rows(values_segment):
                values = self._split_sql_values(row_body)
                for idx in enum_col_indices:
                    if idx < len(values):
                        val = values[idx].strip()
                        # 숫자 값인지 확인 (따옴표 없는 순수 숫자)
                        if val.isdigit() and int(val) > 0:
                            issues.append(CompatibilityIssue(
                                issue_type=IssueType.ENUM_NUMERIC_INDEX,
                                severity="warning",
                                location=location,
                                description=(
                                    f"ENUM 컬럼 '{cols[idx]}'에 숫자 인덱스 값 {val} 사용 "
                                    f"(테이블: {table_name})"
                                ),
                                suggestion="ENUM 컬럼에는 문자열 값을 사용하세요. 숫자 인덱스는 8.4에서 동작이 변경될 수 있습니다.",
                                table_name=table_name,
                                column_name=cols[idx]
                            ))
                            found_in_current_insert = True
                            break  # 테이블당 한 번만 보고
                if found_in_current_insert:
                    break  # 이 INSERT에서 이미 발견 → 다음 INSERT로

        return issues

    # ================================================================
    # D04: ENUM 요소 길이 초과 검사 (라이브 DB)
    # ================================================================
    def check_enum_element_length(self, schema: str) -> List[CompatibilityIssue]:
        """ENUM 요소가 255자 초과하는지 확인"""
        if not self.connector:
            return []

        self._log("🔍 ENUM 요소 길이 검사 중...")
        issues = []
        max_length = IDENTIFIER_LIMITS['ENUM_ELEMENT']

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'enum'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            elements = self._extract_enum_elements(col['COLUMN_TYPE'])
            for elem in elements:
                if len(elem) > max_length:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.ENUM_ELEMENT_LENGTH,
                        severity="error",
                        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                        description=f"ENUM 요소 {max_length}자 초과: '{elem[:30]}...' ({len(elem)}자)",
                        suggestion=f"ENUM 요소는 최대 {max_length}자까지 허용됨",
                        table_name=col['TABLE_NAME'],
                        column_name=col['COLUMN_NAME']
                    ))

        if issues:
            self._log(f"  ⚠️ ENUM 요소 길이 초과 {len(issues)}개 발견")
        else:
            self._log("  ✅ ENUM 요소 길이 정상")

        return issues

    # ================================================================
    # D05: SET 요소 길이 초과 검사 (라이브 DB)
    # ================================================================
    def check_set_element_length(self, schema: str) -> List[CompatibilityIssue]:
        """SET 요소가 255자 초과하는지 확인"""
        if not self.connector:
            return []

        self._log("🔍 SET 요소 길이 검사 중...")
        issues = []
        max_length = IDENTIFIER_LIMITS['SET_ELEMENT']

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE = 'set'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            elements = self._extract_enum_elements(col['COLUMN_TYPE'])  # SET도 동일 형식
            for elem in elements:
                if len(elem) > max_length:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.SET_ELEMENT_LENGTH,
                        severity="error",
                        location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                        description=f"SET 요소 {max_length}자 초과: '{elem[:30]}...' ({len(elem)}자)",
                        suggestion=f"SET 요소는 최대 {max_length}자까지 허용됨",
                        table_name=col['TABLE_NAME'],
                        column_name=col['COLUMN_NAME']
                    ))

        if issues:
            self._log(f"  ⚠️ SET 요소 길이 초과 {len(issues)}개 발견")
        else:
            self._log("  ✅ SET 요소 길이 정상")

        return issues

    def _extract_enum_elements(self, column_type: str) -> List[str]:
        """ENUM/SET COLUMN_TYPE에서 요소 추출"""
        # enum('a','b','c') 또는 set('a','b','c') 형식
        match = re.search(r"(?:enum|set)\s*\((.+)\)", column_type, re.IGNORECASE)
        if not match:
            return []

        elements_str = match.group(1)
        elements = []

        # 작은따옴표로 감싸진 요소 추출
        # 요소 내에 이스케이프된 따옴표('') 처리 필요
        current = ""
        in_quote = False
        i = 0
        while i < len(elements_str):
            char = elements_str[i]

            if char == "'" and not in_quote:
                in_quote = True
                i += 1
                continue
            elif char == "'" and in_quote:
                # 이스케이프된 따옴표('')인지 확인
                if i + 1 < len(elements_str) and elements_str[i + 1] == "'":
                    current += "'"
                    i += 2
                    continue
                else:
                    in_quote = False
                    elements.append(current)
                    current = ""
                    i += 1
                    continue

            if in_quote:
                current += char
            i += 1

        return elements

    # ================================================================
    # D06: 4바이트 UTF-8 문자 감지 (덤프 파일)
    # ================================================================
    def check_4byte_utf8_in_data(self, file_path: Path) -> List[CompatibilityIssue]:
        """TSV/데이터 파일에서 4바이트 UTF-8 문자 감지"""
        issues = []
        count_4byte = 0
        sample_lines = []
        max_lines = 10000
        max_samples = 3

        try:
            truncated = False
            with open(file_path, 'rb') as f:
                for line_num, line in enumerate(f, 1):
                    if line_num > max_lines:
                        truncated = True
                        break

                    # 4바이트 UTF-8 시퀀스: 0xF0-0xF4로 시작
                    for byte in line:
                        if 0xF0 <= byte <= 0xF4:
                            count_4byte += 1
                            if len(sample_lines) < max_samples:
                                sample_lines.append(line_num)
                            break

            if count_4byte > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.DATA_4BYTE_UTF8,
                    severity="warning",
                    location=file_path.name,
                    description=f"4바이트 UTF-8 문자 발견 (이모지 등): {count_4byte}개 행",
                    suggestion="utf8mb3 테이블은 4바이트 문자 저장 불가, utf8mb4로 변환 필요",
                    code_snippet=f"라인: {', '.join(map(str, sample_lines[:3]))}"
                ))

            if truncated:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="info",
                    location=file_path.name,
                    description=f"4바이트 UTF-8 스캔이 {max_lines}행에서 중단됨 (전체 파일 미검사)",
                    suggestion="전체 파일을 검사하려면 max_lines 설정을 조정하거나 데이터베이스에서 직접 확인하세요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")
            issues.append(CompatibilityIssue(
                issue_type=IssueType.DATA_4BYTE_UTF8,
                severity="info",
                location=file_path.name,
                description=f"4바이트 UTF-8 스캔 미완료: {str(e)[:80]}",
                suggestion="파일 접근 권한/인코딩 확인 후 재검사 권장"
            ))

        return issues

    # ================================================================
    # D07: NULL 바이트 감지 (덤프 파일)
    # ================================================================
    def check_null_byte_in_data(self, file_path: Path) -> List[CompatibilityIssue]:
        """데이터에 NULL 바이트 (\\x00) 포함 여부 확인"""
        issues = []
        null_count = 0
        sample_lines = []
        max_lines = 10000
        max_samples = 3

        try:
            truncated = False
            with open(file_path, 'rb') as f:
                for line_num, line in enumerate(f, 1):
                    if line_num > max_lines:
                        truncated = True
                        break
                    if b'\x00' in line:
                        null_count += 1
                        if len(sample_lines) < max_samples:
                            sample_lines.append(line_num)

            if null_count > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.DATA_NULL_BYTE,
                    severity="error",
                    location=file_path.name,
                    description=f"NULL 바이트 포함 데이터: {null_count}개 행",
                    suggestion="NULL 바이트는 문자열 필드에서 문제 발생 가능, 데이터 정제 필요",
                    code_snippet=f"라인: {', '.join(map(str, sample_lines[:3]))}"
                ))

            if truncated:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="info",
                    location=file_path.name,
                    description=f"NULL 바이트 스캔이 {max_lines}행에서 중단됨 (전체 파일 미검사)",
                    suggestion="전체 파일을 검사하려면 max_lines 설정을 조정하거나 데이터베이스에서 직접 확인하세요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")
            issues.append(CompatibilityIssue(
                issue_type=IssueType.DATA_NULL_BYTE,
                severity="info",
                location=file_path.name,
                description=f"NULL 바이트 스캔 미완료: {str(e)[:80]}",
                suggestion="파일 접근 권한 확인 후 재검사 권장"
            ))

        return issues

    # ================================================================
    # D08: TIMESTAMP 범위 초과 검사 (덤프 파일)
    # ================================================================
    def check_timestamp_range(self, file_path: Path) -> List[CompatibilityIssue]:
        """TIMESTAMP 범위 (1970-01-01 ~ 2038-01-19) 초과 확인"""
        issues = []
        out_of_range_count = 0
        sample_values = []
        max_lines = 10000
        max_samples = 3

        try:
            truncated = False
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for line_num, line in enumerate(f, 1):
                    if line_num > max_lines:
                        truncated = True
                        break

                    for match in TIMESTAMP_PATTERN.finditer(line):
                        year = int(match.group(1))
                        # TIMESTAMP 범위: 1970-2038
                        if year < 1970 or year > 2038:
                            out_of_range_count += 1
                            if len(sample_values) < max_samples:
                                sample_values.append(match.group(0))

            if out_of_range_count > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.TIMESTAMP_RANGE,
                    # 덤프 파일만으로는 이 값이 TIMESTAMP 컬럼인지 DATETIME 컬럼인지
                    # 구분할 수 없으므로 error가 아닌 warning으로 완화한다
                    severity="warning",
                    location=file_path.name,
                    description=f"TIMESTAMP 범위 초과 후보 값: {out_of_range_count}개 (컬럼 타입 미확인)",
                    suggestion=(
                        "덤프 파일만으로는 TIMESTAMP/DATETIME 컬럼 여부를 구분할 수 없습니다. "
                        "원본 컬럼 타입을 확인하세요: TIMESTAMP 컬럼이라면 1970-2038 범위를 "
                        "벗어나는 값을 변환/처리해야 하고, DATETIME 컬럼이라면 이 값은 유효한 "
                        "sentinel 값일 수 있습니다."
                    ),
                    code_snippet=f"값: {', '.join(sample_values[:3])}"
                ))

            if truncated:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="info",
                    location=file_path.name,
                    description=f"TIMESTAMP 범위 스캔이 {max_lines}행에서 중단됨 (전체 파일 미검사)",
                    suggestion="전체 파일을 검사하려면 max_lines 설정을 조정하거나 데이터베이스에서 직접 확인하세요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")
            issues.append(CompatibilityIssue(
                issue_type=IssueType.TIMESTAMP_RANGE,
                severity="info",
                location=file_path.name,
                description=f"TIMESTAMP 범위 스캔 미완료: {str(e)[:80]}",
                suggestion="파일 접근 권한/인코딩 확인 후 재검사 권장"
            ))

        return issues

    # ================================================================
    # D09: latin1 비ASCII 데이터 검사 (라이브 DB)
    # ================================================================

    # 컬럼 수 상한: 이 수를 초과하면 부분 스캔 경고를 표시
    _MAX_COLUMNS_TO_CHECK = 50

    # ZEROFILL 패딩 의존성 검사 시 스캔할 최대 행 수 (대용량 테이블 전체 스캔 방지)
    _MAX_ZEROFILL_ROWS_TO_SCAN = 100000

    def check_latin1_non_ascii(self, schema: str) -> List[CompatibilityIssue]:
        """latin1 컬럼에서 비ASCII 데이터 확인 (배치 쿼리 방식)"""
        if not self.connector:
            return []

        self._log("🔍 latin1 비ASCII 데이터 검사 중...")
        issues = []

        # latin1 컬럼 전체 목록 수집 (단일 INFORMATION_SCHEMA 쿼리)
        query = """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND CHARACTER_SET_NAME = 'latin1'
            AND DATA_TYPE IN ('varchar', 'char', 'text', 'mediumtext', 'longtext')
        ORDER BY TABLE_NAME, COLUMN_NAME
        """
        columns = self.connector.execute(query, (schema,))

        if not columns:
            self._log("  ✅ latin1 비ASCII 데이터 없음")
            return issues

        # 컬럼 수 상한 적용
        partial_scan = len(columns) > self._MAX_COLUMNS_TO_CHECK
        if partial_scan:
            self._log(
                f"  ⚠️ latin1 컬럼 {len(columns)}개 감지 — "
                f"상위 {self._MAX_COLUMNS_TO_CHECK}개만 스캔 (부분 스캔)"
            )
            columns = columns[: self._MAX_COLUMNS_TO_CHECK]

        # 테이블별로 컬럼을 묶어 배치 처리 (테이블당 1회 쿼리)
        from itertools import groupby

        def _table_key(col):
            return col['TABLE_NAME']

        for table_name, col_group in groupby(columns, key=_table_key):
            col_list = list(col_group)
            # 각 컬럼에 대한 REGEXP 조건을 OR로 결합하여 단일 쿼리로 처리
            conditions = " OR ".join(
                f"`{c['COLUMN_NAME']}` REGEXP '[^\\x00-\\x7F]'"
                for c in col_list
            )
            # 테이블당 비ASCII가 있는 컬럼을 한 번에 식별
            select_cols = ", ".join(
                f"MAX(`{c['COLUMN_NAME']}` REGEXP '[^\\x00-\\x7F]') AS `{c['COLUMN_NAME']}`"
                for c in col_list
            )
            batch_query = (
                f"SELECT {select_cols} "
                f"FROM `{schema}`.`{table_name}` "
                f"WHERE {conditions} "
                f"LIMIT 1"
            )
            try:
                result = self.connector.execute(batch_query)
                if result:
                    row = result[0]
                    for col in col_list:
                        col_name = col['COLUMN_NAME']
                        if row.get(col_name):
                            issues.append(CompatibilityIssue(
                                issue_type=IssueType.LATIN1_NON_ASCII,
                                severity="warning",
                                location=f"{schema}.{table_name}.{col_name}",
                                description="latin1 컬럼에 비ASCII 데이터 존재",
                                suggestion="utf8mb4 변환 전 데이터 인코딩 확인 필요",
                                table_name=table_name,
                                column_name=col_name
                            ))
            except Exception as e:
                self._log(
                    f"    ⏭️ {table_name} latin1 배치 검사 스킵: {str(e)[:80]}"
                )

        if partial_scan:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.LATIN1_NON_ASCII,
                severity="info",
                location=schema,
                description=(
                    f"latin1 컬럼이 {self._MAX_COLUMNS_TO_CHECK}개를 초과하여 "
                    f"부분 스캔만 수행되었습니다. 나머지 컬럼은 수동 확인 권장."
                ),
                suggestion="SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                           "WHERE TABLE_SCHEMA='<db>' AND CHARACTER_SET_NAME='latin1' 로 전체 목록 확인"
            ))

        if issues:
            self._log(f"  ⚠️ latin1 비ASCII 데이터 {len(issues)}개 발견")
        else:
            self._log("  ✅ latin1 비ASCII 데이터 없음")

        return issues

    # ================================================================
    # D10: ZEROFILL 데이터 의존성 검사 (라이브 DB)
    # ================================================================
    def check_zerofill_data_dependency(self, schema: str) -> List[CompatibilityIssue]:
        """ZEROFILL 컬럼의 실제 데이터가 패딩에 의존하는지 확인 (배치 쿼리 방식)"""
        if not self.connector:
            return []

        self._log("🔍 ZEROFILL 데이터 의존성 검사 중...")
        issues = []

        # ZEROFILL 컬럼 전체 목록 수집 (단일 INFORMATION_SCHEMA 쿼리)
        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND COLUMN_TYPE LIKE '%%ZEROFILL%%'
        ORDER BY TABLE_NAME, COLUMN_NAME
        """
        columns = self.connector.execute(query, (schema,))

        if not columns:
            self._log("  ✅ ZEROFILL 의존 데이터 없음")
            return issues

        # 너비 정보 사전 파싱 — 너비를 알 수 없는 컬럼은 건너뜀
        parsed_cols = []
        for col in columns:
            width_match = re.search(r'\((\d+)\)', col['COLUMN_TYPE'])
            if width_match:
                parsed_cols.append({
                    'TABLE_NAME': col['TABLE_NAME'],
                    'COLUMN_NAME': col['COLUMN_NAME'],
                    'width': int(width_match.group(1)),
                })

        if not parsed_cols:
            self._log("  ✅ ZEROFILL 의존 데이터 없음")
            return issues

        # 컬럼 수 상한 적용
        partial_scan = len(parsed_cols) > self._MAX_COLUMNS_TO_CHECK
        if partial_scan:
            self._log(
                f"  ⚠️ ZEROFILL 컬럼 {len(parsed_cols)}개 감지 — "
                f"상위 {self._MAX_COLUMNS_TO_CHECK}개만 스캔 (부분 스캔)"
            )
            parsed_cols = parsed_cols[: self._MAX_COLUMNS_TO_CHECK]

        # 테이블별로 컬럼을 묶어 배치 처리 (테이블당 1회 쿼리)
        from itertools import groupby

        def _table_key(col):
            return col['TABLE_NAME']

        for table_name, col_group in groupby(parsed_cols, key=_table_key):
            col_list = list(col_group)
            # 각 컬럼의 패딩 의존 여부를 단일 SELECT로 판별
            # LENGTH(CAST(col AS CHAR)) < width 인 행이 존재하면 패딩 의존
            # 집계 자체에는 LIMIT을 걸 수 없으므로(집계 전 행 제한이 무의미),
            # 행 수 상한을 내부 서브쿼리에 적용해 대용량 테이블 전체 스캔을 방지한다
            select_parts = []
            inner_columns = []
            for c in col_list:
                w = c['width']
                cname = c['COLUMN_NAME']
                select_parts.append(
                    f"MAX(CASE WHEN LENGTH(CAST(`{cname}` AS CHAR)) < {w} "
                    f"AND `{cname}` IS NOT NULL AND `{cname}` > 0 THEN 1 ELSE 0 END) "
                    f"AS `{cname}`"
                )
                inner_columns.append(f"`{cname}`")
            batch_query = (
                f"SELECT {', '.join(select_parts)} "
                f"FROM (SELECT {', '.join(inner_columns)} "
                f"FROM `{schema}`.`{table_name}` "
                f"LIMIT {self._MAX_ZEROFILL_ROWS_TO_SCAN}) AS sampled"
            )
            try:
                result = self.connector.execute(batch_query)
                if result:
                    row = result[0]
                    for col in col_list:
                        col_name = col['COLUMN_NAME']
                        if row.get(col_name):
                            issues.append(CompatibilityIssue(
                                issue_type=IssueType.ZEROFILL_USAGE,
                                severity="warning",
                                location=f"{schema}.{table_name}.{col_name}",
                                description=(
                                    f"ZEROFILL 패딩에 의존하는 데이터 존재 "
                                    f"(너비: {col['width']})"
                                ),
                                suggestion="ZEROFILL 제거 시 LPAD() 함수로 애플리케이션에서 처리 필요",
                                table_name=table_name,
                                column_name=col_name
                            ))
            except Exception as e:
                self._log(
                    f"    ⏭️ {table_name} ZEROFILL 배치 검사 스킵: {str(e)[:80]}"
                )

        if partial_scan:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.ZEROFILL_USAGE,
                severity="info",
                location=schema,
                description=(
                    f"ZEROFILL 컬럼이 {self._MAX_COLUMNS_TO_CHECK}개를 초과하여 "
                    f"부분 스캔만 수행되었습니다. 나머지 컬럼은 수동 확인 권장."
                ),
                suggestion="SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                           "WHERE TABLE_SCHEMA='<db>' AND COLUMN_TYPE LIKE '%ZEROFILL%' 로 전체 목록 확인"
            ))

        if issues:
            self._log(f"  ⚠️ ZEROFILL 의존 데이터 {len(issues)}개 발견")
        else:
            self._log("  ✅ ZEROFILL 의존 데이터 없음")

        return issues

    # ================================================================
    # D11: 잘못된 DATETIME 검사 (덤프 파일) - 기존 확장
    # ================================================================
    def check_invalid_datetime(self, file_path: Path) -> List[CompatibilityIssue]:
        """0000-00-00 및 잘못된 날짜 값 확인"""
        issues = []
        invalid_count = 0
        sample_values = []
        max_lines = 10000
        max_samples = 3

        try:
            truncated = False
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for line_num, line in enumerate(f, 1):
                    if line_num > max_lines:
                        truncated = True
                        break

                    # 0000-00-00 / 0000-00-00 00:00:00 / 연·월·일이 00인 경우를
                    # 모두 검사하되, 같은 위치(span)를 여러 패턴이 중복으로
                    # 매치하면 한 행에서 두 번 세는 문제가 있었다. span 기준으로
                    # 중복 제거한 뒤, 행에 하나라도 있으면 행 단위로 1회만 카운트한다
                    # (설명 텍스트가 "개 행" 단위이므로 값 개수가 아닌 행 개수여야 함)
                    seen_spans = []
                    line_values = []
                    for pattern in (
                        INVALID_DATETIME_PATTERN,
                        INVALID_DATE_PATTERN,
                        INVALID_DATE_VALUES_PATTERN,
                    ):
                        for match in pattern.finditer(line):
                            span = match.span()
                            if span in seen_spans:
                                continue
                            seen_spans.append(span)
                            line_values.append(match.group(0))

                    if line_values:
                        invalid_count += 1
                        for value in line_values:
                            if len(sample_values) >= max_samples:
                                break
                            sample_values.append(value)

            if invalid_count > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.INVALID_DATE,
                    severity="error",
                    location=file_path.name,
                    description=f"잘못된 날짜 값 발견: {invalid_count}개 행 (0000-00-00 등)",
                    suggestion="NO_ZERO_DATE SQL 모드 활성화 시 오류 발생, 유효한 날짜로 변환 필요",
                    code_snippet=f"값: {', '.join(sample_values[:3])}"
                ))

            if truncated:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="info",
                    location=file_path.name,
                    description=f"DATETIME 스캔이 {max_lines}행에서 중단됨 (전체 파일 미검사)",
                    suggestion="전체 파일을 검사하려면 max_lines 설정을 조정하거나 데이터베이스에서 직접 확인하세요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    # ================================================================
    # 통합 검사 메서드
    # ================================================================
    def check_all_live_db(self, schema: str) -> List[CompatibilityIssue]:
        """라이브 DB의 모든 데이터 무결성 검사 실행"""
        if not self.connector:
            return []

        issues = []
        issues.extend(self.check_enum_empty_value_definition(schema))
        issues.extend(self.check_enum_element_length(schema))
        issues.extend(self.check_set_element_length(schema))
        issues.extend(self.check_latin1_non_ascii(schema))
        issues.extend(self.check_zerofill_data_dependency(schema))
        return issues

    def check_all_sql_content(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일 내용의 모든 데이터 무결성 검사 실행"""
        issues = []
        issues.extend(self.check_enum_empty_in_sql(content, location))
        issues.extend(self.check_enum_empty_insert(content, location))
        issues.extend(self.check_enum_numeric_index(content, location))
        return issues

    def check_all_data_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """데이터 파일(TSV 등)의 모든 데이터 무결성 검사 실행"""
        issues = []
        issues.extend(self.check_4byte_utf8_in_data(file_path))
        issues.extend(self.check_null_byte_in_data(file_path))
        issues.extend(self.check_timestamp_range(file_path))
        issues.extend(self.check_invalid_datetime(file_path))
        return issues
