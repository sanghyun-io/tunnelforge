"""
migration_constants.py 단위 테스트

상수, Enum, 정규식 패턴의 불변량을 검증합니다.
"""
import re
import pytest

from src.core.migration_constants import (
    REMOVED_SYS_VARS_84,
    NEW_RESERVED_KEYWORDS_84,
    RESERVED_KEYWORDS_80,
    ALL_RESERVED_KEYWORDS,
    ALL_REMOVED_FUNCTIONS,
    REMOVED_FUNCTIONS_84,
    DEPRECATED_FUNCTIONS_84,
    REMOVED_FUNCTIONS_80X,
    OBSOLETE_SQL_MODES,
    AUTH_PLUGINS,
    SYS_VARS_NEW_DEFAULTS_84,
    IDENTIFIER_LIMITS,
    INDEX_SIZE_LIMITS,
    CHARSET_MIGRATION_MAP,
    CHARSET_BYTES_PER_CHAR,
    STORAGE_ENGINE_STATUS,
    MYSQL_SCHEMA_TABLES,
    DEPRECATED_SYNTAX_PATTERNS,
    MYSQL_SHELL_CHECK_IDS,
    DOC_LINKS,
    IssueType,
    CompatibilityIssue,
    # Regex patterns
    INVALID_DATE_PATTERN,
    INVALID_DATETIME_PATTERN,
    INVALID_DATE_VALUES_PATTERN,
    ZEROFILL_PATTERN,
    FLOAT_PRECISION_PATTERN,
    INT_DISPLAY_WIDTH_PATTERN,
    FK_NAME_LENGTH_PATTERN,
    AUTH_PLUGIN_PATTERN,
    FTS_TABLE_PREFIX_PATTERN,
    SUPER_PRIVILEGE_PATTERN,
    SYS_VAR_USAGE_PATTERN,
    YEAR2_PATTERN,
    ENUM_EMPTY_PATTERN,
    DOLLAR_SIGN_PATTERN,
    TRAILING_SPACE_PATTERN,
    CONTROL_CHAR_PATTERN,
    TIMESTAMP_PATTERN,
    BLOB_TEXT_DEFAULT_PATTERN,
)


# ============================================================
# 상수 불변량 테스트
# ============================================================
class TestRemovedSysVars:
    """REMOVED_SYS_VARS_84 불변량 검증"""

    def test_is_tuple(self):
        assert isinstance(REMOVED_SYS_VARS_84, tuple)

    def test_count_47(self):
        assert len(REMOVED_SYS_VARS_84) == 47

    def test_all_unique(self):
        assert len(set(REMOVED_SYS_VARS_84)) == len(REMOVED_SYS_VARS_84)

    def test_all_strings(self):
        for v in REMOVED_SYS_VARS_84:
            assert isinstance(v, str)

    @pytest.mark.parametrize("var", [
        "binlog_format",
        "default_authentication_plugin",
        "innodb_log_file_size",
        "innodb_log_files_in_group",
        "old_alter_table",
    ])
    def test_known_vars_present(self, var):
        assert var in REMOVED_SYS_VARS_84


class TestReservedKeywords:
    """예약어 상수 검증"""

    def test_84_keywords_count(self):
        assert len(NEW_RESERVED_KEYWORDS_84) == 4

    @pytest.mark.parametrize("kw", ["MANUAL", "PARALLEL", "QUALIFY", "TABLESAMPLE"])
    def test_84_keywords_present(self, kw):
        assert kw in NEW_RESERVED_KEYWORDS_84

    def test_all_reserved_is_union(self):
        assert set(ALL_RESERVED_KEYWORDS) == set(RESERVED_KEYWORDS_80) | set(NEW_RESERVED_KEYWORDS_84)

    def test_all_reserved_unique(self):
        assert len(set(ALL_RESERVED_KEYWORDS)) == len(ALL_RESERVED_KEYWORDS)

    def test_80_keywords_not_empty(self):
        assert len(RESERVED_KEYWORDS_80) > 0


