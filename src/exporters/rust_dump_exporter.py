"""Rust DB Core backed dump export/import helpers."""
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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
from src.core.connection_trust import ERROR_MESSAGES as CONNECTION_ERROR_MESSAGES
from src.core.error_report_codes import classify_error_code
from src.core.i18n import translate_text
from src.core.logger import get_logger
from src.exporters.dump_progress import DumpEventCallbacks, TableProgressTracker, emit_core_event

logger = get_logger("rust_dump_exporter")

DEFAULT_DUMP_COMPRESSION = "zstd"
MYSQL_SNAPSHOT_MODES = frozenset({
    # The core picks the method: shared locks first, then mysql_snapshot_fallback.
    "auto",
    "parallel_strict",
    "parallel_table_lock",
    "parallel_unsynchronized",
    "parallel_no_backup_lock",
    "single_connection",
})
# Where "auto" ends when no shared lock works; unsynchronized only by explicit choice.
MYSQL_SNAPSHOT_FALLBACKS = frozenset({"single_connection", "parallel_unsynchronized"})
# service.hello lists it when dump.run takes "auto"; an older core would refuse every export.
MYSQL_SNAPSHOT_AUTO_CAPABILITY = "dump.run.mysql_snapshot_auto"
_AUTO_SNAPSHOT_UNSUPPORTED = (
    "이 Rust DB Core는 내보내는 방법 자동 선택을 지원하지 않습니다. 앱과 함께 배포된 코어를 사용하세요."
)
_ONE_CONNECTION_MODES = frozenset({"single_connection", "parallel_no_backup_lock"})

# How the completion line names the MySQL method that ran (the core's resolved
# mysql_snapshot_mode): what the dump guarantees, and how the tables were read.
_SNAPSHOT_METHODS = {
    "parallel_strict": ("같은 시점으로 내보냄", ""),
    "parallel_table_lock": ("같은 시점으로 내보냄", "테이블만 잠깐 잠금"),
    "single_connection": ("같은 시점으로 내보냄", "연결 하나로 읽음"),
    "parallel_no_backup_lock": ("같은 시점으로 내보냄", "연결 하나로 읽음"),
    "parallel_unsynchronized": ("테이블마다 시점이 다를 수 있음", "시점을 맞추지 않고 나눠 읽음"),
}
# Older cores report only the manifest policy.
_SNAPSHOT_POLICY_MODES = {
    "mysql_shared_consistent_snapshot": "parallel_strict",
    "mysql_table_lock_shared_snapshot": "parallel_table_lock",
    "mysql_single_connection_consistent_snapshot": "single_connection",
    "mysql_parallel_unsynchronized_snapshots": "parallel_unsynchronized",
}
# Why the automatic choice skipped a method (snapshot_fallbacks[].reason).
_SNAPSHOT_FALLBACK_REASONS = {
    "privilege": "잠금 권한이 없어",
    "unsupported": "서버가 백업 잠금을 지원하지 않아",
    "connection_limit": "동시 연결 수 제한 때문에",
    "lock_timeout": "잠금을 제때 얻지 못해",
}
_FEWER_WORKERS = "계정의 동시 연결 수 제한으로 줄임"


def resolved_snapshot_mode(result: dict) -> Optional[str]:
    """The MySQL method that ran, from a dump.run result; None for PostgreSQL."""
    mode = result.get("mysql_snapshot_mode") or _SNAPSHOT_POLICY_MODES.get(str(result.get("snapshot_policy") or ""))
    return mode if mode in _SNAPSHOT_METHODS else None


def describe_snapshot_method(result: dict, requested_threads: int) -> str:
    """One line naming the MySQL method the core used, from its result; empty for PostgreSQL.

    e.g. "같은 시점으로 내보냄 · 동시 작업 8개 (잠금 권한이 없어 테이블만 잠깐 잠금)".
    """
    mode = resolved_snapshot_mode(result)
    if mode is None:
        return ""
    guarantee, how = _SNAPSHOT_METHODS[mode]
    one_connection = mode in _ONE_CONNECTION_MODES
    workers = int(result.get("worker_count") or (1 if one_connection else requested_threads))
    fallbacks = [item for item in result.get("snapshot_fallbacks") or [] if isinstance(item, dict)]
    reason = _SNAPSHOT_FALLBACK_REASONS.get(str(fallbacks[-1].get("reason"))) if fallbacks else None
    notes = [" ".join(part for part in (reason, how) if part)]
    if workers < requested_threads and not one_connection:
        notes.append(_FEWER_WORKERS)
    detail = ", ".join(note for note in notes if note)
    line = f"{guarantee} · 동시 작업 {workers}개"
    return f"{line} ({detail})" if detail else line


