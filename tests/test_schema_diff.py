"""Rust schema.compare 결과를 UI 모델로 바꾸는 parse_compare_result 테스트.

비교·심각도·동기화 SQL 규칙 자체는 migration_core/src/schema_compare.rs 단위 테스트와
migration_core/tests/live_schema_compare.rs 가 검증한다.
"""
from src.core.schema_diff import (
    DiffSeverity, DiffType, IndexInfo, ForeignKeyInfo, parse_compare_result,
)

RESULT = {
    "event": "result", "command": "schema.compare", "success": True,
    "source_version": "8.4.3", "target_version": "8.0.46", "row_counts_exact": False,
    "summary": {"critical": 2, "warning": 1, "info": 1},
    "sync_sql": "-- 스키마 동기화 스크립트\nSET FOREIGN_KEY_CHECKS = 1;",
    "tables": [
        {
            "name": "new_table", "diff_type": "added", "severity": "critical",
            "row_count_source": 12, "row_count_target": 0,
            "source": {"name": "new_table", "columns": [{"name": "id", "column_type": "int", "nullable": False}],
                       "indexes": [], "foreign_keys": [], "engine": "InnoDB", "collation": "utf8mb4_bin", "row_count": 12},
            "target": None, "columns": [], "indexes": [], "foreign_keys": [],
        },
        {
            "name": "child", "diff_type": "modified", "severity": None,
            "row_count_source": 5, "row_count_target": 4,
            "source": {"name": "child", "columns": [{"name": "name", "column_type": "varchar(50)"}]},
            "target": {"name": "child", "columns": [{"name": "name", "column_type": "varchar(20)"}]},
            "columns": [{
                "name": "name", "diff_type": "modified", "severity": "warning",
                "changes": [{"field": "type", "from": "varchar(50)", "to": "varchar(20)", "text": "타입: varchar(50) → varchar(20)"}],
                "source": {"name": "name", "column_type": "varchar(50)", "nullable": True, "default": None},
                "target": {"name": "name", "column_type": "varchar(20)", "nullable": True, "default": None},
            }],
            "indexes": [{
                "name": "ix_name", "diff_type": "renamed", "severity": "info", "old_name": "ix_old",
                "changes": [{"field": "name", "from": "ix_old", "to": "ix_name", "text": "이름 변경: ix_old → ix_name"}],
                "source": {"name": "ix_name", "parts": [{"column": "name", "sub_part": 10}], "unique": False, "index_type": "BTREE"},
                "target": {"name": "ix_old", "parts": [{"column": "name", "sub_part": 10}], "unique": False, "index_type": "BTREE"},
            }],
            "foreign_keys": [{
                "name": "fk_parent", "diff_type": "removed", "severity": "critical", "old_name": None, "changes": [],
                "source": None,
                "target": {"name": "fk_parent", "columns": ["parent_id"], "ref_table": "parent", "ref_columns": ["id"],
                           "on_delete": "CASCADE", "on_update": "RESTRICT"},
            }],
        },
    ],
}


def test_parse_compare_result_builds_ui_models():
    result = parse_compare_result(RESULT)

    assert (result.summary.critical, result.summary.warning, result.summary.info) == (2, 1, 1)
    assert result.summary.has_critical is True
    assert result.version_ctx.source_version_str == "8.4.3"
    assert result.sync_sql.startswith("-- 스키마 동기화 스크립트")
    assert result.row_counts_exact is False

    added, child = result.diffs
    assert (added.table_name, added.diff_type, added.severity) == ("new_table", DiffType.ADDED, DiffSeverity.CRITICAL)
    assert added.source_schema.columns[0].data_type == "int" and added.target_schema is None
    assert (child.row_count_source, child.row_count_target) == (5, 4)

    column = child.column_diffs[0]
    assert column.differences == ["타입: varchar(50) → varchar(20)"]
    assert column.severity == DiffSeverity.WARNING and column.source_info.data_type == "varchar(50)"

    index = child.index_diffs[0]
    assert (index.diff_type, index.old_name, index.severity) == (DiffType.RENAMED, "ix_old", DiffSeverity.INFO)
    assert str(index.source_info) == "INDEX ix_name (name(10)) USING BTREE"

    fk = child.fk_diffs[0]
    assert fk.diff_type == DiffType.REMOVED and fk.source_info is None
    assert str(fk.target_info) == "CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES parent (id) ON DELETE CASCADE ON UPDATE RESTRICT"


def test_info_display_strings():
    assert str(IndexInfo("PRIMARY", ["b", "a"], True)) == "PRIMARY KEY (b, a)"
    assert str(IndexInfo("uq", ["a"], True, "BTREE")) == "UNIQUE INDEX uq (a) USING BTREE"
    assert "REFERENCES p (id)" in str(ForeignKeyInfo("fk", ["x"], "p", ["id"]))


def test_parse_tolerates_missing_optional_fields():
    result = parse_compare_result({"tables": [{"name": "t", "diff_type": "unchanged"}]})
    assert result.diffs[0].diff_type == DiffType.UNCHANGED
    assert result.diffs[0].column_diffs == [] and result.summary.has_critical is False
    assert result.sync_sql == ""
