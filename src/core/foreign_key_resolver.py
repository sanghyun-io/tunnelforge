"""Foreign-key dependency analysis and orphan-record detection for MySQL schemas."""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.core.db_connector import MySQLConnector


@dataclass
class OrphanRecordInfo:
    """Foreign-key orphan record summary."""

    table: str
    column: str
    referenced_table: str
    referenced_column: str
    orphan_count: int
    sample_values: List[str]
    query: str


class ForeignKeyResolver:
    """Foreign-key dependency analysis used before partial table dumps."""

    def __init__(self, connector: MySQLConnector):
        self.connector = connector

    def get_all_dependencies(self, schema: str) -> Dict[str, Set[str]]:
        query = """
        SELECT TABLE_NAME, REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        """
        deps: Dict[str, Set[str]] = {}
        for row in self.connector.execute(query, (schema,)):
            table = row["TABLE_NAME"]
            ref_table = row["REFERENCED_TABLE_NAME"]
            if table != ref_table:
                deps.setdefault(table, set()).add(ref_table)
        return deps

    def get_fk_details(self, schema: str) -> List[Dict]:
        query = """
        SELECT
            TABLE_NAME,
            COLUMN_NAME,
            REFERENCED_TABLE_NAME,
            REFERENCED_COLUMN_NAME,
            CONSTRAINT_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY TABLE_NAME, COLUMN_NAME
        """
        rows = self.connector.execute(query, (schema,))
        return [
            {
                "table": row["TABLE_NAME"],
                "column": row["COLUMN_NAME"],
                "referenced_table": row["REFERENCED_TABLE_NAME"],
                "referenced_column": row["REFERENCED_COLUMN_NAME"],
                "constraint_name": row["CONSTRAINT_NAME"],
            }
            for row in rows
        ]

    def _orphan_join_where(
        self,
        schema: str,
        table: str,
        column: str,
        ref_table: str,
        ref_column: str,
    ) -> str:
        """Shared FROM/LEFT JOIN/WHERE fragment for orphan-record queries."""
        return (
            f"FROM `{schema}`.`{table}` c "
            f"LEFT JOIN `{schema}`.`{ref_table}` p ON c.`{column}` = p.`{ref_column}` "
            f"WHERE c.`{column}` IS NOT NULL AND p.`{ref_column}` IS NULL"
        )

    def generate_orphan_query(
        self,
        schema: str,
        table: str,
        column: str,
        ref_table: str,
        ref_column: str,
    ) -> str:
        join_where = self._orphan_join_where(schema, table, column, ref_table, ref_column)
        return f"SELECT c.*\n{join_where}"

    def find_orphan_records(
        self,
        schema: str,
        tables: Optional[List[str]] = None,
        sample_limit: int = 5,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> List[OrphanRecordInfo]:
        fk_details = self.get_fk_details(schema)
        if tables:
            table_set = set(tables)
            fk_details = [fk for fk in fk_details if fk["table"] in table_set]

        results = []
        for index, fk in enumerate(fk_details, 1):
            table = fk["table"]
            column = fk["column"]
            ref_table = fk["referenced_table"]
            ref_column = fk["referenced_column"]

            if progress_callback:
                progress_callback(f"검사 중... ({index}/{len(fk_details)}) {table}.{column}")

            join_where = self._orphan_join_where(schema, table, column, ref_table, ref_column)

            count_query = f"SELECT COUNT(*) as cnt {join_where}"
            count_result = self.connector.execute(count_query)
            orphan_count = count_result[0]["cnt"] if count_result else 0
            if orphan_count <= 0:
                continue

            sample_query = f"SELECT DISTINCT c.`{column}` as orphan_value {join_where} LIMIT {sample_limit}"
            sample_values = [
                str(row["orphan_value"])
                for row in self.connector.execute(sample_query)
            ]
            results.append(
                OrphanRecordInfo(
                    table=table,
                    column=column,
                    referenced_table=ref_table,
                    referenced_column=ref_column,
                    orphan_count=orphan_count,
                    sample_values=sample_values,
                    query=self.generate_orphan_query(schema, table, column, ref_table, ref_column),
                )
            )
        return results

    def export_orphan_report(
        self,
        schema: str,
        output_path: str,
        tables: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str, int]:
        try:
            orphans = self.find_orphan_records(schema, tables, progress_callback=progress_callback)
            with open(output_path, "w", encoding="utf-8") as file:
                file.write("# 고아 레코드 분석 보고서\n")
                file.write(f"# 스키마: {schema}\n")
                file.write(f"# 생성일시: {datetime.now().isoformat()}\n")
                file.write(f"# 발견된 고아 관계: {len(orphans)}건\n")
                file.write("=" * 80 + "\n\n")
                if not orphans:
                    file.write("고아 레코드가 발견되지 않았습니다.\n")
                else:
                    total_orphans = sum(item.orphan_count for item in orphans)
                    file.write(f"총 {total_orphans:,}개의 고아 레코드 발견\n\n")
                    for index, item in enumerate(orphans, 1):
                        file.write(
                            f"## [{index}] {item.table}.{item.column} -> "
                            f"{item.referenced_table}.{item.referenced_column}\n"
                        )
                        file.write(f"   고아 레코드 수: {item.orphan_count:,}건\n")
                        file.write(f"   샘플 값: {', '.join(item.sample_values)}\n")
                        file.write("\n   조회 쿼리:\n")
                        file.write("   ```sql\n")
                        for line in item.query.split("\n"):
                            file.write(f"   {line}\n")
                        file.write("   ```\n\n")
                        file.write("-" * 80 + "\n\n")
            return True, f"보고서 저장 완료: {output_path}", len(orphans)
        except Exception as exc:
            return False, f"보고서 저장 실패: {exc}", 0

    def get_all_orphan_queries(self, schema: str, tables: Optional[List[str]] = None) -> str:
        fk_details = self.get_fk_details(schema)
        if tables:
            table_set = set(tables)
            fk_details = [fk for fk in fk_details if fk["table"] in table_set]

        queries = [
            f"-- 고아 레코드 조회 쿼리 (스키마: {schema})",
            f"-- 생성일시: {datetime.now().isoformat()}",
            f"-- FK 관계 수: {len(fk_details)}개",
            "",
        ]
        for index, fk in enumerate(fk_details, 1):
            table = fk["table"]
            column = fk["column"]
            ref_table = fk["referenced_table"]
            ref_column = fk["referenced_column"]
            queries.append(f"-- [{index}] {table}.{column} -> {ref_table}.{ref_column}")
            join_where = self._orphan_join_where(schema, table, column, ref_table, ref_column)
            queries.append(
                f"SELECT '{table}.{column}' AS fk_relation, COUNT(*) AS orphan_count {join_where};\n"
            )
        return "\n".join(queries)
