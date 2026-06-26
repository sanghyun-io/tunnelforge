#!/usr/bin/env python
"""Validate One-Click charset/collation real-execution evidence for GitHub #139."""

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
ALLOWED_FIX_TYPES = {"charset_issue"}
ALLOWED_STRATEGIES = {"charset_collation_fk_safe"}
SAFE_SCHEMA_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")
SAFE_IDENTIFIER_RE = re.compile(r"^tf_oneclick_[A-Za-z0-9_]+$")
SAFE_CHARSET_RE = re.compile(r"^[A-Za-z0-9_]+$")
SAFE_COLLATION_RE = re.compile(r"^[A-Za-z0-9_]+$")
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class EvidenceError(RuntimeError):
    """Raised when One-Click charset evidence is incomplete or unsafe."""


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
        raise EvidenceError(f"{label} must use a safe tf_oneclick_ SQL identifier")
    return identifier


def _require_safe_charset(value: Any, label: str) -> str:
    charset = _require_non_empty_text(value, label)
    if not SAFE_CHARSET_RE.fullmatch(charset):
        raise EvidenceError(f"{label} must be a safe charset token")
    return charset


def _require_safe_collation(value: Any, label: str) -> str:
    collation = _require_non_empty_text(value, label)
    if not SAFE_COLLATION_RE.fullmatch(collation):
        raise EvidenceError(f"{label} must be a safe collation token")
    return collation


def _require_git_sha(value: Any, label: str) -> str:
    sha = _require_non_empty_text(value, label)
    if not GIT_SHA_RE.fullmatch(sha):
        raise EvidenceError(f"{label} must be a real git SHA")
    return sha


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


def _expected_charset_sql(schema: str, table: str, charset: str, collation: str) -> str:
    return (
        f"ALTER TABLE `{schema}`.`{table}` "
        f"CONVERT TO CHARACTER SET {charset} COLLATE {collation};"
    )


def _require_table_state(
    tables: Any,
    schema: str,
    table: str,
    charset: str,
    collation: str,
    label: str,
) -> None:
    entry = _find_table(tables, schema, table, label)
    actual_charset = _require_safe_charset(entry.get("charset"), f"{label}.tables[].charset")
    actual_collation = _require_safe_collation(
        entry.get("collation"),
        f"{label}.tables[].collation",
    )
    if actual_charset.lower() != charset.lower():
        raise EvidenceError(f"{label}.tables[].charset must be {charset}")
    if actual_collation.lower() != collation.lower():
        raise EvidenceError(f"{label}.tables[].collation must be {collation}")


def _require_changed_from_before(
    before_tables: Any,
    schema: str,
    table: str,
    target_charset: str,
    target_collation: str,
) -> None:
    before = _find_table(before_tables, schema, table, "before")
    before_charset = _require_safe_charset(before.get("charset"), "before.tables[].charset")
    before_collation = _require_safe_collation(before.get("collation"), "before.tables[].collation")
    if before_charset.lower() == target_charset.lower() and before_collation.lower() == target_collation.lower():
        raise EvidenceError("before.tables[] must prove a charset/collation change")


