"""
마이그레이션 종단간 매핑 커버리지 테스트

IssueType → FixQueryGenerator → SmartFixGenerator 간 종단간 연결을 검증합니다.
AutoRecommendationEngine(Python 자동 추천 엔진)은 Rust DB Core로 이관되어
삭제되었으므로, 이 테스트는 더 이상 Python 추천 선택을 검증하지 않는다.
"""
import pytest

from src.core.migration_constants import IssueType, DOC_LINKS, CompatibilityIssue
from src.core.migration_fix_generator import FixQueryGenerator
from src.core.migration_fix_wizard import (
    FixStrategy,
    SmartFixGenerator,
    create_wizard_steps,
)
from tests.conftest import FakeMySQLConnector, make_issue


# ============================================================
# FixQueryGenerator 매핑 커버리지
# ============================================================
class TestFixQueryGeneratorCoverage:
    """FixQueryGenerator가 지원하는 IssueType 매핑 검증"""

    # FixQueryGenerator.generate() 내부 handlers 키 목록
    EXPECTED_FIX_GENERATOR_TYPES = {
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
    }

    def test_all_expected_types_have_generator(self):
        """FixQueryGenerator에 등록된 모든 타입이 실제 핸들러를 가지는지"""
        gen = FixQueryGenerator()
        for issue_type in self.EXPECTED_FIX_GENERATOR_TYPES:
            issue = make_issue(
                issue_type,
                location="test_db.table.col",
                table_name="table",
                column_name="col",
            )
            result = gen.generate(issue)
            # generate()는 issue를 반환 (fix_query가 추가될 수 있음)
            assert isinstance(result, CompatibilityIssue)

    @pytest.mark.parametrize("issue_type", list(IssueType))
    def test_generate_never_raises(self, issue_type):
        """모든 IssueType에 대해 generate()가 예외를 발생시키지 않음"""
        gen = FixQueryGenerator()
        issue = make_issue(
            issue_type,
            location="test_db.table.col",
            table_name="table",
            column_name="col",
        )
        result = gen.generate(issue)
        assert isinstance(result, CompatibilityIssue)


# ============================================================
# SmartFixGenerator 매핑 커버리지
# ============================================================
class TestSmartFixGeneratorCoverage:
    """SmartFixGenerator가 지원하는 IssueType 매핑 검증"""

    # SmartFixGenerator.get_fix_options() 내부 handlers 키 목록
    EXPECTED_WIZARD_TYPES = {
        IssueType.INVALID_DATE,
        IssueType.CHARSET_ISSUE,
        IssueType.ZEROFILL_USAGE,
        IssueType.FLOAT_PRECISION,
        IssueType.INT_DISPLAY_WIDTH,
        IssueType.ENUM_EMPTY_VALUE,
        IssueType.DEPRECATED_ENGINE,
    }

    def test_all_wizard_types_produce_options(self):
        """SmartFixGenerator의 모든 핸들러 타입이 옵션을 생성하는지"""
        conn = FakeMySQLConnector()
        # INFORMATION_SCHEMA.COLUMNS를 먼저 배치하여 column 쿼리에 우선 매칭
        conn.query_results = {
            'INFORMATION_SCHEMA.COLUMNS': [{
                'COLUMN_TYPE': 'varchar(255)',
                'IS_NULLABLE': 'YES',
                'COLUMN_DEFAULT': None,
                'EXTRA': '',
            }],
            'KEY_COLUMN_USAGE': [],
        }
        gen = SmartFixGenerator(conn, "test_db")
        for issue_type in self.EXPECTED_WIZARD_TYPES:
            issue = make_issue(
                issue_type,
                location="test_db.table.col",
                table_name="table",
                column_name="col",
            )
            options = gen.get_fix_options(issue)
            assert len(options) >= 1, f"{issue_type}: no options"
            # 모든 옵션에 SKIP이 포함
            strategies = [o.strategy for o in options]
            assert FixStrategy.SKIP in strategies, f"{issue_type}: no SKIP option"

    @pytest.mark.parametrize("issue_type", list(IssueType))
    def test_get_fix_options_never_raises(self, issue_type):
        """모든 IssueType에 대해 get_fix_options()가 예외 없이 동작"""
        conn = FakeMySQLConnector()
        # INFORMATION_SCHEMA.COLUMNS를 먼저 배치 (_get_column_definition 포함 쿼리 우선 매칭)
        conn.query_results = {
            'INFORMATION_SCHEMA.COLUMNS': [{
                'COLUMN_TYPE': 'varchar(255)',
                'IS_NULLABLE': 'YES',
                'COLUMN_DEFAULT': None,
                'EXTRA': '',
            }],
            'KEY_COLUMN_USAGE': [],
        }
        gen = SmartFixGenerator(conn, "test_db")
        issue = make_issue(
            issue_type,
            location="test_db.table.col",
            table_name="table",
            column_name="col",
        )
        options = gen.get_fix_options(issue)
        assert len(options) >= 1  # 최소 SKIP
        assert options[-1].strategy == FixStrategy.SKIP  # SKIP은 항상 마지막


