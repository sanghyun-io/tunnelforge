#!/usr/bin/env python
"""Validate PyQt-triggered One-Click charset derivation evidence for GitHub #140."""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_VALIDATOR_PATH = PROJECT_ROOT / "scripts" / "validate-oneclick-charset-evidence.py"
REQUIRED_CAPABILITIES = (
    "oneclick.run",
    "oneclick.preflight",
    "oneclick.analyze",
    "oneclick.recommend",
    "oneclick.derive_charset_contracts",
    "oneclick.apply_fixes",
    "oneclick.validate",
    "oneclick.report",
)


def _load_base_validator():
    spec = importlib.util.spec_from_file_location("validate_oneclick_charset_evidence", BASE_VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_base = _load_base_validator()
EvidenceError = _base.EvidenceError


def _require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _require_bool_true(value: Any, label: str) -> None:
    if value is not True:
        raise EvidenceError(f"{label} must be true")


def _require_bool_false(value: Any, label: str) -> None:
    if value is not False:
        raise EvidenceError(f"{label} must be false")


def _require_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceError(f"{label} is required")
    return text


def _require_list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    if not value:
        raise EvidenceError(f"{label} must not be empty")
    return value


def _require_count(value: Any, expected: int, label: str) -> None:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{label} must be an integer") from exc
    if count != expected:
        raise EvidenceError(f"{label} must be {expected}; found {count}")


def _validate_base_charset_contract(report: Dict[str, Any], report_path: Path) -> Dict[str, Any]:
    base_report = dict(report)
    base_report["issue"] = 139
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".json",
        prefix=f"{report_path.stem}-base-",
        delete=False,
    ) as handle:
        json.dump(base_report, handle)
        temp_path = Path(handle.name)
    try:
        return _base.validate_report(temp_path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def validate_report(report_path: Path | str) -> Dict[str, Any]:
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{report_path}: invalid JSON: {exc}") from exc
    report = _require_mapping(report, "report")

    if int(report.get("issue") or 0) != 140:
        raise EvidenceError("issue must be 140")

    hello = _require_mapping(report.get("service_hello"), "service_hello")
    capabilities = {str(item) for item in hello.get("capabilities") or []}
    for capability in REQUIRED_CAPABILITIES:
        if capability not in capabilities:
            raise EvidenceError(f"missing service capability: {capability}")

    run = _require_mapping(report.get("run"), "run")
    if run.get("command") != "oneclick.run":
        raise EvidenceError("run.command must be oneclick.run for #140 derivation evidence")

    scope = _require_mapping(report.get("scope"), "scope")
    schema = _base._require_safe_schema(scope.get("schema"), "scope.schema")
    target_charset = _base._require_safe_charset(scope.get("target_charset"), "scope.target_charset")
    target_collation = _base._require_safe_collation(scope.get("target_collation"), "scope.target_collation")

    derivation = _require_mapping(report.get("derivation"), "derivation")
    if derivation.get("command") != "oneclick.derive_charset_contracts":
        raise EvidenceError("derivation.command must be oneclick.derive_charset_contracts")
    _require_bool_true(derivation.get("success"), "derivation.success")
    if _require_text(derivation.get("source"), "derivation.source") != "live_mysql_information_schema":
        raise EvidenceError("derivation.source must be live_mysql_information_schema")
    if _base._require_safe_schema(derivation.get("schema"), "derivation.schema") != schema:
        raise EvidenceError("derivation.schema must match scope.schema")
    _require_bool_false(derivation.get("payload_had_table_facts"), "derivation.payload_had_table_facts")
    _require_bool_false(derivation.get("payload_had_issues"), "derivation.payload_had_issues")
    issues = _require_list(derivation.get("issues"), "derivation.issues")
    contracts = _require_list(derivation.get("contracts"), "derivation.contracts")
    _require_count(derivation.get("issues_count"), len(issues), "derivation.issues_count")
    _require_count(derivation.get("contracts_count"), len(contracts), "derivation.contracts_count")

    first_contract = _require_mapping(contracts[0], "derivation.contracts[0]")
    if _base._require_safe_charset(first_contract.get("target_charset"), "derivation.contracts[].target_charset") != target_charset:
        raise EvidenceError("derivation contract target_charset must match scope.target_charset")
    if (
        _base._require_safe_collation(
            first_contract.get("target_collation"),
            "derivation.contracts[].target_collation",
        )
        != target_collation
    ):
        raise EvidenceError("derivation contract target_collation must match scope.target_collation")
    _require_list(first_contract.get("rollback_sql"), "derivation.contracts[].rollback_sql")

    pyqt_payload = _require_mapping(report.get("pyqt_payload"), "pyqt_payload")
    if pyqt_payload.get("builder") != "OneClickMigrationWorker._core_payload":
        raise EvidenceError("pyqt_payload.builder must be OneClickMigrationWorker._core_payload")
    if pyqt_payload.get("dry_run") is not False:
        raise EvidenceError("pyqt_payload.dry_run must be false")
    _require_bool_true(pyqt_payload.get("backup_confirmed"), "pyqt_payload.backup_confirmed")
    _require_bool_true(pyqt_payload.get("included_derived_issues"), "pyqt_payload.included_derived_issues")
    _require_bool_true(pyqt_payload.get("included_charset_contracts"), "pyqt_payload.included_charset_contracts")
    _require_count(pyqt_payload.get("issues_count"), len(issues), "pyqt_payload.issues_count")
    _require_count(pyqt_payload.get("contracts_count"), len(contracts), "pyqt_payload.contracts_count")

    base_summary = _validate_base_charset_contract(report, report_path)
    return {
        "issue": 140,
        "schema": schema,
        "derived_contracts": len(contracts),
        "applied_fixes": base_summary["applied_fixes"],
        "tables": base_summary["tables"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="reports/oneclick_readiness/oneclick-charset-derivation-evidence.json",
        help="Path to a completed One-Click charset derivation evidence JSON report.",
    )
    args = parser.parse_args()
    try:
        summary = validate_report(args.report)
    except (OSError, EvidenceError) as exc:
        print(f"One-Click charset derivation evidence failed: {exc}")
        return 1
    print(
        "One-Click charset derivation evidence passed: "
        f"{summary['derived_contracts']} derived contract(s), "
        f"{summary['applied_fixes']} applied fix(es), "
        f"{summary['tables']} table(s) in {summary['schema']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
