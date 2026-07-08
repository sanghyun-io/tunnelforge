"""
스키마 동기화 SQL 스크립트 생성기
"""
from typing import List

from src.core.schema_diff_models import DiffType, TableDiff, TableSchema


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
            "-- 스키마 동기화 스크립트",
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
                    elif fk_diff.diff_type == DiffType.RENAMED and fk_diff.old_name:
                        # FK rename = DROP old + ADD new (MySQL에 RENAME FK 없음)
                        fk_drops.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"DROP FOREIGN KEY `{fk_diff.old_name}`; "
                            f"-- renamed → {fk_diff.fk_name}"
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
                    elif idx_diff.diff_type == DiffType.RENAMED and idx_diff.old_name:
                        # MySQL 5.7+ RENAME INDEX
                        alter_statements.append(
                            f"ALTER TABLE `{target_schema}`.`{diff.table_name}` "
                            f"RENAME INDEX `{idx_diff.old_name}` TO `{idx_diff.index_name}`;"
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
                    if fk_diff.diff_type in [DiffType.ADDED, DiffType.MODIFIED, DiffType.RENAMED]:
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
        # 컬럼 ordinal 스캔이 아닌 PRIMARY 인덱스 메타데이터(SEQ_IN_INDEX 순서)로
        # 생성해야 복합 PK의 실제 컬럼 순서가 보존된다.
        primary_idx = table.get_index("PRIMARY")
        if primary_idx and primary_idx.columns:
            col_defs.append(f"    {primary_idx.to_sql_definition(table.name)}")

        # 인덱스 (PRIMARY 제외)
        for idx in table.indexes:
            if idx.name.upper() != "PRIMARY":
                col_defs.append(f"    {idx.to_sql_definition(table.name)}")

        # FK
        for fk in table.foreign_keys:
            col_defs.append(f"    {fk.to_sql_definition()}")

        lines.append(",\n".join(col_defs))
        lines.append(f") ENGINE={table.engine} DEFAULT CHARSET={table.charset};")

        return "\n".join(lines)