def validate_report(report_path: Path | str) -> Dict[str, Any]:
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{report_path}: invalid JSON: {exc}") from exc

    report = _require_mapping(report, "report")
    if int(report.get("issue") or 0) != 139:
        raise EvidenceError("issue must be 139")
    _require_git_sha(report.get("git_sha"), "git_sha")
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
        raise EvidenceError("scope.allowed_fix_types must be charset_issue only")
    if allowed_strategies != ALLOWED_STRATEGIES:
        raise EvidenceError("scope.allowed_strategies must be charset_collation_fk_safe only")
    target_charset = _require_safe_charset(scope.get("target_charset"), "scope.target_charset")
    target_collation = _require_safe_collation(scope.get("target_collation"), "scope.target_collation")
    _require_bool(scope.get("requires_fk_safe_ordering"), "scope.requires_fk_safe_ordering")
    _require_bool(scope.get("requires_rollback_metadata"), "scope.requires_rollback_metadata")

    run = _require_mapping(report.get("run"), "run")
    if run.get("command") not in {"oneclick.apply_fixes", "oneclick.run"}:
        raise EvidenceError("run.command must be oneclick.apply_fixes or oneclick.run")
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

    before = _require_mapping(report.get("before"), "before")
    after = _require_mapping(report.get("after"), "after")
    rollback = _require_mapping(report.get("rollback"), "rollback")
    _require_bool(rollback.get("metadata_captured"), "rollback.metadata_captured")

    applied_fixes = run.get("applied_fixes")
    if not isinstance(applied_fixes, list) or not applied_fixes:
        raise EvidenceError("run.applied_fixes must include at least one applied fix")

    fixed_tables: set[str] = set()
    for index, fix_value in enumerate(applied_fixes):
        fix = _require_mapping(fix_value, f"run.applied_fixes[{index}]")
        issue_type = _require_non_empty_text(fix.get("issue_type"), f"run.applied_fixes[{index}].issue_type")
        strategy = _require_non_empty_text(fix.get("strategy"), f"run.applied_fixes[{index}].strategy")
        _require_only_allowed([issue_type], ALLOWED_FIX_TYPES, f"run.applied_fixes[{index}].issue_type")
        _require_only_allowed([strategy], ALLOWED_STRATEGIES, f"run.applied_fixes[{index}].strategy")
        fix_schema = _require_safe_schema(fix.get("schema"), f"run.applied_fixes[{index}].schema")
        if fix_schema != schema:
            raise EvidenceError(f"run.applied_fixes[{index}].schema must match scope.schema")
        if _require_safe_charset(fix.get("target_charset"), f"run.applied_fixes[{index}].target_charset") != target_charset:
            raise EvidenceError(f"run.applied_fixes[{index}].target_charset must match scope.target_charset")
        if (
            _require_safe_collation(fix.get("target_collation"), f"run.applied_fixes[{index}].target_collation")
            != target_collation
        ):
            raise EvidenceError(f"run.applied_fixes[{index}].target_collation must match scope.target_collation")
        _require_bool(fix.get("success"), f"run.applied_fixes[{index}].success")

        tables = _require_text_list(fix.get("tables"), f"run.applied_fixes[{index}].tables")
        fk_order = _require_text_list(fix.get("fk_order"), f"run.applied_fixes[{index}].fk_order")
        sql_values = _require_text_list(fix.get("sql"), f"run.applied_fixes[{index}].sql")
        rollback_sql = _require_text_list(
            fix.get("rollback_sql"),
            f"run.applied_fixes[{index}].rollback_sql",
        )
        if not rollback_sql:
            raise EvidenceError(f"run.applied_fixes[{index}].rollback_sql must include rollback SQL")
        if set(tables) != set(fk_order):
            raise EvidenceError(f"run.applied_fixes[{index}].fk_order must cover the same tables as tables")

        for table in tables:
            safe_table = _require_safe_identifier(table, f"run.applied_fixes[{index}].tables[]")
            fixed_tables.add(safe_table)
            expected = _expected_charset_sql(schema, safe_table, target_charset, target_collation)
            if expected not in sql_values:
                raise EvidenceError(f"run.applied_fixes[{index}].sql must include {expected}")
            _require_changed_from_before(before.get("tables"), schema, safe_table, target_charset, target_collation)
            _require_table_state(
                after.get("tables"),
                schema,
                safe_table,
                target_charset,
                target_collation,
                "after",
            )
            _find_table(rollback.get("tables"), schema, safe_table, "rollback")

    _require_bool(after.get("foreign_keys_valid"), "fk.after_foreign_keys_valid")
    _require_bool(after.get("unrelated_tables_unchanged"), "after.unrelated_tables_unchanged")

    validation = _require_mapping(report.get("validation"), "validation")
    _require_bool(validation.get("all_fixed"), "validation.all_fixed")
    remaining = _require_int_at_least(validation.get("remaining_issues"), "validation.remaining_issues", 0)
    if remaining != 0:
        raise EvidenceError(f"validation.remaining_issues must be 0; found {remaining}")
    _require_bool(validation.get("fk_constraints_valid"), "fk.validation_constraints_valid")
    if _require_safe_charset(validation.get("post_charset"), "validation.post_charset") != target_charset:
        raise EvidenceError("validation.post_charset must match scope.target_charset")
    if _require_safe_collation(validation.get("post_collation"), "validation.post_collation") != target_collation:
        raise EvidenceError("validation.post_collation must match scope.target_collation")

    return {
        "issue": 139,
        "schema": schema,
        "applied_fixes": len(applied_fixes),
        "tables": len(fixed_tables),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="reports/oneclick_readiness/oneclick-charset-evidence.json",
        help="Path to a completed One-Click charset/collation evidence JSON report.",
    )
    args = parser.parse_args()
    try:
        summary = validate_report(args.report)
    except (OSError, EvidenceError) as exc:
        print(f"One-Click charset/collation evidence failed: {exc}")
        return 1
    print(
        "One-Click charset/collation evidence passed: "
        f"{summary['applied_fixes']} applied fix(es), {summary['tables']} table(s) in {summary['schema']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
