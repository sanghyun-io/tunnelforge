#!/usr/bin/env python
"""Capture One-Click v2 plan-preview evidence."""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_ONECLICK_IDENTIFIER_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")
DEFAULT_SCHEMA = "tf_oneclick_readiness"


def _load_db_core_types():
    from src.core.db_core_facade import DbCoreFacade, DbEndpoint
    return DbCoreFacade, DbEndpoint


def current_git_sha() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, check=True, text=True, capture_output=True).stdout.strip()


def _public_plan(plan: Any) -> Dict[str, Any]:
    if isinstance(plan, dict):
        return copy.deepcopy(plan)
    for name in ("to_public_dict", "to_dict", "public_dict"):
        value = getattr(plan, name, None)
        if callable(value):
            result = value()
            if isinstance(result, dict):
                return copy.deepcopy(result)
    raise TypeError("plan_oneclick must return a public dict-serializable plan")


def build_evidence_report(*, git_sha: str, service_hello: Dict[str, Any], plan: Any, source_type: str = "local_mysql_container") -> Dict[str, Any]:
    return {
        "report_version": 2, "issue": 137, "git_sha": git_sha, "source_type": source_type,
        "service_hello": copy.deepcopy(service_hello), "mode": "plan_preview",
        "plan": _public_plan(plan), "approval": None,
        "apply": {"attempted": False, "request_count": 0},
    }


def _planned_artifacts(result: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(result, dict) or "plan" not in result or "approval" not in result:
        raise TypeError("plan_oneclick must return public plan and approval artifacts")
    return _public_plan(result["plan"]), copy.deepcopy(result["approval"])


def capture_oneclick_dry_run(*, host: str, port: int, user: str, password: str, schema: str) -> Dict[str, Any]:
    facade_type, endpoint_type = _load_db_core_types()
    facade = facade_type()
    endpoint = endpoint_type(engine="mysql", host=host, port=port, user=user, password=password, database=schema)
    try:
        service_hello = facade.hello()
        planned = facade.plan_oneclick(endpoint, schema)
    finally:
        facade.client.shutdown()
    plan, _approval = _planned_artifacts(planned)
    return build_evidence_report(git_sha=current_git_sha(), service_hello=service_hello, plan=plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/oneclick_readiness/oneclick-dry-run-evidence.json")
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3406)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default="test")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    args = parser.parse_args()
    report = capture_oneclick_dry_run(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user, password=args.mysql_password, schema=args.schema)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
