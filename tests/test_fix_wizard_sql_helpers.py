from src.core.migration_constants import IssueType
from src.core.migration_fix_models import (
    BatchExecutionResult,
    FixExecutionResult,
    FixOption,
    FixStrategy,
    FixWizardStep,
)
from src.core.migration_fix_wizard import render_all_steps_sql
from src.ui.workers.fix_wizard_worker import CombinedExecutionResult


def _step(
    index,
    location,
    option,
    user_input=None,
    issue_type=IssueType.INVALID_DATE,
):
    return FixWizardStep(
        issue_index=index,
        issue_type=issue_type,
        location=location,
        description="test",
        options=[option],
        selected_option=option,
        user_input=user_input,
    )


def test_fix_wizard_step_rendered_sql_applies_user_input_placeholders():
    option = FixOption(
        strategy=FixStrategy.DATE_TO_CUSTOM,
        label="custom date",
        description="custom",
        sql_template=(
            "UPDATE orders SET created_at = '{custom_date}', "
            "precision_note = '{precision}';"
        ),
        requires_input=True,
    )

    step = _step(0, "app.orders.created_at", option, user_input="2026-07-09")

    assert step.rendered_sql() == (
        "UPDATE orders SET created_at = '2026-07-09', "
        "precision_note = '2026-07-09';"
    )


def test_fix_wizard_step_rendered_sql_returns_empty_without_selected_option():
    step = FixWizardStep(
        issue_index=0,
        issue_type=IssueType.INVALID_DATE,
        location="app.orders.created_at",
        description="test",
        options=[],
    )

    assert step.rendered_sql() == ""


def test_render_all_steps_sql_skips_skip_strategy_and_deduplicates_by_sql_text():
    duplicate_a = FixOption(
        strategy=FixStrategy.MANUAL,
        label="manual a",
        description="manual",
        sql_template="ALTER TABLE orders ENGINE=InnoDB;",
    )
    duplicate_b = FixOption(
        strategy=FixStrategy.MANUAL,
        label="manual b",
        description="manual",
        sql_template="ALTER TABLE orders ENGINE=InnoDB;",
    )
    unique = FixOption(
        strategy=FixStrategy.MANUAL,
        label="manual c",
        description="manual",
        sql_template="ALTER TABLE users ENGINE=InnoDB;",
    )
    skip = FixOption(
        strategy=FixStrategy.SKIP,
        label="skip",
        description="skip",
        sql_template="ALTER TABLE skipped ENGINE=InnoDB;",
    )

    steps = [
        _step(0, "app.orders", duplicate_a),
        _step(1, "app.orders_copy", duplicate_b),
        _step(2, "app.users", unique),
        _step(3, "app.skipped", skip),
    ]

    rendered = render_all_steps_sql(steps)

    assert rendered == [
        (steps[0], "ALTER TABLE orders ENGINE=InnoDB;"),
        (steps[2], "ALTER TABLE users ENGINE=InnoDB;"),
    ]


def test_batch_execution_result_summary_exposes_common_counts():
    result = BatchExecutionResult(
        total_steps=4,
        success_count=2,
        fail_count=1,
        skip_count=1,
        results=[
            FixExecutionResult(True, "ok", "SELECT 1;", affected_rows=7),
        ],
        total_affected_rows=7,
    )

    summary = result.summary()

    assert summary.total == 4
    assert summary.success == 2
    assert summary.fail == 1
    assert summary.skip == 1
    assert summary.affected_rows == 7


def test_combined_execution_result_summary_includes_charset_and_other_counts():
    other = BatchExecutionResult(
        total_steps=3,
        success_count=2,
        fail_count=0,
        skip_count=1,
        results=[],
        total_affected_rows=5,
    )
    result = CombinedExecutionResult(
        charset_success=True,
        charset_tables_count=2,
        charset_fk_count=1,
        other_result=other,
    )

    summary = result.summary()

    assert summary.total == 5
    assert summary.success == 3
    assert summary.fail == 0
    assert summary.skip == 1
    assert summary.affected_rows == 7
