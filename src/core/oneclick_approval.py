"""Immutable validation models for Rust One-Click plans and approvals."""
import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


_U16_MAX = (1 << 16) - 1
_U32_MAX = (1 << 32) - 1
_ACTION_TYPES = frozenset({"engine_innodb", "charset_fk_safe"})
_ACTION_FACTS_DOMAIN = b"tunnelforge.oneclick.action-facts.v1\0"
_SNAPSHOT_DOMAIN = b"tunnelforge.oneclick.snapshot.v1\0"
_PLAN_DOMAIN = b"tunnelforge.oneclick.plan.v1\0"
_FIXED_PROFILE = {
    "profile_version": 1,
    "profile_id": "mysql-utf8mb4-0900-v1",
    "target_charset": "utf8mb4",
    "target_collation": "utf8mb4_0900_ai_ci",
}


class OneClickPlanValidationError(ValueError):
    """Raised when a public plan or approval is not exact and canonical."""


class _WireModel:
    def to_dict(self) -> Dict[str, Any]:
        return _to_wire(self)


def _to_wire(value: Any) -> Any:
    if isinstance(value, ColumnDefault):
        return value.to_wire()
    if is_dataclass(value):
        return {field.name: _to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_to_wire(item) for item in value]
    return value


def _compact_json(document: Any) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_hash(domain: bytes, document: Any) -> str:
    return hashlib.sha256(domain + _compact_json(document)).hexdigest()


