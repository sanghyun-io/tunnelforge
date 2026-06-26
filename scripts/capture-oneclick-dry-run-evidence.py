#!/usr/bin/env python
"""Capture Rust Core-backed One-Click dry-run evidence for GitHub #137."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.db_core_service import DbCoreFacade, DbEndpoint  # noqa: E402
from src.ui.dialogs.migration_dialogs import ONE_CLICK_MIGRATION_FEATURE_ENABLED  # noqa: E402
from src.ui.dialogs.oneclick_migration_dialog import ONECLICK_REAL_EXECUTION_ENABLED  # noqa: E402


DEFAULT_SCHEMA = "tf_oneclick_readiness"
DEFAULT_TABLE = "tf_oneclick_sample"
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
  `name` VARCHAR(64) NOT NULL,
  `created_at` DATETIME NOT NULL
);
INSERT INTO `{table}` (`id`, `name`, `created_at`) VALUES
  (1, 'dry-run-row-1', TIMESTAMP('2026-01-01 00:00:00')),
  (2, 'dry-run-row-2', TIMESTAMP('2026-01-01 00:01:00')),
  (3, 'dry-run-row-3', TIMESTAMP('2026-01-01 00:02:00'));
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


def _require_safe_oneclick_identifier(value: str, label: str) -> None:
    if not SAFE_ONECLICK_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"refusing to seed unsafe One-Click {label}: {value!r}")


def build_evidence_report(
    *,
    git_sha: str,
    service_hello: Dict[str, Any],
    run_events: List[Dict[str, Any]],
    source_type: str = "local_mysql_container",
) -> Dict[str, Any]:
    phases = [
        str(event.get("phase"))
        for event in run_events
        if event.get("event") == "phase" and event.get("phase")
    ]
    progress = [
        int(event.get("percent") or 0)
        for event in run_events
        if event.get("event") == "progress"
    ]
    preflight = _last_event(run_events, "preflight")
    analysis = _last_event(run_events, "analysis")
    execution = _last_event(run_events, "execution")
    validation = _last_event(run_events, "validation")
    result = _last_event(run_events, "result")
    result_report = result.get("report") if isinstance(result.get("report"), dict) else {}

    return {
        "issue": 137,
        "git_sha": git_sha,
        "source_type": source_type,
        "feature_flags": {
            "oneclick_ui_enabled": bool(ONE_CLICK_MIGRATION_FEATURE_ENABLED),
            "oneclick_real_execution_enabled": bool(ONECLICK_REAL_EXECUTION_ENABLED),
        },
        "service_hello": {
            "capabilities": service_hello.get("capabilities") or [],
        },
        "run": {
            "command": result.get("command") or "oneclick.run",
            "dry_run": bool(execution.get("dry_run")),
            "backup_confirmed": True,
            "success": result.get("success") is True,
            "schema": str(result_report.get("schema") or ""),
            "phase_events": phases,
            "progress_percents": progress,
            "preflight": {
                "passed": preflight.get("passed") is True,
                "checks": len(preflight.get("checks") or []),
                "issues": len(preflight.get("issues") or []),
            },
            "analysis": {
                "table_count": int((analysis.get("summary") or {}).get("table_count") or 0),
                "total_issues": int((analysis.get("summary") or {}).get("total_issues") or 0),
            },
            "execution": {
                "dry_run": execution.get("dry_run") is True,
                "success_count": int(execution.get("success_count") or 0),
                "fail_count": int(execution.get("fail_count") or 0),
                "skip_count": int(execution.get("skip_count") or 0),
                "log": execution.get("log") if isinstance(execution.get("log"), list) else [],
            },
            "validation": {
                "all_fixed": validation.get("all_fixed") is True,
                "remaining_issues": len(validation.get("remaining_issues") or []),
            },
            "report": {
                "success": result_report.get("success") is True,
                "pre_issue_count": int(result_report.get("pre_issue_count") or 0),
                "post_issue_count": int(result_report.get("post_issue_count") or 0),
                "execution_log": (
                    result_report.get("execution_log")
                    if isinstance(result_report.get("execution_log"), list)
                    else []
                ),
            },
        },
    }


def _last_event(events: List[Dict[str, Any]], event_name: str) -> Dict[str, Any]:
    for event in reversed(events):
        if event.get("event") == event_name:
            return event
    return {}


def capture_oneclick_dry_run(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    schema: str,
) -> Dict[str, Any]:
    facade = DbCoreFacade()
    events: List[Dict[str, Any]] = []
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
        facade.run_oneclick(
            {
                "connection": endpoint.to_payload(),
                "schema": schema,
                "dry_run": True,
                "backup_confirmed": True,
            },
            on_event=events.append,
        )
    finally:
        facade.client.shutdown()

    return build_evidence_report(
        git_sha=current_git_sha(),
        service_hello=service_hello,
        run_events=events,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/oneclick_readiness/oneclick-dry-run-evidence.json")
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

    report = capture_oneclick_dry_run(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        schema=args.schema,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote One-Click dry-run evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
