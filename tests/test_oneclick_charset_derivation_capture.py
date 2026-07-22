import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_charset_derivation_capture_has_no_legacy_derivation_or_client_contracts(monkeypatch):
    capture = _load(ROOT / "scripts" / "capture-oneclick-charset-derivation-evidence.py", "derivation_capture")
    fixture = _load(ROOT / "tests" / "test_oneclick_dry_run_evidence.py", "oneclick_fixture_derivation")
    calls = []
    class Disabled(RuntimeError): code = "oneclick_apply_disabled"
    class Facade:
        def __init__(self): self.client = type("Client", (), {"shutdown": lambda _self: None})()
        def hello(self): return fixture.hello()
        def plan_oneclick(self, endpoint, schema):
            calls.append("plan"); plan = fixture.public_plan(schema)
            return {"plan": plan, "approval": fixture.approval_for(plan)}
        def apply_oneclick_plan(self, *args, **kwargs): calls.append("apply"); raise Disabled("disabled")
        def __getattr__(self, name):
            if "derive" in name or "run_oneclick" in name: raise AssertionError(name)
            raise AttributeError(name)
    monkeypatch.setattr(capture, "_load_db_core_types", lambda: (Facade, lambda **kwargs: kwargs))
    monkeypatch.setattr(capture, "current_git_sha", lambda: "abcdef123456")
    report = capture.capture_oneclick_charset_derivation(
        host="127.0.0.1", port=3406, user="root", password="test",
        schema="tf_oneclick_derive_charset", tables=["tf_oneclick_parent"],
    )
    assert calls == ["plan", "apply"]
    assert "derivation" not in report and "pyqt_payload" not in report
    assert report["apply"]["request_count"] == 1
