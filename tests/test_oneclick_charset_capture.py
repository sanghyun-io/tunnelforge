import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_capture():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture-oneclick-charset-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_oneclick_charset_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def test_oneclick_charset_capture_rejects_unsafe_seed_identifiers():
    capture = _load_capture()

    with pytest.raises(ValueError, match="unsafe One-Click schema"):
        capture._require_safe_oneclick_identifier("prod", "schema")

    with pytest.raises(ValueError, match="unsafe One-Click table"):
        capture._require_safe_oneclick_identifier("tf_oneclick_child;DROP", "table")


def test_oneclick_charset_capture_builds_validator_backed_report(tmp_path):
    capture = _load_capture()
    validator = _load_validator()
    schema = "tf_oneclick_charset"
    parent = "tf_oneclick_parent"
    child = "tf_oneclick_child"
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
                "issue_type": "charset_issue",
                "strategy": "charset_collation_fk_safe",
                "schema": schema,
                "tables": [parent, child],
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "sql": [
                    (
                        f"ALTER TABLE `{schema}`.`{parent}` CONVERT TO CHARACTER SET "
                        "utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                    ),
                    (
                        f"ALTER TABLE `{schema}`.`{child}` CONVERT TO CHARACTER SET "
                        "utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                    ),
                ],
                "rollback_sql": [
                    (
                        f"ALTER TABLE `{schema}`.`{child}` CONVERT TO CHARACTER SET "
                        "utf8mb3 COLLATE utf8mb3_general_ci;"
                    ),
                    (
                        f"ALTER TABLE `{schema}`.`{parent}` CONVERT TO CHARACTER SET "
                        "utf8mb3 COLLATE utf8mb3_general_ci;"
                    ),
                ],
                "fk_order": [parent, child],
                "success": True,
            }
        ],
    }
    before_tables = [
        {"schema": schema, "table": parent, "charset": "utf8mb3", "collation": "utf8mb3_general_ci"},
        {"schema": schema, "table": child, "charset": "utf8mb3", "collation": "utf8mb3_general_ci"},
    ]
    after_tables = [
        {"schema": schema, "table": parent, "charset": "utf8mb4", "collation": "utf8mb4_0900_ai_ci"},
        {"schema": schema, "table": child, "charset": "utf8mb4", "collation": "utf8mb4_0900_ai_ci"},
    ]
    foreign_keys = [
        {
            "schema": schema,
            "table": child,
            "referenced_table": parent,
            "constraint": "fk_child_parent",
        }
    ]

    report = capture.build_evidence_report(
        git_sha="abcdef123456",
        service_hello=service_hello,
        schema=schema,
        target_charset="utf8mb4",
        target_collation="utf8mb4_0900_ai_ci",
        apply_result=apply_result,
        before_tables=before_tables,
        after_tables=after_tables,
        rollback_tables=before_tables,
        foreign_keys=foreign_keys,
    )

    report_path = tmp_path / "oneclick-charset-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert validator.validate_report(report_path) == {
        "issue": 139,
        "schema": schema,
        "applied_fixes": 1,
        "tables": 2,
    }


def test_oneclick_charset_capture_rejects_unsafe_capture_scope():
    capture = _load_capture()

    with pytest.raises(ValueError, match="unsafe One-Click schema"):
        capture.capture_oneclick_charset(
            host="127.0.0.1",
            port=3406,
            user="root",
            password="test",
            schema="prod",
            tables=["tf_oneclick_parent", "tf_oneclick_child"],
        )


def test_oneclick_charset_capture_cli_fails_closed_without_traceback(monkeypatch, capsys):
    capture = _load_capture()
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture-oneclick-charset-evidence.py", "--schema", "tf_oneclick_charset"],
    )

    assert capture.main() == 2

    output = capsys.readouterr().err
    assert "Rust Core charset/collation real execution is not implemented yet" in output
    assert "Traceback" not in output
