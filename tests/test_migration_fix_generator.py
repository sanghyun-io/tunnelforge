"""
migration_fix_generator.py 단위 테스트

Fix SQL 생성 + adversarial identifier injection 검증.
"""
import pytest
from unittest.mock import MagicMock

from src.core.migration_constants import IssueType, CompatibilityIssue, DOC_LINKS
from src.core.migration_fix_generator import FixQueryGenerator


@pytest.fixture
def generator():
    return FixQueryGenerator()


def _make_issue(issue_type, location="test_db.users", table_name=None, column_name=None, description=""):
    return CompatibilityIssue(
        issue_type=issue_type,
        severity="error",
        location=location,
        description=description,
        suggestion="fix it",
        table_name=table_name,
        column_name=column_name,
    )


# ============================================================
# Fix Generator 기본 동작 테스트
# ============================================================
class TestFixQueryGeneratorBasic:
    """FixQueryGenerator 기본 동작"""

    def test_generate_returns_issue(self, generator):
        issue = _make_issue(IssueType.CHARSET_ISSUE, "db.tbl")
        result = generator.generate(issue)
        assert result is issue  # 동일 객체 반환

    def test_doc_link_added(self, generator):
        issue = _make_issue(IssueType.CHARSET_ISSUE, "db.tbl")
        generator.generate(issue)
        assert issue.doc_link == DOC_LINKS[IssueType.CHARSET_ISSUE]

    def test_unknown_issue_type_no_fix(self, generator):
        """매핑되지 않은 이슈 타입은 fix_query 없음"""
        issue = _make_issue(IssueType.ORPHAN_ROW)
        generator.generate(issue)
        assert issue.fix_query is None

    def test_generate_all(self, generator):
        issues = [
            _make_issue(IssueType.CHARSET_ISSUE, "db.tbl"),
            _make_issue(IssueType.INVALID_DATE, table_name="t", column_name="c"),
        ]
        results = generator.generate_all(issues)
        assert len(results) == 2


# ============================================================
# 이슈 타입별 Fix 생성 테스트
# ============================================================
class TestAuthPluginFix:
    def test_with_user_host(self, generator):
        issue = _make_issue(IssueType.AUTH_PLUGIN_ISSUE, "'admin'@'localhost'")
        generator.generate(issue)
        assert "ALTER USER" in issue.fix_query
        assert "caching_sha2_password" in issue.fix_query
        assert "'admin'" in issue.fix_query

    def test_without_user_host(self, generator):
        issue = _make_issue(IssueType.AUTH_PLUGIN_ISSUE, "invalid_location")
        generator.generate(issue)
        assert "ALTER USER" in issue.fix_query


class TestCharsetFix:
    def test_table_level(self, generator):
        issue = _make_issue(IssueType.CHARSET_ISSUE, "mydb.users")
        generator.generate(issue)
        assert "ALTER TABLE" in issue.fix_query
        assert "`mydb`.`users`" in issue.fix_query
        assert "utf8mb4" in issue.fix_query

    def test_column_level(self, generator):
        issue = _make_issue(IssueType.CHARSET_ISSUE, "mydb.users.name")
        generator.generate(issue)
        assert "MODIFY COLUMN" in issue.fix_query

    def test_single_part_location(self, generator):
        issue = _make_issue(IssueType.CHARSET_ISSUE, "users")
        generator.generate(issue)
        assert issue.fix_query is not None


class TestInvalidDateFix:
    def test_with_table_column(self, generator):
        issue = _make_issue(IssueType.INVALID_DATE, table_name="orders", column_name="created_at")
        generator.generate(issue)
        assert "UPDATE" in issue.fix_query
        assert "`orders`" in issue.fix_query
        assert "`created_at`" in issue.fix_query

    def test_without_table_column(self, generator):
        issue = _make_issue(IssueType.INVALID_DATE)
        generator.generate(issue)
        assert issue.fix_query is not None


class TestZerofillFix:
    def test_with_details(self, generator):
        issue = _make_issue(IssueType.ZEROFILL_USAGE, table_name="t", column_name="c")
        generator.generate(issue)
        assert "ZEROFILL" in issue.fix_query
        assert "LPAD" in issue.fix_query

    def test_without_details(self, generator):
        issue = _make_issue(IssueType.ZEROFILL_USAGE)
        generator.generate(issue)
        assert issue.fix_query is not None


class TestFloatPrecisionFix:
    def test_with_details(self, generator):
        issue = _make_issue(IssueType.FLOAT_PRECISION, table_name="t", column_name="val")
        generator.generate(issue)
        assert "DECIMAL" in issue.fix_query

    def test_without_details(self, generator):
        issue = _make_issue(IssueType.FLOAT_PRECISION)
        generator.generate(issue)
        assert issue.fix_query is not None


