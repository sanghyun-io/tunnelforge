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
_UNSYNCHRONIZED_SNAPSHOT_POLICIES = frozenset({
    "mysql_parallel_unsynchronized_snapshots",
    "mysql_parallel_no_backup_lock_consistent_snapshot",
})
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
    "table_inheritance": "테이블 {}은 상속이나 파티션으로 다른 테이블과 이어져 있어 아직 덤프에 담을 수 없습니다.",
    "key_rule": "제약·인덱스 {}의 NULL 처리·검사 시점·배제 규칙을 덤프에 담을 수 없습니다",
}
# dump.rs LOSSY_SCHEMA_KINDS: refused without a way to leave them out (bypassable false).
LOSSY_REFUSAL_KINDS = frozenset({
    "generated_column", "check_constraint", "unsupported_default", "custom_type", "unsupported_index", "cross_schema_fk", "table_inheritance",
    "key_rule",
})
_SCHEMA_REFUSAL_REASONS = {
    "routine": "저장 프로시저·함수 {}: 덤프에 담을 수 없습니다",
    "event": "예약 이벤트 {}: 덤프에 담을 수 없습니다",
    "materialized_view": "구체화 뷰 {}: 덤프에 담을 수 없습니다",
}
_UNKNOWN_TABLE_REFUSAL = "이 객체는 담을 수 없습니다"


