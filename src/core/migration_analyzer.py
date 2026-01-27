"""
MySQL 마이그레이션 분석기
- 고아 레코드(orphan rows) 탐지
- FK 관계 분석 및 정리
- MySQL 8.0.x → 8.4.x 호환성 검사 (Upgrade Checker 통합)
- dry-run 지원
- 덤프 파일 분석 (SQL/TSV)
"""
import re
from typing import List, Dict, Set, Tuple, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from src.core.db_connector import MySQLConnector


# ============================================================
# MySQL 8.4 Upgrade Checker 상수 (constants.ts에서 포팅)
# ============================================================

# MySQL 8.4에서 제거된 시스템 변수 (47개)
REMOVED_SYS_VARS_84 = (
    'avoid_temporal_upgrade',
    'binlog_transaction_dependency_tracking',
    'default_authentication_plugin',
    'group_replication_ip_allowlist',
    'group_replication_recovery_complete_at',
    'have_openssl',
    'have_ssl',
    'innodb_log_file_size',
    'innodb_log_files_in_group',
    'keyring_file_data',
    'keyring_file_data_file',
    'keyring_encrypted_file_data',
    'keyring_encrypted_file_password',
    'keyring_okv_conf_dir',
    'keyring_hashicorp_auth_path',
    'keyring_hashicorp_ca_path',
    'keyring_hashicorp_caching',
    'keyring_hashicorp_commit_auth_path',
    'keyring_hashicorp_commit_caching',
    'keyring_hashicorp_commit_role_id',
    'keyring_hashicorp_commit_server_url',
    'keyring_hashicorp_commit_store_path',
    'keyring_hashicorp_role_id',
    'keyring_hashicorp_secret_id',
    'keyring_hashicorp_server_url',
    'keyring_hashicorp_store_path',
    'keyring_aws_cmk_id',
    'keyring_aws_conf_file',
    'keyring_aws_data_file',
    'keyring_aws_region',
    'log_bin_use_v1_row_events',
    'master_verify_checksum',
    'old_alter_table',
    'relay_log_info_file',
    'relay_log_info_repository',
    'replica_parallel_type',
    'slave_parallel_type',
    'slave_rows_search_algorithms',
    'sql_slave_skip_counter',
    'sync_master_info',
    'sync_relay_log',
    'sync_relay_log_info',
    'transaction_write_set_extraction',
    'binlog_format',
    'log_slave_updates',
    'replica_compressed_protocol',
    'slave_compressed_protocol',
)

# MySQL 8.4에서 추가된 새 예약어 (4개)
NEW_RESERVED_KEYWORDS_84 = ('MANUAL', 'PARALLEL', 'QUALIFY', 'TABLESAMPLE')

# MySQL 8.4에서 제거된 함수 (6개) - 기존 DEPRECATED_FUNCTIONS와 병합
REMOVED_FUNCTIONS_84 = (
    'PASSWORD', 'ENCODE', 'DECODE', 'DES_ENCRYPT', 'DES_DECRYPT',
    'ENCRYPT', 'OLD_PASSWORD', 'MASTER_POS_WAIT'
)

# 인증 플러그인 상태
AUTH_PLUGINS = {
    'disabled': ['mysql_native_password'],  # 8.4에서 기본 비활성화
    'removed': ['authentication_fido', 'authentication_fido_client'],  # 8.4에서 제거
    'deprecated': ['sha256_password'],  # deprecated, caching_sha2_password 권장
}

# 제거된/deprecated SQL 모드 (10개)
OBSOLETE_SQL_MODES = (
    'DB2', 'MAXDB', 'MSSQL', 'MYSQL323', 'MYSQL40',
    'ORACLE', 'POSTGRESQL', 'NO_FIELD_OPTIONS', 'NO_KEY_OPTIONS',
    'NO_TABLE_OPTIONS', 'NO_AUTO_CREATE_USER',
)

# 기본값이 변경된 시스템 변수
SYS_VARS_NEW_DEFAULTS_84 = {
    'binlog_transaction_dependency_tracking': {
        'old': 'COMMIT_ORDER', 'new': 'removed (use replica_parallel_type)',
    },
    'replica_parallel_workers': {
        'old': '0', 'new': '4',
    },
    'innodb_adaptive_hash_index': {
        'old': 'ON', 'new': 'OFF',
    },
    'innodb_doublewrite_pages': {
        'old': '(innodb_write_io_threads)', 'new': '128',
    },
    'innodb_flush_method': {
        'old': 'fsync (Unix)', 'new': 'O_DIRECT (Linux)',
    },
    'innodb_io_capacity': {
        'old': '200', 'new': '10000',
    },
    'innodb_io_capacity_max': {
        'old': '2 * innodb_io_capacity', 'new': '2 * new default',
    },
    'innodb_log_buffer_size': {
        'old': '16M', 'new': '64M',
    },
    'innodb_redo_log_capacity': {
        'old': '100M', 'new': '100M (now replaces log_file_size * files)',
    },
    'group_replication_consistency': {
        'old': 'EVENTUAL', 'new': 'BEFORE_ON_PRIMARY_FAILOVER',
    },
}

# ============================================================
# 덤프 파일 분석용 정규식 패턴 (rules.ts에서 포팅)
# ============================================================

# 0000-00-00 날짜 (잘못된 날짜)
INVALID_DATE_PATTERN = re.compile(r"['\"]0000-00-00['\"]|^0000-00-00$", re.MULTILINE)
INVALID_DATETIME_PATTERN = re.compile(r"['\"]0000-00-00 00:00:00['\"]|^0000-00-00 00:00:00$", re.MULTILINE)