class TestYear2Fix:
    def test_with_details(self, generator):
        issue = _make_issue(IssueType.YEAR2_TYPE, table_name="t", column_name="yr")
        generator.generate(issue)
        assert "YEAR(4)" in issue.fix_query


class TestDeprecatedEngineFix:
    def test_with_location(self, generator):
        issue = _make_issue(IssueType.DEPRECATED_ENGINE, "db.old_table", table_name="old_table")
        generator.generate(issue)
        assert "ENGINE=InnoDB" in issue.fix_query

    def test_with_location_no_table_name(self, generator):
        issue = _make_issue(IssueType.DEPRECATED_ENGINE, "db.old_table")
        generator.generate(issue)
        assert "ENGINE=InnoDB" in issue.fix_query


class TestKeywordFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.RESERVED_KEYWORD, "db.rank")
        generator.generate(issue)
        assert "RENAME" in issue.fix_query or "백틱" in issue.fix_query


class TestRemovedSysvarFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(
            IssueType.REMOVED_SYS_VAR,
            description="binlog_format variable is removed"
        )
        generator.generate(issue)
        assert "제거" in issue.fix_query or "removed" in issue.fix_query.lower() or "my.cnf" in issue.fix_query


class TestGroupByFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.GROUPBY_ASC_DESC)
        generator.generate(issue)
        assert "ORDER BY" in issue.fix_query


class TestFoundRowsFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.SQL_CALC_FOUND_ROWS_USAGE)
        generator.generate(issue)
        assert "COUNT" in issue.fix_query


class TestTimestampFix:
    def test_with_details(self, generator):
        issue = _make_issue(IssueType.TIMESTAMP_RANGE, table_name="t", column_name="ts")
        generator.generate(issue)
        assert "DATETIME" in issue.fix_query

    def test_without_details(self, generator):
        issue = _make_issue(IssueType.TIMESTAMP_RANGE)
        generator.generate(issue)
        assert "DATETIME" in issue.fix_query


class TestBlobDefaultFix:
    def test_with_details(self, generator):
        issue = _make_issue(IssueType.BLOB_TEXT_DEFAULT, table_name="t", column_name="data")
        generator.generate(issue)
        assert "DROP DEFAULT" in issue.fix_query


class TestDeprecatedFunctionFix:
    def test_password_function(self, generator):
        issue = _make_issue(IssueType.DEPRECATED_FUNCTION, description="PASSWORD() function used")
        generator.generate(issue)
        assert issue.fix_query is not None


class TestSqlModeFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.SQL_MODE_ISSUE, description="ORACLE mode deprecated")
        generator.generate(issue)
        assert "sql_mode" in issue.fix_query.lower() or "SQL" in issue.fix_query


class TestFtsTablePrefixFix:
    def test_generates_rename(self, generator):
        issue = _make_issue(IssueType.FTS_TABLE_PREFIX, table_name="FTS_config")
        generator.generate(issue)
        assert "RENAME" in issue.fix_query or "FTS_" in issue.fix_query


class TestFkRefNotFoundFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.FK_REF_NOT_FOUND, description="ref table missing")
        generator.generate(issue)
        assert "DROP FOREIGN KEY" in issue.fix_query or "FK" in issue.fix_query


class TestIntDisplayWidthFix:
    def test_with_details(self, generator):
        issue = _make_issue(IssueType.INT_DISPLAY_WIDTH, table_name="t", column_name="c")
        generator.generate(issue)
        assert "8.4" in issue.fix_query or "무시" in issue.fix_query


class TestSuperPrivilegeFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.SUPER_PRIVILEGE)
        generator.generate(issue)
        assert "GRANT" in issue.fix_query


class TestFkUniqueFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.FK_NON_UNIQUE_REF)
        generator.generate(issue)
        assert "UNIQUE" in issue.fix_query


class TestPartitionFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.PARTITION_ISSUE, table_name="partitioned_table")
        generator.generate(issue)
        assert "파티션" in issue.fix_query or "partition" in issue.fix_query.lower()


class TestEnumFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.ENUM_EMPTY_VALUE, table_name="t", column_name="status")
        generator.generate(issue)
        assert "ENUM" in issue.fix_query


class TestIndexFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.INDEX_TOO_LARGE)
        generator.generate(issue)
        assert "인덱스" in issue.fix_query or "INDEX" in issue.fix_query


class TestFkNameFix:
    def test_generates_guidance(self, generator):
        issue = _make_issue(IssueType.FK_NAME_LENGTH, table_name="orders")
        generator.generate(issue)
        assert "FK" in issue.fix_query or "64" in issue.fix_query


class TestLatin1Fix:
    def test_with_location(self, generator):
        issue = _make_issue(IssueType.LATIN1_CHARSET, "db.old_table")
        generator.generate(issue)
        assert "utf8mb4" in issue.fix_query