def _path(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def _fail(path: str, message: str) -> None:
    raise OneClickPlanValidationError(f"{path}: {message}")


def _reject_secret_keys(value: Any, path: str = "plan") -> None:
    if type(value) is dict:
        for key, nested in value.items():
            if type(key) is not str:
                _fail(path, "object keys must be strings")
            normalized = key.lower().replace("-", "_")
            if (
                normalized in {"connection", "dsn", "pwd", "passwd"}
                or "password" in normalized
                or "credential" in normalized
                or "secret" in normalized
                or "token" in normalized
                or "private_key" in normalized
                or "api_key" in normalized
            ):
                _fail(_path(path, key), "secret or connection key is prohibited")
            _reject_secret_keys(nested, _path(path, key))
    elif type(value) is list:
        for index, nested in enumerate(value):
            _reject_secret_keys(nested, f"{path}[{index}]")


def _object(value: Any, expected_keys: Sequence[str], path: str) -> Dict[str, Any]:
    if type(value) is not dict:
        _fail(path, "must be an object")
    actual = set(value)
    expected = set(expected_keys)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        detail = []
        if unknown:
            detail.append(f"unknown keys {unknown}")
        if missing:
            detail.append(f"missing keys {missing}")
        _fail(path, "; ".join(detail))
    return value


def _list(value: Any, path: str) -> list:
    if type(value) is not list:
        _fail(path, "must be an array")
    return value


def _string(value: Any, path: str, *, text: bool = True) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if text and (not value.strip() or "\0" in value):
        _fail(path, "must be non-empty text without NUL")
    return value


def _optional_string(value: Any, path: str, *, text: bool = True) -> Optional[str]:
    if value is None:
        return None
    return _string(value, path, text=text)


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _unsigned(value: Any, path: str, maximum: int = _U32_MAX) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail(path, f"must be an unsigned integer no greater than {maximum}")
    return value


def _version(value: Any, path: str) -> int:
    parsed = _unsigned(value, path)
    if parsed != 1:
        _fail(path, "unsupported version")
    return parsed


def _lower_sha256(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if len(parsed) != 64 or any(character not in "0123456789abcdef" for character in parsed):
        _fail(path, "must be a lowercase SHA-256 hex digest")
    return parsed


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _require_strict_order(values: Sequence[Any], key, path: str) -> None:
    keys = [key(value) for value in values]
    if any(left >= right for left, right in zip(keys, keys[1:])):
        _fail(path, "must be in canonical order without duplicates")


def _one_terminal_statement(value: str) -> bool:
    statement = value.strip()
    if not statement:
        return False
    quote = None
    terminal_separator = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif character in "`'\"":
            quote = character
        elif character == ";":
            if terminal_separator is not None:
                return False
            terminal_separator = index
        elif terminal_separator is not None and not character.isspace():
            return False
        index += 1
    return quote is None and terminal_separator == len(statement) - 1


@dataclass(frozen=True)
class OneClickRoute(_WireModel):
    host: str
    port: int


@dataclass(frozen=True)
class OneClickTargetIdentity(_WireModel):
    engine: str
    route: OneClickRoute
    server_uuid: str
    authenticated_user: str
    schema: str


@dataclass(frozen=True)
class OneClickRemediationProfile(_WireModel):
    profile_version: int
    profile_id: str
    target_charset: str
    target_collation: str


@dataclass(frozen=True)
class ColumnDefault:
    kind: str
    value: Optional[str] = None

    def to_wire(self) -> Any:
        if self.kind in {"absent", "null"}:
            return self.kind
        return {self.kind: self.value}


@dataclass(frozen=True)
class ActionColumnFact(_WireModel):
    ordinal_position: int
    name: str
    column_type: str
    nullable: bool
    default: ColumnDefault
    charset: Optional[str]
    collation: Optional[str]
    generated_expression: Optional[str]
    generated_stored: Optional[bool]


@dataclass(frozen=True)
class ActionIndexColumnFact(_WireModel):
    ordinal_position: int
    column_name: Optional[str]
    expression: Optional[str]
    prefix_length: Optional[int]


@dataclass(frozen=True)
class ActionIndexFact(_WireModel):
    name: str
    unique: bool
    index_type: str
    visible: bool
    columns: Tuple[ActionIndexColumnFact, ...]


@dataclass(frozen=True)
class ActionTableDefinitionFact(_WireModel):
    schema: str
    table: str
    engine: Optional[str]
    charset: Optional[str]
    collation: Optional[str]
    columns: Tuple[ActionColumnFact, ...]
    indexes: Tuple[ActionIndexFact, ...]


@dataclass(frozen=True)
class ActionForeignKeyColumnFact(_WireModel):
    ordinal_position: int
    column_name: str
    referenced_column_name: str


@dataclass(frozen=True)
class ActionForeignKeyFact(_WireModel):
    constraint_schema: str
    constraint_name: str
    table_schema: str
    table_name: str
    referenced_table_schema: str
    referenced_table_name: str
    match_option: str
    update_rule: str
    delete_rule: str
    columns: Tuple[ActionForeignKeyColumnFact, ...]


@dataclass(frozen=True)
class ActionFactsDocument(_WireModel):
    action_facts_version: int
    action_type: str
    tables: Tuple[ActionTableDefinitionFact, ...]
    foreign_keys: Tuple[ActionForeignKeyFact, ...]


@dataclass(frozen=True)
class OneClickActionStateExpectation(_WireModel):
    facts: ActionFactsDocument
    facts_hash: str


@dataclass(frozen=True)
class OneClickApplyAction(_WireModel):
    ordinal: int
    action_type: str
    issue_type: str
    strategy: str
    schema: str
    tables: Tuple[str, ...]
    sql: str
    rollback_sql: Optional[str]
    target_charset: Optional[str]
    target_collation: Optional[str]
    expected_pre_facts: OneClickActionStateExpectation
    expected_post_facts: OneClickActionStateExpectation


@dataclass(frozen=True)
class OneClickInspectionFact(_WireModel):
    issue_type: str
    severity: str
    object_kind: str
    schema: str
    table: Optional[str]
    column: Optional[str]


@dataclass(frozen=True)
class OneClickSnapshotDocument(_WireModel):
    snapshot_version: int
    schema: str
    inspection_facts: Tuple[OneClickInspectionFact, ...]
    table_definitions: Tuple[ActionTableDefinitionFact, ...]
    foreign_keys: Tuple[ActionForeignKeyFact, ...]


@dataclass(frozen=True)
class OneClickApproval(_WireModel):
    approval_version: int
    plan_version: int
    target_identity: OneClickTargetIdentity
    remediation_profile: OneClickRemediationProfile
    snapshot_hash: str
    plan_hash: str

    @classmethod
    def parse(cls, value: Any) -> "OneClickApproval":
        if type(value) is cls:
            value = value.to_dict()
        _reject_secret_keys(value, "approval")
        data = _object(
            value,
            (
                "approval_version",
                "plan_version",
                "target_identity",
                "remediation_profile",
                "snapshot_hash",
                "plan_hash",
            ),
            "approval",
        )
        approval = cls(
            approval_version=_version(data["approval_version"], "approval.approval_version"),
            plan_version=_version(data["plan_version"], "approval.plan_version"),
            target_identity=_parse_identity(data["target_identity"], "approval.target_identity"),
            remediation_profile=_parse_profile(
                data["remediation_profile"],
                "approval.remediation_profile",
            ),
            snapshot_hash=_lower_sha256(data["snapshot_hash"], "approval.snapshot_hash"),
            plan_hash=_lower_sha256(data["plan_hash"], "approval.plan_hash"),
        )
        _validate_identity(approval.target_identity, "approval.target_identity")
        _validate_fixed_profile(approval.remediation_profile, "approval.remediation_profile")
        return approval


@dataclass(frozen=True)
class OneClickPlan(_WireModel):
    plan_version: int
    target_identity: OneClickTargetIdentity
    remediation_profile: OneClickRemediationProfile
    snapshot: OneClickSnapshotDocument
    snapshot_hash: str
    actions: Tuple[OneClickApplyAction, ...]
    plan_hash: str

    @classmethod
    def parse(cls, value: Any) -> "OneClickPlan":
        _reject_secret_keys(value)
        data = _object(
            value,
            (
                "plan_version",
                "target_identity",
                "remediation_profile",
                "snapshot",
                "snapshot_hash",
                "actions",
                "plan_hash",
            ),
            "plan",
        )
        plan = cls(
            plan_version=_version(data["plan_version"], "plan.plan_version"),
            target_identity=_parse_identity(data["target_identity"], "plan.target_identity"),
            remediation_profile=_parse_profile(
                data["remediation_profile"],
                "plan.remediation_profile",
            ),
            snapshot=_parse_snapshot(data["snapshot"], "plan.snapshot"),
            snapshot_hash=_lower_sha256(data["snapshot_hash"], "plan.snapshot_hash"),
            actions=tuple(
                _parse_action(item, f"plan.actions[{index}]")
                for index, item in enumerate(_list(data["actions"], "plan.actions"))
            ),
            plan_hash=_lower_sha256(data["plan_hash"], "plan.plan_hash"),
        )
        _validate_plan(plan)
        return plan

    def approval(self) -> OneClickApproval:
        return OneClickApproval(
            approval_version=1,
            plan_version=self.plan_version,
            target_identity=self.target_identity,
            remediation_profile=self.remediation_profile,
            snapshot_hash=self.snapshot_hash,
            plan_hash=self.plan_hash,
        )


@dataclass(frozen=True)
class OneClickCapabilities:
    exact_plan_enabled: bool
    strong_fence_proven: bool

    @classmethod
    def from_hello(cls, hello: Mapping[str, Any]) -> "OneClickCapabilities":
        if not isinstance(hello, Mapping):
            return cls(False, False)
        exact = hello.get("oneclick_exact_plan_enabled")
        fence = hello.get("oneclick_strong_fence_proven")
        return cls(type(exact) is bool and exact, type(fence) is bool and fence)

    @property
    def apply_enabled(self) -> bool:
        return self.exact_plan_enabled and self.strong_fence_proven


def normalize_oneclick_schema(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "\0" in value
    ):
        raise OneClickPlanValidationError(
            "schema: must be a non-empty exact string without surrounding whitespace or NUL"
        )
    return value


def _parse_identity(value: Any, path: str) -> OneClickTargetIdentity:
    data = _object(
        value,
        ("engine", "route", "server_uuid", "authenticated_user", "schema"),
        path,
    )
    route_path = _path(path, "route")
    route_data = _object(data["route"], ("host", "port"), route_path)
    return OneClickTargetIdentity(
        engine=_string(data["engine"], _path(path, "engine")),
        route=OneClickRoute(
            host=_string(route_data["host"], _path(route_path, "host")),
            port=_unsigned(route_data["port"], _path(route_path, "port"), _U16_MAX),
        ),
        server_uuid=_string(data["server_uuid"], _path(path, "server_uuid")),
        authenticated_user=_string(
            data["authenticated_user"],
            _path(path, "authenticated_user"),
        ),
        schema=normalize_oneclick_schema(data["schema"]),
    )


def _parse_profile(value: Any, path: str) -> OneClickRemediationProfile:
    data = _object(
        value,
        ("profile_version", "profile_id", "target_charset", "target_collation"),
        path,
    )
    return OneClickRemediationProfile(
        profile_version=_version(data["profile_version"], _path(path, "profile_version")),
        profile_id=_string(data["profile_id"], _path(path, "profile_id")),
        target_charset=_string(data["target_charset"], _path(path, "target_charset")),
        target_collation=_string(
            data["target_collation"],
            _path(path, "target_collation"),
        ),
    )


def _parse_default(value: Any, path: str) -> ColumnDefault:
    if type(value) is str:
        if value not in {"absent", "null"}:
            _fail(path, "keyword must be absent or null")
        return ColumnDefault(value)
    data = _object(value, tuple(value.keys()) if type(value) is dict else (), path)
    if len(data) != 1:
        _fail(path, "must contain exactly literal or expression")
    kind = next(iter(data))
    if kind not in {"literal", "expression"}:
        _fail(path, "must contain exactly literal or expression")
    return ColumnDefault(kind, _string(data[kind], _path(path, kind), text=False))


def _parse_column(value: Any, path: str) -> ActionColumnFact:
    data = _object(
        value,
        (
            "ordinal_position",
            "name",
            "column_type",
            "nullable",
            "default",
            "charset",
            "collation",
            "generated_expression",
            "generated_stored",
        ),
        path,
    )
    generated_stored = data["generated_stored"]
    if generated_stored is not None:
        generated_stored = _boolean(generated_stored, _path(path, "generated_stored"))
    return ActionColumnFact(
        ordinal_position=_unsigned(data["ordinal_position"], _path(path, "ordinal_position")),
        name=_string(data["name"], _path(path, "name")),
        column_type=_string(data["column_type"], _path(path, "column_type")),
        nullable=_boolean(data["nullable"], _path(path, "nullable")),
        default=_parse_default(data["default"], _path(path, "default")),
        charset=_optional_string(data["charset"], _path(path, "charset")),
        collation=_optional_string(data["collation"], _path(path, "collation")),
        generated_expression=_optional_string(
            data["generated_expression"],
            _path(path, "generated_expression"),
        ),
        generated_stored=generated_stored,
    )


def _parse_index_column(value: Any, path: str) -> ActionIndexColumnFact:
    data = _object(
        value,
        ("ordinal_position", "column_name", "expression", "prefix_length"),
        path,
    )
    prefix = data["prefix_length"]
    if prefix is not None:
        prefix = _unsigned(prefix, _path(path, "prefix_length"))
    return ActionIndexColumnFact(
        ordinal_position=_unsigned(data["ordinal_position"], _path(path, "ordinal_position")),
        column_name=_optional_string(data["column_name"], _path(path, "column_name")),
        expression=_optional_string(data["expression"], _path(path, "expression")),
        prefix_length=prefix,
    )


def _parse_index(value: Any, path: str) -> ActionIndexFact:
    data = _object(value, ("name", "unique", "index_type", "visible", "columns"), path)
    return ActionIndexFact(
        name=_string(data["name"], _path(path, "name")),
        unique=_boolean(data["unique"], _path(path, "unique")),
        index_type=_string(data["index_type"], _path(path, "index_type")),
        visible=_boolean(data["visible"], _path(path, "visible")),
        columns=tuple(
            _parse_index_column(item, f"{path}.columns[{index}]")
            for index, item in enumerate(_list(data["columns"], _path(path, "columns")))
        ),
    )


def _parse_table(value: Any, path: str) -> ActionTableDefinitionFact:
    data = _object(
        value,
        ("schema", "table", "engine", "charset", "collation", "columns", "indexes"),
        path,
    )
    return ActionTableDefinitionFact(
        schema=_string(data["schema"], _path(path, "schema")),
        table=_string(data["table"], _path(path, "table")),
        engine=_optional_string(data["engine"], _path(path, "engine")),
        charset=_optional_string(data["charset"], _path(path, "charset")),
        collation=_optional_string(data["collation"], _path(path, "collation")),
        columns=tuple(
            _parse_column(item, f"{path}.columns[{index}]")
            for index, item in enumerate(_list(data["columns"], _path(path, "columns")))
        ),
        indexes=tuple(
            _parse_index(item, f"{path}.indexes[{index}]")
            for index, item in enumerate(_list(data["indexes"], _path(path, "indexes")))
        ),
    )


def _parse_fk_column(value: Any, path: str) -> ActionForeignKeyColumnFact:
    data = _object(
        value,
        ("ordinal_position", "column_name", "referenced_column_name"),
        path,
    )
    return ActionForeignKeyColumnFact(
        ordinal_position=_unsigned(data["ordinal_position"], _path(path, "ordinal_position")),
        column_name=_string(data["column_name"], _path(path, "column_name")),
        referenced_column_name=_string(
            data["referenced_column_name"],
            _path(path, "referenced_column_name"),
        ),
    )


def _parse_fk(value: Any, path: str) -> ActionForeignKeyFact:
    data = _object(
        value,
        (
            "constraint_schema",
            "constraint_name",
            "table_schema",
            "table_name",
            "referenced_table_schema",
            "referenced_table_name",
            "match_option",
            "update_rule",
            "delete_rule",
            "columns",
        ),
        path,
    )
    return ActionForeignKeyFact(
        constraint_schema=_string(data["constraint_schema"], _path(path, "constraint_schema")),
        constraint_name=_string(data["constraint_name"], _path(path, "constraint_name")),
        table_schema=_string(data["table_schema"], _path(path, "table_schema")),
        table_name=_string(data["table_name"], _path(path, "table_name")),
        referenced_table_schema=_string(
            data["referenced_table_schema"],
            _path(path, "referenced_table_schema"),
        ),
        referenced_table_name=_string(
            data["referenced_table_name"],
            _path(path, "referenced_table_name"),
        ),
        match_option=_string(data["match_option"], _path(path, "match_option")),
        update_rule=_string(data["update_rule"], _path(path, "update_rule")),
        delete_rule=_string(data["delete_rule"], _path(path, "delete_rule")),
        columns=tuple(
            _parse_fk_column(item, f"{path}.columns[{index}]")
            for index, item in enumerate(_list(data["columns"], _path(path, "columns")))
        ),
    )


def _parse_facts(value: Any, path: str) -> ActionFactsDocument:
    data = _object(
        value,
        ("action_facts_version", "action_type", "tables", "foreign_keys"),
        path,
    )
    action_type = _string(data["action_type"], _path(path, "action_type"))
    if action_type not in _ACTION_TYPES:
        _fail(_path(path, "action_type"), "unsupported action type")
    facts = ActionFactsDocument(
        action_facts_version=_version(
            data["action_facts_version"],
            _path(path, "action_facts_version"),
        ),
        action_type=action_type,
        tables=tuple(
            _parse_table(item, f"{path}.tables[{index}]")
            for index, item in enumerate(_list(data["tables"], _path(path, "tables")))
        ),
        foreign_keys=tuple(
            _parse_fk(item, f"{path}.foreign_keys[{index}]")
            for index, item in enumerate(
                _list(data["foreign_keys"], _path(path, "foreign_keys"))
            )
        ),
    )
    if not facts.tables:
        _fail(_path(path, "tables"), "must not be empty")
    _validate_tables_and_fks(facts.tables, facts.foreign_keys, path)
    return facts


def _parse_expectation(value: Any, path: str) -> OneClickActionStateExpectation:
    data = _object(value, ("facts", "facts_hash"), path)
    expectation = OneClickActionStateExpectation(
        facts=_parse_facts(data["facts"], _path(path, "facts")),
        facts_hash=_lower_sha256(data["facts_hash"], _path(path, "facts_hash")),
    )
    expected = _domain_hash(_ACTION_FACTS_DOMAIN, expectation.facts.to_dict())
    if expectation.facts_hash != expected:
        _fail(_path(path, "facts_hash"), "does not match canonical facts")
    return expectation


def _parse_action(value: Any, path: str) -> OneClickApplyAction:
    data = _object(
        value,
        (
            "ordinal",
            "action_type",
            "issue_type",
            "strategy",
            "schema",
            "tables",
            "sql",
            "rollback_sql",
            "target_charset",
            "target_collation",
            "expected_pre_facts",
            "expected_post_facts",
        ),
        path,
    )
    action_type = _string(data["action_type"], _path(path, "action_type"))
    if action_type not in _ACTION_TYPES:
        _fail(_path(path, "action_type"), "unsupported action type")
    tables = tuple(
        _string(item, f"{path}.tables[{index}]")
        for index, item in enumerate(_list(data["tables"], _path(path, "tables")))
    )
    sql = _string(data["sql"], _path(path, "sql"))
    rollback_sql = _optional_string(data["rollback_sql"], _path(path, "rollback_sql"))
    if not _one_terminal_statement(sql):
        _fail(_path(path, "sql"), "must contain one terminal SQL statement")
    if rollback_sql is not None and not _one_terminal_statement(rollback_sql):
        _fail(_path(path, "rollback_sql"), "must contain one terminal SQL statement")
    return OneClickApplyAction(
        ordinal=_unsigned(data["ordinal"], _path(path, "ordinal")),
        action_type=action_type,
        issue_type=_string(data["issue_type"], _path(path, "issue_type")),
        strategy=_string(data["strategy"], _path(path, "strategy")),
        schema=normalize_oneclick_schema(data["schema"]),
        tables=tables,
        sql=sql,
        rollback_sql=rollback_sql,
        target_charset=_optional_string(
            data["target_charset"],
            _path(path, "target_charset"),
        ),
        target_collation=_optional_string(
            data["target_collation"],
            _path(path, "target_collation"),
        ),
        expected_pre_facts=_parse_expectation(
            data["expected_pre_facts"],
            _path(path, "expected_pre_facts"),
        ),
        expected_post_facts=_parse_expectation(
            data["expected_post_facts"],
            _path(path, "expected_post_facts"),
        ),
    )


def _parse_inspection(value: Any, path: str) -> OneClickInspectionFact:
    data = _object(
        value,
        ("issue_type", "severity", "object_kind", "schema", "table", "column"),
        path,
    )
    return OneClickInspectionFact(
        issue_type=_string(data["issue_type"], _path(path, "issue_type")),
        severity=_string(data["severity"], _path(path, "severity")),
        object_kind=_string(data["object_kind"], _path(path, "object_kind")),
        schema=normalize_oneclick_schema(data["schema"]),
        table=_optional_string(data["table"], _path(path, "table")),
        column=_optional_string(data["column"], _path(path, "column")),
    )


def _parse_snapshot(value: Any, path: str) -> OneClickSnapshotDocument:
    data = _object(
        value,
        ("snapshot_version", "schema", "inspection_facts", "table_definitions", "foreign_keys"),
        path,
    )
    snapshot = OneClickSnapshotDocument(
        snapshot_version=_version(data["snapshot_version"], _path(path, "snapshot_version")),
        schema=normalize_oneclick_schema(data["schema"]),
        inspection_facts=tuple(
            _parse_inspection(item, f"{path}.inspection_facts[{index}]")
            for index, item in enumerate(
                _list(data["inspection_facts"], _path(path, "inspection_facts"))
            )
        ),
        table_definitions=tuple(
            _parse_table(item, f"{path}.table_definitions[{index}]")
            for index, item in enumerate(
                _list(data["table_definitions"], _path(path, "table_definitions"))
            )
        ),
        foreign_keys=tuple(
            _parse_fk(item, f"{path}.foreign_keys[{index}]")
            for index, item in enumerate(
                _list(data["foreign_keys"], _path(path, "foreign_keys"))
            )
        ),
    )
    _validate_tables_and_fks(snapshot.table_definitions, snapshot.foreign_keys, path)
    _require_strict_order(
        snapshot.inspection_facts,
        lambda fact: (
            _utf8(fact.issue_type),
            _utf8(fact.severity),
            _utf8(fact.object_kind),
            _utf8(fact.schema),
            _utf8(fact.table or ""),
            _utf8(fact.column or ""),
        ),
        _path(path, "inspection_facts"),
    )
    table_names = {(table.schema, table.table) for table in snapshot.table_definitions}
    for fact in snapshot.inspection_facts:
        if fact.schema != snapshot.schema or fact.table is None:
            _fail(
                _path(path, "inspection_facts"),
                "facts must identify a table in the snapshot schema",
            )
        if (fact.schema, fact.table) not in table_names:
            _fail(_path(path, "inspection_facts"), "fact table is absent from table_definitions")
    if any(table.schema != snapshot.schema for table in snapshot.table_definitions):
        _fail(_path(path, "table_definitions"), "table schema must match snapshot schema")
    if any(
        foreign_key.constraint_schema != snapshot.schema
        or foreign_key.table_schema != snapshot.schema
        or foreign_key.referenced_table_schema != snapshot.schema
        for foreign_key in snapshot.foreign_keys
    ):
        _fail(_path(path, "foreign_keys"), "foreign-key schemas must match snapshot schema")
    return snapshot


def _validate_identity(identity: OneClickTargetIdentity, path: str) -> None:
    if identity.engine != "mysql":
        _fail(_path(path, "engine"), "must be mysql")


def _validate_fixed_profile(profile: OneClickRemediationProfile, path: str) -> None:
    if profile.to_dict() != _FIXED_PROFILE:
        _fail(path, "must equal the fixed remediation profile")


def _validate_tables_and_fks(
    tables: Tuple[ActionTableDefinitionFact, ...],
    foreign_keys: Tuple[ActionForeignKeyFact, ...],
    path: str,
) -> None:
    _require_strict_order(
        tables,
        lambda table: (_utf8(table.schema), _utf8(table.table)),
        _path(path, "tables"),
    )
    table_columns = {}
    for table_index, table in enumerate(tables):
        table_path = f"{path}.tables[{table_index}]"
        column_names = set()
        for index, column in enumerate(table.columns, 1):
            if column.ordinal_position != index:
                _fail(_path(table_path, "columns"), "ordinals must be contiguous from one")
            if column.name in column_names:
                _fail(_path(table_path, "columns"), "column names must be unique")
            column_names.add(column.name)
            generated = column.generated_expression is not None
            if generated != (column.generated_stored is not None):
                _fail(_path(table_path, "columns"), "generated fields must be present together")
        _require_strict_order(
            table.indexes,
            lambda index: _utf8(index.name),
            _path(table_path, "indexes"),
        )
        for index_index, index_fact in enumerate(table.indexes):
            index_path = f"{table_path}.indexes[{index_index}]"
            if not index_fact.columns:
                _fail(_path(index_path, "columns"), "must not be empty")
            for position, member in enumerate(index_fact.columns, 1):
                if member.ordinal_position != position:
                    _fail(_path(index_path, "columns"), "ordinals must be contiguous from one")
                named = member.column_name is not None
                expressed = member.expression is not None
                if named == expressed:
                    _fail(_path(index_path, "columns"), "must identify one column or expression")
                if member.prefix_length == 0:
                    _fail(_path(index_path, "prefix_length"), "must be positive")
                if named and member.column_name not in column_names:
                    _fail(_path(index_path, "column_name"), "must reference a table column")
        table_columns[(table.schema, table.table)] = column_names

    _require_strict_order(
        foreign_keys,
        lambda foreign_key: tuple(
            _utf8(item)
            for item in (
                foreign_key.constraint_schema,
                foreign_key.constraint_name,
                foreign_key.table_schema,
                foreign_key.table_name,
                foreign_key.referenced_table_schema,
                foreign_key.referenced_table_name,
            )
        ),
        _path(path, "foreign_keys"),
    )
    for fk_index, foreign_key in enumerate(foreign_keys):
        fk_path = f"{path}.foreign_keys[{fk_index}]"
        if not foreign_key.columns:
            _fail(_path(fk_path, "columns"), "must not be empty")
        if not (
            foreign_key.constraint_schema
            == foreign_key.table_schema
            == foreign_key.referenced_table_schema
        ):
            _fail(fk_path, "foreign-key schemas must match")
        source_columns = table_columns.get(
            (foreign_key.table_schema, foreign_key.table_name)
        )
        target_columns = table_columns.get(
            (foreign_key.referenced_table_schema, foreign_key.referenced_table_name)
        )
        if source_columns is None or target_columns is None:
            _fail(fk_path, "foreign key must reference declared tables")
        for position, member in enumerate(foreign_key.columns, 1):
            if member.ordinal_position != position:
                _fail(_path(fk_path, "columns"), "ordinals must be contiguous from one")
            if (
                member.column_name not in source_columns
                or member.referenced_column_name not in target_columns
            ):
                _fail(_path(fk_path, "columns"), "must reference declared columns")


def _validate_action_scope(action: OneClickApplyAction, path: str) -> None:
    if not action.tables:
        _fail(_path(path, "tables"), "must not be empty")
    _require_strict_order(action.tables, _utf8, _path(path, "tables"))
    for expectation_name, expectation in (
        ("expected_pre_facts", action.expected_pre_facts),
        ("expected_post_facts", action.expected_post_facts),
    ):
        facts = expectation.facts
        facts_path = f"{path}.{expectation_name}.facts"
        if facts.action_type != action.action_type:
            _fail(_path(facts_path, "action_type"), "must match the action type")
        allowed = set(action.tables)
        if any(
            table.schema != action.schema or table.table not in allowed
            for table in facts.tables
        ):
            _fail(_path(facts_path, "tables"), "facts must stay within action tables")
        if any(
            foreign_key.constraint_schema != action.schema
            or foreign_key.table_schema != action.schema
            or foreign_key.referenced_table_schema != action.schema
            or foreign_key.table_name not in allowed
            or foreign_key.referenced_table_name not in allowed
            for foreign_key in facts.foreign_keys
        ):
            _fail(_path(facts_path, "foreign_keys"), "facts must stay within action tables")


def _canonical_expectation(
    action_type: str,
    table: ActionTableDefinitionFact,
) -> OneClickActionStateExpectation:
    facts = ActionFactsDocument(1, action_type, (table,), ())
    return OneClickActionStateExpectation(
        facts=facts,
        facts_hash=_domain_hash(_ACTION_FACTS_DOMAIN, facts.to_dict()),
    )


def _apply_charset_profile(
    table: ActionTableDefinitionFact,
    profile: OneClickRemediationProfile,
) -> ActionTableDefinitionFact:
    columns = tuple(
        replace(
            column,
            charset=profile.target_charset,
            collation=profile.target_collation,
        )
        if column.charset is not None
        else column
        for column in table.columns
    )
    return replace(
        table,
        charset=profile.target_charset,
        collation=profile.target_collation,
        columns=columns,
    )


def _validate_canonical_actions(plan: OneClickPlan) -> None:
    deprecated = {
        fact.table
        for fact in plan.snapshot.inspection_facts
        if fact.issue_type == "deprecated_engine"
    }
    fk_tables = {
        table_name
        for foreign_key in plan.snapshot.foreign_keys
        for table_name in (foreign_key.table_name, foreign_key.referenced_table_name)
    }
    expected = []
    for table in plan.snapshot.table_definitions:
        if table.table in deprecated:
            expected.append(("engine_innodb", table.table))
    for table in plan.snapshot.table_definitions:
        if table.table not in fk_tables and (
            table.charset != plan.remediation_profile.target_charset
            or table.collation != plan.remediation_profile.target_collation
        ):
            expected.append(("charset_fk_safe", table.table))

    actual = []
    for index, action in enumerate(plan.actions):
        path = f"plan.actions[{index}]"
        if len(action.tables) != 1:
            _fail(_path(path, "tables"), "canonical actions target exactly one table")
        actual.append((action.action_type, action.tables[0]))
        if action.action_type == "engine_innodb":
            if (
                action.issue_type != "deprecated_engine"
                or action.strategy != "engine_innodb"
                or action.target_charset is not None
                or action.target_collation is not None
            ):
                _fail(path, "engine action metadata is not canonical")
        elif (
            action.issue_type != "charset_issue"
            or action.strategy != "charset_fk_safe"
            or action.target_charset != plan.remediation_profile.target_charset
            or action.target_collation != plan.remediation_profile.target_collation
        ):
            _fail(path, "charset action metadata is not canonical")
    if actual != expected:
        _fail("plan.actions", "do not match canonical snapshot action order")

    working = {table.table: table for table in plan.snapshot.table_definitions}
    for index, action in enumerate(plan.actions):
        path = f"plan.actions[{index}]"
        current = working[action.tables[0]]
        expected_pre = _canonical_expectation(action.action_type, current)
        if action.expected_pre_facts != expected_pre:
            _fail(_path(path, "expected_pre_facts"), "contains stale public facts")
        if action.action_type == "engine_innodb":
            updated = replace(current, engine="InnoDB")
        else:
            updated = _apply_charset_profile(current, plan.remediation_profile)
        expected_post = _canonical_expectation(action.action_type, updated)
        if action.expected_post_facts != expected_post:
            _fail(_path(path, "expected_post_facts"), "contains stale public facts")
        working[action.tables[0]] = updated


def _validate_inspection_facts(plan: OneClickPlan) -> None:
    tables = {table.table: table for table in plan.snapshot.table_definitions}
    expected_charset = {
        table.table
        for table in plan.snapshot.table_definitions
        if table.charset != plan.remediation_profile.target_charset
        or table.collation != plan.remediation_profile.target_collation
    }
    actual_charset = set()
    for fact in plan.snapshot.inspection_facts:
        if fact.severity != "warning" or fact.object_kind != "table" or fact.column is not None:
            _fail(
                "plan.snapshot.inspection_facts",
                "only canonical warning table facts are allowed",
            )
        table = tables[fact.table]
        if fact.issue_type == "deprecated_engine":
            if table.engine is None or table.engine.lower() == "innodb":
                _fail("plan.snapshot.inspection_facts", "deprecated engine fact is inconsistent")
        elif fact.issue_type == "charset_issue":
            if table.table not in expected_charset:
                _fail("plan.snapshot.inspection_facts", "charset fact is inconsistent")
            actual_charset.add(table.table)
        else:
            _fail("plan.snapshot.inspection_facts", "unsupported issue type")
    if actual_charset != expected_charset:
        _fail("plan.snapshot.inspection_facts", "charset facts do not cover the snapshot")


def _validate_plan(plan: OneClickPlan) -> None:
    _validate_identity(plan.target_identity, "plan.target_identity")
    _validate_fixed_profile(plan.remediation_profile, "plan.remediation_profile")
    if plan.target_identity.schema != plan.snapshot.schema:
        _fail("plan.target_identity.schema", "must match snapshot schema")
    expected_snapshot_hash = _domain_hash(_SNAPSHOT_DOMAIN, plan.snapshot.to_dict())
    if plan.snapshot_hash != expected_snapshot_hash:
        _fail("plan.snapshot_hash", "does not match canonical snapshot")
    _validate_inspection_facts(plan)
    for index, action in enumerate(plan.actions, 1):
        action_path = f"plan.actions[{index - 1}]"
        if action.ordinal != index:
            _fail(_path(action_path, "ordinal"), "must be contiguous from one")
        if action.schema != plan.snapshot.schema:
            _fail(_path(action_path, "schema"), "must match snapshot schema")
        _validate_action_scope(action, action_path)
    _validate_canonical_actions(plan)
    hash_document = {
        "plan_version": plan.plan_version,
        "target_identity": plan.target_identity.to_dict(),
        "remediation_profile": plan.remediation_profile.to_dict(),
        "snapshot_hash": plan.snapshot_hash,
        "actions": [action.to_dict() for action in plan.actions],
    }
    if plan.plan_hash != _domain_hash(_PLAN_DOMAIN, hash_document):
        _fail("plan.plan_hash", "does not match the canonical plan")


__all__ = [
    "OneClickApproval",
    "OneClickCapabilities",
    "OneClickPlan",
    "OneClickPlanValidationError",
    "normalize_oneclick_schema",
]
