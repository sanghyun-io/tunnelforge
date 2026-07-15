#!/usr/bin/env python
"""Validate One-Click migration dry-run readiness evidence for GitHub #137."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


REQUIRED_CAPABILITIES = (
    "oneclick.run",
    "oneclick.preflight",
    "oneclick.analyze",
    "oneclick.recommend",
    "oneclick.apply_fixes",
    "oneclick.validate",
    "oneclick.report",
)
REQUIRED_PHASES = ("preflight", "analysis", "recommendation", "execution", "validation")
DRY_RUN_MESSAGE = "DRY-RUN: no database changes were executed."


class EvidenceError(RuntimeError):
    """Raised when One-Click dry-run evidence is incomplete or unsafe."""


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


def _require_dry_run_log(values: Any, label: str) -> None:
    if not isinstance(values, list) or DRY_RUN_MESSAGE not in [str(item) for item in values]:
        raise EvidenceError(f"{label} must include {DRY_RUN_MESSAGE!r}")


def validate_report(report_path: Path | str) -> Dict[str, Any]:
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{report_path}: invalid JSON: {exc}") from exc

    report = _require_mapping(report, "report")
    if int(report.get("issue") or 0) != 137:
        raise EvidenceError("issue must be 137")
    _require_non_empty_text(report.get("git_sha"), "git_sha")
    _require_non_empty_text(report.get("source_type"), "source_type")

    flags = _require_mapping(report.get("feature_flags"), "feature_flags")
    _require_bool(flags.get("oneclick_ui_enabled"), "feature_flags.oneclick_ui_enabled")
    if flags.get("oneclick_real_execution_enabled") is not False:
        raise EvidenceError("feature_flags.oneclick_real_execution_enabled must be false")

    hello = _require_mapping(report.get("service_hello"), "service_hello")
    capabilities = {str(item) for item in hello.get("capabilities") or []}
    for capability in REQUIRED_CAPABILITIES:
        if capability not in capabilities:
            raise EvidenceError(f"missing service capability: {capability}")

    run = _require_mapping(report.get("run"), "run")
    if run.get("command") != "oneclick.run":
        raise EvidenceError("run.command must be oneclick.run")
    if run.get("dry_run") is not True:
        raise EvidenceError("run.dry_run must be true")
    _require_bool(run.get("backup_confirmed"), "run.backup_confirmed")
    _require_bool(run.get("success"), "run.success")
    _require_non_empty_text(run.get("schema"), "run.schema")

    phases = [str(item) for item in run.get("phase_events") or []]
    for phase in REQUIRED_PHASES:
        if phase not in phases:
            raise EvidenceError(f"missing phase event: {phase}")
    progress = run.get("progress_percents")
    if not isinstance(progress, list):
        raise EvidenceError("run.progress_percents must be an array")
    if 100 not in [int(item or 0) for item in progress]:
        raise EvidenceError("run.progress_percents must include 100")

    preflight = _require_mapping(run.get("preflight"), "run.preflight")
    _require_bool(preflight.get("passed"), "run.preflight.passed")
    _require_int_at_least(preflight.get("checks"), "run.preflight.checks", 1)
    _require_int_at_least(preflight.get("issues"), "run.preflight.issues", 0)

    analysis = _require_mapping(run.get("analysis"), "run.analysis")
    _require_int_at_least(analysis.get("table_count"), "run.analysis.table_count", 1)
    _require_int_at_least(analysis.get("total_issues"), "run.analysis.total_issues", 0)

    execution = _require_mapping(run.get("execution"), "run.execution")
    if execution.get("dry_run") is not True:
        raise EvidenceError("run.execution.dry_run must be true")
    _require_int_at_least(execution.get("success_count"), "run.execution.success_count", 0)
    fail_count = _require_int_at_least(execution.get("fail_count"), "run.execution.fail_count", 0)
    if fail_count != 0:
        raise EvidenceError(f"run.execution.fail_count must be 0; found {fail_count}")
    _require_int_at_least(execution.get("skip_count"), "run.execution.skip_count", 0)
    _require_dry_run_log(execution.get("log"), "run.execution.log")

    validation = _require_mapping(run.get("validation"), "run.validation")
    _require_bool(validation.get("all_fixed"), "run.validation.all_fixed")
    remaining = _require_int_at_least(
        validation.get("remaining_issues"),
        "run.validation.remaining_issues",
        0,
    )
    if remaining != 0:
        raise EvidenceError(f"run.validation.remaining_issues must be 0; found {remaining}")

    result_report = _require_mapping(run.get("report"), "run.report")
    _require_bool(result_report.get("success"), "run.report.success")
    if int(result_report.get("post_issue_count") or 0) != 0:
        raise EvidenceError("run.report.post_issue_count must be 0")
    _require_dry_run_log(result_report.get("execution_log"), "run.report.execution_log")

    return {"issue": 137, "phase_events": len(phases), "progress_events": len(progress)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="reports/oneclick_readiness/oneclick-dry-run-evidence.json",
        help="Path to a completed One-Click dry-run evidence JSON report.",
    )
    args = parser.parse_args()
    try:
        summary = validate_report(args.report)
    except (OSError, EvidenceError) as exc:
        print(f"One-Click dry-run evidence failed: {exc}")
        return 1
    print(
        "One-Click dry-run evidence passed: "
        f"{summary['phase_events']} phase events, {summary['progress_events']} progress events"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