# ============================================================
# Adversarial Identifier 테스트
# ============================================================
class TestAdversarialIdentifiers:
    """SQL injection, null bytes, unicode 등 악의적 식별자 테스트"""

    @pytest.mark.parametrize("bad_input", [
        "table`; DROP TABLE users; --",
        "table\x00name",
        "'table'",
        "스키마.테이블",
        "",
        "a" * 500,
        "table\nname",
        "table\\name",
        'table"name',
    ])
    def test_charset_fix_safe(self, generator, bad_input):
        """charset fix에 악의적 식별자를 넣어도 예외 없이 SQL 생성"""
        issue = _make_issue(IssueType.CHARSET_ISSUE, f"db.{bad_input}")
        result = generator.generate(issue)
        # 예외 없이 완료되어야 함
        assert result is issue

    @pytest.mark.parametrize("bad_input", [
        "table`; DROP TABLE users; --",
        "table\x00name",
        "",
        "a" * 500,
    ])
    def test_invalid_date_fix_safe(self, generator, bad_input):
        """invalid date fix에 악의적 식별자를 넣어도 예외 없이 SQL 생성"""
        issue = _make_issue(
            IssueType.INVALID_DATE,
            table_name=bad_input,
            column_name="col"
        )
        result = generator.generate(issue)
        assert result is issue

    @pytest.mark.parametrize("bad_input", [
        "'; DROP TABLE users; --",
        "admin\x00",
        "user@host",
    ])
    def test_auth_plugin_fix_safe(self, generator, bad_input):
        """auth plugin fix에 악의적 location을 넣어도 예외 없이 SQL 생성"""
        issue = _make_issue(IssueType.AUTH_PLUGIN_ISSUE, bad_input)
        result = generator.generate(issue)
        assert result is issue

    def test_backtick_in_table_name_charset(self, generator):
        """백틱이 포함된 테이블명으로 charset fix 생성"""
        issue = _make_issue(IssueType.CHARSET_ISSUE, "db.ta`ble")
        generator.generate(issue)
        # SQL은 생성되어야 하나, 실행은 위험
        assert issue.fix_query is not None

    def test_null_byte_in_column_name(self, generator):
        """NULL 바이트가 포함된 컬럼명"""
        issue = _make_issue(
            IssueType.ZEROFILL_USAGE,
            table_name="tbl",
            column_name="col\x00name"
        )
        generator.generate(issue)
        assert issue.fix_query is not None

    def test_unicode_in_engine_fix(self, generator):
        """유니코드 테이블명으로 engine fix"""
        issue = _make_issue(
            IssueType.DEPRECATED_ENGINE,
            "db.테이블명",
            table_name="테이블명"
        )
        generator.generate(issue)
        assert issue.fix_query is not None
        assert "ENGINE=InnoDB" in issue.fix_query


# ============================================================
# 전체 매핑 커버리지
# ============================================================
class TestGeneratorCoverage:
    """generator.generate() 내부 generators dict의 모든 키가 테스트됨을 보장"""

    def test_all_mapped_types_produce_fix(self, generator):
        """generators dict에 매핑된 모든 IssueType에 대해 fix_query 생성"""
        mapped_types = [
            IssueType.AUTH_PLUGIN_ISSUE,
            IssueType.CHARSET_ISSUE,
            IssueType.ZEROFILL_USAGE,
            IssueType.FLOAT_PRECISION,
            IssueType.INVALID_DATE,
            IssueType.YEAR2_TYPE,
            IssueType.DEPRECATED_ENGINE,
            IssueType.ENUM_EMPTY_VALUE,
            IssueType.INDEX_TOO_LARGE,
            IssueType.FK_NAME_LENGTH,
            IssueType.RESERVED_KEYWORD,
            IssueType.INT_DISPLAY_WIDTH,
            IssueType.LATIN1_CHARSET,
            IssueType.FK_NON_UNIQUE_REF,
            IssueType.SUPER_PRIVILEGE,
            IssueType.REMOVED_SYS_VAR,
            IssueType.GROUPBY_ASC_DESC,
            IssueType.SQL_CALC_FOUND_ROWS_USAGE,
            IssueType.PARTITION_ISSUE,
            IssueType.TIMESTAMP_RANGE,
            IssueType.BLOB_TEXT_DEFAULT,
            IssueType.DEPRECATED_FUNCTION,
            IssueType.SQL_MODE_ISSUE,
            IssueType.FTS_TABLE_PREFIX,
            IssueType.FK_REF_NOT_FOUND,
        ]
        for it in mapped_types:
            issue = _make_issue(it, "db.tbl", table_name="tbl", column_name="col", description="test func PASSWORD")
            generator.generate(issue)
            assert issue.fix_query is not None, f"{it} should produce fix_query"
