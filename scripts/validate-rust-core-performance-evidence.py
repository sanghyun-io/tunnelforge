#!/usr/bin/env python
"""Validate archived Rust Core performance evidence JSONL files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable


REQUIRED_EVIDENCE: Dict[str, Dict[str, Any]] = {
    "perf_pg_mysql_1m_migrate.jsonl": {
        "kind": "migrate",
        "min_rows": 1_000_000,
        "label": "1M row migrate",
    },
    "perf_pg_mysql_1m_verify.jsonl": {
        "kind": "verify",
        "label": "1M row verify",
    },
    "perf_stress_10m_resume.jsonl": {
        "kind": "migrate",
        "min_rows": 10_000_000,
        "label": "10M resume/stress migrate",
    },
    "perf_stress_10m_verify.jsonl": {
        "kind": "verify",
        "label": "10M row verify",
    },
}


class EvidenceError(RuntimeError):
    """Raised when performance evidence is missing or invalid."""


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvidenceError(f"{path}: invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(event, dict):
                raise EvidenceError(f"{path}: line {line_number} is not a JSON object")
            yield event


def _rows_proven(event: Dict[str, Any]) -> int:
    rows = int(event.get("rows_copied") or 0)
    state = event.get("state")
    if isinstance(state, dict):
        for table in state.get("tables") or []:
            if isinstance(table, dict):
                rows = max(rows, int(table.get("rows_copied") or 0))
    return rows


def _successful_result(path: Path) -> Dict[str, Any]:
    for event in _read_jsonl(path):
        if event.get("event") == "result" and event.get("success") is True:
            return event
    raise EvidenceError(f"{path.name}: missing successful result event")


def validate_evidence_dir(evidence_dir: Path | str) -> Dict[str, Any]:
    evidence_dir = Path(evidence_dir)
    total_rows_proven = 0
    checked = 0
    for filename, requirement in REQUIRED_EVIDENCE.items():
        path = evidence_dir / filename
        if not path.is_file():
            raise EvidenceError(f"missing required evidence file: {filename}")
        result = _successful_result(path)
        checked += 1
        if requirement["kind"] == "verify":
            mismatches = result.get("mismatches")
            if mismatches not in (None, []):
                raise EvidenceError(f"{filename}: verification reported mismatches")
        min_rows = int(requirement.get("min_rows") or 0)
        if min_rows:
            rows = _rows_proven(result)
            if rows < min_rows:
                raise EvidenceError(
                    f"{filename}: expected at least {min_rows:,} rows, found {rows:,}"
                )
            total_rows_proven += rows
    return {"checked": checked, "total_rows_proven": total_rows_proven}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "evidence_dir",
        nargs="?",
        default="reports/rust_core_performance",
        help="Directory containing Rust Core performance JSONL evidence files.",
    )
    args = parser.parse_args()
    try:
        summary = validate_evidence_dir(args.evidence_dir)
    except EvidenceError as exc:
        print(f"Rust Core performance evidence failed: {exc}")
        return 1
    print(
        "Rust Core performance evidence passed: "
        f"{summary['checked']} files, {summary['total_rows_proven']:,} rows proven"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
