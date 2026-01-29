"""
스키마 비교 (Schema Diff)
- 두 DB 스키마 구조 비교
- 테이블/컬럼/인덱스/FK 차이 분석
- 동기화 SQL 스크립트 생성
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from enum import Enum

from src.core.logger import get_logger

logger = get_logger(__name__)


class DiffType(Enum):
    """차이 유형"""
    ADDED = "added"       # 타겟에 추가 필요
    REMOVED = "removed"   # 타겟에서 삭제 필요
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass
class ColumnInfo:
    """컬럼 정보"""
    name: str
    data_type: str
    nullable: bool
    default: Optional[str]
    extra: str = ""      # AUTO_INCREMENT 등
    key: str = ""        # PRI, UNI, MUL
    charset: str = ""
    collation: str = ""

    def to_sql_definition(self) -> str:
        """SQL 컬럼 정의 생성"""
        parts = [f"`{self.name}`", self.data_type]

        if self.charset and self.charset not in self.data_type:
            parts.append(f"CHARACTER SET {self.charset}")

        if not self.nullable:
            parts.append("NOT NULL")
        else:
            parts.append("NULL")

        if self.default is not None:
            if self.default.upper() in ('CURRENT_TIMESTAMP', 'NULL'):
                parts.append(f"DEFAULT {self.default}")
            else:
                parts.append(f"DEFAULT '{self.default}'")

        if self.extra:
            parts.append(self.extra)

        return " ".join(parts)


@dataclass
class IndexInfo:
    """인덱스 정보"""
    name: str
    columns: List[str]
    unique: bool = False
    type: str = "BTREE"   # BTREE, FULLTEXT, HASH

    def to_sql_definition(self, table_name: str) -> str:
        """인덱스 생성 SQL"""
        cols = ", ".join(f"`{c}`" for c in self.columns)
        if self.name == "PRIMARY":
            return f"PRIMARY KEY ({cols})"
        elif self.unique:
            return f"UNIQUE INDEX `{self.name}` ({cols}) USING {self.type}"
        else:
            return f"INDEX `{self.name}` ({cols}) USING {self.type}"


@dataclass
class ForeignKeyInfo:
    """외래 키 정보"""
    name: str
    columns: List[str]
    ref_table: str
    ref_columns: List[str]
    on_delete: str = "RESTRICT"
    on_update: str = "RESTRICT"

    def to_sql_definition(self) -> str:
        """FK 정의 SQL"""
        cols = ", ".join(f"`{c}`" for c in self.columns)
        ref_cols = ", ".join(f"`{c}`" for c in self.ref_columns)
        return (
            f"CONSTRAINT `{self.name}` FOREIGN KEY ({cols}) "
            f"REFERENCES `{self.ref_table}` ({ref_cols}) "
            f"ON DELETE {self.on_delete} ON UPDATE {self.on_update}"
        )


@dataclass
class TableSchema:
    """테이블 스키마 정보"""
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)
    indexes: List[IndexInfo] = field(default_factory=list)
    foreign_keys: List[ForeignKeyInfo] = field(default_factory=list)
    engine: str = "InnoDB"
    charset: str = "utf8mb4"
    collation: str = "utf8mb4_general_ci"
    row_count: int = 0

    def get_column(self, name: str) -> Optional[ColumnInfo]:
        """이름으로 컬럼 조회"""
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col
        return None

    def get_index(self, name: str) -> Optional[IndexInfo]:
        """이름으로 인덱스 조회"""
        for idx in self.indexes:
            if idx.name.lower() == name.lower():
                return idx
        return None

    def get_foreign_key(self, name: str) -> Optional[ForeignKeyInfo]:
        """이름으로 FK 조회"""
        for fk in self.foreign_keys:
            if fk.name.lower() == name.lower():
                return fk
        return None


@dataclass
class ColumnDiff:
    """컬럼 차이"""
    column_name: str
    diff_type: DiffType
    source_info: Optional[ColumnInfo] = None
    target_info: Optional[ColumnInfo] = None
    differences: List[str] = field(default_factory=list)


@dataclass
class IndexDiff:
    """인덱스 차이"""
    index_name: str
    diff_type: DiffType
    source_info: Optional[IndexInfo] = None
    target_info: Optional[IndexInfo] = None
    differences: List[str] = field(default_factory=list)


@dataclass
class ForeignKeyDiff:
    """FK 차이"""
    fk_name: str
    diff_type: DiffType
    source_info: Optional[ForeignKeyInfo] = None
    target_info: Optional[ForeignKeyInfo] = None
    differences: List[str] = field(default_factory=list)


@dataclass
class TableDiff:
    """테이블 차이"""
    table_name: str
    diff_type: DiffType
    source_schema: Optional[TableSchema] = None
    target_schema: Optional[TableSchema] = None
    column_diffs: List[ColumnDiff] = field(default_factory=list)
    index_diffs: List[IndexDiff] = field(default_factory=list)
    fk_diffs: List[ForeignKeyDiff] = field(default_factory=list)
    row_count_source: int = 0
    row_count_target: int = 0

    def has_differences(self) -> bool:
        """차이가 있는지 확인"""
        if self.diff_type in [DiffType.ADDED, DiffType.REMOVED]:
            return True
        return any(d.diff_type != DiffType.UNCHANGED
                  for d in self.column_diffs + self.index_diffs + self.fk_diffs)


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
            success, result = self.connector.execute_query(query, (schema,))
            if success:
                for row in result:
                    table_name = row[0] if isinstance(row, tuple) else row['TABLE_NAME']
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
            success, result = self.connector.execute_query(query, (schema, table))
            if success:
                for row in result:
                    if isinstance(row, tuple):
                        col = ColumnInfo(
                            name=row[0],
                            data_type=row[1],
                            nullable=(row[2] == 'YES'),
                            default=row[3],
                            extra=row[4] or '',
                            key=row[5] or '',
                            charset=row[6] or '',
                            collation=row[7] or ''
                        )
                    else:
                        col = ColumnInfo(
                            name=row['COLUMN_NAME'],
                            data_type=row['COLUMN_TYPE'],
                            nullable=(row['IS_NULLABLE'] == 'YES'),
                            default=row['COLUMN_DEFAULT'],
                            extra=row['EXTRA'] or '',
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
            success, result = self.connector.execute_query(query, (schema, table))
            if success:
                for row in result:
                    if isinstance(row, tuple):
                        idx_name, col_name, non_unique, idx_type = row
                    else:
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
            success, result = self.connector.execute_query(query, (schema, table))
            if success:
                for row in result:
                    if isinstance(row, tuple):
                        fk_name, col, ref_table, ref_col, on_del, on_upd = row
                    else:
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
            success, result = self.connector.execute_query(query, (schema, table))
            if success and result:
                row = result[0]
                if isinstance(row, tuple):
                    engine, collation = row
                else:
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
        query = f"SELECT COUNT(*) FROM `{schema}`.`{table}`"
        try:
            success, result = self.connector.execute_query(query)
            if success and result:
                row = result[0]
                return row[0] if isinstance(row, tuple) else row['COUNT(*)']
        except Exception:
            pass
        return 0


class SchemaComparator:
    """스키마 비교기"""

    def compare_tables(self, source: TableSchema, target: TableSchema) -> TableDiff:
        """두 테이블 스키마 비교

        Args:
            source: 소스 테이블 스키마
            target: 타겟 테이블 스키마

        Returns:
            TableDiff
        """
        diff = TableDiff(
            table_name=source.name,
            diff_type=DiffType.UNCHANGED,
            source_schema=source,
            target_schema=target,
            row_count_source=source.row_count,
            row_count_target=target.row_count
        )

        # 컬럼 비교
        diff.column_diffs = self._compare_columns(source.columns, target.columns)

        # 인덱스 비교
        diff.index_diffs = self._compare_indexes(source.indexes, target.indexes)

        # FK 비교
        diff.fk_diffs = self._compare_foreign_keys(source.foreign_keys, target.foreign_keys)

        # 전체 상태 결정
        if diff.has_differences():
            diff.diff_type = DiffType.MODIFIED

        return diff

    def compare_schemas(
        self,
        source_tables: Dict[str, TableSchema],
        target_tables: Dict[str, TableSchema]
    ) -> List[TableDiff]:
        """두 스키마 전체 비교

        Args:
            source_tables: 소스 테이블 딕셔너리
            target_tables: 타겟 테이블 딕셔너리

        Returns:
            TableDiff 목록
        """
        diffs = []

        all_tables = set(source_tables.keys()) | set(target_tables.keys())

        for table_name in sorted(all_tables):
            source = source_tables.get(table_name)
            target = target_tables.get(table_name)

            if source and not target:
                # 소스에만 있음 (타겟에 추가 필요)
                diff = TableDiff(
                    table_name=table_name,
                    diff_type=DiffType.ADDED,
                    source_schema=source,
                    row_count_source=source.row_count
                )
            elif target and not source:
                # 타겟에만 있음 (삭제 필요)
                diff = TableDiff(
                    table_name=table_name,
                    diff_type=DiffType.REMOVED,
                    target_schema=target,
                    row_count_target=target.row_count
                )
            else:
                # 둘 다 있음 (상세 비교)
                diff = self.compare_tables(source, target)

            diffs.append(diff)

        return diffs

    def _compare_columns(
        self,
        source_cols: List[ColumnInfo],
        target_cols: List[ColumnInfo]
    ) -> List[ColumnDiff]:
        """컬럼 비교"""
        diffs = []

        source_map = {c.name.lower(): c for c in source_cols}
        target_map = {c.name.lower(): c for c in target_cols}

        all_cols = set(source_map.keys()) | set(target_map.keys())

        for col_name in sorted(all_cols):
            src = source_map.get(col_name)
            tgt = target_map.get(col_name)

            if src and not tgt:
                diffs.append(ColumnDiff(
                    column_name=src.name,
                    diff_type=DiffType.ADDED,
                    source_info=src
                ))
            elif tgt and not src:
                diffs.append(ColumnDiff(
                    column_name=tgt.name,
                    diff_type=DiffType.REMOVED,
                    target_info=tgt
                ))
            else:
                # 상세 비교
                differences = []

                if src.data_type.lower() != tgt.data_type.lower():
                    differences.append(f"타입: {src.data_type} → {tgt.data_type}")

                if src.nullable != tgt.nullable:
                    src_null = "NULL" if src.nullable else "NOT NULL"
                    tgt_null = "NULL" if tgt.nullable else "NOT NULL"
                    differences.append(f"Nullable: {src_null} → {tgt_null}")

                if src.default != tgt.default:
                    differences.append(f"Default: {src.default} → {tgt.default}")

                if src.extra.lower() != tgt.extra.lower():
                    differences.append(f"Extra: {src.extra} → {tgt.extra}")

                if differences:
                    diffs.append(ColumnDiff(
                        column_name=src.name,
                        diff_type=DiffType.MODIFIED,
                        source_info=src,
                        target_info=tgt,
                        differences=differences
                    ))
                else:
                    diffs.append(ColumnDiff(
                        column_name=src.name,
                        diff_type=DiffType.UNCHANGED,
                        source_info=src,
                        target_info=tgt
                    ))

        return diffs

    def _compare_indexes(
        self,
        source_idx: List[IndexInfo],
        target_idx: List[IndexInfo]
    ) -> List[IndexDiff]:
        """인덱스 비교"""
        diffs = []

        source_map = {i.name.lower(): i for i in source_idx}
        target_map = {i.name.lower(): i for i in target_idx}

        all_idx = set(source_map.keys()) | set(target_map.keys())

        for idx_name in sorted(all_idx):
            src = source_map.get(idx_name)
            tgt = target_map.get(idx_name)

            if src and not tgt:
                diffs.append(IndexDiff(
                    index_name=src.name,
                    diff_type=DiffType.ADDED,
                    source_info=src
                ))
            elif tgt and not src:
                diffs.append(IndexDiff(
                    index_name=tgt.name,
                    diff_type=DiffType.REMOVED,
                    target_info=tgt
                ))
            else:
                differences = []

                if src.columns != tgt.columns:
                    differences.append(f"컬럼: {src.columns} → {tgt.columns}")

                if src.unique != tgt.unique:
                    differences.append(f"Unique: {src.unique} → {tgt.unique}")

                if differences:
                    diffs.append(IndexDiff(
                        index_name=src.name,
                        diff_type=DiffType.MODIFIED,
                        source_info=src,
                        target_info=tgt,
                        differences=differences
                    ))
                else:
                    diffs.append(IndexDiff(
                        index_name=src.name,
                        diff_type=DiffType.UNCHANGED,
                        source_info=src,
                        target_info=tgt
                    ))

        return diffs

    def _compare_foreign_keys(
        self,
        source_fks: List[ForeignKeyInfo],
        target_fks: List[ForeignKeyInfo]
    ) -> List[ForeignKeyDiff]:
        """외래 키 비교"""
        diffs = []

        source_map = {f.name.lower(): f for f in source_fks}
        target_map = {f.name.lower(): f for f in target_fks}

        all_fks = set(source_map.keys()) | set(target_map.keys())

        for fk_name in sorted(all_fks):
            src = source_map.get(fk_name)
            tgt = target_map.get(fk_name)

            if src and not tgt:
                diffs.append(ForeignKeyDiff(
                    fk_name=src.name,
                    diff_type=DiffType.ADDED,
                    source_info=src
                ))
            elif tgt and not src:
                diffs.append(ForeignKeyDiff(
                    fk_name=tgt.name,
                    diff_type=DiffType.REMOVED,
                    target_info=tgt
                ))
            else:
                differences = []

                if src.ref_table != tgt.ref_table:
                    differences.append(f"참조 테이블: {src.ref_table} → {tgt.ref_table}")

                if src.columns != tgt.columns:
                    differences.append(f"컬럼: {src.columns} → {tgt.columns}")

                if src.on_delete != tgt.on_delete:
                    differences.append(f"ON DELETE: {src.on_delete} → {tgt.on_delete}")

                if src.on_update != tgt.on_update:
                    differences.append(f"ON UPDATE: {src.on_update} → {tgt.on_update}")

                if differences:
                    diffs.append(ForeignKeyDiff(
                        fk_name=src.name,
                        diff_type=DiffType.MODIFIED,
                        source_info=src,
                        target_info=tgt,
                        differences=differences
                    ))
                else:
                    diffs.append(ForeignKeyDiff(
                        fk_name=src.name,
                        diff_type=DiffType.UNCHANGED,
                        source_info=src,
                        target_info=tgt
                    ))

        return diffs


class SyncScriptGenerator:
    """동기화 SQL 스크립트 생성기"""

    def generate_sync_script(self, diffs: List[TableDiff], target_schema: str) -> str:
        """전체 동기화 스크립트 생성

        Args:
            diffs: TableDiff 목록
            target_schema: 타겟 스키마 이름

        Returns:
            SQL 스크립트
        """
        lines = [
            "-- =======================================================",
            f"-- 스키마 동기화 스크립트",
            f"-- 타겟: {target_schema}",
            "-- 주의: 실행 전 반드시 백업을 수행하세요!",
            "-- =======================================================",
            "",
            "SET FOREIGN_KEY_CHECKS = 0;",
            ""
        ]

        # 1. FK 삭제 (의존성 해제)
        fk_drops = []
        for diff in diffs:
            if diff.diff_type == DiffType.REMOVED:
                if diff.target_schema:
                    for fk in diff.target_schema.foreign_keys:
                        fk_drops.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"DROP FOREIGN KEY `{fk.name}`;"
                        )
            elif diff.diff_type == DiffType.MODIFIED:
                for fk_diff in diff.fk_diffs:
                    if fk_diff.diff_type in [DiffType.REMOVED, DiffType.MODIFIED]:
                        fk_drops.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"DROP FOREIGN KEY `{fk_diff.fk_name}`;"
                        )

        if fk_drops:
            lines.append("-- FK 삭제")
            lines.extend(fk_drops)
            lines.append("")

        # 2. 테이블 삭제 (소스에 없는 테이블)
        table_drops = []
        for diff in diffs:
            if diff.diff_type == DiffType.REMOVED:
                table_drops.append(f"DROP TABLE IF EXISTS `{target_schema}`.`{diff.table_name}`;")

        if table_drops:
            lines.append("-- 테이블 삭제")
            lines.extend(table_drops)
            lines.append("")

        # 3. 테이블 생성 (타겟에 없는 테이블)
        table_creates = []
        for diff in diffs:
            if diff.diff_type == DiffType.ADDED and diff.source_schema:
                table_creates.append(
                    self._generate_create_table(target_schema, diff.source_schema)
                )

        if table_creates:
            lines.append("-- 테이블 생성")
            lines.extend(table_creates)
            lines.append("")

        # 4. 컬럼/인덱스 변경
        alter_statements = []
        for diff in diffs:
            if diff.diff_type == DiffType.MODIFIED:
                # 컬럼 변경
                for col_diff in diff.column_diffs:
                    if col_diff.diff_type == DiffType.ADDED and col_diff.source_info:
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"ADD COLUMN {col_diff.source_info.to_sql_definition()};"
                        )
                    elif col_diff.diff_type == DiffType.REMOVED:
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"DROP COLUMN `{col_diff.column_name}`;"
                        )
                    elif col_diff.diff_type == DiffType.MODIFIED and col_diff.source_info:
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"MODIFY COLUMN {col_diff.source_info.to_sql_definition()};"
                        )

                # 인덱스 변경
                for idx_diff in diff.index_diffs:
                    if idx_diff.index_name == 'PRIMARY':
                        continue  # PRIMARY KEY는 별도 처리 필요

                    if idx_diff.diff_type == DiffType.ADDED and idx_diff.source_info:
                        idx_sql = idx_diff.source_info.to_sql_definition(diff.table_name)
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` ADD {idx_sql};"
                        )
                    elif idx_diff.diff_type == DiffType.REMOVED:
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"DROP INDEX `{idx_diff.index_name}`;"
                        )
                    elif idx_diff.diff_type == DiffType.MODIFIED and idx_diff.source_info:
                        # 인덱스 수정 = 삭제 후 재생성
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"DROP INDEX `{idx_diff.index_name}`;"
                        )
                        idx_sql = idx_diff.source_info.to_sql_definition(diff.table_name)
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` ADD {idx_sql};"
                        )

        if alter_statements:
            lines.append("-- 컬럼/인덱스 변경")
            lines.extend(alter_statements)
            lines.append("")

        # 5. FK 추가 (의존성 복원)
        fk_adds = []
        for diff in diffs:
            if diff.diff_type == DiffType.ADDED and diff.source_schema:
                for fk in diff.source_schema.foreign_keys:
                    fk_adds.append(
                        f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                        f"ADD {fk.to_sql_definition()};"
                    )
            elif diff.diff_type == DiffType.MODIFIED:
                for fk_diff in diff.fk_diffs:
                    if fk_diff.diff_type in [DiffType.ADDED, DiffType.MODIFIED]:
                        if fk_diff.source_info:
                            fk_adds.append(
                                f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                                f"ADD {fk_diff.source_info.to_sql_definition()};"
                            )

        if fk_adds:
            lines.append("-- FK 추가")
            lines.extend(fk_adds)
            lines.append("")

        lines.append("SET FOREIGN_KEY_CHECKS = 1;")
        lines.append("")
        lines.append("-- 스크립트 끝")

        return "\n".join(lines)

    def _generate_create_table(self, schema: str, table: TableSchema) -> str:
        """CREATE TABLE 문 생성"""
        lines = [f"CREATE TABLE `{schema}`.`{table.name}` ("]

        # 컬럼
        col_defs = [f"    {col.to_sql_definition()}" for col in table.columns]

        # PRIMARY KEY
        pk_cols = [col.name for col in table.columns if col.key == 'PRI']
        if pk_cols:
            pk_def = ", ".join(f"`{c}`" for c in pk_cols)
            col_defs.append(f"    PRIMARY KEY ({pk_def})")

        # 인덱스 (PRIMARY 제외)
        for idx in table.indexes:
            if idx.name != 'PRIMARY':
                col_defs.append(f"    {idx.to_sql_definition(table.name)}")

        # FK
        for fk in table.foreign_keys:
            col_defs.append(f"    {fk.to_sql_definition()}")

        lines.append(",\n".join(col_defs))
        lines.append(f") ENGINE={table.engine} DEFAULT CHARSET={table.charset};")

        return "\n".join(lines)
