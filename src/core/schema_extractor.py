"""
DB 스키마 구조 추출기
"""
from typing import Callable, List, Dict, Optional, Tuple

from src.core.logger import get_logger
from src.core.schema_diff_models import (
    ColumnInfo, ForeignKeyInfo, IndexInfo, TableSchema, _normalize_column_extra
)

logger = get_logger(__name__)


class SchemaExtractor:
    """스키마 정보 추출기"""

    def __init__(self, connector):
        """
        Args:
            connector: MySQLConnector 인스턴스
        """
        self.connector = connector

    def _swallow_errors(
        self, action: Callable[[], None], error_message: Optional[str] = None
    ) -> None:
        """action()을 실행하고 예외 발생 시 삼킨다 (반환값 없음).

        action은 호출부의 지역 변수(리스트/딕셔너리 누산기 또는 outcome 딕셔너리)를
        직접 채우는 클로저여야 하며, 호출부가 그 변수를 읽어 결과를 반환한다.
        error_message가 주어지면 `f"{error_message}: {e}"` 형식으로 로깅하고,
        None이면 조용히 삼킨다(로깅 없음) - 각 호출부의 기존 로깅 유무를 그대로 보존한다.
        """
        try:
            action()
        except Exception as e:
            if error_message:
                logger.error(f"{error_message}: {e}")

    def extract_table_schema(self, schema: str, table: str) -> Optional[TableSchema]:
        """테이블 스키마 정보 추출

        Args:
            schema: 데이터베이스 이름
            table: 테이블 이름

        Returns:
            TableSchema 또는 None
        """
        outcome = {}

        def _fetch():
            columns = self._get_columns(schema, table)
            indexes = self._get_indexes(schema, table)
            foreign_keys = self._get_foreign_keys(schema, table)
            engine, charset, collation = self._get_table_options(schema, table)
            row_count = self._get_row_count(schema, table)

            outcome['value'] = TableSchema(
                name=table,
                columns=columns,
                indexes=indexes,
                foreign_keys=foreign_keys,
                engine=engine,
                charset=charset,
                collation=collation,
                row_count=row_count
            )

        self._swallow_errors(_fetch, f"테이블 스키마 추출 실패 ({schema}.{table})")
        return outcome.get('value')

    def extract_all_tables(self, schema: str) -> Dict[str, TableSchema]:
        """스키마 내 모든 테이블 정보 추출

        Args:
            schema: 데이터베이스 이름

        Returns:
            {테이블명: TableSchema} 딕셔너리
        """
        tables = {}

        # 테이블 목록 조회
        query = """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """

        def _fetch():
            result = self.connector.execute(query, (schema,))
            for row in result:
                table_name = row['TABLE_NAME']
                table_schema = self.extract_table_schema(schema, table_name)
                if table_schema:
                    tables[table_name] = table_schema

        self._swallow_errors(_fetch, f"테이블 목록 조회 실패 ({schema})")
        return tables

    def _get_columns(self, schema: str, table: str) -> List[ColumnInfo]:
        """컬럼 정보 조회"""
        query = """
            SELECT
                COLUMN_NAME,
                COLUMN_TYPE,
                IS_NULLABLE,
                COLUMN_DEFAULT,
                EXTRA,
                COLUMN_KEY,
                CHARACTER_SET_NAME,
                COLLATION_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """

        columns = []

        def _fetch():
            result = self.connector.execute(query, (schema, table))
            for row in result:
                columns.append(ColumnInfo(
                    name=row['COLUMN_NAME'],
                    data_type=row['COLUMN_TYPE'],
                    nullable=(row['IS_NULLABLE'] == 'YES'),
                    default=row['COLUMN_DEFAULT'],
                    extra=_normalize_column_extra(row['EXTRA']),
                    key=row['COLUMN_KEY'] or '',
                    charset=row['CHARACTER_SET_NAME'] or '',
                    collation=row['COLLATION_NAME'] or ''
                ))

        self._swallow_errors(_fetch, "컬럼 정보 조회 실패")
        return columns

    def _get_indexes(self, schema: str, table: str) -> List[IndexInfo]:
        """인덱스 정보 조회"""
        query = """
            SELECT
                INDEX_NAME,
                COLUMN_NAME,
                NON_UNIQUE,
                INDEX_TYPE
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY INDEX_NAME, SEQ_IN_INDEX
        """

        index_map = {}

        def _fetch():
            result = self.connector.execute(query, (schema, table))
            for row in result:
                idx_name = row['INDEX_NAME']
                col_name = row['COLUMN_NAME']
                non_unique = row['NON_UNIQUE']
                idx_type = row['INDEX_TYPE']

                if idx_name not in index_map:
                    index_map[idx_name] = IndexInfo(
                        name=idx_name,
                        columns=[],
                        unique=(non_unique == 0),
                        type=idx_type
                    )
                index_map[idx_name].columns.append(col_name)

        self._swallow_errors(_fetch, "인덱스 정보 조회 실패")
        return list(index_map.values())

    def _get_foreign_keys(self, schema: str, table: str) -> List[ForeignKeyInfo]:
        """외래 키 정보 조회"""
        query = """
            SELECT
                kcu.CONSTRAINT_NAME,
                kcu.COLUMN_NAME,
                kcu.REFERENCED_TABLE_NAME,
                kcu.REFERENCED_COLUMN_NAME,
                rc.DELETE_RULE,
                rc.UPDATE_RULE
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
            WHERE kcu.TABLE_SCHEMA = %s
              AND kcu.TABLE_NAME = %s
              AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """

        fk_map = {}

        def _fetch():
            result = self.connector.execute(query, (schema, table))
            for row in result:
                fk_name = row['CONSTRAINT_NAME']
                col = row['COLUMN_NAME']
                ref_table = row['REFERENCED_TABLE_NAME']
                ref_col = row['REFERENCED_COLUMN_NAME']
                on_del = row['DELETE_RULE']
                on_upd = row['UPDATE_RULE']

                if fk_name not in fk_map:
                    fk_map[fk_name] = ForeignKeyInfo(
                        name=fk_name,
                        columns=[],
                        ref_table=ref_table,
                        ref_columns=[],
                        on_delete=on_del,
                        on_update=on_upd
                    )
                fk_map[fk_name].columns.append(col)
                fk_map[fk_name].ref_columns.append(ref_col)

        self._swallow_errors(_fetch, "FK 정보 조회 실패")
        return list(fk_map.values())

    def _get_table_options(self, schema: str, table: str) -> Tuple[str, str, str]:
        """테이블 옵션 조회"""
        query = """
            SELECT ENGINE, TABLE_COLLATION
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """

        outcome = {}

        def _fetch():
            result = self.connector.execute(query, (schema, table))
            if result:
                row = result[0]
                engine = row['ENGINE']
                collation = row['TABLE_COLLATION']

                # Collation에서 charset 추출
                charset = collation.split('_')[0] if collation else 'utf8mb4'
                outcome['value'] = (engine or 'InnoDB', charset, collation or '')

        self._swallow_errors(_fetch, "테이블 옵션 조회 실패")
        return outcome.get('value', ('InnoDB', 'utf8mb4', 'utf8mb4_general_ci'))

    def _get_row_count(self, schema: str, table: str) -> int:
        """테이블 행 수 조회"""
        query = f"SELECT COUNT(*) as cnt FROM `{schema}`.`{table}`"
        outcome = {}

        def _fetch():
            result = self.connector.execute(query)
            if result:
                outcome['value'] = result[0]['cnt']

        self._swallow_errors(_fetch)
        return outcome.get('value', 0)