# ZEROFILL 속성
ZEROFILL_PATTERN = re.compile(r'\bZEROFILL\b', re.IGNORECASE)

# FLOAT(M,D), DOUBLE(M,D) 구문 (deprecated)
FLOAT_PRECISION_PATTERN = re.compile(
    r'\b(FLOAT|DOUBLE|REAL)\s*\(\s*\d+\s*,\s*\d+\s*\)',
    re.IGNORECASE
)

# INT 표시 너비 (deprecated, TINYINT(1) 제외)
INT_DISPLAY_WIDTH_PATTERN = re.compile(
    r'\b(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT)\s*\(\s*(\d+)\s*\)',
    re.IGNORECASE
)

# FK 이름 길이 (64자 초과)
FK_NAME_LENGTH_PATTERN = re.compile(
    r'CONSTRAINT\s+`?(\w{65,})`?\s+FOREIGN\s+KEY',
    re.IGNORECASE
)

# mysql_native_password 인증 플러그인
AUTH_PLUGIN_PATTERN = re.compile(
    r"IDENTIFIED\s+(?:WITH\s+)?['\"]?(mysql_native_password|sha256_password|authentication_fido)['\"]?",
    re.IGNORECASE
)

# FTS_ 접두사 테이블명 (내부 예약)
FTS_TABLE_PREFIX_PATTERN = re.compile(r'CREATE\s+TABLE\s+`?FTS_', re.IGNORECASE)

# GRANT 문의 SUPER 권한
SUPER_PRIVILEGE_PATTERN = re.compile(r'\bGRANT\b.*\bSUPER\b', re.IGNORECASE)

# 제거된 시스템 변수 사용 (SET/SELECT 문에서)
SYS_VAR_USAGE_PATTERN = re.compile(
    r"(?:SET|SELECT)\s+.*(?:@@(?:global|session)?\.)?" +
    r"(" + "|".join(re.escape(v) for v in REMOVED_SYS_VARS_84) + r")\b",
    re.IGNORECASE
)


class IssueType(Enum):
    """문제 유형"""
    # 기존 이슈 타입
    ORPHAN_ROW = "orphan_row"  # 부모 없는 자식 레코드
    DEPRECATED_FUNCTION = "deprecated_function"  # deprecated 함수 사용
    CHARSET_ISSUE = "charset_issue"  # utf8mb3 → utf8mb4 필요
    RESERVED_KEYWORD = "reserved_keyword"  # 예약어 충돌
    SQL_MODE_ISSUE = "sql_mode_issue"  # deprecated SQL 모드

    # MySQL 8.4 Upgrade Checker 이슈 타입 (신규)
    REMOVED_SYS_VAR = "removed_sys_var"  # 제거된 시스템 변수
    AUTH_PLUGIN_ISSUE = "auth_plugin_issue"  # 인증 플러그인 이슈
    INVALID_DATE = "invalid_date"  # 0000-00-00 날짜
    ZEROFILL_USAGE = "zerofill_usage"  # ZEROFILL 속성
    FLOAT_PRECISION = "float_precision"  # FLOAT(M,D) 구문
    INT_DISPLAY_WIDTH = "int_display_width"  # INT(11) 표시 너비
    FK_NAME_LENGTH = "fk_name_length"  # FK 이름 64자 초과
    FTS_TABLE_PREFIX = "fts_table_prefix"  # FTS_ 테이블명
    SUPER_PRIVILEGE = "super_privilege"  # SUPER 권한 사용
    DEFAULT_VALUE_CHANGE = "default_value_change"  # 기본값 변경됨


class ActionType(Enum):
    """조치 유형"""
    DELETE = "delete"  # 삭제
    UPDATE = "update"  # 업데이트
    SET_NULL = "set_null"  # NULL로 설정
    MANUAL = "manual"  # 수동 처리 필요


@dataclass
class OrphanRecord:
    """고아 레코드 정보"""
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    orphan_count: int
    sample_values: List[Any] = field(default_factory=list)


@dataclass
class ForeignKeyInfo:
    """FK 관계 정보"""
    constraint_name: str
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    on_delete: str
    on_update: str


@dataclass
class CompatibilityIssue:
    """호환성 문제"""
    issue_type: IssueType
    severity: str  # "error", "warning", "info"
    location: str  # 테이블명 또는 위치
    description: str
    suggestion: str


@dataclass
class CleanupAction:
    """정리 작업"""
    action_type: ActionType
    table: str
    description: str
    sql: str
    affected_rows: int
    dry_run: bool = True


@dataclass
class AnalysisResult:
    """분석 결과"""
    schema: str
    analyzed_at: str
    total_tables: int
    total_fk_relations: int
    orphan_records: List[OrphanRecord] = field(default_factory=list)
    compatibility_issues: List[CompatibilityIssue] = field(default_factory=list)
    cleanup_actions: List[CleanupAction] = field(default_factory=list)
    fk_tree: Dict[str, List[str]] = field(default_factory=dict)


