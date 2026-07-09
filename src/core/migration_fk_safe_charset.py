"""
마이그레이션 자동 수정 위저드 - FK 안전 Charset 변경기

Error 3780(FK 컬럼 타입 불일치) 방지를 위해 FK를 임시 DROP → charset 변경 →
FK 재생성하는 SQL을 생성한다. 실제 실행(mutation)은 Rust Core가 소유하며,
이 모듈은 dry-run/SQL 미리보기만 담당한다.
"""
from typing import List, Dict, Set, Optional, Tuple, Callable

from src.core.db_connector import MySQLConnector
from src.core.migration_fix_models import (
    FKDefinition,
    DEFAULT_TARGET_CHARSET,
    DEFAULT_TARGET_COLLATION,
)
from src.core.migration_fk_graph import CollationFKGraphBuilder, build_fk_graph


class FKSafeCharsetChanger:
    """FK 안전 Charset 변경기

    Error 3780 방지를 위해 FK를 임시 DROP 후 charset 변경, 다시 FK 재생성합니다.

    문제: SET FOREIGN_KEY_CHECKS = 0은 데이터 삽입 시 FK 검증만 비활성화.
    기존 FK 제약조건의 컬럼 타입 호환성 검사는 여전히 동작함.

    해결:
    1. FK 임시 DROP (영향받는 모든 FK)
    2. CONVERT TO CHARACTER SET (위상 정렬: 부모 먼저)
    3. FK 재생성 (원래 정의대로)
    """

    def __init__(self, connector: MySQLConnector, schema: str):
        self.connector = connector
        self.schema = schema
        self._fk_graph_builder: Optional[CollationFKGraphBuilder] = None

    def _get_fk_graph_builder(self) -> CollationFKGraphBuilder:
        """FK 그래프 빌더 (lazy init)"""
        if self._fk_graph_builder is None:
            self._fk_graph_builder = build_fk_graph(self.connector, self.schema)
        return self._fk_graph_builder

    def get_related_fks(self, tables: Set[str]) -> List[FKDefinition]:
        """대상 테이블과 연관된 모든 FK 정의 조회

        Args:
            tables: 대상 테이블 집합

        Returns:
            FKDefinition 목록 (복합 FK는 ORDINAL_POSITION으로 그룹화)

        Note: VIEW는 FK 관계 대상에서 제외 (BASE TABLE만 포함)
        """
        if not tables:
            return []

        # 테이블 목록을 IN 절에서 사용
        placeholders = ", ".join(["%s"] * len(tables))

        query = f"""
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.TABLE_NAME,
            kcu.COLUMN_NAME,
            kcu.REFERENCED_TABLE_NAME,
            kcu.REFERENCED_COLUMN_NAME,
            kcu.ORDINAL_POSITION,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
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
            AND (kcu.TABLE_NAME IN ({placeholders}) OR kcu.REFERENCED_TABLE_NAME IN ({placeholders}))
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """

        params = [self.schema] + list(tables) + list(tables)
        rows = self.connector.execute(query, tuple(params))

        # 복합 FK 그룹화
        fk_map: Dict[str, FKDefinition] = {}

        for row in rows:
            constraint_name = row['CONSTRAINT_NAME']

            if constraint_name not in fk_map:
                fk_map[constraint_name] = FKDefinition(
                    constraint_name=constraint_name,
                    table_name=row['TABLE_NAME'],
                    columns=[],
                    ref_table=row['REFERENCED_TABLE_NAME'],
                    ref_columns=[],
                    on_delete=row['DELETE_RULE'] or 'RESTRICT',
                    on_update=row['UPDATE_RULE'] or 'RESTRICT'
                )

            fk_map[constraint_name].columns.append(row['COLUMN_NAME'])
            fk_map[constraint_name].ref_columns.append(row['REFERENCED_COLUMN_NAME'])

        return list(fk_map.values())

    def generate_safe_charset_sql(
        self,
        tables: Set[str],
        charset: str = DEFAULT_TARGET_CHARSET,
        collation: str = DEFAULT_TARGET_COLLATION
    ) -> Dict[str, List[str]]:
        """FK 안전 Charset 변경 SQL 생성

        Args:
            tables: 변경할 테이블 집합
            charset: 목표 charset
            collation: 목표 collation

        Returns:
            Dict with keys: 'drop_fks', 'alter_tables', 'add_fks', 'full_sql'
        """
        # 1. 연관 FK 조회
        fks = self.get_related_fks(tables)

        # 2. 위상 정렬 (부모 먼저)
        fk_builder = self._get_fk_graph_builder()
        ordered_tables = fk_builder.get_topological_order(tables)

        # 3. SQL 생성
        drop_fks = []
        add_fks = []

        for fk in fks:
            drop_fks.append(fk.get_drop_sql(self.schema))
            add_fks.append(fk.get_add_sql(self.schema))

        alter_tables = []
        for table in ordered_tables:
            alter_tables.append(
                f"ALTER TABLE `{self.schema}`.`{table}` "
                f"CONVERT TO CHARACTER SET {charset} COLLATE {collation};"
            )

        # 4. 전체 SQL 조합
        full_sql = []
        full_sql.append("-- ===== Phase 1: FK 임시 DROP =====")
        if drop_fks:
            full_sql.extend(drop_fks)
        else:
            full_sql.append("-- (연관 FK 없음)")

        full_sql.append("")
        full_sql.append("-- ===== Phase 2: Charset 변경 (부모 먼저) =====")
        full_sql.extend(alter_tables)

        full_sql.append("")
        full_sql.append("-- ===== Phase 3: FK 재생성 =====")
        if add_fks:
            full_sql.extend(add_fks)
        else:
            full_sql.append("-- (재생성할 FK 없음)")

        return {
            'drop_fks': drop_fks,
            'alter_tables': alter_tables,
            'add_fks': add_fks,
            'full_sql': full_sql,
            'fk_count': len(fks),
            'table_count': len(ordered_tables)
        }

    def execute_safe_charset_change(
        self,
        tables: Set[str],
        charset: str = DEFAULT_TARGET_CHARSET,
        collation: str = DEFAULT_TARGET_COLLATION,
        dry_run: bool = True,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str, Dict]:
        """FK 안전 Charset 변경 실행

        Args:
            tables: 변경할 테이블 집합
            charset: 목표 charset
            collation: 목표 collation
            dry_run: True면 SQL만 생성
            progress_callback: 진행 로그 콜백

        Returns:
            (success, message, result_dict)

        dry-run-only 계약: dry_run=False는 즉시 거부되므로 이 메서드는 항상
        SQL 미리보기만 생성한다. 실제 DDL 실행 및 그에 딸린 recovery-SQL
        스택 관리는 Rust Core가 담당하며, 이 클래스에는 존재하지 않는다.
        """
        if not dry_run:
            raise RuntimeError(
                "Legacy Python Auto-Fix Wizard mutation execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        def log(msg: str):
            if progress_callback:
                progress_callback(msg)

        sql_parts = self.generate_safe_charset_sql(tables, charset, collation)

        log(f"📋 [DRY-RUN] FK 안전 Charset 변경 SQL 생성 완료")
        log(f"   - 영향받는 FK: {sql_parts['fk_count']}개")
        log(f"   - 변경할 테이블: {sql_parts['table_count']}개")
        return True, "DRY-RUN 완료", sql_parts