# ============================================================
# DOC_LINKS 커버리지
# ============================================================
class TestDocLinksCoverage:
    """DOC_LINKS 매핑 검증"""

    def test_all_doc_links_are_issue_type(self):
        """DOC_LINKS의 키가 IssueType인지"""
        for key in DOC_LINKS:
            assert isinstance(key, IssueType), f"{key} is not IssueType"

    def test_all_doc_links_are_urls(self):
        """DOC_LINKS의 값이 URL 형태인지"""
        for issue_type, url in DOC_LINKS.items():
            assert isinstance(url, str), f"{issue_type}: not string"
            assert url.startswith("http"), f"{issue_type}: {url} is not URL"


# ============================================================
# 종단간 Flow 테스트
# ============================================================
class TestEndToEndFlow:
    """IssueType → FixQueryGenerator → SmartFixGenerator → create_wizard_steps 흐름

    AutoRecommendationEngine(Python 자동 추천 선택)은 Rust DB Core로
    이관되어 삭제되었으므로, 여기서는 create_wizard_steps까지만 검증하고
    Python 추천 선택 결과는 assert하지 않는다.
    """

    def test_invalid_date_full_flow(self):
        """INVALID_DATE: 발견 → fix_query → wizard options"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'YES'}],
            'KEY_COLUMN_USAGE': [],
        }

        issue = make_issue(
            IssueType.INVALID_DATE,
            location="test_db.orders.created_at",
            table_name="orders",
            column_name="created_at",
        )

        # Step 1: FixQueryGenerator
        gen = FixQueryGenerator()
        gen.generate(issue)
        assert issue.fix_query is not None

        # Step 2: SmartFixGenerator
        wizard_gen = SmartFixGenerator(conn, "test_db")
        options = wizard_gen.get_fix_options(issue)
        assert len(options) >= 2
        assert any(o.strategy == FixStrategy.DATE_TO_NULL for o in options)

    def test_charset_full_flow(self):
        """CHARSET_ISSUE: 발견 → fix_query → wizard options"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': [],
        }

        issue = make_issue(
            IssueType.CHARSET_ISSUE,
            location="test_db.users",
            table_name="users",
        )

        # Step 1: FixQueryGenerator
        gen = FixQueryGenerator()
        gen.generate(issue)

        # Step 2: SmartFixGenerator
        wizard_gen = SmartFixGenerator(conn, "test_db")
        options = wizard_gen.get_fix_options(issue)
        assert any(o.strategy == FixStrategy.COLLATION_SINGLE for o in options)

    def test_int_display_width_full_flow(self):
        """INT_DISPLAY_WIDTH: SKIP 옵션이 항상 마지막에 포함됨"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}

        issue = make_issue(
            IssueType.INT_DISPLAY_WIDTH,
            location="test_db.products.stock",
            table_name="products",
            column_name="stock",
        )

        gen = FixQueryGenerator()
        gen.generate(issue)

        wizard_gen = SmartFixGenerator(conn, "test_db")
        options = wizard_gen.get_fix_options(issue)
        assert len(options) >= 1
        assert options[-1].strategy == FixStrategy.SKIP

    def test_deprecated_engine_full_flow(self):
        """DEPRECATED_ENGINE: 발견 → fix_query → wizard options"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}

        issue = make_issue(
            IssueType.DEPRECATED_ENGINE,
            location="test_db.logs",
            table_name="logs",
        )

        gen = FixQueryGenerator()
        gen.generate(issue)

        wizard_gen = SmartFixGenerator(conn, "test_db")
        options = wizard_gen.get_fix_options(issue)
        assert len(options) >= 1

    def test_create_wizard_steps_produces_step_per_issue(self):
        """create_wizard_steps: 이슈마다 옵션이 채워진 FixWizardStep 생성"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'YES'}],
            'KEY_COLUMN_USAGE': [],
        }

        issues = [
            make_issue(IssueType.INT_DISPLAY_WIDTH, table_name="t", column_name="c"),
            make_issue(IssueType.ZEROFILL_USAGE),
            make_issue(
                IssueType.INVALID_DATE,
                location="test_db.orders.created_at",
                table_name="orders", column_name="created_at",
            ),
        ]

        steps = create_wizard_steps(issues, conn, "test_db")
        assert len(steps) == 3
        for step in steps:
            assert len(step.options) >= 1
