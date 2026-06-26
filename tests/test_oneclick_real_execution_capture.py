import importlib.util
import json
from pathlib import Path

import pytest


def _load_capture():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture-oneclick-real-execution-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_oneclick_real_execution_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate-oneclick-real-execution-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("validate_oneclick_real_execution_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_oneclick_real_capture_rejects_unsafe_seed_identifiers():
    capture = _load_capture()

    with pytest.raises(ValueError, match="unsafe One-Click schema"):
        capture._require_safe_oneclick_identifier("prod", "schema")

    with pytest.raises(ValueError, match="unsafe One-Click table"):
        capture._require_safe_oneclick_identifier("legacy_engine_table;DROP", "table")


def test_oneclick_real_capture_builds_validator_backed_report(tmp_path):
    capture = _load_capture()
    validator = _load_validator()
    service_hello = {"capabilities": list(validator.REQUIRED_CAPABILITIES)}
    apply_result = {
        "command": "oneclick.apply_fixes",
        "dry_run": False,
        "success": True,
        "success_count": 1,
        "fail_count": 0,
        "skip_count": 0,
        "disallowed_fix_attempts": [],
        "applied_fixes": [
            {
                "issue_type": "deprecated_engine",
                "strategy": "engine_innodb",
                "schema": "tf_oneclick_real_execution",
                "table": "tf_oneclick_legacy_engine_table",
                "sql": (
                    "ALTER TABLE `tf_oneclick_real_execution`."
                    "`tf_oneclick_legacy_engine_table` ENGINE=InnoDB;"
                ),
                "success": True,
                "rows_affected": 0,
            }
        ],
    }
    before = [
        {
            "schema": "tf_oneclick_real_execution",
            "table": "tf_oneclick_legacy_engine_table",
            "engine": "MyISAM",
        }
    ]
    after = [
        {
            "schema": "tf_oneclick_real_execution",
            "table": "tf_oneclick_legacy_engine_table",
            "engine": "InnoDB",
        }
    ]

    report = capture.build_evidence_report(
        git_sha="abcdef123456",
        service_hello=service_hello,
        schema="tf_oneclick_real_execution",
        table="tf_oneclick_legacy_engine_table",
        apply_result=apply_result,
        before_tables=before,
        after_tables=after,
    )

    report_path = tmp_path / "oneclick-real-execution-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert validator.validate_report(report_path) == {
        "issue": 138,
        "schema": "tf_oneclick_real_execution",
        "applied_fixes": 1,
    }
