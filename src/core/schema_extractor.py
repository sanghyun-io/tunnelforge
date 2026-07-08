"""
DB 스키마 구조 추출기
"""
from typing import List, Dict, Optional, Tuple

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

    def extract_table_schema(self, schema: str, table: str) -> Optional[TableSchema]:
        """테이블 스키마 정보 추출

        Args:
            schema: 데이터베이스 이름
            table: 테이블 이름

        Returns:
            TableSchema 또는 None
        """
        try:
            # 컬럼 정보
            columns = self._get_columns(schema, table)

            # 인덱스 정보
            indexes = self._get_indexes(schema, table)

            # 외래 키 정보
            foreign_keys = self._get_foreign_keys(schema, table)

            # 테이블 옵션
            engine, charset, collation = self._get_table_options(schema, table)

            # 행 수
            row_count = self._get_row_count(schema, table)

            return TableSchema(
                name=table,
                columns=columns,
                indexes=indexes,
                foreign_keys=foreign_keys,
                engine=engine,
                charset=charset,
                collation=collation,
                row_count=row_count
            )

        except Exception as e:
            logger.error(f"테이블 스키마 추출 실패 ({schema}.{table}): {e}")
            return None

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

        try:
            result = self.connector.execute(query, (schema,))
            for row in result:
                table_name = row['TABLE_NAME']
                table_schema = self.extract_table_schema(schema, table_name)
                if table_schema:
                    tables[table_name] = table_schema
        except Exception as e:
            logger.error(f"테이블 목록 조회 실패 ({schema}): {e}")

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
        try:
            result = self.connector.execute(query, (schema, table))
            for row in result:
                col = ColumnInfo(
                    name=row['COLUMN_NAME'],
                    data_type=row['COLUMN_TYPE'],
                    nullable=(row['IS_NULLABLE'] == 'YES'),
                    default=row['COLUMN_DEFAULT'],
                    extra=_normalize_column_extra(row['EXTRA']),
                    key=row['COLUMN_KEY'] or '',
                    charset=row['CHARACTER_SET_NAME'] or '',
                    collation=row['COLLATION_NAME'] or ''
                )
                columns.append(col)
        except Exception as e:
            logger.error(f"컬럼 정보 조회 실패: {e}")

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
        try:
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
        except Exception as e:
            logger.error(f"인덱스 정보 조회 실패: {e}")

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
        try:
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
        except Exception as e:
            logger.error(f"FK 정보 조회 실패: {e}")

        return list(fk_map.values())

    def _get_table_options(self, schema: str, table: str) -> Tuple[str, str, str]:
        """테이블 옵션 조회"""
        query = """
            SELECT ENGINE, TABLE_COLLATION
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """

        try:
            result = self.connector.execute(query, (schema, table))
            if result:
                row = result[0]
                engine = row['ENGINE']
                collation = row['TABLE_COLLATION']

                # Collation에서 charset 추출
                charset = collation.split('_')[0] if collation else 'utf8mb4'
                return engine or 'InnoDB', charset, collation or ''
        except Exception as e:
            logger.error(f"테이블 옵션 조회 실패: {e}")

        return 'InnoDB', 'utf8mb4', 'utf8mb4_general_ci'

    def _get_row_count(self, schema: str, table: str) -> int:
        """테이블 행 수 조회"""
        query = f"SELECT COUNT(*) as cnt FROM `{schema}`.`{table}`"
        try:
            result = self.connector.execute(query)
            if result:
                return result[0]['cnt']
        except Exception:
            pass
        return 0
