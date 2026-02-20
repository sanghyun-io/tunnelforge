"""
migration_auto_recommend.py 단위 테스트

AutoRecommendationEngine, DEFAULT_RECOMMENDATION_RULES 검증.
"""
import pytest
from unittest.mock import MagicMock

from src.core.migration_constants import IssueType, CompatibilityIssue
from src.core.migration_fix_wizard import (
    FixStrategy, FixOption, FixWizardStep,
)
from src.core.migration_auto_recommend import (
    AutoRecommendationEngine,
    DEFAULT_RECOMMENDATION_RULES,
    RecommendationReason,
    RecommendationRule,
    RecommendationSummary,
)
from tests.conftest import FakeMySQLConnector


def _make_issue(issue_type, location="test_db.table", table_name=None, column_name=None, **kw):
    return CompatibilityIssue(
        issue_type=issue_type,
        severity="warning",
        location=location,
        description="test",
        suggestion="fix",
        table_name=table_name,
        column_name=column_name,
        **kw,
    )


def _make_option(strategy, label="option", is_recommended=False, **kw):
    return FixOption(
        strategy=strategy,
        label=label,
        description="desc",
        is_recommended=is_recommended,
        **kw,
    )


def _make_step(issue_index, issue_type, options=None, selected_option=None):
    return FixWizardStep(
        issue_index=issue_index,
        issue_type=issue_type,
        location="test_db.table",
        description="test",
        options=options or [],
        selected_option=selected_option,
    )


# ============================================================
# DEFAULT_RECOMMENDATION_RULES 테스트
# ============================================================
class TestDefaultRecommendationRules:
    """기본 권장 규칙 매핑 검증"""

    def test_all_keys_are_issue_type(self):
        for key in DEFAULT_RECOMMENDATION_RULES:
            assert isinstance(key, IssueType), f"{key} is not IssueType"

    def test_all_values_are_recommendation_rule(self):
        for val in DEFAULT_RECOMMENDATION_RULES.values():
            assert isinstance(val, RecommendationRule)

    def test_all_risk_scores_in_range(self):
        for it, rule in DEFAULT_RECOMMENDATION_RULES.items():
            assert 0 <= rule.risk_base_score <= 100, f"{it}: {rule.risk_base_score}"

    def test_charset_issue_recommends_fk_safe(self):
        rule = DEFAULT_RECOMMENDATION_RULES[IssueType.CHARSET_ISSUE]
        assert rule.strategy == FixStrategy.COLLATION_FK_SAFE

    def test_int_display_width_recommends_skip(self):
        rule = DEFAULT_RECOMMENDATION_RULES[IssueType.INT_DISPLAY_WIDTH]
        assert rule.strategy == FixStrategy.SKIP

    def test_invalid_date_default_null(self):
        rule = DEFAULT_RECOMMENDATION_RULES[IssueType.INVALID_DATE]
        assert rule.strategy == FixStrategy.DATE_TO_NULL

    def test_deprecated_engine_is_manual(self):
        rule = DEFAULT_RECOMMENDATION_RULES[IssueType.DEPRECATED_ENGINE]
        assert rule.strategy == FixStrategy.MANUAL

    def test_known_types_covered(self):
        """주요 IssueType이 매핑에 포함되어야 함"""
        expected = [
            IssueType.CHARSET_ISSUE, IssueType.INVALID_DATE,
            IssueType.INT_DISPLAY_WIDTH, IssueType.ZEROFILL_USAGE,
            IssueType.DEPRECATED_ENGINE, IssueType.FLOAT_PRECISION,
            IssueType.RESERVED_KEYWORD, IssueType.AUTH_PLUGIN_ISSUE,
        ]
        for it in expected:
            assert it in DEFAULT_RECOMMENDATION_RULES, f"{it} missing"