def describe_export_refusal(objects: object, reasons: Optional[Dict[str, str]] = None,
                            object_tables: Optional[Dict[str, str]] = None) -> List[str]:
    """One plain sentence per refused object, e.g. "테이블 orders: 트리거(trg)는 덤프에 담을 수 없습니다".

    An unknown code still names its table; the codes themselves go behind "자세히"."""
    lines = []
    for item in objects if isinstance(objects, list) else []:
        kind, _, rest = str(item).partition(":")
        if str(item) in (object_tables or {}):
            table = str(object_tables[str(item)])
            name = rest[len(table) + 1:] if rest.startswith(table + ":") else ""
        elif kind in ("policy", "table_inheritance"):
            table, name = rest, ""
        elif kind == "rule":
            table, _, name = rest.rpartition(":")
        else:
            table, _, name = rest.partition(":")
        code = (reasons or {}).get(str(item)) or {
            "policy": "row_security_unsupported", "rule": "rule_unsupported",
            "table_inheritance": "table_inheritance_unsupported",
        }.get(kind)
        if kind in _SCHEMA_REFUSAL_REASONS:
            reason = describe_core_code((reasons or {}).get(str(item)), rest)
            lines.append(reason or translate_text(_SCHEMA_REFUSAL_REASONS[kind]).format(rest))
        elif code in ("row_security_unsupported", "rule_unsupported", "table_inheritance_unsupported"):
            lines.append(describe_core_code(code, table))
        elif name:
            code = (reasons or {}).get(str(item))
            reason = (translate_text(CORE_CODE_SENTENCES[code]).format(name) if code in CORE_CODE_SENTENCES
                      else translate_text(_TABLE_REFUSAL_REASONS.get(kind, _UNKNOWN_TABLE_REFUSAL)).format(name))
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
                    **({"reasons": dict(event["reasons"])} if event.get("reasons") else {}),
                    **({"object_tables": dict(event["object_tables"])} if event.get("object_tables") else {}),
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
        for key,template in [("triggers","트리거 {}개"),("routines","루틴 {}개"),("events","이벤트 {}개")]:
            if result.get(key):
                message += ", " + translate_text(template).format(int(result[key]))
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
    "row_security_blocks_read": "테이블 {}에 행 수준 보안(RLS)이 걸려 있어 이 계정으로는 모든 행을 읽을 수 없습니다. 슈퍼유저나 BYPASSRLS 권한이 있는 계정으로 다시 시도하세요.",
    "identity_always_unsupported": "열 {}은 GENERATED ALWAYS identity라서 아직 덤프에 담을 수 없습니다.",
    "sequence_options_unsupported": "열 {}의 시퀀스 설정(시작값·증가값·최솟값·최댓값·순환·형식)이 기본값과 다르거나 시퀀스가 이 테이블 소유가 아니어서 덤프에 담을 수 없습니다.",
    "default_expression_unsupported": "열 {}의 기본값이 상수·now()·gen_random_uuid() 같은 옮길 수 있는 형태가 아니어서 덤프에 담을 수 없습니다.",
    "check_constraint_unsupported": "PostgreSQL CHECK 제약 {}은 아직 덤프에 담을 수 없습니다.",
    "custom_type_unsupported": "열 {}이 enum·domain·확장 형식 같은 사용자 정의 형식을 써서 아직 덤프에 담을 수 없습니다.",
    "index_shape_unsupported": "인덱스 {}는 식·부분·btree가 아닌·내림차순·INCLUDE 인덱스라 아직 덤프에 담을 수 없습니다.",
    "cross_schema_fk_unsupported": "외래 키 {}가 다른 스키마의 테이블을 가리켜 덤프에 담을 수 없습니다.",
    "materialized_view_unsupported": "구체화 뷰 {}는 아직 덤프에 담을 수 없습니다. 빼고 내보내려면 고급 설정의 \"담을 수 없는 객체는 빼고 내보내기\"를 켜세요.",
    "postgres_trigger_unsupported": "PostgreSQL 트리거 {}는 아직 덤프에 담을 수 없습니다. 빼고 내보내려면 고급 설정의 \"담을 수 없는 객체는 빼고 내보내기\"를 켜세요.",
    "postgres_routine_unsupported": "PostgreSQL 함수·프로시저 {}는 아직 덤프에 담을 수 없습니다. 빼고 내보내려면 고급 설정의 \"담을 수 없는 객체는 빼고 내보내기\"를 켜세요.",
    "row_security_unsupported": "테이블 {}에 행 수준 보안(RLS) 정책이 있어 아직 덤프에 담을 수 없습니다. 정책 없이 내보내려면 고급 설정의 '담을 수 없는 객체는 빼고 내보내기'를 켜고, 가져온 뒤 정책을 다시 만드세요.",
    "rule_unsupported": "테이블 {}에 걸린 규칙(RULE)은 아직 덤프에 담을 수 없습니다. 규칙 없이 내보내려면 고급 설정의 '담을 수 없는 객체는 빼고 내보내기'를 켜세요.",
    "table_inheritance_unsupported": "테이블 {}은 상속이나 파티션으로 다른 테이블과 이어져 있어 아직 덤프에 담을 수 없습니다.",
    "comments_not_exported": "테이블·열 주석(COMMENT)은 덤프에 담지 않았습니다: {}",
    "generated_expression_unsafe": "계산 열 {}의 식에 덤프로 옮길 수 없는 부분(;, 주석, 괄호 밖 쉼표, SELECT 같은 문장 키워드 등)이 있어 덤프에 담을 수 없습니다.",
    "generated_dependency_unsupported": "계산 열 {}의 식이 PostgreSQL 기본 함수가 아닌 함수나 형식을 써서 아직 덤프에 담을 수 없습니다.",
    "generated_expression_changed": "대상 DB가 계산 열의 식을 원본과 다르게 저장해 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다.",
    "program_name_resolution_changed": "대상 스키마에 이름이 같은 함수나 연산자가 있어, 계산 열이나 트리거가 원본과 다른 객체를 부르게 됩니다. 대상 스키마의 그 객체 이름을 바꾸거나 지운 뒤 다시 가져오세요. 대상 테이블은 바뀌지 않았습니다.",
    "generated_virtual_unsupported_on_target": "대상 PostgreSQL이 18보다 낮아 VIRTUAL 계산 열을 만들 수 없습니다. 대상 테이블은 바뀌지 않았습니다.",
    "generated_values_recomputed": "계산 열 값은 대상 DB에서 다시 계산했습니다. 원본 값과 같은지는 복사본 검증에서 확인할 수 있습니다: {}",
    "deferrable_primary_key_unsupported": "기본 키 {}는 검사를 커밋 때로 미루도록(DEFERRABLE) 만들어져 있어 아직 덤프에 담을 수 없습니다.",
    "exclusion_constraint_unsupported": "배제 제약(EXCLUDE) {}은 btree 인덱스로 열을 같음(=)으로만 비교하는 형태가 아니어서 아직 덤프에 담을 수 없습니다.",
    "nulls_not_distinct_unsupported_on_target": "대상 PostgreSQL이 15보다 낮아, NULL끼리도 중복으로 보는 UNIQUE(NULLS NOT DISTINCT)를 만들 수 없습니다. PostgreSQL 15 이상인 대상으로 가져오세요. 대상 테이블은 바뀌지 않았습니다.",
    "unsupported_constraint_behavior": "NULL끼리도 중복으로 보는 UNIQUE(NULLS NOT DISTINCT)나 배제 제약(EXCLUDE) 같은 PostgreSQL 키 규칙은 MySQL에서 같은 규칙으로 만들 수 없습니다. 이 테이블을 빼고 진행하거나, 원본에서 그 설정을 없앤 뒤 다시 시도하세요.",
    "constraint_deferral_not_kept": "MySQL은 키를 바로 검사하므로, 커밋 때 검사하던 PostgreSQL 키(DEFERRABLE)도 바로 검사하는 키가 됩니다. 행을 넣는 순서에 기대던 작업이 있으면 확인하세요: {}",

    "target_trigger_hidden": "교체할 테이블에 지금 계정으로는 보이지 않는 트리거가 있어 교체하지 않았습니다. 원본은 그대로입니다. TRIGGER 권한이 있는 계정으로 다시 교체하세요.",
    "rollback_routine_binlog_unsafe": "원래 함수를 지금 서버 설정으로는 다시 만들 수 없어 복구를 시작하지 않았습니다. log_bin_trust_function_creators=1로 바꾸거나 SUPER 권한이 있는 계정으로 복구하세요.",
    "rollback_trigger_privilege_missing": "원래 트리거를 다시 만들 TRIGGER 권한이 없어 복구를 시작하지 않았습니다. TRIGGER 권한이 있는 계정으로 복구하세요.",
    "rollback_trigger_binlog_privilege": "원래 트리거를 다시 만들려면 log_bin_trust_function_creators=1 설정이나 SUPER 권한이 필요해 복구를 시작하지 않았습니다.",
    "target_routine_binlog_unsafe": "대상에 같은 이름의 함수가 있는데, 지금 서버 설정으로는 교체가 실패했을 때 그 함수를 원래대로 다시 만들 수 없어 복사 전에 멈췄습니다. 그 함수에 DETERMINISTIC 같은 선언을 추가하거나 log_bin_trust_function_creators=1로 바꾼 뒤 다시 시도하세요. '먼저 복사본에서 검증하고 교체'를 끄면 이 함수는 바꾸지 않고 가져옵니다.",
    "copy_routine_binlog_unsafe": "지금 서버 설정과 계정 권한으로는 이 함수를 만들 수 없어 복사 전에 멈췄습니다. log_bin_trust_function_creators 설정과 함수의 DETERMINISTIC·SQL 접근 선언을 확인하세요. '먼저 복사본에서 검증하고 교체'를 끄면 이 함수만 건너뛰고 가져옵니다.",
    "target_trigger_binlog_privilege": "교체할 테이블에 이미 트리거가 있어, 교체하는 동안 그 트리거를 지웠다가 다시 만들어야 합니다. 지금 서버 설정과 계정 권한으로는 트리거를 만들 수 없어 복사 전에 멈췄습니다. log_bin_trust_function_creators=1로 바꾸거나 SUPER 권한이 있는 계정으로 가져오세요.",
    "target_program_unsavable": "교체 중에 저장했다가 되돌려야 하는 대상 프로그램을 저장할 수 없어 복사 전에 멈췄습니다.",
    "view_calls_dump_routine": "덤프의 뷰가 덤프의 함수를 부릅니다. 복사본 검증에서는 함수를 교체한 뒤에 만들기 때문에 이 뷰를 미리 확인할 수 없어 복사 전에 멈췄습니다. '먼저 복사본에서 검증하고 교체'를 끄고 가져오세요.",
    "rollback_triggers_recreated": "복구 중 트리거를 다시 만들었습니다. 트리거의 생성 기록이 달라졌을 수 있습니다.",

    "program_restore_definer_changed": "이전 정의를 복구하면서 정의자가 가져오는 계정으로 바뀌었습니다. 권한과 소유 계정을 확인하세요.",
    "program_restore_definer_may_change": "다른 계정의 정의자를 지정할 권한이 없어 교체 실패 후 복구하면 정의자가 가져오는 계정으로 바뀔 수 있습니다.",
    "program_restore_unavailable": "대상의 이전 정의를 안전하게 복구할 수 있는지 확인하지 못해 이 객체는 교체하지 않았습니다. 보고서에서 세션 설정과 권한을 확인하세요.",
    "program_event_dropped": "과거의 일회성 이벤트를 서버가 생성 직후 삭제했습니다. 교체 실패로 처리하고 이전 정의 복구 결과를 보고합니다.",
    "routine_grants_reset": "동명 루틴을 교체하면 그 루틴에 직접 부여한 EXECUTE·ALTER ROUTINE 권한이 사라질 수 있습니다. 필요한 권한을 다시 부여하세요.",
    "program_session_unavailable": "원본 세션 설정을 적용할 수 없어 이 객체는 바꾸지 않고 남겨 둡니다(결과는 일부 완료). 대상 서버의 시간대 설정을 확인하세요.",

    "events_enabled": "가져온 뒤 바로 실행될 수 있는 이벤트가 있습니다. 이름과 일정을 확인하세요.",
    "routine_body_unreadable": "루틴 {}의 본문을 읽지 못해 담지 않았습니다. SHOW_ROUTINE 권한 또는 원본 소유 계정이 필요합니다.",
    "program_body_unreadable": "저장 프로그램 {}의 정의를 읽지 못했습니다. 원본 객체와 조회 권한을 확인하세요.",
    "routine_binlog_unsafe": "함수를 바이너리 로그 설정 때문에 만들지 못하면 건너뛰며, 대상의 같은 이름 함수는 유지합니다. log_bin_trust_function_creators 설정과 함수의 DETERMINISTIC·SQL 접근 선언을 확인하세요.",
    "program_probe_cleanup_failed": "확인용 루틴을 지우지 못해 시작하지 않았습니다. 보고서의 남은 객체 이름과 ALTER ROUTINE 권한을 확인하세요.",
    "program_probe_cleanup_unproven": "확인용 루틴을 지우려면 ALTER ROUTINE 권한이 필요합니다.",
    "program_privilege_missing": "저장 프로그램을 만들거나 되돌릴 권한이 없습니다. TRIGGER·CREATE ROUTINE·ALTER ROUTINE·EVENT 권한을 확인하세요.",
    "program_replace_privilege_unproven": "다른 계정이 만든 저장 프로그램은 교체하지 못할 수 있습니다.",
    "program_event_system_timezone": "이 이벤트의 SYSTEM 시간대는 대상 서버의 시스템 시간대로 해석됩니다.",

    "table_definition_unrestorable": "이 테이블의 정의로는 가져오기용 테이블을 만들 수 없어 내보내지 않았습니다. 원본 테이블 정의를 확인하세요.",
    "program_server_warning": "저장 프로그램을 만들 때 서버가 경고를 보냈습니다.",
    "program_create_failed": "데이터를 넣은 뒤 저장 프로그램을 만들지 못했습니다. Import 보고서에서 실패한 객체와 이전 정의 복구 결과를 확인하세요.",
    "trigger_body_unproven": "트리거 {}의 본문이 원본과 같은지 확인하지 못해 담지 않았습니다. 원본 트리거 정의를 확인하세요.",
    "program_definition_invalid": "루틴·이벤트 {}의 정의나 생성 설정을 안전하게 읽을 수 없습니다. 원본 객체의 정의와 문자셋·시간대를 확인한 뒤 다시 내보내세요.",
    "program_charset_unsupported": "저장 프로그램 {}의 문자셋으로는 정의를 안전하게 복원할 수 없습니다. 원본 객체를 utf8mb4 문자셋으로 다시 만든 뒤 내보내세요.",
    "trigger_name_conflict": "같은 이름의 트리거가 대상의 다른 테이블에 있습니다. 그 트리거를 삭제하거나 다른 이름으로 다시 만든 뒤 가져오세요.",
    "trigger_binlog_privilege": "트리거를 만들려면 log_bin_trust_function_creators=1 설정 또는 SUPER 권한이 필요합니다. 트리거를 제외하려면 고급 설정의 '트리거 없이 테이블만 가져오기'를 켜세요.",
    "trigger_privilege_missing": "이 계정에 트리거를 만들 TRIGGER 권한이 없습니다.",
    "program_probe_failed": "저장 프로그램을 만들 수 없어 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다.",
    "program_source_schema_reference": "저장 프로그램 본문이 원본 DB 이름을 참조합니다. 같은 이름 DB로 가져오거나 정의에서 DB 이름을 빼세요.",
    "programs_skipped_cross_engine": "다른 DB 종류로 가져오므로 덤프의 트리거·루틴·이벤트를 만들지 않습니다.",
    "programs_skipped_partial": "선택한 테이블에 딸린 객체만 가져오므로 나머지 객체는 만들지 않습니다.",
    "programs_skipped_toggle": "'트리거 없이 테이블만 가져오기'를 켜서 덤프의 트리거를 만들지 않습니다.",
    "program_definer_replaced": "저장 프로그램 정의자가 가져오는 계정으로 바뀝니다.",
    "program_database_collation_changed": "대상 DB의 기본 정렬 규칙이 저장 프로그램 원본과 다릅니다.",

    "generated_column_unproven": "계산 열 {}의 식이 원본과 같은지 확인하지 못했습니다. 이모지처럼 원본 DB가 다시 표시하지 못하는 문자가 있을 수 있습니다. 그 문자를 쓰지 않도록 식을 고치거나 이 테이블의 체크를 풀어 나머지만 내보내세요.",
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
    "candidate_fk_orphans": "복사본에 부모 행이 없는 자식 행이 있어 교체하지 않았습니다.",
    "legacy_bit_unverifiable": "예전 형식 덤프라 BIT 열을 복사본에서 검증할 수 없습니다. 이 버전으로 다시 내보내세요.",
    "cross_engine": "다른 DB 종류에서 만든 덤프는 복사본에서 검증할 수 없습니다.",
    "partial_table_filter": "일부 테이블만 넣을 때는 복사본에서 검증할 수 없습니다.",
    # Why verify-in-copy stopped before making a copy (copy_blockers)
    "copy_refused": "복사본을 만들기 전에 멈췄습니다. 원본은 그대로입니다.",
    "copy_create_privilege_missing": "이 계정에 복사본과 백업을 둘 DB(PostgreSQL은 스키마)를 만들 권한이 없거나, 권한 목록으로 확인할 수 없습니다.",
    "metadata_visibility_unproven": "이 계정에 서버 전체를 조회하는 권한(SELECT, SHOW VIEW, PROCESS)이 없거나 확인할 수 없어, 대상에 이어진 것을 모두 확인할 수 없습니다.",
    "target_routines": "대상에 저장 프로시저나 함수가 있어, 교체 뒤에도 제대로 동작하는지 확인할 수 없습니다.",
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
    "program_finalize_failed": "객체를 만들었지만 활성화나 마무리 확인을 끝내지 못했습니다. 보고서에서 남아 있는 객체와 실행 상태를 확인하세요.",
    "event_privilege_unproven": "이벤트 생성 권한을 확인하지 못했습니다. 권한이 없으면 이 객체는 만들지 못하고 결과가 일부 완료로 남을 수 있습니다.",
    "program_execution_account": "이 객체들은 가져오는 계정의 권한으로 실행됩니다. 신뢰하는 덤프인지 확인하세요.",
    "unsupported_generated_column": "계산 열은 덤프를 만든 DB와 같은 종류의 대상으로만 복원할 수 있습니다. 같은 종류의 DB로 가져오세요.",
    "unsupported_invisible_column": "이 테이블의 숨김 열은 MySQL 대상에서만 복원할 수 있습니다. MySQL 대상으로 가져오세요.",
    "trigger_definition_invalid": "트리거 {}의 정의나 생성 설정을 안전하게 읽을 수 없습니다. 원본 트리거 정의를 확인한 뒤 다시 내보내세요.",
    "trigger_create_failed": "데이터를 넣은 뒤 트리거를 만들다가 Import가 멈췄습니다. 앞서 완료한 테이블은 유지됩니다. 보고서에서 트리거가 없는 테이블을 확인하세요.",
    "trigger_finalize_failed": "트리거는 만들었지만 마무리 확인 중 Import가 멈췄습니다. 보고서에서 만들어진 트리거와 남은 테이블을 확인하세요.",
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
_CANDIDATE_FK_ORPHANS_FIELDS = re.compile(r"\bcandidate_fk_orphans:\s*fields=(\{[^\n]*\})")