class TestObsoleteSqlModes:
    """OBSOLETE_SQL_MODES 검증"""

    def test_is_tuple(self):
        assert isinstance(OBSOLETE_SQL_MODES, tuple)

    def test_all_unique(self):
        assert len(set(OBSOLETE_SQL_MODES)) == len(OBSOLETE_SQL_MODES)

    @pytest.mark.parametrize("mode", ["ORACLE", "MYSQL323", "MYSQL40", "NO_AUTO_CREATE_USER"])
    def test_known_modes(self, mode):
        assert mode in OBSOLETE_SQL_MODES


class TestAuthPlugins:
    """AUTH_PLUGINS dict 구조 검증"""

    def test_keys_exist(self):
        assert 'disabled' in AUTH_PLUGINS
        assert 'removed' in AUTH_PLUGINS
        assert 'deprecated' in AUTH_PLUGINS
        assert 'recommended' in AUTH_PLUGINS

    def test_mysql_native_password_disabled(self):
        assert 'mysql_native_password' in AUTH_PLUGINS['disabled']

    def test_caching_sha2_recommended(self):
        assert 'caching_sha2_password' in AUTH_PLUGINS['recommended']


class TestSysVarsNewDefaults:
    """SYS_VARS_NEW_DEFAULTS_84 구조 검증"""

    def test_each_has_old_and_new(self):
        for var, val in SYS_VARS_NEW_DEFAULTS_84.items():
            assert 'old' in val, f"{var} missing 'old'"
            assert 'new' in val, f"{var} missing 'new'"

    def test_no_overlap_with_removed(self):
        """기본값 변경 변수는 제거된 변수와 겹치지 않아야 함"""
        for var in SYS_VARS_NEW_DEFAULTS_84:
            assert var not in REMOVED_SYS_VARS_84, f"{var} is both in defaults and removed"


class TestIdentifierLimits:
    """IDENTIFIER_LIMITS, INDEX_SIZE_LIMITS 검증"""

    def test_table_name_limit(self):
        assert IDENTIFIER_LIMITS['TABLE_NAME'] == 64

    def test_column_name_limit(self):
        assert IDENTIFIER_LIMITS['COLUMN_NAME'] == 64

    def test_innodb_max_key_length(self):
        assert INDEX_SIZE_LIMITS['INNODB_MAX_KEY_LENGTH'] == 3072

    def test_all_values_positive(self):
        for k, v in IDENTIFIER_LIMITS.items():
            assert v > 0, f"{k} should be positive"
        for k, v in INDEX_SIZE_LIMITS.items():
            assert v > 0, f"{k} should be positive"


class TestCharsetConstants:
    """Charset 관련 상수 검증"""

    def test_utf8_maps_to_utf8mb4(self):
        assert CHARSET_MIGRATION_MAP['utf8'] == 'utf8mb4'

    def test_utf8mb3_maps_to_utf8mb4(self):
        assert CHARSET_MIGRATION_MAP['utf8mb3'] == 'utf8mb4'

    def test_bytes_per_char_utf8mb4(self):
        assert CHARSET_BYTES_PER_CHAR['utf8mb4'] == 4

    def test_bytes_per_char_latin1(self):
        assert CHARSET_BYTES_PER_CHAR['latin1'] == 1


class TestStorageEngineStatus:
    """STORAGE_ENGINE_STATUS 검증"""

    def test_deprecated_engines(self):
        assert 'MyISAM' in STORAGE_ENGINE_STATUS['deprecated']

    def test_recommended_is_innodb(self):
        assert STORAGE_ENGINE_STATUS['recommended'] == 'InnoDB'


class TestMysqlSchemaTables:
    """MYSQL_SCHEMA_TABLES 검증"""

    def test_is_tuple(self):
        assert isinstance(MYSQL_SCHEMA_TABLES, tuple)

    def test_all_unique(self):
        assert len(set(MYSQL_SCHEMA_TABLES)) == len(MYSQL_SCHEMA_TABLES)

    def test_known_tables_present(self):
        assert 'tables' in MYSQL_SCHEMA_TABLES
        assert 'columns' in MYSQL_SCHEMA_TABLES