# ============================================================
# AutoRecommendationEngine 테스트
# ============================================================
class TestRecommendForIssue:
    """recommend_for_issue 동작 테스트"""

    @pytest.fixture
    def engine(self):
        conn = FakeMySQLConnector()
        return AutoRecommendationEngine(conn, "test_db")

    def test_returns_is_recommended_option(self, engine):
        """is_recommended=True인 옵션이 있으면 그것을 선택"""
        options = [
            _make_option(FixStrategy.MANUAL, "수동"),
            _make_option(FixStrategy.DATE_TO_NULL, "NULL로 변경", is_recommended=True),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.INVALID_DATE)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.DATE_TO_NULL

    def test_returns_matching_strategy(self, engine):
        """기본 규칙과 일치하는 전략 선택"""
        options = [
            _make_option(FixStrategy.MANUAL, "수동"),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.ZEROFILL_USAGE)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.MANUAL

    def test_int_display_width_skips(self, engine):
        """INT 표시 너비는 SKIP 권장"""
        options = [
            _make_option(FixStrategy.MANUAL, "수동"),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.INT_DISPLAY_WIDTH)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.SKIP

    def test_fallback_to_first_non_skip(self, engine):
        """매핑 없는 타입이면 SKIP 아닌 첫 옵션"""
        options = [
            _make_option(FixStrategy.SKIP, "건너뛰기"),
            _make_option(FixStrategy.MANUAL, "수동"),
        ]
        issue = _make_issue(IssueType.ORPHAN_ROW)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.MANUAL

    def test_empty_options(self, engine):
        issue = _make_issue(IssueType.CHARSET_ISSUE)
        result = engine.recommend_for_issue(issue, [])
        assert result is None

    def test_all_skip_returns_first(self, engine):
        options = [
            _make_option(FixStrategy.SKIP, "건너뛰기1"),
            _make_option(FixStrategy.SKIP, "건너뛰기2"),
        ]
        issue = _make_issue(IssueType.ORPHAN_ROW)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.SKIP


class TestRecommendInvalidDate:
    """Invalid Date 동적 권장 테스트"""

    def test_nullable_column_prefers_null(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'YES'}]
        }
        engine = AutoRecommendationEngine(conn, "test_db")

        options = [
            _make_option(FixStrategy.DATE_TO_NULL, "NULL"),
            _make_option(FixStrategy.DATE_TO_MIN, "1970-01-01"),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.INVALID_DATE, table_name="orders", column_name="created_at")
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.DATE_TO_NULL

    def test_not_nullable_column_prefers_min(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'NO'}]
        }
        engine = AutoRecommendationEngine(conn, "test_db")

        options = [
            _make_option(FixStrategy.DATE_TO_NULL, "NULL"),
            _make_option(FixStrategy.DATE_TO_MIN, "1970-01-01"),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.INVALID_DATE, table_name="orders", column_name="created_at")
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.DATE_TO_MIN


