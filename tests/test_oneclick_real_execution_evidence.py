import copy
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


def mutation_report(issue=138, exact=False, fence=False):
    fixture = _load(ROOT / "tests" / "test_oneclick_dry_run_evidence.py", "oneclick_fixture_mutation")
    plan = fixture.public_plan("tf_oneclick_real_execution")
    return {
        "report_version": 2, "issue": issue, "git_sha": "abcdef123456",
        "source_type": "local_mysql_container", "mode": "apply_attempt",
        "service_hello": fixture.hello(exact, fence), "plan": plan,
        "approval": fixture.approval_for(plan),
        "apply": {"attempted": True, "request_count": 1, "success": False,
                  "error_code": "oneclick_apply_disabled"},
        "before": copy.deepcopy(plan["snapshot"]), "after": copy.deepcopy(plan["snapshot"]),
    }


def _validate(tmp_path, report):
    validator = _load(ROOT / "scripts" / "validate-oneclick-real-execution-evidence.py", "real_validator")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return validator, path


@pytest.mark.parametrize("exact,fence", [(False, False), (True, False), (False, True)])
def test_real_validator_accepts_disabled_only_when_a_gate_is_false(tmp_path, exact, fence):
    validator, path = _validate(tmp_path, mutation_report(exact=exact, fence=fence))
    assert validator.validate_report(path) == {"issue": 138, "actions": 1, "status": "disabled"}


def test_real_validator_allows_success_only_with_both_exact_capabilities(tmp_path):
    report = mutation_report(exact=True, fence=True)
    report["apply"] = {"attempted": True, "request_count": 1, "success": True}
    validator, path = _validate(tmp_path, report)
    assert validator.validate_report(path)["status"] == "success"
    report["service_hello"]["oneclick_strong_fence_proven"] = False
    validator, path = _validate(tmp_path, report)
    with pytest.raises(validator.EvidenceError, match="disabled"):
        validator.validate_report(path)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update({"report_version": 1}),
    lambda r: r["approval"].update({"snapshot": r["plan"]["snapshot"]}),
    lambda r: r["plan"]["actions"][0]["expected_post_facts"].update({"facts_hash": "0" * 64}),
    lambda r: r.update({"steps": []}),
    lambda r: r["apply"].update({"request_count": 2}),
    lambda r: r["plan"]["snapshot"].update({"api_token": "secret"}),
])
def test_real_validator_rejects_old_shape_stale_secret_client_or_retry(tmp_path, mutate):
    validator, path = _validate(tmp_path, mutation_report())
    report = mutation_report()
    mutate(report)
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(validator.EvidenceError):
        validator.validate_report(path)