# Why dump.run refused an object (its `kind:table:name` code, schema.rs). Table-scoped kinds name
# the object inside the table; schema-level kinds (routine, event, materialized_view) have no table.
_WHOLE_TABLE_LEFT_OUT = "{} 엔진이라 다른 테이블과 같은 시점으로 읽을 수 없습니다. 빼고 내보내면 이 테이블이 통째로 빠집니다"
_TABLE_REFUSAL_REASONS = {
    # Can be left out ("담을 수 없는 객체는 빼고 내보내기")
    "trigger": "트리거({})는 덤프에 담을 수 없습니다",
    # The core drops such a table from the dump, structure and data, when it is left out.
    "deprecated_engine": _WHOLE_TABLE_LEFT_OUT,
    "non_transactional_table": _WHOLE_TABLE_LEFT_OUT,
    # Definitions that would be lost: never left out
    "generated_column": "계산 열 {}의 정의를 덤프에 담을 수 없습니다",
    "check_constraint": "CHECK 제약 {}의 조건을 덤프에 담을 수 없습니다",
    "unsupported_default": "열 {}의 기본값을 덤프에 담을 수 없습니다",
    "custom_type": "열 {}의 사용자 정의 형식을 덤프에 담을 수 없습니다",
    "unsupported_index": "인덱스({})는 덤프에 담을 수 없습니다",
    "cross_schema_fk": "외래 키({})가 다른 DB나 스키마의 테이블을 가리켜 덤프에 담을 수 없습니다",
}
# dump.rs LOSSY_SCHEMA_KINDS: refused without a way to leave them out (bypassable false).
LOSSY_REFUSAL_KINDS = frozenset({
    "generated_column", "check_constraint", "unsupported_default", "custom_type", "unsupported_index", "cross_schema_fk",
})
_SCHEMA_REFUSAL_REASONS = {
    "routine": "저장 프로시저·함수 {}: 덤프에 담을 수 없습니다",
    "event": "예약 이벤트 {}: 덤프에 담을 수 없습니다",
    "materialized_view": "구체화 뷰 {}: 덤프에 담을 수 없습니다",
}
_UNKNOWN_TABLE_REFUSAL = "이 객체는 담을 수 없습니다"


def describe_export_refusal(objects: object) -> List[str]:
    """One plain sentence per refused object, e.g. "테이블 orders: 트리거(trg)는 덤프에 담을 수 없습니다".

    An unknown code still names its table; the codes themselves go behind "자세히"."""
    lines = []
    for item in objects if isinstance(objects, list) else []:
        kind, _, rest = str(item).partition(":")
        table, _, name = rest.partition(":")
        if kind in _SCHEMA_REFUSAL_REASONS:
            lines.append(translate_text(_SCHEMA_REFUSAL_REASONS[kind]).format(rest))
        elif name:
            reason = translate_text(_TABLE_REFUSAL_REASONS.get(kind, _UNKNOWN_TABLE_REFUSAL)).format(name)
            lines.append(translate_text("테이블 {}: {}").format(table, reason))
        else:
            lines.append(f"{item}: {translate_text(_UNKNOWN_TABLE_REFUSAL)}")
    return lines


def fk_parent_closure(picks: List[str], deps: Dict[str, List[Tuple[str, str]]]) -> Tuple[List[str], List[str]]:
    """The picked tables plus every FK parent they need, transitively, and the parents that were added.

    ``deps`` maps a child table to its ``(constraint, parent)`` pairs (``fetch_fk_parents``)."""
    required = set(picks)
    pending = list(picks)
    while pending:
        for _constraint, parent in deps.get(pending.pop(), []):
            if parent not in required:
                required.add(parent)
                pending.append(parent)
    return sorted(required), sorted(required - set(picks))


