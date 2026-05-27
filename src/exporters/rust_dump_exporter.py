"""Rust DB Core backed dump export/import helpers."""
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.core.db_connector import MySQLConnector
from src.core.db_core_service import (
    DbCoreFacade,
    DbCoreServiceError,
    DbEndpoint,
    get_shared_db_core_facade,
)
from src.core.logger import get_logger

logger = get_logger("rust_dump_exporter")

DEFAULT_DUMP_COMPRESSION = "zstd"


def _safe_dump_child_dir(dump_dir: str, table_path: str) -> Optional[Path]:
    base_path = Path(dump_dir).resolve()
    table_path_obj = Path(table_path)
    if (
        not table_path
        or table_path_obj.is_absolute()
        or any(part == ".." for part in table_path_obj.parts)
    ):
        return None
    child_path = (base_path / table_path_obj).resolve()
    try:
        if not child_path.is_relative_to(base_path):
            return None
    except ValueError:
        return None
    return child_path


def _safe_dump_child_file(dump_dir: str, path: Path) -> Optional[Path]:
    base_path = Path(dump_dir).resolve()
    file_path = path.resolve()
    try:
        if not file_path.is_relative_to(base_path):
            return None
    except ValueError:
        return None
    return file_path if file_path.is_file() else None


def _format_import_phase_message(event: dict) -> str:
    if event.get("strategy") == "insert_fallback":
        return (
            "MySQL local_infile 비활성화: 안전 INSERT fallback으로 진행합니다. "
            "에러는 아니지만 LOAD DATA LOCAL보다 느립니다."
        )
    return str(event.get("message") or event.get("phase") or "Rust DB Core 작업 중...")


@dataclass
class RustDumpConfig:
    """Connection settings for Rust DB Core dump operations."""

    host: str
    port: int
    user: str
    password: str
    schema: str = ""

    def get_uri(self) -> str:
        return f"{self.user}:{self.password}@{self.host}:{self.port}"

    def get_masked_uri(self) -> str:
        return f"{self.user}:****@{self.host}:{self.port}"


class RustDumpChecker:
    """Checks whether the Rust DB Core dump protocol is available."""

    @staticmethod
    def check_installation() -> Tuple[bool, str, Optional[str]]:
        try:
            result = DbCoreFacade().hello()
            service = str(result.get("service", "tunnelforge-core"))
            protocol = str(result.get("protocol_version", ""))
            capabilities = result.get("capabilities", [])
            if "dump.run" not in capabilities or "dump.import" not in capabilities:
                return False, "Rust DB Core에 dump 기능이 없습니다.", None
            version = f"{service} protocol {protocol}".strip()
            return True, version, version
        except FileNotFoundError:
            return False, "Rust DB Core 실행 파일을 찾을 수 없습니다.", None
        except TimeoutError:
            return False, "Rust DB Core 확인 시간 초과", None
        except Exception as exc:
            return False, f"오류: {exc}", None

    @staticmethod
    def get_install_guide() -> str:
        return """
Rust DB Core 준비 방법:

[Windows]
1. migration_core 빌드: cargo build --manifest-path migration_core/Cargo.toml --release
2. tunnel-manager.spec 또는 installer 빌드에 tunnelforge-core.exe 포함 여부 확인

[macOS/Linux]
cargo build --manifest-path migration_core/Cargo.toml --release

배포 패키지에는 tunnelforge-core 실행 파일이 앱과 함께 포함되어야 합니다.
"""


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

    def generate_orphan_query(
        self,
        schema: str,
        table: str,
        column: str,
        ref_table: str,
        ref_column: str,
    ) -> str:
        return f"""SELECT c.*
FROM `{schema}`.`{table}` c
LEFT JOIN `{schema}`.`{ref_table}` p ON c.`{column}` = p.`{ref_column}`
WHERE c.`{column}` IS NOT NULL
  AND p.`{ref_column}` IS NULL"""

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

            count_query = f"""
            SELECT COUNT(*) as cnt
            FROM `{schema}`.`{table}` c
            LEFT JOIN `{schema}`.`{ref_table}` p ON c.`{column}` = p.`{ref_column}`
            WHERE c.`{column}` IS NOT NULL
              AND p.`{ref_column}` IS NULL
            """
            count_result = self.connector.execute(count_query)
            orphan_count = count_result[0]["cnt"] if count_result else 0
            if orphan_count <= 0:
                continue

            sample_query = f"""
            SELECT DISTINCT c.`{column}` as orphan_value
            FROM `{schema}`.`{table}` c
            LEFT JOIN `{schema}`.`{ref_table}` p ON c.`{column}` = p.`{ref_column}`
            WHERE c.`{column}` IS NOT NULL
              AND p.`{ref_column}` IS NULL
            LIMIT {sample_limit}
            """
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
            queries.append(
                f"""SELECT '{table}.{column}' AS fk_relation, COUNT(*) AS orphan_count
FROM `{schema}`.`{table}` c
LEFT JOIN `{schema}`.`{ref_table}` p ON c.`{column}` = p.`{ref_column}`
WHERE c.`{column}` IS NOT NULL AND p.`{ref_column}` IS NULL;
"""
            )
        return "\n".join(queries)

    def resolve_required_tables(
        self,
        selected_tables: List[str],
        schema: str,
    ) -> Tuple[List[str], List[str]]:
        all_deps = self.get_all_dependencies(schema)
        required = set(selected_tables)
        added = []

        changed = True
        while changed:
            changed = False
            for table in list(required):
                for parent in all_deps.get(table, set()):
                    if parent not in required:
                        required.add(parent)
                        added.append(parent)
                        changed = True
        return sorted(required), sorted(added)


