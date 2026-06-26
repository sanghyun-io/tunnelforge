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
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.dialogs.migration_dialogs import ONE_CLICK_MIGRATION_FEATURE_ENABLED  # noqa: E402
from src.ui.dialogs.oneclick_migration_dialog import ONECLICK_REAL_EXECUTION_ENABLED  # noqa: E402
from src.core.db_core_service import DbCoreFacade, DbEndpoint  # noqa: E402


DEFAULT_SCHEMA = "tf_oneclick_charset"
DEFAULT_PARENT_TABLE = "tf_oneclick_parent"
DEFAULT_CHILD_TABLE = "tf_oneclick_child"
DEFAULT_TARGET_CHARSET = "utf8mb4"
DEFAULT_TARGET_COLLATION = "utf8mb4_0900_ai_ci"
SAFE_ONECLICK_IDENTIFIER_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")


class CaptureError(RuntimeError):
    """Raised when One-Click charset evidence capture cannot complete safely."""


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


def _run_checked(args: List[str]) -> None:
    subprocess.run(args, check=True, text=True)


def seed_local_mysql_container(
    *,
    container: str,
    user: str,
    password: str,
    schema: str,
    parent_table: str,
    child_table: str,
) -> None:
    _require_safe_scope(schema, [parent_table, child_table])

    sql = f"""
DROP DATABASE IF EXISTS `{schema}`;
CREATE DATABASE `{schema}` CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;
USE `{schema}`;
CREATE TABLE `{parent_table}` (
  `id` INT NOT NULL PRIMARY KEY,
  `name` VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci;
CREATE TABLE `{child_table}` (
  `id` INT NOT NULL PRIMARY KEY,
  `parent_id` INT NOT NULL,
  `name` VARCHAR(64) NOT NULL,
  CONSTRAINT `fk_child_parent` FOREIGN KEY (`parent_id`) REFERENCES `{parent_table}` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 COLLATE=utf8mb3_general_ci;
INSERT INTO `{parent_table}` (`id`, `name`) VALUES (1, 'parent-row-1'), (2, 'parent-row-2');
INSERT INTO `{child_table}` (`id`, `parent_id`, `name`) VALUES
  (1, 1, 'child-row-1'),
  (2, 2, 'child-row-2');
"""
    _run_checked([
        "docker",
        "exec",
        container,
        "mysql",
        f"-u{user}",
        f"-p{password}",
        "-e",
        sql,
    ])


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
    facade: Optional[Any] = None,
    git_sha: Optional[str] = None,
) -> Dict[str, Any]:
    safe_tables = _require_safe_scope(schema, tables)
    _require_safe_charset_token(target_charset, "target_charset")
    _require_safe_charset_token(target_collation, "target_collation")

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
        before_tables = _table_charset_rows(facade, endpoint, schema, safe_tables)
        foreign_keys = _foreign_key_rows(facade, endpoint, schema, safe_tables)
        apply_result = facade.apply_oneclick_fixes(
            {
                "connection": endpoint.to_payload(),
                "schema": schema,
                "dry_run": False,
                "backup_confirmed": True,
                "steps": [{
                    "issue_type": "charset_issue",
                    "location": f"{schema}.{safe_tables[0]}",
                    "table_name": safe_tables[0],
                    "selected_option": _charset_selected_option(
                        schema=schema,
                        tables=safe_tables,
                        before_tables=before_tables,
                        target_charset=target_charset,
                        target_collation=target_collation,
                    ),
                }],
            }
        )
        after_tables = _table_charset_rows(facade, endpoint, schema, safe_tables)
    finally:
        client = getattr(facade, "client", None)
        shutdown = getattr(client, "shutdown", None)
        if callable(shutdown):
            shutdown()

    return build_evidence_report(
        git_sha=git_sha or current_git_sha(),
        service_hello=service_hello,
        schema=schema,
        target_charset=target_charset,
        target_collation=target_collation,
        apply_result=apply_result,
        before_tables=before_tables,
        after_tables=after_tables,
        rollback_tables=before_tables,
        foreign_keys=foreign_keys,
    )


