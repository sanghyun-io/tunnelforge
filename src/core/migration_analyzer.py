"""
MySQL 마이그레이션(8.0 → 8.4) 분석기

분석(고아 레코드 + 호환성 검사 14종)과 정리 SQL 생성은 Rust core 의 `upgrade.analyze` 가 한다.
이 모듈은 그 결과를 UI 가 쓰는 AnalysisResult 로 바꾸고, 정리 작업의 dry-run 건수만 센다.

데이터클래스와 덤프 파일 분석기는 하위호환을 위해 이 모듈에서 re-export 한다.
"""
import dataclasses
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from src.core.migration_constants import IssueType, CompatibilityIssue
from src.core.migration_analysis_models import (
    ActionType,
    OrphanRecord,
    ForeignKeyInfo,
    CleanupAction,
    AnalysisResult,
    SchemaCheckOptions,
)
from src.core.migration_dump_analyzer import DumpAnalysisResult, DumpFileAnalyzer

__all__ = [
    'MigrationAnalyzer',
    'AnalysisResult',
    'OrphanRecord',
    'CleanupAction',
    'ActionType',
    'ForeignKeyInfo',
    'SchemaCheckOptions',
    'CompatibilityIssue',
    'IssueType',
    'DumpFileAnalyzer',
    'DumpAnalysisResult',
    'analysis_result_from_core',
]

MISSING_CLEANUP_SQL = "-- 이전 버전에서 저장한 분석 결과에는 정리 SQL이 없습니다. 다시 분석하세요."


def _issue_from_core(data: Dict) -> CompatibilityIssue:
    return CompatibilityIssue(
        issue_type=IssueType(data["issue_type"]),
        severity=str(data.get("severity", "")),
        location=str(data.get("location", "")),
        description=str(data.get("description", "")),
        suggestion=str(data.get("suggestion", "")),
        fix_query=data.get("fix_query"),
        table_name=data.get("table_name"),
        column_name=data.get("column_name"),
    )


def _orphan_from_core(data: Dict) -> OrphanRecord:
    return OrphanRecord(
        child_table=str(data.get("child_table", "")),
        child_column=str(data.get("child_column", "")),
        parent_table=str(data.get("parent_table", "")),
        parent_column=str(data.get("parent_column", "")),
        orphan_count=int(data.get("orphan_count", 0) or 0),
        sample_values=list(data.get("sample_values") or []),
        cleanup_sql=dict(data.get("cleanup_sql") or {}),
    )


def analysis_result_from_core(result: Dict, schema: str) -> AnalysisResult:
    """Rust `upgrade.analyze` 결과 → AnalysisResult (고아 레코드마다 기본 DELETE 정리 작업 포함)."""
    analysis = AnalysisResult(
        schema=schema,
        analyzed_at=datetime.now().isoformat(),
        total_tables=int(result.get("total_tables", 0) or 0),
        total_fk_relations=int(result.get("total_fk_relations", 0) or 0),
        orphan_records=[_orphan_from_core(o) for o in result.get("orphan_records") or []],
        compatibility_issues=[_issue_from_core(i) for i in result.get("issues") or []],
        fk_tree={str(k): [str(c) for c in v] for k, v in (result.get("fk_tree") or {}).items()},
    )
    analysis.cleanup_actions = [
        build_cleanup_action(orphan, ActionType.DELETE, schema) for orphan in analysis.orphan_records
    ]
    return analysis


def build_cleanup_action(orphan: OrphanRecord, action: ActionType, schema: str, dry_run: bool = True) -> CleanupAction:
    """고아 레코드 정리 작업. SQL 은 분석 시 Rust core 가 만든 것을 그대로 쓴다."""
    sql_by_action = orphan.cleanup_sql or {}
    if action == ActionType.DELETE:
        sql = sql_by_action.get("delete") or MISSING_CLEANUP_SQL
        description = f"{orphan.child_table}에서 고아 레코드 {orphan.orphan_count}개 삭제"
    elif action == ActionType.SET_NULL:
        sql = sql_by_action.get("set_null") or MISSING_CLEANUP_SQL
        description = f"{orphan.child_table}.{orphan.child_column}을 NULL로 설정 ({orphan.orphan_count}개)"
    else:
        sql = f"-- 수동 처리 필요: {orphan.child_table}.{orphan.child_column}"
        description = f"{orphan.child_table} 수동 검토 필요"
    return CleanupAction(
        action_type=action,
        table=orphan.child_table,
        description=description,
        sql=sql,
        affected_rows=orphan.orphan_count,
        dry_run=dry_run,
        target_schema=schema,
        target_table=orphan.child_table,
        count_sql=sql_by_action.get("count") if action != ActionType.MANUAL else None,
    )


class MigrationAnalyzer:
    """업그레이드 호환성 분석기 (Rust core upgrade.analyze 위임)"""

    def __init__(self, connector):
        self.connector = connector
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        if self._progress_callback:
            self._progress_callback(message)

    def _connection(self):
        connection = getattr(self.connector, "connection", None)
        if connection is None:
            raise RuntimeError("DB에 연결되어 있지 않습니다.")
        return connection

    def analyze_schema(self, schema: str, **check_options: bool) -> AnalysisResult:
        """스키마 전체 분석. check_* 키워드는 SchemaCheckOptions 의 필드 (기본값 모두 True)."""
        valid = {field.name for field in dataclasses.fields(SchemaCheckOptions)}
        unexpected = sorted(set(check_options) - valid)
        if unexpected:
            raise TypeError(f"Unexpected check option(s): {', '.join(unexpected)}")
        connection = self._connection()
        endpoint = dataclasses.replace(connection.endpoint, database=schema)

        def on_event(event: Dict):
            if event.get("event") == "progress" and event.get("message"):
                self._log(str(event["message"]))

        result = connection.facade.analyze_upgrade(endpoint, dict(check_options), on_event=on_event)
        return analysis_result_from_core(result, schema)

    def generate_cleanup_sql(
        self,
        orphan: OrphanRecord,
        action: ActionType,
        schema: str,
        dry_run: bool = True
    ) -> CleanupAction:
        """고아 레코드 정리 SQL (분석 시 만들어 둔 SQL 사용)"""
        return build_cleanup_action(orphan, action, schema, dry_run)

    def execute_cleanup(self, action: CleanupAction, dry_run: bool = True) -> Tuple[bool, str, int]:
        """정리 작업의 dry-run 영향 분석 (실제 실행은 지원하지 않는다)"""
        if not dry_run:
            raise RuntimeError(
                "Legacy Python cleanup mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )
        self._log(f"🔍 [DRY-RUN] 영향 분석: {action.table}")
        if action.action_type == ActionType.MANUAL:
            return True, "수동 처리 필요", 0
        if not action.count_sql:
            return False, "❌ 정리 대상 메타데이터 없음 (다시 분석하세요)", 0
        rows = self.connector.execute(action.count_sql)
        affected = int(rows[0]["cnt"]) if rows else 0
        return True, f"[DRY-RUN] {affected}개 행이 영향받음", affected