def fk_parents_outside(picks: List[str], deps: Dict[str, List[Tuple[str, str]]]) -> List[Dict[str, str]]:
    """Each FK of a picked table whose parent was not picked: ``{child, constraint, parent}``."""
    picked = set(picks)
    return [{"child": child, "constraint": constraint, "parent": parent}
            for child in sorted(picked) for constraint, parent in deps.get(child, []) if parent not in picked]


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
    # Set after a successful export, from the core's result: the MySQL method that ran
    # ("mode", None for PostgreSQL), its one-line description ("method") and the objects
    # the dump left out ("omitted", the "Object not exported" warnings).
    last_run: Optional[Dict] = None

    def fetch_fk_parents(self, schema: str) -> Dict[str, List[Tuple[str, str]]]:
        """Child table -> its ``(constraint, parent)`` FKs, from one schema.inspect call.

        Self-references and parents that are not tables of this schema are left out."""
        inspected = self.facade.inspect_schema(self._endpoint(schema))
        tables = [table for table in (inspected.get("tables") or [] if isinstance(inspected, dict) else [])
                  if isinstance(table, dict) and table.get("name")]
        names = {str(table["name"]) for table in tables}
        deps: Dict[str, List[Tuple[str, str]]] = {}
        for table in tables:
            child = str(table["name"])
            for foreign_key in table.get("foreign_keys") or []:
                parent = str(foreign_key.get("referenced_table") or "") if isinstance(foreign_key, dict) else ""
                if parent in names and parent != child:
                    deps.setdefault(child, []).append((str(foreign_key.get("name") or ""), parent))
        return deps

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
        mysql_snapshot_fallback: str = "single_connection",
    ) -> Tuple[bool, str]:
        callbacks = callbacks or DumpEventCallbacks()
        self.last_refusal = None
        self.last_run = None
        normalized_snapshot_mode = str(mysql_snapshot_mode).strip().lower()
        if normalized_snapshot_mode not in MYSQL_SNAPSHOT_MODES:
            raise ValueError(
                f"unsupported mysql_snapshot_mode: {normalized_snapshot_mode}"
            )
        if mysql_snapshot_fallback not in MYSQL_SNAPSHOT_FALLBACKS:
            raise ValueError(f"unsupported mysql_snapshot_fallback: {mysql_snapshot_fallback}")
        if normalized_snapshot_mode == "auto" and MYSQL_SNAPSHOT_AUTO_CAPABILITY not in (
            self.facade.hello().get("capabilities") or []
        ):
            return False, _AUTO_SNAPSHOT_UNSUPPORTED
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
        if normalized_snapshot_mode == "auto":
            payload["mysql_snapshot_fallback"] = mysql_snapshot_fallback
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
            callbacks.progress(f"Export 시작: {self.config.get_masked_uri()}/{schema}")

        result = self.facade.run_dump(payload, on_event=on_core_event)
        rows = int(result.get("rows_dumped") or 0)
        table_count = int(result.get("tables") or 0)
        view_count = int(result.get("views") or 0)
        message = f"Export 완료: 테이블 {table_count}개, 행 {rows:,}개"
        if view_count:
            message += f", 뷰 {view_count}개"
        method = describe_snapshot_method(result, effective_threads)
        if method:
            message += f" · {method}"
        # The core's own wording stays in the raw output (the result event) and the log.
        lines = []
        warnings = [str(warning) for warning in result.get("manifest_warnings") or []]
        for warning in warnings:
            logger.info("Export manifest warning: %s", warning)
            lines.append(f"Export 경고: {describe_manifest_warning(warning)}")
            if callbacks.progress:
                callbacks.progress(lines[-1])
        if lines:
            message += "\n" + "\n".join(lines)
        self.last_run = {
            "mode": resolved_snapshot_mode(result),
            "method": method,
            "omitted": [warning for warning in warnings if manifest_warning_code(warning)[0] == "omitted_object"],
        }
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
        mysql_snapshot_fallback: str = "single_connection",
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
                mysql_snapshot_fallback=mysql_snapshot_fallback,
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
        include_fk_parents: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None,
        table_progress_callback: Optional[Callable[[int, int, str], None]] = None,
        detail_callback: Optional[Callable[[dict], None]] = None,
        table_status_callback: Optional[Callable[[str, str, str], None]] = None,
        raw_output_callback: Optional[Callable[[str], None]] = None,
        mysql_snapshot_mode: str = "parallel_strict",
        allow_incomplete: bool = False,
        mysql_snapshot_fallback: str = "single_connection",
        auto_added_tables: Optional[List[str]] = None,
        fk_parents_outside_dump: Optional[List[Dict[str, str]]] = None,
    ) -> Tuple[bool, str, List[str]]:
        """Exports exactly ``tables`` unless ``include_fk_parents`` (scheduled table backups) adds their
        FK parents here. The dialog adds them itself and passes, for ``_export_metadata.json``, the
        parents it added (``auto_added_tables``) and the FKs whose parent stays out
        (``fk_parents_outside_dump``, None when it could not tell)."""
        try:
            final_tables = list(tables)
            added_tables = [table for table in auto_added_tables or [] if table in final_tables]
            if include_fk_parents:
                if progress_callback:
                    progress_callback("연결된 부모 테이블 확인 중...")
                final_tables, added_tables = fk_parent_closure(tables, self.fetch_fk_parents(schema))
                fk_parents_outside_dump = []

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
                mysql_snapshot_fallback=mysql_snapshot_fallback,
            )
            if success:
                self._write_metadata(output_dir, schema, "partial", final_tables, added_tables, fk_parents_outside_dump)
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
        fk_parents_outside_dump: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        metadata = {
            "export_time": datetime.now().isoformat(),
            "schema": schema,
            "type": export_type,
            "tables": tables,
            "added_fk_tables": added_tables or [],
            # [{child, constraint, parent}]; null when the dialog could not read the FKs.
            "fk_parents_outside_dump": [] if export_type == "full" else fk_parents_outside_dump,
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
    "target_create_failed:",
)


