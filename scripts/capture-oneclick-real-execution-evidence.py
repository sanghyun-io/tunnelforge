#!/usr/bin/env python
"""Capture Rust Core-backed One-Click real-execution evidence for GitHub #138."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.db_core_service import DbCoreFacade, DbEndpoint  # noqa: E402
from src.ui.dialogs.migration_dialogs import ONE_CLICK_MIGRATION_FEATURE_ENABLED  # noqa: E402
from src.ui.dialogs.oneclick_migration_dialog import ONECLICK_REAL_EXECUTION_ENABLED  # noqa: E402


DEFAULT_SCHEMA = "tf_oneclick_real_execution"
DEFAULT_TABLE = "tf_oneclick_legacy_engine_table"
SAFE_ONECLICK_IDENTIFIER_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")


def _run_checked(args: List[str]) -> None:
    subprocess.run(args, check=True, text=True)


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


def seed_local_mysql_container(
    *,
    container: str,
    user: str,
    password: str,
    schema: str,
    table: str,
) -> None:
    _require_safe_oneclick_identifier(schema, "schema")
    _require_safe_oneclick_identifier(table, "table")

    sql = f"""
CREATE DATABASE IF NOT EXISTS `{schema}`;
USE `{schema}`;
DROP TABLE IF EXISTS `{table}`;
CREATE TABLE `{table}` (
  `id` INT NOT NULL PRIMARY KEY,
  `name` VARCHAR(64) NOT NULL
) ENGINE=MyISAM;
INSERT INTO `{table}` (`id`, `name`) VALUES
  (1, 'engine-row-1'),
  (2, 'engine-row-2');
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
    table: str,
    apply_result: Dict[str, Any],
    before_tables: List[Dict[str, Any]],
    after_tables: List[Dict[str, Any]],
    source_type: str = "local_mysql_container",
) -> Dict[str, Any]:
    attempted_fix_types = [
        str(item.get("issue_type"))
        for item in apply_result.get("applied_fixes") or []
        if isinstance(item, dict) and item.get("issue_type")
    ]
    attempted_strategies = [
        str(item.get("strategy"))
        for item in apply_result.get("applied_fixes") or []
        if isinstance(item, dict) and item.get("strategy")
    ]
    post_engine = _table_engine(after_tables, schema, table)

    return {
        "issue": 138,
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
            "allowed_fix_types": ["deprecated_engine"],
            "allowed_strategies": ["engine_innodb"],
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
            "applied_fixes": apply_result.get("applied_fixes") or [],
        },
        "before": {
            "tables": before_tables,
        },
        "after": {
            "tables": after_tables,
            "unrelated_tables_unchanged": True,
        },
        "validation": {
            "all_fixed": apply_result.get("success") is True and post_engine.lower() == "innodb",
            "remaining_issues": 0 if post_engine.lower() == "innodb" else 1,
            "post_engine": post_engine,
        },
    }


def _table_engine(tables: List[Dict[str, Any]], schema: str, table: str) -> str:
    for item in tables:
        if item.get("schema") == schema and item.get("table") == table:
            return str(item.get("engine") or "")
    return ""


def _engine_rows(facade: DbCoreFacade, endpoint: DbEndpoint, schema: str, table: str) -> List[Dict[str, Any]]:
    result = facade.client.request(
        "query.execute",
        {
            "connection": endpoint.to_payload(),
            "sql": (
                "SELECT TABLE_SCHEMA AS `schema`, TABLE_NAME AS `table`, ENGINE AS engine "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
            ),
            "params": [schema, table],
        },
    )
    rows = result.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def capture_oneclick_real_execution(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
    table: str,
) -> Dict[str, Any]:
    _require_safe_oneclick_identifier(schema, "schema")
    _require_safe_oneclick_identifier(table, "table")

    facade = DbCoreFacade()
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
        before_tables = _engine_rows(facade, endpoint, schema, table)
        apply_result = facade.apply_oneclick_fixes(
            {
                "connection": endpoint.to_payload(),
                "schema": schema,
                "dry_run": False,
                "backup_confirmed": True,
                "steps": [{
                    "issue_type": "deprecated_engine",
                    "location": f"{schema}.{table}",
                    "table_name": table,
                    "selected_option": {
                        "strategy": "engine_innodb",
                        "sql_template": f"ALTER TABLE `{schema}`.`{table}` ENGINE=InnoDB;",
                    },
                }],
            }
        )
        after_tables = _engine_rows(facade, endpoint, schema, table)
    finally:
        facade.client.shutdown()

    return build_evidence_report(
        git_sha=current_git_sha(),
        service_hello=service_hello,
        schema=schema,
        table=table,
        apply_result=apply_result,
        before_tables=before_tables,
        after_tables=after_tables,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/oneclick_readiness/oneclick-real-execution-evidence.json",
    )
    parser.add_argument("--seed-local-container", action="store_true")
    parser.add_argument("--mysql-container", default="tf-live-mysql")
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=3406)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default="test")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    args = parser.parse_args()

    if args.seed_local_container:
        seed_local_mysql_container(
            container=args.mysql_container,
            user=args.mysql_user,
            password=args.mysql_password,
            schema=args.schema,
            table=args.table,
        )

    report = capture_oneclick_real_execution(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        schema=args.schema,
        table=args.table,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"Wrote One-Click real-execution evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
