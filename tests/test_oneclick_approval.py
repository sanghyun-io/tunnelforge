import copy
import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError

import pytest


ACTION_FACTS_DOMAIN = b"tunnelforge.oneclick.action-facts.v1\0"
SNAPSHOT_DOMAIN = b"tunnelforge.oneclick.snapshot.v1\0"
PLAN_DOMAIN = b"tunnelforge.oneclick.plan.v1\0"


def _approval_module():
    return importlib.import_module("src.core.oneclick_approval")


def _domain_hash(domain, document):
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain + encoded).hexdigest()


def _column():
    return {
        "ordinal_position": 1,
        "name": "id",
        "column_type": "int",
        "nullable": False,
        "default": "absent",
        "charset": None,
        "collation": None,
        "generated_expression": None,
        "generated_stored": None,
    }


def _table(engine, charset, collation):
    return {
        "schema": "app",
        "table": "legacy",
        "engine": engine,
        "charset": charset,
        "collation": collation,
        "columns": [_column()],
        "indexes": [
            {
                "name": "PRIMARY",
                "unique": True,
                "index_type": "BTREE",
                "visible": True,
                "columns": [
                    {
                        "ordinal_position": 1,
                        "column_name": "id",
                        "expression": None,
                        "prefix_length": None,
                    }
                ],
            }
        ],
    }


def _expectation(action_type, table):
    facts = {
        "action_facts_version": 1,
        "action_type": action_type,
        "tables": [table],
        "foreign_keys": [],
    }
    return {
        "facts": facts,
        "facts_hash": _domain_hash(ACTION_FACTS_DOMAIN, facts),
    }


def _valid_plan_payload():
    snapshot = {
        "snapshot_version": 1,
        "schema": "app",
        "inspection_facts": [
            {
                "issue_type": "charset_issue",
                "severity": "warning",
                "object_kind": "table",
                "schema": "app",
                "table": "legacy",
                "column": None,
            },
            {
                "issue_type": "deprecated_engine",
                "severity": "warning",
                "object_kind": "table",
                "schema": "app",
                "table": "legacy",
                "column": None,
            },
        ],
        "table_definitions": [_table("MyISAM", "utf8mb3", "utf8mb3_general_ci")],
        "foreign_keys": [],
    }
    actions = [
        {
            "ordinal": 1,
            "action_type": "engine_innodb",
            "issue_type": "deprecated_engine",
            "strategy": "engine_innodb",
            "schema": "app",
            "tables": ["legacy"],
            "sql": "ALTER TABLE `app`.`legacy` ENGINE=InnoDB;",
            "rollback_sql": "ALTER TABLE `app`.`legacy` ENGINE=MyISAM;",
            "target_charset": None,
            "target_collation": None,
            "expected_pre_facts": _expectation(
                "engine_innodb",
                _table("MyISAM", "utf8mb3", "utf8mb3_general_ci"),
            ),
            "expected_post_facts": _expectation(
                "engine_innodb",
                _table("InnoDB", "utf8mb3", "utf8mb3_general_ci"),
            ),
        },
        {
            "ordinal": 2,
            "action_type": "charset_fk_safe",
            "issue_type": "charset_issue",
            "strategy": "charset_fk_safe",
            "schema": "app",
            "tables": ["legacy"],
            "sql": (
                "ALTER TABLE `app`.`legacy` CONVERT TO CHARACTER SET "
                "utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
            ),
            "rollback_sql": (
                "ALTER TABLE `app`.`legacy` CONVERT TO CHARACTER SET "
                "utf8mb3 COLLATE utf8mb3_general_ci;"
            ),
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci",
            "expected_pre_facts": _expectation(
                "charset_fk_safe",
                _table("InnoDB", "utf8mb3", "utf8mb3_general_ci"),
            ),
            "expected_post_facts": _expectation(
                "charset_fk_safe",
                _table("InnoDB", "utf8mb4", "utf8mb4_0900_ai_ci"),
            ),
        },
    ]
    plan = {
        "plan_version": 1,
        "target_identity": {
            "engine": "mysql",
            "route": {"host": "127.0.0.1", "port": 3306},
            "server_uuid": "12345678-1234-1234-1234-123456789abc",
            "authenticated_user": "app@localhost",
            "schema": "app",
        },
        "remediation_profile": {
            "profile_version": 1,
            "profile_id": "mysql-utf8mb4-0900-v1",
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci",
        },
        "snapshot": snapshot,
        "snapshot_hash": _domain_hash(SNAPSHOT_DOMAIN, snapshot),
        "actions": actions,
        "plan_hash": "",
    }
    hash_document = {
        "plan_version": plan["plan_version"],
        "target_identity": plan["target_identity"],
        "remediation_profile": plan["remediation_profile"],
        "snapshot_hash": plan["snapshot_hash"],
        "actions": plan["actions"],
    }
    plan["plan_hash"] = _domain_hash(PLAN_DOMAIN, hash_document)
    return plan


