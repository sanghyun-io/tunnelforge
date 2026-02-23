"""
스키마/객체 규칙 모듈

MySQL 8.0 → 8.4 업그레이드 시 스키마 및 객체 관련 호환성 검사 규칙.
30개 규칙 구현:
- S01: YEAR(2) 데이터 타입
- S02: latin1 charset 권장
- S03-S04: 인덱스 크기 초과
- S05: GROUP BY ASC/DESC 구문
- S06: SQL_CALC_FOUND_ROWS
- S07: 달러 기호 식별자
- S08: 트레일링 스페이스 식별자
- S09: 제어 문자 식별자
- S10-S13: deprecated 스토리지 엔진
- S14-S15: 파티션 이슈
- S16: 생성 컬럼 함수
- S17: old geometry 타입
- S18: BLOB/TEXT DEFAULT
- S19-S20: 트리거/이벤트 구문
- S21: Spatial 타입 변경
- S22: JSON_TABLE 구문
- S23: MySQL 스키마 충돌
- S24-S25: Definer 검사
"""

from typing import List, Optional, Callable, Dict, TYPE_CHECKING

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    INDEX_SIZE_LIMITS,
    CHARSET_BYTES_PER_CHAR,
    MYSQL_SCHEMA_TABLES,
    DEPRECATED_SYNTAX_PATTERNS,
    YEAR2_PATTERN,
    DOLLAR_SIGN_PATTERN,
    TRAILING_SPACE_PATTERN,
    CONTROL_CHAR_PATTERN,
    BLOB_TEXT_DEFAULT_PATTERN,
    GENERATED_COLUMN_PATTERN,
    ALL_REMOVED_FUNCTIONS,
)

if TYPE_CHECKING:
    from ..db_connector import MySQLConnector


