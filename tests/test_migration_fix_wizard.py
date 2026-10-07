"""
migration_fix_wizard.py 단위 테스트

옵션/SQL 생성과 문자셋 계획 자체는 Rust core(upgrade_fix.rs 단위 테스트, live_upgrade_fix)가 검증한다.
여기서는 Rust 결과 → 위저드 모델 변환과 dry-run 요약만 검증한다.
"""
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from src.core.migration_constants import IssueType
from src.core.migration_fix_wizard import (
    BatchExecutionResult,
    BatchFixExecutor,
    FixOption,
    FixStrategy,
    FixWizardStep,
    build_fix_plan,
    charset_fix_sql,
    create_wizard_steps,
)
from tests.conftest import make_issue


@dataclass(frozen=True)
class _Endpoint:
    database: str = ""


class _FakeFacade:
    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []

    def plan_upgrade_fixes(self, endpoint, issues, charset_tables):
        self.calls.append(("plan", endpoint, issues, charset_tables))
        return self.plan

    def charset_fix_sql(self, endpoint, tables, charset, collation):
        self.calls.append(("charset", endpoint, tables, charset, collation))
        return {"full_sql": ["ALTER ..."], "fk_count": 1, "table_count": len(tables)}


def _connector(facade):
    return SimpleNamespace(connection=SimpleNamespace(facade=facade, endpoint=_Endpoint("other")))


PLAN = {
    "steps": [{"issue_index": 0, "options": [
        {"strategy": "date_to_null", "label": "NULL", "description": "d", "sql_template": "UPDATE ...",
         "is_recommended": True, "estimated_rows": 3},
        {"strategy": "skip", "label": "건너뛰기", "description": "s"},
    ]}],
    "charset_tables": [
        {"table_name": "parent", "current_charset": "utf8mb3", "current_collation": "utf8mb3_general_ci",
         "fk_parents": [], "fk_children": ["child"], "is_original_issue": False},
        {"table_name": "child", "current_charset": "utf8mb3", "current_collation": "utf8mb3_general_ci",
         "fk_parents": ["parent"], "fk_children": [], "is_original_issue": True},
    ],
    "cascade_skip": {"child": ["parent"], "parent": []},
    "foreign_keys": [{"constraint_name": "fk_code", "table_name": "child", "ref_table": "parent"}],
}


def _step(option, user_input=None, location="app.t"):
    return FixWizardStep(0, IssueType.INVALID_DATE, location, "test", [option], option, user_input)


def _option(strategy, sql=None, **kw):
    return FixOption(strategy=strategy, label="opt", description="desc", sql_template=sql, **kw)


class TestBuildFixPlan:
    def test_maps_core_plan_to_wizard_models(self):
        facade = _FakeFacade(PLAN)
        issue = make_issue(IssueType.INVALID_DATE, location="app.child.d", table_name="child", column_name="d")

        plan = build_fix_plan([issue], {"child"}, _connector(facade), "app")

        _, endpoint, issues, tables = facade.calls[0]
        assert endpoint.database == "app"
        assert issues[0]["issue_type"] == "invalid_date" and issues[0]["column_name"] == "d"
        assert tables == ["child"]

        step = plan.steps[0]
        assert step.issue_type == IssueType.INVALID_DATE and step.location == "app.child.d"
        assert [o.strategy for o in step.options] == [FixStrategy.DATE_TO_NULL, FixStrategy.SKIP]
        assert step.options[0].is_recommended and step.options[0].estimated_rows == 3

        charset = plan.charset_plan
        assert charset is not None
        assert [t.table_name for t in charset.build_full_table_list()] == ["parent", "child"]
        assert charset.tables[1].is_original_issue and charset.tables[0].fk_children == ["child"]
        assert charset.get_cascade_skip_tables("child") == {"parent"}
        assert charset.get_cascade_skip_tables("unknown") == set()
        assert charset.foreign_keys[0].table_name == "child" and charset.foreign_keys[0].ref_table == "parent"

    def test_no_charset_tables_means_no_charset_plan(self):
        facade = _FakeFacade({"steps": []})
        assert create_wizard_steps([], _connector(facade), "app") == []
        assert build_fix_plan([], (), _connector(facade), "app").charset_plan is None

    def test_requires_connection(self):
        with pytest.raises(RuntimeError):
            create_wizard_steps([], SimpleNamespace(connection=None), "app")


class TestCharsetFixSql:
    def test_calls_core_with_sorted_tables_and_default_target(self):
        facade = _FakeFacade()
        parts = charset_fix_sql(_connector(facade), "app", {"b", "a"})
        _, endpoint, tables, charset, collation = facade.calls[0]
        assert (endpoint.database, tables, charset, collation) == ("app", ["a", "b"], "utf8mb4", "utf8mb4_unicode_ci")
        assert parts["table_count"] == 2

    def test_empty_tables_skip_core(self):
        facade = _FakeFacade()
        parts = charset_fix_sql(_connector(facade), "app", set())
        assert parts["full_sql"] == ["-- 변경할 테이블이 없습니다."] and facade.calls == []


class TestBatchFixExecutor:
    def test_mutation_mode_is_rejected(self):
        with pytest.raises(RuntimeError, match="disabled"):
            BatchFixExecutor().execute_batch([], dry_run=False)

    def test_dry_run_summary(self):
        logs = []
        executor = BatchFixExecutor()
        executor.set_progress_callback(logs.append)
        steps = [
            _step(_option(FixStrategy.SKIP)),
            _step(_option(FixStrategy.MANUAL, "-- 수동 처리 필요")),
            _step(_option(FixStrategy.DATE_TO_NULL, "UPDATE t SET c = NULL;", estimated_rows=42)),
            _step(_option(FixStrategy.MANUAL, "ALTER TABLE t ENGINE=InnoDB;")),
            _step(_option(FixStrategy.DATE_TO_CUSTOM, "UPDATE t SET c = '{custom_date}';", requires_input=True),
                  user_input="2000-01-01"),
        ]

        result = executor.execute_batch(steps)

        assert isinstance(result, BatchExecutionResult)
        assert (result.total_steps, result.success_count, result.skip_count, result.fail_count) == (5, 3, 2, 0)
        assert [r.message for r in result.results[:2]] == ["건너뛰기", "수동 처리 필요"]
        assert result.results[2].affected_rows == 42 and "42" in result.results[2].message
        assert "DDL" in result.results[3].message
        assert result.results[4].sql_executed == "UPDATE t SET c = '2000-01-01';"
        assert result.total_affected_rows == 42
        assert result.summary().affected_rows == 42
        assert logs and "[DRY-RUN]" in logs[0]


class TestDataclasses:
    def test_fix_option_defaults(self):
        option = _option(FixStrategy.SKIP)
        assert option.sql_template is None and option.estimated_rows is None
        assert not option.requires_input

    def test_rendered_sql_is_empty_without_selection(self):
        step = _step(_option(FixStrategy.SKIP))
        assert replace(step, selected_option=None).rendered_sql() == ""
