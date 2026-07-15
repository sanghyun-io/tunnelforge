#!/usr/bin/env python
"""Archived charset derivation evidence capture, disabled during Phase A."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BASE_CAPTURE_PATH = PROJECT_ROOT / "scripts" / "capture-oneclick-charset-evidence.py"
DEFAULT_SCHEMA = "tf_oneclick_derive_charset"
DEFAULT_PARENT_TABLE = "tf_oneclick_parent"
DEFAULT_CHILD_TABLE = "tf_oneclick_child"


def _load_runtime_types():
    from src.core.db_core_service import DbCoreFacade, DbEndpoint
    from src.ui.dialogs.oneclick_migration_dialog import OneClickMigrationWorker

    return DbCoreFacade, DbEndpoint, OneClickMigrationWorker


def _load_base_capture():
    spec = importlib.util.spec_from_file_location("capture_oneclick_charset_evidence", BASE_CAPTURE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_base = _load_base_capture()
CaptureError = _base.CaptureError
OneClickCharsetCaptureDisabled = _base.OneClickCharsetCaptureDisabled
ONECLICK_APPLY_DISABLED_CODE = _base.ONECLICK_APPLY_DISABLED_CODE
ONECLICK_CAPTURE_DISABLED_MESSAGE = _base.ONECLICK_CAPTURE_DISABLED_MESSAGE
DEFAULT_TARGET_CHARSET = _base.DEFAULT_TARGET_CHARSET
DEFAULT_TARGET_COLLATION = _base.DEFAULT_TARGET_COLLATION


def _execution_event_from(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    for event in events:
        if event.get("event") == "execution":
            return event
    raise CaptureError("oneclick.run did not emit an execution event")


def _build_run_apply_result(
    *,
    pyqt_payload: Dict[str, Any],
    run_result: Dict[str, Any],
    execution_event: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "command": run_result.get("command") or "oneclick.run",
        "dry_run": bool(pyqt_payload.get("dry_run")),
        "success": run_result.get("success") is True,
        "success_count": execution_event.get("success_count") or 0,
        "fail_count": execution_event.get("fail_count") or 0,
        "skip_count": execution_event.get("skip_count") or 0,
        "disallowed_fix_attempts": execution_event.get("disallowed_fix_attempts") or [],
        "applied_fixes": execution_event.get("applied_fixes") or [],
    }


def build_derivation_evidence_report(
    *,
    git_sha: str,
    service_hello: Dict[str, Any],
    schema: str,
    target_charset: str,
    target_collation: str,
    derivation_result: Dict[str, Any],
    pyqt_payload: Dict[str, Any],
    run_result: Dict[str, Any],
    execution_event: Dict[str, Any],
    before_tables: List[Dict[str, Any]],
    after_tables: List[Dict[str, Any]],
    rollback_tables: List[Dict[str, Any]],
    foreign_keys: List[Dict[str, Any]],
) -> Dict[str, Any]:
    issues = derivation_result.get("issues") if isinstance(derivation_result.get("issues"), list) else []
    contracts = (
        derivation_result.get("contracts")
        if isinstance(derivation_result.get("contracts"), list)
        else []
    )
    report = _base.build_evidence_report(
        git_sha=git_sha,
        service_hello=service_hello,
        schema=schema,
        target_charset=target_charset,
        target_collation=target_collation,
        apply_result=_build_run_apply_result(
            pyqt_payload=pyqt_payload,
            run_result=run_result,
            execution_event=execution_event,
        ),
        before_tables=before_tables,
        after_tables=after_tables,
        rollback_tables=rollback_tables,
        foreign_keys=foreign_keys,
    )
    report["issue"] = 140
    report["derivation"] = {
        "command": derivation_result.get("command") or "oneclick.derive_charset_contracts",
        "success": derivation_result.get("success") is True,
        "source": "live_mysql_information_schema",
        "schema": schema,
        "payload_had_table_facts": False,
        "payload_had_issues": False,
        "issues_count": len(issues),
        "contracts_count": len(contracts),
        "issues": issues,
        "contracts": contracts,
    }
    report["pyqt_payload"] = {
        "builder": "OneClickMigrationWorker._core_payload",
        "dry_run": bool(pyqt_payload.get("dry_run")),
        "backup_confirmed": pyqt_payload.get("backup_confirmed") is True,
        "included_derived_issues": bool(pyqt_payload.get("issues")),
        "included_charset_contracts": bool(pyqt_payload.get("charset_contracts")),
        "issues_count": len(pyqt_payload.get("issues") or []),
        "contracts_count": len(pyqt_payload.get("charset_contracts") or []),
    }
    return report


def capture_oneclick_charset_derivation(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    tables: List[str],
    target_charset: str = DEFAULT_TARGET_CHARSET,
    target_collation: str = DEFAULT_TARGET_COLLATION,
    facade: Optional[Any] = None,
    git_sha: Optional[str] = None,
) -> Dict[str, Any]:
    _base._require_charset_capture_enabled()
    DbCoreFacade, DbEndpoint, OneClickMigrationWorker = _load_runtime_types()
    safe_tables = _base._require_safe_scope(schema, tables)
    _base._require_safe_charset_token(target_charset, "target_charset")
    _base._require_safe_charset_token(target_collation, "target_collation")

    facade = facade or DbCoreFacade()
    endpoint = DbEndpoint(
        engine="mysql",
        host=host,
        port=port,
        user=user,
        password=password,
        database=schema,
    )
    try:
        service_hello = facade.hello()
        before_tables = _base._table_charset_rows(facade, endpoint, schema, safe_tables)
        foreign_keys = _base._foreign_key_rows(facade, endpoint, schema, safe_tables)
        connection = SimpleNamespace(
            facade=facade,
            connection_id="oneclick-charset-derivation-evidence",
            endpoint=endpoint,
        )
        worker = OneClickMigrationWorker(
            connector=SimpleNamespace(connection=connection),
            schema=schema,
            dry_run=False,
            backup_confirmed=True,
        )
        pyqt_payload = worker._core_payload(connection)
        derivation_result = {
            "command": "oneclick.derive_charset_contracts",
            "success": bool(pyqt_payload.get("issues") and pyqt_payload.get("charset_contracts")),
            "issues": pyqt_payload.get("issues") or [],
            "contracts": pyqt_payload.get("charset_contracts") or [],
        }
        if not derivation_result["success"]:
            raise CaptureError("PyQt payload did not include derived charset issues/contracts")
        run_events: List[Dict[str, Any]] = []
        run_result = facade.run_oneclick(pyqt_payload, on_event=run_events.append)
        execution_event = _execution_event_from(run_events)
        after_tables = _base._table_charset_rows(facade, endpoint, schema, safe_tables)
    finally:
        client = getattr(facade, "client", None)
        shutdown = getattr(client, "shutdown", None)
        if callable(shutdown):
            shutdown()

    return build_derivation_evidence_report(
        git_sha=git_sha or _base.current_git_sha(),
        service_hello=service_hello,
        schema=schema,
        target_charset=target_charset,
        target_collation=target_collation,
        derivation_result=derivation_result,
        pyqt_payload=pyqt_payload,
        run_result=run_result,
        execution_event=execution_event,
        before_tables=before_tables,
        after_tables=after_tables,
        rollback_tables=before_tables,
        foreign_keys=foreign_keys,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/oneclick_readiness/oneclick-charset-derivation-evidence.json",
    )
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3406)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default="test")
    parser.add_argument("--seed-local-container", action="store_true")
    parser.add_argument("--mysql-container", default="tf-live-mysql")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--table", action="append", dest="tables")
    parser.add_argument("--target-charset", default=DEFAULT_TARGET_CHARSET)
    parser.add_argument("--target-collation", default=DEFAULT_TARGET_COLLATION)
    args = parser.parse_args()

    try:
        _base._require_charset_capture_enabled()
    except OneClickCharsetCaptureDisabled as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2

    tables = args.tables or [DEFAULT_PARENT_TABLE, DEFAULT_CHILD_TABLE]
    if len(tables) < 2:
        print(
            "One-Click charset derivation evidence capture failed: "
            "at least two tf_oneclick_ tables are required",
            file=sys.stderr,
        )
        return 1
    if args.seed_local_container:
        _base.seed_local_mysql_container(
            container=args.mysql_container,
            user=args.mysql_user,
            password=args.mysql_password,
            schema=args.schema,
            parent_table=tables[0],
            child_table=tables[1],
        )
    try:
        report = capture_oneclick_charset_derivation(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            schema=args.schema,
            tables=tables,
            target_charset=args.target_charset,
            target_collation=args.target_collation,
        )
    except Exception as exc:
        print(f"One-Click charset derivation evidence capture failed: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"Wrote One-Click charset derivation evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
