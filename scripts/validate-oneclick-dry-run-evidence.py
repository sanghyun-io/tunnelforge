#!/usr/bin/env python
"""Validate One-Click evidence v2 without accepting client remediation data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict


class EvidenceError(ValueError):
    pass


_LEGACY_COMMANDS = {
    "oneclick.run", "oneclick.apply", "oneclick.apply_fixes",
    "oneclick.derive_charset_contracts", "oneclick.recommend",
}
_FORBIDDEN_KEYS = {
    "issues", "charset_contracts", "contracts", "steps", "pyqt_payload",
    "derivation", "run", "retry", "retries",
}
_SECRET_PARTS = ("password", "passwd", "secret", "token", "credential", "private_key", "api_key")


def _mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _hash(domain: bytes, value: Any) -> str:
    return hashlib.sha256(
        domain + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise EvidenceError(f"{label} must be a lowercase SHA-256 hash")
    return value


def _git_sha(value: Any) -> str:
    if not isinstance(value, str) or not 7 <= len(value) <= 64 or any(char not in "0123456789abcdef" for char in value):
        raise EvidenceError("git_sha must be a lowercase git SHA")
    return value


def _reject_forbidden(value: Any, label: str = "report") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lower = str(key).lower()
            if lower in _FORBIDDEN_KEYS:
                raise EvidenceError(f"{label}.{key} is prohibited client remediation data")
            if any(part in lower for part in _SECRET_PARTS):
                raise EvidenceError(f"{label}.{key} contains a secret key")
            _reject_forbidden(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{label}[{index}]")
    elif isinstance(value, str) and value in _LEGACY_COMMANDS:
        raise EvidenceError(f"{label} contains legacy One-Click command {value}")


def _require_exact_keys(value: Dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise EvidenceError(f"{label} has an unsupported v2 shape")


def _validate_plan(plan_value: Any) -> Dict[str, Any]:
    plan = _mapping(plan_value, "plan")
    _require_exact_keys(plan, {
        "plan_version", "target_identity", "remediation_profile", "snapshot",
        "snapshot_hash", "actions", "plan_hash",
    }, "plan")
    if plan["plan_version"] != 1:
        raise EvidenceError("plan.plan_version must be 1")
    identity = _mapping(plan["target_identity"], "plan.target_identity")
    _require_exact_keys(identity, {"engine", "route", "server_uuid", "authenticated_user", "schema"}, "plan.target_identity")
    route = _mapping(identity["route"], "plan.target_identity.route")
    _require_exact_keys(route, {"host", "port"}, "plan.target_identity.route")
    profile = _mapping(plan["remediation_profile"], "plan.remediation_profile")
    _require_exact_keys(profile, {"profile_version", "profile_id", "target_charset", "target_collation"}, "plan.remediation_profile")
    snapshot = _mapping(plan["snapshot"], "plan.snapshot")
    _require_exact_keys(snapshot, {"snapshot_version", "schema", "inspection_facts", "table_definitions", "foreign_keys"}, "plan.snapshot")
    if identity["schema"] != snapshot["schema"]:
        raise EvidenceError("plan target identity schema must match snapshot")
    if _sha(plan["snapshot_hash"], "plan.snapshot_hash") != _hash(b"tunnelforge.oneclick.snapshot.v1\0", snapshot):
        raise EvidenceError("plan.snapshot_hash does not match public snapshot")
    actions = plan["actions"]
    if not isinstance(actions, list):
        raise EvidenceError("plan.actions must be an array")
    for ordinal, action_value in enumerate(actions, 1):
        action = _mapping(action_value, f"plan.actions[{ordinal - 1}]")
        _require_exact_keys(action, {
            "ordinal", "action_type", "issue_type", "strategy", "schema", "tables", "sql",
            "rollback_sql", "target_charset", "target_collation", "expected_pre_facts", "expected_post_facts",
        }, f"plan.actions[{ordinal - 1}]")
        if action["ordinal"] != ordinal or not isinstance(action["sql"], str) or not action["sql"].strip() or action["sql"].count(";") != 1:
            raise EvidenceError("plan actions must be ordered one-statement actions")
        for side in ("expected_pre_facts", "expected_post_facts"):
            expectation = _mapping(action[side], f"plan.actions[{ordinal - 1}].{side}")
            _require_exact_keys(expectation, {"facts", "facts_hash"}, f"plan.actions[{ordinal - 1}].{side}")
            facts = _mapping(expectation["facts"], f"plan.actions[{ordinal - 1}].{side}.facts")
            if facts.get("action_type") != action["action_type"]:
                raise EvidenceError("action fact type does not match action")
            if _sha(expectation["facts_hash"], f"plan.actions[{ordinal - 1}].{side}.facts_hash") != _hash(b"tunnelforge.oneclick.action-facts.v1\0", facts):
                raise EvidenceError("action facts hash does not match public facts")
    plan_hash_document = {
        "plan_version": plan["plan_version"], "target_identity": identity,
        "remediation_profile": profile, "snapshot_hash": plan["snapshot_hash"], "actions": actions,
    }
    if _sha(plan["plan_hash"], "plan.plan_hash") != _hash(b"tunnelforge.oneclick.plan.v1\0", plan_hash_document):
        raise EvidenceError("plan.plan_hash does not match public plan")
    return plan


def _validate_hello(value: Any) -> Dict[str, Any]:
    hello = _mapping(value, "service_hello")
    if hello.get("oneclick_exact_plan_enabled") not in (True, False) or type(hello.get("oneclick_exact_plan_enabled")) is not bool:
        raise EvidenceError("service_hello.oneclick_exact_plan_enabled must be boolean")
    if hello.get("oneclick_strong_fence_proven") not in (True, False) or type(hello.get("oneclick_strong_fence_proven")) is not bool:
        raise EvidenceError("service_hello.oneclick_strong_fence_proven must be boolean")
    capabilities = hello.get("capabilities")
    if not isinstance(capabilities, list) or "oneclick.plan" not in capabilities:
        raise EvidenceError("service_hello must advertise oneclick.plan")
    return hello


def _validate_approval(value: Any, plan: Dict[str, Any]) -> None:
    approval = _mapping(value, "approval")
    _require_exact_keys(approval, {
        "approval_version", "plan_version", "target_identity", "remediation_profile", "snapshot_hash", "plan_hash",
    }, "approval")
    if approval["approval_version"] != 1 or approval["plan_version"] != plan["plan_version"]:
        raise EvidenceError("approval version does not match plan")
    for key in ("target_identity", "remediation_profile", "snapshot_hash", "plan_hash"):
        if approval[key] != plan[key]:
            raise EvidenceError(f"approval.{key} must exactly match plan")


def validate_mutation_report(report_value: Any, issue: int) -> Dict[str, Any]:
    report = _mapping(report_value, "report")
    _reject_forbidden(report)
    _require_exact_keys(report, {
        "report_version", "issue", "git_sha", "source_type", "mode", "service_hello",
        "plan", "approval", "apply", "before", "after",
    }, "report")
    if report["report_version"] != 2 or report["issue"] != issue or report["mode"] != "apply_attempt":
        raise EvidenceError("report is not the expected v2 apply evidence")
    _git_sha(report["git_sha"])
    if report["source_type"] != "local_mysql_container":
        raise EvidenceError("source_type must be local_mysql_container")
    plan = _validate_plan(report["plan"])
    hello = _validate_hello(report["service_hello"])
    _validate_approval(report["approval"], plan)
    apply = _mapping(report["apply"], "apply")
    if apply.get("attempted") is not True or apply.get("request_count") != 1 or type(apply.get("success")) is not bool:
        raise EvidenceError("apply must record exactly one attempted request")
    gated = hello["oneclick_exact_plan_enabled"] is True and hello["oneclick_strong_fence_proven"] is True
    if apply["success"] is True:
        _require_exact_keys(apply, {"attempted", "request_count", "success"}, "apply")
        if not gated:
            raise EvidenceError("disabled apply evidence is required unless both exact-plan capability booleans are true")
        return {"issue": issue, "actions": len(plan["actions"]), "status": "success"}
    _require_exact_keys(apply, {"attempted", "request_count", "success", "error_code"}, "apply")
    if gated or apply.get("error_code") != "oneclick_apply_disabled":
        raise EvidenceError("only definite oneclick_apply_disabled evidence is accepted while apply is disabled")
    if report["before"] != plan["snapshot"] or report["after"] != plan["snapshot"] or report["before"] != report["after"]:
        raise EvidenceError("disabled apply evidence must prove unchanged before/after snapshot")
    return {"issue": issue, "actions": len(plan["actions"]), "status": "disabled"}


def validate_report(report_path: Path | str) -> Dict[str, Any]:
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid JSON: {exc}") from exc
    report = _mapping(report, "report")
    _reject_forbidden(report)
    _require_exact_keys(report, {"report_version", "issue", "git_sha", "source_type", "service_hello", "mode", "plan", "approval", "apply"}, "report")
    if report["report_version"] != 2 or report["issue"] != 137 or report["mode"] != "plan_preview":
        raise EvidenceError("report is not v2 plan preview evidence")
    _git_sha(report["git_sha"])
    if report["source_type"] != "local_mysql_container":
        raise EvidenceError("source_type must be local_mysql_container")
    _validate_hello(report["service_hello"])
    plan = _validate_plan(report["plan"])
    if report["approval"] is not None or report["apply"] != {"attempted": False, "request_count": 0}:
        raise EvidenceError("plan preview must not contain approval or apply attempts")
    return {"issue": 137, "actions": len(plan["actions"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", default="reports/oneclick_readiness/oneclick-dry-run-evidence.json")
    args = parser.parse_args()
    try:
        print(validate_report(args.report))
    except (OSError, EvidenceError) as exc:
        print(f"One-Click evidence failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
