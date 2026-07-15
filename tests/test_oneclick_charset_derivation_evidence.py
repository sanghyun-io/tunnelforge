import importlib.util
import json
from pathlib import Path

import pytest


def _load_validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate-oneclick-charset-derivation-evidence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validate_oneclick_charset_derivation_evidence",
        script,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _archived_evidence():
    schema = "tf_oneclick_derive_charset"
    parent = "tf_oneclick_parent"
    child = "tf_oneclick_child"
    issue = {
        "issue_type": "charset_issue",
        "severity": "warning",
        "location": f"{schema}.{parent}",
        "table_name": parent,
        "message": "Table uses a legacy charset.",
        "suggestion": "Convert table charset/collation after FK-safe review.",
        "blocking": False,
    }
    contract = {
        "issue_index": 0,
        "tables": [parent, child],
        "fk_order": [parent, child],
        "target_charset": "utf8mb4",
        "target_collation": "utf8mb4_0900_ai_ci",
        "rollback_sql": [
            f"ALTER TABLE `{schema}`.`{child}` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
            f"ALTER TABLE `{schema}`.`{parent}` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
        ],
    }
    applied_fix = {
        "issue_type": "charset_issue",
        "strategy": "charset_collation_fk_safe",
        "schema": schema,
        "tables": [parent, child],
        "target_charset": "utf8mb4",
        "target_collation": "utf8mb4_0900_ai_ci",
        "sql": [
            f"ALTER TABLE `{schema}`.`{parent}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
            f"ALTER TABLE `{schema}`.`{child}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
        ],
        "rollback_sql": contract["rollback_sql"],
        "fk_order": [parent, child],
        "success": True,
    }
    return {
        "issue": 140,
        "git_sha": "abcdef123456",
        "source_type": "local_mysql_container",
        "feature_flags": {
            "oneclick_ui_enabled": True,
            "oneclick_real_execution_enabled": True,
        },
        "service_hello": {
            "capabilities": [
                "oneclick.run",
                "oneclick.preflight",
                "oneclick.analyze",
                "oneclick.recommend",
                "oneclick.derive_charset_contracts",
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
        "derivation": {
            "command": "oneclick.derive_charset_contracts",
            "success": True,
            "source": "live_mysql_information_schema",
            "schema": schema,
            "payload_had_table_facts": False,
            "payload_had_issues": False,
            "issues_count": 1,
            "contracts_count": 1,
            "issues": [issue],
            "contracts": [contract],
        },
        "pyqt_payload": {
            "builder": "OneClickMigrationWorker._core_payload",
            "dry_run": False,
            "backup_confirmed": True,
            "included_derived_issues": True,
            "included_charset_contracts": True,
            "issues_count": 1,
            "contracts_count": 1,
        },
        "run": {
            "command": "oneclick.run",
            "dry_run": False,
            "backup_confirmed": True,
            "success": True,
            "schema": schema,
            "attempted_fix_types": ["charset_issue"],
            "attempted_strategies": ["charset_collation_fk_safe"],
            "disallowed_fix_attempts": [],
            "applied_fixes": [applied_fix],
        },
        "before": {
            "tables": [
                {"schema": schema, "table": parent, "charset": "utf8mb3", "collation": "utf8mb3_general_ci"},
                {"schema": schema, "table": child, "charset": "utf8mb3", "collation": "utf8mb3_general_ci"},
            ],
            "foreign_keys": [
                {
                    "schema": schema,
                    "table": child,
                    "referenced_table": parent,
                    "constraint": "fk_child_parent",
                }
            ],
        },
        "after": {
            "tables": [
                {"schema": schema, "table": parent, "charset": "utf8mb4", "collation": "utf8mb4_0900_ai_ci"},
                {"schema": schema, "table": child, "charset": "utf8mb4", "collation": "utf8mb4_0900_ai_ci"},
            ],
            "foreign_keys_valid": True,
            "unrelated_tables_unchanged": True,
        },
        "rollback": {
            "metadata_captured": True,
            "tables": [
                {"schema": schema, "table": parent, "charset": "utf8mb3", "collation": "utf8mb3_general_ci"},
                {"schema": schema, "table": child, "charset": "utf8mb3", "collation": "utf8mb3_general_ci"},
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
    report = tmp_path / "oneclick-charset-derivation-evidence.json"
    report.write_text(json.dumps(evidence), encoding="utf-8")
    return report


def test_oneclick_charset_derivation_validator_accepts_archived_pyqt_shape(tmp_path):
    validator = _load_validator()
    report = _write_report(tmp_path, _archived_evidence())

    assert validator.validate_report(report) == {
        "issue": 140,
        "schema": "tf_oneclick_derive_charset",
        "derived_contracts": 1,
        "applied_fixes": 1,
        "tables": 2,
    }


def test_oneclick_charset_derivation_evidence_requires_derive_command(tmp_path):
    validator = _load_validator()
    evidence = _archived_evidence()
    evidence["service_hello"]["capabilities"].remove("oneclick.derive_charset_contracts")
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="derive"):
        validator.validate_report(report)


def test_oneclick_charset_derivation_evidence_requires_pyqt_payload_inclusion(tmp_path):
    validator = _load_validator()
    evidence = _archived_evidence()
    evidence["pyqt_payload"]["included_charset_contracts"] = False
    report = _write_report(tmp_path, evidence)

    with pytest.raises(validator.EvidenceError, match="pyqt_payload"):
        validator.validate_report(report)


def test_regression_gate_can_require_oneclick_charset_derivation_evidence():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "rust-core-regression-gate.ps1"
    text = gate.read_text(encoding="utf-8")

    assert "RUST_CORE_REQUIRE_ONECLICK_CHARSET_DERIVATION_EVIDENCE" in text
    assert "validate-oneclick-charset-derivation-evidence.py" in text
    assert "reports/oneclick_readiness/oneclick-charset-derivation-evidence.json" in text
