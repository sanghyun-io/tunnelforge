"""
FK 관계 분석기

FK 관계 조회/트리 구성/시각화 및 고아 레코드(orphan rows) 탐지를 담당한다.
데이터클래스는 migration_analysis_models 에서만 import 한다 (순환 import 방지).
"""
import time
from typing import List, Dict, Callable, Optional

from src.core.migration_analysis_models import OrphanRecord, ForeignKeyInfo

# 고아 레코드 탐지 임계값 (인라인 매직넘버 대체)
LARGE_TABLE_ROW_THRESHOLD = 500_000  # 50만 행 이상이면 큰 테이블(최적화 쿼리 사용)
SIZE_INFO_LOG_THRESHOLD = 100_000    # 10만 행 이상이면 크기 정보 로그 표시


class ForeignKeyAnalyzer:
    """FK 관계 분석 및 고아 레코드 탐지"""

    def __init__(self, connector, log: Callable[[str], None]):
        self.connector = connector
        # 파사드가 공유하는 _log 를 주입받아 진행 상황을 동일 콜백으로 전달한다.
        self._log = log

    def get_foreign_keys(self, schema: str) -> List[ForeignKeyInfo]:
        """스키마의 모든 FK 관계 조회"""
        query = """
        SELECT
            tc.CONSTRAINT_NAME,
            kcu.TABLE_NAME as CHILD_TABLE,
            kcu.COLUMN_NAME as CHILD_COLUMN,
            kcu.REFERENCED_TABLE_NAME as PARENT_TABLE,
            kcu.REFERENCED_COLUMN_NAME as PARENT_COLUMN,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        WHERE tc.TABLE_SCHEMA = %s
            AND tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
        ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
        """
        rows = self.connector.execute(query, (schema,))

        fk_list = []
        for row in rows:
            fk_list.append(ForeignKeyInfo(
                constraint_name=row['CONSTRAINT_NAME'],
                child_table=row['CHILD_TABLE'],
                child_column=row['CHILD_COLUMN'],
                parent_table=row['PARENT_TABLE'],
                parent_column=row['PARENT_COLUMN'],
                on_delete=row['DELETE_RULE'],
                on_update=row['UPDATE_RULE']
            ))

        return fk_list

    def build_fk_tree(self, schema: str) -> Dict[str, List[str]]:
        """FK 관계 트리 구성 (부모 → 자식 목록)"""
        fk_list = self.get_foreign_keys(schema)

        tree = {}
        for fk in fk_list:
            if fk.parent_table not in tree:
                tree[fk.parent_table] = []
            if fk.child_table not in tree[fk.parent_table]:
                tree[fk.parent_table].append(fk.child_table)

        return tree

    def _get_table_row_count(self, schema: str, table: str) -> int:
        """테이블 대략적인 행 수 조회 (INFORMATION_SCHEMA 사용, 빠름)"""
        query = f"""
        SELECT TABLE_ROWS
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{table}'
        """
        result = self.connector.execute(query)
        return result[0]['TABLE_ROWS'] if result and result[0]['TABLE_ROWS'] else 0

    def _build_orphan_query(
        self,
        schema: str,
        fk: ForeignKeyInfo,
        is_large: bool,
        select_expr: str,
        limit: Optional[int] = None
    ) -> str:
        """고아 레코드 조회 쿼리 생성 (count/sample 두 형태를 한 곳에서 생성)

        - 대용량 테이블: NOT EXISTS (더 빠름)
        - 일반 테이블: LEFT JOIN
        select_expr 로 count("COUNT(*) as cnt")/sample("DISTINCT ... as orphan_value")를
        구분하고, limit 이 주어지면 LIMIT 절을 덧붙인다.
        """
        if is_large:
            query = f"""
        SELECT {select_expr}
        FROM `{schema}`.`{fk.child_table}` c
        WHERE c.`{fk.child_column}` IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM `{schema}`.`{fk.parent_table}` p
                WHERE p.`{fk.parent_column}` = c.`{fk.child_column}`
            )"""
        else:
            query = f"""
        SELECT {select_expr}
        FROM `{schema}`.`{fk.child_table}` c
        LEFT JOIN `{schema}`.`{fk.parent_table}` p
            ON c.`{fk.child_column}` = p.`{fk.parent_column}`
        WHERE c.`{fk.child_column}` IS NOT NULL
            AND p.`{fk.parent_column}` IS NULL"""

        if limit is not None:
            query += f"\n        LIMIT {limit}"

        return query

    def find_orphan_records(
        self,
        schema: str,
        sample_limit: int = 5,
        large_table_threshold: int = LARGE_TABLE_ROW_THRESHOLD
    ) -> List[OrphanRecord]:
        """고아 레코드 탐지 (부모 없는 자식 레코드)"""
        self._log("🔍 고아 레코드 탐지 중...")

        fk_list = self.get_foreign_keys(schema)
        orphans = []

        for i, fk in enumerate(fk_list, 1):
            try:
                # 테이블 크기 사전 확인
                child_rows = self._get_table_row_count(schema, fk.child_table)
                parent_rows = self._get_table_row_count(schema, fk.parent_table)
                is_large = child_rows > large_table_threshold or parent_rows > large_table_threshold

                size_info = ""
                if child_rows > SIZE_INFO_LOG_THRESHOLD or parent_rows > SIZE_INFO_LOG_THRESHOLD:
                    size_info = f" [자식:{child_rows:,}행, 부모:{parent_rows:,}행]"

                self._log(f"  검사 중: {fk.child_table}.{fk.child_column} → {fk.parent_table}.{fk.parent_column} ({i}/{len(fk_list)}){size_info}")

                start_time = time.time()

                if is_large:
                    # 큰 테이블: NOT EXISTS 사용 (더 빠름)
                    self._log(f"    📊 대용량 테이블 - 최적화 쿼리 사용")
                count_query = self._build_orphan_query(schema, fk, is_large, "COUNT(*) as cnt")

                result = self.connector.execute(count_query)
                orphan_count = result[0]['cnt'] if result else 0

                elapsed = time.time() - start_time
                if elapsed > 3:  # 3초 이상 걸리면 경고
                    self._log(f"    ⏱️ 쿼리 소요시간: {elapsed:.1f}초")

                if orphan_count > 0:
                    # 샘플 값 조회 (항상 LIMIT으로 제한)
                    sample_query = self._build_orphan_query(
                        schema, fk, is_large,
                        f"DISTINCT c.`{fk.child_column}` as orphan_value",
                        limit=sample_limit
                    )
                    samples = self.connector.execute(sample_query)
                    sample_values = [s['orphan_value'] for s in samples]

                    orphans.append(OrphanRecord(
                        child_table=fk.child_table,
                        child_column=fk.child_column,
                        parent_table=fk.parent_table,
                        parent_column=fk.parent_column,
                        orphan_count=orphan_count,
                        sample_values=sample_values
                    ))

                    self._log(f"    ⚠️ 고아 레코드 발견: {orphan_count}개")

            except Exception as e:
                self._log(f"    ❌ 검사 실패: {fk.child_table}.{fk.child_column} - {str(e)}")
                continue

        return orphans

    def get_fk_visualization(self, schema: str) -> str:
        """FK 관계를 트리 형태로 시각화"""
        fk_tree = self.build_fk_tree(schema)

        if not fk_tree:
            return "FK 관계가 없습니다."

        lines = ["FK 관계 트리:", ""]

        # 루트 테이블 찾기 (다른 테이블의 자식이 아닌 테이블)
        all_children = set()
        for children in fk_tree.values():
            all_children.update(children)

        root_tables = set(fk_tree.keys()) - all_children

        def print_tree(table: str, prefix: str = "", is_last: bool = True, visited: set = None):
            if visited is None:
                visited = set()

            connector = "└── " if is_last else "├── "

            # 순환 참조 감지
            if table in visited:
                lines.append(f"{prefix}{connector}🔄 {table} (순환 참조)")
                return

            lines.append(f"{prefix}{connector}{table}")

            if table in fk_tree:
                children = fk_tree[table]
                child_prefix = prefix + ("    " if is_last else "│   ")
                for i, child in enumerate(children):
                    print_tree(child, child_prefix, i == len(children) - 1, visited | {table})

        for i, root in enumerate(sorted(root_tables)):
            print_tree(root, "", i == len(root_tables) - 1, set())

        return "\n".join(lines)