# ============================================================
# IssueType Enum 테스트
# ============================================================
class TestIssueType:
    """IssueType Enum 검증"""

    def test_all_values_unique(self):
        values = [e.value for e in IssueType]
        assert len(set(values)) == len(values)

    def test_all_values_are_strings(self):
        for e in IssueType:
            assert isinstance(e.value, str)

    @pytest.mark.parametrize("member,value", [
        ("CHARSET_ISSUE", "charset_issue"),
        ("RESERVED_KEYWORD", "reserved_keyword"),
        ("INVALID_DATE", "invalid_date"),
        ("DEPRECATED_ENGINE", "deprecated_engine"),
        ("AUTH_PLUGIN_ISSUE", "auth_plugin_issue"),
        ("FK_NON_UNIQUE_REF", "fk_non_unique_ref"),
    ])
    def test_known_members(self, member, value):
        assert IssueType[member].value == value

    def test_from_value_roundtrip(self):
        for e in IssueType:
            assert IssueType(e.value) is e


# ============================================================
# CompatibilityIssue 데이터클래스 테스트
# ============================================================
class TestCompatibilityIssue:
    """CompatibilityIssue dataclass 검증"""

    def test_construction(self):
        issue = CompatibilityIssue(
            issue_type=IssueType.CHARSET_ISSUE,
            severity="warning",
            location="db.table",
            description="test",
            suggestion="fix it",
        )
        assert issue.issue_type == IssueType.CHARSET_ISSUE
        assert issue.severity == "warning"

    def test_optional_fields_default_none(self):
        issue = CompatibilityIssue(
            issue_type=IssueType.CHARSET_ISSUE,
            severity="warning",
            location="db.table",
            description="test",
            suggestion="fix",
        )
        assert issue.fix_query is None
        assert issue.doc_link is None
        assert issue.table_name is None
        assert issue.column_name is None

    def test_with_all_fields(self):
        issue = CompatibilityIssue(
            issue_type=IssueType.INVALID_DATE,
            severity="error",
            location="db.t.c",
            description="desc",
            suggestion="sugg",
            fix_query="UPDATE ...",
            doc_link="https://...",
            mysql_shell_check_id="zeroDates",
            code_snippet="code",
            table_name="t",
            column_name="c",
        )
        assert issue.fix_query == "UPDATE ..."
        assert issue.table_name == "t"


# ============================================================
# 매핑 테스트
# ============================================================
class TestMysqlShellCheckIds:
    """MYSQL_SHELL_CHECK_IDS 매핑 검증"""

    def test_all_keys_are_issue_type(self):
        for key in MYSQL_SHELL_CHECK_IDS:
            assert isinstance(key, IssueType)

    def test_all_values_are_strings(self):
        for val in MYSQL_SHELL_CHECK_IDS.values():
            assert isinstance(val, str)

    def test_known_mapping(self):
        assert MYSQL_SHELL_CHECK_IDS[IssueType.REMOVED_SYS_VAR] == "removedSysVars"


class TestDocLinks:
    """DOC_LINKS 매핑 검증"""

    def test_all_keys_are_issue_type(self):
        for key in DOC_LINKS:
            assert isinstance(key, IssueType)

    def test_all_values_are_urls(self):
        for val in DOC_LINKS.values():
            assert val.startswith("https://")

    def test_charset_issue_has_link(self):
        assert IssueType.CHARSET_ISSUE in DOC_LINKS


