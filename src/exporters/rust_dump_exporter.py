"""Rust DB Core backed dump export/import helpers."""
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.core.db_connector import MySQLConnector
from src.core.db_core_service import (
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DbCoreFacade,
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreServiceError,
    DbEndpoint,
    normalize_db_engine,
)
from src.core.constants import (
    DEFAULT_DB_ENGINE,
    DEFAULT_DB_USER,
    DEFAULT_LOCAL_HOST,
    DEFAULT_MYSQL_PORT,
)
from src.core.foreign_key_resolver import ForeignKeyResolver, OrphanRecordInfo
from src.core.logger import get_logger
from src.exporters.dump_progress import DumpEventCallbacks, TableProgressTracker, emit_core_event

logger = get_logger("rust_dump_exporter")

DEFAULT_DUMP_COMPRESSION = "zstd"
DEFAULT_DUMP_THREADS = 8


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


def _shutdown_owned_facade(facade: DbCoreFacade, owns_facade: bool) -> None:
    if not owns_facade:
        return
    try:
        facade.client.shutdown(
            timeout_seconds=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except DbCoreServiceError:
        raise
    except Exception as exc:
        raise DbCoreServiceError(
            f"Rust DB Core dedicated facade shutdown failed: {type(exc).__name__}: {exc}",
            code="db_core_residual_process",
            request_kind=DbCoreRequestKind.MUTATION,
            outcome=DbCoreOutcome.FAILED,
        ) from exc


@dataclass
class RustDumpConfig:
    """Connection settings for Rust DB Core dump operations."""

    host: str
    port: int
    user: str
    password: str
    schema: str = ""
    engine: str = "mysql"

    def __post_init__(self) -> None:
        self.engine = normalize_db_engine(self.engine, self.port)

    def get_masked_uri(self) -> str:
        return f"{self.user}:****@{self.host}:{self.port}"


def build_rust_dump_config(connector) -> RustDumpConfig:
    """Build RustDumpConfig from connector attributes with legacy fallbacks."""
    return RustDumpConfig(
        host=getattr(connector, 'host', DEFAULT_LOCAL_HOST),
        port=connector.port if hasattr(connector, 'port') else DEFAULT_MYSQL_PORT,
        user=connector.user if hasattr(connector, 'user') else DEFAULT_DB_USER,
        password=connector.password if hasattr(connector, 'password') else "",
        engine=getattr(connector, 'engine', DEFAULT_DB_ENGINE),
    )


class RustDumpChecker:
    """Checks whether the Rust DB Core dump protocol is available."""

    @staticmethod
    def check_installation() -> Tuple[bool, str, Optional[str]]:
        facade = DbCoreFacade()
        try:
            result = facade.hello()
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
        finally:
            _shutdown_owned_facade(facade, True)

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


class _RustDumpClientBase:
    """Shared facade/endpoint plumbing for Rust dump exporter/importer."""

    def __init__(self, config: RustDumpConfig, facade: Optional[DbCoreFacade] = None):
        self.config = config
        self.facade = facade if facade is not None else DbCoreFacade()
        self._owns_facade = facade is None

    def _endpoint(self, schema: str) -> DbEndpoint:
        return DbEndpoint(
            engine=self.config.engine,
            host=self.config.host,
            port=int(self.config.port),
            user=self.config.user,
            password=self.config.password,
            database=schema,
        )


class RustDumpExporter(_RustDumpClientBase):
    """Rust DB Core backed dump exporter."""

    def _resolve_required_tables_from_rust_schema(
        self,
        selected_tables: List[str],
        schema: str,
    ) -> Tuple[List[str], List[str]]:
        inspected = self.facade.inspect_schema(self._endpoint(schema))
        table_deps: Dict[str, Set[str]] = {}
        for table in inspected.get("tables", []) if isinstance(inspected, dict) else []:
            if not isinstance(table, dict):
                continue
            table_name = str(table.get("name") or "")
            if not table_name:
                continue
            parents: Set[str] = set()
            foreign_keys = table.get("foreign_keys")
            if isinstance(foreign_keys, list):
                for foreign_key in foreign_keys:
                    if not isinstance(foreign_key, dict):
                        continue
                    referenced_table = str(foreign_key.get("referenced_table") or "")
                    if referenced_table and referenced_table != table_name:
                        parents.add(referenced_table)
            if parents:
                table_deps[table_name] = parents

        required = set(selected_tables)
        added: Set[str] = set()
        changed = True
        while changed:
            changed = False
            for table_name in list(required):
                for parent in table_deps.get(table_name, set()):
                    if parent not in required:
                        required.add(parent)
                        added.add(parent)
                        changed = True
        return sorted(required), sorted(added)

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
        threads: int = DEFAULT_DUMP_THREADS,
        chunk_size: int = 50000,
        compression: str = DEFAULT_DUMP_COMPRESSION,
        callbacks: Optional[DumpEventCallbacks] = None,
    ) -> Tuple[bool, str]:
        callbacks = callbacks or DumpEventCallbacks()
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

        if callbacks.progress:
            callbacks.progress(f"Rust DB Core export 시작: {self.config.get_masked_uri()}/{schema}")

        result = self.facade.run_dump(
            payload,
            on_event=lambda event: self._emit_core_event(
                event,
                callbacks.progress,
                callbacks.table_progress,
                callbacks.detail,
                callbacks.table_status,
                callbacks.raw_output,
            ),
        )
        rows = int(result.get("rows_dumped") or 0)
        table_count = int(result.get("tables") or 0)
        view_count = int(result.get("views") or 0)
        message = f"Rust DB Core export 완료: {table_count}개 테이블, {rows:,} rows"
        if view_count:
            message += f", View {view_count}개"
        return True, message

    def export_full_schema(
        self,
        schema: str,
        output_dir: str,
        threads: int = DEFAULT_DUMP_THREADS,
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
                callbacks=DumpEventCallbacks(
                    progress=progress_callback,
                    table_progress=table_progress_callback,
                    detail=detail_callback,
                    table_status=table_status_callback,
                    raw_output=raw_output_callback,
                ),
            )
            if success:
                self._write_metadata(output_dir, schema, "full", None)
            return success, message
        except DbCoreServiceError as exc:
            return False, f"Rust DB Core export 오류: {exc}"
        except Exception as exc:
            return False, f"Export 오류: {exc}"
        finally:
            _shutdown_owned_facade(self.facade, self._owns_facade)

    def export_tables(
        self,
        schema: str,
        tables: List[str],
        output_dir: str,
        threads: int = DEFAULT_DUMP_THREADS,
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
                final_tables, added_tables = self._resolve_required_tables_from_rust_schema(
                    tables,
                    schema,
                )

            success, message = self._run_rust_dump(
                schema=schema,
                output_dir=output_dir,
                tables=final_tables,
                threads=threads,
                compression=compression,
                callbacks=DumpEventCallbacks(
                    progress=progress_callback,
                    table_progress=table_progress_callback,
                    detail=detail_callback,
                    table_status=table_status_callback,
                    raw_output=raw_output_callback,
                ),
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
            _shutdown_owned_facade(self.facade, self._owns_facade)

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


def _mark_non_done_import_results_error(
    import_results: dict,
    message: str,
    table_status_callback: Optional[Callable[[str, str, str], None]] = None,
) -> None:
    for table, result in list(import_results.items()):
        status = result.get("status") if isinstance(result, dict) else None
        if status == "done":
            continue
        import_results[table] = {"status": "error", "message": message}
        if table_status_callback:
            table_status_callback(table, "error", message)


class RustDumpImporter(_RustDumpClientBase):
    """Rust DB Core backed dump importer."""

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
        threads: int = DEFAULT_DUMP_THREADS,
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
                "strict_manifest": True,
            }
            if timezone_sql:
                payload["timezone_sql"] = timezone_sql
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
            views_imported = result.get("views_imported") or []
            views_failed = result.get("views_failed") or []
            views_skipped = result.get("views_skipped_cross_engine") or []
            message = f"Rust DB Core import 완료: {len(tables_to_import)}개 테이블, {rows:,} rows"
            if views_imported:
                message += f", View {len(views_imported)}개"
            if views_failed:
                failed_names = ", ".join(
                    str(item.get("name", "")) for item in views_failed if isinstance(item, dict)
                )
                message += f" (View {len(views_failed)}개 생성 실패: {failed_names})"
            if views_skipped:
                message += f" (크로스 엔진 View {len(views_skipped)}개 건너뜀)"
            return True, message, import_results
        except DbCoreServiceError as exc:
            _mark_non_done_import_results_error(import_results, str(exc), table_status_callback)
            return False, f"Rust DB Core import 오류: {exc}", import_results
        except Exception as exc:
            _mark_non_done_import_results_error(import_results, str(exc), table_status_callback)
            return False, f"Import 오류: {exc}", import_results
        finally:
            _shutdown_owned_facade(self.facade, self._owns_facade)


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
    threads: int = DEFAULT_DUMP_THREADS,
    progress_callback: Optional[Callable[[str], None]] = None,
    engine: str = "mysql",
) -> Tuple[bool, str]:
    config = RustDumpConfig(host, port, user, password, engine=engine)
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
    threads: int = DEFAULT_DUMP_THREADS,
    include_fk_parents: bool = True,
    progress_callback: Optional[Callable[[str], None]] = None,
    engine: str = "mysql",
) -> Tuple[bool, str, List[str]]:
    config = RustDumpConfig(host, port, user, password, engine=engine)
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
    threads: int = DEFAULT_DUMP_THREADS,
    import_mode: str = "replace",
    progress_callback: Optional[Callable[[str], None]] = None,
    table_chunk_progress_callback: Optional[Callable[[str, int, int], None]] = None,
    engine: str = "mysql",
) -> Tuple[bool, str, dict]:
    config = RustDumpConfig(host, port, user, password, engine=engine)
    importer = RustDumpImporter(config)
    return importer.import_dump(
        input_dir,
        target_schema,
        threads,
        import_mode=import_mode,
        progress_callback=progress_callback,
        table_chunk_progress_callback=table_chunk_progress_callback,
    )
