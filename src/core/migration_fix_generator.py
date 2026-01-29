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

    def generate_all(self, issues: list) -> list:
        """여러 이슈에 대해 fix_query 일괄 생성"""
        return [self.generate(issue) for issue in issues]