# ============================================================
# 정규식 패턴 테스트
# ============================================================
class TestInvalidDatePattern:
    """INVALID_DATE_PATTERN 검증"""

    @pytest.mark.parametrize("text", [
        "'0000-00-00'",
        "\"0000-00-00\"",
        "0000-00-00",
    ])
    def test_matches_zero_date(self, text):
        assert INVALID_DATE_PATTERN.search(text)

    @pytest.mark.parametrize("text", [
        "'2024-01-15'",
        "'1970-01-01'",
    ])
    def test_no_match_valid_date(self, text):
        assert not INVALID_DATE_PATTERN.search(text)


class TestInvalidDatetimePattern:
    @pytest.mark.parametrize("text", [
        "'0000-00-00 00:00:00'",
        "\"0000-00-00 00:00:00\"",
    ])
    def test_matches(self, text):
        assert INVALID_DATETIME_PATTERN.search(text)


class TestZerofillPattern:
    @pytest.mark.parametrize("text,expected", [
        ("int(8) UNSIGNED ZEROFILL", True),
        ("INT(5) zerofill", True),
        ("int(11) NOT NULL", False),
        ("varchar(255)", False),
    ])
    def test_match(self, text, expected):
        result = ZEROFILL_PATTERN.search(text) is not None
        assert result == expected


class TestFloatPrecisionPattern:
    @pytest.mark.parametrize("text,expected", [
        ("FLOAT(10,2)", True),
        ("DOUBLE(8,4)", True),
        ("REAL(5,3)", True),
        ("float(10, 2)", True),
        ("FLOAT", False),
        ("DECIMAL(10,2)", False),
    ])
    def test_match(self, text, expected):
        result = FLOAT_PRECISION_PATTERN.search(text) is not None
        assert result == expected


class TestIntDisplayWidthPattern:
    @pytest.mark.parametrize("text,expected_match,expected_width", [
        ("INT(11)", True, "11"),
        ("BIGINT(20)", True, "20"),
        ("TINYINT(1)", True, "1"),
        ("SMALLINT(5)", True, "5"),
        ("INT", False, None),
        ("VARCHAR(255)", False, None),
    ])
    def test_match(self, text, expected_match, expected_width):
        m = INT_DISPLAY_WIDTH_PATTERN.search(text)
        if expected_match:
            assert m is not None
            assert m.group(2) == expected_width
        else:
            assert m is None


class TestFKNameLengthPattern:
    def test_matches_long_name(self):
        name = "a" * 65
        text = f"CONSTRAINT `{name}` FOREIGN KEY"
        assert FK_NAME_LENGTH_PATTERN.search(text)

    def test_no_match_short_name(self):
        name = "a" * 64
        text = f"CONSTRAINT `{name}` FOREIGN KEY"
        assert not FK_NAME_LENGTH_PATTERN.search(text)


class TestAuthPluginPattern:
    @pytest.mark.parametrize("text,expected", [
        ("IDENTIFIED WITH mysql_native_password", True),
        ("IDENTIFIED WITH 'sha256_password'", True),
        ("IDENTIFIED WITH caching_sha2_password", False),
        ("IDENTIFIED BY 'password'", False),
    ])
    def test_match(self, text, expected):
        result = AUTH_PLUGIN_PATTERN.search(text) is not None
        assert result == expected


class TestFTSTablePrefixPattern:
    @pytest.mark.parametrize("text,expected", [
        ("CREATE TABLE `FTS_config` (", True),
        ("CREATE TABLE FTS_data (", True),
        ("CREATE TABLE `users` (", False),
    ])
    def test_match(self, text, expected):
        result = FTS_TABLE_PREFIX_PATTERN.search(text) is not None
        assert result == expected


class TestSuperPrivilegePattern:
    @pytest.mark.parametrize("text,expected", [
        ("GRANT SUPER ON *.* TO 'admin'@'%'", True),
        ("GRANT SELECT, SUPER ON *.* TO 'user'@'%'", True),
        ("GRANT SELECT ON *.* TO 'user'@'%'", False),
    ])
    def test_match(self, text, expected):
        result = SUPER_PRIVILEGE_PATTERN.search(text) is not None
        assert result == expected


