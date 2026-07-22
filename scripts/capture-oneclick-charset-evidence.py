#!/usr/bin/env python
"""Capture One-Click charset v2 evidence from the canonical plan and one apply request."""
from __future__ import annotations

import importlib.util
import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_ONECLICK_IDENTIFIER_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")


def _load_real_capture():
    spec = importlib.util.spec_from_file_location("capture_oneclick_real_execution", PROJECT_ROOT / "scripts" / "capture-oneclick-real-execution-evidence.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_real = _load_real_capture()
_load_db_core_types = _real._load_db_core_types
current_git_sha = _real.current_git_sha
build_evidence_report = _real.build_evidence_report


def _require_safe_scope(schema: str, tables: Iterable[str]) -> list[str]:
    if not SAFE_ONECLICK_IDENTIFIER_RE.fullmatch(schema):
        raise ValueError(f"refusing unsafe One-Click schema: {schema!r}")
    result = list(tables)
    if not result or any(not SAFE_ONECLICK_IDENTIFIER_RE.fullmatch(table) for table in result):
        raise ValueError("refusing unsafe One-Click table scope")
    return result


def capture_oneclick_charset(*, host: str, port: int, user: str, password: str, schema: str, tables: Iterable[str]) -> Dict[str, Any]:
    _require_safe_scope(schema, tables)
    # Keep this local implementation so patched facade types are observed by tests and callers.
    facade_type, endpoint_type = _load_db_core_types()
    facade = facade_type()
    endpoint = endpoint_type(engine="mysql", host=host, port=port, user=user, password=password, database=schema)
    try:
        hello = facade.hello()
        planned = facade.plan_oneclick(endpoint, schema)
        public_plan, approval = _real._base._planned_artifacts(planned)
        try:
            facade.apply_oneclick_plan(endpoint, schema, True, approval)
        except BaseException as error:
            if _real._error_code(error) != "oneclick_apply_disabled":
                raise
        else:
            raise RuntimeError("apply evidence capture requires a definite oneclick_apply_disabled result")
    finally:
        facade.client.shutdown()
    return build_evidence_report(git_sha=current_git_sha(), service_hello=hello, plan=public_plan, approval=approval, issue=139)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/oneclick_readiness/oneclick-charset-evidence.json")
    parser.add_argument("--mysql-host", default="127.0.0.1"); parser.add_argument("--mysql-port", type=int, default=3406)
    parser.add_argument("--mysql-user", default="root"); parser.add_argument("--mysql-password", default="test")
    parser.add_argument("--schema", default="tf_oneclick_charset")
    parser.add_argument("--table", action="append", dest="tables", default=["tf_oneclick_parent"])
    args = parser.parse_args()
    report = capture_oneclick_charset(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user, password=args.mysql_password, schema=args.schema, tables=args.tables)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
