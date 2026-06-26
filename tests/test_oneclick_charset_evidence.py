import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate-oneclick-charset-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("validate_oneclick_charset_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _valid_evidence():
    schema = "tf_oneclick_charset"
    table = "tf_oneclick_parent"
    child = "tf_oneclick_child"
    return {
        "issue": 139,
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
            "schema": schema,
            "allowed_fix_types": ["charset_issue"],
            "allowed_strategies": ["charset_collation_fk_safe"],
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci",
            "requires_fk_safe_ordering": True,
            "requires_rollback_metadata": True,
        },
        "run": {
            "command": "oneclick.apply_fixes",
            "dry_run": False,
            "backup_confirmed": True,
            "success": True,
            "schema": schema,
            "attempted_fix_types": ["charset_issue"],
            "attempted_strategies": ["charset_collation_fk_safe"],
            "disallowed_fix_attempts": [],
            "applied_fixes": [
                {
                    "issue_type": "charset_issue",
                    "strategy": "charset_collation_fk_safe",
                    "schema": schema,
                    "tables": [table, child],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                    ],
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                    ],
                    "fk_order": [table, child],
                    "success": True,
                }
            ],
        },
        "before": {
            "tables": [
                {
                    "schema": schema,
                    "table": table,
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
                {
                    "schema": schema,
                    "table": child,
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
            ],
            "foreign_keys": [
                {
                    "schema": schema,
                    "table": child,
                    "referenced_table": table,
                    "constraint": "fk_child_parent",
                }
            ],
        },
        "after": {
            "tables": [
                {
                    "schema": schema,
                    "table": table,
                    "charset": "utf8mb4",
                    "collation": "utf8mb4_0900_ai_ci",
                },
                {
                    "schema": schema,
                    "table": child,
                    "charset": "utf8mb4",
                    "collation": "utf8mb4_0900_ai_ci",
                },
            ],
            "foreign_keys_valid": True,
            "unrelated_tables_unchanged": True,
        },
        "rollback": {
            "metadata_captured": True,
            "tables": [
                {
                    "schema": schema,
                    "table": table,
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
                {
                    "schema": schema,
                    "table": child,
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
            ],
        },
        "validation": {
            "all_fixed": True,
            "remaining_issues": 0,
            "fk_constraints_valid": True,
            "post_charset": "utf8mb4",
            "post_collation": "utf8mb4_0900_ai_ci",
        },
    }


def _write_report(tmp_path, evidence):
    report = tmp_path / "oneclick-charset-evidence.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")
    return report


def test_oneclick_charset_evidence_accepts_fk_safe_report(tmp_path):
    validator = _load_validator()
    report = _write_report(tmp_path, _valid_evidence())

    assert validator.validate_report(report) == {
        "issue": 139,
        "schema": "tf_oneclick_charset",
        "applied_fixes": 1,
        "tables": 2,
    }


def test_oneclick_charset_evidence_rejects_production_source(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["source_type"] = "production"
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="local_mysql_container"):
        validator.validate_report(report)


def test_oneclick_charset_evidence_rejects_unsafe_schema(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["scope"]["schema"] = "prod"
    evidence["run"]["schema"] = "prod"
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="tf_oneclick_"):
        validator.validate_report(report)


def test_oneclick_charset_evidence_rejects_disallowed_strategy(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["run"]["attempted_strategies"].append("charset_collation_single")
    evidence["run"]["disallowed_fix_attempts"] = ["charset_issue:charset_collation_single"]
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="disallowed"):
        validator.validate_report(report)


def test_oneclick_charset_evidence_requires_fk_validation(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["after"]["foreign_keys_valid"] = False
    evidence["validation"]["fk_constraints_valid"] = False
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="fk"):
        validator.validate_report(report)


def test_oneclick_charset_evidence_requires_rollback_metadata(tmp_path):
    validator = _load_validator()
    evidence = _valid_evidence()
    evidence["rollback"]["metadata_captured"] = False
    evidence["run"]["applied_fixes"][0]["rollback_sql"] = []
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="rollback"):
        validator.validate_report(report)


def test_regression_gate_can_require_oneclick_charset_evidence():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "rust-core-regression-gate.ps1"
    text = gate.read_text(encoding="utf-8")

    assert "RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE" in text
    assert "validate-oneclick-charset-evidence.py" in text
    assert "reports/oneclick_readiness/oneclick-charset-evidence.json" in text


def test_oneclick_charset_evidence_rejects_template_file():
    validator = _load_validator()
    template = (
        Path(__file__).resolve().parents[1]
        / "reports"
        / "oneclick_readiness"
        / "oneclick-charset-evidence.template.json"
    )

    with pytest.raises(validator.EvidenceError, match="git_sha"):
        validator.validate_report(template)
