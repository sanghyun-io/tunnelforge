"""
마이그레이션 자동 수정 위저드 - 문자셋 수정 계획 빌더

문자셋 이슈 테이블 + FK 연관 테이블 전체 목록을 만들고, 연쇄 건너뛰기 계산과
FK 안전 변경 SQL 생성을 담당한다.
"""
from typing import List, Dict, Set, Optional, Tuple, Any

from src.core.db_connector import MySQLConnector
from src.core.migration_fix_models import (
    CharsetTableInfo,
    get_table_charset,
    DEFAULT_TARGET_CHARSET,
    DEFAULT_TARGET_COLLATION,
)
from src.core.migration_fk_graph import CollationFKGraphBuilder, build_fk_graph
from src.core.migration_fk_safe_charset import FKSafeCharsetChanger


class CharsetFixPlanBuilder:
    """문자셋 수정 계획 빌더

    문자셋 이슈에 대해:
    1. 원본 이슈 테이블 + FK 연관 테이블 전체 목록 생성
    2. 연쇄 건너뛰기 테이블 계산
    3. FK 안전 변경 SQL 생성 (무조건 FK DROP → 변경 → FK 재생성)
    """

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        original_issue_tables: Set[str]
    ):
        """
        Args:
            connector: DB 연결
            schema: 스키마명
            original_issue_tables: 원본 분석에서 검출된 이슈 테이블 집합
        """
        self.connector = connector
        self.schema = schema
        self.original_issue_tables = original_issue_tables

        # FK 그래프 빌더
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

        # 테이블 정보 캐시
        self._table_info_cache: Dict[str, CharsetTableInfo] = {}

    def _get_fk_graph_builder(self) -> CollationFKGraphBuilder:
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = build_fk_graph(self.connector, self.schema)
        return self._fk_graph_builder

    def _get_table_charset(self, table: str) -> Tuple[str, str]:
        """테이블의 현재 charset/collation 조회 (공유 헬퍼 위임)"""
        return get_table_charset(self.connector, self.schema, table)

    def build_full_table_list(self) -> List[CharsetTableInfo]:
        """원본 이슈 테이블 + FK 연관 테이블 전체 목록 생성

        Returns:
            CharsetTableInfo 목록 (위상 정렬 순서)
        """
        fk_builder = self._get_fk_graph_builder()

        # 1. 원본 이슈 테이블의 모든 FK 연관 테이블 수집
        all_tables: Set[str] = set()
        for table in self.original_issue_tables:
            all_tables.add(table)
            related = fk_builder.get_related_tables(table)
            all_tables.update(related)

        # 2. 위상 정렬 (부모 먼저)
        ordered_tables = fk_builder.get_topological_order(all_tables)

        # 3. 각 테이블 정보 생성
        result: List[CharsetTableInfo] = []
        for table in ordered_tables:
            if table in self._table_info_cache:
                result.append(self._table_info_cache[table])
                continue

            charset, collation = self._get_table_charset(table)
            parents = list(fk_builder.get_parents(table))
            children = list(fk_builder.get_children(table))

            info = CharsetTableInfo(
                table_name=table,
                current_charset=charset,
                current_collation=collation,
                fk_parents=parents,
                fk_children=children,
                is_original_issue=(table in self.original_issue_tables),
                skip=False
            )
            self._table_info_cache[table] = info
            result.append(info)

        return result

    def get_cascade_skip_tables(self, table_to_skip: str) -> Set[str]:
        """연쇄 건너뛰기 테이블 계산

        특정 테이블 건너뛰기 시 FK 관계로 인해 함께 건너뛰어야 하는 테이블 목록.

        Args:
            table_to_skip: 건너뛰기할 테이블

        Returns:
            연쇄적으로 건너뛰어야 하는 테이블 집합 (table_to_skip 제외)
        """
        fk_builder = self._get_fk_graph_builder()

        # 전체 대상 테이블 목록
        target_tables = {info.table_name for info in self.build_full_table_list()}

        return fk_builder.get_cascade_skip_tables(table_to_skip, target_tables)

    def generate_fix_sql(
        self,
        tables_to_fix: Set[str],
        charset: str = DEFAULT_TARGET_CHARSET,
        collation: str = DEFAULT_TARGET_COLLATION
    ) -> Dict[str, Any]:
        """FK 안전 변경 SQL 생성

        무조건 FK DROP → charset 변경 → FK 재생성 방식 사용.

        Args:
            tables_to_fix: 변경할 테이블 집합
            charset: 목표 charset
            collation: 목표 collation

        Returns:
            Dict with keys: 'drop_fks', 'alter_tables', 'add_fks', 'full_sql', 'fk_count', 'table_count'
        """
        if not tables_to_fix:
            return {
                'drop_fks': [],
                'alter_tables': [],
                'add_fks': [],
                'full_sql': ["-- 변경할 테이블이 없습니다."],
                'fk_count': 0,
                'table_count': 0
            }

        # FKSafeCharsetChanger 사용
        changer = FKSafeCharsetChanger(self.connector, self.schema)
        return changer.generate_safe_charset_sql(tables_to_fix, charset, collation)
