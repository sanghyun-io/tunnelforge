import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate-live-ui-migration-evidence.py"
    spec = importlib.util.spec_from_file_location("validate_live_ui_migration_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _valid_evidence():
    direction = {
        "rows_migrated": 1_000_000,
        "migration_success": True,
        "verify_success": True,
        "mismatches": 0,
        "worker_progress_events": 20,
        "ui_heartbeat": {
            "samples": 120,
            "max_gap_ms": 250,
            "max_allowed_gap_ms": 1000,
        },
    }
    return {
        "issue": 136,
        "git_sha": "abcdef123456",
        "source_type": "local_containers",
        "directions": {
            "mysql_to_postgresql": dict(direction),
            "postgresql_to_mysql": dict(direction),
        },
        "stress_10m": {
            "source_type": "synthetic_adapter",
            "rows": 10_000_000,
            "resume_success": True,
            "verify_success": True,
            "mismatches": 0,
            "peak_rss_mb": 512,
            "rss_limit_mb": 2048,
        },
    }


def test_live_ui_migration_evidence_accepts_complete_report(tmp_path):
    validator = _load_validator()
    report = tmp_path / "live-ui-report.json"
    report.write_text(json.dumps(_valid_evidence()), encoding="utf-8")

    summary = validator.validate_report(report)

    assert summary["directions_checked"] == 2
    assert summary["rows_checked"] == 12_000_000


def test_live_ui_migration_evidence_rejects_ui_freeze_gap(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["directions"]["mysql_to_postgresql"]["ui_heartbeat"]["max_gap_ms"] = 5000
    report = tmp_path / "live-ui-report.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(validator.EvidenceError, match="UI heartbeat gap"):
        validator.validate_report(report)


def test_live_ui_migration_evidence_rejects_missing_direction(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["directions"].pop("postgresql_to_mysql")
    report = tmp_path / "live-ui-report.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(validator.EvidenceError, match="missing direction"):
        validator.validate_report(report)


def test_regression_gate_can_require_live_ui_evidence():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "rust-core-regression-gate.ps1"
    text = gate.read_text(encoding="utf-8")

    assert "RUST_CORE_REQUIRE_LIVE_UI_EVIDENCE" in text
    assert "validate-live-ui-migration-evidence.py" in text
    assert "reports/live_ui_migration/live-ui-migration-evidence.json" in text