def _rehash_plan(plan):
    plan["plan_hash"] = _domain_hash(
        PLAN_DOMAIN,
        {
            "plan_version": plan["plan_version"],
            "target_identity": plan["target_identity"],
            "remediation_profile": plan["remediation_profile"],
            "snapshot_hash": plan["snapshot_hash"],
            "actions": plan["actions"],
        },
    )


def test_oneclick_plan_strictly_parses_full_public_plan_as_immutable_model():
    module = _approval_module()
    payload = _valid_plan_payload()

    plan = module.OneClickPlan.parse(payload)

    assert plan.to_dict() == payload
    assert isinstance(plan.actions, tuple)
    assert isinstance(plan.snapshot.table_definitions, tuple)
    assert plan.actions[0].expected_pre_facts.facts.tables[0].columns[0].nullable is False
    with pytest.raises(FrozenInstanceError):
        plan.plan_version = 2
    exported = plan.to_dict()
    exported["actions"][0]["sql"] = "SELECT 1;"
    assert plan.actions[0].sql != "SELECT 1;"


def test_oneclick_plan_matches_rust_action_facts_golden_hash():
    plan = _approval_module().OneClickPlan.parse(_valid_plan_payload())

    assert (
        plan.actions[0].expected_pre_facts.facts_hash
        == "82f25f33ba164c4c2ca938ab3e519561bb881bae6cfa54d6e268b09223c698a5"
    )


def test_oneclick_approval_copies_only_identity_profile_and_two_hashes():
    module = _approval_module()
    plan = module.OneClickPlan.parse(_valid_plan_payload())

    approval = plan.approval()

    assert approval.to_dict() == {
        "approval_version": 1,
        "plan_version": plan.plan_version,
        "target_identity": plan.target_identity.to_dict(),
        "remediation_profile": plan.remediation_profile.to_dict(),
        "snapshot_hash": plan.snapshot_hash,
        "plan_hash": plan.plan_hash,
    }
    approval_keys = set(approval.to_dict())
    assert approval_keys.isdisjoint({"snapshot", "actions", "facts", "sql", "password"})


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda plan: plan.update(plan_version=True), "plan_version"),
        (
            lambda plan: plan["target_identity"]["route"].update(port="3306"),
            "port",
        ),
        (
            lambda plan: plan["snapshot"]["table_definitions"][0]["columns"][0].update(
                nullable=1
            ),
            "nullable",
        ),
        (
            lambda plan: plan["snapshot"]["table_definitions"][0]["columns"][0].update(
                default={"literal": "0", "extra": True}
            ),
            "default",
        ),
        (lambda plan: plan["actions"][0].update(sql=["SELECT 1;"]), "sql"),
    ],
)
def test_oneclick_plan_rejects_non_exact_json_types(mutate, match):
    plan = _valid_plan_payload()
    mutate(plan)

    with pytest.raises(ValueError, match=match):
        _approval_module().OneClickPlan.parse(plan)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan.update(unexpected=True),
        lambda plan: plan["snapshot"]["table_definitions"][0].update(
            password="credential-secret"
        ),
        lambda plan: plan["actions"][0]["expected_pre_facts"]["facts"].update(
            connection={"password": "credential-secret"}
        ),
    ],
)
def test_oneclick_plan_recursively_rejects_unknown_and_secret_keys(mutate):
    plan = _valid_plan_payload()
    mutate(plan)

    with pytest.raises(ValueError):
        _approval_module().OneClickPlan.parse(plan)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["snapshot"]["inspection_facts"].reverse(),
        lambda plan: plan["actions"][1].update(ordinal=3),
        lambda plan: plan["actions"][0]["expected_pre_facts"]["facts"]["tables"][
            0
        ]["columns"][0].update(ordinal_position=2),
    ],
)
def test_oneclick_plan_rejects_noncanonical_order_and_ordinals(mutate):
    plan = _valid_plan_payload()
    mutate(plan)

    with pytest.raises(ValueError):
        _approval_module().OneClickPlan.parse(plan)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan.update(snapshot_hash=plan["snapshot_hash"].upper()),
        lambda plan: plan.update(plan_hash="0" * 63),
        lambda plan: plan["actions"][0]["expected_pre_facts"].update(
            facts_hash="0" * 64
        ),
        lambda plan: plan["snapshot"]["table_definitions"][0].update(engine="InnoDB"),
    ],
)
def test_oneclick_plan_rejects_nonlowercase_or_mismatched_domain_hashes(mutate):
    plan = _valid_plan_payload()
    mutate(plan)

    with pytest.raises(ValueError):
        _approval_module().OneClickPlan.parse(plan)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "ALTER TABLE `app`.`legacy` ENGINE=InnoDB",
        "SELECT 1; SELECT 2;",
        "SELECT ';'; SELECT 2;",
    ],
)
def test_oneclick_plan_rejects_actions_without_one_terminal_sql_statement(sql):
    plan = _valid_plan_payload()
    plan["actions"][0]["sql"] = sql
    _rehash_plan(plan)

    with pytest.raises(ValueError, match="sql"):
        _approval_module().OneClickPlan.parse(plan)


