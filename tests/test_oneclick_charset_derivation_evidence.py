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


def test_charset_derivation_validator_accepts_v2_disabled_plan_without_derivation(tmp_path):
    real = _load(ROOT / "tests" / "test_oneclick_real_execution_evidence.py", "real_fixture_derivation")
    validator = _load(ROOT / "scripts" / "validate-oneclick-charset-derivation-evidence.py", "derivation_validator")
    report = real.mutation_report(issue=140)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert validator.validate_report(path) == {"issue": 140, "actions": 1, "status": "disabled"}


@pytest.mark.parametrize("key", ["derivation", "pyqt_payload", "issues", "contracts"])
def test_charset_derivation_validator_rejects_legacy_client_evidence(tmp_path, key):
    real = _load(ROOT / "tests" / "test_oneclick_real_execution_evidence.py", "real_fixture_derivation_bad")
    validator = _load(ROOT / "scripts" / "validate-oneclick-charset-derivation-evidence.py", "derivation_validator_bad")
    report = real.mutation_report(issue=140)
    report[key] = {}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(validator.EvidenceError):
        validator.validate_report(path)