def describe_core_code(code: object, name: object = "") -> Optional[str]:
    """The sentence for one core code, prefixed with the table (or other object) it names; None when unknown."""
    sentence = CORE_CODE_SENTENCES.get(str(code or "").lower())
    if sentence is None:
        return None
    sentence = translate_text(sentence)
    if "{}" in sentence:
        return sentence.format(name) if name else re.sub(r"\{\}(?:의|은|는|이|가|을|를)?\s*", "", sentence).strip()
    return f"{name}: {sentence}" if name else sentence


def display_program_name(value: object) -> str:
    kind, _, rest = str(value).partition(":")
    if kind == "trigger":
        return rest.replace(":", ".", 1)
    templates = {"routine": "루틴 {}", "event": "이벤트 {}", "view": "뷰 {}"}
    return translate_text(templates[kind]).format(rest) if kind in templates else str(value)


def describe_program_warnings(items: object) -> List[str]:
    lines = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        code, name = item.get("code"), item.get("name", "")
        sentence = describe_core_code(code, name)
        if sentence is None:
            sentence = describe_core_code("program_server_warning", name) + f" ({code}): {item.get('message', '')}"
        elif item.get("source") is not None and item.get("target") is not None:
            sentence += f" ({item['source']} → {item['target']})"
        lines.append(sentence)
    return lines