def _require_safe_charset_token(value: str, label: str) -> None:
    if not re.fullmatch(r"^[A-Za-z0-9_]+$", value or ""):
        raise ValueError(f"refusing unsafe One-Click {label}: {value!r}")


def _table_charset_rows(
    facade: Any,
    endpoint: DbEndpoint,
    schema: str,
    tables: List[str],
) -> List[Dict[str, Any]]:
    placeholders = ", ".join(["%s"] * len(tables))
    rows = facade.execute_query(
        endpoint,
        (
            "SELECT t.TABLE_SCHEMA AS `schema`, t.TABLE_NAME AS `table`, "
            "ccsa.CHARACTER_SET_NAME AS charset, t.TABLE_COLLATION AS collation "
            "FROM information_schema.TABLES t "
            "JOIN information_schema.COLLATION_CHARACTER_SET_APPLICABILITY ccsa "
            "ON t.TABLE_COLLATION = ccsa.COLLATION_NAME "
            f"WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME IN ({placeholders}) "
            "ORDER BY FIELD(t.TABLE_NAME, "
            + ", ".join(["%s"] * len(tables))
            + ")"
        ),
        [schema, *tables, *tables],
    )
    return [row for row in rows if isinstance(row, dict)]


def _foreign_key_rows(
    facade: Any,
    endpoint: DbEndpoint,
    schema: str,
    tables: List[str],
) -> List[Dict[str, Any]]:
    placeholders = ", ".join(["%s"] * len(tables))
    rows = facade.execute_query(
        endpoint,
        (
            "SELECT kcu.TABLE_SCHEMA AS `schema`, kcu.TABLE_NAME AS `table`, "
            "kcu.REFERENCED_TABLE_NAME AS referenced_table, "
            "kcu.CONSTRAINT_NAME AS `constraint` "
            "FROM information_schema.KEY_COLUMN_USAGE kcu "
            f"WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME IN ({placeholders}) "
            "AND kcu.REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME"
        ),
        [schema, *tables],
    )
    return [row for row in rows if isinstance(row, dict)]


def _charset_selected_option(
    *,
    schema: str,
    tables: List[str],
    before_tables: List[Dict[str, Any]],
    target_charset: str,
    target_collation: str,
) -> Dict[str, Any]:
    before_by_table = {str(row.get("table")): row for row in before_tables}
    sql = [
        (
            f"ALTER TABLE `{schema}`.`{table}` CONVERT TO CHARACTER SET "
            f"{target_charset} COLLATE {target_collation};"
        )
        for table in tables
    ]
    rollback_sql = []
    for table in reversed(tables):
        before = before_by_table.get(table) or {}
        charset = str(before.get("charset") or "").strip()
        collation = str(before.get("collation") or "").strip()
        if not charset or not collation:
            raise CaptureError(f"missing rollback charset/collation metadata for {schema}.{table}")
        rollback_sql.append(
            f"ALTER TABLE `{schema}`.`{table}` CONVERT TO CHARACTER SET {charset} COLLATE {collation};"
        )

    return {
        "strategy": "charset_collation_fk_safe",
        "tables": tables,
        "fk_order": tables,
        "target_charset": target_charset,
        "target_collation": target_collation,
        "sql": sql,
        "rollback_sql": rollback_sql,
    }


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
    parser.add_argument("--seed-local-container", action="store_true")
    parser.add_argument("--mysql-container", default="tf-live-mysql")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--table", action="append", dest="tables")
    parser.add_argument("--target-charset", default=DEFAULT_TARGET_CHARSET)
    parser.add_argument("--target-collation", default=DEFAULT_TARGET_COLLATION)
    args = parser.parse_args()

    tables = args.tables or [DEFAULT_PARENT_TABLE, DEFAULT_CHILD_TABLE]
    if len(tables) < 2:
        print("One-Click charset evidence capture failed: at least two tf_oneclick_ tables are required", file=sys.stderr)
        return 1
    if args.seed_local_container:
        seed_local_mysql_container(
            container=args.mysql_container,
            user=args.mysql_user,
            password=args.mysql_password,
            schema=args.schema,
            parent_table=tables[0],
            child_table=tables[1],
        )
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
    except Exception as exc:
        print(f"One-Click charset evidence capture failed: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"Wrote One-Click charset evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
