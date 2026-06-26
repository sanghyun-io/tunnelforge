import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate-oneclick-dry-run-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("validate_oneclick_dry_run_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_capture():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture-oneclick-dry-run-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_oneclick_dry_run_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _valid_evidence():
    return {
        "issue": 137,
        "git_sha": "abcdef123456",
        "source_type": "local_mysql_container",
        "feature_flags": {
            "oneclick_ui_enabled": False,
            "oneclick_real_execution_enabled": False,
        },
        "service_hello": {
            "capabilities": [
                "oneclick.run",
                "oneclick.preflight",
                "oneclick.analyze",
                "oneclick.recommend",
                "oneclick.apply_fixes",
                "oneclick.validate",
                "oneclick.report",
            ],
        },
        "run": {
            "command": "oneclick.run",
            "dry_run": True,
            "backup_confirmed": True,
            "success": True,
            "schema": "tf_oneclick_readiness",
            "phase_events": ["preflight", "analysis", "recommendation", "execution", "validation"],
            "progress_percents": [5, 20, 40, 55, 80, 100],
            "preflight": {"passed": True, "checks": 3, "issues": 0},
            "analysis": {"table_count": 1, "total_issues": 0},
            "execution": {
                "dry_run": True,
                "success_count": 0,
                "fail_count": 0,
                "skip_count": 0,
                "log": ["DRY-RUN: no database changes were executed."],
            },
            "validation": {"all_fixed": True, "remaining_issues": 0},
            "report": {
                "success": True,
                "pre_issue_count": 0,
                "post_issue_count": 0,
                "execution_log": ["DRY-RUN: no database changes were executed."],
            },
        },
    }


def test_oneclick_dry_run_evidence_accepts_complete_report(tmp_path):
    validator = _load_validator()
    report = tmp_path / "oneclick-evidence.json"
    report.write_text(json.dumps(_valid_evidence()), encoding="utf-8")

    summary = validator.validate_report(report)

    assert summary == {"issue": 137, "phase_events": 5, "progress_events": 6}


def test_oneclick_dry_run_evidence_rejects_real_execution(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["run"]["dry_run"] = False
    report = tmp_path / "oneclick-evidence.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(validator.EvidenceError, match="dry_run must be true"):
        validator.validate_report(report)


def test_oneclick_dry_run_evidence_requires_all_oneclick_capabilities(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["service_hello"]["capabilities"].remove("oneclick.validate")
    report = tmp_path / "oneclick-evidence.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(validator.EvidenceError, match="missing service capability"):
        validator.validate_report(report)


def test_oneclick_dry_run_evidence_requires_dry_run_log(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["run"]["execution"]["log"] = []
    evidence["run"]["report"]["execution_log"] = []
    report = tmp_path / "oneclick-evidence.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(validator.EvidenceError, match="DRY-RUN"):
        validator.validate_report(report)


def test_regression_gate_can_require_oneclick_dry_run_evidence():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "rust-core-regression-gate.ps1"
    text = gate.read_text(encoding="utf-8")

    assert "RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE" in text
    assert "validate-oneclick-dry-run-evidence.py" in text
    assert "reports/oneclick_readiness/oneclick-dry-run-evidence.json" in text


def test_oneclick_capture_rejects_unsafe_seed_identifiers():
    capture = _load_capture()

    with pytest.raises(ValueError, match="unsafe One-Click schema"):
        capture._require_safe_oneclick_identifier("tf_oneclick_bad`; DROP DATABASE prod; --", "schema")

    with pytest.raises(ValueError, match="unsafe One-Click table"):
        capture._require_safe_oneclick_identifier("prod_table", "table")


def test_oneclick_capture_summarizes_core_events(tmp_path):
    capture = _load_capture()
    validator = _load_validator()
    events = [
        {"event": "phase", "phase": "preflight", "message": "started"},
        {"event": "progress", "percent": 5, "message": "Pre-flight started"},
        {
            "event": "preflight",
            "passed": True,
            "checks": [{"name": "MySQL engine"}, {"name": "Backup status"}],
            "issues": [],
        },
        {"event": "phase", "phase": "analysis", "message": "started"},
        {"event": "progress", "percent": 20, "message": "Analysis started"},
        {
            "event": "analysis",
            "summary": {"table_count": 1, "total_issues": 0},
        },
        {"event": "phase", "phase": "recommendation", "message": "started"},
        {"event": "progress", "percent": 40, "message": "Recommendation started"},
        {"event": "phase", "phase": "execution", "message": "started"},
        {"event": "progress", "percent": 80, "message": "Execution started"},
        {
            "event": "execution",
            "dry_run": True,
            "success_count": 0,
            "fail_count": 0,
            "skip_count": 0,
            "log": ["DRY-RUN: no database changes were executed."],
        },
        {"event": "phase", "phase": "validation", "message": "started"},
        {"event": "progress", "percent": 100, "message": "Validation complete"},
        {
            "event": "validation",
            "all_fixed": True,
            "remaining_issues": [],
        },
        {
            "event": "result",
            "command": "oneclick.run",
            "success": True,
            "report": {
                "schema": "tf_oneclick_readiness",
                "success": True,
                "pre_issue_count": 0,
                "post_issue_count": 0,
                "execution_log": ["DRY-RUN: no database changes were executed."],
            },
        },
    ]

    report = capture.build_evidence_report(
        git_sha="abcdef123456",
        service_hello={"capabilities": list(validator.REQUIRED_CAPABILITIES)},
        run_events=events,
    )

    assert report["issue"] == 137
    assert report["run"]["dry_run"] is True
    assert report["run"]["schema"] == "tf_oneclick_readiness"
    assert report["run"]["phase_events"] == [
        "preflight",
        "analysis",
        "recommendation",
        "execution",
        "validation",
    ]
    assert report["run"]["preflight"]["checks"] == 2

    report_path = tmp_path / "oneclick-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert validator.validate_report(report_path) == {
        "issue": 137,
        "phase_events": 5,
        "progress_events": 5,
    }