class TestRecommendCharset:
    """Charset 동적 권장 테스트"""

    def test_prefers_fk_safe(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        options = [
            _make_option(FixStrategy.COLLATION_SINGLE, "단일"),
            _make_option(FixStrategy.COLLATION_FK_CASCADE, "FK 일괄"),
            _make_option(FixStrategy.COLLATION_FK_SAFE, "FK 안전"),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.CHARSET_ISSUE)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.COLLATION_FK_SAFE

    def test_fallback_to_cascade(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        options = [
            _make_option(FixStrategy.COLLATION_SINGLE, "단일"),
            _make_option(FixStrategy.COLLATION_FK_CASCADE, "FK 일괄"),
            _make_option(FixStrategy.SKIP, "건너뛰기"),
        ]
        issue = _make_issue(IssueType.CHARSET_ISSUE)
        result = engine.recommend_for_issue(issue, options)
        assert result.strategy == FixStrategy.COLLATION_FK_CASCADE


class TestRecommendAll:
    """recommend_all 테스트"""

    def test_selects_options_for_all_steps(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        issues = [
            _make_issue(IssueType.INT_DISPLAY_WIDTH),
            _make_issue(IssueType.ZEROFILL_USAGE),
        ]
        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH, [
                _make_option(FixStrategy.MANUAL),
                _make_option(FixStrategy.SKIP),
            ]),
            _make_step(1, IssueType.ZEROFILL_USAGE, [
                _make_option(FixStrategy.MANUAL),
                _make_option(FixStrategy.SKIP),
            ]),
        ]

        result = engine.recommend_all(issues, steps)
        assert result[0].selected_option.strategy == FixStrategy.SKIP
        assert result[1].selected_option.strategy == FixStrategy.MANUAL


# ============================================================
# 리스크 스코어 테스트
# ============================================================
class TestCalculateRiskScore:
    def test_base_score_from_rules(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        issue = _make_issue(IssueType.INT_DISPLAY_WIDTH)
        score = engine.calculate_risk_score(issue)
        assert score == 0  # INT_DISPLAY_WIDTH base = 0

    def test_invalid_date_adds_bonus(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        issue = _make_issue(IssueType.INVALID_DATE)
        score = engine.calculate_risk_score(issue)
        assert score >= 30 + 10  # base 30 + data loss 10

    def test_max_100(self):
        """리스크 스코어는 100을 넘지 않음"""
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        issue = _make_issue(IssueType.FK_REF_NOT_FOUND)
        score = engine.calculate_risk_score(issue)
        assert score <= 100

    def test_unknown_issue_type(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        issue = _make_issue(IssueType.ORPHAN_ROW)
        score = engine.calculate_risk_score(issue)
        assert score == 0


# ============================================================
# Summary 테스트
# ============================================================
class TestGetSummary:
    def test_basic_summary(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        issues = [
            _make_issue(IssueType.INT_DISPLAY_WIDTH),
            _make_issue(IssueType.ZEROFILL_USAGE),
            _make_issue(IssueType.CHARSET_ISSUE),
        ]
        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH, selected_option=_make_option(FixStrategy.SKIP)),
            _make_step(1, IssueType.ZEROFILL_USAGE, selected_option=_make_option(FixStrategy.MANUAL)),
            _make_step(2, IssueType.CHARSET_ISSUE, selected_option=_make_option(FixStrategy.COLLATION_FK_SAFE)),
        ]

        summary = engine.get_summary(steps, issues)
        assert isinstance(summary, RecommendationSummary)
        assert summary.total_issues == 3
        assert summary.skip_recommended == 1
        assert summary.manual_review == 1
        assert summary.auto_fixable == 1

    def test_empty_steps(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")
        summary = engine.get_summary([], [])
        assert summary.total_issues == 0
        assert summary.average_risk_score == 0.0


# ============================================================
# 실행 순서 테스트
# ============================================================
class TestGetExecutionOrder:
    def test_orders_by_risk(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        steps = [
            _make_step(0, IssueType.CHARSET_ISSUE, selected_option=_make_option(FixStrategy.COLLATION_FK_SAFE)),
            _make_step(1, IssueType.INVALID_DATE, selected_option=_make_option(FixStrategy.DATE_TO_NULL)),
            _make_step(2, IssueType.INT_DISPLAY_WIDTH, selected_option=_make_option(FixStrategy.SKIP)),
        ]

        order = engine.get_execution_order(steps)
        # SKIP은 제외, DATE_TO_NULL(20) < COLLATION_FK_SAFE(30)
        assert order == [1, 0]

    def test_skips_excluded(self):
        conn = FakeMySQLConnector()
        engine = AutoRecommendationEngine(conn, "test_db")

        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH, selected_option=_make_option(FixStrategy.SKIP)),
            _make_step(1, IssueType.ZEROFILL_USAGE, selected_option=_make_option(FixStrategy.MANUAL)),
        ]
        order = engine.get_execution_order(steps)
        assert len(order) == 0  # SKIP, MANUAL 모두 제외
