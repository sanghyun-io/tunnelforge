"""
마이그레이션 자동 수정 위저드

수정 옵션(SQL 포함)과 문자셋 수정 계획은 Rust core 가 만든다:
- upgrade.fix_plan  : 이슈별 옵션(날짜 수정의 예상 영향 행 수 포함) + 문자셋 대상 테이블/연쇄 건너뛰기/FK
- upgrade.charset_sql: 고른 테이블의 FK 안전 변환 SQL (FK DROP → CONVERT → FK ADD)
이 모듈은 결과를 위저드 모델로 바꾸고, dry-run 요약을 메모리에서 계산한다.
실제 DB 변경은 지원하지 않는다 (dry_run=False 는 항상 거부).
"""
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from src.core.migration_constants import IssueType
from src.core.migration_fix_models import (
    DEFAULT_TARGET_CHARSET,
    DEFAULT_TARGET_COLLATION,
    BatchExecutionResult,
    CharsetTableInfo,
    ExecutionSummary,
    FixExecutionResult,
    FixOption,
    FixStrategy,
    FixWizardStep,
    ForeignKeyRef,
)

_RESULT_MSG_SKIP = "건너뛰기"
_RESULT_MSG_MANUAL = "수동 처리 필요"
_MUTATION_DISABLED = (
    "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
    "DB mutations must be owned by Rust Core."
)


def _core(connector, schema: str):
    """연결된 connector 의 Rust facade 와 (database=schema) endpoint"""
    connection = getattr(connector, "connection", None)
    if connection is None:
        raise RuntimeError("DB에 연결되어 있지 않습니다.")
    return connection.facade, dataclasses.replace(connection.endpoint, database=schema)


def _issue_payload(issue: Any) -> Dict[str, Any]:
    issue_type = issue.issue_type.value if isinstance(issue.issue_type, IssueType) else str(issue.issue_type)
    return {
        "issue_type": issue_type,
        "location": issue.location,
        "table_name": getattr(issue, "table_name", None),
        "column_name": getattr(issue, "column_name", None),
        "description": issue.description,
    }


def _option(data: Dict[str, Any]) -> FixOption:
    return FixOption(
        strategy=FixStrategy(data["strategy"]),
        label=str(data.get("label", "")),
        description=str(data.get("description", "")),
        sql_template=data.get("sql_template"),
        requires_input=bool(data.get("requires_input", False)),
        input_label=data.get("input_label"),
        input_default=data.get("input_default"),
        is_recommended=bool(data.get("is_recommended", False)),
        estimated_rows=data.get("estimated_rows"),
    )


@dataclass
class CharsetFixPlan:
    """문자셋 수정 계획 (원본 이슈 테이블 + FK 연관 테이블, 부모 먼저)"""
    tables: List[CharsetTableInfo]
    cascade_skip: Dict[str, List[str]]
    foreign_keys: List[ForeignKeyRef]
    connector: Any = field(repr=False, default=None)
    schema: str = ""

    def build_full_table_list(self) -> List[CharsetTableInfo]:
        return self.tables

    def get_cascade_skip_tables(self, table_to_skip: str) -> Set[str]:
        """table_to_skip 을 건너뛰면 FK 관계로 함께 건너뛰어야 하는 테이블"""
        return set(self.cascade_skip.get(table_to_skip, []))

    def generate_fix_sql(self, tables_to_fix: Iterable[str], charset: str = DEFAULT_TARGET_CHARSET,
                         collation: str = DEFAULT_TARGET_COLLATION) -> Dict[str, Any]:
        return charset_fix_sql(self.connector, self.schema, tables_to_fix, charset, collation)


@dataclass
class FixPlan:
    steps: List[FixWizardStep]
    charset_plan: Optional[CharsetFixPlan]


def build_fix_plan(issues: List[Any], charset_tables: Iterable[str], connector, schema: str) -> FixPlan:
    """이슈 → 위저드 단계, 문자셋 이슈 테이블 → 문자셋 계획 (Rust upgrade.fix_plan 한 번 호출)"""
    charset_tables = sorted(set(charset_tables))
    facade, endpoint = _core(connector, schema)
    result = facade.plan_upgrade_fixes(endpoint, [_issue_payload(i) for i in issues], charset_tables)
    steps = []
    for issue, step in zip(issues, result.get("steps") or []):
        steps.append(FixWizardStep(
            issue_index=int(step.get("issue_index", len(steps))),
            issue_type=issue.issue_type,
            location=issue.location,
            description=issue.description,
            options=[_option(o) for o in step.get("options") or []],
        ))
    charset_plan = None
    if charset_tables:
        charset_plan = CharsetFixPlan(
            tables=[
                CharsetTableInfo(
                    table_name=t["table_name"],
                    current_charset=t.get("current_charset", ""),
                    current_collation=t.get("current_collation", ""),
                    fk_parents=list(t.get("fk_parents") or []),
                    fk_children=list(t.get("fk_children") or []),
                    is_original_issue=bool(t.get("is_original_issue")),
                )
                for t in result.get("charset_tables") or []
            ],
            cascade_skip={k: list(v) for k, v in (result.get("cascade_skip") or {}).items()},
            foreign_keys=[
                ForeignKeyRef(fk["constraint_name"], fk["table_name"], fk["ref_table"])
                for fk in result.get("foreign_keys") or []
            ],
            connector=connector,
            schema=schema,
        )
    return FixPlan(steps=steps, charset_plan=charset_plan)