def _is_operation_scoped_import_error(message: str) -> bool:
    return message.lstrip().startswith(_OPERATION_SCOPED_IMPORT_CODES)


# One sentence per code the Rust core emits: the leading `code:` of an error, an error
# event's error_code, and the code of a dump.import_preview blocker or warning.
CORE_CODE_SENTENCES = {
    **CONNECTION_ERROR_MESSAGES,
    # Import errors
    "load_failed": "이 테이블을 지우고 다시 만들거나 데이터를 넣는 중에 멈췄습니다.",
    "ddl_preflight_failed": "대상에 테이블을 만들 수 없어 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다.",
    "ddl_probe_cleanup_failed": "확인용으로 잠시 만든 테이블을 지우지 못했습니다. 원래 테이블은 바뀌지 않았습니다.",
    "incompatible_surviving_fk": "덤프에 없는 대상 테이블의 외래 키가 새 테이블과 맞지 않아 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다.",
    "target_dependency_preflight_failed": "대상의 다른 객체가 테이블에 기대고 있어 교체할 수 없습니다. 대상 테이블은 바뀌지 않았습니다.",
    "target_create_failed": "대상 DB(스키마)가 없거나 이 계정에 보이지 않아 새로 만들려 했지만 오류로 멈췄습니다. 대상 테이블은 바뀌지 않았습니다.",
    "post_load_validation_failed": "데이터를 넣은 뒤 행 수를 확인하거나 인덱스·외래 키를 만들다가 멈췄습니다.",
    "import_plan_invalid": "요청한 Import 설정이나 제약 조건 이름 때문에 시작할 수 없습니다.",
    "export_invalid": "덤프 파일이 온전하지 않거나 이 대상에 만들 수 없는 테이블이 있습니다.",
    "view_target_invalid": "덤프의 뷰 정의를 그대로 만들 수 없습니다.",
    # Verify-in-copy
    "safe_promotion": "검증한 복사본을 원래 이름으로 바꾸지 못했습니다.",
    "safe_restore_content_invalid": "복사본을 검증하다가 덤프에서 읽을 수 없는 값을 만났습니다.",
    "safe_restore_content_mismatch": "복사본의 데이터가 덤프와 달라 교체하지 않았습니다.",
    "safe_restore_content_read_failed": "복사본의 데이터를 읽지 못해 검증하지 못했습니다.",
    "legacy_bit_unverifiable": "예전 형식 덤프라 BIT 열을 복사본에서 검증할 수 없습니다. 이 버전으로 다시 내보내세요.",
    "cross_engine": "다른 DB 종류에서 만든 덤프는 복사본에서 검증할 수 없습니다.",
    "partial_table_filter": "일부 테이블만 넣을 때는 복사본에서 검증할 수 없습니다.",
    # Why verify-in-copy stopped before making a copy (copy_blockers)
    "copy_refused": "복사본을 만들기 전에 멈췄습니다. 원본은 그대로입니다.",
    "copy_create_privilege_missing": "이 계정에 복사본과 백업을 둘 DB(PostgreSQL은 스키마)를 만들 권한이 없거나, 권한 목록으로 확인할 수 없습니다.",
    "metadata_visibility_unproven": "이 계정에 서버 전체를 조회하는 권한(SELECT, SHOW VIEW, PROCESS)이 없거나 확인할 수 없어, 대상에 이어진 것을 모두 확인할 수 없습니다.",
    "target_routines": "대상에 저장 프로시저나 함수가 있어, 교체 뒤에도 제대로 동작하는지 확인할 수 없습니다.",
    "target_events": "대상에 예약 이벤트가 있어, 교체 뒤에도 제대로 동작하는지 확인할 수 없습니다.",
    "target_triggers": "대상 테이블에 트리거가 있어 교체할 수 없습니다.",
    "linked_target_only": "덤프에 없는 테이블이나 뷰가 외래 키나 뷰로 덤프 테이블과 이어져 있어, 그대로 둔 채 교체할 수 없습니다.",
    "saved_view_alias": "이전 교체에서 남긴 옛 뷰가 있습니다. 작업 목록에서 이전 Import 기록의 '복원 백업 관리'에서 먼저 정리하세요.",
    "previous_copy_leftover": "이전 복사본이나 백업에 남은 뷰가 덤프 테이블을 읽고 있습니다. 작업 목록에서 이전 Import 기록의 '복원 백업 관리'에서 먼저 정리하세요.",
    "copy_checks_unavailable": "대상을 확인하지 못해, 먼저 복사본에서 검증할 수 있는지 알 수 없습니다.",
    # Preview blockers
    "dump_invalid": "덤프 파일 내용에 문제가 있어 넣을 수 없습니다.",
    "untranslatable_table": "이 테이블은 대상 DB 종류에서 만들 수 없습니다.",
    "spatial_unsupported": "위치·도형(공간) 열은 이 덤프에서 대상으로 넣을 수 없습니다.",
    "temporal_unsupported": "대상 DB 종류에는 이 날짜·시간 형식을 그대로 담을 열이 없습니다.",
    "no_tables": "넣을 테이블이 없습니다.",
    "create_privilege_missing": "대상 DB(스키마)가 없는데 이 계정은 새로 만들 권한이 없습니다.",
    "drop_privilege_missing": "이 계정은 대상 테이블을 지울 권한이 없습니다.",
    "dependent_view": "대상의 뷰가 이 테이블을 쓰고 있어 교체할 수 없습니다.",
    "target_only_fk_child": "덤프에 없는 대상 테이블이 외래 키로 이 테이블을 참조하고 있어 교체할 수 없습니다.",
    "dependent_object": "대상의 다른 객체(시퀀스, 함수 등)가 이 테이블에 기대고 있어 교체할 수 없습니다.",
    "check_name_collision": "같은 이름의 CHECK 제약이 덤프에 없는 대상 테이블에 이미 있습니다.",
    "name_collision": "대상에서 뷰 같은 다른 객체가 이 이름을 쓰고 있어 테이블로 바꿀 수 없습니다.",
    "external_parent_missing": "이 테이블이 참조하는 부모 테이블이 덤프에도 대상에도 없습니다.",
    # Preview warnings
    "unsynchronized_snapshot": "덤프의 테이블들이 같은 시점에 내보내지지 않아 서로 맞지 않을 수 있습니다.",
    "views_outside_snapshot": "뷰 정의는 테이블 데이터와 같은 시점에 담기지 않았습니다.",
    "omitted_object": "내보낼 때 일부 객체가 빠졌습니다.",
    "visibility_unproven": "내보낸 계정의 권한으로는 트리거, 저장 프로시저·함수, 예약 이벤트, 테이블과 열이 모두 보였는지 알 수 없어 덤프에 빠진 것이 있을 수 있습니다.",
    "source_dump_warning": "덤프를 만들 때 남긴 경고가 있습니다.",
    "not_strict": "이 덤프는 내보낼 때 일부를 담지 못했거나, 다 담았는지 확인하지 못했습니다. 이유는 '자세히'에 있습니다.",
    "legacy_timezone": "예전 덤프라 시간대가 기록되어 있지 않습니다. 고른 시간대나 대상 서버 기본값으로 시간을 읽습니다.",
    "legacy_dump": "예전 형식 덤프라 일부 파일의 무결성 정보가 없습니다.",
    "create_privilege_unproven": "대상 DB(스키마)가 없는데 이 계정이 만들 수 있는지 권한 목록으로는 알 수 없습니다.",
    "drop_privilege_unproven": "이 계정이 대상 테이블을 지울 수 있는지 권한 목록으로는 알 수 없습니다.",
    "view_dependencies_unknown": "교체할 테이블을 쓰는 뷰를 확인하지 못했습니다(MySQL 8.0.13 이상 필요).",
}
_REPORT_NOT_SAVED = "Import 보고서를 저장하지 못했습니다. 보고서 내용은 최신이 아닙니다."

