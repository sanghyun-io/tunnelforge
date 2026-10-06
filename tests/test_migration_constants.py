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
    ENGINE_POLICIES,
    IssueType,
    CompatibilityIssue,
    INVALID_DATE_PATTERN,
    INVALID_DATETIME_PATTERN,
    ZEROFILL_PATTERN,
    FLOAT_PRECISION_PATTERN,
    FK_NAME_LENGTH_PATTERN,
    AUTH_PLUGIN_PATTERN,
    FTS_TABLE_PREFIX_PATTERN,
    SUPER_PRIVILEGE_PATTERN,
    SYS_VAR_USAGE_PATTERN,
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


class TestAllRemovedFunctions:
    """ALL_REMOVED_FUNCTIONS 중복 제거 불변량 검증"""

    def test_no_duplicates(self):
        assert len(set(ALL_REMOVED_FUNCTIONS)) == len(ALL_REMOVED_FUNCTIONS)

    def test_contains_all_source_members(self):
        expected = set(REMOVED_FUNCTIONS_84) | set(REMOVED_FUNCTIONS_80X) | set(DEPRECATED_FUNCTIONS_84)
        assert set(ALL_REMOVED_FUNCTIONS) == expected


class TestObsoleteSqlModes:
    """OBSOLETE_SQL_MODES 검증"""

    def test_is_tuple(self):
        assert isinstance(OBSOLETE_SQL_MODES, tuple)

    def test_all_unique(self):
        assert len(set(OBSOLETE_SQL_MODES)) == len(OBSOLETE_SQL_MODES)

    @pytest.mark.parametrize("mode", ["ORACLE", "MYSQL323", "MYSQL40", "NO_AUTO_CREATE_USER"])
    def test_known_modes(self, mode):
        assert mode in OBSOLETE_SQL_MODES


class TestEnginePolicies:
    """ENGINE_POLICIES 정책 dict 검증"""

    @pytest.mark.parametrize("engine", ["MERGE", "CSV", "EXAMPLE", "NDB"])
    def test_known_engines_present(self, engine):
        assert engine in ENGINE_POLICIES

    def test_merge_severity_is_error(self):
        assert ENGINE_POLICIES["MERGE"]["severity"] == "error"

    def test_every_policy_has_non_empty_fields(self):
        for engine, policy in ENGINE_POLICIES.items():
            assert policy.get("severity"), f"{engine} policy missing severity"
            assert policy.get("suggestion"), f"{engine} policy missing suggestion"


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

    def test_removed_members_absent(self):
        """실제 사용처가 없던 구식 구문 이슈 타입은 제거되어야 한다"""
        assert "TRIGGER_OLD_SYNTAX" not in IssueType.__members__
        assert "EVENT_OLD_SYNTAX" not in IssueType.__members__


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
            upgrade_check_id="zeroDates",
            code_snippet="code",
            table_name="t",
            column_name="c",
        )
        assert issue.fix_query == "UPDATE ..."
        assert issue.table_name == "t"


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


class TestSysVarUsagePattern:
    @pytest.mark.parametrize("text,expected", [
        ("SET @@global.binlog_format = 'ROW'", True),
        ("SELECT @@session.old_alter_table", True),
        ("SET NEW.updated_at = NOW()", False),
        ("SET OLD.status = 'archived'", False),
        ("SET innodb_buffer_pool_size = 128M", False),
    ])
    def test_match(self, text, expected):
        result = SYS_VAR_USAGE_PATTERN.search(text) is not None
        assert result == expected


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
