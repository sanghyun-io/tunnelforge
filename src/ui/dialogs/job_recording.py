"""Job-history hooks for the export / import / promotion / migration dialogs (TF-STATUS-132).

The dialogs only call `begin_*` when a run starts and `finish_*` when it ends; everything about
which fields are recorded (and which are never recorded) lives here. All functions swallow
recording failures: the job history must never get in the way of the job itself.
"""
import os
from typing import Any, Dict, Optional

from src.core.job_history import (
    KIND_EXPORT_FULL, KIND_EXPORT_TABLES, KIND_IMPORT, KIND_MIGRATION_PREFLIGHT, KIND_MIGRATION_RESUME,
    KIND_MIGRATION_RUN, KIND_PROMOTE, STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED, STATUS_PARTIAL,
    job_begin, job_finish, job_update_running,
)

DUMP_MANIFEST_NAME = "_tunnelforge_dump.json"


def _profile(context: Optional[Dict[str, Any]], fallback_name: str = "") -> Dict[str, str]:
    context = context or {}
    return {"profile_id": str(context.get("profile_id") or ""),
            "profile_name": str(context.get("profile_name") or fallback_name or "")}


# ------------------------------------------------------------------ export

# 작업 목록에 보이는 실행 모드는 사람이 읽는 라벨로 남긴다. 끝난 MySQL Export 는 Core 결과의 방법 문장
# (describe_snapshot_method)을 쓰고, 그 전에는 고른 설정(시점 일관성 포기 허용 여부)만 보인다.
_SNAPSHOT_LABELS = {
    "single_connection": "내보내는 방법 자동 선택",
    "parallel_unsynchronized": "내보내는 방법 자동 선택(시점 일관성 포기 허용)",
}
_COMPRESSION_LABELS = {"none": "압축 안 함", "zstd": "압축(zstd)"}


def _export_mode(dialog) -> str:
    scope = "전체" if dialog._is_full_export() else f"테이블 {len(dialog.get_selected_tables())}개"
    compression = dialog._compression()
    method = getattr(dialog, "_snapshot_method", "")
    if not method:
        threads = f"동시 작업 {dialog.spin_threads.value()}개"
        method = threads if dialog._engine() == "postgresql" else f"{_SNAPSHOT_LABELS[dialog._snapshot_fallback()]} · {threads}"
    return (f"{scope} · {method} · {_COMPRESSION_LABELS.get(compression, compression)}"
            + (" · 불완전 Export(일부 객체 제외)" if dialog._omitted_objects else ""))


def begin_export_job(dialog) -> Optional[str]:
    try:
        full = dialog._is_full_export()
        return job_begin(
            KIND_EXPORT_FULL if full else KIND_EXPORT_TABLES,
            target=dialog.export_schema, mode=_export_mode(dialog),
            # 다시 열 때는 사용자가 고른 테이블만 채운다. 자동으로 더한 부모 테이블은 그때 설정대로 다시 정한다.
            rerun={"schema": dialog.export_schema, "scope": "full" if full else "tables",
                   "tables": [] if full else dialog._picked_tables(),
                   "compression": dialog._compression(),
                   "threads": dialog.spin_threads.value()},
            **_profile(getattr(dialog, "job_context", None), getattr(dialog, "connection_info", "")),
        )
    except Exception:
        return None


def finish_export_job(dialog, success: bool, message: str) -> None:
    job_id = getattr(dialog, "_job_id", None)
    if not job_id:
        return
    try:
        refusal = getattr(getattr(dialog, "worker", None), "export_refusal", None)
        if getattr(dialog, "_cancel_requested", False):
            status = STATUS_CANCELLED
        elif not success and isinstance(refusal, dict) and refusal.get("objects") and refusal.get("bypassable"):
            # 뺄 수 있는 객체만 있었는데 사용자가 '취소'를 골랐다: 실패가 아니라 사용자가 멈춘 것이다.
            status = STATUS_CANCELLED
        elif success:
            # 실제로 빠진 객체가 있을 때만 불완전: '빼고 내보내기'를 켜 두었어도 뺄 것이 없었으면 완전한 Export 다.
            status = STATUS_PARTIAL if getattr(dialog, "_omitted_objects", None) else STATUS_COMPLETED
        else:
            status = STATUS_FAILED
        output = dialog.input_output_dir.text()
        details = {"tables": int(dialog.export_total_tables or 0), "rows": int(sum(dialog.export_table_done.values()))}
        # begin 은 방법이 정해지기 전에 기록되므로, 끝나면 Core 가 실제로 쓴 방법으로 바꾼다.
        job_finish(job_id, status, error="" if success else message, mode=_export_mode(dialog),
                   report_path=os.path.join(output, DUMP_MANIFEST_NAME) if output else "", details=details)
    except Exception:
        pass
    finally:
        dialog._job_id = None