def test_oneclick_plan_accepts_quoted_semicolon_in_one_terminal_statement():
    plan = _valid_plan_payload()
    plan["actions"][0]["sql"] = "SELECT ';' AS marker;   "
    _rehash_plan(plan)

    parsed = _approval_module().OneClickPlan.parse(plan)

    assert parsed.actions[0].sql == "SELECT ';' AS marker;   "


def test_oneclick_plan_rejects_action_facts_outside_declared_tables():
    plan = _valid_plan_payload()
    expectation = plan["actions"][0]["expected_pre_facts"]
    expectation["facts"]["tables"][0]["table"] = "ghost"
    expectation["facts_hash"] = _domain_hash(ACTION_FACTS_DOMAIN, expectation["facts"])
    _rehash_plan(plan)

    with pytest.raises(ValueError, match="tables"):
        _approval_module().OneClickPlan.parse(plan)


@pytest.mark.parametrize(
    ("expectation_name", "engine"),
    [
        ("expected_pre_facts", "InnoDB"),
        ("expected_post_facts", "MyISAM"),
    ],
)
def test_oneclick_plan_rejects_rehashed_stale_action_facts(
    expectation_name,
    engine,
):
    plan = _valid_plan_payload()
    expectation = plan["actions"][0][expectation_name]
    expectation["facts"]["tables"][0]["engine"] = engine
    expectation["facts_hash"] = _domain_hash(ACTION_FACTS_DOMAIN, expectation["facts"])
    _rehash_plan(plan)

    with pytest.raises(ValueError, match="stale"):
        _approval_module().OneClickPlan.parse(plan)


@pytest.mark.parametrize(
    ("hello", "expected"),
    [
        (
            {
                "oneclick_exact_plan_enabled": True,
                "oneclick_strong_fence_proven": False,
            },
            (True, False, False),
        ),
        (
            {
                "oneclick_exact_plan_enabled": 1,
                "oneclick_strong_fence_proven": "true",
            },
            (False, False, False),
        ),
        ({}, (False, False, False)),
    ],
)
def test_oneclick_capabilities_normalize_exact_booleans(hello, expected):
    capabilities = _approval_module().OneClickCapabilities.from_hello(hello)

    assert (
        capabilities.exact_plan_enabled,
        capabilities.strong_fence_proven,
        capabilities.apply_enabled,
    ) == expected


def test_oneclick_approval_parser_rejects_documents_actions_and_profile_override():
    module = _approval_module()
    approval = module.OneClickPlan.parse(_valid_plan_payload()).approval().to_dict()

    for prohibited in ("snapshot", "actions", "facts", "profile"):
        candidate = copy.deepcopy(approval)
        candidate[prohibited] = {}
        with pytest.raises(ValueError):
            module.OneClickApproval.parse(candidate)


def test_oneclick_approval_parser_revalidates_frozen_instances():
    module = _approval_module()
    approval = module.OneClickPlan.parse(_valid_plan_payload()).approval()
    forged = module.OneClickApproval(
        approval_version=approval.approval_version,
        plan_version=approval.plan_version,
        target_identity=approval.target_identity,
        remediation_profile=approval.remediation_profile,
        snapshot_hash=approval.snapshot_hash,
        plan_hash="NOT-A-SHA256",
    )

    with pytest.raises(ValueError, match="plan_hash"):
        module.OneClickApproval.parse(forged)