class MigrationAnalyzer:
    """마이그레이션 분석기"""

    # MySQL 8.4에서 제거된/deprecated된 함수들 (전역 상수 사용)
    DEPRECATED_FUNCTIONS = list(REMOVED_FUNCTIONS_84)

    # MySQL 8.4에서 새로운 예약어들 (기존 22개 + 8.4 추가 4개)
    NEW_RESERVED_KEYWORDS = [
        'CUME_DIST', 'DENSE_RANK', 'EMPTY', 'EXCEPT', 'FIRST_VALUE',
        'GROUPING', 'GROUPS', 'JSON_TABLE', 'LAG', 'LAST_VALUE', 'LATERAL',
        'LEAD', 'NTH_VALUE', 'NTILE', 'OF', 'OVER', 'PERCENT_RANK',
        'RANK', 'RECURSIVE', 'ROW_NUMBER', 'SYSTEM', 'WINDOW',
        # MySQL 8.4 추가 예약어
        'MANUAL', 'PARALLEL', 'QUALIFY', 'TABLESAMPLE'
    ]

    def __init__(self, connector: MySQLConnector):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def get_foreign_keys(self, schema: str) -> List[ForeignKeyInfo]:
        """스키마의 모든 FK 관계 조회"""
        query = """
        SELECT
            tc.CONSTRAINT_NAME,
            kcu.TABLE_NAME as CHILD_TABLE,
            kcu.COLUMN_NAME as CHILD_COLUMN,
            kcu.REFERENCED_TABLE_NAME as PARENT_TABLE,
            kcu.REFERENCED_COLUMN_NAME as PARENT_COLUMN,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        WHERE tc.TABLE_SCHEMA = %s
            AND tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
        ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
        """
        rows = self.connector.execute(query, (schema,))

        fk_list = []
        for row in rows:
            fk_list.append(ForeignKeyInfo(
                constraint_name=row['CONSTRAINT_NAME'],
                child_table=row['CHILD_TABLE'],
                child_column=row['CHILD_COLUMN'],
                parent_table=row['PARENT_TABLE'],
                parent_column=row['PARENT_COLUMN'],
                on_delete=row['DELETE_RULE'],
                on_update=row['UPDATE_RULE']
            ))

        return fk_list

    def build_fk_tree(self, schema: str) -> Dict[str, List[str]]:
        """FK 관계 트리 구성 (부모 → 자식 목록)"""
        fk_list = self.get_foreign_keys(schema)

        tree = {}
        for fk in fk_list:
            if fk.parent_table not in tree:
                tree[fk.parent_table] = []
            if fk.child_table not in tree[fk.parent_table]:
                tree[fk.parent_table].append(fk.child_table)

        return tree

    def find_orphan_records(
        self,
        schema: str,
        sample_limit: int = 5
    ) -> List[OrphanRecord]:
        """고아 레코드 탐지 (부모 없는 자식 레코드)"""
        self._log("🔍 고아 레코드 탐지 중...")

        fk_list = self.get_foreign_keys(schema)
        orphans = []

        for i, fk in enumerate(fk_list, 1):
            self._log(f"  검사 중: {fk.child_table}.{fk.child_column} → {fk.parent_table}.{fk.parent_column} ({i}/{len(fk_list)})")

            # 고아 레코드 수 조회
            count_query = f"""
            SELECT COUNT(*) as cnt
            FROM `{schema}`.`{fk.child_table}` c
            LEFT JOIN `{schema}`.`{fk.parent_table}` p
                ON c.`{fk.child_column}` = p.`{fk.parent_column}`
            WHERE c.`{fk.child_column}` IS NOT NULL
                AND p.`{fk.parent_column}` IS NULL
            """
            result = self.connector.execute(count_query)
            orphan_count = result[0]['cnt'] if result else 0

            if orphan_count > 0:
                # 샘플 값 조회
                sample_query = f"""
                SELECT DISTINCT c.`{fk.child_column}` as orphan_value
                FROM `{schema}`.`{fk.child_table}` c
                LEFT JOIN `{schema}`.`{fk.parent_table}` p
                    ON c.`{fk.child_column}` = p.`{fk.parent_column}`
                WHERE c.`{fk.child_column}` IS NOT NULL
                    AND p.`{fk.parent_column}` IS NULL
                LIMIT {sample_limit}
                """
                samples = self.connector.execute(sample_query)
                sample_values = [s['orphan_value'] for s in samples]

                orphans.append(OrphanRecord(
                    child_table=fk.child_table,
                    child_column=fk.child_column,
                    parent_table=fk.parent_table,
                    parent_column=fk.parent_column,
                    orphan_count=orphan_count,
                    sample_values=sample_values
                ))

                self._log(f"    ⚠️ 고아 레코드 발견: {orphan_count}개")

        return orphans

    def check_charset_issues(self, schema: str) -> List[CompatibilityIssue]:
        """utf8mb3 사용 테이블/컬럼 확인"""
        self._log("🔍 문자셋 이슈 확인 중...")

        issues = []

        # 테이블 레벨 charset 확인
        table_query = """
        SELECT TABLE_NAME, TABLE_COLLATION
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
            AND TABLE_TYPE = 'BASE TABLE'
            AND (TABLE_COLLATION LIKE 'utf8_%%' OR TABLE_COLLATION LIKE 'utf8mb3_%%')
        """
        tables = self.connector.execute(table_query, (schema,))

        for t in tables:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.CHARSET_ISSUE,
                severity="warning",
                location=f"{schema}.{t['TABLE_NAME']}",
                description=f"테이블이 utf8mb3 collation 사용 중: {t['TABLE_COLLATION']}",
                suggestion="ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            ))

        # 컬럼 레벨 charset 확인
        column_query = """
        SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND CHARACTER_SET_NAME IN ('utf8', 'utf8mb3')
        """
        columns = self.connector.execute(column_query, (schema,))

        for c in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.CHARSET_ISSUE,
                severity="warning",
                location=f"{schema}.{c['TABLE_NAME']}.{c['COLUMN_NAME']}",
                description=f"컬럼이 utf8mb3 사용 중: {c['CHARACTER_SET_NAME']}",
                suggestion="ALTER TABLE ... MODIFY COLUMN ... CHARACTER SET utf8mb4"
            ))

        if issues:
            self._log(f"  ⚠️ 문자셋 이슈 {len(issues)}개 발견")
        else:
            self._log("  ✅ 문자셋 이슈 없음")

        return issues

    def check_reserved_keywords(self, schema: str) -> List[CompatibilityIssue]:
        """예약어와 충돌하는 컬럼/테이블명 확인"""
        self._log("🔍 예약어 충돌 확인 중...")

        issues = []
        keywords_upper = set(k.upper() for k in self.NEW_RESERVED_KEYWORDS)

        # 테이블명 확인
        tables = self.connector.get_tables(schema)
        for table in tables:
            if table.upper() in keywords_upper:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.RESERVED_KEYWORD,
                    severity="error",
                    location=f"{schema}.{table}",
                    description=f"테이블명 '{table}'이 MySQL 8.4 예약어와 충돌",
                    suggestion=f"테이블명을 백틱으로 감싸거나 이름 변경 필요"
                ))

        # 컬럼명 확인
        column_query = """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """
        columns = self.connector.execute(column_query, (schema,))

        for c in columns:
            if c['COLUMN_NAME'].upper() in keywords_upper:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.RESERVED_KEYWORD,
                    severity="warning",
                    location=f"{schema}.{c['TABLE_NAME']}.{c['COLUMN_NAME']}",
                    description=f"컬럼명 '{c['COLUMN_NAME']}'이 MySQL 8.4 예약어와 충돌",
                    suggestion="컬럼 참조 시 백틱(`) 사용 필요"
                ))

        if issues:
            self._log(f"  ⚠️ 예약어 충돌 {len(issues)}개 발견")
        else:
            self._log("  ✅ 예약어 충돌 없음")

        return issues

    def check_deprecated_in_routines(self, schema: str) -> List[CompatibilityIssue]:
        """저장 프로시저/함수에서 deprecated 함수 사용 확인"""
        self._log("🔍 저장 프로시저/함수 검사 중...")

        issues = []

        # 저장 프로시저와 함수 조회
        routine_query = """
        SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION
        FROM INFORMATION_SCHEMA.ROUTINES
        WHERE ROUTINE_SCHEMA = %s
            AND ROUTINE_DEFINITION IS NOT NULL
        """
        routines = self.connector.execute(routine_query, (schema,))

        for routine in routines:
            definition = routine['ROUTINE_DEFINITION'].upper() if routine['ROUTINE_DEFINITION'] else ""

            for func in self.DEPRECATED_FUNCTIONS:
                if func in definition:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.DEPRECATED_FUNCTION,
                        severity="error",
                        location=f"{routine['ROUTINE_TYPE']} {schema}.{routine['ROUTINE_NAME']}",
                        description=f"deprecated 함수 '{func}' 사용 중",
                        suggestion=f"'{func}' 함수를 대체 함수로 변경 필요"
                    ))

        if issues:
            self._log(f"  ⚠️ deprecated 함수 사용 {len(issues)}개 발견")
        else:
            self._log("  ✅ deprecated 함수 없음")

        return issues

    def check_sql_modes(self) -> List[CompatibilityIssue]:
        """현재 SQL 모드 확인"""
        self._log("🔍 SQL 모드 확인 중...")

        issues = []

        # deprecated SQL 모드들
        deprecated_modes = [
            'NO_AUTO_CREATE_USER',  # 8.0에서 제거됨
            'NO_FIELD_OPTIONS',
            'NO_KEY_OPTIONS',
            'NO_TABLE_OPTIONS',
        ]

        result = self.connector.execute("SELECT @@sql_mode as sql_mode")
        if result:
            current_modes = result[0]['sql_mode'].split(',')

            for mode in current_modes:
                mode = mode.strip()
                if mode in deprecated_modes:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.SQL_MODE_ISSUE,
                        severity="warning",
                        location="@@sql_mode",
                        description=f"deprecated SQL 모드 '{mode}' 사용 중",
                        suggestion=f"sql_mode에서 '{mode}' 제거 필요"
                    ))

        if issues:
            self._log(f"  ⚠️ deprecated SQL 모드 {len(issues)}개 발견")
        else:
            self._log("  ✅ SQL 모드 정상")

        return issues

    def generate_cleanup_sql(
        self,
        orphan: OrphanRecord,
        action: ActionType,
        schema: str,
        dry_run: bool = True
    ) -> CleanupAction:
        """고아 레코드 정리 SQL 생성"""
        if action == ActionType.DELETE:
            sql = f"""DELETE FROM `{schema}`.`{orphan.child_table}`
WHERE `{orphan.child_column}` NOT IN (
    SELECT `{orphan.parent_column}` FROM `{schema}`.`{orphan.parent_table}`
)
AND `{orphan.child_column}` IS NOT NULL"""
            description = f"{orphan.child_table}에서 고아 레코드 {orphan.orphan_count}개 삭제"

        elif action == ActionType.SET_NULL:
            sql = f"""UPDATE `{schema}`.`{orphan.child_table}`
SET `{orphan.child_column}` = NULL
WHERE `{orphan.child_column}` NOT IN (
    SELECT `{orphan.parent_column}` FROM `{schema}`.`{orphan.parent_table}`
)
AND `{orphan.child_column}` IS NOT NULL"""
            description = f"{orphan.child_table}.{orphan.child_column}을 NULL로 설정 ({orphan.orphan_count}개)"

        else:
            sql = f"-- 수동 처리 필요: {orphan.child_table}.{orphan.child_column}"
            description = f"{orphan.child_table} 수동 검토 필요"

        return CleanupAction(
            action_type=action,
            table=orphan.child_table,
            description=description,
            sql=sql,
            affected_rows=orphan.orphan_count,
            dry_run=dry_run
        )

    def execute_cleanup(
        self,
        action: CleanupAction,
        dry_run: bool = True
    ) -> Tuple[bool, str, int]:
        """
        정리 작업 실행

        Args:
            action: 실행할 정리 작업
            dry_run: True면 실제 실행하지 않고 영향받는 행 수만 반환

        Returns:
            (성공여부, 메시지, 영향받은 행 수)
        """
        if dry_run:
            # dry-run: 실제 실행하지 않고 영향받는 행 수 확인
            self._log(f"🔍 [DRY-RUN] 영향 분석: {action.table}")

            if action.action_type == ActionType.MANUAL:
                return True, "수동 처리 필요", 0

            # COUNT 쿼리로 변환하여 영향받는 행 수 확인
            # DELETE/UPDATE의 WHERE 절 추출
            sql_upper = action.sql.upper()
            if 'WHERE' in sql_upper:
                where_idx = action.sql.upper().find('WHERE')
                where_clause = action.sql[where_idx:]

                # 테이블명 추출
                if action.action_type == ActionType.DELETE:
                    # DELETE FROM `schema`.`table` WHERE ...
                    count_sql = f"SELECT COUNT(*) as cnt FROM {action.sql.split('FROM')[1].split('WHERE')[0].strip()} {where_clause}"
                else:
                    # UPDATE `schema`.`table` SET ... WHERE ...
                    count_sql = f"SELECT COUNT(*) as cnt FROM {action.sql.split('UPDATE')[1].split('SET')[0].strip()} {where_clause}"

                result = self.connector.execute(count_sql)
                affected = result[0]['cnt'] if result else 0

                return True, f"[DRY-RUN] {affected}개 행이 영향받음", affected

            return True, "[DRY-RUN] 영향 분석 완료", action.affected_rows

        else:
            # 실제 실행
            self._log(f"🔧 실행 중: {action.table}")

            try:
                with self.connector.connection.cursor() as cursor:
                    cursor.execute(action.sql)
                    affected = cursor.rowcount
                    self.connector.connection.commit()

                return True, f"✅ {affected}개 행 처리됨", affected

            except Exception as e:
                self.connector.connection.rollback()
                return False, f"❌ 오류: {str(e)}", 0

    def analyze_schema(
        self,
        schema: str,
        check_orphans: bool = True,
        check_charset: bool = True,
        check_keywords: bool = True,
        check_routines: bool = True,
        check_sql_mode: bool = True,
        check_auth_plugins: bool = True,
        check_zerofill: bool = True,
        check_float_precision: bool = True,
        check_fk_name_length: bool = True
    ) -> AnalysisResult:
        """
        스키마 전체 분석

        Args:
            schema: 분석할 스키마명
            check_orphans: 고아 레코드 검사 여부
            check_charset: 문자셋 이슈 검사 여부
            check_keywords: 예약어 충돌 검사 여부
            check_routines: 저장 프로시저/함수 검사 여부
            check_sql_mode: SQL 모드 검사 여부
            check_auth_plugins: 인증 플러그인 검사 여부
            check_zerofill: ZEROFILL 속성 검사 여부
            check_float_precision: FLOAT(M,D) 구문 검사 여부
            check_fk_name_length: FK 이름 길이 검사 여부

        Returns:
            AnalysisResult
        """
        from datetime import datetime

        self._log(f"📊 스키마 '{schema}' 분석 시작...")

        # 기본 정보 수집
        tables = self.connector.get_tables(schema)
        fk_list = self.get_foreign_keys(schema)
        fk_tree = self.build_fk_tree(schema)

        self._log(f"  테이블 수: {len(tables)}, FK 관계: {len(fk_list)}")

        result = AnalysisResult(
            schema=schema,
            analyzed_at=datetime.now().isoformat(),
            total_tables=len(tables),
            total_fk_relations=len(fk_list),
            fk_tree=fk_tree
        )

        # 고아 레코드 검사
        if check_orphans and fk_list:
            result.orphan_records = self.find_orphan_records(schema)

        # 호환성 검사들 (기존)
        if check_charset:
            result.compatibility_issues.extend(self.check_charset_issues(schema))

        if check_keywords:
            result.compatibility_issues.extend(self.check_reserved_keywords(schema))

        if check_routines:
            result.compatibility_issues.extend(self.check_deprecated_in_routines(schema))

        if check_sql_mode:
            result.compatibility_issues.extend(self.check_sql_modes())

        # MySQL 8.4 Upgrade Checker 검사들 (신규)
        if check_auth_plugins:
            result.compatibility_issues.extend(self.check_auth_plugins())

        if check_zerofill:
            result.compatibility_issues.extend(self.check_zerofill_columns(schema))

        if check_float_precision:
            result.compatibility_issues.extend(self.check_float_precision(schema))

        if check_fk_name_length:
            result.compatibility_issues.extend(self.check_fk_name_length(schema))

        # 정리 작업 생성 (고아 레코드에 대해)
        for orphan in result.orphan_records:
            # 기본적으로 DELETE 작업 생성 (dry-run)
            cleanup = self.generate_cleanup_sql(orphan, ActionType.DELETE, schema, dry_run=True)
            result.cleanup_actions.append(cleanup)

        self._log(f"✅ 분석 완료")
        self._log(f"  - 고아 레코드: {len(result.orphan_records)}개 FK 관계에서 발견")
        self._log(f"  - 호환성 이슈: {len(result.compatibility_issues)}개")

        return result

    # ============================================================
    # MySQL 8.4 Upgrade Checker 검사 메서드들 (신규)
    # ============================================================

    def check_auth_plugins(self) -> List[CompatibilityIssue]:
        """mysql_native_password, sha256_password 사용자 확인"""
        self._log("🔍 인증 플러그인 확인 중...")

        issues = []

        # 사용자별 인증 플러그인 조회
        query = """
        SELECT User, Host, plugin
        FROM mysql.user
        WHERE plugin IN ('mysql_native_password', 'sha256_password', 'authentication_fido')
        """
        try:
            users = self.connector.execute(query)

            for user in users:
                plugin = user['plugin']

                if plugin == 'mysql_native_password':
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                        severity="error",
                        location=f"'{user['User']}'@'{user['Host']}'",
                        description=f"mysql_native_password 인증 사용 (8.4에서 기본 비활성화)",
                        suggestion="ALTER USER ... IDENTIFIED WITH caching_sha2_password"
                    ))
                elif plugin == 'sha256_password':
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                        severity="warning",
                        location=f"'{user['User']}'@'{user['Host']}'",
                        description=f"sha256_password 인증 사용 (deprecated)",
                        suggestion="ALTER USER ... IDENTIFIED WITH caching_sha2_password 권장"
                    ))
                elif plugin == 'authentication_fido':
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                        severity="error",
                        location=f"'{user['User']}'@'{user['Host']}'",
                        description=f"authentication_fido 플러그인 사용 (8.4에서 제거됨)",
                        suggestion="authentication_webauthn 또는 다른 인증 방식으로 변경 필요"
                    ))

            if issues:
                self._log(f"  ⚠️ 인증 플러그인 이슈 {len(issues)}개 발견")
            else:
                self._log("  ✅ 인증 플러그인 정상")

        except Exception as e:
            self._log(f"  ⚠️ 인증 플러그인 확인 실패: {str(e)}")

        return issues

    def check_zerofill_columns(self, schema: str) -> List[CompatibilityIssue]:
        """ZEROFILL 속성 사용 컬럼 확인"""
        self._log("🔍 ZEROFILL 속성 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND COLUMN_TYPE LIKE '%%ZEROFILL%%'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.ZEROFILL_USAGE,
                severity="warning",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"ZEROFILL 속성 사용: {col['COLUMN_TYPE']}",
                suggestion="ZEROFILL은 deprecated됨, 애플리케이션에서 LPAD() 등으로 처리 권장"
            ))

        if issues:
            self._log(f"  ⚠️ ZEROFILL 사용 {len(issues)}개 발견")
        else:
            self._log("  ✅ ZEROFILL 사용 없음")

        return issues

    def check_float_precision(self, schema: str) -> List[CompatibilityIssue]:
        """FLOAT(M,D), DOUBLE(M,D) 구문 확인"""
        self._log("🔍 FLOAT/DOUBLE 정밀도 구문 확인 중...")

        issues = []

        # FLOAT(M,D), DOUBLE(M,D) 형태 확인
        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('float', 'double')
            AND COLUMN_TYPE REGEXP '^(float|double)\\\\([0-9]+,[0-9]+\\\\)'
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.FLOAT_PRECISION,
                severity="warning",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"FLOAT/DOUBLE 정밀도 구문 사용: {col['COLUMN_TYPE']}",
                suggestion="FLOAT(M,D) 구문은 deprecated됨, FLOAT 또는 DECIMAL(M,D) 사용 권장"
            ))

        if issues:
            self._log(f"  ⚠️ FLOAT/DOUBLE 정밀도 구문 {len(issues)}개 발견")
        else:
            self._log("  ✅ FLOAT/DOUBLE 구문 정상")

        return issues

    def check_fk_name_length(self, schema: str) -> List[CompatibilityIssue]:
        """FK 이름 64자 초과 확인"""
        self._log("🔍 FK 이름 길이 확인 중...")

        issues = []

        query = """
        SELECT CONSTRAINT_NAME, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = %s
            AND CONSTRAINT_TYPE = 'FOREIGN KEY'
            AND LENGTH(CONSTRAINT_NAME) > 64
        """
        fks = self.connector.execute(query, (schema,))

        for fk in fks:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.FK_NAME_LENGTH,
                severity="error",
                location=f"{schema}.{fk['TABLE_NAME']}.{fk['CONSTRAINT_NAME']}",
                description=f"FK 이름이 64자 초과: {len(fk['CONSTRAINT_NAME'])}자",
                suggestion="FK 이름을 64자 이하로 변경 필요 (8.4 제한)"
            ))

        if issues:
            self._log(f"  ⚠️ FK 이름 길이 초과 {len(issues)}개 발견")
        else:
            self._log("  ✅ FK 이름 길이 정상")

        return issues

    def check_int_display_width(self, schema: str) -> List[CompatibilityIssue]:
        """INT(11) 등 표시 너비 사용 확인 (TINYINT(1) 제외)"""
        self._log("🔍 INT 표시 너비 확인 중...")

        issues = []

        query = """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
            AND DATA_TYPE IN ('tinyint', 'smallint', 'mediumint', 'int', 'bigint')
            AND COLUMN_TYPE REGEXP '^(tinyint|smallint|mediumint|int|bigint)\\\\([0-9]+\\\\)'
            AND NOT (DATA_TYPE = 'tinyint' AND COLUMN_TYPE LIKE 'tinyint(1)%%')
        """
        columns = self.connector.execute(query, (schema,))

        for col in columns:
            issues.append(CompatibilityIssue(
                issue_type=IssueType.INT_DISPLAY_WIDTH,
                severity="info",
                location=f"{schema}.{col['TABLE_NAME']}.{col['COLUMN_NAME']}",
                description=f"INT 표시 너비 사용: {col['COLUMN_TYPE']}",
                suggestion="표시 너비는 deprecated됨, 8.4에서 자동 무시됨 (영향 최소)"
            ))

        if issues:
            self._log(f"  ℹ️ INT 표시 너비 {len(issues)}개 발견 (경미)")
        else:
            self._log("  ✅ INT 표시 너비 없음")

        return issues

    def get_fk_visualization(self, schema: str) -> str:
        """FK 관계를 트리 형태로 시각화"""
        fk_tree = self.build_fk_tree(schema)

        if not fk_tree:
            return "FK 관계가 없습니다."

        lines = ["FK 관계 트리:", ""]

        # 루트 테이블 찾기 (다른 테이블의 자식이 아닌 테이블)
        all_children = set()
        for children in fk_tree.values():
            all_children.update(children)

        root_tables = set(fk_tree.keys()) - all_children

        def print_tree(table: str, prefix: str = "", is_last: bool = True):
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{table}")

            if table in fk_tree:
                children = fk_tree[table]
                child_prefix = prefix + ("    " if is_last else "│   ")
                for i, child in enumerate(children):
                    print_tree(child, child_prefix, i == len(children) - 1)

        for i, root in enumerate(sorted(root_tables)):
            print_tree(root, "", i == len(root_tables) - 1)

        return "\n".join(lines)