# ------------------------------------------------------------------ import

IMPORT_LABEL = "Import · 덤프 테이블 교체"
# 작업 목록이 백업 관리를 여는 표시 (job_list_dialog.BACKUP_MODE_MARKERS).
COPY_SUFFIX = " · 복사본 검증"


def made_copy(audit: Dict[str, Any]) -> bool:
    """복사본(교체 뒤에는 백업)이 남은 Import: 복원 백업 관리로 정리한다. 없던 대상에 바로 넣은 것은 복사본이 아니다."""
    return audit.get("candidate_created") is True and audit.get("namespace_existed") is not False


def import_job_label(audit: Dict[str, Any], other_name: str = "", engine: str = "") -> str:
    """작업 목록의 Import 실행 모드. 고른 설정이 아니라 실제로 일어난 일로 정한다."""
    label = IMPORT_LABEL
    if made_copy(audit):
        label += COPY_SUFFIX
    elif audit.get("restore_status") == "completed_new_target" or audit.get("created_namespace"):
        # '덮어쓰기'는 쓰지 않는다: BACKUP_MODE_MARKERS 에 걸려 모든 기록에 백업 관리가 켜진다.
        label += " · 새 스키마 생성" if engine == "postgresql" else " · 새 DB 생성"
    if other_name:
        label += f" · 대상 {other_name}"
    return label


def _import_label(dialog) -> str:
    context = getattr(dialog, "last_import_context", None) or {}
    return import_job_label(getattr(dialog, "import_audit", None) or {}, str(context.get("other_name") or ""),
                            str(context.get("engine") or ""))


def begin_import_job(dialog, target: str) -> Optional[str]:
    # 보고서는 실행마다 새 파일이라 Core 가 이름을 알려 줄 때 기록한다 (record_import_report).
    try:
        profile = getattr(dialog, "tunnel_config", None) or {}
        context = {"profile_id": profile.get("id"), "profile_name": profile.get("name")}
        return job_begin(
            KIND_IMPORT, target=target, mode=_import_label(dialog),
            **_profile(context, getattr(dialog, "connection_info", "")),
        )
    except Exception:
        return None


def record_import_report(dialog) -> None:
    """Core 가 보고서 파일을 알려 주거나 복사본을 만들면 실행 중인 기록에 바로 남긴다.
    앱이 도중에 꺼져도(interrupted) 그 기록에서 복원 백업 관리를 열 수 있다."""
    job_update_running(getattr(dialog, "_job_id", None),
                       report_path=str((getattr(dialog, "import_audit", None) or {}).get("report_path") or ""),
                       mode=_import_label(dialog))


def import_status(success: bool, cancelled: bool, done: int, errors: int, *, core_status: str = "", data_imported: bool = False) -> str:
    if cancelled:
        return STATUS_CANCELLED
    if success:
        return STATUS_COMPLETED
    return STATUS_PARTIAL if (done > 0 and errors > 0) or (core_status == "partial" and data_imported) else STATUS_FAILED


def finish_import_job(dialog, success: bool, message: str, done: int, errors: int, blocked: int) -> None:
    job_id = getattr(dialog, "_job_id", None)
    if not job_id:
        return
    try:
        audit = getattr(dialog, "import_audit", {}) or {}
        status = import_status(success, getattr(dialog, "_cancel_requested", False), done, errors,
                               core_status=audit.get("status", ""), data_imported=audit.get("data_imported") is True)
        report = audit.get("report_path") or ""
        job_finish(job_id, status, error="" if success else message, report_path=str(report),
                   mode=_import_label(dialog),
                   details={"tables_done": done, "tables_failed": errors, "tables_not_run": blocked,
                            "restore_status": str(audit.get("restore_status") or "")})
    except Exception:
        pass
    finally:
        dialog._job_id = None


