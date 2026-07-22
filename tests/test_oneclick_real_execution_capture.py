import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fixtures():
    path = ROOT / "tests" / "test_oneclick_dry_run_evidence.py"
    spec = importlib.util.spec_from_file_location("oneclick_evidence_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.hello, module.public_plan


def _load_capture():
    path = ROOT / "scripts" / "capture-oneclick-real-execution-evidence.py"
    spec = importlib.util.spec_from_file_location("capture_oneclick_real", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_real_capture_records_only_definite_disabled_and_one_apply(monkeypatch):
    capture = _load_capture()
    hello, public_plan = _fixtures()
    calls = []

    class Disabled(RuntimeError):
        code = "oneclick_apply_disabled"

    class Facade:
        def __init__(self):
            self.client = type("Client", (), {"shutdown": lambda _self: None})()
        def hello(self): return hello()
        def plan_oneclick(self, endpoint, schema):
            calls.append("plan"); plan = public_plan(schema)
            return {"plan": plan, "approval": {"approval_version": 1, "plan_version": 1, "target_identity": plan["target_identity"], "remediation_profile": plan["remediation_profile"], "snapshot_hash": plan["snapshot_hash"], "plan_hash": plan["plan_hash"]}}
        def apply_oneclick_plan(self, endpoint, schema, backup_confirmed, approval):
            calls.append(("apply", backup_confirmed, approval)); raise Disabled("disabled")

    monkeypatch.setattr(capture, "_load_db_core_types", lambda: (Facade, lambda **kwargs: kwargs))
    monkeypatch.setattr(capture, "current_git_sha", lambda: "abcdef123456")
    report = capture.capture_oneclick_real_execution(
        host="127.0.0.1", port=3406, user="root", password="test",
        schema="tf_oneclick_real_execution", table="tf_oneclick_legacy_engine_table",
    )
    assert calls[0] == "plan"
    assert len(calls) == 2
    assert report["apply"] == {
        "attempted": True, "request_count": 1, "success": False,
        "error_code": "oneclick_apply_disabled",
    }
    assert report["before"] == report["after"] == public_plan("tf_oneclick_real_execution")["snapshot"]


@pytest.mark.parametrize("code", ["oneclick_outcome_indeterminate", "oneclick_plan_changed", None])
def test_real_capture_reraises_non_disabled_without_retry(monkeypatch, code):
    capture = _load_capture()
    hello, public_plan = _fixtures()
    calls = []
    class Failure(RuntimeError):
        pass
    error = Failure("no retry")
    if code is not None:
        error.code = code
    class Facade:
        def __init__(self): self.client = type("Client", (), {"shutdown": lambda _self: None})()
        def hello(self): return hello()
        def plan_oneclick(self, endpoint, schema):
            plan = public_plan(schema)
            return {"plan": plan, "approval": {"approval_version": 1, "plan_version": 1, "target_identity": plan["target_identity"], "remediation_profile": plan["remediation_profile"], "snapshot_hash": plan["snapshot_hash"], "plan_hash": plan["plan_hash"]}}
        def apply_oneclick_plan(self, *args, **kwargs): calls.append("apply"); raise error
    monkeypatch.setattr(capture, "_load_db_core_types", lambda: (Facade, lambda **kwargs: kwargs))
    with pytest.raises(Failure) as raised:
        capture.capture_oneclick_real_execution(
            host="127.0.0.1", port=3406, user="root", password="test",
            schema="tf_oneclick_real_execution", table="tf_oneclick_legacy_engine_table",
        )
    assert raised.value is error
    assert calls == ["apply"]
