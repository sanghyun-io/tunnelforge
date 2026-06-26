#!/usr/bin/env python
"""Validate One-Click real-execution readiness evidence for GitHub #138."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List


REQUIRED_CAPABILITIES = (
    "oneclick.run",
    "oneclick.preflight",
    "oneclick.analyze",
    "oneclick.recommend",
    "oneclick.apply_fixes",
    "oneclick.validate",
    "oneclick.report",
)
ALLOWED_SOURCE_TYPE = "local_mysql_container"
ALLOWED_FIX_TYPES = {"deprecated_engine"}
ALLOWED_STRATEGIES = {"engine_innodb"}
SAFE_SCHEMA_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EvidenceError(RuntimeError):
    """Raised when One-Click real-execution evidence is incomplete or unsafe."""


def _require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _require_non_empty_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceError(f"{label} is required")
    return text


def _require_bool(value: Any, label: str) -> None:
    if value is not True:
        raise EvidenceError(f"{label} must be true")


def _require_int_at_least(value: Any, label: str, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{label} must be an integer") from exc
    if number < minimum:
        raise EvidenceError(f"{label} must be at least {minimum}; found {number}")
    return number


def _require_text_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    values = [str(item).strip() for item in value]
    if any(not item for item in values):
        raise EvidenceError(f"{label} must not contain empty values")
    return values


def _require_safe_schema(value: Any, label: str) -> str:
    schema = _require_non_empty_text(value, label)
    if not SAFE_SCHEMA_RE.fullmatch(schema):
        raise EvidenceError(f"{label} must use a safe tf_oneclick_ test schema")
    return schema


def _require_safe_identifier(value: Any, label: str) -> str:
    identifier = _require_non_empty_text(value, label)
    if not SAFE_IDENTIFIER_RE.fullmatch(identifier):
        raise EvidenceError(f"{label} must be a safe SQL identifier")
    return identifier


def _require_only_allowed(values: Iterable[str], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(values) - allowed)
    if unexpected:
        raise EvidenceError(f"{label} contains disallowed values: {', '.join(unexpected)}")


def _find_table(tables: Any, schema: str, table: str, label: str) -> Dict[str, Any]:
    if not isinstance(tables, list):
        raise EvidenceError(f"{label}.tables must be an array")
    for entry in tables:
        candidate = _require_mapping(entry, f"{label}.tables[]")
        if candidate.get("schema") == schema and candidate.get("table") == table:
            return candidate
    raise EvidenceError(f"{label}.tables must include {schema}.{table}")


def _expected_engine_sql(schema: str, table: str) -> str:
    return f"ALTER TABLE `{schema}`.`{table}` ENGINE=InnoDB;"


def validate_report(report_path: Path | str) -> Dict[str, Any]:
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{report_path}: invalid JSON: {exc}") from exc

    report = _require_mapping(report, "report")
    if int(report.get("issue") or 0) != 138:
        raise EvidenceError("issue must be 138")
    _require_non_empty_text(report.get("git_sha"), "git_sha")
    source_type = _require_non_empty_text(report.get("source_type"), "source_type")
    if source_type != ALLOWED_SOURCE_TYPE:
        raise EvidenceError(f"source_type must be {ALLOWED_SOURCE_TYPE}")

    flags = _require_mapping(report.get("feature_flags"), "feature_flags")
    _require_bool(flags.get("oneclick_ui_enabled"), "feature_flags.oneclick_ui_enabled")
    if "oneclick_real_execution_enabled" in flags and not isinstance(
        flags.get("oneclick_real_execution_enabled"),
        bool,
    ):
        raise EvidenceError("feature_flags.oneclick_real_execution_enabled must be boolean")

    hello = _require_mapping(report.get("service_hello"), "service_hello")
    capabilities = {str(item) for item in hello.get("capabilities") or []}
    for capability in REQUIRED_CAPABILITIES:
        if capability not in capabilities:
            raise EvidenceError(f"missing service capability: {capability}")

    scope = _require_mapping(report.get("scope"), "scope")
    schema = _require_safe_schema(scope.get("schema"), "scope.schema")
    allowed_fix_types = set(_require_text_list(scope.get("allowed_fix_types"), "scope.allowed_fix_types"))
    allowed_strategies = set(
        _require_text_list(scope.get("allowed_strategies"), "scope.allowed_strategies")
    )
    if allowed_fix_types != ALLOWED_FIX_TYPES:
        raise EvidenceError("scope.allowed_fix_types must be deprecated_engine only")
    if allowed_strategies != ALLOWED_STRATEGIES:
        raise EvidenceError("scope.allowed_strategies must be engine_innodb only")

    run = _require_mapping(report.get("run"), "run")
    if run.get("command") != "oneclick.apply_fixes":
        raise EvidenceError("run.command must be oneclick.apply_fixes")
    if run.get("dry_run") is not False:
        raise EvidenceError("run.dry_run must be false")
    _require_bool(run.get("backup_confirmed"), "run.backup_confirmed")
    _require_bool(run.get("success"), "run.success")
    run_schema = _require_safe_schema(run.get("schema"), "run.schema")
    if run_schema != schema:
        raise EvidenceError("run.schema must match scope.schema")

    attempted_fix_types = _require_text_list(run.get("attempted_fix_types"), "run.attempted_fix_types")
    attempted_strategies = _require_text_list(
        run.get("attempted_strategies"),
        "run.attempted_strategies",
    )
    _require_only_allowed(attempted_fix_types, ALLOWED_FIX_TYPES, "run.attempted_fix_types")
    _require_only_allowed(attempted_strategies, ALLOWED_STRATEGIES, "run.attempted_strategies")
    disallowed = _require_text_list(
        run.get("disallowed_fix_attempts", []),
        "run.disallowed_fix_attempts",
    )
    if disallowed:
        raise EvidenceError(f"run.disallowed_fix_attempts must be empty; found disallowed {disallowed}")

    applied_fixes = run.get("applied_fixes")
    if not isinstance(applied_fixes, list) or not applied_fixes:
        raise EvidenceError("run.applied_fixes must include at least one applied fix")

    before = _require_mapping(report.get("before"), "before")
    after = _require_mapping(report.get("after"), "after")
    for index, fix_value in enumerate(applied_fixes):
        fix = _require_mapping(fix_value, f"run.applied_fixes[{index}]")
        issue_type = _require_non_empty_text(fix.get("issue_type"), f"run.applied_fixes[{index}].issue_type")
        strategy = _require_non_empty_text(fix.get("strategy"), f"run.applied_fixes[{index}].strategy")
        _require_only_allowed([issue_type], ALLOWED_FIX_TYPES, f"run.applied_fixes[{index}].issue_type")
        _require_only_allowed([strategy], ALLOWED_STRATEGIES, f"run.applied_fixes[{index}].strategy")
        fix_schema = _require_safe_schema(fix.get("schema"), f"run.applied_fixes[{index}].schema")
        if fix_schema != schema:
            raise EvidenceError(f"run.applied_fixes[{index}].schema must match scope.schema")
        table = _require_safe_identifier(fix.get("table"), f"run.applied_fixes[{index}].table")
        if fix.get("sql") != _expected_engine_sql(schema, table):
            raise EvidenceError(f"run.applied_fixes[{index}].sql must be the quoted engine_innodb SQL")
        _require_bool(fix.get("success"), f"run.applied_fixes[{index}].success")
        _require_int_at_least(fix.get("rows_affected"), f"run.applied_fixes[{index}].rows_affected", 0)

        before_table = _find_table(before.get("tables"), schema, table, "before")
        after_table = _find_table(after.get("tables"), schema, table, "after")
        before_engine = _require_non_empty_text(before_table.get("engine"), "before.tables[].engine")
        after_engine = _require_non_empty_text(after_table.get("engine"), "after.tables[].engine")
        if before_engine.lower() == "innodb":
            raise EvidenceError("before.tables[].engine must prove a deprecated non-InnoDB engine")
        if after_engine.lower() != "innodb":
            raise EvidenceError("after.tables[].engine must be InnoDB")

    _require_bool(after.get("unrelated_tables_unchanged"), "after.unrelated_tables_unchanged")

    validation = _require_mapping(report.get("validation"), "validation")
    _require_bool(validation.get("all_fixed"), "validation.all_fixed")
    remaining = _require_int_at_least(validation.get("remaining_issues"), "validation.remaining_issues", 0)
    if remaining != 0:
        raise EvidenceError(f"validation.remaining_issues must be 0; found {remaining}")
    if _require_non_empty_text(validation.get("post_engine"), "validation.post_engine").lower() != "innodb":
        raise EvidenceError("validation.post_engine must be InnoDB")

    return {"issue": 138, "schema": schema, "applied_fixes": len(applied_fixes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="reports/oneclick_readiness/oneclick-real-execution-evidence.json",
        help="Path to a completed One-Click real-execution evidence JSON report.",
    )
    args = parser.parse_args()
    try:
        summary = validate_report(args.report)
    except (OSError, EvidenceError) as exc:
        print(f"One-Click real-execution evidence failed: {exc}")
        return 1
    print(
        "One-Click real-execution evidence passed: "
        f"{summary['applied_fixes']} applied fix(es) in {summary['schema']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