# ------------------------------------------------------------------ safe promotion

def begin_promotion_job(dialog, payload: Dict[str, Any]) -> Optional[str]:
    try:
        target = payload.get("target") or {}
        profile = getattr(dialog, "tunnel_config", None) or {}
        context = {"profile_id": profile.get("id"), "profile_name": profile.get("name")}
        namespace = target.get("schema") or target.get("database") or ""
        return job_begin(KIND_PROMOTE, target=str(namespace), mode="안전 전환 (잠금 아래 원자적 교체)",
                         **_profile(context, getattr(dialog, "connection_info", "")))
    except Exception:
        return None


def promotion_status(success: bool, cancelled: bool, result: Dict[str, Any]) -> str:
    if cancelled:
        return STATUS_CANCELLED
    return STATUS_COMPLETED if success and result.get("status") == "promoted" else STATUS_FAILED


def finish_promotion_job(dialog, success: bool, message: str, result: Dict[str, Any]) -> None:
    job_id = getattr(dialog, "_promotion_job_id", None)
    if not job_id:
        return
    try:
        status = promotion_status(success, getattr(dialog, "_cancel_requested", False), result or {})
        outcome = str((result or {}).get("status") or "")
        error = ""
        if status != STATUS_COMPLETED:
            from src.exporters.rust_dump_exporter import describe_core_code, describe_core_failure
            error = describe_core_code((result or {}).get("error_code")) or describe_core_failure((result or {}).get("error")) or describe_core_failure(message) or message or str((result or {}).get("message") or "")
            if outcome in ("cutover_unknown", "pending", "preparing", ""):
                error = ("전환 결과를 알 수 없습니다. 원본/백업/복원 대상의 상태를 확인하세요. " + error).strip()
        job_finish(job_id, status, error=error, report_path=str((result or {}).get("report_path") or ""),
                   details={"outcome": outcome, "backup_namespace": str((result or {}).get("backup_namespace") or "")})
    except Exception:
        pass
    finally:
        dialog._promotion_job_id = None


# ------------------------------------------------------------------ cross-engine migration

_MIGRATION_KINDS = {"preflight": KIND_MIGRATION_PREFLIGHT, "migrate": KIND_MIGRATION_RUN, "resume": KIND_MIGRATION_RESUME}


def migration_job_kind(command: str) -> Optional[str]:
    return _MIGRATION_KINDS.get(command)


def _endpoint_label(endpoint: Any, engine: Any) -> str:
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    name = endpoint.get("schema") or endpoint.get("database") or "?"
    return f"{name} ({engine or endpoint.get('engine') or '?'})"


def begin_migration_job(command: str, payload: Dict[str, Any]) -> Optional[str]:
    kind = migration_job_kind(command)
    if kind is None:
        return None
    try:
        options = payload.get("execution_options") or {}
        target = (f"{_endpoint_label(payload.get('source'), payload.get('source_engine'))} -> "
                  f"{_endpoint_label(payload.get('target'), payload.get('target_engine'))}")
        return job_begin(kind, target=target, mode=f"{command} · {options.get('mode', '-')}")
    except Exception:
        return None


def migration_status(success: bool, payload: Any) -> str:
    if isinstance(payload, dict) and payload.get("cancelled"):
        return STATUS_CANCELLED
    return STATUS_COMPLETED if success else STATUS_FAILED


def finish_migration_job(job_id: Optional[str], success: bool, payload: Any, report_path: str = "") -> None:
    if not job_id:
        return
    try:
        status = migration_status(success, payload)
        error = ""
        if status == STATUS_FAILED and isinstance(payload, dict):
            error = str(payload.get("error") or payload.get("message") or "")
        job_finish(job_id, status, error=error, report_path=report_path)
    except Exception:
        pass
