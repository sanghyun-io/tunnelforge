import importlib.util
import json
import sys
from pathlib import Path


class FakeDerivationCharsetFacade:
    def __init__(self, required_capabilities):
        self.required_capabilities = list(required_capabilities)
        self.queries = []
        self.derive_payload = None
        self.run_payload = None
        self.shutdown_called = False
        self._table_rows = [
            [
                {
                    "schema": "tf_oneclick_derive_charset",
                    "table": "tf_oneclick_parent",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
                {
                    "schema": "tf_oneclick_derive_charset",
                    "table": "tf_oneclick_child",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci",
                },
            ],
            [
                {
                    "schema": "tf_oneclick_derive_charset",
                    "table": "tf_oneclick_parent",
                    "charset": "utf8mb4",
                    "collation": "utf8mb4_0900_ai_ci",
                },
                {
                    "schema": "tf_oneclick_derive_charset",
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
                    "schema": "tf_oneclick_derive_charset",
                    "table": "tf_oneclick_child",
                    "referenced_table": "tf_oneclick_parent",
                    "constraint": "fk_child_parent",
                }
            ]
        raise AssertionError(f"unexpected query: {sql}")

    def derive_oneclick_charset_contracts(self, payload):
        self.derive_payload = payload
        schema = "tf_oneclick_derive_charset"
        return {
            "command": "oneclick.derive_charset_contracts",
            "success": True,
            "schema": schema,
            "issues": [
                {
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": f"{schema}.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": False,
                }
            ],
            "contracts": [
                {
                    "issue_index": 0,
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "rollback_sql": [
                        f"ALTER TABLE `{schema}`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        f"ALTER TABLE `{schema}`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                    ],
                }
            ],
        }

    def run_oneclick(self, payload, on_event=None):
        self.run_payload = payload
        schema = "tf_oneclick_derive_charset"
        applied_fix = {
            "issue_type": "charset_issue",
            "strategy": "charset_collation_fk_safe",
            "schema": schema,
            "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci",
            "sql": [
                f"ALTER TABLE `{schema}`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                f"ALTER TABLE `{schema}`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
            ],
            "rollback_sql": [
                f"ALTER TABLE `{schema}`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                f"ALTER TABLE `{schema}`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
            ],
            "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
            "success": True,
        }
        if on_event:
            on_event({
                "event": "execution",
                "dry_run": False,
                "success_count": 1,
                "fail_count": 0,
                "skip_count": 0,
                "disallowed_fix_attempts": [],
                "applied_fixes": [applied_fix],
            })
        return {
            "command": "oneclick.run",
            "success": True,
            "report": {"schema": schema, "success": True},
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
        / "capture-oneclick-charset-derivation-evidence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "capture_oneclick_charset_derivation_evidence",
        script,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def test_oneclick_charset_derivation_capture_uses_pyqt_payload_and_run_oneclick(tmp_path):
    capture = _load_capture()
    validator = _load_validator()
    facade = FakeDerivationCharsetFacade(validator.REQUIRED_CAPABILITIES)

    report = capture.capture_oneclick_charset_derivation(
        host="127.0.0.1",
        port=3406,
        user="root",
        password="test",
        schema="tf_oneclick_derive_charset",
        tables=["tf_oneclick_parent", "tf_oneclick_child"],
        facade=facade,
        git_sha="abcdef123456",
    )
    report_path = tmp_path / "oneclick-charset-derivation-evidence.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert validator.validate_report(report_path)["issue"] == 140
    assert facade.derive_payload["schema"] == "tf_oneclick_derive_charset"
    assert "table_facts" not in facade.derive_payload
    assert "issues" not in facade.derive_payload
    assert facade.run_payload["dry_run"] is False
    assert facade.run_payload["backup_confirmed"] is True
    assert facade.run_payload["issues"][0]["issue_type"] == "charset_issue"
    assert facade.run_payload["charset_contracts"][0]["target_collation"] == "utf8mb4_0900_ai_ci"
    assert facade.shutdown_called is True


def test_oneclick_charset_derivation_capture_cli_reports_failure_without_traceback(monkeypatch, capsys):
    capture = _load_capture()

    def fail_capture(**_kwargs):
        raise RuntimeError("synthetic derivation capture failure")

    monkeypatch.setattr(capture, "capture_oneclick_charset_derivation", fail_capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture-oneclick-charset-derivation-evidence.py",
            "--schema",
            "tf_oneclick_derive_charset",
        ],
    )

    assert capture.main() == 1

    output = capsys.readouterr().err
    assert "synthetic derivation capture failure" in output
    assert "Traceback" not in output
