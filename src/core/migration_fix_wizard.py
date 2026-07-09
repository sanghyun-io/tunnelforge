"""
마이그레이션 자동 수정 위저드 Core 로직 - Facade

MySQL 8.0 → 8.4 마이그레이션 시 검출된 호환성 이슈를 자동 수정하는 핵심 로직.
실제 구현은 도메인별 모듈로 분할되어 있으며, 이 파일은 기존 소비자(dialog/worker/
test)의 import 경로를 유지하기 위한 얇은 re-export facade다.

- SmartFixGenerator / create_wizard_steps → migration_fix_option_generator
- CollationFKGraphBuilder / build_fk_graph → migration_fk_graph
- FKSafeCharsetChanger → migration_fk_safe_charset
- BatchFixExecutor → migration_batch_fix_executor
- CharsetFixPlanBuilder → migration_charset_fix_plan
- 데이터 모델(Enum/dataclass) → migration_fix_models
- RollbackSQLGenerator → migration_rollback_sql_generator
"""

from typing import List, Tuple

from src.core.migration_fix_models import (
    FixStrategy,
    FKDefinition,
    FixOption,
    FixWizardStep,
    FixExecutionResult,
    ExecutionSummary,
    BatchExecutionResult,
    CharsetTableInfo,
)
from src.core.migration_fk_graph import CollationFKGraphBuilder
from src.core.migration_fk_safe_charset import FKSafeCharsetChanger
from src.core.migration_fix_option_generator import (
    SmartFixGenerator,
    create_wizard_steps,
)
from src.core.migration_batch_fix_executor import BatchFixExecutor
from src.core.migration_charset_fix_plan import CharsetFixPlanBuilder
from src.core.migration_rollback_sql_generator import RollbackSQLGenerator


def render_all_steps_sql(steps: List[FixWizardStep]) -> List[Tuple[FixWizardStep, str]]:
    """선택된 수정 단계의 렌더링된 SQL을 중복 제거해 반환한다."""
    rendered = []
    processed_sql: set[str] = set()

    for step in steps:
        if not step.selected_option or step.selected_option.strategy == FixStrategy.SKIP:
            continue

        sql = step.rendered_sql()
        if sql in processed_sql:
            continue

        processed_sql.add(sql)
        rendered.append((step, sql))

    return rendered


__all__ = [
    "FixStrategy",
    "FKDefinition",
    "FixOption",
    "FixWizardStep",
    "FixExecutionResult",
    "ExecutionSummary",
    "BatchExecutionResult",
    "CharsetTableInfo",
    "RollbackSQLGenerator",
    "SmartFixGenerator",
    "CollationFKGraphBuilder",
    "FKSafeCharsetChanger",
    "BatchFixExecutor",
    "CharsetFixPlanBuilder",
    "create_wizard_steps",
    "render_all_steps_sql",
]
