import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate-oneclick-real-execution-evidence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validate_oneclick_real_execution_evidence",
        script,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _valid_evidence():
    return {
        "issue": 138,
        "git_sha": "abcdef123456",
        "source_type": "local_mysql_container",
        "feature_flags": {
            "oneclick_ui_enabled": True,
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
        "scope": {
            "schema": "tf_oneclick_real_execution",
            "allowed_fix_types": ["deprecated_engine"],
            "allowed_strategies": ["engine_innodb"],
        },
        "run": {
            "command": "oneclick.apply_fixes",
            "dry_run": False,
            "backup_confirmed": True,
            "success": True,
            "schema": "tf_oneclick_real_execution",
            "attempted_fix_types": ["deprecated_engine"],
            "attempted_strategies": ["engine_innodb"],
            "disallowed_fix_attempts": [],
            "applied_fixes": [
                {
                    "issue_type": "deprecated_engine",
                    "strategy": "engine_innodb",
                    "schema": "tf_oneclick_real_execution",
                    "table": "legacy_engine_table",
                    "sql": (
                        "ALTER TABLE `tf_oneclick_real_execution`."
                        "`legacy_engine_table` ENGINE=InnoDB;"
                    ),
                    "success": True,
                    "rows_affected": 0,
                }
            ],
        },
        "before": {
            "tables": [
                {
                    "schema": "tf_oneclick_real_execution",
                    "table": "legacy_engine_table",
                    "engine": "MyISAM",
                }
            ],
        },
        "after": {
            "tables": [
                {
                    "schema": "tf_oneclick_real_execution",
                    "table": "legacy_engine_table",
                    "engine": "InnoDB",
                }
            ],
            "unrelated_tables_unchanged": True,
        },
        "validation": {
            "all_fixed": True,
            "remaining_issues": 0,
            "post_engine": "InnoDB",
        },
    }


def _write_report(tmp_path, evidence):
    report = tmp_path / "oneclick-real-execution-evidence.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")
    return report


def test_oneclick_real_execution_evidence_accepts_engine_innodb_report(tmp_path):
    validator = _load_validator()
    report = _write_report(tmp_path, _valid_evidence())

    summary = validator.validate_report(report)

    assert summary == {
        "issue": 138,
        "schema": "tf_oneclick_real_execution",
        "applied_fixes": 1,
    }


def test_oneclick_real_execution_evidence_rejects_production_source(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["source_type"] = "production_mysql"
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="source_type"):
        validator.validate_report(report)


def test_oneclick_real_execution_evidence_rejects_unsafe_schema(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["scope"]["schema"] = "prod"
    evidence["run"]["schema"] = "prod"
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="tf_oneclick_"):
        validator.validate_report(report)


def test_oneclick_real_execution_evidence_rejects_disallowed_fix_type(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["run"]["attempted_fix_types"].append("charset_issue")
    evidence["run"]["disallowed_fix_attempts"] = ["charset_issue"]
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="disallowed"):
        validator.validate_report(report)


def test_oneclick_real_execution_evidence_requires_before_after_engine_proof(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["after"]["tables"][0]["engine"] = "MyISAM"
    evidence["validation"]["post_engine"] = "MyISAM"
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="InnoDB"):
        validator.validate_report(report)


def test_oneclick_real_execution_evidence_requires_boolean_real_execution_flag(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["feature_flags"]["oneclick_real_execution_enabled"] = "yes"
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="boolean"):
        validator.validate_report(report)


def test_regression_gate_can_require_oneclick_real_execution_evidence():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "rust-core-regression-gate.ps1"
    text = gate.read_text(encoding="utf-8")

    assert "RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE" in text
    assert "validate-oneclick-real-execution-evidence.py" in text
    assert "reports/oneclick_readiness/oneclick-real-execution-evidence.json" in text
