"""
마이그레이션 자동 수정 위저드 - 배치 dry-run 추정기

선택된 수정 SQL에 대해 dry-run 영향 행 추정 및 미리보기만 수행한다.
실제 DDL/DML mutation은 Rust Core가 소유하므로 이 모듈에는 없다.
"""
import logging
from typing import List, Dict, Set, Optional, Callable, Tuple
from collections import defaultdict

from src.core.db_connector import MySQLConnector
from src.core.migration_constants import IssueType
from src.core.migration_fix_models import (
    FixStrategy,
    FixWizardStep,
    FixExecutionResult,
    BatchExecutionResult,
    DEFAULT_TARGET_CHARSET,
    DEFAULT_TARGET_COLLATION,
)
from src.core.migration_fk_graph import CollationFKGraphBuilder, build_fk_graph
from src.core.migration_fk_safe_charset import FKSafeCharsetChanger

logger = logging.getLogger(__name__)

# execute_batch 집계가 skip 으로 분류하는 결과 메시지 (건너뛰기/수동 처리)
_RESULT_MSG_SKIP = "건너뛰기"
_RESULT_MSG_MANUAL = "수동 처리 필요"


class BatchFixExecutor:
    """배치 수정 dry-run 추정기

    선택된 수정 SQL에 대해 dry-run 영향 행 추정 및 미리보기만 수행한다.
    실제 DDL/DML 트랜잭션 실행은 Rust Core가 소유하며 이 클래스에는 없다
    (dry_run=False는 즉시 RuntimeError로 거부).

    개선사항:
    - FK 관계에 따른 dry-run 추정 순서 최적화 (위상 정렬)
    - COLLATION_FK_SAFE / COLLATION_SINGLE 배치 클러스터링으로 중복 추정 제거
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        self._progress_callback: Optional[Callable[[str], None]] = None
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 콜백 설정"""
        self._progress_callback = callback

    def _log(self, message: str):
        """진행 로그"""
        if self._progress_callback:
            self._progress_callback(message)

    def _get_fk_graph_builder(self) -> CollationFKGraphBuilder:
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = build_fk_graph(self.connector, self.schema)
        return self._fk_graph_builder

    def _has_charset_issues(self, steps: List[FixWizardStep]) -> bool:
        """문자셋 이슈가 포함되어 있는지 확인 (FK_CHECKS 비활성화 필요 여부)

        참고: COLLATION_FK_SAFE 전략은 자체적으로 FK를 관리하므로 제외
        """
        return any(
            step.issue_type == IssueType.CHARSET_ISSUE
            and step.selected_option
            and step.selected_option.strategy not in (
                FixStrategy.SKIP,
                FixStrategy.COLLATION_FK_SAFE  # FK 안전 변경은 자체 FK 관리
            )
            for step in steps
        )

    def _sort_steps_by_fk_order(self, steps: List[FixWizardStep]) -> List[FixWizardStep]:
        """FK 관계에 따라 실행 순서 정렬 (부모 테이블 먼저)

        위상 정렬을 사용하여 FK 참조 순서에 맞게 정렬합니다.
        부모 테이블이 먼저 변경되어야 자식 테이블 변경 시 FK 충돌이 줄어듭니다.
        """
        # 문자셋 이슈만 정렬 대상
        charset_steps = [s for s in steps if s.issue_type == IssueType.CHARSET_ISSUE]
        other_steps = [s for s in steps if s.issue_type != IssueType.CHARSET_ISSUE]

        if not charset_steps:
            return steps

        try:
            fk_builder = self._get_fk_graph_builder()

            # 테이블명 추출 (location 형식: "schema.table" 또는 "schema.table.column")
            # 컬럼 레벨 스텝(schema.table.column)의 경우 split('.')[-1]이 column명이므로
            # parts[1]을 사용해야 올바른 table명을 얻을 수 있음
            table_to_steps: Dict[str, List[FixWizardStep]] = {}
            for step in charset_steps:
                parts = step.location.split('.')
                table_name = parts[1] if len(parts) >= 2 else parts[0]
                if table_name not in table_to_steps:
                    table_to_steps[table_name] = []
                table_to_steps[table_name].append(step)

            # 위상 정렬
            all_tables = set(table_to_steps.keys())
            sorted_tables = fk_builder.get_topological_order(all_tables)

            # 정렬된 순서로 steps 재배치 (같은 테이블의 여러 스텝 모두 포함)
            sorted_charset_steps = []
            for table in sorted_tables:
                if table in table_to_steps:
                    sorted_charset_steps.extend(table_to_steps[table])

            # 정렬되지 않은 테이블 추가 (FK 관계 없는 테이블)
            sorted_set = set(sorted_tables)
            for step in charset_steps:
                parts = step.location.split('.')
                table_name = parts[1] if len(parts) >= 2 else parts[0]
                if table_name not in sorted_set:
                    sorted_charset_steps.append(step)

            self._log(f"  📊 FK 관계에 따라 {len(sorted_charset_steps)}개 스텝 정렬 완료")

            return sorted_charset_steps + other_steps

        except Exception as e:
            # fallback 계약 유지(broad catch)하되 진단 로그는 남긴다
            logger.exception("FK 정렬 실패, 원본 순서 유지")
            self._log(f"  ⚠️ FK 정렬 실패, 원본 순서 유지: {e}")
            return steps

    def _execute_fk_safe_clusters(
        self, steps: List[FixWizardStep]
    ) -> Tuple[List[FixExecutionResult], Set[int]]:
        """COLLATION_FK_SAFE 스텝을 FK 클러스터별로 배치 추정한다.

        개별 스텝마다 FK DROP→ALTER→ADD를 반복하면 O(N²) DDL 발생.
        FK 클러스터별(related_tables 집합이 동일한 스텝끼리)로 그룹핑하여
        클러스터당 generate_safe_charset_sql을 1회만 호출한다.

        처리 여부는 step identity(id())로 추적한다. location 문자열로
        추적하면 같은 location에 다른 issue_type/strategy를 가진 별개
        step이 우연히 존재할 때 그 step의 선택된 fix가 조용히 누락되는
        버그가 있었다 (remaining-step 루프의 skip 조건 참조).

        Returns:
            (결과 목록, 처리된 step id 집합)
        """
        results: List[FixExecutionResult] = []
        processed: Set[int] = set()

        fk_safe_steps = [
            s for s in steps
            if s.selected_option and s.selected_option.strategy == FixStrategy.COLLATION_FK_SAFE
        ]
        if not fk_safe_steps:
            return results, processed

        # 스키마별 → 클러스터별 2단계 그룹핑
        schema_cluster: Dict[str, Dict[frozenset, List[FixWizardStep]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for s in fk_safe_steps:
            _schema = s.location.split('.')[0] if '.' in s.location else self.schema
            _cluster_key = frozenset(s.selected_option.related_tables or [])
            schema_cluster[_schema][_cluster_key].append(s)

        total_clusters = sum(len(v) for v in schema_cluster.values())
        self._log(
            f"  🔐 FK 안전 변경 배치 처리"
            f" ({len(fk_safe_steps)}개 스텝 → {total_clusters}개 클러스터)..."
        )

        for _schema, cluster_map in schema_cluster.items():
            for cluster_tables_frozen, cluster_steps in cluster_map.items():
                cluster_tables = set(cluster_tables_frozen)
                self._log(
                    f"    📦 클러스터 [{_schema}]: {len(cluster_tables)}개 테이블,"
                    f" {len(cluster_steps)}개 스텝"
                )
                fk_changer = FKSafeCharsetChanger(self.connector, _schema)

                sql_parts = fk_changer.generate_safe_charset_sql(
                    cluster_tables, DEFAULT_TARGET_CHARSET, DEFAULT_TARGET_COLLATION
                )
                fk_msg = (
                    f"DRY-RUN: {sql_parts['fk_count']}개 FK,"
                    f" {sql_parts['table_count']}개 테이블 변경 예정"
                )

                for s in cluster_steps:
                    processed.add(id(s))
                    results.append(FixExecutionResult(
                        success=True,
                        message=f"{fk_msg} (배치)",
                        sql_executed=s.selected_option.sql_template or "",
                        affected_rows=1,
                        location=s.location,
                        description=s.description
                    ))

                self._log(f"    ✅ 클러스터 완료 ({len(cluster_tables)}개 테이블)")

        return results, processed

    def _execute_collation_single_merges(
        self, steps: List[FixWizardStep]
    ) -> Tuple[List[FixExecutionResult], Set[int]]:
        """COLLATION_SINGLE 컬럼별 스텝을 테이블별 1개 DDL로 병합 추정한다.

        같은 테이블의 여러 컬럼(modify_clause 구조화 필드 보유)을 하나의
        ALTER TABLE ... MODIFY COLUMN 문으로 병합한다.

        Returns:
            (결과 목록, 병합 처리된 step id 집합)
        """
        results: List[FixExecutionResult] = []
        merged: Set[int] = set()

        single_col_steps = [
            s for s in steps
            if (s.selected_option
                and s.selected_option.strategy == FixStrategy.COLLATION_SINGLE
                and s.selected_option.modify_clause  # 구조화 필드 존재
                and len(s.location.split('.')) > 2)  # column-level
        ]
        if not single_col_steps:
            return results, merged

        table_groups: Dict[tuple, List[FixWizardStep]] = defaultdict(list)
        for s in single_col_steps:
            parts = s.location.split('.')
            table_groups[(parts[0], parts[1])].append(s)

        for (schema_name, table_name), group_steps in table_groups.items():
            if len(group_steps) < 2:
                continue

            # modify_clause 필드에서 직접 병합 (regex 파싱 불필요)
            clauses = [
                f"MODIFY COLUMN {s.selected_option.modify_clause}"
                for s in group_steps
                if s.selected_option and s.selected_option.modify_clause
            ]
            if len(clauses) < 2:
                continue

            merged_sql = (
                f"ALTER TABLE `{schema_name}`.`{table_name}`\n  "
                + ",\n  ".join(clauses) + ";"
            )

            self._log(
                f"  📦 COLLATION_SINGLE 병합: `{table_name}` "
                f"({len(clauses)}개 컬럼 → 1개 DDL)"
            )

            merge_result = self._estimate_affected_rows(merged_sql, group_steps[0])

            # 그룹 내 모든 스텝 결과 기록 (2-phase bookkeeping: results 확정 후 merged 갱신)
            pending: Set[int] = set()
            for idx, s in enumerate(group_steps):
                results.append(FixExecutionResult(
                    success=merge_result.success,
                    message=merge_result.message + f" (병합: {len(clauses)}컬럼)",
                    sql_executed=(
                        merged_sql if idx == 0
                        else f"-- 병합됨 ({table_name})"
                    ),
                    affected_rows=(
                        merge_result.affected_rows if idx == 0 else 0
                    ),
                    location=s.location,
                    description=s.description
                ))
                pending.add(id(s))
            merged.update(pending)

            if merge_result.success:
                self._log(f"    ✅ {table_name} 병합 완료 ({len(clauses)}컬럼)")

        return results, merged

    def _execute_remaining_steps(
        self, steps: List[FixWizardStep], already_handled_ids: Set[int]
    ) -> List[FixExecutionResult]:
        """배치 처리되지 않은 나머지 스텝을 개별 dry-run 추정한다.

        FK 안전 배치/COLLATION_SINGLE 병합으로 이미 처리된 step(id 기준)은
        건너뛴다. SKIP/수동 처리/개별 UPDATE·DDL 추정을 담당한다.
        """
        results: List[FixExecutionResult] = []
        mode = "[DRY-RUN]"  # dry_run=False는 execute_batch 진입 시점에 이미 거부됨

        for i, step in enumerate(steps, 1):
            # 배치로 이미 처리된 스텝 건너뛰기 (step identity 기준)
            if id(step) in already_handled_ids:
                continue

            # 건너뛰기 옵션 확인
            if step.selected_option and step.selected_option.strategy == FixStrategy.SKIP:
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 건너뛰기")
                results.append(FixExecutionResult(
                    success=True,
                    message=_RESULT_MSG_SKIP,
                    sql_executed="",
                    affected_rows=0,
                    location=step.location,
                    description=step.description
                ))
                continue

            # SQL 생성
            sql = step.selected_option.sql_template if step.selected_option else ""
            if not sql or sql.startswith("--"):
                # 수동 처리 사유: 선택된 옵션의 description 또는 step.description 사용
                skip_desc = ""
                if step.selected_option:
                    skip_desc = step.selected_option.description
                if not skip_desc:
                    skip_desc = step.description
                self._log(f"  [{i}/{len(steps)}] ⏭️ {step.location} - 수동 처리 필요: {skip_desc}")
                results.append(FixExecutionResult(
                    success=True,
                    message=_RESULT_MSG_MANUAL,
                    sql_executed=sql,
                    affected_rows=0,
                    location=step.location,
                    description=skip_desc
                ))
                continue

            # 사용자 입력 대체
            if step.selected_option and step.selected_option.requires_input and step.user_input:
                sql = sql.replace("{custom_date}", step.user_input)
                sql = sql.replace("{precision}", step.user_input)

            self._log(f"  [{i}/{len(steps)}] {mode} {step.location}...")

            # Dry-run: COUNT 쿼리로 영향 행 추정
            result = self._estimate_affected_rows(sql, step)

            # FK 정렬 후 step↔result 매핑 오류 방지: location을 result에 직접 저장
            result.location = step.location
            results.append(result)

            if result.success:
                if result.affected_rows > 0:
                    self._log(f"    ✅ {result.message} ({result.affected_rows}행)")
                else:
                    self._log(f"    ✅ {result.message}")
            else:
                self._log(f"    ❌ {result.message}")

        return results

    def execute_batch(
        self,
        steps: List[FixWizardStep],
        dry_run: bool = True
    ) -> BatchExecutionResult:
        """배치 실행

        Args:
            steps: 실행할 위저드 단계 목록
            dry_run: True면 실제 실행하지 않고 영향 행 추정

        Returns:
            BatchExecutionResult

        dry-run-only 계약: dry_run=False는 즉시 거부되므로 이 메서드는 실제
        DDL/DML을 실행하지 않는다. 세션 상태(sql_mode 등) 변경·복원, 실행 전
        상태 캡처, rollback SQL 자동 생성은 실제 mutation이 있을 때만 의미가
        있던 기능이며 Rust Core가 담당하므로 이 클래스에는 존재하지 않는다.

        개선사항:
        - 문자셋 이슈 포함 시 FK 관계에 따른 실행 순서 최적화
        """
        if not dry_run:
            raise RuntimeError(
                "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        self._log(f"🔧 [DRY-RUN] 배치 수정 시작 ({len(steps)}개)")

        # FK 관계에 따른 실행 순서 정렬
        if self._has_charset_issues(steps):
            steps = self._sort_steps_by_fk_order(steps)

        # 3단계 순차 처리: FK 안전 배치 → COLLATION_SINGLE 병합 → 나머지 개별
        fk_results, fk_ids = self._execute_fk_safe_clusters(steps)
        merge_results, merge_ids = self._execute_collation_single_merges(steps)
        remaining_results = self._execute_remaining_steps(steps, fk_ids | merge_ids)

        results = fk_results + merge_results + remaining_results

        # 집계: skip(건너뛰기/수동) vs success vs fail 분류
        fail_count = sum(1 for r in results if not r.success)
        skip_count = sum(
            1 for r in results
            if r.success and r.message in (_RESULT_MSG_SKIP, _RESULT_MSG_MANUAL)
        )
        success_count = sum(1 for r in results if r.success) - skip_count
        total_affected = sum(r.affected_rows for r in results)

        return BatchExecutionResult(
            total_steps=len(steps),
            success_count=success_count,
            fail_count=fail_count,
            skip_count=skip_count,
            results=results,
            total_affected_rows=total_affected,
            rollback_sql=""
        )

    def _execute_single(self, sql: str) -> FixExecutionResult:
        """단일 SQL 실행 (defense-in-depth: execute_batch 가드를 우회해 직접
        호출되더라도 fail-closed로 DB mutation을 거부한다)"""
        raise RuntimeError(
            "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
            "DB mutations must be owned by Rust Core."
        )

    def _estimate_affected_rows(self, sql: str, step: FixWizardStep) -> FixExecutionResult:
        """영향 행 추정 (Dry-run용)

        UPDATE/DELETE 문을 COUNT 쿼리로 변환
        """
        try:
            sql_upper = sql.upper()

            # UPDATE 문 처리
            if 'UPDATE' in sql_upper and 'WHERE' in sql_upper:
                # UPDATE table SET ... WHERE condition → SELECT COUNT(*) FROM table WHERE condition
                # 간단한 파싱
                where_idx = sql.upper().find('WHERE')
                from_idx = sql.upper().find('UPDATE') + 6
                set_idx = sql.upper().find('SET')

                table_part = sql[from_idx:set_idx].strip()
                where_clause = sql[where_idx:]

                count_sql = f"SELECT COUNT(*) as cnt FROM {table_part} {where_clause}"
                # 세미콜론 제거
                count_sql = count_sql.rstrip(';')

                # 0000-00-00 날짜값이 있을 경우 strict mode에서 COUNT 쿼리가 실패하므로
                # 임시로 sql_mode를 완화한 뒤 실행하고 복원한다
                _saved_mode: Optional[str] = None
                try:
                    _saved_mode = self.connector.get_session_sql_mode()
                    self.connector.set_session_sql_mode('')
                except Exception:
                    # 모드 조회/설정 실패 시 현재 모드로 시도 (fallback 계약 유지)
                    logger.debug(
                        "sql_mode 조회/완화 실패, 현재 세션 모드로 COUNT 추정 진행",
                        exc_info=True
                    )

                try:
                    result = self.connector.execute(count_sql)
                    affected = result[0]['cnt'] if result else 0
                    count_ok = True
                except Exception:
                    affected = 0
                    count_ok = False
                finally:
                    if _saved_mode is not None:
                        try:
                            self.connector.set_session_sql_mode(_saved_mode)
                        except Exception:
                            # 복원 실패 시 세션 모드가 완화된 채 남아 후속 dry-run에
                            # 영향 가능 → 반드시 경고 로그로 감지 가능하게 한다
                            logger.warning(
                                "sql_mode 복원 실패 — 세션 모드가 완화된 채로 남아"
                                " 후속 dry-run 추정에 영향 가능",
                                exc_info=True
                            )

                if not count_ok:
                    return FixExecutionResult(
                        success=True,
                        message="[DRY-RUN] 예상 영향 행: ≥1 (0000-00-00 등 비표준 값 포함으로 정확한 수 불명)",
                        sql_executed=sql,
                        affected_rows=1
                    )

                return FixExecutionResult(
                    success=True,
                    message=f"[DRY-RUN] 예상 영향 행: {affected:,}",
                    sql_executed=sql,
                    affected_rows=affected
                )

            # ALTER TABLE 등 DDL은 영향 행 추정 불가
            elif 'ALTER' in sql_upper:
                return FixExecutionResult(
                    success=True,
                    message="[DRY-RUN] DDL 문 - 영향 행 추정 불가",
                    sql_executed=sql,
                    affected_rows=0
                )

            else:
                return FixExecutionResult(
                    success=True,
                    message="[DRY-RUN] 분석 완료",
                    sql_executed=sql,
                    affected_rows=0
                )

        except Exception as e:
            return FixExecutionResult(
                success=False,
                message=f"[DRY-RUN] 분석 오류: {str(e)}",
                sql_executed=sql,
                error=str(e)
            )
