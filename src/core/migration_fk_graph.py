"""
마이그레이션 자동 수정 위저드 - FK 관계 그래프 분석

Collation 변경 시 FK로 연결된 테이블을 함께 변경하기 위한 그래프 유틸리티.
이 모듈은 leaf 계층으로, connector 외의 wizard-domain 모듈을 import하지 않는다.
"""
from typing import List, Dict, Set
from collections import deque

from src.core.db_connector import MySQLConnector


class CollationFKGraphBuilder:
    """FK 관계 그래프 분석기

    Collation 변경 시 FK로 연결된 테이블을 함께 변경해야 합니다.
    이 클래스는 FK 관계를 분석하여:
    1. 연관된 테이블 목록 탐색 (BFS)
    2. 변경 순서 결정 (위상 정렬)
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        # 양방향 그래프: table -> set of related tables
        self.graph: Dict[str, Set[str]] = {}
        # 방향 그래프: child -> parent (위상 정렬용)
        self.parent_graph: Dict[str, Set[str]] = {}

    def build_graph(self):
        """FK 관계 그래프 구성

        Note: VIEW는 FK 관계 대상에서 제외 (BASE TABLE만 포함)
        """
        query = """
        SELECT
            kcu.TABLE_NAME as CHILD_TABLE,
            kcu.REFERENCED_TABLE_NAME as PARENT_TABLE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.TABLES t_child
            ON kcu.TABLE_NAME = t_child.TABLE_NAME
            AND kcu.TABLE_SCHEMA = t_child.TABLE_SCHEMA
        JOIN INFORMATION_SCHEMA.TABLES t_parent
            ON kcu.REFERENCED_TABLE_NAME = t_parent.TABLE_NAME
            AND kcu.TABLE_SCHEMA = t_parent.TABLE_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s
            AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            AND t_child.TABLE_TYPE = 'BASE TABLE'
            AND t_parent.TABLE_TYPE = 'BASE TABLE'
        """
        rows = self.connector.execute(query, (self.schema,))

        for row in rows:
            child = row['CHILD_TABLE']
            parent = row['PARENT_TABLE']

            # 양방향 그래프
            if child not in self.graph:
                self.graph[child] = set()
            if parent not in self.graph:
                self.graph[parent] = set()

            self.graph[child].add(parent)
            self.graph[parent].add(child)

            # 방향 그래프 (자식 → 부모)
            if child not in self.parent_graph:
                self.parent_graph[child] = set()
            self.parent_graph[child].add(parent)

    def get_related_tables(self, start_table: str) -> Set[str]:
        """BFS로 연관 테이블 탐색

        Args:
            start_table: 시작 테이블

        Returns:
            연관된 모든 테이블 집합 (시작 테이블 제외)
        """
        if start_table not in self.graph:
            return set()

        visited = {start_table}
        queue = deque([start_table])
        related = set()

        while queue:
            current = queue.popleft()
            for neighbor in self.graph.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    related.add(neighbor)
                    queue.append(neighbor)

        return related

    def get_topological_order(self, tables: Set[str]) -> List[str]:
        """위상 정렬 (Kahn's algorithm)

        FK 관계에서 부모 테이블을 먼저 변경해야 합니다.

        Args:
            tables: 정렬할 테이블 집합

        Returns:
            위상 정렬된 테이블 목록 (부모 먼저)
        """
        # 부분 그래프의 진입 차수 계산
        in_degree: Dict[str, int] = {t: 0 for t in tables}

        for child in tables:
            parents = self.parent_graph.get(child, set())
            for parent in parents:
                if parent in tables:
                    in_degree[child] += 1

        # 진입 차수가 0인 노드(루트 테이블)부터 시작
        queue = deque([t for t in tables if in_degree[t] == 0])
        result = []

        while queue:
            current = queue.popleft()
            result.append(current)

            # 현재 노드를 부모로 가진 자식들의 진입 차수 감소
            for child in tables:
                if current in self.parent_graph.get(child, set()):
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

        # 순환 참조가 있으면 남은 테이블 추가
        remaining = [t for t in tables if t not in result]
        result.extend(remaining)

        return result

    def get_children(self, table: str) -> Set[str]:
        """table을 참조하는 자식 테이블 목록

        Args:
            table: 부모 테이블명

        Returns:
            자식 테이블 집합 (이 테이블을 FK로 참조하는 테이블들)
        """
        children = set()
        for child, parents in self.parent_graph.items():
            if table in parents:
                children.add(child)
        return children

    def get_parents(self, table: str) -> Set[str]:
        """table이 참조하는 부모 테이블 목록

        Args:
            table: 자식 테이블명

        Returns:
            부모 테이블 집합 (이 테이블이 FK로 참조하는 테이블들)
        """
        return self.parent_graph.get(table, set()).copy()

    def get_cascade_skip_tables(self, table_to_skip: str, target_tables: Set[str]) -> Set[str]:
        """특정 테이블 건너뛰기 시 연쇄적으로 건너뛰어야 하는 테이블 목록

        규칙:
        1. table_to_skip을 참조하는 자식 테이블 → 반드시 건너뛰기
           (부모 charset이 변경되지 않으면 자식도 변경 불가)
        2. table_to_skip이 참조하는 부모 (target_tables에 있으면) → 건너뛰기
           (자식이 변경되지 않으면 부모만 변경해도 FK 불일치 발생)
        3. 위 테이블들에 대해 재귀적으로 BFS 수행

        Args:
            table_to_skip: 건너뛰기할 테이블
            target_tables: 변경 대상 테이블 집합

        Returns:
            연쇄적으로 건너뛰어야 하는 테이블 집합 (table_to_skip 제외)
        """
        cascade_skip = set()
        visited = {table_to_skip}
        queue = deque([table_to_skip])

        while queue:
            current = queue.popleft()

            # 1. 자식 테이블 (current를 참조하는 테이블)
            children = self.get_children(current)
            for child in children:
                if child in target_tables and child not in visited:
                    visited.add(child)
                    cascade_skip.add(child)
                    queue.append(child)

            # 2. 부모 테이블 (current가 참조하는 테이블)
            # 자식이 건너뛰면 부모도 건너뛰어야 함 (FK 일관성)
            parents = self.get_parents(current)
            for parent in parents:
                if parent in target_tables and parent not in visited:
                    visited.add(parent)
                    cascade_skip.add(parent)
                    queue.append(parent)

        return cascade_skip


def build_fk_graph(connector: MySQLConnector, schema: str) -> CollationFKGraphBuilder:
    """FK 관계 그래프 빌더를 생성하고 build_graph()까지 수행 (lazy-init 공유 헬퍼)

    SmartFixGenerator / FKSafeCharsetChanger / BatchFixExecutor /
    CharsetFixPlanBuilder가 verbatim 중복하던 생성+build 코드를 통합한다.
    각 클래스는 per-instance 캐시 가드만 유지하고 생성은 이 헬퍼에 위임한다.
    """
    builder = CollationFKGraphBuilder(connector, schema)
    builder.build_graph()
    return builder