def create_wizard_steps(issues: List[Any], connector, schema: str) -> List[FixWizardStep]:
    """이슈 목록 → 위저드 단계"""
    return build_fix_plan(issues, (), connector, schema).steps


def charset_fix_sql(connector, schema: str, tables: Iterable[str], charset: str = DEFAULT_TARGET_CHARSET,
                    collation: str = DEFAULT_TARGET_COLLATION) -> Dict[str, Any]:
    """FK 안전 문자셋 변환 SQL: {'drop_fks', 'alter_tables', 'add_fks', 'full_sql', 'fk_count', 'table_count'}"""
    tables = sorted(set(tables))
    if not tables:
        return {"drop_fks": [], "alter_tables": [], "add_fks": [],
                "full_sql": ["-- 변경할 테이블이 없습니다."], "fk_count": 0, "table_count": 0}
    facade, endpoint = _core(connector, schema)
    return facade.charset_fix_sql(endpoint, tables, charset, collation)


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


class BatchFixExecutor:
    """선택된 수정의 dry-run 요약 (영향 행 수는 계획 시 Rust 가 센 값)"""

    def __init__(self, connector=None, schema: str = ""):
        self.connector = connector
        self.schema = schema
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        self._progress_callback = callback

    def _log(self, message: str):
        if self._progress_callback:
            self._progress_callback(message)

    def execute_batch(self, steps: List[FixWizardStep], dry_run: bool = True) -> BatchExecutionResult:
        if not dry_run:
            raise RuntimeError(_MUTATION_DISABLED)
        self._log(f"🔧 [DRY-RUN] 배치 수정 시작 ({len(steps)}개)")
        results = []
        for i, step in enumerate(steps, 1):
            option = step.selected_option
            if option and option.strategy == FixStrategy.SKIP:
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 건너뛰기")
                results.append(FixExecutionResult(True, _RESULT_MSG_SKIP, "", 0, location=step.location,
                                                  description=step.description))
                continue
            sql = step.rendered_sql()
            if not sql or sql.startswith("--"):
                reason = (option.description if option else "") or step.description
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 수동 처리 필요: {reason}")
                results.append(FixExecutionResult(True, _RESULT_MSG_MANUAL, sql, 0, location=step.location,
                                                  description=reason))
                continue
            self._log(f"  [{i}/{len(steps)}] [DRY-RUN] {step.location}...")
            if option is not None and option.estimated_rows is not None:
                rows = int(option.estimated_rows)
                message = f"[DRY-RUN] 예상 영향 행: {rows:,}"
            elif "ALTER" in sql.upper():
                rows, message = 0, "[DRY-RUN] DDL 문 - 영향 행 추정 불가"
            else:
                rows, message = 0, "[DRY-RUN] 분석 완료"
            results.append(FixExecutionResult(True, message, sql, rows, location=step.location,
                                              description=step.description))
            self._log(f"    ✅ {message} ({rows}행)" if rows > 0 else f"    ✅ {message}")

        fail_count = sum(1 for r in results if not r.success)
        skip_count = sum(1 for r in results if r.success and r.message in (_RESULT_MSG_SKIP, _RESULT_MSG_MANUAL))
        return BatchExecutionResult(
            total_steps=len(steps),
            success_count=sum(1 for r in results if r.success) - skip_count,
            fail_count=fail_count,
            skip_count=skip_count,
            results=results,
            total_affected_rows=sum(r.affected_rows for r in results),
        )


__all__ = [
    "FixStrategy",
    "FixOption",
    "FixWizardStep",
    "FixExecutionResult",
    "ExecutionSummary",
    "BatchExecutionResult",
    "CharsetTableInfo",
    "ForeignKeyRef",
    "CharsetFixPlan",
    "FixPlan",
    "BatchFixExecutor",
    "build_fix_plan",
    "create_wizard_steps",
    "charset_fix_sql",
    "render_all_steps_sql",
]
