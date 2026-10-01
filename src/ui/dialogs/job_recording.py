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
    job_begin, job_finish,
)

IMPORT_REPORT_NAME = "_tunnelforge_import_report.json"
DUMP_MANIFEST_NAME = "_tunnelforge_dump.json"


def _profile(context: Optional[Dict[str, Any]], fallback_name: str = "") -> Dict[str, str]:
    context = context or {}
    return {"profile_id": str(context.get("profile_id") or ""),
            "profile_name": str(context.get("profile_name") or fallback_name or "")}


# ------------------------------------------------------------------ export

def begin_export_job(dialog) -> Optional[str]:
    try:
        full = dialog.radio_full.isChecked()
        tables = [] if full else list(dialog.get_selected_tables())
        mode = (f"{'전체' if full else f'테이블 {len(tables)}개'} · snapshot={dialog._snapshot_mode}"
                f" · {dialog.combo_compression.currentText()} · 스레드 {dialog.spin_threads.value()}"
                + (" · 불완전 Export(테이블 데이터만)" if dialog._allow_incomplete else ""))
        return job_begin(
            KIND_EXPORT_FULL if full else KIND_EXPORT_TABLES,
            target=dialog.export_schema, mode=mode,
            rerun={"schema": dialog.export_schema, "scope": "full" if full else "tables", "tables": tables,
                   "compression": dialog.combo_compression.currentText(),
                   "threads": dialog.spin_threads.value(),
                   "snapshot_mode": dialog._snapshot_mode,
                   "include_fk_parents": bool(dialog.chk_include_fk.isChecked())},
            **_profile(getattr(dialog, "job_context", None), getattr(dialog, "connection_info", "")),
        )
    except Exception:
        return None


def finish_export_job(dialog, success: bool, message: str) -> None:
    job_id = getattr(dialog, "_job_id", None)
    if not job_id:
        return
    try:
        if getattr(dialog, "_cancel_requested", False):
            status = STATUS_CANCELLED
        elif success:
            status = STATUS_PARTIAL if getattr(dialog, "_allow_incomplete", False) else STATUS_COMPLETED
        else:
            status = STATUS_FAILED
        output = dialog.input_output_dir.text()
        details = {"tables": int(dialog.export_total_tables or 0), "rows": int(sum(dialog.export_table_done.values()))}
        job_finish(job_id, status, error="" if success else message,
                   report_path=os.path.join(output, DUMP_MANIFEST_NAME) if output else "", details=details)
    except Exception:
        pass
    finally:
        dialog._job_id = None


# ------------------------------------------------------------------ import

def begin_import_job(dialog, input_dir: str, target: str, import_mode: str, threads: int) -> Optional[str]:
    try:
        profile = getattr(dialog, "tunnel_config", None) or {}
        context = {"profile_id": profile.get("id"), "profile_name": profile.get("name")}
        return job_begin(
            KIND_IMPORT, target=target, mode=f"{import_mode} · 스레드 {threads}",
            report_path=os.path.join(input_dir, IMPORT_REPORT_NAME) if input_dir else "",
            **_profile(context, getattr(dialog, "connection_info", "")),
        )
    except Exception:
        return None


def import_status(success: bool, cancelled: bool, done: int, errors: int) -> str:
    if cancelled:
        return STATUS_CANCELLED
    if success:
        return STATUS_COMPLETED
    return STATUS_PARTIAL if done > 0 and errors > 0 else STATUS_FAILED


def finish_import_job(dialog, success: bool, message: str, done: int, errors: int, blocked: int) -> None:
    job_id = getattr(dialog, "_job_id", None)
    if not job_id:
        return
    try:
        status = import_status(success, getattr(dialog, "_cancel_requested", False), done, errors)
        audit = getattr(dialog, "import_audit", {}) or {}
        report = audit.get("report_path") or ""
        job_finish(job_id, status, error="" if success else message, report_path=str(report),
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
            error = message or str((result or {}).get("message") or "")
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