# ============================================================
# 덤프 파일 분석기 (Task 3)
# ============================================================

@dataclass
class DumpAnalysisResult:
    """덤프 파일 분석 결과"""
    dump_path: str
    analyzed_at: str
    total_sql_files: int
    total_tsv_files: int
    compatibility_issues: List[CompatibilityIssue] = field(default_factory=list)


class DumpFileAnalyzer:
    """
    mysqlsh 덤프 파일 분석기

    덤프 폴더의 SQL/TSV 파일을 분석하여 MySQL 8.4 호환성 이슈를 탐지합니다.
    """

    def __init__(self):
        self._progress_callback: Optional[Callable[[str], None]] = None
        self._issue_callback: Optional[Callable[[CompatibilityIssue], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def set_issue_callback(self, callback: Callable[[CompatibilityIssue], None]):
        """이슈 발견 시 콜백 설정"""
        self._issue_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def _report_issue(self, issue: CompatibilityIssue):
        """이슈 발견 시 콜백 호출"""
        if self._issue_callback:
            self._issue_callback(issue)

    def analyze_dump_folder(self, dump_path: str) -> DumpAnalysisResult:
        """
        덤프 폴더 전체 분석

        Args:
            dump_path: mysqlsh 덤프 폴더 경로

        Returns:
            DumpAnalysisResult
        """
        from datetime import datetime

        path = Path(dump_path)
        if not path.exists():
            raise FileNotFoundError(f"덤프 폴더를 찾을 수 없습니다: {dump_path}")

        self._log(f"🔍 덤프 폴더 분석 시작: {dump_path}")

        issues: List[CompatibilityIssue] = []

        # SQL 파일 목록
        sql_files = list(path.glob("*.sql"))
        tsv_files = list(path.glob("*.tsv")) + list(path.glob("*.tsv.zst"))

        self._log(f"  SQL 파일: {len(sql_files)}개, 데이터 파일: {len(tsv_files)}개")

        # SQL 파일 분석
        for i, sql_file in enumerate(sql_files, 1):
            self._log(f"  [{i}/{len(sql_files)}] {sql_file.name} 분석 중...")
            file_issues = self._analyze_sql_file(sql_file)
            issues.extend(file_issues)

            # 실시간 이슈 콜백
            for issue in file_issues:
                self._report_issue(issue)

        # TSV 데이터 파일 분석 (0000-00-00 날짜 등)
        # 압축되지 않은 TSV 파일만 분석 (압축 파일은 너무 느림)
        uncompressed_tsv = [f for f in tsv_files if not str(f).endswith('.zst')]
        if uncompressed_tsv:
            for i, tsv_file in enumerate(uncompressed_tsv, 1):
                self._log(f"  [{i}/{len(uncompressed_tsv)}] {tsv_file.name} 분석 중...")
                file_issues = self._analyze_tsv_file(tsv_file)
                issues.extend(file_issues)

                for issue in file_issues:
                    self._report_issue(issue)

        # 결과 생성
        result = DumpAnalysisResult(
            dump_path=str(dump_path),
            analyzed_at=datetime.now().isoformat(),
            total_sql_files=len(sql_files),
            total_tsv_files=len(tsv_files),
            compatibility_issues=issues
        )

        # 요약
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")

        self._log(f"✅ 덤프 분석 완료")
        self._log(f"  - 오류: {error_count}개")
        self._log(f"  - 경고: {warning_count}개")

        return result

    def _analyze_sql_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """
        SQL 파일 분석 - 스키마 호환성 검사

        Args:
            file_path: SQL 파일 경로

        Returns:
            발견된 이슈 목록
        """
        issues = []

        try:
            content = file_path.read_text(encoding='utf-8', errors='replace')

            # 1. ZEROFILL 속성 검사
            for match in ZEROFILL_PATTERN.finditer(content):
                # 컨텍스트에서 테이블/컬럼 이름 추출 시도
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_end = content.find('\n', match.end())
                line = content[line_start:line_end]

                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ZEROFILL_USAGE,
                    severity="warning",
                    location=f"{file_path.name}",
                    description=f"ZEROFILL 속성 사용: {line.strip()[:80]}...",
                    suggestion="ZEROFILL은 deprecated됨"
                ))

            # 2. FLOAT(M,D), DOUBLE(M,D) 구문 검사
            for match in FLOAT_PRECISION_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FLOAT_PRECISION,
                    severity="warning",
                    location=f"{file_path.name}",
                    description=f"FLOAT/DOUBLE 정밀도 구문: {match.group(0)}",
                    suggestion="FLOAT(M,D) 구문은 deprecated됨"
                ))

            # 3. FK 이름 64자 초과 검사
            for match in FK_NAME_LENGTH_PATTERN.finditer(content):
                fk_name = match.group(1)
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FK_NAME_LENGTH,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"FK 이름 64자 초과: {fk_name[:30]}... ({len(fk_name)}자)",
                    suggestion="FK 이름을 64자 이하로 변경 필요"
                ))

            # 4. 인증 플러그인 검사
            for match in AUTH_PLUGIN_PATTERN.finditer(content):
                plugin = match.group(1).lower()
                severity = "error" if plugin == "mysql_native_password" else "warning"
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                    severity=severity,
                    location=f"{file_path.name}",
                    description=f"인증 플러그인: {plugin}",
                    suggestion="caching_sha2_password 사용 권장"
                ))

            # 5. FTS_ 테이블명 검사
            for match in FTS_TABLE_PREFIX_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FTS_TABLE_PREFIX,
                    severity="error",
                    location=f"{file_path.name}",
                    description="FTS_ 접두사 테이블명 (내부 예약어)",
                    suggestion="FTS_ 접두사는 내부 전문 검색용으로 예약됨, 테이블명 변경 필요"
                ))

            # 6. SUPER 권한 검사
            for match in SUPER_PRIVILEGE_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SUPER_PRIVILEGE,
                    severity="warning",
                    location=f"{file_path.name}",
                    description="SUPER 권한 사용 (deprecated)",
                    suggestion="동적 권한 (BINLOG_ADMIN, CONNECTION_ADMIN 등)으로 세분화 권장"
                ))

            # 7. 제거된 시스템 변수 사용 검사
            for match in SYS_VAR_USAGE_PATTERN.finditer(content):
                var_name = match.group(1)
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.REMOVED_SYS_VAR,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"제거된 시스템 변수 사용: {var_name}",
                    suggestion=f"'{var_name}'은 8.4에서 제거됨, 대체 방법 확인 필요"
                ))

            # 8. 예약어 충돌 (테이블/컬럼 이름) - CREATE TABLE 문에서
            table_pattern = re.compile(
                r'CREATE\s+TABLE\s+`?(\w+)`?\s*\(',
                re.IGNORECASE
            )
            column_pattern = re.compile(
                r'`(\w+)`\s+(?:INT|VARCHAR|TEXT|DATE|DECIMAL|FLOAT|DOUBLE|CHAR|BLOB|ENUM|SET)',
                re.IGNORECASE
            )

            keywords_upper = set(k.upper() for k in MigrationAnalyzer.NEW_RESERVED_KEYWORDS)

            for match in table_pattern.finditer(content):
                table_name = match.group(1)
                if table_name.upper() in keywords_upper:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.RESERVED_KEYWORD,
                        severity="error",
                        location=f"{file_path.name}",
                        description=f"테이블명 '{table_name}'이 예약어와 충돌",
                        suggestion="테이블명 변경 또는 백틱(`) 사용 필요"
                    ))

            for match in column_pattern.finditer(content):
                column_name = match.group(1)
                if column_name.upper() in keywords_upper:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.RESERVED_KEYWORD,
                        severity="warning",
                        location=f"{file_path.name}",
                        description=f"컬럼명 '{column_name}'이 예약어와 충돌",
                        suggestion="컬럼 참조 시 백틱(`) 사용 필요"
                    ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    def _analyze_tsv_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """
        TSV 데이터 파일 분석 - 데이터 무결성 검사

        Args:
            file_path: TSV 파일 경로

        Returns:
            발견된 이슈 목록
        """
        issues = []
        invalid_date_count = 0

        try:
            # 대용량 파일은 샘플링
            max_lines = 10000
            line_count = 0

            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line_count += 1
                    if line_count > max_lines:
                        break

                    # 0000-00-00 날짜 검사
                    if INVALID_DATE_PATTERN.search(line) or INVALID_DATETIME_PATTERN.search(line):
                        invalid_date_count += 1

            if invalid_date_count > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.INVALID_DATE,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"잘못된 날짜 값 발견: {invalid_date_count}개 행 (0000-00-00)",
                    suggestion="NO_ZERO_DATE SQL 모드 활성화 시 오류 발생, 유효한 날짜로 변환 필요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    def quick_scan(self, dump_path: str) -> Tuple[int, int, int]:
        """
        빠른 스캔 - 이슈 개수만 반환

        Args:
            dump_path: 덤프 폴더 경로

        Returns:
            (오류 수, 경고 수, 정보 수)
        """
        try:
            result = self.analyze_dump_folder(dump_path)
            error_count = sum(1 for i in result.compatibility_issues if i.severity == "error")
            warning_count = sum(1 for i in result.compatibility_issues if i.severity == "warning")
            info_count = sum(1 for i in result.compatibility_issues if i.severity == "info")
            return error_count, warning_count, info_count
        except Exception:
            return 0, 0, 0
