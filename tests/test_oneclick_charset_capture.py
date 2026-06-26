import importlib.util
import json
import sys
from pathlib import Path

import pytest


class FakeCharsetFacade:
    def __init__(self, required_capabilities):
        self.required_capabilities = list(required_capabilities)
        self.queries = []
        self.apply_payload = None
        self.shutdown_called = False
        self._table_rows = [
            [
                {
                    "schema": "tf_oneclick_charset",
                    "table": "tf_oneclick_parent",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
                {
                    "schema": "tf_oneclick_charset",
                    "table": "tf_oneclick_child",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
            ],
            [
                {
                    "schema": "tf_oneclick_charset",
                    "table": "tf_oneclick_parent",
                    "charset": "utf8mb4",
                    "collation": "utf8mb4_0900_ai_ci",
                },
                {
                    "schema": "tf_oneclick_charset",
                    "table": "tf_oneclick_child",
                    "charset": "utf8mb4",
                    "collation": "utf8mb4_0900_ai_ci",
                },
            ],
        ]

    def hello(self):
        return {"capabilities": self.required_capabilities}

    def execute_query(self, endpoint, sql, params=None):
        self.queries.append((sql, list(params or []), endpoint.database))
        if "information_schema.TABLES" in sql:
            return self._table_rows.pop(0)
        if "information_schema.KEY_COLUMN_USAGE" in sql:
            return [
                {
                    "schema": "tf_oneclick_charset",
                    "table": "tf_oneclick_child",
                    "referenced_table": "tf_oneclick_parent",
                    "constraint": "fk_child_parent",
                }
            ]
        raise AssertionError(f"unexpected query: {sql}")

    def apply_oneclick_fixes(self, payload):
        self.apply_payload = payload
        schema = "tf_oneclick_charset"
        return {
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
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "sql": [
                        (
                            f"ALTER TABLE `{schema}`.`tf_oneclick_parent` CONVERT TO CHARACTER SET "
                            "utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                        ),
                        (
                            f"ALTER TABLE `{schema}`.`tf_oneclick_child` CONVERT TO CHARACTER SET "
                            "utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                        ),
                    ],
                    "rollback_sql": [
                        (
                            f"ALTER TABLE `{schema}`.`tf_oneclick_child` CONVERT TO CHARACTER SET "
                            "utf8mb3 COLLATE utf8mb3_general_ci;"
                        ),
                        (
                            f"ALTER TABLE `{schema}`.`tf_oneclick_parent` CONVERT TO CHARACTER SET "
                            "utf8mb3 COLLATE utf8mb3_general_ci;"
                        ),
                    ],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "success": True,
                }
            ],
        }

    @property
    def client(self):
        return self

    def shutdown(self):
        self.shutdown_called = True


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


def test_oneclick_charset_capture_orchestrates_validator_backed_live_report(tmp_path):
    capture = _load_capture()
    validator = _load_validator()
    facade = FakeCharsetFacade(validator.REQUIRED_CAPABILITIES)

    report = capture.capture_oneclick_charset(
        host="127.0.0.1",
        port=3406,
        user="root",
        password="test",
        schema="tf_oneclick_charset",
        tables=["tf_oneclick_parent", "tf_oneclick_child"],
        facade=facade,
        git_sha="abcdef123456",
    )

    report_path = tmp_path / "oneclick-charset-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert validator.validate_report(report_path) == {
        "issue": 139,
        "schema": "tf_oneclick_charset",
        "applied_fixes": 1,
        "tables": 2,
    }
    assert facade.apply_payload["dry_run"] is False
    assert facade.apply_payload["backup_confirmed"] is True
    assert facade.apply_payload["steps"][0]["selected_option"]["strategy"] == "charset_collation_fk_safe"
    assert facade.shutdown_called is True


def test_oneclick_charset_capture_cli_reports_failure_without_traceback(monkeypatch, capsys):
    capture = _load_capture()

    def fail_capture(**_kwargs):
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(capture, "capture_oneclick_charset", fail_capture)
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture-oneclick-charset-evidence.py", "--schema", "tf_oneclick_charset"],
    )

    assert capture.main() == 1

    output = capsys.readouterr().err
    assert "synthetic capture failure" in output
    assert "Traceback" not in output