# A known `code:` where the core puts one: at the start (after the importer's own prefix) or after
# "; additionally: ", so code text inside a path or a server message is not taken. The scope of
# `code: name: ...` is kept when it is one word (classified_import_error).
_CORE_CODE_TOKEN = re.compile(
    r"(?:^\s*(?:Rust DB Core import (?:미리보기 )?오류: |Import (?:미리보기 )?오류: "
    r"|Safe restore failed; original database was not changed\. )?"
    r"|; additionally: )"
    r"(" + "|".join(map(re.escape, CORE_CODE_SENTENCES)) + r")\s*:(?:\s*([^\s:]+):\s)?",
    re.IGNORECASE,
)
_TRAILING_CORE_CODE = re.compile(r"\(error_code=([a-z_]+)\)")


def describe_core_code(code: object, name: object = "") -> Optional[str]:
    """The sentence for one core code, prefixed with the table (or other object) it names; None when unknown."""
    sentence = CORE_CODE_SENTENCES.get(str(code or "").lower())
    if sentence is None:
        return None
    sentence = translate_text(sentence)
    return f"{name}: {sentence}" if name else sentence


# How the core's manifest warnings begin (adapters.rs WARNING_*) and the code Import gives
# them (import.rs manifest_warning_code).
_MANIFEST_WARNING_CODES = (
    ("Workers used independent snapshots", "unsynchronized_snapshot"),
    ("View definitions were collected outside the table data snapshot", "views_outside_snapshot"),
    ("Object not exported:", "omitted_object"),
    ("Non-transactional tables excluded from export:", "omitted_object"),
    ("The export account's privileges do not prove it sees every object", "visibility_unproven"),
)


