"""Migration analysis result persistence helpers."""
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from src.core.migration_analyzer import AnalysisResult, OrphanRecord
from src.core.path_safety import safe_child_file, safe_filename_component
from src.core.platform_paths import analysis_dir

PathLike = Union[str, Path]


class MigrationResultStore:
    """Pure file I/O for migration analysis result persistence."""

    def __init__(self, base_dir: Optional[PathLike] = None):
        self._base_dir = Path(base_dir) if base_dir is not None else None

    def analysis_dir(self) -> Path:
        base_dir = self._base_dir or analysis_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    @staticmethod
    def default_name(schema: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_schema = safe_filename_component(schema, "schema")
        return f"{safe_schema}_{timestamp}.json"

    def auto_save(self, result: AnalysisResult) -> Path:
        base_dir = self.analysis_dir()
        path = safe_child_file(base_dir, self.default_name(result.schema), "schema_analysis.json")
        self.write(result, path)
        return path

    @staticmethod
    def write(result: AnalysisResult, path: PathLike) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as file:
            json.dump(result.to_dict(), file, ensure_ascii=False, indent=2, default=str)
        return target

    @staticmethod
    def read(path: PathLike) -> AnalysisResult:
        with Path(path).open("r", encoding="utf-8") as file:
            data = json.load(file)
        return AnalysisResult.from_dict(data)

    @staticmethod
    def export_orphan_queries(
        schema: str,
        orphans: Iterable[OrphanRecord],
        path: PathLike,
        query_builder: Callable[[OrphanRecord, str], str],
        generated_at: Optional[datetime] = None,
    ) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        orphan_list = list(orphans)
        total_count = sum(orphan.orphan_count for orphan in orphan_list)
        generated_at = generated_at or datetime.now()

        with target.open("w", encoding="utf-8") as file:
            file.write("-- ═══════════════════════════════════════════════════════════════\n")
            file.write("-- 고아 레코드 조회 쿼리\n")
            file.write(f"-- 스키마: {schema}\n")
            file.write(f"-- 생성일시: {generated_at.isoformat()}\n")
            file.write(f"-- FK 관계 수: {len(orphan_list)}개\n")
            file.write(f"-- 총 고아 레코드: {total_count:,}개\n")
            file.write("-- ═══════════════════════════════════════════════════════════════\n\n")

            for index, orphan in enumerate(orphan_list, 1):
                file.write(f"-- [{index}/{len(orphan_list)}] {orphan.child_table}.{orphan.child_column}\n")
                file.write(query_builder(orphan, schema))
                file.write("\n\n")

        return target