class TestYear2Pattern:
    @pytest.mark.parametrize("text,expected", [
        ("YEAR(2)", True),
        ("year( 2 )", True),
        ("YEAR(4)", False),
        ("YEAR", False),
    ])
    def test_match(self, text, expected):
        result = YEAR2_PATTERN.search(text) is not None
        assert result == expected


class TestEnumEmptyPattern:
    @pytest.mark.parametrize("text,expected", [
        ("ENUM('active','','inactive')", True),
        ("ENUM('', 'a')", True),
        ("ENUM('active','inactive')", False),
    ])
    def test_match(self, text, expected):
        result = ENUM_EMPTY_PATTERN.search(text) is not None
        assert result == expected


class TestDollarSignPattern:
    @pytest.mark.parametrize("text,expected", [
        ("`price$usd`", True),
        ("`$table`", True),
        ("`normal_name`", False),
    ])
    def test_match(self, text, expected):
        result = DOLLAR_SIGN_PATTERN.search(text) is not None
        assert result == expected


class TestTrailingSpacePattern:
    @pytest.mark.parametrize("text,expected", [
        ("`name `", True),
        ("`name  `", True),
        ("`name`", False),
    ])
    def test_match(self, text, expected):
        result = TRAILING_SPACE_PATTERN.search(text) is not None
        assert result == expected


class TestControlCharPattern:
    @pytest.mark.parametrize("text,expected", [
        ("`na\\x00me`", True),
        ("`na\\x1fme`", True),
        ("`normal`", False),
    ])
    def test_match(self, text, expected):
        # Build actual string with control chars
        if "\\x00" in text:
            text = "`na\x00me`"
        elif "\\x1f" in text:
            text = "`na\x1fme`"
        result = CONTROL_CHAR_PATTERN.search(text) is not None
        assert result == expected


class TestTimestampPattern:
    def test_matches_timestamp(self):
        m = TIMESTAMP_PATTERN.search("'2024-01-15 10:30:45'")
        assert m is not None
        assert m.group(1) == "2024"

    def test_no_match_date_only(self):
        assert not TIMESTAMP_PATTERN.search("'2024-01-15'")


class TestBlobTextDefaultPattern:
    @pytest.mark.parametrize("text,expected", [
        ("`data` TEXT DEFAULT 'hello'", True),
        ("`data` BLOB DEFAULT ''", True),
        ("`data` LONGTEXT DEFAULT NULL", True),
        ("`data` VARCHAR(255) DEFAULT ''", False),
    ])
    def test_match(self, text, expected):
        result = BLOB_TEXT_DEFAULT_PATTERN.search(text) is not None
        assert result == expected


class TestSysVarUsagePattern:
    @pytest.mark.parametrize("text,expected", [
        ("SET @@global.binlog_format = 'ROW'", True),
        ("SELECT @@session.old_alter_table", True),
        ("SET innodb_buffer_pool_size = 128M", False),
    ])
    def test_match(self, text, expected):
        result = SYS_VAR_USAGE_PATTERN.search(text) is not None
        assert result == expected


class TestDeprecatedSyntaxPatterns:
    """DEPRECATED_SYNTAX_PATTERNS dict 검증"""

    def test_group_by_asc_desc(self):
        pattern = DEPRECATED_SYNTAX_PATTERNS['GROUP_BY_ASC_DESC']
        assert pattern.search("SELECT * FROM t GROUP BY col ASC")
        assert not pattern.search("SELECT * FROM t ORDER BY col ASC")

    def test_sql_calc_found_rows(self):
        pattern = DEPRECATED_SYNTAX_PATTERNS['SQL_CALC_FOUND_ROWS']
        assert pattern.search("SELECT SQL_CALC_FOUND_ROWS * FROM t")
        assert not pattern.search("SELECT * FROM t")

    def test_found_rows_func(self):
        pattern = DEPRECATED_SYNTAX_PATTERNS['FOUND_ROWS_FUNC']
        assert pattern.search("SELECT FOUND_ROWS()")
        assert not pattern.search("SELECT COUNT(*)")


