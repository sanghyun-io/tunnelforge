"""
고아 레코드 정리 계획기

고아 레코드에 대한 정리 SQL(DELETE/SET_NULL/MANUAL)을 생성하고,
dry-run 영향 분석을 수행한다. 실제 DB 변경은 Rust Core 소유이므로
dry_run=False 실행은 항상 거부된다.
"""
from typing import Tuple

from src.core.migration_analysis_models import ActionType, CleanupAction, OrphanRecord


class OrphanCleanupPlanner:
    """고아 레코드 정리 SQL 생성 및 dry-run 영향 분석"""

    def __init__(self, connector, log):
        self.connector = connector
        # 파사드가 공유하는 _log 를 주입받아 진행 상황을 동일 콜백으로 전달한다.
        self._log = log

    def generate_cleanup_sql(
        self,
        orphan: OrphanRecord,
        action: ActionType,
        schema: str,
        dry_run: bool = True
    ) -> CleanupAction:
        """고아 레코드 정리 SQL 생성

        NOT IN 대신 NOT EXISTS를 사용한다. 부모 테이블의 참조 컬럼에 NULL이
        하나라도 있으면 `col NOT IN (SELECT ... )`의 서브쿼리 결과에 NULL이
        섞여 비교 결과가 전부 UNKNOWN이 되어, 실제로는 고아 레코드가 있어도
        0건으로 처리되는 NULL-안전성 문제가 있다. find_orphan_records()의
        대용량 테이블 경로와 동일하게 NOT EXISTS로 통일한다.
        """
        if action == ActionType.DELETE:
            sql = f"""DELETE c FROM `{schema}`.`{orphan.child_table}` AS c
WHERE c.`{orphan.child_column}` IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM `{schema}`.`{orphan.parent_table}` AS p
        WHERE p.`{orphan.parent_column}` = c.`{orphan.child_column}`
    )"""
            description = f"{orphan.child_table}에서 고아 레코드 {orphan.orphan_count}개 삭제"

        elif action == ActionType.SET_NULL:
            sql = f"""UPDATE `{schema}`.`{orphan.child_table}` AS c
SET c.`{orphan.child_column}` = NULL
WHERE c.`{orphan.child_column}` IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM `{schema}`.`{orphan.parent_table}` AS p
        WHERE p.`{orphan.parent_column}` = c.`{orphan.child_column}`
    )"""
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
            target_table=orphan.child_table
        )

    def execute_cleanup(
        self,
        action: CleanupAction,
        dry_run: bool = True
    ) -> Tuple[bool, str, int]:
        """
        정리 작업 실행 (dry-run 영향 분석 전용)

        Args:
            action: 실행할 정리 작업
            dry_run: True면 실제 실행하지 않고 영향받는 행 수만 반환

        Returns:
            (성공여부, 메시지, 영향받은 행 수)

        주의:
            dry_run=False는 항상 RuntimeError를 던진다. 실제 DB 변경(mutation)은
            Rust Core 소유이며 레거시 Python 실행 경로는 fail-closed로 비활성화돼 있다.
        """
        if not dry_run:
            raise RuntimeError(
                "Legacy Python cleanup mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        # dry-run: 실제 실행하지 않고 영향받는 행 수 확인
        self._log(f"🔍 [DRY-RUN] 영향 분석: {action.table}")

        if action.action_type == ActionType.MANUAL:
            return True, "수동 처리 필요", 0

        if not action.target_schema or not action.target_table:
            # sql 텍스트를 split('FROM')/split('UPDATE') 등으로 재파싱해
            # 테이블명을 추측하지 않는다. 테이블명이 SETTINGS/ASSETS처럼
            # FROM/SET 같은 키워드를 포함하면 잘못 잘려나가는 문제가 있었다.
            # 메타데이터가 없으면(예: 구버전 직렬화 복원) 추측 대신 명시적으로 실패시킨다.
            return False, "❌ 정리 대상 메타데이터 없음", 0

        # COUNT 쿼리로 변환하여 영향받는 행 수 확인
        # DELETE/UPDATE의 WHERE 절만 추출하고, 테이블은 생성 시점에
        # CleanupAction에 저장해둔 target_schema/target_table을 그대로 사용한다.
        sql_upper = action.sql.upper()
        if 'WHERE' not in sql_upper:
            return True, "[DRY-RUN] 영향 분석 완료", action.affected_rows

        where_idx = action.sql.upper().find('WHERE')
        where_clause = action.sql[where_idx:]

        count_sql = (
            f"SELECT COUNT(*) as cnt FROM `{action.target_schema}`.`{action.target_table}` AS c "
            f"{where_clause}"
        )
        result = self.connector.execute(count_sql)
        affected = result[0]['cnt'] if result else 0

        return True, f"[DRY-RUN] {affected}개 행이 영향받음", affected
