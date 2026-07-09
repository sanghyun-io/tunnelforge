"""
데이터 무결성 규칙 모듈

MySQL 8.0 → 8.4 업그레이드 시 데이터 무결성 관련 호환성 검사 규칙.
11개 규칙 구현(D01-D11):
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
"""

import re
from itertools import groupby
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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
from ..migration_parsers import CreateTableParser, SqlStatementScanner
from ._base import ProgressLoggingRuleBase


class DataIntegrityRules(ProgressLoggingRuleBase):
    """데이터 무결성 규칙 모음"""

    # 덤프/데이터 파일 라인 스캔 상한 (대용량 파일 전체 스캔 방지)
    _MAX_SCAN_LINES = 10000
    # 이슈 설명에 남길 샘플 라인/값 개수 상한
    _MAX_SAMPLE_VALUES = 3

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

        self._log_summary(issues, "ENUM 빈 값 정의", "ENUM 빈 값 정의 없음")

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

    @staticmethod
    def _find_numeric_enum_value(
        values: List[str], enum_col_indices: List[int], cols: List[str]
    ) -> Optional[Tuple[str, str]]:
        """VALUES 한 행에서 ENUM 컬럼 위치의 값이 양의 정수(인덱스)면
        (컬럼명, 값)을 반환한다. 해당하는 값이 없으면 None."""
        for idx in enum_col_indices:
            if idx < len(values):
                val = values[idx].strip()
                # 숫자 값인지 확인 (따옴표 없는 순수 숫자)
                if val.isdigit() and int(val) > 0:
                    return cols[idx], val
        return None

    def check_enum_numeric_index(self, content: str, location: str) -> List[CompatibilityIssue]:
        """INSERT 문에서 ENUM 컬럼에 숫자 인덱스 사용 확인

        CREATE TABLE의 ENUM 정의와 INSERT VALUES를 결합하여
        ENUM 컬럼에 숫자 값(인덱스)이 삽입되는 경우를 감지합니다.
        MySQL 8.4에서 ENUM 인덱스 동작 변경으로 인한 잠재적 문제를 경고합니다.
        """
        issues = []
        scanner = SqlStatementScanner()

        # Step 1: CreateTableParser로 ENUM 컬럼이 있는 테이블 수집
        # table_name -> set of enum column names
        enum_columns: dict = {}
        for statement in scanner.iter_create_table_statements(content):
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
            statement_end = scanner.find_statement_end(content, insert_match.start())
            values_segment = content[insert_match.end():statement_end]

            for row_body in scanner.iter_values_rows(values_segment):
                found = self._find_numeric_enum_value(
                    scanner.split_sql_values(row_body), enum_col_indices, cols
                )
                if found:
                    col_name, val = found
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.ENUM_NUMERIC_INDEX,
                        severity="warning",
                        location=location,
                        description=(
                            f"ENUM 컬럼 '{col_name}'에 숫자 인덱스 값 {val} 사용 "
                            f"(테이블: {table_name})"
                        ),
                        suggestion="ENUM 컬럼에는 문자열 값을 사용하세요. 숫자 인덱스는 8.4에서 동작이 변경될 수 있습니다.",
                        table_name=table_name,
                        column_name=col_name
                    ))
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

        self._log_summary(issues, "ENUM 요소 길이 초과", "ENUM 요소 길이 정상")

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

        self._log_summary(issues, "SET 요소 길이 초과", "SET 요소 길이 정상")

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
    # 파일 라인 스캔 공통 템플릿 (D06-D08, D11)
    # ================================================================
    def _scan_file_lines(
        self,
        file_path: Path,
        mode: str,
        per_line_check: Callable[[int, object], None],
        build_findings: Callable[[], List[CompatibilityIssue]],
        *,
        scan_label: str,
        incomplete_issue_type: IssueType,
        incomplete_suggestion: str,
    ) -> List[CompatibilityIssue]:
        """파일을 라인 단위로 스캔하며 per_line_check를 호출하는 템플릿.

        read-loop / _MAX_SCAN_LINES 절단 / SCAN_TRUNCATED info 이슈 / 읽기
        실패 시 미완료 info 이슈 같은 공통 보일러플레이트를 담당한다. 각
        검사는 per-line 판정(per_line_check, 클로저로 상태 누적)과 스캔
        성공 후의 발견 이슈 생성(build_findings)만 제공한다.

        mode에 'b'가 있으면 바이너리로, 없으면 utf-8/replace 텍스트로 연다.
        """
        issues: List[CompatibilityIssue] = []
        try:
            truncated = False
            if 'b' in mode:
                file_cm = open(file_path, mode)
            else:
                file_cm = open(file_path, mode, encoding='utf-8', errors='replace')
            with file_cm as f:
                for line_num, line in enumerate(f, 1):
                    if line_num > self._MAX_SCAN_LINES:
                        truncated = True
                        break
                    per_line_check(line_num, line)

            issues.extend(build_findings())

            if truncated:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="info",
                    location=file_path.name,
                    description=f"{scan_label} 스캔이 {self._MAX_SCAN_LINES}행에서 중단됨 (전체 파일 미검사)",
                    suggestion="전체 파일을 검사하려면 max_lines 설정을 조정하거나 데이터베이스에서 직접 확인하세요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")
            issues.append(CompatibilityIssue(
                issue_type=incomplete_issue_type,
                severity="info",
                location=file_path.name,
                description=f"{scan_label} 스캔 미완료: {str(e)[:80]}",
                suggestion=incomplete_suggestion
            ))

        return issues

    # ================================================================
    # D06: 4바이트 UTF-8 문자 감지 (덤프 파일)
    # ================================================================
    def check_4byte_utf8_in_data(self, file_path: Path) -> List[CompatibilityIssue]:
        """TSV/데이터 파일에서 4바이트 UTF-8 문자 감지"""
        count_4byte = 0
        sample_lines: List[int] = []

        def per_line(line_num: int, line: bytes):
            nonlocal count_4byte
            # 4바이트 UTF-8 시퀀스: 0xF0-0xF4로 시작
            for byte in line:
                if 0xF0 <= byte <= 0xF4:
                    count_4byte += 1
                    if len(sample_lines) < self._MAX_SAMPLE_VALUES:
                        sample_lines.append(line_num)
                    break

        def build_findings() -> List[CompatibilityIssue]:
            if count_4byte <= 0:
                return []
            return [CompatibilityIssue(
                issue_type=IssueType.DATA_4BYTE_UTF8,
                severity="warning",
                location=file_path.name,
                description=f"4바이트 UTF-8 문자 발견 (이모지 등): {count_4byte}개 행",
                suggestion="utf8mb3 테이블은 4바이트 문자 저장 불가, utf8mb4로 변환 필요",
                code_snippet=f"라인: {', '.join(map(str, sample_lines[:3]))}"
            )]

        return self._scan_file_lines(
            file_path, 'rb', per_line, build_findings,
            scan_label="4바이트 UTF-8",
            incomplete_issue_type=IssueType.DATA_4BYTE_UTF8,
            incomplete_suggestion="파일 접근 권한/인코딩 확인 후 재검사 권장",
        )

    # ================================================================
    # D07: NULL 바이트 감지 (덤프 파일)
    # ================================================================
    def check_null_byte_in_data(self, file_path: Path) -> List[CompatibilityIssue]:
        """데이터에 NULL 바이트 (\\x00) 포함 여부 확인"""
        null_count = 0
        sample_lines: List[int] = []

        def per_line(line_num: int, line: bytes):
            nonlocal null_count
            if b'\x00' in line:
                null_count += 1
                if len(sample_lines) < self._MAX_SAMPLE_VALUES:
                    sample_lines.append(line_num)

        def build_findings() -> List[CompatibilityIssue]:
            if null_count <= 0:
                return []
            return [CompatibilityIssue(
                issue_type=IssueType.DATA_NULL_BYTE,
                severity="error",
                location=file_path.name,
                description=f"NULL 바이트 포함 데이터: {null_count}개 행",
                suggestion="NULL 바이트는 문자열 필드에서 문제 발생 가능, 데이터 정제 필요",
                code_snippet=f"라인: {', '.join(map(str, sample_lines[:3]))}"
            )]

        return self._scan_file_lines(
            file_path, 'rb', per_line, build_findings,
            scan_label="NULL 바이트",
            incomplete_issue_type=IssueType.DATA_NULL_BYTE,
            incomplete_suggestion="파일 접근 권한 확인 후 재검사 권장",
        )

    # ================================================================
    # D08: TIMESTAMP 범위 초과 검사 (덤프 파일)
    # ================================================================
    def check_timestamp_range(self, file_path: Path) -> List[CompatibilityIssue]:
        """TIMESTAMP 범위 (1970-01-01 ~ 2038-01-19) 초과 확인"""
        out_of_range_count = 0
        sample_values: List[str] = []

        def per_line(line_num: int, line: str):
            nonlocal out_of_range_count
            for match in TIMESTAMP_PATTERN.finditer(line):
                year = int(match.group(1))
                # TIMESTAMP 범위: 1970-2038
                if year < 1970 or year > 2038:
                    out_of_range_count += 1
                    if len(sample_values) < self._MAX_SAMPLE_VALUES:
                        sample_values.append(match.group(0))

        def build_findings() -> List[CompatibilityIssue]:
            if out_of_range_count <= 0:
                return []
            return [CompatibilityIssue(
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
            )]

        return self._scan_file_lines(
            file_path, 'r', per_line, build_findings,
            scan_label="TIMESTAMP 범위",
            incomplete_issue_type=IssueType.TIMESTAMP_RANGE,
            incomplete_suggestion="파일 접근 권한/인코딩 확인 후 재검사 권장",
        )

    # ================================================================
    # D09: latin1 비ASCII 데이터 검사 (라이브 DB)
    # ================================================================

    # 컬럼 수 상한: 이 수를 초과하면 부분 스캔 경고를 표시
    _MAX_COLUMNS_TO_CHECK = 50

    # ZEROFILL 패딩 의존성 검사 시 스캔할 최대 행 수 (대용량 테이블 전체 스캔 방지)
    _MAX_ZEROFILL_ROWS_TO_SCAN = 100000

    def _batch_scan_columns(
        self,
        schema: str,
        columns: list,
        *,
        build_query: Callable[[str, str, list], str],
        issue_type: IssueType,
        severity: str,
        describe: Callable[[dict], str],
        column_kind: str,
        per_column_suggestion: str,
        partial_scan_suggestion: str,
    ) -> List[CompatibilityIssue]:
        """테이블별 배치 쿼리로 컬럼 데이터를 스캔하는 공통 골격.

        partial-scan 상한 적용, 테이블별 그룹핑, 배치 쿼리 try-except, 마지막
        부분 스캔 info 이슈를 담당한다. 각 검사는 컬럼 선택 쿼리(build_query)와
        per-컬럼 설명(describe)만 제공한다. columns는 TABLE_NAME으로 정렬돼
        있어야 groupby가 테이블 단위로 올바르게 묶는다.
        """
        issues: List[CompatibilityIssue] = []

        # 컬럼 수 상한 적용
        partial_scan = len(columns) > self._MAX_COLUMNS_TO_CHECK
        if partial_scan:
            self._log(
                f"  ⚠️ {column_kind} 컬럼 {len(columns)}개 감지 — "
                f"상위 {self._MAX_COLUMNS_TO_CHECK}개만 스캔 (부분 스캔)"
            )
            columns = columns[: self._MAX_COLUMNS_TO_CHECK]

        # 테이블별로 컬럼을 묶어 배치 처리 (테이블당 1회 쿼리)
        for table_name, col_group in groupby(columns, key=lambda col: col['TABLE_NAME']):
            col_list = list(col_group)
            batch_query = build_query(schema, table_name, col_list)
            try:
                result = self.connector.execute(batch_query)
                if result:
                    row = result[0]
                    for col in col_list:
                        col_name = col['COLUMN_NAME']
                        if row.get(col_name):
                            issues.append(CompatibilityIssue(
                                issue_type=issue_type,
                                severity=severity,
                                location=f"{schema}.{table_name}.{col_name}",
                                description=describe(col),
                                suggestion=per_column_suggestion,
                                table_name=table_name,
                                column_name=col_name
                            ))
            except Exception as e:
                self._log(
                    f"    ⏭️ {table_name} {column_kind} 배치 검사 스킵: {str(e)[:80]}"
                )

        if partial_scan:
            issues.append(CompatibilityIssue(
                issue_type=issue_type,
                severity="info",
                location=schema,
                description=(
                    f"{column_kind} 컬럼이 {self._MAX_COLUMNS_TO_CHECK}개를 초과하여 "
                    f"부분 스캔만 수행되었습니다. 나머지 컬럼은 수동 확인 권장."
                ),
                suggestion=partial_scan_suggestion
            ))

        return issues

    def check_latin1_non_ascii(self, schema: str) -> List[CompatibilityIssue]:
        """latin1 컬럼에서 비ASCII 데이터 확인 (배치 쿼리 방식)"""
        if not self.connector:
            return []

        self._log("🔍 latin1 비ASCII 데이터 검사 중...")

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
            return []

        def build_query(schema: str, table_name: str, col_list: list) -> str:
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
            return (
                f"SELECT {select_cols} "
                f"FROM `{schema}`.`{table_name}` "
                f"WHERE {conditions} "
                f"LIMIT 1"
            )

        issues = self._batch_scan_columns(
            schema, columns,
            build_query=build_query,
            issue_type=IssueType.LATIN1_NON_ASCII,
            severity="warning",
            describe=lambda col: "latin1 컬럼에 비ASCII 데이터 존재",
            column_kind="latin1",
            per_column_suggestion="utf8mb4 변환 전 데이터 인코딩 확인 필요",
            partial_scan_suggestion=(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='<db>' AND CHARACTER_SET_NAME='latin1' 로 전체 목록 확인"
            ),
        )

        self._log_summary(issues, "latin1 비ASCII 데이터", "latin1 비ASCII 데이터 없음")
        return issues

    # ================================================================
    # D10: ZEROFILL 데이터 의존성 검사 (라이브 DB)
    # ================================================================
    def check_zerofill_data_dependency(self, schema: str) -> List[CompatibilityIssue]:
        """ZEROFILL 컬럼의 실제 데이터가 패딩에 의존하는지 확인 (배치 쿼리 방식)"""
        if not self.connector:
            return []

        self._log("🔍 ZEROFILL 데이터 의존성 검사 중...")

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
            return []

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
            return []

        def build_query(schema: str, table_name: str, col_list: list) -> str:
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
            return (
                f"SELECT {', '.join(select_parts)} "
                f"FROM (SELECT {', '.join(inner_columns)} "
                f"FROM `{schema}`.`{table_name}` "
                f"LIMIT {self._MAX_ZEROFILL_ROWS_TO_SCAN}) AS sampled"
            )

        issues = self._batch_scan_columns(
            schema, parsed_cols,
            build_query=build_query,
            issue_type=IssueType.ZEROFILL_USAGE,
            severity="warning",
            describe=lambda col: (
                f"ZEROFILL 패딩에 의존하는 데이터 존재 (너비: {col['width']})"
            ),
            column_kind="ZEROFILL",
            per_column_suggestion="ZEROFILL 제거 시 LPAD() 함수로 애플리케이션에서 처리 필요",
            partial_scan_suggestion=(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='<db>' AND COLUMN_TYPE LIKE '%ZEROFILL%' 로 전체 목록 확인"
            ),
        )

        self._log_summary(issues, "ZEROFILL 의존 데이터", "ZEROFILL 의존 데이터 없음")
        return issues

    # ================================================================
    # D11: 잘못된 DATETIME 검사 (덤프 파일) - 기존 확장
    # ================================================================
    def check_invalid_datetime(self, file_path: Path) -> List[CompatibilityIssue]:
        """0000-00-00 및 잘못된 날짜 값 확인"""
        invalid_count = 0
        sample_values: List[str] = []

        def per_line(line_num: int, line: str):
            nonlocal invalid_count
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
                    if len(sample_values) >= self._MAX_SAMPLE_VALUES:
                        break
                    sample_values.append(value)

        def build_findings() -> List[CompatibilityIssue]:
            if invalid_count <= 0:
                return []
            return [CompatibilityIssue(
                issue_type=IssueType.INVALID_DATE,
                severity="error",
                location=file_path.name,
                description=f"잘못된 날짜 값 발견: {invalid_count}개 행 (0000-00-00 등)",
                suggestion="NO_ZERO_DATE SQL 모드 활성화 시 오류 발생, 유효한 날짜로 변환 필요",
                code_snippet=f"값: {', '.join(sample_values[:3])}"
            )]

        # 파일 읽기 실패 시 형제 검사와 동일하게 info 이슈 1건을 방출한다 (CC-088)
        return self._scan_file_lines(
            file_path, 'r', per_line, build_findings,
            scan_label="DATETIME",
            incomplete_issue_type=IssueType.INVALID_DATE,
            incomplete_suggestion="파일 접근 권한/인코딩 확인 후 재검사 권장",
        )

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
