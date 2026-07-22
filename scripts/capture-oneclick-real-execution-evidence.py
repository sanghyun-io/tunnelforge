#!/usr/bin/env python
"""Capture One-Click v2 apply evidence; the disabled result is evidence, not a retry."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from importlib.util import module_from_spec, spec_from_file_location

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = "tf_oneclick_real_execution"
DEFAULT_TABLE = "tf_oneclick_legacy_engine_table"


def _load_base():
    spec = spec_from_file_location("capture_oneclick_dry_run_evidence", PROJECT_ROOT / "scripts" / "capture-oneclick-dry-run-evidence.py")
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_base = _load_base()
_load_db_core_types = _base._load_db_core_types
current_git_sha = _base.current_git_sha


def _approval(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {key: copy.deepcopy(plan[key]) for key in ("target_identity", "remediation_profile", "snapshot_hash", "plan_hash")} | {"approval_version": 1, "plan_version": plan["plan_version"]}


def _error_code(error: BaseException) -> Optional[str]:
    return getattr(error, "rust_code", None) or getattr(error, "code", None)


def build_evidence_report(*, git_sha: str, service_hello: Dict[str, Any], plan: Any, approval: Optional[Dict[str, Any]] = None, issue: int = 138, source_type: str = "local_mysql_container") -> Dict[str, Any]:
    public_plan = _base._public_plan(plan)
    return {
        "report_version": 2, "issue": issue, "git_sha": git_sha, "source_type": source_type,
        "mode": "apply_attempt", "service_hello": copy.deepcopy(service_hello), "plan": public_plan,
        "approval": copy.deepcopy(approval) if approval is not None else _approval(public_plan),
        "apply": {"attempted": True, "request_count": 1, "success": False, "error_code": "oneclick_apply_disabled"},
        "before": copy.deepcopy(public_plan["snapshot"]), "after": copy.deepcopy(public_plan["snapshot"]),
    }


def capture_oneclick_real_execution(*, host: str, port: int, user: str, password: str, schema: str, table: str) -> Dict[str, Any]:
    facade_type, endpoint_type = _load_db_core_types()
    facade = facade_type()
    endpoint = endpoint_type(engine="mysql", host=host, port=port, user=user, password=password, database=schema)
    try:
        service_hello = facade.hello()
        planned = facade.plan_oneclick(endpoint, schema)
        public_plan, approval = _base._planned_artifacts(planned)
        try:
            facade.apply_oneclick_plan(endpoint, schema, True, approval)
        except BaseException as error:
            if _error_code(error) != "oneclick_apply_disabled":
                raise
        else:
            raise RuntimeError("apply evidence capture requires a definite oneclick_apply_disabled result")
    finally:
        facade.client.shutdown()
    return build_evidence_report(git_sha=current_git_sha(), service_hello=service_hello, plan=public_plan, approval=approval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/oneclick_readiness/oneclick-real-execution-evidence.json")
    parser.add_argument("--mysql-host", default="127.0.0.1"); parser.add_argument("--mysql-port", type=int, default=3406)
    parser.add_argument("--mysql-user", default="root"); parser.add_argument("--mysql-password", default="test")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA); parser.add_argument("--table", default=DEFAULT_TABLE)
    args = parser.parse_args()
    report = capture_oneclick_real_execution(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user, password=args.mysql_password, schema=args.schema, table=args.table)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