class RustDumpExporter:
    """Rust DB Core backed dump exporter."""

    def __init__(self, config: RustDumpConfig, facade: Optional[DbCoreFacade] = None):
        self.config = config
        self._connector: Optional[MySQLConnector] = None
        self.facade = facade or get_shared_db_core_facade()

    def _get_connector(self) -> MySQLConnector:
        if self._connector is None:
            self._connector = MySQLConnector(
                self.config.host,
                self.config.port,
                self.config.user,
                self.config.password,
            )
            self._connector.connect()
        return self._connector

    def _cleanup(self) -> None:
        if self._connector:
            self._connector.disconnect()
            self._connector = None

    def _endpoint(self, schema: str) -> DbEndpoint:
        return DbEndpoint(
            engine="mysql",
            host=self.config.host,
            port=int(self.config.port),
            user=self.config.user,
            password=self.config.password,
            database=schema,
        )

    def _emit_core_event(
        self,
        event: Dict,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        emit_core_event(
            event,
            progress_callback,
            table_progress_callback,
            detail_callback,
            table_status_callback,
            raw_output_callback,
        )

    def _run_rust_dump(
        self,
        schema: str,
        output_dir: str,
        tables: Optional[List[str]],
        threads: int = 8,
        chunk_size: int = 50000,
        compression: str = DEFAULT_DUMP_COMPRESSION,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        payload = {
            "source": self._endpoint(schema).to_payload(),
            "output_dir": output_dir,
            "overwrite": True,
            "threads": max(1, int(threads)),
            "chunk_size": max(1000, int(chunk_size)),
            "data_format": "tsv",
            "compression": compression if compression in {"none", "zstd"} else DEFAULT_DUMP_COMPRESSION,
        }
        if tables:
            payload["tables"] = tables

        if progress_callback:
            progress_callback(f"Rust DB Core export 시작: {self.config.get_masked_uri()}/{schema}")

        result = self.facade.run_dump(
            payload,
            on_event=lambda event: self._emit_core_event(
                event,
                progress_callback,
                table_progress_callback,
                detail_callback,
                table_status_callback,
                raw_output_callback,
            ),
        )
        rows = int(result.get("rows_dumped") or 0)
        table_count = int(result.get("tables") or 0)
        return True, f"Rust DB Core export 완료: {table_count}개 테이블, {rows:,} rows"

    def export_full_schema(
        self,
        schema: str,
        output_dir: str,
        threads: int = 8,
        compression: str = DEFAULT_DUMP_COMPRESSION,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str]:
        try:
            success, message = self._run_rust_dump(
                schema=schema,
                output_dir=output_dir,
                tables=None,
                threads=threads,
                compression=compression,
                progress_callback=progress_callback,
                table_progress_callback=table_progress_callback,
                detail_callback=detail_callback,
                table_status_callback=table_status_callback,
                raw_output_callback=raw_output_callback,
            )
            if success:
                self._write_metadata(output_dir, schema, "full", None)
            return success, message
        except DbCoreServiceError as exc:
            return False, f"Rust DB Core export 오류: {exc}"
        except Exception as exc:
            return False, f"Export 오류: {exc}"

    def export_tables(
        self,
        schema: str,
        tables: List[str],
        output_dir: str,
        threads: int = 8,
        compression: str = DEFAULT_DUMP_COMPRESSION,
        include_fk_parents: bool = True,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str, List[str]]:
        try:
            final_tables = list(tables)
            added_tables: List[str] = []
            if include_fk_parents:
                if progress_callback:
                    progress_callback("FK 의존성 분석 중...")
                resolver = ForeignKeyResolver(self._get_connector())
                final_tables, added_tables = resolver.resolve_required_tables(tables, schema)

            success, message = self._run_rust_dump(
                schema=schema,
                output_dir=output_dir,
                tables=final_tables,
                threads=threads,
                compression=compression,
                progress_callback=progress_callback,
                table_progress_callback=table_progress_callback,
                detail_callback=detail_callback,
                table_status_callback=table_status_callback,
                raw_output_callback=raw_output_callback,
            )
            if success:
                self._write_metadata(output_dir, schema, "partial", final_tables, added_tables)
                return True, f"{len(final_tables)}개 테이블 Export 완료", final_tables
            return False, message, []
        except DbCoreServiceError as exc:
            return False, f"Rust DB Core export 오류: {exc}", []
        except Exception as exc:
            return False, f"Export 오류: {exc}", []
        finally:
            self._cleanup()

    def _write_metadata(
        self,
        output_dir: str,
        schema: str,
        export_type: str,
        tables: Optional[List[str]],
        added_tables: Optional[List[str]] = None,
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        metadata = {
            "export_time": datetime.now().isoformat(),
            "schema": schema,
            "type": export_type,
            "tables": tables,
            "added_fk_tables": added_tables or [],
            "source": f"{self.config.host}:{self.config.port}",
            "format": "tunnelforge-dump",
        }
        with open(os.path.join(output_dir, "_export_metadata.json"), "w", encoding="utf-8") as file:
            json.dump(metadata, file, indent=2, ensure_ascii=False)


class RustDumpImporter:
    """Rust DB Core backed dump importer."""

    def __init__(self, config: RustDumpConfig, facade: Optional[DbCoreFacade] = None):
        self.config = config
        self.facade = facade or get_shared_db_core_facade()

    def _endpoint(self, schema: str) -> DbEndpoint:
        return DbEndpoint(
            engine="mysql",
            host=self.config.host,
            port=int(self.config.port),
            user=self.config.user,
            password=self.config.password,
            database=schema,
        )

    def _analyze_dump_metadata(self, dump_dir: str) -> Optional[Dict]:
        manifest_path = Path(dump_dir) / "_tunnelforge_dump.json"
        if not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            chunk_counts = {}
            table_sizes = {}
            table_rows = {}
            total_bytes = 0
            total_rows = 0
            for table in manifest.get("tables", []):
                table_name = str(table.get("name", ""))
                if not table_name:
                    continue
                chunk_counts[table_name] = int(table.get("chunks") or 0)
                rows = int(table.get("rows") or 0)
                table_rows[table_name] = rows
                total_rows += rows
                table_dir = _safe_dump_child_dir(dump_dir, str(table.get("path", "")))
                if table_dir is None:
                    return None
                size = 0
                for path in table_dir.glob("chunk_*.*"):
                    safe_file = _safe_dump_child_file(dump_dir, path)
                    if safe_file is None:
                        return None
                    size += safe_file.stat().st_size
                table_sizes[table_name] = size
                total_bytes += size
            return {
                "chunk_counts": chunk_counts,
                "table_sizes": table_sizes,
                "table_rows": table_rows,
                "total_bytes": total_bytes,
                "total_rows": total_rows,
                "schema": manifest.get("database", ""),
                "format": manifest.get("format", ""),
                "format_version": manifest.get("format_version", 0),
            }
        except Exception:
            return None

    def import_dump(
        self,
        input_dir: str,
        target_schema: Optional[str] = None,
        threads: int = 8,
        import_mode: str = "replace",
        timezone_sql: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
        retry_tables: Optional[List[str]] = None,
        metadata_callback: Optional[Callable[[dict], None]] = None,
        table_chunk_progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> Tuple[bool, str, dict]:
        import_results: dict = {}
        try:
            manifest_path = Path(input_dir) / "_tunnelforge_dump.json"
            if not manifest_path.exists():
                return False, "TunnelForge Rust dump manifest를 찾을 수 없습니다.", import_results

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_schema = str(manifest.get("database") or "")
            tables_to_import = [
                str(table.get("name"))
                for table in manifest.get("tables", [])
                if table.get("name")
            ]
            if retry_tables:
                retry_set = set(retry_tables)
                tables_to_import = [table for table in tables_to_import if table in retry_set]

            metadata = self._analyze_dump_metadata(input_dir)
            if metadata and metadata_callback:
                metadata_callback(metadata)

            for table in tables_to_import:
                import_results[table] = {"status": "pending", "message": ""}
                if table_status_callback:
                    table_status_callback(table, "pending", "")

            final_target_schema = target_schema or source_schema
            if not final_target_schema:
                return False, "대상 스키마를 지정할 수 없습니다.", import_results

            payload = {
                "target": self._endpoint(final_target_schema).to_payload(),
                "input_dir": input_dir,
                "mode": import_mode,
                "threads": max(1, int(threads)),
            }
            if retry_tables:
                payload["tables"] = retry_tables

            result = self.facade.import_dump(
                payload,
                on_event=lambda event: emit_core_event(
                    event,
                    progress_callback,
                    table_progress_callback,
                    detail_callback,
                    table_status_callback,
                    raw_output_callback,
                    import_results,
                    table_chunk_progress_callback,
                ),
            )

            for table in tables_to_import:
                import_results[table] = {"status": "done", "message": ""}
            rows = int(result.get("rows_imported") or 0)
            return True, f"Rust DB Core import 완료: {len(tables_to_import)}개 테이블, {rows:,} rows", import_results
        except DbCoreServiceError as exc:
            return False, f"Rust DB Core import 오류: {exc}", import_results
        except Exception as exc:
            return False, f"Import 오류: {exc}", import_results


class TableProgressTracker:
    """Table progress metadata helper for the import UI."""

    def __init__(self, metadata: Optional[Dict]):
        self.chunk_counts = metadata.get("chunk_counts", {}) if metadata else {}
        self.table_sizes = metadata.get("table_sizes", {}) if metadata else {}
        self.total_bytes = metadata.get("total_bytes", 0) if metadata else 0
        self.completed_tables: Set[str] = set()

    def estimate_loading_tables(
        self,
        loaded_bytes: int,
        completed_tables: List[str],
    ) -> List[Tuple[str, int, int]]:
        self.completed_tables = set(completed_tables)
        candidates = [
            (table, self.table_sizes.get(table, 0), self.chunk_counts.get(table, 1))
            for table in self.table_sizes
            if table not in self.completed_tables and self.table_sizes.get(table, 0) > 10_000_000
        ]
        candidates.sort(key=lambda item: -item[1])
        return candidates[:4]

    def get_table_info(self, table_name: str) -> Tuple[int, int]:
        return self.table_sizes.get(table_name, 0), self.chunk_counts.get(table_name, 1)

    def format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def emit_core_event(
    event: Dict,
    progress_callback: Optional[Callable[[str], None]] = None,
    table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
    detail_callback: Optional[Callable[[dict], None]] = None,
    table_status_callback: Optional[Callable[[str, str, str], None]] = None,
    raw_output_callback: Optional[Callable[[str], None]] = None,
    import_results: Optional[dict] = None,
    table_chunk_progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    event_type = event.get("event")
    table = str(event.get("table") or "")
    if raw_output_callback:
        raw_output_callback(json.dumps(event, ensure_ascii=False))

    if event_type == "dump_plan":
        if detail_callback:
            detail_callback({
                "event": "dump_plan",
                "tables_total": int(event.get("tables_total") or 0),
                "rows_total": int(event.get("rows_total") or 0),
                "tables": event.get("tables") if isinstance(event.get("tables"), list) else [],
            })
    elif event_type == "dump_schedule":
        if detail_callback:
            detail_callback({
                "event": "dump_schedule",
                "threads": int(event.get("threads") or 0),
                "table_workers": int(event.get("table_workers") or 0),
                "range_workers_per_table": int(event.get("range_workers_per_table") or 0),
                "chunk_size": int(event.get("chunk_size") or 0),
                "data_format": str(event.get("data_format") or ""),
                "compression": str(event.get("compression") or ""),
                "scheduled_tables": event.get("scheduled_tables") if isinstance(event.get("scheduled_tables"), list) else [],
            })
    elif event_type == "phase" and progress_callback:
        progress_callback(_format_import_phase_message(event))
    elif event_type == "table_progress":
        current = int(event.get("current") or 0)
        total = int(event.get("total") or 0)
        status = str(event.get("status") or "")
        ui_status = "loading" if status in ("dumping", "importing") else "done" if status == "completed" else status
        if table_progress_callback and status == "completed":
            table_progress_callback(current, total, table)
        if table_status_callback and table:
            table_status_callback(table, ui_status, "")
        if import_results is not None and table:
            import_results[table] = {"status": ui_status or "loading", "message": ""}
    elif event_type == "row_progress":
        rows = int(event.get("rows") or 0)
        total = int(event.get("total") or 0)
        chunk_rows = int(event.get("chunk_rows") or 0)
        elapsed_ms = int(event.get("stream_ms") or event.get("read_ms") or event.get("load_ms") or 0)
        rows_sec = int((chunk_rows * 1000) / elapsed_ms) if chunk_rows and elapsed_ms else 0
        percent = int((rows / total) * 100) if total else 0
        if detail_callback:
            detail_callback({
                "event": "row_progress",
                "table": table,
                "percent": min(percent, 100),
                "rows_done": rows,
                "rows_total": total,
                "chunk_rows": chunk_rows,
                "rows_sec": rows_sec,
                "speed": f"{rows_sec:,} rows/s" if rows_sec else "Rust DB Core",
                "chunk_index": event.get("chunk_index"),
                "chunks_done": event.get("chunks_done"),
                "chunks_total": event.get("chunks_total"),
                "strategy": event.get("strategy"),
                "stream_ms": event.get("stream_ms"),
                "read_ms": event.get("read_ms"),
                "write_ms": event.get("write_ms"),
            })
        chunks_done = int(event.get("chunks_done") or 0)
        chunks_total = int(event.get("chunks_total") or 0)
        if table_chunk_progress_callback and table and chunks_done and chunks_total:
            table_chunk_progress_callback(table, chunks_done, chunks_total)


def check_rust_dump() -> Tuple[bool, str]:
    installed, message, _ = RustDumpChecker.check_installation()
    return installed, message


def export_schema(
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    output_dir: str,
    threads: int = 8,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    config = RustDumpConfig(host, port, user, password)
    exporter = RustDumpExporter(config)
    return exporter.export_full_schema(schema, output_dir, threads, progress_callback=progress_callback)


def export_tables(
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    tables: List[str],
    output_dir: str,
    threads: int = 8,
    include_fk_parents: bool = True,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str, List[str]]:
    config = RustDumpConfig(host, port, user, password)
    exporter = RustDumpExporter(config)
    return exporter.export_tables(
        schema,
        tables,
        output_dir,
        threads,
        include_fk_parents=include_fk_parents,
        progress_callback=progress_callback,
    )


def import_dump(
    host: str,
    port: int,
    user: str,
    password: str,
    input_dir: str,
    target_schema: Optional[str] = None,
    threads: int = 8,
    import_mode: str = "replace",
    progress_callback: Optional[Callable[[str], None]] = None,
    table_chunk_progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> Tuple[bool, str, dict]:
    config = RustDumpConfig(host, port, user, password)
    importer = RustDumpImporter(config)
    return importer.import_dump(
        input_dir,
        target_schema,
        threads,
        import_mode=import_mode,
        progress_callback=progress_callback,
        table_chunk_progress_callback=table_chunk_progress_callback,
    )
