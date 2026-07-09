"""
인덱스/charset 규칙 믹스인 (S02-S04)

SchemaRules에 합쳐지는 latin1 charset 권장(S02)과 인덱스 크기 계산/초과
검사(S03-S04) 모음. self._log_summary / self.connector 등 공통 기능은
SchemaRules가 상속하는 ProgressLoggingRuleBase에서 온다.
"""

from typing import Dict, List

from ..migration_constants import (
    IssueType,
    CompatibilityIssue,
    INDEX_SIZE_LIMITS,
    CHARSET_BYTES_PER_CHAR,
)


class IndexCharsetRulesMixin:
    """인덱스/charset 관련 규칙 (S02-S04)"""

    # DECIMAL 인덱스 바이트 근사값 (정밀도 무시, MySQL 저장 바이트 상한 추정)
    _DECIMAL_ESTIMATED_MAX_BYTES = 16

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

        self._log_summary(issues, "latin1 사용", "latin1 사용 없음", emoji="ℹ️")

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
            # MySQL DECIMAL 저장 바이트 근사 (정밀도 무시, 최대값 추정)
            return self._DECIMAL_ESTIMATED_MAX_BYTES

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

        self._log_summary(issues, "인덱스 크기 초과", "인덱스 크기 정상")

        return issues
