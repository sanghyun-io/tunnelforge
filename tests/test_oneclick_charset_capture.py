import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fixtures():
    module = _module(ROOT / "tests" / "test_oneclick_dry_run_evidence.py", "oneclick_v2_fixture")
    return module.hello, module.public_plan


def test_charset_capture_uses_plan_then_one_disabled_apply(monkeypatch):
    capture = _module(ROOT / "scripts" / "capture-oneclick-charset-evidence.py", "charset_capture")
    hello, public_plan = _fixtures()
    calls = []
    class Disabled(RuntimeError): code = "oneclick_apply_disabled"
    class Facade:
        def __init__(self): self.client = type("Client", (), {"shutdown": lambda _self: None})()
        def hello(self): return hello()
        def plan_oneclick(self, endpoint, schema):
            calls.append("plan"); plan = public_plan(schema)
            return {"plan": plan, "approval": {"approval_version": 1, "plan_version": 1, "target_identity": plan["target_identity"], "remediation_profile": plan["remediation_profile"], "snapshot_hash": plan["snapshot_hash"], "plan_hash": plan["plan_hash"]}}
        def apply_oneclick_plan(self, *args, **kwargs): calls.append("apply"); raise Disabled("disabled")
    monkeypatch.setattr(capture, "_load_db_core_types", lambda: (Facade, lambda **kwargs: kwargs))
    monkeypatch.setattr(capture, "current_git_sha", lambda: "abcdef123456")
    report = capture.capture_oneclick_charset(
        host="127.0.0.1", port=3406, user="root", password="test",
        schema="tf_oneclick_charset", tables=["tf_oneclick_parent"],
    )
    assert calls == ["plan", "apply"]
    assert report["plan"] == public_plan("tf_oneclick_charset")
    assert report["approval"].get("actions") is None
    assert report["apply"]["error_code"] == "oneclick_apply_disabled"


def test_charset_capture_reraises_indeterminate_without_retry(monkeypatch):
    capture = _module(ROOT / "scripts" / "capture-oneclick-charset-evidence.py", "charset_capture_error")
    hello, public_plan = _fixtures()
    calls = []
    class Failure(RuntimeError): code = "oneclick_outcome_indeterminate"
    error = Failure("indeterminate")
    class Facade:
        def __init__(self): self.client = type("Client", (), {"shutdown": lambda _self: None})()
        def hello(self): return hello()
        def plan_oneclick(self, endpoint, schema):
            plan = public_plan(schema)
            return {"plan": plan, "approval": {"approval_version": 1, "plan_version": 1, "target_identity": plan["target_identity"], "remediation_profile": plan["remediation_profile"], "snapshot_hash": plan["snapshot_hash"], "plan_hash": plan["plan_hash"]}}
        def apply_oneclick_plan(self, *args, **kwargs): calls.append("apply"); raise error
    monkeypatch.setattr(capture, "_load_db_core_types", lambda: (Facade, lambda **kwargs: kwargs))
    with pytest.raises(Failure) as raised:
        capture.capture_oneclick_charset(
            host="127.0.0.1", port=3406, user="root", password="test",
            schema="tf_oneclick_charset", tables=["tf_oneclick_parent"],
        )
    assert raised.value is error
    assert calls == ["apply"]
