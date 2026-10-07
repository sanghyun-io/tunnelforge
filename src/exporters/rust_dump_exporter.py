"""Rust DB Core backed dump export/import helpers."""
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.core.db_core_service import (
    DbCoreFacade,
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
from src.core.error_report_codes import classify_error_code
from src.core.logger import get_logger
from src.exporters.dump_progress import DumpEventCallbacks, TableProgressTracker, emit_core_event

logger = get_logger("rust_dump_exporter")

DEFAULT_DUMP_COMPRESSION = "zstd"
MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED = (
    "MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED"
)
MYSQL_SNAPSHOT_MODES = frozenset(
    {"parallel_strict", "parallel_no_backup_lock", "single_connection"}
)
# FTWRL(글로벌 read lock) 단계에서 거부되면 병렬 스냅샷 자체가 불가능하고,
# BACKUP_ADMIN(LOCK INSTANCE FOR BACKUP) 단계에서만 거부되면 백업 락을 생략한
# 단일 연결 스냅샷(parallel_no_backup_lock 호환 모드)으로 우회할 수 있다.
MYSQL_PRIVILEGE_BACKUP_ADMIN = "BACKUP_ADMIN"
MYSQL_PRIVILEGE_FLUSH_OR_RELOAD = "FLUSH_TABLES_OR_RELOAD"


def is_mysql_parallel_snapshot_privilege_error(message: object) -> bool:
    return MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED in str(message)


def mysql_parallel_snapshot_denied_privilege(message: object) -> Optional[str]:
    """병렬 스냅샷 권한 거부 마커에서 거부된 권한 이름을 추출한다.

    마커 형식은 ``MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED:<privilege>:<server_error>``.
    마커가 없으면 None을 반환한다.
    """
    text = str(message)
    marker = MYSQL_PARALLEL_SNAPSHOT_PRIVILEGE_REQUIRED
    index = text.find(marker)
    if index == -1:
        return None
    remainder = text[index + len(marker):]
    parts = remainder.split(":")
    # parts[0]은 마커 바로 뒤(구분자로 인한 빈 문자열), parts[1]이 권한 이름이다.
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return None


DEFAULT_DUMP_THREADS = 8


def dump_original_namespace(manifest: dict, target_engine: str) -> str:
    """Return an unambiguous same-engine namespace; empty requires user selection."""
    source_engine = normalize_db_engine(manifest.get("source_engine") or target_engine)
    if source_engine != target_engine:
        return ""
    if source_engine == "postgresql":
        return str(manifest.get("source_schema") or "")
    return str(manifest.get("database") or "")


def restore_target_connection_info(value: object) -> dict:
    """Credential-free endpoint fields for the operator's local handoff."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ("engine", "host", "port", "database", "schema")
            if isinstance(value.get(key), (str, int))}


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
        facade.client.shutdown()
    except Exception as exc:
        logger.warning("Rust DB Core dedicated facade shutdown failed: %s", exc)


@dataclass
class RustDumpConfig:
    """Connection settings for Rust DB Core dump operations."""

    host: str
    port: int
    user: str
    password: str
    schema: str = ""
    engine: str = "mysql"
    database: str = ""

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
        database=getattr(connector, 'database', '') or '',
    )


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


class _RustDumpClientBase:
    """Shared facade/endpoint plumbing for Rust dump exporter/importer."""

    def __init__(self, config: RustDumpConfig, facade: Optional[DbCoreFacade] = None):
        self.config = config
        self.facade = facade if facade is not None else DbCoreFacade()
        self._owns_facade = facade is None
        self.last_error_code: Optional[str] = None

    def _remember_error_code(self, exc: BaseException) -> None:
        """Keep only the allowlisted failure code for anonymous error reports."""
        self.last_error_code = classify_error_code(getattr(exc, "error_code", None), str(exc))

    def _endpoint(self, schema: str) -> DbEndpoint:
        return DbEndpoint(
            engine=self.config.engine,
            host=self.config.host,
            port=int(self.config.port),
            user=self.config.user,
            password=self.config.password,
            database=(self.config.database or "postgres") if self.config.engine == "postgresql" else schema,
            schema=schema if self.config.engine == "postgresql" else "",
        )


class RustDumpExporter(_RustDumpClientBase):
    """Rust DB Core backed dump exporter."""

    # Set when the core refused the export before writing anything
    # (error_code "unsupported_objects"): {"objects": [...], "bypassable": bool}.
    last_refusal: Optional[Dict] = None

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
        mysql_snapshot_mode: str = "parallel_strict",
        allow_incomplete: bool = False,
    ) -> Tuple[bool, str]:
        callbacks = callbacks or DumpEventCallbacks()
        self.last_refusal = None
        normalized_snapshot_mode = str(mysql_snapshot_mode).strip().lower()
        if normalized_snapshot_mode not in MYSQL_SNAPSHOT_MODES:
            raise ValueError(
                f"unsupported mysql_snapshot_mode: {normalized_snapshot_mode}"
            )
        effective_threads = (
            1 if normalized_snapshot_mode == "single_connection"
            else max(1, int(threads))
        )
        payload = {
            "source": self._endpoint(schema).to_payload(),
            "output_dir": output_dir,
            "overwrite": True,
            "threads": effective_threads,
            "chunk_size": max(1000, int(chunk_size)),
            "data_format": "tsv",
            "compression": compression if compression in {"none", "zstd"} else DEFAULT_DUMP_COMPRESSION,
            "mysql_snapshot_mode": normalized_snapshot_mode,
        }
        if tables:
            payload["tables"] = tables
        if allow_incomplete:
            payload["allow_incomplete"] = True

        def on_core_event(event: Dict) -> None:
            if event.get("event") == "error" and event.get("error_code") == "unsupported_objects":
                self.last_refusal = {
                    "objects": [str(item) for item in event.get("objects") or []],
                    "bypassable": bool(event.get("bypassable")),
                }
            self._emit_core_event(
                event,
                callbacks.progress,
                callbacks.table_progress,
                callbacks.detail,
                callbacks.table_status,
                callbacks.raw_output,
            )

        if callbacks.progress:
            callbacks.progress(f"Rust DB Core export 시작: {self.config.get_masked_uri()}/{schema}")

        result = self.facade.run_dump(payload, on_event=on_core_event)
        rows = int(result.get("rows_dumped") or 0)
        table_count = int(result.get("tables") or 0)
        view_count = int(result.get("views") or 0)
        message = f"Rust DB Core export 완료: {table_count}개 테이블, {rows:,} rows"
        if view_count:
            message += f", View {view_count}개"
        snapshot_policy = result.get("snapshot_policy")
        if snapshot_policy == "mysql_shared_consistent_snapshot":
            message += f" (MySQL 공유 일관 스냅샷, {effective_threads}개 워커)"
        elif snapshot_policy == "mysql_parallel_no_backup_lock_consistent_snapshot":
            message += (
                f" (MySQL 락 없는 병렬 스냅샷, {effective_threads}개 워커)"
            )
        elif snapshot_policy == "mysql_single_connection_consistent_snapshot":
            message += " (MySQL 단일 연결 일관 스냅샷)"
        warnings = [str(warning) for warning in result.get("manifest_warnings", [])]
        for warning in warnings:
            if callbacks.progress:
                callbacks.progress(f"Export warning: {warning}")
        if warnings:
            message += "\n" + "\n".join(f"Export warning: {warning}" for warning in warnings)
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
        mysql_snapshot_mode: str = "parallel_strict",
        allow_incomplete: bool = False,
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
                mysql_snapshot_mode=mysql_snapshot_mode,
                allow_incomplete=allow_incomplete,
            )
            if success:
                self._write_metadata(output_dir, schema, "full", None)
            return success, message
        except DbCoreServiceError as exc:
            self._remember_error_code(exc)
            return False, f"Rust DB Core export 오류: {exc}"
        except Exception as exc:
            self._remember_error_code(exc)
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
        mysql_snapshot_mode: str = "parallel_strict",
        allow_incomplete: bool = False,
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
                mysql_snapshot_mode=mysql_snapshot_mode,
                allow_incomplete=allow_incomplete,
            )
            if success:
                self._write_metadata(output_dir, schema, "partial", final_tables, added_tables)
                return True, message, final_tables
            return False, message, []
        except DbCoreServiceError as exc:
            self._remember_error_code(exc)
            return False, f"Rust DB Core export 오류: {exc}", []
        except Exception as exc:
            self._remember_error_code(exc)
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
        failed = status in ("loading", "error") or f": {table}:" in message
        next_status = "error" if failed else "blocked"
        detail = message if failed else "Import stopped; this table was not started."
        import_results[table] = {"status": next_status, "message": detail}
        if table_status_callback:
            table_status_callback(table, next_status, detail)


# Rust Core refusals raised before any target table is changed.
_OPERATION_SCOPED_IMPORT_CODES = (
    "incompatible_surviving_fk:",
    "target_dependency_preflight_failed:",
    "ddl_preflight_failed:",
    "preflight_surviving_fk:",
)


def _is_operation_scoped_import_error(message: str) -> bool:
    return message.lstrip().startswith(_OPERATION_SCOPED_IMPORT_CODES)


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
                "schema": dump_original_namespace(manifest, self.config.engine),
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
        import_mode: str = "safe",
        timezone_sql: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
        retry_tables: Optional[List[str]] = None,
        metadata_callback: Optional[Callable[[dict], None]] = None,
        table_chunk_progress_callback: Optional[Callable[[str, int, int], None]] = None,
        use_source_timezone: bool = True,
    ) -> Tuple[bool, str, dict]:
        import_results: dict = {}
        try:
            manifest_path = Path(input_dir) / "_tunnelforge_dump.json"
            if not manifest_path.exists():
                return False, "TunnelForge Rust dump manifest를 찾을 수 없습니다.", import_results

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_schema = dump_original_namespace(manifest, self.config.engine)
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
                return False, "원본 스키마를 확정할 수 없습니다. 대상 데이터베이스/스키마를 직접 선택하세요.", import_results

            payload = {
                "target": self._endpoint(final_target_schema).to_payload(),
                "input_dir": input_dir,
                "mode": import_mode,
                "threads": max(1, int(threads)),
                "strict_manifest": True,
                "use_source_timezone": use_source_timezone,
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

            if import_mode == "safe":
                candidate = restore_target_connection_info(result.get("candidate_target"))
                state = {key: result.get(key) for key in ("status", "verified", "original_unchanged", "cutover_pending", "ready_for_switch", "blockers", "namespace_existed", "namespace_created", "restore_id", "report_path", "plan_digest")}
                state.update(event="safe_restore_ready", candidate_target=candidate,
                             original_target=restore_target_connection_info(result.get("original_target")))
                if raw_output_callback:
                    raw_output_callback(json.dumps(state, ensure_ascii=False))
                new_target = result.get("status") == "completed_new_target" and result.get("namespace_existed") is False and result.get("namespace_created") is True
                if not ((new_target or result.get("status") in ("ready_for_switch", "ready_for_review"))
                        and result.get("verified") is True and result.get("original_unchanged") is True
                        and result.get("cutover_pending") is (not new_target) and candidate.get("database")
                        and (self.config.engine != "postgresql" or candidate.get("schema"))):
                    return False, "안전 복원의 검증된 새 대상 정보를 확인할 수 없습니다. 원본과 복원 대상의 상태를 보고서에서 확인하세요.", import_results

            for table in tables_to_import:
                import_results[table] = {"status": "done", "message": ""}
            rows = int(result.get("rows_imported") or 0)
            views_imported = result.get("views_imported") or []
            views_failed = result.get("views_failed") or []
            views_skipped = result.get("views_skipped_cross_engine") or []
            message = f"Rust DB Core import 완료: {len(tables_to_import)}개 테이블, {rows:,} rows"
            if import_mode == "safe":
                candidate_name = candidate.get("schema") if self.config.engine == "postgresql" else candidate.get("database")
                message = f"안전 복원 검증 완료: {candidate_name}. 원본 미변경 · 전환 대기."
                if new_target:
                    message = f"요청한 새 대상 복원·검증 완료: {candidate_name}. 기존 대상 변경 없음."
                if result.get("status") == "ready_for_review":
                    message += " 전환 전 추가 검토가 필요합니다."
                    for blocker in result.get("blockers") or []:
                        message += f"\n{blocker}"
            if views_imported:
                message += f", View {len(views_imported)}개"
            if views_failed:
                failed_names = ", ".join(
                    str(item.get("name", "")) for item in views_failed if isinstance(item, dict)
                )
                message += f" (View {len(views_failed)}개 생성 실패: {failed_names})"
            if views_skipped:
                message += f" (크로스 엔진 View {len(views_skipped)}개 건너뜀)"
            for warning in (result.get("verification") or {}).get("warnings", []) or []:
                warning_line = f"Import warning: {warning}"
                message += "\n" + warning_line
                if progress_callback:
                    progress_callback(warning_line)
            return not bool(views_failed), message, import_results
        except DbCoreServiceError as exc:
            self._remember_error_code(exc)
            if not _is_operation_scoped_import_error(str(exc)):
                _mark_non_done_import_results_error(import_results, str(exc), table_status_callback)
            return False, f"Rust DB Core import 오류: {exc}", import_results
        except Exception as exc:
            self._remember_error_code(exc)
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
    import_mode: str = "safe",
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
