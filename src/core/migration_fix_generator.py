"""
Fix Query Generator 모듈

발견된 호환성 이슈에 대한 자동 수정 SQL을 생성합니다.
"""

import re
from typing import Optional

from .migration_constants import IssueType, DOC_LINKS


class CompatibilityIssue:
    """호환성 문제 (확장 버전)"""
    def __init__(
        self,
        issue_type: IssueType,
        severity: str,
        location: str,
        description: str,
        suggestion: str,
        fix_query: Optional[str] = None,
        doc_link: Optional[str] = None,
        mysql_shell_check_id: Optional[str] = None,
        code_snippet: Optional[str] = None,
        table_name: Optional[str] = None,
        column_name: Optional[str] = None
    ):
        self.issue_type = issue_type
        self.severity = severity
        self.location = location
        self.description = description
        self.suggestion = suggestion
        self.fix_query = fix_query
        self.doc_link = doc_link
        self.mysql_shell_check_id = mysql_shell_check_id
        self.code_snippet = code_snippet
        self.table_name = table_name
        self.column_name = column_name


class FixQueryGenerator:
    """호환성 이슈 수정 SQL 생성기"""

    def generate(self, issue) -> 'CompatibilityIssue':
        """이슈에 fix_query와 doc_link 추가"""
        generators = {
            IssueType.AUTH_PLUGIN_ISSUE: self._gen_auth_plugin_fix,
            IssueType.CHARSET_ISSUE: self._gen_charset_fix,
            IssueType.ZEROFILL_USAGE: self._gen_zerofill_fix,
            IssueType.FLOAT_PRECISION: self._gen_float_precision_fix,
            IssueType.INVALID_DATE: self._gen_invalid_date_fix,
            IssueType.YEAR2_TYPE: self._gen_year2_fix,
            IssueType.DEPRECATED_ENGINE: self._gen_engine_fix,
            IssueType.ENUM_EMPTY_VALUE: self._gen_enum_fix,
            IssueType.INDEX_TOO_LARGE: self._gen_index_fix,
            IssueType.FK_NAME_LENGTH: self._gen_fk_name_fix,
            IssueType.RESERVED_KEYWORD: self._gen_keyword_fix,
            IssueType.INT_DISPLAY_WIDTH: self._gen_int_display_width_fix,
            IssueType.LATIN1_CHARSET: self._gen_latin1_fix,
            IssueType.FK_NON_UNIQUE_REF: self._gen_fk_unique_fix,
            IssueType.SUPER_PRIVILEGE: self._gen_super_privilege_fix,
            # 신규 추가 Fix Generator (7개)
            IssueType.REMOVED_SYS_VAR: self._gen_removed_sysvar_fix,
            IssueType.GROUPBY_ASC_DESC: self._gen_groupby_fix,
            IssueType.SQL_CALC_FOUND_ROWS_USAGE: self._gen_found_rows_fix,
            IssueType.PARTITION_ISSUE: self._gen_partition_fix,
            IssueType.TIMESTAMP_RANGE: self._gen_timestamp_fix,
            IssueType.BLOB_TEXT_DEFAULT: self._gen_blob_default_fix,
            IssueType.DEPRECATED_FUNCTION: self._gen_deprecated_function_fix,
        }

        generator = generators.get(issue.issue_type)
        if generator:
            fix_query = generator(issue)
            if fix_query:
                issue.fix_query = fix_query

        # 문서 링크 추가
        if issue.issue_type in DOC_LINKS:
            issue.doc_link = DOC_LINKS[issue.issue_type]

        return issue

    def _gen_auth_plugin_fix(self, issue) -> Optional[str]:
        """인증 플러그인 수정 SQL"""
        # location: 'user'@'host' 형식에서 추출
        match = re.match(r"'([^']+)'@'([^']+)'", issue.location)
        if match:
            user, host = match.groups()
            return f"ALTER USER '{user}'@'{host}' IDENTIFIED WITH caching_sha2_password BY 'NEW_PASSWORD_HERE';"
        return "-- ALTER USER 'user'@'host' IDENTIFIED WITH caching_sha2_password BY 'new_password';"

    def _gen_charset_fix(self, issue) -> Optional[str]:
        """Charset 수정 SQL"""
        # location: schema.table 또는 schema.table.column
        parts = issue.location.split('.')

        if len(parts) == 2:
            # 테이블 레벨
            schema, table = parts
            return f"ALTER TABLE `{schema}`.`{table}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        elif len(parts) == 3:
            # 컬럼 레벨
            schema, table, column = parts
            return f"-- ALTER TABLE `{schema}`.`{table}` MODIFY COLUMN `{column}` ... CHARACTER SET utf8mb4;"

        return "-- ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4;"

    def _gen_zerofill_fix(self, issue) -> Optional[str]:
        """ZEROFILL 속성 제거 SQL"""
        if issue.table_name and issue.column_name:
            return f"""-- ZEROFILL 제거 및 LPAD로 포맷팅:
-- ALTER TABLE `{issue.table_name}` MODIFY COLUMN `{issue.column_name}` INT;
-- 애플리케이션에서: SELECT LPAD(`{issue.column_name}`, width, '0') FROM `{issue.table_name}`;"""
        return "-- ZEROFILL 제거 후 LPAD() 함수로 애플리케이션에서 포맷팅 처리"

    def _gen_float_precision_fix(self, issue) -> Optional[str]:
        """FLOAT(M,D) 구문 수정 SQL"""
        if issue.table_name and issue.column_name:
            return f"ALTER TABLE `{issue.table_name}` MODIFY COLUMN `{issue.column_name}` DECIMAL(10,2);  -- 또는 FLOAT"
        return "-- ALTER TABLE ... MODIFY COLUMN ... FLOAT 또는 DECIMAL(M,D)"

    def _gen_invalid_date_fix(self, issue) -> Optional[str]:
        """0000-00-00 날짜 수정 SQL"""
        if issue.table_name and issue.column_name:
            return f"""UPDATE `{issue.table_name}`
SET `{issue.column_name}` = NULL  -- 또는 적절한 기본값
WHERE `{issue.column_name}` = '0000-00-00';"""
        return "-- UPDATE table SET column = NULL WHERE column = '0000-00-00';"

    def _gen_year2_fix(self, issue) -> Optional[str]:
        """YEAR(2) → YEAR(4) 변환 SQL"""
        if issue.table_name and issue.column_name:
            return f"ALTER TABLE `{issue.table_name}` MODIFY COLUMN `{issue.column_name}` YEAR(4);"
        return "-- ALTER TABLE ... MODIFY COLUMN ... YEAR(4);"

    def _gen_engine_fix(self, issue) -> Optional[str]:
        """스토리지 엔진 변경 SQL"""
        parts = issue.location.split('.')
        if len(parts) >= 2:
            schema, table = parts[0], parts[1]
            return f"ALTER TABLE `{schema}`.`{table}` ENGINE=InnoDB;"
        elif issue.table_name:
            return f"ALTER TABLE `{issue.table_name}` ENGINE=InnoDB;"
        return "-- ALTER TABLE ... ENGINE=InnoDB;"

    def _gen_enum_fix(self, issue) -> Optional[str]:
        """ENUM 빈 값 수정 SQL"""
        if issue.table_name and issue.column_name:
            return f"""-- ENUM 정의에서 빈 문자열('') 제거 필요
-- ALTER TABLE `{issue.table_name}` MODIFY COLUMN `{issue.column_name}` ENUM('valid_value1', 'valid_value2');"""
        return "-- ENUM 정의에서 빈 문자열('') 제거 및 데이터 정제 필요"

    def _gen_index_fix(self, issue) -> Optional[str]:
        """인덱스 크기 초과 수정 SQL"""
        return """-- 인덱스 크기 초과 해결 방법:
-- 1. 인덱스 컬럼 수 줄이기: DROP INDEX ... ADD INDEX (col1, col2)
-- 2. prefix 길이 지정: ADD INDEX (varchar_col(100))
-- 3. 문자셋 변경: utf8mb4(4바이트) → latin1(1바이트) if ASCII only"""

    def _gen_fk_name_fix(self, issue) -> Optional[str]:
        """FK 이름 길이 수정 SQL"""
        if issue.table_name:
            return f"""-- FK 이름 64자 제한 해결:
-- ALTER TABLE `{issue.table_name}` DROP FOREIGN KEY `old_long_fk_name`;
-- ALTER TABLE `{issue.table_name}` ADD CONSTRAINT `shorter_fk_name` FOREIGN KEY ...;"""
        return "-- ALTER TABLE ... DROP FOREIGN KEY, ADD CONSTRAINT (64자 이하 이름)"

    def _gen_keyword_fix(self, issue) -> Optional[str]:
        """예약어 충돌 수정 SQL"""
        return f"""-- 예약어 충돌 해결 방법:
-- 1. 이름 변경 권장: RENAME TABLE `old_name` TO `new_name`;
-- 2. 또는 항상 백틱(`) 사용: SELECT * FROM `{issue.location.split('.')[-1]}`;"""

    def _gen_int_display_width_fix(self, issue) -> Optional[str]:
        """INT 표시 너비 수정 SQL"""
        if issue.table_name and issue.column_name:
            return f"-- 표시 너비는 8.4에서 자동 무시됨 (선택적 수정)\n-- ALTER TABLE `{issue.table_name}` MODIFY COLUMN `{issue.column_name}` INT;"
        return "-- INT 표시 너비는 8.4에서 자동 무시됨 (영향 최소)"

    def _gen_latin1_fix(self, issue) -> Optional[str]:
        """latin1 charset 변환 SQL"""
        parts = issue.location.split('.')
        if len(parts) >= 2:
            schema, table = parts[0], parts[1]
            return f"ALTER TABLE `{schema}`.`{table}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        return "-- ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4;"

    def _gen_fk_unique_fix(self, issue) -> Optional[str]:
        """FK 비고유 참조 수정 SQL"""
        return """-- FK가 참조하는 컬럼에 UNIQUE 인덱스 추가 필요:
-- ALTER TABLE `ref_table` ADD UNIQUE INDEX `idx_unique` (`ref_column`);"""

    def _gen_super_privilege_fix(self, issue) -> Optional[str]:
        """SUPER 권한 대체 SQL"""
        return """-- SUPER 권한을 세분화된 동적 권한으로 대체:
-- GRANT BINLOG_ADMIN ON *.* TO 'user'@'host';  -- binlog 관련
-- GRANT CONNECTION_ADMIN ON *.* TO 'user'@'host';  -- 연결 관리
-- GRANT REPLICATION_SLAVE_ADMIN ON *.* TO 'user'@'host';  -- 복제
-- GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO 'user'@'host';  -- 시스템 변수"""

    def _gen_removed_sysvar_fix(self, issue) -> Optional[str]:
        """제거된 시스템 변수 수정 가이드"""
        # description에서 변수명 추출 시도
        var_name = "unknown"
        if hasattr(issue, 'description'):
            import re
            match = re.search(r"['\"]?(\w+)['\"]?", issue.description)
            if match:
                var_name = match.group(1)

        return f"""-- 제거된 시스템 변수: {var_name}
-- MySQL 8.4에서 이 변수는 제거되었습니다.

-- 조치 사항:
-- 1. my.cnf/my.ini 설정 파일에서 해당 변수 제거
-- 2. 애플리케이션 코드에서 SET 문 제거
-- 3. 대체 변수가 있는 경우 마이그레이션

-- 설정 파일에서 제거:
-- [mysqld]
-- # {var_name} = value  -- 이 줄 제거 또는 주석 처리

-- 참고: https://dev.mysql.com/doc/refman/8.4/en/added-deprecated-removed.html"""

    def _gen_groupby_fix(self, issue) -> Optional[str]:
        """GROUP BY ASC/DESC 수정 가이드"""
        return """-- GROUP BY ASC/DESC는 MySQL 8.0에서 deprecated됨

-- 변경 전:
-- SELECT col1, COUNT(*) FROM table GROUP BY col1 ASC;

-- 변경 후:
-- SELECT col1, COUNT(*) FROM table GROUP BY col1 ORDER BY col1 ASC;

-- 참고: GROUP BY의 ASC/DESC는 정렬 순서를 보장하지 않았습니다.
-- 정렬이 필요하면 명시적으로 ORDER BY를 사용하세요."""

    def _gen_found_rows_fix(self, issue) -> Optional[str]:
        """SQL_CALC_FOUND_ROWS 대체 가이드"""
        return """-- SQL_CALC_FOUND_ROWS와 FOUND_ROWS()는 deprecated됨

-- 변경 전 (기존 패턴):
-- SELECT SQL_CALC_FOUND_ROWS * FROM users WHERE status='active' LIMIT 10;
-- SELECT FOUND_ROWS();

-- 변경 후 (권장 패턴 1: 두 개의 쿼리):
-- SELECT COUNT(*) FROM users WHERE status='active';  -- 전체 개수
-- SELECT * FROM users WHERE status='active' LIMIT 10; -- 실제 데이터

-- 변경 후 (권장 패턴 2: 윈도우 함수, MySQL 8.0+):
-- SELECT *, COUNT(*) OVER() as total_count
-- FROM users WHERE status='active' LIMIT 10;

-- 참고: 대부분의 경우 두 개의 쿼리가 더 효율적입니다."""

    def _gen_partition_fix(self, issue) -> Optional[str]:
        """파티션 이슈 수정 가이드"""
        table = getattr(issue, 'table_name', 'table_name')
        return f"""-- 파티션 재구성 필요: {table}

-- 1. 현재 파티션 구조 확인
SELECT PARTITION_NAME, PARTITION_EXPRESSION, PARTITION_DESCRIPTION,
       TABLE_ROWS, AVG_ROW_LENGTH
FROM information_schema.partitions
WHERE table_schema = DATABASE() AND table_name = '{table}';

-- 2. 파티션 유형 확인
SHOW CREATE TABLE `{table}`\\G

-- 3. 필요 시 파티션 재구성 (예시)
-- ALTER TABLE `{table}` REORGANIZE PARTITION p0, p1 INTO (
--     PARTITION p0_new VALUES LESS THAN (2025),
--     PARTITION p1_new VALUES LESS THAN (2026)
-- );

-- 참고: 파티션 변경은 데이터 양에 따라 시간이 오래 걸릴 수 있습니다.
-- 대용량 테이블은 유지보수 시간에 작업을 권장합니다."""

    def _gen_timestamp_fix(self, issue) -> Optional[str]:
        """TIMESTAMP 범위 수정 SQL"""
        table = getattr(issue, 'table_name', None)
        column = getattr(issue, 'column_name', None)

        if table and column:
            return f"""-- TIMESTAMP 범위 이슈: {table}.{column}

-- TIMESTAMP는 1970-01-01 00:00:01 ~ 2038-01-19 03:14:07 범위만 지원 (2038년 문제)
-- 이 범위를 벗어나는 값이 있거나 필요하면 DATETIME으로 변경 권장

-- 1. 범위 초과 데이터 확인:
SELECT COUNT(*) FROM `{table}`
WHERE `{column}` < '1970-01-01 00:00:01'
   OR `{column}` > '2038-01-19 03:14:07';

-- 2. DATETIME으로 타입 변경:
ALTER TABLE `{table}` MODIFY COLUMN `{column}` DATETIME;

-- 참고: TIMESTAMP는 시간대 변환이 자동 적용됩니다.
-- DATETIME으로 변경 시 시간대 처리가 달라질 수 있으니 확인하세요."""

        return """-- TIMESTAMP 범위 이슈 (2038년 문제)
-- DATETIME으로 타입 변경을 권장합니다.
-- ALTER TABLE table_name MODIFY COLUMN column_name DATETIME;"""

    def _gen_blob_default_fix(self, issue) -> Optional[str]:
        """BLOB/TEXT DEFAULT 수정 SQL"""
        table = getattr(issue, 'table_name', None)
        column = getattr(issue, 'column_name', None)

        if table and column:
            return f"""-- BLOB/TEXT 컬럼에는 DEFAULT 값을 지정할 수 없음: {table}.{column}

-- DEFAULT 제거:
ALTER TABLE `{table}` ALTER COLUMN `{column}` DROP DEFAULT;

-- 또는 컬럼 재정의 (DEFAULT 없이):
-- ALTER TABLE `{table}` MODIFY COLUMN `{column}` TEXT;

-- 참고: INSERT 시 애플리케이션에서 명시적으로 값을 지정하도록 수정 필요
-- 또는 트리거를 사용하여 기본값 설정 가능"""

        return """-- BLOB/TEXT 컬럼에는 DEFAULT 값을 지정할 수 없습니다.
-- ALTER TABLE ... ALTER COLUMN ... DROP DEFAULT;"""

    def _gen_deprecated_function_fix(self, issue) -> Optional[str]:
        """Deprecated 함수 대체 가이드"""
        # 함수별 대체 방법 매핑
        replacements = {
            'PASSWORD': 'SHA2() 또는 애플리케이션 레벨 해싱',
            'ENCODE': 'AES_ENCRYPT()',
            'DECODE': 'AES_DECRYPT()',
            'DES_ENCRYPT': 'AES_ENCRYPT()',
            'DES_DECRYPT': 'AES_DECRYPT()',
            'ENCRYPT': 'SHA2() 또는 caching_sha2_password',
            'OLD_PASSWORD': 'caching_sha2_password 인증 사용',
            'MASTER_POS_WAIT': 'SOURCE_POS_WAIT() (MySQL 8.0.26+)',
        }

        # description에서 함수명 추출
        func_name = "unknown"
        if hasattr(issue, 'description'):
            for key in replacements.keys():
                if key in issue.description.upper():
                    func_name = key
                    break

        replacement = replacements.get(func_name, '대체 함수 확인 필요')

        return f"""-- Deprecated 함수 '{func_name}' 사용 감지

-- 대체 방법: {replacement}

-- 함수별 대체 예시:
-- PASSWORD() → 애플리케이션에서 bcrypt 또는 SHA256 사용
-- ENCODE()/DECODE() → AES_ENCRYPT(str, key), AES_DECRYPT(str, key)
-- DES_ENCRYPT()/DES_DECRYPT() → AES_ENCRYPT(), AES_DECRYPT()
-- ENCRYPT() → SHA2(str, 256) 또는 애플리케이션 레벨 해싱
-- MASTER_POS_WAIT() → SOURCE_POS_WAIT() (8.0.26+)

-- 참고: 저장 프로시저/함수/트리거 내의 사용도 확인하세요."""

    def generate_all(self, issues: list) -> list:
        """여러 이슈에 대해 fix_query 일괄 생성"""
        return [self.generate(issue) for issue in issues]