def manifest_warning_code(warning: str) -> Tuple[str, str]:
    """(code, detail) of one manifest warning: an omitted object keeps its name, an unknown warning its text."""
    for prefix, known in _MANIFEST_WARNING_CODES:
        if warning.startswith(prefix):
            return known, (warning[len(prefix):].strip() if known == "omitted_object" else "")
    return "source_dump_warning", warning


def describe_manifest_warning(warning: str) -> str:
    """The sentence for one manifest warning; an omitted object keeps its name and an unknown
    warning its text, e.g. "내보낼 때 일부 객체가 빠졌습니다. (trigger:orders:trg)"."""
    code, detail = manifest_warning_code(warning)
    sentence = describe_core_code(code)
    return f"{sentence} ({detail})" if detail else sentence


def describe_core_failure(message: object) -> Optional[str]:
    """The sentences for the core codes in a failure text; None when it carries none."""
    text = str(message or "")
    found = [describe_core_code(match.group(1), match.group(2) or "") for match in _CORE_CODE_TOKEN.finditer(text)]
    if not found and (trailing := _TRAILING_CORE_CODE.search(text)):
        found = [describe_core_code(trailing.group(1))]
    sentences = list(dict.fromkeys(sentence for sentence in found if sentence))
    # Only beside a code sentence: an uncoded text keeps the core's own wording, which says so.
    if sentences and "could not be persisted" in text:
        sentences.append(translate_text(_REPORT_NOT_SAVED))
    return " ".join(sentences) or None