def describe_program_results(result: dict) -> Tuple[List[str], List[str]]:
    """Shared user sentences and sanitized details for failed or restored programs."""
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    lines, details, warnings = [], [], list(result.get("program_warnings") or [])
    for failure in result.get("programs_failed") or []:
        if not isinstance(failure, dict):
            continue
        name = sanitize_local_diagnostic(str(failure.get("name") or ""))
        line = describe_core_code(failure.get("code") or "program_create_failed", name) or describe_core_code("program_create_failed", name)
        if "previous_restored" in failure:
            line += " " + translate_text("이전 정의 복구 성공" if failure["previous_restored"] else "이전 정의 복구 실패")
        lines.append(line)
        for key in ("error", "restore_error"):
            if failure.get(key):
                details.append(f"{name}: " + sanitize_local_diagnostic(str(failure[key])))
        warnings.extend(failure.get("warnings") or [])
        warnings.extend(failure.get("restore_warnings") or [])
    if result.get("program_verification_error"):
        details.append(sanitize_local_diagnostic(str(result["program_verification_error"])))
    lines.extend(sanitize_local_diagnostic(line) for line in describe_program_warnings(warnings))
    return list(dict.fromkeys(lines)), list(dict.fromkeys(details))


# How the core's manifest warnings begin (adapters.rs WARNING_*) and the code Import gives
# them (import.rs manifest_warning_code).
_MANIFEST_WARNING_CODES = (
    ("comments_not_exported:", "comments_not_exported"),
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
            return known, (warning[len(prefix):].strip() if known in ("omitted_object", "comments_not_exported") else "")
    return "source_dump_warning", warning


def describe_manifest_warning(warning: str) -> str:
    """The sentence for one manifest warning; an omitted object keeps its name and an unknown
    warning its text, e.g. "내보낼 때 일부 객체가 빠졌습니다. (trigger:orders:trg)"."""
    code, detail = manifest_warning_code(warning)
    if code == "comments_not_exported":
        return describe_core_code(code, detail)
    sentence = describe_core_code(code)
    return f"{sentence} ({detail})" if detail else sentence


def describe_core_failure(message: object) -> Optional[str]:
    """The sentences for the core codes in a failure text; None when it carries none."""
    text = str(message or "")
    # Source scans can wrap this code in another diagnostic. Keep the full table name.
    if rls := re.search(r"(?:^|: )row_security_blocks_read: (.*?): postgresql ", text):
        return describe_core_code("row_security_blocks_read", rls.group(1))
    if fields_match := _CANDIDATE_FK_ORPHANS_FIELDS.search(text):
        try:
            fields = json.loads(fields_match.group(1))
            foreign_key = str(fields["foreign_key"])
            child_table = str(fields["child_table"])
            row_count = int(fields["row_count"])
            snapshot_policy = str(fields.get("snapshot_policy") or "")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            snapshot_reason = (
                translate_text("이 덤프는 테이블마다 다른 시점에 읽었다고 기록되어 있어, 내보내는 동안 원본이 바뀌어 생긴 차이일 수 있습니다. 원본에 원래 있던 행일 수도 있습니다.")
                if snapshot_policy in _UNSYNCHRONIZED_SNAPSHOT_POLICIES
                else translate_text("원본에 원래 있던 행일 수 있습니다.")
            )
            template = (
                "{child_table} 테이블의 행 {row_count}개가 가리키는 부모 행이 덤프에 없어 교체하지 않았습니다"
                "(외래 키 {foreign_key}). {snapshot_reason} 이런 행까지 그대로 가져오려면 "
                "'먼저 복사본에서 검증하고 교체'를 끄세요(mysqldump와 같은 동작). 아니면 모든 테이블을 같은 시점에 "
                "읽도록 다시 내보내거나, 원본에서 '고아 레코드 분석'으로 이 행을 찾아 정리하세요."
            )
            return translate_text(template).format(
                child_table=child_table,
                row_count=row_count,
                foreign_key=foreign_key,
                snapshot_reason=snapshot_reason,
            )
    found = [describe_core_code(match.group(1), match.group(2) or "") for match in _CORE_CODE_TOKEN.finditer(text)]
    if not found and (trailing := _TRAILING_CORE_CODE.search(text)):
        found = [describe_core_code(trailing.group(1))]
    sentences = list(dict.fromkeys(sentence for sentence in found if sentence))
    # Only beside a code sentence: an uncoded text keeps the core's own wording, which says so.
    if sentences and "could not be persisted" in text:
        sentences.append(translate_text(_REPORT_NOT_SAVED))
    return " ".join(sentences) or None


_INCOMPLETE_PROMOTION_SENTENCES = {
    "promoted_incomplete": "테이블은 교체했지만 저장 프로그램이 빠져 있습니다. 백업을 보존하고 복구하세요.",
    "failed_original_triggers_missing": "테이블은 교체하지 않았지만 원래 트리거를 되살리지 못했습니다. 보고서에 저장된 정의로 복구하세요.",
}


def describe_incomplete_promotion(status: object) -> str:
    return translate_text(_INCOMPLETE_PROMOTION_SENTENCES.get(str(status or ""), ""))


def describe_specific_promotion_failure(result: dict, message: object = "") -> Optional[str]:
    """Keep generic promotion wrappers behind the recorded outcome's guidance."""
    code = str(result.get("error_code") or "").lower()
    if code != "safe_promotion" and (sentence := describe_core_code(code)):
        return sentence
    for value in (message, result.get("error")):
        text = re.sub(r"^\s*safe_promotion:\s*", "", str(value or ""))
        if any(match.group(1).lower()=="safe_promotion" for match in _CORE_CODE_TOKEN.finditer(text)):
            continue
        if sentence := describe_core_failure(text):
            return sentence
    return None


_IMPORT_TOGGLE_NAMES = ("먼저 복사본에서 검증하고 교체", "트리거 없이 테이블만 가져오기")


def without_import_toggle_hints(sentence: str) -> str:
    # A toggle-dependent prefix can qualify an otherwise useful base sentence.
    for name in _IMPORT_TOGGLE_NAMES:
        sentence = sentence.replace(f"'{name}'를 켜서 ", "")
    names = (*_IMPORT_TOGGLE_NAMES, *(translate_text(name) for name in _IMPORT_TOGGLE_NAMES))
    return " ".join(part for part in re.split(r"(?<=[.!?])\s+", sentence)
                    if not any(name in part for name in names))


IMPORT_PREVIEW_CAPABILITY = "dump.import_preview"
_UNKNOWN_TARGET_MESSAGE = "원본 스키마를 확정할 수 없습니다. 대상 데이터베이스/스키마를 직접 선택하세요."
_MISSING_MANIFEST_MESSAGE = "TunnelForge Rust dump manifest를 찾을 수 없습니다."

_UNKNOWN_COPY_BLOCKER = "복사본에서 검증할 수 없는 다른 이유가 있습니다. Import 보고서에서 확인하세요."
_UNVERIFIED_COPY_MESSAGE = "복사본 검증 결과를 확인할 수 없습니다. 원본과 복사본의 상태를 Import 보고서에서 확인하세요."


def describe_copy_blockers(blockers: object, unknown: str = _UNKNOWN_COPY_BLOCKER, omit_verify_hint: bool = False) -> List[str]:
    """One line per copy_blockers code (why verify-in-copy stopped), naming the tables or views it concerns.

    Preview ``blockers`` share the shape; ``unknown`` words a code without a sentence."""
    grouped: Dict[str, List[str]] = {}
    for item in blockers if isinstance(blockers, list) else []:
        if isinstance(item, dict):
            names = grouped.setdefault(str(item.get("code") or "").lower(), [])
            if item.get("table"):
                name = {"original triggers": translate_text("원래 트리거"), "tf_restore_*": translate_text("검증용 복사본")}.get(str(item["table"]), str(item["table"]))
                if name not in names:
                    names.append(name)

    lines = []
    for code, names in grouped.items():
        source = CORE_CODE_SENTENCES.get(code) or unknown
        if omit_verify_hint:
            source = without_import_toggle_hints(source)
        sentence = translate_text(source)
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
        skip_triggers: bool = False,
        import_mode: str = "replace",
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
            payload["mode"] = import_mode
            if skip_triggers:
                payload["skip_triggers"] = True
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
        skip_triggers: bool = False,
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
            if payload is not None and skip_triggers:
                payload["skip_triggers"] = True
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
                state = {key: result.get(key) for key in ("status", "verified", "original_unchanged", "cutover_pending", "ready_for_switch", "blockers", "namespace_existed", "namespace_created", "candidate_created", "restore_id", "report_path", "plan_digest", "triggers_dropped", "programs", "programs_kept", "events_enabled", "programs_skipped_toggle")}
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
                    stops = describe_copy_blockers(result.get("copy_blockers"), omit_verify_hint=verify_only)
                    if stops:
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
            if result.get("status") == "partial":
                message = translate_text("테이블 데이터는 가져왔지만 일부 객체를 복원하지 못했습니다.") + f" ({len(tables_to_import)}개 테이블, {rows:,} rows)"
            if import_mode == "safe":
                candidate_name = candidate.get("schema") if self.config.engine == "postgresql" else candidate.get("database")
                message = translate_text("복사본 {}에 넣고 검증했습니다. 원본은 그대로이고 교체를 기다립니다.").format(candidate_name)
                if new_target:
                    message = translate_text("새 대상 {}을 만들어 넣고 검증했습니다.").format(candidate_name)
                for key, sentence in (("programs_kept", "그대로 둘 루틴·이벤트: {}"),):
                    if result.get(key):
                        message += "\n" + translate_text(sentence).format(", ".join(display_program_name(item) for item in result[key]))
                programs = result.get("programs") or {}
                for key, sentence in (("replace", "교체할 루틴·이벤트: {}"), ("create", "만들 루틴·이벤트: {}")):
                    ids = programs.get(key) or []
                    triggers = [item for item in ids if str(item).startswith("trigger:")]
                    if triggers:
                        message += "\n" + translate_text("만들 트리거: {}").format(", ".join(display_program_name(item) for item in triggers))
                    ids = [item for item in ids if item not in triggers]
                    if ids:
                        message += "\n" + translate_text(sentence).format(", ".join(display_program_name(item) for item in ids))
                if result.get("status") == "ready_for_review":
                    message += "\n" + translate_text("교체 전에 따로 확인할 것이 있습니다.")
                    for blocker in result.get("blockers") or []:
                        message += f"\n{blocker}"
            if views_imported:
                message += ("\n" if import_mode == "safe" else ", ") + f"View {len(views_imported)}개"
            for kinds, label in [(("TRIGGER",), "트리거"), (("PROCEDURE", "FUNCTION"), "루틴"), (("EVENT",), "이벤트")]:
                count = sum(item.get("kind", "TRIGGER") in kinds for item in result.get("programs_imported") or [] if isinstance(item, dict))
                if count:
                    message += ("\n" if import_mode == "safe" else ", ") + translate_text({"트리거":"트리거 {}개","루틴":"루틴 {}개","이벤트":"이벤트 {}개"}[label]).format(count)
            programs_failed = result.get("programs_failed") or []
            if programs_failed:
                self.last_error_code = classify_error_code(programs_failed[0].get("code") or "program_create_failed", "")
            program_lines, _ = describe_program_results(result)
            for line in program_lines:
                message += "\n" + line
                if progress_callback:
                    progress_callback(line)
            for key in ("programs_skipped_cross_engine", "programs_skipped_partial", "programs_skipped_toggle"):
                if result.get(key):
                    skipped_line = describe_core_code(key) + " " + ", ".join(display_program_name(item) for item in result[key])
                    message += "\n" + skipped_line
                    if progress_callback:
                        progress_callback(skipped_line)
            if views_failed:
                failed_names = ", ".join(
                    str(item.get("name", "")) for item in views_failed if isinstance(item, dict)
                )
                message += ("\n" if import_mode == "safe" else " ") + f"(View {len(views_failed)}개 생성 실패: {failed_names})"
            if views_skipped:
                message += ("\n" if import_mode == "safe" else " ") + f"(크로스 엔진 View {len(views_skipped)}개 건너뜀)"
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
            return not bool(views_failed or programs_failed or result.get("status") == "partial"), message, import_results
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
