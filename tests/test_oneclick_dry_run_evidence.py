import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def public_plan(schema="tf_oneclick_readiness"):
    facts = {
        "action_facts_version": 1,
        "action_type": "engine_innodb",
        "tables": [],
        "foreign_keys": [],
    }
    facts_hash = hashlib.sha256(
        b"tunnelforge.oneclick.action-facts.v1\0"
        + json.dumps(facts, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = {
        "snapshot_version": 1,
        "schema": schema,
        "inspection_facts": [],
        "table_definitions": [],
        "foreign_keys": [],
    }
    snapshot_hash = hashlib.sha256(
        b"tunnelforge.oneclick.snapshot.v1\0"
        + json.dumps(snapshot, separators=(",", ":")).encode()
    ).hexdigest()
    action = {
        "ordinal": 1,
        "action_type": "engine_innodb",
        "issue_type": "deprecated_engine",
        "strategy": "engine_innodb",
        "schema": schema,
        "tables": [],
        "sql": "SELECT 1;",
        "rollback_sql": None,
        "target_charset": None,
        "target_collation": None,
        "expected_pre_facts": {"facts": facts, "facts_hash": facts_hash},
        "expected_post_facts": {"facts": facts, "facts_hash": facts_hash},
    }
    identity = {
        "engine": "mysql",
        "route": {"host": "127.0.0.1", "port": 3406},
        "server_uuid": "11111111-1111-1111-1111-111111111111",
        "authenticated_user": "root@%",
        "schema": schema,
    }
    profile = {
        "profile_version": 1,
        "profile_id": "mysql-utf8mb4-0900-v1",
        "target_charset": "utf8mb4",
        "target_collation": "utf8mb4_0900_ai_ci",
    }
    plan_hash_document = {
        "plan_version": 1,
        "target_identity": identity,
        "remediation_profile": profile,
        "snapshot_hash": snapshot_hash,
        "actions": [action],
    }
    return {
        **plan_hash_document,
        "snapshot": snapshot,
        "plan_hash": hashlib.sha256(
            b"tunnelforge.oneclick.plan.v1\0"
            + json.dumps(plan_hash_document, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def hello(exact=False, fence=False):
    return {
        "capabilities": ["oneclick.plan"],
        "oneclick_exact_plan_enabled": exact,
        "oneclick_strong_fence_proven": fence,
    }


def approval_for(plan):
    return {
        "approval_version": 1,
        "plan_version": plan["plan_version"],
        "target_identity": copy.deepcopy(plan["target_identity"]),
        "remediation_profile": copy.deepcopy(plan["remediation_profile"]),
        "snapshot_hash": plan["snapshot_hash"],
        "plan_hash": plan["plan_hash"],
    }


def test_dry_run_report_is_v2_plan_preview_without_apply(tmp_path):
    capture = _load("capture-oneclick-dry-run-evidence.py")
    validator = _load("validate-oneclick-dry-run-evidence.py")
    report = capture.build_evidence_report(
        git_sha="abcdef123456",
        service_hello=hello(),
        plan=public_plan(),
    )

    assert report["report_version"] == 2
    assert report["mode"] == "plan_preview"
    assert report["approval"] is None
    assert report["apply"] == {"attempted": False, "request_count": 0}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert validator.validate_report(path) == {"issue": 137, "actions": 1}


def test_dry_run_capture_calls_plan_once_and_never_legacy(monkeypatch):
    capture = _load("capture-oneclick-dry-run-evidence.py")
    calls = []

    class Facade:
        def __init__(self):
            self.client = type("Client", (), {"shutdown": lambda _self: None})()

        def hello(self):
            return hello()

        def plan_oneclick(self, endpoint, schema):
            calls.append((endpoint, schema))
            plan = public_plan(schema)
            return {"plan": plan, "approval": approval_for(plan)}

        def __getattr__(self, name):
            if "oneclick" in name:
                raise AssertionError(f"legacy API called: {name}")
            raise AttributeError(name)

    monkeypatch.setattr(capture, "_load_db_core_types", lambda: (Facade, lambda **kwargs: kwargs))
    monkeypatch.setattr(capture, "current_git_sha", lambda: "abcdef123456")
    report = capture.capture_oneclick_dry_run(
        host="127.0.0.1", port=3406, user="root", password="test", schema="tf_oneclick_readiness"
    )

    assert len(calls) == 1
    assert report["plan"] == public_plan()


@pytest.mark.parametrize("mutate", [
    lambda report: report.update({"run": {"command": "oneclick.run"}}),
    lambda report: report["plan"]["actions"][0]["expected_pre_facts"].update({"facts_hash": "0" * 64}),
    lambda report: report["plan"]["target_identity"].update({"password": "secret"}),
    lambda report: report.update({"issues": []}),
    lambda report: report["apply"].update({"request_count": 1}),
])
def test_dry_run_validator_rejects_legacy_stale_secret_client_or_apply(tmp_path, mutate):
    validator = _load("validate-oneclick-dry-run-evidence.py")
    report = {
        "report_version": 2, "issue": 137, "git_sha": "abcdef123456",
        "source_type": "local_mysql_container", "service_hello": hello(),
        "mode": "plan_preview", "plan": public_plan(), "approval": None,
        "apply": {"attempted": False, "request_count": 0},
    }
    mutate(report)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(validator.EvidenceError):
        validator.validate_report(path)


def test_optional_dry_run_regression_gate_remains_wired():
    gate = (ROOT / "scripts" / "rust-core-regression-gate.ps1").read_text(encoding="utf-8")
    assert "RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE" in gate
    assert "validate-oneclick-dry-run-evidence.py" in gate
