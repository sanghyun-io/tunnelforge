from datetime import datetime

from src.core.migration_analyzer import AnalysisResult, OrphanRecord
from src.ui.dialogs.migration_result_store import MigrationResultStore


def _analysis_result() -> AnalysisResult:
    return AnalysisResult(
        schema="app",
        analyzed_at="2026-07-09T12:34:56",
        total_tables=2,
        total_fk_relations=1,
        orphan_records=[
            OrphanRecord(
                child_table="orders",
                child_column="user_id",
                parent_table="users",
                parent_column="id",
                orphan_count=3,
                sample_values=[10, 20],
            )
        ],
        fk_tree={"users": ["orders"]},
    )


def test_migration_result_store_writes_and_reads_analysis_result(tmp_path):
    store = MigrationResultStore(base_dir=tmp_path)
    path = tmp_path / "analysis.json"

    store.write(_analysis_result(), path)
    loaded = store.read(path)

    assert loaded.schema == "app"
    assert loaded.total_tables == 2
    assert loaded.orphan_records[0].child_table == "orders"
    assert loaded.fk_tree == {"users": ["orders"]}


def test_migration_result_store_auto_save_uses_analysis_directory(tmp_path):
    store = MigrationResultStore(base_dir=tmp_path)

    path = store.auto_save(_analysis_result())

    assert path.parent == tmp_path
    assert path.name.startswith("app_")
    assert store.read(path).schema == "app"


def test_migration_result_store_exports_orphan_queries(tmp_path):
    store = MigrationResultStore(base_dir=tmp_path)
    result = _analysis_result()
    path = tmp_path / "orphans.sql"

    store.export_orphan_queries(
        schema=result.schema,
        orphans=result.orphan_records,
        path=path,
        query_builder=lambda orphan, schema: f"SELECT * FROM `{schema}`.`{orphan.child_table}`;",
        generated_at=datetime(2026, 7, 9, 12, 0, 0),
    )

    content = path.read_text(encoding="utf-8")
    assert "-- 스키마: app" in content
    assert "-- FK 관계 수: 1개" in content
    assert "-- 총 고아 레코드: 3개" in content
    assert "SELECT * FROM `app`.`orders`;" in content