IMPORT_PREVIEW_CAPABILITY = "dump.import_preview"
_UNKNOWN_TARGET_MESSAGE = "원본 스키마를 확정할 수 없습니다. 대상 데이터베이스/스키마를 직접 선택하세요."
_MISSING_MANIFEST_MESSAGE = "TunnelForge Rust dump manifest를 찾을 수 없습니다."

_UNKNOWN_COPY_BLOCKER = "복사본에서 검증할 수 없는 다른 이유가 있습니다. Import 보고서에서 확인하세요."
_UNVERIFIED_COPY_MESSAGE = "복사본 검증 결과를 확인할 수 없습니다. 원본과 복사본의 상태를 Import 보고서에서 확인하세요."


def describe_copy_blockers(blockers: object, unknown: str = _UNKNOWN_COPY_BLOCKER) -> List[str]:
    """One line per copy_blockers code (why verify-in-copy stopped), naming the tables or views it concerns.

    Preview ``blockers`` share the shape; ``unknown`` words a code without a sentence."""
    grouped: Dict[str, List[str]] = {}
    for item in blockers if isinstance(blockers, list) else []:
        if isinstance(item, dict):
            names = grouped.setdefault(str(item.get("code") or "").lower(), [])
            if item.get("table") and str(item["table"]) not in names:
                names.append(str(item["table"]))
    lines = []
    for code, names in grouped.items():
        sentence = translate_text(CORE_CODE_SENTENCES.get(code) or unknown)
        shown = ", ".join(names[:10]) + (f" … (+{len(names) - 10})" if len(names) > 10 else "")
        lines.append(f"- {sentence}" + (f" ({shown})" if names else ""))
    return lines