# ============================================================
# Canonical Parity 테스트 (mysql-upgrade-checker 기준값 대비)
# ============================================================
class TestCanonicalParity:
    """mysql-upgrade-checker canonical 값과의 parity 검증.

    canonical_constants.json에 정의된 기준값이 migration_constants.py에
    모두 포함되어 있는지 검증합니다. 기준값보다 많은 항목은 허용하되,
    기준값에 있는 항목이 누락되면 실패합니다.
    """

    def test_removed_sys_vars_parity(self, canonical_constants):
        """REMOVED_SYS_VARS_84가 canonical 기준값을 모두 포함하는지 검증"""
        canonical = set(canonical_constants["removed_sys_vars"])
        actual = set(REMOVED_SYS_VARS_84)
        missing = canonical - actual
        assert not missing, f"REMOVED_SYS_VARS_84에 누락된 항목: {missing}"

    def test_removed_functions_84_parity(self, canonical_constants):
        """REMOVED_FUNCTIONS_84가 canonical 기준값을 모두 포함하는지 검증"""
        canonical = set(canonical_constants["removed_functions_84"])
        actual = set(REMOVED_FUNCTIONS_84)
        missing = canonical - actual
        assert not missing, f"REMOVED_FUNCTIONS_84에 누락된 항목: {missing}"

    def test_deprecated_functions_84_parity(self, canonical_constants):
        """DEPRECATED_FUNCTIONS_84가 canonical 기준값을 모두 포함하는지 검증"""
        canonical = set(canonical_constants["deprecated_functions_84"])
        actual = set(DEPRECATED_FUNCTIONS_84)
        missing = canonical - actual
        assert not missing, f"DEPRECATED_FUNCTIONS_84에 누락된 항목: {missing}"

    def test_removed_functions_80x_parity(self, canonical_constants):
        """REMOVED_FUNCTIONS_80X가 canonical 기준값을 모두 포함하는지 검증"""
        canonical = set(canonical_constants["removed_functions_80x"])
        actual = set(REMOVED_FUNCTIONS_80X)
        missing = canonical - actual
        assert not missing, f"REMOVED_FUNCTIONS_80X에 누락된 항목: {missing}"

    def test_new_reserved_keywords_84_parity(self, canonical_constants):
        """NEW_RESERVED_KEYWORDS_84가 canonical 기준값을 모두 포함하는지 검증"""
        canonical = set(canonical_constants["new_reserved_keywords_84"])
        actual = set(NEW_RESERVED_KEYWORDS_84)
        missing = canonical - actual
        assert not missing, f"NEW_RESERVED_KEYWORDS_84에 누락된 항목: {missing}"

    def test_obsolete_sql_modes_parity(self, canonical_constants):
        """OBSOLETE_SQL_MODES가 canonical 기준값을 모두 포함하는지 검증"""
        canonical = set(canonical_constants["obsolete_sql_modes"])
        actual = set(OBSOLETE_SQL_MODES)
        missing = canonical - actual
        assert not missing, f"OBSOLETE_SQL_MODES에 누락된 항목: {missing}"

    def test_canonical_fixture_is_loadable(self, canonical_constants):
        """canonical_constants fixture가 정상적으로 로드되는지 검증"""
        assert "removed_sys_vars" in canonical_constants
        assert "removed_functions_84" in canonical_constants
        assert "deprecated_functions_84" in canonical_constants
        assert "removed_functions_80x" in canonical_constants
        assert "new_reserved_keywords_84" in canonical_constants
        assert "obsolete_sql_modes" in canonical_constants

    def test_removed_sys_vars_no_duplicates_vs_canonical(self, canonical_constants):
        """canonical 기준값 자체에 중복이 없는지 검증"""
        canonical = canonical_constants["removed_sys_vars"]
        assert len(set(canonical)) == len(canonical), "canonical removed_sys_vars에 중복 항목 있음"
