#!/usr/bin/env python
"""Validate live bidirectional 1M UI migration evidence for GitHub #136."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


REQUIRED_DIRECTIONS = ("mysql_to_postgresql", "postgresql_to_mysql")
MIN_DIRECTION_ROWS = 1_000_000
MIN_STRESS_ROWS = 10_000_000


class EvidenceError(RuntimeError):
    """Raised when live UI migration evidence is incomplete or failing."""


def _require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _require_bool(value: Any, label: str) -> None:
    if value is not True:
        raise EvidenceError(f"{label} must be true")


def _require_int_at_least(value: Any, label: str, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{label} must be an integer") from exc
    if number < minimum:
        raise EvidenceError(f"{label} must be at least {minimum:,}; found {number:,}")
    return number


def _require_non_empty_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceError(f"{label} is required")
    return text


def _validate_direction(name: str, direction: Dict[str, Any]) -> int:
    rows = _require_int_at_least(
        direction.get("rows_migrated"),
        f"{name}.rows_migrated",
        MIN_DIRECTION_ROWS,
    )
    _require_bool(direction.get("migration_success"), f"{name}.migration_success")
    _require_bool(direction.get("verify_success"), f"{name}.verify_success")
    mismatches = int(direction.get("mismatches") or 0)
    if mismatches != 0:
        raise EvidenceError(f"{name}.mismatches must be 0; found {mismatches}")
    _require_int_at_least(
        direction.get("worker_progress_events"),
        f"{name}.worker_progress_events",
        1,
    )
    heartbeat = _require_mapping(direction.get("ui_heartbeat"), f"{name}.ui_heartbeat")
    _require_int_at_least(heartbeat.get("samples"), f"{name}.ui_heartbeat.samples", 1)
    max_gap = _require_int_at_least(
        heartbeat.get("max_gap_ms"),
        f"{name}.ui_heartbeat.max_gap_ms",
        0,
    )
    allowed_gap = _require_int_at_least(
        heartbeat.get("max_allowed_gap_ms"),
        f"{name}.ui_heartbeat.max_allowed_gap_ms",
        1,
    )
    if max_gap > allowed_gap:
        raise EvidenceError(
            f"{name}: UI heartbeat gap {max_gap}ms exceeds allowed {allowed_gap}ms"
        )
    return rows


def _validate_stress(stress: Dict[str, Any]) -> int:
    _require_non_empty_text(stress.get("source_type"), "stress_10m.source_type")
    rows = _require_int_at_least(stress.get("rows"), "stress_10m.rows", MIN_STRESS_ROWS)
    _require_bool(stress.get("resume_success"), "stress_10m.resume_success")
    _require_bool(stress.get("verify_success"), "stress_10m.verify_success")
    mismatches = int(stress.get("mismatches") or 0)
    if mismatches != 0:
        raise EvidenceError(f"stress_10m.mismatches must be 0; found {mismatches}")
    peak_rss = _require_int_at_least(stress.get("peak_rss_mb"), "stress_10m.peak_rss_mb", 1)
    rss_limit = _require_int_at_least(stress.get("rss_limit_mb"), "stress_10m.rss_limit_mb", 1)
    if peak_rss > rss_limit:
        raise EvidenceError(
            f"stress_10m peak RSS {peak_rss}MB exceeds limit {rss_limit}MB"
        )
    return rows


def validate_report(report_path: Path | str) -> Dict[str, Any]:
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{report_path}: invalid JSON: {exc}") from exc

    report = _require_mapping(report, "report")
    _require_non_empty_text(report.get("git_sha"), "git_sha")
    _require_non_empty_text(report.get("source_type"), "source_type")
    directions = _require_mapping(report.get("directions"), "directions")

    rows_checked = 0
    for name in REQUIRED_DIRECTIONS:
        if name not in directions:
            raise EvidenceError(f"missing direction: {name}")
        rows_checked += _validate_direction(name, _require_mapping(directions[name], name))

    rows_checked += _validate_stress(
        _require_mapping(report.get("stress_10m"), "stress_10m")
    )
    return {"directions_checked": len(REQUIRED_DIRECTIONS), "rows_checked": rows_checked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="reports/live_ui_migration/live-ui-migration-evidence.json",
        help="Path to a completed live UI migration evidence JSON report.",
    )
    args = parser.parse_args()
    try:
        summary = validate_report(args.report)
    except (OSError, EvidenceError) as exc:
        print(f"Live UI migration evidence failed: {exc}")
        return 1
    print(
        "Live UI migration evidence passed: "
        f"{summary['directions_checked']} directions, {summary['rows_checked']:,} rows checked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
