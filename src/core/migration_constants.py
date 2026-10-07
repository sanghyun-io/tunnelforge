"""
MySQL 8.4 Upgrade Checker 상수 모듈

mysql-upgrade-checker 프로젝트에서 포팅된 상수와 패턴 정의.
MySQL 8.0.x → 8.4.x 업그레이드 호환성 검사에 사용.
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple


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
# 덤프 파일 분석용 정규식 패턴
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


# ============================================================
# 자동 수정 가능 이슈 타입 (UI 공용 단일 소스)
# ============================================================
# 마이그레이션 UI(migration_dialogs.py, fix_wizard_issue_selection_page.py 등)에서
# "자동 수정 위저드" 대상 여부를 판단하는 canonical 목록.
AUTO_FIXABLE_ISSUE_TYPES: frozenset = frozenset({
    IssueType.INVALID_DATE,
    IssueType.CHARSET_ISSUE,
    IssueType.ZEROFILL_USAGE,
    IssueType.FLOAT_PRECISION,
    IssueType.INT_DISPLAY_WIDTH,
    IssueType.DEPRECATED_ENGINE,
    IssueType.ENUM_EMPTY_VALUE,
})

# ============================================================
# 이슈 유형별 표시 이름 (UI 공용 단일 소스)
# ============================================================
# migration_dialogs.py, migration_manual_guide_dialog.py, fix_wizard_issue_selection_page.py,
# fix_wizard_option_page.py에 흩어져 있던 5개 type_names dict를 통합한 canonical 매핑.
ISSUE_TYPE_DISPLAY_NAMES: Dict[IssueType, str] = {
    IssueType.ORPHAN_ROW: "고아 레코드",
    IssueType.DEPRECATED_FUNCTION: "deprecated 함수",
    IssueType.CHARSET_ISSUE: "문자셋 이슈",
    IssueType.RESERVED_KEYWORD: "예약어",
    IssueType.SQL_MODE_ISSUE: "SQL 모드",
    IssueType.REMOVED_SYS_VAR: "제거된 시스템 변수",
    IssueType.AUTH_PLUGIN_ISSUE: "인증 플러그인",
    IssueType.INVALID_DATE: "잘못된 날짜",
    IssueType.ZEROFILL_USAGE: "ZEROFILL 속성",
    IssueType.FLOAT_PRECISION: "FLOAT 정밀도",
    IssueType.INT_DISPLAY_WIDTH: "INT 표시 너비",
    IssueType.FK_NAME_LENGTH: "FK 이름 길이",
    IssueType.FTS_TABLE_PREFIX: "FTS_ 테이블명",
    IssueType.SUPER_PRIVILEGE: "SUPER 권한",
    IssueType.DEFAULT_VALUE_CHANGE: "기본값 변경",
    IssueType.DEPRECATED_ENGINE: "deprecated 엔진",
    IssueType.ENUM_EMPTY_VALUE: "ENUM 빈 값",
    IssueType.PARTITION_ISSUE: "파티션 이슈",
    IssueType.INDEX_ISSUE: "인덱스 이슈",
}
