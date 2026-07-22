import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_charset_validator_uses_same_v2_disabled_truth_table(tmp_path):
    real_tests = _load(ROOT / "tests" / "test_oneclick_real_execution_evidence.py", "real_evidence_fixture")
    validator = _load(ROOT / "scripts" / "validate-oneclick-charset-evidence.py", "charset_validator")
    report = real_tests.mutation_report(issue=139, exact=False, fence=True)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert validator.validate_report(path) == {"issue": 139, "actions": 1, "status": "disabled"}


@pytest.mark.parametrize("key", ["issues", "charset_contracts", "contracts", "steps"])
def test_charset_validator_rejects_all_client_remediation_fields(tmp_path, key):
    real_tests = _load(ROOT / "tests" / "test_oneclick_real_execution_evidence.py", "real_evidence_fixture_client")
    validator = _load(ROOT / "scripts" / "validate-oneclick-charset-evidence.py", "charset_validator_client")
    report = real_tests.mutation_report(issue=139)
    report[key] = []
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(validator.EvidenceError):
        validator.validate_report(path)