class SchemaRules:
    """스키마/객체 규칙 모음"""

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

        if issues:
            self._log(f"  ⚠️ YEAR(2) 타입 {len(issues)}개 발견")
        else:
            self._log("  ✅ YEAR(2) 타입 없음")

        return issues

    # ================================================================
    # S01: YEAR(2) 검사 (덤프 파일)
    # ================================================================
    def check_year2_in_sql(self, content: str, location: str) -> List[CompatibilityIssue]:
        """SQL 파일에서 YEAR(2) 타입 확인"""
        issues = []

        for match in YEAR2_PATTERN.finditer(content):
            line_start = content.rfind('\n', 0, match.start()) + 1
            line_end = content.find('\n', match.end())
            line = content[line_start:line_end].strip()

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
    # S02: latin1 charset 권장 (라이브 DB)
    # ================================================================
    def check_latin1_charset(self, schema: str) -> List[CompatibilityIssue]:
        """latin1 charset 사용 테이블/컬럼 확인 (utf8mb4 권장)"""
        if not self.connector:
            return []

        self._log("🔍 latin1 charset 검사 중...")
        issues = []

        # 테이블 레벨
        table_query = """
        SELECT TABLE_NAME, TABLE_COLLATION
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
            AND TABLE_COLLATION LIKE 'latin1_%%'
        """
        tables = self.connector.execute(table_query, (schema,))

        for t in tables:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.LATIN1_CHARSET,
                severity="info",
                location=f"{schema}.{t['TABLE_NAME']}",
                description=f"테이블이 latin1 collation 사용: {t['TABLE_COLLATION']}",
                suggestion="utf8mb4로 마이그레이션 권장",
                table_name=t['TABLE_NAME']
            ))

        # 컬럼 레벨
        column_query = """
        SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND CHARACTER_SET_NAME = 'latin1'
        """
        columns = self.connector.execute(column_query, (schema,))

        for c in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.LATIN1_CHARSET,
                severity="info",
                location=f"{schema}.{c['TABLE_NAME']}.{c['COLUMN_NAME']}",
                description="컬럼이 latin1 charset 사용",
                suggestion="utf8mb4로 마이그레이션 권장",
                table_name=c['TABLE_NAME'],
                column_name=c['COLUMN_NAME']
            ))

        if issues:
            self._log(f"  ℹ️ latin1 사용 {len(issues)}개 발견")
        else:
            self._log("  ✅ latin1 사용 없음")

        return issues

    # ================================================================
    # S03-S04: 인덱스 크기 초과 검사 (라이브 DB)
    # ================================================================
    def calculate_column_byte_size(self, col_info: dict) -> int:
        """컬럼의 인덱스 바이트 크기 계산"""
        data_type = col_info.get('DATA_TYPE', '').lower()
        char_length = col_info.get('CHARACTER_MAXIMUM_LENGTH', 0) or 0
        charset = col_info.get('CHARACTER_SET_NAME', 'utf8mb4')
        sub_part = col_info.get('SUB_PART')  # 인덱스 prefix 길이

        # prefix 지정된 경우
        if sub_part:
            char_length = min(char_length, int(sub_part))

        # 문자열 타입
        if data_type in ('varchar', 'char', 'text', 'mediumtext', 'longtext', 'tinytext'):
            bytes_per_char = CHARSET_BYTES_PER_CHAR.get(charset, 4)
            # VARCHAR는 길이 바이트 추가 (1-2바이트)
            length_bytes = 2 if data_type == 'varchar' else 0
            return char_length * bytes_per_char + length_bytes

        # 숫자 타입
        numeric_sizes = {
            'tinyint': 1, 'smallint': 2, 'mediumint': 3,
            'int': 4, 'integer': 4, 'bigint': 8,
            'float': 4, 'double': 8,
        }
        if data_type in numeric_sizes:
            return numeric_sizes[data_type]

        # DECIMAL - 정밀도에 따라 다름
        if data_type == 'decimal':
            # 간단히 최대값 추정
            return 16

        # 날짜/시간 타입
        datetime_sizes = {
            'date': 3, 'time': 3, 'datetime': 8,
            'timestamp': 4, 'year': 1,
        }
        if data_type in datetime_sizes:
            return datetime_sizes[data_type]

        # BINARY/VARBINARY
        if data_type in ('binary', 'varbinary'):
            length_bytes = 2 if data_type == 'varbinary' else 0
            return char_length + length_bytes

        # 기타 (BLOB 등은 prefix만 인덱싱)
        if sub_part:
            return int(sub_part)
        return INDEX_SIZE_LIMITS['DEFAULT_PREFIX_LENGTH']

    def check_index_too_large(self, schema: str) -> List[CompatibilityIssue]:
        """인덱스 크기 3072바이트 초과 확인"""
        if not self.connector:
            return []

        self._log("🔍 인덱스 크기 검사 중...")
        issues = []
        max_key_length = INDEX_SIZE_LIMITS['INNODB_MAX_KEY_LENGTH']

        # 인덱스 정보 조회
        index_query = """
        SELECT
            s.TABLE_NAME, s.INDEX_NAME, s.COLUMN_NAME, s.SUB_PART,
            s.SEQ_IN_INDEX,
            c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH, c.CHARACTER_SET_NAME
        FROM INFORMATION_SCHEMA.STATISTICS s
        JOIN INFORMATION_SCHEMA.COLUMNS c
            ON s.TABLE_SCHEMA = c.TABLE_SCHEMA
            AND s.TABLE_NAME = c.TABLE_NAME
            AND s.COLUMN_NAME = c.COLUMN_NAME
        WHERE s.TABLE_SCHEMA = %s
        ORDER BY s.TABLE_NAME, s.INDEX_NAME, s.SEQ_IN_INDEX
        """
        stats = self.connector.execute(index_query, (schema,))

        # 인덱스별로 그룹화하여 크기 계산
        index_sizes: Dict[str, int] = {}  # "table.index" -> size
        index_columns: Dict[str, List[str]] = {}  # "table.index" -> columns

        for row in stats:
            key = f"{row['TABLE_NAME']}.{row['INDEX_NAME']}"
            col_size = self.calculate_column_byte_size(row)

            if key not in index_sizes:
                index_sizes[key] = 0
                index_columns[key] = []

            index_sizes[key] += col_size
            index_columns[key].append(row['COLUMN_NAME'])

        # 크기 초과 인덱스 확인
        for key, size in index_sizes.items():
            if size > max_key_length:
                table_name, index_name = key.split('.', 1)
                cols = ', '.join(index_columns[key])
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.INDEX_TOO_LARGE,
                    severity="error",
                    location=f"{schema}.{key}",
                    description=f"인덱스 크기 {size}바이트 > {max_key_length}바이트 제한 ({cols})",
                    suggestion="인덱스 컬럼 수 줄이거나 prefix 길이 지정 필요",
                    table_name=table_name
                ))

        if issues:
            self._log(f"  ⚠️ 인덱스 크기 초과 {len(issues)}개 발견")
        else:
            self._log("  ✅ 인덱스 크기 정상")

        return issues

    # ================================================================
    # S05: GROUP BY ASC/DESC 구문 검사 (덤프 파일)
    # ================================================================
    def check_groupby_asc_desc(self, content: str, location: str) -> List[CompatibilityIssue]:
        """GROUP BY ASC/DESC 구문 사용 확인 (8.4에서 제거됨)"""
        issues = []

        for match in DEPRECATED_SYNTAX_PATTERNS['GROUP_BY_ASC_DESC'].finditer(content):
            line_start = content.rfind('\n', 0, match.start()) + 1
            line_end = content.find('\n', match.end())
            line = content[line_start:line_end].strip()

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
    # S16: 생성 컬럼 함수 검사 (덤프 파일)
    # ================================================================
    def check_generated_column_functions(self, content: str, location: str) -> List[CompatibilityIssue]:
        """생성 컬럼에서 deprecated 함수 사용 확인"""
        issues = []

        for match in GENERATED_COLUMN_PATTERN.finditer(content):
            expression = match.group(1).upper()
            for func in ALL_REMOVED_FUNCTIONS:
                if func in expression:
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

        if issues:
            self._log(f"  ℹ️ Geometry 타입 {len(issues)}개 발견")
        else:
            self._log("  ✅ Geometry 타입 없음")

        return issues

    # ================================================================
    # S18: BLOB/TEXT DEFAULT 검사 (덤프 파일)
    # ================================================================
    def check_blob_text_default(self, content: str, location: str) -> List[CompatibilityIssue]:
        """BLOB/TEXT 컬럼의 DEFAULT 값 확인"""
        issues = []

        for match in BLOB_TEXT_DEFAULT_PATTERN.finditer(content):
            line_start = content.rfind('\n', 0, match.start()) + 1
            line_end = content.find('\n', match.end())
            line = content[line_start:line_end].strip()

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
    # S23: MySQL 스키마 충돌 검사 (라이브 DB)
    # ================================================================
    def check_mysql_schema_conflict(self, schema: str) -> List[CompatibilityIssue]:
        """mysql 스키마 내부 테이블명과 충돌 확인"""
        if not self.connector:
            return []

        self._log("🔍 MySQL 스키마 충돌 검사 중...")
        issues = []

        tables = self.connector.get_tables(schema)
        conflicts = [t for t in tables if t.lower() in MYSQL_SCHEMA_TABLES]

        for table in conflicts:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.MYSQL_SCHEMA_CONFLICT,
                severity="error",
                location=f"{schema}.{table}",
                description=f"테이블명 '{table}'이 mysql 스키마 내부 테이블과 충돌",
                suggestion="테이블명 변경 필요",
                table_name=table
            ))

        if issues:
            self._log(f"  ⚠️ MySQL 스키마 충돌 {len(issues)}개 발견")
        else:
            self._log("  ✅ MySQL 스키마 충돌 없음")

        return issues

    # ================================================================
    # S24-S25: Definer 검사 (라이브 DB)
    # ================================================================
    def check_routine_definer_missing(self, schema: str) -> List[CompatibilityIssue]:
        """저장 프로시저/함수의 definer가 존재하지 않는 사용자인지 확인"""
        if not self.connector:
            return []

        self._log("🔍 루틴 Definer 검사 중...")
        issues = []

        query = """
        SELECT ROUTINE_NAME, ROUTINE_TYPE, DEFINER
        FROM INFORMATION_SCHEMA.ROUTINES
        WHERE ROUTINE_SCHEMA = %s
        """
        routines = self.connector.execute(query, (schema,))

        # 현재 존재하는 사용자 목록 조회
        try:
            users_query = "SELECT CONCAT(User, '@', Host) as definer FROM mysql.user"
            users = self.connector.execute(users_query)
            existing_users = {u['definer'].lower() for u in users}
        except Exception:
            existing_users = set()

        for routine in routines:
            definer = routine.get('DEFINER', '')
            if definer and definer.lower() not in existing_users:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ROUTINE_DEFINER_MISSING,
                    severity="warning",
                    location=f"{routine['ROUTINE_TYPE']} {schema}.{routine['ROUTINE_NAME']}",
                    description=f"Definer '{definer}'가 존재하지 않음",
                    suggestion="Definer를 존재하는 사용자로 변경하거나 사용자 생성 필요"
                ))

        if issues:
            self._log(f"  ⚠️ 루틴 Definer 누락 {len(issues)}개 발견")
        else:
            self._log("  ✅ 루틴 Definer 정상")

        return issues

    def check_view_definer_missing(self, schema: str) -> List[CompatibilityIssue]:
        """뷰의 definer가 존재하지 않는 사용자인지 확인"""
        if not self.connector:
            return []

        self._log("🔍 뷰 Definer 검사 중...")
        issues = []

        query = """
        SELECT TABLE_NAME, DEFINER
        FROM INFORMATION_SCHEMA.VIEWS
        WHERE TABLE_SCHEMA = %s
        """
        views = self.connector.execute(query, (schema,))

        # 현재 존재하는 사용자 목록 조회
        try:
            users_query = "SELECT CONCAT(User, '@', Host) as definer FROM mysql.user"
            users = self.connector.execute(users_query)
            existing_users = {u['definer'].lower() for u in users}
        except Exception:
            existing_users = set()

        for view in views:
            definer = view.get('DEFINER', '')
            if definer and definer.lower() not in existing_users:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.VIEW_DEFINER_MISSING,
                    severity="warning",
                    location=f"VIEW {schema}.{view['TABLE_NAME']}",
                    description=f"Definer '{definer}'가 존재하지 않음",
                    suggestion="Definer를 존재하는 사용자로 변경하거나 사용자 생성 필요"
                ))

        if issues:
            self._log(f"  ⚠️ 뷰 Definer 누락 {len(issues)}개 발견")
        else:
            self._log("  ✅ 뷰 Definer 정상")

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
        return issues
