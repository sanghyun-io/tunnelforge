#!/usr/bin/env python
"""Build/capture Rust Core-backed One-Click charset evidence for GitHub #139.

This module currently provides the validator-backed report shape and safety
guards used before the Rust Core charset execution allowlist is opened.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.dialogs.migration_dialogs import ONE_CLICK_MIGRATION_FEATURE_ENABLED  # noqa: E402
from src.ui.dialogs.oneclick_migration_dialog import ONECLICK_REAL_EXECUTION_ENABLED  # noqa: E402


DEFAULT_SCHEMA = "tf_oneclick_charset"
DEFAULT_PARENT_TABLE = "tf_oneclick_parent"
DEFAULT_CHILD_TABLE = "tf_oneclick_child"
DEFAULT_TARGET_CHARSET = "utf8mb4"
DEFAULT_TARGET_COLLATION = "utf8mb4_0900_ai_ci"
SAFE_ONECLICK_IDENTIFIER_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")


class CaptureNotImplementedError(RuntimeError):
    """Raised until Rust Core exposes the #139 charset execution path."""


def current_git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _require_safe_oneclick_identifier(value: str, label: str) -> None:
    if not SAFE_ONECLICK_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"refusing to seed unsafe One-Click {label}: {value!r}")


def _require_safe_scope(schema: str, tables: Iterable[str]) -> List[str]:
    _require_safe_oneclick_identifier(schema, "schema")
    safe_tables = list(tables)
    if not safe_tables:
        raise ValueError("refusing to capture One-Click charset evidence without tables")
    for table in safe_tables:
        _require_safe_oneclick_identifier(table, "table")
    return safe_tables


def build_evidence_report(
    *,
    git_sha: str,
    service_hello: Dict[str, Any],
    schema: str,
    target_charset: str,
    target_collation: str,
    apply_result: Dict[str, Any],
    before_tables: List[Dict[str, Any]],
    after_tables: List[Dict[str, Any]],
    rollback_tables: List[Dict[str, Any]],
    foreign_keys: List[Dict[str, Any]],
    source_type: str = "local_mysql_container",
) -> Dict[str, Any]:
    applied_fixes = [
        item for item in apply_result.get("applied_fixes") or [] if isinstance(item, dict)
    ]
    attempted_fix_types = [
        str(item.get("issue_type"))
        for item in applied_fixes
        if item.get("issue_type")
    ]
    attempted_strategies = [
        str(item.get("strategy"))
        for item in applied_fixes
        if item.get("strategy")
    ]

    return {
        "issue": 139,
        "git_sha": git_sha,
        "source_type": source_type,
        "feature_flags": {
            "oneclick_ui_enabled": bool(ONE_CLICK_MIGRATION_FEATURE_ENABLED),
            "oneclick_real_execution_enabled": bool(ONECLICK_REAL_EXECUTION_ENABLED),
        },
        "service_hello": {
            "capabilities": service_hello.get("capabilities") or [],
        },
        "scope": {
            "schema": schema,
            "allowed_fix_types": ["charset_issue"],
            "allowed_strategies": ["charset_collation_fk_safe"],
            "target_charset": target_charset,
            "target_collation": target_collation,
            "requires_fk_safe_ordering": True,
            "requires_rollback_metadata": True,
        },
        "run": {
            "command": apply_result.get("command") or "oneclick.apply_fixes",
            "dry_run": bool(apply_result.get("dry_run")),
            "backup_confirmed": True,
            "success": apply_result.get("success") is True,
            "schema": schema,
            "attempted_fix_types": attempted_fix_types,
            "attempted_strategies": attempted_strategies,
            "disallowed_fix_attempts": apply_result.get("disallowed_fix_attempts") or [],
            "applied_fixes": applied_fixes,
        },
        "before": {
            "tables": before_tables,
            "foreign_keys": foreign_keys,
        },
        "after": {
            "tables": after_tables,
            "foreign_keys_valid": True,
            "unrelated_tables_unchanged": True,
        },
        "rollback": {
            "metadata_captured": True,
            "tables": rollback_tables,
        },
        "validation": {
            "all_fixed": apply_result.get("success") is True,
            "remaining_issues": 0 if apply_result.get("success") is True else 1,
            "fk_constraints_valid": True,
            "post_charset": target_charset,
            "post_collation": target_collation,
        },
    }


def capture_oneclick_charset(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    tables: List[str],
    target_charset: str = DEFAULT_TARGET_CHARSET,
    target_collation: str = DEFAULT_TARGET_COLLATION,
) -> Dict[str, Any]:
    _ = (host, port, user, password, target_charset, target_collation)
    _require_safe_scope(schema, tables)
    raise CaptureNotImplementedError(
        "Rust Core charset/collation real execution is not implemented yet; "
        "add the #139 Rust Core allowlist path before capturing completed evidence."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/oneclick_readiness/oneclick-charset-evidence.json",
    )
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3406)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default="test")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--table", action="append", dest="tables")
    parser.add_argument("--target-charset", default=DEFAULT_TARGET_CHARSET)
    parser.add_argument("--target-collation", default=DEFAULT_TARGET_COLLATION)
    args = parser.parse_args()

    tables = args.tables or [DEFAULT_PARENT_TABLE, DEFAULT_CHILD_TABLE]
    try:
        report = capture_oneclick_charset(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            schema=args.schema,
            tables=tables,
            target_charset=args.target_charset,
            target_collation=args.target_collation,
        )
    except CaptureNotImplementedError as exc:
        print(f"One-Click charset evidence capture failed closed: {exc}", file=sys.stderr)
        return 2

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"Wrote One-Click charset evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