class RustDumpImporter(_RustDumpClientBase):
    """Rust DB Core backed dump importer."""

    def _import_payload(
        self,
        input_dir: str,
        target_schema: str,
        threads: int,
        timezone_sql: Optional[str],
        use_source_timezone: bool,
    ) -> Optional[dict]:
        """The dump.import payload without its mode; None when the target namespace is unknown."""
        if not target_schema:
            return None
        payload = {
            "target": self._endpoint(target_schema).to_payload(),
            "input_dir": input_dir,
            "threads": max(1, int(threads)),
            "strict_manifest": True,
            "use_source_timezone": use_source_timezone,
        }
        if timezone_sql:
            payload["timezone_sql"] = timezone_sql
        return payload

    def preview_import(
        self,
        input_dir: str,
        target_schema: Optional[str] = None,
        threads: int = DEFAULT_DUMP_THREADS,
        timezone_sql: Optional[str] = None,
        use_source_timezone: bool = True,
    ) -> Tuple[bool, str, dict]:
        """What a replace import would do, read from the target without changing it.

        Nothing is sent to a core whose ``service.hello`` does not list ``dump.import_preview``.
        """
        try:
            manifest_path = Path(input_dir) / "_tunnelforge_dump.json"
            if not manifest_path.exists():
                return False, _MISSING_MANIFEST_MESSAGE, {}
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = self._import_payload(
                input_dir,
                target_schema or dump_original_namespace(manifest, self.config.engine),
                threads,
                timezone_sql,
                use_source_timezone,
            )
            if payload is None:
                return False, _UNKNOWN_TARGET_MESSAGE, {}
            if IMPORT_PREVIEW_CAPABILITY not in (self.facade.hello().get("capabilities") or []):
                return False, "Import 엔진이 이 버전보다 오래되어 실행 전 확인을 할 수 없습니다. TunnelForge를 다시 설치하세요.", {}
            return True, "", self.facade.preview_import(payload)
        except DbCoreServiceError as exc:
            self._remember_error_code(exc)
            return False, f"Rust DB Core import 미리보기 오류: {exc}", {}
        except Exception as exc:
            self._remember_error_code(exc)
            return False, f"Import 미리보기 오류: {exc}", {}
        finally:
            _shutdown_owned_facade(self.facade, self._owns_facade)

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
        *,
        import_mode: str,
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
        verify_only: bool = False,
    ) -> Tuple[bool, str, dict]:
        """``import_mode`` is ``replace`` (the dialog's default) or ``safe`` (verify in a copy first); it has
        no default, so no caller falls into either one by accident. ``verify_only`` (safe mode): the copy is
        only verified, never swapped in, so the core skips the checks that guard the swap."""
        import_results: dict = {}
        try:
            manifest_path = Path(input_dir) / "_tunnelforge_dump.json"
            if not manifest_path.exists():
                return False, _MISSING_MANIFEST_MESSAGE, import_results

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

            payload = self._import_payload(
                input_dir, target_schema or source_schema, threads, timezone_sql, use_source_timezone,
            )
            if payload is None:
                return False, _UNKNOWN_TARGET_MESSAGE, import_results
            payload["mode"] = import_mode
            if verify_only:
                payload["verify_only"] = True
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
                state = {key: result.get(key) for key in ("status", "verified", "original_unchanged", "cutover_pending", "ready_for_switch", "blockers", "namespace_existed", "namespace_created", "candidate_created", "restore_id", "report_path", "plan_digest")}
                state.update(event="safe_restore_ready", candidate_target=candidate,
                             original_target=restore_target_connection_info(result.get("original_target")))
                if raw_output_callback:
                    raw_output_callback(json.dumps(state, ensure_ascii=False))
                if result.get("status") == "failed_original_untouched":
                    # A refused or failed safe restore is a result, not an error: keep the core's reason.
                    # A failed final report write keeps the first error and says so only in `message`.
                    reason = str((result.get("message") if result.get("report_write_failed") else result.get("error"))
                                 or result.get("message") or "")
                    self.last_error_code = classify_error_code(result.get("error_code"), reason)
                    stops = describe_copy_blockers(result.get("copy_blockers"))
                    if stops:
                        # Stopped before making a copy: one sentence per reason; the core's text is in the report.
                        return False, "\n".join([describe_core_code("copy_refused"), *stops]), import_results
                    return False, "\n".join([reason, *(str(item) for item in result.get("blockers") or [])]), import_results
                new_target = result.get("status") == "completed_new_target" and result.get("namespace_existed") is False and result.get("namespace_created") is True
                if not ((new_target or result.get("status") in ("ready_for_switch", "ready_for_review"))
                        and result.get("verified") is True and result.get("original_unchanged") is True
                        and result.get("cutover_pending") is (not new_target) and candidate.get("database")
                        and (self.config.engine != "postgresql" or candidate.get("schema"))):
                    return False, _UNVERIFIED_COPY_MESSAGE, import_results

            for table in tables_to_import:
                import_results[table] = {"status": "done", "message": ""}
            rows = int(result.get("rows_imported") or 0)
            views_imported = result.get("views_imported") or []
            views_failed = result.get("views_failed") or []
            views_skipped = result.get("views_skipped_cross_engine") or []
            message = f"Rust DB Core import 완료: {len(tables_to_import)}개 테이블, {rows:,} rows"
            if import_mode == "safe":
                candidate_name = candidate.get("schema") if self.config.engine == "postgresql" else candidate.get("database")
                message = f"복사본 {candidate_name}에 넣고 검증했습니다. 원본은 그대로이고 교체를 기다립니다."
                if new_target:
                    message = f"새 대상 {candidate_name}을 만들어 넣고 검증했습니다."
                if result.get("status") == "ready_for_review":
                    message += " 교체 전에 따로 확인할 것이 있습니다."
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
            if import_mode != "safe" and result.get("namespace_created") is True:
                if self.config.engine == "postgresql":
                    # Without a configured database the schema lands in the `postgres` database.
                    message += f"\n새 스키마 {payload['target']['database']}.{payload['target']['schema']}을 만들어 넣었습니다"
                else:
                    message += f"\n새 DB {payload['target']['database']}을 만들어 넣었습니다"
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
    include_fk_parents: bool = False,
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
    *,
    import_mode: str,
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
