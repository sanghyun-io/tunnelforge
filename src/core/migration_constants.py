"""
MySQL 8.4 Upgrade Checker 상수 모듈

mysql-upgrade-checker 프로젝트에서 포팅된 상수와 패턴 정의.
MySQL 8.0.x → 8.4.x 업그레이드 호환성 검사에 사용.
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ============================================================
# MySQL 8.4에서 제거된 시스템 변수 (47개)
# ============================================================
REMOVED_SYS_VARS_84: Tuple[str, ...] = (
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

# ============================================================
# MySQL 8.4에서 추가된 새 예약어 (4개 - 8.4 신규)
# ============================================================
NEW_RESERVED_KEYWORDS_84: Tuple[str, ...] = ('MANUAL', 'PARALLEL', 'QUALIFY', 'TABLESAMPLE')

# 기존 MySQL 8.0 예약어 (주요 충돌 가능성)
RESERVED_KEYWORDS_80: Tuple[str, ...] = (
    'CUME_DIST', 'DENSE_RANK', 'EMPTY', 'EXCEPT', 'FIRST_VALUE',
    'GROUPING', 'GROUPS', 'JSON_TABLE', 'LAG', 'LAST_VALUE', 'LATERAL',
    'LEAD', 'NTH_VALUE', 'NTILE', 'OF', 'OVER', 'PERCENT_RANK',
    'RANK', 'RECURSIVE', 'ROW_NUMBER', 'SYSTEM', 'WINDOW',
)

# 전체 예약어 (8.0 + 8.4)
ALL_RESERVED_KEYWORDS: Tuple[str, ...] = RESERVED_KEYWORDS_80 + NEW_RESERVED_KEYWORDS_84

# ============================================================
# MySQL 8.4에서 제거된 함수
# ============================================================
# mysql-upgrade-checker 참조 구현과 통일된 분류 체계
# 8.0.x에서 deprecated → 8.4에서 완전 제거된 함수
REMOVED_FUNCTIONS_84: Tuple[str, ...] = (
    'PASSWORD',        # 8.0.11 deprecated → 8.4 제거
    'ENCRYPT',         # 8.0.3 deprecated → 8.4 제거
    'ENCODE',          # 8.0.3 deprecated → 8.4 제거
    'DECODE',          # 8.0.3 deprecated → 8.4 제거
    'DES_ENCRYPT',     # 8.0.3 deprecated → 8.4 제거
    'DES_DECRYPT',     # 8.0.3 deprecated → 8.4 제거
)

# 8.4에서 deprecated된 함수 (아직 동작하나 deprecated 경고)
DEPRECATED_FUNCTIONS_84: Tuple[str, ...] = (
    'MASTER_POS_WAIT',      # deprecated alias, SOURCE_POS_WAIT() 사용 권장
    'FOUND_ROWS',           # deprecated, COUNT(*) 별도 쿼리 사용 권장
    'SQL_CALC_FOUND_ROWS',  # deprecated, COUNT(*) 별도 쿼리 사용 권장
)

# 8.0 이전에 이미 제거된 함수 (5.7 → 8.0 마이그레이션 잔존 확인용)
REMOVED_FUNCTIONS_80X: Tuple[str, ...] = (
    'ENCODE',
    'DECODE',
    'DES_ENCRYPT',
    'DES_DECRYPT',
    'ENCRYPT',
    'PASSWORD',
    'OLD_PASSWORD',    # 5.7에서 제거
)

# 마이그레이션 검사 시 사용할 전체 제거/deprecated 함수 목록
# 세 소스 튜플 사이에 겹치는 함수명(예: ENCODE, DECODE)이 있으므로 순서를
# 보존하면서 중복만 제거한다.
ALL_REMOVED_FUNCTIONS: Tuple[str, ...] = tuple(dict.fromkeys(
    REMOVED_FUNCTIONS_84 + REMOVED_FUNCTIONS_80X + DEPRECATED_FUNCTIONS_84
))

# MySQL 8.4에서 generated column 내 동작이 변경된 함수
# (mysql-upgrade-checker의 CHANGED_FUNCTIONS_IN_GENERATED_COLUMNS 참조)
# 이 함수들은 generated column 표현식에서 사용 시 8.4 업그레이드 후 결과가 달라질 수 있음
CHANGED_FUNCTIONS_IN_GENERATED_COLUMNS: Tuple[str, ...] = (
    'IF',
    'IFNULL',
    'NULLIF',
    'CASE',
    'COALESCE',
    'GREATEST',
    'LEAST',
    'BIT_AND',
    'BIT_OR',
    'BIT_XOR',
)

# ============================================================
# 인증 플러그인 상태
# ============================================================
AUTH_PLUGINS: Dict[str, List[str]] = {
    'disabled': ['mysql_native_password'],  # 8.4에서 기본 비활성화
    'removed': ['authentication_fido', 'authentication_fido_client'],  # 8.4에서 제거
    'deprecated': ['sha256_password'],  # deprecated, caching_sha2_password 권장
    'recommended': ['caching_sha2_password'],  # 권장
}

# ============================================================
# 제거된/deprecated SQL 모드 (11개)
# ============================================================
OBSOLETE_SQL_MODES: Tuple[str, ...] = (
    'DB2', 'MAXDB', 'MSSQL', 'MYSQL323', 'MYSQL40',
    'ORACLE', 'POSTGRESQL', 'NO_FIELD_OPTIONS', 'NO_KEY_OPTIONS',
    'NO_TABLE_OPTIONS', 'NO_AUTO_CREATE_USER',
)

# ============================================================
# 기본값이 변경된 시스템 변수
# ============================================================
SYS_VARS_NEW_DEFAULTS_84: Dict[str, Dict[str, str]] = {
    # Note: binlog_transaction_dependency_tracking은 REMOVED_SYS_VARS_84에 포함 (제거됨)
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
        'old': '2000', 'new': '20000',
    },
    'innodb_log_buffer_size': {
        'old': '16M', 'new': '64M',
    },
    'innodb_redo_log_capacity': {
        'old': '100M (innodb_log_file_size * innodb_log_files_in_group)', 'new': '100M',
    },
    'group_replication_consistency': {
        'old': 'EVENTUAL', 'new': 'BEFORE_ON_PRIMARY_FAILOVER',
    },
    'innodb_change_buffering': {
        'old': 'all', 'new': 'none',
    },
    # Note: log_error_verbosity는 8.4에서도 기본값 2 유지 (변경 없음, 삭제)
    'explicit_defaults_for_timestamp': {
        'old': 'OFF', 'new': 'ON',
    },
}

# ============================================================
# 식별자 길이 제한
# ============================================================
IDENTIFIER_LIMITS: Dict[str, int] = {
    'TABLE_NAME': 64,
    'COLUMN_NAME': 64,
    'INDEX_NAME': 64,
    'FOREIGN_KEY_NAME': 64,
    'CONSTRAINT_NAME': 64,
    'DATABASE_NAME': 64,
    'VIEW_NAME': 64,
    'TRIGGER_NAME': 64,
    'ALIAS': 256,
    'ENUM_ELEMENT': 255,
    'SET_ELEMENT': 255,
}

# ============================================================
# 인덱스 크기 제한 (바이트)
# ============================================================
INDEX_SIZE_LIMITS: Dict[str, int] = {
    'INNODB_MAX_KEY_LENGTH': 3072,
    'MYISAM_MAX_KEY_LENGTH': 1000,
    'DEFAULT_PREFIX_LENGTH': 767,
}

# ============================================================
# Deprecated 구문 패턴
# ============================================================
DEPRECATED_SYNTAX_PATTERNS: Dict[str, re.Pattern] = {
    'GROUP_BY_ASC_DESC': re.compile(
        r'\bGROUP\s+BY\b[^;]*\b(ASC|DESC)\b',
        re.IGNORECASE | re.DOTALL
    ),
    'SQL_CALC_FOUND_ROWS': re.compile(
        r'\bSQL_CALC_FOUND_ROWS\b',
        re.IGNORECASE
    ),
    'FOUND_ROWS_FUNC': re.compile(
        r'\bFOUND_ROWS\s*\(\s*\)',
        re.IGNORECASE
    ),
}

# ============================================================
# MySQL 스키마 내부 테이블 (충돌 방지)
# ============================================================
MYSQL_SCHEMA_TABLES: Tuple[str, ...] = (
    'catalogs', 'check_constraints', 'collations', 'columns',
    'column_statistics', 'dd_properties', 'events',
    'foreign_key_column_usage', 'foreign_keys', 'index_column_usage',
    'index_partitions', 'indexes', 'innodb_ddl_log',
    'innodb_dynamic_metadata', 'parameter_type_elements', 'parameters',
    'resource_groups', 'routines', 'schemata',
    'st_spatial_reference_systems', 'table_partition_values',
    'table_partitions', 'table_stats', 'tables', 'tablespace_files',
    'tablespaces', 'triggers', 'view_routine_usage',
    'view_table_usage', 'column_type_elements',
)

# ============================================================
# 스토리지 엔진 상태
# ============================================================
# 엔진별 상세 정책 (severity, suggestion)
# migration_analyzer.py의 check_deprecated_engines와 storage_rules.py가 공유하는 단일 소스.
# STORAGE_ENGINE_STATUS['deprecated']는 이 dict의 키에서 파생되므로,
# 새 엔진을 deprecated로 취급하려면 반드시 이 dict에 항목을 추가해야 한다.
ENGINE_POLICIES: Dict[str, Dict[str, str]] = {
    'MyISAM': {
        'severity': 'warning',
        'suggestion': 'InnoDB로 변환 권장 (트랜잭션/FK 지원)',
    },
    'ARCHIVE': {
        'severity': 'warning',
        'suggestion': 'InnoDB로 변환 권장',
    },
    'BLACKHOLE': {
        'severity': 'info',
        'suggestion': '테스트/복제용 엔진 - 필요시 유지',
    },
    'FEDERATED': {
        'severity': 'warning',
        'suggestion': 'MySQL 8.4에서 제거 예정',
    },
    'MERGE': {
        'severity': 'error',
        'suggestion': 'MySQL 8.4에서 제거됨 - InnoDB 파티셔닝으로 대체',
    },
    'MEMORY': {
        'severity': 'info',
        'suggestion': '임시 테이블용으로는 유지 가능',
    },
    'EXAMPLE': {
        'severity': 'warning',
        'suggestion': '예제/스텁 엔진 - 운영 환경에서는 InnoDB로 변경 권장',
    },
    'NDB': {
        'severity': 'warning',
        'suggestion': 'NDB Cluster 전용 엔진 - 단일 인스턴스 환경에서는 지원되지 않음',
    },
    'CSV': {
        'severity': 'info',
        'suggestion': '로그/내보내기 용도로는 유지 가능, 일반 테이블은 InnoDB 권장',
    },
}

STORAGE_ENGINE_STATUS: Dict[str, any] = {
    # ENGINE_POLICIES에 정의된 엔진 = deprecated 취급 대상 (단일 소스에서 파생)
    'deprecated': list(ENGINE_POLICIES.keys()),
    'recommended': 'InnoDB',
    # 호환성 유지용 메타데이터. 규칙 스캔의 독립적인 소스로는 사용하지 않는다.
    'warning_engines': ['MEMORY', 'CSV'],
}

# ============================================================
# 문자셋 관련 상수
# ============================================================
CHARSET_MIGRATION_MAP: Dict[str, str] = {
    'utf8': 'utf8mb4',
    'utf8mb3': 'utf8mb4',
    'latin1': 'utf8mb4',  # 권장
}

CHARSET_BYTES_PER_CHAR: Dict[str, int] = {
    'utf8mb4': 4,
    'utf8mb3': 3,
    'utf8': 3,
    'latin1': 1,
    'ascii': 1,
    'binary': 1,
    'ucs2': 2,
    'utf16': 4,
    'utf32': 4,
}

# ============================================================
# IssueType Enum (확장)
# ============================================================
class IssueType(Enum):
    """호환성 문제 유형"""
    # 기존 이슈 타입 (마이그레이션 분석기)
    ORPHAN_ROW = "orphan_row"  # 부모 없는 자식 레코드
    DEPRECATED_FUNCTION = "deprecated_function"  # deprecated 함수 사용
    CHARSET_ISSUE = "charset_issue"  # utf8mb3 → utf8mb4 필요
    RESERVED_KEYWORD = "reserved_keyword"  # 예약어 충돌
    SQL_MODE_ISSUE = "sql_mode_issue"  # deprecated SQL 모드

    # MySQL 8.4 Upgrade Checker 이슈 타입
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

    # 신규 이슈 타입 (확장)
    YEAR2_TYPE = "year2_type"  # YEAR(2) 타입
    LATIN1_CHARSET = "latin1_charset"  # latin1 charset
    INDEX_ISSUE = "index_issue"  # 인덱스 관련 이슈 (일반)
    INDEX_TOO_LARGE = "index_too_large"  # 인덱스 크기 초과
    GROUPBY_ASC_DESC = "groupby_asc_desc"  # GROUP BY ASC/DESC
    SQL_CALC_FOUND_ROWS_USAGE = "sql_calc_found_rows"  # SQL_CALC_FOUND_ROWS
    DOLLAR_SIGN_NAME = "dollar_sign_name"  # $ 문자 식별자
    TRAILING_SPACE_NAME = "trailing_space_name"  # 트레일링 스페이스
    CONTROL_CHAR_NAME = "control_char_name"  # 제어 문자
    DEPRECATED_ENGINE = "deprecated_engine"  # deprecated 엔진
    PARTITION_ISSUE = "partition_issue"  # 파티션 이슈
    GENERATED_COLUMN_ISSUE = "generated_column_issue"  # 생성 컬럼 이슈
    OLD_GEOMETRY_TYPE = "old_geometry_type"  # 구 geometry 타입
    BLOB_TEXT_DEFAULT = "blob_text_default"  # BLOB/TEXT DEFAULT
    MYSQL_SCHEMA_CONFLICT = "mysql_schema_conflict"  # mysql 스키마 충돌

    # 데이터 무결성 이슈 타입
    ENUM_EMPTY_VALUE = "enum_empty_value"  # ENUM 빈 값
    ENUM_NUMERIC_INDEX = "enum_numeric_index"  # ENUM 숫자 인덱스
    ENUM_ELEMENT_LENGTH = "enum_element_length"  # ENUM 요소 길이
    SET_ELEMENT_LENGTH = "set_element_length"  # SET 요소 길이
    DATA_4BYTE_UTF8 = "data_4byte_utf8"  # 4바이트 UTF-8
    DATA_NULL_BYTE = "data_null_byte"  # NULL 바이트
    TIMESTAMP_RANGE = "timestamp_range"  # TIMESTAMP 범위 초과
    LATIN1_NON_ASCII = "latin1_non_ascii"  # latin1 비ASCII 데이터

    # FK 크로스 검증 이슈 타입
    FK_NON_UNIQUE_REF = "fk_non_unique_ref"  # FK 비고유 참조
    FK_REF_NOT_FOUND = "fk_ref_not_found"  # FK 참조 테이블 미존재

    # 스캔 관련
    SCAN_TRUNCATED = "scan_truncated"  # 스캔 행 수 제한으로 중단됨

    # Definer 관련
    ROUTINE_DEFINER_MISSING = "routine_definer_missing"  # 루틴 definer 누락
    VIEW_DEFINER_MISSING = "view_definer_missing"  # 뷰 definer 누락

    # 신규 이슈 타입 (이슈 #63)
    PARTITION_PREFIX_KEY = "partition_prefix_key"  # 파티션 키에 prefix 인덱스 사용
    EMPTY_DOT_TABLE_SYNTAX = "empty_dot_table_syntax"  # 스키마 생략 dot 구문 (.tableName)
    INNODB_ROW_FORMAT = "innodb_row_format"  # REDUNDANT/COMPACT ROW_FORMAT (DYNAMIC 권장)
    DEPRECATED_TEMPORAL_DELIMITER = "deprecated_temporal_delimiter"  # deprecated 날짜 구분자
    INVALID_ENGINE_FK = "invalid_engine_fk"  # 비InnoDB 엔진에 FK 사용
    ROUTINE_SYNTAX_KEYWORD = "routine_syntax_keyword"  # 루틴 이름이 예약어와 충돌
    INVALID_57_NAME_MULTIPLE_DOTS = "invalid_57_name_multiple_dots"  # 식별자에 연속 점(..) 사용


# ============================================================
# 호환성 문제 데이터 클래스 (단일 정의, 전 모듈 공용)
# ============================================================
@dataclass
class CompatibilityIssue:
    """호환성 문제 - 전 모듈에서 이 클래스를 import하여 사용"""
    issue_type: IssueType
    severity: str  # "error", "warning", "info"
    location: str  # 테이블명 또는 위치
    description: str
    suggestion: str
    fix_query: Optional[str] = None      # 수정 SQL
    doc_link: Optional[str] = None       # 문서 링크
    upgrade_check_id: Optional[str] = None      # Upgrade check ID
    code_snippet: Optional[str] = None   # 관련 코드
    table_name: Optional[str] = None     # 테이블명
    column_name: Optional[str] = None    # 컬럼명


# ============================================================
# 식별자 이슈 탐지용 컨텍스트 제한 매처
# ============================================================
# 배경: 단순 `[^`]*\$[^`]*` 형태의 raw 정규식은 백틱이 짝을 이루는지
# 확인하지 않아, 인접한 두 식별자 사이의 텍스트(따옴표 문자열 리터럴 포함)를
# 하나의 식별자로 오인해 대량의 오탐을 만들었다. 아래 헬퍼는 완전한 백틱
# 토큰(여는/닫는 백틱이 한 쌍인 구간) 단위로만 술어를 검사하고, 문자열
# 리터럴은 스캔 전에 마스킹하여 데이터 영역의 `$`/제어문자를 식별자 문제로
# 오인하지 않도록 한다. `.search()`/`.finditer()`는 기존 `re.Pattern`과
# 동일하게 실제 `re.Match` 객체를 반환하므로 호출부(`schema_rules.py`,
# 테스트)의 `match.group(0)` 사용은 변경 없이 그대로 동작한다.
class _IdentifierIssuePattern:
    """DDL 식별자 컨텍스트 안의 완전한 백틱 토큰만 검사하는 패턴 매처"""

    # CREATE/ALTER/DROP/RENAME TABLE, CREATE/DROP INDEX 등 식별자가
    # 정의/참조되는 DDL 구문 범위. 세미콜론까지(또는 세미콜론이 없으면
    # 문자열 끝까지)를 하나의 구문으로 간주하는 보수적인 스캐너.
    _DDL_CONTEXT_PATTERN = re.compile(
        r'\b(?:CREATE(?:\s+(?:UNIQUE|FULLTEXT|SPATIAL))?|ALTER|DROP|RENAME)'
        r'\s+(?:TABLE|INDEX)\b[^;]*;?',
        re.IGNORECASE | re.DOTALL,
    )
    # 완전한 백틱 토큰 하나(개행을 넘어가지 않음)
    _TOKEN_PATTERN = re.compile(r'`([^`\r\n]*)`')
    # 작은/큰따옴표 문자열 리터럴 (이스케이프 문자 포함)
    _STRING_LITERAL_PATTERN = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", re.DOTALL)

    def __init__(self, predicate):
        self._predicate = predicate

    @classmethod
    def _mask_string_literals(cls, text: str) -> str:
        """문자열 리터럴 내부를 동일 길이의 'x'로 치환해 오프셋을 보존한다."""
        def _mask(m):
            s = m.group(0)
            if len(s) < 2:
                return s
            return s[0] + 'x' * (len(s) - 2) + s[-1]
        return cls._STRING_LITERAL_PATTERN.sub(_mask, text)

    def _iter_scan_spans(self, text: str):
        """DDL 컨텍스트 구간만 산출. 컨텍스트 키워드가 전혀 없으면
        (상수 단위 테스트처럼 식별자만 단독으로 주어진 입력) 전체 텍스트를
        그대로 하나의 구간으로 취급한다."""
        found = False
        for m in self._DDL_CONTEXT_PATTERN.finditer(text):
            found = True
            yield m.start(), m.group(0)
        if not found:
            yield 0, text

    def finditer(self, text: str):
        for offset, span_text in self._iter_scan_spans(text):
            masked = self._mask_string_literals(span_text)
            for m in self._TOKEN_PATTERN.finditer(masked):
                if self._predicate(m.group(1)):
                    real_match = self._TOKEN_PATTERN.match(text, offset + m.start())
                    if real_match:
                        yield real_match

    def search(self, text: str):
        for m in self.finditer(text):
            return m
        return None


class _ContextualDotPattern:
    """FROM/JOIN/INTO/UPDATE/TABLE/REFERENCES 키워드 바로 뒤에 오는
    식별자 참조에서만 연속 점(..) 오타를 검사하는 패턴 매처.

    키워드 제한 없이 원시 텍스트 전체를 스캔하면 INSERT 데이터나 문자열
    리터럴 안의 '..'까지 식별자 문제로 오인한다. 매치 시작 위치를 키워드
    바로 다음으로 고정하므로 `match.group(0)`에는 키워드가 포함되지 않는다.
    """

    _KEYWORD_PATTERN = re.compile(
        r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE|REFERENCES)\s+',
        re.IGNORECASE,
    )
    _IDENTIFIER_PATTERN = re.compile(r'`?[\w$]+`?\s*\.\.\s*`?[\w$]+`?')

    def finditer(self, text: str):
        for kw_match in self._KEYWORD_PATTERN.finditer(text):
            id_match = self._IDENTIFIER_PATTERN.match(text, kw_match.end())
            if id_match:
                yield id_match

    def search(self, text: str):
        for m in self.finditer(text):
            return m
        return None


# ============================================================
# 덤프 파일 분석용 정규식 패턴
# ============================================================

# 0000-00-00 날짜 (잘못된 날짜)
INVALID_DATE_PATTERN = re.compile(r"['\"]0000-00-00['\"]|^0000-00-00$", re.MULTILINE)
INVALID_DATETIME_PATTERN = re.compile(r"['\"]0000-00-00 00:00:00['\"]|^0000-00-00 00:00:00$", re.MULTILINE)

# 추가적인 잘못된 날짜 패턴 (년/월/일 = 00)
INVALID_DATE_VALUES_PATTERN = re.compile(
    r"'(?:0000-\d{2}-\d{2}|\d{4}-00-\d{2}|\d{4}-\d{2}-00)'",
    re.IGNORECASE
)

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
    r"IDENTIFIED\s+(?:WITH\s+)?['\"]?(mysql_native_password|sha256_password|authentication_fido|authentication_fido_client)['\"]?",
    re.IGNORECASE
)

# FTS_ 접두사 테이블명 (내부 예약)
FTS_TABLE_PREFIX_PATTERN = re.compile(r'CREATE\s+TABLE\s+`?FTS_', re.IGNORECASE)

# GRANT 문의 SUPER 권한
SUPER_PRIVILEGE_PATTERN = re.compile(r'\bGRANT\b.*\bSUPER\b', re.IGNORECASE | re.DOTALL)

# 제거된 시스템 변수 사용 (SET/SELECT 문에서)
SYS_VAR_USAGE_PATTERN = re.compile(
    r"(?:SET|SELECT)\s+.*(?:@@(?:global|session)?\.)?" +
    r"(" + "|".join(re.escape(v) for v in REMOVED_SYS_VARS_84) + r")\b(?!\s*\.)",
    re.IGNORECASE
)

# YEAR(2) 타입 패턴
YEAR2_PATTERN = re.compile(r'\bYEAR\s*\(\s*2\s*\)', re.IGNORECASE)

# ENUM 빈 값 정의 패턴
ENUM_EMPTY_PATTERN = re.compile(
    r"ENUM\s*\([^)]*''\s*[,)]",
    re.IGNORECASE
)

# SET 빈 값 정의 패턴
SET_EMPTY_PATTERN = re.compile(
    r"SET\s*\([^)]*''\s*[,)]",
    re.IGNORECASE
)

# 제어 문자 판별용 내부 문자 클래스
# \x09(tab)은 트레일링 스페이스 검사가 담당하고 \x0a/\x0d(개행)는 백틱
# 토큰이 개행을 넘어가지 않도록 이미 제외되므로 여기서는 제외한다.
_CONTROL_CHAR_INNER_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# 달러 기호 식별자 패턴 (DDL 식별자 컨텍스트로 제한, 문자열 리터럴 제외)
DOLLAR_SIGN_PATTERN = _IdentifierIssuePattern(lambda name: '$' in name)

# 트레일링 스페이스 식별자 패턴 (DDL 식별자 컨텍스트로 제한, 문자열 리터럴 제외)
TRAILING_SPACE_PATTERN = _IdentifierIssuePattern(
    lambda name: bool(name) and name[-1] in (' ', '\t')
)

# 제어 문자 식별자 패턴 (DDL 식별자 컨텍스트로 제한, 문자열 리터럴 제외)
CONTROL_CHAR_PATTERN = _IdentifierIssuePattern(
    lambda name: bool(_CONTROL_CHAR_INNER_PATTERN.search(name))
)

# TIMESTAMP 패턴 (범위 확인용)
TIMESTAMP_PATTERN = re.compile(
    r"'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})'"
)

# BLOB/TEXT DEFAULT 패턴
BLOB_TEXT_DEFAULT_PATTERN = re.compile(
    r'`\w+`\s+(BLOB|TEXT|TINYBLOB|MEDIUMBLOB|LONGBLOB|TINYTEXT|MEDIUMTEXT|LONGTEXT)\s+DEFAULT\s+',
    re.IGNORECASE
)

# GENERATED COLUMN 패턴
GENERATED_COLUMN_PATTERN = re.compile(
    r'GENERATED\s+ALWAYS\s+AS\s*\(([^)]+)\)',
    re.IGNORECASE
)

# ============================================================
# 신규 패턴 (이슈 #63)
# ============================================================

# PARTITION BY KEY/RANGE/LIST with prefix index 패턴
# PARTITION BY KEY (prefix_col(N)) 또는 KEY (col(N)) 형태 감지
PARTITION_PREFIX_KEY_PATTERN = re.compile(
    r'PARTITION\s+BY\s+(?:LINEAR\s+)?KEY\s*\([^)]*\w+\s*\(\s*\d+\s*\)[^)]*\)',
    re.IGNORECASE
)

# 스키마 생략 dot 구문 패턴 (`.tableName` 형태)
# FROM 또는 JOIN 뒤에 오는 .table_name 참조 (스키마 없이 점으로 시작)
EMPTY_DOT_TABLE_SYNTAX_PATTERN = re.compile(
    r'(?:FROM|JOIN)\s+\.\s*`?\w+`?',
    re.IGNORECASE
)

# INNODB ROW_FORMAT REDUNDANT/COMPACT 패턴
INNODB_ROW_FORMAT_PATTERN = re.compile(
    r'\bROW_FORMAT\s*=\s*(REDUNDANT|COMPACT)\b',
    re.IGNORECASE
)

# deprecated 날짜 구분자 패턴 (@ 또는 / 또는 ! 등 비표준 구분자 사용)
# MySQL은 일반적으로 - 또는 / 허용하나 @ ! # 등은 비표준
DEPRECATED_TEMPORAL_DELIMITER_PATTERN = re.compile(
    r"'(\d{4})\s*[@!#]\s*(\d{1,2})\s*[@!#]\s*(\d{1,2})'",
    re.IGNORECASE
)

# 비InnoDB 엔진 테이블에 FOREIGN KEY 정의 패턴
# CREATE TABLE ... ENGINE=MyISAM/MEMORY/ARCHIVE ... FOREIGN KEY
INVALID_ENGINE_FK_PATTERN = re.compile(
    r'CREATE\s+TABLE\s+[^;]+?FOREIGN\s+KEY[^;]+?ENGINE\s*=\s*(MyISAM|MEMORY|ARCHIVE|CSV)\b'
    r'|CREATE\s+TABLE\s+[^;]+?ENGINE\s*=\s*(MyISAM|MEMORY|ARCHIVE|CSV)\b[^;]+?FOREIGN\s+KEY',
    re.IGNORECASE | re.DOTALL
)

# 저장 프로시저/함수/이벤트/트리거 이름이 예약어와 충돌하는 패턴
# CREATE PROCEDURE/FUNCTION `keyword` 또는 CREATE PROCEDURE/FUNCTION keyword
ROUTINE_SYNTAX_KEYWORD_PATTERN = re.compile(
    r'CREATE\s+(?:DEFINER\s*=\s*\S+\s+)?(?:PROCEDURE|FUNCTION|EVENT|TRIGGER)\s+`?(\w+)`?',
    re.IGNORECASE
)

# 식별자에 연속 점(..) 사용 패턴 (schema..table 또는 ..table 형태)
# FROM/JOIN/INTO/UPDATE/TABLE/REFERENCES 등 식별자 참조 컨텍스트로 제한하여
# INSERT 데이터나 문자열 리터럴 내부의 '..'를 오탐하지 않도록 한다.
INVALID_57_NAME_MULTIPLE_DOTS_PATTERN = _ContextualDotPattern()

# ============================================================
# Upgrade check ID 매핑
# ============================================================
UPGRADE_CHECK_IDS: Dict[IssueType, str] = {
    IssueType.REMOVED_SYS_VAR: "removedSysVars",
    IssueType.AUTH_PLUGIN_ISSUE: "authMethodUsage",
    IssueType.CHARSET_ISSUE: "utf8mb3",
    IssueType.RESERVED_KEYWORD: "reservedKeywords",
    IssueType.INVALID_DATE: "zeroDates",
    IssueType.ZEROFILL_USAGE: "zerofillWidth",
    IssueType.FLOAT_PRECISION: "floatAutoToDouble",
    IssueType.INT_DISPLAY_WIDTH: "displayWidth",
    IssueType.DEPRECATED_FUNCTION: "removedFunctions",
    IssueType.SUPER_PRIVILEGE: "superPrivilege",
    IssueType.FK_NAME_LENGTH: "maxIdentifierLength",
    IssueType.DEPRECATED_ENGINE: "deprecatedStorage",
    IssueType.YEAR2_TYPE: "year2Type",
    IssueType.INDEX_TOO_LARGE: "indexKeyLength",
    IssueType.GROUPBY_ASC_DESC: "groupByAscDesc",
    IssueType.SQL_CALC_FOUND_ROWS_USAGE: "sqlCalcFoundRows",
    IssueType.FK_NON_UNIQUE_REF: "fkNonUniqueRef",
    IssueType.FK_REF_NOT_FOUND: "fkRefNotFound",
    IssueType.PARTITION_PREFIX_KEY: "partitionPrefixKey",
    IssueType.EMPTY_DOT_TABLE_SYNTAX: "emptyDotTableSyntax",
    IssueType.INNODB_ROW_FORMAT: "innodbRowFormat",
    IssueType.DEPRECATED_TEMPORAL_DELIMITER: "deprecatedTemporalDelimiter",
    IssueType.INVALID_ENGINE_FK: "invalidEngineFk",
    IssueType.ROUTINE_SYNTAX_KEYWORD: "routineSyntaxKeyword",
    IssueType.INVALID_57_NAME_MULTIPLE_DOTS: "invalid57NameMultipleDots",
}

# ============================================================
# 문서 링크 매핑
# ============================================================
DOC_LINKS: Dict[IssueType, str] = {
    IssueType.AUTH_PLUGIN_ISSUE: "https://dev.mysql.com/doc/refman/8.4/en/caching-sha2-password.html",
    IssueType.CHARSET_ISSUE: "https://dev.mysql.com/doc/refman/8.4/en/charset-unicode-utf8mb4.html",
    IssueType.REMOVED_SYS_VAR: "https://dev.mysql.com/doc/refman/8.4/en/added-deprecated-removed.html",
    IssueType.ZEROFILL_USAGE: "https://dev.mysql.com/doc/refman/8.4/en/numeric-type-attributes.html",
    IssueType.FLOAT_PRECISION: "https://dev.mysql.com/doc/refman/8.4/en/floating-point-types.html",
    IssueType.RESERVED_KEYWORD: "https://dev.mysql.com/doc/refman/8.4/en/keywords.html",
    IssueType.INVALID_DATE: "https://dev.mysql.com/doc/refman/8.4/en/sql-mode.html#sqlmode_no_zero_date",
    IssueType.DEPRECATED_ENGINE: "https://dev.mysql.com/doc/refman/8.4/en/storage-engines.html",
    IssueType.SUPER_PRIVILEGE: "https://dev.mysql.com/doc/refman/8.4/en/privileges-provided.html",
    IssueType.YEAR2_TYPE: "https://dev.mysql.com/doc/refman/8.4/en/year.html",
    IssueType.INDEX_TOO_LARGE: "https://dev.mysql.com/doc/refman/8.4/en/innodb-limits.html",
    IssueType.GROUPBY_ASC_DESC: "https://dev.mysql.com/doc/refman/8.4/en/select.html",
    IssueType.SQL_CALC_FOUND_ROWS_USAGE: "https://dev.mysql.com/doc/refman/8.4/en/information-functions.html#function_found-rows",
}
